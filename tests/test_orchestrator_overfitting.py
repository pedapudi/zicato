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

from tests._proposal_evidence import render_proposal_evidence
from zicato.board.split import HOLDOUT_TAG, split_board
from zicato.core import BoardEntry, DriftCount, ExpectationResult, LossProfile, ScoringWeights
from zicato.core.types import OverfittingConfig
from zicato.core.workspace import loss_profile_path
from zicato.evolve.decision_support import _load_parent_losses, _render_loss_summary
from zicato.patterns import ALL_DETECTORS, DetectorInput, detect_patterns
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
    return render_proposal_evidence(
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
# The baseline round's champion losses come from the pre-flight's A/A band
# ---------------------------------------------------------------------------


def _write_replicate_loss(tmp_path: Path, entry_id: str, replicate: int, loss: LossProfile) -> None:
    canonical = loss_profile_path(tmp_path, _EPOCH, _PARENT, entry_id)
    canonical.parent.mkdir(parents=True, exist_ok=True)
    write_loss_profile(loss, canonical.with_name(f"loss.r{replicate}.json"))


def _degraded_loss(entry_id: str) -> LossProfile:
    """A pre-flight probe result: the champion's snapshot deliberately broken.

    Distinctive kind + a loss an order of magnitude above the clean draws, so
    any leak into the proposer's view is unmistakable in either surface.
    """
    profile = _loss(entry_id, drift_count=99)
    return type(profile)(
        **{
            **{f.name: getattr(profile, f.name) for f in profile.__dataclass_fields__.values()},
            "drift_counts": (DriftCount(kind="task_failed_fatal", severity="critical", count=99),),
            "pass_fail": False,
        }
    )


def test_baseline_round_reads_the_calibration_band_not_the_degraded_probes(
    tmp_path: Path,
) -> None:
    # The shape of an epoch's FIRST round: the contract pre-flight has run the
    # champion over the board (A/A draws at 1000+) and probed it degraded
    # (2000+), but no duel has written replicate 0 yet.
    board = _board()
    for entry in board:
        _write_replicate_loss(tmp_path, entry.id, 1000, _loss(entry.id, drift_count=2))
        _write_replicate_loss(tmp_path, entry.id, 1001, _loss(entry.id, drift_count=4))
        _write_replicate_loss(tmp_path, entry.id, 2000, _degraded_loss(entry.id))
    weights = ScoringWeights()

    train_ids, _holdout = split_board(board, weights.overfitting)
    train_board = [e for e in board if e.id in set(train_ids)]
    losses = _load_parent_losses(tmp_path, _EPOCH, _PARENT, train_board, read_loss_profile)

    # One folded profile per train entry — not one per draw, so the outcome
    # marginals' denominator stays "runs on the board".
    assert len(losses) == len(train_board)
    # The fold is the MEAN of the two calibration draws (2 and 4), which also
    # proves the degraded 99 never entered the arithmetic.
    assert [loss.drift_loss for loss in losses] == [3.0] * len(train_board)
    assert all(loss.pass_fail is True for loss in losses)
    assert all(
        kind not in {"task_failed_fatal"}
        for loss in losses
        for kind in (dc.kind for dc in loss.drift_counts)
    )
    # Provenance reads as the calibration draw it is, not as a duel.
    assert all(loss.match_id == "" or "aa-calibration" in loss.match_id for loss in losses)

    # The detectors now have a non-empty slice to work on — the baseline
    # channel this round used to open empty — and it describes the champion's
    # real code only. (The prompt's own vocabulary block names every valid
    # drift kind by construction, so the check is on the detected patterns.)
    detected = detect_patterns(
        DetectorInput(losses=losses, entries={e.id: e for e in train_board}, events_paths={}),
        detectors=ALL_DETECTORS,
    )
    assert detected
    assert all("task_failed_fatal" not in f"{p.summary} {p.detail}" for p in detected)
    assert "SECRET_HOLDOUT_ENTRY" not in _assemble_prompt(tmp_path, board, weights)


def test_duel_replicate_zero_wins_over_the_calibration_band(tmp_path: Path) -> None:
    # From round 1 on, replicate 0 exists again: it is a draw under the
    # round's own conditions, so it takes precedence entry by entry.
    board = _board()
    _write_losses(tmp_path, board)  # drift_count=3 at replicate 0
    for entry in board:
        _write_replicate_loss(tmp_path, entry.id, 1000, _loss(entry.id, drift_count=50))
    weights = ScoringWeights()

    train_ids, _holdout = split_board(board, weights.overfitting)
    train_board = [e for e in board if e.id in set(train_ids)]
    losses = _load_parent_losses(tmp_path, _EPOCH, _PARENT, train_board, read_loss_profile)

    assert [loss.drift_loss for loss in losses] == [3.0] * len(train_board)


def test_calibration_fallback_never_opens_a_holdout_entry(tmp_path: Path) -> None:
    # The calibration band covers the FULL board, so the guarantee has to come
    # from the reader iterating the train slice — not from what is on disk.
    board = _board()
    for entry in board:
        _write_replicate_loss(tmp_path, entry.id, 1000, _loss(entry.id, drift_count=2))
    weights = ScoringWeights()

    train_ids, holdout_ids = split_board(board, weights.overfitting)
    assert "SECRET_HOLDOUT_ENTRY" in holdout_ids
    train_board = [e for e in board if e.id in set(train_ids)]
    losses = _load_parent_losses(tmp_path, _EPOCH, _PARENT, train_board, read_loss_profile)

    assert {loss.entry_id for loss in losses} == set(train_ids)


# ---------------------------------------------------------------------------
# Per-generation train/holdout loss + gap fields (OVERFITTING.md §12 #5)
# ---------------------------------------------------------------------------


class _FakeResult:
    """A minimal stand-in for a TournamentResult carrying a holdout scalar."""

    def __init__(self, holdout_child_scalar: float | None) -> None:
        self.holdout_child_scalar = holdout_child_scalar


def test_generalization_fields_with_holdout() -> None:
    from zicato.evolve.decision_support import _generalization_fields

    fields = _generalization_fields(0.40, _FakeResult(holdout_child_scalar=0.55))
    assert fields["train_loss"] == 0.40
    assert fields["holdout_loss"] == 0.55
    assert fields["generalization_gap"] == pytest.approx(0.15)


def test_generalization_fields_degrade_without_holdout() -> None:
    from zicato.evolve.decision_support import _generalization_fields

    fields = _generalization_fields(0.40, _FakeResult(holdout_child_scalar=None))
    assert fields["train_loss"] == 0.40
    assert fields["holdout_loss"] is None
    assert fields["generalization_gap"] is None
