"""Tests for the target 2 (goldfive steering) example scaffolding.

These tests are deliberately shallow — they verify that the example
directory's static artifacts (board, predicates module) are structurally
well-formed without actually running anything. The full runtime
behaviour is exercised by the synthetic-adversarial path in the runner
(landing in parallel under R2-L).
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

# Resolve the example directory through the installed
# ``zicato_examples`` package so the test is independent of where the
# examples distribution lives on disk.
import zicato_examples.target_2_goldfive_steering as _t2_pkg  # noqa: E402

EXAMPLE_DIR = Path(_t2_pkg.__file__).resolve().parent


def test_board_loads() -> None:
    """``board.jsonl`` parses as JSONL and has the expected kind mix.

    Adversarial entries MUST declare ``required_drift_kinds`` and
    ``adversarial_agent_spec`` — that is the contract that lets the
    runtime layer materialise the required-drift assertion.
    """

    path = EXAMPLE_DIR / "board.jsonl"
    assert path.exists(), f"board.jsonl missing at {path}"

    entries = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    kinds = {e["kind"] for e in entries}
    assert "synthetic_adversarial" in kinds
    assert "synthetic_clean" in kinds

    for entry in entries:
        if entry["kind"] == "synthetic_adversarial":
            assert entry.get(
                "required_drift_kinds"
            ), f"entry {entry['id']} missing required_drift_kinds"
            assert entry.get(
                "adversarial_agent_spec"
            ), f"entry {entry['id']} missing adversarial_agent_spec"


def test_predicates_module_imports() -> None:
    """The predicates module imports and exports the three documented hooks."""

    mod = importlib.import_module("zicato_examples.target_2_goldfive_steering.predicates")
    assert hasattr(mod, "required_drift_fired")
    assert hasattr(mod, "no_warning_or_critical_drift")
    assert hasattr(mod, "output_mentions_target_token")
