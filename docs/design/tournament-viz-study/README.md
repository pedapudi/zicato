# zicato tournament-/evolution-visualization study

A set of standalone, self-contained, theme-adaptive study pages (one HTML file
per visualization **level**) that explored and signed off how the Console
dashboard draws the evolutionary tournament — from the whole workspace down to a
single generation. Preserved here for posterity: this is the archived design
record behind the tournament views, **not** a TODO list. **All of the picks
below are now implemented in the live Console dashboard.**

Open any file in a browser to view it offline. Each page carries the full
16-theme Console swatch picker (top-right) so every figure recolours live, and a
candidate-dropdown for switching structure where the level is structure-aware.

## How to read the study

- **One file per level.** A "level" is a decision/surface the dashboard
  visualizes (e.g. the cross-epoch home, one tournament round, one matchup).
- **Numbered options per level.** Each page lays out competing design options
  (1..N) for that level, each with a rationale and a live figure rendered from a
  shared sample dataset.
- **`compose.html`** is the composer: it assembles dashboard *pages* (home /
  epoch / round / matchup / candidate / custom) by picking a level option each,
  and it records the operator's **LIKED** options — their final picks — per
  level (and per *structure* for the single-round level). Liked options are
  **marked** (♥), not filtered, so every option stays selectable. See the
  `LIKED_OPTS` / `LIKED_SINGLE_ROUND` maps in `compose.html`.
- **`_embed.js`** is the shared iframe-embed shim the composer uses to inline a
  level page as a live, theme-synced figure.

## Files

| file | level it covers |
| --- | --- |
| `hero.html` | the always-on **hero / live-status** strip (structure-aware) |
| `cross-epoch.html` | **home** — every epoch of the workspace at a glance |
| `cross-round.html` | one **epoch**, round by round |
| `racing.html` | one **round**, structure = **racing** (successive-halving) |
| `gauntlet.html` | one **round**, structure = **gauntlet** (vs the fixed champion) |
| `single-elim.html` | one **round**, structure = **single-elimination** |
| `double-elim.html` | one **round**, structure = **double-elimination** |
| `swiss.html` | one **round**, structure = **swiss** |
| `single-matchup.html` | one **matchup** — a head-to-head champion-vs-challenger duel |
| `single-generation.html` | one **candidate / generation** — its full lifecycle |
| `compose.html` | the **composer** + the liked-picks registry |
| `_embed.js` | shared embed shim |

## The final picks (authoritative — confirmed against `compose.html`)

| level | LIKED option(s) | what won |
| --- | --- | --- |
| hero | **3** | compacted live structure figure (mini survival funnel) |
| cross-epoch | **7** | composed meta-loop ledger (staircase + change rail + heatstrip) |
| cross-round | **4** | field strip / dot plot per round |
| racing | **1** | scalar track (number-line race lane) |
| gauntlet | **5** | replicate distribution (reliability-native) |
| single-elim | **6** | radial bracket (rings inward to the center gate) |
| double-elim | **7** default · **8** optional prototype | mirror-lane bracket combo · radial bracket |
| swiss | **8** | ledger × rank-flow |
| single-matchup | **6** | component / judge slope |
| single-generation | **2** | candidate dossier (as shipped) + folded-in radar |

> The single-round picks (racing / gauntlet / single-elim / double-elim / swiss)
> are stored per-structure in `LIKED_SINGLE_ROUND`; double-elim is the only level
> with two liked options — opt **7** is the default, opt **8** is kept as an
> optional prototype.

---

## Levels, options, and what was picked

Below, every option on every page is described from the actual HTML. **Bold** =
the LIKED final pick.

### hero — live status strip (`hero.html`)

The always-present strip at the top of every dashboard page: what's running right
now, how far through, and who's ahead. Structure-aware (the figure adapts to
racing / gauntlet / single-elim / double-elim / swiss).

