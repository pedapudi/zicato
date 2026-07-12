# The live measurement campaign — deciding the scaffold defaults with evidence

> **STATUS — DESIGNED / EXECUTION GATED ON EXPLICIT OPERATOR GO-AHEAD.**
> Nothing in this document authorizes a run. Every command in §6 is a
> *plan*; a live `zicato evolve` invocation against a real model endpoint
> may be started only after the operator gives an explicit go-ahead for
> that specific arm (the G3 live-run gate). This doc pre-registers the
> arms, the metrics, the power math, and the decision rules **before** any
> data exists — the same discipline `tools/cascade_oc.py` and
> `tests/test_decision_procedure_power.py` apply to every statistical claim
> in the repository (dev-guide `04-evaluation-statistics.md` §13: operating
> characteristics are *measured, not asserted by hope*).

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
`process_exemplars`, and the ensemble roles off. None of those six choices
has ever been measured against promotion-rate-per-cost on a **live** target.
This campaign replaces taste with a pre-registered knob sweep.

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

Ranked by *plausible lift in promotion-rate-per-cost* — the campaign's one
headline quantity. "Per cost" is decisive: a read-side channel that the cost
meter never charges (`genealogy`, `calibration_feedback`) earns its keep on a
far lower bar than a board-run-bearing one (`screen`).

