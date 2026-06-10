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


def _distinct_proposer_response(core_idea: str, new_word: str) -> str:
    """A schema-valid response targeting the ``greeting`` marker, made distinct.

    The multi-challenger field's diversity constraint
    (FUNCTIONALITY-RECOMMENDATIONS.md §4.3) soft-rejects a challenger that
    duplicates an in-flight sibling (same ``modulating`` id-set + core idea).
    A real field is a slate of *distinct* experiments, so a field test must
    feed each challenger a distinct ``core_idea`` (and a distinct replacement
    word) — two byte-identical proposals would, correctly, collapse to one.
    """
    return json.dumps(
        {
            "hypothesis": {
                "core_idea": core_idea,
                "modulating": ["greeting"],
                "why": "exercising the multi-challenger field path",
                "expected_drift_movements": [
                    {"kind": "off_topic", "direction": "decrease", "magnitude": "small"}
                ],
                "expected_pass_rate_delta": "+0.0 to +0.1",
                "risks": "harmless",
            },
            "patches": [
                {
                    "mutation_id": "greeting",
                    "op": "replace",
                    "new_content": f'"{new_word}"',
                    "rationale": "different greeting word",
                }
            ],
        }
    )


def _distinct_field_responses(n: int) -> list[str]:
    """``n`` distinct schema-valid proposer responses for an ``n``-wide field.

    Each carries a unique ``core_idea`` so none is soft-rejected by the
    field-diversity constraint; together they form a genuinely diverse field
    of ``n`` challengers, which is what these end-to-end tests intend.
    """
    return [
        _distinct_proposer_response(f"swap the greeting string (variant {i})", f"word{i}")
        for i in range(n)
    ]


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
                # Hand-built directory-backend snapshot layout below; pin the
                # directory backend so the git default does not look for git
                # tags this fixture never writes.
                "storage_backend": "directory",
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
            auxiliary_call_llm=_make_aux_responder(_distinct_field_responses(2)),
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
    # The funnel field (`entries`) is seeded for the non-gauntlet path —
    # one row per competitor — so the dashboard's per-entry funnel rung
    # renders. The racing/multi-challenger publish historically left this
    # empty (#8), so GET /api/active-tournament returned entries: [].
    assert active.entries, "non-gauntlet envelope should seed per-entry funnel rows"
    entry_ids = {e.entry_id for e in active.entries}
    assert entry_ids == {"v0", "v1", "v2"}
    # `side` carries the competitor role for non-gauntlet structures.
    assert {e.side for e in active.entries} == {"champion", "challenger"}

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
            auxiliary_call_llm=_make_aux_responder(_distinct_field_responses(2)),
        )
    )

    assert outcome.tournament_decision == "rejected"
    # Issue #10: on a rejection the round-summary scalars come from the gate's
    # CROWNING matchup (champion vs the leading challenger) — NOT the child=parent
    # fallback that reported delta 0.0 while the gate's reason cited a real
    # regression. So parent != child and the delta is the actual (non-zero,
    # positive = worse) regression, consistent with rejection_reason.
    assert outcome.delta_scalar != 0.0, "rejection delta must be the real regression, not 0.0"
    assert outcome.delta_scalar > 0.0, "the leading challenger regressed (loss rose)"
    assert outcome.child_scalar > outcome.parent_scalar, "challenger loss is above the champion's"
    assert outcome.rejection_reason, "a rejection carries the gate's reason"
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
            auxiliary_call_llm=_make_aux_responder(_distinct_field_responses(2)),
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
            auxiliary_call_llm=_make_aux_responder(_distinct_field_responses(2)),
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
            auxiliary_call_llm=_make_aux_responder(_distinct_field_responses(2)),
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


def _findability_validation_response() -> str:
    """A schema-INVALID response: predicts an undeclared board judge.

    ``drift:file_findability`` is neither a built-in goldfive drift kind nor
    a declared board judge, so the structured parser rejects it with the
    exact ``file_findability``-style validation message the operator cited as
    invisible on the dashboard. Used to prove the per-attempt reason is
    captured on ``field_status`` and reaches the dashboard.
    """
    return json.dumps(
        {
            "hypothesis": {
                "core_idea": "improve file findability",
                "modulating": ["greeting"],
                "why": "exercising the metric-movement validation reject path.",
                "expected_metric_movements": [
                    {
                        "metric_name": "drift:file_findability",
                        "direction": "decrease",
                        "magnitude": "small",
                    }
                ],
                "expected_pass_rate_delta": "+0.0 to +0.1",
                "risks": "none",
            },
            "patches": [
                {
                    "mutation_id": "greeting",
                    "op": "replace",
                    "new_content": '"world"',
                    "rationale": "different greeting word",
                }
            ],
        }
    )


