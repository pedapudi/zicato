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
- The five expectation kinds.
- Wall-clock budget semantics, weight, tags, and tag-based pattern
  slicing.
- The forward-compatibility story for new entry kinds.

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
| `expectation` | `Expectation` | no | Optional matcher. Absent → drift-loss-only scoring for this entry. See §3. |
| `context` | `object` | no | Opaque adapter-specific metadata. ADK adapters might use `{"attachments": [...], "session_state": {...}}`. Zicato never interprets the contents. |

Plus the per-kind fields in §2.

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
treats abort as worst-case for the entry (in particular, the
`pass_fail` is `False` if an expectation was set; the drift loss
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
    "fires_on": "final_output"
  }
}
```

The `RunResult` for a single-turn entry is:

```python
@dataclass
class SingleTurnRunResult:
    kind: Literal["single_turn"] = "single_turn"
    final_output: str
    aborted: bool
    abort_reason: str | None
```

Expectations on single-turn entries default to `fires_on:
"final_output"` and receive the `final_output` string.

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

The `RunResult` for a multi-turn scripted entry is:

```python
@dataclass
class MultiTurnScriptedRunResult:
    kind: Literal["multi_turn_scripted"] = "multi_turn_scripted"
    transcript: list[Turn]   # alternating user / agent
    aborted: bool
    abort_reason: str | None
    stopped_reason: Literal["script_exhausted", "max_turns", "abort"]
```

Expectations default to `fires_on: "conversation_end"` and receive the
whole transcript.

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
| `constraints` | `list[string]` | no | Behavioural rules ("you are impatient", "you mistype words sometimes"). |
| `stop_when` | `string` | yes | Condition the emulator checks each turn. When matched, the conversation ends. |

Example:

```json
{
  "id": "expert_review_dialog",
  "kind": "multi_turn_emulated",
  "user_persona": {
    "goal": "Get a thorough review of my paper's methodology section.",
    "constraints": [
      "You are a domain expert, not a novice.",
      "You push back when the agent's review is shallow.",
      "You ask one focused follow-up per turn, not five."
    ],
    "stop_when": "The agent has addressed at least three concrete methodology issues."
  },
  "max_turns": 8,
  "wall_clock_budget_seconds": 600,
  "tags": ["multi-turn", "emulated", "expert"]
}
```

The `RunResult` for a multi-turn emulated entry is:

```python
@dataclass
class MultiTurnEmulatedRunResult:
    kind: Literal["multi_turn_emulated"] = "multi_turn_emulated"
    transcript: list[Turn]   # alternating user / agent
    aborted: bool
    abort_reason: str | None
    stopped_reason: Literal["stop_when", "max_turns", "abort"]
    persona_hash: str        # sha256 of the canonicalized persona JSON
