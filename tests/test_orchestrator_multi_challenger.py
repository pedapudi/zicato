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


def test_fast_swiss_reuses_cached_champion(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Fast mode composes with SWISS exactly as with racing/gauntlet.

    With the champion's per-board ``loss.json`` cached, a fast Swiss round
    runs only the challengers — the champion side is never executed — and
    still crowns the strongest challenger. The resolved champion-eval mode
    is recorded in the journal. This proves the runtime fast knob is
    structure-agnostic (it threads through the multi-challenger path for
    every structure, not just racing)."""
    # Reuse the disk-backed caching telemetry stub + champion pre-seed
    # helper from the racing end-to-end test — the only fast-mode harness
    # difference vs the default stubs is that runs persist their loss.json
    # and the cache resolver can read them back.
    from tests.test_example_target_1_racing import (
        _install_caching_telemetry_stubs,
        _preseed_champion_cache,
    )

    workspace, epoch_id = _bootstrap_swiss_workspace(tmp_path, field_size=2, rounds_n=1)
    _install_stub_adapter_factory(monkeypatch)
    # Pre-seed the champion (v0) full-board cache BEFORE installing the
    # reducer stub.
    _preseed_champion_cache(workspace, epoch_id, champion_id="v0", drift_loss=2.0, pass_fail=True)
    champion_runs: list[str] = []
    _install_caching_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 0.5, "v2": 1.5},
        canned_pass_by_gen={"v0": True, "v1": True, "v2": True},
        champion_run_log=champion_runs,
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
            fast_mode=True,
        )
    )

    # The champion (v0) was NOT executed — the cached per-board scalars
    # stood in for every Swiss matchup it appears in.
    assert "v0" not in champion_runs, "fast swiss must not re-run the cached champion"
    assert champion_runs, "the challengers still ran"
    assert outcome.tournament_decision == "promoted"
    crowned = outcome.proposed_generation_id
    gens = workspace / "epochs" / epoch_id / "generations"
    crowned_oc = json.loads((gens / crowned / "experiment.json").read_text())["outcome"]
    assert crowned_oc["champion_eval_mode"] == "fast"


def test_swiss_runs_each_gen_entry_at_most_once_over_multiple_rounds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A Swiss field over ``rounds_n > 1`` runs each (gen, entry) at most once.

    A 2-challenger Swiss with ``rounds_n=2`` schedules MULTIPLE round-robin
    rounds plus the champion-gate — every competitor appears in several
    pairings. The cache-first board-unit runner executes each DISTINCT
    ``(gen, entry)`` unit exactly once; every later pairing/round/the gate
    resolves its competitors from the persisted ``loss.json``. With a
    single-entry board the distinct unit count is exactly the number of
    competitors (champion + challengers), NOT pairings x sides.
    """
    from collections import Counter

    from tests.test_example_target_1_racing import _install_caching_telemetry_stubs

    workspace, epoch_id = _bootstrap_swiss_workspace(tmp_path, field_size=2, rounds_n=2)
    _install_stub_adapter_factory(monkeypatch)
    # The caching stub persists each run's loss.json and logs every
    # actually-executed generation — a cache HIT never reaches it, so the
    # log IS the board-unit execution count.
    run_log: list[str] = []
    _install_caching_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 0.5, "v2": 1.5},
        canned_pass_by_gen={"v0": True, "v1": True, "v2": True},
        champion_run_log=run_log,
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
            # fast (the default) is the always-on cache; assert it explicitly.
            fast_mode=True,
        )
    )

    # The board has ONE entry; the field is v0 + v1 + v2 = 3 competitors. A
    # naive per-pairing-per-round count would be far higher (multiple
    # round-robin rounds + the gate, two sides each). Cache-first collapses
    # it to exactly the distinct competitors, each run ONCE: v0 runs once
    # (no pre-seed, so the first pairing it appears in seeds its cache via
    # degrade-to-full) and is reused thereafter; each challenger runs once
    # across every round + the gate.
    counts = Counter(run_log)
    assert all(c == 1 for c in counts.values()), counts
    assert set(counts) == {"v0", "v1", "v2"}, counts
    assert outcome.tournament_decision == "promoted"


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


