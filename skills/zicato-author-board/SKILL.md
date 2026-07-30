---
name: zicato-author-board
description: Write or extend a zicato board.jsonl (the per-epoch evaluation contract) — entry kinds, the single expectation, judges, the board_meta/disable_drift header, the emulator two-callable rule. Use when adding tasks, expectations, or process judges to a zicato target, or hand-editing a frozen board.
---

# Authoring a zicato `board.jsonl`

A **board** is the per-epoch list of tasks the inner harness is scored against.
It is one JSONL file: an optional `board_meta` header line followed by one
entry per line. The board is part of the **evaluation contract** — changing it
rolls the epoch (see "Mid-epoch protection" below).

Live location: `board.jsonl` sits next to the workspace (alongside `brief.md`
and `scoring.json`); `evolve` reads it from there and freezes a per-epoch copy
under `.zicato/epochs/{epoch_id}/board.jsonl`. Sibling skills:
`zicato-write-brief`, `zicato-tune-scoring`. Schema docs:
[BOARD-FORMAT.md](../../docs/design/BOARD-FORMAT.md),
[BOARD-AUTHORING.md](../../docs/design/BOARD-AUTHORING.md),
[EMULATOR.md](../../docs/design/EMULATOR.md).

> The real on-disk schema is canonical. Some prose in BOARD-FORMAT.md is stale:
> the field is **`expectation`** (singular, one object) — NOT `expectations`
> (list); judges use **`mode`/`body`** — NOT `kind`/`criterion`; `disable_drift`
> tokens are short lowercase goldfive `DriftKind` values like `user_steer` —
> NOT `DRIFT_KIND_*`. Mirror the working example, not the prose.

## The `board_meta` header (optional, must be line 1)

```jsonc
{"board_meta": true, "disable_drift": ["user_steer", "user_pause"], "judge_only": true}
```

The header carries exactly two keys. `judge_only: true` keeps goldfive *judging*
but turns off all steering — reach for it to measure the bare, un-steered
harness; it is emitted only when true, so a board that predates it is
byte-identical.

`disable_drift` is a board-wide list of goldfive `DriftKind` tokens whose
built-in detectors are turned off for every entry. Valid tokens (short,
lowercase): `tool_error`, `agent_refusal`, `plan_divergence`, `user_steer`,
`user_cancel`, `user_pause`, `context_pressure`, `wrong_agent`, `off_topic`,
`looping_tool_call`, `looping_reasoning`, `intent_divergence`,
`confabulation_risk`, `capability_mismatch`, `human_intervention_required`,
`custom`, … (an unknown token is a hard validation error that lists the valid
set). Custom judges are removed by deleting them from `judges`, never via
`disable_drift`. Omit the header entirely when both keys are at their defaults.

## Common envelope (every entry)

| Field | Type | Required | Meaning |
|---|---|---|---|
| `id` | string | yes | Stable, filesystem-safe, unique on the board. Never rename/reuse within an epoch. |
| `kind` | string | yes | `single_turn` \| `multi_turn_scripted` \| `multi_turn_emulated`. |
| `wall_clock_budget_seconds` | int (>0) | yes | Hard ceiling on the whole entry; exceeding it aborts and scores worst-case. |
| `weight` | float | no (1.0) | Relative importance in aggregation. |
| `tags` | string[] | no (`[]`) | Operator labels; pattern detectors slice by tag. |
| `context` | object (string vals) | no | Opaque adapter metadata; zicato never interprets it. |
| `expectation` | object \| absent | no | One OUTCOME check. Absent → drift-loss-only scoring. |
| `judges` | object[] | no (`[]`) | PROCESS checks watched in-run. |

## The three entry kinds

```jsonc
// single_turn — one user message; the agent's final reply is the result
{"id": "waffles_single", "kind": "single_turn", "wall_clock_budget_seconds": 180, "weight": 1.0,
 "tags": ["single_turn", "topic_waffles", "smoke"],
 "input": "Make a presentation about waffles.",
 "expectation": {"kind": "predicate", "spec": "zicato_examples.target_1_presentation.predicates:mentions_waffles", "reads": "final_output"}}
```

