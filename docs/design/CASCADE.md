# Evaluation cascade — staged screen → rung → full → holdout under one budget

> **Status.** DESIGN / **NOT IMPLEMENTED**. This is the staged-evaluation
> design note the deferred register owes. zicato already ships **four
> partial cascade forms** — the pre-tournament candidate screen, racing's
> board-slice rungs, the full-board promote gate, and the Ladder-mediated
> holdout — each independently configured, each with its own reserved
> replicate base. This note asks whether to **unify them as one declared
> `screen → rung → full → holdout` pipeline with per-stage budgets**, and
> — more importantly — treats the reason the unification was *deferred
> rather than built*: **stage thresholds interact with the gate's
> statistics.** Each stage's cut is a selection event, and selection
> compounds bias in what survives to the gate (a winner's curse *per
> stage*). A cascade that is not explicitly noise-aware at every rung
> would deliver a final "survivor" whose apparent margin over the champion
> is inflated by the sum of the upstream selection biases — and promote
> noise the single-stage gate would have caught. No source, config schema,
> or test in the tree changes because of this note. **What gates a build
> decision is the OC-harness evidence in §4**, not this argument.

This is the companion to five shipped docs and two research notes:

- [`SCORING.md`](SCORING.md) — the scalar loss and the three-rule promote
  gate the cascade terminates in.
- [`SELECTION.md`](SELECTION.md) / [`TOURNAMENT-STRUCTURES.md`](TOURNAMENT-STRUCTURES.md)
  — the gauntlet gate and the five schedulers; racing (`§3.5`) is the
  shipped board-slice rung mechanism.
- [`OVERFITTING.md`](OVERFITTING.md) — the train/holdout split, the Ladder,
  and the reused-holdout hazard the terminal stage inherits.
- [`SELECTION-THEORY.md`](SELECTION-THEORY.md) — the winner's-curse /
  optimizer's-curse treatment and the **replicate-first, resolve-second**
  operating rule this note lifts to a *per-stage* discipline.
- dev-guide `04-evaluation-statistics.md` — the noise doctrine, the A/A
  floor, the evidence gate, the placebo arm, the reserved replicate-base
  ledger, and the power-harness methodology every claim below rests on.

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
| **Rung** (racing, downstream) | `strategies/racing.py` (`TOURNAMENT-STRUCTURES.md §3.5`) | **rank-and-halve** — eliminate the worst `1 − 1/eta` by scalar per rung; escalating slice = escalating sample; gate applied only at the **final** rung | rung-0 `board_fraction`/`rung0_board_size`, escalating to full | `0` (real duel slots) | best-arm identification, margin-blind cut |
| **Full** (the gate) | `tournament/gate.py::evaluate_gate` | the three-rule ladder — scalar margin, pass-rate monotonicity, namespace monotonicity | full board × `replicates` × both sides | `0` | the promotion decision itself |
| **Holdout** (terminal confirm) | `tournament/ladder.py` + gate rule 4 | **Ladder-mediated confirmation** — a train-win must hold on a slice the proposer never saw; released only when the train improvement clears the threshold, budgeted per epoch | the `holdout`-tagged slice | (holdout entries, canonical slots) | anti-memorization guard, confirmation-only |

Two of these already **name each other as complementary**: the screen's
own module docstring states the relationship precisely — the screen runs
*upstream* inside one propose-step (before a child is minted into
lineage), racing's rung-0 halving runs *downstream* on applied lineage
children, and "the two compose: the screen keeps a broken candidate out of
the field, racing prunes the mediocre field members." The full gate and
the holdout compose the same way at the other end: the gate decides on the
train slice, the holdout confirms on a slice the optimizer never queried.

**The gap this note names.** These four are configured through four
unrelated surfaces (`proposer_quality` best-of-N params, the racing
`tournament_structure` params, `ScoringWeights`, and the `overfitting` +
`ladder` blocks), draw from three different reserved bases, and — critically
— **no single object reasons about the compounding selection bias across
all four.** The candidate that reaches the holdout has survived up to
three prior selective cuts. Its train scalar is optimistically biased by
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
   embodies — cheap-and-many early, expensive-and-few late — instead of
   four numbers that can silently sum to an unaffordable bill.
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

