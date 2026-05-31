# Variant B — "Editorial Lab Notebook"

> One of four parallel dashboard explorations the operator chooses between.
> Reached via `?ui=B`; mounts into `#variant-root`. Built fresh on top of the
> shared data layer (`js/core/*` + every `/api/*` endpoint) — only the
> presentation is new.

## Concept

The antithesis of a dense database browser. zicato is an **instrument for
automated science**, so Variant B reads like the *write-up* of that science:
a research magazine / lab notebook. Clean typography, generous whitespace,
a serif display voice for headlines and pull-quotes, restrained charts
embedded in flowing prose. Comparison is shown as **figures** (slopegraphs,
diverging bars, sparklines, an optimization-curve trajectory) and **prose**,
never as walls of tabular numbers.

The causal sentence zicato measures —

```
a CODE CHANGE  →  a DRIFT MOVEMENT  →  a VERDICT
 (the patch)      (how behavior moved)   (the gate)
```

— is the spine of every entry. Each experiment reads like a beautifully
typeset notebook page: **the bet as a pull-quote**, the result as elegant
charts + a clear verdict, and the diff tucked away as a tasteful, collapsible
secondary block (the cause is shown in full, but it never leads).

### Design language
- **Type:** a serif display/quote face (Iowan/Palatino/Georgia stack), a calm
  sans for body + UI, a mono for ids and code. Generous measure (~38rem),
  1.65 leading.
- **Themes:** light-first **Paper** (default), dark **Ink**, warm **Sepia** —
  all token sets under `#variant-root[data-vb-theme]`. Semantic colors
  (improve green / regress red / caution amber / running blue) hold across
  all three and are **always paired with a glyph** for accessibility.
- **Charts:** pure SVG factories, total functions (a degenerate input yields a
  labeled fallback, never a blank box or a throw). No tables.
- **Motion:** liveness only (a pulsing live node / live pill), gated by
  `prefers-reduced-motion`.

### Files
```
static/app_B.js                         entry; injects B's CSS, boots shell+router
static/js/variants/B/router.js          hash router, prefix #/B/...
static/js/variants/B/shell.js           masthead, nav, crumbs, theme, live pill
static/js/variants/B/lib/charts.js      sparkline · slopegraph · divergingBars
                                        · trajectoryStory · progressRing ·
                                        marginTimeline · genealogy · dotPlot ·
                                        headToHead
static/js/variants/B/lib/fixtures.js    tournament fixtures: gauntletFixture ·
                                        matchupGridFigure · bracketFixture ·
                                        doubleElimFixture · roundRobinFixture ·
                                        swissFixture · raceFixture
static/js/variants/B/lib/prose.js       SAFE markdown→DOM, renderBrief (TOC +
                                        collapsibles), pullQuote · verdictBadge
                                        · note · section · stat
static/js/variants/B/lib/data.js        shared selectors + tiny async-cache;
                                        boardEntries · gauntlet · fixtureField ·
                                        lifecycleSteps
static/js/variants/B/views/             environment · epoch · experiment ·
                                        board · tournament · run · bench
static/css/variants/B/tokens.css        paper/ink/sepia palettes (scoped)
static/css/variants/B/notebook.css      the editorial stylesheet (scoped)
static/test/variant_b.test.mjs          22 node tests (discovered by run-all)
static/test/variant_b_enrich.test.mjs   17 node tests for the four enrichment
                                        themes (discovered by run-all)
```

### How the proposer brief is surfaced
The brief is the operator's full, possibly-long instructions to the proposer.
It comes back as Markdown on `GET /api/epoch` (`epoch.brief`), with a one-line
`epoch.goal` distilled server-side. Variant B gives it a **real home** on the
Epoch page (§ Epoch view):
- The **objective** (`goal`) is set large as a pull-quote — the chapter thesis.
- The **brief body** is rendered by `renderBrief()` — a small, XSS-safe
  Markdown→DOM renderer (no `innerHTML`) producing a **table-of-contents rail**
  + **collapsible `<details>` sections** per `##` heading, so a long brief stays
  scannable and is never truncated to a single line.
- When no brief text exists, the section still renders its designed home with an
  honest "no proposer brief recorded" note (the goal stands in for it).

---

## Hero screen mockups

### 1 · Environment — the home (the whole workspace as a story)

