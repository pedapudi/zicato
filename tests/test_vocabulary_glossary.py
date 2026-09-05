"""Pin: the glossary is the one place a term is defined, and the surfaces
that name terms resolve to it.

``docs/design/VOCABULARY.md`` defines every term a reader meets on a user
surface. Two other places name terms without defining them: the
developer guide's quick-reference table
(``docs/dev-guide/01-orientation.md`` §2.0), which maps each term to the
symbol and file that own it, and the closed list of terms the glossary
must carry because a dashboard label or a ``--help`` body uses them. Each
of those must resolve to a glossary heading, or a reader who follows the
pointer finds nothing.

The checks are mechanical. A term is a heading; a pointer is a Markdown
link into ``VOCABULARY.md``; resolution is the link's anchor matching a
heading's slug. Nothing here judges the prose.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GLOSSARY = ROOT / "docs" / "design" / "VOCABULARY.md"
DEV_GUIDE = ROOT / "docs" / "dev-guide" / "01-orientation.md"

#: Terms a dashboard label or a ``--help`` body uses, or that the design
#: documents use without defining. Every one must be a glossary heading.
REQUIRED_TERMS = (
    "Champion",
    "Challenger",
    "Scalar",
    "Gate",
    "Field",
    "Rung",
    "Contract",
    "Workspace",
    "Board unit",
    "Replicate",
    "Noise floor",
    "Best-of-N",
    "Screening",
    "Field diversity",
    "Settlement receipt",
    "Placebo arm",
    "Evidence gate",
    "Promotion gate",
    "Champion gate",
    "Regression gate",
    "Pareto frontier",
    "Round log",
    "Proposer scorecard",
    "Generation store",
    "Board reflection",
    "System under test",
    "Detectable effect",
    "Discrimination",
    "Dead letter",
    "Calibration",
    "Trajectory",
    "Copeland score",
    "Resolver",
    "False promotion",
    "Fast mode",
    "Preflight",
)

#: Quick-reference rows that name a code mechanism with no user-facing
#: concept, and so carry no glossary link. A row added to the table must
#: link, or be added here with the same justification.
UNLINKED_ROWS = frozenset({"worker boundary", "record-format guard"})


def _slug(heading: str) -> str:
    """GitHub's heading anchor: lowercase, punctuation dropped, spaces hyphenated."""
    text = re.sub(r"`", "", heading.strip()).lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def _headings(text: str) -> list[str]:
    out: list[str] = []
    fenced = False
    for line in text.splitlines():
        if line.startswith("```"):
            fenced = not fenced
            continue
        if not fenced and line.startswith("## "):
            out.append(line[3:].strip())
    return out


def _glossary_headings() -> list[str]:
    return _headings(GLOSSARY.read_text(encoding="utf-8"))


def _quick_reference_rows() -> list[str]:
    """The body rows of the §2.0 table, as raw Markdown lines."""
    text = DEV_GUIDE.read_text(encoding="utf-8")
    start = text.index("| Term | Owning symbol | Owning file |")
    end = text.index("### 2.1", start)
    rows = [line for line in text[start:end].splitlines() if line.startswith("| ")]
    return [row for row in rows if not row.startswith("| Term") and not row.startswith("|---")]


def test_glossary_headings_are_unique_and_alphabetical() -> None:
    """One entry per term, in the order a reader scans for it."""
    headings = _glossary_headings()
    assert len(headings) == len(set(headings)), "a glossary term is defined twice"
    assert headings == sorted(headings, key=str.casefold), "glossary entries are out of order"


def test_glossary_internal_links_resolve() -> None:
    """Every ``(#anchor)`` link inside the glossary names a heading in it."""
    slugs = {_slug(h) for h in _glossary_headings()}
    text = GLOSSARY.read_text(encoding="utf-8")
    dangling = sorted({a for a in re.findall(r"\]\(#([^)\s]+)\)", text) if a not in slugs})
    assert not dangling, f"glossary links to headings it does not have: {dangling}"


def test_required_terms_have_a_glossary_entry() -> None:
    """The terms user surfaces use are each defined."""
    headings = set(_glossary_headings())
    missing = [t for t in REQUIRED_TERMS if t not in headings]
    assert not missing, f"terms without a glossary entry: {missing}"


def test_quick_reference_rows_point_into_the_glossary() -> None:
    """Every developer-guide row links to a glossary heading that exists."""
    slugs = {_slug(h) for h in _glossary_headings()}
    unlinked: set[str] = set()
    dangling: list[tuple[str, str]] = []
    for row in _quick_reference_rows():
        term_cell = row.split(" | ")[0][2:]
        anchors = re.findall(r"\]\(\.\./design/VOCABULARY\.md#([^)\s]+)\)", term_cell)
        if not anchors:
            unlinked.add(term_cell)
            continue
        dangling.extend((term_cell, a) for a in anchors if a not in slugs)
    assert not dangling, f"quick-reference rows link to missing glossary headings: {dangling}"
    assert unlinked == UNLINKED_ROWS, (
        f"quick-reference rows without a glossary link: {sorted(unlinked)}; "
        f"expected exactly {sorted(UNLINKED_ROWS)}"
    )
