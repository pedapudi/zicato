"""Tests for :mod:`zicato.health` — loop-health diagnostics.

Covers each detector in isolation (fires when it should, silent when it
should not), the :class:`LoopHealth.healthy` rollup rule, and a CLI
smoke test through :class:`click.testing.CliRunner`.
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from zicato.cli.commands.health import health_cmd
from zicato.core.types import (
    BoardEntry,
    DriftCount,
    Expectation,
    JudgeSpec,
    LossProfile,
)
from zicato.health.diagnostics import (
    HealthFinding,
    assess_loop_health,
    detect_dead_judge,
    detect_degenerate_scoring,
    detect_flat_drift_signal,
    detect_generalization_gap,
    detect_no_expectations,
    detect_non_differentiating_entry,
    detect_refresh_cadence,
    detect_stalled_loop,
    detect_tree_never_imported,
)

# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------


def _loss(
    entry_id: str,
    generation_id: str,
    *,
    drift_loss: float = 0.0,
    drift_counts: tuple[DriftCount, ...] = (),
    pass_fail: bool | None = None,
) -> LossProfile:
    """Build a minimal :class:`LossProfile` for detector tests."""
    return LossProfile(
        run_id=f"run_{generation_id}_{entry_id}",
        entry_id=entry_id,
        generation_id=generation_id,
        epoch_id="e1",
        drift_counts=drift_counts,
        plan_revisions=0,
        task_failure_ratio=0.0,
        runtime_ms=1000,
        wall_clock_budget_exceeded=False,
        expectation_result=None,
        drift_loss=drift_loss,
        pass_fail=pass_fail,
    )


def _experiment(generation_id: str, *, scalar_delta: float | None, decision: str) -> dict:
    """Build an ``experiment.json``-shaped dict with a tournament outcome."""
    outcome = None
    if scalar_delta is not None:
        outcome = {
            "ran_at": "2026-05-15T00:00:00Z",
            "drift_movements": [],
            "pass_rate_delta": 0.0,
            "drift_loss_delta": 0.0,
            "scalar_score_delta": scalar_delta,
            "tournament_decision": decision,
            "rejection_reason": "" if decision != "rejected" else "no_improvement",
        }
    return {"generation_id": generation_id, "outcome": outcome}


def _gap_experiment(
    generation_id: str,
    *,
    train_loss: float | None,
    holdout_loss: float | None,
) -> dict:
    """Build an ``experiment.json``-shaped dict carrying per-gen loss fields.

    ``holdout_loss`` of ``None`` models the no-holdout degrade — both the
    holdout loss and the gap are absent on the outcome.
    """
    gap = None if (train_loss is None or holdout_loss is None) else holdout_loss - train_loss
    return {
        "generation_id": generation_id,
        "outcome": {
            "ran_at": "2026-05-15T00:00:00Z",
            "drift_movements": [],
            "pass_rate_delta": 0.0,
            "drift_loss_delta": 0.0,
            "scalar_score_delta": 0.0,
            "tournament_decision": "promoted",
            "rejection_reason": "",
            "train_loss": train_loss,
            "holdout_loss": holdout_loss,
            "generalization_gap": gap,
        },
    }


def _board_entry(entry_id: str, *, with_expectation: bool) -> BoardEntry:
    """Build a single-turn :class:`BoardEntry`, optionally with an expectation."""
    expectation = None
    if with_expectation:
        expectation = Expectation(kind="expected_text", spec="ok")
    return BoardEntry(
        id=entry_id,
        kind="single_turn",
        wall_clock_budget_seconds=60,
        input="hello",
        expectation=expectation,
    )


# ---------------------------------------------------------------------------
# degenerate_scoring
# ---------------------------------------------------------------------------


def test_degenerate_scoring_fires_on_three_zero_delta_tournaments() -> None:
    experiments = [
        _experiment("v1", scalar_delta=0.0, decision="rejected"),
        _experiment("v2", scalar_delta=0.0, decision="rejected"),
        _experiment("v3", scalar_delta=0.0, decision="rejected"),
    ]
    findings = detect_degenerate_scoring(experiments)
    assert len(findings) == 1
    assert findings[0].code == "degenerate_scoring"
    assert findings[0].severity == "critical"
    assert findings[0].detail["generation_ids"] == ["v1", "v2", "v3"]


def test_degenerate_scoring_silent_on_a_real_delta() -> None:
    experiments = [
        _experiment("v1", scalar_delta=0.0, decision="rejected"),
        _experiment("v2", scalar_delta=0.0, decision="rejected"),
        _experiment("v3", scalar_delta=-0.25, decision="promoted"),
    ]
    assert detect_degenerate_scoring(experiments) == []


def test_degenerate_scoring_silent_below_window() -> None:
    # Only two evaluated tournaments — fewer than the default window of 3.
    experiments = [
        _experiment("v1", scalar_delta=0.0, decision="rejected"),
        _experiment("v2", scalar_delta=0.0, decision="rejected"),
    ]
    assert detect_degenerate_scoring(experiments) == []


def test_degenerate_scoring_ignores_unevaluated_experiments() -> None:
    # An experiment with no outcome carries no signal and is skipped;
    # the three evaluated ones still trip the detector.
    experiments = [
        _experiment("v1", scalar_delta=None, decision="rejected"),
        _experiment("v2", scalar_delta=0.0, decision="rejected"),
        _experiment("v3", scalar_delta=0.0, decision="rejected"),
        _experiment("v4", scalar_delta=0.0, decision="rejected"),
    ]
    findings = detect_degenerate_scoring(experiments)
    assert len(findings) == 1
    assert findings[0].detail["generation_ids"] == ["v2", "v3", "v4"]


# ---------------------------------------------------------------------------
# non_differentiating_entry
# ---------------------------------------------------------------------------


def test_non_differentiating_entry_flags_identical_loss_everywhere() -> None:
    losses_by_generation = {
        "v0": [_loss("dead", "v0", drift_loss=1.0), _loss("live", "v0", drift_loss=2.0)],
        "v1": [_loss("dead", "v1", drift_loss=1.0), _loss("live", "v1", drift_loss=3.0)],
        "v2": [_loss("dead", "v2", drift_loss=1.0), _loss("live", "v2", drift_loss=0.5)],
    }
    findings = detect_non_differentiating_entry(losses_by_generation)
    assert len(findings) == 1
    assert findings[0].code == "non_differentiating_entry"
    assert findings[0].severity == "warning"
    assert findings[0].detail["entry_id"] == "dead"


def test_non_differentiating_entry_ignores_varying_entry() -> None:
    losses_by_generation = {
        "v0": [_loss("live", "v0", drift_loss=2.0)],
        "v1": [_loss("live", "v1", drift_loss=3.0)],
    }
    assert detect_non_differentiating_entry(losses_by_generation) == []


def test_non_differentiating_entry_ignores_single_generation_entry() -> None:
    # An entry that ran under only one generation has nothing to compare.
    losses_by_generation = {"v0": [_loss("solo", "v0", drift_loss=1.0)]}
    assert detect_non_differentiating_entry(losses_by_generation) == []


# ---------------------------------------------------------------------------
# flat_drift_signal
# ---------------------------------------------------------------------------


def test_flat_drift_signal_fires_when_all_drift_counts_zero() -> None:
    # Runs exist, but no drift count anywhere.
    losses_by_generation = {
        "v0": [_loss("a", "v0"), _loss("b", "v0")],
        "v1": [_loss("a", "v1"), _loss("b", "v1")],
    }
    findings = detect_flat_drift_signal(losses_by_generation)
    assert len(findings) == 1
    assert findings[0].code == "flat_drift_signal"
    assert findings[0].severity == "warning"
    assert findings[0].detail["runs_inspected"] == 4


def test_flat_drift_signal_silent_when_drift_fired() -> None:
    losses_by_generation = {
        "v0": [
            _loss(
                "a",
                "v0",
                drift_counts=(DriftCount(kind="off_topic", severity="warning", count=2),),
            )
        ],
    }
    assert detect_flat_drift_signal(losses_by_generation) == []


def test_flat_drift_signal_silent_when_no_runs() -> None:
    assert detect_flat_drift_signal({}) == []


# ---------------------------------------------------------------------------
# no_expectations
# ---------------------------------------------------------------------------


def test_no_expectations_fires_past_threshold() -> None:
    # 3 of 4 entries have no expectation: 0.75 > 0.5 default threshold.
    board = [
        _board_entry("e1", with_expectation=False),
        _board_entry("e2", with_expectation=False),
        _board_entry("e3", with_expectation=False),
        _board_entry("e4", with_expectation=True),
    ]
    findings = detect_no_expectations(board)
    assert len(findings) == 1
    assert findings[0].code == "no_expectations"
    assert findings[0].severity == "info"
    assert findings[0].detail["entries_without_expectation"] == 3


def test_no_expectations_silent_at_or_below_threshold() -> None:
    # Exactly half have no expectation: 0.5 is not strictly greater.
    board = [
        _board_entry("e1", with_expectation=False),
        _board_entry("e2", with_expectation=True),
    ]
    assert detect_no_expectations(board) == []


def test_no_expectations_silent_on_empty_board() -> None:
    assert detect_no_expectations([]) == []


# ---------------------------------------------------------------------------
# dead_judge
# ---------------------------------------------------------------------------


def _board_entry_with_judges(entry_id: str, judge_names: list[str]) -> BoardEntry:
    """Build a single-turn entry declaring the named in-run judges."""
    return BoardEntry(
        id=entry_id,
        kind="single_turn",
        wall_clock_budget_seconds=60,
        input="hello",
        judges=tuple(
            JudgeSpec(name=name, mode="inline", body="never violated", severity="warning")
            for name in judge_names
        ),
    )


def _custom(judge_name: str) -> DriftCount:
    """A custom-judge-attributed drift count (what the reducer writes)."""
    return DriftCount(kind=f"custom:{judge_name}", severity="warning", count=1)


def test_dead_judge_fires_for_declared_but_never_fired_judge() -> None:
    board = [_board_entry_with_judges("e1", ["lives", "dead"])]
    losses_by_generation = {
        "v0": [_loss("e1", "v0", drift_counts=(_custom("lives"),))],
        "v1": [_loss("e1", "v1", drift_counts=(_custom("lives"),))],
    }
    findings = detect_dead_judge(losses_by_generation, board)
    assert len(findings) == 1
    assert findings[0].code == "dead_judge"
    assert findings[0].severity == "warning"
    assert findings[0].detail["dead_judges"] == ["dead"]
    assert findings[0].detail["fired_judges"] == ["lives"]


def test_dead_judge_silent_when_every_judge_fires() -> None:
    board = [_board_entry_with_judges("e1", ["a", "b"])]
    losses_by_generation = {
        "v0": [_loss("e1", "v0", drift_counts=(_custom("a"), _custom("b")))],
    }
    assert detect_dead_judge(losses_by_generation, board) == []


def test_dead_judge_silent_when_no_judges_declared() -> None:
    board = [_board_entry("e1", with_expectation=True)]
    losses_by_generation = {"v0": [_loss("e1", "v0")]}
    assert detect_dead_judge(losses_by_generation, board) == []


def test_dead_judge_silent_when_no_runs_yet() -> None:
    # A declared judge cannot be "dead" before any run has had a chance to fire.
    board = [_board_entry_with_judges("e1", ["pending"])]
    assert detect_dead_judge({}, board) == []


# ---------------------------------------------------------------------------
# stalled_loop
# ---------------------------------------------------------------------------


def test_stalled_loop_fires_on_three_consecutive_rejects() -> None:
    experiments = [
        _experiment("v1", scalar_delta=-0.2, decision="promoted"),
        _experiment("v2", scalar_delta=0.0, decision="rejected"),
        _experiment("v3", scalar_delta=0.0, decision="rejected"),
        _experiment("v4", scalar_delta=0.0, decision="rejected"),
    ]
    findings = detect_stalled_loop(experiments)
    assert len(findings) == 1
    assert findings[0].code == "stalled_loop"
    assert findings[0].severity == "warning"
    assert findings[0].detail["rejected_generation_ids"] == ["v2", "v3", "v4"]


def test_stalled_loop_silent_when_recent_promote_breaks_the_run() -> None:
    experiments = [
        _experiment("v1", scalar_delta=0.0, decision="rejected"),
        _experiment("v2", scalar_delta=0.0, decision="rejected"),
        _experiment("v3", scalar_delta=-0.2, decision="promoted"),
    ]
    assert detect_stalled_loop(experiments) == []


def test_stalled_loop_silent_below_threshold() -> None:
    experiments = [
        _experiment("v1", scalar_delta=0.0, decision="rejected"),
        _experiment("v2", scalar_delta=0.0, decision="rejected"),
    ]
    assert detect_stalled_loop(experiments) == []


# ---------------------------------------------------------------------------
# generalization_gap (OVERFITTING.md §6 / §12 #5)
# ---------------------------------------------------------------------------


def test_generalization_gap_fires_warning_when_gap_widens_past_warn() -> None:
    # Train falls, holdout stalls → the gap widens from ~0 to 0.08, between
    # the default warn (0.05) and crit (0.15) bars.
    experiments = [
        _gap_experiment("v1", train_loss=0.50, holdout_loss=0.50),
        _gap_experiment("v2", train_loss=0.42, holdout_loss=0.50),
    ]
    findings = detect_generalization_gap(experiments)
    assert len(findings) == 1
    assert findings[0].code == "generalization_gap"
    assert findings[0].severity == "warning"
    assert findings[0].detail["refresh_recommended"] is False


def test_generalization_gap_fires_critical_past_crit_and_recommends_refresh() -> None:
    # Train falls hard, holdout rises → gap widens to 0.25, above crit (0.15).
    experiments = [
        _gap_experiment("v1", train_loss=0.50, holdout_loss=0.50),
        _gap_experiment("v2", train_loss=0.30, holdout_loss=0.55),
    ]
    findings = detect_generalization_gap(experiments)
    assert len(findings) == 1
    assert findings[0].severity == "critical"
    assert findings[0].detail["refresh_recommended"] is True
    assert "refresh" in findings[0].detail["recommendation"].lower()


def test_generalization_gap_clears_below_warn() -> None:
    # The gap widens but only to 0.02 — below the warn bar, no finding.
    experiments = [
        _gap_experiment("v1", train_loss=0.50, holdout_loss=0.50),
        _gap_experiment("v2", train_loss=0.46, holdout_loss=0.48),
    ]
    assert detect_generalization_gap(experiments) == []


def test_generalization_gap_clears_when_gap_narrows() -> None:
    # A large but *narrowing* gap is healthy — the holdout is catching up.
    experiments = [
        _gap_experiment("v1", train_loss=0.30, holdout_loss=0.60),  # gap 0.30
        _gap_experiment("v2", train_loss=0.40, holdout_loss=0.50),  # gap 0.10
    ]
    assert detect_generalization_gap(experiments) == []


def test_generalization_gap_degrades_with_no_holdout() -> None:
    # No holdout on any generation → no measured gap → no finding.
    experiments = [
        _gap_experiment("v1", train_loss=0.50, holdout_loss=None),
        _gap_experiment("v2", train_loss=0.30, holdout_loss=None),
    ]
    assert detect_generalization_gap(experiments) == []


def test_generalization_gap_degrades_below_two_generations() -> None:
    # A single measured generation has nothing to compare against.
    experiments = [_gap_experiment("v1", train_loss=0.30, holdout_loss=0.55)]
    assert detect_generalization_gap(experiments) == []


def test_generalization_gap_workspace_config_overrides_thresholds() -> None:
    # Lower the warn bar via the workspace config.json 'health' block —
    # the operator surface for these thresholds — so a small widening
    # now fires.
    from zicato.config import health_config_from_workspace

    config = health_config_from_workspace(
        {"health": {"generalization_gap_warn": 0.01, "generalization_gap_crit": 0.5}}
    )
    experiments = [
        _gap_experiment("v1", train_loss=0.50, holdout_loss=0.50),
        _gap_experiment("v2", train_loss=0.46, holdout_loss=0.48),  # gap 0.02
    ]
    findings = detect_generalization_gap(experiments, config)
    assert len(findings) == 1
    assert findings[0].severity == "warning"


# ---------------------------------------------------------------------------
# refresh_cadence (OVERFITTING.md §7 / §12 #6)
# ---------------------------------------------------------------------------


def test_refresh_cadence_silent_when_no_ceiling() -> None:
    experiments = [_experiment(f"v{i}", scalar_delta=-0.1, decision="promoted") for i in range(5)]
    assert detect_refresh_cadence(experiments, None) == []


def test_refresh_cadence_silent_below_ceiling() -> None:
    experiments = [_experiment(f"v{i}", scalar_delta=-0.1, decision="promoted") for i in range(3)]
    assert detect_refresh_cadence(experiments, 5) == []


def test_refresh_cadence_recommends_refresh_at_ceiling() -> None:
    experiments = [_experiment(f"v{i}", scalar_delta=-0.1, decision="promoted") for i in range(5)]
    findings = detect_refresh_cadence(experiments, 5)
    assert len(findings) == 1
    assert findings[0].code == "refresh_cadence"
    assert findings[0].severity == "info"
    assert findings[0].detail["refresh_recommended"] is True
    assert findings[0].detail["evaluated_generations"] == 5


def test_refresh_cadence_ignores_unevaluated_experiments() -> None:
    # Experiments without an outcome do not count toward the cadence.
    experiments = [
        _experiment("v1", scalar_delta=None, decision="rejected"),
        _experiment("v2", scalar_delta=None, decision="rejected"),
    ]
    assert detect_refresh_cadence(experiments, 2) == []


def test_assess_loop_health_threads_cadence_and_gap() -> None:
    # The suite wires both new detectors; a critical gap flips healthy False.
    experiments = [
        _gap_experiment("v1", train_loss=0.50, holdout_loss=0.50),
        _gap_experiment("v2", train_loss=0.30, holdout_loss=0.60),
    ]
    report = assess_loop_health(
        losses_by_generation={},
        experiments=experiments,
        board_entries=[_board_entry("a", with_expectation=True)],
        epoch_id="e1",
        max_generations_per_contract=2,
    )
    codes = {f.code for f in report.findings}
    assert "generalization_gap" in codes
    assert "refresh_cadence" in codes
    assert report.healthy is False  # the critical gap


# ---------------------------------------------------------------------------
# LoopHealth.healthy rollup
# ---------------------------------------------------------------------------


def test_loop_health_healthy_true_when_no_warning_or_critical() -> None:
    # Drift-only board with a single generation: only the info-severity
    # no_expectations detector can fire, which leaves the loop healthy.
    board = [_board_entry("a", with_expectation=False)]
    losses_by_generation = {
        "v0": [
            _loss(
                "a",
                "v0",
                drift_counts=(DriftCount(kind="off_topic", severity="info", count=1),),
            )
        ],
    }
    report = assess_loop_health(
        losses_by_generation=losses_by_generation,
        experiments=[],
        board_entries=board,
        epoch_id="e1",
    )
    assert report.healthy is True
    assert all(f.severity == "info" for f in report.findings)


def test_loop_health_unhealthy_when_a_warning_finding_exists() -> None:
    # flat_drift_signal is a warning → healthy must be False.
    board = [_board_entry("a", with_expectation=True)]
    losses_by_generation = {"v0": [_loss("a", "v0")]}
    report = assess_loop_health(
        losses_by_generation=losses_by_generation,
        experiments=[],
        board_entries=board,
        epoch_id="e1",
    )
    assert report.healthy is False
    assert any(f.severity == "warning" for f in report.findings)


def test_loop_health_unhealthy_when_a_critical_finding_exists() -> None:
    board = [_board_entry("a", with_expectation=True)]
    losses_by_generation = {
        "v0": [
            _loss(
                "a",
                "v0",
                drift_counts=(DriftCount(kind="off_topic", severity="info", count=1),),
            )
        ],
    }
    experiments = [
        _experiment("v1", scalar_delta=0.0, decision="rejected"),
        _experiment("v2", scalar_delta=0.0, decision="rejected"),
        _experiment("v3", scalar_delta=0.0, decision="rejected"),
    ]
    report = assess_loop_health(
        losses_by_generation=losses_by_generation,
        experiments=experiments,
        board_entries=board,
        epoch_id="e1",
    )
    assert report.healthy is False
    assert any(f.code == "degenerate_scoring" and f.severity == "critical" for f in report.findings)


def test_health_finding_is_frozen() -> None:
    finding = HealthFinding(code="x", severity="info", summary="s", detail={})
    try:
        finding.severity = "critical"  # type: ignore[misc]
    except (AttributeError, TypeError):
        pass
    else:  # pragma: no cover - defensive
        raise AssertionError("HealthFinding should be frozen")


# ---------------------------------------------------------------------------
# Threshold overrides via the workspace config.json 'health' block
# ---------------------------------------------------------------------------


def test_scoring_window_workspace_override_widens_detector() -> None:
    from zicato.config import health_config_from_workspace

    config = health_config_from_workspace({"health": {"scoring_window": 5}})
    # Only 3 zero-delta tournaments — below the overridden window of 5.
    experiments = [_experiment(f"v{n}", scalar_delta=0.0, decision="rejected") for n in range(1, 4)]
    assert detect_degenerate_scoring(experiments, config) == []


def test_stalled_rejects_workspace_override() -> None:
    from zicato.config import health_config_from_workspace

    config = health_config_from_workspace({"health": {"stalled_rejects": 2}})
    experiments = [
        _experiment("v1", scalar_delta=0.0, decision="rejected"),
        _experiment("v2", scalar_delta=0.0, decision="rejected"),
    ]
    findings = detect_stalled_loop(experiments, config)
    assert len(findings) == 1
    assert findings[0].detail["consecutive_rejects"] == 2


def test_deleted_health_env_vars_are_ignored(monkeypatch) -> None:
    """The former ``ZICATO_HEALTH_*`` env vars are deleted, not aliased."""
    monkeypatch.setenv("ZICATO_HEALTH_SCORING_WINDOW", "5")
    monkeypatch.setenv("ZICATO_HEALTH_STALLED_REJECTS", "2")
    # 3 flat tournaments == the DEFAULT window of 3 — the env var did not
    # widen it, so the detector fires.
    experiments = [_experiment(f"v{n}", scalar_delta=0.0, decision="rejected") for n in range(1, 4)]
    assert detect_degenerate_scoring(experiments) != []


# ---------------------------------------------------------------------------
# tree_never_imported
# ---------------------------------------------------------------------------


def test_tree_never_imported_warns_once_per_generation_and_tree() -> None:
    """One warning per (generation, tree), naming both — issue #110's alarm."""
    findings = detect_tree_never_imported({"v2": ("goldfive", "agent_pkg"), "v1": ("goldfive",)})
    assert [(f.code, f.severity) for f in findings] == [("tree_never_imported", "warning")] * 3
    # Deterministic order: generations sorted, trees in the recorded order.
    assert [(f.detail["generation_id"], f.detail["tree"]) for f in findings] == [
        ("v1", "goldfive"),
        ("v2", "goldfive"),
        ("v2", "agent_pkg"),
    ]
    assert (
        "mutations to tree goldfive cannot have been under test in generation v1"
        in findings[0].summary
    )


