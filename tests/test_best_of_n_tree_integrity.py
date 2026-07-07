"""Best-of-N tree integrity — the MOUNTED child tree matches the SELECTION.

Known-answer e2e over the target_0 planted-defect contract with
``proposer_quality.best_of_n = 3`` (the shipped DEFAULT slate width): every
slate sample's post-apply validation derives the SAME fixed child snapshot in
place, so before the fix the on-disk child tree belonged to the LAST-sampled
candidate while the critique could choose an EARLIER one — the tournament
then scored (and the field path diversity-judged) a tree that was not the
experiment on record.

Both evolve pipelines are driven for real (subprocess tournament workers, the
default git generation store):

* GAUNTLET — a scripted 3-candidate slate whose critic picks candidate 0
  (the best token set, the known floor) while the LAST-sampled candidate is a
  strictly worse ``fabricate-metrics`` decoy. The round must promote at the
  chosen candidate's exact known scalar and the persisted generation tree
  must carry the chosen candidate's patch content, not the decoy's.
* FIELD (racing, field_size=2) — per-challenger slates with the same shape.
  Every applied challenger's tree must agree with its persisted experiment
  (the same experiment whose hypothesis signature the field-diversity check
  judged), and the best chosen arm must survive to the known floor.

The slate payloads + the scripted critic live in the importable
module-level :mod:`tests._best_of_n_slate_support` (worker-boundary rule:
role callables cross the subprocess boundary as dotted paths).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import zicato_examples.target_0_convergence as _t0_pkg
from tests import _best_of_n_slate_support as slate_mocks
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


def _bootstrap(tmp_path: Path, tournament: dict) -> tuple[Path, str]:
    """A target_0 workspace whose contract samples a best-of-3 slate."""
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
        proposer_path=EXAMPLE_DIR / "proposer",
    )
    return workspace, cfg.id


def _policy_text(workspace: Path, epoch_id: str, generation_id: str) -> str:
    """The generation's policy source, read from BOTH store surfaces.

    The two must agree: ``read_file`` reads the committed tree (what a later
    epoch would be seeded from), ``snapshot_root`` is the materialised
    worktree path the orchestrator records as ``Generation.snapshot_root``
    (what the gate's regression scan and any direct reader consume).
    """
    from zicato.epoch.genstore import default_generation_store

    store = default_generation_store(workspace)
    committed = store.read_file(epoch_id, generation_id, "agent/policy.py").decode()
    mounted = (
        Path(store.snapshot_root(epoch_id, generation_id)) / "agent" / "policy.py"
    ).read_text()
    assert committed == mounted, (
        f"{generation_id}: the committed tree and the materialised snapshot " f"worktree diverged"
    )
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
    from zicato.epoch.journal import read_experiment

    experiment = read_experiment(workspace, epoch_id, generation_id)
    assert len(experiment.patches) == 1
    return str(experiment.patches[0].new_content)


def test_gauntlet_mounts_the_chosen_candidate_tree(tmp_path: Path) -> None:
    """The critic picks candidate 0; the round must score + persist THAT
    candidate's tree, not the last-sampled decoy's."""
    workspace, epoch_id = _bootstrap(
        tmp_path,
        {
            "structure": "gauntlet",
            "params": {"replicates": 1, "promote_confidence_threshold": None},
        },
    )
    slate_mocks.reset()

    from zicato.evolve.loop import evolve_n_rounds

    outcomes = asyncio.run(
        evolve_n_rounds(
            rounds=1,
            workspace_root=workspace,
            epoch_id=epoch_id,
            harness_call_llm=slate_mocks.harness_llm,
            auxiliary_call_llm=slate_mocks.gauntlet_slate_aux_llm,
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


def test_field_mounts_each_chosen_candidate_tree(tmp_path: Path) -> None:
    """Racing field of 2, each challenger proposed through a best-of-3 slate:
    every applied challenger's tree must match its persisted experiment (the
    experiment whose diversity signature `_mint_challenger_field` judged),
    and the best chosen arm survives to the known floor."""
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
    )
    slate_mocks.reset()

    from zicato.evolve.loop import evolve_n_rounds

    outcomes = asyncio.run(
        evolve_n_rounds(
            rounds=1,
            workspace_root=workspace,
            epoch_id=epoch_id,
            harness_call_llm=slate_mocks.harness_llm,
            auxiliary_call_llm=slate_mocks.field_slate_aux_llm,
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
    from zicato.orchestrator import _diversity_signature

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
