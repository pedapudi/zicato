# Dashboard variant bake-off — the durable record

> **Status: a historical record of a completed design bake-off.** It keeps its
> chronology on purpose. For what the dashboard does today, read
> [CONSOLE-DESIGN-LANGUAGE.md](CONSOLE-DESIGN-LANGUAGE.md).
>
> Six rounds of design work produced 23 candidate dashboards, lettered **A**
> through **W**. **T** won and is the console that ships. This document
> is the durable record of the field: a catalogue of the visual elements the
> variants shared (Part 1) and the look and feel of each one (Part 2). It was
> written so that the variant code, which lived under
> `src/zicato/dashboard/static/js/variants/<LETTER>/` and
> `css/variants/<LETTER>/`, and the per-variant `docs/design/variant-<LETTER>.md`
> notes could be archived without losing the design history.
>
> Each variant was a self-contained front end reached at runtime with
> `?ui=<LETTER>` and painted into `#variant-root`, reusing only the shared data
> layer (`js/core/{api,sse,state,dom,format,bus}.js`) and the real `/api/*`
> endpoints. Nothing here is invented data; every mark binds to a field one
> of those endpoints actually serves.

## Retirement record

- **2026-06-01 — bake-off field archived.** Variants A through W were
  removed from `main` and preserved at the git tag
  `dashboard-bakeoff-2026-06-01`. T stayed on `main` as the
  converged, default front end (`app_T.js` plus `js/variants/T/` plus
  `css/variants/T/console4.css`).
- **2026-06-02 — the two fallback shells retired.** Two pre-bake-off front
  ends remained in the tree: a clean-slate shell (`app.js`,
  `js/views/phase0_*.js`, `css/phase0_*.css`) and the two-mode shell
  specified in [DASHBOARD-V2.md](DASHBOARD-V2.md) (`app2.js`, `js/v2/`,
  `css/v2/`). Both were removed from `main` and preserved at the git tag
  `dashboard-v1-v2-archive-2026-06-02`. The `?ui=v1|v2` bootstrap branches
  and both shells' static markup were dropped from `index.html`. T became
  the sole shipping front end, with no fallback shell in the tree.

## The six rounds at a glance

| Round | Variants | Theme |
| --- | --- | --- |
| 1 — explorations | A · B · C · D | Mission Control · Editorial Lab Notebook · Causal Flow · Tufte Data-viz |
| 2 — syntheses | E · F · G | Atlas · Current · Bridge (built from the best of A–D) |
| 3 — convergence | H · I · J · K | Atlas II · Ledger · Console · Monograph (Tufte + 3-theme system + mutation-per-gen + ACM publication) |
| 4 — convergence II | L · M · N · O | Atlas III · Ledger II · Console II · Compass (combined mutation+side-by-side diff · per-board cross-candidate view · typeface picker) |
| 5 — convergence III | P · Q · R · S | Console III · Atlas IV · Strata · Lens (all on a data-model TREE sidebar) |
| 6 — convergence IV | **T** · U · V · W | **Console IV (anchor/winner)** · Atlas V · Reel · Arena |

Most variants carry a design-line name. Where a later round refined an earlier
variant of the same line, a roman numeral marks the iteration; the dense-console
line runs Console (J), Console II (N), Console III (P), and Console IV (T).

The judgements that shaped the convergence, in order:
- A's hierarchical breadcrumb information architecture and its "Fleet"
  epoch-trendline card tested well.
- E's flow was confirmed "likely fine" and became the information-architecture
  base for rounds 3 and 4.
- B's three-theme color system and D's Tufte toolkit were the most-liked visuals.
- K's ACM publication renderer was judged the best of round 3 (but the
  paper-first arrangement was rejected, and the renderer became a tab rather
  than the home surface).
- N (Console II) was judged the most appealing base for round 5.
- M's spacing/proportion and L's mutation-viewer quality were singled out as good.
- P (Console III) was judged the best-looking console and became the round-6
  anchor; S's side-by-side compare and Q's spacing were folded into it.
- **T** is the converged anchor and the console that ships.

---

# Part 1 — Shared visual-element catalogue

Many elements recur across variants. Each is catalogued once below as a
reusable component: what it encodes, the API data it binds, and which variants
use it. Each re-implementation was copied and adapted into that variant's own
namespace rather than imported across variants, so "uses it" means the element
appears in that variant rather than that the variants share code.

### Data-model tree sidebar
- **Encodes:** the real zicato hierarchy as a persistent, expandable/collapsible
  left tree that drives a single detail pane — `Environment (workspace) → Epoch
  <id> → {Generations → <gen>, Boards → <entry>, Mutation surface, Publication}`.
  Multi-epoch AND multi-generation; selection explicit + URL-encoded so a cold
  deep-link hydrates both the open branches and the detail pane.
- **Binds:** `/api/workspace` + `/api/lineage` + `/api/epoch.board` to assemble
  the structural model; each leaf routes to its detail endpoint.
- **Used by:** P, Q, S, T, U, V, W. (The round-5 headline; carried into round 6.)

### Miller-columns navigation (tree variant)
- **Encodes:** the same data-model hierarchy as cascading macOS-Finder-style
  columns — `Environment ▸ Epoch sections ▸ items ▸ detail` — each selection
  driving the next column; the whole column path is URL-encoded.
