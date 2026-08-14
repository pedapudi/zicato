"""The PYTHON half of the render cross-pin.

Its twin is ``src/zicato/dashboard/static/test/render_crosspin.test.mjs``. Both
read the same fixture and assert it against their own implementation of the
mappings the browser derives in JS, so the terminal console and the browser
dashboard cannot drift apart about what "stalled" means, when a rating reads
``provisional``, or whether an absent outcome is a rejection.

If you change ``zicato/tui/present.py`` and this file goes red, the fix is
almost never here — it is that the browser still says something else.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from zicato.tui import present

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "src/zicato/dashboard/static/test/fixtures/render_crosspin.json"
)


@pytest.fixture(scope="module")
def cases() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_fixture_is_the_one_the_node_suite_reads() -> None:
    """The two witnesses must read the same file, not two copies of it."""
    node_test = FIXTURE.parent.parent / "render_crosspin.test.mjs"
    assert node_test.exists()
    assert "fixtures', 'render_crosspin.json'" in node_test.read_text(encoding="utf-8")


def test_loop_verdict(cases: dict[str, Any]) -> None:
    for case in cases["loop_verdict"]:
        got = present.loop_verdict(case["traj"])
        if case["expect"] is None:
            assert got is None, case
        else:
            assert got is not None
            assert got.word == case["expect"]["word"]
            assert got.cls == case["expect"]["cls"]


def test_rating_model(cases: dict[str, Any]) -> None:
    for case in cases["rating_model"]:
        got = present.rating_model(case["src"])
        if case["expect"] is None:
            assert got is None, case
            continue
        assert got is not None
        assert got.elo == case["expect"]["elo"]
        assert got.se == case["expect"]["se"]
        assert got.games == case["expect"]["games"]
        assert got.provisional is case["expect"]["provisional"]
        assert got.text == case["expect"]["text"]


def test_verdict_label(cases: dict[str, Any]) -> None:
    for case in cases["verdict_label"]:
        assert present.verdict_label(case["decision"]) == case["expect"], case


def test_fmt_duration_ms(cases: dict[str, Any]) -> None:
    for case in cases["fmt_duration_ms"]:
        assert present.fmt_duration_ms(case["ms"]) == case["expect"], case


def test_promotion_and_cost_labels(cases: dict[str, Any]) -> None:
    for case in cases["promotion_rate_label"]:
        assert present.promotion_rate_label(case["traj"]) == case["expect"], case
    for case in cases["cost_per_promotion_label"]:
        assert present.cost_per_promotion_label(case["cost"]) == case["expect"], case


def test_number_formatting(cases: dict[str, Any]) -> None:
    for case in cases["score_fmt"]:
        assert present.score_fmt(case["value"], case["digits"]) == case["expect"], case
    for case in cases["fmt_signed"]:
        assert present.fmt_signed(case["value"], case["digits"]) == case["expect"], case


def test_reflection_tones(cases: dict[str, Any]) -> None:
    for case in cases["severity_tone"]:
        assert present.severity_tone(case["severity"]) == case["expect"], case
    for case in cases["practice_tone"]:
        assert present.practice_tone(case["verdict"]) == case["expect"], case


def test_normalize_structure(cases: dict[str, Any]) -> None:
    for case in cases["normalize_structure"]:
        got = present.normalize_structure(case["st"], case["live"])
        if case["expect"] is None:
            assert got is None, case
            continue
        assert got is not None
        assert got["structure"] == case["expect"]["structure"]
        assert got["live"] is case["expect"]["live"]
        assert got["source"] == case["expect"]["source"]
        assert got["phase"] == case["expect"]["phase"]
        assert [r["round_index"] for r in got["rounds"]] == case["expect"]["round_indexes"]


def test_note_progress(cases: dict[str, Any]) -> None:
    """The seq gate — the one cross-pinned mapping with a runtime cost.

    A divergence here is not a cosmetic difference: it is the difference
    between a terminal that sits idle through a no-op beat and one that
    refetches the whole workspace on every file touch.
    """
    for case in cases["note_progress"]:
        got = present.note_progress(case["seq"], None, case["last"])
        assert got.advanced is case["expect"]["advanced"], case["why"]
        assert got.rollover is case["expect"]["rollover"], case["why"]
        assert got.present is case["expect"]["present"], case["why"]


def test_should_refresh_degrades_open_when_seq_is_absent() -> None:
    """No cursor ⇒ refresh anyway. A stale screen beats a saved request."""
    assert present.note_progress(None, None, 4).should_refresh is True
    assert present.note_progress(5, None, 4).should_refresh is True
    assert present.note_progress(1, None, 90).should_refresh is True
    assert present.note_progress(4, None, 4).should_refresh is False


def test_the_four_absences_are_four_distinct_strings() -> None:
    """Collapsing any two of these hides a different operator action."""
    no_measurement = present.measured(None)
    impossible = present.measured(None, enough=False)
    third_verdict = present.measured(None, reason="the A/A calibration never ran")
    assert no_measurement == present.NULL
    assert impossible == present.INSUFFICIENT
    assert third_verdict == "unmeasured · the A/A calibration never ran"
    assert len({no_measurement, impossible, third_verdict}) == 3
    # A real measurement is never displaced by any of them — including zero,
    # which is a legal value and must not read as an absence.
    assert present.measured(0.0, digits=2) == "0.00"
    assert present.measured(0.0, digits=2, reason="ignored") == "0.00"


def test_is_num_rejects_booleans() -> None:
    """``True`` is an ``int`` in Python and is NOT a number in JS's ``isNum``.

    Without this the Python port would render a boolean payload field as ``1``
    where the browser renders ``—``, which is exactly the kind of silent
    divergence the cross-pin exists to prevent.
    """
    assert present.is_num(True) is False
    assert present.is_num(1) is True
    assert present.fmt(True, 2) == present.NULL
