"""Structural tests for ``zicato/dashboard/static/`` — the dashboard UI.

These tests do not run the JavaScript. They parse the static HTML and
assert structural invariants the dashboard service relies on:

* No external resource references (no ``http`` URLs, no remote scripts,
  no remote stylesheets, no Google Fonts).
* The expected sections, IDs, and SVG hooks are all present so
  ``app.js`` can find them.
* The total bundle size sits under the envelope (uncompressed total of
  HTML + CSS + JS + icon sprite).
* The environment-view structure is present: the nav rail and the
  matching view containers, plus each view's section ids.
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

    The dashboard frontend was re-architected from a monolithic
    ``app.js`` into ES modules: the thin entry point ``app.js`` plus the
    modules under ``static/js/`` (the core spine, the shared component
    library, and the render layer). The structural tests assert
    properties of the *bundle* — they concatenate every JS file rather
    than reading ``app.js`` alone, so the assertions hold regardless of
    which module a given symbol lives in.
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
    """The concatenated JS bundle — the modular successor to app.js.

    See :func:`_js_bundle_files`. Every ``app.js`` structural assertion
    in this file checks the bundle, so it is agnostic to the ES-module
    split introduced by the dashboard redesign.
    """
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
    "health-section",
    "tournament-section",
    "runs-section",
    "lineage-section",
    "trajectory-section",
    "tournament-bracket-section",
    "tournament-detail-section",
    "heatmap-section",
    "log-section",
    "epoch-overview-section",
    "epoch-board-section",
    "epoch-brief-section",
    "epoch-scoring-section",
    "epoch-mutations-section",
    "epoch-experiment-log-section",
    "epoch-journal-section",
    "epoch-analysis-section",
    "files-changes-section",
    "files-section",
    "files-patches-section",
    "mutations-section",
}

# The five view containers of the multi-view app.
REQUIRED_VIEW_IDS = {
    "view-overview",
    "view-tree",
    "view-tournament",
    "view-epoch",
    "view-files",
}

# The nav rail's five entries.
REQUIRED_NAV_IDS = {
    "nav-overview",
    "nav-tree",
    "nav-tournament",
    "nav-epoch",
    "nav-files",
}

# Epoch-view panel containers app.js renders into.
REQUIRED_EPOCH_IDS = {
    "epoch-overview",
    "epoch-harness",
    "epoch-board",
    "epoch-brief",
    "epoch-scoring",
    "epoch-mutations",
    "epoch-experiment-log",
    "epoch-journal",
    "epoch-analysis",
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
        "health-panel",
        "tournament-bracket",
        "tournament-detail",
        "active-runs",
        "log-tail",
        "drill-panel",
        "drill-title",
        "drill-body",
        "drill-close",
        "dashboard-version",
        "dashboard-port",
        "dashboard-build",
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
    """Overview / Tree / Tournament / Epoch / Files each have a view container."""
    p = _SectionCollector()
    p.feed(index_html)
    missing = REQUIRED_VIEW_IDS - p.all_ids
    assert not missing, f"missing view containers: {sorted(missing)}"


def test_nav_rail_present_with_four_entries(index_html: str) -> None:
    """The nav rail exists and has exactly the five expected entries."""
    p = _SectionCollector()
    p.feed(index_html)
    assert "nav-rail" in p.all_ids, "nav rail container (#nav-rail) missing"
    missing = REQUIRED_NAV_IDS - p.all_ids
    assert not missing, f"missing nav entries: {sorted(missing)}"
    # The nav entries must fragment-route to the five views.
    for frag in ("#/overview", "#/tree", "#/tournament", "#/epoch", "#/files"):
        assert frag in p.nav_hrefs, f"nav rail missing route {frag}"


def test_epoch_view_section_ids_present(index_html: str) -> None:
    """The Epoch view exposes board / brief / scoring / mutation panels."""
    p = _SectionCollector()
    p.feed(index_html)
    missing = REQUIRED_EPOCH_IDS - p.all_ids
    assert not missing, f"missing epoch-view panel ids: {sorted(missing)}"


# ---------------------------------------------------------------------------
# Tournament gauntlet bracket + per-matchup detail
# ---------------------------------------------------------------------------


def test_tournament_bracket_container_present(index_html: str) -> None:
    """The Tournament view renders a gauntlet bracket container.

    The bracket is the spine app.js paints the champion lineage and the
    discarded challengers into; the detail panel holds the per-matchup
    A/B grid and scalar breakdown.
    """
    p = _SectionCollector()
    p.feed(index_html)
    assert "tournament-bracket" in p.all_ids, "bracket container missing"
    assert "tournament-detail" in p.all_ids, "matchup detail container missing"
    # The bracket and detail each live in their own section.
    assert "tournament-bracket-section" in set(p.section_ids)
    assert "tournament-detail-section" in set(p.section_ids)


def test_loop_health_panel_present(index_html: str) -> None:
    """The Overview view carries a prominent loop-health panel."""
    p = _SectionCollector()
    p.feed(index_html)
    assert "health-section" in set(p.section_ids), "loop-health section missing"
    assert "health-panel" in p.all_ids, "loop-health panel container missing"


def test_app_js_targets_environment_api(app_js: str) -> None:
    """app.js codes against the consolidated environment API.

    The environment view is driven by /api/environment (the coalesced
    read), the /events SSE stream, the run-log tail, and the per-matchup
    detail endpoint. Drill-downs use the files and conversation APIs.
    """
    for path in (
        "/api/environment",
        "/events",
        "/api/run-log",
        "/api/tournaments/",
    ):
        assert path in app_js, f"app.js does not reference endpoint {path}"


# ---------------------------------------------------------------------------
# Harmonograf deep-links — Tournament view + active-run cards
# ---------------------------------------------------------------------------


def test_harmonograf_helpers_present(app_js: str) -> None:
    """app.js carries the harmonograf URL-building helpers.

    The base resolver, the run-URL builder, the full link, the compact
    A/B-grid link, and the bracket-node link must all exist.
    """
    for fn in (
        "function harmonografBase(",
        "function harmonografRunUrl(",
        "function harmonografLink(",
        "function harmonografMini(",
        "function harmonografGenLink(",
    ):
        assert fn in app_js, f"app.js missing harmonograf helper: {fn}"


def test_harmonograf_run_id_convention(app_js: str) -> None:
    """A run with no explicit session id resolves via {generation}--{entry}.

    Every board entry under a generation is a run; zicato names it
    deterministically. The deep-link must fall back to that convention
    rather than rendering nothing.
    """
    assert "deriveRunId" in app_js, "app.js missing the run-id derivation helper"
    # The deterministic run-id form: `${gen}--${entry}`.
    assert "${gen}--${entry}" in app_js, "app.js missing the {generation}--{entry} run-id form"
    # The session deep-link path.
    assert "/#/session/" in app_js, "app.js missing the harmonograf session URL form"


def test_harmonograf_link_never_silently_disappears(app_js: str) -> None:
    """The link must render whenever harmonograf_url is set.

    harmonografRunUrl returns the bare base as a last resort and only
    returns null when harmonografBase() is null (no url at all). This
    is the contract that stops the active-run link from vanishing.
    """
    scrubbed = _strip_js_comments(app_js)
    # harmonografRunUrl falls through to `return base` (the bare url).
    idx = scrubbed.find("function harmonografRunUrl(")
    assert idx != -1, "harmonografRunUrl not found"
    body = scrubbed[idx : idx + 400]
    assert "return base" in body, (
        "harmonografRunUrl must fall back to the bare base url so the "
        "link never silently disappears when harmonograf_url is set"
    )


def test_heartbeat_merge_preserves_harmonograf_url(app_js: str) -> None:
    """Heartbeat updates merge rather than replace.

    A heartbeat ping is minimal and omits harmonograf_url; a wholesale
    replace would drop it and kill every deep-link. setHeartbeat must
    merge so the last-known url survives a ping.
    """
    assert "setHeartbeat(" in app_js, "app.js missing the setHeartbeat merge method"
    scrubbed = _strip_js_comments(app_js)
    idx = scrubbed.find("setHeartbeat(hb)")
    assert idx != -1, "setHeartbeat(hb) method body not found"
    body = scrubbed[idx : idx + 200]
    assert "Object.assign" in body, "setHeartbeat must merge (Object.assign), not replace"
    # The SSE heartbeat handler and the heartbeat refresh both route
    # through the merge — no raw `state.heartbeat =` from a ping.
    assert (
        "state.setHeartbeat(JSON.parse(ev.data))" in app_js
    ), "the SSE heartbeat handler must merge via setHeartbeat"


def test_ab_grid_has_harmonograf_trace_links(app_js: str) -> None:
    """The per-matchup A/B grid wires a harmonograf trace per row side.

    Every A/B-grid row is a run twice (champion side + challenger
    side); each side gets its own harmonograf link.
    """
    # The trace column header and the per-side cell class.
    assert "'trace'" in app_js or '"trace"' in app_js, "A/B grid missing the trace column header"
    assert "ab-trace" in app_js, "A/B grid missing the ab-trace cell"
    # Both sides resolve a run-like record for harmonografMini. The
    # render block lives between the A/B-grid header and the scalar
    # breakdown header.
    idx = app_js.find("Per-entry A/B grid")
    assert idx != -1, "A/B grid render block not found"
    block = app_js[idx : idx + 3000]
    assert "harmonografMini(" in block, "A/B grid does not call harmonografMini"
    assert (
        "parentRun" in block and "childRun" in block
    ), "A/B grid must build a parent-side and child-side run record"


def test_bracket_nodes_have_harmonograf_links(app_js: str) -> None:
    """Bracket generation nodes (champion / challenger / live) link out.

    Each generation node in the gauntlet bracket carries a subtle
    harmonograf affordance.
    """
    scrubbed = _strip_js_comments(app_js)
    # The champion node, the discarded-challenger card, and the live
    # card each attach a harmonografGenLink.
    for marker in ("champHg", "loserHg", "liveHg"):
        assert marker in scrubbed, f"bracket node missing harmonograf link: {marker}"
    assert (
        scrubbed.count("harmonografGenLink(") >= 3
    ), "expected harmonografGenLink on all three bracket node kinds"


def test_harmonograf_link_styles_present(style_css: str) -> None:
    """The harmonograf link variants are styled in the dark palette."""
    for cls in (".harmonograf-link", ".harmonograf-mini", ".harmonograf-sup"):
        assert cls in style_css, f"style.css missing harmonograf style: {cls}"


def test_harmonograf_arrow_affordance(app_js: str) -> None:
    """Every harmonograf link is suffixed with the unobtrusive arrow."""
    # The full link, the mini link, and the superscript link all use
    # the up-right arrow as their affordance.
    assert "↗" in app_js, "harmonograf links missing the ↗ affordance"


def test_mock_heartbeat_carries_harmonograf_url(app_js: str) -> None:
    """Mock mode exercises the deep-links.

    The mock heartbeat must carry a harmonograf_url so ?mock=1 renders
    the Tournament-view and active-run links.
    """
    assert "harmonograf_url" in app_js, "mock heartbeat missing harmonograf_url"
    # The mock entry_grid exercises both the explicit-session-id path
    # and the {generation}--{entry} fallback.
    assert (
        "parent_session_id" in app_js
    ), "mock A/B-grid rows should exercise explicit per-side session ids"


# ---------------------------------------------------------------------------
# Time / heartbeat / stale handling — header clock
# ---------------------------------------------------------------------------


def test_robust_iso_parser_present(app_js: str) -> None:
    """app.js carries a robust ISO parser for the heartbeat timestamps.

    heartbeat.json mixes the `Z` suffix and the `+00:00` offset form;
    a bare ``new Date`` mis-parses a zone-less value as local time. The
    parser normalises both and pins zone-less values to UTC.
    """
    assert "function parseIso(" in app_js, "app.js missing the parseIso helper"
    scrubbed = _strip_js_comments(app_js)
    # The header and the stale-badge math route through parseIso.
    assert "parseIso(" in scrubbed


def test_stale_threshold_is_ninety_seconds(app_js: str) -> None:
    """A live heartbeat must not trip the stale badge.

    Stale = (now - last_heartbeat) > 90s. The threshold is a named
    constant so the badge logic is unambiguous.
    """
    assert "STALE_HEARTBEAT_MS" in app_js, "app.js missing the stale threshold constant"
    assert "90_000" in app_js, "stale threshold should be 90s (90_000 ms)"


def test_header_reads_heartbeat_generation_and_round(app_js: str) -> None:
    """The header reads generation_id / round_index off the heartbeat."""
    scrubbed = _strip_js_comments(app_js)
    idx = scrubbed.find("function renderHeader(")
    assert idx != -1, "renderHeader not found"
    body = scrubbed[idx : idx + 1400]
    assert "generation_id" in body, "renderHeader must read heartbeat.generation_id"
    assert "round_index" in body, "renderHeader must read heartbeat.round_index"
    assert "started_at" in body, "renderHeader elapsed clock must use started_at"


# ---------------------------------------------------------------------------
# Footer — wired from /api/health
# ---------------------------------------------------------------------------


def test_footer_wired_from_health(app_js: str) -> None:
    """renderFooter sources version / port / build from /api/health."""
    assert "/api/health" in app_js, "app.js does not reference /api/health"
    scrubbed = _strip_js_comments(app_js)
    idx = scrubbed.find("function renderFooter(")
    assert idx != -1, "renderFooter not found"
    body = scrubbed[idx : idx + 600]
    assert "state.health" in body, "renderFooter must read state.health"


# ---------------------------------------------------------------------------
# Global data loading — the single coalesced environment read
# ---------------------------------------------------------------------------


def test_loads_consolidated_environment_endpoint(app_js: str) -> None:
    """The dashboard reads the whole environment from /api/environment.

    The redesign keeps the single consolidated read: ``loadEnvironment``
    (core/api.js) fetches ``/api/environment`` and ``applyEnvironment``
    folds it into AppState. After the ES-module split ``applyEnvironment``
    is an ``AppState`` method rather than a free function — both forms
    are accepted so the assertion is agnostic to the refactor.
    """
    assert "/api/environment" in app_js, "bundle does not fetch /api/environment"
    assert "function loadEnvironment(" in app_js, "bundle missing loadEnvironment"
    assert (
        "function applyEnvironment(" in app_js or "applyEnvironment(env)" in app_js
    ), "bundle missing applyEnvironment (free function or AppState method)"


def test_refresh_after_event_is_debounced_single_fetch(app_js: str) -> None:
    """A state_change tick coalesces into ONE debounced environment read.

    The polling-storm fix: refreshAfterEvent must NOT fan out to many
    endpoints per event. It debounces (REFRESH_DEBOUNCE_MS) and then
    does a single loadEnvironment(), so a burst of SSE frames costs at
    most one /api/environment fetch per window.
    """
    scrubbed = _strip_js_comments(app_js)
    idx = scrubbed.find("function refreshAfterEvent(")
    assert idx != -1, "refreshAfterEvent not found"
    body = scrubbed[idx : idx + 1200]
    assert "REFRESH_DEBOUNCE_MS" in body, "refreshAfterEvent must debounce"
    assert "loadEnvironment(" in body, "refreshAfterEvent must call loadEnvironment"
    # The old fan-out helpers must be gone.
    assert "loadLiveFeeds" not in scrubbed, "stale loadLiveFeeds fan-out still present"


def test_log_tail_appends_rather_than_full_rerender(app_js: str) -> None:
    """The run-log tail GROWS by appending rows, not by re-rendering.

    appendLogTail() adds only the new rows; a `run_log` SSE frame drives
    an append-only `?after=<cursor>` poll. This is what stops the log
    tail flashing on every event.
    """
    assert "function appendLogTail(" in app_js, "app.js missing appendLogTail"
    assert "?after=" in app_js, "app.js does not use the run-log ?after= cursor"
    scrubbed = _strip_js_comments(app_js)
    # The run_log SSE frame must drive an append, not a full refresh.
    idx = scrubbed.find("'run_log'")
    assert idx != -1, "app.js does not listen for the run_log SSE frame"


def test_mock_state_carries_contract_shapes(app_js: str) -> None:
    """mockSnapshot enriches every cross-view feed in the new shapes.

    ?mock=1 must preview every view: the active tournament, lineage,
    the structured run-log and the active-runs progress meters.
    """
    scrubbed = _strip_js_comments(app_js)
    # The contract keys for the active tournament and lineage.
    assert "parent_generation_id" in scrubbed, "mock active_tournament missing contract key"
    assert "child_generation_id" in scrubbed, "mock active_tournament missing contract key"
    # active-runs progress meter inputs.
    for key in ("progress", "elapsed_seconds", "budget_seconds"):
        assert key in scrubbed, f"mock active_runs missing {key}"
    # The structured run-log.
    assert "run_log" in scrubbed, "mock missing the run_log feed"


# ---------------------------------------------------------------------------
# Empty states + proposer-brief column — compact CSS
# ---------------------------------------------------------------------------


def test_empty_state_is_compact(style_css: str) -> None:
    """An .empty panel is one muted line, not a tall placeholder box."""
    idx = style_css.find(".empty {")
    assert idx != -1, ".empty rule missing from style.css"
    block = style_css[idx : idx + 240]
    # Compact padding, no inflated min-height.
    assert "min-height: 0" in block, ".empty must not inflate to fill a sized parent"


def test_diagram_svg_is_height_capped(style_css: str) -> None:
    """Diagram SVGs are height-capped so an empty chart is not a slab."""
    idx = style_css.find(".diagram svg")
    assert idx != -1, ".diagram svg rule missing"
    block = style_css[idx : idx + 160]
    assert "max-height" in block, ".diagram svg must cap its height"


def test_brief_block_is_centered_column(style_css: str) -> None:
    """The epoch proposer brief renders as a centred max-width column."""
    idx = style_css.find(".epoch-brief .brief-block {")
    assert idx != -1, ".brief-block rule missing"
    block = style_css[idx : idx + 320]
    assert "max-width: 760px" in block, "brief column should cap at ~760px"
    assert (
        "margin-left: auto" in block and "margin-right: auto" in block
    ), "brief column must be centred"


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
    # 330 KB uncompressed. Raised from 270 KB by the dashboard redesign:
    # the monolithic ``app.js`` was re-architected into ES modules — a
    # thin entry point plus the core spine (state / bus / router / api /
    # sse / dom / format / harmonograf), a shared component library and
    # the render layer. The module boundaries add per-file headers,
    # import statements and the documented contracts, costing a few tens
    # of KB; that cost buys the structural no-flash render spine and the
    # zero-collision modular layout. The +10 KB step from 320 KB carries
    # the route-driven Files view: its "What changed" section — a
    # generation picker and a side-by-side split diff of every changed
    # file — plus the shared `diff` component's split-mode CSS. The
    # ``app_js`` fixture concatenates every shipped JS file, so this
    # envelope covers the whole bundle. The dev-only JS test harness
    # under ``static/test/`` is NOT shipped and is excluded. The
    # dashboard is served off disk by the standalone Python service with
    # no network cost; this guard only keeps the vanilla bundle from
    # drifting unboundedly.
    assert total < 330_000, f"bundle is {total} bytes, exceeds 330_000 envelope"


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
# Epoch experiment log / journal / analysis — new render functions
# ---------------------------------------------------------------------------


def test_epoch_experiment_log_render_function_present(app_js: str) -> None:
    """The render layer exports a renderEpochExperimentLog function."""
    assert "function renderEpochExperimentLog(" in app_js


def test_epoch_journal_render_function_present(app_js: str) -> None:
    """The render layer exports a renderEpochJournal function."""
    assert "function renderEpochJournal(" in app_js


def test_epoch_analysis_render_function_present(app_js: str) -> None:
    """The render layer exports a renderEpochAnalysis function."""
    assert "function renderEpochAnalysis(" in app_js


def test_epoch_view_calls_all_new_sub_renderers(app_js: str) -> None:
    """renderEpochView calls all three new sub-renderers."""
    scrubbed = _strip_js_comments(app_js)
    idx = scrubbed.find("function renderEpochView(")
    assert idx != -1, "renderEpochView not found"
    body = scrubbed[idx : idx + 600]
    for fn in ("renderEpochExperimentLog(", "renderEpochJournal(", "renderEpochAnalysis("):
        assert fn in body, f"renderEpochView must call {fn}"


def test_experiment_log_uses_mutation_diff_renderer(app_js: str) -> None:
    """The experiment log reuses the existing renderMutationDiff function."""
    scrubbed = _strip_js_comments(app_js)
    idx = scrubbed.find("function renderEpochExperimentLog(")
    assert idx != -1, "renderEpochExperimentLog not found"
    # The function is several hundred lines; use a large window.
    body = scrubbed[idx : idx + 8000]
    assert (
        "renderMutationDiff(" in body
    ), "renderEpochExperimentLog must reuse renderMutationDiff for patch diffs"


def test_experiment_log_links_to_tournament_view(app_js: str) -> None:
    """Each experiment row links to its tournament via #/tournament/{genId}."""
    scrubbed = _strip_js_comments(app_js)
    idx = scrubbed.find("function renderEpochExperimentLog(")
    assert idx != -1
    body = scrubbed[idx : idx + 8000]
    assert (
        "#/tournament/" in body
    ), "renderEpochExperimentLog must link experiments to #/tournament/{genId}"


