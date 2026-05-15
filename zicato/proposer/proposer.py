"""Orchestration entry point for the structured proposer.

The function in this module:

1. Builds the system + user prompts from the rubric, patterns, and
   mutation manifest.
2. Calls the auxiliary LLM (zicato never invokes the harness LLM here
   — see :class:`zicato.core.types.RuntimeConfig` for the two-callable
   invariant).
3. Parses the response into a typed :class:`Experiment` via
   :func:`zicato.proposer.structured.parse_experiment_json`.
4. Enforces the rubric's forbidden-id list against the emitted patches.
5. On parse failure or forbidden-id violation, appends the error to the
   next user prompt and retries up to ``max_retries`` times.
6. Raises :class:`ProposerError` after all retries are exhausted.

The orchestrator does NOT write the resulting :class:`Experiment` to
disk — that is the CLI's job (see ``zicato.cli.commands.propose``).
Keeping the I/O at the CLI layer means tests can drive the orchestrator
with stub callables and assert on the returned dataclass without
tmpdir bookkeeping.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable

from zicato.core.types import Experiment, MutationPoint, Pattern
from zicato.proposer.prompts import render_system_prompt, render_user_prompt
from zicato.proposer.rubric import enforce_forbidden
from zicato.proposer.structured import (
    ExperimentParseError,
    parse_experiment_json,
)


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
        joined = "\n".join(
            f"  attempt {i + 1}: {msg}" for i, msg in enumerate(self.attempts)
        )
        super().__init__(
            f"proposer failed after {len(self.attempts)} attempt(s):\n{joined}"
        )


async def propose_experiment(
    *,
    epoch_id: str,
    parent_generation_id: str,
    new_generation_id: str,
    patterns: Iterable[Pattern],
    mutations: Iterable[MutationPoint],
    rubric_text: str,
    current_loss_summary: str,
    aux_call_llm: Callable[[str, str, str], Awaitable[str]],
    model: str = "",
    max_retries: int = 2,
    forbidden_ids: tuple[str, ...] = (),
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
    rubric_text:
        Full text of the operator-edited rubric. Embedded verbatim into
        the system prompt.
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
        the rubric in production. Empty tuple disables the check.

    Returns
    -------
    Experiment
        With outcome left as ``None`` (the tournament fills it in
        after the run).

    Raises
    ------
    ProposerError
        When the proposer fails to emit a schema-valid response after
        ``max_retries`` retries. The exception carries the per-attempt
        error messages for diagnostics.
    """

    mutations_list = list(mutations)
    patterns_list = list(patterns)
    mutations_by_id = {mp.id: mp for mp in mutations_list}

    system_prompt = render_system_prompt(rubric_text)
    feedback = ""
    attempt_errors: list[str] = []

    total_attempts = max_retries + 1
    for _attempt in range(total_attempts):
        user_prompt = render_user_prompt(
            current_loss_summary=current_loss_summary,
            patterns=patterns_list,
            mutations=mutations_list,
            feedback=feedback,
        )
        try:
            response_text = await aux_call_llm(system_prompt, user_prompt, model)
        except Exception as exc:  # noqa: BLE001 — opaque LLM errors are common
            err = f"auxiliary LLM call raised {type(exc).__name__}: {exc}"
            attempt_errors.append(err)
            feedback = err
            continue

        try:
            experiment = parse_experiment_json(
                response_text,
                epoch_id=epoch_id,
                parent_gen=parent_generation_id,
                new_gen=new_generation_id,
                mutations_by_id=mutations_by_id,
            )
        except ExperimentParseError as exc:
            err = str(exc)
            attempt_errors.append(err)
            feedback = err
            continue

        if forbidden_ids:
            violations = enforce_forbidden(list(experiment.patches), forbidden_ids)
            if violations:
                err = (
                    "patches violate rubric forbidden-edits list: "
                    + "; ".join(violations)
                )
                attempt_errors.append(err)
                feedback = err
                continue

        return experiment

    raise ProposerError(attempt_errors)


__all__ = [
    "ProposerError",
    "propose_experiment",
]
