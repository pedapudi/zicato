# Dashboard Variant A — "Mission Control"

> One of four parallel dashboard explorations. A full visual redesign —
> not a reskin. Built fresh under `static/js/variants/A/**` +
> `static/css/variants/A/**`, reusing only the shared data layer
> (`js/core/{api,sse,state,dom,format,bus}.js`). Reached at runtime with
> `?ui=A`; the orchestrator wires the flag and injects `#variant-root`.

## 1. Concept

A dark **live-ops console** — NASA mission control crossed with a
trading terminal. The operator is flying a meta-loop that mutates an
agent (the **cause** = a patch), runs it over a board, measures
**drift / loss** (the **effect**), and a **gate** promotes or rejects
(the **verdict**). Mission Control makes that read as instruments:

- **Big confident numbers.** Scalars, deltas and verdicts are large
  monospace readouts with semantic glow (green = improve/promote, red =
  regress/reject, blue = live, violet = champion lineage), never buried
  in tables.
- **Status lights everywhere.** A persistent live status strip in the
  top bar pulses with the heartbeat phase; every epoch card, run row and
  gauntlet node carries a light.
- **Motion signals liveness.** The in-flight generation pulses; the live
  status light breathes; the elapsed clock ticks. Nothing animates that
  isn't actually live.
- **Dense but composed.** Each screen is a small set of framed
  instruments with a bezel accent rail — not a wall of text, not an ugly
  table.

Custom dark palette is the default and the point. (The shared `--v2-*`
theme tokens can be ridden if the orchestrator opts in, but the bespoke
dark is what makes it Mission Control.)

## 2. Navigation model

A hash router under a distinct `#/A/` prefix, dispatched by a **shell
that owns one persistent content host**. A view switch parses the route
and repaints into the *same* host — it never creates a fresh host, so
nav can't orphan listeners or blank the page (the broken-nav bug the
brief warns about is structurally impossible here).

| Route | Screen |
|---|---|
| `#/A/` | **Environment** — the fleet (home) |
| `#/A/epoch/:epochId` | **Epoch** — control panel |
| `#/A/experiment/:epochId/:genId` | **Experiment** — telemetry readout |
| `#/A/tournament/:epochId` | **Lineage / gauntlet** viz |
| `#/A/run/:runId` | **Run** transcript (lighter) |
| `#/A/bench` | **Bench** — live ops (lighter) |

The operator always knows where they are (a live **breadcrumb** in the
top strip, always rooted at *environment*) and can always get home (the
brand mark and the first crumb), plus a **⌘K palette** to jump to any
epoch / generation / page, built live from state.

Re-render under SSE is coalesced into one paint per frame; chrome is
patched in place (no flash), views build fresh nodes into a single
`textContent=''`+append swap (one frame).

## 3. How the proposer brief is surfaced

The brief can be **long and complex**, so it gets a **dedicated, well
designed home** on the Epoch control panel: a collapsible drawer
directly under the objective, rendering the brief's **full markdown**
(headings, lists, fenced code, inline emphasis) via a tiny safe
markdown→DOM renderer (no `innerHTML`). It is never a truncated line.

The data is real: `GET /api/epoch` returns `brief` (the full `brief.md`
/ legacy `rubric.md` text) alongside `goal`. The objective is rendered
prominently as a violet callout above the brief; the brief drawer is its
own framed instrument below. When no brief is authored, the drawer says
so honestly and explains it is the brief's home when one exists — the
*gap is surfaced, the home is still designed*.

## 4. Hero screens (ASCII mockups)

### 4.1 Environment — the fleet (home)

The whole workspace at once. Top vitals readouts, then every epoch as a
console card with a status light, mini loss-trajectory, gens / best /
promoted, and the live epoch glowing. Loop-health panel + cross-epoch
trajectory below. This is the "see the environment AS A WHOLE" view that
was lost in the current UI.

