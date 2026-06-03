# Console IV — the design language

This document is the single, current source of truth for the **Console IV**
dashboard UI (the variant code-named **T**, the convergence winner of the
dashboard bake-off and the sole shipping front end). It consolidates a design
language that had been scattered across a round-by-round changelog
([variant-T.md](variant-T.md)) and a stale catalogue
([DASHBOARD-VARIANTS.md](DASHBOARD-VARIANTS.md)).

Everything here is derived from the live code under
`src/zicato/dashboard/static/js/variants/T/**` and
`css/variants/T/console4.css`. Where this document and the older docs disagree,
**the code is authoritative** and the older doc was stale. The per-round history
in [variant-T.md](variant-T.md) and the bake-off field record in
[DASHBOARD-VARIANTS.md](DASHBOARD-VARIANTS.md) remain as history; this is the
present-tense reference.

## 1. What Console IV is

Console IV is a **decision-centric console, not a report**. Its one job — the
same as the dashboard's overall ([DASHBOARD.md](DASHBOARD.md)) — is to make the
promote/reject decision over a champion-vs-challenger tournament **legible while
it is still in flight**. The aesthetic stance that follows from that job:

- **Graphical and interactive over tabular and static.** The primary surfaces
  are SVG figures — funnels, ladders, brackets, bump charts, dot-plots,
  sankeys — every one of which fits its pane and responds to hover and click.
  Tables exist (the candidate roster, the publication, the mutation matrix) but
  they are the supporting cast, not the lead.
- **A dense observatory for a power user.** The default skin (`console4.css`
  header: *"Console IV: dense observatory, Monokai default"*) is dense and
  data-ink-maximal. The single permanent spacing baseline is **cozy** (the
  density picker was removed); the operator tunes the fit with a page-wide
  **scale** pill, not a density toggle.
- **A console technical aesthetic.** Monospace data, a `CONSOLE IV` chrome
  brand, terminal-derived colour palettes, and a chess/tournament metaphor
  (crowns, the champion-gate, ladders and brackets) give the surface a
  coherent terminal-and-tournament voice. See §8 for the lineage of these
  choices.

Console IV is self-contained under `js/variants/T/**` + `console4.css` + the
entry `app_T.js`, reusing only the shared `js/core/*` data spine. Exactly one UI
loads at a time.

## 2. The six-colour ROLE system

Every theme is a set of CSS custom properties scoped under
`#variant-root[data-variant="T"][data-t-theme="<id>"]`, swapped by the
`[data-t-theme]` attribute. There is **no hardcoded hex in the marks** — every
figure reads its colour from the active theme's tokens, so a theme swap is a
pure CSS re-skin with no re-render.

### 2.1 The six semantic ROLES

The contract is six roles. The JS swatch tuples in `ui.js` `COLOR_THEMES` carry
them as `[paper, panel, ink, good, bad, accent]`; the CSS defines the same six
as `--v2-paper / --v2-panel / --v2-ink / --v2-good / --v2-bad / --v2-accent`.
Their **semantic meaning is fixed across every theme** — the consistency rules
are what make the marks readable no matter which palette is active:

| token | role | the rule it enforces |
| --- | --- | --- |
| `--v2-paper` | the ground / page background | the deepest surface; everything sits on it |
| `--v2-panel` | the surface of a panel / card / hovercard | one step lifted off the ground |
| `--v2-ink` | primary text + neutral mark strokes | the highest-contrast foreground |
| `--v2-good` | **improvement / promotion / survival** | a dot below the reference rule, a survivor `↑`, a crowned gate, a promoted verdict, the lower-loss side of a slopegraph — *always* the better outcome |
| `--v2-bad` | **regression / rejection / a cut** | a dot above the reference rule, a cut competitor `✕`, a rejected verdict, the worse side of a slopegraph — *always* the worse outcome |
| `--v2-accent` | **the one structural / interactive highlight** | the champion spine, the emphasised current-champion line, an interactive focus — used sparingly so it stays meaningful |

