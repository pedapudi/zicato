"""Meta-loop goldfive event emitter.

The orchestrator's evolve loop is, conceptually, its OWN goldfive session
— the proposer's evaluation LLM call and any in-process LLM "process
judges" (today: the decision-telemetry analyzer's insight call) are
events worth recording on the same harmonograf timeline that workers
already feed.

Before this module, those calls were opaque ``aux_call_llm`` invocations
that returned raw text. There was no goldfive envelope, no JSONL trace,
no harmonograf session — the meta-loop was a black hole between
worker-emitted sessions.

This module bridges that gap. It owns one ``MetaLoopEmitter`` per
``evolve_n_rounds`` invocation, scoped to a single stable session id
(via :func:`zicato.telemetry.harmonograf_supervisor.meta_loop_session_id`).
The emitter holds:

* the JSONL sink (always attached when goldfive is installed) writing
  to ``<workspace>/.zicato/runtime/meta_loop_events.jsonl``;
* the optional harmonograf sink (attached when a non-empty URL is in
  scope — auto-launched or operator-pinned);
* a monotonic sequence counter that goldfive's ``Event.sequence`` field
  requires;
* a stable ``run_id`` (one evolve invocation == one run on the
  meta-loop timeline);
* the resolved ``session_id``.

It exposes two coarse-grained emit methods — ``proposer_started`` /
``proposer_completed`` for the evaluation proposer call, and
``judge_invoked`` / ``judgment_emitted`` for in-process judges — and
:meth:`close` to flush every sink at evolve teardown. The methods reuse
goldfive's canonical envelopes (``AgentInvocationStarted`` /
``AgentInvocationCompleted`` for paired call/result, and
``JudgementEmitted`` for typed verdicts) so the wire shape is identical
to what workers already emit; the dashboard / reducer needs no
meta-loop-specific code path.

Failure isolation is non-negotiable: a sink that raises MUST NOT crash
the proposer or the analyzer. Every emit is wrapped in a per-sink
``try/except`` so the meta-loop telemetry is always additive, never
load-bearing.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Ambient span context (the lifelines coverage — HARMONOGRAF.md §7).
# ---------------------------------------------------------------------------
#
# The proposer / judge emits above are threaded call-site to call-site as an
# explicit ``meta_loop_emitter`` argument. The structural spans (round /
# phase / matchup / worker / slate slot) instead ride two module-level
# ``ContextVar``s so a deep call site — the tournament runner, the board-unit
# scheduler, the best-of-N slate — can open a span WITHOUT every intervening
# signature growing a parameter:
#
# * ``_current_emitter`` carries the evolve's one emitter. The orchestrator
#   sets it once at the top of ``evolve_n_rounds`` (:func:`set_current_emitter`)
#   and resets it at teardown; every descendant asyncio task inherits it
#   because ``asyncio`` copies the context when a task is created. Absent
#   (``None``) → :func:`meta_span` is a no-op, the same null path the proposer
#   emits take when ``meta_loop_emitter is None``.
# * ``_current_span_id`` carries the ENCLOSING span's invocation id. A new
#   span reads it as its ``parent_invocation_id`` (harmonograf infers the span
#   tree from that field — see ``harmonograf_server/ingest.py``
#   ``_on_agent_invocation_started``), then sets itself as the current id for
#   the body's duration. Because a task copies the context at creation, a
#   ``gather``-fanned child (a board unit, a slate slot) inherits its parent
#   span automatically — the fan-out renders as nested, overlapping lifelines
#   with no explicit parent bookkeeping.
#
# Both vars are bounded by construction: the span id is a single string that a
# context manager sets-and-resets, so there is no per-round-growing registry.
_current_emitter: contextvars.ContextVar[MetaLoopEmitter | None] = contextvars.ContextVar(
    "zicato_meta_loop_emitter", default=None
)
_current_span_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "zicato_meta_loop_span", default=""
)


# Envelope kinds used by the dict fallback path (when goldfive's proto
# stubs are unavailable). The strings line up with the goldfive event
# oneof field names so a downstream reducer that recognises one shape
# recognises the other.
_KIND_PROPOSER_STARTED = "proposer_call_started"
_KIND_PROPOSER_COMPLETED = "proposer_call_completed"
_KIND_JUDGE_INVOKED = "judge_invoked"
_KIND_JUDGEMENT_EMITTED = "judgement_emitted"

# The structural span taxonomy (HARMONOGRAF.md §7). ``kind`` selects the
# harmonograf lifeline lane (agent_name ``zicato.<kind>``) so every span of a
# kind shares one lane; the per-instance label rides ``task_id``.
SPAN_ROUND = "round"
SPAN_PHASE = "phase"
SPAN_MATCHUP = "matchup"
SPAN_WORKER = "worker"
SPAN_SLOT = "slot"


class _SpanHandle:
    """Mutable handle yielded by :meth:`MetaLoopEmitter.span`.

    Carries the span's ``invocation_id`` (opaque, for correlation) and a
    ``meta`` dict the body can enrich with values only known mid-span — a
    worker's goldfive session id, a matchup's resolved decision — via
    :meth:`set`. Whatever is accumulated is serialised onto the COMPLETED
    envelope's ``summary`` when the span closes. Metadata is ids / names /
    timings only; never board content and never scores.
    """

    __slots__ = ("invocation_id", "meta")

    def __init__(self, invocation_id: str) -> None:
        self.invocation_id = invocation_id
        self.meta: dict[str, Any] = {}

    def set(self, **fields: Any) -> None:
        """Merge late-known span metadata (ids / names / timings only)."""
        self.meta.update(fields)


class _NullSpanHandle:
    """No-op handle returned on the null path (no emitter / no sinks).

    Supports the same ``.set(...)`` surface so a call site is one shape
    whether or not telemetry is live. It accumulates nothing.
    """

    __slots__ = ()
    invocation_id = ""

    def set(self, **fields: Any) -> None:
        return None


_NULL_SPAN_HANDLE = _NullSpanHandle()


class MetaLoopEmitter:
    """Fan goldfive events for the meta-loop out to a sink list.

    Constructed once per ``evolve_n_rounds`` invocation and threaded into
    the proposer, judge and analyzer call sites, which would otherwise call
    ``aux_call_llm`` with no observability at all.

    A no-op emitter — one whose ``sinks`` list is empty — is a perfectly
    legal value; the call sites use ``emitter is not None`` only to
    decide whether to record any events at all, and the methods on an
    empty-sink emitter are still safe (they do the bookkeeping but emit
    nothing). Callers that pass ``None`` skip the methods entirely.

    Attributes
    ----------
    run_id:
        Stable id for this evolve invocation. Empty string means
        "no goldfive run scope" — used only by the empty default.
    session_id:
        The :func:`meta_loop_session_id` for this evolve. Empty allowed.
    sinks:
        List of goldfive ``EventSink``-shaped objects (i.e. anything
        with an async ``emit(event) -> None`` and ``close() -> None``).
    """

    def __init__(
        self,
        *,
        run_id: str,
        session_id: str,
        sinks: list[Any] | None = None,
    ) -> None:
        self.run_id = run_id
        self.session_id = session_id
        self._sinks: list[Any] = list(sinks or [])
        self._sequence = 0
        self._seq_lock = asyncio.Lock()
        # A proposer / judge STARTED payload, stashed by invocation_id, so it
        # can ride the paired COMPLETED envelope's ``summary`` (the
        # structural-span convention). ``parent_invocation_id`` on the started
        # half now carries the true ambient span rather than this payload — see
        # :meth:`_emit_paired_started` / :meth:`_emit_paired_completed`.
        # Bounded: one entry per open pair, popped when the pair completes.
        self._pending_payloads: dict[str, dict[str, Any]] = {}

    @property
    def sinks(self) -> list[Any]:
        """Read-only view of the configured sinks. Mutating it is undefined."""
        return list(self._sinks)

    async def _next_seq(self) -> int:
        async with self._seq_lock:
            self._sequence += 1
            return self._sequence

    async def proposer_started(
        self,
        *,
        model: str,
        epoch_id: str,
        parent_generation_id: str,
        new_generation_id: str,
    ) -> str:
        """Emit a proposer-call-started envelope; return the invocation_id.

        The invocation id is opaque, generated here, and returned so the
        paired :meth:`proposer_completed` (or :meth:`proposer_failed`)
        can use it. It is also stamped onto the envelope so a sink that
        tracks open invocations can correlate the pair.
        """
        invocation_id = f"proposer-{uuid.uuid4().hex[:12]}"
        await self._emit_paired_started(
            kind=_KIND_PROPOSER_STARTED,
            invocation_id=invocation_id,
            agent_name="zicato.proposer",
            payload={
                "model": str(model or ""),
                "epoch_id": str(epoch_id or ""),
                "parent_generation_id": str(parent_generation_id or ""),
                "new_generation_id": str(new_generation_id or ""),
            },
        )
        return invocation_id

    async def proposer_completed(
        self,
        *,
        invocation_id: str,
        latency_s: float,
        response_chars: int = 0,
        outcome: str = "completed",
    ) -> None:
        """Emit a proposer-call-completed envelope paired with ``invocation_id``.

        ``outcome`` is one of ``"completed"`` (success), ``"timeout"`` /
        ``"error:<ExcName>"`` (failure). Aggregators that want a
        latency histogram read ``latency_s``; transcript renderers read
        ``response_chars`` to size the body.
        """
        await self._emit_paired_completed(
            kind=_KIND_PROPOSER_COMPLETED,
            invocation_id=invocation_id,
            agent_name="zicato.proposer",
            payload={
                "latency_s": float(latency_s),
                "response_chars": int(response_chars),
                "outcome": str(outcome or "completed"),
            },
        )

    async def judge_invoked(
        self,
        *,
        judge_name: str,
        kind: str = "process",
    ) -> str:
        """Emit a judge-invocation-started envelope; return the invocation_id.

        ``kind`` mirrors a goldfive convention: ``"process"`` for in-loop
        judges (e.g. the decision-telemetry analyzer treated as a
        meta-loop process judge) and ``"rubric"`` for board-entry rubric
        judges, although the meta-loop currently only emits ``"process"``
        — boards run inside the worker, which already has its own
        :class:`JudgementEmitted` plumbing.
        """
        invocation_id = f"judge-{uuid.uuid4().hex[:12]}"
        await self._emit_paired_started(
            kind=_KIND_JUDGE_INVOKED,
            invocation_id=invocation_id,
            agent_name=f"zicato.judge:{judge_name}",
            payload={
                "judge_name": str(judge_name or ""),
                "kind": str(kind or "process"),
            },
        )
        return invocation_id

    async def judgment_emitted(
        self,
        *,
        invocation_id: str,
        judge_name: str,
        verdict_kind: str = "drift",
        score: float | None = None,
        detail: str = "",
        latency_s: float = 0.0,
    ) -> None:
        """Emit a judgement-emitted envelope paired with ``invocation_id``.

        ``verdict_kind`` is one of ``"drift" / "rubric" / "boolean" /
        "numeric"`` to mirror :class:`goldfive.JudgementEmitted`'s
        taxonomy. ``score`` may be a normalised rubric score or a raw
        metric depending on ``verdict_kind`` — the dashboard interprets
        it per kind.
        """
        await self._emit_paired_completed(
            kind=_KIND_JUDGEMENT_EMITTED,
            invocation_id=invocation_id,
            agent_name=f"zicato.judge:{judge_name}",
            payload={
                "judge_name": str(judge_name or ""),
                "verdict_kind": str(verdict_kind or "drift"),
                "score": None if score is None else float(score),
                "detail": str(detail or ""),
                "latency_s": float(latency_s),
            },
        )

    @asynccontextmanager
    async def span(
        self,
        name: str,
        *,
        kind: str,
        meta: dict[str, Any] | None = None,
    ) -> AsyncIterator[_SpanHandle]:
        """Bracket a unit of work with a paired goldfive span.

        The single structural-emit surface: ``round`` / ``phase`` /
        ``matchup`` / ``worker`` / ``slot`` all go through here (see the
        ``SPAN_*`` constants). Usage::

            async with emitter.span("propose", kind=SPAN_PHASE) as sp:
                ...
                sp.set(generation_id=next_id)

        The started envelope is emitted on ``__aenter__`` carrying the
        enclosing span (:data:`_current_span_id`) as its
        ``parent_invocation_id`` so harmonograf nests it; the completed
        envelope is emitted on ``__aexit__`` carrying the elapsed wall time
        and an ``outcome`` marker. The pairing discipline is absolute: the
        completed half fires in a ``finally``, so an exception, a
        ``CancelledError``, or a crash mid-body still closes the span (with
        ``outcome="error:<Exc>"`` / ``"cancelled"``). Emits are best-effort
        and swallow every sink failure (:meth:`_fan_emit`); the completed
        emit is additionally shielded so a task cancellation cannot drop it.
        """
        invocation_id = f"{kind}-{uuid.uuid4().hex[:12]}"
        parent = _current_span_id.get()
        await self._emit_span(
            started=True,
            kind=kind,
            name=name,
            invocation_id=invocation_id,
            parent=parent,
            meta=None,
        )
        token = _current_span_id.set(invocation_id)
        handle = _SpanHandle(invocation_id)
        started_at = time.monotonic()
        outcome = "completed"
        try:
            yield handle
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except BaseException as exc:  # noqa: BLE001 — mark then re-raise; body owns it
            outcome = f"error:{type(exc).__name__}"
            raise
        finally:
            _current_span_id.reset(token)
            final_meta = {
                **handle.meta,
                "outcome": outcome,
                "latency_s": round(time.monotonic() - started_at, 6),
            }
            # Shield the completed emit so a cancellation propagating through
            # this ``finally`` cannot drop the closing envelope; swallow any
            # error (including the shield's own re-raised CancelledError) so
            # the body's real exception is never masked by telemetry.
            try:
                await asyncio.shield(
                    self._emit_span(
                        started=False,
                        kind=kind,
                        name=name,
                        invocation_id=invocation_id,
                        parent=parent,
                        meta=final_meta,
                    )
                )
            except BaseException:  # noqa: BLE001 — additive, never load-bearing
                pass

    async def _emit_span(
        self,
        *,
        started: bool,
        kind: str,
        name: str,
        invocation_id: str,
        parent: str,
        meta: dict[str, Any] | None,
    ) -> None:
        seq = await self._next_seq()
        event = self._build_span_event(
            started=started,
            sequence=seq,
            agent_name=f"zicato.{kind}",
            task_id=name,
            invocation_id=invocation_id,
            parent=parent,
            meta=meta,
        )
        await self._fan_emit(event)

    def _build_span_event(
        self,
        *,
        started: bool,
        sequence: int,
        agent_name: str,
        task_id: str,
        invocation_id: str,
        parent: str,
        meta: dict[str, Any] | None,
    ) -> Any:
        """Build the canonical AgentInvocation(Started|Completed) span event.

        Unlike the proposer / judge builders — which repurpose
        ``parent_invocation_id`` to carry a JSON payload because those calls
        are top-level and never nest — a structural span uses
        ``parent_invocation_id`` for its TRUE parent so harmonograf renders
        the tree. The small metadata dict rides the completed envelope's
        ``summary`` instead. Falls back to a dict envelope when goldfive's
        proto stubs are unavailable or the build raises.
        """
        import json  # noqa: PLC0415

        try:
            if started:
                from goldfive.events import agent_invocation_started_event  # noqa: PLC0415

                return agent_invocation_started_event(
                    self.run_id,
                    sequence,
                    agent_name=agent_name,
                    task_id=task_id,
                    invocation_id=invocation_id,
                    parent_invocation_id=parent,
                    session_id=self.session_id,
                )
            from goldfive.events import agent_invocation_completed_event  # noqa: PLC0415

            return agent_invocation_completed_event(
                self.run_id,
                sequence,
                agent_name=agent_name,
                task_id=task_id,
                invocation_id=invocation_id,
                summary=json.dumps(meta or {}, sort_keys=True, default=str),
                session_id=self.session_id,
            )
        except Exception as exc:  # noqa: BLE001 — proto absent / build failed; fall back
            log.debug("meta-loop span envelope build failed (%s); dict fallback", exc)
            t = time.time_ns()
            return {
                "event_id": f"{self.run_id}:{sequence}:{uuid.uuid4().hex[:8]}",
                "run_id": self.run_id,
                "sequence": sequence,
                "session_id": self.session_id,
                "emitted_at": {"seconds": t // 1_000_000_000, "nanos": t % 1_000_000_000},
                "kind": "span_started" if started else "span_completed",
                "invocation_id": invocation_id,
                "parent_invocation_id": parent,
                "agent_name": agent_name,
                "task_id": task_id,
                "payload": dict(meta or {}),
            }

    async def close(self) -> None:
        """Flush + close every sink. Idempotent; per-sink failures absorbed."""
        for sink in list(self._sinks):
            try:
                await sink.close()
            except Exception as exc:  # noqa: BLE001 — never raise from close
                log.debug("meta-loop sink close failed: %s", exc)

    # -----------------------------------------------------------------
    # Internal: shared emit path for paired Started/Completed envelopes.
    # -----------------------------------------------------------------

    async def _emit_paired_started(
        self,
        *,
        kind: str,
        invocation_id: str,
        agent_name: str,
        payload: dict[str, Any],
    ) -> None:
        seq = await self._next_seq()
        # Nest the started event under the ENCLOSING structural span exactly
        # as :meth:`span` does — its ``parent_invocation_id`` carries the true
        # ambient parent (the proposer's propose / slot span, a judge's gate
        # span), NOT the payload. The payload is stashed so it rides the
        # COMPLETED envelope's ``summary`` (the structural-span convention).
        # Absent an ambient span (a bare propose in a test) the
        # parent is empty — the detached-root behaviour, preserved.
        parent = _current_span_id.get()
        self._pending_payloads[invocation_id] = dict(payload)
        event = self._build_started_event(
            kind=kind,
            sequence=seq,
            invocation_id=invocation_id,
            agent_name=agent_name,
            parent=parent,
        )
        await self._fan_emit(event)

    async def _emit_paired_completed(
        self,
        *,
        kind: str,
        invocation_id: str,
        agent_name: str,
        payload: dict[str, Any],
    ) -> None:
        seq = await self._next_seq()
        # Fold the paired started payload (model / gen-ids / judge_name) in
        # UNDER the completed payload so the migrated started metadata lands in
        # the completed ``summary`` — the started half now carries only its
        # ambient parent. The completed payload wins on any key collision
        # (e.g. ``judge_name``, identical anyway).
        started_payload = self._pending_payloads.pop(invocation_id, {})
        merged = {**started_payload, **payload}
        event = self._build_completed_event(
            kind=kind,
            sequence=seq,
            invocation_id=invocation_id,
            agent_name=agent_name,
            payload=merged,
        )
        await self._fan_emit(event)

    def _build_started_event(
        self,
        *,
        kind: str,
        sequence: int,
        invocation_id: str,
        agent_name: str,
        parent: str,
    ) -> Any:
        """Build a goldfive envelope for the started half, or a dict fallback.

        The proto path uses :func:`goldfive.events.agent_invocation_started_event`
        so a sink expecting the canonical envelope sees one regardless of
        whether the emitter was constructed by the orchestrator (meta
        loop) or a worker. ``parent_invocation_id`` carries the ENCLOSING
        structural span (:data:`_current_span_id`, threaded in by
        :meth:`_emit_paired_started`) so harmonograf nests the proposer /
        judge lifeline under its propose / slot / gate span instead of
        rendering it as a detached root. The started payload does NOT ride
        here — it moved to the paired COMPLETED envelope's ``summary``
        (the structural-span convention; see :meth:`_emit_paired_completed`).

        On the dict-fallback path (no proto stubs available) the parent
        rides a ``parent_invocation_id`` field and the payload is empty
        (it too moved to the completed half), with a ``kind`` discriminator
        so the reducer can switch on it. The dict shape mirrors
        :func:`goldfive.events.make_event`.
        """
        try:
            from goldfive.events import agent_invocation_started_event  # noqa: PLC0415
        except Exception:  # noqa: BLE001 — proto stubs absent; fall back
            return self._build_dict_event(
                kind=kind,
                sequence=sequence,
                invocation_id=invocation_id,
                agent_name=agent_name,
                payload={},
                parent=parent,
            )

        # The canonical AgentInvocationStarted envelope, nesting under the
        # ambient structural span via ``parent_invocation_id`` (``task_id``
        # carries the kind discriminator). A future canonical envelope for
        # ProposerCallStarted would replace this.
        try:
            event = agent_invocation_started_event(
                self.run_id,
                sequence,
                agent_name=agent_name,
                task_id=kind,
                invocation_id=invocation_id,
                parent_invocation_id=parent,
                session_id=self.session_id,
            )
            return event
        except Exception as exc:  # noqa: BLE001 — envelope build failed; fall back
            log.debug("meta-loop proto envelope build failed (%s); falling back to dict", exc)
            return self._build_dict_event(
                kind=kind,
                sequence=sequence,
                invocation_id=invocation_id,
                agent_name=agent_name,
                payload={},
                parent=parent,
            )

    def _build_completed_event(
        self,
        *,
        kind: str,
        sequence: int,
        invocation_id: str,
        agent_name: str,
        payload: dict[str, Any],
    ) -> Any:
        try:
            from goldfive.events import agent_invocation_completed_event  # noqa: PLC0415
        except Exception:  # noqa: BLE001 — proto stubs absent; fall back
            return self._build_dict_event(
                kind=kind,
                sequence=sequence,
                invocation_id=invocation_id,
                agent_name=agent_name,
                payload=payload,
            )
        import json  # noqa: PLC0415

        try:
            event = agent_invocation_completed_event(
                self.run_id,
                sequence,
                agent_name=agent_name,
                task_id=kind,
                invocation_id=invocation_id,
                summary=json.dumps(payload, sort_keys=True, default=str),
                session_id=self.session_id,
            )
            return event
        except Exception as exc:  # noqa: BLE001 — envelope build failed; fall back
            log.debug("meta-loop proto envelope build failed (%s); falling back to dict", exc)
            return self._build_dict_event(
                kind=kind,
                sequence=sequence,
                invocation_id=invocation_id,
                agent_name=agent_name,
                payload=payload,
            )

    def _build_dict_event(
        self,
        *,
        kind: str,
        sequence: int,
        invocation_id: str,
        agent_name: str,
        payload: dict[str, Any],
        parent: str = "",
    ) -> dict[str, Any]:
        """Dict envelope used when proto stubs are unavailable / build fails.

        ``parent`` carries the enclosing structural span for the started
        half (empty for the completed half, which nests via ``invocation_id``
        correlation); it mirrors the proto path's ``parent_invocation_id``.
        """
        t = time.time_ns()
        return {
            "event_id": f"{self.run_id}:{sequence}:{uuid.uuid4().hex[:8]}",
            "run_id": self.run_id,
            "sequence": sequence,
            "session_id": self.session_id,
            "emitted_at": {"seconds": t // 1_000_000_000, "nanos": t % 1_000_000_000},
            "kind": kind,
            "invocation_id": invocation_id,
            "parent_invocation_id": parent,
            "agent_name": agent_name,
            "payload": dict(payload),
        }

    async def _fan_emit(self, event: Any) -> None:
        """Send the event to every sink, isolating per-sink failures.

        Unlike :func:`goldfive.events.emit` which re-raises the first
        exception after gathering, the meta-loop swallows every sink
        failure: the proposer / judge call sites must not become flaky
        because harmonograf hiccuped or a JSONL flush failed.
        """
        if not self._sinks:
            return
        for sink in list(self._sinks):
            try:
                await sink.emit(event)
            except Exception as exc:  # noqa: BLE001 — additive, never load-bearing
                log.warning(
                    "meta-loop sink %s.emit raised %s (%s); event dropped",
                    type(sink).__name__,
                    type(exc).__name__,
                    exc,
                )


def build_meta_loop_emitter(
    workspace_root: Path,
    *,
    harmonograf_url: str,
    evolve_started_at_iso: str,
    jsonl_filename: str = "meta_loop_events.jsonl",
) -> MetaLoopEmitter:
    """Construct the per-evolve emitter with JSONL + optional harmonograf sinks.

    The JSONL sink is always attached when goldfive is installed —
    meta-loop telemetry should land on disk even on a degraded install
    that can't reach harmonograf. The harmonograf sink is attached only
    when ``harmonograf_url`` is non-empty AND the harmonograf client
    library is importable.

    Failure isolation: every sink construction is independently wrapped.
    A failed sink is logged at WARNING and skipped — the emitter is
    always returned, possibly with an empty sink list (in which case
    every emit is a no-op but the call sites still bookkeep correctly).

    Parameters
    ----------
    workspace_root:
        The zicato workspace directory (the ``.zicato`` dir or its
        parent — both shapes are tolerated). The JSONL sink lands at
        ``<workspace>/.zicato/runtime/<jsonl_filename>``.
    harmonograf_url:
        Resolved console URL — auto-launched or operator-pinned. Empty
        string means "no harmonograf in scope"; the JSONL sink is
        attached anyway.
    evolve_started_at_iso:
        ISO timestamp captured at the top of ``evolve_n_rounds``. Used
        both as the session-id seed (via
        :func:`zicato.telemetry.harmonograf_supervisor.meta_loop_session_id`)
        and as the deterministic run_id suffix so a reducer rebuilding
        the run id later only needs the start ISO.
    jsonl_filename:
        Name of the JSONL file under ``<workspace>/.zicato/runtime/``.
        Defaults to ``meta_loop_events.jsonl``.

    Returns
    -------
    MetaLoopEmitter
        With ``sinks`` populated as best-effort; the caller MUST
        :meth:`MetaLoopEmitter.close` it at evolve teardown.
    """
    from zicato.telemetry.harmonograf_supervisor import (  # noqa: PLC0415
        build_meta_loop_sink,
        meta_loop_session_id,
    )

    session_id = meta_loop_session_id(evolve_started_at_iso)
    # The run_id is stable across one evolve invocation. Use the session
    # id with a ``-run`` suffix so a reducer can distinguish run vs
    # session at a glance without re-deriving anything.
    run_id = f"{session_id}-run"

    sinks: list[Any] = []

    # JSONL sink — best-effort. Wrapped narrowly so a sibling import
    # error (proto stubs missing) does not skip the harmonograf sink
    # below.
    try:
        from goldfive.sinks.persistence import JSONLPersistenceSink  # noqa: PLC0415

        jsonl_path = _meta_loop_jsonl_path(workspace_root, jsonl_filename)
        jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        sinks.append(JSONLPersistenceSink(path=jsonl_path, mode="append"))
    except Exception as exc:  # noqa: BLE001 — additive sink only
        log.warning(
            "meta-loop JSONL sink not attached (%s); evolve continues without "
            "meta-loop telemetry on disk",
            exc,
        )

    # Harmonograf sink — only when a URL is in scope. The helper already
    # returns ``None`` on missing client / construction failure with a
    # WARNING logged, so we only add non-None returns.
    if harmonograf_url:
        try:
            sink = build_meta_loop_sink(harmonograf_url, session_id)
            if sink is not None:
                sinks.append(sink)
        except Exception as exc:  # noqa: BLE001 — additive sink only
            log.warning(
                "meta-loop harmonograf sink build raised (%s); skipping",
                exc,
            )

    return MetaLoopEmitter(run_id=run_id, session_id=session_id, sinks=sinks)


# ---------------------------------------------------------------------------
# Ambient emitter + span helpers (the no-parameter call surface).
# ---------------------------------------------------------------------------


def set_current_emitter(emitter: MetaLoopEmitter | None) -> contextvars.Token[Any]:
    """Bind ``emitter`` as the ambient meta-loop emitter for this context.

    Called ONCE at the top of ``evolve_n_rounds``; every descendant asyncio
    task inherits the binding (a task copies the context at creation).
    Returns the token the caller passes to :func:`reset_current_emitter` in
    its teardown ``finally`` so the binding never leaks past one evolve.
    """
    return _current_emitter.set(emitter)


def reset_current_emitter(token: contextvars.Token[Any]) -> None:
    """Undo :func:`set_current_emitter`. Best-effort; never raises."""
    try:
        _current_emitter.reset(token)
    except (ValueError, LookupError) as exc:  # reset from a different context
        log.debug("meta-loop emitter reset skipped: %s", exc)


def current_meta_emitter() -> MetaLoopEmitter | None:
    """Return the ambient meta-loop emitter, or ``None`` when unbound."""
    return _current_emitter.get()


@asynccontextmanager
async def meta_span(
    name: str,
    *,
    kind: str,
    meta: dict[str, Any] | None = None,
) -> AsyncIterator[Any]:
    """Open a structural span on the ambient emitter (a no-op when unbound).

    The one-line call surface for a deep site — the tournament runner, the
    board-unit scheduler, the best-of-N slate — that has no ``meta_loop_emitter``
    argument in scope::

        async with meta_span(entry.id, kind=SPAN_MATCHUP) as sp:
            ...

    When no emitter is bound (a degraded install, or a code path outside an
    evolve) this yields a shared no-op handle and emits nothing, so every
    call site is free to run unconditionally.
    """
    emitter = _current_emitter.get()
    if emitter is None:
        yield _NULL_SPAN_HANDLE
        return
    async with emitter.span(name, kind=kind, meta=meta) as handle:
        yield handle


def _meta_loop_jsonl_path(workspace_root: Path, filename: str) -> Path:
    """Resolve the meta-loop JSONL path under the workspace.

    ``workspace_root`` may already be the ``.zicato`` directory or its
    parent. Either way the JSONL lives at
    ``<workspace>/.zicato/runtime/<filename>`` so the dashboard's
    ``runtime/`` reader picks it up alongside the per-run JSONLs.
    """
    if workspace_root.name == ".zicato":
        base = workspace_root
    else:
        candidate = workspace_root / ".zicato"
        base = candidate if candidate.exists() else workspace_root
    return base / "runtime" / filename


__all__ = [
    "SPAN_MATCHUP",
    "SPAN_PHASE",
    "SPAN_ROUND",
    "SPAN_SLOT",
    "SPAN_WORKER",
    "MetaLoopEmitter",
    "build_meta_loop_emitter",
    "current_meta_emitter",
    "meta_span",
    "reset_current_emitter",
    "set_current_emitter",
]
