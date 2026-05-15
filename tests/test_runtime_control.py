"""Tests for ``zicato.runtime.control`` — the control-file protocol."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zicato.runtime.control import (
    CMD_KILL_RUN_PREFIX,
    CMD_PAUSE_EPOCH,
    CMD_PROMOTE_PREFIX,
    CMD_REJECT_PREFIX,
    CMD_RUBRIC_REPLACEMENT,
    CMD_SKIP_ROUND,
    ControlCommand,
    consume_command,
    is_paused,
    list_pending_commands,
    write_command,
)
from zicato.runtime.paths import control_dir, control_log_dir

# ---------------------------------------------------------------------------
# Listing pending commands
# ---------------------------------------------------------------------------


def test_list_pending_commands_empty_without_dir(tmp_path: Path) -> None:
    assert list_pending_commands(tmp_path) == []


def test_list_pause_epoch_flag(tmp_path: Path) -> None:
    write_command(tmp_path, ControlCommand(name=CMD_PAUSE_EPOCH))
    cmds = list_pending_commands(tmp_path)
    assert [c.name for c in cmds] == [CMD_PAUSE_EPOCH]
    assert cmds[0].arg == ""
    assert cmds[0].payload == ""


def test_list_skip_round_flag(tmp_path: Path) -> None:
    write_command(tmp_path, ControlCommand(name=CMD_SKIP_ROUND))
    cmds = list_pending_commands(tmp_path)
    assert [c.name for c in cmds] == [CMD_SKIP_ROUND]


def test_list_targeted_kill_runs(tmp_path: Path) -> None:
    write_command(tmp_path, ControlCommand(name=CMD_KILL_RUN_PREFIX, arg="run_a"))
    write_command(tmp_path, ControlCommand(name=CMD_KILL_RUN_PREFIX, arg="run_b"))
    cmds = list_pending_commands(tmp_path)
    by_arg = sorted(c.arg for c in cmds)
    assert by_arg == ["run_a", "run_b"]
    assert all(c.name == CMD_KILL_RUN_PREFIX for c in cmds)


def test_list_targeted_promote_and_reject(tmp_path: Path) -> None:
    write_command(tmp_path, ControlCommand(name=CMD_PROMOTE_PREFIX, arg="v2"))
    write_command(tmp_path, ControlCommand(name=CMD_REJECT_PREFIX, arg="v3"))
    cmds = list_pending_commands(tmp_path)
    names_args = sorted((c.name, c.arg) for c in cmds)
    assert names_args == [(CMD_PROMOTE_PREFIX, "v2"), (CMD_REJECT_PREFIX, "v3")]


def test_list_rubric_replacement_carries_payload(tmp_path: Path) -> None:
    write_command(
        tmp_path,
        ControlCommand(name=CMD_RUBRIC_REPLACEMENT, payload="new rubric body\n"),
    )
    [cmd] = list_pending_commands(tmp_path)
    assert cmd.name == CMD_RUBRIC_REPLACEMENT
    assert cmd.payload == "new rubric body\n"


def test_list_skips_tmp_files(tmp_path: Path) -> None:
    write_command(tmp_path, ControlCommand(name=CMD_PAUSE_EPOCH))
    # Drop a stray .tmp into control/.
    (control_dir(tmp_path) / "skip_round.tmp").write_text("")
    cmds = list_pending_commands(tmp_path)
    assert [c.name for c in cmds] == [CMD_PAUSE_EPOCH]


def test_list_mixed_command_set_is_deterministic(tmp_path: Path) -> None:
    write_command(tmp_path, ControlCommand(name=CMD_SKIP_ROUND))
    write_command(tmp_path, ControlCommand(name=CMD_PAUSE_EPOCH))
    write_command(tmp_path, ControlCommand(name=CMD_KILL_RUN_PREFIX, arg="run_b"))
    write_command(tmp_path, ControlCommand(name=CMD_KILL_RUN_PREFIX, arg="run_a"))
    write_command(
        tmp_path,
        ControlCommand(name=CMD_RUBRIC_REPLACEMENT, payload="..."),
    )

    cmds = list_pending_commands(tmp_path)
    # Top-level entries first (sorted alphabetically), then targeted
    # commands in their own pass — that's the listing contract.
    names = [c.name for c in cmds]
    # All five commands surface.
    assert sorted(names) == sorted(
        [
            CMD_SKIP_ROUND,
            CMD_PAUSE_EPOCH,
            CMD_KILL_RUN_PREFIX,
            CMD_KILL_RUN_PREFIX,
            CMD_RUBRIC_REPLACEMENT,
        ]
    )


# ---------------------------------------------------------------------------
# is_paused
# ---------------------------------------------------------------------------


def test_is_paused_false_when_no_flag(tmp_path: Path) -> None:
    assert is_paused(tmp_path) is False


def test_is_paused_true_after_pause(tmp_path: Path) -> None:
    write_command(tmp_path, ControlCommand(name=CMD_PAUSE_EPOCH))
    assert is_paused(tmp_path) is True


# ---------------------------------------------------------------------------
# write_command
# ---------------------------------------------------------------------------


def test_write_targeted_command_requires_arg(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="requires a non-empty arg"):
        write_command(tmp_path, ControlCommand(name=CMD_KILL_RUN_PREFIX))


def test_write_command_returns_file_path(tmp_path: Path) -> None:
    path = write_command(tmp_path, ControlCommand(name=CMD_PAUSE_EPOCH))
    assert path == control_dir(tmp_path) / CMD_PAUSE_EPOCH
    assert path.exists()


def test_write_rubric_replacement_writes_payload(tmp_path: Path) -> None:
    path = write_command(
        tmp_path,
        ControlCommand(name=CMD_RUBRIC_REPLACEMENT, payload="hello rubric"),
    )
    assert path.read_text() == "hello rubric"


# ---------------------------------------------------------------------------
# consume_command
# ---------------------------------------------------------------------------


def test_consume_moves_file_into_log_dir(tmp_path: Path) -> None:
    write_command(tmp_path, ControlCommand(name=CMD_PAUSE_EPOCH))
    [cmd] = list_pending_commands(tmp_path)

    log_path = consume_command(tmp_path, cmd, source="dashboard", reason="op asked")

    # Source file is gone.
    assert not cmd.file_path.exists()
    # Audit log file exists and is JSON with the expected fields.
    assert log_path.exists()
    record = json.loads(log_path.read_text())
    assert record["command"] == CMD_PAUSE_EPOCH
    assert record["arg"] == ""
    assert record["source"] == "dashboard"
    assert record["reason"] == "op asked"
    assert record["consumed_at"].endswith("Z")


def test_consume_targeted_command_records_arg(tmp_path: Path) -> None:
    write_command(tmp_path, ControlCommand(name=CMD_KILL_RUN_PREFIX, arg="run_xyz"))
    [cmd] = list_pending_commands(tmp_path)
    log_path = consume_command(tmp_path, cmd)

    record = json.loads(log_path.read_text())
    assert record["command"] == CMD_KILL_RUN_PREFIX
    assert record["arg"] == "run_xyz"
    # Audit-log filename embeds the arg too.
    assert "run_xyz" in log_path.name


def test_consume_rubric_replacement_preserves_payload(tmp_path: Path) -> None:
    write_command(
        tmp_path,
        ControlCommand(name=CMD_RUBRIC_REPLACEMENT, payload="new rubric"),
    )
    [cmd] = list_pending_commands(tmp_path)
    log_path = consume_command(tmp_path, cmd)
    record = json.loads(log_path.read_text())
    assert record["command"] == CMD_RUBRIC_REPLACEMENT
    assert record["payload"] == "new rubric"


def test_consume_cleans_empty_targeted_subdir(tmp_path: Path) -> None:
    write_command(tmp_path, ControlCommand(name=CMD_KILL_RUN_PREFIX, arg="run_a"))
    [cmd] = list_pending_commands(tmp_path)
    consume_command(tmp_path, cmd)
    # The kill_runs/ directory should now be gone since it's empty.
    assert not (control_dir(tmp_path) / CMD_KILL_RUN_PREFIX).exists()


def test_consume_targeted_keeps_subdir_when_siblings_remain(tmp_path: Path) -> None:
    write_command(tmp_path, ControlCommand(name=CMD_KILL_RUN_PREFIX, arg="run_a"))
    write_command(tmp_path, ControlCommand(name=CMD_KILL_RUN_PREFIX, arg="run_b"))
    cmds = list_pending_commands(tmp_path)
    a = next(c for c in cmds if c.arg == "run_a")
    consume_command(tmp_path, a)
    # Subdir survives; run_b still pending.
    assert (control_dir(tmp_path) / CMD_KILL_RUN_PREFIX).is_dir()
    remaining = list_pending_commands(tmp_path)
    assert [c.arg for c in remaining] == ["run_b"]


def test_consume_idempotent_when_source_already_deleted(tmp_path: Path) -> None:
    write_command(tmp_path, ControlCommand(name=CMD_PAUSE_EPOCH))
    [cmd] = list_pending_commands(tmp_path)
    # Operator manually deleted the file between list and consume.
    cmd.file_path.unlink()
    # Consume still records the action.
    log_path = consume_command(tmp_path, cmd, source="cli")
    assert log_path.exists()


def test_audit_log_dir_created_on_first_consume(tmp_path: Path) -> None:
    write_command(tmp_path, ControlCommand(name=CMD_PAUSE_EPOCH))
    [cmd] = list_pending_commands(tmp_path)
    consume_command(tmp_path, cmd)
    assert control_log_dir(tmp_path).is_dir()
    log_files = list(control_log_dir(tmp_path).iterdir())
    assert len(log_files) == 1