```jsonc
// multi_turn_scripted — fixed user turns replayed verbatim; cheap & deterministic
{"id": "waffles_revision_scripted", "kind": "multi_turn_scripted", "wall_clock_budget_seconds": 240,
 "tags": ["multi_turn_scripted", "topic_waffles", "revision"],
 "turns": [{"user": "Make a presentation about waffles."},
           {"user": "Add a slide about Belgian vs American waffles."},
           {"user": "Now move the history slide to be first."}],
 "max_turns": 3}
```

```jsonc
// multi_turn_emulated — an aux-LLM-backed user plays the persona
{"id": "picky_stakeholder_emulated", "kind": "multi_turn_emulated", "wall_clock_budget_seconds": 360, "weight": 1.5,
 "tags": ["multi_turn_emulated", "topic_metrics", "persona_picky"],
 "user_persona": {
   "goal": "Get a Q3 metrics deck for the board meeting you are willing to present without further changes.",
   "constraints": "Be polite but exacting. Push back on vague slides, demand concrete numbers. Never tell the agent the exact answer you want.",
   "stop_when": "the agent produces a revised deck that addresses your latest round of feedback."},
 "max_turns": 5,
 "expectation": {"kind": "predicate", "spec": "zicato_examples.target_1_presentation.predicates:addressed_picky_feedback", "reads": "conversation_end"},
 "judges": [
   {"name": "incorporates_feedback", "mode": "inline", "body": "Each revision visibly responds to the stakeholder's most recent feedback.", "severity": "warning"},
   {"name": "no_fabricated_numbers", "mode": "inline", "body": "The agent never invents metric values it was not given; it asks instead of fabricating.", "severity": "critical"}]}
```

`user_persona.constraints` is a single **string**, not a list. `goal`,
`constraints`, `stop_when` are all required.

## `expectation` — the one OUTCOME check

Singular: zero or one per entry. Fields: `kind`, `spec`, `reads`.
`reads` is `"final_output"` (single-turn default) or `"conversation_end"`
(whole transcript; a `single_turn` entry reading `conversation_end` is
rejected). The five `kind`s:

| `kind` | `spec` | Notes |
|---|---|---|
| `predicate` | `"module.path:func"` (colon before the function) | Callable `(run_result) -> bool`; body lives in project source, never in JSON. May instead be a **scorer** returning a `float` in `[0,1]` or a `(float, metrics)` 2-tuple (e.g. `{"precision": .., "recall": ..}`) for a continuous per-entry score — see "Continuous scores" below. |
| `expected_text` | exact substring | Case- and whitespace-significant. |
| `regex` | a `re` pattern | Matched with `re.search`; anchor with `^`/`$`. |
| `json_schema` | inline JSON Schema | Output is parsed as JSON then validated. |
| `rubric` | JSON string `{"rubric": <text>, "threshold": <float\|null>, "scale": [lo, hi]}` | LLM-graded via the auxiliary callable; `threshold: null` = advisory (always passes, records score). |

```jsonc
{"kind": "regex", "spec": "(?is)slide\\s+\\d", "reads": "final_output"}
```

Predicates feed the **pass-rate** dimension; see `zicato-tune-scoring`.

### Continuous scores (the `predicate` seam is also the scorer seam)

A `predicate` callable need not return `bool`. The same dotted-path seam
accepts a **scorer** returning a continuous per-entry score
(`board/matchers.py:_predicate_outcome_to_result`):

- `bool` — the historical binary verdict. `score` is left `None`; the reducer
  derives `1.0`/`0.0` from `passed`, so a binary board is byte-identical to
  before.
- `float` (or `int`) — a continuous score, clamped to `[0,1]` and recorded as
  `ExpectationResult.score`. `passed` becomes a display-only `score > 0.0` —
  the scalar and gate run on the continuous score, not this bit. A `NaN` scores
  `0.0`.