- **Binds:** same structural model as the tree sidebar.
- **Used by:** R only (back-burnered after round 5; not pursued in round 6).

### Master-detail selector rail (tree variant)
- **Encodes:** a fixed left selector rail ordered strictly by scope (workspace →
  epoch → generations + board entries), right detail pane; selection explicit +
  URL-encoded. A precursor to the round-5 tree sidebar.
- **Binds:** `/api/workspace`, `/api/lineage`, `/api/epoch.board`.
- **Used by:** O.

### Breadcrumb + back/up control
- **Encodes:** the reader's position in the hierarchy and the way back out of
  it. A live breadcrumb rooted at the environment, plus a top-left back/up
  control that walks up the selection hierarchy (entry/compare → candidate →
  generations → epoch → environment). The control renders the destination into
  the MAIN detail pane and never into the sidebar, which is the fix for Q's
  bug, where the destination rendered into the side panel.
- **Binds:** route/selection state only.
- **Used by:** breadcrumb — A, B, C, E, F, G, H, I, J, K, and the
  information-architecture base of rounds 3 and 4. Back/up control — T, U, V,
  W, from the round-6 fix. Q shipped the buggy version.

### Color-theme system (a 6-role token contract; 16 themes in T)
- **Encodes:** B's themeable token system, ported and grown into a **six-role
  semantic contract** — `--v2-paper / --v2-panel / --v2-ink / --v2-good /
  --v2-bad / --v2-accent` (plus secondary `--v2-caution/flat/ink-soft/ink-faint/
  rule/cell-empty`) — swapped via a `data-…-theme` attribute on the variant root
  (CSS-only re-skin, persisted to `localStorage`). The improve/regress/accent
  semantics hold across every theme; no mark carries hardcoded hex. In the
  shipping front end **(T)** this grew from three themes to **sixteen**, with
  monokai as the default. The three originals are monokai, solarized-dark and
  solarized-light. Thirteen more are adapted from the Gogh terminal colour
  schemes (gogh-co.github.io/Gogh): google-light, google-dark, lunaria-light,
  lunaria-eclipse, belafonte-day, belafonte-night, paper, zenburn,
  selenized-black, relaxed, espresso, dracula and ubuntu. Each maps onto the
  same six-role contract. With sixteen options the picker became a **swatch
  dropdown** — a six-swatch preview strip and a name per option — rather than
  a row of inline buttons.
- **Binds:** none (presentation tokens).
- **Used by:** B (the origin — Paper/Ink/Sepia), then the token system across H,
  I, J, K, L, M, N, O, P, Q, R, S, T, U, V, W. (A and D ship bespoke palettes; D
  has a `prefers-color-scheme` dark mode.)
- **Source of truth:** the full role system + the sixteen themes are documented
  in [CONSOLE-DESIGN-LANGUAGE.md](CONSOLE-DESIGN-LANGUAGE.md) §2.

### Typeface themes (three distinct serif / mono / display voices)
- **Encodes:** a second chrome picker swapping the family tokens (`--v2-sans` /
  `--v2-mono`, plus the `--n-font-head` / `--n-font-paper` families) via a
  `data-…-type` root attribute (persisted). In the shipping front end **(T)**
  the picker offers **three distinct voices**, defaulting to **Technical**, rather
  than variations on one sans-serif family. **Editorial** is Source Serif 4
  throughout — body, data, headings and publication. **Technical** is an
  all-monospace mixture: iA Writer Mono for prose body, headings and
  publication, with JetBrains Mono for data, labels and code. **Display**
  pairs a Space Grotesk geometric body with Archivo Narrow condensed headings
  and big numerals. A fourth **Sans** option was dropped as redundant, and
  `sans` normalises to Technical. Google Fonts is the only
  permitted external dependency — fonts only, system fallbacks, `display=swap`.
- **Binds:** none (presentation tokens).
- **Used by:** L, M, N, O, P, Q, R, S, T, U, V, W. (Introduced in round 4.)
- **Source of truth:** the typography system is documented in
  [CONSOLE-DESIGN-LANGUAGE.md](CONSOLE-DESIGN-LANGUAGE.md) §3.

### Density / "roominess" picker
- **Encodes:** a third chrome selector (compact · cozy · roomy) driving a root
  `data-…-density` attribute that swaps spacing/size custom properties (rail
  width, detail padding, section/row/card gaps, font scale, reel scale), so the
  whole UI re-breathes with a pure CSS swap (no re-render). Persisted.
- **Binds:** none (presentation tokens).
- **Used by:** T only, added to the anchor after the six rounds closed.

### Lineage bumps
- **Encodes:** the lineage as a non-colliding bumps chart — the champion spine
  gets its OWN lane, rejected challengers branch into a distinct lower lane.
  This fixes the collision in the pre-bake-off dashboard, where champion and
  challenger shared a row.
  Coincident challengers (e.g. v1/v2 off one parent) are de-collided in x; every
  node is clickable → its candidate/experiment. Tufte alternative to a pannable
  lineage DAG.
- **Binds:** `/api/lineage`, `/api/score-trajectory`.
- **Used by:** D (origin), E, F, G, H, I, J, K, L, M, N, P, Q, S, U, V, W.
  (T replaced bumps on its epoch view with the Reel.)

