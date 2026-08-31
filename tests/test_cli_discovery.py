"""The root command hierarchy is an explicit product contract."""

from click.testing import CliRunner

from zicato.cli.discovery import DIRECT_COMMANDS, GROUP_COMMANDS, build_cli_root


def test_root_exposes_only_primary_commands_and_advanced_namespaces() -> None:
    root = build_cli_root()
    assert tuple(root.commands) == (*DIRECT_COMMANDS, *GROUP_COMMANDS)


def test_moved_commands_have_one_location() -> None:
    root = build_cli_root()
    assert set(root.commands["inspect"].commands) == {
        "environment",
        "logs",
        "mutations",
        "reflection",
        "telemetry",
    }
    assert set(root.commands["repair"].commands) == {
        "epoch-goals",
        "generation-source-backend",
        "generations",
        "index",
        "judge-losses",
        "report",
        "tournament-fk",
        "v0-baseline",
    }
    assert "register" in root.commands["epoch"].commands
    assert "propose" in root.commands["proposer"].commands
    assert set(root.commands["tournament"].commands) == {"run"}


def test_removed_root_spellings_fail() -> None:
    root = build_cli_root()
    for name in ("builder", "config", "logs", "mutations", "propose", "register", "reindex"):
        result = CliRunner().invoke(root, [name])
        assert result.exit_code == 2
