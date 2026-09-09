# 04 — Evaluation Statistics: The Doctrine

> **Covers:** the full measurement chain (goldfive events → reducer → `LossProfile` →
> `aggregate_generation_score` → scalar), the promote gate's rule ladder, the noise
> doctrine and its measured facts, A/A noise-floor calibration (repeat draws of
> one generation measured against itself), the Ladder-mediated holdout, the
> Bradley–Terry evidence gate, replication semantics, measurement purposes,
> contract pre-flight, judge test–retest, the placebo arm,
> the overfitting program map, and the power-harness methodology for proving any
> statistical change.
>
> **Prerequisites:** 01-orientation.md (what a generation / epoch / board is),
> 03-contract-and-epochs.md §3.1–§3.2 (what the contract hash covers and what
> rolls an epoch),
> 06-tournament-and-selection.md §6.10 (who calls the gate).
>
> **Invariants introduced in this chapter:**
> 1. **The scalar is a loss.** Lower is better, everywhere, always.
> 2. **Scoring is pure.** No LLM, no I/O, no wall clock inside a scoring formula.
> 3. **The two scoring seams have exactly one implementation each**, in
>    `src/zicato/scoring/builtins.py`, imported by both the orchestrator and the
>    killable worker. Never re-inline a formula.
> 4. **The dict-then-`sum` accumulation order in the scalar is load-bearing**
>    (float addition is not associative). New scalar terms append LAST, gated
>    to be *exactly absent* at their default.
> 5. **Every measurement is a noise draw.** Every decision procedure over
>    measurements must be explicitly noise-aware, and its operating
>    characteristics must be *measured under seeded noise*, never asserted.
> 6. **A measurement identifies its epoch, generation, entry, purpose, draw,
>    and base seed.** Reusing that identity does not create independent evidence.
> 7. **Measurement purposes separate evaluations.** Diagnostic probes cannot
>    occupy tournament slots or supply promotion evidence (§8).
> 8. **The holdout is confirmation-only and mediated by the Ladder**, the
>    budgeted holdout-reuse mechanism of §5. It can flip a train-win to reject;
>    it never promotes, never steers the proposer, and its raw per-entry results
>    never leave the gate. Every holdout access follows a durable query
>    reservation. Exhaustion or reservation failure starts no holdout work.
> 9. **The evidence gate can only hold a promotion, never force one.** The
>    protected-incumbent invariant strictly strengthens through it.
> 10. **Soundness devices and power devices are different things.** The
>     evidence gate buys soundness; replication buys power. Do not "fix" low
>     power by weakening a soundness device.

This chapter is the one that keeps you from making an unsound change. zicato
exists to produce promotion decisions an operator can trust: when the loop says
"this child is better than its parent," the claim has to hold. Every mechanism
below replaces a naive version that was measured promoting noise, memorizing the
board, or corrupting its own evidence. Changing anything in this chapter's
territory without reproducing the corresponding measurement is guessing, and
12-bug-casebook.md records what each such guess cost.

---

## 1. The measurement chain

One evaluation of one generation against one board entry flows through five
stages. Every stage has exactly one home:

| Stage | What happens | Home | Runs where |
|---|---|---|---|
| 1. Run | The system under test executes the entry and emits the draw’s `events.{purpose}.r{draw}.jsonl` file (§7.3) | adapter + goldfive `JSONLPersistenceSink` | killable worker subprocess |
| 2. Reduce | Events → one `LossProfile` (drift counts, pass/fail, per-judge loss, `drift_loss`) | `src/zicato/telemetry/reducer.py` (`reduce_loss`) | worker subprocess |
| 3. Persist | `LossProfile` → the draw’s `loss.{purpose}.r{draw}.json` file (§7.3) | `src/zicato/tournament/unit_cache.py` | worker writes; orchestrator reads |
| 4. Aggregate | Per-entry losses → one per-generation summary dict (`scalar`, `pass_rate`, `mean_score`, `per_entry`, `namespace_aggregates`, `scalar_components`) | `src/zicato/tournament/scoring.py` (`aggregate_generation_score`) | orchestrator |
| 5. Decide | Two aggregates → `GateOutcome` | `src/zicato/tournament/gate.py` (`evaluate_gate`) | orchestrator |

The reducer is the **only** zicato component that walks raw goldfive events.
Everything downstream — pattern detectors, tournament scoring, journal
rendering, the dashboard — reads `LossProfile`. The seam is narrow by design:
goldfive's event schema evolves upstream, so exactly one place in zicato knows
the wire form.

> ✅ ALWAYS route any new signal you want to score through the reducer into a
> `LossProfile` field (or a namespaced `MetricCount`). Never have a scorer or a
> gate read event files directly — you would create a second event-schema
> dependency that silently breaks when goldfive evolves.

### 1.1 Seam 1 — the per-run drift-loss formula

The per-run reduction of a run's drift EVENTS into a single `drift_loss`
scalar is **Seam 1**. Its formula lives in
`src/zicato/scoring/builtins.py::builtin_drift_loss`:

```python
# src/zicato/scoring/builtins.py — builtin_drift_loss (core)
    sev_w = weights.severity_weights
    # ``math.fsum`` over collected terms, never a running float: this term
    # reaches every served scalar and every parity golden, and both the
    # builtin ``sum`` (whose float behaviour changed in Python 3.12) and a
    # running accumulator make the result depend on something other than the
    # inputs — the interpreter version, or the order the counts arrive in.
    terms = [
        sev_w.get(c.severity, 0.0)
        * _kind_multiplier(c.name.removeprefix("drift:"), weights)
        * c.count
        for c in metric_counts
        if c.name.startswith("drift:")
        and not is_judge_attributed_kind(c.name.removeprefix("drift:"))
    ]
    terms.append(weights.plan_revision_weight * plan_revisions)
    return max(0.0, math.fsum(terms))
```

Facts a change here must respect:

- This computes the `drift:` CHANNEL rather than the run's whole loss. Task
  failures and the not-completed charge are the `failure:` channel, wall-clock is
  `runtime:`, and custom judges are `judge:` — each derived from the profile
  by `LossProfile.scoring_metrics` and coefficiented by `namespace_weights`.
  `builtin_drift_loss` sees `task_failure_ratio` / `runtime_ms` nowhere; the
  `DriftContext` still carries them so a drift PLUGIN can read the run's
  outcome.
- Judge-attributed kinds (`custom` / `custom:<judge_name>`) are SKIPPED here
  and scored off `LossProfile.per_judge_loss` in the `judge:` channel. The
  skip and the derivation are one invariant in two places: charging a
  judge's drift in both channels double-counts it. `per_kind_weights` for
  the `custom` kind is rejected at contract load for the same reason.
- `_kind_multiplier` is `per_kind_weights.get(kind, 1.0)` — a first-class
  kind's multiplier, stacked on the severity weight.
- The reducer applies one piece of **reducer policy** *around* this formula
  rather than inside it: the `task_failure_ratio` floor to 1.0 for a run that
  never completed. It stays in `telemetry/reducer.py`. Do not move it into the
  builtin — the builtin is the *inner per-run formula only*, and the worker
  imports it without importing the reducer.

### 1.2 Why the formula has two homes and must stay byte-identical

`builtin_drift_loss` and `builtin_scalar` live outside the reducer and
`tournament/scoring.py` so that:

1. the orchestrator AND the killable worker subprocess import the **same**
   implementation — no drift between the two sites, ever; and
2. a scoring plugin can *wrap* the default (`ctx.builtin_loss` /
   `ctx.builtin_scalar` on the frozen `DriftContext` / `ScalarContext` in
   `src/zicato/scoring/api.py`) instead of re-implementing it.

The dependency direction is one-way by construction: `scoring/builtins.py`
owns its own judge-kind test rather than importing the reducer, so the worker
can import the builtin without pulling the reducer's world in.
`tests/test_scoring_seams.py` holds a second, INDEPENDENT implementation of
both formulas and pins the two against each other bit-for-bit across a
representative corpus.

> ⛔ Do not change a default scoring behavior by editing
> `src/zicato/scoring/builtins.py` alone. An operator-facing shape change
> rides *on top* via the dispatcher (`src/zicato/scoring/dispatch.py` —
> declarative transforms in `scoring/transforms.py`, dotted-spec plugins in
> `scoring/plugins.py`). A change to the COMPOSITION itself — the kind that
> rolls every epoch in the wild — must move the builtin, the dispatcher's
> mirroring path, and the seam test's second implementation together, and
> must say so in the release note.

### 1.3 Seam 2 — the per-generation scalar, and why the sum is sorted

**Seam 2** synthesizes the per-generation scalar: one bounded pass/miss term
plus every already-weighted channel. The composition in `builtin_scalar` looks
redundant, since it builds a dict and then sums its values. Collapsing it into a
running accumulation would break reproducibility:

```python
# src/zicato/scoring/builtins.py — builtin_scalar (core)
    pass_component = weights.pass_weight * (1.0 - mean_score)
    scalar_components: dict[str, float] = {"pass": pass_component}
    for ns in sorted(namespace_aggregates):
        component_name = ns[:-1] if ns.endswith(":") else ns
        scalar_components[component_name] = namespace_aggregates[ns]
    diff_component = diff_complexity_component(weights, diff_size)
    if diff_component is not None:
        scalar_components["diff_complexity"] = diff_component
    return sum(scalar_components.values())
```

The `sorted` is the load-bearing part, and the reason generalizes to every
float-accumulating surface in zicato: **float addition is not associative**,
and the namespace key set is assembled from a `set` in
`aggregate_namespaced_metrics`. Summing in mapping-iteration order would make
the scalar's last bit depend on the process's hash seed — stable within one
run and different in the next. Sorting makes the result reproducible; the
dict-then-`sum` shape keeps the surfaced `scalar_components` and the summed
value from ever disagreeing.

Concretely: `(a + b) + c != a + (b + c)` for IEEE-754 doubles in general. Three
consumers make a last-bit flip in the scalar matter. The gate compares scalars
against a margin with strict inequalities. Parity goldens and the crash-resume
path compare persisted `gen_score.json` files byte-for-byte. The A/A
calibration measures *spread*, so a formula that produces different bytes for
the same inputs on different code paths manufactures phantom noise.

Three more properties of Seam 2 that any extension must preserve:

- **Every namespace is in the loop, drift included.** There is no privileged
  term and no skip: `aggregate_namespaced_metrics` hands over one
  already-coefficiented value per channel, and the composition adds them all.
  The one thing that must never appear twice is a single measurement in two
  channels — which is why judge-attributed drift is excluded from
  `drift_loss` and the `drift:` MetricCount mirrors are excluded from the
  generic namespace walk.
- **Key collisions collapse to the last writer** (two namespaces stripping to
  the same component name). That is the specified behaviour, mirrored across
  both seams; treat it as a documented property rather than a defect to fix.
- **New terms append LAST and must be exactly absent at their default.** The
  `diff_complexity` term is the template: it is appended after the
  float-order-sensitive namespace accumulation, only when
  `weights.diff_complexity_weight > 0.0` AND a `diff_size` was threaded.
  Otherwise the key is never written and `sum(...)` is byte-identical to the
  same formula with no diff-complexity term at all. `diff_complexity_component` in `builtins.py` is the
  single seam both `builtin_scalar` and `aggregate_generation_score` read, so
  the appended scalar term and the surfaced `scalar_components` entry can
  never disagree.

A scalar term needs a declared effective setting, one shared computation, and
checks that its inactive value contributes nothing. The complete effective
configuration is serialized and hashed (chapter 03 §3.4). Keep accumulation
order explicit so adding a term cannot change rounding in other components.


### 1.4 `pass_rate` vs `mean_score` — the uniform outcome axis

`aggregate_generation_score` (`src/zicato/tournament/scoring.py`) reports both:

- `pass_rate` — the binary pass fraction over entries whose
  `LossProfile.pass_fail` is not `None`. Entries with no expectation are
  excluded from numerator AND denominator; a board with no expectations at all
  reports `1.0` so the `(1 - pass_rate)` term does not punish it.
- `mean_score` — the **uniform continuous outcome axis**: the mean of
  `entry_score(loss)` per entry, where an explicit continuous `score` is
  clamped to `[0, 1]` (non-finite ⇒ `0.0`, so a rogue scorer can never poison
  the mean) and a bool maps to exactly `float(pass_fail)`.

The scalar's pass component runs on `mean_score` rather than `pass_rate`. On an
all-bool board every entry with a `pass_fail` also produces a score and vice
versa, so `mean_score == pass_rate` **byte-for-byte** — that identity is the
back-compat proof, and it is pinned by test. On a graded board, `mean_score`
tracks quality continuously with no threshold cliff.

`per_entry` rows carry `{"drift_loss", "pass_fail", "score"}` — the gate's
per-entry monotonicity scope reads `score` through `_row_score`
(`tournament/gate.py`), which falls back to the binary bit for an aggregate
that carries no `score` field. Keep that fallback intact: it is what lets an
aggregate persisted under any earlier schema score identically today.

### 1.5 Namespace aggregates

`aggregate_namespaced_metrics` (`src/zicato/tournament/scoring.py`) produces
`{namespace: weighted_aggregate}` where each value is the namespace's per-run
mean **already multiplied by its signed weight**. The sign convention encodes
direction:

| Weight sign | Meaning | Examples |
|---|---|---|
| positive | higher is worse; added to the loss as-is | `drift:`, `cost:`, `latency:`, `schema:` |
| negative | higher is better; negation flips it into a loss | `rubric:` |
| zero | tracked, never scored, never direction-gated | `output:` (default) |

Because the sign is folded in *here*, everything downstream — the scalar sum,
the gate's namespace-monotonicity rule — can treat every namespace as one
unified lower-is-better axis and never re-derive direction. If you add a
namespace consumer that reads raw means and re-applies weights, you will
double-apply the sign for someone.

Mechanics worth knowing before you extend this function:

- The `drift:` namespace is special-cased to
  `namespace_weights["drift:"] * mean(LossProfile.drift_loss)` — parity with
  the drift-loss-mean term, so a consumer that reads only the namespace surface
  gets the same drift contribution `drift_loss_mean` carries. Drift
  `MetricCount` mirror entries are *skipped* in the metric walk to avoid
  double-counting.