The cardinal rule: **`good` and `bad` are earned by direction, never by
identity.** A challenger is not red because it is a challenger; it is red only
when it regressed or was cut. An unscored / in-flight candidate is *neutral*
(pending), never `bad` (this is the "Class B" bug the code guards against in
`ui.decisionFor` and `tree.js`).

### 2.2 The secondary tokens

Each theme also defines a full secondary set, so every state has a token:

| token | role |
| --- | --- |
| `--v2-ink-soft` | secondary text (sub-labels, captions) |
| `--v2-ink-faint` | tertiary text (faint context tags, empty-state italics) |
| `--v2-rule` | borders / separators / hovercard outline |
| `--v2-rule-soft` | a fainter rule / inline-code background |
| `--v2-good-soft` / `--v2-bad-soft` | tinted fills behind a good / bad state |
| `--v2-caution` | caution / timeout (e.g. the budget-exceeded `⏱` glyph) |
| `--v2-flat` | unchanged / neutral-flat (a slopegraph that neither improved nor regressed) |
| `--v2-cell-empty` | an empty heatmap cell |

The heatmap ramp is built from the theme tokens at draw time — a cool→hot mix
between `--v2-hm-cool` and `--v2-hm-hot` via `color-mix(in srgb, …)` (see
`svg.heatmap`), so the ramp is theme-correct in light and dark alike.

### 2.3 The sixteen themes

There are **sixteen** colour themes (not three — the older catalogue was
stale). `monokai` is the default. The colour picker is a **swatch dropdown**
(`.dt-cd-trigger` / `.dt-cd-list`): with sixteen options the inline buttons
became a keyboard-accessible listbox, each option a 6-swatch preview strip
(*ground · surface · ink · improve · regress · accent*) plus the theme name.

The full set, each defining the complete `--v2-*` role + secondary contract:

| id | name | ground | lineage |
| --- | --- | --- | --- |
| `monokai` | monokai | dark | original |
| `solarized-dark` | solarized dark | dark | original |
| `solarized-light` | solarized light | light | original |
| `google-light` | google light | light | Gogh |
| `google-dark` | google dark | dark | Gogh |
| `lunaria-light` | lunaria light | light | Gogh |
| `lunaria-eclipse` | lunaria eclipse | dark | Gogh |
| `belafonte-day` | belafonte day | light | Gogh |
| `belafonte-night` | belafonte night | dark | Gogh |
| `paper` | paper | light | Gogh |
| `zenburn` | zenburn | dark | Gogh |
| `selenized-black` | selenized black | dark | Gogh |
| `relaxed` | relaxed | dark | Gogh |
| `espresso` | espresso | dark | Gogh |
| `dracula` | dracula | dark | Gogh |
| `ubuntu` | ubuntu | dark | Gogh |

