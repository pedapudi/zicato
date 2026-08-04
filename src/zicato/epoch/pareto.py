"""The per-epoch Pareto frontier RECORD — candidates the scalar threw away.

The promote gate keeps one generation per round and picks it by a weighted
sum. A weighted sum is a projection: a challenger that halves cost for a
sliver of rubric loses on the scalar, is rejected, and is never mentioned
again. This module records those candidates — the settled ones that beat the
reigning champion on at least one scoring axis and that nothing else on the
record dominates — beside the champion lineage.

**The record changes nothing.** It never enters the gate, selection, the
proposer's prompt, or the champion pointer; it is an observation, in the same
class as the Elo fold and the RoundLog. See
``docs/design/PARETO-FRONTIER.md`` for the full design, including the steps
this deliberately does NOT build (§8) and the cross-round comparability
assumption it writes down rather than resolves (§9).

Everything it needs already lives in the frozen epoch contract, so it adds no
knob:

* the **axes** are the non-zero keys of
  :attr:`~zicato.core.ScoringWeights.namespace_weights`;
* the **units** are ``namespace_aggregates`` — which
  :func:`~zicato.tournament.scoring.aggregate_namespaced_metrics` has already
  multiplied by each namespace's SIGNED weight, so every axis is uniformly
  lower-is-better and the comparison never branches on direction (the same
  property the gate's Rule 3 relies on);
* the **threshold** is :attr:`~zicato.core.ScoringWeights.promote_margin`,
  which ``tournament/calibration.py`` fits to an A/A noise floor — so
  "better" means here exactly what it means at the gate;
* the **reset** is the epoch: a frozen contract, so an epoch roll starts the
  record empty and a promotion keeps it (only the reference point moved).

Admission is guarded by the gate's OWN namespace-monotonicity rule
(:func:`~zicato.tournament.gate.regressed_namespaces`), which is what stops
the record filling with degenerate cut-everything candidates — a frontier has
none of the weighted sum's built-in protection against a candidate that
guts one axis to win another.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from zicato.core import ScoringWeights
from zicato.storage import atomic_write_json, read_json

#: Basename of the per-epoch frontier record, under ``epochs/{epoch}/``.
FRONTIER_FILENAME = "pareto_frontier.json"

#: Retire reasons. ``DOMINATED_BY_PREFIX`` is completed with the generation
#: id that displaced the member (``"dominated_by:v6"``), so the record always
#: names WHAT retired a member, not just that something did.
RETIRED_PROMOTED = "promoted"
RETIRED_DOMINATED_BY_CHAMPION = "dominated_by_champion"
RETIRED_MONOTONICITY = "monotonicity_regression"
DOMINATED_BY_PREFIX = "dominated_by:"


# ---------------------------------------------------------------------------
# Axes + dominance — pure, no I/O, no workspace.
# ---------------------------------------------------------------------------


def frontier_axes(weights: ScoringWeights) -> tuple[str, ...]:
    """The sorted namespace axes the frontier compares on.

    Every key of :attr:`ScoringWeights.namespace_weights` whose weight is not
    zero. Under the defaults: ``cost:``, ``drift:``, ``latency:``, ``rubric:``,
    ``schema:``.

    ``output:`` is excluded because its default weight is ``0.0`` — a zero
    weight has neither a sign nor a scale, so there is no direction in which
    more output is better or worse. That is exactly what the operator said by
    setting it to zero.

    ``latency:`` IS an axis by weight but is empty in practice: the telemetry
    reducer never fills the namespace, so its aggregate is a constant ``0.0``
    on every side. It can therefore never separate two candidates — and never
    wrongly separate them either. Filling it is a scoring change that would
    move every scalar in the workspace (PARETO-FRONTIER.md §8).
    """
    return tuple(sorted(ns for ns, w in weights.namespace_weights.items() if float(w) != 0.0))


def axis_values(aggregate: Mapping[str, Any], axes: Sequence[str]) -> dict[str, float]:
    """Read one generation's per-axis values out of its aggregate.

    The values come from ``aggregate["namespace_aggregates"]`` — already
    weight-multiplied and sign-folded, so they are directly comparable in
    scalar points across axes whose raw units differ by orders of magnitude.

    An axis absent from the aggregate, or carrying a non-finite value, is
    OMITTED rather than defaulted to zero: a missing measurement is not a
    measurement of zero, and fabricating one would invent a dominance
    relation. This mirrors
    :func:`~zicato.tournament.gate.regressed_namespaces`, which skips a
    namespace it cannot see on both sides.
    """
    raw = aggregate.get("namespace_aggregates") or {}
    if not isinstance(raw, Mapping):
        return {}
    out: dict[str, float] = {}
    for ns in axes:
        if ns not in raw:
            continue
        try:
            value = float(raw[ns])
        except (TypeError, ValueError):
            continue
        if math.isfinite(value):
            out[ns] = value
    return out


def beats_on(
    candidate: Mapping[str, float],
    reference: Mapping[str, float],
    *,
    margin: float,
) -> tuple[str, ...]:
    """The sorted axes where ``candidate`` is at least ``margin`` better.

    Both the admission test against the champion and the provenance recorded
    on a member: "this is where it won". Lower is better on every axis, so
    "better by margin" is ``reference - candidate >= margin``. Axes only one
    side carries are skipped.
    """
    return tuple(
        sorted(
            ns
            for ns, value in candidate.items()
            if ns in reference and reference[ns] - value >= margin
        )
    )


def dominates(
    left: Mapping[str, float],
    right: Mapping[str, float],
    *,
    margin: float,
) -> bool:
    """True when ``left`` Pareto-dominates ``right`` within the margin band.

    ``left`` dominates when it is at least ``margin`` BETTER on at least one
    axis and NOT ``margin``-or-more worse on any. Every axis is uniformly
    lower-is-better (the weight's sign is already folded into the aggregate),
    so neither limb branches on direction.

    ``margin`` is :attr:`ScoringWeights.promote_margin` — the width at which
    the loop has already agreed a difference is not noise. That makes this a
    WEAK dominance test: inside the band the two candidates are tied on that
    axis, so a candidate a hair better everywhere dominates nothing, and a
    candidate a hair worse on one axis is not thereby saved from being
    dominated.

    Axes only one side carries are skipped — they neither create nor block a
    relation. With NO shared axes the answer is ``False`` in both directions:
    two candidates with nothing in common are incomparable, not dominant.
    """
    better_any = False
    for ns, value in left.items():
        if ns not in right:
            continue
        other = right[ns]
        if other - value >= margin:
            better_any = True
        elif value - other >= margin:
            return False
    return better_any


# ---------------------------------------------------------------------------
# The record.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FrontierMember:
    """One candidate on the record, with the evidence for why it is there.

    ``champion_generation_id`` is the champion this member was ADMITTED
    against, which is not necessarily the epoch's current champion — keeping
    it per-member means a member never silently re-attributes its provenance
    to a champion that did not exist when it was admitted.
    """

    generation_id: str
    round_admitted: int
    champion_generation_id: str
    axis_values: Mapping[str, float] = field(default_factory=dict)
    beats_champion_on: tuple[str, ...] = ()
    scalar: float | None = None


@dataclass(frozen=True, slots=True)
class RetiredMember:
    """A member that left the frontier, with the round and the reason.

    Nothing is ever deleted from the record: the interesting question later
    is not only what is on the frontier but what WAS on it and what displaced
    it. Same evidence discipline as archive-on-overwrite.
    """

    member: FrontierMember
    round_retired: int
    reason: str


@dataclass(frozen=True, slots=True)
class ParetoFrontier:
    """One epoch's frontier record.

    ``axes`` and ``margin`` are echoed from the contract so the file is
    self-describing — a reader does not need ``scoring.json`` to interpret
    it. ``champion_generation_id`` is the champion the record was last
    evaluated against.
    """

    epoch_id: str
    axes: tuple[str, ...] = ()
    margin: float = 0.0
    champion_generation_id: str = ""
    updated_round: int = 0
    members: tuple[FrontierMember, ...] = ()
    retired: tuple[RetiredMember, ...] = ()


@dataclass(frozen=True, slots=True)
class FrontierCandidate:
    """One generation offered to the record: its id, aggregate, and arm.

    ``aggregate`` is an ``aggregate_generation_score`` output — the same dict
    the gate decided on. ``is_placebo`` marks a random-baseline calibration
    arm, which is never a candidate (see :func:`update_frontier`).
    """

    generation_id: str
    aggregate: Mapping[str, Any] = field(default_factory=dict)
    is_placebo: bool = False


@dataclass(frozen=True, slots=True)
class FrontierUpdate:
    """The result of one settle: the new record plus what moved."""

    frontier: ParetoFrontier
    admitted: tuple[str, ...] = ()
    retired: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        """True when membership moved — the only case anything is written."""
        return bool(self.admitted or self.retired)


# ---------------------------------------------------------------------------
# The update — pure and total.
# ---------------------------------------------------------------------------


def _member_from(
    candidate: FrontierCandidate,
    *,
    values: Mapping[str, float],
    beats: tuple[str, ...],
    champion_generation_id: str,
    round_index: int,
) -> FrontierMember:
    raw_scalar = candidate.aggregate.get("scalar")
    scalar: float | None
    try:
        scalar = float(raw_scalar)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        scalar = None
    if scalar is not None and not math.isfinite(scalar):
        scalar = None
    return FrontierMember(
        generation_id=candidate.generation_id,
        round_admitted=round_index,
        champion_generation_id=champion_generation_id,
        axis_values=dict(values),
        beats_champion_on=beats,
        scalar=scalar,
    )


def update_frontier(
    frontier: ParetoFrontier,
    *,
    champion: FrontierCandidate,
    candidates: Sequence[FrontierCandidate],
    weights: ScoringWeights,
    round_index: int,
) -> FrontierUpdate:
    """Fold one settled round into the record. Pure, deterministic, no I/O.

    ``champion`` is the champion as it stands AFTER the round's decision —
    the promoted generation on a promote, the incumbent otherwise — so the
    record is always evaluated against the champion the round actually ended
    with. ``candidates`` are the round's other settled generations.

    Two passes. The RETIRE pass drops members the new champion has made
    uninteresting:

    * the member IS now the champion (``promoted``);
    * the champion dominates it (``dominated_by_champion``);
    * it regresses a monotonicity-tracked namespace against the champion
      (``monotonicity_regression``).

    The ADMIT pass then walks ``candidates`` in ``generation_id`` order.
    A candidate is admitted only when ALL of:

    #. it is not the champion and not already a member (idempotent re-settle);
    #. it is not a placebo — a random-baseline arm is a no-op re-emission of
       the champion, so it would sit on the record as a permanent tie and
       contaminate exactly the set whose job is to hold interesting
       candidates. The multi-challenger path fields the placebo INSIDE the
       slate, so this is load-bearing, not theoretical;
    #. it is settled enough to compare — at least one finite axis value;
    #. it does not regress a monotonicity-tracked namespace against the
       champion (the gate's own rule, via
       :func:`~zicato.tournament.gate.regressed_namespaces`) — this is the
       control that keeps a cut-everything candidate off the record;
    #. it beats the champion by at least ``promote_margin`` on at least one
       axis. A candidate that ties or loses everywhere carries no information
       the champion does not already carry. (This subsumes "the champion does
       not dominate it": beating the champion on an axis by the margin makes
       the champion margin-worse there.);
    #. no surviving member dominates it.

    Each admission retires any surviving member the newcomer dominates
    (``dominated_by:{gid}``).

    The champion is the REFERENCE, never a member: the record's claim is
    "here is what beat the champion somewhere", which needs the champion
    outside the set being compared.
    """
    from zicato.tournament.gate import regressed_namespaces  # noqa: PLC0415

    axes = frontier_axes(weights)
    margin = float(weights.promote_margin)
    champion_values = axis_values(champion.aggregate, axes)

    surviving: list[FrontierMember] = []
    retired: list[RetiredMember] = list(frontier.retired)
    retired_ids: list[str] = []

    def _retire(member: FrontierMember, reason: str) -> None:
        retired.append(RetiredMember(member=member, round_retired=round_index, reason=reason))
        retired_ids.append(member.generation_id)

    # --- Retire pass: re-evaluate every member against the new champion.
    for member in frontier.members:
        if member.generation_id == champion.generation_id:
            _retire(member, RETIRED_PROMOTED)
            continue
        if regressed_namespaces(dict(champion.aggregate), _as_agg(member), weights):
            _retire(member, RETIRED_MONOTONICITY)
            continue
        if dominates(champion_values, member.axis_values, margin=margin):
            _retire(member, RETIRED_DOMINATED_BY_CHAMPION)
            continue
        surviving.append(member)

    # --- Admit pass.
    admitted_ids: list[str] = []
    known = {m.generation_id for m in surviving} | {champion.generation_id}
    for candidate in sorted(candidates, key=lambda c: c.generation_id):
        if candidate.generation_id in known or candidate.is_placebo:
            continue
        values = axis_values(candidate.aggregate, axes)
        if not values:
            continue
        if regressed_namespaces(dict(champion.aggregate), dict(candidate.aggregate), weights):
            continue
        beats = beats_on(values, champion_values, margin=margin)
        if not beats:
            continue
        if any(dominates(m.axis_values, values, margin=margin) for m in surviving):
            continue
        member = _member_from(
            candidate,
            values=values,
            beats=beats,
            champion_generation_id=champion.generation_id,
            round_index=round_index,
        )
        displaced = [m for m in surviving if dominates(values, m.axis_values, margin=margin)]
        for old in displaced:
            _retire(old, f"{DOMINATED_BY_PREFIX}{candidate.generation_id}")
        surviving = [m for m in surviving if m not in displaced]
        surviving.append(member)
        known.add(candidate.generation_id)
        admitted_ids.append(candidate.generation_id)

    updated = ParetoFrontier(
        epoch_id=frontier.epoch_id,
        axes=axes,
        margin=margin,
        champion_generation_id=champion.generation_id,
        updated_round=round_index,
        members=tuple(sorted(surviving, key=lambda m: m.generation_id)),
        retired=tuple(sorted(retired, key=lambda r: (r.round_retired, r.member.generation_id))),
    )
    return FrontierUpdate(
        frontier=updated,
        admitted=tuple(admitted_ids),
        retired=tuple(retired_ids),
    )


def _as_agg(member: FrontierMember) -> dict[str, Any]:
    """Re-wrap a member's stored axis values as an aggregate-shaped dict.

    :func:`~zicato.tournament.gate.regressed_namespaces` reads
    ``["namespace_aggregates"]``; a stored member carries only the axis map,
    which IS that mapping restricted to the frontier's axes. Namespaces the
    member does not carry are skipped by the rule itself (it needs two points
    to compare), so the restriction is safe.
    """
    return {"namespace_aggregates": dict(member.axis_values)}


# ---------------------------------------------------------------------------
# Persistence — workspace is truth.
# ---------------------------------------------------------------------------


def frontier_path(workspace_root: Path, epoch_id: str) -> Path:
    """Path to one epoch's ``pareto_frontier.json`` (pure path math)."""
    from zicato.core.workspace import epoch_dir  # noqa: PLC0415

    return epoch_dir(workspace_root, epoch_id) / FRONTIER_FILENAME


def _member_to_dict(member: FrontierMember) -> dict[str, Any]:
    return {
        "generation_id": member.generation_id,
        "round_admitted": member.round_admitted,
        "champion_generation_id": member.champion_generation_id,
        "axis_values": dict(member.axis_values),
        "beats_champion_on": list(member.beats_champion_on),
        "scalar": member.scalar,
    }


def _member_from_dict(body: Mapping[str, Any]) -> FrontierMember:
    raw_values = body.get("axis_values") or {}
    values: dict[str, float] = {}
    if isinstance(raw_values, Mapping):
        for ns, value in raw_values.items():
            try:
                values[str(ns)] = float(value)
            except (TypeError, ValueError):
                continue
    raw_scalar = body.get("scalar")
    scalar: float | None
    try:
        scalar = None if raw_scalar is None else float(raw_scalar)
    except (TypeError, ValueError):
        scalar = None
    raw_beats = body.get("beats_champion_on") or ()
    beats = tuple(str(ns) for ns in raw_beats) if isinstance(raw_beats, list | tuple) else ()
    return FrontierMember(
        generation_id=str(body.get("generation_id", "")),
        round_admitted=int(body.get("round_admitted", 0) or 0),
        champion_generation_id=str(body.get("champion_generation_id", "") or ""),
        axis_values=values,
        beats_champion_on=beats,
        scalar=scalar,
    )


def frontier_to_dict(frontier: ParetoFrontier) -> dict[str, Any]:
    """The record's on-disk JSON shape (see PARETO-FRONTIER.md §3)."""
    from zicato.epoch._storage import RECORD_FORMAT_VERSION  # noqa: PLC0415

    retired: list[dict[str, Any]] = []
    for entry in frontier.retired:
        body = _member_to_dict(entry.member)
        body["round_retired"] = entry.round_retired
        body["reason"] = entry.reason
        retired.append(body)
    return {
        "format_version": RECORD_FORMAT_VERSION,
        "epoch_id": frontier.epoch_id,
        "axes": list(frontier.axes),
        "margin": frontier.margin,
        "champion_generation_id": frontier.champion_generation_id,
        "updated_round": frontier.updated_round,
        "members": [_member_to_dict(m) for m in frontier.members],
        "retired": retired,
    }


def frontier_from_dict(body: Mapping[str, Any], *, epoch_id: str) -> ParetoFrontier:
    """Parse a record body; refuses a ``format_version`` this build cannot read."""
    from zicato.epoch._storage import check_record_format  # noqa: PLC0415

    check_record_format(dict(body), FRONTIER_FILENAME)
    raw_axes = body.get("axes") or ()
    axes = tuple(str(ns) for ns in raw_axes) if isinstance(raw_axes, list | tuple) else ()
    members = [_member_from_dict(m) for m in (body.get("members") or ()) if isinstance(m, Mapping)]
    retired: list[RetiredMember] = []
    for entry in body.get("retired") or ():
        if not isinstance(entry, Mapping):
            continue
        retired.append(
            RetiredMember(
                member=_member_from_dict(entry),
                round_retired=int(entry.get("round_retired", 0) or 0),
                reason=str(entry.get("reason", "") or ""),
            )
        )
    try:
        margin = float(body.get("margin", 0.0) or 0.0)
    except (TypeError, ValueError):
        margin = 0.0
    return ParetoFrontier(
        epoch_id=str(body.get("epoch_id", "") or epoch_id),
        axes=axes,
        margin=margin,
        champion_generation_id=str(body.get("champion_generation_id", "") or ""),
        updated_round=int(body.get("updated_round", 0) or 0),
        members=tuple(members),
        retired=tuple(retired),
    )


def load_frontier(workspace_root: Path, epoch_id: str) -> ParetoFrontier:
    """Read one epoch's record; an ABSENT file is an empty frontier.

    Never an error on a workspace that predates the feature — "no record yet"
    and "an epoch that admitted nothing" are the same observable state, and
    both are valid.
    """
    body = read_json(frontier_path(workspace_root, epoch_id))
    if not isinstance(body, dict):
        return ParetoFrontier(epoch_id=epoch_id)
    return frontier_from_dict(body, epoch_id=epoch_id)


def save_frontier(workspace_root: Path, epoch_id: str, frontier: ParetoFrontier) -> None:
    """Atomically write one epoch's record (``.tmp`` + ``fsync`` + rename)."""
    atomic_write_json(frontier_path(workspace_root, epoch_id), frontier_to_dict(frontier))


def record_frontier(
    workspace_root: Path,
    epoch_id: str,
    *,
    champion: FrontierCandidate,
    candidates: Sequence[FrontierCandidate],
    weights: ScoringWeights,
    round_index: int,
) -> FrontierUpdate:
    """Load → :func:`update_frontier` → write, and only when membership moved.

    A round that admits and retires nothing leaves the file byte-identical
    (untouched, not rewritten), so the record's mtime means "something
    happened" rather than "a round ran".
    """
    current = load_frontier(workspace_root, epoch_id)
    update = update_frontier(
        current,
        champion=champion,
        candidates=candidates,
        weights=weights,
        round_index=round_index,
    )
    if update.changed:
        save_frontier(workspace_root, epoch_id, update.frontier)
    return update


__all__ = [
    "DOMINATED_BY_PREFIX",
    "FRONTIER_FILENAME",
    "RETIRED_DOMINATED_BY_CHAMPION",
    "RETIRED_MONOTONICITY",
    "RETIRED_PROMOTED",
    "FrontierCandidate",
    "FrontierMember",
    "FrontierUpdate",
    "ParetoFrontier",
    "RetiredMember",
    "axis_values",
    "beats_on",
    "dominates",
    "frontier_axes",
    "frontier_from_dict",
    "frontier_path",
    "frontier_to_dict",
    "load_frontier",
    "record_frontier",
    "save_frontier",
    "update_frontier",
]
