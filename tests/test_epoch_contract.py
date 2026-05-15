"""Tests for :mod:`zicato.epoch.contract` — contract-hash canonicalization.

The contract hash must be *stable* across spurious edits (whitespace,
row reordering, float-formatting noise) and *sensitive* to semantic
changes (a board entry's input, a scoring weight, the entrypoint, the
mutable-tree set). These tests pin both halves.
"""

from __future__ import annotations

import json
from pathlib import Path

from zicato.epoch.contract import (
    ContractInputs,
    compute_contract_hash,
    resolve_contract_inputs,
)

# ---------------------------------------------------------------------------
# Fixtures — minimal contract files
# ---------------------------------------------------------------------------


_BOARD_LINE_A = json.dumps(
    {
        "id": "entry_a",
        "kind": "single_turn",
        "wall_clock_budget_seconds": 60,
        "input": "hello world",
    }
)
_BOARD_LINE_B = json.dumps(
    {
        "id": "entry_b",
        "kind": "single_turn",
        "wall_clock_budget_seconds": 60,
        "input": "goodbye world",
    }
)

_RUBRIC = "# Rubric\n\n## Focus\n- Be careful.\n"

_SCORING = json.dumps({"drift_weight": 1.0, "pass_weight": 1.0, "promote_margin": 0.01})


def _write_contract(
    tmp_path: Path,
    *,
    board: str = _BOARD_LINE_A + "\n" + _BOARD_LINE_B + "\n",
    rubric: str = _RUBRIC,
    scoring: str = _SCORING,
) -> ContractInputs:
    board_path = tmp_path / "board.jsonl"
    rubric_path = tmp_path / "rubric.md"
    scoring_path = tmp_path / "scoring.json"
    board_path.write_text(board)
    rubric_path.write_text(rubric)
    scoring_path.write_text(scoring)
    return ContractInputs(
        board_path=board_path,
        rubric_path=rubric_path,
        scoring_path=scoring_path,
        entrypoint="pkg.mod:agent",
        mutable_trees=(str(tmp_path / "agent"),),
    )


# ---------------------------------------------------------------------------
# Stability — spurious edits must NOT change the hash
# ---------------------------------------------------------------------------


def test_hash_stable_across_whitespace_only_rubric_edits(tmp_path: Path) -> None:
    base = _write_contract(tmp_path)
    h1 = compute_contract_hash(base)

    # Re-write the rubric with CRLF line endings, trailing whitespace,
    # and extra leading/trailing blank lines.
    base.rubric_path.write_text("\n\n# Rubric   \r\n\r\n## Focus\r\n- Be careful.   \r\n\n\n")
    h2 = compute_contract_hash(base)
    assert h1 == h2


def test_hash_stable_across_board_entry_reordering(tmp_path: Path) -> None:
    base = _write_contract(tmp_path)
    h1 = compute_contract_hash(base)

    # Reorder the two board rows — canonicalization sorts by id.
    base.board_path.write_text(_BOARD_LINE_B + "\n" + _BOARD_LINE_A + "\n")
    h2 = compute_contract_hash(base)
    assert h1 == h2


def test_hash_stable_across_scoring_float_noise(tmp_path: Path) -> None:
    base = _write_contract(tmp_path)
    h1 = compute_contract_hash(base)

    # Float-formatting noise below the 6-dp rounding threshold.
    base.scoring_path.write_text(
        json.dumps(
            {
                "drift_weight": 1.0000000001,
                "pass_weight": 0.9999999999,
                "promote_margin": 0.01,
            }
        )
    )
    h2 = compute_contract_hash(base)
    assert h1 == h2


# ---------------------------------------------------------------------------
# Sensitivity — semantic changes MUST change the hash
# ---------------------------------------------------------------------------


def test_hash_changes_on_board_entry_input_edit(tmp_path: Path) -> None:
    base = _write_contract(tmp_path)
    h1 = compute_contract_hash(base)

    edited = json.dumps(
        {
            "id": "entry_a",
            "kind": "single_turn",
            "wall_clock_budget_seconds": 60,
            "input": "hello CHANGED world",
        }
    )
    base.board_path.write_text(edited + "\n" + _BOARD_LINE_B + "\n")
    h2 = compute_contract_hash(base)
    assert h1 != h2


