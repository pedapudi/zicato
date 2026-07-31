# Loop health

This document specifies **loop-health diagnostics** — zicato's
first-class subsystem for detecting when the meta-loop is
*running but not actually optimising anything*.

The robustness layers in [ROBUSTNESS.md](ROBUSTNESS.md) make the
loop survive hangs, crashes, and pathological inner-harness code.
They answer "is the loop *broken*?". Loop health answers a
different and quieter question: "is the loop *meaningless*?". A
loop can be perfectly healthy by every robustness measure — no
hangs, no crashes, every round completing cleanly — and still be
producing zero optimisation signal. That failure mode is silent.
This subsystem makes it loud.

This document covers:

- The motivating incident: two generations scoring identically
  (§1).
- Loop health as a robustness concern — a toothless eval is a
  failure mode (§2).
- The detectors and their severities (§3): the six "running-but-
  meaningless" detectors plus the "running-but-fake-progress"
  `generalization_gap` detector and the `refresh_cadence` recommendation.
- The `LoopHealth` report (§4).
- The `zicato health` CLI (§5).
- How the orchestrator surfaces critical findings (§6).

## 1. The incident that motivated this

During an early dogfood run, an epoch produced this:

```
v0  scalar = 1.000000
v1  scalar = 1.000000
```

Both generations scored **exactly** `1.000000`. Not "close" —
identical to six decimal places. v1's patches had been applied,
the tournament had run the full board against both sides, the
gate had evaluated, v1 had been rejected on insufficient improvement,
and the round had been journaled. Every robustness layer was
satisfied. The loop reported itself healthy.

But a score of exactly `1.000000` on both sides is **degenerate**.
It does not mean "v1 was no better than v0". It means the
scoring produced no usable signal at all — every entry on the
board returned the same number for every generation, so the
tournament was comparing two identical scalars and the gate
margin could never be cleared by *any* candidate. The proposer
could run for a hundred rounds and never promote, not because its
patches were bad but because nothing it did could move the score.

This was discovered by an operator **manually eyeballing the
journal** and noticing the suspicious round number. Nothing in
zicato flagged it. The loop had been burning LLM budget proposing
and tournamenting for an evaluation that was structurally
incapable of distinguishing anything.

The lesson: **a loop that runs cleanly is not the same as a loop
that works.** zicato needs detectors for the
running-but-meaningless state, and it needs to surface them as
loudly as it surfaces a crash. Loop-health diagnostics is that
subsystem.

## 2. Loop health is a robustness concern

It is tempting to file loop health under "analytics" — a
nice-to-have dashboard panel. That is the wrong frame. A
degenerate evaluation is a **failure mode**, in exactly the sense
[ROBUSTNESS.md](ROBUSTNESS.md) uses the term: a state the system
can enter where it no longer does its job, which therefore needs
a detector and an operator-visible signal.

The robustness layers L1-L6 defend against the loop **breaking**.
Loop health defends against the loop being **toothless** — an
evaluation that cannot fail anything, cannot differentiate
anything, or has no ground truth to check against. A toothless
eval is worse than a broken one in one specific way: a broken
loop stops and the operator notices; a toothless loop *keeps
running*, consumes budget, fills the journal with rounds, and
produces a confident-looking lineage that means nothing.

So loop health is positioned alongside the six robustness layers,
not downstream of them:

