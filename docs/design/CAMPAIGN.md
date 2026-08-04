# The live measurement campaign — deciding the scaffold defaults with evidence

> **STATUS — EXECUTED TWICE; THE STANDING ANSWER IS RECORDED BELOW.
> ANY FURTHER EXECUTION IS GATED ON EXPLICIT OPERATOR GO-AHEAD.**
> Two valid campaigns have run (see **Results**). Nothing in this document
> authorizes a third. Every command in §6 is a *plan*; a live `zicato evolve`
> invocation against a real model endpoint may be started only after the
> operator gives an explicit go-ahead for that specific arm (the G3 live-run
> gate). The design sections below pre-register the arms, the metrics, the
> power math, and the decision rules **before** any new data exists — the same
> discipline `tools/cascade_oc.py` and `tests/test_decision_procedure_power.py`
> apply to every statistical claim in the repository (dev-guide
> `04-evaluation-statistics.md` §13: operating characteristics are *measured,
> not asserted by hope*).

**Reading convention for every number in this document.** A figure marked
**measured** was produced by one of the two valid runs and is quoted from its
run report. A figure marked **derived** is arithmetic on a measured figure,
computed here. A figure marked **planning** is an assumption for a run that has
not happened. The original version of this document mixed all three without
labelling them, and that is how a ~10×-optimistic cost estimate survived to
execution.

---

## Results — the standing record

### R.1 Every pre-#110-fix run is void

`ADKHarnessAdapter.load()` put the generation snapshot on `sys.path` and
re-imported the entrypoint, but never verified that the module actually came
from there. With an absolute, already-importable entrypoint module path it
cannot — so **champion and challenger ran byte-identical code**. Filed and
fixed as **#110**.

Every campaign run predating that fix is **withdrawn, not merely downgraded**:

- An earlier run flagged the **ensemble-roles arm as a lead candidate (3/3
  seeds vs 0/3 for BASE)**. Withdrawn.
- Another had the **screening arm graduate a K=6 screen and then fail to
  confirm**. Withdrawn.

Both were noise with arm labels attached.

**This document's own validity principle, and the reason the controls in §2 are
mandatory rather than advisory:**

> **Without a planted-defect control, a dead instrument is indistinguishable
> from a null result.**

Nothing in the pre-fix output distinguished the two. Every symptom read as an
ordinary negative result: a deliberately crippled control arm scoring *best*, a
best-observed improvement of **+0.041** against a noise floor of
**0.080–0.106**, **71 of 72 duels rejected** (all **measured**, all void). That
is exactly what identical code on both sides produces. The null would have been
written up.

Post-fix — registering an entrypoint that resolves *inside* the snapshot root —
the loop demonstrably works, verified on three independent channels in a 16-cell
run (**measured**):

| Channel | before fix | after fix |
|---|---|---|
| crippled control, champion score | 0.4528 (**highest** of all arms) | **0.3460 (lowest)**, baseline 0.4674 |
| crippled control, mean per-duel improvement | +0.0029 vs baseline | **+0.0690** vs baseline −0.0416 |
| crippled control, promotions | 0 | **4** of the run's 5 |

and directly: the crippled arm starts at score **0.0000** and climbs to
**0.28–0.46** after its challenger is promoted, which is only possible if the
patched generation actually executes.

That 16-cell run is itself **INVALID for ranking**: it failed its own
pre-registered sensitivity bar, because the bar had been specified on the wrong
scale (a per-run sd gating a difference of arm means). It was **not** rescored
on that basis — the threshold was fixed in advance precisely so it could not be
renegotiated after seeing the numbers. §4 fixes the underlying defect: a
threshold is pre-registered **on the same scale as the quantity it gates**.

Also filed from that effort: **#106** pre-flight probes only the first mutation
point · **#107** goal truncation · **#108** replicate averaging takes replicate
0 · **#109** `--mode fast` ignores `replicates` · **#111** gate records scalars
only on reject · **#112** pre-flight never checks margin achievability. Note
**#106** and **#112** together mean **pre-flight would have passed every invalid
run** — a pre-flight that cannot fail on a dead instrument is not a gate.

### R.2 Two valid runs, identical design by construction

| | **Run 1** | **Run 2** |
|---|---|---|
| Date | 2026-07-31 | 2026-08-02 |
| Instrument | post-#110 fix | post-#118–#130 fix wave |
| Design | 12 arms × K=12 paired seeds = **144 cells** | **identical, deliberately** |

Run 2's design was held **identical on purpose**, so that any movement between
the two is attributable to the *instrument* rather than to a redesign. Shared
design (**measured**, both runs):

- **12 arms × K=12 paired seeds = 144 cells**, **3 rounds per cell**.
- Board: **single-turn, 5 entries**. `promote_margin` **0.20**.
- Ten arms are the ones §§1–2 name — BASE, genealogy, screening, mechanical
  recombination, LLM-merge recombination, breadth/depth proposer roles,
  genealogy + calibration feedback, screening + recombination, a best-of-1
  ablation, process exemplars — **plus both controls in the same batch**: an
  **A/A duplicate of BASE** and a **planted-defect arm**.
- **Endpoint:** per-duel challenger improvement
  `d = loss(champion) − loss(challenger)`, averaged to a **cell mean**, compared
  with the **cell** as the unit of analysis (§3.1).
- **Floor, derived not asserted:** `2 × SE(mean d[A/A clone] − mean d[BASE])`
  (§3.2).

Run 2 additionally reports **144/144 cells, 432 rounds, zero missing gates** —
a statistic that exists only because of the integrity check in R.5.

### R.3 Both validity gates pass, on both runs

```
Run 1                                                        (measured)
  floor (derived)  2 × SE(A/A clone − BASE)  = 0.0378
  sensitivity      planted-defect − BASE     = +0.1086  90% CI [+0.0760, +0.1411]  PASS  (2.9× floor)
  specificity      A/A clone      − BASE     = −0.0144  90% CI [−0.0469, +0.0182]  PASS

Run 2                                                        (measured)
  floor (derived)  2 × SE(A/A clone − BASE)  = 0.0400
  sensitivity      planted-defect − BASE     = +0.1526  90% CI [+0.1264, +0.1787]  PASS  (3.8× floor)
  specificity      A/A clone      − BASE     = +0.0013  90% CI [−0.0334, +0.0360]  PASS
```

The A/A duplicate is two configurations **identical by construction**; that it
separates by essentially nothing is what makes the null below *informative*
rather than an absence of evidence. The planted defect registering at 2.9× and
then 3.8× the floor is what makes the instrument *alive*.

### R.4 Zero of nine features graduate — twice

**Run 1** (Holm-adjusted across the nine treatments; every call **inconclusive**):

| arm | feature | diff vs BASE | 90% CI | Holm p |
|---|---|---|---|---|
| A4 | LLM-merge recombination | **+0.0195** | [−0.0162, +0.0552] | 1.000 |
| A6 | genealogy + calibration feedback | −0.0038 | [−0.0339, +0.0264] | 1.000 |
| A2 | screening | −0.0060 | [−0.0399, +0.0279] | 1.000 |
| ablation | best-of-1 | −0.0069 | [−0.0398, +0.0260] | 1.000 |
| A3 | mechanical recombination | −0.0128 | [−0.0428, +0.0171] | 1.000 |
| A1 | genealogy | −0.0129 | [−0.0495, +0.0237] | 1.000 |
| PEXEMPLAR | process exemplars | −0.0136 | [−0.0509, +0.0237] | 1.000 |
| A5 | breadth/depth roles | −0.0148 | [−0.0518, +0.0223] | 1.000 |
| A7 | screening + recombination | −0.0162 | [−0.0507, +0.0183] | 1.000 |

Six of nine arms point below baseline **and the A/A clone (−0.0144) sits among
them**. The negative signs are noise, not evidence the arsenal hurts. The clone
is the ruler.

**Run 2** (same adjustment):

| arm | feature | diff vs BASE | 90% CI | Holm p |
|---|---|---|---|---|
| ablation | best-of-1 (**arsenal OFF**) | **+0.0392** | [+0.0130, +0.0654] | 0.537 |
| A1 | genealogy | +0.0244 | [−0.0022, +0.0510] | 0.842 |
| A7 | screening + recombination | +0.0195 | [−0.0062, +0.0452] | 1.000 |
| A5 | breadth/depth roles | +0.0155 | [−0.0085, +0.0394] | 1.000 |
| A4 | LLM-merge recombination | +0.0098 | [−0.0192, +0.0387] | 1.000 |
| A6 | genealogy + calibration | +0.0044 | [−0.0216, +0.0303] | 1.000 |
| PEXEMPLAR | process exemplars | +0.0047 | [−0.0317, +0.0411] | 1.000 |
| A2 | screening | +0.0028 | [−0.0214, +0.0270] | 1.000 |
| A3 | mechanical recombination | −0.0027 | [−0.0258, +0.0204] | 1.000 |

**The highest arm in run 2 is the ablation — the arsenal turned further off —
sitting just under the floor. No arsenal feature beats doing less.**

Holm adjustment is not optional bookkeeping: nine arms sharing one baseline at
an uncorrected 10% would expect **roughly one spurious graduate**.

**Run-over-run comparison** (**measured**):

| | Run 1 | Run 2 |
|---|---|---|
| derived floor | 0.0378 | **0.0400** |
| sensitivity | +0.1086 (2.9× floor) | **+0.1526 (3.8× floor)** |
| specificity (A/A) | −0.0144 | **+0.0013** |
| promotions | **10 / 432**, all planted-defect | **6 / 432**, all planted-defect |
| graduates | 0 of 9 | **0 of 9** |
| largest arm | A4 +0.0195 | ablation +0.0392 (A4 now +0.0098) |

Readings that survive both runs:

- **The fix wave did not move the floor.** The noise is a property of the
  target, not of the gate arithmetic. What the wave did move: it tightened the
  sensitivity contrast and halved the promotion rate — consistent with a
  stricter, better-calibrated gate. **No arm changed status.**
- **Only the planted-defect arm ever promoted**, in either run — 10 of 12 cells
  in run 1, every other arm 0 of 12. With a healthy champion at
  `promote_margin` 0.20, no arsenal variant produced a challenger that cleared
  the bar. That is consistent with the null on `d`.
- **The planted-defect arm starts from a generation scoring ~0 and climbs back
  toward baseline.** The loop **repairs a maximal planted defect**; what it does
  not do is measurably benefit from any arsenal knob.
- **The per-arm ordering reshuffled, with A4 falling from first to fifth.** That
  is exactly what re-running a set of effects drawn from the same null looks
  like, and it is independent evidence that A4's run-1 lead was noise.

### R.5 The integrity failure this campaign found the hard way

Run 2 reached **"144/144 cells, 12 per arm, both gates PASS" twice on data that
turned out unusable.** The mechanism generalises to anyone running long parallel
evolve sweeps, and it is why §6.6 exists.

When credentials for the model endpoint lapse mid-run, **a cell does not fail
cleanly.** A cell straddling the outage produces a deck from the rounds that ran
beforehand, satisfies a "did we reach the model" liveness check, gets marked
complete, and silently contributes a cell mean built from **fewer duels than its
peers**.

Cell-level accounting reported 144/144 healthy while (**measured**):

- **83 of 496 rounds had no `gate_evaluated` at all**;
- the loss was **arm-correlated** — **26% of the baseline arm versus 8% of
  another**, **73 of 144 cells affected**, **baseline at 11 of 12**.

Because the baseline anchors both the derived floor and every comparison, that
**distorts the ruler itself**. The arm pattern carried no signal — it recorded
which cells happened to be in flight at the moment credentials lapsed. Run 1's
**0 of 432** on the same statistic is what identified this as contamination
rather than a property of the loop.

Two fixes, both verified against the known-bad set, now normative in §6.6:

