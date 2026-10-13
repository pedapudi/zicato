"""Operator rounds: ``zicato proposer checkout`` / ``submit`` and the round slots.

Issue #570. An operator, or an agent for one, edits copies of the champion,
submits them with hypotheses as one round, and the next evolve round runs
them in its first slots under the normal gate. These tests pin:

* the queue: its record, its order, and which items a round consumes,
  skips or takes (:mod:`zicato.runtime.operator_queue`);
* the record: an operator experiment carries its provenance, and a
  proposer experiment is unchanged;
* the commands: checkout writes the copy and its record; submit gives the
  proposer's own findings, refuses another workspace and a round larger
  than the field, prints JSON on every refusal, and queues;
* the round: the candidates run with no proposal episode when exclusive,
  share a field with the proposer otherwise, never soft-reject each other,
  run against the round's champion after a promotion and record what they
  take back, and a candidate that cannot be minted is rejected without a
  proposer fallback.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from tests._contract_pins import deterministic_weights
from tests._orchestrator_harness import (
    bootstrap_workspace,
    evaluation_call_llm,
    install_stub_adapter_factory,
    install_telemetry_stubs,
    run_evolve_once,
)
from tests._workspace_support import read_experiment_record
from tests.test_orchestrator_multi_challenger import _bootstrap_swiss_workspace
from zicato.cli.commands.operator_candidate import checkout_cmd, queue_cmd, submit_cmd
from zicato.core.types import (
    Experiment,
    HypothesisSpec,
    OperatorProvenance,
    TournamentStructure,
)
from zicato.epoch.journal import read_experiment, read_journal, write_experiment
from zicato.evolve.generation_phase import current_generation
from zicato.runtime.control import list_pending_commands
from zicato.runtime.operator_queue import (
    OperatorGeneration,
    QueuedCandidate,
    QueuedRound,
    consume_operator_round,
    enqueue_operator_round,
    next_operator_round,
)
from zicato.runtime.paths import control_log_dir

V0_GREETING = 'GREETING = "hello"\n'
OPERATOR_GREETING = 'GREETING = "hello there"\n'
#: The span of the ``greeting`` point is the literal, so a patch carries it alone.
V0_VALUE = '"hello"'
OPERATOR_VALUE = '"hello there"'


def _hypothesis(**overrides: Any) -> dict[str, Any]:
    hypothesis: dict[str, Any] = {
        "core_idea": "Greet with more warmth",
        "modulating": ["greeting"],
        "why": "A warmer greeting reads as friendlier",
        "expected_pass_rate_delta": "+0 to +5 points",
        "expected_metric_movements": [
            {"metric_name": "drift:goal_drift", "direction": "decrease", "magnitude": "small"}
        ],
        "risks": "",
    }
    hypothesis.update(overrides)
    return hypothesis


def _greeting_patch(content: str = OPERATOR_VALUE) -> dict[str, Any]:
    return {
        "mutation_id": "greeting",
        "op": "replace",
        "new_content": content,
        "new_numeric": None,
        "new_enum": None,
    }


def _queued_candidate(**overrides: Any) -> QueuedCandidate:
    fields: dict[str, Any] = {
        "based_on": "v0",
        "hypothesis": _hypothesis(),
        "patches": (_greeting_patch(),),
    }
    fields.update(overrides)
    return QueuedCandidate(**fields)


def _round(epoch_id: str, *candidates: QueuedCandidate, **overrides: Any) -> QueuedRound:
    fields: dict[str, Any] = {
        "queue_id": "20261013000000-aaaaaaaa",
        "epoch_id": epoch_id,
        "author": "alice",
        "submitted_at": "2026-10-13T00:00:00Z",
        "candidates": candidates or (_queued_candidate(),),
    }
    fields.update(overrides)
    return QueuedRound(**fields)


def _pending_queue_ids(workspace: Path) -> list[str]:
    return [cmd.arg for cmd in list_pending_commands(workspace) if cmd.name == "candidate"]


def _audit_reasons(workspace: Path) -> list[str]:
    log_dir = control_log_dir(workspace)
    if not log_dir.exists():
        return []
    return [
        json.loads(path.read_text())["reason"]
        for path in sorted(log_dir.iterdir())
        if "_candidate_" in path.name
    ]


def _proposal_episodes(workspace: Path, epoch_id: str) -> list[str]:
    from zicato.proposer.input_capture import ROLE_PROPOSAL, read_proposer_inputs

    return [
        r["new_generation_id"]
        for r in read_proposer_inputs(workspace, epoch_id)
        if r["role"] == ROLE_PROPOSAL
    ]


def _gauntlet_workspace(tmp_path: Path) -> tuple[Path, str]:
    return bootstrap_workspace(
        tmp_path,
        weights=deterministic_weights(
            promote_margin=0.01, tournament_structure=TournamentStructure(structure="gauntlet")
        ),
    )


def _is_operator(workspace: Path, epoch_id: str, generation_id: str) -> bool:
    return read_experiment(workspace, epoch_id, generation_id).operator is not None


# ---------------------------------------------------------------------------
# The queue record and its consumption rules
# ---------------------------------------------------------------------------


def test_a_queued_round_round_trips_through_its_body() -> None:
    queued = _round("e1", exclusive=True, holdout_access=False)
    assert QueuedRound.from_body(json.loads(json.dumps(queued.body()))) == queued
    assert queued.provenance(queued.candidates[0], ["greet_logic"]) == OperatorProvenance(
        author="alice",
        based_on="v0",
        queue_id="20261013000000-aaaaaaaa",
        submitted_at="2026-10-13T00:00:00Z",
        holdout_access=False,
        reverted_points=("greet_logic",),
    )


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"format": 9}, "format"),
        ({"author": ""}, "author"),
        ({"exclusive": "yes"}, "exclusive"),
        ({"holdout_access": "no"}, "holdout_access"),
        ({"candidates": []}, "candidates"),
        ({"candidates": [{"based_on": "v0"}]}, "based_on, hypothesis and patches"),
    ],
)
def test_a_malformed_round_is_refused(change: dict[str, Any], message: str) -> None:
    body = {**_round("e1").body(), **change}
    with pytest.raises(ValueError, match=message):
        QueuedRound.from_body(body)


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        ({"mutation_id": "greeting"}, "exactly"),
        ({**_greeting_patch(), "op": "delete"}, "op must be one of"),
        ({**_greeting_patch(), "new_content": 7}, "needs a string new_content"),
        ({**_greeting_patch(), "new_enum": "x"}, "cannot carry new_enum"),
        (
            {**_greeting_patch(), "op": "set_numeric", "new_content": None, "new_numeric": True},
            "needs a number",
        ),
    ],
)
def test_a_queued_patch_with_wrong_types_is_refused(patch: dict[str, Any], message: str) -> None:
    body = _round("e1").body()
    body["candidates"][0]["patches"] = [patch]
    with pytest.raises(ValueError, match=message):
        QueuedRound.from_body(body)


def test_the_round_takes_the_oldest_usable_item_and_consumes_the_dead_ones(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    two = (_queued_candidate(), _queued_candidate())
    enqueue_operator_round(workspace, _round("e0", queue_id="1-other-epoch"))
    enqueue_operator_round(workspace, _round("e1", queue_id="2-settled"))
    enqueue_operator_round(workspace, _round("e1", queue_id="3-pending"))
    enqueue_operator_round(workspace, _round("e1", *two, queue_id="4-too-large"))
    enqueue_operator_round(workspace, _round("e1", queue_id="5-usable"))
    enqueue_operator_round(workspace, _round("e1", queue_id="6-later"))
    candidate_dir = workspace / "runtime" / "control" / "candidate"
    (candidate_dir / "0-unreadable").write_text("{")
    (candidate_dir / "0-not-utf8").write_bytes(b"\xff\xfe\xfa")

    minted = {
        "2-settled": [OperatorGeneration(generation_id="v1", settled=True)],
        "3-pending": [
            OperatorGeneration(generation_id="v2", settled=True),
            OperatorGeneration(generation_id="v3", settled=False),
        ],
    }
    pending = next_operator_round(workspace, "e1", slots=1, minted=lambda: minted)

    assert pending is not None
    assert pending.queued.queue_id == "5-usable"
    # A taken item stays queued until its round settles; the pending one
    # stays for recovery; the dead ones are gone, each with its reason.
    assert _pending_queue_ids(workspace) == ["3-pending", "5-usable", "6-later"]
    reasons = _audit_reasons(workspace)
    assert sum(r.startswith("unreadable operator round") for r in reasons) == 2
    assert any("cannot run in epoch 'e1'" in r for r in reasons)
    assert "operator round already minted as v1" in reasons
    assert any("has 2 candidates, but the round has 1 slot(s)" in r for r in reasons)


def test_an_empty_queue_reads_no_experiments(tmp_path: Path) -> None:
    def _fail() -> dict[str, list[OperatorGeneration]]:
        raise AssertionError("the minted scan ran for an empty queue")

    assert next_operator_round(tmp_path, "e1", slots=1, minted=_fail) is None


def test_consume_records_the_full_body_in_the_control_log(tmp_path: Path) -> None:
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    queued = _round("e1")
    enqueue_operator_round(workspace, queued)
    assert consume_operator_round(workspace, queued.queue_id, reason="test")
    assert not consume_operator_round(workspace, queued.queue_id, reason="again")
    assert _pending_queue_ids(workspace) == []
    (record,) = (json.loads(p.read_text()) for p in control_log_dir(workspace).iterdir())
    assert record["reason"] == "test"
    assert json.loads(record["payload"]) == queued.body()


# ---------------------------------------------------------------------------
# The experiment record
# ---------------------------------------------------------------------------


def _experiment(operator: OperatorProvenance | None) -> Experiment:
    return Experiment(
        id="exp_e1_v1",
        epoch_id="e1",
        generation_id="v1",
        parent_generation_id="v0",
        proposed_at="2026-10-13T00:00:00Z",
        hypothesis=HypothesisSpec(
            core_idea="Greet with more warmth",
            modulating=("greeting",),
            why="friendlier",
            expected_pass_rate_delta="+1",
        ),
        patches=(),
        outcome=None,
        operator=operator,
    )


def test_operator_provenance_round_trips_and_a_proposer_record_has_no_key(
    tmp_path: Path,
) -> None:
    queued = _round("e1")
    provenance = queued.provenance(queued.candidates[0], ["greet_logic"])
    write_experiment(tmp_path, "e1", "v1", _experiment(provenance))
    body = read_experiment_record(
        tmp_path / "epochs" / "e1" / "generations" / "v1" / "experiment.json"
    )
    assert body["operator"]["author"] == "alice"
    assert read_experiment(tmp_path, "e1", "v1").operator == provenance
    assert (
        "**source**: operator alice, based on v0; " "takes back the promoted change at greet_logic"
    ) in read_journal(tmp_path, "e1")

    write_experiment(tmp_path, "e1", "v2", replace(_experiment(None), generation_id="v2"))
    proposer = read_experiment_record(
        tmp_path / "epochs" / "e1" / "generations" / "v2" / "experiment.json"
    )
    assert "operator" not in proposer
    assert read_experiment(tmp_path, "e1", "v2").operator is None


# ---------------------------------------------------------------------------
# The commands
# ---------------------------------------------------------------------------


def _checkout(workspace: Path, into: Path) -> dict[str, Any]:
    result = CliRunner().invoke(checkout_cmd, ["--workspace", str(workspace), "--into", str(into)])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def _edit_and_fill(into: Path, greeting: str = OPERATOR_GREETING, **hypothesis: Any) -> None:
    agent = into / "source" / "agent.py"
    agent.write_text(agent.read_text().replace(V0_GREETING, greeting))
    (into / "hypothesis.json").write_text(json.dumps(_hypothesis(**hypothesis)))


def _submit(*args: str) -> Any:
    return CliRunner().invoke(submit_cmd, ["--author", "alice", *args])


def _queue_status(workspace: Path, queue_id: str) -> dict[str, Any]:
    result = CliRunner().invoke(queue_cmd, ["--workspace", str(workspace), "--queue-id", queue_id])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)["round"]


def _refusal(result: Any) -> dict[str, Any]:
    assert result.exit_code == 1, result.output
    report = json.loads(result.output)
    assert report["ok"] is False
    return report


def test_checkout_writes_the_copy_the_template_and_its_record(tmp_path: Path) -> None:
    workspace, epoch_id = bootstrap_workspace(tmp_path)
    into = tmp_path / "cand"
    report = _checkout(workspace, into)

    assert report["based_on"] == "v0"
    assert report["epoch"] == epoch_id
    assert report["mutation_points"] == ["greet_logic", "greeting"]
    assert "brief.md" in report["evidence"]
    assert V0_GREETING in (into / "source" / "agent.py").read_text()
    record = json.loads((into / "checkout.json").read_text())
    assert record["workspace"] == str(workspace.resolve())
    assert record["based_on"] == "v0"
    assert set(json.loads((into / "hypothesis.json").read_text())) == set(_hypothesis())


def test_checkout_refuses_a_workspace_without_an_epoch_in_json(tmp_path: Path) -> None:
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    (workspace / "config.json").write_text(json.dumps({"instance_id": "test"}))
    result = CliRunner().invoke(
        checkout_cmd, ["--workspace", str(workspace), "--into", str(tmp_path / "cand")]
    )
    (finding,) = _refusal(result)["findings"]
    assert "zicato epoch new" in finding


def test_checkout_before_the_first_round_copies_the_registered_source(tmp_path: Path) -> None:
    """Before round 1 seeds v0, the copy is the source the round will seed."""
    workspace, epoch_id = bootstrap_workspace(tmp_path, mutable_trees=("src",))
    seed_source = tmp_path / "src"
    seed_source.mkdir()
    (seed_source / "agent.py").write_text('# zicato:mutable id="greeting"\n' + V0_GREETING)
    snapshot = workspace / "epochs" / epoch_id / "generations" / "v0" / "snapshot"
    for path in sorted(snapshot.rglob("*"), reverse=True):
        path.unlink() if path.is_file() else path.rmdir()
    snapshot.rmdir()

    into = tmp_path / "cand"
    report = _checkout(workspace, into)
    assert report["mutation_points"] == ["greeting"]
    assert (into / "source" / "src" / "agent.py").read_text().endswith(V0_GREETING)


def test_submit_queues_the_copies_as_one_round(tmp_path: Path) -> None:
    workspace, epoch_id = _bootstrap_swiss_workspace(tmp_path, field_size=2)
    first, second = tmp_path / "a", tmp_path / "b"
    for into, greeting in ((first, OPERATOR_GREETING), (second, 'GREETING = "hi"\n')):
        _checkout(workspace, into)
        _edit_and_fill(into, greeting)

    result = _submit("--from", str(first), "--from", str(second), "--exclusive")
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["ok"] is True
    assert report["slots"] == 2
    assert [c["patches"] for c in report["candidates"]] == [1, 1]
    assert _pending_queue_ids(workspace) == [report["queue_id"]]
    queued = QueuedRound.from_body(json.loads(Path(report["queue_file"]).read_text()))
    assert queued.epoch_id == epoch_id
    assert queued.exclusive is True
    assert queued.holdout_access is None
    assert [c.patches for c in queued.candidates] == [
        (_greeting_patch(),),
        (_greeting_patch('"hi"'),),
    ]


def test_submit_refuses_more_candidates_than_the_field_has_slots(tmp_path: Path) -> None:
    workspace, _epoch_id = _gauntlet_workspace(tmp_path)
    first, second = tmp_path / "a", tmp_path / "b"
    for into in (first, second):
        _checkout(workspace, into)
        _edit_and_fill(into)
    (finding,) = _refusal(_submit("--from", str(first), "--from", str(second)))["findings"]
    assert "has 1 challenger slot(s), so it cannot hold 2 candidates" in finding


def test_submit_reports_an_edit_outside_the_mutation_points_as_the_proposer_sees_it(
    tmp_path: Path,
) -> None:
    workspace, _epoch_id = bootstrap_workspace(tmp_path)
    into = tmp_path / "cand"
    _checkout(workspace, into)
    _edit_and_fill(into)
    agent = into / "source" / "agent.py"
    agent.write_text(agent.read_text().replace("Stub harness", "Edited harness"))

    (candidate,) = _refusal(_submit("--from", str(into)))["candidates"]
    assert any(
        "working source differs from the reconstructed patch source" in f
        for f in candidate["findings"]
    )
    assert _pending_queue_ids(workspace) == []


def test_submit_reports_a_hypothesis_that_the_proposer_parser_refuses(tmp_path: Path) -> None:
    workspace, _epoch_id = bootstrap_workspace(tmp_path)
    into = tmp_path / "cand"
    _checkout(workspace, into)
    _edit_and_fill(into, modulating=["no_such_point"])

    (candidate,) = _refusal(_submit("--from", str(into)))["candidates"]
    (finding,) = candidate["findings"]
    assert "does not match any known mutation point" in finding


def test_submit_refuses_another_workspace_and_an_unchanged_copy_in_json(tmp_path: Path) -> None:
    workspace, _epoch_id = bootstrap_workspace(tmp_path)
    into = tmp_path / "cand"
    _checkout(workspace, into)
    (into / "hypothesis.json").write_text(json.dumps(_hypothesis()))

    other = _submit("--from", str(into), "--workspace", str(tmp_path / "other" / ".zicato"))
    (finding,) = _refusal(other)["findings"]
    assert "were checked out from" in finding

    (candidate,) = _refusal(_submit("--from", str(into)))["candidates"]
    assert candidate["findings"] == ["the copy is unchanged; edit a declared mutation point first"]


# ---------------------------------------------------------------------------
# The round
# ---------------------------------------------------------------------------


def _queue_from_cli(workspace: Path, into: Path, *args: str) -> str:
    _checkout(workspace, into)
    _edit_and_fill(into)
    result = _submit("--from", str(into), *args)
    assert result.exit_code == 0, result.output
    return str(json.loads(result.output)["queue_id"])


def test_an_exclusive_round_runs_the_queued_candidate_with_no_proposal_episode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """From checkout to settlement through the commands and the real round."""
    workspace, epoch_id = bootstrap_workspace(tmp_path)
    queue_id = _queue_from_cli(
        workspace, tmp_path / "cand", "--exclusive", "--holdout-access", "no"
    )
    install_stub_adapter_factory(monkeypatch)
    install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 0.5},
        canned_pass_by_gen={"v0": True, "v1": True},
    )

    outcome = run_evolve_once(workspace, epoch_id, evaluation_call_llm)

    assert outcome.tournament_decision == "promoted"
    assert outcome.proposed_generation_id == "v1"
    assert current_generation(workspace, epoch_id) == "v1"
    assert _proposal_episodes(workspace, epoch_id) == []
    experiment = read_experiment(workspace, epoch_id, "v1")
    assert experiment.parent_generation_id == "v0"
    assert experiment.operator is not None
    assert experiment.operator.queue_id == queue_id
    assert experiment.operator.holdout_access is False
    assert experiment.operator.reverted_points == ()
    assert experiment.patches[0].rationale == "Greet with more warmth"
    snapshot = workspace / "epochs" / epoch_id / "generations" / "v1" / "snapshot"
    assert OPERATOR_GREETING in (snapshot / "agent.py").read_text()
    # Settlement took the item off the queue, with its generations.
    assert _pending_queue_ids(workspace) == []
    assert "minted as v1; round 0 settled" in _audit_reasons(workspace)
    status = _queue_status(workspace, queue_id)
    assert status["state"] == "settled"
    assert status["author"] == "alice"
    assert status["exclusive"] is True
    assert [(g["generation_id"], g["decision"]) for g in status["generations"]] == [
        ("v1", "promoted")
    ]


def test_after_a_promotion_the_operator_version_runs_against_the_new_champion(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Checked out at v0; a proposer round promoted v1 before the submit.

    The operator edited only ``greet_logic``. The operator's tree (v0 plus
    that edit) enters round 2 as a child of v1, so it also takes back v1's
    change to ``greeting``, and the record names that point.
    """
    workspace, epoch_id = _gauntlet_workspace(tmp_path)
    into = tmp_path / "cand"
    _checkout(workspace, into)
    agent = into / "source" / "agent.py"
    agent.write_text(agent.read_text().replace('GREETING + " " + name', 'GREETING + ", " + name'))
    (into / "hypothesis.json").write_text(json.dumps(_hypothesis(modulating=["greet_logic"])))

    install_stub_adapter_factory(monkeypatch)
    install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 1.0, "v2": 0.5},
        canned_pass_by_gen={"v0": True, "v1": True, "v2": True},
    )
    first = run_evolve_once(workspace, epoch_id, evaluation_call_llm, round_index=0)
    assert first.tournament_decision == "promoted"
    assert current_generation(workspace, epoch_id) == "v1"

    result = _submit("--from", str(into))
    assert result.exit_code == 0, result.output
    second = run_evolve_once(workspace, epoch_id, evaluation_call_llm, round_index=1)

    assert second.parent_generation_id == "v1"
    experiment = read_experiment(workspace, epoch_id, "v2")
    assert experiment.parent_generation_id == "v1"
    assert experiment.operator is not None
    assert experiment.operator.based_on == "v0"
    assert experiment.operator.reverted_points == ("greeting",)
    v2_source = (
        workspace / "epochs" / epoch_id / "generations" / "v2" / "snapshot" / "agent.py"
    ).read_text()
    assert v2_source == agent.read_text()


