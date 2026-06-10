"""Regression tests for in-flight round observability (issue #16).

The bug: during a multi-round non-gauntlet ``evolve``, a round's
bookkeeping was committed ONLY at round settle. Mid-round on a live run,
a new round's challengers existed on disk (with accumulating ``loss.json``)
but were absent from EVERY queryable store:

* ``lineage.json`` held only the last settled round's generations, so a
  consumer grouping by ``round_index`` folded the new challengers onto the
  previous round's bracket (round mis-attribution);
* each new challenger's ``experiment.json`` carried the proposer's default
  ``round_index=0`` rather than its real birth round, so the dashboard's
  round-grouping (which treats ``round_index`` as the authoritative birth
  round) mis-attributed every later round to round 0;
* no durable ``tournaments/field-*.json`` record existed for the in-flight
  round, so the index / ``zicato reindex`` / any external tool saw only the
  last settled round.

The holistic fix makes every queryable store reflect the in-flight round
continuously, with an explicit ``in_progress`` → ``settled`` lifecycle:

1. each challenger's ``round_index`` is stamped onto its ``experiment.json``
   and appended to ``lineage.json`` AT CREATION (not at settle);
2. the durable field-tournament envelope is OPENED in ``in_progress`` state
   at round start and FINALISED at settle.

These tests encode the issue's precise scenario — a ``field_size=4``
``single_elim`` epoch over two outer evolve rounds, where round 0 mints
v1..v4 and round 1 mints v5..v8 — and assert the mid-round window is
correct across every store, not just the dashboard.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

# Reuse the fully-mocked harness from the gauntlet orchestrator tests.
from tests.test_orchestrator import (
    _harness_call_llm,
    _install_stub_adapter_factory,
    _install_telemetry_stubs,
)
from zicato.core.types import ScoringWeights, TournamentStructure
from zicato.epoch.lifecycle import new_epoch


def _distinct_proposer_response(core_idea: str, new_word: str) -> str:
    """A valid proposer response with a distinct ``core_idea`` + replacement.

    The field-diversity constraint (FUNCTIONALITY-RECOMMENDATIONS.md §4.3)
    soft-rejects a challenger that duplicates an in-flight sibling, so a field
    of N distinct challengers needs N distinct proposals — these tests intend
    a full field (v1..v4 etc.), so each proposer call gets a unique idea.
    """
    return json.dumps(
        {
            "hypothesis": {
                "core_idea": core_idea,
                "modulating": ["greeting"],
                "why": "exercising the in-flight observability field path",
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


def _infinite_proposer_responder() -> Any:
    """An aux callable that returns a DISTINCT valid proposer response per call.

    These tests run several outer evolve rounds, each minting a field of
    challengers and ending in the epoch analyzer — so the number of aux
    calls is not easily counted ahead of time (proposer retries + the
    per-round analyzer all draw on the aux LLM). A fixed-length responder
    would run dry and narrow a field, perturbing the birth-round scenario.

    Every PROPOSER call (the one carrying the mutation manifest) gets a
    UNIQUE ``core_idea`` so the field-diversity constraint keeps the whole
    field — two byte-identical proposals would, correctly, collapse. A
    non-proposer (analyzer) call gets a benign placeholder; the analyzer
    tolerates a proposer-shaped reply (it falls back to placeholder prose),
    so this never aborts a round.
    """
    counter = {"n": 0}

    async def _aux(system: str, user: str, model: str) -> str:
        del system, model
        if "## Mutation points" not in user:
            # Non-proposer (analyzer) call — a benign placeholder.
            return "report placeholder"
        i = counter["n"]
        counter["n"] = i + 1
        return _distinct_proposer_response(
            core_idea=f"swap the greeting string (variant {i})",
            new_word=f"word{i}",
        )

    return _aux


def _bootstrap_single_elim_workspace(tmp_path: Path, *, field_size: int) -> tuple[Path, str]:
    """Create a workspace + a single_elim epoch + a v0 baseline snapshot.

    Mirrors ``test_orchestrator_multi_challenger._bootstrap_swiss_workspace``
    but stamps a ``single_elim`` structure so ``evolve_once`` takes the
    multi-challenger path with a real bracket.
    """
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    (workspace / "config.json").write_text(
        json.dumps(
            {
                "instance_id": "test",
                "created_at": "2026-06-06T00:00:00Z",
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
        name="elim-epoch",
        board_source=board_src,
        brief_source=brief_src,
        weights=ScoringWeights(
            promote_margin=0.01,
            tournament_structure=TournamentStructure(
                structure="single_elim",
                params={"field_size": field_size, "replicates": 1},
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
    (workspace / "epochs" / cfg.id / "current_generation").write_text("v0\n")
    return workspace, cfg.id


def _lineage_gens(workspace: Path, epoch_id: str) -> dict[str, dict[str, Any]]:
    """Return ``lineage.json``'s generation nodes for ``epoch_id`` keyed by id."""
    lineage = json.loads((workspace / "lineage.json").read_text())
    for ep in lineage.get("epochs", []):
        if ep.get("id") == epoch_id:
            return {g["id"]: g for g in ep.get("generations", [])}
    return {}


