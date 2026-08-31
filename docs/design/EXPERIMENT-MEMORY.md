# Experiment memory — feeding the proposer prior outcomes

> **Status. SHIPPED** (default-on). This document specifies candidate
> *generation* — what the proposer sees before it writes a hypothesis. It
> does not change selection, scoring, or the tournament structure. Every
> claim below is reconciled against `src/zicato/proposer/`,
> `src/zicato/evolve/`, and the analytical index (`src/zicato/index/`).
> The implementing symbols are
> [`prior_experiments_for_epoch`](../../src/zicato/index/query.py) (the
> same-epoch reader), the `PriorExperiment` dataclass and the
> `EXPERIMENT_MEMORY_MAX_ENTRIES` cap defined in
> `zicato/core/experiment.py` and re-exported from
> [`core/types.py`](../../src/zicato/core/types.py),
> [`render_prior_experiments_block`](../../src/zicato/proposer/prompts.py)
> (the `## What's already been tried` prompt section), and the loop wiring
> in `zicato/evolve/` — `_load_prior_experiments` in `evolve/ingest.py`
> plus `produce_candidate_batch` in `evolve/candidate_batch.py`, which
> accumulates the in-flight siblings. §5 describes that shipped code.
> **Cross-contract transfer** (§3.4 and §5.2) — a `PriorExperiment` from a
> different epoch under the *same* `contract_hash`, marked
> `same_contract=False` — is the opt-in `experiment_memory.cross_epoch`
> contract knob. With the knob off, its default, the reader stays
> same-epoch-only and the prompt is byte-identical; see
> [`query.py`](../../src/zicato/index/query.py)'s
> `_cross_contract_entries` and the separated cross-epoch block in the
> prompt renderer.

**Experiment memory** is a compact digest of prior experiments that the
proposer reads before it writes a hypothesis. Each entry carries that
experiment's hypothesis, the mutation points it touched, its verdict
(promoted, rejected, or deferred), the reason, and its Δ-scalar. The
digest reaches the proposer as a user-prompt section, so that it stops
re-proposing known failures and builds on known wins. This document
specifies what the digest draws on, how it is curated and capped, how it
is scoped to one evaluation contract, and how it reaches the prompt.

## 1. Why the proposer needs settled history

The proposer call (`zicato.proposer.proposer.propose_experiment`)
assembles its user prompt from several channels (see
`zicato.proposer.prompts.render_user_prompt`). Four of them describe the
round's present state:

- `current_loss_summary` — a one-line digest of the *current champion's*
  drift-loss mean and pass rate
  (`zicato.evolve.decision_support._render_loss_summary`).
- `patterns` — the detector output for the current generation
  (`zicato.patterns.detectors.detect_patterns`), rendered under
  `## Patterns observed`.
- `insights` — the decision-telemetry analyzer's markdown for the
  epoch, rendered under `## Recent telemetry insights` (loaded by
  `zicato.analyzer.load_latest_insights` when `workspace_root` is
  supplied).
