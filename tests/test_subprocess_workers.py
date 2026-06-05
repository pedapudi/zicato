"""Tests for the L3 subprocess-isolation layer.

Every tournament run executes in its own OS process — a
``python -m zicato._tournament_worker`` subprocess. These tests cover:

* a worker run end-to-end with a stub adapter (no real LLM / goldfive):
  the worker writes ``active_runs/{run_id}.json`` with its OWN pid, runs
  the entry, writes ``loss.json`` + a result file, exits 0, and removes
  its ``active_runs`` file;
* the PARENT-side budget: a worker that blocks past
  ``wall_clock_budget_seconds + GRACE`` is SIGTERM'd then SIGKILL'd by
  :func:`zicato.tournament.runner._run_single`, which returns an aborted
  :class:`LossProfile` so the tournament continues;
* the SUPERVISOR-kill path: a worker killed externally mid-run leaves no
  result file; ``_run_single`` records an aborted run with no exception;
* a worker that ignores SIGTERM: the parent escalates to SIGKILL after
  the grace.

The stub adapter / callables live in
:mod:`tests._subprocess_worker_support` because the worker subprocess
must import them from a real dotted path — closures and
``sys.modules``-monkeypatched stubs are invisible across the process
boundary.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

import zicato.tournament.runner as runner_mod
from tests._subprocess_worker_support import (
    SleepingAdapter,
    SnapshotWritingAdapter,
    StubAdapter,
    auxiliary_call_llm,
    harness_call_llm,
)
from zicato.core import BoardEntry, Generation, LossProfile, RuntimeConfig, ScoringWeights
from zicato.core.workspace import events_jsonl_path, loss_profile_path
from zicato.runtime.paths import active_run_path
from zicato.runtime.state import ActiveRun
from zicato.tournament.runner import _run_single

# ---------------------------------------------------------------------------
# Hermeticity fixture
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_tempdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect ``tempfile.tempdir`` into this test's ``tmp_path``.

    Several tests in this file glob ``tempfile.gettempdir()`` for leaked
    ``ztw-snap-<run_id>-*`` working copies. Two of them use the same
    ``run_id`` (``v0--entry_a``), so a leak from one — e.g. the
    SIGTERM->SIGKILL escalation path under load — pollutes the system
    ``/tmp`` and fails the next run on the *next* pytest invocation.
    Patching ``tempfile.tempdir`` redirects every ``mkdtemp`` /
    ``mkstemp`` call (including the runner's) AND every
    ``gettempdir()`` lookup the assertions perform into this test's own
    ``tmp_path``, so each test starts from an empty, private temp root.
    """
    isolated = tmp_path / "ztw-tmp"
    isolated.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(isolated))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _entry(entry_id: str = "entry_a", budget_s: int = 60) -> BoardEntry:
    return BoardEntry(
        id=entry_id,
        kind="single_turn",
        wall_clock_budget_seconds=budget_s,
        input="hello",
    )


def _generation(workspace: Path, gen_id: str = "v0") -> Generation:
    snap = workspace / "snap" / gen_id
    snap.mkdir(parents=True, exist_ok=True)
    return Generation(
        id=gen_id,
        epoch_id="e0",
        parent_id=None,
        snapshot_root=snap,
        created_at="2026-05-15T00:00:00Z",
    )


def _config(workspace: Path) -> RuntimeConfig:
    # The two callables are real, importable, module-level objects so the
    # worker subprocess can re-resolve them from a dotted path.
    return RuntimeConfig(
        instance_id="test",
        workspace_root=workspace,
        harness_call_llm=harness_call_llm,
        auxiliary_call_llm=auxiliary_call_llm,
    )


def _worker_env() -> dict[str, str]:
    """Env for a directly-spawned worker: worktree root on PYTHONPATH.

    ``_run_single``-driven tests inherit the parent's env automatically;
    a directly-spawned worker needs the worktree root explicitly so it
    can import ``tests._subprocess_worker_support``.
    """
    env = dict(os.environ)
    root = str(Path(__file__).resolve().parent.parent)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = root if not existing else f"{root}{os.pathsep}{existing}"
    return env


