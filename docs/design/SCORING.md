# Scoring

zicato scores each generation against a frozen board and uses the
score to drive tournament promotion. The score is a **weighted scalar**
with exactly two kinds of term:

- **One bounded correctness term** over board expectations: the fraction
  of entries whose outcome checks (`expectations` — `Predicate` /
  `Rubric`) missed. Only entries with a non-empty `expectations` list
  contribute.
- **A signed coefficient per measured metric channel.** Every measured
  signal — drift events, custom judges, run failures, runtime, cost,
  latency, rubric scores, output size, schema failures — is one
  namespace on `namespace_weights`, with no privileged channel among
  them. Channels work without ground-truth expectations.

They combine into a single tournament scalar. The combination is
**operator-tunable** at three levels of escalating power. A fixed
vocabulary of linear weights covers most contracts. A declarative
**transform registry** supplies non-linear shapes (`pow` / `harmonic` /
`cap` / `clip` / `log1p`) with no operator code. A dotted-spec **plugin**
escape hatch takes arbitrary pure logic through the same operator-owned,
contract-referenced mechanism `predicates.py` and `judges.py` already
use.

> **Shaping the score without a core edit.** The linear weights below
> are the neutral default. §11 specifies the transform registry and the
> plugin seams that reshape the score without a core edit, and §2.5
> covers the source-hashing that rolls the epoch when a plugin body
> changes (issue #19).

## 1. Why both signals

Drift loss alone scores "**how cleanly** did the run execute?". A
generation that produces fewer drift events generally has cleaner
runs — fewer plan revisions, fewer escalations, less looping.

Pass-rate alone scores "**did it do the right thing?**". A generation
that answers more questions correctly is better at its job
independent of how chaotic the path was.

Each on its own has a failure mode:

- **Drift only** misses the silent-success regression: the agent
  produces the wrong answer, smoothly, with no drift. Score goes up;
  quality went down.
- **Pass-rate only** misses the chaotic-success: the agent produces
  the right answer after seven plan revisions, three escalations, and
  burning through the budget. Score holds; quality went down.

Weighting both makes the system sensitive to both kinds of regression.
The operator gets a knob (the weights) to emphasize one over the
other when the project's priorities shift.

Correctness stays out of the channel map for three concrete reasons: it
runs on a different denominator (expectation-bearing entries rather than
every entry), it has its own monotonicity mechanism
(`pass_rate_monotonicity_scope`, §5), and the transform seam reads it as
a bounded coefficient (§11.1). Every *measured* signal is a channel.

Both components are **always computed when both are available**. The
operator opts into pass-rate by attaching expectations to board entries
— expectations are not required for the loop to function, but a
generation can only beat its parent on pass-rate if expectations
exist on the board.

## 2. The metric channels

Every measured signal enters the scalar as a **channel**: a namespace
prefix (`drift:`, `judge:`, `failure:`, `runtime:`, `cost:`, `latency:`,
`rubric:`, `output:`, `schema:`) carrying a signed coefficient in
`namespace_weights`. The sign is the channel's "worse" direction —
positive means higher is worse, negative means higher is better (rubric
scores grow with quality), zero excludes the channel from the scalar
while still tracking it.

Some channels also have a **within-channel shape**: a second map that
discriminates between members of the same channel before the channel
coefficient applies. `drift:` has `severity_weights` × `per_kind_weights`,
`judge:` has `per_judge_weights`, and `failure:` has
`task_failure_weight` / `not_completed_weight`.

### 2.1 The drift channel

For one entry the reducer reduces the run's drift EVENTS into a single
`drift_loss` from the fields of the `LossProfile` (see
[TELEMETRY.md](TELEMETRY.md)):

```
drift_loss[entry] =
      sum over (kind k, severity s), k not judge-attributed, of:
          per_kind_weights[k] * severity_weights[s] * drift_counts[k, s]
    + plan_revision_weight * plan_revisions
```

clamped at zero. Judge-attributed kinds (`custom` / `custom:<name>`) are
excluded here and scored in the `judge:` channel (§2.2) — charging them
in both would double-count. Plan revisions stay in this channel because
they are the same telemetry stream; an adapter that emits none
contributes zero. `per_kind_weights["custom"]` is rejected at
contract load: it would be structurally inert, and an operator who set
it would believe they had retuned their judges.

Where (defaults are the `ScoringWeights` field defaults):

| Symbol | Default | Meaning |
|---|---|---|
| `severity_weights[s]` | `info`: `1.0`, `warning`: `3.0`, `critical`: `10.0` | Per-severity multiplier (keys are lowercase). A missing key contributes `0.0`. |
| `per_kind_weights[k]` | empty mapping → uniform `1.0` weighting across kinds | Optional per-drift-kind multiplier, keyed on the lowercase drift-kind string (e.g. `confabulation_risk`). Stacks multiplicatively with `severity_weights`. |
| `per_judge_weights[j]` | empty mapping → falls back to `default_judge_weight` | Per-custom-judge multiplier, keyed on the judge `name`. See §2.2. |
| `default_judge_weight` | `1.0` | Fallback multiplier for a custom judge absent from `per_judge_weights`. |
| `plan_revision_weight` | `0.5` | Each plan-revision event is half a unit of loss. |
| `task_failure_weight` | `10.0` | Multiplier on `task_failure_ratio` in the `failure:` channel (§2.3). Pure failures matter. |
| `not_completed_weight` | `50.0` | What a run that did not complete costs in the `failure:` channel (§2.3). An absolute magnitude. |

The weights are stored in `scoring.json` per epoch. Keys are the
`ScoringWeights` field names; severity keys and drift-kind keys are
**lowercase**:

```json
{
  "pass_weight": 1.0,
  "severity_weights": {
    "info": 1.0,
    "warning": 3.0,
    "critical": 10.0
  },
  "per_kind_weights": {
    "confabulation_risk": 2.0,
    "looping_reasoning": 1.5
  },
  "per_judge_weights": {
    "cite-before-metric": 2.0,
    "ack-before-edit": 0.5
  },
  "default_judge_weight": 1.0,
  "plan_revision_weight": 0.5,
  "task_failure_weight": 10.0,
  "not_completed_weight": 50.0,
  "namespace_weights": {
    "drift:": 1.0,
    "judge:": 1.0,
    "failure:": 1.0,
    "runtime:": 0.0,
    "cost:": 0.001,
    "latency:": 0.0001,
    "rubric:": -1.0,
    "output:": 0.0,
    "schema:": 5.0
  },
  "promote_margin": 0.01,
  "pass_rate_monotonicity": true,
  "pass_rate_monotonicity_scope": "per_entry"
}
```

An adapter that declares the `goldfive` integration in its worker spec requires
a nested `goldfive` block for runtime measurement and steering behavior;
`"goldfive": {}` selects fixed defaults. The built-in Google ADK adapter
declares this capability. The offline check rejects a missing block for a
consuming adapter and an unused block for any other adapter. Generic contracts omit it.
Every configured field is serialized and hashed because changing a detector
threshold, built-in judge endpoint, steering policy, context rule, or
inner-agent limit can change a tournament result. See
[GOLDFIVE-CONFIG.md](GOLDFIVE-CONFIG.md) for the complete scaffold, defaults,
validation, and credential boundary.

`pass_rate_monotonicity_scope` is optional and defaults to `"per_entry"`;
set it to `"aggregate"` to gate on the overall pass-rate instead of every
individual entry (see the pass-rate monotonicity rule in §5).

Per-kind weight lookup uses `per_kind_weights[k]` if present and a
uniform `1.0` otherwise — there is no `_default` sentinel key. An empty
`per_kind_weights` mapping weights every kind uniformly; the operator
adds entries only to elevate or demote specific drift kinds.

An explicit `namespace_weights` mapping REPLACES the shipped defaults
rather than merging with them, and a namespace it omits scores at `0.0`.
One key is exempt from that freedom: `"failure:"` must be present and
strictly positive, and the loader rejects a contract that zeroes or
omits it (§2.3).

### 2.1.1 Why drift counts are weighted rather than reduced to pass/fail

A drift event is **information**. It says "the runtime saw this shape
of trouble during this run". Counting drift events as a loss term
captures the cleanness of the run independent of whether the final
answer was right. Counting only `pass_fail` would discard that signal.

The weights are part of the **evaluation contract** (they live in
`scoring.json`, which is frozen per epoch). Changing weights changes
the contract; if the operator wants different weights, they start a
new epoch.

### 2.2 The judge channel

A board's custom judges (`Judge.custom` / `Judge.python` — see
[BOARD-FORMAT.md](BOARD-FORMAT.md) §4) all emit the same drift kind,
`custom`, so they are their own channel rather than a slice of the drift
one. `namespace_weights["judge:"]` turns the whole channel up or down;
`per_judge_weights` is the within-channel shape when one judge's
violation should count for more than another's.