def test_an_exclusive_round_runs_its_candidates_alone_in_a_wide_field(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Two operator candidates with the same idea: neither soft-rejects the other."""
    workspace, epoch_id = _bootstrap_swiss_workspace(tmp_path, field_size=3)
    twin = _queued_candidate(patches=(_greeting_patch('"hi"'),))
    enqueue_operator_round(workspace, _round(epoch_id, _queued_candidate(), twin, exclusive=True))
    install_stub_adapter_factory(monkeypatch)
    install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 0.5, "v2": 1.0},
        canned_pass_by_gen={"v0": True, "v1": True, "v2": True},
    )

    run_evolve_once(workspace, epoch_id, evaluation_call_llm)

    generations = workspace / "epochs" / epoch_id / "generations"
    assert _is_operator(workspace, epoch_id, "v1")
    assert _is_operator(workspace, epoch_id, "v2")
    assert not (generations / "v3").exists()
    assert _proposal_episodes(workspace, epoch_id) == []
    for generation_id in ("v1", "v2"):
        outcome = read_experiment(workspace, epoch_id, generation_id).outcome
        assert outcome is not None
        assert "field_diversity" not in outcome.rejection_reason
    assert "minted as v1, v2; round 0 settled" in _audit_reasons(workspace)


def test_a_shared_round_takes_the_first_slots_and_the_proposer_fills_the_rest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace, epoch_id = _bootstrap_swiss_workspace(tmp_path, field_size=2)
    enqueue_operator_round(workspace, _round(epoch_id))
    install_stub_adapter_factory(monkeypatch)
    install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 0.5, "v2": 1.5},
        canned_pass_by_gen={"v0": True, "v1": True, "v2": True},
    )

    run_evolve_once(workspace, epoch_id, evaluation_call_llm)

    assert _is_operator(workspace, epoch_id, "v1")
    assert not _is_operator(workspace, epoch_id, "v2")
    assert _proposal_episodes(workspace, epoch_id) == ["v2"]


def test_a_candidate_that_cannot_be_minted_is_rejected_without_a_proposer_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace, epoch_id = bootstrap_workspace(tmp_path)
    broken = _queued_candidate(hypothesis=_hypothesis(modulating=["no_such_point"]))
    enqueue_operator_round(workspace, _round(epoch_id, broken, exclusive=True))
    install_stub_adapter_factory(monkeypatch)
    install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0},
        canned_pass_by_gen={"v0": True},
    )

    outcome = run_evolve_once(workspace, epoch_id, evaluation_call_llm)

    assert outcome.tournament_decision != "promoted"
    assert current_generation(workspace, epoch_id) == "v0"
    assert _proposal_episodes(workspace, epoch_id) == []
    assert _pending_queue_ids(workspace) == []
    (reason,) = _audit_reasons(workspace)
    assert reason.startswith("rejected before the tournament: v1:")
    assert "does not match any known mutation point" in reason
    status = _queue_status(workspace, "20261013000000-aaaaaaaa")
    assert status["state"] == "rejected"
    assert status["generations"] == []
    assert status["reason"] == reason


def test_a_candidate_identical_to_the_champion_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace, epoch_id = bootstrap_workspace(tmp_path)
    same = _queued_candidate(patches=(_greeting_patch(V0_VALUE),))
    enqueue_operator_round(workspace, _round(epoch_id, same, exclusive=True))
    install_stub_adapter_factory(monkeypatch)
    install_telemetry_stubs(
        monkeypatch, canned_loss_by_gen={"v0": 2.0}, canned_pass_by_gen={"v0": True}
    )

    run_evolve_once(workspace, epoch_id, evaluation_call_llm)

    (reason,) = _audit_reasons(workspace)
    assert "identical to the champion v0" in reason


def test_queue_ids_sort_in_submit_order_within_one_second() -> None:
    from zicato.runtime.operator_queue import new_queue_id

    ids = [new_queue_id() for _ in range(50)]
    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids)


def test_an_item_that_cannot_be_read_now_stays_queued(tmp_path: Path) -> None:
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    path = enqueue_operator_round(workspace, _round("e1"))
    path.chmod(0)
    try:
        assert next_operator_round(workspace, "e1", slots=1, minted=dict) is None
        assert _audit_reasons(workspace) == []
    finally:
        path.chmod(0o644)
    assert _pending_queue_ids(workspace) == ["20261013000000-aaaaaaaa"]


def test_submit_refuses_an_empty_author_in_json(tmp_path: Path) -> None:
    workspace, _epoch_id = bootstrap_workspace(tmp_path)
    into = tmp_path / "cand"
    _checkout(workspace, into)
    _edit_and_fill(into)
    result = CliRunner().invoke(submit_cmd, ["--from", str(into), "--author", "  "])
    (finding,) = _refusal(result)["findings"]
    assert "--author" in finding
    assert _pending_queue_ids(workspace) == []


def test_a_store_failure_while_minting_rejects_the_item_and_the_loop_continues(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An error from the store must not fail every round that takes the item."""
    from zicato.epoch.genstore import DirectoryGenerationStore

    workspace, epoch_id = bootstrap_workspace(tmp_path)
    enqueue_operator_round(workspace, _round(epoch_id, exclusive=True))
    install_stub_adapter_factory(monkeypatch)
    install_telemetry_stubs(
        monkeypatch, canned_loss_by_gen={"v0": 2.0}, canned_pass_by_gen={"v0": True}
    )

    def _broken(*_args: Any, **_kwargs: Any) -> Path:
        raise RuntimeError("the store failed")

    monkeypatch.setattr(DirectoryGenerationStore, "derive_scratch", _broken)

    run_evolve_once(workspace, epoch_id, evaluation_call_llm)

    assert _pending_queue_ids(workspace) == []
    (reason,) = _audit_reasons(workspace)
    assert "the store failed" in reason


def test_queue_reports_a_queued_and_a_running_round_and_refuses_an_unknown_id(
    tmp_path: Path,
) -> None:
    workspace, epoch_id = bootstrap_workspace(tmp_path)
    enqueue_operator_round(workspace, _round(epoch_id, queue_id="1-waiting"))
    running = _round(epoch_id, queue_id="2-running")
    enqueue_operator_round(workspace, running)
    provenance = running.provenance(running.candidates[0])
    write_experiment(
        workspace,
        epoch_id,
        "v1",
        replace(_experiment(provenance), epoch_id=epoch_id, id=f"exp_{epoch_id}_v1"),
    )

    result = CliRunner().invoke(queue_cmd, ["--workspace", str(workspace)])
    assert result.exit_code == 0, result.output
    rounds = {r["queue_id"]: r for r in json.loads(result.output)["rounds"]}
    assert rounds["1-waiting"]["state"] == "queued"
    assert rounds["1-waiting"]["generations"] == []
    assert rounds["2-running"]["state"] == "running"
    assert [g["generation_id"] for g in rounds["2-running"]["generations"]] == ["v1"]
    assert rounds["2-running"]["generations"][0]["decision"] is None

    unknown = CliRunner().invoke(
        queue_cmd, ["--workspace", str(workspace), "--queue-id", "no-such-round"]
    )
    (finding,) = _refusal(unknown)["findings"]
    assert "No submitted round has queue id 'no-such-round'" in finding


def test_queue_lists_the_candidates_a_round_rejected_beside_the_ones_that_ran(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace, epoch_id = _bootstrap_swiss_workspace(tmp_path, field_size=2)
    broken = _queued_candidate(hypothesis=_hypothesis(modulating=["no_such_point"]))
    enqueue_operator_round(workspace, _round(epoch_id, _queued_candidate(), broken, exclusive=True))
    install_stub_adapter_factory(monkeypatch)
    install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 0.5},
        canned_pass_by_gen={"v0": True, "v1": True},
    )

    run_evolve_once(workspace, epoch_id, evaluation_call_llm)

    status = _queue_status(workspace, "20261013000000-aaaaaaaa")
    assert status["state"] == "settled"
    assert [g["generation_id"] for g in status["generations"]] == ["v1"]
    (rejected,) = status["rejected_candidates"]
    assert rejected["candidate"] == 1
    assert rejected["generation_id"] == "v2"
    assert "does not match any known mutation point" in rejected["reason"]


