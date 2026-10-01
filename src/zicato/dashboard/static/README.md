# zicato/dashboard/static — dashboard UI bundle

Vanilla HTML / CSS / JS source for the zicato dashboard. The standalone
Python dashboard service (`zicato.dashboard.server`) serves these files
off disk from `/` and `/static/...`.

No build step. No framework. No external network, with one exception:
`console.js` loads the typeface picker's families from Google Fonts with
`display=swap` and system fallbacks. Two monospace faces (iA Writer Mono
and JetBrains Mono) are self-hosted under `fonts/`; JetBrains Mono backs
the fixed brand mono.
Everything else in this directory must remain self-contained — no CDN,
no remote scripts.

One user interface loads: the console. Its design language is documented
in `docs/design/CONSOLE-DESIGN-LANGUAGE.md`. It lives under `js/**` plus
`css/console.css` and reuses the shared `js/core/*` data spine.

## Files

The frontend is a modular ES-module app: a thin entry point (`console.js`)
plus the shared core spine (`js/core/**`) and the console modules
(`js/**`). The full contracts every core module codes against are pinned
in `js/CONTRACTS.md`.

- `index.html` — single-page shell hosting `#console-root`. Links
  `style.css` and loads `console.js` as a module.
- `style.css` — the document shell outside `#console-root`: the page
  ground, the skip link, and the bare-element and SVG-text defaults the
  analyzer's server-rendered publication fragment needs.
- `console.js` — the entry point. Injects the Google Fonts link and the
  `css/console.css` stylesheet, then mounts the console shell
  (`js/shell.js`) into `#console-root`.
- `js/core/` — the data and render spine. `state.js` (the single
  AppState), `bus.js` (publish/subscribe), `api.js` (the consolidated
  environment read plus the drill-down fetches), `sse.js` (EventSource
  plus typed deltas), `dom.js` (the `el`/`svgEl` construction and
  `patchText`/`patchClass` no-flash primitives), `prefs.js` (the
  persisted per-viewer preference store), `harmonograf.js` (the
  harmonograf deep-link builders and their liveness gate).
