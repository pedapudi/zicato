# Board-authoring guide

This guide is the practical companion to
[BOARD-FORMAT.md](BOARD-FORMAT.md), which specifies the on-disk JSONL
schema. This document shows an operator how to *author* a board. It covers
defining entries, choosing between an **outcome** check and a
**process** check, suppressing goldfive's built-in judges, writing
the proposer brief, and tuning the scoring weights, including the
per-judge weights.

It covers both authoring surfaces:

- The **Python builder** (`zicato.board`) — the ergonomic path. You
  construct entries with typed factory helpers and save a board.
- The **JSONL form** — the canonical wire format the runner reads.
  Hand-editable; what the builder writes; what `zicato board add`
  validates.

Read [BOARD-FORMAT.md](BOARD-FORMAT.md) first for the entry kinds,
the envelope fields, and the wall-clock budget semantics. This guide
assumes them.

## 1. The two facets of a board entry

A `BoardEntry` evaluates the system under test along two independent
facets. An entry that confuses the two measures the wrong thing, so
the distinction governs every authoring decision below.

| Facet | What it inspects | When it runs | Authored as |
|---|---|---|---|
| **Outcome** | The run's *product* — the final output, or the whole transcript. | Post-hoc, after the run terminates, in the loss reducer. | One `Predicate` or `Rubric`, attached as `evaluate=...`. |
| **Process** | The run's *reasoning* — the live goldfive event stream as the agent thinks. | In-run, by goldfive judges watching the reasoning stream. | `Judge`, attached as `judges=[...]`. |

An entry carries both. The outcome facet is a **single** expectation
passed as `evaluate=` (the field is singular — one expectation per
entry); the process facet is a **list** of judges:

```python
from goldfive import DriftSeverity

Entry(
    id="cited_market_summary",
    input="Summarise the EV market in three bullets with sources.",
    evaluate=Rubric.score(                # OUTCOME — one check on the output
        "Each bullet is accurate and non-redundant.",
        threshold=7.0,
    ),
    judges=[                              # PROCESS — checked on the reasoning
        Judge.custom(
            "cite_before_claim",
            "The agent must cite a source before stating a market metric.",
            severity=DriftSeverity.WARNING,
        ),
    ],
    budget_s=180,
    tags=["research", "citations"],
)
```

> **One expectation per entry.** The board schema carries a single
> `expectation` object per entry rather than a list, so `Entry` takes
> `evaluate=<one Predicate-or-Rubric>` and has no `expectations=`
> argument. When
> you want several independent outcome assertions on the same prompt,
> split them across several entries (each can share the same `input`),
> or fold them into one `Predicate.python` callable that ANDs the
> conditions.

The split has consequences for coverage. An outcome check cannot see
*how* the agent reached its answer; a process check cannot see the
final answer. A board that only checks outcomes misses a regression
that produces the right answer through degraded reasoning. A board
that only checks process misses a regression that reasons cleanly to
a wrong answer. Author both facets.

### 1.1 Outcome vs process: which goes where

Ask: *"can this property be decided by reading the final output (or
the transcript) alone?"*

- **Yes** → it is an outcome property. Use `Predicate` (deterministic)
  or `Rubric` (graded, LLM-scored). Examples: "the output is valid
  JSON", "the summary mentions cost", "the answer contains a citation
  marker", "a grader would rate the clarity at least 7/10".
- **No — it is a property of the reasoning** → it is a process
  property. Use `Judge`. Examples: "the agent cited a source *before*
  stating a metric", "the agent did not delegate to the writer before
  the researcher reported", "the plan was revised at most once".

A useful tell: process properties are usually about *ordering*,
*causality*, or *intermediate steps* — things that happened on the
way to the answer and left no trace in the answer itself.

## 2. Outcome checks: `Predicate` and `Rubric`

The outcome check attaches to an entry as the single `evaluate=`
argument — one `Predicate` or `Rubric` per entry (the underlying
`BoardEntry.expectation` field is singular; see §2.4).

`Predicate` and `Rubric` are namespaces of static factory helpers —
you never instantiate them. Import the factories from `zicato.board`;
the `OutputScope` enum they reference lives in `zicato.core`:

```python
from zicato.board import Predicate, Rubric
from zicato.core import OutputScope
```

