"""Tests for ``zicato.tournament.regression`` + runner-side wiring + CLI flag.

The regression-suite gate runs the candidate snapshot's own test
suite. These tests:

* Exercise :class:`RegressionResult` for shape / round-tripping.
* Run :func:`run_regression_suite` against a temp tree with passing
  tests, then with failing tests, then with an artificially-slow test
  to drive the timeout path — the THREE real-subprocess tests, kept
  real because process spawn/kill semantics ARE their contract.
* Drive the pure seams directly (:func:`_resolve_test_root` discovery,
  :func:`_classify_completed_run` summary / exit-code / failed-id
  mapping) with canned layouts + output, no subprocess boot per case.
* Drive :func:`run_tournament` with a stub adapter and verify that a
  regression failure forces a ``"rejected"`` :class:`GateOutcome` with
  the right reason — even when the scoring side would otherwise
  promote.
* Verify the CLI ``--skip-regression`` flag bypasses the check.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import sys
import textwrap
import types
from pathlib import Path
from typing import Any

import pytest

from tests._runtime_builders import runtime_config
from zicato.core import (
    BoardEntry,
    DriftCount,
    ExpectationResult,
    Generation,
    LossProfile,
    RunResult,
    RuntimeConfig,
    ScoringWeights,
)
from zicato.tournament.gate import GateOutcome
from zicato.tournament.regression import (
    RegressionResult,
    _classify_completed_run,
    _resolve_test_root,
    run_regression_suite,
)
from zicato.tournament.runner import TournamentResult, run_tournament

# Use the running interpreter's ``-m pytest`` so the subprocess works
# regardless of whether ``pytest`` is on PATH (uv-style ``.venv/bin``
# layouts hide it from a vanilla subprocess env).
_PYTEST_CMD: tuple[str, ...] = (
    sys.executable,
    "-m",
    "pytest",
    "tests/",
    "-q",
    "--tb=line",
)


# ---------------------------------------------------------------------------
# RegressionResult round-trip
# ---------------------------------------------------------------------------


def test_regression_result_is_frozen_and_round_trips() -> None:
    """The dataclass is frozen and ``asdict`` round-trips cleanly."""
    r = RegressionResult(
        passed=False,
        failed_tests=("tests/test_x.py::test_y", "tests/test_x.py::test_z"),
        summary="2 tests failed",
        elapsed_s=1.5,
    )

    with pytest.raises(dataclasses.FrozenInstanceError):
        r.passed = True  # type: ignore[misc]

    payload = dataclasses.asdict(r)
    assert payload == {
        "passed": False,
        "failed_tests": ("tests/test_x.py::test_y", "tests/test_x.py::test_z"),
        "summary": "2 tests failed",
        "elapsed_s": 1.5,
    }
    # JSON-friendly with default=str (Path/tuple coverage already exercised).
    assert json.loads(json.dumps(payload, default=str))["passed"] is False


# ---------------------------------------------------------------------------
# run_regression_suite — discovery + happy / sad paths
# ---------------------------------------------------------------------------


def _make_snapshot_with_test(tmp_path: Path, test_body: str, *, subdir: str | None = None) -> Path:
    """Create a snapshot layout with one ``tests/test_x.py`` file.

    When ``subdir`` is set, the tests live at
    ``snapshot_root/<subdir>/tests/test_x.py`` (the mutable-tree shape);
    otherwise they live directly under ``snapshot_root/tests/``.
    """
    snapshot_root = tmp_path / "snapshot"
    test_root = snapshot_root if subdir is None else snapshot_root / subdir
    tests_dir = test_root / "tests"
    tests_dir.mkdir(parents=True)
    (tests_dir / "__init__.py").write_text("", encoding="utf-8")
    (tests_dir / "test_x.py").write_text(textwrap.dedent(test_body), encoding="utf-8")
    return snapshot_root


def test_run_regression_suite_returns_passed_when_no_tests_dir(tmp_path: Path) -> None:
    """A snapshot without a tests/ directory yields a silent skip."""
    snapshot_root = tmp_path / "snapshot"
    snapshot_root.mkdir()
    # Put a stray file so the directory exists but lacks tests/.
    (snapshot_root / "README.txt").write_text("nothing here", encoding="utf-8")

    result = asyncio.run(run_regression_suite(snapshot_root))

    assert result.passed is True
    assert result.failed_tests == ()
    assert "no tests/ directory" in result.summary
    assert result.elapsed_s == 0.0


@pytest.mark.integration
def test_run_regression_suite_passes_on_green_suite(tmp_path: Path) -> None:
    """A snapshot whose pytest suite passes yields ``passed=True``."""
    snapshot_root = _make_snapshot_with_test(
        tmp_path,
        """
        def test_truth():
            assert 1 + 1 == 2
        """,
    )

    result = asyncio.run(run_regression_suite(snapshot_root, test_command=_PYTEST_CMD))

    assert result.passed is True
    assert result.failed_tests == ()
    assert result.summary == "all tests passed"
    assert result.elapsed_s > 0.0


@pytest.mark.integration
def test_run_regression_suite_fails_with_failed_ids(tmp_path: Path) -> None:
    """A failing pytest run yields ``passed=False`` + populated failed ids."""
    snapshot_root = _make_snapshot_with_test(
        tmp_path,
        """
        def test_one():
            assert False, "boom"

        def test_two():
            assert 0 == 1
        """,
    )

    result = asyncio.run(run_regression_suite(snapshot_root, test_command=_PYTEST_CMD))

    assert result.passed is False
    assert len(result.failed_tests) == 2
    # Both failed ids contain the test name; pytest formats them as
    # ``tests/test_x.py::test_one`` etc.
    assert any("test_one" in tid for tid in result.failed_tests)
    assert any("test_two" in tid for tid in result.failed_tests)
    assert result.summary == "2 tests failed"


def test_resolve_test_root_locates_tests_under_mutable_tree_subdir(
    tmp_path: Path,
) -> None:
    """When the snapshot wraps a goldfive-style checkout the tests still get found.

    Discovery is a pure path decision (:func:`_resolve_test_root`), so it is
    asserted directly instead of booting a real pytest child per layout —
    the green/failing tests above already prove the resolved root is handed
    to a real subprocess correctly.
    """
    snapshot_root = _make_snapshot_with_test(
        tmp_path,
        """
        def test_ok():
            assert True
        """,
        subdir="goldfive",
    )

    # The goldfive-style shape resolves to the child checkout dir.
    assert _resolve_test_root(snapshot_root) == snapshot_root / "goldfive"

    # A tests/ dir directly under the snapshot root wins over any child.
    direct = snapshot_root / "tests"
    direct.mkdir()
    assert _resolve_test_root(snapshot_root) == snapshot_root

    # No tests/ anywhere -> None (run_regression_suite silently skips).
    bare = tmp_path / "bare"
    bare.mkdir()
    assert _resolve_test_root(bare) is None


def test_classify_completed_run_maps_output_and_exit_codes() -> None:
    """The parse seam maps canned pytest stdout + exit codes exactly.

    Drives :func:`_classify_completed_run` — the pure classification layer
    ``run_regression_suite`` delegates to after ``communicate()`` — so the
    summary wording / exit-code mapping / failed-id extraction are covered
    without a subprocess boot per case.
    """
    # Exit 0 -> passed, regardless of chatter in the output.
    ok = _classify_completed_run("....\n4 passed in 0.10s\n", 0, 0.1)
    assert ok.passed is True
    assert ok.failed_tests == ()
    assert ok.summary == "all tests passed"
    assert ok.elapsed_s == 0.1

    # Non-zero exit with FAILED lines -> ids extracted, counted summary.
    output = (
        "FAILED tests/test_x.py::test_one - AssertionError: boom\n"
        "FAILED tests/test_x.py::test_two - assert 0 == 1\n"
        "FAILED tests/test_x.py::test_one - AssertionError: boom\n"  # dupe squashed
        "2 failed, 1 passed in 0.20s\n"
    )
    failed = _classify_completed_run(output, 1, 0.2)
    assert failed.passed is False
    assert failed.failed_tests == (
        "tests/test_x.py::test_one",
        "tests/test_x.py::test_two",
    )
    assert failed.summary == "2 tests failed"

    # Non-zero exit with NO FAILED lines (collection error, crash) -> the
    # exit code itself is the summary and no ids are invented.
    crashed = _classify_completed_run("INTERNALERROR> boom\n", 3, 0.05)
    assert crashed.passed is False
    assert crashed.failed_tests == ()
    assert crashed.summary == "pytest exit code 3"


@pytest.mark.integration
def test_run_regression_suite_times_out_on_slow_test(tmp_path: Path) -> None:
    """A test that outlives the timeout maps to ``passed=False`` w/ timeout summary."""
    snapshot_root = _make_snapshot_with_test(
        tmp_path,
        """
        import time

        # Outlives the 1s timeout below (the process is killed at the
        # deadline, so the exact duration only needs to exceed it).
        def test_slow():
            time.sleep(5)
        """,
    )

    result = asyncio.run(run_regression_suite(snapshot_root, test_command=_PYTEST_CMD, timeout_s=1))

    assert result.passed is False
    assert result.failed_tests == ()
    assert "timeout" in result.summary
    assert "1s" in result.summary


# ---------------------------------------------------------------------------
# Runner integration — regression gate supersedes scoring gate
# ---------------------------------------------------------------------------


def _loss(
    generation_id: str,
    entry_id: str,
    drift_loss: float,
    pass_fail: bool | None,
) -> LossProfile:
    expectation = (
        ExpectationResult(kind="predicate", passed=bool(pass_fail))
        if pass_fail is not None
        else None
    )
    return LossProfile(
        run_id=f"run-{generation_id}-{entry_id}",
        entry_id=entry_id,
        generation_id=generation_id,
        epoch_id="e0",
        drift_counts=(DriftCount(kind="off_topic", severity="info", count=0),),
        plan_revisions=0,
        task_failure_ratio=0.0,
        runtime_ms=1000,
        wall_clock_budget_exceeded=False,
        expectation_result=expectation,
        drift_loss=drift_loss,
        pass_fail=pass_fail,
    )


def _install_run_single_stub(
    monkeypatch: pytest.MonkeyPatch,
    canned: dict[tuple[str, str], LossProfile],
) -> None:
    """Replace ``runner._run_single`` with a canned-loss lookup.

    Since the L3 subprocess-isolation refactor the per-entry run mechanism
    spawns a worker process; the regression-gate tests only care about the
    *generation-level* gate wiring, so they bypass the subprocess entirely
    by stubbing ``_run_single`` with a deterministic loss lookup.
    """
    import zicato.tournament.runner as runner_mod  # noqa: PLC0415

    async def fake_run_single(
        *,
        adapter: Any,
        generation: Generation,
        entry: BoardEntry,
        weights: ScoringWeights,
        config: RuntimeConfig,
        workspace_root: Path,
        epoch_id: str,
        side: str,
        match_id: str = "",
    ) -> LossProfile:
        del adapter, weights, config, workspace_root, epoch_id, side, match_id
        return canned[(generation.id, entry.id)]

    monkeypatch.setattr(runner_mod, "_run_single", fake_run_single)


def _set_regression_result(monkeypatch: pytest.MonkeyPatch, regression: RegressionResult) -> None:
    """Force ``run_regression_suite`` to return a canned result."""

    async def fake_run(snapshot_root: Path, **kwargs: Any) -> RegressionResult:
        del snapshot_root, kwargs
        return regression

    # Patch the symbol the runner imported (function reference is bound
    # at runner import time, so we patch the runner module's view).
    monkeypatch.setattr("zicato.tournament.runner.run_regression_suite", fake_run)


def _board() -> list[BoardEntry]:
    return [
        BoardEntry(
            id="entry_a",
            kind="single_turn",
            wall_clock_budget_seconds=60,
            input="hello",
        ),
    ]


def test_run_tournament_rejects_when_regression_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A child with a perfect scoring delta is rejected when the regression
    suite fails."""
    parent_gen = Generation(
        id="v0",
        epoch_id="e0",
        parent_id=None,
        snapshot_root=tmp_path / "snap_v0",
        created_at="2024-01-01T00:00:00Z",
    )
    child_gen = Generation(
        id="v1",
        epoch_id="e0",
        parent_id="v0",
        snapshot_root=tmp_path / "snap_v1",
        created_at="2024-01-02T00:00:00Z",
    )

    # Scoring side: child clearly beats parent (lower drift, higher pass).
    canned = {
        ("v0", "entry_a"): _loss("v0", "entry_a", drift_loss=5.0, pass_fail=False),
        ("v1", "entry_a"): _loss("v1", "entry_a", drift_loss=0.0, pass_fail=True),
    }
    _install_run_single_stub(monkeypatch, canned)

    # But the regression suite is busted on the child snapshot.
    _set_regression_result(
        monkeypatch,
        RegressionResult(
            passed=False,
            failed_tests=("tests/test_core.py::test_a", "tests/test_core.py::test_b"),
            summary="2 tests failed",
            elapsed_s=0.4,
        ),
    )

    weights = ScoringWeights(
        promote_margin=0.01,
        regression_gate_enabled=True,
    )

    result = asyncio.run(
        run_tournament(
            adapter=object(),
            parent_gen=parent_gen,
            child_gen=child_gen,
            board=_board(),
            weights=weights,
            config=runtime_config(tmp_path),
            workspace_root=tmp_path,
            epoch_id="e0",
        )
    )

    assert result.outcome.decision == "rejected"
    assert "regression suite failed" in result.outcome.reason
    assert "2 tests" in result.outcome.reason
    # Scoring deltas are still recorded for the journal.
    assert result.outcome.delta_scalar < 0
    assert result.outcome.delta_pass_rate == 1.0


