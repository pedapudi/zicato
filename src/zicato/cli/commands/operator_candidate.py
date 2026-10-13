"""``zicato proposer checkout`` and ``zicato proposer submit`` — queue an operator round.

An operator, or an agent that works for one, can enter candidates into the
next evolve round without proposal episodes:

1. ``checkout`` copies the epoch's current champion to a directory, with a
   ``hypothesis.json`` template, the proposer-visible evidence, and a record
   of where the copy came from. Run it once for each candidate.
2. The operator edits each ``source/`` inside the declared mutation points
   and fills in each hypothesis.
3. ``submit`` reads each edit back with the proposal episode's own
   projection, validates the patches and the hypothesis as a proposer's are
   validated, and queues the copies as one round.

The next round puts the queued candidates in its first slots
(:mod:`zicato.runtime.operator_queue`); the proposer fills the other slots
unless the round is exclusive. If a promotion happens first, each operator
version runs as written against the new champion.

Neither command takes the workspace writer lock or administers git
worktrees: ``checkout`` reads, and ``submit`` validates in scratch
directories and writes one queue file. Both can run while ``zicato evolve``
runs on the same workspace. Both print one JSON object, also on failure,
and exit 1 when they refuse.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn

import click

from zicato.core.types import MutationPoint, Patch

#: The record ``checkout`` writes beside the copy. ``submit`` reads it.
CHECKOUT_RECORD = "checkout.json"
#: The checkout record version. ``submit`` refuses any other value.
CHECKOUT_FORMAT = 1
#: The editable copy of the generation, under the checkout directory.
SOURCE_DIR = "source"
#: The proposer-visible evidence, under the checkout directory.
EVIDENCE_DIR = "evidence"
#: The hypothesis template ``checkout`` writes and ``submit`` reads.
HYPOTHESIS_FILE = "hypothesis.json"


class _Refused(Exception):
    """A command refuses; ``payload`` is the JSON it prints."""

    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__(json.dumps(payload))
        self.payload = payload


def _refuse(message: str) -> NoReturn:
    raise _Refused({"ok": False, "findings": [message]})


def _echo_json(payload: dict[str, Any]) -> None:
    click.echo(json.dumps(payload, indent=2, sort_keys=True))


def _run_json(body: Callable[[], dict[str, Any]]) -> None:
    """Run a command body and print its JSON; print a refusal as JSON and exit 1."""
    from zicato.epoch._storage import RecordError  # noqa: PLC0415

    try:
        payload = body()
    except _Refused as exc:
        _echo_json(exc.payload)
        raise SystemExit(1) from None
    # RuntimeError covers a failed git command (GitCommandError) and an
    # adapter that cannot be built; an agent parses stdout, so no traceback.
    except (OSError, ValueError, RuntimeError, RecordError) as exc:
        _echo_json({"ok": False, "findings": [str(exc)]})
        raise SystemExit(1) from None
    _echo_json(payload)


def _workspace_config(workspace_root: Path) -> Any:
    from zicato.workspace.config_io import read_workspace_config  # noqa: PLC0415

    config = read_workspace_config(workspace_root)
    if not config.exists:
        _refuse(f"No workspace config at {config.path}. Run `zicato init` first.")
    return config


def _current_epoch(workspace_root: Path) -> str:
    from zicato.epoch.lifecycle import current_epoch_id  # noqa: PLC0415

    epoch_id = current_epoch_id(workspace_root)
    if not epoch_id:
        _refuse(
            f"No epoch under {workspace_root} yet. Run `zicato epoch new` first: "
            "the seed generation v0 exists only after its epoch exists."
        )
    return epoch_id


@contextmanager
def _generation_tree(
    workspace_root: Path, epoch_id: str, generation_id: str, raw_config: Any
) -> Iterator[Path]:
    """Yield a private copy of the source tree of ``generation_id``.

    The copy comes from :meth:`~zicato.epoch.genstore.GenerationStore.export_snapshot`,
    which changes no shared store state. Before the first round of an epoch,
    the store holds no ``v0`` yet: the round seeds it. The copy then comes
    from the epoch's retained seed source, or, for an epoch with no seed
    record, from the registered source trees, copied with the seed's own
    preparation so the layout matches.
    """
    from zicato.epoch.genstore import default_generation_store  # noqa: PLC0415

    store = default_generation_store(workspace_root)
    with tempfile.TemporaryDirectory(prefix="zicato-operator-") as temporary:
        tree = Path(temporary) / "tree"
        if store.has_generation(epoch_id, generation_id):
            store.export_snapshot(epoch_id, generation_id, tree)
        elif generation_id != "v0":
            _refuse(f"Generation {epoch_id}/{generation_id} has no source tree in the workspace.")
        else:
            _copy_unseeded_v0(workspace_root, epoch_id, raw_config, store.backend_name, tree)
        yield tree


def _copy_unseeded_v0(
    workspace_root: Path, epoch_id: str, raw_config: Any, backend: str, tree: Path
) -> None:
    from zicato.core.adapter_config import registered_mutable_trees  # noqa: PLC0415
    from zicato.epoch.baseline import validate_baseline_seed  # noqa: PLC0415
    from zicato.epoch.publication import BaselineSeed  # noqa: PLC0415
    from zicato.epoch.seed_sources import (  # noqa: PLC0415
        GIT_ADMIN_BASENAMES,
        prepare_seed_sources,
        validated_seed_sources,
    )

    seed = BaselineSeed.read(workspace_root, epoch_id)
    if seed is not None:
        shutil.copytree(validate_baseline_seed(workspace_root, seed), tree, symlinks=True)
        return
    trees = registered_mutable_trees(raw_config, workspace_root)
    if not trees:
        _refuse(
            "The workspace declares no source trees, so it has no seed to copy. "
            "Run `zicato epoch register` first."
        )
    excluded = GIT_ADMIN_BASENAMES if backend == "git" else frozenset()
    prepare_seed_sources(validated_seed_sources(trees, excluded_names=excluded), tree)


@dataclass(frozen=True, slots=True)
class _Surface:
    """The mutation surface of one tree, resolved as the round resolves it."""

    points: list[MutationPoint]
    roots: list[Path]


def _mutation_surface(workspace_root: Path, contract: Any, tree: Path) -> _Surface:
    """Enumerate ``tree`` under the frozen adapter's mutable trees, as a round does."""
    from zicato.adapter_factory import make_adapter_from_config  # noqa: PLC0415
    from zicato.evolve.generation_phase import mutable_trees  # noqa: PLC0415
    from zicato.mutation.enumerator import enumerate_mutations  # noqa: PLC0415
    from zicato.workspace_loader import activate_mutation_surface  # noqa: PLC0415

    activate_mutation_surface(workspace_root)
    adapter = make_adapter_from_config(
        contract.adapter_configuration, workspace_root=workspace_root
    )
    roots = mutable_trees(adapter, tree)
    points = list(enumerate_mutations(roots))
    if not points:
        _refuse(f"No mutation points are declared under {tree}.")
    return _Surface(points=points, roots=roots)


