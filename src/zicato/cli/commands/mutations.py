"""``zicato inspect mutations`` — audit the mutable surface in the registered system under test.

ADVANCED / DEBUGGING — off the happy path. ``zicato evolve`` enumerates
the mutable surface internally when it proposes. Run ``zicato
mutations`` by hand only to audit *what* the proposer is allowed to
change.

Standalone command file auto-discovered under
``zicato/cli/commands/``; this file does NOT import from
``zicato.cli`` so the discovery layer can stay one-way.

The command reads ``<workspace>/config.json`` to learn the source roots
to enumerate, then prints either a fixed-width table or a JSON document
listing every mutation point.
"""

from __future__ import annotations

import json
from pathlib import Path

import click

from zicato.core.types import MutationPoint
from zicato.mutation.enumerator import enumerate_mutations
from zicato.workspace.config_io import read_workspace_config
from zicato.workspace_loader import activate_mutation_surface

_PREVIEW_LEN = 60


def _load_source_roots(workspace_dir: Path) -> list[Path]:
    """Read ``workspace/config.json`` and return its declared source roots.

    Raises :class:`click.ClickException` when the config file is missing
    or malformed — the message points the operator at ``zicato epoch register``,
    which is the documented onramp for populating the workspace.
    """

    try:
        config = read_workspace_config(workspace_dir)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    if not config.exists:
        raise click.ClickException(
            f"No workspace config at {config.path}. "
            "Run `zicato epoch register` to point this workspace at a system under test."
        )
    if not config.source_roots:
        raise click.ClickException(
            f"{config.path} has no 'source_roots' field. "
            "Run `zicato epoch register` to populate it."
        )
    return [Path(r) for r in config.source_roots]


def _glob_match(text: str, pattern: str) -> bool:
    """Trivial glob match — supports ``*`` only.

    Avoids depending on :mod:`fnmatch`'s case-folding behavior; mutation
    ids are case-sensitive and operators may want to filter exactly.
    """

    import fnmatch

    return fnmatch.fnmatchcase(text, pattern)


def _filter_points(
    points: list[MutationPoint],
    id_glob: str | None,
    kind_filter: str | None,
) -> list[MutationPoint]:
    out: list[MutationPoint] = []
    for p in points:
        if id_glob is not None and not _glob_match(p.id, id_glob):
            continue
        if kind_filter is not None and p.kind != kind_filter:
            continue
        out.append(p)
    return out


def _preview(content: str) -> str:
    flat = content.replace("\n", " ").replace("\r", " ")
    flat = " ".join(flat.split())
    if len(flat) <= _PREVIEW_LEN:
        return flat
    return flat[: _PREVIEW_LEN - 1] + "…"


def _render_table(points: list[MutationPoint], show_mode: str) -> str:
    if not points:
        return "(no mutation points found)\n"
    header = f"{'id':<32}  {'kind':<5}  {'lines':<11}  {'file':<40}  preview"
    lines = [header, "-" * len(header)]
    for p in points:
        line_range = f"{p.line_start}-{p.line_end}"
        try:
            file_disp = str(p.file.relative_to(p.source_root))
        except ValueError:
            file_disp = str(p.file)
        if show_mode == "full":
            preview = "\n    " + p.content.replace("\n", "\n    ")
        else:
            preview = _preview(p.content)
        lines.append(f"{p.id:<32}  {p.kind:<5}  {line_range:<11}  {file_disp:<40}  {preview}")
    total = len(points)
    by_kind: dict[str, int] = {}
    mutable_lines = 0
    for p in points:
        by_kind[p.kind] = by_kind.get(p.kind, 0) + 1
        mutable_lines += max(0, p.line_end - p.line_start + 1)
    kind_breakdown = ", ".join(f"{k}={v}" for k, v in sorted(by_kind.items()))
    lines.append("")
    lines.append(
        f"Total: {total} mutation point(s)  [{kind_breakdown}]  ~{mutable_lines} mutable line(s)"
    )
    return "\n".join(lines) + "\n"


def _render_json(points: list[MutationPoint], show_mode: str) -> str:
    items = []
    for p in points:
        item = {
            "id": p.id,
            "kind": p.kind,
            "file": str(p.file),
            "source_root": str(p.source_root),
            "line_start": p.line_start,
            "line_end": p.line_end,
            "content_hash": p.content_hash,
            "metadata": dict(p.metadata),
        }
        if show_mode == "full":
            item["content"] = p.content
        else:
            item["preview"] = _preview(p.content)
        items.append(item)
    total = len(points)
    by_kind: dict[str, int] = {}
    mutable_lines = 0
    for p in points:
        by_kind[p.kind] = by_kind.get(p.kind, 0) + 1
        mutable_lines += max(0, p.line_end - p.line_start + 1)
    payload = {
        "points": items,
        "summary": {
            "total": total,
            "by_kind": by_kind,
            "mutable_lines": mutable_lines,
        },
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


@click.command(
    name="mutations",
    short_help="Advanced: audit the mutable surface the proposer may change.",
)
@click.option(
    "--workspace",
    default=".zicato",
    type=click.Path(),
    show_default=True,
    help="Path to the zicato workspace directory.",
)
@click.option(
    "--id",
    "id_glob",
    default=None,
    help="Filter mutation points by id glob (e.g. 'researcher_*').",
)
@click.option(
    "--kind",
    "kind_filter",
    type=click.Choice(["span", "file", "code"]),
    default=None,
    help="Restrict the listing to one mutation kind.",
)
@click.option(
    "--show",
    "show_mode",
    type=click.Choice(["preview", "full"]),
    default="preview",
    show_default=True,
    help="Truncate content previews (preview) or dump full content (full).",
)
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["table", "json"]),
    default="table",
    show_default=True,
    help="Output format.",
)
def mutations_cmd(
    workspace: str,
    id_glob: str | None,
    kind_filter: str | None,
    show_mode: str,
    fmt: str,
) -> None:
    """Advanced: list the mutable spans in the registered system under test.

    Off the happy path — `zicato evolve` enumerates these itself.
    Use this to audit what the proposer is allowed to change.
    """

    workspace_dir = Path(workspace)
    source_roots = _load_source_roots(workspace_dir)
    activate_mutation_surface(workspace_dir)
    points = enumerate_mutations(source_roots)
    filtered = _filter_points(points, id_glob, kind_filter)
    if fmt == "json":
        click.echo(_render_json(filtered, show_mode), nl=False)
    else:
        click.echo(_render_table(filtered, show_mode), nl=False)


__all__ = ["mutations_cmd"]