def _experiment_round_index(workspace: Path, epoch_id: str, gid: str) -> Any:
    """Read ``round_index`` off a generation's persisted ``experiment.json``."""
    path = workspace / "epochs" / epoch_id / "generations" / gid / "experiment.json"
    return json.loads(path.read_text()).get("round_index")


def test_birth_round_index_stamped_per_round_end_to_end(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Round 0 mints v1..v4 (round_index=0); round 1 mints v5..v8
    (round_index=1) — and BOTH stores agree.

    This is the round mis-attribution from issue #16: before the fix every
    multi-challenger experiment carried the proposer's default round_index=0,
    so round 1's challengers folded onto round 0's bracket. We assert the
    birth round is stamped correctly onto each generation's experiment.json
    AND its lineage.json node, for every round.
    """
    workspace, epoch_id = _bootstrap_single_elim_workspace(tmp_path, field_size=4)
    _install_stub_adapter_factory(monkeypatch)
    # Per-generation losses: v1 (and later v5) lead their fields so a
    # challenger is crowned each round (the loop advances). Exact ordering
    # of the bracket is immaterial to the birth-round assertions.
    _install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={
            "v0": 2.0,
            "v1": 0.5,
            "v2": 1.2,
            "v3": 1.4,
            "v4": 1.6,
            "v5": 0.3,
            "v6": 1.1,
            "v7": 1.3,
            "v8": 1.5,
        },
        canned_pass_by_gen={f"v{i}": True for i in range(9)},
    )

    from zicato.orchestrator import evolve_n_rounds

    # field_size=4 ⇒ four proposer calls per round; two rounds ⇒ eight.
    outcomes = asyncio.run(
        evolve_n_rounds(
            rounds=2,
            workspace_root=workspace,
            epoch_id=epoch_id,
            harness_call_llm=_harness_call_llm,
            auxiliary_call_llm=_infinite_proposer_responder(),
            auto_epoch=False,
        )
    )

    assert len(outcomes) == 2

    # Round 0 minted v1..v4; round 1 minted v5..v8. Every generation's
    # BIRTH round is its outer evolve round — stamped onto both stores.
    expected_round = {
        "v1": 0,
        "v2": 0,
        "v3": 0,
        "v4": 0,
        "v5": 1,
        "v6": 1,
        "v7": 1,
        "v8": 1,
    }
    lineage = _lineage_gens(workspace, epoch_id)
    for gid, want in expected_round.items():
        assert (
            _experiment_round_index(workspace, epoch_id, gid) == want
        ), f"experiment.json round_index for {gid}"
        assert gid in lineage, f"{gid} missing from lineage.json"
        assert lineage[gid].get("round_index") == want, f"lineage.json round_index for {gid}"

    # If the seed v0 is present in lineage, it stays at round 0 — it must
    # never be re-stamped to a later round as it defends across rounds. (The
    # hand-built fixture does not run the genesis seeding path that registers
    # v0, so it may be absent; the invariant only bites when it is present.)
    if "v0" in lineage:
        assert lineage["v0"].get("round_index", 0) == 0


def test_inflight_round_visible_in_every_store_before_settle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Mid round 1 — challengers minted, bracket NOT yet settled — round 1
    is visible to lineage.json, experiment.json AND the durable
    field-tournament record.

    This is the issue's precise failure window. We monkeypatch
    ``resolve_tournament`` so the FIRST time it runs against round 1's field
    (v5..v8) we snapshot the on-disk state at the exact moment the bracket
    is being resolved but has not settled. Before the fix, lineage.json
    still held only v0..v4 and the field record for round 1 did not exist;
    the new round was invisible to every queryable store.
    """
    workspace, epoch_id = _bootstrap_single_elim_workspace(tmp_path, field_size=4)
    _install_stub_adapter_factory(monkeypatch)
    _install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={
            "v0": 2.0,
            "v1": 0.5,
            "v2": 1.2,
            "v3": 1.4,
            "v4": 1.6,
            "v5": 0.3,
            "v6": 1.1,
            "v7": 1.3,
            "v8": 1.5,
        },
        canned_pass_by_gen={f"v{i}": True for i in range(9)},
    )

    import zicato.selection as selection

    real_resolve = selection.resolve_tournament
    captured: dict[str, Any] = {}

    async def _resolve_capturing(strategy: Any, **kwargs: Any) -> Any:
        # Snapshot the on-disk stores at the moment THIS round's bracket is
        # being driven (challengers minted; decision not yet produced). We
        # only care about the round that introduces v5 (round 1) — the
        # mid-round window the issue describes.
        gens_root = workspace / "epochs" / epoch_id / "generations"
        if (gens_root / "v5").is_dir() and "round1" not in captured:
            captured["round1"] = {
                "lineage": _lineage_gens(workspace, epoch_id),
                "exp_round": {
                    gid: _experiment_round_index(workspace, epoch_id, gid)
                    for gid in ("v5", "v6", "v7", "v8")
                    if (gens_root / gid).is_dir()
                },
                "field_v5": (
                    json.loads(
                        (
                            workspace / "epochs" / epoch_id / "tournaments" / "field-v5.json"
                        ).read_text()
                    )
                    if (workspace / "epochs" / epoch_id / "tournaments" / "field-v5.json").is_file()
                    else None
                ),
            }
        return await real_resolve(strategy, **kwargs)

    monkeypatch.setattr(selection, "resolve_tournament", _resolve_capturing)

    from zicato.orchestrator import evolve_n_rounds

    asyncio.run(
        evolve_n_rounds(
            rounds=2,
            workspace_root=workspace,
            epoch_id=epoch_id,
            harness_call_llm=_harness_call_llm,
            auxiliary_call_llm=_infinite_proposer_responder(),
            auto_epoch=False,
        )
    )

    assert "round1" in captured, "resolve_tournament never saw round 1's field"
    snap = captured["round1"]

    # (1) lineage.json already carries round 1's challengers with round_index=1
    #     WHILE the bracket is still in flight — they are NOT folded onto an
    #     earlier round, and round 0's generations are still present.
    lineage = snap["lineage"]
    for gid in ("v5", "v6", "v7", "v8"):
        assert gid in lineage, f"{gid} absent from lineage.json mid-round 1"
        assert (
            lineage[gid].get("round_index") == 1
        ), f"{gid} mis-attributed mid-round 1 (round_index != 1)"
    for gid in ("v1", "v2", "v3", "v4"):
        assert gid in lineage, f"round 0 generation {gid} vanished mid-round 1"

    # (2) each in-flight challenger's experiment.json carries round_index=1
    #     mid-round (the stamp lands at creation, not at settle).
    for gid, ri in snap["exp_round"].items():
        assert ri == 1, f"experiment.json round_index for {gid} mid-round 1"

    # (3) the durable field-tournament envelope for round 1 EXISTS and is
    #     marked in_progress before the bracket settles — the round is
    #     queryable by the index / reindex / external tooling, not just the
    #     dashboard.
    field = snap["field_v5"]
    assert field is not None, "round 1 field-v5.json absent mid-round (not opened)"
    assert field.get("state") == "in_progress", field.get("state")
    comp_ids = {c.get("generation_id") for c in field.get("competitors", [])}
    # Champion (the round-0 winner) plus round 1's challengers.
    assert {"v5", "v6", "v7", "v8"}.issubset(comp_ids), comp_ids