`per_judge_weights` is a mapping **keyed on the judge `name`** (the
first argument to `Judge.custom` / `Judge.python`, and the same
string carried as `judge_name` on the emitted drift). The reducer,
when it counts a `custom` drift, attributes it to its authoring judge
(folded into the drift kind as `custom:<judge_name>`) and looks up
`per_judge_weights[judge_name]`; if the judge has no entry there, it
falls back to `default_judge_weight` (default `1.0`).

In the `ScoringWeights` dataclass these are the `per_judge_weights` and
`default_judge_weight` fields. Like every other weight they are frozen
per epoch — changing them changes the evaluation contract and rolls the
epoch. `per_judge_weights` mirrors `per_kind_weights` one channel over:
`per_kind_weights` discriminates by drift kind within `drift:`,
`per_judge_weights` by `judge_name` within `judge:`.

The channel is DERIVED from `LossProfile.per_judge_loss` (a tuple of
`JudgeLoss` records the reducer writes): each entry becomes one
`judge:<judge_name>` metric carrying that judge's already-per-judge-
weighted loss, and the channel coefficient scales the sum. The two
gestures are therefore distinct and both available: retiring ONE judge
is `per_judge_weights: {name: 0.0}`, retiring the WHOLE channel is
`namespace_weights: {"judge:": 0.0}`.

Example: a board with two judges, `cite-before-metric` and
`ack-before-edit`, where a missing citation is a real defect but a
missing acknowledgement is a nicety:

```json
"per_judge_weights": {
  "cite-before-metric": 2.0,
  "ack-before-edit": 0.5
}
```

A third custom judge not listed here is weighted by
`default_judge_weight` (default `1.0`).

### 2.3 The failure channel