The protected-incumbent invariant is **unchanged and strictly
strengthened**: every stage before the terminal gate can only *narrow the
field* (a screen veto, a rung elimination); only the full gate + holdout
can promote, and both retain their shipped rules verbatim. A cascade
mis-cut at an early rung costs at worst a wasted confirmation — never an
unsafe promotion — exactly as `SELECTION-THEORY.md §1`'s resolver-vs-gate
split already guarantees.

---

## 3. The statistical core — why staged selection is not free

This is the section the deferral exists for. A cascade is a *sequence of
statistical tests on noisy draws*, and the noise doctrine
(`04-evaluation-statistics.md §3`) demands that every one of them state a
noise model and carry a measured false-cut rate. Three coupled facts make
a naive cascade unsound.

### 3.1 The per-stage false-cut rate scales with the slice-size floor

The A/A noise floor is not a single number — it is a *function of the
board slice a stage evaluates on.* The measured full-board floor is
`≈ 1.6·sqrt(σ(1−σ))` for the σ-harness structure (fact #8,
`04-evaluation-statistics.md §3.1`; ≈0.663 at σ=0.22). But a stage's
scalar is a **mean over its slice**, so the standard deviation of that
mean scales as `≈ 1/sqrt(m)` in the slice size `m`. A rung that cuts on a
`board_fraction=0.25` slice measures against a floor roughly **twice** the
full-board floor; the screen's 1–2-entry panel is noisier still.

The consequence, stated as an operating characteristic a build must
measure:

> **The probability that a stage eliminates a genuinely-better candidate
> (a false cut) is governed by that stage's *own* slice-size floor, not
> the full-board floor.** An early rung cutting by rank on a quarter-board
> slice has a materially higher false-cut rate than the terminal gate.
> Planting a true effect at a fixed multiple of the *full-board* floor
> and asking "does the cascade keep it?" is the wrong experiment; the
> effect must be sized against **each stage's** floor.