- Per-loss sums are computed within one loss first, then folded — a loss with
  multiple entries in one namespace counts each entry; a loss with none
  contributes zero to that namespace's sum while still counting in the
  denominator (`n_losses`). This is the same "absent contributes zero"
  per-run-mean model as `drift_loss_mean`.
- Namespaces named in `namespace_weights` but absent from the data are
  promoted to `0.0` aggregates, so downstream consumers iterate a **stable key
  set**. Namespaces present in data but unweighted aggregate at weight `0.0`
  (visible, contributing nothing). Unnamespaced metric names are silently
  ignored.

### 1.6 `LossProfile` anatomy for statisticians

Not every `LossProfile` is a clean measurement. The fields that change how a
unit *counts* statistically:

| Field | Statistical meaning |
|---|---|
| `pass_fail is None` | no expectation, or the expectation could not fire (e.g. budget death before the matcher). Excluded from `pass_rate`/`mean_score` numerator AND denominator — never counted as a fail. |
| `wall_clock_budget_exceeded=True` / `abort_cause == BUDGET_ABORT_CAUSE` | a **deterministic** exhaustion: re-running re-hits the same cap. Cache-eligible (the one cacheable abort cause) and aggregates as a worst-case loss for its side. |
| `abort_cause` set to anything else (`is_infra_abort_cause`) | an **infra blip** — worker crash, spawn failure, endpoint outage. NOT a measurement of the generation. Never cached as a result; consumers like the screen treat it as *no signal* (it can never veto). |
| `not_completed=True` | any non-success terminal state. Charged in the `failure:` channel as `not_completed_weight` (an absolute contract magnitude) on top of `task_failure_weight × task_failure_ratio`, whose ratio is floored to 1.0 for such a run. Both the reducer and the runner's aborted-run synthesiser state the FACTS (`not_completed`, the floored ratio) and let the channel do the arithmetic — so the two paths cannot disagree. |
| `per_judge_loss` | per-judge weighted-loss attribution. It IS the `judge:` channel — `LossProfile.scoring_metrics` derives one `judge:<name>` metric per entry — and it is ALSO carried onto `ScalarContext` by `_per_judge_loss_aggregate` for plugin/provenance visibility. A scalar that adds the context copy on top of the channel double-counts. |

The distinction between the deterministic budget abort and the infra abort is
load-bearing everywhere a loss is *classified* rather than summed. The
screen's veto rules are the clearest statement (see
`src/zicato/epoch/screen.py::_is_budget_abort`): a budget abort vetoes
immediately (deterministic signal — no confirm run is spent on it), an infra
abort is no-signal (an outage must never disqualify a candidate). If you add a
new consumer that reads `abort_cause`, route the classification through
`is_infra_abort_cause` — do not string-match cause values yourself.

A unit that never starts because a scheduling budget expired carries
`execution_started=False`. It is an attempt record with zero spend and no
measured outcome. A later round can run the unit because no reusable cache
slot was filled. The aggregate reports incomplete entries separately, the gate
defers, and rating evidence excludes the incomplete comparison. Completed
per-task timeouts retain their existing scored-failure and cache policies.

### 1.7 The dispatch layer — provenance and the plugin contract

Both seams route through `src/zicato/scoring/dispatch.py`
(`resolve_drift_loss` / `resolve_scalar`), which returns
`(value, provenance)`. The provenance string (`"builtin"`,
`"transform:pow"`, `"plugin:<dotted spec>"`, or the fail-open
`"builtin (fallback: plugin raised)"`) is persisted onto measurement loss files and
`gen_score.json` as `scalar_provenance` — additive, never a contract input.
Rules the dispatch layer enforces that a change must not weaken:

- **Transforms are neutral at absence.** An absent `pass_transform` /
  `drift_kind_aggregation` entry is `linear` — byte-identical to no transform.
  Malformed specs are rejected **fail-fast at contract load**
  (`ScoringWeights.__post_init__`), never mid-scoring where they would produce
  a NaN inside a tournament.
- **Plugins wrap, never replace blindly.** A plugin receives the frozen
  context including `builtin_loss`/`builtin_scalar` and adjusts from there. A
  raising plugin **fails open to the builtin** with the fallback provenance —
  a scoring plugin bug degrades a run's provenance, never aborts a tournament.
- The contexts are frozen dataclasses so a plugin cannot mutate inputs another
  stage already read, and they carry plain data only — scoring stays pure by
  construction.

### 1.8 A worked example: the known-answer arithmetic, end to end

The whole chain is hand-computable on the target_0 convergence example, and
you should be able to reproduce this arithmetic before you change anything in
the chain. From `examples/zicato_examples/target_0_convergence` under its
contract (`severity_weights.info = 1.0`, plus the shipped channel defaults:
`drift:` and `pass_weight` at `1.0`, `runtime:` at `0.0`). The zero runtime
coefficient is load-bearing, because per-run wall clock varies and any nonzero
coefficient would break the exact floor. Under that contract:

- the policy carries `tokens` defect tokens; the harness emits one
  `drift_detected` frame at severity `info` per remaining token per run, so
  **Seam 1** gives `drift_loss = 1.0 · 1.0 · tokens = float(tokens)` for
  every run;
- each known token fails exactly one predicate on the 5-entry board, so
  `mean_score = passes/5` (all-bool board ⇒ equals `pass_rate`
  byte-for-byte);
- **Seam 2**: every other channel is exactly `0.0` — no cost / latency /
  rubric / schema metrics in this world, no custom judges, and no run
  aborts (`failure:` is `0.0` only because every run completes, which is
  itself part of what the oracle pins) — so

  ```
  scalar(tokens, passes) = 1.0·tokens + 1.0·(1 − passes/5)

  v0 (3 tokens, 2/5 pass) = 3.6   seeded baseline
  v1 (2 tokens, 3/5 pass) = 2.4   round 1: PROMOTED  (Δ = −1.2 clears margin 0.01)
  v2 (3 tokens, 2/5 pass) = 3.6   round 2: REJECTED  (the negative control; "challenger regressed")
  v3 (1 token,  4/5 pass) = 1.2   round 3: PROMOTED — the exact floor
  ```

`tests/test_convergence_known_answer.py` pins these numbers through the FULL
loop — real subprocess workers, the git generation store, no tournament
stubs. If your change to any stage of the chain moves any of these bytes, the
oracle tells you before an operator does. The power harness's planted deltas
(§13.4) rest on the same arithmetic: one full token fix is a true effect of 1.2
in scalar units, which measurement-flip noise σ attenuates to `1.2·(1 − 2σ)`.

> **The two-marker harness variant — the recombination oracle.** The example
> harness carries an additive `STYLE_RULES_EXTRA` support (byte-identical when
> unused, so the numbers above are untouched) that plants TWO independent defect
> markers instead of one: v0 scalar 2.4, a fix for either marker alone worth
> Δ = 1.2, and the UNION of both fixes worth Δ = 2.4. The contract pins
> `promote_margin = 1.5` STRICTLY BETWEEN the single-marker and the union
> deltas, so each single-marker fix REJECTS (1.2 < 1.5) while the mechanical
> recombination of their disjoint patches PROMOTES (2.4 > 1.5). That
> planted-defect world is what measures the value of the recombination slot
> (05-proposer.md §5.6.11): `tests/test_recombination_known_answer.py` runs it
> through the full loop and pins the union minted in round 3, chosen
> `mode="recombined"`, promoted. Its stall control runs the same script with
> `recombine` off, where the champion stays v0 because neither single-marker fix
> clears the margin. The two-marker policy template lives in that test.

### 1.9 The observability layer: loop-health detectors over the chain

Nothing in the measurement chain reports its own degradation, so a separate
layer watches it. `src/zicato/health/diagnostics.py` is the recommend-only
observability layer over everything in this chapter: each detector is a pure
function over persisted history, surfaced per round. The detectors that watch
the measurement chain and the decision procedure:

| Finding code | Watches | Fires when |
|---|---|---|
| `degenerate_scoring` | the scalar's discriminating power | scoring stops separating generations |
| `non_differentiating_entry` | per-entry outcomes | an entry gives every generation the same result (dead weight on the board) |
| `flat_drift_signal` | drift counts | the drift channel goes flat (nothing to optimize on) |
| `no_expectations` | the board | entries with no evaluable expectation |
| `dead_judge` | judge emissions | a declared judge never fires |
| `noisy_judge` | §10's test–retest | pairwise disagreement above `0.25` |
| `margin_below_noise_floor` | §4 | `promote_margin` inside the measured A/A spread |
| `generalization_gap` | the generalization-gap lever (§12) | `holdout_loss − train_loss` widened past threshold |
| `refresh_cadence` | the rotation and refresh cadence (§12) | contract mined past `max_generations_per_contract` |
| `placebo_promoted` | §11 | CRITICAL: a no-op won a tournament |
| `preflight_signal_below_floor` / `preflight_saturated_contract` | §9 | the persisted pre-flight verdict re-surfaced every round (severity follows `preflight_gate` — §9.5) |
| `stalled_loop` | the round stream | no genuine progress (placebo arms filtered out first) |

Three rules govern any extension of this family:

- Detectors are **recommend-only**: they never gate and never mutate.
- They must **filter calibration probes out of optimization-stream logic**. A
  placebo arm is rejected by design every cadence tick, and a detector that
  counts it as another failed round reports a stall on a healthy loop.
- A finding that re-fires from PERSISTED state every round must not be
  `critical` unless the operator opted into a hard gate. `has_critical` feeds
  `DegenerateHealthPolicy`, so a repeating critical stops the loop instead of
  reporting on it (§9.5).

---

## 2. The gate's rule ladder

`evaluate_gate` (`src/zicato/tournament/gate.py`) is the single promotion
decision function. Its rungs apply **in order**; the first rejection wins and
is the one named in the journal. Prose elsewhere cites each rung by the name in
the second column. The full ladder, including the pieces wired around
`evaluate_gate` by the runner:

| Order | Rung | Knob | Rejects when | Reject reason prefix |
|---|---|---|---|---|
| 0 | Regression suite | `regression_gate_enabled` (default `False`) | the snapshot's own pytest suite fails or times out | (runner-level; see `tournament/regression.py`) |
| 1 | Scalar margin | `promote_margin` (default `0.01`) | `child_scalar > parent_scalar - promote_margin` | `challenger regressed:` / `insufficient improvement:` |
| 2 | Pass-rate monotonicity | `pass_rate_monotonicity` (default `True`) + `pass_rate_monotonicity_scope` (default `"per_entry"`) | scope-dependent, below | `pass-rate regression` |
| 3 | Namespace monotonicity | `namespace_monotonicity` flags | any flagged namespace's weighted aggregate rose past tolerance | `monotonicity_regression on namespace=` |
| 4 | Holdout confirmation | `overfitting.*` (default on, auto-degrades) | the train-win fails to hold on the holdout | `holdout_not_confirmed:` |

**The regression-suite rung** runs *before* the scoring gate, in
`_gate_with_regression` on the runner path: a patch can improve
`drift_loss`/`pass_rate` on the board while breaking the system under test's own
invariants, and no scoring signal may override a failing suite. It is opt-in because many adapters ship no
tests; a snapshot with no `tests/` directory is a silent, journaled skip
(`"no tests/ directory; skipped"`), never a stall. A timeout counts as a
failure with the distinct summary `"timeout after <N>s"`.

**The scalar-margin rung — promote-margin semantics.** The scalar is a loss, so
the literal check is:

```python
# src/zicato/tournament/gate.py — evaluate_gate, rule 1
    if child_scalar > parent_scalar - weights.promote_margin:
        if delta_scalar > 0.0:
            verdict = (
                f"challenger regressed: loss rose by {delta_scalar:.6f} "
                f"(champion {parent_scalar:.6f} -> challenger {child_scalar:.6f}); "
                f"a promotion needs the loss to drop by at least "
                f"{weights.promote_margin:.6f}"
            )
        else:
            improvement = -delta_scalar
            verdict = (
                f"insufficient improvement: loss fell by only "
                f"{improvement:.6f} ..."
            )
```

A promotion requires the child's loss to *drop by at least* `promote_margin`.
The two reject flavors are distinct because they carry different evidence: a
child that improved but not enough ("insufficient improvement") differs from a
child that got worse ("challenger regressed"). Both state the real
child-minus-parent delta. `promote_margin` is a **noise threshold** rather than
a quality bar; §4 explains why it must sit above the measured A/A floor and what
happens when it does not.

**The pass-rate-monotonicity rung — its two scopes.** The scope knob exists
because the right policy depends on what the board *is*:

- `"per_entry"` (default): for every entry the parent scored, the child's
  continuous score may not drop below the parent's by more than
  `PER_ENTRY_SCORE_MONOTONICITY_TOLERANCE` (`0.02`). A bool entry the parent
  passed has score `1.0`, so the child must still pass. A vanished row reads as
  `0.0`, because dropping ground truth is a regression. This scope suits
  invariant and regression-suite boards, where each entry is a promise.
- `"aggregate"`: reject only when the overall `mean_score` fell by more than
  `PASS_RATE_MONOTONICITY_TOLERANCE` (`1e-9`, pure float-noise padding). The
  child may trade individual entries as long as the net holds. This scope suits
  sampled or noisy evaluation boards: under per-entry scope, a single
  noise-flipped entry vetoes a truly better challenger (measured in §3).

There is no `"off"` scope value; disable the rung with
`pass_rate_monotonicity=False` so existing contracts stay byte-identical.

**The namespace-monotonicity rung** compares per-namespace *weighted
aggregates* (already sign-unified, §1.5) with
`NAMESPACE_MONOTONICITY_TOLERANCE` (`0.0`). Zero-weight namespaces are skipped
even when flagged: an operator who zeroed a namespace's scoring contribution
must not be surprised by it gating. The reason names every regressing namespace
rather than only the first.

