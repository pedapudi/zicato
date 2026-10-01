"""The decision-telemetry insight reaches the next round's proposal evidence.

At the end of every round the analyzer writes ``insights/round_{N}.md``. The
next round reads the most recent of those files into
:attr:`~zicato.proposer.agent.ProposerContext.insights`, and the proposal
task renders it under ``## Recent telemetry insights``.

* A round delivers the text of the highest-numbered round file and no other.
* A placeholder file (no telemetry, or a failed evaluation call) delivers
  nothing.
* The analyzer that the loop runs reads only the training slice's runs, so an
  epoch with a holdout split and restricted proposer visibility keeps the
  holdout entry's telemetry out of the insight the proposer receives.

The delivery tests run under the default restricted proposer visibility on a
one-entry board, which has no holdout split.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

import zicato.analyzer.insights as insights_module
import zicato.tournament.worker_execution as _tournament_worker_execution
from tests._orchestrator_harness import (
    bootstrap_workspace,
    evaluation_call_llm,
    install_stub_adapter_factory,
    install_telemetry_stubs,
    run_evolve_once,
)
from tests.test_orchestrator_multi_challenger_holdout import (
    _bootstrap,
    _install_per_entry_telemetry_stubs,
)
from zicato.core.types import OverfittingConfig
from zicato.proposer.input_capture import ROLE_PROPOSAL, read_proposer_inputs

_HEADING = "## Recent telemetry insights"
# The provenance line the analyzer writes on a training-slice analysis.
TRAINING_SLICE_ANALYSIS_MARKER = (
    "<!-- zicato: decision-telemetry analysis of the training slice -->"
)


def _proposal_tasks(workspace: Path, epoch_id: str) -> list[str]:
    return [
        str(record["user"])
        for record in read_proposer_inputs(workspace, epoch_id)
        if record.get("role") == ROLE_PROPOSAL
    ]


def _write_insight(workspace: Path, epoch_id: str, name: str, body: str) -> None:
    """Write ``body`` as an analysis of the training slice, marker included."""
    directory = workspace / "epochs" / epoch_id / "insights"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(f"{TRAINING_SLICE_ANALYSIS_MARKER}\n{body}", encoding="utf-8")


def _run_scripted_round(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, str]:
    workspace, epoch_id = bootstrap_workspace(tmp_path)
    install_stub_adapter_factory(monkeypatch)
    install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 1.0, "v1": 0.5},
        canned_pass_by_gen={"v0": True, "v1": True},
    )
    return workspace, epoch_id


def test_round_delivers_only_the_most_recent_round_insight(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace, epoch_id = _run_scripted_round(monkeypatch, tmp_path)
    _write_insight(workspace, epoch_id, "round_0001.md", "Older finding: ladder idle.\n")
    _write_insight(workspace, epoch_id, "round_0002.md", "Newest finding: nudge dominates.\n")
    _write_insight(workspace, epoch_id, "latest.md", "Operator report, not evidence.\n")

    run_evolve_once(workspace, epoch_id, evaluation_call_llm)

    tasks = _proposal_tasks(workspace, epoch_id)
    assert tasks, "the round ran no proposal episode"
    for task in tasks:
        assert f"{_HEADING}\nNewest finding: nudge dominates." in task
        assert "Older finding" not in task
        assert "Operator report" not in task


def test_round_after_a_placeholder_insight_carries_no_insight_block(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace, epoch_id = _run_scripted_round(monkeypatch, tmp_path)
    _write_insight(workspace, epoch_id, "round_0001.md", "Real finding from an earlier round.\n")
    # The epoch has no runs yet, so the analyzer writes its no-telemetry
    # placeholder as round 2 without an evaluation call.
    asyncio.run(
        insights_module.analyze_epoch_telemetry(workspace, epoch_id, evaluation_call_llm, round_n=2)
    )

    run_evolve_once(workspace, epoch_id, evaluation_call_llm)

    tasks = _proposal_tasks(workspace, epoch_id)
    assert tasks, "the round ran no proposal episode"
    for task in tasks:
        assert _HEADING not in task


def _policy_event(entry_id: str, policy_name: str) -> str:
    event = {
        "event_id": f"evt_{entry_id}",
        "run_id": f"run_{entry_id}",
        "sequence": 1,
        "emitted_at": {"seconds": 1_700_000_000, "nanos": 0},
        "session_id": f"sess_{entry_id}",
        "policy_applied": {
            "policy_name": policy_name,
            "outcome": "applied",
            "reason": "",
            "detail": "",
        },
    }
    return json.dumps(event) + "\n"


def _emit_policy_telemetry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every stubbed run leave one decision event naming its slice.

    A training entry's run records ``policy_seen_on_training_run``; the
    holdout entry's run (the confirmation of the crowned challenger)
    records ``policy_seen_on_holdout_run``.
    """
    run_single = _tournament_worker_execution._run_single

    async def run_and_emit(**kwargs: Any) -> Any:
        profile = await run_single(**kwargs)
        entry_id = kwargs["entry"].id
        run_dir = (
            Path(kwargs["workspace_root"])
            / "epochs"
            / kwargs["epoch_id"]
            / "generations"
            / kwargs["generation"].id
            / "runs"
            / entry_id
            / "seed-none"
        )
        run_dir.mkdir(parents=True, exist_ok=True)
        policy = "policy_seen_on_holdout_run" if entry_id == "h0" else "policy_seen_on_training_run"
        (run_dir / "events.tournament.r0.jsonl").write_text(
            _policy_event(entry_id, policy), encoding="utf-8"
        )
        return profile

    monkeypatch.setattr(_tournament_worker_execution, "_run_single", run_and_emit)


def test_restricted_round_analyzes_the_training_slice_and_withholds_holdout_telemetry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace, epoch_id = _bootstrap(
        tmp_path,
        structure="racing",
        field_size=2,
        overfitting=OverfittingConfig(restrict_proposer_visibility=True),
    )
    install_stub_adapter_factory(monkeypatch)
    losses = {
        (gid, entry): scalar
        for gid, scalar in (("v0", 2.0), ("v1", 0.5), ("v2", 1.5))
        for entry in (*(f"train_{i}" for i in range(4)), "h0")
    }
    _install_per_entry_telemetry_stubs(
        monkeypatch,
        loss_by_gen_entry=losses,
        pass_by_gen={"v0": True, "v1": True, "v2": True},
    )
    _emit_policy_telemetry(monkeypatch)

    # The analyzer's evaluation call fails in this harness, so the prompt it
    # would have sent is captured where it is rendered.
    analyzer_prompts: list[str] = []
    render = insights_module.render_insight_user_prompt

    def capture(*args: object, **kwargs: object) -> str:
        prompt = render(*args, **kwargs)  # type: ignore[arg-type]
        analyzer_prompts.append(prompt)
        return prompt

    monkeypatch.setattr(insights_module, "render_insight_user_prompt", capture)

    run_evolve_once(workspace, epoch_id, evaluation_call_llm)

    assert len(analyzer_prompts) == 1
    assert "policy_seen_on_training_run" in analyzer_prompts[0]
    assert "policy_seen_on_holdout_run" not in analyzer_prompts[0]
    # The holdout entry did run and leave telemetry; only the analysis skipped it.
    holdout_runs = (workspace / "epochs" / epoch_id / "generations").glob(
        "*/runs/h0/seed-none/events.tournament.r0.jsonl"
    )
    assert any(holdout_runs)
