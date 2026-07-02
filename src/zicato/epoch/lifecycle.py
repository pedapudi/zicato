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

The module is deliberately small and procedural — there is no
``Lifecycle`` class. Functions take ``workspace_root`` explicitly so the
CLI and tests construct calls from explicit Paths without holding on to
shared state.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import re
import shutil
import sys
import warnings
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from zicato.core.types import EpochConfig, ScoringWeights
from zicato.core.workspace import (
    analysis_path,
    board_path,
    epoch_dir,
    journal_path,
    scoring_path,
)
from zicato.epoch._storage import (
    backend_for,
    current_epoch_key,
    epoch_config_key,
    scoring_key,
)
from zicato.workspace import WorkspaceLayout, list_epoch_ids

if TYPE_CHECKING:
    from zicato.board.builder import Board
    from zicato.proposer.brief import ProposerBrief

# A callable shape compatible with goldfive's call_llm:
# (system, user, model) -> awaitable[str].
_AuxCallLLM = Callable[[str, str, str], Awaitable[str]]


def _brief_path(workspace_root: Path, epoch_id: str) -> Path:
    """Path to the frozen proposer brief (``brief.md``) for one epoch.

    The proposer brief used to be stored as ``rubric.md`` and reached
    through ``zicato.core.workspace.rubric_path``. The epoch directory
    is owned by this module, so the brief path is defined here directly
    — keeping the rename self-contained to ``zicato.epoch`` rather than
    threading it through the shared workspace-path module.
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


def _scoring_to_dict(weights: ScoringWeights) -> dict[str, Any]:
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
    from zicato.epoch.contract_serde import dataclass_to_jsonable  # noqa: PLC0415

    return dataclass_to_jsonable(weights)


def _scoring_from_dict(d: dict[str, Any]) -> ScoringWeights:
    """Parse a frozen ``scoring.json`` dict back into :class:`ScoringWeights`.

    The inverse of :func:`_scoring_to_dict`, field-enumerating via
    :func:`zicato.epoch.contract_serde.jsonable_to_dataclass`: every field
    absent from a legacy ``scoring.json`` falls back to the dataclass
    default (so files written before a field landed load cleanly), and
    every present field — including the nested ``tournament`` /
    ``overfitting`` blocks — round-trips. Mirror of
    :func:`zicato.workspace_loader._scoring_weights_from_dict`.
    """
    from zicato.epoch.contract_serde import jsonable_to_dataclass  # noqa: PLC0415
    from zicato.workspace_loader import _reject_retired_pass_exponent  # noqa: PLC0415

    # Reject a retired ``pass_exponent`` key (issue #19) symmetrically with the
    # live loader, so a stale / pre-feature snapshot fails loudly through either
    # path rather than silently scoring linearly.
    _reject_retired_pass_exponent(d)
    return jsonable_to_dataclass(ScoringWeights, dict(d))


def _config_to_dict(cfg: EpochConfig) -> dict[str, Any]:
    return {
        "id": cfg.id,
        "name": cfg.name,
        "created_at": cfg.created_at,
        "board_path": str(cfg.board_path),
        "brief_path": str(cfg.brief_path),
        "scoring": _scoring_to_dict(cfg.scoring),
        "closed": cfg.closed,
        "closed_at": cfg.closed_at,
        # ``None`` ⇒ pre-hash (legacy) epoch, written as null. Newly
        # created epochs always carry a real computed hash.
        "contract_hash": cfg.contract_hash,
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
    }


def _config_from_dict(d: dict[str, Any]) -> EpochConfig:
    # ``contract_hash`` defaults to ``None`` so epochs written before
    # contract-hash auto-epoching landed load cleanly — see
    # :class:`zicato.core.types.EpochConfig` and the contract module. A
    # legacy on-disk ``""`` is normalised to ``None`` (absent ⇒ legacy,
    # "never rolls"); only ``is None`` reads as legacy downstream.
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
        goal=str(d.get("goal", "")),
        proposer_path=Path(raw_proposer) if raw_proposer else None,
        # ``noise_floor`` defaults to ``None`` (never measured) so epochs
        # written before the calibration surface landed load cleanly.
        noise_floor=raw_floor if isinstance(raw_floor, dict) else None,
        # ``preflight`` defaults to ``None`` (never run) so epochs written
        # before the pre-flight surface landed load cleanly.
        preflight=raw_preflight if isinstance(raw_preflight, dict) else None,
    )


def _write_config(workspace_root: Path, cfg: EpochConfig) -> None:
    """Atomically write one epoch's ``config.json`` through the storage seam."""
    backend_for(workspace_root).write_json(epoch_config_key(cfg.id), _config_to_dict(cfg))


# ---------------------------------------------------------------------------
# Current-epoch marker
# ---------------------------------------------------------------------------


def current_epoch_id(workspace_root: Path) -> str | None:
    """Read the workspace's ``current_epoch`` marker file.

    Returns ``None`` when there is no marker (fresh workspace, or the
    marker was removed by hand). Returns the stripped contents otherwise.
    """
    text = backend_for(workspace_root).read_text(current_epoch_key())
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
    backend_for(workspace_root).write_text(current_epoch_key(), epoch_id + "\n")


