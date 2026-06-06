"""Per-run sink wiring on top of goldfive's :class:`JSONLPersistenceSink`.

Zicato does not define its own EventSink primitive — goldfive's
JSONL-backed sink does the right thing (proto-canonical serialisation,
asyncio-safe writes, one line per event). What zicato adds is the
*routing*: every run writes to a stable per-(epoch, generation, entry)
path under the workspace root, and the sink is constructed in ``"write"``
mode so reruns cannot corrupt earlier event boundaries by appending.

The factory is the only place that imports ``goldfive.sinks.persistence``,
and it does so lazily. That keeps :mod:`zicato.telemetry` importable in
environments where goldfive is not (yet) installed — useful for unit
tests over pure-dataclass surface, for ``zicato --help``, and for the
CLI's path-introspection commands.

Path layout is delegated to :mod:`zicato.core.workspace`: there is
exactly one canonical path math definition for the workspace, and it
lives there. This module composes ``events_jsonl_path`` with a parent-
directory ``mkdir`` so the goldfive sink can lazily open the file
without the caller pre-creating the directory tree.

Scope: this module wires harmonograf to the **inner-harness** event
stream (one goldfive.v1.Event stream per entry run) — the board-run
session of the harmonograf taxonomy. Zicato's **meta-loop** — the
orchestrator's own proposer + process-judge calls — is its OWN
harmonograf session, wired separately in
:mod:`zicato.telemetry.meta_loop` (the ``MetaLoopEmitter``) +
:func:`zicato.telemetry.harmonograf_supervisor.build_meta_loop_sink`.
The two sessions and the two dashboard surfaces that link to them are
specified canonically in ``docs/design/HARMONOGRAF.md`` (§2 session
taxonomy, §3 dashboard surfaces). harmonograf is the execution view of
one run / of the meta-loop; the zicato dashboard is the competition
view across runs.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from zicato.config import IntegrationConfig, load_config
from zicato.core.workspace import events_jsonl_path

log = logging.getLogger("zicato.telemetry.sink")

#: Environment variable an operator sets to stream a run's telemetry to
#: a live harmonograf server in addition to the on-disk JSONL. The
#: variable is read by :func:`zicato.config.load_config` into
#: :attr:`zicato.config.IntegrationConfig.harmonograf_url`; this module
#: no longer reads it directly. The name is kept here for the tests and
#: log messages that reference it.
HARMONOGRAF_URL_ENV = "ZICATO_HARMONOGRAF_URL"

#: Environment variable carrying the *gRPC* dial target (a bare
#: ``host:port``) for an AUTO-LAUNCHED harmonograf. The auto-launched
#: server binds two distinct ports — a browser-facing gRPC-Web port (the
#: one in ``ZICATO_HARMONOGRAF_URL``, used for dashboard deep-links) and a
#: native gRPC port the per-run sink must dial. Deriving the gRPC target
#: from the web URL (the old behaviour) would dial the *web* port over
#: native gRPC, fail the handshake, and — because all sink errors are
#: swallowed — silently drop telemetry. The orchestrator's auto-launch
#: wiring sets this to ``host:grpc_port`` so the sink dials the right
#: port. Unset for an EXTERNAL harmonograf, where the web URL *is* the
#: dial target (a single port) and the scheme-stripping fallback applies.
HARMONOGRAF_GRPC_ENV = "ZICATO_HARMONOGRAF_GRPC"


def make_run_sink_path(
    workspace_root: Path,
    epoch_id: str,
    generation_id: str,
    entry_id: str,
) -> Path:
    """Return the per-run ``events.jsonl`` path and ensure its parent exists.

    The path itself is computed by
    :func:`zicato.core.workspace.events_jsonl_path` so the layout stays
    pinned to the workspace contract. We additionally ``mkdir(parents=
    True, exist_ok=True)`` on the run directory so callers that build
    the sink lazily — goldfive's sink opens the file handle on first
    emit — do not need to remember to pre-create the tree.

    Returning the path (not the sink) is deliberate: tests and CLI
    introspection commands need to know where the JSONL will land
    without constructing a sink, and the reducer needs to read the same
    path the sink wrote to. Both call this helper.
    """
    path = events_jsonl_path(workspace_root, epoch_id, generation_id, entry_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def make_run_sink(
    workspace_root: Path,
    epoch_id: str,
    generation_id: str,
    entry_id: str,
) -> Any:
    """Construct a goldfive :class:`JSONLPersistenceSink` for one run.

    The sink is configured in ``mode="write"`` so a rerun overwrites the
    prior events file rather than corrupting it with appended events
    from a fresh attempt. Run boundaries are file-level by design (see
    the telemetry-path note); the post-run reducer assumes one events
    file = one run.

    Goldfive is imported lazily here so this module is import-safe even
    when goldfive is not installed. The return type is annotated as
    :class:`Any` for the same reason — typing it as
    ``JSONLPersistenceSink`` would force a top-level goldfive import
    that the module would never recover from in a no-goldfive
    environment.

    Raises
    ------
    ModuleNotFoundError
        If goldfive is not importable. The original error is preserved
        as the cause so the caller can distinguish "telemetry needs
        goldfive but it's not installed" from any other import failure.
    """
    try:
        from goldfive.sinks.persistence import JSONLPersistenceSink
    except ModuleNotFoundError as exc:  # pragma: no cover — exercised in tests
        raise ModuleNotFoundError(
            "zicato.telemetry.sink.make_run_sink requires the goldfive "
            "package to be installed; install it (or the appropriate "
            "extra) and retry."
        ) from exc

    path = make_run_sink_path(workspace_root, epoch_id, generation_id, entry_id)
    return JSONLPersistenceSink(path=path, mode="write")


def resolve_harmonograf_url(
    workspace_config: dict[str, Any] | None = None,
    *,
    config: IntegrationConfig | None = None,
) -> str:
    """Return the configured harmonograf server URL, or ``""`` when unset.

    Resolution order, first non-empty wins:

    1. The ``ZICATO_HARMONOGRAF_URL`` environment variable — carried by
       :attr:`config.harmonograf_url <zicato.config.IntegrationConfig>`.
    2. The ``harmonograf_url`` key of the workspace ``config.json``
       (passed in as ``workspace_config``).

    The environment variable takes precedence so an operator can point a
    single run at a local harmonograf without editing the workspace
    config. Returns the empty string when neither source supplies a URL.

    Empty-string semantics changed in #202: before, the orchestrator
    treated an empty URL as "JSONL-only telemetry" and shipped no live
    console; now the evolve loop's
    :func:`zicato.orchestrator._resolve_or_launch_harmonograf` auto-
    launches an in-process server in that case and writes the resulting
    URL back into ``ZICATO_HARMONOGRAF_URL`` so subsequent callers of
    this function (the tournament runner, the per-board worker) re-
    resolve to the auto-launched URL via the env-var path above. This
    function itself does NOT trigger a launch — it remains a pure
    resolver.

    Parameters
    ----------
    workspace_config:
        The workspace ``config.json`` as a dict, or ``None``.
    config:
        The :class:`~zicato.config.IntegrationConfig` carrying the
        env-sourced ``harmonograf_url``. When ``None`` it is loaded via
        :func:`zicato.config.load_config` — the single place the
        environment is read.
    """
    integration = config if config is not None else load_config().integration
    env = integration.harmonograf_url.strip()
    if env:
        return env
    if workspace_config:
        cfg = workspace_config.get("harmonograf_url", "")
        if isinstance(cfg, str) and cfg.strip():
            return cfg.strip()
    return ""


def _harmonograf_grpc_target(url: str) -> str:
    """Derive a gRPC dial target (``host:port``) from a harmonograf URL.

    ``ZICATO_HARMONOGRAF_URL`` is documented and consumed elsewhere (the
    heartbeat, the dashboard drill-down link) as a browser-resolvable URL
    — typically ``http://host:port``. The harmonograf client, however,
    hands ``server_addr`` straight to ``grpc.aio.insecure_channel``,
    which expects a bare ``host:port`` target: a leading ``http://`` or
    ``https://`` scheme makes gRPC name resolution fail outright. Strip
    any scheme (and a trailing path/slash) so the same env var works for
    both the human-facing link and the gRPC client.
    """
    target = url.strip()
    for scheme in ("http://", "https://"):
        if target.lower().startswith(scheme):
            target = target[len(scheme) :]
            break
    # gRPC targets are host:port only — drop any path component.
    return target.split("/", 1)[0].rstrip("/")


def resolve_harmonograf_grpc_target(url: str) -> str:
    """Resolve the gRPC dial target for the harmonograf sink to dial.

    The auto-launched harmonograf binds two ports: a browser-facing
    gRPC-Web port (carried by ``ZICATO_HARMONOGRAF_URL`` / ``url`` here,
    used for dashboard deep-links) and a native gRPC port the sink must
    dial. When the orchestrator auto-launches a server it exports the
    native gRPC target as ``ZICATO_HARMONOGRAF_GRPC`` (a bare
    ``host:port``); this resolver prefers that env var so the sink dials
    the gRPC port rather than the web port.

    For an EXTERNAL harmonograf (operator-pinned ``ZICATO_HARMONOGRAF_URL``
    with no separate auto-launch) ``ZICATO_HARMONOGRAF_GRPC`` is unset and
    the web URL *is* the single dial target, so we fall back to scheme-
    stripping it via :func:`_harmonograf_grpc_target` — preserving the
    pre-split behaviour for the external path.
    """
    grpc_env = os.environ.get(HARMONOGRAF_GRPC_ENV, "").strip()
    if grpc_env:
        # Tolerate an accidental scheme on the grpc env (defensive — the
        # orchestrator sets a bare host:port, but normalise anyway).
        return _harmonograf_grpc_target(grpc_env)
    return _harmonograf_grpc_target(url)


def _make_harmonograf_sink(url: str, *, grpc_target: str | None = None) -> Any | None:
    """Build a goldfive-compatible harmonograf sink for ``url``.

    The concrete sink ships in harmonograf's client library
    (``harmonograf_client.HarmonografSink``), not in goldfive itself, so
    the import is deferred and tolerant: if ``harmonograf_client`` is not
    installed (or its API has shifted) we log a warning and return
    ``None`` so the caller can proceed with JSONL-only telemetry. The
    harmonograf sink is an *additive* live-streaming convenience — never
    a hard dependency of a run.

    ``HarmonografSink`` takes a pre-built ``Client``; the client is
    constructed against the gRPC dial target. When ``grpc_target`` is
    supplied (an auto-launched server's native gRPC ``host:port``) it is
    dialed verbatim; otherwise the target is resolved via
    :func:`resolve_harmonograf_grpc_target` (which prefers the
    ``ZICATO_HARMONOGRAF_GRPC`` env, falling back to scheme-stripping the
    web ``url`` for an external harmonograf).
    """
    try:
        from harmonograf_client import Client, HarmonografSink  # noqa: PLC0415
    except ImportError as exc:
        log.warning(
            "harmonograf streaming requested (%s=%s) but harmonograf_client "
            "is not installed — proceeding with JSONL telemetry only (%s)",
            HARMONOGRAF_URL_ENV,
            url,
            exc,
        )
        return None
    try:
        target = grpc_target if grpc_target else resolve_harmonograf_grpc_target(url)
        client = Client(name="zicato", server_addr=target)
        return HarmonografSink(client)
    except Exception as exc:  # noqa: BLE001 — never hard-fail a run on this
        log.warning(
            "could not construct harmonograf sink for %s — proceeding with "
            "JSONL telemetry only (%s)",
            url,
            exc,
        )
        return None


def make_run_sinks(
    workspace_root: Path,
    epoch_id: str,
    generation_id: str,
    entry_id: str,
    *,
    workspace_config: dict[str, Any] | None = None,
) -> list[Any]:
    """Return the LIST of EventSinks to attach for one run.

    Always includes the canonical per-run :class:`JSONLPersistenceSink`
    (when goldfive is installed). When a harmonograf URL is resolvable
    via :func:`resolve_harmonograf_url`, a harmonograf sink is appended
    so operators can watch the run live in the harmonograf console.

    The harmonograf attachment is strictly best-effort: a missing
    ``harmonograf_client`` package, or a failure constructing the sink,
    is logged at ``warning`` level and the run continues with JSONL-only
    telemetry. The JSONL sink is the source of truth the reducer reads;
    harmonograf is an additive live view.

    Returns an empty list only when goldfive itself is not installed —
    matching the runner's pre-existing tolerance for a no-goldfive
    environment (the adapter may wire its own capture).
    """
    try:
        from goldfive.sinks.persistence import JSONLPersistenceSink  # noqa: PLC0415
    except ModuleNotFoundError:
        return []

    path = make_run_sink_path(workspace_root, epoch_id, generation_id, entry_id)
    sinks: list[Any] = [JSONLPersistenceSink(path=path, mode="write")]

    url = resolve_harmonograf_url(workspace_config)
    if url:
        # The grpc target resolution prefers ZICATO_HARMONOGRAF_GRPC (the
        # auto-launched native gRPC port) over deriving from the web URL.
        harmonograf_sink = _make_harmonograf_sink(url)
        if harmonograf_sink is not None:
            sinks.append(harmonograf_sink)
    return sinks


__all__ = [
    "make_run_sink_path",
    "make_run_sink",
    "make_run_sinks",
    "resolve_harmonograf_url",
    "resolve_harmonograf_grpc_target",
    "HARMONOGRAF_URL_ENV",
    "HARMONOGRAF_GRPC_ENV",
]
