"""Crash-recovery tests for the field-settlement commit boundary.

Each case stops the same two-challenger tournament after one durable write.
Startup must complete the recorded decision without running the tournament
again, duplicating journal entries, or leaving canonical records in conflict.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from tests._orchestrator_harness import (
    install_stub_adapter_factory,
    install_telemetry_stubs,
    make_aux_responder,
    run_evolve_once,
)
from tests.test_orchestrator_multi_challenger import (
    _bootstrap_swiss_workspace,
)
from zicato.core.workspace import experiment_json_path, field_tournament_path, lineage_path
from zicato.epoch.journal import outcome_from_dict, read_experiment
from zicato.epoch.lineage import load_lineage
from zicato.evolve import settlement as settlement_module
from zicato.evolve.ingest import index_preflight
from zicato.evolve.settlement_recovery import (
    acknowledge_repaired_settlement_indexes,
    commit_field_settlement,
    field_settlement_intent_path,
    replay_field_settlement,
)
from zicato.health.diagnostics import detect_settlement_receipt_attention
from zicato.health.inputs import epoch_settlement_receipt_attention
from zicato.index.ingest import ensure_index, rebuild_index, validate_index
from zicato.query.gate_view import build_health_report
from zicato.query.paths import WorkspacePaths
from zicato.runtime.paths import active_tournament_path
from zicato.runtime.resume import prepare_resume


class _InjectedCrash(RuntimeError):
    """A process stop immediately after one named persistence boundary."""


_COMMIT_BOUNDARIES = (
    "receipt_persisted",
    "outcome:v1",
    "outcome:v2",
    "lineage",
    "champion_marker",
    "journal:v1",
    "journal:v2",
    "settled_bracket",
    "index_projection",
    "receipt_committed",
)


def _stop_after_receipt_persistence(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the live settlement call stop after its receipt write."""

    def stop_after_receipt(root: Path, intent: dict[str, Any]) -> None:
        def checkpoint(boundary: str) -> None:
            if boundary == "receipt_persisted":
                raise _InjectedCrash(boundary)

        commit_field_settlement(root, intent, crash_checkpoint=checkpoint)

    monkeypatch.setattr(settlement_module, "commit_field_settlement", stop_after_receipt)


def _workspace_with_pending_receipt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[Path, str, dict[str, Any]]:
    """Stop a resolved field immediately after its complete receipt lands."""
    from tests.test_on_promote_hook import _install_hooked_adapter_factory

    workspace, epoch_id = _bootstrap_swiss_workspace(tmp_path, field_size=2)
    _install_hooked_adapter_factory(monkeypatch)
    install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 0.5, "v2": 1.5},
        canned_pass_by_gen={"v0": True, "v1": True, "v2": True},
    )

    _stop_after_receipt_persistence(monkeypatch)
    with pytest.raises(_InjectedCrash, match="receipt_persisted"):
        run_evolve_once(
            workspace,
            epoch_id,
            make_aux_responder([]),
        )
    receipt_path = field_settlement_intent_path(workspace, epoch_id, 0)
    receipt: dict[str, Any] = json.loads(receipt_path.read_text(encoding="utf-8"))
    return workspace, epoch_id, receipt


def _assert_field_is_unmutated(workspace: Path, epoch_id: str) -> None:
    """Assert that validation failed before any settlement write."""
    assert read_experiment(workspace, epoch_id, "v1").outcome is None
    assert read_experiment(workspace, epoch_id, "v2").outcome is None
    epoch = next(row for row in load_lineage(workspace)["epochs"] if row["id"] == epoch_id)
    by_generation = {row["id"]: row for row in epoch["generations"]}
    assert by_generation["v1"]["promoted"] is None
    assert by_generation["v2"]["promoted"] is None