def test_field_status_carries_per_attempt_validation_reason(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A challenger rejected on a metric-movement validation error records the
    SPECIFIC reason (the ``file_findability`` validation message) AND the full
    per-attempt list + hypothesis, so the dashboard proposing tracker can show
    WHY a slot was rejected — not just that it was."""
    workspace, epoch_id = _bootstrap_swiss_workspace(tmp_path, field_size=2, rounds_n=1)
    _install_stub_adapter_factory(monkeypatch)
    _install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 0.5},
        canned_pass_by_gen={"v0": True, "v1": True},
    )

    from zicato.orchestrator import evolve_once

    # Challenger v1 applies; challenger v2 fails BOTH attempts on the same
    # validation error (zero retries → one attempt each here would still
    # reject, but we give 1 retry to prove attempt_reasons accumulates).
    asyncio.run(
        evolve_once(
            workspace_root=workspace,
            epoch_id=epoch_id,
            harness_call_llm=_harness_call_llm,
            auxiliary_call_llm=_make_aux_responder(
                [
                    _valid_proposer_response(),  # v1 attempt 1 → applied
                    _findability_validation_response(),  # v2 attempt 1 → reject
                    _findability_validation_response(),  # v2 attempt 2 → reject
                ]
            ),
            max_proposer_retries=1,
        )
    )

    from zicato.runtime.state import read_active_tournament

    active = read_active_tournament(workspace)
    assert active is not None
    by_gen = {f["generation_id"]: f for f in active.field_status}
    assert set(by_gen) == {"v1", "v2"}

    # The applied challenger carries its hypothesis summary.
    assert by_gen["v1"]["status"] == "applied"
    assert by_gen["v1"]["hypothesis"] == "swap the greeting string"
    assert by_gen["v1"]["attempt_reasons"] == []

    # The rejected challenger carries the SPECIFIC validation message —
    # both in the condensed `reason` and the full per-attempt list.
    v2 = by_gen["v2"]
    assert v2["status"] == "rejected"
    assert "file_findability" in v2["reason"]
    assert v2["attempts"] == 2
    assert len(v2["attempt_reasons"]) == 2
    assert all("file_findability" in r for r in v2["attempt_reasons"])


def test_field_status_publishes_proposing_phase_live(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The orchestrator publishes a ``phase="proposing"`` envelope as each
    challenger slot ENTERS the field (status ``"proposing"``) — before the
    whole batch is minted — so the dashboard reads the proposal phase live,
    not only once it settles. We assert the on_status callback receives a
    ``"proposing"`` record per slot ahead of its terminal record."""
    workspace, epoch_id = _bootstrap_swiss_workspace(tmp_path, field_size=2, rounds_n=1)
    _install_stub_adapter_factory(monkeypatch)
    _install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 0.5, "v2": 1.5},
        canned_pass_by_gen={"v0": True, "v1": True, "v2": True},
    )

    seen: list[tuple[str, str]] = []
    import zicato.orchestrator as orch

    real = orch._propose_and_apply_challenger

    async def _wrapped(*args: object, **kwargs: object) -> object:
        on_status = kwargs.get("on_status")

        def _tap(record: dict) -> None:
            seen.append((str(record.get("generation_id")), str(record.get("status"))))
            if on_status is not None:
                on_status(record)  # type: ignore[operator]

        kwargs["on_status"] = _tap
        return await real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(orch, "_propose_and_apply_challenger", _wrapped)

    from zicato.orchestrator import evolve_once

    asyncio.run(
        evolve_once(
            workspace_root=workspace,
            epoch_id=epoch_id,
            harness_call_llm=_harness_call_llm,
            auxiliary_call_llm=_make_aux_responder(_distinct_field_responses(2)),
        )
    )

    # Each slot announces "proposing" before it settles to "applied".
    assert ("v1", "proposing") in seen
    assert ("v2", "proposing") in seen
    v1_prop = seen.index(("v1", "proposing"))
    v1_applied = seen.index(("v1", "applied"))
    assert v1_prop < v1_applied


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


