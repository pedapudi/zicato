# Console simplification proposal

This document is a proposal written on 2026-09-05. It describes intended changes
to the console (the browser dashboard served by `zicato dashboard`) and the measurements
behind them. The numbers in §1, §2.5, §2.6 and §2.8 are taken at commit
`74ed7514` of `main` with the command shown beside each. The numbers in
§2.1, §2.2, §2.3, §2.4 and §2.7 are the output of `tools/console_measure.py`
(§6) at commit `7a93f6a3`, the last commit before the stylesheet deletion of
§3.7 and the model move of §3.9. The subcommand is named beside each table,
so a maintainer can re-run the measurement after any change. Every
proposal and the export deletions of §2.2 are implemented, and each ends
with a sentence stating the state the code is in (§3.3 in its decision
paragraph).

The proposal is about the size and shape of the console's implementation.
Every proposal keeps the constraints the console already carries:

- the six-role colour tokens, fit-to-width figures, the hovercard singleton
  and shell-owned navigation of `docs/design/CONSOLE-DESIGN-LANGUAGE.md`;
- the digest-gated render rule of
  `src/zicato/dashboard/static/js/CONTRACTS.md` §4, under which a no-op
  heartbeat writes zero DOM;
- static files served by the Python endpoints, with no runtime node
  packages;
- the import-linter contracts in `pyproject.toml`: the query layer imports
  nothing from the dashboard, and the library imports no driver.

The console may later move into its own distribution behind a declared read
API. The proposals are compatible with that move and do not design it.

## 1. The console in numbers

All paths are under `src/zicato/dashboard/` unless written in full.

| Part | Measure | Command |
| --- | --- | --- |
| Browser code | 31,223 lines of JavaScript: 31,110 in 55 modules under `static/js/` (19 in `views/`, 8 in `core/`, 7 in `builder/`, 21 at the top level) plus the 113-line entry `static/console.js` | `find static/js -name '*.js' \| xargs cat \| wc -l` |
| Largest modules | `svg.js` 4,552 · `views/candidate.js` 2,961 · `views/structure.js` 2,496 · `views/builder.js` 2,052 · `live.js` 1,416 · `shell.js` 1,393 · `views/board.js` 1,205 · `ui.js` 1,159 | `wc -l static/js/**/*.js \| sort -n` |
| Stylesheet | 4,600 lines: `static/css/console.css` 4,399 and `static/style.css` 201; 2,075 rules; 1,579 distinct class selectors; 16 theme blocks holding 275 token lines over 32 distinct `--v2-*` tokens | the script in §2.4 |
| Python | 4,084 lines in 8 files: `endpoints.py` 1,521 · `mutations.py` 806 · `server.py` 603 · `filetree.py` 551 · `sse.py` 398 · `settings_api.py` 113 · `static_assets.py` 49 · package files 43 | `wc -l *.py` |
| Routes | 78 routes under `/api/` plus `/events`; 51 of them are rows of the `READ_ENDPOINTS` table, 27 are hand-written handlers in six factories; 68 carry a declared payload type in `zicato.query.contracts.ENDPOINT_PAYLOADS`; the builder mounts 6 more under `/builder/` and the settings API one path under `/settings/` with GET and POST | `grep -oE '"/api/[^"]*"' server.py endpoints.py \| sort -u \| wc -l`; `python -c 'from zicato.query.contracts import ENDPOINT_PAYLOADS as E; print(len(E))'` |
| Browser tests | 67 files, 30,817 lines of `static/test/*.test.mjs` (34,069 lines under `static/test/` with the harness, fixtures and mock server); 1,293 tests pass; 5,258 assertion calls | `cat static/test/*.test.mjs \| wc -l`; `node static/test/run-all.mjs` |
| Python tests | 22 files `tests/test_dashboard*.py`, 13,724 lines; `tests/data/endpoint_route_snapshot.json` pins 100 recorded responses byte for byte | `wc -l tests/test_dashboard*.py` |
| Served bundle | 1,962,751 characters against the 2,070,000 ceiling in `tests/test_dashboard_ui.py:858` | `cat index.html css/console.css console.js icons.svg $(find js -name '*.js') \| wc -c` (run in `static/`) |
| Churn | 282 commits touched `src/zicato/dashboard/` in the 90 days before the measurement; `console.css` 58 times, `endpoints.py` 36, `views/builder.js` 36, `views/candidate.js` 30 | `git log --since='90 days ago' --name-only --pretty=format: -- src/zicato/dashboard \| sort \| uniq -c` |

Two consumers read the routes: the browser console, and the terminal console
under `src/zicato/tui/`, which reaches the workspace only over HTTP and reads
14 routes.

## 2. Findings

### 2.1 Copied code is a small fraction of the browser code

A token-shingle clone finder (`tools/console_measure.py clones`: normalise
each line by stripping whitespace, collapsing string literals and numbers,
dropping blank, comment and brace-only lines; hash every window of eight
consecutive lines; report every window seen twice; merge the hits of one
file pair whose two spans both overlap into one run, and count the
duplicate copy's lines once per run) gives, over the 30,484 lines of
browser code at `7a93f6a3`:

| Pass | Clone pairs | Lines in the duplicate copies | Share of 30,484 |
| --- | --- | --- | --- |
| exact text, window 8 | 9 | 102 | 0.3 % |
| identifiers replaced by a placeholder, window 8 (`--loose`) | 31 | 318 | 1.0 % |
| identifiers replaced, window 12 (`--loose --window 12`) | 8 | 112 | 0.4 % |

The families the loose pass finds:

- `views/builder.js` carries 95 of the 318 lines' pair-ends (a file pairs
  with itself). The repeated unit is a `controlRow(...)` declaration: one
  per contract knob, 48 in all, each a title and an input over the help
  and default `/builder/config` serves (§3.5). Command: `grep -c
  "controlRow(" static/js/views/builder.js`.
- `console.js` lines 80–96 (the Google Fonts family list), `ui.js` 153–168
  and 201–215, `data.js` 90–108, `shell.js` 48–62 and `facets.js` 73–98 are
  self-repeating lines: lists of similar entries, with nothing to fold.
- `views/instrument.js` `buildLanding` and `views/traces.js` `buildLanding`
  share 14 lines: both open with a page head and a "pick a reflection" list;
  a second pair of 11 lines follows at `instrument.js` 363–381 and
  `traces.js` 199–215.
