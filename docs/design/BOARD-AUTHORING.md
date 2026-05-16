# Board-authoring guide

This guide is the practical companion to
[BOARD-FORMAT.md](BOARD-FORMAT.md). Where BOARD-FORMAT specifies the
on-disk JSONL schema, this document shows an operator how to *author*
a board: how to define entries, how to choose between an **outcome**
check and a **process** check, how to suppress goldfive's built-in
judges, how to write the proposer brief, and how to tune scoring
weights — including the per-judge weights.

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

A `BoardEntry` evaluates the inner harness along two independent
facets. Keeping them distinct is the single most important idea in
this guide.

| Facet | What it inspects | When it runs | Authored as |
|---|---|---|---|
| **Outcome** | The run's *product* — the final output, or the whole transcript. | Post-hoc, after the run terminates, in the loss reducer. | `Predicate` and `Rubric`, attached as `expectations=[...]`. |
| **Process** | The run's *reasoning* — the live goldfive event stream as the agent thinks. | In-run, by goldfive judges watching the reasoning stream. | `Judge`, attached as `judges=[...]`. |

An entry carries both:

```python
Entry(
    id="cited_market_summary",
    input="Summarise the EV market in three bullets with sources.",
    expectations=[                       # OUTCOME — checked on the output
        Predicate.regex(r"\[\d+\]"),     # at least one [n] citation marker
        Rubric.score("Each bullet is accurate and non-redundant.",
                     threshold=7.0),
    ],
    judges=[                             # PROCESS — checked on the reasoning
        Judge.custom(
            "cite-before-claim",
            "The agent must cite a source before stating a market metric.",
        ),
    ],
    budget_s=180,
    tags=["research", "citations"],
)
```

The split is not cosmetic. An outcome check cannot see *how* the
agent got to its answer; a process check cannot see the final answer.
A board that only checks outcomes misses the chaotic-success
regression (right answer, sloppy reasoning); a board that only checks
process misses the silent-success regression (clean reasoning, wrong
answer). Author both.

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

Outcome checks attach to an entry as the `expectations` list. Every
expectation in the list is evaluated against the run result; how the
list aggregates into the entry's `pass_fail` is in §2.4.

`Predicate` and `Rubric` are namespaces of static factory helpers —
you never instantiate them. Import once:

```python
from zicato.board import Predicate, Rubric, OutputScope
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
Predicate.python("myproj.predicates.three_bullets_about_solar")
```

`Predicate.python` is the escape hatch for logic the other three
cannot express. The dotted path must resolve under the project's
import path at run time; it receives the typed `RunResult` and returns
`bool`. Predicate **bodies are never serialized** — they live in the
project's own source. Shipping arbitrary logic as JSON would invite
injection, so the board carries only the dotted path.

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

> **Naming note.** This factory is `Rubric.score()`. It was previously
> spelled `Rubric.judge()`; the name moved to `score()` so that
> "judge" unambiguously means the in-run *process* check (`Judge`,
> §3). A rubric grades an outcome; it does not judge a process.

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

The grader runs through `auxiliary_call_llm`, never the harness
callable — the model grading the output must not be the model that
produced it (see [EMULATOR.md](EMULATOR.md) §3 on collusion).

### 2.3 `Predicate` and `Rubric` are both *outcome* checks

This bears repeating because the word "judge" historically attached
to the LLM-graded matcher. In the converged vocabulary:

- `Predicate` + `Rubric` = **outcome** checks. Post-hoc. On the
  product. Listed under `expectations=[...]`.
- `Judge` = **process** check. In-run. On the reasoning. Listed under
  `judges=[...]`.

`Rubric` uses an LLM, but it is still an *outcome* check — it reads
the finished output, not the reasoning stream. The thing that watches
the reasoning stream is `Judge`, and only `Judge`.

### 2.4 Multiple expectations on one entry

`expectations=[...]` is a list. An entry with N expectations passes
iff **every** expectation passes — the entry's `pass_fail` is the
logical AND. Advisory rubrics (`threshold=None`) always pass and so
never affect the AND; their scores are recorded for inspection only.

An entry with an empty `expectations=[]` (or no `expectations` at
all) has `pass_fail = None` and contributes to drift-loss-only
scoring. See [SCORING.md](SCORING.md) §3.