# ---------------------------------------------------------------------------
# Listing / loading
# ---------------------------------------------------------------------------


def load_epoch(workspace_root: Path, epoch_id: str) -> EpochConfig:
    """Read one epoch's ``config.json`` back into an :class:`EpochConfig`."""
    raw = backend_for(workspace_root).read_json(epoch_config_key(epoch_id))
    if raw is None:
        raise FileNotFoundError(f"epoch {epoch_id!r} has no config.json under {workspace_root}")
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
    backend = backend_for(workspace_root)
    out: list[EpochConfig] = []
    for epoch_id in list_epoch_ids(layout):
        try:
            raw = backend.read_json(epoch_config_key(epoch_id))
        except (OSError, json.JSONDecodeError):
            continue
        if raw is None:
            continue
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


def new_epoch(
    workspace_root: Path,
    name: str,
    board_source: Board | Path | str,
    brief_source: ProposerBrief | Path | str,
    weights: ScoringWeights,
    auto_close_previous: bool = True,
    aux_call_llm: _AuxCallLLM | None = None,
    *,
    entrypoint: str = "",
    mutable_trees: tuple[str, ...] = (),
    goal: str = "",
    proposer_path: Path | None = None,
) -> EpochConfig:
    """Create a new epoch directory and switch to it.

    Steps:
      1. If ``auto_close_previous`` and the current epoch is open, close
         it first (warning to stderr). ``aux_call_llm`` is required for
         that close — the analysis pass runs on it.
      2. Compute the epoch id from ``name`` and today's date.
      3. Create ``.zicato/epochs/{id}/`` and write the frozen board +
         proposer brief into it.
      4. Serialize ``weights`` to ``scoring.json``.
      5. Compute the contract hash over the frozen board/brief/scoring
         plus ``entrypoint`` + ``mutable_trees``, and store it on the
         :class:`EpochConfig`. See :mod:`zicato.epoch.contract`.
      6. Write ``config.json`` and update ``lineage.json``.
      7. Update the ``current_epoch`` marker.

    Inputs — in-memory objects or paths
    -----------------------------------
    ``board_source`` accepts a :class:`zicato.board.builder.Board` *or*
    a :class:`~pathlib.Path` to a ``board.jsonl``. ``brief_source``
    accepts a :class:`zicato.proposer.brief.ProposerBrief`, a ``str`` of
    proposer-brief markdown, *or* a :class:`~pathlib.Path` to a
    ``brief.md``. ``weights`` is always an in-memory
    :class:`~zicato.core.types.ScoringWeights`.

    When given in-memory objects ``new_epoch`` owns canonicalization and
    persistence end to end — it writes the frozen ``board.jsonl`` /
    ``brief.md`` / ``scoring.json`` itself. The caller never needs a
    prior ``.save()``; the on-disk files the contract hash is computed
    from are this function's responsibility, not the caller's. Passing
    paths still works and copies the files verbatim.

    ``entrypoint`` and ``mutable_trees`` carry the registered inner-
    harness identity into the contract hash. They default to empty so
    existing callers (and tests) keep working — an epoch created
    without them simply hashes those two components as empty, which is
    stable and back-compatible.

    ``proposer_path`` freezes the epoch's proposer dir
    (``proposers/<name>/``) into the contract hash; ``None`` (the default)
    is the built-in default proposer and canonicalizes to a stable form,
    so existing callers that omit it keep their hashes back-compatible
    except for the one-time roll that adding the proposer component
    introduces.

    ``goal`` is a free-form operator-supplied statement of intent for
    the epoch. It is persisted into ``config.json`` and surfaced in
    the analyzer report header so the *why* of the epoch is machine-
    readable, not just narrative in ``journal.md``. Empty by default
    (rendered as "no goal recorded" downstream); multi-line strings
    are accepted verbatim.

    Returns the constructed :class:`EpochConfig`.
    """
    workspace_root.mkdir(parents=True, exist_ok=True)

    # 1. Auto-close previous if open.
    prev_id = current_epoch_id(workspace_root)
    if auto_close_previous and prev_id is not None:
        try:
            prev_cfg = load_epoch(workspace_root, prev_id)
        except FileNotFoundError:
            prev_cfg = None
        if prev_cfg is not None and not prev_cfg.closed:
            print(
                f"WARNING: previous epoch {prev_id!r} was not closed manually; "
                "auto-closing now. analysis.md may be shorter / lower quality "
                "than a manual close.",
                file=sys.stderr,
            )
            warnings.warn(
                f"auto-closing previous epoch {prev_id!r}",
                stacklevel=2,
            )
            close_epoch(workspace_root, prev_id, aux_call_llm=aux_call_llm)

    # 2. Construct the new id.
    epoch_id = _make_epoch_id(workspace_root, name)

    # 3. Create the directory and write the frozen contracts. Both the
    # board and the proposer brief are materialized here from whatever
    # the caller passed (in-memory object or path) — canonicalization
    # and persistence are owned by new_epoch, not the caller.
    edir = epoch_dir(workspace_root, epoch_id)
    edir.mkdir(parents=True, exist_ok=False)
    target_board = board_path(workspace_root, epoch_id)
    target_brief = _brief_path(workspace_root, epoch_id)
    _materialize_board(board_source, target_board)
    _materialize_brief(brief_source, target_brief)

    # 4. Scoring weights — serialized from the in-memory ScoringWeights,
    # written atomically through the storage seam.
    target_scoring = scoring_path(workspace_root, epoch_id)
    backend_for(workspace_root).write_json(scoring_key(epoch_id), _scoring_to_dict(weights))

    # 5. Contract hash over the frozen board/brief/scoring plus the
    # registered inner-harness identity. Computed from the just-written
    # frozen copies so the stored hash is exactly what a later
    # ``resolve_contract_inputs`` over equivalent live files produces.
    from zicato.epoch.contract import (  # noqa: PLC0415
        ContractInputs,
        compute_contract_hash,
    )

    contract_hash = compute_contract_hash(
        ContractInputs(
            board_path=target_board,
            brief_path=target_brief,
            scoring_path=target_scoring,
            entrypoint=entrypoint,
            mutable_trees=tuple(mutable_trees),
            proposer_path=proposer_path,
        )
    )

    # 6. Config + lineage. ``EpochConfig.brief_path`` carries the path
    # to the frozen proposer brief (the ``brief.md`` file).
    cfg = EpochConfig(
        id=epoch_id,
        name=name,
        created_at=_now_iso(),
        board_path=target_board,
        brief_path=target_brief,
        scoring=weights,
        closed=False,
        closed_at="",
        contract_hash=contract_hash,
        goal=goal,
        proposer_path=proposer_path,
    )
    _write_config(workspace_root, cfg)

    # Lineage update (imported lazily to avoid a circular import at module
    # load time — lineage.py wants to read this module's helpers).
    from zicato.epoch import lineage as _lineage

    _lineage.register_epoch(workspace_root, cfg, parent_epoch_id=prev_id)

    # 7. Marker.
    switch_epoch(workspace_root, epoch_id)
    return cfg