### 2.1 `Predicate` — deterministic outcome checks

`Predicate` covers the four matchers that need no LLM. Each returns a
ready-to-attach expectation.

| Factory | Passes iff | Spec |
|---|---|---|
| `Predicate.contains(substring)` | the output contains `substring` (case-sensitive) | a literal string |
| `Predicate.regex(pattern)` | `re.search(pattern, output)` matches (DOTALL) | a Python regex |
| `Predicate.schema(schema_dict)` | `json.loads(output)` validates against the schema | a JSON-schema dict |
| `Predicate.python(dotted_path)` | the imported callable returns `True` | a dotted path to a callable |

```python
Predicate.contains("Total cost")
Predicate.regex(r"^- .+\n- .+\n- .+$")
Predicate.schema({
    "type": "object",
    "required": ["summary", "citations"],
    "properties": {
        "summary": {"type": "string"},
        "citations": {"type": "array", "items": {"type": "string"}},
    },
})
Predicate.python("myproj.predicates:three_bullets_about_solar")
```

`Predicate.python` is the escape hatch for logic the other three
cannot express. The dotted path uses a **colon** to separate the
module from the callable — `module.path:func` — the same convention
the presentation-agent example board writes, as in
`zicato_examples.target_1_presentation.predicates:mentions_waffles`.
The path must resolve under the project's import path at run time; the
callable receives the `RunResult`. Predicate **bodies are never
serialized** — they live in the project's own source. Shipping
arbitrary logic as JSON would invite injection, so the board carries
only the dotted path.

The callable may return any of three shapes (sync or async):

- **`bool`** — the historical pass/fail. `ExpectationResult.score`
  stays `None`; the reducer derives `1.0` / `0.0` from `passed`, so
  this is byte-identical to the binary path.
- **`float` in `[0, 1]`** — a **continuous per-entry score**: an F1
  score (the harmonic mean of precision and recall), a similarity, or
  a partial-credit fraction. It is clamped to `[0.0, 1.0]`
  and recorded as `ExpectationResult.score`; the scalar and the gate
  read that continuous value rather than the thresholded bit. `passed`
  becomes a display-only derivation (`score > 0.0`).
- **`(float, metrics)`** — a 2-tuple of the continuous score plus a
  `Mapping[str, float]` decomposition (e.g.
  `(0.71, {"precision": 0.8, "recall": 0.64})`). The mapping is
  recorded as `ExpectationResult.metrics` and carried out to
  `loss.json`, so downstream aggregation (the proposer's failure-mode
  profile) can read precision/recall as numbers without re-running the
  scorer.

Returning a continuous score is the right shape for a **sampled
evaluation board** where binary pass/fail throws away information —
pair it with `pass_rate_monotonicity_scope="aggregate"` (§6.4) so a
slightly-lower individual score does not veto an otherwise-better
challenger. A bare `bool` remains the common case.

### 2.2 `Rubric` — graded outcome checks

`Rubric.score` builds an LLM-graded outcome check. The grader reads
the output (or transcript), scores it on a numeric scale against your
criterion, and the expectation passes iff the score meets a threshold.

```python
Rubric.score(
    "Rate how clearly the summary explains the cost trade-offs, 0-10.",
    threshold=7.0,
)
```

> **Naming note.** The factory is `Rubric.score()`, and there is no
> `Rubric.judge()`. The word "judge" is reserved for the in-run
> *process* check (`Judge`, §3). A rubric grades an outcome; it does
> not judge a process.

Parameters:

| Parameter | Meaning |
|---|---|
| `criterion` (positional) | The grading instruction, embedded verbatim into the grader's prompt. Free-form prose describing an **outcome** property. |
| `threshold=` | Minimum score for the expectation to pass. `None` makes it advisory — it always passes and the score lands in the result detail for inspection. |
| `scale=` | `(lo, hi)` numeric bounds the grader scores on. Defaults to `(0.0, 10.0)`. |
| `reads=` | An `OutputScope` enum value: `OutputScope.FINAL` (the agent's final reply) or `OutputScope.TRANSCRIPT` (the whole conversation). Defaults to `OutputScope.FINAL`. |

```python
Rubric.score(
    "Across the whole dialogue, did the agent stay consistent about "
    "the recommended panel wattage?",
    threshold=6.0,
    reads=OutputScope.TRANSCRIPT,
)
```

`reads=` is a typed `OutputScope` enum, never a magic string — see
§7. Use `OutputScope.TRANSCRIPT` for multi-turn entries where the
property spans turns; `OutputScope.FINAL` (the default) for
single-turn entries and for multi-turn entries whose contract is
satisfied by the last reply alone.

The grader runs through `evaluation_call_llm`, never the target
callable — the model grading the output must not be the model that
produced it (see [EMULATOR.md](EMULATOR.md) §3 on collusion).

### 2.3 `Predicate` and `Rubric` are both *outcome* checks

The word "judge" attaches to only one of these two, so the vocabulary
is worth stating outright:

- `Predicate` + `Rubric` = **outcome** checks. Post-hoc. On the
  product. Attached as the single `evaluate=...`.
- `Judge` = **process** check. In-run. On the reasoning. Listed under
  `judges=[...]`.

`Rubric` uses an LLM, but it is still an *outcome* check: it reads
the finished output rather than the reasoning stream. `Judge` is the
only construct that watches the reasoning stream.

### 2.4 One expectation per entry

An entry carries **at most one** outcome check. The underlying
`BoardEntry.expectation` field is a single object rather than a list,
and `Entry(evaluate=...)` takes one `Predicate` / `Rubric`. The entry's
`pass_fail` is that one expectation's result (an advisory rubric with
`threshold=None` always passes and records its score for inspection).

An entry with no `evaluate=` has `pass_fail = None` and contributes to
drift-loss-only scoring. See [SCORING.md](SCORING.md) §3.

When you need several independent outcome assertions on one prompt,
the idiomatic options are:

- **Split into several entries** that share the same `input` (or the
  same `turns` / `persona`), one expectation each. This also lets the
  pattern detectors attribute pass/fail per assertion.
- **Fold the assertions into one `Predicate.python` callable** that
  ANDs the conditions and returns a single `bool`.

## 3. Process checks: `Judge`

A `Judge` watches the agent's reasoning *as the run happens*. It is a
goldfive-side judge: goldfive evaluates it against the live event
stream and, when the criterion is violated, emits a drift event that
flows into the run's `LossProfile` on the same path as any built-in
drift.

Judges attach to an entry as the `judges` list.

```python
from zicato.board import Judge
from goldfive import DriftSeverity
```

### 3.1 `Judge.custom` — an inline natural-language judge

```python
Judge.custom(
    "cite-before-metric",
    "The agent must cite a source before stating any market metric.",
    severity=DriftSeverity.WARNING,
)
```

| Parameter | Meaning |
|---|---|
| `name` (positional) | A stable, filesystem-safe identifier for this judge. It is how the judge is referenced everywhere downstream — the emitted drift carries it as `judge_name`, and `per_judge_weights` keys on it. Choose it once and do not rename it within an epoch. |
| `criterion` (positional) | A natural-language description of a **process** property the judge looks for in the agent's reasoning. |
| `severity=` | A `goldfive.DriftSeverity` enum value — `INFO`, `WARNING` (default), or `CRITICAL`. Controls how heavily a violation weighs in the drift loss. |

The criterion **must describe a process property** — something
observable in the agent's reasoning stream as it works. Good
criteria:

- "The agent must cite a source before stating a metric."
- "The agent must not call the same tool twice with identical
  arguments."
- "The coordinator must let the researcher report before delegating
  to the writer."

Criteria that belong in a `Rubric` instead, **not** a `Judge`,
because they are properties of the finished output:

- "The summary is accurate." → outcome → `Rubric.score(...)`.
- "The answer is three bullets." → outcome →
  `Predicate.regex(...)`.
- "The output cites at least one source." → outcome →
  `Predicate.regex(r"\[\d+\]")` (the *presence* of a citation in the
  output is an outcome; *citing before claiming* is a process).

If you find yourself writing a `Judge` criterion that a grader could
check by reading only the final answer, it should be a `Rubric` or a
`Predicate`.

### 3.2 What a violated `Judge` emits

When a `Judge.custom` criterion is violated, goldfive emits a drift
event of kind `custom` (`DriftKind.CUSTOM`). The event is
**identified by the judge's `name`**, carried on the event as
`judge_name`. So:

