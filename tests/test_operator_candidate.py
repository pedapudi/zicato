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
  are refused once another generation is the champion, and a candidate that
  cannot be minted is rejected without a proposer fallback;
* the envelope: no operator-authored text reaches anything built for the
  proposer or its critic, in the same round or a later one;
* the gate: an operator candidate is confirmed on the holdout and charged
  against the holdout query budget as a proposer candidate is, and a field
  that crash recovery discards runs its queued item again.
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
from zicato.cli.commands.operator_candidate import checkout_cmd, submit_cmd
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
        "holdout_access": False,
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
    queued = _round("e1", exclusive=True, holdout_access=True)
    assert QueuedRound.from_body(json.loads(json.dumps(queued.body()))) == queued
    assert queued.provenance() == OperatorProvenance(
        author="alice",
        queue_id="20261013000000-aaaaaaaa",
        submitted_at="2026-10-13T00:00:00Z",
        holdout_access=True,
    )


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"format": 9}, "format"),
        ({"author": ""}, "author"),
        ({"exclusive": "yes"}, "exclusive"),
        ({"holdout_access": "no"}, "holdout_access"),
        ({"holdout_access": None}, "holdout_access"),
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
    current = _queued_candidate(based_on="v4")
    two = (current, current)
    enqueue_operator_round(workspace, _round("e0", queue_id="1-other-epoch"))
    enqueue_operator_round(workspace, _round("e1", queue_id="2-settled"))
    enqueue_operator_round(workspace, _round("e1", queue_id="3-pending"))
    stale = _queued_candidate(based_on="v0")
    enqueue_operator_round(workspace, _round("e1", stale, queue_id="35-stale"))
    enqueue_operator_round(workspace, _round("e1", *two, queue_id="4-too-large"))
    enqueue_operator_round(workspace, _round("e1", current, queue_id="5-usable"))
    enqueue_operator_round(workspace, _round("e1", current, queue_id="6-later"))
    candidate_dir = workspace / "runtime" / "control" / "candidate"
    (candidate_dir / "0-unreadable").write_text("{")
    (candidate_dir / "0-not-utf8").write_bytes(b"\xff\xfe\xfa")

    minted = {"2-settled": {"v1": True}, "3-pending": {"v2": True, "v3": False}}
    pending = next_operator_round(workspace, "e1", champion_id="v4", slots=1, minted=lambda: minted)

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
    # An item that edits an older champion is refused; the round never
    # rebuilds the older tree, which would take back the promoted changes.
    assert (
        "operator round edits v0, but the champion is now v4; "
        "run `zicato proposer checkout` again and resubmit"
    ) in reasons


def test_an_empty_queue_reads_no_experiments(tmp_path: Path) -> None:
    def _fail() -> dict[str, dict[str, bool]]:
        raise AssertionError("the minted scan ran for an empty queue")

    assert next_operator_round(tmp_path, "e1", champion_id="v0", slots=1, minted=_fail) is None


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
    provenance = _round("e1", holdout_access=True).provenance()
    write_experiment(tmp_path, "e1", "v1", _experiment(provenance))
    body = read_experiment_record(
        tmp_path / "epochs" / "e1" / "generations" / "v1" / "experiment.json"
    )
    assert body["operator"]["author"] == "alice"
    assert read_experiment(tmp_path, "e1", "v1").operator == provenance
    # The journal is where an operator reads experiments, so it prints the
    # author's holdout declaration.
    assert "**source**: operator alice; holdout access: yes" in read_journal(tmp_path, "e1")
    assert read_experiment(tmp_path, "e1", "v1").source == "operator"

    write_experiment(tmp_path, "e1", "v2", replace(_experiment(None), generation_id="v2"))
    proposer = read_experiment_record(
        tmp_path / "epochs" / "e1" / "generations" / "v2" / "experiment.json"
    )
    assert "operator" not in proposer
    assert read_experiment(tmp_path, "e1", "v2").source == "proposer"


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


