"""Deterministic target and workspace for recommended complete-loop acceptance."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import zicato_examples.target_0_convergence as example
from tests._best_of_n_slate_support import slate_aux_llm, target_llm
from tests._contract_pins import resolved_contract_with_proposer
from tests._foe_support import stand_in_proposer_block
from zicato.core.measurement import MeasurementDraw, MeasurementPurpose
from zicato.core.scoring_config import ScoringWeights
from zicato.epoch.contract import scoring_to_canon
from zicato.epoch.lifecycle import new_epoch
from zicato_examples.target_0_convergence.harness import DeterministicPolicyAdapter

EXAMPLE_ROOT = Path(example.__file__).resolve().parent
POLICY_PATH = Path("agent/policy.py")
CHOSEN_POLICIES = {
    "v1": "",
    "v2": "verbose-prose",
    "v3": "verbose-prose; skip-citations",
    "v4": "verbose-prose; skip-citations; omit-summary; residual-defect",
}
REGRESSION = "verbose-prose; skip-citations; omit-summary; fabricate-metrics"


class SourceRecordingSession:
    """Capture the measured source while preserving the real target evaluation."""

    def __init__(self, snapshot: Path) -> None:
        self.inner = DeterministicPolicyAdapter().load(snapshot)
        self.source_digest = hashlib.sha256((snapshot / POLICY_PATH).read_bytes()).hexdigest()

    async def run(self, entry: Any, sinks: Any, config: Any) -> Any:
        generation = entry.context.get("generation_id", "")
        replicate = MeasurementDraw.from_context(entry.context)
        control_path = config.workspace_root / "acceptance-control.json"
        control = json.loads(control_path.read_text()) if control_path.exists() else {}
        if (
            control.get("pause_generation") == generation
            and replicate.purpose == MeasurementPurpose.TOURNAMENT
        ):
            signal = config.workspace_root / "acceptance-worker-started.json"
            signal.write_text(json.dumps({"pid": os.getpid(), "generation": generation}))
            await asyncio.Event().wait()
        result = await self.inner.run(entry, sinks, config)
        assert config.run_context is not None and config.run_context.scratch_dir is not None
        (config.run_context.scratch_dir / "evaluated-source.json").write_text(
            json.dumps({"source_digest": self.source_digest, "generation": generation})
        )
        return result


class SourceRecordingAdapter:
    name = DeterministicPolicyAdapter.name

    def mutation_points(self, source_roots: Any = None) -> list[Any]:
        return DeterministicPolicyAdapter().mutation_points(source_roots)

    def load(self, generation_root: Path) -> SourceRecordingSession:
        return SourceRecordingSession(generation_root)

    def worker_spec(self) -> dict[str, str]:
        return {"kind": "import", "factory": "tests._recommended_loop_support:make_adapter"}


def make_adapter() -> SourceRecordingAdapter:
    return SourceRecordingAdapter()


def bootstrap(workspace_parent: Path) -> tuple[Path, str, dict[str, Any]]:
    """Build the production recommendation over a two-entry known-answer board."""
    workspace = workspace_parent / ".zicato"
    workspace.mkdir()
    policies = {
        f"{generation}#{slot}": {"style_rules": content if slot == 0 else REGRESSION}
        for generation, content in CHOSEN_POLICIES.items()
        for slot in range(3)
    }
    (workspace / "config.json").write_text(
        json.dumps(
            {
                "instance_id": "acceptance",
                "proposer": stand_in_proposer_block(
                    workspace_parent / "proposal-runtime", contents=policies
                ),
                "generation_source_backend": "git",
                "created_at": "2026-09-06T00:00:00Z",
                "adapter": {
                    "kind": "import",
                    "factory": "tests._recommended_loop_support:make_adapter",
                    "mutable_trees": [str(EXAMPLE_ROOT / "agent")],
                },
                "runtime": {"propose_parallelism": 2, "parallelism": 2, "seed": 17},
                "models": {
                    "engines": {
                        "target": {"call_llm": "tests._best_of_n_slate_support:target_llm"},
                        "evaluation": {"call_llm": "tests._best_of_n_slate_support:slate_aux_llm"},
                    }
                },
            }
        )
    )
    board = workspace_parent / "board.jsonl"
    entries = [json.loads(line) for line in (EXAMPLE_ROOT / "board.jsonl").read_text().splitlines()]
    board.write_text(
        "".join(
            json.dumps(entry) + "\n"
            for entry in entries
            if entry["id"] in {"conv_summary", "conv_no_fabrication"}
        )
    )
    brief = workspace_parent / "brief.md"
    brief.write_text(
        "# Objective\nRemove writing-policy defects while preserving factual output.\n"
    )
    weights = ScoringWeights()
    configuration = scoring_to_canon(weights)
    epoch = new_epoch(
        workspace,
        name="recommended-acceptance",
        board_source=board,
        brief_source=brief,
        weights=weights,
        auto_close_previous=False,
        contract=resolved_contract_with_proposer(workspace, EXAMPLE_ROOT / "proposer"),
    )
    return workspace, epoch.id, configuration


async def run_round(workspace: Path, epoch_id: str) -> list[Any]:
    from zicato.evolve.loop import evolve_n_rounds

    return await evolve_n_rounds(
        rounds=1,
        workspace_root=workspace,
        epoch_id=epoch_id,
        target_call_llm=target_llm,
        evaluation_call_llm=slate_aux_llm,
        auto_epoch=False,
        fast_mode=True,
    )
