# Variant U — "Atlas V" (convergence-IV, the comfortable sibling)

Round-6 convergence-IV dashboard. The **comfortable, airy sibling** of the
anchor (Variant T "Console IV"): the *same* P + S + Q synthesis — a data-model
**tree sidebar**, a detail pane built around **first-class side-by-side
comparison**, every Round-5 fix, and a **working back button** — but rendered
**roomy and calm**. Same content + structure; different density + aesthetic.

- **Colour:** Solarized-Light by default (also Solarized-Dark, Monokai).
- **Typeface:** Sans by default (also Editorial, Technical, Display).
- **Spacing:** Q/M-forward — generous panel padding, taller line-heights, wider
  gutters, bigger radii and type than the dense console. A breathable
  alternative to T.

Reach it with `?ui=U` (already wired in `index.html`). Everything is scoped
under `#variant-root[data-variant="U"]`; nothing leaks.

## Lineage

`P (Console III)` anchor (dense data-ink + tree sidebar) + `S (Lens)`
comparison-first detail + `Q (Atlas IV)` roomy spacing. U is the light/roomy
take on the same synthesis as T (the Monokai/Technical dense anchor).

## IA — a persistent data-model tree drives one detail pane

The left **tree sidebar** mirrors the real hierarchy and is the navigation:

```
Environment (workspace)
└─ Epoch <id>                 (every epoch is its own selectable node)
   ├─ Generations
   │  └─ <gen> (champion ♛ / rejected / seed)
   ├─ Boards
   │  └─ <entry>
   ├─ Mutation surface
   └─ Publication
```

Expandable/collapsible (state persists in `localStorage`); selecting any node
drives the detail pane. It navigates **multiple epochs AND multiple
generations**. Selection is URL-encoded, so a cold deep-link rehydrates both the
tree (expanded to the selection) and the detail pane (including a split
comparison). The hash scheme (`js/variants/U/router.js`):

```
#/U/                                    environment (all-epochs-first)
#/U/e/<epoch>                           epoch overview (heatmap)
#/U/e/<epoch>/gen/<gen>                 candidate (lifecycle · gate · matchups)
#/U/e/<epoch>/gen/<gen>/patch           candidate, patch diff opened
#/U/e/<epoch>/gen/<gen>/entry/<entry>   candidate, one board entry drilled
#/U/e/<epoch>/board/<entry>             per-board cross-candidate view
#/U/e/<epoch>/mut[/<mutId>]             mutation surface (+ side-by-side diff)
#/U/e/<epoch>/pub                       epoch publication (ACM)
~cmp=<gen>     suffix: the SECOND candidate for a split comparison
~runs=<a>,<b>  suffix: the two candidates whose transcripts show side by side
```

## Views (one line each)

- **Environment** (`views/env.js`) — all-epochs-first card grid; each card →
  that epoch's overview.
