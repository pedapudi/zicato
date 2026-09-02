"""Evidence gate on the GAUNTLET crowning duel — known-answer e2e.

The gauntlet analogue of ``test_convergence_known_answer``'s racing case:
the target_0 planted-defect contract with an EXPLICIT
``promote_confidence_threshold`` drives one real evolve round through
subprocess workers, and the crowning train-promote must be confirmed by
the Bradley--Terry defer→replicate→inconclusive adjudication before it
is persisted.

Every evidence replicate ``j`` executes at the RESERVED replicate index
``EVIDENCE_REPLICATE_BASE + j`` — a genuine fresh subprocess run of BOTH
sides on a distinct per-unit cache slot — never a cache replay of (or a
force-fresh clobber over) the canonical replicate-0 ``loss.json`` the
crowning tournament scored. Both tests assert that routing on disk: the
reserved-slot files exist for champion AND challenger, tagged with the
slot-encoding matchup id, and the canonical slots carry no replicate tag.

Determinism note (mirrors the Tier-1 racing case): target_0 is exactly
zero-noise, so every fresh draw of the crowning pair is an IDENTICAL
child-win sample — under zero noise, independent sampling and replay are
indistinguishable BY VALUE, which is why this deterministic e2e cannot
(and does not) prove statistical independence. What it proves is that the
production replicate path executes at the reserved base through the real
worker machinery and the confirm still resolves both terminals; the
SOUNDNESS half — distinct draws with real variance, canonical byte
integrity under noise, and the driver refusing duplicate draws — lives in
``test_decision_procedure_power`` and ``test_driver_evidence_pregate``.

The Bradley--Terry fit over n identical wins is a fixed function of n;
its CIs separate only after several dozen duels (the Fisher information
grows slowly on a two-node graph). Both terminals are therefore
byte-deterministic:

* a GENEROUS budget (48) converges — the loop bootstraps to the
  credibility floor, defers while the CIs overlap, and crowns once they
  separate (the promotion path);
* a SMALL budget (2) exhausts at the credibility floor with overlapping
  CIs — terminally ``inconclusive``, the champion stands, and the duel
  is recorded to the dead-letter queue (the champion-stands path).

Cost note: each replicate duel is now a real 2-sides x 5-entries worker
sweep the first time (the reserved slot is a natural cache MISS; a
repeated confirm under the same contract would reuse the persisted
draws). That is the honest price of independent evidence — the pre-fix
"pure cache read" replicates were free precisely because they re-counted
one sample.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

import zicato_examples.target_0_convergence as _t0_pkg
from tests._contract_pins import resolved_contract_with_proposer
from tests._foe_support import stand_in_proposer_block
from zicato.epoch.lifecycle import _scoring_from_dict, new_epoch
from zicato.selection.evidence_gate import EVIDENCE_REPLICATE_BASE
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
                "proposer": stand_in_proposer_block(tmp_path / "foe"),
                "generation_source_backend": "git",
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
        contract=resolved_contract_with_proposer(workspace, EXAMPLE_DIR / "proposer"),
    )
    return workspace, cfg.id


def _run_one_round(workspace: Path, epoch_id: str) -> list:
    from zicato.evolve.loop import evolve_n_rounds

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


#: The five board-entry ids of the target_0 board (the crowning pair duels
#: the full board — no holdout split at this size).
_BOARD_ENTRY_IDS = (
    "conv_body",
    "conv_summary",
    "conv_citations",
    "conv_concise",
    "conv_no_fabrication",
)


def _assert_replicates_ran_at_reserved_slots(
    workspace: Path, epoch_id: str, *, replicates_run: int
) -> None:
    """Assert the evidence replicates executed at the RESERVED cache slots.

    For each evidence replicate ``j`` and BOTH sides of the crowning pair,
    the per-unit draw persisted as ``loss.r{EVIDENCE_REPLICATE_BASE+j}.json``
    next to — never over — the canonical ``loss.json``, and carries the
    slot-encoding matchup id the driver's audit guard keys on. The canonical
    slot itself belongs to the crowning tournament (no replicate tag).
    """
    for gen_id in ("v0", "v1"):
        runs_dir = workspace / "epochs" / epoch_id / "generations" / gen_id / "runs"
        for entry_id in _BOARD_ENTRY_IDS:
            canonical = json.loads((runs_dir / entry_id / "loss.json").read_text())
            assert not str(canonical.get("match_id", "")).startswith(
                "bt-replicate:"
            ), f"{gen_id}/{entry_id}: an evidence replicate clobbered the canonical slot"
            for j in range(replicates_run):
                slot = EVIDENCE_REPLICATE_BASE + j
                reserved = runs_dir / entry_id / f"loss.r{slot}.json"
                assert reserved.exists(), f"{gen_id}/{entry_id}: no reserved draw at r{slot}"
                profile = json.loads(reserved.read_text())
                assert (
                    profile.get("match_id") == f"bt-replicate:r{slot}:v0:v1"
                ), f"{gen_id}/{entry_id}: r{slot} draw is not tagged with its slot"


def _assert_evidence_refits_logged(workspace: Path, epoch_id: str, expected: int) -> None:
    """Every fitted confidence state must survive in the durable round log."""
    from zicato.epoch.round_log import RoundLog

    events = RoundLog(workspace, epoch_id, 0).read()
    refits = [event for event in events if event.type == "evidence_replicated"]
    assert len(refits) == expected
    assert all("ci_state" in event.payload for event in refits)


@pytest.mark.slow
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
    _assert_evidence_refits_logged(workspace, epoch_id, len(evidence["ci_history"]))

    # Every evidence replicate really executed at its reserved slot, both
    # sides, canonical slots untouched.
    _assert_replicates_ran_at_reserved_slots(
        workspace, epoch_id, replicates_run=evidence["replicates_spent"]
    )

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
    _assert_evidence_refits_logged(workspace, epoch_id, len(evidence["ci_history"]))

    # Both bootstrap replicates executed at the reserved slots (r4000,
    # r4001) on both sides; the canonical slots stayed the crowning duel's.
    _assert_replicates_ran_at_reserved_slots(workspace, epoch_id, replicates_run=2)

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