### The Reel (rounds on a time axis)
- **Encodes:** the epoch as a horizontal, fit-to-width timeline/film-strip — the
  champion spine along a time axis, station 0 the seed/champion (♛), one tick or
  station per round carrying the challenger id, verdict-colored dot, and Δscalar.
  In V it is the hero AND the navigator (a scrubber/stepper + pip per station; a
  station opens that round's match-up + gate + lifecycle). In T it is a *slim*
  reel that replaces the lineage bumps on the epoch overview. Fixed viewBox at
  `width:100%`, ticks compress as rounds grow — no pan/zoom, no horizontal scroll.
- **Binds:** `/api/tournaments`.matchups (round order via `ran_at`) + `/api/lineage`.
- **Used by:** V (hero), T (slim, on the epoch view).

### Tufte causal-flow Sankey (patch → drift → gate / candidate → board → aggregate)
- **Encodes:** the causal sentence as flow. Two layouts share the engine:
  patch → drift-kinds-that-moved → gate verdict (ribbon width = movement
  magnitude), and candidate → per-board loss → aggregate scalar (band width =
  each board's contribution). Round 3 redrew it Tufte-style — fit-to-container
  width, NO pan/zoom viewport, thin stroked flows, direct in-place labels,
  restrained improve/regress color. Round 4 fixed label≠value (the per-board
  node's loss value gets its own right-aligned baseline, never overprinting the
  label).
- **Binds:** `/api/files/{e}/{g}/patches` + `/api/drift-movements/{g}` (cause/
  effect), `/api/generation/{e}/{g}/per-entry` (per-board loss), `/api/round/.../gate`.
- **Used by:** C (origin, pannable), F, G (pannable → flagged); fit-to-width
  Tufte version in H, I, J, K, L, M, N. (Q/S/T carry the fit-to-width version on
  the match-ups/candidate surfaces.)

### Drift heatmap (board entry × generation)
- **Encodes:** a compact matrix of per-board drift loss across generations; cell
  color = loss. Round 4 made the ramp **theme-aware** — derived from the active
  color theme's tokens at draw time (or themed ink at value-driven opacity for
  Monokai), no fixed orange/brown ramp. Round 5 de-duplicated it against the
  trellis: the heatmap stays at the EPOCH overview.
- **Binds:** `/api/generation/{e}/{g}/per-entry` pivoted by entry × generation.
- **Used by:** A, D, E, G, H, I, J, K, L, M, N, O, P, Q, R, S, U, V, W.

### Board trellis (small multiples)
- **Encodes:** one micro-chart per board entry (not a table of rows) — the
  entry's kind/budget/weight/tags + a sparkbar of its loss across generations on
  a shared scale + a row of pass/fail/timeout glyphs; sorted meaningfully (by
  kind, then weight, then id). Round 5 de-duplicated it against the heatmap: the
  trellis moves into the BOARDS view.
- **Binds:** `/api/epoch.board` + `/api/generation/{e}/{g}/per-entry`.
- **Used by:** D (origin, "Boards"), E, H, I, J, K, L, M, N (epoch); P, Q, R, S,
  T, U, V, W (Boards view).

### Per-board dot-plot (sorted vs champion)
- **Encodes:** one candidate's absolute per-entry drift loss as a sorted
  value dot-plot (lower = left = better) with a reference line at the champion's
  scalar; dots left of it read improve-colored, right read regress-colored;
  pass/fail/timeout glyph trails each row. Depth 1 of the per-board drill-down.
- **Binds:** `/api/generation/{e}/{g}/per-entry`.
- **Used by:** D (origin), E, G, H, I, J, K, L, M, N, O, P, Q, R, S, T, U, V, W.

### Paired slopegraphs (per-round matchup duels)
- **Encodes:** the heart of one round — the paired (common-random-number)
  per-board duel as champion-loss → challenger-loss slopes, one line per board
  entry, colored by verdict (improved/regressed/flat). Defeats collision three
  ways: per-column label de-collision with hairline leaders to the true datum,
  node jitter for coincident values, and direct end-labelling.
- **Binds:** `/api/matchup-grid/{e}/{champ}/{chall}` (`entry_grid` with
  `parent_drift_loss`/`child_drift_loss`/`delta`/`verdict`/`won_by`).
- **Used by:** B, D (origin), E, F, G, H, I, J, K, L, M, N, O, Q, R (and as the
  per-matchup figure embedded in the publication).

### King-of-the-hill gauntlet ladder
- **Encodes:** the REAL tournament mechanism — one reigning champion per epoch
  defending, each round a rung/ladder step (champion vs challenger, decision,
  Δscalar, hypothesis); expanding a rung loads the paired per-board duel. Marked
  as the real data (no "illustrative" badge).
- **Binds:** `/api/tournaments` (`champion_lineage` + `matchups[]`),
  `/api/matchup-grid/...`.
- **Used by:** A, B, C, D, E, F, G, H, I, J, K (and within match-ups views of
  L–W).

### Match cards / standings (champion-defends)
- **Encodes:** the tournament as live standings. A champion "defending the
  title" banner carries champion id · loss · defence count · promoted pill.
  Beneath it a responsive wrapping grid holds one match card per challenger
  round: challenger vs champion · verdict pill · signed Δscalar · one-line
  truncated hypothesis · decisive-driver judge.
  The standings double as navigation; cards stay short
  and the grid wraps, scaling to many generations.
- **Binds:** `/api/tournaments` + `/api/lineage` + `/api/round/.../gate` (the
  decisive-driver line) + `/api/score-trajectory` (season sparkline).
- **Used by:** W (the Arena hero), T (compact match cards on the generations page).

### Promote-gate ladder (stacked rules + scalar-components)
- **Encodes:** the decisive gate moment as three stacked sub-blocks, fixing
  K's overlapping layout. (a) A decision pill with the change in scalar and
  pass rate and the primary driver. (b) The rules ladder — three
  short-circuiting rules in order (scalar-margin → pass-rate-monotonicity →
  namespace-monotonicity), each on its own row carrying label, status and
  detail; a fired rule stops evaluation and later rules read `not_reached`.
  (c) A separate champion-against-challenger comparison of the scalar
  components, decomposing the loss into cost, drift, latency, output, pass,
  rubric and schema.
- **Binds:** `/api/round/{e}/{champ}/{chall}/gate` (`decision`, `delta_scalar`,
  `delta_pass_rate`, `rules[]`, `scalar_components`, `primary_driver`). (A
  reconstructs the verdict client-side from the experiment outcome where the gate
  route is absent.)
- **Used by:** the stacked gate — K, L, M, N, O, P, Q, R, S, T, U, V, W; gate as
  a go/no-go panel — A, C, D, F, G.

### Lifecycle DAG (cause → effect → verdict)
- **Encodes:** one candidate's whole life as a left-to-right DAG —
  `PARENT → PATCH → board fan → AGGREGATE → GATE → TERMINAL` (crowned champion or
  dead branch). Each board-fan node's radius/color encodes its drift loss; the
  edge into the aggregate is weighted by that entry's contribution. In round 3+
  it is rendered compactly as a static fit-to-width figure (no viewport).
- **Binds:** `/api/lineage` (parent), `/api/files/{e}/{g}/patches` (patch),
  `/api/generation/{e}/{g}/per-entry` (board fan), `/api/round/.../gate` (verdict).
- **Used by:** C (origin), E, F, G, H, I, J, K, L, M, N, O, P, Q, R, S, T, U, V, W.
  (B uses a margin timeline + genealogy figure as its lifecycle idiom; A uses a
  "mission track" + command roster.)

### Mutation surface matrix + side-by-side diff
- **Encodes:** the mutation surface as a **site × generation matrix** (rows =
  `file:line` + role, columns = generations; a filled cell = that generation
  patched that site). Round 4 made it ONE cohesive layout: the matrix + a detail
  pane that fills on cell-select with a **side-by-side** line-diff (two columns:
  champion baseline | challenger new). The diff uses REAL strings — baseline from
  `.baseline.content` (never the `.baseline` object — the "[object Object]" bug),
  challenger from the matching `patches[].new_content` — with a full-file `/diff`
  fallback.
- **Binds:** `/api/mutations/{epoch}` (matrix), `/api/mutations/{e}/{mutation_id}`
  → `.baseline.content`, `/api/files/{e}/{g}/patches` → `.new_content`,
  `/api/files/{e}/{g}/diff` (fallback).
- **Used by:** H, I, J, K (matrix + patch-diff drill); the combined side-by-side
  version in L, M, N, O, P, Q, R, S, T, U, V, W. Round 5 added a per-candidate
  diff reached from the lifecycle's clickable PATCH node (P, Q, R, S, T, U, V, W).

### ACM publication renderer
- **Encodes:** the analyzer's epoch narrative typeset as an ACM-style paper —
  eyebrow / title / masthead-meta / abstract / numbered sections — parsed from
  section markers (`<!-- EYEBROW -->`, `<!-- META -->`, `## Abstract`,
  `<!-- FIGURE:name -->`, `<!-- CALLOUT:... -->`) into DOM (never `innerHTML`).
  The dashboard's own live Tufte figures (lineage bumps, matchup slopegraph,
  drift heatmap) splice in at the figure markers. Round 4 fixed it: GFM tables
  render as real `<table>`s; the aggregate-generation-scores table + summary bar
  chart combine into ONE figure; per-matchup detail is appended. K's renderer was
  judged the best and was reused thereafter.
