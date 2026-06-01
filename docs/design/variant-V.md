# Variant V — "Reel"

Convergence-IV (round 6), the **creative-temporal** take. The epoch is a
horizontal **REEL**: a timeline / playback of the rounds. Built on the **Console
III (P) anchor** — the data-model TREE sidebar, dense data-ink marks, the full
round-5 fix set — with **S "Lens"'s side-by-side compare** folded into the
candidate detail and a **fixed back/up button**. Default colour theme
**Solarized-Dark**; default typeface **Display** (Archivo Narrow).

`?ui=V` is wired in `index.html` (untouched); `app_V.js` boots the variant.

## The reel model (the hero AND the navigator)

A single fit-to-width SVG film strip (`js/variants/V/reel.js`):

- the **champion spine** runs along the top time axis;
- a **station 0** marks the reigning champion / seed (♛);
- **one station per round** — the challengers *entering over time*, ordered by
  `ran_at` from `/api/tournaments` (lineage / champion from `/api/lineage`).
  Each station card carries the **verdict** (promoted ♛ / rejected ✕) and
  **Δscalar**, colour-coded; the connector "entering" the spine is good/bad
  tinted;
- a **scrubber / stepper** under the strip: prev/next steppers + a pip per
  station. Stepping or clicking a pip moves along the rounds.

Selecting any station (or pip) **navigates to that round's challenger** — the
candidate view, which renders that round's **match-up + promote gate +
lifecycle**. The reel is thus the hero *and* doubles as navigation; the tree
sidebar stays (collapsible) for full-fidelity nav to any
epoch/generation/board/mutation/publication.

The reel lays out **fit-to-width** — a fixed `viewBox` SVG at `width:100%`, **no
pan/zoom viewport**. It is **digest-gated on structure only** (round ids /
verdicts / Δ / selection — not `ran_at` timestamps): a steady heartbeat writes
zero DOM. The scrub is a CSS `transition` state change (`.vr-sel`), never a
re-fired infinite keyframe.

## Detail surfaces (one line each)

- **Reel station → candidate** (`views/candidate.js`) — the round's challenger:
  lifecycle DAG, per-board scoring dot-plot, the stacked promote gate, and ALL
  its match-ups. Plus S's **compare**: a "compare with…" picker splits the SAME
  pane into two candidates side by side via the `~cmp=<gen>` hash suffix
  (deep-linkable); clicking a match-up row compares the two.
- **Tree sidebar** (`tree.js`) — Environment → Epoch → {Generations, Boards,
  Mutation surface, Publication}; multi-epoch and multi-generation navigation.
- **Patch node → diff** (`views/diff.js`) — the lifecycle PATCH node opens this
  candidate's **side-by-side diff** with REAL strings (`.baseline.content` left,
  `new_content` right).
- **Boards** (`views/boards.js`) — the small-multiples **trellis**; cards route
  to the per-board view by entry id.
- **Board** (`views/board.js`) — first-class per-board cross-candidate view;
  selecting a run shows the transcript **INLINE, side by side** with the
  champion's (no route to a separate run page).
- **Mutation surface** (`views/mutations.js`) — site × generation matrix + the
  combined side-by-side diff pane.
- **Epoch overview** (`views/epoch.js`) — objective, brief, lineage bumps, and
  the board×generation drift-loss **heatmap** (the trellis lives in Boards —
  de-duped, never both on one page).
- **Publication** (`views/publication.js`) — K's renderer, epoch-scoped ACM
  paper with live Tufte figures + per-matchup detail.
- **Environment** (`views/home.js`) — the workspace as a fleet.

## Back / up (the round-6 fix to Q's bug)

The top-left **↑ up** control resolves `router.upTarget(route)` — the parent of
the current selection (entry → candidate → Generations → epoch → environment) —
and navigates there. The destination paints into the **MAIN detail pane** via
the normal dispatch path; the tree rail host is **never** touched (Q rendered
the destination into the side panel — this fixes that). Disabled at the
environment root.

## Carried-forward round-5 capabilities (intact, do-not-regress)

Tree sidebar; promote gate on the candidate page (stacked, non-overlapping);
patch node → per-candidate side-by-side diff (real strings); ALL match-ups for a
candidate (v0 shows v0→v1 AND v0→v2); first-class board view; board run → inline
side-by-side transcript; trellis in Boards / heatmap at epoch overview
(de-duped); colour picker (3 themes) + typeface picker (Open-Sans Google-Fonts
pairings, the only permitted external dependency); digest-gating; theme-aware
heatmap; Tufte sankey label≠value; cold deep-link hydration; constrained-scroll
transcripts; no pan/zoom anywhere.

## Constraints honoured

Vanilla ESM, no build, no external libs except Google Fonts (fonts only).
Self-contained within `js/variants/V/**` + `css/variants/V/**` (reusing
`js/core/*`). All CSS scoped under `#variant-root[data-variant="V"]`
(`data-v-theme` / `data-v-type`). `index.html`, `js/core`, `js/v2`, `app.js`,
`app2.js`, other variants, and the test harness are untouched. The variant's
`svg.js` is trimmed to the marks the Reel views render (the illustrative
tournament-style marks + Sankey + small-multiple wrapper are dropped) to keep
the bundle lean.

## Files

- `app_V.js` — entry point (stylesheet + Google Fonts + shell mount).
- `js/variants/V/reel.js` — the reel hero (timeline + scrubber).
- `js/variants/V/shell.js` — chrome: reel hero host + tree rail + detail pane +
  back/up control; digest-gated dispatch.
- `js/variants/V/router.js` — `#/V` hierarchy + `~cmp=` compare suffix + back/up
  `upTarget`.
- `js/variants/V/{tree,data,ui,svg,dag,compare}.js` — tree sidebar, read layer,
  chrome helpers, marks, lifecycle DAG, S's compare primitives.
- `js/variants/V/views/{home,epoch,gens,candidate,diff,boards,board,mutations,publication}.js`.
- `css/variants/V/reel.css` — scoped styles (3 colour themes, 4 typefaces, reel,
  compare, back).
- `test/variant_v.test.mjs` — unit tests (19, all green).
