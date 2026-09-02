"""Tests for :mod:`zicato.runtime.spawn_permit` — the host-wide worker cap.

The claim under test is a CROSS-PROCESS one (RUNTIME.md §5.5.7):
``RuntimeConfig.parallelism`` is a per-process semaphore, so two
concurrent orchestrators over-subscribe the host; the permit is what
bounds them together. So the load-bearing tests here hold a permit in a
real second process and assert this process is blocked by it — an
in-process test alone would not distinguish a host-wide cap from a
process-local one.

Every permit call uses its test's private ``tmp_path`` so the suite never
contends with a real run's permits or with another parallel test worker.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from zicato.runtime.spawn_permit import (
    MIN_AUTO_PERMITS,
    OPEN_PERMIT,
    WorkerPermit,
    acquire_worker_permit,
    default_host_worker_permits,
    effective_permit_count,
    permit_dir,
)


@pytest.fixture
def permit_root(tmp_path: Path) -> Path:
    """Return a private permit directory for this test."""
    return tmp_path / "permits"


# ---------------------------------------------------------------------------
# Knob resolution
# ---------------------------------------------------------------------------


def test_none_means_auto_and_auto_is_generous() -> None:
    """``None`` (the default) resolves to a cap a normal run never hits."""
    auto = default_host_worker_permits()
    assert effective_permit_count(None) == auto
    assert auto >= MIN_AUTO_PERMITS
    assert auto >= 2 * (os.cpu_count() or 1)


def test_zero_disables_and_negatives_clamp_to_zero() -> None:
    """``0`` is off; a typo'd negative is also off, never an error.

    A throttle must not be able to fail a run on a config typo.
    """
    assert effective_permit_count(0) == 0
    assert effective_permit_count(-7) == 0
    assert effective_permit_count(3) == 3


def test_permit_dir_resolution_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit configuration wins; then XDG; then a uid-scoped temp path."""
    assert permit_dir("/explicit/override") == Path("/explicit/override")

    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/4242")
    assert permit_dir() == Path("/run/user/4242/zicato/worker-permits")

    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    fallback = permit_dir()
    # Workspace-EXTERNAL is the whole point: never under a .zicato tree.
    assert ".zicato" not in str(fallback)
    assert fallback.name.startswith("zicato-worker-permits-")


# ---------------------------------------------------------------------------
# Admission and queueing
# ---------------------------------------------------------------------------


async def test_cap_admits_up_to_the_limit_then_queues(permit_root: Path) -> None:
    """N permits admit N holders; the N+1th waits until one is released."""
    first = await acquire_worker_permit(2, permit_root)
    second = await acquire_worker_permit(2, permit_root)
    assert first.held and second.held
    assert {first.slot, second.slot} == {0, 1}

    third = asyncio.create_task(acquire_worker_permit(2, permit_root))
    await asyncio.sleep(0.2)
    assert not third.done(), "the third acquirer must queue while both slots are held"

    first.release()
    got = await asyncio.wait_for(third, timeout=5.0)
    assert got.held
    got.release()
    second.release()


async def test_cap_zero_admits_immediately_and_touches_no_filesystem(
    permit_root: Path,
) -> None:
    """With the cap off, nothing is created and every acquire is open."""
    permits = [await acquire_worker_permit(0, permit_root) for _ in range(50)]
    assert all(p is OPEN_PERMIT for p in permits)
    assert not any(p.held for p in permits)
    assert not permit_root.exists(), "the disabled cap must not touch the filesystem"


async def test_release_is_idempotent_and_never_raises(permit_root: Path) -> None:
    """``release`` runs from a ``finally``; it must never raise, ever."""
    permit = await acquire_worker_permit(1, permit_root)
    assert permit.held
    permit.release()
    assert not permit.held
    permit.release()  # second release: a no-op, not an error
    OPEN_PERMIT.release()  # the shared open permit tolerates it too