**The holdout-confirmation rung** is applied only after the three train rungs —
scalar margin, pass-rate monotonicity, namespace monotonicity — would promote,
so a train reject always fires first with its specific reason. Both `None`
holdout arguments (small board, split disabled) skip the step entirely, and the
decision is then byte-identical to a gate with no train/holdout split at all.
Details in §5.

> ⚠️ TRAP: the gate's reject *reasons* are a stable surface. The dashboard's
> decision classifier and several tests consume the structured verdict fields
> (`deciding_rule`, `margin`, `regressed_*` — served by the reader layer, see
> 09-dashboard-and-query.md), but the human-readable strings also appear in
> journals that operators grep. If you must reword a reason, sweep consumers;
> never encode NEW machine-readable data only inside a reason string. That is
> the client-side re-derivation anti-pattern taught by the client champion-scan
> case (`12-bug-casebook.md` case 4).

`GateOutcome` records `delta_scalar` and `delta_pass_rate` **regardless of the
decision**, so the journal always has the same evidence shape whether the round
promoted, rejected, or deferred. Preserve that: dashboards render rejected
rounds too.

---

## 3. The noise doctrine

Read this section before touching *anything* that decides between two
generations.

**Every measurement in zicato is a random draw.** Agents under test are
LLM-backed and vary run to run; judges are LLM-backed and disagree with
themselves (§10); even "the same" generation re-evaluated produces a different
scalar. The decision procedure — margin gate, replication, monotonicity scope,
evidence gate, screen, holdout — is a statistical test executed against those
draws. It therefore has *operating characteristics*: a false-promotion rate
under the null (a challenger identical to the champion), and power at a given
true effect size. This repository holds those characteristics as **measured,
pinned facts**. The measurement instrument is
`tests/test_decision_procedure_power.py` (the decision-procedure power harness,
§13), driving the *real* tournament machinery under seeded noise.

### 3.1 The measured facts

These facts are the reason the defaults are what they are, and any change you
make must not silently invalidate them.

| # | Fact | Where measured / pinned |
|---|---|---|
| 1 | **A single naive duel promotes pure noise.** With `promote_margin=0.01` far below a measured A/A floor of ~0.66 (σ=0.22 harness) and no evidence gate, a challenger *identical* to the champion cleared the gate in **20 of 60** seeded A/A trials (the pinned test bound is ≥ 15/60). | `test_margin_below_noise_floor_without_evidence_gate_is_unsound` |
| 2 | **Confirmation compares the fitted strength difference with zero using covariance and planned-comparison allocation.** Mixed records can resolve as evidence grows; cost depends on effect size, planned field, and budget. | §6.5; `test_selection_evidence_gate.py`; `test_decision_procedure_power.py` |
| 3 | **Power is bought with replication.** Averaging 32 replicates shrinks the per-duel delta sd from ~0.66 to ~0.12, turning a 0.5×-floor true effect (~0.34) into a ~3-sigma-per-duel signal the win streak can sustain. | `EFFECTIVE_REPLICATES = 32` commentary + `test_power_at_planted_deltas` |
| 4 | **The evidence-gated contract's false-promotion rate under the A/A null is zero** over the pinned seeded trials — either the replicated crowning duel fails the margin, or the defer→replicate loop terminates `inconclusive`. | `test_aa_effective_contract_false_promotion_rate_is_zero` |
| 5 | **The naive default misses small true effects the effective contract catches**: at a ~0.5×-floor planted improvement, the naive contract promotes in ≤ half the trials; the effective contract's rate is pinned ≥ naive + 0.25 on the same seeds. | `test_power_at_planted_deltas` |
| 6 | **A 3×-floor effect is unmissable** (power 1.0 across every seeded trial) and power is monotone in effect size. | `test_power_at_planted_deltas` |
| 7 | **Screen false-veto ≈ flip-rate² under confirm-before-veto.** At per-entry flip noise σ=0.10 the confirmed rule measures ~1.0% false vetoes (pinned ≤ 2%) while the naive any-flip rule measures ~10% (pinned ≥ 5%, and confirmed ≤ naive/3). At the hot σ=0.22 world the squaring still holds (~σ² ≈ 4.8%) but *no* single-confirm rule can reach 2% there. | `test_screen_false_veto_rate_confirm_beats_naive_any_flip` |
| 8 | **The A/A noise floor of a deterministic harness is exactly 0.0**, and of the σ=0.22 harness ≈ 0.663 (analytically `1.6·sqrt(σ(1−σ))` for that harness's structure). A measured floor of ~0 on a stochastic harness means the *seeding is broken* rather than that the harness is quiet — see the A/A false-zero-floor case (`12-bug-casebook.md` case 3). | `test_aa_null_calibration_measures_the_noise_floor` |

### 3.2 What the doctrine demands of a change

- **Any new comparison between two measured quantities needs a stated noise
  model.** "Child scalar < parent scalar" is not a decision procedure; "child
  scalar < parent scalar − margin, margin calibrated above the measured A/A
  floor, replicated K times" is.
- **Any new veto/gate needs a measured false-positive rate under the null.**
  The screen's confirm-before-veto design (§3.3; the proposer-side wiring lives
  in 05-proposer.md) exists because the naive rule's false-veto rate measures ~σ
  per flip-capable entry, an order of magnitude above what the screen can
  accept.
- **Any claim of improved power needs the planted-delta measurement**, at
  effect sizes stated in multiples of the measured floor (the harness plants
  0.5×, 1×, 3×).
- **Statistical policy changes require explicit measurements.** Workspace
  scaffolds enable confirmation with a visible budget. Its comparison allowance
  depends on the planned field and number of refits (§6.5). Required null and
  power tests measure the effect of changes to that rule.

> ⛔ NEVER assert a statistical property in a docstring, commit message, or
> test name without a pinned measurement behind it. The power harness states the
> rule directly: an operating characteristic is reported only where it has been
> measured.

> ⚠️ TRAP: deterministic test contracts hide noise bugs. The convergence
> oracle (`tests/test_convergence_known_answer.py`) runs a σ=0 world where a
> cache replay and a fresh draw are *equal by value*, so a procedure that
> replays one sample N times looks correct there. That is how the evidence-gate
> replicate-slot reuse case (`12-bug-casebook.md` case 8) — where evidence
> replicates were not independent samples — passed a green deterministic
> end-to-end test. Every statistical mechanism needs at least one knob-ON test
> under σ>0. See 12-bug-casebook.md §"The meta-lessons".

### 3.3 The screen's statistical doctrine: veto-first, selection bias, confirm-before-veto

The pre-tournament candidate screen (`src/zicato/epoch/screen.py`; the
proposer-side wiring is in 05-proposer.md) is the worked example of designing a
*new* decision surface under the noise doctrine, and of what a weaker estimator
may and may not decide.

The screen is a **worse-powered estimator than the tournament it precedes**:
1–2 entries × 1 replicate against a full board × replicates. The measurement of
that gap: a 2-entry screen ranking close candidates is approximately random
choice plus winner's curse. Four rules follow.

- **Veto-first, never ranking.** The screen's high-confidence regime is
  *categorical failure* — a candidate that flips entries the champion passes,
  or exhausts its wall-clock budget, is detectably broken even at n=1. So the
  screen DISQUALIFIES; the best-of-N critic/heuristic still chooses among
  survivors; an all-vetoed slate falls back to critic-over-all (a veto can
  narrow but never empty a propose step, and a screen *error* degrades to
  no-signal — the screen must never fail a round).
- **Confirm-before-veto.** A pass-flip is a *suspected* veto: the flipped
  entries re-run once at `MeasurementDraw(MeasurementPurpose.SCREEN, 1)`,
  and only a flip that repeats vetoes. Under per-entry flip probability p the false-veto
  probability is bounded near p² instead of p — the measured rates are fact
  #7 in §3.1. Budget aborts skip the confirm (deterministic; nothing to buy).
- **The panel scalar is selection-biased by construction** — a handful of
  champion-passing train entries chosen *for the veto*. It is advisory
  tiebreak material inside the slate only, and it is **never journaled as
  evidence, never compared against tournament scalars**. Winner's curse on
  the survivor is tolerable because the tournament re-measures with
  fresh draws and the holdout confirmation still guards promotion (§5).
- **Restricted visibility holds**: the panel is train-slice only (the holdout
  is never eligible), and every result string carries counts only — never an
  entry id (`_summarize` in `screen.py`).

> ⛔ NEVER promote a screen scalar (or any selection-biased, small-panel
> measurement) into gate evidence, standings, or the journal's scored record.
> The moment a biased estimator's number sits next to an unbiased one in a
> comparable field, some later consumer will compare them.

---

## 4. A/A noise-floor calibration

**What it is.** The standard A/A test of A/B methodology: evaluate the SAME
generation K times and look at the spread of the resulting scalars. Any two
draws form an A/A duel — two arms carrying identical treatment — whose true
effect is exactly zero, so the observed `delta_scalar` spread IS the noise
floor. Home: `src/zicato/tournament/calibration.py`.

**How it measures.** `measure_noise_floor` runs K draws of the champion through
`_run_board_units_fast`, using the same workers, scoring, and persistence as a
duel. The default `DEFAULT_CALIBRATION_RUNS = 5` gives 10 pairwise deltas.

Each draw uses `MeasurementDraw(MeasurementPurpose.CALIBRATION, draw)`.
The runner resolves its base seed from the runtime configuration. Distinct draw
numbers identify separate samples; repeating the audit with the same contract,
generation, and seed can reuse completed calibration draws.

The entry context carries the complete measurement as JSON through
`_stamp_measurement`. A seeded harness must include the purpose and draw in
its noise seed (§13.1). Distinct filenames alone cannot produce fresh noise.

```python
# Measurement selection in measure_noise_floor
for draw in range(runs):
    measurement = MeasurementDraw(MeasurementPurpose.CALIBRATION, draw)
    stamped_board = _stamp_measurement(board, measurement)
    # Pass stamped_board and measurement to _run_board_units_fast.
```

**The two spread statistics** (`delta_spread`, pure and unit-testable):

- `max_abs_delta = max(scalars) − min(scalars)` — the largest `|delta_scalar|`
  any A/A pairing could have shown. **THE floor**: a `promote_margin` below it
  cannot distinguish a real improvement from a re-roll.
- `delta_std = sqrt(2) · population_std(scalars)` — the sd of the difference
  of two independent draws.

**Where it is persisted.** Onto the epoch record — `config.json`'s *additive*
`noise_floor` field via `zicato.epoch.lifecycle.set_epoch_noise_floor`. It is
a **runtime measurement, never a contract input, never hashed** (mirroring the
`goal` field). Changing it does not roll the epoch. The floor records the base seed selected
for its calibration draws. It estimates an evaluation distribution, so a later
seed choice may reuse the identified estimate without claiming those physical
draws were executed under the later seed. A seed change alone does not trigger
automatic recalibration; explicit calibration can replace the estimate.

**When it runs.** Three wirings:

| Surface | Trigger |
|---|---|
| `zicato board audit` | manual, any time; measures the current champion and persists |
| epoch-open hook | workspace `config.json` `"calibrate_noise_floor": K` — once per epoch at the first evolve round, idempotent, best-effort |
| evolve-start check + per-round health | reads the persisted floor; see below |

**What the epoch-open hook costs.** K draws x every board entry, serially,
before the first duel of the epoch's first round — `--parallelism` buys
nothing, so K=3 on a 6-entry board is 18 board-entry runs of dead time up
front. The step logs that arithmetic before its first draw and reports
`{done}/{K}` on the heartbeat as each draw settles (`CALIBRATION_PHASE`), so
the wait is legible rather than mistakable for a hung round. The
`"contract_preflight"` hook below reports itself the same way, over the A/A
draws AND its degraded probes. A loop with no consumer for the floor — no
promote-margin bar to defend, no A/A arm — should set
`"calibrate_noise_floor": 0` rather than pay it.

**The margin-vs-floor warning.** `margin_below_floor(promote_margin, floor)`
returns true when the margin is strictly below the measured `max_abs_delta`.
The health finding `margin_below_noise_floor`
(`src/zicato/health/diagnostics.py::detect_margin_below_noise_floor`) fires as
a **warning** when the evidence gate is off ("duels are decided by the margin
alone") and downgrades to **info** when the gate is on (the defer→replicate
loop still holds promotions to separation of the rating CIs). It never hard-refuses a run —
calibration is recommend-only, like every board-reflection surface.

Four surfaces report that condition: the round-0 evolve log line
(`evolve/round_prepare.py::_warn_margin_below_noise_floor`), that health
finding, the board-reflection finding (`reflection/findings.py`), and the
`promotion_hygiene` practice check (`reflection/practices.py`). None of them
does the arithmetic. `assess_margin_against_floor` in `tournament/calibration.py`
returns one `MarginNoiseAssessment` carrying the comparison, the recommended
margin, which floor statistic backed it, and whether acting on it would raise
the gate; `assess_margin_against_floor_record` is the tolerant entry point for
a persisted floor record, and `margin_below_floor` is its predicate half.
Health converts that assessment into a finding — grading it by the evidence
gate is health's own policy — and the other three render it. Reflection keeps
one thing of its own: the `set_gate` payload built from the recommendation.
`tests/test_noise_floor_one_owner.py` pins the correspondence.

> ✅ ALWAYS treat `NoiseFloor.to_json()` as a tolerant read on the consumer
> side: `margin_below_floor` returns `False` for `None`/malformed input by
> contract. A dashboard or health reader that raises on a missing floor breaks
> every workspace that never calibrated.

> ⚠️ TRAP: a floor of exactly `0.0` has two very different meanings. Either the
> harness is deterministic (the convergence example's planted-defect adapter in
> `examples/zicato_examples/target_0_convergence` measures exactly 0.0 by
> design), or a seeding bug made every draw re-roll the same sample. If you see
> 0.0 on a harness you believe is stochastic, suspect the
> stamp path first (`_stamp_measurement` must reach the entries the run
> actually consumes), and confirm with the power harness's floor test which
> asserts the σ=0.22 world lands in `[0.4, 1.0]`.

---

## 5. The Ladder-mediated holdout

