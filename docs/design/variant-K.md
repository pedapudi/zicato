# Variant K — "Monograph"

Report-first convergence dashboard for zicato. The ACM-style **epoch
publication is the home and primary surface**: the analyzer's narrative is
typeset as a paper, and the dashboard's live Tufte figures are embedded inline
as the paper's plates. Each figure is clickable and drills into the live
dashboard views (which retain Variant E's flow/IA). The paper reads
top-to-bottom; the figures make it live.

Selected behind `?ui=K` (already wired in `index.html`). Self-contained under
`js/variants/K/**` + `css/variants/K/**` + the entry `app_K.js`. Reuses the
shared `js/core/*` data spine; imports nothing from another variant dir.

## Identity

- **The paper is the centerpiece, not a tab.** `/api/epoch/{e}/analysis`'s
  `analysis_md` is parsed (`paper.js`) into eyebrow / title / masthead-meta /
  abstract / body, reusing the section-marker approach of
  `js/v2/views/report.js`, and rendered to DOM (the standalone `/analysis.html`
  may 404, so we never depend on it). Typeset solarized-light by default,
  paper-like, with a serif display voice and a constrained measure.
- **Figures are live.** Wherever the markdown carries a `<!-- FIGURE:name -->`
  marker, a live interactive Tufte figure is spliced in at that exact position.
  When the analyzer emitted no markers (older epochs / not-yet-built reports),
  a canonical figure gallery is embedded after the abstract instead — so the
  paper always reads as a living document.
- **Drill-down retains E's IA.** Every figure click routes into a live view:
  lineage/heatmap/sankey → candidate or run; the duel slopegraph → match-ups;
  the methods figure → mutation sites. Breadcrumbs root everything at the paper.

## Routes (`router.js`, prefix `#/K/`)

| route | view |
| --- | --- |
| `#/K/` · `#/K/paper/<epoch>` | the paper (HOME) |
| `#/K/candidate/<gen>[/<entry>]` | per-board scoring + entry drill |
| `#/K/matchups[/<champ>/<chall>]` | gauntlet ladder + paired duels + gate |
| `#/K/mutations[/<gen>]` | mutation-site × generation matrix + patch diffs |
| `#/K/run/<gen>/<entry>` | the reconstructed transcript |

A missing/foreign hash returns the paper, so a deep-link or stale hash never
lands blank.

## Screens / sections

- **Paper (home, `views/home.js` + `paper.js`).** Masthead (eyebrow · title ·
  rule · meta cells) → abstract lede → body with figures spliced at their
  markers. Honest not-yet / broken states still embed the live figure gallery.
- **Candidate (`views/candidate.js`).** Per-board **value dot-plot** (loss,
  pass/fail/timeout glyphs, optional champion reference rule), drilling into one
  entry's expectation outcomes + per-judge loss + a themed "open full
  transcript" link.
- **Match-ups (`views/matchups.js`).** The gauntlet **ladder** (lineage bumps),
  per-round **paired slopegraphs** (champion → challenger per board entry), and
  the **promote-gate** decomposition (short-circuiting rules + scalar-component
  delta dot-plot) for the focused round.
- **Mutation sites (`views/mutations.js`).** The NEW view: a **mutation-site ×
  generation matrix** (`/api/mutations/{e}`) — a filled cell where a generation
  patched a site — drilling into that generation's patch diffs
  (`/api/files/{e}/{g}/patches`).
- **Run (`views/run.js`).** The transcript, cold-deep-link capable: resolves
  `run_id` from per-entry then fetches `/api/conversation/{run_id}`; constrained
  single-scroll container; honest empty when no run id.

## Figures (`svg.js`, all fit-to-width, NO viewport)

- **Lineage bumps** — champion spine + challenger lane; coincident challengers
  (same depth off one parent — F's collision) are de-collided in x; nodes are
  clickable → candidate.
- **Tufte Sankey** — candidate → per-board loss → aggregate scalar, laid out to
  fit the container (responsive `viewBox` + `preserveAspectRatio`), thin flows
  scaled by loss, direct labels, no pan/zoom surface; board nodes click → run.
- **Paired slopegraph** — de-collided labels + jittered coincident nodes,
  direct-labelled, coloured by verdict; line click → run.
- **Board × generation heatmap** — drift-loss ramp from theme tokens; cell
  click → run.
- **Per-board dot-plot** — high contrast in all three themes (token-bound
  classes, not hard-coded fills).
- **Mutation matrix** — boolean presence/absence cells; on-cells click → patch
  diff.

## Three-theme system (`ui.js` + `monograph.css`)

`solarized-light` (default), `solarized-dark`, `monokai`, with a visible
switcher in the top bar. The pick persists to `localStorage` and is applied as
a `data-vk-theme` attribute on the variant root; a theme switch is CSS-only and
never rebuilds a view (the figures re-skin via `--vk-*` tokens). Every mark and
figure reads in all three; the improve/regress/caution semantics hold across
themes.

## Render discipline (follows E; `js/v2/shell.js`)

- One persistent content host; cleared on a view switch, reused otherwise.
- Every view digest-gates its repaint on **structural data only** (timestamps /
  heartbeat excluded) via `gatedSwap` — a steady heartbeat re-dispatch writes
  zero DOM.
- Drill-down caches invalidate only on a route change, never on a heartbeat.
- Cold deep-link routes fetch their own data (transcript via
  `/api/conversation/{run_id}`), with honest fallbacks.
- Motion via CSS `transition`, never `animation:…infinite`; event tails are
  constrained-scroll containers.

## Bug fixes carried in (do not reproduce)

- E's unstyled "open transcript" anchor → a themed `linkButton`.
- F's colliding + dead lineage nodes → de-collided + each node click wired to
  the candidate.
- G's un-scaled Sankey → fit-to-width Tufte Sankey; per-board scoring readable
  in all three themes.

## Tests

`test/variant_k.test.mjs` (18 cases): the ACM report is the home + renders from
`analysis_md`; embedded figures render + are clickable (drill to a live view);
digest no-op; cold deep-link transcript; mutation-per-gen matrix + patch drill;
Sankey fit-to-width (responsive viewBox, no viewport); lineage
non-colliding + clickable; three-theme switch. Green; full suite 0 failures.
