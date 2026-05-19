# Dashboard frontend — pinned contracts (Phase 1 foundation)

This file is the contract every Phase-2 view agent codes against. It is
**frozen** for the duration of the fan-out. If a view needs a contract
change, it must be raised with the integration lead — never edited
unilaterally, because that is how file collisions and silent breakage
creep in.

The frontend is split into disjoint modules so a large team can work in
parallel with zero shared-file edits:

```
static/
  index.html            — page shell: header, nav rail, view containers, drawer
  style.css             — global tokens + chrome styling (global-chrome agent)
  app.js                — ES-module ENTRY POINT (foundation; do not edit in P2)
  js/
    core/               — the data/render spine (foundation; FROZEN in P2)
      dom.js            — $, el, svgEl, clearChildren, patch helpers
      state.js          — AppState: the single client state object
      api.js            — fetchJson, loadEnvironment, postControl
      sse.js            — EventSource wiring + delta dispatch
      router.js         — hash routing + deep links
      bus.js            — tiny pub/sub event bus
      format.js         — fmtDelta, fmtDuration, parseIso, truncate, ...
      harmonograf.js    — harmonograf URL builders
    components/         — shared component library (foundation; FROZEN in P2)
      card.js, table.js, badge.js, diff.js, chart.js, empty.js
    views/              — ONE FILE PER VIEW — Phase-2 agents own these
      overview.js       — Overview view  (overview agent)
      lineage.js        — Lineage view   (lineage agent)
      tournament.js     — Tournament view (tournament agent)
      epoch.js          — Epoch view     (epoch agent)
      files.js          — Files view     (files agent)
      chrome.js         — header / nav / activity-log drawer (chrome agent)
  test/                 — JS/DOM test harness (foundation)
    harness.mjs         — minimal DOM + assert harness
    *.test.mjs          — per-module behaviour tests
```

**Ownership rule:** a Phase-2 agent edits ONLY its own `views/<name>.js`
file plus appending CSS to a clearly-fenced block. It never edits
`core/`, `components/`, `index.html`, or another view's file. The
backend agent owns `endpoints.py`, `server.py`, `sse.py`,
`state_reader.py` only.

---

## 1. The `/api/environment` snapshot shape

`GET /api/environment` returns ONE coalesced read. Every component
degrades independently — a missing input is `null`/`[]`, never an
exception. Shape (as produced by `state_reader.build_environment`):

```jsonc
{
  "workspace": "/abs/path/.zicato",
  "epoch_id": "2026-05-18_presn" | null,
  "epoch": { ...epoch contract... },          // build_epoch_view
  "epochs": [ { "epoch_id": str, "goal": str|null } ],  // per-epoch goal summary
  "active_tournament": { ...tournament... } | null,
  "tournaments": { "epoch_id", "champion_lineage":[genId], "matchups":[...] },
  "generations": { "generations":[...], "experiments":[...] },
  "score_trajectory": { "epoch_id", "points":[{generation_id,
        parent_generation_id, promoted, scalar, entry_count, created_at}] },
  "active_runs": [ { ..., progress:0..1|null, elapsed_seconds, budget_seconds,
                      adk_session_id:str } ],
  "health_report": { ...loop health... },
  "heartbeat": { generation_id, round_index, last_heartbeat,
        round_started_at, started_at, harmonograf_url? } | null,
  "lock": { ... } | null,
  "run_log": { "events":[{seq,kind,ts,summary}], "cursor":int, "events_path":str },
  "generated_at": "ISO-8601 Z"
}
```

`epochs` is a lightweight per-epoch summary list — one
`{ epoch_id, goal }` row per epoch directory on disk. `goal` is a
one-line description distilled from that epoch's proposer brief (the
`## Goal` section of `brief.md`), or `null` when the brief is absent or
carries no goal. It lets the Overview's epochs table annotate each row
with what the epoch is trying to accomplish without a per-epoch
`/api/epoch` fetch. Folded into AppState as `state.epochs`.

