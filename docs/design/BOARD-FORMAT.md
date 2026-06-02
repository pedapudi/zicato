# Board format

A **board** is the frozen-per-epoch list of tasks the inner harness is
evaluated against. The board defines the evaluation contract: change
the board and you have started a new epoch (see
[EPOCHS-AND-JOURNALING.md](EPOCHS-AND-JOURNALING.md)). Generations
within an epoch are directly comparable because they all answer the
exact same questions.

A board is one JSONL file. Path:
`.zicato/epochs/{epoch_id}/board.jsonl`. One entry per line. Lines are
parsed lazily by the runner; schema-invalid lines fail at
`zicato board add` time, not at run time.

This document specifies:

- The common fields every entry carries.
- The three entry kinds today (`single_turn`, `multi_turn_scripted`,
  `multi_turn_emulated`) and the per-kind fields.
- The two evaluation facets an entry carries: an **outcome** check
  (`expectation` — a single `Predicate` / `Rubric`) and **process**
  checks (`judges` — `Judge`).
- Wall-clock budget semantics, weight, tags, and tag-based pattern
  slicing.
- The board-level `board_meta` header line and its `disable_drift`
  setting.
- The forward-compatibility story for new entry kinds.

This document is the schema reference. For the *practical* side of
authoring — choosing outcome vs process checks, worked builder
examples, scoring weights — see the companion
[BOARD-AUTHORING.md](BOARD-AUTHORING.md).

The emulator's collusion-proof construction is specified in
[EMULATOR.md](EMULATOR.md); this document references it.

## 1. Common fields

Every board entry carries the same envelope:

| Field | Type | Required | Meaning |
|---|---|---|---|
| `id` | `string` | yes | Stable identifier; used as a directory name under `runs/`. Must be filesystem-safe (`[a-zA-Z0-9_-]+`). Globally unique within the board. |
| `kind` | `string` | yes | Discriminator. v0 set: `"single_turn"`, `"multi_turn_scripted"`, `"multi_turn_emulated"`. Open-ended — see §6. |
| `wall_clock_budget_seconds` | `number` | yes | Hard ceiling for the WHOLE entry. Exceeded → run aborts and scores as worst-case. |
| `weight` | `number` | no (default `1.0`) | Relative importance in scoring aggregation. |
| `tags` | `list[string]` | no (default `[]`) | Operator labels; pattern detectors can slice by tag. |
| `expectation` | `Expectation` | no (default absent) | A single **outcome** check — a `Predicate` / `Rubric` matcher run post-hoc on the run's output or transcript. Absent → drift-loss-only scoring for this entry. An entry carries **at most one** expectation. See §3. |
| `judges` | `list[Judge]` | no (default `[]`) | **Process** checks — goldfive judges that watch the reasoning stream in-run. Empty → only goldfive's ambient built-in judges run. See §4. |
| `context` | `object` | no | Opaque adapter-specific metadata. ADK adapters might use `{"attachments": [...], "session_state": {...}}`. Zicato never interprets the contents. |

Plus the per-kind fields in §2.

### 1.0 The `board_meta` header line

A board MAY begin with an optional **`board_meta` header line** — the
first line of the JSONL, discriminated by `"board_meta": true`. It
carries board-wide configuration: the `disable_drift` list (§4.2) and
the `judge_only` flag (§1.0.1). The header line is NOT a board entry and
is parsed separately by the loader. If present, it MUST be the first
line; a `board_meta` object anywhere else is a load error.

The real presentation board's header line is exactly:

```json
{"board_meta": true, "disable_drift": ["user_steer", "user_pause"], "judge_only": true}
```

`disable_drift` is a list of short lowercase `goldfive.DriftKind` wire
tokens (see §4.2). When a board is fully default — an empty
`disable_drift` AND `judge_only` false — the writer emits **no** header
line at all and the first line is the first entry, so a board with no
header is the common, fully-valid case (byte-identical to a board
written before either field existed).

#### 1.0.1 `judge_only`

`judge_only` (boolean, default `false`) selects **judge-only**
evaluation. When `true`, goldfive still JUDGES the wrapped agent — the
drift and process judges stay armed exactly as in the default mode — but
does ZERO steering: no goal-derivation LLM call, no planner replanning,
and no drift-triggered refine. The native agent tree still runs (so
there is a transcript to judge); only goldfive's own steering machinery
is disabled. The default (`false`) leaves the steering path unchanged
and byte-identical.

