# Variant T — "Console IV": the convergence-IV anchor

Console IV is the round-6 **convergence-IV anchor**: the direct synthesis the
operator asked for — **Variant P (Console III, judged the best-looking console)**
with three folds:

1. **S's first-class side-by-side COMPARE detail** — a "compare with…" affordance
   that splits the candidate detail into TWO candidates read side by side
   (lifecycle · promote gate · match-ups · per-board scoring, A | B), with
   champion-vs-challenger transcripts side by side on a board.
2. **Q's generous, proportional spacing** — a roomier rail, calmer detail column,
   more air between sections and inside panels.
3. **A working back/up button** (top-left) — the explicit fix over Q's buggy one.

Round 7 evolves the anchor by adopting two well-liked elements **elegantly**, plus
a new chrome control:

4. **The SLIM REEL on the epoch view** (adopted from Variant V) — a compact,
   fit-to-width "rounds" spine that REPLACES the old lineage-bumps chart.
5. **Compact MATCH CARDS on the generations page** (adopted from Variant W) — a
   champion-defends banner + a responsive wrapping grid of one card per
   challenger round.
6. ~~A density / "roominess" picker~~ — *removed in round 10; **cozy** is now the
   permanent baseline (see the round-10 polish above).*

Round 9 makes the console **scale to the operator's screen**:

7. **A PAGE-WIDE SCALE pill** — a draggable, keyboard-accessible slider in the
   chrome (≈70 %–150 %, 5 % steps, default 100 %) that scales the ENTIRE page —
   text AND diagrams — and reflows (no clipping). Distinct from density.
8. **A FLUID, resolution-responsive layout** — the detail pane and the
   side-by-side compare grid fill the available viewport width instead of being
   clamped to a narrow centred column, so the compare panes (and their SVGs)
   render as large as the screen allows: bigger diagrams on bigger monitors.

Default colour theme: **monokai**. Default typeface theme: **Technical**
(Open Sans body + JetBrains Mono for data / labels / code). Default page scale:
**100 %**. Miller columns (R) are back-burnered and not pursued here.

Self-contained under `js/variants/T/**` + `css/variants/T/console4.css` + the
entry `app_T.js`; reuses only the shared `js/core/*` data spine and imports from
no other variant directory (everything needed from P/S/Q is ported in). T is now
the **converged default UI** (index.html boots `app_T.js`; `?ui=v1`/`?ui=v2` are
the only fallbacks — exactly one UI loads at a time).

## Round-10 polish (the converged default)

Six operator-requested changes, all scoped to Variant T:

1. **Bare `#/` route prefix.** The vestigial `#/` bake-off namespacing prefix
   is dropped — T is the only variant UI, so routes are bare: `#/`,
   `#/e/<epoch>`, `#/e/<epoch>/gen/<gen>`, `#/e/<epoch>/board/<entry>`, … A
   legacy `#/…` link falls back to Environment (never blank).
2. **Density picker removed → COZY is the permanent baseline.** The
   compact/cozy/roomy picker is gone; the **cozy** `--dt-*` spacing tokens are
   baked unconditionally onto the variant root, and the JS SIZE tokens are fixed
   at the cozy values. The page-scale pill is the sizing control now.
3. **"Sans" typeface dropped.** It was redundant with Technical's Open-Sans
   body. The typeface picker is exactly **Editorial / Technical / Display**
   (default Technical); the dropped `sans` id normalises to Technical.
4. **Scale RESET affordance.** A small keyboard-accessible `⟲` button beside the
   scale pill snaps the page scale back to 100 % and persists (`resetScale()`).
5. **Ten Gogh colour themes** (real palettes from gogh-co.github.io/Gogh),
   bringing the total to **thirteen** (monokai stays default). See the chrome
   section below for the palette→token mapping. Round 9 added four more —
   **Paper** (light), **Zenburn**, **Selenized Black**, **Relaxed** (dark).
6. **Colour picker is a SWATCH DROPDOWN.** With thirteen themes, the inline colour
   buttons become a keyboard-accessible dropdown; each option shows a 5-swatch
   preview strip (ground · surface · ink · improve · regress) + the theme name,
   and the closed trigger echoes the current theme's swatch + name. (The
   typeface picker stays as inline buttons — only three options.)

## The headline (carried from P) — a data-model TREE sidebar

The top-tab nav is gone. A persistent LEFT TREE mirrors the real zicato
hierarchy and drives the single detail pane:

```
Environment (workspace)
└─ Epoch <id>                      (one node per epoch — MULTI-epoch nav)
   ├─ Generations
   │  └─ <gen> (champion / former champion / rejected / seed)
   ├─ Boards
   │  └─ <entry>
   ├─ Mutation surface
   └─ Publication
```

Expandable / collapsible; multi-epoch AND multi-generation; selection explicit +
URL-encoded so a cold deep-link hydrates BOTH the open branches and the detail.
Implemented in `js/variants/T/tree.js`; the shell (`shell.js`) assembles the
structural model from `/api/workspace` + `/api/lineage` + `/api/epoch.board` +
`/api/tournaments` (for `champion_lineage`).

