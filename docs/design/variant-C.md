# Dashboard Variant C — "Causal Flow / Diagram-first"

> **Status.** One of four parallel dashboard explorations. This variant
> is a *full visual redesign* built as an isolated front-end that reuses
> the shipped data layer (`static/js/core/*`) verbatim and paints into
> `#variant-root`, reachable via `?ui=C`. It touches **no** Python, no
> `index.html`, no `js/core/*`, no `js/v2/*`, and no other variant.

## 1. The thesis

zicato is a **causal machine**: it mutates an agent (a PATCH — the
*cause*), runs it over a board and measures DRIFT / loss (the *effect*),
and a GATE promotes or rejects (the *verdict*), over and over, across
generations and epochs. The shipped dashboard renders that machine as
tables, spines, and prose. Variant C makes the opposite bet:

> **Show the causality as flow.** Every hero screen is an *interactive
> diagram* — a node-graph or a Sankey — not a table. The operator reads
> the loop the way it actually behaves: things flow left to right, cause
> into effect into verdict, generation into generation, epoch into epoch.

Three diagram idioms carry the whole UI:

| Idiom | Where | Reads as |
|---|---|---|
| **Cross-epoch node-graph** (lanes + DAG) | Environment | "where has the whole workspace been?" |
| **Lineage / gauntlet graph** (spine + branches) | Epoch, Tournament | "which challengers survived, which died?" |
| **Causal Sankey** (patch → drift → gate) | Experiment, Scoring | "*this* change moved *these* drift kinds, so the gate said *this*" |
| **Lifecycle DAG** (parent → patch → board fan → gate → terminal) | Lifecycle | "one candidate’s whole life, board entry by board entry" |
| **Topology switcher** (one node set, five graph shapes) | Match-ups | "how would this field look under *each* tournament style?" |

Everything is pan/zoom, hover-to-highlight, click-to-drill. Diagrams are
hand-built **dependency-free SVG** (no D3, no external libs) on a shared
pan/zoom `surface`. Colour is a single semantic language driven by
`--v2-*` tokens: cause = violet, drift-improved = teal, drift-worsened =
amber, promote = green, reject = red, in-flight = blue.

## 2. The screens

### 2.1 Environment — *the map* (`#/C/env`)

The whole workspace as one navigable graph. Each **epoch is a lane**;
within a lane each **generation is a node** laid out by the lineage DAG
(column = depth from the seed), coloured by verdict, with the promoted
through-line emphasised and cross-epoch parentage drawn as a faint dashed
edge. Pan/zoom across the entire history; hover a node to light its
ancestry path ("what led here"); click a node → its causal flow.

```
 ENVIRONMENT — 3 epochs · 11 generations    ● promoted 6  ● rejected 3  ● in flight 2
┌───────────────────────────────────────────────────────────────[+ − ⤢]─┐
│ presn_v0           (v0)──▶──(v1)──▶──(v3)                               │
│ "make a deck"        │        ╲                                        │
│                      │         ╳ v2 (rejected)                         │
│ ─────────────────────┼──────────────────────────────────────────────  │
│ steering_v0      (v0)┊··▶··(v1)──▶──(v2)──▶──(v4)                       │
│ "tighten drift"      cross-epoch ╲                                     │
│                       seed link   ╳ v3                                 │
│ ─────────────────────────────────────────────────────────────────────  │
│ zicato_self      (v0)──▶──(v1)──▶──(v2*)  ◀ pulsing = in flight        │
│ "self-host loop"                                                       │
└────────────────────────────────────────────────────────────────────────┘
  drag to pan · wheel to zoom · hover a node to trace its lineage
```

### 2.2 Epoch — *lineage graph + objective* (`#/C/epoch/:id`)

The **objective is the headline** — the single biggest thing on the
screen. The **proposer brief** (long, multi-section, operator-authored)
gets its own home: a dedicated right-hand **drawer**, opened from a
prominent button, rendered as readable prose with its `##` headings — it
never crowds the graph. The **gauntlet** is a clean lineage graph: the
champion through-line runs left→right; rejected challengers branch *down*
off the parent they failed against, each on its own row (collision-free
by the layered layout). Board entries render below as a node cluster.

```
 EPOCH  steering_v0                                              [open]
 ╔══════════════════════════════════════════════════════════════════╗
 ║  Tighten the loop's drift response without regressing latency.    ║   ← OBJECTIVE
 ╚══════════════════════════════════════════════════════════════════╝
   [📋 Proposer brief]   [Open gauntlet →]      ┌─ drawer ───────────┐
                                                │ ## Goal            │
 LINEAGE GAUNTLET                  [+ − ⤢]      │ Reduce confab on   │
 ┌──────────────────────────────────────────┐  │ research tasks…    │
 │ (v0)═══▶═══(v1)═══▶═══(v2)┄┄▶┄┄(v4*)       │  │ ## Constraints     │
 │  baseline    │          │       in flight │  │ Never let latency  │
 │              ╲          ╲                  │  │ regress…           │
 │               ╳ vX       ╳ v3              │  │ ## Mutation budget │
 │              rejected   rejected           │  │ ≤ 2 points/round   │
 └──────────────────────────────────────────┘  └────────────────────┘
 BOARD   [short_solar ×1.0] [long_solar ×1.5] [contradictory ×1.0] …
```

