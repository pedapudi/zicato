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


def test_decision_for(cases: dict[str, Any]) -> None:
    for case in cases["decision_for"]:
        spec = case["spec"]
        assert (
            present.decision_for(
                parent=spec.get("parent"),
                promoted=spec.get("promoted"),
                exp=spec.get("exp"),
                gate=spec.get("gate"),
                baseline=spec.get("baseline"),
            )
            == case["expect"]
        ), case


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


def test_is_num_rejects_booleans() -> None:
    """``True`` is an ``int`` in Python and is NOT a number in JS's ``isNum``.

    Without this the Python port would render a boolean payload field as ``1``
    where the browser renders ``—``, which is exactly the kind of silent
    divergence the cross-pin exists to prevent.
    """
    assert present.is_num(True) is False
    assert present.is_num(1) is True
    assert present.fmt(True, 2) == present.NULL
