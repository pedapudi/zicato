"""Evidence gate on the GAUNTLET crowning duel — known-answer e2e.

The gauntlet analogue of ``test_convergence_known_answer``'s racing case:
the target_0 planted-defect contract with an EXPLICIT
``promote_confidence_threshold`` drives one real evolve round through
subprocess workers, and the crowning train-promote must be confirmed by
the Bradley--Terry defer→replicate→inconclusive adjudication before it
is persisted.

Determinism note (mirrors the Tier-1 racing case): target_0 is exactly
zero-noise, so every replicate of the crowning pair is an IDENTICAL
child-win draw. The Bradley--Terry fit over n identical wins is a fixed
function of n; its CIs separate only after several dozen duels (the
Fisher information grows slowly on a two-node graph). Both terminals are
therefore byte-deterministic:

* a GENEROUS budget (48) converges — the loop bootstraps to the
  credibility floor, defers while the CIs overlap, and crowns once they
  separate (the promotion path);
* a SMALL budget (2) exhausts at the credibility floor with overlapping
  CIs — terminally ``inconclusive``, the champion stands, and the duel
  is recorded to the dead-letter queue (the champion-stands path).

``fast_mode=True`` keeps the replicate loop cheap: the first round's
degraded-fast full tournament persists every board unit, so each
replicate duel is a pure cache read (identical draws under zero noise —
exactly the point).
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

ADAPTER_BLOCK = {
    "kind": "import",
    "factory": "zicato_examples.target_0_convergence.harness:make_adapter",
}

#: Known scalars from the shipped scoring formula (see
#: tests/test_convergence_known_answer.py::_expected_scalar).
EXPECTED_V0 = 3.6
EXPECTED_V1 = 2.4


def _bootstrap(tmp_path: Path, replicate_budget: int) -> tuple[Path, str]:
    """A target_0 workspace whose gauntlet contract opts into the pre-gate."""
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
    brief.write_text("# Evidence-gate brief\n- Remove defect tokens, one per round.\n")

    scoring = json.loads(SCORING_PATH.read_text())
    scoring["tournament"] = {
        "structure": "gauntlet",
        "params": {
            # Keep the deterministic single-run duel (the pre-gate's own
            # replicate loop is the mechanism under test here).
            "replicates": 1,
            "promote_confidence_threshold": 0.8,
            "promote_confidence_replicates": replicate_budget,
        },
    }
    weights = _scoring_from_dict(scoring)
    cfg = new_epoch(
        workspace,
        name="t0-evidence-gate",
        board_source=BOARD_PATH,
        brief_source=brief,
        weights=weights,
        auto_close_previous=False,
        proposer_path=EXAMPLE_DIR / "proposer",
    )
    return workspace, cfg.id


def _run_one_round(workspace: Path, epoch_id: str) -> list:
    from zicato.evolve.loop import evolve_n_rounds

    t0_mocks.reset()
    return asyncio.run(
        evolve_n_rounds(
            rounds=1,
            workspace_root=workspace,
            epoch_id=epoch_id,
            harness_call_llm=t0_mocks.harness_llm,
            auxiliary_call_llm=t0_mocks.aux_llm,
            auto_epoch=False,
            fast_mode=True,
        )
    )


def test_gauntlet_promote_confirmed_by_evidence_gate(tmp_path: Path) -> None:
    """A true improvement still promotes — after the defer→replicate loop
    actually ran the crowning pair to CI separation."""
    workspace, epoch_id = _bootstrap(tmp_path, replicate_budget=48)
    outcomes = _run_one_round(workspace, epoch_id)

    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome.tournament_decision == "promoted"
    assert outcome.proposed_generation_id == "v1"
    assert outcome.parent_scalar == EXPECTED_V0
    assert outcome.child_scalar == EXPECTED_V1

    # The promoted head advanced.
    marker = workspace / "epochs" / epoch_id / "current_generation"
    assert marker.read_text().strip() == "v1"

    # The journaled outcome carries the evidence-gate resolution and proves
    # the replication path executed: the fit reached the credibility floor
    # (>= 3 crowning-pair duels), spent replicates chasing separation, and
    # terminally cleared with separated CIs.
    record = json.loads(
        (workspace / "epochs" / epoch_id / "generations" / "v1" / "experiment.json").read_text()
    )["outcome"]
    assert record["tournament_decision"] == "promoted"
    evidence = record["evidence"]
    assert evidence["decision"] == "promoted"
    assert evidence["credible"] is True
    assert evidence["ci_overlap"] is False
    assert evidence["n_duels"] >= 3
    assert evidence["replicates_spent"] >= 3
    assert evidence["p_stronger"] >= 0.8
    # The defer→replicate trace: one entry per refit, converging.
    assert len(evidence["ci_history"]) == evidence["replicates_spent"] + 1
    assert evidence["ci_history"][0]["replicates_spent"] == 0

    # The dead-letter queue stays empty on a confirmed promotion.
    assert not (workspace / "runtime" / "inconclusive").exists()


def test_gauntlet_inconclusive_champion_stands(tmp_path: Path) -> None:
    """CIs never separating within a small budget ⇒ terminal inconclusive:
    champion stands, DEFERRED journaled, dead-letter defer recorded."""
    workspace, epoch_id = _bootstrap(tmp_path, replicate_budget=2)
    outcomes = _run_one_round(workspace, epoch_id)

    assert len(outcomes) == 1
    outcome = outcomes[0]
    # Loop bookkeeping treats the hold as a non-promotion.
    assert outcome.tournament_decision == "rejected"
    assert outcome.proposed_generation_id == "v1"

    # The champion pointer never moved.
    marker = workspace / "epochs" / epoch_id / "current_generation"
    assert marker.read_text().strip() == "v0"

    # The journaled decision is the closed enum's DEFERRED token with the
    # inconclusive reason + the terminal evidence block.
    record = json.loads(
        (workspace / "epochs" / epoch_id / "generations" / "v1" / "experiment.json").read_text()
    )["outcome"]
    assert record["tournament_decision"] == "deferred"
    assert "inconclusive" in record["rejection_reason"]
    evidence = record["evidence"]
    assert evidence["decision"] == "inconclusive"
    assert evidence["credible"] is True
    assert evidence["ci_overlap"] is True
    assert evidence["n_duels"] == 3  # 1 crowning duel + 2 bootstrap replicates
    assert len(evidence["ci_history"]) == 3

    # Lineage records the held generation as a dead branch.
    lineage = json.loads((workspace / "lineage.json").read_text())
    nodes: dict[str, dict[str, object]] = {}
    for ep in lineage.get("epochs", []):
        if ep.get("id") == epoch_id:
            nodes = {n["id"]: n for n in ep.get("generations", [])}
    assert nodes["v1"]["promoted"] is False

    # The dead-letter defer was recorded, same shape as the multi-challenger
    # path writes.
    from zicato.selection.dead_letter import read_inconclusive

    dead = read_inconclusive(workspace, "v1")
    assert dead is not None
    assert dead["champion_id"] == "v0"
    assert dead["epoch_id"] == epoch_id
    assert dead["rating"]["decision"] == "inconclusive"
    assert len(dead["ci_history"]) == 3