def test_applied_inflight_challenger_lineage_reports_pending_then_settles(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An applied-but-unresolved challenger is PENDING on /api/lineage at
    creation, and carries the resolved bool after the round settles.

    Regression for the broken live-racing render: the creation-time
    ``append_to_lineage`` wrote ``promoted=False`` (a dead branch), so an
    in-flight challenger rendered as "rejected" while it was still racing.
    The fix lands it ``promoted=null`` (pending) at creation; the
    settle-time append flips it to the crowned/rejected bool. We assert the
    PENDING state by tapping the orchestrator just after the field is
    applied (before resolution), then the SETTLED state from the final
    record. The dashboard's ``build_lineage_view`` is the /api/lineage
    source, so we assert through it.
    """
    from zicato.dashboard.state_reader import WorkspacePaths, build_lineage_view

    workspace, epoch_id = _bootstrap_swiss_workspace(tmp_path, field_size=2, rounds_n=1)
    _install_stub_adapter_factory(monkeypatch)
    _install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 0.5, "v2": 1.5},
        canned_pass_by_gen={"v0": True, "v1": True, "v2": True},
    )

    import zicato.selection as sel

    # Tap the strategy driver (imported locally in the orchestrator from
    # ``zicato.selection``): by the time it requests the field the
    # challengers have been applied to lineage (creation-time append) but
    # NOT resolved — exactly the in-flight window the dashboard renders.
    pending_snapshot: dict[str, object] = {}
    real_resolve = sel.resolve_tournament

    async def _tap_resolve(strategy, **kwargs):  # type: ignore[no-untyped-def]
        request_field = kwargs["request_field"]

        async def _wrapped_request(n: int):  # type: ignore[no-untyped-def]
            field = await request_field(n)
            # The field is now applied + appended to lineage; capture the
            # in-flight lineage view before any matchup resolves.
            view = build_lineage_view(WorkspacePaths(workspace))
            pending_snapshot.update(
                {node["generation_id"]: node["promoted"] for node in view["generations"]}
            )
            return field

        kwargs["request_field"] = _wrapped_request
        return await real_resolve(strategy, **kwargs)

    monkeypatch.setattr(sel, "resolve_tournament", _tap_resolve)

    from zicato.orchestrator import evolve_once

    outcome = asyncio.run(
        evolve_once(
            workspace_root=workspace,
            epoch_id=epoch_id,
            harness_call_llm=_harness_call_llm,
            auxiliary_call_llm=_make_aux_responder(_distinct_field_responses(2)),
        )
    )

    # Mid-flight: BOTH applied challengers reported promoted=None (pending),
    # NOT False (which the frontend renders as a rejected dead branch).
    assert pending_snapshot.get("v1") is None, "in-flight v1 must be pending, not rejected"
    assert pending_snapshot.get("v2") is None, "in-flight v2 must be pending, not rejected"

    # Settled: the resolved bool lands — crowned True, dead branch False.
    crowned = outcome.proposed_generation_id
    dead = "v2" if crowned == "v1" else "v1"
    settled = {
        n["generation_id"]: n["promoted"]
        for n in build_lineage_view(WorkspacePaths(workspace))["generations"]
    }
    assert settled[crowned] is True, "the crowned challenger settles promoted=True"
    assert settled[dead] is False, "the cut challenger settles promoted=False (dead branch)"


def test_field_entries_seeds_one_row_per_competitor() -> None:
    """`_field_entries` builds the funnel field for the non-gauntlet path.

    Regression for #8: the racing / multi-challenger live-publish path set
    competitors / rounds / standings but NOT `entries`, so GET
    /api/active-tournament returned `entries: []` and the live funnel could
    not render the per-entry rung. The helper now emits one row per
    competitor, deriving status + loss_summary from live standings when
    present and falling back to `queued` before any standings exist.
    """
    from zicato.orchestrator import _field_entries

    competitors = [
        {"generation_id": "v0", "seed": 1, "role": "champion"},
        {"generation_id": "v1", "seed": 2, "role": "challenger"},
        {"generation_id": "v2", "seed": 3, "role": "challenger"},
    ]

    # Pre-schedule (no standings): every row queued, side = role.
    pre = _field_entries(competitors)
    assert [e.entry_id for e in pre] == ["v0", "v1", "v2"]
    assert {e.side for e in pre} == {"champion", "challenger"}
    assert {e.status for e in pre} == {"queued"}

    # Live standings drive status + loss_summary.
    standings = [
        {"generation_id": "v0", "status": "eliminated", "scalar": 2.0, "role": "champion"},
        {"generation_id": "v1", "status": "champion", "scalar": 0.5, "role": "challenger"},
        {"generation_id": "v2", "status": "competing", "scalar": 1.5, "role": "challenger"},
    ]
    live = {e.entry_id: e for e in _field_entries(competitors, standings)}
    assert live["v0"].status == "completed"
    assert live["v1"].status == "completed"
    assert live["v2"].status == "running"
    assert live["v1"].loss_summary["scalar"] == 0.5