## 3. Process checks: `Judge`

A `Judge` watches the agent's reasoning *as the run happens*. It is a
goldfive-side judge: goldfive evaluates it against the live event
stream and, when the criterion is violated, emits a drift event that
flows into the run's `LossProfile` exactly like any built-in drift.

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
event of kind `DriftKind.CUSTOM`. The event is **identified by the
judge's `name`**, carried on the event as `judge_name`. So:

- The judge `"cite-before-metric"` → on violation → a
  `DriftKind.CUSTOM` drift with `judge_name == "cite-before-metric"`.
- The reducer counts it under `DRIFT_KIND_CUSTOM` in
  `drift_counts_by_kind`, and the per-judge breakdown keys the count
  on `judge_name`.
- `ScoringWeights.per_judge_weights["cite-before-metric"]` lets you
  weight that specific judge's violations (§6.3).

Because every custom judge emits the same `DriftKind.CUSTOM`, the
`judge_name` is the discriminator. Two judges on the same board are
told apart by name, never by drift kind. This is why the `name` must
be stable and unique within the board.

The drift-emit path — `Judge.custom` → `DriftKind.CUSTOM` +
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
    "myproj.judges.no_duplicate_tool_calls",
    severity=DriftSeverity.WARNING,
)
```

The second argument is a dotted path to a Python callable that
goldfive invokes against the reasoning stream. Like `Predicate.python`,
the body lives in the project's source, never in the board JSON. The
violation it raises emits the same `DriftKind.CUSTOM` +
`judge_name` shape as `Judge.custom`.

Reach for `Judge.python` only when a natural-language criterion can't
express the check. `Judge.custom` is the common case.

### 3.4 goldfive's built-in judges and `disable_drift`

goldfive ships its own judges — the drift detectors that produce
`DRIFT_KIND_CONFABULATION_RISK`, `DRIFT_KIND_LOOPING_REASONING`, and
the rest of the taxonomy. These are **ambient and default-on**: every
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
irrelevant or expected for *this* inner harness — for instance, an
agent that legitimately retries a flaky tool will trip
`LOOPING_TOOL_CALL` without it being a real failure.

`disable_drift` suppresses **built-ins by kind**. It does not affect
custom judges — those are identified by `judge_name`, not by kind, and
a board removes a custom judge by deleting it from `judges`, not via
`disable_drift`. `disable_drift` is part of the evaluation contract:
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
    OutputScope,
    Predicate,
    Rubric,
)
from goldfive import DriftKind, DriftSeverity
```

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
| `input=` / `turns=` / `persona=` | The per-kind discriminant (exactly one). |
| `expectations=` | List of `Predicate` / `Rubric` outcome checks. Default `[]`. |
| `judges=` | List of `Judge` process checks. Default `[]`. |
| `budget_s=` | Wall-clock budget for the whole entry, in seconds. |
| `weight=` | Relative scoring weight. Default `1.0`. |
| `tags=` | Operator labels for pattern slicing. Default `()`. |

### 4.2 A worked board

```python
board = Board(
    # Suppress one noisy built-in judge for this harness.
    disable_drift=[DriftKind.LOOPING_TOOL_CALL],
)

# Single-turn, outcome-only.
board.add(Entry(
    id="three_bullet_solar",
    input="Summarise solar panels in exactly three bullet points.",
    expectations=[
        Predicate.regex(r"^- .+\n- .+\n- .+$"),
    ],
    budget_s=120,
    tags=["easy", "summarise"],
))

# Single-turn, both facets.
board.add(Entry(
    id="cited_market_summary",
    input="Summarise the EV market in three bullets, each with a source.",
    expectations=[
        Predicate.regex(r"\[\d+\]"),
        Rubric.score("Each bullet is accurate and non-redundant.",
                     threshold=7.0),
    ],
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

# Multi-turn scripted, transcript-scoped rubric + a process judge.
board.add(Entry(
    id="revision_dialog",
    turns=[
        "Make a presentation about solar panels.",
        "Add a slide about cost.",
        "Now make slide 3 less technical.",
    ],
    expectations=[
        Rubric.score(
            "Did the agent correctly apply every requested revision?",
            threshold=7.0,
            reads=OutputScope.TRANSCRIPT,
        ),
    ],
    judges=[
        Judge.custom(
            "ack-before-edit",
            "The agent must acknowledge a revision request before editing.",
        ),
    ],
    budget_s=480,
    tags=["multi-turn", "revision"],
))

board.save(Path("board.jsonl"))
```

