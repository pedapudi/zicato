"""Build the deliberately small, static command hierarchy."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

import click

HAPPY_PATH_COMMANDS = ("init", "evolve")
DIRECT_COMMANDS = (*HAPPY_PATH_COMMANDS, "dashboard", "tui", "health")
GROUP_COMMANDS = ("board", "epoch", "proposer", "tournament", "inspect", "repair")


def _version() -> str:
    try:
        return version("zicato")
    except PackageNotFoundError:
        return "0.0.0"


class ZicatoGroup(click.Group):
    """Render the primary commands before the advanced namespaces."""

    def format_commands(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        rows = {
            name: command
            for name in self.list_commands(ctx)
            if (command := self.get_command(ctx, name)) is not None and not command.hidden
        }
        for title, names in (
            ("Primary commands", DIRECT_COMMANDS),
            ("Advanced namespaces", GROUP_COMMANDS),
        ):
            with formatter.section(title):
                formatter.write_dl(
                    [(name, rows[name].get_short_help_str()) for name in names if name in rows]
                )


def _advanced_groups() -> tuple[click.Group, ...]:
    from zicato.cli.commands.analyze_telemetry import analyze_telemetry_cmd
    from zicato.cli.commands.board import board_grp
    from zicato.cli.commands.config import config_env_cmd
    from zicato.cli.commands.epoch import epoch_grp, repair_epoch_goals_cmd
    from zicato.cli.commands.logs import logs_cmd
    from zicato.cli.commands.mutations import mutations_cmd
    from zicato.cli.commands.propose import propose_cmd
    from zicato.cli.commands.proposer import proposer_grp
    from zicato.cli.commands.reflect import reflect_grp
    from zicato.cli.commands.regenerate_report import regenerate_report_cmd
    from zicato.cli.commands.register import register_cmd
    from zicato.cli.commands.reindex import (
        reindex_cmd,
        reindex_generations_cmd,
        repair_tournament_fk_cmd,
    )
    from zicato.cli.commands.repair_generation_source_backend import (
        repair_generation_source_backend_cmd,
    )
    from zicato.cli.commands.repair_judge_losses import repair_judge_losses_cmd
    from zicato.cli.commands.repair_v0_baseline import repair_v0_baseline_cmd
    from zicato.cli.commands.tournament import tournament_cmd

    @click.group(short_help="Inspect workspace state and derived analysis.")
    def inspect() -> None:
        pass

    @click.group(short_help="Rebuild or repair derived and legacy workspace data.")
    def repair() -> None:
        pass

    @click.group(short_help="Run isolated tournament operations.")
    def tournament() -> None:
        pass

    for group, commands in (
        (
            inspect,
            {
                "telemetry": analyze_telemetry_cmd,
                "environment": config_env_cmd,
                "logs": logs_cmd,
                "mutations": mutations_cmd,
                "reflection": reflect_grp,
            },
        ),
        (
            repair,
            {
                "index": reindex_cmd,
                "generations": reindex_generations_cmd,
                "tournament-fk": repair_tournament_fk_cmd,
                "epoch-goals": repair_epoch_goals_cmd,
                "judge-losses": repair_judge_losses_cmd,
                "v0-baseline": repair_v0_baseline_cmd,
                "generation-source-backend": repair_generation_source_backend_cmd,
                "report": regenerate_report_cmd,
            },
        ),
        (tournament, {"run": tournament_cmd}),
        (epoch_grp, {"register": register_cmd}),
        (proposer_grp, {"propose": propose_cmd}),
    ):
        for name, command in commands.items():
            group.add_command(command, name)
    return board_grp, epoch_grp, proposer_grp, tournament, inspect, repair


def build_cli_root() -> click.Group:
    """Construct the public CLI contract."""

    @click.group(name="zicato", cls=ZicatoGroup)
    @click.version_option(version=_version(), prog_name="zicato")
    def root() -> None:
        """A self-improving harness for any system you can measure.

        Most workflows need only ``init`` once and then ``evolve``.
        """

    from zicato.cli.commands.dashboard import dashboard_cmd
    from zicato.cli.commands.evolve import evolve_cmd
    from zicato.cli.commands.health import health_cmd
    from zicato.cli.commands.init import init_cmd
    from zicato.cli.commands.tui import tui_cmd

    for command in (init_cmd, evolve_cmd, dashboard_cmd, tui_cmd, health_cmd, *_advanced_groups()):
        root.add_command(command)
    return root


__all__ = [
    "DIRECT_COMMANDS",
    "GROUP_COMMANDS",
    "HAPPY_PATH_COMMANDS",
    "ZicatoGroup",
    "build_cli_root",
]
