"""Inspect harness setup without model requests or measured board runs."""

from pathlib import Path

import click

from zicato.check import CheckContext, WorkspaceCheckError, build_report, render_report


@click.command(name="setup")
@click.option("--workspace", default=".zicato", type=click.Path(file_okay=False), show_default=True)
@click.option(
    "--epoch", default=None, help="Check a frozen epoch instead of the editable contract."
)
def setup_cmd(workspace: str, epoch: str | None) -> None:
    """Validate driver imports, adapter loading, grading seams, and configuration.

    This performs the same local checks that gate evolve. It imports operator
    code and loads one snapshot in a bounded subprocess, without calling model
    roles, running board entries, or writing an epoch.
    """
    with CheckContext(
        Path(workspace).resolve(), epoch_id=epoch, live_contract=epoch is None
    ) as ctx:
        report = build_report(ctx)
    if report.findings:
        click.echo(render_report(report), nl=False)
    if report.blocking:
        raise click.ClickException(str(WorkspaceCheckError(report)))
    click.echo("Setup checks passed. No model request or board entry ran.")