`Board.add` checks each `id` for uniqueness as it is appended, so a
collision is reported at construction time, not at `save`. `Board.save`
writes the canonical JSONL; `Board.load` reads it back.

## 5. The JSONL form

The builder writes — and `zicato board add` validates — the JSONL
form specified in [BOARD-FORMAT.md](BOARD-FORMAT.md). One entry per
line. You can also hand-author it.

The two facets serialize as two arrays on the entry:

- `expectations` — an array of expectation objects (the
  `Predicate` / `Rubric` outcome checks).
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
  "expectations": [
    {"kind": "regex", "spec": "\\[\\d+\\]", "fires_on": "final_output"},
    {"kind": "rubric",
     "spec": "{\"rubric\":\"Each bullet is accurate and non-redundant.\",\"scale\":[0.0,10.0],\"threshold\":7.0}",
     "reads": "FINAL"}
  ],
  "judges": [
    {"kind": "custom",
     "name": "cite-before-metric",
     "criterion": "The agent must cite a source before stating any market metric.",
     "severity": "WARNING"}
  ]
}
```

The board-level `disable_drift` is a board-wide setting, not a
per-entry field. It is recorded once for the board (alongside the
scoring configuration the epoch freezes); see
[BOARD-FORMAT.md](BOARD-FORMAT.md) for where the board's
configuration lives.

Enum-valued fields serialize as their **symbolic names** —
`"WARNING"`, `"FINAL"`, `"custom"` — never as integers or free
strings. The reader rejects an unrecognised value loudly at
`zicato board add` time. See §7.

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

`ScoringWeights.per_kind_weights` is a mapping from `DriftKind` to a
multiplier. It elevates or demotes a whole *kind* of drift in the
loss. Custom-judge violations all land under `DriftKind.CUSTOM`, so
`per_kind_weights[DriftKind.CUSTOM]` weights *every* custom judge at
once.

### 6.3 Per-judge weights

When you want to weight one custom judge differently from another,
use `ScoringWeights.per_judge_weights` — a mapping **keyed on the
judge `name`**:

```python
ScoringWeights(
    per_kind_weights={DriftKind.CONFABULATION_RISK: 1.5},
    per_judge_weights={
        "cite-before-metric": 2.0,   # this judge's violations weigh double
        "ack-before-edit": 0.5,      # this one is advisory-ish
    },
)
```

`per_judge_weights` keys are the same `name` strings you passed to
`Judge.custom` / `Judge.python`, and the same strings that appear as
`judge_name` on the emitted drift. A judge with no entry in
`per_judge_weights` is weighted by `per_kind_weights[DriftKind.CUSTOM]`
(or the uniform default if that is also unset). This is the knob for
"violations of *this specific* judge matter more than violations of
*that* one" without splitting them into different drift kinds.

Because `per_judge_weights` is part of `ScoringWeights`, it is frozen
per epoch — changing it rolls the epoch.

## 7. Typed enums, no magic strings

Every choice field in the authoring surface is a typed enum. There
are no magic strings anywhere an operator writes a board.

| Concept | Enum | Owner |
|---|---|---|
| Drift kind (`CONFABULATION_RISK`, `CUSTOM`, …) | `DriftKind` | `goldfive` |
| Drift severity (`INFO` / `WARNING` / `CRITICAL`) | `DriftSeverity` | `goldfive` |
| Which slice a rubric reads (`FINAL` / `TRANSCRIPT`) | `OutputScope` | `zicato` |
| Expectation kind (`predicate` / `regex` / `schema` / `python` / `rubric`) | `ExpectationKind` | `zicato` |

The rule:

- goldfive concepts use **goldfive enums**. A judge's `severity=` is a
  `goldfive.DriftSeverity` member; `disable_drift` is a list of
  `goldfive.DriftKind` members; `per_kind_weights` keys are
  `DriftKind` members.
- zicato concepts use **zicato enums**. A rubric's `reads=` is a
  `zicato` `OutputScope` member.

You import them from where they are defined and pass the member, not
a string:

```python
from goldfive import DriftKind, DriftSeverity
from zicato.board import OutputScope

