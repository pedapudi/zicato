# zicato — board reflection (evaluation-contract validation)

> **Status: partially implemented.** Board reflection is a `reflect` mode that
> runs boards and analyzes the *observed behavior* to validate and tune the
> evaluation contract itself — debug judges, calibrate the loss function and
> gate margin, and optimize board composition and tournament design. It is a
> companion to [`OVERFITTING.md`](OVERFITTING.md), [`SCORING.md`](SCORING.md),
> [`LOOP-HEALTH.md`](LOOP-HEALTH.md),
> [`TOURNAMENT-BUILDER.md`](TOURNAMENT-BUILDER.md), and the audit-driven
> [`FUNCTIONALITY-RECOMMENDATIONS.md`](FUNCTIONALITY-RECOMMENDATIONS.md). The
> two lists below state what runs today and what remains designed only.

## What is implemented

**The pre-flight and passive tier.** The contract pre-flight
(`zicato/epoch/preflight.py`, whose module docstring labels it
`Board-reflection v1`) runs default-on with the gate
`RuntimeConfig.preflight_gate` set to `warn`, `refuse`, or `off`. Beside it run
the noise floor measured by scoring one unchanged candidate against itself
repeatedly (`tournament/calibration.py`) and the seventeen
loop-health detectors in `health/diagnostics.py`; `assess_loop_health` composes
sixteen of them, and the judge-reliability paths call `detect_noisy_judge`
separately. Judge test–retest lives in `judge_runtime/reliability.py` and is
reached from the CLI as `zicato board judges --test-retest`. The placebo arm,
the per-round `RoundLog`, per-judge loss decomposition on disk
(`LossProfile.per_judge_loss`), and the reserved replicate-base ledger all run
today.

**Run-artifact capture.** Runs persist `result.json` and `judge_io.jsonl`,
which supply the fidelity ladder the adjudicator reads. The capture design is
specified in full below.

**The corpus and the first two pillars.** `reflection/plan.py` writes the
pre-registered `plan.json` and carries the stop/resume `executed` flag.
`reflection/corpus.py` holds both corpus builders: `ingest_lineage` references
lineage artifacts and makes zero LLM calls, and the active `run_corpus`
scheduler executes at `REFLECTION_REPLICATE_BASE + j` = `5000 + j`, voids
infrastructure aborts, and is cache-idempotent. The passive ingest reads only
the replicate slots that `is_own_code_board_draw`
(`tournament/unit_cache.py`) admits: the tournament duel and its replicates
(0–999), the noise-floor calibration draws (1000s), the evidence-gate draws
(4000s), reflection's own draws (5000s), and the eval-synthesis admission probes
(6000s). That allowlist excludes the pre-flight's probes, which are degraded by
design (2000s), and the screening draws (3000s). Both cache in the same
directory under
a real generation id and would otherwise read as that generation's behaviour. A
band no owner has claimed is excluded by default. `reflection/analysis.py`
computes the reliability pillar (consumed
noise floor, seeded-bootstrap decision-flip, judge self-consistency fed to
`detect_noisy_judge`, cited placebo) and the discrimination pillar (per-entry
differentiation, the entry×candidate matrix, greedy Pearson redundancy
clustering, closed-form power analysis, coverage), with no LLM calls of its own.
The decision-flip bootstrap resamples k draws with replacement and takes their
mean, matching the mean-of-K estimator the gate reads, and returns
`p_flip=None` with a stated reason when a unit has fewer than two replicates or
a candidate has no observations, rather than a fabricated `0.0`. Judge
self-consistency feeds the detector a pooled disagreement rate and keeps the
worst-unit rate as a secondary diagnostic. `sigma_from_noise_floor()` derives
the per-unit σ the power analysis needs, and the bootstrap seed folds in the
(parent, child) pair.

**The adjudication tier.** `reflection/adjudicator.py` is the meta-judge
engine. `observation_to_judge_context` selects the highest available fidelity —
verbatim `judge_io`, then `result.json`, then an `events.jsonl` preview —
reusing `_freeze_context`. The adjudicator model arrives through the
`RuntimeConfig.adjudicator_call_llm` seam and
`effective_adjudicator_call_llm()`. Collusion is blocked by a hard
`assert_distinct_callables` check plus a soft warning when the adjudicator's
model string matches a judge's; the identity guard re-raises an
adjudication-specific error naming the fix. The strict-JSON protocol is
versioned by `ADJUDICATOR_PROMPT_VERSION` (currently `2`); a malformed response
is retried once with a corrective suffix naming the parse failure, and a second
failure yields `verdict="ambiguous"` rather than raising. The user prompt is
de-anchored: it carries neither the judge's verdict nor its claimed severity, so
the meta-judge decides blind. Verdicts persist in an idempotent
`adjudication/{judge}/{run_ref}.json` cache that is staleness-aware. A persisted
verdict counts as a hit only when the adjudicator model, the prompt version, the
requested `k_adj`, and the currently available fidelity tier all still match. A
model swap, a prompt bump, a replication change, or a preview-to-verbatim
upgrade therefore re-adjudicates and overwrites. `from_json` defaults
the staleness fields to `0` and `""`, so a cache written without them can never
read as fresh. The cache writer routes through the fsync'd atomic JSON writer.
Optional `k_adj` replication produces `adjudicator_self_agreement`.
`reflection/scorecards.py` builds the confusion matrices in this document's
schema, excluding AMBIGUOUS decisions from the rates and counting them
separately, and reports the `disagreement_rate` alongside the Fleiss
`self_consistency_kappa` under their own names. It computes cross-judge
`redundant_with` and `conflicts_with`, skipping zero-variance judges (those that
always fire or never fire) from that cross-correlation, and groups results by
fidelity tier. `reflection/findings.py` emits ranked, evidence-linked findings
whose `proposed_op` names a real builder op validated against that op's
`inspect.signature` at emit time — a margin finding is a
`set_gate {promote_margin: 2.5× floor}` payload, judge pruning is a
`set_weights {per_judge_weights: {j: 0.0}}` payload. Scripted double
adjudicators for tests live in `zicato/testing/adjudicators.py`.

