"""``zicato propose`` — generate a new :class:`Experiment` for the next generation.

Standalone command file. Auto-discovered by the CLI-infrastructure
agent under ``zicato/cli/commands/``; intentionally does not import from
``zicato.cli`` so discovery stays one-way.

The command stitches together the proposer's inputs from the workspace:

* Workspace config (``<workspace>/config.json``) → source roots, current
  epoch, model id for the auxiliary LLM.
* Epoch config (``<workspace>/epochs/<epoch_id>/scoring.json``) and
  rubric (``<workspace>/epochs/<epoch_id>/rubric.md``).
* Latest generation → mutation manifest. If a ``mutations.json`` is
  cached for the latest generation, it is read; otherwise the command
  re-enumerates from the source roots if the enumerator is importable.
* Cross-run loss patterns. Either read from ``--patterns-from <file>``
  (JSON) or, when absent and the detectors package is importable, run
  fresh against the latest generation's loss profiles.

The orchestration is intentionally tolerant of missing sibling
packages: ``zicato.mutation`` and ``zicato.patterns`` are imported
lazily so this command file can be installed and exercised before its
sibling packages land.
"""

from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path
from typing import Any

import click

from zicato.core.types import MutationPoint, Pattern
from zicato.core.workspace import (
    experiment_json_path,
    generation_dir,
    rubric_path,
)
from zicato.epoch.journal import write_experiment
from zicato.proposer.proposer import ProposerError, propose_experiment
from zicato.proposer.rubric import load_rubric

# ---------------------------------------------------------------------------
# Workspace loading helpers
# ---------------------------------------------------------------------------


def _load_workspace_config(workspace_dir: Path) -> dict[str, Any]:
    config_path = workspace_dir / "config.json"
    if not config_path.exists():
        raise click.ClickException(
            f"No workspace config at {config_path}. Run `zicato register` first."
        )
    try:
        loaded: dict[str, Any] = json.loads(config_path.read_text(encoding="utf-8"))
        return loaded
    except json.JSONDecodeError as exc:
        raise click.ClickException(f"Could not parse {config_path}: {exc}") from exc


def _resolve_epoch(workspace_dir: Path, override: str | None) -> str:
    """Resolve the active epoch id.

    When ``--epoch`` is supplied, it wins. Otherwise we read
    ``<workspace>/current_epoch`` (a single-line file). Falls back to
    raising a click error if neither is set.
    """

    if override:
        return override
    current_path = workspace_dir / "current_epoch"
    if current_path.exists():
        text = current_path.read_text(encoding="utf-8").strip()
        if text:
            return text
    raise click.ClickException(
        f"No active epoch. Either pass --epoch or write the id to "
        f"{current_path}."
    )


def _list_generations(workspace_dir: Path, epoch_id: str) -> list[str]:
    """List existing generation ids under one epoch, sorted by suffix.

    Generation ids follow the ``v0``, ``v1``, ... convention; sorting by
    the integer suffix gives the right ordering. Ids that don't match
    the pattern are kept but ordered lexicographically after the typed
    ones — they shouldn't exist in a healthy workspace but we'd rather
    surface them than silently drop them.
    """

    gen_dir = workspace_dir / "epochs" / epoch_id / "generations"
    if not gen_dir.exists():
        return []
    entries = [p.name for p in gen_dir.iterdir() if p.is_dir()]

    def _sort_key(name: str) -> tuple[int, int, str]:
        if name.startswith("v") and name[1:].isdigit():
            return (0, int(name[1:]), name)
        return (1, 0, name)

    return sorted(entries, key=_sort_key)


def _next_generation_id(existing: list[str]) -> str:
    """Choose the id for the new child generation."""

    max_v = -1
    for name in existing:
        if name.startswith("v") and name[1:].isdigit():
            v = int(name[1:])
            if v > max_v:
                max_v = v
    return f"v{max_v + 1}"