def test_commit_reuses_the_recorded_identity_and_rejects_a_conflicting_receipt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A same-round retry cannot replace the first durable decision."""
    workspace, epoch_id, pending = _workspace_with_pending_receipt(monkeypatch, tmp_path)
    receipt_path = field_settlement_intent_path(workspace, epoch_id, 0)

    commit_field_settlement(workspace, pending)
    committed = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert committed["state"] == "committed"
    assert committed["settlement_id"] == pending["settlement_id"]

    # A stale caller holding the original pending value verifies the committed
    # receipt and returns without reverting commit or delivery progress.
    commit_field_settlement(workspace, pending)
    assert json.loads(receipt_path.read_text(encoding="utf-8")) == committed

    conflicting = json.loads(json.dumps(pending))
    conflicting["settlement_id"] = "f" * 32
    with pytest.raises(RuntimeError, match="conflicts with the recorded decision"):
        commit_field_settlement(workspace, conflicting)
    assert json.loads(receipt_path.read_text(encoding="utf-8")) == committed


@pytest.mark.parametrize("crash_boundary", _COMMIT_BOUNDARIES)
def test_resume_completes_each_interrupted_field_settlement_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    crash_boundary: str,
) -> None:
    """Every interrupted prefix converges to one consistent settlement."""
    from tests.test_on_promote_hook import _install_hooked_adapter_factory

    workspace, epoch_id = _bootstrap_swiss_workspace(tmp_path, field_size=2)
    _install_hooked_adapter_factory(monkeypatch)
    install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 0.5, "v2": 1.5},
        canned_pass_by_gen={"v0": True, "v1": True, "v2": True},
    )

    captured: dict[str, Any] = {}

    def stop_commit(root: Path, intent: dict[str, Any]) -> None:
        captured["intent"] = json.loads(json.dumps(intent))

        def checkpoint(boundary: str) -> None:
            if boundary == crash_boundary:
                raise _InjectedCrash(boundary)

        commit_field_settlement(root, intent, crash_checkpoint=checkpoint)

    monkeypatch.setattr(settlement_module, "commit_field_settlement", stop_commit)
    with pytest.raises(_InjectedCrash, match=crash_boundary):
        run_evolve_once(
            workspace,
            epoch_id,
            make_aux_responder([]),
        )

    # Startup imports the recovery owner directly, so restore only the live
    # settlement call before asking it to reconcile the workspace.
    monkeypatch.setattr(settlement_module, "commit_field_settlement", commit_field_settlement)
    plan = prepare_resume(workspace, epoch_id)
    assert plan.classification == "clean"
    assert plan.resume_generation_id is None

    intent = captured["intent"]
    receipt = json.loads(
        field_settlement_intent_path(workspace, epoch_id, 0).read_text(encoding="utf-8")
    )
    assert receipt["state"] == "committed"
    assert receipt["settlement_id"] == intent["settlement_id"]
    assert receipt["candidates"] == intent["candidates"]
    assert receipt["field_tournament_record"] == intent["field_tournament_record"]
    assert receipt["index_projection"] == {"state": "succeeded", "error_type": ""}
    primary = intent["field_tournament_record"]["promoted_generation_id"]
    # Recovery never invokes an external hook. It cannot know whether the dead
    # caller passed the commit boundary, so pending becomes delivery_unknown.
    assert receipt["promotion_hook"]["state"] == "delivery_unknown"
    attention = epoch_settlement_receipt_attention(workspace, epoch_id)
    assert len(attention.unknown_hook_deliveries) == 1
    (finding,) = detect_settlement_receipt_attention(attention)
    assert finding.code == "on_promote_hook_delivery_unknown"
    assert finding.detail["generation_id"] == primary

    candidates = intent["candidates"]
    for candidate in candidates:
        generation_id = candidate["generation_id"]
        experiment = read_experiment(workspace, epoch_id, generation_id)
        assert experiment.outcome == outcome_from_dict(candidate["outcome"])

    lineage = load_lineage(workspace)
    epoch = next(row for row in lineage["epochs"] if row["id"] == epoch_id)
    by_generation = {row["id"]: row for row in epoch["generations"]}
    for candidate in candidates:
        node = by_generation[candidate["generation_id"]]
        assert node["parent_id"] == "v0"
        assert node["promoted"] is (candidate["outcome"]["tournament_decision"] == "promoted")

    marker = workspace / "epochs" / epoch_id / "current_generation"
    assert marker.read_text(encoding="utf-8").strip() == primary

    journal = (workspace / "epochs" / epoch_id / "journal.md").read_text(encoding="utf-8")
    for candidate in candidates:
        identity = f'{intent["settlement_id"]}:{candidate["generation_id"]}'
        marker_text = f'<!-- zicato:field-settlement identity="{identity}" -->'
        assert journal.count(marker_text) == 1

    field_record = json.loads(
        field_tournament_path(
            workspace,
            epoch_id,
            intent["candidates"][0]["generation_id"],
        ).read_text(encoding="utf-8")
    )
    assert field_record == intent["field_tournament_record"]

    # The SQLite file has no independent authority. Dropping and rebuilding
    # it from the recovered canonical files preserves the tournament verdict.
    db_path = workspace / "index.db"
    db_path.unlink(missing_ok=True)
    rebuild_index(workspace, db_path)
    with sqlite3.connect(db_path) as connection:
        indexed = connection.execute(
            "SELECT decision, structure FROM tournaments WHERE tournament_id = ?",
            (field_record["tournament_id"],),
        ).fetchone()
    assert indexed == (field_record["decision"], field_record["structure"])


def test_resume_replays_all_candidates_when_outcomes_already_exist(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Outcome completion cannot make a partially settled field look clean."""
    workspace, epoch_id = _bootstrap_swiss_workspace(tmp_path, field_size=2)
    install_stub_adapter_factory(monkeypatch)
    install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 0.5, "v2": 1.5},
        canned_pass_by_gen={"v0": True, "v1": True, "v2": True},
    )

    def stop_after_outcomes(root: Path, intent: dict[str, Any]) -> None:
        def checkpoint(boundary: str) -> None:
            if boundary == "lineage":
                raise _InjectedCrash(boundary)

        commit_field_settlement(root, intent, crash_checkpoint=checkpoint)

    monkeypatch.setattr(settlement_module, "commit_field_settlement", stop_after_outcomes)
    with pytest.raises(_InjectedCrash):
        run_evolve_once(
            workspace,
            epoch_id,
            make_aux_responder([]),
        )

    assert read_experiment(workspace, epoch_id, "v1").outcome is not None
    assert read_experiment(workspace, epoch_id, "v2").outcome is not None
    plan = prepare_resume(workspace, epoch_id)
    assert plan.classification == "clean"


