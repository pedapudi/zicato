"""Tournament subprocess/process-boundary transport.

The "L3" robustness layer: every tournament run executes in its OWN OS
process — a ``python -m zicato._tournament_worker`` subprocess. This
module owns everything on the wire between the orchestrator process and
those workers:

* the wire-spec builders that serialise a run's inputs into the
  JSON args file the worker re-parses (:func:`_role_worker_spec`,
  :func:`_adapter_spec`, :func:`_weights_spec`, :func:`_entry_to_dict`,
  :func:`_callable_dotted_path`, plus the board-level context stampers
  :func:`_stamp_disable_drift` / :func:`_stamp_judge_only`);
* the per-run ephemeral snapshot working copy (:func:`_make_ephemeral_snapshot`
  / :func:`_discard_ephemeral_snapshot`) that keeps the canonical
  generation snapshot code-only;
* the worker lifecycle helpers (:func:`_terminate_worker`,
  :func:`_load_worker_result`) and the worst-case aborted-run synthesis
  (:func:`_aborted_loss_profile`);
* the small shared primitives the runner and the schedulers both reach
  for (telemetry/runtime-state lazy imports, run-id derivation, the live
  analytical-index dual-write).

These helpers were extracted verbatim from :mod:`zicato.tournament.runner`,
which still owns ``_run_single`` (the orchestrating call site that the
test suite patches in place) and re-exports this module's public surface
so existing ``from zicato.tournament.runner import ...`` imports keep
working unchanged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC
from pathlib import Path
from typing import Any

from zicato.core import (
    BoardEntry,
    Generation,
    LossProfile,
    ScoringWeights,
)

log = logging.getLogger("zicato.tournament.runner")

#: Location of the SQLite analytical index, relative to the workspace
#: root (the ``.zicato/`` directory). Sibling module ``zicato.index``
#: owns the schema; the runner only knows the path so it can dual-write.
_INDEX_DB_RELPATH = "index.db"


def _index_db_path(workspace_root: Path) -> Path:
    """Return the SQLite analytical index path for a workspace."""
    return workspace_root / _INDEX_DB_RELPATH


def _ingest_run_into_index(
    workspace_root: Path,
    epoch_id: str,
    generation_id: str,
    entry_id: str,
) -> None:
    """Best-effort dual-write of one run into the SQLite analytical index.

    Called after the run's ``loss.json`` has been written so the live
    index stays current as the tournament runs. The index module is a
    sibling that may not be present (it lands in parallel); the import is
    lazy and any failure — a missing module, a schema mismatch, an I/O
    error — is logged at ``debug`` level and swallowed. The on-disk
    ``loss.json`` / ``events.jsonl`` remain canonical and ``zicato
    reindex`` can always rebuild the index from scratch.
    """
    try:
        from zicato.index.ingest import ingest_run  # noqa: PLC0415

        ingest_run(
            workspace_root,
            _index_db_path(workspace_root),
            epoch_id,
            generation_id,
            entry_id,
        )
    except ImportError:
        # The index sibling is not installed in this environment — the
        # loop runs fine without the live index.
        log.debug("zicato.index.ingest unavailable; skipping live index dual-write")
    except Exception as exc:  # noqa: BLE001 — index write is best-effort
        log.debug(
            "live index ingest_run skipped for %s/%s/%s: %s",
            epoch_id,
            generation_id,
            entry_id,
            exc,
        )


def _telemetry_helpers() -> tuple[Any, Any]:
    """Lazily import ``zicato.telemetry.sink`` and ``.reducer``.

    Imported per-call rather than at module load so the tournament
    package keeps loading even before telemetry is wired up; the cost
    of the per-call import is negligible compared to the actual run.
    """
    from zicato.telemetry import reducer as _reducer  # noqa: PLC0415
    from zicato.telemetry import sink as _sink  # noqa: PLC0415

    return _sink, _reducer


def _now_iso_utc() -> str:
    from datetime import datetime  # noqa: PLC0415

    return datetime.now(UTC).isoformat()


def _run_id_for(generation: Generation, entry: BoardEntry) -> str:
    return f"{generation.id}--{entry.id}"


#: ``BoardEntry.context`` key under which the board-level ``disable_drift``
#: suppression set is threaded to the adapter. ``context`` is the only
#: per-entry channel that already survives the full
#: runner -> args-file -> subprocess-worker -> ``validate_board_entry`` ->
#: adapter round-trip (it is a plain string-valued mapping serialised by
#: ``zicato.board.jsonl._entry_to_dict`` and re-parsed by
#: ``validate_board_entry``), and it is exactly what
#: ``zicato.adapters.adk._entry_disable_drift`` reads back. The value is a
#: space-separated list of ``goldfive.DriftKind`` wire strings.
_DISABLE_DRIFT_CONTEXT_KEY = "disable_drift"


def _drift_kind_wire(kind: Any) -> str:
    """Project one ``disable_drift`` element to its lowercase wire string.

    ``goldfive.DriftKind`` is a ``StrEnum`` whose ``.value`` is the wire
    token; a bare string is accepted unchanged. A stray
    ``"DriftKind.TOOL_ERROR"`` repr is normalised to its last dotted
    segment, lowercased — the same projection
    ``zicato.judge_runtime.disable`` applies on the read side, so the two
    ends agree on the token form.
    """
    text = str(getattr(kind, "value", kind)).strip()
    if "." in text:
        text = text.rsplit(".", 1)[-1]
    return text.lower()


def _stamp_disable_drift(
    board: list[BoardEntry],
    disable_drift: tuple[Any, ...],
) -> list[BoardEntry]:
    """Return ``board`` with the board-level ``disable_drift`` set on each entry.

    The board-level ``disable_drift`` (parsed by
    :func:`zicato.board.jsonl.load_board_with_meta` from the ``board_meta``
    header) is not a per-entry field, but the adapter is only ever handed
    a :class:`BoardEntry`. This stamps the suppression set onto every
    entry's :attr:`~zicato.core.BoardEntry.context` mapping under
    :data:`_DISABLE_DRIFT_CONTEXT_KEY` so it threads end-to-end —
    through the subprocess worker's entry (de)serialisation — to
    :func:`zicato.adapters.adk._entry_disable_drift`.

    When ``disable_drift`` is empty the board is returned unchanged, so a
    board with no ``board_meta`` header — and any per-entry
    ``context['disable_drift']`` an author set directly — is untouched.
    When non-empty the board-level setting is authoritative: it
    overwrites any per-entry value. :class:`BoardEntry` is frozen, so
    each affected entry is rebuilt via :func:`dataclasses.replace` rather
    than mutated.
    """
    if not disable_drift:
        return board
    wire = " ".join(_drift_kind_wire(k) for k in disable_drift)
    stamped: list[BoardEntry] = []
    for entry in board:
        context = dict(entry.context)
        context[_DISABLE_DRIFT_CONTEXT_KEY] = wire
        stamped.append(replace(entry, context=context))
    return stamped


#: ``BoardEntry.context`` key under which the board-level ``judge_only``
#: flag is threaded to the adapter. Mirrors
#: :data:`_DISABLE_DRIFT_CONTEXT_KEY` exactly — ``context`` is the one
#: per-entry channel that survives the
#: runner -> args-file -> subprocess-worker -> ``validate_board_entry`` ->
#: adapter round-trip. Kept in sync with
#: ``zicato.adapters.adk._JUDGE_ONLY_CONTEXT_KEY`` — the two ends meet on
#: this single string. The value is the lowercase wire form ``"true"`` /
#: ``"false"`` (``context`` is a string-valued mapping).
_JUDGE_ONLY_CONTEXT_KEY = "judge_only"


def _stamp_judge_only(
    board: list[BoardEntry],
    judge_only: bool,
) -> list[BoardEntry]:
    """Return ``board`` with the board-level ``judge_only`` flag on each entry.

    ``judge_only`` is a board-level setting (``Board.judge_only``) but the
    adapter is only ever handed a :class:`BoardEntry`. This stamps the
    flag onto every entry's :attr:`~zicato.core.BoardEntry.context`
    mapping under :data:`_JUDGE_ONLY_CONTEXT_KEY` so it threads end-to-end
    — through the subprocess worker's entry (de)serialisation — to
    :func:`zicato.adapters.adk._entry_judge_only`.

    When ``judge_only`` is ``False`` the board is returned UNCHANGED,
    mirroring :func:`_stamp_disable_drift`'s "empty → untouched"
    behaviour: a default board (steering on) is byte-identical to today
    and any per-entry ``context['judge_only']`` an author set directly is
    left alone. When ``True`` the board-level setting is authoritative
    and overwrites any per-entry value. :class:`BoardEntry` is frozen, so
    each affected entry is rebuilt via :func:`dataclasses.replace`.
    """
    if not judge_only:
        return board
    stamped: list[BoardEntry] = []
    for entry in board:
        context = dict(entry.context)
        context[_JUDGE_ONLY_CONTEXT_KEY] = "true"
        stamped.append(replace(entry, context=context))
    return stamped


def _runtime_state() -> tuple[Any, Any] | None:
    """Lazy-import runtime state helpers; return None if unavailable.

    Returns a ``(state_module, ActiveRun)`` pair, or ``None`` when the
    runtime-state subsystem is not importable. Typed as ``tuple[Any,
    Any]`` because the first element is a module object — there is no
    useful static type for it — and an explicit annotation keeps the
    call sites out of mypy's ``no-untyped-call`` net.
    """
    try:
        from zicato.runtime import state as state_mod  # noqa: PLC0415
        from zicato.runtime.state import ActiveRun  # noqa: PLC0415

        return state_mod, ActiveRun
    except ImportError:
        return None


# ---------------------------------------------------------------------------
# Subprocess worker spawn — the "L3" robustness layer.
# ---------------------------------------------------------------------------
#
# Every tournament run now executes in its OWN OS process: a
# ``python -m zicato._tournament_worker`` subprocess. The motivation is
# hard-enforcement of the per-run wall-clock budget. A run wedged inside
# the orchestrator process used to be un-killable without killing the
# whole ``evolve``; isolated in a subprocess it can be SIGTERM'd then
# SIGKILL'd by this parent — and, independently, by the supervisor
# watchdog keyed on the worker's own pid in ``active_runs/{run_id}.json``.
#
# A free side benefit: the Python-module-caching problem (two
# generations' source loaded into one interpreter, ``sys.modules``
# handing back the wrong one) disappears — each worker imports exactly
# one generation snapshot and then exits.

#: Margin (seconds) added to the entry's wall-clock budget before the
#: PARENT's ``asyncio.wait_for`` fires. The worker keeps its own
#: cooperative ``asyncio.wait_for`` at exactly the entry budget, so under
#: normal operation the worker aborts itself first and exits cleanly with
#: a budget-exceeded result. The parent's wait_for is the SECOND line of
#: defence — it only fires when the worker's cooperative budget did not
#: (a worker stuck in a C extension, a blocked syscall, a hung import).
#: 30s is comfortably longer than the worker's own teardown + loss-reduce
#: path so a healthy worker is never racing the parent.
_PARENT_BUDGET_GRACE_S: float = 30.0

#: Seconds the parent waits after SIGTERM before escalating to SIGKILL.
#: Matches the supervisor's two-stage escalation grace. Used only by the
#: last-resort reaper (:func:`_terminate_worker`) — the normal kill path
#: now delegates escalation to the supervisor (see below).
_SIGTERM_TO_SIGKILL_GRACE_S: float = 5.0

#: Seconds the parent waits for the SUPERVISOR to escalate-kill a worker
#: after the parent writes the kill-request marker, BEFORE falling back to
#: a last-resort self-kill. The supervisor is the single SIGTERM→grace→
#: SIGKILL escalator: the parent requests a kill and waits for the worker
#: to die. This window must comfortably exceed the supervisor's own
#: SIGTERM→SIGKILL grace + its watchdog tick so a healthy supervisor
#: always wins the kill — the parent's fallback fires only when no
#: supervisor is attached (e.g. an ad-hoc run with no watchdog, or a
#: supervisor that itself died), which is exactly when the parent must
#: still guarantee the worker is reaped. Generous on purpose: a few extra
#: seconds on an already-overrun run is cheap; a leaked worker is not.
_SUPERVISOR_KILL_WAIT_S: float = 20.0

#: Filename prefix for a run's ephemeral snapshot working copy. The copy
#: lives in the system temp dir (``tempfile.mkdtemp``) so it never sits
#: inside the workspace tree — nothing under it can be mistaken for a
#: canonical generation snapshot, and it is removed when the run ends.
_EPHEMERAL_SNAPSHOT_PREFIX = "ztw-snap-"


def _make_ephemeral_snapshot(snapshot_root: Path, run_id: str) -> tuple[Path, Path]:
    """Copy a generation's code snapshot into a fresh per-run working dir.

    The worker is pointed at the returned snapshot path, NOT at
    ``snapshot_root`` itself. The canonical snapshot must stay code-only:
    it is the tree
    :meth:`zicato.epoch.genstore.DirectoryGenerationStore.derive_generation`
    copies forward to seed every subsequent generation, so any pollution
    there accumulates without bound (a real disk-exhaustion failure).

    Two layers protect the canonical snapshot:

    1. **Run output is routed to a per-run scratch directory** — this
       function also creates a sibling scratch directory and returns it.
       A target reads the scratch path from
       :data:`zicato.epoch.snapshot_scope.SCRATCH_DIR_ENV` and writes
       there, *outside* its own source tree. That is the primary fix.
    2. **The ephemeral working copy** — a stray write that ignores the
       scratch directory and lands next to the agent's own code still
       only pollutes this throwaway copy, not the canonical snapshot.

    The copy is a plain :func:`shutil.copytree` filtered by the shared
    snapshot-scope ignore — code snapshots are KB-sized, so a copy per
    run (even when many board units run concurrently, and even with the
    champion and challenger of one entry running at once) is cheap. Both
    the working copy and the scratch directory are created under a single
    :func:`tempfile.mkdtemp` parent in the OS temp dir, deliberately
    OUTSIDE the workspace tree.

    The caller owns cleanup — see :func:`_discard_ephemeral_snapshot`,
    which :func:`_run_single` invokes from its ``finally`` block so the
    whole mkdtemp parent (working copy *and* scratch directory) is
    removed even when the run aborts or crashes.

    Returns ``(working_copy, scratch_dir)``: the path the worker mounts
    as the inner harness's source root, and the per-run scratch directory
    the worker exports to the harness via the scratch-dir env var.
    """
    from zicato.epoch.snapshot_scope import copytree_ignore  # noqa: PLC0415

    parent = Path(tempfile.mkdtemp(prefix=f"{_EPHEMERAL_SNAPSHOT_PREFIX}{run_id}-"))
    # Copy *into* a child of the mkdtemp dir, keeping the snapshot's own
    # basename so any path the agent derives from ``__file__`` looks the
    # same as it would under the canonical snapshot. The ignore filter
    # drops run artifacts so a copy never carries forward stale output.
    working_copy = parent / Path(snapshot_root).name
    shutil.copytree(snapshot_root, working_copy, ignore=copytree_ignore())
    # The per-run scratch directory: a sibling of the working copy under
    # the same mkdtemp parent, so one cleanup removes both.
    scratch_dir = parent / "run-scratch"
    scratch_dir.mkdir()
    return working_copy, scratch_dir


def _discard_ephemeral_snapshot(working_copy: Path | None) -> None:
    """Remove a per-run ephemeral snapshot working copy and its temp parent.

    Best-effort: a cleanup failure must never turn a finished run into a
    crash. Removes the whole :func:`tempfile.mkdtemp` directory (the
    working copy's parent), not just the working copy, so no empty temp
    directory is left behind. ``None`` — the run never got as far as
    making a copy — is a no-op.
    """
    if working_copy is None:
        return
    # The mkdtemp dir is the working copy's parent; remove the whole thing.
    temp_root = working_copy.parent
    try:
        shutil.rmtree(temp_root, ignore_errors=True)
    except OSError as exc:  # noqa: BLE001 — ignore_errors already swallows most
        log.debug("ephemeral snapshot cleanup skipped for %s: %s", temp_root, exc)


def _callable_dotted_path(fn: Any) -> str:
    """Return a re-importable ``module:qualname`` dotted path for ``fn``.

    The worker subprocess re-imports the harness / auxiliary LLM
    callables from these paths. A callable must therefore be a
    module-level (or class-attribute) object; a closure-local callable
    has ``<locals>`` in its ``__qualname__`` and cannot be re-imported —
    we surface that as a clear :class:`ValueError` at spawn time rather
    than letting the worker fail opaquely.
    """
    module = getattr(fn, "__module__", None)
    qualname = getattr(fn, "__qualname__", None) or getattr(fn, "__name__", None)
    if not module or not qualname:
        raise ValueError(
            f"cannot derive an import path for callable {fn!r}: it has no __module__/__qualname__"
        )
    if "<locals>" in qualname:
        raise ValueError(
            f"callable {module}:{qualname} is defined inside a function "
            "(closure-local) and cannot be re-imported by a subprocess "
            "worker; pass a module-level callable instead"
        )
    return f"{module}:{qualname}"


def _role_worker_spec(
    role: str,
    *,
    models: Any,
    fallback_callable: Any,
) -> dict[str, Any]:
    """Build the subprocess-worker spec for one LLM role.

    When the workspace ``models.<role>`` block is configured (a dotted path
    OR a model spec), its secret-free :meth:`RoleSpec.to_worker_spec` dict is
    emitted under ``{"models_role": {...}}`` — the worker re-resolves it with
    :func:`zicato.models_config.resolve_text_call_llm` in its fresh
    interpreter (reading any ``api_key_env`` from the worker's own
    :data:`os.environ`). This lets a model-spec role (whose resolved callable
    is a closure that cannot cross the process boundary) reach the worker.

    Otherwise the legacy form is used: the resolved callable's re-importable
    dotted path under ``{"dotted": "module:qualname"}`` — exactly today's
    behavior for an unconfigured role.
    """
    spec = models.role(role)
    if not spec.is_empty:
        return {"models_role": spec.to_worker_spec()}
    return {"dotted": _callable_dotted_path(fallback_callable)}


#: Process-essential environment variables a scrubbed worker still needs to
#: start a Python interpreter, find tools, resolve a home/temp dir, and keep
#: byte-for-byte-stable text output (locale). Deliberately small: this is the
#: floor a worker needs to run at all, NOT a convenience passthrough. Each is
#: copied from the orchestrator's env ONLY if present (an unset key is simply
#: omitted), so the scrub never invents a value.
_WORKER_ESSENTIAL_ENV_KEYS: tuple[str, ...] = (
    # Tool/interpreter discovery + working dirs.
    "PATH",
    "HOME",
    "TMPDIR",
    "TMP",
    "TEMP",
    # Python import path (the worker imports zicato + any dotted-path role).
    "PYTHONPATH",
    "PYTHONHOME",
    "VIRTUAL_ENV",
    # Deterministic text handling — locale changes can shift formatting and
    # default codecs, which would otherwise perturb run output.
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    # Windows interpreter bootstrap (no-op on POSIX, where the key is unset).
    "SYSTEMROOT",
    "SYSTEMDRIVE",
)


def _api_key_env_names(models: Any) -> list[str]:
    """Collect the ``api_key_env`` NAMEs every configured model role needs.

    A model-spec role resolves its credential by reading
    ``os.environ[api_key_env]`` in the worker (see
    :func:`zicato.models_config.resolve_text_call_llm`). When the worker env
    is scrubbed we must keep exactly those named variables so a configured run
    can still authenticate. Returns the env-var NAMES (never secret values),
    de-duplicated and order-stable. An unconfigured / dotted-path role
    contributes nothing here — it carries no ``api_key_env``.
    """
    from zicato.models_config import MODEL_ROLES  # noqa: PLC0415

    names: list[str] = []
    for role in MODEL_ROLES:
        spec = models.role(role)
        env_name = getattr(spec, "api_key_env", None)
        if env_name and env_name not in names:
            names.append(env_name)
    return names


def _scrubbed_worker_env(
    *,
    models: Any,
    extra_env_keys: tuple[str, ...] = (),
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Compose a MINIMAL explicit env for a worker subprocess.

    Instead of inheriting the orchestrator's full environment (which holds
    every credential in the process env — a mutated worker could read all of
    them), the worker is given only:

    * the process-essential keys in :data:`_WORKER_ESSENTIAL_ENV_KEYS`,
    * the ``api_key_env`` NAMEs every configured model role legitimately needs
      to authenticate (:func:`_api_key_env_names`), and
    * any operator-named ``extra_env_keys`` (an escape hatch for a target that
      reads a bespoke variable; opt-in, named explicitly).

    Each key is copied from ``base_env`` (default :data:`os.environ`) ONLY if
    present — an unset key is omitted, never invented. The returned dict is a
    fresh copy safe to hand to ``create_subprocess_exec(env=...)``.
    """
    source: Mapping[str, str] = os.environ if base_env is None else base_env
    wanted: list[str] = list(_WORKER_ESSENTIAL_ENV_KEYS)
    for name in (*_api_key_env_names(models), *extra_env_keys):
        if name and name not in wanted:
            wanted.append(name)
    return {key: source[key] for key in wanted if key in source}


def _adapter_spec(adapter: Any) -> dict[str, Any]:
    """Serialise a harness adapter into a JSON-friendly spec dict.

    The worker reconstructs the adapter from this dict (see
    :func:`zicato._tournament_worker._build_adapter`). Resolution order:

    1. If the adapter exposes a ``worker_spec()`` method, its return
       value is used verbatim — the adapter knows best how to make
       itself re-constructible in a subprocess. This is the
       extensibility hook for non-ADK adapters.
    2. Otherwise the :class:`~zicato.adapters.adk.ADKHarnessAdapter`
       shape is recognised by its ``name == "adk"`` plus the private
       ``_entrypoint`` attribute and the public ``mutable_trees`` list.

    Raises :class:`ValueError` when neither path applies; ``_run_single``
    turns that into an aborted run rather than crashing the tournament.
    """
    worker_spec = getattr(adapter, "worker_spec", None)
    if callable(worker_spec):
        spec = worker_spec()
        if isinstance(spec, dict):
            return spec
        raise ValueError(
            f"adapter {adapter!r}.worker_spec() returned {type(spec).__name__}, expected a dict"
        )

    name = getattr(adapter, "name", None)
    entrypoint = getattr(adapter, "_entrypoint", None)
    if name != "adk" or not entrypoint:
        raise ValueError(
            f"cannot serialise adapter {adapter!r} for subprocess execution: "
            "only the 'adk' adapter shape (or an adapter exposing a "
            "worker_spec() method) is supported"
        )
    trees = [str(Path(p)) for p in getattr(adapter, "mutable_trees", []) or []]
    return {"kind": "adk", "entrypoint": str(entrypoint), "mutable_trees": trees}


def _weights_spec(weights: ScoringWeights) -> dict[str, Any]:
    """Serialise :class:`ScoringWeights` for the subprocess worker.

    Thin delegator to :meth:`ScoringWeights.to_json` — the SINGLE,
    field-enumerating serde shared by this writer and the worker's reader
    (:func:`zicato._tournament_worker._weights_from_args`, which delegates to
    :meth:`ScoringWeights.from_json`). Replacing the former hand-aligned field
    list with one ``dataclasses.fields()``-driven serde means adding a field
    can no longer silently desync the worker into scoring under defaults — the
    documented ``per_judge_weights`` / ``pass_rate_monotonicity_scope`` /
    ``drift_kind_aggregation`` desync class. Every field (including the
    nested config dataclasses) crosses the boundary automatically.
    """
    return weights.to_json()


def _entry_to_dict(entry: BoardEntry) -> dict[str, Any]:
    """Serialise a :class:`BoardEntry` into the JSON shape ``validate_board_entry`` reads.

    The worker re-parses the entry via
    :func:`zicato.core.validate_board_entry`, so the dict must match that
    parser's expected shape (nested ``expectation`` / ``user_persona`` /
    ``turns`` sub-dicts; lists rather than tuples).
    """
    out: dict[str, Any] = {
        "id": entry.id,
        "kind": entry.kind,
        "wall_clock_budget_seconds": entry.wall_clock_budget_seconds,
        "weight": entry.weight,
        "tags": list(entry.tags),
        "context": dict(entry.context),
    }
    if entry.expectation is not None:
        out["expectation"] = {
            "kind": entry.expectation.kind,
            "spec": entry.expectation.spec,
            "reads": entry.expectation.reads,
        }
    if entry.judges:
        out["judges"] = [
            {
                "name": j.name,
                "mode": j.mode.value if hasattr(j.mode, "value") else j.mode,
                "body": j.body,
                "severity": (j.severity.value if hasattr(j.severity, "value") else j.severity),
            }
            for j in entry.judges
        ]
    if entry.input is not None:
        out["input"] = entry.input
    if entry.turns is not None:
        out["turns"] = [{"user": t.user} for t in entry.turns]
    if entry.user_persona is not None:
        out["user_persona"] = {
            "goal": entry.user_persona.goal,
            "constraints": entry.user_persona.constraints,
            "stop_when": entry.user_persona.stop_when,
        }
    if entry.max_turns is not None:
        out["max_turns"] = entry.max_turns
    if entry.adversarial_agent_spec is not None:
        out["adversarial_agent_spec"] = entry.adversarial_agent_spec
    if entry.required_drift_kinds is not None:
        out["required_drift_kinds"] = list(entry.required_drift_kinds)
    return out


def _resolve_harmonograf_url(workspace_root: Path) -> str:
    """Best-effort harmonograf URL resolution for the worker args file.

    The worker runs in a fresh process and re-resolves the env var on
    its own, but the workspace ``config.json`` value is read here (in the
    orchestrator process) and threaded through the args file so the
    worker does not need the workspace-config loader.

    Resilient to a missing / unreadable workspace ``config.json``:
    ``resolve_harmonograf_url`` is called with whatever config dict we
    could load (or ``None`` on failure), so the
    ``ZICATO_HARMONOGRAF_URL`` env path — which the orchestrator's auto-
    launch wiring (#202) writes into — keeps working even when the
    workspace has no on-disk config yet (smoke tests, fresh init).
    """
    try:
        from zicato.telemetry.sink import resolve_harmonograf_url  # noqa: PLC0415
    except Exception:  # noqa: BLE001 — harmonograf wiring is optional
        return ""
    cfg: dict[str, Any] | None
    try:
        from zicato import workspace_loader  # noqa: PLC0415

        cfg = workspace_loader.load_workspace_config(workspace_root)
    except Exception:  # noqa: BLE001 — config is optional; env still wins
        cfg = None
    try:
        return resolve_harmonograf_url(cfg)
    except Exception:  # noqa: BLE001 — never fail a tournament on URL resolution
        return ""


def _resolve_harmonograf_grpc(workspace_root: Path, url: str) -> str:
    """Best-effort gRPC dial target for the worker args file.

    Threaded through the args file (alongside the web ``url``) so the
    worker dials the native gRPC port rather than the browser-facing
    gRPC-Web port. For an auto-launched harmonograf the orchestrator sets
    ``ZICATO_HARMONOGRAF_GRPC`` to ``host:grpc_port``; the resolver below
    prefers it and falls back to scheme-stripping ``url`` for an external
    instance (single port). Empty ``url`` ⇒ empty target.
    """
    if not url:
        return ""
    try:
        from zicato.telemetry.sink import resolve_harmonograf_grpc_target  # noqa: PLC0415

        return resolve_harmonograf_grpc_target(url)
    except Exception:  # noqa: BLE001 — never fail a tournament on target resolution
        return ""


#: Multiplier on ``task_failure_ratio`` in the synthesised aborted-run
#: loss — mirrors :data:`zicato.telemetry.reducer._TASK_FAILURE_RATIO_MULTIPLIER`
#: ("pure failures matter"). Kept as a local constant so this module does
#: not import the reducer just to compute an empty-counts loss.
_ABORTED_TASK_FAILURE_MULTIPLIER: float = 10.0


def _aborted_loss_profile(
    *,
    run_id: str,
    entry: BoardEntry,
    generation_id: str,
    epoch_id: str,
    weights: ScoringWeights,
    runtime_ms: int,
    match_id: str = "",
    abort_cause: str | None = None,
) -> LossProfile:
    """Synthesise a worst-case aborted :class:`LossProfile` for one run.

    Used when the parent has to kill a wedged worker, or when a worker
    vanished (supervisor SIGKILL) without leaving a result file. The
    profile carries ``wall_clock_budget_exceeded=True``.

    ``abort_cause`` records WHY the run aborted (see
    :attr:`zicato.core.LossProfile.abort_cause`): the genuine
    :data:`zicato.core.BUDGET_ABORT_CAUSE` wall-clock exhaustion, or one of
    the INFRA causes (``parent_kill`` / ``gone_no_result`` /
    ``nonzero_exit:{code}`` / ``prepare_failed`` / ``result_unreadable``).
    The caller passes the cause it observed; ``None`` (the default — kept
    for back-compat with ad-hoc callers) leaves the field unset. The cache
    layer reads this to persist ONLY genuine budget exhaustion and never an
    infra abort, so a transient blip cannot poison a unit's score.

    The ``drift_loss`` scalar is computed inline (empty drift counts, a
    full ``task_failure_ratio`` of 1.0, the heavy fixed budget-exceeded
    term) rather than by calling into the reducer — the runner must be
    able to synthesise a definite-loss profile even when the reducer is
    unavailable, so the tournament can always aggregate a killed run as a
    loss for the entry.
    """
    sev_vals = list(weights.severity_weights.values()) or [1.0]
    drift_loss = (
        weights.runtime_weight * (runtime_ms / 1000.0)
        + _ABORTED_TASK_FAILURE_MULTIPLIER * 1.0
        + 5.0 * max(sev_vals)
    )
    drift_loss = max(0.0, drift_loss)
    return LossProfile(
        run_id=run_id,
        entry_id=entry.id,
        generation_id=generation_id,
        epoch_id=epoch_id,
        drift_counts=(),
        plan_revisions=0,
        task_failure_ratio=1.0,
        runtime_ms=runtime_ms,
        wall_clock_budget_exceeded=True,
        expectation_result=None,
        drift_loss=drift_loss,
        pass_fail=(False if entry.expectation is not None else None),
        match_id=match_id,
        abort_cause=abort_cause,
    )


async def _terminate_worker(proc: Any) -> None:
    """LAST-RESORT escalate SIGTERM -> (grace) -> SIGKILL on a worker process.

    The normal over-budget kill path delegates escalation to the
    supervisor (the single SIGTERM→grace→SIGKILL escalator): the parent
    writes a kill-request marker and waits for the worker to die. This
    function is the parent's *fallback*, used only when the supervisor did
    not reap the worker within :data:`_SUPERVISOR_KILL_WAIT_S` — i.e. no
    supervisor is attached (an ad-hoc run with no watchdog) or the
    supervisor itself is gone. In that case the parent MUST still
    guarantee the worker is reaped, so it runs the same escalation here.
    Because it fires only after the supervisor's whole escalation window
    has elapsed with the worker still alive, it never races a healthy
    supervisor over the same pid.

    After SIGTERM we wait :data:`_SIGTERM_TO_SIGKILL_GRACE_S` for a clean
    exit; if the worker is still alive we SIGKILL it. Either way we
    ``await proc.wait()`` so no zombie is left and the parent observes the
    final exit code.
    """
    if proc.returncode is not None:
        return
    try:
        proc.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=_SIGTERM_TO_SIGKILL_GRACE_S)
        return
    except TimeoutError:
        pass
    try:
        proc.kill()
    except ProcessLookupError:
        pass
    try:
        await proc.wait()
    except ProcessLookupError:
        pass