- `builder/chat.js` 151–163 and `shell.js` 340–354 share the pointer and
  keyboard wiring of a drag handle (11 lines).
- `tree.js` 275–290 repeats its branch-row builder for a second branch kind
  at 296–314 (12 lines); `views/settings.js` 465–477 repeats a picker mount
  at 496–508 (11 lines).

Conclusion: the console's size comes from breadth (many panels, many
figures, many knobs), and from the size of single functions, rather than
from copy and paste. At `74ed7514`, twenty-four functions exceed 150 lines;
the largest are `views/board.js render` 525, `views/epoch.js render` 507,
`dag.js lifecycleDag` 435, `views/candidate.js paintCandidate` 428 and
`buildLiveModel` 377 (in `tournament_model.js` since §3.9). Command: a
20-line script that pairs each column-0 `function` with the next column-0 `}`.

The shared builders in `ui.js` are in use: `dataTable` at 24 sites in
10 views, `section` at 109 sites, `empty` at 74 sites, `gatedSwap` 39 times
in 16 modules. The `renderView` scaffold is used by 4 views (`boards`,
`instrument`, `mutations`, `traces`); the other 11 routed views keep a hand
scaffold, which `docs/dev-guide/09-dashboard-and-query.md` §9.9.4 permits
for a view whose flow diverges.

### 2.2 The figure grammar has 52 exports, one of them unused, and two figures for three of the four tournament structures

`svg.js` exports 52 names (`grep -c '^export' static/js/svg.js`). Counting
call sites outside `svg.js` (`tools/console_measure.py exports`, which
counts `svg.<name>(` and named-import uses per module, whether the module
uses the name itself, and bare references in the tests):

| Callers outside `svg.js` | Exports |
| --- | --- |
| none, and unused inside `svg.js` too | `NS` (one line; `elbowPath` left with the flow figure of §3.3) |
| none in production; used inside `svg.js` and exported for tests | `finiteValues`, `decollide`, `jitterColumn`, `fitInto`, `CHAR_EM`, `fitLabel`, `textPx`, `digestOpts`, `title`, `scale` |
| one | `gauntletFieldBarsDigest`, `elimRadialDigest`, `pairedSlopegraph`, `bumps`, `genDots`, `swissOverview`, `radarSilhouetteDigest`, `sparkbar`, `proposingDigest`, `diversityMatrixDigest`, `diversityMatrix`, `roundTimeline`, `reignGantt`, `extent`, `duelFlow`, `racingScalarTrackDigest`, `radarSilhouette`, `metaLoopLedgerDigest`, `waterfall`, `metaLoopLedger` |
| two to six | `trajectoryStrip`, `trajectoryStripDigest`, `valueBars`, `proposingTracker`, `gauntletFieldBars`, `valueDotPlot`, `racingScalarTrack`, `calibrationTrend`, `calibrationTrendDigest`, `heatmap`, `sideBySideDiff`, `edgeText`, `elimRadial`, `swissLadder`, `survivalFunnel`, `sparkline` |
| many | `fmtPercent` 8 · `CROWN` 14 · `fmtSigned` 22 · `fmt` 115 · `isNum` 571 |

**State.** `svg.NS`, `ui.linkButton` (§2.6) and `data.perJudgeForGen` (§2.3)
are deleted; the exports pass reports no `svg.js` export without a caller.

A figure with one caller is the normal case for a figure that draws one
panel; the count says nothing about whether it earns its place. The
overlap finding is about the tournament figures. Seven builders draw the
five tournament structures; racing, swiss and gauntlet have two each:

| Structure | Builders (lines) | Where each is drawn |
| --- | --- | --- |
| racing | `survivalFunnel` (286), `racingScalarTrack` (203) | funnel: epoch overview, structure view, builder preview; track: live hero, structure view |
| swiss | `swissLadder` (186), `swissOverview` (136) | ladder: live hero, structure view, builder preview; overview: epoch overview |
| single and double elimination | `elimRadial` (237) | epoch overview, structure view, builder preview, live hero (§3.3) |
| gauntlet | `duelFlow` (119), `gauntletFieldBars` (145) | duel flow: rounds view; field bars: live hero, structure view |

The racing, swiss and gauntlet pairs draw different readings (an overview
beside a per-rung ladder; a settled field beside a live one). The
elimination structures have one figure, `elimRadial`, which reads the
served model (`rounds` plus `gen_states`) verbatim (§3.3). The elimination
and swiss structures run only for a contract that opts in through
`experimental.tournament_structures` (`src/zicato/selection/experimental/`),
so their figures draw for opted-in epochs only, while the racing and
gauntlet pairs draw for every epoch. Command: `grep -n "svg.elimRadial("
static/js/views/*.js static/js/live.js static/js/builder/preview.js`.

The five trend figures (`sparkline` 118, `sparkbar` 54, `trajectoryStrip`
134, `calibrationTrend` 138, `racingScalarTrack` 203) read different inputs
(a series, a bar strip, a conversation timeline, calibration points, rung
scalars) and are left alone here.

### 2.3 Sixteen routes have no reader, and the beat payload carries four components the client reads again

**Readers per route.** A script normalises every `/api/...` string literal
in `static/js/**`, `static/console.js` and `src/zicato/tui/**` (template
holes and `{param}` segments both become `{x}`) and matches it against the
79 served routes (78 under `/api/` and `/events`). Control POSTs reach the server through `postControl` and
`postFieldOverride` in `core/api.js`, the transcript delta through
`transcript_stream.js`, and `/events` through `core/sse.js`, so those count
as read. The result (`tools/console_measure.py routes`, which joins
`+`-concatenated literals on one line and matches a hole to any one path
segment), at `7a93f6a3`:

- 62 routes are read by the browser console; 14 routes under `/api/` by the terminal console
  (`/api/health`, `/api/epoch`, `/api/lineage`, `/api/workspace`,
  `/api/live/pipeline`, `/api/active-tournament`, `/api/tournaments`,
  `/api/tournament-structure/…`, `/api/epoch/{id}/cost`,
  `/api/epoch/{id}/trajectory`, `/api/reflections`, and three
  `/api/reflection/{id}/…` reads).