def test_tree_never_imported_silent_without_a_gap() -> None:
    """Every healthy epoch — and every non-ADK adapter kind — is silent."""
    assert detect_tree_never_imported(None) == []
    assert detect_tree_never_imported({}) == []
    assert detect_tree_never_imported({"v1": ()}) == []


def test_tree_never_imported_flips_the_report_unhealthy() -> None:
    """A warning-severity finding is enough to make the epoch report unhealthy."""
    health = assess_loop_health({}, [], [], "e1", tree_import_gaps={"v1": ("goldfive",)})
    assert health.healthy is False
    assert [f.code for f in health.findings] == ["tree_never_imported"]


# ---------------------------------------------------------------------------
# CLI smoke test
# ---------------------------------------------------------------------------


def _write_run_loss(workspace: Path, epoch_id: str, generation_id: str, loss: LossProfile) -> None:
    """Write a ``loss.json`` for one run into the workspace layout."""
    run_dir = (
        workspace / "epochs" / epoch_id / "generations" / generation_id / "runs" / loss.entry_id
    )
    run_dir.mkdir(parents=True, exist_ok=True)
    body = {
        "run_id": loss.run_id,
        "entry_id": loss.entry_id,
        "generation_id": loss.generation_id,
        "epoch_id": loss.epoch_id,
        "drift_counts": [
            {"kind": dc.kind, "severity": dc.severity, "count": dc.count}
            for dc in loss.drift_counts
        ],
        "plan_revisions": loss.plan_revisions,
        "task_failure_ratio": loss.task_failure_ratio,
        "runtime_ms": loss.runtime_ms,
        "wall_clock_budget_exceeded": loss.wall_clock_budget_exceeded,
        "expectation_result": None,
        "drift_loss": loss.drift_loss,
        "pass_fail": loss.pass_fail,
    }
    (run_dir / "loss.json").write_text(json.dumps(body), encoding="utf-8")