- **Binds:** `/api/epoch/{epoch}/analysis` → `analysis_md` (the
  `/analysis.html` route may 404 and is never depended on).
- **Used by:** K (origin, the home), H, I, J (as a view); reused as an
  epoch-scoped TAB in L, M, N, O, P, Q, R, S, T, U, V, W.

### Tournament-style topologies (gauntlet / single-elim / double-elim / Swiss / racing)
- **Encodes:** the SAME candidate set re-laid-out under each documented selection
  structure, each a DIFFERENT topology — gauntlet (star/hub or ladder; the only
  REAL data), single-elimination bracket tree, double-elimination winners'/losers'
  brackets, Swiss pairing table/round-robin matrix, racing/successive-halving
  race lanes with an elimination cut-line. The non-gauntlet styles are honest
  CONCEPTUAL overlays (clearly badged "illustrative — not how zicato ran this
  epoch"); only the gauntlet carries real per-round data.
- **Binds:** real — `/api/tournaments` + `/api/matchup-grid/...`; alternatives —
  illustrative overlays on the same generation set (no invented per-round data).
- **Used by:** A, B, C, D, E, F, G, H, I, J, K (the round-3-and-earlier match-ups
  showcase). The later tree-based variants kept the real gauntlet/match-ups but
  dropped the illustrative topology gallery to stay lean.

### Per-board cross-candidate view
- **Encodes:** a dedicated page for ONE board entry showing how EVERY candidate
  performed on it — per-candidate loss + pass/fail/timeout, a sorted comparative
  chart (dot-plot/bars, champion reference), and a drill to each candidate's run
  for that board. Keyed by ENTRY ID — board trellis cards AND heatmap cells route
  HERE (fixing the bug where a trellis click dumped the user on an arbitrary
  candidate). Introduced in round 4.
- **Binds:** `/api/generation/{e}/{g}/per-entry` pivoted by `entry_id` across
  generations; `/api/matchup-grid/...` for paired context; `run_id` →
  `/api/conversation/{run_id}` to drill.
- **Used by:** L, M, N, O, P, Q, R, S, T, U, V, W.

### Inline side-by-side transcript
- **Encodes:** selecting a run on the board view shows the transcript INLINE
  within the board view — ideally two candidates' transcripts (champion vs a
  challenger) in two independently-scrollable columns — NOT a navigation to a
  separate run page (the run route is dropped). Introduced in round 5.