The train/holdout split (`src/zicato/board/split.py`) and the gate's
holdout-confirmation rung (§2) make a *single* holdout query trustworthy. They
do nothing about the deeper failure: the loop queries the *same* holdout every
round, adaptively, and reuse spends a holdout — its confirmations become an
optimistically biased signal. The governor in
`src/zicato/tournament/ladder.py` limits feedback with a finite query allowance
and a training-improvement release threshold. These practical controls do not
inherit a distribution-free guarantee for arbitrary adaptive reuse.

### 5.1 What counts as a query, and the two rules

Here, a **query** is a statistical consultation of the hidden holdout slice.
It is not one target-model call, tool call, database query, board entry, or
replicate. One crowning champion-versus-challenger comparison across the
holdout consumes one query, however much work that comparison performs.

The term matters at the scheduling boundary. A train rejection consumes no
query because the holdout is not consulted. A train promotion with a non-empty
holdout consumes one. A withheld result also consumes one because the runner
observed the holdout before deciding what feedback to release.

**Release rule.** A holdout-based signal is *released* — allowed to flip a
train-win to confirmed/rejected — only when the **train-measured** improvement
over the champion clears the threshold:

```python
# src/zicato/tournament/ladder.py — query_holdout (release decision)
    improvement = train_parent_scalar - train_child_scalar
    if improvement >= threshold:
        # Release: the holdout result counts this round.
        ...
    # Withhold: re-report the previous best confirmation so the proposer
    # cannot chase the fluctuation; the holdout result does NOT count.
```

Within the band the Ladder **withholds**: it re-reports the previous best
confirmation (`LadderState.best_confirmed`) and the round's raw holdout result
does not count. The threshold seeds from the gate's existing `promote_margin`
when `ladder.threshold` is null. An explicit threshold, including zero,
sets the release bar directly.

**Budget rule.** Every query that consults the holdout charges one unit of the
per-epoch budget (`LadderConfig.budget`), charged *before* the release
decision. A withheld query still pays, because the holdout was consulted to
learn the gap was inside the band. When the budget is exhausted, nothing is
released. Required confirmation stays incomplete, the round defers, and the
champion is retained. The operator must refresh the evaluation contract before
more holdout-confirmed promotions can proceed; resetting a counter or merely
rotating reused tasks does not supply fresh evaluation evidence.

| Runner activity | Query charge |
|---|---:|
| train matchup or train rejection | 0 |
| one complete crowning holdout matchup | 1 |
| entries, replicates, target calls, and tool calls inside the matchup | 0 additional queries |
| inspected result that the Ladder withholds | 1 |
| empty holdout | 0 |

The configured budget bounds feedback within one epoch. It is not a
distribution-free proof that the holdout supports that many consultations.
Holdout size, noise, released information, and reuse of the same tasks across
epochs determine the supported assurance. Hash-derived splits rotate across
epochs by default. Rotation over one finite board does not create new data,
and explicitly tagged holdout entries never rotate.

The runner enforces the budget at the scheduling boundary. It serializes the
epoch-local state, atomically publishes a one-query debit, and only then starts
the holdout matchup. A zero balance skips the matchup, so no fresh holdout
evidence exists to release. The promotion remains deferred.

The debit creates an opaque reservation identity bound to the epoch-local
state. The pending record stores the budget before the charge, which later
supplies the released evidence block. Final publication consumes the record.
Reusing the identity, or presenting it to another workspace or epoch, raises
without publishing the holdout result. A mismatched path is rejected before
the target state is opened. The runner also raises before accessing the
holdout on a platform that cannot serialize reservations across processes.

The first state creation also writes an initialization marker. A missing state
is valid only when neither the state nor the marker exists. A malformed state,
an established state that disappeared, or a failed atomic write raises before
the holdout runner starts. A crash after reservation may waste the charged
query. It cannot restore the charge or expose holdout evidence first.

### 5.2 Confirmation status and restricted feedback

Required holdout confirmation is `satisfied` only by complete, released
positive evidence for the current challenger. A released negative result is
`failed` and rejects the challenger. Withheld evidence, incomplete execution,
or exhausted allowance is `incomplete` and defers promotion. An absent holdout
slice is explicitly `disabled`. Disabling the query governor still requires
raw holdout confirmation.

The proposer receives only a released confirmation bit. A withheld result uses
a generic reason that reveals neither its raw negative bit nor its scalar.
`LadderRelease.confirmed` may repeat a historical best bit when withholding;
that bit cannot satisfy the current candidate. Raising the release threshold
can defer a candidate, never authorize one that failed confirmation.

### 5.3 The asymmetry rationale

The holdout confirmation itself (`_holdout_confirms` in
`tournament/gate.py`) is asymmetric on purpose:

- it rejects when the challenger's holdout loss **rose past** the champion's
  by more than `promote_margin`, which marks a real holdout regression rather
  than noise, or
  when the holdout shows a pass-rate regression under the SAME
  `pass_rate_monotonicity_scope` the train slice uses (one consistent policy —
  per-entry on both sides, or aggregate on both);
- it is **never** asked to clear `promote_margin` in the *improving*
  direction. A train-measured win that merely holds flat on the holdout counts
  as a confirmation rather than a failure.

This asymmetry is what makes the holdout a guard against *board
memorization* rather than a second, stricter promotion bar. If you "tighten"
it into requiring holdout improvement, you halve the loop's power for zero
soundness gain. The holdout slice is small and its per-round measurement is
noisier than the train slice, so demanding improvement on it demands a signal
the slice cannot statistically deliver.

### 5.4 State, persistence, and the record shape

`LadderState` (budget totals, `best_holdout_scalar`, `best_confirmed`) is a
small frozen object the runner persists across rounds per epoch; the module
itself is **pure** — no filesystem, no clock, no randomness. The epoch scope is
an implementation boundary. It does not make a repeated holdout task fresh,
so campaign design must account for task reuse across epoch rolls. The stable
`record.holdout` block is assembled by `holdout_record`; the dashboard reads a
display subset from that durable record. The record contains the released
confirmation, train and allowed holdout scalars, and the release threshold. It
also records whether the holdout was consulted, whether the query was durably
reserved, and the budget before and after the decision.
The concrete keys are `confirmed`, `train_scalar`, `holdout_scalar`,
`holdout_consulted`, `ladder_released`, `ladder_budget_total`,
`ladder_budget_before_query`, `ladder_budget_remaining`,
`ladder_query_reserved`, `threshold`, `confirmation_status`, and `reason`.

An absent holdout slice produces `confirmation_status=disabled`. A training
rejection skips its conditional holdout confirmation and has no block. An
exhausted budget produces a block with `holdout_consulted=false` and
`ladder_query_reserved=false`; the block records why no comparison ran without
claiming fresh evidence.

When the holdout is empty — a board under
`overfitting.min_board_size_for_split` (default 6) with no explicit `holdout`
tag, or the split disabled — the Ladder is never consulted and behavior is
the training decision is preserved with an explicit disabled record. When
`LadderConfig.enabled` is `False`, the runner runs that raw confirmation
directly, with no budget and no release rule.

Holdout confirmation is wired through **every** structure rather than the
gauntlet alone: the multi-challenger path routes its crowning through
`runner.confirm_crowning_holdout` (see 06-tournament-and-selection.md
§6.7.3).

> ⛔ NEVER surface a raw holdout artifact to the proposer — no per-entry
> result, no unreleased scalar, no entry id. The proposer's holdout
> view is exactly one bit (the released/re-reported confirmation), by
> Blum–Hardt design. Any widening of that channel re-opens adaptive
> overfitting of the holdout and invalidates the reuse guarantee — this is an
> overfitting-boundary change and requires a design pass (see
> 14-goals-and-roadmap.md §"How to propose new work").

---

## 6. The evidence gate — the Bradley–Terry pre-gate

Home: `src/zicato/selection/evidence_gate.py` (pure verdict machinery) +
`src/zicato/selection/driver.py::confirm_promotion_with_evidence` (the
defer→replicate loop) + `selection/driver.py::make_evidence_replicate_duel`,
which constructs the confirmation runner shared by tournament paths. Opt-in via
`TournamentStructure.params["promote_confidence_threshold"]` — an absent param
adds nothing to the contract canonical form, so the contract hash is
byte-identical when the operator does not opt in.

### 6.1 The verdict

`evidence_verdict` fits Bradley–Terry over admitted independent confirmation
draws of the crowning pair. The strategy fixes that pair using separate
observations. Those selection observations remain visible but never enter
confirmation: racing rungs can overlap, and selecting their winner conditions
on their outcomes. The verdict is:

- **`promoted`** — the adjusted strength-difference interval lies above zero
  and the probability threshold is met.
- **`deferred`** — confirmation remains unresolved and replicate budget remains.
- **`inconclusive`** — the budget is exhausted without confirmation. The terminal
  record retains the evidence and the champion stands.

The difference interval includes covariance and allocates its probability tail
across the planned candidate family and possible refits; §6.5 defines the rule.
A fit is trusted only at `MIN_CREDIBLE_DUELS = 3` resolved duels for the pair.
Below that, the verdict reports `credible=False`. The configured probability
threshold is subject to the minimum one-sided probability 0.975 and comparison
allocation, so the scaffold value 0.8 does not imply a 20% error allowance.

### 6.2 Confirmation draws measure both competitors independently

`make_evidence_replicate_duel` requests each additional comparison with
`first_measurement=MeasurementDraw(MeasurementPurpose.CONFIRMATION, j)`,
where `j` advances from zero. The matchup identity is
`confirmation:r{j}:{left_id}:{right_id}`.

Both competitors use the requested confirmation draw. In fast mode, a missing
draw runs and a completed draw can be reused after interruption. Full mode
remeasures both competitors. Neither mode can substitute a tournament draw
for a confirmation draw. Replaying an admitted sample would falsely reduce
uncertainty; rerunning one side alone would understate the paired variance.

Confirmation files remain separate from the tournament files that selected
the challenger. The factory also passes `cache_scores=False`, so confirmation
aggregates cannot replace the tournament’s cached generation scores.

### 6.3 The duplicate-audit refusal

The driver records every attempt and charges each requested draw before
calling the runner. A repeated matchup identity cannot enter the fit twice.
The driver also checks the returned `measurement_draw`, including its purpose,
draw, and base seed, and rejects a repeated measurement for either competitor.
Matchup names alone cannot establish fresh evidence. Actual independence
still depends on correct execution.

A runner that repeatedly returns one draw spends its finite budget and leaves
confirmation incomplete. Ties, incomplete executions, nonfinite results,
unexpected pairs, and errors remain auditable without counting as resolved
pair evidence.

### 6.4 The two-phase loop and the dead-letter terminal

`confirm_promotion_with_evidence` is part of `evaluate_tournament` for every
selection strategy:

- **Bootstrap** — confirmation begins with no admitted draws. The loop
  measures the fixed crowning pair up to `MIN_CREDIBLE_DUELS` before judging. Missing runner or exhausted budget terminates inconclusive;
  a lack of credible evidence cannot authorize promotion.
- **Refine** — once credible: `promoted` terminates with the crown;
  `deferred` spends another fresh crowning-pair replicate and refits. Budget
  exhaustion or runner failure without confirmation terminates `inconclusive`.

An `inconclusive` terminal maps onto the closed decision enum's `DEFERRED`
token, which keeps the duel for analysis and leaves the lineage head unchanged.
It also fires `on_inconclusive`, which the orchestrator wires to the
dead-letter writer: one record per unresolved duel at
`runtime/inconclusive/<generation_id>.json`
(`src/zicato/selection/dead_letter.py`), carrying the full `gate.rating` block
and the per-refit `ci_history`. Each returned or failed attempt records its
pair, draw identity, eligibility, budget charge, and reason. Ties, duplicates,
nonfinite results, incomplete executions, and runner errors remain auditable
but cannot manufacture resolved pair evidence.

Both confirmation records use explicit `disabled`, `satisfied`, `failed`, and
`incomplete` statuses. Statistical insufficiency means confirmation is
incomplete; it provides no negative holdout finding. Evaluator revision 2 includes these decision semantics and
the covariance-aware rating contrast in the frozen contract hash. Upgrading
rolls an epoch through the existing contract-drift mechanism; it does not
rewrite historical decisions.

> ✅ ALWAYS pass gate-rejects through the pre-gate untouched. The pre-gate is
> consulted only on a gate-promote and can only *hold* a promotion
> (`decision != "promoted"` returns the base verdict verbatim). Any change
> that lets it manufacture a promotion — or a rejection — breaks the
> protected-incumbent invariant.

A confirmation rule change requires unchanged-system controls, planted
improvements, and cost measurements. Changing uncertainty mathematics or its
probability allowance cannot be validated by accepting a favorable sequence
alone.

### 6.5 Strength differences and planned confirmation comparisons

The Bradley–Terry model assigns duel-win probability
`logistic(theta_child - theta_parent)` to the challenger. The fitted quantity
is the log odds of winning a duel. The scalar gate separately enforces the
optimization objective and pass-rate requirements. Confirmation discards the
magnitude of each scalar difference. A paired scalar model could use more
information; adopting one requires its own distributional assumptions and
operating-characteristic measurements.

The fit maximizes the log likelihood with a Gaussian ridge prior of precision
`prior=1.0`. The prior keeps perfect records finite. Its inverse penalized
information is a local normal approximation to the posterior covariance.
Strengths and covariance are centered together: with `P = I - 11'/n`, the
reported covariance is `P C P'`, where `C` is the uncentered inverse information
and `n` is the number of fitted contestants.

For a challenger-parent difference `d`, the mean is
`theta_child - theta_parent` and its variance is
`C_child,child + C_parent,parent - 2*C_child,parent`. A common strength shift
or shared location uncertainty therefore cancels. The returned rating remains
a mapping of contestant ids to `(theta, standard_error)` and also retains
joint covariance. Consumers use `RatingFit.difference` for comparisons.

