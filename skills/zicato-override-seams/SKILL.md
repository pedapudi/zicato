---
name: zicato-override-seams
description: Set up zicato's three override seams when a target or a metric diverges from the defaults — a custom HarnessAdapter for non-ADK targets (--adk module:factory), the predicate expectation for board entries needing partial credit or a metrics decomposition instead of a bare pass/fail, and outcome_summarizer_spec for proposer failure categories zicato does not compute. Use when deciding whether you need an override, writing one so zicato can consume what it returns, and wiring it onto the contract. Every seam attaches by DOTTED PATH — zicato imports your callable from your own package and never serializes your code.
---

# zicato override seams — custom adapters and scoring

Zicato's defaults assume an ADK `root_agent` graded by a built-in matcher. When
that breaks, these seams can help bridge your system to zicato.

| Seam | Reach for it when | Attach on |
|---|---|---|
| 1 · `HarnessAdapter` | the target is not an ADK `root_agent` | `.zicato/config.json` |
| 2 · `predicate` expectation | an entry needs partial credit, a decomposition, or logic no matcher expresses | `board.jsonl` |
| 3 · `outcome_summarizer_spec` | the proposer needs a failure category zicato does not natively compute | `scoring.json` |

## 1. Execution: `HarnessAdapter`

### When

`--adk my_pkg.agent:root_agent` runs the agent inside `goldfive.run()`, which
captures model turns, streams tool calls into `events.jsonl`, evaluates in-run
judges, and extracts `final_output` — all free. Override when the target is not
an ADK `root_agent`: a custom class, a CLI, an external service, a black box.
You then owe zicato the telemetry, final output, and results goldfive would
have produced.

### Implement

```python
from zicato.adapters.base import HarnessAdapter, RunnableHarness
from zicato.core.types import BoardEntry, RunResult, RuntimeConfig
```

`load`, `mutable_subpaths`, and `mutation_points` are required
(`base.py:REQUIRED_ADAPTER_METHODS`). Together they define the **mutation
surface** — which files the proposer may rewrite.

Each generation is a full copy of your source into a fresh `generation_root`.
Most of that copy is support code the worker runs but the proposer must not
touch, so two things narrow it: `mutable_subpaths` picks the directories, and
the `# zicato:mutable` markers inside them pick what is editable. Too wide and
the proposer can break the harness; too narrow and it finds nothing to change.

- **`make_adapter() -> HarnessAdapter`** — zero-argument module-level factory.
  Returns the adapter instance at worker startup.
- **`load(snapshot_root: Path) -> HarnessSession`** — return a session bound to
  `snapshot_root`. Prepend it to `sys.path` and reload modules so the worker
  runs mutated code, not stale baseline code. Raise if the entrypoint will not
  resolve, so zicato fails the candidate cleanly.

  ⚠️ If `load()` imports baseline source instead of `generation_root`, candidate
  mutations never run and **every generation scores identically**.
- **`mutable_subpaths(generation_root: Path) -> list[Path]`** — re-base your
  declared trees onto the snapshot the worker runs. The rule is **basename, not
  relative path**: a registered `./src/my_pkg` lands at
  `generation_root / "my_pkg"`. That basename must be an importable top-level
  module name, since `load()` only puts `generation_root` on `sys.path`. Return
  only paths that exist; `[]` or a missing method falls back to the whole
  snapshot.

  ⚠️ Containment is never validated — a path outside `generation_root` is walked
  as given, so a stale repo path silently enumerates the BASELINE tree.
- **`mutation_points(source_roots=None) -> list[Any]`** — return `[]` to use
  zicato's scanner, which walks files for `# zicato:mutable`. Custom
  `MutationPoint` objects come from `zicato.core.types`, NOT `adapters.base`.
- **`worker_spec() -> dict[str, Any]`** — board entries run in a separate
  killable subprocess your adapter object cannot cross, so instead of the
  object zicato ships a recipe for rebuilding it:
  `{"kind": "import", "factory": "my_pkg.harness:make_adapter"}`, plus an
  optional `"args": [...]` passed positionally. The worker receives only that
  string, which is why `make_adapter` must be importable and module-level.
  Omit this method and only the built-in ADK shape is recognised — anything
  else aborts the run.

The worker calls your session once per `BoardEntry`:

```python
session.run(entry: BoardEntry, sinks: Sequence[EventSink], config: RuntimeConfig) -> RunResult
```

Run `entry.input`, push lifecycle events to `sinks`, return
`RunResult(aborted=True, abort_reason='wall_clock_budget')` past
`entry.wall_clock_budget_seconds`, else `RunResult(final_output=...,
runtime_ms=...)`.

⚠️ **The second parameter's name is load-bearing.** Zicato inspects your `run`
signature, and if that parameter is called `sink_path` or `events_path` it
assumes an older adapter API: it calls `run(entry, <path to events file>)`
instead, so you get a `Path` rather than the sink list, `config` is never
passed, and your returned `RunResult` is thrown away. Nothing errors — the run
just scores as though the agent produced nothing. Call it `sinks`.

**Telemetry is now your job.** Nothing but your adapter writes `events.jsonl`.
Emit nothing and the whole drift half of the score is `0.0` — candidates get
ranked on pass/fail alone, and nothing errors to tell you. Two mechanics: push
to every sink in the list, which can be empty; and never `close()` one, because
the worker owns them.

There are two ways to produce the stream.

**Option A — let goldfive drive it.** Goldfive is not ADK-specific. Wrap your
target in any shape it accepts — an `AgentAdapter`, an ADK agent or `Runner`, a
supported third-party SDK client factory, or a bare async
`(task, session, tools) -> InvocationResult` callable — and pass it the sinks:

```python
outcome = await goldfive.run(
    my_target, entry.input, sinks=sinks, call_llm=config.harness_call_llm,
)
```

You get the full instrumented stream for free — reasoning-drift detectors,
custom judges, `plan_revised`, the terminal frame — and keep the default
dialect. Use this whenever the target can run in-process.

**Option B — emit the events yourself.** For a target you can only watch from
outside: a CLI, a service, a black box. Push plain dicts from within the
adapter and set `telemetry_dialect: "adk_events"` (a supported shape, not a
workaround).

```python
async def emit(sinks, event: dict) -> None:
    for sink in sinks:
        await sink.emit(event)          # async, positional-only; never close()

await emit(sinks, {"type": "tool_call", "tool": "search", "args": {"q": q},
                   "run_id": run_id, "session_id": session_id})
await emit(sinks, {"type": "tool_response", "tool": "search", "status": "error"})
await emit(sinks, {"type": "agent_message", "text": final_output})
```

One JSON object per line, discriminated by `type`: `tool_call`,
`tool_response`, `error`, `agent_transfer`, `model_usage`, `agent_message`,
`user_message`. Refer to
[TELEMETRY-DIALECTS.md](../../docs/design/TELEMETRY-DIALECTS.md) §3.1–§3.3 for
shapes, aliases, and derived signals — do not guess. ⚠️ An unrecognised `type`
is skipped silently, so a typo looks exactly like an event you never sent.

**The dialect you set decides what can be measured at all.**

| Dialect | You emit | You can measure |
|---|---|---|
| `goldfive` (default) | goldfive proto events | everything |
| `adk_events` | plain dicts, as above | tool calls, errors, cost, retry loops |
| `transcript` | `{"role", "content"}` lines | predicates and rubrics only |

Dropping to `adk_events` permanently costs three signals, because they come from
watching the agent think and an event log only records what it did:
`plan_revisions` is always `0`, no reasoning detector fires, and custom judges
never fire — so `per_judge_weights` multiplies zero while still looking active.
⚠️ Zicato warns about that mismatch but does not reject it.

If you need those three, emit goldfive protos and keep the `goldfive` dialect.
Build them with the `goldfive.events` factories rather than by hand, number each
event with a `sequence` counting from `0`, and — for `custom:<judge_name>`
attribution — emit a `judgement_emitted` immediately before each
`drift_detected`.

Whatever you emit, end every exit path with a terminal frame. Zicato writes
`run_aborted` if the worker is killed from outside; your own clean and error
paths are yours to close.

### Apply

```sh
.venv/bin/zicato register --workspace .zicato \
    --adk harness:make_adapter --mutable-tree ./my_pkg
```

Equivalently in `.zicato/config.json`:

```json
{
  "instance_id": "my-project",
  "contract": {
    "adk": "harness:make_adapter",
    "mutable_trees": ["./my_pkg"]
  }
}
```

