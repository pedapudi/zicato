"""Inspect authored configuration and its approved environment boundaries."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import click

from zicato.config import describe_env_vars


def render_env_report() -> str:
    """Render the approved environment contracts and the purpose of each value."""
    lines = ["Environment boundaries", ""]
    for info in describe_env_vars():
        lines.extend([f"{info.name} ({info.role})", f"  {info.description}", ""])
    return "\n".join(lines)


@click.command(name="environment")
@click.option(
    "--json", "as_json", is_flag=True, help="Print the approved boundary declarations as JSON."
)
def config_env_cmd(as_json: bool) -> None:
    """List credential, operating-system, and child-process environment contracts."""
    if as_json:
        click.echo(
            json.dumps(
                [
                    {"name": info.name, "role": info.role, "description": info.description}
                    for info in describe_env_vars()
                ],
                indent=2,
            )
        )
    else:
        click.echo(render_env_report())


@click.command(name="config")
@click.argument("field", required=False)
@click.option("--workspace", default=".zicato", type=click.Path(path_type=Path))
@click.option(
    "--effective", is_flag=True, help="Resolve values for an invocation without CLI overrides."
)
@click.option("--sources", is_flag=True, help="Include the source of each effective value.")
@click.option(
    "--schema", is_flag=True, help="Print editor schemas for workspace and scoring files."
)
@click.option("--scaffold", is_flag=True, help="Print valid workspace and scoring defaults.")
@click.option("--complete", is_flag=True, help="Spell out every default in the scaffold.")
@click.option("--reference", is_flag=True, help="Print the generated Markdown field reference.")
def inspect_config_cmd(
    field: str | None,
    workspace: Path,
    effective: bool,
    sources: bool,
    schema: bool,
    scaffold: bool,
    complete: bool,
    reference: bool,
) -> None:
    """Explain a field or inspect the declared effective configuration."""
    import re  # noqa: PLC0415

    from zicato.workspace.config_inspection import (  # noqa: PLC0415
        configuration_fields,
        configuration_reference,
        configuration_scaffold,
        configuration_schemas,
        effective_configuration,
    )

    if sum((bool(field), effective, schema, scaffold, reference)) > 1:
        raise click.UsageError("choose a field, effective values, schema, scaffold, or reference")
    if complete and not scaffold:
        raise click.UsageError("--complete requires --scaffold")
    if sources and (field or schema or scaffold or reference):
        raise click.UsageError("--sources applies to effective values")
    payload: dict[str, Any]
    try:
        if reference:
            click.echo(configuration_reference())
            return
        if schema:
            payload = configuration_schemas()
        elif scaffold:
            payload = configuration_scaffold(complete=complete)
        elif field:
            candidates = configuration_fields()
            matched = next(
                (
                    value
                    for name, value in candidates.items()
                    if re.fullmatch(re.escape(name).replace(r"<name>", r"[^.]+"), field)
                ),
                None,
            )
            if matched is None:
                raise click.UsageError(f"unknown configuration field {field!r}; inspect --schema")
            payload = {"field": field, **matched}
        else:
            selected = effective_configuration(workspace)
            payload = (
                selected if sources else {path: item["value"] for path, item in selected.items()}
            )
    except (OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps(payload, indent=2, sort_keys=True))


__all__ = ["config_env_cmd", "inspect_config_cmd", "render_env_report"]
