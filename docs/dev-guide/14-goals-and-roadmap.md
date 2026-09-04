# 14 — Goals and Roadmap

> **Covers:** the goal and its operational definition of "effective"; the
> current proof state (what is proven without a model endpoint, and what
> awaits a serving endpoint); the endpoint-gated backlog as the operator's
> runbook, with per-item preconditions, commands, measurements, and gate
> criteria; the deferred register with its reasoning frozen; the discipline
> for proposing new work; and the anti-goals — the things zicato does not do.
>
> **Prerequisites:** 01-orientation.md (the loop), 04-evaluation-statistics.md
> (the noise doctrine — every item below is downstream of it),
> 12-bug-casebook.md §"The meta-lessons" (the failure shapes new work must
> not repeat).
>
> **Invariants introduced in this chapter:**
> 1. **"Effective" is a measured property**, defined by the standing
>    convergence and operating-characteristics proofs rather than by a
>    demonstration or a single lucky run.
> 2. **No live evolve run starts without the operator's explicit go-ahead**,
>    and every live launch enables the dashboard and reports its URL.
>    Agents verify with test suites and CI, never with live runs.
> 3. **Deferring an item is itself a decision.** Every deferred item carries
>    its frozen reasoning and its un-deferral trigger; re-opening one means
>    engaging that reasoning rather than rediscovering the problem.
> 4. **New work near the overfitting boundary or the contract hash is
>    design-first.** Code without the design note is not reviewable.
> 5. **Nothing in git references the model vendor.** No names, no model ids,
>    no trailers. This is a durable repo rule, enforced by scan on every
>    branch.

---

## 1. The goal

zicato exists to be a **self-improvement harness whose effectiveness is
measured**: a loop that takes a target system under a frozen evaluation
contract, proposes targeted mutations to its declared mutable surface,
measures each candidate under tournament conditions, and promotes only what a
noise-aware decision procedure can separate from the incumbent. It journals
enough evidence that a human can audit every crowning after the fact.

