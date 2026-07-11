# zicato — board reflection (evaluation-contract validation)

> **Status: partially implemented.** A `reflect` mode that runs
> boards and analyzes the *observed behavior* to validate and tune the evaluation
> contract itself — debug judges, calibrate the loss function and gate margin, and
> optimize board composition and tournament design. Companion to
> [`OVERFITTING.md`](OVERFITTING.md), [`SCORING.md`](SCORING.md),
> [`LOOP-HEALTH.md`](LOOP-HEALTH.md), [`TOURNAMENT-BUILDER.md`](TOURNAMENT-BUILDER.md),
> and the audit-driven [`FUNCTIONALITY-RECOMMENDATIONS.md`](FUNCTIONALITY-RECOMMENDATIONS.md).
>
> **What is SHIPPED** (the passive/preflight tier): the contract pre-flight
> (`zicato/epoch/preflight.py`, self-labeled "Board-reflection v1", default-on
> `warn` / `refuse` / `off` via `RuntimeConfig.preflight_gate`), the A/A noise
> floor (`tournament/calibration.py`), the 14 loop-health detectors
> (`health/diagnostics.py`), judge test–retest
> (`judge_runtime/reliability.py`, CLI `zicato board judges --test-retest`),
> the placebo arm, the per-round `RoundLog`, per-judge loss decomposition on
> disk (`LossProfile.per_judge_loss`), and the reserved replicate-base ledger.
> **Shipped since (the capture + corpus tiers)**: the run-artifact capture fix
> (`result.json` + `judge_io.jsonl` with the fidelity ladder, R1), and the
> `reflection/` package's **corpus + pillars 1-2** (R2) — `reflection/plan.py`
> (pre-registered `plan.json` with the stop/resume `executed` flag),
> `reflection/corpus.py` (passive `ingest_lineage` referencing lineage
> artifacts with zero LLM, plus the active `run_corpus` scheduler at
> `REFLECTION_REPLICATE_BASE = 5000 + j`, infra-abort-voided and
> cache-idempotent), and `reflection/analysis.py` (pure pillar-1 reliability —
> consumed noise floor, seeded-bootstrap decision-flip, `detect_noisy_judge`-fed
> judge self-consistency, cited placebo — and pillar-2 discrimination —
> per-entry differentiation, entry×candidate matrix, greedy Pearson redundancy
> clustering, closed-form power analysis, coverage).
> **Shipped since (the adjudication tier, R3)**: the meta-judge engine —
> `reflection/adjudicator.py` (the `observation_to_judge_context` fidelity glue
> — verbatim `judge_io` > `result.json` > `events.jsonl` preview, reusing
> `_freeze_context`; the `RuntimeConfig.adjudicator_call_llm` seam +
> `effective_adjudicator_call_llm()`; HARD `assert_distinct_callables` + SOFT
> model-string collusion warning; the strict-JSON protocol at
> `ADJUDICATOR_PROMPT_VERSION = 1` with one retry then `verdict="ambiguous"`
> that never raises; the idempotent `adjudication/{judge}/{run_ref}.json` cache
> — file-exists = HIT; optional `k_adj` replication →
> `adjudicator_self_agreement`), `reflection/scorecards.py` (the doc-schema
> confusion matrices with AMBIGUOUS excluded from the rates and counted, the
> `disagreement_rate` AND Fleiss `self_consistency_kappa` beside it — honestly
> named, cross-judge `redundant_with` / `conflicts_with`, per-fidelity
> grouping), and `reflection/findings.py` (ranked evidence-linked findings whose
> `proposed_op` names a REAL builder op VALIDATED against its `inspect.signature`
> at emit time — margin → `set_gate {promote_margin: 2.5× floor}`, judge-pruning
> → `set_weights {per_judge_weights: {j: 0.0}}`), plus the scripted-double
> adjudicators (`zicato/testing/adjudicators.py`).
> **Shipped since (the surfaces, R4)**: the `zicato reflect` CLI
> (`cli/commands/reflect.py`, auto-discovered) — `run` (build the corpus by
> referencing the lineage's artifacts with zero LLM, analyse the four pillars,
> adjudicate when an independent meta-judge is supplied, and persist
> `corpus.jsonl` / `adjudication/` / `scorecards.json` / `findings.json` /
> `summary.json`; `--pre-register` writes `plan.json` and STOPS; `--passive`
> and `--no-llm-adjudication` run the cheap zero-LLM tier; the default REFUSES
> without `--adjudicator-call-llm` — the live-run gate never silently spends
> budget), `report`, and `apply` (fork a BUILDER DRAFT + stage the finding's
> `proposed_op` — never the sealed contract; `reflection/apply.py`); the index
> projection (schema v11 additive — the `reflections` + `judge_scorecards`
> tables, upserted at finalize AND re-derived by `zicato reindex`, with
> tolerant readers that degrade on a stale/absent index — files canonical, the
> index a projection); the dashboard-free `query/reflection_view.py`
> (bill-of-health summary, scorecards, transcript x-ray, and the
> reflection-independent entry×candidate matrix); and four thin dashboard
> endpoints (`/api/reflections`, `/api/reflection/{id}/summary` / `/scorecards`
> / `/xray/{judge}/{run_ref}`).
> **Review round (R2 hardening, applied)**: an adversarial review of the
> corpus/analysis tier was applied. The passive ingest now honors a
> reserved-base ALLOWLIST (r0 / calibration 1000s / evidence 4000s / reflection
> 5000s ingested; the pre-flight's degraded r2000 probe and the 3000s screen
> bases EXCLUDED); the decision-flip bootstrap resamples k-with-replacement +
> mean (matching the base mean-of-K estimator) and returns `p_flip=None` + a
> reason when a unit has <2 replicates or a candidate has no observations (never
> a fabricated `0.0`); judge self-consistency feeds the detector a POOLED
> disagreement rate (worst-unit kept as a secondary diagnostic);
> `sigma_from_noise_floor()` derives the honest per-unit σ for the power
> analysis; and the bootstrap seed folds the (parent, child) pair.
> **Review round (R3 hardening, applied)**: the adjudication tier was hardened
> too. The idempotent cache is now STALENESS-AWARE — a persisted verdict is a
> HIT only when the adjudicator model, `ADJUDICATOR_PROMPT_VERSION`, the
> requested `k_adj`, AND the currently-available fidelity tier all still match
> (a model swap, prompt bump, replication change, or a preview→verbatim upgrade
> re-adjudicates and overwrites; `from_json` defaults the new fields to
> `0`/`""` so a pre-fix cache can never masquerade as fresh). The user prompt is
> DE-ANCHORED — it no longer leaks the judge's own verdict or claimed severity,
> so the meta-judge decides blind (`ADJUDICATOR_PROMPT_VERSION` bumped to 2,
> which — with the new predicate — invalidates every v1 cache). The identity
> guard re-raises an adjudication-specific actionable error, the single
> parse-retry appends a corrective suffix naming the failure, the verbatim tier
> uses the judge's exact `reasoning_text` (the window only as a fallback), the
> cache writer routes through the fsync'd atomic JSON writer, and zero-variance
> (all-fire / all-silent) judges are skipped from the redundancy/conflict
> cross-correlation.
> **Shipped since (the Instrument lens MVP, R5)**: the console **Instrument
> lens** (`dashboard/static/js/views/instrument.js`, registered in
> `shell.js`/`router.js`/`tree.js`) transliterates the **bill-of-health +
> judge-audit + x-ray** mockups from `reflection-viz-study/` — a reflection
> LANDING (the epoch's reflections as a dataTable), the four-pillar BILL OF
> HEALTH (reliability floor + decision-flip P with an honest "n/a — insufficient
> replication" for a null `p_flip`, discrimination differentiating-entries +
> coverage tallies, validity aggregate-F1 + ambiguous pile, calibration
> margin-to-noise) over the ranked findings (each with its copyable `zicato
> reflect apply <id> <finding_id>` invocation — recommend-only, the CLI is the
> apply path), the per-judge JUDGE AUDIT scorecards (the 2×2 confusion matrix +
> P/R/F1/FPR + severity accuracy + the honestly-labelled disagreement rate AND
> self-consistency κ + redundancy/conflict chips, untested judges greyed "never
> fired"), and the adjudication X-RAY (the fidelity-labelled transcript with the
> `evidence_span` highlighted on a text match, the judge verdict vs the
> meta-judge adjudication + model + prompt version + self-agreement, an honest
> "transcript unavailable" when the capture was not retained). The view is
> SERVER-AUTHORITATIVE (it renders the reader payloads and derives no rate / κ /
> verdict; the arc gauge + top-line pillar VERDICT of the mockup are deferred —
> the reader carries no pillar score) and digest-gated (a completed reflection is
> immutable ⇒ fetch-once, a no-op repaint rebuilds zero DOM). The tree grows an
> "Instrument" node under an epoch only when it has reflections. The remaining
> FIVE viz-study mockups (noise-cloud / coherence / waterfall / corpus-grid /
> the compact status ribbon) stay the deferred component spec.
> **Endpoint-gated** (needs operator go-ahead + a live endpoint): live
> meta-judge adjudication, run-twice decision-flip validation, the "quick
> judge audit" preflight extension, the builder Validate panel, and the full
> Instrument component set.

## Context

zicato's evolve loop treats `board + scoring + judges + gate` as a trusted oracle
that ranks candidates and decides promotions. But that stack is an **instrument**,
and every promotion is a **measurement**. If the instrument is noisy, invalid, or
insensitive, the loop optimizes against a broken signal — Goodhart's law one level
above the overfitting program. [`OVERFITTING.md`](OVERFITTING.md) defends against
the **proposer** gaming the board; **board reflection defends the board itself.**
It is Measurement System Analysis (MSA) for the evaluation contract.

The organizing fact: **reliability you can measure with no ground truth; validity
you cannot.** "Is the instrument *consistent*?" you answer by repetition. "Is the
instrument *correct*?" you can only answer against references whose true ordering
you already know.

### The epistemic stance (decided): validity by adjudicated observation

Rather than manufacture ground truth (synthetic controls or hand-labeled golden
sets — both are themselves error-prone instruments needing their own validation),
reflection **runs the board for real and analyzes the actual observed behavior.**
Ground truth is relocated to *the observed run, interpreted after the fact* by an
**independent adjudicator** (a stronger/different-model meta-judge, surfaced for
operator confirmation), not authored before it. The candidate *spread* needed for
discrimination comes from the **real lineage** the loop already produces.

Consequence to state plainly: pure observation detects *inconsistency*; only
adjudication assigns *direction* of error. So the spine of the design is a
**meta-evaluation layer** — an independent reader of the actual transcript that
decides whether each evaluator got it right.

Operating decisions for this design:
- **Active** — reflection may spend LLM budget to run the board (gated by the
  live-run rule: never without operator go-ahead).
- **Diagnose + recommend** — reflection never auto-edits the contract. It emits
  findings with proposed edits; the operator applies them (which rolls the epoch).
- Reflection runs **outside** the contract — it is measurement, not evolution — so
  running it never rolls the epoch; only acting on its recommendations does.

## What reflection measures — four pillars

**Pillar 1 — Reliability** (repeated runs only, no adjudication).
- *Noise floor* from self-replication: run a fixed candidate K times; the
  run-to-run SD of the scalar is the measurement noise. Basis for `promote_margin`.
  **SHIPPED** (`tournament/calibration.py`, consumed by the preflight and
  `detect_margin_below_noise_floor`).
- *Variance decomposition* (a Gauge R&R): split total variance into candidate
  (signal) / run-to-run (noise) / judge stochasticity.
- *Judge self-consistency*: run a judge K times on the **same** transcript →
  self-agreement. **SHIPPED** as test–retest
  (`judge_runtime/reliability.py`) — honestly labeled: the shipped metric is
  a **pairwise disagreement rate** over the k re-judgements, NOT a
  chance-corrected κ. Fleiss κ lands beside (never replacing) the
  disagreement rate with the adjudication scorecards.
- *Decision-level reliability* (the headline number): **decision-flip
  probability by seeded bootstrap** over per-unit replicate scalars pushed
  through the pure gate decision. The original run-the-whole-tournament-twice
  form is demoted to endpoint-gated *validation* of the bootstrap — it spends
  a full tournament of real budget to confirm what the bootstrap resamples
  for free.

**Pillar 2 — Discrimination / power** (a spread of real candidates).
- *Differentiation per entry*: across candidates, does the entry's score move? A
  flat entry is information-free (generalizes `detect_non_differentiating_entry`).
- *Information geometry*: the entry×candidate score matrix → correlation/PCA;
  redundant entries cluster (prune for cost), orthogonal entries each add a
  discrimination dimension.
- *Power analysis*: from noise floor σ and board size n, the **minimum detectable
  Δscalar** at chosen confidence → replicate count, board size, structure choice.
- *Coverage*: which drift-kinds/capabilities the runs **actually exercised** vs
  what the judges and loss watch for; a judge guarding a never-triggered kind is
  **untested**, not validated.

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
  overfits exactly as the proposer overfits the board; it needs its own
  reference-set train/validate split and is out of scope for the MVP.

## The protocol (sound experiment design)

1. **Pre-register the run plan.** Mirror the mandatory-hypothesis discipline:
   before running, write `plan.json` — entries, replicate count K, candidate set
   (champion + a lineage slice), adjudicator model, checks to run. `--pre-register`
   writes it and stops for review before spending budget. Prevents p-hacking the
   loss to whatever the run happened to show.
2. **Execute and fully capture.** Run the board with replication, reusing the
   tournament runner, capturing *everything*: full transcripts, the event stream,
   every drift event, every judge firing/abstention (with severity), per-entry loss
   decomposition, and the emulator's per-turn audit spans. Output = a frozen,
   version-pinned **observation corpus**.
3. **Adjudicate** (the heart). The independent meta-judge reads the captured
   behavior and produces the judge audit, the coherence divergences, and the loss
   decomposition. Independence is load-bearing: the adjudicator must not be a
   judge's own model — reuse the anti-collusion guard as the principle.
4. **Reliability & coverage** (no adjudication): noise floor, decision-flip rate,
   judge self-consistency, per-entry differentiation, exercised-kind coverage.
5. **Diagnose + recommend.** Emit a reflection report (analyzer-style) of ranked,
   evidence-linked findings — each with the offending transcript spans and a
   **proposed contract edit**. The operator applies it (rolls the epoch).

### Scientific guardrails

- **Adjudicator independence & trust** — different/stronger model than any judge
  under test; every finding is **transcript-span-grounded** so the operator
  verifies in seconds rather than trusting a verdict; replicate the adjudicator
  itself to measure *its* reliability. If the meta-judge is the weak link, validity
  has just moved up a level — these mitigations are non-negotiable.
- **Replication** for the noise floor; **pre-registration** of the plan; a
  **frozen corpus** so re-analysis has a stable yardstick.
- **Separate fitting from evaluation** — any calibration fit (margin, weights) uses
  a reference subset held out from the evolve board's own train/holdout; the
  meta-level of the overfitting program.

## The capture gap — and the persisted run artifacts that close it

The protocol's step 2 says "fully capture". Code-verified reality (the hard
blocker for pillar 3): **verbatim transcripts were never persisted.**

