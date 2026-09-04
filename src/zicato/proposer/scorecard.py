"""The proposer scorecard — per-epoch proposal quality, read from what already ran.

The loop measures the proposer constantly, for free: every round log records
the proposal attempts it made, the validator errors they hit, the screen's
verdict on each slate candidate, the gate's numbers on the child that reached
it, and the terminal decision. Nothing READ those signals as a picture of
proposer quality. This module is that reader — a **pure** one: it opens the
round logs, the epoch configs and the per-generation ``experiment.json`` files
that the loop already wrote, and writes nothing.

Because the proposer is frozen for an epoch (PROPOSER.md §4 — any semantic
change to its skills/agent rolls the epoch), the epoch is the only granularity
at which a proposal-quality number means anything: two epochs' proposals came
from two different proposers. So the unit of the scorecard is one epoch, and
the trend is a sequence of epochs — proposer lineage with a fitness signal.

Honesty rules (the whole reason this is a typed reader and not a dict of floats)
-------------------------------------------------------------------------------
* **Null is not zero.** :class:`Rate` carries its numerator AND denominator;
  ``value`` is ``None`` when nothing was observed. A proposer that never had a
  candidate screened has NO screen-veto rate — reporting ``0.0`` would read as
  "screened plenty, vetoed none", which is the opposite claim.
* **The sample count rides every rate.** ``n`` is in the dataclass and in
  ``to_json``; no surface can render the rate without it.
* **Small samples are marked.** Below :data:`MIN_SAMPLE_N` observations a rate
  is ``provisional`` — still reported (suppressing it loses information) but
  flagged, so four rounds of noise never reads as a measured base rate.

What each aggregate means
-------------------------
* **Validator-failure rates** — per post-apply check code (``A1``…``A4``, from
  :data:`zicato.mutation.validator.POST_APPLY_CHECKS`), the fraction of
  PROPOSAL ATTEMPTS that hit that check. Classification is structural
  (:func:`~zicato.mutation.validator.classify_post_apply_error` reads the
  code the validator stamps); an error carrying no recognised code counts under
  ``unclassified`` rather than being attributed to a check that may not have run.
* **Screen-veto rate** — vetoed candidates over screened candidates. Both are
  ``0`` for a contract that does not opt into screening, so the rate is null.
* **Gate margins** — over children that actually reached the gate with all
  three numbers recorded. The loop scores a LOSS, so the improvement the child
  delivered is ``champion_scalar - challenger_scalar`` and the gate promotes
  when that clears ``margin_required``; ``headroom`` is the signed distance to
  that bar. Gates whose scalars predate the fields are counted as ``unmeasured``
  and excluded from the statistics rather than defaulted to zero.
* **Revision-success rate** — of the bounded screen-informed revise re-samples
  an all-vetoed slate may take, the fraction that survived the screen. A
  re-sample that survives is the one the selector then picks
  (``critique_selected.reason == "screen_revise_survivor"``), so the screened
  verdict and the definitive token agree. The denominator counts re-samples
  that PRODUCED a candidate: a revise whose propose call raised emits no
  ``candidate_screened`` at all, so it is invisible here — the rate answers
  "when the revise produced something, did it survive", not "did the revise
  mechanism work at all".
* **Cost per accepted proposal** — in the two units the round log actually
  records: proposer attempts (the sampling calls) and board units (the
  ``(entry, replicate, side)`` runs the tournament spent). Both are ``None``
  when the epoch accepted nothing — a divide-by-zero is not "free".
* **Per-mutation-site track records** — for each ``mutation_id`` the proposer
  patched, how many rounds proposed it and how many of those promoted.

Re-run rounds
-------------
One ``round_log.jsonl`` can hold more than one attempt at the same round index
(a round that applied patches but died before its experiment was written never
consumes its index). The two families of aggregate take opposite slices, and
:func:`_read_rounds` documents why: gate/decision facts come from the FINAL
attempt only, proposal-failure and cost facts from EVERY attempt.

Redaction
---------
Nothing here reads board content. ``UnitCompleted`` carries an ``entry_id`` and
``GateEvaluated`` carries ``attributable_regressions``; this reader COUNTS the
former and never touches the latter, so no entry id, task text, or holdout
anything can reach a scorecard. That is a construction property, pinned by the
leak-probe test — the same envelope the proposer's failure-mode channel keeps
(PROPOSER.md §2.5).
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from zicato.epoch._storage import RecordError
from zicato.epoch.round_log import (
    CandidateScreened,
    GateEvaluated,
    ProposalAttempted,
    ProposalEpisodeSettled,
    RoundLog,
    RoundLogEnvelope,
    RoundOpened,
    RoundRecord,
    fold_round_record,
)
from zicato.mutation.validator import POST_APPLY_CHECKS, classify_post_apply_error
from zicato.workspace import WorkspaceLayout, round_indices

#: Observations below which a rate is reported but marked ``provisional``. Four
#: rounds of a fresh epoch are noise rather than a base rate; the marker says
#: so without hiding the number.
MIN_SAMPLE_N: int = 5

#: The bucket for a proposal error that carries no recognised check code — a
#: proposer parse failure, a slate slot's credential lapse, or a validator
#: error from a log written before the codes existed. Named explicitly so it
#: is never silently folded into a real check's rate.
UNCLASSIFIED: str = "unclassified"

#: The terminal round decision that counts a proposal as ACCEPTED.
_PROMOTED: str = "promoted"

#: How each episode outcome kind is labelled on the card. The three that
#: keep their own word do; ``failed`` reads as ``errored``, which is the
#: word the card has always used for a round the proposer crashed in.
_OUTCOME_LABEL: dict[str, str] = {
    "completed": "completed",
    "blocked": "blocked",
    "exhausted": "exhausted",
    "failed": "errored",
}


@dataclass(frozen=True, slots=True)
class Rate:
    """A measured rate that cannot be rendered without its sample count.

    ``k`` of ``n`` observations. ``value`` is ``None`` for ``n == 0`` — the
    honest "not measured", distinct from a measured ``0.0``.
    """

    k: int = 0
    n: int = 0

    @property
    def value(self) -> float | None:
        """The rate, or ``None`` when nothing was observed."""
        if self.n <= 0:
            return None
        return self.k / self.n

    @property
    def provisional(self) -> bool:
        """True for a rate measured over fewer than :data:`MIN_SAMPLE_N` samples."""
        return 0 < self.n < MIN_SAMPLE_N

    def to_json(self) -> dict[str, Any]:
        return {
            "k": self.k,
            "n": self.n,
            "value": self.value,
            "provisional": self.provisional,
        }


@dataclass(frozen=True, slots=True)
class MarginStats:
    """The gate's numbers on the children that reached it.

    ``achieved`` is ``champion_scalar - challenger_scalar`` (the loss the child
    removed); ``headroom`` is ``achieved - margin_required`` (signed distance to
    the promote bar, negative for a child the gate rejected on Rule 1).
    ``unmeasured`` counts gates that fired without recording their scalars —
    logs older than the fields — and those contribute to NO statistic.
    """

    n: int = 0
    unmeasured: int = 0
    achieved_median: float | None = None
    achieved_min: float | None = None
    achieved_max: float | None = None
    headroom_median: float | None = None

    @property
    def provisional(self) -> bool:
        return 0 < self.n < MIN_SAMPLE_N

    def to_json(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "unmeasured": self.unmeasured,
            "achieved_median": self.achieved_median,
            "achieved_min": self.achieved_min,
            "achieved_max": self.achieved_max,
            "headroom_median": self.headroom_median,
            "provisional": self.provisional,
        }


@dataclass(frozen=True, slots=True)
class CostAggregate:
    """What one accepted proposal cost, in the units the round log records.

    Wall-clock and token spend are not on disk per proposal, so this reports
    the two meters that ARE: proposer sampling attempts, and the board units a
    tournament spent. Both per-acceptance figures are ``None`` when the epoch
    accepted nothing — an epoch with zero promotions has no cost-per-promotion,
    and rendering ``0`` or ``inf`` would both be lies.
    """

    accepted: int = 0
    proposal_attempts: int = 0
    candidates_sampled: int = 0
    board_units: int = 0

    @property
    def attempts_per_acceptance(self) -> float | None:
        return self.proposal_attempts / self.accepted if self.accepted else None

    @property
    def units_per_acceptance(self) -> float | None:
        return self.board_units / self.accepted if self.accepted else None

    def to_json(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "proposal_attempts": self.proposal_attempts,
            "candidates_sampled": self.candidates_sampled,
            "board_units": self.board_units,
            "attempts_per_acceptance": self.attempts_per_acceptance,
            "units_per_acceptance": self.units_per_acceptance,
        }


@dataclass(frozen=True, slots=True)
class MutationSiteRecord:
    """One mutation site's track record under this epoch's proposer."""

    mutation_id: str
    proposed: int = 0
    promoted: int = 0

    @property
    def promote_rate(self) -> Rate:
        return Rate(k=self.promoted, n=self.proposed)

    def to_json(self) -> dict[str, Any]:
        return {
            "mutation_id": self.mutation_id,
            "proposed": self.proposed,
            "promoted": self.promoted,
            "promote_rate": self.promote_rate.to_json(),
        }


@dataclass(frozen=True, slots=True)
class ProposerScorecard:
    """One epoch's proposal-quality picture, folded from its round logs.

    ``proposer_agent_id`` / ``proposer_skills`` pin WHICH proposer earned this
    card — the epoch record's frozen proposer identity, so a trend row is
    always attributable.
    """

    epoch_id: str
    proposer_agent_id: str = ""
    proposer_skills: tuple[str, ...] = ()
    rounds: int = 0
    rounds_complete: int = 0
    proposals: int = 0
    promote_rate: Rate = field(default_factory=Rate)
    validation_failure_rate: Rate = field(default_factory=Rate)
    validator_failure_rates: dict[str, Rate] = field(default_factory=dict)
    screen_veto_rate: Rate = field(default_factory=Rate)
    revision_success_rate: Rate = field(default_factory=Rate)
    #: How the epoch's proposal episodes ended, counted by outcome kind:
    #: completed, blocked, exhausted, errored. A round that produced no
    #: experiment says which of the three non-completing endings it
    #: reached, because each names a different remedy.
    episode_outcomes: dict[str, int] = field(default_factory=dict)
    #: Blocked episodes counted by their cause, one key per
    #: :data:`~zicato.core.types.ProposerBlockedCode` the epoch saw.
    blocked_codes: dict[str, int] = field(default_factory=dict)
    #: Exhausted episodes counted by the budget dimension that ran out.
    exhausted_limits: dict[str, int] = field(default_factory=dict)
    margins: MarginStats = field(default_factory=MarginStats)
    cost: CostAggregate = field(default_factory=CostAggregate)
    mutation_sites: tuple[MutationSiteRecord, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "epoch_id": self.epoch_id,
            "proposer_agent_id": self.proposer_agent_id,
            "proposer_skills": list(self.proposer_skills),
            "rounds": self.rounds,
            "rounds_complete": self.rounds_complete,
            "proposals": self.proposals,
            "promote_rate": self.promote_rate.to_json(),
            "validation_failure_rate": self.validation_failure_rate.to_json(),
            "validator_failure_rates": {
                code: rate.to_json() for code, rate in sorted(self.validator_failure_rates.items())
            },
            "screen_veto_rate": self.screen_veto_rate.to_json(),
            "revision_success_rate": self.revision_success_rate.to_json(),
            "episode_outcomes": dict(sorted(self.episode_outcomes.items())),
            "blocked_codes": dict(sorted(self.blocked_codes.items())),
            "exhausted_limits": dict(sorted(self.exhausted_limits.items())),
            "margins": self.margins.to_json(),
            "cost": self.cost.to_json(),
            "mutation_sites": [s.to_json() for s in self.mutation_sites],
            "min_sample_n": MIN_SAMPLE_N,
        }


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def _round_indices(workspace_root: Path, epoch_id: str) -> list[int]:
    """The epoch's round indices, ascending; empty when nothing ran."""
    return round_indices(WorkspaceLayout.from_root(workspace_root), epoch_id)


