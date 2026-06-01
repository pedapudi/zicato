# Variant P — "Console III": data-model tree sidebar, convergence III

Console III is the round-5 **convergence-III** main line: the direct successor
to **Variant N** (judged the most appealing) — the same dense, data-ink-maximal
observatory — refined with a persistent, collapsible **nested-tree navigation
sidebar grounded in the data model**, a single detail pane, and every round-5
fix. It combines the praised parts of the field: N's data-ink foundation, L's
mutation viewer + first-class board view, M's generous spacing/proportion, O's
candidate-centric lifecycle + match-ups, and K's publication renderer.

Default colour theme: **monokai**. Default typeface theme: **Technical**
(Open Sans body + JetBrains Mono for data / labels / code). Compact but legible
and proportional — M's proportion sensibility applied: a comfortable 250 px
rail, a roomy detail column, generous gutters.

Self-contained under `js/variants/P/**` + `css/variants/P/**` + the entry
`app_P.js`; reuses only the shared `js/core/*` data spine and imports from no
other variant directory (everything needed from N/L/O/K is ported in). Selected
behind `?ui=P` (already wired in index.html).

## The headline — a data-model TREE sidebar

The top-tab nav is gone. A persistent LEFT TREE mirrors the real zicato
hierarchy and drives the single detail pane:

```
Environment (workspace)
└─ Epoch <id>                      (one node per epoch — MULTI-epoch nav)
   ├─ Generations
   │  └─ <gen> (champion / rejected / seed)
   ├─ Boards
   │  └─ <entry>
   ├─ Mutation surface
   └─ Publication
```

- **Expandable / collapsible.** Each branch toggles; the open set is the union
  of the route's implied open-path and the user's manual toggles, so the active
  branch is always visible and siblings can be opened without losing place.
- **Multi-epoch AND multi-generation.** Every epoch the workspace knows is a
  node; every generation and board entry is a leaf — N's gap (its epoch /
  candidate tabs could not switch which epoch / generation) is closed.
- **Selection is explicit + URL-encoded.** The hash encodes the full path
  (`#/P/e/<epoch>/gen/<gen>`, `…/board/<entry>/<gen>`, …), so a cold deep-link
  hydrates BOTH the open tree branches and the detail pane.
- **Digest-gated.** The tree repaints only when its structural model, the
  selection, or the toggle set changes; a heartbeat writes zero DOM.

Implemented in `js/variants/P/tree.js` (`buildTree`, `treeDigest`,
`routeOpenKeys`); the shell (`shell.js`) assembles the structural model from
`/api/workspace` + `/api/lineage` + `/api/epoch.board`.

## Detail views (router prefix `#/P/`, path = tree path)

| route | view — one line |
| --- | --- |
| `#/P/` | **Environment** — the workspace as a fleet (overview strip, per-epoch loss trendline cards, cross-epoch trajectory). |
| `#/P/e/<epoch>` | **Epoch overview** — objective + proposer brief, non-colliding lineage bumps, the **compact board×generation drift-loss heatmap** (stays here per fix #6). |
| `#/P/e/<epoch>/gens` | **Generations** — the candidate roster (role · parent · scalar · Δ vs champion), each row opening that candidate. |
| `#/P/e/<epoch>/gen/<gen>[/<entry>]` | **Candidate** — lifecycle DAG (clickable patch node), per-board dot-plot, entry drill, **ALL match-ups**, and the **stacked promote gate**. |
| `#/P/e/<epoch>/gen/<gen>/diff[/<mutId>]` | **Patch diff** — this candidate's side-by-side diff (baseline vs new content), reusing the mutation-viewer diff component. |
| `#/P/e/<epoch>/boards` | **Boards** — the board **trellis** (small-multiples; moved here per fix #6); cards route to the per-board view by entry id. |
| `#/P/e/<epoch>/board/<entry>[/<gen>]` | **Per-board** — one entry across every candidate (sorted dot-plot + table) with the **inline side-by-side transcript** when a candidate is selected. |
| `#/P/e/<epoch>/mutations[/<mutId>]` | **Mutation surface** — site × generation matrix + side-by-side diff in one cohesive layout. |
| `#/P/e/<epoch>/paper` | **Publication** — K's ACM renderer (GFM tables, combined table+chart, per-matchup detail), epoch-scoped. |

## The seven round-5 fixes

1. **Promote gate on the candidate page** — `views/candidate.js` renders the
   stacked, non-overlapping gate (decision header · rules ladder, each rule its
   own row · separate champion-vs-challenger scalar-components block) for the
   candidate's round(s); N lacked it.
2. **Patch node → per-candidate diff** — the lifecycle "patch" node
   (`dag.js`, `onPatch`) is clickable → `views/diff.js`, this candidate's
   SIDE-BY-SIDE diff from its own `/api/files/{epoch}/{gen}/patches` +
   baseline `/api/mutations/{epoch}/{id}` `.baseline.content` (the STRING, never
   the object), via the reused mutation-viewer diff component.
3. **ALL match-ups for a candidate** — `views/candidate.js` filters
   `/api/tournaments`.matchups where `champion==gen || challenger==gen`; v0
   shows v0→v1 AND v0→v2 (the O challenger-only bug is fixed).
4. **Board view first-class** — reachable from the tree's Boards group
   (`views/board.js`), keyed by entry id.
5. **Board entry → inline side-by-side transcript** — selecting a candidate on
   the board view shows its transcript INLINE within the same view, side by side
   with the champion's transcript on that board (`/api/conversation/{run_id}`
   per candidate); no navigation to a separate run page (the run route is gone).
6. **Trellis vs heatmap de-dup** — the compact heatmap stays at the epoch
   overview (`views/epoch.js`); the trellis moves into the Boards view
   (`views/boards.js`). Never both on one page.
7. **M's spacing + L's mutation-viewer quality** — applied throughout: roomy
   rail/detail proportions, the side-by-side line-diff component reused for both
   the epoch mutation surface and the per-candidate diff.

## The two pickers

- **Colour** — monokai (default) · solarized-dark · solarized-light, swapped via
  `[data-p-theme]` on the variant root; CSS-only re-skin, persisted
  (`zicato.P.theme`). The heatmap ramp derives from the active theme tokens.
- **Typeface** — **Sans** · **Editorial** (+ Source Serif 4) · **Technical**
  (default; + JetBrains Mono) · **Display** (+ Archivo Narrow), swapped via
  `[data-p-type]` through `--n-font-*` custom properties, persisted
  (`zicato.P.typeface`). Google Fonts are loaded in `app_P.js` with
  `display=swap` and system fallbacks — the only permitted external dependency.

## Render discipline (carried forward)

Digest-gated repaint (structural data only, heartbeat = no-op); host cleared on
selection change; one persistent host per pane; caches invalidated only on
selection change; CSS `transition`, never `animation:…infinite`;
constrained-scroll transcript tails; cold deep-link hydration of both tree and
detail; no pan/zoom viewport diagrams (fit-to-width); theme-aware heatmap; Tufte
sankey with label ≠ value; side-by-side diff with real strings.

## Tests

`test/variant_p.test.mjs` (15 tests) covers: the tree renders Environment →
Epoch → {Generations, Boards, Mutation surface, Publication}; collapse/auto-open
behaviour; multi-candidate nav; the candidate-page promote gate; the patch-node
click → per-candidate diff with real strings; v0 showing ≥2 match-ups; the board
view reachable from the tree + inline transcript (no route change); trellis in
Boards / heatmap in epoch; heatmap-cell + trellis-card routing by entry id; the
two pickers switching + persisting; and digest no-ops for the candidate view and
the tree.