| Concern | Question | Subsystem |
|---|---|---|
| Loop breaks (hang/crash/OOM) | Is the loop *broken*? | Robustness L1-L6 ([ROBUSTNESS.md](ROBUSTNESS.md)) |
| Loop is unproductive | Is the loop *wasting time*? | Circuit breaker L5 ([ROBUSTNESS.md §2.5](ROBUSTNESS.md#25-l5-consecutive-bad-circuit-breaker)) |
| **Loop is meaningless** | **Is the eval *toothless*?** | **Loop health (this document)** |

Loop health and the L5 circuit breaker are close cousins and
share signal, but they are distinct:

- **L5** fires on *unproductive* loops: K consecutive rejects.
  An unproductive loop may still have a perfectly good
  evaluation — the proposer is just not finding wins.
- **Loop health** fires on *meaningless* loops: the evaluation
  itself is degenerate. The proposer could be excellent and
  still never promote.

A loop can be unproductive without being meaningless (good eval,
bad proposals) and meaningless without being unproductive (a
degenerate eval that happens to promote on noise). The two
subsystems overlap — sustained degeneracy is also unproductive —
but they detect different root causes and an operator needs to
know which one they are looking at. Loop health feeds its
findings *into* the richer L5 signals
([ROBUSTNESS.md §2.5](ROBUSTNESS.md#25-l5-consecutive-bad-circuit-breaker)
notes the circuit breaker grows to consume "hypothesis match-rate
below 25%" and similar — those are loop-health detectors).

## 3. The detectors

Loop health runs a fixed set of detectors (in `health/diagnostics.py`)
over the epoch-so-far history — the per-generation `LossProfile`s, the
resolved `Experiment` records, and the epoch's board. Each detector is
a pure function returning a list of `HealthFinding`s; it either stays
silent or emits one or more findings, each with a stable `code` and a
fixed severity. The orchestrator runs them after a round and `zicato
health` runs them on demand.

Each detector's tuning knob is a field of `HealthConfig`, re-tunable
between runs via the `health` block of the workspace `config.json`
(parsed by `zicato.config.health_config_from_workspace`; the former
`ZICATO_HEALTH_*` env vars are deleted). The four knobs and their
defaults: `scoring_window` (3), `scoring_epsilon` (`1e-6`),
`no_expectations_fraction` (`0.5`), `stalled_rejects` (3).

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
detector emits one **`warning`** finding for that entry (one finding
per dead entry, not an aggregate). Entries that ran under only a single
generation are ignored — there is nothing yet to compare. The fix it
recommends: remove the entry or strengthen its expectation.

This detector is the per-entry version of degenerate scoring: it
localises *which* entries are the dead weight, so the operator knows
what to fix.

### 3.3 Flat drift signal — `flat_drift_signal`

**What it catches.** The drift telemetry — zicato's primary loss
signal — counted nothing at all across the epoch. Either the inner
harness genuinely produces no drift (possible but rare), or drift
detection is misconfigured / not wired, or the board's tasks are too
easy to provoke any drift.

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
alone — which is *valid* (see
[SCORING.md §1](SCORING.md#1-why-both-signals): drift loss works
without ground truth) but is also a common accident, where the
operator *meant* to attach expectations and forgot.

**Signal.** Computes the fraction of board entries whose `expectation`
is `None`. Fires when that fraction is *strictly greater than*
`no_expectations_fraction` (default `0.5`) — i.e. more than half the
board is drift-only. Severity is **`info`**, not `warning` —
drift-loss-only is a supported mode, so this is a *notice*, not an
alarm. Silent on an empty board.

### 3.5 Dead judge — `dead_judge`

**What it catches.** A board-declared in-run **process judge that never
fires** across the whole epoch. Each board entry's `judges` declares one or
more process judges; on a violation a judge emits a goldfive `custom` drift
the reducer attributes back as a `custom:<judge_name>` count on the run's
`loss.json` ([BOARD-FORMAT.md](BOARD-FORMAT.md) / [SCORING.md](SCORING.md)).
A judge whose attributed kind appears in *no* run is either mis-wired (it
keys on events that are never emitted) or its criterion is unreachable —
dead weight that gives a false sense of coverage. This is the "judge that
never fires" smell called out in `skills/zicato-design-judges` and failure
mode #3 in the board-audit playbook (`skills/zicato-audit-board`).

**Signal.** Collects the set of declared judge `name`s from the board and
the set of attributed judge names that fired across every run's
`drift_counts`. A declared judge that is absent from the fired set **and**
recorded no call failures is reported in one **`warning`** finding
(`detail.dead_judges` lists them). The inverse — a judge firing on every
run — is *not* a finding: loud is not dead, and a judge can be
legitimately always-on. Silent when no entry declares a judge
(drift-/expectation-only board) or no run has landed yet (nothing has had a
chance to fire).

### 3.5b Erroring judge — `judge_erroring`

**What it catches.** A board-declared judge whose callable **raised** rather
than deciding anything — a misconfigured judge model, a revoked key, an
endpoint outage. Emitted by the same detector as `dead_judge`, from the same
silence, because until issue #121 the two were indistinguishable: a judge
must never crash a run, so zicato's judge boundary and goldfive's steerer
both swallow the exception; goldfive emits no `JudgementEmitted` for the
resulting empty verdict; the reducer writes no `custom:<judge_name>` count.
The broken judge therefore read as "never fired" and routed the operator
into a board audit, while its missing drift made the generation's scalar
*better* than the evidence supports.

**Signal.** zicato's judge boundary counts invocations and errors per judge
name for the worker process
(`zicato.judge_runtime.error_register`); the worker stamps the snapshot onto
`LossProfile.judge_errors` at `loss.json`-write time (absent / empty on every
healthy run and on every profile written before the field existed). Any
declared judge with a non-zero error count across the epoch's runs is
reported in one **`warning`** finding whose `detail` carries
`erroring_judges`, per-judge `judge_error_counts`
(`invocations` / `errors` / `last_error_type`), and a recommendation pointing
at the endpoint and model config rather than the board. The orchestrator
lifts it onto the terminal the round it fires, like `dead_judge`.

**Deliberately not an abort.** A round where a judge errored on 100% of
invocations is, from the artifacts alone, indistinguishable from a transient
endpoint outage, and an outage never disqualifies a contract — so there is no
tolerance knob and nothing here stops or re-runs a round. Registered pending
live evidence of real error rates. A judge that HANGS rather than raises is
still a gap: goldfive's steerer bounds each `evaluate` with its own 30s
timeout and treats an overrun as "no signal" without calling back into
zicato, so it lands in the `dead_judge` bucket.

### 3.6 Stalled loop — `stalled_loop`

**What it catches.** The proposer is not finding improvements — a run
of consecutive rejected generations.

**Signal.** Scans the most-recent evaluated experiments and counts the
trailing run of `rejected` tournament decisions. When that run reaches
`stalled_rejects` (default 3), the detector emits one **`warning`**
finding. There is no conjunction with the other detectors in the
shipped code — the stall is reported on its own as the operator's cue
that the proposer is stuck and the brief or mutable surface may need
attention; it is also the L5 circuit breaker's territory
([ROBUSTNESS.md §2.5](ROBUSTNESS.md#25-l5-consecutive-bad-circuit-breaker)).
Silent until `stalled_rejects` evaluated experiments exist and the
trailing reject-run reaches the threshold.

### 3.7 Generalization gap — `generalization_gap`

**What it catches.** The "running-but-**fake-progress**" failure — the
counterpart to the other detectors' "running-but-meaningless." Where they
catch a *toothless* eval (no signal at all), this catches a *productive*
loop that is producing **fake** progress: the proposer is **memorizing the
board** rather than improving true quality
([OVERFITTING.md §6](OVERFITTING.md) / §12 #5). It depends on the
train/holdout board split ([OVERFITTING.md §3](OVERFITTING.md) / §12 #1):
the proposer optimizes against the *train* slice while the *holdout* slice
is touched only to confirm — so when the proposer overfits, the champion's
train loss keeps falling while its holdout loss stalls or rises.

**Signal.** Reads the per-generation `train_loss` / `holdout_loss` /
`generalization_gap` (`gap = holdout_loss - train_loss`) persisted on each
generation's tournament outcome. Over the generations that carry a measured
holdout, it compares the latest gap to the earliest. A gap that is flat or
*narrowing* is healthy (the holdout tracks train) and clears regardless of
magnitude. A gap that has **widened** fires:

- **`warning`** when `gap ≥ generalization_gap_warn` (default `0.05`);
- **`critical`** when `gap ≥ generalization_gap_crit` (default `0.15`),
  and the finding carries a **board-refresh recommendation**
  (`detail.refresh_recommended = true`) — the cue to refresh the contract:
  roll the epoch (rotating the holdout) per
  [OVERFITTING.md §7](OVERFITTING.md), the *overfitting* reason to retire a
  contract that complements the *diminishing-returns* reason in
  [SELECTION-THEORY.md §5](SELECTION-THEORY.md) (the optimal-stopping
  horizon).

Both thresholds are `HealthConfig` fields re-tunable via the workspace
`config.json`'s `health` block (`generalization_gap_warn` /
`generalization_gap_crit`). The detector **degrades
cleanly to no finding** when there is no holdout (small board, split
disabled — every generation's holdout loss is `null`) or fewer than two
generations carry a measured holdout. This is the safe, default-on degrade:
a board too small to split simply never trips it.

### 3.8 Board-refresh cadence — `refresh_cadence`

**What it catches.** A contract that has been mined for "long enough" even
without a visibly widening gap — across many generations even the *holdout*
can start to be overfit ([OVERFITTING.md §9](OVERFITTING.md)). This is the
cadence half of the refresh policy ([OVERFITTING.md §7](OVERFITTING.md) /
§12 #6).

**Signal.** When the operator sets
`OverfittingConfig.max_generations_per_contract` (a frozen contract field;
`None` by default = no ceiling), the detector emits one **`info`** finding
once the number of evaluated generations under the contract reaches that
ceiling, carrying `detail.refresh_recommended = true` and the same
roll-the-epoch recommendation as the `critical` `generalization_gap`. It is
a **recommendation, never a forced auto-roll**: the operator rolls (or an
explicitly-configured auto-stop acts). Silent when no ceiling is configured
or the contract has not yet reached it.

### 3.9 Severity summary

Each detector emits a *fixed* severity (the shipped detectors do not
escalate by severity tier — they either fire at their one severity or
stay silent):

| Detector `code` | Severity | Fires when |
|---|---|---|
| `degenerate_scoring` | `critical` | last `scoring_window` tournaments all have `\|Δscalar\| ≤ scoring_epsilon` |
| `non_differentiating_entry` | `warning` | a board entry's `drift_loss` is identical across every generation it ran under (one finding per such entry) |
| `flat_drift_signal` | `warning` | total `drift:`-namespace metric count is zero across all runs |
| `no_expectations` | `info` | fraction of entries without an expectation `> no_expectations_fraction` |
| `dead_judge` | `warning` | a board-declared in-run judge never fired (no `custom:<judge_name>` count) across any run in the epoch, and recorded no call failures |
| `judge_erroring` | `warning` | a board-declared in-run judge's callable RAISED on one or more invocations (`LossProfile.judge_errors`) — its zero drift is an error artifact, not a verdict |
| `stalled_loop` | `warning` | trailing run of `rejected` decisions reaches `stalled_rejects` |
| `generalization_gap` | `warning` / `critical` | the champion's `holdout_loss - train_loss` gap has *widened* past `generalization_gap_warn` / `_crit` (board memorization) |
| `refresh_cadence` | `info` | evaluated generations under the contract reach `max_generations_per_contract` |
| `placebo_promoted` | `critical` | a random-baseline placebo challenger (OVERFITTING.md #7) was PROMOTED — gate discrimination is broken |
| `preflight_signal_below_floor` | `critical` under `runtime.preflight_gate="refuse"`, else `warning` | the contract pre-flight verdict is `refuse` (the measured signal did not clear the measured A/A noise floor) |

Two more detectors — `margin_below_noise_floor` (`info` / `warning`), `infra_outage` / `token_budget_clip` / `tree_never_imported` / `on_promote_hook_failed` (`warning`) — shipped alongside the overfitting / pre-flight / infra-robustness programs after this table was first written; none of them emit `critical`, so they are omitted here for brevity. `health/diagnostics.py`'s `assess_loop_health` is the authoritative full detector list.

Severities mean:

- **`info`** — a notice. Recorded in the report; does not flip
  `LoopHealth.healthy` to `False` and never interrupts the loop.
- **`warning`** — something is degrading the loop's discriminating
  power but it is not yet meaningless. Recorded; flips `healthy` to
  `False`; the loop continues.
- **`critical`** — the loop is, or is about to become, meaningless.
  Surfaced as a loud orchestrator stderr WARNING (§6) and the only
  severity that counts toward the default-on early-stop (§6.2).

## 4. The `LoopHealth` report

The detectors' output is collected into a typed `LoopHealth`
report (`health/diagnostics.py`) — assessed per epoch, and produced on
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

The `LoopHealth` dataclass itself has no `round` or `overall` field —
it is keyed to an epoch, and the aggregate health bit is the boolean
`healthy` rather than a max-severity enum. Findings carry their fix
inside `detail.recommendation` where applicable rather than a dedicated
`remedy` field.

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

The shipped command has **no `--round`** flag (the report is per-epoch,
not per-round; `zicato health` recomputes the assessment live and does
not read back the orchestrator's per-round
`epochs/{epoch}/health/round_{N}.json` files) and **no `--format`**
flag — it always prints the colour-coded text rendering below.

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
| non-zero (`1`) | Report produced and at least one `critical` finding is present — the command raises `SystemExit(1)` so a CI / supervisor wrapper notices a degenerate eval. Only `critical` trips this; a `warning`-only report still exits `0`. |

Note this is narrower than a separate "degenerate" exit code: the
shipped command branches solely on the presence of a `critical`
finding, exiting `1` rather than a bespoke code. `degenerate_scoring`
and `placebo_promoted` are unconditionally `critical`;
`generalization_gap` and `preflight_signal_below_floor` escalate to
`critical` conditionally — the former past `generalization_gap_crit`,
the latter only under the hard `runtime.preflight_gate="refuse"` gate
(§3.9) — so any of the four can trip this exit code, not only
`degenerate_scoring`.

A CI wrapper that runs `zicato evolve` overnight pairs it with
`zicato health` so a degenerate epoch is caught the next morning
without an operator eyeballing the journal — the exact manual
step that the §1 incident depended on.

## 6. How the orchestrator surfaces critical findings

Loop health is computed inside the round loop, after the round's
experiment + journal entry are written. The orchestrator calls
`assess_loop_health` over the epoch's accumulated losses, experiments,
and board, persists the report to `epochs/{epoch}/health/round_{N}.json`,
and derives a `(summary, has_critical)` pair from it. Two surfacing
behaviours follow.

### 6.1 Loud warning on critical

When a round's `LoopHealth` carries any `critical` finding
(`has_critical` is true), the orchestrator emits a prominent
`WARNING`-level log line to stderr — it does not let the finding
scroll past as a normal log line:

```
LOOP HEALTH CRITICAL — epoch 2026-05-15_e1 round 7: CRITICAL: last 3
tournaments produced |Δscalar| ≤ 1e-06 — the loop is spinning with no
optimization signal. The evolve loop is producing no usable signal;
inspect the scoring weights / proposer brief before spending more LLM
calls.
```

The point is that the §1 failure mode can never again depend on an
operator happening to notice a suspicious number — the system says it
out loud. Non-critical findings (`warning` / `info`) are recorded in
the persisted report and reflected in the round's outcome summary but
do not trigger this stderr warning.

### 6.2 Early-stop on sustained critical health

Beyond the stderr warning, the orchestrator has a loop-health circuit
breaker. It counts *consecutive* rounds whose health came back
critical; when that run reaches `_DEGENERATE_HEALTH_STOP_THRESHOLD`
(shipped value: **2**), the loop stops cleanly between rounds with the
current epoch's state fully written, and the stop reason is recorded as
`degenerate_health`.

This early-stop is **on by default** — it is the `stop_on_degenerate_health`
parameter of the orchestrator's `run_evolve_loop`, defaulting to `True`.
There is **no `zicato evolve --stop-on-degenerate` CLI flag**; the
behaviour is the orchestrator default and is not toggled from the
command line. (The CLI's separate `--max-consecutive-rejections` flag,
default 3, is the *unproductive-loop* stop — it counts consecutive
tournament rejections, the L5 territory of
[ROBUSTNESS.md §2.5](ROBUSTNESS.md#25-l5-consecutive-bad-circuit-breaker)
— and is distinct from this *meaningless-loop* health stop.)

The threshold is deliberately tight at 2: a single critical round can
be a transient (one degenerate tournament), but two in a row means the
loop is genuinely producing no signal. A `no_expectations` finding is
only `info` and a `non_differentiating_entry` / `flat_drift_signal`
finding is only `warning`, so none of those alone trips the breaker —
only a `critical` finding counts toward the consecutive-critical run.
That is `degenerate_scoring` or `placebo_promoted` (always `critical`),
or `generalization_gap` / `preflight_signal_below_floor` when they
escalate (past `generalization_gap_crit`, or under the hard
`preflight_gate="refuse"` gate respectively) — see the severity
summary in §3.9.

## 7. Cross-references

| Topic | Document |
|---|---|
| The six robustness layers loop health sits beside | [ROBUSTNESS.md](ROBUSTNESS.md) |
| The L5 circuit breaker (unproductive loops) | [ROBUSTNESS.md §2.5](ROBUSTNESS.md#25-l5-consecutive-bad-circuit-breaker) |
| The score whose degeneracy §1 describes | [SCORING.md](SCORING.md) |
| Why drift-loss-only is a supported mode | [SCORING.md §1](SCORING.md#1-why-both-signals) |
| Board expectations the `no_expectations` detector counts | [BOARD-FORMAT.md](BOARD-FORMAT.md) |
| Round mechanics — where loop health is computed | [EPOCHS-AND-JOURNALING.md §8](EPOCHS-AND-JOURNALING.md#8-round-mechanics) |
| The loop-health dashboard panel | [DASHBOARD.md](DASHBOARD.md) |
| `zicato health` in the CLI reference | [CLI.md](CLI.md) |
| The analytical index that projects loop-health reports | [ANALYTICAL-INDEX.md](ANALYTICAL-INDEX.md) |
</content>
</invoke>