1. **Cell acceptance requires round completeness.** Reject any cell containing a
   round that **both** lacks a `gate_evaluated` **and** carries a hard
   credential error. **Deliberately narrow:** a round where the proposer *was*
   reached and genuinely produced an invalid patch is a **real measurement** and
   must still be accepted — otherwise a legitimately-degraded arm gets retried
   to exhaustion.
2. **Analysis requires the completion marker.** Partial round logs must not
   contribute truncated cell means; survivors of an infrastructure failure are
   **not a random subset** of a cell's duels.

When credentials lapsed a second time the hardened check caught it
automatically: **12 cells contaminated, zero marked complete.** Those were
**deleted and re-run — deleted rather than resumed**, since resuming appends
into the poisoned epoch, which is how a cell ended up with four rounds of which
three were void.

> **The general lesson: liveness is not integrity.** A long parallel measurement
> needs its completeness check at the granularity the **endpoint** consumes —
> rounds here — not at the granularity the scheduler tracks. A run reporting
> 100% healthy at the wrong granularity is more dangerous than one that fails
> loudly.

### R.6 The resolution limit, stated plainly

This campaign **resolves effects ≥ 0.040** on the per-duel `d` scale — roughly
**26% of the planted-defect signal** (**measured**, run 2; run 1's floor of
0.0378 is ~35% of its weaker planted-defect signal).

- **Every "inconclusive" above means *no effect larger than the floor*, never
  *no effect*.** A feature worth +0.02 would be invisible here.
- Resolving **~0.023** needs **K ≈ 32** cells per arm (**measured** as the
  operator's sizing anchor, off run 1's 0.0378 floor). Scaling run 2's 0.0400
  the same way puts K=32 at **≈0.025** (**derived**) — the two floors bracket
  it; §3.4 states the arithmetic.
- **Below ~0.013 the cost stops being affordable** at any K this target can
  support (**derived**, `0.0400 · √(12/K) = 0.013` ⇒ ≈ 114 cells per arm).
- Scope bounds, all binding: **one board, one model, three rounds per cell,
  single-turn only.** This says nothing about multi-turn revision robustness,
  nor about interactions beyond the two combined arms actually run.
- **The holdout machinery was structurally inert for both valid runs.** The
  executed board carried **5 entries**, below the `min_board_size_for_split`
  default of **6** (`core/scoring_config.py`), so `split_board` produced an
  **empty holdout** (`board/split.py`) and every holdout-derived reading —
  holdout confirmation, the `generalization_gap` detector — had nothing to run
  on. This is a **limitation of the record, not a retraction**: the primary
  endpoint `d` comes off `GateEvaluated` on the train side and is unaffected.
  But no claim about generalization or overfitting can be sourced from these
  two runs, and §4's process-exemplars bar (which requires a quiet
  generalization gap) was **untestable** on them. §2.3 fixes this for any
  future run.

### R.7 The standing recommendation

> **Keep the arsenal default-off.** This is now the conclusion of **two
> independent 144-cell runs** whose sensitivity and specificity both pass on the
> same data.

What this **settles**: no arsenal feature delivers more than **0.040** in
per-duel proposal quality on this target. The sweep is *not* underpowered
against effects that matter — the floor is about a quarter of the
planted-defect signal.

What this **does not settle**: anything below 0.040, and anything outside the
R.6 scope bounds.

If more compute goes here, the pre-registered spend is **A4 alone at K ≈ 32**
(floor ≈ 0.023) — **not** a re-run of the sweep. Note honestly that run 2
demoted A4 from first to fifth, so even this is a coin the campaign is buying
another look at, not a lead.

---

## 0. Why this campaign exists

zicato's generator arsenal is almost entirely **default-off and unmeasured
on live runs**. Every one of these knobs ships inert and rolls its own epoch
the moment it is set to a non-default (`core/scoring_config.py`
`ProposerQualityConfig`, all flagged `omit_at_default=True` except
`best_of_n` / `critique_enabled`):

| Knob | Default | Rolls epoch when set? | Cost class |
|---|---|---|---|
| `proposer_quality.best_of_n` | `3` (ON) | yes (not omit-at-default) | aux propose calls `× best_of_n` |
| `proposer_quality.critique_enabled` | `True` (ON) | yes | one aux critique call when `best_of_n > 1` |
| `proposer_quality.screen_entries` | `0` (OFF; scaffold writes `2`) | yes | board runs `proposes × best_of_n × panel` |
| `proposer_quality.screen_veto_only` | `False` | yes | none (advisory) |
| `proposer_quality.process_exemplars` | `0` (OFF) | yes | read-side; cost meter untouched |
| `proposer_quality.recombine` | `False` (OFF) | yes | **cost-neutral** (mint replaces a propose call) |
| `proposer_quality.recombine_merge` | `"mechanical"` | yes | `"llm"` adds one aux merge call |
| `proposer_quality.genealogy` | `0` (OFF) | yes | read-side; cost meter untouched |
| `proposer_quality.calibration_feedback` | `0` (OFF) | yes | read-side; cost meter untouched |
| breadth / depth ensemble **roles** | unset (both `None`) | **NO** — runtime infra, not a contract input | none on the board-run axis |

The scaffold (`recommended_scaffold_weights()` in `core/scoring_config.py`)
already makes two of these choices *by taste*: it writes `screen_entries=2`
and leans on the in-code `best_of_n=3` default, while leaving `genealogy`,
`recombine`, `recombine_merge`, `calibration_feedback`,
`process_exemplars`, and the ensemble roles off. This campaign replaced taste
with a pre-registered knob sweep — **and the answer, twice, was that none of the
six unmeasured choices earns a flip** (Results R.4). The document remains live
because the *question* recurs: a new knob, a new target, or a new board reopens
it, and the protocol below is what a reopening must follow.

> **The roles caveat (grounded, load-bearing).** The breadth/depth roles are
> *proposer infrastructure*, resolved by `wrap_with_proposer_quality` from
> `RuntimeConfig.proposer_breadth_call_llm` / `..._depth_call_llm` (a
> `models.proposer_breadth` / `models.proposer_depth` block —
> `models_config.py`), NOT a `ScoringWeights` field. Configuring them does
> **not** roll the epoch by construction. To keep the "one arm = one epoch"
> property honest, the roles arm is run in its **own fresh workspace** (§6),
> not distinguished from BASE by a contract hash. Every other arm rolls its
> epoch purely from its `scoring.json` delta.

## 1. The question set, ranked

Ranked by *plausible lift in proposal quality per cost* — a **prior**, written
before any data. It is retained as the pre-registration record. **All nine
priors were tested and none survived** (R.4); the ranking below is therefore a
record of what was expected, not of what is true.

