# Dashboard

This document describes the **live dashboard** for an in-flight
zicato epoch. The dashboard is the operator's window into what the
loop is doing *right now* — which round is running, which entries
have completed against parent and candidate, what the predicted
gate verdict is given partial results, and which runs are
in-flight. It is the live counterpart to `analysis.html`, which is
the persisted archival snapshot of the epoch.

The dashboard is served by a **standalone Python (Starlette) service**
(`src/zicato/dashboard/`, run as `python -m zicato.dashboard`), spawned
by `zicato evolve` alongside — but separate from — the Rust watchdog
supervisor. (The Rust binary can also serve a dashboard, but `evolve`
always runs it `--no-dashboard`; the UI is the Python service's job.
This split-out into its own service was a deliberate decision — see
[RUNTIME.md](RUNTIME.md) §3.0.) The runtime state files described in
[RUNTIME.md](RUNTIME.md) — plus the `.zicato/index.db` analytical index
and the committed `epochs/` artifacts — are the dashboard's data
source. The robustness phasing in [ROBUSTNESS.md](ROBUSTNESS.md) covers
what each layer of defense catches; this document covers the operator
experience and the HTTP / SSE surface.

> **Shipped vs planned, up front.** The Python dashboard service, all
> the GET endpoints (§6), the `/events` SSE stream, the L0→L4
> navigation, the ⌘K command palette, and the status pill all **ship
> today**. The POST control endpoints (§6.2) are **wired and live** —
> when `evolve` spawns the dashboard it is served `read_only=False`, so
> the control buttons write `control/` files. What is **planned** is
> the *other half* of that loop: the orchestrator does not yet *consume*
> those `control/` files at safe points (see [RUNTIME.md](RUNTIME.md)
> §2.5), so a control action is recorded but not yet acted on. The
> `zicato dashboard` CLI exposes only `--workspace` / `--host` /
> `--port`; the `--read-only` / `--daemon` standalone modes in §2.2 are
> **not shipped** on that CLI (the `--read-only` toggle exists on the
> Rust binary and as a `create_app(read_only=…)` argument, not as a
> `zicato dashboard` flag).

## 1. What the dashboard is

zicato's ecosystem already has a live UI for one goldfive run:
**harmonograf**. Harmonograf is the **execution view** — it
shows the plan, the per-turn drift state, the intervention
ladder, the operator-driven steering controls: the temporal
trace of a single run.

The zicato dashboard is the **competition view**. It shows
**one epoch**: many goldfive runs across many generations, the
lineage tree, the live tournament bracket, the score trajectory,
the drift heatmap. It is the cadence above harmonograf.

