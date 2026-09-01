"""Tournament subprocess/process-boundary transport.

The subprocess worker boundary: every tournament run executes in its OWN OS
process — a ``python -m zicato._tournament_worker`` subprocess. This
module owns everything on the wire between the orchestrator process and
those workers:

* the wire-spec builders that serialise a run's inputs into the
  JSON args file the worker re-parses (:func:`_role_worker_spec`,
  :func:`adapter_worker_spec`, :func:`_weights_spec`, :func:`_entry_to_dict`,
  :func:`_callable_dotted_path`, plus the board-level context stampers
  :func:`_stamp_disable_drift` / :func:`_stamp_judge_only`);
* the per-run ephemeral snapshot checkout (:func:`_checkout_run_snapshot`
  / :func:`_discard_run_snapshot`) that keeps the canonical generation
  snapshot code-only — routed through the workspace's
  :class:`~zicato.epoch.genstore.GenerationStore` so each backend
  materialises the isolated per-run tree its own way;
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
    run_id_for_unit,
)
from zicato.epoch.genstore import EPHEMERAL_SNAPSHOT_PREFIX, EphemeralCheckout

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
    """Return the canonical run id for the entry's stamped replicate."""
    return run_id_for_unit(generation.id, entry.id, _entry_replicate_index(entry))


#: ``BoardEntry.context`` key under which the board-level ``disable_drift``
#: suppression set is threaded to the adapter. ``context`` is the only
#: per-entry channel that already survives the full
#: runner -> args-file -> subprocess-worker -> ``validate_board_entry`` ->
#: adapter round-trip (it is a plain string-valued mapping serialised by
#: ``zicato.board.jsonl.entry_to_dict`` and re-parsed by
#: ``validate_board_entry``), and it is exactly what
#: ``zicato.adapters.adk.entry_disable_drift`` reads back. The value is a
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
    :func:`zicato.adapters.adk.entry_disable_drift`.

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
    :func:`zicato.adapters.adk.entry_judge_only`.

    When ``judge_only`` is ``False`` the board is returned UNCHANGED,
    mirroring :func:`_stamp_disable_drift`'s "empty → untouched"
    behaviour: a default board (steering on) is left untouched, and any
    per-entry ``context['judge_only']`` an author set directly is left
    alone. When ``True`` the board-level setting is authoritative
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


#: ``BoardEntry.context`` key carrying the run's REPLICATE INDEX to the
#: harness under test. Run provenance rather than a contract input: a
#: deterministic/seeded harness (e.g. the convergence example's noisy
#: adapter) derives its per-run noise from stable identifiers, and the
#: replicate index is the one identifier that distinguishes the N
#: otherwise-identical paired runs of a replicated matchup. ``context``
#: is the one per-entry channel that survives the
#: runner -> args-file -> subprocess-worker -> ``validate_board_entry`` ->
#: adapter round-trip (see :data:`_DISABLE_DRIFT_CONTEXT_KEY`). The value
#: is the decimal string form (``context`` is string-valued); an ABSENT
#: key means replicate 0, so single-replicate runs are byte-identical to
#: before this key existed.
_REPLICATE_INDEX_CONTEXT_KEY = "replicate_index"

#: ``BoardEntry.context`` key carrying the run's GENERATION ID to the
#: harness under test. Stamped by ``_run_single`` onto the serialised
#: worker entry only (the in-process board objects are untouched), so a
#: session that never sees its canonical snapshot path — the worker
#: mounts an ephemeral copy with a throwaway name — can still identify
#: WHICH generation it is measuring from a stable identifier. Mirrors
#: :data:`_REPLICATE_INDEX_CONTEXT_KEY`; consumers must tolerate absence
#: (an ad-hoc / in-process drive outside the worker).
_GENERATION_ID_CONTEXT_KEY = "generation_id"


