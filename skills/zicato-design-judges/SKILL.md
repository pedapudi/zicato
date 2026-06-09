---
name: zicato-design-judges
description: Design zicato judges and expectations — the principles behind the syntax. The two expectation families (predicate/rubric and the matcher kinds), OUTCOME expectations vs in-run PROCESS judges, the drift kinds and how drift telemetry becomes loss, severity, the weighting knobs (per_judge_weights/default_judge_weight, per_kind_weights, severity_weights, namespace_weights), the collusion guard (judge/aux callable distinct from the harness callable), and judge_only mode. Use when deciding WHAT to measure and how it should weigh — not the JSON (that is zicato-author-board) or the weight values (zicato-tune-scoring).
---

# Designing zicato judges and expectations

A board entry can be scored on two independent axes: an **OUTCOME** check (did
the finished run satisfy an assertion?) and one or more **PROCESS** checks (how
did the run unfold while it was still running?). Choosing which axis a behavior
belongs on — and how its signal should weigh in the scalar — is a design
decision the loop's quality depends on. This skill covers the principles; the
JSON lives in [`zicato-author-board`](../zicato-author-board/SKILL.md) and the
weight *values* in [`zicato-tune-scoring`](../zicato-tune-scoring/SKILL.md).
It is the design-principles sibling of
[`zicato-design-boards`](../zicato-design-boards/SKILL.md). Design docs:
[BOARD-FORMAT.md](../../docs/design/BOARD-FORMAT.md),
[SCORING.md](../../docs/design/SCORING.md),
[TELEMETRY.md](../../docs/design/TELEMETRY.md). See
[`../AGENTS.md`](../AGENTS.md) for the operating rules.

## OUTCOME vs PROCESS — pick the right axis

| | `expectation` (OUTCOME) | `judges` (PROCESS) |
|---|---|---|
| When evaluated | post-hoc, after the run finishes | in-flight, while the run unfolds |
| Asks | "is the *product* right?" | "is the *process* clean?" |
| Cardinality | zero or one per entry | zero or more per entry |
| Scoring axis | **pass-rate** dimension | **drift-loss** dimension (emits `custom` drift) |
| Reads | `final_output` or `conversation_end` | the live reasoning / event stream |

The rule: assert on the **product** with an `expectation`; assert on the *way
the run got there* with a `judge`. "The deck mentions Belgian waffles" is an
expectation. "The agent never fabricated a metric value it wasn't given" is a
judge — fabrication is a property of the *trajectory*, and a run can land a
correct-looking product through a process you'd reject. The two axes are
independent and combine into the scalar with separate coefficients
(`pass_weight` vs `drift_weight`), so a behavior you care about on both axes
gets *both* an expectation and a judge.

## Expectations — the five matcher kinds

An `expectation` is optional (absent → the entry is scored on drift loss
alone). It is the compiled form of two authoring families: **Predicate** and
**Rubric**. Those compile down to five matcher `kind`s:

| `kind` | `spec` | Grades | Design note |
|---|---|---|---|
| `predicate` | dotted path `module:func` → `(run_result) -> bool` | deterministic, code-defined | the most expressive and reproducible; body lives in project source, never in JSON |
| `expected_text` | exact substring | deterministic | case- and whitespace-significant; brittle — prefer `regex` or `predicate` |
| `regex` | `re` pattern (`re.search`) | deterministic | anchor with `^`/`$`; easy to make accidentally all-pass |
| `json_schema` | inline JSON Schema | deterministic | output parsed as JSON then validated; non-JSON fails |
| `rubric` | JSON `{"rubric": <text>, "threshold": <float\|null>, "scale": [lo,hi]}` | LLM-as-judge via the **auxiliary** callable | use for fuzzy quality you can't code; `threshold: null` = advisory (always passes, records the score) |

`reads` selects the slice graded: `final_output` (the last reply; the single-
turn default) or `conversation_end` (the whole transcript — a `single_turn`
entry reading `conversation_end` is rejected). Design choice: prefer a
deterministic matcher (`predicate`/`regex`/`json_schema`) whenever the behavior
*can* be checked in code — deterministic matchers add zero noise to the
pass-rate signal. Reserve `rubric` for genuinely fuzzy quality, and remember a
rubric runs on the aux LLM, so it costs a call and carries run-to-run variance.

## Judges — in-run PROCESS checks

A judge watches the reasoning stream live and, on an adverse verdict, emits a
goldfive `custom` drift tagged with the judge's `name`. Two modes:

- **`inline`** — `body` is a natural-language criterion the process judge
  evaluates the run against ("Each revision visibly responds to the
  stakeholder's most recent feedback"). Graded by the aux LLM.
- **`python`** — `body` is a dotted import path to a process-judge callable.
  Deterministic; use it when the process property is checkable in code.

`severity` (`info` / `warning` / `critical`) is goldfive's `DriftSeverity` and
sets how hard the violation weighs (see severity weighting below). `name` is a
stable slug, board-unique, and becomes goldfive's `judge_name` — the key
`per_judge_weights` uses, so choose it deliberately.

### Judge the tool-call ledger, not the narration

A `python` judge that needs to grade *what the agent did* must read the real
**tool-call ledger**, never the model's reasoning narration or its completion
summary. goldfive dispatches custom judges only at **reasoning** observation
points, so the `JudgeContext` it hands a judge carries the model's
chain-of-thought (`ctx.reasoning_text`, the transcript window) — narration the
agent merely *wrote*, not the tool round-trips it *ran*. A judge that scans that
text for tool names grades a story the agent told about itself: text that
mentions `read_files` twice will trip a "retry loop" signal while the agent
never actually called the tool, and the same judge fires on unrelated chatter
that names a tool in passing.

The ground-truth record of every tool call is goldfive's session ring buffer,
reachable from the judge as **`ctx.session_state.recent_events`** — each tool
call appends one entry with `kind == "tool_observed"` carrying `tool_name`,
`args_preview`, `result_preview`, `is_error`, and `error_message`. A
deliverable-grading `python` judge reads *that* ledger (de-duplicating across
observation points, since the bounded ring is re-snapshotted on every call). The
narration is at most a last-resort fallback when no structured ledger is present
at all, and must never be allowed to manufacture a signal the ledger contradicts
(derive the count from the structured reads, so the count and the reason can
never disagree). The worked example is `file_findability` in
`examples/zicato_examples/target_1_presentation/judges.py`.

The same fidelity rule binds OUTCOME expectations: an `expectation` that grades
the deliverable must read the agent's *real* output (`final_output` /
`conversation_end`) or the written artifact — never a self-summary / proxy
field. See [`zicato-design-boards`](../zicato-design-boards/SKILL.md) and
[`zicato-audit-board`](../zicato-audit-board/SKILL.md).

### An expectation may now return a continuous per-entry score

An OUTCOME `expectation` is no longer strictly pass/fail. A `predicate`-family
matcher backed by a **scorer** callable may return a *continuous* score — a
float in `[0.0, 1.0]` (an F1, a similarity, a partial-credit rubric) — and an
optional `metrics` decomposition (e.g. `{"precision": .., "recall": ..}`). The
matcher clamps the score to `[0,1]` and records it on `ExpectationResult.score`
(and `metrics`), while `passed` keeps a thresholded bit for display/back-compat.
A plain `bool` matcher leaves `score=None`, and the reducer then derives the
score as `1.0`/`0.0` from `passed`, so a binary board is byte-identical to the
pre-score behaviour. Both `score` and `metrics` are carried out to `loss.json`
(see [`zicato-read-telemetry`](../zicato-read-telemetry/SKILL.md)); they feed the
proposer's failure-mode profile. The *scalar/drift scoring formula* over these
numbers is owned by [`zicato-tune-scoring`](../zicato-tune-scoring/SKILL.md) —
design here decides *whether a behavior wants a graded score vs a binary
verdict*, not the weights.

## How drift telemetry becomes loss

The scalar's drift-loss half is a weighted sum over the run's **drift counts**,
bucketed by `(kind, severity)`. zicato consumes goldfive's drift taxonomy as
its loss signal — the registered kinds include `tool_error`, `plan_divergence`,
`looping_reasoning`, `looping_tool_call`, `intent_divergence`, `off_topic`,
`confabulation_risk`, `hallucination_suspected`, `schema_violation`,
`capability_mismatch`, `goal_drift`, `human_intervention_required`, `custom`,
and others (an unknown token is a hard validation error listing the valid set).

Two sources of drift:

1. **Built-in detectors** — goldfive's own detectors fire the first-class
   kinds during a run. Suppress benign ones board-wide with `disable_drift`
   (see [`zicato-design-boards`](../zicato-design-boards/SKILL.md)).
