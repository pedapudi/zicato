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
already inside that envelope.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any

from zicato.core.types import Experiment, ProposerQualityConfig
from zicato.proposer.agent import ProposerAgent, ProposerContext
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

#: Per-slot edit-class steering for the best-of-N slate — the intra-slate
#: DIVERSITY lever. Slot ``i`` of the slate gets ``EDIT_CLASS_HINTS[i %
#: len]`` stamped on its :class:`ProposerContext` (``sample_hint``), so the
#: N samples explore genuinely different edit strategies instead of the
#: LLM's sampling re-rolling one idea three times. Static instruction
#: strings only — no board identity, no per-entry data — so the hints
#: compose with the restricted-visibility envelope untouched.
EDIT_CLASS_HINTS: tuple[str, ...] = (
    "For THIS candidate, prefer the smallest grounded fix: the minimal, "
    "most surgical edit that directly addresses an observed failure mode.",
    "For THIS candidate, prefer a structurally different mechanism than "
    "the most recent attempts in the experiment memory — do not re-roll a "
    "variation of the last hypothesis; change the approach, not the dial.",
    "For THIS candidate, target the highest-loss failure mode head-on, "
    "even if the edit is larger — go after the biggest observed cost.",
)

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


def _heuristic_best_index(candidates: list[Experiment], ctx: ProposerContext) -> int:
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
    4. **Stable order** — ties break toward the earlier-sampled candidate, so
       the selection is deterministic for a fixed slate.

    Returns the index into ``candidates``. ``candidates`` is non-empty by
    contract (the caller handles the empty slate).
    """
    accuracy = recent_prediction_accuracy(ctx)
    calibrated = accuracy is not None and accuracy >= CALIBRATION_TRUST_BAR

    def _key(i: int) -> tuple[bool, bool, int, int]:
        return (
            not _targets_observed_failure(candidates[i], ctx),  # grounded first
            # False sorts first: with a calibrated lineage, prediction-bearing
            # candidates rank ahead; otherwise the term is constant (inert).
            calibrated and not _carries_expected_movements(candidates[i]),
            _diff_size(candidates[i]),
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
        for sample in range(n):
            # Intra-slate diversity: each slot carries a DISTINCT edit-class
            # hint (rotating through EDIT_CLASS_HINTS) on its context, so the
            # N samples explore different edit strategies rather than
            # re-rolling one idea. The hint is a static instruction string —
            # no board identity — so the restricted-visibility envelope is
            # untouched.
            slot_ctx = replace(ctx, sample_hint=EDIT_CLASS_HINTS[sample % len(EDIT_CLASS_HINTS)])
            try:
                candidates.append(await self.inner.propose(slot_ctx))
            except ProposerError as exc:
                # A candidate the inner proposer could not produce simply
                # narrows the slate; remember the error so an all-failed
                # slate can re-raise the real failure.
                last_error = exc
            else:
                _emit_round_event(ctx, "candidate_sampled", {"i": sample, "n": n})

        if not candidates:
            # The whole slate failed — surface the inner failure exactly as a
            # single propose would (the caller's rejected-outcome path handles
            # it). ``last_error`` is set because n >= 2 means the loop ran.
            if last_error is not None:
                raise last_error
            raise ProposerError(["best-of-N produced no candidates"])  # pragma: no cover

        if len(candidates) == 1:
            return candidates[0]

        chosen, selection_mode = await self._select_best(candidates, ctx)
        chosen, selection_mode = await self._align_child_tree(
            candidates, chosen, selection_mode, ctx
        )
        _emit_round_event(ctx, "critique_selected", {"index": chosen, "reason": selection_mode})
        return candidates[chosen]

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
        self, candidates: list[Experiment], ctx: ProposerContext
    ) -> tuple[int, str]:
        """Return ``(best index, selection mode)`` against the §4.1 quality bar.

        Runs the self-critique LLM pass when it is enabled and an auxiliary
        callable is available; otherwise (or on any critique failure) falls
        back to the deterministic :func:`_heuristic_best_index`. Either way
        the selection sees ONLY the restricted proposer context. The mode
        string (``"critique"`` / ``"heuristic"``) is round-log provenance
        only — the caller's choice of candidate is the index.
        """
        if not self.config.critique_enabled:
            return _heuristic_best_index(candidates, ctx), "heuristic"

        aux_call_llm = ctx.aux_call_llm
        if aux_call_llm is None:  # pragma: no cover — orchestrator always wires it
            return _heuristic_best_index(candidates, ctx), "heuristic"

        choice = await self._critique(aux_call_llm, candidates, ctx)
        if choice is None:
            # Critique failed / unparseable — fall back to the heuristic so a
            # flaky critic never blocks the step.
            return _heuristic_best_index(candidates, ctx), "heuristic"
        return choice, "critique"

    async def _critique(
        self,
        aux_call_llm: Callable[[str, str, str], Awaitable[str]],
        candidates: list[Experiment],
        ctx: ProposerContext,
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
            f"{calibration_note}\n"
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
    "recent_prediction_accuracy",
    "wrap_with_proposer_quality",
]