def _stamp_replicate_index(
    board: list[BoardEntry],
    replicate_index: int,
) -> list[BoardEntry]:
    """Return ``board`` with the replicate index on each entry's context.

    Stamped once per replicate pass by the replication loop
    (:func:`zicato.tournament.scheduling._run_replicated`) so every run of
    replicate ``r`` carries ``context['replicate_index'] == str(r)``
    through the subprocess boundary to the adapter session.

    ``replicate_index == 0`` returns the board UNCHANGED (object identity
    preserved), mirroring :func:`_stamp_disable_drift`'s "empty →
    untouched" behaviour: every single-replicate path — the gauntlet, the
    seed scoring, replicate 0 of a replicated matchup — is byte-identical
    to before, and readers treat an absent key as replicate 0.
    """
    if replicate_index <= 0:
        return board
    stamped: list[BoardEntry] = []
    for entry in board:
        context = dict(entry.context)
        context[_REPLICATE_INDEX_CONTEXT_KEY] = str(replicate_index)
        stamped.append(replace(entry, context=context))
    return stamped


def _entry_replicate_index(entry: BoardEntry) -> int:
    """Read the replicate index stamped onto an entry's context, or ``0``.

    The read side of :func:`_stamp_replicate_index`: an absent key is
    replicate 0 (every single-replicate path), and a malformed value is
    read as 0 rather than raising inside a scoring run.
    """
    raw = dict(entry.context).get(_REPLICATE_INDEX_CONTEXT_KEY, "0")
    try:
        return max(0, int(raw or 0))
    except (TypeError, ValueError):
        return 0


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
# Subprocess worker spawn — the worker-process boundary.
# ---------------------------------------------------------------------------
#
# Every tournament run executes in its OWN OS process: a
# ``python -m zicato._tournament_worker`` subprocess. That is what makes the
# per-run wall-clock budget hard-enforceable. A run wedged inside the
# orchestrator process would be un-killable without killing the whole
# ``evolve``; isolated in a subprocess it can be SIGTERM'd then SIGKILL'd by
# this parent — and, independently, by the supervisor watchdog keyed on the
# worker's own pid in ``active_runs/{run_id}.json``.
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

# NOTE: the window the parent waits for the SUPERVISOR to escalate-kill a
# worker (before falling back to a last-resort self-kill) is configurable
# per run — see :attr:`zicato.core.RuntimeConfig.supervisor_kill_wait_s`.
# It defaults to 20s, which is the abort-latency floor when no supervisor
# is attached.

#: Filename prefix for a run's ephemeral snapshot checkout. Re-exported
#: from the generation-store seam, which owns the mechanism now — the
#: prefix + temp-dir placement is the shape the Rust supervisor's
#: crash-GC (``crates/supervisor/src/reap.rs``) reaps.
_EPHEMERAL_SNAPSHOT_PREFIX = EPHEMERAL_SNAPSHOT_PREFIX