`judge_only` folds into the epoch **contract hash** (§10 / see
`docs/design/EPOCHS-AND-JOURNALING.md`), so flipping it opens a new
epoch. Authoring it from Python sets `Board.judge_only`; on disk it is a
key on the `board_meta` header. A non-boolean value is a load error.

### 1.1 `id`

The id is the canonical reference to this entry. Patterns cite by id;
journal entries refer to entries by id; `runs/{entry_id}/events.jsonl`
uses the id as a directory name. Once written, an id should never be
reused or renamed within an epoch — doing so silently invalidates the
pattern history within that epoch. The CLI refuses to add an entry
with a duplicate id.

### 1.2 `wall_clock_budget_seconds`

A fixed per-run wall-clock budget makes runs directly comparable
regardless of what the patches changed. Drift counts over 30 seconds
and 4 minutes aren't apples-to-apples; a fixed budget normalises the
denominator.

For multi-turn entries the budget covers the WHOLE conversation —
every turn the agent takes plus every turn the user (scripted or
emulated) injects, plus every per-turn LLM call by the goldfive
overlay. When the budget elapses, the adapter aborts the inner work
and emits a `goldfive.v1.RunAborted` with `reason="wall_clock_budget"`.
The reducer stamps `aborted=true` on the loss profile and the scoring
treats abort as worst-case for the entry (in particular, `pass_fail`
is `False` if the entry had an `expectation`; the drift loss
contribution is a large constant).

The budget is in seconds for ergonomics; subsecond timing is not
meaningful given goldfive's per-turn LLM-call latency floor.

### 1.3 `weight`

A multiplier applied to the entry's contributions in the aggregate
score. Default `1.0`. Setting `weight=2.0` on a critical entry roughly
doubles its influence on the generation score. Weights are advisory in
the sense that they don't change pass-rate semantics — pass-rate is
still computed as `(weighted passes) / (weighted entries)`, which a
weight of `2.0` raises a entry to "count twice" for.

### 1.4 `tags`

