# Dashboard frontend — pinned contracts

Every frontend module codes against the contracts stated here: the shape
of each JSON payload the server serves, the change-signal frame types,
the client state object, the render discipline, and the hash routes. A
payload-shape change is a clean break — server and client change in the
same commit, and this file changes with them.

The frontend is split into disjoint modules:

```
static/
  index.html            — page shell hosting `#console-root`
  css/console.css       — every design token and every SVG mark class
  console.js            — ES-module entry point
  js/
    core/               — the data/render spine
      dom.js            — el, svgEl, clearChildren, patch helpers
      state.js          — AppState: the single client state object
      api.js            — fetchJson, loadEnvironment, postControl
      sse.js            — EventSource wiring + delta dispatch
      bus.js            — a small publish/subscribe event bus
      harmonograf.js    — harmonograf URL builders
      prefs.js          — the persisted per-viewer preference store
      admission_viz.js  — the suggestion-admission figures
    router.js           — hash routing + deep links
    shell.js            — chrome, sidebar-to-detail host, page-scale pill
    ui.js               — gatedSwap, pills, tables, themes, typefaces
    svg.js, dag.js      — the figure builders
    matrix.js           — the dn-mtx table-grid primitives
    data.js             — the per-epoch read accessors
    views/              — one module per routed detail pane
    panels/             — page sections a view composes; no route
  test/                 — JS/DOM test harness
    harness.mjs         — minimal DOM + assert harness
    *.test.mjs          — per-module behaviour tests
```

---

## 1. The `/api/environment` snapshot shape

`GET /api/environment` returns ONE coalesced read. Every component
degrades independently — a missing input is `null`/`[]`, never an
exception. It carries only what the client folds into state (§3); the
epoch contract, the bracket, the score trajectory and the health report
are served by their own routes, which a view reads through `data.js`
when it renders. Shape (as produced by `query.judge_view.build_environment`):

```jsonc
{
  "workspace": { "root": "/abs/path/.zicato", ... },  // build_workspace_identity
  "epoch_id": "2026-05-18_presn" | null,
  "epochs": [ { "epoch_id": str, "goal": str|null } ],  // per-epoch goal summary
  "active_tournament": { ...tournament... } | null,
  "generations": { "generations":[...], "experiments":[...] },
  "active_runs": [ { ..., progress:0..1|null, elapsed_seconds, budget_seconds,
                      adk_session_id:str } ],
  "heartbeat": { generation_id, round_index, last_heartbeat,
        round_started_at, started_at, harmonograf_url?,
        harmonograf_persistent? } | null,
  "lock": { ... } | null,
  "run_log": { "events":[{seq,kind,ts,summary}], "cursor":int, "events_path":str },
  "generated_at": "ISO-8601 Z"
}
```

`epochs` is a lightweight per-epoch summary list — one
`{ epoch_id, goal }` row per epoch directory on disk. `goal` is a
one-line description distilled from that epoch's proposer brief (the
`## Goal` section of `brief.md`), or `null` when the brief is absent or
carries no goal. It lets the home view's epochs table annotate each row
with what the epoch is trying to accomplish without a per-epoch
`/api/epoch` fetch. Folded into AppState as `state.epochs`.

`GET /api/health` (separate, fetched ONCE) is the dashboard *service*
identity: `{ status, version, port, build, uptime_seconds, read_only,
workspace }`. It is NOT in the environment payload.

The drill-down / lazy endpoints (unchanged):
- `GET /api/run-log?after=<cursor>` — append-only tail batch.
- `GET /api/tournaments/{gen}` — per-matchup detail. **No client reads
  it**; it is an operator surface for direct HTTP requests. The match-up
  surfaces read `/api/tournaments` (the bracket) and
  `/api/matchup-grid/...` instead (§3).
- `GET /api/drift-movements/{gen}` — drift-kind movements. **No client
  reads it**; operator surface only.
- `GET /api/score-trajectory` — `{ epoch_id, points:[{generation_id,
  parent_generation_id, promoted, scalar, entry_count, created_at}] }`.