def test_lineage_resolution_is_atomic_across_the_candidate_field(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A crash checkpoint cannot expose a partly resolved candidate field."""
    workspace, epoch_id, receipt = _workspace_with_pending_receipt(monkeypatch, tmp_path)

    def stop_after_atomic_resolution(boundary: str) -> None:
        if boundary == "lineage":
            raise _InjectedCrash(boundary)

    with pytest.raises(_InjectedCrash, match="lineage"):
        replay_field_settlement(
            workspace,
            receipt,
            expected_epoch_id=epoch_id,
            expected_round_index=0,
            crash_checkpoint=stop_after_atomic_resolution,
        )

    epoch = next(row for row in load_lineage(workspace)["epochs"] if row["id"] == epoch_id)
    by_id = {row["id"]: row for row in epoch["generations"]}
    assert {by_id["v1"]["promoted"], by_id["v2"]["promoted"]} == {False, True}
    assert prepare_resume(workspace, epoch_id).classification == "clean"
    epoch = next(row for row in load_lineage(workspace)["epochs"] if row["id"] == epoch_id)
    assert {row["id"] for row in epoch["generations"]} >= {"v1", "v2"}


def test_recovery_preserves_a_rejected_field_receipt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A field-wide rejection settles every challenger and keeps v0 current."""
    workspace, epoch_id = _bootstrap_swiss_workspace(tmp_path, field_size=2)
    install_stub_adapter_factory(monkeypatch)
    install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 0.1, "v1": 1.0, "v2": 2.0},
        canned_pass_by_gen={"v0": True, "v1": True, "v2": True},
    )
    _stop_after_receipt_persistence(monkeypatch)
    with pytest.raises(_InjectedCrash, match="receipt_persisted"):
        run_evolve_once(
            workspace,
            epoch_id,
            make_aux_responder([]),
        )

    pending = json.loads(
        field_settlement_intent_path(workspace, epoch_id, 0).read_text(encoding="utf-8")
    )
    assert {candidate["outcome"]["tournament_decision"] for candidate in pending["candidates"]} == {
        "rejected"
    }
    assert pending["field_tournament_record"]["promoted_generation_id"] == ""
    assert not any(
        candidate["outcome"]["tournament_decision"] == "promoted"
        for candidate in pending["candidates"]
    )
    prepare_resume(workspace, epoch_id)
    retained = json.loads(
        field_settlement_intent_path(workspace, epoch_id, 0).read_text(encoding="utf-8")
    )
    assert retained["state"] == "committed"
    assert retained["promotion_hook"]["state"] == "not_applicable"
    assert all(
        candidate["outcome"]["tournament_decision"] == "rejected"
        for candidate in retained["candidates"]
    )
    assert (workspace / "epochs" / epoch_id / "current_generation").read_text().strip() == "v0"


def test_recovery_preserves_a_single_challenger_receipt_without_a_field_bracket(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The shared settlement boundary does not invent a gauntlet field record."""
    workspace, epoch_id = _bootstrap_swiss_workspace(tmp_path, field_size=1)
    install_stub_adapter_factory(monkeypatch)
    install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 0.5},
        canned_pass_by_gen={"v0": True, "v1": True},
    )
    _stop_after_receipt_persistence(monkeypatch)
    with pytest.raises(_InjectedCrash, match="receipt_persisted"):
        run_evolve_once(
            workspace,
            epoch_id,
            make_aux_responder([]),
        )

    pending = json.loads(
        field_settlement_intent_path(workspace, epoch_id, 0).read_text(encoding="utf-8")
    )
    assert pending["field_tournament_record"] is None
    assert len(pending["candidates"]) == 1
    prepare_resume(workspace, epoch_id)
    retained = json.loads(
        field_settlement_intent_path(workspace, epoch_id, 0).read_text(encoding="utf-8")
    )
    assert retained["state"] == "committed"
    assert retained["field_tournament_record"] is None
    assert not field_tournament_path(workspace, epoch_id, "v1").exists()


def test_recovery_preserves_an_operator_multi_promotion_receipt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The receipt and lineage retain every operator-promoted candidate."""
    from tests.test_orchestrator_multi_challenger import _queue_override

    workspace, epoch_id = _bootstrap_swiss_workspace(tmp_path, field_size=3)
    install_stub_adapter_factory(monkeypatch)
    install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 3.0, "v1": 0.5, "v2": 2.5, "v3": 1.5},
        canned_pass_by_gen={"v0": True, "v1": True, "v2": True, "v3": True},
    )
    _queue_override(workspace, "promote", "v1", reason="train leader")
    _queue_override(workspace, "promote", "v3", reason="diverse co-leader")
    _stop_after_receipt_persistence(monkeypatch)
    with pytest.raises(_InjectedCrash, match="receipt_persisted"):
        run_evolve_once(
            workspace,
            epoch_id,
            make_aux_responder([]),
        )

    prepare_resume(workspace, epoch_id)
    retained = json.loads(
        field_settlement_intent_path(workspace, epoch_id, 0).read_text(encoding="utf-8")
    )
    assert {
        candidate["generation_id"]
        for candidate in retained["candidates"]
        if candidate["outcome"]["tournament_decision"] == "promoted"
    } == {"v1", "v3"}
    assert retained["primary_promoted_generation_id"] == "v1"
    assert retained["field_tournament_record"]["promoted_generation_id"] == "v1"
    assert sorted(retained["field_tournament_record"]["promoted_generation_ids"]) == [
        "v1",
        "v3",
    ]
    epoch = next(row for row in load_lineage(workspace)["epochs"] if row["id"] == epoch_id)
    promoted = {row["id"] for row in epoch["generations"] if row.get("promoted") is True}
    assert promoted >= {"v1", "v3"}


