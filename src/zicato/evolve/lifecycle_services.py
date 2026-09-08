"""Invocation-owned service handles, telemetry endpoints, and heartbeat updates.

Service resolution returns explicit browser and native addresses. Only the
handle that launched a service shuts it down; no coordinator environment
variables are changed while acquiring or releasing a service.
"""

from __future__ import annotations

import datetime as _dt
import logging
from pathlib import Path
from typing import Any

from zicato.core.settings import IntegrationConfig
from zicato.runtime.heartbeat import HeartbeatBeater
from zicato.runtime.lock import WorkspaceLock
from zicato.util import best_effort

log = logging.getLogger("zicato.orchestrator")


def _now_iso() -> str:
    return _dt.datetime.now(_dt.UTC).replace(microsecond=0).isoformat()


def _resolve_harmonograf_url(workspace_root: Path, config: IntegrationConfig | None = None) -> str:
    """Resolve authored or inherited telemetry without starting a service."""
    from zicato.runtime.context import inherited_runtime_context  # noqa: PLC0415

    if config is not None:
        from zicato.telemetry.sink import resolve_harmonograf_url  # noqa: PLC0415

        return resolve_harmonograf_url(config=config)
    inherited = inherited_runtime_context()
    if inherited is not None and inherited.telemetry.web_url:
        return inherited.telemetry.web_url
    try:
        from zicato import workspace_loader  # noqa: PLC0415
        from zicato.telemetry.sink import resolve_harmonograf_url  # noqa: PLC0415

        try:
            cfg = workspace_loader.load_workspace_config(workspace_root)
        except Exception:  # noqa: BLE001 — config is optional here
            cfg = None
        return resolve_harmonograf_url(cfg, config=config)
    except Exception as exc:  # noqa: BLE001 — never block a run on this
        log.debug("harmonograf url resolution skipped: %s", exc)
        return ""


def _resolve_or_launch_harmonograf(
    workspace_root: Path,
    config: IntegrationConfig | None = None,
) -> tuple[str, Any]:
    """Return an explicit or inherited service, otherwise ensure the workspace service."""
    configured = _resolve_harmonograf_url(workspace_root, config)
    if configured:
        # Opt-out: external harmonograf in use. No auto-launch, no
        # env-var manipulation, no shutdown needed.
        log.debug("harmonograf auto-launch skipped: external URL configured (%s)", configured)
        from zicato.telemetry.sink import (  # noqa: PLC0415
            _harmonograf_grpc_target,
            resolve_harmonograf_grpc_target,
        )

        target = (
            _harmonograf_grpc_target(configured)
            if config is not None and config.harmonograf_url
            else resolve_harmonograf_grpc_target(configured)
        )
        return configured, _NoopShutdownHandle(target)

    # Route through the per-workspace ensure-helper so an evolve and a
    # concurrently-open standalone dashboard share ONE harmonograf server
    # bound to the workspace's sqlite db (the ``server.json`` record is the
    # single-server-per-workspace contract — see
    # ``harmonograf_supervisor.ensure_workspace_harmonograf``). When the
    # helper REUSES an existing server (a standalone dashboard already
    # launched one), evolve does NOT own its lifecycle — it leaves it
    # running; when evolve LAUNCHED it, the handle's shutdown stops it.
    try:
        from zicato.telemetry.harmonograf_supervisor import (  # noqa: PLC0415
            ensure_workspace_harmonograf,
        )
    except Exception as exc:  # noqa: BLE001 — supervisor import is best-effort
        log.warning("harmonograf auto-launch skipped: supervisor module unavailable (%s)", exc)
        return "", _NoopShutdownHandle()

    handle = ensure_workspace_harmonograf(workspace_root)
    if not handle.web_url:
        # Helper's own failure-isolation path already logged a warning.
        return "", _NoopShutdownHandle()

    return handle.web_url, handle


def _build_meta_loop_emitter_safe(
    workspace_root: Path,
    harmonograf_url: str,
    evolve_started_at_iso: str,
    *,
    grpc_target: str = "",
) -> Any:
    """Build the meta-loop emitter; never raise.

    The factory itself is best-effort — a missing goldfive proto stub
    or a permission error on the JSONL parent directory must not block
    an evolve invocation. Return ``None`` on any unexpected error so
    the orchestrator simply skips meta-loop emits (every call site is
    ``None``-tolerant).
    """
    with best_effort(
        "meta-loop emitter build",
        on_error=lambda exc: log.warning(
            "meta-loop emitter build failed (%s); evolve continues without "
            "proposer / analyzer telemetry envelopes",
            exc,
        ),
    ):
        from zicato.telemetry.meta_loop import (  # noqa: PLC0415
            build_meta_loop_emitter,
        )

        return build_meta_loop_emitter(
            workspace_root,
            harmonograf_url=harmonograf_url,
            evolve_started_at_iso=evolve_started_at_iso,
            grpc_target=grpc_target,
        )
    return None


class _NoopShutdownHandle:
    """A referenced external service whose lifetime belongs to another owner."""

    def __init__(self, grpc_target: str = "") -> None:
        self.grpc_target = grpc_target

    def shutdown(self) -> None:
        pass


def _record_progress(writer: WorkspaceLock, transition: str) -> int | None:
    """Append an owned progress transition; return None on a storage failure."""
    with best_effort(
        "progress-log append",
        on_error=lambda exc: log.debug("progress-log append skipped: %s", exc),
    ):
        from zicato.runtime import progress_log  # noqa: PLC0415

        return progress_log.append_progress(writer, transition)
    return None


def _beat(
    beater: HeartbeatBeater | None,
    *,
    progress_writer: WorkspaceLock | None = None,
    progress: str | None = None,
    **fields: Any,
) -> None:
    """Update and flush heartbeat fields when a beater exists.

    A progress transition additionally requires the acquired workspace writer.
    Its persisted sequence advances the heartbeat's liveness cursor. An update
    with no transition leaves that cursor unchanged. Storage failures remain
    best-effort; omitting a required progress owner is a caller error.
    """
    if beater is None:
        return
    seq = None
    if progress is not None:
        if progress_writer is None:
            raise ValueError("a progress transition requires its workspace writer")
        seq = _record_progress(progress_writer, progress)
    with best_effort(
        "heartbeat update",
        on_error=lambda exc: log.debug("heartbeat update skipped: %s", exc),
    ):
        if seq is not None:
            beater.update(seq=seq, **fields)
        else:
            beater.update(**fields)
        beater.bump_now()
