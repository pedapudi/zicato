"""Pins for the hydration layer's declared-vs-runtime type honesty (#132).

``OutcomeRecord.tournament_decision`` is declared :class:`TournamentDecision`
(a :class:`~enum.StrEnum`). Every in-process construction site passes a real
member; the two read paths that rebuild the record from an untyped mapping
historically passed the raw wire token straight through, so a record loaded
from disk differed in runtime type from an identical one built in memory --
invisible to ``==`` / ``str()`` / ``json.dumps`` but fatal to ``isinstance``,
``match`` on members, and ``.name``.

These tests pin three things per read path:

* the hydrated field IS a :class:`TournamentDecision` member,
* a hydrated record compares equal to the in-process construction, and
* the pre-existing handling of an UNRECOGNISED token is unchanged (the two
  paths differ here, deliberately: ``analysis`` drops the whole outcome,
  ``journal`` keeps whatever the token was).
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from zicato.core.board import ExpectationKind
from zicato.core.loss import ExpectationResult, LossProfile
from zicato.core.tournament import TournamentDecision
from zicato.core.types import (
    DriftMovementActual,
    Experiment,
    HypothesisSpec,
    OutcomeRecord,
)
from zicato.core.workspace import experiment_json_path
from zicato.epoch.analysis import _hydrate_experiment, _hydrate_outcome
from zicato.epoch.journal import _outcome_from_dict, read_experiment, write_experiment
from zicato.telemetry.reducer import (
    _profile_to_dict,
    loss_profile_from_dict,
    read_loss_profile,
    write_loss_profile,
)

ALL_TOKENS = ("promoted", "rejected", "deferred")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _record(decision: TournamentDecision = TournamentDecision.PROMOTED) -> OutcomeRecord:
    return OutcomeRecord(
        ran_at="2026-04-08T12:30:00+00:00",
        drift_movements=(
            DriftMovementActual(
                kind="off_topic",
                from_rate=0.7,
                to_rate=0.2,
                hypothesis_match=True,
            ),
        ),
        pass_rate_delta=0.05,
        drift_loss_delta=-0.18,
        scalar_score_delta=-0.20,
        tournament_decision=decision,
        rejection_reason="",
    )


def _wire(record: OutcomeRecord) -> dict[str, Any]:
    """The record as it lands on disk (asdict + a json round trip)."""
    return json.loads(json.dumps(asdict(record)))  # type: ignore[no-any-return]


def _experiment(outcome: OutcomeRecord | None) -> Experiment:
    return Experiment(
        id="exp_test_v1",
        epoch_id="2026-04-08_test",
        generation_id="v1",
        parent_generation_id="v0",
        proposed_at="2026-04-08T12:00:00+00:00",
        hypothesis=HypothesisSpec(
            core_idea="Tighten the researcher prompt.",
            modulating=("researcher.instruction",),
            why="Confabulation fires on research-tagged entries.",
            expected_drift_movements=(),
            expected_pass_rate_delta="+0.0 to +0.15",
        ),
        patches=(),
        outcome=outcome,
    )


@pytest.fixture()
def epoch_root(tmp_path: Path) -> tuple[Path, str]:
    ws = tmp_path / ".zicato"
    epoch_id = "2026-04-08_test"
    (ws / "epochs" / epoch_id).mkdir(parents=True)
    return ws, epoch_id


# ---------------------------------------------------------------------------
# analysis._hydrate_outcome
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("token", ALL_TOKENS)
def test_analysis_hydrate_outcome_yields_enum_member(token: str) -> None:
    hydrated = _hydrate_outcome(_wire(_record(TournamentDecision(token))))
    assert hydrated is not None
    assert isinstance(hydrated.tournament_decision, TournamentDecision)
    # The StrEnum still equals its wire token -- coercion changes the runtime
    # type, never the comparison every consumer already relies on.
    assert hydrated.tournament_decision == token


@pytest.mark.parametrize("token", ALL_TOKENS)
def test_analysis_hydrated_outcome_equals_in_process(token: str) -> None:
    in_process = _record(TournamentDecision(token))
    hydrated = _hydrate_outcome(_wire(in_process))
    assert hydrated == in_process
    assert hydrated is not None
    assert hydrated.tournament_decision is in_process.tournament_decision


@pytest.mark.parametrize(
    "payload",
    [
        {"tournament_decision": "bogus"},
        {"tournament_decision": "deferred_infra"},
        {"tournament_decision": ""},
        {"tournament_decision": None},
        {},
    ],
    ids=["bogus", "deferred_infra", "empty", "null", "absent"],
)
def test_analysis_hydrate_outcome_unrecognised_token_still_drops_outcome(
    payload: dict[str, Any],
) -> None:
    """The narrowing guard's behaviour is unchanged by the coercion."""
    assert _hydrate_outcome(payload) is None


