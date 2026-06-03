# Vocabulary

A short glossary of zicato's load-bearing terms. Each entry has one
paragraph of definition followed by a cross-link to the document that
covers it in depth. Use this as a disambiguation guide when terms
collide ("is a *generation* a *snapshot* or a *run*?") and as an
on-ramp before reading the deeper docs.

The terms are listed alphabetically.

## Adapter

The `HarnessAdapter` protocol implementation that decouples zicato
from any specific multi-agent framework. The adapter is what zicato
talks to; the inner harness is what the adapter wraps. v0 ships a
concrete ADK adapter; LangChain and plain-callable adapters follow.
An adapter has exactly two methods of interest to zicato:
`run_entry(entry, sinks=[...])` and `mutation_points()`. See
[ARCHITECTURE.md §4.1](ARCHITECTURE.md#41-harnessadapter).

## Applier

The component that takes an `Experiment`, resolves every patch's
`mutation_point_id` to its source location, rewrites the span or
file, and validates the result before publishing a new candidate
generation snapshot. The applier enforces the validator constraints
V1-V7 (see [MUTATION-SURFACE.md §6](MUTATION-SURFACE.md#6-validator-constraints)).
A patch that fails any validator is rejected; the applier does not
silently fix anything. See [ARCHITECTURE.md §4.8](ARCHITECTURE.md#48-applier).

## Audit trail

The `zicato:emulator` lane in the goldfive event stream. Every
emulator turn emits a `GoldfiveLLMCallStart` / `GoldfiveLLMCallEnd`
pair on this lane carrying the persona hash, the transcript chars
in, the emulator's output, and the model identity. Operators replay
the lane in harmonograf to see what the emulator did during a run.
See [EMULATOR.md §8](EMULATOR.md#8-audit-trail-the-zicatoemulator-lane).

## Auxiliary `call_llm`

One of the two required `call_llm` callables. Used by everything
zicato itself runs: the patch proposer, the analysis pass, the
multi-turn user emulator, `rubric`-kind outcome checks (the LLM
grader). MUST be distinct from `harness_call_llm` by callable
identity or by explicit model override. zicato refuses to start if
the two are identical. See
[EMULATOR.md §3](EMULATOR.md#3-the-two-callable-rule).

## Board

The frozen-per-epoch list of tasks the inner harness is evaluated
against. One JSONL file per epoch at
`.zicato/epochs/{epoch}/board.jsonl`, one entry per line. Three entry
kinds today (`single_turn`, `multi_turn_scripted`,
`multi_turn_emulated`) with an open-ended discriminator so future
kinds drop in without schema breakage. Mid-epoch edits change the
evaluation contract and require explicit acknowledgment. See
[BOARD-FORMAT.md](BOARD-FORMAT.md).

## Board entry

One task on the board. Carries an `id`, a `kind` discriminator, a
`wall_clock_budget_seconds`, an optional `weight`, optional `tags`,
and two evaluation facets — an `expectations` list (outcome checks)
and a `judges` list (process checks). Per-kind fields fill in the
rest (single-turn entries carry `input`; multi-turn scripted entries
carry `turns`; multi-turn emulated entries carry `user_persona`). The
runner uses the entry id as a directory name under `runs/`. See
[BOARD-FORMAT.md §1](BOARD-FORMAT.md#1-common-fields) and
[BOARD-AUTHORING.md](BOARD-AUTHORING.md).

## Drift loss

A weighted scalar per run, computed by the loss reducer from the
goldfive event stream. Weighted sum across drift counts (by kind,
by severity), plan revisions, task failure ratio, runtime over budget,
and abort. Always available; works without ground-truth expectations.
Combines with pass-rate into the generation score. See
[SCORING.md §2](SCORING.md#2-per-entry-drift-loss).

## Epoch

The unit of evaluation contract. Within an epoch, the board, the
proposer brief's `## Forbidden` list, and the scoring weights are
frozen. Generations inside an epoch are directly comparable; cross-epoch
comparison is fuzzy by design. Pattern aggregates reset at epoch
boundaries. An epoch is closed by the operator (`zicato epoch close`)
or auto-closed on `zicato epoch new` with a warning. See
[EPOCHS-AND-JOURNALING.md](EPOCHS-AND-JOURNALING.md).

## Experiment

The proposer's output. A typed shape carrying a mandatory structured
**hypothesis** (core_idea, modulating, why, expected_drift_movements,
expected_pass_rate_delta, risks) plus a list of **patches** that
test it. After the tournament concludes, an `outcome` block is
appended to the same `experiment.json` recording actual drift
movements, hypothesis match, score deltas, and tournament decision.
See [EPOCHS-AND-JOURNALING.md §3](EPOCHS-AND-JOURNALING.md#3-the-experiment).

## Experiment memory

The capped, curated digest of **prior experiments** surfaced to the
proposer so it stops re-proposing known failures and builds on known
wins — the fix for the proposer's memoryless greedy hill-climb. Each
entry carries the experiment's `core_idea`, the mutation-point ids it
touched, its verdict (`promoted` / `rejected` / `deferred` / in-flight),
its rejection reason, and its signed Δscalar. Two scopes: settled
cross-round history (read from the analytical index's `experiments`
table) and intra-round sibling awareness (the hypotheses of the other
challengers minted this round in a multi-challenger field, no outcomes
yet). Rendered in the proposer's `## What's already been tried` user-
prompt section. Advisory, not a constraint, and scoped to the current
evaluation contract. Distinct from a *pattern* (a present-tense loss
aggregate) and from the *proposer brief* (the operator's static
steering). See [EXPERIMENT-MEMORY.md](EXPERIMENT-MEMORY.md).

## Expectation

An **outcome** check on a board entry — a matcher run post-hoc on the
run's output or transcript. An entry's `expectations` list holds
zero or more. Five kinds: `predicate` (Python callable),
`expected_text` (exact string), `regex` (Python regex), `json_schema`
(JSON-schema), and `rubric` (LLM-graded via `auxiliary_call_llm`).
Authored with the `Predicate` and `Rubric` namespaces. The loss
reducer ANDs the list into `pass_fail: bool`; an empty list →
drift-loss-only scoring for the entry. Distinct from a *judge* (a
process check). See
[BOARD-FORMAT.md §3](BOARD-FORMAT.md#3-outcome-checks-the-expectations-list).

## Judge

A **process** check on a board entry — a goldfive judge that watches
the agent's reasoning stream in-run, as distinct from an *expectation*
(which inspects the finished output post-hoc). An entry's `judges`
list holds zero or more. Authored with the `Judge` namespace:
`Judge.custom(name, criterion, ...)` for an inline natural-language
judge, `Judge.python(name, dotted_path, ...)` for a programmatic one.
A violation emits a `DriftKind.CUSTOM` drift identified by the judge's
`name` (carried as `judge_name`). goldfive's own built-in judges are
ambient and default-on; a board's `disable_drift` suppresses built-ins
by `DriftKind`. See
[BOARD-FORMAT.md §4](BOARD-FORMAT.md#4-process-checks-the-judges-list)
and [BOARD-AUTHORING.md §3](BOARD-AUTHORING.md).

## Predicate

The namespace of static factory helpers for the deterministic
(non-LLM) outcome-check kinds: `Predicate.contains`, `Predicate.regex`,
`Predicate.schema`, `Predicate.python`. Each returns an `Expectation`
ready to attach to a board entry's `expectations` list. Paired with
`Rubric` (the LLM-graded outcome check). See
[BOARD-AUTHORING.md §2](BOARD-AUTHORING.md).

## Proposer brief

The operator-edited markdown file per epoch that steers the proposer
— focus areas, style guidance, and a mechanically-enforced
`## Forbidden` list of mutation-point ids. Read fresh into the
proposer's prompt every round; no caching. Formerly called the epoch
"rubric"; renamed so "rubric" refers only to the per-entry
`Rubric.score()` outcome check. Distinct from that per-entry rubric:
the proposer brief steers the proposer epoch-wide, a `Rubric` grades
one entry's output. See
[EPOCHS-AND-JOURNALING.md §7](EPOCHS-AND-JOURNALING.md#7-the-proposer-brief).

## Generation

A specific version of the inner harness's source. Named `vN` within
an epoch (`v0`, `v1`, ...). `v0` is the baseline (the registered
source at epoch start, or the final promoted generation of the
previous epoch). Each subsequent generation is the result of an
applier writing a candidate snapshot from an `Experiment`. A
generation's directory holds its `snapshot/`, its `experiment.json`,
its per-entry `runs/`, and its `gen_score.json`. See
[ARCHITECTURE.md §5](ARCHITECTURE.md#5-storage-layout).

## Harness `call_llm`

One of the two required `call_llm` callables. Used by the inner
harness only — `goldfive.wrap`'s `call_llm=` parameter routes through
it to the agent code. MUST be distinct from `auxiliary_call_llm`.
See [EMULATOR.md §3](EMULATOR.md#3-the-two-callable-rule).

## Hypothesis

The mandatory structured pre-run portion of an `Experiment`. Six
required fields: `core_idea` (one sentence), `modulating` (list of
mutation-point ids), `why` (the pattern observation), `expected_drift_movements`
(per-kind direction + magnitude), `expected_pass_rate_delta` (low-high
band), `risks` (list of plausible failure modes). Schema-invalid
proposer responses are rejected; the proposer is re-prompted with the
error. The hypothesis is the load-bearing journaling decision —
without it, journals degenerate into "what changed, what scored". See
[EPOCHS-AND-JOURNALING.md §3.1](EPOCHS-AND-JOURNALING.md#31-hypothesis-schema-mandatory).

## Inner harness

The multi-agent system zicato wraps. Any shape (single agent,
coordinator + specialists, deep sub-agents) — zicato treats it as a
black box behind a `HarnessAdapter`. The inner harness emits a
`goldfive.v1.Event` stream when wrapped with `goldfive.wrap` (the
adapter is responsible for the wrapping). See
[ARCHITECTURE.md §1](ARCHITECTURE.md#1-what-zicato-is-and-why).

## Instance

A zicato workspace keyed by `instance_id`. The default instance is
`default`; nested zicato setups (target 3 — see
[DOGFOOD-TARGETS.md §3](DOGFOOD-TARGETS.md#3-target-3--zicato-itself))
key by distinct ids so outer and inner zicato workspaces don't
cross-talk. `instance_id` is carried in `.zicato/config.json` today
(written by `zicato init --instance-id`). The planned per-instance path
materialization under `.zicato/instances/{instance_id}/` is **not yet
shipped** — there is no per-command `--instance` selector. See
[DOGFOOD-TARGETS.md §3.5](DOGFOOD-TARGETS.md#35-the-recursion--instance_id-need).

## Journal

The running narrative of an epoch. Markdown file at
`.zicato/epochs/{epoch}/journal.md`. Appended every round with the
`core_idea`, `drift_loss_delta`, `pass_rate_delta`, and
`tournament_decision`. Human-readable; read the file directly (there
is no `zicato journal` command — open `journal.md` or view it in the
dashboard). See [EPOCHS-AND-JOURNALING.md §4](EPOCHS-AND-JOURNALING.md#4-the-journal-running).

## Lineage

The cross-epoch DAG of generations. Stored as one file at
`.zicato/lineage.json` recording every epoch's id, start/close
timestamps, promoted versions, rejected versions, and the parent
generation in the previous epoch. Rendered by `zicato epoch list`.
See [EPOCHS-AND-JOURNALING.md §6](EPOCHS-AND-JOURNALING.md#6-lineage).

## Loss profile

The reducer's typed output per run. A dataclass / JSON document
written to `loss.json` next to the per-run `events.jsonl`. Carries
identity (entry_id, epoch_id, generation, tags), drift features
(counts by kind, by severity, escalations, plan revisions, task
failure ratio), multi-turn features (turn count, per-turn drift
counts, stopped_reason), runtime features (runtime_ms, aborted,
abort_reason), and derived fields (drift_loss, pass_fail) plus a
`per_judge_loss` attribution (the weighted-loss contribution of each
named judge, keyed by `judge_name`). The contract every other zicato
component reads from. See
[TELEMETRY.md §3](TELEMETRY.md#3-lossprofile).

## Mutation point

An annotated source location the proposer is allowed to rewrite.
Marked with a `# zicato:mutable id="..."` comment (span marker) or
`# zicato:mutable file id="..."` comment (file marker). Carries a
stable id, a kind (`span` or `file`), a source location, and the
current text. Returned by `HarnessAdapter.mutation_points()`. See
[MUTATION-SURFACE.md](MUTATION-SURFACE.md).

## Outcome

The post-run portion of an `Experiment`. Appended to the same
`experiment.json` after the tournament concludes. Records actual
drift movements, per-movement hypothesis match, drift_loss_delta,
pass_rate_delta, tournament decision, rejection reason if any, and
wall-clock seconds. The single audit trail for why a candidate was
or was not promoted. See
[EPOCHS-AND-JOURNALING.md §3.3](EPOCHS-AND-JOURNALING.md#33-outcome-written-after-the-run).

## Patch

A typed rewrite addressed by mutation-point id. Carries
`mutation_point_id` (must resolve to a current mutation point) and
`new_text` (the post-patch text). One or more patches make up an
`Experiment`'s patches list. Patches are applied by the applier and
validated against constraints V1-V7. See
[MUTATION-SURFACE.md §6](MUTATION-SURFACE.md#6-validator-constraints).

## Pattern

A typed aggregation across loss profiles in the current epoch.
Pattern detectors read every `loss.json` so far in the epoch and
emit `Pattern` objects with kinds like `drift_concentration_by_kind`,
`tag_slice_regression`, `multi_turn_memory_failure`, `unmoved_surface`.
Reset on epoch boundaries (the contract changed). Read by the
proposer. See [TELEMETRY.md §6](TELEMETRY.md#6-patterns-what-aggregates-across-runs).

## Persona

The shape that drives a multi-turn-emulated entry's emulator. Three
fields: `goal` (what the simulated user is trying to accomplish),
`constraints` (behavioural rules), `stop_when` (the condition that
ends the conversation). The emulator's system prompt is built from
the persona; the emulator sees only the persona and the user-facing
transcript. See [BOARD-FORMAT.md §2.3](BOARD-FORMAT.md#23-multi_turn_emulated).

## Proposer

The component that reads patterns and the operator-edited proposer
brief and emits an `Experiment` (hypothesis + patches). Driven by
`auxiliary_call_llm`. Schema-enforced output. See
[ARCHITECTURE.md §4.7](ARCHITECTURE.md#47-patch-proposer).

## Reducer

The post-run function that walks one `events.jsonl` and produces one
`LossProfile`. Function, not sink — the right shape for derivation
work because it has full visibility over the run's events. Testable
in isolation via fixture JSONLs. See [TELEMETRY.md §2](TELEMETRY.md#2-the-post-run-reducer).

## Round

One iteration of the meta-loop within an epoch. Sequence: run parent
against the board → analyze → propose → apply → run candidate against
the board → tournament → journal + outcome. A round either promotes
the candidate (the generation counter advances) or rejects it (the
parent stays; the next round proposes again). Round numbers are
global within an epoch. See [EPOCHS-AND-JOURNALING.md §8](EPOCHS-AND-JOURNALING.md#8-round-mechanics).

## Rubric

The namespace of the single static factory helper `Rubric.score()`,
which builds an LLM-graded **outcome** check: the grader scores the
run's output (or transcript) against an operator-supplied criterion on
a numeric scale, and the resulting `Expectation` passes iff the score
meets a threshold. `reads=OutputScope.FINAL|TRANSCRIPT` selects the
slice graded. Paired with `Predicate` (the deterministic outcome
checks). Formerly `Rubric.judge()` — renamed so "judge" refers only
to the in-run process check. Not to be confused with the *proposer
brief* (the epoch-level steering document, once also called a
"rubric"). See [BOARD-AUTHORING.md §2](BOARD-AUTHORING.md).

## Run

One execution of the inner harness against one board entry, captured
as one `events.jsonl` file at
`.zicato/epochs/{epoch}/generations/v{N}/runs/{entry_id}/events.jsonl`.
A run terminates with `goldfive.v1.RunCompleted` or `RunAborted`.
After termination, the reducer writes `loss.json` next to the
events. Not to be confused with a *round* (one round contains many
runs — one per entry per generation). See
[TELEMETRY.md §1.1](TELEMETRY.md#11-wiring-per-run).

## Snapshot

The directory under `generations/v{N}/snapshot/` holding the full
source of the inner harness at this generation. A full copy, not a
git ref. Self-contained: copying or deleting a snapshot does not
affect any other generation. See
[ARCHITECTURE.md §5](ARCHITECTURE.md#5-storage-layout).

## Tournament

The component that compares champion (parent) vs challenger (child)
generations on the frozen-for-this-epoch board and applies the
promotion gate. The standalone `zicato tournament` command defaults to
`--mode full` (re-run every entry against both generations); `--mode
fast` skips the parent re-run and uses the parent's historical
aggregate (less rigorous; faster). Note `zicato evolve` defaults to
`fast`. The gate requires (a) the challenger beats the champion by
`promote_margin` on the scalar score and (b) strict pass-rate
monotonicity on pre-existing entries (the challenger must not regress
any pre-existing pass).
See [SCORING.md §5](SCORING.md#5-the-tournament-promotion-gate).
