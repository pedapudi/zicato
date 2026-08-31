# Telemetry

zicato consumes telemetry, it does not produce a new wire format. Every
run of the inner harness emits a `goldfive.v1.Event` stream that
zicato captures verbatim through goldfive's own
`JSONLPersistenceSink`, then reduces post-run into a typed
`LossProfile`. The JSONL file is the canonical record; the
`LossProfile` is the surface every other component reads.

This document covers:

- How zicato captures the event stream (no custom EventSink).
- The post-run reducer — its inputs, its output, why it is a function
  and not a sink.
- The harmonograf session model (one server, many sessions) and
  the deep-link route.
- The `LossProfile` shape, field by field.
- Multi-turn aggregation (run-bounded counts + derived signals).
- The emulator's `zicato:emulator` lane for harmonograf visibility.
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

For every run (one entry, one generation), zicato:

1. Constructs the list of sinks via
   `zicato.telemetry.sink.make_run_sinks(...)`. The list always
   includes the canonical per-run `JSONLPersistenceSink`:

   ```python
   from goldfive.sinks.persistence import JSONLPersistenceSink

   sink = JSONLPersistenceSink(
       path=".zicato/epochs/{epoch}/generations/{generation_id}/runs/{entry_id}/events.jsonl",
       mode="write",   # NEVER "append" — see §1.2
   )
   ```

   The exact path comes from `zicato.core.workspace.events_jsonl_path`
   so the layout stays in one place. When a harmonograf URL is
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
   await goldfive.run(inner_harness, board_entry.input, sinks=sinks)
   ```

3. Awaits the terminal event (`RunCompleted` or `RunAborted`).
4. Closes the sinks to flush.
5. Hands the JSONL path to the post-run reducer (§2), which writes
   `loss.json` next to `events.jsonl`. Per-run telemetry is therefore
   exactly two files in the run directory: `events.jsonl` (the
   canonical event stream) and `loss.json` (the reduced
   `LossProfile`).

### 1.2 Why `mode="write"`, never `"append"`

`JSONLPersistenceSink` supports both `"append"` and `"write"`.
**zicato always uses `"write"`**. Each run gets its own file; appending
multiple runs to one file would silently corrupt run boundaries and
the reducer relies on each file being exactly one run.

The path layout enforces this: every `(epoch, generation, entry_id)`
triple maps to a distinct path. Reruns of the same `(epoch,
generation, entry_id)` overwrite — the operator's intent is "redo this
entry against this generation".

### 1.3 The live view is harmonograf

zicato does not build its own live-tail primitive (drift counts
ticking up inside zicato as a run progresses). The live view is
harmonograf: `make_run_sinks` (§1.1) attaches a `HarmonografSink`
alongside the canonical JSONL sink whenever a harmonograf URL is in
scope, so every run streams to the harmonograf console as it unfolds.
`zicato evolve` resolves that URL, auto-launching an in-process
harmonograf server when none is configured (see §1.4), so the live view
is on by default. A zicato-side accumulator would take the shape of an
additive in-process sink alongside the JSONL one, with the JSONL sink
staying the canonical record. It is unbuilt, because harmonograf already
serves the need.

### 1.4 One harmonograf server, many sessions

There is **one** harmonograf server per `evolve` invocation and
**many** sessions on it — harmonograf multiplexes sessions, so a
single console shows every timeline:

- **Per-board-run sessions.** Each tournament run (one generation ×
  one board entry) is its own harmonograf session. Its session id is
  the synthetic run id `{generation_id}--{entry_id}` — the same id the
  index's `runs` table keys on. The parent and child generations each
  produce their own run, hence their own session, for the same entry.
- **The meta-loop session.** The orchestrator's own goldfive
  events — the proposer's auxiliary LLM call and the in-process
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
is the goldfive/ADK session id observed in the run's `events.jsonl`;
the reducer extracts it and stamps it into `loss.json`
(`LossProfile.adk_session_id`) so the dashboard can build the link
without re-opening the event stream. The harmonograf URL itself is
the auto-launched-or-configured one resolved by
`resolve_harmonograf_url` (`--harmonograf-url` / the `config.json`
`harmonograf_url` key, or — on the auto-launch path — the internal
`ZICATO_HARMONOGRAF_URL` handoff).

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
from zicato.telemetry import LossProfile
from zicato.types import BoardEntry, RunResult

def reduce_run(
    events_jsonl: Path,
    *,
    entry: BoardEntry,
    run_result: RunResult,
    weights: dict[str, float],
) -> LossProfile:
    """Read the JSONL, walk the events, and produce a LossProfile.

    The function does not write the result. The caller (the runner)
    writes ``loss.json`` next to ``events.jsonl``. Keeping the
    function pure makes it testable without filesystem fixtures.
    """
    ...
```

