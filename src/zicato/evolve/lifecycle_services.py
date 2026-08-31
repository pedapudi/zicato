"""Evolve-run lifecycle services split out of :mod:`zicato.orchestrator`.

These are the leaf-level services the evolve loop sets up once per
invocation and tears down in its ``finally`` block:

* harmonograf console resolution + auto-launch
  (:func:`_resolve_harmonograf_url`, :func:`_resolve_or_launch_harmonograf`)
  and the handles that own the launched server's lifecycle
  (:class:`_NoopShutdownHandle`, :class:`_LaunchedHandle`);
* environment-variable snapshot/restore for the auto-launched URL + gRPC
  target (:class:`_EnvVarRestorer`);
* the best-effort meta-loop emitter factory
  (:func:`_build_meta_loop_emitter_safe`);
* two tiny shared utilities — the UTC ISO clock (:func:`_now_iso`) and the
  heartbeat phase pusher (:func:`_beat`).

Callers import this module directly.
"""

from __future__ import annotations

import datetime as _dt
import logging
from pathlib import Path
from typing import Any

from zicato.runtime.heartbeat import HeartbeatBeater
from zicato.util import best_effort

log = logging.getLogger("zicato.orchestrator")


def _now_iso() -> str:
    return _dt.datetime.now(_dt.UTC).replace(microsecond=0).isoformat()


def _resolve_harmonograf_url(workspace_root: Path) -> str:
    """Resolve the harmonograf console URL for this run, or ``""``.

    Delegates to :func:`zicato.telemetry.sink.resolve_harmonograf_url`,
    feeding it the workspace ``config.json`` so every source is
    honoured: the ``--harmonograf-url`` flag (pinned into the typed
    config tree), the internal ``ZICATO_HARMONOGRAF_URL`` auto-launch
    handoff, and the ``harmonograf_url`` config key. Best-effort: any
    failure resolving the config falls back to the empty string so a
    broken config never blocks an evolve run.

    Note this resolves only the *configured* URL — it does NOT trigger
    auto-launch. :func:`_resolve_or_launch_harmonograf` is the variant
    the evolve loop uses, which falls back to spawning an in-process
    server when the configured URL is empty.
    """
    try:
        from zicato import workspace_loader  # noqa: PLC0415
        from zicato.telemetry.sink import resolve_harmonograf_url  # noqa: PLC0415

        try:
            cfg = workspace_loader.load_workspace_config(workspace_root)
        except Exception:  # noqa: BLE001 — config is optional here
            cfg = None
        return resolve_harmonograf_url(cfg)
    except Exception as exc:  # noqa: BLE001 — never block a run on this
        log.debug("harmonograf url resolution skipped: %s", exc)
        return ""


def _resolve_or_launch_harmonograf(
    workspace_root: Path,
) -> tuple[str, Any]:
    """Return ``(url, handle)`` for the harmonograf console this evolve uses.

    Auto-launch semantics (the default; issue #202):

    * If the operator pinned a URL — the ``--harmonograf-url`` flag or
      the workspace-config ``harmonograf_url`` key — use it verbatim
      and return a no-op handle — opt-out lets a long-lived shared
      harmonograf collect traffic from multiple zicato invocations.
      (An inherited ``ZICATO_HARMONOGRAF_URL`` handoff from an OUTER
      zicato invocation short-circuits the launch the same way, so a
      nested evolve reuses its parent's console.)
    * Otherwise launch an in-process harmonograf server bound to a free
      localhost port (see :mod:`zicato.telemetry.harmonograf_supervisor`)
      and return its URL + a real handle whose ``shutdown()`` the
      caller MUST invoke at evolve teardown.

    On any auto-launch failure (missing dep, port-bind error, startup
    timeout), the supervisor logs a warning and returns a no-op handle
    with ``url=""``. The orchestrator treats that as "JSONL-only
    telemetry": the live console is additive and never load-bearing.

    Side effect: when auto-launch succeeds, the resolved URL is also
    written into ``os.environ["ZICATO_HARMONOGRAF_URL"]`` so the
    tournament runner and worker subprocesses (which re-resolve via
    :func:`zicato.telemetry.sink.resolve_harmonograf_url`) attach their
    own per-run sinks to the same server without any further plumbing.
    The orchestrator restores the pre-launch env var value on shutdown
    via :class:`_EnvVarRestorer` — a nested evolve invocation that
    inherits a parent's auto-launched URL won't clobber it.
    """
    configured = _resolve_harmonograf_url(workspace_root)
    if configured:
        # Opt-out: external harmonograf in use. No auto-launch, no
        # env-var manipulation, no shutdown needed.
        log.debug("harmonograf auto-launch skipped: external URL configured (%s)", configured)
        return configured, _NoopShutdownHandle()

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

    # Make the resolved URL discoverable to the tournament runner and the
    # worker subprocesses, both of which re-resolve it via
    # resolve_harmonograf_url() (whose second lookup step is this internal
    # env handoff). The restorer is captured on the handle so
    # shutdown unsets / restores the environment cleanly.
    restorers: list[_EnvVarRestorer] = []
    url_restorer = _EnvVarRestorer("ZICATO_HARMONOGRAF_URL")
    url_restorer.set(handle.web_url)
    restorers.append(url_restorer)

    # The server binds TWO ports: the web URL above (for browser deep-
    # links) and a native gRPC port the per-run sinks must dial. Export the
    # gRPC target distinctly so the sink builders dial the gRPC port
    # instead of stripping the web URL and dialing the web port (which
    # would silently drop telemetry).
    grpc_target = getattr(handle, "grpc_target", "") or ""
    if grpc_target:
        grpc_restorer = _EnvVarRestorer("ZICATO_HARMONOGRAF_GRPC")
        grpc_restorer.set(grpc_target)
        restorers.append(grpc_restorer)

    return handle.web_url, _LaunchedHandle(handle, restorers)


