# Variant O — "Compass": a master-detail two-pane workspace

Compass is the round-4 convergence-II variant that answers the operator's
sharpest complaint: clicking a board card "dumped them on candidate v2 with
no fidelity for which candidate." Compass makes **selection explicit and
persistent**. A fixed left **selector rail** lists the epoch → its
generations → its board entries; a right **detail pane** shows whatever is
selected. The selection is encoded in the URL, so it survives reloads and a
cold deep-link hydrates both panes. The operator rejected K's paper-first
metaphor, so Compass is dashboard-first and folds the (best-judged) K
publication renderer in as a **facet/tab**, not the home.

Default color theme: **solarized-dark**. Default typeface theme:
**Display** (Open Sans body + Archivo Narrow for headings & big numbers).
All visuals are Tufte, fit-to-width — no pan/zoom viewport diagrams.

Everything is self-contained under `js/variants/O/**` + `css/variants/O/**`;
only the shared `js/core/*` data layer is reused untouched. Google Fonts is
the single permitted external dependency (fonts only, system fallbacks +
`font-display:swap`).

## Layout

A clean CSS grid: a fixed-width rail (`--vo-rail-w`) + a flexible detail
column. The rail is its own constrained-scroll column; the detail pane
scrolls independently. The top chrome carries branding, a **typeface
picker** (Sans / Editorial / Technical / Display), a **color theme picker**
(Dark / Light / Monokai), and a connection status pill.

### The rail (master)
`rail.js` — epoch header, then a **Generations** group (each row: a
promote/reject dot, the id, a crown for the champion, its Δscalar) and a
**Board** group (each row: a kind glyph + the entry id). A generation row
selects the candidate detail; a **board row opens the per-board
cross-candidate view, keyed by the entry id — never an arbitrary
candidate.** The rail digest-gates its repaint and highlights the active
selection.

### The detail pane (detail), by selection
- **Overview** (`#/O/`, `views/overview.js`) — workspace-at-a-glance stats +
  the clickable lineage bumps.
- **Generation** (`#/O/gen/<gen>/<facet>`, `views/candidate.js`) — a facet
  tab bar switches:
  - **Lifecycle** — per-board value dot-plot (champion reference line) + the
    Tufte sankey + the promote gate.
  - **Match-ups** — the gauntlet bumps ladder + the paired per-board duel +
    the gate for that round.
  - **Mutations** — ONE cohesive visual: the site × generation matrix + a
    side-by-side baseline|new diff that fills on cell-select.
  - **Publication** — K's ACM paper renderer (`paper.js` + the GFM markdown
    in `ui.js`).
- **Board** (`#/O/board/<entryId>`, `views/board.js`) — the NEW first-class
  per-board cross-candidate view: the entry contract, a sorted comparative
  bar chart (one bar per candidate, champion reference), the paired duel
  context, and a drill list to each candidate's run for THIS board.
- **Run** (`#/O/gen/<gen>/run/<entry>`, `views/run.js`) — the reconstructed
  transcript in a constrained-scroll container, with a themed "back to
  board" link.

## The seven mandated fixes (round-4 appendix)

1. **Promote gate** (`ui.gatePanel`) — clean STACKED sections: a decision
   header (pill + Δscalar/Δpass-rate), a rules ladder (one row per rule:
   label · status · detail, nothing overlapping), and a SEPARATE
   champion-vs-challenger scalar-components table below.
2. **Mutation view** (`views/candidate.js` mutations facet + `ui.sideBySideDiff`)
   — one cohesive visual: the `svg.mutationMatrix` site×generation surface +
   a side-by-side diff filling on cell-select. Baseline = the STRING at
   `/api/mutations/{e}/{mid}` `.baseline.content`; challenger = the matching
   patch's `.new_content` from `/api/files/{e}/{g}/patches`. Both are
   strings — the `[object Object]` bug (rendering the baseline object) cannot
   recur.
3. **Publication** (`paper.js`, `ui.renderMarkdown`) — reuses K's renderer;
   GFM **tables render** as real `<table>`s; the aggregate-generation-scores
   table + summary bar chart are COMBINED into one figure
   (`figCombinedScores`); a per-matchup detail figure is included.
4. **Heatmap theme-aware ramp** (`ui.heatRamp` → `svg.heatmap` `ramp`) — the
   ramp endpoints are read from the active theme's `--vo-heat-lo/-hi` tokens
   at draw time (with per-theme fallbacks for the no-DOM test harness).
5. **Sankey label ≠ value** (`svg.sankey`) — the per-board node's label is
   left-aligned and truncated; its loss value is a SEPARATE text node,
   right-aligned at the node's inner edge (or dropped to a second baseline on
   tall nodes). The value never overprints the label.
6. **Proportional figure sizing** — a shared `--vo-fig-max` width caps every
   figure/gate/run-list; figures share a `.vo-figure` frame so no plate
   dwarfs its neighbours.
7. **Per-board cross-candidate view** (`views/board.js`) — first-class:
   selecting a board entry (rail, sankey board, heatmap cell, or a per-board
   dot row) routes here BY ENTRY ID. Shows every candidate's loss +
   pass/fail/timeout, a sorted comparative chart, paired context, and a drill
   to each candidate's run for that board.

## Typeface picker

`ui.typefaceSwitcher` + `applyTypeface` set `data-vo-type` on the root and
persist it. The CSS swaps `--vo-ui` / `--vo-display` / `--vo-mono`:
- **Sans** — Open Sans throughout.
- **Editorial** — Open Sans body + Source Serif 4 for headings & publication.
- **Technical** — Open Sans body + JetBrains Mono for data / labels / code.
- **Display** (O's default) — Open Sans body + Archivo Narrow for headings &
  big numbers.

## Render discipline

`shell.js` mirrors the v2/E digest-gate blueprint: each pane digest-gates
its repaint off structural data only (heartbeat timestamps excluded); on a
selection change the detail host is cleared and the live drill-down cache is
invalidated (rail host reused); re-renders are debounced through the active
selection's digest-gated render; the URL encodes the full selection so a
cold load hydrates both panes; hover is CSS `transition`, never an infinite
keyframe animation.

## Tests

`test/variant_o.test.mjs` pins the router (typed selection + deep-link
round-trip + the mutations site slot), the two-pane layout (rail + detail),
the per-board cross-candidate view opening by entry id (and routing a run by
entry id, not an arbitrary candidate), the side-by-side mutation diff with
real strings (no `[object Object]`), the gate's stacked rules-ladder +
separate scalar-components block, the sankey label≠value separation, the
theme-aware heatmap ramp, the typeface + color pickers switching/persisting,
the digest-gated no-op, the cold deep-link transcript hydration, and the GFM
table rendering in the publication facet.