This is not a new hazard — it is exactly why racing's rung-0 cut is
margin-based best-arm identification rather than a gate (the gate would
falsely reject on that noisy slice), and why the screen is **veto-first,
never ranking** (`screen.py`): a 2-entry screen ranking close candidates
is "approximately random choice plus winner's curse" (`§3.3` of the
dev-guide's screen doctrine), so the screen is only allowed to decide the
one thing detectable at n=1 — categorical breakage — and confirms even
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
  (`04-evaluation-statistics.md §6.2`, §8) exist: an evidence replicate
  that cache-read an upstream stage's sample would "replay one identical
  sample into the fit … shrinking its SE by repetition alone" — an
  unsound-promotion path (bug #8). A cascade's soundness depends
  *entirely* on each stage drawing fresh.
- **If stage k+1 reuses a cached upstream score**, the bias **carries
  forward and compounds.** After `N` selective stages that each reuse
  prior scores, the final survivor's apparent margin over the champion is
  inflated by the *sum* of the per-stage selection biases. A margin
  calibrated to a single full-board A/A floor is then far too permissive.

The escalating-slice structure of racing partially launders this: each
rung runs a *larger* slice, and if the larger slice is drawn fresh (not a
superset cache-hit of the smaller), the survivor is re-measured with more
signal. But racing today caches at base `0` and escalates the *slice*, not
the *replicate index* — so whether a rung's larger slice is genuinely
selection-independent of the smaller rung that fed it is precisely the
kind of claim the doctrine forbids asserting without measurement
(`04-evaluation-statistics.md §3.2`). **A unified cascade must make
"every stage's draw is selection-independent of every prior stage's cut" a
measured invariant, not an assumption.**

### 3.3 What the terminal gate's evidence requirement must become

A candidate reaching the holdout has been selected up to three times. The
correction the terminal stages must apply, built entirely from machinery
zicato already ships:

1. **The final gate measures on a fresh draw, never a cached stage score.**
   Already the canonical-r0 + both-sides-fresh rule
   (`04-evaluation-statistics.md §6.2`, §7.3). The cascade must guarantee
   the terminal full-board evaluation is drawn independently of every rung
   that selected the survivor — the reserved-base discipline extended to
   name the whole ordered pipeline.
2. **The evidence gate provides the selection-independent re-measurement.**
   The Bradley–Terry pre-gate (`evidence_gate.py`,
   `04-evaluation-statistics.md §6`) fits over fresh reserved-base-`4000`
   draws and crowns only on CI *separation* — a test noise cannot pass by
   selection luck ("~37 duels of an essentially unbroken win streak," fact
   #2). This is the natural home for the *N-stage correction*: **the more
   selective the upstream cascade, the larger the terminal replicate
   budget must be.** A candidate that survived three cuts should face a
   stiffer confirmation than one that went straight to the gate, because
   its train margin is more inflated. Concretely, the evidence gate's
   `promote_confidence_replicates` should be a **function of upstream
   selectivity** (how many stages, how selective each) rather than a fixed
   constant — the cascade knows the selectivity; the standalone gate does
   not.
3. **The holdout is the one slice no stage selected on.** The train/holdout
   split (`board/split.py`) already withholds the holdout from proposer
   context, pattern detection, screen panels, and loss summaries
   (`OVERFITTING.md §11`; `04-evaluation-statistics.md §12`). Because *no
   cascade stage is ever eligible to read it*, the holdout confirmation is
   structurally selection-independent of the entire upstream pipeline — it
   is the cascade's de-biasing anchor. The Ladder's reused-holdout budget
   (`§5` of the dev-guide) already governs how many times that anchor can
   be queried before it too "gets used up."
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

## 4. The OC harness — proving a cascade sound BEFORE building it

Per the noise doctrine's non-negotiable rule — *operating characteristics
are measured, not asserted* (`04-evaluation-statistics.md §3.2`, §13) — no
cascade ships until a known-answer harness demonstrates it. This section
is concrete enough to build the harness from alone; it extends the
existing power harness (`tests/test_decision_procedure_power.py`, Tier 2
of the convergence harness) rather than inventing a new instrument.

### 4.1 Reuse the seeded-noise substrate

The harness inherits the target_0 example world verbatim
(`examples/zicato_examples/target_0_convergence/harness.py`): `stable_noise_seed`
derives the RNG seed **only** from
`(workspace_seed, generation_id, entry_id, replicate_index)` — no wall
clock, no global RNG (`04-evaluation-statistics.md §13.1`). This is what
makes cascade trials exactly reproducible and lets "sides vary because the
generation id is in the seed" serve as the A/A premise. The planted-effect
vocabulary is the existing `DELTA_CASES` (0.5×/1×/3× the **full-board**
floor) *plus a new requirement*: effects sized against **each stage's
slice floor** (§4.2). Drive the real machinery, monkeypatch only
`runner._run_single` (`§13.2`).

### 4.2 Experiment A — per-stage false-cut rate vs the slice floor

The foundational measurement (§3.1). For each stage's slice size `m_k`:

1. **Measure the slice-k floor.** Run K seeded A/A draws of the champion
   on the `m_k`-entry slice through the same `_run_board_units_fast`
   calibration path (`calibration.py`), at a reserved base — this is the
   existing `measure_noise_floor` restricted to the slice. Assert the
   floor grows as `m_k` shrinks (roughly `∝ 1/sqrt(m_k)`); a floor that
   *did not* grow on a smaller slice would mean the seeding stopped
   varying (the §4-floor bug class).
2. **Plant a true effect at multiples of the slice-k floor** — not the
   full-board floor. Extend the `sometimes-<pct>-<token>` vocabulary
   (`§13.4`) so a δ can be sized to `{0.5, 1, 3}×` the *slice*'s floor.
3. **Measure P(the better arm is cut at stage k).** Run the stage's real
   cut rule (screen veto / racing rung rank-halve) over the seeded trial
   range; count how often the genuinely-better arm is eliminated.
4. **Pin the coarse-cut discipline.** Assert the *veto* stage's false-cut
   rate follows the confirm-before-veto squaring (≈ flip-rate², fact #7)
   and the *rank* stage's false-cut rate is acceptable **only** at
   effects ≥ 1× its own slice floor — documenting that a stage may not be
   trusted to resolve effects below its slice floor.

### 4.3 Experiment B — end-to-end P(promote | ·), cascade ON vs OFF

The headline decision measurement. On the **identical seeded draws**:

- **Null (the cascade placebo).** Field an identical arm
  (`{"champion": BASE, "challenger": BASE}`) and run it through the *whole*
  pipeline. Measure `P(promote | null)` with the cascade ON and with it OFF
  (today's single-stage full-board contract). The doctrine's fact #4 —
  "the evidence-gated contract's false-promotion rate under the A/A null is
  zero" — is the bar: **the cascade must not raise `P(promote | null)`
  above the single-stage contract's rate.** If compounding selection
  leaks a nonzero null-promotion rate, the cascade is unsound and does not
  ship, full stop.
- **True effect.** Plant δ at `{0.5, 1, 3}×` the full-board floor and
  measure `P(promote | true-improvement)` ON vs OFF. Pin `power == 1.0` at
  3× (unmissable, fact #6) and monotonicity `small ≤ medium ≤ large` — and
  crucially, that the cascade's power at each δ is **≥ the single-stage
  contract's power minus a stated tolerance.** A cascade that saves budget
  by *losing* real improvements at the early rungs is a power regression;
  the harness must quantify exactly how much power the staging costs.
- **Include the failing alternative as documentation** (`§13.5`): the
  naive "run every stage's cut as a gate" rule, shown hot on the same
  seeded draws, so the comparison is between rules, not samples.

### 4.4 Experiment C — the budget-savings-vs-power curve

The build-decision artifact. A cascade's *only* justification is that it
reaches a target power at **fewer total board-unit evaluations** than
running the full board on every candidate. Plot, at a fixed planted δ:

- **x-axis:** total board-unit evaluations spent per promotion (summed
  across all stages, counting each reserved-base draw once).
- **y-axis:** power (`P(promote | true)`) at that δ.

Sweep the cascade's stage allocation (screen panel size, rung `eta` /
`board_fraction`, terminal `replicates`) and overlay the single-stage
contract as a reference point. The cascade earns a build **iff** a
configuration exists that reaches the reference power at **materially
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

## 5. The config sketch (NOT implemented)

The endorsed shape is **one nested frozen block** on the contract, layered
under the existing `tournament_structure` — not a new top-level structure,
and not four independent knobs. It follows the omit-at-default discipline
(`03-contract-and-epochs.md §"Omit-at-default"`; `SCORING.md §2.4`) so that
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
    // the N-stage correction (§3.3.2): terminal evidence budget scales
    // with measured upstream selectivity rather than a fixed constant.
    "terminal_evidence": { "scale_with_selectivity": true, "min_replicates": 8 }
  }
}
```

Design properties this sketch commits to:

- **`cascade` is a nested frozen dataclass**, so it recurses through
  `scoring_to_canon` exactly like the shipped `overfitting` / `ladder` /
  `tournament_structure` blocks (`03-contract-and-epochs.md §3.2`), and the
  same omit-at-default check applies to each field. **It rolls the epoch on
  change** — a cascade edits *what a promotion means*, the same rationale
  as any `tournament_structure` change (`TOURNAMENT-STRUCTURES.md §4.1`).
- **Default = empty stage list ⇒ today's behavior**: the screen, racing,
  gate, and holdout run exactly as they do now, independently configured.
  The cascade block is purely additive opt-in.
- **Each stage names its own reserved base** at load, extending the ledger
  (`04-evaluation-statistics.md §8.1`) — the loader assigns and
  cross-checks bases so §4.5's independence invariant holds by
  construction. The next free base is `6000`.
- **The terminal gate, the holdout/Ladder, the evidence gate, and the
  `SelectionStrategy` seam are all unchanged** — the cascade *orders and
  budgets* them; it does not reimplement any of them.

This section is a sketch of where the block would attach. **No such key
exists in the loader, the strategies, or the tests today.**

---

## 6. Relationship to the partial forms — what unification absorbs

| Shipped form | Under a cascade | Deprecated? |
|---|---|---|
| Candidate screen (`epoch/screen.py`) | becomes **stage 0** (`kind: screen`, veto-first, confirm-before-veto retained verbatim) | the standalone `proposer_quality` screen wiring is **absorbed**, not removed — a cascade with no `screen` stage runs today's screen unchanged |
| Racing rungs (`strategies/racing.py`) | become the **middle `rung` stages** (rank-halve, escalating slice) | racing as a standalone `tournament_structure` **stays**; the cascade merely lets its rungs be interleaved with a screen and an explicit terminal budget |
| Full gate (`tournament/gate.py`) | becomes the penultimate **`full` stage** | **unchanged** — the three-rule ladder is the cut rule, verbatim |
| Holdout / Ladder (`ladder.py`, gate rule 4) | becomes the terminal **`holdout` stage** | **unchanged** — Ladder release + budget rules retained; the cascade only guarantees it is the never-selected-on anchor |
| A/A calibration (`calibration.py`) | **not a stage** — a measurement the cascade *consumes* (per-slice floors, §4.2) | unchanged |
| Evidence gate (`evidence_gate.py`) | **not a stage** — the terminal confirmation whose budget the cascade *scales with selectivity* (§3.3.2) | unchanged; opt-in as today |
| Placebo arm (`evolve/placebo.py`) | **not a stage** — the whole-pipeline control (§3.3.4) | unchanged; its finding is elevated to cascade-level |

The honest read: unification is a **configuration and accounting**
change — one ordered spec, one budget ledger, one reserved-base
allocation, and one place that scales the terminal evidence with upstream
selectivity — over four mechanisms that already exist and already compose
pairwise. It **absorbs** their independent wiring; it **deprecates
nothing** operators rely on, because the empty default is byte-identical to
today. Nothing here weakens the protected-incumbent invariant, the noise
doctrine, or the overfitting boundary — a cascade that tried to would fail
Experiment B's null bar (§4.3) and never ship.

---

## 7. STATUS and the build-decision gate

- **Status: DESIGN / NOT IMPLEMENTED.** No loader, strategy, config field,
  or test in the tree reads a `cascade` block. The four partial forms ship
  and are documented in their own docs; this note only proposes unifying
  them.
- **What gates a build decision:** the **OC-harness evidence in §4**,
  specifically:
  1. Experiment A shows per-stage false-cut rates that respect each
     stage's *slice* floor and the coarse-cut discipline (§4.2).
  2. Experiment B shows the cascade does **not** raise `P(promote | null)`
     above the single-stage contract's rate (the hard soundness bar,
     fact #4) **and** holds power within a stated tolerance of the
     single-stage contract at every planted δ (§4.3).
  3. Experiment C exhibits at least one stage allocation that reaches the
     reference power at **materially lower** total board-units while
     passing (2) — the *only* thing that justifies the added machinery
     (§4.4).
  4. §4.5's slot-integrity test proves cross-stage draw independence.
- **A legitimate outcome is "do not build."** If Experiment C finds no
  budget-saving configuration that preserves soundness and power, the
  correct decision is to keep the four forms independently wired and leave
  this note deferred. The harness is designed to be able to *reject* the
  cascade, not only to bless it.

---

## 8. Cross-references

| Topic | Document |
|---|---|
| The scalar loss + the three-rule gate the cascade terminates in | [`SCORING.md`](SCORING.md) |
| The gauntlet gate, the schedulers, racing's board-slice rungs (`§3.5`) | [`SELECTION.md`](SELECTION.md), [`TOURNAMENT-STRUCTURES.md`](TOURNAMENT-STRUCTURES.md) |
| Winner's curse / optimizer's curse, replicate-first-resolve-second | [`SELECTION-THEORY.md`](SELECTION-THEORY.md) |
| Train/holdout split, the Ladder, restricted proposer visibility | [`OVERFITTING.md`](OVERFITTING.md) |
| The noise doctrine, A/A floors, evidence gate, placebo, reserved bases, the power-harness methodology | dev-guide `04-evaluation-statistics.md` |
| The candidate screen's veto-first / confirm-before-veto doctrine | `src/zicato/epoch/screen.py`, `04-evaluation-statistics.md §3.3` |
| The contract hash + omit-at-default discipline the config sketch follows | `03-contract-and-epochs.md`, [`EPOCHS-AND-JOURNALING.md`](EPOCHS-AND-JOURNALING.md) |