`adk` must resolve to your factory from the workspace root. Scope
`mutable_trees` to just the code you want rewritten — leave support code and
anything that grades the run outside it, or the proposer can edit the thing
measuring it.

## 2. Board entry scoring: the `predicate` expectation

### When

The built-in matchers (`expected_text`, `regex`, `json_schema`, `rubric`) emit a
bare pass/fail bit. Reach for `predicate` when an entry needs partial credit (a
continuous score, so the optimizer sees 0.8 > 0.2 instead of a cliff), a
decomposition carried alongside the score, or scoring logic no matcher can
express such as a weighted rubric.

Note `rubric` emits **neither `score` nor `metrics`** — only `passed` and a
`detail` string. The grader's number and its per-dimension breakdown are
formatted into that string and discarded. For numbers out of an LLM judge, call
the auxiliary callable from inside a `predicate`.

### Implement

```python
from zicato.core.types import RunResult

def grade_retrieval(result: RunResult) -> bool | float | tuple[float, dict[str, float]]:
    ...
```

Receives the whole `RunResult` — `final_output`, `transcript`, `runtime_ms`,
`aborted`, `abort_reason`. Sync or async. Must be deterministic; re-scoring an
epoch re-invokes it.

**The return type IS the seam.**

| Return | `score` | `pass_fail` | `metrics` |
|---|---|---|---|
| `True` / `False` | `1.0` / `0.0` | your bit | — |
| `0.82` | clamped `[0,1]` | degenerate (`score > 0.0`) | — |
| `(0.82, {...})` | clamped `[0,1]` | degenerate (`score > 0.0`) | your dict |
| anything else | — | `False` | — |

- **`bool`** signals binary pass/fail. A bool entry the champion passed scores
  `1.0`, and under the default `per_entry` scope the challenger must still score
  `1.0` — the only way to make an entry must-not-regress.
- **`float`** gives the optimizer a gradient. The pass term runs on
  `mean_score`, so 0.2 → 0.6 is rewarded even though neither is a pass.
- **`(score, metrics)`** adds the per-entry decomposition.

⚠️ One entry cannot do both. On a float return `passed` is `score > 0.0` —
display-only, true for any credit at all. For a gradient AND a gate, use two
entries.

**The `metrics` mapping.** A flat dict of name → number recording *why* the
entry scored what it did: a 0.4 from poor recall reads differently from a 0.4
from over-retrieval. Values must be plain numbers (non-numeric and infinite are
dropped silently) and quantities an average is meaningful over — zicato means
each key across replicates, so rates and scores work but ids and running totals
do not. Names decide who can read the value:

- **`precision` and `recall` are reserved** — those exact spellings feed the
  built-in outcome marginals that separate over-retrieval from misses.
- Any other name is inert until wired through `outcome_summarizer_spec` (§3),
  though it still reaches `loss.json`, the dashboard's metrics digest, and the
  query views' `parent_metrics` / `child_metrics`.
- ⚠️ No namespace routing. `"rubric:accuracy"` does NOT feed the `rubric:`
  namespace weight or the scalar — that colon convention belongs to
  `metric_counts`, a separate surface. Use bare names.

Either way **`metrics` never affects scoring**; only the score moves a number
that decides promotions.

**Everything fails closed** to `passed=False` plus an explanatory `detail`: an
unimportable path, a non-callable target, a raise, a 2-tuple with a `bool` first
element, a 2-tuple whose second element has no `.items()`, or any other return
type. `NaN` clamps to `0.0`.

### Apply

Module and callable are separated by a **colon**; the form is validated at
contract load, not at run time.

```json
"expectation": {
  "kind": "predicate",
  "spec": "my_pkg.scorers:grade_retrieval",
  "reads": "final_output"
}
```

Or build it with `Predicate.python(...)` from `zicato.board.predicates`. Bodies
are never serialized — only the path lives in the board JSON.

`reads` (`"final_output"` default; `"conversation_end"` is multi-turn only)
selects what the runner puts in `RunResult.final_output` before calling you.
`RunResult.transcript` is always available.

## 3. Proposer feedback: `outcome_summarizer_spec`

### When

Each round zicato hands the proposer a **failure-mode profile** so it can target
*why* answers are wrong, not just *that* a scalar moved. Eight marginals are
built in: `empty_rate`, `terse_rate`, `looping_rate`, `pass_rate`, `mean_score`,
`recall_mean` / `precision_mean`, and `over_retrieval_rate`.