**The command surface.** `zicato inspect reflection`
(`cli/commands/reflect.py`, auto-discovered) carries five subcommands. `run`
builds the corpus by referencing the lineage's artifacts with no LLM calls,
analyses the four pillars, adjudicates when an independent meta-judge is
supplied, and persists `corpus.jsonl`, `adjudication/`, `scorecards.json`,
`findings.json`, and `summary.json`. `--pre-register` writes `plan.json` and
stops. `--passive` and `--no-llm-adjudication` run the zero-LLM tier. Without
`--adjudicator-call-llm` the command refuses, so a run never spends adjudicator
budget the operator did not ask for. `report` renders a stored reflection,
`practices` runs the contract-and-history practice review, `suggest` synthesises
eval suggestions (see [`EVAL-SYNTHESIS.md`](EVAL-SYNTHESIS.md)), and `apply`
forks a builder draft and stages the finding's `proposed_op` there rather than
editing the sealed contract (`reflection/apply.py`).

**The index projection and the read side.** The analytical index carries a
`reflections` table and a `judge_scorecards` table, upserted at finalize and
re-derived by `zicato repair index`. Their readers tolerate a stale or absent
index and degrade to an empty payload: the files are canonical and the index is
a projection. `query/reflection_view.py` is the dashboard-free reader for the
bill-of-health summary, the scorecards, the transcript x-ray, the practice
review (`build_practice_review`), and the reflection-independent
entry×candidate matrix. The dashboard endpoints that serve the Instrument lens
are `/api/reflections`, `/api/reflection/{id}/summary`,
`/api/reflection/{id}/scorecards`, `/api/reflection/{id}/practices`, and
`/api/reflection/{id}/xray/{judge}/{run_ref}`; each reads the file first and
degrades to an empty shape rather than returning an error.

**The console Instrument lens.** `dashboard/static/js/views/instrument.js`,
registered in `shell.js`, `router.js`, and `tree.js`, renders three of the
surfaces the mockups in `reflection-viz-study/` specify: the bill of health, the
judge audit, and the transcript x-ray. A reflection landing page lists the
epoch's reflections as a dataTable. The four-pillar bill of health sits over the
ranked findings. For reliability it shows the noise floor and the
decision-flip probability, printing "n/a — insufficient replication" for a null
`p_flip`. For discrimination it shows the differentiating-entry and coverage
tallies. For validity it shows the aggregate judge F1 score (the harmonic mean
of precision and recall) and the ambiguous pile. For calibration it shows the
margin-to-noise ratio. Each finding carries a copyable
`zicato reflect apply <id> <finding_id>` invocation, because the CLI is the
apply path and the lens itself recommends only. The per-judge judge-audit scorecards show the 2×2
confusion matrix, precision, recall, the F1 score, the false positive rate,
and severity accuracy. Beside those they carry the disagreement rate and the
self-consistency κ under their own names, plus redundancy and conflict chips. A
judge that never fired is greyed and labelled "never fired". The adjudication x-ray shows the
fidelity-labelled transcript with the `evidence_span` highlighted on a text
match, beside the judge's verdict, the meta-judge's adjudication, the
adjudicator model, the prompt version, and the self-agreement figure; when the
capture was not retained it says "transcript unavailable". The view is
server-authoritative: it renders the reader payloads and derives no rate, κ, or
verdict of its own. It is digest-gated, so a no-op repaint rebuilds no DOM,
which a completed reflection permits because it is immutable and fetched once.
The tree grows an "Instrument" node under an epoch only when that epoch has
reflections.

**The practice review.** `reflection/practices.py`'s `review_practices` is a
pure, zero-LLM read over the contract, the operating history, and the reflection
artifacts. It emits a `PracticeReview` of eleven checks in the four-verdict
`sound` / `attend` / `unsound` / `unmeasured` vocabulary. Each check composes the
loop-health detector or analysis function that owns its signal rather than
re-deriving it, and each `proposed_op` is signature-validated the way the
findings are. The review persists as `practices.json`, `reflect run` writes it
on both tiers, `reflect practices` computes the contract-and-history checks
alone, and `reflect report` renders a Practice-review section.

## What is designed and unimplemented

Each item below needs both an operator go-ahead and a live model endpoint:

- Live meta-judge adjudication against a real endpoint.
- Run-the-tournament-twice validation of the bootstrap decision-flip estimate.
- The quick-judge-audit extension to the pre-flight.
- The builder's Validate panel.
- The four remaining mockup surfaces (the coherence scatter, the noise cloud,
  the loss-decomposition waterfall, and the live corpus grid). The console's
  bill of health also omits the mockup's arc gauge and top-line pillar verdict,
  because the reader carries no pillar score.

## Context

zicato's evolve loop treats `board + scoring + judges + gate` as a trusted oracle
that ranks candidates and decides promotions. That stack is an **instrument**,
and every promotion is a **measurement** it produces. If the instrument is noisy,
invalid, or insensitive, the loop optimizes against a broken signal — the same
failure the overfitting program guards against, one level higher.
[`OVERFITTING.md`](OVERFITTING.md) defends against the **proposer** gaming the
board; **board reflection validates the board itself.** It applies Measurement
System Analysis (MSA) — the quality-engineering practice of measuring a
measuring instrument before trusting its readings — to the evaluation contract.

The design turns on one asymmetry: **reliability can be measured with no ground
truth; validity cannot.** Whether the instrument is *consistent* is settled by
repetition — run the same thing twice and compare. Whether the instrument is
*correct* can only be settled against references whose true ordering is already
known.

### Validity by adjudicated observation

Rather than manufacture ground truth (synthetic controls or hand-labeled golden
sets — both are themselves error-prone instruments needing their own validation),
reflection **runs the board for real and analyzes the actual observed behavior.**
Ground truth is relocated to *the observed run, interpreted after the fact* by an
**independent adjudicator** (a stronger or different-model meta-judge, surfaced
for operator confirmation) rather than authored before the run. The candidate
*spread* needed for discrimination comes from the **real lineage** the loop
already produces.

Consequence to state plainly: pure observation detects *inconsistency*; only
adjudication assigns *direction* of error. So the spine of the design is a
**meta-evaluation layer** — an independent reader of the actual transcript that
decides whether each evaluator got it right.

Operating decisions for this design:
- **Active** — reflection may spend LLM budget to run the board (gated by the
  live-run rule: never without operator go-ahead).
- **Diagnose + recommend** — reflection never auto-edits the contract. It emits
  findings with proposed edits; the operator applies them (which rolls the epoch).
- Reflection runs **outside** the contract: it measures the contract rather than
  evolving under it, so running it never rolls the epoch. Only acting on its
  recommendations does.

## What reflection measures — four pillars

**Pillar 1 — Reliability** (repeated runs only, no adjudication).
- *Noise floor* from self-replication: run a fixed candidate K times; the
  run-to-run SD of the scalar is the measurement noise. Basis for `promote_margin`.
  **Implemented** (`tournament/calibration.py`, consumed by the preflight and
  `detect_margin_below_noise_floor`).
