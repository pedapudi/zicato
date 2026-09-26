# Telemetry

zicato consumes telemetry, it does not produce a new wire format. Under
the default `goldfive` telemetry dialect, every run of the system under
test emits a `goldfive.v1.Event` stream that zicato captures verbatim
through goldfive's own `JSONLPersistenceSink`, then reduces post-run into
a typed `LossProfile`. The JSONL file is the canonical record; the
`LossProfile` is the surface every other component reads.
[TELEMETRY-DIALECTS.md](TELEMETRY-DIALECTS.md) specifies the other
producers (`adk_events`, `transcript`), which feed the same
`LossProfile`.

This document covers:

- How zicato captures the event stream (no custom EventSink).
- The post-run reducer — its inputs, its output, why it is a function
  and not a sink.
- The harmonograf session model (one server per workspace, many
  sessions) and the deep-link route.
- The `LossProfile` shape, field by field.
- Multi-turn aggregation (run-bounded counts + derived signals).
- The emulator's per-turn audits and their `zicato:emulator` lane.
- What's used as feature vs as loss.

## 1. No zicato-specific EventSink

goldfive ships `JSONLPersistenceSink` at
`goldfive.sinks.persistence.JSONLPersistenceSink`. It already does
the right thing:

- Proto-canonical serialization via `MessageToJson(event,
  sort_keys=True, indent=None)` for byte-stable output.
- Async-safe writes (a single `asyncio.Lock` serialises concurrent
  emits so lines never interleave).
- Lazy file-handle open on first emit (constructing the sink is
  side-effect-free).
- A companion `replay_from_jsonl(path)` helper that parses the JSONL
  back into proto `Event` messages.

Adding a `ZicatoSink` would be a thin per-run-path wrapper over the
same. It would also couple zicato to the `EventSink` ABI for no gain.
zicato composes goldfive's sink and avoids the dependency.

### 1.1 Wiring per run

For every measurement (one entry, one generation, one replicate), zicato:

1. Constructs the list of sinks via
   `zicato.telemetry.sink.make_run_sinks(...)`. The list always
   includes the canonical per-run `JSONLPersistenceSink`:

   ```python
   from goldfive.sinks.persistence import JSONLPersistenceSink

   sink = JSONLPersistenceSink(
       path=events_jsonl_path(workspace_root, epoch_id, generation_id, entry_id, measurement),
       mode="write",   # NEVER "append" — see §1.2
   )
   ```

   The path comes from `zicato.core.workspace.events_jsonl_path`, which
   resolves through the workspace layout:
   `.zicato/epochs/{epoch}/generations/{generation_id}/runs/{entry_id}/[seed-{n}/]events.{purpose}.r{draw}.jsonl`
   (for example `events.tournament.r0.jsonl` for replicate zero of a
   tournament measurement). When a harmonograf URL is
   resolvable (`resolve_harmonograf_url`), `make_run_sinks` **also**
   appends a `harmonograf_client.HarmonografSink` so the run streams
   live to the harmonograf console. That attachment is strictly
   best-effort: a missing `harmonograf_client`, or any failure
   building the sink, is logged at `warning` and the run continues
   JSONL-only. The JSONL sink is the source of truth; harmonograf is
   an additive live view.

