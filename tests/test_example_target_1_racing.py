"""End-to-end mock-harness test for the target_1_presentation example
run under the NON-GAUNTLET ``racing`` tournament structure.

This is the runnable counterpart to the example's gauntlet smoke recipe
(``examples/zicato_examples/target_1_presentation/RUN.md``): it drives the
*real* presentation example — its annotated ``agent/`` tree, its
``board.jsonl``, its ``scoring.racing.json`` contract, and its
``mocks.aux_llm`` proposer — through ``evolve_once`` under the racing
(successive-halving) strategy, with NO live LLM.

It mirrors ``tests/test_orchestrator_multi_challenger.py`` (the synthetic
Swiss field test) but, instead of a hand-built one-marker stub snapshot
and a canned ``_valid_proposer_response``, it:

* copies the vendored example ``agent/`` tree into the v0 snapshot so the
  *real* ``coordinator_instruction`` / ``researcher_instruction`` mutation
  markers are enumerated and the example's proposer patches actually
  apply;
* loads the example's ``scoring.racing.json`` so the frozen epoch contract
  carries the racing ``tournament`` block (field_size=4, eta=2, board
  slices over the example board ids);
* uses the example's real ``mocks.aux_llm`` as the proposer/aux callable
  (it rotates ``researcher_instruction`` / ``coordinator_instruction``
  patches across the four challengers in the field).

The per-run harness (the ADK agent's inner LLM + the loss reducer) is
mocked exactly as the orchestrator-test suite mocks it — the L3
subprocess worker cannot see in-process harness mocks, so canned
per-generation losses stand in. That is the same fidelity the existing
multi-challenger end-to-end test runs at; here the *contract* (board +
scoring + agent tree + proposer) is the real example.

The test asserts the full multi-challenger racing path executes:
N challengers proposed + applied, racing rungs/cuts recorded, a champion
decision, and the persisted ``ActiveTournament`` envelope + per-match
``OutcomeRecord`` audit.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import pytest

# Reuse the fully-mocked harness from the gauntlet orchestrator tests.
# ``zicato_examples`` is resolved through the installed examples package so
# the test is independent of where the examples distribution lives on disk.
import zicato_examples.target_1_presentation as _t1_pkg
from tests.test_orchestrator import (
    _harness_call_llm,
    _install_stub_adapter_factory,
    _install_telemetry_stubs,
)
from zicato.epoch.lifecycle import _scoring_from_dict, new_epoch
from zicato_examples.target_1_presentation import mocks as _t1_mocks

EXAMPLE_DIR = Path(_t1_pkg.__file__).resolve().parent
AGENT_DIR = EXAMPLE_DIR / "agent"
BOARD_PATH = EXAMPLE_DIR / "board.jsonl"
BRIEF_PATH = EXAMPLE_DIR / "rubric.md"
RACING_SCORING_PATH = EXAMPLE_DIR / "scoring.racing.json"

# The four challenger ids the racing field mints (v1..v4) off the v0
# champion. Distinct canned losses make the racing cuts deterministic.
_CHALLENGER_IDS = ("v1", "v2", "v3", "v4")


def _make_example_aux_responder() -> object:
    """Return a fresh async aux callable backed by the example's mock.

    The example's :func:`mocks.aux_llm` rotates proposer payloads across
    rounds; we reset its module-level round counter first so the field
    starts from challenger 0 regardless of test-ordering side effects.
    """
    _t1_mocks._AUX_STATE["proposer_round"] = 0

    async def _aux(system: str, user: str, model: str) -> str:
        reply: str = await _t1_mocks.aux_llm(system, user, model)
        return reply

    return _aux


def _bootstrap_racing_workspace(tmp_path: Path) -> tuple[Path, str]:
    """Create a workspace + a racing epoch + a v0 snapshot of the example tree.

    Mirrors ``test_orchestrator_multi_challenger._bootstrap_swiss_workspace``
    but freezes the example's ``scoring.racing.json`` contract and seeds
    v0 with a copy of the *real* annotated ``agent/`` tree (so the example's
    proposer patches resolve against real mutation markers).
    """
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    (workspace / "config.json").write_text(
        json.dumps(
            {
                "instance_id": "test",
                "created_at": "2026-05-31T00:00:00Z",
                "adapter": {"kind": "stub"},
            }
        )
    )

    weights = _scoring_from_dict(json.loads(RACING_SCORING_PATH.read_text()))
    assert weights.tournament_structure.structure == "racing"
    assert weights.tournament_structure.params["field_size"] == 4

    cfg = new_epoch(
        workspace,
        name="t1-racing",
        board_source=BOARD_PATH,
        brief_source=BRIEF_PATH,
        weights=weights,
        auto_close_previous=False,
    )

    # v0 snapshot == a copy of the vendored example agent tree. The stub
    # adapter has no mutable_subpaths, so the orchestrator enumerates
    # markers across the whole snapshot — i.e. the real coordinator /
    # researcher / writer instructions and tool descriptions.
    v0_dir = workspace / "epochs" / cfg.id / "generations" / "v0"
    snap_agent = v0_dir / "snapshot" / "agent"
    snap_agent.parent.mkdir(parents=True)
    shutil.copytree(AGENT_DIR, snap_agent)

    # Pin the promoted head to v0 (the production seeding path writes this;
    # a hand-built v0 must too, else a rejected round's dir-scan fallback
    # resolves to the highest vN dir).
    (workspace / "epochs" / cfg.id / "current_generation").write_text("v0\n")
    return workspace, cfg.id


def test_presentation_racing_field_runs_end_to_end_and_promotes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The example runs under racing: a 4-challenger field is proposed +
    applied off v0, the racing rungs cut the field on board slices, the
    survivor clears the full-board champion gate, and the live
    ActiveTournament envelope + per-challenger OutcomeRecord audit persist."""
    workspace, epoch_id = _bootstrap_racing_workspace(tmp_path)
    _install_stub_adapter_factory(monkeypatch)
    # Strictly-descending challenger losses: v1 is the best arm and survives
    # every rung, then beats champion v0 on the full board. v4 is worst and
    # dies in rung 0. The cuts are by rank (best-arm identification), the
    # final crowning is the unchanged promote gate.
    _install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 0.4, "v2": 0.8, "v3": 1.2, "v4": 1.6},
        canned_pass_by_gen={gid: True for gid in ("v0", *_CHALLENGER_IDS)},
    )

    from zicato.orchestrator import evolve_once

    outcome = asyncio.run(
        evolve_once(
            workspace_root=workspace,
            epoch_id=epoch_id,
            harness_call_llm=_harness_call_llm,
            auxiliary_call_llm=_make_example_aux_responder(),
        )
    )

    # --- A challenger from the field was crowned over the champion.
    assert outcome.tournament_decision == "promoted"
    assert outcome.parent_generation_id == "v0"
    crowned = outcome.proposed_generation_id
    assert crowned == "v1", "the lowest-loss arm should survive the rungs and clear the gate"
    assert outcome.child_scalar < outcome.parent_scalar

    gens = workspace / "epochs" / epoch_id / "generations"

    # --- All four challengers were proposed + applied as real children of
    # v0, each carrying a snapshot of the patched agent tree. The example's
    # proposer rotates researcher_instruction / coordinator_instruction;
    # every applied snapshot is a real, validator-surviving edit.
    for gid in _CHALLENGER_IDS:
        gdir = gens / gid
        assert (gdir / "experiment.json").exists(), gid
        assert (gdir / "snapshot" / "agent" / "agent.py").exists(), gid

    # --- The crowned challenger carries a promoted outcome under the racing
    # structure with a non-empty match_record; the dead branches carry
    # rejected outcomes. Every challenger's outcome is stamped "racing".
    for gid in _CHALLENGER_IDS:
        oc = json.loads((gens / gid / "experiment.json").read_text())["outcome"]
        assert oc["structure"] == "racing", gid
        if gid == crowned:
            assert oc["tournament_decision"] == "promoted"
            assert oc["match_record"], "crowned generation should carry a match audit"
        else:
            assert oc["tournament_decision"] == "rejected", gid

    # --- current_generation advanced to the crowned challenger only.
    marker = workspace / "epochs" / epoch_id / "current_generation"
    assert marker.read_text().strip() == crowned

    # --- Lineage records every challenger as a child of v0; crowned promoted.
    lineage = json.loads((workspace / "lineage.json").read_text())
    gens_nodes: list[dict[str, object]] = []
    for ep in lineage.get("epochs", []):
        if ep.get("id") == epoch_id:
            gens_nodes = ep.get("generations", [])
    by_id = {n["id"]: n for n in gens_nodes}
    for gid in _CHALLENGER_IDS:
        assert by_id[gid]["parent_id"] == "v0", gid
        assert by_id[gid]["promoted"] is (gid == crowned), gid

    # --- The live ActiveTournament envelope persisted with the racing
    # structure + the full competitor field + the rung records (the
    # successive-halving ladder) + final standings.
    from zicato.runtime.state import read_active_tournament

    active = read_active_tournament(workspace)
    assert active is not None
    assert active.structure == "racing"
    comp_ids = {c["generation_id"] for c in active.competitors}
    assert comp_ids == {"v0", *_CHALLENGER_IDS}
    assert active.rounds, "settled racing envelope should carry the rung records"
    standings_ids = {s["generation_id"] for s in active.standings}
    assert standings_ids == {"v0", *_CHALLENGER_IDS}

    # --- The rungs actually cut the field: at least one rung records a
    # non-empty `cut`, proving successive halving ran (not a single
    # full-board final). The match audit names a racing rung match id.
    all_cuts: list[str] = []
    rung_match_ids: list[str] = []
    for rnd in active.rounds:
        for m in rnd.get("matches", []):
            rung_match_ids.append(str(m.get("match_id", "")))
            all_cuts.extend(m.get("cut", []) or [])
    assert any(mid.startswith("rung") for mid in rung_match_ids), rung_match_ids
    assert all_cuts, "successive halving should eliminate at least one arm at a rung"
    assert "v4" in all_cuts, "the worst arm should be cut in an early rung"

    # --- The crowning duel is the full-board champion-gate against v0.
    crowned_oc = json.loads((gens / crowned / "experiment.json").read_text())["outcome"]
    opponents = {m["opponent"] for m in crowned_oc["match_record"]}
    assert "v0" in opponents, "the crowned arm's audit must include the champion-gate duel"


