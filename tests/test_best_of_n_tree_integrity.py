"""Selected best-of-three candidates agree with stored and measured trees.

Two complete rounds use the planted-defect example, real proposal episodes,
subprocess tournament workers, and the Git generation store. The gauntlet
samples its slate serially; the two-challenger racing field samples each
slate concurrently. The critic selects slot zero while the final slot carries
a worse policy, so incorrect tree selection changes the known scalar.

Both rounds check proposal repair, persisted patches, committed and mounted
tree agreement, containment, and scratch cleanup. The field also checks that
its distinct hypotheses describe distinct applied policies.

Both structures use the same candidate-production owner. Direct tests in
``test_slate_concurrency.py`` compare serial and concurrent slate results and
events, force out-of-order completion with real Git mounts, and check cleanup
after cancellation or sibling failure. Those tests cover scheduling variations
without repeating the complete tournament for each structure and width.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import pytest

import zicato_examples.target_0_convergence as _t0_pkg
from tests import _best_of_n_slate_support as slate_mocks
from tests._contract_pins import resolved_contract_with_proposer
from tests._foe_support import stand_in_proposer_block
from zicato.epoch.lifecycle import _scoring_from_dict, new_epoch

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
EXPECTED_V0 = 3.6  # 3 tokens, 2/5 pass
EXPECTED_FLOOR = 1.2  # 1 token (verbose-prose), 4/5 pass
EXPECTED_TWO_TOKENS = 2.4  # 2 tokens, 3/5 pass


def _bootstrap(
    tmp_path: Path,
    tournament: dict,
    *,
    propose_parallelism: int = 4,
    policies: dict[str, dict[str, str]],
) -> tuple[Path, str]:
    """A target_0 workspace whose contract samples a best-of-3 slate.

    ``propose_parallelism`` is written into the workspace ``runtime`` block so
    the slate gather runs at the requested width (1 = serial reference; 4 =
    the concurrent gather). It is a RUNTIME knob — never part of the frozen
    contract — so it does not perturb the known-answer scalars.

    ``policies`` is what each slate slot's episode writes, keyed
    ``<candidate>#<slot>``, so the slate's slots are known answers rather
    than whatever a proposer happened to invent.
    """
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    (workspace / "config.json").write_text(
        json.dumps(
            {
                "instance_id": "default",
                "proposer": stand_in_proposer_block(
                    tmp_path / "foe", contents=policies, break_first=1
                ),
                "generation_source_backend": "git",
                "created_at": "2026-07-01T00:00:00Z",
                "adapter": {**ADAPTER_BLOCK, "mutable_trees": [str(AGENT_DIR)]},
                "runtime": {"propose_parallelism": propose_parallelism},
                "models": {
                    "engines": {
                        "target": {"call_llm": "tests._best_of_n_slate_support:target_llm"},
                        "evaluation": {"call_llm": "tests._best_of_n_slate_support:slate_aux_llm"},
                    }
                },
            }
        )
    )
    brief = tmp_path / "brief.md"
    brief.write_text("# Slate brief\n- Remove defect tokens from the writing policy.\n")

    scoring = json.loads(SCORING_PATH.read_text())
    scoring["tournament"] = tournament
    # The surface under test: a real best-of-3 slate with the self-critique
    # pass enabled (the critic is scripted to pick candidate 0 — never the
    # last-validated slot).
    scoring["proposer_quality"] = {"best_of_n": 3}
    weights = _scoring_from_dict(scoring)
    cfg = new_epoch(
        workspace,
        name="t0-slate-integrity",
        board_source=BOARD_PATH,
        brief_source=brief,
        weights=weights,
        auto_close_previous=False,
        contract=resolved_contract_with_proposer(workspace, EXAMPLE_DIR / "proposer"),
    )
    return workspace, cfg.id


def _assert_no_scratch_residue(
    workspace: Path, epoch_id: str, scratch_tmp: Path, expected_ids: set[str]
) -> None:
    """The WS-CONC post-condition: no scratch tree survived, anywhere.

    Two checks:

    * **Namespace** — ``list_generations`` returns EXACTLY ``expected_ids``,
      so no per-slot scratch tree ever entered the generation namespace (the
      ``derive_scratch`` off-namespace guarantee — a scratch tree is invisible
      to every walker).
    * **Temp dir** — no ``ztw-slate-*`` slate-scratch parent survives under
      the (test-local) temp dir; every slot's ``try/finally`` cleaned its
      lease.
    """
    from zicato.epoch.genstore import default_generation_store

    store = default_generation_store(workspace)
    assert (
        set(store.list_generations(epoch_id)) == expected_ids
    ), "a scratch derivation leaked into the generation namespace"
    leaked = list(scratch_tmp.glob("ztw-slate-*"))
    assert leaked == [], f"slate scratch parents survived the round: {leaked}"


def _policy_text(workspace: Path, epoch_id: str, generation_id: str) -> str:
    """The generation's policy source, read from BOTH store surfaces.

    The two must agree: ``read_file`` reads the committed tree (what a later
    epoch would be seeded from), ``snapshot_root`` is the materialised
    worktree path the orchestrator records as ``Generation.snapshot_root``
    (what the gate's regression scan and any direct reader consume).
    """
    from zicato.epoch.containment import attest_generation
    from zicato.epoch.genstore import default_generation_store
    from zicato.epoch.journal import read_experiment

    store = default_generation_store(workspace)
    committed = store.read_file(epoch_id, generation_id, "agent/policy.py").decode()
    mounted = (
        Path(store.materialize_snapshot(epoch_id, generation_id)) / "agent" / "policy.py"
    ).read_text()
    assert committed == mounted, (
        f"{generation_id}: the committed tree and the materialised snapshot " f"worktree diverged"
    )
    experiment = read_experiment(workspace, epoch_id, generation_id)
    assert experiment.parent_generation_id is not None
    attestation = attest_generation(
        workspace,
        epoch_id=epoch_id,
        parent_generation_id=experiment.parent_generation_id,
        generation_id=generation_id,
        parent_root=store.snapshot_path(epoch_id, experiment.parent_generation_id),
        child_root=store.snapshot_path(epoch_id, generation_id),
    )
    assert attestation.status == "contained", attestation
    episode = workspace / "epochs" / epoch_id / "episodes" / f"{generation_id}-0" / "episode.jsonl"
    events = [json.loads(line) for line in episode.read_text().splitlines()]
    verdicts = [
        event["data"]["value"]
        for event in events
        if event["type"] == "tool/result" and event["data"]["name"] == "validate_patches"
    ]
    assert verdicts[0], "the selected slate slot did not exercise repair"
    assert verdicts[-1] == []
    return committed


def _style_rules_line(policy: str) -> str:
    """The ``STYLE_RULES = ...`` assignment line — the mutated span.

    The module docstring above it NAMES every known token, so containment
    assertions must scope to the assignment the patch actually rewrote.
    """
    lines = [ln for ln in policy.splitlines() if ln.startswith("STYLE_RULES")]
    assert len(lines) == 1, f"expected exactly one STYLE_RULES line, got {lines!r}"
    return lines[0]


def _patch_content(workspace: Path, epoch_id: str, generation_id: str) -> str:
    """The token list the persisted patch sets, read out of its literal.

    A span patch carries the applier's unit — the string-literal source —
    so the token list is what that literal encloses.
    """
    from zicato.epoch.journal import read_experiment

    experiment = read_experiment(workspace, epoch_id, generation_id)
    assert len(experiment.patches) == 1
    return str(experiment.patches[0].new_content).strip().strip("\"'")


def test_gauntlet_mounts_the_chosen_candidate_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Serial proposal sampling persists and scores the critic's chosen tree."""
    # Route the in-process slate scratch (``ztw-slate-*``) into a test-local
    # temp dir so the residue check cannot collide with a sibling xdist worker.
    scratch_tmp = tmp_path / "tmp"
    scratch_tmp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch_tmp))
    workspace, epoch_id = _bootstrap(
        tmp_path,
        {
            "structure": "gauntlet",
            "params": {"replicates": 1, "promote_confidence_threshold": None},
        },
        propose_parallelism=1,
        policies=slate_mocks.GAUNTLET_POLICIES,
    )

    from zicato.evolve.loop import evolve_n_rounds

    outcomes = asyncio.run(
        evolve_n_rounds(
            rounds=1,
            workspace_root=workspace,
            epoch_id=epoch_id,
            target_call_llm=slate_mocks.target_llm,
            evaluation_call_llm=slate_mocks.slate_aux_llm,
            auto_epoch=False,
        )
    )

    assert len(outcomes) == 1
    outcome = outcomes[0]
    # The chosen candidate (slot 0: only verbose-prose left) is the known
    # floor — the decision + scalar prove which tree the tournament SCORED.
    # The last-sampled decoy would have scored 4.8 and been rejected.
    assert outcome.tournament_decision == "promoted"
    assert outcome.proposed_generation_id == "v1"
    assert outcome.parent_scalar == EXPECTED_V0
    assert outcome.child_scalar == EXPECTED_FLOOR

    # The persisted experiment is the CHOSEN candidate...
    assert _patch_content(workspace, epoch_id, "v1") == slate_mocks.GAUNTLET_CHOSEN_CONTENT
    # ...and the persisted generation tree carries exactly its content (the
    # mutated span, not the docstring that names every known token).
    rules = _style_rules_line(_policy_text(workspace, epoch_id, "v1"))
    assert slate_mocks.GAUNTLET_CHOSEN_CONTENT in rules
    assert "fabricate-metrics" not in rules
    assert "omit-summary" not in rules

    # The round log proves the critique path really selected a NON-last slot
    # (index 0 of a 3-slate) — the exact coordinate the bug corrupted.
    rlog_path = workspace / "epochs" / epoch_id / "rounds" / "0" / "round_log.jsonl"
    events = [json.loads(line) for line in rlog_path.read_text().splitlines() if line.strip()]
    sampled = [e for e in events if e.get("type") == "candidate_sampled"]
    assert len(sampled) == 3
    selected = [e for e in events if e.get("type") == "critique_selected"]
    assert len(selected) == 1
    assert selected[0]["payload"]["index"] == 0
    assert selected[0]["payload"]["reason"] == "critique"

    # WS-CONC post-condition: the chosen tree is mounted and NO scratch tree
    # survived — not in the namespace, not on disk.
    _assert_no_scratch_residue(workspace, epoch_id, scratch_tmp, {"v0", "v1"})


