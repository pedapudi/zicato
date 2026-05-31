# Variant D — "Tufte data-viz"

> One of four parallel dashboard explorations the operator chooses
> between. Reach it via `?ui=D`; the orchestrator paints it into
> `#variant-root`. Routes are hash-scoped under `#/D/…`.

## Concept

A beautiful interactive **Tufte poster** for the meta-loop. The thesis
of every screen is the same as zicato's: a **patch** (the cause) drives
**drift / loss** (the effect), and a **gate** returns a **verdict**,
across **generations** and **epochs**. Variant D refuses raw tables and
text walls — **every number that has a trend is a tiny chart**.

Design rules, applied uniformly:

- **High data-ink, minimal chrome.** Hairline rules, no boxes-in-boxes,
  whitespace instead of borders, captions in a quiet mono.
- **Small multiples everywhere.** Per-epoch trajectories, predicted-vs-
  actual bullets, per-judge trends — all rendered as grids of word-sized
  marks the eye can scan in one pass.
- **Sparklines + range-frames.** Each sparkline carries a faint min–max
  band and a single emphasised end-dot coloured by direction (loss ↓ =
  good, teal; loss ↑ = bad, rose). Gaps break the line — never an
  interpolated lie.
- **A slopegraph done RIGHT.** The classic slopegraph fails when labels
  collide. Variant D runs a one-dimensional *de-collision* pass on each
  column (push neighbours apart to a minimum gap, clamp to frame) and
  draws a hairline **leader** from the nudged label back to the true
  datum — the connecting line always terminates at the real value.
- **A non-colliding bumps chart.** The lineage gives the **champion
  spine its own lane** and branches **rejected challengers into a
  distinct lower lane**, fixing the collision in the current UI where
  champion and challenger share a row.
- **Refined `--v2-*` palette.** A calm, precise default: warm paper
  ground, near-black warm ink, **teal** (good) and **rose** (bad) as the
  only saturated accents, **terracotta** for the champion spine.
  Diverging/sequential ramps are tuned to the same colourway. Dark mode
  via `prefers-color-scheme`.

Everything is **dependency-free SVG** built with the shared `core/dom.js`
`svgEl` helper. Every interactive mark has a native `<title>` (hover →
exact value) and a click target (drill down). The whole variant reuses
the shared data layer (`core/{api,sse,state,format,dom}.js`) untouched;
its own code lives under `js/variants/D/**` + `css/variants/D/**`, entry
`app_D.js`.

## Files

```
static/app_D.js                         entry — injects CSS, mounts into #variant-root
static/css/variants/D/tufte.css         --v2-* palette + all mark styling (scoped to #variant-root)
static/js/variants/D/
  app.js        orchestrator: nav, hash router wiring, SSE re-render
  router.js     #/D/… hash router (parseRoute / href / navigate)
  svg.js        the data-viz toolkit: sparkline, sparkbar, dotPlot,
                valueDotPlot, valueBars, genDots, slopegraph,
                pairedSlopegraph + jitterColumn, bumps, heatmap,
                predictedActual, bracketMini, roundRobinMatrix, raceLanes,
                smallMultiple, decollide
  data.js       cached, failure-tolerant drill-down reads over core/api
                (incl. lineage, gate, expectations, per-judge-for-run,
                conversation)
  ui.js         crumb, sections, verdict pills, SAFE markdown (the brief)
  views/
    environment.js   cross-epoch small multiples + master slopegraph
    epoch.js         objective + brief + bumps lineage + heatmaps
    lifecycle.js     THEME 1 — per-candidate loss-profile small multiples
    experiment.js    predicted-vs-actual + drift slopegraph + gate + diff
    bench.js         THEME 2 — the board trellis of small multiples
    run.js           THEME 3 — per-board scoring dot-plot → entry detail → transcript
    tournament.js    THEME 4 — paired non-colliding slopegraphs + alt styles
static/test/variant_d.test.mjs          22 unit tests (node)
static/test/variant_d_enrich.test.mjs   21 enrichment tests (node)
```

## Where the proposer brief lives

The brief is long and complex markdown. It is read from `/api/epoch`
(the `brief` key — `state_reader.build_epoch_view` reads `brief.md`,
falling back to the legacy `rubric.md`). Variant D gives it a **clean,
readable, collapsible panel** on the **Epoch** screen, directly under
the prominent objective banner. It is rendered by a defensive tiny-
markdown renderer (`ui.renderMarkdown`) that builds DOM nodes — headings,
paragraphs, bullet lists, inline/fenced code — and **never uses
innerHTML**, so the brief is always readable, never raw, never an
injection. A long brief starts collapsed; a short one starts open.

---

## Hero screen — Environment (cross-epoch)

The whole workspace at once: per-epoch small-multiple sparklines + a
master cross-epoch slopegraph of best scalar. (Data: `/api/workspace`,
`/api/score-trajectory`, `/api/health-report`.)

