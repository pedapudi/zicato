# 14 — Goals and Roadmap

> **Covers:** the north star and its operational definition of "effective";
> the current proof state (what is proven endpoint-free vs what awaits a
> serving endpoint); the ENDPOINT-GATED BACKLOG as the operator's runbook,
> with per-item preconditions, exact commands, measurements, and gate
> criteria; the deliberately-deferred register with its reasoning frozen;
> the discipline for proposing new work; and the anti-goals — the things
> zicato deliberately does not do.
>
> **Prerequisites:** 01-orientation.md (the loop), 04-evaluation-statistics.md
> (the noise doctrine — every item below is downstream of it),
> 12-bug-casebook.md §"The meta-lessons" (the failure shapes new work must
> not repeat).
>
> **Invariants introduced in this chapter:**
> 1. **"Effective" is a measured property**, defined by the standing
>    convergence + operating-characteristics proofs — never by demo vibes,
>    never by a single lucky run.
> 2. **No live evolve run starts without the operator's explicit go-ahead**,
>    and every live launch enables the dashboard and reports its URL.
>    Agents verify with test suites and CI, never with live runs.
> 3. **Deferred is a decision, not a gap.** Every deferred item carries its
>    frozen reasoning and its un-deferral trigger; re-opening one means
>    engaging that reasoning, not rediscovering the problem.
> 4. **New work near the overfitting boundary or the contract hash is
>    design-first.** Code without the design note is not reviewable.
> 5. **Nothing in git references the model vendor.** No names, no model ids,
>    no trailers. This is a durable repo rule, enforced by scan on every
>    branch.

---

## 1. The north star

zicato exists to be a **demonstrably effective self-improvement harness**: a
loop that takes an agent system under a frozen evaluation contract, proposes
targeted mutations to its declared mutable surface, measures each candidate
under tournament conditions, and promotes only what a noise-aware decision
procedure can actually distinguish from the incumbent — while journaling
enough evidence that a human can audit every crowning after the fact.