def test_queue_reads_the_generations_before_the_queue_file(tmp_path: Path) -> None:
    """A settled round whose queue file a crash left behind still reads as settled."""
    from zicato.core.types import OutcomeRecord, TournamentDecision

    workspace, epoch_id = bootstrap_workspace(tmp_path)
    queued = _round(epoch_id)
    enqueue_operator_round(workspace, queued)
    experiment = replace(
        _experiment(queued.provenance(queued.candidates[0])),
        epoch_id=epoch_id,
        id=f"exp_{epoch_id}_v1",
        outcome=OutcomeRecord(
            ran_at="2026-10-13T00:00:00Z",
            pass_rate_delta=0.0,
            drift_loss_delta=-1.0,
            scalar_score_delta=-1.0,
            tournament_decision=TournamentDecision.PROMOTED,
        ),
    )
    write_experiment(workspace, epoch_id, "v1", experiment)

    status = _queue_status(workspace, queued.queue_id)
    assert status["state"] == "settled"
    assert status["generations"][0]["decision"] == "promoted"


def test_a_queue_file_wins_over_its_audit_record(tmp_path: Path) -> None:
    """A crash after the audit write leaves the item queued; the next round runs it."""
    workspace, epoch_id = bootstrap_workspace(tmp_path)
    queued = _round(epoch_id)
    enqueue_operator_round(workspace, queued)
    consume_operator_round(workspace, queued.queue_id, reason="rejected before the tournament: x")
    enqueue_operator_round(workspace, queued)

    status = _queue_status(workspace, queued.queue_id)
    assert status["state"] == "queued"
    assert status["reason"] is None


def test_the_listing_survives_a_consumer_that_removes_the_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import zicato.runtime.operator_queue as operator_queue

    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    enqueue_operator_round(workspace, _round("e1"))
    real = operator_queue.list_pending_commands
    calls = []

    def _racing(root: Path) -> Any:
        calls.append(root)
        if len(calls) == 1:
            raise FileNotFoundError("control/candidate vanished")
        return real(root)

    monkeypatch.setattr(operator_queue, "list_pending_commands", _racing)
    assert [r.queue_id for r in operator_queue.operator_round_records(workspace)] == [
        "20261013000000-aaaaaaaa"
    ]


def test_queue_reports_unreadable_experiment_records(tmp_path: Path) -> None:
    workspace, epoch_id = bootstrap_workspace(tmp_path)
    queued = _round(epoch_id)
    enqueue_operator_round(workspace, queued)
    broken = workspace / "epochs" / epoch_id / "generations" / "v1"
    broken.mkdir(parents=True)
    (broken / "experiment.json").write_text("{")

    result = CliRunner().invoke(queue_cmd, ["--workspace", str(workspace)])
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["record_errors"]
    assert any("v1" in error for error in report["record_errors"])
