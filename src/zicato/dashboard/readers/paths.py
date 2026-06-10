"""paths — extracted from zicato.dashboard.state_reader (pure move)."""

from __future__ import annotations

import datetime as _dt
import re
from pathlib import Path
from typing import Any

from zicato.runtime._atomic import read_json


def _utc_now() -> _dt.datetime:
    return _dt.datetime.now(_dt.UTC)


def _iso(dt: _dt.datetime) -> str:
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Workspace path layout
# ---------------------------------------------------------------------------


class WorkspacePaths:
    """The ``.zicato/`` layout the dashboard reads.

    ``root`` is the ``.zicato`` directory itself, matching the convention
    every other zicato helper uses (``runtime/`` and ``epochs/`` hang
    directly off it).
    """

    def __init__(self, root: Path, *, harmonograf_url: str = "") -> None:
        self.root = Path(root)
        # The persistent per-workspace harmonograf web URL the dashboard
        # PROCESS resolved at startup (``ensure_workspace_harmonograf``).
        # Injected into the heartbeat payload so the standalone /
        # post-mortem dashboard can deep-link into persisted sessions even
        # though no live evolve is writing ``harmonograf_url``. Empty when
        # the dashboard could not resolve a server (failure isolation) —
        # the readers then inject nothing. See ``read_heartbeat_dict``.
        self.harmonograf_url = harmonograf_url

    @property
    def runtime(self) -> Path:
        return self.root / "runtime"

    @property
    def epochs(self) -> Path:
        return self.root / "epochs"

    @property
    def heartbeat(self) -> Path:
        return self.runtime / "heartbeat.json"

    @property
    def lock(self) -> Path:
        return self.runtime / "lock.json"

    @property
    def active_runs_dir(self) -> Path:
        return self.runtime / "active_runs"

    @property
    def active_tournament(self) -> Path:
        return self.runtime / "active_tournament.json"

    @property
    def control_dir(self) -> Path:
        return self.runtime / "control"

    @property
    def current_epoch_marker(self) -> Path:
        return self.root / "current_epoch"

    @property
    def lineage(self) -> Path:
        return self.root / "lineage.json"

    @property
    def index_db(self) -> Path:
        return self.root / "index.db"

    def epoch_health_dir(self, epoch_id: str) -> Path:
        return self.epochs / epoch_id / "health"


# ---------------------------------------------------------------------------
# Small primitives
# ---------------------------------------------------------------------------


def _read_json_value(path: Path) -> Any | None:
    """Best-effort JSON read; missing / empty / malformed -> ``None``."""
    try:
        return read_json(path)
    except Exception:
        return None


def to_snake(name: str) -> str:
    """Convert a ``camelCase`` / ``PascalCase`` identifier to ``snake_case``.

    Idempotent on input already in snake_case. Mirrors the Rust
    ``run_log::to_snake`` so event kinds key on one stable vocabulary
    (the zicato#1 normalization).
    """
    out: list[str] = []
    prev_lower_or_digit = False
    for ch in name:
        if ch.isascii() and ch.isupper():
            if prev_lower_or_digit:
                out.append("_")
            out.append(ch.lower())
            prev_lower_or_digit = False
        else:
            out.append(ch)
            prev_lower_or_digit = ch.isascii() and (ch.islower() or ch.isdigit())
    return "".join(out)


def _preview(text: str) -> str:
    text = text.strip()
    if len(text) <= _PREVIEW_CHARS:
        return text
    return text[:_PREVIEW_CHARS] + "..."


# Char ceiling on truncated text previews (board input, mutation body),
# matching the Rust ``epoch::PREVIEW_CHARS``.
_PREVIEW_CHARS = 120


def read_current_epoch(paths: WorkspacePaths) -> str | None:
    """Return the current epoch id from the ``current_epoch`` marker."""
    try:
        text = paths.current_epoch_marker.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


_NUM_RUN = re.compile(r"(\d+)")


def _natural_key(name: str) -> tuple[tuple[int, Any], ...]:
    """Numeric-aware sort key so ``v2`` sorts before ``v10`` (and ``e2``
    before ``e10``), instead of the lexical order that puts ``v10`` first.

    Splits the string into alternating text / digit runs and compares digit
    runs numerically. This yields chronological order for the sequentially
    minted ``eN`` epoch ids and ``vN`` generation ids, and preserves the
    already-chronological lexical order of ISO-date-prefixed epoch ids
    (``2026-04-01_slug``). The leading ``0``/``1`` tag keeps text and number
    runs from ever being compared across types.
    """
    return tuple(
        (1, int(part)) if part.isdigit() else (0, part) for part in _NUM_RUN.split(name) if part
    )


def _epoch_created_at(epoch_dir: Path) -> str:
    """The epoch's recorded creation timestamp from its ``config.json`` (an
    ISO-8601 string whose lexical order is chronological), or ``""`` when
    absent so ordering falls back to the numeric id key.
    """
    cfg = _read_json_value(epoch_dir / "config.json")
    if isinstance(cfg, dict):
        ts = cfg.get("created_at")
        if isinstance(ts, str) and ts:
            return ts
    return ""


def _epoch_sort_key(epoch_dir: Path) -> tuple[str, tuple[tuple[int, Any], ...]]:
    """Order epochs by recorded creation time, with the numeric-aware id as a
    deterministic tiebreaker (and the fallback when the timestamp is missing).
    Sorting by the actual timestamp — not the id — keeps date-named or
    mixed-scheme epochs in true chronological order.
    """
    return (_epoch_created_at(epoch_dir), _natural_key(epoch_dir.name))


def list_epoch_ids(paths: WorkspacePaths) -> list[str]:
    """Every epoch id on disk (the ``epochs/`` subdirectories), sorted.

    The set of epochs a ``?epoch=<id>`` request may legally resolve to.
    Returns an empty list when the workspace has no ``epochs/`` directory.
    """
    if not paths.epochs.is_dir():
        return []
    epoch_dirs = [d for d in paths.epochs.iterdir() if d.is_dir()]
    epoch_dirs.sort(key=_epoch_sort_key)
    return [d.name for d in epoch_dirs]


def _resolve_epoch_id(paths: WorkspacePaths, epoch_id: str | None) -> str | None:
    """Validate + resolve the epoch a scoped build should read.

    ``None`` resolves to the current epoch (the unchanged default — every
    existing caller). A given id is validated against the on-disk epoch set
    and rejected (``ValueError``) when unknown or path-unsafe, so a
    ``?epoch=../foo`` cannot escape the workspace. The validated id is
    returned verbatim.
    """
    if epoch_id is None:
        return read_current_epoch(paths)
    # reject path-traversal / separators outright — an epoch id is a single
    # directory name, never a path.
    if (
        not isinstance(epoch_id, str)
        or not epoch_id
        or "/" in epoch_id
        or "\\" in epoch_id
        or epoch_id in (".", "..")
        or "\x00" in epoch_id
    ):
        raise ValueError(f"invalid epoch id: {epoch_id!r}")
    if epoch_id not in list_epoch_ids(paths):
        raise ValueError(f"unknown epoch id: {epoch_id!r}")
    return epoch_id


def _parse_iso(value: Any) -> _dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    raw = value.replace("Z", "+00:00")
    try:
        dt = _dt.datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.UTC)
    return dt


def _is_finite(value: float) -> bool:
    try:
        return value == value and value not in (float("inf"), float("-inf"))
    except TypeError:
        return False
