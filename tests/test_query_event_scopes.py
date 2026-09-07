"""Recorded candidate and comparison ownership survives incomplete round logs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_query_execution_plan import EPOCH, _of_kind, _plan, _workspace, _write_loss
from zicato.epoch.round_log import (
    CandidateSampled,
    CritiqueSelected,
    EvidenceReplicated,
    GateEvaluated,
    HoldoutReleased,
    PatchesApplied,
    ProposalAttempted,
    RoundLog,
    RoundOpened,
    ValidationFailed,
    round_log_path,
)


@pytest.mark.parametrize("first_candidate_missing", [False, True])
def test_interleaved_comparisons_keep_their_recorded_owners(
    tmp_path: Path, first_candidate_missing: bool
) -> None:
    root = _workspace(tmp_path)
    log = RoundLog(root, EPOCH, 0)
    log.append(RoundOpened())
    for generation, decision in (("v2", "reject"), ("v1", "promote")):
        scope = {
            "generation_id": generation,
            "step": "gate",
            "attributes": {"matchup_id": f"match-{generation}", "opponent_generation_id": "v0"},
        }
        log.append(GateEvaluated(decision=decision), scope=scope)
    recorded = {node["id"]: node for node in _of_kind(_plan(root), "gate_evaluation")}
    if first_candidate_missing:
        path = round_log_path(root, EPOCH, 0)
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        path.write_text("".join(json.dumps(row) + "\n" for row in rows if row["seq"] != 3))
    gates = _of_kind(_plan(root), "gate_evaluation")
    assert all(node == recorded[node["id"]] for node in gates)
    assert [(n["coordinates"]["generation_id"], n["outcome"]["decision"]) for n in gates] == (
        [("v2", "reject")] if first_candidate_missing else [("v2", "reject"), ("v1", "promote")]
    )
    assert all(n["coordinates"]["opponent_generation_id"] == "v0" for n in gates)
    assert all(
        n["coordinates"]["matchup_id"] == f"match-{n['coordinates']['generation_id']}"
        for n in gates
    )


def test_interleaved_proposals_keep_candidate_summaries_and_survive_interruption(
    tmp_path: Path,
) -> None:
    root = _workspace(tmp_path)
    log = RoundLog(root, EPOCH, 0)
    log.append(RoundOpened())
    for generation, index in (("v2", 1), ("v1", 0)):
        scope = {"generation_id": generation, "step": "propose"}
        log.append(ProposalAttempted(slot_index=index), scope=scope)
        log.append(CandidateSampled(i=index, n=2), scope=scope)
    for generation, index, reason in (("v1", 0, "first choice"), ("v2", 1, "second choice")):
        log.append(
            CritiqueSelected(index=index, reason=reason),
            scope={"generation_id": generation, "step": "propose"},
        )
    path = round_log_path(root, EPOCH, 0)
    with path.open("a") as stream:
        stream.write('{"seq":8')
    plan = _plan(root)
    attempts = _of_kind(plan, "proposal_attempt")
    assert [node["coordinates"]["generation_id"] for node in attempts] == ["v2", "v1"]
    assert [node["coordinates"]["event_seq"] for node in attempts] == [2, 4]
    proposal = _of_kind(plan, "propose_step")[0]
    summaries = {row["generation_id"]: row for row in proposal["outcome"]["candidates"]}
    assert summaries["v1"]["critique_index"] == 0
    assert summaries["v1"]["critique_reason"] == "first choice"
    assert summaries["v2"]["critique_index"] == 1
    assert summaries["v2"]["critique_reason"] == "second choice"
    assert _of_kind(plan, "round")[0]["status"] == "running"


def test_each_released_holdout_and_evidence_record_keeps_its_candidate(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    log = RoundLog(root, EPOCH, 0)
    log.append(RoundOpened())
    for generation, confirmed in (("v1", True), ("v2", False)):
        scope = {"generation_id": generation, "step": "gate"}
        log.append(HoldoutReleased(confirmed=confirmed), scope=scope)
        log.append(EvidenceReplicated(ci_state={"replicates_spent": 1}), scope=scope)
    plan = _plan(root)
    released = _of_kind(plan, "holdout_release")
    assert [(n["coordinates"]["generation_id"], n["outcome"]["confirmed"]) for n in released] == [
        ("v1", True),
        ("v2", False),
    ]
    evidence = _of_kind(plan, "gate_step")[0]["outcome"]["evidence_trail"]
    assert [row["coordinates"]["generation_id"] for row in evidence] == ["v1", "v2"]


def test_missing_comparison_scope_is_partial_and_extensions_are_not_exposed(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    log = RoundLog(root, EPOCH, 0)
    log.append(RoundOpened())
    log.append(ProposalAttempted(), scope={"generation_id": "v1", "step": "propose"})
    log.append(
        GateEvaluated(decision="reject"),
        scope={"step": "gate", "attributes": {"task_text": "protected scope canary"}},
    )
    plan = _plan(root)
    gate = _of_kind(plan, "gate_evaluation")[0]
    assert gate["provenance"] == "partial"
    assert "generation_id" not in gate["coordinates"]
    assert "scope" in gate["outcome"]["note"]
    assert "protected scope canary" not in json.dumps(plan)


@pytest.mark.parametrize("event", [CandidateSampled(i=0, n=1), EvidenceReplicated()])
def test_missing_scope_on_summary_only_events_marks_the_round_partial(
    tmp_path: Path, event
) -> None:
    root = _workspace(tmp_path)
    log = RoundLog(root, EPOCH, 0)
    log.append(RoundOpened())
    log.append(event)
    assert _of_kind(_plan(root), "round")[0]["provenance"] == "partial"


def test_interleaved_validation_failures_keep_their_candidate(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    log = RoundLog(root, EPOCH, 0)
    log.append(RoundOpened())
    for generation in ("v1", "v2"):
        log.append(
            PatchesApplied(generation_id=generation),
            scope={"generation_id": generation, "step": "apply"},
        )
    for generation in ("v2", "v1"):
        log.append(
            ValidationFailed(findings=(f"invalid {generation}",)),
            scope={"generation_id": generation, "step": "apply"},
        )
    failures = _of_kind(_plan(root), "validate")
    assert [(n["coordinates"]["generation_id"], n["outcome"]["findings"]) for n in failures] == [
        ("v2", ["invalid v2"]),
        ("v1", ["invalid v1"]),
    ]


def test_application_scope_disagreement_cannot_claim_another_candidate(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    _write_loss(root, "v2", "login")
    log = RoundLog(root, EPOCH, 0)
    log.append(RoundOpened())
    log.append(PatchesApplied(generation_id="v2"), scope={"generation_id": "v1", "step": "apply"})
    plan = _plan(root)
    applied = _of_kind(plan, "apply_patches")[0]
    assert applied["provenance"] == "partial"
    assert "generation_id" not in applied["coordinates"]
    assert "disagrees" in applied["outcome"]["note"]
    assert _of_kind(plan, "run_step")[0]["children"] == []
