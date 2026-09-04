"""Importable stub adapter + callables for the subprocess-worker tests.

The :mod:`zicato._tournament_worker` worker runs in a *separate* OS
process, so the adapter and ``call_llm`` callables it uses cannot be
closures or ``sys.modules``-monkeypatched stubs — they must be real,
importable, module-level objects the worker subprocess can resolve from
a dotted path. This module provides exactly that, mock-driven and with
no goldfive / real-LLM dependency.

The stub adapter exposes a session with the *legacy* ``run(entry,
sink_path)`` shape, which the worker's ``_drive_session`` detects by
parameter name. The legacy path does not touch goldfive: the session
just writes a minimal ``events.jsonl`` itself.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any


async def target_call_llm(system: str, user: str, model: str) -> str:
    """Stub harness-side LLM callable. Never actually invoked by the stub."""
    del system, user, model
    return "stub-target-response"


async def evaluation_call_llm(system: str, user: str, model: str) -> str:
    """Stub evaluation LLM callable, distinct from :func:`target_call_llm`."""
    del system, user, model
    return "stub-aux-response"


class _StubSession:
    """A loaded harness with the legacy ``run(entry, sink_path)`` shape.

    Writes a single empty line to the events JSONL so the reducer has a
    file to read; the reducer's JSON-fallback path tolerates a file with
    no parseable events and produces an empty-walk loss profile.
    """

    async def run(self, entry: Any, sink_path: Path) -> None:
        del entry
        sink_path.parent.mkdir(parents=True, exist_ok=True)
        sink_path.write_text("", encoding="utf-8")


class _SnapshotWritingSession:
    """A session that writes runtime output INTO the snapshot it loaded from.

    Mimics a real target agent (e.g. the target-1 presentation agent's
    ``write_webpage`` tool) that writes near its own code — here, into an
    ``output/`` directory under the generation source root it was handed.
    The L3 isolation fix exists precisely so this write lands in a
    discarded per-run working copy, NOT in the canonical generation
    snapshot. The session also writes the events JSONL so the reducer
    has a file to read (the legacy ``run(entry, sink_path)`` shape).
    """

    def __init__(self, generation_root: Path) -> None:
        self._generation_root = Path(generation_root)

    async def run(self, entry: Any, sink_path: Path) -> None:
        del entry
        # The pollution: write a runtime artifact under the snapshot root
        # the worker mounted — exactly what the presentation agent's
        # ``write_webpage`` does with ``os.path.dirname(__file__)/output``.
        output_dir = self._generation_root / "output" / "demo_topic"
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "index.html").write_text("<html>runtime artifact</html>", encoding="utf-8")
        sink_path.parent.mkdir(parents=True, exist_ok=True)
        sink_path.write_text("", encoding="utf-8")


class SnapshotWritingAdapter:
    """Adapter whose session writes runtime output into the loaded snapshot.

    ``load`` captures the ``generation_root`` it is handed and passes it
    to the session, so the session's write targets exactly the directory
    the worker was pointed at. Used by the snapshot-pollution test to
    prove that directory is a discardable per-run copy, never the
    canonical generation snapshot.
    """

    name = "stub"

    def load(self, generation_root: Path) -> _SnapshotWritingSession:
        return _SnapshotWritingSession(generation_root)

    def mutation_points(self, source_roots: Any = None) -> list[Any]:
        del source_roots
        return []

    def worker_spec(self) -> dict[str, Any]:
        return {
            "kind": "import",
            "factory": "tests._subprocess_worker_support:make_snapshot_writing_adapter",
        }


class _SleepingSession:
    """A session whose ``run`` blocks the event loop far past any budget.

    Uses a *blocking* :func:`time.sleep` rather than :func:`asyncio.sleep`
    on purpose: a blocking sleep wedges the worker's event loop so its
    own cooperative ``asyncio.wait_for`` budget CANNOT fire. That is what
    forces the PARENT's ``wait_for`` + SIGTERM/SIGKILL escalation (and,
    in production, the supervisor) to be the layer that stops the run —
    exactly the wedged-run scenario the L3 layer exists for.
    """

    async def run(self, entry: Any, sink_path: Path) -> None:
        del entry, sink_path
        time.sleep(3600.0)


class _CooperativeSleepSession:
    """A session that sleeps via a *cancellable* :func:`asyncio.sleep`.

    Unlike :class:`_SleepingSession` this does NOT block the event loop,
    so the worker's own cooperative ``asyncio.wait_for`` budget fires and
    cancels the run cleanly — the worker self-aborts and exits 0.
    """

    async def run(self, entry: Any, sink_path: Path) -> None:
        import asyncio  # noqa: PLC0415

        del entry, sink_path
        await asyncio.sleep(3600.0)


class CooperativeAdapter:
    """Adapter whose session self-aborts on the worker's cooperative budget."""

    name = "stub"

    def load(self, generation_root: Path) -> _CooperativeSleepSession:
        del generation_root
        return _CooperativeSleepSession()

    def mutation_points(self, source_roots: Any = None) -> list[Any]:
        del source_roots
        return []