async def test_cancelling_a_waiter_leaks_no_slot_and_no_descriptor(
    permit_root: Path,
) -> None:
    """A round cancelled by ``max_wall_clock_seconds`` unwinds waiters.

    ``evolve/loop.py`` wraps a round in ``asyncio.wait_for``, so a queued
    board unit can be cancelled while parked in the poll loop. A cancellation
    that stranded a descriptor would burn a slot for the life of the
    orchestrator — the cap would tighten silently, run after run.
    """
    fd_dir = Path(f"/proc/{os.getpid()}/fd")
    if not fd_dir.exists():  # pragma: no cover — Linux-only assertion
        pytest.skip("no /proc fd table on this platform")

    # The ONLY slot is held, so every waiter below really parks in the poll
    # loop rather than acquiring on its first sweep.
    held = await acquire_worker_permit(1, permit_root)
    before = len(list(fd_dir.iterdir()))
    for _ in range(50):
        waiter = asyncio.create_task(acquire_worker_permit(1, permit_root))
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
    assert len(list(fd_dir.iterdir())) == before, "a cancelled waiter leaked a descriptor"
    held.release()

    # The slot is free again, so nothing was stranded.
    again = await asyncio.wait_for(acquire_worker_permit(1, permit_root), timeout=5.0)
    assert again.held
    again.release()


async def test_released_slot_is_immediately_reusable(permit_root: Path) -> None:
    """A released permit frees its slot for the next acquirer."""
    for _ in range(5):
        permit = await acquire_worker_permit(1, permit_root)
        assert permit.held
        permit.release()


# ---------------------------------------------------------------------------
# Degrade OPEN — an infra problem must never block a run
# ---------------------------------------------------------------------------


async def test_unusable_permit_dir_degrades_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A permit dir that cannot be created admits, rather than failing."""
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("i am a file", encoding="utf-8")
    directory = blocker / "under-a-file"
    permit = await acquire_worker_permit(1, directory)
    assert not permit.held, "an uncreatable permit dir must degrade OPEN"
    permit.release()


async def test_degrade_open_warns_once_per_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A degraded cap is the one throttle failure with no other symptom.

    The runs proceed, so nothing looks wrong and the configured cap is simply
    absent. At debug level an operator would never learn that. So the first
    degrade WARNS — and only the first, or a permanently unwritable runtime
    dir would emit a line per board unit.
    """
    from zicato.runtime import spawn_permit

    blocker = tmp_path / "not-a-dir"
    blocker.write_text("i am a file", encoding="utf-8")
    directory = blocker / "under-a-file"
    monkeypatch.setattr(spawn_permit, "_degraded_open_warned", False)

    with caplog.at_level("DEBUG", logger="zicato.runtime.spawn_permit"):
        for _ in range(5):
            (await acquire_worker_permit(1, directory)).release()

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1, f"expected exactly one warning, got {len(warnings)}"
    assert "NOT in force" in warnings[0].getMessage()


