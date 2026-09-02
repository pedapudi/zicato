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


def _raise_adapter_factory() -> object:
    raise RuntimeError("adapter construction failed")


def _frozen_contract_codes(workspace: Path, epoch_id: str) -> set[str]:
    from zicato.check.context import CheckContext
    from zicato.check.validators import frozen_epoch_contract_identity

    with CheckContext(workspace, epoch_id=epoch_id) as context:
        return {code for code, _summary, _detail in frozen_epoch_contract_identity(context)}


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
_BRIEF = "# Proposer brief\n- Be careful.\n"
_SCORING = json.dumps({"pass_weight": 1.0})


def _bootstrap(tmp_path: Path) -> tuple[Path, dict[str, Path]]:
    """Create a registered workspace with live contract files.

    Returns ``(workspace_root, {board, brief, scoring})`` so tests can
    mutate the live contract files and re-resolve.
    """
    workspace = tmp_path / ".zicato"
    workspace.mkdir()

    board = tmp_path / "board.jsonl"
    brief = tmp_path / "brief.md"
    scoring = tmp_path / "scoring.json"
    board.write_text(_BOARD)
    brief.write_text(_BRIEF)
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
                    "brief_path": str(brief),
                    "scoring_path": str(scoring),
                },
            }
        )
    )
    return workspace, {"board": board, "brief": brief, "scoring": scoring}


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


def test_default_scope_does_not_auto_roll(tmp_path: Path) -> None:
    """A live scoring.json that never mentions pass_rate_monotonicity_scope
    creates an epoch and re-resolves with NO roll — the new field defaults
    to ``per_entry`` on both the live and frozen sides, so the contract
    hash is unchanged (issue #17 back-compat)."""
    workspace, _ = _bootstrap(tmp_path)
    first = asyncio.run(
        ensure_epoch_for_contract(workspace, auto_epoch=True, aux_call_llm=_aux_llm)
    )
    second = asyncio.run(
        ensure_epoch_for_contract(workspace, auto_epoch=True, aux_call_llm=_aux_llm)
    )
    assert first == second
    assert len(list_epochs(workspace)) == 1


def test_aggregate_scope_epoch_new_then_evolve_does_not_auto_roll(tmp_path: Path) -> None:
    """An ``epoch new`` -> ``evolve`` flow with a NON-DEFAULT scope must not
    auto-roll: the frozen snapshot captures ``aggregate`` and the next
    resolve re-hashes identically (issue #17 + the issue #13 serializer)."""
    workspace, files = _bootstrap(tmp_path)
    # The operator's live scoring.json opts into aggregate scope.
    files["scoring"].write_text(
        json.dumps(
            {
                "pass_weight": 1.0,
                "pass_rate_monotonicity": True,
                "pass_rate_monotonicity_scope": "aggregate",
            }
        )
    )
    first = asyncio.run(
        ensure_epoch_for_contract(workspace, auto_epoch=True, aux_call_llm=_aux_llm)
    )

    # The frozen snapshot persisted the non-default scope, so the loader
    # reads it back as aggregate (no silent drop to the default).
    frozen = json.loads((workspace / "epochs" / first / "scoring.json").read_text(encoding="utf-8"))
    assert frozen["pass_rate_monotonicity_scope"] == "aggregate"

    # Re-resolving (the next evolve) must NOT roll — same contract.
    second = asyncio.run(
        ensure_epoch_for_contract(workspace, auto_epoch=True, aux_call_llm=_aux_llm)
    )
    assert first == second
    assert len(list_epochs(workspace)) == 1


def test_flipping_scope_to_aggregate_auto_rolls(tmp_path: Path) -> None:
    """Changing the scope from the default to ``aggregate`` between resolves
    DOES roll — it is a real evaluation-contract change (issue #17)."""
    workspace, files = _bootstrap(tmp_path)
    first = asyncio.run(
        ensure_epoch_for_contract(workspace, auto_epoch=True, aux_call_llm=_aux_llm)
    )
    files["scoring"].write_text(
        json.dumps(
            {
                "pass_weight": 1.0,
                "pass_rate_monotonicity_scope": "aggregate",
            }
        )
    )
    rolled = asyncio.run(
        ensure_epoch_for_contract(workspace, auto_epoch=True, aux_call_llm=_aux_llm)
    )
    assert rolled != first
    assert len(list_epochs(workspace)) == 2


# ---------------------------------------------------------------------------
# Contract changed
# ---------------------------------------------------------------------------


def test_contract_changed_auto_rolls(tmp_path: Path) -> None:
    workspace, files = _bootstrap(tmp_path)
    first = asyncio.run(
        ensure_epoch_for_contract(workspace, auto_epoch=True, aux_call_llm=_aux_llm)
    )

    # Edit the proposer brief (semantic change) → contract drifts.
    files["brief"].write_text("# Proposer brief\n- Be careful.\n- Now also: be bold.\n")

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
    files["scoring"].write_text(json.dumps({"pass_weight": 9.0}))

    with pytest.raises(RuntimeError, match="drifted"):
        asyncio.run(ensure_epoch_for_contract(workspace, auto_epoch=False, aux_call_llm=_aux_llm))


