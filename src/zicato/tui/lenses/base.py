"""The lens contract and the shared row vocabulary.

Two things live here. First :class:`Lens` — the tiny protocol every lens
satisfies. Second the handful of row builders that would otherwise be copied
into six modules: the champion line, the rating cell, the evidence slots.

Keeping those shared matters for more than brevity. The five-slot evidence
convention means a row's drawer always answers the same five questions in the
same order; a per-lens copy is how that convention rots.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from zicato.tui import glyphs, present
from zicato.tui.client import Client, ServiceError
from zicato.tui.routes import Route
from zicato.tui.view import Block, Row, Span, View, digest_of, row


@dataclass(frozen=True)
class LensContext:
    """Everything a lens needs to know about the surface it is drawing on."""

    route: Route
    width: int = 100
    ascii_only: bool = False

    @property
    def narrow(self) -> bool:
        """Under ~100 columns the rail collapses and tables shed columns."""
        return self.width < 100

    @property
    def epoch(self) -> str | None:
        return self.route.params.get("epoch")


class Lens(Protocol):
    """A named surface that renders served payloads into a :class:`View`."""

    name: str
    title: str
    #: The one-word rail label.
    label: str

    @staticmethod
    def render(client: Client, ctx: LensContext) -> View:
        """Return the lens's screen. Never raises for a degraded payload."""


def safe_render(lens: Lens, client: Client, ctx: LensContext) -> View:
    """Render ``lens``, converting a service failure into a visible state.

    A terminal that shows a stale screen while the service is gone is the one
    outcome worth engineering against, so a :class:`ServiceError` becomes a
    View that SAYS the service is unreachable and prints the command that
    fixes it. Any other exception is surfaced the same way rather than tearing
    the app down — a lens bug must not cost the operator their session.
    """
    try:
        return lens.render(client, ctx)
    except ServiceError as exc:
        return View(
            title=lens.title,
            degraded=str(exc),
            blocks=(Block(rows=(row("hint", (exc.hint or "", "faint")),)),) if exc.hint else (),
            digest=digest_of("service-error", str(exc)),
        )
    except Exception as exc:  # noqa: BLE001 - a lens bug degrades, never crashes
        return View(
            title=lens.title,
            degraded=f"this lens failed to render: {exc.__class__.__name__}: {exc}",
            digest=digest_of("lens-error", lens.name, repr(exc)),
        )


# ---------------------------------------------------------------------------
# Shared row vocabulary
# ---------------------------------------------------------------------------

#: The five slots, in order. Every drawer answers these five questions about
#: the row under the cursor — identity, what it measured, how sure, what was
#: decided, and where the evidence lives.
EVIDENCE_SLOTS = ("what", "measured", "uncertainty", "decision", "provenance")


def evidence(
    what: str,
    measured: str,
    uncertainty: str,
    decision: str,
    provenance: str,
) -> tuple[tuple[str, str], ...]:
    """Build the five-slot evidence block for a row, in the fixed order."""
    return tuple(
        zip(EVIDENCE_SLOTS, (what, measured, uncertainty, decision, provenance), strict=True)
    )


#: Semantic style per decision token. Colour is redundant with the word itself,
#: which is always printed — a NO_COLOR terminal loses nothing.
DECISION_STYLE = {
    "promoted": "good",
    "rejected": "bad",
    "deferred": "warn",
    "pending": "faint",
    "baseline": "accent",
}


def decision_span(decision: str) -> Span:
    """A decision token rendered with its severity style and browser label."""
    return Span(present.verdict_label(decision), DECISION_STYLE.get(decision, "plain"))


#: Width of the rating VALUE cell. Fixed so that whiskers in a column start at
#: the same character — two intervals drawn on one axis but at two different
#: offsets would compare wrongly, which is worse than not drawing them.
RATING_WIDTH = 10


def rating_spans(
    src: Any,
    *,
    ascii_only: bool = False,
    scale: tuple[float, float] | None = None,
) -> list[Span]:
    """Rating value, whisker and provisional suffix — uncertainty inline.

    The whisker is drawn on the caller's shared ``scale`` so two rows'
    intervals can be compared by eye; without a scale it is omitted rather
    than drawn on a private axis that would invite a false comparison.
    """
    model = present.rating_model(src)
    if model is None:
        return [Span(present.NULL.ljust(RATING_WIDTH), "faint")]
    spans = [Span(model.text.ljust(RATING_WIDTH))]
    if model.se is not None and scale is not None:
        spans.append(
            Span(
                glyphs.whisker(
                    model.elo - model.se,
                    model.elo,
                    model.elo + model.se,
                    scale=scale,
                    ascii_only=ascii_only,
                ),
                "faint",
            )
        )
    if model.provisional:
        spans.append(Span(" provisional", "faint"))
    return spans


def rating_scale(rows: list[dict[str, Any]]) -> tuple[float, float] | None:
    """The shared rating axis across ``rows``: the union of every interval."""
    lo: float | None = None
    hi: float | None = None
    for record in rows:
        model = present.rating_model(record)
        if model is None:
            continue
        se = model.se or 0
        lo = model.elo - se if lo is None else min(lo, model.elo - se)
        hi = model.elo + se if hi is None else max(hi, model.elo + se)
    if lo is None or hi is None or hi <= lo:
        return None
    return (lo, hi)


def missing(title: str, message: str, *, hint: str | None = None) -> View:
    """The degraded view a lens returns when its payload is absent.

    The null-degrade shape every reader shares: say what is missing and what
    would make it appear. Never an empty screen, and never a fabricated zero.
    """
    return View(
        title=title,
        degraded=message,
        blocks=(Block(rows=(row("hint", (hint, "faint")),)),) if hint else (),
        digest=digest_of("missing", title, message, hint),
    )


def as_dict(value: Any) -> dict[str, Any]:
    """A payload field as a dict — an absent or wrong-typed field is empty."""
    return value if isinstance(value, dict) else {}


def as_list(value: Any) -> list[Any]:
    """A payload field as a list — an absent or wrong-typed field is empty."""
    return value if isinstance(value, list) else []


#: The label column for every ``label   value`` line, across every lens. One
#: number so the six lenses read as one surface rather than six tables.
LABEL_WIDTH = 24


def kv_row(key: str, label: str, value: str, *, style: str = "plain", indent: int = 0) -> Row:
    """A ``label   value`` line, label faint, value carrying the meaning."""
    return row(
        key,
        (label.ljust(LABEL_WIDTH), "faint"),
        (value, style),
        indent=indent,
    )


__all__ = [
    "DECISION_STYLE",
    "EVIDENCE_SLOTS",
    "LABEL_WIDTH",
    "RATING_WIDTH",
    "Lens",
    "LensContext",
    "as_dict",
    "as_list",
    "decision_span",
    "evidence",
    "kv_row",
    "missing",
    "rating_scale",
    "rating_spans",
    "safe_render",
]