def test_recovery_preserves_a_deferred_field_receipt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An uncertainty hold remains deferred and never advances the champion."""
    workspace, epoch_id = _bootstrap_swiss_workspace(tmp_path, field_size=2, rounds_n=2)
    scoring_path = workspace / "epochs" / epoch_id / "scoring.json"
    scoring = json.loads(scoring_path.read_text(encoding="utf-8"))
    scoring["tournament"]["params"]["uncertainty_gate"] = 0.999
    scoring_path.write_text(json.dumps(scoring), encoding="utf-8")
    install_stub_adapter_factory(monkeypatch)
    install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 0.51, "v1": 0.50, "v2": 1.5},
        canned_pass_by_gen={"v0": True, "v1": True, "v2": True},
    )
    _stop_after_receipt_persistence(monkeypatch)
    with pytest.raises(_InjectedCrash, match="receipt_persisted"):
        run_evolve_once(
            workspace,
            epoch_id,
            make_aux_responder([]),
        )

    pending = json.loads(
        field_settlement_intent_path(workspace, epoch_id, 0).read_text(encoding="utf-8")
    )
    assert "deferred" in {
        candidate["outcome"]["tournament_decision"] for candidate in pending["candidates"]
    }
    prepare_resume(workspace, epoch_id)
    retained = json.loads(
        field_settlement_intent_path(workspace, epoch_id, 0).read_text(encoding="utf-8")
    )
    decisions = {
        candidate["generation_id"]: candidate["outcome"]["tournament_decision"]
        for candidate in retained["candidates"]
    }
    assert "deferred" in decisions.values()
    assert not any(
        candidate["outcome"]["tournament_decision"] == "promoted"
        for candidate in retained["candidates"]
    )
    assert (workspace / "epochs" / epoch_id / "current_generation").read_text().strip() == "v0"


def test_index_refresh_failure_retains_a_repairable_canonical_settlement(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A partial derived refresh is one reported repair requirement."""
    workspace, epoch_id, receipt = _workspace_with_pending_receipt(monkeypatch, tmp_path)

    def fail_index_refresh(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("injected index failure")

    monkeypatch.setattr("zicato.index.ingest.ingest_field_settlement", fail_index_refresh)
    plan = prepare_resume(workspace, epoch_id)
    assert plan.classification == "clean"

    retained = json.loads(
        field_settlement_intent_path(workspace, epoch_id, 0).read_text(encoding="utf-8")
    )
    assert retained["state"] == "committed"
    assert retained["index_projection"] == {
        "state": "repair_required",
        "error_type": "OSError",
    }
    for candidate in receipt["candidates"]:
        generation_id = candidate["generation_id"]
        assert read_experiment(workspace, epoch_id, generation_id).outcome == outcome_from_dict(
            candidate["outcome"]
        )
    settled = json.loads(
        field_tournament_path(workspace, epoch_id, "v1").read_text(encoding="utf-8")
    )
    assert settled == receipt["field_tournament_record"]

    attention = epoch_settlement_receipt_attention(workspace, epoch_id)
    assert len(attention.index_repairs) == 1
    assert "settlement_index_repair_required" in {
        finding.code for finding in detect_settlement_receipt_attention(attention)
    }
    assert build_health_report(WorkspacePaths(workspace))["healthy"] is False

    # Counts can already match even though the indexed outcomes and promotion
    # states predate settlement. The retained receipt therefore forces the
    # evolve preflight to rebuild rather than trusting a no-op cheap heal.
    assert validate_index(workspace) == ()
    rebuild_index(workspace)
    still_pending = json.loads(
        field_settlement_intent_path(workspace, epoch_id, 0).read_text(encoding="utf-8")
    )
    assert still_pending["index_projection"]["state"] == "repair_required"
    assert index_preflight(workspace) == "index: built fresh (settlement-repair-required)"
    repaired = json.loads(
        field_settlement_intent_path(workspace, epoch_id, 0).read_text(encoding="utf-8")
    )
    assert repaired["index_projection"] == {
        "state": "repaired",
        "error_type": "OSError",
    }
    assert epoch_settlement_receipt_attention(workspace, epoch_id).index_repairs == ()
    codes = {
        finding["code"] for finding in build_health_report(WorkspacePaths(workspace))["findings"]
    }
    assert "settlement_index_repair_required" not in codes


def test_resume_discards_an_entire_field_when_no_receipt_was_persisted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A pre-receipt crash cannot leave siblings pending in canonical views."""
    workspace, epoch_id = _bootstrap_swiss_workspace(tmp_path, field_size=2)
    install_stub_adapter_factory(monkeypatch)
    install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 0.5, "v2": 1.5},
        canned_pass_by_gen={"v0": True, "v1": True, "v2": True},
    )

    def stop_before_receipt(_root: Path, _intent: dict[str, Any]) -> None:
        raise _InjectedCrash("before receipt")

    monkeypatch.setattr(settlement_module, "commit_field_settlement", stop_before_receipt)
    with pytest.raises(_InjectedCrash, match="before receipt"):
        run_evolve_once(
            workspace,
            epoch_id,
            make_aux_responder([]),
        )

    receipt_path = field_settlement_intent_path(workspace, epoch_id, 0)
    assert not receipt_path.exists()
    lineage_before = next(row for row in load_lineage(workspace)["epochs"] if row["id"] == epoch_id)
    pending = [row["id"] for row in lineage_before["generations"] if row.get("promoted") is None]
    assert pending == ["v1", "v2"]
    in_progress_path = field_tournament_path(workspace, epoch_id, "v1")
    assert json.loads(in_progress_path.read_text(encoding="utf-8"))["state"] == "in_progress"

    plan = prepare_resume(workspace, epoch_id)
    assert plan.classification == "discard_unrecorded_field"
    assert not receipt_path.exists()
    assert not in_progress_path.exists()
    assert not active_tournament_path(workspace).exists()
    assert not (workspace / "epochs" / epoch_id / "rounds" / "0").exists()
    for generation_id in ("v1", "v2"):
        assert not (workspace / "epochs" / epoch_id / "generations" / generation_id).exists()

    lineage_after = next(row for row in load_lineage(workspace)["epochs"] if row["id"] == epoch_id)
    assert {row["id"] for row in lineage_after["generations"]}.isdisjoint({"v1", "v2"})
    # Cleanup invalidates the derived projection. The evolve preflight rebuilds
    # it after canonical recovery, so prepare_resume itself leaves no stale DB.
    assert not (workspace / "index.db").exists()


def test_unrecorded_field_cleanup_includes_a_sibling_missing_its_experiment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A pending lineage marker makes a missing experiment discard the whole field."""
    workspace, epoch_id = _bootstrap_swiss_workspace(tmp_path, field_size=2)
    install_stub_adapter_factory(monkeypatch)
    install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 0.5, "v2": 1.5},
        canned_pass_by_gen={"v0": True, "v1": True, "v2": True},
    )

    def stop_before_receipt(_root: Path, _intent: dict[str, Any]) -> None:
        raise _InjectedCrash("before receipt")

    monkeypatch.setattr(settlement_module, "commit_field_settlement", stop_before_receipt)
    with pytest.raises(_InjectedCrash, match="before receipt"):
        run_evolve_once(
            workspace,
            epoch_id,
            make_aux_responder([]),
        )

    # Candidate creation writes pending lineage before experiment.json. Model
    # a process death after that marker for the second sibling.
    experiment_path = workspace / "epochs" / epoch_id / "generations" / "v2" / "experiment.json"
    experiment_path.unlink()

    plan = prepare_resume(workspace, epoch_id)

    assert plan.classification == "discard_unrecorded_field"
    lineage = next(row for row in load_lineage(workspace)["epochs"] if row["id"] == epoch_id)
    assert {row["id"] for row in lineage["generations"]}.isdisjoint({"v1", "v2"})
    from zicato.evolve.generation_phase import next_generation_id

    assert next_generation_id(workspace, epoch_id) == "v1"


