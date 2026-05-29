---
name: zicato-diagnose-health
description: Run zicato health and interpret it — detect a toothless / degenerate evolve loop (one that runs cleanly but optimises nothing), read the detectors and severities, branch on exit code 9, and prescribe the contract fix. Use whenever an epoch promotes nothing, scores look suspiciously identical, or you want to confirm the loop has real optimization signal before trusting a tournament.
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

Real flags only: `zicato health` exposes `--workspace` and `--epoch`. (The
design doc shows `--round` / `--format json`; those are **not** in the
shipped `--help` — do not pass them. If you need the raw report, read
`.zicato/epochs/{epoch}/loop_health/round_{NNN}.json` directly.)

It prints one block per firing detector — `[severity] detector_name`, a
one-sentence summary, and a `→ remedy`. With no findings the loop has signal.

## The five detectors

| Detector | What it catches | `info` | `warning` | `critical` |
|---|---|---|---|---|
| `degenerate_scoring` | parent and candidate produce identical/near-identical gen scores round after round — scoring isn't distinguishing them | one round of tied scores (`|Δscore| < 1e-6`) | — | N consecutive degenerate rounds (default 3), **or** one shared-constant score across the whole lineage (the §1 fingerprint) |
| `non_differentiating_entries` | board entries that always return the same `drift_loss` + `pass_fail` for every generation — dead weight that can't move a tournament | — | a minority of entries are dead weight | the **majority** of entries are dead weight |
| `flat_drift_signal` | drift telemetry — zicato's primary loss signal — not moving across the epoch | — | drift low-but-nonzero and static | drift **identically zero** across the whole epoch (suggests goldfive telemetry isn't reaching the reducer) |
| `no_expectations` | no board entry carries an expectation, so `pass_fail` is `None` everywhere and scoring runs on drift loss alone | always `info` — drift-loss-only is a supported mode, this is a reminder, not an alarm | — | — |
| `stalled_loop` | no promotions for K rounds **AND** another detector firing — structural reason the loop *cannot* promote | — | — | the conjunction fires |

`stalled_loop` is the bridge to the L5 circuit breaker: "no promotions for K
rounds" alone is L5's job (the proposer just isn't winning); loop-health
fires only when there's *also* a degenerate-eval reason.

## Severities and exit codes

- **`ok`** — no findings. **`info`** — a notice; never interrupts.
  **`warning`** — discriminating power is degrading. **`critical`** — the
  loop is, or is about to be, meaningless.

| Exit code | When |
|---|---|
| `0` | report produced; `overall` is `ok` or `info` |
| `9` | report produced; `overall` is `warning` or `critical` — the distinct "the loop is degenerate" code |
| `2` / `3` | usage / configuration error (e.g. no active epoch) |

A CI wrapper branches on `9` exactly the way it branches on other meaningful
outcomes:

```sh
.venv/bin/zicato health --workspace .zicato; rc=$?
if [ "$rc" -eq 9 ]; then echo "loop is degenerate — do not trust the lineage"; fi
```

## Prescribe the fix for a toothless loop

Each finding carries its own `remedy`; the contract-level fixes:

- **`degenerate_scoring` / `non_differentiating_entries`** → inspect
  `scoring.json` weights and the per-entry `loss.json` files; the board
  cannot drive a tournament when every entry scores identically. Replace the
  dead-weight entries with tasks that provoke *variable* behaviour, or check
  the adapter is actually running distinct generations.
- **`flat_drift_signal` (critical/zero)** → drift detection is likely not
  wired; verify goldfive's event stream reaches the reducer (see
  [zicato-read-telemetry](../zicato-read-telemetry/SKILL.md)).
- **`no_expectations`** → if pass/fail ground truth was intended, attach
  expectations to board entries (BOARD-FORMAT). Drift-loss-only is valid but
  is only half the signal — and silent degeneracy is far more likely with
  half the scoring structurally absent.

## Pair with `evolve --stop-on-degenerate`

For unattended runs, the operator can pair health diagnostics with an opt-in
early stop so a degenerate epoch doesn't burn the rest of the budget:

```sh
zicato evolve --rounds 20 --stop-on-degenerate
```

It halts cleanly — state fully written — the first time it sees a `critical`
report whose cause is *sustained* degeneracy (the N-consecutive / shared-
constant trigger, or a majority-non-differentiating board), and exits with
the same code **`9`** as `zicato health`. A single tied round or a mere
`no_expectations` notice does **not** stop the loop — only provably-wasted
compute does. (This is a documented evolve flag; confirm against
`zicato evolve --help` before relying on it. Per project policy, never start
a live `evolve` yourself — verify via the test suite and the report files.)

Critical findings also fire a bannered orchestrator warning to stderr and a
`loop_health_critical` SSE event that turns the dashboard's loop-health panel
red — see [zicato-watch-dashboard](../zicato-watch-dashboard/SKILL.md).

## Guardrails

- Cite only flags present in real `--help`. `--round` / `--format` are
  doc-only; `--stop-on-degenerate` is an evolve flag.
- Never launch a live `evolve` to test health — read the on-disk
  `loop_health/round_{NNN}.json` reports instead.
