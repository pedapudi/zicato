# Eval synthesis — generative reflection (the instrument's second loop)

> **Status: DESIGNED. WS-MINE (the episode extractor) SHIPPING with this doc;
> WS-SYNTH / WS-ADMIT / WS-SURFACE build FROM this doc.** This is the
> program's execution contract: the miner (§9, Commit 2) lands with it, and
> the three sibling workstreams (§8) are specified so they build in parallel
> against nothing but this file, the episode taxonomy it fixes (§2), and the
> suggestion shapes it pins (§3). Recommend-only end to end — nothing here
> ever auto-edits the sealed contract; every path terminates at a builder
> draft the operator seals.

Companion to [`BOARD-REFLECTION.md`](BOARD-REFLECTION.md) (this is its **fifth
pillar** — see the cross-reference added there), [`EVAL-VIEW.md`](EVAL-VIEW.md)
(the instrument-quality readers this program consumes), and
[`OVERFITTING.md`](OVERFITTING.md) (the collusion hazard §4 defends against).

## 1. Thesis — the two-loop framing

Zicato already runs one improvement loop: the **evolve loop** improves the
**candidate** (the inner harness) against a **fixed instrument** (the
board + scoring + judges + gate). Board reflection (BOARD-REFLECTION.md)
turned the camera on that instrument and asked *"can I trust how it
decided?"* — measuring the instrument's reliability, discrimination,
validity, and calibration. But reflection is **diagnostic**: it says a judge
misses failures, an entry is dead, a claim type goes unmeasured. It does not
**author the fix**.

Eval synthesis is the **second loop**: it improves the **instrument** from
what the first loop observes.

> The first loop optimises the candidate against a fixed instrument.
> The second loop synthesises instrument improvements from the candidate
> loop's observed behaviour — mined episodes → drafted entries/judges → a
> statistical admission pipeline → operator review in the builder.

**The zicato move — evals are hypotheses too.** The overfitting program
treats every *candidate* as a hypothesis that must clear a noise-aware gate
before it is trusted. Eval synthesis applies the same discipline to *evals*:
every synthesised entry or judge carries **measured operating
characteristics** — its A/A noise, its discrimination against recent
candidates, its leakage posture — **before the operator ever sees it**. A
suggestion is not "here is an entry that looks useful"; it is "here is an
entry, flip rate 0.10, discriminates 3/5 recent settled pairs, targets the
incoming rotation set — apply it or don't." An unmeasured eval suggestion is
exactly the meta-overfitting the reflection non-goals forbid (§7); a
**measured** one is a hypothesis with its evidence attached.

### Relationship to reflection (pillar 5)

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
instrument change. The miner (`reflection/mining.py`, WS-MINE) extracts five
kinds, each bound to a **real, tree-verified** data source. The binding
discipline is the eval-view lesson (EVAL-VIEW.md §2): bind to the shapes the
real writers produce, never a synthetic shape the pipeline can't emit.