- The judge `"cite-before-metric"` → on violation → a `custom` drift
  with `judge_name == "cite-before-metric"`.
- The reducer attributes it under `custom:<judge_name>` in the run's
  `drift_counts`, and the per-judge breakdown (`per_judge_loss`) keys
  the count on `judge_name`.
- `ScoringWeights.per_judge_weights["cite-before-metric"]` lets you
  weight that specific judge's violations (§6.3).

Because every custom judge emits the same `custom` drift kind, the
`judge_name` is the discriminator. Two judges on the same board are
told apart by name, never by drift kind. This is why the `name` must
be stable and unique within the board.

The drift-emit path — `Judge.custom` → `custom` drift +
`JudgementEmitted.judge_name` — is specified in
[ARCHITECTURE.md](ARCHITECTURE.md) §4.6.1 and
[TELEMETRY.md](TELEMETRY.md).

### 3.3 `Judge.python` — the programmatic escape hatch

When a property is too mechanical for a natural-language criterion —
counting events, inspecting tool arguments, asserting on structured
plan state — use `Judge.python`:

```python
Judge.python(
    "no-duplicate-tool-calls",
    "myproj.judges:no_duplicate_tool_calls",
    severity=DriftSeverity.WARNING,
)
```

The second argument (the judge's `body`) is a dotted import path to a
Python callable that goldfive invokes against the reasoning stream;
`Judge.python` requires a path with a module component. Like
`Predicate.python`, the body lives in the project's source, never in
the board JSON. The violation it raises emits the same `custom` drift +
`judge_name` shape as `Judge.custom`.

> **Grade the tool ledger rather than the narration.** goldfive
> dispatches custom judges at *reasoning* observation points and does
> **not** set `ctx.extras["tool_event"]`. A `Judge.python` body that
> needs to know what the agent actually *did* — which tools it called,
> with what arguments, and whether they erred — must read the tool-call
> ledger at `ctx.session_state.recent_events`. Each `tool_observed`
> entry there carries `tool_name`, `args_preview`, `result_preview`,
> and `is_error`. Do **not** infer tool behaviour from the reasoning
> text: a model's narration can report a failure that never occurred,
> or omit one that did, so a judge grading the narration can be misled
> in either direction. The structured ledger is the ground truth.

Reach for `Judge.python` only when a natural-language criterion cannot
express the check. `Judge.custom` is the common case.

### 3.4 goldfive's built-in judges and `disable_drift`

goldfive ships its own judges — the drift detectors that produce
`confabulation_risk`, `looping_reasoning`, and the rest of the
`DriftKind` taxonomy. These are **ambient and default-on**: every
run is watched by them whether or not the board entry adds any
`judges` of its own. The `judges=[...]` list *adds* custom judges on
top of the ambient set; it does not replace it.

To suppress a built-in judge, a board declares `disable_drift` — a
list of `goldfive.DriftKind` enum values whose detectors are turned
off for every entry on that board:

```python
from goldfive import DriftKind

board = Board(
    disable_drift=[DriftKind.LOOPING_TOOL_CALL],
)
```

This suppresses the `LOOPING_TOOL_CALL` built-in judge for the whole
board. Use it when a built-in detector produces noise that is
irrelevant or expected for *this* system under test — for instance, an
agent that legitimately retries a flaky tool will trip
`LOOPING_TOOL_CALL` without it being a real failure.

`disable_drift` suppresses **built-ins by kind**. It does not affect
custom judges: those are identified by `judge_name` rather than by
kind, and a board removes a custom judge by deleting it from `judges`
rather than through `disable_drift`. `disable_drift` is part of the
evaluation contract:
changing it changes which signals score the board, so it rolls the
epoch (see [EPOCHS-AND-JOURNALING.md](EPOCHS-AND-JOURNALING.md) §10).

## 4. Defining a board with the Python builder

The `zicato.board` builder is the ergonomic authoring surface.

```python
from pathlib import Path

from zicato.board import (
    Board,
    Entry,
    Judge,
    Predicate,
    Rubric,
)
from zicato.core import OutputScope, UserPersona
from goldfive import DriftKind, DriftSeverity
```

The builder factories (`Board`, `Entry`, `Judge`, `Predicate`,
`Rubric`) live in `zicato.board`; the `OutputScope` enum and the
`UserPersona` dataclass are core types imported from `zicato.core`.

### 4.1 `Entry` — the entry factory

`Entry(...)` infers the entry kind from the keyword arguments you
supply and returns a fully-validated `BoardEntry`.

| Supply | Inferred kind |
|---|---|
| `input=` only | `single_turn` |
| `turns=[...]` | `multi_turn_scripted` |
| `persona=UserPersona(...)` | `multi_turn_emulated` |

Common keyword arguments:

| Argument | Meaning |
|---|---|
| `id=` | Stable, filesystem-safe identifier. Unique within the board. |
| `input=` / `turns=` / `persona=` | The per-kind discriminant; supply one of the three. `turns=` accepts plain strings; `persona=` takes a `UserPersona`. |
| `evaluate=` | A **single** `Predicate` / `Rubric` outcome check (or omitted). Not a list — one expectation per entry. |
| `judges=` | List of `Judge` process checks. Default `()`. |
| `budget_s=` | Wall-clock budget for the whole entry, in seconds. Default `300`. |
| `weight=` | Relative scoring weight. Default `1.0`. |
| `tags=` | Operator labels for pattern slicing. `holdout` and `facet:{name}` are reserved (BOARD-FORMAT.md §1.4). Default `()`. |

For a `multi_turn_scripted` entry, `Entry` auto-fills `max_turns` to
`len(turns)` when you do not pass it; for `multi_turn_emulated` it
defaults `max_turns` to `5`.

### 4.2 A worked board

```python
board = Board(
    # Suppress one noisy built-in judge for this harness.
    disable_drift=[DriftKind.LOOPING_TOOL_CALL],
)

# Single-turn, outcome-only (one expectation).
board.add(Entry(
    id="three_bullet_solar",
    input="Summarise solar panels in exactly three bullet points.",
    evaluate=Predicate.regex(r"^- .+\n- .+\n- .+$"),
    budget_s=120,
    tags=["easy", "summarise"],
))

# Single-turn, both facets — one outcome check, one process judge.
board.add(Entry(
    id="cited_market_summary",
    input="Summarise the EV market in three bullets, each with a source.",
    evaluate=Rubric.score(
        "Each bullet is accurate and non-redundant.",
        threshold=7.0,
    ),
    judges=[
        Judge.custom(
            "cite-before-metric",
            "The agent must cite a source before stating any market metric.",
            severity=DriftSeverity.WARNING,
        ),
    ],
    budget_s=180,
    weight=1.5,
    tags=["research", "citations"],
))

# A second entry sharing the same prompt carries the citation-marker
# check — one expectation per entry, so independent assertions live on
# separate entries.
board.add(Entry(
    id="cited_market_summary_has_marker",
    input="Summarise the EV market in three bullets, each with a source.",
    evaluate=Predicate.regex(r"\[\d+\]"),
    budget_s=180,
    tags=["research", "citations"],
))

# Multi-turn scripted, transcript-scoped rubric + a process judge.
board.add(Entry(
    id="revision_dialog",
    turns=[
        "Make a presentation about solar panels.",
        "Add a slide about cost.",
        "Now make slide 3 less technical.",
    ],
    evaluate=Rubric.score(
        "Did the agent correctly apply every requested revision?",
        threshold=7.0,
        reads=OutputScope.TRANSCRIPT,
    ),
    judges=[
        Judge.custom(
            "ack-before-edit",
            "The agent must acknowledge a revision request before editing.",
            severity=DriftSeverity.WARNING,
        ),
    ],
    budget_s=480,
    tags=["multi-turn", "revision"],
))

board.save(Path("board.jsonl"))
```

`Board.add` checks each `id` for uniqueness as it is appended, so a
collision is reported at construction time rather than at `save`.
`Board.save` writes the canonical JSONL; `Board.load` reads it back.

## 5. The JSONL form

The builder writes — and `zicato board add` validates — the JSONL
form specified in [BOARD-FORMAT.md](BOARD-FORMAT.md). One entry per
line. You can also hand-author it.

The two facets serialize differently because one is singular and one
is a list:

- `expectation` — a single expectation object (the one
  `Predicate` / `Rubric` outcome check), or absent.
- `judges` — an array of judge objects (the `Judge` process checks).

A single-turn entry with both facets, as one JSONL line (shown
pretty-printed; on disk it is one line):

```json
{
  "id": "cited_market_summary",
  "kind": "single_turn",
  "input": "Summarise the EV market in three bullets, each with a source.",
  "wall_clock_budget_seconds": 180,
  "weight": 1.5,
  "tags": ["research", "citations"],
  "expectation": {
    "kind": "rubric",
    "spec": "{\"rubric\":\"Each bullet is accurate and non-redundant.\",\"scale\":[0.0,10.0],\"threshold\":7.0}",
    "reads": "final_output"
  },
  "judges": [
    {"name": "cite-before-metric",
     "mode": "inline",
     "body": "The agent must cite a source before stating any market metric.",
     "severity": "warning"}
  ]
}
```

(`Board.save` writes the budget under the short key `budget_s`; the
reader accepts both `budget_s` and `wall_clock_budget_seconds`, so the
long form shown here is equally valid for hand-authored boards.)

The board-level `disable_drift` is a board-wide setting rather than a
per-entry field. It is recorded once on the optional `board_meta`
header line at the top of the JSONL — see
[BOARD-FORMAT.md](BOARD-FORMAT.md) §1.0.

Enum-valued fields serialize as their **bare wire tokens**, the
lowercase string value of the enum. Severity writes `"warning"` or
`"critical"`; `reads` writes `"final_output"` or `"conversation_end"`;
a judge's `mode` writes `"inline"` or `"python"`; an expectation's
`kind` writes `"rubric"`, `"regex"`, `"predicate"` and the rest of its
roster. (The enums subclass
`str`, so the value *is* the token.) The reader rejects an
unrecognised value loudly at `zicato board add` time. See §7.

## 6. Scoring weights

A board's entries are scored with the weights in `ScoringWeights`
(frozen per epoch in the epoch's scoring configuration — see
[SCORING.md](SCORING.md)). Three weight surfaces matter when you are
authoring a board with judges.

### 6.1 Per-entry `weight`

`Entry(weight=...)` scales an entry's whole contribution — both its
drift loss and its pass/fail — in the per-generation aggregate. A
critical entry gets `weight=2.0`; the default is `1.0`. See
[BOARD-FORMAT.md](BOARD-FORMAT.md) §1.3.

### 6.2 Per-drift-kind weights

`ScoringWeights.per_kind_weights` is a mapping from a **drift-kind
token** (the lowercase `DriftKind` wire string) to a multiplier. It
elevates or demotes a whole *kind* of drift in the loss. The keys are
the bare tokens, in the form the frozen `scoring.json` records them:

```json
"per_kind_weights": {"confabulation_risk": 2.0, "looping_reasoning": 1.5}
```

In Python you may key with the `DriftKind` member directly
(`DriftKind.CONFABULATION_RISK`) since `DriftKind` subclasses `str` and
the member equals its token, but the canonical on-disk form is the
lowercase string. Custom-judge violations all land under the `custom`
kind, so `per_kind_weights["custom"]` weights *every* custom judge at
once.

### 6.3 Per-judge weights

When you want to weight one custom judge differently from another,
use `ScoringWeights.per_judge_weights` — a mapping **keyed on the
judge `name`**:

```python
ScoringWeights(
    per_kind_weights={"confabulation_risk": 1.5},
    per_judge_weights={
        "cite-before-metric": 2.0,   # this judge's violations weigh double
        "ack-before-edit": 0.5,      # this one is advisory-ish
    },
)
```

`per_judge_weights` keys are the same `name` strings you passed to
`Judge.custom` / `Judge.python`, and the same strings that appear as
`judge_name` on the emitted drift. A judge with no entry in
`per_judge_weights` falls back to `ScoringWeights.default_judge_weight`
(default `1.0`). This is the knob for "violations of *this specific*
judge matter more than violations of *that* one" without splitting
them into different drift kinds.

Because `per_judge_weights` is part of `ScoringWeights`, it is frozen
per epoch — changing it rolls the epoch.

### 6.4 Pass-rate monotonicity scope — match it to your board's shape

The promote gate's pass-rate monotonicity rule (SCORING.md §5, Rule 2)
has a *granularity* knob, `pass_rate_monotonicity_scope`, that should be
chosen to match what your board's entries represent:

```json
"pass_rate_monotonicity": true,
"pass_rate_monotonicity_scope": "per_entry"
```

- **`per_entry`** (the default) — *every* entry the champion passed
  must still pass on the challenger, or the gate rejects and names the
  regressed entry ids. Choose this when each entry is a
  **must-not-regress invariant**: a regression suite where any entry
  that was passing and now fails is a real breakage.

- **`aggregate`** — the gate rejects only when the challenger's
  **overall** pass-rate falls below the champion's (modulo float
  noise). A challenger may trade *which* individual entries pass as
  long as the net pass-rate holds or improves. Choose this for a
  **sampled evaluation board**, where the entries are samples of a
  capability and individual pass/fail is noisy. Under `per_entry`, any
  entry with run-to-run nondeterminism (sampling, retrieval ties,
  timeouts) becomes a permanent veto that no amount of aggregate
  improvement can overcome, and a challenger that is better on every
  aggregate can be rejected over a single entry flip. `aggregate` lets
  promotions track the aggregate the operator is optimizing.

