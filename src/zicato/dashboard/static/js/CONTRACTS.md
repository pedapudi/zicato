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
      dom.js            — el, svgEl, clearChildren, patch helpers
      state.js          — AppState: the single client state object
      api.js            — fetchJson, loadEnvironment, postControl
      sse.js            — EventSource wiring + delta dispatch
      router.js         — hash routing + deep links
      bus.js            — tiny pub/sub event bus
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
carries no goal. It lets the Overview's epochs table annotate each row
with what the epoch is trying to accomplish without a per-epoch
`/api/epoch` fetch. Folded into AppState as `state.epochs`.

`GET /api/health` (separate, fetched ONCE) is the dashboard *service*
identity: `{ status, version, port, build, uptime_seconds, read_only,
workspace }`. It is NOT in the environment payload.

The drill-down / lazy endpoints (unchanged):
- `GET /api/run-log?after=<cursor>` — append-only tail batch.
- `GET /api/tournaments/{gen}` — per-matchup detail. **NO client.** The
  per-beat loader that used to pull it is deleted (§3); the Match-ups
  surfaces read `/api/tournaments` (the bracket) + `/api/matchup-grid/...`
  instead. Curl/operator surface only.
- `GET /api/drift-movements/{gen}` — drift-kind movements. **NO client**
  (same deletion). Curl/operator surface only.
- `GET /api/score-trajectory` — same shape as `environment.score_trajectory`.
- `GET /api/files...`, `GET /api/mutations/...` — Files view.
- `GET /api/conversation/{run}` — the run_id-keyed transcript
  (`D.conversation()`; `D.runTranscript()` is the preferred gen×entry read).
- `GET /api/run/{epoch}/{gen}/{entry}/transcript/delta?after=<cursor>` — the
  live conversation pane's **cursor-append** read (issue #194 §2). Returns
  only what changed since `after`, so following a running unit never
  re-sends a settled conversation:

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

  The **cursor counts parsed events**, not goldfive `sequence` numbers: a
  `multi_turn_emulated` entry restarts `sequence` at 0 per run, so only the
  parsed-event count is monotone over the file. Being a count it sits one
  past the last index it covers, so the server's filter is **inclusive**
  (`source_index >= after`) — which is what re-delivers the OPEN final turn
  when it grows, at its existing `turn_index`. Replaying an unchanged cursor
  therefore yields an empty delta. A torn final line takes no cursor
  position, so it arrives whole on a later poll. `limit` is clamped by the
  run-log's own `clamp_run_log_limit`. This is a SEPARATE route from
  `/transcript` above, which keeps its full-payload shape for the
  side-by-side panes.
- `GET /api/matchup/{entry_id}/conversations` — **NO client, by decision.**
  It is a **curl / operator surface**, not a live drill-down: the champion-vs-
  challenger transcript comparison the operator actually uses is the Board
  view's inline side-by-side, which resolves each side through the
  deterministic `(epoch, gen, entry)` triple (`D.runTranscript()`) rather
  than an entry-keyed pair. Documented rather than wired so the next audit
  reads this as deliberate.
- `GET /api/epoch/{epoch_id}/journal.md` — **NO client, by decision.** The
  raw `journal.md` bytes are a **curl / operator surface**; the dashboard
  renders the same text from the `journal` field the `/api/epoch` payload
  already carries, so a second fetch of the same file would be pure
  duplication.
- `GET /api/search` — **NO client and no CLI command:**
  *unconsumed pending a search box.* Building one is new scope (a chrome-level
  affordance + a results route), deliberately not taken on here.
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

  **The verdict is the server's, and it names its channel.** `verdict` /
  `won_by` resolve on the first channel that SEPARATES the two sides —
  the continuous `score` (higher better), then the pass predicate, then
  the drift loss (lower better) — and `decided_by` says which one did.
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
  exactly like a clean run — so a client told `false` HIDES the drift
  columns rather than painting zeroes. A payload that omits the field
  predates it: treat the absence as unknown and keep showing drift.

  **`score_se`** is the sample standard deviation of the challenger's
  replicate scores over the square root of their count — the
  candidate's own measurement precision on the entry, NOT the delta's
  full variance (the champion side is frequently a single cached draw).
  It is `null` below two draws and renders as `--`, never `±0.000`.

  The **Epoch publication** view (`js/views/publication.js`) and the
  **candidate dossier** (`js/views/candidate.js`, which feeds the
  lifecycle figure in `js/dag.js`) are the renderers. The dossier reads
  ONE grid for its champion comparison — it does not fetch the
  champion's per-entry rows to join and slice them itself. A malformed
  coordinate degrades to an empty grid (HTTP 200), never a 500.
- `GET /api/generation/{epoch}/{generation}/per-entry` also carries
  `drift_present` — the same flag, scoped to one generation: true when
  any of its runs recorded a drift event or a non-zero drift loss. The
  Boards trellis, the epoch heatmap and the per-board drill-down read it
  to decide whether the drift channel is worth painting; each also
  prefers a per-entry `score` over drift when the board carries one, so
  a figure never plots a quantity the verdict beside it did not use.