- **Binds:** `/api/conversation/{run_id}` per candidate (run ids resolved from
  per-entry).
- **Used by:** P, Q, R, S, T, U, V, W. (S "Lens" generalized it into a
  first-class compare model across the whole detail pane.)

### Side-by-side compare model (whole-pane)
- **Encodes:** a "compare with…" affordance that splits the SAME detail pane
  into two candidate panels A | B (lifecycle · gate · match-ups · per-board
  scoring), each in its own digest-gated host. The compare target rides in
  the hash (`~cmp=<gen>` / `~runs=<a>,<b>`) so it deep-links. Never
  navigates away.
- **Binds:** the per-candidate endpoints, twice (once per side).
- **Used by:** S (origin), T, U, V, W (folded into the round-6 anchor and siblings).

### Proposer-brief home (safe markdown)
- **Encodes:** the operator's full, possibly-long brief to the proposer rendered
  as readable prose (headings, lists, fenced code) via a tiny XSS-safe
  markdown→DOM renderer (no `innerHTML`), with a one-line distilled goal set
  prominently above it; a long brief starts collapsed, a short one open. Honest
  "no brief recorded" state.
- **Binds:** `/api/epoch` — the `brief` field, the older `rubric.md` field
  name it falls back to, and `goal`.
- **Used by:** every variant carries it on the epoch surface (A–W).

