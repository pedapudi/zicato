"""Structural tests for ``zicato/dashboard/static/`` — the dashboard UI.

These tests do not run the JavaScript. They parse the static HTML and
assert structural invariants the dashboard service relies on:

* No external resource references (no ``http`` URLs, no remote scripts,
  no remote stylesheets, no Google Fonts).
* The expected sections, IDs, and SVG hooks are all present so
  ``app.js`` can find them.
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
    point ``app.js`` plus the modules under ``static/js/`` (the core
    spine, the shared component library, and the per-level view
    modules). The structural tests assert properties of the *bundle* —
    they concatenate every JS file rather than reading ``app.js`` alone,
    so the assertions hold regardless of which module a given symbol
    lives in.
    """
    files = [STATIC_DIR / "app.js"]
    js_dir = STATIC_DIR / "js"
    if js_dir.is_dir():
        # Deterministic order; the test bundle is order-insensitive but a
        # stable order keeps any failure message reproducible.
        files += sorted(js_dir.rglob("*.js"))
    return files


@pytest.fixture(scope="module")
def app_js() -> str:
    """The concatenated JS bundle — the modular successor to app.js."""
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
# Expected DOM structure — the phase-0 shell
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


# The five L0..L4 view containers + the sidebar-driven Files view the
# shell must keep wired so app.js can switch between them without
# re-fetching the HTML. (Search is no longer a route — it is an
# always-visible sidebar input that filters inline.)
REQUIRED_PHASE0_VIEW_IDS = {
    "phase0-view-workspace",
    "phase0-view-epoch",
    "phase0-view-generation",
    "phase0-view-round",
    "phase0-view-run",
    "phase0-view-files",
}

# The header / footer chrome containers app.js paints into.
REQUIRED_CHROME_IDS = {
    "header-bar",
    "footer-bar",
    "epoch-id",
    "generation-id",
    "round-id",
    "elapsed",
    "health-badge",
    "mock-badge",
    "dashboard-version",
    "dashboard-port",
    "dashboard-build",
    "drill-panel",
    "drill-title",
    "drill-body",
    "drill-close",
}

REQUIRED_SHELL_IDS = {
    "phase0-shell",
    # The clean-slate navigation rework dropped the sidebar entirely;
    # everything global lives in the top bar. ``#phase0-topbar`` is the
    # single live slot the shell paints branding / breadcrumb / ⌘K
    # button / status pill / Files icon / Harmonograf icon into.
    "phase0-topbar",
    # The status pill expands a small dropdown panel anchored just
    # below the top bar; always present in the DOM (hidden).
    "phase0-status-dropdown",
    # The ⌘K command palette overlay + its input + results list.
    "phase0-palette-overlay",
    "phase0-palette-input",
    "phase0-palette-results",
    # The L0 Recent Decisions card slot.
    "phase0-workspace-recent",
}

REQUIRED_IDS = REQUIRED_CHROME_IDS | REQUIRED_SHELL_IDS | REQUIRED_PHASE0_VIEW_IDS


def test_required_element_ids_present(index_html: str) -> None:
    p = _SectionCollector()
    p.feed(index_html)
    missing = REQUIRED_IDS - p.all_ids
    assert not missing, f"missing required element ids: {sorted(missing)}"


def test_index_loads_local_css_and_js(index_html: str) -> None:
    assert 'href="style.css"' in index_html, "style.css <link> missing"
    # The v2 feature-flag bootstrap replaced the static <script src="app.js">
    # with an inline loader that picks one local ES-module entry — app.js
    # (v1, default) or app2.js (v2) — based on the ?ui flag. Assert the
    # bootstrap references both local entries and loads them as modules.
    assert "'app.js'" in index_html, "v1 entry app.js missing from the bootstrap"
    assert "'app2.js'" in index_html, "v2 entry app2.js missing from the bootstrap"
    assert "'module'" in index_html, "the entry must load as an ES module"


