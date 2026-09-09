"""Tournament-structure types: decision/scope literals, match record, structure.

Split out of :mod:`zicato.core.types`; re-exported from there and from
:mod:`zicato.core` so existing import paths keep working.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

from zicato.core.constraints import KnobConstraint
from zicato.core.measurement import (
    EVIDENCE_REPLICATE_BASE,
    MeasurementPurpose,
    measurement_range,
    validate_measurement_interval,
)

# ---------------------------------------------------------------------------
# Tournament decision / structure
# ---------------------------------------------------------------------------


class TournamentDecision(StrEnum):
    """The tournament's decision about an experiment.

    * :attr:`PROMOTED` (``"promoted"``) — child wins; becomes the new
      lineage head.
    * :attr:`REJECTED` (``"rejected"``) — child loses or regresses a hard
      gate.
    * :attr:`DEFERRED` (``"deferred"``) — neither wins decisively; lineage
      head unchanged but the experiment is kept for analysis.

    A :class:`~enum.StrEnum`, so a member equals its lowercase wire token
    and serialises through ``json.dumps`` with no converter — the JSON
    output and contract hash are byte-identical to the prior ``Literal``.
    The three members are exactly the prior ``Literal`` tokens, so any
    value loaded from disk as a bare ``str`` still compares equal.
    """

    PROMOTED = "promoted"
    REJECTED = "rejected"
    DEFERRED = "deferred"


class ConfirmationStatus(StrEnum):
    """Whether a promotion requirement is disabled, satisfied, failed, or incomplete."""

    DISABLED = "disabled"
    SATISFIED = "satisfied"
    FAILED = "failed"
    INCOMPLETE = "incomplete"


def recorded_decision_token(outcome: Any) -> str | None:
    """Read the recorded tournament decision, preserving an absent outcome."""
    if outcome is None:
        return None
    if not isinstance(outcome, Mapping):
        raise ValueError("outcome must be an object or null")
    value = outcome["tournament_decision"]
    return None if value is None else TournamentDecision(value).value


class Side(StrEnum):
    """The tournament side a scheduled run belongs to.

    * :attr:`PARENT` (``"parent"``) — the champion / lineage head.
    * :attr:`CHILD` (``"child"``) — the challenger being evaluated.

    A :class:`~enum.StrEnum`, so a member equals its lowercase wire token
    and serialises identically to the bare string. These are the two
    gauntlet sides; non-gauntlet structures carry an opaque competitor
    generation id in the same ``side`` slot, so the slot's storage type
    stays ``str`` — this enum names only the two closed gauntlet tokens
    at the sites that produce them.
    """

    PARENT = "parent"
    CHILD = "child"


#: Granularity of the promote gate's pass-rate monotonicity check, gating
#: how a pass-rate movement rejects a challenger when
#: :attr:`ScoringWeights.pass_rate_monotonicity` is on:
#:
#: * ``"per_entry"`` (default, the default behaviour) — EVERY entry the
#:   champion passed must still pass on the challenger; any entry that
#:   flips champion-pass → challenger-fail rejects. The right policy when
#:   every board entry is a must-not-regress invariant (a regression
#:   suite).
#: * ``"aggregate"`` — reject only when the challenger's OVERALL pass-rate
#:   falls below the champion's (modulo a small float-noise tolerance). A
#:   challenger may trade individual entries as long as the net pass-rate
#:   holds or improves. The right policy for sampled evaluation boards
#:   where individual pass/fail is noisy and promotions should track the
#:   optimized aggregate.
#:
#: There is intentionally no ``"off"`` token: ``off`` is already expressed
#: by ``pass_rate_monotonicity=False``. Keeping the on/off switch a bool
#: and the granularity a separate field gives each setting one purpose.
PassRateMonotonicityScope = Literal["per_entry", "aggregate"]


#: Every tournament structure token a contract may name. ``"racing"``
#: is the shared scoring default; ``"gauntlet"`` is explicitly selectable;
#: the three in :data:`EXPERIMENTAL_TOURNAMENT_STRUCTURES` resolve only
#: under the opt-in named by :data:`EXPERIMENTAL_STRUCTURES_KEY`. The
#: tokens are the closed enum the loader validates against and the keys
#: the selection-strategy registries map to concrete strategy classes.
VALID_TOURNAMENT_STRUCTURES: tuple[str, ...] = (
    "gauntlet",
    "single_elim",
    "double_elim",
    "swiss",
    "racing",
)

#: The structures an operator must opt into. Each pairs challengers against
#: each other, so a candidate's fate depends on its draw; the second life a
#: losers' bracket buys is what ``replicates`` already buys, and Swiss
#: pairing is racing without the escalating board slice. None has a
#: measured case at zicato's field size of two to four candidates under an
#: expensive, noisy evaluator, so they stay available for an operator who
#: wants to try them and sit outside the default structure choice.
EXPERIMENTAL_TOURNAMENT_STRUCTURES: frozenset[str] = frozenset(
    {"single_elim", "double_elim", "swiss"}
)

#: The ``scoring.json`` path of the flag that admits the experimental
#: structures, spelled the way an operator writes it.
EXPERIMENTAL_STRUCTURES_KEY: str = "experimental.tournament_structures"


def experimental_structure_refusal(structure: str) -> str:
    """The one message every surface uses to refuse an experimental structure.

    Names the token, states its tier, and names the key that admits it, so
    the contract loader, configuration edits, the CLI option, and the strategy
    registry refuse with the same wording.
    """
    return (
        f"tournament structure {structure!r} is experimental; enable it with "
        f"{EXPERIMENTAL_STRUCTURES_KEY} = true in scoring.json"
    )


#: Bounds on the structure params that carry a domain regardless of which
#: structure reads them. The params mapping is otherwise opaque to the data
#: layer, and a strategy that clamps what it reads (``max(1, ...)``) cannot
#: tell an operator their setting was ignored — so a nonsensical value is
#: refused at contract load instead of silently corrected at round start.
#: ``replicates`` is how many times a duel is re-run and averaged, so fewer
#: than one is not a cheaper tournament but no measurement at all.
TOURNAMENT_PARAM_CONSTRAINTS: Mapping[str, KnobConstraint] = {
    "promote_confidence_threshold": KnobConstraint(
        minimum=0,
        maximum=1,
        exclusive_maximum=True,
        allow_none=True,
        label='tournament params["promote_confidence_threshold"]',
    ),
    "replicates": KnobConstraint(
        minimum=1,
        maximum=measurement_range(MeasurementPurpose.TOURNAMENT).span,
        label='tournament params["replicates"]',
    ),
    "promote_confidence_replicates": KnobConstraint(
        minimum=0,
        maximum=measurement_range(MeasurementPurpose.CONFIRMATION).span,
        allow_none=True,
        label='tournament params["promote_confidence_replicates"]',
    ),
}

# The complete recommended specification owns these confirmation values.
DEFAULT_PROMOTE_CONFIDENCE_THRESHOLD: float = 0.8
DEFAULT_CONFIRMATION_BUDGET: int = 32


def read_promote_confidence_threshold(params: Mapping[str, Any]) -> float | None:
    """Read a finite probability requirement; absence, null, and zero disable it."""
    raw = params.get("promote_confidence_threshold")
    TOURNAMENT_PARAM_CONSTRAINTS["promote_confidence_threshold"].check(
        "promote_confidence_threshold", raw
    )
    return None if raw is None or raw == 0 else float(raw)


def read_replicate_budget(params: Mapping[str, Any]) -> int:
    """Read the supported confirmation budget; zero permits no extra measurements."""
    raw = params.get("promote_confidence_replicates")
    if raw is None:
        return DEFAULT_CONFIRMATION_BUDGET
    if type(raw) is not int or raw < 0:
        raise ValueError("promote_confidence_replicates must be a nonnegative integer")
    validate_measurement_interval(EVIDENCE_REPLICATE_BASE, raw, allow_empty=True)
    return raw


@dataclass(frozen=True, slots=True)
class MatchOutcome:
    """One match a generation played inside its tournament.

    A small audit record carried on :class:`OutcomeRecord` so a
    non-gauntlet structure (bracket / Swiss / racing) can record, per
    generation, which opponents it faced and how each duel went. A
    gauntlet leaves :attr:`OutcomeRecord.match_record` empty — its single
    crowning duel is already described by the top-level outcome fields.

    Fields
    ------
    match_id:
        Stable id of the match within the tournament (e.g. ``"WB-R0-0"``,
        ``"r2_m1"``, ``"rung1"``).
    opponent:
        The generation id this generation was paired against. Empty for a
        bye or an N-way racing rung.
    won:
        ``True`` when this generation was the match's winner (the side the
        gate / rank preferred).
    delta_scalar:
        ``this.scalar - opponent.scalar`` for the match. Negative = this
        generation scored the lower (better) loss.
    """

    match_id: str
    opponent: str
    won: bool
    delta_scalar: float


@dataclass(frozen=True, slots=True)
class TournamentStructure:
    """The per-epoch tournament structure and its tuning params.

    Part of the frozen evaluation contract: it is modelled as a field of
    :class:`ScoringWeights` (and therefore folds into the contract hash
    automatically), so changing the structure — or any param — rolls the
    epoch, exactly as retuning ``promote_margin`` does. A gauntlet
    champion and a Swiss champion are selected under different rules and
    are not directly comparable, which is the contract-roll
    rationale.

    Fields
    ------
    structure:
        Required: one of :data:`VALID_TOURNAMENT_STRUCTURES`. Authored scoring
        resolves omission through its complete racing specification.
    params:
        A structure-specific JSON object, stored and round-tripped
        verbatim as a ``Mapping[str, Any]``. Per-key
        semantics (``field_size``, ``swiss.rounds_n``, ``racing.eta`` /
        ``board_fraction`` / ``rung0_board_size``, …) are owned by the
        selection strategy that reads them; the data layer enforces that
        this is a mapping and that any key in
        :data:`TOURNAMENT_PARAM_CONSTRAINTS` holds a value in its
        declared range.

    :meth:`gauntlet` constructs an explicit single-challenger specification
    without confirmation. Scoring defaults are owned by
    :func:`_default_tournament_structure`.
    """

    structure: str = field(
        metadata={"constraint": KnobConstraint(choices=VALID_TOURNAMENT_STRUCTURES)},
    )
    params: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.structure not in VALID_TOURNAMENT_STRUCTURES:
            valid = ", ".join(repr(s) for s in VALID_TOURNAMENT_STRUCTURES)
            raise ValueError(
                f"invalid tournament structure {self.structure!r}; " f"valid values are: {valid}"
            )
        if not isinstance(self.params, Mapping):
            raise ValueError(
                f"tournament params must be a JSON object (mapping), got "
                f"{type(self.params).__name__}"
            )
        for key, constraint in TOURNAMENT_PARAM_CONSTRAINTS.items():
            if key in self.params:
                constraint.check(key, self.params[key])
        if read_promote_confidence_threshold(self.params) is not None:
            object.__setattr__(
                self,
                "params",
                {
                    **self.params,
                    "promote_confidence_replicates": read_replicate_budget(self.params),
                },
            )

    @classmethod
    def gauntlet(cls) -> TournamentStructure:
        """An explicit single-challenger specification without confirmation."""
        return cls(structure="gauntlet", params={})


def _default_tournament_structure() -> TournamentStructure:
    """Default-factory for :attr:`ScoringWeights.tournament_structure`."""
    return TournamentStructure(
        structure="racing",
        params={
            "field_size": 4,
            "eta": 2,
            "board_fraction": 0.4,
            "replicates": 2,
            "promote_confidence_threshold": DEFAULT_PROMOTE_CONFIDENCE_THRESHOLD,
            "promote_confidence_replicates": DEFAULT_CONFIRMATION_BUDGET,
        },
    )
