"""Known-answer convergence harness — the FULL loop, no tournament stubs.

This is the end-to-end proof that the shipped evolve loop converges on a
planted-defect target: real propose → apply → validate → **subprocess
tournament workers** → reduce → gate → persist, under the DEFAULT git
generation-store backend, with a scalar that lands on an exact,
hand-computable floor. Nothing tournament-side is monkeypatched — only
the shared conftest autouse fixtures apply (default-proposer text shim,
harmonograf launch stub).

The target is ``examples/zicato_examples/target_0_convergence``:

* ``agent/policy.py`` seeds three defect tokens; each remaining token
  emits one ``drift_detected`` frame (severity ``info`` → ``+1.0`` drift
  loss per run) and each KNOWN token fails exactly one board predicate.
* The scripted proposer (``mocks.aux_llm``) runs a three-round gauntlet
  script: remove a token (→ promote), ADD a token (→ reject, the
  negative control), remove another token (→ promote to the floor).

The exact floor, from the shipped scoring formulas
(``zicato.scoring.builtins``) with the example contract
(``severity_weights.info = 1.0``, ``drift_weight = pass_weight = 1.0``,
``runtime_weight = 0``):

    scalar(k, passes) = 1.0 * k  +  1.0 * (1 - passes/5)

    v0 (3 tokens, 2/5 pass) = 3.6      — seeded baseline
    v1 (2 tokens, 3/5 pass) = 2.4      — round 1, PROMOTED
    v2 (3 tokens, 2/5 pass) = 3.6      — round 2, REJECTED (control)
    v3 (1 token,  4/5 pass) = 1.2      — round 3, PROMOTED = THE FLOOR
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import zicato_examples.target_0_convergence as _t0_pkg
from zicato.epoch.lifecycle import _scoring_from_dict, new_epoch
from zicato_examples.target_0_convergence import mocks as t0_mocks

EXAMPLE_DIR = Path(_t0_pkg.__file__).resolve().parent
AGENT_DIR = EXAMPLE_DIR / "agent"
BOARD_PATH = EXAMPLE_DIR / "board.jsonl"
SCORING_PATH = EXAMPLE_DIR / "scoring.json"
RACING_SCORING_PATH = EXAMPLE_DIR / "scoring.effective.json"

#: The board size — the pass component denominator in the scalar.
BOARD_SIZE = 5

#: The adapter block the workspace declares. kind="import" is the honest
#: production shape (adapter_factory + the subprocess worker reconstruct
#: the SAME object from it) — no factory monkeypatch anywhere.
ADAPTER_BLOCK = {
    "kind": "import",
    "factory": "zicato_examples.target_0_convergence.harness:make_adapter",
}


def _expected_scalar(tokens: int, passes: int) -> float:
    """The exact scalar for a generation, from the shipped formula.

    Mirrors ``zicato.scoring.builtins.builtin_drift_loss`` +
    ``builtin_scalar`` under the example contract: every run carries
    ``tokens`` drift frames at severity ``info`` (weight 1.0, kind
    weight 1.0), zero plan revisions, zero task failures, and
    ``runtime_weight == 0`` — so the per-run drift loss is exactly
    ``float(tokens)`` and the drift mean over identical runs equals it.
    The pass component is ``pass_weight * (1 - mean_score)`` with
    ``mean_score = passes / BOARD_SIZE`` (all-bool board). Every default
    namespace aggregate is exactly ``0.0`` here (no LLM-call cost, no
    latency/rubric/schema metrics), so it contributes nothing.
    """
    drift_component = 1.0 * float(tokens)
    mean_score = float(passes) / BOARD_SIZE
    pass_component = 1.0 * (1.0 - mean_score)
    return sum([drift_component, pass_component])


#: The known floor: one token (verbose-prose) left, 4/5 predicates pass.
EXPECTED_FLOOR = _expected_scalar(tokens=1, passes=4)

#: Per-round expectations for the gauntlet script.
EXPECTED_V0 = _expected_scalar(tokens=3, passes=2)  # 3.6
EXPECTED_V1 = _expected_scalar(tokens=2, passes=3)  # 2.4
EXPECTED_V2 = _expected_scalar(tokens=3, passes=2)  # 3.6 (negative control)


def _bootstrap_workspace(tmp_path: Path, scoring_path: Path) -> tuple[Path, str]:
    """Create a workspace + one epoch; leave v0 to the production seeder.

    Unlike the orchestrator tests' hand-built directory layout, this
    bootstrap deliberately uses the DEFAULT storage backend (no
    ``storage_backend`` knob ⇒ the git generation store): the first
    evolve round's ``_ensure_baseline_snapshot`` seeds ``v0`` from the
    registered ``mutable_trees`` through the git backend — the shipped
    default path.
    """
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    (workspace / "config.json").write_text(
        json.dumps(
            {
                "instance_id": "default",
                "created_at": "2026-07-01T00:00:00Z",
                "adapter": ADAPTER_BLOCK,
                "mutable_trees": [str(AGENT_DIR)],
            }
        )
    )

    brief = tmp_path / "brief.md"
    brief.write_text(
        "# Convergence brief\n"
        "- Remove defect tokens from the writing policy, one per round.\n"
        "- Never fabricate metrics.\n"
    )

    weights = _scoring_from_dict(json.loads(scoring_path.read_text()))
    cfg = new_epoch(
        workspace,
        name="t0-convergence",
        board_source=BOARD_PATH,
        brief_source=brief,
        weights=weights,
        auto_close_previous=False,
        # The example's skills-only proposer dir selects the REAL
        # skill-composed text-shim proposer (an explicit dir:* spec) —
        # the same engine the RUN.md no-endpoint recipe uses. This test
        # therefore does not depend on the conftest default-proposer pin:
        # a dir:* spec flows through the real build_proposer_agent.
        proposer_path=EXAMPLE_DIR / "proposer",
    )
    return workspace, cfg.id


def test_gauntlet_converges_to_known_floor(tmp_path: Path) -> None:
    """Three real rounds: promoted, rejected, promoted — to the exact floor."""
    workspace, epoch_id = _bootstrap_workspace(tmp_path, SCORING_PATH)
    t0_mocks.reset()

    from zicato.evolve.loop import evolve_n_rounds

    outcomes = asyncio.run(
        evolve_n_rounds(
            rounds=3,
            workspace_root=workspace,
            epoch_id=epoch_id,
            harness_call_llm=t0_mocks.harness_llm,
            auxiliary_call_llm=t0_mocks.aux_llm,
            auto_epoch=False,
        )
    )

    # --- (a) The decision sequence is exactly promoted, rejected, promoted.
    assert [o.tournament_decision for o in outcomes] == ["promoted", "rejected", "promoted"]
    assert [o.proposed_generation_id for o in outcomes] == ["v1", "v2", "v3"]
    assert [o.parent_generation_id for o in outcomes] == ["v0", "v1", "v1"]

    # --- (b) Champion scalars strictly decrease across promotions and the
    # final champion lands on the exact known floor. The expected floats
    # come from the shipped scoring formula (see _expected_scalar).
    r1, r2, r3 = outcomes
    assert r1.parent_scalar == EXPECTED_V0
    assert r1.child_scalar == EXPECTED_V1
    assert r2.parent_scalar == EXPECTED_V1
    assert r2.child_scalar == EXPECTED_V2
    assert r2.child_scalar > r2.parent_scalar, "the negative control must regress"
    assert r3.parent_scalar == EXPECTED_V1
    assert r3.child_scalar == EXPECTED_FLOOR
    assert r1.parent_scalar > r1.child_scalar > r3.child_scalar
    assert EXPECTED_FLOOR == 1.2

    # --- The default git generation store actually backed the run: the
    # private repo exists and carries every generation as a tag.
    from zicato.epoch.genstore import default_generation_store
    from zicato.epoch.git_genstore import GitGenerationStore

    store = default_generation_store(workspace)
    assert isinstance(store, GitGenerationStore)
    assert (workspace / "repo" / ".git").exists()
    assert store.list_generations(epoch_id) == ["v0", "v1", "v2", "v3"]

    # --- (c) Artifacts. experiment.json + one patch record per challenger.
    from zicato.epoch.journal import read_experiment

    gens_dir = workspace / "epochs" / epoch_id / "generations"
    for gid, decision in (("v1", "promoted"), ("v2", "rejected"), ("v3", "promoted")):
        exp_path = gens_dir / gid / "experiment.json"
        assert exp_path.exists(), gid
        experiment = read_experiment(workspace, epoch_id, gid)
        assert len(experiment.patches) == 1, gid
        assert experiment.patches[0].mutation_id == "style_rules", gid
        outcome = json.loads(exp_path.read_text())["outcome"]
        assert outcome["tournament_decision"] == decision, gid

    # Lineage: promoted flags + parents match the script.
    lineage = json.loads((workspace / "lineage.json").read_text())
    nodes: dict[str, dict[str, object]] = {}
    for ep in lineage.get("epochs", []):
        if ep.get("id") == epoch_id:
            nodes = {n["id"]: n for n in ep.get("generations", [])}
    assert nodes["v1"]["parent_id"] == "v0" and nodes["v1"]["promoted"] is True
    assert nodes["v2"]["parent_id"] == "v1" and nodes["v2"]["promoted"] is False
    assert nodes["v3"]["parent_id"] == "v1" and nodes["v3"]["promoted"] is True

    # Journal: one markdown section per round (the journal is a running
    # narrative, one "## vN — <core idea>" heading per experiment).
    from zicato.core.workspace import journal_path

    journal_text = journal_path(workspace, epoch_id).read_text()
    for gid in ("v1", "v2", "v3"):
        assert f"## {gid} — " in journal_text, gid

    # Per-unit loss.json for the final champion, with the exact per-run
    # numbers the floor is built from: one info-severity drift frame
    # (the remaining verbose-prose token) and 4/5 predicates passing.
    from zicato.board.jsonl import load_board
    from zicato.core.workspace import board_path, loss_profile_path
    from zicato.telemetry.reducer import read_loss_profile

    passes = {}
    for entry in load_board(board_path(workspace, epoch_id)):
        lp_path = loss_profile_path(workspace, epoch_id, "v3", entry.id)
        assert lp_path.exists(), entry.id
        profile = read_loss_profile(lp_path)
        assert profile.drift_loss == 1.0, entry.id
        assert [(c.kind, c.severity, c.count) for c in profile.drift_counts] == [
            ("unexpected_output", "info", 1)
        ], entry.id
        passes[entry.id] = profile.pass_fail
    assert passes == {
        "conv_body": True,
        "conv_summary": True,
        "conv_citations": True,
        "conv_concise": False,  # verbose-prose is the token left at the floor
        "conv_no_fabrication": True,
    }

    # The promoted head advanced to the final champion only.
    marker = workspace / "epochs" / epoch_id / "current_generation"
    assert marker.read_text().strip() == "v3"

    # --- (d) Loop health: the harness must not trip the signal detectors.
    # A degenerate_scoring or non_differentiating_entry finding would mean
    # the planted-defect design stopped differentiating generations.
    health_dir = workspace / "epochs" / epoch_id / "health"
    reports = sorted(health_dir.glob("round_*.json"))
    assert reports, "per-round loop-health reports should have been written"
    for report in reports:
        payload = json.loads(report.read_text())
        codes = {str(f.get("code", "")) for f in payload.get("findings", [])}
        assert "degenerate_scoring" not in codes, report.name
        assert "non_differentiating_entry" not in codes, report.name
    assert not any(o.health_critical for o in outcomes)


def test_racing_field_best_arm_survives_to_floor(tmp_path: Path) -> None:
    """The racing contract (field 4, replicates 2, evidence pre-gate at
    0.8) drives a real multi-challenger round through subprocess workers:
    the scripted field's best-known arm (v2 — only ``verbose-prose``
    left) survives every rung, clears the champion gate, and is promoted
    at the exact known floor."""
    workspace, epoch_id = _bootstrap_workspace(tmp_path, RACING_SCORING_PATH)
    t0_mocks.reset()

    from zicato.evolve.loop import evolve_n_rounds

    outcomes = asyncio.run(
        evolve_n_rounds(
            rounds=1,
            workspace_root=workspace,
            epoch_id=epoch_id,
            harness_call_llm=t0_mocks.harness_llm,
            auxiliary_call_llm=t0_mocks.racing_aux_llm,
            auto_epoch=False,
        )
    )

    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome.tournament_decision == "promoted"
    # The scripted field is a strict superset chain of defect-token sets,
    # so the second payload (tokens == {verbose-prose}) is the best arm on
    # EVERY board slice and must be the survivor.
    assert outcome.proposed_generation_id == "v2"
    assert outcome.parent_generation_id == "v0"
    assert outcome.parent_scalar == EXPECTED_V0
    assert outcome.child_scalar == EXPECTED_FLOOR

    # All four challengers were really proposed + applied; only the best
    # arm was promoted, the dead branches carry rejected racing outcomes.
    gens_dir = workspace / "epochs" / epoch_id / "generations"
    for gid in ("v1", "v2", "v3", "v4"):
        oc = json.loads((gens_dir / gid / "experiment.json").read_text())["outcome"]
        assert oc["structure"] == "racing", gid
        expected = "promoted" if gid == "v2" else "rejected"
        assert oc["tournament_decision"] == expected, gid

    # The promoted head advanced to the surviving arm.
    marker = workspace / "epochs" / epoch_id / "current_generation"
    assert marker.read_text().strip() == "v2"
