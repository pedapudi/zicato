---
name: zicato-diagnose-health
description: Run zicato health and interpret it — detect a toothless / degenerate evolve loop (one that runs cleanly but optimises nothing), read the detectors and severities, branch on the exit code (critical ⇒ exit 1), and prescribe the contract fix. Use whenever an epoch promotes nothing, scores look suspiciously identical, or you want to confirm the loop has real optimization signal before trusting a tournament.
---

# Diagnose loop health

A zicato loop can be perfectly healthy by every robustness measure — no
hangs, no crashes, every round completing — and still produce **zero
optimization signal**. That failure mode is silent: the loop keeps running,
burns budget, and fills the journal with meaningless rounds. `zicato health`
makes it loud. Full spec:
[../../docs/design/LOOP-HEALTH.md](../../docs/design/LOOP-HEALTH.md).

The motivating incident: an epoch where `v0` and `v1` both scored exactly
`1.000000` — found only by an operator eyeballing the journal. Loop health
exists so that never depends on a human noticing a suspicious number again.

## Run it

```sh
.venv/bin/zicato health --workspace .zicato      # current epoch, latest round
.venv/bin/zicato health --epoch <epoch_id>       # target a non-current epoch
```

Real flags only: `zicato health` exposes `--workspace` and `--epoch`. (There
is no `--round` / `--format json` — the design doc now documents their
absence too. If you need the raw report, read
`.zicato/epochs/{epoch}/health/round_{N}.json` directly.)

It prints one line per firing detector — `[SEVERITY] detector_code:` a
one-sentence summary, then its `detail` keys (including a `recommendation`).
With no findings the loop has signal.

## The detectors the CLI runs

Thresholds come from the workspace `config.json`'s `health` block
(`HealthConfig`, `config.py`); defaults in parentheses. The pre-flight /
noise-floor / mutated-tree-import findings below are sourced from the same
persisted workspace records the orchestrator's per-round assessment reads —
`zicato.health.inputs` (`epoch_preflight_record`, `epoch_noise_floor_inputs`,
`epoch_tree_import_gaps`, `workspace_preflight_gate`) — so the CLI and a live
round now see the identical finding set for anything already written to disk.

| Detector | Severity | Fires when |
|---|---|---|
| `degenerate_scoring` | `critical` | every one of the last `scoring_window` (3) evaluated tournaments had `abs(scalar_score_delta)` at or below `scoring_epsilon` (1e-6) — the loop is spinning on a flat loss surface |
| `non_differentiating_entry` | `warning`, one per entry | a board entry ran under ≥2 generations and produced an *identical* `drift_loss` every time — a dead test |
| `flat_drift_signal` | `warning` | zero `drift:`-namespace metric counts across every run in the epoch — the drift half of the loss is inert (goldfive drift detection likely unwired) |
| `no_expectations` | `info` | more than `no_expectations_fraction` (0.5) of board entries carry no expectation — the pass/fail half is mostly absent |
| `dead_judge` | `warning` | a board-declared judge's `custom:<name>` drift never appears in ANY run of the epoch AND it recorded no call failures — 0-fire dead weight, not coverage |
| `judge_erroring` | `warning` | a board-declared judge's callable RAISED (`LossProfile.judge_errors` counts invocations/errors/last type) — the same silence as `dead_judge`, but the fix is the named `judge` engine (or inherited `evaluation` engine), NOT the board. Its missing drift made the round's scalar better than the evidence supports |
| `stalled_loop` | `warning` | `stalled_rejects` (3) consecutive generations were `rejected` — the proposer isn't finding improvements; the L5 breaker is about to or has fired |
| `generalization_gap` | `warning` / `critical` | the champion's `holdout_loss - train_loss` **widened** since the first measured generation AND reached `generalization_gap_warn` (0.05) / `_crit` (0.15) — board memorization; critical recommends rolling the epoch |
| `refresh_cadence` | `info` | evaluated generations reached `overfitting.max_generations_per_contract` (unset by default) — the contract has been mined enough |
| `placebo_promoted` | `critical` | a random-baseline placebo challenger was **promoted** — a no-op won a tournament, so gate discrimination is broken and recent wins are suspect |
| `margin_below_noise_floor` | `info` gate ON / `warning` gate OFF | `promote_margin` sits inside the measured A/A noise floor |
| `preflight_signal_below_floor` | `critical` **only** under `runtime.preflight_gate="refuse"`, else `warning` | pre-flight verdict `refuse` — the measured signal is at/below the noise floor. The one pre-flight finding that can hard-stop a run, because it is the one measured honestly. Gate-aware on purpose: this re-fires from the persisted record every round, and two criticals in a row would stop a run the operator explicitly set to `"warn"` |
| `preflight_inert_probe` | `warning` | every probed mutation point left the scalar exactly at the champion mean while the A/A draws varied — the signal is UNMEASURED, not zero |
| `preflight_saturated_contract` | `warning` | pre-flight verdict `warn` — zero spread across every probe including a deliberately-degraded tree (the `1.000000` signature) |
| `preflight_margin_above_achievable` | `warning`, never gating | `promote_margin` ≥ the measured DEGRADATION signal — how far the scalar fell when a mutation point was destroyed. That does not bound how far a challenger can improve (issue #119), and the probe degrades ONE point so it under-reports even the movement it measures. Worth checking the margin; not evidence the run is null |
| `preflight_margin_below_floor` | `warning` | the margin window's lower bound fails — margin inside the floor |
| `tree_never_imported` | `warning`, one per (generation, tree) | no unit of a generation ever imported a mutable tree, so **mutations to it cannot have been under test** — the board scored code the loop never changed. Read `generations/<gen>/harness_load.json` |

The placebo arm is split out of the optimization stream before the other
detectors run, so an always-rejected control never reads as a stall or a
flat-scoring window.

## Findings only the per-round report carries

Two findings stay orchestrator-only because their inputs are live-round
state with no persisted workspace record for the CLI to read after the
fact — a later `zicato health` invocation cannot reconstruct them:

| Detector | Severity | Fires when |
|---|---|---|
| `infra_outage` | `warning` | the round deferred on `runtime.infra_abort_round_threshold` — the endpoint, not the loop, is failing |
| `round_token_clipped` | `warning` | `runtime.max_tokens_per_round` clipped the round; the verdict rests on partial coverage |

`detect_noisy_judge` (`warning` per judge whose test–retest disagreement
exceeds 0.25) is not part of `assess_loop_health` at all — it is reached via
`zicato board judges --test-retest` and the reflection analysis.

## Severities and exit codes

- **`ok`** — no findings. **`info`** — a notice; never interrupts.
  **`warning`** — discriminating power is degrading. **`critical`** — the
  loop is, or is about to be, meaningless.

| Exit code | When |
|---|---|
| `0` | report produced; the worst finding is `info` or **`warning`** (or there are none) — only `critical` exits non-zero |
| `1` | a **`critical`** finding is present (`raise SystemExit(1)`, `cli/commands/health.py:268`) — the "do not trust the lineage" signal |
| `1` | usage / configuration error too (no active epoch, unreadable board) — these raise `click.ClickException`, which also exits `1` |

**There is no distinct "degenerate" exit code.** The shipped
CLI exits `1` on a critical finding (and `1` on a config error), so you cannot
distinguish "degenerate" from "bad usage" on the code alone — read the printed
report. A CI wrapper branches on non-zero plus the report text:

```sh
.venv/bin/zicato health --workspace .zicato; rc=$?
if [ "$rc" -ne 0 ]; then echo "health critical or usage error — read the report above"; fi
```

Note: a `warning`-only report exits `0` — health only fails the process on
`critical`. For a programmatic warning/critical distinction, read the raw
`.zicato/epochs/{epoch}/health/round_{N}.json` report: `healthy` is `false`
for any warning or critical, `has_critical` isolates the criticals. (Or
`GET /api/health-report`, which serves the latest round report.)

## Prescribe the fix for a toothless loop

Each finding carries its own `recommendation` in its `detail`; the
contract-level fixes:

- **`degenerate_scoring` / `non_differentiating_entry`** → inspect
  `scoring.json` weights and the per-entry `loss.json` files; the board
  cannot drive a tournament when every entry scores identically. Replace the
  dead-weight entries with tasks that provoke *variable* behaviour, or check
  the adapter is actually running distinct generations.
- **`flat_drift_signal`** → drift detection is likely not
  wired; verify goldfive's event stream reaches the reducer (see
  [zicato-read-telemetry](../zicato-read-telemetry/SKILL.md)).
