# Experiment memory — feeding the proposer prior outcomes

> **Status. SHIPPED** (default-on). This document specifies a change to
> candidate *generation* — what the proposer sees before it writes a
> hypothesis. It does not change selection, scoring, or the tournament
> structure. The "current behaviour" sections are reconciled against
> `src/zicato/proposer/`, `src/zicato/orchestrator.py`, and the
> analytical index (`src/zicato/index/`); the mechanism described below
> is **built and live**:
> [`prior_experiments_for_epoch`](../../src/zicato/index/query.py) (the
> same-epoch reader), the `PriorExperiment` dataclass +
> `EXPERIMENT_MEMORY_MAX_ENTRIES` cap in
> [`core/types.py`](../../src/zicato/core/types.py),
> [`render_prior_experiments_block`](../../src/zicato/proposer/prompts.py)
> (the `## What's already been tried` prompt section), and the
> orchestrator wiring — `_load_prior_experiments` plus the gauntlet and
> multi-challenger (`_evolve_multi_challenger`, including in-flight
> sibling accumulation) call sites. The §5 implementation plan describes
> the as-built contract; read it in the past tense. The **only** part
> that remains future work is the §3.4 / §5.2 **cross-contract transfer**
> (`same_contract=False` — a different epoch under the *same*
> `contract_hash`): the reader ships same-epoch-only, with the
> cross-contract branch left as an explicit, unbuilt extension point in
> [`query.py`](../../src/zicato/index/query.py).

The proposer is the only learning component in zicato that, today, does
not learn. The loss reducer accumulates across runs, the pattern
detectors aggregate across the epoch, the analyzer summarises the
previous round's telemetry — but the proposer itself is **memoryless**.
Each round it sees the current champion's loss summary, the freshly
detected patterns, and the static proposer brief, and from those it
writes one hypothesis. It does **not** see the history of what has
already been tried this epoch and how it fared. So it re-proposes
mutations that were already rejected, and it cannot deliberately build
on the edits that already worked.

This document specifies **experiment memory**: a compact digest of
prior experiments — each one's hypothesis, the mutation points it
touched, its verdict (promoted / rejected / deferred), the reason, and
its Δ-scalar — surfaced to the proposer in a new user-prompt section so
it (a) stops re-proposing known failures and (b) builds on known wins.

## 1. The memoryless-hill-climb limitation

Today's proposer call (`zicato.proposer.proposer.propose_experiment`)
assembles its user prompt from these live, present-tense inputs (see
`zicato.proposer.prompts.render_user_prompt`):

- `current_loss_summary` — a one-line digest of the *current champion's*
  drift-loss mean and pass rate (`orchestrator._render_loss_summary`).
- `patterns` — the detector output for the current generation
  (`orchestrator.detect_patterns`), rendered under `## Patterns observed`.
- `insights` — the decision-telemetry analyzer's markdown for the
  epoch, rendered under `## Recent telemetry insights` (loaded by
  `zicato.analyzer.load_latest_insights` when `workspace_root` is
  supplied).
- `failure_profile` — the bucketed, board-anonymized **outcome-marginal
  failure-mode profile** (`orchestrator._render_failure_profile` →
  `render_failure_mode_profile`): board-wide rates for *why* answers
  failed (over-retrieval / misses / empty answers, plus precision/recall
  when the board's continuous scores carry it), computed over the
  **train slice only** and coarsened so no entry id, question, or output
  leaks. This is the §11.5 channel in
  [`OVERFITTING.md`](OVERFITTING.md); it carries the same
  marginal-not-joint, holdout-integrity guarantees as the rest of the
  proposer feed.

Every one of those describes the *present*: the champion's current
state and the most recent round's observations. None of them carries
the **settled history** — "round 3 already tried tightening the
researcher's instruction and it was rejected for a pass-rate
regression", "round 5 tightened the coordinator's routing and it
promoted with Δscalar +0.12". That history exists on disk (every
generation's `experiment.json` carries a hypothesis and, once the
tournament settles, an outcome) and in the analytical index (the
`experiments` and `tournaments` tables), but the proposer never reads
it.

The consequence is a **greedy hill-climb with amnesia**. The proposer
optimises against the current champion's gradient with no record of the
search it has already done. Two failure modes follow directly:

- **Re-proposing known failures.** A mutation that was rejected in
  round 3 looks, to a round-7 proposer, exactly as attractive as it did
  in round 3 — the pattern that motivated it is still present, and
  nothing in the prompt says "we tried that; it regressed `[summarise]`
  pass-rate". The proposer burns a round re-discovering the rejection.
- **Failing to build on wins.** A mutation that promoted in round 5 is
  now baked into the champion, but the *direction* that worked — "terser
  specialist descriptions reduce off-topic preambles" — is not surfaced
  as a learned signal. The proposer cannot deliberately extend a winning
  line; it rediscovers it by luck or not at all.

The `forbidden_ids` thread (`brief.forbidden_ids`, enforced by
`enforce_forbidden`) is **not** a fix for this. Forbidden-ids dedups by
mutation-point *id* — "never touch `coordinator.routing`" — a contract
constraint the operator sets. Experiment memory dedups by *semantics* —
"you proposed this exact direction on these ids in round 3 and it
regressed". They are orthogonal: one is a hard operator gate on *which
ids are legal*, the other is advisory feedback on *what has already been
attempted and how it fared*.

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

(`orchestrator._evolve_multi_challenger`, ~line 1314). Each call to
`_propose_and_apply_challenger` is a **blind** proposer call: challenger
k has no idea what challengers 0..k-1 in the *same round* just proposed.
Two siblings can — and in practice do — propose the same mutation,
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
scoped **out** here; the sibling scope (§2.2) deliberately shares only
*hypotheses*, never *outcomes*, precisely because the outcomes do not
exist at field-minting time. See [SELECTION.md](SELECTION.md) §9 for the
adaptive-generation direction.

## 3. Methodology

### 3.1 The source: the `experiments` table

The settled history is read from the analytical index
(`.zicato/index.db`), **not** by re-parsing `journal.md` markdown or
walking every `experiment.json`. The index already projects exactly the
fields experiment memory needs into a relational shape that a single
indexed `SELECT` answers, and the orchestrator already dual-writes it
each round (`orchestrator._ingest_experiment_into_index`).

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
selects every column above; the new digest reader (§5.2) layers
selection / capping / shaping on top of it.

### 3.2 What to surface per prior experiment

The digest data shape (one entry per prior experiment) is a new frozen
dataclass `PriorExperiment` in `zicato.core.types`:

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
this round, not yet run, so `scalar_score_delta` is `None` and
`rejection_reason` is `""`.

### 3.3 Which experiments, and in what order

The digest is **capped and curated**, not a full dump — a long epoch can
accumulate dozens of experiments and the proposer prompt must not bloat
(§6). The reader selects, in priority order:

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
contract** (board + brief + scoring + harness identity — see
[EPOCHS-AND-JOURNALING.md](EPOCHS-AND-JOURNALING.md) §10). A Δscalar from
a different contract is measured against a different board and different
weights; a mutation id may not even resolve in the current surface.

So the digest is scoped to the **current epoch by default** — one epoch
is one contract by the auto-epoching invariant, so "current epoch"
*is* "current contract". Cross-epoch transfer is **opt-in and clearly
flagged**: a cross-contract `PriorExperiment` (a different epoch under
the *same* `contract_hash`, found via `epochs.contract_hash`) is marked
`same_contract=False`, surfaced only as a *core-idea + decision* line
with its Δscalar **omitted** (the number does not transfer), and only
when same-epoch history is sparse. The default and shipped behaviour is
**same-epoch only**; cross-contract transfer is a documented extension
point, not the v1 default. Experiments from a *different* `contract_hash`
are never surfaced — their mutation ids and losses are not comparable.

### 3.5 Where it lands in the prompt, and the rendered shape

A new section, `## What's already been tried`, is added to the user
prompt, alongside the existing `## Recent telemetry insights`,
`## Current loss summary`, and `## Patterns observed` sections (see
`render_user_prompt`). It is rendered by a new
`render_prior_experiments_block` helper in `zicato.proposer.prompts`,
compact — one line per experiment — to keep the prompt small:

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

