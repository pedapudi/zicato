# Variant R — "Strata": Miller-columns over the data model, convergence III

Strata is a round-5 **convergence-III** dashboard. It keeps Variant N's
data-ink content and the same data model, but navigates it as macOS-Finder-style
**Miller columns** — cascading columns where each selection drives the next and
the rightmost pane is the detail — rather than N's flat tab nav or a nested
accordion tree. It is a distinct *expression of the same hierarchy*: the data
model laid out horizontally, with the whole column path encoded in the URL so a
cold deep-link reconstructs every column **and** the detail pane.

Default colour theme: **solarized-dark**. Default typeface: **Display** (Open
Sans body + Archivo Narrow condensed headings & big numbers).

Self-contained under `js/variants/R/**` + `css/variants/R/**` + the entry
`app_R.js`; reuses only the shared `js/core/*` data spine and imports from no
other variant directory. Selected behind `?ui=R` (already wired in index.html).
All CSS is scoped under `#variant-root[data-variant="R"]` (`dr-*` chrome/marks,
`ezr-*` lifecycle DAG).

## The column model (router prefix `#/R/`)

```
Col 1  Environment ─▸  Col 2  Epoch sections ─▸  Col 3  items ─▸  Detail pane
        the epoch(s)         Generations              the generations list   the selected item
                             Boards                   the board entries list
                             Mutation surface  ───────────────────────────▸  (no items → straight to detail)
                             Publication       ───────────────────────────▸  (no items → straight to detail)
```

Each column is independently scrollable and digest-gated (it rebuilds only when
its own structural data or its active selection changes; a heartbeat re-dispatch
writes zero DOM). Selection is explicit and URL-encoded as a flat segment path:

| path | detail pane |
| --- | --- |
| `#/R/` | **Environment** — the workspace as a fleet (overview strip, per-epoch trajectory, loop health). |
| `#/R/<epoch>` | **Epoch overview** — objective, proposer brief, lineage trajectory, board×generation drift **heatmap**. |
| `#/R/<epoch>/generations/<gen>` | **Candidate** — lifecycle DAG · per-board dot-plot · **promote gate** · **all match-ups** · **patch diff**. |
| `…/generations/<gen>/entry/<e>` | the candidate's per-entry drill (expectations + per-judge losses). |
| `…/generations/<gen>/patch/<id>` | that candidate's **side-by-side diff** for one mutation site. |
| `#/R/<epoch>/boards/<entry>` | **Board** — one entry across every candidate · dot-plot · **trellis** · breakdown. |
| `…/boards/<entry>/run/<gen>` | the **inline side-by-side transcript** (champion vs the selected candidate). |
| `#/R/<epoch>/mutations[/<id>]` | **Mutation surface** — site × generation matrix + side-by-side diff. |
| `#/R/<epoch>/publication` | **Publication** — K's ACM renderer (GFM tables, combined table+chart, per-matchup detail). |

One line per detail pane: **Environment** = the fleet at a glance;
**Epoch overview** = the epoch's substrate + the drift heatmap; **Candidate** =
one generation's full lifecycle, gate, every match-up and patch diff; **Board** =
one board entry across every candidate plus its trellis and an inline duel of
transcripts; **Mutation surface** = the whole epoch's site×generation matrix and
diff; **Publication** = the epoch's ACM paper with live figures.

## The two pickers

- **Colour** — solarized-dark (default) · monokai · solarized-light, swapped via
  `[data-r-theme]` on the variant root; CSS-only re-skin, persisted.
- **Typeface** — Sans · Editorial (Source Serif 4 headings) · Technical
  (JetBrains Mono data) · **Display** (default; Archivo Narrow condensed
  headings). Swapped via `[data-r-type]` through `--r-font-*` custom properties,
  persisted. Google Fonts are the only external dependency (fonts only,
  `display=swap`, system fallbacks).

## The seven mandatory fixes

1. **Promote gate on the candidate detail** — a stacked, non-overlapping gate
   (decision header → rules ladder, each rule its own row → a *separate*
   champion-vs-challenger scalar-components table) is rendered on every
   non-seed candidate's detail pane (N lacked it there).
2. **Patch node → per-candidate side-by-side diff** — the lifecycle DAG's PATCH
   node is clickable and a patch-sites rail lists each site this candidate
   touched; selecting one fills a diff built from REAL strings: baseline from
   `/api/mutations/{epoch}/{id}` `.baseline.content`, challenger from
   `/api/files/{epoch}/{gen}/patches` `.new_content`, full-file `/diff`
   fallback — never the baseline object (no "[object Object]").
3. **All match-ups for a candidate** — `/api/tournaments`.matchups filtered by
   `champion==gen || challenger==gen`, so the seed v0 shows v0-vs-v1 **and**
   v0-vs-v2, each as a de-collided paired slopegraph.
4. **Board entries are a first-class column** — Boards is one of the four
   sections in column 2; selecting it lists every board entry in column 3, each
   opening the per-board cross-candidate detail.
5. **Board entry → inline side-by-side transcript** — selecting a candidate's
   run on the board (dot-plot click, breakdown link, or `…/run/<gen>`) renders
   TWO transcripts side by side *in the board detail pane* — the champion and
   the selected candidate, each from `/api/conversation/{run_id}` — with no
   navigation to a separate page.
6. **Trellis vs heatmap de-dup** — the compact board×generation drift **heatmap**
   stays at the epoch overview; the board **trellis** (one bar per candidate)
   lives only in the board detail. Never both on one page.
7. **M's spacing/proportion + L's mutation-viewer quality** — roomy panels,
   generous section rhythm, and the L-quality side-by-side LCS diff reused for
   both the per-candidate (fix #2) and the epoch-scoped (mutation surface) views.

## Render discipline

Digest-gated columns and detail pane (structural data only; heartbeat re-tick is
a true no-op); the detail pane is cleared on a detail-kind change and caches are
invalidated only on selection change; CSS `transition`, no infinite animations;
constrained-scroll columns + transcripts (`max-height` + `overflow`); cold
deep-link hydration of the full column path + detail; no pan/zoom viewport
diagrams (the lifecycle DAG and Sankey are fit-to-width); theme-aware heatmap
(token ink at value-driven opacity); Tufte Sankey with the per-board loss value
as a mark distinct from its label.

## Tests

`test/variant_r.test.mjs` covers: the column path round-trips (cold deep-link);
the three-column cascade renders and a selection drives the next; navigating to
a second generation; the promote gate on the candidate; the patch → per-candidate
diff with real strings; all match-ups (v0 ≥ 2); the board column → per-board
detail + inline two-column transcript on run-select; trellis-in-board /
heatmap-at-epoch de-dup; the mutations + publication detail views; Sankey
label≠value; the theme-token heatmap; the two pickers; and the column + detail
digest no-ops.