The reducer accepts the entry and the typed run result because:

- The entry's `expectation` is matched against the run result here,
  not anywhere else. Centralising it means the reducer is the single
  place "did this pass?" is decided.
- The entry's `tags` are stamped onto the `LossProfile` so the
  pattern detectors don't need to re-join later.

The `weights` parameter is the per-epoch scoring weights from
`scoring.json` — see [SCORING.md](SCORING.md). The reducer uses them
to compute the scalar `drift_loss`.

### 2.2 Reading the JSONL

The reducer uses goldfive's `replay_from_jsonl`:

```python
from goldfive.sinks.persistence import replay_from_jsonl

events = replay_from_jsonl(events_jsonl)
```

This returns a list of parsed proto `Event` messages in emit order.
The reducer walks them once, dispatching on `evt.WhichOneof("payload")`
to update its working counters.

### 2.3 Handling truncated / malformed JSONL

A run that crashed before the goldfive boundary closed may leave a
JSONL without a terminal event. The reducer handles this gracefully:

- If the last event is not a `RunCompleted` / `RunAborted`, the
  reducer computes whatever features it can from the partial stream;
  a budget-exhaustion abort is recorded via
  `wall_clock_budget_exceeded`, which scoring then treats as
  worst-case for the entry.
- Malformed lines (parse errors) propagate the parser's exception.
  The runner catches and logs them; the per-run loss profile records
  the failure.

Both cases are rare, because goldfive's sink flushes per-line and the
adapter is responsible for emitting a terminal event. The reducer is
nonetheless written to be safe against operational reality rather than
only against the expected path.

## 3. `LossProfile`

The reducer's output. The contract every other zicato component reads
from. Pattern detectors and tournament scoring are blind to JSONL —
they read `LossProfile`s. The dataclass is defined in
`zicato.core.types` (not `zicato.telemetry`); the shape below mirrors
that definition.

The structure is **flat by design** — every field is a scalar, a
tuple of scalars, or a tuple of small frozen dataclasses
(`DriftCount`, `MetricCount`, `JudgeLoss`) — so the profile
round-trips through JSON and is diffable in the journal. Counts are
carried as *tuples of typed measurement rows* rather than as `dict`s.

```python
from dataclasses import dataclass
from zicato.core.types import (
    DriftCount, MetricCount, JudgeLoss, ExpectationResult,
)

@dataclass(frozen=True, slots=True)
class LossProfile:
    # --- identity ---
    run_id: str            # the synthetic {generation_id}--{entry_id}
    entry_id: str
    generation_id: str
    epoch_id: str

    # --- drift + outcome features ---
    drift_counts: tuple[DriftCount, ...]   # (kind, severity, count) rows
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
    metric_counts: tuple[MetricCount, ...] = ()  # superset of drift_counts
    tokens_spent: int = 0
    output_chars: int = 0
    schema_failures: int = 0

    # --- harmonograf deep-link ---
    adk_session_id: str = ""               # /#/session/<adk_session_id>

    # --- per-judge attribution ---
    per_judge_loss: tuple[JudgeLoss, ...] = ()
```

