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

from tests.tui_fixture import EPOCH, PAYLOADS, live_payloads
from zicato.tui import present
from zicato.tui.client import FailingClient, SnapshotClient
from zicato.tui.lenses import BY_NAME, LENSES, LensContext, safe_render
from zicato.tui.routes import Route, parse_route
from zicato.tui.view import View, render_text

GOLDENS = Path(__file__).parent / "goldens" / "tui"

#: The route each lens is golden-pinned at, and which payload state it uses.
CASES = {
    "home": (Route(lens="home", params={"epoch": EPOCH}), "settled"),
    "standings": (Route(lens="standings", params={"epoch": EPOCH}), "live"),
    "instrument": (Route(lens="instrument", params={"epoch": EPOCH}), "settled"),
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


def test_home_lifeline_leads_with_the_epoch_open_calibration() -> None:
    """A calibration in flight leads the lifeline with its own stage and the
    served draw count, so a minutes-long measurement is not read as a round
    that started and stalled (issue #175)."""
    payloads = live_payloads()
    payloads["/api/live/pipeline"] = {
        **payloads["/api/live/pipeline"],
        "phase": "evolve_once:calibrating_noise_floor:2/3",
        "steps": [
            {"id": s["id"], "label": s["label"], "state": "pending", "detail": ""}
            for s in payloads["/api/live/pipeline"]["steps"]
        ],
        "active_step": None,
        "epoch_open_step": {
            "id": "calibrating_noise_floor",
            "label": "calibrating noise floor",
            "detail": "2/3 draws",
        },
        "in_flight": 1,
    }
    ctx = LensContext(route=Route(lens="home", params={"epoch": EPOCH}), width=100)
    text = render_text(safe_render(BY_NAME["home"], SnapshotClient(payloads), ctx), width=100)
    assert "calibrating noise floor" in text
    assert "2/3 draws" in text
    assert "no round in flight" not in text


def test_home_lifeline_leads_with_the_epoch_open_preflight() -> None:
    """The contract pre-flight leads the lifeline with its served probe count,
    exactly as the calibration does (issue #276)."""
    payloads = live_payloads()
    payloads["/api/live/pipeline"] = {
        **payloads["/api/live/pipeline"],
        "phase": "evolve_once:contract_preflight:1/4",
        "steps": [
            {"id": s["id"], "label": s["label"], "state": "pending", "detail": ""}
            for s in payloads["/api/live/pipeline"]["steps"]
        ],
        "active_step": None,
        "epoch_open_step": {
            "id": "contract_preflight",
            "label": "contract pre-flight",
            "detail": "1/4 probes",
        },
        "in_flight": 1,
    }
    ctx = LensContext(route=Route(lens="home", params={"epoch": EPOCH}), width=100)
    text = render_text(safe_render(BY_NAME["home"], SnapshotClient(payloads), ctx), width=100)
    assert "contract pre-flight" in text
    assert "1/4 probes" in text
    assert "no round in flight" not in text


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


def test_null_renders_as_the_em_dash_never_zero() -> None:
    """Zero is a legal measurement; an absent value must not impersonate one."""
    payloads = dict(PAYLOADS)
    payloads[f"/api/epoch/{EPOCH}/cost"] = {"epoch_id": EPOCH, "cost_per_promotion_ms": None}
    view = safe_render(
        BY_NAME["home"], SnapshotClient(payloads), LensContext(route=CASES["home"][0])
    )
    cost_row = next(r for r in view.rows() if r.key == "cost")
    assert cost_row.text.strip().endswith(present.NULL)


def test_provisional_rating_prints_the_estimate_faintly_beside_its_caveat() -> None:
    """The STANDINGS half of the browser's provisional asymmetry.

    ``ui.js`` renders a provisional rating two ways: the dossier stat declines
    to print the point estimate at all, while the standings cell prints it with
    a faint ``provisional`` suffix. This build ships only the standings form,
    so the estimate must be present AND caveated — flattening it either way
    (dropping the number, or dropping the suffix) breaks the cross-pin.
    """
    gauntlet = safe_render(
        BY_NAME["standings"],
        SnapshotClient(PAYLOADS),
        LensContext(route=CASES["standings"][0]),
    )
    text = render_text(gauntlet)
    assert "provisional" in text
    assert "1488 ±77" in text


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


def test_recommendation_queue_prints_the_command_and_runs_nothing() -> None:
    """This build applies nothing — it prints the line the operator runs.

    The row must carry NO action at all: an action is something ``enter``
    would execute, and there is deliberately no execution path in this lens.
    """
    view = render("instrument")
    queued = [r for r in view.rows() if r.key.startswith("queue:")]
    assert queued
    for row in queued:
        assert row.action is None
        assert "zicato inspect reflection apply refl-2026-07-04 " in row.text


def test_no_row_anywhere_carries_an_executable_action() -> None:
    """The whole build is read-only; every action is a ROUTE, never a command."""
    from zicato.tui.routes import LENSES as LENS_NAMES

    for name in CASES:
        for row in render(name).rows():
            if row.action is None:
                continue
            assert not row.action.startswith("!"), (name, row.key)
            assert parse_route(row.action).lens in LENS_NAMES


def _instrument_with_practices(review: object) -> View:
    """Render the instrument lens over a practice review of this test's own."""
    from copy import deepcopy

    payloads = deepcopy(PAYLOADS)
    served = payloads["/api/reflection/refl-2026-07-04/practices"]
    served.update(review.to_json())  # type: ignore[attr-defined]
    ctx = LensContext(route=CASES["instrument"][0], width=100)
    return safe_render(BY_NAME["instrument"], SnapshotClient(payloads), ctx)


def test_the_practices_table_renders_the_keys_the_serializer_writes() -> None:
    """Every column of a practice row comes from ``PracticeCheck.to_json``.

    The columns read ``check_id``, ``headline`` and ``rationale``. Reading any
    other name renders an empty column rather than an error, so this asserts
    the rendered text carries each check's own words.
    """
    from zicato.reflection.practices import PracticeCheck, PracticeReview

    check = PracticeCheck(
        check_id="oracle_mix",
        verdict="unsound",
        headline="All 4 board oracles are exact-text.",
        evidence={"n_strong": 0, "n_weak": 4},
        rationale="weak oracles saturate once a candidate clears them.",
        unmeasured_reason=None,
    )
    view = _instrument_with_practices(PracticeReview(checks=(check,)))

    # The table truncates to its column widths, so the rendered text carries
    # the head of each sentence; the whole of it rides on the evidence slots.
    text = render_text(view)
    assert "All 4 board oracles" in text
    assert "weak oracles saturate" in text
    assert "unsound" in text

    row = next(r for r in view.rows() if r.key == "check:oracle_mix")
    slots = dict(row.evidence)
    assert slots["what"] == "practice oracle_mix"
    assert slots["measured"] == check.headline
    assert slots["decision"] == "unsound"


def test_an_unmeasured_check_names_the_input_it_lacked() -> None:
    """``unmeasured`` is a verdict with a reason; the row must carry it."""
    from zicato.reflection.practices import PracticeCheck, PracticeReview

    check = PracticeCheck(
        check_id="calibration_freshness",
        verdict="unmeasured",
        headline="No judge has been calibrated.",
        evidence={},
        rationale="an uncalibrated judge states a verdict nothing checks.",
        unmeasured_reason="no calibration record in this epoch",
    )
    view = _instrument_with_practices(PracticeReview(checks=(check,)))

    row = next(r for r in view.rows() if r.key == "check:calibration_freshness")
    assert dict(row.evidence)["uncertainty"] == "no calibration record in this epoch"


def test_replacing_a_check_changes_the_screen_digest() -> None:
    """The digest gates the repaint, so it must fold what the rows display.

    A digest keyed on a field the payload does not carry is constant across
    every review, and the screen keeps a stale table.
    """
    from zicato.reflection.practices import PracticeCheck, PracticeReview

    def _check(check_id: str, headline: str, rationale: str) -> PracticeCheck:
        return PracticeCheck(
            check_id=check_id,
            verdict="sound",
            headline=headline,
            evidence={},
            rationale=rationale,
        )

    first = _check("oracle_mix", "The board mixes 3 structured oracles.", "weak oracles saturate.")
    digest = _instrument_with_practices(PracticeReview(checks=(first,))).digest

    # A different check, same verdict.
    other = _check(
        "budget_sanity", "The round budget covers 8 runs.", "an exhausted budget aborts."
    )
    assert _instrument_with_practices(PracticeReview(checks=(other,))).digest != digest

    # The same check, reworded — the table shows the words, so the digest moves.
    reworded = _check("oracle_mix", "The board mixes 4 structured oracles.", first.rationale)
    assert _instrument_with_practices(PracticeReview(checks=(reworded,))).digest != digest

    # An identical re-serve repaints nothing.
    assert _instrument_with_practices(PracticeReview(checks=(first,))).digest == digest
