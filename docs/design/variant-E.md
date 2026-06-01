# Variant E — "Atlas": a data-first observatory

Atlas is the round-2 synthesis that leads with quantitative data-viz. It is
calm, precise, and high data-ink: a Solarized-Dark observatory where the
numbers are the interface and the diagrams are on demand. It combines the
best parts of the four round-1 explorations:

- **A** — the hierarchical breadcrumb IA (it tested well; almost everything
  clickable went where expected) and the "Fleet" epoch card with the
  per-epoch loss **trendline** hero that users explicitly loved.
- **D** — the Tufte data-viz toolkit: sparklines, sparkbars, the
  non-colliding bumps chart, the entries×generation heatmap, the board
  trellis of small multiples, the per-board value dot-plot, the paired
  slopegraph (de-collided + jittered), and the illustrative
  alternative-tournament marks (bracket / round-robin / race-lanes).
- **C** — the causal **lifecycle DAG** (parent → patch → board fan → Σ →
  gate → terminal), rendered compactly as a static figure on the candidate
  screen.
- **B / D** — aesthetic restraint and the layered token theming; Atlas's
  default is **Solarized-Dark** (ground `#04222B`, surface `#0A2D38`, ink
  `#93A1A1`, improve `#8BB80E`, regress `#E0483C`, caution `#C4920A`).

Everything is self-contained under `js/variants/E/**` + `css/variants/E/**`;
the data-viz (`svg.js`), drill-down read layer (`data.js`), and chrome
helpers (`ui.js`) are copied and adapted into the E namespace so the variant
does not import from the (losing, later-deleted) A–D namespaces. Only the
shared `js/core/*` data layer is reused untouched.

## The screens

Navigation is A's hierarchical breadcrumb IA over five Atlas screens, with a
calm top bar (branding · breadcrumb · primary nav · connection status pill).

- **Home / Environment** (`#/E/`) — the workspace as a fleet. A cross-epoch
  overview strip (epochs / generations / best scalar / phase), then one
  console **fleet card per epoch** carrying the per-epoch loss **trendline**
  hero (A's loved element), loop-health, and a cross-epoch trajectory
  sparkline.
- **Epoch** (`#/E/epoch`) — the data substrate of one epoch: the objective
  banner + collapsible proposer brief, then D's **lineage bumps**, the
  **entries × generation drift-loss heatmap**, and the **board trellis**
  (small multiples, one micro sparkbar + pass/fail dot-row per board entry).
- **Candidate** (`#/E/candidate/<gen>[/<entry>]`) — one generation's life:
  D's **per-board value dot-plot** (absolute drift loss, sorted worst-first,
  champion reference line) alongside a compact **C-style lifecycle DAG**.
  Selecting an entry (in the URL) drills into its expectation outcomes +
  per-judge weighted losses, with a link into the full run transcript.
- **Match-ups** (`#/E/matchups`) — D's **gauntlet bumps ladder**, the
  per-round **paired slopegraphs** (champion → challenger per board entry),
  and the **illustrative alternative tournament styles** (single-elim
  bracket, round-robin matrix, racing / successive-halving lanes), clearly
  labelled illustrative because only the gauntlet has real per-round data.
- **Run** (`#/E/run/<gen>/<entry>`) — the run detail: the reconstructed
  transcript (turns, tool calls, drift annotations).

## Render discipline (the recurring flashing bugs are designed out)

The shell (`shell.js`) mirrors the v2 digest-gate blueprint:

1. **Digest-gated repaints.** Every view computes a stable digest of ONLY
   structural/content data (epoch ids, scalars, losses, verdicts, transcript
   turns) — heartbeat timestamps are excluded — and renders through
   `ui.gatedSwap(host, digest, build)`. If the digest equals the host's
   recorded `data-e-digest` and the host still has children, NOTHING is
   written. A steady heartbeat that re-dispatches the active view is a true
   no-op, so nothing flashes.
2. **View-switch clear.** On a view change (`route.view` changes) the shell
   clears the one persistent content host before the incoming view renders,
   so a digest-gated view never wrongly skips its first paint.
3. **One persistent host, debounced re-render.** `state:changed` is debounced
   and routed through the active view's own digest-gated render; the content
   host is reused, never recreated.
4. **Cache invalidation only on user action.** The drill-down cache is busted
   only on a view change (a user navigation) — never on a heartbeat.
5. **URL-driven drill-down selection.** The selected board entry lives in the
   route param, so the drill-down DOM rebuilds only when the selection
   changes (the digest carries the entry id), never on a heartbeat.
6. **Hover via CSS `transition`, never infinite keyframes** — no animation
   re-fires on insertion.
7. **Log/transcript tails in normal block flow** inside a single constrained,
   scrollable container (`.e-transcript`: `max-height` + `overflow-y:auto` +
   `min-height:0` flex column) — no absolute positioning, so lines never
   overlap.
8. **Deep-link hydration.** Every route fetches its own data on a cold load
   from the URL params. The run view resolves the `run_id` from
   `/api/generation/.../per-entry`, then fetches `/api/conversation/{run_id}`
   itself: loading → content, never an empty panel.

## Tests

`test/variant_e.test.mjs` pins the router (A-style IA + breadcrumb), the
`gatedSwap` no-flash helper, the compact lifecycle DAG, and the views against
the live fixture (one epoch `2026-05-30_e0`; v0 crowned; v1/v2 rejected; all
board entries fail). It includes the two mandated guarantees: a digest-gated
no-op (a second render with identical data does not rebuild the DOM) and a
cold deep-link run/transcript render that asserts transcript content paints.
