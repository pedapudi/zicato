# Console — the dashboard's application of the design language

> **Canonical language doc:** the zicato design *language* — the colour tokens,
> the three typeface modes, the line-art figure conventions, render discipline,
> accessibility, and a worked "build a new surface" walkthrough — lives in
> **[DESIGN-LANGUAGE.md](DESIGN-LANGUAGE.md)**, the single source of truth for
> any zicato UI. **This** document is the dashboard's *specific application* of
> that language: the Console's information architecture (the tree-to-detail
> router, the overview and drill-down levels), the figure-to-purpose mapping,
> the live-vs-completed conventions, and the design lineage. For a shared
> token, typeface or figure value, defer to DESIGN-LANGUAGE.md; for how the
> Console uses it, read on here. Where the two overlap, DESIGN-LANGUAGE.md is
> canonical for the language and this document is canonical for the dashboard
> application.

This document is the source of truth for the **Console** dashboard interface —
the dashboard's sole front end.
It states in one place the design language that
[CONSOLE-CHANGELOG.md](CONSOLE-CHANGELOG.md) records round by round and that
[DASHBOARD-VARIANTS.md](DASHBOARD-VARIANTS.md) catalogues across the
bake-off field.

Everything here is derived from the live code: the modules under
`src/zicato/dashboard/static/js/` and the stylesheet
`src/zicato/dashboard/static/css/console.css`. Where this document and the code
disagree, **the code is authoritative**. [CONSOLE-CHANGELOG.md](CONSOLE-CHANGELOG.md) and
[DASHBOARD-VARIANTS.md](DASHBOARD-VARIANTS.md) are historical records; this is
the present-tense reference.

## 1. What Console is

Console is a decision-centric console rather than a report. Its one job — the
same as the dashboard's overall ([DASHBOARD.md](DASHBOARD.md)) — is to make the
promote/reject decision over a champion-vs-challenger tournament **legible while
it is still in flight**. The aesthetic stance that follows from that job:

- **Graphical and interactive over tabular and static.** The primary surfaces
  are SVG figures — funnels, ladders, brackets, bump charts, dot-plots,
  slopegraphs — every one of which fits its pane and responds to hover and click.
  Tables exist (the candidate roster, the publication, the mutation matrix) but
  they support the figures rather than leading.
- **A dense observatory for a power user.** The default skin (`css/console.css`)
  is dense and data-ink-maximal, on the `monokai` palette. The single permanent
  spacing baseline is **cozy**; the operator tunes the fit with a page-wide
  **scale** control in Settings → Appearance.
- **A console technical aesthetic.** Monospace data, a `console` tag beside
  the top-bar wordmark, terminal-derived colour palettes, and a chess/tournament metaphor
  (crowns, the champion-gate, ladders and brackets) give the surface a
  coherent terminal-and-tournament voice. See §8 for the lineage of these
  choices.

Console is self-contained: the entry `console.js`, the modules under `js/`, and
the stylesheet `css/console.css`, with the data spine in `js/core/*`. It is
the only UI the dashboard loads.

## 2. The six-colour ROLE system

Every theme is a set of CSS custom properties scoped under
`#console-root[data-t-theme="<id>"]`, swapped by the
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
| `--v2-bad` | **regression / rejection / a cut** | a dot above the reference rule, a cut competitor's fail mark, a rejected verdict, the worse side of a slopegraph — *always* the worse outcome |
| `--v2-accent` | **the one structural / interactive highlight** | the champion spine, the emphasised current-champion line, an interactive focus — used sparingly so it stays meaningful |

The cardinal rule: **`good` and `bad` are earned by direction, never by
identity.** A challenger is not red because it is a challenger; it is red only
when it regressed or was cut. An unscored / in-flight candidate is *neutral*
(pending), never `bad`. The server supplies both the decision token and its
display label; browser views only choose the matching visual token.

### 2.2 The secondary tokens

Each theme also defines a full secondary set, so every state has a token:

| token | role |
| --- | --- |
| `--v2-ink-soft` | secondary text (sub-labels, captions) |
| `--v2-ink-faint` | tertiary text (faint context tags, empty-state italics) |
| `--v2-rule` | borders / separators / hovercard outline |
| `--v2-rule-soft` | a fainter rule / inline-code background |
| `--v2-good-soft` / `--v2-bad-soft` | tinted fills behind a good / bad state |
| `--v2-caution` | caution / timeout (e.g. the budget-exceeded timeout mark) |
| `--v2-flat` | unchanged / neutral-flat (a slopegraph that neither improved nor regressed) |
| `--v2-cell-empty` | an empty heatmap cell |

The heatmap ramp is built from the theme tokens at draw time — a cool→hot mix
between `--v2-hm-cool` and `--v2-hm-hot` via `color-mix(in srgb, …)` (see
`svg.heatmap`), so the ramp is theme-correct in light and dark alike.

### 2.3 The sixteen themes

There are **sixteen** colour themes; `monokai` is the default. The colour
picker is a **swatch dropdown** (`.dt-cd-trigger` / `.dt-cd-list`): sixteen
options need a keyboard-accessible listbox rather than inline buttons, and each
option is a 6-swatch preview strip (*ground · surface · ink · improve ·
regress · accent*) plus the theme name.

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
schemes at gogh-co.github.io/Gogh. One mapping rule carries each of them onto
the 6-role contract: `paper ← background`, `panel ← background nudged toward
the foreground/host`, `ink ← bright-white/host` with `ink-soft ← foreground`,
`good ← green`, `bad ← red`, `caution ← yellow`, and `accent ← cyan` — or the
palette's blue where the cyan is a low-contrast neutral, as for Belafonte. A
few accents and cautions sit off the source palette so every mark reads on its
ground: Paper keys `ink` off near-black and `accent` off its blue, and Zenburn
takes a true sage for `good` and its canonical cyan for `accent`. See §8 for
the Gogh lineage in full.

> Note on the swatch preview: the `COLOR_THEMES` 6th tuple element is the
> theme's signature accent for the *preview strip only*. `lunaria-eclipse`
> substitutes a more distinct magenta (`#C8429F`) in the preview because its
> true `--v2-accent` (a pale blue) would be indistinguishable from its pale ink
> in the 6-swatch strip; the live `--v2-accent` token is unchanged.

## 3. The typography system

Typography is a separate axis from colour: a **typeface** picker swaps the
family tokens via the `[data-t-type]` attribute on the root. It is a **grouped
popover** (`typefacedropdown.js`) in Settings → Appearance. It carries three **mode** headers — **Technical
(default) · Editorial · Display** — each over **four** pairings, twelve in
all. Every option row is a true micro-specimen rendered in its own faces.

The picker governs both faces, and every pairing keeps the interface rule
"sans for prose and controls, mono for data": the prose face is a sans, the
data face is a monospace, and the heading face is never a monospace. A
monospace face (Google Sans Mono, Inconsolata) is therefore offered as a data
face paired with Open Sans; a serif or display face is offered as a heading
face over a sans prose face and JetBrains Mono data.

| mode | heading voice | pairings (`TYPE_OPTIONS`) |
| --- | --- | --- |
| `technical` (default) | the prose sans | Open Sans + Google Sans Mono · Source Sans 3 + Source Code Pro · Open Sans + Inconsolata · Ubuntu + Ubuntu Mono |
| `editorial` | a typeset serif | Fraunces · Bitter · Literata · Domine, each over Open Sans and JetBrains Mono |
| `display` | a punchy display face | Archivo Narrow + Space Grotesk · Hanken Grotesk · Barlow Condensed + Space Grotesk · Bricolage Grotesque, each with JetBrains Mono data |

Alongside the face picker the popover carries an **S/M/L font-size** segmented
control (`FONTSIZE_OPTIONS`). `applyFontSize` stamps a text-only multiplier
(`--dt-font-scale`) and syncs every live picker instance; it is orthogonal to
the page scale, which zooms figures and text together.