def _write_args_file(
    args_path: Path,
    *,
    workspace: Path,
    generation: Generation,
    entry: BoardEntry,
    result_path: Path,
    adapter_factory: str,
) -> None:
    """Write a worker args file pointing at a stub adapter."""
    sink_path = events_jsonl_path(workspace, "e0", generation.id, entry.id)
    loss_path = loss_profile_path(workspace, "e0", generation.id, entry.id)
    payload = {
        "workspace_root": str(workspace),
        "epoch_id": "e0",
        "generation_id": generation.id,
        "snapshot_root": str(generation.snapshot_root),
        "entry": {
            "id": entry.id,
            "kind": entry.kind,
            "wall_clock_budget_seconds": entry.wall_clock_budget_seconds,
            "input": entry.input,
        },
        "adapter": {"kind": "import", "factory": adapter_factory},
        "harness_role": {"dotted": "tests._subprocess_worker_support:harness_call_llm"},
        "auxiliary_role": {"dotted": "tests._subprocess_worker_support:auxiliary_call_llm"},
        "sink_events_path": str(sink_path),
        "loss_path": str(loss_path),
        "result_path": str(result_path),
        "instance_id": "test",
        "seed": None,
        "harmonograf_url": "",
        "weights": {},
    }
    args_path.write_text(json.dumps(payload), encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. Worker run end-to-end
# ---------------------------------------------------------------------------


def test_worker_runs_entry_end_to_end(tmp_path: Path) -> None:
    """A directly-spawned worker writes active_runs with its own pid, runs the
    entry, writes loss.json + result file, exits 0, and cleans up active_runs."""
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    generation = _generation(workspace)
    entry = _entry()

    args_path = tmp_path / "args.json"
    result_path = tmp_path / "result.json"
    _write_args_file(
        args_path,
        workspace=workspace,
        generation=generation,
        entry=entry,
        result_path=result_path,
        adapter_factory="tests._subprocess_worker_support:make_stub_adapter",
    )

    proc = _spawn_worker_blocking(args_path)
    assert proc.returncode == 0, "worker should exit cleanly"

    run_id = f"{generation.id}--{entry.id}"

    # loss.json was written by the worker.
    loss_path = loss_profile_path(workspace, "e0", generation.id, entry.id)
    assert loss_path.exists()

    # The result file carries the agreed schema and points at loss.json.
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["schema"] == "zicato.tournament_worker.result/1"
    assert result["loss_profile_path"] == str(loss_path)
    assert result["aborted"] is False
    assert result["abort_reason"] == ""
    assert isinstance(result["runtime_ms"], int)

    # On a clean exit the worker removed its own active_runs file.
    assert not active_run_path(workspace, run_id).exists()


def test_worker_penalises_aborted_run_in_loss_json(tmp_path: Path) -> None:
    """A run whose adapter returns an aborted RunResult is scored worst-case.

    Regression for F3: a run that crashes (here, the adapter returns a
    ``RunResult(aborted=True, abort_reason='harness_exception:...')``)
    finishes near-instantly with an empty events file. The worker still
    exits 0 with a clean result file, so the runner reads back the
    ``loss.json`` it wrote. That ``loss.json`` must carry the
    not-completed penalty — never ``drift_loss == 0.0`` — or a
    challenger generation could win a tournament by crashing fast.
    """
    from zicato.telemetry.reducer import read_loss_profile

    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    generation = _generation(workspace)
    entry = _entry()

    args_path = tmp_path / "args.json"
    result_path = tmp_path / "result.json"
    _write_args_file(
        args_path,
        workspace=workspace,
        generation=generation,
        entry=entry,
        result_path=result_path,
        adapter_factory="tests._subprocess_worker_support:make_aborting_adapter",
    )

    proc = _spawn_worker_blocking(args_path)
    # A crashed *run* is a clean *worker* exit — the worker caught the
    # adapter's aborted RunResult and wrote loss.json + result file.
    assert proc.returncode == 0, "an aborted-run worker still exits cleanly"

    result = json.loads(result_path.read_text(encoding="utf-8"))
    # The result-file ``aborted`` flag tracks the worker's *budget* abort
    # only; a harness crash is not a budget abort, so it stays False. The
    # run-level abort lives on the embedded RunResult.
    assert result["aborted"] is False
    assert result["run_result"]["aborted"] is True

    loss_path = loss_profile_path(workspace, "e0", generation.id, entry.id)
    assert loss_path.exists()
    profile = read_loss_profile(loss_path)
    # The crash is penalised, not rewarded: drift_loss is the worst-case
    # not-completed magnitude, never 0.0.
    assert profile.drift_loss > 0.0
    # Heavy term 5.0 * max(severity_weights)=50.0 + floored
    # task_failure_ratio 1.0 * 10.0 = 60.0 under the default weights.
    assert profile.drift_loss == pytest.approx(60.0)
    assert profile.task_failure_ratio == pytest.approx(1.0)


def test_worker_stamps_its_own_pid_into_active_runs(tmp_path: Path) -> None:
    """The active_runs file the worker writes carries the WORKER's pid.

    The worker removes the file on a clean exit, so we capture it mid-run
    by giving the worker a sleeping session and reading the file before
    we kill the worker.
    """
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    generation = _generation(workspace)
    entry = _entry(budget_s=3600)

    args_path = tmp_path / "args.json"
    result_path = tmp_path / "result.json"
    _write_args_file(
        args_path,
        workspace=workspace,
        generation=generation,
        entry=entry,
        result_path=result_path,
        adapter_factory="tests._subprocess_worker_support:make_sleeping_adapter",
    )

    run_id = f"{generation.id}--{entry.id}"
    run_path = active_run_path(workspace, run_id)

    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, "-m", "zicato._tournament_worker", str(args_path)],
        env=_worker_env(),
    )
    try:
        # Wait for the worker to write its active_runs file.
        deadline = time.monotonic() + 15.0
        while not run_path.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert run_path.exists(), "worker never wrote its active_runs file"

        record = ActiveRun.from_dict(json.loads(run_path.read_text(encoding="utf-8")))
        # The key L3 invariant: the pid is the WORKER's own pid, not the
        # parent test process's.
        assert record.pid == proc.pid
        assert record.pid != os.getpid()
        assert record.run_id == run_id
        assert record.entry_id == entry.id
        assert record.deadline  # deadline = started_at + budget
    finally:
        proc.kill()
        proc.wait(timeout=10)


