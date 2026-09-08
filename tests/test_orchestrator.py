"""Tests for :mod:`zicato.orchestrator`.

These tests stub every external dependency (LLM callables, harness
adapter, telemetry sink, reducer) so the orchestrator can be exercised
end-to-end without goldfive, google-adk, or any real model traffic.

The proposer is not stubbed. These tests are about the orchestrator's
tournament / promotion / lineage logic rather than about how a candidate
is invented, but a round cannot open without a proposal runtime, so
``bootstrap_workspace`` declares the stand-in one
(:func:`tests._foe_support.stand_in_proposer_block`) and every round runs
a real Foe episode per candidate against a binary that needs no
credential, no network and no model. A test whose subject IS a
misbehaving proposer steers that stand-in through the same helper.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from tests._contract_pins import deterministic_weights
from tests._foe_support import stand_in_proposer_block
from tests._orchestrator_harness import (
    bootstrap_workspace,
    evaluation_call_llm,
    install_stub_adapter_factory,
    install_telemetry_stubs,
    run_evolve_once,
    target_call_llm,
)
from zicato.core.types import TournamentStructure
from zicato.epoch.lifecycle import new_epoch

# ---------------------------------------------------------------------------
# LLM stub callables — two distinct objects so the two-callable check passes.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Workspace bootstrap
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Adapter / telemetry stubs
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Proposer canned response
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_evolve_once_promotes_on_improvement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """One improving round publishes its candidate and diagnostic outputs."""
    workspace, epoch_id = bootstrap_workspace(tmp_path)
    install_stub_adapter_factory(monkeypatch)
    install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 1.0},
        canned_pass_by_gen={"v0": True, "v1": True},
    )

    outcome = run_evolve_once(workspace, epoch_id, evaluation_call_llm)

    assert outcome.tournament_decision == "promoted"
    assert outcome.parent_generation_id == "v0"
    assert outcome.proposed_generation_id == "v1"
    assert outcome.child_scalar < outcome.parent_scalar

    # experiment.json + patches/{id}.json exist for v1.
    v1_dir = workspace / "epochs" / epoch_id / "generations" / "v1"
    assert (v1_dir / "experiment.json").exists()
    body = json.loads((v1_dir / "experiment.json").read_text())
    assert body["outcome"]["tournament_decision"] == "promoted"
    assert len(body["patch_ids"]) == 1
    patch_file = v1_dir / "patches" / f"{body['patch_ids'][0]}.json"
    assert patch_file.exists()

    # Snapshot was applied: the episode's edit landed.
    snap_text = (v1_dir / "snapshot" / "agent.py").read_text()
    assert "hello [v1]" in snap_text

    # current_generation marker bumped.
    marker = workspace / "epochs" / epoch_id / "current_generation"
    assert marker.exists()
    assert marker.read_text().strip() == "v1"

    # Journal entry appended.
    journal = (workspace / "epochs" / epoch_id / "journal.md").read_text()
    assert "Tag the greeting literal for candidate v1." in journal

    # The real health assessment publishes the selected epoch.
    report_path = workspace / "epochs" / epoch_id / "health" / "round_1.json"
    assert report_path.exists()
    body = json.loads(report_path.read_text())
    assert body["epoch_id"] == epoch_id

    # Patches are stored separately from the experiment body.
    v1 = workspace / "epochs" / epoch_id / "generations" / "v1"
    body = json.loads((v1 / "experiment.json").read_text())
    assert "patches" not in body
    assert isinstance(body["patch_ids"], list)
    assert len(body["patch_ids"]) == 1
    assert (v1 / "patches" / f"{body['patch_ids'][0]}.json").exists()

    from zicato.core.workspace import mutations_json_path

    snapshot_path = mutations_json_path(workspace, epoch_id)
    assert snapshot_path.exists()
    points = json.loads(snapshot_path.read_text())
    assert isinstance(points, list)
    # Both registered mutation kinds appear in the persisted snapshot.
    assert len(points) == 2
    point = next(p for p in points if p["id"] == "greeting")
    assert set(point.keys()) == {
        "id",
        "kind",
        "file",
        "line_start",
        "line_end",
        "content",
        "content_hash",
    }
    assert point["id"] == "greeting"
    assert point["kind"] == "span"
    # Path fields are stringified for JSON.
    assert isinstance(point["file"], str)
    assert point["file"].endswith("agent.py")
    assert isinstance(point["line_start"], int)
    assert isinstance(point["line_end"], int)
    assert '"hello"' in point["content"]
    assert isinstance(point["content_hash"], str)
    # No leftover .tmp file from the atomic write.
    assert not snapshot_path.with_name(snapshot_path.name + ".tmp").exists()

    # The analysis includes the round hypothesis and generation.
    epoch_dir = workspace / "epochs" / epoch_id
    md = epoch_dir / "analysis.md"
    html = epoch_dir / "analysis.html"
    assert md.is_file()
    assert html.is_file()

    md_text = md.read_text()
    assert "epoch analysis report" in md_text.lower()
    assert "<!-- EYEBROW -->" in md_text
    for section in (
        "## Abstract",
        "## Introduction",
        "## Methodology",
        "## Experimental Results",
        "## Conclusion & Next Directions",
    ):
        assert section in md_text, section
    assert "Tag the greeting literal for candidate v1." in md_text
    assert "v1" in md_text
    assert html.read_text().startswith("<!DOCTYPE html>")

    # Insights are also published for the round.
    insights = epoch_dir / "insights"
    assert insights.is_dir()


def test_evolve_round_stamps_birth_round_index_on_lineage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A minted challenger carries its round's index; carried champions keep theirs.

    Round 0 mints v1 (promoted) — its birth round is 0. Round 1 mints v2
    — its birth round is 1 — while v1 (carried forward as champion) keeps
    its original birth round: a defending champion is NOT re-stamped each
    round. (The bootstrap seeds v0's snapshot without a lineage row, so
    the seed=0 invariant is covered by the lineage + index round-trip
    tests rather than asserted here.)
    """
    from zicato.epoch.lineage import load_lineage

    workspace, epoch_id = bootstrap_workspace(
        tmp_path,
        weights=deterministic_weights(
            promote_margin=0.01, tournament_structure=TournamentStructure(structure="gauntlet")
        ),
    )
    install_stub_adapter_factory(monkeypatch)
    install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 1.0, "v2": 0.5},
        canned_pass_by_gen={"v0": True, "v1": True, "v2": True},
    )

    # Round 0: v0 -> v1, promoted. Birth round of v1 is 0.
    out0 = run_evolve_once(workspace, epoch_id, evaluation_call_llm, round_index=0)
    assert out0.tournament_decision == "promoted"
    assert out0.proposed_generation_id == "v1"

    rounds0 = _lineage_round_index(load_lineage(workspace).to_dict(), epoch_id)
    assert rounds0["v1"] == 0  # minted in round 0

    # Round 1: v1 -> v2, promoted. Birth round of v2 is 1; v1 keeps its.
    out1 = run_evolve_once(workspace, epoch_id, evaluation_call_llm, round_index=1)
    assert out1.proposed_generation_id == "v2"

    rounds1 = _lineage_round_index(load_lineage(workspace).to_dict(), epoch_id)
    # The carried champion keeps its BIRTH round — not re-stamped to 1.
    assert rounds1["v1"] == 0
    # The newly-minted challenger carries the current round.
    assert rounds1["v2"] == 1


