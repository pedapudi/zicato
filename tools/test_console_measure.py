"""Each pass of `tools/console_measure.py` runs on the real tree and returns its documented shape.

The tool lives outside `testpaths`, so this file rides CI on the same explicit
argument `tools/test_prose_lint.py` does. The shape assertions hold the keys
`docs/design/CONSOLE-SIMPLIFICATION.md` §2 cites; the stylesheet assertion
holds the state §3.7 leaves: no rule whose every selector names a class the
console never emits.
"""

from __future__ import annotations

import json
from pathlib import Path

import console_measure
import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def run_json(capsys: pytest.CaptureFixture[str]):
    def run(*argv: str) -> dict[str, object]:
        assert console_measure.main([*argv, "--json"]) == 0
        return json.loads(capsys.readouterr().out)

    return run


def test_css_pass_finds_no_dead_rule(run_json) -> None:
    result = run_json("css")
    assert set(result) >= {
        "distinct_classes",
        "rules",
        "no_static_reference",
        "explained_by_prefix",
        "unreferenced",
        "unreferenced_test_only",
        "dead_rules",
        "dead_rule_lines",
        "prefixes",
        "families",
        "unreferenced_classes",
        "test_only_classes",
        "dead_rule_spans",
    }
    assert result["distinct_classes"] > 1000 and result["rules"] > 1000
    assert "dn-turn-" in result["prefixes"]
    assert result["dead_rules"] == 0, result["dead_rule_spans"]


def test_css_prefix_rule_reads_both_forms() -> None:
    strings = console_measure.js_strings(
        "const a = 'dn-turn dn-turn-' + role;\n"
        "const b = `dt-glyph-${kind}`;\n"
        "const c = `dn-pill dn-${verdict}`;\n"
        "const d = 'dn-plain';\n"
    )
    prefixes, families = console_measure._dynamic_prefixes(strings)
    assert prefixes == {"dn-turn-", "dt-glyph-"}
    assert families == {"dn-"}


def test_js_strings_skip_comments_and_regexes() -> None:
    strings = console_measure.js_strings(
        "// 'dn-in-comment'\n/* \"dn-in-block\" */\n"
        "const re = /'dn-in-regex'/;\nconst s = 'dn-live';\n"
    )
    assert [s.text for s in strings] == ["dn-live"]


def test_exports_pass_counts_callers(run_json) -> None:
    result = run_json("exports")
    assert set(result) == {"module", "exports", "dead", "rows"}
    rows = {r["name"]: r for r in result["rows"]}
    assert rows["isNum"]["outside"] > 100 and rows["isNum"]["inside"]
    assert result["dead"] == []


def test_routes_pass_matches_holes_to_segments(run_json) -> None:
    assert console_measure._route_matches("/api/control/kill/{x}", "/api/control/{x}/{x}")
    assert not console_measure._route_matches("/api/files", "/api/files/")
    result = run_json("routes")
    assert set(result) == {
        "served",
        "read_by_browser",
        "read_by_tui",
        "unread",
        "unread_routes",
        "tui_routes",
    }
    assert result["served"] > 70
    assert "/api/health" in result["tui_routes"]
    assert "/api/heartbeat" in result["unread_routes"]


def test_assertions_pass_partitions_every_line(run_json) -> None:
    result = run_json("assertions")
    assert set(result) == {
        "files",
        "assertions",
        "class_literal",
        "dom_property",
        "query_selector",
        "other",
    }
    parts = ("class_literal", "dom_property", "query_selector", "other")
    assert sum(result[k] for k in parts) == result["assertions"] > 1000


def test_clones_pass_reports_merged_runs(run_json) -> None:
    result = run_json("clones", "--loose", "--window", "12")
    assert set(result) == {"window", "loose", "pairs", "duplicate_lines", "per_file", "runs"}
    assert result["window"] == 12 and result["loose"] is True
    assert result["pairs"] == len(result["runs"])
    assert all(run["lines"] >= 12 for run in result["runs"])
    assert result["duplicate_lines"] == sum(run["lines"] for run in result["runs"])


def test_normalise_line_collapses_literals_and_drops_braces() -> None:
    assert console_measure.normalise_line("  x = foo('a b', 42);  ", loose=False) == 'x=foo("",0);'
    assert console_measure.normalise_line("  x = foo('a b', 42);  ", loose=True) == '_=_("",0);'
    assert console_measure.normalise_line("  }", loose=False) is None
    assert console_measure.normalise_line("// note", loose=False) is None