```
 zicato  lab notebook    Environment  Epoch  Lineage  Bench        ● live   P I S
 ───────────────────────────────────────────────────────────────────────────────
 Environment

           THE WORKSPACE
           An automated-science notebook, read across 2 epochs.
           zicato mutates an agent, runs it over a board, measures the drift
           it causes, and a gate decides. Each epoch below is a chapter.
           ● The loop is healthy — no findings on the latest round.

  Lineage    ╭─ promoted spine (bold) ─────────────────────────╮
  The         v0●───v2●────────v4_seed●──v4●·····v5◌ (live, pulsing)
  curve         ╲v1✗   ╲v2x✗                       y = loss, lower better
  being       cross-epoch best loss  ▁▂▃▅▄  (sparkline)
  climbed.    (click a node → its experiment)

  Chapters   01  2026-05-10_e0
  each        ┌ Stabilise the extraction schema so invoice fields parse.
  epoch's     │   ▁▃▅▄▂ best loss 0.42      3 generations  2 promoted
  objective   └                                      Read the chapter →
  + arc       02  2026-05-15_e1                                  current
              ┌ Cut off-topic drift by compressing verbose tool docs.
              │   ▅▄▃▂▁ best loss 0.31      4 generations  3 promoted
              └                                      Read the chapter →
```

### 2 · Epoch — the chapter (objective + brief home + lineage + experiments)

```
  2026-05-15_e1  open
  ┌───────────────────────────────────────────────────────────┐
  │  Cut off-topic drift by compressing verbose                │  ← objective
  │  researcher tool docs.                                     │     pull-quote
  └───────────────────────────────────────────────────────────┘
  4 generations    3 promoted
  Rolled by 1 change vs 2026-05-10_e0:  [brief]

  Proposer's   ┌ Contents ──────────┐   # Proposer brief
  brief        │ Forbidden edits    │   Steering for the proposer this epoch.
  the full     │ Preferred edits    │   ▾ Forbidden edits
  instructions └────────────────────┘     • Do not touch `researcher.schema`…
  that shaped                             ▾ Preferred edits
  every bet                                 • Prefer compressing tool docs…

  Lineage      v4_seed●──v4●────v5◌   (trajectory; click → experiment)

  Experiments  ┌ v1     ✓Promoted ┐ ┌ v2     ✓Promoted ┐ ┌ v2x    ✗Rejected ┐
  newest first │ Tighten extract  │ │ Move JSON valid… │ │ Inline validator │
  (cards, not  │ Δloss −0.06 …    │ │ Δloss −0.04 …    │ │ Δloss +0.02 …    │
  a table)     └──────────────────┘ └──────────────────┘ └──────────────────┘
```

### 3 · Experiment — the notebook entry (bet → result → verdict → change)

```
  Generation v2   vs champion v1
  ┌───────────────────────────────────────────────────────────┐
  │  Move JSON validation earlier in the pipeline.            │  ← the bet
  └───────────────────────────────────────────────────────────┘     (pull-quote)
  predicted  pass-rate ↑, schema_violation ↓

  Verdict    ✓ Promoted (large badge)
             The challenger cleared the gate and was promoted to champion.
             ✓ Scalar margin   pass
             ✓ Pass-rate monotonicity   pass

  Drift      Per board entry, champion → challenger:
  movement   waffles      ◀────────●   −0.20  improved   (click → run)
             q3_metrics          ●─▶   +0.05  worsened
             Which drift kinds moved:  off_topic ◀──── −5  improved

  Scalar     champion ●────────────● challenger    ↓ 0.470
             Δ scalar −0.080   Δ pass +0.05   primary driver incorporates_feedback

  The change ▸ The change — patch diff   (collapsed, secondary)
             pipeline.order  [reorder]  validate-before-emit
```

The **seed (v0)** has no parent champion: instead of red "no champion" errors,
the entry opens "This is the seed — the absolute baseline…" and shows its
absolute per-entry board drift loss.

### 4 · Lineage — the trajectory / slopegraph

```
  THE CLIMB
  The lineage, end to end

  Trajectory       v0●───v2●─────v4_seed●──v4●····v5◌(live)
                     ╲v1✗   ╲v2x✗
  ● promoted  ● rejected  ● open  ── champion spine

  Verdicts   v5     ● Running   —      from v4     (click → experiment)
             v4     ✓ Promoted  0.310  from v4_seed
             v2x    ✗ Rejected  0.420  from v1
```

### 5 · Run — the transcript (typeset evidence)

```
  Run · waffles · generation v2
  What actually happened

  Transcript   2 turns. Annotations are framework steering, in the margin.

   USER  10:02:11
   Do the thing.

   AGENT · researcher  10:02:14                    │ drift
   Working on it. …                                │ off_topic detected
   [call] search                                   │
```