class _EmittingThenSleepingSession:
    """Rich ``run(entry, sinks, config)`` session: emit one frame then sleep.

    The session pushes a single ``run_started`` event through the goldfive
    sink list (so the worker's :class:`SequenceTrackingSink` observes a
    run_id + sequence on the wire), then awaits a cancellable
    :func:`asyncio.sleep`. The worker's cooperative budget fires, the
    ``goldfive.run``-equivalent task is cancelled, and the terminal-event
    fix is the only thing that can leave a ``run_aborted`` frame on disk.
    """

    async def run(self, entry: Any, sinks: Any, config: Any) -> Any:
        import asyncio  # noqa: PLC0415

        from goldfive.events import emit, run_started_event  # noqa: PLC0415

        from zicato.core import RunResult  # noqa: PLC0415

        del config
        run_id = "test-emitting-run"
        evt = run_started_event(run_id=run_id, sequence=1, goal_summary="t")
        await emit(list(sinks), evt)
        await asyncio.sleep(3600.0)
        # Unreachable: the worker's cooperative wait_for cancels us.
        return RunResult(
            run_id=run_id,
            entry_id=entry.id,
            final_output="",
            transcript=(),
            runtime_ms=0,
            aborted=False,
            abort_reason="",
        )


class EmittingThenSleepingAdapter:
    """Adapter whose rich-shape session emits a frame then sleeps to cancel."""

    name = "stub"

    def load(self, generation_root: Path) -> _EmittingThenSleepingSession:
        del generation_root
        return _EmittingThenSleepingSession()

    def mutation_points(self, source_roots: Any = None) -> list[Any]:
        del source_roots
        return []

    def worker_spec(self) -> dict[str, Any]:
        return {
            "kind": "import",
            "factory": "tests._subprocess_worker_support:make_emitting_then_sleeping_adapter",
        }


class _AbortingSession:
    """A session with the rich ``run(entry, sinks, config)`` shape that aborts.

    Returns a :class:`~zicato.core.RunResult` with ``aborted=True`` and an
    ``abort_reason`` mimicking a harness exception — exactly what the adk
    adapter synthesises when the inner agent crashes. The run finishes
    near-instantly with an empty events file, so without the reducer's
    not-completed penalty it would score ``drift_loss == 0.0``.
    """

    async def run(self, entry: Any, sinks: Any, config: Any) -> Any:
        del sinks, config
        from zicato.core import RunResult  # noqa: PLC0415

        return RunResult(
            run_id=f"abort-{entry.id}",
            entry_id=entry.id,
            final_output="",
            transcript=(),
            runtime_ms=1,
            aborted=True,
            abort_reason="harness_exception:TypeError",
        )


class AbortingAdapter:
    """Adapter whose session returns an aborted RunResult (a simulated crash)."""

    name = "stub"

    def load(self, generation_root: Path) -> _AbortingSession:
        del generation_root
        return _AbortingSession()

    def mutation_points(self, source_roots: Any = None) -> list[Any]:
        del source_roots
        return []

    def worker_spec(self) -> dict[str, Any]:
        return {
            "kind": "import",
            "factory": "tests._subprocess_worker_support:make_aborting_adapter",
        }


