# Dashboard

This document describes the **live dashboard** for an in-flight
zicato epoch. The dashboard is the operator's window into what the
loop is doing *right now* — which round is running, which entries
have completed against parent and candidate, what the predicted
gate verdict is given partial results, and which runs are
in-flight. It is the live counterpart to `analysis.html`, which is
the persisted archival snapshot of the epoch.

The dashboard is served by the same Rust binary that runs the
watchdog. The runtime state files described in
[RUNTIME.md](RUNTIME.md) are the dashboard's data source. The
robustness phasing in [ROBUSTNESS.md](ROBUSTNESS.md) covers what
each layer of defense catches; this document covers the operator
experience and the HTTP / SSE surface.

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
[evolve] workspace: /home/op/myagent/.zicato
[evolve] supervisor: spawning zicato-supervisor on :7892
[evolve] dashboard:  http://localhost:7892/  (live for this epoch)
[evolve] round 1 of 5...
```

The URL is printed to stdout once, then the orchestrator runs the
meta-loop. The supervisor exits when the orchestrator exits; the
dashboard URL stops working at that point.

### 2.1 Opt-out and tuning flags

| Flag | Default | Meaning |
|---|---|---|
| `--no-dashboard` | off | Do not spawn the supervisor. Useful for non-interactive CI where no operator will look at it; cuts startup by ~20ms. The robustness watchdog also doesn't run; the operator should use OS-level supervision instead. |
| `--dashboard-port <port>` | `7892` | Bind to a specific port. If taken, fails — the auto +1 retry is only for the default. |
| `--dashboard-bind <addr>` | `127.0.0.1` | Bind address. Set to `0.0.0.0` for LAN access (with the usual security caveats — there's no auth on the dashboard). |
| `--dashboard-only` | off | Run the supervisor without running the meta-loop (post-mortem mode against a completed workspace). Equivalent to `zicato dashboard --read-only`. |

Two notes:

- `--no-dashboard` is the only common opt-out. CI scripts that
  want predictable noise sometimes use it.
- There is **no authentication** on the dashboard. It binds to
  loopback by default. Operators who expose it to the LAN are
  expected to put a reverse proxy in front of it (basic auth, SSO,
  etc.). The dashboard does not include auth itself because that's
  out of scope for the runtime layer and an over-eager built-in
  would be the wrong defaults for any specific deployment.

### 2.2 Standalone modes (v1.2+)

Two genuine use cases for a standalone `zicato dashboard` command
land later as thin CLI wrappers around the same supervisor binary
in different modes:

| Mode | Use case | Behavior |
|---|---|---|
| `zicato dashboard --read-only` | Post-mortem of a completed epoch | Reads only committed state in `epochs/`; no `.zicato/runtime/` interaction. No control surface. |
| `zicato dashboard --daemon` | Long-running CI scenarios | Same as auto-spawn mode but doesn't exit when the active `evolve` exits — picks up the next `evolve` invocation. Uses `.zicato/runtime/supervisor.pid` to ensure only one daemon at a time. |

The auto-spawn case is the common one and gets the simplest CLI:
no command, just a flag on `evolve`. The standalone modes are for
when the operator wants to keep the dashboard around longer than
one `evolve` invocation. They are NOT required for v1.2; they land
when an operator names a concrete reason for one.

## 3. Architecture (HTTP + SSE)

The dashboard server is a single-page HTML application talking
to a Rust HTTP server over two channels:

- **HTTP GET** for initial state and on-demand snapshots.
- **Server-Sent Events** (`text/event-stream`) for live updates.

```
┌────────────────────────────────┐         ┌─────────────────────────────┐
│  Browser (single-page UI)      │         │  zicato-supervisor (Rust)   │
│  ──────────────────────────    │         │  ───────────────────────    │
│  index.html + bundled JS/CSS   │◄────────┤  GET /  → serves index      │
│                                │         │                             │
│  On load:                      │         │  GET /api/state             │
│   1. fetch /api/state          │◄────────┤   ◄ reads .zicato/runtime/  │
│   2. open EventSource(/events) │         │   ◄ returns full snapshot   │
│                                │         │                             │
│  On every SSE event:           │         │  inotify on .zicato/runtime │
│   - apply delta to UI state    │◄────────┤   → write SSE event         │
│                                │         │     to every subscriber     │
│                                │         │                             │
│  On user action (v1.3):        │         │  POST /api/<action>         │
│   - POST /api/{action}         ├────────►│   → write .zicato/runtime/  │
│                                │         │     control/<file>           │
└────────────────────────────────┘         └─────────────────────────────┘
                                                       │
                                                       │ orchestrator polls
                                                       ▼
                                          ┌─────────────────────────────┐
                                          │  zicato evolve (Python)     │
                                          │  reads control/ at safe     │
                                          │  points; consumed commands  │
                                          │  move to control_log/.      │
                                          └─────────────────────────────┘
