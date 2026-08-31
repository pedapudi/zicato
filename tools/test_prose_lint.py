"""Cover the prose lint: each rule, the allowlist, waivers, extraction, ratchet."""

from __future__ import annotations

import json
from pathlib import Path

import prose_lint
import pytest

# One hitting and one clean rendering of the same claim, per rule. The clean
# side is asserted silent under EVERY rule, so a widened pattern that starts
# swallowing ordinary sentences turns these red.
FIXTURES = {
    "codename-label": (
        "The WS-REC lane and the L3 layer run in Phase B of Wave-2.",
        "The recombination lane and the worker-isolation layer run at launch.",
    ),
    "narrated-history": (
        "The writer no longer emits a header, and the old format was pre-#42.",
        "The writer emits a header on every record.",
    ),
    "antithesis-apposition": (
        "The gate reads the record, not the tree.",
        "The gate reads the record.",
    ),
    "empty-intensifier": (
        "The bound is demonstrably tight and holds precisely.",
        "The bound is tight to within one unit in the last place.",
    ),
    "appositive-verb-tag": (
        "The slate settles, measured, before the gate runs.",
        "The gate measures the slate before it settles.",
    ),
    "bare-issue-reference": (
        "#241 adds the plan builder.",
        "Issue #241 adds the plan builder.",
    ),
}


def rules_hit(source: str, name: str = "doc.md") -> set[str]:
    return {hit.rule for hit in prose_lint.scan(source, name, prose_lint.RULES)}


@pytest.mark.parametrize(("rule", "pair"), sorted(FIXTURES.items()))
def test_each_rule_fires_on_its_construction_only(rule: str, pair: tuple[str, str]) -> None:
    hitting, clean = pair
    assert rules_hit(hitting) == {rule}
    assert rules_hit(clean) == set()


def test_every_rule_has_a_fixture() -> None:
    assert set(FIXTURES) == {rule.name for rule in prose_lint.RULES}


def test_standard_collocations_are_allowed() -> None:
    allowed = "An L4 load balancer fronts it; the L2 norm and the L1/L2 penalty score it."
    assert rules_hit(allowed) == set()
    assert rules_hit("The L1 cache misses, the L2 cache holds, the L3 cache is shared.") == set()
    assert rules_hit("An L4 stage fronts it.") == {"codename-label"}


def test_the_scanned_roots_cover_every_prose_tree() -> None:
    assert set(prose_lint.DEFAULT_PATHS) == {
        "CHANGELOG.md",
        "README.md",
        "docs",
        "examples",
        "skills",
        "src/zicato",
        "tools",
    }
    found = prose_lint.collect(prose_lint.DEFAULT_PATHS, prose_lint.ROOT)
    for root in prose_lint.DEFAULT_PATHS:
        assert any(path.is_relative_to(prose_lint.ROOT / root) for path in found), root


def test_generated_help_and_captured_evidence_are_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    found = prose_lint.collect(prose_lint.DEFAULT_PATHS, prose_lint.ROOT)
    for item in prose_lint.EXCLUDED:
        skipped = prose_lint.ROOT / item
        assert skipped.exists(), item
        assert not any(path.is_relative_to(skipped) for path in found), item
    # The generated help file sits under a scanned root, so the exclusion is
    # what removes it, and dropping the exclusion brings it back.
    monkeypatch.setattr(prose_lint, "EXCLUDED", ())
    reinstated = prose_lint.collect(("docs",), prose_lint.ROOT)
    assert prose_lint.ROOT / "docs" / "design" / "CLI.md" in reinstated


def test_an_excluded_directory_drops_its_whole_subtree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("keep", "drop"):
        (tmp_path / name).mkdir()
        (tmp_path / name / f"{name}.md").write_text("text\n", encoding="utf-8")
    monkeypatch.setattr(prose_lint, "EXCLUDED", ("drop",))
    assert [path.name for path in prose_lint.collect(["."], tmp_path)] == ["keep.md"]