def test_contract_change_message_names_component(tmp_path: Path) -> None:
    """The roll-time message names which contract component changed."""
    workspace, files = _bootstrap(tmp_path)
    asyncio.run(ensure_epoch_for_contract(workspace, auto_epoch=True, aux_call_llm=_aux_llm))
    files["scoring"].write_text(json.dumps({"pass_weight": 9.0}))
    with pytest.raises(RuntimeError, match="scoring"):
        asyncio.run(ensure_epoch_for_contract(workspace, auto_epoch=False, aux_call_llm=_aux_llm))


# ---------------------------------------------------------------------------
# Legacy epoch (empty contract_hash)
# ---------------------------------------------------------------------------


def test_legacy_epoch_treated_as_matching(tmp_path: Path) -> None:
    """A legacy on-disk contract_hash "" must not trigger a spurious roll.

    The reader normalises the legacy empty string to ``None``; the
    epoching "never rolls" rule is an ``is None`` check, so a pre-feature
    epoch is treated as always-matching.
    """
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
        brief_source=files["brief"],
        weights=ScoringWeights(),
        auto_close_previous=False,
    )
    config_path = workspace / "epochs" / cfg.id / "config.json"
    raw = json.loads(config_path.read_text())
    raw["contract_hash"] = ""
    config_path.write_text(json.dumps(raw))

    # Even with a wildly different contract, a legacy epoch is treated
    # as always-matching → no roll.
    files["brief"].write_text("# totally different proposer brief content\n")
    resolved = asyncio.run(
        ensure_epoch_for_contract(workspace, auto_epoch=True, aux_call_llm=_aux_llm)
    )
    assert resolved == cfg.id
    assert len(list_epochs(workspace)) == 1


# ---------------------------------------------------------------------------
# Proposer drift auto-rolls
# ---------------------------------------------------------------------------


_SKILL = "---\nname: tighten\ndescription: keep it terse\n---\n\nPrefer terse patches.\n"


def _set_proposer_path(workspace: Path, proposer_path: Path | None) -> None:
    """Rewrite ``config.json``'s ``contract.proposer_path`` in place."""
    config_path = workspace / "config.json"
    config = json.loads(config_path.read_text())
    if proposer_path is None:
        config["contract"].pop("proposer_path", None)
    else:
        config["contract"]["proposer_path"] = str(proposer_path)
    config_path.write_text(json.dumps(config))


def test_configuring_a_proposer_dir_auto_rolls(tmp_path: Path) -> None:
    """Pointing the contract at a proposer dir between resolves rolls the epoch."""
    workspace, _ = _bootstrap(tmp_path)
    first = asyncio.run(
        ensure_epoch_for_contract(workspace, auto_epoch=True, aux_call_llm=_aux_llm)
    )

    # Configure a proposer dir (builtin → dir:<name> drifts the contract).
    proposer = tmp_path / "proposers" / "p1"
    (proposer / "skills").mkdir(parents=True)
    (proposer / "skills" / "a.md").write_text(_SKILL)
    _set_proposer_path(workspace, proposer)

    rolled = asyncio.run(
        ensure_epoch_for_contract(workspace, auto_epoch=True, aux_call_llm=_aux_llm)
    )
    assert rolled != first
    assert current_epoch_id(workspace) == rolled
    assert len(list_epochs(workspace)) == 2
    # The new epoch froze the proposer dir into its config.
    assert load_epoch(workspace, rolled).proposer_path == proposer


def test_editing_a_skill_auto_rolls_and_cites_proposer(tmp_path: Path) -> None:
    """Editing a configured skill between resolves rolls + cites ``proposer``."""
    workspace, _ = _bootstrap(tmp_path)
    proposer = tmp_path / "proposers" / "p1"
    (proposer / "skills").mkdir(parents=True)
    (proposer / "skills" / "a.md").write_text(_SKILL)
    _set_proposer_path(workspace, proposer)

    asyncio.run(ensure_epoch_for_contract(workspace, auto_epoch=True, aux_call_llm=_aux_llm))

    # Semantic skill edit → contract drifts; the message must name proposer.
    (proposer / "skills" / "a.md").write_text(
        "---\nname: tighten\ndescription: keep it terse\n---\n\nA materially different skill.\n"
    )
    with pytest.raises(RuntimeError, match="proposer"):
        asyncio.run(ensure_epoch_for_contract(workspace, auto_epoch=False, aux_call_llm=_aux_llm))


