"""Contract hashing for auto-epoching.

An epoch is the unit of *evaluation contract*. Four things make up that
contract:

1. The board — test inputs + expectations (``board.jsonl``).
2. The rubric — operator steering text (``rubric.md``).
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
    rubric_path:
        Filesystem path to the live ``rubric.md``.
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
    rubric_path: Path
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
    return "\n".join(canon_entries)


def _canon_rubric(rubric_path: Path) -> str:
    """Canonical form of the rubric: line-ending + whitespace normalized.

    Normalizes line endings to ``\\n``, strips trailing whitespace per
    line, and strips leading/trailing blank lines. Whitespace-only edits
    (re-indenting, CRLF churn, trailing-newline changes) do not move the
    hash; editing the actual prose does.
    """
    if not rubric_path.exists():
        log.warning(
            "contract: rubric file %s is missing; hashing it as empty",
            rubric_path,
        )
        return ""
    text = rubric_path.read_text(encoding="utf-8")
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
      id, serialize each to a sorted-key JSON dict, join. Semantic
      content only.
    * **rubric** — read text, normalize line endings to ``\\n``, strip
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
        _canon_rubric(inputs.rubric_path),
        _canon_scoring(inputs.scoring_path),
        _canon_entrypoint(inputs.entrypoint),
        _canon_mutable_trees(inputs.mutable_trees),
    ]
    joined = _SEP.join(components)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def compute_component_hashes(inputs: ContractInputs) -> dict[str, str]:
    """Return a per-component ``sha256`` hex digest.

    Used by the auto-roll path to report *which* contract component
    changed. The keys are ``"board"``, ``"rubric"``, ``"scoring"``,
    ``"entrypoint"``, ``"mutable_trees"``.
    """
    return {
        "board": _sha(_canon_board(inputs.board_path)),
        "rubric": _sha(_canon_rubric(inputs.rubric_path)),
        "scoring": _sha(_canon_scoring(inputs.scoring_path)),
        "entrypoint": _sha(_canon_entrypoint(inputs.entrypoint)),
        "mutable_trees": _sha(_canon_mutable_trees(inputs.mutable_trees)),
    }


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def resolve_contract_inputs(workspace_root: Path) -> ContractInputs:
    """Resolve the contract inputs for a workspace from ``config.json``.

    Reads ``{workspace_root}/config.json``, then resolves:

    * ``contract.board_path`` / ``contract.rubric_path`` /
      ``contract.scoring_path`` — the canonical contract source paths
      recorded by ``zicato register``. When the ``contract`` key is
      absent (a workspace registered before auto-epoching landed) the
      default convention is used: ``<workspace_root>/board.jsonl``,
      ``rubric.md``, ``scoring.json`` relative to the workspace root's
      parent (the operator's working directory).
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
    rubric_path = Path(
        contract.get("rubric_path") or _default_contract_path(workspace_root, "rubric.md")
    )
    scoring_path = Path(
        contract.get("scoring_path") or _default_contract_path(workspace_root, "scoring.json")
    )

    entrypoint = str(config.get("adk_entrypoint", ""))
    raw_trees = config.get("mutable_trees") or config.get("source_roots") or []
    mutable_trees = tuple(str(t) for t in raw_trees)

    return ContractInputs(
        board_path=board_path,
        rubric_path=rubric_path,
        scoring_path=scoring_path,
        entrypoint=entrypoint,
        mutable_trees=mutable_trees,
    )


def default_contract_paths(workspace_root: Path) -> dict[str, Path]:
    """Return the default canonical contract source paths for a workspace.

    The convention is ``<workspace_root_parent>/board.jsonl``,
    ``rubric.md``, ``scoring.json`` — the operator's live, editable
    copies sitting alongside the ``.zicato/`` directory. ``zicato
    register`` records these in ``config.json`` so subsequent commands
    do not have to re-derive them.
    """
    return {
        "board_path": Path(_default_contract_path(workspace_root, "board.jsonl")),
        "rubric_path": Path(_default_contract_path(workspace_root, "rubric.md")),
        "scoring_path": Path(_default_contract_path(workspace_root, "scoring.json")),
    }


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
