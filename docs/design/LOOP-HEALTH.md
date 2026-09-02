# Loop health

Loop-health diagnostics is zicato's subsystem for detecting when the
meta-loop runs without optimising anything.

The robustness layers in [ROBUSTNESS.md](ROBUSTNESS.md) make the loop
survive hangs, crashes, and pathological inner-harness code. They
answer whether the loop is broken. Loop health answers a quieter
question: whether the loop is meaningless. A loop can pass every
robustness measure — no hangs, no crashes, every round completing
cleanly — and still produce no optimisation signal at all. That failure
mode is silent, and this subsystem makes it visible.

Below: the run that motivated the subsystem (§1); why an evaluation
that cannot distinguish anything counts as a robustness failure (§2);
the detectors and their severities (§3); the `LoopHealth` report (§4);
the `zicato health` command (§5); and how the orchestrator surfaces
critical findings (§6).

## 1. The incident that motivated this

During an early dogfood run, an epoch produced this:

```
v0  scalar = 1.000000
v1  scalar = 1.000000
```

Both generations scored exactly `1.000000` — identical to six decimal
places. The patches for `v1` had been applied, the tournament had run
the full board against both sides, the gate had evaluated, `v1` had
been rejected on insufficient improvement, and the round had been
journaled. Every robustness layer was satisfied. The loop reported
itself healthy.

A score of `1.000000` on both sides is degenerate. It does not mean
that `v1` was no better than `v0`. It means the scoring produced no
usable signal: every entry on the board returned the same number for
every generation, so the tournament compared two identical scalars and
no candidate could ever clear the gate margin. The proposer could run
for a hundred rounds and never promote — the cause being that nothing
it did could move the score, rather than that its patches were bad.

An operator found this by reading the journal and noticing the
suspicious number. Nothing in zicato flagged it. The loop had been
spending model budget on proposals and tournaments for an evaluation
structurally incapable of distinguishing anything.

A loop that runs cleanly is therefore not the same as a loop that
works. zicato needs detectors for the running-but-meaningless state,
and it needs to surface them as loudly as it surfaces a crash.
Loop-health diagnostics is that subsystem.

## 2. Loop health is a robustness concern

Loop health is not an analytics dashboard panel. A degenerate
evaluation is a failure mode in the sense
[ROBUSTNESS.md](ROBUSTNESS.md) uses the term: a state the system can
enter where it stops doing its job, and which therefore needs a
detector and an operator-visible signal.

The six robustness layers defend against the loop breaking. Loop health
defends against an evaluation that cannot fail anything, cannot
differentiate anything, or has no ground truth to check against — a
toothless evaluation. A toothless evaluation is worse than a broken
loop in one specific way. A broken loop stops and the operator notices.
A toothless loop keeps running, consumes budget, fills the journal with
rounds, and produces a confident-looking lineage that means nothing.

Loop health therefore sits alongside the six robustness layers rather
than downstream of them:

| Concern | Question | Subsystem |
|---|---|---|
| Loop breaks (hang, crash, out-of-memory kill) | Is the loop *broken*? | the six robustness layers ([ROBUSTNESS.md](ROBUSTNESS.md)) |
| Loop is unproductive | Is the loop *wasting time*? | the consecutive-bad circuit breaker ([ROBUSTNESS.md §2.5](ROBUSTNESS.md#25-the-consecutive-bad-circuit-breaker)) |
| **Loop is meaningless** | **Is the evaluation *toothless*?** | **Loop health (this document)** |

Loop health and the consecutive-bad circuit breaker share signal and
remain distinct:

- **The circuit breaker** fires on an unproductive loop: K consecutive
  rejects. An unproductive loop may still have a sound evaluation, with
  the proposer simply not finding wins.
- **Loop health** fires on a meaningless loop, where the evaluation
  itself is degenerate. The proposer could be excellent and still never
  promote.

A loop can be unproductive without being meaningless, with a sound
evaluation and poor proposals. It can be meaningless without being
unproductive, when a degenerate evaluation happens to promote on noise.
The two subsystems overlap, since sustained degeneracy is also
unproductive, but they detect different root causes and an operator
needs to know which one they are looking at. Loop health feeds its
findings into the circuit breaker's unbuilt richer signals: the
patterns named at
[ROBUSTNESS.md §2.5](ROBUSTNESS.md#25-the-consecutive-bad-circuit-breaker),
such as a hypothesis match-rate below 25 percent, are themselves
loop-health detectors.

## 3. The detectors

Loop health runs a fixed set of detectors (in `health/diagnostics.py`)
over the epoch-so-far history — the per-generation `LossProfile`s, the
resolved `Experiment` records, and the epoch's board. Each detector is
a pure function returning a list of `HealthFinding`s; it either stays
silent or emits one or more findings, each with a stable `code` and a
fixed severity. The orchestrator runs them after a round and `zicato
health` runs them on demand.

Each detector's tuning knob is a field of `HealthConfig`, re-tunable
between runs through the `health` block of the workspace `config.json`
and parsed by `zicato.config.health_config_from_workspace`. No
environment variable configures a detector. The knobs the detectors
below rely on, with their defaults, are `scoring_window` (3),
`scoring_epsilon` (`1e-6`), `no_expectations_fraction` (`0.5`),
`stalled_rejects` (3), `generalization_gap_warn` (`0.05`), and
`generalization_gap_crit` (`0.15`).

### 3.1 Degenerate scoring — `degenerate_scoring`

**What it catches.** The motivating incident (§1): tournaments produce
no usable score signal, round after round, because the scoring is not
distinguishing the candidates from their parents.

**Signal.** Looks at the most-recent `scoring_window` (default 3)
experiments that carry a tournament outcome. When *every* one of them
has `|scalar_score_delta|` below `scoring_epsilon` (default `1e-6`),
the loop is spinning on a flat loss surface. Severity is always
**`critical`** — a degenerate scorer wastes every round's wall-clock.
The detector is silent until at least `scoring_window` evaluated
experiments exist and silent if any tournament in the window showed a
real delta.

### 3.2 Non-differentiating board entry — `non_differentiating_entry`

**What it catches.** Individual board entries whose `drift_loss` never
moves across the generations they ran under. Such an entry contributes
a constant to every generation's score and therefore *cannot* move a
tournament — it is a dead test.

**Signal.** For each `entry_id`, collect its `drift_loss` across every
generation it ran under. When the entry ran under two or more
generations and produced an *identical* `drift_loss` every time, the
detector emits one **`warning`** finding for that entry, one finding
per dead entry rather than a single aggregate. Entries that ran under
only a single generation are ignored, since there is nothing yet to
compare. The recommended fix is to remove the entry or strengthen its
expectation.

This detector is the per-entry version of degenerate scoring: it
localises which entries are inert, so the operator knows what to fix.

### 3.3 Flat drift signal — `flat_drift_signal`

**What it catches.** The drift telemetry — zicato's primary loss
signal — counted nothing at all across the epoch. Three causes are
possible: the inner harness produces no drift, which happens but is
rare; drift detection is misconfigured or unwired; or the board's tasks
are too easy to provoke any drift.

**Signal.** Walks every run's unified metric view and sums the `count`
of every metric in the `drift:` namespace. When that total is exactly
zero across all runs in the epoch, the drift side of the loss is inert.
Severity is **`warning`**: the loop can still optimise on the pass/fail
side, but half the loss surface is dead. Silent when there are no runs
to assess.

### 3.4 No expectations — `no_expectations`

**What it catches.** Most of the board carries no expectation, so
`pass_fail` is `None` for most runs and the pass-rate side of scoring
contributes almost nothing. The loop is then running on drift loss
alone. That is a supported mode, because drift loss works without
ground truth (see [SCORING.md §1](SCORING.md#1-why-both-signals)), and
it is also a common accident in which the operator intended to attach
expectations and did not.

**Signal.** Computes the fraction of board entries whose `expectation`
is `None`. Fires when that fraction is strictly greater than
`no_expectations_fraction` (default `0.5`), meaning more than half the
board is drift-only. Severity is **`info`** rather than `warning`,
because a drift-loss-only board is supported; the finding is a notice.
Silent on an empty board.

**Also reported before the first round.** The fraction is a static
property of the board file, so the pre-spend workspace gate
(`src/zicato/check/validators.py`) reports it too, as an advisory that
names the ungraded entries and never blocks a run. Both surfaces read one
rule, `zicato.board.expectation_coverage.measure_expectation_coverage`,
including the `no_expectations_fraction` threshold, so a board reads the
same way at both moments and retuning the threshold moves both.

### 3.5 Dead judge — `dead_judge`

**What it catches.** A board-declared in-run **process judge that never
fires** across the whole epoch. Each board entry's `judges` declares one or
more process judges; on a violation a judge emits a goldfive `custom` drift
the reducer attributes back as a `custom:<judge_name>` count on the run's
`loss.json` ([BOARD-FORMAT.md](BOARD-FORMAT.md) / [SCORING.md](SCORING.md)).
A judge whose attributed kind appears in no run is either mis-wired, in
that it keys on events that are never emitted, or its criterion is
unreachable. Either way it contributes nothing while giving a false
sense of coverage. `skills/zicato-design-judges` names the same problem,
and the board-audit playbook (`skills/zicato-audit-board`) lists it as
its third failure mode.

**Signal.** Collects the set of declared judge `name`s from the board and
the set of attributed judge names that fired across every run's
`drift_counts`. A declared judge that is absent from the fired set **and**
recorded no call failures is reported in one **`warning`** finding, and
`detail.dead_judges` lists them. A judge that fires on every run
produces no finding, because a judge may legitimately be always-on. The
detector is silent when no entry declares a judge, which is the case for
a board using only drift and expectations, and when no run has landed
yet, because nothing has had a chance to fire.

### 3.5b Erroring judge — `judge_erroring`

**What it catches.** A board-declared judge whose callable **raised** rather
than deciding anything, whether from a misconfigured judge model, a
revoked key, or an endpoint outage. The same detector emits it as emits
`dead_judge`, because both arise from the same on-disk silence. A judge
must never crash a run, so zicato's judge boundary and goldfive's
steerer both swallow the exception; goldfive emits no
`JudgementEmitted` for the resulting empty verdict; and the reducer
writes no `custom:<judge_name>` count. Without the separate error
count, a broken judge reads as one that never fired, which routes the
operator into a board audit, and its missing drift makes the
generation's scalar better than the evidence supports (issue #121).

**Signal.** zicato's judge boundary counts invocations and errors per judge
name for the worker process
(`zicato.judge_runtime.error_register`), and the worker stamps that
snapshot onto `LossProfile.judge_errors` when it writes `loss.json`.
The field is absent or empty on a healthy run, and a reader must treat
an absent field as "no errors recorded". Any
declared judge with a non-zero error count across the epoch's runs is
reported in one **`warning`** finding whose `detail` carries
`erroring_judges`, per-judge `judge_error_counts`
(`invocations` / `errors` / `last_error_type`), and a recommendation pointing
at the endpoint and model config rather than the board. The orchestrator
lifts it onto the terminal the round it fires, like `dead_judge`.

**The finding never aborts a round.** From the artifacts alone, a round
where a judge errored on every invocation is indistinguishable from a
transient endpoint outage, and an outage never disqualifies a contract.
There is therefore no tolerance knob, and nothing here stops or re-runs
a round; whether one should is left open until live evidence of real
error rates exists. A judge that hangs rather than raises remains
uncovered: goldfive's steerer bounds each `evaluate` with its own
30-second timeout and treats an overrun as no signal without calling
back into zicato, so such a judge is reported as `dead_judge`.

### 3.6 Stalled loop — `stalled_loop`

**What it catches.** The proposer is not finding improvements — a run
of consecutive rejected generations.

**Signal.** Scans the most-recent evaluated experiments and counts the
trailing run of `rejected` tournament decisions. When that run reaches
`stalled_rejects` (default 3), the detector emits one **`warning`**
finding. The shipped code does not combine this detector with any
other. The stall is reported on its own, as the operator's cue that the
proposer is stuck and that the brief or the mutable surface may need
attention. It is also the territory of the consecutive-bad circuit
breaker
([ROBUSTNESS.md §2.5](ROBUSTNESS.md#25-the-consecutive-bad-circuit-breaker)).
Silent until `stalled_rejects` evaluated experiments exist and the
trailing reject-run reaches the threshold.

### 3.7 Generalization gap — `generalization_gap`

**What it catches.** Progress that is not real. The other detectors
catch an evaluation that produces no signal at all; this one catches a
productive-looking loop in which the proposer is memorizing the board
rather than improving true quality
([OVERFITTING.md §6](OVERFITTING.md), item 5 of its §12). It depends on
the train/holdout board split ([OVERFITTING.md §3](OVERFITTING.md),
item 1 of its §12). The proposer optimizes against the train slice
while the holdout slice is touched only to confirm. When the proposer
overfits, the champion's train loss keeps falling while its holdout
loss stalls or rises.

**Signal.** Reads the per-generation `train_loss` / `holdout_loss` /
`generalization_gap` (`gap = holdout_loss - train_loss`) persisted on each
generation's tournament outcome. Over the generations that carry a measured
holdout, it compares the latest gap to the earliest. A gap that is flat
or narrowing is healthy, because the holdout tracks the train slice,
and it clears regardless of magnitude. A gap that has widened fires:

- **`warning`** when `gap ≥ generalization_gap_warn` (default `0.05`);
- **`critical`** when `gap ≥ generalization_gap_crit` (default `0.15`),
  and the finding carries a board-refresh recommendation
  (`detail.refresh_recommended = true`). That recommendation is the cue
  to roll the epoch, which rotates the holdout, per
  [OVERFITTING.md §7](OVERFITTING.md). Overfitting is one reason to
  retire a contract; diminishing returns is the other, described as the
  optimal-stopping horizon in
  [SELECTION-THEORY.md §5](SELECTION-THEORY.md).

Both thresholds are `HealthConfig` fields, re-tunable through the
`health` block of the workspace `config.json` as
`generalization_gap_warn` and `generalization_gap_crit`. The detector
produces no finding when there is no holdout — on a small board, or
with the split disabled, every generation's holdout loss is `null` — or
when fewer than two generations carry a measured holdout. A board too
small to split therefore never trips it.

### 3.8 Board-refresh cadence — `refresh_cadence`

**What it catches.** A contract that has been optimized against for
many generations, even without a visibly widening gap. Across enough
generations the holdout itself starts to be overfit
([OVERFITTING.md §9](OVERFITTING.md)). This detector is the cadence half
of the refresh policy ([OVERFITTING.md §7](OVERFITTING.md), item 6 of
its §12).

**Signal.** `OverfittingConfig.max_generations_per_contract` is a
frozen contract field, `None` by default, which imposes no ceiling.
When the operator sets it, the detector emits one **`info`** finding
once the number of evaluated generations under the contract reaches
that ceiling. The finding carries `detail.refresh_recommended = true`
and the same
roll-the-epoch recommendation as a `critical` `generalization_gap`
finding. The finding is a recommendation and never forces a roll: the
operator rolls the epoch, or a configured auto-stop acts. Silent when
no ceiling is configured, and when the contract has not yet reached
it.

### 3.9 Severity summary

A rule fixes the severity each detector emits. Most detectors fire at
one severity or stay silent; `generalization_gap` and
`preflight_signal_below_floor` are the two that choose between
`warning` and `critical`.

| Detector `code` | Severity | Fires when |
|---|---|---|
| `degenerate_scoring` | `critical` | last `scoring_window` tournaments all have `\|Δscalar\| ≤ scoring_epsilon` |
| `non_differentiating_entry` | `warning` | a board entry's `drift_loss` is identical across every generation it ran under (one finding per such entry) |
| `flat_drift_signal` | `warning` | total `drift:`-namespace metric count is zero across all runs |
| `no_expectations` | `info` | fraction of entries without an expectation `> no_expectations_fraction` |
| `dead_judge` | `warning` | a board-declared in-run judge never fired (no `custom:<judge_name>` count) across any run in the epoch, and recorded no call failures |
| `judge_erroring` | `warning` | a board-declared in-run judge's callable raised on one or more invocations (`LossProfile.judge_errors`), so its zero drift is an artifact of the error rather than a verdict |
| `stalled_loop` | `warning` | trailing run of `rejected` decisions reaches `stalled_rejects` |
| `generalization_gap` | `warning` / `critical` | the champion's `holdout_loss - train_loss` gap has *widened* past `generalization_gap_warn` / `_crit` (board memorization) |
| `refresh_cadence` | `info` | evaluated generations under the contract reach `max_generations_per_contract` |
| `placebo_promoted` | `critical` | a random-baseline placebo challenger (item 7 of [OVERFITTING.md](OVERFITTING.md) §12) was promoted, which means the gate has stopped discriminating |
| `preflight_signal_below_floor` | `critical` under `runtime.preflight_gate="refuse"`, else `warning` | the contract pre-flight verdict is `refuse` (the measured signal did not clear the measured A/A noise floor) |

Further detectors ship without appearing in the table above:
`margin_below_noise_floor` at `info` or `warning`, and `infra_outage`,
`token_budget_clip`, `tree_never_imported`, and
`on_promote_hook_failed` at `warning`. None of them emits `critical`.
`assess_loop_health` in `health/diagnostics.py` is the authoritative
full detector list.

The three severities mean:

- **`info`** — a notice. Recorded in the report; it does not flip
  `LoopHealth.healthy` to `False` and never interrupts the loop.
- **`warning`** — something is degrading the loop's discriminating
  power without yet making it meaningless. Recorded; flips `healthy` to
  `False`; the loop continues.
- **`critical`** — the loop is meaningless, or about to become so.
  Surfaced as a prominent orchestrator warning on standard error (§6),
  and the only severity that counts toward the default-on early-stop
  (§6.2).

## 4. The `LoopHealth` report

The detectors' output is collected into a typed `LoopHealth` report
(`health/diagnostics.py`). It is assessed per epoch and produced on
demand by `zicato health`.

```json
{
  "epoch_id": "2026-05-15_e1",
  "healthy": false,
  "checked_at": "2026-05-15T14:22:00Z",
  "findings": [
    {
      "code": "degenerate_scoring",
      "severity": "critical",
      "summary": "last 3 tournaments produced |Δscalar| ≤ 1e-06 — the loop is spinning with no optimization signal",
      "detail": {
        "window": 3,
        "epsilon": 1e-06,
        "generation_ids": ["v5", "v6", "v7"],
        "scalar_score_deltas": [0.0, 0.0, 0.0]
      }
    },
    {
      "code": "non_differentiating_entry",
      "severity": "warning",
      "summary": "board entry 'short_solar' scored an identical drift_loss (1) across all 8 generations it ran under — a dead test",
      "detail": {
        "entry_id": "short_solar",
        "drift_loss": 1.0,
        "generation_ids": ["v0", "v1", "v2", "v3", "v4", "v5", "v6", "v7"],
        "recommendation": "remove the entry or strengthen its expectation so it can differentiate generations"
      }
    },
    {
      "code": "no_expectations",
      "severity": "info",
      "summary": "8/10 board entries (80%) have no expectation — the pass/fail side of the loss is mostly absent",
      "detail": {
        "entries_without_expectation": 8,
        "total_entries": 10,
        "fraction": 0.8,
        "threshold": 0.5,
        "entry_ids_without_expectation": ["..."]
      }
    }
  ]
}
```

Fields:

| Field | Meaning |
|---|---|
| `epoch_id` | The epoch this report describes. |
| `healthy` | `True` iff no finding has `warning` or `critical` severity. Purely-`info` findings do not flip it to `False`. |
| `checked_at` | ISO-8601 UTC timestamp of when the assessment ran. |
| `findings` | Every `HealthFinding` produced by every detector, in detector order (`degenerate_scoring`, `non_differentiating_entry`, `flat_drift_signal`, `no_expectations`, `stalled_loop`, `generalization_gap`, `refresh_cadence`). A detector may emit more than one finding (`non_differentiating_entry` emits one per dead entry). |
| `findings[].code` | The detector's stable symbolic identifier (§3). |
| `findings[].severity` | `info` / `warning` / `critical`. |
| `findings[].summary` | One-line human-readable rendering for terminal output. |
| `findings[].detail` | Structured specifics — entry ids, generation ids, the numbers that tripped the detector, and (where the detector offers one) a `recommendation`. JSON-friendly so the report round-trips. |

The `LoopHealth` dataclass has no `round` or `overall` field. It is
keyed to an epoch, and the aggregate health signal is the boolean
`healthy` rather than a maximum-severity enumeration. A finding carries
its fix inside `detail.recommendation` where it has one, and there is
no dedicated `remedy` field.

When the orchestrator runs the assessment each round it persists the
report to `epochs/{epoch}/health/round_{N}.json` (the per-round JSON
the orchestrator writes adds the `round` it was computed at as an
envelope). `zicato health` recomputes the assessment live rather than
reading these files back.

## 5. The `zicato health` CLI

`zicato health` runs the detectors against an epoch's full history and
prints the `LoopHealth` report.

```
zicato health [--workspace <dir>] [--epoch <id>]
```

- With no flags, assesses the workspace's current epoch (read from the
  `current_epoch` marker) and prints the report.
- `--epoch <id>` targets a specific epoch instead of the current one.
- `--workspace <dir>` points at a non-default workspace root (default
  `.zicato`).

The shipped command has no `--round` flag, because the report covers a
whole epoch: `zicato health` recomputes the assessment live and does
not read back the orchestrator's per-round
`epochs/{epoch}/health/round_{N}.json` files. It has no `--format`
flag either, and always prints the colour-coded text rendering below.

Text output (colour-coded by severity; the `detail` keys are printed
indented under each finding's summary):

```
$ zicato health
Loop health for epoch '2026-05-15_e1'
checked_at: 2026-05-15T14:22:00Z
UNHEALTHY — one or more findings need attention.

3 finding(s):
  [CRITICAL] degenerate_scoring: last 3 tournaments produced |Δscalar| ≤ 1e-06 — the loop is spinning with no optimization signal
      epsilon: 1e-06
      generation_ids: ['v5', 'v6', 'v7']
      scalar_score_deltas: [0.0, 0.0, 0.0]
      window: 3
  [WARNING] non_differentiating_entry: board entry 'short_solar' scored an identical drift_loss (1) across all 8 generations it ran under — a dead test
      drift_loss: 1.0
      entry_id: short_solar
      generation_ids: ['v0', 'v1', '...']
      recommendation: remove the entry or strengthen its expectation so it can differentiate generations
  [INFO] no_expectations: 8/10 board entries (80%) have no expectation — the pass/fail side of the loss is mostly absent
      ...
```

When every detector stays silent the command prints the `HEALTHY` line
and "No findings — every detector stayed silent."

Exit codes:

| Code | When |
|---|---|
| `0` | Report produced and no `critical` finding is present (including a report that has only `warning` / `info` findings). |
| non-zero (`1`) | Report produced and at least one `critical` finding is present. The command raises `SystemExit(1)` so a continuous-integration or supervisor wrapper notices a degenerate evaluation. Only `critical` trips this; a report holding only `warning` findings still exits `0`. |

There is no separate "degenerate" exit code. The command branches
solely on the presence of a `critical` finding and exits `1`. Four
detectors can therefore trip that exit code.
`degenerate_scoring` and `placebo_promoted` are always `critical`.
`generalization_gap` reaches `critical` past
`generalization_gap_crit`, and `preflight_signal_below_floor` reaches
it only under the hard `runtime.preflight_gate="refuse"` gate (§3.9).

A continuous-integration wrapper that runs `zicato evolve` overnight
pairs it with `zicato health`, so a degenerate epoch is caught the next
morning without the manual journal reading that §1 describes.

## 6. How the orchestrator surfaces critical findings

Loop health is computed inside the round loop, after the round's
experiment record and journal entry are written. The orchestrator calls
`assess_loop_health` over the epoch's accumulated losses, experiments,
and board, persists the report to `epochs/{epoch}/health/round_{N}.json`,
and derives a `(summary, has_critical)` pair from it. Two surfacing
behaviours follow.

### 6.1 Loud warning on critical

When a round's `LoopHealth` carries any `critical` finding, so that
`has_critical` is true, the orchestrator emits a prominent
`WARNING`-level line to standard error rather than letting the finding
scroll past among ordinary log lines:

```
LOOP HEALTH CRITICAL — epoch 2026-05-15_e1 round 7: CRITICAL: last 3
tournaments produced |Δscalar| ≤ 1e-06 — the loop is spinning with no
optimization signal. The evolve loop is producing no usable signal;
inspect the scoring weights / proposer brief before spending more LLM
calls.
```

Detecting the failure mode described in §1 therefore does not depend
on an operator happening to notice a suspicious number. Findings at
`warning` and `info` severity are recorded in the persisted report and
reflected in the round's outcome summary, and do not trigger this
standard-error warning.

### 6.2 Early-stop on sustained critical health

Beyond the stderr warning, the orchestrator has a loop-health circuit
breaker. It counts *consecutive* rounds whose health came back
critical; when that run reaches `_DEGENERATE_HEALTH_STOP_THRESHOLD`
(shipped value: **2**), the loop stops cleanly between rounds with the
current epoch's state fully written, and the stop reason is recorded as
`degenerate_health`.

This early-stop is on by default: it is the
`stop_on_degenerate_health` parameter of the orchestrator's
`run_evolve_loop`, which defaults to `True`. There is no `zicato evolve
--stop-on-degenerate` command-line flag; the behaviour is the
orchestrator default and cannot be toggled from the command line. The
separate `--max-consecutive-rejections` flag, default 3, is the
unproductive-loop stop. It counts consecutive tournament rejections and
belongs to the consecutive-bad circuit breaker
([ROBUSTNESS.md §2.5](ROBUSTNESS.md#25-the-consecutive-bad-circuit-breaker)),
which is a different stop from this meaningless-loop health stop.

The threshold is set tight at 2. A single critical round can be a
transient, such as one degenerate tournament, while two in a row mean
the loop is producing no signal. Only a `critical` finding counts
toward the consecutive-critical run, so a `no_expectations` finding at
`info`, or a `non_differentiating_entry` or `flat_drift_signal` finding
at `warning`, never trips the breaker on its own. The findings that do
count are `degenerate_scoring` and `placebo_promoted`, which are always
`critical`, and `generalization_gap` and
`preflight_signal_below_floor` when they escalate — past
`generalization_gap_crit`, or under the hard `preflight_gate="refuse"`
gate respectively. The severity summary in §3.9 lists them.

## 7. Cross-references

| Topic | Document |
|---|---|
| The six robustness layers loop health sits beside | [ROBUSTNESS.md](ROBUSTNESS.md) |
| The consecutive-bad circuit breaker (unproductive loops) | [ROBUSTNESS.md §2.5](ROBUSTNESS.md#25-the-consecutive-bad-circuit-breaker) |
| The score whose degeneracy §1 describes | [SCORING.md](SCORING.md) |
| Why drift-loss-only is a supported mode | [SCORING.md §1](SCORING.md#1-why-both-signals) |
| Board expectations the `no_expectations` detector counts | [BOARD-FORMAT.md](BOARD-FORMAT.md) |
| Round mechanics — where loop health is computed | [EPOCHS-AND-JOURNALING.md §8](EPOCHS-AND-JOURNALING.md#8-round-mechanics) |
| The loop-health dashboard panel | [DASHBOARD.md](DASHBOARD.md) |
| `zicato health` in the CLI reference | [CLI.md](CLI.md) |
| The analytical index that projects loop-health reports | [ANALYTICAL-INDEX.md](ANALYTICAL-INDEX.md) |
