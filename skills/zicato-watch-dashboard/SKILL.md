---
name: zicato-watch-dashboard
description: Open and read zicato's live "Console" dashboard during (or after) an evolve run — navigate the view hierarchy Environment → Epoch → Generations/round Match-ups → Boards → Mutation surface → Publication, read the configured tournament structure's match-up figure (racing survival funnel / swiss ladder / elim flow / gauntlet Δ-lanes), tell whether the loop improved, and narrate what is running. Screenshot it with browser-use. Use whenever you need to observe an in-flight or post-mortem epoch.
---

# Watch the zicato dashboard (Console)

The dashboard is zicato's **decision-centric console**: its one job is to make
the promote/reject decision over a champion-vs-challenger tournament legible
**while it is still in flight**. The shipping UI is **Console** (the
converged winner of the dashboard bake-off, internally "variant T"); it is the
sole front end. The primary surfaces are fit-to-width SVG figures — funnels,
ladders, brackets, dot-plots, heatmaps — built on a fixed visual grammar (the
same mark means the same thing everywhere). See
[../../docs/design/CONSOLE-DESIGN-LANGUAGE.md](../../docs/design/CONSOLE-DESIGN-LANGUAGE.md)
(the present-tense source of truth) and [../../AGENTS.md](../../AGENTS.md) for
the operating rules.

## 1. Find / serve the URL

`zicato evolve` **auto-spawns** the dashboard and prints its URL once on
startup — bound to **`127.0.0.1:7892`** (override with `--dashboard-port`;
`--no-dashboard` disables it). Per project policy, always report that URL to
the operator. There is no LAN-expose flag in the shipped CLI — the dashboard is
local-only by default; do not invent a `--dashboard-bind` / `0.0.0.0`. Per repo
convention the operator views it at **http://127.0.0.1:7892** from their box.

To serve the dashboard standalone against an existing workspace (post-mortem of
a closed epoch, or attach to a workspace another `evolve` is driving):

```sh
uv run --project . zicato dashboard --workspace .zicato        # foreground; Ctrl-C to stop
uv run --project . zicato dashboard --port 7892                # default host 127.0.0.1
```

Real flags only — `zicato dashboard` exposes `--workspace` (default `.zicato`),
`--host` (default `127.0.0.1`), `--port` (default `7892`). Do not pass flags
`--help` does not list (the design-doc `--read-only`/`--daemon` modes are not in
the shipped CLI). In an agent run, prefer reading the URL `evolve` already
printed over binding a port yourself.

## 2. The view hierarchy (navigate top-down)

A persistent **left tree** mirrors the real zicato hierarchy and drives a single
detail pane; the hash encodes the full path so any view deep-links. Routes are
bare `#/`:

| View | Route | What it shows / what to look for |
|---|---|---|
| **Environment** | `#/` | the workspace as a **FLEET** of epochs: an overview strip (epochs / generations / best scalar / LIVE-or-IDLE), one console card per epoch with its own loss **trendline** + best/gens/promoted, the **loop-health** panel, and the **cross-epoch trajectory** sparkline. Read health first: a finding here means the eval may be toothless. |
| **Epoch** | `#/e/<epoch>` | the **objective**, the collapsible **proposer brief** (the operator's brief to the proposer), the structure pill, then the **champion-spine ROUND TIMELINE** hero (one node per evolve round along the descending champion spine) with the **loss-floor waterfall** riding above it, then the **board × generation drift-loss HEATMAP**. Quicklinks to Generations / Boards / Mutation surface / Publication. |
| **Generations / round Match-ups** | `#/e/<epoch>/gens` (all rounds) · `#/e/<epoch>/gens/r/<round>` (one round) | the **Match-ups** — the per-structure tournament figure (see §3), the standings, and (for gauntlet) the per-round Δ-vs-champion lanes + roster. A `/r/<round>` drill scopes it to ONE evolve round's tournament. |
| **Candidate** | `#/e/<epoch>/gen/<gen>[/<entry>]` | one challenger's life as a lifecycle DAG → gate; comparison-first (a **"compare with…"** picker sets a `~cmp=<gen>` hash suffix and splits the pane side-by-side). `/diff` shows its patch diff. |
| **Boards** | `#/e/<epoch>/boards` · `#/e/<epoch>/board/<entry>[/<gen>]` | the small-multiples **board trellis** (one sparkbar + pass/fail dot row per entry); a board entry opens per-board scoring with champion-vs-challenger transcripts read **side-by-side inline**. |
| **Mutation surface** | `#/e/<epoch>/mutations[/<mutId>[/<gen>]]` | the mutable surface + the mutation matrix; a `mutId` pins one site (all gens that patched it), `mutId/gen` pins one site×generation diff. |
| **Publication** | `#/e/<epoch>/paper` | the **ACM-style epoch report** (eyebrow / title / abstract / body, GFM tables render, figures splice in). |

Navigation: the top-left **`↑ up`** control climbs the selection hierarchy
(candidate → generations → epoch → environment; a compare split collapses to the
bare candidate first). A page-wide **scale** pill (≈70–150 %) reflows the whole
page; a **color theme** swatch dropdown (16 themes, monokai default) and a
**typeface** picker (Technical default) re-skin without re-render.

## 3. The per-structure Match-ups figure (consistency matters)

zicato's per-epoch tournament **structure** is configurable; each renders a
DIFFERENT figure, and the SAME figure appears on the epoch round-timeline (as
the compact per-round figure), the live Environment/Epoch hero, and the
Match-ups detail — so the operator reads one shape, not three:

| Structure | Match-ups figure | Notes |
|---|---|---|
| **racing** (successive halving) | the **survival funnel** — the field flowing `N → N/2 → … → 1 → champion-gate`, each rung a trapezoid sized ∝ surviving field; survivors ride inside the band (`↑`), eliminated competitors peel off as labelled dead-end branches (`✕`). Same funnel on epoch page, live hero, and Match-ups. | the field is raced Δ-vs-champion-v0 on a growing board fraction; v0 defends at the gate, never a rung lane. |
| **swiss** | the **swiss ladder** (a column per round, accumulating Copeland points: win 1 / draw ½, leader flowing into the gate) + **Copeland standings**. The epoch hero uses the compact **swiss overview** (a standings bump chart over a ranked Copeland bar). | leader must beat the incumbent at the champion-gate. |
| **single_elim / double_elim** | the **elimination flow** (`elimFlow`) — the Tufte bracket-as-flow: rounds are columns, one lane per generation; two lanes converge at a match, the winner's lane continues (`↑`), the loser's terminates (`✕`), the champion's reaches the crowned gate (`♛`). Double-elim renders the losers' bracket as a second re-converging band. | the old seat/box bracket tree is retired. |
| **gauntlet** (default) | the field as **Δ-vs-champion lanes** — a signed dot-plot: a Δ=0 reference rule is the champion, each challenger a dot **below** the rule when it improved (good) / **above** when it regressed (bad), status as a glyph (`↑`/`✕`/`○`). | per-challenger hypothesis + exact Δ on hover. |

**Standings are structure-aware.** Racing **drops the W/L columns** (there is no
head-to-head — each rung ranks by scalar and cuts the worst; the promote/reject
is the gate, not a match record), showing **scalar + status** only;
single/double-elim and swiss keep W/L. The in-contention status word is also
structure-correct: elim → "in bracket", swiss → "playing", racing → "racing";
the terminal verdicts (champion / eliminated) read identically everywhere.

**Shared mark grammar (everywhere):** `↑` survives / lane continues · `✕` cut /
lane terminates · `○` pending (racing, undecided) · `♛` **current** champion ·
`♔` **former** champion (displaced incumbent) · a reference rule at Δ=0 where
**below = lower loss = good, above = higher loss = bad**. Hover any mark for a
themed hovercard with the exact numbers.

## 4. Left-nav round grouping

Generations group under their **birth round**:
`Environment > Epoch > Generations > Round 0 / Round 1 / … > {generations}`. A
champion is a **full node in its birth round** and a dimmed **carried
reference** ("↑ … defends") under each later round it defends — labeled
**defends · cached** (fast eval, the cached result that round reused) vs
**defends · re-run** (full eval, a fresh re-run that round). The round node
header itself reads the gate outcome (e.g. `v3 defends · ▲ v6 promoted` or
`v3 defends · — held`). This round layer shows ONLY when there is real round
structure (`>1` round, or a `round_index` stamp on the generations); **without
`round_index` it degrades to today's flat generation list.** A generation with
no parent and no resolved outcome is badged `◌ unscored` (an orphan), never a
misleading "seed" or a default "rejected".

## 5. Liveness — what "live" means, and the no-flash rule

- **The status pill** has two parts. The connection **word** (`live` /
  `connecting…` / `offline`) = the **SSE connection** state, nothing more. The
  separate pulsing **RUN badge** names the structure + phase (`racing · rung 0`,
  `swiss · round 2`, `proposing field`) and the in-flight unit count.
- **A tournament reads running ONLY when the heartbeat is FRESH.** The
  supervisor rewrites the heartbeat every few seconds; a frozen heartbeat from a
  torn-down run (older than ~30 s, or with no parseable timestamp) **must not**
  read "running" — even though `active_tournament.json` lingers on disk with
  `phase: "running"`. (An in-flight `active-runs` record is the one exception —
  per-run beaters bump it independently, so a present active-run forces live on
  its own.) This closes the dead-run-shows-LIVE bug class.
- **Digest-gated rendering — no flashing.** A no-op SSE heartbeat writes **zero
  DOM**: every pane computes a digest of its *structural* data only (timestamps
  + heartbeat fields excluded), and a steady beat that changes nothing repaints
  nothing — scroll position, focus, and hovercards survive. Live figures animate
  *values/positions* (CSS transitions, GPU-friendly), never `animation:infinite`.
- **What you can watch fill in live:** the survival funnel / swiss ladder /
  bracket fills board-by-board; the "what's running" per-match block shows each
  lane racing `k/N boards` with a partial Δ; the hero "blooms" from the proposing
  tracker (`N proposed · k applied`) into the live standings ladder the moment
  the field is applied. A not-yet-decided rung reads **pending** (neutral,
  nobody struck) and the gate reads **"deciding…"** — never a faked winner.

## 6. Reading it — the playbook

- **Did the loop improve?** The loss-floor **waterfall** and the **cross-epoch
  trajectory** should **descend** (lower scalar = better). A flat champion spine
  is the *stalled loop* signal.
- **Which round promoted?** Read the round-timeline gate outcomes / the left-nav
  round headers (`▲ v6 promoted` vs `— held`).
- **Was the champion cached or re-run that round?** The carried-reference tag in
  the left nav: `defends · cached` (fast mode reused the prior eval) vs
  `defends · re-run` (full mode re-ran it that round).
- **Is the loop healthy?** The Environment loop-health panel: does the
  evaluation **distinguish** candidates, or is it toothless? A toothless eval
  makes the whole tournament meaningless — fix the contract first (see
  [zicato-diagnose-health](../zicato-diagnose-health/SKILL.md)).

## 7. Screenshot it with browser-use

The browser-use MCP tools are **deferred** — load their schemas before calling,
or the call fails with `InputValidationError`:

```text
ToolSearch  query: select:mcp__browser-use__browser_navigate,mcp__browser-use__browser_screenshot,mcp__browser-use__browser_get_state,mcp__browser-use__browser_click
```

Then drive the views (the hash switches view without a reload):

```text
mcp__browser-use__browser_navigate   url: http://127.0.0.1:7892/#/                        # Environment (the fleet)
mcp__browser-use__browser_screenshot
mcp__browser-use__browser_navigate   url: http://127.0.0.1:7892/#/e/<epoch>                # Epoch: round timeline + waterfall + heatmap
mcp__browser-use__browser_screenshot
mcp__browser-use__browser_navigate   url: http://127.0.0.1:7892/#/e/<epoch>/gens           # Match-ups: the structure figure + standings
mcp__browser-use__browser_screenshot
```

Use `mcp__browser-use__browser_get_state` to read DOM text when you need to
narrate exact numbers rather than capture a picture, and
`mcp__browser-use__browser_click` to open a round, a candidate, or a board
before screenshotting.

## 8. No-browser snapshot

For a no-browser read, run the loop-health CLI or curl the live server's
file-backed JSON endpoints:

```sh
uv run --project . zicato health --workspace .zicato        # loop-health report (no browser)
curl -s http://127.0.0.1:7892/api/environment               # consolidated state the UI loads
curl -s http://127.0.0.1:7892/api/active-tournament         # live topology (null when idle)
curl -s http://127.0.0.1:7892/api/active-runs               # in-flight board units
curl -s http://127.0.0.1:7892/api/heartbeat                 # phase + freshness
```

`/api/active-tournament`, `/api/active-runs`, `/api/heartbeat` are file-backed
and live (files-canonical); `/api/tournaments` reads the lagging analytical
index and only covers **closed** rounds — never trust it for in-flight state.
The live views prefer the live `/api/active-tournament` topology over the
completed record so a mid-run epoch never shows an empty ladder.

## Guardrails

- Report the dashboard URL to the operator; the dashboard binds `127.0.0.1`
  only (no LAN-expose flag, no auth) — do not invent one.
- **Do not start a live `zicato evolve`** to get a dashboard — only the operator
  starts live runs (the live-run gate). *Reading* a running dashboard, attaching
  with `zicato dashboard`, or post-morteming a finished epoch is always fine.
- Cite only flags that appear in real `uv run --project . zicato dashboard
  --help` output — the design docs (`docs/design/CLI.md`) are stale.
- Files are canonical; the live panels read JSON/JSONL, the index is derived and
  lags to generation boundaries.

## See also

- [CONSOLE-DESIGN-LANGUAGE.md](../../docs/design/CONSOLE-DESIGN-LANGUAGE.md) — the visual grammar + the figures (the present-tense source of truth).
- [zicato-tournament-forensics](../zicato-tournament-forensics/SKILL.md) — read one promote/reject decision (the matchup detail + the gate).
- [zicato-diagnose-health](../zicato-diagnose-health/SKILL.md) — interpret the loop-health panel; fix a toothless eval.
- [zicato-analyze-epoch](../zicato-analyze-epoch/SKILL.md) — the epoch retrospective behind the Publication tab.