```
┌ ZICATO ─────────────────────────────────────────────────────────────────────┐
│ ◢ MISSION CONTROL   environment            ● RUNNING · hardened_research · 01:02:07   bench  ⌘K │
└──────────────────────────────────────────────────────────────────────────────┘
  ENVIRONMENT   the workspace as a fleet — every epoch, at a glance

  ┌ EPOCHS ─────┐ ┌ GENERATIONS ┐ ┌ BEST SCALAR ┐ ┌ PHASE ─────────┐
  │   3         │ │   13        │ │   0.420     │ │   RUNNING       │
  │ 2 open      │ │ 6 promoted  │ │ lowest·fleet│ │ tournament live │   ← big glowing readouts
  └─────────────┘ └─────────────┘ └─────────────┘ └─────────────────┘

  ┌ FLEET · 3 epochs ───────────────────────────────────────────────────────────┐
  │ ┌ bootstrap        [closed]┐ ┌ hardened_research   ● LIVE ┐ ┌ latency_pass [open]┐│
  │ │ Get the agent answering. │ │ Cut confabulation on long… │ │ Trim coordinator…  ││
  │ │  ╲___        best  0.910 │ │  ╲____       best  0.420   │ │  ╲___    best 0.660││  ← sparkline +
  │ │      ╲_      gens     4   │ │       ╲___   gens     6    │ │      ╲_  gens    3 ││     stat grid
  │ │            promoted  2    │ │            promoted  3     │ │        promoted  1 ││
  │ └──────────────────────────┘ └════════════════════════════┘ └────────────────────┘│  (live=glow border)
  └──────────────────────────────────────────────────────────────────────────────┘

  ┌ LOOP HEALTH · hardened_research ────────────────────────────────────────────┐
  │ [warn] narrow_score_variance — last 2 gens differ by < 0.01 scalar.          │
  └──────────────────────────────────────────────────────────────────────────────┘

  ┌ CROSS-EPOCH TRAJECTORY · best scalar per epoch · lower is better ────────────┐
  │  ●╲                                                          ╱●               │  ← wide violet curve
  │     ╲____________________________________________________●╱                  │
  └──────────────────────────────────────────────────────────────────────────────┘
```

### 4.2 Epoch — control panel

Objective up top (prominent violet callout), vitals, the **proposer
brief drawer** (its real home), the **gauntlet** as a bold bracket with
**clean lanes** (champion spine straight along the top; each rejected
challenger in its own offset lane, dashed branch, never colliding; the
in-flight node pulses), then the **board-entry × generation drift
heatmap** (hover → detail, click → drill to the experiment).

```
  EPOCH  hardened_research

  ┌ MISSION OBJECTIVE ──────────────────────────────────────────────────────────┐
  │ Cut confabulation on long research briefs while keeping citations tight.      │  ← violet callout, big
  └──────────────────────────────────────────────────────────────────────────────┘

  [ GENERATIONS 5 ]  [ PROMOTED 3 ]  [ SPINE Δscalar -0.490 ]  [ STATE OPEN ]   ← readouts

  ┌ PROPOSER BRIEF · the operator's brief to the proposer ─────────── expand ∨ ──┐
  │  (collapsed; expands to the FULL markdown brief — headings, lists, code)      │  ← dedicated drawer
  └──────────────────────────────────────────────────────────────────────────────┘

  ┌ GAUNTLET · champion spine · challengers in own lanes · click → telemetry ────┐
  │                                                                               │
  │   (v0)━━━━━(v1)━━━━━(v3)━━━━━(v4)━━━━━(v5)   ← spine, violet; v5 pulses (live) │
  │   1.000     0.740 ╲  0.600    0.460    ◀running                                │
  │                    ╲ (own lane, dashed)                                        │
  │                    (v2)  +0.03 ✗        ← rejected challenger, red             │
  │   ● champion spine   ● promoted   ● rejected   ● in flight                     │
  └──────────────────────────────────────────────────────────────────────────────┘

  ┌ BOARD ENTRY × GENERATION DRIFT · green low → red high · hover · click drill ──┐
  │                v0    v1    v2    v3    v4                                      │
  │  contradictory ▢amb  ▢amb  ▢amb  ▢amb  ▢amb                                    │  ← glowing cells
  │  long_brief    ▢red  ▢red  ▢red  ▢red  ▢red                                    │
  │  short_solar   ▢grn  ▢grn  ▢grn  ▢grn  ▢grn                                    │
  └──────────────────────────────────────────────────────────────────────────────┘
```

### 4.3 Experiment — telemetry readout

Visual-first causal story **CODE CHANGE → DRIFT MOVEMENT → VERDICT**.
Leads with the verdict + headline deltas, then cause / verdict side by
side (the gate is a glowing **GO / NO-GO** panel with the three promote
rules checked off), then the drift that moved as a diverging-bar table,
then the per-entry A/B grid. The **patch diff is a collapsed drawer at
the very bottom** — secondary, never the top wall. A seed (v0) instead
shows its absolute baseline board results, no comparison.