```

### 3.1 Why SSE, not WebSockets

| Property | SSE | WebSockets |
|---|---|---|
| Server → client only | ✓ (exactly what we want) | ✓ |
| Client → server | (separate HTTP POST) | ✓ (same connection) |
| Auto-reconnect with `Last-Event-ID` | ✓ (builtin) | manual |
| Implementation complexity (Rust + browser) | low | medium |
| Plays nicely with HTTP middleware (gzip, headers) | ✓ | partial |

Live updates are strictly server → client (the orchestrator
generates events; the browser displays them). Client → server
actions (v1.3) are infrequent enough to use plain `POST /api/...`.
SSE's auto-reconnect makes the dashboard tolerant of transient
network drops or supervisor restarts; the browser sends the last
event ID it saw and the server replays anything missed.

### 3.2 Bundled assets

The supervisor binary embeds the dashboard's HTML, CSS, and JS as
static resources via `include_str!`. No external CDN, no
node_modules, no separate static directory to ship. The binary is
the whole dashboard.

Styling mirrors `analysis.html`'s aesthetic — same font stack,
same colour palette, same chart conventions. The two should look
like the same thing in two modes: live and archival.

### 3.3 SSE event format

```
event: state_changed
id: 142
data: {"kind": "active_tournament_updated", "round": 4, "entry_id": "long_solar", "side": "candidate", "status": "done", "drift_loss": 0.31}

event: state_changed
id: 143
data: {"kind": "active_run_started", "run_id": "e4f2_revision_dialog_parent", "generation": "v4", "entry_id": "revision_dialog"}

event: state_changed
id: 144
data: {"kind": "heartbeat_stale", "last_seen_at": "2026-05-14T12:35:02.418Z", "age_seconds": 18}
```

| Event kind | Trigger |
|---|---|
| `state_changed` | Generic; payload's `kind` field narrows it. |
| `kind: heartbeat_bumped` | Orchestrator wrote a new `heartbeat.json`. |
| `kind: heartbeat_stale` | Supervisor's local timer noticed no fresh heartbeat. |
| `kind: active_tournament_updated` | An entry's status flipped (queued → running → done). |
| `kind: active_run_started` | New `active_runs/{run_id}.json` appeared. |
| `kind: active_run_phase` | A worker bumped its `phase`. |
| `kind: active_run_finished` | `active_runs/{run_id}.json` removed (clean) or finalised (killed/crashed). |
| `kind: round_finished` | Orchestrator wrote round outcome to `experiment.json`. |
| `kind: loop_health_critical` | A round's `LoopHealth` report came back `critical` (see [LOOP-HEALTH.md](LOOP-HEALTH.md)); payload carries the firing findings. |
| `kind: epoch_close` | Operator (or auto) closed the epoch. |
| `kind: control_applied` | A `control/<file>` command was consumed; payload includes the audit entry from `control_log/`. |
| `kind: escalation_started` | Supervisor sent SIGTERM to a stalled worker. |
| `kind: escalation_finished` | Supervisor confirmed the worker is dead. |

Event IDs are monotonic per dashboard session. The browser stores
the last ID it saw in `localStorage` so a reload reconnects
without missing events.

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

### 4.0 View structure

The dashboard is a single page with a fixed header and a
left-hand nav rail switching between **four views**. Each view
composes the panels described in §4.1-§4.9. The header
(§4.1) is always visible; the nav rail routes by URL fragment
(`#/overview`, `#/tree`, `#/tournament`, `#/epoch`).

