# 04 — Evaluation Statistics: The Doctrine

> **Covers:** the full measurement chain (goldfive events → reducer → `LossProfile` →
> `aggregate_generation_score` → scalar), the promote gate's rule ladder, the noise
> doctrine and its measured facts, A/A noise-floor calibration, the Ladder-mediated
> holdout, the Bradley–Terry evidence gate, replication semantics, the reserved
> replicate-base ledger, contract pre-flight, judge test–retest, the placebo arm,
> the overfitting program map, and the power-harness methodology for proving any
> statistical change.
>
> **Prerequisites:** 01-orientation.md (what a generation / epoch / board is),
> 03-contract-and-epochs.md §"The contract hash" (what rolls an epoch),
> 06-tournament-and-selection.md §"Structures" (who calls the gate).
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
> 6. **A board unit `(generation_id, entry_id, replicate_index)` is evaluated at
>    most once per contract** and its persisted result is immutable.
> 7. **Replicate indices are a partitioned namespace** (the reserved-base
>    ledger, §8). Never run an out-of-tournament evaluation at an index you
>    have not formally claimed.
> 8. **The holdout is confirmation-only and Ladder-mediated.** It can flip a
>    train-win to reject; it never promotes, never steers the proposer, and its
>    raw per-entry results never leave the gate.
> 9. **The evidence gate can only hold a promotion, never force one.** The
>    protected-incumbent invariant strictly strengthens through it.
> 10. **Soundness devices and power devices are different things.** The
>     evidence gate buys soundness; replication buys power. Do not "fix" low
>     power by weakening a soundness device.

This chapter is the one that keeps you from making an unsound change. zicato's
whole reason to exist is that its promotion decisions are *trustworthy*: when
the loop says "this child is better than its parent," an operator must be able
to believe it. Every mechanism below exists because a naive version was tried
(or measured) and demonstrated to promote noise, memorize the board, or corrupt
its own evidence. If you change anything in this chapter's territory without
reproducing the corresponding measurement, you are guessing — and the history in
12-bug-casebook.md shows what guessing costs.

---

## 1. The measurement chain

One evaluation of one generation against one board entry flows through five
stages. Every stage has exactly one home:

| Stage | What happens | Home | Runs where |
|---|---|---|---|
| 1. Run | The agent under test executes the entry; goldfive emits `events.jsonl` (one file per run) | adapter + goldfive `JSONLPersistenceSink` | killable worker subprocess |
| 2. Reduce | Events → one `LossProfile` (drift counts, pass/fail, per-judge loss, `drift_loss`) | `src/zicato/telemetry/reducer.py` (`reduce_loss`) | worker subprocess |
| 3. Persist | `LossProfile` → the unit's replicate-keyed `loss.json` cache slot | `src/zicato/tournament/unit_cache.py` | worker writes; orchestrator reads |
| 4. Aggregate | Per-entry losses → one per-generation summary dict (`scalar`, `pass_rate`, `mean_score`, `per_entry`, `namespace_aggregates`, `scalar_components`) | `src/zicato/tournament/scoring.py` (`aggregate_generation_score`) | orchestrator |
| 5. Decide | Two aggregates → `GateOutcome` | `src/zicato/tournament/gate.py` (`evaluate_gate`) | orchestrator |

The reducer is the **only** zicato component that walks raw goldfive events.
Everything downstream — pattern detectors, tournament scoring, journal
rendering, the dashboard — reads `LossProfile`. That single narrow seam is
deliberate: goldfive's event schema evolves upstream, and zicato wants exactly
one place that knows the wire form.

> ✅ ALWAYS route any new signal you want to score through the reducer into a
> `LossProfile` field (or a namespaced `MetricCount`). Never have a scorer or a
> gate read `events.jsonl` directly — you would create a second event-schema
> dependency that silently breaks when goldfive evolves.

### 1.1 Seam 1 — the per-run drift-loss formula

The per-run reduction of drift counts + plan revisions + task failures +
runtime into a single `drift_loss` scalar is **Seam 1**. Its formula lives in
`src/zicato/scoring/builtins.py::builtin_drift_loss` and is byte-identical to
what `zicato.telemetry.reducer.compute_drift_loss` historically inlined:

```python
# src/zicato/scoring/builtins.py — builtin_drift_loss (core)
    sev_w = weights.severity_weights
    loss = 0.0
    for c in drift_counts:
        sev_mult = sev_w.get(c.severity, 0.0)
        kind_mult = _kind_multiplier(c.kind, weights)
        loss += sev_mult * kind_mult * c.count
    loss += weights.plan_revision_weight * plan_revisions
    loss += _TASK_FAILURE_RATIO_MULTIPLIER * task_failure_ratio
    loss += weights.runtime_weight * (runtime_ms / 1000.0)
    return max(0.0, float(loss))
```

Facts a change here must respect:

- `_TASK_FAILURE_RATIO_MULTIPLIER` is a **pinned constant 10.0**, not a knob.
  The contract froze it ("pure failures matter"). Operators who want to dampen
  failures up-weight drift via `severity_weights` / `per_kind_weights` /
  `per_judge_weights` instead.
- `_kind_multiplier` splits **first-class drift kinds** (weighted by
  `per_kind_weights.get(kind, 1.0)`) from **custom-judge kinds** (`custom` /
  `custom:<judge_name>`, weighted by
  `per_judge_weights.get(judge_name, weights.default_judge_weight)`). The
  reducer attributes each custom drift to its authoring judge by folding the
  paired `JudgementEmitted.judge_name` into the kind string — this is how two
  distinct process judges weigh independently.
- The reducer applies two pieces of **reducer policy** *around* this formula,
  not inside it: the not-completed heavy penalty and the
  `task_failure_ratio` floor for a run that never completed. Those stay in
  `telemetry/reducer.py`. Do not move them into the builtin — the builtin is
  the *inner per-run formula only*, and the worker imports it without importing
  the reducer.

### 1.2 Why the formula has two homes and must stay byte-identical

`builtin_drift_loss` and `builtin_scalar` were **lifted verbatim** out of the
reducer and `tournament/scoring.py` so that:

1. the orchestrator AND the killable worker subprocess import the **same**
   implementation — no drift between the two sites, ever; and
2. a scoring plugin can *wrap* the default (`ctx.builtin_loss` /
   `ctx.builtin_scalar` on the frozen `DriftContext` / `ScalarContext` in
   `src/zicato/scoring/api.py`) instead of re-implementing it.

The dependency direction is one-way by construction: `scoring/builtins.py`
inlines its own copy of the judge-kind split rather than importing the
reducer, precisely so the worker can import the builtin without pulling the
reducer's world in. The golden test `tests/test_scoring_seams.py` pins
byte-identity across a representative corpus.

> ⛔ NEVER change a default scoring behavior by editing
> `src/zicato/scoring/builtins.py`. The builtins are frozen extraction
> artifacts; behavior changes ride *on top* via the dispatcher
> (`src/zicato/scoring/dispatch.py` — declarative transforms in
> `scoring/transforms.py`, dotted-spec plugins in `scoring/plugins.py`).
> Editing the builtin breaks the byte-identical guarantee that the golden
> test, every persisted `loss.json`, and every historical epoch relies on.

### 1.3 Seam 2 — the per-generation scalar, and why dict-then-`sum` is load-bearing

**Seam 2** synthesizes the per-generation scalar. The composition in
`builtin_scalar` looks redundant — build a dict, then `sum` its values —
and a well-meaning "simplification" into a running accumulation is exactly
the wrong move:

```python
# src/zicato/scoring/builtins.py — builtin_scalar (core)
    drift_component = weights.drift_weight * drift_loss_mean
    pass_component = weights.pass_weight * (1.0 - mean_score)
    scalar_components: dict[str, float] = {
        "drift": drift_component,
        "pass": pass_component,
    }
    for ns, value in namespace_aggregates.items():
        if ns == "drift:":
            continue
        component_name = ns[:-1] if ns.endswith(":") else ns
        scalar_components[component_name] = value
    diff_component = diff_complexity_component(weights, diff_size)
    if diff_component is not None:
        scalar_components["diff_complexity"] = diff_component
    return sum(scalar_components.values())
```

The module's own docstring states the reason, and it is worth internalizing
because it generalizes to every float-accumulating surface in zicato:

> The summation reproduces the ORIGINAL term order EXACTLY — drift, pass, then
> each namespace in `namespace_aggregates` iteration order — because **float
> addition is not associative**: accumulating the namespaces in a different
> order can flip the last bit of the result. The dict-then-`sum` shape is
> therefore load-bearing for the byte-identical guarantee (the golden test
> pins it), not a stylistic choice.

