# Variant W — "Arena": the tournament as a live broadcast, convergence IV

Arena is the round-6 **convergence-IV CREATIVE** dashboard. It re-frames the
king-of-the-hill gauntlet as a live broadcast: a billboard header, the reigning
champion **defending the title**, and each challenger rendered as a **match
card**. The standings are the hero AND the navigation. Underneath, it keeps the
Console III (Variant P) data-model **tree sidebar** and detail views, folds in
Variant S's **side-by-side comparison**, and adds a **fixed back/up control**.

Default colour theme: **monokai** (a broadcast-dark canvas; **solarized-dark**
is the calm alternate, **solarized-light** the third). Default typeface:
**Display** (Open Sans body + Archivo Narrow for headings & big numbers — the
billboard voice). Energetic but legible; everything is **fit-to-width** (NO
pan/zoom viewport). Selected behind `?ui=W` (already wired in index.html).

## The headline — broadcast STANDINGS + MATCH CARDS

`js/variants/W/views/home.js` is the hero and the home view:

- a **billboard header** — `ZICATO · ARENA` kicker, an ON-AIR/idle status pill,
  the epoch title, the operator's goal, and a stat strip (reigning champion ·
  title loss · rounds fought · board entries);
- the **champion card** at the top — the title-holder, "defending the title",
  with its loss and defence count, a crown, and a `promoted` pill;
- one **match card per challenger round** — the `champion vs challenger`
  matchup, a verdict pill, the **Δscalar** (signed, coloured by improve/regress),
  the **hypothesis core idea**, and the decisive driver judge.

The standings **double as navigation**: a match card is an anchor to that
challenger's candidate detail (`#/W/e/<e>/gen/<chall>`); the champion card opens
the champion. Bound to `/api/tournaments` + `/api/lineage` +
`/api/score-trajectory` + the per-round `/api/round/{e}/{champ}/{chall}/gate`
(for the decisive-driver line); rounds order by `ran_at`. A compact season
trajectory sparkline closes the page.

## The data-model TREE sidebar (carried forward, collapsible)

`js/variants/W/tree.js` keeps Console III's persistent, collapsible left tree:

```
Environment (workspace)
└─ Epoch <id>                       (one node per epoch — multi-epoch nav)
   ├─ Generations
   │  └─ <gen> (champion ♛ / rejected / seed)
   ├─ Boards
   │  └─ <entry>
   ├─ Mutation surface
   └─ Publication
```

Selecting any node drives the single detail pane; the open set is the union of
the route's open-path and the user's manual toggles; selection is URL-encoded
(cold deep-link hydrates branches + detail); digest-gated (a heartbeat writes
zero DOM). It navigates multiple epochs AND multiple generations.

## Detail views (router prefix `#/W/`, path = tree path)

| route | view |
| --- | --- |
| `#/W/` | **Arena** — broadcast standings + match cards (the hero). |
| `#/W/e/<e>` | **Epoch** — objective + proposer brief, lineage bumps, the board×generation drift-loss **heatmap** (the trellis is NOT here — de-dup). |
| `#/W/e/<e>/gens` | **Generations** — the candidate roster. |
| `#/W/e/<e>/gen/<gen>[/<entry>][~cmp=<g2>]` | **Candidate** — lifecycle DAG + per-board dot-plot + entry drill + ALL match-ups + the stacked promote gate; `~cmp=` splits it into two candidates A \| B. |
| `#/W/e/<e>/gen/<gen>/diff[/<mutId>]` | **Patch diff** — this candidate's side-by-side diff (real strings). |
| `#/W/e/<e>/boards` | **Boards** — the board **trellis** (small-multiples). |
| `#/W/e/<e>/board/<entry>[~runs=A,B]` | **Per-board** — one entry across every candidate + the **inline side-by-side transcripts**. |
| `#/W/e/<e>/mutations[/<mutId>]` | **Mutation surface** — site × generation matrix + side-by-side diff. |
| `#/W/e/<e>/paper` | **Publication** — K's ACM renderer, epoch-scoped. |

