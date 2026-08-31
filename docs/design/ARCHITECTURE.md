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

zicato is a **meta-harness** for any system whose behaviour you can
measure. It takes a system you have already built, declares part of its
source tree mutable, and turns it into the *inner harness* of a learning
loop.

Multi-agent systems are the founding and primary use case — a coordinator
with specialists, a deep `sub_agents` tree, a single planning agent, an
arbitrary callable that fronts a model — and the only concrete adapter
shipped so far targets Google ADK. But the definition is not agent-shaped.
The loop requires:

* an **entrypoint** the runner can drive to produce an output;
* one or more **mutable trees** — source roots the proposer may edit,
  which need not contain the entrypoint;
* a **board** of tasks carrying typed expectations, so each run yields a
  score.

Any system that fits that shape can be evolved. The mutation surface is
declared with in-source comment markers rather than derived from agent
structure, so nothing in the patch path inspects agent classes or role
graphs: the enforced adapter contract is
`("mutable_subpaths", "load", "mutation_points")`, and a `RunResult`
carries strings. The markers are not Python-only either: a marker may sit
in any allowlisted text file, so a markdown prompt or a YAML policy is
first-class mutable surface (see
[MUTATION-SURFACE.md](MUTATION-SURFACE.md) §2.4). The entrypoint is still
Python, because the runner drives it.

The in-tree proof is `examples/zicato_examples/target_0_convergence`: a
`DeterministicPolicyAdapter`, registered through `adapter.kind = "import"`,
whose mutable surface is a module-level string constant and whose
docstring notes there is **no LLM anywhere**. The full loop — propose,
apply, worker, reduce, gate — runs against it under continuous
integration. The other end of the range is goldfive itself: a library,
mutated with the entrypoint outside every mutable tree (see
[DOGFOOD-TARGETS.md](DOGFOOD-TARGETS.md)).

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
becomes the parent of the next round. Generations form a directed
acyclic graph. **Epochs** group generations under a stable evaluation
contract — the board, the proposer brief, and the scoring weights all
hold steady — so
generations within an epoch are directly comparable. Cross-epoch
comparison is approximate by design, because the two epochs score
generations under different contracts.

### How much of this requires goldfive

goldfive is an **optional extra** (`zicato[goldfive]`) rather than a
hard install dependency. The core import surface — `import zicato`,
`import zicato.core`, the board reader/writer and builder, the epoch
contract, the storage and query layers, and every `zicato --help` —
resolves without it. What makes
that true is `src/zicato/core/drift_kinds.py`: it mirrors goldfive's
`DriftKind` / `DriftSeverity` as `enum.StrEnum` classes with the same member
names, values, and declaration order, so the board types keep the full
vocabulary (unknown tokens still raise, with the same message) without
importing upstream. Mirror members and goldfive members compare equal and
serialise to the same token, so boards authored against either symbol behave
identically. A goldfive-gated test pins the two enums member-for-member, and
`tests/test_no_goldfive_import.py` runs the import surface in a child
interpreter whose `sys.meta_path` refuses `goldfive`, so neither half of the
property rots.

The extra buys three things:

* the ADK adapter path, where `zicato.adapters.adk` wraps the inner
  harness in `goldfive.wrap`;
* the in-run process judges — a board's `judges` are handed to goldfive
  as additional judges, and without the extra they are inert;
* the default `goldfive` telemetry dialect, the only dialect that yields
  drift kinds and plan revisions.

The live telemetry client and server are part of the
`observability` profile, so a base install does not resolve their transitive
graph. The import surface and package metadata both make the dependency
boundary real.

Installation profiles and their capability guarantees are specified in
[`INSTALL-PROFILES.md`](INSTALL-PROFILES.md).

What that does *not* imply is that your target must be a goldfive
application. The adapter protocol is framework-neutral — it
asks for `load`, `mutable_subpaths`, and `mutation_points`, plus
`run(entry, sinks, config) -> RunResult` on the loaded harness — and a
workspace declares a non-ADK harness through `adapter.kind = "import"`.
A target that emits no drift events is scored on its predicates, rubrics,
and any other metric namespaces it reports. The loss surface accepts
arbitrary namespaced metrics (`drift:*`, `cost:*`, `latency:*`). Drift is
one input to the scalar rather than a precondition for having one.

One setting decouples a target from goldfive: `scoring.json`'s
`telemetry_dialect`. The default, `goldfive`, is the only
dialect that produces drift kinds and plan revisions; `adk_events` and
`transcript` are tolerant readers for a harness that does not run under the
drift-instrumented ecosystem harness at all. Under `transcript` the drift
term is structurally zero, the drift knobs go inert (zicato warns rather
than failing), and scoring degrades to predicates plus optional in-run
judges. See [TELEMETRY-DIALECTS.md](TELEMETRY-DIALECTS.md).

Two board-level headers are often mistaken for that setting, and neither
has that effect.
`disable_drift` names drift kinds whose **mapped built-in judges** are
suppressed on the ADK path; naming a kind with no mapped judge does
nothing. Every other channel survives untouched: plan revisions in
`drift:`, custom judges in `judge:`, task failures and aborts in
`failure:`, and wall-clock in `runtime:`. There is no "all drift off"
board mode; setting `namespace_weights["drift:"]` to `0.0` is what
removes the drift channel from the scalar, and it removes only that
channel. `judge_only` is orthogonal again: it keeps judges armed while
disabling steering entirely (no goal-derivation call, no replanning, no
drift-triggered refine). See
[BOARD-FORMAT.md](BOARD-FORMAT.md).