### Fleet / environment overview
- **Encodes:** the whole workspace at once — a cross-epoch overview stat strip
  (epochs / generations / best scalar / phase), one console "fleet card" per
  epoch with a per-epoch loss **trendline** hero (A's loved element), loop-health
  findings, and a cross-epoch best-scalar trajectory.
- **Binds:** `/api/workspace`, `/api/score-trajectory`, `/api/health-report`,
  `/api/environment`.
- **Used by:** A (origin), E, F, G, H, I, J, L, M, N, O, P, Q, R, S, T, U, V (and
  W closes its Arena page with a season trajectory sparkline).

### Render-discipline spine (shared, non-visual)
- A single persistent content host per pane. Every view is **digest-gated** on
  structural data alone, with timestamps and the heartbeat excluded, so a
  steady heartbeat tick rebuilds zero DOM. The host is cleared only on a view
  or selection change, which also invalidates the drill-down caches; a
  heartbeat never clears it. Selection is URL-driven, so a cold deep-link
  hydrates its own data. Hover and theme swaps use CSS `transition` and never
  `animation:…infinite`. Transcript and event tails scroll inside a constraint
  rather than using absolute-positioned rows. No diagram carries a pan or zoom
  viewport; every one fits to width.
- This blueprint (mirrored from `js/v2/shell.js`) carries from E onward and is the
  reason the recurring flashing / stale-view / cold-deep-link / colliding-marks
  bugs are designed out across the convergence.

---

# Part 2 — Per-variant look-and-feel

One section per variant, twenty-three in all. Each gives the variant's
identity, its default colour and typeface theme, its signature elements, its
navigation model, and a one-line statement of what set it apart.

## Round 1 — explorations (A–D)

### A — "Mission Control"
- **Identity:** a dark live-ops console — NASA mission control crossed with a
  trading terminal; big confident monospace readouts with semantic glow, status
  lights, motion only where something is live.
- **Theme:** bespoke dark palette (default; the point of the variant).
- **Signature:** the gauntlet as a clean-lane bracket; the sortie-board
  status-light tile grid; the verdict-first telemetry experiment; the match-up
  "theatre" with a style switcher.
- **Navigation:** hash router under `#/A/` with a persistent shell + breadcrumb +
  ⌘K command palette.
- **Distinct:** instruments rather than tables — the loop read as flight
  telemetry.

### B — "Editorial Lab Notebook"
- **Identity:** the opposite of a database browser — zicato's science
  presented as a research magazine or lab notebook; serif display voice,
  generous whitespace, figures and prose, no tables.
- **Theme:** light-first **Paper** (default), dark **Ink**, warm **Sepia** — the
  origin of the three-theme token system. Serif display + calm sans + mono.
- **Signature:** the bet as a pull-quote; charts-as-figures (slopegraph, diverging
  bars, genealogy tree with a crowned champion); the brief with a table-of-
  contents rail + collapsible sections.
- **Navigation:** hash router `#/B/`, masthead + crumbs.
- **Distinct:** every experiment reads as a beautifully typeset notebook page.

### C — "Causal Flow / Diagram-first"
- **Identity:** zicato as a causal machine; every hero screen is an interactive
  diagram, a node graph or a Sankey, rather than a table; everything flows
  left to right, cause → effect → verdict.
- **Theme:** semantic `--v2-*` token language (cause violet, improve teal,
  worsen amber, promote green, reject red, live blue).
- **Signature:** the patch → drift → gate **Sankey** (the origin); the lifecycle
  DAG; the cross-epoch node-graph; the five-topology match-up switcher. Pan/zoom
  surface (later flagged and dropped).
- **Navigation:** hash router `#/C/`, persistent chrome + drawer.
- **Distinct:** the causality shown as flow — the origin of the Sankey and DAG.

### D — "Tufte data-viz"
- **Identity:** a beautiful interactive Tufte poster — high data-ink, minimal
  chrome, small multiples everywhere; every number with a trend is a tiny chart.
- **Theme:** a refined calm `--v2-*` palette (warm paper, teal/rose accents,
  terracotta champion spine); dark mode via `prefers-color-scheme`.
- **Signature:** the data-viz TOOLKIT itself (sparklines + range-frames, the
  non-colliding bumps, the de-collided slopegraph done right, the heatmap, the
  board trellis, the per-board value dot-plot) — the origin of nearly every
  later Tufte mark.
- **Navigation:** hash router `#/D/`.
- **Distinct:** the slopegraph and bumps "done right" — de-collision the rest of
  the field reused.

## Round 2 — syntheses (E–G)

### E — "Atlas"
- **Identity:** a data-first observatory; numbers are the interface, diagrams on
  demand. Combines A's IA + Fleet card, D's Tufte toolkit, C's compact lifecycle
  DAG, B/D's restraint.
- **Theme:** **Solarized-Dark** (default).
- **Signature:** the five-screen Atlas flow (the IA confirmed "likely fine" and
  reused as the base for rounds 3–4); the digest-gated render spine that designs
  out the flashing bugs.
- **Navigation:** A's hierarchical breadcrumb IA, `#/E/`.
- **Distinct:** the calm data-first base the whole convergence built on.

### F — "Current"
- **Identity:** causal-narrative, flow-first — the patch → drift → gate causality
  and lifecycle DAG as the hero of every candidate/epoch; data-viz as inline
  evidence; editorial typographic voice.
- **Theme:** **Solarized-Dark** (default), airy serif headlines.
- **Signature:** C's diagrams + D's evidence + B's pull-quotes, unified.
- **Navigation:** A's breadcrumb IA, `#/F/`.
- **Distinct:** the causal story leads; the numbers are the evidence behind it.

### G — "Bridge": A done right
- **Identity:** A's command-center navigation rebuilt right — keep the IA and
  Fleet home that worked, replace every dense/ugly element with calmer data-viz,
  fix all four A bugs.
- **Theme:** **Solarized-Dark** token system (vs A's loud console).
- **Signature:** the four named A-bug fixes proven by test; A's IA over D's marks.
- **Navigation:** A's IA, rebound under `#/G/`.
- **Distinct:** the navigation that worked, with the visuals that did not fight.

## Round 3 — convergence (H–K)

### H — "Atlas II"
- **Identity:** the conservative refinement of E — keep E's flow verbatim, fix
  the named bugs, add the three-theme switcher, add the two missing views, enforce
  fit-to-width Tufte.
- **Theme:** **Solarized-Dark** (default); the named three-theme system.
- **Signature:** the fit-to-width Tufte Sankey; the mutation-site × generation
  matrix with its patch-diff drill, introduced in this round; the ACM report
  view with inline live figures, also introduced in this round.
- **Navigation:** E's breadcrumb IA, `#/H/`.
- **Distinct:** the safe, complete convergence of E.

### I — "Ledger"
- **Identity:** editorial, light-first, publication-leaning — reads like a
  research publication; the ACM paper promoted to a prominent first-class tab.
- **Theme:** **Solarized-Light** (default); serif display voice, airy whitespace.
- **Signature:** the publication woven throughout with the dashboard's own live
  Tufte figures as its plates; report-style eyebrows/ledes/captions.
- **Navigation:** E's IA, `#/I/`.
- **Distinct:** the dashboard read as a paper.

### J — "Console"
- **Identity:** a dense, data-ink-maximal console for power users — E's flow
  reskinned compact, tighter trellises and side-by-side panels, keyboard-friendly.
- **Theme:** **Monokai** (default); the heatmap is theme-safe as themed ink at
  value-driven opacity.
- **Signature:** compact chrome + more small-multiples per screen; the dense skin
  later refined into Console II/III/IV.
- **Navigation:** E's IA, `#/J/`.
- **Distinct:** the dense observatory — the lineage that leads to the winner.

### K — "Monograph"
- **Identity:** report-first — the ACM-style epoch publication IS the home and
  primary surface; live Tufte figures embedded inline as the paper's plates,
  each clickable into the live dashboard.
- **Theme:** **Solarized-Light** (default), paper-like, serif display.
- **Signature:** the publication renderer (`paper.js`) — **judged the best of the
  round** and reused as a tab in every later variant. The paper-first metaphor
  itself was rejected.
- **Navigation:** `#/K/`, breadcrumbs rooted at the paper.
- **Distinct:** the best publication renderer in the field (but not as the home).

## Round 4 — convergence II (L–O)

### L — "Atlas III"
- **Identity:** the main-line convergence-II dashboard — clean dark, every round-3
  fix folded in, all seven round-4 fixes applied; K's publication reused as a TAB.
- **Theme:** **Solarized-Dark** (default) color · **Sans** (default) typeface.
- **Signature:** the combined mutation matrix + **side-by-side diff** with real
  strings; the per-board cross-candidate view, introduced in this round; the
  stacked promote gate; and the typeface picker, also introduced in this
  round.
- **Navigation:** E's IA, `#/L/`.
- **Distinct:** the safe converged pick of round 4 — and the reference mutation
  viewer.

### M — "Ledger II"
- **Identity:** editorial, light-first, publication-forward — I's refined skin,
  dashboard-first with the publication as a prominent tab.
- **Theme:** **Solarized-Light** (default) color · **Editorial** (default; Source
  Serif 4) typeface; airy whitespace.
- **Signature:** generous **spacing/proportion** (singled out as good and folded
  into rounds 5–6); typographic eyebrows/ledes/pull-quotes/captions.
- **Navigation:** E's IA, `#/M/`.
- **Distinct:** the comfortable, proportion-led take.

### N — "Console II"
- **Identity:** the dense observatory, convergence II — J's dense Console skin
  with the seven fixes and the typeface picker; **judged the most appealing base**
  for round 5.
- **Theme:** **Monokai** (default) color · **Technical** (default; JetBrains Mono)
  typeface; compact but proportional.
- **Signature:** the dense data-ink foundation everything in rounds 5–6 built on;
  the side-by-side mutation diff and per-board view in a compact skin.
- **Navigation:** E's IA, `#/N/`. (Its gap: tabs could not switch which
  epoch/candidate — fixed by the round-5 tree.)
- **Distinct:** the most-appealing base; the direct ancestor of the winner.

### O — "Compass"
- **Identity:** a master-detail two-pane workspace answering "clicking a board
  card dumped me on an arbitrary candidate" — selection made explicit and
  persistent via a fixed left selector rail + URL-encoded scope.
- **Theme:** **Solarized-Dark** (default) color · **Display** (default; Archivo
  Narrow) typeface.
- **Signature:** the scope-ordered selector rail (workspace → epoch → generation →
  board); epoch-scoped publication + mutation surface; the candidate-centric
  lifecycle + match-ups (folded forward).
- **Navigation:** scope-ordered rail + detail, `#/O/`.
- **Distinct:** explicit, high-fidelity selection — the precursor to the tree.

## Round 5 — convergence III (P–S) — all on a data-model TREE sidebar

### P — "Console III"
- **Identity:** the main line — the direct successor to N, the same dense
  data-ink observatory refined with the round-5 headline: a persistent,
  collapsible **data-model tree sidebar** + a single detail pane + every round-5
  fix. **Judged the best-looking console** and chosen as the round-6 anchor.
- **Theme:** **Monokai** (default) color · **Technical** (default) typeface;
  M's proportion applied (250px rail, roomy detail).
- **Signature:** the data-model tree sidebar (multi-epoch + multi-gen); promote
  gate on the candidate page; clickable patch node → per-candidate diff; ALL
  match-ups; first-class board view + inline side-by-side transcript; trellis/
  heatmap de-dup.
- **Navigation:** tree sidebar, `#/P/`.
- **Distinct:** the dense console on the data-model tree — the winner's base.

### Q — "Atlas IV"
- **Identity:** the comfortable take — N's content + diagrams dressed in M's
  generous spacing/proportion + L's mutation-viewer quality, on the tree sidebar.
- **Theme:** **Solarized-Dark** (default) color · **Sans** (default) typeface;
  roomy by design — larger padding, taller line-heights, bigger type.
- **Signature:** the **spacing/proportion** folded into the round-6 anchor; a
  shared `gatePanel()`. Shipped the (buggy) back button later fixed in round 6.
- **Navigation:** tree sidebar, `#/Q/`.
- **Distinct:** comfort over density on the same synthesis.

### R — "Strata"
- **Identity:** the same hierarchy navigated as macOS-Finder **Miller columns** —
  cascading columns where each selection drives the next and the rightmost pane
  is the detail.
- **Theme:** **Solarized-Dark** (default) color · **Display** (default; Archivo
  Narrow) typeface.
- **Signature:** the Miller-columns navigation metaphor (distinct from the nested
  accordion tree). Back-burnered after round 5 — not pursued in round 6.
- **Navigation:** Miller columns, `#/R/`.
- **Distinct:** the cascading-columns expression of the data model.

### S — "Lens"
- **Identity:** comparison-first — the tree sidebar plus a detail pane whose
  defining signature is first-class **side-by-side comparison**.
- **Theme:** **Solarized-Light** (default) color · **Editorial** (default; Source
  Serif 4) typeface; M's roomy gutters separate the two sides.
- **Signature:** the **compare model** (`comparePicker` + `splitFrame`, `~cmp=` /
  `~runs=` hash suffixes) — two candidates' lifecycle/gate/match-ups/scoring side
  by side, and champion-vs-challenger transcripts side by side. Folded into the
  round-6 anchor.
- **Navigation:** tree sidebar + split detail, `#/S/`.
- **Distinct:** comparison as the first-class detail idiom.

## Round 6 — convergence IV (T–W)

### T — "Console IV" — the convergence winner and the shipping console
- **Identity:** the round-6 anchor and the console that ships. **P (Console
  III)** supplies the base. Four elements are folded in from sibling variants
  S, Q, V and W. A navigation fix that round 6 applied across the field is
  included. One element, the density picker, is new in T.
- **Theme:** **Monokai** (default) color · **Technical** (default) typeface ·
  **compact** (default) density.
- **Folded into T:**
  - **P's** dense Console base + data-model tree sidebar + every round-5 fix.
  - **S's** first-class side-by-side **compare** detail (`~cmp=` split; champion
    vs challenger transcripts side by side).
  - **Q's** generous, proportional **spacing**.
  - a working **back/up button** (the explicit fix of Q's bug — navigates UP and
    renders into the MAIN detail pane, never the sidebar).
  - **the Reel** (from V) as a *slim* fit-to-width rounds spine on the **epoch
    view**, in place of the lineage bumps chart P carried there.
  - **the Arena match cards** (from W) on the **generations page** — a
    champion-defends banner + a wrapping grid of one compact match card per
    challenger round.
  - a **density / roominess picker** (compact · cozy · roomy) — a third chrome
    selector beside the color and typeface pickers, resizing the whole
    interface through a pure CSS token swap. Added to T after the six rounds
    closed.
