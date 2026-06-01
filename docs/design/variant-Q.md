# Variant Q — "Atlas IV": roomy convergence-III on a data-model tree

Atlas IV is the round-5 **convergence-III** dashboard, the *comfortable* take.
It keeps Variant N's content + diagrams (judged the most appealing base) but
dresses them in Variant M's **generous spacing/proportion and typographic
comfort** (singled out as good), adopts Variant L's **mutation-viewer quality**
throughout, and replaces N's top-tab nav with the round-5 headline: a persistent
**data-model TREE sidebar**.

Default colour theme: **Solarized-Dark**. Default typeface theme: **Sans**
(Open Sans throughout, tabular figures for data). The roominess is deliberate —
larger panel padding, taller line-heights, wider gaps, and bigger type than the
dense N — comfort over density, every figure proportional.

Self-contained under `js/variants/Q/**` + `css/variants/Q/**` + the entry
`app_Q.js`; reuses only the shared `js/core/*` data spine and imports from no
other variant directory. Selected behind `?ui=Q` (already wired in index.html).
The only external dependency is Google Fonts (fonts only, `display=swap`, with
system fallbacks).

## Headline — the data-model tree sidebar

A persistent left tree mirrors the real hierarchy and *is* the navigation:

```
Environment (workspace)
└─ Epoch <id>
   ├─ Generations
   │  └─ <gen>  (♛ champion / rejected)
   ├─ Boards
   │  └─ <entry>
   ├─ Mutation surface
   └─ Publication
```

Each node is expandable/collapsible; selecting any node drives the single,
well-spaced detail pane. Selection is **explicit and URL-encoded** — the routes
carry the epoch id (and, where it matters, the generation), so the tree
navigates **multiple epochs AND multiple generations** (N's gap was that its
tabs could not switch which epoch/candidate was shown). A cold deep-link
hydrates both the tree (the selection's ancestors are force-expanded) and the
detail pane. Only one epoch exists in the live data — the tree degrades
gracefully but is structured all-epochs-first. Expansion state persists in
`localStorage`; the sidebar itself collapses via a top-bar toggle.

## Screens (router prefix `#/Q/`)

| route | detail pane |
| --- | --- |
| `#/Q/` | **Environment** — the workspace as a fleet (overview strip, per-epoch loss trendline cards, cross-epoch trajectory). |
| `#/Q/epoch/<id>` | **Epoch overview** — objective + proposer brief, non-colliding lineage bumps, theme-aware drift **heatmap** (the trellis is *not* here — fix #6). |
| `#/Q/gen/<id>/<gen>[/<entry>]` | **Candidate** — lifecycle DAG (clickable PATCH node), per-board dot-plot, the **promote gate**, and **all match-ups** for this candidate; URL-driven entry drill. |
| `#/Q/matchups/<id>/<gen>` | **Match-ups** — every round this candidate appeared in: Tufte sankey, gauntlet bumps, paired slopegraphs, the stacked gate, illustrative alternatives. |
| `#/Q/board/<id>[/<entry>][/<cmp>]` | **Boards** — first-class: the board **trellis** (fix #6); selecting an entry shows the cross-candidate dot-plot/table **and** an INLINE side-by-side transcript (fix #5). |
| `#/Q/mutations/<id>[/<gen>/<mutId>]` | **Mutation surface** — site × generation matrix + side-by-side diff in one cohesive layout; `?gen` focuses the per-candidate diff. |
| `#/Q/publication/<id>` | **Publication** — K's ACM renderer (GFM tables, combined table+chart, per-matchup detail), epoch-scoped. |
| `#/Q/run/<id>/<gen>/<entry>` | **Run** — the reconstructed transcript (cold-deep-link safe). |

## The seven mandatory fixes

1. **Promote gate on the candidate page.** Every non-seed candidate detail
   mounts the stacked, non-overlapping gate (decision header · rules ladder, one
   rule per row · a separate champion-vs-challenger scalar-components block).
   Shared `gatePanel()` (exported from `views/matchups.js`) so the candidate and
   match-ups pages render the identical layout.
2. **Patch node → per-candidate side-by-side diff.** The lifecycle DAG's PATCH
   node is clickable → `#/Q/mutations/<id>/<gen>`, which auto-pins that
   candidate's first patched site and fills the side-by-side diff from its own
   `/api/files/{epoch}/{gen}/patches` (`.new_content`) against the baseline
   `/api/mutations/{epoch}/{id}` `.baseline.content` (the STRING — never the
   object, so no "[object Object]"). Reuses L/N's `svg.sideBySideDiff`.
3. **All match-ups for a candidate.** `data.matchupsForGen` filters
   `/api/tournaments`.matchups for `champion == gen || challenger == gen`, so v0
   shows v0→v1 AND v0→v2; the candidate page lists every round, and the
   match-ups view renders the sankey/slopegraph/gate for each.
4. **Board view first-class.** The Boards group in the tree opens the Boards
   view; its entries open the per-board cross-candidate detail.
5. **Board entry → INLINE side-by-side transcript.** Selecting a board entry
   loads two candidates' transcripts (champion left, the worst-scoring
   challenger right by default; a row click swaps the right column) **within**
   the board view via `/api/conversation/{run_id}` — no navigation away.
6. **Trellis vs heatmap de-dup.** The compact board×generation drift-loss
   **heatmap stays at the epoch overview**; the board **trellis (small
   multiples) lives in the Boards view**. Never both on one page.
7. **M's spacing + L's mutation viewer throughout.** Roomy panel padding,
   line-heights, gaps, and type; the L/N side-by-side mutation diff with real
   strings.

## Carried-forward discipline

Digest-gated rendering (a `data-q-digest` no-op writes zero DOM); host cleared
only on a selection change; per-selection caches invalidate only on selection
change; CSS `transition` (theme/typeface swap, hovers), never an infinite
animation; constrained, scrollable transcripts; cold deep-link hydration;
fit-to-width diagrams (NO pan/zoom viewport); theme-aware heatmap (ink at
value-driven opacity, legible in all three themes); Tufte fit-to-width sankey
with the per-board loss VALUE on its own right-aligned baseline (label ≠ value);
clickable, non-colliding lineage bumps. Three colour themes + four typeface
themes, both persisted.

## Tests

`test/variant_q.test.mjs` (run with `node test/variant_q.test.mjs`) covers: the
tree renders the full hierarchy + multi-generation navigation; the promote gate
on the candidate page; the PATCH node → per-candidate diff with real strings;
all match-ups (v0 → 2); the Boards view first-class with an inline side-by-side
transcript (and no nav-away); the trellis in the Boards view and NOT the epoch
overview; the colour + typeface pickers + verdict pills; and a digest-gated
no-op repaint.