- **Epoch** (`views/epoch.js`) — objective + proposer brief, a non-colliding
  clickable lineage **bumps** chart, and the board × generation drift-loss
  **heatmap** (heatmap lives HERE only; cells route to the per-board view by
  entry id — fix #6).
- **Candidate** (`views/candidate.js`) — the signature comparison screen. One
  candidate's **lifecycle DAG** (the PATCH node → its side-by-side diff, fix
  #2), the **stacked promote gate** on the candidate page (decision + Δs · the
  rules ladder · a separate champion-vs-challenger scalar-components block, fix
  #1), **ALL** of the candidate's match-ups (champion OR challenger; v0 shows
  ≥2, fix #3), and the per-board scoring dot-plot. A **"compare with…"** picker
  splits the pane into two candidates, A | B, each digest-gated independently.
- **Board** (`views/board.js`) — first-class per-board cross-candidate view
  (fix #4): a sorted comparative dot-plot, the board **trellis** small-multiple
  (trellis lives HERE only — fix #6), a tabular breakdown, and the **signature
  INLINE side-by-side transcripts** — selecting runs sets the `runs` target and
  fills two independently-scrollable transcript columns within the same view,
  never a separate page (fix #5).
- **Mutation surface** (`views/mutations.js`) — one cohesive visual: the
  site × generation matrix + a detail pane that fills on select with the
  line-diffed **side-by-side** diff (REAL strings via `.baseline.content` —
  never the baseline object).
- **Publication** (`views/publication.js`) — K's epoch-scoped ACM renderer:
  eyebrow / title / meta / abstract / body, GFM tables, live Tufte figures
  spliced at `<!-- FIGURE -->` markers, the aggregate-scores table + bar chart
  combined into one figure, per-matchup detail appended.

## Comparison model (Round-6 NEW #1 — from S)

`js/variants/U/compare.js` provides `comparePicker` (sets/clears the `cmp` or
`runs` route target, URL-encoded — never navigates away) and `splitFrame` (a
two-column A | B frame; each side paints into its own host so its digest gate
fires independently). The candidate view uses it to put two candidates'
lifecycle / gate / match-ups / per-board scoring side by side; the board view
uses it to put two candidates' transcripts side by side INLINE.

## Back / up control (Round-6 NEW #3 — Q's bug, FIXED)

A fixed **back/up** button sits top-left in the chrome. `router.parentRoute()`
computes the destination one step UP the selection hierarchy (a drilled
patch/entry, an active comparison, or a board run-pair steps up WITHIN the
candidate/board first; then candidate → epoch → environment). The shell's
`goBack()` simply `navigate()`s there, which flows through the standard dispatch
and renders the destination into the **MAIN detail pane** — it NEVER paints into
the sidebar (Q's bug). The tree rail is only ever painted by `paintTree()`; the
back action does not touch it. A unit test asserts that after a back action the
rail still holds the data-model tree (no detail view, no heatmap, no page-head
leaks in) and the detail host holds the destination.

## Carried-forward Round-5 capabilities + fixes (unchanged)

Data-model tree sidebar · multi-epoch + multi-gen nav · promote gate on the
candidate page (stacked, fix #1) · patch node → per-candidate side-by-side diff
with real strings (fix #2) · ALL match-ups for a candidate (fix #3) ·
first-class board view (fix #4) · board run → INLINE side-by-side transcript
(fix #5) · trellis in Boards / heatmap at epoch overview, de-duped (fix #6) ·
colour picker (3 themes) + typeface picker (Open-Sans Google-Fonts pairings) ·
digest-gating · NO pan/zoom viewport (fit-to-width) · theme-aware heatmap ·
Tufte sankey label ≠ value · K's publication epoch-scoped.

## Render discipline

Every pane is digest-gated (structural-only digest; a heartbeat tick writes
zero DOM). The detail host clears only on a selection change; caches invalidate
only on selection change. The tree and detail are persistent hosts. Transitions
are CSS `transition` (theme swap, hovers) — never an infinite animation.
Transcripts are constrained-scroll. Cold deep-links hydrate. No pan/zoom
viewport anywhere — every mark fits its container.

## Constraints

Vanilla ESM, no build, no external libraries except **Google Fonts** (fonts
only — loaded in `app_U.js`, which lives at the static root and is NOT part of
the bundle fixture, with system fallbacks + `font-display: swap`). Reuses
`js/core/*`. Self-contained within `js/variants/U/` — U carries its own copies
of the rendering primitives (`svg.js`, `dag.js`), trimmed to only the marks U
actually renders (the unused sankey / bracket / round-robin / race / sparkline
marks are dropped to keep the variant lean). CSS lives in
`css/variants/U/atlasv.css`, scoped under the variant root.

## Files

```
app_U.js                          entry: fonts + stylesheet + mountShell
js/variants/U/shell.js            topbar (FIXED back button) + tree + one detail host
js/variants/U/router.js           hash router + parentRoute (the back/up target)
js/variants/U/tree.js             the data-model tree sidebar
js/variants/U/model.js            shared hierarchy resolver (tree + detail agree)
js/variants/U/data.js             cached, failure-tolerant drill-down reads
js/variants/U/ui.js               gatedSwap · themes · pills · GFM markdown
js/variants/U/compare.js          comparePicker + splitFrame (the signature)
js/variants/U/svg.js              SVG marks (bumps · heatmap · dot-plot · diff · slopegraph)
js/variants/U/dag.js              the candidate-lifecycle DAG
js/variants/U/views/{env,epoch,candidate,board,mutations,publication}.js
css/variants/U/atlasv.css         scoped, roomy, three colour + four typeface themes
test/variant_u.test.mjs           unit tests (tree · gate · diff · matchups · board · compare · back button · trellis · pickers · digest no-op)
```

## Tests

`node test/variant_u.test.mjs` — 15 cases, all green; included in
`node test/run-all.mjs` (0 failures across the JS suite). Covers: the tree
sidebar + multi-gen nav; `parentRoute` walking up the hierarchy; the stacked
promote gate on the candidate page; the patch → per-candidate side-by-side diff
(real strings); ALL match-ups (v0 ≥ 2); the first-class board view + INLINE
side-by-side transcript; the candidate compare split; **the FIXED back button
rendering into the MAIN pane with the rail unchanged**; the trellis-in-board /
heatmap-at-epoch de-dup; the pickers + pills (default Sans + Solarized-Light);
the digest no-op; cold deep-link hydration.