2. **Your custom judges** — every custom judge emits the *single* `custom`
   kind on violation, attributed to its `judge_name`. The reducer folds this
   into a `custom:<judge_name>` bucket so the analyzer can answer "which judge
   drove this run's loss" — but the aggregate `drift_loss` sums all judges into
   one scalar.

The per-run severity-weighted sum for a judge is
`sum(severity_weights[c.severity] * c.count)`, then multiplied by the judge's
weight. Design implication: a judge's *severity* and its *per-judge weight*
both scale its loss, multiplicatively — set severity by *kind of badness*
(critical = qualitatively unacceptable) and per-judge weight by *which judge
matters most this epoch*.

## The weighting knobs (what they do — values live in tune-scoring)

| Knob | Keys on | Designs for |
|---|---|---|
| `severity_weights` | severity (`info`/`warning`/`critical`) | how much worse a critical is than an info (default `1 / 3 / 10`); a missing key scores `0.0` |
| `per_kind_weights` | first-class `DriftKind` token | up-weighting the *built-in* drift kinds that matter for this harness; stacks multiplicatively with severity |
| `per_judge_weights` | judge `name` | telling *custom judges apart* — they all share the `custom` kind, so `per_kind_weights["custom"]` can't distinguish them; this is the only per-judge lever |
| `default_judge_weight` | — | the fallback multiplier for a custom judge absent from `per_judge_weights` (default `1.0`) |
| `namespace_weights` | namespace prefix (`drift:`, `cost:`, `rubric:`, `schema:`, …) | the multi-objective scalar — signed coefficients turning each namespace's per-run mean into a scalar component (positive = higher-is-worse for drift/cost/schema; negative = higher-is-better for rubric quality; zero = tracked but not optimized) |

The two layers compose: a custom judge's contribution is
`severity_weights[severity] × per_judge_weights[name] (or default_judge_weight)`,
and the `drift:` namespace coefficient then scales the aggregate drift term
into the multi-objective scalar. Decide *what to measure and at what severity*
here; pick the *numbers* in
[`zicato-tune-scoring`](../zicato-tune-scoring/SKILL.md).

## The collusion guard — judge/aux callable ≠ harness callable

Everything that grades a run — the rubric matcher, every `inline`/aux-backed
judge, the user-emulator, the proposer, the analysis pass — runs on the
**auxiliary** callable (`--auxiliary-call-llm`). The inner system under test
runs on the **harness** callable (`--harness-call-llm`). zicato hard-errors at
startup (`assert_distinct_callables`, an identity `is` check) if the two are
the same object: a shared callable lets the judge and the harness collude
through shared process state — the judge could perceive the harness's
internals, or the harness could shape the judge's verdict. The rubric matcher
*never sees* the harness callable by construction; the guard makes that
structural. Design implication: always wire two distinct callables, and never
implement a judge that reaches into the harness's state — judge only the
observable event stream / output.

(Identity comparison is intentional: two distinct callables wrapping the same
underlying endpoint pass the check; keeping them genuinely independent is the
operator's responsibility.)

## `judge_only` mode — measure without steering

Setting `judge_only: true` in the `board_meta` header (board-wide) keeps
goldfive *judging* the wrapped agent — drift detectors and your process judges
stay armed — but turns off **all steering**: no goal-derivation call, no
replanning, no drift-triggered refine. Use it when the epoch's question is "how
does the bare harness behave, measured but un-steered?", or when you are
evolving the steerer-free path itself. In this mode your judges and
expectations *are* the entire signal (there is no steerer cleaning up drift
before it's counted), so design them to stand alone. Default is `false`
(steering on). The board-side mechanics are in
[`zicato-design-boards`](../zicato-design-boards/SKILL.md).

## Smells

- A judge that never fires (no `custom:<name>` bucket ever populated) is dead
  weight — it adds no discrimination. Either the criterion is unreachable or
  the behavior never occurs; cut it or sharpen the criterion. Cross-ref
  `non_differentiating_entries` in
  [`zicato-diagnose-health`](../zicato-diagnose-health/SKILL.md).
- An `expected_text` expectation that always matches (or a `regex` with no
  anchors that any plausible output satisfies) is all-pass — it pins pass-rate
  and removes the entry's discrimination.
- Over-weighting one judge so a single violation swamps every other signal
  collapses the multi-objective scalar to one axis. Check the per-judge drift
  attribution in `zicato-tournament-forensics` before piling weight on.
- A rubric or inline judge that disagrees with your eye on the same transcript
  means the criterion is ambiguous — tighten the natural-language `body`, or
  switch to a `python` judge / `predicate` you can pin down.
