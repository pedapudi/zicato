# Architecture

This document explains what zicato is, what problem it solves, where it
sits in the goldfive + harmonograf ecosystem, and how its internal
components compose into a self-improving loop.

It is the entry point for the design-doc set. Read this first; the
other documents drill into a single concern (the [mutation
surface](MUTATION-SURFACE.md), the [board format](BOARD-FORMAT.md), the
[scoring model](SCORING.md), and so on) and assume the architecture in
this file.

## 1. What zicato is and why

zicato is a **meta-harness** for multi-agent systems. It takes a
multi-agent system you have already built — a coordinator with
specialists, a deep `sub_agents` tree, a single planning agent, an
arbitrary callable that fronts a model — and turns it into the *inner
harness* of a learning loop.

Across many runs of that inner harness, zicato:

1. Captures structured runtime telemetry (the `goldfive.v1.Event`
   stream the inner harness emits).
2. Reduces that telemetry to a typed **loss profile** per run.
3. Aggregates loss profiles across runs into **patterns** — recurring
   shapes of failure (e.g. "the research specialist keeps hallucinating
   sources when the question is short", "the coordinator delegates to
   the writer before the researcher has reported").
4. Proposes typed **patches** to the inner harness's annotated mutation
   surface (specialist instructions, coordinator routing strings, tool
   descriptions, planner templates, judge prompts — never free-form
   source edits).
5. Runs **tournaments** between the parent and the candidate generation
   against a frozen board of tasks.
6. Promotes the candidate when it wins by a configured margin and does
   not regress on pre-existing pass-rate.

The loop is self-improving in the sense that every promoted generation
becomes the parent of the next round. Generations form a DAG.
**Epochs** group generations under a stable evaluation contract — the
board, the rubric, the scoring weights all hold steady — so generations
within an epoch are directly comparable. Cross-epoch comparison is
fuzzy by design (the contract changed; the goalposts moved).

### Why this is a separate library

The orchestration scaffolding that makes drift legible — goals, plans,
per-turn drift analysis, the intervention ladder — already exists. It
is goldfive. The observability + steering console that makes drift
*visible to operators* already exists. It is harmonograf. What did not
exist before zicato was the layer that consumes the same telemetry
**across runs** and acts on it by rewriting the harness itself.

The three libraries have non-overlapping cadences:

| Library | Cadence | Acts on |
|---|---|---|
| goldfive | within one run | the live plan (refine on drift, intervene at Level 0-5) |
| harmonograf | within one run | the operator's view (steer, pause, cancel; annotate) |
| **zicato** | **across generations** | **the inner harness's source** (rewrite annotated spans, run tournaments) |

Keeping zicato a separate library keeps the cadence clean. goldfive
must never reach across runs; harmonograf must never reach into the
agent's source. zicato is the only thing that does either.

### What zicato is *not*

- Not a planner. It does not propose plans; it proposes patches to the
  things that produce plans (the planner's prompt, the coordinator's
  routing instructions, specialist system prompts).
- Not an LLM client. It calls models only through a caller-supplied
  `call_llm(system: str, user: str, model: str) -> str` callable. The
  core never imports a vendor SDK.
- Not framework-coupled. The inner harness can be anything that exposes
  a `HarnessAdapter`. Google ADK is the first concrete adapter;
  plain-callable and LangChain follow.
- Not a runtime steerer. Live runs go through goldfive (and
  harmonograf, if the operator wants the console). zicato only acts
  between runs.
- Not a single-file editor. The mutation surface is annotated and
  granular — a span, or at most a whole file marked mutable. zicato
  never rewrites the inner harness's tree at large.

## 2. The meta-loop, end to end

```
                                                ┌────────────────────────────┐
                                                │  zicato-supervisor (Rust)  │
                                                │  ────────────────────────  │
                                                │  spawned by `evolve`;      │
                                                │  watches .zicato/runtime/  │
                                                │  serves the live dashboard │
                                                │  (HTTP + SSE on :7892).    │
                                                │                            │
                                                │  Reads heartbeat.json,     │
                                                │  active_runs/*, controls.  │
                                                │  Escalates SIGTERM →       │
                                                │  SIGKILL on stalled work.  │
                                                └────────────┬───────────────┘
                                                             │ inotify; signals
                                                             │
   ┌─────────────────────────────────────────────────────────┼───────────────┐
   │                       zicato meta-loop                  │               │
   │                       (orchestrator)                    │               │
   │   ┌──────────────┐    ┌───────────────────────────────┐ │               │
   │   │  Board       │    │  Inner harness (HarnessAdapter)│ │               │
   │   │ (.jsonl,     │    │                                │ │               │
   │   │  frozen      │    │   any multi-agent system       │ │               │
   │   │  per epoch)  │    │   exposing:                    │ │               │
   │   └──────┬───────┘    │     run_entry(entry) -> result │ │               │
   │          │            │     mutation_points() -> [...] │ │               │
   │          │            └─────────────┬──────────────────┘ │               │
   │          ▼                          │                    │               │
   │   ┌──────────────┐                  │                    ▼               │
   │   │  Runner      │ goldfive.wrap()  │     .zicato/runtime/heartbeat.json │
   │   │  (per entry) ├─────────────────►│     .zicato/runtime/active_runs/*  │
   │   └──────┬───────┘   one run        │     atomic-renamed every 2s + on   │
   │          │                          │     every phase transition         │
   │          ▼                          ▼                           │
   │   ┌──────────────────────────────────────┐                      │
   │   │  goldfive event stream               │                      │
   │   │   ── JSONLPersistenceSink ──►        │                      │
   │   │  events.jsonl  (one file per run)    │                      │
   │   └──────────────────┬───────────────────┘                      │
   │                      │                                          │
   │                      ▼                                          │
   │              ┌────────────────────┐                             │
   │              │  Loss reducer      │  walks events.jsonl,        │
   │              │  (post-run)        │  emits loss.json (typed)    │
   │              └─────────┬──────────┘                             │
   │                        │                                        │
   │                        ▼                                        │
   │              ┌────────────────────┐                             │
   │              │  Pattern detectors │  aggregate across runs      │
   │              │                    │  in the current epoch       │
   │              └─────────┬──────────┘                             │
   │                        │                                        │
   │                        ▼                                        │
   │              ┌────────────────────┐                             │
   │              │  Patch proposer    │  reads patterns + rubric    │
   │              │  (auxiliary LLM)   │  emits Experiment           │
   │              │                    │   = hypothesis + patches    │
   │              └─────────┬──────────┘                             │
   │                        │                                        │
   │                        ▼                                        │
   │              ┌────────────────────┐                             │
   │              │  Applier           │  resolves mutation-point    │
   │              │                    │  ids, rewrites annotated    │
   │              │                    │  spans, validates the tree  │
   │              │                    │  parses + imports survive   │
   │              └─────────┬──────────┘                             │
   │                        │                                        │
   │                        ▼                                        │
   │              ┌────────────────────┐                             │
   │              │  Tournament        │  re-runs the WHOLE board    │
   │              │  (default mode)    │  against parent + candidate │
   │              │                    │  computes per-gen score     │
   │              └─────────┬──────────┘                             │
   │                        │                                        │
   │                        ▼                                        │
   │              ┌────────────────────┐                             │
   │              │  Promotion gate    │  margin on drift score      │
   │              │                    │  AND strict monotonicity    │
   │              │                    │  on pre-existing pass-rate  │
   │              └─────────┬──────────┘                             │
   │                        │                                        │
   │                        ▼                                        │
   │              ┌────────────────────┐                             │
   │              │  Journal + outcome │  experiment.json `outcome`  │
   │              │                    │  block + journal.md row     │
   │              └─────────┬──────────┘                             │
   │                        │                                        │
   │                        ▼                                        │
   │              (next round; promoted candidate is new parent)     │
   │                                                                 │
   └─────────────────────────────────────────────────────────────────┘
```

The shaded box is a single **round**. A round advances one generation
within an epoch — `v3 → v4` — or rejects the proposal and stays at the
parent. Many rounds happen within an epoch; an epoch is closed by the
operator (or auto-closed on the next `epoch new`) and an analysis pass
runs over its journal.

## 3. Cadence comparison

zicato fits the ecosystem at a strictly slower cadence than its
siblings. Every component of zicato sees runs in aggregate; nothing in
zicato runs while a single run is in flight.

| Property | goldfive | harmonograf | zicato |
|---|---|---|---|
| Acts on | live plan / live agent invocation | live UI + control channel | inner-harness source code |
| Unit of work | one turn | one operator action | one **round** (parent vs candidate over a board) |
| Per-run? | yes, every turn | yes, every annotation | no — between runs only |
| Cross-run? | no | no (per-session views aside) | yes — the whole point |
| Loss model | drift detected this turn | n/a | reduced from events; aggregated across runs in an epoch |
| Owns | `Plan` / `Task` / `Drift*` state machines | UI + control channel | `MutationPoint` / `Experiment` / `Generation` / `Epoch` |
| Mutates source? | no | no | **yes** — annotated spans only |

The cadence separation is also a safety boundary. goldfive must never
edit the harness's source; harmonograf must never act between runs;
zicato must never act inside a run. Violating any of those would let
two systems race on the same surface with incompatible models of what
"now" means.

## 4. Component-by-component

The rest of this document walks each component in the meta-loop in
order. Each section names the component, gives its responsibility in
one paragraph, then lists what it consumes, what it produces, and the
key contracts. The full schemas live in the topic-specific docs linked
below.

### 4.1 HarnessAdapter

**Responsibility.** The narrow protocol that decouples zicato from any
specific multi-agent framework. The adapter implementer is the
inner-harness author; zicato treats the adapter as the only handle on
the system under test.

**Consumes.** A registration (`zicato register --adk path:agent
[--mutable-tree <path>]` for the ADK adapter; one entrypoint per
adapter kind). The registration captures the agent factory and the
list of source roots that contain mutation-point annotations.

**Produces.** Two pieces of behaviour:

- `async run_entry(entry: BoardEntry, *, sinks: list[EventSink]) -> RunResult`
  — exercises the inner harness against one board entry, with the
  caller-supplied sinks attached. The adapter is responsible for
  wrapping the inner harness in `goldfive.wrap` (or
  `harmonograf_client.observe(goldfive.wrap(...))` when the operator
  wants harmonograf live), driving the entry's input (single-turn) or
  conversation (multi-turn scripted / emulated), and returning a typed
  `RunResult` shaped by the entry kind.
- `mutation_points() -> list[MutationPoint]` — walks every registered
  source root and returns every annotated mutation point (span markers
  and file markers; see [MUTATION-SURFACE.md](MUTATION-SURFACE.md)).
  The return value is a list because v0+1 needs to walk multiple roots
  for cross-repo dogfood (target 2 — see
  [DOGFOOD-TARGETS.md](DOGFOOD-TARGETS.md)) even though v0 typically
  uses one.

**Contracts.**

- The adapter MUST emit a `goldfive.v1.RunStarted` and exactly one
  terminal event (`RunCompleted` or `RunAborted`) per entry. The
  zicato runner relies on this to bound the JSONL file per entry.
- The adapter MUST exhaust the entry's `wall_clock_budget_seconds` on
  itself — if the inner harness runs over budget, the adapter aborts
  the inner work and emits `RunAborted(reason="wall_clock_budget")`.
- The adapter MAY attach additional sinks for its own use (logging,
  in-process accumulators) but MUST NOT modify the sinks zicato
  supplied.

### 4.2 Board

**Responsibility.** The frozen-per-epoch list of tasks the inner
harness is evaluated against. One JSONL file at
`.zicato/epochs/{epoch}/board.jsonl`. One entry per line.

**Consumes.** Operator authoring (`zicato board add ...`,
`zicato board remove ...`) — only between epochs. Within an epoch the
board is frozen; mutation in-epoch is the easiest way to corrupt a
lineage and is refused by the CLI.

**Produces.** A typed `list[BoardEntry]` for the runner. Three entry
kinds today (`single_turn`, `multi_turn_scripted`, `multi_turn_emulated`)
with an open-ended `kind` discriminator so future kinds (e.g.
`synthetic_adversarial` for target 2) drop in without schema breakage.

The full schema, expectation kinds, wall-clock budget semantics, and
emulator contract are documented in [BOARD-FORMAT.md](BOARD-FORMAT.md)
and [EMULATOR.md](EMULATOR.md).

### 4.3 Runner

**Responsibility.** The per-entry driver. Constructs a fresh
`JSONLPersistenceSink(path=..., mode="write")` for each entry, calls
`adapter.run_entry(entry, sinks=[the_sink, ...])`, awaits the terminal
event, and closes the sink.

**Consumes.** A `Generation` snapshot of the inner-harness source, a
`BoardEntry`, the two configured `call_llm` callables
(`harness_call_llm` and `auxiliary_call_llm` — see §4.10).

**Produces.** A path to the just-written `events.jsonl`. Nothing more —
the runner is deliberately minimal; loss computation happens in a
separate reducer step.

**Contracts.**

- `mode="write"` not `"append"`. The runner allocates a fresh file per
  entry. Appending would silently corrupt run boundaries and the
  reducer relies on each file being exactly one run.
- One JSONL per `(epoch, generation, entry_id)`. Path:
  `.zicato/epochs/{epoch}/generations/v{N}/runs/{entry_id}/events.jsonl`.
- The runner does NOT process events incrementally. It hands the file
  to the reducer once the run terminates.

### 4.4 Telemetry capture (no zicato-specific sink)

**Responsibility.** Persist the inner harness's `goldfive.v1.Event`
stream verbatim. This is goldfive's job; zicato uses goldfive's
`JSONLPersistenceSink` as-is. There is no zicato-specific EventSink
primitive — the JSONL file is the wire-canonical record.

**Why no custom sink.** A `ZicatoSink` would be a thin per-run-path
wrapper over `JSONLPersistenceSink` and would couple zicato to the
goldfive EventSink ABI for no gain. Goldfive's sink does the right
thing (proto-canonical serialization, async-safe writes, lazy file
handle, `replay_from_jsonl` helper). zicato composes it.

Full integration details, including the emulator's per-turn LLM-call
spans on the `zicato:emulator` lane, are in
[TELEMETRY.md](TELEMETRY.md).

### 4.5 Loss reducer

**Responsibility.** Read the per-entry `events.jsonl` after the run
terminates, walk every event, and produce a typed `LossProfile` written
to `loss.json` next to it.

**Consumes.** One `events.jsonl` (proto-canonical, via
`replay_from_jsonl`). Optionally the `BoardEntry`'s `expectation` —
when present, the reducer also runs the expectation against the
appropriate slice of the run result and stamps `pass_fail: bool` on the
profile.

**Produces.** `LossProfile` (Python dataclass, JSON-serializable):

| Field | Type | Source |
|---|---|---|
| `drift_counts_by_kind` | `dict[str, int]` | count `DriftDetected` payloads bucketed by `kind` |
| `drift_counts_by_severity` | `dict[str, int]` | same, bucketed by `severity` |
| `escalations` | `int` | count `DriftDetected` payloads with `lifecycle == ESCALATING` |
| `plan_revisions` | `int` | count `PlanRevised` payloads |
| `task_failure_ratio` | `float` | `TaskFailed` count / `TaskStarted` count |
| `human_intervention_required` | `bool` | any `DRIFT_KIND_HUMAN_INTERVENTION_REQUIRED` emit |
| `runtime_ms` | `int` | terminal event's `emitted_at` minus `RunStarted.started_at` |
| `aborted` | `bool` | terminal event is `RunAborted` |
| `drift_loss` | `float` | weighted scalar (see [SCORING.md](SCORING.md)) |
| `pass_fail` | `bool \| None` | expectation result, or `None` when no expectation |

The reducer runs **once per run** with full visibility. Sinks must
make incremental decisions; reducers don't — they read the whole file,
which is the right shape for derivation work and keeps the loss
computation testable in isolation (feed it a fixture JSONL, assert on
the `LossProfile` out).

See [TELEMETRY.md](TELEMETRY.md) for the full event-to-feature map and
[SCORING.md](SCORING.md) for the loss-scalar formula.

### 4.6 Pattern detectors

**Responsibility.** Aggregate `LossProfile`s across runs within the
current epoch into patterns the proposer can act on. A pattern is a
recurring shape (typed, named) rather than raw counts.

**Consumes.** Every `loss.json` written so far in the current epoch.
The reset happens on epoch boundaries — patterns from epoch A do not
flow into epoch B because the evaluation contract changed (see
[EPOCHS-AND-JOURNALING.md](EPOCHS-AND-JOURNALING.md)).

**Produces.** A typed `list[Pattern]` for the proposer. Each pattern
carries:

- `id` — stable identifier within the epoch
- `kind` — symbolic name (`drift_concentration_by_kind`,
  `tag_slice_regression`, `multi_turn_memory_failure`, etc.)
- `evidence` — pointers to the contributing runs (epoch + generation +
  entry id), structured so the proposer can include exact citations in
  its hypothesis
- `summary` — one-paragraph human-readable rendering
- `tags` — the operator tags from the contributing board entries

Pattern kinds intentionally lift goldfive's drift taxonomy as features
(rather than redefining typed failure shapes). See
[RATIONALE.md](RATIONALE.md) for why.

### 4.7 Patch proposer

**Responsibility.** Read patterns and the operator-edited rubric for
the current epoch, then propose an `Experiment` — a hypothesis plus the
patches that test it.

**Consumes.**

- `list[Pattern]` from §4.6.
- `.zicato/epochs/{epoch}/rubric.md` — the operator's steering
  document. Read fresh every round; no caching. Contains preferred
  targets, forbidden edits (enforced mechanically), style guidance.
- `adapter.mutation_points()` — the full mutation surface. The
  proposer addresses patches by mutation-point id.
- The `auxiliary_call_llm` (distinct by identity or model from
  `harness_call_llm` — see §4.10).

**Produces.** An `Experiment`:

```json
{
  "hypothesis": {
    "core_idea": "<one sentence>",
    "modulating": ["mp_id_1", "mp_id_2", ...],
    "why": "<the pattern observation>",
    "expected_drift_movements": [
      {"kind": "CONFABULATION_RISK", "direction": "down", "magnitude": "moderate"},
      ...
    ],
    "expected_pass_rate_delta": {"low": 0.0, "high": 0.1},
    "risks": ["...", "..."]
  },
  "patches": [
    {"mutation_point_id": "mp_id_1", "new_text": "..."},
    ...
  ]
}
```

The hypothesis fields are **mandatory and structured**. Schema-invalid
proposer responses are rejected and the proposer is re-prompted.
Writing the hypothesis BEFORE the run is the load-bearing decision
that makes the journal interpretable later — without it, every entry
in the journal reduces to "something changed; here's the score
delta".

See [EPOCHS-AND-JOURNALING.md](EPOCHS-AND-JOURNALING.md) for the full
`Experiment` schema and [RATIONALE.md](RATIONALE.md) for why the
hypothesis is mandatory.

### 4.8 Applier

**Responsibility.** Take an `Experiment` and produce a candidate
`Generation` snapshot on disk. Resolve every `mutation_point_id` to its
source location, rewrite the span or file, and validate the result
before publishing the snapshot.

**Consumes.** A parent `Generation`'s snapshot, the `Experiment`'s
patches, the adapter's `mutation_points()` for ID resolution.

**Produces.** A new snapshot under
`.zicato/epochs/{epoch}/generations/v{N+1}/snapshot/` plus
`patches_applied.json` recording exactly what changed.

**Validator constraints (every patch must pass all):**

- The patched file still parses as valid Python (`ast.parse`).
- Every imported name in the patched file resolves (no new
  `NameError` on import).
- The targeted mutation-point id resolves to exactly one location
  after the patch (so future generations can re-find it).
- For prompt templates, all required `{...}` placeholders that the
  pre-patch text contained are preserved in the post-patch text.
- The patch does NOT touch any mutation-point id that appears in the
  rubric's `forbidden:` section.

Patches that fail validation are rejected; the proposer is informed
and may re-propose, subject to the round's wall-clock budget for the
proposal step.

### 4.9 Tournament

**Responsibility.** Compare the candidate generation against its parent
on the whole board, produce a generation score for each, and apply the
promotion gate.

**Consumes.** Parent generation `vN` and candidate `vN+1`. The
frozen-for-this-epoch `board.jsonl`. The scoring weights from
`scoring.json`.

**Produces.** Per-entry comparison rows, a generation score for each
side, and a `tournament_decision` (`promote` | `reject`) with a
human-readable reason.

The default mode (rigorous tournament) re-runs the entire board against
both generations. The fast mode (`zicato evolve --mode fast`) skips the
A/B re-run and uses the candidate's score against the parent's
historical score on the same board — less rigorous but much faster.
Default is rigorous.

The full scalar, weights, and gate are in [SCORING.md](SCORING.md).

### 4.10 The two `call_llm` callables

zicato is configured with **two** distinct `call_llm` callables:

- `harness_call_llm` — used by the inner harness only (passed through
  `goldfive.wrap`'s `call_llm=` parameter; reaches the agent code).
- `auxiliary_call_llm` — used by everything zicato itself runs:
  the patch proposer, the analysis pass, the multi-turn user emulator,
  any judge-shaped expectation, and the rubric-extraction step if
  enabled.

**Hard rule at config time.** The two callables MUST differ by
*callable identity* OR by an explicit `model=` override. If they don't
differ, zicato refuses to start the run. This is a HARD ERROR, not a
warning.

The rule is enforced not because vendor diversity is the goal — it is
because *collusion is the risk*. If the same model is judging itself
and emulating users for itself and proposing patches for itself, every
loop in zicato becomes degenerate at a different level. See
[EMULATOR.md](EMULATOR.md) §"Collusion-proof by construction" for the
full argument and [RATIONALE.md](RATIONALE.md) for why this is
*configured*, not *defaulted*.

### 4.11 Analytical index

**Responsibility.** Make cross-run questions fast. The
filesystem layout (§5) is canonical and human-legible but poor
for `GROUP BY` / `JOIN` queries that range across many
generations. The analytical index is a derived, fully-rebuildable
SQLite sidecar — `.zicato/index.db` — that projects the canonical
artifacts into a relational schema.

**Consumes.** Every `gen_score.json`, `experiment.json`,
`patches/*.json`, `runs/*/loss.json`, and `lineage.json` in the
workspace.

**Produces.** Eight tables (`epochs`, `generations`,
`experiments`, `patches`, `runs`, `loss_profiles`,
`metric_counts`, `tournaments`) plus a `hypothesis_movements`
table. Cross-run views — the dashboard's tournament analytics,
loop-health detectors, the lineage queries — read the index
instead of walking files.

**Contracts.**

- **Files are canonical; the index is derived.** The index holds
  no fact not also on disk. `zicato reindex` reconstructs it
  exactly from the filesystem; it is disposable.
- **Canonical-file-first dual-write.** The orchestrator writes
  the canonical file, then the index row. The index can only
  ever lag the filesystem, never lead it — so a crash leaves a
  self-healing behind-index, never a phantom row.
- **Single writer.** Only the orchestrator writes the index; the
  Rust supervisor opens it read-only via `rusqlite`.

The full schema, the rebuild semantics, and the
SQLite-here-not-there boundary are in
[ANALYTICAL-INDEX.md](ANALYTICAL-INDEX.md).

### 4.12 Loop-health diagnostics

**Responsibility.** Detect when the meta-loop is *running but not
optimising anything*. The robustness layers (§5) keep the loop
from *breaking*; loop-health keeps it from being *toothless* — a
degenerate evaluation that cannot distinguish any candidate. The
motivating incident: a real run had `v0` and `v1` both score
exactly `1.000000`, and the degeneracy was found only by an
operator eyeballing the journal.

**Consumes.** Each round's runs, scores, and the epoch-so-far
history.

**Produces.** A typed `LoopHealth` report per round, written to
`epochs/{epoch}/loop_health/round_{NNN}.json`. Five detectors
(degenerate scoring, non-differentiating board entries, flat
drift signal, no-expectations, stalled loop) emit findings with
`info` / `warning` / `critical` severities.

**Contracts.**

- A `critical` finding triggers a bannered orchestrator warning
  and an SSE event to the dashboard's loop-health panel — the §1
  silent-degeneracy failure mode never again depends on an
  operator noticing.
- `zicato evolve --stop-on-degenerate` opts into an early-stop
  on sustained degeneracy. Opt-in, not default.

The detectors, severities, and the `zicato health` CLI are in
[LOOP-HEALTH.md](LOOP-HEALTH.md).

### 4.13 Journaling and analysis

**Responsibility.** Make every round narratable and every epoch
analyzable, without the operator hand-writing the prose.

**Per-round:**

- `experiment.json` is augmented with an `outcome` block immediately
  after the tournament decision: actual drift movements, per-movement
  match against the hypothesis, score delta, decision, rejection
  reason if any.
- `journal.md` appends a short paragraph: `core_idea`,
  `drift_loss_delta`, `pass_rate_delta`, `tournament_decision`.

**Per-epoch close:**

- `analysis.md` is generated by an `auxiliary_call_llm` pass over the
  full journal: headline movements, which hypotheses held, which did
  not, what surface is still open, recommended focus for the next
  epoch.

Closing is **manual primary** (`zicato epoch close`) with **auto-close
on `epoch new`** as a fallback that emits a warning so operators
notice they missed the manual step. See
[EPOCHS-AND-JOURNALING.md](EPOCHS-AND-JOURNALING.md).

### 4.14 The CLI

zicato is operated through a single CLI binary. Every component above
has at least one CLI subcommand that surfaces it. The full reference is
in [CLI.md](CLI.md); a one-line tour:

| Subcommand | What it does |
|---|---|
| `zicato init` | Create a `.zicato/` workspace with an `initial` epoch. |
| `zicato register --adk path:agent --mutable-tree <path>` | Register an inner harness via an adapter. |
| `zicato board add/list/remove` | Edit the current epoch's board (refused mid-epoch unless `--force`). |
| `zicato mutations` | Audit the current mutation surface — every span, every file marker. |
| `zicato run --generation vN --entry <id>` | Run one entry against one generation. |
| `zicato analyze` | Aggregate loss profiles into patterns. |
| `zicato propose` | Run the proposer; emit `Experiment`. |
| `zicato patch apply` | Apply an experiment's patches to a new candidate snapshot. |
| `zicato tournament vN vN+1` | Run the tournament between two generations. |
| `zicato epoch new/close/list/switch` | Manage epochs. |
| `zicato evolve [--rounds N] [--mode fast\|tournament]` | The orchestrator: one command, many rounds. |
| `zicato reindex` | Rebuild the `.zicato/index.db` analytical index from the filesystem. |
| `zicato health` | Run loop-health diagnostics on the current epoch. |
| `zicato journal show` | Render `journal.md`. |
| `zicato analysis show` | Render `analysis.md` for a closed epoch. |

## 5. Runtime and observability layer

The components above describe the meta-loop's logical structure.
The runtime layer is the surrounding scaffold that makes the loop
**survivable** (hangs, crashes, OOMs, the long tail of pathology)
and **observable** (a live operator view of in-flight rounds, a
durable audit trail of override decisions).

The runtime layer's substrate is the file tree under
`.zicato/runtime/` — a small set of single-writer state files
that capture every important runtime fact on disk. No important
state lives only in process memory. A crashed orchestrator
restarts and resumes from the durable record; a crashed
supervisor restarts and rebuilds its in-memory view from the
files.

```
   ┌────────────────────────────────┐         ┌────────────────────────────┐
   │  zicato evolve (Python)        │         │  zicato-supervisor (Rust)  │
   │  ───────────────────────────   │  spawn  │  ────────────────────────  │
   │  • acquires .zicato/runtime/   ├────────►│  Watchdog + dashboard      │
   │    lock.json                   │         │  server in one binary.     │
   │  • writes heartbeat.json (2s)  │         │                            │
   │  • spawns subprocess workers   │         │  inotify on runtime/*      │
   │    for each tournament run     │         │                            │
   │  • reads control/ at safe      │         │  Heartbeat-stale → flag    │
   │    points (between rounds)     │         │  the orchestrator stalled  │
   └────────────────────────────────┘         │                            │
                  │                           │  Worker stale → SIGTERM    │
                  │ Popen                     │  → grace → SIGKILL         │
                  ▼                           │                            │
   ┌────────────────────────────────┐         │  Serves HTTP + SSE on      │
   │  worker (Python subprocess)    │         │  :7892 (default). Renders  │
   │  ───────────────────────────   │  reads  │  live dashboard from state │
   │  • writes active_runs/{id}.json├────────►│  file changes.             │
   │  • bumps phase + heartbeat_at  │         │                            │
   │    every 1s                    │         │  v1.3: accepts POST writes │
   │  • runs adapter.run_entry      │         │  to control/ files for     │
   │  • dies on SIGTERM cleanly,    │         │  operator actions.         │
   │    SIGKILL if it doesn't       │         └────────────────────────────┘
   └────────────────────────────────┘
```

Three properties hold across the runtime layer:

1. **File-based state is the only source of truth.** Memory
   state in either process is a cache of what's on disk.
2. **No LLM in the watchdog path.** Watchdog decisions are
   deterministic functions of file timestamps.
3. **Single-writer per file.** Each state file has exactly one
   process that writes to it (orchestrator, supervisor, or one
   specific worker). No locking beyond `lock.json`.

The full design lives in seven documents:

| Concern | Document |
|---|---|
| State file layout, supervisor lifecycle, resume semantics, concurrency model | [RUNTIME.md](RUNTIME.md) |
| Live dashboard panels, HTTP + SSE API, predicted gate verdict, control-file protocol for v1.3 interactivity | [DASHBOARD.md](DASHBOARD.md) |
| The tournament competition model — the king-of-the-hill gauntlet, the bracket view, the per-matchup detail, the tournament analytics | [TOURNAMENT.md](TOURNAMENT.md) |
| The six-layer defense model (`asyncio.wait_for` → cancellation → subprocess workers → watchdog → circuit breaker → atomic writes) and what each catches | [ROBUSTNESS.md](ROBUSTNESS.md) |
| Loop-health diagnostics — detectors for a degenerate / toothless evaluation, the `LoopHealth` report, `zicato health` | [LOOP-HEALTH.md](LOOP-HEALTH.md) |
| v0 directory-backed storage today, plus the v0+1 git-backed roadmap (G0-G10) for blob dedup + `git log` / `git diff` / `git bisect` over generations | [STORAGE.md](STORAGE.md) |
| The `.zicato/index.db` SQLite analytical index — schema, the files-canonical / index-derived discipline, `zicato reindex` | [ANALYTICAL-INDEX.md](ANALYTICAL-INDEX.md) |

The runtime layer ships in phases. v1 has L1+L2 from
[ROBUSTNESS.md](ROBUSTNESS.md) — `asyncio.wait_for` per call
and structured cancellation — sufficient for cooperative inner
harnesses. v1.1 is the production-readiness pass: subprocess
workers, the Rust supervisor's watchdog role, atomic writes
everywhere, the resume protocol. v1.2 adds the dashboard's
read-only mode; v1.3 adds the interactive controls. The git
storage backend (v0+1) lands after v1.3 in the sequencing that
[STORAGE.md](STORAGE.md) §4 lays out.

## 6. Storage layout

zicato keeps everything under a per-project workspace, by default
`.zicato/` next to the inner harness's source root. Multi-instance
deployments (target 3 — nested zicato instances) key the workspace by
`instance_id` configured at runtime so workspaces never cross-talk.

```
.zicato/
  config.json                      # registered adapter, call_llm wiring, instance_id
  epochs/
    {epoch_id}/
      board.jsonl                  # frozen for this epoch
      rubric.md                    # operator-edited; read fresh each round
      scoring.json                 # weights + tournament thresholds
      generations/
        v0/
          snapshot/                # inner-harness source at this generation
          experiment.json          # absent for v0 (the baseline)
          runs/
            {entry_id}/
              events.jsonl         # goldfive wire, via JSONLPersistenceSink
              loss.json            # post-run reducer output
          gen_score.json
        v1/
          snapshot/
          experiment.json          # hypothesis + patch_ids + outcome
          patches/
            {patch_id}.json        # one file per patch
          runs/{entry_id}/
            events.jsonl
            loss.json
          gen_score.json
        ...
      current_generation           # marker: id of the promoted head
      patterns/
        round_{NNN}.json           # detector output, one per round
      loop_health/
        round_{NNN}.json           # loop-health report, one per round
      journal.md                   # running narrative across generations
      analysis.md                  # generated at epoch close
  index.db                         # derived SQLite analytical index (rebuildable)
  lineage.json                     # cross-epoch generation DAG
```

`experiment.json` carries `patch_ids: [...]` and each patch lives in
its own `patches/{patch_id}.json` file. Writes go patches-first,
`experiment.json` last; the read helper transparently accepts the
older inline `patches: [...]` form for backward compatibility. See
[EPOCHS-AND-JOURNALING.md](EPOCHS-AND-JOURNALING.md) §3.2 for the
write-order rationale.

Every **canonical** artifact is a human-readable file — JSON, JSONL,
or markdown. The cost of `ls`-and-`cat` debugging is the budget for
the storage design; the operator's first-class interface is the
filesystem.

The one non-text file is `.zicato/index.db` — the **analytical
index**. It is *not* canonical: it is a derived, fully-rebuildable
SQLite projection of the files above, a cache that makes cross-run
`GROUP BY` / `JOIN` queries fast without a file-walk. It holds no
fact not also on disk; `zicato reindex` reconstructs it exactly. The
filesystem stays the source of truth; the index is a sidecar. See
[ANALYTICAL-INDEX.md](ANALYTICAL-INDEX.md).

## 7. The harmonograf split: execution view vs competition view

zicato and harmonograf both render a "view of a run", and the
boundary between them is load-bearing — they are deliberately
two tools, linked, not one merged UI.

> **harmonograf is the execution view; the zicato dashboard is
> the competition view. They are linked by a per-run drill-down,
> not merged.**

- **harmonograf — the execution view.** It renders *one
  goldfive run*: the temporal trace of a single execution — the
  plan unfolding, per-turn drift, the intervention ladder, the
  operator's live steering. It answers "what happened, moment by
  moment, in *this run*?".
- **the zicato dashboard — the competition view.** It renders
  *one zicato epoch*: many runs across many generations — the
  tournament bracket, the per-matchup A/B grid, the gate
  verdict, the score trajectory, the hypothesis ledger. It
  answers "which generation is winning, and why?".

These are different objects. A run is a *trace*; a tournament is
a *comparison of aggregates over many traces*. One is not a
zoomed-in version of the other, so they are not merged — a
single tool good at both a millisecond-resolution timeline and
an epoch-resolution bracket would be mediocre at both.

They are *linked* by a **per-run drill-down**. Anywhere the
zicato dashboard shows an individual run — a cell in the A/B
grid, a row in the active-runs list — there is an "open in
harmonograf" affordance that hands off to harmonograf pointed at
that run's `events.jsonl`. The operator moves *down* the
competition view (epoch → round → matchup → run) and at the run
level steps *across* into the execution view.

This split is the observability face of the cadence separation
in §3: goldfive acts within a run, harmonograf observes within a
run, zicato acts across runs. harmonograf and the zicato
dashboard are the within-a-run and across-runs observability
surfaces respectively. The full treatment is in
[TOURNAMENT.md §5](TOURNAMENT.md#5-the-harmonograf-split).

## 8. The data flow, in narrow contracts

The diagram in §2 names the components; this section names the
contracts between them so two components can be reimplemented without
breaking the third.

| Producer | Consumer | Contract |
|---|---|---|
| `HarnessAdapter` | `Runner` | `run_entry(entry, sinks=[...])` emits goldfive events to the supplied sinks; terminates with `RunCompleted` or `RunAborted`. |
| `Runner` | `Loss reducer` | A path to one `events.jsonl` that is a complete run (one `RunStarted`, one terminal event). |
| `Loss reducer` | `Pattern detectors` | One `LossProfile` JSON per run, schema in §4.5. |
| `Pattern detectors` | `Patch proposer` | A `list[Pattern]` reset on epoch boundaries. |
| `Patch proposer` | `Applier` | An `Experiment` (schema in §4.7) with patches addressing valid mutation-point ids. |
| `Applier` | `Tournament` | A candidate `Generation` snapshot that passes all validator constraints in §4.8. |
| `Tournament` | `Journal + outcome` | A `tournament_decision` with score deltas. |

A reader can replace any single component with their own implementation
as long as the contracts hold. This is what "framework-agnostic" means
in zicato — not "any framework works on day one" but "every contract is
between named, typed shapes, so a new framework adapter is the only
thing that needs to change to adopt zicato for it".

## 9. What's deliberately out of scope for v0

- Cross-machine distribution. v0 runs locally; nothing in the design
  prevents distributed runners but the v0 storage layout is a single
  filesystem.
- Caching of LLM calls. Caching is a wrapper concern on the
  `call_llm` callable; zicato makes no assumptions either way.
- Per-turn intervention. zicato never acts inside a run; that is
  goldfive's domain.
- A web UI. The CLI is the surface; harmonograf exists for the live
  run view.
- Multi-tenant workspaces. v0 has one workspace per project,
  optionally keyed by `instance_id` for nested zicato (target 3).
- Anonymisation or PII scrubbing on the event stream. The board is
  whatever the operator put on it; the JSONL captures exactly what
  flowed.

## 10. Further reading

| Topic | Document |
|---|---|
| Annotated mutation points, AST resolution, audit CLI | [MUTATION-SURFACE.md](MUTATION-SURFACE.md) |
| Board entry schema, expectation kinds, multi-turn emulator | [BOARD-FORMAT.md](BOARD-FORMAT.md) |
| Epoch concept, experiment journaling, analysis pass | [EPOCHS-AND-JOURNALING.md](EPOCHS-AND-JOURNALING.md) |
| goldfive event capture, loss reducer, emulator audit lane | [TELEMETRY.md](TELEMETRY.md) |
| Drift loss scalar, pass-rate, tournament promotion gate | [SCORING.md](SCORING.md) |
| The tournament competition model — gauntlet, bracket, per-matchup detail, analytics | [TOURNAMENT.md](TOURNAMENT.md) |
| User emulator design + collusion-proof construction | [EMULATOR.md](EMULATOR.md) |
| Three dogfood targets and the v0 design they force | [DOGFOOD-TARGETS.md](DOGFOOD-TARGETS.md) |
| `.zicato/runtime/` state files, the Rust supervisor binary, resume protocol | [RUNTIME.md](RUNTIME.md) |
| Live dashboard panels, HTTP + SSE, predicted gate verdict, control-file protocol | [DASHBOARD.md](DASHBOARD.md) |
| The six-layer defense model against hangs and crashes | [ROBUSTNESS.md](ROBUSTNESS.md) |
| Loop-health diagnostics — detectors for a degenerate evaluation, `zicato health` | [LOOP-HEALTH.md](LOOP-HEALTH.md) |
| Git-backed storage roadmap (G0-G10) + migration tooling | [STORAGE.md](STORAGE.md) |
| The `.zicato/index.db` SQLite analytical index — schema, discipline, `zicato reindex` | [ANALYTICAL-INDEX.md](ANALYTICAL-INDEX.md) |
| CLI reference, every subcommand | [CLI.md](CLI.md) |
| Why each major decision was made the way it was | [RATIONALE.md](RATIONALE.md) |
| Glossary | [VOCABULARY.md](VOCABULARY.md) |
