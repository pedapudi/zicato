# Eval synthesis — generative reflection (the instrument's second loop)

> **Status: implemented.** The episode extractor
> (`src/zicato/reflection/mining.py`), the episode-to-suggestion synthesis
> (`synthesis.py`), the admission pipeline (`admission.py`), and the
> suggestion surface (the `inspect reflection suggest` CLI mode,
> `suggestions.json` persistence, the `add_board_entry` builder operation, and
> the builder's suggestions inbox) are all built. §8 names the work that is designed and not built.
> Recommend-only end to end: nothing here ever auto-edits the sealed contract,
> and every path terminates at a builder draft the operator seals.

Companion to [`BOARD-REFLECTION.md`](BOARD-REFLECTION.md) (this is its **fifth
pillar**, cross-referenced there), [`EVAL-VIEW.md`](EVAL-VIEW.md)
(the instrument-quality readers this program consumes), and
[`OVERFITTING.md`](OVERFITTING.md) (the collusion hazard §4 defends against).

## 1. Thesis — the two-loop framing

Zicato already runs one improvement loop: the **evolve loop** improves the
**candidate** (the inner harness) against a **fixed instrument** (the
board + scoring + judges + gate). Board reflection (BOARD-REFLECTION.md)
measures that instrument's reliability, discrimination, validity, and
calibration, so that an operator can judge whether to trust how it decided.
Reflection is **diagnostic**: it reports that a judge misses failures, that an
entry is dead, that a claim type goes unmeasured. It does not **author the
fix**.

Eval synthesis is the **second loop**: it improves the **instrument** from
what the first loop observes.

> The first loop optimises the candidate against a fixed instrument.
> The second loop synthesises instrument improvements from the candidate
> loop's observed behaviour — mined episodes → drafted entries/judges → a
> statistical admission pipeline → operator review in the builder.

**Evals are hypotheses too.** The overfitting work treats every *candidate*
as a hypothesis that must clear a noise-aware gate before it is trusted. Eval
synthesis applies the same discipline to *evals*: every synthesised entry or
judge carries **measured operating characteristics** — its noise under a
self-duel, its discrimination against recent candidates, and its leakage
posture — **before the operator ever sees it**. A suggestion does not say
that an entry looks useful; it reports a flip rate of 0.10, three separated
pairs out of five recent settled ones, and a target of the incoming rotation
set, and leaves the operator to apply it or not. An unmeasured eval
suggestion is the meta-overfitting the reflection non-goals forbid (§7); a
**measured** one is a hypothesis with its evidence attached.

### Relationship to board reflection

Reflection's four pillars *read* the instrument. This is the fifth pillar:
it *writes* candidate improvements to the instrument, sourced from the same
observation corpus and the same demand signals the four pillars surface. It
reuses reflection's spine wholesale — the observation corpus
(`reflection/corpus.py`), the adjudicated corpus
(`reflection/adjudicator.py`), the findings→builder-draft apply seam
(`reflection/apply.py`), and the **operator-only output rule** (§7). It adds
one engine (the miner + synthesiser + admission pipeline) and one surface
(the `reflect suggest` CLI mode + the builder suggestions inbox). It does
**not** restructure reflection: BOARD-REFLECTION.md grows a short
cross-reference section pointing here and is otherwise untouched.

## 2. The episode taxonomy the miner extracts

An **episode** is a bounded observation from the first loop that motivates an
instrument change. The miner (`reflection/mining.py`) extracts five kinds,
each bound to a **real, tree-verified** data source. The binding discipline
follows EVAL-VIEW.md §2: bind to the shapes the real writers produce, never to
a synthetic shape the pipeline cannot emit.

Every extractor is a **pure function** over already-parsed inputs (mirroring
`reflection/analysis.py`'s pure-analyzer discipline); `mine_episodes` (§9) is
the one I/O orchestrator that reads the artifacts and calls the extractors.

### (a) FAILURE episodes — per-run `events.jsonl` + `loss.json`

**Source (verified):** the dialect-agnostic **`LossProfile` convergence
point** (TELEMETRY-DIALECTS.md §1: "`LossProfile` is the convergence point …
everything downstream reads `LossProfile` and NEVER knows which dialect
produced it"). The miner therefore binds to `LossProfile` rather than to a
dialect's raw event shape — a `goldfive` run and an `adk_events` run both fold to the
same `LossProfile`, so one binding covers both dialects. Concretely it reuses **`reflection.corpus.ObservationRun`** (`corpus.py:90`).
`ingest_lineage` builds that record at `corpus.py:460` from the persisted
`loss.json`, `result.json` and `judge_io.jsonl`, and it already carries the
fields a failure needs:

| Failure kind | `ObservationRun` binding (verified) |
|---|---|
| **predicate miss** | `pass_fail is False` (the `LossProfile.pass_fail` bit; `expectation` failed post-hoc, BOARD-FORMAT.md §3) |
| **tool-error / abort cascade** | `aborted` / `abort_cause` (a budget abort sets `BUDGET_ABORT_CAUSE`; an infra abort its own cause — `core.loss.is_infra_abort_cause`); under `adk_events`, tool errors fold to `DriftCount("tool_error", critical)` per TELEMETRY-DIALECTS.md §3.2 |
| **drift spike** | `drift_events` (`{kind, severity, judge_name, count}` off `LossProfile.drift_counts`) and `drift_loss` above a per-corpus threshold |

Fidelity rides every episode (`ObservationRun.fidelity`: `verbatim` >
`result` > `preview`), so a failure grounded in the judge's exact bytes
outranks one grounded in a truncated preview.

> **Infra aborts are not candidate failures.** An abort whose cause is an
> *infrastructure* fault (`core.loss.is_infra_abort_cause` — an endpoint 500, a
> worker crash, an unreadable result; anything but the genuine
> `BUDGET_ABORT_CAUSE`) is a **flake** rather than a behaviour the instrument
> should pin. The miner classes it separately (`infra_abort`, the softest severity) and
> seeds **no regression** — its `suggestion_hint` routes to nothing. It still
> rides the mining output for operator visibility. A genuine budget/tool abort
> cascade remains a real regression demand.

### (b) JUDGE-DISAGREEMENT episodes — reflection's adjudicated corpus

**Source (verified):** `reflection.adjudicator.JudgeAdjudication` records
(`adjudicator.py:142`) at `adjudication/{judge_name}/{run_ref}.json`
(`workspace.reflection_adjudication_path`), read via
`adjudicator.read_adjudication` (`adjudicator.py:597`). An **in-run judge vs
meta-judge flip** is a non-agreeing verdict:

- `verdict == "FP"` (`VERDICT_FP`, `adjudicator.py:103`) — the judge
  **fired**, the independent meta-judge adjudicated the transcript **clean**.
  The judge's criterion is too loose; the episode seeds a **rubric revision**.
- `verdict == "FN"` (`VERDICT_FN`, `adjudicator.py:104`) — the judge stayed
  **silent**, the meta-judge found the failure **exhibited**. The board has a
  real failure no judge catches; the episode seeds a **regression entry** or
  a **new judge** drafted from the missed span.

Each record carries `evidence_span`, `meta_judge_rationale`,
`meta_judge_model`, and `fidelity` — the episode's provenance is the
adjudication path plus that span. Absent a reflection with an adjudicated
corpus, this source yields **zero** episodes (honest degrade, never a
fabricated flip).

### (c) COVERAGE GAPS — churned mutation points × zero-discrimination board

**Source (verified), the cross:** the **mutation-surface reader** ×
the **eval-view discrimination binding** (the MATCHUP-RECORD source, NOT
`loss_profiles` pairs — the EVAL-VIEW §2.3 lesson: the `loss_profiles` PK is
`run_id`, one row per `(gen, entry)`, so same-`match_id` pairs never exist).

- **Churn** = a mutation point the proposer keeps rewriting across the
  lineage. Bound to the applied-patch history: `_read_epoch_experiments`
  (`epoch_view.py:230`) collects each generation's `patches/*.json` keyed by
  `mutation_id` (`epoch_view.py:269`, stamped onto `record["patches"]`). A
  `mutation_id` rewritten in N generations has churn N. (The current mutable
  surface — `HarnessAdapter.mutation_points()`, MUTATION-SURFACE.md §5 — is
  the optional enrichment when a live adapter is resolvable; the patch
  history is the endpoint-free binding and the primary source.)
- **Discrimination** — whether any board entry separates two candidates. Bound to
  `query.eval_view.build_eval_health` (`eval_view.py:1197`), whose `dead`
  list is entries with **zero** discrimination over the reign's settled
  matchups (read via `build_matchup_grid` per matchup — the durable
  matchup records, `_discrimination_by_entry`, `eval_view.py:693`), above the
  `_MIN_DISCRIMINATION_COMPARISONS = 3` honesty threshold (`eval_view.py:68`).

A `mutation_id` with churn ≥ threshold whose instrument has **no
discriminating entry** (the board cannot tell whether those rewrites help) is
a **coverage gap** → seeds a **coverage entry** exercising that mutation
point's surface. Provenance: the generations that churned it (the source
lineage ids) + the dead entry list.

### (d) UNRESOLVED-CLAIM types — the hypothesis calibration ledger

**Source (verified):** `tournament.detail.hypothesis_ledger(db_path,
epoch_id)` (`detail.py:943`) → `list[HypothesisGrade]`, each carrying
`movements: tuple[MovementGrade, ...]`. A **claim nothing measures** is a
`MovementGrade` the proposer predicted but the outcome **never recorded**.
`_grade_movement` (`detail.py:1133`) is where this shows: `actual is None`
yields a grade with `actual_from is None and actual_to is None`, carrying the
comment *"the proposer predicted a movement the outcome never recorded."* The
predicted `metric_name` names a channel for which the board has no entry and
no judge, so the episode seeds a **coverage entry** (a predicate) or a **new
judge** measuring that metric. (This is the same ledger the proposer-facing calibration channel —
`proposer/calibration.py`'s `CalibrationClaim` — reads; the synthesis output
never re-enters the proposer envelope, §7.)

### (e) STALENESS — dead entries + gap-detector firings → harder variants

**Source (verified):** two instrument-panel signals, both operator-side.

- **Dead entries** — `build_eval_health`'s `dead` list (above), an entry that
  never separated any two candidates. A dead entry whose earlier rounds did
  separate candidates is a saturated channel → seeds a **harder variant** (a
  perturbation that restores discrimination).
- **Gap-detector firings** — `health.diagnostics.detect_generalization_gap`
  (`diagnostics.py:607`) over the epoch's experiments: a widening
  holdout-vs-train gap is board memorization (OVERFITTING.md §6/§7). A gap
  firing is a **board-wide** demand for harder variants and rotation rather
  than one entry's → seeds harder-variant demand across the train slice, and
  links to the "roll the epoch / rotate the holdout" remediation.

**Ranking (deterministic total order).** `mine_episodes` returns episodes
sorted by a total key so a re-run is byte-stable (the eval-view fixture
discipline):

```
sort key = (−severity_rank, −recency_key, −coverage_key, episode_id)
```

- `severity_rank` — an int per episode-kind × intra-kind severity (a critical
  FN / abort cascade outranks an info drift spike); descending.
- `recency_key` — the max lineage position among the episode's source
  generations (a fresh failure outranks a stale one); descending. Derived
  from a natural-key parse of the generation id (the `_natural_key` idiom),
  so `v10` outranks `v9`.
- `coverage_key` — how many source episodes/generations the item folds (a gap
  spanning five generations outranks a one-off); descending.
- `episode_id` — the content-stable sha256 tiebreak (ascending) that makes
  the order **total** (never order-dependent on the input walk).

## 3. The suggestion types

A **suggestion** is a synthesised, admission-measured draft the operator can
carry to a builder draft. Synthesis turns ranked episodes into these; each
carries a **draft artifact** (a valid BOARD-FORMAT entry or `Judge` spec) and
a **provenance block** (§4).

| Suggestion | From episode kind | Draft artifact | Default target slice (§4) | Synthesis |
|---|---|---|---|---|
| **regression entry** | (a) failure, (b) FN | a BOARD-FORMAT entry pinning the failing input + the expectation it must now pass (BOARD-FORMAT.md §3) | **train allowed** (a regression test working as intended) | mechanical (pins a recorded episode) |
| **coverage entry** | (c) coverage gap, (d) unresolved claim | a BOARD-FORMAT entry exercising the blind mutation point / unmeasured metric | **incoming rotation** (never seen by the motivating proposer) | mechanical or model-drafted |
| **judge suggestion** | (b) FN, (d) unresolved claim | a `Judge` spec (`{name, mode: "inline", body, severity}`, BOARD-FORMAT.md §4) drafted from the observed episodes | **incoming rotation** | model-drafted (writes the criterion) |
| **rubric revision** | (b) FP | a revised judge `body` for a poorly-discriminating judge, evidence-linked to its false-positive record | edits an existing judge (no new slice) | model-drafted (rewrites the criterion) |
| **harder variant** | (e) staleness | a perturbation of a dead entry (BOARD-FORMAT.md envelope, same kind) | **incoming rotation** | mechanical (perturb) or model-drafted |

**Draft-artifact validity.** A drafted entry is validated with the real
`BoardEntry.validate` (BOARD-FORMAT.md §8) and a drafted judge with the
`Judge`/`JudgeSpec` shape before it is ever surfaced — a suggestion the board
loader would reject never ships. This extends the findings'
`validate_proposed_op` discipline one level up, to the *artifact* as well as
the operation.

**Provenance block** (§4) rides every suggestion in `board_meta` /
`context` so the operator (and the admission pipeline) can trace it back to
the episodes that motivated it.

## 4. Provenance + contamination control (load-bearing)

Synthesised evals that the motivating proposer has already seen are
**automated overfitting**: the two loops collude, and the instrument certifies
the memorization it was built to catch (OVERFITTING.md's threat model, applied
one level up).

**Every suggestion carries `provenance`** in the drafted entry's `context` /
`board_meta` (BOARD-FORMAT.md §1.0 / §1.5):

```jsonc
"provenance": {
  "miner_version": "eval-synth/1",
  "source_episodes": ["<episode_id>", ...],   // content-stable ids (§2 ranking)
  "source_refs": ["<loss_ref | adjudication_path | patch mutation_id>", ...],
  "source_lineage_ids": ["v7", "v9", ...],    // the generations that motivated it
  "suggestion_type": "regression_entry",
  "target_slice": "incoming_rotation"          // or "train"
}
```

**The rotation rule (per suggestion type).** The default target is the
**incoming-rotation set** — the slice that the proposer motivating the entry
has *not* seen (OVERFITTING.md rotation; EVAL-VIEW.md §2.4 `split_board`
membership is the canonical binding, so a synthesised entry lands byte-exact
with the gate's holdout logic). The exception is stated per type:

- **regression entry** — MAY target **train**. A regression test that pins a
  known past failure is *working as intended* when the proposer sees it: the
  point is that the failure never returns, and a proposer optimising to keep
  passing a pinned regression is doing what the operator wants.
- **coverage entry / judge suggestion / harder variant** — DEFAULT to
  **incoming rotation**. These probe *unmeasured* behaviour; a proposer that
  saw them before they were measured could optimise against the new channel
  before it is validated — the collusion path. They enter the holdout/rotation
  set so the motivating proposer meets them blind.
- **rubric revision** — edits an existing judge; no new slice, but the
  self-preference guard below applies.

**The self-preference hazard.** When the model family that *authored* a
suggestion's expected answer is also the family that will *judge* it, the
eval is self-graded — the judge prefers its own family's output. The
mitigation is the **independent-adjudicator discipline** (BOARD-REFLECTION.md
§"Scientific guardrails"): admission (§5) adjudicates a synthesised judge
with a **different/stronger** model than the judge under test, reusing the
`assert_distinct_callables` anti-collusion guard as the principle. A
suggestion whose expected answer and judge share a model family is flagged in
admission rather than silently admitted.

## 5. The admission pipeline

Admission attaches **measured operating characteristics** to a suggestion
before the operator sees it — the "evals are hypotheses" move made
mechanical. Four probes, each recommend-only; the statistics **ride** the
suggestion, thresholds never auto-reject silently.

1. **Execution probe.** The champion runs the drafted entry through the real
   board-unit runner (`_run_board_units_fast`, the corpus scheduler's path).
   Confirms the entry executes, produces a `LossProfile`, and the expectation
   is evaluable. A draft the runner cannot execute is rejected here, loudly.

2. **Self-duel noise measurement at reserved base `6000`.** Measure the
   drafted entry's own flip rate by replicating the champion against itself
   on just that entry — the `measure_noise_floor` discipline
   (`tournament/calibration.py`) restricted to the new entry, at
   **`SYNTHESIS_REPLICATE_BASE = 6000`**. This is the **next free base** in
   the reserved-replicate ledger (dev-guide 04 §8.1: *"5000 is claimed by
   board reflection; the next free base is 6000"*; CASCADE.md §6 confirms
   6000). The admission pipeline declares the constant with the §8.1 claiming
   procedure, in four steps. Pick 6000 and declare
   `SYNTHESIS_REPLICATE_BASE`. Cross-reference the ledger (this doc +
   `EVIDENCE_REPLICATE_BASE`'s docstring + `calibration.py` /
   `preflight.py` / `screen.py` / `corpus.py` + the dev-guide §8 table).
   Stamp and key with the same index. Prove r0 isolation with the
   `test_full_mode_evidence_loop_never_touches_canonical_slots` pattern. A
   high flip rate ⇒ a noisy eval; the number rides the suggestion.

3. **Discrimination probe (the matchup-record method).** Run the drafted
   entry against a spread of **recent settled candidates** (the reign's
   settled matchups, `_discrimination_by_entry`'s source) and count how many
   pairs the entry separates — three separated out of five recent pairs, say.
   A drafted
   entry that separates nothing is a dead channel *before it ships*; the
   count rides the suggestion. Same binding as EVAL-VIEW §2.3 (matchup
   records, never `loss_profiles` pairs).

4. **Leakage / collusion checks.** Verify the rotation rule (§4) holds — a
   coverage/judge suggestion targets a slice the motivating proposer has not
   seen (`split_board` membership) — and the self-preference guard (§4) — the
   suggestion's expected answer and its judge do not share a model family
   (`assert_distinct_callables`).

**The admission record rides the suggestion:**

```jsonc
"admission": {
  "execution": { "ran": true, "aborted": false },
  "noise": { "flip_rate": 0.10, "runs": 5, "measured": true, "base": 6000 },
  "discrimination": { "separated": 3, "pairs": 5, "measured": true },
  "leakage": { "target_slice_ok": true, "self_preference_flag": false }
}
```

The operator sees a flip rate of 0.10, three separated pairs out of five
recent ones, a target of the incoming rotation set, and no self-preference
flag, and decides. Thresholds (a recommended flip-rate ceiling, a minimum
discrimination) render as **advisory banners** and never drop a suggestion
silently, which keeps the posture recommend-only end to end.

**Live probes need an operator go-ahead.** The execution, noise and
discrimination probes spend real champion budget and are gated the same way
`reflect run`'s adjudication is: never without an explicit operator go-ahead
and a live endpoint. The pipeline is **fully testable against fixtures and
mocks** — the
probes monkeypatch `runner._run_single` on a seeded noise model (the
cascade-OC / power-harness precedent), so every admission statistic has a
known-answer test with zero live spend.

## 6. Surfaces

- **`zicato inspect reflection suggest`** — a `reflect` CLI mode, sibling of
  `run` / `report` / `apply` / `practices` (`cli/commands/reflect.py`,
  auto-discovered). It mines episodes (§2), synthesises suggestions (§3), and
  runs admission (§5), with the live probes behind an operator go-ahead; a
  `--no-probe` cheap tier mines, synthesises and validates artifacts only. It
  then **persists findings the way reflection findings are persisted**: a
  `suggestions.json` in the reflection directory beside `findings.json`, a
  canonical file plus a tolerant reader that degrades on absence.
  > **Reflection directories that hold suggestions alone.** A `reflect
  > suggest` run that mints a fresh reflection id writes a directory carrying
  > **only** a `suggestions.json` and no `plan.json`. The absent `plan.json`
  > distinguishes such a directory from a full `reflect run` reflection, and
  > the plan.json-keyed reflection discovery (`list_reflections`) skips it, so
  > the builder suggestions inbox scans `reflections/*/suggestions.json`
  > **directly** to surface that output. Pruning these directories is not
  > built (see the unbuilt list in §8).
- **`reflect apply` carries a suggestion into a builder draft.** The existing
  `reflection/apply.py` mechanism forks a builder draft off the live contract
  and applies a finding's `proposed_op` (verified: `apply_finding_to_draft`).
  Two operation families carry the suggestions:
  - **Edit operations** — `set_gate` and `set_weights` (findings), and, for a
    new judge or a rubric revision, `add_judge` (`builder/operations.py`,
    `add_judge(draft, entry_id, judge: JudgeSpec)`), through which a judge
    suggestion applies.
  - **New-entry operations** — `add_board_entry(draft, entry: BoardEntry)`
    (`builder/operations.py`, mirroring `add_judge`'s validate-then-replace
    shape), through which regression, coverage and harder-variant suggestions
    apply at the same draft-fork seam. The suggestion's `proposed_op` is
    `validate_proposed_op`-checked against that operation's signature at emit
    time, the same as every finding.
- **The builder's suggestions inbox and the Instrument-lens links.** The board
  editor carries a **suggestions inbox**: the ranked suggestions as verdict-led
  list rows, the loop-health findings-panel treatment BOARD-REFLECTION.md
  §"UI — the Instrument lens" mandates rather than a bespoke card grid. Each row carries its
  admission banner (flip rate and discrimination as `dn-stat` rather than
  chips) and a "stage to draft" affordance. The Instrument lens links a
  suggestion back to the x-ray of the episode that motivated it — the
  false-negative span, or the false-positive transcript. The human stays at the
  contract boundary: the inbox stages a draft, and only the operator seals it.

## 7. Envelope + cost

**Reflection is operator-side (full visibility).** Everything eval synthesis
produces — episodes, suggestions, admission statistics, the source spans — is
**operator-facing only** and NEVER enters the proposer's prompt envelope
(BOARD-REFLECTION.md §"The proposer envelope", inherited verbatim). A
proposer that could read a synthesised coverage entry before it was measured
would optimise against the new channel — the collusion §4 defends against. If
a future decision ever crosses synthesis signal to the proposer it MUST reuse
the banded / sanitised / visibility-gated `proposer/prompts.py` machinery,
never raw suggestions.

**The cost model:**

- **Mechanical synthesis calls no model.** Regression pinning (from a
  recorded episode), harder-variant perturbation, and coverage-entry
  scaffolding from a mutation point or metric name are pure transforms of
  already-captured data, and they spend no model budget. This is the passive
  tier.
- **Model-drafted synthesis is auxiliary-metered and needs a live endpoint.**
  Drafting a judge criterion, rewriting a rubric, or drafting a coverage entry
  from a model spends **`auxiliary_call_llm`** budget — the rubric-grader and
  emulator channel, never the harness callable. For live use it needs the same
  operator go-ahead as reflection's adjudication.
- **The live admission probes need the same go-ahead** — §5. The fixture and
  mock tier is free and covers the whole pipeline.

The free, always-on tier therefore mines, synthesises mechanically, and
validates artifacts. The metered tier drafts from a model and runs the live
admission probes. The default posture is reflection's own: refuse to spend
live budget without an explicit operator go-ahead.

## 8. Review focus and unbuilt work

Three areas repay adversarial review. The first is contamination control
(§4): whether every coverage and judge suggestion lands in the incoming
rotation set, and whether a regression entry can leak a holdout answer. The
second is the admission statistics (§5): whether the flip rate is measured at
base 6000 with replicate 0 untouched, and whether discrimination uses the
matchup-record method. The third is the miner's episode precision (§2):
whether the failure, false-negative and false-positive bindings are exact, and
whether a cold workspace yields no fabricated episodes.

**Designed and not built:**

- **Live synthesis and admission validation** — real model-drafted judges and
  real champion admission probes against a live endpoint, which need an
  operator go-ahead.
- **Cross-workspace episode mining** — mining episodes across epochs and
  workspaces, giving an entry's demand history beyond the current epoch. This
  needs the cross-epoch lifetime record that EVAL-VIEW.md §7 also leaves
  unbuilt.
- **Pruning reflection directories that hold suggestions alone** — a `reflect
  suggest` run that mints a fresh reflection id writes a directory with a
  `suggestions.json` and no `plan.json` (§6). Nothing prunes stale ones: they
  are cheap and operator-visible, and the inbox surfaces only the freshest.

## 9. The episode extractor

`src/zicato/reflection/mining.py`. Pure extraction functions per §2 kind + one
orchestrating `mine_episodes(paths, epoch_id) -> list[MinedEpisode]`:

- **`MinedEpisode`** — a frozen, slotted dataclass. It identifies the
  episode with `episode_id` (a content-stable sha256), `episode_type` (the
  five §2 kinds) and `subject` (entry / judge / mutation_id / metric /
  claim). It carries the three §2 ranking keys, a one-line `summary`, an
  `evidence` bag, and a `suggestion_hint` naming which §3 type it seeds.
  Last come the §4 `provenance` fields (`source_episodes`, `source_refs`,
  `source_lineage_ids`, `miner_version`).
- **Pure extractors** — `failure_episodes(observations)`,
  `judge_disagreement_episodes(adjudications)`,
  `coverage_gap_episodes(mutation_churn, health)`,
  `unresolved_claim_episodes(ledger)`,
  `staleness_episodes(health, gap_findings)`. Each is pure over
  already-parsed inputs, following `analysis.py`.
- **`mine_episodes`** — the one I/O orchestrator: builds the corpus
  (`ingest_lineage`), reads the latest reflection's adjudications, reads the
  experiments (patch churn) + `build_eval_health`, reads `hypothesis_ledger`,
  runs `detect_generalization_gap`, calls each extractor, concatenates, and
  ranks with the §2 total order. Every read is **tolerant**: a cold
  workspace, absent reflection, absent index, or malformed line degrades to
  fewer episodes. Malformed lines are counted rather than raised, and no read
  raises.

Bindings are the tree-verified sources in §2. Fixtures are **seeded from the
real writers** (real `loss.json` via `write_loss_profile`, real
`JudgeAdjudication` via `write_adjudication`, real experiment `patches/*.json`,
real `HypothesisGrade`) — never a synthetic shape the pipeline cannot emit.

## 10. Cross-references

| Topic | Document |
|---|---|
| The four pillars + the apply→builder-draft seam + adjudicator independence | [`BOARD-REFLECTION.md`](BOARD-REFLECTION.md) |
| The instrument-quality readers (discrimination, flip rate, dead evals) | [`EVAL-VIEW.md`](EVAL-VIEW.md) |
| `LossProfile` as the dialect convergence point | [`TELEMETRY-DIALECTS.md`](TELEMETRY-DIALECTS.md) |
| The board entry + judge schema the drafts obey | [`BOARD-FORMAT.md`](BOARD-FORMAT.md) |
| The mutable surface + `mutation_points()` | [`MUTATION-SURFACE.md`](MUTATION-SURFACE.md) |
| The rotation / holdout split the contamination rule binds to | [`OVERFITTING.md`](OVERFITTING.md) |
| The reserved-replicate-base ledger (6000 = next free) | dev-guide `04-evaluation-statistics.md` §8, [`CASCADE.md`](CASCADE.md) §6 |