| opt | title | what it shows |
| --- | --- | --- |
| 1 | Status pill + KPI stat strip | the at-a-glance fact sheet — liveness dot, run badge (structure·phase·epoch), in-flight, rung & matchups, champ vs projected challenger, best-loss — each fact its own tile |
| 2 | Progress bar + caption + dumbbell | how far *through* the run (filled bar + "rung 1 of 3") paired with a dumbbell of champion↔challenger scalar gap and the projected pull toward the promote bar |
| **3** | **Compacted live structure figure (mini funnel)** | **the actual structure in miniature — a survival funnel with the live rung lit, in-flight matchups dashed, the projected survivor ghosted; double-elim draws BOTH brackets as separate labelled lanes converging into the grand final** |
| 4 | Bullet graph — projected scalar vs promote bar | the single decision number: is the challenger's projected loss under the promote bar? Bullet with champion as comparison marker and the gate as the target line |
| 5 | Radial progress ring + live sparkline | tournament fraction as a blink-readable ring + best-loss trajectory sparkline whose latest (projected) point pulses |
| 6 | Activity ticker + crown/role chips + match marquee | what's running NOW — role chips (♛ champion / challenger), a board-progress marquee per live match, and an append-only event ticker |

### cross-epoch — the workspace home (`cross-epoch.html`)

The home view: the whole meta-loop across epochs — is the loss floor making net
progress contract-to-contract, which lever moved at each roll, and is effort
buying floor.

| opt | title | what it shows |
| --- | --- | --- |
| 1 | Loss-floor step chart + contract-change marker rail | floor resets are discontinuous events: a step chart holds the floor and snaps at each roll; a component-coded vertical marker rail says *which* contract component changed |
| 2 | Handoff slope chart over the v0_parent seam | each epoch→epoch handoff (`prev:final` → `this:best`) as one tilted segment — slope sign = did the new contract beat where the last one ended |
| 3 | Contract-component heatstrip (epochs × components) | a categorical matrix: epochs down, the 5 surfaced components + proposer across; filled = changed vs predecessor (exposes the proposer column the dashboard diff omits) |
| 4 | Icicle of Epoch ⊃ Round ⊃ Generation | width ∝ `generation_count` (effort), hue ∝ `best_scalar` (payoff) — is effort buying floor or is a big epoch a low-yield slog |
| 5 | Champion-lineage alluvial across the v0_parent seam | lineage as a flow: each epoch's champion seeds the next as its v0_parent; ribbon width ∝ promoted_count, notched where a roll breaks comparability |
| 6 | Epoch-lifetime Gantt + per-epoch floor dumbbell | lifetime bars (open vs closed) with a predecessor-floor ●——● this-floor dumbbell docked to each — duration and floor-progress in one row |
| **7** | **Composed meta-loop ledger** | **braids the three liked sub-views: the held-floor staircase (opt 1) over effort-proportional bands (opt 4), every roll seam carrying opt 1's component-coded change chip, and a contract-component heatstrip (opt 3, incl. the proposer column) docked under each band — trajectory + attribution + effort/champion in one scan** |

### cross-round — one epoch, round by round (`cross-round.html`)

This epoch's rounds: the loss floor each round defended, who held the reign, and
how the field churned. (Liked in the registry but not surfaced in the default
page templates; included for completeness.)

| opt | title | what it shows |
| --- | --- | --- |
| 1 | Loss-floor waterfall / cascade | one step per round: floor drops on promotes (green, magnitude labelled), flat-dashed on holds — the clearest "is the floor descending?" |
| 2 | Champion-reign Gantt / ribbon | each champion is a horizontal ribbon spanning the rounds it held the crown; ▲ marks each handoff, current reign accented |
| 3 | Challenger-rank bump chart | rank the field by scalar per round; a green spine threads the promoted entry round to round |
| **4** | **Field strip / dot plot per round** | **every challenger is a dot on a shared scalar axis, one column per round, with the champion reference line and the dashed running floor crossing all columns; cached-champion columns flagged** |
| 5 | Per-round slope (floor → promote) | each round is a 2-point slope (incoming floor → promoted loss); downward = improvement, flat-dashed = hold |
| 6 | Per-round small-multiples glyph | each round a tiny self-contained card (field dots, champion tick, verdict chip, floor delta); current-champion card accent-bordered |

### racing — successive-halving round (`racing.html`)

One round under the racing structure: challengers raced on growing board slices
(25% → 50% → 100%), the field halved each rung, the survivor pointed at a pending
full-board champion gate. Each option offers a hero / single-round / within-round
view.