### 6 · Bench — live operations (editorial voice)

```
  ● Live
  A run is in flight

  Testing    "Compress researcher tool descriptions to under 80 tokens…"
             champion v4 → challenger v5   [round_1]

  The board  3 / 5 runs complete.  (click an entry → transcript)
             board entry          champion        challenger
             extract_invoice_001  done 0.4        ◐ 60%
             schema_response      done 0.5        queued

  Activity   10:02:14  research_agent call
             10:02:11  judge incorporates_feedback
```

## The four enrichment visualizations

The enrichment wave adds the four shared themes in the lab-notebook idiom.
Every figure is a pure SVG/CSS/DOM factory (no build step, no library), scoped
under `#variant-root`, total (a degenerate input yields a labeled fallback,
never a blank box or a throw), and bound only to the real `/api/*` endpoints.

### 1 · Candidate lifecycle (Experiment view)
The Experiment entry becomes a true notebook page. A **margin timeline**
(`marginTimeline`) runs down the left rail — *conceived → patched → ran the
board → judged → verdict* — each beat toned by what the data records (the
terminal beat fails red on a rejection). The hypothesis
(`hypothesis_core_idea`) is set as a **pull-quote**; the **rejection reason**
(from `/api/tournaments`) is set as a second, contrasting pull-quote. A
hand-drawn-feeling **genealogy figure** (`genealogy`) draws the family tree
(v0 → {v1, v2}) with curved connectors and the reigning champion marked with a
crown ♔; every node is clickable into its own entry.

### 2 · The board a candidate faces (The Board view, `#/B/board`)
The fixed task suite as a typeset **plate**, not a grid of cells and not a
table. Entries are **grouped by `kind`** (single-turn / multi-turn scripted /
multi-turn emulated), each a quiet card with its id, an `input_preview` set as
a pull-line, **tags in small caps**, and **budget + weight hung as fine
marginal annotations**. A **figure caption** (entry count, kinds, total
weight) closes the plate. Binds to `epoch.board`.

### 3 · Per-board scoring + drill-down (Experiment view)
Three depths. Depth 1: each board entry the candidate ran is a prose row with
a small **inline dot-plot** (`dotPlot`, the loss riding a thin track) and a
pass/fail glyph, bound to `/api/generation/{e}/{g}/per-entry`. Depth 2:
clicking a row opens a **footnote/aside in place** — its expectation outcomes
(`…/expectations`) and per-judge losses (`…/per-judge`). Depth 3: a *“read the
run transcript”* link drills to the run dialogue.

### 4 · Match-ups across tournament styles (Lineage view)
The **real king-of-the-hill gauntlet** (`gauntletFixture`, from
`/api/tournaments`) as a head-to-head **ladder** — one crowned champion spine,
each challenger a clickable rung with its verdict and Δscalar — and the heart
of one round, the **paired matchup grid** (`matchupGridFigure` +
`headToHead`, from `/api/matchup-grid/…`) as a beautifully set table-figure
(per board: champion vs challenger loss bars, Δ, won_by). Both are real data
and carry no “illustrative” mark. Then five **illustrative alternative
structures** over the same generation field, each a *different* topology with
a SELECTION.md-grounded caption and an explicit “not how this epoch ran” mark:
a single-elimination **bracket** engraving, a coupled winners’/losers’
**double-elimination** figure, a **round-robin matrix**, a **Swiss pairing
ledger**, and a **racing / successive-halving lane** chart
(`bracketFixture` · `doubleElimFixture` · `roundRobinFixture` ·
`swissFixture` · `raceFixture`, in `js/variants/B/lib/fixtures.js`).

## Honest states & re-render safety
Every async section renders through `note(kind)` — `not_yet | running | empty
| broken` — never a bare "No data". Views clear-and-repaint their own host but
the shell keeps the frame's node identity, and digest gates keep a heartbeat
tick from rewriting unchanged chrome. The seed, zero-turn runs, missing briefs,
absent index, and unreachable endpoints all degrade to a labeled state.

## Verification
`node static/test/variant_b.test.mjs` — 22 tests covering the chart toolkit's
totality, the safe Markdown brief renderer + TOC, the router, and every view
rendering against the shared mock snapshot (including the seed path, clickable
drill-downs, and re-render safety). `node static/test/variant_b_enrich.test.mjs`
— 17 tests covering the four enrichment themes: the new chart + fixture
primitives' totality, the board plate, the per-entry scoring drill-down (all
three depths), and the gauntlet + five illustrative tournament fixtures. Both
discovered by `static/test/run-all.mjs`.
