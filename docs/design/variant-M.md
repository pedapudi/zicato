# Variant M — "Ledger II"

Round-4 convergence-II dashboard. Editorial, light-first, publication-forward.
Built on Variant E's information architecture and flow (dashboard-first),
wearing a refined editorial skin descended from Variant I ("Ledger"). The
ACM-style epoch **Publication** is a prominent first-class tab that reuses
Variant K's publication renderer (judged the best of the round).

Activate with `?ui=M` (already wired in `index.html`). Entry point:
`app_M.js`. Everything visual is self-contained under
`js/variants/M/**` + `css/variants/M/**`, scoped to
`#variant-root[data-variant="M"]`.

## Identity

- **Light-first** — default colour theme `solarized-light`.
- **Editorial typeface** by default — Open Sans body + Source Serif 4 for
  headings & the publication (the new typeface picker; see below).
- Airy whitespace, typographic eyebrows / ledes / pull-quotes / figure
  captions; the publication is beautiful and prominent.

## Screens

- **Environment** (`home`) — the workspace as a fleet: an overview strip,
  per-epoch loss-trendline cards, loop health, a cross-epoch trajectory.
- **Epoch** (`epoch`) — the opening pages of a paper: objective lede, the
  proposer brief, proportionally-sized figures (lineage bumps, board ×
  generation drift heatmap, board trellis), and rails to the per-board view
  and the publication.
- **Candidate** (`candidate`) — one generation's life: hypothesis pull-quote,
  the fit-to-width Tufte causal-flow Sankey, the lifecycle DAG, the per-board
  scoring dot-plot, the **promote gate** as clean stacked sections, and the
  entry drill-down (expectation outcomes + per-judge losses + transcript link).
- **Match-ups** (`matchups`) — the real king-of-the-hill gauntlet ladder,
  per-round paired slopegraphs (de-collided + jittered), and illustrative
  overlays of the other tournament styles over the same candidate set.
- **Mutations** (`mutations`) — ONE cohesive visual: the mutation-site ×
  generation matrix plus a side-by-side champion-baseline-vs-challenger diff
  pane that fills on cell-select.
- **Board** (`board`, NEW) — per-board cross-candidate detail keyed by entry
  id: how every candidate scored on one board entry, a sorted comparative
  dot-plot vs the champion, and a per-candidate table drilling to each run.
- **Publication** (`paper`) — the ACM-style epoch paper, typeset via K's
  renderer, with the dashboard's own live Tufte figures embedded inline.
- **Run** (`run`) — the deepest screen: one run's reconstructed transcript,
  cold-deep-link hydrated.

## The seven mandatory convergence-II fixes

1. **Promote gate — stacked, no overlap.** `views/candidate.js` `gatePanel()`
   lays the gate out as three clean stacked sub-blocks: (a) decision pill +
   Δscalar / Δpass-rate, (b) the rules ladder with each rule its own row
   (label · status · detail), (c) a separate champion-vs-challenger
   scalar-components comparison. Bound from `/api/round/{e}/{champ}/{chall}/gate`.
2. **Mutation view — one cohesive visual + side-by-side diff.**
   `views/mutations.js` renders the site × generation matrix (`svg.mutationMatrix`,
   based on K's element) plus a detail pane with `ui.sideBySideDiff` (LCS-aligned
   two columns). The baseline string comes from
   `/api/mutations/{epoch}/{mutation_id}` → `.baseline.content`
   (`data.baselineContent` reads `.baseline.content`, never the object — that
   was the "[object Object]" bug); the challenger string from
   `/api/files/{epoch}/{gen}/patches` → matching `mutation_id` `.new_content`,
   with a full-file `/api/files/{epoch}/{gen}/diff` fallback.
3. **Publication — reuse K's renderer; tables; combined table+chart; matchup
   detail.** `views/paper.js` imports `js/variants/K/paper.js` +
   `js/variants/K/ui.js`. K's `renderMarkdown` renders GFM tables (fixing I's
   raw "| … |" aggregate-scores table). The aggregate-generation-scores table
   and its summary bar chart are combined into ONE figure
   (`aggregateScoresFig`). Per-matchup detail is a live paired slopegraph bound
   from `/api/matchup-grid/…`.
4. **Heatmap — theme-aware ramp.** `svg.rampColor` interpolates between the
   active colour theme's `--m-ramp-lo`/`--m-ramp-hi` (and `--m-ramp-good`/`-bad`)
   tokens read at draw time; legible in all three themes, no fixed orange/brown.
5. **Sankey label/value alignment.** `diagram/sankey.js` draws the entry id
   left-anchored and the loss value right-anchored on the same baseline
   (separate `i-sankey-label` / `i-sankey-value` marks) — they anchor from
   opposite edges and the label is clipped, so they never overlap.
6. **Proportional figure sizing.** A shared `m-fig-md` figure envelope caps
   figure widths so the drift heatmap no longer dwarfs the bumps / dot-plot.
7. **New per-board cross-candidate view; trellis + heatmap route to it.** The
   `board` view (NEW). In `views/epoch.js`, board-trellis cards AND heatmap
   cells route to `board` keyed by entry id — not to an arbitrary candidate.

## Typeface picker (NEW)

A second segmented control in the chrome beside the colour switcher
(`typeface.js`). Four Open-Sans-based options, persisted to localStorage,
written to `data-m-face` on the variant root:

- **Sans** — Open Sans throughout.
- **Editorial** (M's default) — Open Sans body + Source Serif 4 headings/paper.
- **Technical** — Open Sans body + JetBrains Mono for data / labels / code.
- **Display** — Open Sans body + Archivo Narrow (condensed) headings.

Fonts load via a single Google-Fonts `<link>` in `app_M.js` (the only
permitted external dependency — fonts only), with `display=swap` and
system-font fallbacks baked into `ledger2.css`, so the dashboard never blocks
on the network.

## Render discipline

Follows E / `js/v2/shell.js`: one persistent content host; every view
digest-gates its repaint (`ui.gatedSwap`, `data-m-digest`) so a steady
heartbeat writes zero DOM; the host is cleared and per-resource caches
invalidated only on a view switch; drill-down state lives in the URL; figures
are static SVG (no pan/zoom viewport); cold deep-links fetch their own data.

## Tests

`test/variant_m.test.mjs` (20 cases) covers all seven fixes, the typeface +
colour pickers (switch + persist), the digest no-op, and the cold deep-link
transcript. `node test/run-all.mjs` is green.