def test_field_status_records_applied_challengers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The settled ActiveTournament envelope carries a ``field_status``
    record per challenger the proposer minted, each ``status="applied"``
    with a seed — the proposing-step tracker's live data source."""
    workspace, epoch_id = _bootstrap_swiss_workspace(tmp_path, field_size=2, rounds_n=1)
    _install_stub_adapter_factory(monkeypatch)
    _install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 0.5, "v2": 1.5},
        canned_pass_by_gen={"v0": True, "v1": True, "v2": True},
    )

    from zicato.orchestrator import evolve_once

    asyncio.run(
        evolve_once(
            workspace_root=workspace,
            epoch_id=epoch_id,
            harness_call_llm=_harness_call_llm,
            auxiliary_call_llm=_make_aux_responder(
                [_valid_proposer_response(), _valid_proposer_response()]
            ),
        )
    )

    from zicato.runtime.state import read_active_tournament

    active = read_active_tournament(workspace)
    assert active is not None
    by_gen = {f["generation_id"]: f for f in active.field_status}
    assert set(by_gen) == {"v1", "v2"}
    for gid in ("v1", "v2"):
        assert by_gen[gid]["status"] == "applied"
        assert by_gen[gid]["reason"] == ""
        assert by_gen[gid]["seed"] >= 2
    # Seeds match the competitor seeding (challengers 2, 3 in mint order).
    assert {by_gen["v1"]["seed"], by_gen["v2"]["seed"]} == {2, 3}

    # The structure endpoint surfaces field_status for the current epoch.
    from zicato.dashboard.state_reader import WorkspacePaths, build_tournament_structure

    paths = WorkspacePaths(workspace)
    struct = build_tournament_structure(paths, epoch_id, active.tournament_id)
    struct_by_gen = {f["generation_id"]: f for f in struct["field_status"]}
    assert set(struct_by_gen) == {"v1", "v2"}
    assert all(f["status"] == "applied" for f in struct["field_status"])


def test_field_status_when_all_challengers_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When the proposer cannot mint a single valid challenger, the round
    rejects but still PUBLISHES the proposing-phase envelope with a
    ``field_status`` of rejected entries (+ reason) so the dashboard reads
    "N proposed · 0 applied — all rejected" instead of an idle state."""
    workspace, epoch_id = _bootstrap_swiss_workspace(tmp_path, field_size=2, rounds_n=1)
    _install_stub_adapter_factory(monkeypatch)
    _install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0},
        canned_pass_by_gen={"v0": True},
    )

    from zicato.orchestrator import evolve_once

    # Empty proposer responses exhaust the (zero-retry) budget for every
    # challenger, so the whole field is rejected with a reason.
    outcome = asyncio.run(
        evolve_once(
            workspace_root=workspace,
            epoch_id=epoch_id,
            harness_call_llm=_harness_call_llm,
            auxiliary_call_llm=_make_aux_responder(["", ""]),
            max_proposer_retries=0,
        )
    )

    assert outcome.tournament_decision == "rejected"

    from zicato.runtime.state import read_active_tournament

    active = read_active_tournament(workspace)
    assert active is not None
    assert active.phase == "proposing"
    # Two challengers were attempted; none applied.
    assert len(active.field_status) == 2
    assert all(f["status"] == "rejected" for f in active.field_status)
    assert all(f["reason"] for f in active.field_status), "each rejection carries a reason"
    applied = [f for f in active.field_status if f["status"] == "applied"]
    assert applied == []

    # The structure endpoint surfaces the all-rejected field post-hoc.
    from zicato.dashboard.state_reader import WorkspacePaths, build_tournament_structure

    paths = WorkspacePaths(workspace)
    struct = build_tournament_structure(paths, epoch_id, active.tournament_id)
    assert len(struct["field_status"]) == 2
    assert all(f["status"] == "rejected" for f in struct["field_status"])


def test_field_status_absent_is_empty_and_back_compatible() -> None:
    """An ActiveTournament with no ``field_status`` (old data / gauntlet)
    loads as an empty list and round-trips byte-identically when the key
    is absent from the source dict."""
    from zicato.runtime.state import ActiveTournament

    legacy = {
        "tournament_id": "t1",
        "parent_generation_id": "v0",
        "child_generation_id": "v1",
        "epoch_id": "e1",
        "started_at": "2026-06-01T00:00:00Z",
    }
    t = ActiveTournament.from_dict(legacy)
    assert t.field_status == []
    # Present in to_dict() as an (additive) empty list.
    assert t.to_dict()["field_status"] == []