There is no `"off"` scope value — disable the rule entirely with
`pass_rate_monotonicity: false`. The chosen scope applies to both the
train slice and the holdout-confirmation check, so the two use one
consistent policy. Because the scope is part of `ScoringWeights`, it is
frozen per epoch — changing it rolls the epoch.

## 7. Typed enums, no magic strings

Every choice field in the authoring surface is a typed enum. There
are no magic strings anywhere an operator writes a board.

| Concept | Enum | Owner | Wire token examples |
|---|---|---|---|
| Drift kind (built-ins + `custom`) | `DriftKind` | `goldfive` | `"confabulation_risk"`, `"looping_reasoning"`, `"custom"` |
| Drift severity | `DriftSeverity` | `goldfive` | `"info"` / `"warning"` / `"critical"` |
| Which slice an expectation reads | `OutputScope` | `zicato` | `"final_output"` / `"conversation_end"` |
| Expectation kind | `ExpectationKind` | `zicato` | `"predicate"` / `"regex"` / `"json_schema"` / `"expected_text"` / `"rubric"` |
| Judge mode | `JudgeMode` | `zicato` | `"inline"` / `"python"` |

The rule:

- goldfive concepts use **goldfive enums**. A judge's `severity=` is a
  `goldfive.DriftSeverity` member; `disable_drift` is a list of
  `goldfive.DriftKind` members.