The framing line is deliberate: **"avoid repeating failures, build on
wins"** — not "only do X". The section advises; it does not constrain.
The proposer is free to re-propose a rejected direction when it has a
reason to believe something changed (a new pattern, a different
champion); the section's job is to make that a *deliberate* choice, not
an accident of amnesia.

When no prior experiments exist (the baseline round, or the first
challenger of a field with no settled history), the helper returns the
empty string and the section is omitted entirely — exactly as the
insights and pattern blocks behave today. The empty string is the
proposer-side sentinel for "skip this section".

## 4. Risks and limits

**Prompt bloat.** A long epoch accumulates many experiments; dumping all
of them would crowd out the mutation manifest (the part the proposer
must read in full). *Mitigation:* the hard cap of §3.3
(`EXPERIMENT_MEMORY_MAX_ENTRIES`, default 12), the compact one-line-per-
experiment render of §3.5 (no full `why`, no patch bodies), and the
curation that keeps the highest-signal entries (all wins, the sharpest
recent rejections) when the cap bites.

**Over-anchoring / conservatism.** A prominent list of "rejected
things" can make the proposer timid — refusing to revisit a direction
that failed once under conditions that have since changed. *Mitigation:*
the framing of §3.5 is explicitly *advisory* ("avoid repeating failures,
build on wins", not "only do X"); the rejected block is captioned "do
NOT re-propose **unless something changed**". The section never enters
the *system* prompt or the hard schema — it is advisory user-prompt
context, exactly like patterns. Forbidden-ids remains the only hard
gate.

**Staleness across contracts.** A mutation id from an old contract may
no longer resolve, and a Δscalar from a different board is not
comparable. *Mitigation:* the default scope is same-epoch (= same
contract); cross-contract entries are opt-in, flagged `same_contract=
False`, rendered without their Δscalar, and never drawn from a different
`contract_hash` (§3.4).

**Noise — a near-zero rejected Δ is not a bad idea.** An experiment
rejected for `insufficient_margin` with Δscalar ≈ 0 is a *toss-up*, not
a proven failure; treating it as "never do this" discards a possibly
fine direction. *Mitigation:* the digest carries the *signed Δscalar*
verbatim and renders it, so the proposer sees `Δscalar=-0.020` (a near-
miss) differently from `Δscalar=-0.300` (a real regression). The
rejection ordering (§3.3) ranks the sharpest regressions first within
the window, so the strongest "avoid" signals are the most visible and a
near-zero rejection is the first to fall off the cap. We deliberately do
**not** collapse the verdict to a binary "rejected" flag — the magnitude
*is* the signal.

## 5. Implementation plan

> **As-built.** This section was the implementation contract and is now
> the shipped shape — read it as a description of the live code, not a
> plan. The §3.4 cross-contract branch is the lone exception (still an
> unbuilt extension point; see the §5.2 note).

No behaviour changes unless `workspace_root` is supplied to
`propose_experiment` (the same gating the analyzer-insights surface
already uses), so every existing standalone-proposer test keeps passing
untouched.

### 5.1 New dataclass — `zicato/core/types.py`

Add the frozen dataclass `PriorExperiment` exactly as in §3.2
(`generation_id`, `epoch_id`, `core_idea`, `modulating`, `decision`,
`rejection_reason`, `scalar_score_delta`, `same_contract`). Add it to
`__all__`. Add a module-level constant
`EXPERIMENT_MEMORY_MAX_ENTRIES = 12` (or place it on the reader module
in §5.2 — keep it in one place and import it).

### 5.2 New reader — `zicato/index/query.py`

Add `prior_experiments_for_epoch(db_path, epoch_id, *, max_entries=EXPERIMENT_MEMORY_MAX_ENTRIES) -> list[PriorExperiment]`:

- Read the epoch's settled experiments. Reuse `experiments_for_epoch`
  (it already selects `hypothesis_json`, `tournament_decision`,
  `rejection_reason`, `scalar_score_delta`). Decode each row's
  `hypothesis_json` to lift `modulating` into a tuple; on a decode
  failure fall back to an empty tuple (best-effort — never raise).
- **Skip unsettled rows** (`tournament_decision IS NULL`) — an
  experiment with no verdict carries no learning signal and would
  otherwise surface the *current* round's own just-written, outcome-less
  experiment. (Sibling in-flight entries come from a different source —
  §5.4 — not from the index.)
- Apply the curation + cap of §3.3: collect all `promoted`, then the
  most-recent-K `rejected` ordered by sharpest regression within the
  window, then `deferred` if budget remains; total ≤ `max_entries`.
  Build `PriorExperiment` objects with `same_contract=True`.
- Tolerate a missing index exactly as the other selectors do (return
  `[]` via the `_select` / `open_index` path — never raise
  `IndexNotBuiltError` to the caller).
- Add to `__all__`.

(Cross-contract transfer of §3.4 is a follow-on extension on this same
reader — a second query keyed on `epochs.contract_hash` that yields
`same_contract=False` entries with `scalar_score_delta=None`. Ship the
same-epoch reader first; leave a one-line `# extension point` comment
where the cross-contract branch attaches. Do not implement the
cross-contract branch in this phase unless explicitly asked.)

### 5.3 Prompt rendering — `zicato/proposer/prompts.py`

- `render_prior_experiments_block(prior_experiments, *, restrict=...) -> str`
  produces the compact three-block render of §3.5 (promoted / rejected /
  in-flight), returning `""` when `prior_experiments` is empty. It groups
  by `decision` and renders `same_contract=False` entries without their
  Δscalar. (As shipped it also takes the §11 leakage-restriction flag so
  the block obeys the same proposer-visibility discipline as the pattern
  block — see [`OVERFITTING.md`](OVERFITTING.md) §11.)
- `render_user_prompt` takes a `prior_experiments: Iterable[PriorExperiment] = ()`
  keyword. When non-empty it prepends the `## What's already been tried`
  section. **As built**, the section is prepended *before* the
  failure-profile and telemetry-insights blocks (which are themselves
  prepended after it), so the final top-to-bottom order is
  `## Recent telemetry insights` → `## Failure-mode profile` →
  `## What's already been tried` → the core loss/pattern/mutation body.
  (The original plan said "after insights, before the loss summary"; the
  implementation settled on this adjacent ordering — settled history sits
  just above the current-state body, under the round's aggregate
  signals.) The block mirrors the existing `insights`-block conditional —
  empty input omits the section and renders a byte-identical prompt.
- Update `__all__` and the `render_user_prompt` docstring.
- Do **not** touch `SYSTEM_PROMPT_TEMPLATE` — experiment memory is
  advisory user-prompt context, never part of the hard schema.

### 5.4 Proposer wiring — `zicato/proposer/proposer.py`

- Add two keyword-only params to `propose_experiment`:
  - `prior_experiments: Iterable[PriorExperiment] = ()` — the settled
    digest, assembled by the caller (the orchestrator). Passing it
    explicitly (rather than reading the index inside the proposer) keeps
    the proposer's only filesystem dependency the existing lazy analyzer
    import, and lets the orchestrator inject the round's in-flight
    siblings (§2.2) into the same list.
  - Reuse the existing `workspace_root` gate is **not** needed here —
    the orchestrator does the index read and passes the typed list, so
    the proposer stays a pure prompt-assembler over its inputs. (Contrast
    the analyzer-insights surface, which the proposer reads itself via
    `workspace_root`; experiment memory is assembled caller-side because
    the sibling entries are only known to the orchestrator.)
- Thread `prior_experiments` into the `render_user_prompt(...)` call
  inside the retry loop (it is loop-invariant, so it is rendered into
  every attempt unchanged).
- Update the docstring's parameter list.

### 5.5 Orchestrator fetch + threading — `zicato/orchestrator.py`

- Add a helper `_load_prior_experiments(workspace_root, epoch_id) -> list[PriorExperiment]`
  that calls `prior_experiments_for_epoch(_index_db_path(workspace_root), epoch_id)`
  inside a best-effort `try/except` (a missing or stale index must never
  abort a round — log at debug and return `[]`, mirroring
  `_ingest_experiment_into_index`).
- **Gauntlet call site (~line 684).** Compute `prior = _load_prior_experiments(workspace_root, resolved_epoch_id)`
  once, before the proposer call; pass `prior_experiments=prior`.
- **Multi-challenger field (`_evolve_multi_challenger`, ~line 1314).**
  - Compute the settled `prior` once, before the `for offset in range(field_n)` loop.
  - Maintain a running list `siblings: list[PriorExperiment] = []`,
    accumulated in mint order. Before each
    `_propose_and_apply_challenger` call, pass the concatenation
    `prior + tuple(siblings)`.
  - After a challenger is successfully proposed and applied (the
    `_AppliedChallenger` branch), append an `in_flight` `PriorExperiment`
    built from that challenger's `experiment.hypothesis` (`core_idea`,
    `modulating`) to `siblings`. A challenger whose proposer *failed*
    contributes no sibling entry (there is no hypothesis to share).
  - Add a `prior_experiments` keyword to `_propose_and_apply_challenger`
    and thread it into its inner `propose_experiment(...)` call (~line 1155).
