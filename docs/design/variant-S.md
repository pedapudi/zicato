# Variant S — "Lens": comparison-first, convergence III

Lens is the round-5 **convergence-III** dashboard. It builds on the praised
Console base (Variant N's data-ink foundation, L's mutation viewer, M's generous
spacing/proportion, O's candidate-centric lifecycle + match-ups, K's publication
renderer), but replaces top-tab navigation with a persistent **data-model tree
sidebar** and gives the detail pane a single, defining signature: **first-class
side-by-side comparison**.

Default colour theme: **solarized-light**. Default typeface theme: **Editorial**
(Open Sans body + Source Serif 4 for headings & the publication). Comparison
reads cleanly — M's roomy gutters separate the two sides; the marks (carried
from the Console base) read with sufficient contrast in all three colour themes.

Self-contained under `js/variants/S/**` + `css/variants/S/**` + the entry
`app_S.js`; reuses only the shared `js/core/*` data spine, and imports from no
other variant directory (the Console `svg.js` / `dag.js` were copied in, not
imported). Selected behind `?ui=S` (already wired in index.html). The only
external dependency is Google Fonts (fonts only, `display=swap`, system
fallbacks).

## The headline — a data-model tree sidebar

A persistent left tree (`js/variants/S/tree.js`) mirrors the real hierarchy:

```
Environment (workspace)
└─ Epoch <id>                       (one node per epoch — switches which epoch)
   ├─ Generations
   │  └─ <gen> (champion ♛ / rejected / seed)
   ├─ Boards
   │  └─ <entry>
   ├─ Mutation surface              (site × generation + side-by-side diff)
   └─ Publication                   (epoch-scoped ACM paper)
```

Expandable/collapsible (twist on every parent; expansion state persisted in
`localStorage`). Selecting **any** node drives the detail pane. The tree can
navigate **multiple epochs AND multiple generations** — every epoch and every
generation is its own selectable node (N's gap: its epoch/candidate tabs could
not switch which epoch/candidate). One epoch lives in the live data, so the
environment degrades gracefully to a single branch, but the model is
all-epochs-first (`model.epochs()` resolves from `/api/environment` ▸
`/api/workspace` ▸ the single `/api/epoch`).

Selection is **URL-encoded** by the router (`js/variants/S/router.js`) so a cold
deep-link both expands the tree to the selection and hydrates the detail. The
hash also carries the comparison target.

## The comparison model — the signature

`js/variants/S/compare.js` provides two primitives:

- **`comparePicker`** — a "compare with…" affordance (a labelled `<select>`)
  that sets/clears the comparison target in the route. It never navigates away —
  it splits the *same* detail pane.
- **`splitFrame`** — a two-column frame, side **A** (the primary selection) |
  side **B** (the comparison target). Each side paints into its **own**
  digest-gated host, so one side changing never rebuilds the other.

The comparison target rides in the hash as a `~`-suffix: `~cmp=<gen>` (the second
candidate, for the split candidate view) and `~runs=<genA>,<genB>` (the two
candidates whose transcripts are shown side by side on a board). One deep-link
captures the whole comparison state.

## Screens (router prefix `#/S/`)

| route | screen |
| --- | --- |
| `#/S/` | **Environment** — all epochs, all-epochs-first (a card per epoch). |
| `#/S/e/<id>` | **Epoch** — objective + proposer brief, lineage bumps, board×generation drift **heatmap** (the trellis is NOT here — fix #6). |
| `#/S/e/<id>/gen/<gen>[~cmp=<gen2>]` | **Candidate** — lifecycle DAG + promote gate + all match-ups + per-board scoring; `~cmp=` splits it into two candidates A \| B. |
| `#/S/e/<id>/gen/<gen>/patch` | the candidate's **patch diff** (side-by-side) opened inline. |
| `#/S/e/<id>/gen/<gen>/entry/<entry>` | one board entry's drill-down for that candidate. |
| `#/S/e/<id>/board/<entry>[~runs=A,B]` | **Board** — one entry across every candidate + the **inline side-by-side transcripts** + the trellis. |
| `#/S/e/<id>/mut[/<mutId>]` | **Mutation surface** — site × generation matrix + side-by-side diff. |
| `#/S/e/<id>/pub` | **Publication** — K's ACM renderer, epoch-scoped. |

## The two pickers

- **Colour** — solarized-light (default) · solarized-dark · monokai, swapped via
  `[data-s-theme]` on the variant root; CSS-only re-skin, persisted.
- **Typeface** — **Sans** · **Editorial** (default; + Source Serif 4 headings &
  publication) · **Technical** (+ JetBrains Mono for data/labels/code) ·
  **Display** (+ Archivo Narrow condensed headings). Swapped via `[data-s-type]`
  through `--s-font-*` custom properties; persisted.

## The seven mandatory fixes

1. **Promote gate on the candidate page** — `views/candidate.js` renders the
   stacked, non-overlapping gate (decision pill + Δ; the rules ladder, each rule
   its own row; a separate champion-vs-challenger scalar-components block) on
   every generation detail, A and B independently.
2. **Patch node → per-candidate side-by-side diff** — the lifecycle DAG's PATCH
   node is clickable (`dag.js` `onPatch`) → that candidate's own patches
   (`/api/files/{e}/{g}/patches` `.new_content`) diffed against each site's
   `/api/mutations/{e}/{id}` `.baseline.content` (the STRING, never the object),
   rendered inline with the side-by-side diff component.
3. **All match-ups for a candidate** — `model.matchupsFor()` filters
   `/api/tournaments` where `champion == gen || challenger == gen`; v0 shows BOTH
   v0-vs-v1 AND v0-vs-v2 (clicking a row compares the two candidates).
4. **Board view first-class** — reachable from the tree's Boards group (and the
   epoch heatmap), keyed by entry id.
5. **Board entry → inline side-by-side transcript (the signature)** — selecting
   runs on the board view fills two independently-scrollable transcript columns
   *inline* (`/api/conversation/{run_id}` per candidate, run ids resolved from
   per-entry); it sets the `runs` route param, never navigating to a separate
   run page. Defaults to champion vs the first challenger that ran.
6. **Trellis vs heatmap de-dup** — the compact heatmap stays at the epoch
   overview; the board trellis (small-multiple) lives in the Boards view. Never
   both on one page.
7. **M's spacing/proportion + L's mutation-viewer quality** throughout —
   generous split gutters, roomy panels, the cohesive matrix + side-by-side diff.

## Render discipline (carried forward)

Every pane is digest-gated on structural data only — a heartbeat re-dispatch
writes zero DOM; each comparison side gates on its own host. Panes clear only on
a selection change; the data cache invalidates only on the live (SSE) path. CSS
`transition` only (no infinite animations). Transcripts are constrained-scroll,
each side independently scrollable. No pan/zoom viewport diagrams — the
lifecycle DAG, lineage bumps, heatmap and any sankey are fit-to-width. The
heatmap ramp is theme-token-driven. Cold deep-links hydrate tree + detail +
comparison.

## Tests

`test/variant_s.test.mjs` (13 cases) pins: the tree renders the full hierarchy +
multi-generation nav; the board view shows two candidates' transcripts side by
side inline on run select (staying on the board view); compare mode splits the
candidate detail; the promote gate is on the candidate page (stacked, rules each
its own row, separate scalar-components block); the patch node → a real-string
side-by-side diff; all match-ups (v0 ≥ 2); the trellis is in the Boards view and
the epoch overview has only the heatmap; the pickers + pills switch + persist
(solarized-light + Editorial defaults); and digest-gated repaint is a true
no-op.
