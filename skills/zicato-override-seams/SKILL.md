---
name: zicato-override-seams
description: Configure a custom harness, target predicates, and optional outcome summaries. Use this skill to choose a supported adapter and grading interface, register its fixed driver imports, validate setup without model requests, and keep implementation changes in the evaluation contract.
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
judges, and extracts `final_output`. Override for anything else — a custom
class, a CLI, a service, a black box — and you owe zicato all of that yourself.

### Implement

```python
from zicato.adapters import HarnessAdapter, RunnableHarness, entry_disable_drift
from zicato.core.types import BoardEntry, RunResult, RuntimeConfig
from zicato.judge_runtime import assemble_judges
```

`load`, `mutable_subpaths`, and `mutation_points` are required
(`base.py:REQUIRED_ADAPTER_METHODS`). Each generation is a full copy of your
source into a fresh `generation_root`; `mutable_subpaths` picks which
directories of it the proposer may rewrite and the `# zicato:mutable` markers
inside them pick what is editable. Too wide and the proposer breaks the
harness; too narrow and it finds nothing to change.

- **`make_adapter() -> HarnessAdapter`** — zero-argument module-level factory.
  Returns the adapter instance at worker startup.
- **`load(snapshot_root: Path) -> RunnableHarness`** — return a session bound to
  `snapshot_root` so the worker exercises mutated code rather than the baseline.
  Raise if the entrypoint will not resolve, so zicato fails the candidate
  cleanly.

  ⚠️ Reach the snapshot WITHOUT importing it (`ast.parse`, read its files, or a
  subprocess): it is proposer-patched code, and a destructive patch must score
  badly rather than crash the worker. Bind to the baseline by mistake and
  **every generation scores identically**.
- **`mutable_subpaths(generation_root: Path) -> list[Path]`** — re-base your
  declared trees onto the snapshot by **basename** rather than relative path: a
  registered `./src/my_pkg` lands at `generation_root / "my_pkg"`. Return only
  paths that exist; `[]` or a missing method falls back to the whole snapshot.

  ⚠️ Containment is never validated — a path outside `generation_root` is walked
  as given, so a stale repo path silently enumerates the BASELINE tree.
- **`mutation_points(source_roots=None)`** — required, but return `[]`: zicato's
  own scanner walks the mutable trees for `# zicato:mutable`.
- **`worker_spec() -> dict[str, Any]`** — entries run in a killable subprocess
  your adapter object cannot cross, so zicato ships a recipe instead:
  `{"kind": "import", "factory": "my_pkg.harness:make_adapter"}` plus an
  optional JSON-serializable `"args": [...]` replayed positionally. Hence
  `make_adapter` must be importable and module-level. Omit this method and only
  the built-in ADK shape is recognised — anything else aborts the run.

The worker `await`s your session once per `BoardEntry`:

```python
async def run(self, entry: BoardEntry, sinks: list[Any], config: RuntimeConfig) -> RunResult:
```

Dispatch on `entry.kind` — `entry.input` is set only for `single_turn`; both
multi-turn kinds leave it `None` and carry `entry.turns` / `entry.user_persona`
instead. Push lifecycle events to `sinks` and return a `RunResult`. Five fields
have no defaults — `run_id`, `entry_id`, `final_output`, `transcript`,
`runtime_ms` — so every construction supplies all five, the abort path
included: `RunResult(..., aborted=True, abort_reason="wall_clock_budget")` past
`entry.wall_clock_budget_seconds`. That string is matched verbatim; any other
spelling leaves `budget_exceeded` false.

⚠️ **The second parameter's name is load-bearing.** Zicato inspects your `run`
signature, and if that parameter is called `sink_path` or `events_path` it
assumes an older adapter API: it calls `run(entry, <path to events file>)`
instead, so you get a `Path` rather than the sink list, `config` is never
passed, and your returned `RunResult` is thrown away. Nothing errors — the run
just scores as though the agent produced nothing. Call it `sinks`.

**Telemetry is now your job.** Nothing but your adapter writes `events.jsonl`.
Emit nothing and the whole drift half of the score is `0.0` — candidates rank
on pass/fail alone. Push to every sink in the list, which can be empty, and
never `close()` one; the worker owns them. Two ways to produce the stream:

**Option A — let goldfive drive it.** Goldfive is not ADK-specific. Wrap your
target in any shape it accepts — an `AgentAdapter`, an ADK agent or `Runner`, a
supported third-party SDK client factory, or a bare async
`(task, session, tools) -> InvocationResult` callable — and pass it the sinks:

```python
judges = assemble_judges(              # drop `judges=` below and NO judge runs
    entry_judges=entry.judges, disable_drift=entry_disable_drift(entry),
    aux_call_llm=config.effective_judge_call_llm(),  # evaluation, never target
)
outcome = await goldfive.run(
    my_target, entry.input, sinks=sinks, call_llm=config.target_call_llm,
    judges=judges,
)
```

