# Variant L — "Atlas III" (convergence-II main line)

The main-line convergence-II dashboard for zicato: a clean dark dashboard
with every round-3 fix folded in and all seven Round-4 fixes applied. Built
on Variant E's IA/flow (confirmed "likely fine"), with the ACM publication
reused as a TAB (K's renderer — judged the best of the round) rather than the
home. Self-contained under `js/variants/L/**` + `css/variants/L/**`; nothing
imported from other variant dirs (ported what was needed).

Wired via `?ui=L` (already in index.html → loads `app_L.js`, sets
`data-variant="L"`).

## Identity

- **Dashboard-first**, refined and balanced — proportional, legible.
- Default **colour** theme: solarized-dark (clean dark). Three colour themes
  (solarized-light / -dark / monokai), every mark legible in all three.
- Default **typeface** theme: "Sans" (Open Sans). Four Open-Sans-based
  Google-Fonts pairings (see Typeface picker below).
- The safe converged pick.

## Screens

- **Environment** (`home`) — the workspace as a fleet: one console card per
  epoch with a per-epoch loss trendline hero, a cross-epoch overview strip,
  loop health, and a cross-epoch trajectory sparkline.
- **Epoch** (`epoch`) — objective + proposer brief, then proportionally-sized
  figures (lineage bumps beside the theme-aware drift heatmap) and the board
  trellis small-multiples. Heatmap cells and trellis cards route to the Board
  view by entry id.
- **Candidate** (`candidate`) — the lifecycle: a Tufte causal-flow sankey
  (candidate → per-board loss → aggregate scalar), the promote GATE (clean
  stacked sections), the per-board scoring dot-plot, and the per-entry
  drill-down (expectations + per-judge losses + a link to the transcript).
- **Board** (`board`) — NEW. One board entry across EVERY candidate:
  per-candidate loss + pass/fail/timeout, a sorted comparative bar chart, and
  a per-candidate table that drills to each run. Trellis/heatmap/dot-plot
  clicks land here, keyed by entry id.
- **Match-ups** (`matchups`) — the real gauntlet ladder (bumps), per-round
  paired slopegraphs (de-collided + jittered, click → run), and illustrative
  alternative structures (bracket / round-robin / race lanes) on the same set.
- **Mutations** (`mutations`) — NEW combined visual: the mutation-site ×
  generation matrix plus a detail pane that fills on cell-select with a
  side-by-side, line-diffed view (champion baseline | challenger new).
- **Publication** (`publication`) — the ACM-style epoch publication as a tab,
  reusing K's paper renderer; GFM tables render; the aggregate-generation-
  scores table + summary bar chart are combined into one figure; per-matchup
  detail is added.
- **Run** (`run`) — the deepest screen: one run's reconstructed transcript,
  with first-class cold deep-link hydration and an honest fallback.

## The seven Round-4 fixes

1. **Promote gate — no overlap.** `candidate.js` lays the gate out as three
   STACKED, fit-to-width sub-blocks: (a) decision pill + Δscalar / Δpass-rate,
   (b) the rules ladder — each rule its OWN three-column row (label · status ·
   detail), nothing overlapping, (c) a SEPARATE champion-vs-challenger
   scalar-components comparison table below.
2. **Mutation view — one cohesive visual + side-by-side diff.** `mutations.js`
   renders the site × generation matrix; a cell selects both a generation and
   a site, filling a detail pane with `svg.sideBySideDiff` (two line-diffed
   columns). The baseline STRING comes from
   `GET /api/mutations/{epoch}/{mutation_id}` → `.baseline.content` (never the
   `.baseline` object — the "[object Object]" fix); the challenger STRING from
   the matching `patches[].new_content`; a full-file `…/diff` fallback covers
   either side missing.
3. **Publication — K's renderer; tables; combined table+chart; matchup.**
   `publication.js` reuses the ported `paper.js`; GFM tables render via L's
   `renderMarkdown`; `figAggregateScores` combines the scores table and its
   summary bar chart into one figure; `figMatchup` adds champion-vs-challenger
   per-board detail from `/api/matchup-grid`.
4. **Heatmap theme-aware ramp.** `ui.themeRamp()` reads `--l-heat-lo` /
   `--l-heat-hi` from the active colour theme via getComputedStyle, falling
   back to a per-theme lookup keyed by `data-vl-theme`; `svg.heatmap` colours
   cells from that ramp. No fixed orange/brown.
5. **Sankey label/value alignment.** `svg.sankey` draws each board node's
   label left-anchored at the node's left and its loss value right-anchored to
   the node's right edge — disjoint x-ranges on a shared baseline, so a long
   entry id and its value can never overlap.
6. **Proportional figure sizing.** The epoch page wraps figures in
   `.vl-figrow` / `.vl-figcard` with a shared `--l-fig-max` so the heatmap can
   never dwarf the bumps/trellis; SVGs are `max-width:100%`.
7. **Per-board cross-candidate view.** New `board.js` + `#/L/board/<entryId>`
   route; epoch trellis cards AND heatmap cells (and the candidate dot-plot)
   navigate here by entry id.

## Typeface picker (new)

A second picker beside the colour picker, offering four Open-Sans-based
pairings, each re-mapping the `--l-sans` / `--l-serif` / `--l-mono` /
`--l-display` family tokens (keyed off `data-vl-type`):

- **Sans** (default) — Open Sans throughout.
- **Editorial** — Open Sans body + Source Serif 4 headings & publication.
- **Technical** — Open Sans body + JetBrains Mono for data / labels / code.
- **Display** — Open Sans body + Archivo Narrow for headings & big numbers.

Fonts load via a Google-Fonts `<link>` in `app_L.js` (`display=swap`, the only
permitted external dependency — fonts only); every token carries a system
fallback. The choice persists to `localStorage` (`zicato.vl.type`); the colour
choice persists to `zicato.vl.theme`.

## Render discipline

Mirrors E's digest-gated spine: one persistent host; every view digest-gates
its repaint on structural data only (no timestamps), so a heartbeat re-dispatch
writes zero DOM; the host is cleared on a view switch (and the live cache
invalidated there, never on a heartbeat); both pickers re-skin via root
data-attributes (CSS only, no view rebuild); transcripts scroll in a
constrained container; cold deep-link routes fetch their own data; all
diagrams are fit-to-width (viewBox + preserveAspectRatio), never pan/zoom.

## Files

- `app_L.js` — entry: injects the scoped stylesheet + the Google-Fonts link,
  mounts the shell.
- `js/variants/L/{shell,router,data,ui,svg,paper}.js`
- `js/variants/L/views/{home,epoch,candidate,board,matchups,mutations,publication,run}.js`
- `css/variants/L/atlas.css`
- `test/variant_l.test.mjs`

## Tests

`node test/variant_l.test.mjs` — 16 cases covering the side-by-side mutation
diff (real string baseline+new, no "[object Object]"), the per-board view +
trellis/heatmap routing by entry id, the stacked non-overlapping gate, the
separate sankey label/value, the theme-token heatmap ramp, both pickers, the
digest no-op, the cold deep-link transcript, and the publication GFM table.
