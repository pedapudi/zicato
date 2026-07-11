"""query.reflection_view — the Instrument-lens read surface.

Index-first / file-fallback readers, the DQ3 same-shape-empty degrade, the
transcript x-ray (result-tier + verbatim judge_io), and the
reflection-INDEPENDENT entry×candidate matrix (parity vs a hand-computed
fixture). The module must stay dashboard-free (asserted structurally by
lint-imports; here we only exercise behaviour).
"""

from __future__ import annotations

import json
from pathlib import Path

from zicato.core import DriftCount, JudgeLoss, LossProfile, ScoringWeights
from zicato.core.workspace import (
    reflection_adjudication_path,
    reflection_dir,
    reflection_findings_path,
    reflection_plan_path,
    reflection_scorecards_path,
)
from zicato.index.ingest import ingest_reflection, rebuild_index
from zicato.judge_runtime.io_capture import JudgeIOFileSink, judge_io_path_for_loss
from zicato.query import reflection_view as rv
from zicato.query.paths import WorkspacePaths
from zicato.reflection.corpus import ingest_lineage, write_corpus
from zicato.tournament.unit_cache import _unit_loss_path, unit_result_path

EPOCH = "epoch-1"
REFL = "refl-20260701000000-view0001"
SPAN = "VERBATIM-window-slice-xyz"


def _paths(workspace: Path) -> WorkspacePaths:
    return WorkspacePaths(workspace)


def _write_loss(workspace: Path, gen: str, entry: str, *, drift: float, replicate: int = 0) -> Path:
    from zicato.telemetry import reducer

    loss = LossProfile(
        run_id=f"run-{gen}-{entry}",
        entry_id=entry,
        generation_id=gen,
        epoch_id=EPOCH,
        drift_counts=((DriftCount(kind="custom:j", severity="warning", count=1),) if drift else ()),
        plan_revisions=0,
        task_failure_ratio=0.0,
        runtime_ms=10,
        wall_clock_budget_exceeded=False,
        expectation_result=None,
        drift_loss=drift,
        pass_fail=drift == 0.0,
        per_judge_loss=(JudgeLoss("j", raw_loss=drift, weight=1.0, weighted_loss=drift),),
    )
    path = _unit_loss_path(workspace, EPOCH, gen, entry, replicate)
    reducer.write_loss_profile(loss, path)
    return path


def _write_result(loss_path: Path) -> None:
    unit_result_path(loss_path).write_text(
        json.dumps(
            {
                "format_version": 1,
                "run_id": "r",
                "entry_id": "entryA",
                "final_output": "the final answer",
                "transcript": ["user asked", "assistant replied"],
                "runtime_ms": 10,
                "aborted": False,
                "abort_reason": "",
                "clipped": False,
            }
        ),
        encoding="utf-8",
    )


def _write_judge_io(loss_path: Path, *, fired: bool) -> None:
    sink = JudgeIOFileSink(judge_io_path_for_loss(loss_path))
    sink.record(
        "j",
        reasoning_text=SPAN,
        transcript_window=(SPAN,),
        raw_response="{}",
        drift_emitted=fired,
        kind="custom:j",
        severity="warning" if fired else "info",
        detail="claim" if fired else "",
    )


