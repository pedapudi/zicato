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
**harmonograf**. Harmonograf shows the plan, the per-turn drift
state, the intervention ladder, the operator-driven steering
controls — all within a single run.

The zicato dashboard is the cadence above harmonograf. It shows
**one epoch**: many goldfive runs across many generations, the
lineage DAG, the live tournament, the score trajectory, the drift
heatmap. The two are complementary; the dashboard's per-run
drill-down opens harmonograf against the run's `events.jsonl`.

| Tool | Scope | Cadence |
|---|---|---|
| harmonograf | one goldfive run | within one run |
| **zicato dashboard** | **one zicato epoch** | **across runs within the epoch** |
| `analysis.html` | one zicato epoch (snapshot) | regenerated each round; persisted at close |

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
| `kind: epoch_close` | Operator (or auto) closed the epoch. |
| `kind: control_applied` | A `control/<file>` command was consumed; payload includes the audit entry from `control_log/`. |
| `kind: escalation_started` | Supervisor sent SIGTERM to a stalled worker. |
| `kind: escalation_finished` | Supervisor confirmed the worker is dead. |

Event IDs are monotonic per dashboard session. The browser stores
the last ID it saw in `localStorage` so a reload reconnects
without missing events.

## 4. UI panels

The dashboard is a single page with seven panels stacked
vertically. Each panel can collapse to a one-line summary. The
order is fixed; the most operationally relevant panels are at the
top.

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

### 4.2 Active tournament panel

The biggest panel. The whole point of the dashboard. Shows every
board entry × (parent, candidate) with per-entry status, drift
loss, pass/fail, and the running aggregate.

```
┌────────────────────────────────────────────────────────────────────────────────────┐
│ Active tournament — round 4 — v4 → v5                                              │
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

**Status glyphs.** `[⋯] queued`, `[▶ N%] running` (with progress
percent from `wall_clock_deadline` countdown), `[✓] done`, `[!]
aborted`, `[✗] killed`.

**Predicted gate verdict.** Computed deterministically from
partial results — no LLM in this path.

#### 4.2.1 The predicted-verdict projection

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

A flat table of every `active_runs/{run_id}.json`. Updated on
every SSE `active_run_*` event.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ Active runs                                                                 │
│                                                                             │
│  run_id                              gen   entry          phase       %    │
│ ──────────────────────────────────  ────  ───────────────  ──────────  ──── │
│  e4f2_long_solar_candidate          v5    long_solar       agent       73% │
│  e4f2_contradictory_brief_parent    v4    contradictory…   adapter…    12% │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

Clicking a row opens the per-run drill-down (§4.8).

### 4.4 Lineage SVG

Same renderer as `analysis.html`'s lineage graph. Nodes are
generations, edges are parent → promoted-child relationships
within the epoch (and dashed edges to the previous epoch's final
generation for cross-epoch parentage). The currently-running
candidate is highlighted with a pulse animation.

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
the **current-trend projection** (see §4.2.1) with a confidence
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

### 4.7 Log tail

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

### 4.8 Drill-down side panels

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

No control surface. The dashboard cannot pause the loop, kill a
run, or override the gate. The operator can only watch.

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

The active runs list only.

#### `GET /api/lineage`

Lineage DAG plus per-generation experiment summaries.

#### `GET /api/generation/{id}`

Full `experiment.json` for the named generation, rendered ready
for the side panel.

#### `GET /api/run/{run_id}`

Live `active_runs/{run_id}.json` plus a tail of the run's
`events.jsonl`.

#### `GET /api/log-tail`

Just the log tail. Useful for an "open in new tab" experience that
just wants the rolling event view.

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
| **v1.2** | Read-only dashboard. Auto-spawn from `zicato evolve`. All GET endpoints. All 7 panels render from state files. SSE for live updates. Drill-down side panels. |
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
| The `experiment.json` shape displayed in drill-downs | [EPOCHS-AND-JOURNALING.md](EPOCHS-AND-JOURNALING.md) §3 |
| Progressive `analysis.html` generation | [EPOCHS-AND-JOURNALING.md](EPOCHS-AND-JOURNALING.md) §5.2 |
| Robustness layers backing the supervisor | [ROBUSTNESS.md](ROBUSTNESS.md) |
| CLI surface (`zicato evolve --no-dashboard`, `zicato dashboard`) | [CLI.md](CLI.md) |
