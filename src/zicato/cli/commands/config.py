"""``zicato inspect environment`` — zicato's configuration surface.

ADVANCED — off the happy path. Operator knobs live on CLI flags, in the
workspace ``config.json``, and in contract files such as ``scoring.json``.
No environment variable is a configuration knob.

The command prints the small MERITED set of environment variables zicato
touches. Each is a process-boundary contract: a harness contract, an
internal handoff, the secrets boundary, or a CI/test toggle. The set is sourced from
:func:`zicato.config.describe_env_vars`, so this command cannot drift
from the code.

Standalone command file picked up by :mod:`zicato.cli.discovery`.
"""

from __future__ import annotations

import json

import click

from zicato.config import describe_env_vars

#: Render order + one-line meaning for each role label, so the report
#: groups contracts of the same kind together and explains the label.
_ROLE_HEADINGS: tuple[tuple[str, str], ...] = (
    (
        "harness-contract",
        "set by zicato for the inner harness — part of the run contract",
    ),
    (
        "internal-handoff",
        "set and restored by zicato itself to hand a value across its own processes",
    ),
    (
        "external-integration",
        "inputs and child-process controls required by an optional external tool",
    ),
    (
        "secrets-boundary",
        "operator-NAMED variables so credentials stay in the environment, never in files",
    ),
    (
        "test-toggle",
        "CI / test-suite switches; never read on an operator path",
    ),
)


def render_env_report() -> str:
    """Render the merited env-var set as grouped, labelled terminal text.

    Kept as a free function (not inlined into the command) so tests can
    assert on the rendered text without invoking the click runner.
    """
    infos = describe_env_vars()
    lines: list[str] = []
    lines.append("Environment variables zicato touches — the deliberate set.")
    lines.append("")
    lines.append(
        "Operator knobs are NOT here: they live on CLI flags (see each "
        "command's --help;\nevery flag names the config knob it shadows) "
        "and in workspace JSON files\n(config.json and contract files such "
        "as scoring.json). Everything below is a\nprocess-boundary "
        "contract, kept on purpose."
    )
    for role, meaning in _ROLE_HEADINGS:
        members = [info for info in infos if info.role == role]
        if not members:
            continue
        lines.append("")
        lines.append(click.style(f"[{role}]", bold=True) + f" — {meaning}")
        for info in members:
            lines.append(f"  {click.style(info.name, fg='cyan')}")
            lines.append(f"      {info.description}")
    return "\n".join(lines)


@click.command(name="environment")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit the set as a JSON array instead of grouped text.",
)
def config_env_cmd(as_json: bool) -> None:
    """List the environment variables zicato deliberately touches.

    NO environment variable is a configuration knob. Every variable
    printed here is a process-boundary contract: the per-run harness
    contract, the internal harmonograf handoff pair, the secrets
    boundary (operator-named api_key_env variables and the worker
    passthrough allowlist), and the CI/test toggles.
    """
    if as_json:
        payload = [
            {"name": info.name, "role": info.role, "description": info.description}
            for info in describe_env_vars()
        ]
        click.echo(json.dumps(payload, indent=2))
        return
    click.echo(render_env_report())


__all__ = ["config_env_cmd", "render_env_report"]
