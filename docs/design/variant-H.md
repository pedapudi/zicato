# Variant H — "Atlas II" (convergence)

Atlas II is the **conservative refinement of Variant E**. The operator
confirmed E's flow is "likely fine", so H keeps E's exact information
architecture and screen flow and *completes* it: it fixes the named bugs,
adds the three-theme switcher, adds the two views E lacked, and enforces
fit-to-width Tufte discipline on every diagram. The safe, complete
convergence.

`?ui=H` is wired in `index.html` (untouched). `app_H.js` injects the scoped
stylesheet and mounts the shell into `#variant-root`.

## Identity

- **Default theme: Solarized-Dark.** A calm, precise, high-data-ink colourway.
- E's flow, verbatim: a top bar (branding · hierarchical breadcrumb · primary
  nav · status pill) over ONE persistent, digest-gated content host.
- Tufte everywhere; no diagram lives in a pan/zoom viewport.

## Screens

| Route | Screen | What it shows |
| --- | --- | --- |
| `#/H/` | **Environment** | The workspace as a fleet — per-epoch loss-trendline hero cards, a cross-epoch overview strip + trajectory, loop health. |
| `#/H/epoch` | **Epoch** | Objective + proposer brief, the lineage **bumps** chart (non-colliding, clickable), the board-entry × generation drift-loss **heatmap** (theme-aware ramp), and the board **trellis** of per-entry sparkbars. Jump links to the two new views. |
| `#/H/candidate/<gen>` | **Candidate** | The compact **lifecycle DAG** (parent → patch → board → Σ → gate → terminal), the NEW fit-to-width **Tufte Sankey** (candidate → per-board loss → aggregate scalar), and the per-board scoring **dot-plot** vs the champion reference. Entry param drills in. |
| `#/H/candidate/<gen>/<entry>` | **Entry drill-down** | The entry's expectation outcomes + per-judge weighted losses, and a properly themed **button-like link** into the full run transcript (the E bug fix). |
| `#/H/matchups` | **Match-ups** | The real gauntlet ladder (fit-to-width bumps), per-round paired slopegraphs (de-collided + jittered), and illustrative alternative tournament structures (bracket / round-robin / race lanes). |
| `#/H/run/<gen>/<entry>` | **Run detail** | The reconstructed transcript in one constrained-scroll container. Cold-deep-link safe: resolves `run_id` from per-entry, fetches `/api/conversation/{run_id}`, honest fallback when no events. |
| `#/H/mutations` | **Mutation sites × generations** (NEW) | A matrix of every enumerated mutation site (rows = `file:line` + role) against every challenger generation (cols); a filled cell marks a patch at that site. |
| `#/H/mutations/<id>` | **Patch diff** (NEW drill-down) | The realized red/green line diff of what each generation changed at the selected site. |
| `#/H/report` | **Epoch analysis report** (NEW) | The ACM-style epoch publication, rendered from `analysis_md` (eyebrow / title / meta / abstract / sections / tables / callouts) with live Tufte figures (the lineage bumps) embedded inline. |

## The mandates (round-3 appendix)

1. **Three-theme system.** `solarized-light`, `solarized-dark` (default),
   `monokai`, as the `--v2-*` token set the marks read, redefined per
   `#variant-root[data-h-theme=…]`. A visible switcher in the top bar sets the
   attribute and persists the choice through `localStorage`. Every mark reads
   with sufficient contrast in all three.
2. **Tufte Sankey, fit-to-width, NO viewport.** `js/variants/H/diagram/sankey.js`
   reuses C's `layoutSankey` plumbing, re-skinned Tufte: thin *stroked* flows
   (not filled ribbons), direct in-place labels, a hairline column header,
   restrained improve/regress colour. The view passes the container width; the
   `<svg>` is `width:100%;height:auto` so it fits and never pans.
3. **Lineage = Tufte bumps**, ported from D/E (`svg.bumps` + `decollide`):
   nodes are clickable (→ candidate) and de-collided so coincident challengers
   (v1/v2) never overdraw.
4. **NEW — mutation sites per generation**: `/api/mutations/{epoch}` for the
   matrix, `/api/files/{epoch}/{gen}/patches` for the drill-down diff (ported
   patch-diff renderer, `hd-*`).
5. **NEW — ACM-style epoch publication**: `/api/epoch/{epoch}/analysis`
   `analysis_md`, rendered editorially (`hp-*`) — same approach as
   `js/v2/views/report.js`, but parsed + typeset here so it themes with H.
6. **Per-board scoring readable in all themes**: the heatmap ramp endpoints
   come from theme tokens (`--v2-ramp-lo/-hi`, `--v2-good/-bad`), and the
   dot-plot marks read the themed `--v2-*` palette.

## Bug fixes carried in (must not reproduce)

- **E** — the "open full transcript" link is now a themed `.h-link-btn`
  button-like link, never an unstyled anchor.
- **F** — lineage nodes are de-collided and every node's click is wired to its
  candidate; no viewport.
- **G** — the Sankey is fit-to-width Tufte (no pan/zoom); the per-board
  dot-plot / heatmap have sufficient contrast in all three themes.

## Render discipline

Mirrored from E / `js/v2/shell.js`: a `state:changed` heartbeat re-dispatches
the active view, which digest-gates its own repaint (digest = structural data
only, no timestamps) — an unchanged tick writes ZERO DOM. The host is cleared
only on a view switch (which also invalidates the drill-down cache — a user
action, never a heartbeat); the host is reused, never recreated. Drill-down
selection lives in the URL so it rebuilds only on a route change. CSS uses
`transition`, never `animation:…infinite`; the transcript / event tails are
constrained-scroll containers with no overlap.

## Self-containment

Everything lives under `js/variants/H/**` + `css/variants/H/**` + `app_H.js`.
H imports only from `js/core/*`; the data-viz toolkit (`svg.js`), the lifecycle
DAG (`dag.js`), the patch-diff renderer (`patchDiff.js`) and the chrome helpers
(`ui.js`) are ported in, not imported from other variant dirs. All CSS is
scoped under `#variant-root`.

## Tests

`test/variant_h.test.mjs` covers: the router (E's IA + the two new routes);
digest no-op on a heartbeat re-dispatch; the cold deep-link transcript; the
mutation matrix + its patch-diff drill-down; the ACM report (eyebrow / title /
meta / abstract / sections / inline live figure / table); the Tufte Sankey is
fit-to-width with no pan/zoom wrapper; lineage bumps de-collide + are clickable;
the themed transcript link; and the three-theme switch applying `data-h-theme`.