- **`RunResult` died with the run.** The worker wrote
  `RunResult{final_output, transcript}` into a temp result file the parent
  read back and then **unlinked** in its cleanup `finally`
  (`tournament/runner.py`, the temp args/result unlink at ~:825-828). The
  user-facing conversation the judges graded survived nowhere.
- **Judge I/O was never retained.** `_InlineCriterionJudge.evaluate`
  (`judge_runtime/builder.py`) builds its prompt from `ctx.reasoning_text`,
  gets a raw LLM response, and parses it down to a `JudgeVerdict`; only the
  verdict (as a `JudgementEmitted` event with a one-line `detail`) survives.
  The judge's exact input bytes and raw response — precisely what an
  adjudicator must re-read — were dropped on the floor.
- **`events.jsonl` is previews-only.** The dashboard transcript
  reconstruction (`dashboard/transcript.py:424-504`) maps
  `input_preview` / `output_preview` / `summary` fields — truncated
  summaries, not verbatim text.

The capture fix (phase R1) persists two zicato-owned artifacts into the run
directory, both **always-on with an opt-out** (`RuntimeConfig`
`persist_run_results` / `persist_judge_io`, additive runtime knobs, never
contract-hashed — flipping them never rolls the epoch), both written
**best-effort** (a capture failure never re-scores or aborts a run) and
**atomically** (tmp + fsync + rename, the durability doctrine):

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
`evaluate` call, emitted through a tiny `io_sink` protocol
(`judge_runtime/io_capture.py`) threaded into `_InlineCriterionJudge`.
Deliberately NOT a new `events.jsonl` frame — goldfive's proto taxonomy is
pinned by three parsers:

```
{ format_version: 1, judge_name, ts, call_index,
  input: { reasoning_text, reasoning_sha256, transcript_window: [...], clipped },
  raw_response,
  verdict: { drift_emitted, kind, severity, detail } }
```

Text fields clip at 65536 chars; `reasoning_sha256` is the sha256 of the
**unclipped** input, so adjudication can verify it is reading the exact
bytes the judge read.

**The fidelity ladder.** Every reflection observation is stamped with the
fidelity of its source, and tiers are aggregated separately — never silently
mixed; a verbatim-tier finding outranks a preview-tier one:

| Tier | Source | What it gives you |
|---|---|---|
| `verbatim` | `judge_io.jsonl` | the judge's exact input bytes + raw response |
| `result` | `result.json` | the full user-facing transcript + final output |
| `preview` | `events.jsonl` previews | truncated summaries (historical runs only) |

A degraded preview-fidelity tier over historical (pre-capture) runs is
possible if labeled honestly; it can rank suspects but not ground verdicts.

**Collusion analysis.** `RunResult`'s docstring exclusion ("internal agent
reasoning … intentionally not exposed here so the emulator and the judge
cannot trivially collude with the inner harness") guards the **during-run**
channel: what a live emulator/judge can see of the run it is inside.
Post-run persistence into the run directory does not re-open that channel —
the file is written after the run's judgements are already settled, the
worker process then exits, and no later run's judge/emulator context ever
reads a prior run's `result.json` / `judge_io.jsonl`. The reader is the
offline reflection engine (and the operator), on the other side of the
process boundary.