def _write_experiment(workspace: Path, epoch_id: str, generation_id: str, body: dict) -> None:
    """Write an ``experiment.json`` into the workspace layout."""
    gen_dir = workspace / "epochs" / epoch_id / "generations" / generation_id
    gen_dir.mkdir(parents=True, exist_ok=True)
    (gen_dir / "experiment.json").write_text(json.dumps(body), encoding="utf-8")


def _build_degenerate_workspace(tmp_path: Path) -> Path:
    """Lay out a workspace that reproduces the toothless-eval failure mode."""
    workspace = tmp_path / ".zicato"
    epoch_id = "2026-05-15_loop"
    epoch_root = workspace / "epochs" / epoch_id
    epoch_root.mkdir(parents=True, exist_ok=True)

    # Board: one entry, with an expectation so no_expectations stays quiet.
    board_row = {
        "id": "entry_a",
        "kind": "single_turn",
        "wall_clock_budget_seconds": 60,
        "input": "hello",
        "expectation": {"kind": "expected_text", "spec": "ok"},
    }
    (epoch_root / "board.jsonl").write_text(json.dumps(board_row) + "\n", encoding="utf-8")

    # current_epoch marker.
    (workspace / "current_epoch").write_text(epoch_id, encoding="utf-8")

    # Two generations, both scoring an identical drift_loss, no drift fired.
    for gen in ("v0", "v1"):
        _write_run_loss(workspace, epoch_id, gen, _loss("entry_a", gen, drift_loss=1.0))

    # Three consecutive zero-delta rejected tournaments → critical finding.
    for gen in ("v1", "v2", "v3"):
        _write_experiment(
            workspace,
            epoch_id,
            gen,
            _experiment(gen, scalar_delta=0.0, decision="rejected"),
        )
    return workspace