Each `[data-t-type]` rule sets the four font tokens to literal stacks:
**`--v2-sans`** (prose, controls and chrome; the console root is set in it),
**`--v2-mono`** (data, code, ids, hashes, key names, the numbers in tables and
figures), `--n-font-head` (headings) and `--n-font-paper` (the publication
title). The top bar, the tree, buttons, section headings and prose resolve to
the sans; an element opts into the mono by its data role (`.dn-mono`, a
table, a numeric tile value, a tree row named by an id, a figure's tick
values and ids). A figure's captions, axis titles, legends and sentences are
sans, and so is a tile value led by a word (`ui.valueFace`). A matrix table
is set in the mono for its ids and numbers, and its labels opt back into the
sans: the corner naming the axes (`entry · candidate →`), the round group
headers, the `holdout` word and the word `flip` before a flip rate. The Google
Fonts families load in `console.js` with `display=swap` and system sans or
monospace fallbacks — the only external dependency; the self-hosted JetBrains
Mono under `fonts/` backs the fixed brand mono and the editorial and display
data face. `test/interface_rules.test.mjs` fails if a chrome rule sets the
mono token or if any option breaks the pairing rule.

## 4. The visual-vocabulary grammar

Every figure is built in `svg.js` (the data-viz primitives) and `dag.js` (the
lifecycle DAG), composed by the views. The champion-spine reel is part of
`svg.js`'s `roundTimeline`. They share **one grammar** — the same marks mean
the same thing everywhere.

### 4.1 The figures

| renderer (`svg.*` unless noted) | purpose |
| --- | --- |
| `survivalFunnel` | the **racing epoch hero** — a dot ladder of the field flowing `N → N/2 → … → 1 → champion-gate`: each rung is a column of dots, one per competitor entering it, and splines carry each survivor's dot to its next-rung position as the field converges. A cut drops a small fail mark after its last dot and grows no further spline; every competitor is named once at its left-edge entry row (survivors carry the up mark, cuts dimmed), and the winner's splines carry the accent end to end into the crowned gate. |
| `racingScalarTrack` | the racing field on a **shared scalar axis** — one track per rung, each competitor a marker at its scalar, survivors kept and cut candidates drawn hollow, the champion as a dashed accent benchmark line and the cut threshold as a dashed caution tick. Queued, in-flight, projected and settled markers each have their own treatment. |
| `gauntletFieldBars` | the gauntlet field against the **fixed champion standard** — one bar per challenger from the champion line to its own scalar, coloured by outcome, with the promote gate (champion − margin) as a dashed accent threshold. |
| `swissLadder` | the swiss **standings ladder** — a column per round, accumulating Copeland points (win 1 / draw ½), the leader flowing into a champion-gate. |
| `swissOverview` | the swiss epoch-overview centerpiece — a **standings bump chart** (one line per competitor, y = rank, lines cross as the leader emerges) over a **ranked Copeland-point bar**. |
| `elimRadial` | the elim figure EVERYWHERE (epoch hero, Match-ups, live hero) — the **radial bracket**: rounds are concentric rings narrowing to a centre champion seat, one spoke per competitor; a spoke's surviving segments read good, the ring it was eliminated at ends with the fail mark (bad), the champion's spoke dashes into the seat, which carries the solid crown. Double-elim puts the winners' bracket on the upper arc and the losers' on the lower, split by a dashed equator; a winners'→losers' drop is a rim-hugging transfer arc. Outcome + round on hover. |
| `duelFlow` | the **gauntlet** structure-flow — the round's field as Δ-vs-champion lanes: a horizontal Δ=0 reference rule is the champion (the gate node, crowned), each challenger a lane with a dot **below** the rule when it improved (good) / **above** when it regressed (bad), status as a mark (up / fail / ring). The per-challenger hypothesis and its Δ live on hover. |
| `waterfall` | the **loss-floor descent across rounds** — one downward step per round sized by its promotion Δ (good by direction; a held round is flat), the running floor annotated, the champion-spine baseline in `accent`, the winning mutation per step on hover. The headline figure of the epoch round-timeline. |
| `reignGantt` | **champion tenure across rounds** — one bar per champion spanning the rounds it held; the current champion `accent` + the solid crown, former champions dim ink + the open crown. The candidate page's **reign ribbon** (shown only for a generation that became champion). |
| `roundTimeline` | the **epoch overview hero** — the epoch's N evolve rounds along a horizontal champion **spine** (one node per round's incoming champion, its loss annotated so the descending floor reads at a glance), each round an episode card (incoming champion + a fan of minted challengers + a compact per-round structure figure + the gate outcome). A single round degrades to one episode. The `waterfall` rides above it as the descent headline. |
| `metaLoopLedger` | the **cross-epoch home overview** combines a held-floor staircase, effort-proportional epoch bands, and a contract-component heatstrip. The staircase shows the best scalar each contract held. Band width follows the number of generations spent. The heatstrip names the component that caused each reset: board · brief · scoring · evaluator revision · adapter · mutable trees · structure · proposer. A structure roll is a SOFT seam because floors on opposite sides are not comparable; the staircase and structure cell use a dashed boundary. Rendering is digest-gated by `metaLoopLedgerDigest` and degrades on zero or one epoch. |
| `bumps` | the lineage as ranked lanes — the champion spine on its own lane, rejected challengers branching into a lower lane. |
| `heatmap` | the **board × generation drift-loss matrix** (epoch overview), a theme-token cool→hot ramp. |
| `valueDotPlot` | per-board scoring — one row per entry, a dot vs a reference rule, an outcome mark at the right edge. |
| `sparkbar` | a micro loss-bar strip + a verdict triangle, for trellis cells. |
| `genDots` | a proportional row of pass/fail/timeout marks for a trellis cell. |
| `valueBars` | per-judge losses as horizontal bars. |
| `pairedSlopegraph` | a per-board **slopegraph** — champion value → challenger value, one line per entry, coloured by improved / regressed / flat. |
| `radarSilhouette` | the candidate against the champion across the axes the gate weighs — scalar, pass rate, and each per-judge drift — outer is better. |
| `diversityMatrix` | the field-diversity grid: one column per challenger, one row per mutation site, a filled square where that challenger touched that site. |
| `calibrationTrend` | the proposer's prediction calibration across the lineage, reading the served latest fraction. |
| `trajectoryStrip` | one imported trace as a strip, drawn from the server's precomputed strip model (see [TRAJECTORY-UI.md](TRAJECTORY-UI.md)). |
| `lifecycleDag` (`dag.js`) | one candidate's life as a cause→effect summary: `parent → patch → board fan → Σ → gate → terminal`. |
| `proposingTracker` | the field forming — one row per minted challenger (`vN` + pass mark + `applied` / `vN` + fail mark + `rejected`), the seed of the live hero. |

