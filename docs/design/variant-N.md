# Variant N — "Console II": dense observatory, convergence II

Console II is the round-4 **convergence-II** dashboard. It keeps Variant E's
IA/flow wholesale (confirmed "likely fine") and refines Variant J's dense,
data-ink-maximal Console skin — but addresses the seven mandatory fixes the
operator raised against the round-3 variants, reuses Variant K's *publication
renderer* (judged the best of the round) as a TAB rather than the home, and adds
a second chrome picker (typeface) beside the colour picker.

Default colour theme: **monokai**. Default typeface theme: **Technical**
(Open Sans body + JetBrains Mono for data / labels / code). Compact chrome, more
small-multiples per screen, tight spacing — but everything legible and
proportional (no oversized-heatmap-beside-tiny-bumps problem).

Self-contained under `js/variants/N/**` + `css/variants/N/**` + the entry
`app_N.js`; reuses only the shared `js/core/*` data spine and imports from no
other variant directory. Selected behind `?ui=N` (already wired in index.html).

## Screens (router prefix `#/N/`)

| route | screen |
| --- | --- |
| `#/N/` | **Environment** — the workspace as a fleet (overview strip, per-epoch loss trendline cards, cross-epoch trajectory). |
| `#/N/epoch[/<id>]` | **Epoch** — objective + proposer brief, non-colliding lineage bumps, theme-aware drift heatmap, board trellis. |
| `#/N/candidate/<gen>[/<entry>]` | **Candidate** — compact lifecycle DAG + per-board dot-plot; URL-driven entry drill. |
| `#/N/matchups` | **Match-ups** — fit-to-width Tufte sankey, gauntlet bumps, paired slopegraphs, **promote gate** (stacked), illustrative alternatives. |
| `#/N/mutations[/<mutId>]` | **Mutations** — site × generation matrix **+ side-by-side diff** in one cohesive layout. |
| `#/N/board/<entry>` | **Board** (NEW) — one board entry across **every** candidate. |
| `#/N/publication[/<id>]` | **Publication** — K's ACM renderer as a tab (GFM tables, combined table+chart, per-matchup detail). |
| `#/N/run/<gen>/<entry>` | **Run** — the reconstructed transcript (cold-deep-link safe). |

## The two pickers

- **Colour** — monokai (default) · solarized-dark · solarized-light, swapped via
  `[data-n-theme]` on the variant root; CSS-only re-skin, persisted.
- **Typeface** (NEW) — **Sans** (Open Sans throughout) · **Editorial** (+ Source
  Serif 4 headings & publication) · **Technical** (default; + JetBrains Mono for
  data/labels/code) · **Display** (+ Archivo Narrow condensed headings). Swapped
  via `[data-n-type]`; the heading / data / publication voices switch through
  `--n-font-*` custom properties, persisted. Google Fonts are loaded in
  `app_N.js` via a `<link>` (fonts only — the sole permitted external dependency)
  with system fallbacks and `display=swap`.

## The seven mandatory fixes

1. **Promote gate** — clean STACKED sections, nothing overlaps, fit-to-width:
   (a) a decision header (pill + Δscalar / Δpass-rate + primary driver), (b) the
   rules ladder with each rule on its OWN grid row (label · status · detail),
   short-circuiting in order, (c) a SEPARATE champion-vs-challenger
   scalar-components comparison table below. Bound to
   `/api/round/{e}/{champ}/{chall}/gate` for every decided round.
2. **Mutation view** — ONE cohesive visual: the site × generation matrix and a
   detail pane that fills on select with a **side-by-side** line diff (two
   columns: champion baseline | challenger new), LCS-aligned. Baseline string =
   `/api/mutations/{epoch}/{mutation_id}` → `.baseline.content` (the STRING, not
   the object — that was the "[object Object]" bug). Challenger string =
   `/api/files/{epoch}/{gen}/patches` → matching `mutation_id` → `.new_content`
   (+ `.op`, `.rationale`), with the detail `versions[].content` as a fallback.
3. **Publication** — reuses K's renderer approach as a TAB. GFM **tables render**
   (the markdown renderer parses header/`---`/body rows into a real `<table>`).
   The aggregate-generation-scores TABLE and its summary BAR CHART are COMBINED
   into ONE figure; per-matchup detail (champion vs challenger per board, from
   `/api/matchup-grid/...`) is appended to the paper.
4. **Heatmap** — theme-aware: each cell is the themed ink token at a value-driven
   `fill-opacity` (no fixed orange/brown hex ramp), so it reads in all three
   colour themes — especially monokai.
5. **Sankey label/value alignment** — each per-board drift node's loss VALUE is a
   distinct, right-aligned `text` mark on its own; the label is truncated harder
   to reserve room, so label and value never overlap.
6. **Proportional figure sizing** — shared figure widths / row heights; the drift
   heatmap is sized to sit beside, not dwarf, its neighbours.
7. **Per-board cross-candidate view** (NEW) — a dedicated page for one board
   entry: per-candidate loss + pass/fail/timeout as a sorted comparative
   dot-plot (champion reference rule) and a tabular breakdown, drilling to each
   candidate's run. **Board trellis cards AND heatmap cells route HERE**, keyed
   by entry id — not to an arbitrary candidate. Pivots
   `/api/generation/{e}/{g}/per-entry` by `entry_id` across generations.

## Render discipline (follows E; `js/v2/shell.js`)

ONE persistent content host (never recreated); every view digest-gates its
repaint on structural data only (timestamps excluded) via `gatedSwap`, so a
steady heartbeat re-dispatch writes zero DOM; the host is cleared on a view
switch; drill-down caches invalidate only on a route change; cold deep-link
routes fetch their own data with honest fallbacks; motion is CSS `transition`,
never `animation: … infinite`; transcript tails are constrained-scroll
containers; NO diagram lives in a pan/zoom viewport (every mark fits its
container).

## Files

- `app_N.js` — entry (injects the Google Fonts link + the scoped sheet, mounts the shell).
- `js/variants/N/shell.js` — compact top bar, colour + typeface pickers, digest-gated dispatch.
- `js/variants/N/router.js` — `#/N/` hash router + breadcrumb (adds board + publication).
- `js/variants/N/data.js` — cached, failure-tolerant reads (adds `mutationDetail`).
- `js/variants/N/ui.js` — gatedSwap, helpers, the GFM-capable markdown renderer, the theme tables.
- `js/variants/N/svg.js` — self-contained `dn-*` data-viz incl. the theme-aware heatmap, the fit-to-width sankey (label≠value), and the side-by-side diff.
- `js/variants/N/dag.js` — the compact lifecycle DAG (`ezn-*`).
- `js/variants/N/views/{home,epoch,candidate,matchups,mutations,board,publication,run}.js`.
- `css/variants/N/console2.css` — the dense Console II skin, the three colour theme token sets, and the four typeface themes, scoped under `#variant-root[data-variant="N"]`.

## Tests

`test/variant_n.test.mjs` covers: the side-by-side mutation diff with real
strings (never "[object Object]"); the per-board view + trellis/heatmap routing
to it by entry id; the stacked, non-overlapping promote gate; the sankey
label≠value separation; the theme-token heatmap (monokai); both pickers
switching + persisting; the digest-gated no-op; the cold deep-link transcript;
and the publication GFM table rendering. Green; full suite 0 failures.