### Why this is a separate library

The orchestration scaffolding that makes drift legible — goals, plans,
per-turn drift analysis, the intervention ladder — already exists. It
is goldfive. The observability + steering console that makes drift
*visible to operators* already exists. It is harmonograf. zicato is
the third layer: it consumes that same telemetry **across runs** and
acts on it by rewriting the harness itself. Neither sibling reaches
across runs.

The three libraries have non-overlapping cadences:

| Library | Cadence | Acts on |
|---|---|---|
| goldfive | within one run | the live plan (refine on drift, intervene at Level 0-5) |
| harmonograf | within one run | the operator's view (steer, pause, cancel; annotate) |
| **zicato** | **across generations** | **the inner harness's source** (rewrite annotated spans, run tournaments) |

Keeping zicato a separate library keeps the cadence clean. goldfive
must never reach across runs; harmonograf must never reach into the
inner harness's source. zicato is the only thing that does either.

### What zicato is *not*

- Not a planner. It does not propose plans; it proposes patches to the
  things that produce plans (the planner's prompt, the coordinator's
  routing instructions, specialist system prompts).
- Not an LLM client. It calls models only through a caller-supplied
  `call_llm(system: str, user: str, model: str) -> str` callable. The
  core never imports a vendor SDK.
- Not framework-coupled. The inner harness can be anything that exposes
  a `HarnessAdapter`. Google ADK is the only adapter implemented in the
  tree (`zicato/adapters/adk.py`); any other harness is registered
  through the generic `adapter.kind = "import"` shape, which resolves a
  dotted path the operator supplies (`zicato/adapter_factory.py`).
- Not a runtime steerer. Live runs go through goldfive (and
  harmonograf, if the operator wants the console). zicato only acts
  between runs.
- Not a single-file editor. The mutation surface is annotated and
  granular — a string span, a bracketed region, or at most a whole file
  marked mutable. zicato never rewrites the inner harness's tree at
  large, and an unmarked file is immutable whatever its type.

## 2. The meta-loop, end to end

```
                                                ┌────────────────────────────┐
                                                │  watchdog supervisor (Rust)│
                                                │  + dashboard service (Py)  │
                                                │  ────────────────────────  │
                                                │  both spawned by `evolve`. │
                                                │  Watchdog (Rust, no-dash): │
                                                │  reads heartbeat.json,     │
                                                │  active_runs/*; escalates  │
                                                │  SIGTERM → SIGKILL; serves │
                                                │  /statusz.                 │
                                                │  Dashboard (Python/        │
                                                │  Starlette): HTTP + SSE on │
                                                │  :7892, reads runtime/ +   │
                                                │  index.db + epochs/.       │
                                                └────────────┬───────────────┘
                                                             │ inotify; signals
                                                             │
   ┌─────────────────────────────────────────────────────────┼───────────────┐
   │                       zicato meta-loop                  │               │
   │                       (orchestrator)                    │               │
   │   ┌──────────────┐    ┌───────────────────────────────┐ │               │
   │   │  Board       │    │  Inner harness (HarnessAdapter)│ │               │
   │   │ (.jsonl,     │    │                                │ │               │
   │   │  frozen      │    │   any system under test        │ │               │
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
   │              │  Patch proposer    │  reads patterns + brief     │
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

The inner box is a single **round**. A round advances one generation
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
| Cross-run? | no | no (per-session views aside) | yes; aggregating across runs is its purpose |
| Loss model | drift detected this turn | n/a | reduced from events; aggregated across runs in an epoch |
| Owns | `Plan` / `Task` / `Drift*` state machines | UI + control channel | `MutationPoint` / `Experiment` / `Generation` / `Epoch` |
| Mutates source? | no | no | **yes** — annotated spans only |

The cadence separation is also a safety boundary. goldfive must never
edit the harness's source; harmonograf must never act between runs;
zicato must never act inside a run. Violating any of those would let
two systems race on the same surface with incompatible models of what
"now" means.

## 4. Component-by-component

Each component below carries its responsibility, what it consumes, what
it produces, and its contracts. The full schemas live in the
topic-specific documents that each section links.

### 4.1 HarnessAdapter

**Responsibility.** The narrow protocol that decouples zicato from any
specific harness framework. The adapter implementer is the
inner-harness author; zicato treats the adapter as the only handle on
the system under test.

**Consumes.** A registration (`zicato epoch register --adk path:agent
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
  source root and returns every annotated mutation point (span, region,
  and file markers, in `.py` and in any allowlisted text file; see
  [MUTATION-SURFACE.md](MUTATION-SURFACE.md)).
  The return value is a list because a target whose sources span several
  repositories needs multiple roots walked — evolving goldfive's own
  steering layer is the worked example (see
  [DOGFOOD-TARGETS.md](DOGFOOD-TARGETS.md)) — even though a
  single-repository target uses one root.

**Contracts.**

- The adapter MUST emit a `goldfive.v1.RunStarted` and a single
  terminal event (`RunCompleted` or `RunAborted`) per entry. The
  zicato runner relies on this to bound the JSONL file per entry.
- The adapter MUST exhaust the entry's `wall_clock_budget_seconds` on
  itself — if the inner harness runs over budget, the adapter aborts
  the inner work and emits `RunAborted(reason="wall_clock_budget")`.
