# Variant I — "Ledger" (round-3 convergence)

Editorial, light-first, publication-leaning convergence dashboard, built on
**Variant E's IA / flow** (the operator confirmed E's flow is "likely fine").
Where Atlas (E) is a data-first observatory in Solarized-Dark, **Ledger reads
like a research publication**: Solarized-Light default, airy generous
whitespace, a serif display voice, and the ACM-style epoch paper promoted to a
prominent first-class tab with the dashboard's own live Tufte figures embedded
as its figures. Report-style framing (eyebrows, ledes, figure captions,
pull-quotes) appears throughout.

`?ui=I` is wired in `index.html` (do not touch). Entry point: `app_I.js` →
`js/variants/I/shell.js`. Everything is scoped under
`#variant-root[data-variant="I"]` and is **self-contained within
`js/variants/I/**` + `css/variants/I/**`** — it imports only `js/core/*`,
never another variant directory (E's marks/dag are ported in, not imported).

## Screens

`#/I/` hash routes (E-style hierarchical breadcrumb IA, extended with two new
views):

- **Environment** (`home`) — the workspace as a fleet: an eyebrow + serif
  title + report lede, an overview stat strip, per-epoch loss-trendline fleet
  cards (the loved element), loop health, and a cross-epoch trajectory figure
  with a caption.
- **Epoch** (`epoch`) — the epoch as a paper's opening pages: objective lede,
  a prominent rail linking to the **Publication**, the proposer brief
  (collapsible, safe markdown), then three captioned figures — the
  non-colliding **lineage bumps**, the board × generation **drift heatmap**
  (theme-aware ramp), and the **board trellis** small-multiples.
- **Candidate** (`candidate`) — one generation's life: the hypothesis bet as a
  **pull-quote**; the **fit-to-width Tufte Sankey** (candidate → per-board loss
  → aggregate scalar, NO viewport, board nodes clickable); the compact
  **lifecycle DAG**; the per-board scoring **dot-plot** (vs the champion
  reference); on rejection, the gate's reason as a pull-quote; and the
  URL-driven entry drill-down (expectations + per-judge bars + a **themed
  button** into the transcript — the E bare-anchor bug is fixed).
- **Match-ups** (`matchups`) — the real gauntlet ladder (bumps), per-round
  paired slopegraphs (de-collided + jittered), and illustrative alternative
  tournament structures (bracket / round-robin / race lanes), clearly labelled.
- **Mutations** (`mutations`) — **NEW.** The mutation-site × generation matrix
  from `/api/mutations/{epoch}` joined with `/api/files/{e}/{g}/patches`; a
  filled cell is a patch, click it (or the site name) to drill into the
  per-generation patch diff (baseline vs patched content, via
  `/api/mutations/{e}/{mutation_id}` with a patches-endpoint fallback). Laid
  out in a constrained-scroll x-rail — no pan/zoom.
- **Publication** (`paper`) — **NEW, the signature.** Renders the epoch's
  `analysis_md` (`/api/epoch/{id}/analysis`) as a typeset publication: the same
  section-marker model the standalone renderer uses (`<!-- EYEBROW -->` /
  `# Title` / `<!-- META -->` / `## numbered sections` / `<!-- FIGURE: id -->`
  / `Caption:`) parsed into DOM nodes (never `innerHTML`). The paper's figures
  are the dashboard's **own live Tufte charts** (lineage bumps / matchup
  slopegraph / drift sparkline) substituted at each FIGURE marker; when the
  markdown carries no figure slots a "Figures" movement of live charts is woven
  in. Honest not-built / unavailable states; never the v2 iframe.
- **Run** (`run`) — the reconstructed transcript; cold deep-links hydrate the
  run_id then fetch `/api/conversation/{run_id}` themselves; one constrained,
  scrollable container (no absolute positioning).

## Three-theme system

`js/variants/I/theme.js` ports B's three-theme **token system** into the
operator's named palettes — **solarized-light (default)**, **solarized-dark**,
**monokai** — with a visible segmented switcher in the top bar, persisted to
`localStorage`. Each theme defines both the `--v2-*` token set the carried-over
`d-*`/`ez-*` marks read AND the `--i-*` editorial + heatmap-ramp tokens. The
heatmap/dot ramp (`svg.js rampColor`) interpolates between **live theme
tokens** read at draw time, so per-board scoring and every diagram read with
sufficient contrast in all three themes.

## Render discipline

Mirrors DASHBOARD-V2 / E: one persistent content host; digest-gated repaint
(`gatedSwap`, `data-i-digest`) where a heartbeat-only tick writes zero DOM;
host cleared + caches invalidated only on a view switch; module-scope
drill-down via URL params; cold deep-link routes fetch their own data;
constrained-scroll for the transcript and the wide matrix/diagrams; CSS
`transition`, never `animation:…infinite`; no pan/zoom viewport on any diagram
(the Tufte Sankey is `viewBox` + `width:100%`, fit-to-container).

## Tests

`test/variant_i.test.mjs` (19 tests) covers: router incl. the two new views;
gatedSwap no-op; home digest no-op; **lineage non-colliding + clickable**
(distinct lanes + `decollide` min-gap); **Tufte Sankey fit-to-width, no
viewport** + clickable board drill; candidate Sankey + dot-plot + pull-quotes +
drill; **cold deep-link transcript** + honest empty; **mutation-per-gen matrix**
+ patch drill; report parser markers; **ACM paper masthead + numbered sections
+ live embedded figure** + not-built fallback; **three-theme switch**; epoch
figures + digest gate.

## Note on the bundle envelope

`tests/test_dashboard_ui.py::test_bundle_under_size_envelope` caps the
*deliberately temporary* side-by-side exploration bundle. That cap has been
raised at every wave (470 KB → … → 1,900 KB for round-2's seven variants).
Adding any complete round-3 variant exceeds it; Variant I's JS (after trimming
unused marks) is ~150 KB and the round-2 base left ~86 KB of headroom, so the
cap needs the routine one-line bump to ~2,000 KB. The named JS gate
(`node test/run-all.mjs`) is green at 625/0.