def test_cli_health_reports_and_exits_nonzero_on_critical(tmp_path: Path) -> None:
    workspace = _build_degenerate_workspace(tmp_path)
    runner = CliRunner()
    result = runner.invoke(health_cmd, ["--workspace", str(workspace)])
    # A critical finding must drive a non-zero exit.
    assert result.exit_code == 1, result.output
    assert "UNHEALTHY" in result.output
    assert "degenerate_scoring" in result.output


def test_cli_health_reads_thresholds_from_workspace_config(tmp_path: Path) -> None:
    """The config.json ``health`` block round-trips into the detectors.

    The same degenerate workspace, plus a ``health`` block that widens
    the scoring window past the 3 flat tournaments on disk — the
    degenerate-scoring detector must now stay silent. This is the
    end-to-end proof that the block (the former ``ZICATO_HEALTH_*``
    surface) actually reaches the assessment.
    """
    workspace = _build_degenerate_workspace(tmp_path)
    (workspace / "config.json").write_text(
        json.dumps({"health": {"scoring_window": 5}}), encoding="utf-8"
    )
    runner = CliRunner()
    result = runner.invoke(health_cmd, ["--workspace", str(workspace)])
    assert "degenerate_scoring" not in result.output


def test_cli_health_rejects_typo_in_workspace_health_block(tmp_path: Path) -> None:
    """A typo'd key in the ``health`` block fails loudly, not silently."""
    workspace = _build_degenerate_workspace(tmp_path)
    (workspace / "config.json").write_text(
        json.dumps({"health": {"scoring_windw": 5}}), encoding="utf-8"
    )
    runner = CliRunner()
    result = runner.invoke(health_cmd, ["--workspace", str(workspace)])
    assert result.exit_code != 0
    assert "scoring_windw" in result.output
    assert "known fields" in result.output