- The adapter MAY attach additional sinks for its own use (logging,
  in-process accumulators) but MUST NOT modify the sinks zicato
  supplied.

#### 4.1.1 `on_promote` — the post-promotion hook

**Optional.** One more coroutine an adapter MAY declare:

```python
async def on_promote(
    self, *,
    epoch_id: str,
    generation_id: str,
    parent_generation_id: str | None,
    snapshot_root: Path,
    workspace_root: Path,
) -> None: ...
```

**Why it exists.** For most targets the evolved artifact IS the
snapshot: the promoted tree plus the `current_generation` marker is the
whole story, and there is nothing further to do. A target whose real
state lives somewhere the mutable tree cannot reach — a database row, a
served artifact, a cache, a remote config — has no such closure. Without
the hook, such a target has to poll `lineage.json` from outside the loop
and reconcile the promoted head itself.

**When it fires.** Exactly once per settled promotion, immediately after
the champion marker advances — the first moment the promotion is
durable. Both promote seams fire it through the one helper
(`zicato.evolve.promote_hook.fire_on_promote`): the gauntlet's
`_finalize_generation(advance_current_generation=True)` tail and the
multi-challenger inline crowning. A rejected round never fires it. Under
a multi-challenger structure with an operator multi-promote it fires for
the PRIMARY head only — the generation `current_generation` advanced
to — not for every generation lineage marks `promoted`.

It fires on the *transition*, never on observing promoted state, so it
cannot repeat: a crash-restart re-enters only an **un-outcomed**
generation (`prepare_resume`, RUNTIME.md §4.2) and a promoted generation
always carries a committed outcome. The converse window — a crash
between the marker advance and the hook — loses the call rather than
repeating it, which is the chosen direction given the failure
semantics below.

**Failure semantics: best-effort.** A hook that raises, or that exceeds
`ON_PROMOTE_TIMEOUT_SECONDS` (120s), NEVER un-promotes the generation
and never fails the round. The promotion is already durable on every
store by the time the hook runs, so reporting the failure is all that a
failure can honestly do. It is reported twice: an `ERROR` log carrying
the traceback, and an `on_promote_hook_failed` WARNING in the round's
loop-health report naming the adapter, the generation, and the exception
type. Reconciling the external side effect is then the operator's job.
An adapter that needs promotion to be all-or-nothing must make its own
side effect idempotent and reconcile from `lineage.json`.

**Optionality.** An adapter that declares no `on_promote` is still a
`HarnessAdapter`: the member is listed in `OPTIONAL_ADAPTER_MEMBERS`, and
the Protocol's `__subclasshook__` keeps the runtime `isinstance` gate
keyed on the three required methods. Such an adapter is never called.

**Trust model.** The hook runs operator-authored adapter code in the
evolve process — code the operator already registered and which zicato
already imports and executes to run every board entry. It grants no new
authority. Contract-declared shell commands were considered as an
alternative carrier and rejected. A promotion hook spelled as a command
string in a contract file turns the epoch contract into an executable
surface, and the epoch contract is a data file that the loop reads,
writes, and hands to a proposer. That is a different trust boundary,
and it would need its own security review to justify. Non-Python
targets use the polling fallback below rather than a command hook.

**Fallback for targets that cannot host a Python hook.** Poll
`lineage.json` for the promoted head — the last entry with
`promoted: true` — and reconcile against your own record of the last
head you applied. Polling stays supported and correct; the hook removes
the polling latency and the second bookkeeping store. See
[DOGFOOD-TARGETS.md](DOGFOOD-TARGETS.md) §6.

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
with an open-ended `kind` discriminator, so a further kind (for example
`synthetic_adversarial`, wanted for evolving goldfive's steering layer)
drops in without schema breakage.

The full schema — the `expectations` (outcome) and `judges` (process)
facets, wall-clock budget semantics, and the emulator contract — is
documented in [BOARD-FORMAT.md](BOARD-FORMAT.md) and
[EMULATOR.md](EMULATOR.md); the practical authoring guide is
[BOARD-AUTHORING.md](BOARD-AUTHORING.md).

### 4.3 Runner

**Responsibility.** The per-entry driver. Constructs a fresh
`JSONLPersistenceSink(path=..., mode="write")` for each entry, calls
`adapter.run_entry(entry, sinks=[the_sink, ...])`, awaits the terminal
event, and closes the sink.

**Consumes.** A `Generation` snapshot of the inner-harness source, a
`BoardEntry`, the two configured `call_llm` callables
(`harness_call_llm` and `auxiliary_call_llm` — see §4.10).

**Produces.** A path to the just-written `events.jsonl`. Nothing more —
the runner stays minimal, and loss computation happens in a
separate reducer step.

**Contracts.**

- `mode="write"` rather than `"append"`. The runner allocates a fresh
  file per entry. Appending would silently corrupt run boundaries, and
  the reducer relies on each file holding a single run.
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
`replay_from_jsonl`). The `BoardEntry`'s `expectations` list — when
non-empty, the reducer runs every outcome check against the
appropriate slice of the run result and ANDs them into `pass_fail:
bool`. (The entry's `judges` need no separate reducer step: a
violated process judge already emitted a `DriftKind.CUSTOM` drift
into the event stream, which the reducer counts like any other
drift — see §4.6.1.)

**Produces.** `LossProfile` (Python dataclass, JSON-serializable):

