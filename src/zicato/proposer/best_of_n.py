"""Best-of-N sampling + a self-critique pass over the proposer step.

FUNCTIONALITY-RECOMMENDATIONS.md §4.1 — the top proposal-quality lever. Today
the proposer takes ONE sample, and the retry loop only fires on *invalid*
output: a valid-but-mediocre proposal is never reconsidered. This module wraps
any :class:`~zicato.proposer.agent.ProposerAgent` so that, per propose-step,
it samples ``best_of_n`` candidate experiments and then a cheap self-critique
pass picks (or, by heuristic, selects) the best against a quality bar —
grounded in a tool call? targets a real failure mode? minimal diff?

Best-of-N is the DEFAULT (``proposer_quality.best_of_n == 3``): each
propose-step samples a slate of three and critiques. Each slate slot carries a
distinct edit-class hint (:data:`EDIT_CLASS_HINTS`) so the N samples explore
different edit strategies rather than re-rolling one. A contract that pins
``best_of_n: 1`` short-circuits to a single inner ``propose`` call with NO
critique and NO extra work (:meth:`BestOfNProposerAgent.propose`) — the
historical single-sample proposer, the right pin for scripted/deterministic
proposers.

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

Screen-informed revise (WS-R; bounded, rides the screen opt-in)
---------------------------------------------------------------
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
that propose-step cost class. With screening off nothing here runs and
the propose path is byte-identical.

The recombination slot (WS-REC; opt-in, rides ``recombine`` + ``best_of_n > 1``)
---------------------------------------------------------------------------------
When the orchestrator threads a per-round
:attr:`~zicato.proposer.agent.ProposerContext.recombine_pair` (two
rejected complementary challengers of the current reign, selected by
:mod:`zicato.epoch.recombine`), the LAST slate slot MINTS their patch
union (:mod:`zicato.proposer.recombine`) instead of sampling the LLM —
cost-neutral by construction: the mint REPLACES the slot's auxiliary
propose call. The mint runs ``enforce_forbidden`` plus the SAME validate
hook every sample runs; any finding degrades the slot to a normal fresh
sample. A NON-VETOED mint short-circuits the selection
(``selection_mode="recombined"``, no critic call) because the heuristic's
minimal-diff key would systematically starve the union — its diff is
larger than either parent's by construction; a VETOED mint stays an
ordinary slate member and every existing path is unchanged.

Overfitting discipline (LOAD-BEARING)
-------------------------------------
The self-critique pass sees ONLY the SAME restricted prompt context the
proposer itself sees — the train-slice patterns (aggregated when
``restrict_visibility``), the banded experiment memory, the bucketed
failure-mode profile — assembled by the same
:func:`~zicato.proposer.prompts.render_user_prompt` renderer under the same
``restrict_visibility`` flag. It NEVER sees the holdout and never sees a
per-entry identity the proposer is not already allowed to see. The critic is
inside the same overfitting-visibility envelope as the proposer
(OVERFITTING.md §11); it cannot widen what the proposer learns about the
board. The candidates it ranks are the proposer's own outputs, which are
already inside that envelope. The screen's feeds keep the same envelope:
counts only, never an entry id.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any

from zicato.core.types import Experiment, ProposerQualityConfig
from zicato.proposer.agent import ProposerAgent, ProposerContext

# EDIT_CLASS_HINTS moved to :mod:`zicato.proposer.hints` (its canonical home,
# alongside the failure-mode-conditioned FAILURE_MODE_HINTS and the pure
# slot→hint mapping); re-exported here so every existing import keeps working.
from zicato.proposer.hints import EDIT_CLASS_HINTS, hint_for_slot
from zicato.proposer.prompts import render_user_prompt
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
       leaves the term constant — inert, byte-identical ordering.
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
    size. Deliberately compact — the critic ranks the candidates, it does not
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
    "Pick the candidate that best satisfies the bar. Respond with ONLY the "
    "integer index of the best candidate (e.g. `0`) — no prose, no JSON, no "
    "explanation."
)


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


def _emit_round_event(ctx: ProposerContext, type_token: str, fields: dict[str, Any]) -> None:
    """Best-effort round-log emission through the context's optional emitter.

    The emitter seam keeps the proposer decoupled from the round-log module
    (WS8): the orchestrator threads an ``emitter(type_token, fields)``
    callable on :attr:`ProposerContext.round_event_emitter`; ``None`` (every
    caller that does not opt in) emits nothing. Guarded here so a raising
    emitter can never fail a propose step.
    """
    emitter = ctx.round_event_emitter
    if emitter is None:
        return
    try:
        emitter(type_token, fields)
    except Exception as exc:  # noqa: BLE001 — emission must never fail a propose
        log.debug("round-log %s emission skipped: %s", type_token, exc)


def _parse_critic_choice(response: str, n: int) -> int | None:
    """Parse the critic's chosen index out of its raw response.

    Tolerant: the critic is asked for a bare integer, but a stray fence /
    prose is salvaged by scanning for the first integer token. Returns the
    chosen index when it is in ``range(n)``, else ``None`` (the caller falls
    back to the heuristic).
    """
    import re  # noqa: PLC0415

    if not response:
        return None
    match = re.search(r"-?\d+", response)
    if match is None:
        return None
    try:
        choice = int(match.group(0))
    except ValueError:
        return None
    if 0 <= choice < n:
        return choice
    return None


@dataclass
class BestOfNProposerAgent:
    """Wrap a :class:`ProposerAgent` with best-of-N sampling + self-critique.

    Construct it around the epoch's resolved inner agent and the contract's
    :class:`~zicato.core.types.ProposerQualityConfig`. With an explicit
    ``best_of_n == 1`` pin :meth:`propose` is a transparent pass-through to
    the inner agent — no extra sampling, no critique call — the historical
    single-sample behaviour (the DEFAULT config samples a slate of 3).

    The wrapper preserves the inner agent's failure contract: when no
    candidate can be sampled it re-raises the inner
    :class:`~zicato.proposer.proposer.ProposerError`, exactly as a single
    ``propose`` would, so every call site that already handles a failed
    propose is unaffected.
    """

    inner: ProposerAgent
    config: ProposerQualityConfig

    async def propose(self, ctx: ProposerContext) -> Experiment:
        n = self.config.best_of_n
        if n <= 1:
            # Byte-identical to today: one inner sample, no critique.
            return await self.inner.propose(ctx)

        candidates: list[Experiment] = []
        last_error: ProposerError | None = None
        recombined_index: int | None = None
        for sample in range(n):
            if sample == n - 1 and ctx.recombine_pair is not None:
                # The recombination slot (WS-REC): the LAST slot MINTS the
                # union of the round's selected pair instead of sampling
                # the LLM — the mint REPLACES the slot's auxiliary propose
                # call (cost-neutral: n−1 calls, never more). Landing in
                # the last slot makes a chosen mint the LAST-validated
                # candidate, so the tree alignment below is a no-op on the
                # happy path. A mint that fails forbidden-enforcement or
                # the validate hook DEGRADES to the normal fresh sample
                # below — the identical slot body, with the slot's normal
                # exploratory hint (a recombination failure must never
                # narrow the slate).
                minted = await self._mint_recombined(ctx, sample, n)
                if minted is not None:
                    candidates.append(minted)
                    recombined_index = len(candidates) - 1
                    continue
            last_error = await self._sample_slot(candidates, ctx, sample, n) or last_error

        if not candidates:
            # The whole slate failed — surface the inner failure exactly as a
            # single propose would (the caller's rejected-outcome path handles
            # it). ``last_error`` is set because n >= 2 means the loop ran.
            if last_error is not None:
                raise last_error
            raise ProposerError(["best-of-N produced no candidates"])  # pragma: no cover

        if len(candidates) == 1:
            return candidates[0]

        # Optional pre-tournament candidate screen (tryouts) — VETO-FIRST:
        # a catastrophic regression is disqualified here, but the screen
        # never ranks; the critic/heuristic below still chooses among the
        # survivors. ``None`` (unscreened — no runner threaded, screen
        # error, or malformed result) leaves the selection byte-identical.
        screen_results = await self._screen_slate(candidates, ctx)

        if recombined_index is not None and (
            screen_results is None or not screen_results[recombined_index].vetoed
        ):
            # SELECTION SHORT-CIRCUIT (WS-REC): a NON-VETOED mint is chosen
            # outright — no critic call (the sole-survivor precedent). The
            # heuristic's minimal-diff key would systematically STARVE the
            # union (its diff is larger than either parent's BY
            # CONSTRUCTION — the parsimony bias the slot exists to
            # overcome; the starved-heuristic OC test documents the failing
            # alternative). The mint is grounded in MEASURED per-entry
            # evidence from two real tournament rounds, the screen above
            # could still veto it, and the unchanged gate remains the
            # arbiter. A VETOED mint takes the else-branch as an ordinary
            # slate member — every existing path is unchanged.
            chosen, selection_mode = recombined_index, "recombined"
            chosen, selection_mode = await self._align_child_tree(
                candidates, chosen, selection_mode, ctx
            )
            _emit_round_event(ctx, "critique_selected", {"index": chosen, "reason": selection_mode})
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
                # screen-informed revise pass first (WS-R); only when it
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
            # critique call needed. It is also the LAST-validated
            # candidate, so the tree alignment below is a no-op.
            chosen, selection_mode = len(candidates) - 1, "screen_revise_survivor"
        elif screen_results is not None and not all_vetoed and len(survivor_indices) == 1:
            # A single survivor needs no critique call — the veto already
            # decided the slate.
            chosen, selection_mode = survivor_indices[0], "screen_sole_survivor"
        else:
            chosen, selection_mode = await self._select_over(
                candidates, survivor_indices, screen_results, ctx
            )
            if all_vetoed:
                selection_mode = f"{vetoed_mode_prefix}:{selection_mode}"
        chosen, selection_mode = await self._align_child_tree(
            candidates, chosen, selection_mode, ctx
        )
        _emit_round_event(ctx, "critique_selected", {"index": chosen, "reason": selection_mode})
        return candidates[chosen]

    async def _sample_slot(
        self, candidates: list[Experiment], ctx: ProposerContext, sample: int, n: int
    ) -> ProposerError | None:
        """One ordinary slate slot — the extracted loop body.

        Intra-slate diversity: each slot carries a DISTINCT edit-class
        hint on its context, so the N samples explore different edit
        strategies rather than re-rolling one idea. The pure mapping
        (:func:`zicato.proposer.hints.hint_for_slot`) conditions slots
        0..N-2 on the profile's DOMINANT failure mode and keeps the LAST
        slot exploratory; with no profile signal it is the historical
        EDIT_CLASS_HINTS rotation, byte-identical. Hints are static
        instruction strings — no board identity — so the
        restricted-visibility envelope is untouched.

        Appends the sampled candidate on success and emits its
        ``candidate_sampled`` event; returns the :class:`ProposerError`
        when the inner proposer could not produce one (the slate simply
        narrows; the caller remembers the error so an all-failed slate can
        re-raise the real failure). Extracted so the recombination slot's
        degrade path reuses the slot body VERBATIM.
        """
        slot_ctx = replace(ctx, sample_hint=hint_for_slot(sample, n, ctx.failure_profile))
        try:
            candidates.append(await self.inner.propose(slot_ctx))
        except ProposerError as exc:
            return exc
        _emit_round_event(ctx, "candidate_sampled", {"i": sample, "n": n})
        return None

    async def _mint_recombined(
        self, ctx: ProposerContext, sample: int, n: int
    ) -> Experiment | None:
        """Mint the round's recombination pair into the slate. GUARDED.

        Pure mint (:func:`zicato.proposer.recombine.mint_recombined_experiment`
        — no LLM call, no IO), then two defenses before the mint may enter
        the slate:

        * :func:`~zicato.proposer.brief.enforce_forbidden` — defense in
          depth: both parents cleared the brief when they were proposed,
          but the brief's forbidden set may have changed since.
        * the SAME post-apply validate hook every sampled candidate runs
          (``ctx.validate_experiment``) — it derives the shared child
          snapshot from the mint's patches, so a chosen mint is the
          last-validated candidate and the tree alignment stays honest.

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
        _emit_round_event(ctx, "candidate_sampled", {"i": sample, "n": n, "recombined": True})
        return minted

    async def _screen_slate(
        self, candidates: list[Experiment], ctx: ProposerContext
    ) -> list[CandidateScreenResult] | None:
        """Run the optional candidate screen over the settled slate. GUARDED.

        ``None`` — no screen runner on the context (every contract that
        does not opt in), a raising runner, or a malformed result — means
        UNSCREENED: the caller selects exactly as before. Screening must
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
            _emit_round_event(ctx, "candidate_screened", _screened_event_fields(i, res))
        return results

    async def _revise_all_vetoed(
        self,
        candidates: list[Experiment],
        screen_results: list[CandidateScreenResult],
        ctx: ProposerContext,
        n: int,
    ) -> str:
        """The ONE bounded screen-informed revise pass (WS-R). GUARDED.

        Called only for an ALL-VETOED screened slate — the one screen
        verdict under which proceeding is *knowingly* wasteful (the step
        would send a vetoed candidate to a full tournament round). That
        is deliberately the WHOLE trigger: a cold-start slate whose
        survivors were merely crash-only screened (no champion-passing
        baseline, so no pass-flip was ever detectable —
        :class:`~zicato.epoch.screen.ScreenPanel.baseline_pass_ids`
        empty) does NOT revise, because a replacement would face the same
        crash-only panel and could earn no stronger signal than the
        survivors already hold; and a no-signal survivor (screen error)
        is the screen's own degrade-to-unscreened contract, not evidence
        against the slate.

        One replacement is re-sampled with the slate's COUNTS-ONLY veto
        summary seeded through the repair-feedback machinery
        (:attr:`ProposerContext.revise_feedback` — the same ``feedback``
        slot a validation failure threads on retry; never an entry id,
        so the restricted-visibility envelope is untouched), then
        screened GUARDED. Exactly one revise per propose — this method
        never re-enters :meth:`propose` and never loops.

        MUTATES ``candidates``: a successfully-proposed replacement is
        APPENDED whatever its own screen verdict, because its post-apply
        validation (inside the inner ``propose``) just re-derived the
        shared on-disk child tree — appending keeps
        :meth:`_align_child_tree`'s last-validated bookkeeping honest on
        every downstream path.

        Returns one of:

        * ``"chosen"`` — the replacement survived (or could not be
          screened — the screen-failure degrade): the caller selects it.
        * ``"fallback"`` — the replacement was itself vetoed: the caller
          degrades to critic-over-ALL over the ORIGINAL slate, with the
          ``screen_all_vetoed_after_revise`` mode prefix recording that
          the revise was spent.
        * ``"unavailable"`` — the inner proposer produced no replacement:
          the caller degrades exactly as before the revise existed
          (``screen_all_vetoed``). The last-validated original's child
          tree is restored first, since the failed revise attempts may
          have clobbered it.
        """
        revise_index = len(candidates)
        feedback = _render_revise_feedback(screen_results)
        try:
            replacement = await self.inner.propose(replace(ctx, revise_feedback=feedback))
        except Exception as exc:  # noqa: BLE001 — the revise must never fail a propose
            log.debug(
                "screen-informed revise produced no replacement (%s); "
                "degrading to critic-over-all",
                exc,
            )
            await self._restore_last_validated_tree(candidates, ctx)
            return "unavailable"
        _emit_round_event(ctx, "candidate_sampled", {"i": revise_index, "n": n, "revise": True})
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
        _emit_round_event(ctx, "candidate_screened", _screened_event_fields(index, res, True))
        return res

    async def _restore_last_validated_tree(
        self, candidates: list[Experiment], ctx: ProposerContext
    ) -> None:
        """Re-derive the last original candidate's child tree after a failed revise.

        A revise ``propose`` that failed may still have run its post-apply
        validation attempts, each of which re-derives the SHARED child
        snapshot in place — so the on-disk tree can belong to a rejected
        revise attempt rather than to ``candidates[-1]`` (the state every
        downstream path assumes). One idempotent hook call restores it.
        If even the restore fails, no candidate's tree can be mounted
        consistently and the step surfaces the standard
        :class:`~zicato.proposer.proposer.ProposerError` — the
        :meth:`_align_child_tree` both-failed precedent.
        """
        validate = ctx.validate_experiment
        if validate is None:
            return
        findings = await self._revalidate(validate, candidates[-1])
        if findings:
            raise ProposerError(
                [
                    f"restore of last-validated candidate after a failed revise failed: {f}"
                    for f in findings
                ]
            )

    async def _select_over(
        self,
        candidates: list[Experiment],
        survivor_indices: list[int],
        screen_results: list[CandidateScreenResult] | None,
        ctx: ProposerContext,
    ) -> tuple[int, str]:
        """Select over the surviving sub-slate; map the index back to the slate.

        The screen's measurements feed the selection only as a LATE
        tiebreak — and only when the contract has not pinned
        ``screen_veto_only`` — through two advisory channels: the critique
        prompt's counts-only ``## Screen measurements`` block, and the
        heuristic's penultimate panel-scalar key. Unscreened (or
        veto-only), both channels are inert and the selection is
        byte-identical to the pre-screen wrapper.
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
        sub_choice, selection_mode = await self._select_best(
            sub, ctx, screen_scalars=screen_scalars, screen_note=screen_note
        )
        return survivor_indices[sub_choice], selection_mode

    async def _align_child_tree(
        self,
        candidates: list[Experiment],
        chosen: int,
        selection_mode: str,
        ctx: ProposerContext,
    ) -> tuple[int, str]:
        """Make the on-disk child tree match the CHOSEN candidate.

        Every slate sample's post-apply validation (``ctx.validate_experiment``)
        derives the SAME fixed child snapshot in place — each attempt clears
        the previous attempt's tree (see
        :func:`zicato.evolve.round.build_post_apply_validator`) — so after N
        samples the on-disk child tree belongs to the LAST successfully-
        validated candidate, while the selection above may pick an EARLIER
        one. Both evolve pipelines mount that snapshot while persisting the
        CHOSEN candidate's experiment (and the field path additionally judges
        the chosen hypothesis's diversity signature), so a mismatch would
        score — and diversity-judge — a tree that is not the experiment on
        record.

        When the chosen candidate is not the last-validated one, run the
        validation hook once more on it: the hook re-derives the child
        snapshot from the chosen candidate's patches (the same idempotent
        clear-and-reapply a retry performs), so the mounted tree and the
        returned experiment agree.

        The chosen candidate validated cleanly moments ago, so findings here
        are unexpected (e.g. the parent tree changed underneath the slate).
        On any finding the selection FALLS BACK to the last-validated
        candidate — whose tree is restored by one more hook call, because the
        failed re-derive cleared it — so tree and experiment stay consistent
        either way. If even the restore fails, no candidate's tree can be
        mounted consistently and the step surfaces the standard
        :class:`~zicato.proposer.proposer.ProposerError` every call site
        already handles.

        Returns the (possibly changed) ``(chosen, selection_mode)`` pair; a
        fallback stamps ``:revalidate-fallback`` onto the mode string so the
        round log records why the critic's pick was not returned.
        """
        last_validated = len(candidates) - 1
        validate = ctx.validate_experiment
        if validate is None or chosen == last_validated:
            return chosen, selection_mode
        findings = await self._revalidate(validate, candidates[chosen])
        if not findings:
            return chosen, selection_mode
        log.warning(
            "best-of-N: re-validating chosen candidate %d failed unexpectedly (%s); "
            "falling back to last-validated candidate %d so the mounted child tree "
            "matches the persisted experiment",
            chosen,
            "; ".join(findings),
            last_validated,
        )
        restore_findings = await self._revalidate(validate, candidates[last_validated])
        if restore_findings:
            # The fallback candidate no longer re-derives either — there is
            # no candidate whose tree can be mounted consistently with its
            # experiment. Surface the standard proposer failure.
            raise ProposerError(
                [f"re-validate of chosen candidate {chosen} failed: {f}" for f in findings]
                + [
                    f"re-validate of fallback candidate {last_validated} failed: {f}"
                    for f in restore_findings
                ]
            )
        return last_validated, f"{selection_mode}:revalidate-fallback"

    @staticmethod
    async def _revalidate(
        validate: Callable[[Experiment], Awaitable[list[str]]], candidate: Experiment
    ) -> list[str]:
        """Run the post-apply hook once; any raise is reported as a finding.

        The hook's own contract is to RETURN findings (it already folds a
        ``derive_generation`` rejection into one), so an exception here is
        doubly unexpected — fold it into the findings list so the caller's
        fallback logic handles both failure shapes identically.
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
    ) -> tuple[int, str]:
        """Return ``(best index, selection mode)`` against the §4.1 quality bar.

        Runs the self-critique LLM pass when it is enabled and an auxiliary
        callable is available; otherwise (or on any critique failure) falls
        back to the deterministic :func:`_heuristic_best_index`. Either way
        the selection sees ONLY the restricted proposer context. The mode
        string (``"critique"`` / ``"heuristic"``) is round-log provenance
        only — the caller's choice of candidate is the index.

        ``screen_scalars`` / ``screen_note`` are the OPTIONAL candidate-
        screen tiebreak feeds (heuristic key / critic prompt block); both
        default inert — every unscreened caller is byte-identical.
        """
        if not self.config.critique_enabled:
            return _heuristic_best_index(candidates, ctx, screen_scalars), "heuristic"

        aux_call_llm = ctx.aux_call_llm
        if aux_call_llm is None:  # pragma: no cover — orchestrator always wires it
            return _heuristic_best_index(candidates, ctx, screen_scalars), "heuristic"

        choice = await self._critique(aux_call_llm, candidates, ctx, screen_note)
        if choice is None:
            # Critique failed / unparseable — fall back to the heuristic so a
            # flaky critic never blocks the step.
            return _heuristic_best_index(candidates, ctx, screen_scalars), "heuristic"
        return choice, "critique"

    async def _critique(
        self,
        aux_call_llm: Callable[[str, str, str], Awaitable[str]],
        candidates: list[Experiment],
        ctx: ProposerContext,
        screen_note: str = "",
    ) -> int | None:
        """One cheap self-critique LLM call; returns the chosen index or None.

        Builds the critic's user prompt from the SAME restricted context the
        proposer saw — rendered through :func:`render_user_prompt` under the
        same ``restrict_visibility`` flag, so the patterns are aggregated and
        the experiment memory banded exactly as for the proposer — plus the
        compact candidate slate. The critic NEVER receives the holdout or any
        identity the proposer did not already see. Best-effort: a raising /
        timing-out / unparseable critic returns ``None`` and the caller falls
        back to the heuristic.
        """
        restricted_context = render_user_prompt(
            current_loss_summary=ctx.current_loss_summary,
            patterns=ctx.patterns,
            mutations=ctx.mutations,
            prior_experiments=ctx.prior_experiments,
            restrict_visibility=ctx.restrict_visibility,
            custom_judge_names=ctx.custom_judge_names or frozenset(),
            failure_profile=ctx.failure_profile,
            # The critic stays inside the SAME visibility envelope as the
            # proposer: the redacted exemplar block (when the contract opted
            # in) is part of that envelope — never anything beyond it.
            process_exemplars=ctx.process_exemplars,
        )
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
            # the prompt is byte-identical to the pre-screen wrapper.
            f"{calibration_note}"
            f"{screen_note}\n"
            f"Respond with ONLY the integer index (0..{len(candidates) - 1}) "
            "of the best candidate."
        )
        try:
            response = await aux_call_llm(_CRITIC_SYSTEM_PROMPT, user_prompt, ctx.model)
        except Exception as exc:  # noqa: BLE001 — opaque LLM errors are common
            log.debug("best-of-N critique call failed (%s); using heuristic", exc)
            return None
        return _parse_critic_choice(response or "", len(candidates))


def wrap_with_proposer_quality(
    inner: ProposerAgent, config: ProposerQualityConfig
) -> ProposerAgent:
    """Interpose best-of-N + self-critique only when an operator opts in.

    Returns ``inner`` UNCHANGED when ``config.best_of_n <= 1`` (the default),
    so a contract that does not opt in pays nothing and behaves
    byte-identically — there is not even a wrapper object in the call path.
    Otherwise wraps ``inner`` in a :class:`BestOfNProposerAgent`. The
    orchestrator calls this once per evolve invocation, right after it builds
    the epoch's proposer agent.
    """
    if config.best_of_n <= 1:
        return inner
    return BestOfNProposerAgent(inner=inner, config=config)


__all__ = [
    "CALIBRATION_TRUST_BAR",
    "EDIT_CLASS_HINTS",
    "BestOfNProposerAgent",
    "CandidateScreenResult",
    "ScreenRunner",
    "recent_prediction_accuracy",
    "wrap_with_proposer_quality",
]