- 17 GET routes have no reader in either console: `/api/active-runs`,
  `/api/config`, `/api/contract-diff/{epoch_id}`,
  `/api/drift-movements/{generation_id}`,
  `/api/epoch/{epoch_id}/analysis.html`,
  `/api/epoch/{epoch_id}/execution-plan`, `/api/epoch/{epoch_id}/journal`,
  `/api/epoch/{epoch_id}/journal.md`, `/api/files`,
  `/api/files/{epoch_id}/{generation_id}/tree`, `/api/heartbeat`,
  `/api/live/execution-plan`, `/api/matchup/{entry_id}/conversations`,
  `/api/run/{run_id}/per-judge`, `/api/search`, `/api/state`,
  `/api/tournaments/{generation_id}`. `CONTRACTS.md` §1 documents six of
  them as operator surfaces for direct HTTP requests; the CLI's report
  command, which is not a console, names `/api/epoch/{id}/analysis.html` as
  the rendered page.
- `data.js perJudgeForGen` had no caller and is deleted, so the
  `/api/generation/{epoch_id}/{generation_id}/per-judge` route it read joins
  the routes no console reads; `fieldStatusSummary` is called by one test
  and no module.

**The beat path reads components the views discard.** On every
`state_change` frame the client fetches `/api/environment` once
(`CONTRACTS.md` §2). `query.judge_view.build_environment` assembles it from
thirteen readers, and `core/state.js applyEnvironment` folds the result into
AppState. Counting reads of the folded fields across the browser code
(`grep -rohE "state\.(epochDef|lineage|bracket|scoreTrajectory|healthReport|epochs)\b" static/js`):

| Environment component | Reader that builds it (lines) | State field | Reads outside `state.js` |
| --- | --- | --- | --- |
| `tournaments` | `build_bracket` (16, plus callees) | `state.bracket` | 0 |
| `score_trajectory` | `build_score_trajectory` (90) | `state.scoreTrajectory` | 0 |
| `health_report` | `build_health_report` (25) | `state.healthReport` | 0 |
| `epoch` | `build_epoch_view` (202) | `state.epochDef` | 1 (a fallback in `views/publication.js:88`) |
| `generations` | `build_lineage_view` (187) | `state.lineage` | 1 (`data.js liveDataSignature`, the cache-bust signature) |
| `epochs` | `build_epochs_summary` (15) | `state.epochs` | 4 |

The views read the same data through `data.js` instead, with a cached GET
per epoch: `D.epoch` from 8 modules, `D.generationsForEpoch` (a
`/api/lineage?epoch=` read) from 7 views, `D.bracket` from 5,
`D.scoreTrajectory` from 6, and `D.activeTournament` — an uncached
`fetch` — from 3 sites (`views/epoch.js:166`, `views/gens.js:290` and
`:399`). So each beat builds the bracket, the trajectory, the health report
and the epoch contract for the client to discard, and each view render
fetches them again for the epoch it shows.

**The candidate page joins ten reads on the client.** `views/candidate.js`
opens with `D.epoch`, then `Promise.all` over `D.generationsForEpoch`,
`D.scoreTrajectory`, `D.bracket`, `D.roundTimeline`, then per candidate
`D.perEntry`, `D.hypothesisAccuracy`, `D.episodeExport`, `D.matchupGrid`,
`D.gate`, `D.perJudgeComparison`, `D.expectations`, `D.perJudgeForRun`,
`D.runHeader`, and `D.racingField` for a racing epoch: fifteen routes for
one page (`grep -nE "\bD\.[a-zA-Z]+\(" static/js/views/candidate.js`). The
functions that resolve and fold them are `resolveCandidate` (206 lines),
`candidateDigest` (156) and the fetch part of `paintCandidate`. Together
they are a cross-endpoint join, which the dev guide's §9.2.5 assigns to the
server ("the server owns every cross-endpoint join"). The server already
serves joins of this kind for a board entry
(`/api/epoch/{id}/eval/{entry}`, read by `D.evalDossier`) and for the
rounds (`/api/epoch/{id}/round-timeline`). `views/epoch.js` makes
15 accessor calls in the same way; `views/home.js` and `views/epoch.js`
both fetch `trajectory`, `tournamentCost` and `calibrationTrend`.

### 2.4 Fifty-eight class selectors are styled and never emitted at `7a93f6a3`

A five-source pass over the stylesheet (`tools/console_measure.py css`)
takes every class in a selector and counts references in (1) string
literals under `static/js/**` and `static/console.js`, (2)
`static/index.html`, (3) Python under `src/zicato/**`, (4) the dynamic
prefixes the JavaScript completes at run time, and (5) `static/test/**`.
Test references are reported separately, because a test naming a class the
console never emits pins nothing. The pass collects every string literal whose
last token ends in `-` and is followed by `+` (`'dn-turn dn-turn-' +
role`) or a template hole (`` `dt-glyph-${kind}` ``); a class that starts
with such a token is matched by prefix. That yields 23 prefixes
(`dn-chip-`, `dn-kind-`, `dn-turn-`, `dn-instr-t-`, `dt-glyph-`,
`dt-logs-t-`, …). A literal whose last token is only the family name
(`` `dn-pill dn-${verdict}` `` in `ui.js verdictPill`) explains a class when
the rest of the class is a bare string literal somewhere in the JavaScript
(`'pending'`, `'deferred'`). `dn-active`, `dn-nav`, `dn-running` and
`dn-status` are kept on that ground.

| Measure | Value |
| --- | --- |
| distinct class selectors | 1,548 |
| rules | 2,012 |
| classes with no static reference in JS, HTML or Python | 148 |
| of those 148, the ones a dynamic prefix explains | 90 |
| classes with no static and no dynamic reference | 58 |
| of those 58, the ones only a test names | 8 (`dt-type-btn`, `dn-set-select`, `dt-swiss-pairings`, `dn-radar-axistick`, `dn-viewhost`, `dt-run-pulse`, `dt-struct-strip`, `dt-type-switch`) |
| rules made only of unreferenced classes | 72, 95 lines |
| classes referenced from one JS site only | 1,087 (70 %) |

The unreferenced families are the remains of retired chrome:

- a breadcrumb (`dn-crumb*`, `dn-crumbs`);
- a top bar and brand block (`dn-topbar*`, `dn-brand*`, `dn-nav-link`);
- inline theme and typeface buttons (`dn-theme-*`, `dn-type-*`,
  `dt-type-*`);
