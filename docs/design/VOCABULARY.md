# Vocabulary

The glossary of zicato's terms, and the one place a term is defined.
Every entry gives the idea in plain words, then the term, then one
instance of it, and closes with a link to the document that covers it in
depth. Read it before the deeper design documents, and return to it when
two terms seem to name one thing (a *generation* against a *snapshot*, a
*champion* against a *parent*).

The developer guide's quick reference
([`docs/dev-guide/01-orientation.md`
§2.0](../dev-guide/01-orientation.md#20-quick-reference-term--owning-type--owning-file))
maps each term here to the symbol and file that own it in code. That
table carries no definitions; this file carries no code map. A test
(`tests/test_vocabulary_glossary.py`) checks that every term the table
lists resolves to a heading here.

Seven mechanisms carry the word *gate*. Each is listed under its
qualified name — promotion gate, champion gate, evidence gate, regression
gate — and the [Gate](#gate) entry tells them apart. User-facing text
always uses the qualified name.

The terms are listed alphabetically.

## Adapter

The protocol implementation that separates zicato from any particular
agent framework: the `HarnessAdapter`. Zicato talks to the adapter; the
adapter wraps the system under test. An adapter has two methods zicato
depends on: `run_entry(entry, sinks=[...])`, which executes one board
entry, and `mutation_points()`, which lists the source locations the
proposer may rewrite. The one adapter in the tree
(`src/zicato/adapters/adk.py`) targets the agent development kit (ADK);
another framework needs its own implementation of the protocol. See
[ARCHITECTURE.md §4.1](ARCHITECTURE.md#41-harnessadapter).

## Adaptive holdout query

One consultation of the hidden holdout slice about a challenger that was
chosen using earlier tournament results. The word *query* comes from
adaptive data analysis, where an optimization process asks a statistical
question of evaluation data it has already used. One crowning holdout
comparison is one query, even when it evaluates several board entries
and makes several model or tool calls. Training runs, model calls,
tokens, wall-clock limits, and database reads are accounted separately.
The [Ladder](#ladder) charges each query against an epoch-level budget,
because repeated confirmation feedback lets later challengers overfit the
hidden slice. See
[OVERFITTING.md §"What query budget means"](OVERFITTING.md#what-query-budget-means).

## Analytical index

A SQLite database at `.zicato/index.db` that is rebuilt from the files
in the workspace, so that a question spanning many runs is one query
rather than a walk over every generation directory. Tables: `epochs`, `generations`,
`experiments`, `patches`, `runs`, `loss_profiles`, `metric_counts`,
`tournaments`, `judge_losses`, and the Pareto frontier. The files are the
record; the index holds no fact that is absent from them, so deleting it
costs only a rebuild (`zicato repair index`). The loop writes to it as
rounds settle, and the dashboard and the CLI read it. See
[ANALYTICAL-INDEX.md](ANALYTICAL-INDEX.md).

## Applier

The component that turns an experiment into a candidate generation. It
takes an `Experiment`, resolves every patch's `mutation_point_id` to its
source location, rewrites the span or file, validates the result, and
publishes a new candidate snapshot. A patch that fails any validator
check is rejected; the applier repairs nothing. See
[ARCHITECTURE.md §4.8](ARCHITECTURE.md#48-applier) and
[MUTATION-SURFACE.md §6](MUTATION-SURFACE.md#6-validator-constraints).

## Audit trail

The `zicato:emulator` lane in the goldfive event stream. Every emulator
turn emits a `GoldfiveLLMCallStart` / `GoldfiveLLMCallEnd` pair on this
lane carrying the persona hash, the transcript characters in, the
emulator's output, and the model identity. An operator replays the lane
in harmonograf to see what the simulated user said during a run. See
[EMULATOR.md §8](EMULATOR.md#8-audit-trail-the-zicatoemulator-lane).

## Best-of-N

Sampling several candidate experiments in one propose step and choosing
one of them, instead of taking the first valid proposal. A single
proposer call takes one sample and retries only when the output is
invalid, so without best-of-N a valid but mediocre proposal is never
reconsidered. With the default `proposer_quality.best_of_n = 3`, each
propose step samples three candidates, each steered by a different
edit-class hint, and a self-critique pass picks one against a quality
bar: grounding in a tool call, a real targeted failure mode, and a
minimal diff. Pinning `best_of_n: 1` runs a single sample with no
critique. When [screening](#screening) is enabled it runs between the
sampling and the choice. See
[PROPOSER.md](PROPOSER.md) and `src/zicato/proposer/best_of_n.py`.

## Board

The frozen list of tasks the system under test is evaluated against.
One JSONL file per epoch at `.zicato/epochs/{epoch}/board.jsonl`, one
entry per line. Three entry kinds (`single_turn`,
`multi_turn_scripted`, `multi_turn_emulated`) sit behind an open-ended
discriminator, so a further kind drops in without a schema break. An
edit to the board changes the evaluation contract and rolls the epoch.
See [BOARD-FORMAT.md](BOARD-FORMAT.md).

## Board entry

One task on the board. It carries an `id`, a `kind`, a
`wall_clock_budget_seconds`, an optional `weight`, optional `tags`, and
two lists of checks: `expectations` (outcome checks) and `judges`
(process checks). Per-kind fields fill in the rest: a single-turn entry
carries `input`, a scripted multi-turn entry carries `turns`, an
emulated multi-turn entry carries `user_persona`. The runner uses the
entry id as a directory name under `runs/`. See
[BOARD-FORMAT.md §1](BOARD-FORMAT.md#1-common-fields) and
[BOARD-AUTHORING.md](BOARD-AUTHORING.md).

## Board reflection

Treating the evaluation contract as a measuring instrument and checking
the instrument itself. The evolve loop trusts the board, the scoring
weights, the judges, and the promotion gate as an oracle. Board
reflection measures four properties of that oracle:

- whether it is noisy — the [noise floor](#noise-floor), judge
  self-consistency, and the decision-flip probability;
- whether it separates candidates — [discrimination](#discrimination)
  and the [detectable effect](#detectable-effect);
- whether its judges fire on the behaviour they claim to watch — an
  independent meta-judge adjudicates transcripts;
- whether its margin and weights fit the measured noise —
  [calibration](#calibration).

It reports findings and recommends edits;
nothing it produces changes the contract or reaches the proposer. Run by
`zicato inspect reflection`. See
[BOARD-REFLECTION.md](BOARD-REFLECTION.md).

## Board split

The division of the board into a training slice and a holdout slice.
Every selection decision and every promotion-gate decision reads the
training slice; the holdout slice is used only for
[holdout confirmation](#holdout-confirmation) of a crowning. Entries
tagged `holdout` are always in the holdout slice; otherwise the split is
drawn once per epoch from a rotation seed. The proposer sees neither the
holdout entries nor their results. A board too small to split runs as
`train = full board`, which scores byte-identically to running with no
split. See [OVERFITTING.md §3](OVERFITTING.md#3-train--validation--test-splits--cross-validation).

## Board unit

The smallest thing the tournament evaluates: one generation run against
one board entry at one replicate index, written
`(generation, entry, replicate)`. Under a fixed contract a unit's result
is immutable, so it is evaluated at most once and its `loss.json` is
reused by every pairing, round, and structure that needs it. That reuse
is what `zicato evolve --mode fast` means. The scheduler admits units
per board entry: in full mode a pairing's champion unit and challenger
unit for one entry run together, so `parallelism` entries in flight are
up to `2 × parallelism` units. See
[SCORING.md §7](SCORING.md#7-fast-mode-and-the-tournament) and
`src/zicato/tournament/unit_cache.py`.

## Calibration

Checking a setting of the evaluation against the evaluation's measured
behaviour. The word is used in three places, each with the instance
named:

- **Noise-floor calibration.** Evaluating the champion against itself to
  measure the [noise floor](#noise-floor). Run by `zicato board audit`
  and by the epoch-open step the console shows as "calibrating noise
  floor"; the draws sit at replicate indices 1000 and above.
- **The calibration pillar of board reflection.** Recommendations on
  the promote margin against the noise floor, on loss terms that never
  move the scalar or swamp the rest, and on judges that duplicate one
  another. Findings from it carry `pillar: calibration`.
- **Critic calibration.** Feeding each settled hypothesis's predicted
  outcome and its recorded outcome back to the proposer's critic, so an
  overconfident proposer is visible as a trend
  ([PROPOSER.md §2.8](PROPOSER.md#28-the-critic-calibration-channel--feeding-prediction-accuracy-back)).

See [BOARD-REFLECTION.md](BOARD-REFLECTION.md#what-reflection-measures--four-pillars).

## Challenger

A candidate generation competing to replace the [champion](#champion).
In the default gauntlet a round mints one challenger; a wider
[field](#field) mints several, which compete under the epoch's
[tournament structure](#tournament-structure) until one reaches the
champion gate. Lineage calls the same generation the *child*, because it
was derived from the champion's snapshot. Tournament text says
challenger; lineage text says child. See
[SELECTION.md §3](SELECTION.md#3-where-zicato-sits-today-the-king-of-the-hill-gauntlet).

## Champion

The generation that stands as the head of the epoch: the one every
challenger is measured against and the one the next round proposes
from. It is replaced only when a challenger clears the promotion gate,
and it keeps its birth round when carried into later rounds. On a
difference plot the champion's score is the reference line. Lineage
calls the same generation the *parent*. See
[SELECTION.md §3](SELECTION.md#3-where-zicato-sits-today-the-king-of-the-hill-gauntlet).

## Champion gate

The single crowning duel between the reigning champion and the finalist
a tournament structure produced. Single elimination, double
elimination, Swiss pairing, and racing differ only in how they narrow a
field to one finalist; each then ends in this duel, which the
unchanged [promotion gate](#promotion-gate) decides. Narrowing the field changes
who reaches the duel and never the standard applied there. The gauntlet
schedules one duel, which is the champion gate itself. See
[TOURNAMENT-STRUCTURES.md §3](TOURNAMENT-STRUCTURES.md#3-the-five-concrete-strategies)
and `src/zicato/selection/strategies/champion_gate.py`.

## Contract

Everything that decides what a score means: the board, the proposer
brief, the scoring weights, zicato's evaluator revision, the registered
adapter and its source outside the mutable trees, the registered
mutable-tree paths, and the proposer's identity, tools, and skills. Its
identity is the *contract hash*. An [epoch](#epoch) is one contract held
still; a change to any input rolls a fresh epoch at the next `evolve`,
because scores measured under two contracts are not comparable. The
source content of the system under test is outside the contract: it is
what zicato changes within an epoch. Also called the *evaluation
contract*. See
[EPOCHS-AND-JOURNALING.md §10](EPOCHS-AND-JOURNALING.md#10-contract-hash-auto-epoching).

## Control protocol

How an operator acts on a running loop: a command file written under
`.zicato/runtime/control/` by the dashboard, the CLI, or a bare `touch`,
which the loop consumes at its next safe point. Commands include a gate
override and a skip-round. See
[RUNTIME.md §2.5](RUNTIME.md#25-control-and-control_log--operator-action-channel).

## Copeland score

A contestant's count of pairwise duels won. It ranks a field from duels
alone, without a common scale. The Swiss structure orders its standings
by Copeland score, tie-broken by mean scalar, and a bye counts as a win.
The `copeland` [resolver](#resolver) ranks by wins minus losses over the
net margin matrix. A duel whose two sides tie on the scalar counts for
neither side. Copeland is margin-blind: a win by 0.001 and a win by 0.5
each count once. See
[SELECTION-THEORY.md §3.4](SELECTION-THEORY.md#34-copeland).

## Crowning

The final champion-gate decision of a round and its result. The
selection layer records it as a `SelectionDecision` carrying the promoted
generation id (or none), the crowning matchup id, the full matchup
audit, and the standings. The settled bracket and the champion pointer
must agree before any lineage write. See
[TOURNAMENT-STRUCTURES.md §2](TOURNAMENT-STRUCTURES.md#2-the-selectionstrategy-interface).

## Dead letter

The record of a crowning duel the [evidence gate](#evidence-gate) could
neither confirm nor reject: its replicate budget ran out and the two
ratings' confidence intervals still overlapped. One file per such duel
is written under `.zicato/runtime/inconclusive/`, so the champion stands
and the unresolved verdict is visible to the operator and the dashboard
instead of being read later as a rejection. See
`src/zicato/selection/dead_letter.py`.

## Deferral

The verdict of the endpoint-outage circuit. When the number of runs in
a round aborted by infrastructure failure reaches
`infra_abort_round_threshold`, the round defers instead of spending the
experiment: nothing is journaled, the experiment stays without an
outcome for [resume](#resume) to reconcile, and the loop backs off
exponentially. Recorded as `deferred_infra`, which does not count toward
the consecutive-rejection stop. See
[ERROR-HANDLING.md](ERROR-HANDLING.md).

## Detectable effect

The smallest scalar difference the board can be expected to tell apart
from noise, given the measured [noise floor](#noise-floor) and the number
of replicates. The two-sample form is
`(t_{α/2} + t_β) · floor · √(2/n)` with `n` replicates per side. The
console serves it with every input beside it, and reports "floor
unmeasured" or "insufficient replication" rather than a number when an
input is missing. A promote margin below the detectable effect decides
on variance. Also called the *minimum detectable effect*. See
[EVAL-VIEW.md §2.5](EVAL-VIEW.md#25-the-noise-floor-and-the-inputs-to-the-detectable-effect-ladder).

## Discrimination

Whether the evaluation separates one candidate from another. Two
measurements carry the word:

- **Entry discrimination.** Whether a board entry's score moves across
  candidates. An entry that returns the same verdict for every candidate
  carries no information; loop health reports it as
  `non_differentiating_entry`, and board reflection measures it over a
  spread of candidates.
- **Gate discrimination.** Whether the promotion gate tells "no change"
  from "improvement". The [placebo arm](#placebo-arm) measures it: a
  promoted placebo means the gate is promoting noise.

See [LOOP-HEALTH.md §3.2](LOOP-HEALTH.md#32-non-differentiating-board-entry--non_differentiating_entry)
and [BOARD-REFLECTION.md](BOARD-REFLECTION.md#what-reflection-measures--four-pillars).

## Drift

Goldfive's taxonomy of what went wrong while a run was in flight: typed
drift kinds (off-topic output, a skipped tool, a judge's named
violation) at the severities `info`, `warning`, and `critical`. Zicato
registers the valid kind strings in `src/zicato/core/drift_kinds.py`,
and drift events are the raw material of the [drift loss](#drift-loss).
See [SCORING.md §2.1](SCORING.md#21-the-drift-channel).

## Drift loss

A weighted number per run computed by the reducer from the goldfive
event stream: a weighted sum over drift counts (by kind, by severity),
plan revisions, task failure ratio, runtime over budget, and abort. It
is available for every run, including runs on entries that declare no
expectation, and it combines with the pass rate into the generation
[scalar](#scalar). See
[SCORING.md §2](SCORING.md#2-the-metric-channels).

## Epoch

The span during which the [contract](#contract) is frozen. Within an
epoch the board, the proposer brief's `## Forbidden` list, and the
scoring weights do not change, so the generations inside it are
directly comparable; comparison across epochs is approximate by design.
Pattern aggregates reset at the boundary. An epoch is closed by the
operator (`zicato epoch close`), auto-closed on `zicato epoch new`, or
rolled automatically when the contract hash changes. See
[EPOCHS-AND-JOURNALING.md](EPOCHS-AND-JOURNALING.md).

## Evaluation model

The model role for everything zicato itself runs: the proposer, the
analysis pass, the multi-turn user emulator, and `rubric`-kind outcome
checks. It is supplied as the `evaluation_call_llm` callable and named
`evaluation` under `models.roles`. It must differ from the
[target model](#target-model) by callable identity or by an explicit
model override; zicato refuses to start when the two are the same. See
[EMULATOR.md §3](EMULATOR.md#3-the-two-callable-rule) and
[MODEL-CONFIG.md](MODEL-CONFIG.md).

## Evidence gate

An optional check that holds a crowning promotion until the measured
[ratings](#rating) separate it from noise. It fits Bradley–Terry
strengths over the duels already run and returns one of three verdicts
for the crowning pair:

- `promoted` — the probability that the challenger is stronger meets
  `promote_confidence_threshold` and the two confidence intervals do not
  overlap;
- `deferred` — either condition fails and replicate budget remains, so
  the driver replicates the least-resolved duel and refits;
- `inconclusive` — the budget is spent and the intervals still overlap,
  which lands in the [dead letter](#dead-letter) queue.

It is off unless
`promote_confidence_threshold` is set; the scaffold `zicato init` writes
enables it with a stated budget. Its confidence intervals separate only
after an unbroken run of roughly 37 wins, so it protects soundness
rather than adding power. See
[SELECTION-THEORY.md §7.1](SELECTION-THEORY.md#71-bradleyterry) and
`src/zicato/selection/evidence_gate.py`.

## Expectation

An outcome check on a board entry: a matcher run after the run on its
output or transcript. An entry's `expectations` list holds zero or more.
Five kinds: `predicate` (a Python callable), `expected_text` (an exact
string), `regex`, `json_schema`, and `rubric` (graded by the evaluation
model). They are authored with the `Predicate` and `Rubric` namespaces.
The loss reducer combines the list into one `pass_fail`; an empty list
scores the entry on drift loss alone. A [judge](#judge) is the
process-check counterpart. See
[BOARD-FORMAT.md §3](BOARD-FORMAT.md#3-outcome-check-the-expectation-field).

## Experiment

The proposer's output: a mandatory structured [hypothesis](#hypothesis)
plus the list of [patches](#patch) that test it. After the tournament
concludes, an [outcome](#outcome) block is appended to the same
`experiment.json`, so one file records why a candidate was or was not
promoted. See
[EPOCHS-AND-JOURNALING.md §3](EPOCHS-AND-JOURNALING.md#3-the-experiment).

## Experiment memory

The capped digest of earlier experiments shown to the proposer, so it
stops re-proposing known failures and builds on known wins. Each entry
carries the experiment's `core_idea`, the mutation points it touched,
its verdict (`promoted`, `rejected`, `deferred`, or in flight), its
rejection reason, and its signed scalar delta. Two scopes: settled
history from the analytical index's `experiments` table, and the
hypotheses of the other challengers minted this round in a wider field.
It is rendered in the proposer prompt's `## What's already been tried`
section, is advisory, and is scoped to the current contract. A
[pattern](#pattern) is a present-tense loss aggregate and the
[proposer brief](#proposer-brief) is the operator's steering; experiment
memory is neither. See [EXPERIMENT-MEMORY.md](EXPERIMENT-MEMORY.md).

## Facet

A named diagnostic slice of the board. It is declared by tagging
entries `facet:{name}`. The console re-aggregates a candidate over each facet at
the epoch's frozen weights, so a facet's scalar is comparable to the
candidate's own. Facets are display only: nothing is persisted and no
decision reads them. See
[EVAL-VIEW.md §3.4](EVAL-VIEW.md#34-facet_scores_for_generationpaths-epoch_id-generation_id---dict).

## Failure profile

The board-anonymous summary of outcomes rendered into the proposer
prompt: counts and bands of which failure modes occurred, with no entry
id. It is what lets the proposer learn from failure without learning
the board. See
[OVERFITTING.md §11.5](OVERFITTING.md#115-the-outcome-marginal-failure-mode-channel-shipped).

## False promotion

Crowning a challenger that is not better than the champion. The loop
builds every later round on the champion, so one false promotion
corrupts what is proposed after it. Every margin, replicate, holdout
confirmation, and control arm exists to make it rarer, and the
[placebo arm](#placebo-arm) is the standing check that the gate still
rejects a no-op. See
[SELECTION.md §2](SELECTION.md#2-three-families-of-promotion-decision).

## Fast mode

`--mode fast`, the default for `zicato evolve`: every
[board unit](#board-unit) is evaluated at most once and its result is
reused across pairings, rounds, and structures, so only cache misses
run. On the gauntlet the champion's side is a cached aggregate from
earlier rounds, so replicates reduce noise on the challenger's side
only, and repeated rounds are not independent draws of the same
contrast. `--mode full` bypasses the cache and evaluates every unit on
both sides afresh, for noise re-sampling or debugging. The resolved
provenance (`full`, `fast`, or `fast-degraded`) is recorded on the
outcome as `champion_eval_mode`; it is runtime provenance and never
rolls the epoch. See
[SCORING.md §7](SCORING.md#7-fast-mode-and-the-tournament).

## Field

The challengers one round mints and runs together. The gauntlet has a
field of one; the other structures read `field_size` from the
`tournament.params` block (default 2). The round runs as named phases
over every slot of the field: propose and apply, run, gate, decide. Under a
wider field the [placebo arm](#placebo-arm), when due, enters as one
extra slot. See
[TOURNAMENT-STRUCTURES.md §4](TOURNAMENT-STRUCTURES.md#4-the-shared-tournament-config-contract)
and [ROUND-PIPELINE.md](ROUND-PIPELINE.md).

## Field diversity

How much the challengers in one field differ: the pairwise overlap
between the sets of mutation points each one touches, as a Jaccard
ratio. Two challengers that edit the same points answer one question
at the cost of two. An exact duplicate is always soft-rejected; when the
runtime knob `diversity_tolerance` is set, a challenger whose overlap
with an accepted sibling exceeds it is soft-rejected as well, dropped
from the run and recorded with `diversity_status: soft_rejected`. The
summary is shown in the console's tournament view. See
`src/zicato/selection/diversity.py`.

## Gate

A rule that decides whether something proceeds. Several mechanisms carry
the word, and text on any user-facing surface uses the qualified name:

- the [promotion gate](#promotion-gate) — the per-duel rules that decide
  whether a challenger supersedes the champion;
- the [champion gate](#champion-gate) — the crowning duel that ends a
  structure, which the promotion gate decides;
- the [evidence gate](#evidence-gate) — the optional rating-based hold
  on a crowning;
- the [regression gate](#regression-gate) — the candidate's own test
  suite as a veto;
- the [Ladder](#ladder) release threshold — which decides whether a
  holdout confirmation may affect the result;
- the [screening](#screening) veto — which disqualifies a candidate
  before the tournament.

## Gauntlet

The default [tournament structure](#tournament-structure): one challenger
per round meets the standing champion directly in a single duel that is
the [champion gate](#champion-gate). A challenger that clears the
promotion gate becomes the champion; otherwise the champion stands and
the next round proposes again. Laid beside elitist iterated racing, the
mature algorithm for this problem, the correspondence is close, which is
why it is the default. See
[SELECTION.md §4](SELECTION.md#4-the-reframe-this-is-a-degenerate-elitist-iterated-race)
and [TOURNAMENT.md §1](TOURNAMENT.md#1-the-gauntlet-structure).

## Generation

One version of the system under test's source. It is named `vN`
within an epoch. `v0` is the baseline: the registered source at epoch start, or
the final promoted generation of the previous epoch. Each later
generation is the applier's output from one experiment. A generation's
records are its `experiment.json`, its per-entry `runs/`, and its
`gen_score.json`; its source tree lives in the
[generation store](#generation-store). See
[ARCHITECTURE.md §6](ARCHITECTURE.md#6-storage-layout).

## Generation store

Where a generation's source tree is kept. Two backends implement the
`GenerationStore` protocol. In the git backend, the default, a
generation is a commit on an epoch branch in the workspace-private
`repo/`, and a run reads it from a content-addressed `git worktree`. In
the directory backend a generation is a full copy under
`generations/{id}/snapshot/`. Every workspace records its backend in
`config.json` as `generation_source_backend`, and everything above the
store addresses generations by id. See
[STORAGE.md §5.2](STORAGE.md#52-generationstore-protocol-both-backends-behind-it)
and [STORAGE.md §7](STORAGE.md#7-the-git-backed-generation-store-gitgenerationstore).

## Goldfive configuration document

The optional `goldfive` mapping on the scoring weights, which configures
the goldfive runtime an adapter builds for a run. Zicato holds it as an
immutable JSON mapping inside the contract and loads no goldfive schema
of its own; when the selected adapter declares the goldfive integration,
a lazy bridge asks goldfive's `RuntimeConfigDocument` to apply defaults,
validate, and canonicalize it. See
[GOLDFIVE-CONFIG.md](GOLDFIVE-CONFIG.md).

## Heartbeat

The liveness signal under `.zicato/runtime/`: `heartbeat.json`, written
about every two seconds by the running loop, and a progress log whose
sequence number advances only on a real transition (loop start, round
start, propose, tournament start and settle, promote or reject, settled
or stopped). The watchdog supervisor reads the heartbeat to tell a
working loop from a wedged one, and the console reads the progress log.
See [RUNTIME.md §2.2](RUNTIME.md#22-heartbeatjson--orchestrator-pulse).

## Holdout confirmation

The final champion-versus-challenger comparison on the board entries
withheld from the process that chose the challenger. A challenger that
won on the training slice confirms when it does not meaningfully regress
on the holdout slice; a second improvement on the smaller, noisier slice
is not required. Every tournament structure uses the same confirmation
path. A released non-confirmation rejects the challenger; an absent
holdout leaves the training verdict unchanged. See
[OVERFITTING.md §"What query budget means"](OVERFITTING.md#what-query-budget-means)
and [SCORING.md §5](SCORING.md#5-the-tournament-promotion-gate).

## Hypothesis

The half of an [experiment](#experiment) written before the run. Six
required fields: `core_idea` (one sentence), `modulating` (the mutation
points the patches address), `why` (the pattern observation behind it),
`expected_drift_movements` (direction and magnitude per kind),
`expected_pass_rate_delta` (a low–high band), and `risks`. A proposal
whose hypothesis fails the schema is rejected and the proposer is
re-prompted with the error. The hypothesis is written before the run and
the outcome after; that pairing is what makes the journal an experiment
log. See
[EPOCHS-AND-JOURNALING.md §3.1](EPOCHS-AND-JOURNALING.md#31-hypothesis-schema-mandatory).

## Instance

A zicato workspace keyed by `instance_id`, which `zicato init
--instance-id` writes into `.zicato/config.json`. The
default is `default`. Nested setups, where an outer zicato optimizes an
inner one, key each workspace by a distinct id so the two do not
cross-talk. Per-instance path materialization under
`.zicato/instances/{instance_id}/` is unimplemented; there is no
per-command `--instance` selector. See
[DOGFOOD-TARGETS.md §3.5](DOGFOOD-TARGETS.md#35-the-recursion--instance_id-need).

## Journal

The running narrative of an epoch, a Markdown file at
`.zicato/epochs/{epoch}/journal.md`. Every round appends the
`core_idea`, `drift_loss_delta`, `pass_rate_delta`, and
`tournament_decision`. There is no `zicato journal` command; open the
file or view it in the dashboard. See
[EPOCHS-AND-JOURNALING.md §4](EPOCHS-AND-JOURNALING.md#4-the-journal-running).

## Judge

A process check on a board entry: a goldfive judge that watches the
agent's reasoning stream while the run happens. An
[expectation](#expectation) inspects the finished output afterwards; a
judge watches the process. An entry's `judges` list holds zero or more,
authored with the `Judge` namespace: `Judge.custom(name, criterion, ...)`
for a natural-language criterion and `Judge.python(name, dotted_path,
...)` for a programmatic one. A violation emits a `DriftKind.CUSTOM`
drift carrying the judge's `name`. Goldfive's built-in judges are on by
default; a board's `disable_drift` suppresses them by `DriftKind`. See
[BOARD-FORMAT.md §4](BOARD-FORMAT.md#4-process-checks-the-judges-list).

## Ladder

The governor that limits how much feedback the loop receives from the
holdout slice. A holdout confirmation can affect promotion only when the
challenger's training improvement clears the Ladder release threshold,
and each consultation consumes one unit of an epoch-level query budget.
A withheld query reuses the last released confirmation and still
consumes budget, because the holdout was inspected. After the budget is
exhausted the training verdict stands. The proposer never receives raw
holdout entries or per-entry results. Configured by `LadderConfig`. See
[OVERFITTING.md §"What query budget means"](OVERFITTING.md#what-query-budget-means).

## Lineage

The graph of generations across epochs, held in one atomically
rewritten file at `.zicato/lineage.json`. It records every epoch's id,
its start and close timestamps, its promoted and rejected generations,
and the parent generation in the previous epoch. A generation's
`promoted` field has three states: `true`, `false` (a rejected dead
branch), and `null` (applied and still competing). Rendered by
`zicato epoch list`. See
[EPOCHS-AND-JOURNALING.md §6](EPOCHS-AND-JOURNALING.md#6-lineage).

## Loop health

Detectors that report whether the evaluation still carries optimization
signal. A board every generation passes completely, or fails completely,
produces rounds and journal entries while teaching the proposer nothing.
Findings include `degenerate_scoring`, `non_differentiating_entry`,
`flat_drift_signal`, `no_expectations`, `dead_judge`, `judge_erroring`,
`stalled_loop`, `generalization_gap`, `refresh_cadence`, and
`placebo_promoted`; a sustained critical finding stops the loop.
Reported by `zicato health`. See [LOOP-HEALTH.md](LOOP-HEALTH.md).

## Loss profile

The reducer's typed output for one run. It is written to `loss.json`
beside the run's `events.jsonl`. It carries identity (entry id, epoch
id, generation, tags), drift features (counts by kind and severity,
escalations, plan revisions, task failure ratio), and multi-turn
features (turn count, per-turn drift counts, stop reason). It also
carries runtime features (runtime, aborted, abort reason), the derived
`drift_loss` and `pass_fail`, and a `per_judge_loss` attribution keyed
by judge name.
Scoring and pattern detection read loss profiles and never the raw
events. See [TELEMETRY.md §3](TELEMETRY.md#3-lossprofile).

## Mutation point

An annotated source location the proposer may rewrite. It is marked
with a `# zicato:mutable id="..."` comment for a span or a
`# zicato:mutable file id="..."` comment for a whole file. It carries a
stable id, a kind (`span` or `file`), a source location, and the current
text. `HarnessAdapter.mutation_points()` returns the list. The set of
registered mutable trees is part of the contract; the content of those
trees is what the loop changes. See
[MUTATION-SURFACE.md](MUTATION-SURFACE.md).

## Noise floor

What a scalar difference of zero looks like on this board. Evaluations
vary between runs, so the floor is measured by evaluating the champion
against itself several times and reading the spread of the resulting
`delta_scalar` values: any two draws form a comparison whose true
difference is zero. A `promote_margin` below the floor promotes and
rejects on variance, so the floor is what says whether a margin means
anything. Measured by `zicato board audit` and at epoch open, persisted
on the epoch record as `noise_floor`, and never part of the contract.
See [SCORING.md §4.1](SCORING.md#41-default-weights-and-the-calibration-problem)
and `src/zicato/tournament/calibration.py`.

## Outcome

The half of an [experiment](#experiment) written after the tournament.
Appended to the same `experiment.json`, it records the actual drift
movements, whether each matched the hypothesis, `drift_loss_delta`,
`pass_rate_delta`, the tournament decision, the rejection reason if any,
the wall-clock seconds, and the runtime evidence (holdout block,
train-versus-holdout loss, evidence-gate resolution, `champion_eval_mode`).
See
[EPOCHS-AND-JOURNALING.md §3.3](EPOCHS-AND-JOURNALING.md#33-outcome-written-after-the-run).

## Pareto frontier

The per-epoch record of settled candidates the scalar threw away: those
that beat the reigning champion on at least one scoring axis and that
nothing else on the record dominates on every axis. A weighted sum is a
projection, so a challenger that halves cost for a small loss in rubric
score loses on the scalar and is otherwise never mentioned again. The
axes are the non-zero `namespace_weights`; admission reuses the
promotion gate's namespace-monotonicity rule. The record is written
after a round's decision is final, enters no decision, and is shown in
the console. See [PARETO-FRONTIER.md](PARETO-FRONTIER.md).

## Patch

One typed rewrite addressed by mutation-point id. It carries
`mutation_point_id`, which must resolve to a current mutation point, and
`new_text`. One or more patches make up an experiment's patches list;
the applier applies them and the validator checks the result before and
after. See
[MUTATION-SURFACE.md §6](MUTATION-SURFACE.md#6-validator-constraints).

## Pattern

A typed aggregation across the loss profiles recorded so far in the
epoch. Detectors read every `loss.json` and emit `Pattern` objects with
kinds such as `drift_concentration_by_kind`, `tag_slice_regression`,
`multi_turn_memory_failure`, and `unmoved_surface`. Patterns reset at
epoch boundaries because the contract changed, and the proposer reads
them. See
[TELEMETRY.md §6](TELEMETRY.md#6-patterns-what-aggregates-across-runs).

## Persona

What drives the simulated user of a multi-turn emulated entry. Three
fields: `goal` (what the user is trying to accomplish), `constraints`
(behavioural rules), and `stop_when` (the condition that ends the
conversation). The emulator's system prompt is built from the persona,
and the emulator sees only the persona and the user-facing transcript.
See [BOARD-FORMAT.md §2.3](BOARD-FORMAT.md#23-multi_turn_emulated).

## Placebo arm

An optional control challenger whose patch re-emits a mutation point's
current text unchanged, so the candidate behaves like the champion.
Every Nth round (`overfitting.random_baseline_every_n`, off by default)
the loop fields one. A working promotion gate must reject it, because no
improvement can clear `promote_margin` between identical behaviours; a
promoted placebo raises the critical `placebo_promoted` finding, which
means recent wins are suspect. Its hypothesis carries a marker so every
reader treats it as a probe rather than as part of the optimization. See
[OVERFITTING.md §12](OVERFITTING.md#12-the-recommendation-ranked) and
`src/zicato/evolve/placebo.py`.

## Predicate

The namespace of factory helpers for the deterministic outcome-check
kinds: `Predicate.contains`, `Predicate.regex`, `Predicate.schema`, and
`Predicate.python`. Each returns an `Expectation` ready to attach to a
board entry. [Rubric](#rubric) is the model-graded counterpart. See
[BOARD-AUTHORING.md §2](BOARD-AUTHORING.md).

## Preflight

The check of the contract that runs at epoch open, before the first
round spends budget. It measures the noise floor and the degradation
signal (the champion against a copy degraded on purpose), and it
records an `ok`, `warn`, or `refuse` verdict on the epoch record. The console shows
it as "contract pre-flight". It is the same analysis as
[board reflection](#board-reflection) at a cheaper cadence. See
[BOARD-REFLECTION.md](BOARD-REFLECTION.md#the-evolve-pre-flight-checks).

## Process exemplars

Windows of a run's event stream, three events either side of an anchor
drift, that are redacted and shown to the proposer. They come from the
champion's training-slice runs, so the proposer sees how a failure
unfolds without seeing which entry it unfolded on. Opt-in through
`proposer_quality.process_exemplars`. See
[PROCESS-EXEMPLARS.md](PROCESS-EXEMPLARS.md).

## Promotion gate

The rules that decide whether a challenger supersedes the champion,
applied in order to the training-slice aggregates:

1. the challenger's scalar beats the champion's by at least
   `promote_margin`;
2. no entry the champion passed regresses (per-entry or aggregate
   scope);
3. for each namespace whose `namespace_monotonicity` flag is set, the
   namespace does not regress.

An opt-in diff-complexity ceiling can veto before rule
one. Every rule is written as a rejection condition, so a non-finite
number rejects the duel with an `invalid evidence` reason. The gate
reports a reason on every rejection, and every tournament structure
reuses it for each scheduled duel. A holdout confirmation can revise the
result the three rules reach. See
[SCORING.md §5](SCORING.md#5-the-tournament-promotion-gate).

## Proposer

The component that reads the patterns, the experiment memory, and the
proposer brief and emits an [experiment](#experiment). It runs on the
evaluation model, its output must satisfy a schema, and it is part of
the contract: a change to the proposer or its skills rolls the epoch.
See [PROPOSER.md](PROPOSER.md).

## Proposer brief

The operator-edited Markdown file per epoch that steers the proposer:
focus areas, style guidance, and a mechanically enforced `## Forbidden`
list of mutation-point ids. It is read fresh into the proposer's prompt
every round. A [rubric](#rubric) grades one entry's output; the brief
steers the proposer across the epoch. See
[EPOCHS-AND-JOURNALING.md §7](EPOCHS-AND-JOURNALING.md#7-the-proposer-brief).

## Proposer scorecard

A reading of proposal quality assembled from the round logs the loop
already wrote, so it costs no extra evaluation. Per epoch it reports the
validator-failure rate per check, the screen-veto rate, the gate margins
on children that reached the gate, and the promotion rate; across epochs
it reports the trend. Every rate carries its sample count, is null when
nothing was observed, and is marked provisional below a minimum sample.
Rendered by `zicato proposer scorecard`; `zicato proposer reflect` reads
it to draft edits to the proposer's skills. See
[PROPOSER.md §6](PROPOSER.md#6-the-proposer-scorecard--recommend-only-self-reflection).

## Racing

The [tournament structure](#tournament-structure) that gives every
challenger a small budget, cuts the weakest, and repeats with a larger
budget. Rung 0 duels every challenger against the champion on a board
slice (`board_fraction` or `rung0_board_size` of the board). After each
[rung](#rung) the worst `1 − 1/eta` of the field by scalar is cut, and
the survivors re-duel on a larger slice. The last survivor meets the
champion at the champion gate on the full board. Elimination within a
rung is by rank rather than by the promotion gate. The scaffold `zicato
init` writes selects racing with a field of four. See
[TOURNAMENT-STRUCTURES.md §3.5](TOURNAMENT-STRUCTURES.md#35-racing-the-endorsed-bracket-shaped-option).

## Rating

A strength fitted to each contestant from the duels observed, under the
Bradley–Terry model, in which the chance that one contestant beats
another follows the gap between their strengths. The fit also yields a
confidence interval per contestant; contestants whose intervals overlap
are the pairs worth running again. The optional `rating: bradley_terry`
param orders a structure's internal standings by fitted strength; the
[evidence gate](#evidence-gate) reads the same fit; and the analytical
index re-fits it at every ingest for display on the Elo scale
(`1500 + θ·400/ln 10`), where it never touches a decision. See
[SELECTION-THEORY.md §7](SELECTION-THEORY.md#7-rating-from-pairwise-results-the-noise-backbone).

## Reducer

The function that walks one run's `events.jsonl` and produces one
[loss profile](#loss-profile). It is a function rather than an event
sink because derivation needs all of a run's events at once, and it is
testable against fixture streams in isolation. See
[TELEMETRY.md §2](TELEMETRY.md#2-the-post-run-reducer).

## Regression gate

Running the candidate snapshot's own test suite and rejecting the
candidate on any failure, however strong its scoring signal. A patch can
improve the drift loss and the pass rate on the board while breaking an
invariant the board never exercises. Opt-in through
`regression_gate_enabled`; a snapshot with no `tests/` directory under
the mutable tree passes with an explanatory summary, and a suite that
exceeds its wall-clock limit fails as `timeout`. See
`src/zicato/tournament/regression.py`.

## Replicate

One of several runs of the same generation against the same board
entry, at a distinct replicate index. Outputs and model-backed judges
vary between runs, so a single run is one sample; averaging paired
replicates before the gate is what separates a real difference from
that variation. The `replicates` param defaults to 2 for the gauntlet,
the elimination brackets, and Swiss, and to 1 for racing. Replicate
indices are partitioned by purpose so no two purposes share a cache
slot: tournament duels from 0, noise-floor calibration from 1000, the
preflight from 2000, screening from 3000, the evidence gate from 4000,
and board reflection from 5000. See
[SELECTION-THEORY.md §2](SELECTION-THEORY.md#2-the-operating-rule-replicate-first-resolve-second).

## Resolver

What decides an internal leader when pairwise results form a cycle: one
contestant beats a second, the second beats a third, and the third beats
the first. Two values of the optional `resolver` param are accepted:
`copeland`, which ranks by wins minus losses, and `ranked_pairs`, which
locks in the most decisive duels first and skips any that would close a
cycle. Both run behind a Condorcet check (a contestant that beats every
other wins outright) and a Smith-set prune (only the smallest set that
beats everyone outside it is considered); neither of those is selectable
on its own. A resolver only proposes a leader; the champion gate still
decides promotion. See
[SELECTION-THEORY.md §5](SELECTION-THEORY.md#5-ranking--condorcet-completion-methods-the-resolver-tier).

## Resume

What the loop does at start-up when a workspace holds the traces of a
run that died: it replays every pending [settlement
receipt](#settlement-receipt), discards a field that died before its
receipt was written, finalizes stale run records, and reconciles an
experiment left without an outcome by a [deferral](#deferral). See
[RUNTIME.md §4](RUNTIME.md#4-resume-semantics).

## Round

One iteration of the loop within an epoch: run the champion against the
board, analyze, propose, apply, run the field, decide, then journal and
record the outcome. A round either promotes a challenger (the generation
counter advances) or leaves the champion standing, and returns an
`EvolveRoundOutcome` summary. Round numbers are
cumulative within an epoch, and re-running `evolve` continues them. A
racing rung, a bracket round, or a Swiss round is a *stage* within one
round, and is recorded as `stage_index`. See
[EPOCHS-AND-JOURNALING.md §8](EPOCHS-AND-JOURNALING.md#8-round-mechanics).

## Round log

The durable per-round event log at
`epochs/{epoch}/rounds/{round}/round_log.jsonl`: a typed, sequenced,
append-only JSONL trace of the round's full arc, from open through
proposal attempts, apply and validate, board units, gate, holdout and
evidence, the Pareto record, the decision, and close. One writer
appends; a reader tolerates one torn final line. `zicato epoch rounds`
classifies every round from it, and the
[proposer scorecard](#proposer-scorecard) is read from it. See
`src/zicato/epoch/round_log.py`.

## Rubric

The namespace of the single factory helper `Rubric.score()`, which
builds a model-graded outcome check: the evaluation model scores the
run's output or transcript against an operator-supplied criterion on a
numeric scale, and the resulting `Expectation` passes when the score
meets a threshold. `reads=OutputScope.FINAL|TRANSCRIPT` selects the
slice graded. [Predicate](#predicate) is the deterministic counterpart.
See [BOARD-AUTHORING.md §2](BOARD-AUTHORING.md).

## Run

One execution of the system under test against one board entry,
captured as one `events.jsonl` file at
`.zicato/epochs/{epoch}/generations/v{N}/runs/{entry_id}/events.jsonl`.
A run terminates with `goldfive.v1.RunCompleted` or `RunAborted`, after
which the reducer writes `loss.json` beside the events. One round
contains many runs. See
[TELEMETRY.md §1.1](TELEMETRY.md#11-wiring-per-run).

## Rung

One stage of a [racing](#racing) tournament, in which the surviving
field duels on a board slice and the weakest fraction is cut. The
console labels a rung by what it does to the field: "Rung 0 · 4→2" means
four contestants entered and two survived. Survivors carry forward until
one remains to meet the champion on the full board. See
[TOURNAMENT-STRUCTURES.md §3.5](TOURNAMENT-STRUCTURES.md#35-racing-the-endorsed-bracket-shaped-option).

## Runtime configuration

The settings that shape how a run executes without changing what a
score means: the two model callables, `parallelism`, worker environment
scrubbing, the field-diversity tolerance, the infrastructure-abort
circuit, and the per-round token budget. They are held in
`RuntimeConfig`, are never part of the contract, and never roll the
epoch. See [RUNTIME.md](RUNTIME.md).

## Scalar

The one comparable number per generation, lower is better. It
aggregates the generation's loss profiles under the epoch's scoring
weights into the weighted drift loss plus `pass_weight × (1 −
mean_score)` plus the namespace terms. Everything that ranks, gates, or plots a difference
reads it, which is why the weights that produce it are frozen with the
board. A comparison reports `delta_scalar`, the challenger's scalar
minus the champion's, so a negative delta is an improvement. See
[SCORING.md §4](SCORING.md#4-per-generation-aggregate-score).

## Scoring weights

The frozen per-epoch weight set that turns loss profiles into the
[scalar](#scalar) and parameterizes the [promotion gate](#promotion-gate):
drift weights by kind and severity, `pass_weight`, namespace weights,
`promote_margin`, the monotonicity flags, and the nested tournament,
overfitting, proposer-quality, and experiment-memory blocks. Held in
`ScoringWeights`, written as the operator's live `scoring.json`, frozen
into each epoch, and part of the contract. See
[SCORING.md §2.5](SCORING.md#25-scoring-config-is-part-of-the-frozen-contract).

## Screening

An optional check between best-of-N sampling and the choice of a
candidate. Each candidate runs on a small rotating panel of training
entries. A candidate that breaks an entry the champion passes, or that
exceeds its wall-clock budget, is disqualified before it can spend a
tournament round. The screen disqualifies and never ranks; the critique still
chooses among the survivors, and an all-vetoed slate falls back to
choosing among all. Enabled by `proposer_quality.screen_entries`. Also
called *tryouts*. See `src/zicato/epoch/screen.py`.

## Settlement receipt

The record of a round's complete decision. It is written before the
first outcome write and kept afterwards with `state: committed`. Settling a
field crosses several files (the outcomes, the lineage, the champion
pointer, the journal, the settled bracket), and a process that dies
between two of them would leave the round half-decided. On restart,
resume replays every pending receipt in a fixed idempotent order and
lands in the same place; a field that died before its receipt was
written is discarded whole. See
`src/zicato/evolve/settlement_recovery.py` and
[RUNTIME.md §4](RUNTIME.md#4-resume-semantics).

## Snapshot

The full source of the system under test at one generation: a complete
copy in the directory backend, a git worktree in the git backend.
Copying or deleting a snapshot affects no other generation. See
[Generation store](#generation-store) and
[ARCHITECTURE.md §6](ARCHITECTURE.md#6-storage-layout).

## System under test

The system zicato wraps and optimizes. Zicato treats it as a black box
behind an [adapter](#adapter). It may be a multi-agent system of any shape, and it
may equally be a library or any Python source tree with an entry point
and a board. It may involve no language model at all: an agent uses one,
a deterministic program does not, and the [target model](#target-model)
role exists only when it does. Under the default `goldfive` telemetry
dialect it emits a `goldfive.v1.Event` stream, which is where drift kinds
come from; the `adk_events` and `transcript` dialects read a system that
never runs under goldfive. See
[ARCHITECTURE.md §1](ARCHITECTURE.md#1-what-zicato-is-and-why) and
[TELEMETRY-DIALECTS.md](TELEMETRY-DIALECTS.md).

## Target model

The model role the system under test runs on, when it runs on one. It is
supplied as the `target_call_llm` callable, named `target` under
`models.roles`, and routed into the agent code through `goldfive.wrap`'s
`call_llm=` parameter. It must differ from the
[evaluation model](#evaluation-model). A deterministic system leaves the
role unused. See [EMULATOR.md §3](EMULATOR.md#3-the-two-callable-rule)
and [MODEL-CONFIG.md](MODEL-CONFIG.md).

## Tournament

The comparison of champion against challengers on the board frozen for
the epoch, ending in a promotion-gate decision. Its shape is the
[tournament structure](#tournament-structure); its cache discipline is
[fast mode](#fast-mode). The standalone `zicato tournament` command runs
one comparison outside the loop and defaults to `--mode full`. See
[TOURNAMENT.md](TOURNAMENT.md) and
[SCORING.md §5](SCORING.md#5-the-tournament-promotion-gate).

## Tournament structure

The shape of competition an epoch declares in its `tournament` block.
Five are implemented: `gauntlet` (the default, one challenger against
the champion), `single_elim` and `double_elim` (pair the field, cut the
losers, repeat), `swiss` (every contestant plays every round, paired by
standing), and `racing` (successive halving over growing board slices).
The four wider structures differ only in how they narrow a field to one
finalist and then end in the same [champion gate](#champion-gate). The
structure and its params are part of the contract, so a change rolls the
epoch. See [TOURNAMENT-STRUCTURES.md](TOURNAMENT-STRUCTURES.md).

## Trajectory

The scalar across the promoted lineage of an epoch: one point per
promoted generation, with its per-namespace metric means, plus the
promotion rate and a plateau flag that is set only when enough promoted
generations exist to measure one. A flat trajectory beside a
`stalled_loop` finding is the stalled-loop signal. Served as
`/api/epoch/{id}/trajectory` and drawn by the console. The unrelated
*trajectory bootstrap* drafts board entries from a directory of foreign
agent traces ([TRAJECTORY-BOOTSTRAP.md](TRAJECTORY-BOOTSTRAP.md)). See
[TOURNAMENT.md §4.4](TOURNAMENT.md#44-optimization-trajectory).

## Workspace

The `.zicato/` directory holding everything zicato records for one
project: `config.json` (the registration record: adapter, mutable trees,
model roles, generation store backend), the `current_epoch` marker,
`lineage.json`, `index.db`, `epochs/`, `runtime/` (ephemeral liveness
and control files), and `repo/` (the git generation store). The
operator's live `board.jsonl`, `brief.md`, and `scoring.json` sit next
to `.zicato/`, and each epoch holds its own frozen copies. Created by
`zicato init`. See
[ARCHITECTURE.md §6](ARCHITECTURE.md#6-storage-layout).