A run that crashed, was killed, or exhausted its wall-clock budget did
not produce a result. The expectation cannot fire and the drift counts
are incomplete; the reducer cannot tell whether the agent was about to
succeed or still flailing. Run outcome is a fact of every harness rather
than of any telemetry dialect, so it is its own channel with two
members:

```
failure[entry] =
      task_failure_weight   * task_failure_ratio
    + not_completed_weight  (only when the run did not complete)
```

`task_failure_ratio` is failed-to-started tasks in `[0, 1]`, floored to
`1.0` for a run that did not complete — a run that crashed before
emitting a single `task_failed` event still failed everything it
started. `not_completed` is a first-class `LossProfile` field, set for
ANY non-success terminal state (killed, crashed, harness exception,
emulator abort, budget exhausted). It is not derived from
`not_completed_reason`, which is legitimately absent when the adapter
supplied no reason; reading that absence as "completed" would hand a
crashed run the best possible score.

`not_completed_weight` is an ABSOLUTE magnitude rather than a multiple
of `max(severity_weights)`. Keying it to the severity scale would let a
retune of severities silently rescale what every abort costs, so a
contract that scales severities scales `failure:` explicitly to keep the
same proportion.

**The channel coefficient must be positive.** The loader rejects
`namespace_weights["failure:"] <= 0`, and rejects omitting the key from
an explicit mapping, naming the invariant: a contract must not be able
to make crashing free. Without that rule a challenger that fails fast
scores best of all — and the rule has to be structural, because the
failure mode is silent (the scalar simply stops seeing aborts). Dampen
the channel with a small positive coefficient instead of zeroing it.

### 2.4 The runtime channel, and the adapter-supplied ones

`runtime:seconds` is the run's wall-clock duration, derived from
`LossProfile.runtime_ms`. Its default coefficient is `0.0`: operators
usually rely on the per-entry wall-clock budget as a hard ceiling rather
than scoring duration continuously, but the channel is there when
duration matters intrinsically.

It is separate from `latency:`, whose default coefficient
is calibrated for adapter-supplied millisecond percentiles. Members of
one channel are SUMMED before the coefficient applies, and a sum of
whole-run seconds and per-turn millisecond percentiles is not a
quantity.

The remaining channels — `cost:`, `latency:`, `rubric:`, `output:`,
`schema:` — carry whatever the adapter reports as `MetricCount` entries
under those prefixes, plus the first-class scalars the reducer always
emits (`cost:llm_calls`, `cost:tokens_spent`, `output:chars`,
`schema:failures`). An unrecognised namespace in the data is aggregated
at coefficient `0.0` and surfaced for visibility.

### 2.5 Scoring config is part of the frozen contract

Every `ScoringWeights` field — the linear weights, the gate knobs, the
declarative transforms (§11.1), and the plugin specs (§11.2) — folds
into the **frozen per-epoch contract hash** through the
field-enumerating canonicalizer (`zicato/epoch/contract.py`). Changing
any of them rolls the epoch: the next `evolve` closes the current epoch
and opens a fresh one, so a scoring change is never silently applied to
an in-flight epoch's already-scored generations.

For the dotted-spec plugins this goes one step further than a plain
field. The canonicalizer hashes the **plugin spec string AND the
resolved module's source** (`spec_with_source_hash`). Editing a plugin's
*body*, and not only its dotted reference, is therefore detected as a
contract change and rolls the epoch. This is the **single
source-hashing mechanism shared across every grading plugin**
(predicates, judges, the outcome summarizer, and the scoring `scalar_fn`
/ `drift_reducer`): a body edit anywhere on the operator's grading
surface rolls the contract consistently. The declarative transforms
need no source hashing — they serialize natively as dicts and are
covered by the field canonicalizer for free.

## 3. Per-entry pass-rate

For an entry with a non-empty `expectations` list, the reducer
evaluates every outcome check against the run result and ANDs the
results into `pass_fail: bool` — the entry passes iff every
expectation passes (advisory `Rubric`s, `threshold=None`, always
pass). For an entry with an empty `expectations` list, `pass_fail` is
`None` and the entry does not contribute to the pass-rate
denominator.

The outcome-check kinds and their evaluation are specified in
[BOARD-FORMAT.md](BOARD-FORMAT.md) §3.

Note the division of labour: an entry's **process** checks (`judges`,
[BOARD-FORMAT.md](BOARD-FORMAT.md) §4) never touch pass-rate. A
violated judge emits a `DriftKind.CUSTOM` drift, which the reducer
attributes to its authoring judge and scores in the `judge:` channel
(§2.2). Pass-rate is purely the outcome-check side; the channels are
where every process signal lands.

## 4. Per-generation aggregate score

Given a generation's loss profiles for all N board entries, the live
path is `aggregate_generation_score` in `tournament/scoring.py`. It
mechanically aggregates the per-run profiles (means, namespace rollups)
and then synthesises the scalar through **Seam 2**, the scoring
dispatcher `zicato.scoring.dispatch.resolve_scalar`. That dispatcher
composes the built-in formula (`zicato.scoring.builtins.builtin_scalar`)
with any declarative `pass_transform` and dotted-spec `scalar_fn` plugin
(§11). The built-in formula is:

```
observed  = count of entries i whose pass_fail is not None
passes    = count of those entries with pass_fail == True
pass_rate = passes / observed   (or 1.0 when observed == 0)

channel[ns] = namespace_weights[ns] * mean over entries of that
              namespace's within-channel-weighted total

scalar = pass_weight * (1.0 - mean_score)
       + sum over SORTED namespaces ns of channel[ns]
       + diff_complexity term (when configured; see OVERFITTING.md §12)
```

The namespace sum is **sorted**. Float addition is not associative, and
the namespace key set is assembled from a `set`, so an unsorted sum
would make the scalar's last bit depend on hash seeding — reproducible
within one process and different in the next.

The pass term runs on the uniform continuous `mean_score` axis (issue
#18). On an all-bool board `mean_score == pass_rate`, so the term is
byte-identical to `1 - pass_rate`. A separate
`telemetry/scoring.py::combined_scalar` helper computes a two-axis
PROJECTION over drift and pass alone, for callers that hold nothing
else; the live scalar is the dispatcher path above.

Notes:

- Each channel is the **arithmetic mean** across the generation's runs
  (one run per board entry). The shipped aggregator does not re-weight
  by `BoardEntry.weight` — the per-entry `weight` field exists on the
  board but the shipped aggregator means uniformly; an empty generation
  means `0.0`.
- `pass_rate` counts only runs whose `pass_fail` is not `None`
  (entries with an expectation). If no run has an expectation,
  `pass_rate` is `1.0` by convention (no failures observed) so the
  `pass_weight` term contributes 0 to the scalar.
- Every term is oriented so **lower is better**: `1.0 - pass_rate` is
  in `[0, 1]`, larger when more entries failed, and each channel's
  coefficient sign points its measurement in the loss direction.
- `scalar` is smaller-is-better. It is non-negative unless a contract
  configures a negative coefficient large enough to dominate (the
  `rubric:` default is negative by design).

The terms are **additive** rather than multiplicative, so an epoch can zero out
one axis — `pass_weight = 0` to ignore expectations entirely, or
`namespace_weights: {"drift:": 0.0}` to stop scoring drift — without
obliterating the others. Zeroing `drift:` does not silence judges,
task failures, or the crash charge: each is its own channel.

`scalar_components` decomposes the result for display and the gate:
`"pass"` plus one entry per namespace (keyed by the colon-stripped
name), written in sorted namespace order and summing exactly to
`scalar`.

`zicato tournament run v3 v4` prints the tournament result — both
generations' aggregates and the gate verdict — as a JSON object.

### 4.1 Default weights and the calibration problem

The default weights in `scoring.json` are a *starting point* rather than
a final answer. The right weights depend on:

- Which drift kinds the operator cares about most for *this* system
  under test.
- How costly an abort really is (does it always mean failure, or
  sometimes the agent was on the right track and just slow?).
- Whether pass-rate or cleanness dominates the operator's notion
  of "better".

The honest position is that **good defaults are unknown until the
loop has run real epochs**. The recommended workflow:

1. Start with the shipped channel coefficients (`drift:` and `pass` both
   at `1.0`) and the per-kind / severity weights from §2.
2. Run an epoch.
3. Inspect the journal: do the promoted generations align with what
   the operator would have promoted by eye?
4. If the scalar's decisions disagree with the operator's
   intuition, tune `scoring.json`, start a new epoch, repeat.

The first few epochs after registering a new system under test are
expected to be calibration epochs. The journal makes the disagreement
visible (a promoted generation whose `core_idea` the operator would
have rejected → drift weights are off; a rejected generation whose
patches the operator agrees with → pass-rate weight is too high).

## 5. The tournament promotion gate

The training-slice promotion gate (`evaluate_gate` in `tournament/gate.py`)
decides whether the candidate (child) is eligible to replace the parent. It
applies three rules **in order** and returns a provisional promotion only if
none rejects. A separate hidden-holdout confirmation can revise that
provisional result before settlement. `evaluate_gate` returns a `GateOutcome`
with fields:

| `GateOutcome` field | Meaning |
|---|---|
| `decision` | `"promoted"` or `"rejected"`. (`"deferred"` is in the `TournamentDecision` literal but `evaluate_gate` itself only returns the first two — deferral is a runner-level concept.) |
| `reason` | Human-readable explanation; empty string when promoted, otherwise names the rule that fired (and, for pass-rate / namespace regressions, the entries / namespaces). |
| `delta_scalar` | `child.scalar - parent.scalar`. Negative = improvement. |
| `delta_pass_rate` | `child.pass_rate - parent.pass_rate`. Positive = improvement. |

**Rule 1 — scalar margin.** The combined scalar is "lower is better",
so a promotion needs the child's loss to drop by at least
`promote_margin`. The literal reject condition is:

```
reject iff child.scalar > parent.scalar - promote_margin
```

The rejection reason distinguishes two cases: a child whose loss *rose*
(`delta_scalar > 0`) is rejected with a `"challenger regressed: ..."`
reason; a child that improved but by less than `promote_margin` is
rejected with an `"insufficient improvement: ..."` reason. Both reasons
state the real child-minus-parent delta and cite `promote_margin` as
the threshold.

**Rule 2 — pass-rate monotonicity** (only when
`pass_rate_monotonicity` is `true`, the default). The *granularity* is
selected by `pass_rate_monotonicity_scope` (`"per_entry"` | `"aggregate"`,
default `"per_entry"`):

- **`per_entry`** (the default) — for every entry where the parent
  recorded `pass_fail == True`, the child MUST also record
  `pass_fail == True`. A child whose `pass_fail` comes back `False` *or*
  `None` (the expectation did not evaluate, or the entry did not run) on
  an entry the parent passed is a regression. If any such entry
  regressed the gate rejects with `"pass-rate regression on entries:
  <id>, <id>, ..."` (every regressing entry id, sorted). Entries the
  parent failed or had no expectation for are not gated on this rule.
- **`aggregate`** — reject only when the child's *overall* pass-rate falls
  below the parent's by more than a tiny float-noise tolerance
  (`PASS_RATE_MONOTONICITY_TOLERANCE`). The child may trade *which*
  entries pass as long as the net pass-rate holds or improves. The reject
  reason reports the overall rate: `"pass-rate regression: overall
  pass-rate fell by <Δ> (champion <p> -> challenger <c>)"`.

There is no `"off"` scope value — disable the rule entirely with
`pass_rate_monotonicity: false`. The same scope is applied to the
holdout-confirmation step, so the train and holdout slices use one
consistent policy.

**Choosing a scope.** `per_entry` is the right policy when *every* board
entry is a must-not-regress invariant — a regression suite where any flip
is a real breakage. `aggregate` is the right policy for *sampled
evaluation boards*, where each entry is one noisy sample of a capability.
There, individual pass/fail is subject to run-to-run nondeterminism
(sampling, retrieval ties, timeouts). A strictly-better challenger should
not be permanently vetoed by a single entry flip when the aggregate, the
quantity the operator optimizes, improved or held. Under `per_entry`,
the champion's exact passing *set* becomes a frozen invariant and any
nondeterministic entry turns into a ratchet that no amount of aggregate
improvement can overcome; `aggregate` trades that ratchet for a net-rate
guard.

**Rule 3 — per-namespace monotonicity.** For each namespace whose flag in `namespace_monotonicity` is
`true`, the child's weighted per-namespace aggregate may not have moved
in the namespace's "worse" direction (the sign of the `namespace_weights`
coefficient already folds direction into the aggregate). A regression
rejects with `"monotonicity_regression on namespace=<ns>, ..."`. Zero-
weight namespaces are skipped. The shipped defaults guard `rubric:` and
`schema:` and leave the rest unguarded, including `drift:`: a proposer
may trade some drift movement for gains elsewhere. The rule applies to
every channel uniformly, `judge:` and `failure:` included — and it
widens the gate very little in practice, because non-aborting
generations all tie at `failure: = 0.0`.