- a connection status dot (`dn-status-dot`, `dn-connected`);
- a hand-rolled diff (`dn-diff-*`) and a paired-slopegraph grid
  (`dn-pslope-grid/-cell/-title`);
- a structure strip (`dt-struct-strip*`, `dt-struct-over`),
  `dt-funnel-card`, `dt-swiss-pairings`, `dt-swiss-round-h`,
  `dn-pairing-*`;
- `dn-alt-*`, `dn-patch-scalar*`, `dn-illustrative-banner`,
  `dn-evalmtx-flip`, `dn-set-panel`, `dn-set-kvrow-static`,
  `dn-set-select`, `dn-viewhost`, `dt-run-pulse`, `dn-radar-axistick`, and
  `mono`, which `style.css` styles beside the `code` element.

Selector counts by component prefix at `74ed7514` show where the
stylesheet's weight sits: `dn-bld` (the builder) 240 selectors, `dt-live` 75, `dn-instr` 72,
`dn-elimflow` 65, `dn-roundtl` 64, `dn-evalmtx` 61, `dn-set` 53,
`dn-metaledger` 48, `dn-funnel` 46, `dn-swissladder` 39, `dn-fieldbars`
39, `dn-scalartrack` 37, `dn-elimradial` 34, `dn-duelflow` 28. The 16
theme blocks hold 275 token lines and are the design language's contract;
they are excluded from every proposal below.

`style.css` (201 lines) is linked once from `index.html` and holds the page
ground, the skip link and the bare-element defaults for the publication
fragment.

### 2.5 Four of the nineteen view modules are libraries or panels rather than views

`shell.js` imports 15 view modules and the router names 15 view ids
(`home`, `epoch`, `gens`, `candidate`, `diff`, `boards`, `board`,
`mutations`, `instrument`, `traces`, `evals`, `publication`, `builder`,
`logs`, `settings`). The other four modules under `views/`:

| Module | Lines | Exports | Imported by | Role |
| --- | --- | --- | --- | --- |
| `structure.js` | 2,496 | 28 | `live.js` (12 names), `candidate.js` (10), `epoch.js` (9), `gens.js` (6), `boards.js` (3), `board.js` (1) | tournament model builders (`buildLiveModel` 377 lines, `racingModel` 133, `gauntletModel` 111, `swissModel` 96, `swissOverviewModel` 70, `structureDigest` 79, …) and the renderers `renderStructure`, `renderRacing`, `renderSwiss`, `renderSingleElim`, `renderDoubleElim`, `standingsTable` (205) |
| `boardstatus.js` | 460 | 3 | `epoch.js` | the train/holdout panel of the epoch page |
| `ledger.js` | 186 | 3 | `epoch.js` | the experiments table of the epoch page |
| `evals_health.js` | 488 | 6 | `evals.js`, through `import('./evals_health.js')` guarded by `.catch(() => {})` and a `try` around the mount | the instrument-health strip and section of the evals page |

`buildLiveModel` is called only from `live.js` (lines 1022 and 1212) and
from three one-line wrappers in `structure.js`; the live hero's model
builder lives in a view module. The dynamic import in `views/evals.js`
lines 594–607 exists so that two branches could merge without editing each
other's lines (its own comment says so); its `catch` swallows any load or
render failure of the health panel.

The views the question names, and what each shows:

- `evals.js` (606) draws the entries-by-candidates matrix from
  `/api/epoch/{id}/evals`; `evals_health.js` draws noise-floor, dead-eval,
  redundancy and cost panels from `/api/epoch/{id}/eval-health` into two
  hosts `evals.js` owns. One page, two payloads, two modules.
- `boards.js` (212) is the trellis, one small multiple per board entry;
  `board.js` (1,205) is one entry across every candidate with the inline
  transcript, judges panel, facets, attribution and reflection findings;
  `boardstatus.js` is the epoch page's split panel. The first two are
  different levels of the same hierarchy (a route each); the third is a
  panel.
- `gens.js` (543) is the rounds page: the champion-defends banner, the
  per-round structure figure, the roster table, and a round drill-down;
  `candidate.js` (2,961) is one generation: lifecycle, per-board scoring,
  match-ups, gate, rung ladder, field standings, prediction accuracy,
  generalization, entry drill-down. They share the gate read, the
  `dataTable`/`deltaCell`/`ratingCellEl` verdict rows and the structure
  model resolution from `structure.js`. Merging them would put a
  per-generation page inside a per-epoch page, and nothing they could
  share is left unshared.
- `instrument.js` (873) and `traces.js` (336) share a reflection-picker
  landing (14 cloned lines) and otherwise draw different payloads.
- `diff.js` (488) and `mutations.js` (308) both read
  `/api/mutations/{epoch}/{site}` and `/api/files/{e}/{g}/patches` and both
  call `svg.sideBySideDiff`; `diff.js` is per candidate (and carries the
  baseline picker and the expandable diff, 117 lines), `mutations.js` is
  per site.

### 2.6 The live path is written once, and its model builder lives in a view

The change-signal plumbing exists in one place: `core/sse.js` (142 lines)
folds frames into AppState, which emits `state:changed`; `shell.js` is the
only subscriber (`grep -rn "bus.on('state:changed'" static/js` gives one
hit, `shell.js:708`); `onStateChanged` compares `liveDataSignature()` and
dispatches. Per-view gating uses `gatedSwap` (39 calls in 16 modules);
`shell.js` keeps 14 inline `_last*Digest` guards for the chrome (tree,
status, loop controls, breadcrumb) instead of hosts.

What each of the four modules owns:

| Module | Lines | Owns |
| --- | --- | --- |
| `live.js` | 1,416 | `LiveController` (536 lines, lines 709–1245: the band, the drawer, the live figure, the match rows), `ActivityTicker`, `liveProgress` (67), `deriveActivity` (62), `liveSnapshot`, the pipeline and rung steppers, the kill and follow buttons. One export is imported elsewhere (`LiveController`, by `shell.js`); the other ten are exported for tests. |
| `livestatus.js` | 547 | `deriveLiveStatus` (119) and `deriveLiveness` fold heartbeat phase, active runs and the active tournament into one verdict; `livenessFor` is the entry point 13 modules import; `liveStatusDigest`, `treeLiveSet`, the labels. Seven exports have no importer; all seven are used inside the module and by tests. |
| `shell.js` | 1,393 | `mountShell` (241), `buildTreeModel` (177), `wireRailHandle` (77), `renderStatus` (75, the chrome status pill), `buildLoopControls` (46), `renderLoopControls` (30), `dispatch` (56), `dispatchSettingsOverlay` (43), `onStateChanged` (33), `refreshLive` (21). |
| `ui.js` | 1,159 | 85 exports: `gatedSwap`, `renderView`, `dataTable`, `section`, `empty`, the pills, `overrideControlCell` (79), `renderMarkdown` (74), the theme and typeface tables, `persistColor` (41). Ten exports have no importer; one is test-only (`_resetPendingOverrides`, 13), the rest are used inside the module (`linkButton`, which no site called, is deleted). |