- **Navigation:** data-model tree sidebar + back/up control, `#/T/`.
- **Distinct:** the converged anchor — P's console, S's compare, Q's spacing, the
  Reel on the epoch view, the Arena cards on the generations page, and a density
  picker, all in one.

### U — "Atlas V"
- **Identity:** the comfortable, airy sibling of T — the same P + S + Q synthesis
  (tree sidebar, compare-first detail, every round-5 fix, the fixed back button)
  rendered roomy and calm.
- **Theme:** **Solarized-Light** (default) color · **Sans** (default) typeface;
  Q/M-forward spacing.
- **Signature:** same content/structure as T, breathable aesthetic.
- **Navigation:** tree sidebar + back/up, `#/U/`.
- **Distinct:** the light/roomy take on the winning synthesis.

### V — "Reel"
- **Identity:** the creative-temporal take — the epoch as a horizontal **REEL**
  (timeline/playback of rounds); the reel is the hero AND the navigator. Built on
  the P anchor + S's compare + the fixed back button.
- **Theme:** **Solarized-Dark** (default) color · **Display** (default; Archivo
  Narrow) typeface.
- **Signature:** the fit-to-width reel film-strip with a scrubber/stepper —
  selecting a station opens that round's match-up + gate + lifecycle. (Its slim
  reel was adopted into T's epoch view.)