The measurement requirement carries the whole goal, because the failure it
guards against is **a loop that runs convincingly while proving nothing**.
Two such results are on record. A live run produced a null: every probe
scored identically, the `1.000000` saturation signature. A noise-blind
default gate, once measured, promoted a challenger identical to its champion
in a third of seeded trials (04-evaluation-statistics.md §3.1, fact #1). A
harness that cannot separate improvement from noise reports progress it has
not made.

### 1.1 What "effective" means operationally

"Effective" has a standing, machine-checked definition. It is the conjunction
of two proofs, both living in the test suite, both required to stay green
forever:

1. **The convergence proof** — `tests/test_convergence_known_answer.py` over
   the deterministic convergence example
   (`examples/zicato_examples/target_0_convergence/`). The whole shipped loop
   runs: propose, apply, validate, subprocess tournament workers, reduce,
   gate, persist, under the default git generation store, with nothing on the
   tournament side stubbed. On a target carrying a deliberately introduced
   defect (a planted defect), the loop converges to a floor that can be
   computed by hand: scalars 3.6 → 2.4 → (3.6 rejected, the negative control)
   → 1.2, decisions `promoted → rejected → promoted`. This is the existence
   proof: when improvement is real and measurable the loop finds it, and when
   a candidate is strictly worse the gate refuses it.

2. **The operating-characteristics proof** —
   `tests/test_decision_procedure_power.py`. The decision procedure itself,
   under seeded noise that mimics production (agents vary, judges are LLMs).
   Under the effective contract, a generation dueled against itself (an A/A
   null) promotes nothing. Power at planted effects of 0.5×, 1×, and 3× the
   measured noise floor rises monotonically and reaches 1.0 at 3×. The naive
   procedure's failures are pinned as documentation beside the effective
   procedure's recoveries on the same draws. This is the soundness proof: the
   loop's yes/no holds up under the noise it will face.

Everything else in the repository — calibration, pre-flight, the Ladder, the
evidence gate, screening, the placebo arm — is instrumentation that keeps
those two proofs true as the system grows. A change that leaves both proofs
green and their pinned numbers honest is progress. A change that needs either
proof weakened is not.

> ✅ ALWAYS treat these two test files as the definition of done for any work
> touching the loop. "My feature works and the convergence and power suites
> are green with unchanged pinned numbers" is a complete claim. "My feature
> works" on its own states nothing a reviewer can check.

### 1.2 The dogfood escalation ladder

`docs/design/DOGFOOD-TARGETS.md` fixes the sequence past the backlog: the
known-answer instrument, then three progressively harder targets. Every
target is named early, because each one forces architectural needs the design
has to accommodate before that rung is reached.

| Rung | Target | When it is taken on | What it forces |
|---|---|---|---|
| 0 | the deterministic convergence example (`target_0_convergence`) — a policy agent carrying a planted defect | already standing as the convergence proof; backlog item 1 takes it live | nothing new; it is the known-answer instrument |
| 1 | the presentation agent (`target_1_presentation`) — a multi-agent presentation coordinator with fifteen mutation points across specialist instructions, coordinator routing, tool descriptions, path logic, and topic naming | the first dogfood; backlog item 2 | a real agent with real failure modes whose drift signal is the loss; it exercises the shipped stack end to end |
| 2 | the sibling project's steering layer (`target_2_goldfive_steering`) | after rung 1 has produced live, audited improvement | cross-repo mutation; a loss signal that is not drift |
| 3 | zicato itself | after rungs 1 and 2 have produced live, audited improvement | nested zicato instances; recursion guards |

The earlier rungs exist to validate the loop before it is trusted on itself.
Rung 3 — zicato evolving zicato — is the end state the whole soundness
program exists to make safe: you do not point a self-improvement loop at its
own decision procedure until that procedure's operating characteristics are
proven and its promotion path has no second door (§6). Nothing on rung 3
begins until rungs 1 and 2 have produced live, audited improvement.

The presentation agent's expected drift movements are pre-registered in
`DOGFOOD-TARGETS.md` §1.4: confabulation is answered by tightening the
researcher, capability mismatch by encoding pipeline order in the
coordinator, and looping by adding exit conditions to the reviser. Treat that
table as working hypotheses for the first live epoch to confirm or falsify.

### 1.3 The known-answer instrument, in detail

The deterministic convergence example can serve as "the definition of done"
(§1.1) because its scalar can be computed by hand. The example ships a
deterministic harness and a scalar that lands on a fixed floor
(`examples/zicato_examples/target_0_convergence/`;
`tests/test_convergence_known_answer.py`). The formula:

```
scalar(k, passes) = 1.0 * k  +  1.0 * (1 - passes/5)     # k = tokens, /5 = board size
    v0 (3 tokens, 2/5 pass) = 3.6      — seeded baseline
    v1 (2 tokens, 3/5 pass) = 2.4      — round 1, PROMOTED
    v2 (3 tokens, 2/5 pass) = 3.6      — round 2, REJECTED (the negative control)
    v3 (1 token,  4/5 pass) = 1.2      — round 3, PROMOTED = THE FLOOR
```

`test_gauntlet_converges_to_known_floor` runs three real rounds — propose,
apply, validate, subprocess workers, reduce, gate, persist, on the git
backend, with nothing on the tournament side stubbed. It asserts three
things: that the decision sequence is `["promoted", "rejected", "promoted"]`;
that each parent and child scalar equals the formula's value (`EXPECTED_V0 ==
3.6`, `EXPECTED_V1 == 2.4`, `EXPECTED_FLOOR == 1.2`); and that the
negative-control child (v2) *regresses* against its parent, so the gate must
refuse it. That is the executable form of "when improvement is real the loop
finds it, and when a candidate is strictly worse the gate refuses it".

Three properties make the convergence example an instrument rather than a
demonstration:

1. **The floor is exact.** It is `1.2`, computed from the shipped
   `builtin_scalar`, so any drift in the scoring path, the reducer, the gate,
   or the worker boundary moves a pinned number and fails the test. A test
   asserting only "the scalar went down" would pass through a dozen silent
   bugs.
2. **The negative control is first-class.** Round 2 is a challenger the gate
   must reject, so a loop that promoted everything is caught in CI rather
   than in production. "Effective" requires both halves: find the improvement
   and refuse the regression.
3. **No model endpoint is involved.** The whole run uses a scripted proposer
   and a deterministic harness, so it reproduces byte for byte in CI. That is
   why backlog item 1 (live convergence on this example, §3) exists: swap the
   scripted proposer for a real one and ask whether a real proposer finds the
   same planted defects.

> ✅ ALWAYS treat a red pinned number in the convergence oracle as a contract
> breach in the loop rather than a stale golden to re-capture. The floor is
> derivable by hand; if it moved, either the derivation changed (update the
> doc and the comment) or a bug entered the scoring, gate, or worker path (fix
> the bug). See 11-testing.md §11.7.8 for the general rule, and the
> calibration false-zero-floor case (`12-bug-casebook.md` case 3) for what a
> silently-broken scalar path looks like.

### 1.4 Rung 3 and why self-improvement is gated on soundness

The escalation ladder (§1.2) ends at **zicato evolving zicato**, and the whole
soundness program exists so that rung can be attempted safely. The design
already carries the seam: `RuntimeConfig.instance_id` "distinguishes nested
instances when an outer zicato is optimizing an inner zicato"
(`src/zicato/core/runtime.py`), so workspaces, event streams, and lineage are
keyed per instance rather than colliding. Carrying the seam is not the same as
being ready to use it.

Rung 3 is last for one reason: **you do not point a self-improvement loop at
its own decision procedure until that procedure's operating characteristics
are proven and its promotion path has no second door.** If the inner loop's
gate could be satisfied without a real improvement (§6, "no gate-bypassing
shortcuts"), an outer loop optimizing the inner one would find that route,
because raising the measured score is what an optimizer does. Two things make
an optimizer pointed at the gate tractable: the A/A soundness proof (§2.3)
and the single-promotion-door invariant. Nothing on rung 3 begins until rungs
1 and 2 have produced live, audited improvement (§1.2).

That is why the anti-goals (§6) are load-bearing. Each one closes a route by
which an inner optimizer could raise its measured score without improving
anything: a free-edit path, a silent default change, a health finding that
auto-rolls an epoch. Each looks harmless until something is optimizing
against it. The discipline that looks excessive at rung 0 is the precondition
for rung 3.

> ⚠️ TRAP — "the loop improved itself once" is not rung 3. A single lucky
> self-improvement is a demonstration, and §1 names the failure it resembles:
> a loop that runs convincingly while proving nothing. Rung 3 is the loop
> improving itself under the standing soundness guarantees, audited, and
> repeatedly — a measured property rather than an anecdote (invariant 1 of
> this chapter).

---

## 2. The current proof state

### 2.1 Proven endpoint-free (machine-checked in CI, standing)

Everything below runs with **no model endpoint anywhere**: deterministic
harnesses, scripted proposers, and seeded noise. That is what makes these
claims strong — no external dependency that can flake, reproducibility to the
byte, and nulls the harness can measure.

| Claim | Instrument |
|---|---|
| The full loop converges to a known answer through real workers, git backend, no stubs | `tests/test_convergence_known_answer.py` plus the convergence example's `RUN.md` CLI walkthrough |
| The gate rejects a strictly-worse child (negative control) | round 2 of the same oracle |
| The A/A noise floor is measurable: the deterministic harness measures 0.0 and the σ=0.22 harness measures approximately the analytic 0.663 | `test_aa_null_calibration_measures_the_noise_floor`; calibration end-to-end test |
| An under-margined, un-gated procedure promotes pure noise in 20 of 60 A/A trials; the evidence-gated procedure promotes none | `test_margin_below_noise_floor_without_evidence_gate_is_unsound`, `test_aa_effective_contract_false_promotion_rate_is_zero` |
| The Bradley–Terry gate is a soundness device (confidence intervals separate only after about 37 unbroken wins); replication is the power device (at 32 replicates, effects at half the noise floor reach about 3σ) | power harness (04-evaluation-statistics.md §3.1, facts #2–#3) |
| Evidence replicates are independent draws at reserved slots; canonical slots are never touched; duplicate draws are refused | `test_evidence_replicates_are_independent_draws`, `test_full_mode_evidence_loop_never_touches_canonical_slots` — the pins for the evidence-gate slot-reuse case (`12-bug-casebook.md` case 8) |
| The candidate screen vetoes a broken candidate deterministically (none of 12 forwarded, against all 12 forwarded unscreened), and under noise its false-veto rate is close to the square of the per-run flip rate | screen operating-characteristic tests in the power harness |
| Seeded noise crosses the real subprocess-worker boundary intact (reproducible, side-independent, replicate-independent) | `test_noisy_adapter_seeded_draws_cross_the_worker_boundary` |
| Each of the twelve documented bugs is pinned by a regression test that fails with the fix stashed | 12-bug-casebook.md, per case |

### 2.2 What awaits the endpoint

Every remaining claim takes the form *"and this holds when the measurements
come from a real serving model."* Seeded noise is a model of production
noise, and the endpoint-gated backlog (§3) exists to check that model against
reality. Five claims are unproven:

- live convergence — whether a **real proposer** finds real improvements on a
  real harness at all, and at what cost per accepted improvement;
- the **real** A/A noise floor of an LLM-backed harness (its magnitude,
  shape, and stationarity), and whether the shipped `promote_margin` and
  replication defaults are calibrated for it;
- real judge self-consistency (test–retest on live judges);
- the composed system — racing, Bradley–Terry evidence, best-of-N sampling,
  and screening together — under a real proposer's output distribution;
- the screen's live economics: cost per accepted improvement, with screening
  on and with it off.

Until those run, the honest status line is: **the mechanism is proven under
seeded noise; production noise is only modeled.** Do not claim more in any
document or commit message.

### 2.3 Reading the two proofs (so you can keep them green)

"Keep both proofs green with unchanged pinned numbers" (§1.1) is only
actionable if you know what each proof asserts and how to read a failure.

**The convergence oracle** (`tests/test_convergence_known_answer.py`) is the
*existence* proof, walked through in §1.3. Its failure modes and what they
mean:

| Failure | What it means |
|---|---|
| decision sequence ≠ `[promoted, rejected, promoted]` | the gate's yes/no changed — a scoring, reducer, or gate-threshold regression |
| a child scalar ≠ its `EXPECTED_*` | the scalar path drifted (a reducer, a weight default, or a worker-boundary desync — compare the `per_judge_weights` desync class, 03-contract-and-epochs.md §3.5) |
| the negative control (v2) stops regressing | the harness or the scoring path stops separating the worse candidate; the instrument itself is broken |

**The operating-characteristics oracle**
(`tests/test_decision_procedure_power.py`) is the *soundness* proof. It builds
a seeded-noise world that mimics production, where agents vary and judges
behave like language models, and pins the decision procedure's behavior. It
names the naive procedure's failures and the effective procedure's recoveries
on the same draws. Three tests carry the proof:

- `test_aa_null_calibration_measures_the_noise_floor` — a generation dueled
  against itself. The spread of the A/A `delta_scalar` is the noise floor.
  This test is where the claim "the floor is measurable" lives.
- `test_aa_effective_contract_false_promotion_rate_is_zero` — under the
  effective contract, every A/A trial ends with the incumbent standing:
  **zero false promotions**. The paired naive-contract test
  (`test_margin_below_noise_floor_without_evidence_gate_is_unsound`, cited in
  §2.1) promotes pure noise in a fraction of trials. The two tests together
  are the soundness claim.
- the planted-effect power sweep — effects at 0.5×, 1×, and 3× the floor,
  where power rises monotonically and reaches 1.0 at 3× (§1.1). The effect at
  half the floor reaches about 3σ through the effective contract's per-duel
  averaging and replication. Replication supplies the power; the
  Bradley–Terry gate supplies the soundness (04-evaluation-statistics.md
  §3.1, facts #2–#3).

Every statistical test prints its rates and pins a bound, so a failure shows
the measured number beside the threshold — the power-harness idiom (§5.4). A
new decision surface ships with a test in this file or it does not ship (§6,
"no untestable statistics").

> ⛔ NEVER "fix" a red oracle by loosening a pinned number or widening a bound
> to match a new measurement. A pinned number is a hypothesis about the
> machine (11-testing.md §11.7.8). If it moved, either your change altered the
> decision procedure — which is the thing under test, so say so explicitly and
> re-derive the number — or you introduced a bug. The one legitimate reason to
> re-pin is a declared decision-procedure change carrying its own design note
> and CHANGELOG entry (§5.3), never a quiet edit inside an unrelated PR.

> ⚠️ TRAP — a green power suite proves the procedure is sound under the seeded
> noise model. It says nothing about production noise. Closing that gap is
> what the backlog in §3 is for, because seeded noise is a model of production
> noise (§2.2). A change that keeps the suite green has preserved the modeled
> guarantee without verifying the model. Do not report "proven under real
> conditions" from a green CI run.

### 2.4 What proving without an endpoint buys

"No model endpoint anywhere" (§2.1) reads at first like a weakness, as though
the proofs avoid the real system. Running without an endpoint is what makes
these results proofs rather than anecdotes. Three properties follow from it,
and all three are load-bearing:

- **Reproducibility.** A seeded harness produces the same draws on every run,
  so a pinned number states an invariant rather than a threshold that can
  flake. Once a proof depends on a live endpoint it stops being reproducible,
  and a result nobody can reproduce cannot be checked.
- **Nulls the harness can measure.** The A/A null (§2.3) can be trusted only
  if the harness injects the noise it claims and nothing else. A deterministic
  or seeded harness lets the test measure the null it planted. A live
  endpoint's null cannot be measured, because a serving model cannot be asked
  to be as noisy today as it was yesterday.
- **No hidden environmental leakage.** The seed discipline — nothing about
  process ids, temp-directory names, or the clock reaches the measurement —
  is the statistics-side counterpart of the contract hash's
  identity-versus-location rule (03-contract-and-epochs.md §3.2.5; the
  contract-hash-embeds-the-checkout case, `12-bug-casebook.md` case 10). A
  measurement that varied with the temp directory would be as broken as a
  contract hash that varied with the working directory.

That is why "no untestable statistics" (§6) admits no exception. A decision
surface whose soundness can be checked only live has no reproducible null, so
its guarantee cannot be falsified in CI. The constraint produced better
design: seeding from stable identifiers exists because seeding from the
wall clock or the process id failed the reproducibility bar. The seeded
harness crossing the real subprocess-worker boundary intact (§2.1,
`test_noisy_adapter_seeded_draws_cross_the_worker_boundary`) shows the
discipline holds through the process split the live runs will use.

> ✅ ALWAYS design a new decision surface so its operating characteristics are
> checkable under a seeded harness in CI *before* writing it (§5.4). Being
> unable to see how to null-test it without an endpoint is a design problem to
> resolve now rather than a reason to defer verification to a live run. The
> backlog's live items check the model against reality; they do not replace a
> proof of the mechanism under seeded noise.

### 2.5 Keeping §2 honest as live items land

The proof state (§2.1 and §2.2) is a live claim, and the line between the two
lists moves in one direction only and only with evidence. When a backlog item
(§3) produces an audited live result, three rules govern the update to this
chapter:

- **A claim moves from §2.2 to §2.1 only once a live run has audited it.**
  Ready code does not qualify, and neither does one run that happened to
  succeed. "Effective is a measured property" (invariant 1) applies to the
  chapter's own claims. A single live convergence is one data point; the §2.1
  entries are standing, reproducible properties, and a live claim earns that
  status only when it is repeatable and audited. §1 names the failure this
  guards against: a loop trusted on one convincing run.
- **A live finding that contradicts a pinned endpoint-free number moves
  nothing.** It is a stop-and-investigate (§3, standing rule 4). Either the
  noise model was wrong — fix the model, re-derive the defaults, and re-pin
  with the reasoning stated — or the live wiring is broken, which makes it a
  casebook entry. The endpoint-free proof is never weakened to accommodate a
  live anecdote.
- **The honest status line updates with the lists.** It currently reads "the
  mechanism is proven under seeded noise; production noise is only modeled"
  (§2.2). As live items land that sentence changes, but only as far as the
  audited evidence supports, and no commit message or report may claim more
  than the §2.1 list holds.

This is the chapter's own form of "never make the loop's yes/no less
trustworthy than you found it": never let the proof-state claim outrun the
evidence. An over-claimed §2.1 is the same failure as an over-margined gate, a
statement of confidence the measurements do not support.

> ⚠️ TRAP — after a first successful live run the temptation is to move its
> claim into §2.1 and stop there. §2.1 is the set of properties that must stay
> green permanently, so admitting a claim commits you to keep proving it. A
> live property that cannot be made reproducible enough to stand in §2.1 stays
> in §2.2 with its evidence noted; a partial claim that is accurate serves the
> reader better than a settled one that is not.

---

## 3. The endpoint-gated backlog — the operator's runbook

These six items are queued waiting on a serving model endpoint. They are
ordered, because each item's interpretation depends on the ones before it. A
live convergence result cannot be interpreted without a live noise floor, and
a dogfood run cannot be interpreted while its harness is a structural null —
a setup in which no mutation the proposer can make changes any measured
output, so every candidate scores the same.

> ⛔ NEVER start any run in this section without the operator's **explicit
> go-ahead for that specific run**. This is a standing rule rather than a
> formality: live runs cost real money, occupy the workspace's runtime lock,
> and produce artifacts an operator may need to quarantine. Agent teams
> verify via test suites and CI. A live run is an operator decision that an
> agent executes, never an agent initiative.

> ✅ ALWAYS launch live evolve runs with the dashboard enabled (the default)
> and report the printed URL (default `http://127.0.0.1:7892`; do not pass
> `--dashboard-bind` unless the operator asks) in your first status message,
> before the first round settles. The operator watches the bracket live; a
> run whose URL was never reported is a run the operator cannot supervise.