def test_journal_uses_minimal_markdown_renderer(app_js: str) -> None:
    """renderEpochJournal renders with renderMinimalMarkdown."""
    scrubbed = _strip_js_comments(app_js)
    idx = scrubbed.find("function renderEpochJournal(")
    assert idx != -1, "renderEpochJournal not found"
    body = scrubbed[idx : idx + 600]
    assert "renderMinimalMarkdown(" in body, "renderEpochJournal must use renderMinimalMarkdown"


def test_analysis_offers_html_link(app_js: str) -> None:
    """renderEpochAnalysis links the analysis.html report when available."""
    scrubbed = _strip_js_comments(app_js)
    idx = scrubbed.find("function renderEpochAnalysis(")
    assert idx != -1, "renderEpochAnalysis not found"
    body = scrubbed[idx : idx + 1200]
    assert (
        "analysis_html_available" in body
    ), "renderEpochAnalysis must check analysis_html_available"
    assert "analysis.html" in body, "renderEpochAnalysis must link to the analysis.html endpoint"


def test_mock_epoch_carries_experiment_log_fields(app_js: str) -> None:
    """mockSnapshot includes experiments, journal, and analysis_md on the epoch."""
    scrubbed = _strip_js_comments(app_js)
    for key in ("experiments", "journal", "analysis_md", "analysis_html_available"):
        assert key in scrubbed, f"mock epoch missing field: {key}"