@pytest.mark.parametrize(
    "cleanup_boundary",
    ("index_invalidated", "canonical_records_removed", "lineage_committed"),
)
def test_unrecorded_field_cleanup_recovers_from_each_durability_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    cleanup_boundary: str,
) -> None:
    """Index invalidation and lineage-last cleanup are retryable after a crash."""
    import zicato.runtime.resume as resume_module

    workspace, epoch_id = _bootstrap_swiss_workspace(tmp_path, field_size=2)
    install_stub_adapter_factory(monkeypatch)
    install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 0.5, "v2": 1.5},
        canned_pass_by_gen={"v0": True, "v1": True, "v2": True},
    )

    def stop_before_receipt(_root: Path, _intent: dict[str, Any]) -> None:
        raise _InjectedCrash("before receipt")

    monkeypatch.setattr(settlement_module, "commit_field_settlement", stop_before_receipt)
    with pytest.raises(_InjectedCrash, match="before receipt"):
        run_evolve_once(
            workspace,
            epoch_id,
            make_aux_responder([]),
        )

    def stop_cleanup(boundary: str) -> None:
        if boundary == cleanup_boundary:
            raise _InjectedCrash(boundary)

    monkeypatch.setattr(resume_module, "_field_cleanup_checkpoint", stop_cleanup)
    with pytest.raises(_InjectedCrash, match=cleanup_boundary):
        prepare_resume(workspace, epoch_id)
    assert not (workspace / "index.db").exists()

    monkeypatch.setattr(resume_module, "_field_cleanup_checkpoint", lambda _boundary: None)
    plan = prepare_resume(workspace, epoch_id)
    assert plan.classification in {"clean", "discard_unrecorded_field"}
    epoch = next(row for row in load_lineage(workspace)["epochs"] if row["id"] == epoch_id)
    assert {row["id"] for row in epoch["generations"]}.isdisjoint({"v1", "v2"})
    assert not (workspace / "epochs" / epoch_id / "rounds" / "0").exists()
    ensure_index(workspace)
    assert (workspace / "index.db").exists()