Every one of these reads can outlive the tree it describes: snapshot GC
prunes generation source trees and keeps the records. So each response
declares `provenance` — `"snapshot"` (re-enumerated from a materialised
tree, exact) or `"records"` (reconstructed) — and a records-sourced
response carries `provenance_note`, the caption the view MUST render
**verbatim**. The server knows *why* a tree is missing (pruned vs
unreachable); the client does not, and must not re-word it.

The Files-view endpoints in full:
- `GET /api/files` — `{ epochs:[{ epoch_id, generations:[{ generation_id,
  file_count, patch_count, has_tree }] }] }`. Generations are listed in
  store order; the last element is the latest generation. `has_tree:false`
  with `file_count:0` means the tree was pruned, not that it was empty.
- `GET /api/files/{epoch}/{gen}/tree` — `{ epoch_id, generation_id,
  entries:[{ path, is_dir, size }], error? }`. Still served by the
  server (and exercised by tests) but no longer consumed by the
  dashboard — the per-generation file browser was removed in favour of
  the What-changed diff + the mutation-site browser. Reserved for
  external clients / future tooling.
- `GET /api/files/{epoch}/{gen}/content?path=` — one file's content.
  Same reserved-for-external-use status as `/tree`.
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
  Files view renders each entry as a side-by-side split diff
  (`old_content` left, `new_content` right) via the `diff` component
  in `mode:'split'`.
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

The Files view's CUMULATIVE patch chain ("Patches applied to reach
v{N}") is derived ON THE CLIENT — no new endpoint. The walk follows
`parent_generation_id` through `state.lineage.generations` from the
selected generation back to the seed, then fetches each ancestor's
`/api/files/{epoch}/{gen}/patches` (cached in
`filesState.patchCache`). Only the lineage path is on the chain;
rejected sibling generations that share a parent edge are NOT walked.

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
state.selectedEntry   | null
state.healthReport
state.lineage         { generations, experiments }
state.scoreTrajectory { points: [] }
state.logTail         { events:[] }   logCursor   logEventsPath
state.health                          — dashboard-service identity
state.epoch           { id, generation, round, startedAt }
state.epochDef        — full epoch contract
state.epochs          — per-epoch goal summary [ { epoch_id, goal } ]
state.workspace
state.files / state.mutations         — Files-view scratch state
```

Mutation methods: `applySnapshot(snap)`, `applyEnvironment(env)`,
`setHeartbeat(hb)` (merge, never replace — keeps `harmonograf_url`),
`setHealth(h)`, `setLogTail(t)`, `mergeLogTail(batch)`. State changes
publish on the bus (§5).

**Deleted, deliberately:** `matchupDetail` / `driftMovements` /
`selectedMatchup` and their loader `loadMatchupDetail()`. They cached
`/api/tournaments/{gen}` (whole payload, `ab_grid` included) and
`/api/drift-movements/{gen}` on EVERY SSE beat and **no view ever read
either field** — two per-beat round-trips, both discarded. The beat path
now makes exactly ONE consolidated `/api/environment` read; per-matchup
detail is an on-demand drill-down through `js/data.js`.

## 4. The render spine — digest-gated, no-flash

The anti-flash mechanism is `gatedSwap` (ui.js): a view computes a cheap
content digest; when the digest is unchanged the DOM is left strictly
untouched (no builder run, no writes), and when it changed the panel's
subtree is rebuilt and swapped in whole.

`core/dom.js` exports the building blocks under that discipline:
- `el(tag, props, children)`, `svgEl(...)`, `clearChildren(n)` —
  element construction / explicit teardown for a rebuild.
- `patchText(node, text)` — sets textContent only if changed.
- `patchClass(node, name, on)` — toggles a class only if changed.

Each view exposes `render(state, route)` and a `mount()` called once.
A view's `render` is re-run after every state change but MUST gate all
DOM writes on a digest (`gatedSwap`) or use the `patch*` helpers so
unchanged nodes are untouched. A view never sets `container.innerHTML`.

### 4a. Figure width — intrinsic, capped

A figure's width is its **intrinsic content width**, capped; full width is
reserved for tables and timelines, whose rows genuinely use it. An SVG at
`width:100%` scales its own coordinate system, so every mark, radius and
especially every `<text>` magnifies with the pane — a 340×64 trend rendered
across 1000px draws its captions at 3× the size CSS asked for. Two builders in
`svg.js` encode the choice: `applyIntrinsic` pins the viewBox width in CSS
pixels (scale exactly 1) with `max-width:100%` + `xMinYMid meet`, so a narrow
pane shrinks the whole figure uniformly; `applyResponsive` opts into the
aspect-locked full-width hero mode and is legitimate ONLY for a builder that
also ships a matched `svg.dn-*-hero` max-width cap in `console.css`. A
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

## 8. Routes (js/router.js)

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
  still accepts those key names for back-compat;
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