def test_hash_changes_on_scoring_weight_edit(tmp_path: Path) -> None:
    base = _write_contract(tmp_path)
    h1 = compute_contract_hash(base)

    base.scoring_path.write_text(
        json.dumps({"drift_weight": 2.0, "pass_weight": 1.0, "promote_margin": 0.01})
    )
    h2 = compute_contract_hash(base)
    assert h1 != h2


def test_hash_changes_on_entrypoint_edit(tmp_path: Path) -> None:
    base = _write_contract(tmp_path)
    h1 = compute_contract_hash(base)

    from dataclasses import replace

    moved = replace(base, entrypoint="pkg.mod:OTHER_agent")
    h2 = compute_contract_hash(moved)
    assert h1 != h2


def test_hash_changes_on_adding_a_mutable_tree(tmp_path: Path) -> None:
    base = _write_contract(tmp_path)
    h1 = compute_contract_hash(base)

    from dataclasses import replace

    expanded = replace(
        base,
        mutable_trees=base.mutable_trees + (str(tmp_path / "extra_agent"),),
    )
    h2 = compute_contract_hash(expanded)
    assert h1 != h2


def test_hash_stable_across_mutable_tree_reordering(tmp_path: Path) -> None:
    from dataclasses import replace

    base = _write_contract(tmp_path)
    two = replace(
        base,
        mutable_trees=(str(tmp_path / "a"), str(tmp_path / "b")),
    )
    h1 = compute_contract_hash(two)
    swapped = replace(
        base,
        mutable_trees=(str(tmp_path / "b"), str(tmp_path / "a")),
    )
    h2 = compute_contract_hash(swapped)
    assert h1 == h2


# ---------------------------------------------------------------------------
# Missing files
# ---------------------------------------------------------------------------


def test_missing_files_hash_deterministically(tmp_path: Path) -> None:
    """A workspace with no contract files still hashes the same twice."""
    inputs = ContractInputs(
        board_path=tmp_path / "nope_board.jsonl",
        rubric_path=tmp_path / "nope_rubric.md",
        scoring_path=tmp_path / "nope_scoring.json",
        entrypoint="",
        mutable_trees=(),
    )
    h1 = compute_contract_hash(inputs)
    h2 = compute_contract_hash(inputs)
    assert h1 == h2
    assert isinstance(h1, str) and len(h1) == 64


def test_missing_board_differs_from_present_board(tmp_path: Path) -> None:
    """A missing board hashes differently than a populated one."""
    base = _write_contract(tmp_path)
    h_present = compute_contract_hash(base)

    from dataclasses import replace

    h_missing = compute_contract_hash(replace(base, board_path=tmp_path / "absent.jsonl"))
    assert h_present != h_missing


# ---------------------------------------------------------------------------
# resolve_contract_inputs
# ---------------------------------------------------------------------------


def test_resolve_contract_inputs_reads_config(tmp_path: Path) -> None:
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    (workspace / "config.json").write_text(
        json.dumps(
            {
                "adk_entrypoint": "pkg.mod:agent",
                "mutable_trees": ["/abs/agent"],
                "contract": {
                    "board_path": "/abs/board.jsonl",
                    "rubric_path": "/abs/rubric.md",
                    "scoring_path": "/abs/scoring.json",
                },
            }
        )
    )
    inputs = resolve_contract_inputs(workspace)
    assert inputs.entrypoint == "pkg.mod:agent"
    assert inputs.mutable_trees == ("/abs/agent",)
    assert inputs.board_path == Path("/abs/board.jsonl")


def test_resolve_contract_inputs_raises_without_config(tmp_path: Path) -> None:
    import pytest

    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    with pytest.raises(FileNotFoundError, match="zicato register"):
        resolve_contract_inputs(workspace)


def test_resolve_contract_inputs_defaults_when_no_contract_key(
    tmp_path: Path,
) -> None:
    """A workspace registered before auto-epoching uses the default paths."""
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    (workspace / "config.json").write_text(
        json.dumps({"adk_entrypoint": "pkg.mod:agent", "mutable_trees": []})
    )
    inputs = resolve_contract_inputs(workspace)
    # Defaults sit next to the workspace dir (the operator's project root).
    assert inputs.board_path == (tmp_path / "board.jsonl").resolve()
    assert inputs.rubric_path == (tmp_path / "rubric.md").resolve()