`GET /api/health` (separate, fetched ONCE) is the dashboard *service*
identity: `{ status, version, port, build, uptime_seconds, read_only,
workspace }`. It is NOT in the environment payload.

The drill-down / lazy endpoints (unchanged):
- `GET /api/run-log?after=<cursor>` — append-only tail batch.
- `GET /api/tournaments/{gen}` — per-matchup detail.
- `GET /api/drift-movements/{gen}` — drift-kind movements (reuse landed builder).
- `GET /api/score-trajectory` — same shape as `environment.score_trajectory`.
- `GET /api/files...`, `GET /api/mutations/...` — Files view.
- `GET /api/conversation/{run}`, `GET /api/matchup/{entry}/conversations`.
- `GET /api/matchup-grid/{epoch}/{champion}/{challenger}` — the
  per-entry A/B grid for a **completed** matchup, read straight off the
  persisted per-run loss files (NOT the SQLite index). `/api/tournaments/{gen}`
  sources its `ab_grid` from the analytical index, a best-effort
  dual-write; a finished tournament whose index was never (re)built
  carries an empty `ab_grid`, so the Tournament matchup-detail panel
  loses its per-board outcomes. This endpoint reconstructs them from
  `generations/{gen}/runs/{entry}/loss.json` (the reducer's `LossProfile`)
  for both generations plus the `generations/{gen}/gen_score.json`
  aggregates. Shape:
  ```jsonc
  {
    "epoch_id", "champion", "challenger",
    "entry_grid": [ { "entry_id",
        "parent_drift_loss":num|null, "child_drift_loss":num|null,
        "parent_pass":bool|null, "child_pass":bool|null,
        "delta":num|null,                       // child − champion drift loss
        "verdict": "improved"|"regressed"|"flat",
        "won_by": <genId>|null,                 // lower drift loss wins
        "parent_session_id"?, "child_session_id"? } ],
    "scalar": { "parent":num|null, "child":num|null, "delta":num|null,
        "components": { <component>: num } } | null,  // delta of each
                                                      // scalar_components term
    "source": "loss_files"
  }
  ```
  `entry_grid` rows are sorted by entry id; an entry that ran on only
  one side still appears (the absent side is `null`). The Tournament
  view fetches this lazily for a non-live matchup and folds it into the
  matchup-detail panel as the `entry_grid` / `scalar` fallback when the
  index-sourced detail has neither. A malformed coordinate degrades to
  an empty grid (HTTP 200), never a 500.

The Files-view endpoints in full:
- `GET /api/files` — `{ epochs:[{ epoch_id, generations:[{ generation_id,
  file_count, patch_count }] }] }`. Generations are listed in store
  order; the last element is the latest generation.
- `GET /api/files/{epoch}/{gen}/tree` — `{ epoch_id, generation_id,
  entries:[{ path, is_dir, size }], error? }`.
- `GET /api/files/{epoch}/{gen}/content?path=` — one file's content.
- `GET /api/files/{epoch}/{gen}/patches` — the applied patch set.
- `GET /api/files/{epoch}/{gen}/diff` — the files the generation
  CHANGED relative to its parent (the parent recorded in
  `experiment.json`, else the `v(N-1)` / `v0` fallback). Shape:
  `{ epoch_id, generation_id, parent_generation_id:str|null,
  files:[{ path, status:"added"|"modified"|"removed", old_content,
  new_content, old_binary, new_binary }], error? }`. `files` lists
  only files that differ, sorted by path; a seed generation
  (`parent_generation_id == null`) reads every file as `added`. The
  Files view renders each entry as a side-by-side split diff
  (`old_content` left, `new_content` right) via the `diff` component
  in `mode:'split'`.

## 2. SSE delta types

`GET /events` yields, in order:
- `event: snapshot` — `{ type:"snapshot", data:{...build_snapshot...} }`
  on connect. Folded into AppState via `state.applySnapshot`.
- `event: state_change` — `{ type:"state_change", kind, kinds:[...], ts }`.
  `kinds` is the coalesced set of changed regions. The client debounces
  and performs ONE `/api/environment` fetch, then `applyEnvironment`.
- `event: run_log` — `{ type:"run_log", events_path, size, ts }`. Drives
  an append-only `/api/run-log?after=<cursor>` poll.
- `: ping` — keepalive comment, ignored.

**The structural rule (the flashing fix):** a delta NEVER rebuilds a
panel's `innerHTML`. After `applyEnvironment`, the render layer diffs
state and patches only the affected DOM node, keyed by a stable
`data-*` id (see §4). The activity-log drawer is strictly append-only.

## 3. The client state object — `AppState` (core/state.js)

Single source of truth. Views are pure `render(state, route)`; they
never fetch and never mutate state. Fields:

```
state.connected / connecting / mock     — connection status
state.heartbeat                         — merged heartbeat record
state.activeRuns []                     — active run records
state.activeTournament | null
state.pastTournaments []
state.bracket                           — { champion_lineage, matchups }
state.matchupDetail   Map<genId, detail>
state.driftMovements  { genId: movements }
state.selectedMatchup | null
state.selectedEntry   | null
state.healthReport
state.lineage         { generations, experiments }
state.scoreTrajectory { points: [] }
state.logTail         { events:[] }   logCursor   logEventsPath
state.health                          — dashboard-service identity
state.epoch           { id, generation, round, startedAt }
state.epochDef        — full epoch contract
state.epochs          — per-epoch goal summary [ { epoch_id, goal } ]
state.scoring         { margin }
state.workspace
state.files / state.mutations         — Files-view scratch state
```

Mutation methods: `applySnapshot(snap)`, `applyEnvironment(env)`,
`setHeartbeat(hb)` (merge, never replace — keeps `harmonograf_url`),
`setHealth(h)`, `setLogTail(t)`, `mergeLogTail(batch)`,
`setMatchupDetail(gen, d)`. State changes publish on the bus (§5).

## 4. The render spine — incremental, keyed, no-flash

`core/dom.js` exports:
- `$(id)`, `el(tag, props, children)`, `svgEl(...)`, `clearChildren(n)`.
- `mount(host, key, builder)` — idempotent mount: builds the node once,
  keyed by `data-node` = `key`; on re-call updates in place.
- `patchText(node, text)` — sets textContent only if changed.
- `patchAttr(node, name, value)` — sets attribute only if changed.
- `reconcileList(host, items, keyFn, buildFn, updateFn)` — keyed list
  reconciliation: existing rows (matched by `data-key`) are updated in
  place, new rows appended, gone rows removed. **No list is ever
  cleared-and-rebuilt.** This is what makes click handlers survive a
  delta and the log tail not flash.

Each view exposes `render(state, route)` and a `mount()` called once.
A view's `render` is re-run after every state change but MUST route all
DOM writes through `mount`/`patch*`/`reconcileList` so unchanged nodes
are untouched. A view never sets `container.innerHTML`.

## 5. The event bus — `core/bus.js`

`bus.on(topic, fn)` / `bus.emit(topic, payload)`. Topics:
- `state:changed` — AppState mutated; the active view re-renders.
- `route:changed` — `{ view, params }` — router resolved a new route.
- `log:appended` — `{ events:[...] }` — new run-log rows to append.

## 6. Shared component API (components/)

All components are pure factories returning a detached DOM node (never
mount themselves). Re-render-safe: call again with new data → same node
identity if a `key` is supplied.

- `card({ title, meta, body, key })` → `<section class="card">`
- `table({ columns, rows, key, onRowClick })` → keyed `<table>`,
  rows reconciled by `row.key`.
- `badge(text, kind)` — kind ∈ `ok|warn|err|muted|pending|info`.
- `statusBadge(status)` — maps a run/entry status to a badge.
- `diff(oldText, newText, { mode })` — line diff renderer; `mode` ∈
  `unified|split`.
- `lineChart({ points, x, y, width, height, svg })` — paints into a
  provided `<svg>`; keyed, incremental.
- `emptyLine(text)` — the single-line muted empty state.

## 7. Per-view one-paragraph specs

- **Overview** (`views/overview.js`, container `#view-overview`): the
  environment home. Identity block (workspace, instance, registered
  inner-harness, # mutation sites, # epochs); a loop-health line; a
  COMPACT live-activity card linking to the Tournament view (not the
  full board); the environment-wide score trajectory (reuse
  `build_score_trajectory`); an epochs table — each row carries the
  epoch's goal (from `state.epochs`) alongside its stats; recent
  experiments — an unfinished experiment reads as `incomplete`, and the
  "Full experiment log" link lands on the Epoch view's Experiments
  section; aggregate stats.
- **Lineage** (`views/lineage.js`, container `#view-tree`): the
  generation DAG, the navigation hub. Pan/zoom. Click a node → route to
  its experiment / matchup. Terminology: parent/child.
- **Tournament** (`views/tournament.js`, container `#view-tournament`):
  3-zoom drill-down. (1) verdict summary — champion/challenger, verdict
  distinguishing regression from near-miss, side-by-side scalars by
  axis, a data-quality indicator ("14 runs: 9 completed / 5 failed"),
  drift-kind movements (reuse `build_drift_movements`), tournament-level
  harmonograf jump. (2) the board — one row per entry, champion+
  challenger together, per-entry Δ contribution sorted by |Δ|, failure-
  mode badges, per-board harmonograf jump. (3) matchup detail (expand a
  row) — inline conversation diff + per-run loss breakdown + drift
  events + per-run harmonograf jumps. Past-tournament selector.
  **Matchup-click MUST work** — handlers survive deltas via §4.
- **Epoch** (`views/epoch.js`, container `#view-epoch`): the epoch's
  NARRATIVE. A header block — epoch id, open/closed status, and a stat
  strip tallying experiments / promoted / rejected / `incomplete` / net
  Δscalar. The proposer brief rendered as a readable block, framed as
  the operator's goal for the epoch. The **Experiments section** (one
  merged section — the experiment narrative AND the epoch journal as a
  single chronological per-round log; there is NO separate Journal
  section): one entry per experiment, **terse by default** — a one-line
  summary (round ordinal · generation id · core idea · verdict ·
  Δscalar) — and **expandable** to the full four-beat detail: *what*
  (core idea + lineage), *hypothesis* (the pre-run structured
  prediction: why, expected pass-rate move, predicted drift, risks,
  modulating sites), *change* (the patch summary, with an expandable
  line diff against the epoch baseline), and *outcome* (the tournament
  verdict — did the challenger beat the champion — the scalar Δ and its
  components, the rejection reason, a jump to the Tournament view). A
  journal round's free prose folds into the matching entry as a
  *journal note*; a "view raw journal" link to the journal endpoint is
  offered (not its own section). An experiment whose tournament never
  reached a verdict is `incomplete` and STILL appears here (the raw
  journal drops it). The entry's left-edge accent is coloured by the
  decision so the promoted/rejected/incomplete arc is scannable.
  Supporting context panels: registered harness, board entries, scoring
  weights, mutation surface, and the analysis report.

  **Epoch data source.** Every field above comes from ONE read —
  `state.epochDef`, populated from `GET /api/epoch` and the `epoch` key
  on `/api/environment`. No new endpoint was needed: `build_epoch_view`
  already exposes `experiments` (per-generation records carrying the
  raw `hypothesis`, `outcome`, and `patches` keyed by mutation id),
  `brief`, `journal`, `analysis_md`, and the contract blocks. An
  experiment record's shape: `{ generation_id, parent_generation_id,
  hypothesis:{core_idea, why, modulating[], expected_pass_rate_delta,
  expected_drift_movements[], risks}, patches:{<mutId>:{mutation_id, op,
  rationale, new_content|new_numeric|new_enum}}, outcome:{ran_at,
  tournament_decision, scalar_score_delta, pass_rate_delta,
  drift_loss_delta, rejection_reason} | null }`. The optional patch diff
  reuses the lazy `/api/mutations/{epoch}/{site}` baseline read.
- **Files** (`views/files.js`, container `#view-files`): route-driven
  (`#/files/{epoch}/{gen}`). A "What changed" section — a generation
  picker and a side-by-side (split) diff of every file the selected
  generation changed vs its parent (or the `v0` baseline), via the
  `diff` component in `mode:'split'`; the file-tree of the selected
  generation's snapshot + its applied patches; the mutation-site
  browser. Defaults to the current epoch's latest generation.