That gives the full instrumented stream on the default dialect — drift
detectors, `plan_revised`, terminal events. Use it whenever the target is
in-process.

**Option B — emit the events yourself.** For a target you can only watch from
outside: a CLI, a service, a black box. Push plain dicts from the adapter and
set `"telemetry_dialect": "adk_events"` in **`scoring.json`** — a supported
shape, and the one knob in this section that does NOT live in `config.json`.

```python
async def emit(sinks, event: dict) -> None:
    for sink in sinks:
        await sink.emit(event)          # async, positional-only; never close()

await emit(sinks, {"type": "tool_call", "tool": "search", "args": {"q": q},
                   "run_id": run_id, "session_id": session_id})
await emit(sinks, {"type": "tool_response", "tool": "search", "status": "error"})
await emit(sinks, {"type": "agent_message", "text": final_output})
```

One JSON object per line, discriminated by `type`. See
[TELEMETRY-DIALECTS.md](../../docs/design/TELEMETRY-DIALECTS.md) §3.1–§3.3 for
the recognised types, their shapes, aliases, and derived signals — do not
guess. ⚠️ An unrecognised `type` is skipped silently, so a typo is
indistinguishable from an event you never sent.

Dropping to `adk_events` permanently costs three signals, because they come from
watching the agent think and an event log only records what it did:
`plan_revisions` is always `0`, no reasoning detector fires, and custom judges
never fire — so `per_judge_weights` multiplies zero while still looking active.
⚠️ Zicato warns about that mismatch but does not reject it.

If you need those three, emit goldfive protos and keep the `goldfive` dialect:
build them with the `goldfive.events` factories, number each with a `sequence`
from `0`, and for `custom:<judge_name>` attribution emit a `judgement_emitted`
immediately before each `drift_detected`. Either way, close every exit path
with a terminal event (`run_completed` / `conversation_ended`); zicato only
writes `run_aborted` for a worker killed from outside.

### Apply

Register the fixed driver separately from the mutable target:

```sh
zicato epoch register --workspace .zicato \
  --factory my_driver.harness:make_adapter --import-root . \
  --mutable-tree ./my_target
zicato inspect setup --workspace .zicato
```

The factory receives `--factory-args` as a JSON array and `--factory-options`
as a JSON object of keyword arguments. Their persisted fields are `adapter.args`
and `adapter.options`; the implementation validates its arguments. The fixed
import roots are relative to the workspace parent unless absolute. They are
operational locations and do not enter the contract hash. Workers receive the
resolved roots explicitly, with their candidate snapshot first in the import
search path. The coordinator restores its import tables when execution ends.

The canonical `adapter` declaration also carries `mutable_trees`. Registration
writes the compatible `mutable_trees` and `source_roots` aliases used by existing
commands. Fixed drivers, predicates, and summarizers belong outside those trees.
The contract hashes the declared factory path, constructor configuration, and
resolved implementation source, even when `worker_spec()` returns a constant
specification. Changing any of those semantic inputs rolls the epoch; a declared
factory without inspectable source is refused.

`inspect setup` imports the configured hooks and loads a snapshot in a bounded
subprocess. It makes no model request and runs no board entry. A custom adapter
using only stock grading receives an advisory unless `--confirm-stock-grading`
records that choice explicitly.

## 2. Board entry scoring: the `predicate` expectation

### When

The built-in matchers (`expected_text`, `regex`, `json_schema`, `rubric`) emit a
bare pass/fail bit. Reach for `predicate` when an entry needs partial credit (so
the optimizer sees 0.8 > 0.2 instead of a cliff), a decomposition alongside the
score, or logic no matcher expresses.

Note `rubric` emits **neither `score` nor `metrics`** — only `passed` and a
`detail` string; the grader's number and per-dimension breakdown are formatted
into that string and discarded.

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

- **`bool`** signals binary pass/fail; a bool entry the champion passed must
  still pass under the default `per_entry` gate.
- **`float`** gives the optimizer a gradient. The pass term runs on
  `mean_score`, so 0.2 → 0.6 is rewarded even though neither is a pass. The
  `per_entry` gate covers it too, allowing a dip of 0.02 before it trips.
- **`(score, metrics)`** adds the per-entry decomposition. On a float return
  `passed` is `score > 0.0` — display-only, true for any credit at all.

**The `metrics` mapping.** A flat dict of name → number recording *why* the
entry scored what it did: a 0.4 from poor recall reads differently from a 0.4
from over-retrieval. Values must be plain numbers an average is meaningful over
— zicato means each key across replicates, so rates and scores work but ids and
running totals do not. **`metrics` never affects scoring.** Names decide who
can read the value:

- **`precision` and `recall` are reserved** — those exact spellings feed the
  built-in outcome marginals that separate over-retrieval from misses.
- Any other name is inert until wired through `outcome_summarizer_spec` (§3),
  though it still reaches `loss.json`, the dashboard's metrics digest, and the
  query views' `parent_metrics` / `child_metrics`.
