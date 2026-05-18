# zicato/dashboard/static — dashboard UI bundle

Vanilla HTML / CSS / JS source for the zicato dashboard. The standalone
Python dashboard service (`zicato.dashboard.server`) serves these files
off disk from `/` and `/static/...`.

No build step. No framework. No external network. Everything in this
directory must remain self-contained — no Google Fonts, no CDN, no
remote scripts. The renderer mirrors the palette and typography of
`zicato/epoch/html_report.py` so `analysis.html` and the live
dashboard read as siblings.

## Files

The frontend is a modular ES-module app — a thin entry point plus the
core spine, a shared component library and the render layer. No build
step, no framework, no external network. The full contracts every
module codes against are pinned in `js/CONTRACTS.md`.

- `index.html` — single-page shell. Top-level `<svg>` elements are
  declared here so the JS modules can populate them via
  `createElementNS`. Loads `style.css` and `app.js` (as a module).
- `style.css` — all styling. CSS custom properties drive light + dark
  themes; the dark branch lives under `@media (prefers-color-scheme:
  dark)`. Includes a print stylesheet (snapshot-only).
- `app.js` — the thin entry point. Imports the core spine, wires the
  event bus (state mutation / route change → render), and bootstraps.
- `js/core/` — the data/render spine. `state.js` (the single AppState),
  `bus.js` (pub/sub), `router.js` (hash routing + deep links),
  `api.js` (the consolidated `/api/environment` read + drill fetches),
  `sse.js` (EventSource + typed deltas), `dom.js` (the incremental,
  keyed, no-flash render primitives — `mount`, `reconcileList`,
  `appendRows`, `patch*`), `format.js`, `harmonograf.js`.
- `js/components/index.js` — the shared component library: cards,
  tables, badges, the diff renderer, the line chart, progress meters.
- `js/views/` — the render layer. `render.js` paints every view
  (Overview / Lineage / Tournament / Epoch / Files / Conversation +
  the chrome); `shared.js` holds the cross-view helpers
  (`predictedGateVerdict`, the entry-status bucket, the data-quality
  summary); `mock.js` is the offline `?mock=1` snapshot.
- `js/CONTRACTS.md` — the pinned frontend contracts: the
  `/api/environment` shape, the SSE delta types, the AppState shape,
  the component API, per-view specs, the routes.
- `test/` — a dependency-free JS/DOM test harness. `harness.mjs` is a
  minimal DOM + assertion runner; `*.test.mjs` files verify the render
  spine (incremental updates, the append-only no-flash log tail,
  matchup-click survival across a state delta). Run with
  `node test/run-all.mjs`; also driven from `tests/test_dashboard_js.py`.
  The `test/` directory is a dev tool and is NOT shipped in the wheel.
- `icons.svg` — inline-able sprite. Reference via
  `<use href="/static/icons.svg#icon-name"/>`.

### The structural no-flash render spine

A delta NEVER rebuilds a panel's `innerHTML`. After the api/sse layer
folds new data into AppState, the render layer patches only the
affected DOM nodes — keyed by a stable `data-*` id — via the
`core/dom.js` primitives. Because nodes keep identity across a
re-render, their event listeners survive (the matchup-click fix) and
the browser does not repaint an unchanged subtree (no flashing). The
activity-log tail is strictly append-only: `appendRows` adds only
genuinely-new keyed rows and never clears the host.

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

Total bundle (index.html + style.css + app.js + icons.svg) is held
under an uncompressed envelope by the structural test in
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