def test_run_tournament_promotes_when_regression_passes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A green regression suite hands control back to the scoring gate."""
    parent_gen = Generation(
        id="v0",
        epoch_id="e0",
        parent_id=None,
        snapshot_root=tmp_path / "snap_v0",
        created_at="2024-01-01T00:00:00Z",
    )
    child_gen = Generation(
        id="v1",
        epoch_id="e0",
        parent_id="v0",
        snapshot_root=tmp_path / "snap_v1",
        created_at="2024-01-02T00:00:00Z",
    )

    canned = {
        ("v0", "entry_a"): _loss("v0", "entry_a", drift_loss=5.0, pass_fail=True),
        ("v1", "entry_a"): _loss("v1", "entry_a", drift_loss=0.0, pass_fail=True),
    }
    _install_run_single_stub(monkeypatch, canned)
    _set_regression_result(
        monkeypatch,
        RegressionResult(
            passed=True,
            failed_tests=(),
            summary="all tests passed",
            elapsed_s=0.3,
        ),
    )

    weights = ScoringWeights(
        promote_margin=0.01,
        regression_gate_enabled=True,
    )

    result = asyncio.run(
        run_tournament(
            adapter=object(),
            parent_gen=parent_gen,
            child_gen=child_gen,
            board=_board(),
            weights=weights,
            config=runtime_config(tmp_path),
            workspace_root=tmp_path,
            epoch_id="e0",
        )
    )

    assert result.outcome.decision == "promoted"
    assert result.outcome.reason == ""


def test_run_tournament_skips_regression_when_flag_off(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``regression_gate_enabled=False`` (default) leaves run_regression_suite
    unused — even if it would fail."""
    parent_gen = Generation(
        id="v0",
        epoch_id="e0",
        parent_id=None,
        snapshot_root=tmp_path / "snap_v0",
        created_at="2024-01-01T00:00:00Z",
    )
    child_gen = Generation(
        id="v1",
        epoch_id="e0",
        parent_id="v0",
        snapshot_root=tmp_path / "snap_v1",
        created_at="2024-01-02T00:00:00Z",
    )

    canned = {
        ("v0", "entry_a"): _loss("v0", "entry_a", drift_loss=5.0, pass_fail=True),
        ("v1", "entry_a"): _loss("v1", "entry_a", drift_loss=0.0, pass_fail=True),
    }
    _install_run_single_stub(monkeypatch, canned)

    called = {"hit": False}

    async def fake_run(snapshot_root: Path, **kwargs: Any) -> RegressionResult:
        del snapshot_root, kwargs
        called["hit"] = True
        return RegressionResult(False, ("x",), "1 tests failed", 0.1)

    monkeypatch.setattr("zicato.tournament.runner.run_regression_suite", fake_run)

    weights = ScoringWeights(promote_margin=0.01)  # regression_gate_enabled=False
    result = asyncio.run(
        run_tournament(
            adapter=object(),
            parent_gen=parent_gen,
            child_gen=child_gen,
            board=_board(),
            weights=weights,
            config=runtime_config(tmp_path),
            workspace_root=tmp_path,
            epoch_id="e0",
        )
    )

    assert called["hit"] is False
    assert result.outcome.decision == "promoted"


