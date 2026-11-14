"""The per-epoch Pareto frontier RECORD — candidates the scalar threw away.

The promote gate keeps one generation per round and picks it by a weighted
sum. A weighted sum is a projection: a challenger that halves cost for a
sliver of rubric loses on the scalar, is rejected, and is never mentioned
again. This module records those candidates — the settled ones that beat the
reigning champion on at least one scoring axis and that nothing else on the
record dominates — beside the champion lineage.

**The record decides nothing.** It never enters the gate, selection, or the
champion pointer. Its default readers are the operator's trade-off table
(:func:`render_frontier_tradeoffs`) and :func:`confirm_member`, which
re-checks one member against the current champion from a fresh measurement
before anything uses it. Two opt-in consumers in ``zicato.evolve`` let it
shape what is proposed: the recombination pool and the steering block. See
``docs/design/PARETO-FRONTIER.md`` for the full design, including the steps
this deliberately does NOT build (§8) and the cross-round comparability
assumption it writes down rather than resolves (§9).

Everything it needs already lives in the frozen epoch contract, so it adds no
knob:

* the **axes** are the non-zero keys of
  :attr:`~zicato.core.ScoringWeights.namespace_weights`, plus the
  highest-weight judges and scored metrics inside them
  (:func:`frontier_axes`);
* the **units** are ``namespace_aggregates`` — which
  :func:`~zicato.tournament.scoring.aggregate_namespaced_metrics` has already
  multiplied by each namespace's SIGNED weight, so every axis is uniformly
  lower-is-better and the comparison never branches on direction (the same
  property the gate's Rule 3 relies on) — and, for a dimension, the same
  product split per metric
  (:func:`~zicato.tournament.scoring.aggregate_dimension_metrics`);
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
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from zicato.core import ScoringWeights
from zicato.storage import atomic_write_json, read_json

#: Basename of the per-epoch frontier record, under ``epochs/{epoch}/``.
FRONTIER_FILENAME = "pareto_frontier.json"

#: Retire reasons. ``DOMINATED_BY_PREFIX`` is completed with the generation
#: id that displaced the member (``"dominated_by:v6"``), so the record always
#: names WHAT retired a member rather than only that something did.
RETIRED_PROMOTED = "promoted"
RETIRED_DOMINATED_BY_CHAMPION = "dominated_by_champion"
RETIRED_MONOTONICITY = "monotonicity_regression"
RETIRED_UNCONFIRMED = "unconfirmed"
DOMINATED_BY_PREFIX = "dominated_by:"

#: Aggregate key carrying one generation's per-dimension contributions, as
#: :func:`~zicato.tournament.scoring.aggregate_dimension_metrics` returns
#: them. The round's recorder adds it to the aggregates it offers the record;
#: ``gen_score.json`` does not carry it.
DIMENSION_AGGREGATES_KEY = "dimension_aggregates"

#: How many dimension axes join the namespace axes. Every extra axis is one
#: more chance to clear the margin against the champion by noise, so the set
#: is bounded and :func:`confirm_member` re-checks a member before use.
FRONTIER_DIMENSION_CAP = 8


# ---------------------------------------------------------------------------
# Axes + dominance — pure, no I/O, no workspace.
# ---------------------------------------------------------------------------


def _dimension_weight(name: str, weights: ScoringWeights) -> float:
    """The magnitude of the contract weight on one dimension.

    The namespace coefficient times the within-channel weight the contract
    names for that dimension: the per-judge weight for ``judge:<name>``, the
    two failure magnitudes for ``failure:``, and ``1.0`` for every other
    metric. This is the weight the proposer's scored targets are ranked by.
    """
    namespace, _, leaf = name.partition(":")
    within = {
        "failure:tasks": weights.task_failure_weight,
        "failure:not_completed": weights.not_completed_weight,
        f"judge:{leaf}": weights.per_judge_weights.get(leaf, weights.default_judge_weight),
    }.get(name, 1.0)
    return abs(float(weights.namespace_weights.get(f"{namespace}:", 0.0)) * float(within))


def uses_dimension_axes(weights: ScoringWeights) -> bool:
    """Whether the contract enables a consumer of the record.

    The dimension axes exist for the two opt-in consumers
    (``experimental.recombine_pool: "frontier"`` and
    ``experimental.frontier_steering``). Every extra axis is one more chance
    for a candidate no better than the champion to clear the margin by noise,
    so a contract with neither consumer keeps the namespace axes alone.
    """
    experimental = weights.experimental
    return experimental.recombine_pool == "frontier" or experimental.frontier_steering


def frontier_axes(
    weights: ScoringWeights, aggregate: Mapping[str, Any] | None = None
) -> tuple[str, ...]:
    """The axes the frontier compares on: namespaces, then dimensions.

    The namespace axes are the sorted keys of
    :attr:`ScoringWeights.namespace_weights` whose weight is not zero. Under
    the defaults: ``cost:``, ``drift:``, ``failure:``, ``judge:``,
    ``latency:``, ``rubric:``, ``schema:``.

    ``output:`` is excluded because its default weight is ``0.0`` — a zero
    weight has neither a sign nor a scale, so there is no direction in which
    more output is better or worse. That is exactly what the operator said by
    setting it to zero.

    ``latency:`` IS an axis by weight but is empty in practice: the telemetry
    reducer never fills the namespace, so its aggregate is a constant ``0.0``
    on every side. It can therefore never separate two candidates — and never
    wrongly separate them either. Filling it is a scoring change that would
    move every scalar in the workspace (PARETO-FRONTIER.md §8).

    The dimension axes are the judges and scored metrics inside those
    namespaces, named by ``aggregate`` (the reference champion's, under
    :data:`DIMENSION_AGGREGATES_KEY`), because only the data knows which
    metrics a board reports. They follow the namespaces, ordered by contract
    weight then name, at most :data:`FRONTIER_DIMENSION_CAP` of them. A
    namespace with one dimension contributes none: that dimension is the
    namespace axis. With no ``aggregate``, or under a contract that enables
    no consumer of the record (:func:`uses_dimension_axes`), the result is the
    namespace axes.
    """
    namespaces = tuple(sorted(ns for ns, w in weights.namespace_weights.items() if float(w) != 0.0))
    raw = (aggregate or {}).get(DIMENSION_AGGREGATES_KEY) or {}
    if not isinstance(raw, Mapping) or not uses_dimension_axes(weights):
        return namespaces
    per_namespace: dict[str, list[str]] = {}
    for name in raw:
        namespace = str(name).partition(":")[0] + ":"
        if namespace in namespaces and str(name) != namespace:
            per_namespace.setdefault(namespace, []).append(str(name))
    dimensions = [name for names in per_namespace.values() if len(names) > 1 for name in names]
    dimensions.sort(key=lambda name: (-_dimension_weight(name, weights), name))
    return namespaces + tuple(dimensions[:FRONTIER_DIMENSION_CAP])


def axis_values(aggregate: Mapping[str, Any], axes: Sequence[str]) -> dict[str, float]:
    """Read one generation's per-axis values out of its aggregate.

    The values come from ``aggregate["namespace_aggregates"]`` and, for a
    dimension axis, ``aggregate["dimension_aggregates"]`` — both already
    weight-multiplied and sign-folded, so they are directly comparable in
    scalar points across axes whose raw units differ by orders of magnitude.

    An axis absent from the aggregate, or carrying a non-finite value, is
    OMITTED rather than defaulted to zero: a missing measurement is not a
    measurement of zero, and fabricating one would invent a dominance
    relation. This mirrors
    :func:`~zicato.tournament.gate.regressed_namespaces`, which skips a
    namespace it cannot see on both sides.
    """
    raw: dict[str, Any] = {}
    for key in (DIMENSION_AGGREGATES_KEY, "namespace_aggregates"):
        part = aggregate.get(key) or {}
        if isinstance(part, Mapping):
            raw.update(part)
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

    The second conjunct is what keeps the test meaningful at ``margin == 0``,
    which :class:`ScoringWeights` accepts (nothing validates the field to a
    positive number). Without it a zero margin makes ``>= margin`` true for
    an exact TIE, and every candidate would "beat" the champion everywhere it
    merely equals it — admitting no-information copies onto a record whose
    stated admission rule is that a tie carries nothing the champion does not
    already carry. For every ``margin > 0`` the conjunct is implied by the
    first and changes nothing.
    """
    return tuple(
        sorted(
            ns
            for ns, value in candidate.items()
            if ns in reference and reference[ns] - value >= margin and value < reference[ns]
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
    two candidates with nothing in common are incomparable rather than dominant.

    BOTH limbs require a strict difference on top of clearing the margin,
    which is what keeps the relation well-formed at ``margin == 0`` — a value
    :class:`ScoringWeights` accepts, since nothing validates the field to a
    positive number. At a zero margin ``>= margin`` is true for an exact TIE.
    That would make every candidate dominate itself, and any two identical
    candidates dominate each other, neither of which a partial order may do.
    A tie on the worse limb would meanwhile veto a candidate that is strictly
    better somewhere and equal everywhere else, which is the textbook
    dominant case. With
    the conjuncts, ``margin == 0`` degrades to exactly strict Pareto
    dominance. For every ``margin > 0`` both conjuncts are implied by the
    inequality beside them and nothing changes.
    """
    better_any = False
    for ns, value in left.items():
        if ns not in right:
            continue
        other = right[ns]
        if other - value >= margin and value < other:
            better_any = True
        elif value - other >= margin and other < value:
            return False
    return better_any


# ---------------------------------------------------------------------------
# The record.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FrontierMember:
    """One candidate on the record, with the evidence for why it is there.

    ``champion_generation_id`` is the champion this member's values were
    last measured against: the one it was ADMITTED against, until
    :func:`confirm_member` re-measures it against a later one. It is not
    necessarily the epoch's current champion — keeping it per-member means a
    member never silently re-attributes its provenance to a champion it was
    never compared with.

    ``beats_champion_by`` is the gain on each axis of ``beats_champion_on``,
    in scalar points (the champion's value minus the member's). Empty on a
    record written before the field existed.

    ``confirmed_round`` is the round a fresh measurement last confirmed the
    member, or ``None`` when its values are still the tournament draws that
    selected it — which favour it, since it was admitted for having them.
    """

    generation_id: str
    round_admitted: int
    champion_generation_id: str
    axis_values: Mapping[str, float] = field(default_factory=dict)
    beats_champion_on: tuple[str, ...] = ()
    scalar: float | None = None
    beats_champion_by: Mapping[str, float] = field(default_factory=dict)
    confirmed_round: int | None = None


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
    confirmed: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        """True when the record moved — the only case anything is written."""
        return bool(self.admitted or self.retired or self.confirmed)


# ---------------------------------------------------------------------------
# The update — pure and total.
# ---------------------------------------------------------------------------


def _member_from(
    candidate: FrontierCandidate,
    *,
    values: Mapping[str, float],
    champion_values: Mapping[str, float],
    beats: tuple[str, ...],
    champion_generation_id: str,
    round_index: int,
    confirmed_round: int | None = None,
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
        beats_champion_by={axis: champion_values[axis] - values[axis] for axis in beats},
        confirmed_round=confirmed_round,
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
       slate, so this is load-bearing rather than theoretical;
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

    A PLACEBO champion is refused outright and the round records nothing. A
    placebo is a semantics-preserving no-op copy of the champion, so it is
    numerically the champion wearing a different generation id: measuring
    against it would stamp every admission's ``champion_generation_id`` with
    a generation that only exists to test the gate. The field path fields the
    placebo inside the slate and the gate CAN crown it — that is what the
    ``placebo_promoted`` CRITICAL finding is for — so this is reachable, and
    when it happens the loop is already in an alarm state where the last
    thing wanted is provenance pointing at the alarm.
    """
    from zicato.tournament.gate import regressed_namespaces  # noqa: PLC0415

    if champion.is_placebo:
        return FrontierUpdate(frontier=frontier)

    axes = frontier_axes(weights, champion.aggregate)
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
            champion_values=champion_values,
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


def confirm_member(
    frontier: ParetoFrontier,
    *,
    champion: FrontierCandidate,
    member: FrontierCandidate,
    weights: ScoringWeights,
    round_index: int,
) -> FrontierUpdate:
    """Re-evaluate one member against the CURRENT champion. Pure, no I/O.

    ``champion`` and ``member`` carry aggregates from one fresh paired
    measurement, taken under a purpose other than the tournament's. A member
    is on the record because its tournament draws beat a champion on some
    axis, so those draws overstate it, and after a promotion they describe a
    comparison with a champion that has been replaced.

    The member is REFRESHED when the fresh values beat the champion by
    ``promote_margin`` on at least one axis it was RECORDED as winning, and
    regress no monotonicity namespace: it takes the fresh axis values and
    scalar, every axis it now wins on, this champion's id, and
    ``confirmed_round``. Otherwise it is RETIRED with reason ``unconfirmed``:
    a fresh win on some other axis is a new observation and replicates
    nothing. Dominance between members is not re-run: the other members'
    values come from different draws.

    A generation that is not a member, or a placebo champion, changes
    nothing.
    """
    from zicato.tournament.gate import regressed_namespaces  # noqa: PLC0415

    current = next((m for m in frontier.members if m.generation_id == member.generation_id), None)
    if current is None or champion.is_placebo:
        return FrontierUpdate(frontier=frontier)
    axes = frontier_axes(weights, champion.aggregate)
    values = axis_values(member.aggregate, axes)
    champion_values = axis_values(champion.aggregate, axes)
    beats = beats_on(values, champion_values, margin=float(weights.promote_margin))
    others = tuple(m for m in frontier.members if m is not current)
    if not set(beats) & set(current.beats_champion_on) or regressed_namespaces(
        dict(champion.aggregate), dict(member.aggregate), weights
    ):
        retired = (
            *frontier.retired,
            RetiredMember(member=current, round_retired=round_index, reason=RETIRED_UNCONFIRMED),
        )
        return FrontierUpdate(
            frontier=replace(
                frontier,
                updated_round=round_index,
                members=others,
                retired=tuple(
                    sorted(retired, key=lambda r: (r.round_retired, r.member.generation_id))
                ),
            ),
            retired=(current.generation_id,),
        )
    refreshed = _member_from(
        member,
        values=values,
        champion_values=champion_values,
        beats=beats,
        champion_generation_id=champion.generation_id,
        round_index=current.round_admitted,
        confirmed_round=round_index,
    )
    return FrontierUpdate(
        frontier=replace(
            frontier,
            updated_round=round_index,
            members=tuple(sorted((*others, refreshed), key=lambda m: m.generation_id)),
        ),
        confirmed=(current.generation_id,),
    )


def render_frontier_tradeoffs(frontier: ParetoFrontier) -> str:
    """The record as the operator's trade-off table, in Markdown.

    One row per member: the axes it beats its reference champion on and the
    scalar points it gains on each, its scalar, the champion those values
    were measured against, and whether a fresh measurement has confirmed
    them. Adoption is described once, below the table. Reads the record
    alone, so it needs no workspace. Empty when the record has no members.
    """
    if not frontier.members:
        return ""
    rows = [
        "| Candidate | Beats the champion on (scalar points gained) | Scalar | Measured against"
        " | Confirmed |",
        "|---|---|---:|---|---|",
    ]
    for member in frontier.members:
        gains = member.beats_champion_by
        wins = ", ".join(
            f"`{axis}`" + (f" ({gains[axis]:.4g})" if axis in gains else "")
            for axis in member.beats_champion_on
        )
        scalar = "n/a" if member.scalar is None else f"{member.scalar:.4f}"
        confirmed = (
            "unconfirmed (single measurement)"
            if member.confirmed_round is None
            else f"confirmed (round {member.confirmed_round})"
        )
        rows.append(
            f"| `{member.generation_id}` | {wins or 'none recorded'} | {scalar} "
            f"| `{member.champion_generation_id}` | {confirmed} |"
        )
    return "\n".join(
        [
            "## Trade-off candidates",
            "",
            f"Rejected candidates of epoch `{frontier.epoch_id}` that beat the champion by at"
            f" least the promote margin ({frontier.margin:g} scalar points) on a scored axis,"
            " lost on the weighted scalar, and are dominated by no other candidate listed. Lower"
            " is better on every axis and on the scalar. An unconfirmed row rests on the one"
            " measurement that selected it, which favours it and may be noise.",
            "",
            *rows,
            "",
            "To adopt a candidate, queue a promote override for it while its round is still"
            " running (`control/promote/<generation>`, or the dashboard's promote control): the"
            " round settles with that candidate promoted and its outcome records"
            " `operator_override`. An override queued after the round has settled is never"
            " applied. A settled candidate's patches are in"
            " `generations/<generation>/experiment.json` under the epoch directory.",
        ]
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
    body: dict[str, Any] = {
        "generation_id": member.generation_id,
        "round_admitted": member.round_admitted,
        "champion_generation_id": member.champion_generation_id,
        "axis_values": dict(member.axis_values),
        "beats_champion_on": list(member.beats_champion_on),
        "scalar": member.scalar,
    }
    # Written only when present, so a member from a record that predates
    # these fields is rewritten in the shape it was read in.
    if member.beats_champion_by:
        body["beats_champion_by"] = dict(member.beats_champion_by)
    if member.confirmed_round is not None:
        body["confirmed_round"] = member.confirmed_round
    return body


def _float_map(raw: Any) -> dict[str, float]:
    """A stored ``{axis: number}`` mapping, dropping entries that are not numbers."""
    values: dict[str, float] = {}
    if isinstance(raw, Mapping):
        for ns, value in raw.items():
            try:
                values[str(ns)] = float(value)
            except (TypeError, ValueError):
                continue
    return values


def _member_from_dict(body: Mapping[str, Any]) -> FrontierMember:
    values = _float_map(body.get("axis_values"))
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
        beats_champion_by=_float_map(body.get("beats_champion_by")),
        confirmed_round=(
            body["confirmed_round"] if type(body.get("confirmed_round")) is int else None
        ),
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
    from zicato.workspace.projection import mark_epoch_changed  # noqa: PLC0415

    mark_epoch_changed(workspace_root, epoch_id)
    atomic_write_json(frontier_path(workspace_root, epoch_id), frontier_to_dict(frontier))


def frontier_tradeoffs_section(workspace_root: Path, epoch_id: str) -> str:
    """One epoch's trade-off table read from its canonical record.

    What ``analysis.md`` and the epoch-close output print. Empty when the
    epoch has no members. Total: a record that cannot be read or rendered,
    for any reason, yields an empty section and one warning, because a
    report and an epoch close must still complete beside a record that needs
    repair.
    """
    try:
        return render_frontier_tradeoffs(load_frontier(workspace_root, epoch_id))
    except Exception as exc:  # noqa: BLE001 — a record must never fail a close
        import logging  # noqa: PLC0415

        message = "frontier: epoch %s — unreadable record, trade-off table is omitted (%s)"
        logging.getLogger("zicato.orchestrator").warning(message, epoch_id, exc)
        return ""


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
    untouched rather than rewritten, so the record's mtime means "something
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
    "DIMENSION_AGGREGATES_KEY",
    "DOMINATED_BY_PREFIX",
    "FRONTIER_DIMENSION_CAP",
    "FRONTIER_FILENAME",
    "RETIRED_DOMINATED_BY_CHAMPION",
    "RETIRED_MONOTONICITY",
    "RETIRED_PROMOTED",
    "RETIRED_UNCONFIRMED",
    "FrontierCandidate",
    "FrontierMember",
    "FrontierUpdate",
    "ParetoFrontier",
    "RetiredMember",
    "axis_values",
    "beats_on",
    "confirm_member",
    "dominates",
    "frontier_axes",
    "frontier_from_dict",
    "frontier_path",
    "frontier_to_dict",
    "frontier_tradeoffs_section",
    "load_frontier",
    "record_frontier",
    "render_frontier_tradeoffs",
    "save_frontier",
    "update_frontier",
    "uses_dimension_axes",
]
