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

Round 7 evolves the anchor by adopting two well-liked elements **elegantly**, plus
a new chrome control:

4. **The SLIM REEL on the epoch view** (adopted from Variant V) — a compact,
   fit-to-width "rounds" spine that REPLACES the old lineage-bumps chart.
5. **Compact MATCH CARDS on the generations page** (adopted from Variant W) — a
   champion-defends banner + a responsive wrapping grid of one card per
   challenger round.
6. **A density / "roominess" picker** — a THIRD chrome selector
   (compact · cozy · roomy) beside the colour-theme and typeface pickers.

Round 9 makes the console **scale to the operator's screen**:

7. **A PAGE-WIDE SCALE pill** — a draggable, keyboard-accessible slider in the
   chrome (≈70 %–150 %, 5 % steps, default 100 %) that scales the ENTIRE page —
   text AND diagrams — and reflows (no clipping). Distinct from density.
8. **A FLUID, resolution-responsive layout** — the detail pane and the
   side-by-side compare grid fill the available viewport width instead of being
   clamped to a narrow centred column, so the compare panes (and their SVGs)
   render as large as the screen allows: bigger diagrams on bigger monitors.

Default colour theme: **monokai**. Default typeface theme: **Technical**
(Open Sans body + JetBrains Mono for data / labels / code). Default density:
**compact** (the dense Console look). Default page scale: **100 %**. Miller
columns (R) are back-burnered and not pursued here.

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
| `#/T/e/<epoch>` | **Epoch overview** — objective + proposer brief, the **slim REEL** (rounds along the champion spine; replaces the old bumps), the **compact board×generation drift-loss heatmap** (stays here per fix #6). |
| `#/T/e/<epoch>/gens` | **Generations** — the **champion-defends banner** + a wrapping grid of compact **MATCH CARDS** (one per challenger round), plus the dense candidate roster (role · parent · scalar · Δ vs champion). Cards + rows open that candidate. |
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

## The slim reel on the epoch view (fold 4, adopted from V)

`js/variants/T/reel.js` (ported IN — no cross-variant import) renders a compact,
**fit-to-width** champion spine: station 0 is the seed/champion (♛), and each
round is a small **tick** on the spine carrying its ordinal (`r1…rN`), a
verdict-coloured dot, and the challenger id. The rounds come from
`/api/tournaments`.matchups (round-ordered by `ran_at`; lineage fallback). It
**replaces the old lineage-bumps** chart on the epoch view — the same
champion-vs-challenger-over-rounds story, so only ONE appears; the heatmap stays.

The big per-challenger cards are deliberately NOT hung off the reel (that does
not scale); the per-challenger detail lives in the generations match cards.

**Scaling to many generations.** The SVG has a FIXED viewBox (`0 0 1000 92`) laid
out left→right and is set to `width:100%` (NO pan/zoom, no horizontal scroll).
Stations are evenly distributed between `x0` and `xMax`, so as rounds grow the
step shrinks and the ticks **compress** — no element ever exceeds the viewBox
width. The selected/hovered tick highlights via a CSS state-class swap (never an
infinite keyframe). The reel's vertical scale follows the density picker
(`--dt-reel-scale`) while its width stays fit-to-container.

## The match cards on the generations page (fold 5, adopted from W)

`views/gens.js` leads with a **champion-defends banner** (champion id · loss · N
title defences · promoted badge) and a **responsive wrapping grid** of compact
challenger match cards — `grid-template-columns: repeat(auto-fill,
minmax(--dt-card-min, 1fr))`, one card per challenger round. Each card:
`<challenger> vs <champion>` · verdict pill · Δscalar · a **one-line (truncated)**
hypothesis · the decisive-driver judge (from the round gate) · a status link
(dead-branch / promoted → opens the candidate). Clicking a card opens that
candidate. The dense roster table is retained below the cards for the
at-a-glance scan.

**Scaling to many generations.** Cards stay short (the full hypothesis lives on
the candidate page; the one-line idea truncates with ellipsis), and the grid
wraps to multiple rows, so it stays tidy whether there are 3 OR ~30 generations.
These cards appear on the **generations** scope only — never on the
environment / workspace view.

## The chrome controls (three pickers + the scale pill)

- **Colour** — monokai (default) · solarized-dark · solarized-light, swapped via
  `[data-t-theme]` on the variant root; CSS-only re-skin, persisted
  (`zicato.T.theme`). The heatmap ramp derives from the active theme tokens.
- **Typeface** — **Sans** · **Editorial** (+ Source Serif 4) · **Technical**
  (default; + JetBrains Mono) · **Display** (+ Archivo Narrow), swapped via
  `[data-t-type]`, persisted (`zicato.T.typeface`). Google Fonts loaded in
  `app_T.js` with `display=swap` and system fallbacks — the only external dep.
- **Density / "roominess"** (fold 6) — **compact** (default; the dense Console
  look) · **cozy** · **roomy** (Atlas-like air). A third chrome selector beside
  the others; a root `[data-t-density]` attribute drives the spacing/size custom
  properties (`--dt-rail` rail width, `--dt-pad-x/-y` detail padding,
  `--dt-section-gap`, `--dt-panel-pad-*`, `--dt-row-gap`, `--dt-card-min` /
  `--dt-card-gap` / `--dt-card-pad`, `--dt-reel-scale`, and a global
  `--dt-font-scale`), so the WHOLE UI — reel, match cards, tables, gate, tree —
  re-breathes with a pure CSS swap (no re-render). Persisted (`zicato.T.density`);
  the active value is reflected on the pills.

  **Density also scales the VISUAL-ELEMENT size** (round 8), not only the
  whitespace around them. The SVG figures are laid out in JS, so a parallel
  size-token table lives in `ui.js` (`densityTokens(density)` → `{ sizeScale,
  fontScale, nodeRadius, dagRowStep, heatCell, dotRow, sparkbarH, reelScale }`),
  keyed by the same density id. Each view reads it at render time and feeds the
  figure's INTRINSIC dimensions: the lifecycle-DAG height + row step, the heatmap
  cell size, the per-board / candidate / board-view dot-plot row height, the
  trellis sparkbar height, and the per-judge value-bar row height all grow
  compact → roomy (and shrink in compact). The reel + match cards keep their
  existing CSS scaling (`--dt-reel-scale` / `--dt-card-min`). Width is never
  touched — every figure stays fit-to-width at every density (see below).

## The page-wide SCALE pill (round 9) — and how it differs from density

A fourth chrome control, a **draggable range slider** (`.dt-scale-range` inside a
`.dt-scale-pill`, beside the colour / typeface / density pickers) with a small
**% readout**. It is a continuous control over **≈70 %–150 %** in **5 % steps**,
**default 100 %**, and is **keyboard-accessible** (a native range input: arrow
keys step ±5; it carries `aria-valuemin/max/now` + an `aria-label`).

- **Mechanism.** `shell.applyScale(n, root)` (mirrors `applyTheme` / `applyDensity`)
  normalises `n` (clamp to range + snap to the 5 % grid), then applies it
  **page-wide** by setting **`zoom`** on the Variant-T app **ROOT**
  (`#variant-root[data-variant="T"]`) — e.g. 130 % → `root.style.zoom = 1.3`. It
  also stamps the raw ratio as `--dt-page-scale` and records `data-t-scale="130"`
  on the root, and updates the slider + the % readout. `zoom` **reflows** (it is
  not a CSS transform), so the page re-wraps at the scaled size and never clips.
- **Page-wide, NOT per-pane.** The scale is applied at the app root only — there
  is deliberately **no** per-pane zoom control. In the side-by-side compare view
  the two panes scale together with the rest of the page.
- **Persistence.** `readScale` / `persistScale` use their own key
  (`zicato.T.scale`); the value is restored and re-applied on every mount.
- **Scale vs density — distinct axes that COMPOSE.** Labels + tooltips spell out
  the roles so the two are not confused: **density** = the *spacing rhythm /
  proportion* (compact · cozy · roomy — how the layout breathes, via the `--dt-*`
  tokens and the JS size tokens); the **scale pill** = the *overall page size*
  (one master `zoom` multiplier on top of whatever density layout is in effect).
  They persist under separate keys and never reset one another — changing density
  leaves the scale untouched and vice-versa, so the page scale simply multiplies
  the chosen density layout.

## Fluid, resolution-responsive layout (round 9)

The content now **fills the available viewport width** instead of sitting in a
narrow centred column that wasted space on wide monitors. The detail pane
(`.dt-viewhost`) and the legacy `.dn-viewhost` were clamped to **1160 px / 1320 px
centred**; both are now **`width:100%`** with only a very generous, *non-centred*
cap (`max-width: min(100%, 2400px)` / `min(100%, 2200px)`) that merely guards
prose line-length on ultra-wide displays. Because the detail pane fills its
column, the side-by-side compare grid (`.dt-split`, a `1fr 1fr` grid that only
collapses to one column below 1080 px) splits the **FULL content width** — so
each pane, and every fit-to-width SVG inside it, renders **as large as the screen
allows**. Net effect: bigger diagrams on bigger screens, still tidy on small ones.
This composes with the scale pill (the whole fluid layout is then `zoom`-scaled)
and with density (which still drives the padding rhythm).

## Fit-to-width — visual elements never escape their pane (round 8)

Every visual element is a **responsive SVG** (`width: 100%` + a `viewBox` +
`preserveAspectRatio`, no fixed pixel width that exceeds the panel and no
horizontal-scroll wrapper around the whole figure). This now holds for the
**lifecycle DAG** (`dag.js` — was a fixed 900 px SVG inside an `overflow-x:auto`
panel; the right-hand stages spilled out and forced sideways scrolling — now the
viewBox-internal coordinate width scales down to fit the pane, so all six stages
parent → patch → board → Σ → gate → terminal are visible without scrolling), the
**Tufte sankey**, the **heatmap** (epoch overview — the `overflow-x:auto` panel
wrapper is gone), the **bumps**, the per-board / candidate / board-view
**dot-plots** (`valueDotPlot`), the per-judge **value-bars**, the matchup
**slopegraph**, and the trellis **sparkbar / gen-dots**. Inherently-wide tabular
content carries its OWN contained overflow — the **publication** GFM tables, the
aggregate-scores table, the per-matchup-detail tables, and the **mutation
matrix** are each wrapped in a `.dn-table-scroll` box (`max-width:100%;
overflow-x:auto` on the wrapper only), so a wide table scrolls WITHIN its box and
never pushes the paper/panel layout sideways. As a backstop, `.dn-panel` itself
is `max-width:100%; overflow-x:hidden` so no element can visually escape a panel.

## Render discipline (carried forward)

Digest-gated repaint (structural data only, heartbeat = no-op; each compare side
independently gated); host cleared on selection change — and a `~cmp` change is
part of the selection; one persistent host per pane; caches invalidated only on
selection change; CSS `transition`, never `animation:…infinite`;
constrained-scroll transcripts; cold deep-link hydration of tree + detail +
compare target; no pan/zoom viewport diagrams (fit-to-width); theme-aware
heatmap; Tufte sankey with label ≠ value; side-by-side diff with real strings.

## Tests

`test/variant_t.test.mjs` (34 tests) covers: the tree renders Environment →
Epoch → {Generations, Boards, Mutation surface, Publication}; multi-generation
nav; the candidate-page promote gate; the patch-node click → per-candidate diff
with real strings; v0 showing ≥2 match-ups; the board view reachable from the
tree + inline side-by-side transcript on run select; the **side-by-side COMPARE
splitting the detail into two candidates**; the **back button navigating UP and
rendering the destination into the MAIN detail pane while the rail host stays the
tree**; trellis in Boards / heatmap in epoch; the colour + typeface pickers + the
compare primitives; and digest no-ops for the candidate view and the tree. Round
7 adds: the epoch view renders the **slim reel** (spine + ticks) and NOT the old
bumps; the reel stays **fit-to-width** (fixed `0 0 1000 92` viewBox, ticks
compress, nothing exceeds the width) under a **~12-generation / 11-round**
fixture; the generations page renders the **champion-defends banner + one match
card per challenger** in a wrapping grid; match cards do NOT render on the
environment view; and the **density picker** switches compact↔roomy (root
attribute + token) and persists. Round 8 adds: the **lifecycle DAG and sankey
render as fit-to-width responsive SVG** (`width:100%` + viewBox) with NO
horizontal-scroll wrapper around the figure; the epoch **heatmap** is responsive
and its panel does not scroll horizontally; the **publication** view's wide
tables each sit in a contained `.dn-table-scroll` box (the panel itself never
scrolls) and its live figures are `width:100%`; and **density scales a diagram
SIZE token** (not only spacing) — `densityTokens('compact')` vs `'roomy'` differ
on every intrinsic size token, and the rendered DAG height grows compact → roomy
while BOTH stay `width:100%` (fit-to-width holds at every density). Round 9 adds:
`ui` exposes a **70–150 % / 5 %-step / 100 %-default** scale surface that snaps +
clamps; the **draggable scale pill exists in the chrome** and setting it applies a
**page-wide** scale at the app **ROOT** (`data-t-scale` + `root.style.zoom` change,
NOT on any pane) and **persists + restores** (a fresh mount re-applies it); the
pill is a **keyboard-accessible** native range with aria bounds; the page scale
**composes with density** (switching density does not reset the scale, and the two
persist independently); and the **layout is fluid** — the old narrow 1160 px /
1320 px caps are gone, the detail pane is `width:100%`, and the compare split is a
`1fr 1fr` grid so its two panes share the FULL content width.