- **`tree_never_imported`** → the strongest finding in the set: it says the
  mutations were not under test at all. Check the harness entrypoint imports
  the mutable tree rather than an installed copy under another top-level name.
- **`no_expectations`** → if pass/fail ground truth was intended, attach
  expectations to board entries (BOARD-FORMAT). Drift-loss-only is valid but
  is only half the signal — and silent degeneracy is far more likely with
  half the scoring structurally absent.

## `evolve` stops itself on a degenerate loop (on by default)

For unattended runs you do not need a flag: `zicato evolve` **stops itself**
after `_DEGENERATE_HEALTH_STOP_THRESHOLD` (2) *consecutive* rounds whose
health assessment carried a `critical` finding, so a degenerate epoch doesn't
burn the rest of the budget. This is `DegenerateHealthPolicy`
(`evolve/loop.py:295`), enabled by the `stop_on_degenerate_health` argument,
**true by default** (`evolve/loop.py:375`); a non-critical round resets the
streak. The loop halts cleanly — state fully written — and the terminal round
records `stop_reason == "degenerate_health"` (`cli/commands/evolve.py:1001`).

Only `critical` findings advance the streak, so warnings — including every
`preflight_*` finding under the default `preflight_gate="warn"`, every
`tree_never_imported`, and a `stalled_loop` — are loud but structurally
unable to stop the loop. There is **no `--stop-on-degenerate` CLI flag** — it
is not opt-in, it is the default behaviour; the opt-out lives at the API level
(`stop_on_degenerate_health=False` on `evolve_n_rounds`), not on the CLI.
Confirm the flag surface against `zicato evolve --help` (the design docs
drift). Per project policy, never start a live `evolve` yourself — verify via
the test suite (`test_orchestrator_health.py`) and the on-disk report files.

Critical findings also fire a bannered `LOOP HEALTH CRITICAL` orchestrator
warning to stderr; `dead_judge`, `judge_erroring` and `tree_never_imported`
get their own terminal warnings even though they are only warnings, because
from the terminal they are indistinguishable from an honest null result. The
dashboard's loop-health panel reads `/api/health-report` (the latest round
report) — see [zicato-watch-dashboard](../zicato-watch-dashboard/SKILL.md).

## Guardrails

- Cite only flags present in real `--help`. `zicato health` has no `--round` /
  `--format`; there is no `--stop-on-degenerate` evolve flag — degenerate-stop
  is on by default, not a flag.
- Don't promise the operator a finding `zicato health` cannot print:
  `infra_outage` and `round_token_clipped` are live-round-only (no persisted
  reader), so they live in the per-round report exclusively. Every other
  finding — including the `preflight_*` family, `margin_below_noise_floor`,
  and `tree_never_imported` — now reaches the CLI too, from the same
  persisted records.
- Never launch a live `evolve` to test health — read the on-disk
  `epochs/{epoch}/health/round_{N}.json` reports instead.