def _final_attempt_span(events: list[RoundLogEnvelope]) -> list[RoundLogEnvelope]:
    """The envelopes from the LAST ``round_opened`` onward.

    One round log can hold MORE THAN ONE attempt at the same round index: a
    round that applied patches but died before its experiment was written never
    consumes its index, so the next invocation reopens it and APPENDS to the
    same log (:func:`zicato.epoch.round_integrity._final_attempt_span`, which
    this mirrors). ``fold_round_record`` accumulates across the whole stream and
    cannot tell the attempts apart.
    """
    last_open = -1
    for index, envelope in enumerate(events):
        if isinstance(envelope.event, RoundOpened):
            last_open = index
    return events if last_open < 0 else events[last_open:]


def _read_rounds(
    workspace_root: Path, epoch_id: str
) -> list[tuple[list[RoundLogEnvelope], RoundRecord]]:
    """Every readable round, as ``(all events, final-attempt record)`` pairs.

    The two halves answer two different questions, and a re-run round is where
    the difference bites.

    * The RECORD is folded from the FINAL attempt span only. A round the loop
      reopened carries the earlier attempt's gate and decision in the same log,
      and counting both would let a dead attempt contribute a second gate — and
      possibly a second promotion — to a round the epoch settled once. The last
      attempt is the one whose outcome the epoch actually carries, so gates,
      the decision, the applied generations and the units all come from it.
    * The EVENTS are the whole stream, because proposal attempts are the
      opposite case: a failed attempt is not noise to be sliced away, it is
      the signal this scorecard exists to measure. Slicing to the
      final span would hide the failures that caused the re-run — the rate would
      improve exactly when the proposer did worst. Sampling cost is counted the
      same way: a call spent on an attempt that later died was still spent.

    Reading the raw events also recovers what the fold discards: the per-attempt
    grouping the A1–A4 rates are defined over (the fold flattens every attempt's
    errors into one tuple) and the ``revise`` flag that separates a re-sample
    from an ordinary slate slot (the fold folds its veto in with the slate's).

    A corrupt interior line raises out of :meth:`RoundLog.read` by design (the
    append-only invariant was violated). The scorecard is a REPORT rather than
    the loop, so one damaged round must not deny the operator the other twenty —
    it is skipped, and shows up as a gap between the round directories on disk
    and the ``rounds`` count.
    """
    out: list[tuple[list[RoundLogEnvelope], RoundRecord]] = []
    for index in _round_indices(workspace_root, epoch_id):
        try:
            events = RoundLog(workspace_root, epoch_id, index).read()
        except (OSError, ValueError):
            continue
        out.append((events, fold_round_record(_final_attempt_span(events))))
    return out


