"""Tests for the conservative crash-resume protocol (``runtime/resume.py``).

These tests build a *simulated interrupted workspace* on disk — a
generation directory carrying some mix of ``experiment.json``,
``snapshot/``, and per-entry ``loss.json`` markers — and assert that
:func:`zicato.runtime.resume.prepare_resume` reconciles it exactly as
RUNTIME.md §4.2 specifies:

* a tournament interrupted *with completed units on disk* resumes in
  place (reuse the persisted experiment, do not discard the loss cache);
* any ambiguity (no experiment, unapplied snapshot, no completed units,
  garbled marker) discards the partial generation so the round re-runs
  fresh;
* a clean workspace (nothing interrupted) yields the no-op plan, so a
  cold start is byte-identical to today.

They exercise the resume module directly (no LLM, no tournament), so
they are fast and hermetic. The end-to-end "resume re-enters the loop
and the unit cache hits the done units" path is covered in
``tests/test_orchestrator_resume.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

from zicato.core.types import (
    Experiment,
    HypothesisSpec,
    LossProfile,
    Patch,
)
from zicato.runtime.paths import (
    active_runs_dir,
    active_tournament_path,
    ensure_runtime_dirs,
    heartbeat_path,
)
from zicato.runtime.resume import ResumePlan, clear_runtime_state, prepare_resume
from zicato.telemetry.reducer import write_loss_profile

EPOCH = "alpha"


# ---------------------------------------------------------------------------
# Workspace fixture helpers — pure on-disk shapes, no orchestrator.
# ---------------------------------------------------------------------------


def _gen_dir(workspace: Path, generation_id: str) -> Path:
    return workspace / "epochs" / EPOCH / "generations" / generation_id


def _make_experiment(generation_id: str, *, with_outcome: bool = False) -> Experiment:
    """Build a minimal, self-consistent :class:`Experiment`."""
    from zicato.core.types import OutcomeRecord

    patch = Patch(
        id="p0",
        mutation_id="greeting",
        op="replace",
        new_content='GREETING = "hi"',
        new_numeric=None,
        new_enum=None,
        rationale="tighten greeting",
    )
    outcome = (
        OutcomeRecord(
            ran_at="2026-06-09T00:00:00Z",
            drift_movements=(),
            pass_rate_delta=0.0,
            drift_loss_delta=0.0,
            scalar_score_delta=0.0,
            tournament_decision="rejected",
            rejection_reason="",
        )
        if with_outcome
        else None
    )
    return Experiment(
        id=f"exp_{EPOCH}_{generation_id}",
        epoch_id=EPOCH,
        generation_id=generation_id,
        parent_generation_id="v0",
        proposed_at="2026-06-09T00:00:00Z",
        hypothesis=HypothesisSpec(
            core_idea="x",
            modulating=("greeting",),
            why="y",
            expected_drift_movements=(),
            expected_pass_rate_delta="flat",
        ),
        patches=(patch,),
        outcome=outcome,
    )


def _write_experiment(workspace: Path, generation_id: str, *, with_outcome: bool = False) -> None:
    from zicato.epoch.journal import write_experiment

    write_experiment(
        workspace,
        EPOCH,
        generation_id,
        _make_experiment(generation_id, with_outcome=with_outcome),
    )


def _write_snapshot(workspace: Path, generation_id: str) -> None:
    snap = _gen_dir(workspace, generation_id) / "snapshot"
    snap.mkdir(parents=True, exist_ok=True)
    (snap / "agent.py").write_text('GREETING = "hi"\n')


def _write_loss(workspace: Path, generation_id: str, entry_id: str) -> None:
    from zicato.core.types import DriftCount

    loss = LossProfile(
        run_id=f"r-{generation_id}-{entry_id}",
        entry_id=entry_id,
        generation_id=generation_id,
        epoch_id=EPOCH,
        drift_counts=(DriftCount(kind="off_topic", severity="info", count=0),),
        plan_revisions=0,
        task_failure_ratio=0.0,
        runtime_ms=10,
        wall_clock_budget_exceeded=False,
        expectation_result=None,
        drift_loss=0.1,
        pass_fail=True,
    )
    path = _gen_dir(workspace, generation_id) / "runs" / entry_id / "loss.json"
    write_loss_profile(loss, path)


def _seed_v0(workspace: Path) -> None:
    """Create a v0 baseline so the latest-generation logic has a floor."""
    _write_snapshot(workspace, "v0")


# ---------------------------------------------------------------------------
# Clean workspace — byte-identical no-op.
# ---------------------------------------------------------------------------


def test_clean_workspace_is_noop(tmp_path: Path) -> None:
    """No interrupted generation → the default no-op plan."""
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    _seed_v0(workspace)

    plan = prepare_resume(workspace, EPOCH)

    assert plan == ResumePlan(classification="clean")
    assert plan.resume_generation_id is None
    assert plan.discarded_generation_id is None
    assert not plan.resumes_in_place


def test_no_generations_at_all_is_noop(tmp_path: Path) -> None:
    """An epoch with no generations directory at all → clean."""
    workspace = tmp_path / ".zicato"
    workspace.mkdir()

    plan = prepare_resume(workspace, EPOCH)

    assert plan.classification == "clean"
    assert not plan.resumes_in_place


def test_latest_generation_already_outcomed_is_noop(tmp_path: Path) -> None:
    """A finished round (latest gen carries an outcome) → nothing to resume."""
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    _seed_v0(workspace)
    _write_snapshot(workspace, "v1")
    _write_experiment(workspace, "v1", with_outcome=True)

    plan = prepare_resume(workspace, EPOCH)

    assert plan.classification == "clean"
    # The finished generation is NOT discarded.
    assert _gen_dir(workspace, "v1").is_dir()


# ---------------------------------------------------------------------------
# The single safe-and-free resume case.
# ---------------------------------------------------------------------------


def test_interrupted_tournament_with_loss_resumes_in_place(tmp_path: Path) -> None:
    """experiment (no outcome) + snapshot + >=1 loss.json → resume in place."""
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    _seed_v0(workspace)
    _write_experiment(workspace, "v1", with_outcome=False)
    _write_snapshot(workspace, "v1")
    _write_loss(workspace, "v1", "entry_a")  # one completed board unit

    plan = prepare_resume(workspace, EPOCH)

    assert plan.classification == "resume_tournament"
    assert plan.resume_generation_id == "v1"
    assert plan.resume_experiment is not None
    assert plan.resumes_in_place
    # The persisted patches survive the round-trip (the cache-soundness
    # invariant: the same patches that produced the cached loss.json).
    assert plan.resume_experiment.patches[0].mutation_id == "greeting"
    # NOTHING is discarded — the completed loss.json must survive to be a
    # cache HIT on resume.
    assert plan.discarded_generation_id is None
    assert (_gen_dir(workspace, "v1") / "runs" / "entry_a" / "loss.json").is_file()


# ---------------------------------------------------------------------------
# Discard-on-ambiguity cases (RUNTIME.md §4.2 conservatism).
# ---------------------------------------------------------------------------


def test_applied_but_no_units_ran_discards(tmp_path: Path) -> None:
    """experiment + snapshot but NO loss.json → discard and re-run fresh."""
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    _seed_v0(workspace)
    _write_experiment(workspace, "v1", with_outcome=False)
    _write_snapshot(workspace, "v1")
    # No loss.json — nothing completed.

    plan = prepare_resume(workspace, EPOCH)

    assert plan.classification == "discard_no_progress"
    assert plan.discarded_generation_id == "v1"
    assert not plan.resumes_in_place
    # The interrupted directory is gone so _next_generation_id re-picks v1.
    assert not _gen_dir(workspace, "v1").exists()


def test_proposed_but_not_applied_discards(tmp_path: Path) -> None:
    """experiment present but NO snapshot/ → discard and re-run fresh."""
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    _seed_v0(workspace)
    _write_experiment(workspace, "v1", with_outcome=False)
    # No snapshot directory.

    plan = prepare_resume(workspace, EPOCH)

    assert plan.classification == "discard_unapplied"
    assert plan.discarded_generation_id == "v1"
    assert not _gen_dir(workspace, "v1").exists()


def test_partial_proposal_no_experiment_discards(tmp_path: Path) -> None:
    """A generation directory exists but has no experiment.json → discard."""
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    _seed_v0(workspace)
    # A snapshot left from a crashed derive, but no experiment marker.
    _write_snapshot(workspace, "v1")

    plan = prepare_resume(workspace, EPOCH)

    assert plan.classification == "discard_partial_proposal"
    assert plan.discarded_generation_id == "v1"
    assert not _gen_dir(workspace, "v1").exists()


def test_garbled_experiment_marker_discards(tmp_path: Path) -> None:
    """A present-but-unreadable experiment.json → discard (garbled)."""
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    _seed_v0(workspace)
    gen = _gen_dir(workspace, "v1")
    gen.mkdir(parents=True)
    _write_snapshot(workspace, "v1")
    _write_loss(workspace, "v1", "entry_a")  # even with a completed unit...
    # ...a torn / garbled experiment.json forces a conservative discard.
    (gen / "experiment.json").write_text("{ this is not valid json")

    plan = prepare_resume(workspace, EPOCH)

    assert plan.classification == "discard_garbled"
    assert plan.discarded_generation_id == "v1"
    # The whole directory (including the otherwise-cacheable loss.json) is
    # discarded — we never trust a unit cache we cannot tie to a readable
    # experiment.
    assert not gen.exists()


def test_dangling_patch_reference_discards(tmp_path: Path) -> None:
    """experiment.json references a patch file that does not exist → discard."""
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    _seed_v0(workspace)
    gen = _gen_dir(workspace, "v1")
    gen.mkdir(parents=True)
    _write_snapshot(workspace, "v1")
    _write_loss(workspace, "v1", "entry_a")
    # A well-formed experiment.json that points at a missing patch file —
    # read_experiment raises, and the conservative protocol discards.
    (gen / "experiment.json").write_text(
        json.dumps(
            {
                "id": "exp_alpha_v1",
                "epoch_id": EPOCH,
                "generation_id": "v1",
                "parent_generation_id": "v0",
                "proposed_at": "2026-06-09T00:00:00Z",
                "hypothesis": {
                    "core_idea": "x",
                    "modulating": ["greeting"],
                    "why": "y",
                    "expected_drift_movements": [],
                    "expected_pass_rate_delta": "flat",
                    "risks": "",
                    "expected_metric_movements": [],
                },
                "patch_ids": ["does_not_exist"],
                "outcome": None,
            }
        )
    )

    plan = prepare_resume(workspace, EPOCH)

    assert plan.classification == "discard_garbled"
    assert plan.discarded_generation_id == "v1"
    assert not gen.exists()


# ---------------------------------------------------------------------------
# Stale runtime-state cleanup.
# ---------------------------------------------------------------------------


def test_clear_runtime_state_removes_live_files(tmp_path: Path) -> None:
    """heartbeat / active_tournament / active_runs are cleared; lock is not."""
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    ensure_runtime_dirs(workspace)
    heartbeat_path(workspace).write_text("{}")
    active_tournament_path(workspace).write_text("{}")
    (active_runs_dir(workspace) / "run_a.json").write_text("{}")
    lock = workspace / "runtime" / "lock.json"
    lock.write_text("{}")

    clear_runtime_state(workspace)

    assert not heartbeat_path(workspace).exists()
    assert not active_tournament_path(workspace).exists()
    assert not (active_runs_dir(workspace) / "run_a.json").exists()
    # The lock is owned by the live orchestrator — never cleared here.
    assert lock.exists()


def test_prepare_resume_clears_runtime_state(tmp_path: Path) -> None:
    """prepare_resume always clears stale runtime/ even on a clean epoch."""
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    _seed_v0(workspace)
    ensure_runtime_dirs(workspace)
    heartbeat_path(workspace).write_text("{}")
    (active_runs_dir(workspace) / "run_a.json").write_text("{}")

    prepare_resume(workspace, EPOCH)

    assert not heartbeat_path(workspace).exists()
    assert not (active_runs_dir(workspace) / "run_a.json").exists()