- **Chrome** (`views/chrome.js`): persistent header (context: epoch /
  generation / round / elapsed / connection); the collapsible,
  append-only activity-log drawer; the route shell (nav rail active
  state); the rebrand. Owns the footer.

## 8. Routes (core/router.js)

Hash routes; `#/overview` is default. Each is deep-linkable:
- `#/overview`
- `#/tree`  (Lineage)
- `#/tournament`  ·  `#/tournament/{genId}` (open a matchup)
- `#/epoch`  ·  `#/epoch/{epochId}`
- `#/files`  ·  `#/files/{epochId}/{genId}`
- `#/conversation/{entryId}` (focused conversation diff)

The Files route is **route-driven**: the selected epoch + generation
live in the hash. Bare `#/files` resolves to a default — the current
epoch (`environment.epoch`) and that epoch's latest generation — and is
canonicalised in place into `#/files/{epochId}/{genId}` so a reload or
a shared link lands on the same generation. The Files view never falls
through to Overview.

`router.current()` → `{ view, params:{...} }`. `router.go(hash)`
navigates. The router emits `route:changed` on the bus.

## 9. Harmonograf (core/harmonograf.js)

Built from `ZICATO_HARMONOGRAF_URL` surfaced on the heartbeat as
`harmonograf_url`. Exports `harmonografBase()`, `harmonografRunUrl(rec)`,
`harmonografLink(run, label)`, `harmonografMini(target, label, aria)`,
`harmonografGenLink(genId)`, `harmonografSessionId(rec)`, `deriveRunId(rec)`.