def _build_meta_loop_emitter_safe(
    workspace_root: Path,
    harmonograf_url: str,
    evolve_started_at_iso: str,
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
        )
    return None


class _NoopShutdownHandle:
    """Tiny stand-in for the auto-launch handle when no launch happened.

    Used in two places: when the operator pinned an external URL (opt-
    out) and when the supervisor refused to launch (degraded install).
    Mirrors the ``shutdown()`` contract of the real handle so the
    orchestrator's ``finally`` block can call it unconditionally.
    """

    url: str = ""

    def shutdown(self) -> None:
        return None


class _LaunchedHandle:
    """Composite handle: server lifecycle plus env-var restoration.

    Holds a list of :class:`_EnvVarRestorer` — one for the web URL
    (``ZICATO_HARMONOGRAF_URL``) and, on the auto-launch path, one for the
    native gRPC target (``ZICATO_HARMONOGRAF_GRPC``) — so shutdown returns
    both env vars to their pre-launch state.
    """

    def __init__(self, inner: Any, restorers: list[_EnvVarRestorer]) -> None:
        self._inner = inner
        self._restorers = list(restorers)
        # ``inner`` may be a ``HarmonografHandle`` (``.url``) or a
        # ``WorkspaceHarmonografHandle`` (``.web_url``); accept either.
        self.url = getattr(inner, "url", None) or getattr(inner, "web_url", "")

    def shutdown(self) -> None:
        # Restore env BEFORE stopping the server so a concurrent
        # tournament-runner re-resolve does not pick up the auto-launched
        # URL after the server is gone.
        for restorer in self._restorers:
            with best_effort(
                "env restoration during harmonograf shutdown",
                on_error=lambda exc: log.debug(
                    "env restoration during harmonograf shutdown failed: %s", exc
                ),
            ):
                restorer.restore()
        with best_effort(
            "harmonograf shutdown",
            on_error=lambda exc: log.debug("harmonograf shutdown raised: %s", exc),
        ):
            self._inner.shutdown()


class _EnvVarRestorer:
    """RAII-style snapshot+restore for a single environment variable.

    Captures the variable's prior value on construction (or its absence)
    so :meth:`restore` returns the environment to the exact state the
    process started in. Idempotent — re-calling :meth:`restore` is a
    no-op.
    """

    def __init__(self, name: str) -> None:
        import os  # noqa: PLC0415

        self._os = os
        self._name = name
        self._had = name in os.environ
        self._prior = os.environ.get(name)
        self._restored = False

    def set(self, value: str) -> None:
        self._os.environ[self._name] = value

    def restore(self) -> None:
        if self._restored:
            return
        self._restored = True
        if self._had and self._prior is not None:
            self._os.environ[self._name] = self._prior
        else:
            self._os.environ.pop(self._name, None)


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
