# zicato/dashboard/static — dashboard UI bundle

Vanilla HTML / CSS / JS source for the zicato dashboard. The standalone
Python dashboard service (`zicato.dashboard.server`) serves these files
off disk from `/` and `/static/...`.

No build step. No framework. No external network (the sole exception is
Google Fonts, loaded by `app_T.js` with `display=swap` + system
fallbacks). Everything else in this directory must remain self-contained
— no CDN, no remote scripts.

The shipping UI is **Console** (the bake-off convergence winner,
code-named **Variant T**); its design language is documented in
`docs/design/CONSOLE-DESIGN-LANGUAGE.md`. It is self-contained under
`js/**` + `css/console.css`, reusing only the
shared `js/core/*` data spine. Exactly one UI loads at a time.

## Files

The frontend is a modular ES-module app — a thin entry point (`app_T.js`)
plus the shared core spine (`js/core/**`) and the Console UI
(`js/**`). No build step. The full contracts every core
module codes against are pinned in `js/CONTRACTS.md`.

- `index.html` — single-page shell hosting `#variant-root`. Loads the
  variant CSS + `app_T.js` (as a module).
- `app_T.js` — the thin entry point for Console. Loads Google Fonts,
  imports the core spine + the Variant-T shell, wires the event bus
  (state mutation / route change → render), and bootstraps.
- `js/core/` — the data/render spine shared by the variant. `state.js`
  (the single AppState), `bus.js` (pub/sub), `router.js` (hash routing +
  deep links), `api.js` (the consolidated environment read + drill
  fetches), `sse.js` (EventSource + typed deltas), `dom.js` (the keyed,
  no-flash `el`/`svgEl` render primitives), `format.js`,
  `harmonograf.js`, `hypothesis_block.js`.
- `js/` — the Console UI. `shell.js` (chrome + the
  tree-sidebar ↔ detail-pane router host + the page-scale pill),
  `router.js` (the hierarchical hash routes), `tree.js` (the data-model
  TREE sidebar, round-grouped), `svg.js` (the data-viz primitives —
  `heatmap`, `valueDotPlot`, `sparkbar`/`genDots`, the structure figures
  `survivalFunnel`/`swissLadder`/`swissOverview`/
  `elimFlow`/`duelFlow`, the epoch figures `roundTimeline`/`waterfall`/
  `reignGantt`, the `sankey`, the side-by-side diff), `dag.js` (the
  lifecycle DAG), `live.js` + `livestatus.js` (the live-run controller +
  status), `hovercard.js` (the singleton hover-for-detail card),
  `compare.js` (the side-by-side compare picker + split frame), `ui.js`
  (digest-gated swap, pills, themes), `data.js` (the per-epoch read
  accessors). `js/views/**` paints one detail pane each
  (`home`, `epoch`, `gens`, `candidate`, `board`, `boards`, `rounds`,
  `structure`, `mutations`, `publication`, `diff`).
- `css/console.css` — all Console styling: the
  sixteen-theme `--v2-*` six-role token contract (swapped by
  `[data-t-theme]`), the typeface tokens (`[data-t-type]`), and every
  fit-to-width SVG mark's classes (`dn-*` / `dt-*`).
- `js/CONTRACTS.md` — the pinned frontend contracts (the API shape, the
  SSE delta types, the AppState shape, the routes).
- `test/` — a dependency-free JS/DOM test harness. `harness.mjs` is a
  minimal DOM + assertion runner; `variant_t.test.mjs` + `core.test.mjs`
  verify the render spine, the figures, and the digest discipline. Run
  with `node test/run-all.mjs`; also driven from
  `tests/test_dashboard_js.py`. The `test/` directory is a dev tool and
  is NOT shipped in the wheel.
- `icons.svg` — inline-able sprite. Reference via
  `<use href="/static/icons.svg#icon-name"/>`.

### The structural no-flash render spine (digest-gating)

A no-op SSE heartbeat NEVER rebuilds the DOM. Each pane computes a stable
digest of only its structural/content data (timestamps + heartbeat fields
excluded) and writes via `ui.gatedSwap(host, digest, build)`: when the
digest equals the one the host last painted and the host still has
children, nothing is written — a steady heartbeat is a true no-op, so
scroll position, focus and the hovercard survive. Live state animates
*values / positions* (CSS transitions, never `animation: …infinite`),
while digest-gating governs *structure*. See
`docs/design/CONSOLE-DESIGN-LANGUAGE.md` §6.

## Environment-view data flow

The dashboard presents the state of an instantiated zicato environment.
It reads the whole environment through ONE consolidated endpoint and
refreshes it on a single coalesced poll — it does NOT fan out to many
per-section endpoints, and it does NOT poll on a tight timer.

```
GET  /                              — index.html
GET  /static/{path}                 — style.css, app.js, icons.svg, ...
GET  /api/environment                — the consolidated environment read:
                                       epoch contract, live + past
                                       tournaments, generation lineage,
                                       active runs, health, heartbeat,
                                       run-log tail. ONE request.
GET  /api/run-log?after=<cursor>     — append-only run-log tail batch
GET  /api/tournaments/{gen_id}       — per-matchup detail (drill-down)
GET  /api/files...                   — generation file tree + patches
GET  /api/mutations/{epoch}          — epoch mutation surface (sites)
GET  /api/mutations/{epoch}/{id}     — one site: baseline + patched diff
GET  /api/conversation/{run_id}      — reconstructed transcript
GET  /api/matchup/{entry}/conversations — champion + challenger pair
GET  /events                         — SSE: snapshot, coalesced
                                       state_change, run_log, heartbeat
POST /api/control/{pause,skip-round,kill,promote,reject,brief}
```

On an SSE `state_change` frame the client debounces and performs ONE
`/api/environment` fetch. On a `run_log` frame it performs an
append-only `/api/run-log?after=<cursor>` poll so the log tail GROWS
rather than re-rendering. The dedicated per-section endpoints
(`/api/state`, `/api/epoch`, `/api/tournaments`, `/api/active-tournament`,
`/api/lineage`, `/api/active-runs`, `/api/health-report`) remain served
for compatibility but are not on the steady-state path.

## Mock mode

For offline preview without a running dashboard service:

```
file:///path/to/zicato/dashboard/static/index.html?mock=1
```

A hardcoded `mockSnapshot()` populates `AppState` and every panel
renders normally. SSE is not opened. Useful for design iteration and
for the structural test in `tests/test_dashboard_ui.py`.

## Size envelope

Total bundle (index.html + the Variant-T CSS + `app_T.js` + the
`js/**` modules + icons.svg) is held under an uncompressed envelope
by the structural test in
`tests/test_dashboard_ui.py`. It is a localhost-served vanilla bundle
with no network cost; the guard exists only to keep the bundle from
drifting unboundedly.

## Accessibility

- `role` and `aria-label` on every interactive region
- skip-link at the top of the page (visible on focus)
- keyboard activation on every clickable lineage node, run card, and
  tournament entry row (Enter / Space)
- `aria-live="polite"` on the log tail so screen readers announce new
  lines without interrupting
- focus outlines preserved on every focusable element
- `Escape` closes the drill-down side panel
- print stylesheet hides live-only panels and shows the snapshot