# ---------------------------------------------------------------------------
# CLI flag — --skip-regression flips the weights' opt-in off
# ---------------------------------------------------------------------------


def _make_cli_stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wire CLI-side stubs so ``tournament_cmd`` runs without a real workspace."""
    loader_mod = types.SimpleNamespace(
        load_workspace_config=lambda root: {"mutable_trees": []},
        load_current_board=lambda root: _board(),
        # The CLI threads board-level disable_drift + judge_only, so it
        # loads the board via load_current_board_with_meta; this board has
        # no board_meta header, so the suppression tuple is empty and
        # judge_only is False (steering on, the default).
        load_current_board_with_meta=lambda root: (_board(), (), False),
        load_current_scoring=lambda root: ScoringWeights(regression_gate_enabled=True),
    )
    adapter_factory_mod = types.SimpleNamespace(
        make_adapter_from_config=lambda cfg: object(),
    )
    runtime_factory_mod = types.SimpleNamespace(
        make_runtime_config=lambda cfg, *, workspace_root: runtime_config(workspace_root),
    )
    monkeypatch.setattr(
        "zicato.cli.commands.tournament._resolve_workspace_components",
        lambda: (loader_mod, adapter_factory_mod, runtime_factory_mod),
    )


def test_cli_skip_regression_bypasses_gate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """When ``--skip-regression`` is passed, the CLI mutates weights so the
    runner takes the fast path even if scoring.json enabled the gate."""
    from click.testing import CliRunner

    from zicato.cli.commands.tournament import tournament_cmd

    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "current_epoch").write_text("e0", encoding="utf-8")
    snap_v0 = workspace / "epochs" / "e0" / "generations" / "v0" / "snapshot"
    snap_v1 = workspace / "epochs" / "e0" / "generations" / "v1" / "snapshot"
    snap_v0.mkdir(parents=True)
    snap_v1.mkdir(parents=True)

    _make_cli_stubs(monkeypatch)

    captured: dict[str, ScoringWeights] = {}

    async def fake_run_tournament(**kwargs: Any) -> Any:
        captured["w"] = kwargs["weights"]
        return TournamentResult(
            parent_generation_id="v0",
            child_generation_id="v1",
            parent_agg={"scalar": 1.0, "pass_rate": 1.0},
            child_agg={"scalar": 0.0, "pass_rate": 1.0},
            outcome=GateOutcome(
                decision="promoted",
                reason="",
                delta_scalar=-1.0,
                delta_pass_rate=0.0,
            ),
            per_entry_losses={},
        )

    monkeypatch.setattr("zicato.tournament.run_tournament", fake_run_tournament)

    runner = CliRunner()
    res = runner.invoke(
        tournament_cmd,
        ["v0", "v1", "--workspace", str(workspace), "--skip-regression"],
        catch_exceptions=False,
    )

    assert res.exit_code == 0, res.output
    assert "w" in captured
    assert captured["w"].regression_gate_enabled is False


def test_cli_keeps_regression_flag_when_not_skipped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Without ``--skip-regression`` the CLI passes weights through verbatim."""
    from click.testing import CliRunner

    from zicato.cli.commands.tournament import tournament_cmd

    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "current_epoch").write_text("e0", encoding="utf-8")
    snap_v0 = workspace / "epochs" / "e0" / "generations" / "v0" / "snapshot"
    snap_v1 = workspace / "epochs" / "e0" / "generations" / "v1" / "snapshot"
    snap_v0.mkdir(parents=True)
    snap_v1.mkdir(parents=True)

    _make_cli_stubs(monkeypatch)

    captured: dict[str, ScoringWeights] = {}

    async def fake_run_tournament(**kwargs: Any) -> Any:
        captured["w"] = kwargs["weights"]
        return TournamentResult(
            parent_generation_id="v0",
            child_generation_id="v1",
            parent_agg={"scalar": 1.0, "pass_rate": 1.0},
            child_agg={"scalar": 0.0, "pass_rate": 1.0},
            outcome=GateOutcome(
                decision="promoted",
                reason="",
                delta_scalar=-1.0,
                delta_pass_rate=0.0,
            ),
            per_entry_losses={},
        )

    monkeypatch.setattr("zicato.tournament.run_tournament", fake_run_tournament)

    runner = CliRunner()
    res = runner.invoke(
        tournament_cmd,
        ["v0", "v1", "--workspace", str(workspace)],
        catch_exceptions=False,
    )

    assert res.exit_code == 0, res.output
    assert captured["w"].regression_gate_enabled is True


# Silence unused-import warnings on RunResult — exported for parity with the
# wider tournament-test fixtures even if the regression tests don't construct
# one directly.
_ = RunResult
