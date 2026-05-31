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
| **Causal Sankey** (patch → drift → gate) | Experiment | "*this* change moved *these* drift kinds, so the gate said *this*" |

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

### 2.5 Run + Bench (reachable from nav)

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
  │   └ sankey.js           the 3-stage patch→drift→gate layout
  └ variants/C/views/       environment · epoch · experiment · tournament · run · bench
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

`static/test/variant_c.test.mjs` (17 cases, run by `node test/run-all.mjs`)
covers the router, the collision-free DAG layout, the Sankey layout
(stage ordering + magnitude-proportional heights), the chrome + drawer,
and that each hero screen paints real content from `state` (objective
headline, brief drawer prose, patch→drift→gate Sankey + verdict, spine vs
branch placement) alongside honest empty states.
