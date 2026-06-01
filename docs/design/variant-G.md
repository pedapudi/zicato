# Variant G — "Bridge": A done right

Bridge is a round-2 synthesis: **Variant A's command-center navigation,
rebuilt right.** A user successfully navigated Variant A — "almost
everything I wanted to click took me where I wanted to go" — and
explicitly liked its **Fleet epoch-trendline home**. But A's information
density, its heavy live-ops console aesthetic, and four bugs spoiled it.
Bridge keeps A's exact navigation/IA and the Fleet home, and:

- **Replaces every dense/ugly A element with calmer data-viz.** The
  per-entry drift table becomes a **board × generation heatmap**; the
  gauntlet SVG becomes a **non-colliding bumps chart**; the A/B grid
  becomes **paired slopegraphs**; the per-board scores become a sorted
  **value dot-plot** with a clean drill-down; the candidate lifecycle is
  a **patch → drift → gate Sankey** plus the lineage DAG.
- **Adopts a calmer design language** — a Solarized-Dark token system
  (ground `#04222B`, surface `#0A2D38`, ink `#93A1A1`, improve `#8BB80E`,
  regress `#E0483C`, caution `#C4920A`) instead of A's loud console look.
- **Fixes all four A bugs.** This variant is the proof they are gone.

It is **self-contained** under `js/variants/G/**` + `css/variants/G/**`
— it imports nothing from variants A/B/C/D. The data-viz toolkit
(`svg.js`) and the diagram layouts (`diagram/*`) are adopted copies,
re-themed via the variant's own tokens.

## Screens (A's IA, rebound under `#/G`)

| Screen | What it shows |
|---|---|
| **Environment** (`#/G/`) | The Fleet: every epoch as a card with a quiet loss sparkline, plus the cross-epoch trendline hero. |
| **Epoch** (`#/G/epoch/:e`) | Objective + proposer-brief drawer; lineage as a non-colliding bumps chart; board entry × generation drift **heatmap**. |
| **Candidate** (`#/G/experiment/:e/:g`) | Verdict readout; the **patch → drift → gate Sankey**; the go/no-go gate; per-board **dot-plot** with clean drill-down; patch diff drawer. |
| **Match-ups** (`#/G/tournament/:e`) | The real gauntlet as **paired slopegraphs**, plus a topology switcher (gauntlet real; single/double-elim, Swiss, racing illustrative). |
| **Run** (`#/G/run/:runId`) | Status + the reconstructed working transcript (turns, tool calls, drift annotations). |
| **Bench** (`#/G/bench`) | Runs in flight + a clean, scrolling live event tail. |

## The four A bugs — fixed and proven

The whole shell follows the v2 render discipline: ONE persistent content
host (never recreated on a repaint), cleared only on a view switch;
`state:changed` coalesced into one repaint per frame; every view
**digest-gated** on structural data that EXCLUDES timestamps; the elapsed
clock ticks via a chrome-only text patch, never a view repaint.

1. **Drill-down flashing / constant refresh.** The selected board entry
   lives in **module scope**; the candidate view is digest-gated and the
   digest folds in the selection + whether its drill data has loaded —
   but not timestamps. A heartbeat tick leaves the digest unchanged, so
   the view (and its drilldown) is a no-op. The drilldown rebuilds only
   when the selection changes or its data arrives.
   *Proven by* `BUG#1: experiment drilldown does NOT rebuild on a
   heartbeat-only state change`.

2. **Empty transcript on cold deep-link.** The Run view reads its run id
   from the route params and, on any entry (cold or warm), fetches both
   `/api/run/{id}` and `/api/conversation/{id}`, rendering the
   conversation's turns. First paint is a loading state, then content —
   never empty.
   *Proven by* `BUG#2: cold deep-link run view renders transcript turns
   (not empty)`.

3. **Jerky looping hover on fleet cards.** The Environment view is
   digest-gated, so a heartbeat does not replace the fleet-card DOM; the
   cards persist across heartbeats and their hover uses a CSS
   `transition` (never an infinite keyframe), so it never resets.
   *Proven by* `BUG#3: environment render is a no-op when epoch/fleet
   data is unchanged` (asserts the card node survives a heartbeat
   repaint).

4. **Overlapping log lines in the bench event tail.** The tail is a
   constrained-scroll container (`height` + `overflow-y:auto`, flex with
   `min-height:0`) and the rows are normal block/flex-column **siblings**
   — never absolutely positioned — so they cannot overlap.
   *Proven by* `BUG#4: bench event tail is a constrained-scroll container
   with sibling rows`.

## Tests

`test/variant_g.test.mjs` (21 cases): router/IA, components, the D
data-viz non-collision guarantees (`decollide`, `jitterColumn`), the C
diagram layouts (DAG distinct cells, Sankey columns), model selectors
against the live data (one epoch, v0 crowned, v1/v2 rejected), the six
view renders, and the four explicit bug regressions above. Green under
`node test/run-all.mjs`.