def _lineage_round_index(lineage: dict[str, Any], epoch_id: str) -> dict[str, int]:
    """Map generation_id -> round_index for one epoch's lineage rows."""
    for entry in lineage.get("epochs", []):
        if entry.get("id") == epoch_id:
            return {
                g["id"]: g["round_index"]
                for g in entry.get("generations", [])
                if "round_index" in g
            }
    return {}


def test_evolve_once_fast_mode_degrades_to_full_when_no_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A6: fast mode with no cached parent aggregate runs a full round.

    ``--mode fast`` is now the CLI default; a fresh epoch's first round
    has no cached ``gen_score.json`` yet. Rather than raising
    ``FileNotFoundError``, fast mode degrades to a single full A/B
    tournament that round — which scores the parent and writes the
    cache — so subsequent fast rounds have a cache to reuse.
    """
    workspace, epoch_id = bootstrap_workspace(tmp_path)
    install_stub_adapter_factory(monkeypatch)
    install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 1.0},
        canned_pass_by_gen={"v0": True, "v1": True},
    )

    # No gen_score.json exists for v0 — fast mode must not crash.
    v0_cache = workspace / "epochs" / epoch_id / "generations" / "v0" / "gen_score.json"
    assert not v0_cache.exists()

    outcome = run_evolve_once(workspace, epoch_id, evaluation_call_llm, fast_mode=True)

    assert outcome.tournament_decision == "promoted"
    # The seeding full round wrote the parent's cached aggregate, so a
    # later fast round has something to reuse.
    assert v0_cache.exists()


def test_evolve_once_rejects_when_child_regresses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A child with higher drift and lower pass_rate does NOT promote."""
    workspace, epoch_id = bootstrap_workspace(tmp_path)
    install_stub_adapter_factory(monkeypatch)
    install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 0.0, "v1": 5.0},
        canned_pass_by_gen={"v0": True, "v1": False},
    )

    outcome = run_evolve_once(workspace, epoch_id, evaluation_call_llm)

    assert outcome.tournament_decision == "rejected"
    assert outcome.rejection_reason  # non-empty

    # current_generation marker NOT bumped — still v0 (no marker yet).
    marker = workspace / "epochs" / epoch_id / "current_generation"
    assert not marker.exists()

    # Experiment.json still persisted with the rejected outcome.
    v1_dir = workspace / "epochs" / epoch_id / "generations" / "v1"
    body = json.loads((v1_dir / "experiment.json").read_text())
    assert body["outcome"]["tournament_decision"] == "rejected"