- *Variance decomposition* (a Gauge R&R): split total variance into candidate
  (signal) / run-to-run (noise) / judge stochasticity.
- *Judge self-consistency*: run a judge K times on the **same** transcript →
  self-agreement. **Implemented** as test–retest
  (`judge_runtime/reliability.py`). The metric it computes is a **pairwise
  disagreement rate** over the k re-judgements, which is not a chance-corrected
  κ, and it is named accordingly. The adjudication scorecards report a Fleiss κ
  beside that rate; neither figure replaces the other.
- *Decision-level reliability* (the headline number): **decision-flip
  probability by seeded bootstrap** over per-unit replicate scalars pushed
  through the pure gate decision. Running the whole tournament twice measures
  the same quantity directly, and it stays on the endpoint-gated list as a
  *validation* of the bootstrap: it spends a full tournament of real budget to
  confirm what the bootstrap resamples at no cost.

**Pillar 2 — Discrimination / power** (a spread of real candidates).
- *Differentiation per entry*: whether the entry's score moves across
  candidates. An entry whose score is flat across the whole spread carries no
  information about which candidate is better; this generalizes
  `detect_non_differentiating_entry`.
- *Information geometry*: the entry×candidate score matrix → correlation/PCA;
  redundant entries cluster (prune for cost), orthogonal entries each add a
  discrimination dimension.
- *Power analysis*: from noise floor σ and board size n, the **minimum detectable
  effect** on the scalar at a chosen confidence level, which in turn sets the
  replicate count, the board size, and the tournament structure.
- *Coverage*: which drift-kinds and capabilities the runs **exercised**, against
  what the judges and the loss watch for. A judge guarding a kind no run
  triggered is reported as **untested** rather than as validated.

**Pillar 3 — Validity** (adjudicated observation).
- *Judge audit*: an independent meta-judge cross-checks every judge decision
  against the actual transcript → false-fire / missed-fire piles, each linked to
  the offending span (see confusion-matrix definitions below).
- *Score↔behavior coherence*: rank runs by |scalar move| and by observed severity;
  the *disagreements* (aborted-but-fine, observable-failure-but-flat-loss,
  clean-transcript-but-penalized) are where the loss is blind or miscalibrated.
- *Loss validity*: rank correlation (Kendall τ) between the scalar ordering and the
  adjudicated ordering over the candidate spread.

**Pillar 4 — Calibration** (recommend-only; the risky, highest-value pillar).
- *Margin from noise floor*: recommend `promote_margin ≈ 2–3× noise SD`; ROC-sweep
  the margin over adjudicated pairs to pick an operating point.
- *Loss-term decomposition*: on the observed runs, decompose the scalar into its
  terms (severity / per-kind / per-judge / namespace); a term that never moves the
  scalar is dead, one that swamps the rest is drowning the signal.
- *Judge pruning*: drop judges that correlate ~1 with another (no independent
  signal) or systematically conflict.
- **Loss-weight fitting is deferred** (see Non-goals): fitting the instrument
  overfits in the same way the proposer overfits the board. It would need its
  own reference-set train/validate split, and no part of it is implemented.

## Practice review — the narrative layer above the pillars

The four pillars answer **what the numbers say** about one contract: how noisy,
how discriminating, how valid, how calibrated. The **practice review** answers a
different question — **what to change about the way the board evaluates**. It
reads the contract, the operating history, and (when present) the reflection
artifacts, and reports which evaluation practices are sound and which are not.
Its output is a statement about the board's construction, such as "the board's
oracles are all substring matches, which saturate", rather than a per-judge
statistic such as "judge X has precision 0.4".

The review reports sound practices as well as deficient ones. A review that only
reports problems gives the operator no signal about what to preserve, while a
review that also reports "your margin clears the measured floor 3.1× — sound"
identifies a property worth holding fixed through later contract edits. So
`sound` verdicts are **reported, never suppressed.**

**Placement — the free passive tier.** The practice review makes **zero LLM
calls**: it is a pure read over the contract (`board` / `scoring` / `epoch`), the
operating history (`experiments`, the persisted `noise_floor` / `preflight`), and
the reflection artifacts (`scorecards` / the corpus term-contributions) when a
`reflect run` produced them. It therefore rides the same passive, always-free tier
as continuous reflection — and a dedicated `zicato inspect reflection practices` runs the
contract+history checks on **any** epoch instantly, with no corpus at all.

**Each check composes the code that owns its signal.** Where a loop-health
detector or an analysis function already owns a signal, the practice check
**calls it and translates its finding**: `statistical_power` consumes
`power_analysis` (fed by `sigma_from_noise_floor`), `placebo_outcomes` composes
`detect_placebo_promoted`, `generalization_trend` composes
`detect_generalization_gap`, and `promotion_hygiene` composes
`detect_margin_below_noise_floor`. The practice verdict follows the detector and
never re-implements its arithmetic, so a threshold change in the detector moves
the practice verdict with it and the two cannot drift apart.

### The verdict vocabulary

| verdict | meaning |
|---|---|
| `sound` | the practice is correct; reported rather than suppressed, so the operator knows what to preserve |
| `attend` | a soft deficiency worth the operator's attention, short of unsound |
| `unsound` | an anti-practice the noise doctrine / overfitting program names as wrong |
| `unmeasured` | the check's inputs are absent — reported honestly with what is missing, **never a fabricated verdict** |

An `unmeasured` verdict is a first-class outcome: a check that lacks its noise
floor, its scorecards, or its corpus says so (and names the missing input) rather
than inventing a `sound`/`unsound` it cannot support. Honesty over coverage.

### The check catalog

Each check's rationale is one line grounded in the noise doctrine
(04-evaluation-statistics.md) or the overfitting program (OVERFITTING.md); the
grounding section is cited in the check's docstring. Thresholds are module
constants in `reflection/practices.py` with rationale comments.