```
environment
Environment
The whole workspace across epochs.  lower is better.

┌──────────────────────────────────────────────────────────────────┐
│  2        4           3            0.200       2026-05-16_e0       │
│  EPOCHS   GENERATIONS PROMOTIONS   BEST SCALAR LIVE EPOCH          │
└──────────────────────────────────────────────────────────────────┘

Cross-epoch trajectory · best scalar
        PREV EPOCH                                NEXT EPOCH
        2026-05-16_e0 0.20 ●━━━━━━━━━━━━━━━━━━━●  0.14  2026-05-17_e1
                            ╲ (slope down = improving)
        ── improved (loss↓)   ── regressed (loss↑)   bold = live epoch

Per-epoch trajectories          (small multiples, one sparkline each)
  ● 2026-05-16_e0    0.200      ╱╲╱   ◀ click → epoch
    Tighten the planner…
    2026-05-17_e1       —       ┄┄┄
    Reduce wall-clock variance…

Loop health
  ✓ No loop-health findings — the meta-loop looks healthy.
```

## Hero screen — Epoch (the data substrate)

Objective prominent; the brief gets a real home; the lineage is a
**non-colliding bumps chart**; board × generations and per-judge × gen
are quiet heatmaps. (Data: `/api/epoch`, `/api/score-trajectory`,
`/api/generation/{e}/{g}/per-entry`, `/api/epoch/{e}/per-judge-trend`.)

```
environment › epoch
Epoch 2026-05-16_e0
▏OBJECTIVE
▏Tighten the planner to reduce drift on multi-turn boards.   ← prominent

┌ 1 BOARD  4 EXPERIMENTS  3 PROMOTED  -0.150 Δ SPINE  open STATE ┐

Operator’s brief to the proposer
▾ Proposer brief · 6 lines                          ← collapsible panel
   BRIEF
   GOAL
   Tighten the planner to reduce drift on multi-turn boards.
   …(headings, paragraphs, bullets, code — safe markdown)…

Lineage                                  ← NON-COLLIDING bumps chart
  CHAMPION   v0 ●━━━━━━━●━━━━━━━━━━━━━━━━━●  v2     (spine = own lane)
                 ╲v0      v1               v2
  CHALLENGER       ╲╌╌╌╌╌╌○ v1a                    (rejected = own lane)
  ── champion spine   ○ rejected challenger   click a node → experiment

Board entries × generations · drift loss        ← themed heatmap
                v0  v1  v1a v2
   entry_alpha [▓] [░] [▓] [ ]    darker = more drift · click → experiment

Per-judge trend                                 ← quiet heatmap
              v0  v1  v2
   critic_A  [▓] [░] [░]
   critic_B  [▓] [░] [ ]      weighted loss per judge across the spine
```

## Hero screen — Experiment (predicted-vs-actual, visual-first)

CODE CHANGE → DRIFT → VERDICT, **led by visuals**. The diff is secondary
and collapsible. (Data: `/api/epoch` experiment record,
`/api/matchup-grid/…`, `/api/drift-movements/{g}`, `/api/files/…/diff`.
A v0 seed shows its absolute baseline board results instead.)

```
environment › epoch › experiment v1
Experiment v1  (promoted)
Derived from v0 by a patch. The cause → the drift → the gate's verdict.
“Add an explicit plan-revision budget to the planner prompt.”

Predicted vs actual            ← small multiples (the bet vs the outcome)
  scalar Δ -0.050   pass-rate Δ +0.120   drift-loss Δ -0.040
  ●──○|             |○──●                 ●──○|
  ○ predicted (the bet)   ● actual (the outcome)   ┄ prediction error

Drift by kind · champion → challenger        ← slopegraph (non-colliding)
  (per-kind counts champion → challenger; fewer = teal, more = rose)

Per-entry deltas                              ← sorted dot plot
   entry_alpha ●┤          left/teal = improved
   entry_beta   ●┤         right/rose = regressed   click → run
   entry_gamma   ├──●

The gate                                      ← compact visual
  0.420 │━━━━━━━━━━━━━━━━━━━━━━━━━━━━━│ 0.380
  CHAMPION                           CHALLENGER
  -0.040 (promoted)
  scalar components · Δ champion → challenger
     drift_loss ●┤
     pass_rate   ├●

The cause (code change)
  ▸ Patch & code diff · 2 files                ← collapsed by default
```

## Tournament / lineage — the slopegraph done right

Every champion-vs-challenger matchup as one non-colliding slopegraph,
plus the bumps lanes. (Data: `/api/epoch`, `/api/score-trajectory`.)

