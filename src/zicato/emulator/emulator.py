"""Collusion-proof multi-turn user-emulator driver.

The driver in this module runs one ``multi_turn_emulated`` board entry
end-to-end. It calls a caller-supplied ``run_harness_turn`` closure to
drive the inner agent and uses :attr:`RuntimeConfig.auxiliary_call_llm`
to drive the user emulator. The two callables are checked for identity
inequality at the start of :meth:`EmulatedMultiTurnDriver.drive` via
:func:`zicato.core.workspace.assert_distinct_callables`; sharing a
callable raises :class:`EmulationCollusionError`.

Decoupling note: this module does NOT import any goldfive adapter
shape. The harness turn-runner is injected by the caller (the ADK
adapter or any other adapter) as an ``async (user_msg) -> agent_output``
closure. That keeps the emulator pluggable across harness backends.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from zicato.aux_timeout import aux_call_timeout_s
from zicato.core.types import BoardEntry, RunResult, RuntimeConfig
from zicato.core.workspace import assert_distinct_callables
from zicato.emulator.answer_leak import check_answer_leak
from zicato.emulator.audit import EmulatorTurnAudit, audit_turn, emit_audit_span
from zicato.emulator.sealed import (
    END_TOKEN,
    build_emulator_system_prompt,
    build_emulator_user_prompt,
)

_log = logging.getLogger(__name__)

#: Default model string forwarded to ``auxiliary_call_llm``. Concrete
#: backends interpret this; zicato never inspects it. The driver
#: intentionally does not expose a knob for this — operators wire the
#: model identity inside their ``auxiliary_call_llm`` closure.
_DEFAULT_EMULATOR_MODEL = "zicato-emulator-default"


class EmulationCollusionError(RuntimeError):
    """The runtime config's two LLM callables are not distinct.

    Raised by :meth:`EmulatedMultiTurnDriver.drive` when
    ``harness_call_llm`` and ``auxiliary_call_llm`` share identity.
    The driver refuses to start the run; this is a hard error rather than a
    warning, because shared callables risk collusion between the
    emulator and the inner harness.
    """


class EmulatedMultiTurnDriver:
    """Run one ``multi_turn_emulated`` board entry to completion.

    The driver alternates emulator and harness turns until either the
    emulator emits :data:`zicato.emulator.sealed.END_TOKEN` on a line
    by itself, the conversation hits :attr:`BoardEntry.max_turns`, or
    the answer-leak heuristic fires (aborting the run).

    Parameters
    ----------
    sink_emit_fn:
        Optional callable for emitting audit spans. Either a goldfive-
        shaped sink object exposing an ``emit(event)`` method, or
        ``None`` to keep audits in memory only. When ``None``, audits
        are still produced and exposed via :attr:`audits` after a
        :meth:`drive` call completes.
    """

    def __init__(self, sink_emit_fn: Any = None) -> None:
        self._sink = sink_emit_fn
        self._audits: list[EmulatorTurnAudit] = []

    @property
    def audits(self) -> tuple[EmulatorTurnAudit, ...]:
        """Audits produced by the most recent :meth:`drive` call."""
        return tuple(self._audits)

    async def drive(
        self,
        run_harness_turn: Callable[[str], Awaitable[str]],
        entry: BoardEntry,
        config: RuntimeConfig,
    ) -> RunResult:
        """Drive one multi-turn-emulated conversation.

        Parameters
        ----------
        run_harness_turn:
            Async closure ``(user_msg) -> agent_output`` that runs one
            inner-harness turn and returns the agent's user-facing
            output for that turn. Caller (the harness adapter) owns
            session state, tool wiring, and goldfive event emission for
            inside-the-harness work.
        entry:
            The board entry being executed. Must be of kind
            ``"multi_turn_emulated"`` with a populated
            :attr:`BoardEntry.user_persona` and
            :attr:`BoardEntry.max_turns`.
        config:
            The runtime config carrying ``auxiliary_call_llm``. The
            two-callable invariant is checked at the top of this
            method via :func:`assert_distinct_callables`.

        Returns
        -------
        RunResult
            Transcript-shape result. The transcript is the agent's
            user-facing turns only (the emulator's user turns are not
            in :attr:`RunResult.transcript` by design — the runner
            keeps them separately for reducer use).

        Raises
        ------
        EmulationCollusionError
            If the two configured LLM callables share identity.
        ValueError
            If the entry is not a ``"multi_turn_emulated"`` entry or
            is missing required fields.
        """
        # Two-callable hard check. Identity equality only — wrapping the
        # same client in two distinct closures is the operator's
        # responsibility and passes this check.
        try:
            assert_distinct_callables(
                config.harness_call_llm, config.effective_user_emulator_call_llm()
            )
        except RuntimeError as exc:
            raise EmulationCollusionError(str(exc)) from exc

        if entry.kind != "multi_turn_emulated":
            raise ValueError(
                f"EmulatedMultiTurnDriver requires kind='multi_turn_emulated'; "
                f"got {entry.kind!r}"
            )
        if entry.user_persona is None:
            raise ValueError(
                f"BoardEntry {entry.id!r}: multi_turn_emulated requires " "'user_persona'"
            )
        if entry.max_turns is None or entry.max_turns <= 0:
            raise ValueError(
                f"BoardEntry {entry.id!r}: multi_turn_emulated requires " "'max_turns' > 0"
            )

        self._audits = []
        persona = entry.user_persona
        max_turns = entry.max_turns
        system_prompt = build_emulator_system_prompt(persona)

        run_id = uuid.uuid4().hex
        agent_transcript: list[str] = []
        started_ns = time.monotonic_ns()
        aborted = False
        abort_reason = ""

        for _turn_index in range(max_turns):
            user_prompt = build_emulator_user_prompt(tuple(agent_transcript))
            try:
                emulator_output = await asyncio.wait_for(
                    config.effective_user_emulator_call_llm()(
                        system_prompt, user_prompt, _DEFAULT_EMULATOR_MODEL
                    ),
                    timeout=aux_call_timeout_s(),
                )
            except TimeoutError:
                aborted = True
                abort_reason = "emulator_timeout"
                _log.warning(
                    "run %s aborted: emulator turn timed out after %.1fs (entry=%s)",
                    run_id,
                    aux_call_timeout_s(),
                    entry.id,
                )
                break

            audit = audit_turn(persona, tuple(agent_transcript), emulator_output)
            self._audits.append(audit)
            emit_audit_span(self._sink, audit)

            # Stop signal: <<END>> on a line by itself.
            stripped_lines = [line.strip() for line in emulator_output.splitlines()]
            if END_TOKEN in stripped_lines:
                break

            # Answer-leakage heuristic. Abort the run on detection.
            leak = check_answer_leak(emulator_output)
            if leak is not None:
                aborted = True
                abort_reason = "emulator_leak_detected"
                _log.warning("run %s aborted: %s (entry=%s)", run_id, leak, entry.id)
                break

            # Forward the clean user message to the inner harness.
            agent_output = await run_harness_turn(emulator_output)
            agent_transcript.append(agent_output)

        runtime_ms = (time.monotonic_ns() - started_ns) // 1_000_000
        final_output = agent_transcript[-1] if agent_transcript else ""
        return RunResult(
            run_id=run_id,
            entry_id=entry.id,
            final_output=final_output,
            transcript=tuple(agent_transcript),
            runtime_ms=int(runtime_ms),
            aborted=aborted,
            abort_reason=abort_reason,
        )


__all__ = [
    "EmulationCollusionError",
    "EmulatedMultiTurnDriver",
]