**Reliable epoch listing (walkthrough BUG 2).** The epoch roster is derived from
FOUR authoritative sources, unioned, so an existing epoch ALWAYS lists on EVERY
route: (1) `/api/lineage` generations grouped by `epoch_id` — the source that is
populated wherever the detail pane shows the epoch; (2) `/api/workspace.epochs`;
(3) the `/api/epoch` contract; and (4) the epoch the route is pointing at
(`route.params.epochId`). Earlier the tree read only (2) ∪ (3) — both of which can
be empty/stale on some routes (e.g. a workspace digest that omitted `epochs`, or
an `/api/epoch` that 404s for a non-current epoch on the **publication** view) —
which left the rail showing *"No epochs in this workspace yet."* even though the
breadcrumb + detail clearly named the epoch and `/api/lineage` returned its
generations. The focused epoch's generation bundle now also resolves from
`/api/lineage` (falling back to `/api/epoch.experiments`), keyed by the contract's
epoch OR the routed epoch, so a deep-link / the publication route fills its
generations even when `/api/epoch` is sparse. The empty state appears **only**
when every source is genuinely empty.

**Current vs former champion.** Several generations may carry `promoted` over an
epoch's life, but only ONE is the **current** champion — the last id in
`champion_lineage`. The shell stamps `currentChampion` / `formerChampion` on each
gen; the tree badges the current crown with a solid **♚ "champion"** marker
(`gen-champ`) and every former champion with a distinct, dimmer **hollow-crown
♔ "former champion"** marker (`gen-former`). A legacy model with no
current/former split keeps the champion badge for any promoted gen (back-compat).
Rejected / seed branches are unchanged.

## Detail views (router prefix `#/`, path = tree path)