- zicato concepts use **zicato enums**. A rubric's `reads=` is a
  `zicato.core` `OutputScope` member; a judge's `mode` is a
  `JudgeMode` member.

You import them from where they are defined and pass the member, not
a string:

```python
from goldfive import DriftKind, DriftSeverity
from zicato.core import OutputScope

Judge.custom("x", "...", severity=DriftSeverity.CRITICAL)
Rubric.score("...", threshold=7.0, reads=OutputScope.TRANSCRIPT)
Board(disable_drift=[DriftKind.LOOPING_TOOL_CALL])
```

On the JSONL wire the enum serializes to its **bare wire token** — the
lowercase `str` value of the enum member (`"critical"`,
`"conversation_end"`, `"inline"`, …) rather than an uppercase symbolic
name. These enums subclass `str`, so the value *is* the token and the
file stays self-describing and human-readable; on read the token is
validated back to its enum member. An unrecognised value is rejected
at `zicato board add` time, never silently defaulted. That rejection
is why the rule exists: an unchecked string reaches the runner and
misbehaves there, while a typed enum fails at authoring time with a
message naming the field.

## 8. The proposer brief

The board defines *what the system under test is evaluated against*. The
**proposer brief** is the separate document that steers *how the
proposer rewrites the harness* in response.

> **Naming note.** The steering document is the **proposer brief**,
> and the word "rubric" names only the per-entry `Rubric.score()`
> outcome check. The two are distinct concepts: one grades an entry's
> output, the other briefs the proposer. The brief's file on disk is
> `brief.md`; a workspace that still carries a `rubric.md` beside the
> `.zicato/` directory is read as the brief when `brief.md` is absent.