The load-bearing word is **demonstrably**. The project's founding failure
mode — the reason the 2026-07 program happened — was a loop that *ran*
convincingly while proving nothing: a null live result (the `1.000000`
saturation signature, every probe scoring identically) and a noise-blind
default gate that, when finally measured, promoted a challenger identical to
its champion in a third of seeded trials
(04-evaluation-statistics.md §3.1, fact #1). A self-improvement harness that
cannot tell improvement from noise is a random walk with a progress bar.

### 1.1 What "effective" means operationally

"Effective" has a standing, machine-checked definition. It is the conjunction
of two proofs, both living in the test suite, both required to stay green
forever:

1. **The convergence proof** — `tests/test_convergence_known_answer.py` over
   `examples/zicato_examples/target_0_convergence/`. The FULL shipped loop —
   real propose → apply → validate → subprocess tournament workers → reduce →
   gate → persist, under the default git generation store, nothing
   tournament-side stubbed — converges on a planted-defect target to an
   **exact, hand-computable floor**: scalars 3.6 → 2.4 → (3.6 rejected, the
   negative control) → 1.2, decisions `promoted → rejected → promoted`. This
   is the existence proof: when improvement is real and measurable, the loop
   finds it, and when a candidate is strictly worse, the gate refuses it.

2. **The operating-characteristics proof** —
   `tests/test_decision_procedure_power.py`. The decision procedure itself,
   under seeded noise that mimics production (agents vary, judges are LLMs):
   the A/A null promotes nothing under the effective contract; power at
   planted effects of 0.5×/1×/3× the measured noise floor is monotone and
   hits 1.0 at 3×; the naive procedure's failures are pinned *as
   documentation* alongside the effective procedure's recoveries. This is
   the soundness proof: the loop's yes/no is trustworthy under the noise it
   will actually face.

Everything else in the repository — calibration, pre-flight, the Ladder, the
evidence gate, screening, the placebo arm — is instrumentation in service of
keeping those two proofs true as the system grows. A change that leaves both
proofs green and their pinned numbers honest is progress; a change that needs
either one weakened is, definitionally, not.

> ✅ ALWAYS treat these two test files as the definition of done for any work
> touching the loop. "My feature works and the convergence + power suites are
> green with unchanged pinned numbers" is a complete claim. "My feature works"
> alone is not a claim at all.

### 1.2 The dogfood escalation ladder

The long arc past the backlog is fixed and documented
(`docs/design/DOGFOOD-TARGETS.md`): three inner-harness targets in
escalating order, each named early because each forces architectural needs
the earlier design had to pre-accommodate.

| Rung | Target | Cadence | What it forces |
|---|---|---|---|
| 0 | `target_0_convergence` — the planted-defect policy agent | the standing proof + backlog Item 1 | nothing new; it IS the known-answer instrument |
| 1 | `target_1_presentation` — the multi-agent presentation coordinator (9 mutation points: specialist instructions, coordinator routing, tool descriptions) | v0 dogfood; backlog Item 2 | a real agent with real failure modes whose drift signal IS the loss — validates the v0 stack end to end |
| 2 | `target_2_goldfive_steering` — goldfive's own steering layer | v0+1 | cross-repo mutation; non-drift loss signal |
| 3 | zicato itself | v0+2 | nested zicato instances; recursion guards |

The point of running the earlier rungs is stated in the design doc and worth
repeating verbatim in spirit: **validate the loop before trusting it on
itself.** Rung 3 — zicato evolving zicato — is the end state the whole
soundness program exists to make non-insane: you do not point a
self-improvement loop at its own decision procedure until that procedure's
operating characteristics are proven and its promotion path has no second
door (§6). Nothing on rung 3 begins until rungs 1–2 have produced live,
audited improvement.

target_1's expected drift movements are pre-registered in
`DOGFOOD-TARGETS.md` §1.4 (confabulation → tighten the researcher;
capability-mismatch → encode pipeline order in the coordinator; looping →
exit conditions in the reviser) — treat that table as working hypotheses the
first live epoch confirms or falsifies, not as promises.

---

## 2. The current proof state

### 2.1 Proven endpoint-free (machine-checked in CI, standing)

Everything below runs with **no model endpoint anywhere** — deterministic
harnesses, scripted proposers, seeded noise — which is precisely why it is
strong: no flaky external dependency, exact reproducibility, honest nulls.

| Claim | Instrument |
|---|---|
| The full loop converges to a known answer through real workers, git backend, no stubs | `tests/test_convergence_known_answer.py` + target_0's `RUN.md` CLI demo |
| The gate rejects a strictly-worse child (negative control) | round 2 of the same oracle |
| The A/A noise floor is measurable, and the deterministic harness measures exactly 0.0 while the σ=0.22 harness measures ≈ the analytic 0.663 | `test_aa_null_calibration_measures_the_noise_floor`; calibration e2e |
| An under-margined, un-gated procedure promotes pure noise (20/60 A/A trials); the evidence-gated procedure promotes none | `test_margin_below_noise_floor_without_evidence_gate_is_unsound`, `test_aa_effective_contract_false_promotion_rate_is_zero` |
| The Bradley–Terry gate is a soundness device (CIs separate only after ~37 unbroken wins); replication is the power device (32 replicates → 0.5×-floor effects become ~3σ) | power harness (04-evaluation-statistics.md §3.1, facts #2–#3) |
| Evidence replicates are independent draws at reserved slots; canonical slots are never touched; duplicate draws are refused | `test_evidence_replicates_are_independent_draws`, `test_full_mode_evidence_loop_never_touches_canonical_slots` (post bug #8) |
| The candidate screen vetoes a broken candidate deterministically (0/12 forwarded vs 12/12 unscreened) with false-veto ≈ flip² under noise | screen OC tests in the power harness |
| Seeded noise crosses the real subprocess-worker boundary intact (reproducible, side-independent, replicate-independent) | `test_noisy_adapter_seeded_draws_cross_the_worker_boundary` |
| The ten program bugs are each pinned by a regression test that fails with the fix stashed | 12-bug-casebook.md, per case |

### 2.2 What awaits the endpoint

Every remaining claim is of the form *"and this holds when the measurements
come from a real serving model."* Seeded noise is a model OF production
noise; the endpoint-gated backlog (§3) exists to check the model against
reality. Specifically unproven today:

- live convergence — that a **real proposer** finds real improvements on a
  real harness, at all, and at what cost per accepted improvement;
- the **real** A/A noise floor of an LLM-backed harness (magnitude, shape,
  stationarity), and whether the shipped `promote_margin`/replication
  defaults are calibrated for it;
- real judge self-consistency (test–retest on live judges);
- the composed system — racing × Bradley–Terry evidence × best-of-N ×
  screening — under a real proposer's output distribution;
- the screen's live economics (cost per accepted improvement, screened vs
  unscreened).

Until those run, the honest status line is: **the machine is proven; the
world is modeled.** Do not claim more in any document or commit message.

---

## 3. The ENDPOINT-GATED BACKLOG — the operator's runbook

These items are queued waiting on a serving model endpoint. They are ordered:
each item's interpretation depends on the ones before it (a live convergence
result is uninterpretable without a live noise floor; a dogfood run is
uninterpretable while its harness is a structural null).

> ⛔ NEVER start any run in this section without the operator's **explicit
> go-ahead for that specific run**. This is a standing rule, not a
> formality: live runs cost real money, occupy the workspace's runtime lock,
> and produce artifacts an operator may need to quarantine. Agent teams
> verify via test suites and CI — a live run is an *operator decision* that
> an agent executes, never an agent initiative.

> ✅ ALWAYS launch live evolve runs with the dashboard enabled (the default)
> and report the printed URL (default `http://127.0.0.1:7892`; do not pass
> `--dashboard-bind` unless the operator asks) in your first status message,
> before the first round settles. The operator watches the bracket live; a
> run whose URL was never reported is a run the operator cannot supervise.

### Item 1 — target_0 live convergence

*The known-answer demo, with the scripted proposer replaced by the real one.*

- **What it proves:** a real proposer, reading the real mutation surface and
  loss summaries, can find the planted defects the scripted proposer was
  handed — the first live evidence that the propose step generates signal.
- **Preconditions:**
  - a serving endpoint wired through `--harness-call-llm` /
    `--auxiliary-call-llm` dotted callables (target_0's harness stays
    deterministic — only the *proposer/aux* side goes live first; this
    isolates the proposer variable);
  - the workspace bootstrapped exactly as
    `examples/zicato_examples/target_0_convergence/RUN.md` steps 1–4
    (init, adapter block, contract publish, `zicato mutations` sanity
    check: exactly one id, `style_rules`);
  - operator go-ahead recorded.
- **Commands** (the RUN.md flow, live aux callable substituted):

  ```bash
  zicato init --workspace .zicato
  # …RUN.md steps 2–3 (adapter block, board/scoring/brief publish)…
  zicato mutations --workspace .zicato          # expect: style_rules only
  zicato evolve --workspace .zicato --rounds 3 --mode full \
      --harness-call-llm  zicato_examples.target_0_convergence.mocks:harness_llm \
      --auxiliary-call-llm <live aux dotted path>
  # report the printed Dashboard: http://127.0.0.1:7892 URL immediately
  zicato epoch close --workspace .zicato
  ```

- **What to measure:** rounds-to-floor (scripted proposer: 3); scalar
  trajectory vs the exact ladder 3.6/2.4/1.2; count and content of rejected
  experiments; proposer token cost per round; any `ProposerError` /
  retry-path activations.
- **Gate criteria:** the champion reaches the 1.2 floor within an
  operator-agreed round budget (suggested: ≤ 3× the scripted proposer's 3
  rounds); zero gate contradictions (a promotion the journal's evidence does
  not support); the epoch report's hypotheses are coherent with the patches
  actually applied. Failure is informative, not shameful: a proposer that
  cannot find a planted single-token defect on a one-point surface is a
  proposer-quality finding to fix *before* any richer target.

### Item 2 — target_1 dogfood — AFTER fixing its structural mock-null

*The presentation-agent target is currently incapable of measuring anything.
Fix the harness first; the defect is precise and documented here.*

- **The defect, exactly:** in
  `examples/zicato_examples/target_1_presentation/mocks.py`, `harness_llm`
  **discards the system prompt**. The dispatch is
  `lowered = user.lower()` over canned substring keys, with the line
  `_ = system, model` explicitly throwing the system prompt away.
  Meanwhile target_1's entire mutation surface is instruction spans —
  `researcher_instruction` (`role="system_instruction"`),
  `coordinator_instruction`, and siblings in
  `examples/zicato_examples/target_1_presentation/agent/agent.py` — which
  flow into the inner agents' **system prompts**. Ergo: no patch the
  proposer can legally make can change any mock output. Every generation
  scores identically. This is a **structural null** — the exact saturation
  pathology (`warn`, the `1.000000` signature) that
  `zicato board preflight` exists to catch
  (04-evaluation-statistics.md §9), baked into the target's own mock.
- **Preconditions:**
  1. Rework the target_1 harness path so instruction content *reaches the
     measured behavior* — either a live harness endpoint (the real fix:
     `--harness-call-llm <live>` making the system prompt actually steer
     output), or a repaired mock that conditions on `system` content for the
     smoke-test lane. Under a live harness the mocks are simply not used;
     the mock repair matters only if the CI smoke test should become
     signal-bearing too.
  2. Run `zicato board preflight` against the fixed setup and require a
     verdict of `ok` — the pre-flight is the acceptance test for the fix
     (a `warn` means the null is still there; a `refuse` means the live
     noise swamps the probe and Item 3's calibration must come first).
  3. Items 1 and 3 complete (a live proposer that works, and a measured live
     floor to set the margin against).
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
- **Preconditions:** a live harness endpoint for the target being
  calibrated; a champion generation to self-duel; operator go-ahead (the
  audit costs K full-board runs).
- **Commands:**

  ```bash
  zicato board audit --workspace .zicato --runs 5      # K=5 default; raise for noisy harnesses
  # floor persists onto the epoch record (config.json noise_floor, never hashed)
  # optionally wire the epoch-open hook: config.json "calibrate_noise_floor": 5
  zicato board preflight --workspace .zicato           # floor + achievable signal + verdict
  ```

- **What to measure:** `max_abs_delta` and `delta_std`; the per-draw scalars
  (stationarity — do later draws drift?); the pre-flight's achievable-signal
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
- **Preconditions:** a live auxiliary endpoint; a board that declares
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
  for each judge above it (with the epoch-roll acknowledged). A judge at
  coin-flip consistency is removed, not down-weighted — a 0.5-disagreement
  judge is a random number generator wired into the loss.

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
  modes (how often the critic's pick ≠ last-validated — the bug-#6 seam
  under real diversity); wall-clock and token cost per round; heartbeat /
  watchdog behavior over long rungs.
- **Gate criteria:** no runtime-invariant violation (no canonical-slot
  writes from evidence duels, no stale-tree mounts — the bug-casebook
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
4. A live-run finding that contradicts an endpoint-free pinned number is a
   **stop-and-investigate**, not a re-pin: either the simulation's noise
   model is wrong (fix the model, re-derive the defaults) or the live wiring
   is broken (bug-casebook time). Never adjust the pinned test to match the
   live anecdote.

---

## 4. The deliberately-deferred register

Each entry here was *considered and consciously deferred*, with the reasoning
frozen at decision time. The register exists so a future agent (a) does not
re-litigate from scratch, and (b) knows the exact trigger that un-defers the
item. Re-opening any entry means engaging its frozen reasoning head-on in the
proposal (see §5).

| # | Deferred item | Frozen reasoning | Un-deferral trigger |
|---|---|---|---|
| D1 | **Physical wheel split** (`zicato-lib` / `zicato-cli` / `zicato-dashboard` as separate distributions) | The boundary already exists and is CI-enforced *inside one distribution*: zero core→driver imports, the 36-name lazy facade (`zicato/api.py`), `dashboard/readers` hoisted to `zicato/query`, import-linter contracts in CI. Separate wheels add real hazards — the `python -m zicato._tournament_worker` and `-m zicato.dashboard` spawns cross a shared namespace; the `_bin/` supervisor binary can be force-included by only one wheel — and the benefit (independent installs) has no consumer. The enforced single-distribution boundary is also the hard prerequisite of the split, so nothing is lost by waiting. | An **external library consumer** actually exists (someone imports zicato-as-library without the CLI/dashboard). The `zicato-examples` uv-workspace member is the working packaging precedent to copy. |
| D2 | **Hybrid numeric/enum parameter search** (dedicated search over `new_numeric` / `new_enum` mutation ops instead of LLM-proposed values) | Value depends on surface composition, and every current target is **text-dominant** (instruction spans, prompt bodies). Building a numeric optimizer with no numeric-heavy surface to validate on produces untested machinery — the exact "asserted by hope" failure the doctrine forbids. | A real target shows a **numeric-heavy mutable surface** (thresholds, budgets, weights as first-class mutation points) where per-round LLM proposals demonstrably waste rounds vs a line search. |
| D3 | **Critic calibration from RoundLog** (tune the best-of-N critic against its own historical pick quality) | Needs **accumulated live logs** to calibrate against — RoundLog emission shipped (schema + fold in `epoch/round_log.py`, wired through the evolve seams), but the log corpus is empty of live rounds. Calibrating a critic on synthetic rounds teaches it the synthetic distribution. | Live runs from §3 accumulate enough RoundLog history that pick-vs-outcome joins have statistical power (state the N in the un-deferral proposal). |
| D4 | **Portfolio / quality-diversity search** (maintaining a population of diverse champions rather than a single lineage head) | Architectural: it changes what "champion" means across the lineage, journal, dashboard, and gate — every consumer of the promoted spine. Not a knob; needs its **own design pass** with the protected-incumbent and server-authority invariants renegotiated explicitly. Bolting a population onto the single-champion data model would be meta-lesson M1 (one slot, N logical artifacts) committed on purpose. | An operator-level need for diversity preservation (e.g. measured premature convergence on a live target), and a design note that survives review. |
| D5 | **Screen baseline hardening at extreme σ** | The screen's champion-passing baseline is the parent's replicate-0 canonical measurement — the same baseline the promote gate itself trusts. At extreme harness noise (the Tier-2 σ=0.22 world) a noisy baseline can admit a truly-failing entry to the panel as "champion-passing"; that failure mode belongs to the *baseline measurement*, not the confirm rule, and no single-confirm rule can reach a 2% false-veto rate there anyway (σ² is already 4.8%). The documented upgrade — a **paired champion-baseline re-run at base 3000 under the real champion id** — is designed but unbuilt, because a contract that noisy is outside the usable regime the pre-flight would wave through. | A live floor measurement (Item 3) showing a *usable* contract whose noise still makes screen false-vetoes material in practice; then build the paired-baseline upgrade per the design note in the screen test docstrings (`tests/test_decision_procedure_power.py` §WS-S). |
| D6 | **Per-run worktree pool** (pre-warmed ephemeral checkouts to amortize the admin-lock window) | The measured cost says no: per-add 6.4–28 ms serial, 14–41 ms *total* under 16-way contention (benchmark frozen in `git_genstore.py::checkout_ephemeral`'s docstring and commit `e91fe1f`), 3–18× faster than the copytree it replaced. A pool adds shared mutable state (a pool IS a shared-slot design — meta-lesson M1) to shave milliseconds nobody has observed in a profile. The rejected `git archive` alternative is likewise frozen in the same docstring. | Checkout cost visibly shows in a live-run profile (Item 5's wall-clock measurements are where it would surface). |

> ⚠️ TRAP: the register is not a graveyard. If your work genuinely needs a
> deferred capability, the correct move is an un-deferral proposal that
> quotes the frozen reasoning and shows its trigger fired — not a quiet
> partial implementation that "doesn't count" because it is small. Partial
> implementations of deferred architecture are how a codebase grows two half
> answers to one question.

---

## 5. How to propose new work

The discipline below is not process for its own sake — every rule is a
generalization of a real failure from 12-bug-casebook.md or a real save from
the program's design passes (the screen's cache-leakage trap was caught *in
the design pass*, before it shipped, by exactly this checklist).

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
rates + pinned bounds). "Operating characteristics measured, not asserted by
hope" is the standard for *any* new decision surface, not just the gate. If
the work is not statistical, the acceptance criterion is still executable: a
named test that fails today and passes after.

### 5.5 The phase / PR / oracle cadence

- **Fixes first.** Confirmed bugs ship as their own leading PR, independently
  valuable, before the features that exposed them (the WS-F precedent: both
  program-fix PRs led their phases).
- **Stacked, single-concern PRs** with a stated dependency order; parallel
  workstreams only on genuinely disjoint surfaces (and module moves —
  facade/boundary work — sequenced last so files do not churn under active
  branches).
- **Both oracles green at every merge point**: the convergence known-answer
  suite and the decision-procedure power suite, plus byte-identity checks for
  every default-off knob you added (the "oracle byte-identical at
  default-off" acceptance line).
- **Regression tests fail with the fix stashed** (12-bug-casebook.md M3) —
  demonstrated, not assumed.
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
4. **Slots audit** — per 12-bug-casebook.md's checklist: every artifact
   written, logical-instances-per-slot, invalidation story.
5. **Acceptance** — the measurements and bounds, stated before code.
6. **Cadence** — PR stack, oracle checkpoints, docs to sweep.

---

## 6. Anti-goals

Things zicato deliberately does not do. These are load-bearing refusals — an
agent "helpfully" adding any of them is regressing the system, not extending
it.

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
a second door voids them all. Corollaries that look innocent and are not:
promoting a screen scalar into evidence (selection-biased, §3.3 of
04-evaluation-statistics.md), letting the evidence pre-gate *force* a
promotion (it may only hold one), releasing raw holdout results to the
proposer (Blum–Hardt guarantee void), or letting a health finding auto-roll
an epoch (every detector is recommend-only; rolls are operator acts).

**No agent-initiated live runs.** Restated from §3 because it is also an
anti-goal: agents prove things with suites and seeded harnesses; operators
spend money and mint live epochs. The strongest possible agent contribution
to the live program is a runbook item so precise the operator's go-ahead is
a formality — which is what §3 is.

**No untestable statistics.** No decision procedure ships whose operating
characteristics cannot be measured under seeded noise in CI. If a mechanism's
soundness can only be checked live, the mechanism is redesigned until it can
be checked cold — that constraint produced the stable-identifier seed
discipline, and it is non-negotiable because it is the only thing that keeps
the statistics chapter's facts *facts*.

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

The north star, restated once more in operational terms: **keep the two
proofs green, move the endpoint-gated items through the runbook with the
operator, and never make the loop's yes/no less trustworthy than you found
it.**