@pytest.mark.parametrize("structure", ("swiss", "single_elim"))
def test_unrecorded_field_cleanup_uses_the_strategy_default_width(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    structure: str,
) -> None:
    """An omitted field_size keeps each non-gauntlet strategy's default of two."""
    workspace, epoch_id = _bootstrap_swiss_workspace(
        tmp_path,
        field_size=2,
        structure=structure,
    )
    scoring_path = workspace / "epochs" / epoch_id / "scoring.json"
    scoring = json.loads(scoring_path.read_text(encoding="utf-8"))
    scoring["tournament"]["params"].pop("field_size")
    scoring_path.write_text(json.dumps(scoring), encoding="utf-8")
    install_stub_adapter_factory(monkeypatch)
    install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 0.5, "v2": 1.5},
        canned_pass_by_gen={"v0": True, "v1": True, "v2": True},
    )

    def stop_before_receipt(_root: Path, _intent: dict[str, Any]) -> None:
        raise _InjectedCrash("before receipt")

    monkeypatch.setattr(settlement_module, "commit_field_settlement", stop_before_receipt)
    with pytest.raises(_InjectedCrash, match="before receipt"):
        run_evolve_once(
            workspace,
            epoch_id,
            make_aux_responder([]),
        )

    plan = prepare_resume(workspace, epoch_id)
    assert plan.classification == "discard_unrecorded_field"


def test_wide_field_with_one_applied_candidate_is_discarded_after_proposal_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Configured width identifies a field even when its second slot never applied."""
    workspace, epoch_id = _bootstrap_swiss_workspace(tmp_path, field_size=2)
    install_stub_adapter_factory(monkeypatch)
    install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 0.5},
        canned_pass_by_gen={"v0": True, "v1": True},
    )

    def stop_before_receipt(_root: Path, _intent: dict[str, Any]) -> None:
        raise _InjectedCrash("before receipt")

    monkeypatch.setattr(settlement_module, "commit_field_settlement", stop_before_receipt)
    with pytest.raises(_InjectedCrash, match="before receipt"):
        run_evolve_once(workspace, epoch_id, make_aux_responder([]))

    plan = prepare_resume(workspace, epoch_id)
    assert plan.classification == "discard_unrecorded_field"
    assert not (workspace / "epochs" / epoch_id / "generations" / "v1").exists()


def test_unrecorded_field_cleanup_preserves_a_terminal_diversity_rejection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Cleanup removes pending entrants while retaining a resolved soft reject."""
    # Both challengers state the same core idea, so the second is
    # soft-rejected for field diversity and settles before the crash.
    workspace, epoch_id = _bootstrap_swiss_workspace(
        tmp_path, field_size=2, idea="swap the greeting string"
    )
    install_stub_adapter_factory(monkeypatch)
    install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 0.5, "v2": 1.5},
        canned_pass_by_gen={"v0": True, "v1": True, "v2": True},
    )

    def stop_before_receipt(_root: Path, _intent: dict[str, Any]) -> None:
        raise _InjectedCrash("before receipt")

    monkeypatch.setattr(settlement_module, "commit_field_settlement", stop_before_receipt)
    with pytest.raises(_InjectedCrash, match="before receipt"):
        run_evolve_once(workspace, epoch_id, make_aux_responder([]))

    before = next(row for row in load_lineage(workspace)["epochs"] if row["id"] == epoch_id)
    assert {row["id"]: row["promoted"] for row in before["generations"]} | {
        "v1": None,
        "v2": False,
    } == {row["id"]: row["promoted"] for row in before["generations"]}

    plan = prepare_resume(workspace, epoch_id)
    assert plan.classification == "discard_unrecorded_field"
    after = next(row for row in load_lineage(workspace)["epochs"] if row["id"] == epoch_id)
    by_id = {row["id"]: row for row in after["generations"]}
    assert "v1" not in by_id
    assert by_id["v2"]["promoted"] is False
    assert read_experiment(workspace, epoch_id, "v2").outcome is not None


