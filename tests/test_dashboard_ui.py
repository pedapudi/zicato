"""Structural tests for ``zicato/dashboard/static/`` — the dashboard UI.

These tests do not run the JavaScript. They parse the static HTML and
assert structural invariants the dashboard service relies on:

* No external resource references (no ``http`` URLs, no remote scripts,
  no remote stylesheets, no Google Fonts).
* The Variant-T mount point + the entry bootstrap are present so
  ``app_T.js`` can find them.
* The total bundle size sits under the envelope (uncompressed total of
  HTML + CSS + JS + icon sprite).
* The dark-mode media query exists in the CSS.

These tests are pure parsing — no headless browser, no JS engine. They
are the bare floor that protects the contract between the dashboard
service and the static bundle.
"""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

import pytest

# Locate the static directory via the installed ``zicato.dashboard``
# package rather than a path walk from this test file — the package
# moved under a ``src/`` root and resolving through the import keeps
# this test layout-agnostic.
import zicato.dashboard as _dashboard_pkg  # noqa: E402

STATIC_DIR = Path(_dashboard_pkg.__file__).resolve().parent / "static"


@pytest.fixture(scope="module")
def index_html() -> str:
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def style_css() -> str:
    return (STATIC_DIR / "style.css").read_text(encoding="utf-8")


def _js_bundle_files() -> list[Path]:
    """Every JS file in the shipped bundle.

    The dashboard frontend is a modular ES-module app: the thin entry
    point ``app_T.js`` (Variant T, the sole shipping UI) plus the modules
    under ``static/js/`` (the core spine and the Variant-T view modules).
    The structural tests assert properties of the *bundle* — they
    concatenate every JS file rather than reading ``app_T.js`` alone, so
    the assertions hold regardless of which module a given symbol lives
    in.
    """
    files = [STATIC_DIR / "app_T.js"]
    js_dir = STATIC_DIR / "js"
    if js_dir.is_dir():
        # Deterministic order; the test bundle is order-insensitive but a
        # stable order keeps any failure message reproducible.
        files += sorted(js_dir.rglob("*.js"))
    return files


@pytest.fixture(scope="module")
def app_js() -> str:
    """The concatenated JS bundle — Variant T's entry plus js/ modules."""
    return "\n".join(p.read_text(encoding="utf-8") for p in _js_bundle_files())


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
    """The JS bundle must only talk to relative paths under /api and /events.

    Two literals are NOT network data fetches and are allowed:

    * The W3C SVG namespace literal (``http://www.w3.org/2000/svg``) is
      the XML-namespace URI required by ``createElementNS``.
    * The Google Fonts stylesheet (``https://fonts.googleapis.com/css2``)
      is the single external dependency the Variant-T brief permits —
      fonts only, injected with ``display=swap`` and system fallbacks, so
      a slow font never blocks paint. No application data crosses it.
    """
    # Strip comments first; then strip the permitted literals.
    scrubbed = _strip_js_comments(app_js)
    scrubbed = scrubbed.replace("http://www.w3.org/2000/svg", "")
    scrubbed = scrubbed.replace("https://fonts.googleapis.com/css2", "")
    for needle in ("http://", "https://"):
        assert (
            needle not in scrubbed
        ), f"the JS bundle still references {needle} outside comments / permitted literals"


# ---------------------------------------------------------------------------
# Expected DOM structure — the Variant-T mount
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


# Variant T (the sole shipping UI) paints its entire shell at runtime
# into a single host element. The static page only has to provide that
# mount point + the skip link; everything else is rendered by app_T.js.
REQUIRED_IDS = {
    "variant-root",
}


def test_required_element_ids_present(index_html: str) -> None:
    p = _SectionCollector()
    p.feed(index_html)
    missing = REQUIRED_IDS - p.all_ids
    assert not missing, f"missing required element ids: {sorted(missing)}"


def test_index_loads_local_css_and_js(index_html: str) -> None:
    assert 'href="style.css"' in index_html, "style.css <link> missing"
    # Variant T is the only UI. The bootstrap loads its entry point as a
    # local ES module; there is no ?ui branching or fallback shell.
    assert "'app_T.js'" in index_html, "Variant-T entry app_T.js missing from the bootstrap"
    assert "'module'" in index_html, "the entry must load as an ES module"