2. Hands the sinks to `goldfive.run` / `goldfive.wrap` (or the
   adapter's equivalent):

   ```python
   await goldfive.run(system_under_test, board_entry.input, sinks=sinks)
   ```

3. Awaits the terminal event (`RunCompleted` or `RunAborted`).
4. Closes the sinks to flush.
5. Hands the JSONL path to the post-run reducer (§2), which writes the
   matching `loss.{purpose}.r{draw}.json` beside the events file. Beside
   those two files the run directory also holds the captured result and
   judge input/output (`result.*.json`, `judge_io.*.jsonl`) and any run
   artifacts ([STORAGE.md §5.2.1](STORAGE.md#521-the-mutable-surface-is-code-only--artifact-exclusion)).

### 1.2 Why `mode="write"`, never `"append"`

`JSONLPersistenceSink` supports both `"append"` and `"write"`.
**zicato always uses `"write"`**. Each measurement gets its own file;
appending multiple runs to one file would silently corrupt run
boundaries and the reducer relies on each file being exactly one run.

The path layout enforces this: every `(epoch, generation, entry_id,
seed, purpose, draw)` coordinate maps to a distinct path. A rerun of the
same coordinate overwrites, and the file it truncates is first kept as
`events.{purpose}.r{draw}.prev.jsonl` (`archive_prior_events`), so a
re-measured unit's previous raw telemetry survives one overwrite.

### 1.3 The live view is harmonograf

zicato does not build its own live-tail primitive (drift counts
ticking up inside zicato as a run progresses). The live view is
harmonograf: `make_run_sinks` (§1.1) attaches a `HarmonografSink`
alongside the canonical JSONL sink whenever a harmonograf URL is in
scope, so every run streams to the harmonograf console as it unfolds.
`zicato evolve` resolves that URL, launching or reusing the workspace's
harmonograf server when none is configured (see §1.4), so the live view
is on by default. A zicato-side accumulator would take the shape of an
additive in-process sink alongside the JSONL one, with the JSONL sink
staying the canonical record. It is unbuilt, because harmonograf already
serves the need.

### 1.4 One harmonograf server, many sessions

There is **one** persistent harmonograf server per workspace and
**many** sessions on it — harmonograf multiplexes sessions, so a
single console shows every timeline. [HARMONOGRAF.md](HARMONOGRAF.md)
specifies the server lifecycle and the session taxonomy; in brief:

- **Per-board-run sessions.** Each tournament run (one generation ×
  one board entry × one replicate) is its own harmonograf session.
  goldfive mints a fresh session id per run and stamps it as
  `session_id` on every emitted event, so the JSONL file and the
  harmonograf server see the same id.
- **The meta-loop session.** The orchestrator's own goldfive
  events — the proposer's evaluation LLM call and the in-process
  process-judge calls (e.g. the decision-telemetry analyzer's insight
  call) — are conceptually a distinct session from any board run.
  They are bucketed under one stable id per evolve invocation,
  `zicato-meta-loop-<sanitized-iso>`, where the suffix is the evolve
  start ISO timestamp with `:`→`-` and ` `→`_` (so it is URL-safe);
  the id is built by
  `zicato.telemetry.harmonograf_supervisor.meta_loop_session_id`. The
  meta-loop's canonical JSONL is written to
  `<workspace>/.zicato/runtime/meta_loop_events.jsonl` by the
  `MetaLoopEmitter` (`zicato.telemetry.meta_loop`), and the same
  harmonograf server receives the meta-loop sink when a URL is in
  scope. The emitter reuses goldfive's canonical envelopes
  (`AgentInvocationStarted` / `AgentInvocationCompleted`,
  `JudgementEmitted`), so the dashboard and reducer need no
  meta-loop-specific code path.

#### Deep-linking into a session

The dashboard deep-links into harmonograf at
`<harmonograf_url>/#/session/<adk_session_id>`. The `adk_session_id`
is the session id observed in the run's events file; the reducer
extracts it and stamps it into the loss file
(`LossProfile.adk_session_id`) so the dashboard can build the link
without re-opening the event stream. The harmonograf URL itself is
resolved by `resolve_harmonograf_url`: the `--harmonograf-url` flag or
the `integration.harmonograf_url` setting first, then the runtime
context a worker inherits from its parent.

## 2. The post-run reducer

The reducer is a **function** rather than a sink. A sink makes
incremental decisions about each event as it arrives; the reducer runs
once per run with full visibility over the whole stream. That shape suits
derivation work:

- The reducer can compute features that depend on the relationship
  between events (e.g. `task_failure_ratio`).
- It can compute features that depend on the terminal event (e.g.
  `runtime_ms`).
- It is trivially testable in isolation — feed it a fixture JSONL,
  assert on the `LossProfile` out.

### 2.1 Signature

```python
from pathlib import Path
from zicato.core import BoardEntry, LossProfile, ScoringWeights

def reduce_loss(
    events_jsonl_path: Path,
    entry: BoardEntry,
    generation_id: str,
    epoch_id: str,
    expectation_result: ExpectationResult | None,
    runtime_ms: int,
    wall_clock_budget_exceeded: bool,
    weights: ScoringWeights,
    final_output: str | None = None,
    run_not_completed: bool = False,
) -> LossProfile:
    """Read the events file, reduce it, and return a LossProfile."""
```

`reduce_loss` lives in `zicato.telemetry.reducer`. It does not write the
result; the worker writes the loss file next to the events file.

The reducer takes the entry and the already-evaluated expectation
result because:

- The expectation verdict (`expectation_result`) decides `pass_fail`
  and the continuous `score`; the worker evaluates the entry's
  predicates against the run result and hands the verdict in.
- `entry.kind` decides whether the multi-turn signals (§3.3) are
  computed.

The `weights` parameter is the epoch's frozen `ScoringWeights` — see
[SCORING.md](SCORING.md). The reducer uses them to select the telemetry
dialect and to compute the scalar `drift_loss`.

### 2.2 Reading the JSONL

Under the `goldfive` dialect the reducer prefers goldfive's
`replay_from_jsonl` for strict proto parsing and converts each message
into an event record. When goldfive is not importable, or its strict
parser refuses the file (for example a file mixing field-name
spellings), the reducer reads the file through
`zicato.telemetry.event_log.read_event_log` instead
([TELEMETRY-DIALECTS.md §1](TELEMETRY-DIALECTS.md#1-one-reader-under-every-dialect)).
Either way the records carry one spelling of the payload case and its
field names, and the reducer walks them once in emit order.

### 2.3 Handling truncated / malformed JSONL

A run that crashed before the goldfive boundary closed may leave a
JSONL without a terminal event. The reducer handles this gracefully:

- If the last event is not a `RunCompleted` / `RunAborted`, the
  reducer computes whatever features it can from the partial stream.
  A budget-exhaustion abort (`wall_clock_budget_exceeded`) or any other
  abnormal termination (`run_not_completed`) floors
  `task_failure_ratio` to `1.0` and sets `not_completed`, which scoring
  treats as worst-case for the entry
  ([SCORING.md §2.3](SCORING.md#23-the-failure-channel)).
- A line that is not a JSON object is counted malformed and skipped;
  invalid UTF-8 decodes to replacement characters; a missing file reads
  as an empty log. The reducer logs the malformed-line count as a
  warning rather than raising.

Both cases are rare, because goldfive's sink flushes per-line and the
adapter is responsible for emitting a terminal event. The reducer is
nonetheless written to be safe against operational reality rather than
only against the expected path.

## 3. `LossProfile`

The reducer's output. The contract every other zicato component reads
from. Pattern detectors and tournament scoring are blind to JSONL —
they read `LossProfile`s. The dataclass is defined in
`zicato.core.loss` (re-exported from `zicato.core`); the shape below
lists its fields in declaration order.

The structure is **flat by design** — every field is a scalar, a
tuple of scalars, or a tuple of small frozen dataclasses
(`MetricCount`, `JudgeLoss`) — so the profile
round-trips through JSON and is diffable in the journal. Counts are
carried as *tuples of typed measurement rows* rather than as `dict`s.

```python
from dataclasses import dataclass
from zicato.core.types import (
    MetricCount, JudgeLoss, ExpectationResult,
)

@dataclass(frozen=True, slots=True)
class LossProfile:
    # --- identity ---
    run_id: str            # the run id from the events, or {generation_id}:{entry_id}
    entry_id: str
    generation_id: str
    epoch_id: str

    # --- drift + outcome features ---
    plan_revisions: int
    task_failure_ratio: float
    runtime_ms: int
    wall_clock_budget_exceeded: bool
    expectation_result: ExpectationResult | None

    # --- derived ---
    drift_loss: float                      # weighted scalar (see SCORING.md)
    pass_fail: bool | None                 # None when no expectation attached

    # --- multi-turn extras (None on single-turn entries) ---
    turns_completed: int | None = None
    memory_failure_count: int | None = None
    context_loss_count: int | None = None

    # --- generalised metric surface ---
    metric_counts: tuple[MetricCount, ...] = ()
    tokens_spent: int = 0
    output_chars: int = 0
    schema_failures: int = 0

    # --- harmonograf deep-link and matchup ---
    adk_session_id: str = ""               # /#/session/<adk_session_id>
    match_id: str = ""                     # the matchup this run ran within

    # --- per-judge attribution and judge failures ---
    per_judge_loss: tuple[JudgeLoss, ...] = ()
    judge_errors: tuple[JudgeError, ...] = ()

    # --- reuse provenance ---
    cached: bool = False
    source_epoch: str = ""
    source_run: str = ""

    # --- continuous outcome ---
    score: float | None = None             # per-entry quality in [0, 1]
    metrics: dict[str, float] | None = None

    # --- scoring and abort provenance ---
    scoring_provenance: str | None = None  # see SCORING.md §10.4
    abort_cause: str | None = None         # e.g. "budget_exhausted", "parent_kill"
    not_completed: bool = False
    not_completed_reason: str | None = None

    # --- execution and measurement identity ---
    started_at: str | None = None
    ended_at: str | None = None
    execution_started: bool | None = None
    measurement: MeasurementDraw | None = None
    source_measurements: tuple[MeasurementDraw | None, ...] = ()
```

The fields, in groups:

### 3.1 Identity

The quad `(run_id, entry_id, generation_id, epoch_id)` names this
profile, and `measurement` names the purpose, draw, and seed of the
measurement. `run_id` is the first `run_id` found in the events; a run
whose events carry none falls back to `{generation_id}:{entry_id}`.
The worker's run directory keys the measurement on disk (§1.1); the
`run_id` plays no part in the path.

### 3.2 Drift counts and outcome features

| Field | Computation |
|---|---|
| `plan_revisions` | Number of plan-revision events observed. |
| `task_failure_ratio` | Fatally-failed tasks / total tasks, in `[0.0, 1.0]`. |
| `runtime_ms` | Total wall-clock duration in milliseconds. |
| `wall_clock_budget_exceeded` | `True` iff the run hit `BoardEntry.wall_clock_budget_seconds` and was force-aborted; scoring then treats the run as worst-case for the entry. |
| `not_completed` / `not_completed_reason` | Set for any non-success terminal state (budget exhausted, crash, harness exception, emulator abort, killed worker); charged in the `failure:` channel ([SCORING.md §2.3](SCORING.md#23-the-failure-channel)). |
| `expectation_result` | The `ExpectationResult(kind, passed, detail)` of evaluating the entry's expectation, or `None` when the entry had no expectation (or the run aborted before it could fire). |

Named metrics retain each drift kind as a ``drift:<kind>`` name and keep
info, warning, and critical observations in separate severity buckets.

#### 3.2.1 Custom judges and `custom:<judge_name>`

A board entry can carry **process** checks — `Judge.custom` /
`Judge.python` — in its `judges` list (see
[BOARD-FORMAT.md](BOARD-FORMAT.md) §4 and
[BOARD-AUTHORING.md](BOARD-AUTHORING.md) §3). goldfive evaluates these
custom judges against the live reasoning stream; an adverse verdict is
emitted as a `DriftDetected` of kind `custom`, paired with a
`JudgementEmitted` carrying the judge's stable `judge_name`.

The reducer pairs each drift with its judge and records the name as
`drift:custom:<judge_name>`. Separate judges retain separate measurements:

```json
"metric_counts": [
  {"name": "drift:looping_reasoning", "severity": "warning", "count": 1},
  {"name": "drift:custom:cite-before-metric", "severity": "critical", "count": 2},
  {"name": "drift:custom:ack-before-edit", "severity": "warning", "count": 1}
]
```

`per_judge_loss` carries the severity-weighted attribution and each judge's
configured multiplier. These values enter the `judge:` scoring channel;
`drift_loss` excludes them so the same event contributes once.

goldfive's built-in judges emit their own native drift kinds (not
`custom`), so they are already discriminated by kind and never need
the `custom:` prefix.

The drift kinds zicato cares about most are documented in goldfive's
DRIFT.md; the full taxonomy is `DriftKind` in
`goldfive/proto/goldfive/v1/types.proto`. Notable kinds for the
presentation-agent dogfood target:

- `confabulation_risk` — research-shaped task produced output without
  calling a tool. Fires often when a research specialist's prompt
  doesn't require source-checking.
- `capability_mismatch` — coordinator delegated to an agent whose
  tools can't perform the bound task.
- `looping_reasoning` — chain-of-thought repeated across turns.
- `looping_tool_call` — same tool called with same args.
- `plan_divergence` — what the agent did doesn't match the plan.
- `intent_divergence` — agent pursued a goal different from
  `session.goals`.

### 3.3 Multi-turn extras

These fields are `None` on single-turn entries.

| Field | Computation |
|---|---|
| `turns_completed` | Number of conversational turns the run executed before terminating (by `stop_when`, `max_turns`, or abort). |
| `memory_failure_count` | Zicato-derived: how many times the inner agent re-asked something the simulated user had already answered. The reducer computes it; goldfive does not emit it. |
| `context_loss_count` | Zicato-derived: how many times the agent appeared to forget a fact established earlier in the conversation. Same multi-turn-pattern detector as `memory_failure_count`. |

These shapes are not new drift kinds; they are zicato-level
computations from goldfive's events plus the transcript.

### 3.4 Generalised metric surface

The reducer generalises drift counts into a namespaced metric surface
so the same per-run unit can carry cost, latency, rubric, and
schema-failure metrics alongside drift.

| Field | Computation |
|---|---|
| `metric_counts` | Named measurements as `MetricCount(name, severity, count)` rows. Names carry a namespace, severity preserves event buckets, and the numeric count supports rates, scores, durations, and fractional replicate means. `LossProfile.scoring_metrics()` adds the derived judge, failure, and runtime channels. |
| `tokens_spent` | First-class scalar mirrored into `metric_counts` as `"cost:tokens_spent"`. |
| `output_chars` | First-class scalar mirrored as `"output:chars"`. |
| `schema_failures` | First-class scalar mirrored as `"schema:failures"`. |

`runtime_ms` (§3.2) is bounded by `wall_clock_budget_seconds * 1000`
plus a small grace period the adapter's abort path takes; the
`wall_clock_budget_exceeded` flag records the exhaustion case.

### 3.5 Derived

| Field | Source |
|---|---|
| `drift_loss` | Weighted scalar computed from drift metrics and plan revisions. Judge-attributed events enter their own channel. Higher = worse. See [SCORING.md](SCORING.md). |
| `pass_fail` | Derived from `expectation_result`. `None` when no expectation was attached, so pass-rate aggregation across the board can ignore entries without ground truth. |

`drift_loss` is computed in the reducer (not in a downstream component)
because the reducer is the single place that has both the per-kind
counts and the weights. Pattern detectors and tournament scoring read
the scalar.

### 3.6 Harmonograf deep-link: `adk_session_id`

`adk_session_id` is the ADK/goldfive session id carried on every
event envelope in the run's `events.jsonl` (the `sessionId` field).
The reducer extracts it and stamps it onto the profile so the
dashboard can build the harmonograf deep-link
`<harmonograf_url>/#/session/<adk_session_id>` (§1.4) without
re-opening the event stream. It is the empty string when the events
file is absent or carries no envelope `sessionId`.

### 3.7 Per-judge attribution: `per_judge_loss`

`per_judge_loss` is a tuple of `JudgeLoss(judge_name, raw_loss,
weight, weighted_loss)` rows — one per custom judge that fired
against the run. Each judge's `weighted_loss` (`raw_loss * weight`)
enters the scalar through the `judge:` channel as a `judge:<name>`
metric, and `drift_loss` excludes it. `per_judge_loss` also carries the
attribution out of the reducer so the analyzer's per-judge
drift-attribution view and the analytical index's `judge_losses`
table (see [ANALYTICAL-INDEX.md §3.9](ANALYTICAL-INDEX.md#39-judge_losses))
can answer "which judges drove this run's loss" without re-walking
`events.jsonl`. `raw_loss` is the judge's unweighted
severity-weighted drift sum; `weight` is the
`per_judge_weights` multiplier (falling back to
`default_judge_weight`); the empty-string `judge_name` is the
catch-all bucket for `custom`-kind drifts the reducer could not pair
with a `JudgementEmitted`.

## 4. Multi-turn aggregation

Goldfive's drift events fire per turn (the planner refines per-turn;
detectors fire per-turn). A multi-turn entry produces many drift
events across many turns. The reducer aggregates them **run-bounded**:
`metric_counts` is the total per (kind, severity) across the whole
conversation. This is the comparable-to-single-turn view, and it is
the view the tournament uses to score the entry.

zicato does not ship a per-turn `metric_counts` breakdown as a profile
field. The per-turn *shape* questions ("did the agent re-ask
something already answered", "did it forget a fact established
earlier") are instead surfaced as the derived multi-turn signals
`memory_failure_count` and `context_loss_count` (§3.3), computed by
the reducer from goldfive's events plus the transcript. These are
zicato-level computations rather than new goldfive drift kinds.

### 4.1 Where the multi-turn signals come from

goldfive's event stream carries no first-class user or assistant
messages, so the reducer reconstructs a best-effort transcript from the
payloads that carry short text: `AgentInvocationCompleted.summary`,
`TaskCompleted.summary`, and `RunCompleted.outcome_summary` stand in
for agent turns, and `RunStarted.goal_summary` for the user's request
(`_agent_and_user_turns_from_events` in `zicato.telemetry.reducer`).
`turns_completed` counts the reconstructed agent turns, and the
memory-failure and context-loss heuristics run over them. The
`adk_events` and `transcript` dialects supply real message turns
instead ([TELEMETRY-DIALECTS.md](TELEMETRY-DIALECTS.md)).

### 4.2 The emulator's `zicato:emulator` audit lane

The multi-turn user emulator records one audit per turn
(`EmulatorTurnAudit`, `zicato.emulator.audit`). Each audit carries:

- `persona_hash` — a short SHA-256 fingerprint of the persona, so audits
  correlate across runs without revealing the persona body;
- `transcript_chars_in` — the size of the prompt fed to the emulator,
  a cost proxy;
- `output_chars_out` and `output_preview` — the size and the first 200
  characters of the emulator's reply.

A driver constructed with a sink (`EmulatedMultiTurnDriver(sink_emit_fn=...)`)
also emits each audit as a plain event on the lane `zicato:emulator`
with kind `zicato.emulator.turn_audit`; emission is best-effort, and an
audit failure is logged and never fails the run. The tournament path
does not wire a sink: `run_emulated` accepts the run's sinks but does not
pass them to the emulator, so audits stay in memory and no
`zicato:emulator` events reach the events file or harmonograf.

### 4.3 What the emulator lane is for

The lane would let an operator replaying a run see what the emulator
produced on each turn and roughly what it cost. The emulator's model
time counts against the entry's `wall_clock_budget_seconds`, so the lane
would explain "why did this multi-turn entry take 8 minutes when the
agent only spent 4 minutes thinking?". The lane name is the
discriminator: anything emitted on `zicato:emulator` is the emulator's
work, anything emitted on the system-under-test lane is the agent's
work. The audit record is specified in [EMULATOR.md](EMULATOR.md).

## 5. What's a feature, what's a loss

Some `LossProfile` fields are **features** the proposer reads to form
hypotheses; others contribute to **loss** that the tournament uses
for scoring. Some are both. The split:

| Field | Feature? | Loss? |
|---|---|---|
| `metric_counts` | yes (per-(kind, severity) movement is hypothesis-shaped) | yes (severity-weighted into `drift_loss`) |
| `per_judge_loss` | yes (per-custom-judge movement is hypothesis-shaped) | yes (each judge's `weighted_loss` enters the `judge:` channel) |
| `plan_revisions` | yes | yes |
| `task_failure_ratio` | yes | yes |
| `turns_completed` | yes (efficiency signal) | no |
| `memory_failure_count` | yes (multi-turn pattern) | no (run-bounded drift counts dominate the score) |
| `context_loss_count` | yes (multi-turn pattern) | no |
| `tokens_spent` / `output_chars` / `schema_failures` | yes (cost/output signals) | through the `cost:`, `output:`, and `schema:` channels, at the contract's coefficients (defaults `0.001`, `0.0`, `5.0`) |
| `runtime_ms` | yes | through the `runtime:` channel (default coefficient `0.0`) |
| `wall_clock_budget_exceeded` / `not_completed` | yes | yes (the `failure:` channel's worst-case charge) |
| `pass_fail` | yes | yes (the pass-rate side of the score) |
| `expectation_result` | yes (journal-only — the matcher detail) | no (`pass_fail` already carries the verdict) |

The proposer sees aggregated patterns
([ARCHITECTURE.md §4.6](ARCHITECTURE.md#46-pattern-detectors)) rather
than raw loss profiles. The tournament sees the per-channel aggregates, the
`pass_fail` and `score` outcomes, and the scalar built from them rather
than the raw counts. This keeps the two views
clean: the proposer reasons in patterns; the tournament reasons in
scalars.

## 6. Patterns: what aggregates across runs

Pattern detectors (`ALL_DETECTORS` in `zicato.patterns.detectors`)
read the champion's loss profiles and events on the training slice of
the board at the start of each round and emit typed `Pattern` objects.
The shipped kinds:

| Pattern kind | What it surfaces |
|---|---|
| `drift_metric_frequency` | A drift kind firing on at least a fifth of the runs. |
| `cost_metric_frequency` | A cost metric recurring across runs. |
| `rubric_metric_frequency` | A rubric score recurring across runs. |
| `hot_task` | A task whose failed-or-blocked rate stands well above the median. |
| `hot_agent` | An agent drawing far more drift events than the mean agent. |
| `plan_revision_instability` | Runs that revise their plan unusually often. |
| `multi_turn_memory_failure` | The agent re-asked something already answered. |
| `multi_turn_context_loss` | The agent forgot a fact established earlier in the conversation. |

Patterns are recomputed each round from the current champion's
measurements, so they never carry across an epoch boundary (the
contract changed).

## 7. Determinism and reproducibility

The JSONL file is the canonical record. Given the same:

- System under test source (a generation snapshot)
- Board entry
- `target_call_llm` callable behaviour
- `evaluation_call_llm` callable behaviour (for multi-turn emulated)

… two runs *should* produce similar JSONL. They won't be byte-equal —
LLM calls are usually non-deterministic — but the drift counts should
cluster. The tournament answers run-to-run noise with replication: each
entry runs once per replicate (two by default) and the per-entry losses
are averaged, and the replicate count can be sized from the epoch's
measured noise floor ([SELECTION.md §9.1](SELECTION.md#91-the-measured-noise-floor-sizes-the-replicate-count-and-the-racing-cuts)).
The reduction itself is deterministic: re-reducing the same events file
under the same contract yields the same `LossProfile`.

Tournaments remain vulnerable to noise the replicates do not average
out. The promote margin (see [SCORING.md](SCORING.md)) and the optional
evidence gate guard against promoting candidates that beat the parent by
noise alone.

## 8. Telemetry path in detail

Putting it all together, the full per-run telemetry path:

```
                                    ┌─────────────────────────────┐
                                    │  goldfive.run(harness, input, sinks=[the_sink, ...])
                                    └──────────────┬──────────────┘
                                                   │ emits goldfive.v1.Event stream
                                                   ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ JSONLPersistenceSink(                                                        │
│     path=".../runs/{entry_id}/[seed-{n}/]events.{purpose}.r{draw}.jsonl",    │
│     mode="write",                                                            │
│ )                                                                            │
│                                                                              │
│ writes one JSON-line-per-event, byte-stable, async-safe                      │
└──────────────────────────────────────────┬───────────────────────────────────┘
                                           │
                                           │ (RunCompleted / RunAborted observed)
                                           │
                                           ▼
                                   ┌───────────────────┐
                                   │ reduce_loss(...)  │  reads the events
                                   └─────────┬─────────┘  file, walks events,
                                             │            takes the expectation
                                             │            verdict, computes
                                             │            drift_loss
                                             ▼
                          ┌─────────────────────────────────────┐
                          │ loss.{purpose}.r{draw}.json         │
                          │ (LossProfile)                       │
                          └─────────────────────────────────────┘
                                             │
                                             ▼
                                  Pattern detectors + Tournament
```

Nothing in this path requires a zicato-specific protocol. The single
foreign dependency is goldfive — which is the whole point: the
ecosystem already produced the right event stream, zicato consumes
it.

## 9. Cross-references

| Topic | Document |
|---|---|
| Drift kinds zicato counts | goldfive's `proto/goldfive/v1/types.proto` (the `DriftKind` enum) |
| Event envelope, sink contract | goldfive's `proto/goldfive/v1/events.proto` and `docs/design/EVENT-MODEL.md` |
| Persistence sink API | goldfive's `goldfive.sinks.persistence.JSONLPersistenceSink` |
| Drift loss scalar formula | [SCORING.md](SCORING.md) |
| Emulator audit-trail spans | [EMULATOR.md](EMULATOR.md) |
| What the pattern detectors do with loss profiles | [ARCHITECTURE.md §4.6](ARCHITECTURE.md#46-pattern-detectors) |
