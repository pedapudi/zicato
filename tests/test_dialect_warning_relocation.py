"""The telemetry-dialect capability warning is surfaced ONCE at preflight.

LOGGING.md §6: ``dialect_capability_warnings`` is a pure function of the
contract's weights, so it moved OUT of the per-board-unit reducer (where it
fired N times, invisibly, inside the killable worker) and INTO a single
contract-load preflight emission. These tests pin both halves: the reducer
no longer emits it, and the preflight helper emits it exactly once.
"""

from __future__ import annotations

import logging
from pathlib import Path

from zicato.core import BoardEntry, ExpectationResult, ScoringWeights
from zicato.telemetry.dialects import DIALECT_TRANSCRIPT, dialect_capability_warnings
from zicato.telemetry.reducer import reduce_loss


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    import json

    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def _warn_y_weights() -> ScoringWeights:
    # transcript produces no drift, so a non-default drift_weight is a
    # capability-warning trigger (dialect_capability_warnings is non-empty).
    w = ScoringWeights(telemetry_dialect=DIALECT_TRANSCRIPT, drift_weight=99.0)
    assert dialect_capability_warnings(w), "fixture must actually warn"
    return w


def test_reducer_no_longer_emits_capability_warnings(tmp_path: Path, caplog) -> None:
    """Reducing a run does NOT log the 'inert under' capability warnings.

    They are contract-level, not run-level — the reducer must be silent on
    them however many board units it reduces.
    """
    events = tmp_path / "t.jsonl"
    _write_jsonl(events, [{"role": "assistant", "content": "hello world"}])
    entry = BoardEntry(id="e", kind="single_turn", wall_clock_budget_seconds=60, input="hi")
    er = ExpectationResult(kind="predicate", passed=True)
    weights = _warn_y_weights()

    with caplog.at_level(logging.WARNING, logger="zicato.telemetry.reducer"):
        # Reduce twice — the old code would have emitted the warning on EACH.
        reduce_loss(events, entry, "g", "e", er, 0, False, weights)
        reduce_loss(events, entry, "g", "e", er, 0, False, weights)

    assert not any("inert under" in rec.getMessage() for rec in caplog.records)


def test_preflight_helper_emits_each_warning_once(tmp_path: Path, caplog, monkeypatch) -> None:
    """``emit_dialect_capability_warnings`` logs each warning exactly once."""
    from zicato.evolve import loop as loop_mod

    weights = _warn_y_weights()
    expected = dialect_capability_warnings(weights)

    # Stub the scoring load so the helper reads our warn-y weights without a
    # full workspace on disk.
    monkeypatch.setattr("zicato.workspace_loader.load_current_scoring", lambda _root: weights)

    with caplog.at_level(logging.WARNING, logger="zicato.orchestrator"):
        emitted = loop_mod.emit_dialect_capability_warnings(Path("."))

    assert emitted == expected
    warn_lines = [r.getMessage() for r in caplog.records if "contract pre-flight" in r.getMessage()]
    # One WARNING per capability warning — surfaced once, not N-per-entry.
    assert len(warn_lines) == len(expected)
    assert all(dialect in msg for dialect in ["transcript"] for msg in warn_lines)


def test_goldfive_dialect_emits_nothing(tmp_path: Path, caplog, monkeypatch) -> None:
    """The capable default dialect surfaces no capability warning."""
    from zicato.evolve import loop as loop_mod

    monkeypatch.setattr(
        "zicato.workspace_loader.load_current_scoring", lambda _root: ScoringWeights()
    )
    with caplog.at_level(logging.WARNING, logger="zicato.orchestrator"):
        emitted = loop_mod.emit_dialect_capability_warnings(Path("."))
    assert emitted == ()
    assert not any("contract pre-flight" in r.getMessage() for r in caplog.records)