Independent reference tests reduce a balanced pair to the scalar parameter
`theta_child=t`, `theta_parent=-t`. With `N` games its standard error is
`1/sqrt(N + 2*prior)`. Mixed 80% winning records also resolve as their sample
count grows. These tests protect uncertainty convergence rather than only
checking that one small sample has a smaller error than another. Joint
covariance is necessary when comparing fitted abilities; see
[Turner and Firth's Bradley–Terry treatment](https://www.jstatsoft.org/article/view/v048i09).

Confirmation applies two changes with distinct purposes:

- Covariance correction measures uncertainty in the difference instead of
  separating individual strength intervals.
- Comparison allocation accounts for choosing a finalist and repeatedly
  refitting confirmation evidence. The driver captures the planned candidate
  count `K` before requesting candidates. Failed applications do not reduce it.
  With `B` extra duels, at most `K*(B+1)` comparisons can produce a promotion.

Let `q` be the authored probability threshold. The one-sided error allowance is
`a = min(1-q, 0.025)`, retaining the positive endpoint of the existing two-sided
95% standard. Each comparison uses tail `a/(K*(B+1))`. Its normal interval uses
quantile `Phi^-1(1-a/(K*(B+1)))`, where `Phi` is the standard normal distribution
function. The challenger is confirmed only when the lower bound is positive.
The rating record includes the applied confidence level and comparison count.
Individual 95% intervals and their overlap remain diagnostic.

This is [Bonferroni allocation](https://www.itl.nist.gov/div898/handbook/prc/section4/prc473.htm).
It does not require independence between looks, but its coverage claim depends
on calibrated individual intervals. The normal approximation, ridge shrinkage,
adaptive tournament schedules, and repeated rounds prevent treating the nominal
allowance as a universal observed false-promotion guarantee. Seeded controls,
planted improvements, and complete worker measurements remain required.

At threshold 0.8 with 32 confirmation draws, an isolated pair with unanimous
wins first confirms after 16 draws for one planned candidate and 21 for four.
An exact Bernoulli stopping calculation gives conditional null promotion
probabilities 0.00048793 and 0.00002545 respectively. These calculations begin
with zero confirmation observations and assume independent, identically
distributed duel outcomes with no ties. They do not establish the full racing
procedure's error rate. The deterministic racing driver confirms one, two,
and four applied candidates within the configured budget while retaining the
four-candidate allocation.

The recorded confirmation block declares `evidence_basis=independent_confirmation`.
The dashboard reads that block, including its attempts and confidence history.
Historical strategy summaries without that basis remain unknown or incomplete;
matchup counts cannot reconstruct independent confidence intervals. See
[confirmation power and cost](../design/CONFIRMATION-POWER.md) for measured
operating characteristics and the finite-noise reference.

### 6.6 The visibility rating fold (index-side BT on the Elo scale)

Home: `src/zicato/index/elo.py::fold_elo_into_index`, run on every reindex /
ingest after the tournaments land. This is the SAME `fit_bradley_terry`
engine as §6.5, in a different role: a **read-only analytics fold** over the
persisted match ledger that writes each generation's
`generations.elo` / `elo_se` / `elo_games` columns (schema v10 + v12). The
fitted strength is mapped onto the conventional Elo scale for legibility —
`elo = 1500 + theta·(400/ln 10)` — so a 400-point gap reads as 10:1 modeled
odds and the zero-sum gauge puts the field mean at 1500. These are descriptive
ratings. The ledger lacks independent measurement provenance, so `elo_se` is
stored and served as null, including reads of historical derived indexes.

The doctrine, in one line: **the rating is for VISIBILITY, never the gate.**
The fold writes the three columns and nothing gate-side ever reads them back
— the standings tables, the gens roster, and the candidate dossier render
them; `evaluate_gate` / the selection strategies never touch them (pinned by
`test_rating_columns_are_never_read_gate_side`). Facts a consumer must hold:

- **Batch and order-independent.** The fold is a batch maximum-likelihood fit
  over the de-duplicated game list (crowning rows + field-bracket rows, keyed
  `(tournament_id, match_id, {sides})`), so the same ledger yields identical
  point ratings in any fold order — re-derived from scratch at every
  ingest, never incrementally updated.
- **Margins are ignored.** BT is fit on win/loss only; the
  `|delta_scalar|` magnitude rides the *gate* (§2), and folding it into the
  rating would double-count the same evidence.
- **Zero games ⇒ NULL rather than a carried prior.** A generation that never played
  a settled two-competitor duel has no measured strength; its columns stay
  NULL and the display renders `—` (honest-degrade, never a fabricated
  number).
- **Racing rungs are rated with the Plackett–Luce (PL) model.** A racing
  intermediate rung
  persists a survivor/cut *set* with no single named winner. The fold's fit is
  `fit_plackett_luce`, a strict generalisation of `fit_bradley_terry`. A
  two-competitor game is the singleton case where PL's choice probability
  `p_i/(p_i+p_j)` *is* the BT logistic, so pairwise ratings stay byte-identical
  to the BT fit, pinned by a reduction test. A rung is a grouped observation:
  survivor set `S` above cut set `C`, whose likelihood is the **exact marginal
  over the within-`S` orderings** (`|S|!` sequential-choice terms; the
  within-`C` orderings marginalise to one). A generation cut only at a rung
  therefore carries a rating, which a pairwise-only fit cannot give it.
  `elo_games` counts *observations a generation appeared in*: a game counts for
  its two sides, and a rung group counts once per participant. Guards: a survivor set over
  `PL_MAX_SURVIVORS = 8` is skipped with a debug log (never approximated —
  the marginal is factorial in `|S|`, and racing fields are single-digit);
  rung groups de-dup on `(tournament_id, rung_id)`. Slice size is
  **unweighted**: a thin-slice rung is noisier evidence but weighs the same,
  which is acceptable because the rating never gates. Variance-aware weighting
  is registered as future work.
- **Display honesty.** Point ratings and observation counts remain
  descriptive. Without independent measurement provenance, `elo_se` is null
  and no uncertainty interval is drawn. The independent confirmation block is
  the source of promotion confidence. The descriptive match ledger supplies
  point ratings and counts.

---

## 7. Replication semantics

Replication is the loop's power lever (§3, fact #3). Its mechanics:

### 7.1 Averaging and the strict-majority pass

`run_matchup(..., replicates=N)` runs the paired board N times for every
production tournament structure. The standalone `run_tournament` API also
runs paired replicates; `run_fast_mode` remains a one-sided debug API.
`average_replicate_losses`
(`src/zicato/tournament/scoring.py`) folds the N runs into one per-entry
loss map *before* aggregation.

Scoring never sees the individual replicates, so a field the fold does not
aggregate is DISCARDED rather than merely unaveraged. The rule the fold holds
to is:
**a field the scalar or the gate reads is aggregated; a field neither reads
carries the first replicate in the requested sequence**, and its docstring names every
pass-through with the reason it may be one.

Aggregated:

- `drift_loss` — the arithmetic mean; reaches the scalar as the `"drift"`
  component;
- `score` — the mean of each replicate's **resolved outcome**, that is of
  `entry_score(replicate)` rather than of the raw `score` field. `entry_score`
  reads that resolved outcome FIRST, so it is the continuous outcome axis the
  duel turns on, and folding it is what makes the fold correct in the two cases
  where the raw field is unset. Only ONE of those two is an abstention. An entry
  with **no expectation at all** produces no outcome on any replicate and folds
  to `None`, excluded from `mean_score` the same way a single unreplicated draw
  is. An **aborted** replicate (spent budget, infra kill) records `score=None`
  with `pass_fail=False`, because it observed a failure rather than nothing, so
  `entry_score` maps it to `0.0` and it votes. Treating an abort as an
  abstention lets a K-replicate duel ignore a measured failure: one
  clean pass plus one abort reports the clean replicate's `1.0` verbatim while
  `pass_fail`'s majority says `False`, giving a folded profile that contradicts
  itself. Folding the resolved outcome also means an all-bool board (score-less,
  `pass_fail` only) gets the same arithmetic as a scored one — 1 of 4 replicates
  passing reads `0.25` rather than the single majority bit;
- `metrics` — per-key mean over the replicates reporting the key, so the
  decomposition decomposes the folded `score` beside it;
- `metric_counts` (and the `tokens_spent` / `output_chars` / `schema_failures`
  scalars) — namespace-bearing via `aggregate_namespaced_metrics`, whose
  per-namespace values are summed into the scalar for any contract with a
  non-zero `cost:` / `output:` / `schema:` weight. Meaned with an
  absent-bucket-contributes-zero divisor, which is the per-run-mean model that
  aggregator uses — so the namespace aggregate over the fold equals
  the aggregate over the replicates it folded;
- `per_judge_loss` — meaned per judge; it rides `ScalarContext`, so a scalar
  plugin can read it;
- `pass_fail` — the **strict-majority vote** (`true_count * 2 > len(votes)`; an
  even split is a fail), with `None` preserved when no replicate produced a
  pass/fail. This vote does not decide the scalar: `entry_score` returns the
  folded continuous outcome before it can consult `pass_fail`. The vote drives
  the binary `pass_rate` and the gate's `pass_fail` fallback for score-less
  aggregates, so it stays a majority rather than a mean, and it can disagree in
  sign with the folded `score` (2 of 5 replicates passing is `pass_fail` `False`
  and `score` `0.4`). Those are the binary and the continuous view of one duel
  rather than an inconsistency.

Pass-through from the first requested replicate, and why each may be: `run_id` /
`expectation_result` (raw provenance of the representative replicate — the fold
is not a run and has no matcher verdict of its own);
`abort_cause` / cache provenance and friends (they describe ONE execution and
have no meaningful fold).

A fold of multiple draws sets `measurement=None` and preserves their identities
in `source_measurements`. It also retains an incomplete-execution marker if
any requested unit did not start. The gate reads the folded loss without
misrepresenting that aggregate as one physical measurement.

### 7.2 Per-structure defaults

The base `SelectionStrategy` declares `_default_replicates = 2` — the
noise-aware default (`src/zicato/selection/strategy.py`). Per structure:

| Structure | Default `replicates` | Rationale (from the strategy docstrings) |
|---|---|---|
| gauntlet | 2 (inherits base) | pin `"replicates": 1` in params for single-run behavior |
| single_elim | 2 | a single-elim knockout has no second chance; replication is its noise defense |
| double_elim | 2 | replication rather than relying on the losers' bracket for noise correction |
| swiss | 2 | per-pairing replication is how a swiss earns trustworthy standings |
| racing | **1** | racing's adaptive resource allocation (rung halving) IS its per-sample noise weapon; the final rung runs the full board × replicates × both sides and is already the expensive step |

`replicates` lives in `TournamentStructure.params`, so changing it **rolls the
epoch** — it changes what a measurement *is* under the contract.

### 7.3 Measurement identity determines artifact paths

Within an epoch, the unit cache distinguishes generation, entry, purpose,
draw, and base seed. `_unit_loss_path` maps that identity to
`run_dir/seed-{seed}/loss.{purpose}.r{draw}.json`, where `run_dir` is the
entry’s run directory. An explicitly unset seed uses `seed-none`.

Events, captured results, and judge captures use the same directory and
measurement suffix: `events.{purpose}.r{draw}.jsonl`,
`result.{purpose}.r{draw}.json`, and `judge_io.{purpose}.r{draw}.jsonl`.
Tournament draw zero follows this naming rule.

`first_measurement` selects the purpose and starting draw for a replicated
matchup. Replicate `i` uses `first_measurement.offset(i)`. The runner stamps
the same identity into entry context and records the selected runtime seed.
The worker request, persisted record, and path must agree.

Cache reuse is incremental: a request runs only missing eligible draws when
reuse is enabled. Forced reruns retain displaced artifacts before replacing
a slot. Retained attempts document execution history and cannot count as
additional independent draws. An interrupted run can reuse valid completed
measurements; incomplete or conflicting records cannot satisfy a cache read.

`champion_eval_mode` provenance (`"full"` / `"fast"` / `"fast-degraded"`) is
derived from the LEFT side's pre-run cache state and is journal provenance
only — it never enters the gate or the contract.

---

### 7.4 Where replication does and does not reduce variance

Production tournament evaluation resolves every requested replicate for both
competitors through the cache key `(generation, entry, purpose, draw, base_seed)`
within an epoch. Fast mode may reuse a completed draw only for the requested
identity. Missing slots run and persist independently. Full mode
forces both competitors fresh. The power analysis therefore describes the
paired production contrast.

The standalone `run_fast_mode` library API accepts an aggregate measured
earlier and evaluates only the challenger. Direct callers of that API have a one-sided
contrast and must not apply the paired-variance formula. The evolve pipeline
does not call that API.

After the per-round token budget expires, requested replicate slots still
receive explicit attempt records for missing units. Existing measurements can
be reused at no cost. The fold preserves the incomplete-execution fact, so an
omitted draw cannot turn a smaller sample into apparently complete evidence.
Attempt records never populate measurement slots.

---

## 8. Measurement purposes separate evidence sources

`core/measurement.py` defines `MeasurementDraw(purpose, draw, base_seed)`.
The purpose is a `MeasurementPurpose`, the draw is a nonnegative integer,
and the base seed is an integer or `None`. Each purpose numbers its own
draws from zero. Draw counts must be positive when work is requested.

| Purpose value | Meaning |
|---|---|
| `tournament` | Paired samples used to select a challenger |
| `calibration` | Repeated measurements of a generation’s own source for the noise floor |
| `contract_preflight` | Degraded-copy probes of selected mutation points |
| `candidate_screen` | Candidate panel evaluation; draw one confirms a suspected veto |
| `evidence_confirmation` | Additional paired draws for promotion confirmation |
| `board_reflection` | Active observation-corpus draws |
| `eval_synthesis_admission` | Draft-entry noise and discrimination probes |

Entry context carries `measurement` as JSON with `purpose`, `draw`, and
`base_seed`. Loss records, worker envelopes, and captured results preserve
those fields. Measurement output does not enter the evaluation contract hash.

### 8.1 Readers validate identity and source eligibility

The physical path and recorded identity must agree on purpose, draw, and seed.
Loss validation also checks epoch, generation, and entry. Missing, malformed,
conflicting, or unstarted records cannot supply a reusable measurement.

Preflight and screen draws evaluate altered source under a generation’s
identity. They are excluded from readers that require measurements of that
generation’s own source. Tournament and confirmation draws are eligible for
cell evidence; the independent confirmation gate further restricts admission
as described in §6.3. A reader must check these roles before combining draws.

### 8.2 Additional purposes require writer and reader checks

Add a purpose to `MeasurementPurpose` and define its source and evidence
eligibility in `core/measurement.py`. Use the board-unit runner and provide
a query label explaining what the draws measure. Tests must prove that
artifacts, cache reads, context stamping, and evidence admission remain
separate across purposes and seeds.

Recovery tests must cover interruption before publication, retained attempts,
and repeated reads of completed measurements. Reusing a completed result
resumes work; admitting the same result twice fabricates evidence.

---

## 9. Contract pre-flight — prove the board can out-signal its own noise

Home: `src/zicato/epoch/preflight.py`. Before an epoch burns rounds, two cheap
measurements answer the one question that decides whether an evolve loop can
work at all: **whether the movement this contract can measure is larger than
its own noise floor**.

- **(a) the A/A floor** — reuses `measure_noise_floor`'s draws (same cache
  slots as `zicato board audit`; idempotent between the two surfaces);
- **(b) the scripted-perturbation duels** — the champion against degraded
  copies of itself: each probed mutation point has its span
  blanked/scrambled (`degraded_content_for`: spans reverse
  character-by-character, code regions become `pass`, `.py` files blank to a
  comment) in an **ephemeral** scratch copy via the real applier. The degraded
  trees never enter the lineage; probe `j`'s draw caches under the
  *champion's* id with `MeasurementDraw(MeasurementPurpose.PREFLIGHT, j)`. The **max**
  over probes of `|degraded_scalar − mean(champion_scalars)|` is the
  contract's demonstrated **degradation signal** — how far the scalar moves
  when a mutation point is destroyed. §9.3 explains why that is not the same as
  achievable improvement, and what the pre-flight is therefore allowed to claim.

Board reflection’s **active observation corpus** uses
`MeasurementDraw(MeasurementPurpose.REFLECTION, j)` through
`reflection/corpus.py::run_corpus`. These draws evaluate the generation’s own
source and remain separate from degraded preflight probes. An infrastructure
abort invalidates the draw with `ReflectionDrawInconclusive`, as it does with
`NoiseFloorInconclusive` in preflight.

### 9.1 Probe selection draws a sample of mutation points (issue #106)

Degrading only `points[0]` would make the measurement a statement about ONE
mutation point rather than about the contract. `enumerate_mutations` sorts by
`(source_root, file, line_start, id)`, which is deterministic but carries **no
information about which points matter**. When the first point is **inert** under
the current contract, a single-point probe measures signal 0 on a healthy board
and condemns it — the same way every round, with no flakiness to expose the
error (issue #106).

A concrete inert point: the presentation-agent target enumerates
`write_webpage_tool_description` alongside its instruction spans. Configure the
deliverable to come from a **structured-output schema** on the producing
sub-agent rather than from that tool call, and the tool's description stops
reaching the artifact at all. Degrading it changes nothing, while degrading
`coordinator_instruction` — exercised on every run — moves the scalar
substantially.

`select_probe_points` (pure, deterministic, unit-tested in
`tests/test_preflight_probe_and_margin_window.py`) fixes selection in three
layers:

1. **Free no-op skip.** A point whose degradation would produce byte-identical
   content (`is_no_op_degradation` — a palindromic span reverses to itself; a
   code region already exactly `pass` blanks to itself) is dropped before the
   sample is drawn. It is provably inert with zero board evaluations spent, and
   it must not consume a sample slot.
2. **Role round-robin.** The remaining points are grouped by their declared
   `role` metadata (falling back to `kind`, so an unannotated harness still
   gets span/code/file spread) and interleaved: one point from every role
   before a second from any. A `limit`-sized sample therefore spans the *kinds*
   of mutable surface the harness declares instead of walking one corner of one
   file. Group order is first appearance in the enumeration and within-group
   order is the enumeration's, so the sample is fully deterministic.
3. **Explicit pin.** `runtime.preflight_probe_mutation_ids` (or
   `zicato board preflight --degrade-mutation-id`) names the points outright,
   in order, ignoring the limit. A pinned id the enumeration does not produce
   raises
   `PreflightConfigError` rather than silently falling back to the automatic
   sample — a silent fallback would report a verdict measured on points the
   operator did not choose, which is worse than no answer.

**Selection runs BEFORE the floor is measured.** Enumeration and selection are
pure filesystem reads, and every way they can fail is a deterministic property
of the snapshot or of the operator's config, so `run_contract_preflight`
validates them first and only then spends K champion draws on the A/A floor. The
reverse order would charge an operator K real evaluations before reporting a
mistyped knob. `RuntimeConfig.__post_init__` catches the
cheapest case earlier still: `preflight_probe_points` must be a positive
integer.

**`preflight_probe_points` is a ceiling rather than a spend.** Probing stops at
the first probe whose signal clears `max(floor_max_abs_delta, promote_margin)`.
Past that bound no further probe can change either verdict, so continuing would
only spend champion evaluations refining a number nothing reads. A healthy
contract therefore costs exactly **one** degraded draw, and the extra evidence
is bought only on a contract that is about to be called unmeasurable. The bound
is the *margin* rather than the floor alone: short-circuiting at the floor would
let the reported signal understate the true maximum and falsely trip §9.3's
`margin_above_achievable`.

Every point considered lands on `PreflightReport.probed_points` (additive) with
its per-point signal or the reason it cost no draw (`no_op_patch` /
`verdict_settled`). Reporting only the winner would hide the diagnosis: an
operator judging whether a `refuse` is about the board or about the sample
needs to see an inert point *next to* a live one.

### 9.2 The verdicts

The pure verdict (`preflight_verdict`) takes the BEST probe's scalar — passing
only the best loses nothing, because if it moved the scalar by zero then every
probe did, so the saturation test below decides identically to one run over the
whole probe set:

| Verdict | Condition | Pathology |
|---|---|---|
| `warn` (saturated) | spread across ALL probes — every A/A draw plus the best degraded draw — is **exactly zero** | zero variance / saturation: even a broken tree scores identically. The signature is the `1.000000` null run — the loop spins forever with nothing to climb. The board rather than the noise is the problem. |
| `inert` | `signal == 0` exactly, while the champion's own draws DID vary | the probe rather than the contract. Two facts hold at once: the harness can move the scalar, and the degradation moved it by nothing. So the signal is **unmeasured** rather than measured as zero. Fix = pick a representative point. NARROW — see the honest reading below. |
| `refuse` (recommended) | `0 < signal <= floor_max_abs_delta` | noise swamps the margin: an A/A re-roll moves the scalar as much as a deliberate degradation does; every duel is decided by noise. The contract cannot possibly resolve the *smaller* improvements a proposer will offer. |
| `ok` | otherwise | signal clears noise |

Saturation is checked **first**: a saturated contract also has
`signal == floor == 0`, and the saturation diagnosis is the
actionable one. `inert` is checked second, before the floor comparison, so that
"the probe moved nothing" is never reported as "the board is noise-limited".

> ⚠️ **The honest reading of `inert`.** The branch is narrower than it looks,
> and it is not what protects a healthy board from the false refusal issue #106
> reported. It needs BOTH champion spread `> 0` AND the degraded scalar exactly
> equal to `mean(champion_scalars)`. Neither realistic harness reaches it:
>
> - **Noisy (continuous) harness** — hitting the arithmetic mean of K noisy
>   draws exactly is measure-zero. A live point the deliverable merely routes
>   around measures a small NON-zero signal, so it lands in **`refuse`** rather
>   than `inert`.
> - **Deterministic harness** — the champion's draws do not vary, so a
>   behaviourally-identical degraded tree gives spread `== 0` and the
>   **saturation** branch claims the case first.
>
> What is left is the **quantized** case: a discrete scoring scale on which the
> champion mean is itself an attainable score (e.g. draws {0.4, 0.6}, degraded
> 0.5). There `inert` fires, and there it is correct and useful. The verdict is
> kept for that case: it is additive, correct when it fires, and removing it
> would churn the persisted schema. It does not address the false refusal issue
> #106 reported.
>
> **What actually protects a healthy board from a false `refuse` is (1) the
> role-diverse multi-point sample of §9.1, which out-measures a routed-around
> point, and (2) the gate-aware SEVERITY of §9.5's health finding, which keeps
> a warn-mode run alive while the operator fixes the sample.** Pinned in
> `tests/test_preflight_severity_and_config_gate.py`.

The verdict persists onto the epoch record (`config.json`'s additive
`preflight` field, never hashed); re-surfaced every round through loop health
(`detect_preflight_verdict`: `preflight_signal_below_floor` — critical only
under `preflight_gate="refuse"`, warning otherwise, §9.5 — / warning
`preflight_saturated_contract` / warning `preflight_inert_probe`).

### 9.3 The promote-margin window (issues #112 and #119)

Whether a contract can out-signal its own noise and whether `promote_margin` is
set sanely are **different questions**. A 24-cell, 72-duel campaign measured the
gap: floor `delta_std` 0.080–0.106, best single-round improvement across all 72
duels **+0.041**, configured `promote_margin` **0.10**, giving **71 of 72 duels
rejected**. Every cell terminated at its starting generation, and the comparison
the campaign existed to make could only return a null. The pre-flight raised
nothing, because the contract *could* out-signal its noise in the sense the
pre-flight tested. The failure sat one level above that test.

`preflight_window_verdict` places the margin against the floor and the measured
signal and names the side it fell outside of, because the two sides have
opposite fixes:

| `window_failure` | Condition | Verdict | What the operator must do |
|---|---|---|---|
| `empty_window` | `signal <= noise` | `warn` | **Nothing to the margin.** No value of it is defensible on a board whose measurable movement is inside its own noise. Fix the board / reduce noise. |
| `margin_above_achievable` | `margin >= signal` | `warn` | Check the margin against what a real fix is worth. See the reading below — this is NOT proof nothing can promote. |
| `margin_below_floor` | `margin <= floor` | `warn` | Raise the margin above the noise, and/or keep the evidence gate on. |
| — (`None`) | both bounds hold | `ok` | — |

`empty_window` is checked first because it invalidates the other two
diagnoses — an operator told "your margin is mis-set" will spend a cycle tuning
a number that has no valid value. Every outcome is `warn`: the refusal that
matters is §9.2's floor comparison, which the gate already acts on, and the
upper comparison here measures something that does not bound a challenger's
reach (below).

Bounds are inclusive on the failing side (`>=` / `<=`): a margin exactly AT the
measured signal exceeds everything the probe saw, and one exactly at the floor
is indistinguishable from noise.

> ⛔ **The signal measures DEGRADATION headroom and never achievable
> IMPROVEMENT.** `signal = |degraded_scalar − champion_mean|` is how far the
> scalar moves when a mutation point is **destroyed**, which is how much this
> champion has left to **lose**. A promotion needs movement the other way. The
> two quantities are unrelated in general, and they diverge hardest where an
> evolve loop is most often started: a champion seeded near the failing end has
> little left to break (small degradation headroom) and much to gain (large
> improvement headroom). Enforcing the margin against degradation headroom
> therefore fails in both directions — a **false refuse** for a floor-anchored
> champion whose margin the board could clear, and a **silent false OK** for a
> champion at the score ceiling, whose large degradation headroom says nothing
> about an improvement that is unavailable (issue #119).
>
> The correction is a relabel rather than a new number. The measurement persists
> under `degradation_signal`, and the `signal` key is retained beside it so
> existing readers keep working. `margin_above_achievable` is a **warning that
> cannot hard-refuse a run**, even under `preflight_gate="refuse"`, and every
> operator-facing string says what was measured. `effective_gate_verdict` also
> declines to escalate a *persisted* `margin_above_achievable` refusal, so an
> epoch pre-flighted while that finding still refused does not keep stopping on
> it.
>
> **One tempting fix is unsafe.** Defining improvement headroom as
> `champion_mean − 0` assumes the scalar's reachable floor is zero, which it is
> not: a namespace with a **negative** weight (a rubric, where higher is better)
> pushes the scalar below zero, so that subtraction would fabricate a bound.
> Deriving a real bound from the namespace weights is **registered as future
> work and not built**; improvement headroom is **unmeasured**, and the code
> says so.

> ⚠️ **The signal is also a SINGLE-POINT lower bound.** Separately from the
> labelling problem above, the probe degrades one mutation point per draw, so it
> under-reports even the movement it *does* measure. A patch that touches
> several points exceeds it, and **recombination does so by design**: `recombine`
> exists to union two individually sub-margin fixes into a promotable one (see
> the known-answer tests in `tests/test_recombination_known_answer.py`). That is
> a second, independent reason the finding is a **warning** rather than a
> critical: a critical would trip `evolve_n_rounds`'s degenerate-health circuit
> breaker (`_DEGENERATE_HEALTH_STOP_THRESHOLD`) and kill the legitimate run
> recombination was built for.

### 9.3.1 The holdout's own bound (issue #118)

The window above places the **train** margin. When the split is active, a
promotion must also survive the holdout confirmation, which applies its own
scalar tolerance and its own pass-rate rule to a **smaller** slice. A slice of N
entries moves its scalar in `1/N` steps, so the holdout's steps are the coarse
ones. Its bound can therefore be the binding one while the train window looks
healthy.

Without a separate knob, `promote_margin` serves as that tolerance as well as
the Ladder's release threshold — one knob with three duties. On the
DEFAULT-produced 12-train / 6-holdout split with one holdout entry flipping,
**no margin value promotes** under that arrangement: the scalar-margin rung
needs `margin <= 2/12` while tolerating the holdout needs `margin >= 1/6`, which
are the same number, and float rounding closes even that single point. Past the
scalar bound, the holdout's pass-rate rule — carrying only its float-noise
tolerance and no operator knob at all — rejects at every margin anyway.

Two additive, default-inert contract fields split the bounds off
(`ScoringWeights`, both omitted from the canonical form at their default so no
existing epoch's hash moves):

| Field | Default | Effect |
|---|---|---|
| `holdout_margin` | `None` | The holdout confirmation's scalar tolerance (`gate.effective_holdout_margin`). `None` ⇒ fall back to `promote_margin`. Scoped to the confirmation only — it does not move the Ladder's release threshold, which gates a *train*-measured improvement. |
| `holdout_entry_regression_budget` | `0` | How many holdout entries may regress before the confirmation rejects. `0` ⇒ today's zero-tolerance rule. Applies under both monotonicity scopes — per-entry as a count, aggregate as a widened `budget / entries` band, so one budget unit means one entry either way. |

For commensurable bounds set `holdout_margin ≈ promote_margin × N_train /
N_holdout`, roughly double on the default split. The budget follows the gate's
own doctrine: the holdout **confirms** rather than re-decides, so a
train-measured win must merely avoid regressing. A confirmation that no
achievable margin can satisfy acts as a second gate instead. The TRAIN side
keeps its zero-tolerance rule, so neither field can loosen the primary
decision.

`preflight.holdout_window_note` renders the feasibility note — prose on the
pre-flight record (`holdout_note`) and printed by `zicato board preflight`.
The note is advisory and cannot refuse execution. It names
both facts an operator cannot otherwise see without doing the arithmetic: that
one entry flipping moves the holdout scalar by about `pass_weight / N`, and
that at budget `0` a single flip rejects at **every** margin, which raising the
holdout margin cannot fix.

### 9.4 The floor statistic a recommendation may scale

The 24-cell campaign of §9.3 also walked into a second trap (issue #112). The
measured floor is surfaced as `max_abs_delta`, a **range** statistic whose
expectation grows without bound in K. Recommending a margin above *that* means
the recommendation **drifts upward on an unchanged board as calibration
improves**, pushing the margin toward — and in the campaign's case past — the
achievable signal. A recommendation that degrades as the measurement gets
better is backwards.

`recommended_promote_margin` (in `tournament/calibration.py`) scales
`delta_std` instead: the standard deviation of the A/A `delta_scalar`, i.e. of
the difference the promote gate thresholds, already computed and
persisted alongside the range by `delta_spread`. It is a consistent
estimator — more draws sharpen it rather than inflate it. The multiple is
`MARGIN_NOISE_MULTIPLE = 2.5` (≈1.2% two-sided chance an A/A pair clears the
margin). `recommended_promote_margin_from_floor` is the tolerant persisted-dict
entry point; it falls back to the range only when a record carries no usable
`delta_std`, which never happens on measured data (a positive range implies a
positive std). The recommendation rides along on the pre-flight record as the
additive `recommended_margin`.

> Use `max_abs_delta` for the *comparison* that asks whether a margin sits
> inside the noise (`margin_below_floor`), and `delta_std` for the
> *recommendation*. Conflating the two is the defect.

### 9.5 Gating at evolve start (issue #84)

The pre-flight is **default-on**: at evolve start the loop measures it once per
epoch, idempotently and best-effort, unless the runtime opts out. It then acts
on `effective_gate_verdict`, which collapses the two verdicts of §9.2 and §9.3
into the one answer the gate needs — `refuse` when either refuses, else the
signal verdict verbatim. The runtime-only `RuntimeConfig.preflight_gate` knob
chooses what that answer does, and like `infra_abort_round_threshold` it never
rolls the epoch:

| `preflight_gate` | On a refuse-worthy / saturated / inert verdict, or any window failure |
|---|---|
| `"warn"` (**default**) | LOUD `log.warning` at evolve start + the per-round health finding at **warning** severity; the run **proceeds** (recommend-only philosophy) |
| `"refuse"` | additionally raises `PreflightRefusedError` when the SIGNAL verdict refuses (signal at/below the floor); `evolve_n_rounds` catches it and stops with reason `preflight_refused` **before spending rounds**, no traceback. The health finding is **critical** here (and moot: no round runs) |
| `"off"` | skip the measurement entirely, so no pre-flight runs at all (the escape hatch deterministic oracles use so the orthogonal probe never runs the champion) |

Only the **floor-based** refusal reaches the hard gate. §9.3's window verdicts
are all warnings, because they compare the margin against numbers that do not
bound a challenger's reach (issue #119). An `inert` verdict is **never** a
refusal under any gate mode: the probe came up short rather than the contract,
and hard-stopping a possibly-healthy board there is the failure issue #106
reported. `effective_gate_verdict` reads the persisted record rather than the
live `PreflightReport`, so a resumed or later round reaches the identical
decision as the round that measured. That is also why it skips a *persisted*
`margin_above_achievable` refusal instead of re-refusing every round on a
finding that does not refuse.

> ⛔ **The health finding's severity MUST follow the gate mode.** It is what
> makes the two gate modes differ at all. `detect_preflight_verdict` re-emits
> from the **persisted** record,
> so a refuse verdict re-fires identically every round for as long as the epoch
> carries it. A `critical` there is therefore never one finding — it is an
> unbroken critical streak, and `diagnostics.py`'s `healthy` flag counts
> warnings but `orchestrator.py`'s `has_critical` counts only criticals, which
> is what `evolve_n_rounds` feeds to `DegenerateHealthPolicy`. Two
> rounds and the loop stops with reason `degenerate_health`. Under the DEFAULT
> `"warn"` that would contradict the knob: an operator who asked to be warned
> would get a hard stop two rounds later. So
> `preflight_signal_below_floor` is `critical` only under
> `preflight_gate="refuse"` — where the run already stopped at the pre-flight,
> so the breaker cannot fire anyway — and `warning` under `"warn"` / `"off"`,
> where it stays fully visible in `zicato health`, the round report and the
> dashboard (any warning makes `LoopHealth.healthy` false) while being
> structurally unable to stop the run. The gate mode reaches the detector via
> `zicato.health.inputs.workspace_preflight_gate`, whose only source is the
> `runtime` block, and which both the orchestrator's per-round assessment and
> the standalone `zicato health` CLI share. The mode also rides along on the
> finding's `detail["preflight_gate"]`, so a persisted report says which choice
> graded it.

**A config typo must not silently disable a `refuse` gate.** The evolve-start
hook runs under `best_effort` because *an outage never disqualifies a
contract* — a transient endpoint failure must skip the pre-flight and
re-measure next round, never condemn the board. But that reasoning is about
NONDETERMINISTIC infra. A misspelled `runtime.preflight_probe_mutation_ids`
entry, or a nonpositive probe count, is deterministic
operator error: it will fail identically every round, and swallowing it would
leave a `preflight_gate="refuse"` run proceeding with **no gate at all** because
of a typo. Runtime configuration validation rejects invalid probe counts. Probe selection
raises `PreflightConfigError` for unknown mutation ids;
`_maybe_contract_preflight` escalates that error to `PreflightRefusedError`
under `"refuse"` and reports a warning under `"warn"`.

The evolve-start warning is **per-verdict prose** (`_preflight_diagnosis` in
`orchestrator.py`): "noise swamps the signal", "the probe was inert", "the
margin exceeds what we measured" and "the margin is inside the noise" have four
different fixes, and issues #106 and #112 both record operator time wasted when
those cases were reported in the same words.

Surfaces: `zicato board preflight` (manual, always recommend-only; carries
`--degrade-mutation-id` and `--probe-points`, prints every probe and the window
verdict) + the epoch-open hook `"contract_preflight": K` still sets the number
of A/A draws K (absent ⇒ `DEFAULT_CALIBRATION_RUNS`). Like the calibration it
is serial and front-loaded: K A/A draws plus the degraded probes over every
board entry, before the round's first duel. It therefore owns the heartbeat for
its duration (`PREFLIGHT_PHASE`, owned by `epoch/preflight.py`) and logs its
whole expected cost first — K A/A draws plus up to `preflight_probe_points`
degraded probes, times every board entry. `run_contract_preflight`'s `on_probe`
callback reports `{done}/{total}` as each A/A draw and each probe settles, one
count over both stages because each stage is one pass over the board, and
`total` is the ceiling the probe loop may stop short of. The phase restores to
`evolve_once:round_{N}` in a `finally` on every path, refusal included; the
served projection renders it beside the calibration
(`loop_view._EPOCH_OPEN_STEPS`). Because the measurement
runs the champion for its A/A floor, a fast-mode test asserting "the champion
is never re-run" must set `runtime.preflight_gate: "off"`.

**The knobs are RUNTIME knobs.** `preflight_probe_points` (ceiling, default
`PREFLIGHT_PROBE_POINTS_DEFAULT = 5` — one per declared role on a realistic
multi-agent harness) and `preflight_probe_mutation_ids` live on `RuntimeConfig`
under `runtime.*` rather than on `ScoringWeights`: which points a
diagnostic probe degrades is not part of the frozen evaluation contract, so
tuning it must not roll the epoch or invalidate every existing epoch's
comparability. `propose_parallelism` is the precedent, and the property is
asserted directly (`test_probe_knobs_do_not_move_the_contract_hash`) rather
than trusted.

Note the connection to 14-goals-and-roadmap.md: the presentation-agent target's
structural mock-null (`mocks.py` discards the system prompt, so no instruction
patch can move any measurement) is the saturation pathology. The pre-flight
exists so that class of dead contract is caught before the epoch spends rounds
on it.

---

## 10. Judge test–retest

A process judge folds into the loss as a `custom:<judge_name>` drift count,
weighted by `per_judge_weights` (§1.1). A judge that disagrees with **itself**
— different verdicts on byte-identical input — injects pure noise into every
scalar it touches. `src/zicato/judge_runtime/reliability.py` measures it with
the psychometric test–retest protocol: build the live goldfive judge from the
board's declarative `JudgeSpec` through the SAME builder every real run uses,
then judge one frozen transcript `k` times (default `DEFAULT_RETEST_K = 3`).

The compared quantity is the `drift_emitted` flag — the bit that becomes (or
does not become) a `drift:custom:<judge_name>` `MetricCount` on a real run, i.e.
the noise the judge injects into the scalar. The disagreement measure
is pairwise and pure:

```python
# src/zicato/judge_runtime/reliability.py — pairwise_disagreement
def pairwise_disagreement(fired: int, k: int) -> float:
    if k < 2:
        return 0.0
    pairs = k * (k - 1) / 2
    return (fired * (k - fired)) / pairs
```

A deterministic judge scores `0.0`; a coin-flip judge tends to ~`0.5`; a
strict alternator at k=2 scores `1.0`. Above
`NOISY_JUDGE_DISAGREEMENT_THRESHOLD = 0.25` the `noisy_judge` health finding
fires (warning, recommend-only), and its recommendation points at
`per_judge_weights` — the contract's routing knob for this signal:
down-weight the noisy judge rather than letting it thrash the scalar.

Surface: `zicato board judges --test-retest [--retest-k K]
--auxiliary-call-llm <dotted-path>` over a settled transcript from a prior run
or the synthetic `FIXTURE_TRANSCRIPT`. The `aux_call_llm` parameter is the
endpoint seam — tests script it; a real evaluation endpoint slots in unchanged
(that live measurement is endpoint-gated; see 14-goals-and-roadmap.md
§"The endpoint-gated backlog").

---

## 11. The placebo arm

`src/zicato/evolve/placebo.py` — the control arm of A/B methodology, opt-in
via `experimental.random_baseline_every_n` (default 0 = off; omitted from the
contract canonical form at the default). Every Nth epoch-cumulative round the
orchestrator fields ONE extra challenger whose patch is a
**semantics-preserving no-op**: the first enumerated mutation point's current
value re-emitted unchanged (with the applier-aware span handling in
`placebo_noop_content` so a `.py` span re-emits its resolved *value* rather than
an assignment echo). The placebo is a genuine lineage child derived through the
real `GenerationStore.derive_generation` seam — never a synthetic score
injection — and its hypothesis `core_idea` opens with
`PLACEBO_HYPOTHESIS_MARKER` so every consumer can recognize the arm.

The arm measures **the gate itself**:

- **rejected** — the expected outcome, every time: identical behavior leaves
  no improvement to clear `promote_margin`. Each cadence tick re-confirms that
  the gate still separates no change from improvement.
- **promoted** — the alarm case. A no-op that wins a tournament means the decision
  procedure is promoting noise — margin under the floor, a broken reducer, a
  rigged gate. `detect_placebo_promoted` raises the CRITICAL
  `placebo_promoted` health finding, and the correct operator reading is:
  **recent real "wins" are suspect too.** A promoted placebo is never a
  placebo problem; it is a decision-procedure problem that the placebo
  happened to expose.

Placebo experiments are filtered out of the optimization-stream health
detectors, because an always-rejected control must not read as a stall. On the
gauntlet path the placebo runs as an extra scheduled duel that never advances
the champion pointer. On a multi-challenger field it is one extra slate slot,
passed through the unchanged strategy and gate.

---

## 12. The overfitting program map

The threat model: the board is reused adaptively across rounds, so the
optimizer can optimize the measure instead of the quality it stands for
(Goodhart's law), memorizing entries rather than improving true quality.
`docs/design/OVERFITTING.md` is the survey; this table is the program's shipped
state and where each lever lives.

| # | Lever | Status / default | Home |
|---|---|---|---|
| 1 | Train/holdout split + holdout-gated promotion | SHIPPED, default-on; auto-degrades to empty holdout below `min_board_size_for_split` (6) or with `enabled=False`; `holdout_fraction` 0.3; explicit `holdout` tags always win | `src/zicato/board/split.py`, `tournament/gate.py` |
| 2 | Ladder-mediated, budgeted holdout feedback | SHIPPED, default-on (no-op when holdout empty); one query is one complete crowning holdout comparison | `src/zicato/tournament/ladder.py` (§5) |
| 3 | Restricted proposer visibility | SHIPPED, default-on (`restrict_proposer_visibility`) — patterns train-slice-only, per-entry identities aggregated to counts/rates, exact failing inputs withheld; plus the sanitized outcome-marginal channel | `patterns/`, `proposer/prompts.py`, `analyzer/outcome_marginals.py` |
| 3b | **Banding** (part of #3) | Δscalar in experiment memory coarsened to `improved` / `flat` / `regressed` buckets via `_bucket_scalar_delta` — never the exact number | `src/zicato/proposer/prompts.py` |
| 4 | Diff-complexity regularization (parsimony, or minimum description length) | SHIPPED in FULL — both the opt-in loss term (`diff_complexity_weight`, default 0.0, exactly absent when off) AND the complexity-*ceiling* half (`diff_complexity_ceiling`, default 0.0 = off; a structural admissibility veto in `tournament/gate.py::evaluate_gate`, checked before the scalar-margin rung, for a challenger whose diff complexity exceeds the budget) | `scoring/builtins.py::diff_complexity_component`, `scoring/diff_complexity.py`, `tournament/gate.py::evaluate_gate` |
| 5 | Generalization-gap detector | SHIPPED — fires warning/critical when `holdout_loss − train_loss` **widened** since the first measured generation AND exceeds the threshold; a flat or narrowing gap is healthy regardless of magnitude | `health/diagnostics.py::detect_generalization_gap` |
| 6 | Rotation / refresh cadence | SHIPPED — `rotate_holdout` (default `True`) folds the epoch id into the split hash so a different slice is held out each epoch (stable within an epoch; explicit tags never rotate); `max_generations_per_contract` surfaces a refresh *recommendation*, never an auto-roll | `board/split.py` (`rotation_seed`), `detect_refresh_cadence` |
| 7 | Random-baseline placebo | SHIPPED, opt-in (`random_baseline_every_n`, default 0) | `evolve/placebo.py` (§11) |

Two boundary rules for anyone extending near this table:

> ⛔ NEVER widen what the proposer can see of per-entry evaluation results —
> entry ids, exact inputs, exact Δscalars, holdout anything — without a
> design-first PR that states the redaction rules and the empirical
> harm-detection protocol (the gap detector + placebo arm are the
> instruments). The screen's counts-only result strings and the
> process-exemplars channel both followed this discipline.

> ✅ ALWAYS check which side of the train/holdout split your new surface reads
> from. The holdout is never eligible for: proposer context, pattern
> detection, screen panels, exemplars, loss summaries. Grep for
> `split_board` call sites to see how existing surfaces select the train
> slice.

---

## 13. How to prove a statistical change: the power-harness methodology

Every mechanism above ships with measured operating characteristics. When you
change one — or add one — you extend the same instrument:
`tests/test_decision_procedure_power.py`, the decision-procedure power harness.
Its design is the methodology, stated as five rules in §13.1 to §13.5.

### 13.1 Seeded noise from stable identifiers only

The noise model is the target_0 example harness's own
(`examples/zicato_examples/target_0_convergence/harness.py`):
`stable_noise_seed` derives the random-number-generator (RNG) seed **only**
from
`(workspace_seed, generation_id, entry_id, measurement.purpose, measurement.draw)`.
No wall clock,
no global RNG, no process ids, no tempdir names. Consequences:

- trials are exactly reproducible (the asserted "rates" are deterministic
  functions of the chosen seeds — *calibrated documentation* rather than flaky
  statistics);
- trials vary by advancing the workspace seed; replicates vary by the stamped
  measurement purpose and draw; sides vary because the generation id is in the seed (the
  A/A premise: identical trees under two ids draw independent noise);
- `test_noisy_session_seed_derives_only_from_stable_identifiers` pins each
  component independently: same coordinates ⇒ byte-identical run; any single
  component change ⇒ a fresh draw. A seeding regression in any component
  fails loudly.

### 13.2 Drive the real machinery; fake only the worker boundary

The statistical trials drive the real `run_matchup` (board-unit scheduling,
replicate averaging, and promotion gate) and the real
`evaluate_tournament`/`confirm_promotion_with_evidence` strategy and evidence
loop. They replace one seam, `runner._run_single`, with `_NoisyWorld`. The
replacement is an in-process evaluator that uses the same noise model, output
synthesis, and board predicates:

```python
# tests/test_decision_procedure_power.py — _NoisyWorld.install
    def install(self, monkeypatch: pytest.MonkeyPatch, *, persist: bool = False) -> None:
        monkeypatch.setattr(runner_mod, "_run_single", self._fake_run_single)
        monkeypatch.setattr(scheduling_mod, "_runtime_state", lambda: None)
        if not persist:
            monkeypatch.setattr(scheduling_mod, "_persist_unit_loss", lambda **_kw: None)
```

`persist=True` keeps the real per-unit cache persistence for the
slot-integrity tests that inspect measurement loss files on disk. One test at the
bottom (`test_noisy_adapter_seeded_draws_cross_the_worker_boundary`) drives
the actual `NoisyPolicyAdapter` through **real subprocess workers, twice**, to
prove the seeded draw crosses the process boundary intact (reproducible,
side-independent, replicate-independent) — so the in-process shortcut is
licensed by an end-to-end anchor.

### 13.3 A/A nulls first

Before any power claim, measure the null. The harness plants σ=0.22 and
derives the analytic floor (~0.663). The null-calibration test runs 60 seeded A/A
single-sample duels and checks the floor, margin premise, and planted-effect scales
from that report. Parallel workers therefore compute the calibration once.
The measured sd must land in `[0.4, 1.0]`. A floor
of ~0 would mean the draws stopped varying, which is a seeding regression, and a
floor outside that band would mean the noise model broke. Then the null is run through the
*decision procedures*: the naive contract's noise-promotion rate (fact #1) and
the effective contract's zero false promotions (fact #4).

### 13.4 Planted deltas in floor units

Effects are planted by construction — token sets whose measured scalar deltas
are arithmetic consequences of σ — and stated in multiples of the measured
floor:

```python
# tests/test_decision_procedure_power.py — the planted effects
DELTA_CASES: dict[str, tuple[tuple[str, ...], float]] = {
    # ~0.5x floor: half-fix one defect (it now manifests only half the time).
    "small": (("verbose-prose", "omit-summary", "sometimes-50-skip-citations"), 0.336),
    # ~1x floor: fully fix one defect.
    "medium": (("verbose-prose", "omit-summary"), 0.672),
    # ~3x floor: fix all three defects.
    "large": ((), 2.016),
}
```

The tests first assert the planted effects really sit near their advertised
multiples of the measured floor (the instrument is self-checking), then pin
the power curve: `rates["large"] == 1.0` and monotonicity
`small <= medium <= large`.

### 13.5 Operating characteristics as pinned tests

The end state of any statistical change is a set of assertions that (a)
document the measured rates in printed output, (b) pin acceptance bounds loose
enough to survive re-seeding but tight enough to catch a regression, and (c)
include the **failing alternative** as documentation — e.g. the screen tests
compute the naive any-flip rate *on the identical seeded draws the engine
consumed*, so the comparison is between rules rather than between samples.

### 13.6 Recipe: proving a change to the decision procedure

1. **State the claim quantitatively.** "The new X reduces false promotions
   under the A/A null from A to B at σ=0.22 without reducing power at the 1×
   planted delta by more than C." If you cannot phrase it this way, you are
   not ready to implement.
2. **Write the null test first.** Install `_NoisyWorld` with an A/A world
   (`{"champion": BASE_TOKENS, "challenger": BASE_TOKENS}`), run your
   procedure over the seeded trial range, count decisions.
3. **Write the planted-delta tests** at 0.5×/1×/3× the floor using
   `DELTA_CASES` (or extend the token vocabulary if your effect shape is
   new — `sometimes-<pct>-<token>` gives continuously tunable true effects).
4. **Include the failing alternative** as a measured, printed, pinned
   comparison — the naive rule you are replacing must be shown failing on the
   same draws.
5. **If your change touches persistence or measurement identity**, add a
   slot-integrity test with `persist=True`. Verify that confirmation leaves
   tournament artifacts unchanged and writes its own purpose, draw, and seed
   for both competitors. The existing pattern is
   `test_full_mode_evidence_loop_never_touches_canonical_slots`.
6. **Re-run the whole power file and the convergence oracle** — your change
   must leave every existing pinned number standing, or the commit message
   must say exactly which number moved and why that is honest (commit eb55266,
   which updated the "budget 48 → confirmed" expectations, is the example to
   follow).
7. **Verify**:

```bash
uv run pytest tests/test_decision_procedure_power.py tests/test_convergence_known_answer.py -q
```

> ⚠️ TRAP: do not "stabilize" a flaky statistical test by widening its bounds
> until it passes. These tests are deterministic given their seeds — if a rate
> moved, the *procedure's behavior* moved, and the correct responses are
> (a) your change is wrong, or (b) the new rate is the honest new
> characteristic and the commit documents it. A silently widened bound is a
> deleted measurement.

> ✅ ALWAYS print the measured rates (`print(f"[power/...] ...")`) alongside
> the assertions. The printed line is the calibration record a future agent
> reads to know what "normal" looks like; the assertion alone tells them only
> that some bound held.

### 13.7 Recipe: adding a scored namespace to the contract

1. Choose a namespace prefix with the trailing colon (`"mycost:"`) and emit
   `MetricCount(name="mycost:<metric>", count=...)` rows from the reducer (or
   an adapter-side emission the reducer folds through
   `LossProfile.scoring_metrics()`).
2. Add the coefficient to the operator contract's
   `namespace_weights` — sign encodes direction (§1.5): positive =
   higher-is-worse. Zero means "tracked, never scored" — a legitimate first
   deployment state while you watch the metric's distribution.
3. Decide monotonicity: only flag the namespace in `namespace_monotonicity`
   once you know its per-round noise — a knife-edge monotonic gate on a noisy
   namespace vetoes real improvements (the same failure mode per-entry
   pass-rate scope has on sampled boards).
4. Remember this is a **contract change**: the epoch rolls. Say so in the
   change description.
5. **Verify** — the namespace appears, weighted, in both surfaces and sums
   into the scalar:

```bash
uv run pytest tests/test_scoring_seams.py tests/test_tournament_scoring.py -q
```

### 13.8 Recipe: retuning `promote_margin` on a live contract

1. Measure first: `zicato board audit` (persists the A/A floor onto the epoch
   record). Do NOT pick a margin from intuition.
2. Read the floor: `max_abs_delta` from the epoch record's `noise_floor`
   field. A margin below it is inside the noise (§4); a margin several
   multiples above it costs power against small true effects.
3. If the evidence gate is on, the margin's role is softer (the
   defer→replicate loop absorbs noise; the health finding downgrades to
   info) — bias toward the floor. If the gate is off, the margin is the ONLY
   noise defense — set it at or above the floor and expect fewer, larger
   promotions.
4. Changing `promote_margin` rolls the epoch (it is a `ScoringWeights` field).
   Note that the Ladder's default release threshold seeds from it
   (`effective_threshold`), so you are also retuning the holdout release bar
   unless `ladder.threshold` pins one explicitly.
5. **Verify** — start one round and confirm no `margin_below_noise_floor`
   warning in the health report:

```bash
uv run zicato board audit --workspace <ws>   # then inspect the epoch record + health output
```

### 13.9 Recipe: enabling the evidence gate on an operator contract

1. Set both params together in the tournament structure block —
   `promote_confidence_threshold` (the scaffolds write `0.8`) AND
   `promote_confidence_replicates` (the scaffolds write 32). The planned field
   and budget determine the interval's comparison allocation (§6.5). Setting
   the threshold without a budget uses `DEFAULT_REPLICATE_BUDGET = 3`, which
   can leave a true improvement inconclusive.
2. Price it before running: each evidence replicate is a fresh
   2-sides × board sweep. The contract estimator reports that cost
   (10-cli-and-configuration.md §10.3).
3. Expect and monitor the dead-letter queue
   (`runtime/inconclusive/*.json`) — an `inconclusive` terminal is a designed
   outcome rather than an error; a *stream* of them means the budget cannot
   resolve
   the effect sizes your proposer produces (raise `replicates`, or accept the
   holds).
4. Both params live in `TournamentStructure.params`, so enabling rolls the
   epoch; absent params add nothing to the canonical form (no retroactive
   roll for anyone else).
5. **Verify** — the pre-gate engages and journals a rating block:

```bash
uv run pytest tests/test_gauntlet_evidence_gate_e2e.py tests/test_driver_evidence_pregate.py -q
```

---

## 14. Quick reference — the constants

| Constant | Value | Home |
|---|---|---|
| `promote_margin` default | `0.01` | `core/scoring_config.py::ScoringWeights` |
| `PER_ENTRY_SCORE_MONOTONICITY_TOLERANCE` | `0.02` | `tournament/gate.py` |
| `PASS_RATE_MONOTONICITY_TOLERANCE` | `1e-9` | `tournament/gate.py` |
| `NAMESPACE_MONOTONICITY_TOLERANCE` | `0.0` | `tournament/gate.py` |
| `_TASK_FAILURE_RATIO_MULTIPLIER` | `10.0` (a pinned constant rather than a knob) | `scoring/builtins.py` |
| `DEFAULT_CALIBRATION_RUNS` | `5` | `tournament/calibration.py` |

| `MIN_CREDIBLE_DUELS` | `3` | `selection/evidence_gate.py` |
| `CI_Z` | `1.959963984540054` | `selection/evidence_gate.py` |
| `DEFAULT_REPLICATE_BUDGET` | `3` | `selection/evidence_gate.py` |
| `DEFAULT_PROMOTE_CONFIDENCE_THRESHOLD` (scaffold-written) | `0.8` | `selection/evidence_gate.py` |
| strategy `_default_replicates` | `2` (racing pins `1`) | `selection/strategy.py` + strategies |
| `min_board_size_for_split` | `6` | `core/scoring_config.py::OverfittingConfig` |
| `holdout_fraction` | `0.3` | `core/scoring_config.py::OverfittingConfig` |
| `DEFAULT_RETEST_K` | `3` | `judge_runtime/reliability.py` |
| `NOISY_JUDGE_DISAGREEMENT_THRESHOLD` | `0.25` | `judge_runtime/reliability.py` |

Cross-references: the tournament structures and strategy protocol are
06-tournament-and-selection.md; the unit cache's durability story is
07-runtime-and-durability.md; the test-suite discipline that keeps these
measurements honest is 11-testing.md; every bug named above is a full case in
12-bug-casebook.md; the live-validation items that finish this program are
14-goals-and-roadmap.md.
