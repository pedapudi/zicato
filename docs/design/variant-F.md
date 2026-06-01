# Variant F — "Current"

A synthesis dashboard variant for zicato, reachable at `?ui=F`. It tells the
cause→effect story of the meta-harness: an agent is mutated by a **patch**
into a challenger, run over a fixed **board**, measured for **drift/loss**,
and a promote **gate** decides whether it unseats the reigning champion —
across generations and epochs.

It is built from the best parts of the four round-1 explorations, kept
**self-contained** under `js/variants/F/` and `css/variants/F/` (it imports
nothing from variants A–D; the shared parts were copied and adapted into
F's own namespace). It reuses only `js/core/*` — the data layer, SSE, the
no-flash render spine — exactly like every other variant.

## Identity — causal-narrative, flow-first

The dashboard leads with the **causal flow + lifecycle DAG** as the hero of
every candidate and epoch; the patch → drift → gate causality *is* the main
view. Compact data-viz (sparklines, dot-plots, slopegraphs) is embedded
**inline as supporting evidence**, never as the lead. Editorial typographic
framing — pull-quotes, captions, generous whitespace — carries the voice.
Aesthetic: an airy serif headline language over **Solarized-Dark** (default;
DASHBOARD-V2 §3.1) with C's interactive diagrams.

### What came from where (copied + adapted, self-contained)

- **Backbone — C (causal flow + lifecycle):** the pan/zoom SVG surface, the
  layered collision-free DAG layout, the three-stage Sankey, the
  tournament-style topology engine, and the lifecycle/scoring/experiment
  views. (`js/variants/F/diagram/*`, most of `js/variants/F/views/*`.)
- **Evidence — D (data-viz):** the dependency-free Tufte toolkit —
  `sparkline`, `valueDotPlot`, `sparkbar`, the **non-colliding**
  `pairedSlopegraph`/`decollide`/`jitterColumn`. (`js/variants/F/lib/svg.js`.)
- **Editorial voice — B:** the safe Markdown + pull-quote primitives.
  (`js/variants/F/lib/prose.js`.)
- **Navigation — A:** the hierarchical breadcrumb IA (the top-nav screens),
  plus A's loved **Fleet** epoch-trendline card, redrawn with D's sparkline.
- **Theme:** Solarized-Dark mapped onto the `--v2-*` token names both the C
  diagrams and the D marks already read, all scoped under `.czF-root`.

## Screens

- **Home / Environment** — the lineage DAG is the centrepiece (every epoch a
  lane, every generation a node, coloured by verdict); beneath it the
  editorial **Fleet** strip, one card per epoch with a loss-trajectory
  sparkline, objective, and gen/promoted counts.
- **Epoch** — the objective is the headline; the generation lineage is a
  causal DAG (champion spine + branching dead ends); each node opens its
  story; the board and proposer brief are reachable (the brief in a drawer).
- **Candidate (Lifecycle)** — the hero: C's lifecycle DAG + the
  patch → board → aggregate → gate causal flow, with the **hypothesis** and
  the **gate's rejection reason** pulled out as editorial pull-quotes, and
  D's per-board value-dot-plot + sparkbar embedded as the evidence behind the
  flow. (The signature Sankey lives on the Experiment screen.)
- **Match-ups** — C's tournament-style topology switcher (gauntlet star/hub,
  single/double-elim, Swiss, racing), enriched with D's real paired-matchup
  slopegraph for each round's duel grid (`/api/matchup-grid`).
- **Run** — the transcript as a readable top-to-bottom narrative: turns, tool
  calls, and inline drift annotations.

## Data

Bound to the real API in `_ENRICHMENT-BRIEF.md` — no invented fields:
`/api/lineage`, `/api/epoch`, `/api/generation/{e}/{g}/per-entry`,
`/api/run/.../expectations` + `.../per-judge`, `/api/tournaments`,
`/api/matchup-grid/...`, `/api/round/.../gate`, `/api/conversation/{run_id}`,
`/api/score-trajectory`. It looks right with the live data (one epoch
`2026-05-30_e0`, v0 crowned, v1/v2 rejected, all entries failing) and
degrades to honest empty states on null.

## Render discipline (the four recurring bugs, and why they cannot appear)

1. **Flashing / refresh on a heartbeat.** Every view is **digest-gated**: it
   computes a stable digest of structural/content data *only* (verdicts,
   scalars, ids, decisions — **never** timestamps), stores `_lastDigest`, and
   `if (digest === _lastDigest && stage.firstChild) return`. A
   heartbeat-only tick re-stamps a clock → identical digest → zero DOM
   written. Asserted by `variant_f.test.mjs` (the no-op repaint test:
   identical node identity, zero `innerHTML` writes).
2. **Stale view after navigation.** `app_F.js` clears the single persistent
   stage host on a **view switch** before rendering, so a digest-gated view
   never inherits the previous screen's DOM. One content host is reused, not
   recreated.
3. **Drill-down caches churning on every heartbeat.** Per-entry / gate /
   tournament / transcript caches invalidate **only** on a route/view change
   (`resetXCaches()` called from `app_F` on view switch), never on a
   `state:changed` tick. The match-ups topology selection lives in module
   scope and rebuilds only when the selection changes.
4. **Animation re-firing / colliding marks.** The one marching-ants flow edge
   is a CSS `transition`/keyframe applied at build time; because the SVG is
   only rebuilt when the digest changes, it cannot re-fire on a steady tick.
   The transcript tail is normal block flow inside a `max-height` +
   `overflow-y:auto` (`min-height:0`) scroll container — no overlapping
   absolute rows. The paired matchup slopegraph uses D's `decollide` +
   `jitterColumn` so coincident values never overdraw (asserted in the suite).

**Cold deep-link hydration.** A direct load of `#/F/run/<run_id>` has no live
state, so the Run view fetches its own transcript from
`/api/conversation/{run_id}`, showing loading → content, never empty.
Asserted by `variant_f.test.mjs`.

## Files

- `app_F.js` — entry point (debounced single render, view-switch host clear +
  cache invalidation, SSE + environment load).
- `js/variants/F/{router,model,chrome}.js`
- `js/variants/F/diagram/{surface,primitives,sankey,topology}.js`
- `js/variants/F/lib/{svg,prose}.js` — evidence + editorial primitives.
- `js/variants/F/views/{environment,epoch,experiment,lifecycle,scoring,styles,tournament,run,bench}.js`
- `css/variants/F/variant.css` — Solarized-Dark, scoped under `.czF-root`.
- `test/variant_f.test.mjs` — incl. the digest-gated no-op and cold deep-link
  transcript tests.

`?ui=F` is wired in `index.html` (untouched). Run the suite with
`node test/run-all.mjs`.