```

The emulator is **collusion-proof by construction**. The detailed
construction — context-isolation rules, the two-callable rule, the
answer-leakage heuristic, the audit-trail spans on the
`zicato:emulator` lane — is in [EMULATOR.md](EMULATOR.md). The board
format here covers only the entry shape.

## 3. Expectation kinds

An expectation is an optional matcher. When present, the loss reducer
runs it against the appropriate slice of the run result and stamps
`pass_fail: bool` on the loss profile. When absent, `pass_fail` is
`None` and the entry contributes to drift-loss only.

All expectations share:

| Field | Type | Required | Meaning |
|---|---|---|---|
| `kind` | `string` | yes | One of the five values below. |
| `spec` | varies | yes | Matcher-specific (see each kind). |
| `fires_on` | `string` | no | `"final_output"` (single-turn default) or `"conversation_end"` (multi-turn default). |

### 3.1 `predicate`

A Python callable, addressed by dotted path. The callable receives the
`RunResult` (typed by entry kind) and returns `bool`.

```json
{
  "kind": "predicate",
  "spec": "myproj.predicates.three_bullets_about_solar_panels"
}
```

The path must resolve under the project's import path at run time.
Predicate bodies are NEVER serialized — they live in the project's
own source. This is intentional: predicates can express arbitrary
logic and shipping them as JSON would invite injection.

### 3.2 `expected_text`

Exact-string match on the run result (or transcript end-text). Case-
sensitive, whitespace-significant.

```json
{
  "kind": "expected_text",
  "spec": "OK"
}
```

Useful for narrow regression tests where the agent's final reply is
known.

### 3.3 `regex`

A Python `re`-flavour regex on the run result.

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
output → pass fails.

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

### 3.5 `judge`

A judge model. Like `predicate` but the callable goes through the
`auxiliary_call_llm`. The spec is a dotted path to a Python function
that constructs the judge prompt:

```json
{
  "kind": "judge",
  "spec": "myproj.judges.is_summary_accurate"
}
```

The function receives the `RunResult` and returns a tuple
`(system_prompt: str, user_prompt: str, expected_yes: bool)`. The
reducer calls `auxiliary_call_llm(system_prompt, user_prompt,
model=judge_model)` and checks whether the response (case-insensitive,
first-token) is `"yes"` or `"no"`, comparing against `expected_yes`.

Judge expectations use the **auxiliary** callable, never the harness
callable. This is enforced — see [EMULATOR.md](EMULATOR.md).

### 3.6 `fires_on` semantics

| Value | Single-turn | Multi-turn |
|---|---|---|
| `"final_output"` | The agent's final reply string. | The agent's reply on the LAST turn only. |
| `"conversation_end"` | (Not valid; rejected at `board add`.) | The whole transcript (joined with `\n` between turns). |

The defaults are sensible: single-turn entries fire on
`final_output`; multi-turn entries fire on `conversation_end`.
Operators override only when the contract is unusual (e.g. a
multi-turn entry where only the last reply is meant to satisfy the
schema).

## 4. Wall-clock budget semantics

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

## 5. Tag-based pattern slicing

Pattern detectors slice loss profiles by tag. A few examples:

- `drift_concentration_by_kind` produces patterns like "
  `DRIFT_KIND_CONFABULATION_RISK` fires 14 times across entries tagged
  `[multi-turn]` and 0 times on entries tagged `[single-turn]`".
- `tag_slice_regression` produces patterns like "pass-rate on entries
  tagged `[hard]` dropped from 0.75 to 0.5 between v3 and v4".
- `multi_turn_memory_failure` produces patterns like "agent re-asked
  the user's name on 4 of 6 entries tagged `[multi-turn,
  long-conversation]`".

The detector set is open-ended. Tags are the operator's lever for
making the proposer attend to specific slices of the board.

## 6. Forward-compatibility: open-ended `kind`

`kind` is a string, not an enum. The v0 registered set is
`{"single_turn", "multi_turn_scripted", "multi_turn_emulated"}`.
Adding a new kind is a matter of:

1. Registering the new string in zicato's entry-kind registry.
2. Providing a `RunResult` shape for it.
3. Telling the loss reducer how to map it to a `LossProfile`.

This matters because the **target 2 dogfood** (goldfive's steering
layer — see [DOGFOOD-TARGETS.md](DOGFOOD-TARGETS.md)) needs entry
kinds zicato does not ship in v0:

- `synthetic_adversarial` — a known-bad inner harness wired in,
  where the expectation is "the steerer fires the right drift in time".
  Pass = drift detected; fail = drift missed.
- `synthetic_clean` — a known-good inner harness wired in, where the
  expectation is "no spurious drift fires". Pass = no false-positive
  drift; fail = drift fired when none was warranted.

Both kinds will use the same envelope (id, wall-clock budget, weight,
tags, expectation, context) and add per-kind fields specifying the
synthetic harness wiring.

The forward-compat property holds because:

- The discriminator is a string.
- The expectation kinds (predicate, judge) can express arbitrary
  matching logic, so new entry kinds don't need new expectation
  kinds.
- The `RunResult` is typed per kind; the loss reducer dispatches on
  the kind to map to a `LossProfile`.

When the new kinds land, this document grows a §2.4 / §2.5; the
v0 schema does not break.

## 7. Validation

`zicato board add` validates the entry eagerly. Validation failures
are noisy errors, not silent drops. The validator checks:

1. `id` is filesystem-safe and unique within the board.
2. `kind` is in the registered set.
3. `wall_clock_budget_seconds` is a positive number.
4. Per-kind fields are present and correctly shaped.
5. `expectation`, if present, has a recognised `kind` and a
   `spec` that the kind accepts.
6. `tags` is a list of strings, no duplicates.

`zicato board list` walks the board and re-validates as it renders.

`zicato board remove <id>` removes the line by id. Removing entries
mid-epoch closes the epoch implicitly (the contract changed); the CLI
refuses without `--force`.

## 8. Editing the board mid-epoch

Don't. The epoch's evaluation contract includes the board; changing
the board changes the contract. The CLI enforces this:

- `zicato board add` / `remove` mid-epoch require `--force` AND emit
  a warning that the operator should `zicato epoch new` first.
- Even with `--force`, the action invalidates the in-progress
  patterns and the round counter, which the CLI surfaces in the
  warning.

The right workflow is: close the current epoch (manually or by
starting a new one), edit the board on the new epoch, run rounds
there. See [EPOCHS-AND-JOURNALING.md](EPOCHS-AND-JOURNALING.md) for
why epochs are designed this way.

## 9. Example board

A small but realistic board for the presentation-agent dogfood:

```json
{"id":"short_solar","kind":"single_turn","input":"Make a 3-slide presentation about solar panels.","wall_clock_budget_seconds":180,"tags":["easy","presentation","short"]}
{"id":"long_solar_with_constraints","kind":"single_turn","input":"Make a 15-slide presentation about solar panels for a non-technical audience.","wall_clock_budget_seconds":300,"weight":1.5,"tags":["medium","presentation","long","audience"]}
{"id":"contradictory_brief","kind":"single_turn","input":"Make a presentation about solar panels that is both very technical and accessible to grade-school children.","wall_clock_budget_seconds":300,"tags":["hard","ambiguous"]}
{"id":"revision_dialog","kind":"multi_turn_scripted","turns":[{"user":"Make a presentation about solar panels."},{"user":"Add a slide about cost."},{"user":"Now make slide 3 less technical."}],"max_turns":6,"wall_clock_budget_seconds":480,"tags":["multi-turn","revision","presentation"]}
{"id":"expert_review","kind":"multi_turn_emulated","user_persona":{"goal":"Get feedback on a presentation outline you wrote.","constraints":["You are a domain expert.","Push back when the agent's feedback is shallow."],"stop_when":"The agent has given at least three concrete improvements."},"max_turns":8,"wall_clock_budget_seconds":600,"tags":["multi-turn","emulated","expert"]}
```

That's 5 entries: 3 single-turn, 1 scripted multi-turn, 1 emulated
multi-turn. A real first epoch usually has 20-50.

## 10. Cross-references

| Topic | Document |
|---|---|
| Loss profile fields written from entry runs | [TELEMETRY.md](TELEMETRY.md) |
| How `weight` and `pass_fail` enter the score | [SCORING.md](SCORING.md) |
| Emulator collusion-proofing for multi-turn emulated | [EMULATOR.md](EMULATOR.md) |
| Why entries can't be edited mid-epoch | [EPOCHS-AND-JOURNALING.md](EPOCHS-AND-JOURNALING.md) |
| Future entry kinds for target 2 | [DOGFOOD-TARGETS.md](DOGFOOD-TARGETS.md) |
