---
name: zicato-read-telemetry
description: Trace a zicato run through its telemetry — the canonical per-run events.jsonl and loss.json, and the harmonograf session that renders it — and relate zicato's own meta-loop session (the tool itself) to the per-board-run sessions (the system under test). Use when you need to follow what a specific run did, read its drift/loss profile, or deep-link into harmonograf.
---

# Read zicato telemetry

zicato **consumes** telemetry, it does not invent a wire format. A run emits a
JSONL stream, which is reduced post-run into a typed `LossProfile`. **The JSONL
file is canonical; the `LossProfile` (`loss.json`) is the surface every other
component reads.** Full spec:
[../../docs/design/TELEMETRY.md](../../docs/design/TELEMETRY.md).

**Which producer read that JSONL is a contract knob.** `LossProfile` is the
convergence point and a *dialect* is a named producer feeding it, chosen by
`scoring.json`'s `telemetry_dialect`
([TELEMETRY-DIALECTS.md](../../docs/design/TELEMETRY-DIALECTS.md)):

- **`goldfive`** (the DEFAULT, and everything below describes it) — a
  drift-instrumented `goldfive.v1.Event` stream captured verbatim via goldfive's
  `JSONLPersistenceSink`. The only dialect that produces drift kinds and plan
  revisions.
- **`adk_events`** / **`transcript`** — tolerant readers for a harness that does
  NOT run under the drift-instrumented ecosystem harness. goldfive is no longer
  required. Under `transcript` the drift-shaping knobs
  (`namespace_weights["drift:"]`, `plan_revision_weight`, `per_kind_weights`,
  `drift_reducer`) and the judge-channel knobs are **inert** — zicato warns
  rather than failing, because no drift kinds are produced. The `failure:`
  and `runtime:` channels still score under every dialect: a run that crashed
  did so regardless of what telemetry it wrote.

Everything downstream of the reducer (scoring, the gate, the index, board
reflection) reads `LossProfile` and never knows which dialect produced it. The
knob is omitted from the contract hash at its `goldfive` default; setting it to
anything else rolls the epoch.

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

`loss.json` is a direct `asdict(LossProfile)` (`telemetry/reducer.py
:_profile_to_dict`), so its keys are the `LossProfile` field names verbatim,
tuples rendered as lists. The fields that matter most when tracing a run:

| Field | Meaning |
|---|---|
| `entry_id`, `epoch_id`, `generation_id`, `run_id` | identity |
| `drift_counts[].{kind,severity,count}` | one entry per `(kind, severity)` bucket; `kind` is the **lowercase wire-canonical** drift-kind string (`off_topic`, `confabulation_risk`, …); a fired custom judge appears as `kind: "custom:<judge_name>"` |
| `per_judge_loss[].{judge_name,raw_loss,weight,weighted_loss}` | the custom-judge attribution split — what scoring weights via `per_judge_weights`; empty when no custom judge fired |
| `plan_revisions`, `task_failure_ratio`, `wall_clock_budget_exceeded` | other drift/loss features |
| `drift_loss` | the weighted scalar the tournament scores on (computed in the reducer — the one place with both counts and weights; already includes the per-judge contributions) |
| `pass_fail` | the entry's expectation verdict; `None` when there is no expectation |
| `expectation_result.{kind,passed,detail,score,metrics}` | the matcher's record; `None` when no expectation was attached |
| `score` | top-level **continuous** per-entry quality in `[0,1]`; `None` (pre-score profiles / no expectation) → downstream derives `1.0`/`0.0` from `pass_fail`, so a binary board is byte-identical to before |
| `metrics` | optional per-entry decomposition a scorer exposed (e.g. `{"precision": .., "recall": ..}`), carried straight through so the proposer's failure-mode profile can read it without re-running the scorer |
| `metric_counts[]` | the generalised namespaced-metric superset (`drift:…`, `cost:…`, `rubric:…`); `unified_metrics()` merges drift + metric counts |
| `turns_completed`, `memory_failure_count`, `context_loss_count` | multi-turn features |
| `runtime_ms`, `match_id`, `cached`/`source_epoch`/`source_run` | runtime + provenance |
| `adk_session_id` | the harmonograf deep-link key (below) |

Drift counts feed both the proposer (as hypothesis-shaped features) and the
tournament (`drift_loss`, `pass_fail`). If `drift_counts` is identically empty
across a whole epoch, the goldfive stream probably isn't reaching the reducer —
that's the `flat_drift_signal` critical in
[zicato-diagnose-health](../zicato-diagnose-health/SKILL.md).

> **Provenance of the weights.** `per_judge_loss` is the per-run *attribution*,
> but the weights that produced it (`per_judge_weights` / `default_judge_weight`
> / `pass_rate_monotonicity_scope`) only score correctly because they now
> survive the subprocess-worker transport — a serialize/deserialize symmetry
> (`tournament/runner.py:_weights_spec` ↔ `_tournament_worker.py
> :_weights_from_args`). A field present in-process but dropped from that
> transport is silently reset to its default inside the worker (it once scored
> all custom-judge drift at weight `1.0`), so a `per_judge_loss` whose weights
> don't match `scoring.json` is the tell. Tracing provenance, not tuning values
> ([`zicato-tune-scoring`](../zicato-tune-scoring/SKILL.md)).