def _spawn_worker_blocking(args_path: Path) -> subprocess.CompletedProcess[bytes]:
    """Spawn the worker and block until it exits; return the finished proc."""
    return subprocess.run(  # noqa: S603
        [sys.executable, "-m", "zicato._tournament_worker", str(args_path)],
        env=_worker_env(),
        timeout=60,
        check=False,
    )


# ---------------------------------------------------------------------------
# 2. Parent-side budget — SIGTERM -> SIGKILL escalation
# ---------------------------------------------------------------------------


def test_parent_kills_worker_that_blocks_past_budget_plus_grace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A worker wedged past budget+GRACE is terminated by the parent; _run_single
    returns an aborted LossProfile and never raises."""
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    generation = _generation(workspace)
    # Tiny budget; the SleepingAdapter wedges the worker's event loop with
    # a blocking sleep so its OWN cooperative budget cannot fire.
    entry = _entry(budget_s=1)

    # Shrink the parent's grace margins so the test is fast.
    monkeypatch.setattr(runner_mod, "_PARENT_BUDGET_GRACE_S", 2.0)
    monkeypatch.setattr(runner_mod, "_SIGTERM_TO_SIGKILL_GRACE_S", 2.0)

    started = time.monotonic()
    loss = asyncio.run(
        _run_single(
            adapter=SleepingAdapter(),
            generation=generation,
            entry=entry,
            weights=ScoringWeights(),
            config=_config(workspace),
            workspace_root=workspace,
            epoch_id="e0",
            side="parent",
        )
    )
    elapsed = time.monotonic() - started

    # Aborted, worst-case loss profile so the tournament can still aggregate.
    assert isinstance(loss, LossProfile)
    assert loss.wall_clock_budget_exceeded is True
    assert loss.entry_id == entry.id
    assert loss.drift_loss > 0.0
    # The parent fired at budget(1) + grace(2) = ~3s, not 3600s.
    assert elapsed < 30.0
    # The parent cleaned up the worker's active_runs file.
    assert not active_run_path(workspace, f"{generation.id}--{entry.id}").exists()
    # The per-run ephemeral snapshot working copy is discarded even on
    # the abort path — _run_single's finally block runs unconditionally.
    run_id = f"{generation.id}--{entry.id}"
    leaked = list(
        Path(tempfile.gettempdir()).glob(f"{runner_mod._EPHEMERAL_SNAPSHOT_PREFIX}{run_id}-*")
    )
    assert not leaked, f"ephemeral snapshot not cleaned up after abort: {leaked}"


def test_tournament_continues_after_a_budget_killed_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """One budget-killed entry does NOT abort the whole tournament — a later
    entry still runs to a clean LossProfile.

    Mirrors what ``_run_generation`` does (one ``_run_single`` per entry,
    sequentially): the first entry wedges and is killed, the second runs
    the fast stub. The wedged run must not raise — if it did, the loop
    would never reach the second entry.
    """
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    generation = _generation(workspace)

    monkeypatch.setattr(runner_mod, "_PARENT_BUDGET_GRACE_S", 2.0)
    monkeypatch.setattr(runner_mod, "_SIGTERM_TO_SIGKILL_GRACE_S", 2.0)

    losses: dict[str, LossProfile] = {}
    for entry, adapter in (
        (_entry("entry_wedged", budget_s=1), SleepingAdapter()),
        (_entry("entry_ok", budget_s=60), StubAdapter()),
    ):
        losses[entry.id] = asyncio.run(
            _run_single(
                adapter=adapter,
                generation=generation,
                entry=entry,
                weights=ScoringWeights(),
                config=_config(workspace),
                workspace_root=workspace,
                epoch_id="e0",
                side="parent",
            )
        )

    # The wedged entry aborted; the next entry still produced a profile.
    assert losses["entry_wedged"].wall_clock_budget_exceeded is True
    assert losses["entry_ok"].entry_id == "entry_ok"


# ---------------------------------------------------------------------------
# 3. Supervisor-kill path — worker gone, no result file
# ---------------------------------------------------------------------------


def test_run_single_handles_externally_killed_worker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A worker SIGKILLed externally (the supervisor) mid-run leaves no result
    file; _run_single records an aborted run with no exception."""
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    generation = _generation(workspace)
    entry = _entry(budget_s=3600)  # long budget so neither side's timeout fires

    run_id = f"{generation.id}--{entry.id}"

    # Patch create_subprocess_exec so that as soon as the worker has
    # written its active_runs file we SIGKILL it — simulating exactly
    # what the independent supervisor watchdog does to a wedged run.
    real_create = asyncio.create_subprocess_exec

    async def killing_create(*args: object, **kwargs: object) -> object:
        proc = await real_create(*args, **kwargs)
        run_path = active_run_path(workspace, run_id)

        async def _kill_when_started() -> None:
            deadline = time.monotonic() + 20.0
            while not run_path.exists() and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
            # Simulate the supervisor's SIGKILL on a deadline-exceeded run.
            try:
                os.kill(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

        asyncio.ensure_future(_kill_when_started())  # noqa: RUF006
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", killing_create)

    loss = asyncio.run(
        _run_single(
            adapter=SleepingAdapter(),
            generation=generation,
            entry=entry,
            weights=ScoringWeights(),
            config=_config(workspace),
            workspace_root=workspace,
            epoch_id="e0",
            side="parent",
        )
    )

    # Worker gone + missing result file == a normal aborted run, NOT a
    # crash. The tournament continues.
    assert isinstance(loss, LossProfile)
    assert loss.wall_clock_budget_exceeded is True
    assert loss.entry_id == entry.id
    # The parent cleaned up the active_runs file the killed worker left.
    assert not active_run_path(workspace, run_id).exists()


# ---------------------------------------------------------------------------
# 4. Worker ignores SIGTERM -> parent escalates to SIGKILL
# ---------------------------------------------------------------------------


def test_parent_escalates_to_sigkill_when_worker_ignores_sigterm(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A worker that installs SIG_IGN for SIGTERM survives the parent's
    SIGTERM; the parent escalates to SIGKILL after the grace and _run_single
    still returns an aborted LossProfile."""
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    generation = _generation(workspace)
    entry = _entry(budget_s=1)

    monkeypatch.setattr(runner_mod, "_PARENT_BUDGET_GRACE_S", 2.0)
    monkeypatch.setattr(runner_mod, "_SIGTERM_TO_SIGKILL_GRACE_S", 2.0)

    started = time.monotonic()
    loss = asyncio.run(
        _run_single(
            # ignore_sigterm=True -> the worker installs SIG_IGN for
            # SIGTERM inside the subprocess, so only SIGKILL stops it.
            adapter=SleepingAdapter(ignore_sigterm=True),
            generation=generation,
            entry=entry,
            weights=ScoringWeights(),
            config=_config(workspace),
            workspace_root=workspace,
            epoch_id="e0",
            side="parent",
        )
    )
    elapsed = time.monotonic() - started

    assert isinstance(loss, LossProfile)
    assert loss.wall_clock_budget_exceeded is True
    # budget(1) + grace(2) + sigterm->sigkill grace(2) ≈ 5s; well under 60.
    assert elapsed < 40.0
    assert not active_run_path(workspace, f"{generation.id}--{entry.id}").exists()


# ---------------------------------------------------------------------------
# 5. Worker self-aborts on its own cooperative budget (clean exit)
# ---------------------------------------------------------------------------


def test_worker_cooperative_budget_produces_clean_aborted_result(
    tmp_path: Path,
) -> None:
    """When the worker's OWN asyncio.wait_for budget fires, the worker still
    exits 0 with a result file flagged aborted — the parent reads it back as a
    budget-exceeded LossProfile rather than treating it as a kill."""
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    generation = _generation(workspace)
    # 1s budget; the cooperative-budget adapter sleeps via asyncio.sleep
    # (cancellable), so the worker's own wait_for cancels it cleanly.
    entry = _entry(budget_s=1)

    args_path = tmp_path / "args.json"
    result_path = tmp_path / "result.json"
    _write_args_file(
        args_path,
        workspace=workspace,
        generation=generation,
        entry=entry,
        result_path=result_path,
        adapter_factory="tests._subprocess_worker_support:make_cooperative_adapter",
    )

    proc = _spawn_worker_blocking(args_path)
    assert proc.returncode == 0, "a self-aborted worker still exits cleanly"

    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["aborted"] is True
    assert result["abort_reason"] == "wall_clock_budget"

    # The loss.json the worker wrote carries the budget-exceeded flag.
    from zicato.telemetry.reducer import read_loss_profile  # noqa: PLC0415

    loss = read_loss_profile(Path(result["loss_profile_path"]))
    assert loss.wall_clock_budget_exceeded is True


# ---------------------------------------------------------------------------
# 6. Snapshot isolation — a run that writes near its own code does NOT
#    pollute the canonical generation snapshot.
# ---------------------------------------------------------------------------


def _hash_tree(root: Path) -> str:
    """Return a byte-level digest of a directory tree (paths + file bytes).

    Walks ``root`` deterministically (sorted) and folds every relative
    path and every file's bytes into a single SHA-256 digest. Two trees
    with the same digest are byte-identical in both structure and
    content — the assertion the snapshot-pollution test needs.
    """
    import hashlib  # noqa: PLC0415

    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        digest.update(rel.encode("utf-8"))
        if path.is_file():
            digest.update(b"\0F\0")
            digest.update(path.read_bytes())
        else:
            digest.update(b"\0D\0")
    return digest.hexdigest()


def _canonical_snapshot(workspace: Path, epoch_id: str, gen_id: str) -> Path:
    """Return (and create) a canonical ``generations/{id}/snapshot/`` dir.

    Lays the snapshot down at exactly the workspace path
    :class:`~zicato.epoch.genstore.DirectoryGenerationStore` resolves —
    ``epochs/{epoch}/generations/{gen}/snapshot/`` — with a tiny stub
    ``agent/agent.py`` so the tree is code-only to begin with.
    """
    snap = workspace / "epochs" / epoch_id / "generations" / gen_id / "snapshot"
    (snap / "agent").mkdir(parents=True, exist_ok=True)
    (snap / "agent" / "agent.py").write_text("# stub agent module — code only\n", encoding="utf-8")
    return snap


def test_run_does_not_pollute_canonical_generation_snapshot(tmp_path: Path) -> None:
    """A board-entry run whose agent writes ``output/`` near its own code does
    NOT mutate the canonical generation snapshot.

    The :class:`SnapshotWritingAdapter`'s session writes a runtime
    artifact under the snapshot root it is loaded from — exactly what the
    target-1 presentation agent's ``write_webpage`` tool does. ``_run_single``
    must execute that run against a per-run EPHEMERAL working copy, so:

    * the canonical ``generations/{id}/snapshot/`` tree is byte-identical
      before and after the run (zero pollution), and
    * a subsequent ``derive_generation`` copies a code-only snapshot
      forward — the runtime ``output/`` directory never appears in the
      child generation.
    """
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    epoch_id = "e0"

    snap = _canonical_snapshot(workspace, epoch_id, "v0")
    generation = Generation(
        id="v0",
        epoch_id=epoch_id,
        parent_id=None,
        snapshot_root=snap,
        created_at="2026-05-15T00:00:00Z",
    )
    entry = _entry(budget_s=60)

    # Digest the canonical snapshot BEFORE the run.
    before = _hash_tree(snap)

    loss = asyncio.run(
        _run_single(
            adapter=SnapshotWritingAdapter(),
            generation=generation,
            entry=entry,
            weights=ScoringWeights(),
            config=_config(workspace),
            workspace_root=workspace,
            epoch_id=epoch_id,
            side="parent",
        )
    )
    # The run itself completed and produced a loss profile.
    assert isinstance(loss, LossProfile)
    assert loss.entry_id == entry.id

    # Zero pollution: the canonical snapshot is byte-identical, and the
    # runtime ``output/`` directory the agent wrote is NOT in it.
    assert _hash_tree(snap) == before, "canonical snapshot was mutated by the run"
    assert not (
        snap / "agent" / "output"
    ).exists(), "runtime output/ leaked into the canonical generation snapshot"

    # No ephemeral working copy for THIS run was left behind in the
    # system temp dir — _run_single's finally block discards it. The
    # glob is scoped to this run's run_id so a concurrent test's temp
    # dir cannot cause a false failure.
    run_id = f"{generation.id}--{entry.id}"
    leaked = list(
        Path(tempfile.gettempdir()).glob(f"{runner_mod._EPHEMERAL_SNAPSHOT_PREFIX}{run_id}-*")
    )
    assert not leaked, f"ephemeral snapshot working copies were not cleaned up: {leaked}"

    # A subsequent derive_generation carries a code-only snapshot forward.
    from zicato.epoch.genstore import DirectoryGenerationStore  # noqa: PLC0415

    store = DirectoryGenerationStore(workspace)
    child_root = store.derive_generation(epoch_id, "v0", "v1", patches=[])
    assert (child_root / "agent" / "agent.py").is_file()
    assert not (
        child_root / "agent" / "output"
    ).exists(), "derive_generation carried runtime output/ forward into the child generation"
