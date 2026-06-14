# zicato — board reflection (evaluation-contract validation)

> **Status: proposal / design note — not implemented.** A `reflect` mode that runs
> boards and analyzes the *observed behavior* to validate and tune the evaluation
> contract itself — debug judges, calibrate the loss function and gate margin, and
> optimize board composition and tournament design. Companion to
> [`OVERFITTING.md`](OVERFITTING.md), [`SCORING.md`](SCORING.md),
> [`LOOP-HEALTH.md`](LOOP-HEALTH.md), [`TOURNAMENT-BUILDER.md`](TOURNAMENT-BUILDER.md),
> and the audit-driven [`FUNCTIONALITY-RECOMMENDATIONS.md`](FUNCTIONALITY-RECOMMENDATIONS.md).

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
- *Variance decomposition* (a Gauge R&R): split total variance into candidate
  (signal) / run-to-run (noise) / judge stochasticity.
- *Judge self-consistency*: run a judge K times on the **same** transcript →
  self-agreement (κ).
- *Decision-level reliability* (the headline number): run the **whole tournament**
  twice and measure **P(the gate decision flips)**. Integrates noise and margin.

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
- **Self-consistency** κ: over K replicates of the same transcript, chance-corrected
  agreement of the judge's own decisions. A judge below a κ threshold is unreliable
  regardless of its precision/recall.
- **Cross-judge** matrix: pairwise correlation of firings → `redundant_with`
  (corr≈1, prune candidate) and `conflicts_with` (systematic disagreement).
- **Exercised**: did any run trigger the kind this judge guards? If not, the judge
  is reported **untested** (cannot be validated by this corpus).

**JudgeScorecard** (`scorecards.json`):
```
{ judge_name, n_decisions, tp, fp, fn, tn, ambiguous,
  precision, recall, f1, fpr, severity_accuracy, self_consistency_kappa,
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

zicato reflect report <reflection_id>      # render a stored reflection report
zicato reflect apply  <finding_id>         # apply a recommended contract edit (rolls the epoch)
```

The **builder's "validate" action calls `reflect`** on the draft contract: you are
authoring/sealing a contract, so validating the instrument belongs there. Builder
owns the decision UX; the `reflection/` engine owns the analysis.

## evolve preflight check list

A cheap, default-on subset run before an `evolve` spends real budget (warn by
default; `--strict` to block). Surfaced through the existing loop-health channel.

- **Noise floor** — a small-K champion self-replication; estimate scalar SD.
- **Margin sanity** — block/warn if `promote_margin < noise_SD` (promoting on noise).
- **Dead judge** — a judge that never fired across recent runs / is never exercised.
- **Dead entry** — non-differentiating across the recent candidate spread.
- **Degenerate scoring** — the loss collapses (no spread) over recent runs.
- **Quick judge audit** — a small meta-judge pass over the champion's latest runs;
  surface any obvious false-fire / missed-fire with the span.

These reuse the loop-health detectors (`detect_dead_judge`,
`detect_non_differentiating_entry`, `detect_degenerate_scoring`,
`detect_flat_drift_signal`) — preflight is loop-health promoted from passive
observation to an active pre-run gate.

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

## UI — the Instrument lens

**Where it lives.** The console (Variant T) is **candidate-centric**: it is
organized around the *time axis of evolution* — lineage over rounds, decisions,
the question *"what did the loop decide?"*. Reflection is **instrument-centric**:
a cross-section through the **measurement instrument** at one contract — the
same data viewed perpendicular. **Turn the camera 90°.** So it is a **dedicated
top-level "Instrument" lens** in the console — a peer to the epoch / tournament /
lineage views, **not nested inside them** and **not a standalone app**. It
reuses the console idiom wholesale: the transcript reader, the board heatmap,
per-judge trends, the [theme system](CONSOLE-DESIGN-LANGUAGE.md), and the
digest-gated / SSE render discipline. The **same components** embed in the
[builder](TOURNAMENT-BUILDER.md) as its **"Validate" step**. So the UI inherits
the engine's "one engine, three surfaces" shape:

- **console Instrument lens** — monitoring a *sealed* contract: read-only
  recommendations, the deep `reflect` reports, and the continuous passive tier
  surfaced inline.
- **builder Validate panel** — *authoring-time*: the operator runs `reflect` on
  the **draft** and applies a recommended edit before sealing.

The console answers *"what did the loop decide?"*; the Instrument lens adds
*"can I trust **how** it decided?"*

**Components.** The emotional core is the **transcript x-ray** — clicking any
statistic lands you in the *actual conversation the judge graded*, the
disagreement lit up. Everything else is the map that leads there.

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