Every extractor is a **pure function** over already-parsed inputs (mirroring
`reflection/analysis.py`'s pure-analyzer discipline); `mine_episodes` (§9) is
the one I/O orchestrator that reads the artifacts and calls the extractors.

### (a) FAILURE episodes — per-run `events.jsonl` + `loss.json`

**Source (verified):** the dialect-agnostic **`LossProfile` convergence
point** (TELEMETRY-DIALECTS.md §1: "`LossProfile` is the convergence point …
everything downstream reads `LossProfile` and NEVER knows which dialect
produced it"). The miner therefore binds to `LossProfile`, not to a dialect's
raw event shape — a `goldfive` run and an `adk_events` run both fold to the
same `LossProfile`, so one binding covers both dialects. Concretely it
reuses **`reflection.corpus.ObservationRun`** (`corpus.py:90`, built by
`ingest_lineage` at `corpus.py:460` from the persisted `loss.json` /
`result.json` / `judge_io.jsonl`), which already carries the exact fields a
failure needs:

| Failure kind | `ObservationRun` binding (verified) |
|---|---|
| **predicate miss** | `pass_fail is False` (the `LossProfile.pass_fail` bit; `expectation` failed post-hoc, BOARD-FORMAT.md §3) |
| **tool-error / abort cascade** | `aborted` / `abort_cause` (a budget abort sets `BUDGET_ABORT_CAUSE`; an infra abort its own cause — `core.loss.is_infra_abort_cause`); under `adk_events`, tool errors fold to `DriftCount("tool_error", critical)` per TELEMETRY-DIALECTS.md §3.2 |
| **drift spike** | `drift_events` (`{kind, severity, judge_name, count}` off `LossProfile.drift_counts`) and `drift_loss` above a per-corpus threshold |

Fidelity rides every episode (`ObservationRun.fidelity`: `verbatim` >
`result` > `preview`) so a failure grounded in the judge's exact bytes
outranks one grounded in a truncated preview — the R1 ladder, unchanged.

### (b) JUDGE-DISAGREEMENT episodes — reflection's adjudicated corpus

**Source (verified):** `reflection.adjudicator.JudgeAdjudication` records
(`adjudicator.py:142`) at `adjudication/{judge_name}/{run_ref}.json`
(`workspace.reflection_adjudication_path`), read via
`adjudicator.read_adjudication` (`adjudicator.py:597`). An **in-run judge vs
meta-judge flip** is exactly a non-agreeing verdict:

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
- **Discrimination** = does any board entry separate two candidates? Bound to
  `query.eval_view.build_eval_health` (`eval_view.py:1197`), whose `dead`
  list is entries with **zero** discrimination over the reign's settled
  matchups (read via `build_matchup_grid` per matchup — the durable
  matchup records, `_discrimination_by_entry`, `eval_view.py:693`), above the
  `_MIN_DISCRIMINATION_COMPARISONS = 3` honesty threshold (`eval_view.py:68`).

A `mutation_id` with churn ≥ threshold whose instrument has **no
discriminating entry** (the board can't tell whether those rewrites help) is
a **coverage gap** → seeds a **coverage entry** exercising that mutation
point's surface. Provenance: the generations that churned it (the source
lineage ids) + the dead entry list.

### (d) UNRESOLVED-CLAIM types — the hypothesis calibration ledger

**Source (verified):** `tournament.detail.hypothesis_ledger(db_path,
epoch_id)` (`detail.py:943`) → `list[HypothesisGrade]`, each carrying
`movements: tuple[MovementGrade, ...]`. A **claim nothing measures** is a
`MovementGrade` the proposer predicted but the outcome **never recorded** —
verified in `_grade_movement` (`detail.py:1133`): `actual is None` yields a
grade with `actual_from is None and actual_to is None` and the comment *"the
proposer predicted a movement the outcome never recorded."* The predicted
`metric_name` names a channel the board has no entry/judge for → the episode
seeds a **coverage entry** (a predicate) or a **new judge** measuring that
metric. (This is the same ledger the proposer-facing calibration channel —
`proposer/calibration.py`'s `CalibrationClaim` — reads; the synthesis output
never re-enters the proposer envelope, §7.)

### (e) STALENESS — dead entries + gap-detector firings → harder variants

**Source (verified):** two instrument-panel signals, both operator-side.

- **Dead entries** — `build_eval_health`'s `dead` list (above), an entry that
  never separated any two candidates. A dead entry that *used to* be live is
  a saturated channel → seeds a **harder variant** (a perturbation that
  restores discrimination).
- **Gap-detector firings** — `health.diagnostics.detect_generalization_gap`
  (`diagnostics.py:607`) over the epoch's experiments: a widening
  holdout-vs-train gap is board memorization (OVERFITTING.md §6/§7). A gap
  firing is **board-wide** demand for harder variants / rotation, not one
  entry's → seeds harder-variant demand across the train slice, and links to
  the existing "roll the epoch / rotate the holdout" remediation.

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
carry to a builder draft. WS-SYNTH turns ranked episodes into these; each
carries a **draft artifact** (a valid BOARD-FORMAT entry or `Judge` spec) and
a **provenance block** (§4).

| Suggestion | From episode kind | Draft artifact | Default target slice (§4) | Synthesis |
|---|---|---|---|---|
| **regression entry** | (a) failure, (b) FN | a BOARD-FORMAT entry pinning the failing input + the expectation it must now pass (BOARD-FORMAT.md §3) | **train allowed** (a regression test working as intended) | mechanical (pins a recorded episode) |
| **coverage entry** | (c) coverage gap, (d) unresolved claim | a BOARD-FORMAT entry exercising the blind mutation point / unmeasured metric | **incoming rotation** (never seen by the motivating proposer) | mechanical or LLM |
| **judge suggestion** | (b) FN, (d) unresolved claim | a `Judge` spec (`{name, mode: "inline", body, severity}`, BOARD-FORMAT.md §4) drafted from the observed episodes | **incoming rotation** | LLM (drafts the criterion) |
| **rubric revision** | (b) FP | a revised judge `body` for a poorly-discriminating judge, evidence-linked to its FP pile | edits an existing judge (no new slice) | LLM (rewrites the criterion) |
| **harder variant** | (e) staleness | a perturbation of a dead entry (BOARD-FORMAT.md envelope, same kind) | **incoming rotation** | mechanical (perturb) or LLM |

**Draft-artifact validity.** A drafted entry is validated with the real
`BoardEntry.validate` (BOARD-FORMAT.md §8) and a drafted judge with the
`Judge`/`JudgeSpec` shape before it is ever surfaced — a suggestion the board
loader would reject never ships (the findings' `validate_proposed_op`
discipline, one level up: validate the *artifact*, not just the op).

**Provenance block** (§4) rides every suggestion in `board_meta` /
`context` so the operator (and the admission pipeline) can trace it back to
the episodes that motivated it.

## 4. Provenance + contamination control (load-bearing)

This is the section the program exists to get right. Synthesised evals that
the motivating proposer has seen are **automated overfitting** — the two
loops collude, and the instrument certifies the very memorization it was
built to catch (OVERFITTING.md's threat model, one level up).

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
**incoming-rotation set** — the slice the proposer that motivated an entry
has *not* seen (OVERFITTING.md rotation; EVAL-VIEW.md §2.4 `split_board`
membership is the canonical binding, so a synthesised entry lands byte-exact
with the gate's holdout logic). The exception is stated per type:

- **regression entry** — MAY target **train**. A regression test that pins a
  known past failure is *working as intended* when the proposer sees it: the
  point is that the failure never returns, and a proposer optimising to keep
  passing a pinned regression is doing exactly what the operator wants.
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
§"adjudicator independence"): admission (§5) adjudicates a synthesised judge
with a **different/stronger** model than the judge under test, reusing the
`assert_distinct_callables` anti-collusion guard as the principle. A
suggestion whose expected answer and judge share a model family is flagged in
admission, not silently admitted.

## 5. The admission pipeline (spec for WS-ADMIT)

Admission attaches **measured operating characteristics** to a suggestion
before the operator sees it — the "evals are hypotheses" move made
mechanical. Four probes, each recommend-only; the statistics **ride** the
suggestion, thresholds never auto-reject silently.

1. **Execution probe.** The champion runs the drafted entry through the real
   board-unit runner (`_run_board_units_fast`, the corpus scheduler's path).
   Confirms the entry executes, produces a `LossProfile`, and the expectation
   is evaluable — a draft the runner can't execute is rejected here (loudly).

2. **A/A noise measurement at the NEW reserved base `6000`.** Measure the
   drafted entry's own flip rate by replicating the champion against itself
   on just that entry — the `measure_noise_floor` discipline
   (`tournament/calibration.py`) restricted to the new entry, at
   **`SYNTHESIS_REPLICATE_BASE = 6000`**. This is the **next free base** in
   the reserved-replicate ledger (dev-guide 04 §8.1: *"5000 is claimed by
   board reflection; the next free base is 6000"*; CASCADE.md §6 confirms
   6000). WS-ADMIT declares the constant with the §8.1 claiming procedure:
   pick 6000, declare `SYNTHESIS_REPLICATE_BASE`, cross-reference the ledger
   (this doc + `EVIDENCE_REPLICATE_BASE`'s docstring + `calibration.py` /
   `preflight.py` / `screen.py` / `corpus.py` + the dev-guide §8 table),
   stamp-AND-key with the same index, and prove r0 isolation with the
   `test_full_mode_evidence_loop_never_touches_canonical_slots` pattern. A
   high flip rate ⇒ a noisy eval; the number rides the suggestion.

3. **Discrimination probe (the matchup-record method).** Run the drafted
   entry against a spread of **recent settled candidates** (the reign's
   settled matchups, `_discrimination_by_entry`'s source) and count how many
   pairs the entry separates — "discriminates 3/5 recent pairs". A drafted
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

The operator sees *"flip rate 0.10, discriminates 3/5 recent pairs, targets
incoming rotation, no self-preference"* and decides. Thresholds
(a recommended flip-rate ceiling, a minimum discrimination) are **advisory
banners**, never a silent drop — the recommend-only posture, end to end.

**LIVE probes are endpoint-gated (G3-class).** The execution / noise /
discrimination probes spend real champion budget and are gated exactly like
`reflect run`'s adjudication: never without operator go-ahead + a live
endpoint. The pipeline is **fully testable against fixtures/mocks** — the
probes monkeypatch `runner._run_single` on a seeded noise model (the
cascade-OC / power-harness precedent), so every admission statistic has a
known-answer test with zero live spend.

## 6. Surfaces

- **`zicato reflect suggest`** — a new `reflect` CLI mode, sibling of
  `run` / `report` / `apply` / `practices` (`cli/commands/reflect.py`,
  auto-discovered). Mines episodes (§2), synthesises suggestions (§3), runs
  admission (§5, endpoint-gated for the live probes; a `--no-probe` cheap
  tier mines + synthesises + validates artifacts only), and **persists
  findings the way reflection findings are persisted** — a `suggestions.json`
  in the reflection directory beside `findings.json`, canonical file + a
  tolerant reader that degrades on absence (the reflection persistence
  idiom).
- **`reflect apply` carries a suggestion into a builder draft.** The existing
  `reflection/apply.py` mechanism forks a builder draft off the live contract
  and applies a finding's `proposed_op` (verified: `apply_finding_to_draft`).
  What it supports **today** and what needs **extending**:
  - **Edit ops work now** — `set_gate` / `set_weights` (findings) and, for a
    **new-judge / rubric revision**, `add_judge` already exists
    (`builder/operations.py:892`, `add_judge(draft, entry_id, judge:
    JudgeSpec)`), so a judge suggestion applies through the existing op.
  - **NEW-entry ops need a builder op** — there is **no `add_board_entry`**
    today (only `remove_board_entry`, `operations.py:821`). WS-SURFACE adds
    `add_board_entry(draft, entry: BoardEntry)` (mirroring `add_judge`'s
    validate-then-replace shape) so regression / coverage / harder-variant
    suggestions apply through the same draft-fork seam. The suggestion's
    `proposed_op` is `validate_proposed_op`-checked against that new op's
    signature at emit time, exactly like every finding.
- **Builder board-editor suggestions inbox + Instrument-lens links**
  (spec only; a sibling builds it). The board editor grows a **suggestions
  inbox** — the ranked suggestions as verdict-led list rows (the loop-health
  findings-panel treatment BOARD-REFLECTION.md §"UI language" mandates, not a
  bespoke card grid), each with its admission banner (flip rate /
  discrimination as `dn-stat`, not chips) and a "stage to draft" affordance.
  The Instrument lens links a suggestion back to the x-ray of the episode
  that motivated it (the FN span, the FP transcript). The human stays at the
  contract boundary — the inbox stages; only the operator seals.

## 7. Envelope + cost

**Reflection is operator-side (full visibility).** Everything eval synthesis
produces — episodes, suggestions, admission statistics, the source spans — is
**operator-facing only** and NEVER enters the proposer's prompt envelope
(BOARD-REFLECTION.md §"the proposer envelope", inherited verbatim). A
proposer that could read a synthesised coverage entry before it was measured
would optimise against the new channel — the collusion §4 defends against. If
a future decision ever crosses synthesis signal to the proposer it MUST reuse
the banded / sanitised / visibility-gated `proposer/prompts.py` machinery,
never raw suggestions.

**The cost model:**

- **Mechanical synthesis is LLM-free.** Regression pinning (from a recorded
  episode), harder-variant perturbation, and coverage-entry scaffolding from
  a mutation point / metric name are pure transforms of already-captured
  data — **zero LLM budget**, the passive tier.
- **LLM synthesis is aux-metered and endpoint-gated.** Drafting a judge
  criterion, rewriting a rubric, or an LLM-authored coverage entry spends
  **`auxiliary_call_llm`** budget (the rubric-grader / emulator channel, never
  the harness callable) and is endpoint-gated for live use exactly like
  reflection's adjudication.
- **Admission's live probes are endpoint-gated (G3-class)** — §5. The
  fixture/mock tier is free and fully covers the pipeline.

So the free, always-on tier is *mine + mechanically-synthesise + validate
artifacts*; the metered, gated tier is *LLM synthesis + live admission
probes*. The default posture is the reflection default: refuse to spend live
budget without explicit operator go-ahead.

## 8. Execution plan

1. **This wave.** This doc (the program contract) + **WS-MINE**
   (`reflection/mining.py`, §9, Commit 2) — the endpoint-free episode
   extractor with its tests. Cross-reference added to BOARD-REFLECTION.md
   (pillar 5).
2. **Three parallel workstreams on this branch** (build from §3 / §5 / §6):
   - **WS-SYNTH** — episode → suggestion synthesis (mechanical + LLM),
     draft-artifact validity, provenance blocks.
   - **WS-ADMIT** — the admission pipeline (§5): the `SYNTHESIS_REPLICATE_BASE
     = 6000` claim, the four probes, the fixture/mock test tier.
   - **WS-SURFACE** — `reflect suggest` CLI, `suggestions.json` persistence,
     the `add_board_entry` builder op, the suggestions-inbox spec.
3. **Adversarial review** — pointed at (a) **contamination control** (§4: does
   every coverage/judge suggestion actually land in the incoming rotation set;
   does a regression entry ever leak a holdout answer), (b) **admission
   statistics** (§5: is the flip rate measured at 6000 with r0 untouched; is
   discrimination the matchup-record method), and (c) **miner episode
   precision** (§2: are the failure/FN/FP bindings exact; no fabricated
   episodes on a cold workspace).
4. **Fixes → integration ladder → PR.**

**Deferred + recorded** (do NOT build this wave):

- **Live synthesis / admission validation** — the endpoint-gated queue: real
  LLM-drafted judges and real champion admission probes on a live endpoint.
- **Cross-workspace episode mining** — mining episodes across epochs /
  workspaces (an entry's demand history beyond the current epoch); wants the
  cross-epoch lifetime record EVAL-VIEW.md §7 also defers.

## 9. WS-MINE — the episode extractor (this wave, Commit 2)

`src/zicato/reflection/mining.py`. Pure extraction functions per §2 kind + one
orchestrating `mine_episodes(paths, epoch_id) -> list[MinedEpisode]`:

- **`MinedEpisode`** — a frozen, slotted dataclass carrying `episode_id`
  (content-stable sha256), `episode_type` (the five §2 kinds), `subject`
  (entry / judge / mutation_id / metric / claim), the three ranking keys
  (§2), a one-line `summary`, an `evidence` bag, a `suggestion_hint` (which
  §3 type it seeds), and the §4 `provenance` fields (`source_episodes`,
  `source_refs`, `source_lineage_ids`, `miner_version`).
- **Pure extractors** — `failure_episodes(observations)`,
  `judge_disagreement_episodes(adjudications)`,
  `coverage_gap_episodes(mutation_churn, health)`,
  `unresolved_claim_episodes(ledger)`,
  `staleness_episodes(health, gap_findings)`. Each is pure over
  already-parsed inputs (the analysis.py discipline).
- **`mine_episodes`** — the one I/O orchestrator: builds the corpus
  (`ingest_lineage`), reads the latest reflection's adjudications, reads the
  experiments (patch churn) + `build_eval_health`, reads `hypothesis_ledger`,
  runs `detect_generalization_gap`, calls each extractor, concatenates, and
  ranks with the §2 total order. Every read is **tolerant**: a cold
  workspace, absent reflection, absent index, or malformed line degrades to
  fewer episodes (malformed lines counted, never a crash — the dialect
  discipline), never an exception.

Bindings are the tree-verified sources in §2. Fixtures are **seeded from the
real writers** (real `loss.json` via `write_loss_profile`, real
`JudgeAdjudication` via `write_adjudication`, real experiment `patches/*.json`,
real `HypothesisGrade`) — never a synthetic shape the pipeline can't emit.

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