> **Two figures the catalogue does not list.** There is no seat/box bracket
> tree and no lane-flow bracket; `elimRadial` (the radial bracket) is the elim
> figure everywhere. There is no standalone champion-spine reel module;
> `roundTimeline`'s spine is that reel, generalised across all structures and
> rounds.

### 4.2 The shared mark conventions

Every figure above honours this table:

| convention | meaning | where set |
| --- | --- | --- |
| `up` icon | this competitor **survives** the rung / round — the winner's lane **continues** | funnel rail names, swiss ladder, elim-radial spokes, duel-flow lanes |
| `fail` icon | this competitor was **cut** — the loser's lane **terminates** | funnel cut marks, swiss ladder, elim-radial spokes, duel-flow lanes |
| `ring` icon | this competitor is **pending** (still racing, undecided) | duel-flow lanes |
| `crown` icon (solid) | the **current champion** (the crowned survivor of the gate) | gate labels, round-timeline spine, reign-gantt bar, tree mark, candidate / board / publication accents |
| `crown-former` icon (open) | a **former champion** — the displaced incumbent / a transient round-leader before the gate decides | swiss ladder, bump chart, standings |
| drawn icons | every mark is an icon from `js/icons.js` (16-unit grid, 1.5-unit round-capped stroke, `currentColor`, `aria-hidden`); a figure places one with `svg.figIcon` / `svg.iconBeside`. No mark is a typed Unicode symbol, because the bundled faces lack them | every module; pinned by `test/icons.test.mjs` |
| reference rule | a Δ-vs-champion baseline at Δ=0; **good = below / lower loss, bad = above / higher loss** | dot-plot `dn-ref-rule`, the racing track's champion benchmark line |
| hover-for-detail | a **styled, theme-aware hovercard** (`hovercard.js`) replaces the native SVG `<title>` tooltip — every mark calls `hov(node, tip)` | `svg.js`, `dag.js` |
| fit-to-width | `width:100%` + a `viewBox` + `preserveAspectRatio`; **no fixed pixel width that exceeds the pane, no pan/zoom** | every figure |
| proportional 1:1 marks | status marks (pass, fail, timeout, no run; verdict triangles) render in a **fixed 1:1-aspect overlay SVG** so a stretched cell never shears them into ovals | `outcomeGlyph`, `genDots`, `sparkbar` |