Concretely: `(a + b) + c != a + (b + c)` for IEEE-754 doubles in general. A
last-bit flip in the scalar sounds harmless until you remember that (a) the
gate compares scalars against a margin with strict inequalities, (b) persisted
`gen_score.json` files are compared byte-for-byte by parity goldens and by the
crash-resume path, and (c) the A/A calibration measures *spread* — a formula
that produces different bytes for the same inputs on different code paths
manufactures phantom noise.

Three more properties of Seam 2 that any extension must preserve:

- **The `drift:` namespace is excluded from the loop** because the `drift`
  component already owns the drift contribution — including it would
  double-count whenever `drift_weight` equals `namespace_weights["drift:"]`
  (the common back-compat case).
- **Key collisions collapse to the last writer** (two namespaces stripping to
  the same component name), exactly as the original inline code behaved. This
  is documented, mirrored behavior — not a bug to "fix."
- **New terms append LAST and must be exactly absent at their default.** The
  `diff_complexity` term is the template: it is appended after the
  float-order-sensitive namespace accumulation, only when
  `weights.diff_complexity_weight > 0.0` AND a `diff_size` was threaded.
  Otherwise the key is never written and `sum(...)` is byte-identical to the
  pre-feature formula. `diff_complexity_component` in `builtins.py` is the
  single seam both `builtin_scalar` and `aggregate_generation_score` read, so
  the appended scalar term and the surfaced `scalar_components` entry can
  never disagree.

> ✅ ALWAYS follow the `diff_complexity` template when adding a scalar term:
> (1) a `ScoringWeights` field defaulting to the inert value, (2) omitted from
> the contract canonical form at that default (see 03-contract-and-epochs.md
> §"Omit-at-default fields") so existing epochs never roll, (3) the component
> computed by ONE shared function, (4) appended last, (5) a golden test that
> proves byte-identity when off. If you skip (2), every existing workspace
> auto-rolls its epoch on upgrade; if you skip (4), you shift every namespace
> term's accumulated rounding and break the goldens; if you skip (5), you will
> not notice either failure until an operator does.

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

The scalar's pass component runs on `mean_score`, not `pass_rate`. On an
all-bool board every entry with a `pass_fail` also produces a score and vice
versa, so `mean_score == pass_rate` **byte-for-byte** — that identity is the
back-compat proof, and it is pinned by test. On a graded board, `mean_score`
tracks quality continuously with no threshold cliff.

