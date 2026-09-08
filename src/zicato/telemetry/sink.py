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

Scope: this module wires harmonograf to the **system-under-test** event
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
from pathlib import Path
from typing import Any

from zicato.config import IntegrationConfig, resolve_configuration
from zicato.core.workspace import events_jsonl_path
from zicato.runtime.context import inherited_runtime_context

log = logging.getLogger("zicato.telemetry.sink")


def make_run_sink_path(
    workspace_root: Path,
    epoch_id: str,
    generation_id: str,
    entry_id: str,
    replicate_index: int = 0,
) -> Path:
    """Return one replicate's canonical events path, parent ensured."""
    path = events_jsonl_path(workspace_root, epoch_id, generation_id, entry_id, replicate_index)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def events_prev_path_for(path: Path) -> Path:
    """The retained predecessor of one events file. See :func:`archive_prior_events`.

    Derived from the stem so each replicate keeps its own archive:
    ``events.jsonl`` → ``events.prev.jsonl`` and ``events.r{n}.jsonl`` →
    ``events.r{n}.prev.jsonl``. A fixed filename would make every replicate
    of a unit archive over the same file.
    """
    return path.with_name(f"{path.stem}.prev.jsonl")


def archive_prior_events(path: Path) -> None:
    """Retain the events file a ``mode="write"`` sink is about to truncate.

    A replicate's events file is keyed by ``(epoch, generation, entry,
    replicate)`` with no round dimension, so the next round's sink would
    otherwise truncate the raw telemetry of a re-measured unit — the
    champion under ``--mode full``, which is re-run every round.
    ``loss.json`` can in principle be re-derived from the events; once they
    are gone the measurement is unreconstructable by any means (issue #122).

    This renames an EXISTING events file to its
    :func:`events_prev_path_for` sibling before the sink opens, keeping
    exactly ONE predecessor PER REPLICATE: the archive is
    bounded (a champion defending twenty rounds costs two files, not
    twenty), which is the trade for preserving the immediately-clobbered
    measurement without unbounded growth. A no-op when nothing is there
    (the first run of a unit — the common case).

    Best-effort: a failed rename leaves the truncating sink to do what it
    did before, never an aborted run.
    """
    if not path.is_file():
        return
    try:
        path.replace(events_prev_path_for(path))
    except OSError as exc:  # pragma: no cover — unwritable workspace
        log.debug("events archive skipped for %s: %s", path, exc)


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
    file = one run. The file it is about to truncate is first retained
    as ``events.prev.jsonl`` (:func:`archive_prior_events`) so a
    re-measured unit's prior raw telemetry survives one clobber.

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
    archive_prior_events(path)
    return JSONLPersistenceSink(path=path, mode="write")


def resolve_harmonograf_url(
    workspace_config: dict[str, Any] | None = None,
    *,
    config: IntegrationConfig | None = None,
) -> str:
    """Resolve an explicit URL, then inherited context, then workspace settings."""
    if config is not None and config.harmonograf_url.strip():
        return config.harmonograf_url.strip()
    inherited = inherited_runtime_context()
    if inherited is not None and inherited.telemetry.web_url:
        return inherited.telemetry.web_url
    if workspace_config:
        selected = resolve_configuration(workspace_config).values.integration.harmonograf_url
        return selected.strip()
    return ""


def _harmonograf_grpc_target(url: str) -> str:
    """Remove a browser URL's scheme and path to obtain its host and port."""
    target = url.strip()
    for scheme in ("http://", "https://"):
        if target.lower().startswith(scheme):
            target = target[len(scheme) :]
            break
    # gRPC targets are host:port only — drop any path component.
    return target.split("/", 1)[0].rstrip("/")


def resolve_harmonograf_grpc_target(url: str) -> str:
    """Use a matching inherited native endpoint, otherwise the external URL's port."""
    inherited = inherited_runtime_context()
    if inherited is not None and inherited.telemetry.web_url == url:
        return inherited.telemetry.grpc_target or _harmonograf_grpc_target(url)
    return _harmonograf_grpc_target(url)


def _make_harmonograf_sink(
    url: str,
    *,
    grpc_target: str | None = None,
    metadata: dict[str, str] | None = None,
    identity_root: Path | None = None,
) -> Any | None:
    """Build a goldfive-compatible harmonograf sink for ``url``.

    The concrete sink ships in harmonograf's client library
    (``harmonograf_client.HarmonografSink``) rather than in goldfive itself, so
    the import is deferred and tolerant: if ``harmonograf_client`` is not
    installed (or its API has shifted) we log a warning and return
    ``None`` so the caller can proceed with JSONL-only telemetry. The
    harmonograf sink is an *additive* live-streaming convenience — never
    a hard dependency of a run.

    ``HarmonografSink`` takes a pre-built ``Client``; the client is
    constructed against the gRPC dial target. When ``grpc_target`` is
    supplied (an auto-launched server's native gRPC ``host:port``) it is
    dialed verbatim; otherwise the target is resolved via
    :func:`resolve_harmonograf_grpc_target`, which preserves the native
    address of a matching inherited runtime context and otherwise derives
    the target from an external single-port URL.

    ``identity_root`` selects the persistent client registry. Tests supply
    a temporary directory; ``None`` uses the client's platform default.
    Per-unit ``metadata`` travels on registration envelopes only.
    """
    try:
        from harmonograf_client import Client, HarmonografSink  # noqa: PLC0415
    except ImportError as exc:
        log.warning(
            "harmonograf streaming requested for %s but harmonograf_client "
            "is not installed — proceeding with JSONL telemetry only (%s)",
            url,
            exc,
        )
        return None
    try:
        target = grpc_target if grpc_target else resolve_harmonograf_grpc_target(url)
        client_kwargs: dict[str, Any] = {"name": "zicato", "server_addr": target}
        if identity_root is not None:
            client_kwargs["identity_root"] = str(identity_root)
        if metadata:
            from harmonograf_client.identity import load_or_create  # noqa: PLC0415

            # Unit labels belong to the registration envelope. Passing an
            # existing identity prevents Client from persisting those labels.
            identity = load_or_create("zicato", root=identity_root)
            client_kwargs["agent_id"] = identity.agent_id
            client_kwargs["metadata"] = metadata
        client = Client(**client_kwargs)
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
    identity_root: Path | None = None,
) -> list[Any]:
    """Return the LIST of EventSinks to attach for one run.

    Always includes the canonical per-run :class:`JSONLPersistenceSink`
    (when goldfive is installed). When a harmonograf URL is resolvable
    via :func:`resolve_harmonograf_url`, a harmonograf sink is appended
    so operators can watch the run live in the harmonograf console.
    ``identity_root`` selects the client registry independently of the workspace.

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
    archive_prior_events(path)
    sinks: list[Any] = [JSONLPersistenceSink(path=path, mode="write")]

    url = resolve_harmonograf_url(workspace_config)
    if url:
        # A matching inherited context retains the service's native port.
        harmonograf_sink = _make_harmonograf_sink(url, identity_root=identity_root)
        if harmonograf_sink is not None:
            sinks.append(harmonograf_sink)
    return sinks


__all__ = [
    "archive_prior_events",
    "events_prev_path_for",
    "make_run_sink_path",
    "make_run_sink",
    "make_run_sinks",
    "resolve_harmonograf_url",
    "resolve_harmonograf_grpc_target",
]