The proposer brief is the operator's per-epoch steering document for
the proposer. It is markdown, read fresh into the proposer's prompt
every round, with one mechanically-enforced section:

```markdown
# Proposer brief — epoch: hardened_research

## Focus
- Reduce CONFABULATION_RISK on entries tagged `[research]`.
- Investigate why custom judge `cite-before-metric` fires on
  revision turns specifically.

## Style
- Prefer terse, imperative specialist instructions.

## Forbidden
- coordinator.routing
- writer.tools.summarize.description
```

The `## Forbidden` section is the **forbidden-id list**: any patch
targeting a mutation-point id listed there is rejected at validate
time. Every other section is advisory natural language the proposer
reads to steer.

The proposer brief is *not* the per-entry `Rubric`:

| | Per-entry `Rubric.score()` | Proposer brief |
|---|---|---|
| Scope | one board entry | the whole epoch |
| Read by | the loss reducer, post-run | the proposer, every round |
| Decides | that entry's `pass_fail` | what the proposer tries next |
| Form | a typed expectation passed as `evaluate=...` | a markdown file |
| Enforced? | yes — scored | only `## Forbidden` is enforced |

The full proposer-brief design — what causes an epoch boundary, why
mid-epoch edits to everything *except* `## Forbidden` are fine — is in
[EPOCHS-AND-JOURNALING.md](EPOCHS-AND-JOURNALING.md) §7.

