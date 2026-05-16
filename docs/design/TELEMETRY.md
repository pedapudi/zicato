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
- The `LossProfile` shape, field by field.
- Multi-turn aggregation (turn-bounded vs run-bounded views).
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

1. Constructs the sink:

   ```python
   from goldfive.sinks.persistence import JSONLPersistenceSink

   sink = JSONLPersistenceSink(
       path=".zicato/epochs/{epoch}/generations/v{N}/runs/{entry_id}/events.jsonl",
       mode="write",   # NEVER "append" — see §1.2
   )
   ```

2. Hands the sink to `goldfive.run` / `goldfive.wrap` (or the
   adapter's equivalent):

   ```python
   await goldfive.run(inner_harness, board_entry.input, sinks=[sink])
   ```

3. Awaits the terminal event (`RunCompleted` or `RunAborted`).
4. Calls `await sink.close()` to flush.
5. Hands the JSONL path to the post-run reducer (§2).

### 1.2 Why `mode="write"`, never `"append"`

`JSONLPersistenceSink` supports both `"append"` and `"write"`.
**zicato always uses `"write"`**. Each run gets its own file; appending
multiple runs to one file would silently corrupt run boundaries and
the reducer relies on each file being exactly one run.

The path layout enforces this: every `(epoch, generation, entry_id)`
triple maps to a distinct path. Reruns of the same `(epoch,
generation, entry_id)` overwrite — the operator's intent is "redo this
entry against this generation".

### 1.3 No live UX in v0

A live-tail view (drift counts ticking up as the run progresses) is
an ergonomic addition, not a foundational primitive. If `zicato run
--tail` ever needs one, the right shape is an in-process accumulator
sink alongside the JSONL one (strictly additive — the JSONL sink still
captures the canonical record). v0 does not ship this; harmonograf
already exists for the live view and is the right tool when an
operator wants to watch a generation run unfold.

## 2. The post-run reducer

The reducer is a **function**, not a sink. Sinks make incremental
decisions about each event as it arrives; the reducer runs once per
run with full visibility over the whole stream. That shape is
strictly better for derivation work:

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
  reducer stamps `aborted=true` with `abort_reason="no_terminal_event"`
  and computes whatever features it can.
- Malformed lines (parse errors) propagate the parser's exception.
  The runner catches and logs them; the per-run loss profile records
  the failure.

In practice both cases are rare — goldfive's sink flushes per-line
and the adapter is responsible for emitting a terminal event. But the
reducer's job is to be safe against operational reality, not just
the happy path.

## 3. `LossProfile`

The reducer's output. The contract every other zicato component reads
from. Pattern detectors and tournament scoring are blind to JSONL —
they read `LossProfile`s.

```python
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class LossProfile:
    # --- identity ---
    entry_id: str
    epoch_id: str
    generation: str
    tags: list[str]

    # --- drift features ---
    drift_counts_by_kind: dict[str, int] = field(default_factory=dict)
    drift_counts_by_judge: dict[str, int] = field(default_factory=dict)
    drift_counts_by_severity: dict[str, int] = field(default_factory=dict)
    escalations: int = 0
    plan_revisions: int = 0
    task_failure_ratio: float = 0.0
    human_intervention_required: bool = False

    # --- multi-turn features ---
    turn_count: int = 0
    drift_counts_by_kind_per_turn: list[dict[str, int]] = field(default_factory=list)
    stopped_reason: Optional[str] = None  # "stop_when" / "max_turns" / "script_exhausted" / "abort"

    # --- runtime features ---
    runtime_ms: int = 0
    aborted: bool = False
    abort_reason: Optional[str] = None

    # --- derived ---
    drift_loss: float = 0.0    # weighted scalar (see SCORING.md)
    pass_fail: Optional[bool] = None   # None if expectations list is empty
    rubric_scores: list[float] = field(default_factory=list)  # scores from rubric-kind checks
```

The fields, in groups:

### 3.1 Identity

The triple `(entry_id, epoch_id, generation)` uniquely names this
profile. `tags` is duplicated from the board entry so pattern detectors
can slice without re-joining.

### 3.2 Drift features

| Field | Computation |
|---|---|
| `drift_counts_by_kind` | Count `DriftDetected` payloads bucketed by the symbolic `kind` (e.g. `"DRIFT_KIND_CONFABULATION_RISK"`). Custom-judge violations all bucket under `"DRIFT_KIND_CUSTOM"`. |
| `drift_counts_by_judge` | Count `DRIFT_KIND_CUSTOM` payloads bucketed by `judge_name` — the per-custom-judge breakdown. See §3.2.1. |
| `drift_counts_by_severity` | Same, bucketed by `severity` (`"INFO"` / `"WARNING"` / `"CRITICAL"`). |
| `escalations` | Count of `DriftDetected` payloads whose `lifecycle == DRIFT_LIFECYCLE_ESCALATING`. |
| `plan_revisions` | Count of `PlanRevised` payloads. |
| `task_failure_ratio` | `TaskFailed` count / max(1, `TaskStarted` count). |
| `human_intervention_required` | True if any drift kind was `DRIFT_KIND_HUMAN_INTERVENTION_REQUIRED`. |

The buckets use the symbolic enum names (the strings from the
`.proto`) rather than the integer values. This keeps the JSON
self-describing and survives proto-enum reordering in goldfive
without invalidating historical loss profiles.

#### 3.2.1 Custom judges and `DRIFT_KIND_CUSTOM`

A board entry can carry **process** checks — `Judge.custom` /
`Judge.python` — in its `judges` list (see
[BOARD-FORMAT.md](BOARD-FORMAT.md) §4 and
[BOARD-AUTHORING.md](BOARD-AUTHORING.md) §3). goldfive evaluates these
custom judges against the live reasoning stream, and a violation is
emitted as a `JudgementEmitted` event with `kind == DriftKind.CUSTOM`
and `judge_name` set to the `Judge`'s `name`.

The reducer treats these like any other drift event — they land in
`drift_counts_by_kind` under `"DRIFT_KIND_CUSTOM"`. But because every
custom judge shares that one kind, the kind alone cannot tell two
judges apart. The reducer therefore also buckets `DRIFT_KIND_CUSTOM`
payloads by their `judge_name` into `drift_counts_by_judge`:

```json
"drift_counts_by_kind":  {"DRIFT_KIND_CUSTOM": 3, "DRIFT_KIND_LOOPING_REASONING": 1},
"drift_counts_by_judge": {"cite-before-metric": 2, "ack-before-edit": 1}
```

`drift_counts_by_judge` is what the scoring layer reads to apply
`ScoringWeights.per_judge_weights` — the per-judge weight is keyed on
`judge_name` (see [SCORING.md](SCORING.md) §2.2). It is also what the
journal and the pattern detectors use to attribute a custom-judge
failure to a specific judge.

goldfive's built-in judges emit their own native `DriftKind`s, not
`CUSTOM`, so they never appear in `drift_counts_by_judge` — they are
already discriminated by kind.

The drift kinds zicato cares about most are documented in goldfive's
DRIFT.md; the full taxonomy is `DriftKind` in
`goldfive/proto/goldfive/v1/types.proto`. Notable kinds for the v0
dogfood (presentation agent):

- `DRIFT_KIND_CONFABULATION_RISK` — research-shaped task produced
  output without calling a tool. Fires often when a research
  specialist's prompt doesn't require source-checking.
- `DRIFT_KIND_CAPABILITY_MISMATCH` — coordinator delegated to an
  agent whose tools can't perform the bound task.
- `DRIFT_KIND_LOOPING_REASONING` — chain-of-thought repeated across
  turns.
- `DRIFT_KIND_LOOPING_TOOL_CALL` — same tool called with same args.
- `DRIFT_KIND_PLAN_DIVERGENCE` — what the agent did doesn't match
  what the plan said.
- `DRIFT_KIND_INTENT_DIVERGENCE` — agent pursued a goal different
  from `session.goals`.

### 3.3 Multi-turn features

These fields are populated only on multi-turn entries.

| Field | Computation |
|---|---|
| `turn_count` | Number of agent turns observed (`AgentInvocationStarted` events from the inner harness lane). |
| `drift_counts_by_kind_per_turn` | One dict per turn, same shape as `drift_counts_by_kind` but bounded to that turn. |
| `stopped_reason` | Why the conversation ended: `"stop_when"`, `"max_turns"`, `"script_exhausted"`, or `"abort"`. |

Both turn-bounded and run-bounded counts are useful and the cost of
keeping both is small (one extra walk through the events keyed by
turn boundaries).

Per-turn slices let pattern detectors surface "memory failure" shapes
— agent re-asked the same question across turns, agent forgot a fact
established earlier. These shapes are not new drift kinds; they are
zicato-level computations from goldfive's events + the transcript.

### 3.4 Runtime features

| Field | Computation |
|---|---|
| `runtime_ms` | Terminal event's `emitted_at` minus `RunStarted.started_at`, in milliseconds. |
| `aborted` | True if terminal event is `RunAborted`. |
| `abort_reason` | The `RunAborted.reason` string, or `None`. |

`runtime_ms` is bounded by `wall_clock_budget_seconds * 1000` plus a
small grace period the adapter's abort path takes.

### 3.5 Derived

| Field | Source |
|---|---|
| `drift_loss` | Weighted scalar computed from the drift features. See [SCORING.md](SCORING.md). |
| `pass_fail` | The AND of the entry's `expectations` (outcome checks) evaluated against the run result. `None` when the `expectations` list is empty. |
| `rubric_scores` | The numeric scores returned by the entry's `rubric`-kind outcome checks, in `expectations` order. Useful for the journal — including advisory rubrics (`threshold=None`) whose scores are recorded but do not gate `pass_fail`. |

`drift_loss` is computed in the reducer (not in a downstream component)
because the reducer is the single place that has both the per-kind
counts and the weights. Pattern detectors and tournament scoring read
the scalar.

## 4. Multi-turn aggregation: run-bounded and turn-bounded

Goldfive's drift events fire per turn (the planner refines per-turn;
detectors fire per-turn). A multi-turn entry produces many drift
events across many turns. Two aggregation views matter:

- **Run-bounded:** total drift counts across the whole conversation.
  This is the comparable-to-single-turn view; the tournament uses it
  to score the entry.
- **Turn-bounded:** counts per turn. This is the per-turn shape view;
  pattern detectors use it to surface progression patterns ("the
  CONFABULATION_RISK drift fires on turn 3 specifically, not earlier",
  "LOOPING_REASONING ramps from turn 4 onward").

The reducer computes both. The run-bounded view is the field
`drift_counts_by_kind`; the turn-bounded view is the list field
`drift_counts_by_kind_per_turn` where index `i` is turn `i`'s counts.

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

Operators replaying a run in harmonograf can see exactly what the
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
| `drift_counts_by_kind` | yes (per-kind movement is hypothesis-shaped) | yes (weighted into `drift_loss`) |
| `drift_counts_by_judge` | yes (per-custom-judge movement is hypothesis-shaped) | yes (the `CUSTOM` slice of `drift_loss`, weighted by `per_judge_weights`) |
| `drift_counts_by_severity` | yes | yes |
| `escalations` | yes | yes |
| `plan_revisions` | yes | yes |
| `task_failure_ratio` | yes | yes |
| `human_intervention_required` | yes | no (already captured by drift counts; a yes/no flag would double-count) |
| `turn_count` | yes (efficiency signal) | no |
| `drift_counts_by_kind_per_turn` | yes (per-turn pattern) | no (run-bounded counts dominate the score) |
| `stopped_reason` | yes (efficiency signal — did the persona's `stop_when` fire?) | no |
| `runtime_ms` | yes | partial (only the budget-exhaustion case adds a loss term) |
| `aborted` | yes | yes (heavy loss term) |
| `pass_fail` | yes | yes (the pass-rate side of the score) |
| `rubric_scores` | yes (journal-only — not directly fed to the proposer's input by default) | no (`pass_fail` already carries each rubric's threshold verdict) |

The proposer sees aggregated patterns (§4.6 of the architecture doc),
not raw loss profiles. The tournament sees `drift_loss` and `pass_fail`
from the loss profiles, not the raw counts. This keeps the two views
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
│     path=".zicato/epochs/{epoch}/generations/v{N}/runs/{entry_id}/events.jsonl",
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
