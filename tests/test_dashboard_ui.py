"""Structural tests for ``supervisor/static/`` — the bundled dashboard UI.

These tests do not run the JavaScript. They parse the static HTML and
assert structural invariants that the Rust supervisor relies on:

* No external resource references (no ``http`` URLs, no remote scripts,
  no remote stylesheets, no Google Fonts).
* The expected sections, IDs, and SVG hooks are all present so
  ``app.js`` can find them.
* The total bundle size sits under the envelope (130 KB uncompressed
  for HTML + CSS + JS + icon sprite).
* The four-view structure is present: a nav rail with four entries and
  the matching view containers, plus the Epoch view's section ids.
* The dark-mode media query exists in the CSS.

These tests are pure parsing — no headless browser, no JS engine. They
are the bare floor that protects the contract between the Rust binary
and the static bundle.
"""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

import pytest

# Locate the static directory relative to this test file. The structure
# is fixed: ``<repo>/tests/`` and ``<repo>/supervisor/static/`` are
# siblings.
STATIC_DIR = Path(__file__).resolve().parent.parent / "supervisor" / "static"


@pytest.fixture(scope="module")
def index_html() -> str:
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def style_css() -> str:
    return (STATIC_DIR / "style.css").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def app_js() -> str:
    return (STATIC_DIR / "app.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def icons_svg() -> str:
    return (STATIC_DIR / "icons.svg").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# No external resource references
# ---------------------------------------------------------------------------


def test_index_has_no_external_urls(index_html: str) -> None:
    """No http://, https://, or // protocol-relative resource refs."""
    lowered = index_html.lower()
    # Allow xmlns and ARIA namespace references that contain http
    # (those are URI literals, not network fetches). We only want to
    # block actual resource attributes: src, href, action.
    for forbidden in (
        'src="http',
        "src='http",
        'href="http',
        "href='http",
        'action="http',
        "action='http",
        'src="//',
        "src='//",
        'href="//',
        "href='//",
    ):
        # Filter out xmlns (the SVG namespace href starts with http)
        # by checking only resource attributes.
        assert (
            forbidden not in lowered
        ), f"index.html contains a forbidden external resource ref: {forbidden!r}"


def test_index_has_no_inline_external_script(index_html: str) -> None:
    """The page must not pull JS from any external host."""
    assert '<script src="http' not in index_html.lower()
    assert "<script src='http" not in index_html.lower()
    # CDN heuristic: any URL with cdn., googleapis, jsdelivr, unpkg
    for needle in ("cdn.", "googleapis.com", "jsdelivr", "unpkg", "fonts.google"):
        assert needle not in index_html.lower(), f"CDN ref found: {needle}"


def test_css_has_no_external_url(style_css: str) -> None:
    """CSS must not @import or url() external resources."""
    lowered = style_css.lower()
    assert "@import" not in lowered, "style.css uses @import"
    # url(http://...) or url(//...) is banned. Inline data: URIs would be
    # fine but we don't currently emit any.
    for needle in ("url(http", "url('http", 'url("http', "url(//", "url('//", 'url("//'):
        assert needle not in lowered, f"style.css references external URL: {needle}"


def test_js_has_no_external_fetch(app_js: str) -> None:
    """app.js must only talk to relative paths under /api and /events.

    The W3C SVG namespace literal (``http://www.w3.org/2000/svg``) is
    NOT a network fetch — it is the XML-namespace URI required by
    ``createElementNS``. It is allowed.
    """
    # Strip comments first; then strip the one permitted literal.
    scrubbed = _strip_js_comments(app_js)
    scrubbed = scrubbed.replace("http://www.w3.org/2000/svg", "")
    for needle in ("http://", "https://"):
        assert (
            needle not in scrubbed
        ), f"app.js still references {needle} outside comments / xmlns literal"


# ---------------------------------------------------------------------------
# Expected DOM structure
# ---------------------------------------------------------------------------


class _SectionCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.section_ids: list[str] = []
        self.all_ids: set[str] = set()
        self.svg_ids: set[str] = set()
        self.nav_hrefs: list[str] = []
        self._in_nav = False
        self._svg_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_d = {k: v for k, v in attrs}
        if tag == "svg":
            self._svg_depth += 1
            if "id" in attrs_d and attrs_d["id"]:
                self.svg_ids.add(attrs_d["id"])
        if tag == "nav":
            self._in_nav = True
        if self._in_nav and tag == "a" and attrs_d.get("href"):
            self.nav_hrefs.append(attrs_d["href"])
        if "id" in attrs_d and attrs_d["id"]:
            self.all_ids.add(attrs_d["id"])
            if tag == "section":
                self.section_ids.append(attrs_d["id"])

    def handle_endtag(self, tag: str) -> None:
        if tag == "svg":
            self._svg_depth = max(0, self._svg_depth - 1)
        if tag == "nav":
            self._in_nav = False


# Sections that must exist somewhere in the page (now spread across the
# four views rather than one flat scroll).
REQUIRED_SECTIONS = {
    "tournament-section",
    "runs-section",
    "lineage-section",
    "trajectory-section",
    "heatmap-section",
    "log-section",
    "epoch-overview-section",
    "epoch-board-section",
    "epoch-rubric-section",
    "epoch-scoring-section",
    "epoch-mutations-section",
}

# The four view containers of the multi-view app.
REQUIRED_VIEW_IDS = {
    "view-overview",
    "view-tree",
    "view-tournament",
    "view-epoch",
}

# The nav rail's four entries.
REQUIRED_NAV_IDS = {
    "nav-overview",
    "nav-tree",
    "nav-tournament",
    "nav-epoch",
}

# Epoch-view panel containers app.js renders into.
REQUIRED_EPOCH_IDS = {
    "epoch-overview",
    "epoch-harness",
    "epoch-board",
    "epoch-rubric",
    "epoch-scoring",
    "epoch-mutations",
}

REQUIRED_IDS = (
    {
        "header-bar",
        "footer-bar",
        "nav-rail",
        "epoch-id",
        "generation-id",
        "round-id",
        "elapsed",
        "health-badge",
        "tournament-title",
        "tournament-body",
        "tournament-elapsed",
        "active-runs",
        "log-tail",
        "drill-panel",
        "drill-title",
        "drill-body",
        "drill-close",
        "supervisor-version",
        "supervisor-port",
        "supervisor-build",
    }
    | REQUIRED_VIEW_IDS
    | REQUIRED_NAV_IDS
    | REQUIRED_EPOCH_IDS
)

REQUIRED_SVG_IDS = {
    "lineage-svg",
    "trajectory-svg",
    "heatmap-svg",
}


def test_required_sections_present(index_html: str) -> None:
    p = _SectionCollector()
    p.feed(index_html)
    missing = REQUIRED_SECTIONS - set(p.section_ids)
    assert not missing, f"missing required sections: {sorted(missing)}"


def test_required_element_ids_present(index_html: str) -> None:
    p = _SectionCollector()
    p.feed(index_html)
    missing = REQUIRED_IDS - p.all_ids
    assert not missing, f"missing required element ids: {sorted(missing)}"


def test_required_svg_ids_present(index_html: str) -> None:
    p = _SectionCollector()
    p.feed(index_html)
    missing = REQUIRED_SVG_IDS - p.svg_ids
    assert not missing, f"missing required svg ids: {sorted(missing)}"


def test_index_loads_local_css_and_js(index_html: str) -> None:
    assert 'href="style.css"' in index_html, "style.css <link> missing"
    assert 'src="app.js"' in index_html, "app.js <script> missing"
    assert 'type="module"' in index_html, "app.js must load as ES module"


# ---------------------------------------------------------------------------
# Multi-view structure — nav rail + four view containers
# ---------------------------------------------------------------------------


def test_four_view_containers_present(index_html: str) -> None:
    """Overview / Tree / Tournament / Epoch each have a view container."""
    p = _SectionCollector()
    p.feed(index_html)
    missing = REQUIRED_VIEW_IDS - p.all_ids
    assert not missing, f"missing view containers: {sorted(missing)}"


def test_nav_rail_present_with_four_entries(index_html: str) -> None:
    """The nav rail exists and has exactly the four expected entries."""
    p = _SectionCollector()
    p.feed(index_html)
    assert "nav-rail" in p.all_ids, "nav rail container (#nav-rail) missing"
    missing = REQUIRED_NAV_IDS - p.all_ids
    assert not missing, f"missing nav entries: {sorted(missing)}"
    # The nav entries must fragment-route to the four views.
    for frag in ("#/overview", "#/tree", "#/tournament", "#/epoch"):
        assert frag in p.nav_hrefs, f"nav rail missing route {frag}"


def test_epoch_view_section_ids_present(index_html: str) -> None:
    """The Epoch view exposes board / rubric / scoring / mutation panels."""
    p = _SectionCollector()
    p.feed(index_html)
    missing = REQUIRED_EPOCH_IDS - p.all_ids
    assert not missing, f"missing epoch-view panel ids: {sorted(missing)}"


# ---------------------------------------------------------------------------
# Dark mode + palette
# ---------------------------------------------------------------------------


def test_dark_mode_branch_exists(style_css: str) -> None:
    assert "prefers-color-scheme: dark" in style_css


def test_palette_matches_html_report(style_css: str) -> None:
    """The shared palette must include the four canonical colors."""
    for color in ("#2ea043", "#d73a49", "#6e7681", "#bf8700"):
        assert color in style_css, f"palette missing canonical color {color}"


def test_app_js_uses_same_palette(app_js: str) -> None:
    for color in ("#2ea043", "#d73a49", "#6e7681", "#bf8700"):
        assert color in app_js, f"app.js missing canonical color {color}"


# ---------------------------------------------------------------------------
# Bundle size envelope
# ---------------------------------------------------------------------------


def test_bundle_under_size_envelope(
    index_html: str, style_css: str, app_js: str, icons_svg: str
) -> None:
    total = len(index_html) + len(style_css) + len(app_js) + len(icons_svg)
    # 130 KB uncompressed — the envelope grew with the multi-view app
    # (nav rail, Tree / Tournament / Epoch views).
    assert total < 130_000, f"bundle is {total} bytes, exceeds 130_000 envelope"


def test_each_file_is_non_empty() -> None:
    for name in ("index.html", "style.css", "app.js", "icons.svg"):
        path = STATIC_DIR / name
        assert path.exists(), f"missing required file {name}"
        assert path.stat().st_size > 0, f"empty file {name}"


# ---------------------------------------------------------------------------
# Accessibility — minimal floor
# ---------------------------------------------------------------------------


def test_skip_link_present(index_html: str) -> None:
    assert 'class="skip-link"' in index_html
    assert 'href="#main-content"' in index_html


def test_roles_and_aria_labels(index_html: str) -> None:
    # A reasonable floor: at least banner / main / contentinfo /
    # complementary roles on the structural landmarks.
    for role in (
        'role="banner"',
        'role="main"',
        'role="contentinfo"',
        'role="complementary"',
        'role="log"',
    ):
        assert role in index_html, f"missing landmark role {role}"


def test_drill_panel_is_hidden_initially(index_html: str) -> None:
    # The drill side panel must start hidden so the first paint does
    # not flash a half-rendered detail view.
    assert 'aria-hidden="true"' in index_html


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _strip_js_comments(src: str) -> str:
    """Crude but adequate stripper for // and /* */ comments.

    Treats string-literal contents naively; that is fine for the
    purpose here — we only need to determine that no http URL
    appears OUTSIDE a comment.
    """
    out: list[str] = []
    i = 0
    n = len(src)
    in_line = False
    in_block = False
    in_string: str | None = None  # quote char or None
    while i < n:
        ch = src[i]
        nxt = src[i + 1] if i + 1 < n else ""
        if in_line:
            if ch == "\n":
                in_line = False
                out.append(ch)
        elif in_block:
            if ch == "*" and nxt == "/":
                in_block = False
                i += 1
        elif in_string is not None:
            if ch == "\\" and nxt:
                i += 1
            elif ch == in_string:
                in_string = None
            out.append(ch)
        else:
            if ch == "/" and nxt == "/":
                in_line = True
                i += 1
            elif ch == "/" and nxt == "*":
                in_block = True
                i += 1
            elif ch in ('"', "'", "`"):
                in_string = ch
                out.append(ch)
            else:
                out.append(ch)
        i += 1
    return "".join(out)