## Data model — the observation corpus

Stored under the validated contract: `epochs/{epoch_id}/reflections/{reflection_id}/`
for a sealed epoch, or a builder scratch area for a draft contract. Canonical
files, derived index rows (the index is a projection, per AGENTS.md rule 4).

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

**Reserved replicate base.** Active-corpus runs (replicate j of a
(candidate, entry) unit) execute at `REFLECTION_REPLICATE_BASE + j` =
`5000 + j` — reflection's claimed row in the reserved replicate-base ledger
(dev-guide ch. 04 §8: `0` tournament, `1000` calibration, `2000` preflight,
`3000` screen, `4000` evidence gate, **`5000` reflection**). Same rules as
every other owner: stamp AND key with the index, cache-idempotent (a re-run
of the same frozen plan re-reads persisted draws), and the canonical r0
slots are never touched. The constant lands in `reflection/corpus.py` with
the active scheduler.

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

Grounded in the **adjudicated transcript**, not in any pre-authored label:

| Observed \ Adjudicated | transcript exhibits the failure | transcript clean |
|---|---|---|
| judge **fired**  | **TP** | **FP** (false fire) |
| judge **silent** | **FN** (missed fire) | **TN** |

- `precision = TP/(TP+FP)`, `recall = TP/(TP+FN)`, `FPR = FP/(FP+TN)`, `F1`.
- **Ambiguous** decisions (adjudicator + operator cannot decide) are *excluded from
  the rates and counted separately* — an ambiguous pile is itself a finding: the
  judge's criterion is underspecified.