def test_evolve_once_retries_destructive_patch_then_succeeds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A destructive proposer patch triggers a bounded retry, not a reject.

    The proposer's first response parses cleanly but its patch breaks
    the child snapshot post-apply. The orchestrator must NOT waste the
    round — it feeds the post-apply validator findings back to the
    proposer, which re-proposes a clean patch, and the round proceeds to
    a real tournament decision.
    """
    # The episode's first edit is destructive; its verifier reports the
    # findings and the episode repairs itself on the next turn.
    workspace, epoch_id = bootstrap_workspace(tmp_path, break_first=1)
    install_stub_adapter_factory(monkeypatch)
    install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 1.0},
        canned_pass_by_gen={"v0": True, "v1": True},
    )

    outcome = run_evolve_once(workspace, epoch_id, evaluation_call_llm)

    # The round was NOT wasted — it reached a real tournament decision.
    assert outcome.tournament_decision == "promoted"
    assert outcome.proposed_generation_id == "v1"

    # The child snapshot carries the CLEAN retry's patch, not the
    # destructive one — and it still parses.
    import ast

    snap_text = (
        workspace / "epochs" / epoch_id / "generations" / "v1" / "snapshot" / "agent.py"
    ).read_text()
    ast.parse(snap_text)
    assert "hello [v1]" in snap_text
    assert "    if\n" not in snap_text


def test_evolve_once_rejects_when_the_episode_cannot_repair_its_edit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An episode whose verifier is never satisfied rejects the round cleanly.

    The verifier's retries are bounded. Once they are spent the episode
    ends BLOCKED rather than crashing, and the round settles that as a
    rejection whose reason names the block — so the evolve loop keeps
    going and the journal says why nothing was proposed.
    """
    # Every turn writes the same unparseable edit, so the verifier's
    # findings can never be answered.
    workspace, epoch_id = bootstrap_workspace(
        tmp_path,
        break_first=99,
        weights=deterministic_weights(
            promote_margin=0.01, tournament_structure=TournamentStructure(structure="gauntlet")
        ),
    )
    install_stub_adapter_factory(monkeypatch)
    install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 1.0},
        canned_pass_by_gen={"v0": True, "v1": True},
    )

    outcome = run_evolve_once(workspace, epoch_id, evaluation_call_llm, max_proposer_retries=2)

    assert outcome.tournament_decision == "rejected"
    assert outcome.rejection_reason

    # A clean, append-only journal entry was still written.
    journal = (workspace / "epochs" / epoch_id / "journal.md").read_text()
    assert journal.strip()  # non-empty — the round left a record


def test_evolve_n_rounds_stops_on_consecutive_rejections(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Three rejections in a row should halt the loop early."""
    workspace, epoch_id = bootstrap_workspace(
        tmp_path,
        weights=deterministic_weights(
            promote_margin=0.01, tournament_structure=TournamentStructure(structure="gauntlet")
        ),
    )
    install_stub_adapter_factory(monkeypatch)
    # Same canned losses → every round rejects.
    install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 0.0, "v1": 5.0, "v2": 5.0, "v3": 5.0, "v4": 5.0},
        canned_pass_by_gen={"v0": True, "v1": False, "v2": False, "v3": False, "v4": False},
    )

    from zicato.orchestrator import evolve_n_rounds

    outcomes = asyncio.run(
        evolve_n_rounds(
            rounds=8,
            workspace_root=workspace,
            epoch_id=epoch_id,
            target_call_llm=target_call_llm,
            evaluation_call_llm=evaluation_call_llm,
            max_consecutive_rejections=3,
        )
    )
    assert len(outcomes) == 3
    assert all(o.tournament_decision == "rejected" for o in outcomes)


# ---------------------------------------------------------------------------
# mutations.json per-epoch snapshot
# ---------------------------------------------------------------------------


def test_mutations_json_path_helper(tmp_path: Path) -> None:
    """mutations_json_path resolves under the epoch directory."""
    from zicato.core.workspace import epoch_dir, mutations_json_path

    p = mutations_json_path(tmp_path, "ep1")
    assert p == epoch_dir(tmp_path, "ep1") / "mutations.json"
    assert p.name == "mutations.json"


# ---------------------------------------------------------------------------
# Heartbeat metadata populated during a round
# ---------------------------------------------------------------------------


def test_evolve_n_rounds_populates_heartbeat_metadata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The heartbeat carries the real epoch / generation / round during a round."""
    workspace, epoch_id = bootstrap_workspace(tmp_path)
    install_stub_adapter_factory(monkeypatch)
    install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 1.0},
        canned_pass_by_gen={"v0": True, "v1": True},
    )

    from zicato.orchestrator import evolve_n_rounds
    from zicato.runtime.state import read_heartbeat

    outcomes = asyncio.run(
        evolve_n_rounds(
            rounds=1,
            workspace_root=workspace,
            epoch_id=epoch_id,
            target_call_llm=target_call_llm,
            evaluation_call_llm=evaluation_call_llm,
            instance_id="hb-meta",
        )
    )
    assert len(outcomes) == 1

    hb = read_heartbeat(workspace)
    assert hb is not None
    # Real coordinates, not empty strings.
    assert hb.epoch_id == epoch_id
    assert hb.generation_id == "v1"
    assert hb.round_index == 0
    assert hb.phase  # descriptive, non-empty
    # The harmonograf_url field round-trips (empty when unconfigured).
    assert hb.harmonograf_url == ""