def _write_reflection_meta(
    workspace: Path, *, summary: dict, findings: list, scorecards: list
) -> None:
    p = reflection_plan_path(workspace, EPOCH, REFL)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(
            {
                "format_version": 1,
                "reflection_id": REFL,
                "epoch_id": EPOCH,
                "candidates": ["v0", "v1"],
                "entries": ["entryA"],
                "replicates": 3,
                "adjudicator_model": "meta",
                "checks": ["judge-audit"],
                "mode": "active",
                "pre_registered": False,
                "executed": True,
                "created_at": "2026-07-01T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    reflection_scorecards_path(workspace, EPOCH, REFL).write_text(
        json.dumps({"reflection_id": REFL, "scorecards": scorecards}), encoding="utf-8"
    )
    reflection_findings_path(workspace, EPOCH, REFL).write_text(
        json.dumps({"reflection_id": REFL, "findings": findings}), encoding="utf-8"
    )
    (reflection_dir(workspace, EPOCH, REFL) / "summary.json").write_text(
        json.dumps(summary), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# list_reflections + summary + scorecards
# ---------------------------------------------------------------------------


def _epoch_config(workspace: Path) -> None:
    (workspace / "epochs" / EPOCH).mkdir(parents=True, exist_ok=True)
    (workspace / "epochs" / EPOCH / "config.json").write_text(
        json.dumps({"id": EPOCH, "name": EPOCH, "created_at": "2026-07-01", "closed": False}),
        encoding="utf-8",
    )
    (workspace / "lineage.json").write_text(
        json.dumps({"epochs": [{"id": EPOCH, "generations": []}]}), encoding="utf-8"
    )


def test_list_reflections_index_first(tmp_path: Path) -> None:
    workspace = tmp_path / ".zicato"
    _epoch_config(workspace)
    _write_reflection_meta(
        workspace,
        summary={"noise_floor_max_abs_delta": 0.04, "decision_flip_p": 0.1},
        findings=[{"finding_id": "f1"}],
        scorecards=[{"judge_name": "j", "tp": 1, "fp": 0, "fn": 0, "tn": 1, "ambiguous": 0}],
    )
    ingest_reflection(workspace, None, EPOCH, REFL)
    out = rv.list_reflections(_paths(workspace))
    assert [r["reflection_id"] for r in out["reflections"]] == [REFL]
    assert out["reflections"][0]["n_findings"] == 1
    assert out["reflections"][0]["decision_flip_p"] == 0.1


def test_list_reflections_file_fallback_without_index(tmp_path: Path) -> None:
    workspace = tmp_path / ".zicato"
    _epoch_config(workspace)
    _write_reflection_meta(workspace, summary={}, findings=[], scorecards=[])
    # No index built at all — the file fallback still finds the reflection.
    out = rv.list_reflections(_paths(workspace))
    assert [r["reflection_id"] for r in out["reflections"]] == [REFL]
    assert out["reflections"][0]["mode"] == "active"


def test_build_reflection_summary_found_and_missing(tmp_path: Path) -> None:
    workspace = tmp_path / ".zicato"
    _epoch_config(workspace)
    _write_reflection_meta(
        workspace,
        summary={
            "noise_floor_max_abs_delta": 0.02,
            "decision_flip_p": 0.0,
            "pillars": {"reliability": {"consumed": True}},
            "fidelity_tiers": ["verbatim"],
        },
        findings=[{"finding_id": "f1", "severity": "critical"}],
        scorecards=[],
    )
    summary = rv.build_reflection_summary(_paths(workspace), REFL)
    assert summary["found"] is True
    assert summary["noise_floor_max_abs_delta"] == 0.02
    assert summary["pillars"]["reliability"]["consumed"] is True
    assert summary["findings"][0]["finding_id"] == "f1"

    missing = rv.build_reflection_summary(_paths(workspace), "refl-does-not-exist")
    assert missing["found"] is False
    assert missing["pillars"] == {}
    assert missing["findings"] == []


def test_build_judge_scorecards_index_and_fallback(tmp_path: Path) -> None:
    workspace = tmp_path / ".zicato"
    _epoch_config(workspace)
    card = {
        "judge_name": "j",
        "tp": 2,
        "fp": 1,
        "fn": 0,
        "tn": 3,
        "ambiguous": 0,
        "precision": 0.667,
        "recall": 1.0,
        "f1": 0.8,
        "severity_accuracy": 1.0,
        "disagreement_rate": 0.0,
        "self_consistency_kappa": 0.9,
        "redundant_with": [],
        "exercised": True,
    }
    _write_reflection_meta(workspace, summary={}, findings=[], scorecards=[card])
    # File fallback (no index).
    out = rv.build_judge_scorecards(_paths(workspace), REFL)
    assert out["judges"][0]["judge_name"] == "j"

    # Index-first (after ingest) yields the same judge, from the projection.
    ingest_reflection(workspace, None, EPOCH, REFL)
    out2 = rv.build_judge_scorecards(_paths(workspace), REFL)
    assert out2["judges"][0]["judge_name"] == "j"
    assert out2["judges"][0]["self_consistency_kappa"] == 0.9


def test_scorecards_degrade_unknown_reflection(tmp_path: Path) -> None:
    workspace = tmp_path / ".zicato"
    _epoch_config(workspace)
    out = rv.build_judge_scorecards(_paths(workspace), "refl-nope")
    assert out == {"reflection_id": "refl-nope", "judges": []}


# ---------------------------------------------------------------------------
# transcript x-ray
# ---------------------------------------------------------------------------


def test_adjudication_xray_result_tier(tmp_path: Path) -> None:
    workspace = tmp_path / ".zicato"
    _epoch_config(workspace)
    loss_path = _write_loss(workspace, "v1", "entryA", drift=1.0)
    _write_result(loss_path)
    _write_judge_io(loss_path, fired=True)
    corpus = ingest_lineage(
        workspace_root=workspace,
        epoch_id=EPOCH,
        reflection_id=REFL,
        candidates=["v1"],
        entries=["entryA"],
        weights=ScoringWeights(),
    )
    write_corpus(workspace, EPOCH, REFL, corpus)
    _write_reflection_meta(workspace, summary={}, findings=[], scorecards=[])

    run_ref = "v1:entryA:r0"
    reflection_adjudication_path(workspace, EPOCH, REFL, "j", run_ref).parent.mkdir(
        parents=True, exist_ok=True
    )
    reflection_adjudication_path(workspace, EPOCH, REFL, "j", run_ref).write_text(
        json.dumps(
            {
                "format_version": 1,
                "judge_name": "j",
                "run_ref": run_ref,
                "verdict": "TP",
                "evidence_span": SPAN,
                "meta_judge_rationale": "the transcript exhibits it",
            }
        ),
        encoding="utf-8",
    )

    xray = rv.build_adjudication_xray(_paths(workspace), REFL, "j", run_ref)
    assert xray["found"] is True
    # Verbatim judge_io beats result-tier; either way turns are present.
    assert xray["transcript"]["fidelity"] in ("verbatim", "result")
    assert xray["transcript"]["turns"]
    assert xray["judge_verdict"]["judge_name"] == "j"
    assert xray["judge_verdict"]["fired"] is True
    assert xray["adjudication"]["verdict"] == "TP"


def test_adjudication_xray_degrades_on_unknown(tmp_path: Path) -> None:
    workspace = tmp_path / ".zicato"
    _epoch_config(workspace)
    _write_reflection_meta(workspace, summary={}, findings=[], scorecards=[])
    write_corpus(workspace, EPOCH, REFL, [])
    xray = rv.build_adjudication_xray(_paths(workspace), REFL, "j", "v9:entryZ:r0")
    assert xray["found"] is False
    assert xray["transcript"] == {"fidelity": "unavailable", "turns": []}
    assert xray["judge_verdict"] is None
    assert xray["adjudication"] is None


# ---------------------------------------------------------------------------
# entry_candidate_matrix — reflection-independent, parity with hand-computed
# ---------------------------------------------------------------------------


def test_entry_candidate_matrix_parity(tmp_path: Path) -> None:
    workspace = tmp_path / ".zicato"
    _epoch_config(workspace)
    # Two candidates × two entries. The reflection-INDEPENDENT matrix reads the
    # index loss_profiles (one canonical run per (gen, entry)) — replicate slots
    # are NOT in the index, so each cell is that run's drift_loss.
    _write_loss(workspace, "v0", "entryA", drift=2.0, replicate=0)
    _write_loss(workspace, "v0", "entryB", drift=1.0, replicate=0)
    _write_loss(workspace, "v1", "entryA", drift=6.0, replicate=0)
    # v1/entryB intentionally absent -> a None cell.
    # Register the generations in lineage so the index walk sees them.
    (workspace / "lineage.json").write_text(
        json.dumps(
            {
                "epochs": [
                    {
                        "id": EPOCH,
                        "generations": [
                            {"id": "v0", "parent_id": None, "promoted": True, "created_at": "1"},
                            {"id": "v1", "parent_id": "v0", "promoted": False, "created_at": "2"},
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    rebuild_index(workspace)

    out = rv.entry_candidate_matrix(_paths(workspace), EPOCH)
    assert out["entries"] == ["entryA", "entryB"]
    assert out["candidates"] == ["v0", "v1"]
    # entryA: v0 = 2.0, v1 = 6.0 ; entryB: v0 = 1.0, v1 = None (absent cell).
    assert out["matrix"] == [[2.0, 6.0], [1.0, None]]


def test_entry_candidate_matrix_empty_without_index(tmp_path: Path) -> None:
    workspace = tmp_path / ".zicato"
    out = rv.entry_candidate_matrix(_paths(workspace), EPOCH)
    assert out == {"epoch_id": EPOCH, "entries": [], "candidates": [], "matrix": []}