| Rank | Knob (arm) | Why it plausibly raises promotion-rate-per-cost | What result flips its default |
|---|---|---|---|
| 1 | **`recombine` (mechanical)** | The only knob with a mechanism *proven in the known-answer oracle* to capture a promotion the gate would otherwise reject: dev-guide §1.8's two-marker world plants two disjoint single-fixes each worth Δ 1.2 that **each reject** at `promote_margin=1.5`, while the mechanical union of their patches (Δ 2.4) **promotes** (`tests/test_recombination_known_answer.py`). It raises promotions-per-round at **zero** extra board-run cost (the mint replaces a slot's propose call). Best per-cost candidate on the board. | Flip `recombine` → default-`True` if the arm clears the §4 bar. |
| 2 | **`genealogy`** | In-context lineage evolution reaches even the pure-drift-side rejected pairs the mechanical slot cannot see (`ProposerQualityConfig.genealogy` docstring). **Read-side only — the cost meter is untouched** — so any promotion-rate lift is free on the board-run axis. | Flip `genealogy` → a non-zero scaffold default if it clears the (low, read-side) §4 bar. |
| 3 | **`best_of_n` (held at 3 in BASE)** | The top proposal-quality lever per `FUNCTIONALITY-RECOMMENDATIONS.md` §4.1 — a valid-but-mediocre single sample was never reconsidered. Already ON; BASE validates it still earns its `× best_of_n` aux-call cost. A `best_of_n=1` ablation is the pre-registered follow-on (§4) if BASE underperforms. | Keep `best_of_n=3` if BASE beats the ablation; revert toward `1` otherwise. |
| 4 | **`screen_entries` (currently scaffold-ON=2)** | Vetoes catastrophic candidates *before* the tournament spends on them — but **adds** board runs (`proposes × best_of_n × panel`, §5). Its per-cost effect is genuinely ambiguous, which is exactly why it is the scaffold choice most in need of audit. Screen false-veto ≈ flip-rate² under confirm-before-veto (dev-guide §3.1 fact #7) means the veto is *sound*; the open question is whether the extra panel runs buy net throughput. | **Reverse null**: `screen_entries` stays scaffold-`2` only if it clears §4; otherwise the pre-registered action is to **remove it** from `recommended_scaffold_weights` (scaffold default → `0`). |
| 5 | **breadth/depth roles** | If breadth explores a wider slate and depth refines the critique/merge, slate quality rises with **no** board-run cost. Second-order: it reshapes *which* candidate wins, not how many board units run. | Flip to a scaffolded two-role `models` block if it clears §4 and the per-call cost delta is acceptable. |
| 6 | **`calibration_feedback`** | Showing the proposer its own hit/miss pattern (`/api/hypothesis-accuracy` grader, `proposer/calibration.py`) plausibly improves **hypothesis calibration** more than raw promotion rate; read-side/free. | Flip on if it clears §4 on either the promotion endpoint **or** the calibration-fraction endpoint (§3) at no board-run cost. |
| 7 | **`recombine_merge="llm"`** | Conditional on `recombine`: relaxes disjointness so two *overlapping* rejected fixes can be merged by one aux call. Incremental reach beyond mechanical, at +1 aux call on merge rounds. | Flip `recombine_merge` → `"llm"` only if the llm arm beats the mechanical arm (§4), given `recombine` already flipped on. |
| 8 | **`process_exemplars`** | Highest-risk: it **widens the proposer-visibility channel** (OVERFITTING.md §11), so its default-flip bar is not just promotion rate but a **clean generalization-gap + placebo record** (dev-guide §12). Opt-in-deliberate under the PROCESS-EXEMPLARS.md §5 harm runbook; NOT scaffold-set. | Ranked last for a default flip; evaluated as a **pre-registered extension arm** (§4), never graduated on promotion rate alone. |

## 2. The arm matrix (fractional, not full factorial)

Eight generator knobs would be 2⁸ = 256 full-factorial cells. That is
infeasible on a live target and mostly wasted — the knobs are not
independent (recombine's merge mode is meaningless without recombine;
genealogy and calibration are both read-side in-context channels). The
campaign runs a **justified 8-arm fractional design**: one baseline, six
single-knob arms isolating each required lever, and two principled
combinations that probe the two natural knob *families* (read-side
in-context vs. board-run mechanical).

**Shared campaign controls (identical on every arm — held constant so the
only thing that varies is the named knob):**

- **Target:** `target_1_presentation` (the v0 dogfood — DOGFOOD-TARGETS.md
  §1; real coordinator+specialist agent, 7-entry board, real drift). A
  **live** proposer is mandatory: the generator arsenal (`best_of_n>1`,
  `screen`, `genealogy`, `recombine`, roles) only does anything with a real
  model sampling the slate; `target_0`'s scripted proposer would leave every
  arm byte-identical. `target_0_convergence` is used only as the
  **deterministic instrument dry-run** (§6.1), never as a measurement arm.
- **Tournament structure:** `gauntlet`, `replicates: 2` (buy a little
  per-duel power without the evidence gate's ~37-duel crowning budget —
  dev-guide §3.1 fact #3), with `field_size` pinned to **1**. The gauntlet's
  `GauntletStrategy.field_size()` hard-returns `1`
  (`selection/strategies/gauntlet.py`), but `estimate_cost` defaults an
  unset `field_size` to `2`; pinning `field_size: 1` in the shared control
  makes the a-priori cost meter (§5) read the *true* runtime board-run count
  instead of double-counting a challenger the gauntlet never runs. The
  evidence gate is deliberately **off** for the
  campaign: its honest cost would dominate the meter and it is a *soundness*
  device, orthogonal to the *proposal-generation* questions under test
  (dev-guide invariant #10). Holding structure fixed keeps the board-run
  denominator comparable across arms.
- **Overfitting controls:** `rotate_holdout: false`, `holdout_fraction: 0.6`
  set **explicitly and identically** on every arm, so all arms (each a
  distinct epoch) see the **same** train/holdout split of the fixed board —
  removing the cross-epoch holdout-rotation confounder (§3.4). Everything
  else in `OverfittingConfig` stays default-on. The fraction is 0.6, NOT
  the 0.3 default, and this is load-bearing: the hash-based split at 0.3
  puts **zero** of this 7-entry board's ids into the holdout (verified by
  running `split_board` — every id hashes above the 0.3 threshold), which
  would leave the generalization-gap and holdout-confirm metrics silently
  inert for the whole campaign. At 0.6 the split is **train = 5, holdout =
  2** (`q3_metrics_outline`, `every_expectation_kind_demo`), and the cost
  meter still reads the same 14/20 board-runs per round (the smaller train
  is offset by the 2 × replicates holdout-confirm runs — re-verified with
  `estimate_cost`).
- **Seed v0:** every arm starts from the **same** registered champion (the
  vendored `target_1` agent), so the headroom to the floor is identical at
  round 0.
- **Round budget:** `--rounds 12` per run. **Replication:** `K = 6`
  independent runs per arm (each a fresh workspace clone — §3.2).

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
    "params": { "replicates": 2, "field_size": 1 }
  },
  "overfitting": { "rotate_holdout": false, "holdout_fraction": 0.6 }
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

**Coverage check.** Every required knob appears at least once as the *sole*
delta from BASE (A1 genealogy, A2 screen, A3 recombine-mech, A4
recombine-llm, A5 roles) — so each single-knob effect is identified — and
`calibration_feedback` + the two families appear in the two combos. `best_of_n`
is validated by BASE against its pre-registered `best_of_n=1` ablation (§4).
`process_exemplars` is the extension arm (§4), off the main matrix by design.

## 3. Measurement plan

### 3.1 The endpoints and exactly where each number comes from

| # | Metric | Source (verified in tree) |
|---|---|---|
| **E1 (primary)** | **Final champion Δscalar** (seed v0 → final promoted head), in A/A-floor units | `EpochReportData.final_scalar` minus the seed scalar (analyzer / PUBLICATION §1); floor = `noise_floor.max_abs_delta` on the epoch record (`epoch/lifecycle.set_epoch_noise_floor`, dev-guide §4) |
| E2 (secondary) | **Promotion count** and **promotion_rate** = promoted / challengers | `tournament/detail.py::tournament_summary` → `promotion_rate` (line ~1256): `promoted_count / challenger_count` |
| E3 (secondary) | **cost_per_promotion** (wall-clock) = total_runtime_ms / promoted | `tournament/detail.py::tournament_cost` → `cost_per_promotion_ms` (line ~1436) |
| E4 (secondary) | **Board-runs cost** (a priori, deterministic) | `builder/operations.py::estimate_cost` → `board_runs_per_round`; this is the campaign's *primary* cost unit (§5) because it is exact given the structure, unlike wall-clock |
| E5 | **Gate margin vs A/A floor** per promotion | `RoundRecord` fold: `GateEvaluated` (`delta_scalar`, `margin`) vs the persisted `noise_floor` (PUBLICATION §5, "Statistical integrity"); the `margin_below_noise_floor` health finding (dev-guide §4) is the guardrail |
| E6 | **Hypothesis-calibration fraction** (predicted Δ vs measured Δ) | `tournament/detail.py::hypothesis_ledger` / `/api/hypothesis-accuracy` feed; `proposer/calibration.py`; PUBLICATION §6 |
| E7 | **BT / Elo ratings** ± SE at crowning | the `elo` column (ANALYTICAL-INDEX schema v10: `1500 + θ·400/ln 10`, re-fit at reindex); `selection/rating.py`, PUBLICATION §4 |
| E8 | **Statistical-integrity record** (placebo, ladder budget, screen veto/confirm, generalization gap) | `RoundRecord` fold — `CandidateScreened`, `HoldoutReleased`, `decision_provenance`; `health/diagnostics.py` `placebo_promoted` / `generalization_gap` (PUBLICATION §5, dev-guide §12) |

E1 is the **primary** endpoint deliberately. Promotion (E2) is a *rare,
binary, high-variance* event; the champion scalar is the **uniform continuous
outcome axis** (`aggregate_generation_score` `mean_score`, dev-guide §1.4)
with no threshold cliff, so it carries far more information per round than the
promote/no-promote bit. Stating the effect in floor units (dev-guide §13.4
"planted deltas in floor units") makes it comparable across arms whose
absolute scalars differ.

### 3.2 Replication and the rare-event power math (why K=6 screening, K≈24 confirmatory)

Promotions are rare events, so a **decision-grade CI on the binary promotion
rate is prohibitively expensive on a live target** — this is the honest
headline, and it drives the whole plan. The math, following the dev-guide §13
methodology (measure the null, state effects in floor units, pin operating
characteristics):

**A "replicate run" for a live target** = one fresh workspace clone of
`target_1`, same arm contract, run independently for R rounds. Unlike the
deterministic power harness (`stable_noise_seed` from
`(workspace_seed, generation_id, entry_id, replicate_index)`, dev-guide
§13.1), a live model exposes **no seed we control** — the endpoint's own
sampling variance is the noise source. Consequence, stated plainly: live
runs are **not** byte-reproducible, and the CIs are genuine sampling CIs over
model nondeterminism, not the calibrated-documentation determinism the Tier-2
harness enjoys.

**Binary-endpoint power (the infeasible instrument).** Treat each of the
`n = K·R` round-decisions as an approximately-Bernoulli promote event (Wilson
CI, matching `cascade_oc.py::_wilson_ci`). To detect a promotion-rate lift
from a plausible live baseline `p0 ≈ 0.20` to `p1 ≈ 0.30` (a relative +50%,
the smallest lift worth flipping a default for) at α=0.05 two-sided, power
0.80:

```
n_per_arm ≈ (z_{α/2} + z_β)² · [p0(1−p0) + p1(1−p1)] / (p1 − p0)²
          = (1.96 + 0.84)² · [0.16 + 0.21] / (0.10)²
          = 7.84 · 0.37 / 0.01  ≈  290 round-decisions per arm.
```

At R=12 that is **K ≈ 24 runs per arm** — 8 × 24 × 12 = 2,304 live rounds
just for the binary endpoint. **Rejected as the primary instrument**, for the
same reason the evidence gate is opt-in rather than default-on (dev-guide
§3.2: an honest cost that would freeze a real budget).

**Continuous-endpoint power (the feasible instrument).** E1 (final Δscalar in
floor units) is continuous, so K independent runs give a t-CI on the arm mean
with K−1 df. Power is bought with replication as dev-guide §3.1 fact #3
describes (averaging shrinks the per-run sd). The quantity a screen turns on is
not the WITHIN-arm precision but the resolvable INTER-arm gap, and these are
different numbers. At **K=6** the within-arm 90% mean-CI half-width is
≈ `2.02 · sd/√6 ≈ 0.82·sd` — but resolving a gap *between* two arms is a
**two-sample** question. Recomputed for `sd ≈ floor` (n=6/arm, df=10):

- the minimum detectable effect at 80% power is **≈ 1.79·floor** at α=.05
  (**≈ 1.55·floor** at α=.10), and
- the §4 non-overlap clause (two non-overlapping 90% t-CIs) needs an observed
  gap **> ≈ 1.48·floor**.

So the K=6 screen cleanly resolves **~1.5-floor** effects — **not** the
0.5–1.0 band an earlier draft claimed (that draft conflated the 0.82·sd
single-arm mean-CI half-width with the inter-arm gap). Arms whose *true* effect
sits in the 0.5–1.0 band will therefore **mostly land "ambiguous → graduate to
K≈24"** rather than resolve at K=6; that is the accepted screen/confirm trade,
and it is safe — the per-arm false-flip probability under it is ≈1%. (The
0.5×/1×/3× floor ladder and the "0.5-floor is catchable" claim are dev-guide
§13.4 / §3.1-fact-#5 properties of the **32-replicate evidence gate**, which
this campaign deliberately turns OFF — §2. That borrowed guarantee does not
transfer to a K=6 screen, so it is dropped here.) K=6 is therefore
**screening-grade for E1** (it graduates arms, it never flips a default — §4,
§5), with the K≈24 confirmatory run the only decision-grade instrument;
E2/E3 stay screening-grade context only.

**Grounding the flip bar's sd assumption (pre-registered).** The "~1.5-floor
resolution" and the §4 non-overlap bar assume the cross-run sd of E1 is on the
order of the A/A floor — an assumption that is **ungrounded** for a live
endpoint until measured. Before any arm's read is trusted against the bar,
**estimate the actual cross-run sd of E1 from BASE's first K runs** and compare
it to the floor. **Adjustment rule:** the resolvable gap scales linearly with
sd (MDE ∝ sd), so if the measured sd exceeds the floor by a factor `f`, every
resolvable-gap and non-overlap threshold above multiplies by `f` (and holding a
~1.5-floor resolution would require raising K by ≈ `f²`); if sd is materially
below the floor, the screen resolves proportionally finer. This estimate is
made and recorded **before** any graduate/ambiguous verdict is read.

**The two-tier plan that follows:**

1. **Screening campaign (this doc's budget): K=6 × R=12 × 8 arms = 576 live
   evolve rounds.** Primary read on E1 (**screening-grade — graduates arms to
   confirmation, never flips a scaffold default**); E2/E3/E6 pooled across the
   6 runs as screening-grade context.
2. **Confirmatory extension (separate go-ahead): K≈24 × R=12 on the ≤2 arms
   that graduate** from screening (§4). This is where the binary
   promotion-rate CI becomes decision-grade, at ~4× the per-arm screening
   cost. Pre-registering it now is the whole point — no arm is promoted to a
   scaffold default on a screening-grade read alone.

### 3.3 The metrics table the analytics will actually populate

All of E1–E8 are already produced by shipped surfaces — the campaign adds no
new instrumentation, it *consumes* the analytics the publication and the
analytical index emit (PUBLICATION §§1,3,4,5,6; ANALYTICAL-INDEX). Each arm's
run closes its epoch (`zicato epoch close`) → `analysis.md` / `analysis.html`
carry E1 (Abstract), E5+E8 (§5 Statistical integrity), E6 (§6 Proposer
analytics), E7 (§4 Ratings). The cross-run roll-up (mean E1 ± CI per arm) is a
read over the K closed epochs' `EpochReportData` — no ad-hoc file walks
(ANALYTICAL-INDEX is the cross-run query layer).

### 3.4 The honest confounders

- **Target-difficulty drift across epochs.** Each arm is a distinct epoch;
  naively the holdout slice would *rotate* by epoch id (`rotate_holdout`
  default `True`, `board/split.py` `rotation_seed`), so different arms would
  be scored on different holdout slices. **Mitigated** by pinning
  `rotate_holdout: false` on every arm (§2) — all arms share one split. The
  residual drift is that a promoted champion changes the *remaining headroom*,
  so late-round promotion rate is **not exchangeable** across arms with
  different early trajectories; this is exactly why E1 (final Δ over a fixed
  round budget from a fixed v0) is primary and per-round E2 is only secondary.
- **LLM nondeterminism.** No controllable seed on a live endpoint (§3.2). The
  model's sampling variance is the noise the K=6 replication is designed to
  average over; report every arm as mean ± CI, never a point estimate.
- **Temporal endpoint drift (across-arm).** A hosted model can change version
  mid-campaign; over a multi-hour run, arms measured early and arms measured
  late may be scored against a **different underlying endpoint** — a confounder
  that aliases with the knob effect *across the arm axis* (distinct from the
  within-arm sampling nondeterminism above, which averages out per arm).
  **Mitigated** by running the 8 arms in **parallel workspaces** over the same
  wall-clock window (§5), so any version shift hits every arm together rather
  than confounding the arm contrast; residual drift is folded into the
  cross-run sd the §3.2 pre-registered estimate captures.
- **Cost variance.** Board runs (E4) are **deterministic** given the structure
  — the campaign's cost is reported in board runs, not wall-clock. Wall-clock
  (E3) varies with endpoint latency and parallelism and is reported only as
  secondary color, with the caveat that a budget-clipped duel biases scalars
  pessimistically for the clipped side (dev-guide §1.6).
- **Multiplicity.** Eight arms × several endpoints invites false positives.
  Pre-registration (§4) is the defense: E1 is the single primary endpoint,
  the flip bar is fixed before data, and ambiguous reads graduate to
  confirmation rather than being declared wins.

## 4. Decision rules — pre-registered (written before the data)

For every arm, define on the primary endpoint:

```
E1(arm)  = mean over K runs of (final champion Δscalar), in A/A-floor units
ΔE1(arm) = E1(arm) − E1(A0_BASE)
CI90     = the 90% t-CI (K−1 df) on each arm's mean E1
CPP(arm) = pooled cost_per_promotion (E3), board-run form: total board runs / promotions
```

**The one authority rule — which read edits a scaffold.** **No K=6 screening
read ever flips a scaffold default.** Clearing the graduation bar below at K=6
**GRADUATES** an arm to the K≈24 confirmatory run (§3.2); a scaffold default is
flipped **only** by that confirmatory read. "Recommend" is the strongest verdict
a K=6 screen can return. Every "flip" in the per-knob rules below therefore
names the action the **confirmatory** read would authorize, not a K=6 outcome —
§3.2 and this section are reconciled to that single rule.

**The graduation bar (clearing it at K=6 → graduate to confirmation) — BOTH
must hold:**

1. **Signal clears its own noise:** `ΔE1(arm) ≥ +0.5·floor` **AND** the 90%
   CIs of `E1(arm)` and `E1(A0)` do **not** overlap. (+0.5×floor is the
   smallest *candidate* effect worth the confirmatory spend — it is a
   graduation trigger, not a decision-grade catch; recall the K=6 screen only
   cleanly resolves ~1.5-floor effects, §3.2, so a true 0.5–1.0 effect usually
   fails the non-overlap clause and lands "ambiguous", which graduates it too.)
2. **It does not cost more than it delivers:** `CPP(arm) ≤ 1.10 · CPP(A0)`
   (board-run cost per real promotion rises by at most 10%). A read-side knob
   (`genealogy`, `calibration_feedback`, roles) trivially satisfies this —
   the cost meter is untouched — so its graduation reduces to rule 1 alone.

**Does not graduate** iff `ΔE1(arm) < +0.5·floor` **OR** `CPP(arm) > 1.10 ·
CPP(A0)`.

**Ambiguous → also graduates, do not decide:** `ΔE1(arm) ≥ +0.5·floor` but the
CIs overlap (or CPP lands in `(1.0, 1.10]·CPP(A0)`) → the arm advances to the
**K≈24 confirmatory run** (§3.2). Because the K=6 screen resolves ~1.5 floor,
most true 0.5–1.0 effects land here by design. No scaffold default changes on a
screening read alone.

**Per-knob specialization of the bar (each states the graduation trigger and
the flip direction the confirmatory read would then authorize):**

- **`recombine` (A3):** graduates on the bar; the confirmatory read then flips
  it to default-`True`. Because A3 is
  cost-neutral (mint replaces a propose call — `estimate_cost` charges it as
  `best_of_n − 1` propose calls, no extra board run), rule 2 is automatic;
  the decision is rule 1 on E1, with E2 (promotions A3 caught that A0 didn't)
  as the mechanistic confirmation the oracle predicts (dev-guide §1.8).
- **`recombine_merge="llm"` (A4):** evaluated **relative to A3**, not A0.
  Graduates (and the confirmatory read flips to `"llm"`) iff `E1(A4) − E1(A3)
  ≥ +0.5·floor` with non-overlapping CIs, and the +1-aux-merge-call cost keeps
  `CPP(A4) ≤ 1.10·CPP(A3)`. If A3 itself does not graduate, A4 is moot (llm
  merge requires recombine on). **The A4-vs-A3 contrast is bundled:** `"llm"`
  merge changes both the merge *method* (one aux merge call vs. mechanical
  concatenation) **and** the candidate-pair eligibility — it reaches
  OVERLAPPING rejected pairs the mechanical mint's disjointness predicate
  rejects (`proposer/best_of_n.py` §2.6.1). The decision rule therefore reads
  the *bundle* (merge method + disjointness relaxation), not the pure merge
  effect.
- **`screen_entries` (A2) — REVERSED NULL.** The scaffold *currently* writes
  `screen_entries=2`, so the pre-registered action is to **keep** it only if
  A2 clears the graduation bar over A0. If A2 fails the bar (its ~+43%
  board-run cost, §5, is not repaid in E1), the pre-registered action is to
  **remove
  `screen_entries` from `recommended_scaffold_weights()`** — scaffold default
  → `0`. (The in-code default is already `0`; this only touches the scaffold.)
- **`genealogy` (A1), `calibration_feedback` (A6-contribution), roles (A5):**
  read-side / cost-neutral → rule 1 alone. `calibration_feedback` may flip on
  either E1 **or** a `≥ +0.10` absolute lift in the hypothesis-calibration
  fraction E6 (its designed effect is honesty, not raw promotion — §1 rank 6),
  provided E1 does not regress below A0's lower CI.
- **`best_of_n` (BASE):** pre-registered ablation — one K=6 run of A0 with
  `best_of_n: 1` (single sample, no critique). Keep the `best_of_n=3` default
  iff `E1(A0) − E1(A0-ablation) ≥ +0.5·floor`; otherwise flag `best_of_n=3`'s
  aux-call cost as unearned and revert the recommendation toward `1`.
- **`process_exemplars` — extension arm, higher bar.** Evaluated only as a
  separate K=6 arm under the PROCESS-EXEMPLARS.md §5 harm runbook. Graduates
  (and the confirmatory read may flip it on) **only if** it clears the E1
  graduation bar **AND** its `generalization_gap`
  detector stays quiet **AND** its placebo arm never promotes (dev-guide §12
  boundary rules). A promotion-rate lift bought with a widening
  generalization gap is a *reject*, not a win — the whole point of ranking it
  last.

**Combos (A6, A7):** report interaction as
`ΔE1(combo) − [ΔE1(single_1) + ΔE1(single_2)]`. A positive interaction beyond
the CI is evidence the family compounds (scaffold both); a negative
interaction beyond the CI is evidence they interfere (scaffold at most one).
Combos never override a single-knob decision — they inform the *joint*
recommendation only.

## 5. Cost and wall-clock estimate

**Cost-meter semantics (grounded in `builder/operations.py::estimate_cost`).**
The meter reports **board runs per round** (each = one agent execution on one
board entry). Auxiliary LLM calls (`best-of-N propose calls`) are labelled and
**excluded from the board-run headline** but are real spend. Assumptions,
stated so the estimate is auditable:

- `target_1` board = **7 entries**. At the default `holdout_fraction=0.3`
  the hash-based split puts **zero** ids into the holdout (all 7 hash above
  the threshold) — which is exactly why the shared control pins
  `holdout_fraction: 0.6` (§2): the split becomes **train = 5, holdout = 2**
  (`q3_metrics_outline`, `every_expectation_kind_demo`), so the
  generalization-gap and holdout-confirm mechanics are live for the
  campaign instead of silently inert. (Verified by running `split_board`
  and `estimate_cost` on the arm contracts, §6.1.)
- Structure `gauntlet`, `field_size` pinned to **1** in the shared control
  (§2 — the meter defaults an unset `field_size` to `2`, but
  `GauntletStrategy.field_size()` hard-returns `1`, so pinning it makes the
  meter read the true runtime board-run count), `replicates` = 2. Per
  `estimate_cost`: `duel runs = field_size·replicates·train = 1·2·5 = 10`;
  `holdout-confirm = holdout·replicates = 2·2 = 4`. **BASE board
  runs/round = 14** — unchanged from the zero-holdout figure, so every
  downstream total in this section stands.
- Screen arms add `candidate-screen runs = proposes·best_of_n·panel =
  1·3·min(2,7) = 6` → **20 runs/round** (a +42.9% board-run premium — the
  exact quantity A2's decision rule prices).
- `recombine` arms: cost-neutral (no board-run change; propose calls drop to
  `best_of_n−1` on recombining rounds). `genealogy` / `calibration_feedback` /
  roles / `process_exemplars`: read-side → **cost meter untouched**, 14
  runs/round. `recombine_merge="llm"` adds ~1 aux merge call on merge rounds
  (not a board run).
- Auxiliary `best-of-N propose calls` = `proposes·best_of_n = 1·3 = 3` per
  round on every arm (aux LLM, not board runs).

**Per-arm board-run totals (× R=12 rounds × K=6 runs):**

| Arm | Runs/round | Board runs (× 12 × 6) | Aux propose calls (× 12 × 6) |
|---|---|---|---|
| A0 BASE | 14 | 1,008 | 216 |
| A1 +genealogy | 14 | 1,008 | 216 |
| A2 +screen | 20 | 1,440 | 216 |
| A3 +recombine(mech) | 14 | 1,008 | ≈180 (−1 on mint rounds) |
| A4 +recombine(llm) | 14 | 1,008 | ≈216 (+merge calls) |
| A5 +roles | 14 | 1,008 | 216 |
| A6 combo-R (gene+cal) | 14 | 1,008 | 216 |
| A7 combo-M (screen+recomb) | 20 | 1,440 | ≈180 |
| **Screening total** | — | **≈ 8,928 board runs** | **≈ 1,656 aux propose calls** |

**LLM-call envelope (assumption-driven).** Each `target_1` board run drives
the presentation agent's coordinator + 4 specialists ≈ **5 harness LLM
calls/run** (DOGFOOD-TARGETS.md §1.3 surface), plus ≈ 1–2 aux judge calls/run.
So the screening campaign's order-of-magnitude LLM spend:

```
harness calls ≈ 8,928 runs × 5   ≈ 44,640
aux judge     ≈ 8,928 runs × 1.5 ≈ 13,392
aux propose   ≈ 1,656 (slate)  + ~660 critique/merge ≈ 2,300
─────────────────────────────────────────────────────────
TOTAL         ≈ 60,000 LLM calls for the whole 8-arm screening campaign
```

**Wall-clock (secondary, high-variance).** At an assumed ≈ 0.8 s/LLM-call and
`--parallelism 4`, one board run (≈ 5 serial-ish harness calls) ≈ 4 s of
critical path; 8,928 runs / 4 parallel × 4 s ≈ **2.5 h of pure board-run
wall-clock**, plus proposer/reduce/gate overhead → budget **≈ 3.5–5.5 h total**
if arms run serially, or ≈ 1 h if the 8 arms run in parallel workspaces. These
are planning figures only; real latency is endpoint-dependent (§3.4).

**Confirmatory extension (pre-registered, separate budget):** K≈24 on ≤2
graduated arms ≈ 2 × (24/6) × ~1,200 board runs ≈ **9,600 additional board
runs** (~55k LLM calls again). Not part of this doc's authorization.

## 6. Execution runbook (GATED — do not run without the §0 go-ahead)

Every arm follows the same shape: fresh workspace → publish the arm's
contract → `zicato evolve` **with the dashboard** (house rule: `evolve`
launches the dashboard on `127.0.0.1:7892` by default; do **not** pass
`--no-dashboard`, do not pass a bind flag — `cli/commands/evolve.py`
`--dashboard-port` default `7892`, bound on loopback) → `epoch close` →
`reflect run`. The `target_1` registration mirrors the vendored example's
adapter/mutable-tree wiring.

### 6.1 Instrument dry-run first (deterministic, zero endpoint — not gated)

Before any live arm, prove the harness/cost-meter/analytics chain end-to-end
on the **deterministic** `target_0` (its scripted proposer needs no
endpoint), exactly as `examples/zicato_examples/target_0_convergence/RUN.md`
§"End-to-end demo". This validates wiring under a known answer (dev-guide
§1.8: v0 3.6 → v3 1.2) with **no** live-run gate, the same way `cascade_oc.py`
validates the statistics offline before any real spend. Also dry-run
`zicato builder`'s cost meter on each arm's `scoring.json` to confirm the §5
board-run numbers before committing budget.

### 6.2 Per-arm live runbook (one block per arm; A0 shown)

```bash
# --- Arm A0 BASE, replicate run k (repeat k = 1..6, fresh dir each) ---
WS=/tmp/campaign/A0/k${k}
rm -rf "$WS" && mkdir -p "$WS" && cd "$WS"
EX=/home/sunil/git/zicato/examples/zicato_examples/target_1_presentation

zicato init --workspace .zicato
# Register the ADK agent by its DOTTED IMPORT PATH + the vetted mutable
# subtree (the example's own RUN.md — evolve resolves the adapter via
# importlib, so this MUST be a module path, not a filesystem path):
zicato register --workspace .zicato \
    --adk zicato_examples.target_1_presentation.agent.agent:root_agent \
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

# With the epoch open, inspect the mutation surface + eyeball the cost
# meter BEFORE spending (no run yet):
zicato mutations --workspace .zicato
# (optional) open the builder to eyeball the cost meter for this scoring.json

# GATED: only after explicit operator go-ahead for this arm ---------------
zicato evolve --workspace .zicato --rounds 12 \
    --harness-call-llm   <operator harness endpoint dotted path> \
    --auxiliary-call-llm <operator auxiliary endpoint dotted path>
# evolve prints:  Dashboard: http://127.0.0.1:7892   (watch the bracket live)

# After the loop settles:
zicato epoch close   --workspace .zicato          # → analysis.md / analysis.html
zicato reflect run   --workspace .zicato          # MSA pass over the eval contract
```

- **A1–A4, A6, A7** are identical except the `--scoring` file is the arm's §2
  delta.
- **A5 (roles)** additionally writes the `models.proposer_breadth` /
  `models.proposer_depth` block into the workspace `config.json` before
  `evolve` (its `scoring.json` == A0's).
- **A0-ablation** (`best_of_n=1`) and the **process_exemplars** extension arm
  reuse the A0 block with the one-field change.

### 6.3 Monitoring checklist (per arm, per run)

- **Log stream:** watch for `preflight_signal_below_floor` /
  `preflight_saturated_contract` at evolve start (dev-guide §9 — a saturated
  contract means the arm cannot resolve any improvement; abort and
  investigate). Watch `margin_below_noise_floor` (E5), `stalled_loop`,
  `placebo_promoted` (CRITICAL — the decision procedure is broken, every
  recent "win" is suspect; dev-guide §11).
- **Publication LIVING DRAFT:** the dashboard's publication tab regenerates
  its deterministic sections every settled round (PUBLICATION freshness
  model). Eyeball §5 Statistical integrity and §3 Reign narrative as they
  fill in — a promotion whose gate margin sits below the A/A floor is a
  suspect promotion.
- **Board-status surface:** watch the `generalization_gap` and holdout budget
  panels (dev-guide §12 #5, §5) — a widening gap on a read-side arm
  (genealogy, calibration, exemplars) is the overfitting alarm those arms
  exist to be checked against.

### 6.4 Abort criteria (any one → stop that run, log, do not silently continue)

1. `placebo_promoted` CRITICAL fires (decision procedure compromised — the
   whole arm's evidence is void until explained).
2. Pre-flight verdict `refuse` (signal ≤ floor) at evolve start — the arm's
   contract cannot out-signal its own noise; fix before spending rounds.
3. A stream of `inconclusive` dead-letters (if the evidence gate were ever
   enabled) or a `stalled_loop` across the full round budget with zero
   promotions on **BASE** — indicates a target/endpoint problem, not a knob
   effect; halt the whole campaign, not just the arm.
4. Wall-clock or spend exceeds 1.5× the §5 planning estimate for the arm —
   stop and re-price before continuing (cost variance, §3.4).
5. Any run whose infra-abort rate (§dev-guide §1.6 `is_infra_abort_cause`)
   dominates real measurements — those are endpoint outages, not signal;
   the run is void and re-run, never averaged in.

## 7. Cross-references

| Topic | Source |
|---|---|
| The knob defaults + validation + omit-at-default | `src/zicato/core/scoring_config.py` (`ProposerQualityConfig`, `recommended_scaffold_weights`) |
| Cost-meter semantics (board runs, aux calls, screen/recombine terms) | `src/zicato/builder/operations.py::estimate_cost` |
| Breadth/depth role wiring (why roles are runtime, not contract) | `src/zicato/proposer/best_of_n.py`, `src/zicato/models_config.py` |
| Noise doctrine, A/A floor, replication power, planted-delta method | dev-guide `04-evaluation-statistics.md` §§3,4,13 |
| Offline power-harness precedent + report style | `tests/test_decision_procedure_power.py`, `tools/cascade_oc.py` |
| The dogfood ladder (target_0 dry-run → target_1 live) | `docs/design/DOGFOOD-TARGETS.md` §1 |
| The analytics that consume the results | `docs/design/PUBLICATION.md`, `docs/design/ANALYTICAL-INDEX.md`, `tournament/detail.py` |
| Recombination known-answer oracle (why recombine ranks #1) | dev-guide `04` §1.8, `tests/test_recombination_known_answer.py` |
| Overfitting boundary rules (why process_exemplars ranks last) | `docs/design/OVERFITTING.md`, dev-guide `04` §12 |
| The live-run gate | dev-guide `14-goals-and-roadmap.md` §"Endpoint-gated backlog"; the G3 gate above |
