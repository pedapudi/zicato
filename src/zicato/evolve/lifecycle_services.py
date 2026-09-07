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


def _record_progress(workspace_root: Path | None, transition: str | None) -> int | None:
    """Append one orchestrator progress transition; return the new ``seq``.

    The TRUE liveness step: on a genuine transition
    the loop appends a typed event to the progress event log
    (:mod:`zicato.runtime.progress_log`), whose monotonic ``seq`` advances
    only here — never on the heartbeat timer. Returns the new tail ``seq``
    so the caller can stamp it onto the heartbeat, or ``None`` when there
    is nothing to record (no ``workspace_root`` / ``transition``, e.g. a
    standalone ``evolve_once`` with no lifecycle).

    Best-effort: a failure to append must never abort the evolve round, so
    a write error swallows to ``None`` (the heartbeat simply keeps its
    prior ``seq``) rather than propagating.
    """
    if workspace_root is None or transition is None:
        return None
    seq: int | None = None

    def _remember(value: int) -> None:
        nonlocal seq
        seq = value

    with best_effort(
        "progress-log append",
        on_error=lambda exc: log.debug("progress-log append skipped: %s", exc),
    ):
        from zicato.runtime import progress_log  # noqa: PLC0415

        _remember(progress_log.append_progress(workspace_root, transition))
    return seq


def _beat(
    beater: HeartbeatBeater | None,
    *,
    workspace_root: Path | None = None,
    progress: str | None = None,
    **fields: Any,
) -> None:
    """Push a heartbeat phase/coordinate update and flush it immediately.

    A no-op when ``beater`` is ``None`` (a standalone ``evolve_once``
    call with no heartbeat lifecycle). Every update is followed by a
    :meth:`HeartbeatBeater.bump_now` so the dashboard sees the new phase
    without waiting for the next periodic bump. Best-effort: a failure
    to write the heartbeat must never abort the evolve round.

    ``progress`` — when supplied alongside ``workspace_root`` — names a
    GENUINE orchestrator transition (a :mod:`zicato.runtime.progress_log`
    type such as ``progress_log.PROPOSE``). The transition is appended to
    the progress event log and its new ``seq`` is stamped onto the
    heartbeat, so the heartbeat's ``seq`` advances on real progress
    (distinct from the timer-driven ``last_heartbeat``). A heartbeat-only
    ``_beat`` (no ``progress``) leaves ``seq`` unchanged — it carries the
    prior value forward, so a phase relabel that is not a fresh transition
    does not falsely advance the liveness cursor.
    """
    if beater is None:
        return
    seq = _record_progress(workspace_root, progress)
    with best_effort(
        "heartbeat update",
        on_error=lambda exc: log.debug("heartbeat update skipped: %s", exc),
    ):
        if seq is not None:
            beater.update(seq=seq, **fields)
        else:
            beater.update(**fields)
        beater.bump_now()