`per_entry` rows carry `{"drift_loss", "pass_fail", "score"}` — the gate's
per-entry monotonicity scope reads `score` through `_row_score`
(`tournament/gate.py`), which falls back to the binary bit for pre-score
aggregates. Keep that fallback intact: it is what makes historical persisted
aggregates score identically today.

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
  the drift-loss-mean term, so a consumer of only the namespace surface gets
  the same drift contribution it used to derive from `drift_loss_mean`. Drift
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
| not-completed penalty | any non-success terminal state adds `not_completed_penalty(weights)` = `_NOT_COMPLETED_HEAVY_TERM_FACTOR * max(severity_weights)` to the drift loss (`telemetry/reducer.py`, exposed publicly so the runner's aborted-run synthesiser computes the *identical* magnitude — one source of truth for "what a not-completed run costs"). |
| `per_judge_loss` | per-judge weighted-loss attribution, aggregated by `_per_judge_loss_aggregate` for plugin/provenance visibility only — the builtin scalar does NOT add it separately (each judge's contribution is already folded into `drift_loss` by the reducer). Adding it again is a double-count. |

The distinction between the deterministic budget abort and the infra abort is
load-bearing everywhere a loss is *classified* rather than summed. The
screen's veto rules are the clearest statement (see
`src/zicato/epoch/screen.py::_is_budget_abort`): a budget abort vetoes
immediately (deterministic signal — no confirm run is spent on it), an infra
abort is no-signal (an outage must never disqualify a candidate). If you add a
new consumer that reads `abort_cause`, route the classification through
`is_infra_abort_cause` — do not string-match cause values yourself.

Skipped units (a matchup whose wall-clock budget ran out before the unit
launched) are synthesized as budget-exceeded losses by `_skipped_unit_loss`
(`src/zicato/tournament/unit_cache.py`) through the SAME aborted-run path a
killed worker uses — so a partial aggregate scores *consistently pessimistic*
(worst-case for the side that got clipped) and the skipped unit is a cache hit
next time. The statistical implication: a budget-clipped duel is biased
*against* whichever side had more units pending — the cut-short event is
always logged, never silent, and an operator comparing scalars across duels
with different clip states should know they are not exchangeable.

### 1.7 The dispatch layer — provenance and the plugin contract

Both seams route through `src/zicato/scoring/dispatch.py`
(`resolve_drift_loss` / `resolve_scalar`), which returns
`(value, provenance)`. The provenance string (`"builtin"`,
`"transform:pow"`, `"plugin:<dotted spec>"`, or the fail-open
`"builtin (fallback: plugin raised)"`) is persisted onto `loss.json` /
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
contract (`severity_weights.info = 1.0`, `drift_weight = pass_weight = 1.0`,
`runtime_weight = 0` — the zero runtime weight is load-bearing, because
per-run wall clock varies and any nonzero weight would break the exact
floor):

- the policy carries `tokens` defect tokens; the harness emits one
  `drift_detected` frame at severity `info` per remaining token per run, so
  **Seam 1** gives `drift_loss = 1.0 · 1.0 · tokens = float(tokens)` for
  every run;
- each known token fails exactly one predicate on the 5-entry board, so
  `mean_score = passes/5` (all-bool board ⇒ equals `pass_rate`
  byte-for-byte);
- **Seam 2**: every default namespace aggregate is exactly `0.0` (no cost /
  latency / rubric / schema metrics in this world), so

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
oracle tells you before an operator does. The same arithmetic seeded the
power harness's planted deltas (§13.4): one full token fix is a true effect
of 1.2 in scalar units, measured as `1.2·(1 − 2σ)` under measurement-flip
noise σ.

### 1.9 The observability layer: loop-health detectors over the chain

Statistics you cannot see rot silently. `src/zicato/health/diagnostics.py` is
the recommend-only observability layer over everything in this chapter — each
detector is a pure function over persisted history, surfaced per round. The
ones that watch the measurement chain and decision procedure:

| Finding code | Watches | Fires when |
|---|---|---|
| `degenerate_scoring` | the scalar's discriminating power | scoring stops separating generations |
| `non_differentiating_entry` | per-entry outcomes | an entry gives every generation the same result (dead weight on the board) |
| `flat_drift_signal` | drift counts | the drift channel goes flat (nothing to optimize on) |
| `no_expectations` | the board | entries with no evaluable expectation |
| `dead_judge` | judge emissions | a declared judge never fires |
| `noisy_judge` | §10's test–retest | pairwise disagreement above `0.25` |
| `margin_below_noise_floor` | §4 | `promote_margin` inside the measured A/A spread |
| `generalization_gap` | §12 #5 | `holdout_loss − train_loss` widened past threshold |
| `refresh_cadence` | §12 #6 | contract mined past `max_generations_per_contract` |
| `placebo_promoted` | §11 | CRITICAL: a no-op won a tournament |
| `preflight_signal_below_floor` / `preflight_saturated_contract` | §9 | the persisted pre-flight verdict re-surfaced every round |
| `stalled_loop` | the round stream | no genuine progress (placebo arms filtered out first) |

Two rules when extending this family: detectors are **recommend-only** (they
never gate, never mutate) and they must **filter calibration probes out of
optimization-stream logic** — a placebo arm is rejected by design every
cadence tick, and a detector that counts it as "another failed round" will
cry stall on a healthy loop.

---

## 2. The gate's rule ladder

`evaluate_gate` (`src/zicato/tournament/gate.py`) is the single promotion
decision function. Its rules apply **in order**; the first rejection wins and
is the one named in the journal. The full ladder, including the pieces wired
around `evaluate_gate` by the runner:

| Order | Rule | Knob | Rejects when | Reject reason prefix |
|---|---|---|---|---|
| 0 | Regression suite | `regression_gate_enabled` (default `False`) | the snapshot's own pytest suite fails or times out | (runner-level; see `tournament/regression.py`) |
| 1 | Scalar margin | `promote_margin` (default `0.01`) | `child_scalar > parent_scalar - promote_margin` | `challenger regressed:` / `insufficient improvement:` |
| 2 | Pass-rate monotonicity | `pass_rate_monotonicity` (default `True`) + `pass_rate_monotonicity_scope` (default `"per_entry"`) | scope-dependent, below | `pass-rate regression` |
| 3 | Namespace monotonicity | `namespace_monotonicity` flags | any flagged namespace's weighted aggregate rose past tolerance | `monotonicity_regression on namespace=` |
| 4 | Holdout confirmation | `overfitting.*` (default on, auto-degrades) | the train-win fails to hold on the holdout | `holdout_not_confirmed:` |

**Rule 0** runs *before* the scoring gate, in `_gate_with_regression` on the
runner path: a patch can trivially improve `drift_loss`/`pass_rate` on the
board while breaking the inner harness's own invariants, and no scoring signal
may override a failing suite. It is opt-in because many adapters ship no
tests; a snapshot with no `tests/` directory is a silent, journaled skip
(`"no tests/ directory; skipped"`), never a stall. A timeout counts as a
failure with the distinct summary `"timeout after <N>s"`.

**Rule 1 — the promote-margin semantics.** The scalar is a loss, so the
literal check is:

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
The two reject flavors are deliberately distinct — a child that improved but
not enough ("insufficient improvement") is different evidence from a child
that got worse ("challenger regressed") — and both state the real
child-minus-parent delta. `promote_margin` is a **noise threshold**, not a
quality bar: §4 explains why it must sit above the measured A/A floor and what
happens when it does not.

**Rule 2 — the two monotonicity scopes.** The scope knob exists because the
right policy depends on what the board *is*:

- `"per_entry"` (default): for every entry the parent scored, the child's
  continuous score may not drop below the parent's by more than
  `PER_ENTRY_SCORE_MONOTONICITY_TOLERANCE` (`0.02`). A bool entry the parent
  passed has score `1.0`, so the child must still pass — the historical
  must-still-pass rule, exactly. A vanished row reads as `0.0` (dropping
  ground truth is a regression). Right for invariant / regression-suite
  boards, where each entry is a promise.
- `"aggregate"`: reject only when the overall `mean_score` fell by more than
  `PASS_RATE_MONOTONICITY_TOLERANCE` (`1e-9`, pure float-noise padding). The
  child may trade individual entries as long as the net holds. Right for
  sampled/noisy evaluation boards — under per-entry scope, a single
  noise-flipped entry vetoes a genuinely better challenger (measured in §3).

There is deliberately no `"off"` scope value; disable the rule with
`pass_rate_monotonicity=False` so existing contracts stay byte-identical.

**Rule 3** compares per-namespace *weighted aggregates* (already
sign-unified, §1.5) with `NAMESPACE_MONOTONICITY_TOLERANCE` (`0.0`).
Zero-weight namespaces are skipped even when flagged — an operator who zeroed
a namespace's scoring contribution must not be surprised by it gating. Every
regressing namespace is named in the reason, not just the first.

**Rule 4** — holdout confirmation — is applied only after the three train
rules would promote, so a train reject always fires first with its specific
reason. Both `None` holdout arguments (small board, split disabled) skip the
step entirely: the decision is byte-identical to the pre-split gate. Details
in §5.

> ⚠️ TRAP: the gate's reject *reasons* are a stable surface. The dashboard's
> decision classifier and several tests consume the structured verdict fields
> (`deciding_rule`, `margin`, `regressed_*` — served by the reader layer, see
> 09-dashboard-and-query.md), but the human-readable strings also appear in
> journals that operators grep. If you must reword a reason, sweep consumers;
> never encode NEW machine-readable data only inside a reason string — that is
> exactly the client-side re-derivation anti-pattern bug #4 in
> 12-bug-casebook.md exists to teach.

`GateOutcome` records `delta_scalar` and `delta_pass_rate` **regardless of the
decision**, so the journal always has the same evidence shape whether the
experiment promoted, rejected, or deferred. Preserve that: dashboards render
rejected rounds too.

---

## 3. The noise doctrine

This section is the heart of the chapter. Read it before touching *anything*
that decides between two generations.

**Every measurement in zicato is a random draw.** Agents under test are
LLM-backed and vary run to run; judges are LLM-backed and disagree with
themselves (§10); even "the same" generation re-evaluated produces a different
scalar. The decision procedure — margin gate, replication, monotonicity scope,
evidence gate, screen, holdout — is a statistical test executed against those
draws. It therefore has *operating characteristics*: a false-promotion rate
under the null (a challenger identical to the champion), and power at a given
true effect size. Those characteristics are **measured, pinned facts** in this
repository, not vibes. The measurement instrument is
`tests/test_decision_procedure_power.py` (the Tier-2 power harness, §13),
driving the *real* tournament machinery under seeded noise.

### 3.1 The measured facts

Commit these to memory. They are the reason the defaults are what they are,
and any change you make must not silently invalidate them.

| # | Fact | Where measured / pinned |
|---|---|---|
| 1 | **A single naive duel promotes pure noise.** With `promote_margin=0.01` far below a measured A/A floor of ~0.66 (σ=0.22 harness) and no evidence gate, a challenger *identical* to the champion cleared the gate in **20 of 60** seeded A/A trials (the pinned test bound is ≥ 15/60). | `test_margin_below_noise_floor_without_evidence_gate_is_unsound` |
| 2 | **The Bradley–Terry evidence gate's CIs separate only after ~37 duels of an essentially unbroken win streak** on a two-contestant field; ANY mixed record never separates. It is therefore a pure **soundness** device — noise cannot manufacture 37 consistent wins — and never a power device. | module docstring + `EFFECTIVE_BUDGET = 38` calibration in the power harness; `evidence_gate.py` docstring |
| 3 | **Power is bought with replication.** Averaging 32 replicates shrinks the per-duel delta sd from ~0.66 to ~0.12, turning a 0.5×-floor true effect (~0.34) into a ~3-sigma-per-duel signal the win streak can sustain. | `EFFECTIVE_REPLICATES = 32` commentary + `test_power_at_planted_deltas` |
| 4 | **The evidence-gated contract's false-promotion rate under the A/A null is zero** over the pinned seeded trials — either the replicated crowning duel fails the margin, or the defer→replicate loop terminates `inconclusive`. | `test_aa_effective_contract_false_promotion_rate_is_zero` |
| 5 | **The naive default misses small true effects the effective contract catches**: at a ~0.5×-floor planted improvement, the naive contract promotes in ≤ half the trials; the effective contract's rate is pinned ≥ naive + 0.25 on the same seeds. | `test_naive_default_misses_small_effects_the_evidence_gate_catches` |
| 6 | **A 3×-floor effect is unmissable** (power 1.0 across every seeded trial) and power is monotone in effect size. | `test_power_at_planted_deltas` |
| 7 | **Screen false-veto ≈ flip-rate² under confirm-before-veto.** At per-entry flip noise σ=0.10 the confirmed rule measures ~1.0% false vetoes (pinned ≤ 2%) while the naive any-flip rule measures ~10% (pinned ≥ 5%, and confirmed ≤ naive/3). At the deliberately hot σ=0.22 the squaring still holds (~σ² ≈ 4.8%) but *no* single-confirm rule can reach 2% there. | `test_screen_false_veto_rate_confirm_beats_naive_any_flip` |
| 8 | **The A/A noise floor of a deterministic harness is exactly 0.0**, and of the σ=0.22 harness ≈ 0.663 (analytically `1.6·sqrt(σ(1−σ))` for that harness's structure). A measured floor of ~0 on a stochastic harness means the *seeding is broken*, not that the harness is quiet — see bug #3 in 12-bug-casebook.md. | `test_aa_null_calibration_measures_the_noise_floor` |

### 3.2 What the doctrine demands of a change

- **Any new comparison between two measured quantities needs a stated noise
  model.** "Child scalar < parent scalar" is not a decision procedure; "child
  scalar < parent scalar − margin, margin calibrated above the measured A/A
  floor, replicated K times" is.
- **Any new veto/gate needs a measured false-positive rate under the null.**
  The screen's confirm-before-veto design (§ in 05-proposer.md) exists because
  the naive rule's false-veto rate was measured at ~σ per flip-capable entry —
  an order of magnitude too hot.
- **Any claim of improved power needs the planted-delta measurement**, at
  effect sizes stated in multiples of the measured floor (the harness plants
  0.5×, 1×, 3×).
- **Soundness may not be traded for power silently.** The evidence gate is
  opt-in *in code* precisely because its honest cost (a ~37-duel streak,
  ~32×2×board fresh runs per crowning) would freeze a small-budget default;
  the scaffolded contracts (`zicato init`, the builder's blank draft) enable
  it **explicitly** with an honest replicate budget so operators see the bill.
  If you are tempted to make it default-on with a small budget "so everyone
  gets soundness," you will instead freeze every true promotion at
  `inconclusive` — that trade was measured and rejected.

> ⛔ NEVER assert a statistical property in a docstring, commit message, or
> test name without a pinned measurement behind it. The phrase to internalize
> from the power harness: operating characteristics are "measured, not
> asserted by hope."

> ⚠️ TRAP: deterministic test contracts hide noise bugs. The convergence
> oracle (`tests/test_convergence_known_answer.py`) runs a σ=0 world where a
> cache replay and a fresh draw are *equal by value* — so a procedure that
> accidentally replays one sample N times looks correct there. This is
> exactly how bug #8 (evidence replicates were not independent samples) hid
> behind a green deterministic e2e. Every statistical mechanism needs at
> least one knob-ON test under σ>0. See 12-bug-casebook.md §"Meta-lessons".

### 3.3 The screen's statistical doctrine: veto-first, selection bias, confirm-before-veto

The pre-tournament candidate screen (`src/zicato/epoch/screen.py`; the
proposer-side wiring is 05-proposer.md) deserves its own doctrinal note here
because it is the clearest worked example of designing a *new* decision
surface under the noise doctrine — and of what a weaker estimator is and is
not allowed to decide.

The screen is a **worse-powered estimator than the tournament it precedes**:
1–2 entries × 1 replicate versus a full board × replicates. The design pass
measured what that means — a 2-entry screen ranking close candidates is
approximately random choice plus winner's curse. The design that survived:

- **Veto-first, never ranking.** The screen's high-confidence regime is
  *categorical failure* — a candidate that flips entries the champion passes,
  or blows its wall-clock budget, is detectably broken even at n=1. So the
  screen DISQUALIFIES; the best-of-N critic/heuristic still chooses among
  survivors; an all-vetoed slate falls back to critic-over-all (a veto can
  narrow but never empty a propose step, and a screen *error* degrades to
  no-signal — the screen must never fail a round).
- **Confirm-before-veto.** A pass-flip is a *suspected* veto: the flipped
  entries re-run ONCE at `SCREEN_REPLICATE_BASE + 1` (3001), and only a flip
  that flips twice vetoes. Under per-entry flip probability p the false-veto
  probability is bounded near p² instead of p — the measured rates are fact
  #7 in §3.1. Budget aborts skip the confirm (deterministic; nothing to buy).
- **The panel scalar is selection-biased by construction** — a handful of
  champion-passing train entries chosen *for the veto*. It is advisory
  tiebreak material inside the slate only, and it is **never journaled as
  evidence, never compared against tournament scalars**. Winner's curse on
  the survivor is tolerable exactly because the tournament re-measures with
  fresh draws and the Ladder/holdout still guards promotion.
- **Restricted visibility holds**: the panel is train-slice only (the holdout
  is never eligible), and every result string carries counts only — never an
  entry id (`_summarize` in `screen.py`).

> ⛔ NEVER promote a screen scalar (or any selection-biased, small-panel
> measurement) into gate evidence, standings, or the journal's scored record.
> The moment a biased estimator's number sits next to an unbiased one in a
> comparable field, some later consumer will compare them.

---

## 4. A/A noise-floor calibration

**What it is.** The oldest trick in A/B testing: evaluate the SAME generation
K times and look at the spread of the resulting scalars. Any two draws form an
A/A duel whose true effect is exactly zero, so the observed `delta_scalar`
spread IS the noise floor. Home: `src/zicato/tournament/calibration.py`.

**How it measures.** `measure_noise_floor` runs K (default
`DEFAULT_CALIBRATION_RUNS = 5`, giving 10 pairwise deltas) fresh draws of the
champion through `_run_board_units_fast` — the *same* board-unit machinery,
subprocess workers, scoring, and per-unit persistence every duel uses — so the
floor is measured under exactly the conditions duels run under. Each draw runs
at a distinct reserved replicate index (`CALIBRATION_REPLICATE_BASE = 1000`,
`1000 + draw`), which does two things at once:

1. distinct cache slots ⇒ each draw is a fresh sample, and re-running the
   audit under the same contract is an idempotent set of cache hits (a
   repeated `zicato board audit` costs nothing);
2. the index is **stamped onto each entry's context**
   (`_stamp_replicate_index`) before the run — the cache key alone does not
   reach the harness, and a seeded harness derives its noise draw from the
   *stamped* index. Without the stamp, every "fresh" draw re-rolls the
   identical seed and a stochastic harness measures a floor of exactly 0.0.
   That was a real shipped bug (case #3 in 12-bug-casebook.md).

```python
# src/zicato/tournament/calibration.py — measure_noise_floor (the stamp)
    for draw in range(runs):
        replicate_index = CALIBRATION_REPLICATE_BASE + draw
        losses = await _run_board_units_fast(
            adapter=adapter,
            child_gen=generation,
            board=_stamp_replicate_index(board, replicate_index),
            ...
            match_id=f"aa-calibration:{draw}",
            replicate_index=replicate_index,
        )
        agg = aggregate_generation_score(list(losses.values()), weights)
        scalars.append(float(agg.get("scalar", 0.0)))
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
`goal` field). Changing it does not — must not — roll the epoch.

**When it runs.** Three wirings:

| Surface | Trigger |
|---|---|
| `zicato board audit` | manual, any time; measures the current champion and persists |
| epoch-open hook | workspace `config.json` `"calibrate_noise_floor": K` — once per epoch at the first evolve round, idempotent, best-effort |
| evolve-start check + per-round health | reads the persisted floor; see below |

**The margin-vs-floor warning.** `margin_below_floor(promote_margin, floor)`
returns true when the margin is strictly below the measured `max_abs_delta`.
The health finding `margin_below_noise_floor`
(`src/zicato/health/diagnostics.py::detect_margin_below_noise_floor`) fires as
a **warning** when the evidence gate is off ("duels are decided by the margin
alone") and downgrades to **info** when the gate is on (the defer→replicate
loop still holds promotions to CI separation). It never hard-refuses a run —
calibration is recommend-only, like every board-reflection surface.

> ✅ ALWAYS treat `NoiseFloor.to_json()` as a tolerant read on the consumer
> side: `margin_below_floor` returns `False` for `None`/malformed input by
> contract. A dashboard or health reader that raises on a missing floor breaks
> every workspace that never calibrated.

> ⚠️ TRAP: a floor of exactly `0.0` has two very different meanings — a
> genuinely deterministic harness (target_0's planted-defect adapter measures
> exactly 0.0 by design), or a seeding bug where every draw re-rolled the same
> sample. If you see 0.0 on a harness you believe is stochastic, suspect the
> stamp path first (`_stamp_replicate_index` must reach the entries the run
> actually consumes), and confirm with the power harness's floor test which
> asserts the σ=0.22 world lands in `[0.4, 1.0]`.

---

## 5. The Ladder-mediated holdout

Phase A of the overfitting program (§12) built the train/holdout split
(`src/zicato/board/split.py`) and the gate's confirmation step (§2 rule 4).
That makes a *single* holdout query trustworthy. It does nothing about the
deeper failure: the loop queries the *same* holdout every round, adaptively,
and a reused holdout "gets used up" — its confirmations become an
optimistically-biased signal the optimizer can climb. The Ladder
(`src/zicato/tournament/ladder.py`) is the Blum–Hardt 2015 mechanism for
exactly this "submit, see score, submit again" loop, in its parameter-free
variant.

### 5.1 The two rules

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
(`effective_threshold` = `cfg.threshold or promote_margin`, plus
`noise_scale`, default 0) — parameter-free by default.

**Budget rule.** Every query that consults the holdout charges one unit of the
per-epoch budget (`LadderConfig.budget`), charged *before* the release
decision — a withheld query still pays, because the holdout was consulted to
learn the gap was inside the band. When the budget is exhausted, nothing is
released and the state is returned unchanged: the loop degrades to the
train-only decision — a train-win is no longer holdout-gated, exactly Phase-A
behavior with no holdout.

### 5.2 The released-non-confirmation-is-the-only-flip rule

Put the two rules together and you get the invariant a weaker agent must not
break: **the only thing that can flip a train-measured promotion to a reject
via the holdout is a RELEASED non-confirmation.** A withheld query cannot flip
anything (its result does not count this round); an exhausted budget cannot
flip anything; and the proposer is only ever shown the threshold-gated
confirmation *bit* — never the raw per-entry holdout result, never the raw
holdout scalar of an unreleased round. `LadderRelease.confirmed` on a withheld
query is the *previous best* bit, deliberately stale.

### 5.3 The asymmetry rationale

The holdout confirmation itself (`_holdout_confirms` in
`tournament/gate.py`) is asymmetric on purpose:

- it rejects when the challenger's holdout loss **rose past** the champion's
  by more than `promote_margin` (a real holdout regression, not noise), or
  when the holdout shows a pass-rate regression under the SAME
  `pass_rate_monotonicity_scope` the train slice uses (one consistent policy —
  per-entry on both sides, or aggregate on both);
- it is **never** asked to clear `promote_margin` in the *improving*
  direction. A train-measured win that merely holds flat on the holdout is a
  confirmation, not a failure.

This asymmetry is what makes the holdout a guard against *board
memorization* rather than a second, stricter promotion bar. If you "tighten"
it into requiring holdout improvement, you halve the loop's power for zero
soundness gain — the holdout slice is small, its per-round measurement is
noisier than the train slice, and demanding improvement on it is demanding a
signal the slice cannot statistically deliver.

### 5.4 State, persistence, and the record shape

`LadderState` (budget totals, `best_holdout_scalar`, `best_confirmed`) is a
small frozen object the runner persists across rounds per epoch; the module
itself is **pure** — no filesystem, no clock, no randomness. The stable
`record.holdout` block the dashboard consumes is assembled by
`holdout_record` and its shape is frozen (`confirmed`, `train_scalar`,
`holdout_scalar`, `ladder_released`, `ladder_budget_total`,
`ladder_budget_remaining`, `threshold`); the runner writes
`record.holdout = None` when there was no holdout to consult at all, so a
populated block always means a holdout existed.

When the holdout is empty — a board under
`overfitting.min_board_size_for_split` (default 6) with no explicit `holdout`
tag, or the split disabled — the Ladder is never consulted and behavior is
byte-identical to Phase A. When `LadderConfig.enabled` is `False`, the runner
runs the raw Phase-A confirmation directly (no budget, no release rule).

Holdout confirmation is wired through **every** structure, not just the
gauntlet: the multi-challenger path routes its crowning through
`runner.confirm_crowning_holdout` (see 06-tournament-and-selection.md
§"Holdout through structures").

> ⛔ NEVER surface a raw holdout artifact to the proposer: not a per-entry
> result, not an unreleased scalar, not an entry id. The proposer's holdout
> view is exactly one bit (the released/re-reported confirmation), by
> Blum–Hardt design. Any widening of that channel re-opens adaptive
> overfitting of the holdout and invalidates the reuse guarantee — this is an
> overfitting-boundary change and requires a design pass (see
> 14-goals-and-roadmap.md §"How to propose new work").

---

## 6. The evidence gate (Bradley–Terry pre-gate), post-fix mechanics

Home: `src/zicato/selection/evidence_gate.py` (pure verdict machinery) +
`src/zicato/selection/driver.py::confirm_promotion_with_evidence` (the
defer→replicate loop) + the orchestrator's two `_replicate_duel`
implementations (gauntlet confirm and multi-challenger field). Opt-in via
`TournamentStructure.params["promote_confidence_threshold"]` — an absent param
adds nothing to the contract canonical form, so the contract hash is
byte-identical when the operator does not opt in.

### 6.1 The verdict

`evidence_verdict` fits Bradley–Terry over the accumulated duel audit and
answers for the crowning pair:

- **`promoted`** — `P(theta_child > theta_champion) >= threshold` AND the two
  rating CIs (`theta ± CI_Z·se`, `CI_Z = 1.959963984540054`, the 95% normal
  quantile) are **separated**. Crown on evidence.
- **`deferred`** — the bar is unmet or CIs overlap, and replicate budget
  remains. Hold and replicate the closest-CI duel.
- **`inconclusive`** — budget exhausted, CIs still overlap. Terminal;
  recorded to the dead-letter queue; the champion stands.

A fit is only trusted at `MIN_CREDIBLE_DUELS = 3` resolved duels for the pair
— below that the Fisher-information SE is dominated by the prior and a CI
computed there would defer (or crown) on noise, so the verdict is the gate's
own (`credible=False`, no override). The recommended threshold the scaffolds
write is `0.8` — deliberately below the 0.95 the CI level speaks at, because
CI separation is the sharp half of the test.

### 6.2 The reserved base 4000 and both-sides-fresh

Every evidence replicate `j` runs the crowning pair at replicate index
`EVIDENCE_REPLICATE_BASE + j` (`4000 + j`) through the same `run_matchup` every
duel uses, with the `replicate_base` parameter threaded down to
`_run_replicated`. Why a reserved slot, verbatim from the orchestrator's
wiring:

```python
# src/zicato/orchestrator.py — the gauntlet _replicate_duel closure
        # ... at a RESERVED replicate index (EVIDENCE_REPLICATE_BASE
        # + j for evidence replicate j), so each replicate draws BOTH sides
        # fresh: a fast-mode cache read of the canonical replicate-0 slots
        # would replay one identical sample into the Bradley--Terry fit
        # (shrinking its SE by repetition alone), and a full-mode force-fresh
        # re-run at slot 0 would clobber the canonical ``loss.json`` that
        # reindex/crash-resume key on. The reserved slot is a natural MISS
        # the first time (a fresh draw of champion AND challenger) and an
        # idempotent cache HIT on a resumed confirm ...
        replicate_slot = EVIDENCE_REPLICATE_BASE + evidence_replicates_run
        evidence_replicates_run += 1
        matchup_id = f"bt-replicate:r{replicate_slot}:{left_id}:{right_id}"
```

Three properties, each of which was a *shipped bug* before the fix
(12-bug-casebook.md case #8):

1. **Fresh, not replayed** (fast mode). At slot 0 the child cache-read its
   canonical `loss.json`, so every "replicate" was a byte-identical replay —
   duplicate data shrank the BT SE by repetition alone until CIs separated:
   an unsound promotion path in the exact device that exists for soundness.
2. **Never clobber canonical** (full mode). A force-fresh re-run at slot 0
   re-persisted over the child's canonical `loss.json` — the file reindex and
   crash-resume key on.
3. **Both sides fresh** (full mode). The champion side was never re-drawn —
   one-sided sampling makes the CIs narrower than the truth.

### 6.3 The duplicate-audit refusal

The driver's loop enforces independence *structurally*, not by trust:

```python
# src/zicato/selection/driver.py — confirm_promotion_with_evidence
        extra = await replicate_duel(candidate.left_id, candidate.right_id)
        # The spend is counted regardless: the budget bounds duels RUN, and
        # skipping the count on a duplicate would loop forever against a
        # runner that keeps replaying one draw.
        replicates_spent += 1
        if extra.matchup_id in seen_matchup_ids:
            log.warning(
                "evidence pre-gate: replicate duel returned an already-audited "
                "draw (matchup_id %r) — not appended ... identical data must "
                "never separate CIs", extra.matchup_id,
            )
            continue
        seen_matchup_ids.add(extra.matchup_id)
        audit.append(extra)
```

The matchup id encodes the reserved slot
(`bt-replicate:r{slot}:{left}:{right}`), so "same id" means "same draw." A
runner that replays one draw spends its budget but never grows the audit — the
gate verdict passes through instead of a repetition-driven crown. Note the
spend-counting rule: the budget bounds duels *run*, and a duplicate that did
not count would loop forever.

### 6.4 The two-phase loop and the dead-letter terminal

`confirm_promotion_with_evidence` is shared by BOTH selection shapes (the
multi-challenger driver via `resolve_tournament`'s `pre_gate`, and the
gauntlet path calling it directly on its single crowning duel):

- **Bootstrap** — a gauntlet produces one crowning duel, below the credibility
  floor; the loop replicates the crowning pair up to `MIN_CREDIBLE_DUELS`
  before judging. With no runner/budget, the gate verdict passes through
  unchanged (no fit to override it — safe).
- **Refine** — once credible: `promoted` terminates with the crown;
  `deferred` spends another closest-CI replicate (`closest_ci_duel`, argmin of
  the CI gap, restricted to the crowning pair on the gauntlet path) and
  refits; budget exhausted with overlapping CIs terminates `inconclusive`.

An `inconclusive` terminal maps onto the closed decision enum's `DEFERRED`
token (kept for analysis, lineage head unchanged) and fires
`on_inconclusive`, which the orchestrator wires to the dead-letter writer:
one record per unresolved duel at `runtime/inconclusive/<generation_id>.json`
(`src/zicato/selection/dead_letter.py`) carrying the full `gate.rating` block
and the per-refit `ci_history` — **nothing is silently dropped**. The record
is additive: it exists only on runs that opted into the pre-gate and reached
the terminal, so every other run's runtime tree is byte-identical.

> ✅ ALWAYS pass gate-rejects through the pre-gate untouched. The pre-gate is
> consulted only on a gate-promote and can only *hold* a promotion
> (`decision != "promoted"` returns the base verdict verbatim). Any change
> that lets it manufacture a promotion — or a rejection — breaks the
> protected-incumbent invariant.

> ⚠️ TRAP: "the CIs aren't separating; let me widen `CI_Z` down / lower the
> threshold / accept overlapping CIs after N tries." Each of those converts
> the soundness device into a noise-promotion device. If separations are not
> happening for *true* improvements, the deficiency is measurement variance —
> raise per-duel `replicates` (fact #3 in §3.1) or the effect is genuinely
> below the contract's resolvable floor (run `zicato board preflight`, §9).

### 6.5 Inside the fit — why ~37 wins, and what the prior is doing

The fit itself (`src/zicato/selection/rating.py::fit_bradley_terry`) is a
pure-Python Newton solve of the Bradley–Terry maximum likelihood: contestant
`i` has latent strength `theta_i`, `P(i beats j) = sigma(theta_i − theta_j)`,
each duel outcome is one Bernoulli win (a replicate of the same pairing is a
separate outcome — that is how replication sharpens the fit natively). The
properties a consumer must understand:

- **The ridge prior (`prior=1.0`) is what makes the model identifiable.** BT
  strengths are only defined up to an additive constant, and a contestant
  with a perfect (or empty) record would diverge to ±inf without shrinkage.
  The prior keeps every strength finite and the Fisher information matrix
  positive-definite (SEs always exist). The fit is then centered to the
  zero-sum gauge for cross-call comparability.
- **The prior is also why the SE shrinks slowly.** At small n the information
  is prior-dominated (hence `MIN_CREDIBLE_DUELS`); each additional
  *consistent* win adds `p(1−p)` information, but as the streak lengthens `p`
  saturates toward 1 and each win adds *less*. The compounding effect on a
  two-contestant field: the 95% CIs (`theta ± 1.96·se`) first separate at
  ~37 duels of an essentially unbroken streak, and any mixed record never
  separates — the measured fact #2 in §3.1. This is a *property of the
  shipped fit with its shipped prior*, not a tunable; if you change `prior`,
  you have changed the soundness/cost point and must re-measure the
  separation cost on the power harness.
- **Ties are not observations.** `audit_duels` feeds only resolved
  (`delta_scalar != 0.0`) duels; the continuous loss makes exact ties
  measure-zero, and a tie fed to BT would be a modeling error.
- `prob_stronger` treats the two fitted strengths as independent normals —
  `P(theta_a > theta_b) = Phi((theta_a − theta_b)/sqrt(se_a² + se_b²))` —
  with degenerate point estimates resolving to hard 1.0/0.0/0.5. This is the
  probability the threshold speaks to; the CI-separation requirement is the
  sharper, correlated-information-free half of the test.

The fit is opt-in as a *standings* device too (`params["rating"]` selects
`theta_rank` ordering — see 06-tournament-and-selection.md §"Rating layer");
in that role it only ever proposes an ordering. The gate is never involved.

---

## 7. Replication semantics

Replication is the loop's power lever (§3, fact #3). Its mechanics:

### 7.1 Averaging and the strict-majority pass

`run_matchup(..., replicates=N)` runs the paired board N times;
`_average_losses` (`src/zicato/tournament/unit_cache.py`) folds the N runs
into one per-entry loss map *before* aggregation:

- `drift_loss` — the arithmetic mean across replicates (the scalar-bearing
  field);
- `pass_fail` — the **strict-majority vote** (`true_count * 2 > len(votes)`;
  an even split is a fail), with `None` preserved when no replicate produced a
  pass/fail — so the pass-rate monotonicity rule stays meaningful under
  replication;
- every other field — taken from the first run's profile (not scalar-bearing
  in the gate).

A noisy single run no longer decides a duel; the gate itself is unchanged.

### 7.2 Per-structure defaults

The base `SelectionStrategy` declares `_default_replicates = 2` — the
noise-aware default (`src/zicato/selection/strategy.py`). Per structure:

| Structure | Default `replicates` | Rationale (from the strategy docstrings) |
|---|---|---|
| gauntlet | 2 (inherits base) | pin `"replicates": 1` in params for the historical single-run behavior |
| single_elim | 2 | a single-elim knockout has no second chance; replication is its noise defense |
| double_elim | 2 | replication rather than relying on the losers' bracket for noise correction |
| swiss | 2 | per-pairing replication is how a swiss earns trustworthy standings |
| racing | **1** | racing's adaptive resource allocation (rung halving) IS its per-sample noise weapon; the final rung runs the full board × replicates × both sides and is already the expensive step |

`replicates` lives in `TournamentStructure.params`, so changing it **rolls the
epoch** — it changes what a measurement *is* under the contract.

### 7.3 Replicate-keyed cache slots and the canonical-r0 rule

The unit cache key is the board unit itself: `(generation_id, entry_id,
replicate_index)`. The path mapping (`_unit_loss_path`):

- **replicate 0 ⇒ the canonical `runs/<entry>/loss.json`** the worker writes —
  the same file the seed champion's full-board scoring, reindex, crash-resume,
  and every single-replicate run key on;
- **replicate r>0 ⇒ the sibling `runs/<entry>/loss.r<r>.json`** — additional
  noise samples cache per replicate without colliding with the canonical file.

Corollaries you must not violate:

- **The canonical r0 slot is written by the tournament's own replicate-0 run
  and by nothing else.** The worker routes its write through
  `_unit_loss_path(..., _entry_replicate_index(entry))` — before that fix
  (12-bug-casebook.md case #1), later replicates clobbered r0 with the last
  draw.
- **Replication is incremental.** Requesting R replicates when r<R already
  exist runs only the missing R−r; cached samples are reused, never re-run.
  (Cheap replication is why the evidence gate's `DEFAULT_REPLICATE_BUDGET` can
  be small.)
- **`replicate_base` shifts the whole window**: replicate `i` runs, caches,
  and stamps its harness noise draw at `replicate_base + i`. Base 0 is every
  tournament matchup, byte-identical to before the parameter existed; reserved
  bases are §8's ledger.
- The stamped index and the cache index must be the **same number** — the
  stamp is what a seeded harness derives its draw from, the key is where the
  draw persists. Diverge them and you either alias draws or mislabel slots
  (bugs #1 and #3 are the two halves of getting this wrong).

`champion_eval_mode` provenance (`"full"` / `"fast"` / `"fast-degraded"`) is
derived from the LEFT side's pre-run cache state and is journal provenance
only — it never enters the gate or the contract.

---

## 8. THE RESERVED REPLICATE-BASE LEDGER

This is a formal registry. The replicate-index space of every
`(generation, entry)` unit is partitioned by convention, and the convention is
enforced only by this ledger plus the cross-referencing docstring on
`EVIDENCE_REPLICATE_BASE` — there is no runtime collision checker. Treat it
like a port-number registry.

| Base | Range in practice | Owner | Constant | Purpose |
|---|---|---|---|---|
| `0` | `0 .. replicates-1` | tournament duels | (implicit; `replicate_base=0`) | real matchup samples; r0 is the canonical `loss.json` |
| `1000` | `1000 .. 1000+K-1` | A/A calibration | `zicato.tournament.calibration.CALIBRATION_REPLICATE_BASE` | noise-floor draws; idempotent across `board audit` re-runs |
| `2000` | `2000` (single draw) | contract pre-flight | `zicato.epoch.preflight.PREFLIGHT_REPLICATE_BASE` | the degraded-copy probe draw (cached under the CHAMPION's id) |
| `3000` | `3000` + confirm at `3001` | candidate screen | `zicato.epoch.screen.SCREEN_REPLICATE_BASE` | tryout panel runs (`3000`); the confirm-before-veto re-run (`3001`) |
| `4000` | `4000 .. 4000+budget-1` | evidence gate | `zicato.selection.evidence_gate.EVIDENCE_REPLICATE_BASE` | independent evidence draws of BOTH sides of the crowning pair |
| `5000` | `5000 .. 5000+K-1` | board reflection (claimed; constant lands with `reflection/corpus.py`) | `zicato.reflection.corpus.REFLECTION_REPLICATE_BASE` | active observation-corpus replicates (BOARD-REFLECTION.md); infra-abort draws voided |

Design properties of the ledger:

- Bases are spaced far apart so no plausible K (calibration runs, evidence
  budget, screen panel) can walk one owner's range into another's.
- Every reserved-base evaluation **stamps** its index onto the entries as well
  as keying the cache with it (§7.3's same-number rule), so seeded harnesses
  draw fresh per slot.
- Reserved-base results are cache-idempotent: re-running the audit /
  pre-flight / a resumed evidence confirm re-reads persisted draws instead of
  burning new runs.
- The screen additionally uses **ephemeral generation ids**
  (`{parent}-screen-r{round}c{i}`, which can never match a real `v\d+` id) so
  even its reserved-slot files live under phantom directories that are swept —
  belt and suspenders on top of the base.

### 8.1 The claiming procedure for a new base

If you are building a new out-of-tournament evaluation (a new probe, a new
audit, a new confirmation loop), follow this procedure exactly:

1. **Pick the next free thousand** (`5000` is claimed by board reflection; the
   next free base is `6000`). Do not squat in
   an owner's range and do not subdivide an existing owner's range without
   that owner's module adopting the sub-slot explicitly (the screen's `+1`
   confirm slot is declared in `SCREEN_REPLICATE_BASE`'s own docstring).
2. **Declare a module-level constant** named `<PURPOSE>_REPLICATE_BASE` in the
   owning module, with a docstring that states what runs there and why it can
   never collide.
3. **Cross-reference the ledger.** Update the reserved-ladder note on
   `zicato.selection.evidence_gate.EVIDENCE_REPLICATE_BASE` (the canonical
   in-code ledger) and every sibling docstring that enumerates the ladder
   (`calibration.py`, `preflight.py`, `screen.py`) — and this table.
4. **Stamp AND key** with the same index (`_stamp_replicate_index` +
   `replicate_index=`/`replicate_base=`), through the same board-unit runner
   every duel uses.
5. **Prove isolation with a test**: canonical r0 slots byte-identical across
   your new evaluation (the pattern in
   `test_full_mode_evidence_loop_never_touches_canonical_slots`), and your
   draws persisted under your base for every side you run.

**Verify** (the ledger self-audit — run after any change in this area):

```bash
grep -rn "REPLICATE_BASE\b *[:=]" src/zicato --include="*.py"
# Expect exactly: 1000 (calibration), 2000 (preflight), 3000 (screen), 4000 (evidence)
```

### 8.2 The corruption that follows from squatting

What actually goes wrong if you run an out-of-band evaluation at an
unreserved index — every one of these is a *measured* failure mode, not a
hypothetical:

- **At 0**: you either replay the canonical sample as if it were fresh
  (fast-mode: repetition masquerading as evidence — the unsound-promotion half
  of bug #8) or you overwrite the canonical `loss.json` that reindex and
  crash-resume key on (full-mode: a crash mid-loop resumes onto your
  corrupted r0 — the clobber half of bug #8, and the whole of bug #1).
- **At 0..R-1 generally**: you pre-seed slots a later replicated duel will
  cache-HIT, so *its* "fresh" samples are your probe's draws — your evaluation
  leaks into tournament evidence. This is the cache-leakage trap the screen's
  design pass identified before it shipped: screen-selection luck would have
  inflated the subsequent tournament score on the screened entries.
- **In another owner's range**: you make their idempotence a lie. A re-run
  `board audit` would silently read your draws as its own, reporting a floor
  measured on the wrong distribution.

> ⛔ NEVER "borrow" a replicate index because the cache is conveniently warm
> there. A warm cache at an index you did not claim is somebody's evidence.

---

## 9. Contract pre-flight — prove the board can out-signal its own noise

Home: `src/zicato/epoch/preflight.py`. Before an epoch burns rounds, two cheap
measurements answer the one question that decides whether an evolve loop can
work at all: **is the contract's achievable signal larger than its noise
floor?**

- **(a) the A/A floor** — reuses `measure_noise_floor`'s draws (same cache
  slots as `zicato board audit`; idempotent between the two surfaces);
- **(b) the scripted-perturbation duel** — the champion vs a deliberately
  degraded copy of itself: the FIRST enumerated mutation point has its span
  blanked/scrambled (`degraded_content_for`: spans reverse
  character-by-character, code regions become `pass`, `.py` files blank to a
  comment) in an **ephemeral** scratch copy via the real applier. The degraded
  tree never enters the lineage; its single draw caches under the *champion's*
  id at `PREFLIGHT_REPLICATE_BASE` (2000). `|degraded_scalar −
  mean(champion_scalars)|` is the contract's demonstrated **achievable
  signal**.

The pure verdict (`preflight_verdict`) encodes the two mirror pathologies:

| Verdict | Condition | Pathology |
|---|---|---|
| `warn` (saturated) | spread across ALL probes — every A/A draw plus the degraded draw — is **exactly zero** | zero variance / saturation: even a deliberately-broken tree scores identically. The historical signature is the `1.000000` null run — the loop spins forever with nothing to climb. The board, not the noise, is the problem. |
| `refuse` (recommended) | `signal <= floor_max_abs_delta` | noise swamps the margin: an A/A re-roll moves the scalar as much as a deliberate degradation does; every duel is decided by noise. The contract cannot possibly resolve the *smaller* improvements a proposer will offer. |
| `ok` | otherwise | signal demonstrably clears noise |

Saturation is checked **first**, deliberately: a saturated contract trivially
also has `signal == floor == 0`, and the saturation diagnosis is the
actionable one.

The verdict persists onto the epoch record (`config.json`'s additive
`preflight` field, never hashed); re-surfaced every round through loop health
(`detect_preflight_verdict`: critical `preflight_signal_below_floor` / warning
`preflight_saturated_contract`).

**Gating at evolve start (issue #84).** The pre-flight is **default-on**: at
evolve start the loop measures it once per epoch (idempotent, best-effort)
unless the runtime opts out, and acts on the verdict per the runtime-only
`RuntimeConfig.preflight_gate` knob (never rolls the epoch — a runtime tuning
knob like `infra_abort_round_threshold`):

| `preflight_gate` | On a below-floor (`refuse`) / saturated (`warn`) verdict |
|---|---|
| `"warn"` (**default**) | LOUD `log.warning` at evolve start + the per-round health finding; the run **proceeds** (recommend-only philosophy) |
| `"refuse"` | additionally raises `PreflightRefusedError` on a `refuse` verdict — `evolve_n_rounds` catches it and stops with reason `preflight_refused` **before spending rounds**, no traceback |
| `"off"` | skip the measurement entirely — byte-identical to the pre-#84 behavior (the escape hatch deterministic oracles use so the orthogonal probe never runs the champion) |

Surfaces: `zicato board preflight` (manual, always recommend-only) + the
epoch-open hook `"contract_preflight": K` still sets the number of A/A draws K
(absent ⇒ `DEFAULT_CALIBRATION_RUNS`). Because the measurement runs the
champion for its A/A floor, a fast-mode test asserting "the champion is never
re-run" must set `runtime.preflight_gate: "off"`.

Note the connection to 14-goals-and-roadmap.md: target_1's structural
mock-null (`mocks.py` discards the system prompt, so no instruction patch can
move any measurement) is precisely the saturation pathology — the pre-flight
exists so that class of dead contract is caught at the door instead of in
round 7.

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
does not become) a `custom:<judge_name>` `DriftCount` on a real run, i.e.
exactly the noise the judge injects into the scalar. The disagreement measure
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
`per_judge_weights` — the contract's routing knob for exactly this signal:
down-weight the noisy judge rather than letting it thrash the scalar.

Surface: `zicato board judges --test-retest [--retest-k K]
--auxiliary-call-llm <dotted-path>` over a settled transcript from a prior run
or the synthetic `FIXTURE_TRANSCRIPT`. The `aux_call_llm` parameter is the
endpoint seam — tests script it; a real auxiliary endpoint slots in unchanged
(that live measurement is endpoint-gated; see 14-goals-and-roadmap.md
§"Endpoint-gated backlog").

---

## 11. The placebo arm

`src/zicato/evolve/placebo.py` — the control arm of A/B methodology, opt-in
via `overfitting.random_baseline_every_n` (default 0 = off; omitted from the
contract canonical form at the default). Every Nth epoch-cumulative round the
orchestrator fields ONE extra challenger whose patch is a
**semantics-preserving no-op**: the first enumerated mutation point's current
value re-emitted unchanged (with the applier-aware span handling in
`placebo_noop_content` so a `.py` span re-emits its resolved *value*, not an
assignment echo). The placebo is a genuine lineage child derived through the
real `GenerationStore.derive_generation` seam — never a synthetic score
injection — and its hypothesis `core_idea` opens with
`PLACEBO_HYPOTHESIS_MARKER` so every consumer can recognize the arm.

The arm measures **the gate itself**:

- **rejected** — the expected outcome, every time: identical behavior leaves
  no improvement to clear `promote_margin`. Each cadence tick quietly
  recalibrates the fact that the gate can still tell "no change" from
  "improvement."
- **promoted** — the alarm. A no-op that wins a tournament means the decision
  procedure is promoting noise — margin under the floor, a broken reducer, a
  rigged gate. `detect_placebo_promoted` raises the CRITICAL
  `placebo_promoted` health finding, and the correct operator reading is:
  **recent real "wins" are suspect too.** A promoted placebo is never a
  placebo problem; it is a decision-procedure problem that the placebo
  happened to expose.

Placebo experiments are filtered out of the optimization-stream health
detectors (an always-rejected control must not read as a stall), and on the
gauntlet path the placebo runs as an extra scheduled duel that never advances
the champion pointer; on a multi-challenger field it is one extra slate slot
through the unchanged strategy + gate.

---

## 12. The overfitting program map

The threat model: the board is reused adaptively across rounds, so the
optimizer can Goodhart it — memorize entries instead of improving true
quality. `docs/design/OVERFITTING.md` is the survey; this table is the
program's shipped state and where each lever lives.

| # | Lever | Status / default | Home |
|---|---|---|---|
| 1 | Train/holdout split + holdout-gated promotion | SHIPPED, default-on; auto-degrades to empty holdout below `min_board_size_for_split` (6) or with `enabled=False`; `holdout_fraction` 0.3; explicit `holdout` tags always win | `src/zicato/board/split.py`, `tournament/gate.py` |
| 2 | Ladder/Thresholdout noisy budgeted holdout query | SHIPPED, default-on (no-op when holdout empty) | `src/zicato/tournament/ladder.py` (§5) |
| 3 | Restricted proposer visibility | SHIPPED, default-on (`restrict_proposer_visibility`) — patterns train-slice-only, per-entry identities aggregated to counts/rates, exact failing inputs withheld; plus the sanitized outcome-marginal channel | `patterns/`, `proposer/prompts.py`, `analyzer/outcome_marginals.py` |
| 3b | **Banding** (part of #3) | Δscalar in experiment memory coarsened to `improved` / `flat` / `regressed` buckets via `_bucket_scalar_delta` — never the exact number | `src/zicato/proposer/prompts.py` |
| 4 | Diff-complexity (parsimony/MDL) regularization | SHIPPED as an opt-in loss term (`diff_complexity_weight`, default 0.0, exactly absent when off); the complexity-*ceiling* half (reject oversized diffs in the gate) is **unbuilt** | `scoring/builtins.py::diff_complexity_component`, `scoring/diff_complexity.py` |
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
`tests/test_decision_procedure_power.py` (Tier 2 of the convergence harness).
Its design is the methodology; internalize the five pillars.

### 13.1 Seeded noise from stable identifiers only

The noise model is the target_0 example harness's own
(`examples/zicato_examples/target_0_convergence/harness.py`):
`stable_noise_seed` derives the RNG seed **only** from
`(workspace_seed, generation_id, entry_id, replicate_index)`. No wall clock,
no global RNG, no process ids, no tempdir names. Consequences:

- trials are exactly reproducible (the asserted "rates" are deterministic
  functions of the chosen seeds — *calibrated documentation*, not flaky
  statistics);
- trials vary by advancing the workspace seed; replicates vary by the stamped
  replicate index; sides vary because the generation id is in the seed (the
  A/A premise: identical trees under two ids draw independent noise);
- `test_noisy_session_seed_derives_only_from_stable_identifiers` pins each
  component independently: same coordinates ⇒ byte-identical run; any single
  component change ⇒ a fresh draw. A seeding regression in any component
  fails loudly.

### 13.2 Drive the real machinery; fake only the worker boundary

The statistical trials drive the REAL `run_matchup` (board-unit scheduling,
replicate averaging, the unchanged gate) and the REAL
`resolve_tournament`/`_confirm_gauntlet_promotion` (strategy + evidence loop),
monkeypatching exactly one seam — `runner._run_single`, the suite's documented
monkeypatch anchor — with `_NoisyWorld`, an in-process evaluator on the same
noise model, output synthesis, and real board predicates:

```python
# tests/test_decision_procedure_power.py — _NoisyWorld.install
    def install(self, monkeypatch: pytest.MonkeyPatch, *, persist: bool = False) -> None:
        monkeypatch.setattr(runner_mod, "_run_single", self._fake_run_single)
        monkeypatch.setattr(scheduling_mod, "_runtime_state", lambda: None)
        if not persist:
            monkeypatch.setattr(scheduling_mod, "_persist_unit_loss", lambda **_kw: None)
```

`persist=True` keeps the real per-unit cache persistence for the
slot-integrity tests that watch `loss.json` files on disk. One test at the
bottom (`test_noisy_adapter_seeded_draws_cross_the_worker_boundary`) drives
the actual `NoisyPolicyAdapter` through **real subprocess workers, twice**, to
prove the seeded draw crosses the process boundary intact (reproducible,
side-independent, replicate-independent) — so the in-process shortcut is
licensed by an end-to-end anchor.

### 13.3 A/A nulls first

Before any power claim, measure the null. The harness plants σ=0.22 and
derives the analytic floor (~0.663); `_measure_noise_floor` runs 60 seeded
A/A single-sample duels and asserts the measured sd lands in `[0.4, 1.0]` — a
floor of ~0 would mean the draws stopped varying (a seeding regression), a
wild floor would mean the noise model broke. Then the null is run through the
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
consumed*, so the comparison is between rules, not samples.

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
   comparison — the naive rule you are replacing must be shown hot on the
   same draws.
5. **If your change touches persistence or replicate indices**, add a
   slot-integrity test with `persist=True`: canonical r0 bytes unchanged,
   your draws present under your reserved base for every side (the
   `test_full_mode_evidence_loop_never_touches_canonical_slots` pattern).
6. **Re-run the whole power file and the convergence oracle** — your change
   must leave every existing pinned number standing, or the commit message
   must say exactly which number moved and why that is honest (the eb55266
   message updating "budget 48 → confirmed" expectations is the model).
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
   `LossProfile.unified_metrics()`).
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
   `promote_confidence_replicates` (an honest budget: the CI-separation cost
   is ~37 unbroken wins on a two-contestant field; the shipped racing example
   pairs budget 38 with per-duel `replicates: 32`). Setting the threshold
   without a budget gets you `DEFAULT_REPLICATE_BUDGET = 3` — sound, but a
   true improvement will usually terminate `inconclusive`.
2. Price it before running: each evidence replicate is a fresh
   2-sides × board sweep. The builder's cost meter line exists for exactly
   this (10-builder-cli-library.md §"Cost meter").
3. Expect and monitor the dead-letter queue
   (`runtime/inconclusive/*.json`) — an `inconclusive` terminal is a designed
   outcome, not an error; a *stream* of them means the budget cannot resolve
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
| `_TASK_FAILURE_RATIO_MULTIPLIER` | `10.0` (pinned, not a knob) | `scoring/builtins.py` |
| `DEFAULT_CALIBRATION_RUNS` | `5` | `tournament/calibration.py` |
| `CALIBRATION_REPLICATE_BASE` | `1000` | `tournament/calibration.py` |
| `PREFLIGHT_REPLICATE_BASE` | `2000` | `epoch/preflight.py` |
| `SCREEN_REPLICATE_BASE` (+1 confirm) | `3000` / `3001` | `epoch/screen.py` |
| `EVIDENCE_REPLICATE_BASE` | `4000` | `selection/evidence_gate.py` |
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