| Field | Type | Source |
|---|---|---|
| `drift_counts_by_kind` | `dict[str, int]` | count `DriftDetected` payloads bucketed by `kind` (custom-judge violations land under `DRIFT_KIND_CUSTOM`) |
| `drift_counts_by_judge` | `dict[str, int]` | count `DRIFT_KIND_CUSTOM` payloads bucketed by `judge_name` — the per-judge breakdown |
| `drift_counts_by_severity` | `dict[str, int]` | same, bucketed by `severity` |
| `escalations` | `int` | count `DriftDetected` payloads with `lifecycle == ESCALATING` |
| `plan_revisions` | `int` | count `PlanRevised` payloads |
| `task_failure_ratio` | `float` | `TaskFailed` count / `TaskStarted` count |
| `human_intervention_required` | `bool` | any `DRIFT_KIND_HUMAN_INTERVENTION_REQUIRED` emit |
| `runtime_ms` | `int` | terminal event's `emitted_at` minus `RunStarted.started_at` |
| `aborted` | `bool` | terminal event is `RunAborted` |
| `drift_loss` | `float` | weighted scalar (see [SCORING.md](SCORING.md)) |
| `pass_fail` | `bool \| None` | AND of the entry's `expectations`, or `None` when the list is empty |

The reducer runs **once per run** with full visibility. A sink has to
decide what to record as each event arrives; a reducer reads the whole
file at once. That shape suits derivation work and keeps the loss
computation testable in isolation: feed the reducer a fixture JSONL file
and assert on the `LossProfile` it returns.

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

#### 4.6.1 Pluggable judges: the goldfive integration

A board entry evaluates the inner harness along two facets (see
[BOARD-FORMAT.md](BOARD-FORMAT.md) and
[BOARD-AUTHORING.md](BOARD-AUTHORING.md)):

- **Outcome** check — the entry's single `expectation` (`Predicate` /
  `Rubric`). Run post-hoc by the loss reducer (§4.5) against the run's
  output or transcript. They drive **pass-rate**.
- **Process** checks — the entry's `judges` list (`Judge`). Run
  in-flight by goldfive against the agent's reasoning stream. They
  drive **drift loss**.

The process facet is a **pluggable-judge integration with goldfive**.
goldfive already runs an ambient set of built-in judges — the
detectors behind the standard `DriftKind` taxonomy
(`CONFABULATION_RISK`, `LOOPING_REASONING`, …). zicato extends that
set per board entry without forking goldfive's detector code:

- A `Judge.custom(name, criterion, *, severity=...)` is an **inline,
  natural-language** judge. Its `criterion` describes a *process*
  property goldfive should watch for in the reasoning stream (e.g.
  "the agent must cite a source before stating a metric").
- A `Judge.python(name, dotted_path, *, severity=...)` is a
  **programmatic** judge — the escape hatch for checks too mechanical
  for prose. The callable lives in the project's source.

zicato hands the entry's `judges` to goldfive as additional judges
when it wraps the inner harness for a run; goldfive evaluates them
alongside its built-ins.

goldfive dispatches custom judges at *reasoning* observation points,
with the live `Session` reachable as `ctx.session_state`. A
`Judge.python` body that needs to grade what the agent actually *did*
reads the structured tool-call ledger at `session_state.recent_events`,
whose `tool_observed` entries carry `tool_name`, `args_preview`,
`result_preview`, and `is_error`. It reads that ledger rather than the
agent's narration, which can name a tool failure it never hit or omit
one it did. goldfive does not set `ctx.extras["tool_event"]`; the
ledger is the ground truth.

**The drift-emit path.** When a judge's criterion is violated,
goldfive emits a judgement on the same wire it uses for any drift —
there is no zicato-specific event type. The emit path is:

```
Judge.custom("cite-before-metric", "...")   ← board entry, judges=[...]
        │  handed to goldfive as a custom judge
        ▼
goldfive evaluates the criterion against the live reasoning stream
        │  criterion violated
        ▼
goldfive emits a JudgementEmitted with:
        kind       = DriftKind.CUSTOM      ← every custom judge uses this kind
        judge_name = "cite-before-metric"  ← the Judge's `name`, verbatim
        severity   = the Judge's severity
        ▼
the run's events.jsonl captures it like any other drift event
        ▼
the loss reducer (§4.5) counts it under DRIFT_KIND_CUSTOM and keys
the per-judge breakdown on judge_name
```

Every custom judge — `custom` or `python` — emits `DriftKind.CUSTOM`.
The judge is told apart from other custom judges by its **`name`**,
carried on the event as `judge_name`. So `judge_name` is the
discriminator at every downstream stage: the reducer's per-judge
counts, the journal, and `ScoringWeights.per_judge_weights` (which
keys on `judge_name` — see [SCORING.md](SCORING.md) §2.2) all key on
it. This is why a `Judge`'s `name` must be stable and board-unique.

