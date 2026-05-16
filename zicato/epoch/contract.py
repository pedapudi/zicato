"""Contract hashing for auto-epoching.

An epoch is the unit of *evaluation contract*. Four things make up that
contract:

1. The board — test inputs + expectations + judges (``board.jsonl``).
2. The proposer brief — operator steering text (``brief.md``).
3. The scoring — weights + gate thresholds (``scoring.json``).
4. The registered inner-harness IDENTITY — the ``--adk`` entrypoint
   string plus the sorted list of ``--mutable-tree`` paths.

A change to any of these means generations on either side of the change
are no longer directly comparable, so the epoch must roll. The inner
harness's *source content* is deliberately NOT part of the contract —
that is exactly what zicato mutates within an epoch.

This module reduces the four contract components to a single
``sha256`` hex digest. The hash is *canonicalized* so spurious edits
(whitespace, board-entry reordering, float-formatting noise) do not
trigger a roll — only semantic changes do.

The hash is computed at epoch-creation time and stored on
:class:`zicato.core.types.EpochConfig`; the orchestrator recomputes it
on every ``evolve`` and rolls the epoch when it drifts. See
``docs/design/EPOCHS-AND-JOURNALING.md`` for the full mechanism.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("zicato.epoch.contract")

#: Separator between the five canonical component forms before hashing.
#: Chosen to be a byte sequence that cannot appear in any canonical
#: component (a NUL plus a marker word).
_SEP = "\x00--zicato-contract-component--\x00"


@dataclass(frozen=True, slots=True)
class ContractInputs:
    """The four contract components, resolved to concrete inputs.

    Fields
    ------
    board_path:
        Filesystem path to the live ``board.jsonl``.
    brief_path:
        Filesystem path to the live proposer brief (``brief.md``).
    scoring_path:
        Filesystem path to the live ``scoring.json``.
    entrypoint:
        The registered ``--adk`` entrypoint string, verbatim.
    mutable_trees:
        The registered ``--mutable-tree`` paths. Stored as a tuple of
        strings; :func:`compute_contract_hash` sorts and absolutises
        them so order and relative/absolute spelling do not matter.
    """

    board_path: Path
    brief_path: Path
    scoring_path: Path
    entrypoint: str
    mutable_trees: tuple[str, ...]


# ---------------------------------------------------------------------------
# Per-component canonicalization
# ---------------------------------------------------------------------------


def _canon_board(board_path: Path) -> str:
    """Canonical form of the board: semantic content only, id-sorted.

    Loads the board through :func:`zicato.board.jsonl.load_board` so the
    canonical form is the validated, parsed shape — not the raw bytes.
    Entries are sorted by id and each is serialized to a sorted-key JSON
    dict. Reordering rows or reformatting the JSONL leaves the hash
    unchanged; editing an entry's input/expectation/weight changes it.

    Beyond the per-entry rows the board also carries two *board-level*
    pieces of contract: the configured ``judges`` and the board-level
    ``disable_drift`` flag. Both are canonicalized here so swapping a
    judge — or toggling drift scoring off — correctly rolls the epoch.
    They are read defensively (see :func:`_canon_board_meta`) so a board
    that predates those fields still hashes deterministically.
    """
    if not board_path.exists():
        log.warning(
            "contract: board file %s is missing; hashing it as empty",
            board_path,
        )
        return ""
    from zicato.board.jsonl import _entry_to_dict, load_board  # noqa: PLC0415

    entries = load_board(board_path)
    canon_entries = [
        json.dumps(_entry_to_dict(entry), sort_keys=True, ensure_ascii=False)
        for entry in sorted(entries, key=lambda e: e.id)
    ]
    meta = _canon_board_meta(board_path)
    # Prepend the board-level metadata line so it participates in the
    # hash; the leading marker keeps it from colliding with an entry row.
    return "\n".join(["\x00board-meta\x00" + meta, *canon_entries])


def _canon_board_meta(board_path: Path) -> str:
    """Canonical form of the board-level ``judges`` + ``disable_drift``.

    The board carries two pieces of contract beyond its entry rows: the
    list of configured judges and a board-level ``disable_drift`` flag
    (both introduced alongside multi-judge scoring). This helper reduces
    them to a stable, sorted-key JSON string.

    The board-level fields are resolved defensively — the loader API for
    them is owned by :mod:`zicato.board` and is reconciled at
    integration time:

    * If :mod:`zicato.board.jsonl` exposes a ``load_board_meta`` callable
      it is used directly.
    * Otherwise the raw JSONL is scanned for a board-level object (a line
      that carries ``judges`` / ``disable_drift`` but no entry ``id``).
    * A board with neither canonicalizes to the empty-meta form, so
      boards written before these fields existed keep a stable hash.
    """
    judges: object = []
    disable_drift = False

    from zicato.board import jsonl as _board_jsonl  # noqa: PLC0415

    loader = getattr(_board_jsonl, "load_board_meta", None)
    if callable(loader):
        try:
            meta = loader(board_path)
        except Exception:  # noqa: BLE001 — defensive: board API may evolve
            meta = None
        if meta is not None:
            judges = _meta_get(meta, "judges", [])
            disable_drift = bool(_meta_get(meta, "disable_drift", False))
    else:
        judges, disable_drift = _scan_raw_board_meta(board_path)

    return json.dumps(
        {"judges": _canon_judges(judges), "disable_drift": bool(disable_drift)},
        sort_keys=True,
        ensure_ascii=False,
    )


def _meta_get(meta: object, key: str, default: object) -> object:
    """Read ``key`` off a board-meta object that may be a dict or struct."""
    if isinstance(meta, Mapping):
        return meta.get(key, default)
    return getattr(meta, key, default)


def _scan_raw_board_meta(board_path: Path) -> tuple[object, bool]:
    """Best-effort scan for a board-level metadata object in raw JSONL.

    A board-level object is a JSON line carrying ``judges`` and/or
    ``disable_drift`` but no entry ``id`` (entry rows always have one).
    Returns ``([], False)`` when no such line exists.
    """
    judges: object = []
    disable_drift = False
    try:
        text = board_path.read_text(encoding="utf-8")
    except OSError:
        return judges, disable_drift
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict) or "id" in payload:
            continue
        if "judges" in payload:
            judges = payload["judges"]
        if "disable_drift" in payload:
            disable_drift = bool(payload["disable_drift"])
    return judges, disable_drift


def _canon_judges(judges: object) -> object:
    """Reduce the board's ``judges`` to a stable, order-independent form.

    Each judge is normalized to a sorted-key dict; the list is then
    sorted by its serialized form so judge declaration order does not
    move the hash. Adding, removing, or editing a judge does.
    """
    if not isinstance(judges, list | tuple):
        return judges
    normalized: list[object] = []
    for judge in judges:
        if isinstance(judge, Mapping):
            normalized.append({str(k): judge[k] for k in sorted(judge, key=str)})
        else:
            normalized.append(judge)
    normalized.sort(key=lambda j: json.dumps(j, sort_keys=True, ensure_ascii=False, default=str))
    return normalized


def _canon_brief(brief_path: Path) -> str:
    """Canonical form of the proposer brief: line-ending + ws normalized.

    Normalizes line endings to ``\\n``, strips trailing whitespace per
    line, and strips leading/trailing blank lines. Whitespace-only edits
    (re-indenting, CRLF churn, trailing-newline changes) do not move the
    hash; editing the actual prose does.
    """
    if not brief_path.exists():
        log.warning(
            "contract: proposer-brief file %s is missing; hashing it as empty",
            brief_path,
        )
        return ""
    text = brief_path.read_text(encoding="utf-8")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    # Strip leading / trailing blank lines.
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def _canon_scoring(scoring_path: Path) -> str:
    """Canonical form of the scoring config: float-rounded, key-sorted.

    Parses ``scoring.json`` into a fully-defaulted
    :class:`zicato.core.types.ScoringWeights` and serializes *that*,
    rather than the raw JSON. This matters for stability: the operator's
    live ``scoring.json`` is commonly a partial document (only the
    fields they care about), while the per-epoch frozen copy is the
    full serialized form. Routing both through ``ScoringWeights`` makes
    the two canonicalize identically, so an epoch's stored hash matches
    the hash re-derived from the live file.

    Every float is rounded to 6 decimal places (so ``0.1`` and
    ``0.10000000001`` collapse) and keys are sorted. Reformatting the
    JSON or float-precision noise does not move the hash; changing a
    weight does.
    """
    if not scoring_path.exists():
        log.warning(
            "contract: scoring file %s is missing; hashing it as empty",
            scoring_path,
        )
        return ""
    from zicato.workspace_loader import _scoring_weights_from_dict  # noqa: PLC0415

    raw = json.loads(scoring_path.read_text(encoding="utf-8"))
    weights = _scoring_weights_from_dict(raw)
    return json.dumps(_round_floats(_scoring_to_canon(weights)), sort_keys=True)


def _scoring_to_canon(weights: object) -> dict[str, object]:
    """Reduce a :class:`ScoringWeights` to a plain JSON-shaped dict.

    Every public field is included so the canonical form is complete
    and independent of which fields the operator spelled out in their
    ``scoring.json``.
    """
    from dataclasses import fields

    out: dict[str, object] = {}
    for f in fields(weights):  # type: ignore[arg-type]
        value = getattr(weights, f.name)
        if hasattr(value, "items"):
            out[f.name] = dict(value)
        elif isinstance(value, tuple):
            out[f.name] = list(value)
        else:
            out[f.name] = value
    return out


def _round_floats(value: object) -> object:
    """Recursively round every float in a JSON-shaped value to 6 dp."""
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, dict):
        return {k: _round_floats(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_round_floats(v) for v in value]
    return value


def _canon_entrypoint(entrypoint: str) -> str:
    """Canonical form of the entrypoint: the string verbatim."""
    return entrypoint


def _canon_mutable_trees(mutable_trees: tuple[str, ...]) -> str:
    """Canonical form of the mutable trees: sorted absolute path strings.

    Each path is resolved to an absolute string and the result is
    sorted, so the registration order and any relative/absolute spelling
    differences do not move the hash. Adding or removing a tree does.
    """
    resolved = sorted(str(Path(p).resolve()) for p in mutable_trees)
    return "\n".join(resolved)


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


def compute_contract_hash(inputs: ContractInputs) -> str:
    """Return the ``sha256`` hex digest of the canonicalized contract.

    Canonicalization (so spurious edits don't roll the epoch):

    * **board** — :func:`zicato.board.jsonl.load_board`, sort entries by
      id, serialize each to a sorted-key JSON dict, join. The board-level
      ``judges`` and ``disable_drift`` are folded in too. Semantic
      content only.
    * **brief** — read text, normalize line endings to ``\\n``, strip
      trailing whitespace per line, strip leading/trailing blank lines.
    * **scoring** — ``json.load``, round every float to 6 decimal
      places, ``json.dumps(sort_keys=True)``.
    * **entrypoint** — the string verbatim.
    * **mutable_trees** — sorted tuple of absolute path strings.

    The five canonical forms are concatenated with a NUL-delimited
    separator and hashed. Missing files are treated as the empty string
    for that component (so a board-less workspace still hashes
    deterministically) — a warning is logged when that happens.
    """
    components = [
        _canon_board(inputs.board_path),
        _canon_brief(inputs.brief_path),
        _canon_scoring(inputs.scoring_path),
        _canon_entrypoint(inputs.entrypoint),
        _canon_mutable_trees(inputs.mutable_trees),
    ]
    joined = _SEP.join(components)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def compute_component_hashes(inputs: ContractInputs) -> dict[str, str]:
    """Return a per-component ``sha256`` hex digest.

    Used by the auto-roll path to report *which* contract component
    changed. The keys are ``"board"``, ``"brief"``, ``"scoring"``,
    ``"entrypoint"``, ``"mutable_trees"``.
    """
    return {
        "board": _sha(_canon_board(inputs.board_path)),
        "brief": _sha(_canon_brief(inputs.brief_path)),
        "scoring": _sha(_canon_scoring(inputs.scoring_path)),
        "entrypoint": _sha(_canon_entrypoint(inputs.entrypoint)),
        "mutable_trees": _sha(_canon_mutable_trees(inputs.mutable_trees)),
    }


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def resolve_contract_inputs(workspace_root: Path) -> ContractInputs:
    """Resolve the contract inputs for a workspace from ``config.json``.

    Reads ``{workspace_root}/config.json``, then resolves:

    * ``contract.board_path`` / ``contract.brief_path`` /
      ``contract.scoring_path`` — the canonical contract source paths
      recorded by ``zicato register``. The proposer-brief path is also
      accepted under its legacy ``contract.rubric_path`` key so
      workspaces registered before the rename keep resolving. When the
      ``contract`` key is absent (a workspace registered before
      auto-epoching landed) the default convention is used:
      ``<workspace_root>/board.jsonl``, ``brief.md``, ``scoring.json``
      relative to the workspace root's parent (the operator's working
      directory).
    * ``adk_entrypoint`` — the registered adapter entrypoint.
    * ``mutable_trees`` — the registered source roots.

    Raises
    ------
    FileNotFoundError
        When ``config.json`` is missing. The message suggests running
        ``zicato register``.
    """
    config_path = workspace_root / "config.json"
    if not config_path.exists():
        raise FileNotFoundError(
            f"no config.json under {workspace_root}; run "
            f"`zicato register` to record the evaluation contract before "
            "evolving"
        )
    config = json.loads(config_path.read_text(encoding="utf-8"))

    contract = config.get("contract") or {}
    board_path = Path(
        contract.get("board_path") or _default_contract_path(workspace_root, "board.jsonl")
    )
    # ``brief_path`` is the current key; ``rubric_path`` is the legacy
    # name kept readable so pre-rename workspaces still resolve.
    brief_path = Path(
        contract.get("brief_path")
        or contract.get("rubric_path")
        or _default_brief_path(workspace_root)
    )
    scoring_path = Path(
        contract.get("scoring_path") or _default_contract_path(workspace_root, "scoring.json")
    )

    entrypoint = str(config.get("adk_entrypoint", ""))
    raw_trees = config.get("mutable_trees") or config.get("source_roots") or []
    mutable_trees = tuple(str(t) for t in raw_trees)

    return ContractInputs(
        board_path=board_path,
        brief_path=brief_path,
        scoring_path=scoring_path,
        entrypoint=entrypoint,
        mutable_trees=mutable_trees,
    )


def default_contract_paths(workspace_root: Path) -> dict[str, Path]:
    """Return the default canonical contract source paths for a workspace.

    The convention is ``<workspace_root_parent>/board.jsonl``,
    ``brief.md``, ``scoring.json`` — the operator's live, editable
    copies sitting alongside the ``.zicato/`` directory. ``zicato
    register`` records these in ``config.json`` so subsequent commands
    do not have to re-derive them.

    The proposer-brief default is returned under both ``brief_path``
    (the current key) and ``rubric_path`` (a legacy alias) so callers
    that have not yet adopted the rename keep resolving.
    """
    brief_default = Path(_default_brief_path(workspace_root))
    return {
        "board_path": Path(_default_contract_path(workspace_root, "board.jsonl")),
        "brief_path": brief_default,
        "rubric_path": brief_default,
        "scoring_path": Path(_default_contract_path(workspace_root, "scoring.json")),
    }


def _default_brief_path(workspace_root: Path) -> str:
    """The conventional location of the operator's live proposer brief.

    Prefers ``brief.md`` next to the ``.zicato/`` directory. When that
    file is absent but a legacy ``rubric.md`` exists in the same place,
    the legacy file wins so workspaces created before the rename keep
    resolving without an operator-side file rename.
    """
    brief = workspace_root.parent / "brief.md"
    if not brief.exists():
        legacy = workspace_root.parent / "rubric.md"
        if legacy.exists():
            return str(legacy.resolve())
    return str(brief.resolve())


def _default_contract_path(workspace_root: Path, filename: str) -> str:
    """The conventional location of a contract file for a workspace.

    The convention is *next to* the ``.zicato/`` directory — i.e. in
    the operator's project root — not inside the workspace, so the
    operator's live copies are not confused with the per-epoch frozen
    copies under ``epochs/{id}/``.
    """
    return str((workspace_root.parent / filename).resolve())


__all__ = [
    "ContractInputs",
    "compute_contract_hash",
    "compute_component_hashes",
    "resolve_contract_inputs",
    "default_contract_paths",
]