- **Severity correctness** is tracked apart from fire/silence: a judge that fires at
  `warning` where the transcript warrants `critical` is a *severity* defect, not a
  detection defect — it still mis-weights the loss.
- **Self-consistency**: over K replicates of the same transcript, BOTH metrics,
  honestly named — the shipped test–retest **pairwise disagreement rate**
  (`judge_runtime/reliability.py`; NOT chance-corrected) and a chance-corrected
  **Fleiss κ** computed beside it in the scorecards. A judge below threshold is
  unreliable regardless of its precision/recall.
- **Cross-judge** matrix: pairwise correlation of firings → `redundant_with`
  (corr≈1, prune candidate) and `conflicts_with` (systematic disagreement).
- **Exercised**: did any run trigger the kind this judge guards? If not, the judge
  is reported **untested** (cannot be validated by this corpus).

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
zicato reflect [--workspace PATH]
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

zicato reflect report <reflection_id>            # render a stored reflection report
zicato reflect apply  <reflection_id> <finding_id>  # fork a BUILDER DRAFT + apply the finding's
                                                 # proposed_op; the operator seals via the
                                                 # builder (sealing rolls the epoch)
```

The **builder's "validate" action calls `reflect`** on the draft contract: you are
authoring/sealing a contract, so validating the instrument belongs there. Builder
owns the decision UX; the `reflection/` engine owns the analysis. (The builder
Validate *panel* is DEFERRED to the endpoint-gated backlog — the builder's
preflight op already covers cheap authoring-time validation, and `reflect
apply`'s draft-fork gives the finding→builder path.)

## evolve preflight check list — SHIPPED (Board-reflection v1)

This tier is live: `zicato/epoch/preflight.py` (self-labeled
"Board-reflection v1") runs default-on at evolve start, gated by
`RuntimeConfig.preflight_gate` — `"warn"` (the default), `"refuse"`
(hard-stop via `PreflightRefusedError`), or `"off"`. Its verdict persists
onto the epoch record and surfaces through the loop-health channel.

- **Noise floor** — K champion A/A draws; scalar SD
  (`tournament/calibration.py`, cache-idempotent with `zicato board audit`).
- **Achievable signal** — champion vs a deliberately-degraded ephemeral copy
  of itself; `refuse` when the signal does not clear the noise floor,
  `saturated` warn when every probe scores identically.
- **Margin sanity** — `detect_margin_below_noise_floor` (promoting on noise).
- **Dead judge / dead entry / degenerate scoring / flat drift** — the
  loop-health detectors (`detect_dead_judge`,
  `detect_non_differentiating_entry`, `detect_degenerate_scoring`,
  `detect_flat_drift_signal`), part of the 14-detector set in
  `health/diagnostics.py`.
- **Noisy judge** — test–retest disagreement above threshold
  (`detect_noisy_judge`, fed by `judge_runtime/reliability.py`).

Preflight is loop-health promoted from passive observation to an active
pre-run gate. The **quick judge audit** (a small meta-judge pass over the
champion's latest runs, surfacing obvious false-/missed-fires with spans) is
STRUCK from this list to the **endpoint-gated backlog**: it spends real
adjudicator budget and needs a live endpoint plus operator go-ahead.

## Continuous passive reflection

The free tier: re-analyze the runs `evolve` already produces, offline, across
rounds — accumulate the entry×candidate matrix, recompute discrimination /
redundancy / judge-drift, and surface in the dashboard + as enriched loop-health
findings. No new LLM calls (no adjudication); catches the instrument *drifting* as
the board ages.

## Implementation — one engine, three surfaces

The dedicated mode, the preflight, and the continuous tier are the **same analysis
at three cadences and cost points**, so: one engine, three surfaces.

- **`reflection/` engine** — pure analyzers (the four pillars over the observation
  corpus) + an active scheduler that reuses the tournament runner to *produce* the
  corpus and an independent meta-judge to *adjudicate* it.
- **`zicato reflect`** — the deep active validation + report; also the builder's
  "validate" action.
- **evolve preflight** — the cheap subset (above), default-on.
- **continuous passive** — offline re-analysis feeding the dashboard + loop-health.

**Reuse map (evolution, not greenfield):**
- `tournament/runner.py` (+ `_tournament_worker.py`) — execute the board.
- `dashboard/transcript.py`, `RunResult`, `events.jsonl`, `emulator/audit.py`
  spans — observe.
- `judge_runtime/` + the two-callable anti-collusion guard — adjudicate
  *independently*.
- `synthetic/` — optional adversarial/clean entries as *additional* exercised
  behavior (not the primary ground truth).
- `index/` — store the corpus + findings as derived rows; `analyzer/` — render the
  reflection report at the grain of the epoch report.
- `health/diagnostics.py` — the preflight detectors.
- `builder/` — the "validate" action + applying recommended edits.
- `epoch/contract.py` — applying a recommendation is a contract edit → rolls the
  epoch (so reflection sits at authoring / epoch-boundary time by construction).

## Adopted design verdicts (2026-07 review)

Seven improvement decisions, settled during the implementation program:

1. **Decision-flip by seeded bootstrap.** The headline reliability number is
   computed by bootstrap resampling per-unit replicate scalars through the
   pure gate decision — free, deterministic under a seed. The literal
   run-the-tournament-twice measurement is demoted to endpoint-gated
   *validation* of the bootstrap.
2. **κ AND disagreement rate, both, honestly named.** The shipped test–retest
   pairwise disagreement rate keeps its name; Fleiss κ is computed beside it
   in the scorecards. Neither masquerades as the other.
3. **Consume the persisted noise floor.** Reflection reads the noise floor /
   preflight verdicts already persisted on the epoch record rather than
   re-measuring (a `--fresh` flag re-measures deliberately). No silent
   double-spend of calibration budget.
4. **`detect_noisy_judge` wired in unchanged.** Reflection's judge
   self-consistency measurements feed the existing health detector — one
   threshold, one finding shape, no parallel taxonomy.
5. **Entry×candidate matrix as a query reader.** The discrimination matrix is
   a reflection-independent `zicato/query` reader over persisted losses, so
   the continuous passive tier (and the dashboard) get it without a
   reflection run.
6. **Findings carry executable builder-op payloads.** Every finding's
   `proposed_op` names a REAL builder op and is validated against that op's
   signature at emit time — the margin finding is exactly a
   `set_gate {promote_margin: ...}` payload; judge pruning is exactly
   `set_weights {per_judge_weights: {judge: 0.0}}`. No prose-only
   recommendations.
7. **RoundLog judge events: REJECTED.** No judge granularity is added to the
   RoundLog — judge I/O belongs in the zicato-owned `judge_io.jsonl` sidecar
   (see the capture section), not in a new frame taxonomy the log's three
   parsers would all have to learn.

**Apply path**: `zicato reflect apply <finding_id>` forks a **builder draft**
and applies the finding's `proposed_op` to it; the operator reviews and seals
through the builder. Reflection never edits the sealed contract directly —
the recommend-only invariant holds end-to-end.

## UI — the Instrument lens

**Where it lives.** The console (Variant T) is **candidate-centric**: it is
organized around the *time axis of evolution* — lineage over rounds, decisions,
the question *"what did the loop decide?"*. Reflection is **instrument-centric**:
a cross-section through the **measurement instrument** at one contract — the
same data viewed perpendicular. **Turn the camera 90°.** So it is a **dedicated
top-level "Instrument" lens** in the console — a peer to the epoch / tournament /
lineage views, **not nested inside them** and **not a standalone app**.
Concretely (the console as it exists today, not the tabbed sketch this doc
originally assumed): a **hash-router view** plus a **tree-sidebar entry** —
the standard four-file registration (`views/instrument.js`, the `RENDERERS`
map, the router's `VIEWS` + parse/href/crumbs, and the tree entry). It
reuses the console idiom wholesale: the transcript reader, the board heatmap,
per-judge trends, the [theme system](CONSOLE-DESIGN-LANGUAGE.md), and the
digest-gated render discipline (completed reflections are immutable, so the
lens is a fetch-once render with a pinned digest no-op). The **same
components** embed in the [builder](TOURNAMENT-BUILDER.md) as its
**"Validate" step** (endpoint-gated). So the UI inherits the engine's
"one engine, three surfaces" shape:

- **console Instrument lens** — monitoring a *sealed* contract: read-only
  recommendations, the deep `reflect` reports, and the continuous passive tier
  surfaced inline.
- **builder Validate panel** — *authoring-time*: the operator runs `reflect` on
  the **draft** and applies a recommended edit before sealing.

The console answers *"what did the loop decide?"*; the Instrument lens adds
*"can I trust **how** it decided?"*

**Components.** The visual spec EXISTS: eight theme-adaptive HTML mockups in
[`docs/design/reflection-viz-study/`](reflection-viz-study/) — do not
re-derive the design. The **MVP subset is bill-of-health + judge-audit +
transcript x-ray**; the other five mockups (coherence scatter, noise-cloud,
waterfall, corpus grid, and the live process hero) are the deferred-component
spec on the endpoint-gated backlog. The emotional core is the **transcript
x-ray** — clicking any statistic lands you in the *actual conversation the
judge graded*, the disagreement lit up. Everything else is the map that leads
there.

- **Bill of health (landing)** — a top-line verdict over **the four pillars** as
  a **gauge quadrant**: *Reliability* (noise floor + P(gate decision flips)),
  *Discrimination* (% entries that differentiate + coverage), *Validity*
  (aggregate judge F1 + # coherence divergences), *Calibration* (margin-to-noise
  + loss-term balance). The golden-spiral mark doubles as a convergence motif.
- **Transcript x-ray (centerpiece)** — split view: the conversation with the
  judge's **claimed span** highlighted, beside the independent meta-judge's
  adjudication rationale and a **confirm / deny** toggle. Fixed colour grammar:
  **TP** a quiet-green seam, **FP** a red mark where nothing happened, **FN** the
  highlighted span the judge slept through.
- **Judge audit** — per judge: the **2×2 confusion matrix** (TP/FP/FN/TN) with
  `precision` / `recall` / `f1` / `fpr`, a **self-consistency κ** gauge, a strip
  of **evidence chips** for the FP/FN piles (each clicks into the x-ray), and the
  **cross-judge redundancy / conflict graph**. Untested judges are greyed
  *"never fired."*
- **Coherence scatter** — runs plotted by **|scalar move| vs adjudicated
  severity**; the diagonal is trustworthy, the **off-diagonal outliers glow**
  (penalized-but-clean, failed-but-flat-loss) and click into the x-ray.
- **Reliability noise-cloud** — a **violin** of the replicated scalars with
  `promote_margin` drawn across it: a margin **inside** the cloud reads as
  *"promoting on noise."* The **decision-flip count** sits alongside.
- **Loss decomposition** — a **waterfall** of each term's contribution to the
  scalar (dead terms greyed, dominating terms oversized) with a reweight
  **preview** — preview only; the fit stays a non-goal.
- **Discrimination & coverage** — the **board heatmap** idiom (flat rows
  flagged) beside a **coverage map** of the exercised drift-kinds vs what the
  judges watch.
- **Live process** — an instrument-themed live hero: the **corpus grid**
  (entry × candidate × replicate cells) filling in, then an **adjudication
  phase** — the *same* digest-gated render discipline as the tournament live
  hero.

**Outcome → action loop.** Findings are **first-class**: evidence-linked,
ranked, each carrying its **proposed contract edit** and a **"send to builder"**
affordance. Console = read-only recommendations *carried* to the builder;
builder Validate panel = **apply to the draft inline**. The human stays at the
contract boundary — the UI never auto-edits, exactly as the engine never does.

**Design-language fit.** Leans the console's **"Technical" register** with
calibration-bench restraint: confusion matrices, ROC sweeps, and the noise-cloud
rendered with the console's quiet precision, **not clutter**. The
spiral-as-convergence motif, the theme system, and the digest-gated / SSE
machinery carry over unchanged. **Reflection is a new grammar on the existing
design language, not a new language.**

## The proposer envelope — reflection output is operator-only

Reflection findings, scorecards, and adjudication rationales are
**operator-facing only**: nothing reflection produces is placed in the
proposer's prompt envelope. The proposer already gets a carefully bounded
view of outcome statistics through `proposer/prompts.py`'s banding, whose
numeric inputs pass through `sanitize_operator_marginals`
(`analyzer/outcome_marginals.py`) and whose exposure is gated by
`OverfittingConfig.restrict_proposer_visibility`. If a future decision ever
crosses reflection signal to the proposer, it MUST reuse exactly that
machinery — banded, sanitized, visibility-gated — never raw findings: a
proposer that can read the judge audit can optimize against the judges'
measured blind spots, which is the overfitting program's threat model one
level up.

## MVP sequencing

1. **Judge audit on champion runs** — pure "debug judges," no labels, just real
   runs + an independent meta-judge. Highest match to the stated goal.
2. **Noise floor → `promote_margin`** calibration — cheap, high-leverage.
3. **Score↔behavior coherence + loss-term decomposition** — the "optimize loss"
   diagnostics.
4. **Discrimination + coverage over lineage** — board pruning / gap-filling.

## Non-goals (for now)

- **Automatic loss-weight fitting / contract edits.** Reflection diagnoses and
  recommends; the operator applies. Auto-fitting the instrument is meta-overfitting
  and silently rolls the epoch.
- **External validity.** Whether board performance predicts *production* quality
  needs deployment data; reflection surfaces *coverage* (does the board span the
  failure modes you care about) but does not claim external validity.
- **Replacing the gate or the gauntlet default.** Reflection tunes the inputs to
  the gate; the gate's decision rules are unchanged.