```
  EXPERIMENT  hardened_research · v1

  ┌ VERDICT ────┐ ┌ Δ SCALAR ──┐ ┌ Δ DRIFT LOSS ┐ ┌ Δ PASS RATE ┐
  │ PROMOTED    │ │  -0.170    │ │  -0.200      │ │  +0.050     │   ← verdict leads, glowing
  └─────────────┘ └────────────┘ └──────────────┘ └─────────────┘

  ┌ THE CHANGE (CAUSE) ───────────────┐ ┌ THE VERDICT · promote gate · go/no-go ──┐
  │ Tighten researcher prompt for     │ │   ┌─────────┐  ✓ Scalar margin  Δ=-0.170 │
  │ citations.                        │ │   │   ▲     │  ✓ Pass-rate mono Δ=+0.050  │
  │ why · confab on 60% of research   │ │   │ PROMOTE │  ✓ Namespace monotonicity   │
  │ [researcher.instruction] [.descr] │ │   └─────────┘                             │
  └───────────────────────────────────┘ └──────────────────────────────────────────┘

  ┌ DRIFT MOVEMENT (EFFECT) · champion → challenger · green = improved ──────────┐
  │  CONFABULATION_RISK    9 → 3    ◀━━━━━━│           -6  (green, improved)       │
  │  CAPABILITY_MISMATCH   2 → 4           │━━▶         +2  (red, regressed)       │
  │  PLAN_REVISION         5 → 5           │             0                          │
  └──────────────────────────────────────────────────────────────────────────────┘

  ┌ PER-ENTRY A/B · champion vs challenger ──────────────────────────────────────┐
  │  short_solar   0.420 ✓   0.310 ✓   -0.110   [child]                           │
  │  long_brief    0.550 ✗   0.400 ✓   -0.150   [child]                           │
  └──────────────────────────────────────────────────────────────────────────────┘

  ┌ PATCH DIFF · the exact change — secondary to the verdict ─────── expand ∨ ────┐
  └──────────────────────────────────────────────────────────────────────────────┘
```

### 4.4 Lineage / gauntlet viz

The whole epoch's king-of-the-hill gauntlet as a bold, non-colliding
bracket — champion through-line plus every challenger matchup,
promote/reject color-coded, each node clickable into its experiment
telemetry. When a tournament is in flight, the **active matchup**
renders live below from `active_tournament.json` (champion vs
challenger, per-entry status, partial scalars).

```
  ┌ GAUNTLET — FULL LINEAGE · 5 champion hops · 1 rejected · click any node ──────┐
  │   (v0)━━━(v1)━━━(v3)━━━(v4)━━━(v5◀running)                                     │
  │            ╲(v2 ✗)                                                             │
  └──────────────────────────────────────────────────────────────────────────────┘
  ┌ ACTIVE MATCHUP · round 5 of 6 · live ────────────────────────────────────────┐
  │ [done 3] [running 2] [queued 1]  champion 0.48   challenger 0.40              │
  │  short_solar    0.42 ✓        0.31 ✓                                          │
  │  long_brief     0.55 ✗        ● running                                        │
  └──────────────────────────────────────────────────────────────────────────────┘
```

## 4b. Enrichment — the four shared themes, in Mission Control's idiom

The variant-enrichment wave adds the four cross-variant themes
(candidate lifecycle, the boards a candidate faces, per-board scoring +
drill-down, match-ups across tournament styles). Each is expressed as an
instrument, bound only to the real API (see `_ENRICHMENT-BRIEF.md`), and
degrades honestly against the live one-epoch / three-generation /
all-fail data.

### 4b.1 Candidate lifecycle — the mission track + command roster

On the **Experiment** view, a candidate's life reads as a horizontal
**telemetry track**: BORN (patch armed off a parent) → BOARD SORTIE
(entries firing) → PROMOTE GATE (GO / NO-GO) → OUTCOME (crowned champion,
or aborted dead branch). Each station carries a status light; the rail
between stations lights up to the furthest stage reached.

```
  ┌ CANDIDATE LIFECYCLE · born → board sortie → gate → outcome ──────────────────┐
  │   (✓)━━━━━━━━(✓)━━━━━━━━(✗)━━━━━━━━(✗)                                        │
  │   Born      Board       Promote     Aborted                                   │
  │   patch off  sortie      gate        dead branch                              │
  │   v0         4 entries   NO-GO                                                 │
  │   ● reached  ● go/crowned  ● no-go/aborted  ● in flight  ● not reached         │
  └──────────────────────────────────────────────────────────────────────────────┘
```

On the **Lineage** view, the lineage is a **command roster** — a defended
hill: the crowned champion banner at the top (defending, pulsing GO
light), challengers as call-signs below (call-sign · ↳ parent · verdict ·
Δscalar), with dead branches dimmed and live ones ringed.