- `js/` — the console. `shell.js` (chrome, the tree-sidebar to
  detail-pane router host, and the page-scale control), `router.js` (the
  hierarchical hash routes), `tree.js` (the data-model TREE sidebar,
  round-grouped), `svg.js` (the data-viz primitives — `heatmap`,
  `valueDotPlot`, `sparkbar`/`genDots`, the structure figures
  `survivalFunnel`/`swissLadder`/`swissOverview`/`elimRadial`/`duelFlow`,
  the epoch figures `roundTimeline`/`waterfall`/`reignGantt`, the
  side-by-side diff), `tournament_model.js` (the tournament-structure
  models those figures draw), `dag.js` (the lifecycle DAG),
  `matrix.js` (the `dn-mtx` table grid the mutation surface, the
  field-diversity figure and the evals matrix are all built from),
  `live.js` and `livestatus.js` (the live-run controller and its status
  derivation), `hovercard.js` (the singleton hover-for-detail card),
  `compare.js` (the side-by-side compare picker and split frame),
  `ui.js` (digest-gated swap, state labels, themes, typefaces), `data.js` (the
  per-epoch read accessors), plus `convo.js`, `facets.js`, `rounds.js`,
  `icons.js` (the one drawn icon set: every control, verdict, crown and
  tree mark is an icon from it, never a typed symbol),
  `swatchdropdown.js`, `transcript_stream.js`, `turns.js`,
  `typefacedropdown.js`, `dropdown.js` and `unit_liveness.js`. Each of
  the fourteen routed views has a module under `js/views/`: `home`,
  `epoch`, `gens`, `candidate`, `board`, `boards`, `diff`, `evals`,
  `instrument`, `logs`, `mutations`, `publication`, `settings`, `traces`.
  `js/panels/` holds a page section a view composes rather than routes to
  (`evals_health`, the evals page's instrument-health panel);
  `boardstatus`, `ledger` and `structure` are panels of the same kind
  that sit under `js/views/` beside the epoch and rounds pages that
  mount them.
- `css/console.css` — all console styling: the sixteen-theme `--v2-*`
  six-role token contract (swapped by `[data-t-theme]`), the typeface
  tokens (`[data-t-type]`: `--v2-sans` for prose, controls and chrome,
  `--v2-mono` for data, code and ids), and every fit-to-width SVG mark's
  classes (`dn-*` / `dt-*`, and `ezn-*` for the lifecycle DAG). The
  stylesheet draws no accent left rail and no pill, tag or badge: a
  selected item renders its name in the accent colour, and a state is
  plain text in its tone colour (`js/CONTRACTS.md` §6a).
- `js/CONTRACTS.md` — the pinned frontend contracts (the API shape, the
  server-sent-event delta types, the AppState shape, the routes).
- `test/` — a dependency-free JS/DOM test harness. `harness.mjs` is a
  minimal DOM and assertion runner; the `*.test.mjs` files (shared
  fixtures in `fixtures.mjs`, recorded endpoint responses read through
  `recorded.mjs`) verify the render spine, the figures, and the digest
  discipline. Run with `node test/run-all.mjs`; also driven
  from `tests/test_dashboard_js.py`. The `test/` directory is a
  development tool and is NOT shipped in the wheel.
- `brand/` and `fonts/` — the favicons and logo marks `index.html`
  links, and the self-hosted woff2 faces `css/console.css` declares.

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
The live state it keeps (`js/core/state.js`) comes from ONE consolidated
endpoint, refreshed once per coalesced change signal — a change signal
does not fan out to many per-section endpoints, and nothing polls on a
tight timer. A view reads the rest of what it draws through the cached
per-route accessors in `js/data.js` when it renders.

```
GET  /                              — index.html
GET  /static/{path}                 — console.css, console.js, js/*.js, ...
GET  /api/environment                — the consolidated environment read:
                                       workspace identity, epoch summary,
                                       active tournament, generation
                                       lineage, active runs, heartbeat,
                                       liveness, lock, run-log tail.
                                       ONE request.
GET  /api/run-log?after=<cursor>     — append-only run-log tail batch
GET  /api/epoch, /api/lineage, …     — per-view reads through js/data.js
GET  /api/files/...                  — generation patches, diff, content
GET  /api/mutations/{epoch}          — epoch mutation surface (sites)
GET  /api/mutations/{epoch}/{id}     — one site: baseline + patched diff
GET  /api/run/{e}/{g}/{entry}/transcript[/delta] — one run's transcript
GET  /events                         — server-sent events: snapshot,
                                       coalesced state_change, run_log
GET  /settings/models                — configured model engines, secrets
                                       withheld
POST /api/control/{pause,resume,skip-round,promote,reject,brief}
```

`js/CONTRACTS.md` lists the payload shapes and names the routes no
client reads (`/api/tournaments/{gen}`, `/api/drift-movements/{gen}`,
`/api/matchup/{entry}/conversations`, `/api/search`, the raw
`journal.md`), which stay served for direct HTTP callers. The route
table itself is `READ_ENDPOINTS` in `zicato/dashboard/endpoints.py` plus
the routes `server.py` binds by hand.

On a `state_change` frame the client debounces and performs ONE
`/api/environment` fetch. On a `run_log` frame it performs an
append-only `/api/run-log?after=<cursor>` poll so the log tail GROWS
rather than re-rendering.

## Size envelope

The structural test in `tests/test_dashboard_ui.py` holds the total
bundle under an uncompressed size ceiling. It counts every hand-written
text file the static route serves — `index.html`, `style.css`,
`console.js`, `css/console.css`, the `js/**` modules, this
README and `js/CONTRACTS.md` — and excludes `brand/`, `fonts/` and
`test/`. The bundle
is served from localhost and costs no network time; the ceiling exists
only to keep it from growing without bound.

## Accessibility

- `role` and `aria-label` on interactive regions
- a skip link at the top of the page (visible on focus) that moves focus
  to the region holding the active view (`#main-content`)
- keyboard activation (Enter / Space) on clickable figure marks, the
  lifecycle DAG's nodes, and the live standings and trace rows
- `aria-live="polite"` on the live activity ticker, the run-state label
  and the execution link, so screen readers announce changes without
  interrupting
- `Escape` closes the settings drawer, an open dropdown and the
  hovercard
- a print rule in `style.css` that drops the page background and the
  width cap