def test_auto_count_follows_the_cpus_this_process_may_USE(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AUTO must honour a cpuset, not the host's core count.

    ``os.cpu_count()`` reports the HOST, so a container pinned to 2 CPUs on a
    128-core box would get 256 permits — a cap that cannot bind exactly where
    over-subscription hurts most.
    """
    from zicato.runtime import spawn_permit

    monkeypatch.setattr(os, "cpu_count", lambda: 128)
    monkeypatch.setattr(os, "sched_getaffinity", lambda pid: {0, 1}, raising=False)
    assert default_host_worker_permits() == max(MIN_AUTO_PERMITS, 4)

    # No ``sched_getaffinity`` (macOS / Windows) ⇒ fall back to cpu_count.
    monkeypatch.delattr(os, "sched_getaffinity", raising=False)
    assert spawn_permit._usable_cpus() == 128


async def test_missing_fcntl_degrades_open(
    permit_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ``fcntl`` (a non-POSIX host) admits, rather than failing."""
    import builtins

    real_import = builtins.__import__

    def _no_fcntl(name: str, *args: object, **kwargs: object) -> object:
        if name == "fcntl":
            raise ImportError("no fcntl on this platform")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", _no_fcntl)
    permit = await acquire_worker_permit(1, permit_root)
    monkeypatch.undo()
    assert not permit.held, "a platform without flock must degrade OPEN"
    permit.release()


# ---------------------------------------------------------------------------
# The cross-process claim — this is what makes the cap HOST-wide
# ---------------------------------------------------------------------------


_HOLDER_SOURCE = """
import asyncio, sys
from zicato.runtime.spawn_permit import acquire_worker_permit


async def main() -> None:
    permit = await acquire_worker_permit(int(sys.argv[2]), sys.argv[1])
    assert permit.held, "holder failed to take a permit"
    print("HELD", flush=True)
    # Hold until killed / stdin closes.
    await asyncio.sleep(300)


asyncio.run(main())
"""


def _spawn_holder(permit_root: Path, count: int) -> subprocess.Popen[str]:
    """Start a second process that takes and holds one permit."""
    proc = subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(_HOLDER_SOURCE), str(permit_root), str(count)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    line = proc.stdout.readline().strip()
    if line != "HELD":
        proc.kill()
        stderr = proc.stderr.read() if proc.stderr else ""
        pytest.fail(f"holder process did not take a permit: {line!r} / {stderr}")
    return proc


@pytest.mark.integration
async def test_permit_is_held_across_processes(permit_root: Path) -> None:
    """A permit held by ANOTHER process blocks this one.

    This is the whole point: ``parallelism`` cannot see a second
    orchestrator, and this cap must.
    """
    holder = _spawn_holder(permit_root, 1)
    try:
        waiter = asyncio.create_task(acquire_worker_permit(1, permit_root))
        await asyncio.sleep(0.3)
        assert not waiter.done(), "another process's permit must block this one"
    finally:
        holder.kill()
        holder.wait(timeout=10)
    # The kernel drops the flock when the holder dies — see the next test.
    got = await asyncio.wait_for(waiter, timeout=10.0)
    assert got.held
    got.release()


@pytest.mark.integration
async def test_killed_holder_leaks_no_permit(permit_root: Path) -> None:
    """``flock`` is released by the kernel on process death.

    This is the reason ``flock`` was chosen over a counter file: a crashed
    orchestrator cannot leak a permit, so there is no stale-permit reaper
    to write and no liveness protocol to get wrong.
    """
    holder = _spawn_holder(permit_root, 1)
    holder.kill()  # SIGKILL: no chance to clean up after itself
    holder.wait(timeout=10)

    permit = await asyncio.wait_for(acquire_worker_permit(1, permit_root), timeout=10.0)
    assert permit.held, "a SIGKILLed holder must not leave a permanently-held slot"
    permit.release()


# ---------------------------------------------------------------------------
# The runner integration — acquired before the spawn, released in the finally
# ---------------------------------------------------------------------------


def test_runner_resolves_the_permit_helpers_on_its_own_namespace() -> None:
    """``_run_single`` calls these as module globals, so tests can patch them.

    Pins the seam the runner integration tests below rely on: the helper
    names live on the runner module, not behind a function-local import.
    """
    from zicato.tournament import runner

    assert runner.acquire_worker_permit is acquire_worker_permit
    assert runner.OPEN_PERMIT is OPEN_PERMIT
    assert runner.WorkerPermit is WorkerPermit


def _stub_run_inputs(tmp_path: Path) -> tuple[Path, object, object, object]:
    """A workspace + generation + entry + config for a stub-adapter run."""
    from tests._subprocess_worker_support import auxiliary_call_llm, harness_call_llm
    from zicato.core import BoardEntry, Generation, RuntimeConfig

    workspace = tmp_path / ".zicato"
    workspace.mkdir(exist_ok=True)
    snap = workspace / "snap" / "v0"
    snap.mkdir(parents=True, exist_ok=True)
    generation = Generation(
        id="v0",
        epoch_id="e0",
        parent_id=None,
        snapshot_root=snap,
        created_at="2026-05-15T00:00:00Z",
    )
    entry = BoardEntry(id="entry_a", kind="single_turn", wall_clock_budget_seconds=60, input="hi")
    config = RuntimeConfig(
        instance_id="test",
        workspace_root=workspace,
        harness_call_llm=harness_call_llm,
        auxiliary_call_llm=auxiliary_call_llm,
        supervisor_kill_wait_s=2.0,
        host_worker_permits=1,
    )
    return workspace, generation, entry, config


async def test_runner_asks_for_a_permit_and_always_releases_it(
    permit_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_run_single`` acquires with the configured knob and releases it.

    Patches the permit helper on the runner's namespace so the assertion
    does not depend on a real worker spawn (which the next test covers).
    """
    from zicato.core import ScoringWeights
    from zicato.tournament import runner as runner_mod

    asked: list[tuple[int | None, Path | str | None]] = []
    released: list[bool] = []

    class _RecordingPermit(WorkerPermit):
        def release(self) -> None:
            released.append(True)

    async def _fake_acquire(limit: int | None, directory: Path | str | None = None) -> WorkerPermit:
        asked.append((limit, directory))
        return _RecordingPermit()

    monkeypatch.setattr(runner_mod, "acquire_worker_permit", _fake_acquire)

    workspace, generation, entry, config = _stub_run_inputs(tmp_path)
    # A deliberately unserialisable adapter: the run takes the
    # ``prepare_failed`` early-return, the path most likely to skip the
    # release if the ``finally`` were placed wrongly. No worker is spawned.
    loss = await runner_mod._run_single(
        adapter=object(),
        generation=generation,
        entry=entry,
        weights=ScoringWeights(),
        config=config,
        workspace_root=workspace,
        epoch_id="e0",
        side="parent",
    )
    assert loss.abort_cause == "prepare_failed"

    assert asked == [(1, None)], "the runner must pass both permit settings through"
    assert released == [True], "the permit must be released on the early-return path too"


async def test_worker_spawn_failure_returns_an_aborted_loss(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests._subprocess_worker_support import StubAdapter
    from zicato.core import ScoringWeights
    from zicato.tournament import runner as runner_mod

    async def _fail_spawn(*args: object, **kwargs: object) -> object:
        raise OSError("process table unavailable")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fail_spawn)
    workspace, generation, entry, config = _stub_run_inputs(tmp_path)
    loss = await runner_mod._run_single(
        adapter=StubAdapter(),
        generation=generation,
        entry=entry,
        weights=ScoringWeights(),
        config=config,
        workspace_root=workspace,
        epoch_id="e0",
        side="parent",
    )
    assert loss.abort_cause == "prepare_failed"


@pytest.mark.integration
async def test_two_concurrent_runs_serialise_under_a_one_permit_cap(
    permit_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two real worker runs under a cap of 1 both complete.

    This is the end-to-end proof that the permit is both acquired around a
    real spawn AND released afterwards: a leaked permit would hang the
    second run forever, and a permit never taken would make the cap a lie.
    """
    import tempfile as _tempfile

    from tests._subprocess_worker_support import StubAdapter
    from zicato.core import LossProfile, ScoringWeights
    from zicato.tournament.runner import _run_single

    isolated = tmp_path / "ztw-tmp"
    isolated.mkdir()
    monkeypatch.setattr(_tempfile, "tempdir", str(isolated))

    from dataclasses import replace

    workspace, generation, entry, config = _stub_run_inputs(tmp_path)
    config = replace(config, worker_permit_dir=permit_root)

    async def _one(side: str) -> LossProfile:
        return await _run_single(
            adapter=StubAdapter(),
            generation=generation,
            entry=entry,
            weights=ScoringWeights(),
            config=config,
            workspace_root=workspace,
            epoch_id="e0",
            side=side,
        )

    losses = await asyncio.wait_for(
        asyncio.gather(_one("parent"), _one("child")),
        timeout=120.0,
    )
    assert all(isinstance(loss, LossProfile) for loss in losses)

    # And the cap is free again — nothing leaked out of either run.
    permit = await asyncio.wait_for(acquire_worker_permit(1, permit_root), timeout=5.0)
    assert permit.held
    permit.release()
