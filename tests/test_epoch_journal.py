"""Tests for :mod:`zicato.epoch.journal`."""

from __future__ import annotations

from pathlib import Path

import pytest

from zicato.core.types import (
    DriftMovementActual,
    Experiment,
    ExpectedDriftMovement,
    HypothesisSpec,
    OutcomeRecord,
    Patch,
)
from zicato.core.workspace import journal_path
from zicato.epoch import append_journal_entry, read_journal


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _experiment(
    *,
    generation_id: str = "v1",
    core_idea: str = "Tighten researcher's prompt to require citations.",
    modulating: tuple[str, ...] = ("researcher.instruction", "researcher.description"),
    why: str = (
        "Pattern observed across rounds 3-5: confabulation fires on 70% of "
        "research-tagged entries. The current instruction does not require "
        "source citations."
    ),
    outcome: OutcomeRecord | None = None,
) -> Experiment:
    hypothesis = HypothesisSpec(
        core_idea=core_idea,
        modulating=modulating,
        why=why,
        expected_drift_movements=(
            ExpectedDriftMovement(
                kind="off_topic",
                direction="decrease",
                magnitude="medium",
            ),
        ),
        expected_pass_rate_delta="+0.0 to +0.15",
        risks="May over-cite when sources are unavailable.",
    )
    patches = (
        Patch(
            id="p1",
            mutation_id="researcher.instruction",
            op="replace",
            new_content="new instruction",
            new_numeric=None,
            new_enum=None,
            rationale="cite sources",
        ),
    )
    return Experiment(
        id=f"exp_test_{generation_id}",
        epoch_id="2026-04-08_test",
        generation_id=generation_id,
        parent_generation_id="v0",
        proposed_at="2026-04-08T12:00:00+00:00",
        hypothesis=hypothesis,
        patches=patches,
        outcome=outcome,
    )


def _outcome(
    decision: str = "promoted",
    rejection_reason: str = "",
) -> OutcomeRecord:
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
        tournament_decision=decision,  # type: ignore[arg-type]
        rejection_reason=rejection_reason,
    )


@pytest.fixture()
def epoch_root(tmp_path: Path) -> tuple[Path, str]:
    ws = tmp_path / ".zicato"
    epoch_id = "2026-04-08_test"
    (ws / "epochs" / epoch_id).mkdir(parents=True)
    return ws, epoch_id


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_append_journal_entry_creates_file(epoch_root: tuple[Path, str]) -> None:
    ws, eid = epoch_root
    exp = _experiment(outcome=_outcome())
    append_journal_entry(ws, eid, exp)
    path = journal_path(ws, eid)
    assert path.exists()
    text = path.read_text()
    assert "## v1 — Tighten researcher's prompt to require citations." in text
    assert "**proposed_at**: 2026-04-08T12:00:00+00:00" in text
    assert "**modulating**: researcher.instruction, researcher.description" in text
    assert "**why**: Pattern observed across rounds 3-5" in text
    assert "**outcome**: promoted" in text
    assert "Δscalar=-0.200" in text
    assert "Δpass_rate=+0.050" in text


def test_append_journal_entry_without_outcome(epoch_root: tuple[Path, str]) -> None:
    ws, eid = epoch_root
    exp = _experiment(outcome=None)
    append_journal_entry(ws, eid, exp)
    text = read_journal(ws, eid)
    assert "## v1 —" in text
    assert "**modulating**:" in text
    assert "**outcome**:" not in text
    assert "**rejection_reason**:" not in text


def test_append_journal_entry_rejected_includes_reason(
    epoch_root: tuple[Path, str],
) -> None:
    ws, eid = epoch_root
    exp = _experiment(
        outcome=_outcome(
            decision="rejected",
            rejection_reason="pass_rate_regression_on_summarise_short",
        )
    )
    append_journal_entry(ws, eid, exp)
    text = read_journal(ws, eid)
    assert "**outcome**: rejected" in text
    assert (
        "**rejection_reason**: pass_rate_regression_on_summarise_short" in text
    )


def test_append_journal_entry_appends_multiple_sections(
    epoch_root: tuple[Path, str],
) -> None:
    ws, eid = epoch_root
    a = _experiment(generation_id="v1", outcome=_outcome())
    b = _experiment(
        generation_id="v2",
        core_idea="Reduce coordinator re-routing.",
        outcome=_outcome(decision="rejected", rejection_reason="regression"),
    )
    append_journal_entry(ws, eid, a)
    append_journal_entry(ws, eid, b)
    text = read_journal(ws, eid)
    assert text.count("## v1 —") == 1
    assert text.count("## v2 —") == 1
    # v1 appears before v2.
    assert text.index("## v1 —") < text.index("## v2 —")


def test_append_journal_entry_first_sentence_of_why(
    epoch_root: tuple[Path, str],
) -> None:
    ws, eid = epoch_root
    exp = _experiment(
        why="First sentence. Second sentence with more detail.",
        outcome=None,
    )
    append_journal_entry(ws, eid, exp)
    text = read_journal(ws, eid)
    assert "**why**: First sentence" in text
    assert "Second sentence" not in text


def test_append_journal_entry_missing_epoch_dir(tmp_path: Path) -> None:
    ws = tmp_path / ".zicato"
    with pytest.raises(FileNotFoundError):
        append_journal_entry(ws, "missing", _experiment())


def test_read_journal_returns_empty_when_missing(tmp_path: Path) -> None:
    assert read_journal(tmp_path / ".zicato", "missing") == ""


def test_append_journal_handles_empty_modulating(
    epoch_root: tuple[Path, str],
) -> None:
    ws, eid = epoch_root
    exp = _experiment(modulating=(), outcome=None)
    append_journal_entry(ws, eid, exp)
    text = read_journal(ws, eid)
    assert "**modulating**: (none)" in text