# ---------------------------------------------------------------------------
# Epoch analysis report regeneration (orchestrator wiring)
# ---------------------------------------------------------------------------


def test_evolve_once_survives_report_generation_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A crash inside report generation never aborts the round."""
    workspace, epoch_id = bootstrap_workspace(tmp_path)
    install_stub_adapter_factory(monkeypatch)
    install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 1.0},
        canned_pass_by_gen={"v0": True, "v1": True},
    )

    # Make the report generator raise unconditionally — the orchestrator's
    # best-effort wrapper must swallow it.
    import zicato.analyzer as _analyzer_pkg

    async def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("report generation exploded")

    monkeypatch.setattr(_analyzer_pkg, "generate_epoch_report", _boom)

    outcome = run_evolve_once(workspace, epoch_id, evaluation_call_llm)
    # The round still produced its real verdict despite the report crash.
    assert outcome.tournament_decision == "promoted"
    assert outcome.proposed_generation_id == "v1"


def test_evolve_once_threads_configured_proposer_skill_into_the_episode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A skill on the epoch's configured proposer dir reaches the episode.

    Bootstraps a workspace whose epoch freezes a ``proposers/demo/`` dir
    carrying one skill, then asserts the skill body lands in the
    instructions the proposal episode ran under — proving the spec →
    ``build_proposer_agent`` → ``ProposerContext`` wiring flows the skill
    through the real evolve path, all the way into the model's context.
    """
    proposer_dir = tmp_path / "proposers" / "demo"
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    (workspace / "config.json").write_text(
        json.dumps(
            {
                "instance_id": "test",
                "created_at": "2026-05-14T00:00:00Z",
                # Hand-built directory-backend snapshot layout below; pin it.
                "generation_source_backend": "directory",
                "adapter": {"kind": "import", "factory": "tests._stub_adapter:make_stub_adapter"},
                "proposer": stand_in_proposer_block(tmp_path / "foe"),
                "contract": {"proposer_path": str(proposer_dir)},
                "runtime": {},
                "models": {
                    "engines": {
                        "target": {"call_llm": "tests._orchestrator_harness:target_call_llm"},
                        "evaluation": {
                            "call_llm": "tests._orchestrator_harness:evaluation_call_llm"
                        },
                    }
                },
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

    # The configured proposer directory and its skill are sealed with the epoch.
    skills_dir = proposer_dir / "skills"
    skills_dir.mkdir(parents=True)
    skill_body = "Prefer the smallest patch that moves the loss."
    (skills_dir / "minimal.md").write_text(
        "---\nname: minimal\ndescription: keep patches small\n---\n" + skill_body + "\n"
    )

    cfg = new_epoch(
        workspace,
        name="alpha",
        board_source=board_src,
        brief_source=brief_src,
        # Pinned deterministic knobs (replicates 1, evidence gate off,
        # single-sample proposer): these tests drive SCRIPTED proposers and
        # stub reducers whose call sequences assume the historical
        # single-run duel. See tests/_contract_pins.py.
        weights=deterministic_weights(promote_margin=0.01),
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

    from zicato.epoch.journal import write_seed_experiment

    write_seed_experiment(workspace, cfg.id, proposed_at=cfg.created_at)

    install_stub_adapter_factory(monkeypatch)
    install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 1.0},
        canned_pass_by_gen={"v0": True, "v1": True},
    )

    run_evolve_once(workspace, cfg.id, evaluation_call_llm)

    # The durable input capture records exactly what the episode was
    # given, so this reads the model's own context rather than a proxy.
    from zicato.proposer.input_capture import read_proposer_inputs

    instructions = [r["system"] for r in read_proposer_inputs(workspace, cfg.id)]
    assert instructions, "no proposal episode recorded its inputs"
    assert any(skill_body in text for text in instructions)
    assert any("Operating procedures for this epoch" in text for text in instructions)