def _checkout_run_snapshot(
    *,
    workspace_root: Path,
    epoch_id: str,
    generation: Generation,
    run_id: str,
) -> EphemeralCheckout:
    """Materialise the per-run ephemeral snapshot for one tournament run.

    The worker is pointed at the returned checkout's ``working_dir``,
    NOT at the canonical generation source tree. The canonical tree must
    stay code-only: it is what
    :meth:`zicato.epoch.genstore.GenerationStore.derive_generation`
    derives every subsequent generation from, so any pollution there
    accumulates without bound (a real disk-exhaustion failure).

    Two layers protect the canonical tree:

    1. **Run output is routed to a per-run scratch directory** — the
       checkout carries one. A target reads the scratch path from
       :data:`zicato.epoch.snapshot_scope.SCRATCH_DIR_ENV` and writes
       there, *outside* its own source tree. That is the primary fix.
    2. **The ephemeral checkout itself** — a stray write that ignores
       the scratch directory and lands next to the agent's own code
       still only pollutes the throwaway checkout.

    Routing: when the workspace's :class:`~zicato.epoch.genstore
    .GenerationStore` owns this generation (it exists under the
    ``(epoch_id, generation.id)`` coordinate and the recorded
    ``generation.snapshot_root`` IS the store's canonical path), the
    checkout is delegated to
    :meth:`~zicato.epoch.genstore.GenerationStore.checkout_ephemeral` —
    the directory backend copies, the git backend checks out a per-run
    worktree (measurably cheaper). A store-unmanaged generation (an
    ad-hoc caller pointing ``snapshot_root`` at an arbitrary tree) falls
    back to the same ``copytree`` mechanism the directory backend uses
    (:func:`zicato.epoch.genstore.copy_checkout_ephemeral`).

    The caller owns cleanup — see :func:`_discard_run_snapshot`, which
    :func:`_run_single` invokes from its ``finally`` block so the whole
    ``ztw-snap-*`` parent (working dir *and* scratch dir) is removed
    even when the run aborts or crashes. Raises only :class:`OSError` /
    :class:`ValueError` shapes on failure, which ``_run_single`` degrades
    to an aborted (``prepare_failed``) run.
    """
    from zicato.epoch.genstore import (  # noqa: PLC0415
        copy_checkout_ephemeral,
        default_generation_store,
    )

    snapshot_root = Path(generation.snapshot_root)
    # Building the store is the ONE step allowed to fail into the copy
    # fallback: an ad-hoc caller may hand this an arbitrary tree with no
    # initialized workspace behind it, so a workspace whose source backend
    # cannot be resolved simply has no store to route through. Everything
    # after this point is a real generation the store claims to own, and a
    # failure there is a failure of the run.
    try:
        store = default_generation_store(workspace_root)
    except (FileNotFoundError, ValueError):
        return copy_checkout_ephemeral(snapshot_root, run_id)

    try:
        if store.has_generation(epoch_id, generation.id):
            canonical = store.materialize_snapshot(epoch_id, generation.id)
            if Path(canonical).resolve() == snapshot_root.resolve():
                return store.checkout_ephemeral(epoch_id, generation.id, run_id)
    except (OSError, ValueError):
        # Including the FileNotFoundError raised for a canonical tree the
        # store owns but cannot materialise. Copying whatever
        # ``snapshot_root`` still points at would let a run against a
        # half-removed generation report as a clean one; ``_run_single``
        # turns this into an aborted (``prepare_failed``) run instead.
        raise
    except Exception as exc:
        # A backend-specific failure (e.g. a git plumbing error) is
        # normalised to OSError so _run_single's existing
        # degrade-to-aborted-run handling applies unchanged.
        raise OSError(f"ephemeral checkout failed for {epoch_id}/{generation.id}: {exc}") from exc
    return copy_checkout_ephemeral(snapshot_root, run_id)


def _discard_run_snapshot(checkout: EphemeralCheckout | None) -> None:
    """Tear down a per-run ephemeral snapshot checkout.

    Best-effort and idempotent: a cleanup failure must never turn a
    finished run into a crash (the backend cleanups already guarantee
    that; the belt-and-braces guard here covers a patched-in checkout).
    ``None`` — the run never got as far as a checkout — is a no-op.
    """
    if checkout is None:
        return
    try:
        checkout.cleanup()
    except Exception as exc:  # noqa: BLE001 — cleanup is best-effort
        log.debug("ephemeral snapshot cleanup skipped for %s: %s", checkout.working_dir, exc)


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

    Otherwise the dotted form is used: the resolved callable's re-importable
    path under ``{"dotted": "module:qualname"}``, which is what an
    unconfigured role crosses the boundary as.
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