### Item 1 — live convergence on the deterministic convergence example

*The known-answer run, with the scripted proposer replaced by a real one.*

- **What it proves:** a real proposer, reading the real mutation surface and
  loss summaries, can find the planted defects the scripted proposer was
  handed. This is the first live evidence that the propose step generates
  signal.
- **Preconditions:**
  - a serving endpoint wired through the `--harness-call-llm` and
    `--auxiliary-call-llm` dotted callables. The convergence example's own
    harness stays deterministic and only the proposer and evaluation side goes
    live, which isolates the proposer as the single changed variable;
  - the workspace bootstrapped as
    `examples/zicato_examples/target_0_convergence/RUN.md` steps 1–4 specify
    (init, adapter block, contract publish, and a `zicato inspect mutations`
    check that reports exactly one id, `style_rules`);
  - operator go-ahead recorded.
- **Commands** (the RUN.md flow, live aux callable substituted):

  ```bash
  zicato init --workspace .zicato
  # …RUN.md steps 2–3 (adapter block, board/scoring/brief publish)…
  zicato inspect mutations --workspace .zicato          # expect: style_rules only
  zicato evolve --workspace .zicato --rounds 3 --mode full \
      --harness-call-llm  zicato_examples.target_0_convergence.mocks:target_llm \
      --auxiliary-call-llm <live aux dotted path>
  # report the printed Dashboard: http://127.0.0.1:7892 URL immediately
  zicato epoch close --workspace .zicato
  ```

- **What to measure:** rounds to reach the floor (the scripted proposer takes
  three); the scalar trajectory against the ladder 3.6, 2.4, 1.2; the count
  and content of rejected experiments; proposer token cost per round; and any
  `ProposerError` or retry-path activation.