def _classify_attempts(
    attempt_errors: list[tuple[str, ...]],
) -> tuple[dict[str, int], int]:
    """Count attempts hitting each check code; return ``(per_code, failed)``.

    An attempt is counted at most ONCE per code however many errors of that
    code it raised — the rate is "attempts that hit A4" rather than "A4
    errors".
    """
    per_code: dict[str, int] = dict.fromkeys((*POST_APPLY_CHECKS, UNCLASSIFIED), 0)
    failed = 0
    for errors in attempt_errors:
        if not errors:
            continue
        failed += 1
        codes = {classify_post_apply_error(e) or UNCLASSIFIED for e in errors}
        for code in codes:
            per_code[code] = per_code.get(code, 0) + 1
    return per_code, failed


def _margin_stats(gates: list[GateEvaluated]) -> MarginStats:
    """Fold the gate events into :class:`MarginStats`, excluding unmeasured gates."""
    achieved: list[float] = []
    headroom: list[float] = []
    unmeasured = 0
    for gate in gates:
        if gate.champion_scalar is None or gate.challenger_scalar is None:
            unmeasured += 1
            continue
        delta = gate.champion_scalar - gate.challenger_scalar
        achieved.append(delta)
        if gate.margin_required is not None:
            headroom.append(delta - gate.margin_required)
    if not achieved:
        return MarginStats(n=0, unmeasured=unmeasured)
    return MarginStats(
        n=len(achieved),
        unmeasured=unmeasured,
        achieved_median=statistics.median(achieved),
        achieved_min=min(achieved),
        achieved_max=max(achieved),
        headroom_median=statistics.median(headroom) if headroom else None,
    )