| View | Route | What it shows | Primary source |
|---|---|---|---|
| **Overview** | `#/overview` | The live tournament panel — parent/child entry groups with per-entry **elapsed-vs-budget** bars — plus the log tail. The operator's "what is happening right now" view. | live files: `active_tournament.json`, `active_runs/*` (via `/api/active-runs`), `/api/run-log` |
| **Tree** | `#/tree` | The cross-epoch lineage graph, **including in-flight generations** (the proposed-but-not-yet-resolved candidate is drawn mid-run), plus the score trajectory. | `/api/lineage` (directory walk, live) |
| **Tournament** | `#/tournament` | The competition view: the bracket (champion spine + challengers) for resolved rounds, **and the in-progress tournament rendered live**. Selecting a node opens its matchup detail. | live: `active_tournament.json` for the in-progress round; index for closed rounds |
| **Epoch** | `#/epoch` | The epoch's evaluation contract: scoring with **nested weight dicts** (including `per_judge_weights`), the board, the proposer brief, mutation paths shown **relativized** to the workspace root. | `/api/epoch` |

Two behaviors are the result of fixes and are called out per-view
below:

- The **Tournament** view renders the in-progress tournament live
  — it previously read `index.db` and so was blank for the whole
  duration of every round (see §3.4). It now reads
  `active_tournament.json` via `/api/active-tournament` for the
  active round and only reads the index for the bracket of closed
  rounds.
- The **Tree** view includes in-flight generations because
  `/api/lineage` walks generation directories rather than reading
  the resolved-only `lineage.json`.

The per-panel sections below (§4.1-§4.9) describe the panel
building blocks; the table above maps them onto views.

| # | Panel | What it shows |
|---|---|---|
| 4.1 | Header | epoch / generation / round / elapsed |
| 4.2 | Tournament view | the competition — bracket, active matchup, predicted gate verdict |
| 4.3 | Active runs list | every in-flight tournament run |
| 4.4 | Lineage SVG | the cross-epoch generation tree |
| 4.5 | Score trajectory | gen-score over rounds |
| 4.6 | Drift-kind heatmap | which drift kinds move across the epoch |
| 4.7 | Loop-health panel | loop-health findings — is the eval toothless? |
| 4.8 | Log tail | rolling tail of the latest goldfive events |

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

The dashboard is **read-only by default** in v1.2. Interactive
controls land in v1.3 because the action surface needs safety
review — gate overrides are contract violations and must leave a
durable audit trail.

### 5.1 Read-only mode (v1.2)

| Read operation | Source |
|---|---|
| All panel data | `.zicato/runtime/` + `.zicato/epochs/` |
| Open in harmonograf | Constructs URL; no zicato state change |
| Reload page | Re-fetches `/api/state`, opens SSE |

No active control surface. The dashboard cannot pause the loop,
kill a run, or override the gate; the operator can only watch.
The orchestrator control-channel buttons — **Pause**, **Skip**,
**Force-kill**, **Override** — do render in the v1.2 UI, but as
**disabled previews**: they are visible so the eventual control
surface has a place in the layout, but they are inert and the
POST endpoints behind them (§6.2) are a **v1.3 deliverable**.
They become live only when v1.3 lands the `control/` file
protocol and the action surface clears its safety review.

### 5.2 Write-back via the control-file protocol (v1.3)

