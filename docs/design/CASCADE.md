# Evaluation cascade — staged screen → rung → full → holdout under one budget

> **Status.** A design note carrying measurements; **the build decision
> is pending**. zicato ships **four partial cascade forms** — the
> pre-tournament candidate screen, racing's board-slice rungs, the
> full-board promote gate, and the holdout confirmation the Ladder
> mediates — each independently configured, each with its own reserved
> replicate base. This note asks whether to **unify them as one declared
> `screen → rung → full → holdout` pipeline with per-stage budgets**, and
> it sets out why that unification has not been built: **stage thresholds
> interact with the gate's statistics.** Each stage's cut is a selection
> event, and selection compounds bias in what survives to the gate — a
> winner's curse incurred once per stage. A cascade that is not
> noise-aware at every rung would deliver a final survivor whose apparent
> margin over the champion is inflated by the sum of the upstream
> selection biases, and would promote noise that the single-stage gate
> catches. No source, config schema, or test in the tree changes because
> of this note. A build decision is gated by the measured **operating
> characteristics** of §4 rather than by the argument above, and those
> measurements now exist: the §4 harness is built (`tools/cascade_oc.py`
> and `tests/test_cascade_oc_harness.py`) and its first full run is
> reported in **§5**. The decision itself remains the operator's; this
> note does not make it.

This note builds on five design documents and one dev-guide chapter:

- [`SCORING.md`](SCORING.md) — the scalar loss and the three-rule promote
  gate the cascade terminates in.
- [`SELECTION.md`](SELECTION.md) / [`TOURNAMENT-STRUCTURES.md`](TOURNAMENT-STRUCTURES.md)
  — the gauntlet gate and the five schedulers; racing (`§3.5`) is the
  shipped board-slice rung mechanism.
- [`OVERFITTING.md`](OVERFITTING.md) — the train/holdout split, the
  budgeted mechanism that limits how often the holdout may be queried (the
  Ladder), and the reused-holdout hazard the terminal stage inherits.
- [`SELECTION-THEORY.md`](SELECTION-THEORY.md) — the winner's-curse /
  optimizer's-curse treatment and the **replicate-first, resolve-second**
  operating rule this note lifts to a *per-stage* discipline.
- dev-guide `04-evaluation-statistics.md` — the noise doctrine, the
  same-versus-same (A/A) noise floor, the evidence gate, the placebo arm,
  the reserved replicate-base ledger, and the power-harness methodology
  every claim below rests on.

---

## 1. The four partial cascades zicato already runs

A cascade is any pipeline that spends **cheap, noisy** evaluation early to
discard obvious losers, and **expensive, precise** evaluation late on the
few survivors. zicato already runs four such stages — but wired
independently, tuned independently, and never reasoned about as *one*
compounding-selection pipeline.

| Stage (order) | Shipped as | Cut rule | Board slice | Replicate base | Selectivity |
|---|---|---|---|---|---|
| **Screen** (upstream, in-propose-step) | `src/zicato/epoch/screen.py` | **veto-first, confirm-before-veto** — disqualify a candidate that flips a champion-passing train entry twice, or blows its wall-clock budget; never ranks | 1–2 rotating **train** entries | `3000` (`+1` confirm at `3001`) | high-recall filter: cuts only categorical breakage |
| **Rung** (racing, downstream) | `selection/strategies/racing.py` (`TOURNAMENT-STRUCTURES.md §3.5`) | **rank-and-halve** — eliminate the worst `1 − 1/eta` by scalar per rung; escalating slice = escalating sample; gate applied only at the **final** rung | rung-0 `board_fraction`/`rung0_board_size`, escalating to full | `0` (real duel slots) | best-arm identification, margin-blind cut |
| **Full** (the gate) | `tournament/gate.py::evaluate_gate` | the three-rule ladder — scalar margin, pass-rate monotonicity, namespace monotonicity | full board × `replicates` × both sides | `0` | the promotion decision itself |
| **Holdout** (terminal confirm) | `tournament/ladder.py` + gate rule 4 | **Ladder-mediated confirmation** — a train-win must hold on a slice the proposer never saw; released only when the train improvement clears the threshold, budgeted per epoch | the `holdout`-tagged slice | (holdout entries, canonical slots) | anti-memorization guard, confirmation-only |

Two of these already **name each other as complementary**. The screen's
own module docstring states the relationship: the screen runs *upstream*
inside one propose-step, before a child is minted into lineage, while
racing's rung-0 halving runs *downstream* on applied lineage children.
The docstring's own summary is that "the two compose: the screen keeps a
broken candidate out of the field, racing prunes the mediocre field
members." The full gate and the holdout compose the same way at the other
end: the gate decides on the train slice, and the holdout confirms on a
slice the optimizer never queried.

**The gap this note names.** These four are configured through four
unrelated surfaces: the `proposer_quality` best-of-N params, the racing
`tournament_structure` params, `ScoringWeights`, and the `overfitting`
plus `ladder` blocks. They draw from three different reserved bases.
Above all, **no single object reasons about the compounding selection
bias across all four.** The candidate that reaches the holdout has
survived up to three prior selective cuts. Its train scalar is optimistically biased by
every one of them. Whether the terminal gate's evidence requirement is
still sufficient after that compounding is *not something any shipped
mechanism measures.* That is the deferred question.

---

## 2. The unification: one declared pipeline, one budget ledger

The proposal is a single ordered **stage list** that supersedes the four
independent wirings:

```
propose-step candidates
      │
      ▼  STAGE 0 — screen   (veto-first; 1–2 train entries; base 3000)
      │        cut: categorical breakage only (confirm-before-veto)
      ▼  STAGE 1..k — rungs  (rank-and-halve; escalating train slice)
      │        cut: worst 1−1/eta by scalar per rung
      ▼  STAGE k+1 — full   (the three-rule gate; full train board × replicates)
      │        cut: promote-margin + monotonicity
      ▼  STAGE k+2 — holdout (Ladder-mediated; the holdout slice)
               cut: confirmation-only; can flip a train-win to reject, never promote
```

The unification buys three things the independent wirings cannot:

1. **One budget ledger.** Today each stage's cost is set in isolation
   (screen panel size, racing `eta`/`board_fraction`, the gate's
   `replicates`, the Ladder `budget`). A cascade block lets an operator
   state a *total* per-promotion evaluation budget and have it allocated
   across stages by the successive-halving discipline racing already
   embodies: cheap-and-many early, expensive-and-few late. Four
   independent numbers can instead sum silently to more evaluation than
   the operator meant to spend.
2. **One reserved-base allocation.** The replicate-base ledger
   (`04-evaluation-statistics.md §8`) already partitions the replicate
   index space per out-of-tournament evaluator (calibration `1000`,
   preflight `2000`, screen `3000`, evidence `4000`, reflection `5000`).
   A declared cascade makes the *ordering* of stages explicit, so the
   "each stage draws fresh, never replays an upstream stage's cached
   sample" invariant (the whole reason bases exist) is enforced by
   construction rather than by four modules independently remembering to
   stamp their own base.
3. **One place to reason about compounding selection** — §3. This is the
   load-bearing reason to unify at all. A pipeline object can compute how
   selective each upstream stage was and *raise the terminal gate's
   evidence requirement to match*. Four independent stages structurally
   cannot.

The protected-incumbent invariant is **unchanged**, and a declared
pipeline makes it easier to enforce. Every stage before the terminal gate
can only *narrow the field* (a screen veto, a rung elimination); only the
full gate and the holdout can promote, and both keep their shipped rules
verbatim. A cascade mis-cut at an early rung costs at worst a wasted
confirmation and never an unsafe promotion, which is what the
resolver-and-gate split of `SELECTION-THEORY.md §1` already guarantees.

---

## 3. The statistical core — what staged selection costs

The reasoning in this section is why the unification has not been built.
A cascade is a *sequence of statistical tests on noisy draws*, and the
noise doctrine (`04-evaluation-statistics.md §3`) demands that every one
of them state a noise model and carry a measured false-cut rate. Two
coupled facts make a naive cascade unsound (§3.1 and §3.2); §3.3 states
the correction the terminal stages must apply.

### 3.1 The per-stage false-cut rate scales with the slice-size floor

The A/A noise floor is a *function of the board slice a stage evaluates
on* rather than a single number. The measured full-board floor is
`≈ 1.6·sqrt(σ(1−σ))` for the σ-harness structure (fact #8,
`04-evaluation-statistics.md §3.1`; ≈0.663 at σ=0.22). But a stage's
scalar is a **mean over its slice**, so the standard deviation of that
mean scales as `≈ 1/sqrt(m)` in the slice size `m`. A rung that cuts on a
`board_fraction=0.25` slice measures against a floor roughly **twice** the
full-board floor; the screen's 1–2-entry panel is noisier still.

The consequence, stated as an operating characteristic a build must
measure:

> **The probability that a stage eliminates a candidate that is actually
> better (a false cut) is governed by that stage's *own* slice-size floor
> rather than by the full-board floor.** An early rung cutting by rank on
> a quarter-board slice has a materially higher false-cut rate than the
> terminal gate. Sizing a planted true effect at a fixed multiple of the
> *full-board* floor therefore measures the wrong thing; the effect must
> be sized against **each stage's** floor.