```
  ┌ COMMAND ROSTER · champion defending · dead branches dimmed ──────────────────┐
  │  ♚  REIGNING CHAMPION · DEFENDING        v0                              ●go   │
  │  ───────────────────────────────────────────────────────────────────────────  │
  │  ● v1   ↳ v0   DEAD BRANCH                                          +75.71      │  (dimmed)
  │  ● v2   ↳ v0   DEAD BRANCH                                           +1.51      │  (dimmed)
  └──────────────────────────────────────────────────────────────────────────────┘
```

### 4b.2 The boards a candidate faces — the sortie board

The fixed per-epoch board is a **status-light tile grid** (not a wall of
rows): every entry the candidate "faces" is a tile carrying the kind
(◆ single / ⋯◆ scripted / ⟳◆ emulated), the input preview, budget_s,
weight, tags, a per-entry **loss bar**, and a status **lamp**
(pass = green / fail = red / timeout = amber). A tally strip sums the
lamps.

```
  ┌ SORTIE BOARD · lamp = pass/fail/timeout · click a tile to drill in ──────────┐
  │ 4 entries  ●0 pass  ●3 fail  ●1 timeout  ●0 unflown                            │
  │ ┌●waffles_single  ◆single┐ ┌●q3_metrics_outline ◆single┐ ┌●waffles_revision ⋯◆┐│
  │ │ Make a presentation…    │ │ Outline a deck on Q3…     │ │ (multi-turn script) ││
  │ │ ▇▇▇▇▇▁▁▁  60.5          │ │ ▇▇▇▇▇▇▁▁  63.5            │ │ ▇▇▇▇▇▇▇▇  88.0     ││
  │ │ ⏱180s ×1.0  TIMEOUT     │ │ ⏱180s ×1.0  FAIL          │ │ ⏱240s ×1.0  FAIL   ││
  │ └─────────────────────────┘ └───────────────────────────┘ └────────────────────┘│
  └──────────────────────────────────────────────────────────────────────────────┘
```

### 4b.3 Per-board scoring + drill-down — the instrument panel

Three depths. **Depth 1** is the loss bar on each sortie tile. **Depth 2**:
clicking a tile slides in an **instrument panel** showing the entry's
**expectation outcomes** (`…/expectations`) with pass/fail marks + detail,
and its **per-judge loss bars** (`…/per-judge`). **Depth 3**: an *open run
transcript →* button routes to the run view (`/api/conversation/{run_id}`,
`run_id` taken from the per-entry record).

```
  ┌ instrument · board entry   waffles_single                              [✕] ──┐
  │ single_turn   ⏱180s   loss 60.5   [TIMEOUT]                                   │
  │ ┌ EXPECTATION OUTCOMES ─────────┐ ┌ PER-JUDGE LOSS ────────────────────────┐ │
  │ │ ✗ predicate                   │ │ incorporates_feedback ▇▇▇▇▇▇ 27.0  ×1.0 │ │
  │ │   predicate returned False    │ │                                         │ │
  │ └───────────────────────────────┘ └─────────────────────────────────────────┘ │
  │ [ open run transcript → ]                                                      │
  └──────────────────────────────────────────────────────────────────────────────┘
```

### 4b.4 Match-ups across tournament styles — the match-up theatre

The **REAL** mechanism is rendered first: a **king-of-the-hill ladder**
(`/api/tournaments`) — the champion crowned at the top defending, each
round a rung (`R1 v0 vs v1 … champion holds / challenger crowned`).
Expanding a rung loads the paired **per-board duel**
(`/api/matchup-grid/…`): parent vs child loss bars per entry with a
`won_by`. Then a **style switcher** re-renders the SAME candidate set
under the other documented structures, each banner-labelled
**CONCEPTUAL — not how zicato ran this epoch**, with a *different* visual
topology per style (SELECTION.md §2/§5/§6):

- **single-elim** → an SVG bracket tree (the wrong primitive — noise-fragile),
- **double-elim** → a winners' rail + a losers' bracket (a second life),
- **swiss** → a pairing table with running scores,
- **racing** → race lanes with an elimination cut-line (survivors keep racing → replicate).

