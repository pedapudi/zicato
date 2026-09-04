# zicato/dashboard/static — dashboard UI bundle

Vanilla HTML / CSS / JS source for the zicato dashboard. The standalone
Python dashboard service (`zicato.dashboard.server`) serves these files
off disk from `/` and `/static/...`.

No build step. No framework. No external network (the sole exception is
Google Fonts, loaded by `console.js` with `display=swap` and system
fallbacks). Everything else in this directory must remain self-contained
— no CDN, no remote scripts.

One user interface loads: the console. Its design language is documented
in `docs/design/CONSOLE-DESIGN-LANGUAGE.md`. It lives under `js/**` plus
`css/console.css` and reuses the shared `js/core/*` data spine.

## Files

The frontend is a modular ES-module app: a thin entry point (`console.js`)
plus the shared core spine (`js/core/**`) and the console modules
(`js/**`). The full contracts every core module codes against are pinned
in `js/CONTRACTS.md`.

- `index.html` — single-page shell hosting `#console-root`. Loads
  `css/console.css` and `console.js` (as a module).
- `console.js` — the entry point. Loads Google Fonts, imports the core
  spine and the console shell, wires the event bus (state mutation or
  route change triggers a render), and bootstraps.
- `js/core/` — the data and render spine. `state.js` (the single
  AppState), `bus.js` (publish/subscribe), `api.js` (the consolidated
  environment read plus the drill-down fetches), `sse.js` (EventSource
  plus typed deltas), `dom.js` (the keyed, no-flash `el`/`svgEl` render
  primitives), `admission_viz.js` (the suggestion-admission figures),
  `harmonograf.js` (the generative background mark).
- `js/` — the console. `shell.js` (chrome, the tree-sidebar to
  detail-pane router host, and the page-scale pill), `router.js` (the
  hierarchical hash routes), `tree.js` (the data-model TREE sidebar,
  round-grouped), `svg.js` (the data-viz primitives — `heatmap`,
  `valueDotPlot`, `sparkbar`/`genDots`, the structure figures
  `survivalFunnel`/`swissLadder`/`swissOverview`/`elimFlow`/`duelFlow`,
  the epoch figures `roundTimeline`/`waterfall`/`reignGantt`, the
  `sankey`, the side-by-side diff), `dag.js` (the lifecycle DAG),
  `matrix.js` (the `dn-mtx` table grid the mutation surface, the
  field-diversity figure and the evals matrix are all built from),
  `live.js` and `livestatus.js` (the live-run controller and its status
  derivation), `hovercard.js` (the singleton hover-for-detail card),
  `compare.js` (the side-by-side compare picker and split frame),
  `ui.js` (digest-gated swap, pills, themes, typefaces), `data.js` (the
  per-epoch read accessors), plus `convo.js`, `facets.js`, `rounds.js`,
  `swatchdropdown.js`, `transcript_stream.js`, `turns.js`,
  `typefacedropdown.js` and `unit_liveness.js`. Each module under
  `js/views/**` paints one detail pane: `home`, `epoch`, `gens`,
  `candidate`, `board`, `boards`, `boardstatus`, `builder`, `diff`,
  `evals`, `evals_health`, `instrument`, `ledger`, `logs`, `mutations`,
  `publication`, `settings`, `structure`, `traces`.
- `css/console.css` — all console styling: the sixteen-theme `--v2-*`
  six-role token contract (swapped by `[data-t-theme]`), the typeface
  tokens (`[data-t-type]`), and every fit-to-width SVG mark's classes
  (`dn-*` / `dt-*`).
- `js/CONTRACTS.md` — the pinned frontend contracts (the API shape, the
  server-sent-event delta types, the AppState shape, the routes).
- `test/` — a dependency-free JS/DOM test harness. `harness.mjs` is a
  minimal DOM and assertion runner; the `*.test.mjs` files (shared
  fixtures in `fixtures.mjs`) verify the render spine, the figures, and
  the digest discipline. Run with `node test/run-all.mjs`; also driven
  from `tests/test_dashboard_js.py`. The `test/` directory is a
  development tool and is NOT shipped in the wheel.
- `icons.svg` — inline-able sprite. Reference via
  `<use href="/static/icons.svg#icon-name"/>`.

### The structural no-flash render spine (digest-gating)

A no-op heartbeat frame NEVER rebuilds the DOM. Each pane computes a stable
digest of only its structural and content data (timestamps and heartbeat
fields excluded) and writes via `ui.gatedSwap(host, digest, build)`: when the
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
GET  /static/{path}                 — console.css, console.js, icons.svg, ...
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
GET  /events                         — server-sent events: snapshot,
                                       coalesced state_change, run_log,
                                       heartbeat
POST /api/control/{pause,skip-round,kill,promote,reject,brief}
```

On a `state_change` frame the client debounces and performs ONE
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
renders normally. No server-sent-event stream is opened. This mode
serves design iteration and the structural test in
`tests/test_dashboard_ui.py`.

## Size envelope

The structural test in `tests/test_dashboard_ui.py` holds the total
bundle (`index.html`, `css/console.css`, `console.js`, the `js/**`
modules and `icons.svg`) under an uncompressed size ceiling. The bundle
is served from localhost and costs no network time; the ceiling exists
only to keep it from growing without bound.

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
