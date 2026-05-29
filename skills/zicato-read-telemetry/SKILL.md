---
name: zicato-read-telemetry
description: Trace a zicato run through its telemetry — the canonical per-run events.jsonl and loss.json, and the harmonograf session that renders it — and relate zicato's own meta-loop session (the tool itself) to the per-board-run sessions (the system under test). Use when you need to follow what a specific run did, read its drift/loss profile, or deep-link into harmonograf.
---

# Read zicato telemetry

zicato **consumes** telemetry, it does not invent a wire format. Every run of
the inner harness emits a goldfive `goldfive.v1.Event` stream, captured
verbatim via goldfive's `JSONLPersistenceSink`, then reduced post-run into a
typed `LossProfile`. **The JSONL file is canonical; the `LossProfile`
(`loss.json`) is the surface every other component reads.** Full spec:
[../../docs/design/TELEMETRY.md](../../docs/design/TELEMETRY.md).

## The two canonical per-run files

Each `(epoch, generation, entry_id)` triple maps to exactly one run
directory:

```
.zicato/epochs/{epoch}/generations/v{N}/runs/{entry_id}/
  ├── events.jsonl   # canonical: one goldfive event per line, byte-stable, mode="write"
  └── loss.json      # the reduced LossProfile for this run
```

```sh
# walk the runs of a generation
ls .zicato/epochs/*/generations/*/runs/*/
# read one run's reduced profile
cat .zicato/epochs/<epoch>/generations/v3/runs/<entry_id>/loss.json
```

### Reading `events.jsonl`

One JSON object per line, in emit order (goldfive's `replay_from_jsonl`
parses it back to proto `Event`s). goldfive writes two envelope shapes — a
camelCase shape (`steeringDecisionMade`, …) and a normalized `{kind,
payload, emitted_at, …}` shape. A truncated tail (run crashed before the
terminal event) is expected and tolerated — the reducer stamps
`aborted=true`, `abort_reason="no_terminal_event"`.

### Reading `loss.json` (the `LossProfile`)

The fields that matter most when tracing a run:

| Field | Meaning |
|---|---|
| `entry_id`, `epoch_id`, `generation` | identity triple |
| `drift_counts_by_kind` | per-`DriftKind` counts (symbolic enum names, e.g. `DRIFT_KIND_CONFABULATION_RISK`); `DRIFT_KIND_CUSTOM` buckets all custom-judge violations |
| `drift_counts_by_judge` | the `CUSTOM` slice split by `judge_name` — what scoring weights via `per_judge_weights` |
| `drift_counts_by_severity` | `INFO` / `WARNING` / `CRITICAL` |
| `escalations`, `plan_revisions`, `task_failure_ratio` | other drift/loss features |
| `drift_loss` | the weighted scalar the tournament scores on (computed in the reducer — the one place with both counts and weights) |
| `pass_fail` | AND of the entry's expectations; `None` when there are no expectations |
| `turn_count`, `drift_counts_by_kind_per_turn`, `stopped_reason` | multi-turn features |
| `runtime_ms`, `aborted`, `abort_reason` | runtime features |

Drift counts feed both the proposer (as hypothesis-shaped features) and the
tournament (`drift_loss`, `pass_fail`). If `drift_counts_by_kind` is
identically empty across a whole epoch, the goldfive stream probably isn't
reaching the reducer — that's the `flat_drift_signal` critical in
[zicato-diagnose-health](../zicato-diagnose-health/SKILL.md).

### Regenerate the analyzer insight out of band

```sh
.venv/bin/zicato analyze-telemetry --workspace .zicato            # current epoch, latest
.venv/bin/zicato analyze-telemetry --epoch <id> --round <N>       # named round
```

`evolve` runs the analyzer per round automatically; reach for this only to
(re)generate an insight after the fact. Real flags: `--workspace`,
`--epoch`, `--round`.

## One harmonograf server, many sessions

There is exactly **one** harmonograf server for an evolve invocation (zicato
auto-launches one, or you pin an external one via `ZICATO_HARMONOGRAF_URL` /
the `harmonograf_url` config key). It hosts **many sessions**, and URLs
deep-link to a session:

```
<harmonograf_url>/#/session/<adk_session_id>
```

Two *kinds* of session live on that one server — this is the load-bearing
distinction:

| Session kind | What it traces | `session_id` shape | Telemetry file |
|---|---|---|---|
| **meta-loop** (the tool itself) | zicato's *own* proposer + judge LLM calls — the orchestrator deciding what to mutate and how to score | `zicato-meta-loop-<sanitized-evolve-start-iso>` (one stable id per evolve invocation) | `.zicato/runtime/meta_loop_events.jsonl` |
| **per-board-run** (the system under test) | one run of the inner harness against one board entry | `<gen_id>--<entry_id>` | the per-run `events.jsonl` above |

The meta-loop session is "zicato thinking about the system under test"; the
per-board-run sessions are "the system under test running". They bucket as
distinct sessions so the dashboard renders the meta-loop as one continuous
timeline rather than scattering its events across per-round sessions. The
meta-loop emitter is best-effort and strictly additive — telemetry never
blocks or breaks the evolve loop.

### Following a run into harmonograf

From the dashboard, each L4 run drill-down renders an **Open in harmonograf**
link with exactly the `/#/session/<adk_session_id>` href above (the dashboard
only renders it when both `harmonograf_url` and the run's `adk_session_id`
are known). The `adk_session_id` comes off the run's events
(`session_id` / `sessionId`) and is carried on its `loss.json`. To go from a
run directory to its harmonograf view: read `adk_session_id` from the run,
then open `<harmonograf_url>/#/session/<that_id>`. See
[zicato-watch-dashboard](../zicato-watch-dashboard/SKILL.md) for driving the
browser.

### The emulator lane

Multi-turn runs emit the user-emulator's per-turn LLM calls on a dedicated
lane named **`zicato:emulator`** (bracketed `emulator_turn`, carrying the
emulator `model` and previews). The lane name is the discriminator: anything
on `zicato:emulator` is the emulator's work, anything on the inner-harness
lane is the agent's. This is why a multi-turn entry's wall-clock can exceed
the agent's own thinking time — the emulator's LLM time counts toward the
entry's `wall_clock_budget_seconds`.

## Guardrails

- **Files are canonical, index is derived.** Trace runs from `events.jsonl` /
  `loss.json`, not `index.db` (the index lags to generation boundaries and is
  rebuildable via `zicato reindex`).
- Cite only flags in real `--help` (`analyze-telemetry`: `--workspace`,
  `--epoch`, `--round`).
- Never start a live `evolve` to produce telemetry — read existing run
  directories.