## 9. Authoring inside the evolve workflow

In the common workflow you do not call the board subcommands by hand.
The happy path is two commands:

```
zicato init
zicato evolve
```

`zicato init` scaffolds the workspace. `zicato evolve` runs the
meta-loop and **auto-epochs**: it hashes the evaluation contract
(board plus proposer brief plus scoring plus harness identity) and
rolls a new epoch when any of them has been edited. So the authoring
loop is:

1. Edit `board.jsonl` (by hand, or regenerate it with the Python
   builder) and the proposer brief.
2. Run `zicato evolve`.
3. `evolve` notices the contract changed and rolls the epoch for you.

`zicato board add` / `list` / `remove`, `zicato epoch new` / `close`,
`zicato proposer propose`, `zicato tournament`, and the rest are **advanced /
debug** commands — the manual escape hatches for when you want to drive
one stage in isolation. They are fully documented in [CLI.md](CLI.md),
but a first-time operator authoring a board only needs the builder,
this guide, and `zicato evolve`.

## 10. Authoring checklist

- [ ] Every entry has a stable, filesystem-safe, board-unique `id`.
- [ ] Every entry has a `budget_s` (wall-clock budget).
- [ ] The outcome property is one `Predicate` / `Rubric` passed as
      `evaluate=...` (a single expectation per entry).
- [ ] Process properties are `Judge` in `judges=[...]`.
- [ ] Every `Judge` criterion describes a property of the *reasoning*,
      not the *output*.
- [ ] Every `Judge` has a stable, unique `name` (you will reference it
      in `per_judge_weights`).
- [ ] Multi-turn rubrics that span turns use
      `reads=OutputScope.TRANSCRIPT`.
- [ ] Every choice field is a typed enum member rather than a string.
- [ ] Noisy built-in judges are suppressed via `Board(disable_drift=[...])`.
- [ ] The proposer brief's `## Forbidden` section lists the
      mutation-point ids that are off-limits this epoch.

## 11. Cross-references

| Topic | Document |
|---|---|
| The JSONL schema, entry kinds, wall-clock budget | [BOARD-FORMAT.md](BOARD-FORMAT.md) |
| `Judge` → `custom` drift + `judge_name` emit path | [ARCHITECTURE.md §4.6.1](ARCHITECTURE.md#461-pluggable-judges-the-goldfive-integration) |
| How the `expectation`, `judges`, and weights enter the score | [SCORING.md](SCORING.md) |
| The proposer brief and epoch boundaries | [EPOCHS-AND-JOURNALING.md](EPOCHS-AND-JOURNALING.md) |
| Drift counting, `judge_name` in the loss profile | [TELEMETRY.md](TELEMETRY.md) |
| Why `evaluation_call_llm` grades rubrics (collusion) | [EMULATOR.md](EMULATOR.md) |
| The evolve-centric CLI; advanced/debug subcommands | [CLI.md](CLI.md) |