def _load_contract(workspace_root: Path, epoch_id: str) -> Any:
    from zicato.epoch.execution import load_epoch_execution_contract  # noqa: PLC0415

    return load_epoch_execution_contract(workspace_root, epoch_id)


def _hypothesis_template() -> dict[str, Any]:
    """An empty hypothesis in the proposer schema, for the operator to fill in."""
    return {
        "core_idea": "",
        "modulating": [],
        "why": "",
        "expected_pass_rate_delta": "",
        "expected_metric_movements": [
            {"metric_name": "", "direction": "decrease", "magnitude": "small"}
        ],
        "risks": "",
    }


@click.command(
    name="checkout",
    short_help="Copy the current champion for an operator candidate.",
)
@click.option(
    "--workspace",
    default=".zicato",
    type=click.Path(),
    show_default=True,
    help="Path to the zicato workspace directory.",
)
@click.option(
    "--into",
    "into",
    required=True,
    type=click.Path(file_okay=False),
    help="New directory for the copy. It must not exist, or must be empty.",
)
def checkout_cmd(workspace: str, into: str) -> None:
    """Copy the current epoch's champion so an operator can edit a candidate.

    Writes source/ (the editable copy), hypothesis.json (a template in the
    proposer schema), evidence/ (the epoch's brief, mutation inventory and
    captured proposer inputs, when present) and checkout.json (where the
    copy came from). Edit only inside the declared mutation points, then run
    `zicato proposer submit`. Prints JSON.
    """
    _run_json(lambda: _checkout(Path(workspace).resolve(), Path(into).resolve()))