The fields, in groups:

### 3.1 Identity

The quad `(run_id, entry_id, generation_id, epoch_id)` names this
profile. `run_id` is the synthetic `{generation_id}--{entry_id}` id —
the same id the analytical index's `runs` table keys on and the same
id used as the per-board-run harmonograf session (§1.4).

### 3.2 Drift counts and outcome features

| Field | Computation |
|---|---|
| `drift_counts` | A tuple of `DriftCount(kind, severity, count)` rows. `kind` is the lowercase wire-canonical drift-kind string (see `zicato.core.drift_kinds`); `severity` is `"info"` / `"warning"` / `"critical"`. A given kind may appear in several rows, one per severity bucket. Custom-judge violations fold into a `kind` of `"custom:<judge_name>"` — see §3.2.1. |
| `plan_revisions` | Number of plan-revision events observed. |
| `task_failure_ratio` | Fatally-failed tasks / total tasks, in `[0.0, 1.0]`. |
| `runtime_ms` | Total wall-clock duration in milliseconds. |
| `wall_clock_budget_exceeded` | `True` iff the run hit `BoardEntry.wall_clock_budget_seconds` and was force-aborted; scoring then treats the run as worst-case for the entry. |
| `expectation_result` | The `ExpectationResult(kind, passed, detail)` of evaluating the entry's expectation, or `None` when the entry had no expectation (or the run aborted before it could fire). |

`DriftCount.kind` carries the wire-canonical lowercase string, not
the symbolic proto enum integer — this keeps the JSON self-describing
and survives proto-enum reordering in goldfive without invalidating
historical loss profiles.

#### 3.2.1 Custom judges and `custom:<judge_name>`

A board entry can carry **process** checks — `Judge.custom` /
`Judge.python` — in its `judges` list (see
[BOARD-FORMAT.md](BOARD-FORMAT.md) §4 and
[BOARD-AUTHORING.md](BOARD-AUTHORING.md) §3). goldfive evaluates these
custom judges against the live reasoning stream; an adverse verdict is
emitted as a `DriftDetected` of kind `custom`, paired with a
`JudgementEmitted` carrying the judge's stable `judge_name`.

The reducer attributes each such drift to its authoring judge by
folding the judge name into the `DriftCount.kind` as
`"custom:<judge_name>"` (via
`zicato.telemetry.reducer._judge_attributed_kind`). So two different
custom judges appear as two distinct `DriftCount` kinds rather than
collapsing into one bucket:

```json
"drift_counts": [
  {"kind": "looping_reasoning",      "severity": "warning",  "count": 1},
  {"kind": "custom:cite-before-metric", "severity": "critical", "count": 2},
  {"kind": "custom:ack-before-edit",    "severity": "warning",  "count": 1}
]
```