Operator actions become `POST /api/<action>` requests on the
dashboard. The dashboard server writes a file under
`.zicato/runtime/control/`; the orchestrator polls `control/` at
safe points and acts on the request.

```
operator clicks "pause epoch"
            │
            ▼
browser → POST /api/pause
            │
            ▼
supervisor → write .zicato/runtime/control/pause_epoch
            │ (empty file; presence is the signal)
            │
            ▼
            ... at next safe point (between rounds or between
                board entries depending on command priority) ...
            │
            ▼
orchestrator → reads control/pause_epoch
            │ moves it to control_log/<ts>_pause_epoch.json
            │ updates heartbeat.json with phase="paused"
            │
            ▼
supervisor → inotify sees heartbeat change
            │ broadcasts SSE event with control_applied + new phase
            │
            ▼
browser → updates header pill to "PAUSED"
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

**Safe points** are the orchestrator's natural pause boundaries:
between rounds, between entries within a round, end of tournament
before journaling. Commands are categorised by which safe point
they apply to; checking only at safe points avoids the
"orchestrator dies mid-board-entry because the operator clicked
pause" failure mode.

**Gate-override audit.** When `promote_override` lands and the
tournament would have rejected, the audit log entry records both
the override AND the tournament's would-have-been decision. The
`experiment.json` outcome block also carries an
`override_by_operator: true` field. The journal entry shows the
override; `analysis.md` includes it in the closing pass. The
override is not silent.

### 5.4 Authorization model

There is no authentication on the dashboard in v1.2 or v1.3. The
dashboard binds to loopback by default; any local user can issue
any command. The audit log captures `issued_by:
"operator-localhost"` because the dashboard doesn't ask for an
operator identity.

If/when the dashboard needs real auth (multi-operator setups,
remote access without a reverse proxy), the spot to add it is the
HTTP middleware in the supervisor — between the request being
accepted and the control file being written. The audit log gains
an `issued_by` field that's no longer constant. Until that
landing, the operator's name in the audit log is symbolic.

## 6. HTTP API surface

The full API surface for v1.3. v1.2 ships the GET endpoints only.

### 6.1 GET endpoints

#### `GET /`

Returns the single-page UI as `text/html`. The HTML inlines its
CSS and JS; no separate `<script src>` or `<link rel>` to load.

#### `GET /api/state`

Returns a complete snapshot. Used on first load and on
reconnect.

Response shape:

```json
{
  "workspace": "/home/op/myagent/.zicato",
  "instance_id": "default",
  "epoch": {
    "id": "hardened_research",
    "started_at": "2026-04-08T14:31:00Z",
    "rounds_total": 5,
    "round_current": 4
  },
  "heartbeat": {
    "ts": "2026-05-14T12:35:02.418Z",
    "age_seconds": 1.2,
    "phase": "tournament",
    "stale": false
  },
  "active_tournament": { ... },
  "active_runs": [ { ... }, { ... } ],
  "lineage": { ... },
  "score_trajectory": { ... },
  "drift_heatmap": { ... },
  "log_tail": [ ... ]
}
```

The shape mirrors the file layout in [RUNTIME.md](RUNTIME.md) — a
join across `heartbeat.json`, `active_tournament.json`,
`active_runs/*.json`, plus derived data from `epochs/`.

#### `GET /events`

Server-Sent Events stream. Sends a `state_changed` event for every
inotify event the supervisor processes. Reconnect-safe: the
browser sends `Last-Event-ID: <id>` to resume from where it left
off.

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
  "build": "0.3.0+g67b5fac"
}
```

| Field | Meaning |
|---|---|
| `port` | The TCP port the server actually **bound** — useful because the default `:7892` walks `+1` on a port clash, so the bound port is not always the requested one. The dashboard footer shows this so the operator can confirm which port to point a browser at. |
| `build` | A build identifier: the crate version plus a short git SHA when the build script could resolve one (`0.3.0+g67b5fac`); the bare version otherwise. Always non-empty. |
| `read_only` | `true` when the supervisor was started with `--read-only` (post-mortem mode); the POST control endpoints return `403` in that state. |

### 6.2 POST endpoints (v1.3)

| Endpoint | Body | Effect |
|---|---|---|
| `POST /api/pause` | (empty) | Write `control/pause_epoch`. |
| `POST /api/resume` | (empty) | Write `control/resume_epoch`. |
| `POST /api/skip-round` | (empty) | Write `control/skip_round`. |
| `POST /api/kill/{run_id}` | (empty) | Write `control/kill_runs/{run_id}`. |
| `POST /api/promote/{gen_id}` | `{"justification": "...", "ack_override": true}` | Write `control/promote/{gen_id}`. Body's `ack_override` must be `true` for the request to be honoured; this is the in-UI confirmation step. |
| `POST /api/reject/{gen_id}` | same as promote | Same shape. |
| `POST /api/rubric` | `{"content": "..."}` | Write `control/rubric_replacement.txt`. |

All POST responses are immediate (the dashboard writes the file
and returns `202 Accepted`); the actual effect is asynchronous —
the orchestrator applies it at the next safe point. The browser
relies on the subsequent SSE `control_applied` event to know
when the action took effect.

### 6.3 Error responses

| Code | When |
|---|---|
| `400` | Malformed body (e.g. `ack_override: false` on a gate-override request). |
| `404` | Path references a `run_id` or `gen_id` that doesn't exist. |
| `409` | Conflict — e.g. `POST /api/resume` when not paused. |
| `503` | Orchestrator stalled (`heartbeat.json` is stale); request not written. The dashboard greys out the controls in this state, but the safety net is here too. |

## 7. Progressive `analysis.html` and the dashboard

`analysis.html` is regenerated after every generation completes
(see [EPOCHS-AND-JOURNALING.md](EPOCHS-AND-JOURNALING.md) §5.2).
It is the **persisted archival snapshot**; the dashboard is the
live view.

| Property | dashboard | `analysis.html` |
|---|---|---|
| Updates | live (SSE) | regenerated each round |
| Source | `.zicato/runtime/` + `epochs/` | `epochs/` only |
| Process | supervisor (Rust) | orchestrator (Python) |
| Reachable while orchestrator is paused | partially (heartbeat shows phase) | yes (it's a file) |
| Reachable after `evolve` exits | no (supervisor exits with evolve) | yes (the file persists) |
| Includes LLM narrative | no | yes (at epoch close) |

The two are intentionally redundant. The dashboard is the
"watching live" tool; `analysis.html` is the "send a link to a
teammate" tool. Either can stand alone.

## 8. Phasing

| Phase | What ships |
|---|---|
| **v1.2** | Read-only dashboard. Auto-spawn from `zicato evolve`. All GET endpoints. The four views (Overview, Tree, Tournament, Epoch). Live panels read the runtime JSON files; only the bracket of *resolved* rounds and the cross-run analytics read the analytical index (see §3.4). SSE for live updates. Drill-down side panels. The v1.3 control buttons render as disabled previews (§5.1). |
| **v1.3** | POST endpoints (`pause`, `resume`, `skip-round`, `kill`, `promote`, `reject`, `rubric`). Control file protocol. `control_log/` audit. Gate-override confirmation UX. |

The split is the same split as the runtime work — v1.2 is the
observability pass, v1.3 is the controls pass. The two can be
operated independently: v1.2 alone is operationally useful (the
operator can see what's happening even if they can't change it);
v1.3 layers on after the safety review of the action surface is
complete.

## 9. Cross-references

| Topic | Document |
|---|---|
| State file layout the dashboard reads from | [RUNTIME.md](RUNTIME.md) §2 |
| Supervisor binary that serves the dashboard | [RUNTIME.md](RUNTIME.md) §3 |
| `control/` and `control_log/` file shapes | [RUNTIME.md](RUNTIME.md) §2.5 |
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