Four further conventions hold within this grammar:

- **Solid crown current vs open crown former, consistently.** The current
  champion (the last id in `champion_lineage`) takes the solid crown; every
  former champion — and a transient round-leader *before* the gate decides —
  takes the open crown. Once the gate crowns a winner, the solid crown takes
  over (no double crown). This holds across the funnel, ladders, bump chart,
  standings, and the tree legend. *(The crowns have one definition —
  `js/icons.js` exports `CROWN = { current: 'crown', former: 'crown-former' }`,
  `svg.js` re-exports it, and every emitter imports it, so the rule cannot
  drift. See §9.)*
- **Survival-funnel names sit on a rail, never on a line.** Each competitor
  is named once, at its left-edge entry row; a cut is a fail mark in the gap after
  its last dot, so no spline or connector ever runs through a label (no
  strikethrough).
- **Match-ups collapse to a single section.** The swiss/racing/elim detail lives
  in one Match-ups section and is not duplicated elsewhere; the epoch overview
  shows a compact at-a-glance figure with a *"See Match-ups →"* link into the
  full detail.
- **"unscored" orphan labeling.** A generation with no parent and no resolved
  outcome is an *orphan* (`g.orphan` in `shell.js`); the tree marks it with
  the dashed `unscored` ring and the `unscored` tag (`gen-orphan`) — never a misleading "seed", never a default
  rejection.

### 4.3 The hovercard

Hover-for-detail is a first-class, intentional choice. `hovercard.js` mounts a
**singleton** card *inside* `#console-root`, so it inherits the live per-theme
tokens (`--v2-panel` background, `--v2-ink` text, `--v2-rule` border, the sans
face for its prose) and reads correctly across all sixteen themes. It is positioned with
viewport flip/clamp so it never clips, honours `prefers-reduced-motion`, and is
keyboard-accessible (focusable target + `role="tooltip"` via `aria-describedby`).
It is a **transient overlay that sits outside the digest-gated render** (§6):
showing and hiding it only toggles a class, so it can never trigger a repaint
loop.

### 4.4 States, captions, selection and where navigation lives

A durable discipline for any new surface: **reuse the grammars Console already
speaks; do not invent chrome beside them.** Five rules:

- **No pill, tag or badge.** A semantic state — a `verdict`, a `severity`, a
  row's role — is plain text in its ROLE-token colour, led by a drawn mark
  from `js/icons.js` where it has one (`verdictLabel`, `stateLabel`,
  `flagLabel` in `ui.js`). Nothing draws a rounded box, a fill or a border
  round a label. A metric, a count, a relation, or a model name is not
  semantic state and stays uncoloured text.