- `GET /api/files...`, `GET /api/mutations/...` — the patch-diff and
  mutation-surface panes.
- `GET /api/conversation/{run}` — the run_id-keyed transcript
  (`D.conversation()`; `D.runTranscript()` is the preferred gen×entry read).
- `GET /api/generation/{epoch}/{gen}/episode-export` — whether that
  candidate's proposal episode has Foe's static page, plus the episode log's
  path and the command that renders one (`D.episodeExport()`). The page
  itself is a whole HTML document served at the same path with an `.html`
  suffix (`D.episodeExportHref()`), opened in a new tab and never fetched.
- `GET /api/run/{epoch}/{gen}/{entry}/transcript/delta?after=<cursor>` — the
  live conversation pane's **cursor-append** read. It returns only what
  changed since `after`, so following a running unit never re-sends a
  settled conversation:

  ```jsonc
  { "found": true, "cursor": int,            // feed back as the next `after`
    "turns": [ { ...turn, "turn_index": int } ],   // only new/changed turns
    "annotations": [ { ...annotation } ],
    "turn_total": int, "event_count": int,
    "complete": bool,                        // a terminal event was seen
    "truncated": bool,                       // delta exceeded `limit` — the
                                             // client must re-read in full
    "fidelity": "events", "verbatim_available": bool,
    "events_path": str|null }
  ```

  The **cursor counts parsed events** rather than goldfive `sequence`
  numbers: a `multi_turn_emulated` entry restarts `sequence` at 0 per run,
  so only the parsed-event count is monotone over the file. Being a count it sits one
  past the last index it covers, so the server's filter is **inclusive**
  (`source_index >= after`) — which is what re-delivers the OPEN final turn
  when it grows, at its existing `turn_index`. Replaying an unchanged cursor
  therefore yields an empty delta. A torn final line takes no cursor
  position, so it arrives whole on a later poll. `limit` is clamped by the
  run-log's own `clamp_run_log_limit`. This is a SEPARATE route from
  `/transcript` above, which keeps its full-payload shape for the
  side-by-side panes.
- `GET /api/matchup/{entry_id}/conversations` — **no client reads it.**
  It is an operator surface for direct HTTP requests. The champion-versus-
  challenger transcript comparison the console offers is the board view's
  inline side-by-side, which resolves each side through the deterministic
  `(epoch, gen, entry)` triple (`D.runTranscript()`) rather than through
  an entry-keyed pair. The endpoint stays served for external callers.
- `GET /api/epoch/{epoch_id}/journal.md` — **no client reads it.** The raw
  `journal.md` bytes are an operator surface; the console renders the same
  text from the `journal` field the `/api/epoch` payload already carries,
  so a second fetch of the same file would duplicate it.
- `GET /api/search` — **no client and no CLI command read it.** Consuming
  it requires a search affordance in the chrome and a results route; the
  console has neither.
