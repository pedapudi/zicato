"""``zicato inspect telemetry`` analyzes only what the proposer may see.

A ``round_{N}.md`` the command writes is read into the next round's
proposal evidence, so the command resolves the epoch's training slice and
visibility posture from the frozen contract and passes both to the
analyzer. An epoch whose board cannot be read is a command error.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

import zicato.cli.commands.analyze_telemetry as analyze_telemetry
from tests.test_orchestrator_multi_challenger_holdout import _bootstrap


def _policy_events(workspace: Path, epoch_id: str, entry_id: str, policy_name: str) -> None:
    run_dir = (
        workspace / "epochs" / epoch_id / "generations" / "v0" / "runs" / entry_id / "seed-none"
    )
    run_dir.mkdir(parents=True, exist_ok=True)
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
    (run_dir / "events.tournament.r0.jsonl").write_text(json.dumps(event) + "\n")


def _invoke(workspace: Path, epoch_id: str) -> object:
    return CliRunner().invoke(
        analyze_telemetry.analyze_telemetry_cmd,
        ["--workspace", str(workspace), "--epoch", epoch_id, "--round", "3"],
    )


def test_command_analyzes_the_training_slice_of_a_board_with_a_holdout_entry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace, epoch_id = _bootstrap(tmp_path, structure="racing", field_size=2)
    _policy_events(workspace, epoch_id, "train_0", "policy_seen_on_training_run")
    _policy_events(workspace, epoch_id, "h0", "policy_seen_on_holdout_run")

    prompts: list[str] = []

    async def echo_prompt(_system: str, user: str, _model: str) -> str:
        prompts.append(user)
        return user

    monkeypatch.setattr(analyze_telemetry, "_resolve_aux_llm", lambda _config: echo_prompt)

    result = _invoke(workspace, epoch_id)

    assert result.exit_code == 0, result.output  # type: ignore[attr-defined]
    assert len(prompts) == 1
    assert "policy_seen_on_training_run" in prompts[0]
    assert "policy_seen_on_holdout_run" not in prompts[0]
    written = (workspace / "epochs" / epoch_id / "insights" / "round_0003.md").read_text()
    assert "policy_seen_on_training_run" in written
    assert "policy_seen_on_holdout_run" not in written