- `failure_profile` — the bucketed, board-anonymized **outcome-marginal
  failure-mode profile**
  (`zicato.evolve.decision_support._render_failure_profile` →
  `render_failure_mode_profile`): board-wide rates for *why* answers
  failed (over-retrieval / misses / empty answers, plus precision/recall
  when the board's continuous scores carry it), computed over the
  **train slice only** and coarsened so no entry id, question, or output
  leaks. This is the §11.5 channel in
  [`OVERFITTING.md`](OVERFITTING.md); it carries the same
  marginal-not-joint, holdout-integrity guarantees as the rest of the
  proposer feed.

Each of those four describes the champion's current state and the most
recent round's observations. None of them carries the **settled
history** — "round 3 already tried tightening the researcher's
instruction and it was rejected for a pass-rate regression", or "round 5
tightened the coordinator's routing and it promoted with Δscalar +0.12".
That history exists on disk, because every generation's
`experiment.json` carries a hypothesis and, once the tournament settles,
an outcome; it is also projected into the analytical index's
`experiments` and `tournaments` tables. Experiment memory is the channel
that puts it in front of the proposer.

Without settled history the proposer optimises against the current
champion's gradient with no record of the search it has already done,
and two failure modes follow directly:

- **Re-proposing known failures.** A mutation that was rejected in
  round 3 looks, to a round-7 proposer, as attractive as it did in round
  3 — the pattern that motivated it is still present, and nothing else in
  the prompt says "we tried that; it regressed `[summarise]` pass-rate".
  The proposer spends a round re-discovering the rejection.
- **Failing to build on wins.** A mutation that promoted in round 5 is
  already folded into the champion, but the *direction* that worked —
  "terser specialist descriptions reduce off-topic preambles" — is not
  surfaced as a learned signal. The proposer cannot extend a winning line
  on purpose; it rediscovers it by luck or not at all.

Two further channels reach the proposer and carry lineage-scoped
history: the candidate-genealogy block (promoted ancestors and diverse
rejected ideas) and the prediction-calibration block (how the proposer's
own movement predictions landed). Both are specified in
[PROPOSER.md](PROPOSER.md); experiment memory is the channel that
carries per-experiment verdicts and Δ-scalars for the whole epoch.

The forbidden-ids list (`brief.forbidden_ids`, enforced by
`enforce_forbidden`) does not cover this. It deduplicates by
mutation-point *id* — "never touch `coordinator.routing`" — and is a
contract constraint the operator sets. Experiment memory deduplicates by
semantics — "you proposed this direction on these ids in round 3 and it
regressed". The two are orthogonal: the forbidden-ids list is a hard
operator gate on *which ids are legal*, and experiment memory is advisory
feedback on *what has already been attempted and how it fared*.

## 2. What experiment memory adds — two scopes

Experiment memory has two complementary scopes. The design must cover
both because they answer different questions and read from different
sources.

### 2.1 Cross-round outcome history (the main loop)

This is the primary scope: **settled** experiments from prior rounds of
the current epoch (and, conditionally, earlier epochs under the same
contract — see §4). Each carries a verdict (`promoted` / `rejected` /
`deferred`), a rejection reason, and a Δ-scalar. This is the history the
proposer reads to avoid known failures and build on known wins. It is
sourced from the analytical index's `experiments` table (§3.1).

### 2.2 Intra-round sibling awareness (the multi-challenger field)

Under a non-gauntlet tournament structure (`field_size() > 1` — see
[SELECTION.md](SELECTION.md) §9 and [TOURNAMENT-STRUCTURES.md](TOURNAMENT-STRUCTURES.md)),
the orchestrator mints a *field* of N challengers in one round before
any of them runs:

```python
for offset in range(field_n):
    challenger, status = await _propose_and_apply_challenger(...)
```

(`produce_candidate_batch` in `evolve/candidate_batch.py`). Each call to
`_propose_and_apply_challenger` would otherwise be a **blind** proposer
call, in which challenger k cannot see what challengers 0..k-1 in the
*same round* just proposed. Two siblings can then propose the same
mutation,
collapsing a field of N into fewer than N distinct experiments and
wasting tournament compute on duplicates.

The siblings have **no outcomes yet** — they have not run. But "already
attempted *this round*" is itself useful: it lets challenger k diversify
away from its siblings. So the sibling scope surfaces the *hypotheses*
(core idea + modulating ids) of the siblings already minted this round,
with no verdict, under the same prompt section as the settled history
but flagged as in-flight.

### 2.3 Where it sits in the loop — and what it is NOT

Experiment memory changes **candidate generation** only — the proposer
step (ARCHITECTURE.md §4.7), upstream of the applier, the tournament,
and the gate. It is **orthogonal to tournament structure**: the digest
is assembled the same way and threaded into the same `propose_experiment`
call whether the round is a gauntlet duel, a Swiss round, a racing
ladder, or an elimination bracket. Every structure proposes challengers;
every structure benefits identically. The gate, the scalar, and the
promotion rules are untouched.

It is **not** intra-tournament adaptive generation. A separate, larger
lever — *propose challenger k+1 conditioned on the realised results of
challengers 0..k within the same round* — would require interleaving
proposing and running inside one tournament, which the current
"mint-the-whole-field-then-run" shape forbids. That lever is named and
scoped **out** here. The sibling scope (§2.2) shares only *hypotheses*
and never *outcomes*, because the outcomes do not exist at
field-minting time. See [SELECTION.md](SELECTION.md) §9 for the
adaptive-generation direction.

## 3. Methodology

### 3.1 The source: the `experiments` table

The settled history is read from the analytical index
(`.zicato/index.db`), **not** by re-parsing `journal.md` markdown or
walking every `experiment.json`. The index already projects the
fields experiment memory needs into a relational shape that a single
indexed `SELECT` answers, and the loop dual-writes it each round
(`zicato.evolve.ingest._ingest_experiment_into_index`).

The `experiments` table (`zicato.index.schema`, the `CREATE TABLE
experiments` statement) carries, per `(epoch_id, generation_id)`:

| Column | Use in the digest |
|---|---|
| `hypothesis_core_idea` | the one-line "what was tried" |
| `hypothesis_why` | the rationale (optional in the compact render) |
| `hypothesis_json` | full `HypothesisSpec` JSON — carries `modulating` (the targeted mutation-point ids) |
| `tournament_decision` | `promoted` / `rejected` / `deferred` / `NULL` (unsettled) |
| `rejection_reason` | the symbolic reason when rejected |
| `scalar_score_delta` | the Δ-scalar (sign gates promotion) |
| `drift_loss_delta`, `pass_rate_delta` | secondary deltas, available for richer renders |

The targeted mutation-point ids come from the `modulating` array inside
`hypothesis_json` (decoded), which is the proposer's own declaration of
what the hypothesis touched. (The `patches` table's `mutation_id` column
is the *applied* set; `modulating` is the *declared* set. They normally
agree; `modulating` is preferred because it is what the proposer
reasoned about and is present even when a patch set failed post-apply
validation.) The existing reader
`zicato.index.query.experiments_for_epoch(db_path, epoch_id)` already
selects every column above, and the digest reader of §5.2 layers
selection, capping, and shaping on top of it.

### 3.2 What to surface per prior experiment

The digest data shape, one entry per prior experiment, is the frozen
dataclass `PriorExperiment` (defined in `zicato.core.experiment` and
re-exported from `zicato.core.types`):

```python
@dataclass(frozen=True, slots=True)
class PriorExperiment:
    generation_id: str            # "v3"
    epoch_id: str                 # "2026-04-08_hardened_research"
    core_idea: str                # one-sentence hypothesis core
    modulating: tuple[str, ...]   # targeted mutation-point ids (from modulating)
    decision: str                 # "promoted" | "rejected" | "deferred" | "in_flight"
    rejection_reason: str         # symbolic reason, "" when not rejected
    scalar_score_delta: float | None   # Δscalar; None when unsettled / unavailable
    same_contract: bool = True    # False for a cross-contract (different-epoch) entry
```

`decision == "in_flight"` is the sibling case (§2.2): a hypothesis minted
this round and not yet run, so `scalar_score_delta` is `None` and
`rejection_reason` is `""`.

The dataclass also carries `prediction_accuracy` — the fraction of a
settled experiment's falsifiable predictions
(`expected_drift_movements`, `expected_metric_movements`, and
`expected_pass_rate_delta`) that the realized movements bore out, in
`[0.0, 1.0]`. It is `None` for an unsettled or in-flight entry and for an
experiment that made no gradable prediction. It is an advisory
calibration signal rendered in banded form alongside the rest of the
digest, and it never gates promotion.

### 3.3 Which experiments, and in what order

The digest is **capped and curated** rather than a full dump: a long
epoch can accumulate dozens of experiments and the proposer prompt must
stay small (§4). The reader selects, in priority order:

1. **All promoted wins** (decision `promoted`), most recent first. These
   are the learned signal the proposer should build on. Wins are rare
   and high-value, so they are never dropped by the cap; if wins alone
   exceed the cap the oldest wins are dropped last.
2. **The most informative rejected failures** — the K most recent
   rejections, then within that window the ones with the *largest*
   regression (most negative `scalar_score_delta`) ranked first. A recent
   sharp regression is the strongest "do not retry this" signal; an old
   marginal rejection is the weakest.
3. **`deferred`** experiments are included only if budget remains after
   wins and rejections — a deferred verdict is the weakest signal.

A single cap `EXPERIMENT_MEMORY_MAX_ENTRIES` (default **12**) bounds the
settled section. Siblings (§2.2) are surfaced separately and in full
(the field is small — `field_size()` is single-digit) so the proposer
always sees its complete in-flight cohort.

Ordering inside the rendered section: **wins first, then failures**,
each block most-recent-first, so the proposer reads "here is what worked"
before "here is what to avoid".

### 3.4 Contract scoping

History is only safely comparable **within the same evaluation
contract** — the board, the proposer brief, the scoring, the harness
identity, and the proposer itself (see
[EPOCHS-AND-JOURNALING.md](EPOCHS-AND-JOURNALING.md) §10). A Δscalar from
a different contract is measured against a different board and different
weights; a mutation id may not even resolve in the current surface.

So the digest is scoped to the **current epoch by default**. One epoch is
one contract by the auto-epoching invariant, so the current epoch *is*
the current contract. Cross-epoch transfer is **opt-in and clearly
flagged**. A cross-contract `PriorExperiment` — a different epoch under
the *same* `contract_hash`, found via `epochs.contract_hash` — is marked
`same_contract=False` and surfaced only as a core-idea plus decision line,
with its Δscalar **omitted** because the number does not transfer. Such an
entry takes only whatever cap-budget the same-epoch entries leave, so
same-epoch history keeps priority; that is the mechanised form of
"only when same-epoch history is sparse". The default behaviour is
**same-epoch only**; cross-contract
transfer is the opt-in `experiment_memory.cross_epoch` knob on the frozen
contract (`ScoringWeights.experiment_memory`, omitted-at-default from the
contract canonical form so existing epochs never roll; opting in rolls
the epoch). Experiments from a *different* `contract_hash` are never
surfaced, because their mutation ids and losses are not comparable, and
an epoch that records no contract hash is never treated as
transferable.

### 3.5 Where it lands in the prompt, and the rendered shape

The digest reaches the proposer as a `## What's already been tried`
section of the user prompt, alongside `## Recent telemetry insights`,
`## Current loss summary`, and `## Patterns observed` (see
`render_user_prompt`). `render_prior_experiments_block` in
`zicato.proposer.prompts` renders it at one line per experiment, to keep
the prompt small:

```
## What's already been tried (this epoch — avoid repeating failures, build on wins)

Already promoted (build on these — the direction worked):
- v5 PROMOTED Δscalar=+0.120  [coordinator.routing]
    Add a budget hint to the coordinator routing so it stops re-routing on revision turns.
- v2 PROMOTED Δscalar=+0.080  [researcher.instruction]
    Require the researcher to cite a source before asserting a metric.

Already rejected (do NOT re-propose these unless something changed):
- v6 REJECTED Δscalar=-0.090  [writer.tools.summarize.description]  (pass_rate_regression_on_summarise_short)
    Loosen the writer's summarise tool description to allow longer outputs.
- v4 REJECTED Δscalar=-0.020  [coordinator.routing]  (insufficient_margin)
    Soften the coordinator routing to defer to the writer earlier.

Proposed this round, not yet evaluated (diversify away from these):
- v8 IN-FLIGHT  [researcher.instruction]
    Tighten the researcher instruction to forbid uncited claims.
```

The framing line reads **"avoid repeating failures, build on wins"**
rather than "only do X". The section advises; it does not constrain. The
proposer may re-propose a rejected direction when it has reason to
believe something changed, such as a fresh pattern or a different
champion. The section exists so that such a re-proposal is a considered
choice rather than an accident.

When no prior experiments exist (the baseline round, or the first
challenger of a field with no settled history), the helper returns the
empty string and the section is omitted entirely, in the same way as the
insights and pattern blocks. The empty string is the proposer-side
sentinel for "skip this section".

## 4. Risks and limits

**Prompt bloat.** A long epoch accumulates many experiments; dumping all
of them would crowd out the mutation manifest, the part the proposer
must read in full. *Countermeasure:* the hard cap of §3.3
(`EXPERIMENT_MEMORY_MAX_ENTRIES`, default 12), the compact
one-line-per-experiment render of §3.5 (no full `why`, no patch bodies),
and the curation that keeps the highest-signal entries when the cap
binds — every win, plus the sharpest recent rejections.

**Over-anchoring and conservatism.** A prominent list of rejected things
can make the proposer timid, refusing to revisit a direction that failed
once under conditions that have since changed. *Countermeasure:* the
framing of §3.5 is advisory ("avoid repeating failures, build on wins")
rather than restrictive, and the rejected block is captioned "do NOT
re-propose **unless something changed**". The section never enters the
*system* prompt or the hard schema; it is advisory user-prompt context,
in the same way as patterns. The forbidden-ids list remains the only
hard gate.

**Staleness across contracts.** A mutation id from a different contract
may fail to resolve in the current surface, and a Δscalar from a
different board is not comparable. *Countermeasure:* the default scope is
the current epoch, which is the current contract; cross-contract entries
are opt-in, flagged `same_contract=False`, rendered without their
Δscalar, and never drawn from a different `contract_hash` (§3.4).

**Noise — a near-zero rejected Δ can still be a good idea.** An
experiment rejected for `insufficient_margin` with Δscalar ≈ 0 is a
toss-up rather than a proven failure, and treating it as "never do this"
discards a possibly fine direction. *Countermeasure:* the digest carries
the *signed Δscalar* verbatim and renders it, so the proposer sees
`Δscalar=-0.020` (a near miss) differently from `Δscalar=-0.300` (a real
regression). The rejection ordering (§3.3) ranks the sharpest
regressions first within the window, so the strongest avoid-signals are
the most visible and a near-zero rejection is the first to fall off the
cap. The digest does not collapse the verdict to a binary rejected flag,
because the magnitude carries the signal.

## 5. The shipped implementation

The proposer receives `prior_experiments` from its caller as a typed
list; a call that passes none renders a byte-identical prompt, so a
standalone proposer call is unaffected by this surface.

### 5.1 The `PriorExperiment` dataclass — `zicato/core/experiment.py`

The frozen dataclass of §3.2 lives in `zicato.core.experiment` and is
re-exported from `zicato.core.types`, which lists it in `__all__`. It
carries nine fields: `generation_id`, `epoch_id`, `core_idea`,
`modulating`, `decision`, `rejection_reason`, `scalar_score_delta`,
`same_contract`, and `prediction_accuracy`. The module-level constant
`EXPERIMENT_MEMORY_MAX_ENTRIES = 12` sits beside it, so the cap is
defined once and imported wherever it is applied.

### 5.2 The reader — `zicato/index/query.py`

`prior_experiments_for_epoch(db_path, epoch_id, *,
max_entries=EXPERIMENT_MEMORY_MAX_ENTRIES, cross_epoch=False) ->
list[PriorExperiment]`:

- Reads the epoch's settled experiments through `experiments_for_epoch`,
  which already selects `hypothesis_json`, `tournament_decision`,
  `rejection_reason`, and `scalar_score_delta`. Each row's
  `hypothesis_json` is decoded to lift `modulating` into a tuple; a decode
  failure yields an empty tuple rather than raising.
- **Skips unsettled rows** (`tournament_decision IS NULL`). An experiment
  with no verdict carries no learning signal, and including them would
  surface the current round's own just-written, outcome-less experiment.
  In-flight sibling entries come from a different source (§5.5) rather
  than from the index.
- Applies the curation and cap of §3.3: it collects every `promoted` row,
  then the most-recent-K `rejected` rows ordered by sharpest regression
  within that window, then `deferred` rows if budget remains, to a total
  of at most `max_entries`. Same-epoch entries are built with
  `same_contract=True`.
- Tolerates a missing index the way the other selectors do, returning
  `[]` through the `_select` / `open_index` path and never raising
  `IndexNotBuiltError` to the caller.
- Is exported in the module's `__all__`.

Cross-contract transfer (§3.4) rides on the same reader. With
`cross_epoch=True`, `_cross_contract_entries` runs a second query joining
`experiments` to `epochs` on `contract_hash` and yields
`same_contract=False` entries with `scalar_score_delta=None`, capped to
whatever budget the same-epoch entries leave. The operator-facing knob is
`experiment_memory.cross_epoch` on the frozen contract, threaded through
`_load_prior_experiments` at both the gauntlet and the multi-challenger
call sites; the renderer gives cross-contract entries their own
epoch-tagged block after the same-epoch blocks.

### 5.3 Prompt rendering — `zicato/proposer/prompts.py`

- `render_prior_experiments_block(prior, *, restrict=False) -> str`
  produces the compact three-block render of §3.5 (promoted, rejected,
  in-flight) and returns `""` when `prior` is empty. It groups by
  `decision` and renders `same_contract=False` entries without their
  Δscalar. The `restrict` flag carries the proposer-visibility discipline
  of [`OVERFITTING.md`](OVERFITTING.md) §11, so the block obeys the same
  leakage restriction as the pattern block.
- `render_user_prompt` takes a
  `prior_experiments: Iterable[PriorExperiment] = ()` keyword. When it is
  non-empty the renderer prepends the `## What's already been tried`
  section; when it is empty the section is omitted and the prompt is
  byte-identical, mirroring the `insights`-block conditional. In the
  assembled prompt the section sits immediately above the core
  loss/pattern/mutation body, below the round's aggregate signals
  (`## Recent telemetry insights` and `## Failure-mode profile`) and below
  the genealogy and prediction-calibration channels when those are
  present. Settled history therefore reads as the last framing the
  proposer sees before the current-state body.
- `render_prior_experiments_block` is listed in the module's `__all__`,
  and the `render_user_prompt` docstring documents the keyword.
- `SYSTEM_PROMPT_TEMPLATE` is untouched: experiment memory is advisory
  user-prompt context and never part of the hard schema.

### 5.4 Proposer wiring — `zicato/proposer/proposer.py`

- `propose_experiment` takes a keyword-only
  `prior_experiments: Iterable[PriorExperiment] = ()` — the settled
  digest, assembled by the caller. Passing it explicitly, rather than
  reading the index inside the proposer, keeps the proposer's only
  filesystem dependency its existing lazy analyzer import, and lets the
  caller inject the round's in-flight siblings (§2.2) into the same list.
- The `workspace_root` gate is not reused here. The caller performs the
  index read and passes the typed list, so the proposer stays a pure
  prompt-assembler over its inputs. The analyzer-insights surface differs:
  the proposer reads that one itself through `workspace_root`. Experiment
  memory is assembled caller-side because only the caller knows the
  sibling entries.
- `prior_experiments` is threaded into the `render_user_prompt(...)` call
  inside the retry loop. It is loop-invariant, so every attempt renders it
  unchanged.

### 5.5 Loading the digest and threading it through the field

- `_load_prior_experiments(workspace_root, epoch_id, *,
  cross_epoch=False) -> list[PriorExperiment]` in `zicato/evolve/ingest.py`
  calls `prior_experiments_for_epoch(_index_db_path(workspace_root),
  epoch_id)` inside a best-effort `try/except`. A missing or stale index
  never aborts a round: the failure is logged at debug level and the
  function returns `[]`, mirroring `_ingest_experiment_into_index`.
- **Candidate batch (`produce_candidate_batch` in
  `zicato/evolve/candidate_batch.py`).** The settled digest is computed
  once, before the loop over the requested slots. A running
  `siblings: list[PriorExperiment]` accumulates in mint order, and each
  `_propose_and_apply_challenger` call receives the concatenation of the
  settled digest and the siblings so far. After a challenger is proposed
  and applied, an `in_flight` `PriorExperiment` built from that
  challenger's `experiment.hypothesis` (`core_idea` and `modulating`) is
  appended to `siblings`. A challenger whose proposer failed contributes
  no sibling entry, because there is no hypothesis to share.
  `_propose_and_apply_challenger` takes a `prior_experiments` keyword and
  threads it into its inner `propose_experiment(...)` call.
- **The standalone propose command** (`zicato/cli/commands/propose.py`)
  loads the digest the same way, so `zicato proposer propose` sees the
  section the loop sees.

### 5.6 The tests

- **`tests/test_index_prior_experiments.py`** — the reader. A missing or
  empty index returns `[]`. Unsettled rows are skipped. `modulating` is
  lifted from `hypothesis_json`, and a malformed `hypothesis_json`
  degrades to an empty tuple rather than raising. A rejection carries its
  reason and its signed delta. Under the cap, every win survives, the
  sharpest-regression recent rejections fill the remainder, and `deferred`
  rows enter only on leftover budget. The cross-epoch knob has its own
  cases: with the knob off the result is byte-identical, and with it on the
  flagged entries are appended and never displace same-epoch entries.
- **`tests/test_proposer_prior_experiments_block.py`** — the renderer.
  Empty input returns `""` and the section is omitted from
  `render_user_prompt`; the promoted, rejected, and in-flight groups render
  as in §3.5; `same_contract=False` entries render without a Δscalar and in
  their own separated block; the section lands between the telemetry
  insights and the loss summary.
- **`tests/test_proposer_prior_experiments.py`** — the end-to-end prompt. A
  stub auxiliary LLM captures the user prompt and the tests assert the
  block appears when `prior_experiments` is non-empty and is absent when it
  is empty.
- **`tests/test_orchestrator_prior_experiments.py`** — the field loop.
  Siblings accumulate, so challenger k's prompt contains the in-flight core
  ideas of challengers 0..k-1; a failed challenger contributes no sibling
  line.

### 5.7 What experiment memory does not touch

The index schema (`index/schema.py` — `experiments` already carries every
column), `index/ingest.py`, the system prompt, the scalar and the scoring,
the promote gate, the tournament structures, the dashboard, and
`journal.md` rendering are all unchanged. The contract-hash and
auto-epoching machinery is read-only here: the reader scopes by
`epoch_id`.

## 6. Cross-references

| Topic | Document |
|---|---|
| Hypothesis schema, the experiment record this digest reads from | [EPOCHS-AND-JOURNALING.md](EPOCHS-AND-JOURNALING.md) §3 |
| Why the proposer's search is greedy without settled history; the autoresearch-inspired memory direction | [RATIONALE.md](RATIONALE.md) §17 |
| The proposer's place in the meta-loop data flow | [ARCHITECTURE.md](ARCHITECTURE.md) §4.7, §8 |
| The `experiments` / `tournaments` tables this digest reads | [ANALYTICAL-INDEX.md](ANALYTICAL-INDEX.md) |
| Orthogonal to tournament structure (gauntlet / swiss / racing / elim) | [SELECTION.md](SELECTION.md) §1, [TOURNAMENT.md](TOURNAMENT.md) §1 |
| The multi-challenger field whose siblings this digest dedups | [TOURNAMENT-STRUCTURES.md](TOURNAMENT-STRUCTURES.md), [SELECTION.md](SELECTION.md) §9 |
| The proposer brief — the static operator steering this complements | [EPOCHS-AND-JOURNALING.md](EPOCHS-AND-JOURNALING.md) §7 |
| Glossary | [VOCABULARY.md](VOCABULARY.md) |