def test_analysis_hydrate_outcome_none_input() -> None:
    assert _hydrate_outcome(None) is None


def test_analysis_hydrate_experiment_disk_round_trip(epoch_root: tuple[Path, str]) -> None:
    """The full on-disk path: written by the journal, read by the analyser."""
    ws, eid = epoch_root
    in_process = _record(TournamentDecision.DEFERRED)
    write_experiment(ws, eid, "v1", _experiment(in_process))

    body = json.loads(experiment_json_path(ws, eid, "v1").read_text())
    hydrated = _hydrate_experiment(body)

    assert hydrated is not None
    assert hydrated.outcome is not None
    assert isinstance(hydrated.outcome.tournament_decision, TournamentDecision)
    assert hydrated.outcome.tournament_decision == in_process.tournament_decision


# ---------------------------------------------------------------------------
# journal._outcome_from_dict (the primary read path)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("token", ALL_TOKENS)
def test_journal_read_experiment_yields_enum_member(
    epoch_root: tuple[Path, str], token: str
) -> None:
    ws, eid = epoch_root
    in_process = _record(TournamentDecision(token))
    write_experiment(ws, eid, "v1", _experiment(in_process))

    loaded = read_experiment(ws, eid, "v1").outcome
    assert loaded is not None
    assert isinstance(loaded.tournament_decision, TournamentDecision)
    assert loaded.tournament_decision == token
    assert loaded == in_process


def test_journal_outcome_from_dict_absent_decision_defaults_to_rejected() -> None:
    """The historical ``"rejected"`` default now arrives as the member."""
    hydrated = _outcome_from_dict({})
    assert hydrated is not None
    assert hydrated.tournament_decision is TournamentDecision.REJECTED


@pytest.mark.parametrize("token", ["bogus", "deferred_infra", ""])
def test_journal_outcome_from_dict_keeps_unrecognised_token(token: str) -> None:
    """Unrecognised tokens are preserved verbatim, exactly as before.

    This path has no narrowing guard, so coercing unconditionally would
    raise on a hand-edited or future-format record that reads fine today.
    The token is kept as-is (and still compares unequal to every member),
    rather than inventing a verdict the record does not carry.
    """
    hydrated = _outcome_from_dict({"tournament_decision": token})
    assert hydrated is not None
    assert hydrated.tournament_decision == token
    assert not isinstance(hydrated.tournament_decision, TournamentDecision)
    assert hydrated.tournament_decision != TournamentDecision.PROMOTED
    assert hydrated.tournament_decision != TournamentDecision.REJECTED
    assert hydrated.tournament_decision != TournamentDecision.DEFERRED


@pytest.mark.parametrize(
    "value",
    [None, 3, 1.5, ["promoted"], {"a": 1}, b"promoted"],
    ids=["null", "int", "float", "list", "dict", "bytes"],
)
def test_journal_outcome_from_dict_non_string_decision_does_not_raise(value: Any) -> None:
    """A structurally wrong value reads exactly as it did before the coercion.

    The unhashable cases are the ones worth pinning: the enum lookup must
    surface them as ``ValueError`` (which the fallback catches) and never as
    an uncaught ``TypeError`` out of a read path that previously could not
    fail at all.
    """
    hydrated = _outcome_from_dict({"tournament_decision": value})
    assert hydrated is not None
    assert hydrated.tournament_decision == value