| opt | title | what it shows |
| --- | --- | --- |
| **1** | **Scalar Track** | **every gen on one shared scalar number-line with the champion as a dashed benchmark; marker SIZE = inverse loss (bigger = better) so the leader is the fattest dot and cuts shrink away — cut closeness becomes literal distance** |
| 2 | Funnel Ribbons | the survival topology, graded: node ring + flow width scale with `board_fraction`; each cut terminates in a red stub annotated with its scalar |
| 3 | Evidence Ladder | bump-chart of scalar across rungs, x-axis weighted by cumulative board_fraction so rungs widen as evidence accumulates |
| 4 | Standings Table+ | a real leaderboard: inline scalar-vs-champ bar, an evidence pip-strip (one pip per rung), a cut-rung status chip, signed Δ-champ column |
| 5 | Confidence Bands | each gen a scalar dot with an error band whose width is *inversely* proportional to board_fraction — a partial-board cut gets a wide, uncertain band |
| 6 | Rung Stack | a vertical bracket where each rung's height = board_fraction, so the field visibly sinks into deeper evidence; survivors solid, cuts hollow |

### gauntlet — vs the fixed champion (`gauntlet.html`)

One round under the gauntlet structure: a single challenger run against the fixed
champion standard, board by board. Hero / single-round / within-round views each.

| opt | title | what it shows |
| --- | --- | --- |
| 1 | Margin gauge | delta_scalar on a gate-margin axis — the gate is a hard line and the bar reaches across it; reads clear / by-how-much |
| 2 | Per-board win/loss strip | one cell per board, green=better / red=regression; spread = "on how many boards", gated regression outlined |
| 3 | Champion-vs-challenger scalar bars | two opposed scalar bars (lower=better) with the delta bracket and the gate line |
| 4 | Crown handoff | the crown line carries the champion; the challenger forks in and, clearing the gate, takes the crown |
| **5** | **Replicate distribution** | **reliability-first: each side's replicate scalars as a dot strip with a mean±σ band — band separation = confidence in the decision** |
| 6 | Promote/hold verdict card | decision-first composite: a big verdict + the four gate signals as a checklist, with standings |

### single-elim — single-elimination round (`single-elim.html`)

One round under single-elimination: a knockout bracket of challengers narrowing
to a finalist who faces the champion gate. Hero / single-round / within-round.

| opt | title | what it shows |
| --- | --- | --- |
| 1 | Standings ladder + scalar number-line | the leaderboard IS the chart: rank-ordered rows on a shared scalar axis, dashed champion-gate line, finalist haloed |
| 2 | Bracket tree with FUSED standing chips | a real bracket where each seat carries rank/W-L/scalar; winner edges green, losers fade red + strike, survivor→gate edge dashed |
| 3 | Survival timeline (rounds × rank) | rounds left→right, gens as vertically rank-ordered tracks ending with ✕ at elimination; survivor/champion reach the gate |
| 4 | Champion-gate tug-of-war + ranked grid | leads with the finalist-vs-champion duel as a tug-of-war (knob position = gate Δ), with a compact ranked field grid below |
| 5 | Ranked scalar bars + gate clearance band | bars sorted by scalar (shorter=better), state-colored, with a shaded "clears the gate" band behind everything left of the champion line |
| **6** | **Radial bracket (rings inward to the center gate)** | **rounds as concentric rings, spokes advancing inward as gens win; per-round survival colors each segment (survived green, eliminated red + ✕), champion holds the center gate; a rank-ordered standings list flanks it so the novel shape never costs legibility** |

### double-elim — double-elimination round (`double-elim.html`)

One round under double-elimination: winners' (WB, ●● two lives) and losers' (LB,
●○ one life) brackets converging at a grand-final crossover. The hard design
problem here was keeping WB vs LB **clearly distinct** and making a WB→LB **drop**
read spatially. 8 options.

