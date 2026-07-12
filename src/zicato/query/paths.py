"""paths — extracted from the former dashboard state_reader monolith (pure move)."""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Any

from zicato.storage import read_json
from zicato.workspace import WorkspaceLayout
from zicato.workspace import epochs as _ws_epochs

# The ONE definition of the epoch ordering + enumeration now lives in
# :mod:`zicato.workspace.epochs`; re-export the primitives here so existing
# ``from zicato.query.paths import _natural_key`` / ``_NUM_RUN`` /
# ``_epoch_sort_key`` imports (and the readers' ``__init__`` exports) keep
# resolving to the single source of truth — there is no second definition.
_NUM_RUN = _ws_epochs._NUM_RUN
_natural_key = _ws_epochs.natural_key
_epoch_created_at = _ws_epochs.epoch_created_at
_epoch_sort_key = _ws_epochs.epoch_sort_key


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
    def logs(self) -> Path:
        # The structured operator-log streams (LOGGING.md): one
        # ``<utc-stamp>-<pid>.jsonl`` per evolve/reflect invocation.
        return self.root / "logs"

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
        # The LEGACY snapshot — kept for the compat reader. The live state
        # is the event log below (RUNTIME-V2 Phase 3).
        return self.runtime / "active_tournament.json"

    @property
    def active_tournament_log(self) -> Path:
        # The active-tournament EVENT LOG: the single-writer append-only
        # JSONL the orchestrator/runner publish live state onto, folded by
        # ``read_active_tournament`` into the structure view.
        return self.runtime / "active_tournament.events.jsonl"

    @property
    def progress_log(self) -> Path:
        # The ORCHESTRATOR progress EVENT LOG (RUNTIME-V2 Phase 4): the
        # single-writer append-only JSONL whose monotonic ``seq`` is the
        # true liveness signal (advances only on a genuine transition).
        return self.runtime / "progress.events.jsonl"

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


def layout_of(paths: WorkspacePaths) -> WorkspaceLayout:
    """The :class:`~zicato.workspace.WorkspaceLayout` for a dashboard
    ``WorkspacePaths``.

    The single bridge the readers cross to reach the canonical enumeration +
    path math. ``WorkspacePaths.root`` is the inner ``.zicato`` directory —
    exactly the root the layout expects — so this is a zero-cost wrap.
    """
    return WorkspaceLayout(paths.root)


def list_epoch_ids(paths: WorkspacePaths) -> list[str]:
    """Every epoch id on disk (the ``epochs/`` subdirectories), sorted.

    The set of epochs a ``?epoch=<id>`` request may legally resolve to.
    Returns an empty list when the workspace has no ``epochs/`` directory.
    Delegates to the single ordering authority
    (:func:`zicato.workspace.list_epoch_ids`, timestamp-first).
    """
    return _ws_epochs.list_epoch_ids(layout_of(paths))


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


def coerce_float(value: Any) -> float | None:
    """``float(value)`` for a real number, else ``None``.

    THE one numeric payload coercer (bools excluded — a stray ``True`` is
    not a scalar). Replaces the dozens of inline
    ``float(x) if isinstance(x, int | float) else None`` copies.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def coerce_numeric_dict(value: Any) -> dict[str, float]:
    """A ``{str: float}`` projection of a raw mapping (non-numeric dropped)."""
    if not isinstance(value, dict):
        return {}
    out: dict[str, float] = {}
    for k, v in value.items():
        f = coerce_float(v)
        if f is not None:
            out[str(k)] = f
    return out


def _opt_bool(value: Any) -> bool | None:
    """Coerce a stored pass/fail flag to a JSON boolean (or ``None``).

    ONE spelling on the wire: the SQLite index stores 0/1 ints, loss.json
    stores real booleans — every payload emits ``true`` / ``false`` /
    ``null``, never a bare int the frontend has to re-interpret.
    """
    if value is None:
        return None
    return bool(value)


def _is_finite(value: float) -> bool:
    try:
        return value == value and value not in (float("inf"), float("-inf"))
    except TypeError:
        return False