def test_field_record_finalises_to_settled_after_round(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """After a round settles, its field-tournament record is upserted from
    ``in_progress`` to ``settled`` under the SAME key — the open + settle
    compose idempotently (no duplicate record, no stale in_progress)."""
    workspace, epoch_id = _bootstrap_single_elim_workspace(tmp_path, field_size=4)
    _install_stub_adapter_factory(monkeypatch)
    _install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={
            "v0": 2.0,
            "v1": 0.5,
            "v2": 1.2,
            "v3": 1.4,
            "v4": 1.6,
        },
        canned_pass_by_gen={f"v{i}": True for i in range(5)},
    )

    from zicato.orchestrator import evolve_once

    asyncio.run(
        evolve_once(
            workspace_root=workspace,
            epoch_id=epoch_id,
            harness_call_llm=_harness_call_llm,
            auxiliary_call_llm=_infinite_proposer_responder(),
        )
    )

    field_dir = workspace / "epochs" / epoch_id / "tournaments"
    records = sorted(field_dir.glob("field-*.json"))
    # Exactly one field record for the single round — keyed on its first
    # challenger (v1) — finalised to settled (no leftover in_progress, no
    # duplicate).
    assert [p.name for p in records] == ["field-v1.json"], records
    field = json.loads(records[0].read_text())
    assert field.get("state") == "settled", field.get("state")
    assert field.get("rounds"), "settled record should carry the resolved bracket"
    assert field.get("standings"), "settled record should carry standings"

    # The index dual-write carries the SAME single field-level tournament
    # row (idempotent upsert on the field tournament_id), not two rows.
    db = sqlite3.connect(workspace / "index.db")
    try:
        rows = db.execute(
            "SELECT tournament_id FROM tournaments WHERE tournament_id = ?",
            (f"{epoch_id}:field:v1",),
        ).fetchall()
    finally:
        db.close()
    assert len(rows) == 1, rows