def _mutation_sites(
    workspace_root: Path,
    epoch_id: str,
    records: list[RoundRecord],
) -> tuple[MutationSiteRecord, ...]:
    """Per-``mutation_id`` proposed/promoted counts, read from the experiments.

    A round's generations are the children it minted; each one's
    ``experiment.json`` names the sites it patched. A generation whose
    experiment is missing or unreadable contributes nothing — the site counts
    are then simply smaller, never wrong.

    The promotion credit goes to the ONE generation the decision names
    (``provenance.promoted_generation_id``) rather than to every child of a promoted
    round. A multi-challenger structure mints several children and promotes at
    most one, so crediting the round would inflate every losing challenger's
    site to a winner — and the sites a multi-challenger round explores are
    exactly the ones a reflection pass would then wrongly praise. A log whose
    provenance predates the field falls back to the unambiguous case: credit
    only when the round minted a single child.
    """
    from zicato.epoch.journal import read_experiment  # noqa: PLC0415

    proposed: dict[str, int] = {}
    promoted: dict[str, int] = {}
    for record in records:
        winner: str | None = None
        if record.decision == _PROMOTED:
            raw = record.decision_provenance.get("promoted_generation_id")
            if isinstance(raw, str) and raw:
                winner = raw
            elif len(record.generation_ids) == 1:
                winner = record.generation_ids[0]
        for generation_id in record.generation_ids:
            try:
                experiment = read_experiment(workspace_root, epoch_id, generation_id)
            except (OSError, RecordError, ValueError, KeyError, FileNotFoundError):
                continue
            for mutation_id in {p.mutation_id for p in experiment.patches}:
                proposed[mutation_id] = proposed.get(mutation_id, 0) + 1
                if generation_id == winner:
                    promoted[mutation_id] = promoted.get(mutation_id, 0) + 1
    return tuple(
        MutationSiteRecord(
            mutation_id=mutation_id,
            proposed=count,
            promoted=promoted.get(mutation_id, 0),
        )
        # Worst first: the sites the proposer keeps failing on are the ones a
        # reflection pass has something to say about.
        for mutation_id, count in sorted(
            proposed.items(), key=lambda kv: (promoted.get(kv[0], 0) / kv[1], -kv[1], kv[0])
        )
    )