Override when your board has a failure mode none of those name — a
domain-specific defect the proposer keeps re-introducing because nothing in the
prompt says it exists.

### Implement

```python
from collections.abc import Mapping, Sequence
from zicato.core.types import LossProfile

def board_marginals(losses: Sequence[LossProfile]) -> Mapping[str, float]:
    vals = [m["clarity"] for lp in losses if (m := lp.metrics) and "clarity" in m]
    if not vals:
        return {}
    return {"low_clarity_rate": sum(v < 0.5 for v in vals) / len(vals)}
```

Runs once per round on the **train-slice** `LossProfile` list — the holdout is
already excluded, and the hook reads no files, so it cannot widen its own slice.

Return **board-wide rates only**, never per-entry rows. That is the contract:
the proposer may learn properties of the agent's behaviour but must never see
enough to reconstruct a board entry. Zicato enforces it — an entry is dropped
unless the key is a non-empty lowercase `str` of at most 48 characters (no mixed
case, spaces, or punctuation, so an entry id cannot ride in as a key) and the
value is a finite `int` or `float` (`bool` is rejected, not coerced). A
non-mapping return discards everything.

**How the proposer sees it.** Each survivor becomes one line in the round's
failure-mode profile, sorted by name and coarsened to the nearest 10%:

```
- pass-rate: ~60% | mean score: mid
- low_clarity_rate: ~30% of runs
```

`none` means zero and `~all` means essentially every run. The blurring is
deliberate: exact per-round rates would let the proposer work out which edit
moved which number and tune itself to this particular board instead of getting
genuinely better.

Two things follow for your hook:

- **Name keys for a reader.** The key is printed into the prompt verbatim, so
  `low_clarity_rate` tells the model something and `m3` does not.
- **Your marginals are read, not acted on.** Each round zicato samples several
  candidate edits and steers each one with a hint chosen from whichever failure
  mode dominates the profile — but that choice only recognises four built-in
  modes. A custom marginal reaches the model as text it can reason about; it
  never changes which hint a candidate gets.

⚠️ **The seam fails quiet.** An unresolvable spec, a non-callable target, or a
raise yields an empty mapping and the round continues silently. Cover the hook
with a unit test over fixture `LossProfile`s. If nothing appears, check in
order: the spec resolves, the target is callable, it does not raise, every key
survives the rules above.

**What your hook can read.** `lp.metrics` holds your custom numbers only when a
§2 predicate returned `(score, metrics)`. On a board of `rubric` / `regex` /
`json_schema` entries it is empty, so a summarizer reading it finds nothing —
and, because this seam fails quiet, reports no error. Always populated whatever
the expectation kind: `drift_counts`, `output_chars`, `pass_fail`, and `score`.
So §2 and §3 are coupled: you can only aggregate a per-dimension number if a
scorer emitted it first. Read `LossProfile.metrics`, NOT
`LossProfile.expectation_result.metrics` — the former is the replicate mean, the
latter replicate 0's raw values.

### Apply

A plain `ScoringWeights` field — hand-edit `scoring.json`. No builder op, no CLI
flag.

```json
{"outcome_summarizer_spec": "my_pkg.summarize:board_marginals"}
```

Both dotted forms resolve (`pkg.mod:fn`, `pkg.mod.fn`). Default `""` means no
hook.

⚠️ Hand-editing `scoring.json` is unguarded: the loader builds `ScoringWeights`
by enumerating its declared fields, so a key that is not one of them is never
read. A misspelled knob does not error — it is silently ignored and whatever it
meant to configure keeps its default.

## Reference

- [BOARD-FORMAT.md](../../docs/design/BOARD-FORMAT.md) — the `expectation` schema and matcher kinds.
- [SCORING.md](../../docs/design/SCORING.md) — the scalar, the gate, and the `scalar_fn` / `drift_reducer` seams.
- [TELEMETRY-DIALECTS.md](../../docs/design/TELEMETRY-DIALECTS.md) — the three dialects and the `adk_events` shapes.
- [TELEMETRY.md](../../docs/design/TELEMETRY.md) — `LossProfile` and the namespaced metric surface.
- `skills/zicato-author-board`, `skills/zicato-tune-scoring` — board JSON, weights and gate knobs.