- **Navigation:** the reel + a collapsible tree sidebar, `#/V/`.
- **Distinct:** the epoch as a playable timeline.

### W — "Arena"
- **Identity:** the creative-broadcast take — the tournament as a live
  standings/leaderboard + **match cards**; the champion "defending the title" at
  top, each challenger a match card. Built on the P anchor + S's compare + the
  fixed back button.
- **Theme:** **Monokai** (default; broadcast-dark) color · **Display** (default;
  Archivo Narrow billboard voice) typeface.
- **Signature:** the broadcast billboard header + champion card + per-challenger
  match cards (standings double as navigation). (Its match cards were adopted
  into T's generations page.)
- **Navigation:** standings-as-nav + a collapsible tree sidebar, `#/W/`.
- **Distinct:** the tournament as a live broadcast.

---

## Provenance of the winner (T)

T did not introduce a new metaphor; it converged the field. For the record, the
threads that meet in T:

| Element in T | Came from |
| --- | --- |
| Dense Console base + data-model tree sidebar | P (← N ← J) |
| Side-by-side compare detail | S |
| Generous, proportional spacing | Q (← M) |
| Working back/up control | round-6 fix of Q's bug |
| Slim Reel on the epoch view | V |
| Match cards on the generations page | W |
| Density / roominess picker | new in T |
| Promote-gate ladder · per-candidate diff · per-board view · inline transcript | round-5 fixes (P/Q/R/S) |
| Theme + typeface pickers · theme-aware heatmap · Tufte Sankey label≠value | rounds 3–4 (B/D · H–O) |
| ACM publication tab | K (reused as a tab from round 4 on) |
| Tufte marks (bumps · slopegraph · dot-plot · trellis · heatmap) | D |
| Digest-gated render spine · breadcrumb IA · Fleet home | E (← A) |