def _proposer_identity(workspace_root: Path, epoch_id: str) -> tuple[str, tuple[str, ...]]:
    """The epoch's frozen proposer identity, or empty when it cannot be resolved."""
    from zicato.epoch.lifecycle import load_epoch  # noqa: PLC0415
    from zicato.proposer.skills import resolve_proposer_spec  # noqa: PLC0415

    try:
        epoch_cfg = load_epoch(workspace_root, epoch_id)
        spec = resolve_proposer_spec(epoch_cfg.proposer_path)
    except (OSError, ValueError, KeyError, FileNotFoundError):
        return "", ()
    return spec.agent_id, tuple(skill.name for skill in spec.skills)


def read_epoch_scorecard(workspace_root: Path, epoch_id: str) -> ProposerScorecard:
    """Fold one epoch's round logs into a :class:`ProposerScorecard`.

    Pure read: opens round logs, the epoch config and the per-generation
    experiments, and writes nothing. An epoch that never ran a round returns a
    card whose every rate is null, which is the correct report rather than an
    error.
    """
    rounds = _read_rounds(workspace_root, epoch_id)

    attempt_errors: list[tuple[str, ...]] = []
    gates: list[GateEvaluated] = []
    screened = 0
    vetoes = 0
    revise_screened = 0
    revise_survived = 0
    episode_outcomes: dict[str, int] = {}
    blocked_codes: dict[str, int] = {}
    exhausted_limits: dict[str, int] = {}
    candidates_sampled = 0
    board_units = 0
    promotions = 0
    for events, record in rounds:
        for envelope in events:
            event = envelope.event
            if isinstance(event, ProposalAttempted):
                attempt_errors.append(event.errors)
            elif isinstance(event, ProposalEpisodeSettled):
                kind = _OUTCOME_LABEL.get(event.kind, event.kind)
                episode_outcomes[kind] = episode_outcomes.get(kind, 0) + 1
                if event.kind == "blocked" and event.code:
                    blocked_codes[event.code] = blocked_codes.get(event.code, 0) + 1
                elif event.kind == "exhausted" and event.code:
                    exhausted_limits[event.code] = exhausted_limits.get(event.code, 0) + 1
            elif isinstance(event, CandidateScreened):
                screened += 1
                if event.vetoed:
                    vetoes += 1
                if event.revise:
                    revise_screened += 1
                    if not event.vetoed:
                        revise_survived += 1
        gates.extend(record.gates)
        candidates_sampled += record.proposal.candidates_sampled
        board_units += len(record.units)
        if record.decision == _PROMOTED:
            promotions += 1

    per_code, failed_attempts = _classify_attempts(attempt_errors)
    proposals = len(attempt_errors)
    agent_id, skills = _proposer_identity(workspace_root, epoch_id)
    records = [record for _events, record in rounds]

    return ProposerScorecard(
        epoch_id=epoch_id,
        proposer_agent_id=agent_id,
        proposer_skills=skills,
        rounds=len(records),
        rounds_complete=sum(1 for r in records if r.complete),
        proposals=proposals,
        promote_rate=Rate(k=promotions, n=len(records)),
        validation_failure_rate=Rate(k=failed_attempts, n=proposals),
        validator_failure_rates={
            code: Rate(k=count, n=proposals) for code, count in sorted(per_code.items())
        },
        screen_veto_rate=Rate(k=vetoes, n=screened),
        revision_success_rate=Rate(k=revise_survived, n=revise_screened),
        episode_outcomes=episode_outcomes,
        blocked_codes=blocked_codes,
        exhausted_limits=exhausted_limits,
        margins=_margin_stats(gates),
        cost=CostAggregate(
            accepted=promotions,
            proposal_attempts=proposals,
            candidates_sampled=candidates_sampled,
            board_units=board_units,
        ),
        mutation_sites=_mutation_sites(workspace_root, epoch_id, records),
    )


def read_scorecard_trend(
    workspace_root: Path,
    *,
    limit: int | None = None,
) -> list[ProposerScorecard]:
    """One card per epoch, in the workspace's canonical epoch order.

    ``limit`` keeps the ``limit`` MOST RECENT epochs (the tail of the canonical
    order) — a trend reads forward, so the rendered slice must still be
    chronological. Epochs whose config will not load are skipped: an
    unreadable contract has no attributable proposer, and a row that cannot say
    which proposer earned it is worse than an absent row.
    """
    from zicato.epoch.lifecycle import list_epochs  # noqa: PLC0415

    try:
        epochs = list_epochs(workspace_root)
    except (OSError, ValueError, FileNotFoundError):
        return []
    selected = epochs if limit is None else epochs[-limit:]
    return [read_epoch_scorecard(workspace_root, cfg.id) for cfg in selected]


__all__ = [
    "MIN_SAMPLE_N",
    "UNCLASSIFIED",
    "CostAggregate",
    "MarginStats",
    "MutationSiteRecord",
    "ProposerScorecard",
    "Rate",
    "read_epoch_scorecard",
    "read_scorecard_trend",
]