def test_explicit_epoch_contract_check_rejects_adapter_and_tree_drift(tmp_path: Path) -> None:
    workspace, _ = _bootstrap(tmp_path)
    epoch_id = asyncio.run(
        ensure_epoch_for_contract(workspace, auto_epoch=True, aux_call_llm=_aux_llm)
    )
    assert "frozen_epoch_contract_mismatch" not in _frozen_contract_codes(workspace, epoch_id)

    config_path = workspace / "config.json"
    config = json.loads(config_path.read_text())
    config["adk_entrypoint"] = "pkg.changed:agent"
    config_path.write_text(json.dumps(config))
    assert "frozen_epoch_contract_mismatch" in _frozen_contract_codes(workspace, epoch_id)

    config["adk_entrypoint"] = "pkg.mod:agent"
    config["mutable_trees"].append(str(tmp_path / "another-tree"))
    config_path.write_text(json.dumps(config))
    assert "frozen_epoch_contract_mismatch" in _frozen_contract_codes(workspace, epoch_id)


@pytest.mark.parametrize("artifact", ["board.jsonl", "scoring.json"])
def test_explicit_epoch_contract_check_rejects_frozen_artifact_edits(
    tmp_path: Path,
    artifact: str,
) -> None:
    workspace, _ = _bootstrap(tmp_path)
    epoch_id = asyncio.run(
        ensure_epoch_for_contract(workspace, auto_epoch=True, aux_call_llm=_aux_llm)
    )
    path = workspace / "epochs" / epoch_id / artifact
    if artifact == "board.jsonl":
        path.write_text(
            json.dumps(
                {
                    "id": "entry_a",
                    "kind": "single_turn",
                    "wall_clock_budget_seconds": 60,
                    "input": "edited frozen input",
                }
            )
            + "\n"
        )
    else:
        path.write_text(json.dumps({"pass_weight": 2.0}))
    assert "frozen_epoch_contract_mismatch" in _frozen_contract_codes(workspace, epoch_id)


def test_explicit_epoch_contract_check_rejects_proposer_edits(tmp_path: Path) -> None:
    workspace, _ = _bootstrap(tmp_path)
    proposer = tmp_path / "proposers" / "p1"
    (proposer / "skills").mkdir(parents=True)
    skill = proposer / "skills" / "a.md"
    skill.write_text(_SKILL)
    _set_proposer_path(workspace, proposer)
    epoch_id = asyncio.run(
        ensure_epoch_for_contract(workspace, auto_epoch=True, aux_call_llm=_aux_llm)
    )

    skill.write_text(
        "---\nname: tighten\ndescription: keep it terse\n---\n\nChanged proposer behavior.\n"
    )
    assert "frozen_epoch_contract_mismatch" in _frozen_contract_codes(workspace, epoch_id)


def test_frozen_contract_validator_aggregates_adapter_factory_failures(tmp_path: Path) -> None:
    workspace, _ = _bootstrap(tmp_path)
    epoch_id = asyncio.run(
        ensure_epoch_for_contract(workspace, auto_epoch=True, aux_call_llm=_aux_llm)
    )
    config_path = workspace / "config.json"
    config = json.loads(config_path.read_text())
    config.pop("adk_entrypoint")
    config["adapter"] = {
        "kind": "import",
        "factory": "tests.test_auto_epoch:_raise_adapter_factory",
    }
    config_path.write_text(json.dumps(config))
    assert _frozen_contract_codes(workspace, epoch_id) == {"frozen_epoch_contract_unreadable"}


def test_generic_adapter_configuration_changes_auto_roll_the_epoch(tmp_path: Path) -> None:
    workspace, _ = _bootstrap(tmp_path)
    config_path = workspace / "config.json"
    config = json.loads(config_path.read_text())
    config.pop("adk_entrypoint")
    config["adapter"] = {
        "kind": "import",
        "factory": "tests.test_epoch_contract:_make_contract_adapter",
        "args": ["one", []],
    }
    config_path.write_text(json.dumps(config))
    first = asyncio.run(
        ensure_epoch_for_contract(workspace, auto_epoch=True, aux_call_llm=_aux_llm)
    )

    config["adapter"]["args"] = ["two", ["goldfive"]]
    config_path.write_text(json.dumps(config))
    second = asyncio.run(
        ensure_epoch_for_contract(workspace, auto_epoch=True, aux_call_llm=_aux_llm)
    )
    assert second != first


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
    files["brief"].write_text("# changed proposer brief\n")

    import zicato.evolve.epoching as epoching
    import zicato.orchestrator as orch

    async def _boom(*args: object, **kwargs: object) -> str:
        raise AssertionError("ensure_epoch_for_contract should be skipped")

    original = epoching.ensure_epoch_for_contract
    epoching.ensure_epoch_for_contract = _boom  # type: ignore[assignment]
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
        epoching.ensure_epoch_for_contract = original  # type: ignore[assignment]

    # Still exactly one epoch — no roll.
    assert len(list_epochs(workspace)) == 1
