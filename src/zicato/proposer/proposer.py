"""Orchestration entry point for the structured proposer.

The function in this module:

1. Builds the system + user prompts from the proposer brief, patterns,
   and mutation manifest.
2. Calls the auxiliary LLM (zicato never invokes the harness LLM here
   — see :class:`zicato.core.types.RuntimeConfig` for the two-callable
   invariant).
3. Parses the response into a typed :class:`Experiment` via
   :func:`zicato.proposer.structured.parse_experiment_json`.
4. Enforces the proposer brief's forbidden-id list against the emitted
   patches.
5. Optionally runs a caller-supplied post-parse validation hook against
   the typed experiment (the orchestrator uses this to apply the patch
   set and run the post-apply validator).
6. On parse failure, forbidden-id violation, or a non-empty finding
   list from the validation hook, appends the error to the next user
   prompt and retries up to ``max_retries`` times.
7. Raises :class:`ProposerError` after all retries are exhausted.

The orchestrator does NOT write the resulting :class:`Experiment` to
disk — that is the CLI's job (see ``zicato.cli.commands.propose``).
Keeping the I/O at the CLI layer means tests can drive the orchestrator
with stub callables and assert on the returned dataclass without
tmpdir bookkeeping.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from zicato.aux_timeout import aux_call_timeout_s
from zicato.core.types import (
    Experiment,
    MutationPoint,
    Pattern,
    PriorExperiment,
    ProposerSkill,
)
from zicato.proposer.brief import enforce_forbidden
from zicato.proposer.input_capture import ROLE_PROPOSAL, capture_proposer_input
from zicato.proposer.prompts import render_system_prompt, render_user_prompt
from zicato.proposer.structured import (
    ExperimentParseError,
    PostApplyValidationError,
    parse_experiment_json,
)

if TYPE_CHECKING:  # pragma: no cover - typing-only import
    from zicato.index.query import MutationTrackRecord
    from zicato.proposer.calibration import CalibrationSummary
    from zicato.proposer.genealogy import GenealogyItem
    from zicato.telemetry.meta_loop import MetaLoopEmitter

#: An optional post-parse validation hook. The proposer calls it with a
#: fully-parsed, forbidden-id-clean :class:`Experiment` and expects back
#: a list of human-readable error strings — empty when the experiment is
#: acceptable. A non-empty list is treated as a *retryable* failure: the
#: errors are fed back to the proposer as concrete feedback and the next
#: attempt re-proposes, exactly as a parse error would. The orchestrator
#: supplies a hook that applies the patch set to a child snapshot and
#: runs :func:`zicato.mutation.validator.validate_post_apply`, so a
#: destructive patch (a dropped import, a vanished marker) costs one
#: retry instead of a wasted tournament round.
ExperimentValidator = Callable[[Experiment], Awaitable[list[str]]]


class ProposerError(RuntimeError):
    """Raised when the proposer fails to produce a schema-valid Experiment.

    Carries the accumulated list of attempt failures so the operator
    (or the calling CLI) can render a sensible error message rather
    than a one-line "the proposer gave up". The :attr:`attempts` field
    is the per-attempt error message in call order; the human-readable
    rendering joins them.
    """

    def __init__(self, attempts: list[str]) -> None:
        self.attempts = list(attempts)
        joined = "\n".join(f"  attempt {i + 1}: {msg}" for i, msg in enumerate(self.attempts))
        super().__init__(f"proposer failed after {len(self.attempts)} attempt(s):\n{joined}")


async def propose_experiment(
    *,
    epoch_id: str,
    parent_generation_id: str,
    new_generation_id: str,
    patterns: Iterable[Pattern],
    mutations: Iterable[MutationPoint],
    brief_text: str,
    current_loss_summary: str,
    aux_call_llm: Callable[[str, str, str], Awaitable[str]],
    model: str = "",
    max_retries: int = 2,
    forbidden_ids: tuple[str, ...] = (),
    workspace_root: Path | None = None,
    validate_experiment: ExperimentValidator | None = None,
    meta_loop_emitter: MetaLoopEmitter | None = None,
    custom_judge_names: frozenset[str] | None = None,
    prior_experiments: Iterable[PriorExperiment] = (),
    skills: tuple[ProposerSkill, ...] = (),
    restrict_visibility: bool = False,
    failure_profile: str = "",
    process_exemplars: str = "",
    genealogy: tuple[GenealogyItem, ...] = (),
    calibration: CalibrationSummary | None = None,
    sample_hint: str = "",
    mutation_track_records: Mapping[str, MutationTrackRecord] | None = None,
    revise_feedback: str = "",
    slot_index: int | None = None,
) -> Experiment:
    """Compose prompts, call the auxiliary LLM, parse the response.

    Parameters
    ----------
    epoch_id:
        Lineage coordinate for the epoch under which this experiment
        runs.
    parent_generation_id:
        The generation this experiment is challenging.
    new_generation_id:
        The id assigned to the child generation this experiment
        produces.
    patterns:
        Iterable of cross-run :class:`Pattern` observations to surface
        to the proposer. Advisory — the proposer chooses which to
        address.
    mutations:
        Iterable of :class:`MutationPoint` instances — the valid patch
        targets. The orchestrator builds the id→MutationPoint map once
        from this iterable.
    brief_text:
        Full text of the operator-edited proposer brief. Embedded
        verbatim into the system prompt.
    current_loss_summary:
        Short human-readable summary of the previous generation's
        losses. The orchestrator does not inspect it; it goes straight
        into the user prompt.
    aux_call_llm:
        The auxiliary LLM callable from
        :class:`zicato.core.types.RuntimeConfig`. Signature
        ``(system, user, model) -> Awaitable[response]``.
    model:
        Model identifier forwarded to *aux_call_llm*. Free-form string;
        zicato does not switch on its value.
    max_retries:
        Maximum number of retry attempts after the first failure. The
        total number of LLM calls is therefore ``max_retries + 1``.
        Defaults to 2 — three calls in the worst case.
    forbidden_ids:
        Mutation-point ids the proposer MUST NOT target. Sourced from
        the proposer brief in production. Empty tuple disables the
        check.
    workspace_root:
        Optional path to the ``.zicato/`` workspace root. When supplied,
        the proposer reads the decision-telemetry analyzer's accumulated
        insights for *epoch_id* (via
        :func:`zicato.analyzer.load_latest_insights`) and prepends them
        to the user prompt under a ``## Recent telemetry insights``
        heading. When omitted, the proposer behaves exactly as before —
        callers that pre-date the analyzer surface keep working.
    meta_loop_emitter:
        Optional :class:`~zicato.telemetry.meta_loop.MetaLoopEmitter` —
        the orchestrator builds one per ``evolve_n_rounds`` invocation
        and threads it here so each proposer LLM call lands a paired
        ``proposer_call_started`` / ``proposer_call_completed`` envelope
        on the meta-loop session. When ``None`` (a standalone
        :func:`propose_experiment` call without orchestrator wiring) no
        events are emitted — the proposer's behaviour is unchanged.
        Sink failures are absorbed by the emitter; this call site
        treats the emit as best-effort.
    validate_experiment:
        Optional post-parse validation hook (see
        :data:`ExperimentValidator`). Runs *after* the response parses
        and clears the forbidden-id check, on the fully-typed
        :class:`Experiment`. When it returns a non-empty list of error
        strings the attempt is treated as a *retryable* failure — the
        errors are appended to the next user prompt as feedback and the
        proposer re-proposes, exactly as it does for a parse error.
        This is how a destructive patch (one that breaks the snapshot
        post-apply) costs a single bounded retry instead of a wasted
        tournament round. The orchestrator supplies a hook that applies
        the patch set and runs
        :func:`zicato.mutation.validator.validate_post_apply`. The
        retry budget is shared with parse-error retries — at most
        ``max_retries + 1`` LLM calls total, so the per-run wall-clock
        budget is still honoured. When omitted, no post-parse validation
        runs and the proposer behaves exactly as before.
    custom_judge_names:
        Names of the custom judges declared on the active board /
        ``per_judge_weights``, forwarded to
        :func:`~zicato.proposer.structured.parse_experiment_json` so a
        ``drift:<judge_name>`` metric in the hypothesis validates against
        a declared custom judge (not just the built-in goldfive drift
        kinds). ``None`` keeps the built-in-only behaviour.
    prior_experiments:
        The experiment-memory digest — settled cross-round history plus
        this round's in-flight siblings — as a sequence of
        :class:`~zicato.core.types.PriorExperiment`. Assembled by the
        *caller* (the orchestrator reads the index for settled history
        and injects the round's siblings); the proposer stays a pure
        prompt-assembler over its inputs and does not read the index
        itself. Threaded into :func:`render_user_prompt` so the proposer
        sees a ``## What's already been tried`` section and can avoid
        re-proposing known failures and build on known wins. Empty (the
        default) omits the section entirely — every standalone caller
        that pre-dates this surface keeps producing a byte-identical
        prompt.
    skills:
        The resolved proposer skill modules (see
        :class:`~zicato.core.types.ProposerSkill`) for the active epoch's
        proposer. Forwarded to
        :func:`~zicato.proposer.prompts.render_system_prompt`, where a
        non-empty tuple appends a ``Proposer skills`` section after the
        brief block so each skill's guidance reaches the model as operating
        procedure for the epoch. Empty (the default) appends nothing — the
        built-in default proposer carries no skills, so a caller that
        supplies none renders a byte-identical system prompt to before this
        surface existed.
    restrict_visibility:
        When ``True`` (the default-on
        :attr:`~zicato.core.types.OverfittingConfig.restrict_proposer_visibility`
        posture, threaded by the orchestrator), the user prompt aggregates
        per-entry pattern identities to counts/rates and coarsens
        experiment-memory Δscalar to buckets (OVERFITTING.md §11). ``False``
        (the default here so standalone callers are unaffected) renders the
        verbatim prompt, byte-for-byte as before this lever existed.
    failure_profile:
        Optional pre-rendered, train-slice-only, BUCKETED outcome-marginal
        block (Capability 2 of issue #18). When non-empty, a
        ``## Failure-mode profile`` section is spliced into the user prompt
        so the proposer can target *why* answers are wrong, not just *that* a
        scalar moved. The string is already board-anonymized + banded by its
        renderer (:func:`~zicato.proposer.prompts.render_failure_mode_profile`);
        this engine only forwards it. Empty (the default) omits the section,
        so a caller that supplies no profile renders a byte-identical prompt
        to before this surface existed.
    process_exemplars:
        Optional pre-rendered, train-slice-only, REDACTED process-exemplar
        block (the opt-in ``proposer_quality.process_exemplars`` channel —
        ``docs/design/PROCESS-EXEMPLARS.md``). When non-empty, a
        ``## Process exemplars`` section is spliced into the user prompt
        directly after the failure-mode profile so the proposer can see HOW
        a detected failure unfolds — never WHICH entry it unfolded on. The
        string is already mechanically redacted by its extractor + renderer
        (:func:`~zicato.analyzer.process_exemplars.extract_process_exemplars`
        / :func:`~zicato.proposer.prompts.render_process_exemplars`); this
        engine only forwards it. Empty (the default) omits the section, so
        a caller that supplies no exemplars renders a byte-identical prompt
        to before this surface existed.
    genealogy:
        Optional sampled genealogy items (the opt-in
        ``proposer_quality.genealogy`` channel — ``docs/design/PROPOSER.md``
        §2.7). Forwarded to :func:`~zicato.proposer.prompts.render_user_prompt`,
        which renders them via
        :func:`~zicato.proposer.prompts.render_genealogy_block` and splices a
        ``## Candidate genealogy`` section directly above the experiment-memory
        block so the proposer can extend a promoted line or re-frame a rejected
        one (in-context evolution). Each item is already banded + capped by its
        sampler (:func:`~zicato.proposer.genealogy.sample_genealogy`) — no
        entry ids, no per-entry results, no exact deltas; this engine only
        forwards them. Empty (the default) omits the section, byte-identical to
        before this surface existed.
    calibration:
        Optional per-reign prediction-calibration summary (the opt-in
        ``proposer_quality.calibration_feedback`` channel —
        ``docs/design/PROPOSER.md`` §2.8). Forwarded to
        :func:`~zicato.proposer.prompts.render_user_prompt`, which renders it
        via :func:`~zicato.proposer.prompts.render_calibration_block` and
        splices a ``## Prediction calibration`` section above the
        experiment-memory block so the proposer sees how its own past
        predictions landed. Already banded + reduced to hit/miss verdicts +
        aggregate counts by its sampler
        (:func:`~zicato.proposer.calibration.sample_calibration`) — no entry
        ids, no per-entry results, no exact deltas; this engine only forwards
        it. ``None`` (the default) omits the section, byte-identical to before
        this surface existed.

    Returns
    -------
    Experiment
        With outcome left as ``None`` (the tournament fills it in
        after the run).

    sample_hint:
        Optional per-sample edit-class steering line (the best-of-N slate
        diversifier — :data:`zicato.proposer.best_of_n.EDIT_CLASS_HINTS`).
        Threaded verbatim into :func:`render_user_prompt`, which prepends an
        ``## Edit-class hint (this sample)`` section when non-empty. A static
        instruction string carrying no board identity, so the restricted-
        visibility envelope is untouched. Empty (the default) renders a
        byte-identical prompt.

    mutation_track_records:
        Optional per-mutation-point track records (the fertility map —
        :func:`zicato.index.query.mutation_point_track_record`), assembled
        by the *caller* exactly like ``prior_experiments`` (the orchestrator
        reads the index best-effort; the proposer stays a pure
        prompt-assembler). Threaded into :func:`render_user_prompt`, which
        annotates each manifest entry that has a record with one compact,
        BANDED advisory line — aggregates only, labelled "experiments
        touching this point" (never causal). ``None`` (the default) renders
        a byte-identical manifest.

    revise_feedback:
        Optional SEED for the repair-feedback loop's first attempt — the
        best-of-N screen-informed revise channel (WS-R). When non-empty,
        the FIRST attempt already renders the repair section with this
        string in the ``feedback`` slot, exactly as a retry after a
        validation failure would; subsequent retries overwrite it with
        their own concrete errors as usual. The wrapper stamps only the
        screen's COUNTS-ONLY veto summary here (never an entry id), so
        the restricted-visibility envelope is untouched. Empty (the
        default) seeds nothing — every existing caller renders a
        byte-identical first prompt.

    slot_index:
        Optional best-of-N slate coordinate, recorded on this call's
        durable input capture (:mod:`zicato.proposer.input_capture`) so
        the concurrently-written records of one round's slate can be told
        apart. It reaches no renderer — the prompt is byte-identical with
        or without it. ``None`` (the default) is a call outside a slate.

    Raises
    ------
    ProposerError
        When the proposer fails to emit a schema-valid response after
        ``max_retries`` retries, or when every attempt produces an
        experiment that ``validate_experiment`` rejects. The exception
        carries the per-attempt error messages for diagnostics.
    """

    mutations_list = list(mutations)
    patterns_list = list(patterns)
    mutations_by_id = {mp.id: mp for mp in mutations_list}

    system_prompt = render_system_prompt(brief_text, skills)

    # Lazy import: keeps :mod:`zicato.proposer.proposer` independent of
    # the analyzer module so the proposer is importable even when the
    # analyzer's siblings haven't been installed. The function returns
    # the empty string if no insights exist, which is the sentinel for
    # "skip the insights block entirely".
    insights_block = ""
    if workspace_root is not None:
        from zicato.analyzer import load_latest_insights  # noqa: PLC0415

        insights_block = load_latest_insights(workspace_root, epoch_id)

    # Loop-invariant: the experiment-memory digest is rendered into every
    # retry attempt unchanged. Materialise once so a generator caller is
    # not exhausted on the first attempt.
    prior_experiments_list = list(prior_experiments)

    # The revise channel seeds the FIRST attempt's feedback (empty for every
    # non-revise call, rendering a byte-identical prompt); retries then
    # overwrite it with their own concrete errors exactly as before.
    feedback = revise_feedback
    # Repair-turn carriers. Each failed attempt that produced a response
    # populates these so the NEXT attempt's prompt can echo the prior raw
    # output back and target the empty-vs-malformed failure mode. An
    # attempt that produced no response (timeout / opaque LLM error)
    # clears them so a stale prior output is never shown.
    feedback_prior_output = ""
    feedback_was_empty = False
    attempt_errors: list[str] = []

    total_attempts = max_retries + 1
    for attempt in range(total_attempts):
        user_prompt = render_user_prompt(
            current_loss_summary=current_loss_summary,
            patterns=patterns_list,
            mutations=mutations_list,
            feedback=feedback,
            feedback_prior_output=feedback_prior_output,
            feedback_was_empty=feedback_was_empty,
            insights=insights_block,
            prior_experiments=prior_experiments_list,
            restrict_visibility=restrict_visibility,
            custom_judge_names=custom_judge_names or frozenset(),
            failure_profile=failure_profile,
            process_exemplars=process_exemplars,
            genealogy=genealogy,
            calibration=calibration,
            sample_hint=sample_hint,
            mutation_track_records=mutation_track_records,
        )
        # Durable input capture, BEFORE the call: an attempt that times out
        # is precisely the one whose prompt is worth reading, and each retry
        # renders its own repair feedback, so every attempt lands its own
        # record. Best-effort — it cannot fail the round.
        capture_proposer_input(
            workspace_root=workspace_root,
            epoch_id=epoch_id,
            role=ROLE_PROPOSAL,
            system=system_prompt,
            user=user_prompt,
            model=model,
            parent_generation_id=parent_generation_id,
            new_generation_id=new_generation_id,
            attempt=attempt,
            slot=slot_index,
        )
        # Meta-loop bookends: one paired ``proposer_call_started`` /
        # ``proposer_call_completed`` per attempt. ``invocation_id`` is
        # threaded through so a sink can correlate the pair. Every emit
        # is best-effort — the emitter swallows sink failures, but we
        # additionally guard the emit calls so a misconfigured emitter
        # cannot regress the proposer.
        invocation_id: str | None = None
        started_at = time.monotonic()
        if meta_loop_emitter is not None:
            try:
                invocation_id = await meta_loop_emitter.proposer_started(
                    model=model,
                    epoch_id=epoch_id,
                    parent_generation_id=parent_generation_id,
                    new_generation_id=new_generation_id,
                )
            except Exception:  # noqa: BLE001 — additive telemetry only
                invocation_id = None
        try:
            response_text = await asyncio.wait_for(
                aux_call_llm(system_prompt, user_prompt, model),
                timeout=aux_call_timeout_s(),
            )
        except TimeoutError:
            err = f"auxiliary LLM call timed out after {aux_call_timeout_s():.1f}s"
            attempt_errors.append(err)
            feedback = err
            # No response was produced — clear the repair carriers so the
            # next attempt does not echo back a stale prior output.
            feedback_prior_output = ""
            feedback_was_empty = False
            if meta_loop_emitter is not None and invocation_id is not None:
                try:
                    await meta_loop_emitter.proposer_completed(
                        invocation_id=invocation_id,
                        latency_s=time.monotonic() - started_at,
                        response_chars=0,
                        outcome="timeout",
                    )
                except Exception:  # noqa: BLE001 — additive telemetry only
                    pass
            continue
        except Exception as exc:  # noqa: BLE001 — opaque LLM errors are common
            err = f"auxiliary LLM call raised {type(exc).__name__}: {exc}"
            attempt_errors.append(err)
            feedback = err
            # No response was produced — clear the repair carriers.
            feedback_prior_output = ""
            feedback_was_empty = False
            if meta_loop_emitter is not None and invocation_id is not None:
                try:
                    await meta_loop_emitter.proposer_completed(
                        invocation_id=invocation_id,
                        latency_s=time.monotonic() - started_at,
                        response_chars=0,
                        outcome=f"error:{type(exc).__name__}",
                    )
                except Exception:  # noqa: BLE001 — additive telemetry only
                    pass
            continue

        if meta_loop_emitter is not None and invocation_id is not None:
            try:
                await meta_loop_emitter.proposer_completed(
                    invocation_id=invocation_id,
                    latency_s=time.monotonic() - started_at,
                    response_chars=len(response_text or ""),
                    outcome="completed",
                )
            except Exception:  # noqa: BLE001 — additive telemetry only
                pass

        try:
            experiment = parse_experiment_json(
                response_text,
                epoch_id=epoch_id,
                parent_gen=parent_generation_id,
                new_gen=new_generation_id,
                mutations_by_id=mutations_by_id,
                custom_judge_names=custom_judge_names,
            )
        except ExperimentParseError as exc:
            err = str(exc)
            attempt_errors.append(err)
            feedback = err
            # Make the next attempt a genuine repair turn: echo the prior
            # raw output back (so the model sees the stray <think> block /
            # prose / fence it actually produced) and flag the empty case
            # so the prompt can target "skip all reasoning, emit now".
            feedback_prior_output = response_text or ""
            feedback_was_empty = not (response_text or "").strip()
            continue

        if forbidden_ids:
            violations = enforce_forbidden(list(experiment.patches), forbidden_ids)
            if violations:
                err = "patches violate proposer-brief forbidden-edits list: " + "; ".join(
                    violations
                )
                attempt_errors.append(err)
                feedback = err
                # The response was well-formed JSON — this is a content
                # (not shape) failure, so no prior-output echo / empty
                # framing; the feedback string already names the offending
                # ids. Clear the carriers so a stale parse-failure echo
                # cannot leak in.
                feedback_prior_output = ""
                feedback_was_empty = False
                continue

        # Post-parse validation hook — the experiment is well-formed and
        # forbidden-id-clean, but its patches may still break the child
        # snapshot once applied (a dropped import, a syntax error, a
        # vanished marker). Treat a non-empty finding list exactly like
        # a parse error: feed the concrete validator strings back and
        # retry, within the same bounded budget.
        if validate_experiment is not None:
            try:
                post_apply_errors = await validate_experiment(experiment)
            except PostApplyValidationError as exc:
                post_apply_errors = exc.errors
            if post_apply_errors:
                err = "patches failed post-apply validation: " + "; ".join(post_apply_errors)
                attempt_errors.append(err)
                feedback = err
                # Well-formed JSON whose patches broke the snapshot — a
                # content failure, not a shape one. The validator findings
                # in the feedback string are the actionable signal; no
                # prior-output echo / empty framing.
                feedback_prior_output = ""
                feedback_was_empty = False
                continue

        return experiment

    raise ProposerError(attempt_errors)


__all__ = [
    "ExperimentValidator",
    "ProposerError",
    "propose_experiment",
]