> **`tool_observed` events.** Beyond reasoning/decision envelopes, the stream
> carries goldfive's tool ledger — one `kind == "tool_observed"` event per tool
> call (`tool_name`, `args_preview`, `result_preview`, `is_error`,
> `error_message`). This is the ground-truth record of *what the agent did*; a
> process judge that grades the deliverable reads it (off
> `session.recent_events`), not the model's narration. See
> [zicato-design-judges](../zicato-design-judges/SKILL.md).

### Regenerate the analyzer insight out of band

```sh
.venv/bin/zicato inspect telemetry --workspace .zicato            # current epoch, latest
.venv/bin/zicato inspect telemetry --epoch <id> --round <N>       # named round
```

`evolve` runs the analyzer per round automatically; reach for this only to
(re)generate an insight after the fact. Real flags: `--workspace`,
`--epoch`, `--round`.

## The third stream: one structured log per invocation

Distinct from run telemetry, each `evolve` / `reflect run` invocation writes
`.zicato/logs/<utc-stamp>-<pid>.jsonl` — every `zicato.*` log record, structured,
with the `epoch_id` / `generation_id` / `run_id` context bound in. Worker
subprocesses append to the SAME file, so a parallel round's records land in one
stream. At most 20 streams are retained, oldest pruned first.

```sh
.venv/bin/zicato inspect logs --workspace .zicato --list             # streams, newest first
.venv/bin/zicato inspect logs --workspace .zicato --follow           # tail the latest
.venv/bin/zicato inspect logs --workspace .zicato --invocation <stamp>-<pid> --level WARNING
```

Capture floor is INFO (override with `ZICATO_LOG_LEVEL`); `--level` re-filters on
read. **Logs are observability only** — nothing in scoring / gate / journal reads
them back, so they explain a run but never define it.

## One harmonograf server, many sessions

There is exactly **one** harmonograf server for an evolve invocation (zicato
auto-launches one, or you pin an external one via `zicato evolve
--harmonograf-url` / the `harmonograf_url` config key). It hosts **many sessions**, and URLs
deep-link to a session:

```
<harmonograf_url>/#/session/<adk_session_id>
```

Two *kinds* of session live on that one server — this is the load-bearing
distinction:

| Session kind | What it traces | `session_id` shape | Telemetry file |
|---|---|---|---|
| **meta-loop** (the tool itself) | zicato's *own* proposer + judge LLM calls — the orchestrator deciding what to mutate and how to score | `zicato-meta-loop-<sanitized-evolve-start-iso>` (one stable id per evolve invocation) | `.zicato/runtime/meta_loop_events.jsonl` |
| **per-board-run** (the system under test) | one target run against one board entry and replicate | event-stream session id (also persisted as `adk_session_id`) | the per-run `events.jsonl` above |

The meta-loop session is "zicato thinking about the system under test"; the
per-board-run sessions are "the system under test running". They bucket as
distinct sessions so the dashboard renders the meta-loop as one continuous
timeline rather than scattering its events across per-round sessions. The
meta-loop emitter is best-effort and strictly additive — telemetry never
blocks or breaks the evolve loop.

### Following a run into harmonograf

From the dashboard, each L4 run drill-down renders an **Open in harmonograf**
link with exactly the `/#/session/<adk_session_id>` href above — but only while a
run is **LIVE**. zicato's auto-launched harmonograf dies with the run while the
heartbeat's `harmonograf_url` lingers, so a link built from a known URL alone
would point at a dead port; the builders return nothing unless an active
tournament or active run says otherwise. The `adk_session_id` comes off the run's events
(`session_id` / `sessionId`) and is carried on its `loss.json`. To go from a
run directory to its harmonograf view: read `adk_session_id` from the run,
then open `<harmonograf_url>/#/session/<that_id>`. See
[zicato-watch-dashboard](../zicato-watch-dashboard/SKILL.md) for driving the
browser.

For a live tournament, **Open tournament traces** opens the same session
picker filtered by the exact `zicato.tournament_id` session label. Switch
among the matching sessions there; each Activity/Graph/Trajectory view still
shows one target execution. Harmonograf's filter is generic—the label names
and their tournament meaning belong to zicato. Session labels contain only
coordinates (`epoch`, tournament, matchup, generation, entry, side,
replicate, trace kind), never prompts or expected answers.

### The emulator lane

Multi-turn runs emit the user-emulator's per-turn LLM calls on a dedicated
lane named **`zicato:emulator`** (bracketed `emulator_turn`, carrying the
emulator `model` and previews). The lane name is the discriminator: anything
on `zicato:emulator` is the emulator's work, anything on the target
lane is the agent's. This is why a multi-turn entry's wall-clock can exceed
the agent's own thinking time — the emulator's LLM time counts toward the
entry's `wall_clock_budget_seconds`.

Session scope is one generation × board entry × replicate. Separate board
entries and replicates must never share a target or emulator session. Several
turns share state only when they are steps of one compound board entry.

## Guardrails

- **Files are canonical, index is derived.** Trace runs from `events.jsonl` /
  `loss.json`, not `index.db` (the index lags to generation boundaries and is
  rebuildable via `zicato repair index`).
- Cite only flags in real `--help` (`analyze-telemetry`: `--workspace`,
  `--epoch`, `--round`).
- Never start a live `evolve` to produce telemetry — read existing run
  directories.
