"""The replicate count an epoch runs, and where that count came from.

A duel's replicate count is resolved from three tiers, in order:

* the contract pins ``params["replicates"]`` in its tournament block;
* the epoch carries a measured A/A noise floor with a usable ``delta_std``,
  and the count is the smallest one whose minimum detectable effect is
  within the contract's ``promote_margin``
  (:func:`zicato.tournament.detectable_effect.replicates_for_margin`), clamped to
  at least the structure's default;
* the structure's own default (:func:`zicato.selection.registry
  .default_replicates_for`).

:func:`resolve_replicates` is the one reader of those tiers. The evolve loop
calls it at epoch open, once the floor is known, and threads the count to
:func:`zicato.selection.registry.make_strategy`; the instrument-health
reader calls it over the same persisted records to show the count the run
is operating under. Both see one answer because both call one function.

A derived count is a runtime record like the floor it came from: it is
stamped on the heartbeat's effective-settings record, never written into the
contract, so the contract hash does not move when the floor does.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from zicato.core.types import TournamentStructure
from zicato.selection.registry import default_replicates_for
from zicato.tournament.detectable_effect import (
    REPLICATE_SIZING_CAP,
    minimum_detectable_effect,
    replicates_for_margin,
)

#: The contract's tournament block pins ``replicates``.
SOURCE_CONTRACT = "contract scoring.json"
#: The count was derived from the epoch's measured noise floor.
SOURCE_NOISE_FLOOR = "derived from the noise floor"
#: The structure's own default applies.
SOURCE_STRUCTURE_DEFAULT = "structure default"

#: Every tier a replicate count can be attributed to.
REPLICATE_SOURCE_TIERS: tuple[str, ...] = (
    SOURCE_CONTRACT,
    SOURCE_NOISE_FLOOR,
    SOURCE_STRUCTURE_DEFAULT,
)


@dataclass(frozen=True, slots=True)
class ReplicateSetting:
    """The replicate count in effect for one epoch, with its provenance.

    Fields
    ------
    replicates:
        The count every duel of the epoch runs.
    source:
        The tier that decided it, one of :data:`REPLICATE_SOURCE_TIERS`.
    delta_std:
        The floor's draw-count-stable dispersion when the epoch carries a
        usable one, else ``None``. Carried whatever the source, because a
        racing rung resolves its cuts from it even under a pinned count.
    detectable_effect:
        The minimum detectable effect at ``replicates`` from ``delta_std``,
        or ``None`` when there is no usable floor.
    promote_margin:
        The contract margin the count was sized against, when known.
    note:
        Why the floor did not size the count, when it did not: the cap was
        exhausted, or the derived count fell below the structure default.
        ``None`` otherwise.
    """

    replicates: int
    source: str
    delta_std: float | None = None
    detectable_effect: float | None = None
    promote_margin: float | None = None
    note: str | None = None

    @property
    def under_powered(self) -> bool:
        """Whether the count's detectable effect exceeds the promote margin."""
        return (
            self.detectable_effect is not None
            and self.promote_margin is not None
            and self.detectable_effect > self.promote_margin
        )


def pinned_replicates(params: Mapping[str, Any]) -> int | None:
    """The contract's ``params["replicates"]`` as a count, or ``None`` when unpinned.

    Reads the value the way every strategy's ``__init__`` does: an integer
    at or above one. An absent, unparseable, or non-positive value is
    unpinned, so the structure default applies there too.
    """
    raw = params.get("replicates")
    if raw is None or isinstance(raw, bool):
        return None
    try:
        count = int(raw)
    except (TypeError, ValueError):
        return None
    return count if count >= 1 else None


def usable_delta_std(floor: Mapping[str, Any] | None) -> float | None:
    """The floor's ``delta_std`` when it is a positive number, else ``None``.

    A floor of ``0.0`` (a deterministic system under test) has nothing to
    size: any count resolves any margin, and the structure default stands.
    """
    if not isinstance(floor, Mapping):
        return None
    try:
        value = float(floor["delta_std"])
    except (KeyError, TypeError, ValueError):
        return None
    return value if value > 0.0 else None


def resolve_replicates(
    spec: TournamentStructure,
    *,
    floor: Mapping[str, Any] | None,
    promote_margin: float | None,
    cap: int = REPLICATE_SIZING_CAP,
) -> ReplicateSetting:
    """Resolve the replicate count for an epoch from the three tiers. Pure.

    ``spec`` is the frozen tournament block; ``floor`` the epoch's persisted
    :meth:`~zicato.tournament.calibration.NoiseFloor.to_json` record or
    ``None``; ``promote_margin`` the contract margin. A pinned count wins and
    is reported with the effect it resolves. Otherwise a usable floor sizes
    the count against the margin; a derived count below the structure default
    is lifted to the default, and a margin no count up to ``cap`` resolves
    leaves the default in force, each with a note saying so.
    """
    default = default_replicates_for(spec.structure)
    delta_std = usable_delta_std(floor)
    margin = None if promote_margin is None else float(promote_margin)

    def _effect(count: int) -> float | None:
        return None if delta_std is None else minimum_detectable_effect(delta_std, count)

    pinned = pinned_replicates(spec.params)
    if pinned is not None:
        return ReplicateSetting(
            replicates=pinned,
            source=SOURCE_CONTRACT,
            delta_std=delta_std,
            detectable_effect=_effect(pinned),
            promote_margin=margin,
        )
    if delta_std is None or margin is None:
        return ReplicateSetting(
            replicates=default,
            source=SOURCE_STRUCTURE_DEFAULT,
            delta_std=delta_std,
            detectable_effect=_effect(default),
            promote_margin=margin,
        )
    derived = replicates_for_margin(delta_std, margin, cap=cap)
    if derived is None:
        return ReplicateSetting(
            replicates=default,
            source=SOURCE_STRUCTURE_DEFAULT,
            delta_std=delta_std,
            detectable_effect=_effect(default),
            promote_margin=margin,
            note=(
                f"no replicate count up to {cap} resolves promote_margin {margin:.6g} "
                f"at delta_std {delta_std:.6g}; raise the margin"
            ),
        )
    if derived < default:
        return ReplicateSetting(
            replicates=default,
            source=SOURCE_STRUCTURE_DEFAULT,
            delta_std=delta_std,
            detectable_effect=_effect(default),
            promote_margin=margin,
            note=(
                f"the floor resolves promote_margin {margin:.6g} at {derived} replicates; "
                f"the {spec.structure} default of {default} is larger and applies"
            ),
        )
    return ReplicateSetting(
        replicates=derived,
        source=SOURCE_NOISE_FLOOR,
        delta_std=delta_std,
        detectable_effect=_effect(derived),
        promote_margin=margin,
    )


__all__ = [
    "REPLICATE_SOURCE_TIERS",
    "SOURCE_CONTRACT",
    "SOURCE_NOISE_FLOOR",
    "SOURCE_STRUCTURE_DEFAULT",
    "ReplicateSetting",
    "pinned_replicates",
    "resolve_replicates",
    "usable_delta_std",
]
