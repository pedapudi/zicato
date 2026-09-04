"""Best-of-N sampling + a self-critique pass over the proposer step.

The proposal-quality lever of FUNCTIONALITY-RECOMMENDATIONS.md §4.1. A single
proposer call takes ONE sample, and its retry loop fires only on *invalid*
output, so a valid-but-mediocre proposal is never reconsidered. This module
wraps any :class:`~zicato.proposer.agent.ProposerAgent` so that, per
propose-step, it samples ``best_of_n`` candidate experiments and then a cheap
self-critique pass — or, with critique disabled, a deterministic heuristic —
picks the best against a quality bar: grounding in a tool call, a real
targeted failure mode, and a minimal diff.

Best-of-N is the DEFAULT (``proposer_quality.best_of_n == 3``): each
propose-step samples a slate of three and critiques. Each slate slot carries a
distinct edit-class hint (:data:`EDIT_CLASS_HINTS`) so the N samples explore
different edit strategies rather than re-rolling one. A contract that pins
``best_of_n: 1`` short-circuits to a single inner ``propose`` call with NO
critique and NO extra work (:meth:`BestOfNProposerAgent.propose`) — a
single-sample proposer, the right pin for scripted/deterministic proposers.

Slate concurrency
-----------------
The N samples are independent — each varies only by a deterministic
per-slot hint (:func:`~zicato.proposer.hints.hint_for_slot` +
:func:`~zicato.proposer.hints.strategy_for_slot`) and its OWN per-slot scratch
VALIDATION tree — so :meth:`BestOfNProposerAgent.propose` gathers them under an
``asyncio.Semaphore`` sized from ``propose_parallelism`` (threaded from
:attr:`~zicato.core.runtime.RuntimeConfig.propose_parallelism`). Each slot
leases a fresh scratch validator from
``ProposerContext.scratch_validator_factory`` (built beside the shared
post-apply validator by
:func:`zicato.evolve.round.build_scratch_validator_factory`), so no two slots
ever derive into the same on-disk tree — the shared ``next_id`` derive that
would otherwise serialise the slate is done EXACTLY ONCE, for the chosen
candidate, after selection
(:meth:`BestOfNProposerAgent._mount_chosen`). A deterministic post-gather pass
emits the ``candidate_sampled`` events and appends candidates in SLOT order, so
the slate, event sequence, and chosen candidate are byte-identical regardless
of completion order; ``propose_parallelism == 1`` runs the slate serially.

Candidate screening (tryouts; opt-in)
-------------------------------------
When the contract opts in (``proposer_quality.screen_entries > 0`` with
``best_of_n > 1``), the orchestrator threads a per-round
:data:`ScreenRunner` on :attr:`~zicato.proposer.agent.ProposerContext
.screen_candidates` and the wrapper calls it GUARDED once the slate
settles: each candidate runs a small rotating TRAIN panel
(:mod:`zicato.epoch.screen`) and a catastrophic regression is VETOED
before the selection pass. Veto-first by design — the screen
disqualifies, it never ranks; the critic/heuristic chooses among the
survivors, an all-vetoed slate degrades to critic-over-all, and any
screen failure degrades to an unscreened selection (screening can never
fail a propose). The survivors' counts-only panel measurements feed the
selection only as a LATE tiebreak (suppressed entirely by
``screen_veto_only``); the panel scalar is selection-biased and is never
journaled as evidence.

Screen-informed revise (bounded, rides the screen opt-in)
---------------------------------------------------------
When the screen runs and the slate ends ALL-VETOED, the wrapper takes
exactly ONE revise pass before degrading to critic-over-all: it
re-samples a single replacement candidate with the slate's COUNTS-ONLY
veto summary seeded into the repair-feedback machinery
(:attr:`~zicato.proposer.agent.ProposerContext.revise_feedback` — the
same ``feedback`` slot a validation failure threads on retry), screens
the replacement GUARDED, and returns it when it survives
(``screen_revise_survivor``). A replacement that is itself vetoed — or a
revise the inner proposer cannot produce — falls back to the existing
``screen_all_vetoed`` critic-over-all degrade. There is NO new config
knob: the revise rides ``screen_entries > 0``, because an all-vetoed
slate with no revise wastes the whole propose step on a known-vetoed
candidate — the single re-sample is the cheapest possible recovery, and
a contract that opted into paying for the screen has already accepted
that propose-step cost class. With screening off nothing here runs.

The recombination slot (opt-in, rides ``recombine`` + ``best_of_n > 1``)
------------------------------------------------------------------------
When the orchestrator threads a per-round
:attr:`~zicato.proposer.agent.ProposerContext.recombine_pair` (two
rejected complementary challengers of the current reign, selected by
:mod:`zicato.epoch.recombine`), the LAST slate slot COMPOSES their union
instead of sampling the LLM. Two merge modes (PROPOSER.md §2.6.1),
chosen by ``proposer_quality.recombine_merge``:

* ``"mechanical"`` (default) — a PURE mint of the disjoint patch union
  (:mod:`zicato.proposer.recombine`), NO LLM call — cost-neutral by
  construction (the mint REPLACES the slot's evaluation propose call, so a
  recombining round spends ``best_of_n − 1`` calls).
* ``"llm"`` — ONE evaluation merge call (the depth refinement role) whose
  response flows through the NORMAL parse/validate path; it SUBSTITUTES the
  slot's own sample call (cost: ``best_of_n`` calls, a recombine-off round)
  and reaches OVERLAPPING pairs the mechanical mint cannot compose.

Either composition runs ``enforce_forbidden`` plus the SAME validate hook
every sample runs; any finding degrades the slot to a normal fresh sample.
A NON-VETOED mint/merge short-circuits the selection
(``selection_mode="recombined"``, no critic call) because the heuristic's
minimal-diff key would systematically starve the union — its diff is
larger than either parent's by construction; a VETOED one stays an
ordinary slate member and goes through the normal selection.

Ensemble proposer roles
-----------------------
The generic wrapper may use separate generation and review roles. Both live in
one proposer trust domain. Session-native proposers own both stages and reject
those overrides. Configuration and routing details live in
``docs/design/PROPOSER.md``.

Overfitting discipline (LOAD-BEARING)
-------------------------------------
The self-critique pass sees ONLY the SAME restricted prompt context the
proposer itself sees — the train-slice patterns (aggregated when
``restrict_visibility``), the banded experiment memory, the bucketed
failure-mode profile — assembled by the same
:func:`~zicato.proposer.foe_request.render_evidence` renderer under the same
``restrict_visibility`` flag. It NEVER sees the holdout and never sees a
per-entry identity the proposer is not already allowed to see. The critic is
inside the same overfitting-visibility envelope as the proposer
(OVERFITTING.md §11); it cannot widen what the proposer learns about the
board. The candidates it ranks are the proposer's own outputs, which are
already inside that envelope. The screen's feeds keep the same envelope:
counts only, never an entry id.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from zicato.core.types import CallLLM, Experiment, ProposerQualityConfig
from zicato.proposer.agent import ProposerAgent, ProposerContext

# EDIT_CLASS_HINTS moved to :mod:`zicato.proposer.hints` (its canonical home,
# alongside the failure-mode-conditioned FAILURE_MODE_HINTS and the pure
# slot→hint mapping); re-exported here so every existing import keeps working.
from zicato.proposer.hints import EDIT_CLASS_HINTS, hint_for_slot, strategy_for_slot
from zicato.proposer.input_capture import (
    ROLE_CRITIQUE,
    ROLE_RECOMBINE_MERGE,
    capture_proposer_input,
)
from zicato.proposer.proposer import ProposerError
from zicato.scoring.diff_complexity import diff_char_size as _diff_size

log = logging.getLogger("zicato.proposer.best_of_n")


@dataclass(frozen=True, slots=True)
class CandidateScreenResult:
    """One slate candidate's pre-tournament screen verdict (tryouts).

    Produced per candidate by the screen runner
    (:mod:`zicato.epoch.screen`) and consumed by
    :class:`BestOfNProposerAgent` — VETO-FIRST semantics: the screen
    disqualifies catastrophic regressions, it never ranks; the
    critic/heuristic still chooses among the survivors.

    Fields
    ------
    vetoed:
        The candidate is disqualified from the slate selection (a
        confirmed pass-flip on a champion-passing panel entry, or a
        budget abort). An all-vetoed slate still selects — the wrapper
        falls back to critic-over-all — so a veto can narrow but never
        empty the step.
    reason:
        Human-readable veto/clear summary. COUNTS ONLY by contract —
        never an entry id, never a question/output token — so the string
        can flow into the round log and the (restricted-visibility)
        proposer stack without widening what the proposer may learn
        about the board (OVERFITTING.md §11).
    scalar:
        The candidate's aggregate panel scalar (lower = better), or
        ``None`` when the screen produced no usable signal (every panel
        unit infra-aborted, or the screen errored for this candidate).
        SELECTION-BIASED by construction: it is measured on a small,
        champion-passing panel chosen for the veto — advisory tiebreak
        material only, never journaled as evidence and never compared
        against tournament scalars.
    entries_screened:
        How many panel entries this candidate ran.
    baseline_passes:
        How many of those entries the champion (parent, replicate-0
        baseline) passes — the flip-eligible subset.
    candidate_passes:
        How many panel entries the candidate passed.
    confirmed:
        ``True`` only for a veto that survived the confirm re-run (the
        pass-flip re-ran at the reserved confirm replicate and flipped
        twice). Immediate vetoes (budget aborts) carry ``False``.
    """

    vetoed: bool
    reason: str
    scalar: float | None
    entries_screened: int
    baseline_passes: int
    candidate_passes: int
    confirmed: bool


#: The screen-runner seam (the :data:`~zicato.proposer.proposer.ExperimentValidator`
#: precedent): the orchestrator builds ONE closure per round — binding the
#: rotating train panel, the parent baseline, the adapter and the frozen
#: weights — and threads it on :attr:`ProposerContext.screen_candidates`.
#: Called with the settled slate; returns one result per candidate, in
#: slate order. ``None`` on the context (every caller that does not opt
#: in) screens nothing.
ScreenRunner = Callable[[Sequence[Experiment]], Awaitable[Sequence[CandidateScreenResult]]]


def _noop_cleanup() -> None:
    """The scratch-lease cleanup used when no scratch factory is threaded.

    A context with no ``scratch_validator_factory`` (single-sample proposers
    and every unit-test context that wires no genstore) leases the SHARED
    ``validate_experiment`` hook with this no-op cleanup, so the wrapper's
    per-slot ``finally: cleanup()`` is uniform on both paths.
    """


@dataclass(frozen=True, slots=True)
class _SlotOutcome:
    """One best-of-N slate slot's result, collected for the ordered pass.

    The concurrent gather produces one per slot; the deterministic
    post-gather pass reads them in SLOT order (``sample``) to emit the
    ``candidate_sampled`` events and append candidates, so the observable
    outcome never depends on completion order. A failed slot carries its
    :class:`~zicato.proposer.proposer.ProposerError` (the slate narrows and
    the error is REPORTED — see :meth:`BestOfNProposerAgent.propose`),
    never losing the slots that did succeed.

    ``degraded_errors`` is the SURVIVED-BUT-FAILED channel: a call that was
    swallowed to a degrade rather than failing the slot, so the slot can
    still carry a candidate alongside it. Its one producer is the LLM
    recombination merge (:meth:`BestOfNProposerAgent._merge_recombined`),
    whose call exception degrades the slot to a fresh sample. Carrying that
    exception here is what keeps an outage-driven degrade distinguishable
    from a clean mechanical mint (issue #141).
    """

    sample: int
    candidate: Experiment | None
    error: Any | None
    recombined: bool
    degraded_errors: tuple[str, ...] = ()


#: The recent-lineage hypothesis prediction-accuracy bar above which the
#: candidate selection prefers hypotheses that carry concrete expected
#: movements. When the proposer's recent predictions have mostly borne out,
#: a candidate that states falsifiable expectations is worth more than one
#: that does not; when the lineage's calibration is poor (or unknown), the
#: term is inert so a badly-calibrated proposer is not rewarded for
#: confident guessing. ADVISORY ordering only — never a gate.
CALIBRATION_TRUST_BAR: float = 0.6


def recent_prediction_accuracy(ctx: ProposerContext) -> float | None:
    """The lineage's recent hypothesis prediction-accuracy, or ``None``.

    The mean of the graded ``prediction_accuracy`` values over the
    experiment-memory digest already on the context (the orchestrator's
    curated, capped, restricted view — nothing new is read). ``None`` when
    no settled prior experiment carries a grade (a fresh epoch, or
    prediction grading unavailable).
    """
    values = [
        p.prediction_accuracy for p in ctx.prior_experiments if p.prediction_accuracy is not None
    ]
    if not values:
        return None
    return sum(values) / len(values)


def _carries_expected_movements(experiment: Experiment) -> bool:
    """Whether the candidate's hypothesis states falsifiable movements."""
    hyp = experiment.hypothesis
    return bool(hyp.expected_drift_movements or hyp.expected_metric_movements)


def _targets_observed_failure(experiment: Experiment, ctx: ProposerContext) -> bool:
    """Whether the experiment touches a mutation id an observed pattern flags.

    A candidate that targets a mutation point the detector patterns name as
    *affected* is grounded in a real, observed failure mode rather than a
    speculative edit — the §4.1 quality bar ("targets a real failure mode?").
    Uses only the restricted, train-slice patterns already on the context, so
    the heuristic stays inside the overfitting-visibility envelope.
    """
    affected: set[str] = set()
    for pattern in ctx.patterns:
        affected.update(pattern.affected_mutation_ids)
    if not affected:
        return False
    touched = {p.mutation_id for p in experiment.patches}
    touched.update(experiment.hypothesis.modulating)
    return bool(touched & affected)


def _heuristic_best_index(
    candidates: list[Experiment],
    ctx: ProposerContext,
    screen_scalars: list[float | None] | None = None,
) -> int:
    """Deterministically select the best candidate by the §4.1 quality bar.

    The no-LLM selection (used when critique is disabled, or as the fallback
    when the critique call fails / is unparseable). Ranks by, in order:

    1. **Grounded in an observed failure mode** — a candidate that touches a
       pattern-flagged mutation id beats one that does not.
    2. **Calibration-aware predictions** — WHEN the lineage's recent
       hypothesis ``prediction_accuracy`` clears
       :data:`CALIBRATION_TRUST_BAR`, a candidate whose hypothesis states
       concrete expected movements beats one that does not (the proposer
       has EARNED trust in its predictions, so a falsifiable hypothesis is
       worth acting on). Below the bar — or with no graded history — the
       term is inert for every candidate. Advisory ordering only, never a
       gate.
    3. **Minimal diff** — among equally-ranked candidates, the smaller
       edit wins (MDL parsimony; OVERFITTING.md §5).
    4. **Screen panel scalar** (opt-in tiebreak) — when the candidate
       screen ran and the contract has not pinned ``screen_veto_only``,
       the smaller (better) SELECTION-BIASED panel scalar breaks the
       remaining tie; a ``None`` scalar (no signal) sorts after every
       measured one. ``screen_scalars is None`` (unscreened / veto-only)
       leaves the term constant, so the earlier keys decide alone.
    5. **Stable order** — ties break toward the earlier-sampled candidate, so
       the selection is deterministic for a fixed slate.

    Returns the index into ``candidates``. ``candidates`` is non-empty by
    contract (the caller handles the empty slate).
    """
    accuracy = recent_prediction_accuracy(ctx)
    calibrated = accuracy is not None and accuracy >= CALIBRATION_TRUST_BAR

    def _key(i: int) -> tuple[bool, bool, int, tuple[bool, float], int]:
        # The PENULTIMATE key (after diff size, before the stable index):
        # the screen's panel scalar, lower = better, None-scalar last.
        # Constant (inert) when the screen did not run or is veto-only.
        screen_key = (True, 0.0)
        if screen_scalars is not None:
            scalar = screen_scalars[i]
            screen_key = (scalar is None, scalar if scalar is not None else 0.0)
        return (
            not _targets_observed_failure(candidates[i], ctx),  # grounded first
            # False sorts first: with a calibrated lineage, prediction-bearing
            # candidates rank ahead; otherwise the term is constant (inert).
            calibrated and not _carries_expected_movements(candidates[i]),
            _diff_size(candidates[i]),
            screen_key,
            i,
        )

    best_index = 0
    best_key = _key(0)
    for i in range(1, len(candidates)):
        key = _key(i)
        if key < best_key:
            best_key = key
            best_index = i
    return best_index


def _render_candidate_slate(candidates: list[Experiment]) -> str:
    """Render the sampled candidates compactly for the critic's user prompt.

    One block per candidate: its 0-based index, the hypothesis core idea, the
    targeted mutation ids, a per-patch op + rationale summary, and the diff
    size. Compact by design — the critic ranks the candidates, it does not
    re-derive them — and carries no per-entry board identity (only the
    proposer's own declared targets + rationale, already inside the
    visibility envelope).
    """
    blocks: list[str] = []
    for i, exp in enumerate(candidates):
        hyp = exp.hypothesis
        ids = ", ".join(hyp.modulating) if hyp.modulating else "—"
        patch_lines = [
            f"    - {p.op} {p.mutation_id}: {p.rationale or '(no rationale)'}" for p in exp.patches
        ]
        patch_block = "\n".join(patch_lines) if patch_lines else "    (no patches)"
        blocks.append(
            f"### Candidate {i}\n"
            f"core_idea: {hyp.core_idea}\n"
            f"targets: [{ids}]\n"
            f"diff_size: {_diff_size(exp)} chars across {len(exp.patches)} patch(es)\n"
            f"patches:\n{patch_block}"
        )
    return "\n\n".join(blocks)


def _render_screen_note(results: list[CandidateScreenResult]) -> str:
    """The critic's counts-only ``## Screen measurements`` advisory block.

    One line per RENDERED candidate (indexes match the slate block the
    critic sees), carrying pass COUNTS only — never an entry id, never a
    raw scalar — so the block stays inside the restricted-visibility
    envelope exactly like the calibration note. Advisory tiebreak
    material: the instruction explicitly subordinates it to the quality
    bar, because the panel measurement is selection-biased (a small,
    champion-passing tryout panel) and must not become a ranking.
    """
    lines: list[str] = []
    for i, res in enumerate(results):
        if res.entries_screened <= 0:
            lines.append(f"Candidate {i}: not screened (no signal).")
            continue
        status = "VETOED (confirmed regression)" if res.vetoed else "clear"
        lines.append(
            f"Candidate {i}: {status}; passed {res.candidate_passes}/"
            f"{res.entries_screened} tryout entries "
            f"({res.baseline_passes} champion-passing)."
        )
    return (
        "\n## Screen measurements\n"
        "Each candidate ran a small train-panel tryout (counts only; "
        "selection-biased). Use these ONLY to break ties the quality bar "
        "leaves — they are not a ranking.\n" + "\n".join(lines) + "\n"
    )


_CRITIC_SYSTEM_PROMPT = (
    "You are a strict reviewer selecting the single best improvement proposal "
    "from a slate of candidates for a multi-agent system. You see ONLY the "
    "same restricted, board-anonymized context the proposer saw — never any "
    "held-out evaluation data. Judge each candidate against this quality bar:\n"
    "  1. Does it target a REAL, observed failure mode (a mutation point the "
    "observed patterns / failure-mode profile flag), not a speculative edit?\n"
    "  2. Is it the MINIMAL diff that achieves its goal (prefer a small, "
    "general edit over a large or special-cased one)?\n"
    "  3. Is its hypothesis grounded and falsifiable (a clear expected "
    "movement), not vague?\n"
    "Pick the candidate that best satisfies the bar. Reply with the integer "
    "index of that candidate ALONE on the FIRST line (e.g. `0`). On the "
    "SECOND line write ONE sentence, under 200 characters, naming the bar "
    "clause that decided it — the CLAUSE, never the candidate number. "
    "No JSON, no fences, nothing else."
)

#: Cap on the critic's recorded rationale, in characters. The rationale is
#: asked for as ONE sentence and lands in one ``round_log.jsonl`` line, so a
#: model that ignores the length instruction must not bloat the log. The
#: truncation is silent by design: the rationale is provenance, never a
#: parsed input, so a clipped tail costs a reader the end of a sentence and
#: costs the loop nothing. Set ABOVE the 200 characters the prompt
#: asks for, so an answer that obeys the instruction is never clipped — the
#: cap catches runaway text, it does not enforce the ask.
RATIONALE_CAP: int = 240


def normalize_selection_rationale(rationale: str) -> str:
    """Return the bounded, single-line form stored in round-log provenance.

    Both critic transports persist this untrusted model output. Keeping the
    normalization here makes their ``critique_selected`` records
    interchangeable and protects the canonical JSONL log from runaway text.
    """
    return " ".join(rationale.split())[:RATIONALE_CAP]


def _slate_scope(ctx: ProposerContext) -> dict[str, Any]:
    """The durable plan scope shared by every event from one challenger slate.

    A field round drives several challengers through the same round-log
    emitter while each slate's candidate indexes restart at zero, so the
    challenger id is the only thing that keeps two slates apart in the log.
    """
    return {"generation_id": ctx.new_generation_id}


def _screened_event_fields(
    index: int, res: CandidateScreenResult, revise: bool = False
) -> dict[str, Any]:
    """One ``candidate_screened`` event payload — the counts-only summary.

    Shared by the slate screen and the revise-replacement screen so both
    emit the identical shape; ``revise`` marks the replacement's event
    (its ``index`` is one past the original slate). NEVER carries an
    entry id — only the counts and the counts-only reason string.
    """
    return {
        "index": index,
        "vetoed": res.vetoed,
        "confirmed": res.confirmed,
        "screen_summary": {
            "entries_screened": res.entries_screened,
            "baseline_passes": res.baseline_passes,
            "candidate_passes": res.candidate_passes,
            "reason": res.reason,
        },
        "revise": revise,
    }


def _selected_event_fields(
    candidates: list[Experiment], index: int, mode: str, rationale: str = ""
) -> dict[str, Any]:
    """One ``critique_selected`` event payload — the selection's provenance.

    ``index`` alone does not say WHAT was chosen: a reader holding the round
    log has to re-open the captured critique prompt to learn what candidate 1
    was. So the payload carries a per-candidate summary of the whole slate —
    the proposer's OWN declared core idea and the mutation ids it targets —
    and, when a critic chose, that critic's one-line reason.

    Both extra fields are PROVENANCE: nothing in the loop reads them back,
    and an empty one (a heuristic mode, a critic that wrote no sentence) is a
    thinner record, never a broken one. The shape matches what the ``pi``
    transport already writes, so the two selection routes log identically.

    Redaction: every string here is PROPOSER-authored (``core_idea``) or
    CRITIC-authored under the same restricted envelope, plus mutation ids
    from the manifest the proposer was offered. No entry id, no task text,
    and no holdout value can reach this payload — none of the three writers
    ever saw one.
    """
    return {
        "index": index,
        "reason": mode,
        # A TUPLE, matching the declared field type — the round-log decoder
        # re-tuples top-level lists on read, so emitting a list would make
        # the written event unequal to its own decoded form
        # (``_slot_error_texts`` keeps the same contract for
        # ``ProposalAttempted.errors``).
        "slate": tuple(
            {
                "index": i,
                # BOUNDED like the rationale beside it: ``core_idea`` is
                # unbounded model text (no ``maxLength`` in the proposer
                # schema), and capping only one of the payload's two text
                # fields would leave the round-log line unbounded anyway.
                "core_idea": normalize_selection_rationale(item.hypothesis.core_idea),
                "mutation_ids": list(item.hypothesis.modulating),
            }
            for i, item in enumerate(candidates)
        ),
        "rationale": rationale,
    }


def _render_revise_feedback(results: Sequence[CandidateScreenResult]) -> str:
    """The revise re-sample's repair-feedback string. COUNTS ONLY by contract.

    Composed exclusively from the per-candidate
    :attr:`CandidateScreenResult.reason` strings — themselves counts-only
    by that field's contract (never an entry id, never a question/output
    token) — plus static instruction text, so the string can flow into
    the (restricted-visibility) proposer prompt without widening what the
    proposer may learn about the board (OVERFITTING.md §11).
    """
    per_candidate = "; ".join(f"candidate {i}: {res.reason}" for i, res in enumerate(results))
    return (
        "the pre-tournament candidate screen VETOED every sampled candidate "
        f"({per_candidate}). Each candidate ran a small tryout panel; a veto "
        "means a confirmed regression on behaviour the current champion gets "
        "right, or a wall-clock budget exhaustion. Propose ONE different "
        "experiment that avoids these failure modes: prefer a smaller, more "
        "conservative edit that cannot regress currently-passing behaviour "
        "and stays well inside the per-run budget."
    )


def _emit_round_event(
    ctx: ProposerContext,
    type_token: str,
    fields: dict[str, Any] | Callable[[], dict[str, Any]],
    scope: Mapping[str, Any] | None = None,
) -> None:
    """Best-effort round-log emission through the context's optional emitter.

    The emitter seam keeps the proposer decoupled from the round-log module:
    the orchestrator threads an ``emitter(type_token, fields, scope)``
    callable on :attr:`ProposerContext.round_event_emitter`; ``None`` (every
    caller that does not opt in) emits nothing. Guarded here so a raising
    emitter can never fail a propose step.

    ``scope`` is the event's PLAN coordinates and travels separately from the
    payload, so it can never be mistaken for one of the typed event's own
    fields. ``fields`` may be a THUNK for a payload that costs something to
    build. It is called only after the ``None`` check and inside the guard, so
    an unwired emitter pays nothing and a raising builder cannot fail a
    propose any more than a raising emitter can.
    """
    emitter = ctx.round_event_emitter
    if emitter is None:
        return
    try:
        emitter(type_token, fields() if callable(fields) else fields, scope)
    except Exception as exc:  # noqa: BLE001 — emission must never fail a propose
        log.debug("round-log %s emission skipped: %s", type_token, exc)


def _slot_error_texts(outcome: _SlotOutcome) -> tuple[str, ...]:
    """Every error string ONE slate slot produced, in the order it produced it.

    The slot's own failure trail is its :class:`ProposerError`'s ``attempts``
    — the per-attempt strings VERBATIM, never the joined ``str(exc)`` — so the
    call-boundary templates the errors were raised with survive into the round
    log intact. That fidelity is load-bearing downstream:
    :mod:`zicato.epoch.round_integrity` anchors its infra-marker scan to those
    exact prefixes, and an error re-wrapped in the "proposer failed after N
    attempt(s)" envelope would fail to match one. An ``attempts``-less error
    (nothing raises one today) falls back to its own text so the slot is never
    silently error-free.

    ``degraded_errors`` comes FIRST because the degrade happened before the
    replacement sample that may follow it in the same slot.
    """
    texts: list[str] = list(outcome.degraded_errors)
    error = outcome.error
    if error is not None:
        attempts = [str(attempt) for attempt in getattr(error, "attempts", ())]
        texts.extend(attempts or [str(error)])
    return tuple(texts)


def _parse_critic_choice(response: str, n: int) -> tuple[int | None, str]:
    """Parse the critic's chosen index and rationale out of its raw response.

    Returns ``(index, rationale)``. The index is the chosen candidate when it
    is in ``range(n)``, else ``None`` (the caller falls back to the
    heuristic). The rationale is the critic's own one-line justification,
    whitespace-collapsed and capped at :data:`RATIONALE_CAP` — ``""`` when the
    critic wrote none — every response that is a bare integer.

    Tolerant on BOTH halves, because a flaky critic must never fail a propose:

    * The index is scanned as the first integer token of the first line of
      the STRIPPED response (leading blank lines are routine), and the whole
      response is re-scanned when that line holds no digits (a model that
      opens with a fence or a lead-in still parses).
    * The rationale is whatever follows, or ``""``. A missing rationale is
      never an error — it costs the round log a sentence, never the step.

    A rejected index yields ``(None, "")``: the rationale explains a CHOICE,
    so it is meaningless once the choice is discarded.
    """
    import re  # noqa: PLC0415

    if not response:
        return None, ""
    # STRIP first: a leading blank line is routine model output, and
    # partitioning the raw text would hand the scan an empty first line and
    # discard the rationale behind the fallback below.
    head, _, tail = response.strip().partition("\n")
    match = re.search(r"-?\d+", head)
    if match is None:
        # The first line carried no digits — fall back to scanning the whole
        # response, and keep NO rationale (the split that would have separated
        # them is the thing that did not hold).
        match = re.search(r"-?\d+", response)
        tail = ""
    if match is None:
        return None, ""
    try:
        choice = int(match.group(0))
    except ValueError:
        return None, ""
    if not 0 <= choice < n:
        return None, ""
    return choice, normalize_selection_rationale(tail)


@dataclass
class BestOfNProposerAgent:
    """Wrap a :class:`ProposerAgent` with best-of-N sampling + self-critique.

    Construct it around the epoch's resolved inner agent and the contract's
    :class:`~zicato.core.types.ProposerQualityConfig`. With an explicit
    ``best_of_n == 1`` pin :meth:`propose` is a transparent pass-through to
    the inner agent — no extra sampling, no critique call (the DEFAULT config
    samples a slate of 3).

    The wrapper preserves the inner agent's failure contract: when no
    candidate can be sampled it re-raises the inner
    :class:`~zicato.proposer.proposer.ProposerError`, exactly as a single
    ``propose`` would, so every call site that already handles a failed
    propose is unaffected.
    """

    inner: ProposerAgent
    config: ProposerQualityConfig
    #: Optional generic-wrapper generation and review routes.
    breadth_call_llm: CallLLM | None = None
    depth_call_llm: CallLLM | None = None
    breadth_model: str | None = None
    depth_model: str | None = None
    #: Slate-gather concurrency cap — the ``asyncio.Semaphore`` size for the
    #: best-of-N sampling fan-out (threaded from
    #: :attr:`~zicato.core.runtime.RuntimeConfig.propose_parallelism`, whose
    #: own default is ``4``). ``1`` — this field's default, so a construction
    #: that passes nothing gets it — runs the slate serially;
    #: the deterministic post-gather pass makes any value produce the SAME
    #: slate + event stream regardless of slot completion order. Effectively capped at ``best_of_n``
    #: (never more tasks than slots).
    propose_parallelism: int = 1

    def _breadth_call_llm(self, ctx: ProposerContext) -> CallLLM:
        """Resolve the generic wrapper's generation callable."""
        return self.breadth_call_llm if self.breadth_call_llm is not None else ctx.aux_call_llm

    def _depth_call_llm(self, ctx: ProposerContext) -> CallLLM:
        """Resolve the generic wrapper's review callable."""
        return self.depth_call_llm if self.depth_call_llm is not None else ctx.aux_call_llm

    def _breadth_model(self, ctx: ProposerContext) -> str:
        """Resolve the generic wrapper's generation model."""
        return self.breadth_model if self.breadth_model is not None else ctx.model

    def _depth_model(self, ctx: ProposerContext) -> str:
        """Resolve the generic wrapper's review model."""
        return self.depth_model if self.depth_model is not None else ctx.model

    async def propose(self, ctx: ProposerContext) -> Experiment:
        n = self.config.best_of_n
        if n <= 1:
            # One inner sample, no critique.
            return await self.inner.propose(ctx)

        # The N slate samples are independent (each varies only by a
        # deterministic per-slot hint + its OWN scratch validation tree), so
        # fan them out under the propose-parallelism cap and collect one
        # ``_SlotOutcome`` per slot. ``asyncio.gather`` preserves INPUT order
        # in its result list regardless of completion order, so the ordered
        # pass below is deterministic; ``propose_parallelism == 1`` runs the
        # slots serially. Every slot leases its OWN scratch derivation tree (see
        # :meth:`_run_one_slot`), so two concurrent slots never race on the
        # shared ``next_id`` tree — that shared derive happens exactly once,
        # for the chosen candidate, in :meth:`_mount_chosen` after selection.
        outcomes = await self._gather_slate(ctx, n)

        # Deterministic post-gather pass — SLOT order. Build the round-log
        # trail and the candidate list in one walk: every slot that produced
        # ERROR EVIDENCE contributes a ``proposal_attempted``, every slot that
        # produced a CANDIDATE contributes a ``candidate_sampled``, and a slot
        # that did both (a degraded merge whose fresh sample then landed)
        # contributes both, in that order.
        #
        # A failed slot narrows the slate, and its error is reported rather
        # than discarded because a sibling survived (issue #141). Discarding it
        # would let a credential-lapsed round reach the integrity reader as
        # ``candidates_sampled=1, errors=()``: zero model responses and no
        # recorded evidence of the outage.
        #
        # The events are STAGED rather than emitted inline because the
        # all-failed path must not double-report: it raises a
        # :class:`ProposerError` aggregating every slot's attempts, and
        # ``evolve/propose_apply.py`` already emits one ``proposal_attempted``
        # per attempt of an escaping error. Staging lets this pass emit only
        # when the slate survived, in the order the walk visits the slots.
        candidates: list[Experiment] = []
        staged: list[tuple[str, dict[str, Any]]] = []
        slot_attempts: list[str] = []
        recombined_index: int | None = None
        for outcome in outcomes:
            errors = _slot_error_texts(outcome)
            if errors:
                staged.append(
                    ("proposal_attempted", {"errors": errors, "slot_index": outcome.sample})
                )
                slot_attempts.extend(f"slot {outcome.sample}: {text}" for text in errors)
            if outcome.candidate is None:
                continue
            candidates.append(outcome.candidate)
            fields: dict[str, Any] = {"i": outcome.sample, "n": n}
            if outcome.recombined:
                recombined_index = len(candidates) - 1
                fields["recombined"] = True
            staged.append(("candidate_sampled", fields))

        if not candidates:
            # The whole slate failed — surface the inner failure exactly as a
            # single propose would (the caller's rejected-outcome path handles
            # it), but carrying EVERY slot's attempts rather than only the last
            # slot's. Re-raising the last error alone would drop slots
            # 0..n-2, so a slate whose earlier slots hit a credential lapse
            # and whose final slot hit a parse error would report only the
            # parse error, losing the infra evidence from the one channel
            # that outlives the run. Each attempt is prefixed with its slot so the
            # aggregate stays readable; the integrity reader strips that prefix
            # before anchoring its marker scan (``epoch/round_integrity.py``).
            # ``slot_attempts`` is non-empty because n >= 2 means the loop ran.
            if slot_attempts:
                raise ProposerError(slot_attempts)
            raise ProposerError(["best-of-N produced no candidates"])  # pragma: no cover

        for type_token, event_fields in staged:
            _emit_round_event(ctx, type_token, event_fields, _slate_scope(ctx))

        if len(candidates) == 1:
            # Even a sole survivor is mounted into the real ``next_id`` — its
            # scratch validation tree was already cleaned up, so the shared
            # tree must be derived from its patches before the caller mounts.
            await self._mount_chosen(candidates, 0, ctx)
            # A collapsed slate is still a SELECTION, and the invariant is that
            # every round which minted a generation records what was chosen out
            # of what. Nothing CHOSE here — ``n >= 2`` slots were sampled and
            # all but one failed — so the mode names that degenerate basis and
            # the slate summary carries the one survivor. Same builder as the
            # two deciding paths below, so a reader folding the log sees one
            # event shape whether or not a critic ran.
            _emit_round_event(
                ctx,
                "critique_selected",
                lambda: _selected_event_fields(candidates, 0, "sole_candidate"),
                _slate_scope(ctx),
            )
            return candidates[0]

        # Optional pre-tournament candidate screen (tryouts) — VETO-FIRST:
        # a catastrophic regression is disqualified here, but the screen
        # never ranks; the critic/heuristic below still chooses among the
        # survivors. ``None`` (unscreened — no runner threaded, screen
        # error, or malformed result) leaves the selection to the quality bar.
        screen_results = await self._screen_slate(candidates, ctx)

        if recombined_index is not None and (
            screen_results is None or not screen_results[recombined_index].vetoed
        ):
            # SELECTION SHORT-CIRCUIT: a NON-VETOED mint is chosen
            # outright — no critic call (the sole-survivor precedent). The
            # heuristic's minimal-diff key would systematically STARVE the
            # union (its diff is larger than either parent's BY
            # CONSTRUCTION — the parsimony bias the slot exists to
            # overcome; the starved-heuristic OC test documents the failing
            # alternative). The mint is grounded in MEASURED per-entry
            # evidence from two real tournament rounds, the screen above
            # could still veto it, and the unchanged gate remains the
            # arbiter. A VETOED mint takes the else-branch as an ordinary
            # slate member.
            chosen, selection_mode = recombined_index, "recombined"
            await self._mount_chosen(candidates, chosen, ctx)
            _emit_round_event(
                ctx,
                "critique_selected",
                lambda: _selected_event_fields(candidates, chosen, selection_mode),
                _slate_scope(ctx),
            )
            return candidates[chosen]

        survivor_indices = list(range(len(candidates)))
        all_vetoed = False
        revise_chosen = False
        vetoed_mode_prefix = "screen_all_vetoed"
        if screen_results is not None:
            survivors = [i for i, res in enumerate(screen_results) if not res.vetoed]
            if survivors:
                survivor_indices = survivors
            else:
                # Every candidate vetoed — take the ONE bounded
                # screen-informed revise pass first; only when it
                # too produces nothing usable does the step degrade to
                # critic-over-ALL, with the mode string recording the
                # degraded selection basis. The screen may narrow but
                # never empty the step either way.
                all_vetoed = True
                revise_outcome = await self._revise_all_vetoed(candidates, screen_results, ctx, n)
                if revise_outcome == "chosen":
                    revise_chosen = True
                elif revise_outcome == "fallback":
                    vetoed_mode_prefix = "screen_all_vetoed_after_revise"

        if revise_chosen:
            # The revise replacement survived its own screen (or could not
            # be screened — the guarded degrade): it is the choice, no
            # critique call needed.
            chosen, selection_mode, rationale = (
                len(candidates) - 1,
                "screen_revise_survivor",
                "",
            )
        elif screen_results is not None and not all_vetoed and len(survivor_indices) == 1:
            # A single survivor needs no critique call — the veto already
            # decided the slate.
            chosen, selection_mode, rationale = survivor_indices[0], "screen_sole_survivor", ""
        else:
            chosen, selection_mode, rationale = await self._select_over(
                candidates, survivor_indices, screen_results, ctx
            )
            if all_vetoed:
                selection_mode = f"{vetoed_mode_prefix}:{selection_mode}"
        # Unconditional final derive: mount the CHOSEN candidate into the real
        # ``next_id`` tree (its slate scratch tree is gone). This is the
        # ⛔-funnel guaranteeing the mounted tree == the chosen candidate.
        await self._mount_chosen(candidates, chosen, ctx)
        _emit_round_event(
            ctx,
            "critique_selected",
            lambda: _selected_event_fields(candidates, chosen, selection_mode, rationale),
            _slate_scope(ctx),
        )
        return candidates[chosen]

    async def _gather_slate(self, ctx: ProposerContext, n: int) -> list[_SlotOutcome]:
        """Fan the N slate slots out under the propose-parallelism cap.

        Returns one :class:`_SlotOutcome` per slot IN SLOT ORDER
        (``asyncio.gather`` preserves input order regardless of which slot
        finishes first), so the caller's ordered pass is deterministic.
        ``propose_parallelism == 1`` runs the slots strictly serially, in
        slot order, and skips the task/semaphore machinery so the no-factory
        unit-test path stays a plain loop.

        Scratch-lease safety: ``_run_one_slot``'s own
        ``try/finally`` always releases ITS slot's scratch lease, but plain
        ``asyncio.gather()`` (``return_exceptions=False``) propagates the
        FIRST exception the instant any one slot raises, WITHOUT cancelling
        or awaiting the remaining slots — they keep running as orphaned
        background tasks, so a sibling's scratch parent can still be on disk
        at the exact moment this call returns control to the caller. Passing
        ``return_exceptions=True`` makes ``gather`` wait for every slot to
        actually finish (success or exception) — and therefore for every
        slot's ``finally: cleanup()`` to have already run — before this
        method ever returns or raises, so the caller can never observe a
        still-open lease. Findings are re-raised in SLOT order (not
        completion order) for the same determinism the rest of the gather
        provides; a slot's own :class:`~zicato.proposer.proposer.ProposerError`
        never reaches here (:meth:`_sample_slot` already folds it into a
        normal ``_SlotOutcome``), so only an unexpected exception takes this
        path.
        """
        parallelism = max(1, min(self.propose_parallelism, n))
        if parallelism == 1:
            return [await self._run_one_slot(ctx, sample, n) for sample in range(n)]
        sem = asyncio.Semaphore(parallelism)

        async def _guarded(sample: int) -> _SlotOutcome:
            async with sem:
                return await self._run_one_slot(ctx, sample, n)

        results = await asyncio.gather(
            *(_guarded(sample) for sample in range(n)), return_exceptions=True
        )
        outcomes: list[_SlotOutcome] = []
        for result in results:
            if isinstance(result, BaseException):
                raise result
            outcomes.append(result)
        return outcomes

    async def _run_one_slot(self, ctx: ProposerContext, sample: int, n: int) -> _SlotOutcome:
        """Run ONE slate slot into its OWN scratch validation tree.

        Leases a per-slot ``(validate, cleanup)`` from
        ``ctx.scratch_validator_factory`` (or the shared ``validate_experiment``
        hook + a no-op cleanup when no factory is threaded — the serial
        unit-test path), threads the scratch validator onto the slot context,
        runs the slot body (the recombination mint/merge for the last slot, an
        ordinary sample otherwise, degrading a failed mint/merge to an ordinary
        sample VERBATIM), and ALWAYS releases the scratch tree in ``finally`` —
        including on propose failure or a recombination degrade. Emits NO
        events: the deterministic post-gather pass owns emission in slot order.

        A merge-call exception the degrade swallowed rides out on the returned
        outcome's ``degraded_errors`` so that pass can report it (issue #141);
        the degrade decision itself is unchanged.
        """
        from zicato.telemetry.meta_loop import SPAN_SLOT, meta_span  # noqa: PLC0415

        validate, cleanup = self._slot_validate_lease(ctx)
        slot_ctx = replace(ctx, validate_experiment=validate, slot_index=sample)
        degraded_errors: tuple[str, ...] = ()
        try:
            # Slate-slot span: the N slots gather concurrently, so these render
            # as overlapping lifelines under the propose phase (HARMONOGRAF.md §7).
            async with meta_span(f"slot {sample}", kind=SPAN_SLOT, meta={"sample": sample}):
                if sample == n - 1 and ctx.recombine_pair is not None:
                    # The recombination slot: the LAST slot composes the
                    # round's selected pair instead of sampling the LLM (the two
                    # merge modes; PROPOSER.md §2.6.1). It validates through the SAME
                    # per-slot scratch hook every sample uses. Any failure DEGRADES
                    # to the normal fresh sample below — the identical slot body,
                    # with the slot's normal exploratory hint (a recombination
                    # failure must never narrow the slate).
                    if self.config.recombine_merge == "llm":
                        minted, degraded_errors = await self._merge_recombined(slot_ctx)
                    else:
                        minted = await self._mint_recombined(slot_ctx)
                    if minted is not None:
                        return _SlotOutcome(
                            sample=sample, candidate=minted, error=None, recombined=True
                        )
                outcome = await self._sample_slot(slot_ctx, sample, n)
                # A merge call that was swallowed to a degrade rides out on the
                # slot's own outcome (issue #141) — the fresh sample below may
                # well succeed, so the round would otherwise close with no trace
                # that the merge endpoint was refusing.
                if degraded_errors:
                    outcome = replace(outcome, degraded_errors=degraded_errors)
                return outcome
        finally:
            cleanup()

    def _slot_validate_lease(
        self, ctx: ProposerContext
    ) -> tuple[Callable[[Experiment], Awaitable[list[str]]] | None, Callable[[], None]]:
        """Lease this slot's ``(validate, cleanup)`` scratch pair.

        With a ``scratch_validator_factory`` on the context (the real
        orchestrator paths) each call mints a FRESH, disjoint scratch tree so
        concurrent slots never collide. Without one (single-sample proposers,
        unit-test contexts with no genstore) the slot leases the SHARED
        ``validate_experiment`` hook + a no-op cleanup and the slate runs
        serially.
        """
        factory = ctx.scratch_validator_factory
        if factory is not None:
            return factory()
        return ctx.validate_experiment, _noop_cleanup

    async def _mount_chosen(
        self, candidates: list[Experiment], chosen: int, ctx: ProposerContext
    ) -> None:
        """Mount the chosen candidate into the real ``next_id`` tree (⛔-funnel).

        UNCONDITIONAL by construction: each slot validated into its OWN
        scratch tree, already cleaned up, so there is no candidate whose tree
        is mounted and no branch that may skip this derive. The chosen
        candidate is ALWAYS derived into the canonical ``next_id`` exactly
        once here, through the round's shared ``validate_experiment`` hook
        (:func:`zicato.evolve.round.build_post_apply_validator`). That single
        derive is what guarantees the mounted ``next_id`` tree == the chosen
        candidate + populates the caller's ``last_child_snapshot``.

        The chosen candidate validated cleanly in scratch moments ago, so a
        finding here is unexpected (e.g. the parent tree changed underneath the
        slate). There is no shared tree to fall back to, so any finding
        surfaces the standard :class:`~zicato.proposer.proposer.ProposerError`
        every call site already handles. ``validate_experiment is None`` (a
        context with no derive hook) mounts nothing and returns.
        """
        validate = ctx.validate_experiment
        if validate is None:
            return
        findings = await self._revalidate(validate, candidates[chosen])
        if findings:
            raise ProposerError(
                [
                    f"mounting chosen candidate {chosen} into the generation tree failed: {f}"
                    for f in findings
                ]
            )

    async def _sample_slot(self, slot_ctx: ProposerContext, sample: int, n: int) -> _SlotOutcome:
        """One ordinary slate slot — the sample body, returning a ``_SlotOutcome``.

        Intra-slate diversity, on TWO axes: each slot carries a DISTINCT
        edit-class hint (WHICH failure to target) composed WITH a distinct
        STRATEGY framing (HOW to approach the fix), so the N samples explore
        different edit strategies AND different strategic framings rather
        than re-rolling one idea. The edit-class mapping
        (:func:`zicato.proposer.hints.hint_for_slot`) conditions slots
        0..N-2 on the profile's DOMINANT failure mode and keeps the LAST
        slot exploratory; with no profile signal it is the plain
        :data:`EDIT_CLASS_HINTS` rotation. The strategy framing
        (:func:`zicato.proposer.hints.strategy_for_slot`) is a small fixed
        vocabulary rotated deterministically per (slot, round) — no RNG, no
        extra sampling params (the ``aux_call_llm`` seam is
        ``(system, user, model) -> str`` and accepts no temperature, so the
        variation rides the PROMPT only). Both are static instruction strings
        — no board identity — so the restricted-visibility envelope is
        untouched.

        ``slot_ctx`` already carries this slot's per-slot SCRATCH validation
        hook (leased by :meth:`_run_one_slot`), so the sample validates into
        its OWN tree. Returns a :class:`_SlotOutcome` carrying the sampled
        candidate on success, or the :class:`ProposerError` when the inner
        proposer could not produce one (the slate simply narrows; the ordered
        pass reports the error either way, and an all-failed slate re-raises
        every slot's). Emits NO event — the deterministic post-gather pass owns
        emission in slot order. The recombination slot's degrade path reuses
        this body VERBATIM.
        """
        # Slate SAMPLING runs on the breadth role. The swap is a no-op (same
        # object) when no breadth role is configured; when configured, the
        # inner proposer's ``ctx.aux_call_llm`` consumers reach the breadth
        # endpoint. ``ctx.model`` is swapped to the breadth model name too, so
        # the DEFAULT ADK proposer — which binds to the model STRING rather
        # than the callable — honors the role; absent a breadth model the string is
        # replaced with its OWN value. The recombination-mint
        # DEGRADE path routes here too, so a degraded slot samples on breadth
        # exactly like an ordinary one.
        # Compose the two diversity axes into the single sample-hint string:
        # the edit-class hint (WHICH failure), then the per-(slot, round)
        # strategy framing (HOW to fix it). Both static, board-identity-free.
        edit_hint = hint_for_slot(sample, n, slot_ctx.failure_profile)
        strategy = strategy_for_slot(sample, slot_ctx.new_generation_id)
        sample_ctx = replace(
            slot_ctx,
            sample_hint=f"{edit_hint}\n{strategy}",
            aux_call_llm=self._breadth_call_llm(slot_ctx),
            model=self._breadth_model(slot_ctx),
        )
        try:
            candidate = await self.inner.propose(sample_ctx)
        except ProposerError as exc:
            return _SlotOutcome(sample=sample, candidate=None, error=exc, recombined=False)
        return _SlotOutcome(sample=sample, candidate=candidate, error=None, recombined=False)

    async def _mint_recombined(self, ctx: ProposerContext) -> Experiment | None:
        """Mint the round's recombination pair into the slate. GUARDED.

        Pure mint (:func:`zicato.proposer.recombine.mint_recombined_experiment`
        — no LLM call, no IO), then two defenses before the mint may enter
        the slate:

        * :func:`~zicato.proposer.brief.enforce_forbidden` — defense in
          depth: both parents cleared the brief when they were proposed,
          but the brief's forbidden set may have changed since.
        * the SAME post-apply validate hook every sampled candidate runs
          (``ctx.validate_experiment`` — this slot's per-slot SCRATCH hook) —
          it derives the mint's patches into this slot's own scratch tree, so
          the mint is validated in isolation exactly like an ordinary sample.

        Any finding → DEBUG log → ``None``: the caller DEGRADES to the
        normal fresh sample for the slot (recombination must never narrow
        a slate, let alone fail a propose). A successful mint emits its
        ``candidate_sampled`` event with the ``recombined`` marker.
        """
        from zicato.proposer.brief import enforce_forbidden  # noqa: PLC0415
        from zicato.proposer.recombine import mint_recombined_experiment  # noqa: PLC0415
        from zicato.util.iso_time import now_iso  # noqa: PLC0415

        pair = ctx.recombine_pair
        assert pair is not None  # caller-checked
        minted = mint_recombined_experiment(
            pair,
            epoch_id=ctx.epoch_id,
            parent_generation_id=ctx.parent_generation_id,
            new_generation_id=ctx.new_generation_id,
            proposed_at=now_iso(),
        )
        findings = enforce_forbidden(list(minted.patches), ctx.forbidden_ids)
        if not findings:
            validate = ctx.validate_experiment
            if validate is not None:
                findings = await self._revalidate(validate, minted)
        if findings:
            log.debug(
                "recombination mint (%s + %s) degraded to a fresh sample: %s",
                pair.a_generation_id,
                pair.b_generation_id,
                "; ".join(findings),
            )
            return None
        return minted

    async def _merge_recombined(
        self, ctx: ProposerContext
    ) -> tuple[Experiment | None, tuple[str, ...]]:
        """LLM-guided merge of the round's recombination pair. GUARDED.

        The ``recombine_merge = "llm"`` counterpart to :meth:`_mint_recombined`
        (PROPOSER.md §2.6.1): instead of mechanically concatenating a disjoint
        patch union, it issues ONE evaluation merge call — the DEPTH refinement
        role (:meth:`_depth_call_llm`, exactly as the self-critique call), so
        the merge SUBSTITUTES the slot's own sample call (cost: n calls, a
        recombine-off round). The merge prompt
        (:func:`~zicato.proposer.prompts.render_recombine_merge_prompt`) is
        rendered from the envelope-clean :class:`RecombinationPair` — both
        parents' patches, core ideas, BANDED outcomes and counts-only
        complementarity, never an entry id — and the response flows through
        the NORMAL proposal parse
        (:func:`~zicato.proposer.structured.parse_experiment_json`), so a merge
        is a proposal like any other. The parsed experiment is stamped with the
        pair's ``recombined_from`` provenance and then runs the SAME two
        defenses the mechanical mint runs (``enforce_forbidden`` + the validate
        hook, which derives the merge's patches into this slot's own scratch
        tree so it is validated in isolation exactly like an ordinary sample).

        Any failure — an opaque LLM error, an unparseable / schema-invalid
        response, a forbidden-id or validation finding → DEBUG log → ``None``:
        the caller DEGRADES to the normal fresh sample for the slot (the exact
        mechanical-mint degrade; a merge failure must never narrow the slate,
        let alone fail a propose). A successful merge emits its
        ``candidate_sampled`` event with the ``recombined`` marker.

        Returns ``(merged_or_None, degrade_evidence)``. The second element is
        the round-log channel for the swallowed CALL exception, rendered as a
        call-boundary error string for the slot's ``proposal_attempted``
        (issue #141). It is EVIDENCE ONLY: it steers no degrade decision above,
        and a caller that ignores it still degrades identically. Empty on
        success and on every post-response degrade.
        """
        import dataclasses  # noqa: PLC0415

        from zicato.proposer.brief import enforce_forbidden  # noqa: PLC0415
        from zicato.proposer.prompts import render_recombine_merge_prompt  # noqa: PLC0415
        from zicato.proposer.structured import (  # noqa: PLC0415
            ExperimentParseError,
            parse_experiment_json,
        )

        pair = ctx.recombine_pair
        assert pair is not None  # caller-checked
        system_prompt, user_prompt = render_recombine_merge_prompt(
            pair,
            brief_text=ctx.brief_text,
            mutations=ctx.mutations,
            custom_judge_names=ctx.custom_judge_names or frozenset(),
            metric_priorities=ctx.metric_priorities,
        )
        aux_call_llm = self._depth_call_llm(ctx)
        capture_proposer_input(
            workspace_root=ctx.workspace_root,
            epoch_id=ctx.epoch_id,
            role=ROLE_RECOMBINE_MERGE,
            system=system_prompt,
            user=user_prompt,
            model=ctx.model,
            parent_generation_id=ctx.parent_generation_id,
            new_generation_id=ctx.new_generation_id,
            slot=ctx.slot_index,
        )
        try:
            response = await aux_call_llm(system_prompt, user_prompt, ctx.model)
        except Exception as exc:  # noqa: BLE001 — opaque LLM errors are common
            log.debug(
                "recombination merge (%s + %s) call failed (%s); degrading to a fresh sample",
                pair.a_generation_id,
                pair.b_generation_id,
                exc,
            )
            # The swallowed CALL exception is the one degrade the round log
            # must not lose (issue #141). It is rendered with the SAME
            # call-boundary template the retry loop uses for an evaluation call
            # (``proposer/proposer.py``) so the integrity reader's marker scan,
            # which anchors on that prefix, sees a merge-call outage exactly as
            # it sees a sample-call outage. The suffix is zicato-authored and
            # stays BEHIND the prefix so the anchor is untouched. The parse /
            # forbidden / validation degrades below are content rejections
            # about a response that DID come back — they carry no infra
            # evidence, so they stay a debug line.
            return None, (
                f"evaluation LLM call raised {type(exc).__name__}: {exc}"
                " (recombination merge; degraded to a fresh sample)",
            )
        try:
            merged = parse_experiment_json(
                response or "",
                epoch_id=ctx.epoch_id,
                parent_gen=ctx.parent_generation_id,
                new_gen=ctx.new_generation_id,
                mutations_by_id={mp.id: mp for mp in ctx.mutations},
                custom_judge_names=ctx.custom_judge_names,
            )
        except ExperimentParseError as exc:
            log.debug(
                "recombination merge (%s + %s) response unparseable (%s); "
                "degrading to a fresh sample",
                pair.a_generation_id,
                pair.b_generation_id,
                exc,
            )
            return None, ()
        # Stamp the same provenance the mechanical mint carries so the merge is
        # indistinguishable downstream — the gate/journal consumers key on
        # ``recombined_from`` + ``selection_mode="recombined"``.
        merged = dataclasses.replace(
            merged, recombined_from=(pair.a_generation_id, pair.b_generation_id)
        )
        findings = enforce_forbidden(list(merged.patches), ctx.forbidden_ids)
        if not findings:
            validate = ctx.validate_experiment
            if validate is not None:
                findings = await self._revalidate(validate, merged)
        if findings:
            log.debug(
                "recombination merge (%s + %s) degraded to a fresh sample: %s",
                pair.a_generation_id,
                pair.b_generation_id,
                "; ".join(findings),
            )
            return None, ()
        return merged, ()

    async def _screen_slate(
        self, candidates: list[Experiment], ctx: ProposerContext
    ) -> list[CandidateScreenResult] | None:
        """Run the optional candidate screen over the settled slate. GUARDED.

        ``None`` — no screen runner on the context (every contract that
        does not opt in), a raising runner, or a malformed result — means
        UNSCREENED: the caller selects over the whole slate. Screening must
        never fail a propose step. Emits one ``candidate_screened`` round
        event per candidate (after the ``candidate_sampled`` events,
        before ``critique_selected``) with the counts-only summary.
        """
        screen = ctx.screen_candidates
        if screen is None:
            return None
        try:
            results = list(await screen(candidates))
        except Exception as exc:  # noqa: BLE001 — screening must never fail a propose
            log.debug("candidate screen failed (%s); selecting unscreened", exc)
            return None
        if len(results) != len(candidates):
            log.debug(
                "candidate screen returned %d result(s) for %d candidate(s); "
                "selecting unscreened",
                len(results),
                len(candidates),
            )
            return None
        for i, res in enumerate(results):
            _emit_round_event(
                ctx, "candidate_screened", _screened_event_fields(i, res), _slate_scope(ctx)
            )
        return results

    async def _revise_all_vetoed(
        self,
        candidates: list[Experiment],
        screen_results: list[CandidateScreenResult],
        ctx: ProposerContext,
        n: int,
    ) -> str:
        """The ONE bounded screen-informed revise pass. GUARDED.

        Called only for an ALL-VETOED screened slate — the one screen
        verdict under which proceeding is *knowingly* wasteful (the step
        would send a vetoed candidate to a full tournament round). That
        is the WHOLE trigger: a cold-start slate whose
        survivors were merely crash-only screened (no champion-passing
        baseline, so no pass-flip was ever detectable —
        :class:`~zicato.epoch.screen.ScreenPanel.baseline_pass_ids`
        empty) does NOT revise, because a replacement would face the same
        crash-only panel and could earn no stronger signal than the
        survivors already hold; and a no-signal survivor (screen error)
        is the screen's own degrade-to-unscreened contract rather than
        evidence against the slate.

        One replacement is re-sampled with the slate's COUNTS-ONLY veto
        summary seeded through the repair-feedback machinery
        (:attr:`ProposerContext.revise_feedback` — the same ``feedback``
        slot a validation failure threads on retry; never an entry id,
        so the restricted-visibility envelope is untouched), then
        screened GUARDED. Exactly one revise per propose — this method
        never re-enters :meth:`propose` and never loops.

        MUTATES ``candidates``: a successfully-proposed replacement is
        APPENDED whatever its own screen verdict, so the selection can pick
        it. It validates into its OWN per-slot SCRATCH tree (leased exactly
        like a slate slot), so a failed revise cannot clobber any other
        candidate's tree — there is no shared on-disk tree to restore, since
        the chosen candidate is derived into the real ``next_id`` once, after
        selection, in :meth:`_mount_chosen`.

        Runs sequentially AFTER the gather (an all-vetoed slate is the only
        trigger), so its single re-sample does not participate in the
        propose-parallelism fan-out.

        Returns one of:

        * ``"chosen"`` — the replacement survived (or could not be
          screened — the screen-failure degrade): the caller selects it.
        * ``"fallback"`` — the replacement was itself vetoed: the caller
          degrades to critic-over-ALL over the ORIGINAL slate, with the
          ``screen_all_vetoed_after_revise`` mode prefix recording that
          the revise was spent.
        * ``"unavailable"`` — the inner proposer produced no replacement:
          the caller degrades to critic-over-ALL (``screen_all_vetoed``).
          No tree restore is needed — the
          replacement validated into its own throwaway scratch tree.
        """
        revise_index = len(candidates)
        feedback = _render_revise_feedback(screen_results)
        # Lease a per-slot scratch validator so the revise re-sample validates
        # into its OWN tree (never the shared ``next_id``, never another
        # candidate's tree). Released in ``finally`` — the screen derives its
        # own tempdir, and the chosen candidate is mounted from patches later.
        validate, cleanup = self._slot_validate_lease(ctx)
        try:
            # The screen-informed REVISE is a DEPTH pass (a targeted repair
            # rather than exploration) — it runs on the depth role, falling
            # back to ``ctx.aux_call_llm`` when unconfigured.
            # ``ctx.model`` is swapped to the depth model name for the same
            # reason as the sampling site: so the default ADK proposer (which
            # binds the model STRING) honors the role; absent it, the string is
            # replaced with its own value.
            replacement = await self.inner.propose(
                replace(
                    ctx,
                    revise_feedback=feedback,
                    validate_experiment=validate,
                    aux_call_llm=self._depth_call_llm(ctx),
                    model=self._depth_model(ctx),
                )
            )
        except Exception as exc:  # noqa: BLE001 — the revise must never fail a propose
            log.debug(
                "screen-informed revise produced no replacement (%s); "
                "degrading to critic-over-all",
                exc,
            )
            return "unavailable"
        finally:
            cleanup()
        _emit_round_event(
            ctx,
            "candidate_sampled",
            {"i": revise_index, "n": n, "revise": True},
            _slate_scope(ctx),
        )
        result = await self._screen_replacement(replacement, revise_index, ctx)
        candidates.append(replacement)
        if result is not None and result.vetoed:
            return "fallback"
        return "chosen"

    async def _screen_replacement(
        self, replacement: Experiment, index: int, ctx: ProposerContext
    ) -> CandidateScreenResult | None:
        """Screen the ONE revise replacement. GUARDED like :meth:`_screen_slate`.

        ``None`` — a raising runner or a malformed result — means
        UNSCREENED: the caller treats the replacement as chosen (the
        screen-failure discipline: degrade to unscreened, never fail or
        empty the propose; the feedback-informed replacement is strictly
        better-informed than the known-vetoed originals). A real result
        emits the replacement's ``candidate_screened`` event with the
        ``revise`` marker (``index`` is one past the original slate).
        """
        screen = ctx.screen_candidates
        if screen is None:  # pragma: no cover — only a screened slate revises
            return None
        try:
            results = list(await screen([replacement]))
        except Exception as exc:  # noqa: BLE001 — screening must never fail a propose
            log.debug("revise-replacement screen failed (%s); treating as unscreened", exc)
            return None
        if len(results) != 1:
            log.debug(
                "revise-replacement screen returned %d result(s) for 1 candidate; "
                "treating as unscreened",
                len(results),
            )
            return None
        res = results[0]
        _emit_round_event(
            ctx,
            "candidate_screened",
            _screened_event_fields(index, res, revise=True),
            _slate_scope(ctx),
        )
        return res

    async def _select_over(
        self,
        candidates: list[Experiment],
        survivor_indices: list[int],
        screen_results: list[CandidateScreenResult] | None,
        ctx: ProposerContext,
    ) -> tuple[int, str, str]:
        """Select over the surviving sub-slate; map the index back to the slate.

        Returns ``(slate index, selection mode, rationale)`` — the rationale
        is the critic's own words, ``""`` for every non-critique mode.

        The screen's measurements feed the selection only as a LATE
        tiebreak — and only when the contract has not pinned
        ``screen_veto_only`` — through two advisory channels: the critique
        prompt's counts-only ``## Screen measurements`` block, and the
        heuristic's penultimate panel-scalar key. Unscreened (or
        veto-only), both channels are inert and the selection runs on the
        quality bar alone.

        ⛔ The critic sees the SUB-slate, renumbered from 0 by
        :func:`_render_candidate_slate` — its "candidate 1" is
        ``survivor_indices[1]`` rather than slate slot 1. The returned index is
        mapped back here, but the RATIONALE is free text that cannot be
        mapped, so a sentence naming a candidate number would point at the
        wrong row of the event's own ``slate`` field. That is why
        :data:`_CRITIC_SYSTEM_PROMPT` asks for the bar CLAUSE and forbids the
        candidate number: the fix belongs at the source, because no amount of
        post-processing can recover which numbering a sentence meant.
        """
        sub = [candidates[i] for i in survivor_indices]
        feed = screen_results is not None and not self.config.screen_veto_only
        screen_scalars: list[float | None] | None = None
        screen_note = ""
        if feed:
            assert screen_results is not None  # narrowed by ``feed``
            sub_results = [screen_results[i] for i in survivor_indices]
            screen_scalars = [res.scalar for res in sub_results]
            screen_note = _render_screen_note(sub_results)
        sub_choice, selection_mode, rationale = await self._select_best(
            sub, ctx, screen_scalars=screen_scalars, screen_note=screen_note
        )
        return survivor_indices[sub_choice], selection_mode, rationale

    @staticmethod
    async def _revalidate(
        validate: Callable[[Experiment], Awaitable[list[str]]], candidate: Experiment
    ) -> list[str]:
        """Run the post-apply hook once; any raise is reported as a finding.

        The hook's own contract is to RETURN findings (it already folds a
        ``derive_generation`` rejection into one), so an exception here is
        doubly unexpected — fold it into the findings list so
        :meth:`_mount_chosen` (and the recombination-degrade defenses) handle
        both failure shapes identically.
        """
        try:
            return list(await validate(candidate))
        except Exception as exc:  # noqa: BLE001 — fold into the fallback path
            return [f"validation hook raised unexpectedly: {exc}"]

    async def _select_best(
        self,
        candidates: list[Experiment],
        ctx: ProposerContext,
        screen_scalars: list[float | None] | None = None,
        screen_note: str = "",
    ) -> tuple[int, str, str]:
        """Return ``(best index, mode, rationale)`` against the §4.1 quality bar.

        Runs the self-critique LLM pass when it is enabled and an evaluation
        callable is available; otherwise (or on any critique failure) falls
        back to the deterministic :func:`_heuristic_best_index`. Either way
        the selection sees ONLY the restricted proposer context. The mode
        string (``"critique"`` / ``"heuristic"``) is round-log provenance
        only — the caller's choice of candidate is the index.

        The rationale is the critic's own one-line justification, round-log
        provenance exactly like the mode. It is ``""`` on EVERY heuristic
        path: the heuristic's basis is its deterministic sort key, which the
        mode string already names, and inventing prose for it would put words
        in a comparator's mouth.

        ``screen_scalars`` / ``screen_note`` are the OPTIONAL candidate-
        screen tiebreak feeds (heuristic key / critic prompt block); both
        default inert, so an unscreened caller selects on the quality bar
        alone.
        """
        if not self.config.critique_enabled:
            return _heuristic_best_index(candidates, ctx, screen_scalars), "heuristic", ""

        # The self-CRITIQUE selection call is a DEPTH pass (it judges + ranks
        # the slate) — resolve the depth role, falling back to
        # ``ctx.aux_call_llm`` when no depth role is set.
        aux_call_llm = self._depth_call_llm(ctx)
        if aux_call_llm is None:  # pragma: no cover — orchestrator always wires it
            return _heuristic_best_index(candidates, ctx, screen_scalars), "heuristic", ""

        choice, rationale = await self._critique(aux_call_llm, candidates, ctx, screen_note)
        if choice is None:
            # Critique failed / unparseable — fall back to the heuristic so a
            # flaky critic never blocks the step.
            return _heuristic_best_index(candidates, ctx, screen_scalars), "heuristic", ""
        return choice, "critique", rationale

    async def _critique(
        self,
        aux_call_llm: Callable[[str, str, str], Awaitable[str]],
        candidates: list[Experiment],
        ctx: ProposerContext,
        screen_note: str = "",
    ) -> tuple[int | None, str]:
        """One cheap self-critique LLM call; returns ``(index, rationale)``.

        Builds the critic's user prompt from the SAME restricted context the
        proposer saw — the same evidence, rendered by the same function
        under the same ``restrict_visibility`` flag, so the patterns are
        aggregated and the experiment memory banded exactly as for the
        proposer — plus the compact candidate slate. The critic NEVER receives the holdout or any
        identity the proposer did not already see. Best-effort: a raising /
        timing-out / unparseable critic returns ``(None, "")`` and the caller
        falls back to the heuristic.

        The rationale rides the SAME envelope as the call that produced it:
        the critic can only paraphrase what it was shown, and it was shown
        nothing the proposer had not already seen, so no holdout value and no
        entry identity can reach it. It is the ``pi`` transport's
        ``select_candidate`` rationale by another route.
        """
        # The critic stays inside the SAME visibility envelope as the
        # proposer, and does so by construction: this is the very evidence
        # the proposal episode was given, projected off the same context
        # and rendered by the same function. Nothing can be added here
        # that the proposer had not already seen.
        from zicato.proposer.foe_agent import evidence_from_context  # noqa: PLC0415
        from zicato.proposer.foe_request import render_evidence  # noqa: PLC0415

        restricted_context = render_evidence(evidence_from_context(ctx))
        slate = _render_candidate_slate(candidates)
        # Calibration-aware advisory note (never a gate): when the lineage's
        # recent hypothesis predictions have mostly borne out, tell the
        # critic to value candidates that state concrete expected movements.
        accuracy = recent_prediction_accuracy(ctx)
        calibration_note = ""
        if accuracy is not None and accuracy >= CALIBRATION_TRUST_BAR:
            calibration_note = (
                "\nNote: this lineage's recent hypothesis predictions have "
                f"mostly borne out (accuracy {accuracy:.0%}). All else equal, "
                "prefer a candidate whose hypothesis states concrete expected "
                "movements over one that does not.\n"
            )
        user_prompt = (
            "Select the single best candidate proposal.\n\n"
            "## Round context (the SAME restricted view the proposer saw — "
            "no held-out data)\n"
            f"{restricted_context}\n\n"
            "## Candidate proposals\n"
            f"{slate}\n"
            # Optional counts-only screen block (the calibration-note
            # precedent) — empty for every unscreened / veto-only call, so
            # the prompt then carries no screen block at all.
            f"{calibration_note}"
            f"{screen_note}\n"
            f"Respond with the integer index (0..{len(candidates) - 1}) of "
            "the best candidate ALONE on the first line, then one sentence "
            "under 200 characters naming the quality-bar clause that decided it."
        )
        # The critique is the call that picks the winner, so its input is the
        # record that answers "which candidate shipped, and on what basis".
        capture_proposer_input(
            workspace_root=ctx.workspace_root,
            epoch_id=ctx.epoch_id,
            role=ROLE_CRITIQUE,
            system=_CRITIC_SYSTEM_PROMPT,
            user=user_prompt,
            model=ctx.model,
            parent_generation_id=ctx.parent_generation_id,
            new_generation_id=ctx.new_generation_id,
        )
        try:
            response = await aux_call_llm(_CRITIC_SYSTEM_PROMPT, user_prompt, ctx.model)
        except Exception as exc:  # noqa: BLE001 — opaque LLM errors are common
            log.debug("best-of-N critique call failed (%s); using heuristic", exc)
            return None, ""
        return _parse_critic_choice(response or "", len(candidates))


def wrap_with_proposer_quality(
    inner: ProposerAgent,
    config: ProposerQualityConfig,
    *,
    breadth_call_llm: CallLLM | None = None,
    depth_call_llm: CallLLM | None = None,
    breadth_model: str | None = None,
    depth_model: str | None = None,
    propose_parallelism: int = 1,
) -> ProposerAgent:
    """Interpose best-of-N + self-critique unless the contract opts out.

    Returns ``inner`` UNCHANGED when ``config.best_of_n <= 1`` — the opt-out a
    contract pins to get the historical single-sample proposer — so a contract
    that pins it pays nothing: there is not even a wrapper object in the call
    path. Otherwise wraps ``inner`` in a :class:`BestOfNProposerAgent`. The
    orchestrator calls this once per evolve invocation, right after it builds
    the epoch's proposer agent.

    ``breadth_call_llm`` / ``depth_call_llm`` are the ensemble roles
    (typically ``config.proposer_breadth_call_llm`` /
    ``config.proposer_depth_call_llm`` off the
    :class:`~zicato.core.runtime.RuntimeConfig`): the slate SAMPLING callable
    and the CRITIQUE + REVISE callable. Both default to ``None``, in which
    case the wrapper resolves each per-propose to ``ctx.aux_call_llm`` — the
    workspace's evaluation callable, so an unconfigured ensemble runs every
    call on it. They are irrelevant on the ``best_of_n <= 1`` pass-through
    (no wrapper, no critique).

    ``breadth_model`` / ``depth_model`` are the role MODEL-NAME strings that
    accompany the callables (typically ``config.proposer_breadth_model`` /
    ``config.proposer_depth_model`` off the :class:`RuntimeConfig`, set only
    when the role was configured via a *model spec*). The wrapper swaps them
    onto ``ctx.model`` at the sampling/revise sites so the default ADK
    proposer — which binds the model STRING, not ``ctx.aux_call_llm`` — honors
    the role. ``None`` (the common case, a callable-only or absent role) leaves
    ``ctx.model`` at its own value.

    ``propose_parallelism`` (typically ``config.propose_parallelism`` off the
    :class:`RuntimeConfig`, whose own default is ``4``) sizes the
    slate-sampling gather's semaphore. ``1`` — this parameter's default, so a
    caller that passes nothing gets it — runs the slate serially; the
    deterministic post-gather pass makes any value produce the SAME slate +
    event stream regardless of slot completion order. Irrelevant on the
    ``best_of_n <= 1`` pass-through.
    """
    if config.best_of_n <= 1:
        return inner
    return BestOfNProposerAgent(
        inner=inner,
        config=config,
        breadth_call_llm=breadth_call_llm,
        depth_call_llm=depth_call_llm,
        breadth_model=breadth_model,
        depth_model=depth_model,
        propose_parallelism=propose_parallelism,
    )


__all__ = [
    "CALIBRATION_TRUST_BAR",
    "EDIT_CLASS_HINTS",
    "RATIONALE_CAP",
    "BestOfNProposerAgent",
    "CandidateScreenResult",
    "ScreenRunner",
    "normalize_selection_rationale",
    "recent_prediction_accuracy",
    "wrap_with_proposer_quality",
]