def _load_mutations(workspace_dir: Path, epoch_id: str, parent_gen: str) -> list[MutationPoint]:
    """Load mutation points for the parent generation.

    Prefers a cached ``mutations.json`` under the generation directory.
    Falls back to importing :mod:`zicato.mutation` and re-enumerating
    against the workspace's source roots. When neither path works,
    raises a click error pointing the operator at ``zicato mutations``.
    """

    gen_dir = generation_dir(workspace_dir, epoch_id, parent_gen)
    cache_path = gen_dir / "mutations.json"
    if cache_path.exists():
        try:
            data = json.loads(cache_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise click.ClickException(
                f"Could not parse {cache_path}: {exc}"
            ) from exc
        return [
            MutationPoint(
                id=item["id"],
                kind=item["kind"],
                file=Path(item["file"]),
                source_root=Path(item["source_root"]),
                line_start=int(item["line_start"]),
                line_end=int(item["line_end"]),
                content=item.get("content", ""),
                content_hash=item["content_hash"],
                metadata=dict(item.get("metadata", {})),
            )
            for item in data.get("points", [])
        ]

    try:
        mutation_pkg = importlib.import_module("zicato.mutation.enumerator")
    except ImportError as exc:
        raise click.ClickException(
            "No cached mutations.json and zicato.mutation is unavailable. "
            "Run `zicato mutations --format=json > "
            f"{cache_path}` first."
        ) from exc
    config = _load_workspace_config(workspace_dir)
    source_roots = [Path(r) for r in config.get("source_roots", [])]
    if not source_roots:
        raise click.ClickException(
            "Workspace config has no 'source_roots'; cannot enumerate mutations."
        )
    enumerate_mutations = mutation_pkg.enumerate_mutations
    return list(enumerate_mutations(source_roots))


def _load_patterns(
    workspace_dir: Path,
    epoch_id: str,
    parent_gen: str,
    patterns_from: str | None,
) -> list[Pattern]:
    """Load cross-run patterns either from a file or by running detectors."""

    if patterns_from is not None:
        path = Path(patterns_from)
        if not path.exists():
            raise click.ClickException(
                f"--patterns-from path does not exist: {path}"
            )
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise click.ClickException(
                f"Could not parse {path}: {exc}"
            ) from exc
        return [_pattern_from_dict(d) for d in data]

    try:
        detector_pkg = importlib.import_module("zicato.patterns")
    except ImportError:
        # Detectors unavailable — proceed with no patterns, the proposer
        # will still propose something based on the loss summary.
        return []

    run_detectors = getattr(detector_pkg, "run_detectors", None)
    if run_detectors is None:
        return []
    return list(run_detectors(workspace_dir, epoch_id, parent_gen))


def _pattern_from_dict(d: dict[str, Any]) -> Pattern:
    return Pattern(
        id=d["id"],
        kind=d["kind"],
        summary=d.get("summary", ""),
        detail=dict(d.get("detail", {})),
        affected_mutation_ids=tuple(d.get("affected_mutation_ids", ())),
        severity=d.get("severity", "info"),
    )


def _load_loss_summary(workspace_dir: Path, epoch_id: str, parent_gen: str) -> str:
    """Read a short loss summary for the parent generation, if cached."""

    gen_dir = generation_dir(workspace_dir, epoch_id, parent_gen)
    summary_path = gen_dir / "loss_summary.txt"
    if summary_path.exists():
        return summary_path.read_text(encoding="utf-8").strip()
    # Fall back to the gen-score summary if telemetry has dropped one there.
    score_path = gen_dir / "gen_score.json"
    if score_path.exists():
        try:
            score = json.loads(score_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return "(loss summary unavailable)"
        return json.dumps(score, indent=2, sort_keys=True)
    return "(loss summary unavailable)"


# The historic in-line serializer used to live here; the per-patch
# storage layout makes that obsolete. Writes now go through
# :func:`zicato.epoch.journal.write_experiment`. We keep the import
# pattern (asdict / Path coercion) localised in journal.py so every
# writer of an experiment routes through one helper.


# ---------------------------------------------------------------------------
# Auxiliary LLM resolution
# ---------------------------------------------------------------------------


async def _missing_aux_llm(_system: str, _user: str, _model: str) -> str:
    """Fallback callable that fails loudly when no aux LLM is wired in.

    The CLI tries to import a registered auxiliary callable from the
    workspace; if none is set, this stub is used so the failure surfaces
    at proposer call time with a clear message rather than at import
    time.
    """

    raise RuntimeError(
        "No auxiliary LLM callable is registered. Wire one into the "
        "workspace config under 'auxiliary_call_llm' (dotted import path)."
    )


def _resolve_aux_llm(config: dict[str, Any]) -> Any:
    """Look up the auxiliary LLM callable from the workspace config.

    The config field ``"auxiliary_call_llm"`` is a dotted import path
    (e.g. ``"my_pkg.llms.aux_call_llm"``). If absent, the missing-stub
    is returned so the command can still parse args and report state.
    """

    dotted = config.get("auxiliary_call_llm")
    if not dotted:
        return _missing_aux_llm
    mod_name, _, attr = dotted.rpartition(".")
    if not mod_name:
        raise click.ClickException(
            f"auxiliary_call_llm config value is not a dotted path: {dotted!r}"
        )
    try:
        module = importlib.import_module(mod_name)
    except ImportError as exc:
        raise click.ClickException(
            f"Could not import {mod_name!r} for auxiliary_call_llm: {exc}"
        ) from exc
    if not hasattr(module, attr):
        raise click.ClickException(
            f"Module {mod_name!r} has no attribute {attr!r} for auxiliary_call_llm"
        )
    return getattr(module, attr)


# ---------------------------------------------------------------------------
# Click command
# ---------------------------------------------------------------------------


@click.command(name="propose")
@click.option(
    "--workspace",
    default=".zicato",
    type=click.Path(),
    show_default=True,
    help="Path to the zicato workspace directory.",
)
@click.option(
    "--epoch",
    default=None,
    help="Epoch id. Defaults to the workspace's 'current_epoch' file contents.",
)
@click.option(
    "--patterns-from",
    default=None,
    type=click.Path(),
    help="Path to a Patterns JSON file. If absent, detectors are run fresh.",
)
@click.option(
    "--max-retries",
    default=2,
    show_default=True,
    type=click.IntRange(min=0, max=10),
    help="How many times to ask the proposer to fix a malformed response.",
)
def propose_cmd(
    workspace: str,
    epoch: str | None,
    patterns_from: str | None,
    max_retries: int,
) -> None:
    """Generate a new Experiment for the next generation."""

    workspace_dir = Path(workspace)
    config = _load_workspace_config(workspace_dir)
    epoch_id = _resolve_epoch(workspace_dir, epoch)

    rubric_file = rubric_path(workspace_dir, epoch_id)
    if not rubric_file.exists():
        raise click.ClickException(
            f"No rubric at {rubric_file}. Create it before proposing."
        )
    rubric = load_rubric(rubric_file)

    existing = _list_generations(workspace_dir, epoch_id)
    if not existing:
        raise click.ClickException(
            f"Epoch {epoch_id!r} has no generations yet; cannot propose a child."
        )
    parent_gen = existing[-1]
    new_gen = _next_generation_id(existing)

    mutations = _load_mutations(workspace_dir, epoch_id, parent_gen)
    if not mutations:
        raise click.ClickException(
            "No mutation points were enumerated; cannot propose a patch set."
        )

    patterns = _load_patterns(workspace_dir, epoch_id, parent_gen, patterns_from)
    loss_summary = _load_loss_summary(workspace_dir, epoch_id, parent_gen)
    aux_call_llm = _resolve_aux_llm(config)
    model = config.get("auxiliary_model", "")

    try:
        experiment = asyncio.run(
            propose_experiment(
                epoch_id=epoch_id,
                parent_generation_id=parent_gen,
                new_generation_id=new_gen,
                patterns=patterns,
                mutations=mutations,
                rubric_text=rubric.text,
                current_loss_summary=loss_summary,
                aux_call_llm=aux_call_llm,
                model=model,
                max_retries=max_retries,
                forbidden_ids=rubric.forbidden_ids,
            )
        )
    except ProposerError as exc:
        raise click.ClickException(str(exc)) from exc

    write_experiment(workspace_dir, epoch_id, new_gen, experiment)
    out_path = experiment_json_path(workspace_dir, epoch_id, new_gen)
    click.echo(
        f"Wrote experiment {experiment.id} for {epoch_id}/{new_gen} "
        f"to {out_path}"
    )


__all__ = ["propose_cmd"]