def test_crash_during_promotion_hook_delivery_is_not_retried(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The pre-call receipt write makes an interrupted side effect explicit."""
    from tests.test_on_promote_hook import _install_hooked_adapter_factory

    workspace, epoch_id = _bootstrap_swiss_workspace(tmp_path, field_size=2)
    _install_hooked_adapter_factory(monkeypatch)
    install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 0.5, "v2": 1.5},
        canned_pass_by_gen={"v0": True, "v1": True, "v2": True},
    )
    delivery_attempts = 0

    async def stop_during_delivery(*_args: Any, **_kwargs: Any) -> None:
        nonlocal delivery_attempts
        delivery_attempts += 1
        raise _InjectedCrash("hook delivery")

    monkeypatch.setattr(settlement_module, "fire_on_promote", stop_during_delivery)
    with pytest.raises(_InjectedCrash, match="hook delivery"):
        run_evolve_once(
            workspace,
            epoch_id,
            make_aux_responder([]),
        )
    receipt_path = field_settlement_intent_path(workspace, epoch_id, 0)
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["state"] == "committed"
    assert receipt["promotion_hook"]["state"] == "delivery_unknown"

    plan = prepare_resume(workspace, epoch_id)
    assert plan.classification == "clean"
    assert delivery_attempts == 1
    recovered = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert recovered["promotion_hook"]["state"] == "delivery_unknown"


@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        pytest.param("tournament_id", "wrong-field", id="identity"),
        pytest.param("epoch_id", "wrong-epoch", id="epoch"),
        pytest.param("structure", "racing", id="structure"),
        pytest.param("structure_params", {"field_size": 99}, id="structure-params"),
        pytest.param("champion_generation_id", "v2", id="champion"),
        pytest.param("competitors", [], id="candidates"),
        pytest.param("promoted_generation_ids", ["not-a-candidate"], id="promoted-set"),
        pytest.param("decision", "rejected", id="decision"),
        pytest.param("reason", None, id="reason-type"),
    ),
)
def test_field_record_conflicts_are_rejected_before_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    field: str,
    replacement: Any,
) -> None:
    """Every bracket identity and decision surface is checked before replay."""
    workspace, epoch_id, receipt = _workspace_with_pending_receipt(monkeypatch, tmp_path)
    receipt["field_tournament_record"][field] = replacement
    receipt_path = field_settlement_intent_path(workspace, epoch_id, 0)
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    with pytest.raises(RuntimeError, match="field settlement tournament record"):
        prepare_resume(workspace, epoch_id)
    _assert_field_is_unmutated(workspace, epoch_id)


def test_primary_champion_must_agree_across_multi_promotion_facts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A bracket edit cannot redirect the champion to a promoted sibling."""
    workspace, epoch_id, receipt = _workspace_with_pending_receipt(monkeypatch, tmp_path)
    first = next(row for row in receipt["candidates"] if row["generation_id"] == "v1")
    first["outcome"]["tournament_decision"] = "promoted"
    first["outcome"]["rejection_reason"] = ""
    record = receipt["field_tournament_record"]
    record["promoted_generation_id"] = "v1"
    record["promoted_generation_ids"] = ["v1", "v2"]
    receipt_path = field_settlement_intent_path(workspace, epoch_id, 0)
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    with pytest.raises(RuntimeError, match="primary champion conflicts"):
        prepare_resume(workspace, epoch_id)
    _assert_field_is_unmutated(workspace, epoch_id)


def test_existing_field_snapshot_conflict_is_rejected_before_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A receipt cannot silently replace conflicting canonical field identity."""
    workspace, epoch_id, _receipt = _workspace_with_pending_receipt(monkeypatch, tmp_path)
    snapshot_path = field_tournament_path(workspace, epoch_id, "v1")
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    snapshot["champion_generation_id"] = "v2"
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")

    with pytest.raises(RuntimeError, match="existing in-progress field tournament conflicts"):
        prepare_resume(workspace, epoch_id)
    _assert_field_is_unmutated(workspace, epoch_id)


def test_receipt_format_is_validated_before_committed_state_is_interpreted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A future-format record cannot bypass validation by claiming completion."""
    workspace, epoch_id, receipt = _workspace_with_pending_receipt(monkeypatch, tmp_path)
    receipt["format_version"] = 999
    receipt["state"] = "committed"
    receipt_path = field_settlement_intent_path(workspace, epoch_id, 0)
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    with pytest.raises(RuntimeError, match="unsupported format_version"):
        prepare_resume(workspace, epoch_id)
    _assert_field_is_unmutated(workspace, epoch_id)


def test_non_finite_settlement_scalar_is_rejected_before_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """NaN and infinity cannot enter lineage or index scalar columns."""
    workspace, epoch_id, receipt = _workspace_with_pending_receipt(monkeypatch, tmp_path)
    receipt["candidates"][0]["parent_scalar"] = float("nan")
    receipt_path = field_settlement_intent_path(workspace, epoch_id, 0)
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    with pytest.raises(RuntimeError, match="parent_scalar must be finite or null"):
        prepare_resume(workspace, epoch_id)
    _assert_field_is_unmutated(workspace, epoch_id)


def test_receipt_round_must_match_its_containing_namespace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Recovery rejects a valid receipt stored under a different round."""
    workspace, epoch_id, _receipt = _workspace_with_pending_receipt(monkeypatch, tmp_path)
    source = field_settlement_intent_path(workspace, epoch_id, 0)
    misplaced = field_settlement_intent_path(workspace, epoch_id, 1)
    misplaced.parent.mkdir(parents=True, exist_ok=True)
    source.replace(misplaced)

    with pytest.raises(RuntimeError, match="names round 0 inside round 1"):
        prepare_resume(workspace, epoch_id)
    _assert_field_is_unmutated(workspace, epoch_id)


@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        pytest.param("id", "wrong-experiment", id="id"),
        pytest.param("epoch_id", "wrong-epoch", id="epoch"),
        pytest.param("generation_id", "wrong-generation", id="generation"),
        pytest.param("round_index", 7, id="round"),
    ),
)
def test_experiment_coordinates_must_match_the_receipt_before_replay(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    field: str,
    replacement: Any,
) -> None:
    """Recovery never interprets a receipt against a different experiment."""
    workspace, epoch_id, _receipt = _workspace_with_pending_receipt(monkeypatch, tmp_path)
    path = experiment_json_path(workspace, epoch_id, "v1")
    experiment = json.loads(path.read_text(encoding="utf-8"))
    experiment[field] = replacement
    path.write_text(json.dumps(experiment), encoding="utf-8")

    with pytest.raises(RuntimeError, match="does not match its experiment"):
        prepare_resume(workspace, epoch_id)
    assert read_experiment(workspace, epoch_id, "v2").outcome is None


def test_every_settlement_candidate_must_name_a_parent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A missing parent cannot disappear from the shared-parent comparison."""
    workspace, epoch_id, _receipt = _workspace_with_pending_receipt(monkeypatch, tmp_path)
    path = experiment_json_path(workspace, epoch_id, "v1")
    experiment = json.loads(path.read_text(encoding="utf-8"))
    experiment["parent_generation_id"] = None
    path.write_text(json.dumps(experiment), encoding="utf-8")

    with pytest.raises(RuntimeError, match="has no parent generation"):
        prepare_resume(workspace, epoch_id)
    _assert_field_is_unmutated(workspace, epoch_id)


@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        pytest.param("parent_id", "v9", id="parent"),
        pytest.param("created_at", "2099-01-01T00:00:00Z", id="created-at"),
        pytest.param("round_index", 7, id="round"),
        pytest.param("promoted", True, id="verdict"),
    ),
)
def test_lineage_coordinates_and_verdict_must_match_before_replay(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    field: str,
    replacement: Any,
) -> None:
    """Recovery validates lineage-owned coordinates instead of rewriting them."""
    workspace, epoch_id, _receipt = _workspace_with_pending_receipt(monkeypatch, tmp_path)
    path = lineage_path(workspace)
    lineage = json.loads(path.read_text(encoding="utf-8"))
    epoch = next(row for row in lineage["epochs"] if row["id"] == epoch_id)
    generation = next(row for row in epoch["generations"] if row["id"] == "v1")
    generation[field] = replacement
    path.write_text(json.dumps(lineage), encoding="utf-8")

    with pytest.raises(RuntimeError, match="lineage generation 'v1'"):
        prepare_resume(workspace, epoch_id)
    assert read_experiment(workspace, epoch_id, "v1").outcome is None
    assert read_experiment(workspace, epoch_id, "v2").outcome is None