# ---------------------------------------------------------------------------
# Files view — route-driven, side-by-side changed-files diff
# ---------------------------------------------------------------------------


def test_files_view_is_route_driven(app_js: str) -> None:
    """The Files view resolves the route to a default and is reachable.

    Bare ``#/files`` must not fall through to Overview: the bundle
    carries the ``applyFilesRoute`` route entry point, which resolves a
    default epoch + generation and canonicalises the hash.
    """
    scrubbed = _strip_js_comments(app_js)
    assert (
        "function applyFilesRoute(" in scrubbed
    ), "bundle missing applyFilesRoute — the Files view route entry point"
    # The router branch must consume #/files explicitly so the
    # epoch/generation segments are real route params, not a drill.
    assert "'files'" in scrubbed, "applyRoute must branch on the files view"


def test_files_view_renders_split_diff_of_changes(app_js: str) -> None:
    """The Files view shows a side-by-side (split) diff of what changed.

    It fetches the per-generation diff endpoint and renders each changed
    file through the shared ``diff`` component in ``mode:'split'``.
    """
    scrubbed = _strip_js_comments(app_js)
    assert "/diff" in scrubbed, "Files view must fetch the per-generation /diff endpoint"
    assert "renderFilesChanges" in scrubbed, "Files view missing the changes renderer"
    assert (
        "mode: 'split'" in scrubbed or 'mode: "split"' in scrubbed
    ), "the changed-files diff must render in split mode"


def test_files_changes_section_in_required_sections() -> None:
    """The Files-view 'What changed' section is in REQUIRED_SECTIONS."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_test_dashboard_ui_files_check",
        Path(__file__),
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    assert "files-changes-section" in mod.REQUIRED_SECTIONS


def test_epoch_experiment_log_section_in_required_sections() -> None:
    """The three new epoch sections are in REQUIRED_SECTIONS."""
    # This is a code-level assertion: the test file's own constant must
    # include the new sections (verified by running the full suite).
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_test_dashboard_ui_check",
        Path(__file__),
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    for sect in (
        "epoch-experiment-log-section",
        "epoch-journal-section",
        "epoch-analysis-section",
    ):
        assert sect in mod.REQUIRED_SECTIONS, f"REQUIRED_SECTIONS missing {sect}"


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