def test_presentation_racing_field_rejects_when_no_arm_beats_champion(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When the racing survivor cannot clear the full-board champion gate,
    the champion stands and every challenger is a dead branch — the example
    contract's promote_margin gate is the unchanged final arbiter."""
    workspace, epoch_id = _bootstrap_racing_workspace(tmp_path)
    _install_stub_adapter_factory(monkeypatch)
    # Every challenger regresses vs the champion (higher loss), so even the
    # racing survivor cannot clear the champion gate on the full board.
    _install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 0.2, "v1": 1.0, "v2": 1.4, "v3": 1.8, "v4": 2.2},
        canned_pass_by_gen={gid: True for gid in ("v0", *_CHALLENGER_IDS)},
    )

    from zicato.orchestrator import evolve_once

    outcome = asyncio.run(
        evolve_once(
            workspace_root=workspace,
            epoch_id=epoch_id,
            harness_call_llm=_harness_call_llm,
            auxiliary_call_llm=_make_example_aux_responder(),
        )
    )

    assert outcome.tournament_decision == "rejected"

    # Champion stands — the promoted head is still v0 (no marker advance).
    from zicato.orchestrator import _resolve_current_generation

    assert _resolve_current_generation(workspace, epoch_id) == "v0"

    gens = workspace / "epochs" / epoch_id / "generations"
    for gid in _CHALLENGER_IDS:
        oc = json.loads((gens / gid / "experiment.json").read_text())["outcome"]
        assert oc["tournament_decision"] == "rejected", gid
        assert oc["structure"] == "racing", gid