def _checkout(workspace_root: Path, target: Path) -> dict[str, Any]:
    from zicato.driver_imports import (  # noqa: PLC0415
        driver_import_scope,
        workspace_driver_imports,
    )
    from zicato.evolve.generation_phase import current_generation  # noqa: PLC0415
    from zicato.util.iso_time import now_iso  # noqa: PLC0415
    from zicato.workspace import WorkspaceLayout  # noqa: PLC0415

    config = _workspace_config(workspace_root)
    epoch_id = _current_epoch(workspace_root)
    if target.exists() and any(target.iterdir()):
        _refuse(f"{target} is not empty.")
    champion = current_generation(workspace_root, epoch_id)
    contract = _load_contract(workspace_root, epoch_id)

    with (
        driver_import_scope(workspace_driver_imports(workspace_root)),
        _generation_tree(workspace_root, epoch_id, champion, config.raw) as tree,
    ):
        surface = _mutation_surface(workspace_root, contract, tree)
        target.mkdir(parents=True, exist_ok=True)
        shutil.copytree(tree, target / SOURCE_DIR, symlinks=True)
        relative_points = [
            {"id": point.id, "kind": point.kind, "file": str(point.file.relative_to(tree))}
            for point in sorted(surface.points, key=lambda p: p.id)
        ]

    layout = WorkspaceLayout.from_root(workspace_root)
    evidence_dir = target / EVIDENCE_DIR
    evidence_dir.mkdir()
    evidence: list[str] = []
    for source, name in (
        (layout.brief(epoch_id), "brief.md"),
        (layout.mutations(epoch_id), "mutations.json"),
        (layout.proposer_inputs(epoch_id), "proposer_inputs.jsonl"),
    ):
        if source.is_file():
            shutil.copyfile(source, evidence_dir / name)
            evidence.append(name)

    (target / HYPOTHESIS_FILE).write_text(
        json.dumps(_hypothesis_template(), indent=2) + "\n", encoding="utf-8"
    )
    (target / CHECKOUT_RECORD).write_text(
        json.dumps(
            {
                "format": CHECKOUT_FORMAT,
                "workspace": str(workspace_root),
                "epoch_id": epoch_id,
                "based_on": champion,
                "created_at": now_iso(),
                "mutation_points": relative_points,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return {
        "ok": True,
        "workspace": str(workspace_root),
        "epoch": epoch_id,
        "based_on": champion,
        "source": str(target / SOURCE_DIR),
        "hypothesis": str(target / HYPOTHESIS_FILE),
        "evidence": evidence,
        "mutation_points": [point["id"] for point in relative_points],
    }


def _read_checkout_record(directory: Path) -> dict[str, Any]:
    path = directory / CHECKOUT_RECORD
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _refuse(f"Cannot read {path}: {exc}. Run `zicato proposer checkout` to make the copy.")
    if not isinstance(record, dict) or record.get("format") != CHECKOUT_FORMAT:
        _refuse(f"{path} is not a checkout record this zicato can read.")
    for key in ("workspace", "epoch_id", "based_on"):
        if not isinstance(record.get(key), str) or not record[key]:
            _refuse(f"{path} has no {key}.")
    return record


def _read_hypothesis(path: Path) -> dict[str, Any]:
    try:
        hypothesis = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _refuse(f"Cannot read the hypothesis {path}: {exc}")
    if not isinstance(hypothesis, dict):
        _refuse(f"The hypothesis {path} must be a JSON object.")
    return hypothesis


@dataclass(slots=True)
class _CheckedCopy:
    """One copy that ``submit`` validated, with its findings and notes."""

    directory: Path
    based_on: str
    hypothesis: dict[str, Any]
    patches: list[Patch] = field(default_factory=list)
    findings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def report(self) -> dict[str, Any]:
        return {
            "from": str(self.directory),
            "based_on": self.based_on,
            "patches": len(self.patches),
            "findings": self.findings,
            "notes": self.notes,
        }


def _field_size(contract: Any) -> int:
    """The number of challenger slots a round of this epoch has."""
    from zicato.selection.registry import make_strategy  # noqa: PLC0415

    board, _drift, _judge_only = contract.board_with_meta
    scoring = contract.scoring
    return make_strategy(
        scoring.tournament_structure,
        board_ids=[entry.id for entry in board],
        experimental=scoring.experimental,
    ).field_size()


def _check_copy(
    copy: _CheckedCopy,
    *,
    workspace_root: Path,
    epoch_id: str,
    contract: Any,
    raw_config: Any,
) -> None:
    """Project one edited copy and validate it as a proposer's output is validated."""
    from zicato.evolve.operator_slot import (  # noqa: PLC0415
        operator_rationale,
        parse_operator_experiment,
        patch_document,
    )
    from zicato.evolve.round_api import declared_custom_judge_names  # noqa: PLC0415
    from zicato.mutation.policy import MutationPolicy  # noqa: PLC0415
    from zicato.proposer.foe_scratch import (  # noqa: PLC0415
        EditOutsideMutationPointError,
        project_working_copy,
    )
    from zicato.proposer.structured import ExperimentParseError  # noqa: PLC0415
    from zicato.proposer.tool_context import (  # noqa: PLC0415
        ProposerToolContext,
        bind_proposer_tool_context,
    )
    from zicato.proposer.validate import validate_patches  # noqa: PLC0415

    board, _drift, _judge_only = contract.board_with_meta
    brief = contract.brief
    with _generation_tree(workspace_root, epoch_id, copy.based_on, raw_config) as tree:
        surface = _mutation_surface(workspace_root, contract, tree)
        policy = MutationPolicy.capture(
            tree, surface.points, brief.forbidden_ids, enumeration_roots=surface.roots
        )
        try:
            copy.patches = project_working_copy(policy, copy.directory / SOURCE_DIR)
        except EditOutsideMutationPointError as exc:
            copy.findings = list(exc.findings)
            return
        if not copy.patches:
            copy.findings = ["the copy is unchanged; edit a declared mutation point first"]
            return
        try:
            parse_operator_experiment(
                copy.hypothesis,
                copy.patches,
                epoch_id=epoch_id,
                parent_id=copy.based_on,
                next_id="queued",
                mutations=surface.points,
                custom_judge_names=declared_custom_judge_names(board, contract.scoring),
            )
        except ExperimentParseError as exc:
            copy.findings = [str(exc)]
            return
        # The same context a round's proposal episode validates under: the
        # contract's static checks and the frozen adapter for the load probe.
        context = ProposerToolContext(
            workspace_root=workspace_root,
            generation_root=tree,
            epoch_id=epoch_id,
            mutations=tuple(surface.points),
            generation_id=copy.based_on,
            static_checks=contract.static_checks,
            adapter_configuration_json=json.dumps(contract.adapter_configuration).encode(),
            forbidden_ids=tuple(brief.forbidden_ids),
            mutation_policy=policy,
        )
        rationale = operator_rationale(copy.hypothesis)
        with bind_proposer_tool_context(context):
            report = json.loads(
                validate_patches(json.dumps([patch_document(p, rationale) for p in copy.patches]))
            )
    copy.findings = [str(error) for error in report.get("errors") or ()]
    stages = report.get("stages") or {}
    copy.notes = [str(note) for stage in stages.values() for note in stage.get("notes") or ()]


@click.command(
    name="submit",
    short_help="Validate edited copies and queue them as the next round.",
)
@click.option(
    "--from",
    "from_dirs",
    required=True,
    multiple=True,
    type=click.Path(exists=True, file_okay=False),
    help="A directory that `zicato proposer checkout` made. Repeat it to queue "
    "several candidates in one round.",
)
@click.option(
    "--author",
    required=True,
    help="Who wrote the candidates: a person or an agent. Recorded with each.",
)
@click.option(
    "--holdout-access",
    type=click.Choice(["yes", "no"]),
    default=None,
    help="Whether the author could read holdout results. Recorded with each candidate.",
)
@click.option(
    "--exclusive",
    is_flag=True,
    default=False,
    help="Run these candidates alone in their round, with no proposal episodes.",
)
@click.option(
    "--workspace",
    default=None,
    type=click.Path(),
    help="Workspace to queue into. Defaults to the one the copies came from; "
    "any other workspace is an error.",
)
def submit_cmd(
    from_dirs: Sequence[str],
    author: str,
    holdout_access: str | None,
    exclusive: bool,
    workspace: str | None,
) -> None:
    """Validate edited copies and queue them as one round of candidates.

    Each edit is read back as patches with the proposal episode's own
    projection, so an edit outside the declared mutation points fails with
    the message a proposer gets. The patches then pass the proposer's
    validation (structure, application, declared static checks and the load
    probe), and each FROM/hypothesis.json passes the proposer's parser. The
    candidates fill the next round's first slots; the round cannot hold more
    candidates than its field has slots. Prints JSON and exits 1 when a check
    fails; nothing is queued then.
    """
    _run_json(
        lambda: _submit(
            [Path(d).resolve() for d in from_dirs],
            author=author,
            holdout_access=None if holdout_access is None else holdout_access == "yes",
            exclusive=exclusive,
            workspace=None if workspace is None else Path(workspace).resolve(),
        )
    )


def _submit(
    directories: list[Path],
    *,
    author: str,
    holdout_access: bool | None,
    exclusive: bool,
    workspace: Path | None,
) -> dict[str, Any]:
    from zicato.driver_imports import (  # noqa: PLC0415
        driver_import_scope,
        workspace_driver_imports,
    )
    from zicato.evolve.operator_slot import queued_patch_body  # noqa: PLC0415
    from zicato.runtime.operator_queue import (  # noqa: PLC0415
        QueuedCandidate,
        QueuedRound,
        enqueue_operator_round,
        new_queue_id,
    )
    from zicato.util.iso_time import now_iso  # noqa: PLC0415

    author = author.strip()
    if not author:
        _refuse("--author must name a person or an agent.")
    if len(set(directories)) != len(directories):
        _refuse("The same copy is named more than once.")
    records = [_read_checkout_record(directory) for directory in directories]
    workspaces = {Path(record["workspace"]) for record in records}
    if len(workspaces) != 1:
        _refuse(
            "The copies come from different workspaces. Submit the copies of each "
            "workspace in a separate command."
        )
    (workspace_root,) = workspaces
    if workspace is not None and workspace != workspace_root:
        _refuse(
            f"The copies were checked out from {workspace_root}, not {workspace}. "
            "Check out from that workspace instead."
        )
    config = _workspace_config(workspace_root)
    epoch_id = _current_epoch(workspace_root)
    stale = [str(d) for d, r in zip(directories, records, strict=True) if r["epoch_id"] != epoch_id]
    if stale:
        _refuse(
            f"The workspace is now in epoch {epoch_id!r}, so these copies are stale: "
            f"{', '.join(stale)}. Check out the current champion again."
        )
    contract = _load_contract(workspace_root, epoch_id)
    slots = _field_size(contract)
    if len(directories) > slots:
        _refuse(
            f"A round of this epoch has {slots} challenger slot(s), so it cannot hold "
            f"{len(directories)} candidates."
        )

    copies = [
        _CheckedCopy(
            directory=directory,
            based_on=record["based_on"],
            hypothesis=_read_hypothesis(directory / HYPOTHESIS_FILE),
        )
        for directory, record in zip(directories, records, strict=True)
    ]
    with driver_import_scope(workspace_driver_imports(workspace_root)):
        for copy in copies:
            _check_copy(
                copy,
                workspace_root=workspace_root,
                epoch_id=epoch_id,
                contract=contract,
                raw_config=config.raw,
            )
    if any(copy.findings for copy in copies):
        raise _Refused({"ok": False, "candidates": [copy.report() for copy in copies]})

    submitted_at = now_iso()
    queued = QueuedRound(
        queue_id=new_queue_id(),
        epoch_id=epoch_id,
        author=author,
        submitted_at=submitted_at,
        candidates=tuple(
            QueuedCandidate(
                based_on=copy.based_on,
                hypothesis=copy.hypothesis,
                patches=tuple(queued_patch_body(patch) for patch in copy.patches),
            )
            for copy in copies
        ),
        exclusive=exclusive,
        holdout_access=holdout_access,
    )
    path = enqueue_operator_round(workspace_root, queued)
    return {
        "ok": True,
        "queue_id": queued.queue_id,
        "queue_file": str(path),
        "workspace": str(workspace_root),
        "epoch": epoch_id,
        "slots": slots,
        "exclusive": exclusive,
        "candidates": [copy.report() for copy in copies],
    }


__all__ = ["checkout_cmd", "submit_cmd"]
