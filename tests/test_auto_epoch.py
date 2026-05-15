"""Tests for :func:`zicato.orchestrator.ensure_epoch_for_contract`.

These exercise the contract-hash auto-roll logic in isolation — no
proposer, no tournament. The orchestrator's epoch-resolution hook is
the unit under test.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from zicato.epoch.lifecycle import current_epoch_id, list_epochs, load_epoch
from zicato.orchestrator import ensure_epoch_for_contract

# ---------------------------------------------------------------------------
# LLM stubs
# ---------------------------------------------------------------------------


async def _aux_llm(system: str, user: str, model: str) -> str:
    del system, user, model
    return "stub analysis"


# ---------------------------------------------------------------------------
# Workspace bootstrap
# ---------------------------------------------------------------------------


_BOARD = (
    json.dumps(
        {
            "id": "entry_a",
            "kind": "single_turn",
            "wall_clock_budget_seconds": 60,
            "input": "hello",
        }
    )
    + "\n"
)
_RUBRIC = "# Rubric\n- Be careful.\n"
_SCORING = json.dumps({"drift_weight": 1.0, "pass_weight": 1.0})


def _bootstrap(tmp_path: Path) -> tuple[Path, dict[str, Path]]:
    """Create a registered workspace with live contract files.

    Returns ``(workspace_root, {board, rubric, scoring})`` so tests can
    mutate the live contract files and re-resolve.
    """
    workspace = tmp_path / ".zicato"
    workspace.mkdir()

    board = tmp_path / "board.jsonl"
    rubric = tmp_path / "rubric.md"
    scoring = tmp_path / "scoring.json"
    board.write_text(_BOARD)
    rubric.write_text(_RUBRIC)
    scoring.write_text(_SCORING)

    (workspace / "config.json").write_text(
        json.dumps(
            {
                "instance_id": "test",
                "adk_entrypoint": "pkg.mod:agent",
                "mutable_trees": [str(tmp_path / "agent")],
                "source_roots": [str(tmp_path / "agent")],
                "contract": {
                    "board_path": str(board),
                    "rubric_path": str(rubric),
                    "scoring_path": str(scoring),
                },
            }
        )
    )
    return workspace, {"board": board, "rubric": rubric, "scoring": scoring}


# ---------------------------------------------------------------------------
# No current epoch
# ---------------------------------------------------------------------------


def test_no_current_epoch_auto_creates_e0(tmp_path: Path) -> None:
    workspace, _ = _bootstrap(tmp_path)
    eid = asyncio.run(ensure_epoch_for_contract(workspace, auto_epoch=True, aux_call_llm=_aux_llm))
    assert eid.endswith("_e0")
    assert current_epoch_id(workspace) == eid
    # The created epoch carries a non-empty contract hash.
    cfg = load_epoch(workspace, eid)
    assert cfg.contract_hash


def test_no_current_epoch_no_auto_raises(tmp_path: Path) -> None:
    workspace, _ = _bootstrap(tmp_path)
    with pytest.raises(FileNotFoundError, match="epoch new"):
        asyncio.run(ensure_epoch_for_contract(workspace, auto_epoch=False, aux_call_llm=_aux_llm))


# ---------------------------------------------------------------------------
# Contract unchanged
# ---------------------------------------------------------------------------


def test_contract_unchanged_returns_same_epoch(tmp_path: Path) -> None:
    workspace, _ = _bootstrap(tmp_path)
    first = asyncio.run(
        ensure_epoch_for_contract(workspace, auto_epoch=True, aux_call_llm=_aux_llm)
    )
    # No edits — a second call must return the same epoch, no roll.
    second = asyncio.run(
        ensure_epoch_for_contract(workspace, auto_epoch=True, aux_call_llm=_aux_llm)
    )
    assert first == second
    assert len(list_epochs(workspace)) == 1


# ---------------------------------------------------------------------------
# Contract changed
# ---------------------------------------------------------------------------


def test_contract_changed_auto_rolls(tmp_path: Path) -> None:
    workspace, files = _bootstrap(tmp_path)
    first = asyncio.run(
        ensure_epoch_for_contract(workspace, auto_epoch=True, aux_call_llm=_aux_llm)
    )

    # Edit the rubric (semantic change) → contract drifts.
    files["rubric"].write_text("# Rubric\n- Be careful.\n- Now also: be bold.\n")

    rolled = asyncio.run(
        ensure_epoch_for_contract(workspace, auto_epoch=True, aux_call_llm=_aux_llm)
    )
    assert rolled != first
    assert rolled.endswith("_e1")
    assert current_epoch_id(workspace) == rolled

    # Old epoch was closed by the roll.
    old_cfg = load_epoch(workspace, first)
    assert old_cfg.closed

    # Two epochs now, and the new one points back at the old via lineage.
    epochs = list_epochs(workspace)
    assert len(epochs) == 2


def test_contract_changed_no_auto_raises(tmp_path: Path) -> None:
    workspace, files = _bootstrap(tmp_path)
    asyncio.run(ensure_epoch_for_contract(workspace, auto_epoch=True, aux_call_llm=_aux_llm))
    files["scoring"].write_text(json.dumps({"drift_weight": 9.0}))

    with pytest.raises(RuntimeError, match="drifted"):
        asyncio.run(ensure_epoch_for_contract(workspace, auto_epoch=False, aux_call_llm=_aux_llm))


def test_contract_change_message_names_component(tmp_path: Path) -> None:
    """The roll-time message names which contract component changed."""
    workspace, files = _bootstrap(tmp_path)
    asyncio.run(ensure_epoch_for_contract(workspace, auto_epoch=True, aux_call_llm=_aux_llm))
    files["scoring"].write_text(json.dumps({"drift_weight": 9.0}))
    with pytest.raises(RuntimeError, match="scoring"):
        asyncio.run(ensure_epoch_for_contract(workspace, auto_epoch=False, aux_call_llm=_aux_llm))


# ---------------------------------------------------------------------------
# Legacy epoch (empty contract_hash)
# ---------------------------------------------------------------------------


def test_legacy_epoch_treated_as_matching(tmp_path: Path) -> None:
    """An epoch with contract_hash == "" must not trigger a spurious roll."""
    workspace, files = _bootstrap(tmp_path)
    from zicato.core.types import ScoringWeights
    from zicato.epoch.lifecycle import new_epoch

    # Create an epoch the old way — new_epoch with no entrypoint/trees
    # still computes a hash, so we forcibly blank it to simulate a
    # pre-auto-epoch workspace.
    cfg = new_epoch(
        workspace,
        name="legacy",
        board_source=files["board"],
        rubric_source=files["rubric"],
        weights=ScoringWeights(),
        auto_close_previous=False,
    )
    config_path = workspace / "epochs" / cfg.id / "config.json"
    raw = json.loads(config_path.read_text())
    raw["contract_hash"] = ""
    config_path.write_text(json.dumps(raw))

    # Even with a wildly different contract, a legacy epoch is treated
    # as always-matching → no roll.
    files["rubric"].write_text("# totally different rubric content\n")
    resolved = asyncio.run(
        ensure_epoch_for_contract(workspace, auto_epoch=True, aux_call_llm=_aux_llm)
    )
    assert resolved == cfg.id
    assert len(list_epochs(workspace)) == 1


# ---------------------------------------------------------------------------
# Explicit --epoch skips auto-rolling
# ---------------------------------------------------------------------------


def test_explicit_epoch_skips_auto_roll(tmp_path: Path) -> None:
    """When evolve_n_rounds is given an explicit epoch_id, no roll happens.

    evolve_n_rounds only calls :func:`ensure_epoch_for_contract` when
    ``epoch_id is None``. We patch the hook to blow up and run with a
    real round count + an explicit epoch — the run will fail somewhere
    downstream (no real proposer is wired) but NOT with our sentinel
    AssertionError, proving the auto-roll branch was never entered.
    """
    workspace, files = _bootstrap(tmp_path)
    first = asyncio.run(
        ensure_epoch_for_contract(workspace, auto_epoch=True, aux_call_llm=_aux_llm)
    )
    # Drift the contract — would trigger a roll if the hook ran.
    files["rubric"].write_text("# changed rubric\n")

    import zicato.orchestrator as orch

    async def _boom(*args: object, **kwargs: object) -> str:
        raise AssertionError("ensure_epoch_for_contract should be skipped")

    original = orch.ensure_epoch_for_contract
    orch.ensure_epoch_for_contract = _boom  # type: ignore[assignment]
    try:
        with pytest.raises(Exception) as excinfo:  # noqa: PT011
            asyncio.run(
                orch.evolve_n_rounds(
                    rounds=1,
                    workspace_root=workspace,
                    epoch_id=first,
                    harness_call_llm=_aux_llm,
                    auxiliary_call_llm=_aux_llm,
                )
            )
        # The failure must NOT be our sentinel — the hook was skipped.
        assert "ensure_epoch_for_contract should be skipped" not in str(excinfo.value)
    finally:
        orch.ensure_epoch_for_contract = original  # type: ignore[assignment]

    # Still exactly one epoch — no roll.
    assert len(list_epochs(workspace)) == 1