Judge.custom("x", "...", severity=DriftSeverity.CRITICAL)
Rubric.score("...", threshold=7.0, reads=OutputScope.TRANSCRIPT)
Board(disable_drift=[DriftKind.LOOPING_TOOL_CALL])
```

On the JSONL wire the enum serializes to its symbolic name (a string
like `"CRITICAL"`) so the file stays self-describing and human-
readable — but that string is the *projection* of an enum, validated
back to the enum on read. An unrecognised value is a loud rejection at
`zicato board add` time, never a silent default. Magic strings, by
contrast, fail late and quietly; typed enums fail early and
mechanically. That is the whole reason for the rule.

## 8. The proposer brief

The board defines *what the inner harness is evaluated against*. The
**proposer brief** is the separate document that steers *how the
proposer rewrites the harness* in response.

> **Naming note.** The proposer brief was previously called the
> epoch "rubric" (`rubric.md`). It is renamed to **proposer brief** so
> that "rubric" unambiguously means the per-entry `Rubric.score()`
> outcome check. The two were distinct concepts sharing a word: one
> grades an entry's output, the other briefs the proposer. They now
> have distinct names.

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
| Form | a typed expectation in `expectations=[...]` | a markdown file |
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

`zicato init` scaffolds the workspace; `zicato evolve` runs the
meta-loop and **auto-epochs** — it hashes the evaluation contract
(board + proposer brief + scoring + harness identity) and rolls a new
epoch automatically when you have edited any of them. So the authoring
loop is:

1. Edit `board.jsonl` (by hand, or regenerate it with the Python
   builder) and the proposer brief.
2. Run `zicato evolve`.
3. `evolve` notices the contract changed and rolls the epoch for you.

`zicato board add` / `list` / `remove`, `zicato epoch new` / `close`,
`zicato propose`, `zicato tournament`, and the rest are **advanced /
debug** commands — the manual escape hatches for when you want to drive
one stage in isolation. They are fully documented in [CLI.md](CLI.md),
but a first-time operator authoring a board only needs the builder,
this guide, and `zicato evolve`.

## 10. Authoring checklist

- [ ] Every entry has a stable, filesystem-safe, board-unique `id`.
- [ ] Every entry has a `budget_s` (wall-clock budget).
- [ ] Outcome properties are `Predicate` / `Rubric` in
      `expectations=[...]`.
- [ ] Process properties are `Judge` in `judges=[...]`.
- [ ] Every `Judge` criterion describes a property of the *reasoning*,
      not the *output*.
- [ ] Every `Judge` has a stable, unique `name` (you will reference it
      in `per_judge_weights`).
- [ ] Multi-turn rubrics that span turns use
      `reads=OutputScope.TRANSCRIPT`.
- [ ] Every choice field is a typed enum member, not a string.
- [ ] Noisy built-in judges are suppressed via `Board(disable_drift=[...])`.
- [ ] The proposer brief's `## Forbidden` section lists the
      mutation-point ids that are off-limits this epoch.

## 11. Cross-references

| Topic | Document |
|---|---|
| The JSONL schema, entry kinds, wall-clock budget | [BOARD-FORMAT.md](BOARD-FORMAT.md) |
| `Judge` → `DriftKind.CUSTOM` + `judge_name` emit path | [ARCHITECTURE.md §4.6.1](ARCHITECTURE.md#461-pluggable-judges-the-goldfive-integration) |
| How `expectations`, `judges`, and weights enter the score | [SCORING.md](SCORING.md) |
| The proposer brief and epoch boundaries | [EPOCHS-AND-JOURNALING.md](EPOCHS-AND-JOURNALING.md) |
| Drift counting, `judge_name` in the loss profile | [TELEMETRY.md](TELEMETRY.md) |
| Why `auxiliary_call_llm` grades rubrics (collusion) | [EMULATOR.md](EMULATOR.md) |
| The evolve-centric CLI; advanced/debug subcommands | [CLI.md](CLI.md) |
