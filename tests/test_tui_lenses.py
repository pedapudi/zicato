"""Every lens, as text: goldens, ASCII mode, narrow mode, degraded payloads.

The lenses are pure functions from served payloads to a :class:`View`, so the
whole surface is testable without a terminal. Three renders per lens are
golden-pinned (default / ASCII / 80 columns); the rest of the file asserts the
honesty constraints that a golden alone would let rot: no fabricated zeros, no
dropped evidence, and a visible degrade when a payload is absent.

Regenerate the goldens with ``ZICATO_UPDATE_TUI_GOLDENS=1 pytest
tests/test_tui_lenses.py`` — and READ the diff. A golden that changed because
the data changed is a bug; these fixtures are frozen.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.tui_fixture import CHALLENGER, EPOCH, PAYLOADS, live_payloads
from zicato.tui import present
from zicato.tui.client import FailingClient, SnapshotClient
from zicato.tui.lenses import BY_NAME, LENSES, LensContext, safe_render
from zicato.tui.routes import Route
from zicato.tui.view import View, render_text

GOLDENS = Path(__file__).parent / "goldens" / "tui"

#: The route each lens is golden-pinned at, and which payload state it uses.
CASES = {
    "home": (Route(lens="home", params={"epoch": EPOCH}), "settled"),
    "standings": (Route(lens="standings", params={"epoch": EPOCH}), "live"),
    "candidate": (
        Route(lens="candidate", params={"epoch": EPOCH, "gen": CHALLENGER}),
        "settled",
    ),
    "board": (Route(lens="board", params={"epoch": EPOCH}), "settled"),
    "instrument": (Route(lens="instrument", params={"epoch": EPOCH}), "settled"),
    "health": (Route(lens="health", params={"epoch": EPOCH}), "settled"),
}


def client_for(state: str) -> SnapshotClient:
    return SnapshotClient(PAYLOADS if state == "settled" else live_payloads())


def render(name: str, *, width: int = 100, ascii_only: bool = False) -> View:
    route, state = CASES[name]
    ctx = LensContext(route=route, width=width, ascii_only=ascii_only)
    return safe_render(BY_NAME[name], client_for(state), ctx)


def check_golden(path: Path, text: str) -> None:
    if os.environ.get("ZICATO_UPDATE_TUI_GOLDENS"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n", encoding="utf-8")
        return
    assert path.exists(), f"missing golden {path} — regenerate with ZICATO_UPDATE_TUI_GOLDENS=1"
    assert text + "\n" == path.read_text(encoding="utf-8")


@pytest.mark.parametrize("name", list(CASES))
def test_golden_default(name: str) -> None:
    view = render(name)
    check_golden(GOLDENS / f"{name}.txt", render_text(view, width=100))


@pytest.mark.parametrize("name", list(CASES))
def test_golden_ascii(name: str) -> None:
    """The ASCII/NO_COLOR render is a tested artefact, not an aspiration."""
    view = render(name, ascii_only=True)
    text = render_text(view, width=100, ascii_only=True)
    assert text.isascii(), "ASCII mode must emit no non-ASCII characters"
    check_golden(GOLDENS / f"{name}.ascii.txt", text)


@pytest.mark.parametrize("name", list(CASES))
def test_golden_narrow(name: str) -> None:
    view = render(name, width=80)
    text = render_text(view, width=80)
    assert all(len(line) <= 80 for line in text.splitlines())
    check_golden(GOLDENS / f"{name}.narrow.txt", text)


# ---------------------------------------------------------------------------
# Degrade paths — one per lens
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("lens", LENSES, ids=lambda lens: lens.name)
def test_service_absent_degrades_with_connect_instructions(lens: object) -> None:
    """No service is a VISIBLE state carrying the command that fixes it."""
    view = safe_render(lens, FailingClient(), LensContext(route=Route(lens=lens.name)))
    assert view.degraded
    assert "dashboard service" in view.degraded
    assert any("zicato dashboard" in row.text for row in view.rows())


@pytest.mark.parametrize("lens", LENSES, ids=lambda lens: lens.name)
def test_empty_workspace_degrades_without_fabricating_numbers(lens: object) -> None:
    """Every payload absent (the DQ3 null-degrade shape): say so, show no zeros."""
    view = safe_render(lens, SnapshotClient({}), LensContext(route=Route(lens=lens.name)))
    text = render_text(view)
    assert view.degraded or view.blocks
    # A degraded lens may print an em-dash for a null, never a made-up 0.
    assert " 0.000" not in text
    assert "0 ±0" not in text


@pytest.mark.parametrize("lens", LENSES, ids=lambda lens: lens.name)
def test_a_lens_bug_degrades_rather_than_crashing(lens: object) -> None:
    """A raising lens costs the operator a panel, never their session."""

    class Exploding:
        def begin_pass(self) -> None:
            return None

        def get(self, path: str) -> object:
            raise ZeroDivisionError("boom")

    view = safe_render(lens, Exploding(), LensContext(route=Route(lens=lens.name)))
    assert view.degraded and "ZeroDivisionError" in view.degraded


def test_index_absent_shows_the_reindex_note_rather_than_an_empty_table() -> None:
    """A never-indexed workspace serves a ``note``; the lens must print it."""
    payloads = dict(PAYLOADS)
    payloads[f"/api/generation/{EPOCH}/{CHALLENGER}/per-entry"] = {
        "epoch_id": EPOCH,
        "generation_id": CHALLENGER,
        "entries": [],
        "note": "index not built; run zicato reindex",
    }
    view = safe_render(
        BY_NAME["candidate"],
        SnapshotClient(payloads),
        LensContext(route=CASES["candidate"][0]),
    )
    assert "run zicato reindex" in render_text(view)


# ---------------------------------------------------------------------------
# Honesty constraints
# ---------------------------------------------------------------------------


def test_null_renders_as_the_em_dash_never_zero() -> None:
    """Zero is a legal measurement; an absent value must not impersonate one."""
    payloads = dict(PAYLOADS)
    payloads[f"/api/epoch/{EPOCH}/cost"] = {"epoch_id": EPOCH, "cost_per_promotion_ms": None}
    view = safe_render(
        BY_NAME["home"], SnapshotClient(payloads), LensContext(route=CASES["home"][0])
    )
    cost_row = next(r for r in view.rows() if r.key == "cost")
    assert cost_row.text.strip().endswith(present.NULL)


def test_provisional_rating_says_so() -> None:
    """Under the games threshold the number carries its own caveat."""
    view = render("standings")
    text = render_text(view)
    assert "provisional" not in text  # the live swiss field has no provisional rows
    gauntlet = safe_render(
        BY_NAME["standings"],
        SnapshotClient(PAYLOADS),
        LensContext(route=CASES["standings"][0]),
    )
    assert "provisional" in render_text(gauntlet)


def test_projected_scalar_is_marked_as_projected() -> None:
    """An in-flight competitor's number must never read as settled."""
    view = render("standings")
    row = next(r for r in view.rows() if r.key == "s:v7")
    assert "~0.046" in row.text


