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
