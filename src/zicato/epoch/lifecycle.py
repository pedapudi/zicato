"""Epoch lifecycle: new / close / list / switch / load.

An epoch is the unit of evaluation contract: a frozen board, a frozen
proposer brief, and a frozen scoring configuration. The functions in
this module are the only supported way to create, close, enumerate, and
switch between epochs on disk.

Storage layout managed here::

    {workspace_root}/
      current_epoch                # marker file, single line = epoch id
      lineage.json                 # cross-cutting DAG (see lineage.py)
      epochs/
        {epoch_id}/
          board.jsonl              # frozen board
          brief.md                 # frozen proposer brief
          scoring.json             # serialized ScoringWeights
          config.json              # EpochConfig serialized (id/name/created_at/closed/closed_at)
          journal.md               # appended per experiment (see journal.py)
          analysis.md              # written at close (see analysis.py)

Epoch ids are formed as ``{YYYY-MM-DD}_{short_name}`` where ``short_name``
is a filesystem-safe slug of the operator-supplied name. If the same
name is created twice on the same day the second call gets a numeric
suffix.

The module is small and procedural by design — there is no ``Lifecycle`` class.
Functions take ``workspace_root`` explicitly so the CLI and tests construct
calls from explicit Paths without holding on to shared state.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import re
import shutil
import sys
import tempfile
import warnings
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

from zicato.core.settings import AuxConfig
from zicato.core.types import EpochConfig, ScoringWeights
from zicato.core.workspace import (
    analysis_path,
    epoch_dir,
    journal_path,
)
from zicato.epoch._storage import (
    RECORD_FORMAT_VERSION,
    check_record_format,
    current_epoch_key,
    epoch_config_key,
)
from zicato.epoch.publication import (
    BaselineSeed,
    EpochPublication,
    epoch_publication_path,
    prepared_directory,
)
from zicato.epoch.seed_sources import seed_content_identity
from zicato.proposer.staging import acknowledge_staged_recommendations, staged_recommendations
from zicato.storage import (
    atomic_write_json,
    durable_unlink,
    publish_directory,
    sync_directory_tree,
    workspace_backend,
)
from zicato.workspace import WorkspaceLayout, list_epoch_ids

if TYPE_CHECKING:
    from zicato.board.builder import Board
    from zicato.epoch.contract import ContractInputs
    from zicato.proposer.brief import ProposerBrief
    from zicato.runtime.lock import WorkspaceLock

# A callable shape compatible with goldfive's call_llm:
# (system, user, model) -> awaitable[str].
_AuxCallLLM = Callable[[str, str, str], Awaitable[str]]


def _brief_path(workspace_root: Path, epoch_id: str) -> Path:
    """Path to the frozen proposer brief (``brief.md``) for one epoch.

    The epoch directory is owned by this module, so the brief path is defined
    here directly, keeping the name self-contained to ``zicato.epoch`` rather
    than threading it through the shared workspace-path module.
    """
    return epoch_dir(workspace_root, epoch_id) / "brief.md"


# ---------------------------------------------------------------------------
# Id construction
# ---------------------------------------------------------------------------


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(name: str) -> str:
    """Coerce a human name into a filesystem-safe slug.

    Lowercased, non-alphanumerics collapsed to underscore, leading and
    trailing underscores stripped. An empty result raises — the operator
    must give us SOMETHING to anchor the id on.
    """
    slug = _SLUG_RE.sub("_", name.lower()).strip("_")
    if not slug:
        raise ValueError(f"epoch name {name!r} produced an empty slug")
    return slug


def _today() -> str:
    """ISO date for the epoch id prefix. UTC by convention."""
    return _dt.datetime.now(_dt.UTC).date().isoformat()


def _now_iso() -> str:
    """ISO-8601 UTC second-precision timestamp."""
    return _dt.datetime.now(_dt.UTC).replace(microsecond=0).isoformat()


def _make_epoch_id(workspace_root: Path, name: str) -> str:
    """Construct ``{date}_{slug}`` with a numeric suffix if necessary."""
    base = f"{_today()}_{_slugify(name)}"
    candidate = base
    suffix = 2
    while epoch_dir(workspace_root, candidate).exists():
        candidate = f"{base}_{suffix}"
        suffix += 1
    return candidate


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------


def scoring_to_dict(weights: ScoringWeights) -> dict[str, Any]:
    """Serialize :class:`ScoringWeights` to the frozen ``scoring.json`` shape.

    Field-enumerating (and recursive over the nested
    :class:`TournamentStructure` / :class:`OverfittingConfig` /
    :class:`LadderConfig`) via
    :func:`zicato.epoch.contract_serde.dataclass_to_jsonable`, so adding a
    field to any of those dataclasses is covered automatically and the
    frozen snapshot can never silently drop a field behind the
    field-enumerating contract canonicalizer (issue #13). The output is
    byte-compatible with the historical hand-written form: the tournament
    structure is still emitted under the ``"tournament"`` key.
    """
    return weights.to_json()


def _scoring_from_dict(d: dict[str, Any]) -> ScoringWeights:
    """Parse a frozen ``scoring.json`` dict back into :class:`ScoringWeights`.

    The inverse of :func:`scoring_to_dict`, field-enumerating via
    :func:`zicato.epoch.contract_serde.historical_dataclass_from_json`: every field
    absent from a ``scoring.json`` falls back to the dataclass default, so a
    file written before a field existed loads cleanly, and
    every present field — including the nested ``tournament`` /
    ``overfitting`` blocks — round-trips. Mirror of
    :func:`zicato.workspace_loader.historical_scoring_weights_from_dict`.
    """
    from zicato.core.scoring_config import _reject_retired_scoring_keys  # noqa: PLC0415
    from zicato.epoch.contract_serde import historical_dataclass_from_json  # noqa: PLC0415

    # Reject retired keys symmetrically with the live loader, so a stale
    # snapshot fails loudly through either path rather than silently scoring
    # under a default nobody chose.
    _reject_retired_scoring_keys(d)
    return historical_dataclass_from_json(ScoringWeights, dict(d))


def _config_to_dict(cfg: EpochConfig) -> dict[str, Any]:
    return {
        # Record-format version: stamped at write, checked at read. An absent
        # stamp reads as version 1, so an epoch written before it loads.
        "format_version": RECORD_FORMAT_VERSION,
        "id": cfg.id,
        "name": cfg.name,
        "created_at": cfg.created_at,
        "board_path": str(cfg.board_path),
        "brief_path": str(cfg.brief_path),
        "scoring": scoring_to_dict(cfg.scoring),
        "closed": cfg.closed,
        "closed_at": cfg.closed_at,
        # ``None`` ⇒ an epoch written before contract hashing, stored as
        # null. A newly created epoch always carries a computed hash.
        "contract_hash": cfg.contract_hash,
        # System revisions whose behavior contributes to the contract hash.
        # Stored explicitly so an archived workspace remains auditable without
        # reversing an opaque hash.
        "implementation_identity": dict(cfg.implementation_identity),
        "goal": cfg.goal,
        # ``None`` ⇒ built-in default proposer. Written as null so an
        # epoch that never configured a proposer round-trips cleanly.
        "proposer_path": str(cfg.proposer_path) if cfg.proposer_path is not None else None,
        # Measured A/A noise floor (runtime measurement, never hashed).
        # ``None`` ⇒ never measured; written as null so it round-trips.
        "noise_floor": cfg.noise_floor,
        # Contract pre-flight verdict (runtime measurement, never hashed).
        # ``None`` ⇒ never run; written as null so it round-trips.
        "preflight": cfg.preflight,
        # Applied proposer-reflection recommendation ids (proposer lineage,
        # never hashed). Empty list ⇒ the proposer was not changed by an
        # applied recommendation.
        "applied_proposer_recommendations": list(cfg.applied_proposer_recommendations),
    }


def _config_from_dict(d: dict[str, Any]) -> EpochConfig:
    # ``contract_hash`` defaults to ``None`` so epochs written before
    # contract-hash auto-epoching landed load cleanly — see
    # :class:`zicato.core.types.EpochConfig` and the contract module. A
    # on-disk ``""`` is normalised to ``None``, which downstream reads as
    # "this epoch carries no hash, so it never rolls".
    #
    # ``brief_path`` is the current key; ``rubric_path`` is the
    # pre-rename name, still accepted so an epoch ``config.json`` written
    # before the field rename keeps loading.
    #
    # ``goal`` defaults to "" so epochs written before the field landed
    # load as "no goal recorded".
    #
    # ``proposer_path`` defaults to ``None`` (the built-in default
    # proposer) so an epoch ``config.json`` written before the field
    # landed loads cleanly.
    raw_proposer = d.get("proposer_path")
    raw_floor = d.get("noise_floor")
    raw_preflight = d.get("preflight")
    raw_identity = d.get("implementation_identity")
    return EpochConfig(
        id=d["id"],
        name=d["name"],
        created_at=d["created_at"],
        board_path=Path(d["board_path"]),
        brief_path=Path(d.get("brief_path") or d["rubric_path"]),
        scoring=_scoring_from_dict(d.get("scoring", {})),
        closed=bool(d.get("closed", False)),
        closed_at=d.get("closed_at", ""),
        contract_hash=(str(raw_hash) if (raw_hash := d.get("contract_hash")) else None),
        implementation_identity=(
            {
                key: value
                for key, value in raw_identity.items()
                if isinstance(key, str) and (isinstance(value, str) or type(value) is int)
            }
            if isinstance(raw_identity, dict)
            else {}
        ),
        goal=str(d.get("goal", "")),
        proposer_path=Path(raw_proposer) if raw_proposer else None,
        # ``noise_floor`` defaults to ``None`` (never measured) so epochs
        # written before the calibration surface landed load cleanly.
        noise_floor=raw_floor if isinstance(raw_floor, dict) else None,
        # ``preflight`` defaults to ``None`` (never run) so epochs written
        # before the pre-flight surface landed load cleanly.
        preflight=raw_preflight if isinstance(raw_preflight, dict) else None,
        # ``applied_proposer_recommendations`` defaults to ``()`` so epochs
        # written before proposer reflection existed load as "no applied
        # recommendation", which is what they are.
        applied_proposer_recommendations=tuple(
            str(x) for x in (d.get("applied_proposer_recommendations") or [])
        ),
    )


def _write_config(workspace_root: Path, cfg: EpochConfig) -> None:
    """Atomically write one epoch's ``config.json`` through the storage seam."""
    from zicato.workspace.projection import mark_epoch_changed  # noqa: PLC0415

    mark_epoch_changed(workspace_root, cfg.id)
    backend = workspace_backend(workspace_root, start=False)
    backend.write_json(epoch_config_key(cfg.id), _config_to_dict(cfg))


# ---------------------------------------------------------------------------
# Current-epoch marker
# ---------------------------------------------------------------------------


def current_epoch_id(workspace_root: Path) -> str | None:
    """Read the workspace's ``current_epoch`` marker file.

    Returns ``None`` when there is no marker (fresh workspace, or the
    marker was removed by hand). Returns the stripped contents otherwise.
    """
    text = workspace_backend(workspace_root, start=False).read_text(current_epoch_key())
    if text is None:
        return None
    return text.strip() or None


def switch_epoch(workspace_root: Path, epoch_id: str) -> None:
    """Point the ``current_epoch`` marker at ``epoch_id``.

    The target epoch directory MUST exist; we refuse to dangle the
    marker. Use :func:`new_epoch` to create an epoch and switch in one
    step — that path is the common one.
    """
    if not epoch_dir(workspace_root, epoch_id).exists():
        raise FileNotFoundError(f"epoch {epoch_id!r} does not exist under {workspace_root}")
    workspace_backend(workspace_root, start=False).write_text(current_epoch_key(), epoch_id + "\n")


# ---------------------------------------------------------------------------
# Listing / loading
# ---------------------------------------------------------------------------


def load_epoch(workspace_root: Path, epoch_id: str) -> EpochConfig:
    """Read one epoch's ``config.json`` back into an :class:`EpochConfig`."""
    raw = workspace_backend(workspace_root, start=False).read_json(epoch_config_key(epoch_id))
    if raw is None:
        raise FileNotFoundError(f"epoch {epoch_id!r} has no config.json under {workspace_root}")
    # Record-format guard: absent ⇒ version 1, so an epoch written before the
    # stamp keeps loading; a future incompatible version refuses with a clear
    # error.
    check_record_format(raw, f"epochs/{epoch_id}/config.json")
    return _config_from_dict(raw)


def list_epochs(workspace_root: Path) -> list[EpochConfig]:
    """Enumerate every epoch known to the workspace, in canonical order.

    Directories under ``epochs/`` without a readable ``config.json`` are
    skipped silently — they are presumed to be in-progress writes from a
    crashed ``epoch new`` and the operator can clean them up by hand.

    Epoch *ids* are discovered and ordered by the single enumeration
    authority (:func:`zicato.workspace.list_epoch_ids`) rather than a local
    directory walk + re-sort, so the order here is the canonical
    timestamp-first one (recorded ``created_at`` with the numeric-aware id as
    tiebreaker) — identical to every other epoch enumeration. Each id's
    ``config.json`` is then read back through the storage seam; an id whose
    config is unreadable / malformed is dropped, preserving the prior
    skip-the-in-progress-write behavior.
    """
    layout = WorkspaceLayout.from_root(workspace_root)
    backend = workspace_backend(workspace_root, start=False)
    out: list[EpochConfig] = []
    for epoch_id in list_epoch_ids(layout):
        try:
            raw = backend.read_json(epoch_config_key(epoch_id))
        except (OSError, json.JSONDecodeError):
            continue
        if raw is None:
            continue
        # Record-format guard: a future incompatible config.json is a LOUD
        # refusal rather than a silent skip. Unlike a torn in-progress write,
        # the record is intact, and the operator must know why it will not
        # load.
        check_record_format(raw, f"epochs/{epoch_id}/config.json")
        try:
            out.append(_config_from_dict(raw))
        except (KeyError, TypeError):
            continue
    return out


# ---------------------------------------------------------------------------
# new_epoch / close_epoch
# ---------------------------------------------------------------------------


def _materialize_board(board_source: Board | Path | str, target: Path) -> None:
    """Write the frozen ``board.jsonl`` for an epoch from any board input.

    ``board_source`` may be:

    * a :class:`zicato.board.builder.Board` — its entries are serialized
      to ``target`` via :func:`zicato.board.jsonl.save_board`, so no
      caller-side ``.save()`` is needed;
    * a :class:`pathlib.Path` (or ``str``) — the file is copied verbatim.

    Passing an in-memory ``Board`` is the preferred path; the ``Path``
    form is kept so callers holding an on-disk board still work.
    """
    if isinstance(board_source, str | Path):
        shutil.copyfile(Path(board_source), target)
        return
    # In-memory Board: persist it ourselves. Import lazily so the epoch
    # package does not hard-depend on the board builder at import time.
    from zicato.board.jsonl import save_board  # noqa: PLC0415

    save_board(list(board_source.entries), target)


def _materialize_brief(brief_source: ProposerBrief | Path | str, target: Path) -> None:
    """Write the frozen ``brief.md`` for an epoch from any brief input.

    ``brief_source`` may be:

    * a :class:`zicato.proposer.brief.ProposerBrief` — its ``text`` is
      written to ``target`` verbatim;
    * a ``str`` of proposer-brief markdown — written to ``target`` as-is;
    * a :class:`pathlib.Path` — the file is copied verbatim.

    Plain ``str`` is treated as brief *text*, never as a path; callers
    with an on-disk brief pass a ``Path``. This keeps the in-memory path
    free of any "does this string look like a filename" guessing.
    """
    if isinstance(brief_source, Path):
        shutil.copyfile(brief_source, target)
        return
    if isinstance(brief_source, str):
        target.write_text(brief_source, encoding="utf-8")
        return
    # ProposerBrief instance — persist its source text.
    target.write_text(brief_source.text, encoding="utf-8")


def _prepare_epoch(
    workspace_root: Path,
    name: str,
    board_source: Board | Path | str,
    brief_source: ProposerBrief | Path | str,
    weights: ScoringWeights,
    auto_close_previous: bool = True,
    aux_call_llm: _AuxCallLLM | None = None,
    *,
    contract: ContractInputs | None = None,
    entrypoint: str = "",
    mutable_trees: tuple[str, ...] = (),
    goal: str = "",
    proposer_path: Path | None = None,
    writer: WorkspaceLock,
    baseline_sources: tuple[Path, ...] | None = None,
    baseline_coordinates: tuple[str, str] | None = None,
    before_contract_roll: Callable[[str], None] | None = None,
    contract_adoption: str | None = None,
) -> EpochPublication:
    """Validate and retain a complete epoch before any canonical publication."""
    from zicato.runtime.lock import validate_workspace_lock  # noqa: PLC0415

    validate_workspace_lock(writer, workspace_root)
    if EpochPublication.read(workspace_root) is not None:
        raise RuntimeError("finish the pending epoch publication before preparing another epoch")
    # Rejected before any directory is created, so a caller that mixes the
    # two spellings gets an error rather than a half-written epoch.
    if contract is not None and (entrypoint or mutable_trees or proposer_path is not None):
        raise ValueError(
            "new_epoch: pass either `contract` (the resolved live contract) or the "
            "entrypoint/mutable_trees/proposer_path shorthand, not both — with both, "
            "one spelling would silently lose"
        )

    # Validate optional integration documents before closing an epoch or
    # creating files. Contract canonicalization repeats this check after
    # materialization; running it here keeps failure transactional.
    if weights.goldfive is not None:
        from zicato.integrations.goldfive import normalize_config  # noqa: PLC0415

        normalize_config(weights.goldfive)

    workspace_root.mkdir(parents=True, exist_ok=True)

    epoch_id = _make_epoch_id(workspace_root, name)
    prev_id = current_epoch_id(workspace_root)
    previous = load_epoch(workspace_root, prev_id) if prev_id is not None else None
    closed_at = _now_iso() if auto_close_previous and previous and not previous.closed else None
    final_directory = epoch_dir(workspace_root, epoch_id)
    parent = Path(tempfile.mkdtemp(prefix=".epoch-publication-", dir=workspace_root))
    seed: BaselineSeed | None = None
    try:
        epoch_content = parent / "epoch"
        epoch_content.mkdir()
        target_board = epoch_content / "board.jsonl"
        target_brief = epoch_content / "brief.md"
        _materialize_board(board_source, target_board)
        _materialize_brief(brief_source, target_brief)

        target_scoring = epoch_content / "scoring.json"
        atomic_write_json(target_scoring, scoring_to_dict(weights))

        # Hash the frozen board, brief, and scoring plus every
        # registered component the caller carried in. Computed from the
        # just-written frozen copies so the stored hash is exactly what a
        # later ``resolve_contract_inputs`` over equivalent live files
        # produces.
        from zicato.epoch.contract import (  # noqa: PLC0415
            ContractInputs,
            compute_component_hashes,
            compute_contract_hash,
            evaluation_implementation_identity,
        )

        if contract is None and not entrypoint and not mutable_trees and proposer_path is None:
            from zicato.epoch.contract import resolve_contract_inputs
            from zicato.workspace.config_io import read_workspace_config

            # Default construction captures the registered execution declarations.
            # Explicit legacy arguments retain their direct-construction identity.
            contract = resolve_contract_inputs(
                workspace_root, workspace_config=read_workspace_config(workspace_root).raw
            )
        if contract is None:
            contract = ContractInputs(
                board_path=target_board,
                brief_path=target_brief,
                scoring_path=target_scoring,
                entrypoint=entrypoint,
                mutable_trees=tuple(mutable_trees),
                proposer_path=proposer_path,
            )
        else:
            from dataclasses import replace

            contract = replace(
                contract,
                board_path=target_board,
                brief_path=target_brief,
                scoring_path=target_scoring,
            )
        from zicato.epoch.execution import capture_execution_bindings

        execution_bytes, proposer_spec = capture_execution_bindings(contract)
        (epoch_content / "execution.json").write_bytes(execution_bytes)
        contract_hash = compute_contract_hash(contract, proposer_spec=proposer_spec)
        component_path = epoch_content / "contract_components.json"
        component_path.write_text(
            json.dumps(
                compute_component_hashes(contract, proposer_spec=proposer_spec),
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

        # Recommendation ids remain pending until the epoch is durably published.
        cfg = EpochConfig(
            id=epoch_id,
            name=name,
            created_at=_now_iso(),
            board_path=final_directory / "board.jsonl",
            brief_path=final_directory / "brief.md",
            scoring=weights,
            closed=False,
            closed_at="",
            contract_hash=contract_hash,
            implementation_identity=evaluation_implementation_identity(weights),
            goal=goal,
            # Read off the hashed contract rather than the shorthand parameter, so the
            # proposer this epoch's rounds are built with is the one its hash
            # was taken over.
            proposer_path=contract.proposer_path,
            applied_proposer_recommendations=staged_recommendations(workspace_root),
        )
        atomic_write_json(epoch_content / "config.json", _config_to_dict(cfg))
        if baseline_sources is not None:
            from zicato.epoch.baseline import prepare_baseline_seed
            from zicato.epoch.genstore import default_generation_store

            seed = prepare_baseline_seed(
                workspace_root,
                epoch_id,
                baseline_sources,
                backend=default_generation_store(workspace_root).backend_name,
                created_at=cfg.created_at,
                source_coordinates=baseline_coordinates,
            )
            atomic_write_json(epoch_content / "baseline_seed.json", seed.body())
            if baseline_coordinates is not None:
                source_snapshot = default_generation_store(workspace_root).snapshot_path(
                    *baseline_coordinates
                )
                (epoch_content / "v0_seed_from").write_text(
                    str(source_snapshot) + "\n", encoding="utf-8"
                )
        if contract_adoption is not None:
            from zicato.contract_draft.publication import validate_prepared_contract

            validate_prepared_contract(
                workspace_root,
                contract_adoption,
                writer=writer,
                frozen_directory=epoch_content,
                frozen_contract_hash=contract_hash,
            )
            (epoch_content / "contract_adoption.json").write_text(
                contract_adoption, encoding="utf-8"
            )
        sync_directory_tree(parent)
        operation = EpochPublication(
            epoch_id=epoch_id,
            prepared_directory=epoch_content.relative_to(workspace_root).as_posix(),
            contract_hash=contract_hash,
            content_identity=seed_content_identity(epoch_content),
            predecessor_id=prev_id,
            predecessor_closed_at=closed_at,
            recommendation_ids=cfg.applied_proposer_recommendations,
        )
        if before_contract_roll is not None and prev_id is not None:
            before_contract_roll(prev_id)
        operation.write(workspace_root)
        return operation
    except BaseException:
        if not epoch_publication_path(workspace_root).exists():
            shutil.rmtree(parent)
            if seed is not None:
                source = prepared_directory(workspace_root, seed.prepared_directory)
                shutil.rmtree(source.parent)
        raise


def recover_epoch_publication(workspace_root: Path, *, writer: WorkspaceLock) -> EpochConfig | None:
    """Finish the prepared epoch without consulting mutable live inputs."""
    from zicato.epoch import lineage
    from zicato.epoch.baseline import validate_baseline_seed
    from zicato.runtime.lock import validate_workspace_lock  # noqa: PLC0415

    validate_workspace_lock(writer, workspace_root)
    operation = EpochPublication.read(workspace_root)
    if operation is None:
        return None
    prepared = prepared_directory(workspace_root, operation.prepared_directory)
    destination = epoch_dir(workspace_root, operation.epoch_id)
    if prepared.exists() and destination.exists():
        raise FileExistsError(f"epoch publication destination is occupied: {destination}")
    content = destination if destination.exists() else prepared
    if seed_content_identity(content) != operation.content_identity:
        raise ValueError(f"prepared epoch content changed: {operation.epoch_id}")
    seed = BaselineSeed.read(
        workspace_root, operation.epoch_id, path=content / "baseline_seed.json"
    )
    if seed is not None:
        validate_baseline_seed(workspace_root, seed)
    adoption = content / "contract_adoption.json"
    if adoption.exists():
        from zicato.contract_draft.publication import publish_prepared_contract

        publish_prepared_contract(
            workspace_root, adoption.read_text(encoding="utf-8"), writer=writer
        )
    if not destination.exists():
        from zicato.workspace.projection import mark_epoch_changed  # noqa: PLC0415

        mark_epoch_changed(workspace_root, operation.epoch_id)
        publish_directory(prepared, destination)
    cfg = load_epoch(workspace_root, operation.epoch_id)
    if cfg.id != operation.epoch_id or cfg.contract_hash != operation.contract_hash:
        raise ValueError(f"published epoch contract differs from intent: {cfg.id}")
    parent = operation.predecessor_id
    if seed is not None and seed.source_epoch is not None:
        parent = f"{seed.source_epoch}:{seed.source_generation}"
    lineage.register_epoch(workspace_root, cfg, parent_epoch_id=parent)
    switch_epoch(workspace_root, cfg.id)
    if operation.predecessor_id is not None and operation.predecessor_closed_at is not None:
        _close_epoch_prelude(
            workspace_root,
            operation.predecessor_id,
            closed_at=operation.predecessor_closed_at,
        )
    acknowledge_staged_recommendations(workspace_root, operation.recommendation_ids)
    durable_unlink(epoch_publication_path(workspace_root))
    with suppress(OSError):
        prepared.parent.rmdir()
    return cfg


def new_epoch(
    workspace_root: Path,
    name: str,
    board_source: Board | Path | str,
    brief_source: ProposerBrief | Path | str,
    weights: ScoringWeights,
    auto_close_previous: bool = True,
    aux_call_llm: _AuxCallLLM | None = None,
    *,
    contract: ContractInputs | None = None,
    entrypoint: str = "",
    mutable_trees: tuple[str, ...] = (),
    goal: str = "",
    proposer_path: Path | None = None,
    writer: WorkspaceLock | None = None,
    contract_adoption: str | None = None,
) -> EpochConfig:
    """Prepare a complete evaluation contract, publish it, and switch epochs.

    Invalid inputs leave the predecessor unchanged. Interrupted publication
    resumes from retained content under the workspace writer. Contract paths
    name final locations and preserve the existing canonical hash semantics.
    A supplied writer is validated and borrowed for the entire operation.
    Optional contract_adoption contains accepted publication bytes prepared by
    the contract owner; admission verifies their identity against the epoch.
    """
    from zicato.runtime.lock import acquire_workspace_lock, validate_workspace_lock

    _slugify(name)
    if writer is None:
        with acquire_workspace_lock(workspace_root, "epoch-publication") as owned_writer:
            return new_epoch(
                workspace_root,
                name,
                board_source,
                brief_source,
                weights,
                auto_close_previous,
                aux_call_llm,
                contract=contract,
                entrypoint=entrypoint,
                mutable_trees=mutable_trees,
                goal=goal,
                proposer_path=proposer_path,
                writer=owned_writer,
                contract_adoption=contract_adoption,
            )
    validate_workspace_lock(writer, workspace_root)
    recovered = recover_epoch_publication(workspace_root, writer=writer)
    if recovered is not None and recovered.name == name:
        return recovered
    registered_sources = contract.mutable_trees if contract is not None else mutable_trees
    baseline_sources = tuple(
        path if path.is_absolute() else workspace_root.parent / path
        for path in map(Path, registered_sources)
    )
    operation = _prepare_epoch(
        workspace_root,
        name,
        board_source,
        brief_source,
        weights,
        auto_close_previous,
        aux_call_llm,
        contract=contract,
        entrypoint=entrypoint,
        mutable_trees=mutable_trees,
        goal=goal,
        proposer_path=proposer_path,
        writer=writer,
        baseline_sources=baseline_sources or None,
        contract_adoption=contract_adoption,
    )
    cfg = recover_epoch_publication(workspace_root, writer=writer)
    assert cfg is not None
    if operation.predecessor_closed_at is not None:
        print(
            f"WARNING: auto-closing previous epoch {operation.predecessor_id!r}",
            file=sys.stderr,
        )
        warnings.warn(f"auto-closing previous epoch {operation.predecessor_id!r}", stacklevel=2)
        close_epoch(workspace_root, operation.predecessor_id, aux_call_llm=aux_call_llm)
    return cfg


def _close_epoch_prelude(
    workspace_root: Path,
    epoch_id: str | None,
    *,
    closed_at: str | None = None,
) -> tuple[str, Path]:
    """Mark an epoch closed + stamp lineage; return ``(epoch_id, out_path)``.

    Shared by the sync :func:`close_epoch` and the async
    :func:`close_epoch_async` so the only thing that differs between
    the two is *how* the (possibly async) analysis pass is driven.
    """
    if epoch_id is None:
        epoch_id = current_epoch_id(workspace_root)
        if epoch_id is None:
            raise RuntimeError("close_epoch: no epoch_id supplied and no current_epoch marker")

    cfg = load_epoch(workspace_root, epoch_id)
    if not cfg.closed:
        from dataclasses import replace

        cfg = replace(cfg, closed=True, closed_at=closed_at or _now_iso())
        _write_config(workspace_root, cfg)

    # Update lineage's per-epoch closed_at.
    from zicato.epoch import lineage as _lineage

    _lineage.mark_closed(workspace_root, epoch_id, cfg.closed_at)

    # Re-stamp any persisted living-draft analysis.md now that ``closed`` is
    # on disk: mid-epoch the masthead carries a "LIVING DRAFT" stamp, and the
    # no-LLM close path (``zicato epoch close``) leaves the existing document
    # in place, so without this the stamp would persist forever. The re-stamp
    # is data-derived (it reads the now-closed config), preserves the LLM
    # prose verbatim, and is a no-op when no living draft exists. Strictly
    # best-effort — a re-stamp failure must never fail the close.
    from zicato.util.best_effort import best_effort

    with best_effort("post-close report re-stamp"):
        from zicato.analyzer import restamp_persisted_report  # noqa: PLC0415

        restamp_persisted_report(workspace_root, epoch_id)

    # OPT-IN snapshot GC on close (the workspace ``storage_gc`` config
    # block; absent = off, the default). Best-effort by construction —
    # the hook logs-and-swallows internally, so closing an epoch can
    # never fail because a source-tree prune hiccuped.
    from zicato.epoch.gc import maybe_prune_on_epoch_close

    maybe_prune_on_epoch_close(workspace_root, epoch_id)
    return epoch_id, analysis_path(workspace_root, epoch_id)


def _write_stub_analysis(workspace_root: Path, epoch_id: str, out_path: Path) -> None:
    """Write a stub ``analysis.md`` + HTML companion (no-LLM close path)."""
    if not out_path.exists():
        jpath = journal_path(workspace_root, epoch_id)
        journal_content = jpath.read_text() if jpath.exists() else "(no journal entries)"
        out_path.write_text(
            f"# Epoch analysis: {epoch_id}\n\n"
            "_No evaluation LLM was supplied at close; this is a stub. "
            "Re-run `zicato epoch close` with an `aux_call_llm` configured "
            "to regenerate._\n\n"
            "## Journal snapshot\n\n"
            f"{journal_content}\n"
        )
    from zicato.epoch.analysis import write_html_companion

    write_html_companion(workspace_root, epoch_id, out_path)


def close_epoch(
    workspace_root: Path,
    epoch_id: str | None = None,
    aux_call_llm: _AuxCallLLM | None = None,
    aux_config: AuxConfig | None = None,
) -> Path:
    """Mark an epoch closed and generate ``analysis.md`` for it.

    If ``epoch_id`` is ``None`` we close the current epoch. If
    ``aux_call_llm`` is ``None`` we still mark the epoch closed and
    write a stub ``analysis.md`` (the operator can re-run the analysis
    pass later by hand). The return value is the analysis path so the
    caller can render it / chmod it / etc.

    This is the **synchronous** entry point — it drives the (async)
    analysis pass via :func:`asyncio.run`, so it must NOT be called
    from inside a running event loop. Async callers use
    :func:`close_epoch_async`.
    """
    epoch_id, out_path = _close_epoch_prelude(workspace_root, epoch_id)

    # Generate analysis.md. If no aux callable was provided we still
    # leave a placeholder so callers see a non-empty file — the analysis
    # pass is rerunnable. Either path also writes the sibling
    # ``analysis.html`` so the HTML report stays available when
    # operators close an epoch without an evaluation LLM (e.g. the smoke
    # test).
    if aux_call_llm is not None:
        from zicato.epoch import analysis as _analysis

        asyncio.run(
            _analysis.generate_analysis(
                workspace_root,
                epoch_id,
                aux_call_llm,
                model="",
                aux_config=aux_config,
            )
        )
    else:
        _write_stub_analysis(workspace_root, epoch_id, out_path)
    return out_path


async def close_epoch_async(
    workspace_root: Path,
    epoch_id: str | None = None,
    aux_call_llm: _AuxCallLLM | None = None,
    aux_config: AuxConfig | None = None,
) -> Path:
    """Async sibling of :func:`close_epoch`.

    Identical behaviour, but ``await``\\ s the analysis pass instead of
    driving it through :func:`asyncio.run`. This is the path the
    orchestrator's contract-hash auto-roll uses — it already runs
    inside an event loop, so a nested :func:`asyncio.run` would raise.
    """
    epoch_id, out_path = _close_epoch_prelude(workspace_root, epoch_id)

    if aux_call_llm is not None:
        from zicato.epoch import analysis as _analysis

        await _analysis.generate_analysis(
            workspace_root,
            epoch_id,
            aux_call_llm,
            model="",
            aux_config=aux_config,
        )
    else:
        _write_stub_analysis(workspace_root, epoch_id, out_path)
    return out_path


def set_epoch_goal(workspace_root: Path, epoch_id: str, goal: str) -> EpochConfig:
    """Set (or overwrite) the ``goal`` field on an existing epoch's config.

    Loads the epoch's ``config.json``, replaces the ``goal`` value with
    the supplied string, and writes the config back. Returns the
    updated :class:`EpochConfig`. Idempotent — calling it twice with
    the same goal is a no-op rewrite of the same bytes.

    Designed for the post-hoc CLI: when an epoch was opened via the
    contract-hash auto-roll (mid-``evolve``, no opportunity to prompt
    the operator), the goal is empty and the operator can fill it in
    later with ``zicato epoch set-goal --epoch <id> --goal "..."``.

    Raises :class:`FileNotFoundError` if the epoch does not exist.
    """
    from dataclasses import replace

    cfg = load_epoch(workspace_root, epoch_id)
    cfg = replace(cfg, goal=goal)
    _write_config(workspace_root, cfg)
    return cfg


def set_epoch_noise_floor(
    workspace_root: Path, epoch_id: str, noise_floor: dict[str, Any]
) -> EpochConfig:
    """Persist the measured A/A noise floor onto an existing epoch's config.

    Mirrors :func:`set_epoch_goal`: loads the epoch's ``config.json``,
    replaces the additive ``noise_floor`` field with the supplied
    :meth:`zicato.tournament.calibration.NoiseFloor.to_json` dict, and
    writes the config back. The floor is a RUNTIME measurement, never a
    contract input — writing it does not touch ``contract_hash`` and never
    rolls the epoch. Re-measuring overwrites the prior record.

    Raises :class:`FileNotFoundError` if the epoch does not exist.
    """
    from dataclasses import replace

    cfg = load_epoch(workspace_root, epoch_id)
    cfg = replace(cfg, noise_floor=dict(noise_floor))
    _write_config(workspace_root, cfg)
    return cfg


def set_epoch_preflight(
    workspace_root: Path, epoch_id: str, preflight: dict[str, Any]
) -> EpochConfig:
    """Persist a contract pre-flight verdict onto an existing epoch's config.

    Mirrors :func:`set_epoch_noise_floor`: loads the epoch's
    ``config.json``, replaces the additive ``preflight`` field with the
    supplied :meth:`zicato.epoch.preflight.PreflightReport.to_json` dict,
    and writes the config back. The verdict is a RUNTIME measurement,
    never a contract input — writing it does not touch ``contract_hash``
    and never rolls the epoch. Re-running overwrites the prior record.

    Raises :class:`FileNotFoundError` if the epoch does not exist.
    """
    from dataclasses import replace

    cfg = load_epoch(workspace_root, epoch_id)
    cfg = replace(cfg, preflight=dict(preflight))
    _write_config(workspace_root, cfg)
    return cfg


__all__ = [
    "new_epoch",
    "close_epoch",
    "close_epoch_async",
    "list_epochs",
    "switch_epoch",
    "current_epoch_id",
    "load_epoch",
    "set_epoch_goal",
    "set_epoch_noise_floor",
    "set_epoch_preflight",
    "scoring_to_dict",
]