- **Gate criteria:** the champion reaches the 1.2 floor within an
  operator-agreed round budget (suggested: at most three times the scripted
  proposer's three rounds); no gate contradictions, meaning no promotion the
  journal's evidence fails to support; and epoch-report hypotheses coherent
  with the patches actually applied. A failure here is a useful result: a
  proposer that cannot find a planted single-token defect on a one-point
  surface is a proposer-quality finding to fix before attempting any richer
  target.

### Item 2 — the presentation-agent dogfood

*The first live run against a real agent, gated on a pre-flight verdict of
`ok`.*

- **What it proves:** that the loop moves a real agent's measured behavior
  when the harness is a serving model rather than a fixture. This is the
  first dogfood result (§1.2, rung 1).
- **The failure mode this item is gated on:** a structural null, in which the
  harness ignores the part of the target the mutation surface edits, so every
  generation scores identically. Most of the presentation agent's mutation
  surface is instruction spans — `researcher_instruction`
  (`role="system_instruction"`), `coordinator_instruction`, and their
  siblings in `examples/zicato_examples/target_1_presentation/agent/agent.py`
  — and those flow into the inner agents' system prompts. A harness that
  discarded the system prompt would leave every one of those patches
  unobservable. The CI smoke-test mock therefore reads it: `target_llm` in
  `examples/zicato_examples/target_1_presentation/mocks.py` selects a base
  deck from `user`, then branches on `system`, so the researcher instruction
  alone decides whether the response carries a fabricated metric or a cited
  figure. That makes `researcher_instruction` the sole lever over the judged
  `no_fabricated_numbers` marker and keeps the smoke lane signal-bearing.
  Under a live harness the mocks are unused, and the pre-flight is what
  confirms the live setup carries signal (04-evaluation-statistics.md §9).
- **Preconditions:**
  1. A live target endpoint and a live evaluation endpoint, so instruction
     content reaches the measured behavior.
  2. `zicato board preflight` returns a verdict of `ok` against that setup.
     A `warn` means the board cannot out-signal its own noise — the
     saturation pathology, whose signature is every probe scoring
     `1.000000` — so the run would not be interpretable. A `refuse` means
     live noise swamps the probe, and the noise-floor calibration of item 3
     must come first.
  3. Item 1 (live convergence) and item 3 (the real noise floor) complete, so
     a working live proposer and a measured floor to set the margin against
     both exist.
  4. Operator go-ahead.
- **Commands:**

  ```bash
  zicato board preflight --workspace .zicato   # gate: verdict ok
  zicato board audit --workspace .zicato       # persist the live floor
  zicato evolve --workspace .zicato --rounds <N> \
      --harness-call-llm <live> --auxiliary-call-llm <live>
  # dashboard URL reported; watch the picky-stakeholder entry specifically
  ```

- **What to measure:** pre-flight signal vs floor before/after the fix;
  per-round scalar movement vs the measured floor; the generalization gap
  (train vs holdout) across the run; which mutation points the proposer
  touches (the fertility view); drift-kind movements vs the hypotheses'
  predictions (the calibration diagnostic).
- **Gate criteria:** pre-flight `ok` is the hard entry gate; a completed run
  with ≥1 promotion whose holdout confirmation released (not withheld);
  no `placebo_promoted` finding if the placebo cadence is enabled; and an
  epoch analysis a human reads as "the loop did something real here."

### Item 3 — real A/A noise-floor + margin calibration

*The single highest-information cheap measurement on a live endpoint.*

- **What it proves:** the actual magnitude and shape of live evaluation
  noise — the number every default (margin 0.01, replicates 2, evidence
  budget) was chosen in *simulation* against.
- **Preconditions:** a live target endpoint for the target being
  calibrated; a champion generation to self-duel; operator go-ahead (the
  audit costs K full-board runs).
- **Commands:**

  ```bash
  zicato board audit --workspace .zicato --runs 5      # K=5 default; raise for noisy harnesses
  # floor persists onto the epoch record (config.json noise_floor, never hashed)
  # optionally wire the epoch-open hook: config.json "calibrate_noise_floor": 5
  zicato board preflight --workspace .zicato           # floor + degradation signal + verdict
  ```

- **What to measure:** `max_abs_delta` and `delta_std`; the per-draw scalars
  (stationarity — do later draws drift?); the pre-flight's degradation-signal
  ratio; whether `margin_below_noise_floor` fires against the shipped
  margin.
- **Gate criteria:** a nonzero, stable floor (a 0.0 floor on a live LLM
  harness means the stamp/plumbing is broken — see bug #3, never "good
  news"); an explicit operator decision recorded for the margin: either
  `promote_margin` raised above the floor, or the evidence gate enabled with
  a budget priced via the builder's cost meter. This item's output is a
  *contract change decision*, and contract changes roll the epoch — say so.

### Item 4 — judge test–retest on real judges

- **What it proves:** how much scalar noise each live process judge injects
  (04-evaluation-statistics.md §10) — simulated judges cannot answer this.
- **Preconditions:** a live evaluation endpoint; a board that declares
  judges (target_1 does); ideally a settled transcript from a prior live run
  (Item 2) rather than the synthetic fixture; operator go-ahead (cost:
  judges × k calls).
- **Commands:**

  ```bash
  zicato board judges --workspace .zicato               # list declared judges first
  zicato board judges --workspace .zicato --test-retest --retest-k 5 \
      --auxiliary-call-llm <live aux dotted path>
  ```

- **What to measure:** per-judge `disagreement_rate`, `fired`/k, and the
  verdict `details` for the disagreeing calls (the post-mortem material).
- **Gate criteria:** every judge below the `0.25` noisy-judge threshold, OR
  an explicit `per_judge_weights` down-weighting recorded in the contract
  for each judge above it (with the epoch-roll acknowledged). A judge that
  agrees with itself only half the time is removed rather than down-weighted,
  because at that disagreement rate it contributes a random number to the
  loss.

### Item 5 — racing × Bradley–Terry × best-of-N real-proposer shakeout

- **What it proves:** the composed system — the multi-challenger racing
  structure, the evidence gate's defer→replicate loop, best-of-N sampling
  with screening — under a *real* proposer's output distribution. Every
  pairwise composition is tested endpoint-free; the full stack under real
  variance is not.
- **Preconditions:** Items 1 and 3 complete; the racing contract shape from
  `examples/zicato_examples/target_0_convergence/scoring.effective.json`
  (racing, `field_size: 4`, `replicates: 2`,
  `promote_confidence_threshold: 0.8`,
  `promote_confidence_replicates: 32`) or the builder's scaffold; the cost
  meter consulted and the bill acknowledged (the crowning confirm alone is
  hundreds of board runs); operator go-ahead.
- **Commands:**

  ```bash
  # publish the racing contract (or set via the builder), then:
  zicato evolve --workspace .zicato --rounds <N> \
      --harness-call-llm <live> --auxiliary-call-llm <live>
  # dashboard URL reported; the racing ladder + evidence cockpit are the views to watch
  ```

- **What to measure:** rung survival patterns (does rung-0 halving cut the
  right challengers?); evidence-gate outcomes (crowns vs `inconclusive`
  dead-letters — read `runtime/inconclusive/*.json`); best-of-N selection
  modes (how often the critic's pick differs from the last-validated
  candidate — the seam behind the gauntlet tree-mismatch case,
  `12-bug-casebook.md` case 6, under real diversity); wall-clock and token cost per round; heartbeat /
  watchdog behavior over long rungs.
- **Gate criteria:** no runtime-invariant violation (no canonical-slot
  writes from evidence duels, no stale-tree mounts — the casebook's
  regression suites double as live monitors here); every crowning's journal
  evidence self-consistent; dead-letter rate acknowledged by the operator as
  an acceptable hold rate rather than silently zero (a zero dead-letter rate
  with a real proposer and a 38-ish budget would itself be suspicious —
  see 04-evaluation-statistics.md §6).

### Item 6 — screened-vs-unscreened live economics

- **What it proves:** the screen's *money* claim. Endpoint-free tests proved
  the veto's soundness (broken candidates never forwarded; false-veto ≈
  flip²); what remains is whether screening pays for itself live — cost per
  accepted improvement, screened vs not.
- **Preconditions:** Item 5's configuration running stably; two comparable
  run budgets approved (this is an A/B over *runs*, the most expensive item
  here); operator go-ahead for both arms.
- **Commands:** two matched evolve campaigns on the same target and round
  budget, one with `proposer_quality.screen_entries: 2` (the scaffold
  value), one with `0` — note this is a contract change, so the arms are
  separate epochs by construction:

  ```bash
  # arm A: screening on (scaffold default); arm B: screen_entries: 0
  zicato evolve --workspace .zicato --rounds <N> --harness-call-llm <live> --auxiliary-call-llm <live>
  ```

- **What to measure:** per-arm — accepted improvements (released, confirmed
  promotions), total harness runs (screen runs are `proposes × best_of_n ×
  K` extra; the cost meter's line item vs actuals), rounds-to-first-
  improvement, vetoed-candidate counts and their would-have-been tournament
  cost.
- **Gate criteria:** a written comparison of cost-per-accepted-improvement.
  The decision this gates is the *default posture* of screening in
  scaffolded contracts (currently ON in scaffolds, OFF in code defaults) —
  keep, strengthen, or demote, with the measured economics attached.

### Standing rules for every backlog item

1. Explicit operator go-ahead **per run**, recorded.
2. Dashboard on, URL reported immediately (`http://127.0.0.1:7892` default).
3. Measurements land in the journal/epoch record where the instruments
   already put them — do not invent side-channel result files.
4. A live-run finding that contradicts an endpoint-free pinned number stops
   the program for investigation rather than prompting a re-pin. Either the simulation's noise
   model is wrong (fix the model, re-derive the defaults) or the live wiring
   is broken (bug-casebook time). Never adjust the pinned test to match the
   live anecdote.

### 3.7 The backlog as a dependency graph

The items are ORDERED because each one's result is *uninterpretable* without the
ones before it. The ordering is not preference — it is the answer to "what would
running this early actually prove?" (nothing, or worse, a false conclusion):

```
Item 1 (target_0 live convergence)  ─┐
Item 3 (real A/A floor + margin)    ─┼─→ Item 5 (racing × BT × best-of-N shakeout)
                                     │        │
                                     └─→ Item 2 (target_1 dogfood)   Item 6 (screened economics)
Item 4 (judge test–retest) ── informs Item 2's judge weights
```

| Item | Blocked on | Why running it early proves nothing |
|---|---|---|
| 1 — target_0 live convergence | operator go-ahead only | the cheapest first live signal; nothing depends on it *first*, but everything downstream assumes a proposer that works |
| 3 — real A/A floor | a live harness for the target | a margin decision without a measured floor is guesswork; every later gate-criterion reads against this number |
| 2 — target_1 dogfood | Items 1 + 3, AND the mock-null fix | uninterpretable while the harness is a structural null (a `1.000000` saturation, §3 Item 2) — the run would measure the mock rather than the loop |
| 4 — judge test–retest | a live aux endpoint; ideally a settled live transcript | a synthetic judge can't tell you a live judge's self-consistency; feeds Item 2's `per_judge_weights` |
| 5 — racing × BT × best-of-N | Items 1 + 3 | the composed stack under a real proposer's output distribution is meaningless without a working proposer and a real floor to size the budget |
| 6 — screened economics | Item 5 running stably | a cost-per-accepted-improvement A/B needs a stable pipeline and two approved run budgets — the most expensive item, last |

The dependency that trips people up: **Item 2 before its mock-null fix is the
single most tempting early run** (the presentation agent is the "real" dogfood),
and it is the run most likely to produce a confident null. The pre-flight
(`zicato board preflight`, verdict `ok`) is the hard entry gate, so the
structural null cannot be run past accidentally. Treat a `warn` verdict as "the
null is still here," not "close enough."

> ✅ ALWAYS check an item's precondition list before proposing its run — and if a
> precondition item has not produced an *audited* result, the dependent run is
> premature no matter how ready the code looks. "Ready to run" and
> "interpretable if run" are different questions; the ordering answers the second.

### 3.8 The live-run artifact map — where each measurement lands

Standing rule 3 says measurements land "where the instruments already put them —
do not invent side-channel result files." This is that map: every
measurement a backlog item asks for has a named home below, and each home is a
canonical store that an existing surface already reads.

| Measurement | Where it lands | Read by |
|---|---|---|
| A/A noise floor (Item 3) | `config.json` `noise_floor` (never hashed — 03-contract-and-epochs.md §3.6) | evolve-start margin check; loop-health detector |
| pre-flight verdict (Item 2 gate) | `config.json` `preflight` (never hashed) | `detect_preflight_verdict` health finding |
| per-round scalar / gate decision | `experiment.json` `outcome` + `journal.md` | dashboard lineage; epoch report |
| generalization gap (Item 2) | `OutcomeRecord.train_loss` / `holdout_loss` / `generalization_gap` on `experiment.json` | gap detector; dashboard board-status |
| evidence-gate resolution (Item 5) | `OutcomeRecord.evidence` (rating block + ci_history) | evidence cockpit view |
| inconclusive dead-letters (Item 5) | `runtime/inconclusive/*.json` | operator inspects the hold rate |
| best-of-N selection modes (Item 5) | the round log `round_log.jsonl` (`critique_selected.reason`) | proposal-session fold; 05-proposer.md §5.7 |
| which mutation points the proposer touches (Item 2) | the fertility view over the index (mutation track records) | dashboard fertility surface |
| judge test–retest (Item 4) | the `zicato board judges --test-retest` report | operator; `per_judge_weights` decision |

Two operating rules ride on this map. First, the **dashboard is on and its URL
reported before the first round settles** (§3's standing rule 2; default
`http://127.0.0.1:7892`) — the racing ladder and evidence cockpit are the live
views for Items 5–6, and a run whose URL was never surfaced is one the operator
cannot supervise. Second, a measurement that would need a *new* file is a signal
you are measuring the wrong thing: if the instrument that already exists cannot
surface it, either extend that instrument (with its own test) or the measurement
is not the one the item asked for.

> ⚠️ TRAP — a **zero** dead-letter rate on Item 5 with a real proposer and a
> ~38-run evidence budget is itself suspicious (§3 Item 5 gate
> criteria; 04-evaluation-statistics.md §6). Some crownings *should* land
> inconclusive under real noise; a rate of exactly zero suggests the pre-gate is
> forcing promotions rather than holding them — the exact second-door failure §6
> forbids. The dead-letter rate is a health signal rather than a defect to drive
> to zero.

### 3.9 What "explicit operator go-ahead, per run" means

"Agent teams verify via test suites and CI — a live run is an *operator decision*
that an agent executes, never an agent initiative" (§3). This is the single most
important operating boundary in the whole backlog, so its shape is precise:

- **Go-ahead covers one run.** Go-ahead for Item 1 is not go-ahead for Item 2. A
  new run — even a re-run of the same item after a config change — is a new
  decision. An agent that ran Item 3 does not get to "continue to Item 5"; it
  reports Item 3's result and stops.
- **Go-ahead is explicit.** "The operator asked me to work on the live program"
  is not go-ahead for a specific run. Go-ahead is the operator saying *run this
  specific run now*, and it is recorded (in the run's provenance / the operator's
  message) rather than inferred from context.
- **What the agent DOES contribute:** everything up to the launch. The strongest
  agent contribution to the live program is "a runbook item so precise the
  operator's go-ahead is a formality" (§6) — preconditions verified, commands
  ready, the dashboard-URL report drafted, the measurement plan written. The agent
  makes the run *trivial to authorize and supervise*; it does not authorize it.
- **The launch reports the dashboard URL immediately** (§3's standing rule 2;
  default `http://127.0.0.1:7892`), before the first round settles, so the
  operator can supervise from the first bracket.

The reason this is a hard boundary and not a courtesy: live runs cost real money,
occupy the workspace's runtime lock, and produce artifacts an operator may need to
quarantine. An agent that starts one on its own initiative has spent the
operator's money and locked their workspace without consent — and, at rung 3
(§1.4), an agent that could self-authorize live runs is an optimizer that has
found a way to spend unboundedly.

> ⛔ NEVER start a live `evolve` (or any board-audit / judge-retest run that hits a
> live endpoint) without recorded per-run operator go-ahead — even if a previous
> run was authorized, even if the code is obviously ready, even if "it's just a
> quick check." This is chapter invariant #2 and it has no fast path. Verify with
> suites and seeded harnesses; hand the operator a run so precise that saying yes
> is a formality; then wait for the yes.

---

## 4. The deferred register

Each entry here was *considered and consciously deferred*, with the reasoning
frozen at decision time. The register exists so a future agent (a) does not
re-litigate from scratch, and (b) knows the exact trigger that un-defers the
item. Re-opening any entry means engaging its frozen reasoning head-on in the
proposal (see §5).

| ID | Deferred item | Frozen reasoning | Un-deferral trigger |
|---|---|---|---|
| D1 | **Physical wheel split** (`zicato-lib` / `zicato-cli` / `zicato-dashboard` as separate distributions) | The boundary already exists and is CI-enforced *inside one distribution*: zero core→driver imports, the 37-name lazy facade (`src/zicato/__init__.py`), `dashboard/readers` hoisted to `zicato/query`, import-linter contracts in CI. Separate wheels add real hazards — the `python -m zicato._tournament_worker` and `-m zicato.dashboard` spawns cross a shared namespace; the `_bin/` supervisor binary can be force-included by only one wheel — and the benefit (independent installs) has no consumer. The enforced single-distribution boundary is also the hard prerequisite of the split, so nothing is lost by waiting. | An **external library consumer** actually exists (someone imports zicato-as-library without the CLI/dashboard). The `zicato-examples` uv-workspace member is the working packaging precedent to copy. |
| D2 | **Hybrid numeric/enum parameter search** (dedicated search over `new_numeric` / `new_enum` mutation ops instead of LLM-proposed values) | Value depends on surface composition, and every current target is **text-dominant** (instruction spans, prompt bodies). Building a numeric optimizer with no numeric-heavy surface to validate on produces untested machinery — the exact "asserted by hope" failure the doctrine forbids. | A real target shows a **numeric-heavy mutable surface** (thresholds, budgets, weights as first-class mutation points) where per-round LLM proposals demonstrably waste rounds vs a line search. |
| D3 | **Critic calibration from RoundLog** (tune the best-of-N critic against its own historical pick quality) | Needs **accumulated live logs** to calibrate against — RoundLog emission shipped (schema + fold in `epoch/round_log.py`, wired through the evolve seams), but the log corpus is empty of live rounds. Calibrating a critic on synthetic rounds teaches it the synthetic distribution. | Live runs from §3 accumulate enough RoundLog history that pick-vs-outcome joins have statistical power (state the N in the un-deferral proposal). |
| D4 | **Portfolio / quality-diversity search** (maintaining a population of diverse champions rather than a single lineage head) | Architectural: it changes what "champion" means across the lineage, journal, dashboard, and gate — every consumer of the promoted spine. It is a design change rather than a knob, and needs its **own design pass** with the protected-incumbent and server-authority invariants renegotiated explicitly. Bolting a population onto the single-champion data model would commit the shared-mutable-state lesson (many logical artifacts on one physical slot, `12-bug-casebook.md`) on purpose. | An operator-level need for diversity preservation (e.g. measured premature convergence on a live target), and a design note that survives review. |
| D5 | **Screen baseline hardening at extreme σ** | The screen's champion-passing baseline is the parent's replicate-0 canonical measurement — the same baseline the promote gate itself trusts. At extreme harness noise (the Tier-2 σ=0.22 world) a noisy baseline can admit a truly-failing entry to the panel as "champion-passing"; that failure mode belongs to the *baseline measurement* rather than to the confirm rule, and no single-confirm rule can reach a 2% false-veto rate there anyway (σ² is already 4.8%). The documented upgrade — a **paired champion-baseline re-run at base 3000 under the real champion id** — is designed but unbuilt, because a contract that noisy is outside the usable regime the pre-flight would wave through. | A live floor measurement (Item 3) showing a *usable* contract whose noise still makes screen false-vetoes material in practice; then build the paired-baseline upgrade per the design note in the screen test docstrings (`tests/test_decision_procedure_power.py` §candidate-screen). |
| D6 | **Per-run worktree pool** (pre-warmed ephemeral checkouts to amortize the admin-lock window) | The measured cost says no: per-add 6.4–28 ms serial, 14–41 ms *total* under 16-way contention (benchmark frozen in `git_genstore.py::checkout_ephemeral`'s docstring and commit `e91fe1f`), 3–18× faster than the copytree it replaced. A pool adds shared mutable state, because a pool is by construction a shared-slot design (the shared-mutable-state lesson, `12-bug-casebook.md`), to shave milliseconds nobody has observed in a profile. The rejected `git archive` alternative is likewise frozen in the same docstring. | Checkout cost visibly shows in a live-run profile (Item 5's wall-clock measurements are where it would surface). |

> ⚠️ TRAP: an entry in the register can still ship. If your work needs a
> deferred capability, the correct move is an un-deferral proposal that
> quotes the frozen reasoning and shows its trigger fired. A quiet partial
> implementation that "doesn't count" because it is small is not that move.
> Partial
> implementations of deferred architecture are how a codebase grows two half
> answers to one question.

### 4.1 The un-deferral protocol, worked

Re-opening a deferred item is a specific, bounded move. "The deferral lapsed,
so I will build it" is not one. The protocol:

1. **Quote the frozen reasoning verbatim.** Name the register entry, paste its
   reasoning, and address it head-on. A proposal that ignores the frozen
   reasoning asks the reviewer to re-derive the original decision, which is what
   the register exists to prevent.
2. **Show the trigger fired, with evidence.** Each entry names a concrete
   un-deferral trigger. The proposal must show that the trigger happened — a real
   consumer, a real profile, a real measurement — rather than that it plausibly
   could.
3. **Re-cost against today's code.** The frozen reasoning was true at decision
   time; some of it may have changed (a boundary that was only CI-enforced may
   now be shipped; a benchmark may need re-running). Re-ground every claim.
4. **All of the item or none of it.** A deferred *architecture* — portfolio
   search, the wheel split — comes back as its own design pass. A partial that
   "grows into it" is the failure the trap above describes, and avoiding it is
   why the register exists.

**Worked example — un-deferring hybrid numeric/enum search.** The frozen
reasoning: "every current target is text-dominant … building a numeric optimizer
with no numeric-heavy surface to validate on produces untested machinery." The
trigger: "a real target shows a numeric-heavy mutable surface where per-round LLM
proposals demonstrably waste rounds vs a line search." A valid un-deferral
proposal would:

- **Quote that entry's reasoning** and confirm it still holds structurally (the applier's
  `set_numeric` / `set_enum` ops exist — 05-proposer.md §5.4.3 —
  so the *mechanism* is there; only the *search* was deferred).
- **Show a measurement of the trigger:** a shipped target whose mutation manifest is
  ≥ N numeric points (thresholds, budgets, weights as first-class mutation
  points), plus a live run (§3) showing the proposer spending rounds re-rolling
  numeric values a line search would have swept — the "demonstrably waste rounds"
  clause, with round counts.
- **State the acceptance criterion up front** (§5.4): the numeric search finds
  the same or better optimum in fewer harness runs than the LLM-proposed baseline
  on that surface, measured on the same seeds, pinned as a test.
- **Confine the change** to a search strategy over the existing numeric ops — it
  does NOT touch the contract hash (a search strategy is a `RuntimeConfig`-shaped
  concern rather than a scoring rule; 03-contract-and-epochs.md §3.12) and does NOT
  widen proposer visibility.

An un-deferral that cannot show the trigger fired is a request to re-litigate the
deferral, and the answer to that request is that the reasoning still holds and
the item stays deferred.

> ⛔ NEVER un-defer portfolio / quality-diversity search or the wheel split as a
> "small" change. Both are architectural. Portfolio search changes what
> "champion" means across the lineage, journal, dashboard, and gate; the wheel
> split changes packaging boundaries that the worker and dashboard spawns cross.
> Un-deferring either is a design pass that renegotiates the protected-incumbent
> and server-authority invariants explicitly (§4). Bolting a population onto the
> single-champion data model would commit the shared-mutable-state lesson on
> purpose (12-bug-casebook.md §"The meta-lessons").

### 4.2 Deferred is not an anti-goal (and vice versa)

Two categories of "we don't do this" live in this chapter, and confusing them is
a real failure mode. They are opposites in their futures:

| | Deferred register (§4) | Anti-goals (§6) |
|---|---|---|
| Might it ship someday? | **Yes** — when the un-deferral trigger fires | **No** — never, by design |
| What re-opens it | evidence the trigger fired (§4.1) | nothing; it is a standing refusal |
| Why it is here | the work is *not yet worth it* or *not yet validatable* | the work would *break a guarantee* the system exists to hold |
| Example | hybrid numeric/enum search — valuable once a numeric surface exists | a free-form source-edit path — dissolves the containment/auditability guarantees |

A deferred item is a *timing* decision: the machinery is fine, the moment is
wrong. An anti-goal is a *soundness* decision: the machinery itself would void a
proof. Numeric search can therefore un-defer cleanly. "Let the model edit files
directly" can never un-defer, however convenient it looks, because it is refused
rather than postponed.

> ⚠️ TRAP — do not propose an anti-goal as if it were a deferred item ("we could
> add a free-edit path behind a flag, deferred for now"). An anti-goal has no
> un-deferral trigger because there is no future state that makes it safe — the
> guarantee it would break is not conditional. If your proposal's premise is "this
> anti-goal is fine under condition X," the condition is the thing to scrutinize,
> and the burden is a design note showing the guarantee survives (§5.3) rather
> than a deferral entry.

### 4.3 Adding a deferral (the register is bidirectional)

The register grows in both directions: §4.1 removes entries, and sometimes a
proposal's right outcome is a *new* deferral rather than a build or a rejection.
A good deferral saves a future agent the whole investigation. A register-worthy
deferral records three things:

1. **The frozen reasoning, at decision time.** Record *why it is not worth
   building now* — the specific cost/benefit or not-yet-validatable argument,
   grounded in code as it stands (§5.1). "We did not get to it" is not that
   reasoning. The worktree-pool entry is the model: it freezes the benchmark numbers
   (`git_genstore.py::checkout_ephemeral`'s docstring) so a future agent does not
   re-run them to rediscover "the cost says no."
2. **The un-deferral trigger, concrete.** State a future condition an agent can
   *check*. "When a numeric-heavy surface exists", "when a live-run profile shows
   checkout cost", and "when an external library consumer exists" are each
   verifiable. "When it seems worth it" gives the next agent nothing to check, so
   it invites the whole decision to be re-argued.
3. **What a partial build would cost** (the §4 trap): naming why a fragment is
   worse than nothing makes the all-or-nothing rule self-enforcing.

The discipline that makes the register valuable: a deferral is a *decision with
its reasoning attached*, so re-opening it (§4.1) engages the reasoning rather than
starting over. A "deferred: TODO" with no reasoning records no decision at all;
the next agent re-investigates from scratch, which is the waste the register
exists to prevent.

> ✅ ALWAYS write a deferral so the next agent can act on it without re-deriving
> it: frozen reasoning, checkable trigger, partial-build cost. If you find yourself
> unable to state a concrete trigger, the honest move is usually a *rejection* (it
> will never be worth it — say so and why) or a *build* (it is worth it now)
> rather than a deferral. A deferral with no trigger records an undecided question
> under the label of a decision.

---

## 5. How to propose new work

Every rule below generalizes a real failure from 12-bug-casebook.md or a real save from
the program's design passes (the screen's cache-leakage trap was caught *in
the design pass*, before it shipped, by this checklist).

### 5.1 Ground it in code before you claim it

Read the actual seams your proposal touches and cite them by symbol and path.
The program's planning documents did this to great effect — e.g. discovering
that "anonymized failure exemplars" was *half-built already* (the
outcome-marginal channel existed with an enforced sanitizer), which shrank
the item from a feature to an extension. The claim "X does not exist" or "X
behaves like Y" in a proposal must be greppable. If your proposal's premise
is wrong, the best time to find out is before the branch exists.

### 5.2 Adversarially verify your own premise

Every serious proposal carries a self-critique section: the strongest
objection you can construct, and either its resolution or the design change
it forced. The screen's surviving design is the model — the objection ("a
2-entry screen is a worse estimator than the tournament; argmax over it is
random choice plus winner's curse") reshaped the semantics (veto-first, never
ranking) rather than being argued away. A proposal with no credible objection
listed has not been thought about hard enough to review.

### 5.3 Design-first zones: the overfitting boundary and the contract hash

Two territories require a written design note **before** implementation:

- **Anything that widens what the proposer can see** of per-entry evaluation
  results (entry ids, exact inputs, exact deltas, holdout anything, transcript
  content). The note must state the redaction rules and the **empirical
  harm-detection protocol** — which instruments (generalization-gap detector,
  placebo arm) will be watched, and what reading constitutes harm. The
  process-exemplars work is the precedent: design doc first
  (`docs/design/PROCESS-EXEMPLARS.md`), redaction rules explicit, opt-in and
  deliberately NOT scaffolded.
- **Anything that touches contract identity** — new `ScoringWeights` fields,
  canonicalization changes, hash inputs. The note must state: the
  omit-at-default decision (does an unset field roll existing epochs? it must
  not), the epoch-roll semantics of setting it, and — if the hash moves for
  anyone — the explicit BREAKING declaration with the one-time roll called
  out (the `8d0a94f` precedent: when the hash is *wrong*, move it once,
  loudly, rather than shimming the wrongness stable).

### 5.4 Measured acceptance criteria, stated up front

Every proposal states, before implementation, what will be measured and what
numbers constitute acceptance — in the power-harness idiom where the work is
statistical (04-evaluation-statistics.md §13.6: nulls first, planted deltas
in floor units, the failing alternative measured on the same draws, printed
rates + pinned bounds). Every new decision surface states its operating
characteristics as measurements rather than expectations; the gate holds no
special position here. If
the work is not statistical, the acceptance criterion is still executable: a
named test that fails today and passes after.

### 5.5 The fix, PR, and oracle cadence

- **Fixes first.** Confirmed bugs ship as their own leading PR, independently
  valuable, before the features that exposed them.
- **Stacked, single-concern PRs** with a stated dependency order; parallel
  workstreams only on genuinely disjoint surfaces (and module moves —
  facade/boundary work — sequenced last so files do not churn under active
  branches).
- **Both oracles green at every merge point**: the convergence known-answer
  suite and the decision-procedure power suite, plus byte-identity checks for
  every default-off knob you added (the "oracle byte-identical at
  default-off" acceptance line).
- **Regression tests fail with the fix stashed** — the fail-with-the-fix-stashed
  lesson (12-bug-casebook.md), demonstrated on each test rather than assumed.
- **Per-branch vendor scan** before any push or PR: no model-vendor names,
  ids, or trailers anywhere in the diff or commit messages. This rule has no
  exceptions and no "it's just a comment" carve-out.
- **Docs land with the change**: CLI.md is a generated artifact (regenerate
  from `zicato --help` on CLI changes — `--help` stays canonical); design
  docs whose claims your change stales get swept in the same PR (the
  `docs: sweep stale default claims` commits are the precedent).

### 5.6 The proposal template, minimally

1. **Premise** — the current behavior, cited by symbol/path.
2. **Objection** — the strongest case against, and what it changed.
3. **Boundary check** — overfitting visibility? contract identity? If either:
   the design note.
4. **Slots audit** — per the casebook's pre-flight checklist
   (12-bug-casebook.md): every artifact written, the number of logical instances
   per physical slot, and the invalidation story.
5. **Acceptance** — the measurements and bounds, stated before code.
6. **Cadence** — PR stack, oracle checkpoints, docs to sweep.

### 5.7 A worked proposal (the template, filled in)

The template below is filled in for one unbuilt piece of the winner-resolution
layer: **maximal lotteries** as a resolver option
(`docs/design/SELECTION-THEORY.md`). The rest of that layer ships opt-in —
`zicato.selection.rating` fits Bradley–Terry strengths from replicated duels, and
`zicato.selection.resolve` supplies the Smith prune, Ranked Pairs, and Copeland
behind the `resolver` structure param — so the example is an extension to a
standing seam. It illustrates the shape a reviewable proposal takes and does not
endorse starting the work.

1. **Premise (cited).** The gauntlet resolves a duel by scalar delta and the
   racing structure by rung survival; the evidence gate adds a Bradley–Terry
   *confidence* threshold (`recommended_scaffold_weights`,
   03-contract-and-epochs.md §3.5). A separate winner-resolution pass turns a set
   of pairwise results into an ordering under a social-choice rule:
   `resolve_leader` dispatches to Ranked Pairs or Copeland over a Smith-pruned
   margin matrix, for a strategy's internal leader selection and never for the
   gate. Maximal lotteries is the resolver option that rule set does not offer.
   Grounding a premise this way is what §5.1 asks for: the item is an extension
   to a built seam rather than the feature it looks like from the design doc.

2. **Objection (and what it changed).** The strongest objection: "a
   social-choice resolver over noisy pairwise results is a more elaborate way to
   overfit the noise — argmax over a Smith set is still argmax." The resolution
   must be the SELECTION-THEORY discipline: **replicate first, resolve second**.
   The resolver runs over replicated, gate-cleared pairwise evidence rather than
   raw single duels, so it supplies a soundness-preserving ordering on top of the
   gate rather than a second promotion door (§6, "no gate-bypassing shortcuts").
   If the
   objection cannot be answered without weakening the gate, the item does not
   ship.

3. **Boundary check.** Does it widen proposer visibility? No — resolution is
   evaluation-side. Does it touch contract identity? YES: a new resolution rule
   changes what "winner" means, so it is a `TournamentStructure` param (folds into
   the contract hash via the scoring recursion, 03-contract-and-epochs.md §3.2.3)
   and needs the omit-at-default treatment (§3.4) so existing epochs do not roll.
   → design note required (§5.3), stating the omit-at-default decision and the
   epoch-roll semantics of opting in.

4. **Slots audit.** Every artifact the resolver writes (a rating block, a
   ci_history) already has a home on `OutcomeRecord.evidence`
   (journal.py `_outcome_from_dict`); confirm one logical instance per generation
   slot and an invalidation story on re-resolution (the shared-mutable-state
   lesson, 12-bug-casebook.md §"The meta-lessons").

5. **Acceptance (stated before code).** In the power-harness idiom (§5.4): under
   the A/A null the resolver promotes nothing (soundness preserved); on planted
   effects it recovers the true order at ≥ the current procedure's power on the
   same seeds; the naive resolver's failure (argmax over noisy pairs) is measured
   on the same draws and pinned as documentation. A named test that fails today
   and passes after.

6. **Cadence.** Any confirmed resolver bug ships first (§5.5, fixes-first); the
   new resolver option stacks as a single-concern PR behind it; both oracles green
   at the merge point with the new knob byte-identical at default-off; docs
   (`SELECTION-THEORY.md`) land in the same PR.

The point of the worked example: a reviewable proposal is *almost entirely*
premise, objection, and acceptance — the code is the small part. A proposal that
leads with the code has skipped the parts that decide whether it should exist.

### 5.8 The fixes-first cadence, worked

"Fixes first" (§5.5) is the cadence rule most often skipped under time pressure,
and it has a precise shape worth spelling out because every bug in
12-bug-casebook.md was found *while building a feature* — the feature exposed the
bug, and the discipline is to land the fix independently before the feature that
found it.

The shape, using the casebook's pattern as the template:

1. **Isolate the fix as its own leading PR.** When a feature branch surfaces a
   confirmed bug (a wrong mounted tree, a replicate-slot reuse, a spurious roll),
   the fix ships FIRST, on its own branch, before the feature. It is independently
   valuable — a bug fix stands alone — and it keeps the feature PR reviewable (a
   reviewer is not asked to distinguish "the fix" from "the feature" in one diff).
   Each of the program's two fix PRs led the work that found it (§5.5).
2. **The regression test must fail with the fix stashed.** This is the
   fail-with-the-fix-stashed lesson
   (12-bug-casebook.md §"The meta-lessons"): a regression test that passes whether
   or not the fix is present pins nothing. *Demonstrate* the failure — stash the
   fix, watch the test go red, restore it, watch it go green. A test asserted to
   catch a bug it never saw fail is a test that will silently rot.
3. **Then the feature stacks behind the fix**, single-concern, with a stated
   dependency order (§5.5). Module moves / facade work sequence LAST so files do
   not churn under active branches.
4. **Both oracles green at the merge point** (§2.3), plus the byte-identical-at-
   default check for every default-off knob the feature added (03-contract-and-
   epochs.md §3.11 step 8 is the same discipline on the contract side).

Shipping the fix and the feature in one PR because they are related costs two
things. A combined diff hides which change did what, so a later bisect cannot
separate a fix regression from a feature regression, and the fix waits on the
feature's review despite standing on its own. The casebook exists because
these bugs were subtle; the cadence exists so the *next* subtle bug's fix is not
buried in an unrelated feature diff.

> ⛔ NEVER ship a regression test you have not watched fail with the fix removed.
> "It would have caught the bug" is a hypothesis, and watching it fail makes it a
> demonstrated one. The most common way a regression test rots is being written against
> a codebase where the bug is already fixed, so it is green from birth and pins the
> wrong invariant (or none). Stash, red, restore, green — every time.

### 5.9 Docs land with the change

"Docs land with the change" (§5.5) matters because a doc that describes behavior
the code does not have is worse than no doc: a reader trusts it. Three specific
disciplines:

- **CLI.md is a GENERATED artifact.** `docs/design/CLI.md` is regenerated from
  `zicato --help`, which is the canonical source. On any CLI change, regenerate;
  never hand-edit CLI.md to match. A hand-edit that drifts from `--help` states
  something false with the authority of a document.
- **Sweep the design docs your change stales.** If a change moves a default, a
  boundary, or an invariant a design doc asserts, that doc's claim is now false —
  sweep it in the SAME PR (the `docs: sweep stale default claims` commits are the
  precedent, §5.5). A stale claim in a design doc is a future agent's wrong premise
  (§5.1).
- **The dev guide is code-grounded too.** This guide's rule — "ground EVERY claim
  in the branch's actual code; prefer symbol names + file paths over line numbers"
  — applies to edits here as much as to the source docs. A chapter that describes a
  `_canon_*` function the code does not have is the same failure as a stale design
  doc. When you change a subsystem this guide documents, the chapter is part of
  the change.

The CHANGELOG is the fourth surface (§5.5, §6.1): a behavior-affecting default is a
CHANGELOG entry with the pin spelled out, in the loud `⚠️ BREAKING DEFAULTS` idiom
the noise-aware-defaults and contract-hash-fix entries established
(03-contract-and-epochs.md §3.11 step 9). A default changed with no CHANGELOG entry is
the kind of change the casebook records (§6, "no silent defaults changes").

> ⚠️ TRAP — regenerating CLI.md is a mechanical step easy to forget in a CLI-flag
> PR, and the drift is invisible until a reader follows the stale doc into a flag
> that the CLI does not have. Regenerate inside the same change rather than as a
> follow-up: the `--help` output is the source of truth, the doc is derived from
> it, and a derived artifact that is not re-derived is stale by definition.

---

## 6. Anti-goals

Things zicato does not do. Each is a load-bearing refusal: an agent that
"helpfully" adds one regresses the system rather than extending it.

**No free-form source edits.** The proposer mutates the target ONLY through
the enumerated mutation surface — declared mutation points, applied via the
validating applier (`zicato.mutation.applier`) with its post-apply syntax
gate, through the generation store's transactional `derive_generation`. There
is no "just let the model edit the file" path, and there must never be one:
the mutation surface is what makes changes enumerable, auditable,
diffable-by-point, and attributable (the fertility map, diff-complexity, and
patch journaling all key on it). A free-edit path would also dissolve the
containment guarantees (diff containment checks, forbidden-path checks) that
make an autonomous loop safe to leave running.

**No vendor coupling.** Nothing in git references the model vendor — no
names, no model-id strings, no commit trailers. Mechanically: every LLM touch
goes through the `CallLLM` callable seam (`(system, user, model) -> str`,
dotted-path importable so it crosses the worker boundary), and model
selection lives in operator-owned config (`models_config.py`, `builder.json`)
— never in code, never in examples, never in mocks ("The mocks NEVER
reference any specific model vendor" is written into the example sources).
This is both a portability property and a durable repo rule; the per-branch
vendor scan enforces it.

**No silent defaults changes.** A behavioral default is part of the measured
system: the shipped defaults were *chosen against measurements* (replicates=2,
best_of_n=3, margin 0.01 + calibration warnings, evidence gate opt-in-but-
scaffolded — each traceable to a pinned number in the power harness).
Changing one is a contract-visible act: omit-at-default canonicalization so
nobody rolls retroactively, an epoch roll for those who opt in, a CHANGELOG
entry, and a docs sweep. The noise-aware defaults flip and the contract-hash
fix both shipped as loud, declared changes — that is the template. A default
changed in passing, inside an unrelated PR, is a casebook entry waiting to
happen.

**No gate-bypassing shortcuts.** Every path to the champion pointer goes
through the gate + its confirmation stack. No test hook, resume path, repair
tool, dashboard control, or "operator convenience" may write the promoted
spine directly. The system's soundness claims are claims about *the gate*;
a second door voids them all. Four corollaries look innocent and
break the same rule: promoting a screen scalar into evidence (selection-biased, §3.3 of
04-evaluation-statistics.md), letting the evidence pre-gate *force* a
promotion (it may only hold one), releasing raw holdout results to the
proposer (Blum–Hardt guarantee void), or letting a health finding auto-roll
an epoch (every detector is recommend-only; rolls are operator acts).

**No agent-initiated live runs.** Agents prove things with suites and seeded
harnesses; operators spend money and mint live epochs (§3 states the same rule
as an operating boundary). The strongest possible agent contribution
to the live program is a runbook item so precise the operator's go-ahead is
a formality — which is what §3 is.

**No untestable statistics.** No decision procedure ships whose operating
characteristics cannot be measured under seeded noise in CI. If a mechanism's
soundness can only be checked live, the mechanism is redesigned until it can
be checked cold — that constraint produced the stable-identifier seed
discipline, and it is non-negotiable because it is the only thing that keeps
the statistics chapter's facts *facts*.

### 6.1 Anti-goals as a pre-merge checklist

A mechanical check enforces each anti-goal, so that none of them depends on
someone remembering it. Run this before you open a PR; a "no" on any row blocks
the PR:

| Anti-goal | The mechanical check | Where it is enforced |
|---|---|---|
| No free-form source edits | every mutation goes through an enumerated mutation point + the validating applier's post-apply syntax gate | `zicato.mutation.applier`; `derive_generation` all-or-nothing (03-contract-and-epochs.md §3.9.2) |
| No vendor coupling | the per-branch scan finds no model-vendor name / id / trailer in the diff or commit message; every LLM touch is the `CallLLM` seam | the vendor scan; `models_config.py` / `builder.json` own model selection, never code/mocks |
| No silent default change | a changed behavioral default carries a CHANGELOG `⚠️ BREAKING DEFAULTS` entry with the pin spelled out; omit-at-default canonicalization so non-pinners roll deliberately | `CHANGELOG.md`; 03-contract-and-epochs.md §3.4 |
| No gate-bypassing shortcut | no test hook, resume path, repair tool, dashboard control, or override writes the promoted spine directly | every champion-pointer write goes through the gate + confirmation stack (§6) |
| No agent-initiated live run | the run has recorded explicit operator go-ahead; the launch reported its dashboard URL | §3's standing rules; the gate-live-runs discipline |
| No untestable statistic | the new decision surface has a power-harness test (nulls first, planted deltas in floor units, printed rates + pinned bounds) that fails today and passes after | `tests/test_decision_procedure_power.py` (§2.3, §5.4) |

An anti-goal enforced only by memory is one that will eventually be violated.
Each therefore carries a mechanical check, for the reason rung 3 exists (§1.4):
an optimizer routes around any rule that nothing checks. A change that makes one
of these checks harder to run automatically is itself a regression worth
flagging.

> ⛔ NEVER treat "it's just a comment / a test hook / an operator convenience" as a
> carve-out for any row. The gate-bypass and vendor-coupling rows in particular
> have "no exceptions" written into them (§6, §5.5): a second door voids the
> soundness claims wholesale, and a single vendor string in a comment fails the
> scan for the whole branch. The checklist has no asterisks.

### 6.2 The anti-goals are the shape of the north star

Each anti-goal states one face of the goal in §1 rather than a restriction bolted
onto the system. A measured claim of effectiveness is the conjunction of these
refusals:

| Anti-goal | The property of the north star it protects |
|---|---|
| no free-form source edits | **auditability** — every change is enumerable, diffable-by-point, attributable (the fertility map, diff-complexity, patch journaling all key on the mutation surface) |
| no vendor coupling | **portability** — the loop is a property of the mechanism rather than of one endpoint; every LLM touch is the `CallLLM` seam |
| no silent default changes | **comparability** — a default is part of the measured system, so changing one is a contract-visible act (03-contract-and-epochs.md §3.4) |
| no gate-bypassing shortcuts | **soundness** — the whole claim is a claim about *the gate*; a second door voids it |
| no agent-initiated live runs | **operator authority** — money and live epochs are operator decisions (§3.9) |
| no untestable statistics | **falsifiability** — a guarantee that cannot be null-tested in CI is not a guarantee (§2.4) |

Reading the table this way changes how a proposal reads. A proposal that "works
around" an anti-goal proposes to give up the property in that row. "Let the model
edit files directly" trades away auditability; "hard-code the model" trades away
portability; "let the pre-gate force a promotion" trades away soundness. Each
refusal is a statement of what the system guarantees.

> ✅ ALWAYS state, for any change that brushes an anti-goal, which property in the
> table above it preserves and how. That is the same discipline as §1.1's two-proof
> definition of done, applied to the refusals: if you cannot name the property your
> change keeps intact, you are probably trading it away.

---

## 7. Where to go from here

- Extending the decision procedure → 04-evaluation-statistics.md §13 first,
  then 06-tournament-and-selection.md for the strategy seams.
- Touching storage, worktrees, or caches → 07-runtime-and-durability.md plus
  casebook cases 1, 2, 8, 9.
- Building proposer-side capability → 05-proposer.md, with §5.3's
  overfitting-boundary rule in hand.
- Anything else → 13-recipes.md for the mechanical how, and this chapter's
  §5 for whether and in what order.

### 7.1 By task — the fuller map

| If you are about to… | Read first | And do not skip |
|---|---|---|
| add a scoring / gate / selection rule | 04-evaluation-statistics.md §13, 06-tournament-and-selection.md | this chapter §5.4 (measured acceptance) + §2.3 (the power oracle) |
| add a contract knob | 03-contract-and-epochs.md §3.11 | §3.4 (omit-at-default) — a missed registration mass-rolls the fleet |
| widen what the proposer sees | 05-proposer.md §5.8 | this chapter §5.3 (design-first) — an overfitting-boundary change needs a note before code |
| touch storage / worktrees / caches | 07-runtime-and-durability.md | casebook cases 1, 2, 8, 9 (the identity-vs-location + slot-reuse classes) |
| add a runtime tuning knob | 03-contract-and-epochs.md §3.12 | the choose-which table — a scoring rule mis-filed as runtime silently breaks comparability |
| propose a live run | this chapter §3 (the item's preconditions) + §3.9 | the operator's per-run go-ahead — never an agent initiative |
| re-open a deferred item | this chapter §4.1 | the frozen reasoning — engage it, don't re-derive it |
| fix a bug a feature exposed | 12-bug-casebook.md (the class) | this chapter §5.5/§5.8 — fixes-first, and the test must fail with the fix stashed |

### 7.2 A reading order for a new contributor

If you are new to zicato and want the shortest path to *contributing without
breaking a proof*: 01-orientation.md (what the loop is) → 02-architecture.md (the
process split) → 04-evaluation-statistics.md (the noise doctrine — everything
downstream is built on it) → this chapter §1–§2 (what "effective" means and what
is proven) → the chapter for your subsystem. The one non-negotiable before any
change to the loop's decision path: §1.1's two-proof definition of done. If you
cannot state which of the two proofs your change is preserving and how, you are
not ready to make it yet.

The north star, restated once more in operational terms: **keep the two
proofs green, move the endpoint-gated items through the runbook with the
operator, and never make the loop's yes/no less trustworthy than you found
it.**