class _CompletingSession:
    """Rich ``run(entry, sinks, config)`` session that completes normally.

    Returns a completed :class:`~zicato.core.RunResult` with a real
    ``final_output`` + two-turn ``transcript`` — the shape the result.json
    capture persists. Additionally exercises the worker-bound judge-I/O
    sink when one rides the config (``config.judge_io_sink``): it records
    one scripted judge call, standing in for the inline judge's emission
    (the real emission path is unit-tested against
    ``_InlineCriterionJudge`` directly; this proves the WORKER threaded a
    live sink to the session's config and that the sidecar lands beside
    ``loss.json``).
    """

    async def run(self, entry: Any, sinks: Any, config: Any) -> Any:
        del sinks
        from zicato.core import RunResult  # noqa: PLC0415

        sink = getattr(config, "judge_io_sink", None)
        if sink is not None:
            sink.record(
                "stub_judge",
                reasoning_text="the exact reasoning under judgement",
                transcript_window=("turn one", "turn two"),
                raw_response="OK looks fine",
                drift_emitted=False,
                kind="",
                severity="",
                detail="",
            )
        return RunResult(
            run_id=f"complete-{entry.id}",
            entry_id=entry.id,
            final_output="final answer text",
            transcript=("intermediate turn", "final answer text"),
            runtime_ms=7,
            aborted=False,
            abort_reason="",
        )


class CompletingAdapter:
    """Adapter whose rich-shape session completes with a full RunResult."""

    name = "stub"

    def load(self, generation_root: Path) -> _CompletingSession:
        del generation_root
        return _CompletingSession()

    def mutation_points(self, source_roots: Any = None) -> list[Any]:
        del source_roots
        return []

    def worker_spec(self) -> dict[str, Any]:
        return {
            "kind": "import",
            "factory": "tests._subprocess_worker_support:make_completing_adapter",
        }


class _ArtifactWritingSession:
    """Write files with names known only at run time into the supplied scratch tree."""

    async def run(self, entry: Any, sinks: Any, config: Any) -> Any:
        del sinks, config
        from zicato.core import RunResult  # noqa: PLC0415
        from zicato.epoch.snapshot_scope import SCRATCH_DIR_ENV  # noqa: PLC0415

        scratch = Path(os.environ[SCRATCH_DIR_ENV])
        output = scratch / "reports" / entry.id
        output.mkdir(parents=True)
        (output / "summary.html").write_text("<h1>captured</h1>", encoding="utf-8")
        (scratch / "render.bin").write_bytes(b"\x00\x01")
        return RunResult(
            run_id=f"artifacts-{entry.id}",
            entry_id=entry.id,
            final_output="done",
            transcript=("done",),
            runtime_ms=1,
        )


class ArtifactWritingAdapter:
    name = "stub"

    def load(self, generation_root: Path) -> _ArtifactWritingSession:
        del generation_root
        return _ArtifactWritingSession()

    def mutation_points(self, source_roots: Any = None) -> list[Any]:
        del source_roots
        return []


def artifact_inventory_is_visible(result: Any) -> bool:
    """Predicate proving post-run graders receive the captured inventory."""
    artifacts = result.artifacts
    return artifacts is not None and [item.path for item in artifacts.files] == [
        "render.bin",
        f"reports/{result.entry_id}/summary.html",
    ]


class _ConfigProbeSession:
    """A session that records the WORKER process's resolved typed config.

    Writes the worker-side ``load_config()`` view of the evaluation-call
    budget to ``config_probe.json`` next to the events file. The
    config-pins threading test reads it back to prove a value pinned in
    the ORCHESTRATOR process (a CLI flag) crossed the subprocess
    boundary via the args file — with no environment variable involved.
    """

    async def run(self, entry: Any, sink_path: Path) -> None:
        import json  # noqa: PLC0415

        from zicato.config import load_config  # noqa: PLC0415

        del entry
        cfg = load_config()
        probe = {"aux_call_timeout_s": cfg.aux.call_timeout_s}
        sink_path.parent.mkdir(parents=True, exist_ok=True)
        (sink_path.parent / "config_probe.json").write_text(json.dumps(probe), encoding="utf-8")
        sink_path.write_text("", encoding="utf-8")


