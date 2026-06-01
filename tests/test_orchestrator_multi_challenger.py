"""End-to-end tests for the multi-challenger (non-gauntlet) evolve path.

These prove the orchestrator's :func:`evolve_once` drives a real
``field_size > 1`` tournament structure through the SelectionStrategy
against a fully mocked harness:

* a Swiss structure proposes + applies N challengers, schedules the
  strategy's matchups, runs each via the (mocked) board-unit runner +
  unchanged promote gate, advances the champion, records the rejected
  field as dead branches, and persists the ``ActiveTournament`` envelope
  + per-challenger ``OutcomeRecord`` audit + the v3 index columns;
* the gauntlet (``field_size == 1``) path is unchanged — covered by
  ``test_orchestrator.py`` and ``test_orchestrator_selection.py``; here we
  only assert the dispatch does not take the multi path for it.

The harness mock (stub adapter + canned per-generation losses) is reused
from ``test_orchestrator`` so a multi-challenger round resolves entirely
on synthetic losses with no real model / subprocess traffic.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

# Reuse the fully-mocked harness from the gauntlet orchestrator tests.
from tests.test_orchestrator import (
    _harness_call_llm,
    _install_stub_adapter_factory,
    _install_telemetry_stubs,
    _make_aux_responder,
    _valid_proposer_response,
)
from zicato.core.types import ScoringWeights, TournamentStructure
from zicato.epoch.lifecycle import new_epoch


def _bootstrap_swiss_workspace(
    tmp_path: Path, *, field_size: int, rounds_n: int = 1
) -> tuple[Path, str]:
    """Create a workspace + a Swiss epoch + a v0 baseline snapshot.

    Mirrors ``test_orchestrator._bootstrap_workspace`` but stamps a
    non-gauntlet ``tournament_structure`` onto the epoch's frozen
    ``ScoringWeights`` so ``evolve_once`` takes the multi-challenger path.
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

    board_src = tmp_path / "board.jsonl"
    board_src.write_text(
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
    brief_src = tmp_path / "brief.md"
    brief_src.write_text("# Proposer brief\n- Be careful.\n")

    cfg = new_epoch(
        workspace,
        name="swiss-epoch",
        board_source=board_src,
        brief_source=brief_src,
        weights=ScoringWeights(
            promote_margin=0.01,
            tournament_structure=TournamentStructure(
                structure="swiss",
                params={"field_size": field_size, "rounds_n": rounds_n, "replicates": 1},
            ),
        ),
        auto_close_previous=False,
    )

    v0_dir = workspace / "epochs" / cfg.id / "generations" / "v0"
    snap = v0_dir / "snapshot"
    snap.mkdir(parents=True)
    (snap / "agent.py").write_text(
        '"""Stub harness source for tests."""\n'
        "\n"
        '# zicato:mutable id="greeting"\n'
        'GREETING = "hello"\n'
    )
    # Pin the promoted head to v0 — the production seeding path
    # (_ensure_baseline_snapshot) writes this marker; tests that hand-build
    # v0 must do it too so a rejected round leaves the head at v0 (rather
    # than the dir-scan fallback resolving to the highest vN dir).
    (workspace / "epochs" / cfg.id / "current_generation").write_text("v0\n")
    return workspace, cfg.id


def test_swiss_field_runs_end_to_end_and_promotes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A 2-challenger Swiss round proposes+applies the field, runs the
    strategy's matchups, crowns the strongest challenger, records the
    other as a dead branch, and persists the envelope + audit + index."""
    workspace, epoch_id = _bootstrap_swiss_workspace(tmp_path, field_size=2, rounds_n=1)
    _install_stub_adapter_factory(monkeypatch)
    # v1 is the strongest (lowest drift loss) and beats both the champion
    # (v0) and the other challenger (v2), so it should be crowned.
    _install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 0.5, "v2": 1.5},
        canned_pass_by_gen={"v0": True, "v1": True, "v2": True},
    )

    from zicato.orchestrator import evolve_once

    outcome = asyncio.run(
        evolve_once(
            workspace_root=workspace,
            epoch_id=epoch_id,
            harness_call_llm=_harness_call_llm,
            # field_size=2 ⇒ two proposer calls, one per challenger.
            auxiliary_call_llm=_make_aux_responder(
                [_valid_proposer_response(), _valid_proposer_response()]
            ),
        )
    )

    # A challenger from the field was crowned over the champion.
    assert outcome.tournament_decision == "promoted"
    assert outcome.parent_generation_id == "v0"
    crowned = outcome.proposed_generation_id
    dead = "v2" if crowned == "v1" else "v1"
    assert crowned in ("v1", "v2")
    assert outcome.child_scalar < outcome.parent_scalar

    gens = workspace / "epochs" / epoch_id / "generations"

    # Both challengers were proposed + applied as real children of v0.
    for gid in ("v1", "v2"):
        gdir = gens / gid
        assert (gdir / "experiment.json").exists(), gid
        assert (gdir / "snapshot" / "agent.py").exists(), gid

    # The crowned challenger carries a promoted outcome under the swiss
    # structure with a non-empty match_record (the audit trail); the dead
    # branch carries a rejected outcome — both with the structure stamped.
    crowned_outcome = json.loads((gens / crowned / "experiment.json").read_text())["outcome"]
    dead_outcome = json.loads((gens / dead / "experiment.json").read_text())["outcome"]
    assert crowned_outcome["tournament_decision"] == "promoted"
    assert crowned_outcome["structure"] == "swiss"
    assert crowned_outcome["match_record"], "crowned generation should carry a match audit"
    assert dead_outcome["tournament_decision"] == "rejected"
    assert dead_outcome["structure"] == "swiss"

    # current_generation advanced to the crowned challenger only.
    marker = workspace / "epochs" / epoch_id / "current_generation"
    assert marker.read_text().strip() == crowned

    # Lineage records every challenger as a child of the champion; the
    # crowned one is promoted, the dead branch is not.
    lineage = json.loads((workspace / "lineage.json").read_text())
    gens_nodes: list[dict] = []
    for ep in lineage.get("epochs", []):
        if ep.get("id") == epoch_id:
            gens_nodes = ep.get("generations", [])
    by_id = {n["id"]: n for n in gens_nodes}
    assert by_id[crowned]["promoted"] is True
    assert by_id[dead]["promoted"] is False
    assert by_id[crowned]["parent_id"] == "v0"
    assert by_id[dead]["parent_id"] == "v0"

    # The live ActiveTournament envelope persisted with the structure
    # envelope (competitors / rounds / standings) per the data model.
    from zicato.runtime.state import read_active_tournament

    active = read_active_tournament(workspace)
    assert active is not None
    assert active.structure == "swiss"
    comp_ids = {c["generation_id"] for c in active.competitors}
    assert comp_ids == {"v0", "v1", "v2"}
    assert active.rounds, "settled envelope should carry the swiss rounds"
    standings_ids = {s["generation_id"] for s in active.standings}
    assert standings_ids == {"v0", "v1", "v2"}

    # The v3 index columns are populated for the crowned generation.
    db = sqlite3.connect(workspace / "index.db")
    try:
        row = db.execute(
            "SELECT structure, competitors_json, child_generation_id "
            "FROM tournaments WHERE child_generation_id = ?",
            (crowned,),
        ).fetchone()
    finally:
        db.close()
    assert row is not None
    assert row[0] == "swiss"
    assert crowned in json.loads(row[1])

    # Journal carries an entry for both challengers.
    journal = (workspace / "epochs" / epoch_id / "journal.md").read_text()
    assert journal.count("swap the greeting string") >= 2


def test_swiss_field_rejects_when_no_challenger_beats_champion(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When the swiss leader does not clear the champion gate, the
    champion stands and every challenger is a dead branch."""
    workspace, epoch_id = _bootstrap_swiss_workspace(tmp_path, field_size=2, rounds_n=1)
    _install_stub_adapter_factory(monkeypatch)
    # Both challengers regress vs the champion (higher loss), so even the
    # swiss leader cannot clear the champion gate.
    _install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 0.2, "v1": 1.0, "v2": 2.0},
        canned_pass_by_gen={"v0": True, "v1": True, "v2": True},
    )

    from zicato.orchestrator import evolve_once

    outcome = asyncio.run(
        evolve_once(
            workspace_root=workspace,
            epoch_id=epoch_id,
            harness_call_llm=_harness_call_llm,
            auxiliary_call_llm=_make_aux_responder(
                [_valid_proposer_response(), _valid_proposer_response()]
            ),
        )
    )

    assert outcome.tournament_decision == "rejected"
    # Champion stands — the promoted head is still v0 (no marker advance).
    from zicato.orchestrator import _resolve_current_generation

    assert _resolve_current_generation(workspace, epoch_id) == "v0"

    gens = workspace / "epochs" / epoch_id / "generations"
    for gid in ("v1", "v2"):
        oc = json.loads((gens / gid / "experiment.json").read_text())["outcome"]
        assert oc["tournament_decision"] == "rejected"
        assert oc["structure"] == "swiss"


def test_gauntlet_does_not_take_multi_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A gauntlet epoch (field_size == 1) keeps the single-challenger path
    — proving the dispatch only diverts when the field is wider."""
    from tests.test_orchestrator import _bootstrap_workspace

    workspace, epoch_id = _bootstrap_workspace(tmp_path)
    _install_stub_adapter_factory(monkeypatch)
    _install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 1.0},
        canned_pass_by_gen={"v0": True, "v1": True},
    )

    from zicato.orchestrator import evolve_once

    # A single proposer response suffices iff the gauntlet path (one
    # challenger) ran; the multi path would request a second and the
    # responder would raise on exhaustion.
    outcome = asyncio.run(
        evolve_once(
            workspace_root=workspace,
            epoch_id=epoch_id,
            harness_call_llm=_harness_call_llm,
            auxiliary_call_llm=_make_aux_responder([_valid_proposer_response()]),
        )
    )
    assert outcome.tournament_decision == "promoted"
    assert outcome.proposed_generation_id == "v1"
    # Gauntlet leaves only v0 + v1 — no second challenger was proposed.
    gens = workspace / "epochs" / epoch_id / "generations"
    assert not (gens / "v2").exists()