def _close_epoch_prelude(
    workspace_root: Path,
    epoch_id: str | None,
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

        cfg = replace(cfg, closed=True, closed_at=_now_iso())
        _write_config(workspace_root, cfg)

    # Update lineage's per-epoch closed_at.
    from zicato.epoch import lineage as _lineage

    _lineage.mark_closed(workspace_root, epoch_id, cfg.closed_at)
    return epoch_id, analysis_path(workspace_root, epoch_id)


def _write_stub_analysis(workspace_root: Path, epoch_id: str, out_path: Path) -> None:
    """Write a stub ``analysis.md`` + HTML companion (no-LLM close path)."""
    if not out_path.exists():
        jpath = journal_path(workspace_root, epoch_id)
        journal_content = jpath.read_text() if jpath.exists() else "(no journal entries)"
        out_path.write_text(
            f"# Epoch analysis: {epoch_id}\n\n"
            "_No auxiliary LLM was supplied at close; this is a stub. "
            "Re-run `zicato epoch close` with an `aux_call_llm` configured "
            "to regenerate._\n\n"
            "## Journal snapshot\n\n"
            f"{journal_content}\n"
        )
    _write_stub_html_companion(workspace_root, epoch_id, out_path)


def close_epoch(
    workspace_root: Path,
    epoch_id: str | None = None,
    aux_call_llm: _AuxCallLLM | None = None,
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
    # operators close an epoch without an auxiliary LLM (e.g. the smoke
    # test).
    if aux_call_llm is not None:
        from zicato.epoch import analysis as _analysis

        asyncio.run(
            _analysis.generate_analysis(
                workspace_root,
                epoch_id,
                aux_call_llm,
                model="",
            )
        )
    else:
        _write_stub_analysis(workspace_root, epoch_id, out_path)
    return out_path


async def close_epoch_async(
    workspace_root: Path,
    epoch_id: str | None = None,
    aux_call_llm: _AuxCallLLM | None = None,
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


def _write_stub_html_companion(
    workspace_root: Path,
    epoch_id: str,
    md_path: Path,
) -> None:
    """Emit ``analysis.html`` when closing without an auxiliary LLM.

    Mirrors the companion-write that ``generate_analysis`` performs in
    the LLM-driven path; we hydrate the typed generation / experiment
    view from on-disk artifacts and hand them to
    :func:`zicato.epoch.html_report.write_html_report`. Failures are
    swallowed — HTML is a non-critical artifact and we should not
    block ``epoch close`` on rendering glitches.
    """
    try:
        from zicato.epoch.analysis import (  # noqa: PLC0415
            _collect_experiments,
            _hydrate_typed_view,
            _write_html_companion,
        )
    except ImportError:
        return
    try:
        raw_experiments = _collect_experiments(workspace_root, epoch_id)
        typed_gens, typed_exps = _hydrate_typed_view(workspace_root, epoch_id, raw_experiments)
        _write_html_companion(md_path, epoch_id, typed_gens, typed_exps)
    except Exception:  # noqa: BLE001 — HTML is best-effort at close
        return


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
]