class ConfigProbeAdapter:
    """Adapter whose session records the worker's resolved typed config."""

    name = "stub"

    def load(self, generation_root: Path) -> _ConfigProbeSession:
        del generation_root
        return _ConfigProbeSession()

    def mutation_points(self, source_roots: Any = None) -> list[Any]:
        del source_roots
        return []

    def worker_spec(self) -> dict[str, Any]:
        return {
            "kind": "import",
            "factory": "tests._subprocess_worker_support:make_config_probe_adapter",
        }


class StubAdapter:
    """A minimal :class:`~zicato.adapters.base.HarnessAdapter`-shaped object.

    ``load`` returns a fresh :class:`_StubSession` per call. The
    ``worker_spec`` method is the hook
    :func:`zicato.tournament.runner.adapter_worker_spec` uses to make the
    adapter re-constructible inside the worker subprocess.
    """

    name = "stub"

    def load(self, generation_root: Path) -> _StubSession:
        del generation_root
        return _StubSession()

    def mutation_points(self, source_roots: Any = None) -> list[Any]:
        del source_roots
        return []

    def worker_spec(self) -> dict[str, Any]:
        return {
            "kind": "import",
            "factory": "tests._subprocess_worker_support:make_stub_adapter",
        }


class SleepingAdapter:
    """Like :class:`StubAdapter` but its session blocks the loop forever."""

    name = "stub"

    def __init__(self, ignore_sigterm: bool = False) -> None:
        self._ignore_sigterm = ignore_sigterm

    def load(self, generation_root: Path) -> _SleepingSession:
        del generation_root
        return _SleepingSession()

    def mutation_points(self, source_roots: Any = None) -> list[Any]:
        del source_roots
        return []

    def worker_spec(self) -> dict[str, Any]:
        factory = (
            "tests._subprocess_worker_support:make_sigterm_ignoring_adapter"
            if self._ignore_sigterm
            else "tests._subprocess_worker_support:make_sleeping_adapter"
        )
        return {"kind": "import", "factory": factory}


def make_stub_adapter() -> StubAdapter:
    """Factory used by the ``import`` adapter spec for the happy path."""
    return StubAdapter()


def make_config_probe_adapter() -> ConfigProbeAdapter:
    """Factory for the adapter that records the worker's resolved config."""
    return ConfigProbeAdapter()


def make_snapshot_writing_adapter() -> SnapshotWritingAdapter:
    """Factory for the snapshot-pollution test's snapshot-writing adapter."""
    return SnapshotWritingAdapter()


def make_sleeping_adapter() -> SleepingAdapter:
    """Factory used by the ``import`` adapter spec for the budget tests."""
    return SleepingAdapter()


def make_sigterm_ignoring_adapter() -> SleepingAdapter:
    """Factory that installs a SIGTERM-ignoring handler, then sleeps forever.

    Runs *inside the worker subprocess* (the worker calls the adapter
    factory there), so the ``signal.signal`` call makes the worker
    survive the parent's SIGTERM and forces escalation to SIGKILL.
    """
    import signal  # noqa: PLC0415

    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    return SleepingAdapter()


def make_cooperative_adapter() -> CooperativeAdapter:
    """Factory for a worker that self-aborts on its own cooperative budget."""
    return CooperativeAdapter()


def make_emitting_then_sleeping_adapter() -> EmittingThenSleepingAdapter:
    """Factory for the emit-one-frame-then-sleep adapter."""
    return EmittingThenSleepingAdapter()


def make_aborting_adapter() -> AbortingAdapter:
    """Factory for the adapter whose session returns an aborted RunResult."""
    return AbortingAdapter()


def make_completing_adapter() -> CompletingAdapter:
    """Factory for the adapter whose session completes with a full RunResult."""
    return CompletingAdapter()


def make_artifact_writing_adapter() -> ArtifactWritingAdapter:
    """Factory for the adapter that emits arbitrary scratch files."""
    return ArtifactWritingAdapter()


def pid_marker_path() -> Path:
    """Return a per-process marker path — unused placeholder for symmetry."""
    return Path(os.getcwd()) / f".ztw-pid-{os.getpid()}-{time.time_ns()}"