- **Standalone `propose` CLI path (~line 1326 region / `zicato propose`).**
  If the standalone propose command calls `propose_experiment` directly,
  pass `prior_experiments=_load_prior_experiments(...)` there too so the
  debug command matches the loop. (Verify the exact call site; if
  `propose` routes through the same orchestrator entry it inherits the
  wiring for free.)

### 5.6 Tests to add

Match the existing test layout (`tests/` mirrors `src/zicato/`):

- **`tests/index/test_prior_experiments.py`** —
  - Empty / missing index → `[]`.
  - Unsettled rows (`tournament_decision IS NULL`) are skipped.
  - All promoted wins are retained; `modulating` is lifted from
    `hypothesis_json`; a malformed `hypothesis_json` degrades to an empty
    `modulating` tuple, not an exception.
  - Capping: with > `max_entries` experiments, all wins survive and the
    sharpest-regression recent rejections fill the remainder; near-zero
    rejections fall off first.
- **`tests/proposer/test_prior_experiments_block.py`** —
  - Empty input → `""` and the section is omitted from `render_user_prompt`.
  - Promoted / rejected / in-flight grouping renders as in §3.5;
    `same_contract=False` entries render without a Δscalar.
  - The `## What's already been tried` section lands below the round's
    aggregate signals (`## Recent telemetry insights`,
    `## Failure-mode profile`) and above the core loss/pattern/mutation
    body — the as-built ordering of §5.3.