### 2.3 Experiment — *the causal flow* (THE signature screen) (`#/C/experiment/:epoch/:gen`)

A three-stage **Sankey**: the PATCH (mutation points, left) → the DRIFT
KINDS that moved (middle, ribbon **width = magnitude** of the movement,
teal for improved / amber for worsened) → the GATE outcome (right). This
literally draws cause → effect → verdict as flow. Hovering any node lights
only the ribbons it participates in; clicking a mutation node opens its
**diff** in the drawer — the diff is one click away, but the *flow leads*,
not a wall of text. A v0 seed (no tournament) shows its baseline board
results as nodes instead.

```
 EXPERIMENT  v1            ┃ Hypothesis: tighten the researcher's prompt
 ─────────────────────────┃────────────────────────────────────────[+ − ⤢]
  PATCH · the cause          DRIFT · the effect        GATE · the verdict
 ┌────────────────┐         ┌──────────────────┐      ┌──────────────┐
 │ researcher.    │█████╗   │ Confabulation ▓▓▓▓│█████╗│              │
 │ instruction    │     ╚══▶│ improved −4      │     ╚│   PROMOTED   │
 └────────────────┘     ╔══▶│ Tool Error  ▓    │╔════│              │
 ┌────────────────┐█████╝   │ worsened  +1     │║    └──────────────┘
 │ coordinator.   │         └──────────────────┘║
 │ description    │█████════════════════════════╝   (ribbon width =
 └────────────────┘  ↑ click a patch node → diff      movement magnitude)

  ┌ PROMOTED ┐  Δscalar −0.20   Δdrift −0.30   Δpass +0.10
```

### 2.4 Tournament — *the gauntlet, crisp* (`#/C/tournament/:epoch`)

The king-of-the-hill gauntlet as a standalone bracket/flow graph (same
collision-free language as the Epoch lineage), plus the **live in-flight
matchup** read from `active_tournament` rendered at the tip with a pulsing
edge and per-entry status chips.

```
 THE GAUNTLET                                               [+ − ⤢]
 (v0)══▶══(v1)══▶══(v2)┄┄▶┄┄(v4*)        Active matchup ●
  baseline  Δ−.31  Δ−.18    in flight    v2 vs v4   [flow →]
            │       │                    [short_solar done][long running]
            ╳ vX    ╳ v3
           DISCARD +.02
```

### 2.5 Lifecycle — *one candidate’s life as a DAG* (themes 1+2) (`#/C/lifecycle/:epoch/:gen`)

A left-to-right **lifecycle DAG** of a single candidate, read as a
life-story rather than a form:

```
 PARENT ─▶ PATCH ─▶ ╭ board fan ╮ ─▶ AGGREGATE ─▶ GATE ─▶ TERMINAL
 (lineage)  (cause)  ● waffles_single                       ♛ promoted
                     ● q3_metrics    Σ loss     REJECT      ✕ dead branch
                     ● picky_stake…
```

- **PARENT** node = the champion this challenger was patched off (lineage
  origin); for a seed it reads `∅ seed`.
- **PATCH** node = the cause — the count of mutation points the patch
  touched.
- **BOARD fan (theme 2)** = one node per board entry the candidate faces.
  Each node’s **radius + colour encodes its drift loss** (bigger/redder =
  worse; green = passed; amber = budget-exceeded); the **edge into the
  aggregate is weighted by that entry’s contribution** to the total loss.
- **AGGREGATE** = the summed scalar; **GATE** = the verdict climax (real
  `decision`/`delta_scalar` from `…/gate`); **TERMINAL** = a crowned
  champion (`♛ promoted`) or a `✕ dead branch`.

Below the spine, the **lineage DAG** draws the family tree — `v0` root →
`v1`/`v2` children, the champion **crowned (♛)** — every node a link to
that candidate’s own lifecycle. Animated flow runs along the spine.

### 2.6 Scoring — *per-board Sankey + 3-depth drill-down* (theme 3) (`#/C/scoring/:epoch/:gen`)

A **Sankey** (reusing `sankey.js`): `candidate → per-board loss →
aggregate scalar`, where **band width = each board’s contribution** to
the total loss; a board node’s colour is pass (green) / fail (red) /
timeout (amber). Three depths of drill-down:

1. the Sankey itself (board-field).
2. **click a board node** → it expands in the drawer into an
   **expectation sub-graph** (`…/expectations`, drawn as outcome nodes)
   plus a **per-judge loss** bar-graph (`…/per-judge`).