- `(float, metrics)` 2-tuple — the float is the score; the mapping (e.g.
  `{"precision": .., "recall": ..}`) is recorded verbatim as
  `ExpectationResult.metrics` and carried out to `loss.json` for the proposer's
  failure-mode profile. Any other return shape (including a `(bool, metrics)`
  tuple) is a contract error that fails the entry with a descriptive `detail`.

The score and `metrics` land on the run's `loss.json` (see
`zicato-read-telemetry`). The body lives in project source, exactly like a
boolean predicate; only the *return shape* changes. The weighting/formula over
the score is `zicato-tune-scoring`.

## `judges` — PROCESS checks (in-run)

A tuple of process judges watching the reasoning stream live. A violation emits
a goldfive `custom` drift identified by the judge `name` and feeds the
**drift-loss** side (NOT pass-rate). Fields:

- `name` — stable slug (lowercase alphanumerics, `_`, `-`); board-unique. This
  is goldfive's `judge_name` and the key `per_judge_weights` uses in
  `scoring.json` (see `zicato-tune-scoring`).
- `mode` — `inline` (`body` is a natural-language criterion) or `python`
  (`body` is a dotted import path to a judge callable).
- `body` — the criterion text or the dotted path, per `mode`.
- `severity` — `info` \| `warning` \| `critical` (goldfive `DriftSeverity`).

A `python` judge that grades *what the agent did* must read the real tool-call
ledger — goldfive's `ctx.session_state.recent_events` (`kind ==
"tool_observed"`: `tool_name` / `result_preview` / `error_message`) — NOT the
model's reasoning narration (`ctx.reasoning_text` / the transcript): custom
judges fire only at reasoning observation points, so the narration is the story
the agent told, not the tool round-trips it ran. Worked example:
`examples/zicato_examples/target_1_presentation/judges.py` (`file_findability`).
See `zicato-design-judges`.

## The emulator two-callable rule

`multi_turn_emulated` entries run the user-emulator on the **auxiliary**
callable (`--auxiliary-call-llm`), never the inner-harness callable
(`--harness-call-llm`). zicato hard-errors at startup if the two are the same
object (`assert_distinct_callables`, identity `is` check) — sharing one risks
the emulator and harness colluding through shared state. The persona is the
only runtime input the emulator sees; harness internals and the entry's
expectation are withheld by construction. Always wire two distinct callables.

## Validate

```sh
PY=/home/sunil/git/zicato/.venv/bin/python
# List + re-validate every entry in the current epoch's board:
$PY -m zicato.cli board list --workspace .zicato
# Append ONE entry from a JSON file (validated eagerly; rejects bad/duplicate id):
$PY -m zicato.cli board add  --workspace .zicato path/to/entry.json
# Remove by id:
$PY -m zicato.cli board remove --workspace .zicato <entry_id>
```

`board add` takes a path to a JSON file (one entry), not inline JSON. There is
no `--force` flag on these commands.

## Mid-epoch protection — prefer a new epoch

The board is frozen per epoch. Do not hand-edit a live board mid-epoch:
changing it changes the evaluation contract and degrades the in-progress
pattern history and round counter. The supported path is to let `evolve`
auto-roll — it hashes the contract (board + brief + scoring + harness identity)
and, when it has drifted, closes the current epoch and opens a fresh one before
running (`--no-auto-epoch` to error on drift instead). To roll deliberately,
edit the live `board.jsonl` then run `evolve`, or `zicato epoch new`. See
[EPOCHS-AND-JOURNALING.md](../../docs/design/EPOCHS-AND-JOURNALING.md).

## What a good board looks like

- A spread of kinds: cheap `single_turn` smoke tests, `multi_turn_scripted`
  for deterministic revision regressions, one or two `multi_turn_emulated` for
  realistic dialog (up-weight these — they carry the strongest signal).
- Outcome `expectation`s where a crisp pass/fail exists; drift-loss-only
  (no expectation) where only cleanliness matters.
- `judges` for properties of the *process* (ordering, no fabrication), not the
  product.
- Consistent `tags` so pattern detectors and the brief can target slices.
- Stable `id`s — they are the pattern/journal/`runs/` directory key forever.
