# Variant J — "Console": a dense observatory, Monokai default

Console is a round-3 **convergence** dashboard. It keeps Variant E's IA and
flow wholesale (the operator confirmed E's flow is "likely fine") but reskins
it as a **dense, data-ink-maximal console for power users**: Monokai default
theme, compact chrome, tighter trellises and side-by-side panels, minimal
padding, keyboard-friendly. Everything stays legible but packed.

It is **self-contained** under `js/variants/J/**` + `css/variants/J/**` and
reuses only the shared `js/core/*` data layer. It imports from no other
variant directory.

## The screens

Navigation is E's hierarchical breadcrumb IA over a compact top bar
(branding · breadcrumb · dense primary-nav · three-theme switcher · status
pill), routed under the `#/J/` prefix.

- **Home / Environment** (`#/J/`) — the workspace as a fleet: a cross-epoch
  overview strip, one tight console card per epoch carrying the per-epoch
  loss **trendline** hero, loop-health, and a cross-epoch trajectory
  sparkline.
- **Epoch** (`#/J/epoch`) — the dense data substrate of one epoch: the
  objective banner + collapsible proposer brief, quick-links to the two new
  views, a **non-colliding lineage bumps** chart, the board entries ×
  generations **theme-aware drift-loss heatmap**, and a tight **board
  trellis** of small multiples.
- **Candidate** (`#/J/candidate/<gen>[/<entry>]`) — the compact **lifecycle
  DAG** (parent → patch → board fan → Σ → gate → terminal) + the per-board
  **value dot-plot** (reference line at the champion's scalar), with a
  URL-driven entry drill-down (expectation outcomes + per-judge losses + a
  **themed** "open full transcript" link/button).
- **Match-ups** (`#/J/matchups`) — the **fit-to-width Tufte Sankey** (causal
  flow: challenger patch → per-board drift → gate verdict), the gauntlet
  **bumps** ladder, per-round **paired slopegraphs** (de-collided + jittered),
  and the illustrative alternative-tournament marks (bracket / round-robin /
  race-lanes).
- **Mutations** (`#/J/mutations[/<mutId>]`) — **NEW.** A dense **mutation-site
  × generation matrix**: rows = mutation sites (`file:line` + role), columns =
  generations, a filled cell = that generation patched that site. Click a
  cell/site to drill into the **patch diff** that generation applied
  (`/api/files/{epoch}/{gen}/patches`), rendered as a themed line diff.
- **Report** (`#/J/report[/<epochId>]`) — **NEW.** The **ACM-style epoch
  publication**, rendered Tufte/editorial from `analysis_md`. It parses the
  analyzer's section markers (`<!-- EYEBROW -->`, `<!-- META -->`,
  `## Abstract`, body sections, `<!-- FIGURE:NAME -->`, `<!-- CALLOUT:... -->`)
  to typeset DOM and **embeds live Tufte figures inline** (lineage bumps,
  matchup slopegraph, drift heatmap) where the markdown places a figure
  marker. (`analysis.html` may 404 — `analysis_md` is the source of truth.)
- **Run** (`#/J/run/<gen>/<entry>`) — the reconstructed transcript in one
  constrained, scrollable container. **Cold deep-link safe**: it resolves the
  `run_id` from per-entry, then fetches `/api/conversation/{run_id}` itself.

## The three themes (Monokai default)

Three switchable themes — **monokai (default)**, **solarized-dark**,
**solarized-light** — each a CSS custom-property set mapping to the `--v2-*`
token names every mark reads, swapped by the `[data-j-theme]` attribute on the
variant root and persisted in `localStorage`. The semantic mapping (improve /
regress / caution) holds across all three. Every mark reads with sufficient
contrast in each theme — including the per-board dot-plot in Monokai — because
no mark hard-codes a colour. The **heatmap** in particular is theme-safe by
construction: each cell is the themed ink token at a value-driven *opacity*
(denser ink = more drift) rather than a hard-coded hex ramp.

## Diagram discipline (no viewport)

NO diagram lives in a pan/zoom viewport. The **Tufte Sankey** is laid out to
**fit the container width** (responsive `width: 100%` + a `viewBox` so it
scales, never pans), with thin flows, direct in-place labels, restrained
improve/regress colour, and no decorative gradients or shadows. The **lineage
bumps** de-collide coincident node x-positions within each lane (the F bug:
v1/v2 collided) and every node is clickable → its candidate. The paired
slopegraph de-collides labels and jitters coincident nodes.

## Render discipline (DASHBOARD-V2)

Mirrors E's spine: ONE persistent content host (never recreated); every view
**digest-gates** its repaint on structural data only (timestamps excluded), so
a steady heartbeat tick that re-dispatches the active view is a true DOM no-op
(zero `innerHTML` writes); the host is cleared on a view switch; drill-down
caches invalidate only on a route change or explicit action, never on a
heartbeat; cold deep-link routes fetch their own data with honest fallbacks;
theme/hover changes are CSS `transition`, never `animation: … infinite`.

## Files

- `app_J.js` — entry point (injects the scoped sheet, mounts the shell).
- `js/variants/J/shell.js` — compact top bar, three-theme switcher, digest-
  gated dispatch.
- `js/variants/J/router.js` — `#/J/` hash router + breadcrumb (adds the two
  new views).
- `js/variants/J/data.js` — cached, failure-tolerant drill-down reads (adds
  `/api/mutations/{epoch}`, `/api/files/{epoch}/{gen}/patches`,
  `/api/epoch/{epoch}/analysis`).
- `js/variants/J/svg.js` — self-contained data-viz primitives (`dj-*`),
  incl. the theme-aware heatmap and the fit-to-width Tufte Sankey.
- `js/variants/J/dag.js` — the compact lifecycle DAG (`ezj-*`).
- `js/variants/J/ui.js` — gatedSwap, section/pill/stat helpers, the themed
  link/button, the tiny-markdown renderer.
- `js/variants/J/views/{home,epoch,candidate,matchups,mutations,report,run}.js`.
- `css/variants/J/console.css` — the dense Console skin + all three theme
  token sets, scoped under `#variant-root[data-variant="J"]`.

## Tests

`test/variant_j.test.mjs` covers: digest no-op (home + epoch); cold deep-link
transcript (+ honest empty); the mutation-per-generation matrix + patch-diff
drill; the ACM report rendering from `analysis_md` with a live inline figure
(+ honest "not built yet"); the Sankey fit-to-width (responsive width, no
viewport) + proportional ribbons; lineage bumps non-colliding + clickable;
and the three-theme switcher with Monokai as default.