def test_every_selectable_row_carries_the_five_evidence_slots() -> None:
    """The five-slot convention is what makes the drawer trustworthy."""
    from zicato.tui.lenses.base import EVIDENCE_SLOTS

    for name in CASES:
        view = render(name)
        for row in view.selectable_rows():
            if not row.evidence:
                continue
            assert tuple(label for label, _ in row.evidence) == EVIDENCE_SLOTS, (
                name,
                row.key,
            )


def test_gate_shows_all_rules_in_order_with_the_deciding_one_marked() -> None:
    view = render("candidate")
    rule_rows = [r for r in view.rows() if r.key.startswith("rule:")]
    assert [r.key for r in rule_rows] == [
        "rule:no_regression",
        "rule:scalar_margin",
        "rule:holdout_veto",
    ]
    deciding = [r for r in rule_rows if r.text.startswith("→")]
    assert [r.key for r in deciding] == ["rule:scalar_margin"]


def test_heat_strip_distinguishes_never_run_from_never_passed() -> None:
    view = render("board")
    strip_rows = {r.key: r.text for r in view.rows() if r.key.startswith("entry:")}
    assert "·" in strip_rows["entry:plan-long-2"]  # v4 never ran this entry
    assert "·" not in strip_rows["entry:plan-long-1"]


def test_recommendation_queue_only_shells_out() -> None:
    """The console never applies anything; it drives the operator's own CLI."""
    view = render("instrument")
    queued = [r for r in view.rows() if r.key.startswith("queue:")]
    assert queued
    for row in queued:
        assert row.action and row.action.startswith("!zicato reflect apply ")