| id | verdict inputs | rationale (one line) | remediation |
|---|---|---|---|
| `oracle_mix` | board expectation kinds | a board whose oracles are all `expected_text` or `regex` saturates: every candidate passes and the entries stop discriminating (ch.04 §3; issue #84) | authoring only — rewrite the expectations in the board editor |
| `judge_criterion_quality` | inline judge bodies; `scorecards` | underspecified criteria breed ambiguous adjudications (ch.04 §10) | authoring only (edit the judge body) |
| `statistical_power` | `noise_floor`→σ, replicates, train board size, `promote_margin` | when the minimum detectable effect exceeds the margin, the gate cannot resolve a difference the size of its own threshold, so no promotion at this power carries evidence (ch.04 §3, §13) | `set_param` — raise `replicates` until the minimum detectable effect drops below the margin (capped at 8) |
| `overfitting_posture` | `overfitting`, `proposer_quality`, board size, promotions | memorization defense must scale with a splittable board (OVERFITTING.md §4/§6/§7) | `set_holdout` / `set_screening` |
| `loss_monoculture` | corpus term-contributions (or namespace/judge weights) | a monoculture loss optimizes one blind spot (ch.04 §1.5) | `set_namespace_weights` (advisory sketch) |
| `budget_sanity` | entry wall-clock budgets | a >10×-median entry dominates the round's wall-clock (builder validate heuristic) | authoring only (retune the budget) |
| `calibration_freshness` | `noise_floor` age vs lineage, `promote_margin` | a noise floor measured many generations back calibrates the current gate against the noise of a different lineage state (ch.04 §3, §4) | re-run `zicato board audit` |
| `placebo_outcomes` | placebo `experiments`, cadence | a rejected placebo proves gate discrimination; a promoted one disproves it (ch.04 §11) | `set_holdout` (set/keep the placebo cadence) |
| `generalization_trend` | holdout/train gap over lineage, rotation | a widening holdout gap is board memorization (OVERFITTING.md §6/§7) | roll the epoch / `set_holdout` rotation |
| `promotion_hygiene` | promotions, evidence gate, margin vs floor, holdout | a promotion on a sub-floor margin with no evidence gate promotes noise (ch.04 §3, §6) | `set_gate` — lift `promote_margin` clear of the floor, but only when `2.5 × delta_std` exceeds it. When the floor's range and its dispersion disagree, the check reports the diagnosis and proposes no op, rather than shipping one that would LOWER the margin |
| `weight_revisit` | default-weighted judges, `scorecards` reliabilities | a judge left at default weight despite divergent measured reliability mis-weights the loss (ch.04 §10) | `set_weights` (advisory `per_judge_weights`) |

### Output shape and the apply path

The review is a `PracticeReview` (`reflection/practices.py`) — a list of
`PracticeCheck` results, each `{check_id, verdict, headline (one sentence, numbers
inline), evidence, rationale (one line), proposed_op, unmeasured_reason}`. A
`proposed_op` — present only for the mechanically-fixable checks — names a REAL
builder op and is VALIDATED against that op's signature at emit time via the same
`validate_proposed_op` the findings use, so a payload the builder would reject
never ships. The apply path is identical to the findings': the operator carries a
proposed op to a **builder draft** and seals it there (sealing rolls the epoch);
the review, like everything reflection produces, is **recommend-only** and
operator-facing, and never crosses into the proposer envelope.

The review persists as `practices.json` in the reflection directory; the file is
canonical and the reader degrades on its absence. `reflect run` writes it on
both the passive and the full tier, since it costs nothing to compute.
`reflect report` renders a **Practice review** section in three parts. The
`sound` verdicts come first, so the deficiencies that follow are read against a
statement of what the contract already gets right. The `attend` and `unsound`
deficiencies follow, ranked worst-first. The `unmeasured` checks come last, each
naming the input it needs.

## The protocol (sound experiment design)

1. **Pre-register the run plan.** Following the same discipline the loop's
   mandatory hypotheses use, write `plan.json` before running: entries,
   replicate count K, candidate set (the champion plus a lineage slice),
   adjudicator model, and the checks to run. `--pre-register` writes it and
   stops for review before spending budget. Fixing the analysis in advance stops
   the loss from being retuned to whatever the run happened to show.
2. **Execute and capture in full.** Run the board with replication, reusing the
   tournament runner, and capture the full transcripts, the event stream, every
   drift event, every judge firing and abstention with its severity, the
   per-entry loss decomposition, and the emulator's per-turn audit spans. The
   output is a frozen, version-pinned **observation corpus**.
3. **Adjudicate.** The independent meta-judge reads the captured behavior and
   produces the judge audit, the coherence divergences, and the loss
   decomposition. Independence carries the whole step: the adjudicator must run
   a different model from any judge under test, enforced by the same
   anti-collusion guard the emulator uses.
4. **Measure reliability and coverage** without adjudication: the noise floor,
   the decision-flip rate, judge self-consistency, per-entry differentiation,
   and exercised-kind coverage.
5. **Diagnose and recommend.** Emit a reflection report of ranked,
   evidence-linked findings, each carrying the transcript spans it rests on and
   a **proposed contract edit**. The operator applies the edit, which rolls the
   epoch.

### Scientific guardrails

- **Adjudicator independence and verifiability.** The adjudicator runs a
  different or stronger model than any judge under test. Every finding is
  grounded in a transcript span, so the operator confirms it by reading the
  transcript instead of trusting the verdict. The adjudicator is itself
  replicated, which measures its own reliability. These three together are
  what keeps the meta-judge from becoming an unvalidated instrument, which
  would move the validity problem up one level rather than solving it.
- **Replication** for the noise floor, **pre-registration** of the plan, and a
  **frozen corpus**, so a re-analysis compares against a fixed set of
  observations.
- **Separate fitting from evaluation.** Any calibration fit (margin, weights)
  uses a reference subset held out from the evolve board's own train and
  holdout slices — the overfitting program's discipline applied to the
  instrument.

## The persisted run artifacts adjudication reads

The protocol's step 2 requires a full capture, and adjudication (pillar 3) is
impossible without the verbatim text a judge read. Two of the run's own outputs
cannot supply it:

- **The worker's temp result file does not survive the run.** The worker writes
  `RunResult{final_output, transcript}` into a temp result file, and the parent
  reads it back and unlinks it in its cleanup `finally`
  (`tournament/runner.py`). Nothing about that path retains the user-facing
  conversation the judges graded.
- **`events.jsonl` carries previews only.** The dashboard's transcript
  reconstruction (`dashboard/transcript.py`) reads the `input_preview`,
  `output_preview`, and `summary` fields, which are truncated summaries rather
  than verbatim text. The same limit applies to a judge's decision: it reaches
  `events.jsonl` as a `JudgementEmitted` event with a one-line `detail`, which
  carries neither the judge's input bytes nor its raw response.

So each run persists two zicato-owned artifacts into its own run directory.
Both are on by default with an opt-out (`RuntimeConfig.persist_run_results` and
`RuntimeConfig.persist_judge_io`, additive runtime knobs that are never
contract-hashed, so flipping either never rolls the epoch). Both are written
best-effort, so a capture failure never re-scores or aborts a run, and
atomically, through the temp-file, fsync, rename sequence every mutable record
uses:

**`result.json`** — beside `loss.json`, replicate-slotted the same way
(`result.r{n}.json` mirrors `loss.r{n}.json`; helper `unit_result_path` in
`tournament/unit_cache.py`), written by the worker immediately after
`write_loss_profile` — including the budget-abort path's synthesized
`RunResult`:

```
{ format_version: 1, run_id, entry_id, final_output, transcript: [...],
  runtime_ms, aborted, abort_reason, clipped }
```

Each turn and `final_output` is clipped at 262144 chars (256 KiB) with a
`" … [truncated]"` marker and `clipped: true`.

**`judge_io.jsonl`** — a zicato-owned sidecar beside `loss.json`
(`judge_io.r{n}.jsonl` for replicates), append-only, one line per judge
`evaluate` call, emitted through a small `io_sink` protocol
(`judge_runtime/io_capture.py`) threaded into `_InlineCriterionJudge`. It is a
zicato file rather than a new `events.jsonl` frame, because goldfive's proto
taxonomy is pinned by three parsers that would each have to learn the new
frame:

```
{ format_version: 1, judge_name, ts, call_index,
  input: { reasoning_text, reasoning_sha256, transcript_window: [...], clipped },
  raw_response,
  verdict: { drift_emitted, kind, severity, detail } }
```

Text fields clip at 65536 chars. `reasoning_sha256` is the sha256 of the
**unclipped** input, so adjudication can verify that it is reading the same
bytes the judge read.

**The fidelity ladder.** Every reflection observation is stamped with the
fidelity of its source. Tiers are aggregated separately rather than mixed, and
a verbatim-tier finding outranks a preview-tier one:

| Tier | Source | What it supplies |
|---|---|---|
| `verbatim` | `judge_io.jsonl` | the judge's input bytes and its raw response |
| `result` | `result.json` | the full user-facing transcript and final output |
| `preview` | `events.jsonl` previews | truncated summaries |

A run whose directory carries neither `result.json` nor `judge_io.jsonl` — one
executed with the persistence knobs off, or before the run directory carried
them — is still analysable at the `preview` tier as long as the tier is
labelled. Preview fidelity can rank suspects; it cannot ground a verdict.

**Collusion analysis.** `RunResult`'s docstring excludes internal agent
reasoning, stating that it is "intentionally not exposed here so the emulator
and the judge cannot trivially collude with the inner harness". That exclusion
guards the **during-run** channel: what a live emulator or judge can see of the
run it is inside. Persisting the artifacts into the run directory after the run
does not reopen that channel. The file is written after the run's judgements
are settled, the worker process then exits, and no later run's judge or
emulator context reads a prior run's `result.json` or `judge_io.jsonl`. The
readers are the offline reflection engine and the operator, on the far side of
the process boundary.

## Data model — the observation corpus

A reflection's artifacts are stored under the contract it validated:
`epochs/{epoch_id}/reflections/{reflection_id}/` for a sealed epoch, or a
builder scratch area for a draft contract. The files are canonical and the
index rows derived from them are a projection, following the repository rule
that the filesystem is canonical and the index is derived (AGENTS.md, rule 4).

```
epochs/{e}/reflections/{id}/
  plan.json              # pre-registered run plan (entries, K, candidates, adjudicator, checks)
  corpus/
    {candidate}/{entry}/r{n}/        # one observed run (references runs/ artifacts)
      events.jsonl, loss.json        # reuse the existing run artifacts
      observation.json               # the captured behavior record (below)
  adjudication/{judge_name}/{run_ref}.json   # per-decision meta-judge verdict
  scorecards.json        # aggregated per-judge / per-entry / loss-term metrics
  findings.json          # ranked findings + proposed contract edits
  report.md / report.html
```

**Reserved replicate base.** An active-corpus run — draw j of a
(candidate, entry) unit — executes at `REFLECTION_REPLICATE_BASE + j` =
`5000 + j`, reflection's claimed row in the reserved replicate-base ledger
(dev-guide ch. 04 §8: `0` tournament duels, `1000` calibration, `2000`
pre-flight probes, `3000` screening, `4000` evidence gate, **`5000`
reflection**, `6000` eval-synthesis admission probes). Reflection follows the
same three rules as every other owner in that ledger. It stamps the index onto
the draw and keys the cache by it. It is cache-idempotent, so a re-run of the
same frozen plan re-reads the persisted draws. And it never touches the
canonical replicate-0 slots. The constant is defined in `reflection/corpus.py`
beside the active scheduler.

**ObservationRun** (`observation.json`, one per (candidate, entry, replicate)):
```
{ reflection_id, candidate_id, entry_id, replicate,
  scalar, drift_loss, pass_fail, runtime_ms, aborted, abort_cause,
  transcript_ref, drift_events: [{kind, severity, judge_name, span_ref}],
  judge_decisions: [{judge_name, fired, severity, claim, transcript_span}],
  loss_decomposition: {term_name -> contribution_to_scalar} }
```

**JudgeAdjudication** (`adjudication/{judge}/{run_ref}.json`, one per judge decision):
```
{ judge_name, run_ref, observed: "fired"|"silent",
  adjudicated: "should_fire"|"should_be_silent"|"ambiguous",
  verdict: "TP"|"FP"|"FN"|"TN"|"ambiguous",
  severity_match: bool|null,            # fired, but at the right severity?
  evidence_span, meta_judge_rationale,
  meta_judge_model, adjudicator_self_agreement,   # κ over replicated adjudication
  operator_confirmed: bool|null }
```

## Judge audit — confusion-matrix definitions

Every cell below is decided by the **adjudicated transcript** rather than by a
label authored before the run:

| Observed \ Adjudicated | transcript exhibits the failure | transcript clean |
|---|---|---|
| judge **fired**  | **TP** | **FP** (false fire) |
| judge **silent** | **FN** (missed fire) | **TN** |

- `precision = TP/(TP+FP)`, `recall = TP/(TP+FN)`, `FPR = FP/(FP+TN)`, and the
  F1 score (the harmonic mean of precision and recall).
- **Ambiguous** decisions, where neither the adjudicator nor the operator can
  decide, are excluded from the rates and counted separately. A large ambiguous
  pile is itself a finding: the judge's criterion is underspecified.
- **Severity correctness** is tracked apart from fire-versus-silence. A judge
  that fires at `warning` where the transcript warrants `critical` has detected
  the failure correctly and graded it wrongly, which still mis-weights the loss.
- **Self-consistency**, over K replicates of the same transcript, is reported as
  two figures under their own names: the test–retest **pairwise disagreement
  rate** (`judge_runtime/reliability.py`), which is not chance-corrected, and a
  chance-corrected **Fleiss κ** computed beside it in the scorecards. A judge
  below the reliability threshold is unreliable whatever its precision and
  recall say.
- **Cross-judge** matrix: pairwise correlation of firings produces
  `redundant_with` (correlation near 1, a pruning candidate) and
  `conflicts_with` (systematic disagreement).
- **Exercised**: whether any run triggered the kind this judge guards. When none
  did, the judge is reported **untested**, since this corpus cannot validate it.

**JudgeScorecard** (`scorecards.json`):
```
{ judge_name, n_decisions, tp, fp, fn, tn, ambiguous,
  precision, recall, f1, fpr, severity_accuracy,
  disagreement_rate, self_consistency_kappa,     # both, honestly named
  redundant_with: [{judge, corr}], conflicts_with: [{judge, corr}],
  exercised: bool, recommendation: str }
```

## The `reflect` command surface

```
zicato inspect reflection [--workspace PATH]
  --epoch EPOCH_ID                 # contract to validate (default: current)
  --candidate GEN_ID ...           # default: champion + recent lineage slice
  --entries ENTRY_ID ...           # default: whole board
  --replicates K                   # default: from the noise-floor target
  --adjudicator-call-llm SPEC      # the independent meta-judge; MUST differ from any judge model
  --checks judge-audit,reliability,coherence,decomposition,discrimination,coverage   # default: all
  --no-llm-adjudication            # operator-only adjudication → reliability + coverage only (cheap)
  --pre-register                   # write plan.json and STOP (review before spending)
  --max-wall-clock-seconds N       # budget ceiling
  --output PATH                    # report destination

zicato inspect reflection report <reflection_id>            # render a stored reflection report
zicato inspect reflection apply  <reflection_id> <finding_id>  # fork a BUILDER DRAFT + apply the finding's
                                                 # proposed_op; the operator seals via the
                                                 # builder (sealing rolls the epoch)
```

Two further subcommands sit beside those three: `zicato inspect reflection
practices` runs the practice review over the contract and history with no
corpus, and `zicato inspect reflection suggest` synthesises eval suggestions
(see [`EVAL-SYNTHESIS.md`](EVAL-SYNTHESIS.md)).

The **builder's "validate" action calls `reflect`** on the draft contract,
because validating the instrument belongs at the point where a contract is
authored and sealed. The builder owns the decision interface; the `reflection/`
engine owns the analysis. The builder's Validate *panel* is on the
endpoint-gated list: the builder's preflight op already covers cheap
authoring-time validation, and `reflect apply`'s draft fork already carries a
finding into the builder.

## The evolve pre-flight checks

`zicato/epoch/preflight.py`, whose module docstring labels it
`Board-reflection v1`, runs default-on at evolve start, gated by
`RuntimeConfig.preflight_gate` — `"warn"` (the default), `"refuse"`
(hard-stop via `PreflightRefusedError`), or `"off"`. Its verdict persists
onto the epoch record and surfaces through the loop-health channel.

- **Noise floor** — K same-versus-same champion draws; the scalar's standard
  deviation (`tournament/calibration.py`, cache-idempotent with
  `zicato board audit`).
- **Achievable signal** — the champion scored against degraded ephemeral copies
  of itself, over a deterministic, role-diverse sample of mutation points
  (`select_probe_points`). Sampling several points means one inert point cannot
  veto a contract, and probing short-circuits once the verdict is settled, so a
  healthy contract still costs one draw (issue #106). The verdict is `refuse`
  when the largest signal does not clear the noise floor, a `saturated` warning
  when every probe scores identically, and a distinct `inert` verdict when the
  probes moved nothing while the same-versus-same draws did vary. An `inert`
  result means the signal is *unmeasured* rather than zero, so it never
  refuses; it is rare, because a probe scoring the champion's mean to the last
  digit requires a quantized scoring scale. The `refuse` verdict's health finding is
  **critical only under `preflight_gate="refuse"`** and a warning otherwise,
  because it re-fires from the persisted record every round: grading it
  critical under the default gate would trip the degenerate-health breaker and
  hard-stop a run the operator chose to let run. A probe-selection
  configuration error — an unknown pinned id, an over-wide ceiling — also
  refuses under the hard gate, since the best-effort path exists to absorb
  endpoint outages rather than typos.
- **Margin sanity** — `detect_margin_below_noise_floor` catches a gate promoting
  on noise. Beside it, `preflight_window_verdict` compares the margin against
  both the floor and the achievable signal (issue #112): `empty_window` reports
  that no margin is defensible at all, and `margin_above_achievable` reports
  that the margin exceeds the measured DEGRADATION signal. All of these warn and
  none refuses, because the upper comparison measures how much the champion has
  left to LOSE, which does not bound how far a challenger can improve (issue
  #119). It therefore names a number worth checking rather than stopping the
  run. Margin *recommendations* scale `delta_std`, which is stable across draw
  counts, rather than the range, which climbs as calibration accumulates draws
  (`recommended_promote_margin`). When the board is split, `holdout_window_note`
  states the holdout confirmation's own bounds (`holdout_margin` and
  `holdout_entry_regression_budget`, issue #118) in prose.
- **Dead judge, dead entry, degenerate scoring, flat drift** — four of the
  seventeen loop-health detectors in `health/diagnostics.py`
  (`detect_dead_judge`, `detect_non_differentiating_entry`,
  `detect_degenerate_scoring`, `detect_flat_drift_signal`).
- **Noisy judge** — test–retest disagreement above threshold
  (`detect_noisy_judge`, fed by `judge_runtime/reliability.py`).

The pre-flight runs the loop-health detectors before the epoch spends rounds
rather than after, which is what makes it a gate rather than an observation.
The **quick judge audit** is a small meta-judge pass over the champion's latest
runs that surfaces obvious false fires and missed fires with their spans. It is
on the endpoint-gated list instead of in this set, because it spends real
adjudicator budget and needs a live endpoint plus an operator go-ahead.

## Continuous passive reflection

Continuous passive reflection re-analyses the runs `evolve` already produces,
offline and across rounds. It accumulates the entry×candidate matrix, recomputes
discrimination, redundancy, and judge drift, and surfaces the results in the
dashboard and as enriched loop-health findings. It makes no LLM calls and runs
no adjudication, so it costs nothing to keep on. What it catches is the
instrument changing as the board ages.

## Implementation — one engine, three surfaces

The dedicated mode, the pre-flight, and the continuous tier run the **same
analysis at three cadences and cost points**, so one engine feeds three
surfaces.

- **`reflection/` engine** — pure analyzers computing the four pillars over the
  observation corpus, plus an active scheduler that reuses the tournament runner
  to *produce* the corpus and an independent meta-judge to *adjudicate* it.
- **`zicato inspect reflection`** — the deep active validation and its report,
  and the analysis the builder's "validate" action calls.
- **evolve pre-flight** — the cheap subset above, default-on.
- **continuous passive** — offline re-analysis feeding the dashboard and
  loop-health.

**What the engine reuses:**
- `tournament/runner.py` and `_tournament_worker.py` execute the board.
- `dashboard/transcript.py`, `RunResult`, `events.jsonl`, and `emulator/audit.py`
  spans supply the observations.
- `judge_runtime/` and the two-callable anti-collusion guard adjudicate
  independently.
- `synthetic/` supplies optional adversarial and clean entries as *additional*
  exercised behavior; they are never the primary ground truth.
- `index/` stores the corpus and findings as derived rows, and `analyzer/`
  renders the reflection report at the grain of the epoch report.
- `health/diagnostics.py` supplies the pre-flight detectors.
- `builder/` carries the "validate" action and applies recommended edits.
- `epoch/contract.py` handles the contract edit that applying a recommendation
  is, which rolls the epoch — so reflection sits at authoring time or an epoch
  boundary by construction.

## Seven design decisions and what they settle

1. **Decision-flip by seeded bootstrap.** The headline reliability number is
   computed by bootstrap resampling per-unit replicate scalars through the pure
   gate decision. It costs no model calls and is deterministic under a seed.
   Running the tournament twice measures the same quantity directly, and stays
   on the endpoint-gated list as a *validation* of the bootstrap.
2. **The κ and the disagreement rate are reported separately.** The test–retest
   pairwise disagreement rate keeps that name, and Fleiss κ is computed beside
   it in the scorecards. Neither figure is presented as the other.
3. **Consume the persisted noise floor.** Reflection reads the noise floor and
   pre-flight verdicts already persisted on the epoch record rather than
   re-measuring; `--fresh` re-measures on request. Calibration budget is never
   spent twice without the operator asking for it.
4. **`detect_noisy_judge` is wired in unchanged.** Reflection's judge
   self-consistency measurements feed the existing health detector, so there is
   one threshold and one finding shape rather than a parallel taxonomy.
5. **The entry×candidate matrix is a query reader.** The discrimination matrix
   is a reflection-independent `zicato/query` reader over persisted losses, so
   the continuous passive tier and the dashboard get it without a reflection
   run.
6. **Findings carry executable builder-op payloads.** Every finding's
   `proposed_op` names a real builder op and is validated against that op's
   signature at emit time. A margin finding is a
   `set_gate {promote_margin: ...}` payload; judge pruning is a
   `set_weights {per_judge_weights: {judge: 0.0}}` payload. No finding is
   prose-only.
7. **The `RoundLog` gains no judge events.** Judge input and output belong in
   the zicato-owned `judge_io.jsonl` sidecar (see the capture section) rather
   than in a new frame taxonomy that the round log's three parsers would each
   have to learn.

**Apply path**: `zicato inspect reflection apply <finding_id>` forks a **builder
draft** and applies the finding's `proposed_op` to it; the operator reviews and
seals through the builder. Reflection never edits the sealed contract directly,
so the recommend-only invariant holds end to end.

## UI — the Instrument lens

**Where it lives.** The console is organized around the time axis of evolution
— lineage over rounds, and the decisions taken at each round — so it reports
what the loop decided. Reflection reports on the measurement instrument at one
contract, which is a cross-section through the same data along a different axis.
The two do not nest, so reflection gets a **dedicated top-level "Instrument"
lens** in the console: a peer to the epoch, tournament, and lineage views, held
inside the console rather than split into a standalone application. In the
console's own structure that means a **hash-router view** plus a
**tree-sidebar entry**, registered across the standard four files
(`views/instrument.js`, the `RENDERERS` map, the router's `VIEWS` with its
parse, href, and crumb functions, and the tree entry). It reuses the console's
existing idioms: the transcript reader, the board heatmap, per-judge trends, the
[theme system](CONSOLE-DESIGN-LANGUAGE.md), and the digest-gated render
discipline. A completed reflection is immutable, so the lens fetches once and
pins a digest that makes every later repaint a no-op. The **same components**
embed in the [builder](TOURNAMENT-BUILDER.md) as its **"Validate" step**, which
is on the endpoint-gated list. So the read side takes the same three-surface
shape as the engine:

- **console Instrument lens** — monitoring a *sealed* contract: read-only
  recommendations, the deep `reflect` reports, and the continuous passive tier
  surfaced inline.
- **builder Validate panel** — authoring time: the operator runs `reflect` on
  the **draft** and applies a recommended edit before sealing.

The console reports what the loop decided; the Instrument lens reports whether
the way it decided can be trusted.

**Components.** The visual specification is a set of theme-adaptive HTML mockups
in [`docs/design/reflection-viz-study/`](reflection-viz-study/): seven surface
pages plus a landing index. The console implements three of them — the bill of
health, the judge audit, and the transcript x-ray. The other four — the
coherence scatter, the noise cloud, the loss-decomposition waterfall, and the
live corpus grid — are the deferred component specification on the
endpoint-gated list. The **transcript x-ray** is the surface the others lead to:
clicking any statistic opens the conversation the judge actually graded, with
the disagreement highlighted.

- **Bill of health (landing)** — a top-line verdict over **the four pillars** as
  a **gauge quadrant**: *Reliability* (the noise floor and the probability that
  the gate's decision flips), *Discrimination* (the share of entries that
  differentiate, plus coverage), *Validity* (the aggregate judge F1 score and
  the count of coherence divergences), and *Calibration* (margin-to-noise and
  loss-term balance). The golden-spiral mark also serves as the convergence
  motif.
- **Transcript x-ray (the surface the others lead to)** — a split view: the
  conversation with the judge's **claimed span** highlighted, beside the
  independent meta-judge's adjudication rationale and a **confirm / deny**
  toggle. The colour grammar is fixed: **TP** is a quiet-green seam, **FP** is a
  red mark where the transcript was clean, and **FN** is the highlighted span
  the judge stayed silent on.
- **Judge audit** — per judge: the **2×2 confusion matrix** (TP/FP/FN/TN) with
  `precision`, `recall`, `f1`, and `fpr`; a **self-consistency κ** gauge; a strip
  of **evidence chips** for the FP and FN piles, each clicking into the x-ray;
  and the **cross-judge redundancy and conflict graph**. A judge that never fired
  is greyed and labelled *"never fired."*
- **Coherence scatter** — runs plotted by **|scalar move| against adjudicated
  severity**. The diagonal holds the runs whose scores match their observed
  severity; the **off-diagonal outliers glow** (penalized but clean, failed but
  flat-loss) and click into the x-ray.
- **Reliability noise cloud** — a **violin** of the replicated scalars with
  `promote_margin` drawn across it. A margin that falls **inside** the cloud is
  a margin smaller than the run-to-run noise, so promotions at that margin
  cannot be distinguished from noise. The **decision-flip count** sits alongside.
- **Loss decomposition** — a **waterfall** of each term's contribution to the
  scalar, with dead terms greyed and dominating terms oversized, plus a reweight
  **preview**. The preview never fits weights; automatic fitting stays a
  non-goal.
- **Discrimination and coverage** — the **board heatmap** idiom with flat rows
  flagged, beside a **coverage map** of the drift kinds the runs exercised
  against the kinds the judges watch.
- **Live process** — an instrument-themed live hero: the **corpus grid** of
  entry × candidate × replicate cells filling in, then an **adjudication
  phase**, under the same digest-gated render discipline as the tournament live
  hero.

**From finding to contract edit.** Findings are first-class objects:
evidence-linked, ranked, each carrying its **proposed contract edit** and a
**"send to builder"** control. The console offers read-only recommendations that
the operator carries to the builder; the builder's Validate panel applies one to
the draft inline. The operator stays at the contract boundary, because the read
side never auto-edits, matching the engine.

**Design-language fit.** The lens uses the console's **"Technical" register**
with the restraint of a calibration bench: confusion matrices, ROC sweeps, and
the noise cloud rendered at the console's usual density rather than crowded.
The spiral-as-convergence motif, the theme system, and the digest-gated
server-sent-events machinery carry over unchanged. Reflection adds a new
grammar to the existing design language rather than a second design language.

**Build the lens from the grammars the console already speaks.** The
Instrument-lens surfaces — the bill of health, the practice review, the judge
audit — MUST reuse existing console components rather than introducing a
component vocabulary of their own:

- **The practice review reuses the loop-health findings panel's list treatment.**
  A practice check is a loop-health finding in a different domain, so it renders
  as the same verdict-led list row (headline, one-line rationale, evidence)
  rather than as a bespoke card grid.
- **Per-judge and per-check trends reuse the per-judge trend panel's card
  treatment** — the same card the console already uses for a judge's history,
  rather than a new tile shape.
- **Figures carry a `dn-faint` caption**, the console's established
  quiet-caption treatment, rather than a heavier frame invented for reflection.
- **Tags and chips are reserved for semantic state the console already pills** —
  a `verdict` (`sound` / `attend` / `unsound` / `unmeasured`) or a `severity`.
  These map onto the console's existing pill palette. Everything else is text.
- **Metadata is a caption line rather than a per-row tag.** The fidelity tier,
  the adjudicator model, the prompt version, and similar fields belong in a
  single `dn-faint` caption under a figure. Scattering them as a chip on every
  row would present them as semantic state, which they are not.
- **Navigation lives in the shell** — the hash router's routes and the tree
  sidebar. The lens never grows an internal navigation rail of its own; a
  reflection is reached the way every other view is reached.

## Pillar 5 — generative reflection (eval synthesis)

The four pillars above **read** the instrument: they diagnose a noisy judge, a
dead entry, or an unmeasured claim type, and recommend a contract edit. They do
not **author** a new eval. That is the fifth pillar, **eval synthesis**,
specified in its own design of record
[`EVAL-SYNTHESIS.md`](EVAL-SYNTHESIS.md). It mines episodes from the candidate
loop's observed behaviour, synthesises draft entries and judges, measures each
draft's operating characteristics (same-versus-same noise, discrimination,
leakage) **before** the operator sees it, and stages the survivors into a
builder draft through the same `reflect apply` seam. Measuring a draft eval
before adopting it treats the eval as a hypothesis about the target, the same
way the loop treats a patch. Eval synthesis reuses this document's structure
whole: the observation corpus, the adjudicated corpus, the
apply-to-builder-draft mechanism, and the operator-only envelope below. It
leaves the four pillars unchanged and consumes their demand signals as its raw
material — the dead, noisy, and redundant lists, the judge scorecards, and the
calibration ledger's unresolved claim types. It is recommend-only end to end,
like the four pillars: nothing it produces auto-edits the sealed contract. See
EVAL-SYNTHESIS.md for the episode taxonomy, the contamination-control
discipline, and the admission pipeline.

## The proposer envelope — reflection output is operator-only

Reflection findings, scorecards, and adjudication rationales are
**operator-facing only**: nothing reflection produces is placed in the
proposer's prompt envelope. The proposer receives a bounded view of outcome
statistics through the banding in `proposer/prompts.py`, whose numeric inputs
pass through `sanitize_operator_marginals`
(`analyzer/outcome_marginals.py`) and whose exposure is gated by
`OverfittingConfig.restrict_proposer_visibility`. Any later decision to cross
reflection signal to the proposer MUST route it through that same machinery —
banded, sanitized, visibility-gated — and never as raw findings. A proposer
that can read the judge audit can optimize against the judges' measured blind
spots, which is the overfitting program's threat model applied one level up.

## Build order

The four analyses land in this order, cheapest and most directly tied to the
stated goal first:

1. **Judge audit on champion runs** — debugging the judges from real runs and an
   independent meta-judge, with no authored labels anywhere.
2. **Noise floor into `promote_margin`** — cheap to compute and it moves the
   gate's central threshold.
3. **Score-versus-behavior coherence and loss-term decomposition** — the
   diagnostics that inform loss tuning.
4. **Discrimination and coverage over the lineage** — board pruning and
   gap-filling.

## Non-goals

- **Automatic loss-weight fitting and automatic contract edits.** Reflection
  diagnoses and recommends; the operator applies. Fitting the instrument
  automatically overfits the instrument and would roll the epoch without the
  operator deciding to.
- **External validity.** Whether board performance predicts *production* quality
  needs deployment data. Reflection reports *coverage* — whether the board spans
  the failure modes the operator cares about — and claims no external validity.
- **Replacing the gate or the gauntlet default.** Reflection tunes the inputs to
  the gate; the gate's decision rules are unchanged.