| route | view — one line |
| --- | --- |
| `#/` | **Environment** — the workspace as a fleet (overview strip, per-epoch loss trendline cards, cross-epoch trajectory). |
| `#/e/<epoch>` | **Epoch overview** — objective + proposer brief, the **slim REEL** (rounds along the champion spine; replaces the old bumps), the **compact board×generation drift-loss heatmap** (stays here per fix #6). |
| `#/e/<epoch>/gens` | **Generations** — the **champion-defends banner** + a wrapping grid of compact **MATCH CARDS** (one per challenger round), plus the dense candidate roster (role · parent · scalar · Δ vs champion). Cards + rows open that candidate. |
| `#/e/<epoch>/gen/<gen>[/<entry>]` | **Candidate** — lifecycle DAG (clickable patch node), per-board dot-plot, entry drill, **ALL match-ups**, and the **stacked promote gate**. |
| `#/e/<epoch>/gen/<gen>~cmp=<gen2>` | **Candidate · COMPARE** — the SAME pane split into TWO candidate panels A \| B (S's comparison-first detail). |
| `#/e/<epoch>/gen/<gen>/diff[/<mutId>]` | **Patch diff** — this candidate's side-by-side diff (baseline vs new content), reusing the mutation-viewer diff component. |
| `#/e/<epoch>/boards` | **Boards** — the board **trellis** (small-multiples; here per fix #6); cards route to the per-board view by entry id. |
| `#/e/<epoch>/board/<entry>[/<gen>]` | **Per-board** — one entry across every candidate (sorted dot-plot + table) with the **inline side-by-side transcript** when a candidate is selected. |
| `#/e/<epoch>/mutations[/<mutId>[/<gen>]]` | **Mutation surface** — site × generation matrix + side-by-side diff in one cohesive layout. A bare `<mutId>` pins the **SITE** (all generations that patched it, stacked); a trailing `<gen>` pins **ONE cell** (that single generation's diff). |
| `#/e/<epoch>/paper` | **Publication** — K's ACM renderer (GFM tables, combined table+chart, per-matchup detail), epoch-scoped. |

## The compare model (NEW — fold 1, from S)

The candidate detail is **comparison-first**. By default it reads ONE candidate.
A **"compare with…"** picker (`js/variants/T/compare.js`, `comparePicker`) sets a
`~cmp=<gen>` suffix on the hash (so the comparison **deep-links**); `splitFrame`
then renders TWO candidate panels side by side, each in its **own digest-gated
host** so one side changing never rebuilds the other. Each side carries the full
lifecycle DAG, the per-board scoring dot-plot, ALL match-ups, and the stacked
promote gate. Clicking any match-up row sets the other candidate as the compare
target. Champion-vs-challenger transcripts read side by side INLINE on the board
view (`views/board.js`). The split collapses to a single column when there is no
compare target.

## The back-button fix (NEW — fold 3)

A top-left **back/up control** (`shell.js`, `goBack` / `renderBack`, plus
`router.up`) navigates UP the selection hierarchy:
candidate → generations → epoch → environment; a COMPARE split collapses to the
bare candidate first; an entry/transcript selection steps up to its bare parent
first. **The bug to avoid** (Q's): Q rendered the destination into the SIDE
PANEL. T instead **navigates** (changes the route); the normal dispatch then
repaints the destination into the **MAIN DETAIL PANE** — the rail/tree host is
never touched. The button is inert at the environment root. Tested explicitly:
after a back action the rail host still holds the tree and the detail host holds
the destination view.

## The seven round-5 fixes (carried forward — not regressed)

1. **Promote gate on the candidate page** — `views/candidate.js` renders the
   stacked, non-overlapping gate (decision header · rules ladder, each rule its
   own row · separate champion-vs-challenger scalar-components block); present on
   BOTH sides of a compare split.
2. **Patch node → per-candidate diff** — the lifecycle "patch" node is clickable
   → `views/diff.js`, this candidate's SIDE-BY-SIDE diff from its own
   `/api/files/{epoch}/{gen}/patches` + baseline `/api/mutations/{epoch}/{id}`
   `.baseline.content` (the STRING, never the object).
3. **ALL match-ups for a candidate** — filters `/api/tournaments`.matchups where
   `champion==gen || challenger==gen`; v0 shows v0→v1 AND v0→v2.
4. **Board view first-class** — reachable from the tree's Boards group
   (`views/board.js`), keyed by entry id.
5. **Board entry → inline side-by-side transcript** — selecting a candidate on
   the board view shows its transcript INLINE, side by side with the champion's
   on that board (`/api/conversation/{run_id}` per candidate); no run page.
6. **Trellis vs heatmap de-dup** — heatmap stays at the epoch overview
   (`views/epoch.js`); the trellis lives in the Boards view (`views/boards.js`).
   Never both on one page.
7. **Q/M spacing + L's mutation-viewer quality** — applied throughout (fold 2).

## The slim reel on the epoch view (fold 4, adopted from V)

`js/variants/T/reel.js` (ported IN — no cross-variant import) renders a compact,
**fit-to-width** champion spine: station 0 is the seed/champion (♛), and each
round is a small **tick** on the spine carrying its ordinal (`r1…rN`), a
verdict-coloured dot, and the challenger id. The rounds come from
`/api/tournaments`.matchups (round-ordered by `ran_at`; lineage fallback). It
**replaces the old lineage-bumps** chart on the epoch view — the same
champion-vs-challenger-over-rounds story, so only ONE appears; the heatmap stays.

The big per-challenger cards are deliberately NOT hung off the reel (that does
not scale); the per-challenger detail lives in the generations match cards.

**Scaling to many generations.** The SVG has a FIXED viewBox (`0 0 1000 92`) laid
out left→right and is set to `width:100%` (NO pan/zoom, no horizontal scroll).
Stations are evenly distributed between `x0` and `xMax`, so as rounds grow the
step shrinks and the ticks **compress** — no element ever exceeds the viewBox
width. The selected/hovered tick highlights via a CSS state-class swap (never an
infinite keyframe). The reel's vertical scale is the fixed cozy
`--dt-reel-scale` while its width stays fit-to-container.

**Structure-aware (the reel is gauntlet-only).** The champion-spine reel is the
right picture for a **gauntlet** epoch — N sequential champion-vs-challenger
title defences — but it is the WRONG story for a non-gauntlet structure (racing
is successive halving, not a sequence of defences). So `views/epoch.js` reads
`ep.tournament.structure` (via `isNonGauntlet`) and, for a non-gauntlet epoch,
**replaces the reel** with a compact **structure strip** (`.dt-struct-strip`):
the structure label, the field size (`field of N`), the rung / round count, and
a **"See Match-ups →"** affordance that opens the real ladder / bracket /
standings. The gauntlet reel is unchanged; only the non-gauntlet case swaps it
out. The heatmap is unaffected either way.

## The match cards on the generations page (fold 5, adopted from W)

`views/gens.js` leads with a **champion-defends banner** (champion id · loss · N
title defences · promoted badge) and a **responsive wrapping grid** of compact
challenger match cards — `grid-template-columns: repeat(auto-fill,
minmax(--dt-card-min, 1fr))`, one card per challenger round. Each card:
`<challenger> vs <champion>` · verdict pill · Δscalar · a **one-line (truncated)**
hypothesis · the decisive-driver judge (from the round gate) · a status link
(dead-branch / promoted → opens the candidate). Clicking a card opens that
candidate. The dense roster table is retained below the cards for the
at-a-glance scan.

**Scaling to many generations.** Cards stay short (the full hypothesis lives on
the candidate page; the one-line idea truncates with ellipsis), and the grid
wraps to multiple rows, so it stays tidy whether there are 3 OR ~30 generations.
These cards appear on the **generations** scope only — never on the
environment / workspace view.

## The configured tournament STRUCTURE (bracket · standings · racing)

The match-ups page renders the **actual configured tournament structure**, not
just the gauntlet. `views/gens.js` reads `ep.tournament.structure` (from the
`tournament: {structure, params}` block on `/api/epoch`; absent ⇒ `gauntlet`)
and branches:

- **`gauntlet`** (default) — the champion-defends banner + match-card grid +
  roster table above, **unchanged**. A gauntlet epoch with no `tournament`
  block reads byte-identically to before (no structure pill, no bracket).
- **non-gauntlet** — fetches the full structure state from
  `GET /api/tournament-structure/{epoch_id}/{tournament_id}` (the new
  `data.tournamentStructure()` read; tournament id resolved from the
  `tournaments[]` array on `/api/tournaments`, falling back to the
  `{epoch}:{champion}->{challenger}` crowning-pair convention) and dispatches
  through `views/structure.js`:
  - **`single_elim` / `double_elim`** — a **fit-to-width bracket**
    (`svg.structureBracket`): columns = real `rounds[]`, nodes = real
    `{competitors, winner, decision, bracket_slot, bye}` matches, winners'→next
    connector lines; double-elim splits the matches into a **winners'** band
    and a **losers'** band by `bracket_slot` prefix (`WB-` / `LB-`). A standings
    leaderboard rides below.
  - **`swiss`** — a **standings table** hero (`dt-standings`, reusing
    `dn-board-table`) + a per-round **pairings** list from `rounds[]`.
  - **`racing`** — a **successive-halving rung ladder**
    (`svg.racingLadder`): one column per rung, escalating left→right to a
    trailing **champion-gate** column. Each rung shows its full field racing on
    its `board_fraction` of the board (shown in the column head, escalating ×η),
    each runner's **Δ-vs-champion** right-aligned, the `cut[]` worst-by-η struck
    through (✕ = cut, ↑ = survives) and faint survivor→next-rung **connectors**
    that trace the halving. The lone final survivor flows into the
    **champion-gate** seat; when the gate is settled and won it crowns the **new
    champion ♚** (a `dn-good` box, confirmed against `champion_lineage`), else it
    reads **"champion stands"**. A **live** race leaves a pending rung neutral
    (nobody cut until that rung's results land) and the gate reads "deciding…"
    rather than crowning a not-yet-committed winner.

    **Reconstruction from per-challenger records.** A racing tournament is NOT
    persisted as one assembled-rung record — it is persisted as **one record per
    challenger** on `/api/tournaments` → `{champion_lineage, tournaments:[…]}`,
    each `{ tournament_id:"<epoch>:<champ>-><chall>", structure:"racing",
    competitors:[champ, chall], rounds:[{match_id, opponent, won, delta_scalar}]}`.
    A single `data.tournamentStructure()` fetch therefore only sees ONE
    challenger's flattened rounds and cannot rebuild the ladder (the old code
    rendered an empty "RUNG · RUNG · CHAMPION-GATE: tbd" skeleton). So for the
    idle racing case `views/gens.js` calls
    `structure.reconstructRacing(/api/tournaments, epochId)`, which **aggregates
    every racing record** and **groups its matches by the `match_id` rung
    prefix** (`rungN_*` → rung N; `racing-final` → the full-board champion gate):
    - **field(rung N)** = challengers with a `rungN_*` match;
    - **survivors(rung N)** = those that ALSO appear at rung N+1 (or in the
      final); **cut(rung N)** = the rest;
    - the **champion gate** is the lone survivor's `racing-final` match — `won`
      (Δ negative ⇒ lower loss) ⇒ **promoted**, and the new champion is confirmed
      by `champion_lineage`'s last id.
    The reconstruction normalises into the SAME `{structure, competitors, rounds,
    standings, champion_lineage}` shape the LIVE `/api/active-tournament`
    produces (rung rounds carrying `{competitors, survivors, cut, board_fraction,
    deltas}` + a `racing-final` gate round), so the one `renderRacing` handles
    both. `reconstructRacing` also passes an already-assembled record (the LIVE
    shape, or a test fixture) straight through.

A **structure pill** (`dt-structure-pill`: "structure · Swiss (4 rounds)" etc.)
labels the configured structure in both the **epoch** header (`views/epoch.js`)
and the **match-ups** header. The SVG marks follow T's fit-to-width discipline
(`width:100%` + viewBox, no pan/zoom, token-themed across all thirteen swatches,
scaling with the page-scale pill). Since the live workspace is gauntlet-only,
the non-gauntlet renderers are driven + tested with **mock structure payloads**
(`test/variant_t.test.mjs`) and degrade gracefully (an honest empty state, no
throw) when the structure payload is absent.

### Surfacing the LIVE tournament during a run

The completed `/api/tournaments` record only commits the promote decision at the
**very end** of a run, so reading it mid-run shows an empty ladder ("No
tournament has run yet") and mislabels the eventual winner as
rejected/eliminated. `views/gens.js` therefore prefers the **LIVE**
`/api/active-tournament` (`data.activeTournament()`, never cached) whenever a run
is in flight — detected with the same structure-agnostic
`deriveLiveStatus({heartbeat, activeRuns, activeTournament})` the chrome reads.
Both endpoints share the `{structure, competitors, rounds, standings}` shape;
`structure.normalizeStructure(st, live)` folds them into one renderer input and
stamps `live` when the active record carries a non-idle `phase`. In `live` mode
the renderers suppress committed verdicts — standings read **racing** instead of
champion/eliminated, a rung with no recorded cut/survivors is shown **pending**
(neutral, nobody struck), and a **LIVE** badge (`.dt-live-pill`) rides beside the
structure pill. When idle, the view falls back to the completed record exactly as
before. The lineage tree's crown likewise reflects the in-flight state through
the heartbeat-driven model rebuild.

## The live-status indicator — structure-agnostic (running for ANY structure)

The chrome status pill (`.dt-status`) reports whether a run is **ACTIVE for any
tournament structure**, not just the gauntlet. The earlier pill was
gauntlet-shaped: it lit only off `state.activeTournament`, so a live **racing**
(or swiss / single_elim / double_elim) run read as "nothing is running" even
though the run was plainly in flight.

`livestatus.deriveLiveStatus({heartbeat, activeRuns, activeTournament})` folds
the three live read signals — all already in `AppState` (the consolidated
`/api/environment` read populates `heartbeat` / `activeRuns` /
`activeTournament`, and the SSE `heartbeat` event keeps `heartbeat` fresh) —
into one structure-agnostic verdict:

- **`/api/heartbeat.phase`** is the primary signal. A **non-idle** phase ⇒
  running — covering both `proposing:…` and every `tournament:…` structure.
  `isActivePhase()` treats `idle` / `done` / `complete` / `finished` / `stopped`
  / `error` (and an empty/absent phase) as at-rest; anything else is active. The
  phase may be a colon-path (`tournament:round_0:rung0_m3`); the first segment
  names the stage.
- **`/api/active-runs.length`** is the in-flight board-unit count (was 14
  mid-rung) — corroborates, and surfaces a "· N units" tail on the badge.
- **`/api/active-tournament.phase === "running"`** corroborates and supplies the
  `structure` for the label.

`phaseLabel(phase, structure)` derives a readable label — e.g. racing →
`racing · rung 0`, swiss → `swiss · round 2`, proposing → `proposing field`.
When running, the pill carries the `dt-running` state and shows a **RUN badge**
(`.dt-run-badge` — a pulsing dot + the structure/phase label + the in-flight
count); idle/done hides the badge. `renderStatus()` is **digest-gated**
(`liveStatusDigest`): a steady heartbeat re-tick with an unchanged verdict
writes ZERO DOM (no flash). The badge reads in all thirteen themes (good/ink-faint
tokens) and the pulse honours `prefers-reduced-motion`.

## Board-detail — in-flight runs (a mid-run entry is never blank)

The per-board view (`#/e/<epoch>/board/<entry>`) surfaces the candidates
**currently executing** on that one entry. `inflightForEntry(state.activeRuns,
entryId)` filters the structure-agnostic active-runs feed to the page's entry
(matching `entry_id` / `board_entry_id` / `entry`); each in-flight candidate
renders with its generation id, run id, and a progress bar (`progressRatio`
normalises 0..1 or 0..100, falling back to `elapsed/budget`). The completed
per-candidate breakdown still renders for finished runs; an entry mid-run with
no completed results yet now reads **"N candidates running"** with progress
rather than appearing empty (and a truly-quiet entry shows an honest "no
candidate has run yet" rather than a blank). The panel folds into the view's
digest so it stays live-updating + flash-free on the same SSE/poll cadence.

## The chrome controls (colour dropdown · typeface buttons · scale pill)

- **Colour — a SWATCH DROPDOWN** of **thirteen** themes (round 10; +4 in round 9). The closed
  trigger (`.dt-cd-trigger`) shows the current theme's swatch strip + name;
  opening reveals a listbox (`.dt-cd-list`) with one `.dt-cd-option` per theme,
  each a 5-swatch preview strip (`.dt-swatch-strip`: ground · surface · ink ·
  improve · regress) + name. Keyboard-accessible: Enter/Space/ArrowDown open;
  ArrowUp/ArrowDown move the active option; Enter/Space select; Esc closes; a
  click outside closes. Choosing a theme sets `[data-t-theme]` on the variant
  root (CSS-only re-skin), persists (`zicato.T.theme`), and updates the trigger.
  The heatmap ramp + every mark derive from the active theme tokens.

  The thirteen themes (monokai stays default): the three originals — **monokai**,
  **solarized-dark**, **solarized-light** — plus ten **Gogh** palettes
  (gogh-co.github.io/Gogh): **google-light**, **google-dark**, **lunaria-light**,
  **lunaria-eclipse**, **belafonte-day**, **belafonte-night**, and (round 9)
  **paper** (light), **zenburn** (dark), **selenized-black** (dark), **relaxed**
  (dark). Each Gogh terminal
  palette is mapped to T's `--v2-*` token contract by a single principled rule:
  `paper ← background`, `panel ← background nudged toward fg/host`, `ink ←
  bright-white/host` and `ink-soft ← foreground`, `ink-faint ← fg mixed toward
  bg`, `rule`/`rule-soft ← bg mixed toward fg`, `good ← green`, `bad ← red`,
  `caution ← yellow`, `accent ← cyan` (or the palette's **blue** where the cyan
  is a low-contrast neutral, as for Belafonte), `flat ← bright-black/grey`,
  `cell-empty ← bg mixed toward fg`. A few accents/cautions were nudged for
  contrast on their grounds so every mark, diagram, and the heatmap ramp read in
  all thirteen. **"Belafonte Light" does not exist in Gogh** — per the brief we ship
  Belafonte **Day** (the light variant) + **Night** (dark).

  The four round-9 palettes resolved to the exact Gogh filenames `Paper.yml`,
  `Zenburn.yml`, `Selenized Black.yml`, `Relaxed.yml` (no name resolution
  needed). Two legibility nudges: **Paper** (a light theme) keys `ink` off
  near-black — its palette white `#aaaaaa` is too low-contrast for ink on the
  cream ground — and keys `accent` off the palette's blue (its cyan `#158c86` is
  a deep teal); **Zenburn**'s "green" channel is a yellow-green that collides
  with its yellow, so `good` is nudged to a true sage green and `accent` to
  Zenburn's canonical cyan `#8cd0d3` (the yml cyan `#93bea3` is a near-neutral
  grey-green), keeping improve/caution/accent separable.
- **Typeface — inline buttons** (only three, so no dropdown needed):
  **Editorial** (+ Source Serif 4) · **Technical** (default; + JetBrains Mono) ·
  **Display** (+ Archivo Narrow), swapped via `[data-t-type]`, persisted
  (`zicato.T.typeface`). The old redundant **Sans** option is dropped (Technical
  already gives an Open-Sans body; the `sans` id normalises to Technical). Google
  Fonts loaded in `app_T.js` with `display=swap` and system fallbacks — the only
  external dep.
- **Density — removed; COZY baked in.** There is no density picker. The **cozy**
  `--dt-*` spacing tokens (`--dt-rail`, `--dt-pad-x/-y`, `--dt-section-gap`,
  `--dt-panel-pad-*`, `--dt-row-gap`, `--dt-card-*`, `--dt-reel-scale`, the global
  `--dt-font-scale`) live **unconditionally** on the variant root, and the JS
  SIZE tokens (`ui.densityTokens()` → `{ sizeScale, fontScale, nodeRadius,
  dagRowStep, heatCell, dotRow, sparkbarH, reelScale }`) are **fixed at the cozy
  values** — the views still feed them to the figures' INTRINSIC dimensions
  (DAG height/row step, heatmap cell, dot-plot rows, sparkbar height) so every
  figure stays fit-to-width. The shell stamps `data-t-density="cozy"` for any
  rule that keys on it, but it never changes.

## The page-wide SCALE pill (the sole sizing control) + reset

The chrome's sizing control is a **draggable range slider** (`.dt-scale-range`
inside a `.dt-scale-pill`, beside the colour + typeface pickers) with a small
**% readout** and a **reset button**. It is a continuous control over
**≈70 %–150 %** in **5 % steps**, **default 100 %**, and is
**keyboard-accessible** (a native range input: arrow keys step ±5; it carries
`aria-valuemin/max/now` + an `aria-label`).

- **Mechanism.** `shell.applyScale(n, root)` (mirrors `applyTheme`) normalises
  `n` (clamp to range + snap to the 5 % grid), then applies it **page-wide** by
  setting **`zoom`** on the Variant-T app **ROOT**
  (`#variant-root[data-variant="T"]`) — e.g. 130 % → `root.style.zoom = 1.3`. It
  also stamps the raw ratio as `--dt-page-scale` and records `data-t-scale="130"`
  on the root, and updates the slider + the % readout. `zoom` **reflows** (it is
  not a CSS transform), so the page re-wraps at the scaled size and never clips.
- **RESET (round 10).** A small `.dt-scale-reset` `<button>` (`⟲`, with an
  `aria-label`) beside the pill calls `shell.resetScale()` → `applyScale(100)`,
  snapping the page back to 100 % and persisting it. It is inherently
  keyboard-accessible (focusable; Enter/Space activate).
- **Page-wide, NOT per-pane.** The scale is applied at the app root only — there
  is deliberately **no** per-pane zoom control. In the side-by-side compare view
  the two panes scale together with the rest of the page.
- **Persistence.** `readScale` / `persistScale` use their own key
  (`zicato.T.scale`); the value is restored and re-applied on every mount, and is
  untouched by colour/typeface changes (orthogonal axes).

## Fluid, resolution-responsive layout (round 9)

The content now **fills the available viewport width** instead of sitting in a
narrow centred column that wasted space on wide monitors. The detail pane
(`.dt-viewhost`) and the legacy `.dn-viewhost` were clamped to **1160 px / 1320 px
centred**; both are now **`width:100%`** with only a very generous, *non-centred*
cap (`max-width: min(100%, 2400px)` / `min(100%, 2200px)`) that merely guards
prose line-length on ultra-wide displays. Because the detail pane fills its
column, the side-by-side compare grid (`.dt-split`, a `1fr 1fr` grid that only
collapses to one column below 1080 px) splits the **FULL content width** — so
each pane, and every fit-to-width SVG inside it, renders **as large as the screen
allows**. Net effect: bigger diagrams on bigger screens, still tidy on small ones.
This composes with the scale pill (the whole fluid layout is then `zoom`-scaled)
on top of the fixed cozy padding rhythm.

## Fit-to-width — visual elements never escape their pane (round 8)

Every visual element is a **responsive SVG** (`width: 100%` + a `viewBox` +
`preserveAspectRatio`, no fixed pixel width that exceeds the panel and no
horizontal-scroll wrapper around the whole figure). This now holds for the
**lifecycle DAG** (`dag.js` — was a fixed 900 px SVG inside an `overflow-x:auto`
panel; the right-hand stages spilled out and forced sideways scrolling — now the
viewBox-internal coordinate width scales down to fit the pane, so all six stages
parent → patch → board → Σ → gate → terminal are visible without scrolling), the
**Tufte sankey**, the **heatmap** (epoch overview — the `overflow-x:auto` panel
wrapper is gone), the **bumps**, the per-board / candidate / board-view
**dot-plots** (`valueDotPlot`), the per-judge **value-bars**, the matchup
**slopegraph**, and the trellis **sparkbar / gen-dots**. Inherently-wide tabular
content carries its OWN contained overflow — the **publication** GFM tables, the
aggregate-scores table, the per-matchup-detail tables, and the **mutation
matrix** are each wrapped in a `.dn-table-scroll` box (`max-width:100%;
overflow-x:auto` on the wrapper only), so a wide table scrolls WITHIN its box and
never pushes the paper/panel layout sideways. As a backstop, `.dn-panel` itself
is `max-width:100%; overflow-x:hidden` so no element can visually escape a panel.

## Lifecycle DAG — BOARD column dedupes per ENTRY (rung multiplicity)

The lifecycle DAG (`dag.js`) is the clean cause→effect SUMMARY of one
candidate's life: `parent → patch → BOARD fan → Σ → gate → terminal`. The BOARD
column is a vertical fan of per-entry nodes (a loss disc + an entry label). For a
**RACING** candidate the same board ENTRY is run multiple times across rungs
(rung0 slice → rung1 larger slice → racing-final full board), so the raw
`/api/generation/.../per-entry` stream repeats an `entry_id` N times. Rendered
naively that fan showed the same entry as ~20 confusing duplicate nodes
(`q3_metrics_outline` ×3, `waffles_single` ×4, …) and the labels overlapped the
discs.

The column now **dedupes to one node per distinct entry** (grouped by
`entry_id`, first-seen order; ≤ ~7 nodes, not ~20). Each node shows a
**representative loss** — the entry's LAST run (the racing-final / full-board
run) — and, when the entry was raced more than once, a **rung-multiplicity
badge** (`×N`, class `ezn-board-mult`) to the right of the disc plus a dashed
`ezn-board-raced` disc marker, so the repetition reads as "the same board
re-raced across rungs", not random duplicates. The Σ-loss aggregate sums the
representative (deduped) losses. Clicking a node still drills into that entry's
per-board detail (which shows ALL its runs). The per-rung detail lives in the
racing ladder (Match-ups) + the per-board scoring dot-plot — NOT in this
summary.

**Text spacing:** the entry label is **end-anchored to the LEFT of the disc**
(`x = cx − (r + 8)`, `text-anchor="end"`) so it can never sit on the circle or on
the loss text (which lives INSIDE the disc); long ids are clipped with an
ellipsis (title tooltip carries the full id + per-rung note); rows are spaced by
the density-scaled fan step. For a **GAUNTLET** candidate (one run per entry) every
group has size 1 → dedupe is a no-op, no badge, no raced marker — rendering is
unchanged. The DAG stays fit-to-width (`width:100%` + viewBox), theme-aware
across the 13 themes, and scales with the page-scale pill.

## Render discipline (carried forward)

Digest-gated repaint (structural data only, heartbeat = no-op; each compare side
independently gated); host cleared on selection change — and a `~cmp` change is
part of the selection; one persistent host per pane; caches invalidated only on
selection change; CSS `transition`, never `animation:…infinite`;
constrained-scroll transcripts; cold deep-link hydration of tree + detail +
compare target; no pan/zoom viewport diagrams (fit-to-width); theme-aware
heatmap; Tufte sankey with label ≠ value; side-by-side diff with real strings.

## Tests

`test/variant_t.test.mjs` (76 tests) covers, carried forward: the tree renders
Environment → Epoch → {Generations, Boards, Mutation surface, Publication};
multi-generation nav; the candidate-page promote gate; the patch-node click →
per-candidate diff with real strings; v0 showing ≥2 match-ups; the board view
reachable from the tree + inline side-by-side transcript on run select; the
**side-by-side COMPARE splitting the detail into two candidates**; the **back
button navigating UP and rendering the destination into the MAIN detail pane
while the rail host stays the tree**; trellis in Boards / heatmap in epoch; the
typeface picker + compare primitives; digest no-ops; the **slim reel** (spine +
ticks, fixed `0 0 1000 92` viewBox, ticks compress under a ~12-gen/11-round
fixture); the **champion-defends banner + one match card per challenger**; match
cards absent on the environment view; the **lifecycle DAG / sankey / heatmap as
fit-to-width responsive SVG** (`width:100%` + viewBox, no horizontal-scroll
wrapper); the **publication** view's wide tables contained in `.dn-table-scroll`;
the scale pill's **70–150 % / 5 %-step / 100 %-default** surface, its **page-wide
ROOT zoom** (not per-pane), persistence + restore, and keyboard accessibility;
and the **fluid layout** (no narrow caps; detail pane `width:100%`; `1fr 1fr`
compare split).

The round-10 polish adds: **(a)** routes use the bare **`#/`** prefix
(`router.PREFIX === '#'`, no `/T`), a deep route parses, and an href round-trips
(a legacy `#/T/…` link falls back to home); **(b)** the **density picker is gone**
(no `DENSITY_THEMES` / `readDensity` / `applyDensity`), `ui.DENSITY === 'cozy'`,
the SIZE tokens are fixed at the cozy values, the mounted root carries
`data-t-density="cozy"`, and the CSS has no density-conditional selectors;
**(c)** the typeface options are **exactly** `editorial/technical/display` (no
`sans`, default Technical); **(d)** the **scale RESET** button returns the page to
100 % and persists (and `resetScale()` does the same); **(e)** **all thirteen** colour
themes are registered, each defines the full `--v2-*` token contract in the CSS,
and selecting each (incl. the ten Gogh palettes) applies + persists it — and a
dedicated test pins the four round-9 additions (Paper/Zenburn/Selenized Black/
Relaxed) registering with swatch strips, full token contracts that differ from
the default, and a root-attribute change on select; **(f)**
the colour picker is a **swatch dropdown** — a trigger with the current swatch +
name, thirteen options each with a ≥4-swatch strip preview, clicking an option
applies + persists, and the keyboard (ArrowDown opens, Esc closes) works.

The **live-status** fix adds: **(a)** a live **racing** run (a non-idle
heartbeat phase + a non-empty active-runs feed + active-tournament
`phase:"running"`) derives a **RUNNING** verdict naming the structure (`racing`)
and phase (`rung 0`) with the in-flight count; the heartbeat phase **alone**
(`proposing:field`) and active-runs **alone** each light the running state
(structure-agnostic), and `isActivePhase` separates running phases from
idle/terminal ones; **(b)** an **idle** heartbeat + empty active-runs + null
(or `complete`) tournament reads **idle/done** (not running); the chrome RUN
badge lights for a live racing run, names the structure, shows the unit count,
and is **digest-gated** (an unchanged re-tick does not rewrite the badge);
**(c)** the **board-detail** view, given active-runs matching the `entry_id`,
renders the **in-flight candidates** with progress (filtered to that entry,
excluding other entries), while an entry **with** completed results still
renders the per-candidate breakdown (no in-flight panel when nothing is live).

The **walkthrough** fixes add: **BUG 1** — the mutation-surface route carries an
optional per-cell generation (`#/e/<epoch>/mutations/<mutId>/<gen>`); clicking a
▪ **CELL** (carrying `data-gen` + `data-site`) renders **exactly ONE**
generation's side-by-side diff for that site, while clicking the **SITE row
label** (a bare `<mutId>` link, made visually distinct from the cells) renders
**ALL** generations that patched the site, stacked — both with real-string
content (never `[object Object]`), and a scope chip names whether the pane shows
*one generation* or *all*. **BUG 2** — given `/api/lineage` generations across an
epoch (with a sparse workspace + a 404 `/api/epoch`, the publication-route case),
the tree **lists** that epoch and does **not** show *"No epochs in this workspace
yet."*; the empty state appears only when every authoritative source is genuinely
empty.

The **lifecycle BOARD-column** fix adds: a **RACING** candidate whose per-entry
stream repeats an `entry_id` across rungs renders **one node per distinct entry**
(count == distinct entries, not total runs), each raced entry carries a `×N`
multiplicity badge + an `ezn-board-raced` marker, and the node shows the
representative (final full-board) loss, not the rung0 loss; the entry **label is
end-anchored left of the disc** (`x ≤ cx − r`) and adjacent rows keep a ≥24 px
gap, so a label never overlaps the disc; a **GAUNTLET** candidate (one run per
entry) renders unchanged (one node per entry, multiplicity 1, no badge, no raced
marker); and `.ezn-board-mult` / `.ezn-board-raced` are themed via the scoped
`--v2-*` token contract.