# ---------------------------------------------------------------------------
# telemetry.reducer.loss_profile_from_dict
# ---------------------------------------------------------------------------


def _loss_profile(kind: ExpectationKind) -> LossProfile:
    return LossProfile(
        run_id="run-1",
        entry_id="e1",
        generation_id="v1",
        epoch_id="2026-04-08_test",
        drift_counts=(),
        plan_revisions=0,
        task_failure_ratio=0.0,
        runtime_ms=1200,
        wall_clock_budget_exceeded=False,
        expectation_result=ExpectationResult(kind=kind, passed=True, detail="ok"),
        drift_loss=0.25,
        pass_fail=True,
    )


@pytest.mark.parametrize("kind", list(ExpectationKind))
def test_loss_profile_round_trip_yields_expectation_kind_member(
    tmp_path: Path, kind: ExpectationKind
) -> None:
    """The same defect one layer over: ``ExpectationResult.kind`` off disk.

    ``board/matchers.py`` already re-coerces this field defensively before
    dispatching on it, which is the tell that the raw token was reaching
    consumers.
    """
    in_process = _loss_profile(kind)
    path = tmp_path / "loss.json"
    write_loss_profile(in_process, path)

    loaded = read_loss_profile(path)
    assert loaded.expectation_result is not None
    assert isinstance(loaded.expectation_result.kind, ExpectationKind)
    assert loaded.expectation_result.kind is kind
    assert loaded == in_process


def test_loss_profile_from_dict_unknown_kind_raises_valueerror() -> None:
    """Every caller of this reader already catches ``ValueError``.

    Pinned as ValueError specifically: the callers' degrade paths (skip the
    file / treat the slot as a predecessor) name that exception, so a token
    that fails to coerce must not surface as anything else.
    """
    payload = _profile_to_dict(_loss_profile(ExpectationKind.PREDICATE))
    payload["expectation_result"]["kind"] = "not_a_kind"
    with pytest.raises(ValueError):
        loss_profile_from_dict(payload)


def test_an_operator_override_round_records_the_enum_not_the_wire_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The WRITE side of the same defect, on the gauntlet's override path.

    ``GateOverride.decision`` is a control-protocol wire token (``str``), and
    ``evolve_once`` assigns it straight onto the outcome the round persists.
    Every other gauntlet round builds ``tournament_decision`` from the
    strategy's enum, so an operator force-promote/force-reject was the one
    round whose LIVE record carried a bare str -- while the same record read
    back through the journal hydrator carried the member, inverting the
    invariant the rest of this file pins.

    Driven through the real round rather than the helper, because the helper
    is not where the token enters the record.
    """
    import zicato.orchestrator as orch
    import zicato.runtime.control_consumer as cc
    from tests.test_pareto_frontier import _drive_round

    recorded: list[OutcomeRecord] = []
    real_outcome_record = orch.OutcomeRecord

    def _spy(*args: Any, **kwargs: Any) -> OutcomeRecord:
        record = real_outcome_record(*args, **kwargs)
        recorded.append(record)
        return record

    def _force_promote(workspace_root: Path, generation_id: str) -> cc.GateOverride:
        return cc.GateOverride(
            decision="promoted", generation_id=generation_id, reason="operator says so"
        )

    monkeypatch.setattr(orch, "OutcomeRecord", _spy)
    monkeypatch.setattr(orch, "claim_gate_override", _force_promote)

    _workspace, _epoch_id, outcome = _drive_round(
        monkeypatch,
        tmp_path,
        drift_by_gen={"v0": 1.0, "v1": 3.0},
        tokens_by_gen={"v0": 1000, "v1": 500},
    )
    # The override really did flip the verdict (the round rejects on merit).
    assert outcome.tournament_decision == "promoted"
    assert recorded, "the round persisted no OutcomeRecord"
    decision = recorded[0].tournament_decision
    assert isinstance(decision, TournamentDecision)
    assert decision is TournamentDecision.PROMOTED