The comparison target rides in the hash as a `~`-suffix (S's scheme): `~cmp=<gen>`
splits the candidate detail; `~runs=<genA>,<genB>` is the two transcripts shown
side by side on a board. One deep-link captures the whole comparison state.

## The two folded-in round-6 capabilities

1. **Side-by-side comparison (from S).** First-class in the detail pane. On the
   candidate view a "compare with…" picker (`js/variants/S/compare.js`
   `comparePicker` + `splitFrame`) splits it into two candidates A \| B —
   lifecycle, promote gate, match-ups, per-board scoring side by side — each side
   on its own digest-gated host. Clicking a match-up row compares the two
   candidates. On the board view the two transcript columns are an S split-frame.
2. **Fixed back/up control.** A top-left control that navigates **UP** the
   selection hierarchy (`router.parentRoute`) and renders the destination into
   the **MAIN detail pane** (`_viewHost`), **NEVER** the sidebar — the round-6
   fix of Q's bug. It routes via `navigate(...)` → hashchange → the regular
   dispatch, which only ever clears + repaints the detail host; the rail is
   untouched. The test asserts this explicitly (after back, the rail still holds
   the tree and the detail holds the destination).

## Carried-forward Round-5 fixes (intact, never regressed)

- **Promote gate ON the candidate page** — stacked, non-overlapping (decision
  header · rules ladder, each rule its own row · separate champion-vs-challenger
  scalar-components block).
- **Patch node → per-candidate side-by-side diff** — the lifecycle DAG's PATCH
  node (and an explicit link) routes to the `diff` view: this candidate's own
  `/api/files/{e}/{g}/patches` `.new_content` diffed against each site's
  `/api/mutations/{e}/{id}` `.baseline.content` (the STRING, never the object).
- **ALL match-ups for a candidate** — `model.matchupsFor` filters
  `/api/tournaments` where `champion==gen || challenger==gen`; v0 shows ≥2.
- **First-class board view** — reachable from the tree's Boards group, the
  trellis, and the epoch heatmap (keyed by entry id).
- **Board run → INLINE side-by-side transcript** — selecting a candidate sets
  the `runs` target and fills two columns inline; no separate run page.
- **Trellis in Boards / heatmap at epoch overview** — de-duplicated; never both
  on one page.
- **Pickers** — colour (3 themes) + typeface (4 Open-Sans Google-Fonts pairings),
  CSS-only swaps via `[data-w-theme]` / `[data-w-type]`, persisted.
- Digest-gating; theme-aware heatmap; Tufte fit-to-width visuals with label ≠
  value; K's publication epoch-scoped.

## Reuse + self-containment

W's NEW logic — the arena standings (`views/home.js`), the broadcast shell
(`shell.js`) with the fixed back control, the router (`router.js`) with the
comparison suffix + `parentRoute`, the tree (`tree.js`), the shared resolver
(`model.js`), the comparison-first candidate + board (`views/candidate.js`,
`views/board.js`), the chrome helpers + theme tables (`ui.js`) — all live under
`js/variants/W/`. Per the round-6 brief ("reuse P's views; do NOT regress") it
binds the **router-agnostic** Console III detail views (epoch, generations,
boards, patch diff, mutation surface, publication) and the leaf primitives
(`svg.js`, `dag.js`, the read layer `data.js`) directly from Variant P, and S's
`compare.js` — driving them entirely through W's injected `ctx.navigate` /
`ctx.href` and arena chrome. This reuse keeps the (temporary) side-by-side
exploration bundle within its byte envelope; once a variant is chosen and the
others are deleted, W would inline whatever it still needs.

All CSS is scoped under `#variant-root[data-variant="W"]`
(`css/variants/W/arena.css`): the ported Console marks (`dn-*`), the arena chrome
(`dw-*` — back control, billboard, standings, champion + match cards, tree), and
S's comparison frame (`vs-*`). CSS `transition` only — no infinite animations.
The only external dependency is Google Fonts (fonts only, `display=swap`, system
fallbacks), loaded in `app_W.js`.

## Render discipline

Digest-gated repaint (structural data only; heartbeat = no-op); the MAIN detail
host cleared on selection change (never the sidebar); one persistent host per
pane; caches invalidated only on the live (SSE) path; constrained-scroll
transcripts; cold deep-link hydration of tree + detail + comparison; NO pan/zoom
viewport (everything fits the container width).

## Tests

`test/variant_w.test.mjs` (17 tests) pins: the router (home default, the
comparison suffix, href round-trip); the **standings render the champion
(defending) + a match card per challenger with a verdict pill + Δscalar**;
**clicking a match card opens that challenger**; the standings are
**fit-to-width** (no pan/zoom); the **tree sidebar** renders the hierarchy AND
navigates multiple generations; the **promote gate on the candidate page**
(stacked, rules each its own row, separate scalar-components block); the
**patch → per-candidate side-by-side diff with real strings** and the lifecycle
patch node routing there; **ALL match-ups** (v0 ≥ 2); the **compare split**;
the **first-class board view + INLINE side-by-side transcripts** (run select
stays on the board, sets `runs`); trellis in Boards / heatmap at epoch; the
**back/up control rendering into the MAIN pane, not the sidebar**; the
side-by-side diff primitive; the **monokai + Display** picker defaults switching
+ persisting; and a digest no-op repaint.