The shipped code already recognizes this hazard. It is why racing's
rung-0 cut is margin-based best-arm identification rather than a gate: a
gate would falsely reject on that noisy slice. It is also why the screen
vetoes and never ranks (`screen.py`). A 2-entry screen ranking close
candidates is "approximately random choice plus winner's curse" (`§3.3`
of the dev-guide's screen doctrine), so the screen may decide only the
one thing detectable at n=1, categorical breakage, and it confirms even
that before acting. A unified cascade must inherit this discipline
*per stage*: **the noisier the stage, the coarser the cut it is permitted
to make.** A high-recall veto early; a margin-aware rank in the middle; a
calibrated margin + confirmation only at the end.

### 3.2 Selection at stage k biases the distribution entering stage k+1

Survivors of stage `k` are, by definition, the candidates that drew
*favorably* at stage `k`. Their stage-`k` scalars are therefore
**optimistically biased** — the classic optimizer's curse
(`SELECTION-THEORY.md §4`), now incurred once per stage. Two regimes:

- **If stage k+1 re-measures on fresh draws** (a new slice, a new
  replicate index), the bias from stage `k` is *reset* — the fresh draw is
  selection-independent of why the candidate survived. This is why the
  reserved-base ledger and the both-sides-fresh rule
  (`04-evaluation-statistics.md §6.2`, §8) exist. An evidence replicate
  that cache-read an upstream stage's sample would "replay one identical
  sample into the fit … shrinking its SE by repetition alone", which is an
  unsound-promotion path and the defect that evidence-gate slot reuse
  actually produced (case 8 in `12-bug-casebook.md`). A cascade's
  soundness depends *entirely* on each stage drawing fresh.
- **If stage k+1 reuses a cached upstream score**, the bias **carries
  forward and compounds.** After `N` selective stages that each reuse
  prior scores, the final survivor's apparent margin over the champion is
  inflated by the *sum* of the per-stage selection biases. A margin
  calibrated to a single full-board A/A floor is then far too permissive.

The escalating-slice structure of racing partly offsets this: each rung
runs a *larger* slice, and if the larger slice is drawn fresh rather than
served as a superset cache-hit of the smaller, the survivor is re-measured
with more signal. But racing caches at base `0` and escalates the *slice*
rather than the *replicate index*, so whether a rung's larger slice is
selection-independent of the smaller rung that fed it is the kind of claim
the doctrine forbids asserting without measurement
(`04-evaluation-statistics.md §3.2`). **A unified cascade must measure
that every stage's draw is selection-independent of every prior stage's
cut, rather than assume it.**

### 3.3 What the terminal gate's evidence requirement must become

A candidate reaching the holdout has been selected up to three times. The
correction the terminal stages must apply, built entirely from machinery
zicato already ships:

1. **The final gate measures on a fresh draw rather than a cached stage score.**
   Already the canonical-r0 + both-sides-fresh rule
   (`04-evaluation-statistics.md §6.2`, §7.3). The cascade must guarantee
   the terminal full-board evaluation is drawn independently of every rung
   that selected the survivor — the reserved-base discipline extended to
   name the whole ordered pipeline.
2. **The evidence gate provides the selection-independent re-measurement.**
   The Bradley–Terry pre-gate (`evidence_gate.py`,
   `04-evaluation-statistics.md §6`) fits over fresh reserved-base-`4000`
   draws and crowns only when the confidence intervals (CIs) *separate*.
   Noise cannot pass that test by selection luck, because separation takes
   roughly 37 duels of an essentially unbroken win streak (fact #2). This is the natural home for the *N-stage correction*: **the more
   selective the upstream cascade, the larger the terminal replicate
   budget must be.** A candidate that survived three cuts should face a
   stiffer confirmation than one that went straight to the gate, because
   its train margin is more inflated. Concretely, the evidence gate's
   `promote_confidence_replicates` should be a **function of upstream
   selectivity** (how many stages, how selective each) rather than a fixed
   constant — the cascade knows the selectivity; the standalone gate does
   not. The division of labour: the *fresh draw* of item 1 is what removes
   the selection bias from the point estimate, while replicate-scaling only
   tightens the confidence interval. Replicate-scaling is a power hedge
   that makes a noise-driven survivor harder to confirm; it does not itself
   correct the bias.
3. **The holdout is the one slice no stage selected on.** The train/holdout
   split (`board/split.py`) already withholds the holdout from proposer
   context, pattern detection, screen panels, and loss summaries
   (`OVERFITTING.md §11`; `04-evaluation-statistics.md §12`). Because *no
   cascade stage is ever eligible to read it*, the holdout confirmation is
   structurally selection-independent of the entire upstream pipeline — it
   is the cascade's de-biasing anchor. The Ladder's reused-holdout budget
   (`§5` of the dev-guide) already governs how many times that anchor can
   be queried before repeated queries stop being independent of it.
4. **The placebo arm becomes the cascade's end-to-end control.** The
   shipped placebo (`evolve/placebo.py`, `04-evaluation-statistics.md §11`)
   fields a semantics-preserving no-op the gate must reject. Under a
   cascade it must be run through **the whole pipeline** — screen, rungs,
   gate, holdout — and still rejected. A no-op that *survives the cascade
   and promotes* is the alarm that the compounding selection has broken the
   decision procedure (the `placebo_promoted` CRITICAL finding, elevated
   from "the gate promoted noise" to "the cascade promoted noise").

The synthesis: zicato already owns every building block the correction
needs — **per-slice A/A floors** (calibration, extended to measure the
floor at each stage's slice size), **the evidence gate** as the
selection-independent re-measurement, **the holdout/Ladder** as the
never-selected-on anchor, and **the placebo** as the whole-pipeline
control. What is missing is the object that *composes* them with the
selectivity accounting — and the measured evidence that the composition is
sound. That evidence is §4.

---

## 4. The operating-characteristic harness — proving a cascade sound before building it

The noise doctrine requires that operating characteristics be measured
rather than asserted (`04-evaluation-statistics.md §3.2`, §13), so no
cascade ships until a known-answer harness demonstrates it. This section
is concrete enough to build the harness from on its own. It extends the
existing decision-procedure power harness
(`tests/test_decision_procedure_power.py`, the second of the two
convergence oracles) rather than introducing a new instrument.

### 4.1 Reuse the seeded-noise substrate

The harness inherits the deterministic convergence example world verbatim
(`examples/zicato_examples/target_0_convergence/harness.py`): `stable_noise_seed`
derives the RNG seed **only** from
`(workspace_seed, generation_id, entry_id, replicate_index)` — no wall
clock, no global RNG (`04-evaluation-statistics.md §13.1`). This is what
makes cascade trials reproducible run for run, and it lets "sides vary
because the generation id is in the seed" serve as the A/A premise. The
planted-effect vocabulary is the existing `DELTA_CASES` (0.5×, 1× and 3×
the **full-board** floor) with one added requirement: effects sized
against **each stage's slice floor** (§4.2). Drive the real machinery, monkeypatch only
`runner._run_single` (`§13.2`).

### 4.2 Experiment A — per-stage false-cut rate vs the slice floor

The foundational measurement (§3.1). For each stage's slice size `m_k`:

1. **Measure the slice-k floor.** Run K seeded A/A draws of the champion
   on the `m_k`-entry slice through the same `_run_board_units_fast`
   calibration path (`calibration.py`), at a reserved base — this is the
   existing `measure_noise_floor` restricted to the slice. Assert the
   floor grows as `m_k` shrinks (roughly `∝ 1/sqrt(m_k)`); a floor that
   *did not* grow on a smaller slice would mean the seeding stopped
   varying, which is the calibration false-zero-floor defect (case 3 in
   `12-bug-casebook.md`).
2. **Plant a true effect at multiples of the slice-k floor** rather than
   the full-board floor. Extend the `sometimes-<pct>-<token>` vocabulary
   (`§13.4`) so a δ can be sized to `{0.5, 1, 3}×` the *slice*'s floor.
3. **Measure P(the better arm is cut at stage k).** Run the stage's real
   cut rule (screen veto / racing rung rank-halve) over the seeded trial
   range; count how often the better arm is eliminated.
4. **Pin the coarse-cut discipline.** Assert that the *veto* stage's
   false-cut rate follows the confirm-before-veto squaring (≈ flip-rate²,
   fact #7). Assert that the *rank* stage's false-cut rate is acceptable
   **only** at effects of 1× its own slice floor or larger. Together these
   document that a stage may not be trusted to resolve effects below its
   slice floor.

### 4.3 Experiment B — end-to-end P(promote | ·), cascade ON vs OFF

The headline decision measurement. On the **identical seeded draws**:

- **Null (the cascade placebo).** Field an identical arm
  (`{"champion": BASE, "challenger": BASE}`) and run it through the *whole*
  pipeline. Measure `P(promote | null)` with the cascade ON and with it OFF
  (today's single-stage full-board contract). The doctrine's fact #4 —
  "the evidence-gated contract's false-promotion rate under the A/A null is
  zero" — sets the requirement: **the cascade must not raise
  `P(promote | null)` above the single-stage contract's rate.** If
  compounding selection leaks a nonzero null-promotion rate, the cascade
  is unsound and does not ship.
- **True effect.** Plant δ at `{0.5, 1, 3}×` the full-board floor and
  measure `P(promote | true-improvement)` ON vs OFF. Pin `power == 1.0` at
  3× (unmissable, fact #6) and monotonicity `small ≤ medium ≤ large`. Pin
  also that the cascade's power at each δ is **≥ the single-stage
  contract's power minus a stated tolerance.** A cascade that saves budget
  by *losing* real improvements at the early rungs is a power regression;
  the harness must quantify how much power the staging costs.
- **Include the failing alternative as documentation** (`§13.5`): the
  naive "run every stage's cut as a gate" rule, run on the same seeded draws
  *and through the same terminal*, so the comparison isolates the rung rule
  rather than the samples or the terminal. §5.3 reports what that
  comparison shows: the rung rule governs power and budget rather than
  soundness.

### 4.4 Experiment C — the budget-savings-vs-power curve

The build-decision artifact. A cascade's *only* justification is that it
reaches a target power at **fewer total board-unit evaluations** than
running the full board on every candidate. A board unit is one
`(generation, entry, replicate)` evaluation, the smallest thing the
tournament pays for. Plot, at a fixed planted δ:

- **x-axis:** total board-unit evaluations spent per promotion (summed
  across all stages, counting each reserved-base draw once).
- **y-axis:** power (`P(promote | true)`) at that δ.

Sweep the cascade's stage allocation (screen panel size, rung `eta` /
`board_fraction`, terminal `replicates`) and overlay the single-stage
contract as a reference point. The cascade earns a build **if and only
if** a configuration exists that reaches the reference power at **materially
lower** total board-units *while holding `P(promote | null)` at the
single-stage rate* (§4.3). If no such configuration exists — if every
budget saving costs either power or soundness — the honest finding is
*"the four partial cascades should stay independently wired; unification
buys nothing measurable,"* and the note stays deferred. **That negative
result is a legitimate and expected outcome of the harness.**

### 4.5 Slot-integrity and the cross-stage independence proof

Because a cascade adds stages and reserved bases, the harness must include
a `persist=True` slot-integrity test (`§13.2`, `§8.1` step 5): assert the
canonical r0 slots are byte-identical across a full cascade run, and every
stage's draws persist **only** under that stage's reserved base — the
proof that no stage replays an upstream stage's sample (the §3.2
independence invariant made mechanical). This is the
`test_full_mode_evidence_loop_never_touches_canonical_slots` pattern
lifted to the whole pipeline.

---

## 5. Measured results (first run)

The §4 harness is built and run. Home: `tools/cascade_oc.py` (the
measurement engine + a `python -m tools.cascade_oc` runner that emits a
machine-readable JSON report and a printed summary) and
`tests/test_cascade_oc_harness.py` (the pinned assertions behind the
`cascade_oc` pytest marker, **excluded from the default run**; one cheap
unmarked smoke test keeps the harness from rotting). The cascade under
measurement is a *simulated* composition of the shipped stages — the draws
flow through `runner._run_single` (the documented monkeypatch anchor) on the
seeded convergence-example noise model, but every **decision** is the shipped code: the
real `measure_noise_floor` (per-slice floors), the real
`RacingStrategy._apply_cut` rung, the real `evaluate_gate` / `holdout_confirms`,
and the real evidence-gated `resolve_tournament` terminal. Nothing about the
cascade itself was built.

**The numbers below are estimates rather than verdicts.** The end-to-end
promotion rates are proportions from a finite trial count: the Experiment-B
**null** condition (the soundness bar) is measured on **60** trials, every
Experiment-B **effect** condition and every Experiment-C cell on **16**. Each
rate therefore carries sampling error, reported as a **95% Wilson score
interval** (the count and interval travel with each rate in the tables). The
per-slice A/A **floors** (Experiment A) and the **board-unit budgets**
(Experiment C's `board-units` columns) are the exception: a floor is a measured
standard deviation and a budget is an **exact count** — a deterministic
function of the seeds, counted once per `runner._run_single` call, carrying
**no** sampling error. So in every table below the *rates* carry intervals and
the *budgets* do not.

### 5.1 Chosen parameters (§4.2 leaves these underspecified)

| Knob | Value | Rationale |
|---|---|---|
| `sigma` | `0.22` | the power harness's high noise setting (one full defect ≈ 1× the full-board A/A floor) — reused verbatim |
| A/A floor draws | `30` per slice | the power harness's precedent is 60 single-sample A/A draws; 30 on each of five slices keeps the sweep cheap while resolving the floor |
| rung false-cut trials | `48` | each trial runs two duels (better-arm and champion-equal decoy vs the champion) on the slice; 48 resolves the rate to ~2% |
| **rung field composition** | 1 better arm + 1 champion-equal decoy, `eta=2` | halving a 2-arm rung keeps exactly one, so a false cut is unambiguously "the better arm lost the rung to a champion-equal decoy on this noisy slice" — the sharpest per-rung operating characteristic |
| screen veto trials | `200` | matches the shipped screen OC test |
| Experiment B and C end-to-end **effect** trials | `16` | the power harness's regime; the evidence terminal is the cost driver |
| Experiment B **null** trials | `60` | the soundness requirement rests on this count, so it is raised to the doctrine's `AA_TRIALS=60` precedent (95% Wilson upper ~0.06 rather than 16's ~0.20); the null field never triggers the expensive evidence streak, so the extra trials stay cheap |
| Experiment B and C representative rung slice | `2` entries (~40%) | on this **5-entry** board a literal quarter-board rung is a *single* entry (pathologically noisy — see the m=1 column of §5.2); Experiment A sweeps the full m∈{1..4} separately |
| Experiment C field size | `6` (1 true + 5 decoys) | the budget saving is "prune the field cheaply, gate only the survivor" vs "full-board every candidate"; a field is needed to expose it |
| Experiment C terminal margin | `0.55 × floor` | a legitimate operator choice (`04-evaluation-statistics.md §13.8`): below the 1× planted effect so power survives, above the R-averaged null noise so `P(promote|null)` stays small |
| Experiment C terminal replicates | `16` | averages the terminal duel; the margin terminal's soundness/power hinge |

All seeds derive from `stable_noise_seed(workspace_seed, generation_id,
entry_id, replicate_index)` and are recorded in the JSON report. Every
confidence interval below is a **95% Wilson score interval** on the reported
count `k` of `n` trials (stated once here; not restated per cell). The
persisted JSON report is **byte-identical** across runs: the only run-to-run
varying field — the per-experiment wall-clock timing — is stripped from the
report on write and survives only in the printed summary. Verified: the
persisted report is `md5`-identical across two full runs and across
`PYTHONHASHSEED=0` and `PYTHONHASHSEED=12345` (no `hash()`-randomised seed
leaked in). The numbers below are from that run
(`uv run python -m tools.cascade_oc`); re-running reproduces them byte for
byte.

### 5.2 Experiment A — per-stage false-cut vs the slice floor

Measured full-board A/A floor (sd of the A/A `delta_scalar`): **0.640**. The
floor **grows as the slice shrinks**, confirming §3.1 (the floor is a
function of the slice rather than a constant):

| slice size m | 1 | 2 | 3 | 4 | 5 (full) |
|---|---|---|---|---|---|
| A/A floor | 1.042 | 1.086 | 0.903 | 0.740 | **0.640** |

The rung's false-cut rate (the probability that the better arm is eliminated) is
governed by that slice's **own** floor — an effect well above the slice floor
is essentially never cut; an effect below it is cut materially more often:

| planted δ (measured) | m=1 | m=2 | m=3 | m=4 | δ / slice-floor range |
|---|---|---|---|---|---|
| small (0.336) | 0.23 | 0.23 | 0.21 | 0.21 | ~0.31–0.45× |
| medium (0.672) | 0.19 | 0.19 | 0.06 | 0.06 | ~0.62–0.91× |
| large (2.016) | 0.02 | 0.00 | 0.00 | 0.00 | ~1.9–2.7× |

This is the coarse-cut discipline of §3.1 and step 4 of §4.2: a rung may not be
trusted to resolve an effect below its slice floor. The **veto stage**
(screen) follows the confirm-before-veto squaring: measured false-veto rate
**0.035 ≈ σ² (0.048)**, an order below the naive any-flip alternative's
**0.195 ≈ σ** on the identical seeded draws — the noisiest stage is allowed
only the coarsest (categorical-breakage) cut.

### 5.3 Experiment B — end-to-end P(promote | ·), cascade ON vs single-stage OFF

Evidence-gated terminal (the shipped `resolve_tournament` pre-gate), on
identical seeded draws for every column. All three columns share the **same**
evidence-gated terminal and the **same** per-trial draws — the naive column
differs from the cascade column *only* in its rung rule (gate-at-every-rung vs
rank-halve), so the contrast is rule-vs-rule on identical samples. Each rate is
`k/n` with its 95% Wilson interval; the null bar is 60 trials, each effect 16:

| condition | cascade ON | single-stage OFF | naive "gate at every rung" |
|---|---|---|---|
| **null (A/A)**, n=60 | **0/60 = 0.00** [0.00–0.06] | **0/60 = 0.00** [0.00–0.06] | **0/60 = 0.00** [0.00–0.06] |
| small (0.5× floor), n=16 | 9/16 = 0.56 [0.33–0.77] | 15/16 = 0.94 [0.72–0.99] | 12/16 = 0.75 [0.51–0.90] |
| medium (1× floor), n=16 | 8/16 = 0.50 [0.28–0.72] | 16/16 = 1.00 [0.81–1.00] | 10/16 = 0.62 [0.39–0.82] |
| large (3× floor), n=16 | **16/16 = 1.00** [0.81–1.00] | **16/16 = 1.00** [0.81–1.00] | 16/16 = 1.00 [0.81–1.00] |

Reading:

- **The hard soundness requirement holds in all three columns.**
  `P(promote | null)` is zero over all 60 null trials under the cascade,
  under the single stage, **and** under the naive gate-at-every-rung rule
  (95% Wilson upper bound ~0.06 for each) — indistinguishable from the
  doctrine's A/A behaviour (fact #4). Pairing the gate-at-every-rung rule
  with a *weaker margin terminal* instead produces a null-promotion rate of
  0.25, so that rate is a property of the weaker terminal rather than of the
  rung rule. Measured on the same draws through the same shipped
  evidence-gated terminal, the naive rung rule is **sound too**. On a
  fresh-draw terminal the terminal is what enforces soundness, because the
  fresh draw is what removes the selection bias (§3.3, item 1); the rung
  rule governs power and budget.
- **Staging costs power at small effects, and the harness quantifies it.** At
  the unmissable 3× effect all three columns promote in all 16 trials, so the
  cascade loses nothing there. At the 1× and 0.5× effects the cascade's
  rank-halve recovers only ~0.50–0.56 of what the single stage does — the
  2-entry early rung false-cuts the true improvement on its noisy slice before
  the terminal ever sees it. The rank-halve is the *lowest*-power column: the
  naive gate-filter, which barely cuts a 2-arm field, keeps more of the true
  effect (0.62–0.75) than the forced rank-halve does. Experiment B measures
  none of this in board-units, so the cascade's justification rests on
  Experiment C's budget curve rather than on power. The cascade never *gains*
  power the single stage lacks, which is the protected-incumbent invariant in
  measured form.

### 5.4 Experiment C — the budget-savings-vs-power curve (the build artifact)

Margin terminal, `field=6`, `margin = 0.55×floor = 0.352`, terminal
replicates 16, board-units counted exactly (one per `runner._run_single`
call). x = mean total board-units spent per promotion; y = `P(promote|true)`:

Power is `k/16` with its 95% Wilson interval; board-units are exact counts
(no interval):

| planted δ | config | power (n=16) | board-units (exact) | vs baseline |
|---|---|---|---|---|
| **large** | single-stage baseline | 15/16 = 0.94 [0.72–0.99] | 1152 | — |
| large | half-r2 | **16/16 = 1.00** [0.81–1.00] | 240 | **4.8× cheaper, +0.06 power** |
| large | quarter-r1 | 15/16 = 0.94 [0.72–0.99] | 216 | **5.3× cheaper, equal power** |
| large | aggressive-r1 | 15/16 = 0.94 [0.72–0.99] | 216 | **5.3× cheaper, equal power** |
| **medium** | single-stage baseline | 14/16 = 0.88 [0.64–0.96] | 1152 | — |
| medium | half-r2 | 5/16 = 0.31 [0.14–0.56] | 240 | cheaper but **loses 0.57 power** |
| medium | quarter/aggressive | 3/16 = 0.19 [0.07–0.43] | 216 | loses 0.69 power |
| **small** | single-stage baseline | 6/16 = 0.38 [0.18–0.61] | 1152 | — |
| small | all cascade configs | 0/16 = 0.00 [0.00–0.19] | 216–240 | loses all power |

The build-candidate rule the harness applies (report-only: a config that
reaches the reference power within five percentage points at no more than
75% of the reference budget) returns configs **only at the large effect**
and **none** at the medium or small effects. Even at the large effect the
qualification is uneven. One configuration, `half-r2`, promotes in all 16
trials and clears the threshold comfortably. The other two, `quarter-r1` and
`aggressive-r1`, promote in 15 of 16 and clear it only **marginally**: one
trial flipping (to 14 of 16, or 0.875) would drop them below the
`baseline − 0.05` = 0.888 threshold and disqualify them. The large-effect
budget signal is real, but for two of the three configs it rests on a single
trial.

### 5.5 The slot-integrity and cross-stage independence proof (§4.5)

The test passes: across a full cascade run the canonical replicate-0 `loss.json` bytes
are unchanged for both sides, the calibration draws persist under base
**1000**, the evidence draws persist under base **4000** for both sides, and
the three bases `{0, 1000, 4000}` are disjoint. The screen's base-3000 draws
live under swept phantom directories by design, so its isolation is witnessed
by r0 being untouched rather than by a persisted slot. The cross-stage
draw-independence invariant (§3.2) holds mechanically.

### 5.6 An honest reading of what this supports — and does not

What the first run **supports**:

1. **The staged cascade is sound.** `P(promote|null)` is zero over all 60
   null trials under the cascade — indistinguishable from the single-stage
   contract's zero over 60 and from the doctrine's A/A behaviour (95% Wilson
   upper ~0.06) — and the reserved-base ledger keeps every stage's draws
   independent (§4.5). Measured on identical draws through the identical
   evidence-gated terminal, the naive "gate-at-every-rung" alternative is
   zero over 60 **too**: the fresh-draw terminal is what enforces soundness
   (§3.3, item 1). Pairing that alternative with a weaker terminal instead
   yields 0.25, which is why the terminal rather than the rung rule carries
   the soundness. What the harness validates is that the *terminal* must be a
   fresh-draw evidence gate; the rung rule (rank-halve, veto, or gate) is a
   power and budget knob beneath it rather than a soundness lever.
2. **There is a real budget win at large effects.** When the true
   improvement is unmissable (≥ ~2× the full-board floor), a cascade reaches
   the single-stage contract's power (0.94–1.00 [95% CI 0.72–1.00]) at **≈5×
   fewer board-unit evaluations** (216–240 vs 1152, exact counts) by pruning the
   field cheaply and gating only the survivor.

What it **does not** support, and why the build decision remains open:

3. **The budget win disappears at the effect sizes that matter most.** At 1× and
   below the full-board floor — the regime a maturing proposer actually
   operates in — the cheap early rungs false-cut the true improvement.
   Experiment A measures the sub-slice-floor false-cut rates that produce
   it. Experiment B measures 0.50–0.56 power over 16 trials (95% CI
   0.28–0.77) against the single stage's 0.94–1.00 (0.72–1.00), and
   Experiment C measures a collapse to 0.00–0.31 power over 16 trials. On
   this 5-entry board a realistic early rung is 1–2 entries, and a
   1–2-entry slice cannot resolve a floor-sized effect. So the cascade saves budget **only**
   where the single stage would already have succeeded, and *costs* power in
   the regime where the single stage carries the decision.

Both outcomes the harness can return remain open. **Build** is defensible if
operators judge the large-effect budget saving worth the small-effect power
cost, as in an early-epoch, many-candidate, large-effect regime. **Do not
build** is defensible if the small-effect power loss is unacceptable, keeping
the four partial forms independently wired, which §4.4 allows. A larger board
would widen the early rung slices and would likely soften point 3; that is the
single most useful next measurement. **This note does not make the decision;
it reports the measurements.**

---

## 6. The config sketch (NOT implemented)

The endorsed shape is **one nested frozen block** on the contract, layered
under the existing `tournament_structure`, rather than a top-level structure
or four independent knobs. It follows the omit-at-default discipline
(`03-contract-and-epochs.md §"Omit-at-default"`; `SCORING.md §2.5`) so that
**an absent `cascade` block canonicalizes byte-for-byte identically to
today** and no existing epoch rolls:

```jsonc
// FUTURE / SPECULATIVE — no loader, strategy, or test reads this today.
"tournament": {
  "structure": "racing",
  "params": { "field_size": 8, "eta": 2, "board_fraction": 0.25 },
  "cascade": {
    "total_budget_board_units": 240,   // one ledger; allocated cheap-early/expensive-late
    "stages": [
      { "kind": "screen",  "slice": "train:2",       "cut": "veto",           "confirm": true },
      { "kind": "rung",    "slice": "train:0.25",     "cut": "rank_halve",     "eta": 2 },
      { "kind": "rung",    "slice": "train:0.5",      "cut": "rank_halve",     "eta": 2 },
      { "kind": "full",    "slice": "train:1.0",      "cut": "gate" },
      { "kind": "holdout", "slice": "holdout",        "cut": "ladder_confirm" }
    ],
    // the N-stage correction (§3.3, item 2): terminal evidence budget scales
    // with measured upstream selectivity rather than a fixed constant.
    "terminal_evidence": { "scale_with_selectivity": true, "min_replicates": 8 }
  }
}
```

Design properties this sketch commits to:

- **`cascade` is a nested frozen dataclass**, so it recurses through
  `scoring_to_canon` in the same way as the shipped `overfitting` / `ladder` /
  `tournament_structure` blocks (`03-contract-and-epochs.md §3.2`), and the
  same omit-at-default check applies to each field. **It rolls the epoch on
  change** — a cascade edits *what a promotion means*, the same rationale
  as any `tournament_structure` change (`TOURNAMENT-STRUCTURES.md §4.1`).
- **Default = empty stage list ⇒ today's behavior**: the screen, racing,
  gate, and holdout run as they do now, independently configured.
  The cascade block is purely additive opt-in.
- **Each stage names its own reserved base** at load, extending the ledger
  (`04-evaluation-statistics.md §8.1`) — the loader assigns and
  cross-checks bases so §4.5's independence invariant holds by
  construction. (`6000` is claimed by eval-synthesis admission —
  EVAL-SYNTHESIS.md §5 — so a cascade build takes the next free base.)
- **The terminal gate, the holdout/Ladder, the evidence gate, and the
  `SelectionStrategy` seam are all unchanged** — the cascade *orders and
  budgets* them; it does not reimplement any of them.

This section is a sketch of where the block would attach. **No such key
exists in the loader, the strategies, or the tests today.**

---

## 7. Relationship to the partial forms — what unification absorbs

| Shipped form | Under a cascade | Deprecated? |
|---|---|---|
| Candidate screen (`epoch/screen.py`) | becomes **stage 0** (`kind: screen`, veto-first, confirm-before-veto retained verbatim) | the standalone `proposer_quality` screen wiring is **absorbed** rather than removed — a cascade with no `screen` stage runs today's screen unchanged |
| Racing rungs (`selection/strategies/racing.py`) | become the **middle `rung` stages** (rank-halve, escalating slice) | racing as a standalone `tournament_structure` **stays**; the cascade merely lets its rungs be interleaved with a screen and an explicit terminal budget |
| Full gate (`tournament/gate.py`) | becomes the penultimate **`full` stage** | **unchanged** — the three-rule ladder is the cut rule, verbatim |
| Holdout / Ladder (`ladder.py`, gate rule 4) | becomes the terminal **`holdout` stage** | **unchanged** — Ladder release + budget rules retained; the cascade only guarantees it is the never-selected-on anchor |
| A/A calibration (`calibration.py`) | **not a stage** — a measurement the cascade *consumes* (per-slice floors, §4.2) | unchanged |
| Evidence gate (`evidence_gate.py`) | **not a stage** — the terminal confirmation whose budget the cascade *scales with selectivity* (§3.3, item 2) | unchanged; opt-in as today |
| Placebo arm (`evolve/placebo.py`) | **not a stage** — the whole-pipeline control (§3.3, item 4) | unchanged; its finding is elevated to cascade-level |

Unification is therefore a **configuration and accounting**
change — one ordered spec, one budget ledger, one reserved-base
allocation, and one place that scales the terminal evidence with upstream
selectivity — over four mechanisms that already exist and already compose
pairwise. It **absorbs** their independent wiring; it **deprecates
nothing** operators rely on, because the empty default is byte-identical to
today. Nothing here weakens the protected-incumbent invariant, the noise
doctrine, or the overfitting boundary — a cascade that tried to would fail
Experiment B's null bar (§4.3) and never ship.

---

## 8. Status and the build-decision gate

- **Status: designed and measured; the build decision is pending.** No loader,
  strategy, or `cascade` config block exists in the tree — the cascade itself
  is still unbuilt, and the four partial forms ship and are documented in
  their own docs. What *has* landed is the §4 **OC harness** and its first
  measured run (§5): `tools/cascade_oc.py` + the `cascade_oc`-marked
  `tests/test_cascade_oc_harness.py`. The build/no-build call is the
  operator's and is **not** made here.
- **What gates a build decision:** the **operating-characteristic evidence
  of §4**, whose measurements §5 reports:
  1. Experiment A shows per-stage false-cut rates that respect each
     stage's *slice* floor and the coarse-cut discipline (§4.2) — **passed**
     (§5.2): the floor grows as the slice shrinks, and rung false-cut tracks
     δ / slice-floor; the veto stage squares to ≈ σ².
  2. Experiment B shows the cascade does **not** raise `P(promote | null)`
     above the single-stage contract's rate, which is the hard soundness
     requirement (fact #4). **Passed** (§5.3): zero promotions over 60 null
     trials for the cascade, the single stage, **and** the naive
     gate-at-every-rung alternative on identical draws through the same
     terminal, with a 95% Wilson upper bound of ~0.06 each, so the terminal
     rather than the rung rule carries the soundness. The power the cascade
     holds at the 1× and sub-1× planted δ is nevertheless well *below* the
     single-stage contract — 0.50–0.56 over 16 trials (95% CI 0.28–0.77)
     against 0.94–1.00 (0.72–1.00). That is a quantified staging cost and
     lands outside the stated tolerance of §4.3.
  3. Experiment C exhibits a stage allocation that reaches the reference power
     at materially lower board-units — **only at the large (≥ ~2× floor)
     effect** (≈5× cheaper, §5.4). Even there the qualification is uneven:
     one config (`half-r2`, promoting in all 16 trials) clears the report-only
     build threshold comfortably; the other two (15 of 16) clear it only
     marginally, one trial from disqualifying. At the 1× and small effects
     *no* config qualifies. The
     justification for the added machinery therefore holds in one effect-size
     regime — and there only partly — and fails in another (§4.4).
  4. §4.5's slot-integrity test proves cross-stage draw independence —
     **passed** (§5.5).
- **"Do not build" is a legitimate outcome.** The first run leaves both
  outcomes open (§5.6): a build is defensible for an early-epoch,
  many-candidate, large-effect regime, and keeping the four forms
  independently wired is defensible if the sub-floor power loss is
  unacceptable. The harness can *reject* the cascade as well as endorse it,
  and here it does neither; it reports the curves to the operator.

---

## 9. Cross-references

| Topic | Document |
|---|---|
| The scalar loss + the three-rule gate the cascade terminates in | [`SCORING.md`](SCORING.md) |
| The gauntlet gate, the schedulers, racing's board-slice rungs (`§3.5`) | [`SELECTION.md`](SELECTION.md), [`TOURNAMENT-STRUCTURES.md`](TOURNAMENT-STRUCTURES.md) |
| Winner's curse / optimizer's curse, replicate-first-resolve-second | [`SELECTION-THEORY.md`](SELECTION-THEORY.md) |
| Train/holdout split, the Ladder, restricted proposer visibility | [`OVERFITTING.md`](OVERFITTING.md) |
| The noise doctrine, A/A floors, evidence gate, placebo, reserved bases, the power-harness methodology | dev-guide `04-evaluation-statistics.md` |
| The candidate screen's veto-first / confirm-before-veto doctrine | `src/zicato/epoch/screen.py`, `04-evaluation-statistics.md §3.3` |
| The contract hash + omit-at-default discipline the config sketch follows | `03-contract-and-epochs.md`, [`EPOCHS-AND-JOURNALING.md`](EPOCHS-AND-JOURNALING.md) |