The thirteen Gogh palettes are adapted from the established terminal colour
schemes at gogh-co.github.io/Gogh, mapped onto the 6-role contract by a single
principled rule (`paper ← background`, `panel ← background nudged toward the
foreground/host`, `ink ← bright-white/host` with `ink-soft ← foreground`,
`good ← green`, `bad ← red`, `caution ← yellow`, `accent ← cyan` — or the
palette's blue where the cyan is a low-contrast neutral, as for Belafonte). A
few accents/cautions were nudged for contrast so every mark reads on its ground
(e.g. Paper keys `ink` off near-black and `accent` off its blue; Zenburn nudges
`good` to a true sage and `accent` to its canonical cyan). See §8 for the Gogh
lineage in full.

> Note on the swatch preview: the `COLOR_THEMES` 6th tuple element is the
> theme's signature accent for the *preview strip only*. `lunaria-eclipse`
> substitutes a more distinct magenta (`#C8429F`) in the preview because its
> true `--v2-accent` (a pale blue) would be indistinguishable from its pale ink
> in the 6-swatch strip; the live `--v2-accent` token is unchanged.

## 3. The typography system

Typography is a separate axis from colour: a **typeface theme** picker (inline
buttons — only three options, so no dropdown) swaps the family tokens via the
`[data-t-type]` attribute on the root. The default is **Technical**. The three
voices are genuinely distinct, body included, so toggling is immediately
recognizable.

| id | voice | body | data / mono | headings | display |
| --- | --- | --- | --- | --- | --- |
| `editorial` | a typeset, literary reading voice | Source Serif 4 | Source Serif 4 | Source Serif 4 | Source Serif 4 |
| `technical` (default) | a console technical voice | Open Sans | JetBrains Mono | Open Sans | — |
| `display` | a punchy headline voice | Space Grotesk (geometric) | JetBrains Mono | Archivo Narrow (condensed) | Archivo Narrow |

The CSS resolves these through intermediate `--n-font-*` families and exposes
two tokens the marks read: **`--v2-sans`** (body) and **`--v2-mono`** (all
data, labels, axis text, code), plus `--n-font-head` (headings) and
`--n-font-paper` (the publication body). Editorial routes *everything* —
including the mono token — to the serif, so data and prose share one face;
Display gives the body a geometric grotesque and the headings a condensed
display face. Google Fonts is loaded in `app_T.js` with `display=swap` and
system fallbacks — the only external dependency.

(The legacy **Sans** typeface was dropped — it was redundant with Technical's
Open-Sans body; the `sans` id normalises to Technical.)

## 4. The visual-vocabulary grammar

Every figure is built in `svg.js` (the data-viz primitives) and `dag.js` (the
lifecycle DAG), composed by the views. (The champion-spine reel, once its own
`reel.js`, is folded into `svg.js`'s `roundTimeline`.) They share **one
grammar** — the same marks mean the same thing everywhere.

### 4.1 The figures

| renderer (`svg.*` unless noted) | purpose |
| --- | --- |
| `survivalFunnel` | the **racing epoch hero** — the field flowing `N → N/2 → … → 1 → champion-gate`, each rung a trapezoid stage whose width ∝ surviving field; survivors ride inside the band (`↑`), eliminated competitors peel off as labelled dead-end branches (`✕`). |
| `racingLadder` | the per-rung **successive-halving ladder** on Match-ups — one column per rung escalating to a trailing champion-gate, each runner's Δ-vs-champion right-aligned, a persistent v0 pace line at Δ=0. |
| `swissLadder` | the swiss **standings ladder** — a column per round, accumulating Copeland points (win 1 / draw ½), the leader flowing into a champion-gate. |
| `swissOverview` | the swiss epoch-overview centerpiece — a **standings bump chart** (one line per competitor, y = rank, lines cross as the leader emerges) over a **ranked Copeland-point bar**. |
| `elimFlow` | the elim figure EVERYWHERE (epoch hero + Match-ups) — the Tufte **bracket-as-flow**: rounds are columns, one lane per generation; two lanes **converge** at a match node, the winner's lane continues (`↑`, good), the loser's terminates (`✕`, bad), the champion's lane reaches the crowned gate (`♛`). Double-elim renders the losers' bracket as a second band of re-converging lanes. Pairing + Δ on hover. **Replaces the retired `elimBracket` seat/box tree.** |
| `duelFlow` | the **gauntlet** structure-flow — the round's field as Δ-vs-champion lanes: a horizontal Δ=0 reference rule is the champion (the crowned `♛` gate node), each challenger a lane with a dot **below** the rule when it improved (good) / **above** when it regressed (bad), status as a glyph (`↑`/`✕`/`○`). The per-challenger hypothesis + exact Δ live on hover. **Replaces the boxed champion banner + per-challenger match cards.** |
| `waterfall` | the **loss-floor descent across rounds** — one downward step per round sized by its promotion Δ (good by direction; a held round is flat), the running floor annotated, the champion-spine baseline in `accent`, the winning mutation per step on hover. The headline figure of the epoch round-timeline. |
| `reignGantt` | **champion tenure across rounds** — one bar per champion spanning the rounds it held; the current champion `accent` + `♛`, former champions dim ink + `♔`. The candidate page's **reign ribbon** (shown only for a generation that became champion). |
| `roundTimeline` | the **epoch overview hero** — the epoch's N evolve rounds along a horizontal champion **spine** (one node per round's incoming champion, its loss annotated so the descending floor reads at a glance), each round an episode card (incoming champion + a fan of minted challengers + a compact per-round structure figure + the gate outcome). Subsumes the old gauntlet reel; a single round degrades to one episode. The `waterfall` rides above it as the descent headline. |
| `bumps` | the lineage as ranked lanes — the champion spine on its own lane, rejected challengers branching into a lower lane. |
| `heatmap` | the **board × generation drift-loss matrix** (epoch overview), a theme-token cool→hot ramp. |
| `valueDotPlot` | per-board scoring — one row per entry, a dot vs a reference rule, an outcome glyph at the right edge. |
| `sparkbar` | a micro loss-bar strip + a verdict triangle, for trellis cells. |
| `genDots` | a proportional row of pass/fail/timeout glyphs for a trellis cell. |
| `valueBars` | per-judge losses as horizontal bars. |
| `pairedSlopegraph` | a per-board **slopegraph** — champion value → challenger value, one line per entry, coloured by improved / regressed / flat. |
| `sankey` / `layoutSankey` | the causal-flow **Tufte sankey**: `patch → per-board drift → gate`. |
| `lifecycleDag` (`dag.js`) | one candidate's life as a cause→effect summary: `parent → patch → board fan → Σ → gate → terminal`. |
| `proposingTracker` | the field forming — one row per minted challenger (`vN ✓ applied` / `vN ✗ rejected`), the seed of the live hero. |

> **Retired / folded.** `elimBracket` (the seat/box bracket tree) is **retired** —
> the elim figure everywhere is now `elimFlow` (the bracket-as-flow). The slim
> champion-spine **reel** (`reel.js`) is **folded into `roundTimeline`**: the
> timeline's spine IS the reel, generalised across all structures and rounds.

### 4.2 The shared mark conventions

Every figure above honours this table:

| convention | meaning | where set |
| --- | --- | --- |
| `↑` | this competitor **survives** the rung / round — the winner's lane **continues** | funnel, ladder runners, elim-flow / duel-flow lanes |
| `✕` | this competitor was **cut** — the loser's lane **terminates** | funnel dead-end branches, ladder, elim-flow / duel-flow lanes |
| `○` | this competitor is **pending** (racing, not yet decided) | duel-flow lanes |
| `♛` | the **current champion** (the crowned survivor of the gate) | gate labels, round-timeline spine, reign-gantt bar, tree badge, candidate / board / publication accents |
| `♔` | a **former champion** — the displaced incumbent / a transient round-leader before the gate decides | swiss ladder, bump chart, standings |
| reference rule | a Δ-vs-champion baseline at Δ=0; **good = below / lower loss, bad = above / higher loss** | dot-plot `dn-ref-rule`, racing ladder v0 pace line |
| hover-for-detail | a **styled, theme-aware hovercard** (`hovercard.js`) replaces the native SVG `<title>` tooltip — every mark calls `hov(node, tip)` | `svg.js`, `dag.js` |
| fit-to-width | `width:100%` + a `viewBox` + `preserveAspectRatio`; **no fixed pixel width that exceeds the pane, no pan/zoom** | every figure |
| proportional 1:1 glyphs | status glyphs (`✓ ✕ ⏱ ○`, verdict triangles) render in a **fixed 1:1-aspect overlay SVG** so a stretched cell never shears them into ovals | `outcomeGlyph`, `genDots`, `sparkbar` |

Recently-settled refinements within this grammar:

- **`♛` current vs `♔` former, consistently.** The current champion (the last id
  in `champion_lineage`) is the solid crown `♛`; every former champion — and a
  transient round-leader *before* the gate decides — is the hollow crown `♔`.
  Once the gate crowns a winner, `♛` takes over (no double crown). This holds
  across the funnel, ladders, bump chart, standings, and the tree legend. *(The
  glyphs are now defined ONCE — `svg.js` exports `CROWN = { current: '♛', former:
  '♔' }` and every emitter imports it, so the rule cannot drift. The historical
  `♚`/`♛` mix in the gate labels is resolved — see §9.)*
- **Survival-funnel cut labels: the connector leads INTO the label.** A cut
  competitor's dead-end branch drops from the band's lower edge and stops a few
  pixels *left* of its name (`H${labelX - 4}`) — the connector points into the
  label and never runs a line through the text (no strikethrough).
- **Match-ups collapse to a single section.** The swiss/racing/elim detail lives
  in one Match-ups section, not duplicated; the epoch overview shows a compact
  at-a-glance figure with a *"See Match-ups →"* link into the full detail.
- **"unscored" orphan labeling.** A generation with no parent and no resolved
  outcome is an *orphan* (`g.orphan` in `shell.js`); the tree badges it `◌
  unscored` (`gen-orphan`) — never a misleading "seed", never a default
  rejection.

### 4.3 The hovercard

Hover-for-detail is a first-class, intentional choice. `hovercard.js` mounts a
**singleton** card *inside* `#variant-root`, so it inherits the live per-theme
tokens (`--v2-panel` background, `--v2-ink` text, `--v2-rule` border, the mono
face) and reads correctly across all sixteen themes. It is positioned with
viewport flip/clamp so it never clips, honours `prefers-reduced-motion`, and is
keyboard-accessible (focusable target + `role="tooltip"` via `aria-describedby`).
Crucially it is a **transient overlay, not part of the digest-gated render**
(§6) — showing/hiding only toggles a class, so it can never trigger a repaint
loop.

## 5. Layout and interaction principles

- **Fit-to-width panes.** Every figure scales to its pane (§4.2); no figure
  forces horizontal scroll. Inherently-wide tables (publication GFM tables, the
  aggregate-scores table, the mutation matrix) carry their *own* contained
  overflow (`.dn-table-scroll`) so a wide table scrolls within its box and never
  pushes the page sideways.
- **The page-wide SCALE pill.** The sole sizing control is a draggable,
  keyboard-accessible range slider (`.dt-scale-pill` / `.dt-scale-range`) over
  ≈70 %–150 % in 5 % steps, default 100 %, with a `⟲` reset button. It applies
  page-wide via `zoom` on the app root (`shell.applyScale`), which **reflows**
  (not a transform), so the page re-wraps at the scaled size and never clips.
  Persisted under `zicato.T.scale`, orthogonal to colour/typeface.
- **Fluid, resolution-responsive layout.** The detail pane fills the available
  viewport width (only a generous, non-centred `max-width` guards prose
  line-length on ultra-wide displays), so the side-by-side compare grid
  (`.dt-split`, `1fr 1fr`) splits the full width and every fit-to-width SVG
  inside renders as large as the screen allows — bigger diagrams on bigger
  monitors, still tidy on small ones.
- **The data-model TREE sidebar ↔ detail-view router.** A persistent left tree
  (`tree.js`) mirrors the real zicato hierarchy — `Environment → Epoch →
  {Generations → <gen>, Boards → <entry>, Mutation surface, Publication}` — and
  drives a single detail pane. Routes are bare-prefixed (`#/`, `#/e/<epoch>`,
  `#/e/<epoch>/gen/<gen>`, …); the **`#/` path is the tree path**, so a cold
  deep-link hydrates both the open branches and the detail. The rail is a
  resizable left side-panel (a draggable `.dt-rail-handle`, persisted under
  `zicato.T.rail`), distinct from the page-scale pill.
- **The "up" control.** A top-left **`↑ up`** control navigates *up the
  selection hierarchy* (the parent route): candidate → generations → epoch →
  environment, a compare split collapsing to the bare candidate first. It
  **navigates** (changes the route) and lets the normal dispatch repaint the
  destination into the main detail pane — it never renders into the sidebar (the
  explicit fix of an earlier bug).
- **The side-by-side COMPARE model.** The candidate detail is comparison-first.
  A *"compare with…"* picker sets a `~cmp=<gen>` suffix on the hash (so the
  comparison deep-links); `splitFrame` then renders two candidate panels side by
  side, **each in its own digest-gated host** so one side changing never
  rebuilds the other. Champion-vs-challenger transcripts read side by side
  inline on the board view.

## 6. Render discipline — digest-gating

The first-class render principle: **never rebuild the DOM on a no-op SSE
heartbeat.** The bug class this prevents is the **flashing / refresh bug** — a
steady heartbeat re-dispatch wiping and rebuilding a panel every tick, flashing
the screen, losing scroll position, and destroying hovercard/focus state.

The mechanism is `ui.gatedSwap(host, digest, build)`: a view computes a stable
digest of **only its structural/content data** (timestamps and heartbeat fields
*excluded*); if that digest equals the one the host last painted *and* the host
still has children, **nothing is written** — a steady heartbeat is a true no-op.
The named digests (`treeDigest`, `structureDigest`, `funnelDigest`,
`proposingDigest`, `liveStatusDigest`, and per-view/per-pane digests) each gate
their own host. The discipline in full:

- Digest-gated repaint, structural data only; the heartbeat is a no-op.
- One persistent host per pane; each compare side and each board sub-host
  (`board-upper` vs `board-xscript`) is **independently** gated, so advancing
  in-flight progress repaints the upper pane while the transcript host (which
  excludes the in-flight set) is untouched and keeps its scroll position.
- The host is cleared on a real selection change (and a `~cmp` change is part of
  the selection).
- Motion is CSS `transition`, never `animation: …infinite`; live state animates
  *values / positions*, while digest-gating governs *structure*.
- The hovercard is a transient overlay outside the gated render (§4.3).

## 7. Live vs completed conventions

A live run must feel alive **without faking completed state** — the rule is
*animate actual state changes, never repaint-loop, and prefer push (SSE) over
poll*. `live.js` owns one persistent `LiveController` patched in place on every
`state:changed` tick.

- **Live pills / markers.** A structure-agnostic status pill (`.dt-status`,
  `livestatus.deriveLiveStatus`) reads a non-idle heartbeat phase, the
  in-flight active-runs count, and the active-tournament phase into one verdict;
  when running it shows a pulsing RUN badge naming the structure and phase
  (`racing · rung 0`, `swiss · round 2`, `proposing field`). A `LIVE` pill
  (`.dt-live-pill`) rides beside the structure pill.
- **Structure-aware pending labels — never a faked verdict.** A rung with no
  recorded cut/survivors renders **pending** (neutral, nobody struck), and the
  gate reads **"deciding…"** rather than crowning a not-yet-committed winner. A
  queued future round is dimmed, not blanked. The lifecycle DAG's pending
  terminal node reads racing / competing / in bracket / at gate per the
  structure — never a hardcoded "racing" for a non-racing candidate.
- **The hero "bloom".** During the proposing phase the hero leads with the
  proposing tracker (the field forming); the moment the field is applied and the
  tournament starts running, `buildLiveRoundModel` seeds zero-point standings
  from the applied competitors so the hero **blooms** from the tracker into the
  live standings ladder — the tracker is the *seed* of the ladder, not a
  dead-end.
- **The proposing tracker — honest field shape.** `proposingTracker` reads the
  field's shape honestly: *"N proposed · k applied"*, and a field that minted
  **zero** applied challengers reads *"— all rejected"* — never an empty/idle
  hero.
- **Live-first data resolution.** A view in flight prefers the live
  `/api/active-tournament` topology over the completed `/api/tournaments` record
  (which only commits the decision at the very end), so a mid-run epoch never
  shows an empty ladder or mislabels the eventual winner as eliminated. When
  idle it falls back to the completed record. Every live figure is still
  digest-gated; its motion is GPU-friendly (`transform`/`opacity`/`width`) and
  collapses under `prefers-reduced-motion`.

## 8. Design-language inspirations / lineage

Console IV's grammar is not arbitrary — each principle traces to a public design
authority. (These influences belong in this document; they are deliberately
**not** cited in the source code.)

### 8.1 Edward Tufte

Tufte's analytical-design principles map directly onto the figures:

- **Data-ink ratio / no chartjunk → fit-to-width minimal SVGs.** Every figure
  carries the maximum data per stroke and drops decoration: no gridlines for
  their own sake, no 3-D, no chart frames — just the band, the dot, the rule,
  the label. The fit-to-width discipline (`width:100%` + `viewBox`, no scroll
  wrappers) is the layout corollary.
- **Small multiples → the board trellis.** The Boards view is a small-multiples
  trellis — one tiny `sparkbar` + `genDots` card per board entry, all on a
  shared scale, scanned at a glance.
- **Sparklines → the `sparkbar` (and `sparkline`).** Word-sized, label-free
  trend marks embedded directly in a card.
- **Slopegraphs → the paired per-round `pairedSlopegraph`.** Champion value →
  challenger value as a slope per board entry, the up/down of each line reading
  improvement or regression directly.
- **Layering & separation; micro/macro reading → the overview-vs-drill-down
  IA.** The epoch overview is the macro read (a compact funnel / bump / mini-
  bracket); Match-ups and the candidate page are the micro read. The colour
  roles layer the good/bad/accent signal cleanly off the neutral ink ground.
- **The causal-flow Sankey → `sankey` (`patch → per-board drift → gate`).** A
  Tufte-style flow whose band widths carry the causal magnitude from the patch,
  through the per-board drift, to the gate — label ≠ value, by discipline.

### 8.2 Gogh terminal colour schemes

Thirteen of the sixteen themes are **adapted from the established terminal
colour schemes catalogued at gogh-co.github.io/Gogh** — Solarized, Monokai,
Dracula, Nord-adjacent, Gruvbox-adjacent and the like (the concrete set in §2.3:
google-light/dark, lunaria-light/eclipse, belafonte-day/night, paper, zenburn,
selenized-black, relaxed, espresso, dracula, ubuntu). Each Gogh palette is
mapped onto the 6-role `--v2-*` contract by one principled rule (§2.3), so the
provenance is a real terminal palette while the semantic role system stays
intact across all sixteen. The choice of *terminal* palettes is itself part of
the console aesthetic (§8.3).

### 8.3 The terminal / console technical aesthetic

The `CONSOLE IV` chrome brand, the monospace data face (the default Technical
typeface's JetBrains Mono for all data/labels/code), and the terminal-derived
palettes together give the surface a **terminal-and-console voice** — the
instrument reads like a power-user's console, not a consumer report.

### 8.4 The chess / tournament metaphor

The decision is a tournament: a reigning **champion** defends its title against
**challengers** at a **champion-gate**. The visual vocabulary makes the metaphor
literal — the crowns `♛` (current champion) and `♔` (former champion / displaced
incumbent), the champion-gate as the terminal confirmation seat, and the
ladder / bracket / funnel as the bracket-sheet shapes of the configured
tournament structure. (The champion/challenger vs parent/child terminology is in
[VOCABULARY.md](VOCABULARY.md).)

## 9. Known ambiguities / inferred intent

A few places where the code's design rule was not fully settled and this
document states the *intended* rule:

- **`♛` vs `♚` for the crowned champion. — RESOLVED.** The single rule is `♛` for
  the current champion and `♔` for a former champion. The crown glyphs are now
  defined ONCE — `svg.js` exports `CROWN = { current: '♛', former: '♔' }` — and
  every emitter (`svg.js` funnel/ladder/elim-flow/duel-flow gate labels +
  `waterfall`/`reignGantt`/`roundTimeline` crowns, `views/structure.js`
  gate notes + legends + standings, `live.js` activity feed, `tree.js` badges,
  `dag.js` terminal, `views/epoch.js` overview captions) imports it. No site
  emits `♚` for the current/just-crowned champion any longer; the residual mix is
  gone.
- **`--v2-serif` / `--v2-display` as token names.** The task framing referenced
  per-theme `--v2-serif`/`--v2-display` tokens; the actual implementation
  exposes the typeface families through `--v2-sans` + `--v2-mono` (the two
  tokens the marks read) plus the intermediate `--n-font-serif` / `--n-font-
  display` / `--n-font-geo` families that `[data-t-type]` routes into them. The
  *system* is as documented in §3; the token spelling is `--v2-sans`/`--v2-mono`
  + `--n-font-*`, not `--v2-serif`/`--v2-display`.