def test_phase0_view_containers_present(index_html: str) -> None:
    """Each L0..L4 + sidebar view has its container wired."""
    p = _SectionCollector()
    p.feed(index_html)
    missing = REQUIRED_PHASE0_VIEW_IDS - p.all_ids
    assert not missing, f"missing phase0 view containers: {sorted(missing)}"


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
    drifting unboundedly. After the legacy 5-tab shell + its render
    layer (``render.js`` ~270 KB) were torn out the bundle dropped
    sharply — the cap below was set to the measured size + ~30 KB of
    headroom for natural growth.
    """
    total = len(index_html) + len(style_css) + len(app_js) + len(icons_svg)
    # Raised from 270 KB by the dashboard redesign:
    # the monolithic ``app.js`` was re-architected into ES modules — a
    # thin entry point plus the core spine (state / bus / router / api /
    # sse / dom / format / harmonograf), a shared component library and
    # the render layer. The module boundaries add per-file headers,
    # import statements and the documented contracts, costing a few tens
    # of KB; that cost buys the structural no-flash render spine and the
    # zero-collision modular layout. Raised again by the route-driven
    # Files view (its "What changed" section — a generation picker and a
    # side-by-side split diff of every changed file — plus the shared
    # `diff` component's split-mode CSS), the Overview environment-home
    # rebuild (identity / live-activity / epochs / recent-experiments
    # panels and the score-trajectory chart) and the Epoch-view redesign
    # (the experiment narrative renders each experiment as a four-beat
    # card — description / hypothesis / change / outcome — with a
    # coloured-accent layout, partly offset by deleting the prior flat
    # experiment-log markup). Raised again by the dashboard-hardening
    # integration, which folds in four independent dashboard fixes:
    #   * the Files-view live-refresh fix — the generation picker routes
    #     through the keyed reconcile spine (so a generation created
    #     mid-run appears without a reload and without DOM churn) via a
    #     flattened picker-row model;
    #   * the Epoch-view journal renderer — the journal markdown is
    #     parsed into a labelled round-by-round timeline instead of being
    #     emitted verbatim (which had leaked literal ``**`` markers onto
    #     the page), plus the ``inlineMarkdown`` splitter gaining
    #     `**bold**` support and the timeline's CSS;
    #   * the Overview score-trajectory axis labels (offset by deleting
    #     the duplicate Tree-view trajectory chart);
    #   * the Tournament view's scroll / conversation-diff fixes and the
    #     champion/challenger terminology rename.
    # The single-branch caps measured each fix in isolation; the
    # fully integrated bundle was re-measured directly and the cap
    # below set comfortably above it. The ``app_js`` fixture
    # concatenates every shipped JS file, so this envelope covers
    # the whole bundle. The dev-only JS test harness
    # under ``static/test/`` is NOT shipped and is excluded. The
    # dashboard is served off disk by the standalone Python service with
    # no network cost; this guard only keeps the vanilla bundle from
    # drifting unboundedly.
    # Raised again by the dashboard IA / analysis integration, which
    # folds in three further dashboard fixes on top of the redesign:
    #   * the Files-view folding split-diff — the "What changed" diff
    #     gained a folding split-diff renderer (foldDiffOps +
    #     renderFoldingSplitDiff: long unchanged runs collapse to a
    #     click-to-expand marker so a small change is not buried under a
    #     wall of identical source), the mutation-site viewer and
    #     file-content pane route through the keyed reconcile spine
    #     (swapIfChanged) so an SSE repaint keeps their horizontal scroll
    #     position, plus Files-view soft-wrap / working-scroll CSS;
    #   * the completed-tournament A/B matchup grid + scalar breakdown
    #     (a new /api/matchup-grid endpoint and its render path);
    #   * the Epoch-view / Overview information-architecture redesign —
    #     the Epoch view's "Experiment log" and "Journal" panels are
    #     merged into one chronological "Experiments" section with
    #     progressive disclosure, and the Overview epochs table gains a
    #     per-epoch goal column fed by a new `/api/environment.epochs`
    #     summary.
    # The single-branch caps (410 KB / 385 KB) each measured one fix in
    # isolation; the fully integrated bundle was re-measured directly
    # (index.html + style.css + icons.svg + the concatenated JS bundle)
    # at 399,681 bytes. A 440 KB cap leaves ~40 KB of headroom for
    # incidental drift without re-licensing every minor edit.
    # Raised again by the dashboard-refresh / fast-mode / paper-style
    # integration, which folds in three further surfaces:
    #   * the renderAll-level digest gate (a no-op SSE tick now yields
    #     zero DOM writes), the renderHeader/renderFooter rewrites onto
    #     patchText/patchClass, and the matchup-detail + conversation-view
    #     split onto key + populate + swapIfChanged;
    #   * the fast-mode champion-side `cached` pill (a tiny addition in
    #     renderBoardSide and the cached -> done bucket in state_reader
    #     + shared.js);
    #   * the ACM-style paper analysis report — a substantial addition
    #     under src/zicato/analyzer/ that does not ship in the bundle, but
    #     the inline-fragment epoch view path (analysis_html_inline +
    #     .analysis-paper-card) and the .paper CSS rules do.
    # The single-branch caps (421,859 / 403,909, the latter shipped
    # alone) each measured one fix in isolation; the fully integrated
    # bundle was re-measured directly at 423,713 bytes. A 460 KB cap
    # leaves ~36 KB of headroom for incidental drift.
    # Raised again by the phase-0 dashboard redesign (task #181): the
    # bundle now ships a second, level-aligned shell behind the default
    # entry path while keeping the legacy 5-tab UI reachable behind
    # ``?legacy=1``. Six new view modules (workspace / epoch /
    # generation / round / run + the breadcrumb & sidebar shell + a
    # phase-0 router) and their scoped ``phase0-*`` CSS land alongside
    # the existing modules. The single-branch bundle measured directly
    # at ~501 KB; a 540 KB cap leaves ~40 KB of headroom for incidental
    # drift while phase-1 lights up the stubbed sections (per-judge
    # data, contract-diff polish, transcript wiring).
    # Reset by the integration-wave-3 squash. The legacy 5-tab shell +
    # its render layer (the ~270 KB ``render.js`` blob), the
    # shell-picker plumbing, and the now-orphaned ``refresh.test.mjs`` /
    # ``render.test.mjs`` / ``files.test.mjs`` JS tests are gone — a
    # very large net reduction. The visual-design pass adds the design
    # system on top (seven component modules under
    # ``static/js/components/`` and the rewritten phase-0 view modules
    # that compose them; the ``components.css`` / ``tokens.css`` sheets
    # ship under ``static/css/`` linked alongside ``style.css`` but are
    # not part of the four bundle fixtures, so this envelope only covers
    # the JS + ``index.html`` + ``style.css`` + ``icons.svg`` quartet).
    # The phase-1.5 cleanup lands the shared
    # ``core/hypothesis_block.js`` module rendered from L1 (Recent
    # experiments, compact) and L2 (full), the L4 expectation outcomes
    # table renderer, and the L4 run-header tiles + their two cache
    # fetchers. The fully integrated bundle was re-measured directly at
    # 307,717 bytes; a 340 KB cap leaves ~32 KB of headroom for
    # incidental drift.
    # Reset by the integration-wave-4 squash, which lands three large
    # frontend features on top of the visual-design baseline:
    #   * L1 redesign — branch nodes on the spine, full-width experiment
    #     cards, and a rendered analysis-report card at the foot of the
    #     epoch view (``components/spine.js`` + ``views/phase0_epoch.js``
    #     gain ~500 lines together; ``components.css`` picks up the
    #     spine-branch + analysis-report selectors).
    #   * Sidebar inline search — replaces the dedicated search page with
    #     a search input + results panel in the left rail
    #     (``views/phase0_sidebar_search.js`` is new; ``app.js`` /
    #     ``phase0_router.js`` lose the legacy ``#/search`` route).
    #   * L4 conversation diff — a compare picker and side-by-side
    #     transcript renderer on the run view (``views/phase0_run.js``
    #     gains ~700 lines).
    # The fully integrated bundle was re-measured directly at 349,957
    # bytes; a 380 KB cap leaves ~30 KB of headroom for incidental drift
    # while the three features settle.
    # Raised by the integration-wave-6 squash, which lands four further
    # frontend features on top of the wave-4 baseline (the harmonograf
    # self-host work in the same wave is backend-only and does not touch
    # the bundle):
    #   * Sidebar redesign — three sectioned cards with eyebrow icons,
    #     hairline dividers, and a live indicator in the live-activity
    #     header (``views/phase0_shell.js`` + new
    #     ``components/sidebar_section.js`` + the ``phase0-sidebar-*``
    #     selectors in ``components.css``).
    #   * Spine SVG connectors — the spine is now an SVG canvas with
    #     bezier connectors landing on actual dot centers
    #     (``components/spine.js`` gains a measure-then-paint pass).
    #   * L2 generation redesign — hero verdict + alignment-vs-outcome
    #     panel + per-entry vs-champion deltas, dropping the redundant
    #     bottom verdict tile (``views/phase0_generation.js`` gains
    #     ~600 lines and ``components.css`` picks up the L2 layout
    #     selectors).
    #   * Harmonograf link call sites restored across L0/L1/L3/L4
    #     (small additions to the four phase-0 view modules; the
    #     helper itself was already in the bundle).
    # The fully integrated bundle was re-measured directly at 397,325
    # bytes; a 430 KB cap leaves ~33 KB of headroom for incidental drift
    # while the four features settle.
    # Raised by the integration-wave-7 squash, which lands six branches on
    # top of the wave-6 baseline (one is backend-only, the other five are
    # frontend):
    #   * L4 cold-deeplink rerender fix — adds harmonograf_url to the
    #     header digest so cold deep-links rehydrate
    #     (``views/phase0_run.js`` digest helper).
    #   * L4 default-compare-to-parent — the compare picker defaults to
    #     the parent generation with a per-user override map
    #     (``views/phase0_run.js`` gains ``defaultCompareGenFor`` +
    #     ``_compareUserOverride``).
    #   * L2 compare picker — side-by-side compare picker with stable
    #     select + URL hash sync (``views/phase0_generation.js`` gains
    #     ~1100 lines; ``phase0_router.js`` + ``components.css`` pick up
    #     the L2 compare-grid selectors).
    #   * L0 Live Activity dedup — drops the redundant in-content card,
    #     adds a "Workspace at a glance" tile strip, and grows the cross-
    #     epoch trend sparkline (``views/phase0_workspace.js`` +
    #     ``components.css``).
    #   * Clean-slate navigation — drops the sidebar entirely; top bar v2
    #     with brand + breadcrumb + ⌘K palette + status pill replaces it
    #     (``views/phase0_shell.js`` rewrite, new
    #     ``components/command_palette.js`` +
    #     ``components/status_pill.js`` +
    #     ``components/status_pill_dropdown.js``; ``views/phase0_workspace.js``
    #     gains the Recent Decisions card).
    # The fully integrated bundle was re-measured directly at 435,521
    # bytes; a 470 KB cap leaves ~30 KB of headroom for incidental drift
    # while the five features settle.
    # Raised by the decision-centric dashboard redesign. Seven new
    # components (gate ladder, diverging bars, scalar waterfall, verdict
    # glyph, scalar band, lineage ribbon, loop-health banner) plus the
    # five rewired views (L0 workspace, L1 epoch, L2 generation, L3
    # decision view, the shell live rail + L4) and their scoped CSS.
    # The fully integrated bundle was re-measured directly at 505,027
    # bytes; a 540 KB cap leaves ~35 KB of headroom for incidental drift.
    # Raised again by the ground-up v2 dashboard rewrite (DASHBOARD-V2),
    # which ships behind a feature flag alongside v1 until cutover: the
    # v2 foundation (shell/router/spine + the dense primitives under
    # js/v2/** and css/v2/**) lands first, the v2 views follow. Foundation
    # bundle measured 566,705 bytes. The v2 view wave (overview / bench /
    # epoch + report / experiment / run, with scoped CSS) then landed:
    # v1 and v2 ship side-by-side behind the flag, so the bundle carries
    # BOTH until cutover. Fully integrated bundle measured 696,388 bytes;
    # a 730 KB cap holds it. At v1 removal/cutover this drops sharply and
    # the cap is reset.
    # Raised by the visual rebuild (Tufte slopegraph tournament, the
    # small-multiples Bench + boardCell, the 3-theme system, the
    # tournament view) — still side-by-side with v1 behind the flag.
    # Measured 759,830 bytes; an 800 KB cap held it.
    # EXPLORATION PHASE: four complete, parallel dashboard redesign
    # variants (A Mission-Control / B Editorial / C Causal-Flow / D
    # Tufte) now ship side-by-side behind ?ui=A|B|C|D so the operator can
    # interact with all four and pick one. That roughly doubles the
    # concatenated bundle (measured 1,121,177 bytes).
    # ENRICHMENT WAVE: each variant gained candidate-lifecycle, board-field,
    # per-board scoring drill-down, and tournament-style match-up
    # visualizations (each in its own diagrammatic idiom). Measured
    # 1,337,956 bytes under a 1,400 KB cap.
    # SYNTHESIS WAVE (round 2): three new variants E (Atlas), F (Current),
    # G (Bridge) combine the best parts of A–D — A's navigation, C's
    # lifecycle/causal-flow, D's data-viz, B/D theming — each self-contained
    # and digest-gated. Seven variants + v1/v2 now ship side-by-side behind
    # ?ui= so the operator can compare and pick. Measured 1,813,130 bytes.
    # A 1,900 KB cap holds the (deliberately temporary) exploration; once a
    # variant is chosen, all the others + v1/v2 are deleted and the envelope
    # drops sharply and is reset.
    assert total < 1_900_000, f"bundle is {total} bytes, exceeds 1_900_000 envelope"


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