The two are **not merged** — they are different objects (a run
is a trace; a tournament is a comparison of aggregates over many
traces). They are *linked* by a per-run drill-down: the
dashboard's drill-down opens harmonograf against the run's
`events.jsonl`. The operator moves *down* the competition view
(epoch → round → matchup → run) and at the run level steps
*across* into harmonograf's execution view. The full treatment
of this split is in
[TOURNAMENT.md §5](TOURNAMENT.md#5-the-harmonograf-split) and
[ARCHITECTURE.md §7](ARCHITECTURE.md#7-the-harmonograf-split-execution-view-vs-competition-view).

| Tool | View | Scope | Cadence |
|---|---|---|---|
| harmonograf | execution | one goldfive run | within one run |
| **zicato dashboard** | **competition** | **one zicato epoch** | **across runs within the epoch** |
| `analysis.html` | competition (snapshot) | one zicato epoch | regenerated each round; persisted at close |

The same operator may have all three open at once: harmonograf
focused on the specific run they're inspecting, the zicato
dashboard for tournament progress, `analysis.html` from a closed
epoch in a third tab for comparison.

## 2. Auto-spawn from `zicato evolve`

The common case is `zicato evolve --rounds N`. In this case, the
dashboard auto-starts; the operator doesn't think about it.

```
$ zicato evolve --rounds 5
...
Dashboard: http://127.0.0.1:7892
[evolve] round 1 of 5...
```

The URL is printed to stdout once the dashboard service reports the
port it actually bound (read back from `runtime/dashboard.json` — the
service walks `+1` if the preferred port is taken, so the URL is the
real one, not an assumed one). Both the dashboard service and the
watchdog supervisor exit when `evolve` exits; the dashboard URL stops
working at that point.

### 2.1 Opt-out and tuning flags

These are the flags that actually ship on `zicato evolve`:

| Flag | Default | Meaning |
|---|---|---|
| `--no-dashboard` | off | Do not spawn the dashboard service (and the watchdog supervisor that guards it). `evolve` still runs the loop. Useful for non-interactive CI. |
| `--dashboard-port <port>` | `7892` | Preferred port for the dashboard HTTP server (always bound on `127.0.0.1`). If taken, the service walks `+1` up to ten times. |

Notes:

- There is **no `--dashboard-bind` flag.** The dashboard always binds
  `127.0.0.1` — the operator views it from the same host as the evolve
  loop, so loopback is correct. (LAN exposure would be a reverse-proxy
  concern; it is not a CLI option.)
- There is **no `--dashboard-only` flag** on `evolve`. For a
  post-mortem against a completed workspace, run the standalone
  `zicato dashboard` command (§2.2).
- There is **no authentication** on the dashboard. It binds to
  loopback by default. Operators who put it behind a reverse proxy own
  the auth story; the dashboard does not include auth itself.

### 2.2 The standalone `zicato dashboard` command

A standalone `zicato dashboard` command **ships today** for serving an
*existing* workspace — a post-mortem of a completed epoch, or a
read-along view of a workspace another `zicato evolve` is currently
driving. It is a thin wrapper that runs the same Python service in the
foreground until interrupted (Ctrl-C):

| Flag | Default | Meaning |
|---|---|---|
| `--workspace <path>` | `.zicato` | Workspace root to serve. |
| `--host <addr>` | `127.0.0.1` | Bind address. |
| `--port <port>` | `7892` | Preferred port (walks `+1` if taken). |

> **Planned modes.** Two richer modes are **not yet shipped** as
> `zicato dashboard` flags:
>
> | Planned mode | Use case | Intended behavior |
> |---|---|---|
> | `--read-only` | Post-mortem / shared view | Disable the POST control surface. (The capability exists — `create_app(read_only=…)` and the Rust binary's `--read-only` — but is not surfaced as a `zicato dashboard` flag; the standalone command currently serves with the control surface enabled.) |
> | `--daemon` | Long-running CI | Outlive one `evolve` invocation and pick up the next. (The Rust binary has a `--daemon`; the Python `zicato dashboard` does not.) |

The auto-spawn case is the common one and gets the simplest entry
point: no command, just `evolve` doing it for you.

## 3. Architecture (HTTP + SSE)

The dashboard server is a single-page HTML application talking
to the Python (Starlette) HTTP server over two channels:

- **HTTP GET** for initial state and on-demand snapshots.
- **Server-Sent Events** (`text/event-stream`) for live updates.

```
┌────────────────────────────────┐         ┌─────────────────────────────┐
│  Browser (single-page UI)      │         │  zicato.dashboard (Python/  │
│  ──────────────────────────    │         │  Starlette + uvicorn)       │
│  index.html + JS/CSS bundle    │◄────────┤  GET /  → serves index      │
│                                │         │                             │
│  On load:                      │         │  GET /api/state             │
│   1. fetch /api/state          │◄────────┤   ◄ reads .zicato/runtime/  │
│   2. open EventSource(/events) │         │   ◄ + index.db + epochs/    │
│                                │         │                             │
│  On every SSE event:           │         │  watches .zicato/runtime/   │
│   - apply delta to UI state    │◄────────┤   → snapshot then           │
│                                │         │     state_change events     │
│                                │         │                             │
│  On user action:               │         │  POST /api/control/<action> │
│   - POST /api/control/{action} ├────────►│   → write .zicato/runtime/  │
│                                │         │     control/<file>          │
└────────────────────────────────┘         └─────────────────────────────┘
                                                       │
                                                       │ (planned) orchestrator
                                                       ▼  consumes at safe points
                                          ┌─────────────────────────────┐
                                          │  zicato evolve (Python)     │
                                          │  PLANNED: read control/ at  │
                                          │  safe points; archive into  │
                                          │  control_log/.              │
                                          └─────────────────────────────┘
```

The control POST endpoints write the `control/` files today; the
orchestrator's *consumption* of them is the planned half (see
[RUNTIME.md](RUNTIME.md) §2.5).

### 3.1 Why SSE, not WebSockets

| Property | SSE | WebSockets |
|---|---|---|
| Server → client only | ✓ (exactly what we want) | ✓ |
| Client → server | (separate HTTP POST) | ✓ (same connection) |
| Auto-reconnect with `Last-Event-ID` | ✓ (builtin) | manual |
| Implementation complexity (server + browser) | low | medium |
| Plays nicely with HTTP middleware (gzip, headers) | ✓ | partial |

Live updates are strictly server → client (the orchestrator
generates events; the browser displays them). Client → server
actions are infrequent enough to use plain `POST /api/control/...`.
SSE's auto-reconnect makes the dashboard tolerant of transient
network drops or service restarts; on reconnect the server re-sends a
fresh `snapshot` before resuming the live `state_change` stream.

### 3.2 Bundled assets

The Python dashboard service serves its HTML, CSS, and JS bundle off
disk from `src/zicato/dashboard/static/` (`index.html`, `app.js`,
`style.css`, `icons.svg`, plus `css/` and `js/`). `evolve` resolves the
static directory and hands it to the server; an unknown asset 404s, and
a missing bundle falls back to a placeholder page. No external CDN, no
node_modules at runtime.

Styling mirrors `analysis.html`'s aesthetic — same font stack,
same colour palette, same chart conventions. The two should look
like the same thing in two modes: live and archival.

### 3.3 SSE event format

The shipped wire protocol (`src/zicato/dashboard/sse.py`) is: a new
client first receives one `event: snapshot` carrying the full state,
then `event: state_change` frames as files under `.zicato/runtime/`
change. `state_change` frames are **coalesced** — a burst of writes
within a short debounce window collapses into a single frame whose
payload carries the set of changed *kinds* (`payload.kinds`), so the
dashboard does one coalesced refresh instead of a frame per file.

```
event: snapshot
data: { ...the same shape as GET /api/state... }

event: state_change
data: {"kinds": ["active_tournament", "active_runs"]}

event: state_change
data: {"kinds": ["heartbeat"]}
```

The `kind` regions are derived from which file changed (heartbeat,
active_tournament, active_runs, run_log, lineage, …); the dashboard JS
reads `payload.kinds` and re-fetches the affected endpoints. A
keep-alive comment is sent periodically so idle connections survive
proxy read-timeouts.

### 3.4 Data sourcing: files-canonical, index-derived

zicato persistence is **files-canonical, index-derived**. This is
a load-bearing rule for the dashboard, and getting it wrong is a
real bug class — it produced a blank Tournament view mid-run (see
below). The rule:

> **Live dashboard state MUST read the JSON / JSONL files (or the
> live endpoints that wrap them). Only resolved historical and
> analytical queries may read `index.db`.**

There are two tiers of storage, and they are refreshed on
different cadences:

| Tier | Files | Written | Read for |
|---|---|---|---|
| **Canonical, live** | `runtime/active_tournament.json`, `runtime/heartbeat.json`, `runtime/active_runs/*.json`, `lineage.json`, per-run `events.jsonl` | Live — the moment state changes, by the orchestrator or the worker that owns the file | The live dashboard: anything that must reflect *right now* |
| **Derived, lagging** | `index.db` (SQLite) | Dual-written at **generation/round boundaries** only (see [ANALYTICAL-INDEX.md §2.3](ANALYTICAL-INDEX.md#23-the-orchestrator-dual-writes-live)); fully rebuildable via `zicato reindex` | Resolved historical / analytical queries: the bracket of *closed* rounds, cross-run aggregates |

`index.db` is a **derived analytical cache**. It is rebuilt from
the canonical files by `zicato reindex`, and during a live run it
is only refreshed at generation boundaries — so mid-round it does
not yet contain the in-flight generation or the in-progress
tournament. It is the right source for *resolved* data (closed
rounds, cross-run aggregates) and the wrong source for *live*
data.

**The bug this rule prevents.** The Tournament view previously
read its in-progress matchup from `index.db`. Because the index is
only refreshed at generation boundaries, mid-round there was no
row for the running tournament — so the panel rendered **blank**
for the entire duration of every round, exactly when an operator
most wants to watch it. The fix: the Tournament view now reads
`runtime/active_tournament.json` (via `GET /api/active-tournament`)
live. The index is still read — but only for the bracket of rounds
that have already *closed*.

The endpoints in §6 follow this split: `/api/active-tournament`,
`/api/active-runs`, `/api/lineage`, `/api/run-log`, and
`/api/heartbeat` are file-backed and live; `/api/tournaments`
(the bracket) and `/api/tournaments/{id}` (matchup detail of
resolved rounds) read the index and degrade gracefully — a missing
or stale `index.db` yields an empty bracket with a `note` rather
than an error.

## 4. UI panels

### 4.0 Navigation structure

> **Shipped navigation (clean-slate redesign).** The current UI drops
> the left-hand sidebar in favour of a **top bar** with a drill-down
> navigation across five levels — **L0 Workspace → L1 Epoch → L2
> Generation → L3 Round → L4 Run** — a **⌘K command palette** for
> jumping between epochs / generations / runs, and a **status pill**
> reflecting the live heartbeat/phase. The panels described in §4.1-§4.9
> are the building blocks; the drill-down levels compose them.

The levels nest the way the data does — the operator moves *down* the
competition view and at the run level steps *across* into harmonograf:

| Level | Scope | What it composes | Primary source |
|---|---|---|---|
| **L0 Workspace** | cross-epoch | a workspace-at-a-glance summary + trend sparkline across epochs | `/api/workspace` |
| **L1 Epoch** | one epoch | the live tournament / active runs / log tail (the "what's happening now" panels) plus the epoch's evaluation contract (scoring with nested weight dicts incl. `per_judge_weights`, board, proposer brief, mutation paths relativized to the workspace root) | live `active_tournament.json`, `/api/active-runs`, `/api/run-log`, `/api/epoch` |
| **L2 Generation** | one generation | the lineage graph (**including in-flight generations**), score trajectory, per-generation drill-downs; a **side-by-side compare picker** (defaulting to the parent generation) with URL-hash sync | `/api/lineage`, `/api/generation/...`, `/api/score-trajectory` |
| **L3 Round** | one round / matchup | the bracket for resolved rounds **and the in-progress tournament rendered live**; the per-matchup A/B grid | `active_tournament.json` for the active round; `index.db` for closed rounds |
| **L4 Run** | one run | the run's live status + event tail; "open in harmonograf" handoff | `/api/run/...`, `/api/run-log`, transcript endpoints |

Two behaviors are the result of fixes and are called out per-panel
below:

- The in-progress tournament renders **live** — it previously read
  `index.db` and so was blank for the whole duration of every round
  (see §3.4). It now reads `active_tournament.json` via
  `/api/active-tournament` for the active round and only reads the
  index for the bracket of closed rounds.
- The lineage includes in-flight generations because `/api/lineage`
  walks generation directories rather than reading the resolved-only
  `lineage.json`.

The per-panel sections below (§4.1-§4.9) describe the panel
building blocks; the drill-down levels above map them onto screens.

| # | Panel | What it shows |
|---|---|---|
| 4.1 | Header / status pill | epoch / generation / round / elapsed + live phase |
| 4.2 | Tournament view | the competition — bracket, active matchup, predicted gate verdict |
| 4.3 | Active runs list | every in-flight tournament run |
| 4.4 | Lineage SVG | the cross-epoch generation tree |
| 4.5 | Score trajectory | gen-score over rounds |
| 4.6 | Drift-kind heatmap | which drift kinds move across the epoch |
| 4.7 | Loop-health panel | loop-health findings — is the eval toothless? |
| 4.8 | Log tail | rolling tail of the latest goldfive events |
| 4.9 | Drill-down side panels | per-generation and per-run detail |

### 4.1 Header

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  zicato · myagent · epoch hardened_research · round 4 / 5 · 00:08:42 elapsed │
│  parent v4   →   candidate v5     [TOURNAMENT RUNNING]                       │
└─────────────────────────────────────────────────────────────────────────────┘
```

Always visible at the top. Phase indicator (`PROPOSING`,
`APPLYING`, `TOURNAMENT RUNNING`, `JOURNALING`, `PAUSED`,
`STALLED`) updates from `heartbeat.json`'s `phase` field. Elapsed
time is `now - evolve_started_at`. Round count is from
`evolve_args` plus the running counter.

### 4.2 Tournament view

The biggest panel. The whole point of the dashboard. The
**Tournament view** is the competition view of the epoch's
king-of-the-hill gauntlet (see [TOURNAMENT.md](TOURNAMENT.md) for
the model). It has two levels.

#### 4.2.1 The bracket

At the top of the panel: the **bracket** — the whole epoch's
gauntlet at a glance. The winners' spine runs left to right, each
round's matchup hangs off it, discarded challengers are marked.

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ Tournament — epoch hardened_research — 4 rounds, 3 promotions                 │
│                                                                                │
│   v0 ──▶── v1 ──▶── v2 ────────▶── v3 ──▶── (v5 running)                      │
│   │        │        │              │        │                                 │
│   r1       r2       r3   ✗         r4       r5  ◀ in flight                    │
│   PROM     PROM     DISCARD        PROM                                        │
│   Δ -0.31  Δ -0.18  +0.02          Δ -0.24                                     │
│                                                                                │
│   click any round → matchup detail                                            │
└──────────────────────────────────────────────────────────────────────────────┘
```

Each row shows the round, the verdict (`PROM` / `DISCARD`), and
the score delta (negative is an improvement). Promoted rounds sit
on the spine; discarded rounds are marked `✗`. The bracket of
**resolved** rounds is driven by the `tournaments` table of the
analytical index (see
[ANALYTICAL-INDEX.md §3.8](ANALYTICAL-INDEX.md#38-tournaments)).
The **in-progress** round at the tip of the spine is rendered
live from `active_tournament.json`, not the index — see §3.4 for
why this distinction is load-bearing. Clicking any round opens its
**matchup detail** — the hypothesis,
patches, per-entry A/B grid, scalar breakdown, and gate verdict
(specified in [TOURNAMENT.md §3](TOURNAMENT.md#3-per-matchup-detail)).

#### 4.2.2 The active matchup

Below the bracket: the **active matchup** — the in-flight round
drilled in, with live partial data. Shows every board entry ×
(parent champion, candidate challenger) with per-entry status,
drift loss, pass/fail, and the running aggregate.

```
┌────────────────────────────────────────────────────────────────────────────────────┐
│ Active matchup — round 4 — v4 → v5                                                 │
│                                                                                    │
│  entry_id                       weight    parent              candidate    Δ-drift │
│ ─────────────────────────────  ──────   ───────────────    ──────────────  ─────── │
│  short_solar                   1.0      [✓] 0.42 ✓ pass    [✓] 0.31 ✓ pass  -0.11  │
│  long_solar_with_constraints   1.5      [✓] 0.55 ✗ fail    [▶ 73%] running  --     │
│  contradictory_brief           1.0      [▶ 12%] running    [⋯] queued        --    │
│  revision_dialog               1.0      [⋯] queued         [⋯] queued        --    │
│  expert_review                 1.0      [⋯] queued         [⋯] queued        --    │
│                                                                                    │
│  completed: 3 / 10 sides · in-flight: 2 · queued: 5                                 │
│                                                                                    │
│  ─── PREDICTED GATE VERDICT (from partial results) ───                              │
│  best case  (remaining favour candidate):   PROMOTE   confidence 0.74               │
│  worst case (remaining favour parent):      REJECT    confidence 0.31               │
│  current trend if continued:                PROMOTE   margin 0.08 above threshold   │
│                                                                                    │
└────────────────────────────────────────────────────────────────────────────────────┘
```

**Status glyphs.** `[⋯] queued`, `[▶ N%] running` (where `N%` is
the **deadline-elapsed fraction** — how far through its
wall-clock budget the run is, from the `wall_clock_deadline`
countdown — not true task progress), `[✓] done`, `[!] aborted`,
`[✗] killed`.

**Predicted gate verdict.** Computed deterministically from
partial results — no LLM in this path.

#### 4.2.3 The predicted-verdict projection

The gate (see [SCORING.md](SCORING.md)) checks two things:

- `drift_loss_delta < -MARGIN` (candidate's drift loss must beat
  parent's by at least `MARGIN`).
- `pass_rate_candidate >= pass_rate_parent` (strict monotonicity
  on pre-existing pass-rate).

For a partially-complete tournament, we have:

```
completed_entries: list[Entry]            # both sides done
in_flight_entries: list[Entry]            # at least one side still running
queued_entries: list[Entry]               # both sides queued

partial_drift_delta = weighted_sum(
    entry.candidate_drift_loss - entry.parent_drift_loss
    for entry in completed_entries
)
remaining_weight = sum(e.weight for e in (in_flight + queued))

# Best case: every remaining entry favours the candidate by the
# observed best-case delta so far (or a fixed floor of -1.0 drift).
best_case_remaining_delta = remaining_weight * min(
    -1.0,
    min(e.delta_drift_loss for e in completed_entries) if completed_entries else -1.0
)

# Worst case: every remaining entry favours the parent by the
# observed worst-case delta so far (or a fixed ceiling of +1.0).
worst_case_remaining_delta = remaining_weight * max(
    +1.0,
    max(e.delta_drift_loss for e in completed_entries) if completed_entries else +1.0
)

projected = {
    "best_case_drift_delta": partial_drift_delta + best_case_remaining_delta,
    "worst_case_drift_delta": partial_drift_delta + worst_case_remaining_delta,
    "current_trend": partial_drift_delta * (1 + remaining_weight / completed_weight),
}
```

The projection has three outputs:

- **Best case** — if every remaining entry runs maximally in the
  candidate's favour. If this case still rejects, the round is
  decided early; the operator knows there's no recovering.
- **Worst case** — if every remaining entry runs maximally
  against the candidate. If this case still promotes, the round
  is decided early in the other direction.
- **Current trend** — straight-line projection of the partial
  result to the full board. This is the operator's "is it
  trending toward promote or reject?" gauge.

The projection is **deterministic**. Same partial results in,
same prediction out. No LLM, no randomness. The dashboard updates
the prediction every time an entry finishes; once both best-case
and worst-case agree, the round is decided early (logged as
"early exit by predicted verdict") and the orchestrator can skip
the remaining runs.

(Note: early-exit-by-predicted-verdict is a v1.2 ergonomic add-on,
not a v1.1 correctness requirement. The default v1.1 behaviour is
to always finish every entry; the dashboard just shows the
projection so operators know what to expect.)

### 4.3 Active runs list

A flat table of every `active_runs/{run_id}.json`, served by
`GET /api/active-runs`. Updated on every SSE `active_run_*` event.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ Active runs                                                                 │
│                                                                             │
│  run_id                              gen   entry          phase    deadline │
│ ──────────────────────────────────  ────  ───────────────  ───────  ──────── │
│  e4f2_long_solar_candidate          v5    long_solar       agent     73% ▓▓▓ │
│  e4f2_contradictory_brief_parent    v4    contradictory…   adapter…  12% ▓   │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

The `deadline` bar is the run's **elapsed-vs-budget** fraction —
`elapsed_seconds / budget_seconds`, served as the `progress`
field — NOT a measure of how much of the task is done. A run at
`73%` is 73% through its wall-clock budget; it may finish (or be
escalated) at any point. The bar is `null` / absent for a run
with no budget or no start time. See
[§6.1 `GET /api/active-runs`](#get-apiactive-runs) for the field
shapes.

Clicking a row opens the per-run drill-down (§4.9).

### 4.4 Lineage SVG

Same renderer as `analysis.html`'s lineage graph. Nodes are
generations, edges are parent → promoted-child relationships
within the epoch (and dashed edges to the previous epoch's final
generation for cross-epoch parentage). The graph is driven by
`GET /api/lineage`, which walks generation directories — so it
**includes in-flight generations**: a candidate that has been
proposed and applied but whose tournament has not yet resolved
appears immediately, with `promoted: null`. The
currently-running candidate is highlighted with a pulse
animation.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ Lineage                                                                     │
│                                                                             │
│      v0 ──→ v1 ──→ v2 ──╳ (rejected)                                        │
│              ╲                                                              │
│               ──→ v3 ──→ v4 ──→ v5* (running)                               │
│                                  ↑                                          │
│                                  parent of next candidate                   │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

Hovering a node shows the experiment's `core_idea`. Clicking opens
a side panel with the full `experiment.json` rendered (hypothesis
fields, outcome fields, per-entry deltas).

### 4.5 Score trajectory chart

Time series of `gen_score.json`'s drift-loss scalar per
generation, two lines: parent and candidate at each round. The
horizontal axis is round number, not wall-clock time. Promoted
rounds are marked; rejected rounds are marked differently
(typically a `✗` glyph on the candidate's data point).

```
loss
1.0 ┤
0.8 ┤  ●
    │  │╲
0.6 ┤  │ ╲    ●
    │  │  ╲  ╱│ ╲
0.4 ┤  │   ●  │  ╲     ●        ●  ← candidate v5 (current trend)
    │  │      │   ╲___/__       │
0.2 ┤  │      │       │  ╲___◌
    │  │      │       │      ╲
0.0 ┼──┴──────┴───────┴────────●─────────
    r0    r1     r2      r3     r4 (in flight)
```

When the candidate is mid-tournament, its data point is shown as
the **current-trend projection** (see §4.2.3) with a confidence
band drawn from the best/worst case spread.

### 4.6 Drift-kind heatmap

A small heatmap with drift kinds on the y-axis and rounds on the
x-axis. Cell intensity is the per-kind count weighted by severity
across all runs in that round. Helps the operator see which drift
kinds are moving across the epoch.

```
                   r1   r2   r3   r4
CONFABULATION_RISK ███  ██   █    ░
TOOL_ERROR         ░    ░    ░    ░
CAPABILITY_MISMATCH█    ██   ███  ████  ← surging this round
LOOPING_REASONING  ░    █    █    ░
PLAN_REVISION      ██   ██   ██   ██
```

The heatmap is the "what's the lineage learning?" view. When
`CAPABILITY_MISMATCH` surges in round 4, the operator can
immediately drill into round 4's runs and see what the proposer
changed.

### 4.7 Loop-health panel

The loop-health panel surfaces zicato's loop-health diagnostics
(see [LOOP-HEALTH.md](LOOP-HEALTH.md)) — the detectors that catch
a *running but meaningless* loop, where the evaluation is
degenerate and the tournament cannot distinguish any candidate.

The panel renders the latest round's `LoopHealth` report: the
`overall` severity and one row per firing detector, each with its
severity, summary, and remedy.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ Loop health — round 7 — OVERALL: CRITICAL                                    │
│                                                                             │
│  [critical] degenerate_scoring                                              │
│    v0..v7 all carry gen_score = 1.000000; the evaluation has produced       │
│    zero score variance across 8 generations.                                │
│    → Inspect scoring.json and the per-entry loss.json files.                │
│                                                                             │
│  [critical] non_differentiating_entries                                     │
│    9 of 10 board entries return identical drift_loss + pass_fail.            │
│    → These entries are dead weight; replace them.                           │
│                                                                             │
│  [info] no_expectations                                                     │
│    No board entry carries an expectation; scoring runs on drift loss only.  │
│                                                                             │
│  health trajectory:  ok ok ok warn warn crit crit  ← worsening              │
└─────────────────────────────────────────────────────────────────────────────┘
```

When the panel goes red the operator knows the loop is toothless
*without* having to eyeball the journal — the exact manual step
the motivating incident depended on (a real run had `v0` and `v1`
both score exactly `1.000000`, found only by inspection). The
panel border turns red on a `critical` SSE `loop_health_critical`
event; the health trajectory strip shows the `overall` severity
of each round so a worsening trend is visible at a glance. The
per-round reports come from
`epochs/{epoch}/loop_health/round_{NNN}.json` (projected into the
analytical index for the trajectory query).

### 4.8 Log tail

A rolling tail of the latest goldfive events across all active
runs. Each line: `{ts} {run_id_short} {event_kind} {summary}`.
Bounded to the last 200 lines client-side; older entries scroll
off.

```
12:35:04.800  e4f2_long…   GoldfiveLLMCallStart      researcher (model=...)
12:35:05.412  e4f2_long…   GoldfiveDriftDetected     CONFABULATION_RISK · sev=MEDIUM
12:35:05.450  e4f2_long…   GoldfiveLLMCallEnd        904 tokens out
12:35:05.512  e4f2_contr…  GoldfiveTaskStarted       coordinator: route_to(researcher)
```

The log tail is read from a Rust file watcher on each
`events.jsonl` file referenced by `active_runs/*.json`. Each file
is `read+tail`-ed; new lines stream to the SSE subscribers.

### 4.9 Drill-down side panels

Clicking any **generation node** in the lineage opens a side
panel:

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ Generation v3 — promoted in round 3                                         │
│                                                                             │
│ Experiment                                                                  │
│   core_idea: Tighten the researcher's system prompt for citations           │
│   modulating: researcher.instruction, researcher.description                │
│   why:        Pattern observed: CONFABULATION_RISK on 60% of [research]      │
│                                                                             │
│ Hypothesis match                                                            │
│   CONFABULATION_RISK   predicted DOWN moderate    actual DOWN moderate   ✓  │
│   TOOL_ERROR           predicted UP   minor       actual FLAT            ✗  │
│                                                                             │
│ Outcome                                                                     │
│   drift_loss_delta   -0.18                                                  │
│   pass_rate_delta    +0.05                                                  │
│   decision           promote                                                │
│                                                                             │
│ Per-entry deltas                                                            │
│   short_solar                  -0.11   pass→pass   ✓                        │
│   long_solar_with_constraints  -0.24   fail→pass   ✓✓                       │
│   contradictory_brief          -0.02   pass→pass                            │
│   ...                                                                       │
│                                                                             │
│ Patches                                                                     │
│   patches/be4c8de.json  →  researcher.instruction                           │
│   patches/1f29c6a.json  →  researcher.description                           │
└─────────────────────────────────────────────────────────────────────────────┘
```

Clicking any **active-run row** opens:

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ Run e4f2_long_solar_with_constraints_candidate                              │
│                                                                             │
│ Live status                                                                 │
│   phase            agent_running                                            │
│   started_at       12:35:00                                                 │
│   wall_clock       38s / 120s   (32%)                                       │
│   heartbeat_age    0.4s                                                     │
│   drift_count      2                                                        │
│                                                                             │
│ Events (last 20)                                                            │
│   ...                                                                       │
│                                                                             │
│ [ Open in harmonograf → ]                                                   │
└─────────────────────────────────────────────────────────────────────────────┘
```

The "Open in harmonograf" button is a `mailto:`-style handoff: it
constructs a URL that harmonograf understands (typically a
`file://` URL pointing at the run's `events.jsonl`, optionally a
`harmonograf://` deep link if the operator has harmonograf
installed as a protocol handler).

## 5. Interactivity model

> **What ships vs what's planned.** As spawned by `evolve` the
> dashboard service runs `read_only=False`, so the **control POST
> endpoints are live and the write side works**: clicking a control
> button drops the corresponding `control/` file atomically. What is
> **planned** is the orchestrator's *consumption* of those files — the
> evolve loop does not yet read `control/` at safe points (see
> [RUNTIME.md](RUNTIME.md) §2.5), and there is no `control_log/`
> audit-on-consume yet. So today a control action is recorded on disk
> but does not yet change the running loop. The safety review framing
> below (gate overrides as contract violations needing a durable audit
> trail) still governs the planned consume side.

### 5.1 The control surface (shipped write side)

| Operation | Source / effect |
|---|---|
| All panel data (read) | `.zicato/runtime/` + `.zicato/index.db` + `.zicato/epochs/` |
| Open in harmonograf | Constructs a handoff URL; no zicato state change |
| Reload page | Re-fetches `/api/state`, re-opens SSE (fresh snapshot) |
| **Pause / Skip / Kill / Promote / Reject / Brief** | `POST /api/control/...` → atomic write of a `control/` file (returns `202`). **Consumption is planned** (see §5 banner). |

The control buttons render and are wired to the POST endpoints in
§6.2. The `--read-only` posture (where the POST endpoints return `403`)
is available via `create_app(read_only=…)` and the Rust binary's
`--read-only`, but is not surfaced as a `zicato dashboard` flag yet
(§2.2).

### 5.2 Write-back via the control-file protocol

Operator actions are `POST /api/control/<action>` requests on the
dashboard service. The service writes a file under
`.zicato/runtime/control/` (today). The orchestrator is *to* poll
`control/` at safe points and act on the request (planned).

```
operator clicks "pause epoch"
            │
            ▼
browser → POST /api/control/pause                       (SHIPPED)
            │
            ▼
dashboard service → atomically write
            │ .zicato/runtime/control/pause_epoch        (SHIPPED)
            │ (JSON payload {reason, ts})
            │
            ▼
            ... at next safe point ...                   (PLANNED)
            │
            ▼
orchestrator → reads control/pause_epoch                 (PLANNED)
            │ archives it into control_log/<ts>_*.json
            │ updates heartbeat.json with phase="paused"
            │
            ▼
dashboard → notices heartbeat change via its watcher     (SHIPPED path)
            │ emits a state_change SSE frame
            │
            ▼
browser → updates the status pill to "PAUSED"
```

### 5.3 Command catalogue and safe-point semantics

| Command | File written | Safe point | Priority | Audit log entry |
|---|---|---|---|---|
| `pause_epoch` | `control/pause_epoch` | between rounds | normal | `command=pause_epoch` |
| `resume_epoch` | `control/resume_epoch` | (orchestrator is in pause-wait loop) | normal | `command=resume_epoch` |
| `skip_round` | `control/skip_round` | between rounds OR start of new round | normal | `command=skip_round`, `skipped_round=N` |
| `kill_run` | `control/kill_runs/{run_id}` | immediate (high priority; orchestrator checks every 500ms) | high | `command=kill_run`, `run_id=...`, `cause=operator` |
| `promote_override` | `control/promote/{gen_id}` | end of tournament, before journaling | gate-override | `command=promote`, `gen=...`, `tournament_decision_was=reject` |
| `reject_override` | `control/reject/{gen_id}` | end of tournament, before journaling | gate-override | `command=reject`, `gen=...`, `tournament_decision_was=promote` |
| `rubric_replace` | `control/rubric_replacement.txt` | between rounds | normal | `command=rubric_replace`, `old_hash=...`, `new_hash=...` |

**Shipped endpoints vs catalogue.** The POST endpoints that exist
today are `pause`, `skip-round`, `kill/{run_id}`, `promote/{gen_id}`,
`reject/{gen_id}`, and `brief` (the brief/rubric-replacement). There is
**no** `resume` endpoint yet, and the `*_was` / `old_hash` audit fields
above describe the **planned** `control_log/` entries — none of the
consume-side audit ships today.

**Safe points** are the orchestrator's natural pause boundaries:
between rounds, between entries within a round, end of tournament
before journaling. Commands are categorised by which safe point
they apply to; checking only at safe points avoids the
"orchestrator dies mid-board-entry because the operator clicked
pause" failure mode. (This is the planned consume contract.)

**Gate-override audit.** When `promote_override` lands and the
tournament would have rejected, the audit log entry records both
the override AND the tournament's would-have-been decision. The
`experiment.json` outcome block also carries an
`override_by_operator: true` field. The journal entry shows the
override; `analysis.md` includes it in the closing pass. The
override is not silent.

### 5.4 Authorization model

There is no authentication on the dashboard. The dashboard binds to
loopback by default; any local user can issue any command. The
(planned) audit log would capture a symbolic `issued_by` because the
dashboard doesn't ask for an operator identity.

If/when the dashboard needs real auth (multi-operator setups,
remote access without a reverse proxy), the spot to add it is HTTP
middleware in the dashboard service — between the request being
accepted and the control file being written.

## 6. HTTP API surface

This is the shipped surface served by `src/zicato/dashboard/server.py`.
The GET endpoints and `/events` are always available; the POST control
endpoints (§6.2) are available unless the app was built `read_only`.

### 6.1 Routes at a glance

The full route table registered by the server:

| Route | Purpose |
|---|---|
| `GET /` | The single-page UI (`index.html`, off-disk bundle). |
| `GET /static/{path}` and `GET /{path}` | UI assets / fallback to the bundle. |
| `GET /events` | SSE stream (§3.3): one `snapshot` then coalesced `state_change` frames. |
| `GET /api/health` | Liveness/identity (§6.1 detail below). |
| `GET /api/state` | Composite live snapshot. |
| `GET /api/environment?run-log-limit=N` | One coalesced read of the whole environment — the front-end refreshes the entire view from this instead of fanning out to many endpoints. |
| `GET /api/workspace` | L0 cross-epoch workspace summary. |
| `GET /api/epoch` | Current epoch's evaluation-contract view (scoring incl. `per_judge_weights`, board, brief, mutation paths). |
| `GET /api/lineage` | Generation DAG incl. in-flight generations. |
| `GET /api/active-tournament` | In-progress tournament shape (live). |
| `GET /api/active-runs` | In-flight runs with computed progress fields. |
| `GET /api/heartbeat` | Heartbeat snapshot. |
| `GET /api/run-log?limit=N` | Tail of the active run's `events.jsonl`. |
| `GET /api/tournaments` | Bracket of resolved rounds (index). |
| `GET /api/tournaments/{generation_id}` | One resolved round's matchup detail. |
| `GET /api/matchup-grid/{epoch_id}/{champion_id}/{challenger_id}` | Per-entry A/B grid. |
| `GET /api/score-trajectory` | Gen-score trajectory across rounds. |
| `GET /api/drift-movements/{generation_id}` | Drift-movement / heatmap data. |
| `GET /api/health-report` | Latest loop-health report. |
| `GET /api/search?...` | Cross-workspace search. |
| `GET /api/contract-diff/{epoch_id}` | Contract diff vs the parent epoch. |
| `GET /api/epoch/{epoch_id}/per-judge-trend` | Per-judge loss trend across the epoch. |
| `GET /api/generation/{epoch_id}/{generation_id}/per-judge` | Per-judge breakdown for one generation. |
| `GET /api/generation/{epoch_id}/{generation_id}/per-entry` | Per-entry breakdown for one generation. |
| `GET /api/round/{epoch_id}/{champion_id}/{challenger_id}/per-judge-comparison` | Per-judge A/B comparison for a round. |
| `GET /api/run/{run_id}/per-judge` | Per-judge breakdown for one run. |
| `GET /api/run/{epoch_id}/{generation_id}/{entry_id}/per-judge` | Same, addressed by triple. |
| `GET /api/run/{epoch_id}/{generation_id}/{entry_id}/expectations` | Outcome-check (expectations) results. |
| `GET /api/run/{epoch_id}/{generation_id}/{entry_id}/header` | Run header metadata. |
| `GET /api/run/{epoch_id}/{generation_id}/{entry_id}/transcript` | Run transcript. |
| `GET /api/conversation/{run_id}` | Multi-turn conversation for a run. |
| `GET /api/matchup/{entry_id}/conversations` | Side-by-side conversations for a matchup entry. |
| `GET /api/files` and `GET /api/files/{epoch_id}/{generation_id}/{tree,content,patches,diff}` | Snapshot file tree, content, patches, and diffs. |
| `GET /api/mutations/{epoch_id}` and `.../{mutation_id}` | Mutation surface listing and detail. |
| `GET /api/epoch/{epoch_id}/journal` and `.../journal.md` | Journal as data or rendered markdown. |
| `GET /api/epoch/{epoch_id}/analysis` and `.../analysis.html` | Analysis as data or rendered HTML. |
| `POST /api/control/{pause,skip-round,kill/{run_id},promote/{gen},reject/{gen},brief}` | Control surface (§6.2). |

The sections below detail the endpoints whose response shape is
load-bearing.

#### `GET /api/state`

Returns a complete live snapshot (`state_reader.build_snapshot`),
joining `heartbeat.json`, `active_tournament.json`, `active_runs/*`,
and derived `epochs/` + index data. It is the same shape sent as the
SSE `snapshot` frame and used on first load and on reconnect.

#### `GET /events`

Server-Sent Events stream. On connect it sends one `event: snapshot`,
then `event: state_change` frames as files under `.zicato/runtime/`
change. Frames are **coalesced** over a short debounce window and carry
the set of changed `kinds` (§3.3). A keep-alive comment keeps idle
connections open through proxy read-timeouts.

#### `GET /api/active-tournament`

The current tournament panel's data only. Used by the panel for
isolated refresh; cheaper than `/api/state`.

#### `GET /api/active-runs`

The active runs list only. Each element carries the raw
`active_runs/{run_id}.json` fields plus three computed fields the
per-entry progress bars need:

| Field | Meaning |
|---|---|
| `progress` | A `0..1` fraction, or `null`. This is the **deadline-elapsed fraction** — `elapsed / budget` clamped to `[0,1]` — NOT a measure of true task progress. A run at `progress: 0.9` is 90% through its wall-clock budget, not 90% done with its work. |
| `elapsed_seconds` | Wall-clock seconds since the run started. |
| `budget_seconds` | The run's wall-clock budget (`wall_clock_budget_seconds`). |

`progress` is `null` when the run carries no budget or no start
time. The dashboard renders it as an elapsed-vs-budget bar, not a
completion bar — the distinction matters because a run can finish
well before its deadline.

#### `GET /api/lineage`

Lineage DAG plus per-generation metadata. The response is
`{"generations": [...]}`; each node is:

```json
{
  "generation_id": "v5",
  "epoch_id": "hardened_research",
  "parent_generation_id": "v4",
  "promoted": null,
  "created_at": "2026-05-14T12:34:55Z"
}
```

The view is built by walking every generation **directory** in
every epoch, so it includes **in-flight generations** — a
candidate that has been proposed and applied but whose tournament
has not yet resolved. `promoted` is a tri-state:

| `promoted` | Meaning |
|---|---|
| `true` | Generation was promoted (won its tournament). |
| `false` | Generation was rejected. |
| `null` | Generation is **still being scored** — tournament in flight. |

The legacy `lineage.json` only lists resolved (promoted)
generations; it is used as a fallback for a root node's
`created_at` / `parent_generation_id`, but the directory walk is
authoritative. This is what lets the Tree view draw `v0` plus the
in-flight `v5` mid-run rather than waiting for the round to close.

#### `GET /api/run-log?limit=N`

Tails the active run's `events.jsonl` and returns the last `N`
goldfive events for the log-tail panel. `limit` defaults to `40`
and is clamped to `1..=500` (an absent or zero value falls back to
the default). When there is no active run, it falls back to the
most recent `events.jsonl` under `epochs/`.

Response shape:

```json
{
  "events": [
    {
      "seq": 1423,
      "kind": "drift_detected",
      "ts": "2026-05-14T12:35:05.412Z",
      "summary": "CONFABULATION_RISK · sev=MEDIUM"
    }
  ]
}
```

| Field | Meaning |
|---|---|
| `seq` | The event's sequence number, or `null` if the record carries none. |
| `kind` | The goldfive payload kind, **normalized to snake_case**. |
| `ts` | RFC-3339 emission timestamp, or `null` if unknown. |
| `summary` | A short human-readable summary; falls back to `kind`. |

goldfive writes events in two envelope shapes — a camelCase shape
(`steeringDecisionMade`, `taskProgress`, ...) and a normalized
`{kind, payload, emitted_at, ...}` shape from the reducer's
proto-reparse path. The endpoint handles both and normalizes
every `kind` to snake_case (the same normalization as zicato#1)
so the dashboard keys on one stable vocabulary. Every failure
mode — missing file, truncated tail line, unparseable record —
degrades to fewer or zero events; the endpoint never `500`s.

#### `GET /api/generation/{id}`

Full `experiment.json` for the named generation, rendered ready
for the side panel.

#### `GET /api/run/{run_id}`

Live `active_runs/{run_id}.json` plus a tail of the run's
`events.jsonl`.

#### `GET /api/log-tail`

Just the log tail. Useful for an "open in new tab" experience that
just wants the rolling event view.

#### `GET /api/health`

A liveness/identity endpoint for the dashboard footer and for
process-level health checks. Always `200`:

```json
{
  "status": "ok",
  "version": "0.3.0",
  "uptime_seconds": 412,
  "read_only": false,
  "workspace": "/home/op/myagent/.zicato",
  "port": 7893,
  "build": "0.3.0"
}
```

| Field | Meaning |
|---|---|
| `port` | The TCP port the server actually **bound** (`app.state.bound_port`) — useful because the default `:7892` walks `+1` on a port clash, so the bound port is not always the requested one. |
| `version` / `build` | The dashboard version string (`_dashboard_version()`). Both fields carry it. |
| `read_only` | `true` when the app was built `read_only=True`; the POST control endpoints return `403` in that state. As spawned by `evolve` the dashboard runs `read_only=False`. |

### 6.2 POST control endpoints

These are **wired and live** (the write side). They are mounted under
`/api/control/` and gated by the `read_only` flag.

| Endpoint | Body | Effect |
|---|---|---|
| `POST /api/control/pause` | optional `{"reason": "..."}` | Atomically write `control/pause_epoch` (JSON `{reason, ts}`). |
| `POST /api/control/skip-round` | optional `{"reason": "..."}` | Write `control/skip_round`. |
| `POST /api/control/kill/{run_id}` | (none) | Write `control/kill_runs/{run_id}`. |
| `POST /api/control/promote/{generation_id}` | (none) | Write `control/promote/{generation_id}`. |
| `POST /api/control/reject/{generation_id}` | (none) | Write `control/reject/{generation_id}`. |
| `POST /api/control/brief` | raw text body | Write `control/rubric_replacement.txt` (the on-disk file keeps its protocol name; the UI calls it the proposer brief). |

Each writes its `control/` file atomically and returns `202 Accepted`.
The effect is **asynchronous and planned**: once the orchestrator's
consume side lands (see §5 banner and [RUNTIME.md](RUNTIME.md) §2.5) it
will apply the command at the next safe point; today the file is
written but not consumed. There is no `resume` endpoint and no
`ack_override` confirmation step in the shipped surface.

### 6.3 Error responses

| Code | When |
|---|---|
| `202` | Success — the control file was written. |
| `400` | A path-parameter `run_id` / `generation_id` is unsafe (fails the `[A-Za-z0-9._-]` id check). |
| `403` | The dashboard is in `read_only` mode; the control endpoint refuses. |

## 7. Progressive `analysis.html` and the dashboard

`analysis.html` is regenerated after every generation completes
(see [EPOCHS-AND-JOURNALING.md](EPOCHS-AND-JOURNALING.md) §5.2).
It is the **persisted archival snapshot**; the dashboard is the
live view.

| Property | dashboard | `analysis.html` |
|---|---|---|
| Updates | live (SSE) | regenerated each round |
| Source | `.zicato/runtime/` + `index.db` + `epochs/` | `epochs/` only |
| Process | the Python dashboard service | orchestrator (Python) |
| Reachable while orchestrator is paused | partially (heartbeat shows phase) | yes (it's a file) |
| Reachable after `evolve` exits | no (the auto-spawned service exits with evolve — but `zicato dashboard` can re-serve the workspace) | yes (the file persists) |
| Includes LLM narrative | no | yes (at epoch close) |

The two are intentionally redundant. The dashboard is the
"watching live" tool; `analysis.html` is the "send a link to a
teammate" tool. Either can stand alone.

## 8. Phasing

| Phase | What ships |
|---|---|
| **v1.2 — shipped** | The dashboard as a **separate Python service**, auto-spawned from `zicato evolve` (and a standalone `zicato dashboard` command). All GET endpoints (§6.1). The L0→L4 drill-down navigation, ⌘K palette, and status pill. Live panels read the runtime JSON files; only the bracket of *resolved* rounds and the cross-run analytics read the analytical index (see §3.4). SSE for live updates (snapshot + coalesced state_change). Drill-down side panels. |
| **v1.3 — partially shipped** | POST control endpoints under `/api/control/` (`pause`, `skip-round`, `kill`, `promote`, `reject`, `brief`) — the **write side is live**. **Planned:** the orchestrator's consume-at-safe-points half, the `control_log/` audit, and gate-override confirmation UX. |

The split is the same split as the runtime work — observability first,
controls after. The two are operated independently: the observability
pass alone is useful (the operator can see what's happening); the
control surface's write side ships now, with the loop-side consumption
landing after the safety review of the action surface.

## 9. Cross-references

| Topic | Document |
|---|---|
| State file layout the dashboard reads from | [RUNTIME.md](RUNTIME.md) §2 |
| The watchdog supervisor + the dashboard-as-separate-service split | [RUNTIME.md](RUNTIME.md) §3, §3.0 |
| `control/` and `control_log/` file shapes (and the planned consume side) | [RUNTIME.md](RUNTIME.md) §2.5 |
| Tournament gate formula the predicted verdict approximates | [SCORING.md](SCORING.md) |
| The tournament competition model — bracket, per-matchup detail, analytics | [TOURNAMENT.md](TOURNAMENT.md) |
| The harmonograf split — execution view vs competition view | [TOURNAMENT.md](TOURNAMENT.md) §5 |
| Loop-health diagnostics behind the loop-health panel | [LOOP-HEALTH.md](LOOP-HEALTH.md) |
| The analytical index — derived, refreshed at generation boundaries, read only for resolved rounds (see §3.4) | [ANALYTICAL-INDEX.md](ANALYTICAL-INDEX.md) |
| The dual-write discipline behind the files-canonical / index-derived rule | [ANALYTICAL-INDEX.md §2.3](ANALYTICAL-INDEX.md#23-the-orchestrator-dual-writes-live) |
| The `experiment.json` shape displayed in drill-downs | [EPOCHS-AND-JOURNALING.md](EPOCHS-AND-JOURNALING.md) §3 |
| Progressive `analysis.html` generation | [EPOCHS-AND-JOURNALING.md](EPOCHS-AND-JOURNALING.md) §5.2 |
| Robustness layers backing the supervisor | [ROBUSTNESS.md](ROBUSTNESS.md) |
| CLI surface (`zicato evolve --no-dashboard`, `zicato dashboard`) | [CLI.md](CLI.md) |
