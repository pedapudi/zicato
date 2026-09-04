"""``zicato proposer propose`` — run one proposal episode and keep its experiment.

ADVANCED / DEBUGGING — off the happy path. ``zicato evolve`` proposes on
every round. Run this by hand only to see what the proposer does with a
workspace's current evidence, without spending a tournament on it.

It is the same episode. The command assembles the round's proposal
context and hands it to the agent the round resolves, which builds its
request through :func:`zicato.proposer.foe_request.build_request` — so
what an operator debugs here is what the loop runs, rather than a second
stitching of the same inputs that drifts from it.

What it includes of the round's inputs, and what it does not:

* Included: the epoch's frozen proposer brief and skills, the mutation
  manifest enumerated from the parent generation's own snapshot, the
  cross-run loss patterns, the loss summary, the board's declared judge
  names, and the settled experiment-memory digest. These are what make a proposal grounded, and
  every one of them is already train-slice-only and redacted where the
  round redacts it.
* Not included: the round's per-round derived channels — the failure-mode
  profile, the metric priorities, the process exemplars, the genealogy
  sample and the calibration record. Each is computed by the round from
  the tournament state it is about to spend, and none is reconstructible
  outside a round without opening one.

The command is read-only with respect to the loop. It appends nothing
to the lineage, opens no tournament, records no outcome and reads no
unit cache. Of the board it reads one thing, the set of declared judge
names, because that is what makes a predicted movement valid — never an
entry's content, and so never a holdout entry's content. Its one write
is the experiment document, under ``epochs/<epoch>/proposals/``, a
directory nothing in the loop reads.

Standalone command file. Auto-discovered under
``zicato/cli/commands/``; intentionally does not import from
``zicato.cli`` so discovery stays one-way.
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
    epoch_dir,
    generation_dir,
)
from zicato.proposer.agent import ProposerContext
from zicato.proposer.brief import load_brief
from zicato.proposer.proposer import ProposerError
from zicato.workspace import WorkspaceLayout, generation_ids, next_generation_id
from zicato.workspace.config_io import WorkspaceConfig, read_workspace_config


def _epoch_brief_path(workspace_root: Path, epoch_id: str) -> Path:
    """Path to the frozen proposer brief (``brief.md``) for one epoch.

    ``rubric.md`` is accepted as a fallback spelling of the same file.
    """
    brief = epoch_dir(workspace_root, epoch_id) / "brief.md"
    if not brief.exists():
        legacy = epoch_dir(workspace_root, epoch_id) / "rubric.md"
        if legacy.exists():
            return legacy
    return brief


# ---------------------------------------------------------------------------
# Workspace loading helpers
# ---------------------------------------------------------------------------


def _load_workspace_config(workspace_dir: Path) -> WorkspaceConfig:
    """The workspace config, or a clean CLI error naming the onramp."""
    try:
        config = read_workspace_config(workspace_dir)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    if not config.exists:
        raise click.ClickException(
            f"No workspace config at {config.path}. Run `zicato epoch register` first."
        )
    return config


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
        f"No active epoch. Either pass --epoch or write the id to {current_path}."
    )


def _list_generations(workspace_dir: Path, epoch_id: str) -> list[str]:
    """List existing generation ids under one epoch, in round-number order.

    Generation ids follow the ``v0``, ``v1``, ... convention, so ``v2``
    precedes ``v10``. An id that does not follow it is kept rather than
    dropped — it should not exist in a healthy workspace, and hiding it would
    hide the workspace's real contents.
    """

    return generation_ids(WorkspaceLayout.from_root(workspace_dir), epoch_id)


def _load_mutations(workspace_dir: Path, generation_root: Path) -> list[MutationPoint]:
    """Enumerate the mutation points of the tree the episode will edit.

    Read off the parent generation's materialised snapshot rather than
    the workspace's ``source_roots``, and rather than a cached manifest:
    the episode edits a copy of that snapshot, and every change it makes
    is projected back onto these points by path and line range. A
    manifest describing any other tree names files the projection cannot
    match, and every proposal would be refused as an edit outside every
    declared point.
    """
    try:
        mutation_pkg = importlib.import_module("zicato.mutation.enumerator")
    except ImportError as exc:  # pragma: no cover - the package ships with zicato
        raise click.ClickException(f"zicato.mutation is unavailable: {exc}") from exc

    from zicato.workspace_loader import activate_mutation_surface  # noqa: PLC0415

    activate_mutation_surface(workspace_dir)
    return list(mutation_pkg.enumerate_mutations([generation_root]))


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
            raise click.ClickException(f"--patterns-from path does not exist: {path}")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise click.ClickException(f"Could not parse {path}: {exc}") from exc
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


def _load_custom_judge_names(workspace_dir: Path) -> frozenset[str]:
    """Return the names of custom judges declared by the active contract.

    The union of every board entry's ``JudgeSpec.name`` and every
    ``per_judge_weights`` key. These are valid ``drift:<judge_name>``
    metric targets in a proposer hypothesis. Best-effort: any load failure
    yields the empty set, so the proposer falls back to built-in-only
    drift-kind validation rather than crashing.
    """
    try:
        from zicato import workspace_loader  # noqa: PLC0415

        board = workspace_loader.load_current_board(workspace_dir)
        weights = workspace_loader.load_current_scoring(workspace_dir)
    except Exception:  # noqa: BLE001 — advisory; never block a manual propose
        return frozenset()
    names: set[str] = set()
    for entry in board:
        for judge in getattr(entry, "judges", ()) or ():
            judge_name = getattr(judge, "name", None)
            if judge_name:
                names.add(str(judge_name))
    names.update(str(k) for k in (getattr(weights, "per_judge_weights", None) or {}))
    return frozenset(names)


def _write_proposal(
    workspace_dir: Path, epoch_id: str, generation_id: str, experiment: Any
) -> Path:
    """Write one debug proposal under the epoch, outside the loop's tree.

    Deliberately NOT
    :func:`zicato.epoch.journal.write_experiment`: that mints the
    generation the loop is about to mint, so a debugging run would leave a
    half-built generation in the epoch's own sequence. A proposal lands in
    ``proposals/`` instead, named for the generation it was proposed for,
    where re-running the command overwrites its own last answer and
    nothing else.
    """
    from dataclasses import asdict  # noqa: PLC0415

    out_dir = epoch_dir(workspace_dir, epoch_id) / "proposals"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{generation_id}.json"
    path.write_text(
        json.dumps(asdict(experiment), indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    return path


def _resolve_agent(
    workspace_dir: Path, config: WorkspaceConfig, epoch_id: str, parent_gen: str
) -> tuple[Any, Path]:
    """The agent the round would resolve, and the tree it proposes against.

    Resolved from the same three inputs the round resolves it from — the
    workspace's declared proposer, the epoch's frozen proposer directory,
    and the parent generation's materialised snapshot — so a debugging
    proposal runs the epoch's own proposer rather than a stand-in for it.
    """
    from zicato.epoch.genstore import default_generation_store  # noqa: PLC0415
    from zicato.epoch.lifecycle import load_epoch  # noqa: PLC0415
    from zicato.proposer.agent import build_proposer_agent  # noqa: PLC0415
    from zicato.proposer.external import external_proposer_config  # noqa: PLC0415
    from zicato.proposer.skills import resolve_proposer_spec  # noqa: PLC0415

    binding = external_proposer_config(config.raw, workspace_dir)
    if binding is None:
        raise click.ClickException(
            f"{config.path} declares no `proposer` block, so this workspace has "
            "not said how it proposes. Add one naming the absolute path of the "
            "Foe binary its proposal episodes run (see docs/design/PROPOSER.md)."
        )
    epoch_cfg = load_epoch(workspace_dir, epoch_id)
    spec = resolve_proposer_spec(epoch_cfg.proposer_path, binding)
    try:
        agent = build_proposer_agent(
            spec, proposer_path=epoch_cfg.proposer_path, external_config=binding
        )
        generation_root = default_generation_store(workspace_dir).materialize_snapshot(
            epoch_id, parent_gen
        )
    except (ValueError, FileNotFoundError) as exc:
        raise click.ClickException(str(exc)) from exc
    return agent, generation_root


# ---------------------------------------------------------------------------
# Evaluation LLM resolution
# ---------------------------------------------------------------------------


async def _missing_aux_llm(_system: str, _user: str, _model: str) -> str:
    """Fallback callable that fails loudly when no aux LLM is wired in.

    The CLI tries to import a registered evaluation callable from the
    workspace; if none is set, this stub is used so the failure surfaces
    at proposer call time with a clear message rather than at import
    time.
    """

    raise RuntimeError(
        "No evaluation LLM callable is registered. Wire one into the "
        "workspace config under 'evaluation_call_llm' (dotted import path)."
    )


def _resolve_aux_llm(config: WorkspaceConfig) -> Any:
    """Look up the evaluation LLM callable from the workspace config.

    The config field ``"evaluation_call_llm"`` is a dotted import path
    (e.g. ``"my_pkg.llms.aux_call_llm"``). If absent, the missing-stub
    is returned so the command can still parse args and report state.
    """

    dotted = config.raw.get("evaluation_call_llm")
    if not dotted:
        return _missing_aux_llm
    mod_name, _, attr = dotted.rpartition(".")
    if not mod_name:
        raise click.ClickException(
            f"evaluation_call_llm config value is not a dotted path: {dotted!r}"
        )
    try:
        module = importlib.import_module(mod_name)
    except ImportError as exc:
        raise click.ClickException(
            f"Could not import {mod_name!r} for evaluation_call_llm: {exc}"
        ) from exc
    if not hasattr(module, attr):
        raise click.ClickException(
            f"Module {mod_name!r} has no attribute {attr!r} for evaluation_call_llm"
        )
    return getattr(module, attr)


# ---------------------------------------------------------------------------
# Click command
# ---------------------------------------------------------------------------


@click.command(
    name="propose",
    short_help="Advanced: generate one Experiment for the next generation.",
)
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
    """Advanced: generate one Experiment for the next generation.

    Off the happy path — `zicato evolve` proposes on every round.
    Run this by hand only to produce and inspect a single experiment
    without running the tournament.
    """

    workspace_dir = Path(workspace)
    config = _load_workspace_config(workspace_dir)
    epoch_id = _resolve_epoch(workspace_dir, epoch)

    brief_file = _epoch_brief_path(workspace_dir, epoch_id)
    if not brief_file.exists():
        raise click.ClickException(
            f"No proposer brief at {brief_file}. Create it before proposing."
        )
    brief = load_brief(brief_file)

    existing = _list_generations(workspace_dir, epoch_id)
    if not existing:
        raise click.ClickException(
            f"Epoch {epoch_id!r} has no generations yet; cannot propose a child."
        )
    parent_gen = existing[-1]
    new_gen = next_generation_id(existing)

    agent, generation_root = _resolve_agent(workspace_dir, config, epoch_id, parent_gen)

    mutations = _load_mutations(workspace_dir, generation_root)
    if not mutations:
        raise click.ClickException(
            "No mutation points were enumerated; cannot propose a patch set."
        )

    patterns = _load_patterns(workspace_dir, epoch_id, parent_gen, patterns_from)
    loss_summary = _load_loss_summary(workspace_dir, epoch_id, parent_gen)
    aux_call_llm = _resolve_aux_llm(config)
    model = config.evaluation_model

    # Custom judges declared on the board / per_judge_weights are valid
    # ``drift:<judge_name>`` metric targets in a hypothesis. Best-effort:
    # if the board/scoring cannot be loaded the proposer falls back to
    # built-in-only drift-kind validation.
    custom_judge_names = _load_custom_judge_names(workspace_dir)

    # Experiment memory: feed the settled cross-round digest to the
    # standalone propose path too so the debug command matches the loop.
    # Best-effort — a missing / stale index yields an empty list and the
    # prompt section is omitted.
    from zicato.evolve.ingest import _load_prior_experiments  # noqa: PLC0415

    prior = _load_prior_experiments(workspace_dir, epoch_id)

    try:
        experiment = asyncio.run(
            agent.propose(
                ProposerContext(
                    epoch_id=epoch_id,
                    parent_generation_id=parent_gen,
                    new_generation_id=new_gen,
                    patterns=tuple(patterns),
                    mutations=tuple(mutations),
                    brief_text=brief.text,
                    current_loss_summary=loss_summary,
                    aux_call_llm=aux_call_llm,
                    model=model,
                    max_retries=max_retries,
                    forbidden_ids=brief.forbidden_ids,
                    workspace_root=workspace_dir,
                    generation_root=generation_root,
                    custom_judge_names=custom_judge_names,
                    prior_experiments=tuple(prior),
                )
            )
        )
    except ProposerError as exc:
        raise click.ClickException(str(exc)) from exc

    out_path = _write_proposal(workspace_dir, epoch_id, new_gen, experiment)
    click.echo(f"Wrote experiment {experiment.id} for {epoch_id}/{new_gen} to {out_path}")


__all__ = ["propose_cmd"]