def test_receipt_corruption_is_unhealthy_and_prevents_partial_repair_acknowledgement(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A malformed receipt remains visible and blocks every acknowledgement write."""
    workspace, epoch_id, _receipt = _workspace_with_pending_receipt(monkeypatch, tmp_path)

    def fail_index_refresh(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("injected index failure")

    monkeypatch.setattr("zicato.index.ingest.ingest_field_settlement", fail_index_refresh)
    prepare_resume(workspace, epoch_id)
    corrupt = field_settlement_intent_path(workspace, epoch_id, 1)
    corrupt.parent.mkdir(parents=True, exist_ok=True)
    corrupt.write_text("[]", encoding="utf-8")

    with pytest.raises(RuntimeError, match="must contain a JSON object"):
        acknowledge_repaired_settlement_indexes(workspace)
    retained = json.loads(
        field_settlement_intent_path(workspace, epoch_id, 0).read_text(encoding="utf-8")
    )
    assert retained["index_projection"]["state"] == "repair_required"
    attention = epoch_settlement_receipt_attention(workspace, epoch_id)
    assert len(attention.index_repairs) == 1
    assert attention.corruptions[0]["exception_type"] == "RuntimeError"
    report = build_health_report(WorkspacePaths(workspace))
    assert report["healthy"] is False
    codes = {finding["code"] for finding in report["findings"]}
    assert {"settlement_receipt_corrupt", "settlement_index_repair_required"} <= codes