- **`tests/proposer/test_proposer_prior_experiments.py`** — a stub
  `aux_call_llm` captures the user prompt; assert the block appears when
  `prior_experiments` is non-empty and is absent when it is empty.
- **`tests/orchestrator/` (extend the multi-challenger field test)** —
  assert siblings accumulate: with a stub proposer that records each
  call's prompt, challenger k's prompt contains the in-flight core-ideas
  of challengers 0..k-1, and a failed challenger contributes no sibling
  line.

### 5.7 What this phase does NOT touch

No change to: the schema (`index/schema.py` — `experiments` already has
every column), `index/ingest.py`, the system prompt, the scalar /
scoring, the promote gate, the tournament structures, the dashboard, or
`journal.md` rendering. The contract-hash and auto-epoching machinery is
read-only here (the reader scopes by `epoch_id`).

## 6. Cross-references

| Topic | Document |
|---|---|
| Hypothesis schema, the experiment record this digest reads from | [EPOCHS-AND-JOURNALING.md](EPOCHS-AND-JOURNALING.md) §3 |
| Why the proposer is greedy today; the autoresearch-inspired memory direction | [RATIONALE.md](RATIONALE.md) §17 |
| The proposer's place in the meta-loop data flow | [ARCHITECTURE.md](ARCHITECTURE.md) §4.7, §8 |
| The `experiments` / `tournaments` tables this digest reads | [ANALYTICAL-INDEX.md](ANALYTICAL-INDEX.md) |
| Orthogonal to tournament structure (gauntlet / swiss / racing / elim) | [SELECTION.md](SELECTION.md) §1, [TOURNAMENT.md](TOURNAMENT.md) §1 |
| The multi-challenger field whose siblings this digest dedups | [TOURNAMENT-STRUCTURES.md](TOURNAMENT-STRUCTURES.md), [SELECTION.md](SELECTION.md) §9 |
| The proposer brief — the static operator steering this complements | [EPOCHS-AND-JOURNALING.md](EPOCHS-AND-JOURNALING.md) §7 |
| Glossary | [VOCABULARY.md](VOCABULARY.md) |
