"""Proposal channels share accepted history without exposing withheld task results."""

from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from zicato.core.types import (
    Experiment,
    ExperimentalConfig,
    HypothesisSpec,
    OutcomeRecord,
    Patch,
    ProposerQualityConfig,
    ScoringWeights,
)
from zicato.epoch import journal
from zicato.evolve.round_context import _build_candidate_history
from zicato.index import query
from zicato.query import tournament_view
from zicato.workspace import reads


@pytest.fixture
def history(tmp_path: Path):
    from tests.test_matchup_grid_signal import EPOCH, _write_loss

    for number in range(3):
        gid = f"v{number}"
        patch = Patch(
            id=f"patch{number}",
            mutation_id=f"m{number}",
            op="replace",
            new_content=f"edit {number}",
            new_numeric=None,
            new_enum=None,
            rationale="improve",
        )
        experiment = Experiment(
            id=f"experiment{number}",
            epoch_id=EPOCH,
            generation_id=gid,
            parent_generation_id="v0" if number else None,
            proposed_at="2026-01-01T00:00:00Z",
            round_index=number,
            hypothesis=HypothesisSpec(
                core_idea=f"improve capability {number}",
                modulating=(f"m{number}",),
                why="correct a task",
                expected_pass_rate_delta="positive",
            ),
            patches=(patch,),
            outcome=OutcomeRecord(
                ran_at="2026-01-01T00:00:00Z",
                pass_rate_delta=0.1,
                drift_loss_delta=-0.5,
                scalar_score_delta=-0.5,
                tournament_decision="rejected" if number else "promoted",
            ),
        )
        journal.write_experiment(tmp_path, EPOCH, gid, experiment)
        for entry in ("retrieval", "formatting", "withheld"):
            passes = (number == 1 and entry == "retrieval") or (
                number == 2 and entry in {"formatting", "withheld"}
            )
            _write_loss(tmp_path, gid, entry, passes=passes)
    # A malformed sibling must not hide the accepted candidates.
    bad = tmp_path / "epochs" / EPOCH / "generations" / "v3" / "experiment.json"
    bad.parent.mkdir(parents=True)
    bad.write_text("{}")
    weights = ScoringWeights(
        experimental=ExperimentalConfig(recombine=True, genealogy=3),
        proposer_quality=ProposerQualityConfig(best_of_n=3),
    )
    return dict(
        weights=weights,
        workspace_root=tmp_path,
        epoch_id=EPOCH,
        parent_id="v0",
        train_entry_ids=frozenset({"retrieval", "formatting"}),
        mutations=[SimpleNamespace(id="m1"), SimpleNamespace(id="m2")],
    )


def test_candidate_channels_share_reads_and_preserve_training_contributions(history, monkeypatch):
    walk = Mock(wraps=journal.read_epoch_experiments)
    rankings = Mock(return_value=[])
    losses = Mock(wraps=reads.read_generation_losses)
    monkeypatch.setattr(journal, "read_epoch_experiments", walk)
    monkeypatch.setattr(query, "elo_for_epoch", rankings)
    monkeypatch.setattr(reads, "read_generation_losses", losses)
    monkeypatch.setattr(tournament_view, "build_matchup_grid", Mock(side_effect=AssertionError))

    pair, items = _build_candidate_history(**history)

    assert (pair.a_generation_id, pair.b_generation_id) == ("v1", "v2")
    assert pair.combined_improved_count == 2
    assert pair.combined_regressed_count == 0
    assert {item.generation_id for item in items} == {"v0", "v1", "v2"}
    assert all(entry not in repr(asdict(pair)) for entry in history["train_entry_ids"])
    assert "withheld" not in repr(asdict(pair)) + repr(items)
    assert walk.call_count == rankings.call_count == 1
    assert [call.args[2] for call in losses.call_args_list] == ["v0", "v2", "v1"]


def test_disabled_candidate_channels_read_nothing(history, monkeypatch):
    monkeypatch.setattr(journal, "read_epoch_experiments", Mock(side_effect=AssertionError))
    history["weights"] = replace(history["weights"], experimental=ExperimentalConfig())
    assert _build_candidate_history(**history) == (None, ())


def test_genealogy_without_combination_does_not_read_task_results(history, monkeypatch):
    monkeypatch.setattr(reads, "read_generation_losses", Mock(side_effect=AssertionError))
    history["weights"] = replace(history["weights"], experimental=ExperimentalConfig(genealogy=3))
    pair, items = _build_candidate_history(**history)
    assert pair is None
    assert {item.generation_id for item in items} == {"v0", "v1", "v2"}


def test_unavailable_rankings_preserve_candidate_history(history, monkeypatch):
    expected = _build_candidate_history(**history)
    monkeypatch.setattr(query, "elo_for_epoch", Mock(side_effect=OSError("unavailable")))
    assert _build_candidate_history(**history) == expected


def test_combination_read_failure_preserves_genealogy(history, monkeypatch):
    expected_items = _build_candidate_history(**history)[1]
    monkeypatch.setattr(reads, "read_generation_losses", Mock(side_effect=OSError("unavailable")))
    pair, items = _build_candidate_history(**history)
    assert pair is None
    assert items == expected_items