def test_variant_t_mount_present(index_html: str) -> None:
    """The single Variant-T host element is wired into the page."""
    p = _SectionCollector()
    p.feed(index_html)
    assert "variant-root" in p.all_ids, "missing #variant-root mount for Variant T"


# ---------------------------------------------------------------------------
# Dark mode + palette
# ---------------------------------------------------------------------------


def test_dark_mode_branch_exists(style_css: str) -> None:
    assert "prefers-color-scheme: dark" in style_css


def test_palette_matches_html_report(style_css: str) -> None:
    """The shared palette must include the four canonical colors."""
    for color in ("#2ea043", "#d73a49", "#6e7681", "#bf8700"):
        assert color in style_css, f"palette missing canonical color {color}"


# ---------------------------------------------------------------------------
# Bundle size envelope
# ---------------------------------------------------------------------------


def test_bundle_under_size_envelope(
    index_html: str, style_css: str, app_js: str, icons_svg: str
) -> None:
    """The bundle stays under its uncompressed envelope.

    The dashboard is served off disk by the standalone Python service
    with no network cost; this guard only keeps the vanilla bundle from
    drifting unboundedly. The ``app_js`` fixture concatenates every
    shipped JS file (the Variant-T entry ``app_T.js`` + the modules under
    ``static/js/``); the dev-only JS test harness under ``static/test/``
    is NOT shipped and is excluded.
    """
    total = len(index_html) + len(style_css) + len(app_js) + len(icons_svg)
    # The dashboard converged on Variant T (Console IV). The retired v1
    # (phase0 shell) and v2 (Notebook/Bench) UIs — and the bake-off field
    # A–W before them — were removed from `main` and archived at the git
    # tags `dashboard-v1-v2-archive-2026-06-02` and
    # `dashboard-bakeoff-2026-06-01` respectively, so the served bundle
    # dropped sharply to Variant T alone. The Variant-T feature set has since
    # grown (per-structure epoch overviews, live trackers, the swiss/elim
    # standings-bump + mini-bracket visualizations). Integration wave 8 adds the
    # match-grouped live "what's running" block, the tree live-activity pulse,
    # and the elim generations-across-rounds flow (a new svg renderer + its CSS),
    # which push the served total just past the prior 640 KB line; the envelope
    # is raised to 680 KB to leave headroom for continued Variant-T iteration.
    # The Console-IV de-chartjunk wave then turns the boxed widgets into
    # data-graphics: it ADDS three in-language SVG renderers (the gauntlet
    # `duelFlow`, the loss-floor `waterfall`, the champion `reignGantt`) + their
    # CSS and enhances `elimFlow` into a bracket-as-flow with match convergences —
    # net +~11 KB even after RETIRING the seat/box `elimBracket` renderer + the
    # boxed champion-banner / match-card markup + their CSS. The envelope is
    # raised to 720 KB to cover the added graphics with headroom.
    #
    # B2 then lands the tournament-builder FRONTEND — a self-contained four-pane
    # view (left-rail sections · center controls · live preview reusing the
    # svg.js per-structure figures · a drag-resizable chat-copilot pane) plus its
    # supporting modules (REST client, builder metadata + preview schematic, an
    # accessible info popover, the SSE chat reader) and its scoped CSS. That is a
    # whole new interactive surface (~66 KB of view + module + CSS), so the
    # envelope is raised to 820 KB to cover it with headroom for B3's re-homing.
    assert total < 820_000, f"bundle is {total} bytes, exceeds 820_000 envelope"


def test_each_file_is_non_empty() -> None:
    for name in ("index.html", "style.css", "app_T.js", "icons.svg"):
        path = STATIC_DIR / name
        assert path.exists(), f"missing required file {name}"
        assert path.stat().st_size > 0, f"empty file {name}"


# ---------------------------------------------------------------------------
# Accessibility — minimal floor
# ---------------------------------------------------------------------------


def test_skip_link_present(index_html: str) -> None:
    assert 'class="skip-link"' in index_html
    assert 'href="#main-content"' in index_html


def test_skip_link_targets_main_content(index_html: str) -> None:
    # Variant T paints its own landmark roles (banner / main /
    # contentinfo) at runtime into ``#variant-root``; the static page
    # only needs to provide the skip-link target the shell renders.
    assert 'href="#main-content"' in index_html


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
