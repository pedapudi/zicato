# Variant T — "Console IV": the convergence-IV anchor

Console IV is the round-6 **convergence-IV anchor**: the direct synthesis the
operator asked for — **Variant P (Console III, judged the best-looking console)**
with three folds:

1. **S's first-class side-by-side COMPARE detail** — a "compare with…" affordance
   that splits the candidate detail into TWO candidates read side by side
   (lifecycle · promote gate · match-ups · per-board scoring, A | B), with
   champion-vs-challenger transcripts side by side on a board.
2. **Q's generous, proportional spacing** — a roomier rail, calmer detail column,
   more air between sections and inside panels.
3. **A working back/up button** (top-left) — the explicit fix over Q's buggy one.

Default colour theme: **monokai**. Default typeface theme: **Technical**
(Open Sans body + JetBrains Mono for data / labels / code). Miller columns (R)
are back-burnered and not pursued here.

Self-contained under `js/variants/T/**` + `css/variants/T/console4.css` + the
entry `app_T.js`; reuses only the shared `js/core/*` data spine and imports from
no other variant directory (everything needed from P/S/Q is ported in). Selected
behind `?ui=T` (already wired in index.html).

## The headline (carried from P) — a data-model TREE sidebar

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

Expandable / collapsible; multi-epoch AND multi-generation; selection explicit +
URL-encoded so a cold deep-link hydrates BOTH the open branches and the detail.
Implemented in `js/variants/T/tree.js`; the shell (`shell.js`) assembles the
structural model from `/api/workspace` + `/api/lineage` + `/api/epoch.board`.

## Detail views (router prefix `#/T/`, path = tree path)

