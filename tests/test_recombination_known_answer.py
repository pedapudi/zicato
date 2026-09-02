"""Known-answer recombination harness (WS-REC) — the FULL loop, no stubs.

The convergence-oracle bootstrap driven over a TWO-MARKER variant of the
convergence target: real propose → apply → validate → **subprocess
tournament workers** → reduce → gate → persist, under the DEFAULT git
generation-store backend. The policy carries TWO mutation points so the
two single-fix challengers touch DISJOINT mutation ids (the selector's
hard disjointness predicate):

    # zicato:mutable id="style_rules"        → seeds ``omit-summary``
    # zicato:mutable id="style_rules_extra"  → seeds ``skip-citations``

The exact arithmetic, from the shipped scoring formulas (σ = 0,
``severity_weights.info = 1.0``, the shipped channel coefficients — the
``drift:`` channel and ``pass_weight`` both at ``1.0``, ``runtime:`` at
``0.0`` — 5-entry board):

    scalar(k tokens, p passes) = k + (1 - p/5)

    v0 (2 tokens, 3/5)  = 2.4      — seeded baseline
    fix A alone (1, 4/5) = 1.2     — Δ 1.2 < promote_margin 1.5 → REJECT
    fix B alone (1, 4/5) = 1.2     — Δ 1.2 < 1.5               → REJECT
    the union   (0, 5/5) = 0.0     — Δ 2.4 > 1.5               → PROMOTE

Only the mechanical recombination slot can realise the union: the
scripted proposer knows only the two single fixes. Round 3's last
best-of-N slot mints the union (patches A + B, disjoint), the selection
short-circuit chooses it (``selection_mode="recombined"``), and the
unchanged gate promotes it — with EXACTLY n−1 auxiliary propose calls in
the minting round (cost-neutrality: the mint REPLACES the slot's call).

The STALL CONTROL runs the identical script with ``recombine`` OFF: the
champion must stay v0 (the V2 inverse — same seeds, same script, only
the mechanism differs). The DEDUP arm pins ``promote_margin = 3.0`` so
even the union rejects: the persisted mint must never re-mint (selector
predicate #5 over the persisted ``recombined_from``).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import zicato_examples.target_0_convergence as _t0_pkg
from tests._contract_pins import resolved_contract_with_proposer
from zicato.epoch.lifecycle import _scoring_from_dict, new_epoch
from zicato_examples.target_0_convergence import mocks_recombine as rec_mocks

EXAMPLE_DIR = Path(_t0_pkg.__file__).resolve().parent
BOARD_PATH = EXAMPLE_DIR / "board.jsonl"

BOARD_SIZE = 5

ADAPTER_BLOCK = {
    "kind": "import",
    "factory": "zicato_examples.target_0_convergence.harness:make_adapter",
}

#: The two-marker policy template — the OC variant of the shipped
#: ``agent/policy.py``. TWO mutation points so fix A and fix B are
#: disjoint; the seeded defects fail ``has_summary`` / ``has_citations``.
TWO_MARKER_POLICY = '''\
"""Two-marker recombination-OC policy — one defect per mutation point."""

from __future__ import annotations

# zicato:mutable id="style_rules" role="writing_policy"
STYLE_RULES = "omit-summary"

# zicato:mutable id="style_rules_extra" role="writing_policy_extra"
STYLE_RULES_EXTRA = "skip-citations"

__all__ = ["STYLE_RULES", "STYLE_RULES_EXTRA"]
'''


def _expected_scalar(tokens: int, passes: int) -> float:
    drift_component = 1.0 * float(tokens)
    pass_component = 1.0 * (1.0 - float(passes) / BOARD_SIZE)
    return sum([drift_component, pass_component])


EXPECTED_V0 = _expected_scalar(tokens=2, passes=3)  # 2.4
EXPECTED_SINGLE_FIX = _expected_scalar(tokens=1, passes=4)  # 1.2
EXPECTED_UNION = _expected_scalar(tokens=0, passes=5)  # 0.0

#: Strictly between the single-fix Δ (1.2) and the union Δ (2.4).
PROMOTE_MARGIN = 1.5
#: Above even the union Δ — the dedup arm where the mint itself rejects.
DEDUP_MARGIN = 3.0

#: The OC contract's slate width. The mint replaces the LAST slot, so the
#: minting round spends exactly ``BEST_OF_N - 1`` auxiliary propose calls.
BEST_OF_N = 2


def _scoring_dict(*, promote_margin: float, recombine: bool) -> dict:
    return {
        "pass_weight": 1.0,
        "severity_weights": {"info": 1.0, "warning": 3.0, "critical": 10.0},
        "plan_revision_weight": 0.5,
        "promote_margin": promote_margin,
        "pass_rate_monotonicity": True,
        "tournament": {
            "structure": "gauntlet",
            "params": {"replicates": 1, "promote_confidence_threshold": None},
        },
        "proposer_quality": {
            "best_of_n": BEST_OF_N,
            "critique_enabled": False,
            "recombine": recombine,
        },
    }


def _bootstrap_workspace(
    tmp_path: Path, *, promote_margin: float, recombine: bool
) -> tuple[Path, str]:
    """Two-marker workspace + one epoch; v0 seeded by the production path."""
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    (agent_dir / "policy.py").write_text(TWO_MARKER_POLICY, encoding="utf-8")

    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    (workspace / "config.json").write_text(
        json.dumps(
            {
                "instance_id": "default",
                "created_at": "2026-07-01T00:00:00Z",
                "generation_source_backend": "directory",
                "adapter": ADAPTER_BLOCK,
                "mutable_trees": [str(agent_dir)],
            }
        )
    )
    brief = tmp_path / "brief.md"
    brief.write_text(
        "# Recombination brief\n"
        "- Remove defect tokens from the writing policy.\n"
        "- Never fabricate metrics.\n"
    )
    weights = _scoring_from_dict(_scoring_dict(promote_margin=promote_margin, recombine=recombine))
    cfg = new_epoch(
        workspace,
        name="t0-recombination",
        board_source=BOARD_PATH,
        brief_source=brief,
        weights=weights,
        auto_close_previous=False,
        contract=resolved_contract_with_proposer(workspace, EXAMPLE_DIR / "proposer"),
    )
    return workspace, cfg.id


def _run_rounds(
    workspace: Path, epoch_id: str, rounds: int, *, max_consecutive_rejections: int = 3
) -> list:
    from zicato.evolve.loop import evolve_n_rounds
    from zicato_examples.target_0_convergence import mocks as t0_mocks

    return asyncio.run(
        evolve_n_rounds(
            rounds=rounds,
            workspace_root=workspace,
            epoch_id=epoch_id,
            harness_call_llm=t0_mocks.harness_llm,
            auxiliary_call_llm=rec_mocks.aux_llm,
            auto_epoch=False,
            max_consecutive_rejections=max_consecutive_rejections,
        )
    )


def test_recombination_promotes_where_singles_reject(tmp_path: Path) -> None:
    """The 3-round known-answer: reject, reject, union minted → PROMOTED."""
    workspace, epoch_id = _bootstrap_workspace(
        tmp_path, promote_margin=PROMOTE_MARGIN, recombine=True
    )
    rec_mocks.reset()
    outcomes = _run_rounds(workspace, epoch_id, 3)

    # --- (a) The decision sequence: two sub-margin rejects, one promote.
    assert [o.tournament_decision for o in outcomes] == ["rejected", "rejected", "promoted"]
    assert [o.proposed_generation_id for o in outcomes] == ["v1", "v2", "v3"]
    assert [o.parent_generation_id for o in outcomes] == ["v0", "v0", "v0"]

    # --- (b) The exact arithmetic (σ = 0, hand-computable).
    r1, r2, r3 = outcomes
    assert r1.parent_scalar == EXPECTED_V0 == 2.4
    assert r1.child_scalar == EXPECTED_SINGLE_FIX == 1.2
    assert r2.parent_scalar == EXPECTED_V0
    assert r2.child_scalar == EXPECTED_SINGLE_FIX
    assert r3.parent_scalar == EXPECTED_V0
    assert r3.child_scalar == EXPECTED_UNION == 0.0
    # Each single fix improved by 1.2 — real, but strictly under the 1.5
    # margin; the union's 2.4 clears it. The margin sits strictly between.
    assert EXPECTED_V0 - EXPECTED_SINGLE_FIX == 1.2 < PROMOTE_MARGIN
    assert EXPECTED_V0 - EXPECTED_UNION == 2.4 > PROMOTE_MARGIN

    # --- (c) Provenance: the journaled union carries the machine field.
    from zicato.epoch.journal import read_experiment

    v3 = read_experiment(workspace, epoch_id, "v3")
    assert v3.recombined_from == ("v1", "v2")
    assert v3.hypothesis.core_idea.startswith("[recombined]")
    assert sorted(p.mutation_id for p in v3.patches) == ["style_rules", "style_rules_extra"]
    # The single fixes are ordinary experiments — no conditional key.
    for gid in ("v1", "v2"):
        exp = read_experiment(workspace, epoch_id, gid)
        assert exp.recombined_from == ()
        body = json.loads(
            (workspace / "epochs" / epoch_id / "generations" / gid / "experiment.json").read_text()
        )
        assert "recombined_from" not in body

    # --- (d) The RoundLog flags + the selection short-circuit.
    from zicato.epoch.round_log import CandidateSampled, RoundLog, fold_round_record

    for round_index, expect_recombined in ((0, 0), (1, 0), (2, 1)):
        events = RoundLog(workspace, epoch_id, round_index).read()
        record = fold_round_record(events)
        assert record.complete, round_index
        assert record.proposal.candidates_sampled == BEST_OF_N, round_index
        assert record.proposal.recombined_sampled == expect_recombined, round_index
    round3 = fold_round_record(RoundLog(workspace, epoch_id, 2).read())
    assert round3.proposal.critique_reason == "recombined"
    assert round3.proposal.critique_index == BEST_OF_N - 1
    sampled = [
        e.event
        for e in RoundLog(workspace, epoch_id, 2).read()
        if isinstance(e.event, CandidateSampled)
    ]
    assert [s.recombined for s in sampled] == [False, True]

    # --- (e) The COST-NEUTRALITY counter: the mint REPLACED round 3's last
    # slot's auxiliary propose call. Rounds 1-2 spent BEST_OF_N calls each;
    # round 3 spent exactly BEST_OF_N - 1.
    assert rec_mocks.proposer_calls() == 2 * BEST_OF_N + (BEST_OF_N - 1)

    # The promoted head advanced to the union.
    marker = workspace / "epochs" / epoch_id / "current_generation"
    assert marker.read_text().strip() == "v3"

    # The union's per-entry floor: zero drift, all five predicates pass.
    from zicato.board.jsonl import load_board
    from zicato.core.workspace import board_path, loss_profile_path
    from zicato.telemetry.reducer import read_loss_profile

    for entry in load_board(board_path(workspace, epoch_id)):
        profile = read_loss_profile(loss_profile_path(workspace, epoch_id, "v3", entry.id))
        assert profile.drift_loss == 0.0, entry.id
        assert profile.pass_fail is True, entry.id


def test_stall_control_same_script_recombine_off_stays_v0(tmp_path: Path) -> None:
    """The V2 inverse: identical script, ``recombine`` OFF ⇒ v0 stalls.

    Same policy, same board, same margin, same scripted proposer — the
    only difference is the knob. Without the mechanical slot the loop can
    only re-propose the two sub-margin single fixes, so after the same 3
    rounds the champion is still v0. Recombination — nothing else — is
    what converts the two rejected complements into the promotion above.
    """
    workspace, epoch_id = _bootstrap_workspace(
        tmp_path, promote_margin=PROMOTE_MARGIN, recombine=False
    )
    rec_mocks.reset()
    outcomes = _run_rounds(workspace, epoch_id, 3)

    assert [o.tournament_decision for o in outcomes] == ["rejected", "rejected", "rejected"]
    marker = workspace / "epochs" / epoch_id / "current_generation"
    assert marker.read_text().strip() == "v0"

    # No mint anywhere: every sampled candidate was an ordinary LLM sample
    # and no experiment carries recombination provenance.
    from zicato.epoch.journal import read_experiment
    from zicato.epoch.round_log import RoundLog, fold_round_record

    for round_index in range(3):
        record = fold_round_record(RoundLog(workspace, epoch_id, round_index).read())
        assert record.proposal.recombined_sampled == 0, round_index
    for gid in ("v1", "v2", "v3"):
        assert read_experiment(workspace, epoch_id, gid).recombined_from == ()
    # Off-knob rounds spend the full slate budget every round.
    assert rec_mocks.proposer_calls() == 3 * BEST_OF_N


def test_pair_dedup_a_persisted_rejected_union_never_reminits(tmp_path: Path) -> None:
    """Selector predicate #5 end to end: a round-spending mint never re-mints.

    ``promote_margin = 3.0`` sits above even the union's Δ 2.4, so round
    3's mint REJECTS and persists with its ``recombined_from`` provenance.
    Round 4's selector must skip the (v1, v2) pair — the persisted
    frozenset is in the tried set — and the union itself is excluded as a
    parent (predicate #4: no chains), so round 4 mints NOTHING and samples
    the full slate normally.
    """
    workspace, epoch_id = _bootstrap_workspace(
        tmp_path, promote_margin=DEDUP_MARGIN, recombine=True
    )
    rec_mocks.reset()
    # Rounds 1-3 all reject, so the consecutive-rejection breaker must be
    # disabled for round 4 to run (the round under test).
    outcomes = _run_rounds(workspace, epoch_id, 4, max_consecutive_rejections=0)

    assert [o.tournament_decision for o in outcomes] == ["rejected"] * 4
    marker = workspace / "epochs" / epoch_id / "current_generation"
    assert marker.read_text().strip() == "v0"

    from zicato.epoch.journal import read_experiment
    from zicato.epoch.round_log import RoundLog, fold_round_record

    # Round 3 DID mint the union (Δ 2.4 < 3.0 → rejected + persisted).
    v3 = read_experiment(workspace, epoch_id, "v3")
    assert v3.recombined_from == ("v1", "v2")
    assert v3.outcome is not None and v3.outcome.tournament_decision == "rejected"
    round3 = fold_round_record(RoundLog(workspace, epoch_id, 2).read())
    assert round3.proposal.recombined_sampled == 1

    # Round 4 did NOT re-mint: no recombined sample, no provenance, and the
    # full slate budget was spent on ordinary samples.
    v4 = read_experiment(workspace, epoch_id, "v4")
    assert v4.recombined_from == ()
    round4 = fold_round_record(RoundLog(workspace, epoch_id, 3).read())
    assert round4.proposal.recombined_sampled == 0
    assert round4.proposal.candidates_sampled == BEST_OF_N
    # Cost: rounds 1-2 full slate, round 3 saved one call, round 4 full.
    assert rec_mocks.proposer_calls() == 2 * BEST_OF_N + (BEST_OF_N - 1) + BEST_OF_N