```
  ┌ MATCH-UP THEATRE ────────────────────────────────────────────────────────────┐
  │ [●Gauntlet] [Single-elim] [Double-elim] [Swiss] [Racing]                       │
  │ REAL · how zicato actually ran this epoch — king of the hill, paired board.    │
  │ ♚ KING OF THE HILL  v0  defending                                              │
  │ R1  v0 vs v1   Δ +75.71   [champion holds] ▾                                   │
  │      q3_metrics_outline   ▇▇▇ 71.0 │ ▇▇ 63.5      won_by v1                     │
  │      picky_stakeholder    ▇▇ 105.5 │ ▇▇▇▇▇▇ 642.5  won_by v0                    │
  │ R2  v0 vs v2   Δ +1.51    [champion holds]                                      │
  └──────────────────────────────────────────────────────────────────────────────┘
  (switch to Racing →)
  ┌ CONCEPTUAL — race lanes with an elimination cut; §5 recommendation ───────────┐
  │ v0   ▸──────────────────────────────●  70.94   (leader)                        │
  │ v2   ▸───────────────────────●          72.45                                  │
  │ v1   ▸──────────●  (out)                146.65                                 │
  │ ╴╴ elimination cut-line @ 72.45 — lanes past the cut are dropped; replicate    │
  └──────────────────────────────────────────────────────────────────────────────┘
```

## 5. Files

```
static/app_A.js                                  entry — paints into #variant-root
static/css/variants/A/mission-control.css        the whole dark instrument theme (.mcA scope)
static/js/variants/A/router.js                   #/A/ hash router + breadcrumb model
static/js/variants/A/shell.js                    persistent shell: top strip, status pill, dispatch
static/js/variants/A/components/instruments.js   panel, readout, chip, sparkline, heatmap, tooltip
static/js/variants/A/components/gauntlet.js      the clean-lane bracket SVG
static/js/variants/A/components/markdown.js      tiny safe markdown → DOM (for the brief)
static/js/variants/A/components/palette.js       ⌘K command palette
static/js/variants/A/components/lifecycle.js     theme 1 — mission track + command roster
static/js/variants/A/components/sortie.js        theme 2/3 — sortie-board status-light tiles + tally
static/js/variants/A/components/drilldown.js     theme 3 — slide-in instrument panel (expectations + per-judge)
static/js/variants/A/components/matchups.js      theme 4 — gauntlet ladder + style switcher (single/double/swiss/racing)
static/js/variants/A/views/environment.js        L0 fleet (home)
static/js/variants/A/views/epoch.js              L1 control panel (objective + brief + gauntlet + heatmap)
static/js/variants/A/views/experiment.js         L3 telemetry readout (lifecycle track + sortie board + drill-down; verdict-first; diff drawer)
static/js/variants/A/views/tournament.js         lineage command roster + match-up theatre + bracket SVG + live matchup
static/js/variants/A/views/run.js                L4 run transcript (lighter)
static/js/variants/A/views/bench.js              live ops (lighter)
static/variant_A_preview.html                    dev/preview host (?mock=1 for offline review)
static/test/variant_a.test.mjs                   node tests (router, gauntlet, markdown, views)
static/test/variant_a_enrich.test.mjs            node tests (lifecycle, sortie board, drill-down, match-up styles)
```

## 6. Data sources (reused, not rebuilt)

All reads go through the shared `core/api.js` + `core/sse.js`. Endpoints
used: `/api/workspace`, `/api/health-report`, `/api/active-tournament`,
`/api/environment` (folds `epoch` = the contract incl. `goal` + `brief`
+ `board` + `experiments`), `/api/generation/{e}/{g}/per-entry`,
`/api/matchup-grid/{e}/{c}/{ch}`, `/api/drift-movements/{g}`,
`/api/files/{e}/{g}/diff`, `/api/run/{id}`, `/api/conversation/{id}`.

The enrichment views add: `/api/tournaments` (the real gauntlet — champion
lineage + per-round matchups), `/api/generation/{e}/{g}/per-entry` (the
sortie-board lamps + per-entry loss + `run_id`),
`/api/run/{e}/{g}/{entry}/expectations` and `…/per-judge` (the drill-down
instrument panel). The board itself comes from the folded `epoch.board`.

Note on the gate: the brief references `/api/round/.../gate`, which is
**not** a shipped route. The promote verdict + the three-rule
evaluation are reconstructed deterministically on the client from the
experiment `outcome` (decision + `scalar_score_delta` + `pass_rate_delta`
+ `rejection_reason`) per SELECTION.md §3.2 — so the go/no-go panel is
honest about what each rule did without needing a server gate endpoint.

Every panel degrades to an honest state: *loading* while a fetch is in
flight, *empty* with a reason when there is genuinely no data, and the
last-known view stays painted across a transient SSE drop.
```