| route | view — one line |
| --- | --- |
| `#/T/` | **Environment** — the workspace as a fleet (overview strip, per-epoch loss trendline cards, cross-epoch trajectory). |
| `#/T/e/<epoch>` | **Epoch overview** — objective + proposer brief, non-colliding lineage bumps, the **compact board×generation drift-loss heatmap** (stays here per fix #6). |
| `#/T/e/<epoch>/gens` | **Generations** — the candidate roster (role · parent · scalar · Δ vs champion), each row opening that candidate. |
| `#/T/e/<epoch>/gen/<gen>[/<entry>]` | **Candidate** — lifecycle DAG (clickable patch node), per-board dot-plot, entry drill, **ALL match-ups**, and the **stacked promote gate**. |
| `#/T/e/<epoch>/gen/<gen>~cmp=<gen2>` | **Candidate · COMPARE** — the SAME pane split into TWO candidate panels A \| B (S's comparison-first detail). |
| `#/T/e/<epoch>/gen/<gen>/diff[/<mutId>]` | **Patch diff** — this candidate's side-by-side diff (baseline vs new content), reusing the mutation-viewer diff component. |
| `#/T/e/<epoch>/boards` | **Boards** — the board **trellis** (small-multiples; here per fix #6); cards route to the per-board view by entry id. |
| `#/T/e/<epoch>/board/<entry>[/<gen>]` | **Per-board** — one entry across every candidate (sorted dot-plot + table) with the **inline side-by-side transcript** when a candidate is selected. |
| `#/T/e/<epoch>/mutations[/<mutId>]` | **Mutation surface** — site × generation matrix + side-by-side diff in one cohesive layout. |
| `#/T/e/<epoch>/paper` | **Publication** — K's ACM renderer (GFM tables, combined table+chart, per-matchup detail), epoch-scoped. |

## The compare model (NEW — fold 1, from S)

The candidate detail is **comparison-first**. By default it reads ONE candidate.
A **"compare with…"** picker (`js/variants/T/compare.js`, `comparePicker`) sets a
`~cmp=<gen>` suffix on the hash (so the comparison **deep-links**); `splitFrame`
then renders TWO candidate panels side by side, each in its **own digest-gated
host** so one side changing never rebuilds the other. Each side carries the full
lifecycle DAG, the per-board scoring dot-plot, ALL match-ups, and the stacked
promote gate. Clicking any match-up row sets the other candidate as the compare
target. Champion-vs-challenger transcripts read side by side INLINE on the board
view (`views/board.js`). The split collapses to a single column when there is no
compare target.

## The back-button fix (NEW — fold 3)

A top-left **back/up control** (`shell.js`, `goBack` / `renderBack`, plus
`router.up`) navigates UP the selection hierarchy:
candidate → generations → epoch → environment; a COMPARE split collapses to the
bare candidate first; an entry/transcript selection steps up to its bare parent
first. **The bug to avoid** (Q's): Q rendered the destination into the SIDE
PANEL. T instead **navigates** (changes the route); the normal dispatch then
repaints the destination into the **MAIN DETAIL PANE** — the rail/tree host is
never touched. The button is inert at the environment root. Tested explicitly:
after a back action the rail host still holds the tree and the detail host holds
the destination view.

## The seven round-5 fixes (carried forward — not regressed)

1. **Promote gate on the candidate page** — `views/candidate.js` renders the
   stacked, non-overlapping gate (decision header · rules ladder, each rule its
   own row · separate champion-vs-challenger scalar-components block); present on
   BOTH sides of a compare split.
2. **Patch node → per-candidate diff** — the lifecycle "patch" node is clickable
   → `views/diff.js`, this candidate's SIDE-BY-SIDE diff from its own
   `/api/files/{epoch}/{gen}/patches` + baseline `/api/mutations/{epoch}/{id}`
   `.baseline.content` (the STRING, never the object).
3. **ALL match-ups for a candidate** — filters `/api/tournaments`.matchups where
   `champion==gen || challenger==gen`; v0 shows v0→v1 AND v0→v2.
4. **Board view first-class** — reachable from the tree's Boards group
   (`views/board.js`), keyed by entry id.
5. **Board entry → inline side-by-side transcript** — selecting a candidate on
   the board view shows its transcript INLINE, side by side with the champion's
   on that board (`/api/conversation/{run_id}` per candidate); no run page.
6. **Trellis vs heatmap de-dup** — heatmap stays at the epoch overview
   (`views/epoch.js`); the trellis lives in the Boards view (`views/boards.js`).
   Never both on one page.
7. **Q/M spacing + L's mutation-viewer quality** — applied throughout (fold 2).

## The two pickers

- **Colour** — monokai (default) · solarized-dark · solarized-light, swapped via
  `[data-t-theme]` on the variant root; CSS-only re-skin, persisted
  (`zicato.T.theme`). The heatmap ramp derives from the active theme tokens.
- **Typeface** — **Sans** · **Editorial** (+ Source Serif 4) · **Technical**
  (default; + JetBrains Mono) · **Display** (+ Archivo Narrow), swapped via
  `[data-t-type]`, persisted (`zicato.T.typeface`). Google Fonts loaded in
  `app_T.js` with `display=swap` and system fallbacks — the only external dep.

## Render discipline (carried forward)

Digest-gated repaint (structural data only, heartbeat = no-op; each compare side
independently gated); host cleared on selection change — and a `~cmp` change is
part of the selection; one persistent host per pane; caches invalidated only on
selection change; CSS `transition`, never `animation:…infinite`;
constrained-scroll transcripts; cold deep-link hydration of tree + detail +
compare target; no pan/zoom viewport diagrams (fit-to-width); theme-aware
heatmap; Tufte sankey with label ≠ value; side-by-side diff with real strings.

## Tests

`test/variant_t.test.mjs` (17 tests) covers: the tree renders Environment →
Epoch → {Generations, Boards, Mutation surface, Publication}; multi-generation
nav; the candidate-page promote gate; the patch-node click → per-candidate diff
with real strings; v0 showing ≥2 match-ups; the board view reachable from the
tree + inline side-by-side transcript on run select; the **side-by-side COMPARE
splitting the detail into two candidates**; the **back button navigating UP and
rendering the destination into the MAIN detail pane while the rail host stays the
tree**; trellis in Boards / heatmap in epoch; the two pickers + the compare
primitives; and digest no-ops for the candidate view and the tree.