- ⚠️ No namespace routing. `"rubric:accuracy"` does NOT feed the `rubric:`
  namespace weight or the scalar — that colon convention belongs to
  `metric_counts`. Use bare names.

**Everything fails closed** to `passed=False` plus a `detail`: an unimportable
path, a non-callable target, a raise, a 2-tuple with a `bool` first element, a
2-tuple whose second element has no `.items()`, any other return type. `NaN`
clamps to `0.0`. ⚠️ The one exception is `metrics` itself — every value is
coerced with `float(v)` outside that guard, so a non-numeric one raises out of
the evaluation. Sanitize in your scorer.

### Apply

Both dotted forms resolve, as everywhere else in zicato: `pkg.mod:fn` and
`pkg.mod.fn`. Parsing the board does not check that the path imports — the
`zicato evolve` preflight does, reporting `predicate_unresolvable` before the
loop starts, and at run time an unresolvable path fails the entry closed.

```json
"expectation": {
  "kind": "predicate",
  "spec": "my_pkg.scorers:grade_retrieval",
  "reads": "final_output"
}
```

Or build it with `Predicate.python(...)` from `zicato.board.predicates`; only
the path lives in the board JSON, but the contract hash covers the resolved
function's source, so editing the body rolls the epoch. `reads`
(`"final_output"` default) is recorded but has no evaluation-time consumer —
read `RunResult.transcript` yourself for anything past the last turn.

## 3. Proposer feedback: `outcome_summarizer_spec`

### When

Each round zicato hands the proposer a **failure-mode profile** so it can target
*why* answers are wrong. Eight marginals are built in: `empty_rate`,
`terse_rate`, `looping_rate`, `pass_rate`, `mean_score`, `recall_mean` /
`precision_mean`, `over_retrieval_rate`. Override when your board has a failure
mode none of those name — a defect the proposer keeps re-introducing because
nothing in the prompt says it exists.

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

Return **board-wide rates only**, never per-entry rows: the proposer may learn
properties of the agent's behaviour but must never see enough to reconstruct a
board entry. Zicato drops an entry unless the key is a non-empty lowercase
`str` of at most 48 characters and the value is a finite `int` or `float`
(`bool` is rejected). A non-mapping return discards everything. ⚠️ `_ - : .`
all pass the key filter, so keep entry ids out of your keys yourself.

**How the proposer sees it.** Each survivor becomes one line in the round's
failure-mode profile, sorted by name and coarsened to the nearest 10%:

```
- pass-rate: ~60% | mean score: medium (~0.6)
- low_clarity_rate: ~30% of runs
```

`none` means zero, `~all` essentially every run. The blurring stops the
proposer tuning itself to this particular board.

**Name keys for a reader.** The key is printed into the prompt verbatim, so
`low_clarity_rate` tells the model something and `m3` does not. Names also
steer: each round zicato picks one hint from whichever failure mode dominates
the rendered profile by pattern-matching its text, so a key echoing a built-in
mode (`looping`, `over-retrieval`, empty / terse) changes the hint every
candidate gets.

Setup validation refuses a configured hook that does not resolve to a callable
accepting one argument. If an optional summarizer raises during a round or
returns a non-mapping result, the round continues without its marginals and logs
`outcome_summarizer_failed`. The failure is retained under the epoch's `health/`
directory and appears in both the round health report and `zicato health`.
Cover the hook with representative `LossProfile` inputs and check that every
returned key survives the sanitization rules above.

**What your hook can read.** `lp.metrics` holds your custom numbers only when a
§2 predicate returned `(score, metrics)`; on a board of `rubric` / `regex` /
`json_schema` entries it is empty. `drift_counts` and `output_chars` are always there, but `pass_fail` and
`score` are `None` on aborted or skipped units — guard before arithmetic. Read
`LossProfile.metrics`, NOT `LossProfile.expectation_result.metrics`: the former
is the replicate mean, the latter replicate 0's raw values.

### Apply

A plain `ScoringWeights` field — hand-edit `scoring.json`. No CLI
flag. Default `""` means no hook; both dotted forms resolve as in §2.

```json
{"outcome_summarizer_spec": "my_pkg.summarize:board_marginals"}
```

The scoring document and the resolved summarizer source are contract inputs.
A semantic edit to either rolls the epoch. Validate the configured dotted path
with `zicato inspect setup` before starting measured rounds.

## Reference

- [BOARD-FORMAT.md](../../docs/design/BOARD-FORMAT.md) · [SCORING.md](../../docs/design/SCORING.md) · [TELEMETRY-DIALECTS.md](../../docs/design/TELEMETRY-DIALECTS.md) · [TELEMETRY.md](../../docs/design/TELEMETRY.md) — expectation schema, the scalar and gate, dialect shapes, `LossProfile`.
- `skills/zicato-author-board`, `skills/zicato-tune-scoring` — board JSON, weights and gate knobs.
- `examples/zicato_examples/target_0_convergence/` — a working import-kind adapter (`harness.py`) and the `config.json` block that wires it (`RUN.md` §2).