```
environment › tournament
Tournament · lineage
Each line is one matchup. Slope down = challenger lowered loss & promoted.

Matchups
        CHAMPION                          CHALLENGER
        v1  0.50 ●━━━━━━━━━━━━━━━━━━━━━━━● 0.30  v1   (teal, bold=promoted)
        v1a 0.50 ●╲                       ● 0.70  v1a (rose, regressed)
              de-collided ↑  (both champions at 0.50 keep separate labels
              with hairline leaders to the true datum — no overlap)
        v2  0.30 ●━━━━━━━━━━━━━━━━━━━━━━━● 0.20  v2
        ── challenger improved   ── regressed   bold = promoted · click → exp

Lineage lanes  (bumps chart — champion spine + rejected challenger lane)
```

---

## The four enrichment themes (Tufte idiom)

The variant carries the four shared themes, each in the high-data-ink /
small-multiples language. New SVG primitives live in `svg.js`
(`sparkbar`, `genDots`, `valueDotPlot`, `valueBars`, `pairedSlopegraph`,
`jitterColumn`, `bracketMini`, `roundRobinMatrix`, `raceLanes`); new
views are `lifecycle.js`, an enriched `bench.js` (now "Boards"), an
enriched `run.js` (now "Scoring"), and an enriched `tournament.js` (now
"Match-ups").

### 1 · Candidate lifecycle — `views/lifecycle.js`

A **small-multiple strip**: one tiny multiple per candidate, each a
`sparkbar` of that candidate's per-board drift-loss profile (one thin bar
per board entry, on a **shared loss scale** so the strip is directly
comparable), topped by a gate-verdict glyph — `▲` promoted (teal), `▼`
rejected (rose). Failed entries carry a foot-tick, timed-out entries are
hatched. Each card footers the life-story: `← parent` and the verdict
pill. Beneath the strip, the **lineage bumps** chart gives the champion
spine its own lane and branches rejected challengers into a distinct
lower lane (the non-colliding lineage). Click a candidate → its per-board
scoring; click a bumps node → its experiment.

### 2 · The boards a candidate faces — `views/bench.js`

A **trellis of small multiples**, one micro-chart per board entry (not a
table of rows). Each cell shows the entry's `kind` tag (single /
scripted / emulated), budget, weight, tags and input preview; a
`sparkbar` of that entry's loss **across the candidate generations** on a
trellis-wide shared scale; and a `genDots` row of pass / fail / timeout
glyphs, one per generation, beneath the bars. The trellis is **sorted
meaningfully** — by kind (emulated → scripted → single), then descending
weight, then id — so the heaviest, most-structured tests read first.
Click a board → its run scoring.

### 3 · Per-board scoring + drill-down — `views/run.js`

Three depths, narrowing:

- **Depth 1** — a sorted `valueDotPlot` of one candidate's absolute
  per-entry drift loss (lower = left = better) with a **reference line at
  the champion's scalar**; dots left of it (beating the champion) read
  teal, right read rose; a pass/fail/timeout glyph trails each row. A
  candidate switcher lets the operator move across generations.
- **Depth 2** — clicking an entry opens its detail small-multiple:
  expectation outcomes as pass/fail/no-verdict dots (from
  `…/expectations`) and the per-judge weighted losses as direct-labelled
  `valueBars` (from `…/per-judge`).
- **Depth 3** — a collapsible transcript panel lazily loads
  `/api/conversation/{run_id}` and renders the turns, tool calls, and
  drift / judge margin annotations.

### 4 · Match-ups across tournament styles — `views/tournament.js`

The real **king-of-the-hill gauntlet** first: a non-colliding bumps
ladder, then one **paired slopegraph per round** drawn from the real
`/api/matchup-grid` — champion loss → challenger loss for *every board
entry*. The operator flagged colliding slopegraph lines as a defect;
`pairedSlopegraph` defeats collision three ways at once: (a) a per-column
**label de-collision** pass with hairline leaders back to the true datum;
(b) a node **jitter** (`jitterColumn`) that fans coincident values a hair
apart so two lines ending at the same loss do not overdraw; (c) **direct
labelling** at both ends. Lines are coloured by `verdict`
(improved / regressed / flat). Below, the same candidate set under
**alternative structures**, each a different topology and clearly badged
*illustrative* (only the gauntlet has real per-round data, per
SELECTION.md §6): a single-elimination `bracketMini` tree, a round-robin
`roundRobinMatrix` heat grid, and a successive-halving `raceLanes` dot
plot with an elimination cut.

## Interaction & honesty

- **Hover → exact value** on every mark (native SVG `<title>`).
- **Click → drill down**: epoch tiles → Epoch, lineage/bumps nodes &
  slopegraph lines → Experiment, dot-plot rows → Run.
- **Honest states**: empty/loading/unavailable are explicit and quiet
  (e.g. "No drift-kind movements recorded", "index not built"); a failed
  drill-down fetch is cached as `null` and surfaced as unavailable, then
  retried after a live invalidation.
- **Re-render-safe under SSE**: a `state:changed` tick busts the live
  drill-down cache and re-runs *only the current view*; a view switch
  paints into a fresh host (clean unmount), so listeners never leak and
  panels never flash.
```