| opt | title | what it shows |
| --- | --- | --- |
| 1 | Two-lane ledger (split rail) | WB lane top / LB lane below with a labelled divider; curved drop-arrows trace a gen falling WB→LB; life pips on every chip; GF gate as a right-edge margin bar |
| 2 | Lives-as-pips standings table | a rank-ordered leaderboard where lives are the dominant column (●● / ●○ / ○○), bracket side as a coloured left border + WB/LB/CH/OUT badge; most legible "who's alive and where" |
| 3 | Mirror bracket (WB top / LB bottom → GF) | the textbook shape: WB flowing right on top, LB mirrored below, converging into the grand-final gate; drops physically fall from a WB node into the LB band |
| 4 | Lives-remaining swimlanes | lanes keyed on LIVES not rounds (2 lives / 1 life / eliminated); a gen migrates DOWN a lane per loss, so "second life" is literal vertical position; x-axis = scalar |
| 5 | Bracket-side columns + drop traces | WB and LB get their own column families with life pips + scalar; curved drop-traces link a gen's WB origin to its LB re-entry; champion + GF gate in a far-right gutter |
| 6 | Survival timeline (life-line DAG) | each gen a horizontal life-line whose colour is its lives state (green undefeated → caution after first loss → red ✕ at second); the dot marks the WB→LB drop |
| **7** | **Mirror-lane bracket (opt 1 + opt 3 combo) — DEFAULT** | **opt 1's full-width tinted lane bands + lives gutter + true WB→LB drop connectors, fused with opt 3's stacked bracket nodes (both competitors per match, winner emphasised, life pips) and a literal GF crossover box — match-level detail without losing the at-a-glance "which lane, how many lives". Connectors use clean orthogonal-pipe (drop-bus elbow) routing so WB→LB drops don't cross.** |
| **8** | **Radial bracket (polar progression) — OPTIONAL PROTOTYPE** | **a polar layout: the GF crossover at the CENTRE, rounds fanning OUTWARD as concentric rings (time reads inward); WB owns the upper arc (accent, ●●), LB the lower arc (caution, ●○), split by a dashed equator; a WB loss is a drop-arc crossing the equator. Kept as an optional prototype, not the default.** |

### swiss — swiss round (`swiss.html`)

One round under swiss pairing: each round splits the field into score groups (same
record) and pairs like-vs-like; standings churn is the signature. 8 options.

| opt | title | what it shows |
| --- | --- | --- |
| 1 | Pairing Ledger | makes the pairing logic the star: hero groups standings into record bands; cross-round is the full round-by-round ledger of matchups tagged with entering records `[a–b]` |
| 2 | Rank-Flow Bump | each contestant's standing position traced across rounds as smooth bump lines that cross when ranks swap; per-segment ●/○ marks the win/loss that caused each move |
| 3 | Score-Group Funnel | the swiss skeleton as a column-per-round funnel: the field starts at 0 pts and fans out into 0/1/2/3-point groups |
| 4 | Road to the Gate | frames the tournament as the run-up to the gate: a points-accrual climb converging on a GATE node where the leader must clear the champion |
| 5 | Scalar-Weighted | surfaces the mean-scalar tiebreak as a first-class magnitude: Copeland bars overlaid with a μ-scalar marker; dual-track rank bump + scalar descent |
| 6 | Compact Console | the densest terminal-native take: a single monospace standings block as hero with block-character win-bars; cross-round is a slope matrix |
| 7 | Pairing Ledger · Refined | opt 1's record-tagged ledger rebuilt with real structure: a score-group spine per round + diverging Δscalar margin bars per duel |
| **8** | **Ledger × Rank-Flow** | **fuses opt 1's pairing ledger with the rank-flow bump so cause sits above effect: a standings-churn signature on top, and directly beneath, a round-by-round pairing strip (records `[a–b]`, winner ▸ loser, Δ-margin bar) that EXPLAINS every move the bump shows** |

### single-matchup — one head-to-head duel (`single-matchup.html`)

One champion-vs-challenger matchup, board by board: did the challenger beat the
champion, and why. (Under racing the page swaps to a field-relative
race-comparison lens, since racing has no head-to-head gate.)