def _load_worker_result(result_path: Path) -> dict[str, Any] | None:
    """Read the worker's result JSON; return ``None`` if missing or corrupt.

    A missing or unparseable result file is the canonical signal that the
    worker did NOT finish cleanly — it was SIGKILLed (by the parent's own
    escalation or by the supervisor) before it could write the file.
    ``_run_single`` treats that as a normal aborted-run outcome, never a
    crash.
    """
    if not result_path.exists():
        return None
    try:
        with open(result_path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


__all__ = [
    "_ABORTED_TASK_FAILURE_MULTIPLIER",
    "_DISABLE_DRIFT_CONTEXT_KEY",
    "_EPHEMERAL_SNAPSHOT_PREFIX",
    "_INDEX_DB_RELPATH",
    "_JUDGE_ONLY_CONTEXT_KEY",
    "_PARENT_BUDGET_GRACE_S",
    "_SIGTERM_TO_SIGKILL_GRACE_S",
    "_SUPERVISOR_KILL_WAIT_S",
    "_WORKER_ESSENTIAL_ENV_KEYS",
    "_aborted_loss_profile",
    "_adapter_spec",
    "_api_key_env_names",
    "_callable_dotted_path",
    "_discard_ephemeral_snapshot",
    "_drift_kind_wire",
    "_entry_to_dict",
    "_index_db_path",
    "_ingest_run_into_index",
    "_load_worker_result",
    "_make_ephemeral_snapshot",
    "_now_iso_utc",
    "_resolve_harmonograf_grpc",
    "_resolve_harmonograf_url",
    "_role_worker_spec",
    "_run_id_for",
    "_runtime_state",
    "_scrubbed_worker_env",
    "_stamp_disable_drift",
    "_stamp_judge_only",
    "_telemetry_helpers",
    "_terminate_worker",
    "_weights_spec",
]