| Rank | Knob (arm) | Why it plausibly raises proposal-quality-per-cost | What result flips its default |
|---|---|---|---|
| 1 | **`recombine` (mechanical)** | The only knob with a mechanism *proven in the known-answer oracle* to capture a promotion the gate would otherwise reject: dev-guide §1.8's two-marker world plants two disjoint single-fixes each worth Δ 1.2 that **each reject** at `promote_margin=1.5`, while the mechanical union of their patches (Δ 2.4) **promotes** (`tests/test_recombination_known_answer.py`). It raises promotions-per-round at **zero** extra board-run cost (the mint replaces a slot's propose call). Best per-cost candidate on the board. **Measured: A3 = −0.0128 then −0.0027. The oracle mechanism is real; it does not show up on a live target at this resolution.** | Flip `recombine` → default-`True` if the arm clears the §4 bar. |
| 2 | **`genealogy`** | In-context lineage evolution reaches even the pure-drift-side rejected pairs the mechanical slot cannot see (`ProposerQualityConfig.genealogy` docstring). **Read-side only — the cost meter is untouched** — so any lift is free on the board-run axis. But *free on cost is not free on risk*: its own docstring says that, like `process_exemplars`, it **widens the proposer-visibility channel** and is therefore NOT scaffold-set — the operator opts in deliberately. Its flip bar carries the same `generalization_gap` + placebo condition §4 puts on the extension arm. **Measured: A1 = −0.0129 then +0.0244 (largest single-knob arm in run 2, still under the floor and Holm p 0.842).** | Flip `genealogy` → a non-zero scaffold default if it clears the (low, read-side) §4 bar. |
| 3 | **`best_of_n` (held at 3 in BASE)** | The top proposal-quality lever per `FUNCTIONALITY-RECOMMENDATIONS.md` §4.1 — a valid-but-mediocre single sample was never reconsidered. Already ON; BASE validates it still earns its `× best_of_n` aux-call cost. **Measured: the `best_of_n=1` ablation was the HIGHEST arm in run 2 (+0.0392) and mid-pack in run 1 (−0.0069). Doing less did not measurably hurt.** | Keep `best_of_n=3` if BASE beats the ablation; revert toward `1` otherwise. |
| 4 | **`screen_entries` (currently scaffold-ON=2)** | Vetoes catastrophic candidates *before* the tournament spends on them — but **adds** board runs (`proposes × best_of_n × panel`, §5). Its per-cost effect is genuinely ambiguous, which is exactly why it is the scaffold choice most in need of audit. Screen false-veto ≈ flip-rate² under confirm-before-veto (dev-guide §3.1 fact #7) means the veto is *sound*; the open question is whether the extra panel runs buy net throughput. **Measured: A2 = −0.0060 then +0.0028 — no signal either way, at a real cost premium.** | **Reverse null**: `screen_entries` stays scaffold-`2` only if it clears §4; otherwise the pre-registered action is to **remove it** from `recommended_scaffold_weights` (scaffold default → `0`). |
| 5 | **breadth/depth roles** | If breadth explores a wider slate and depth refines the critique/merge, slate quality rises with **no** board-run cost. Second-order: it reshapes *which* candidate wins, not how many board units run. **Measured: A5 = −0.0148 then +0.0155. The pre-#110 "3/3 seeds vs 0/3" lead for this arm is WITHDRAWN (R.1).** | Flip to a scaffolded two-role `models` block if it clears §4 and the per-call cost delta is acceptable. |
| 6 | **`calibration_feedback`** | Showing the proposer its own hit/miss pattern (`/api/hypothesis-accuracy` grader, `proposer/calibration.py`) plausibly improves **hypothesis calibration** more than raw proposal quality; read-side/free on the cost meter. Same caveat as rank 2: its docstring also flags it as **widening the proposer-visibility channel** and NOT scaffold-set, so its flip bar carries the same generalization-gap + placebo condition. **Measured only inside A6 (genealogy + calibration) = −0.0038 then +0.0044. A6 beat A1 alone in run 1 (−0.0038 vs −0.0129) and lost to it in run 2 (+0.0044 vs +0.0244) — a sign flip well inside the floor, i.e. no evidence either way about the calibration contribution.** | Flip on if it clears §4 on either the primary endpoint **or** the calibration-fraction endpoint (§3) at no board-run cost. |
| 7 | **`recombine_merge="llm"`** | Conditional on `recombine`: relaxes disjointness so two *overlapping* rejected fixes can be merged by one aux call. Incremental reach beyond mechanical, at +1 aux call on merge rounds. **Measured: A4 = +0.0195 (run 1's largest arm) then +0.0098 (fifth). The reshuffle is the evidence that the run-1 lead was noise.** | Flip `recombine_merge` → `"llm"` only if the llm arm beats the mechanical arm (§4), given `recombine` already flipped on. |
| 8 | **`process_exemplars`** | Highest-risk: it **widens the proposer-visibility channel** (OVERFITTING.md §11), so its default-flip bar is not just proposal quality but a **clean generalization-gap + placebo record** (dev-guide §12). Opt-in-deliberate under the PROCESS-EXEMPLARS.md §5 harm runbook; NOT scaffold-set. **Measured: PEXEMPLAR = −0.0136 then +0.0047.** | Ranked last for a default flip; evaluated as a **pre-registered extension arm** (§4), never graduated on proposal quality alone. |

## 2. The arm matrix — treatments plus two mandatory controls

Eight generator knobs would be 2⁸ = 256 full-factorial cells. That is
infeasible on a live target and mostly wasted — the knobs are not
independent (recombine's merge mode is meaningless without recombine;
genealogy and calibration are both read-side in-context channels). The
campaign runs a **justified fractional design**: one baseline, single-knob arms
isolating each required lever, two principled combinations probing the two
natural knob *families* (read-side in-context vs. board-run mechanical), a
`best_of_n=1` ablation, the `process_exemplars` extension arm — **and two
controls that are not optional.**

### 2.1 The two mandatory controls (run IN THE SAME BATCH)

R.1 is the whole argument for this subsection: a campaign without them cannot
tell a dead instrument from a null. Both controls are **arms of the same
batch**, not a separate validation exercise — they must be measured on the same
endpoint, in the same wall-clock window, against the same baseline.

- **The A/A duplicate (specificity control).** A second arm whose contract is
  **identical to BASE by construction**. Its contrast against BASE is the
  empirical null on exactly the scale being tested, and it is the source of the
  derived floor (§3.2). Passing means the instrument does not manufacture
  differences.
- **The planted-defect arm (sensitivity control).** A deliberately crippled
  generation — the arm whose seed scores ~0 and which the loop must repair.
  Passing means the instrument can see a difference that is genuinely there.
  **Measured at 2.9× and 3.8× the floor** (R.3).

**A run whose planted-defect arm does not clear the floor is void, whatever
else it reports.** A run whose A/A duplicate clears the floor is void for the
opposite reason. Neither verdict is negotiable after seeing the treatment arms.

### 2.2 The planted-defect check is ALSO a pre-gate, not only an arm

The single most expensive lesson in R.1: **a positive control discovered *post
hoc* costs a full campaign.** As an arm, the planted defect only tells you the
instrument was alive *after* the money is spent. So it runs **twice**, and the
cheap half runs first:

1. **A static wiring assertion** — does the challenger's code actually come
   from the challenger's snapshot? This is now structural in the tree: the
   `harness_loaded` round-log event records the snapshot-relative
   `entrypoint_file` plus `trees_verified` / `trees_never_imported` per
   generation (`epoch/round_log.py`), and the loop-health check turns a
   never-imported mutable tree into a finding. **This caught #110 in
   milliseconds.**
2. **A short live probe** — a handful of rounds on the planted-defect arm only,
   read for the one question "does the crippled arm score *worse*?".
   **This caught #110 in ~30 minutes.**

Both are §6.1 preconditions. **Neither is an acceptable substitute for the
planted-defect arm in the batch** — they gate the spend; the arm calibrates the
ruler.

### 2.3 Shared campaign controls (identical on every arm)

- **Target:** a **live** proposer is mandatory — the generator arsenal
  (`best_of_n>1`, `screen`, `genealogy`, `recombine`, roles) only does anything
  with a real model sampling the slate; `target_0`'s scripted proposer would
  leave every arm byte-identical. `target_0_convergence` is used only as the
  **deterministic instrument dry-run** (§6.2), never as a measurement arm. The
  two executed runs used a **single-turn board of 5 entries** at
  `promote_margin` **0.20** (R.2); `target_1_presentation` (the v0 dogfood —
  DOGFOOD-TARGETS.md §1) remains the planned target for a multi-turn campaign,
  whose cost math is §5.
- **Tournament structure:** `gauntlet`, with `field_size` pinned to **1**. The
  gauntlet's `GauntletStrategy.field_size()` hard-returns `1`
  (`selection/strategies/gauntlet.py`), but `estimate_cost` defaults an unset
  `field_size` to `2`; pinning `field_size: 1` in the shared control makes the
  a-priori cost meter (§5) read the *true* runtime board-run count instead of
  double-counting a challenger the gauntlet never runs. The evidence gate is
  deliberately **off**: its honest cost would dominate the meter and it is a
  *soundness* device, orthogonal to the *proposal-generation* questions under
  test (dev-guide invariant #10).
- **Replication is bought in CELLS, not in rounds or replicates.** §3.4 is the
  argument. Note also that the `replicates` lever was **broken two ways**
  (#108 replicate averaging took replicate 0; #109 `--mode fast` ignored
  `replicates`) throughout the pre-fix era, so the original design's
  variance-reduction plan did not exist. Both are fixed; neither is the sizing
  lever.
- **Overfitting controls:** `rotate_holdout: false` and an explicit
  `holdout_fraction` set **identically** on every arm, so all arms (each a
  distinct epoch) see the **same** train/holdout split of the fixed board —
  removing the cross-epoch holdout-rotation confounder (§3.5). Everything else
  in `OverfittingConfig` stays default-on. On a 7-entry board the fraction must
  be **0.6, NOT the 0.3 default**, and this is load-bearing: the hash-based
  split at 0.3 puts **zero** of that board's ids into the holdout (verified by
  running `split_board`), which would leave the generalization-gap and
  holdout-confirm metrics silently inert for the whole campaign. At 0.6 the
  split is **train = 5, holdout = 2**.
- **The board must carry ≥ 6 entries** — or the split never happens at all.
  `min_board_size_for_split` defaults to **6** (`core/scoring_config.py`) and
  `split_board` returns an empty holdout below it (`board/split.py`), silently
  and by design. **The two executed runs used a 5-entry board and were
  therefore running with the entire holdout machinery inert** (R.6). Any future
  run either uses a board of **≥ 6 entries** (the 7-entry `target_1` shape at
  `holdout_fraction` 0.6 gives train 5 / holdout 2) **or** sets an explicit,
  justified `min_board_size_for_split` override and records the justification.
  **The §7 holdout fields — `holdout_confirms`, `holdout_rejects` — are only
  meaningful when the split is live**; on an inert split they are structurally
  0 and must be reported as *not applicable*, never as a clean record.
- **The placebo arm is ON: `random_baseline_every_n: 3`.** It defaults to `0`
  (OFF), and leaving it there would pre-register two rules that can never fire:
  §4's `process_exemplars` bar requires "its placebo arm never promotes", and
  §6.4 abort trigger 1 fires on `placebo_promoted` CRITICAL. A gate-integrity
  check that is switched off does not pass — it is absent. Cost: one extra
  placebo duel every third round, which rides the §5 board-run meter like any
  other duel. **The planted-defect ARM and the placebo arm are DIFFERENT
  controls and both are mandatory:** the planted defect asks *can the
  instrument see a real effect* (sensitivity, §2.1); the placebo asks *does the
  decision procedure reject a candidate that changed nothing* (gate integrity).
  Passing one says nothing about the other.
- **Seed v0:** every treatment arm starts from the **same** registered champion,
  so the headroom to the floor is identical at round 0. The planted-defect arm
  deliberately does not.
- **Round + seed budget:** **3 rounds per cell**, **K = 12 paired seeds per
  arm** — the design both valid runs used (R.2). §3.4 is why the seeds and not
  the rounds carry the power.

Each arm's `scoring.json` delta is written verbatim below. Recall the
on-disk key rename: `ScoringWeights.tournament_structure` serialises under
`"tournament"` (`epoch/contract_serde.py`); `proposer_quality` keeps its
name. Only the fields that differ from the shared control are shown; the
scaffold serializer writes every field, so the live file is the full
effective contract.

**The shared control block every arm carries:**

```json
{
  "tournament": {
    "structure": "gauntlet",
    "params": { "field_size": 1 }
  },
  "overfitting": {
    "rotate_holdout": false,
    "holdout_fraction": 0.6,
    "random_baseline_every_n": 3
  }
}
```

### Arm A0 — BASE (the shipped in-code default proposer)

The reference cell: `best_of_n=3` + critique on, **every** generator-arsenal
knob off. This is what an operator gets from the in-code defaults with
`screen_entries` forced back to 0 (so BASE isolates the *proposer defaults*
from the *scaffold's* screen choice, which arm A2 tests).

```json
{ "proposer_quality": { "best_of_n": 3, "critique_enabled": true } }
```

### Arm AA — A/A DUPLICATE (specificity control, §2.1)

**Byte-identical to A0's contract**, run as a separate arm with its own K seeds.
It is not a copy of A0's *results*; it is an independent draw from the same
configuration. Its contrast with A0 **is** the floor (§3.2).

```json
{ "proposer_quality": { "best_of_n": 3, "critique_enabled": true } }
```

### Arm PD — PLANTED DEFECT (sensitivity control, §2.1)

A0's contract, seeded from a **deliberately crippled generation** (scoring ~0 at
v0). Not a knob arm: it measures whether the loop can see and repair a maximal
defect. Its contrast with BASE is the sensitivity gate (§3.3).

```json
{ "proposer_quality": { "best_of_n": 3, "critique_enabled": true } }
```

### Arm A1 — +GENEALOGY

```json
{ "proposer_quality": { "best_of_n": 3, "critique_enabled": true,
                        "genealogy": 4 } }
```

### Arm A2 — +SCREEN (the scaffold's current choice, isolated)

```json
{ "proposer_quality": { "best_of_n": 3, "critique_enabled": true,
                        "screen_entries": 2, "screen_veto_only": false } }
```

### Arm A3 — +RECOMBINE (mechanical)

```json
{ "proposer_quality": { "best_of_n": 3, "critique_enabled": true,
                        "recombine": true, "recombine_merge": "mechanical" } }
```

### Arm A4 — +RECOMBINE (llm merge) — requires `recombine` on

```json
{ "proposer_quality": { "best_of_n": 3, "critique_enabled": true,
                        "recombine": true, "recombine_merge": "llm" } }
```

### Arm A5 — +ROLES (breadth/depth ensemble)

`scoring.json` is **identical to A0** (roles are not a contract field). The
delta is written into the workspace `config.json` `models` block (there is no
separate `models.json` file) — it binds the two propose call-classes to
distinct model roles:

```json
{
  "models": {
    "proposer_breadth": { "model": "<exploratory-role model string>" },
    "proposer_depth":   { "model": "<refinement-role model string>" }
  }
}
```

Run in its own workspace (§0 caveat); the model strings name the operator's
two endpoints for the breadth/depth roles (no vendor named here — the
operator fills them at go-ahead).

### Arm A6 — COMBO-R: read-side in-context stack (genealogy + calibration)

Probes whether the two **read-side, cost-meter-untouched** in-context
channels compound (both widen the proposer's *context*, neither its board
visibility of holdout data):

```json
{ "proposer_quality": { "best_of_n": 3, "critique_enabled": true,
                        "genealogy": 4, "calibration_feedback": 4 } }
```

### Arm A7 — COMBO-M: mechanical stack (screen + recombine)

Probes whether the two **slate-mechanics** knobs compound (screen vetoes
catastrophic slate members; recombine mints a union from rejected reign
challengers). Both are evaluation/slate-side, neither widens
proposer-visibility:

```json
{ "proposer_quality": { "best_of_n": 3, "critique_enabled": true,
                        "screen_entries": 2, "screen_veto_only": false,
                        "recombine": true, "recombine_merge": "mechanical" } }
```

### Arm ABLATION — `best_of_n = 1` (the arsenal turned FURTHER off)

```json
{ "proposer_quality": { "best_of_n": 1, "critique_enabled": false } }
```

Not a knob arm and not a control — a **direction check**. It was the highest
arm in run 2 (R.4), which is the single most important thing the campaign
found about the arsenal's direction of effect.

### Arm PEXEMPLAR — `process_exemplars` (extension arm)

```json
{ "proposer_quality": { "best_of_n": 3, "critique_enabled": true,
                        "process_exemplars": 4 } }
```

**Coverage check.** Every required knob appears at least once as the *sole*
delta from BASE (A1 genealogy, A2 screen, A3 recombine-mech, A4
recombine-llm, A5 roles) — so each single-knob effect is identified — and
`calibration_feedback` + the two families appear in the two combos. `best_of_n`
is validated by BASE against the ABLATION arm. `process_exemplars` is the
extension arm, off the main matrix by design. **AA and PD are not coverage —
they are the ruler and the alive-check.** Ten treatment/reference arms + 2
controls = the **12-arm** design both valid runs executed.

## 3. Measurement plan

### 3.1 The primary endpoint, and the unit of analysis

**E1 (primary) — per-duel challenger improvement**

```
d = loss(champion) − loss(challenger)          # one duel
E1(cell) = mean of d over the cell's duels     # the CELL MEAN
E1(arm)  = mean of E1(cell) over the arm's K cells
```

Two changes from this document's original design, both forced by measurement:

**(a) The endpoint is `d`, not the final champion scalar and not the promotion
outcome.** Promotion is a *thresholded* read of a quantity noisier than the
effect: it yields **~4 observations per arm where the continuous read yields
~24 for the same compute** (**measured**). The final champion scalar has the
same problem one level up — it is a single number per run. `d` is the finest
continuous read the loop emits, and every duel produces one.

Sourcing: `GateEvaluated` now records `champion_scalar` / `challenger_scalar` /
`margin_required` on **both** decisions (`epoch/round_log.py`), so `d` is
reconstructable from the round log alone. Before issue **#111** those numbers
survived only inside the human-readable REJECT text — meaning a sample
recovered from the log was **missing exactly its promotions**, which are by
definition the largest improvements. That gap was *correlated with the quantity
being measured*; any configuration comparison built on it was biased toward
whichever arm promoted least.

**(b) The CELL is the unit of analysis, not the duel.** Duels within a cell
share an evolving champion, so they are **correlated**. An earlier run treated
them as independent, which **understated every standard error by about 2×** and
**turned a failing sensitivity gate into a passing one** (**measured**). Cluster
on the cell: reduce each cell to its mean `d`, then do all inference on the K
cell means. Nothing downstream of this section ever consumes a duel-level SE.

**Secondary endpoints** (context only, never a decision):

| # | Metric | Source (verified in tree) |
|---|---|---|
| E2 | **Promotion count / rate** = promoted / challengers | `tournament/detail.py::optimization_trajectory(db_path, epoch_id)` → `Trajectory.promotion_rate` |
| E3 | **cost_per_promotion** (wall-clock) = total_runtime_ms / promoted | `tournament/detail.py::tournament_cost` → `cost_per_promotion_ms` |
| E4 | **Board-runs cost** (a priori, deterministic) | `builder/operations.py::estimate_cost` → `board_runs_per_round`; the campaign's *primary cost unit* (§5) because it is exact given the structure, unlike wall-clock |
| E5 | **Gate margin vs derived floor** per promotion | `RoundRecord` fold: `GateEvaluated` (`champion_scalar`, `challenger_scalar`, `margin_required`) vs the derived floor; the `margin_below_noise_floor` health finding (dev-guide §4) is the guardrail |
| E6 | **Hypothesis-calibration fraction** (predicted Δ vs measured Δ) | `tournament/detail.py::hypothesis_ledger` / `/api/hypothesis-accuracy`; `proposer/calibration.py`; PUBLICATION §6 |
| E7 | **BT / Elo ratings** ± SE at crowning | the `elo` / `elo_games` columns (ANALYTICAL-INDEX schema **v10**) and `elo_se` (schema **v12**); `selection/rating.py`, PUBLICATION §4 |
| E8 | **Statistical-integrity record** (placebo, ladder budget, screen veto/confirm, generalization gap) | `RoundRecord` fold — `CandidateScreened`, `HoldoutReleased`, `decision_provenance`; `health/diagnostics.py` |

E2 in particular is **not** a decision endpoint on this target: across both
valid runs, **only the planted-defect arm ever promoted at all** (R.4). A
promotion count of zero for every treatment arm carries no ranking information.

### 3.2 The floor is DERIVED, never asserted

```
floor = 2 × SE( mean d[A/A clone] − mean d[BASE] )
```

where the SE is computed on **cell means** (§3.1b). The A/A arm is two
configurations **identical by construction**, so the spread of that difference
is the empirical null **on exactly the scale being tested**. Anything smaller is
indistinguishable from running BASE twice.

**Measured: 0.0378 (run 1), 0.0400 (run 2).** The two agree to within their own
resolution, across a fix wave that changed the gate arithmetic — which is the
evidence that **the noise is a property of the target, not of the instrument**
(R.4).

This replaces the original design's asserted floor. The rule generalises:

> **A campaign may not state its own resolution. It must measure it, in the same
> batch, on the same endpoint, from a contrast that is null by construction.**

An A/A contrast borrowed from another run, another board, or another epoch is
not this quantity. Neither is `noise_floor.max_abs_delta` from
`zicato board preflight` — that is a *board-entry* A/A floor (`epoch/preflight.py`,
persisted by `epoch/lifecycle.set_epoch_noise_floor`) and it remains the right
instrument for the pre-flight go/no-go and for the per-entry MDE ladder below,
but it is not the arm-contrast scale the decision rules gate on. **Two different
floors, two different jobs; never substitute one for the other.**

### 3.3 The two validity gates, pre-registered

Both are computed on the same cell-level scale as everything else, and both are
read **before** any treatment arm is looked at:

```
sensitivity:  mean d[planted-defect] − mean d[BASE]  must exceed the floor by a
              stated multiple, with its 90% CI excluding the floor
specificity:  mean d[A/A clone]      − mean d[BASE]  must sit INSIDE the floor,
              with its 90% CI containing 0
```

**Measured** (R.3): sensitivity +0.1086 (2.9× floor) then +0.1526 (3.8× floor),
both PASS; specificity −0.0144 then +0.0013, both PASS.

**Failing either gate voids the run for ranking.** R.1 records what happens when
this rule is applied to a run that fails it: the run is not rescored on a
renegotiated threshold. The threshold is fixed in advance precisely so that it
cannot be.

### 3.4 Sizing: cells buy power, rounds do not

**The finding that drives all sizing (measured): between-cell variance dominates
within-cell variance by roughly 4×.** Consequences, and they are not
negotiable:

- **Extra rounds per cell buy very little.** Averaging more duels inside a cell
  shrinks the smaller variance component.
- **Extra seeds (cells) buy the power.** They shrink the dominant one.
- **`replicates` is not the lever either** — and for the whole pre-fix era it
  was broken two ways (#108, #109), so the original design's variance-reduction
  plan never existed even in principle.

**The measured noise scales.** Intrinsic run-to-run sd is **~0.19** at
temperature 0 — a multi-agent pipeline diverges regardless — against an
achievable per-round improvement of **~0.10**. **Duel-level reads are
noise-dominated**; only the cell-mean aggregate is usable. Per-duel sd for
non-degraded arms is **~0.12** (**measured**).

**What is answerable, at what price** (**measured**, from the ~0.12 per-duel sd):

| arsenal effect size | duels/arm | cells/arm | 8 arms |
|---|---|---|---|
| **0.05** | ~33 | **~6** | ~48 cells, ~15 h |
| **0.02** | ~208 | ~35 | **infeasible** |

(The ~15 h is anchored on the 6-rounds-per-cell run; §5 explains why that rate
does not transfer to a 3-round design without re-anchoring. **The "8 arms"
column is the ORIGINAL 8-arm screening design's count, not §2's matrix** — the
executed matrix is **12** arms (9 treatments + BASE + 2 controls), so the same
0.05 sizing against §2 is 12 × ~6 ≈ **72** cells, not 48. Re-multiply for
whatever matrix you actually run.)

> **The campaign is tractable iff arsenal features move proposal quality by
> ≳ 0.05 on the `d` scale.** Below that, this target cannot resolve them at any
> affordable cost, and the honest move is to **change the target or the
> question** — not to buy more seeds.

The executed runs went to **K=12** and achieved a derived floor of **0.040**
(**measured**, run 2). Scaling that single anchor by `floor ∝ 1/√K`
(**derived**, and every figure below recomputes from `0.0400 · √(12/K)`):
K=6 → ≈0.057, K=32 → ≈0.025, and the **~0.013** affordability wall the operator
names would need **≈114 cells/arm**. Run 1's floor of 0.0378 gives the same
curve one notch tighter — K=32 → ≈0.023, which is the operator's own sizing
figure; **quote the anchor with the number**, because the two differ by more
than the third decimal the sizing table is read to.

**Why the original K=6 screen is withdrawn.** The old design justified K=6 by
assuming the cross-run sd of the endpoint was *on the order of the derived
floor*. **The measured between-cell variance does not support that assumption**,
so the "K=6 resolves ~1.5-floor effects" claim is not a property of this target.
K=6 remains a defensible *screen* size for a ~0.055 floor (the table above),
but it is now justified by the **measured** sd, not by the assumption — and it
graduates arms, it never flips a default.

**The two-sample MDE arithmetic itself is retained and unchanged**, because it
is arithmetic and other surfaces pin it. For a two-sample comparison at
α=.05 / power .80, with `n` per arm and `df = 2·(n−1)`:

```
MDE = (t_{α/2,df} + t_{β,df}) · sd · √(2/n)
```

At **n=6, df=10** this is **≈ 1.79·sd** (α=.05) and **≈ 1.55·sd** (α=.10), and
the non-overlap of two 90% t-CIs needs an observed gap **> ≈ 1.48·sd**. These
are the numbers `docs/design/EVAL-VIEW.md` §4.3 and
`src/zicato/query/eval_view.py`'s live MDE ladder cite as "the numbers
CAMPAIGN.md §3 pins", and they are unchanged. **What changed is the substitution
`sd ≈ floor`:** for the live MDE ladder's per-entry job that substitution stands
(it uses the board-entry A/A floor, §3.2); for *this campaign's arm contrasts*
it does not, and sizing here uses the measured between-cell sd instead.

### 3.5 The honest confounders

- **Clustering (the one that actually bit).** Duels within a cell are not
  independent — §3.1b. Treating them as independent understated SEs ~2× and
  flipped a gate. **Mitigated** by making the cell the unit of analysis.
- **Instrument death (the one that cost six runs).** #110 — see R.1.
  **Mitigated** by §2.1's planted-defect arm and §2.2's pre-gate, plus the
  structural `harness_loaded` provenance in the round log.
- **Silent truncation (the one that cost two "successful" runs).** Credential
  lapse mid-run producing partial cells that pass a liveness check — R.5.
  **Mitigated** by §6.6's round-completeness check.
- **Target-difficulty drift across epochs.** Each arm is a distinct epoch;
  naively the holdout slice would *rotate* by epoch id (`rotate_holdout`
  default `True`, `board/split.py` `rotation_seed`). **Mitigated** by pinning
  `rotate_holdout: false` on every arm (§2.3). The residual drift is that a
  promoted champion changes the *remaining headroom*, so per-round promotion
  rate is **not exchangeable** across arms with different early trajectories —
  another reason E2 is context-only.
- **Model nondeterminism.** No controllable seed on a live endpoint. Unlike the
  deterministic power harness (`stable_noise_seed` from
  `(workspace_seed, generation_id, entry_id, replicate_index)`, dev-guide
  §13.1), a live endpoint's own sampling variance **is** the noise source. Live
  runs are **not** byte-reproducible; the CIs are genuine sampling CIs over
  model nondeterminism. Measured at ~0.19 run-to-run sd even at temperature 0.
- **Temporal endpoint drift (across-arm).** A hosted model can change version
  mid-campaign; arms measured early and late may be scored against a
  **different underlying endpoint**, a confounder that aliases with the knob
  effect *across the arm axis*. **Mitigated** by running the arms in parallel
  workspaces over the same wall-clock window (§5), so any version shift hits
  every arm together. The A/A control also detects it: a drifting endpoint
  inflates the A/A contrast, which is exactly what the specificity gate reads.
- **Cost variance.** Board runs (E4) are **deterministic** given the structure.
  Wall-clock (E3) varies with endpoint latency and parallelism and is secondary
  color only, with the caveat that a budget-clipped duel biases scalars
  pessimistically for the clipped side (dev-guide §1.6).
- **Multiplicity.** Nine treatments sharing one baseline at an uncorrected 10%
  would expect **roughly one spurious graduate**. **Mitigated** by Holm
  correction across the treatments (§4), pre-registration of the single primary
  endpoint, and a fixed bar.

## 4. Decision rules — pre-registered (written before the data)

For every arm, on the primary endpoint:

```
E1(arm)   = mean over the arm's K CELL MEANS of per-duel d          (§3.1)
ΔE1(arm)  = E1(arm) − E1(BASE)
CI90      = the 90% t-CI on ΔE1, computed on CELL means (K−1 df per arm)
floor     = 2 × SE(E1(A/A clone) − E1(BASE))                        (§3.2)
p_holm    = the Holm-adjusted p across ALL treatment arms           (§3.5)
CPP(arm)  = board-run cost per promotion (E4/E2), context only
```

**Rule 0 — the validity gates come first, and they are read blind.** Compute
the §3.3 sensitivity and specificity contrasts and the derived floor **before
looking at any treatment arm**. Either gate failing ⇒ **the run is void for
ranking**; report it as void, do not rescore it against a renegotiated
threshold. R.1 is the precedent.

**Rule 1 — thresholds live on the scale of the quantity they gate.** Every
threshold below is stated in units of `d` (or as a multiple of the derived
floor, which is also in units of `d`). **A threshold expressed on a different
scale than the statistic it gates is a defect, not a conservative choice** — it
is how a per-run sd came to gate a difference of arm means (R.1), and the
resulting failure was uninterpretable rather than strict.

**Rule 2 — the authority rule.** A screening read **GRADUATES** an arm to a
confirmatory run; it never flips a scaffold default. "Recommend" is the
strongest verdict a screen can return. Every "flip" in §1's table names the
action a **confirmatory** read would authorize.

**The graduation bar — ALL THREE must hold:**

1. **Above the ruler:** `ΔE1(arm) ≥ floor`, with the 90% CI on `ΔE1` excluding
   0.
2. **Survives multiplicity:** `p_holm < 0.10` across the treatment arms.
3. **Does not cost more than it delivers:** `CPP(arm) ≤ 1.10 · CPP(BASE)`. A
   read-side knob (`genealogy`, `calibration_feedback`, roles) satisfies this
   trivially — the cost meter is untouched — so its graduation reduces to rules
   1–2. **On a target where only the planted-defect arm promotes, CPP is
   undefined for every treatment arm and this rule is reported as
   `not applicable`, never silently passed.**

**Does not graduate** iff any of the three fails.

**"Inconclusive" is a specific claim and must be reported as one:** *no effect
larger than the floor* — **never** *no effect*. Run 1 called all nine treatments
inconclusive in exactly this sense; run 2's nine likewise all fail the bar, the
closest (the ablation at +0.0392, CI excluding 0) failing on rule 1 by sitting
just **under** the derived floor of 0.0400 and on rule 2 at Holm p 0.537. A
report that writes "no effect" where the data says "nothing above 0.040" has
overclaimed.

**Per-knob specialization (each states the graduation trigger and the flip
direction a confirmatory read would then authorize):**

- **`recombine` (A3):** graduates on the bar; a confirmatory read then flips it
  to default-`True`. A3 is cost-neutral (the mint replaces a propose call —
  `estimate_cost` charges it as `best_of_n − 1` propose calls, no extra board
  run), so rule 3 is automatic. The mechanistic confirmation the oracle predicts
  (dev-guide §1.8) is E2 — promotions A3 caught that BASE did not — which on
  this target was **zero for both**, so the oracle's mechanism is unobservable
  here rather than refuted.
- **`recombine_merge="llm"` (A4):** evaluated **relative to A3**, not BASE.
  Graduates iff `E1(A4) − E1(A3) ≥ floor` with the CI excluding 0 and the
  +1-aux-merge-call cost keeping `CPP(A4) ≤ 1.10·CPP(A3)`. If A3 itself does
  not graduate, A4 is moot. **The A4-vs-A3 contrast is bundled:** `"llm"` merge
  changes both the merge *method* (one aux merge call vs. mechanical
  concatenation) **and** the candidate-pair eligibility — it reaches
  OVERLAPPING rejected pairs the mechanical mint's disjointness predicate
  rejects (`proposer/best_of_n.py` §2.6.1). The rule reads the *bundle*.
- **`screen_entries` (A2) — REVERSED NULL.** The scaffold *currently* writes
  `screen_entries=2`, so the pre-registered action is to **keep** it only if A2
  clears the bar over BASE. If A2 fails the bar (its board-run premium, §5, is
  not repaid in E1), the pre-registered action is to **remove `screen_entries`
  from `recommended_scaffold_weights()`** — scaffold default → `0`. (The
  in-code default is already `0`; this only touches the scaffold.) **A2 failed
  the bar in both runs**; that action is now live and unexecuted.
- **`genealogy` (A1), `calibration_feedback` (A6-contribution), roles (A5):**
  read-side / cost-neutral → rules 1–2 alone **for roles**. `genealogy` and
  `calibration_feedback` are cost-neutral but **not** risk-neutral: both
  docstrings flag them, like `process_exemplars`, as widening the
  proposer-visibility channel, so neither is scaffold-set today and neither
  graduates on E1 alone. Both carry the extension-arm conditions —
  `generalization_gap` quiet **and** the placebo arm never promoting — because
  a scaffold flip is exactly the irreversible step those conditions guard.
  `calibration_feedback` may flip on
  either E1 **or** a `≥ +0.10` absolute lift in the hypothesis-calibration
  fraction E6 (its designed effect is honesty, not raw proposal quality — §1
  rank 6), provided E1 does not regress below BASE's lower CI.
- **`best_of_n` (BASE vs ABLATION):** keep the `best_of_n=3` default iff
  `E1(BASE) − E1(ABLATION) ≥ floor`. Otherwise flag `best_of_n=3`'s aux-call
  cost as **unearned** and revert the recommendation toward `1`. **Measured: the
  ablation was the highest arm in run 2 (+0.0392 vs BASE), so this test's
  precondition is not merely unmet — it points the other way, within the
  floor.**
- **`process_exemplars` — extension arm, higher bar.** Graduates **only if** it
  clears the E1 bar **AND** its `generalization_gap` detector stays quiet **AND**
  its placebo arm never promotes (dev-guide §12 boundary rules). A
  proposal-quality lift bought with a widening generalization gap is a *reject*,
  not a win — the whole point of ranking it last.

**Combos (A6, A7):** report interaction as
`ΔE1(combo) − [ΔE1(single_1) + ΔE1(single_2)]`. A positive interaction beyond
the CI is evidence the family compounds (scaffold both); a negative interaction
beyond the CI is evidence they interfere (scaffold at most one). Combos never
override a single-knob decision.

## 5. Cost and wall-clock — the measured model

**The original estimate in this document was wrong by roughly an order of
magnitude, and it was wrong in the optimistic direction.** It projected ≈ 8,928
board runs at **≈ 2.5 h** of parallel board-run wall-clock. Against measurement:

| | original estimate | measured |
|---|---|---|
| a 16-cell × 6-round run | — | **4 h 56 m** |
| a 144-cell × 3-round run | — | **7 h 23 m** |
| the 8-arm screening design | ≈ 2.5 h parallel board-run wall-clock | **several times a 16-cell run again** |

**Use the measured figures for every future budget.** The failure mode of the
original was assuming ≈0.8 s per model call and ≈5 serial-ish calls per board
run, then dividing by parallelism — a chain of planning assumptions with no
measured anchor. The two anchors now available are the two runs above; a new
campaign's estimate should be **interpolated from them**, in cells × rounds, and
labelled **planning** until it too is measured.

**Sizing tables (measured, §3.4) — this replaces the K=6-screen premise:**

| arsenal effect size on `d` | cells/arm | whole batch | verdict |
|---|---|---|---|
| **0.05** | ~6 | 8 arms ⇒ ~48 cells, **~15 h at 6 rounds/cell** | feasible |
| **0.040** | 12 | 12 arms ⇒ **144 cells, 7 h 23 m at 3 rounds/cell** | **what was actually run** |
| **~0.023** | ~32 *(run-1 floor anchor; run 2's 0.0400 puts K=32 at ≈0.025 — §3.4)* | — | affordable only for a **single** arm |
| **0.02** | ~35 | — | **infeasible** |
| **~0.013** | ≈114 *(derived)* | — | **unaffordable at any K this target supports** |

**The two wall-clock anchors do not agree on a per-cell-round rate, and that
matters.** 16 cells × 6 rounds in 4 h 56 m is ≈3.1 min per cell-round; 144 cells
× 3 rounds in 7 h 23 m is ≈1.0 min per cell-round. Different board, different
concurrency, different endpoint latency. **Do not extrapolate wall-clock from
one anchor as though the rate were a constant** — that is a smaller version of
exactly the error the original ≈2.5 h estimate made. Quote the anchor you are
interpolating from, and label the result **planning**.

**Cost-meter semantics (grounded in `builder/operations.py::estimate_cost`).**
The meter reports **board runs per round** (each = one agent execution on one
board entry). Auxiliary model calls (`best-of-N propose calls`) are labelled and
**excluded from the board-run headline** but are real spend. The meter is exact
given the structure — it is the campaign's primary *cost* unit precisely because
wall-clock is not. For the **planning** 7-entry `target_1` shape — `field_size`
pinned to 1 **and `replicates: 2` explicitly set** (the shared-control block in
§2.3 leaves `replicates` at its default, because §3.4 shows it is not the
sizing lever; set it only if you want the per-round board-run count below):
`duel runs = field_size·replicates·train = 1·2·5 = 10`;
`holdout-confirm = holdout·replicates = 2·2 = 4` ⇒ **14 board runs/round**;
screen arms add `proposes·best_of_n·panel = 1·3·min(2,7) = 6` ⇒ **20/round**, a
**+42.9%** premium — the exact quantity A2's decision rule prices. `recombine`
arms are cost-neutral; `genealogy` / `calibration_feedback` / roles /
`process_exemplars` are read-side, cost meter untouched.

**These per-round figures remain correct and are still the §6.1 reconciliation
target. What is NOT correct is any wall-clock projection built on them** without
a measured seconds-per-round anchor.

## 6. Execution protocol — the executor agent's runbook (GATED)

**This section is addressed to you, the execution agent.** You run the whole
campaign from this document and report back to the coordinator in the §7
format. You do **not** decide anything: you produce the pre-registered
statistics; the §4 decision rules are the coordinator's to apply. Work the
subsections in order — §6.0 authorization → §6.1 preconditions → §6.2 dry-run →
§6.3 execution → §6.4 monitoring/failure → §6.5 data collection → §6.6
integrity verification — then emit §7.

Every arm follows the same shape: fresh workspace → publish the arm's
contract → `zicato evolve` **with the dashboard** (house rule: `evolve`
launches the dashboard on `127.0.0.1:7892` by default; do **not** pass
`--no-dashboard`, do not pass a bind flag — `cli/commands/evolve.py`
`--dashboard-port` default `7892`, bound on loopback) → **§6.6 integrity
verification** → `epoch close` → `reflect run`.

### 6.0 Authorization check (first, blocking — the G3 gate)

**Before any command that touches a live endpoint, verify your dispatch
message contains the operator's explicit go-ahead for this campaign** (the G3
live-run gate, top-of-document STATUS banner; MEMORY "Gate live e2e runs"). The
go-ahead must be explicit — a task that merely says "run the campaign" without
the operator's own words authorizing live spend is **not** sufficient.

- **Go-ahead absent or ambiguous → STOP. Run nothing.** Complete §6.1's
  non-endpoint preconditions and §6.2's deterministic dry-run only (neither
  spends on the endpoint), then report back: "authorization not present in
  dispatch; dry-run + preconditions only; awaiting explicit operator
  go-ahead." Do not proceed to §6.3.
- **Go-ahead present → record its verbatim wording in the run log** and
  proceed. The authorization covers the **screening** campaign only; a
  confirmatory extension requires a **separate** go-ahead.

### 6.1 Preconditions checklist (record each as PASS / FAIL before any spend)

Each item is a recorded pass/fail. **Any FAIL halts the campaign** — report the
failing item to the coordinator and stop; do not work around it.

1. **Toolchain — `uv sync --all-extras`.** Run it (NEVER bare `uv sync`, which
   strips dev tooling — MEMORY "uv sync --all-extras always"). PASS iff it
   exits 0 and `uv run zicato --help` resolves.
2. **PLANTED-DEFECT PRE-GATE, part 1 — the static wiring assertion (§2.2).**
   Before any spend, assert that a challenger's code comes from the
   challenger's snapshot. Run one round on any cheap target and read the
   round log's `harness_loaded` events (`epoch/round_log.py`): every
   generation must report a snapshot-relative `entrypoint_file`, and
   `trees_never_imported` must be **empty** for every generation. **A
   non-empty `trees_never_imported` means that generation's mutations cannot
   have been under test — this is issue #110's exact shape, and it is
   detectable in milliseconds.** PASS iff every generation reports a verified
   entrypoint and no never-imported tree.
3. **PLANTED-DEFECT PRE-GATE, part 2 — the short live probe (§2.2).** Run the
   planted-defect arm ONLY, for a handful of rounds, and read one question:
   **does the crippled arm score worse than baseline?** It must. **This is the
   ~30-minute version of the check that a post-hoc positive control costs a
   full campaign to learn.** PASS iff the crippled arm's champion score is
   below baseline and its per-duel `d` contrast is positive (the arm has
   headroom to repair). A crippled arm scoring *best* is the #110 signature —
   **halt.**
4. **Auxiliary + harness endpoints configured and responding.**
   `zicato board preflight` with the minimum draw count, on the first
   workspace immediately after `epoch new` (§6.3):
   ```bash
   zicato board preflight --workspace .zicato --runs 2 \
       --harness-call-llm   <operator harness endpoint dotted path> \
       --auxiliary-call-llm <operator auxiliary endpoint dotted path>
   ```
   `board preflight` requires **both** dotted callables (`cli/commands/board.py`
   `preflight_cmd`, both `required=True`) and takes as few as **2** A/A draws
   (`--runs` `IntRange(min=2)`; default 5 = `DEFAULT_CALIBRATION_RUNS`). It is
   cache-idempotent with `zicato board audit`, so it is the cheapest genuine
   endpoint probe in the tree. Its A/A measurement is the **board-entry** noise
   floor (`epoch/preflight.py` `noise_floor_max_abs_delta`, persisted via
   `epoch/lifecycle.set_epoch_noise_floor`) — **not** the campaign's derived
   arm-contrast floor, which comes from the A/A arm (§3.2). PASS iff it returns
   a verdict rather than an import/connection error. The vocabulary is
   **OK / WARN / INERT / REFUSE** (`epoch/preflight.py`). A `REFUSE` verdict is
   a §6.4 abort condition, not a precondition failure. **`INERT` is WARN-class
   here:** it means the A/A draws did not vary at all, i.e. the floor is
   *unmeasured*, not that the contract failed — proceed, and record the note on
   the run, because an unmeasured board-entry floor makes the §6.4 E5 margin
   guardrail inert for that arm too.
   **Note the honest limit: #106 (probes only the first mutation point) and
   #112 (never checks margin achievability) meant pre-flight would have passed
   every invalid pre-fix run. Both are fixed; neither makes pre-flight a
   substitute for items 2 and 3.**
5. **Disk headroom.** Confirm free space on the volume holding
   `campaign-<ts>/` is comfortably above the run footprint. PASS iff
   `df -h <campaign root>` shows headroom > 3× the projected footprint.
6. **The §6.2 deterministic dry-run is green.** PASS iff the dry-run converges
   (v0 3.6 → v3 1.2) and `epoch close` emits `analysis.md`.
7. **Cost-meter reconciliation.** Run the cost meter
   (`builder/operations.py::estimate_cost`) on each arm's `scoring.json` and
   confirm it reads the **§5 numbers** for the chosen board shape (on the
   7-entry `target_1` shape: **14** board-runs/round for the non-screen arms,
   **20** for the screen arms). PASS iff the meter reads them exactly. **A
   mismatch means a contract or `field_size` pin is wrong — halt before
   spending.**
8. **Both controls are IN the arm matrix.** Confirm the batch contains an A/A
   duplicate arm and a planted-defect arm with the same K as every treatment
   arm (§2.1). PASS iff both are present. **A batch missing either cannot
   produce a valid result and must not be started** — R.1.

### 6.2 Instrument dry-run (deterministic, zero endpoint — not gated)

Before any live arm, prove the harness/cost-meter/analytics chain end-to-end
on the **deterministic** `target_0` (its scripted proposer needs no endpoint),
exactly as `examples/zicato_examples/target_0_convergence/RUN.md`
§"End-to-end demo". This validates wiring under a known answer (dev-guide §1.8:
v0 3.6 → v3 1.2) with **no** live-run gate, the same way `cascade_oc.py`
validates the statistics offline before any real spend. This is precondition
§6.1 item 6; it runs regardless of the §6.0 authorization outcome.

### 6.3 Execution recipe

**Workspace layout.** One timestamped campaign root; arms in separate workspace
trees (never share a `.zicato`); one dir per arm×seed:

```
campaign-<ts>/<arm>/k<seed_index>        # e.g. campaign-20260802T0900/A0/k1
campaign-<ts>/results/                    # the §7 artifacts land here
```

`<arm>` ∈ {A0, AA, PD, A1…A7, ABLATION, PEXEMPLAR}; `<seed_index>` ∈ {1…K}.
Each `k<seed_index>` is a **fresh clone** — the live endpoint exposes no seed we
control, so the seed index names an independent replicate run, not a
reproducible seed (§3.5). **Seeds are paired across arms**: seed index `k` means
the same starting condition for every arm, which is what makes the cell-level
contrast a paired comparison.

**Per-run command sequence (one block per arm×seed; A0/k shown):**

```bash
# --- Arm A0 BASE, replicate run k (repeat k = 1..K, fresh dir each) ---
TS=<campaign timestamp>          # one value for the whole campaign
WS=campaign-${TS}/A0/k${k}
rm -rf "$WS" && mkdir -p "$WS" && cd "$WS"
EX=/home/sunil/git/zicato/examples/zicato_examples/target_1_presentation

zicato init --workspace .zicato
# Register the ADK agent by its DOTTED IMPORT PATH + the vetted mutable
# subtree (the example's own RUN.md — evolve resolves the adapter via
# importlib, so this MUST be a module path, not a filesystem path). The
# path is SNAPSHOT-RELATIVE: the snapshot copies "$EX/agent" under its
# basename, so the entrypoint's top-level module is `agent`, not
# `zicato_examples` — the installed-package form would silently run the
# INSTALLED copy and score no-ops (issue #110), and `register` refuses it:
zicato register --workspace .zicato \
    --adk agent.agent:root_agent \
    --mutable-tree "$EX/agent"

# Open the epoch from the example's board / brief / scoring, using THIS
# arm's scoring delta. `epoch new` freezes a per-epoch copy AND publishes
# board.jsonl / brief.md / scoring.json to the canonical location so the
# `evolve` below continues this epoch instead of rolling a fresh one
# (RUN.md "End-to-end loop"). The proposer brief is the example's
# `rubric.md` (there is no `brief.md` in the example):
zicato epoch new campaign_A0_k${k} --workspace .zicato \
    --board   "$EX/board.jsonl" \
    --brief   "$EX/rubric.md" \
    --scoring ./arm_A0.scoring.json      # the §2 A0 contract (full effective form)

# ENDPOINT SMOKE-TEST (§6.1 item 4) — run ONCE, on this first workspace.
zicato board preflight --workspace .zicato --runs 2 \
    --harness-call-llm   <operator harness endpoint dotted path> \
    --auxiliary-call-llm <operator auxiliary endpoint dotted path>

# With the epoch open, inspect the mutation surface + eyeball the cost
# meter BEFORE spending (no run yet):
zicato mutations --workspace .zicato

# GATED: only after §6.0 explicit operator go-ahead -----------------------
zicato evolve --workspace .zicato --rounds 3 \
    --harness-call-llm   <operator harness endpoint dotted path> \
    --auxiliary-call-llm <operator auxiliary endpoint dotted path>
# evolve prints:  Dashboard: http://127.0.0.1:7892   (RECORD this exact URL
# into the run record's dashboard_url; watch the bracket live)

# INTEGRITY VERIFICATION (§6.6) — BEFORE the cell counts as done:
zicato epoch rounds --workspace .zicato --verify

# After the loop settles AND the cell is accepted:
zicato epoch close   --workspace .zicato          # → analysis.md / analysis.html
zicato reflect run   --workspace .zicato          # MSA pass over the eval contract
```

- **A1–A4, A6, A7, ABLATION, PEXEMPLAR** are identical except the `--scoring`
  file is the arm's §2 delta (the `board preflight` smoke-test is NOT repeated).
- **AA (A/A duplicate)** uses A0's contract verbatim, in its own workspace tree.
- **PD (planted defect)** uses A0's contract with the crippled seed generation.
- **A5 (roles)** additionally writes the `models.proposer_breadth` /
  `models.proposer_depth` block into the workspace `config.json` before
  `evolve` (its `scoring.json` == A0's).

**Parallelism (recommend ≤ 4 concurrent `evolve` processes).** Arms run in
separate workspaces, so they *can* run concurrently, but bound concurrency at
**≤ 4** for two grounded reasons:

1. **Shared endpoint rate limits.** Every arm hits the same operator harness +
   auxiliary endpoints; N concurrent arms multiply the offered load N× against
   one rate limit. Beyond ~4, the endpoint throttles and wall-clock inflates.
2. **One dashboard port each.** `zicato evolve` binds the dashboard on
   `--dashboard-port` (default **7892**, `cli/commands/evolve.py`). Concurrent
   evolves do **not** collide on it — `dashboard/server.py::_pick_port` walks
   `preferred..preferred+10` and takes the first free one — but they do land on
   ports nobody chose. The house rule forbids a `--dashboard-bind` flag and says
   nothing about the port, so assign each concurrent run a **distinct** port —
   7892, 7893, 7894, 7895 — to keep the mapping legible, and **record each run's
   actual printed URL**, which is the port that was really bound.

**Run ordering — the CONTROLS first, then BASE, then the treatments.** The
pre-gates (§6.1 items 2–3) come before any spend. Then launch the **A/A** and
**BASE** arms together: their contrast is the floor, and **no treatment arm's
read means anything until the floor exists**. Only once the floor is derived and
the specificity gate passes do the treatment arms launch. The planted-defect arm
runs alongside them (its contrast is the sensitivity gate, read at analysis
time).

**Do NOT re-derive the floor after seeing a treatment arm.** The floor is a
property of the batch, computed once, from the A/A contrast, before any
treatment read. Re-deriving it later is threshold renegotiation by another name.

### 6.4 Monitoring + failure policy (per arm, per run)

**Per-run signals to watch (log stream + dashboard):**

- **Log stream:** `preflight_signal_below_floor` / `preflight_saturated_contract`
  at evolve start (dev-guide §9); `margin_below_noise_floor` (E5);
  `stalled_loop`; `placebo_promoted` (CRITICAL —
  `health/diagnostics.detect_placebo_promoted`; the decision procedure is broken
  and every recent "win" is suspect; dev-guide §11).
- **Publication LIVING DRAFT:** the dashboard's publication tab regenerates its
  deterministic sections every settled round. A promotion whose gate margin sits
  below the floor is a suspect promotion.
- **Board-status surface:** the `generalization_gap`
  (`health/diagnostics.detect_generalization_gap`) and holdout budget panels — a
  widening gap on a read-side arm (genealogy, calibration, exemplars) is the
  overfitting alarm those arms exist to be checked against.
- **Credential/endpoint health.** R.5 is the reason this is now a watch item and
  not an assumption. Do **not** rely on the loop noticing: §6.6 is the check.

**Abort triggers (signal → threshold → action; any one fires → stop THAT run,
log the reason, never silently continue):**

| # | Signal | Threshold | Action |
|---|---|---|---|
| 1 | `placebo_promoted` CRITICAL | fires once | Abort the run AND freeze that arm — its evidence is void until the coordinator explains it. |
| 2 | Pre-flight verdict `REFUSE` (signal ≤ floor) at evolve start | verdict == `VERDICT_REFUSE` | Abort the run — the arm's contract cannot out-signal its own noise. |
| 3 | `stalled_loop` / zero promotions **on the PLANTED-DEFECT arm** | whole round budget | **Halt the WHOLE campaign.** The instrument is dead — R.1. (Zero promotions on a *treatment* arm is an ordinary result on this target and is **not** an abort trigger; R.4.) |
| 4 | Wall-clock or spend for the arm | > 1.5× the §5 **measured** estimate | Stop and re-price with the coordinator before continuing. |
| 5 | Infra-abort rate (`core/loss.is_infra_abort_cause`, dev-guide §1.6) | dominates real measurements in the run | The run is **void** — re-run it, never average it in. |
| 6 | **§6.6 integrity verification fails for a cell** | `zicato epoch rounds --verify` exits non-zero — any VOID round, **or zero rounds** | The **cell** is void. **DELETE and re-run it — never resume.** R.5: resuming appends into the poisoned epoch, which is how a cell ended up with four rounds of which three were void. |

**Crash / restart policy:**

- A run that **crashes** (process death, infra outage, an abort-trigger #5 void)
  is **restarted ONCE** from a **fresh clone** of its
  `campaign-<ts>/<arm>/k<seed_index>` workspace.
- **Twice-crashed → record the run as `aborted`** in `runs.jsonl`
  (`"aborted": true, "abort_reason": "<one line>"`). **Never silently drop it.**
- An **aborted run does NOT get re-seeded** — K shrinks for that arm, and the §7
  report **says so explicitly**. Do not substitute a fresh seed to "top up" K;
  that would silently bias the arm. **Note the tension with paired seeds: a
  shrunk K breaks the pairing for that seed index, so the paired contrast must
  drop that seed from EVERY arm, not just the aborted one.** Report both counts.

### 6.5 Data collection (one record per arm×seed run; every field's SOURCE named)

Emit exactly one `runs.jsonl` record (§7.1 schema) per arm×seed run. Pull each
field from the shipped surface below — no ad-hoc file walks:

| Field | Source (verified in tree) |
|---|---|
| `arm`, `seed_index`, `workspace` | executor bookkeeping (the §6.3 workspace path) |
| `epoch_id` | the epoch opened by `epoch new` (`epoch/lifecycle.current_epoch_id`) |
| `rounds_completed` | RoundLog fold — count of settled `RoundRecord`s (`epoch/round_log.py`) |
| **`round_integrity`** | **`zicato epoch rounds --json` (§6.6) — the per-round classification and the cell-acceptance verdict** |
| `duels` | one per `GateEvaluated` in the epoch's round logs (`epoch/round_log.py`) |
| **`cell_mean_d`** | **mean over the cell's duels of `champion_scalar − challenger_scalar` off `GateEvaluated` (§3.1) — the PRIMARY endpoint** |
| `promotions` | `tournament/detail.optimization_trajectory(db_path, epoch_id)` → `Trajectory.promoted_count` |
| `board_runs` | `builder/operations.estimate_cost.board_runs_per_round` × `rounds_completed` — deterministic given the structure (E4, §5) |
| `wall_clock_s` | `tournament/detail.tournament_cost` → `total_runtime_ms`, **milliseconds** — divide by 1000, since the wire field is named in seconds (matches E3, which already reads the `_ms` name) |
| `calibration_fraction` | `proposer/calibration.sample_calibration.calibration_fraction` / `tournament/detail.proposer_calibration_rate` |
| `holdout_confirms`, `holdout_rejects` | RoundLog fold — `HoldoutReleased` events |
| `placebo_events` | `health/diagnostics.detect_placebo_promoted` count |
| `gate_margin_summary` | RoundLog fold — `GateEvaluated.margin_required` over settled rounds, reduced to `{median, max}` |
| `dashboard_url` | the exact URL `evolve` printed (§6.3) |
| `aborted`, `abort_reason`, `notes` | executor bookkeeping (§6.4 policy) |

**Do not compute the arm-level statistic from a pooled list of duels.** Reduce
each cell to `cell_mean_d` first; every arm-level number in §7 is a statistic
over **cell means** (§3.1b). This is the step whose omission understated every
SE by ~2×.

### 6.6 Round-completeness verification (integrity, not liveness) — MANDATORY

R.5 is the whole justification: **a cell that reports healthy at the scheduler's
granularity can be built from truncated data at the endpoint's granularity.**
Verify every cell at the **round** level before it counts, and again before
analysis.

zicato ships the reader:

```bash
# Render the per-round classification with its evidence (human read):
zicato epoch rounds --workspace .zicato --epoch <epoch_id>

# Gate on it — exit 1 when any round is VOID (the campaign's cell-acceptance rule):
zicato epoch rounds --workspace .zicato --epoch <epoch_id> --verify

# Machine-readable, for the §7 artifacts:
zicato epoch rounds --workspace .zicato --epoch <epoch_id> --json
```

The reader (`src/zicato/epoch/round_integrity.py`) walks
`epochs/{epoch}/rounds/*/round_log.jsonl` and classifies **every round**:

| Class | Wire token | Meaning | Effect on the cell |
|---|---|---|---|
| **COMPLETE** | `complete` | opened **and** closed, with **≥ 1 `gate_evaluated`** | contributes a duel to `cell_mean_d` |
| **SETTLED-DEGRADED** | `settled_degraded` | closed with a **real measurement** but no gate — the proposer *was* reached and genuinely produced an invalid patch | **accepted**; contributes no duel |
| **VOID** | `void` | everything else — a torn/partial log without its completion marker; no `gate_evaluated` plus evidence of hard infra/credential failure; **or** a round that closed with no gate and no evidence a patch was produced and rejected (a transport error does not count — see below) | **the whole cell is rejected** |

The wire tokens are the ones `--json` emits; §7's artifacts use them verbatim.

**The SETTLED-DEGRADED class is deliberately narrow, and the narrowness is the
governing rule, quoted here in the form the run report states it:**

> Reject any cell containing a round that both lacks a `gate_evaluated` and
> carries a hard credential error. **Deliberately narrow:** a round where the
> proposer *was* reached and genuinely produced an invalid patch is a real
> measurement and must still be accepted — otherwise a legitimately-degraded arm
> gets retried to exhaustion.

That is the whole reason the class exists. The loop did its job and the answer
was "this candidate is no good"; voiding it would spend the arm's retry budget
re-measuring a result already in hand, which is a different way to bias the same
comparison. **The reader is strictly stricter than that rule in one direction,
and the executor must know it: VOID is the default, not the special case.**
Acceptance of a gateless round requires the positive evidence above, so a
gateless round carrying neither a measurement nor an explanation is void with no
infra marker anywhere in sight (`round_integrity.py` rule 5). The operator's
rule says which rounds must *never* be accepted; the reader additionally
declines to accept rounds that can show nothing at all.

**What "the proposer was reached" is actually asserted from.** The reader has no
direct record of a model response, so it infers reach from three round-log
tokens: a **non-recombined** candidate sampled, an experiment minted, or patches
applied. The exclusion is load-bearing — a *mechanical* recombination mint
(`proposer/recombine.py`, pure, no IO) produces a candidate with no model call
at all, so on A3/A7 counting it as reach would let a round with zero model
responses read as a real degraded measurement.

**The marker rule now catches that round too, and the two mechanisms are
independent — which is the part worth internalising.** Until issue #141,
best-of-N *discarded* the failed slots' errors whenever any slot survived
(`proposer/best_of_n.py`), so such a round closed with an EMPTY error list and
the reach predicate was the only thing standing between it and an accepted
cell. The wrapper now emits one `proposal_attempted` per failed slot, carrying
that slot's attempts verbatim, so a credential-lapsed slate leaves its evidence
in the log and voids by **rule 3** on the matched marker — with the endpoint's
own words in the report, which the reach predicate alone could never give the
operator. **The reach predicate remains, as the backstop.** It needs no
evidence to have been written, so it still holds if a future proposer path
forgets to emit or an endpoint's prose matches no marker; the marker scan, in
turn, holds when a mint is not flagged as recombined. Do not treat either as
redundant. Reach is also read over the
**final attempt span only** (the events after the last `round_opened`), because
the round log is append-only and a round index can be reused after an attempt
died before its experiment was persisted; without the span, a prior attempt's
tokens would vouch for this one.

**On the hard-infra vocabulary — the PREFIX ANCHOR is the mechanism; the marker
set is a floor on top of it.** Marker matching is restricted to proposal-error
strings that begin with a **transport-shaped prefix** — the templates the
proposer emits when a request failed before a response came back (`auxiliary LLM
call raised …`, `auxiliary LLM call timed out after …`, `proposer agent run
raised …`). Every other string in a round's error trail is a *post-response
content rejection* that quotes text zicato does not control: validator findings
over the child agent's own source, mutation ids taken from the operator's own
`# zicato:mutable` markers and brief, the built-in drift-kind list, and the
model's own offending values echoed back by a schema violation. Anchoring is
what makes those structurally ineligible rather than merely unlikely. (One
zicato-authored tag may sit in front of the prefix and is stripped before the
anchor is tested: `slot 0: `, which an all-failed best-of-N slate puts on each
aggregated attempt. Expect to see it in the report; it names the slate slot the
error came from.) Three consequences the executor must understand:

- **A false positive is far worse than a false negative here, and the asymmetry
  governs every choice below.** A false positive voids a *real* measurement,
  burns the arm's retry budget, and — because arms differ in how often they emit
  invalid patches — deletes rounds in an **arm-correlated** pattern, which is
  precisely the contamination shape R.5 describes. A false negative merely falls
  through to rule 5, **which still voids a genuine credential lapse** — see the
  next paragraph for what that fallthrough actually rests on, because the
  obvious reason ("nothing mints a reach token") is *not* the one that holds.
- **The excluded tokens stay excluded, as defense in depth.** Bare `timeout`
  (one attempt timing out while a later one returns a real, if invalid, proposal
  is a real measurement), bare `forbidden` (the proposer's own forbidden-id
  rejections use that word), and bare numeric status codes (a three-digit run
  occurs inside ids and offsets). **Do not widen the set in the doc or by
  editing it in place.**
- **Widen it per-call instead.** `round_integrity()`, `epoch_round_integrity()`
  and `classify_round()` each take an `infra_markers` argument; `zicato epoch
  rounds` runs the default set, so a widened vocabulary means calling the reader
  directly. If a campaign's endpoint reports outages in prose the default set
  does not cover, pass a widened set for that campaign and **record the widening
  in the run log** — it is a change to the acceptance rule and must be visible
  in the report, not buried in a config. Widening is safe *because* of the
  anchor: a broader token can only ever be tested against transport-shaped
  errors.

**What the rule-5 fallthrough actually rests on: a transport error is never
patch evidence.** The reason a vocabulary miss is survivable is *not* that an
outage leaves no reach token — issue #141 made that reasoning obsolete. A
best-of-N slate can have one slot survive (minting `candidate_sampled`, a
perfectly good reach token) while a sibling slot dies at the call boundary, and
since #141 the sibling's error is *written to the log* rather than discarded.
So the round arrives at the reader with reach asserted AND an error on the
record. What keeps it from being accepted is that `invalid_patch` counts
**content rejections only**: a request that failed before a response came back
produced no patch, so it cannot be an invalid one, and it cannot satisfy rule
4's "the proposer was reached and genuinely produced an invalid patch". Without
that exclusion, a vocabulary miss would flip such a round from VOID to
`settled_degraded` — the fix for a *reporting* gap would have quietly loosened
the *acceptance* rule, which is the exact inversion this section exists to
prevent. The governing invariant, worth memorising:

> **Reporting more evidence can only ever move a verdict toward VOID, never away
> from it.**

The executor's practical consequence: a round whose only errors are
transport-shaped and unmatched is VOID, and its evidence line says so by name —
`closed without a gate, carrying a call-boundary error that matched no infra
marker (…) — consider widening \`infra_markers\``. **Treat that line as the
signal to widen for this endpoint** (per the bullet above, and record the
widening in the run log), then re-read. The verdict does not change with the
widening — VOID either way — but the *reason* does, and the anomaly belongs in
§7 as an endpoint-prose finding rather than an unexplained void.

**Cell acceptance rule:** a cell is **ACCEPTED iff it contains zero VOID
rounds** — **and has at least one round.** Zero rounds is vacuously free of
void rounds, so `--verify` fails an empty epoch too; a cell whose `evolve` died
before its first round log measured nothing, and nothing is not health. A
rejected cell is **deleted and re-run — never resumed** (§6.4 trigger 6).

**One limit the executor must read as a limit, not as coverage.** A round is
classified `complete` on **≥ 1** gate. A round that ran several challengers and
lost one of them to a credential error still gates on the survivors, so it is
consumed at full weight while resting on a narrower field than its peers — the
founding failure mode of this section, one level down. The evidence survives:
the matched marker is on the record even for a `complete` round. **Nothing acts
on it yet.** Until something does, treat a `complete` round carrying a non-empty
`infra_markers` list as an anomaly to report under §7's Anomalies heading rather
than a clean measurement.

**Two properties of this check that are not optional:**

1. **It renders its evidence.** The default output shows, per round, why the
   classification was made — including the matched infra-error string verbatim.
   A boolean "healthy" is exactly the surface that reported 144/144 on unusable
   data.
2. **It runs at the endpoint's granularity.** Cell-level accounting cannot see
   83 missing gates across 496 rounds. If a future campaign's endpoint consumes
   something finer than a round, the check moves with it.

**Report the round-level statistic in §7, always** — "N rounds, of which C
complete / D settled-degraded / V void", per arm. Run 2's headline
**"144/144 cells, 432 rounds, zero missing gates"** is the shape of an honest
completeness claim; **"144/144 cells healthy"** is the shape of the claim that
was wrong twice.

## 7. The reporting contract (the coordinator's required output format)

**This is what you deliver. Produce every artifact below; make no decisions.**
You compute the pre-registered statistics; the §4 decision rules are applied by
the **coordinator**. Do not declare wins, do not flip defaults, do not graduate
arms.

### 7.1 Artifacts (all under `campaign-<ts>/results/`)

**`runs.jsonl`** — one JSON object per arm×seed run (fields sourced per §6.5):

```json
{"arm": "A2", "seed_index": 3, "workspace": "campaign-20260802T0900/A2/k3",
 "epoch_id": "campaign_A2_k3", "rounds_completed": 3, "duels": 3,
 "cell_mean_d": -0.0061,
 "round_integrity": {"complete": 3, "settled_degraded": 0, "void": 0,
                     "accepted": true, "round_count": 3, "no_rounds": false},
 "promotions": 0, "board_runs": 42, "wall_clock_s": 1042,
 "calibration_fraction": 0.5, "holdout_confirms": 2, "holdout_rejects": 0,
 "placebo_events": 0, "gate_margin_summary": {"median": 0.2, "max": 0.2},
 "dashboard_url": "http://127.0.0.1:7892", "aborted": false,
 "abort_reason": null, "notes": ""}
```

`holdout_confirms` / `holdout_rejects` are shown with a LIVE split. When the board is below `min_board_size_for_split` the holdout is empty and both are structurally 0 — emit `null` and flag the run, never 0 (§2.3).

`round_integrity` is a **reduction** of `zicato epoch rounds --json`, not its
raw payload: flatten that payload's `counts` object and carry `accepted`,
`round_count`, and `no_rounds` across. **Carry `no_rounds` even though it is
almost always `false`** — it is the field that distinguishes a clean cell from
one that never ran, and `accepted` is `true` for both (§6.6).

**`floor.json`** — the **derived** floor and the two validity gates. Note the
shape: the floor is a *contrast*, not a per-arm constant.

```json
{
  "derived_floor": {
    "formula": "2 * SE(mean d[AA] - mean d[BASE]), cell-clustered",
    "value": 0.0400, "K_per_arm": 12, "unit": "per-duel d"
  },
  "sensitivity": { "contrast": "PD - BASE", "value": 0.1526,
                   "ci90": [0.1264, 0.1787], "floor_multiple": 3.8,
                   "verdict": "PASS" },
  "specificity": { "contrast": "AA - BASE", "value": 0.0013,
                   "ci90": [-0.0334, 0.0360], "verdict": "PASS" },
  "board_entry_noise_floor": { "value": 0.64, "source": "board preflight",
                               "note": "NOT the arm-contrast floor; see §3.2" }
}
```

**`campaign_summary.json`** — per-arm aggregates over **cell means** (§3.1b).
The pre-registered statistic is **mean `cell_mean_d` vs BASE, with its 90% CI
and its Holm-adjusted p**:

```json
{
  "A0":  { "K_completed": 12, "mean_d": 0.0000, "role": "baseline" },
  "AA":  { "K_completed": 12, "mean_d": 0.0013, "diff_vs_base": 0.0013,
           "ci90": [-0.0334, 0.0360], "role": "specificity control" },
  "PD":  { "K_completed": 12, "diff_vs_base": 0.1526,
           "ci90": [0.1264, 0.1787], "role": "sensitivity control" },
  "A2":  { "K_completed": 12, "diff_vs_base": 0.0028,
           "ci90": [-0.0214, 0.0270], "holm_p": 1.000,
           "promotions_total": 0, "rounds_total": 36, "board_runs_total": 720,
           "cost_per_promotion_board_runs": null,
           "round_integrity": {"complete": 36, "settled_degraded": 0, "void": 0},
           "flags": ["cpp_not_applicable_zero_promotions"] }
}
```

`cost_per_promotion_board_runs` is `null` — **not** a large number and **not**
omitted — when the arm never promoted (§4 rule 3).

**`CAMPAIGN-REPORT.md`** — the human summary, on this **fixed template**:

```markdown
# Campaign <ts> — results (K=<K> cells/arm × R=<R> rounds × <n> arms)

Authorization: <verbatim operator go-ahead wording, §6.0>.
Derived floor: <value> = 2 x SE(AA - BASE), cell-clustered, K=<K>.

## Validity gates (READ FIRST — a failure here voids the ranking)
| gate | contrast | value | 90% CI | floor multiple | verdict |
|------|----------|-------|--------|----------------|---------|
| sensitivity | PD - BASE  | | | | |
| specificity | AA - BASE  | | | | |

## Round integrity (§6.6)
<total rounds>, of which <complete> complete / <degraded> settled-degraded /
<void> void. Cells accepted: <n>/<N>. Cells deleted-and-re-run: <n>.

## Headline table (cell-clustered; every number is a statistic over CELL MEANS)
| Arm | K | mean d vs BASE | 90% CI | Holm p | promotions / rounds | board-runs | cost/promotion | flags |
|-----|---|----------------|--------|--------|---------------------|-----------|----------------|-------|

## Per-arm narratives (one paragraph each)
<arm>: <what the numbers show — diff vs BASE, CI, Holm p, promotions, cost,
any calibration/holdout/placebo notes. No verdict.>

## Anomalies
<aborts (arm, seed, reason, resulting K), abort-trigger fires, void cells and
their re-runs, endpoint throttling, anything that departed from the plan.>

## Artifact inventory
- runs.jsonl (<n> records)
- floor.json
- campaign_summary.json
- per-run analysis.md / analysis.html paths
```

### 7.2 The final message back to the coordinator (verbatim contract)

Send the coordinator, in one message:

1. **The validity-gate table first.** If either gate fails, say so before
   anything else and label the ranking VOID.
2. **The round-integrity line** (§6.6) — total rounds, the three counts, cells
   accepted, cells deleted-and-re-run. **Never report cell health alone.**
3. **The headline table**, stated as statistics over cell means.
4. **Aborts / anomalies** — each with a one-line cause (arm, seed, trigger,
   resulting K, and the paired-seed consequence per §6.4).
5. **The artifact paths** (`campaign-<ts>/results/…`).
6. **Explicitly: NO decision-making and NO raw dumps.** The §4 decision rules
   are the coordinator's to apply. Do not paste raw `runs.jsonl` — reference it.

**Language discipline for every inconclusive arm:** write *"no effect larger
than <floor>"*, never *"no effect"* (§4). The distinction is the difference
between a result and an overclaim.

**Progress cadence:** send **one status report per completed ARM** (its per-arm
row + any anomaly), **not per round**. The A/A + BASE status report additionally
carries the **derived floor and the specificity gate** — and it is the message
that PAUSES the campaign if specificity fails, before any treatment arm is read.

## 8. Cross-references

| Topic | Source |
|---|---|
| The knob defaults + validation + omit-at-default | `src/zicato/core/scoring_config.py` (`ProposerQualityConfig`, `recommended_scaffold_weights`) |
| Cost-meter semantics (board runs, aux calls, screen/recombine terms) | `src/zicato/builder/operations.py::estimate_cost` |
| The round event log + the `d` endpoint's source events | `src/zicato/epoch/round_log.py` (`GateEvaluated`, `HarnessLoaded`) |
| **Round-completeness verification (§6.6)** | `src/zicato/epoch/round_integrity.py`, `zicato epoch rounds` |
| Breadth/depth role wiring (why roles are runtime, not contract) | `src/zicato/proposer/best_of_n.py`, `src/zicato/models_config.py` |
| Noise doctrine, A/A floor, replication power, planted-delta method | dev-guide `04-evaluation-statistics.md` §§3,4,13 |
| The live MDE ladder that pins §3's two-sample numbers | `docs/design/EVAL-VIEW.md` §4.3, `src/zicato/query/eval_view.py` |
| Offline power-harness precedent + report style | `tests/test_decision_procedure_power.py`, `tools/cascade_oc.py` |
| The dogfood ladder (target_0 dry-run → target_1 live) | `docs/design/DOGFOOD-TARGETS.md` §1 |
| The analytics that consume the results | `docs/design/PUBLICATION.md`, `docs/design/ANALYTICAL-INDEX.md`, `tournament/detail.py` |
| Recombination known-answer oracle (why recombine ranked #1 a priori) | dev-guide `04` §1.8, `tests/test_recombination_known_answer.py` |
| Overfitting boundary rules (why process_exemplars ranks last) | `docs/design/OVERFITTING.md`, dev-guide `04` §12 |
| The live-run gate | dev-guide `14-goals-and-roadmap.md` §"Endpoint-gated backlog"; the G3 gate above |