3. a **transcript flow** button (`/api/conversation/{run_id}`) renders
   the run turn-by-turn as a vertical flow.

### 2.7 Match-ups — *tournament-style topology switcher* (theme 4, the showcase) (`#/C/styles/:epoch`)

The **same candidate set re-laid-out under five selection structures**,
each a *different* graph topology, behind a style switcher that re-runs
layout on the same node ids:

| Style | Topology | Data |
|---|---|---|
| **Gauntlet** | star / hub (champion centre, challenger spokes) | **REAL** — `/api/tournaments` verdicts + deltas; spoke click opens the paired per-board **duel grid** (`/api/matchup-grid`) as a slopegraph |
| Single-elim | binary bracket tree | illustrative |
| Double-elim | two coupled trees (winners’ / losers’) | illustrative |
| Swiss | round-by-round bipartite pairing | illustrative |
| Racing | parallel lanes + elimination cut-lines | illustrative |

Only the gauntlet carries real per-round data; the other four are honest
**conceptual overlays** of the same generations (SELECTION.md §2/§5/§6),
each labelled `illustrative` on its tab and `CONCEPTUAL OVERLAY` in its
banner. The gauntlet’s spoke / round-card click opens the **paired
(common-random-number) per-board duel** — champion loss vs challenger
loss per entry, coloured by who won, with a who-won tally.

### 2.8 Run + Bench (reachable from nav)

- **Run** (`#/C/run/:id`) — deliberately diagram-light: live status tiles,
  the rolling event tail, an active-runs picker, and the "open in
  harmonograf" handoff (harmonograf owns the execution view).
- **Bench** (`#/C/bench`) — the instrument panel: service identity, live
  phase, and the loop-health detectors that catch a *running-but-
  meaningless* loop.

## 3. Architecture

```
app_C.js                    entry; reuses core/{state,api,sse,format,dom};
  ├ variants/C/router.js    #/C/... hash routes + href builder
  ├ variants/C/chrome.js    persistent shell: nav · status pill · drawer
  ├ variants/C/model.js     pure selectors over AppState (total functions)
  ├ variants/C/diagram/
  │   ├ surface.js          pan/zoom SVG canvas (wheel-zoom, drag-pan)
  │   ├ primitives.js       verdict palette · bezier/ribbon paths · layered DAG
  │   ├ sankey.js           the 3-stage Sankey layout (patch→drift→gate AND candidate→board→aggregate)
  │   └ topology.js         five tournament-style layouts over one candidate set (theme 4)
  └ variants/C/views/       environment · epoch · lifecycle · experiment · scoring · styles · tournament · run · bench
css/variants/C/variant.css  --v2-* tokens (scoped to .cz-root) + all styling
```

**Data.** Everything is live. The shared `core/api.js` reads one
consolidated `/api/environment`; `core/sse.js` coalesces `state_change`
frames into a single debounced refresh. Variant C adds only lazy
drill-down fetches: `/api/drift-movements/:gen` (the Sankey's effect
column), `/api/files/:e/:g/patches` and `.../diff` (the cause detail).
The proposer brief comes from `state.epochDef.brief` (the `/api/epoch`
contract; the on-disk file is `brief.md`, legacy `rubric.md`).

**Re-render safety.** A `state:changed` or `hashchange` schedules one
debounced render. The chrome (nav, pill, drawer) is built **once** and
patched in place; each screen owns and repaints its own `stage`. Per-gen
fetch caches mean an SSE tick never re-fetches. Honest states throughout:
loading vs empty vs populated are distinct.

**No collisions.** `layoutDag` assigns every node a distinct `(col,row)`
cell and only ever draws left→right beziers between columns, so lineage
and gauntlet edges can never tangle — verified in `variant_c.test.mjs`.

## 4. Reaching it

Load the dashboard with `?ui=C`. The orchestrator wires the query param
and provides `#variant-root`; `app_C.js` paints into it and self-injects
its stylesheet (idempotent) so a bare load also works. Routes live under
`#/C/...` and never collide with the shipped shell's un-prefixed space.

## 5. Tests

`static/test/variant_c.test.mjs` covers the router, the collision-free
DAG layout, the Sankey layout (stage ordering + magnitude-proportional
heights), the chrome + drawer, and that each hero screen paints real
content from `state` (objective headline, brief drawer prose,
patch→drift→gate Sankey + verdict, spine vs branch placement) alongside
honest empty states.

`static/test/variant_c_enrich.test.mjs` covers the enrichment wave: the
new routes; the lifecycle DAG’s six columns + board fan + crowned lineage
DAG; the per-board scoring Sankey + clickable board drill-down; the five
tournament-style topologies (every style keeps the same candidate ids in
a distinct shape, exactly one is flagged real, the switcher re-lays-out
on click, the real gauntlet round opens the paired duel grid) — all
offline with stubbed network and graceful degradation on empty data.