def _submit(*args: str, holdout_access: str = "no") -> Any:
    return CliRunner().invoke(
        submit_cmd, ["--author", "alice", "--holdout-access", holdout_access, *args]
    )


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
    assert queued.holdout_access is False
    assert report["holdout_access"] is False
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
    queue_id = _queue_from_cli(workspace, tmp_path / "cand", "--exclusive")
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
    assert experiment.patches[0].rationale == "Greet with more warmth"
    snapshot = workspace / "epochs" / epoch_id / "generations" / "v1" / "snapshot"
    assert OPERATOR_GREETING in (snapshot / "agent.py").read_text()
    # Settlement took the item off the queue, with its generations.
    assert _pending_queue_ids(workspace) == []
    assert "minted as v1; round 0 settled" in _audit_reasons(workspace)
    assert "**source**: operator alice; holdout access: no" in read_journal(workspace, epoch_id)


def test_a_candidate_based_on_an_older_champion_is_refused_and_never_rebased(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Checked out at v0; a round promoted v1 before the candidate could run.

    The operator edited only ``greet_logic``. Rebuilding their v0 tree as a
    child of v1 would take back v1's gate-approved change to ``greeting``, a
    point they never touched. So ``submit`` refuses the copy, and a round
    that finds such an item already queued consumes it and proposes as usual.
    """
    workspace, epoch_id = _gauntlet_workspace(tmp_path)
    into = tmp_path / "cand"
    _checkout(workspace, into)
    agent = into / "source" / "agent.py"
    agent.write_text(agent.read_text().replace('GREETING + " " + name', 'GREETING + ", " + name'))
    (into / "hypothesis.json").write_text(json.dumps(_hypothesis(modulating=["greet_logic"])))
    queued_early = _submit("--from", str(into))
    assert queued_early.exit_code == 0, queued_early.output
    (queue_id,) = _pending_queue_ids(workspace)
    early = QueuedRound.from_body(
        json.loads((workspace / "runtime" / "control" / "candidate" / queue_id).read_text())
    )
    consume_operator_round(workspace, queue_id, reason="test: held back")

    install_stub_adapter_factory(monkeypatch)
    install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 1.0, "v2": 0.5},
        canned_pass_by_gen={"v0": True, "v1": True, "v2": True},
    )
    first = run_evolve_once(workspace, epoch_id, evaluation_call_llm, round_index=0)
    assert first.tournament_decision == "promoted"
    assert current_generation(workspace, epoch_id) == "v1"
    v1_source = (
        workspace / "epochs" / epoch_id / "generations" / "v1" / "snapshot" / "agent.py"
    ).read_text()

    (finding,) = _refusal(_submit("--from", str(into)))["findings"]
    assert "The champion is now v1" in finding
    assert "zicato proposer checkout" in finding
    assert _pending_queue_ids(workspace) == []

    # The same candidate, queued before the promotion.
    enqueue_operator_round(workspace, early)
    run_evolve_once(workspace, epoch_id, evaluation_call_llm, round_index=1)

    assert _pending_queue_ids(workspace) == []
    assert (
        "operator round edits v0, but the champion is now v1; "
        "run `zicato proposer checkout` again and resubmit"
    ) in _audit_reasons(workspace)
    # The round ran the proposer's candidate, as a child of v1 with v1's edit.
    assert not _is_operator(workspace, epoch_id, "v2")
    assert _proposal_episodes(workspace, epoch_id) == ["v1", "v2"]
    assert v1_source != agent.read_text()


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
        assert next_operator_round(workspace, "e1", champion_id="v0", slots=1, minted=dict) is None
        assert _audit_reasons(workspace) == []
    finally:
        path.chmod(0o644)
    assert _pending_queue_ids(workspace) == ["20261013000000-aaaaaaaa"]


def test_submit_refuses_an_empty_author_in_json(tmp_path: Path) -> None:
    workspace, _epoch_id = bootstrap_workspace(tmp_path)
    into = tmp_path / "cand"
    _checkout(workspace, into)
    _edit_and_fill(into)
    result = CliRunner().invoke(
        submit_cmd, ["--from", str(into), "--author", "  ", "--holdout-access", "no"]
    )
    (finding,) = _refusal(result)["findings"]
    assert "--author" in finding
    assert _pending_queue_ids(workspace) == []


# ---------------------------------------------------------------------------
# The restricted-visibility envelope: no operator text reaches the proposer
# ---------------------------------------------------------------------------

#: Stands for what an operator who read the holdout could write down.
LEAK = "holdout entry h-17 fails on the refund task text"


def _leaking_hypothesis() -> dict[str, Any]:
    return _hypothesis(
        core_idea=f"Greet warmly because {LEAK}",
        why=f"why: {LEAK}",
        risks=f"risks: {LEAK}",
        expected_pass_rate_delta=f"+5 points; {LEAK}",
    )


def test_no_operator_text_reaches_the_proposer_or_the_critic_in_any_round(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """G8: an operator can read the holdout, so none of their free text is shown.

    Round 0 runs an operator candidate beside a proposer candidate (an
    in-flight sibling) and promotes it. Round 1 runs a second operator
    candidate that loses, and round 2 only proposes. Every captured proposer
    input, and the evidence block that the proposer and its critic share with
    every optional channel on, names the operator candidates by source,
    mutation points and decision only.
    """
    from zicato.epoch.execution import load_epoch_execution_contract
    from zicato.evolve.ingest import _load_prior_experiments
    from zicato.evolve.round_context import _build_calibration_summary, _build_candidate_history
    from zicato.proposer.foe_request import ProposalEvidence, render_evidence
    from zicato.proposer.genealogy import OPERATOR_TEXT_WITHHELD
    from zicato.proposer.input_capture import read_proposer_inputs

    workspace, epoch_id = _bootstrap_swiss_workspace(tmp_path, field_size=2, structure="racing")
    install_stub_adapter_factory(monkeypatch)
    install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 0.5, "v2": 1.5, "v3": 1.5, "v4": 1.5, "v5": 1.5},
        canned_pass_by_gen={f"v{n}": True for n in range(7)},
    )
    enqueue_operator_round(
        workspace, _round(epoch_id, _queued_candidate(hypothesis=_leaking_hypothesis()))
    )
    first = run_evolve_once(workspace, epoch_id, evaluation_call_llm, round_index=0)
    assert first.tournament_decision == "promoted"
    assert current_generation(workspace, epoch_id) == "v1"
    assert LEAK in read_experiment(workspace, epoch_id, "v1").hypothesis.why  # the record keeps it

    loser = _queued_candidate(
        based_on="v1", hypothesis=_leaking_hypothesis(), patches=(_greeting_patch('"hi"'),)
    )
    enqueue_operator_round(workspace, _round(epoch_id, loser, queue_id="20261014-second"))
    run_evolve_once(workspace, epoch_id, evaluation_call_llm, round_index=1)
    assert _is_operator(workspace, epoch_id, "v3")
    run_evolve_once(workspace, epoch_id, evaluation_call_llm, round_index=2)

    captured = read_proposer_inputs(workspace, epoch_id)
    by_candidate = {r["new_generation_id"]: json.dumps(r, ensure_ascii=False) for r in captured}
    assert set(by_candidate) >= {"v2", "v4", "v5"}
    raw = (workspace / "epochs" / epoch_id / "proposer_inputs.jsonl").read_text()
    assert "refund" not in raw and "h-17" not in raw
    # The same round: the proposer's slot sees its operator sibling by source alone.
    assert "- v1 IN-FLIGHT  [greeting]  source:operator" in by_candidate["v2"]
    # Later rounds: the settled operator candidates, with a banded outcome.
    assert "- v1 PROMOTED Δscalar=improved  [greeting]  source:operator" in by_candidate["v4"]
    assert "- v3 IN-FLIGHT  [greeting]  source:operator" in by_candidate["v4"]
    assert "- v3 REJECTED Δscalar=regressed  [greeting]" in by_candidate["v5"]

    # Every optional channel on: genealogy, calibration and recombination read
    # the same records, and the proposer and its critic render this one block.
    scoring = load_epoch_execution_contract(workspace, epoch_id).scoring
    weights = replace(
        scoring,
        experimental=replace(scoring.experimental, genealogy=6, calibration_feedback=6),
    )
    pair, genealogy = _build_candidate_history(
        weights=weights,
        workspace_root=workspace,
        epoch_id=epoch_id,
        parent_id="v1",
        train_entry_ids=frozenset(),
        mutations=[],
    )
    assert pair is None
    items = {item.generation_id: item for item in genealogy}
    assert items["v1"].core_idea == OPERATOR_TEXT_WITHHELD
    assert items["v1"].patch_summary.diff_excerpt == ""
    assert items["v1"].patch_summary.mutation_ids == ("greeting",)
    assert "v3" not in items and "v4" in items
    evidence = render_evidence(
        ProposalEvidence(
            prior_experiments=tuple(_load_prior_experiments(workspace, epoch_id, cross_epoch=True)),
            genealogy=genealogy,
            calibration=_build_calibration_summary(
                weights=weights, workspace_root=workspace, epoch_id=epoch_id
            ),
            restrict_visibility=True,
        )
    )
    assert "refund" not in evidence and "h-17" not in evidence
    assert OPERATOR_TEXT_WITHHELD in evidence
    assert "source:operator" in evidence


def test_the_calibration_record_and_the_recombination_pool_leave_operator_candidates_out(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Both channels copy hypothesis text or patches into what the proposer reads."""
    from zicato.core.types import OutcomeRecord, Patch, TournamentDecision
    from zicato.evolve import round_context
    from zicato.proposer.prompts import render_recombine_merge_prompt

    def rejected(generation_id: str, point: str, operator: OperatorProvenance | None) -> Experiment:
        return replace(
            _experiment(operator),
            id=f"exp_e1_{generation_id}",
            generation_id=generation_id,
            hypothesis=HypothesisSpec(
                core_idea=f"{generation_id}: {LEAK}",
                modulating=(point,),
                why=LEAK,
                expected_pass_rate_delta="+1",
            ),
            patches=(
                Patch(
                    id=f"p-{generation_id}",
                    mutation_id=point,
                    op="replace",
                    new_content=LEAK,
                    new_numeric=None,
                    new_enum=None,
                    rationale=LEAK,
                ),
            ),
            outcome=OutcomeRecord(
                ran_at="2026-10-13T00:00:00Z",
                pass_rate_delta=0.0,
                drift_loss_delta=1.0,
                scalar_score_delta=1.0,
                tournament_decision=TournamentDecision.REJECTED,
            ),
        )

    passes = {
        "v0": {"a": {"pass_fail": False}, "b": {"pass_fail": False}},
        "v1": {"a": {"pass_fail": True}, "b": {"pass_fail": False}},
        "v2": {"a": {"pass_fail": False}, "b": {"pass_fail": True}},
    }
    monkeypatch.setattr(
        "zicato.workspace.reads.read_generation_losses",
        lambda _layout, _epoch, generation_id: passes[generation_id],
    )
    scoring = deterministic_weights()
    weights = replace(
        scoring,
        experimental=replace(scoring.experimental, recombine=True, calibration_feedback=4),
        proposer_quality=replace(scoring.proposer_quality, best_of_n=2),
    )

    def pair_for(operator: OperatorProvenance | None) -> Any:
        experiments = [
            ("v1", rejected("v1", "greeting", operator)),
            ("v2", rejected("v2", "greet_logic", None)),
        ]
        return round_context._build_recombination_pair(
            weights=weights,
            workspace_root=tmp_path,
            epoch_id="e1",
            parent_id="v0",
            train_entry_ids=frozenset({"a", "b"}),
            mutations=[type("Point", (), {"id": point})() for point in ("greeting", "greet_logic")],
            experiments=experiments,
            elo_by_gid={},
        )

    # Two proposer candidates recombine, and the merge prompt quotes both.
    proposer_pair = pair_for(None)
    assert proposer_pair is not None
    assert LEAK in render_recombine_merge_prompt(proposer_pair, brief_text="")[1]
    # An operator's candidate is never a parent.
    assert pair_for(_round("e1").provenance()) is None

    # The calibration record grades the proposer's own predictions.
    claims: list[Any] = []
    for generation_id, operator in (("v1", _round("e1").provenance()), ("v2", None)):
        write_experiment(
            tmp_path, "e1", generation_id, rejected(generation_id, "greeting", operator)
        )
    monkeypatch.setattr(
        "zicato.proposer.calibration.sample_calibration",
        lambda found, _k: claims.extend(found),
    )
    round_context._build_calibration_summary(
        weights=weights, workspace_root=tmp_path, epoch_id="e1"
    )
    assert [claim.generation_id for claim in claims] == ["v2"]


def test_an_operator_hypothesis_cannot_open_with_a_marker_zicato_reserves(tmp_path: Path) -> None:
    """Health and the frontier read the placebo prefix as the arm's identity."""
    workspace, _epoch_id = bootstrap_workspace(tmp_path)
    into = tmp_path / "cand"
    _checkout(workspace, into)
    _edit_and_fill(into, core_idea="[placebo:random-baseline] not a placebo")
    (candidate,) = _refusal(_submit("--from", str(into)))["candidates"]
    (finding,) = candidate["findings"]
    assert "reserves for its own candidates" in finding


def test_the_index_and_the_memory_name_the_operator(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A settled operator round reaches the index with its source and author."""
    import sqlite3

    from zicato.index.query import prior_experiments_for_epoch

    workspace, epoch_id = _bootstrap_swiss_workspace(tmp_path, field_size=2)
    enqueue_operator_round(workspace, _round(epoch_id))
    install_stub_adapter_factory(monkeypatch)
    install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 0.5, "v2": 1.5},
        canned_pass_by_gen={"v0": True, "v1": True, "v2": True},
    )
    run_evolve_once(workspace, epoch_id, evaluation_call_llm)

    memory = {
        pe.generation_id: (pe.source, pe.core_idea, pe.prediction_accuracy)
        for pe in prior_experiments_for_epoch(workspace / "index.db", epoch_id)
    }
    assert memory["v1"] == ("operator", "", None)
    assert memory["v2"][0] == "proposer" and memory["v2"][1]
    with sqlite3.connect(workspace / "index.db") as conn:
        rows = {
            generation_id: (source, author)
            for generation_id, source, author in conn.execute(
                "SELECT generation_id, source, author FROM experiments"
            )
        }
    assert rows == {"v0": (None, None), "v1": ("operator", "alice"), "v2": ("proposer", None)}


# ---------------------------------------------------------------------------
# The gate: holdout confirmation, the query budget, and crash recovery
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("holdout_scalar", [0.5, 5.0], ids=["confirmed", "refuted"])
def test_an_operator_candidate_is_confirmed_on_the_holdout_and_charged_like_a_proposers(
    holdout_scalar: float, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The same round twice: v1 comes once from the proposer and once from an operator.

    Both win the training duel. The holdout then confirms or refutes the
    win, and either way spends one of the epoch's two holdout queries. The
    holdout record, the decision and the remaining budget are identical for
    the two sources.
    """
    from tests.test_orchestrator_multi_challenger_holdout import (
        _bootstrap,
        _crowned_outcome,
        _install_per_entry_telemetry_stubs,
    )
    from zicato.core.types import LadderConfig, OverfittingConfig
    from zicato.core.workspace import ladder_state_path

    results: dict[str, tuple[Any, ...]] = {}
    for source in ("proposer", "operator"):
        (tmp_path / source).mkdir()
        workspace, epoch_id = _bootstrap(
            tmp_path / source,
            structure="racing",
            field_size=2,
            overfitting=OverfittingConfig(ladder=LadderConfig(budget=2, threshold=0.1)),
        )
        if source == "operator":
            enqueue_operator_round(workspace, _round(epoch_id, holdout_access=True))
        install_stub_adapter_factory(monkeypatch)
        losses = {
            (generation_id, entry): scalar
            for generation_id, scalar in (("v0", 2.0), ("v1", 0.5), ("v2", 1.5))
            for entry in (*(f"train_{i}" for i in range(4)), "h0")
        }
        losses[("v1", "h0")] = holdout_scalar
        _install_per_entry_telemetry_stubs(
            monkeypatch,
            loss_by_gen_entry=losses,
            pass_by_gen={"v0": True, "v1": True, "v2": True},
        )

        outcome = run_evolve_once(workspace, epoch_id, evaluation_call_llm)

        assert _is_operator(workspace, epoch_id, "v1") is (source == "operator")
        block = _crowned_outcome(workspace, epoch_id, "v1")["holdout"]
        assert isinstance(block, dict)
        assert block["holdout_consulted"] is True
        assert block["ladder_budget_before_query"] == 2
        assert block["ladder_budget_remaining"] == 1
        state = json.loads(ladder_state_path(workspace, epoch_id).read_text())
        results[source] = (
            str(outcome.tournament_decision),
            current_generation(workspace, epoch_id),
            block,
            state["budget_remaining"],
        )

    expected = ("promoted", "v1") if holdout_scalar < 2.0 else ("rejected", "v0")
    assert results["operator"][:2] == expected
    assert results["operator"][3] == 1
    assert results["operator"] == results["proposer"]


def test_a_field_that_crash_recovery_discards_runs_its_queued_item_again(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The process stops before the settlement receipt; the item is still queued.

    Recovery discards the whole field, so no experiment carries the queue id
    any more. The next round takes the item again and mints the same
    candidate, and its settlement takes the item off the queue once.
    """
    from zicato.evolve import settlement as settlement_module
    from zicato.runtime.resume import prepare_resume

    workspace, epoch_id = _bootstrap_swiss_workspace(tmp_path, field_size=2, structure="racing")
    enqueue_operator_round(workspace, _round(epoch_id))
    install_stub_adapter_factory(monkeypatch)
    install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 0.5, "v2": 1.5},
        canned_pass_by_gen={"v0": True, "v1": True, "v2": True},
    )

    class _Crash(RuntimeError):
        pass

    def stop_before_receipt(_root: Path, _intent: dict[str, Any]) -> None:
        raise _Crash("before receipt")

    with monkeypatch.context() as patched:
        patched.setattr(settlement_module, "commit_field_settlement", stop_before_receipt)
        with pytest.raises(_Crash):
            run_evolve_once(workspace, epoch_id, evaluation_call_llm)
    assert _is_operator(workspace, epoch_id, "v1")
    assert _pending_queue_ids(workspace) == ["20261013000000-aaaaaaaa"]
    assert _audit_reasons(workspace) == []

    plan = prepare_resume(workspace, epoch_id)
    assert plan.classification == "discard_unrecorded_field"
    assert not (workspace / "epochs" / epoch_id / "generations" / "v1").exists()
    assert _pending_queue_ids(workspace) == ["20261013000000-aaaaaaaa"]

    outcome = run_evolve_once(workspace, epoch_id, evaluation_call_llm)

    assert outcome.tournament_decision == "promoted"
    assert current_generation(workspace, epoch_id) == "v1"
    experiment = read_experiment(workspace, epoch_id, "v1")
    assert experiment.operator is not None
    assert experiment.operator.queue_id == "20261013000000-aaaaaaaa"
    assert _pending_queue_ids(workspace) == []
    assert _audit_reasons(workspace) == ["minted as v1; round 0 settled"]
