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

    These literals are NOT network data fetches and are allowed:

    * The W3C SVG namespace literal (``http://www.w3.org/2000/svg``) is
      the XML-namespace URI required by ``createElementNS``.
    * The Google Fonts stylesheet (``https://fonts.googleapis.com/css2``)
      is the single external dependency the Variant-T brief permits —
      fonts only, injected with ``display=swap`` and system fallbacks, so
      a slow font never blocks paint. No application data crosses it.
    * The two Google-Fonts ``<link rel="preconnect">`` origins
      (``https://fonts.googleapis.com`` and ``https://fonts.gstatic.com``)
      are part of that same fonts-only dependency — they only warm the
      connection for the css2 request and the woff2 host, no data fetch.
    """
    # Strip comments first; then strip the permitted literals. Strip the css2
    # stylesheet URL before the bare preconnect origin so the longer match wins.
    scrubbed = _strip_js_comments(app_js)
    scrubbed = scrubbed.replace("http://www.w3.org/2000/svg", "")
    scrubbed = scrubbed.replace("https://fonts.googleapis.com/css2", "")
    scrubbed = scrubbed.replace("https://fonts.googleapis.com", "")
    scrubbed = scrubbed.replace("https://fonts.gstatic.com", "")
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
    #
    # LIVE PROJECTED STANDINGS then adds the projected-standing treatment across
    # every structure + viz level: the projected overlay + per-structure re-rank
    # in the live model, the dict-bug fix, the projected render in the funnel /
    # swiss ladder / elim flow / standings table / live hero / candidate headline,
    # the cross-round projected fallback, the digest quantization, and the
    # theme-derived projected tokens + treatment CSS — ~22 KB. The envelope is
    # raised to 880 KB to cover it with headroom for continued iteration.
    #
    # The TOURNAMENT-VIZ REDESIGN then ports the operator-selected study designs
    # into the live Console: four new in-language svg builders (the racing
    # `racingScalarTrack`, the gauntlet `gauntletFieldBars`, the `elimRadial`
    # bracket, the candidate `radarSilhouette`) + the orthogonal-pipe `elimFlow`
    # combo, their wiring across structure.js / candidate.js / the live hero, and
    # the live-protocol coverage — ~70 KB of real new data-graphics. The envelope
    # is raised to 980 KB to cover the redesign with headroom for iteration.
    #
    # The LIVE-RENDERING pass then makes every structure figure responsive
    # (aspect-locked scale-to-width), renders each racing rung's FULL field (union
    # lane source) with a no-scalar spread, names the radar axes, reorganizes the
    # candidate dossier to the study layout, and renders the in-flight rung instead
    # of an empty — adding the full-field/responsive logic + the dossier grid. The
    # envelope is raised to 1.02 MB to cover it with headroom.
    #
    # The CROSS-EPOCH META-LOOP LEDGER (tournament-viz study opt 7) then adds the
    # composed `metaLoopLedger` svg builder + its digest — the held floor
    # staircase over effort-proportional bands + the contract-component heatstrip
    # (incl. the proposer* column the contract-diff omits) — wired as the home
    # view's primary cross-epoch overview. ~11 KB of new data-graphics; the
    # envelope is raised to 1.06 MB to cover it with headroom.
    #
    # The ledger zone-A LEGIBILITY pass then de-crowds the contract-change chip
    # row — a COMPACT chip label (headline + "+N" overflow, the full change-set
    # moved to the chip hovercard) plus a variable-WIDTH de-collide pass so
    # adjacent roll boundaries never overlap/clip — and haloes the floor value
    # labels (CSS-only, not in this counted bundle). The de-collide logic is net
    # ~2 KB; the prior 1.06 MB line already sat ~2 KB under it, so the envelope is
    # nudged to 1.062 MB to clear the de-collide pass with a thin margin.
    #
    # The CONTINUOUS PER-ENTRY SCORE surface (#18) then adds the per-board score
    # column to the candidate dumbbell (a 0→1 mini-bar + readout + the P/R tag),
    # the score + precision/recall columns to the board breakdown table, the
    # per-generation mean-score caption, three shared ui.js helpers (prText /
    # metricsDigest / scoreFmt), and their scoped score CSS — ~5 KB of new
    # surface. The envelope is raised to 1.072 MB to cover it with headroom.
    #
    # The SCORING-PROVENANCE decomposition (#19 phase 4) then adds the gate
    # panel's scalar-provenance block — per-side rows naming which transform /
    # plugin produced the pass term + drift component (parsed from the recorded
    # provenance tokens), a FIRST-CLASS fail-open caution banner + row for a
    # plugin that degraded silently, the decomposition digest fold, and the
    # scoped decomposition CSS — ~3 KB of new surface (back-compat: a pre-#19 /
    # built-in round renders nothing new). The envelope is raised to 1.080 MB to
    # cover it with headroom.
    #
    # The IN-FLIGHT ROUND surface (#16 second half) then makes a NEW round that
    # is still proposing/applying its field show as its OWN round on the
    # champion-spine timeline (an `appendInflightRound` overlay derived from the
    # live envelope's `field_status`) instead of being folded under the prior
    # settled round, with a LIVE badge + an incrementing "N proposed · M applied"
    # banner + per-challenger proposing/applied/rejected chip states — the round
    # model + the round-timeline renderer + the wiring through the epoch / round
    # views + the scoped in-flight CSS, ~6 KB of new surface (back-compat: an
    # epoch with no live proposing round renders byte-identically). The envelope
    # is raised to 1.092 MB to cover it with headroom.
    #
    # The FEATURE-WAVE INTEGRATION then lands every dashboard surface on one
    # branch at once: the double-elim demotion line-routing (#34) and the
    # in-flight LIVE-badge / status-chip round timeline (#31) both grew the
    # shared svg.js + Variant-T view modules, and their union sits ~1.7 KB above
    # the prior 1.092 MB line (no shared widget was duplicated — both renderers
    # coexist; the frontend suite asserts both behaviours). The envelope is
    # raised to 1.10 MB to cover the combined surface with headroom.
    #
    # The SEQ-DRIVEN LIVENESS + PRINCIPLED RENDER GATE (the evidence-cockpit
    # render-discipline backbone) then keys liveness on the orchestrator
    # progress `seq` now carried on the SSE frames + the heartbeat, instead of
    # a heartbeat timestamp: a progress cursor on AppState (noteProgress —
    # advance / no-op / rollover), a seq no-op-skip gate in core/sse.js (a
    # non-advancing state_change writes ZERO DOM), a four-state run verdict in
    # livestatus.js (LIVE / STALLED / SETTLED / DEAD) with the legacy
    # timestamp degrade when no seq is present, and the chrome `dt-run-state`
    # pill in shell.js — ~10 KB of new spine (back-compat: a seq-less frame is
    # byte-identical to the prior always-refresh path). The envelope is raised
    # to 1.11 MB to cover it with headroom.
    #
    # The UNIFIED DECISION-STATE TAXONOMY + overrideChip primitive (the
    # evidence-cockpit foundation BT/field-override consume) then threads the
    # dormant `deferred` verdict end-to-end and adds `overrideChip`/
    # `overrideDigest` in ui.js — a SIBLING to verdictPill that layers operator-
    # override provenance (forced↑ / forced✕ / queued / drained) BESIDE the gate
    # verdict / standings status pill WITHOUT recoloring it — wired into
    # gatePanel (candidate.js) + standingsTable (structure.js) and folded into
    # the candidate/structure digests (no timestamp leak). ~0.4 KB of new
    # primitive + two consumers (back-compat: absent override → byte-identical
    # to today). The envelope is raised to 1.111 MB to cover it with headroom.
    #
    # The ABSOLUTE SCALARS IN THE GATE HEAD then surface the gate's absolute
    # champion_scalar / challenger_scalar (and, mid-flight, the live projected
    # challenger scalar + boards_done/total) as paired dn-stat chips LEFT of the
    # existing Δ chips in candidate.js gatePanel — reusing the shipped `projStat`
    # treatment for the in-flight side and folding the rounded, timestamp-free
    # endpoints into candidateDigest. This closes the "Δ without its endpoints"
    # projection gap from data that already rides on the gate object (no new
    # backend field). ~3.6 KB of new surface incl. the prior wave's accrued
    # spend (back-compat: a gate with no resolved scalars renders byte-identical
    # to today). The envelope is raised to 1.124 MB to cover it with headroom.
    #
    # The BRADLEY–TERRY UNCERTAINTY GATE (the evidence-cockpit marquee) then
    # mounts `ratingBlock`/`replicationStrip`/`ratingDigest` into candidate.js
    # gatePanel: the champion/challenger θ̂ whiskers with credible-interval caps,
    # the P(challenger stronger) bar against the configured threshold marker, and
    # — when the rating is deferred — the replication strip (replicates-spent
    # dt-rungstep pips + the next closest-CI duel + a CI-convergence sparkline,
    # capped with an explicit "inconclusive" caption when the schedule exhausts).
    # It overlays the challenger's CI band on the radar scalar vertex
    # (buildRadarModel → `chalBand`, drawn by svg.radarSilhouette) and threads a
    # field-level "deferred" caption under the structure.js standings table. All
    # of it reads gate.rating VERBATIM (build_rating_view) + its own CSS block;
    # back-compat: rating absent / present:false → the gate panel is byte-
    # identical to today. ~10.5 KB of real new evidence surface (JS + CSS). The
    # envelope is raised to 1.14 MB to cover it with headroom.
    #
    # The FIELD-TOURNAMENT OVERRIDE CONTROL PLANE then wires the operator's per-
    # challenger force-promote/reject into the standings row for ALL structures:
    # an `overrideControlCell` (ui.js) — CONFIRM-INLINE (arm → reason → POST, never
    # one-click), an OPTIMISTIC 'queued' stamp held in a module pending registry
    # (folded into structureDigest with NO timestamp so it repaints on a real
    # override but is byte-identical on a no-op beat) and a DISABLED (not POST-and-
    # fail) state when the workspace is read-only — a `postFieldOverride` helper
    # (core/api.js) for the per-generation /api/control/{promote|reject}/{gid} route
    # with the {reason, epoch, tournament_id, structure} body, the standings control
    # column + DRAINED-state resolution + the 'gate said … · operator forced …'
    # provenance caption (structure.js standings + candidate.js gate head), the
    # swiss standings table so the control plane is consistent across every
    # structure, and MULTIPLE-promoted/tie support. All reads ride on gate.override
    # / override_status / promoted_generation_ids VERBATIM (no new backend field;
    # back-compat: no override / no read-only → byte-identical). ~8 KB of new
    # control surface. The envelope is nudged to 1.152 MB to cover it with a thin
    # margin (additions kept lean — only the control plane, no extra chrome).
    #
    # The HYPOTHESIS PREDICTION-ACCURACY + CALIBRATION diagnostic then adds two
    # consumption-only surfaces: `buildPredictionScorecard` in the candidate
    # dossier (candidate.js) — the proposer's predicted-vs-realised movements per
    # claim with hit/miss/unresolved/unpredicted glyphs + the calibration fraction,
    # consuming /api/hypothesis-accuracy/{epoch}/{gen}, with every hover-level
    # detail in the hovercard singleton — and `svg.calibrationTrend` in the home
    # meta-loop ledger region (home.js) — the score fraction over the epoch's
    # lineage reusing the sparkline/staircase grammar, consuming
    # /api/calibration-trend. Both carry the EXPLICIT 'diagnostic — does not affect
    # the gate' caption and never couple to the gate; both fold a rounded, timestamp-
    # free digest into the candidate/home digests (a no-op beat is byte-identical).
    # Plus the two data.js readers + the scoped CSS (in console.css, not this
    # counted bundle). ~16 KB of new diagnostic surface (back-compat: a seed / no-
    # claims candidate and an epoch with no scored predictions render byte-identical
    # to today). The envelope is raised to 1.176 MB to cover it with headroom.
    #
    # The FIELD-DIVERSITY ribbon + overlap matrix then adds the consumption-only
    # idea-overlap surface under the proposed-field section (structure.js): a
    # `diversitySection` ribbon — the distinct-ideas/field-size + mean/max stat
    # strip, a dual mean/max pairwise-Jaccard `overlapMeter` whose fill earns its
    # tone BY DIRECTION against the diversity tolerance, a soft-reject count riding
    # the DEFERRED pill, and the max-overlap-pair hovercard — plus a per-standings-
    # row `diversityBadge` (soft_rejected → deferred pill, penalized → caution chip)
    # and `svg.diversityMatrix`/`diversityMatrixDigest` cloning the dn-mtx grid
    # (challenger × mutation-site). All of it reads the additive `diversity` block +
    # per-slot `diversity_status` VERBATIM (build_tournament_structure) and folds a
    # ROUNDED, timestamp-free `diversityDigest` into structureDigest (a no-op beat is
    # byte-identical). The overlap matrix degrades to nothing when per-challenger
    # membership is absent (the diversity block carries only the field scalars + the
    # max pair — membership is a noted Python followup). ~15 KB of new evidence
    # surface (back-compat: a gauntlet / single-challenger / pre-feature field renders
    # byte-identical to today). The envelope is raised to 1.19 MB to cover it with
    # headroom.
    #
    # The SIDEBAR-ORDER fix then re-sorts buildTreeModel's assembled epoch list
    # (shell.js) to the timestamp-ordered /api/workspace.epochs order, so the sidebar
    # tree lists epochs chronologically like the fleet cards — a ZERO-generation epoch
    # (absent from /api/lineage, previously appended last) now lands in its correct
    # middle slot. A small stable decorate-sort-undecorate + its rationale comment
    # (back-compat: a single-epoch / lineage-complete workspace renders byte-identical).
    # The envelope is raised to 1.191 MB to cover it with headroom.
    #
    # The EVIDENCE-COCKPIT LIVENESS / TRANSCRIPT fixes then add six bug fixes across
    # the live surfaces: (1) the three redundant chrome "live" signals consolidate
    # into ONE `dt-run-state` pill carrying the phase + count + stale affordance
    # (shell.js); (2) a derived `alive` (LIVE/STALLED) verdict in livestatus.js gates
    # the hero so it no longer flickers out when `running` momentarily drops during a
    # long call (live.js + shell.js); (4) the "what's running" panel shows in-flight
    # matches via active-runs corroboration when a fresh epoch roll desyncs the
    # heartbeat epoch tag — `structureDrawableRunning` + `tournamentHasActiveRuns`
    # (live.js); (5) `runIsTerminal`/`runProgressRatio` read 100% for a completed run
    # keyed to task/board completion not the wall-clock budget (structure.js); (6)
    # `dedupConsecutiveTurns` folds the duplicated goal turn in the transcript
    # (board.js). All digest-gated (a no-op beat stays byte-identical) + back-compat
    # (a seq-less / non-terminal / single-goal payload renders as today). The envelope
    # is raised to 1.20 MB to cover the new spine with headroom.
    #
    # The SVG-RENDER-FAMILY structural fix then lands two things: (a) the shared
    # text-fitting PRIMITIVES in svg.js — `textPx` (one mono char-width model),
    # `fitLabel` (truncate to a PIXEL budget, head or mid), `edgeText` (a <text>
    # kept inside its box by clamping x + flipping the anchor near an edge) and
    # `fitInto` (the two together) — the ONE home for "size text to its box",
    # replacing the per-figure char-cap-then-hand-clamp math that was the root of
    # the recurring clip/collision class; and (b) the family of correctness fixes
    # routed onto them across ~18 figure builders + the views — label-clip /
    # adjacent-collision / disconnected-line / degenerate-cardinality guards, plus
    # the elimFlow eliminated-lane + duplicate-slot dedup, the live "what's
    # running" in-flight fallback, and the chrome connection/run-state
    # disambiguation. Net additive surface (back-compat: every figure renders
    # byte-identical for normal data; the primitives only change OUT-of-box cases).
    # ~28 KB of real new render-discipline code. The envelope is raised to 1.24 MB
    # to cover it with headroom.
    #
    # The LOOP-COMMUNICATION surface (WS4-A) then adds: the trajectory / cost /
    # per-judge-trend panels (epoch view) + the fleet-card promotion-rate and
    # cost-per-promotion stats + the uncertainty-honest plateau/no-signal chip
    # (home), reading the new /api/epoch/{id}/trajectory + /cost endpoints; the
    # sparkline's opt-in measured-noise band; the topbar pause/resume + skip-round
    # controls and the per-run kill buttons through the previously-dead
    # postControl; and the authoritative /api/live/pipeline propose→apply→run→gate
    # stepper in the live hero (server-side inference rendered verbatim). All
    # digest-gated (a no-op beat is byte-identical) + back-compat (absent
    # endpoints — the Rust supervisor — render byte-identical to today). ~30 KB of
    # new loop-communication surface. The envelope is raised to 1.29 MB to cover
    # it with headroom.
    #
    # The BUILDER KNOB-GUI completion (B3) then closes the remaining copilot-only
    # contract knobs in the builder view: the weights scalar rows (default-judge /
    # plan-revision / runtime) + the severity / per-kind / per-judge mapping
    # editors (fixed vocab rows + a per-judge/namespace add-key row that fixes the
    # iterate-existing-only gap), the gate's namespace-monotonicity mapping editor,
    # the overfitting board-refresh ceiling, the proposer picker (discovered dirs +
    # builtin default + a free-text path) with a rewritten lede, the slot strip's
    # revert-to-live + undo lifecycle controls, and the racing rung0_board_size
    # param spec — plus their scoped CSS. ~8 KB of new control surface; the
    # envelope is raised to 1.31 MB to cover it with headroom.
    #
    # The INSTRUMENT LENS (R5) then lands the board-reflection view alongside the
    # B3 knob GUI in the same program integration: views/instrument.js (the
    # reflection landing / bill-of-health / judge-audit / transcript x-ray), its
    # router/tree/shell registration, the reflection data accessors, and the
    # scoped dn-instr-* CSS — ~22 KB. Each wave's raise accounted only for its
    # own additions, so the merged program needs the union: the envelope is
    # raised to 1.35 MB to cover both with headroom.
    #
    # The ELIM-RADIAL RESTORE then brings back the concentric-ring bracket figure
    # the U4 cut (C1) had retired: svg.elimRadial + elimRadialDigest, the
    # dn-elimradial-* + dt-fig-switch CSS, the svg.dn-elimradial-hero cap, and the
    # structure.js radial-primary/flow-companion + double-elim combo/radial toggle
    # arrangements. The C1 cut was VETOED after the fact — the operator kept
    # elimRadial as a visual — so the ~6 KB of source/CSS comes back (now reading
    # the SERVED gen_states verbatim, no client re-derivation). The envelope is
    # raised to 1.37 MB to cover it with headroom.
    #
    # The RATINGS/RECOMBINATION/GENEALOGY/ENSEMBLE program integration then lands
    # its three GUI touches at once: the builder's recombine toggle row + the
    # genealogy numeric row (builder.js) and the proposer breadth/depth role
    # cards with their honest default-proposer copy (settings.js). Each branch
    # fit under the prior line alone; their union sits ~0.8 KB above it — the
    # same per-wave-raise interaction the 1.35 MB entry records. The envelope is
    # nudged to 1.372 MB to cover the combined surface with a thin margin.
    #
    # The CRITIC-CALIBRATION knob row (WS-CAL) then adds one builder controlRow
    # (builder.js) — the `calibration_feedback` numeric input beside the
    # genealogy row, driving set_proposer_quality — so the operator can opt the
    # proposer into a view of its own prediction calibration. ~0.7 KB of new
    # control surface (a numeric row + its tooltip copy).
    #
    # The STRUCTURED-LOGGING foundation then adds the operator-log pane: a new
    # workspace-level view (views/logs.js), its toolbar/row CSS in console.css,
    # and the shell/router/data wiring for the `#/logs` route + top-bar entry —
    # ~8 KB of real new surface.
    #
    # Each wave raised the line for only its own addition (1.374 / 1.386 MB);
    # the merged program needs the union — the per-wave-raise interaction the
    # 1.35 MB entry records. The envelope is set to 1.388 MB to cover the
    # calibration row + the log pane (+ the publication overflow CSS, which is
    # in console.css and rides the same counted bundle) with a thin margin.
    #
    # The LIVE-TRANSCRIPT STREAMING surface (WS3) then makes the board view's
    # inline transcript GROW during a run: the frame gates on a STRUCTURE digest
    # (headers / columns / caption / scroller shells) while the turn CONTENT is
    # reconciled into a persistent scroller — a new turn APPENDS one node rather
    # than rebuilding the thread, so a live beat no longer flashes the column
    # (board.js). The live refetch is gated on a genuine progress-seq ADVANCE
    # (state.lastSeq) so a burst of heartbeats at a stable seq issues zero
    # re-reads, and a running column carries a quiet "streaming — through turn N"
    # caption that vanishes on completion (a 5-line console.css rule reusing the
    # shipped live vocabulary — no new chrome). All digest-gated (a no-op beat is
    # byte-identical, scroll preserved) + back-compat (no in-flight run → the
    # settled view renders byte-identical to today, no caption). ~6 KB of real new
    # streaming-discipline code. The envelope is raised to 1.392 MB to cover it
    # with a thin margin.
    #
    # The STREAMING SCROLL-DISCIPLINE review fix then hardens the same board.js
    # reconcile: the divergence-rebuild branch dropped the scroll pin (a merged
    # reasoning turn whose text grows across two seqs flips only the last turn's
    # signature → prefix divergence → clamp-to-0 → live-tail breaks). The fix adds
    # a LAST-TURN-GREW path (re-render just that one node in place, preserving the
    # prefix + scroll position) and captures the pin BEFORE the remaining wholesale
    # rebuild so a bottom-pinned reader stays pinned and a scrolled-up one keeps
    # their offset; the live-seq tracker is also keyed per-entry so a return visit
    # after the seq advanced elsewhere refetches. ~0.6 KB of real reconcile code +
    # its rationale on the same surface. The envelope is nudged to 1.393 MB to
    # cover it with a thin margin.
    #
    # The WAVE-3 INTEGRATION then lands the other two GUI touches beside the
    # streaming work: the telemetry-dialect select row with its capability-tier
    # caption (builder.js, ~1.5 KB) and the diff-complexity ceiling row + the
    # scope caveats on both parsimony tooltips (builder.js, ~1.5 KB). Each
    # branch fit under the prior line alone; their union sits ~3 KB above it —
    # the per-wave-raise interaction the 1.35 MB entry records, fourth
    # occurrence. The envelope is raised to 1.398 MB to cover the combined
    # surface with headroom.
    #
    # The EVAL-CENTRIC VIEW then lands as three sibling workstreams that merge
    # onto one branch. WS-MATRIX adds the new top-level Evals surface:
    # views/evals.js — the entries × candidates matrix (the board-as-instrument
    # OUTCOMES lens) rendered in the shipped `dn-mtx` grid grammar off
    # /api/epoch/{id}/evals, with evidence shading (single → faint, replicated →
    # firm — the SERVED tier, never re-derived), per-entry flip-rate row badges
    # (honest "unmeasured", never 0), champion-spine column marking, the
    # round-grouped header, three client-side filters (failures / flips / holdout,
    # digest-folded so a no-op beat is byte-identical), and cell click-through into
    # the run transcript + a live harmonograf deep-link — plus its router/shell/tree
    # registration and the data.js reader (~19.4 KB). WS-HEALTH (EVAL-VIEW.md §5)
    # adds views/evals_health.js — the board read as a MEASURING DEVICE: the mono
    # noise-floor + live MDE-ladder strip (the CAMPAIGN.md §3 two-sample form,
    # stating its formula + n — never a bare number) and the ranked
    # instrument-quality findings (noisiest evals, dead channels with the
    # minimum-comparisons honesty threshold, runtime cost, the holdout-ladder
    # budget + rotation cadence, and reflection redundancy clusters when already
    # built) — a SEPARATE module the matrix view mounts as a strip + section INSIDE
    # itself (a merge-safe `mount` seam), digest-gated and recommend-only, reusing
    # the shipped dn-stat / dn-faint / dn-board-table idiom (~21 KB). The
    # entry-DOSSIER surface rides the same branch. Each fit under its own line in
    # isolation — three branches measured 1,448,900 bytes together — but their union
    # sits above any one alone: the per-wave-raise interaction the 1.35 MB entry
    # records, now with THREE overlapping read-only surfaces landing at once, so the
    # envelope is raised once to cover the combined total rather than three times in
    # sequence. All three are back-compat (a cold / un-calibrated epoch renders the
    # honest-empty panel; every other view renders byte-identical) and EVAL-VIEW.md
    # §6 pre-registers the bump — the house rationale (read-only surfaces that pay
    # for themselves in operator time) covers it. The envelope is raised to 1.452 MB
    # to cover the combined surface with headroom.
    #
    # The EVAL-SYNTHESIS SUGGESTIONS INBOX (WS-SURFACE, EVAL-SYNTHESIS.md §6) then
    # adds the board-editor's suggestions inbox to views/builder.js — the persisted
    # `reflect suggest` output as verdict-led rows (rationale + provenance + the
    # admission stats rendered HONESTLY: measured-with-n / `unmeasured`, the
    # recommended bands as quiet advice, never an auto-verdict), a "stage to draft"
    # affordance driving add_board_entry / add_judge, and one Instrument-lens link
    # back to the motivating reflection — plus the `getSuggestions` reader in
    # builder/api.js. Measured bundle 1,455,487 bytes, ~3.5 KB over the prior line.
    # It is the generative-reflection surface's only frontend (recommend-only:
    # staging forks a draft the operator seals; back-compat: an empty / cold feed
    # renders the honest empty state, every other view byte-identical). The
    # envelope is raised to 1.46 MB to cover it with headroom.
    #
    # The TRAJECTORY-BOOTSTRAP VISUALS (WS-SUGVIZ, TRAJECTORY-UI.md §2.2) then
    # add the suggestion / board-creation surface. A shared admission-visuals
    # module (js/core/admission_viz.js) renders a suggestion's admission stats as
    # marks reusing the shipped vocabulary — the flip-rate WHISKER (the BT-whisker
    # figure + the advisory-ceiling reference rule; over-ceiling rides caution),
    # the discrimination PIPS (the dt-rungstep idiom), and the evidence TIER
    # (probed = firm / planned = faint) — with honest `unmeasured` states (never a
    # fabricated 0). The inbox rows (builder.js) upgrade to CARDS carrying those
    # visuals + a PROVENANCE MINI-STRIP: the shared trajectory-strip figure's
    # compact mode behind a GUARDED dynamic import (absent → a textual fallback
    # from the real provenance payload), a Traces detail link, and the
    # roll-honesty note. The Evals matrix (evals.js) gains GHOST ROWS for
    # suggested-but-not-yet-scored board entries — pending-styled + visually
    # unambiguous (never mistakable for measured data, §4), the admission marks in
    # the row, an apply affordance, joined client-side from the same
    # `/builder/suggestions` feed the inbox reads — plus the two data.js readers
    # and the scoped dn-adm-* / ghost-row / card CSS. All digest-gated (a no-op
    # beat is byte-identical; the no-ghost matrix is byte-identical to before the
    # feature) + recommend-only (every affordance forks a builder draft the
    # operator seals). Measured bundle 1,482,829 bytes, ~16.8 KB over the branch
    # base — the house rationale (a read-only surface that pays for itself in
    # operator time; TRAJECTORY-UI.md §4 pre-registers the bump) covers it. The
    # envelope is raised to 1.49 MB to cover it with headroom.
    assert total < 1_490_000, f"bundle is {total} bytes, exceeds 1_490_000 envelope"


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