The aggregate `drift_loss` already sums in every judge's
contribution, but it does not preserve *which* judge drove the loss.
`per_judge_loss` (§3.6) carries that attribution out separately. The
per-judge weight applied is `ScoringWeights.per_judge_weights` keyed
on `judge_name` (see [SCORING.md §2.2](SCORING.md#22-the-judge-channel)).

goldfive's built-in judges emit their own native drift kinds (not
`custom`), so they are already discriminated by kind and never need
the `custom:` prefix.

The drift kinds zicato cares about most are documented in goldfive's
DRIFT.md; the full taxonomy is `DriftKind` in
`goldfive/proto/goldfive/v1/types.proto`. Notable kinds for the v0
dogfood (presentation agent):

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
| `metric_counts` | A tuple of `MetricCount(name, severity, count)` rows. `name` is namespaced (`"drift:looping_reasoning"`, `"cost:input_tokens"`, `"rubric:slide_structure"`, ...); `count` is a float so the same row can carry counts, rates, scores, and durations. When populated it is a **superset** of `drift_counts` (every drift row also appears under the `"drift:"` namespace). When left empty, `LossProfile.unified_metrics()` synthesises it on the fly from `drift_counts` plus the first-class scalars. |
| `tokens_spent` | First-class scalar mirrored into `metric_counts` as `"cost:tokens_spent"`. |
| `output_chars` | First-class scalar mirrored as `"output:chars"`. |
| `schema_failures` | First-class scalar mirrored as `"schema:failures"`. |

`runtime_ms` (§3.2) is bounded by `wall_clock_budget_seconds * 1000`
plus a small grace period the adapter's abort path takes; the
`wall_clock_budget_exceeded` flag records the exhaustion case.

### 3.5 Derived

| Field | Source |
|---|---|
| `drift_loss` | Weighted scalar computed from `drift_counts` (severity-weighted, with per-judge weights folded in). Higher = worse. See [SCORING.md](SCORING.md). |
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
file is absent or carries no envelope `sessionId`. Back-compat
default `""` so profiles written before the field was added load
cleanly.

### 3.7 Per-judge attribution: `per_judge_loss`

`per_judge_loss` is a tuple of `JudgeLoss(judge_name, raw_loss,
weight, weighted_loss)` rows — one per custom judge that fired
against the run. The aggregate `drift_loss` already sums in each
judge's `weighted_loss` (`raw_loss * weight`), but it does not
preserve which judge drove the loss. `per_judge_loss` carries that
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
`drift_counts` is the total per (kind, severity) across the whole
conversation. This is the comparable-to-single-turn view, and it is
the view the tournament uses to score the entry.

zicato does not ship a per-turn `drift_counts` breakdown as a profile
field. The per-turn *shape* questions ("did the agent re-ask
something already answered", "did it forget a fact established
earlier") are instead surfaced as the derived multi-turn signals
`memory_failure_count` and `context_loss_count` (§3.3), computed by
the reducer from goldfive's events plus the transcript. These are
zicato-level computations rather than new goldfive drift kinds.

### 4.1 Turn boundaries

The reducer identifies turn boundaries by looking for the agent's
top-level `AgentInvocationStarted` events on the inner-harness lane.
Each pair `(AgentInvocationStarted, AgentInvocationCompleted)` brackets
one agent turn; events emitted between them are bucketed to that turn.

A nuance: sub-agent invocations (AgentTool nesting) emit their own
`AgentInvocationStarted/Completed` pairs. The reducer attributes
nested events to the **outermost** ongoing invocation — which is the
turn boundary the operator cares about. Sub-agent dispatch is
internal to one turn.

### 4.2 Per-turn LLM calls on the `zicato:emulator` lane

The multi-turn emulator's per-turn LLM call emits
`GoldfiveLLMCallStart` / `GoldfiveLLMCallEnd` events on a dedicated
lane name: `zicato:emulator`. These events:

- Are bracketed by `name="emulator_turn"`.
- Carry `model` (the emulator's model).
- Carry an `input_preview` that includes the persona's `goal` (hashed,
  for privacy) and the count of transcript chars passed in.
- Carry an `output_preview` with the emulator's produced user turn.

The lane name `zicato:emulator` is convention. Harmonograf renders
events keyed by their `target_agent_id` or by the lane convention
established for goldfive's internal-LLM spans, so the emulator's
work shows up as its own row on the Gantt.

### 4.3 What the emulator lane is for

Operators replaying a run in harmonograf see what the
emulator produced and what it cost. The emulator's LLM time
contributes to the entry's `wall_clock_budget_seconds`, so visibility
on that lane explains "why did this multi-turn entry take 8 minutes
when the agent only spent 4 minutes thinking?".

The emulator lane is **not** a new wire format. It uses goldfive's
existing `GoldfiveLLMCallStart` / `GoldfiveLLMCallEnd` proto messages.
The convention is that the lane name is the discriminator: anything
emitted on `zicato:emulator` is the emulator's work, anything emitted
on the inner-harness lane is the agent's work.

The audit-trail span shape is specified in
[EMULATOR.md](EMULATOR.md).

## 5. What's a feature, what's a loss

Some `LossProfile` fields are **features** the proposer reads to form
hypotheses; others contribute to **loss** that the tournament uses
for scoring. Some are both. The split:

| Field | Feature? | Loss? |
|---|---|---|
| `drift_counts` | yes (per-(kind, severity) movement is hypothesis-shaped) | yes (severity-weighted into `drift_loss`) |
| `per_judge_loss` | yes (per-custom-judge movement is hypothesis-shaped) | yes (each judge's `weighted_loss` is already summed into `drift_loss` via `per_judge_weights`) |
| `plan_revisions` | yes | yes |
| `task_failure_ratio` | yes | yes |
| `turns_completed` | yes (efficiency signal) | no |
| `memory_failure_count` | yes (multi-turn pattern) | no (run-bounded drift counts dominate the score) |
| `context_loss_count` | yes (multi-turn pattern) | no |
| `tokens_spent` / `output_chars` / `schema_failures` | yes (cost/output signals) | no by default (available to the scorer via `metric_counts` if an epoch weights them) |
| `runtime_ms` | yes | partial (only the budget-exhaustion case adds a loss term) |
| `wall_clock_budget_exceeded` | yes | yes (worst-case loss term for the entry) |
| `pass_fail` | yes | yes (the pass-rate side of the score) |
| `expectation_result` | yes (journal-only — the matcher detail) | no (`pass_fail` already carries the verdict) |

The proposer sees aggregated patterns (§4.6 of the architecture doc),
not raw loss profiles. The tournament sees `drift_loss` and `pass_fail`
from the loss profiles rather than the raw counts. This keeps the two views
clean: the proposer reasons in patterns; the tournament reasons in
scalars.

## 6. Patterns: what aggregates across runs

Pattern detectors read every `LossProfile` written so far in the
epoch and emit typed `Pattern` objects. The detectors are
out-of-scope for this document (full taxonomy is on the roadmap once
the v0 dogfood produces real signal), but a few canonical kinds:

| Pattern kind | What it surfaces |
|---|---|
| `drift_concentration_by_kind` | One drift kind dominating loss across many entries. |
| `tag_slice_regression` | Pass-rate dropped between generations on entries tagged X. |
| `drift_persistence` | The same drift kind on the same entry across generations. |
| `multi_turn_memory_failure` | Agent forgot facts established earlier (cross-turn detection). |
| `unmoved_surface` | Mutation-point ids that have not been touched this epoch. |

Patterns reset on epoch boundaries (the contract changed). Within an
epoch they accumulate.

## 7. Determinism and reproducibility

The JSONL file is the canonical record. Given the same:

- Inner harness source (a generation snapshot)
- Board entry
- `harness_call_llm` callable behaviour
- `auxiliary_call_llm` callable behaviour (for multi-turn emulated)

… two runs *should* produce similar JSONL. They won't be byte-equal —
LLM calls are usually non-deterministic — but the drift counts should
cluster. Run-to-run noise is a known issue; the right response is
multi-trial scoring per entry (each entry run N times against each
generation), which v0 does not ship but the scoring infrastructure
admits.

Tournaments are vulnerable to this noise. The default tournament
margin (see [SCORING.md](SCORING.md)) is set conservatively to avoid
promoting candidates that beat the parent by noise alone.

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
│     path=".zicato/epochs/{epoch}/generations/{generation_id}/runs/{entry_id}/events.jsonl",
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
                                   │  reduce_run(...)  │  reads JSONL via
                                   └─────────┬─────────┘  replay_from_jsonl,
                                             │            walks events,
                                             │            applies expectation,
                                             │            computes drift_loss
                                             ▼
                          ┌─────────────────────────────────────┐
                          │  loss.json (LossProfile)            │
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