Operator labels. The pattern detectors slice by tag (e.g. "show me the
drift counts on `[hard, multi-turn]` entries only"). Tags also let the
rubric steer the proposer toward or away from certain slices. Tags
have no semantic meaning to zicato — they are operator strings.

Conventional tags worth adopting:

- `easy` / `medium` / `hard` — operator difficulty estimate
- `regression:{name}` — pinned regression tests
- `adversarial` — designed to provoke a specific failure mode

### 1.5 `context`

A free-form JSON object the adapter receives verbatim. The adapter
authors what keys mean — the ADK adapter might consume
`{"attachments": [...], "session_state": {...}}` to set up the run.
Zicato never reads it.

## 2. Per-kind fields

### 2.1 `single_turn`

The default kind. Carries one user message; the agent's final response
is the run result.

| Field | Type | Required | Meaning |
|---|---|---|---|
| `input` | `string` | yes | The raw user message handed to the inner harness. |

Example:

```json
{
  "id": "summarise_short_paper",
  "kind": "single_turn",
  "input": "Summarise this paper in three bullet points: ...",
  "wall_clock_budget_seconds": 180,
  "weight": 1.0,
  "tags": ["easy", "summarise"],
  "expectation": {
    "kind": "regex",
    "spec": "^- .+\\n- .+\\n- .+$",
    "reads": "final_output"
  }
}
```

The runner hands the entry's run to expectation evaluators as a
single `RunResult` (the same dataclass for every kind):

```python
@dataclass(frozen=True, slots=True)
class RunResult:
    run_id: str
    entry_id: str
    final_output: str           # the last assistant turn's user-facing text
    transcript: tuple[str, ...] # assistant turns only, in order
    runtime_ms: int
    aborted: bool = False
    abort_reason: str = ""
```

For a single-turn entry `transcript` is a length-1 tuple matching
`final_output`. The expectation on a single-turn entry defaults to
`reads: "final_output"` and is evaluated against the `final_output`
string. `Judge` process checks (§4) are unaffected by `reads` — they
watch the reasoning stream regardless of entry kind.

### 2.2 `multi_turn_scripted`

Replays a fixed transcript of user turns. The user side is
deterministic — `turns[i]` is injected verbatim on the i-th user turn,
regardless of what the agent said. Cheap, fast, deterministic.

| Field | Type | Required | Meaning |
|---|---|---|---|
| `turns` | `list[{user: string}]` | yes | The pre-scripted user turns. |
| `max_turns` | `int` | yes | Hard ceiling on the number of agent turns. Conversation ends when the script is exhausted OR `max_turns` is reached, whichever first. |

Example:

```json
{
  "id": "follow_up_clarification",
  "kind": "multi_turn_scripted",
  "turns": [
    {"user": "What are the three main findings?"},
    {"user": "Can you explain the second one in plain language?"},
    {"user": "Thanks. What does the paper recommend doing next?"}
  ],
  "max_turns": 6,
  "wall_clock_budget_seconds": 300,
  "tags": ["multi-turn", "clarification"]
}
```

The run is returned as the same flat `RunResult` (see §2.1); for a
multi-turn entry `transcript` is the agent's user-facing turns in
order (user turns are not included — the entry already carries the
scripted user turns) and `final_output` is the agent's last turn.

A transcript-scoped expectation is authored with
`reads="conversation_end"` (the `OutputScope.TRANSCRIPT` enum value)
and is evaluated against the whole transcript; see
[BOARD-AUTHORING.md](BOARD-AUTHORING.md) §2.2.

### 2.3 `multi_turn_emulated`

A `call_llm`-backed user agent plays the user. Each turn the emulator
sees the agent's user-facing output so far plus a persona and produces
the next user turn. Stops on the persona's `stop_when` OR `max_turns`.

| Field | Type | Required | Meaning |
|---|---|---|---|
| `user_persona` | `Persona` | yes | What the simulated user wants and how they behave. |
| `max_turns` | `int` | yes | Hard ceiling on conversation length. |

`Persona`:

| Field | Type | Required | Meaning |
|---|---|---|---|
| `goal` | `string` | yes | What this simulated user is trying to accomplish. |
| `constraints` | `string` | yes | A single free-text block describing what the simulated user will and will not say or do — tone, format, willingness to provide details. Not a list. |
| `stop_when` | `string` | yes | Condition the emulator checks each turn. When matched, the conversation ends. |

Example:

```json
{
  "id": "expert_review_dialog",
  "kind": "multi_turn_emulated",
  "user_persona": {
    "goal": "Get a thorough review of my paper's methodology section.",
    "constraints": "You are a domain expert, not a novice. You push back when the agent's review is shallow, and you ask one focused follow-up per turn, not five.",
    "stop_when": "The agent has addressed at least three concrete methodology issues."
  },
  "max_turns": 8,
  "wall_clock_budget_seconds": 600,
  "tags": ["multi-turn", "emulated", "expert"]
}
```

The run is returned as the same flat `RunResult` (see §2.1):
`transcript` holds the agent's user-facing turns only — the emulator's
user turns are kept separately for reducer use and are not in
`RunResult.transcript`.

The emulator is **collusion-proof by construction**. The detailed
construction — context-isolation rules, the two-callable rule, the
answer-leakage heuristic, the audit-trail spans on the
`zicato:emulator` lane — is in [EMULATOR.md](EMULATOR.md). The board
format here covers only the entry shape.

## 3. Outcome check: the `expectation` field

An entry's `expectation` is its **outcome** check: a single matcher
run post-hoc, after the run terminates, against the run's *product* —
the final output or the whole transcript. It is evaluated in the loss
reducer, which records its result as `pass_fail: bool` on the loss
profile. An entry with no `expectation` has `pass_fail = None` and
contributes to drift-loss only.

`expectation` is a **single object**, not a list — a board entry
carries at most one expectation. The two authoring namespaces that
build it are `Predicate` (deterministic) and `Rubric` (LLM-graded);
see [BOARD-AUTHORING.md](BOARD-AUTHORING.md) §2.

The expectation object has exactly three fields:

| Field | Type | Required | Meaning |
|---|---|---|---|
| `kind` | `ExpectationKind` enum | yes | One of the five values below. |
| `spec` | `string` | yes | A single string field, matcher-specific (see each kind). Carried as one string so the discriminated union round-trips through JSON without nested objects. |
| `reads` | `OutputScope` enum | no | Which slice of the run the matcher is evaluated against — `"final_output"` (the default) or `"conversation_end"`. Applies to every kind. |

### 3.1 `predicate`

A Python callable, addressed by dotted path. The callable receives the
`RunResult` and returns `bool`. Built with `Predicate.python(...)`.

The `spec` is a dotted import path with a **colon** separating the
module from the callable — `module.path:func` — exactly as the real
presentation board writes it:

```json
{
  "kind": "predicate",
  "spec": "zicato_examples.target_1_presentation.predicates:mentions_waffles",
  "reads": "final_output"
}
```

The path must resolve under the project's import path at run time.
Predicate bodies are NEVER serialized — only the dotted path is in the
JSON; the callable lives in the project's own source. This is
intentional: predicates can express arbitrary logic and shipping them
as JSON would invite injection.

### 3.2 `expected_text`

Exact-string match on the run result (or transcript end-text). Case-
sensitive, whitespace-significant. Built with `Predicate.contains(...)`.

```json
{
  "kind": "expected_text",
  "spec": "OK"
}
```

Useful for narrow regression tests where the agent's final reply is
known.

### 3.3 `regex`

A Python `re`-flavour regex on the run result. Built with
`Predicate.regex(...)`.

```json
{
  "kind": "regex",
  "spec": "^Final answer: \\d+\\.\\d+$"
}
```

The regex is compiled with `re.DOTALL` and matched with `re.search`
(not `re.match`) so it can find structure anywhere in the output. To
anchor, include `^` / `$` explicitly.

### 3.4 `json_schema`

A JSON-schema validation. The run result is parsed as JSON, then
validated against the schema. Schema-invalid → pass fails. Non-JSON
output → pass fails. Built with `Predicate.schema(...)`.

```json
{
  "kind": "json_schema",
  "spec": {
    "type": "object",
    "required": ["summary", "citations"],
    "properties": {
      "summary": {"type": "string"},
      "citations": {"type": "array", "items": {"type": "string"}}
    }
  }
}
```

Useful for structured-output agents where the contract is JSON.

### 3.5 `rubric`

An LLM-graded outcome check. The grader reads the output (or
transcript), scores it on a numeric scale against an operator-supplied
criterion, and the expectation passes iff the score meets a
threshold. Built with `Rubric.score(...)`.

```json
{
  "kind": "rubric",
  "spec": "{\"rubric\":\"Each bullet is accurate and non-redundant.\",\"scale\":[0.0,10.0],\"threshold\":7.0}",
  "reads": "final_output"
}
```

The `spec` is a JSON document (encoded as a string, like every
`spec`) carrying the rubric criterion text, the `scale` `(lo, hi)`
bounds, and the `threshold` (`null` for an advisory rubric that
always passes and records its score for inspection). The `reads`
field is an `OutputScope` enum value — `"final_output"` (grade the
final output) or `"conversation_end"` (grade the whole transcript).

The grader runs through **`auxiliary_call_llm`**, never the harness
callable — the model grading the output must not be the model that
produced it. This is enforced; see [EMULATOR.md](EMULATOR.md).

A `rubric` is still an **outcome** check — it reads a finished
product. The in-run check that watches the *reasoning* is the `Judge`
(§4), a distinct concept. The `Rubric.score()` factory was previously
named `Rubric.judge()`; it was renamed so "judge" means only the
process check.

### 3.6 `reads` semantics

`reads` selects which slice of the run the expectation matches
against. It is an `OutputScope` enum value and applies to every
expectation kind (there is no separate `fires_on` field — `reads` was
formerly spelled `fires_on`; the loader still accepts the old key on
input for boards mid-migration, but the canonical key is `reads`).

| Value (`OutputScope`) | Single-turn | Multi-turn |
|---|---|---|
| `"final_output"` (`OutputScope.FINAL`) | The agent's final reply string. | The agent's reply on the LAST turn only. |
| `"conversation_end"` (`OutputScope.TRANSCRIPT`) | (Not valid; rejected by `BoardEntry.validate` — a single-turn entry cannot read the full transcript.) | The whole transcript. |

`reads` defaults to `"final_output"`. There is no per-kind default at
the schema level: the field always defaults to `"final_output"`, and
operators set `"conversation_end"` explicitly on a multi-turn entry
whose contract spans turns. Setting `"conversation_end"` on a
single-turn entry is rejected at validation time.

## 4. Process checks: the `judges` list

An entry's `judges` are its **process** checks: goldfive judges that
watch the agent's *reasoning stream* while the run is in flight.
Where an outcome check inspects the finished product, a process check
inspects *how the agent got there* — ordering, causality, intermediate
steps.

A process check is authored with the `Judge` namespace (see
[BOARD-AUTHORING.md](BOARD-AUTHORING.md) §3). It is a goldfive-side
judge: goldfive evaluates the criterion against the live event
stream, and a violation emits a drift event that flows into the run's
`LossProfile` exactly like any built-in drift.

Every judge object has exactly four fields:

| Field | Type | Required | Meaning |
|---|---|---|---|
| `name` | `string` | yes | Stable, board-unique, slug-like identifier (lowercase alphanumerics, underscores, hyphens; starts with an alphanumeric). Becomes goldfive's `judge_name`; `ScoringWeights.per_judge_weights` keys on it. |
| `mode` | `JudgeMode` enum | yes | `"inline"` (natural-language criterion) or `"python"` (dotted path to a process-judge callable). |
| `body` | `string` | yes | The criterion text when `mode` is `"inline"`; a dotted import path to a Python process-judge callable when `mode` is `"python"`. The Python body itself lives in project source, never in the board JSON. |
| `severity` | `DriftSeverity` enum | yes | `"info"` / `"warning"` / `"critical"` — the goldfive severity an adverse verdict is reported at; controls how heavily a violation weighs in the drift loss. |

There is no `kind` / `criterion` / `dotted_path` field on a judge —
the discriminator is `mode` and the criterion-or-path is `body`.

An `inline`-mode judge, as it appears on the wire:

```json
{
  "name": "cite_before_metric",
  "mode": "inline",
  "body": "The agent must cite a source before stating any market metric.",
  "severity": "warning"
}
```

### 4.1 What a violated judge emits

A violated judge (either mode) emits a drift event of kind `custom`
(`DriftKind.CUSTOM`), **identified by the judge's `name`** carried as
`judge_name`. The reducer attributes it under `custom:<judge_name>` in
the run's `drift_counts`, and keys the per-judge breakdown on
`judge_name`. Because every custom judge emits the same `custom` drift
kind, the `judge_name` is the discriminator — two judges on a board
are told apart by name, not by kind. This is why `name` must be stable
and board-unique.

The emit path is specified in [ARCHITECTURE.md](ARCHITECTURE.md)
§4.6.1 and [TELEMETRY.md](TELEMETRY.md).

### 4.2 Built-in judges and `disable_drift`

goldfive ships its own judges — the detectors behind the
`confabulation_risk`, `looping_reasoning`, and the rest of the
`DriftKind` taxonomy. They are **ambient and default-on**: every run
is watched by them regardless of the entry's `judges` list. An entry's
`judges` *adds* custom judges on top of the ambient set.

A board suppresses a built-in judge with the board-level
`disable_drift` setting — a list of `goldfive.DriftKind` **wire
tokens** (short lowercase strings like `"user_steer"`,
`"confabulation_risk"`, `"looping_reasoning"`) whose detectors are
turned off for every entry on the board. The tokens are the bare
enum values, not a `DRIFT_KIND_*` constant form. It is a **board-wide**
setting carried on the `board_meta` header line (§1.0 / §10), not a
per-entry field, and it suppresses **built-ins by kind only** — custom
judges are removed by deleting them from `judges`, never via
`disable_drift`. Changing `disable_drift` changes which signals score
the board, so it is part of the evaluation contract and rolls the
epoch (see [EPOCHS-AND-JOURNALING.md](EPOCHS-AND-JOURNALING.md) §10).

## 5. Wall-clock budget semantics

The budget applies to the WHOLE entry. Specifically:

- **Single-turn:** wall-clock from `goldfive.v1.RunStarted` to the
  terminal event (`RunCompleted` or `RunAborted`).
- **Multi-turn scripted:** wall-clock from the first agent turn's
  invocation start to the terminal event of the run that hosts the
  whole conversation. The script is replayed on the same run id.
- **Multi-turn emulated:** wall-clock from the first agent turn's
  invocation start to the terminal event. The emulator's per-turn
  `auxiliary_call_llm` time is included in the budget — slow
  emulators consume budget that the agent could have used.

When the budget elapses mid-run, the adapter is responsible for:

1. Cancelling the in-flight agent invocation cooperatively (via the
   goldfive cancellation path, see goldfive's CANCELLATION-CONTRACT.md
   if the operator wants the deep version).
2. Emitting a final `goldfive.v1.RunAborted` with
   `reason="wall_clock_budget"`.

The reducer treats `aborted` runs as worst-case. The exact loss
contribution is in [SCORING.md](SCORING.md).

## 6. Tag-based pattern slicing

Pattern detectors slice loss profiles by tag. A few examples:

- `drift_concentration_by_kind` produces patterns like "
  `confabulation_risk` fires 14 times across entries tagged
  `[multi-turn]` and 0 times on entries tagged `[single-turn]`".
- `tag_slice_regression` produces patterns like "pass-rate on entries
  tagged `[hard]` dropped from 0.75 to 0.5 between v3 and v4".
- `multi_turn_memory_failure` produces patterns like "agent re-asked
  the user's name on 4 of 6 entries tagged `[multi-turn,
  long-conversation]`".

The detector set is open-ended. Tags are the operator's lever for
making the proposer attend to specific slices of the board.

## 7. Forward-compatibility: the `kind` discriminator

`kind` is a closed `Literal` (`BoardEntryKind`), not a bare string —
but it is designed to extend without a schema break. The v0 *runtime*
set is `{"single_turn", "multi_turn_scripted", "multi_turn_emulated"}`.
Two further tokens — `"synthetic_adversarial"` and `"synthetic_clean"`
— are already **reserved in the type today** as forward-compat slots
(planned for target 2; see below) so that adding them to the runtime
later does not require a schema bump for existing operators. Bringing a
reserved kind online is a matter of:

1. Wiring the kind's discriminant fields into the runner.
2. Providing the run path that produces its `RunResult`.
3. Telling the loss reducer how to map it to a `LossProfile`.

This matters because the **target 2 dogfood** (goldfive's steering
layer — see [DOGFOOD-TARGETS.md](DOGFOOD-TARGETS.md)) motivates the two
reserved kinds. They are **planned**, not yet wired into the runner —
but `BoardEntry.validate` already enforces their discriminant fields:

- `synthetic_adversarial` (planned) — a known-bad inner harness wired
  in, where the contract is "the steerer fires the right drift in
  time". The entry carries an `adversarial_agent_spec` (dotted path to
  the known-bad agent) and a non-empty `required_drift_kinds` list.
  Pass = the required drift detected; fail = drift missed.
- `synthetic_clean` (planned) — a known-good inner harness wired in,
  where the contract is "no spurious drift fires". Pass = no
  false-positive drift; fail = drift fired when none was warranted.

Both kinds use the same envelope (id, wall-clock budget, weight, tags,
`expectation`, `judges`, context) plus their per-kind discriminant
fields above.

The forward-compat property holds because:

- The reserved tokens are already in the `BoardEntryKind` `Literal`
  and validated by `BoardEntry.validate`, so adding them to the
  runtime is not a schema break.
- The expectation kinds (`predicate`, `rubric`, …) can express
  arbitrary matching logic, so new entry kinds don't need new
  expectation kinds.
- The single flat `RunResult` carries both `final_output` and the full
  `transcript`, so the loss reducer can map any kind to a
  `LossProfile` from one shape.

When the reserved kinds are brought online, this document grows a
§2.4 / §2.5; the schema does not break.

## 8. Validation

`zicato board add ENTRY_PATH` appends **one** validated board entry
read from a JSON file to the current epoch's `board.jsonl`. Validation
is eager; failures are noisy errors, not silent drops. The validator
(`BoardEntry.validate` / `validate_board_entry`) checks:

1. `id` is present; `wall_clock_budget_seconds` is `> 0`; `weight` is
   `>= 0`.
2. `kind` is one of the recognised tokens.
3. Per-kind discriminant fields are present and the wrong-kind fields
   are absent (e.g. a `single_turn` entry must set `input` and must
   not set `turns` / `user_persona`).
4. The `expectation`, if any, has a recognised `kind`, a `spec`, and a
   `reads` value that is valid for the entry kind (a single-turn entry
   may not read the full transcript).
5. Each judge in `judges`, if any, has a stable board-unique slug
   `name`, a recognised `mode` (`inline` / `python`), a `body`, and a
   `severity` that resolves to a `DriftSeverity` member.
6. Any `disable_drift` token on the `board_meta` header resolves to a
   `DriftKind` member.

`zicato board list` walks the board and re-validates as it renders.

`zicato board remove ENTRY_ID` removes the entry with that id from the
current epoch's board.

Neither `board add` nor `board remove` takes a `--force` flag (their
only option is `--workspace`). Mid-epoch contract protection is NOT a
flag on the `board` group — see §9.

## 9. Editing the board mid-epoch

The epoch's evaluation contract includes the board; changing the board
changes the contract. Protection is enforced by **`evolve`'s
contract-hash auto-epoching**, not by a `--force` flag on the `board`
subcommands:

- `evolve` resolves the evaluation contract (board + proposer brief +
  scoring + the registered inner-harness identity), hashes it, and
  compares it to the current epoch. When the hash has drifted — for
  instance because you ran `board add` / `board remove`, or hand-edited
  `board.jsonl` — `evolve` **closes the current epoch and opens a fresh
  one** before running. You do not run `epoch new` by hand.
- Pass `--no-auto-epoch` to make a drifted contract a hard error
  instead of rolling the epoch, or `--epoch ID` to pin an explicit
  epoch and skip the check.

So the workflow is simply: edit the board (via `board add` / `remove`
or by hand), then run `zicato evolve`; it notices the contract changed
and rolls the epoch for you. See
[EPOCHS-AND-JOURNALING.md](EPOCHS-AND-JOURNALING.md) for why epochs are
designed this way.

## 10. Example board

A small but realistic board for the presentation-agent dogfood. It
opens with a `board_meta` header line, then five entries. Two entries
carry an `expectation`; one also carries a `judges` list (its
`cite_before_cost` process check):

```json
{"board_meta": true, "disable_drift": ["user_steer", "user_pause"]}
{"id":"short_solar","kind":"single_turn","input":"Make a 3-slide presentation about solar panels.","wall_clock_budget_seconds":180,"tags":["easy","presentation","short"]}
{"id":"long_solar_with_constraints","kind":"single_turn","input":"Make a 15-slide presentation about solar panels for a non-technical audience.","wall_clock_budget_seconds":300,"weight":1.5,"tags":["medium","presentation","long","audience"],"expectation":{"kind":"rubric","spec":"{\"rubric\":\"Accessible to a non-technical audience.\",\"scale\":[0.0,10.0],\"threshold\":7.0}","reads":"final_output"},"judges":[{"name":"cite_before_cost","mode":"inline","body":"The agent must cite a source before stating a cost figure.","severity":"warning"}]}
{"id":"contradictory_brief","kind":"single_turn","input":"Make a presentation about solar panels that is both very technical and accessible to grade-school children.","wall_clock_budget_seconds":300,"tags":["hard","ambiguous"]}
{"id":"revision_dialog","kind":"multi_turn_scripted","turns":[{"user":"Make a presentation about solar panels."},{"user":"Add a slide about cost."},{"user":"Now make slide 3 less technical."}],"max_turns":6,"wall_clock_budget_seconds":480,"tags":["multi-turn","revision","presentation"],"expectation":{"kind":"rubric","spec":"{\"rubric\":\"Every requested revision was applied.\",\"scale\":[0.0,10.0],\"threshold\":7.0}","reads":"conversation_end"}}
{"id":"expert_review","kind":"multi_turn_emulated","user_persona":{"goal":"Get feedback on a presentation outline you wrote.","constraints":"You are a domain expert. Push back when the agent's feedback is shallow.","stop_when":"The agent has given at least three concrete improvements."},"max_turns":8,"wall_clock_budget_seconds":600,"tags":["multi-turn","emulated","expert"]}
```

That's a header line plus 5 entries: 3 single-turn, 1 scripted
multi-turn, 1 emulated multi-turn. A real first epoch usually has
20-50. The Python builder
([BOARD-AUTHORING.md](BOARD-AUTHORING.md) §4) is the ergonomic way to
produce a board this shape.

## 11. Cross-references

| Topic | Document |
|---|---|
| Practical authoring — outcome vs process, builder, weights | [BOARD-AUTHORING.md](BOARD-AUTHORING.md) |
| Loss profile fields written from entry runs | [TELEMETRY.md](TELEMETRY.md) |
| How `weight`, the `expectation`, and `judges` enter the score | [SCORING.md](SCORING.md) |
| Emulator collusion-proofing for multi-turn emulated | [EMULATOR.md](EMULATOR.md) |
| Why entries can't be edited mid-epoch | [EPOCHS-AND-JOURNALING.md](EPOCHS-AND-JOURNALING.md) |
| Future entry kinds for target 2 | [DOGFOOD-TARGETS.md](DOGFOOD-TARGETS.md) |