**Suppressing built-ins.** A board's `disable_drift` setting — a list
of `goldfive.DriftKind` enum values — turns off the named *built-in*
judges for every entry on that board. It suppresses built-ins by
kind; it does not touch custom judges (those are removed by deleting
them from an entry's `judges`). `disable_drift` is part of the
evaluation contract, and it is the *set of named kinds* that is
contract — editing the list to disable a different kind rolls the
epoch, reordering it does not (see
[EPOCHS-AND-JOURNALING.md](EPOCHS-AND-JOURNALING.md) §10).

**No new typology.** This integration follows the decision in
[RATIONALE.md](RATIONALE.md) §5 to lift goldfive's drift taxonomy rather
than invent a zicato one. A custom judge adds no new `DriftKind`: it
rides the existing extensible `CUSTOM` kind, and the `judge_name` field
carries the operator's discriminator. zicato adds *judges* and leaves
the *drift kinds* alone.

### 4.7 Patch proposer

**Responsibility.** Read patterns and the operator-edited proposer
brief for the current epoch, then propose an `Experiment` — a
hypothesis plus the patches that test it.

**Consumes.**

- `list[Pattern]` from §4.6.
- `.zicato/epochs/{epoch}/brief.md` — the operator's
  steering document for the proposer. Read fresh every round; no
  caching. Contains preferred targets, a mechanically-enforced
  `## Forbidden` list of mutation-point ids, and style guidance. The name
  "brief" keeps this per-epoch steering document distinct from the
  per-entry `Rubric` outcome check (see
  [EPOCHS-AND-JOURNALING.md](EPOCHS-AND-JOURNALING.md) §7).
- `adapter.mutation_points()` — the full mutation surface. The
  proposer addresses patches by mutation-point id.
- A capped digest of **prior experiments** for the current epoch —
  each settled experiment's `core_idea`, the mutation-point ids it
  touched, its verdict, and its Δscalar — read from the analytical
  index's `experiments` table (§4.11). This is **experiment memory**:
  it stops the proposer from re-proposing known failures and lets it
  build on known wins, turning the memoryless hill-climb into a search
  that remembers what it already tried. Advisory context, scoped to
  the current contract; see [EXPERIMENT-MEMORY.md](EXPERIMENT-MEMORY.md).
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
in the journal reduces to "something changed, and here is the score
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
`patches_applied.json` recording what changed.

**Validator constraints (every patch must pass all):**

- The patched file still parses as valid Python (`ast.parse`).
- Every imported name in the patched file resolves (no new
  `NameError` on import).
- The targeted mutation-point id resolves to a single location
  after the patch, so a later generation can find it again.
- For prompt templates, all required `{...}` placeholders that the
  pre-patch text contained are preserved in the post-patch text.
- The patch does NOT touch any mutation-point id that appears in the
  proposer brief's `## Forbidden` list.

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
  and the LLM grader behind any `rubric`-kind outcome check.

**Hard rule at config time.** The two callables MUST differ by
*callable identity* OR by an explicit `model=` override. If they do not
differ, zicato refuses to start the run. The check is a hard error;
there is no warn-and-continue path.

The rule exists because *collusion is the risk*, rather than because
vendor diversity is a goal in itself. If the same model is judging itself
and emulating users for itself and proposing patches for itself, every
loop in zicato becomes degenerate at a different level. See
[EMULATOR.md](EMULATOR.md) §"Collusion-proof by construction" for the
full argument and [RATIONALE.md](RATIONALE.md) for why zicato makes the
operator configure the pair rather than supplying a default.

The `CallLLM` return remains answer text. A backend that emits separate private
reasoning and answer channels can opt into `zicato.reasoning` before registering
its callable. That adapter performs one bounded, reasoning-disabled fallback
only when the backend explicitly reports answer-budget exhaustion; it never
uses private reasoning as answer text. See
[REASONING-AWARE-CALLS.md](REASONING-AWARE-CALLS.md). A flattened text callable
cannot recover the channel boundary and is not eligible for this adaptation.

### 4.11 Analytical index

**Responsibility.** Make cross-run questions fast. The
filesystem layout (§6) is canonical and human-legible but poor
for `GROUP BY` / `JOIN` queries that range across many
generations. The analytical index is a derived, fully-rebuildable
SQLite sidecar — `.zicato/index.db` — that projects the canonical
artifacts into a relational schema.

**Consumes.** Every `gen_score.json`, `experiment.json`,
`patches/*.json`, `runs/*/loss.json`, and `lineage.json` in the
workspace.

**Produces.** Nine tables (`epochs`, `generations`,
`experiments`, `patches`, `runs`, `loss_profiles`,
`metric_counts`, `tournaments`, `judge_losses`).
Cross-run views — the dashboard's tournament analytics,
loop-health detectors, the lineage queries — read the index
instead of walking files.

**Contracts.**

- **Files are canonical; the index is derived.** The index holds
  no fact not also on disk. `zicato repair index` reconstructs it
  in full from the filesystem; it is disposable.
- **Canonical-file-first dual-write.** The orchestrator writes
  the canonical file, then the index row. The index can only
  ever lag the filesystem, never lead it — so a crash leaves a
  self-healing behind-index, never a phantom row.
- **Single writer.** Only the orchestrator writes the index; the
  dashboard service opens it read-only (the Rust supervisor's
  read-only `rusqlite` access exists but, under `--no-dashboard`, is
  not the live-dashboard reader).

The full schema, the rebuild semantics, and the
SQLite-here-not-there boundary are in
[ANALYTICAL-INDEX.md](ANALYTICAL-INDEX.md).

### 4.12 Loop-health diagnostics

**Responsibility.** Detect when the meta-loop is *running but not
optimising anything*. The robustness layers (§5) keep the loop
from *breaking*; loop-health catches an evaluation that cannot
distinguish any candidate. In one real run the seed generation
`v0` and its child `v1` both scored `1.000000`, and only an
operator reading the journal noticed.

**Consumes.** Each round's runs, scores, and the epoch-so-far
history.

**Produces.** A typed `LoopHealth` report per round, written to
`epochs/{epoch}/loop_health/round_{NNN}.json`. Five detectors
(degenerate scoring, non-differentiating board entries, flat
drift signal, no-expectations, stalled loop) emit findings with
`info` / `warning` / `critical` severities.

**Contracts.**

- A `critical` finding triggers a bannered orchestrator warning
  and a server-sent-events (SSE) update to the dashboard's loop-health
  panel, so the silent degeneracy described above does not
  depend on an operator noticing.
- The orchestrator **stops early on sustained degeneracy by
  default** (two consecutive rounds with a CRITICAL loop-health
  finding stop the loop with a `degenerate_health` reason). The
  behaviour is built into the orchestrator rather than exposed as a
  flag; there is no `--stop-on-degenerate` option on `zicato evolve`.

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

zicato is operated through a single CLI binary. The CLI is
**evolve-centric**: the happy path is two commands, and everything
else is advanced / debug tooling for driving one stage in isolation.

**The happy path:**

| Subcommand | What it does |
|---|---|
| `zicato init` | Create a `.zicato/` workspace and scaffold the contract files. |
| `zicato evolve [--rounds N]` | The orchestrator: one command, many rounds. Auto-epochs — hashes the evaluation contract (board + proposer brief + scoring + harness identity) and rolls a new epoch when any of it changed. This is the command an operator runs day to day. |

`zicato evolve` does internally what the advanced commands below do
one stage at a time: register-aware setup, per-entry runs, pattern
analysis, proposal, applying patches, the tournament, journaling, and
auto-epoching. An operator authoring a board edits `board.jsonl` and
the proposer brief, then runs `zicato evolve`; the epoch rolls
automatically.

**Advanced / debug commands** (drive one stage in isolation):

| Subcommand | What it does |
|---|---|
| `zicato epoch register --adk path:agent --mutable-tree <path>` | Register an inner harness via an adapter. |
| `zicato board add/list/remove` | Edit the current epoch's board by hand. |
| `zicato inspect mutations` | Audit the current mutation surface — every span, every file marker. |
| `zicato proposer propose` | Run the proposer; emit one `Experiment`. |
| `zicato tournament run PARENT CHILD` | Run the tournament between two generations in isolation. |
| `zicato epoch new/close/list/switch/set-goal` | Manage epochs manually (the escape hatch from auto-epoching). |
| `zicato repair index` / `zicato repair generations` | Rebuild (or reconcile just the `generations` table of) the `.zicato/index.db` analytical index. |
| `zicato health` | Report whether the evolve loop has real optimization signal (loop-health diagnostics). |
| `zicato inspect telemetry` | (Re)run the decision-telemetry analyzer for an epoch. |
| `zicato repair report` | Re-render an epoch's `analysis.md` / `analysis.html` from on-disk data. |
| `zicato dashboard` | Serve the dashboard for an existing workspace (evolve auto-spawns it; this is the standalone form). |
| `zicato repair-*` | Targeted index/file migration helpers (`repair-epoch-goals`, `repair-judge-losses`, `repair-tournament-fk`, `repair-v0-baseline`). |

There is no standalone `zicato run`, `zicato analyze`, `zicato patch
apply`, `zicato journal show`, or `zicato analysis show` in the shipped
CLI; those stages run only inside `evolve` (the rendered report is
produced/regenerated by `analyze-telemetry` / `regenerate-report`).

The full reference for every subcommand is in [CLI.md](CLI.md).

## 5. Runtime and observability layer

The components above describe the meta-loop's logical structure.
The runtime layer is the surrounding scaffold that makes the loop
**survivable** (hangs, crashes, out-of-memory kills, and the long
tail of rarer failures)
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
   │  zicato evolve (Python)        │  spawn  │  watchdog supervisor (Rust)│
   │  ───────────────────────────   ├────────►│  ────────────────────────  │
   │  • acquires .zicato/runtime/   │         │  --no-dashboard mode:      │
   │    lock.json (pid-based)       │         │  watches heartbeat.json +  │
   │  • writes heartbeat.json (2s)  │         │  active_runs/*.            │
   │  • runs each tournament run    │         │  Heartbeat-stale → flag    │
   │    in a subprocess worker,     │         │  orchestrator stalled.     │
   │    so a hung run cannot        │         │  Run stale/past deadline → │
   │    wedge the orchestrator      │         │  SIGTERM → grace → SIGKILL.│
   │  • writes active_runs/{id}.json│         │  Serves /statusz.          │
   │  • reads control/ at           │         └────────────────────────────┘
   │    safe points                 │  spawn  ┌────────────────────────────┐
   └────────────────────────────────┼────────►│  dashboard service (Python)│
                                     │         │  ────────────────────────  │
                                     │         │  Starlette + uvicorn.      │
                                     │         │  Serves HTTP + SSE on      │
                                     │         │  :7892 (walks +1). Reads   │
                                     │         │  runtime/ + index.db +     │
                                     │         │  epochs/. POST /api/       │
                                     │         │  control/* writes control/ │
                                     │         │  files; the orchestrator   │
                                     │         │  consumes them.            │
                                     │         └────────────────────────────┘
```

Three properties hold across the runtime layer:

1. **File-based state is the only source of truth.** Memory
   state in any process is a cache of what is on disk.
2. **No LLM in the watchdog path.** Watchdog decisions are
   deterministic functions of file timestamps.
3. **Single-writer per file.** Each state file has a single writing
   process — the orchestrator, the dashboard service, or one
   tournament worker. No locking beyond the pid-based `lock.json`.

**Supervisor-binary ownership.** The Rust watchdog binary splits along
the library/driver boundary. *Packaging* belongs to the root wheel: the
hatchling build hook (`hatch_build.py`) compiles the crate and bundles
the artifact at `zicato/_bin/zicato-supervisor`, so every wheel install
carries it. *Resolution* is CLI policy.
`zicato.cli.commands.evolve._resolve_supervisor_binary` decides which
binary actually runs (the `--supervisor-binary` flag / config pin, the
freshest of the bundled `_bin/` copy vs. a dev checkout's
`target/release` build, then `PATH`). The library never locates or
spawns the supervisor; a caller embedding zicato as a library brings
its own watchdog story.

The full design lives in seven documents:

| Concern | Document |
|---|---|
| State file layout, supervisor lifecycle, resume semantics, concurrency model | [RUNTIME.md](RUNTIME.md) |
| Live dashboard panels, HTTP + SSE API, predicted gate verdict, the control-file protocol for operator interactivity | [DASHBOARD.md](DASHBOARD.md) |
| The tournament competition model — the king-of-the-hill gauntlet, the bracket view, the per-matchup detail, the tournament analytics | [TOURNAMENT.md](TOURNAMENT.md) |
| The six-layer defense model (`asyncio.wait_for` → cancellation → subprocess workers → watchdog → circuit breaker → atomic writes) and what each catches | [ROBUSTNESS.md](ROBUSTNESS.md) |
| Loop-health diagnostics — detectors for an evaluation that cannot distinguish candidates, the `LoopHealth` report, `zicato health` | [LOOP-HEALTH.md](LOOP-HEALTH.md) |
| Directory-backed generation storage, and the git-backed generation store (§7 there) that gives blob deduplication plus `git log` / `git diff` / `git bisect` over generations | [STORAGE.md](STORAGE.md) |
| The `.zicato/index.db` SQLite analytical index — schema, the files-canonical / index-derived discipline, `zicato repair index` | [ANALYTICAL-INDEX.md](ANALYTICAL-INDEX.md) |

The runtime layer ships in stages (see [ROBUSTNESS.md](ROBUSTNESS.md)
§4 and [RUNTIME.md](RUNTIME.md) §8 for the what-ships boundary).
**Shipped today:**

* the per-call and per-budget timeouts (`asyncio.wait_for`), together
  with structured cancellation;
* the subprocess worker boundary — each board-entry run executes in its
  own `python -m zicato._tournament_worker` subprocess, so a hung run
  can be sent SIGTERM without taking down the whole `evolve`;
* atomic writes everywhere;
* the orchestrator watchdog, which is the Rust supervisor binary that
  `evolve` auto-spawns;
* the consecutive-reject circuit breaker;
* the `.zicato/runtime/` state files;
* the dashboard, served as a **separate Python service** rather than as
  a role of the Rust binary, with its GET API, its server-sent-events
  stream, and both sides of the control endpoints. The orchestrator
  consumes `control/` commands at safe points
  (`zicato.runtime.control_consumer`, called from `evolve/loop.py`,
  `epoching.py`, `field.py`, `gauntlet.py` and `gate.py`) and archives
  each consumed command into `control_log/`;
* the conservative crash-resume protocol
  (`zicato.runtime.resume.prepare_resume`, called from `evolve/loop.py`),
  which resumes an interrupted epoch where the durable markers allow it
  and discards partial work where they do not.

Generation source trees are stored in a private git repository per
workspace, which is the default backend that `zicato init` records
(`DEFAULT_GENERATION_SOURCE_BACKEND` in `epoch/genstore.py`; the
implementation is `epoch/git_genstore.py`). The directory-snapshot
backend remains fully supported and is selected with
`generation_source_backend: "directory"` for an environment where a
private git repository is unwanted. [STORAGE.md](STORAGE.md) §7
specifies the git backend.

Because each run crosses a process boundary, the run's inputs are
serialised to a temp args file and rebuilt inside the worker. The
`ScoringWeights` carried across that seam is written by
`runner._weights_spec` and read back by
`_tournament_worker._weights_from_args`. Those two must stay
field-for-field in lock-step. A field present in the parent but missing
from the reader is silently reset to its default in the subprocess,
which desynchronises the worker's gate decision from the parent's.
`per_judge_weights` (and `pass_rate_monotonicity_scope`) are carried
across this boundary for
that reason — so the worker's per-judge loss attribution and
its gate-view match what the parent would have computed in-process.

## 6. Storage layout

zicato keeps everything under a per-project workspace, by default
`.zicato/` next to the inner harness's source root. A deployment
that runs several zicato instances at once — one zicato evolving a
nested zicato, for instance — keys each workspace by an `instance_id`
configured at runtime, so workspaces never cross-talk.

```
.zicato/
  config.json                      # registered adapter, call_llm wiring, instance_id
  epochs/
    {epoch_id}/
      board.jsonl                  # frozen for this epoch
      brief.md            # operator-edited; read fresh each round
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
or markdown. The storage design spends its budget on keeping a
workspace debuggable with `ls` and `cat`, because the filesystem is the
operator's first-class interface.

The one non-text file is `.zicato/index.db` — the **analytical
index**. It is *not* canonical: it is a derived, fully-rebuildable
SQLite projection of the files above, a cache that makes cross-run
`GROUP BY` / `JOIN` queries fast without a file-walk. It holds no
fact not also on disk; `zicato repair index` reconstructs it in full. The
filesystem stays the source of truth; the index is a sidecar. See
[ANALYTICAL-INDEX.md](ANALYTICAL-INDEX.md).

## 7. The harmonograf split: execution view vs competition view

zicato and harmonograf both render a "view of a run", and the
boundary between them is load-bearing: they are two linked
tools rather than one merged interface.

> **harmonograf is the execution view; the zicato dashboard is
> the competition view. A per-run drill-down links them, and they
> stay separate tools.**

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

The diagram in §2 names the components. The table below names the
contract between each producer and its consumer, so either side can be
reimplemented without breaking the other.

| Producer | Consumer | Contract |
|---|---|---|
| `HarnessAdapter` | `Runner` | `run_entry(entry, sinks=[...])` emits goldfive events to the supplied sinks; terminates with `RunCompleted` or `RunAborted`. |
| `Runner` | `Loss reducer` | A path to one `events.jsonl` that is a complete run (one `RunStarted`, one terminal event). |
| `Loss reducer` | `Pattern detectors` | One `LossProfile` JSON per run, schema in §4.5. |
| `Pattern detectors` | `Patch proposer` | A `list[Pattern]` reset on epoch boundaries. |
| `Analytical index` (`experiments`) | `Patch proposer` | A capped `list[PriorExperiment]` for the current epoch (experiment memory) — settled verdicts + Δscalars + touched ids, scoped to the contract. Advisory; never gates. See [EXPERIMENT-MEMORY.md](EXPERIMENT-MEMORY.md). |
| `Patch proposer` | `Applier` | An `Experiment` (schema in §4.7) with patches addressing valid mutation-point ids. |
| `Applier` | `Tournament` | A candidate `Generation` snapshot that passes all validator constraints in §4.8. |
| `Tournament` | `Journal + outcome` | A `tournament_decision` with score deltas. |

A reader can replace any single component with their own implementation
as long as the contracts hold. That is what "framework-agnostic" means
in zicato: every contract is between named, typed shapes, so writing one
new adapter is all that adopting zicato for another framework requires.
It does not mean that every framework works on day one.

## 9. What is out of scope

- Cross-machine distribution. zicato runs locally; nothing in the design
  prevents distributed runners, but the storage layout assumes a single
  filesystem.
- Caching of LLM calls. Caching is a wrapper concern on the
  `call_llm` callable; zicato makes no assumptions either way.
- Per-turn intervention. zicato never acts inside a run; that is
  goldfive's domain.
- A web UI. The CLI is the surface; harmonograf exists for the live
  run view.
- Multi-tenant workspaces. There is one workspace per project,
  optionally keyed by `instance_id` when one zicato evolves a nested
  zicato instance.
- Anonymisation or PII scrubbing on the event stream. The board is
  whatever the operator put on it; the JSONL captures what
  flowed.

## 10. Further reading

| Topic | Document |
|---|---|
| Annotated mutation points, AST resolution, audit CLI | [MUTATION-SURFACE.md](MUTATION-SURFACE.md) |
| Board entry schema — `expectations` + `judges`, multi-turn emulator | [BOARD-FORMAT.md](BOARD-FORMAT.md) |
| Authoring boards — outcome vs process checks, builder, scoring weights | [BOARD-AUTHORING.md](BOARD-AUTHORING.md) |
| Epoch concept, the proposer brief, experiment journaling, analysis pass | [EPOCHS-AND-JOURNALING.md](EPOCHS-AND-JOURNALING.md) |
| Experiment memory — feeding prior experiment outcomes back to the proposer | [EXPERIMENT-MEMORY.md](EXPERIMENT-MEMORY.md) |
| goldfive event capture, loss reducer, emulator audit lane | [TELEMETRY.md](TELEMETRY.md) |
| Drift loss scalar, pass-rate, tournament promotion gate | [SCORING.md](SCORING.md) |
| The tournament competition model — gauntlet, bracket, per-matchup detail, analytics | [TOURNAMENT.md](TOURNAMENT.md) |
| User emulator design + collusion-proof construction | [EMULATOR.md](EMULATOR.md) |
| The three dogfood targets and the design they force | [DOGFOOD-TARGETS.md](DOGFOOD-TARGETS.md) |
| `.zicato/runtime/` state files, the Rust supervisor binary, resume protocol | [RUNTIME.md](RUNTIME.md) |
| Live dashboard panels, HTTP + SSE, predicted gate verdict, control-file protocol | [DASHBOARD.md](DASHBOARD.md) |
| The six-layer defense model against hangs and crashes | [ROBUSTNESS.md](ROBUSTNESS.md) |
| Loop-health diagnostics — detectors for a degenerate evaluation, `zicato health` | [LOOP-HEALTH.md](LOOP-HEALTH.md) |
| The git-backed generation store and its migration tooling | [STORAGE.md](STORAGE.md) |
| The `.zicato/index.db` SQLite analytical index — schema, discipline, `zicato repair index` | [ANALYTICAL-INDEX.md](ANALYTICAL-INDEX.md) |
| CLI reference, every subcommand | [CLI.md](CLI.md) |
| Why each major decision was made the way it was | [RATIONALE.md](RATIONALE.md) |
| Glossary | [VOCABULARY.md](VOCABULARY.md) |