- **No accent left rail.** Neither a container nor a selection carries a
  coloured left edge. The selected item renders its name in the accent colour
  (the tree row, the pinned matrix row, the selected trace episode, the open
  Settings section). A meaning a container must show (a turn's role, a
  held-out entry, a finding's tone) rides a drawn mark and the text colour.
- **Metadata is a caption.** Fidelity tier, adjudicator model, prompt version,
  self-agreement, a verdict tally — all ride ONE `dn-faint` caption line under
  the relevant figure or section, never a per-row tag.
- **Navigation lives in the shell** — the hash router's routes and the tree
  sidebar. A view never grows an internal navigation rail of its own.

The Instrument lens (the board-reflection surface) is the worked case.
Findings and the practice review render as the loop-health findings panel's
quiet verdict-led rows: a tone mark, a headline, and a `dn-faint` rationale.
Scorecard rates use the `dn-stat` idiom, redundancy and conflict read as one
faint inline sentence, and evidence appears as inline x-ray links. Metadata
collapses to a caption, the one coloured state word is the adjudication
verdict, and navigation rides the routes and the tree. The lens carries no internal rail and
no per-row tags. See [BOARD-REFLECTION.md](BOARD-REFLECTION.md#ui--the-instrument-lens)
and [dev-guide §9.7.7](../dev-guide/09-dashboard-and-query.md#977-the-console-grammar-discipline--reuse-grammars-dont-invent-chrome).

## 5. Layout and interaction principles

- **Fit-to-width panes.** Every figure scales to its pane (§4.2); no figure
  forces horizontal scroll. Inherently-wide tables (publication GFM tables, the
  aggregate-scores table, the mutation matrix) carry their *own* contained
  overflow (`.dn-table-scroll`) so a wide table scrolls within its box and never
  pushes the page sideways.
- **The page-wide SCALE control.** The page-sizing control is a keyboard-
  accessible range slider in Settings → Appearance (`.dn-set-range`) over
  70 %–150 % in 5 % steps, default 100 %, with a `⟲` reset button. It applies
  page-wide via `zoom` on the app root (`shell.applyScale`), which **reflows**
  the page rather than transforming it, so the page re-wraps at the scaled size
  and never clips.
  Persisted under `zicato.console.scale`, orthogonal to colour/typeface.
- **Fluid, resolution-responsive layout.** The detail pane fills the available
  viewport width; only a generous, non-centred `max-width` guards prose
  line-length on ultra-wide displays. The side-by-side compare grid
  (`.dt-split`, `1fr 1fr`) therefore splits the full width, and every
  fit-to-width SVG inside it renders as large as the screen allows: bigger
  diagrams on bigger monitors, still tidy on small ones.
- **The data-model TREE sidebar ↔ detail-view router.** A persistent left tree
  (`tree.js`) mirrors the real zicato hierarchy — `Environment → Epoch →
  {Rounds → Round <n> → <gen>, Boards → <entry>, Evals, Instrument (→ Traces
  when the epoch has reflections), Mutation surface, Publication}` — and
  drives a single detail pane. Routes are bare-prefixed (`#/`, `#/e/<epoch>`,
  `#/e/<epoch>/gen/<gen>`, …); the **`#/` path is the tree path**, so a cold
  deep-link hydrates both the open branches and the detail. The rail is a
  resizable left side-panel (a draggable `.dt-rail-handle`, also set from a
  width slider in Settings → Appearance, persisted under
  `zicato.console.rail`), distinct from the page scale.
- **The "up" control.** A top-left **`↑ up`** control navigates *up the
  selection hierarchy* (the parent route): candidate → rounds → epoch →
  environment, a compare split collapsing to the bare candidate first. It
  **navigates** (changes the route) and lets the normal dispatch repaint the
  destination into the main detail pane; it never renders into the sidebar.
- **The side-by-side COMPARE model.** The candidate detail is comparison-first.
  A *"compare with…"* picker sets a `~cmp=<gen>` suffix on the hash (so the
  comparison deep-links); `splitFrame` then renders two candidate panels side by
  side, **each in its own digest-gated host** so one side changing never
  rebuilds the other. Champion-vs-challenger transcripts read side by side
  inline on the board view.
- **The picked BASELINE.** The patch diff is taken against the candidate's
  recorded parent. A *"baseline"* picker sets a `~base=<gen>` suffix
  on the hash and moves the LEFT column to that generation; the right column
  and the rows stay the candidate's own. It is the same select, and it carries
  the accent because it is that page's one control. The parent is the default and carries no
  suffix, so the default view keeps one canonical URL, and a non-default choice
  tints the strip rather than changing the diff quietly.

## 6. Render discipline — digest-gating

The first-class render principle: **never rebuild the DOM on a no-op
server-sent-events (SSE) heartbeat.** The bug class this prevents is the **flashing / refresh bug** — a
steady heartbeat re-dispatch wiping and rebuilding a panel every tick, flashing
the screen, losing scroll position, and destroying hovercard/focus state.

The mechanism is `ui.gatedSwap(host, digest, build)`. A view computes a stable
digest of **only its structural and content data**, excluding timestamps and
heartbeat fields. If that digest equals the one the host last painted *and* the
host still has children, **nothing is written**, so a steady heartbeat is a
true no-op.
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

- **Live state words / markers.** A structure-agnostic status line
  (`.dt-status`, `livestatus.deriveLiveStatus`) folds a non-idle heartbeat
  phase, the in-flight active-runs count, and the active-tournament phase into
  one verdict. The top bar shows it as one drawn status mark (filled while
  running, open once settled, struck when the loop stopped without settling,
  dashed with no current verdict), then the state word. While a run is going
  the mark pulses and the label names the structure and phase (`racing · rung 0`, `swiss · round 2`,
  `proposing field`). A `LIVE` word (`.dt-live-state`) rides beside the
  structure label, as plain text.
- **Structure-aware pending labels — never a faked verdict.** A rung with no
  recorded cut/survivors renders **pending** (neutral, nobody struck), and the
  gate reads **"deciding…"** rather than crowning a not-yet-committed winner. A
  queued future round is dimmed rather than blanked. The lifecycle DAG's pending
  terminal node reads racing / competing / in bracket / at gate per the
  structure — never a hardcoded "racing" for a non-racing candidate.
- **The hero "bloom".** During the proposing phase the hero leads with the
  proposing tracker, which shows the field forming. The moment the tournament
  publishes its competitors, `buildLiveModel` (`tournament_model.js`) builds the
  live standings from the served `/api/active-tournament` record, so the hero
  **blooms** from the tracker into the live standings. The tracker is the
  *seed* of the standings, and the same competitors carry across.
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

Each principle in Console's grammar traces to a public design authority. These
influences are cited in the documentation and never in the source code.

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
- **Layering & separation; micro/macro reading → the overview-and-drill-down
  information architecture.** The epoch overview is the macro read (a compact funnel / bump / mini-
  bracket); Match-ups and the candidate page are the micro read. The colour
  roles layer the good/bad/accent signal cleanly off the neutral ink ground.
- **Cause and effect → the lifecycle DAG (`parent → patch → board fan → Σ →
  gate → terminal`).** One candidate's life reads left to right as a causal
  chain from the patch, through the per-board results, to the gate.

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

The `CONSOLE` chrome brand, the monospace data face (the default Technical
pairing sets data, ids and code in Google Sans Mono beside Open Sans prose),
and the terminal-derived palettes together give the surface a
**terminal-and-console voice** while the chrome reads as text. The instrument reads like a power-user's console rather than a
consumer report.

### 8.4 The chess / tournament metaphor

The decision is a tournament: a reigning **champion** defends its title against
**challengers** at a **champion-gate**. The visual vocabulary makes the metaphor
literal — the solid crown (current champion) and the open crown (former
champion / displaced incumbent), the champion-gate as the terminal confirmation seat, and the
ladder / bracket / funnel as the bracket-sheet shapes of the configured
tournament structure. (The champion/challenger vs parent/child terminology is in
[VOCABULARY.md](VOCABULARY.md).)

## 9. Two rules a reader may look for and not find spelled out

- **Which crown a champion takes.** The single rule is the solid `crown` icon
  for the current champion and the open `crown-former` icon for a former
  champion. The crowns have one definition: `js/icons.js` exports
  `CROWN = { current: 'crown', former: 'crown-former' }`, re-exported by
  `svg.js`. Every
  emitter imports it — the `svg.js` funnel, swiss ladder, elim-radial and
  duel-flow gate labels, the `waterfall` / `reignGantt` / `roundTimeline` crowns,
  `views/structure.js` gate notes, legends and standings, the `live.js`
  activity feed, `tree.js` marks, the `dag.js` terminal, and `views/epoch.js`
  overview captions. No site types a crown character.
- **Which token names carry the typeface families.** The marks read two tokens,
  `--v2-sans` and `--v2-mono`, which each `[data-t-type]` rule sets to literal
  font stacks alongside `--n-font-head` and `--n-font-paper`. There are no
  `--v2-serif` or `--v2-display` tokens. The system is as documented in §3.
