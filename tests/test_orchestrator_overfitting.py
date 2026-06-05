"""The proposer never sees the holdout slice (OVERFITTING.md §11 / §12 #1).

The orchestrator restricts the detector patterns AND the loss summary to
the TRAIN slice before assembling the proposer prompt, so the holdout's
per-entry behaviour is never surfaced. These tests reconstruct that exact
restriction path — ``split_board`` → ``_load_parent_losses`` over the
train board → detectors + ``_render_loss_summary`` → ``render_user_prompt``
— and assert the holdout entry's identity never reaches the prompt, while
a train entry's pattern is still surfaced (aggregated, under the default-on
restriction).

When the board is too small to split, the same path degrades to the full
board, byte-identically to before the split existed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from zicato.board.split import HOLDOUT_TAG, split_board
from zicato.core import BoardEntry, DriftCount, ExpectationResult, LossProfile, ScoringWeights
from zicato.core.types import OverfittingConfig
from zicato.core.workspace import loss_profile_path
from zicato.orchestrator import _load_parent_losses, _render_loss_summary
from zicato.patterns import ALL_DETECTORS, DetectorInput, detect_patterns
from zicato.proposer.prompts import render_user_prompt
from zicato.telemetry.reducer import read_loss_profile, write_loss_profile

_EPOCH = "e0"
_PARENT = "v0"


def _loss(entry_id: str, *, drift_count: int) -> LossProfile:
    return LossProfile(
        run_id=f"run-{entry_id}",
        entry_id=entry_id,
        generation_id=_PARENT,
        epoch_id=_EPOCH,
        drift_counts=(DriftCount(kind="off_topic", severity="warning", count=drift_count),),
        plan_revisions=0,
        task_failure_ratio=0.0,
        runtime_ms=1000,
        wall_clock_budget_exceeded=False,
        expectation_result=ExpectationResult(kind="predicate", passed=True),
        drift_loss=float(drift_count),
        pass_fail=True,
    )


def _board() -> list[BoardEntry]:
    # Four train entries that all fire off_topic drift + one tagged holdout
    # entry that ALSO fires it. Under the split the holdout entry must not
    # reach the proposer's view at all.
    train = [
        BoardEntry(id=f"train_{i}", kind="single_turn", wall_clock_budget_seconds=60, input="x")
        for i in range(4)
    ]
    holdout = BoardEntry(
        id="SECRET_HOLDOUT_ENTRY",
        kind="single_turn",
        wall_clock_budget_seconds=60,
        tags=(HOLDOUT_TAG,),
        input="x",
    )
    return [*train, holdout]


def _write_losses(tmp_path: Path, board: list[BoardEntry]) -> None:
    for e in board:
        path = loss_profile_path(tmp_path, _EPOCH, _PARENT, e.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        write_loss_profile(_loss(e.id, drift_count=3), path)


def _assemble_prompt(tmp_path: Path, board: list[BoardEntry], weights: ScoringWeights) -> str:
    """Reproduce the orchestrator's train-restricted proposer context."""
    train_ids, _holdout = split_board(board, weights.overfitting)
    train_id_set = set(train_ids)
    train_board = [e for e in board if e.id in train_id_set]
    losses = _load_parent_losses(tmp_path, _EPOCH, _PARENT, train_board, read_loss_profile)
    detector_input = DetectorInput(
        losses=losses,
        entries={e.id: e for e in train_board},
        events_paths={},
    )
    patterns = detect_patterns(detector_input, detectors=ALL_DETECTORS)
    loss_summary = _render_loss_summary(losses)
    return render_user_prompt(
        current_loss_summary=loss_summary,
        patterns=patterns,
        mutations=(),
        restrict_visibility=weights.overfitting.restrict_proposer_visibility,
    )


def test_holdout_entry_never_appears_in_proposer_prompt(tmp_path: Path) -> None:
    board = _board()
    _write_losses(tmp_path, board)
    weights = ScoringWeights()  # default-on overfitting config

    prompt = _assemble_prompt(tmp_path, board, weights)

    # The holdout entry's identity is absent from every surface.
    assert "SECRET_HOLDOUT_ENTRY" not in prompt
    # The train slice is still surfaced: the off_topic drift pattern fired
    # (aggregated under the default-on restriction — a count, not ids).
    assert "off_topic" in prompt
    assert "train_" not in prompt  # restriction aggregated the named ids away too
    # The loss summary counts only the 4 train runs, never the 5th holdout.
    assert "over 4 runs" in prompt


def test_small_board_degrades_to_the_full_board(tmp_path: Path) -> None:
    # A board with NO holdout tag, below the split floor: the hash-derived
    # split degrades to an empty holdout, so every entry is train and the
    # rendering is verbatim — byte-identical to before this phase.
    board = [
        BoardEntry(id="ENTRY_X", kind="single_turn", wall_clock_budget_seconds=60, input="x"),
        BoardEntry(id="ENTRY_Y", kind="single_turn", wall_clock_budget_seconds=60, input="x"),
    ]
    _write_losses(tmp_path, board)
    weights = ScoringWeights(
        overfitting=OverfittingConfig(
            min_board_size_for_split=8, restrict_proposer_visibility=False
        )
    )

    prompt = _assemble_prompt(tmp_path, board, weights)

    # Degraded split + unrestricted rendering ⇒ the full board reaches the
    # proposer verbatim (named entry ids included), exactly as before.
    assert "ENTRY_X" in prompt
    assert "ENTRY_Y" in prompt
    assert "over 2 runs" in prompt


# ---------------------------------------------------------------------------
# Per-generation train/holdout loss + gap fields (OVERFITTING.md §12 #5)
# ---------------------------------------------------------------------------


class _FakeResult:
    """A minimal stand-in for a TournamentResult carrying a holdout scalar."""

    def __init__(self, holdout_child_scalar: float | None) -> None:
        self.holdout_child_scalar = holdout_child_scalar


def test_generalization_fields_with_holdout() -> None:
    from zicato.orchestrator import _generalization_fields

    fields = _generalization_fields(0.40, _FakeResult(holdout_child_scalar=0.55))
    assert fields["train_loss"] == 0.40
    assert fields["holdout_loss"] == 0.55
    assert fields["generalization_gap"] == pytest.approx(0.15)


def test_generalization_fields_degrade_without_holdout() -> None:
    from zicato.orchestrator import _generalization_fields

    fields = _generalization_fields(0.40, _FakeResult(holdout_child_scalar=None))
    assert fields["train_loss"] == 0.40
    assert fields["holdout_loss"] is None
    assert fields["generalization_gap"] is None