Three functions derive liveness at different scopes and compose rather than
repeat: `livestatus.livenessFor` (the loop), `unit_liveness.unitLiveness`
(one board unit; it composes the loop verdict with the active-run record),
and `live.liveProgress` (progress of the running tournament).
`buildLiveModel` (377 lines) and the three `buildLive*Model` wrappers are
called only from `live.js`; they are in `tournament_model.js` (§3.9), so
the live hero imports no view module.

### 2.7 Tests pin markup in about a third of their assertions, and endpoint-keyed fixtures pin the route table

The harness exports `assert`, `assertEqual` and `assertDeep`. Classifying
every assertion line in the 67 test files (`tools/console_measure.py
assertions`: a line that quotes a class literal counts as the first row,
otherwise one naming a DOM property as the second, otherwise one calling
`querySelector` as the third), 5,297 lines at `7a93f6a3`:

| Assertion names | Count | Share |
| --- | --- | --- |
| a `dn-`/`dt-` class literal | 859 | 16 % |
| `textContent`, `innerHTML`, an attribute, `tagName`, `classList` or `dataset` | 1,006 | 19 % |
| `querySelector` inside the assertion | 8 | 0 % |
| none of these (a digest string, a count, node identity across two renders, a fetched path, a thrown error, a model field) | 3,424 | 65 % |

A further 1,195 non-assertion lines select DOM or name a class to find the
node under test. 699 distinct `dn-`/`dt-` class names appear in tests, 44 %
of the 1,579 the stylesheet defines. The dev guide §9.9.4 makes class names
the stable contract, so a builder that emits the same classes passes these
tests; a proposal that renames or removes a class must touch them.

What the tests pin per module (test lines in files that import the module):
`views/epoch.js` 12,938 lines across 15 files for 842 production lines;
`views/candidate.js` 12,339 across 17 files; `views/board.js` 9,651 across
9; `views/gens.js` 7,115 across 8; `router.js` 5,699 across 18;
`data.js` 6,594 across 19. Eight production modules are imported by no
test file (`dropdown.js`, `matrix.js`, `tree.js`, `compare.js`,
`core/admission_viz.js`, `core/prefs.js`, `builder/preview.js`,
`builder/api.js`); `tree.js` and `compare.js` are exercised through
`shell.js` and the views.

Two properties of the fixtures decide the cost of any endpoint change:

- Fixture maps are keyed by route path: 202 keys of the form
  `'/api/...':` across 22 test files, 118 of them in the 17 files that
  import `views/candidate.js`. Merging or reshaping a route rewrites those
  keys.
- `static/test/mock_server.mjs` (445 lines) re-implements two Python
  readers (`build_round_timeline`, `build_racing_field`) in JavaScript so
  that fixtures written against the granular routes can produce the served
  joins. Its header says any divergence from the Python readers is a bug in
  the file. The Python side already records every table route's response
  in `tests/data/endpoint_route_snapshot.json` (100 probes).

### 2.8 The Python side is a route table over the query layer, plus two readers that live in the wrong package

`endpoints.py` (1,521 lines) holds 51 `ReadEndpoint` rows, every one of
each of which calls one `zicato.query` reader (`grep -oE "reader=[A-Za-z_.]+"
endpoints.py | grep -vc "query\."` gives 0), and six hand-written
factories with 27 handlers: `_make_control_endpoints` 176 lines (7 POST
handlers), `_make_conversation_endpoints` 134 (4 handlers that parse `run`,
`after` and `limit` query parameters), `_make_files_endpoints` 89
(7 handlers), `_make_state_endpoints` 79 (5), `_make_epoch_document_endpoints`
48 (2), `_make_proposal_episode_endpoints` 48 (2). The hand-written handlers
parse query parameters or serve a media type other than JSON; none shapes a
payload the reader could not shape.

The seven file and mutation handlers call readers in the dashboard package:
`filetree.py` (551 lines: `build_file_index`, `build_generation_tree`,
`read_generation_file`, `build_generation_patches`, `build_generation_diff`)
and `mutations.py` (806 lines: `build_mutation_index`,
`build_mutation_detail`, `reconstructed_spans`). They import
`zicato.epoch.genstore`, `zicato.epoch.journal` and
`zicato.mutation.enumerator`; the query layer already imports
`zicato.epoch.journal`, `zicato.epoch._storage`, `zicato.epoch.preflight`,
`zicato.storage` and `zicato.workspace`, so nothing in the import-linter
contracts keeps these two modules out of `zicato.query`. They are the only
reader code under `src/zicato/dashboard/`; there is no second projection of
the workspace beside `query/` (the snapshot and environment builders are
`query.runtime_view.build_snapshot` and `query.judge_view.build_environment`).

`server.py create_app` (240 lines) lists the routes inline; `sse.py` (398)
is the change broker with its filesystem watcher and coalescing debounce;
`settings_api.py` and `static_assets.py` are small.

## 3. Proposals

Each proposal names the change, the estimated line delta (production, then
tests), what an operator sees, the risk and the check that catches it, and
what it depends on. Line deltas are estimates from the function sizes in
§2; a minus sign is a reduction.

### 3.1 Serve the candidate dossier from the query layer

**Change.** Add one reader, `query.build_candidate_dossier(paths, epoch_id,
generation_id)`, that returns what `views/candidate.js` assembles from
`per-entry`, `hypothesis-accuracy`, `episode-export`, `matchup-grid`,
`gate`, `per-judge-comparison`, `expectations`, `per-judge` and `header`,
plus the racing field when the epoch is racing. Add it to `READ_ENDPOINTS`
as `/api/epoch/{epoch_id}/candidate/{generation_id}` with a
`DetailPayload` entry, in the pattern of `/api/epoch/{id}/eval/{entry}`.
`resolveCandidate` reads the one payload; `candidateDigest` folds it. The
granular routes stay served (the board view and the publication read
`matchup-grid` and `per-entry` on their own).

