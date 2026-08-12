"""``zicato tui`` — the Console in the terminal.

A keyboard-driven review surface for a zicato workspace: watch a live round,
triage reflection findings, read a candidate's gate evidence, decide on a
recommendation — over SSH, without a browser.

**A second renderer, never a second brain.** The TUI renders the same served
model the dashboard does: the dashboard service's JSON payloads and its SSE
stream. Nothing here re-derives a verdict, an aggregate or a standing. Numbers
in the terminal are byte-equal to the browser's because they are the same
bytes, and the handful of presentation mappings the browser performs in JS are
ported into :mod:`zicato.tui.present` and cross-pinned against ``ui.js`` by a
shared fixture, so the two surfaces cannot disagree.

Textual is an optional dependency (the ``tui`` extra). This package imports
nothing from it until :func:`run_tui` is called, so ``zicato --help`` stays
fast and an install without the extra fails with an instruction rather than a
traceback.
"""

from __future__ import annotations

from pathlib import Path

from zicato.tui.client import ServiceError
from zicato.tui.routes import Route, parse_route

#: The message an install without the ``tui`` extra gets.
MISSING_EXTRA = (
    "the terminal console needs the `tui` extra: install it with "
    "`uv sync --extra tui` (or `pip install 'zicato[tui]'`)"
)


def run_tui(
    *,
    url: str | None = None,
    workspace: Path | None = None,
    view: str | None = None,
    port: int = 7892,
    ascii_only: bool | None = None,
) -> None:
    """Attach to a dashboard service (or start one) and run the console.

    Raises :class:`ImportError` with :data:`MISSING_EXTRA` when Textual is not
    installed, and :class:`~zicato.tui.client.ServiceError` (which carries an
    operator-facing hint) when no service can be reached.
    """
    try:
        from zicato.tui.app import ZicatoTui
    except ImportError as exc:  # pragma: no cover - depends on optional deps
        raise ImportError(MISSING_EXTRA) from exc

    from zicato.tui.service import attach

    attachment = attach(url=url, workspace=workspace, port=port)
    try:
        ZicatoTui(
            client=attachment.client,
            route=parse_route(view),
            url=attachment.url,
            workspace=str(workspace) if workspace else "",
            ascii_only=ascii_only,
        ).run()
    finally:
        attachment.close()


__all__ = ["MISSING_EXTRA", "Route", "ServiceError", "parse_route", "run_tui"]