harmonograf keys its session views by the **ADK session id** — the
`sessionId` present on every goldfive event envelope. The backend
surfaces this as `adk_session_id` on run-like records:
- active-run rows from `/api/environment`;
- `ab_grid` cells from `/api/tournaments/{gen}` (`parent_adk_session_id`
  / `child_adk_session_id`);
- `active_tournament.entries[]` rows — the runner stamps the run's
  `adk_session_id` onto the per-(entry × side) row the instant the run
  finishes (read from the run's `LossProfile`, never from `events.jsonl`
  in the SSE hot path). Empty string until the side's run completes.

Session path: `/#/session/<adk_session_id>`. No harmonograf-side change
is required — the integration is complete.

The Tournament view surfaces these as visible jump-off links: one
tournament-overall link in the hall head, and one per board side on the
board card (deep-linked by that side's `adk_session_id`, falling back to
the bare base while the run is still in flight).

`harmonografSessionId(rec)` resolution order:
1. `rec.adk_session_id` / `rec.child_adk_session_id` /
   `rec.parent_adk_session_id` — the real ADK session id (preferred).
2. `rec.session_id` / `rec.session` / `rec.harmonograf_session` —
   legacy aliases for back-compat.
3. bare `harmonograf_url` fallback when no session id is present.

`deriveRunId(rec)` returns the synthetic `${generation}--${entry}`
string for callers that need the run-id directly; it is no longer
used for session resolution.