**Delta.** Browser: about −450 lines (`resolveCandidate` 206,
`candidateDigest` 156, the fetch fan-out of `render` and `paintCandidate`,
replaced by about 60). Python: about +300 (the reader, a table row, the
contract entry). Tests: the 118 route-keyed fixture entries in the 17
candidate test files become one entry each (about −300 lines after §3.8),
plus a reader test and one golden probe (+150).

**Operator sees.** The same panels. One request per candidate page instead
of ten to fifteen; the compare split (`~cmp=`) makes two.

**Risk and check.** The reader must return the server's verdicts unchanged
(`decided_by`, `won_by`, the gate's `deciding_rule`) rather than re-derive
them; `tests/test_dashboard_reader_parity.py` and the recorded golden catch
a reshaped field, and the node suites `candidate_surfaces`,
`candidate_identity`, `lifecycle_dag`, `bt_rating_gate`,
`gate_absolute_scalars`, `prediction_calibration`, `episode_export_link`
pin the rendered result. The racing branch (`D.racingField`) needs the
`mock_evolve_racing` golden workspace as a fixture.

**Depends on.** §3.8 first makes the fixture rewrite cheap; otherwise
independent.

**State.** Implemented. `query.build_candidate_dossier` (in
`src/zicato/query/candidate_view.py`) joins the per-entry read, the
prediction scorecard, the episode export, the matchup grid, the gates the
candidate stood at with their per-judge comparisons, the drill-down
`?entry=` names and, on a racing epoch, the racing field, calling the same
readers the granular routes call, so every verdict on the page is the one
those readers serve. The route
`/api/epoch/{epoch_id}/candidate/{generation_id}` is a row of
`READ_ENDPOINTS` with a `DetailPayload` contract, pinned in the endpoint
golden and the reader-parity snapshot; `views/candidate.js` reads one
dossier per candidate shown through `D.candidateDossier`, and the six
accessors the page alone used are deleted from `data.js`. The candidate
suites render recorded dossiers, and vary one served field on a recorded
copy (`dossierVariant`) where they pin a panel's rendering of that field.

### 3.2 Trim the environment payload to the components the client reads from state

**Change.** Remove `tournaments`, `score_trajectory`, `health_report` and
`epoch` from `build_environment` and from `applyEnvironment`; delete the
state fields `bracket`, `scoreTrajectory`, `healthReport` and `epochDef`
and the `_foldEpoch` step; change the one fallback in
`views/publication.js:88` to the `D.epoch` read the view already makes.
Keep `generations` (the cache-bust signature reads it), `epochs`,
`workspace`, `active_runs`, `active_tournament`, `heartbeat`, `liveness`,
`lock` and `run_log`. Update `CONTRACTS.md` §1 and §3.

**Delta.** Python: about −15 lines in `build_environment`. Browser: about
−40 in `core/state.js`, −3 in `views/publication.js`. Tests: the
`/api/environment` assertions in `tests/test_dashboard_server.py` (7
references) and `static/test/core.test.mjs` change with the shape; node
fixtures construct no environment payload with these keys (`grep -l
"score_trajectory\s*:" static/test/*.mjs` gives none).

**Operator sees.** Nothing. Each change-signal beat stops running
`build_epoch_view` (202 lines), `build_score_trajectory` (90),
`build_health_report` (25) and `build_bracket`; a beat that arrives while a
run is writing does less file reading.

**Risk and check.** A reader of a deleted state field that the grep missed.
The measurement (`grep -rnE "state\.(epochDef|bracket|scoreTrajectory|healthReport)\b" static/js`)
finds one production read; the node suite and `tests/test_dashboard_server.py`
run after the change confirm it. The TUI does not read `/api/environment`.

**Depends on.** Nothing.

**State.** Implemented. `build_environment` serves the ten kept components,
`AppState` holds no `bracket`, `scoreTrajectory`, `healthReport` or
`epochDef` field, and `views/publication.js` resolves the epoch id from its
own `D.epoch` read. `tests/test_dashboard_server.py` pins the four keys
absent and `static/test/core.test.mjs` pins that folding them changes no
state.

### 3.3 Draw one elimination figure

**Change.** Delete `svg.elimRadial` and `elimRadialDigest`, the
`dn-elimradial-*` rules, the radial card and the combo/radial toggle in
`views/structure.js renderSingleElim` and `renderDoubleElim`, and the
single-elimination branch of `live.js _buildLiveFigure` that picks the
radial; `elimFlow` draws every elimination structure, as
`CONSOLE-DESIGN-LANGUAGE.md` §4.1 states. The hero cap
`svg.dn-elimflow-hero` exists in `console.css`, so the live hero keeps its
width discipline.

**Delta.** Browser: −257 in `svg.js`, about −45 in `views/structure.js`,
about −15 in `live.js`. Stylesheet: −65 lines (29 rules). Tests: 10
`elimRadial` references and 18 `dn-elimradial` class references across
`figures`, `live_hero`, `live_protocol`, `live_waves` (about −120 lines,
some rewritten to the flow).

**Operator sees.** The single-elimination structure page and the live hero
for a single-elimination epoch show the bracket-as-flow instead of the
concentric rings; the double-elimination page loses the combo/radial
toggle. Both pages exist only for an epoch whose contract opts into the
experimental structures. This is the one proposal with a visible change,
and it needs the operator's choice of which figure survives; the design
document names the
flow.

**Decided.** The radial is the one elimination figure: `elimRadial` draws
every elimination surface, and `elimFlow`, its digest, the `dn-elimflow-*`
rules and the double-elimination figure toggle are deleted; §4.1 of the design
document names the radial.

**Risk and check.** Visual. Check with a review of the structure page and
the live hero on the `mock_evolve_single_elim_full` and
`mock_evolve_double_elim_full` workspaces (the parity lanes in
`tools/parity`), and with `bracket.test.mjs`, `tournament_structures.test.mjs`,
`live_hero.test.mjs`. Update §4.1 of the design document if the radial is
the one kept instead.

**Depends on.** Nothing.

### 3.4 Move the file and mutation readers into the query layer as table rows

**Change.** Move `filetree.py` and `mutations.py` to
`src/zicato/query/file_view.py` and `src/zicato/query/mutation_view.py`,
give each of the seven routes a `ReadEndpoint` row (with `_echo` degrades
for the coordinate forms `filetree._missing_tree_error` produces),
and delete `_make_files_endpoints`. `zicato.analyzer.report_data` already
refers to `zicato.dashboard.mutations` in a docstring only.

**Delta.** Dashboard Python: −1,357 (the two modules), −89 (the factory),
about −10 of imports. Query: +1,357 and the table rows (+60 in
`endpoints.py`, +7 contract entries). Net about −100 lines; the visible
gain is that `src/zicato/dashboard/*.py` holds no workspace reader, which
is the boundary a console distribution behind a read API needs.

**Operator sees.** Nothing.

**Risk and check.** The degrade shapes for a rejected coordinate must match
byte for byte; the endpoint golden gains seven probes and
`tests/test_dashboard_endpoint_table.py` checks each row's degrade against
its payload type. `tests/test_dashboard_filetree.py` and
`tests/test_dashboard_mutations.py` move with the modules. `uv run
lint-imports` proves the query layer still imports no driver.

**Depends on.** Nothing. It is a prerequisite for declaring the read API.

**State.** Implemented. `src/zicato/query/file_view.py` and
`src/zicato/query/mutation_view.py` hold the readers, and the seven routes
are rows of `READ_ENDPOINTS` serving the degrade bodies and status codes the
hand-written handlers served, pinned by fourteen probes in the endpoint
golden. `tests/test_dashboard_endpoint_table.py` holds every GET route
outside a declared hand-written set to being a table row, and the route
module's `zicato` imports to `zicato.query`.

### 3.5 Serve the builder's knob help from the scoring configuration

**Change.** `zicato.core.scoring_config` documents 45 knobs in field
docstrings (`grep -cE "^    [a-z_]+:$" src/zicato/core/scoring_config.py`)
with their defaults; `views/builder.js` repeats a help paragraph and a
default for 48 `controlRow` calls. Extend `/builder/config` — which already
serves the enum vocabulary so that "the JS never hardcodes an enum" — with
`{knob: {help, default}}` extracted from the field docstrings, and have
`controlRow` read them by knob key, keeping only the title and the input
in the JS.

**Delta.** Browser: about −190 lines (the `body:` and `def:` lines of 48
rows). Python: about +80 (the extractor and a test that every knob the
builder names is served). Tests: `builder.test.mjs` fixtures gain the
served help; assertions that quote help text (few; 26 of its 263 assertions
name a class) change.

**Operator sees.** The same rows; where the two texts differ, the served
one wins, so the help reads as the configuration reference does.

**Risk and check.** A knob the builder names that the extractor does not
find; the Python test enumerates the builder's knob keys against the served
map. `builder.test.mjs` and `builder_board_editor.test.mjs` pin the rows.

**Depends on.** Nothing.

**State.** Implemented. `zicato.builder.knob_help` reads the `Fields`
section of each scoring-config dataclass docstring, `GET /builder/config`
serves it as `knob_help` keyed by contract path, and every scoring-knob row
of `views/builder.js` reads its help and default through `knobInfo(key)`;
`tests/test_knob_registry.py` fails for a builder knob with no docstring
entry and for a row key the server does not serve. The rows for the
structure parameters and the proposer directory keep their own text, since
neither is a scoring-config field.

### 3.6 Import the eval-health panel statically and file the panel modules as panels

**Change.** Replace the guarded `import('./evals_health.js')` in
`views/evals.js` with a static import and a direct call; move
`views/boardstatus.js`, `views/ledger.js` and `views/evals_health.js` to
`static/js/panels/`, so `views/` holds the fifteen routed views and
`panels/` the page sections other views compose.

**Delta.** Browser: −12 in `views/evals.js`; the moves are 0 lines. Tests:
import paths in `boardstatus.test.mjs`, `experiments_ledger.test.mjs`,
`evals_health.test.mjs`.

**Operator sees.** Nothing, except that a failure inside the health panel
surfaces in the console log instead of being swallowed.

**Risk and check.** None beyond the suite; `evals.test.mjs` and
`evals_health.test.mjs`.

**Depends on.** Nothing.

**State.** Implemented for the eval-health panel. `views/evals.js` imports
`panels/evals_health.js` statically and calls its `mount` directly, so a
failure inside the panel reaches the console log. `boardstatus.js` and
`ledger.js` still sit under `views/`; moving them edits the import lines of
`views/epoch.js`, which remains to do.

### 3.7 Delete the unreferenced stylesheet rules

**Change.** Remove the 73 rules (about 170 lines) whose every class the
five-source pass finds nowhere, and the test lines that name the ten
test-only classes. Keep the pass as a script under `tools/` so a later
retirement of chrome can re-run it.

**Delta.** Stylesheet: −170. Tests: about −20. The 29 `dn-elimradial`
rules of §3.3 are separate.

**Operator sees.** Nothing.

**Risk and check.** A class emitted by a construction the prefix rule
misses. The rule catches `'prefix-' + x` and `` `prefix-${x}` ``; a class
assembled any other way would be a third form to add to the script. The
node suite covers every rendered panel; a visual pass over the settings
page and the publication (the two pages whose families appear in the list)
closes the check.

**Depends on.** Do after §3.3 so the pass runs once over the final set.

**State.** Implemented. `console.css` holds no rule whose every selector
names an unreferenced class: the 72 rules (95 lines) the pass found at
`7a93f6a3` and the `.dn-evalmtx-flip` selector are deleted, with the nine
test lines that named the eight test-only classes.
`tools/test_console_measure.py` holds the pass at zero dead rules.

### 3.8 Feed the node fixtures from the recorded endpoint responses

**Change.** The Python golden `tests/data/endpoint_route_snapshot.json`
records every table route's response over the fixture workspace. Give the
node harness a loader for that file, so a view test can stub `fetch` with
recorded payloads keyed by route instead of hand-written maps. Delete
`static/test/mock_server.mjs` once the tests that rely on its two derived
joins read the recorded `round-timeline` and `racing-field` responses.

**Delta.** Tests: −445 (`mock_server.mjs`), about +80 (the loader), and a
gradual shrink of the 202 hand-written fixture keys as suites adopt it.
Production: 0.

**Operator sees.** Nothing.

**Risk and check.** A recorded fixture that lacks a case a hand-written map
covered (an empty rung, a pending gate); the adopting suite keeps a
hand-written override for that case. The golden is re-recorded by the
existing `tests/test_dashboard_endpoint_table.py` procedure, so the two
suites cannot drift from each other.

**Depends on.** Nothing; it lowers the cost of §3.1 and of every later
route change.

**State.** Implemented. `static/test/recorded.mjs` serves the responses
`tests/data/endpoint_route_snapshot.json` records over the workspaces
`tests/_console_scenarios.py` writes, keyed by the URL in
`tests/data/endpoint_route_probes.json`, and the elimination folds a suite
draws come from `tests/data/elim_states_served.json`, recorded over the round
lists `tests/data/elim_states_cases.json` declares. `mock_server.mjs` is
deleted, and the fixture module derives no served join: the round timeline,
the racing field, the matchup grid and the elimination model a browser test
renders are each a recorded server response.

### 3.9 Move the tournament model builders out of the structure view

**Change.** Split `views/structure.js`: the model builders
(`buildLiveModel`, `buildLive*Model`, `racingModel`, `swissModel`,
`swissOverviewModel`, `elimModel`, `gauntletModel`, `structureDigest`,
`normalizeStructure`, `resolveNonGauntletSt`, the liveness helpers; about
1,700 lines) become `static/js/tournament_model.js`; the renderers
(`renderStructure` and the per-structure renderers, `standingsTable`,
`diversitySection`; about 800 lines) stay as the view. `live.js` then
imports from a model module rather than from a view.

**Delta.** 0 production lines. Tests: import paths in
`tournament_structures.test.mjs`, `live_*.test.mjs`.

**Operator sees.** Nothing.

**Risk and check.** None beyond the suite.

**Depends on.** Nothing.

**State.** Implemented. `static/js/tournament_model.js` (1,758 lines) holds
the model builders, the resolver, the digests and the liveness helpers, and
`views/structure.js` (661 lines) holds the renderers, the standings table
and the diversity ribbon and imports the models it draws; `live.js` imports
the model module and no view.

## 4. Ordering

1. **§3.6, §3.7 and the two dead exports of §2.2** (`svg.NS`, `svg.elbowPath`,
   `ui.linkButton`, `data.perJudgeForGen`): mechanical, no visible change,
   about −200 lines; they also exercise the measurement scripts this
   document rests on, which should land under `tools/` in the same change.
2. **§3.2** (trim the environment payload): small, independent, and it
   removes server work from every beat.
3. **§3.4** (file and mutation readers into the query layer): Python only;
   after it, the dashboard package holds no reader, which the read-API
   declaration needs.
4. **§3.8 then §3.1** (recorded fixtures, then the served candidate
   dossier): the largest browser reduction; the fixture step first, so the
   17 candidate suites are rewritten once.
5. **§3.3** (one elimination figure): after the operator chooses the figure;
   independent of the rest.
6. **§3.5 and §3.9** (served knob help; the model module): independent,
   any time.

Total, if every proposal lands: about −950 browser lines, −235 stylesheet
lines, −1,470 dashboard Python lines against about +1,740 lines in the query
and builder packages (the readers move, one reader and one extractor are
added), and about −650 test lines. The bundle
ceiling in `tests/test_dashboard_ui.py` and the repository line budget both
move down; the budget ledger records the query-layer increase as the
counterpart of the dashboard decrease.

## 5. Recommended against

- **Deleting the 16 GET routes with no reader.** Each is a table row of
  about eight lines over a reader that other routes or the CLI may share;
  `CONTRACTS.md` names six as operator surfaces; the recorded golden pins
  all of them. The read-API declaration is the place to decide which
  routes the console distribution exposes; §2.3 lists them for that
  decision.
- **Merging `boards`/`board`/`boardstatus` or `gens`/`candidate`.** The
  pairs are levels of one hierarchy with a route each, they share the
  builders they can share, and merging removes a page the tree links to.
  §3.6 files the panel among panels instead.
- **Reducing the 16 themes or the 12 typefaces.** 275 token lines are the
  design language's contract, and a theme swap is a pure re-skin; there is
  no implementation weight to remove.
- **Forcing `renderView` onto the 11 hand-scaffolded views.** The dev guide
  §9.9.4 keeps the hand scaffold for a view whose flow diverges; the
  measured saving is under 15 lines per view.
- **Rewriting the 833 assertions that name a class.** Class names are the
  stable contract that lets a shared builder replace a hand-rolled block
  without touching tests; the assertions are the mechanism that makes the
  proposals above checkable.
- **Splitting the giant render functions for their own sake.** `board.js
  render` (525 lines) and `epoch.js render` (507) would become the same
  lines in more files, in the two modules with the highest churn (29 and
  19 commits in 90 days); §3.1 shrinks the candidate page by moving a join,
  which is the kind of split that removes lines.
- **Pruning exports with no importer.** The measurement finds 7 dead lines;
  the remaining un-imported exports are used inside their module or by a
  test.
- **Unifying the five trend figures.** They read five input shapes; a
  common builder would carry five option branches.

## 6. Measurement scripts

The scripts behind §2 are short and are meant to be re-run:

- **Clone finder** (§2.1): normalise lines, hash eight-line windows, merge
  overlapping hits into runs, report pairs and per-file totals; a `--loose`
  flag maps identifiers to one placeholder.
- **Export call counts** (§2.2): for every `export` in `svg.js`, count
  `svg.<name>(` and named-import uses in every other module, and bare
  references in `static/test`.
- **Route coverage** (§2.3): normalise every `/api/` literal in the browser
  and terminal consoles to `{x}` segments and match it against the served
  routes.
- **Five-source class liveness** (§2.4): every class in a selector against
  JS literals, HTML, tests, Python and dynamic prefixes.
- **Assertion classifier** (§2.7): every `assert*` line by what it names.
- **Test-to-module map** (§2.7): every test file's imports, with the test
  lines summed per production module.
- **Function sizes** (§2.1, §2.6): column-0 `function` to the next
  column-0 `}` for JavaScript; `ast` for Python.

`tools/console_measure.py` holds the first five as subcommands (`clones`,
`exports`, `routes`, `css`, `assertions`), each printing a table or, with
`--json`, one object; `tools/test_console_measure.py` runs every subcommand
on the tree. The test-to-module map and the function-size pass are not in
the tool.