def test_cli_health_healthy_workspace_exits_zero(tmp_path: Path) -> None:
    # A workspace with a single generation, drift firing, an expectation,
    # and no tournaments → every detector stays silent.
    workspace = tmp_path / ".zicato"
    epoch_id = "2026-05-15_ok"
    epoch_root = workspace / "epochs" / epoch_id
    epoch_root.mkdir(parents=True, exist_ok=True)
    board_row = {
        "id": "entry_a",
        "kind": "single_turn",
        "wall_clock_budget_seconds": 60,
        "input": "hello",
        "expectation": {"kind": "expected_text", "spec": "ok"},
    }
    (epoch_root / "board.jsonl").write_text(json.dumps(board_row) + "\n", encoding="utf-8")
    (workspace / "current_epoch").write_text(epoch_id, encoding="utf-8")
    _write_run_loss(
        workspace,
        epoch_id,
        "v0",
        _loss(
            "entry_a",
            "v0",
            drift_loss=0.5,
            drift_counts=(DriftCount(kind="off_topic", severity="info", count=1),),
        ),
    )

    runner = CliRunner()
    result = runner.invoke(health_cmd, ["--workspace", str(workspace)])
    assert result.exit_code == 0, result.output
    assert "HEALTHY" in result.output


def test_cli_health_missing_epoch_marker_errors_cleanly(tmp_path: Path) -> None:
    workspace = tmp_path / ".zicato"
    workspace.mkdir(parents=True, exist_ok=True)
    runner = CliRunner()
    result = runner.invoke(health_cmd, ["--workspace", str(workspace)])
    assert result.exit_code != 0
    assert "No active epoch" in result.output