| opt | title | what it shows |
| --- | --- | --- |
| 1 | Per-board dumbbell | each board a row: hollow champion dot + filled challenger dot joined by a bar whose length is \|Δ\| and colour is the verdict — magnitude AND direction at a glance |
| 2 | Per-board diverging bar | same per-board Δ anchored to a shared zero axis: left = improved, right = regressed — the balance of the win as a center-of-mass |
| 3 | Gate rule ladder | the 3 short-circuiting gate rules + holdout step as stacked rungs in firing order; the deciding rule framed with a "◀ DECIDES" badge — exactly WHY |
| 4 | Margin bullet graph | the challenger's Δscalar drawn against the qualitative promote_margin band — did it clear the threshold and by how much |
| 5 | Replicate box plot | each side a box (mean ± σ) with min–max whiskers and every replicate plotted — the robustness lens: is the win noise? |
| **6** | **Component / judge slope** | **two side-by-side slope panels (scalar-component buckets + per-judge weighted losses), each sloping champion→challenger (down = improved); the steepest down-slope is the primary driver, drawn thick and flagged — attribution is structural** |

### single-generation — one candidate's lifecycle (`single-generation.html`)

One candidate / generation, end to end: what it changed, how it scored against the
champion, whether it cleared the promote gate, and whether it generalized.

| opt | title | what it shows |
| --- | --- | --- |
| 1 | Lifecycle spine | the candidate-view DAG (parent→patch→boards→Σ→gate→verdict) de-crowded, each node carrying its Δ-vs-champion, with the board fan + gate-rule ladder hanging off their nodes |
| **2** | **Candidate dossier (as shipped) + radar** | **the REAL dashboard candidate view folded into the study: the lifecycle DAG across the top; beneath it the shipped per-board dumbbell + short-circuiting promote-gate ladder, read together with the RADAR SILHOUETTE folded in as the at-a-glance shape panel. Under racing it swaps to field-relative panels.** |
| 3 | Diverging dumbbell | per-board Δ-vs-champion as the spine: champion ○ → candidate ●, sorted by Δ, with replicate σ as a whisker — exactly what the patch did to each board |
| 4 | Radar silhouette | the candidate's whole shape vs the champion across the heterogeneous axes the gate weighs — domination as a visible shape |
| 5 | Patch→sites→boards→drift Sankey | causal attribution flow: mutation sites → boards touched → drift-kinds (judges) moved → Σ; band width = magnitude, colour = improved/regressed |
| 6 | Trajectory + generalization fork | the epoch loss trajectory with this gen pinned, hypothesis as a callout, and the train/holdout pair drawn as a literal fork so the generalization gap is the headline |

---

## Iteration history (what shaped the final designs)

The study went through several rounds of operator feedback. The notable
corrections that moved the picks to their final form:

- **Candidate dossier folded into single-generation opt 2.** The shipped
  dashboard candidate view was pulled into the study as opt 2, with the
  **radar silhouette** (opt 4) folded in as its at-a-glance shape panel. The
  redundant **scalar-component bars were dropped** (the radar already carries
  that), and the **train→holdout generalization visual was shrunk** so the
  dossier reads cleanly.
- **WB / LB made clearly distinct across every double-elim view.** An earlier
  hero option had handled the winners'/losers'-bracket distinction poorly; the
  double-elim level reworks every view so the two brackets, their lives glyphs
  (●● vs ●○), and the WB→LB drop are unmistakable.
- **double-elim opt 8 (radial)** was flagged "needs work, can't be the default"
  and kept as an **optional prototype** only. Its connector lines were "really
  hard to follow", so they were reworked into **rim-hugging arcs** (no chords
  cutting through the centre).
- **double-elim opt 7 (the default)** had its connectors redrawn in cleaner,
  less-cluttered **orthogonal-pipe routing (drop-bus elbows)** so WB→LB drops
  don't cross.
- **racing opt 1**: the marker radius was **inverted to be inversely
  proportional to loss** (bigger = better / lower-loss), so the surviving leader
  reads as the fattest dot.
- **single-matchup × racing**: loss had been plotted counter-intuitively in the
  racing-comparison lens and was **corrected**.
- **Picks-tray "like" mechanism added to the composer.** A localStorage-backed
  "like" selection was added so the operator could mark likes that generate a
  returnable list; later, **all options were RESTORED** with the liked ones
  **clearly marked (♥)** rather than filtered out, so every option stays
  viewable and selectable.

## Status

These designs have been **implemented in the live Console dashboard**. This
directory is the archived design record — the bake-off behind the shipped
tournament views — not a list of outstanding work.