def test_field_mounts_each_chosen_candidate_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Concurrent slates preserve each field candidate's tree and hypothesis."""
    scratch_tmp = tmp_path / "tmp"
    scratch_tmp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch_tmp))
    workspace, epoch_id = _bootstrap(
        tmp_path,
        {
            "structure": "racing",
            "params": {
                "field_size": 2,
                "replicates": 1,
                "eta": 2,
                "board_fraction": 0.4,
            },
        },
        propose_parallelism=4,
        policies=slate_mocks.FIELD_POLICIES,
    )

    from zicato.evolve.loop import evolve_n_rounds

    outcomes = asyncio.run(
        evolve_n_rounds(
            rounds=1,
            workspace_root=workspace,
            epoch_id=epoch_id,
            target_call_llm=slate_mocks.target_llm,
            evaluation_call_llm=slate_mocks.slate_aux_llm,
            auto_epoch=False,
        )
    )

    assert len(outcomes) == 1
    outcome = outcomes[0]
    # v1's chosen candidate (only verbose-prose) is the strictly best arm on
    # every board slice, so it must survive every rung and be promoted at
    # the known floor. Pre-fix, BOTH arms' mounted trees were the identical
    # fabricate-metrics decoy (the last slate slot), collapsing the field.
    assert outcome.tournament_decision == "promoted"
    assert outcome.proposed_generation_id == "v1"
    assert outcome.parent_scalar == EXPECTED_V0
    assert outcome.child_scalar == EXPECTED_FLOOR

    from zicato.epoch.journal import read_experiment
    from zicato.evolve.propose_apply import _diversity_signature

    signatures = []
    for gid, chosen_content in zip(("v1", "v2"), slate_mocks.FIELD_CHOSEN_CONTENTS, strict=True):
        # Tree/experiment agreement per challenger: the persisted experiment
        # is the chosen candidate and the mounted tree carries its content.
        assert _patch_content(workspace, epoch_id, gid) == chosen_content, gid
        rules = _style_rules_line(_policy_text(workspace, epoch_id, gid))
        assert chosen_content in rules, gid
        assert "fabricate-metrics" not in rules, gid
        # The diversity signature the field judged is computed from this
        # same experiment — with the tree now matching it, the signature
        # describes the tree that actually raced.
        signatures.append(_diversity_signature(read_experiment(workspace, epoch_id, gid)))
    # The two chosen hypotheses are genuinely distinct (the field did not
    # collapse), matching their genuinely distinct trees.
    assert signatures[0] != signatures[1]

    # WS-CONC post-condition: exactly the two challenger trees are mounted and
    # NO per-slot scratch survived (namespace or temp dir) across the whole
    # sequential-field × concurrent-slate fan-out.
    _assert_no_scratch_residue(workspace, epoch_id, scratch_tmp, {"v0", "v1", "v2"})