- `GET /api/matchup-grid/{epoch}/{champion}/{challenger}` — the
  per-entry A/B grid for a **completed** matchup, read straight off the
  persisted per-run loss files (NOT the SQLite index). `/api/tournaments/{gen}`
  sources its `ab_grid` from the analytical index, a best-effort
  dual-write; a finished tournament whose index was never rebuilt
  carries an empty `ab_grid`, so a match-up detail read off that payload
  has no per-board outcomes. This endpoint reconstructs them from
  `generations/{gen}/runs/{entry}/loss.json` (the reducer's `LossProfile`)
  for both generations plus the `generations/{gen}/gen_score.json`
  aggregates. Shape:
  ```jsonc
  {
    "epoch_id", "champion", "challenger",
    "drift_present": bool,       // false ⇒ hide every drift readout
    "entry_grid": [ { "entry_id",
        "parent_drift_loss":num|null, "child_drift_loss":num|null,
        "parent_pass":bool|null, "child_pass":bool|null,
        "parent_score":num|null, "child_score":num|null,   // continuous (#18)
        "parent_metrics":obj|null, "child_metrics":obj|null, // precision/recall
        "delta":num|null,                       // child − champion drift loss
                                                //   POSITIVE = worse
        "delta_score":num|null,                 // child − champion score
                                                //   POSITIVE = better
        "score_replicates":int,                 // challenger's draws on this entry
        "score_se":num|null,                    // null below two draws
        "verdict": "improved"|"regressed"|"flat",
        "won_by": <genId>|null,
        "decided_by": "score"|"pass"|"drift"|null,
        "parent_session_id"?, "child_session_id"? } ],
    "scalar": { "parent":num|null, "child":num|null, "delta":num|null,
        "components": { <component>: num } } | null,  // delta of each
                                                      // scalar_components term
    "source": "loss_files"
  }
  ```
  `entry_grid` rows are sorted by entry id; an entry that ran on only
  one side still appears (the absent side is `null`).

  **The verdict is the server's, and it names its channel.** `verdict`
  and `won_by` resolve on the first channel that SEPARATES the two
  sides. The channels are tried in order: the continuous `score` (higher
  is better), then the pass predicate, then the drift loss (lower is
  better). `decided_by` names the channel that separated them.
  A channel populated but equal on both sides has separated nothing, so
  resolution falls through to the next; when none separates them the
  entry is `"flat"` and `decided_by` names the channel it was read on.
  Resolution is per ROW, so a champion generation scored before the
  `score` field existed degrades on that row alone. Clients render these
  three fields; they never re-derive a verdict from the numbers beside
  them.

  **`delta_score` sums over the shared slice.** Only entries both sides
  ran carry one, which is the same restriction the promote gate applies,
  so summing (or averaging) the column reproduces the gate's comparison
  instead of a client-defined slice.

  **`drift_present`** is true when any run on either side recorded a
  drift event or a non-zero drift loss. An adapter that emits no drift
  stream writes a structural `0.000` everywhere, which reads on the wire
  the same as a clean run — so a client told `false` HIDES the drift
  columns rather than painting zeroes. A payload that omits the field
  leaves the question unanswered: treat the absence as unknown and keep
  showing drift.

  **`score_se`** is the sample standard deviation of the challenger's
  replicate scores over the square root of their count — the
  candidate's own measurement precision on the entry, NOT the delta's
  full variance (the champion side is frequently a single cached draw).
  It is `null` below two draws and renders as `--`, never `±0.000`.

  The renderers are the epoch publication (`js/views/publication.js`)
  and the candidate dossier (`js/views/candidate.js`, which feeds the
  lifecycle figure in `js/dag.js`). The dossier reads
  ONE grid for its champion comparison — it does not fetch the
  champion's per-entry rows to join and slice them itself. A malformed
  coordinate degrades to an empty grid (HTTP 200), never a 500.
- `GET /api/generation/{epoch}/{generation}/per-entry` also carries
  `drift_present` — the same flag, scoped to one generation: true when
  any of its runs recorded a drift event or a non-zero drift loss. The
  board trellis, the epoch heatmap and the per-board drill-down read it
  to decide whether the drift channel is worth painting. Each also
  prefers a per-entry `score` over drift when the board carries one, so
  a figure never plots a quantity the verdict beside it did not use.

Every one of these reads can outlive the tree it describes: snapshot GC
prunes generation source trees and keeps the records. So each response
declares `provenance` — `"snapshot"` (re-enumerated from a materialised
tree, exact) or `"records"` (reconstructed) — and a records-sourced
response carries `provenance_note`, the caption the view MUST render
**verbatim**. The server knows *why* a tree is missing (pruned vs
unreachable); the client does not, and must not re-word it.

The file and mutation endpoints in full:
- `GET /api/files` — `{ epochs:[{ epoch_id, generations:[{ generation_id,
  file_count, patch_count, has_tree }] }] }`. Generations are listed in
  store order; the last element is the latest generation. `has_tree:false`
  with `file_count:0` means the tree was pruned rather than empty.
- `GET /api/files/{epoch}/{gen}/tree` — `{ epoch_id, generation_id,
  entries:[{ path, is_dir, size }], error? }`. Served and covered by
  tests, but no client reads it: the console offers the changed-files
  diff and the mutation-site browser instead of a per-generation file
  browser. Reserved for external callers.
- `GET /api/files/{epoch}/{gen}/content?path=` — one file's content.
  Reserved for external callers in the same way as `/tree`.
- `GET /api/files/{epoch}/{gen}/patches` — the applied patch set for a
  SINGLE generation (parent -> selected).
- `GET /api/files/{epoch}/{gen}/diff` — the files the generation
  CHANGED relative to its parent (the parent recorded in
  `experiment.json`, else the `v(N-1)` / `v0` fallback). Shape:
  `{ epoch_id, generation_id, parent_generation_id:str|null,
  files:[{ path, status:"added"|"modified"|"removed", old_content,
  new_content, old_binary, new_binary }], error? }`. `files` lists
  only files that differ, sorted by path; a seed generation
  (`parent_generation_id == null`) reads every file as `added`. The
  patch-diff pane renders each entry side by side, `old_content` on the
  left and `new_content` on the right.
  When the generation's tree — or its RECORDED parent's — is gone,
  `files` instead carries the patch-touched SPANS reconstructed from the
  records, each flagged `reconstructed:true` with `span:{mutation_id, op,
  rationale, line_start, line_end}` and, where the record cannot honestly
  produce content, `new_content:null` plus a `note` saying why.
- `GET /api/mutations/{epoch}` — `{ epoch_id, generations:[str],
  mutations:[{ mutation_id, kind, file, role, line_start, line_end,
  metadata, patched_by, patched_generation_ids }], provenance,
  provenance_note, error? }`. `generations` is the union of the store's
  listing and the epoch's per-generation RECORD directories, so a
  generation whose tree is gone still gets its matrix column.
- `GET /api/mutations/{epoch}/{mutation_id}` — one site:
  `{ …site fields…, baseline:{ generation_id, content, content_hash,
  file, role, line_start, line_end, provenance }, versions:[{
  generation_id, patch_id, op, rationale, content, provenance,
  note?, error? }], provenance_note, error? }`. `baseline.generation_id`
  is `null` on the records path — the frozen enumeration is the round's
  champion surface, and naming it `v0` would be a guess — so the diff's
  left label reads "from records" rather than a generation id.

A cumulative patch chain — every patch applied to reach one generation
— is derived ON THE CLIENT from the endpoints above, with no endpoint of
its own. The walk follows `parent_generation_id` through
`state.lineage.generations` from the selected generation back to the
seed, then fetches each ancestor's `/api/files/{epoch}/{gen}/patches`.
Only the lineage path is on the chain; rejected sibling generations that
share a parent edge are NOT walked.

## 2. Server-sent event frame types

`GET /events` yields, in order:
- `event: snapshot` — `{ type:"snapshot", data:{...build_snapshot...} }`
  on connect. Folded into AppState via `state.applySnapshot`.
- `event: state_change` — `{ type:"state_change", kind, kinds:[...], ts }`.
  `kinds` is the coalesced set of changed regions. The client debounces
  and performs ONE `/api/environment` fetch, then `applyEnvironment`.
- `event: run_log` — `{ type:"run_log", events_path, size, ts }`. Drives
  an append-only `/api/run-log?after=<cursor>` poll.
- `: ping` — keepalive comment, ignored.

**The structural rule:** a frame NEVER rebuilds a panel's `innerHTML`.
After `applyEnvironment` the render layer diffs state and writes only the
affected DOM node, keyed by a stable `data-*` id (§4). The run-log tail
is strictly append-only.

## 3. The client state object — `AppState` (core/state.js)

AppState is the single source of truth. A pane's `render` reads it and
never mutates it. Fields:

```
state.connected / connecting / mock     — connection status
state.heartbeat                         — merged heartbeat record
state.activeRuns []                     — active run records
state.activeTournament | null
state.pastTournaments []
state.selectedEntry   | null
state.lineage         { generations, experiments }
state.logTail         { events:[] }   logCursor   logEventsPath
state.health                          — dashboard-service identity
state.epoch           { id, generation, round, startedAt }
state.epochs          — per-epoch goal summary [ { epoch_id, goal } ]
state.workspace
state.files / state.mutations         — file + mutation pane scratch state
```

Mutation methods: `applySnapshot(snap)`, `applyEnvironment(env)`,
`setHeartbeat(hb)` (merge, never replace — keeps `harmonograf_url`),
`setHealth(h)`, `setLogTail(t)`, `mergeLogTail(batch)`. State changes
publish on the bus (§5).

**AppState holds no per-matchup cache.** There are no `matchupDetail`,
`driftMovements` or `selectedMatchup` fields and no `loadMatchupDetail()`
loader: caching `/api/tournaments/{gen}` and `/api/drift-movements/{gen}`
on every change signal would cost two round-trips per beat that no view
reads. The beat path makes exactly ONE consolidated `/api/environment`
read, and per-matchup detail is an on-demand drill-down through
`js/data.js`.

## 4. The render spine — digest-gated, no-flash

The anti-flash mechanism is `gatedSwap` (`ui.js`). A pane computes a
cheap content digest. When the digest is unchanged the DOM is left
strictly untouched: no builder runs and nothing is written. When it
changed, the panel's subtree is rebuilt and swapped in whole.

`core/dom.js` exports the building blocks under that discipline:
- `el(tag, props, children)`, `svgEl(...)`, `clearChildren(n)` —
  element construction / explicit teardown for a rebuild.
- `patchText(node, text)` — sets textContent only if changed.
- `patchClass(node, name, on)` — toggles a class only if changed.

Each pane exports `render(host, ctx, params)`. It is re-run after every
state change and MUST gate all DOM writes on a digest (`gatedSwap`) or
use the `patch*` helpers, so unchanged nodes are untouched. A pane never
sets `host.innerHTML`.

### 4a. Figure width — intrinsic, capped

A figure's width is its **intrinsic content width**, capped; full width is
reserved for tables and timelines, whose rows genuinely use it. An SVG at
`width:100%` scales its own coordinate system, so every mark, radius and
especially every `<text>` magnifies with the pane — a 340×64 trend rendered
across 1000px draws its captions at 3× the size CSS asked for. Two builders in
`svg.js` encode the choice. `applyIntrinsic` pins the viewBox width in CSS
pixels (scale exactly 1) with `max-width:100%` and `xMinYMid meet`, so a
narrow pane shrinks the whole figure uniformly. `applyResponsive` opts into
the aspect-locked full-width hero mode, and is legitimate only for a builder
that also ships a matched `svg.dn-*-hero` max-width cap in `console.css`. A
`preserveAspectRatio:'none'` figure stretched across a `1fr` grid lane is the
same defect in its other shape — a horizontal-only scale that flattens slopes
and smears dots into ellipses. Compact figures pack side by side into a shared
wrapping grid band (the epoch view's Measurement band) rather than each taking
a full-width panel; each card keeps its own collapsed `figCaption` "?".

## 5. The event bus — `core/bus.js`

`bus.on(topic, fn)` / `bus.emit(topic, payload)`. Topics:
- `state:changed` — AppState mutated; the active view re-renders.
- `route:changed` — `{ view, params }` — router resolved a new route.
- `log:appended` — `{ events:[...] }` — new run-log rows to append.

## 6. Shared builders (`js/ui.js`, `js/svg.js`)

Every builder is a pure factory returning a detached DOM node; none
mounts itself, and none reads global state. A caller composes the nodes
and hands them to `gatedSwap`.

From `js/ui.js`:
- `section(title, ...children)` → `<section class="dn-section">` with an
  `<h2>` heading; `subhead(text)` is the smaller in-section label.
- `dataTable({ columns, rows, class })` → a `<table>`. A cell is a
  string, a number, `{ text, class, title }`, or `{ el, class }` for a
  composed cell; a falsy cell is dropped, so a conditional column needs
  no branch at the call site. A row may carry `class`, `dataset`,
  `style` and `onClick`.
- `deltaCell(value, opts)` — the sign-coloured delta cell spec for
  `dataTable`. A positive delta is a regression (`dn-bad-t`), a negative
  one an improvement (`dn-good-t`).
- `pill(cls, word, extra)`, `chip(cls, word, extra)`,
  `verdictPill(decision, opts)`, `stat(value, key)` — the small labelled
  marks.
- `figCaption(lines, opts)` — a figure caption that refuses to stack.
  The first line stays visible; the rest collapse behind a focusable "?"
  glyph (`moreMark`) that opens the singleton hovercard.
- `empty(text)` / `loading(text)` — the single-line muted states.
- `renderMarkdown(md, opts)` — the restricted Markdown renderer used for
  the proposer brief, the journal, and the analysis report.
- `renderView(host, ctx, spec)` — the standard view scaffold. It paints
  a loading line into an empty host, resolves the route's epoch (and
  paints an honest empty state when there is none), runs an optional
  `guard`, awaits `load`, and swaps on `digest`/`build`. A view whose
  flow diverges — parallel fetches, several hosts, a non-epoch gate —
  keeps its own scaffold.

From `js/svg.js`: the figure builders (`heatmap`, `valueDotPlot`,
`sparkbar`, `sparkline`, `genDots`, `survivalFunnel`, `swissLadder`,
`swissOverview`, `elimRadial`, `duelFlow`, `racingScalarTrack`,
`gauntletFieldBars`, `radarSilhouette`, `roundTimeline`, `waterfall`,
`reignGantt`, `metaLoopLedger`, `calibrationTrend`, `sideBySideDiff`,
and the rest). Each returns an `<svg>`; each figure that a view gates on
ships a matching `…Digest` function so the digest and the drawing read
the same fields. `digestOpts(opts, omit)` folds a builder's options into
that digest with the volatile keys named explicitly.

`js/dag.js` builds the candidate lifecycle DAG, and `js/hovercard.js`
owns the single hover-for-detail card every `moreMark` attaches to.

## 7. The detail panes (`js/views/`) and the panels (`js/panels/`)

The shell hosts one detail pane at a time, chosen by the route (§8).
The view registry (`VIEWS` in `router.js`, `RENDERERS` in `shell.js`)
names fifteen views, and each has a module under `js/views/` that exports
`render(host, ctx, params)`. A page section that another view composes,
with no route of its own, is a panel. `js/panels/evals_health.js` is one:
the evals page imports it and mounts it into two hosts it owns. Three
panels still sit under `js/views/` beside the views that mount them:
`structure.js` (the tournament model builders and figures the epoch page,
the rounds page and the live band draw), `boardstatus.js` and `ledger.js`
(both mounted by the epoch page).

- **home** — the workspace as a fleet: a cross-epoch overview strip, one
  compact card per epoch carrying its loss trendline, the composed
  meta-loop ledger, and loop health.
- **epoch** — one epoch's substrate: the objective, the collapsible
  proposer brief, the rounds along the champion spine (or a compact
  structure overview for a non-gauntlet structure), and the
  board-by-generation drift-loss heatmap.
- **gens** — the epoch's tournament rounds, optionally scoped to one
  round.
- **structure** (panel) — the configured tournament structure, drawn per
  structure kind.
- **candidate** — one generation, comparison-first: the lifecycle DAG,
  the per-board scoring dot plot, every match-up, and the stacked
  promote gate. A "compare with…" picker splits the pane into two
  candidates read side by side.
- **diff** — that candidate's patches against the generation it was
  derived from, side by side, one block per mutation site.
- **boards** — the board trellis: one small multiple per board entry.
- **board** — one board entry across every candidate, with the
  champion-versus-challenger transcript inline.
- **boardstatus** (panel) — the train/holdout split and where each slice
  is played, derived defensively from `/api/epoch`.
- **evals** — the entries-by-candidates matrix: rows are board entries
  (the instrument), columns are candidates (what it measured).
- **evals_health** (panel, under `js/panels/`) — the board read as a
  measuring device: the measured same-versus-same noise floor, the
  minimum-detectable-effect ladder, and the ranked instrument-quality
  findings.
- **ledger** (panel) — the epoch's experiments as one list: each proposed
  idea, the sites it touched, and how the gate settled it.
- **instrument** — the board-reflection lens: the bill of health, the
  practice review, the judge audit, and the adjudication x-ray.
- **traces** — imported foreign trajectories: one trajectory strip per
  trace over the reconstructed conversation, with the mined episodes
  bracketed.
- **mutations** — the mutation surface as a site-by-generation matrix,
  with the selected cell's patch diffed side by side.
- **publication** — the epoch write-up, typeset with live figures
  spliced in at the `<!-- FIGURE:NAME -->` markers.
- **builder** — the tournament builder: a rail of contract sections, the
  active section's controls, a live preview with a cost estimate, and a
  copilot chat pane. It edits a draft and writes only on confirmation.
- **settings** — the contract roll-up, the model-engine configuration,
  and appearance (theme, typeface, scale).
- **logs** — the operator-log pane: one structured stream per `evolve`
  or `reflect` invocation, tailed through the query layer. It sits at
  workspace level rather than inside an epoch, because the streams are
  per-invocation.

**Where the epoch panes get their data.** The epoch contract is read
from `GET /api/epoch` (`D.epoch`, cached per epoch), built by
`build_epoch_view`. It exposes `experiments` (per-generation
records carrying the raw `hypothesis`, `outcome`, and `patches` keyed by
mutation id), `brief`, `journal`, `analysis_md`, and the contract
blocks. One experiment record's shape:

```jsonc
{ "generation_id", "parent_generation_id",
  "hypothesis": { "core_idea", "why", "modulating": [],
      "expected_pass_rate_delta", "expected_drift_movements": [], "risks" },
  "patches": { "<mutId>": { "mutation_id", "op", "rationale",
      "new_content" | "new_numeric" | "new_enum" } },
  "outcome": { "ran_at", "tournament_decision", "scalar_score_delta",
      "pass_rate_delta", "drift_loss_delta", "rejection_reason" } | null }
```

An experiment whose tournament never reached a verdict is `incomplete`
and still appears; the raw journal drops it. The patch diff reuses the
lazy `/api/mutations/{epoch}/{site}` baseline read.

## 8. Routes (`js/router.js`)

Hash routes under a bare `#/` prefix. Every route is deep-linkable, and
an unrecognised hash resolves to home so a stale link never lands blank.

```
#/                                     the workspace (home)
#/e/<epochId>                          epoch overview
#/e/<epochId>/gens[/r/<round>]         rounds, optionally one round
#/e/<epochId>/gen/<gen>[/<entry>]      candidate (lifecycle + gate)
#/e/<epochId>/gen/<gen>/diff[/<mutId>] that candidate's patch diff
#/e/<epochId>/boards                   the board trellis
#/e/<epochId>/board/<entry>[/<gen>]    one board + inline transcript
#/e/<epochId>/evals                    the entries × candidates matrix
#/e/<epochId>/mutations[/<mutId>[/<gen>]]   mutation surface + diff
#/e/<epochId>/instrument[/<reflectionId>[/<judge>[/<runRef>]]]
#/e/<epochId>/traces[/<reflectionId>[/<traceId>]]
#/e/<epochId>/paper                    the epoch publication
#/builder                              the tournament builder
#/logs                                 the operator-log pane
#/settings[/<section>]                 contract / models / appearance
```

A bare `#/settings` opens the contract section
(`DEFAULT_SETTINGS_SECTION`). Under `mutations`, a bare mutation id pins
the site with every generation that patched it stacked; a trailing
generation pins one site-by-generation cell.

Suffix parameters ride the hash after a `~`, so one link captures the
whole pane state and a cold load hydrates it:
- `~cmp=<gen>` — the compare target that splits the candidate pane.
- `~base=<gen>` — which version a patch diff is taken against (the
  candidate's recorded parent when absent).
- `~follow=1` — on a board route, open the selected candidate's
  conversation in the live follow pane.

`parseRoute(hash)` → `{ view, params, cmp }`. `href(view, params, opts)`
builds a hash from a params object plus an optional `{ cmp }`, so the
tree, the breadcrumb, the back button and every view share one
signature. The router emits `route:changed` on the bus.

## 9. Harmonograf (core/harmonograf.js)

> Canonical integration design: **`docs/design/HARMONOGRAF.md`** (server
> lifecycle · session taxonomy · the two dashboard surfaces · liveness vs
> post-mortem). This section is the frontend-contract slice of that doc.

Built from `ZICATO_HARMONOGRAF_URL` surfaced on the heartbeat as
`harmonograf_url`. Exports `harmonografBase()`, `harmonografRunUrl(rec)`,
`harmonografLink(run, label)`, `harmonografMini(target, label, aria)`,
`harmonografGenLink(genId)`, `harmonografSessionId(rec)`,
and the **zicato-level** builders `harmonografMetaSession()`,
`harmonografMetaUrl()`, `harmonografMetaLink(label, aria)`.

**Liveness gate.** Links resolve only against a REACHABLE server.
`harmonografIsLive()` is true when EITHER (a) a run is in flight (an
active tournament or any active run) — the evolve-launched server lives
only then — OR (b) the heartbeat carries `harmonograf_persistent: true`.
The latter is set by the STANDALONE dashboard / builder, which reuses-or-
launches ONE persistent per-workspace harmonograf bound to the workspace's
`.harmonograf/harmonograf.db` (`ensure_workspace_harmonograf`) and injects
its `web_url` into the heartbeat payload (`state_reader.read_heartbeat_dict`)
so the post-mortem deep-links into PERSISTED sessions light up with no live
run. Precedence: a live evolve's own heartbeat `harmonograf_url` always
wins; the injected url only fills the field when the heartbeat has none.

harmonograf keys its session views by the **ADK session id** — the
`sessionId` present on every goldfive event envelope. The backend
surfaces this as `adk_session_id` on run-like records:
- active-run rows from `/api/environment`;
- the per-run header `/api/run/{epoch}/{gen}/{entry}/header`
  (`D.runHeader()`) — the read the candidate drill-down's
  `harmonografLink()` actually uses;
- `entry_grid` rows from `/api/matchup-grid/{epoch}/{champion}/{challenger}`
  (`parent_session_id` / `child_session_id`) — the per-board deep links in
  the Epoch publication's per-match-up tables;
- `ab_grid` cells from `/api/tournaments/{gen}` (`parent_adk_session_id`
  / `child_adk_session_id`) carry the ids too, but **no client fetches that
  endpoint** (§ drill-down list above), so that path is reachable code over
  unreachable data — it deep-links nothing today. `harmonografSessionId()`
  still accepts those key names;
- `active_tournament.entries[]` rows — the runner stamps the run's
  `adk_session_id` onto the per-(entry × side) row the instant the run
  finishes (read from the run's `LossProfile`, never from `events.jsonl`
  in the SSE hot path). Empty string until the side's run completes.

Session path: `/#/session/<adk_session_id>`. Filtered navigation uses the
generic `/#/sessions?metadata.<key>=<value>` picker route. Zicato stamps
namespaced coordinates and constructs that URL; Harmonograf does not know
what a tournament or board means.

**Zicato-level (meta-loop) surface.** Beyond the per-run links, the top
bar (`js/shell.js`) carries a single liveness-gated `execution ↗`
deep-link into the **meta-loop** session — zicato's own proposer + judge
timeline (the operator's "Gantt view of zicato itself"). The backend
surfaces its session id on the heartbeat as `harmonograf_meta_session`
(a live evolve writes it from the `MetaLoopEmitter`; the standalone
dashboard recovers it off `runtime/meta_loop_events.jsonl` for
post-mortem). `harmonografMetaUrl()` resolves
`<harmonograf_url>/#/session/<harmonograf_meta_session>`, gated on the same
liveness rule as the per-run links. See `docs/design/HARMONOGRAF.md` §2b/§3b.

The live candidate view surfaces one tournament-overall filtered-picker link;
per-run and A/B-grid links continue to deep-link the exact session id.

`harmonografSessionId(rec)` accepts `adk_session_id`,
`child_adk_session_id`, or `parent_adk_session_id`. Missing identity returns
null; synthetic run ids are never substituted.