def scrubbed_worker_env(
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


def adapter_worker_spec(adapter: Any) -> dict[str, Any]:
    """Serialise a harness adapter into a JSON-friendly spec dict.

    The worker reconstructs the adapter from this dict (see
    :func:`zicato._tournament_worker.build_adapter`). Resolution order:

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
    :meth:`ScoringWeights.from_json`). One ``dataclasses.fields()``-driven
    serde on both ends is what stops a newly added field from silently
    desyncing the worker into scoring under defaults, the way a hand-aligned
    field list did for ``per_judge_weights`` /
    ``pass_rate_monotonicity_scope`` / ``drift_kind_aggregation``. Every
    field, including the nested config dataclasses, crosses the boundary
    automatically.
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


def _config_pins() -> dict[str, dict[str, Any]]:
    """Snapshot the process-pinned config overrides for the worker args file.

    CLI flags that shadow typed-config knobs (``--harness-call-timeout-ms``,
    ``--aux-call-timeout``, ...) are pinned process-wide via
    :func:`zicato.config.pin_overrides`. Some of those knobs are consumed
    INSIDE the worker subprocess — the adapter reads the harness call
    timeout when it builds the goldfive runtime, the judge/emulator call
    sites read the aux budget — so the pins must cross the process
    boundary. They travel in the args file (this snapshot) and the worker
    re-pins them at startup; no environment variable is involved.

    Best-effort by construction: an empty dict (no flags pinned) is the
    common case and the worker then runs on its own defaults, exactly as
    the orchestrator does.
    """
    from zicato.config import get_pinned_overrides  # noqa: PLC0415

    return get_pinned_overrides()


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


def _aborted_loss_profile(
    *,
    run_id: str,
    entry: BoardEntry,
    generation_id: str,
    epoch_id: str,
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

    The profile states the FACTS of the abort — no drift events observed
    (``drift_loss=0.0``), every started task failed
    (``task_failure_ratio=1.0``), and the run did not complete
    (``not_completed=True``) — and the ``failure:`` channel derives the loss
    from them at aggregation time. Nothing is computed inline here, so this
    synthesiser can never disagree with the reducer's arithmetic, and it
    needs no reducer import to produce a definite-loss profile for a killed
    run.
    """
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
        not_completed=True,
        expectation_result=None,
        drift_loss=0.0,
        pass_fail=(False if entry.expectation is not None else None),
        match_id=match_id,
        abort_cause=abort_cause,
        # Provenance twin of the worker's own stamp: this profile carries the
        # same worst-case charge the reducer applies to a not-completed run,
        # so it must name its cause the same way. Here the cause and the
        # cache signal happen to coincide — both are the parent's observation
        # — but a reader of ``not_completed_reason`` sees an attributed
        # charge on either synthesis path.
        not_completed_reason=abort_cause,
    )


async def _terminate_worker(proc: Any) -> None:
    """LAST-RESORT escalate SIGTERM -> (grace) -> SIGKILL on a worker process.

    The normal over-budget kill path delegates escalation to the
    supervisor (the single SIGTERM→grace→SIGKILL escalator): the parent
    writes a kill-request marker and waits for the worker to die. This
    function is the parent's *fallback*, used only when the supervisor did
    not reap the worker within the config's ``supervisor_kill_wait_s``
    window (:attr:`zicato.core.RuntimeConfig.supervisor_kill_wait_s`) — i.e. no
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
    "_DISABLE_DRIFT_CONTEXT_KEY",
    "_EPHEMERAL_SNAPSHOT_PREFIX",
    "_GENERATION_ID_CONTEXT_KEY",
    "_INDEX_DB_RELPATH",
    "_JUDGE_ONLY_CONTEXT_KEY",
    "_REPLICATE_INDEX_CONTEXT_KEY",
    "_PARENT_BUDGET_GRACE_S",
    "_SIGTERM_TO_SIGKILL_GRACE_S",
    "_WORKER_ESSENTIAL_ENV_KEYS",
    "_aborted_loss_profile",
    "adapter_worker_spec",
    "_api_key_env_names",
    "_callable_dotted_path",
    "_checkout_run_snapshot",
    "_config_pins",
    "_discard_run_snapshot",
    "_drift_kind_wire",
    "_entry_replicate_index",
    "_entry_to_dict",
    "_index_db_path",
    "_ingest_run_into_index",
    "_load_worker_result",
    "_now_iso_utc",
    "_resolve_harmonograf_grpc",
    "_resolve_harmonograf_url",
    "_role_worker_spec",
    "_run_id_for",
    "_runtime_state",
    "scrubbed_worker_env",
    "_stamp_disable_drift",
    "_stamp_judge_only",
    "_stamp_replicate_index",
    "_telemetry_helpers",
    "_terminate_worker",
    "_weights_spec",
]