def test_waiver_applies_on_the_line_and_the_line_above() -> None:
    marker = "<!-- prose-lint: allow codename-label -->"
    assert rules_hit(f"The L3 layer holds. {marker}") == set()
    assert rules_hit(f"{marker}\nThe L3 layer holds.") == set()
    assert rules_hit(f"{marker}\n\nThe L3 layer holds.") == {"codename-label"}


def test_waiver_names_one_rule_and_covers_only_that_rule() -> None:
    assert rules_hit("<!-- prose-lint: allow narrated-history -->\nThe L3 layer holds.") == {
        "codename-label"
    }
    assert rules_hit("<!-- prose-lint: allow all -->\nThe L3 layer holds.") == set()


def test_fenced_blocks_and_backtick_spans_are_quoted_material() -> None:
    fenced = "Text.\n\n```\nThe L3 layer holds.\n```\n\nMore text.\n"
    assert rules_hit(fenced) == set()
    assert rules_hit("The `L3` symbol is served under `WS-REC`.") == set()


SAMPLE_MODULE = '''\
"""The old reader is gone."""

VALUE = "The old reader is gone."


def read() -> str:
    """Return the value, not the tree."""
    # The L3 layer holds.
    return VALUE
'''


def test_docstrings_and_comments_are_read_and_other_strings_are_not() -> None:
    hits = prose_lint.scan(SAMPLE_MODULE, "module.py", prose_lint.RULES)
    assert {(hit.line, hit.rule) for hit in hits} == {
        (1, "narrated-history"),
        (7, "antithesis-apposition"),
        (8, "codename-label"),
    }


def test_unparseable_python_reports_nothing() -> None:
    assert prose_lint.scan("def (:\n", "broken.py", prose_lint.RULES) == []


def _write(directory: Path, text: str) -> None:
    (directory / "doc.md").write_text(text, encoding="utf-8")


def test_baseline_records_a_count_per_rule_and_ratchets(tmp_path: Path) -> None:
    _write(tmp_path, "The L3 layer holds.\n")
    baseline = tmp_path / "baseline.json"
    paths = ["--paths", str(tmp_path)]

    assert prose_lint.main([*paths, "--write-baseline", str(baseline)]) == 0
    recorded = json.loads(baseline.read_text())
    assert recorded["codename-label"] == 1
    assert recorded["narrated-history"] == 0

    assert prose_lint.main([*paths, "--baseline", str(baseline)]) == 0

    _write(tmp_path, "The L3 layer holds. The L4 stage holds.\n")
    assert prose_lint.main([*paths, "--baseline", str(baseline)]) == 1

    _write(tmp_path, "The worker-isolation layer holds.\n")
    assert prose_lint.main([*paths, "--baseline", str(baseline)]) == 0


def test_a_review_rule_alone_does_not_fail_the_run(tmp_path: Path) -> None:
    _write(tmp_path, "The bound is demonstrably tight.\n")
    assert prose_lint.main(["--paths", str(tmp_path)]) == 0  # reported, and green
    _write(tmp_path, "The bound is demonstrably tight, not loose.\n")
    assert prose_lint.main(["--paths", str(tmp_path)]) == 1


def test_rule_selection_narrows_the_scan(tmp_path: Path) -> None:
    _write(tmp_path, "The L3 layer holds.\n")
    assert prose_lint.main(["--paths", str(tmp_path), "--rule", "narrated-history"]) == 0
    assert prose_lint.main(["--paths", str(tmp_path), "--rule", "codename-label"]) == 1


def test_the_lint_passes_over_its_own_source() -> None:
    source = (prose_lint.ROOT / "tools" / "prose_lint.py").read_text(encoding="utf-8")
    failing = {rule.name for rule in prose_lint.RULES if rule.severity == prose_lint.FAILURE}
    assert rules_hit(source, "tools/prose_lint.py") & failing == set()
