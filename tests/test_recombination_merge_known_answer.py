"""Known-answer LLM-guided recombination merge (WS-MERGE) — the FULL loop.

The ``recombine_merge = "llm"`` counterpart to
``tests/test_recombination_known_answer.py``, driven over a SINGLE-marker
variant of the convergence target whose two single-fix challengers touch the
SAME mutation id, so their patch sets OVERLAP:

    # zicato:mutable id="style_rules"  → seeds "omit-summary;skip-citations"

The mechanical mint REQUIRES a disjoint pair (selector predicate #7), so on
this fixture mechanical mode selects NOTHING (the control below). Only an LLM
merge can compose the union — a single edit removing BOTH defect tokens.

The arithmetic (σ = 0, ``info = 1.0``, the ``drift:`` channel and
``pass_weight`` both at ``1.0``,
5-entry board), from the shipped scoring formulas:

    scalar(k tokens, p passes) = k + (1 - p/5)

    v0  ("omit-summary;skip-citations", 2 tok, 3/5) = 2.4
    fix A ("skip-citations",            1 tok, 4/5) = 1.2   — Δ 1.2 < 1.5 REJECT
    fix B ("omit-summary",              1 tok, 4/5) = 1.2   — Δ 1.2 < 1.5 REJECT
    the LLM merge (""      , 0 tok, 5/5)            = 0.0   — Δ 2.4 > 1.5 PROMOTE

Round 3's last best-of-N slot issues ONE merge call (recognised by the merge
prompt marker) that returns the union; it flows through the normal proposal
parse/validate path, is stamped with ``recombined_from = (v1, v2)``, and the
unchanged gate promotes it. The cost story: the merge call SUBSTITUTES the
slot's own sample call, so an ``"llm"`` recombining round spends exactly
``best_of_n`` calls (a recombine-off round) — pinned by the call counter.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import zicato_examples.target_0_convergence as _t0_pkg
from tests._contract_pins import resolved_contract_with_proposer
from tests._foe_support import stand_in_proposer_block
from zicato.epoch.lifecycle import _scoring_from_dict, new_epoch
from zicato_examples.target_0_convergence import mocks_recombine_merge as merge_mocks

EXAMPLE_DIR = Path(_t0_pkg.__file__).resolve().parent
BOARD_PATH = EXAMPLE_DIR / "board.jsonl"

BOARD_SIZE = 5

ADAPTER_BLOCK = {
    "kind": "import",
    "factory": "zicato_examples.target_0_convergence.harness:make_adapter",
}

#: The SINGLE-marker policy — one mutation point carrying BOTH defect tokens,
#: so fix A and fix B both patch ``style_rules`` (they OVERLAP).
SINGLE_MARKER_POLICY = '''\
"""Single-marker recombination-merge-OC policy — two defects on one point."""

from __future__ import annotations

# zicato:mutable id="style_rules" role="writing_policy"
STYLE_RULES = "omit-summary;skip-citations"

__all__ = ["STYLE_RULES"]
'''


def _expected_scalar(tokens: int, passes: int) -> float:
    drift_component = 1.0 * float(tokens)
    pass_component = 1.0 * (1.0 - float(passes) / BOARD_SIZE)
    return sum([drift_component, pass_component])


EXPECTED_V0 = _expected_scalar(tokens=2, passes=3)  # 2.4
EXPECTED_SINGLE_FIX = _expected_scalar(tokens=1, passes=4)  # 1.2
EXPECTED_UNION = _expected_scalar(tokens=0, passes=5)  # 0.0

PROMOTE_MARGIN = 1.5

#: The OC contract's slate width. In ``"llm"`` mode the merge call substitutes
#: the last slot's sample call, so a recombining round spends exactly BEST_OF_N.
BEST_OF_N = 2


def _scoring_dict(*, merge_mode: str | None) -> dict:
    proposer_quality: dict = {
        "best_of_n": BEST_OF_N,
        "critique_enabled": False,
        "recombine": True,
    }
    if merge_mode is not None:
        proposer_quality["recombine_merge"] = merge_mode
    return {
        "pass_weight": 1.0,
        "severity_weights": {"info": 1.0, "warning": 3.0, "critical": 10.0},
        "plan_revision_weight": 0.5,
        "promote_margin": PROMOTE_MARGIN,
        "pass_rate_monotonicity": True,
        "tournament": {
            "structure": "gauntlet",
            "params": {"replicates": 1, "promote_confidence_threshold": None},
        },
        "proposer_quality": proposer_quality,
    }


def _bootstrap_workspace(tmp_path: Path, *, merge_mode: str | None) -> tuple[Path, str]:
    """Single-marker workspace + one epoch; v0 seeded by the production path."""
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    (agent_dir / "policy.py").write_text(SINGLE_MARKER_POLICY, encoding="utf-8")

    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    (workspace / "config.json").write_text(
        json.dumps(
            {
                "instance_id": "default",
                "proposer": stand_in_proposer_block(
                    tmp_path / "foe", contents=merge_mocks.SLATE_POLICIES
                ),
                "created_at": "2026-07-01T00:00:00Z",
                "generation_source_backend": "directory",
                "adapter": ADAPTER_BLOCK,
                "mutable_trees": [str(agent_dir)],
            }
        )
    )
    brief = tmp_path / "brief.md"
    brief.write_text(
        "# Recombination-merge brief\n"
        "- Remove defect tokens from the writing policy.\n"
        "- Never fabricate metrics.\n"
    )
    weights = _scoring_from_dict(_scoring_dict(merge_mode=merge_mode))
    cfg = new_epoch(
        workspace,
        name="t0-recombination-merge",
        board_source=BOARD_PATH,
        brief_source=brief,
        weights=weights,
        auto_close_previous=False,
        contract=resolved_contract_with_proposer(workspace, EXAMPLE_DIR / "proposer"),
    )
    return workspace, cfg.id


def _run_rounds(workspace: Path, epoch_id: str, rounds: int, *, aux) -> list:
    from zicato.evolve.loop import evolve_n_rounds
    from zicato_examples.target_0_convergence import mocks as t0_mocks

    return asyncio.run(
        evolve_n_rounds(
            rounds=rounds,
            workspace_root=workspace,
            epoch_id=epoch_id,
            harness_call_llm=t0_mocks.harness_llm,
            auxiliary_call_llm=aux,
            auto_epoch=False,
            max_consecutive_rejections=3,
        )
    )


def _proposal_episodes(workspace: Path, epoch_id: str) -> int:
    """How many proposal episodes this epoch ran, off its durable record.

    Filtered to the proposal role: the critique and merge calls land in
    the same capture under their own roles, and are not episodes.
    """
    from zicato.proposer.input_capture import ROLE_PROPOSAL, read_proposer_inputs

    records = read_proposer_inputs(workspace, epoch_id)
    return sum(1 for r in records if r.get("role") == ROLE_PROPOSAL)


def test_llm_merge_promotes_over_overlapping_pair(tmp_path: Path) -> None:
    """The 3-round known-answer: reject, reject, LLM merge → PROMOTED."""
    workspace, epoch_id = _bootstrap_workspace(tmp_path, merge_mode="llm")
    merges_before = merge_mocks.merge_calls()
    outcomes = _run_rounds(workspace, epoch_id, 3, aux=merge_mocks.aux_llm)

    # --- (a) The decision sequence: two sub-margin rejects, one promote.
    assert [o.tournament_decision for o in outcomes] == ["rejected", "rejected", "promoted"]
    assert [o.proposed_generation_id for o in outcomes] == ["v1", "v2", "v3"]

    # --- (b) The exact arithmetic.
    r1, r2, r3 = outcomes
    assert r1.child_scalar == EXPECTED_SINGLE_FIX == 1.2
    assert r2.child_scalar == EXPECTED_SINGLE_FIX
    assert r3.parent_scalar == EXPECTED_V0 == 2.4
    assert r3.child_scalar == EXPECTED_UNION == 0.0

    # --- (c) Provenance: the LLM merge carries the SAME machine field as a
    # mechanical mint, and its single union patch removed both defects.
    from zicato.epoch.journal import read_experiment

    v3 = read_experiment(workspace, epoch_id, "v3")
    assert v3.recombined_from == ("v1", "v2")
    assert [p.mutation_id for p in v3.patches] == ["style_rules"]
    assert v3.patches[0].new_content.strip() == ""
    # The overlapping single fixes are ordinary experiments — no provenance.
    for gid in ("v1", "v2"):
        assert read_experiment(workspace, epoch_id, gid).recombined_from == ()

    # --- (d) The RoundLog flags + the selection short-circuit (merge is a mint).
    from zicato.epoch.round_log import CandidateSampled, RoundLog, fold_round_record

    for round_index, expect_recombined in ((0, 0), (1, 0), (2, 1)):
        record = fold_round_record(RoundLog(workspace, epoch_id, round_index).read())
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

    # --- (e) The COST story: an "llm" recombining round spends exactly
    # BEST_OF_N proposals — the merge SUBSTITUTES the last slot's episode,
    # so round 3 ran BEST_OF_N - 1 episodes and made one merge call, and
    # three rounds together cost what three recombine-off rounds cost.
    assert _proposal_episodes(workspace, epoch_id) == 3 * BEST_OF_N - 1
    assert merge_mocks.merge_calls() - merges_before == 1

    marker = workspace / "epochs" / epoch_id / "current_generation"
    assert marker.read_text().strip() == "v3"


def test_mechanical_mode_mints_nothing_on_overlapping_pair(tmp_path: Path) -> None:
    """The CONTROL: the SAME overlapping fixture, mechanical mode ⇒ v0 stalls.

    Mechanical mint requires a DISJOINT pair (predicate #7); the two single
    fixes share ``style_rules``, so no pair is ever selected and the last slot
    just samples the LLM. The loop can only re-propose the two sub-margin
    single fixes, so the champion is still v0 after 3 rounds. The LLM merge —
    nothing else — is what promotes in the sibling test.
    """
    workspace, epoch_id = _bootstrap_workspace(tmp_path, merge_mode="mechanical")
    merges_before = merge_mocks.merge_calls()
    outcomes = _run_rounds(workspace, epoch_id, 3, aux=merge_mocks.aux_llm)

    assert [o.tournament_decision for o in outcomes] == ["rejected", "rejected", "rejected"]
    marker = workspace / "epochs" / epoch_id / "current_generation"
    assert marker.read_text().strip() == "v0"

    from zicato.epoch.journal import read_experiment
    from zicato.epoch.round_log import RoundLog, fold_round_record

    for round_index in range(3):
        record = fold_round_record(RoundLog(workspace, epoch_id, round_index).read())
        assert record.proposal.recombined_sampled == 0, round_index
    for gid in ("v1", "v2", "v3"):
        assert read_experiment(workspace, epoch_id, gid).recombined_from == ()
    # No merge call was ever issued (no pair) — every round ran the full slate.
    assert _proposal_episodes(workspace, epoch_id) == 3 * BEST_OF_N
    assert merge_mocks.merge_calls() == merges_before


def test_llm_merge_garbage_response_degrades_to_fresh_sample(tmp_path: Path) -> None:
    """A garbage merge response DEGRADES the slot to a fresh sample; round runs.

    The merge parse fails, so the last slot falls back to the normal sample
    body (the mechanical mint's exact degrade). No mint is recorded, the round
    completes normally (a merge failure must never fail a propose), and the
    champion stays v0 (both fallback samples are sub-margin single fixes).
    """
    workspace, epoch_id = _bootstrap_workspace(tmp_path, merge_mode="llm")
    outcomes = _run_rounds(workspace, epoch_id, 3, aux=_garbage_merge_aux)

    assert [o.tournament_decision for o in outcomes] == ["rejected", "rejected", "rejected"]
    marker = workspace / "epochs" / epoch_id / "current_generation"
    assert marker.read_text().strip() == "v0"

    from zicato.epoch.journal import read_experiment
    from zicato.epoch.round_log import RoundLog, fold_round_record

    round3 = fold_round_record(RoundLog(workspace, epoch_id, 2).read())
    assert round3.complete
    assert round3.proposal.recombined_sampled == 0
    assert read_experiment(workspace, epoch_id, "v3").recombined_from == ()


async def _garbage_merge_aux(system: str, user: str, model: str, **kwargs) -> str:
    """Answer every auxiliary site normally but GARBAGE on the merge call."""
    if merge_mocks.MERGE_MARKER in user:
        return "this is not a JSON object at all — {oops"
    return await merge_mocks.aux_llm(system, user, model, **kwargs)


def test_contract_hash_pins_recombine_merge() -> None:
    """Default ``"mechanical"`` is omit-at-default; ``"llm"`` rolls the epoch."""
    from zicato.epoch.contract import scoring_to_canon

    def _canon(merge_mode: str | None) -> dict:
        return scoring_to_canon(_scoring_from_dict(_scoring_dict(merge_mode=merge_mode)))

    omitted = _canon(None)
    mechanical = _canon("mechanical")
    llm = _canon("llm")

    # At the default the key is absent from the canonical form (byte-identical
    # to a contract that predates the field) — both directions.
    assert mechanical == omitted
    assert "recombine_merge" not in json.dumps(mechanical)
    # "llm" reintroduces the key and changes the canonical form (rolls).
    assert llm != mechanical
    assert '"recombine_merge": "llm"' in json.dumps(llm, sort_keys=True)