If no rule rejects, the gate returns `decision="promoted"`.

**Crowning holdout confirmation.** A tournament structure first selects its
leader and runs the three training rules. When the resulting crowning duel
would promote, zicato compares the champion and challenger on the hidden
holdout slice. The challenger confirms when its holdout loss and pass behavior
do not meaningfully regress. It need not improve again on the smaller holdout.

The Ladder governor controls whether the holdout result may revise the
training verdict. A released non-confirmation changes the result to rejected.
A released confirmation preserves promotion. A withheld result or exhausted
query budget leaves the training verdict unchanged. The word *query* counts
one statistical consultation of the holdout, regardless of how many board
entries or model calls the comparison requires. See
[`OVERFITTING.md`](OVERFITTING.md#what-query-budget-means) for the feedback and
budget rules.

The per-namespace "did this namespace move the wrong way" test is
`tournament.gate.regressed_namespaces`, and it is public because it is
asked outside the gate too: the gate-rule view renders it, and the Pareto
frontier record ([`PARETO-FRONTIER.md`](PARETO-FRONTIER.md)) uses it as
admission control. There is exactly one implementation, so the record and
the gate cannot disagree about what a regression is.

A weighted sum is a projection, so a challenger that wins on an
under-weighted axis loses the scalar and is dropped. That candidate is now
*recorded* — never re-decided — beside the champion lineage; see
[`PARETO-FRONTIER.md`](PARETO-FRONTIER.md). The record reads the same
axes, signs, units, and `promote_margin` this document defines, and adds
no knob of its own.

### 5.1 Why strict monotonicity on pass-rate

Pass-rate regressions are *categorical* failures: an entry that
passed before now fails. There is no "small" regression on a passing
entry — either the right answer is still produced or it is not. Drift
loss is *graded*: messier vs cleaner, on a continuum.

Treating the two differently captures the asymmetry. A candidate
that reduces drift but breaks a passing entry is almost always worse
than the parent (the loop's whole point is to reduce drift WITHOUT
breaking what worked).

The strict gate is also a guard against the proposer over-fitting to
drift patterns at the expense of correctness — a tightened prompt
might reduce CONFABULATION_RISK by also refusing to attempt the
question, which would tank pass-rate. The gate catches this.

The per-entry-versus-aggregate granularity is operator-selectable via
`pass_rate_monotonicity_scope` (the pass-rate monotonicity rule above).
The per-namespace monotonicity rule is aggregate-scoped already: it
compares per-namespace *means* rather than per-entry pass/fail, so the
same scope field does not apply there. The analogous knob for namespaces
would be "all tracked namespaces combined versus each individually", a
different axis the operator already controls by choosing which
namespaces to flag in `namespace_monotonicity`. A combined-axis
namespace scope is a **documented follow-up**, kept out of the pass-rate
scope field so that two distinct concepts do not share one field.

### 5.2 Why a tolerance band on drift

LLM-driven runs are noisy. The same generation against the same
entry produces somewhat different drift counts across reruns. The
promote margin is the operator's stated noise floor: "I only
believe an improvement if the candidate beats the parent by more than
this."

`promote_margin` defaults to `0.01` in `ScoringWeights`. A
`promote_margin` of `0.01` means the candidate must lower its combined
`scalar` by at least 0.01 to be promoted. Operators raise it for noisy
boards where small deltas are likely spurious.

There is no separate `drift_tolerance_band` parameter in the shipped
`ScoringWeights` — the single `promote_margin` threshold absorbs
run-to-run noise on the combined scalar. Multi-trial scoring with
confidence intervals remains a roadmap item (§9).

## 6. Worked example

A small board with 3 entries; v3 → v4 candidate proposal.

Using the shipped defaults (`namespace_weights["drift:"] = 1.0`,
`pass_weight = 1.0`, `promote_margin = 0.01`) and the unweighted mean.
Neither generation aborted and neither board carries custom judges, so
the `judge:` and `failure:` channels contribute zero throughout and the
scalar reduces to the two terms shown.

**v3 (parent):**

| Entry | `pass_fail` | `drift_loss` |
|---|---|---|
| `short_solar` | True | 0.4 |
| `long_solar` | True | 1.2 |
| `contradictory` | False | 2.5 |

```
drift_loss_mean = (0.4 + 1.2 + 2.5) / 3 = 4.1 / 3 = 1.367

observed = 3 (every entry has an expectation)
passes   = 2  (short_solar, long_solar)
pass_rate = 2 / 3 = 0.667

scalar = 1.0 * 1.367 + 1.0 * (1.0 - 0.667) = 1.367 + 0.333 = 1.700
```

**v4 (candidate):**

| Entry | `pass_fail` | `drift_loss` |
|---|---|---|
| `short_solar` | True | 0.3 |
| `long_solar` | True | 0.9 |
| `contradictory` | True | 1.8 |

```
drift_loss_mean = (0.3 + 0.9 + 1.8) / 3 = 3.0 / 3 = 1.000
pass_rate = 3 / 3 = 1.0
scalar = 1.0 * 1.000 + 1.0 * 0.0 = 1.000
```

**Tournament gate:**

- Rule 1 (scalar margin): `child.scalar = 1.000`,
  `parent.scalar - promote_margin = 1.700 - 0.01 = 1.690`. Since
  `1.000 > 1.690` is false, the rule does NOT reject. `delta_scalar =
  1.000 - 1.700 = -0.700` (an improvement). ✓
- Rule 2 (pass-rate monotonicity): parent passes were `{short_solar,
  long_solar}`; candidate passes those plus `contradictory`. No entry
  the parent passed regressed. ✓
- Rule 3 (per-namespace monotonicity): with default weights `drift:` is
  unguarded, `judge:` / `failure:` are both `0.0` on each side, and
  `rubric:` / `schema:` aggregates are absent here. ✓

**Decision: `promoted`.** v4 becomes the new parent.

If instead v4's `contradictory` came back `pass_fail = True` but
`long_solar` flipped to `False` — even with substantially lower drift —
the pass-rate monotonicity rule fires and the gate returns
`decision="rejected"` with reason
`"pass-rate regression on entries: long_solar"`.

## 7. Fast mode and the tournament

`zicato evolve --mode fast` skips the parent-vs-candidate A/B run on
the same board. Instead, it accepts or rejects the candidate against
the parent's **historical** score on the same board.

The fast-mode gate runs the same `evaluate_gate` rules (§5), but the
parent side comes from its cached historical aggregate rather than a
fresh run:

```
fast_promote iff (child.scalar <= parent.historical_scalar - promote_margin)
             AND no pass-rate regression observed on the candidate's run
             AND no guarded-namespace regression

(a stronger promote_margin is advisable in fast mode — historical
parent scores carry their own staleness noise)
```

Fast mode is less rigorous: the world may have drifted (LLM provider
updated; rate-limit shape changed; a tool dependency upgraded
silently) between when the parent was scored and when the candidate
is being scored. The parent's historical score is then not a fair
comparison.

In exchange, fast mode is **much faster**: one board run instead of
two. The `--mode` flag selects between them, and the two CLI entry
points ship with *different* defaults. `zicato evolve --mode` defaults
to **fast**, because the loop favours iteration speed and re-scores the
champion only when no cache exists. The standalone `zicato tournament
--mode` defaults to **full**, an explicit one-off re-score of a specific
pair. See [TOURNAMENT.md](TOURNAMENT.md) for the CLI detail.

## 8. Stamping outcomes onto the experiment

Once the tournament gate has decided, the tournament runner appends
the `outcome` block to the candidate's `experiment.json` (see
[EPOCHS-AND-JOURNALING.md §3.3](EPOCHS-AND-JOURNALING.md#33-outcome-written-after-the-run)).

The `drift_loss_delta`, `pass_rate_delta`, `scalar_score_delta`, and
`tournament_decision` fields in `outcome` (an `OutcomeRecord`) are
populated from the gate's computation. The `rejection_reason` field
(empty string when promoted) carries the gate's `reason`, one of:

- `""` (promoted)
- `"insufficient improvement: ..."` (scalar margin; the child improved but by less than `promote_margin`)
- `"challenger regressed: ..."` (scalar margin; the child's loss rose)
- `"pass-rate regression on entries: <id>, ..."` (pass-rate monotonicity)
- `"monotonicity_regression on namespace=<ns>, ..."` (per-namespace monotonicity)

This is the single audit trail for why a candidate was or was not
promoted.

## 9. Limits and caveats

Three things scoring does not do:

- **Confidence intervals over the replicates.** An entry runs
  `tournament.params["replicates"]` times per generation, which defaults
  to 2 for every structure except racing, and the per-entry losses are
  averaged before aggregation. Scoring does not carry an interval around
  that average; the conservative `promote_margin` stands in for one.
- **Cost-aware scoring.** Token counts and per-call cost are carried
  (`LossProfile.tokens_spent` surfaces under the `cost:` namespace),
  and a cost-aware penalty needs no core edit: a `scalar_fn` plugin
  reading `ctx.namespace_aggregates["cost:"]` expresses it (§11.5).
  Folding cost into the *default* scalar shape remains a roadmap item,
  required by the goldfive steering target (see
  [DOGFOOD-TARGETS.md](DOGFOOD-TARGETS.md)).
- **Operator pinning.** "This entry must pass" and "the score must
  improve on this tag slice" are not available as hard gates. The
  proposer brief's `## Forbidden` list covers the mutation side of
  pinning; the scoring side is implicit through pass-rate
  monotonicity.

These are roadmap items rather than contract failures. The shipped score
is intentionally narrow.

## 11. Pluggable scoring — transforms and plugins

The linear weights above are the neutral default; they cannot express a
non-linear *shape* (a quadratic recall curve, a diminishing-returns
aggregation, a cap, a cost-aware blend). Scoring therefore carries the
**operator-owned, contract-referenced plugin** treatment `predicates.py`
and `judges.py` already have, at two **seams** (issue #19):

```
per-run events ──(Seam 1: drift_reducer)──▶ per-run drift_loss   # reducer.py
        │  (aggregate: means, namespace rollups — mechanical)
        ▼
per-gen aggregates ──(Seam 2: scalar_fn)──▶ per-gen scalar       # tournament/scoring.py
        ▼
(parent, child) ──(gate)──▶ decision                             # gate.py (§5)
```

Both seams are reshaped by a **hybrid**: a declarative transform
registry for the common cases (no operator code, serializable) plus a
dotted-spec plugin escape hatch for arbitrary logic. Every layer is
**neutral by default** — absent any transform or plugin config, scoring
is byte-identical to §4.

### 11.1 Declarative transform registry

`zicato.scoring.transforms` ships a handful of named, pure, parameterized
shapes, each a single `{"op": "<name>", ...params}` spec:

| `op` | params | shape |
|---|---|---|
| `linear` | — | identity (the neutral default) |
| `pow` | `exponent` | `x ** exponent` |
| `harmonic` | — | `1 + 1/2 + … + 1/n` (diminishing returns; the opt-in `looping_reasoning` curve) |
| `cap` | `max` | `min(x, max)` |
| `clip` | `lo`, `hi` | clamp to `[lo, hi]` (requires `lo <= hi`) |
| `log1p` | — | `log(1 + x)` |

Two `ScoringWeights` slots take a transform:

```json
"pass_transform":  { "op": "pow", "exponent": 2.0 },
"drift_kind_aggregation": {
    "looping_reasoning": { "op": "harmonic" },
    "off_topic":         { "op": "cap", "max": 5 }
}
```

- **`pass_transform`** (Seam 2) reshapes the scalar's pass/miss term
  `(1 - mean_score)`. `{"op":"pow","exponent":2.0}` gives quadratic
  recall. An absent spec, or `linear`, gives the plain linear miss
  term.
- **`drift_kind_aggregation`** (Seam 1) reshapes, per drift KIND, how
  that kind's *count* aggregates into the drift loss
  (`severity × kind_weight × transform(count)` in place of
  `… × count`). An absent kind entry is `linear`, the built-in shape.
  `{"looping_reasoning":{"op":"harmonic"}}` opts THIS contract — and no
  other — into the harmonic curve.

A single `op` per slot — no pipelines (arbitrary multi-step logic is a
plugin). Specs are **validated fail-fast at contract load**
(`ScoringWeights.__post_init__` → `validate_transform_spec`): an unknown
op, a missing / non-finite / non-numeric param, a typo'd param name, or a
`clip` with `lo > hi` is rejected loudly. `apply_transform` is therefore
total at scoring time and never produces a `NaN` mid-run. Both slots serialize
natively and fold into the contract hash (§2.5).

### 11.2 Dotted-spec plugins (the escape hatch)

For anything the registry cannot express (an F-beta recall/precision
blend, a cost-aware penalty reading `ctx.namespace_aggregates["cost:"]`),
two optional dotted specs on the contract, resolved by the **same
importer** predicates / judges use:

```json
"drift_reducer": "mypkg.contract.scoring:my_drift_reducer",   // Seam 1
"scalar_fn":     "mypkg.contract.scoring:my_scalar"           // Seam 2
```

Each is a **pure, deterministic, NO-LLM, no-I/O, no-wall-clock** function
over a read-only typed **frozen context** (`zicato.scoring.api`:
`DriftContext` / `ScalarContext`). Each context carries the **built-in /
post-transform value** (`builtin_loss` / `builtin_scalar`), so a plugin
*wraps/adjusts* the declarative shape rather than reimplementing the
formula:

```python
def my_drift_reducer(ctx: DriftContext) -> float:
    loop = sum(c.count for c in ctx.drift_counts if c.kind == "looping_reasoning")
    base = ctx.builtin_loss - _linear_looping(ctx)
    return base + sum(1.0 / k for k in range(1, int(loop) + 1))
```

The composition order at each seam is **built-in → transform → plugin**:
the plugin sees the post-transform value as its `builtin_*`. Because
scoring is pure, re-scoring an epoch is reproducible — unlike judges,
there is no evaluation callable to pass.

**Fail-open semantics.** A plugin that raises, returns `NaN`/`inf`, or
fails to resolve must NOT crash the run. Mirroring `evaluate_judges`, the
dispatcher wraps the call in try/except, logs at WARNING, and **falls
back to the pre-plugin (transformed-or-builtin) value** — and records the
fallback in the provenance (§11.4) so a silently degraded plugin is
visible rather than buried in a log.

**Proposer immutability.** Scoring plugins live in the operator package
(`mypkg/contract/scoring.py`), in the same way as predicates and judges, and are
**never** enumerated as mutation points — the proposer cannot rewrite
the operator's grading. A guard/test keeps the mutation walker
off them.

**Worker ↔ orchestrator parity.** Seam 1 (`drift_reducer`) runs INSIDE
the killable worker subprocess; Seam 2 (`scalar_fn`) in the orchestrator.
Both resolve through one shared importer, and `drift_reducer` (like
`drift_kind_aggregation`) crosses the worker `_weights_spec` boundary so
the worker and orchestrator never disagree on which plugin is active.

### 11.3 Seam architecture (`zicato/scoring/`)

| module | role |
|---|---|
| `api.py` | the frozen typed contexts (`DriftContext` / `ScalarContext`) + the `ScoringProvenance` token type |
| `builtins.py` | the extracted default formulas (`builtin_drift_loss` / `builtin_scalar`) — the value every layer starts from |
| `transforms.py` | the declarative registry (`linear`/`pow`/`harmonic`/`cap`/`clip`/`log1p`) + `validate_transform_spec` |
| `plugins.py` | dotted-spec resolution, source-hashing (`spec_with_source_hash`), and the fail-open `apply_drift_reducer` / `apply_scalar_fn` |
| `dispatch.py` | the single seam the live paths call (`resolve_drift_loss` / `resolve_scalar`) — composes built-in → transform → plugin and emits the provenance |

`reducer.py` (Seam 1) and `tournament/scoring.py` (Seam 2) do not
inline their formulas: each builds the typed context and hands it to the
matching dispatcher.

### 11.4 Scoring provenance

To make a scalar **explainable without reading code**, each dispatcher
returns a parseable provenance token alongside the value. It is recorded
on the per-run `loss.json` (`LossProfile.scoring_provenance`, Seam 1) and
the per-generation aggregate (`scalar_provenance` in `gen_score.json`,
Seam 2), and surfaced in the dashboard's promote-gate breakdown as a
per-side **scalar decomposition** (which transform / plugin produced the
pass term + each channel). Token shapes:

| token | meaning |
|---|---|
| `builtin` | the default formula produced it; a record written before scoring provenance existed carries `None` |
| `transform:pass=pow(2.0)` | Seam-2 pass transform |
| `transform:drift{looping_reasoning=harmonic, off_topic=cap(5)}` | Seam-1 per-kind drift transforms |
| `plugin:scalar_fn=<spec>` / `plugin:drift_reducer=<spec>` | a dotted plugin produced it |
| `<pre-plugin token> (fallback: <reason>)` | **FAIL-OPEN** — a fired plugin failed and fell back to the pre-plugin value |

The fail-open form is surfaced **prominently** (caution-colored) in the
dashboard so a degraded plugin is obvious, never silent.

### 11.5 Common shapes and how a contract expresses them

| shape | contract expression |
|---|---|
| quadratic recall | `"pass_transform": {"op":"pow","exponent":2.0}` |
| harmonic looping | `"drift_kind_aggregation": {"looping_reasoning":{"op":"harmonic"}}` — opt-in, and scoped to this contract |
| F-beta blend | a `scalar_fn` plugin |
| cost-aware penalty | a `scalar_fn` plugin reading `ctx.namespace_aggregates["cost:"]` |

## 10. Cross-references

| Topic | Document |
|---|---|
| `LossProfile` fields and how they are computed | [TELEMETRY.md](TELEMETRY.md) |
| `BoardEntry.weight`, `expectations`, and `judges` | [BOARD-FORMAT.md](BOARD-FORMAT.md) |
| Authoring outcome/process checks and `per_judge_weights` | [BOARD-AUTHORING.md](BOARD-AUTHORING.md) |
| `tournament_decision` field on `experiment.json` | [EPOCHS-AND-JOURNALING.md](EPOCHS-AND-JOURNALING.md) |
| The non-drift loss model of the goldfive steering target | [DOGFOOD-TARGETS.md](DOGFOOD-TARGETS.md) |
| Why the score uses drift loss and pass-rate rather than free-text scoring | [RATIONALE.md](RATIONALE.md) |
