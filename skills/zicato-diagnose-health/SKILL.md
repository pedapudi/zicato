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
| `0` | report produced; `overall` is `ok` / `info` / **`warning`** — only `critical` exits non-zero |
| `1` | a **`critical`** finding is present (`raise SystemExit(1)`, `health.py:241`) — the "do not trust the lineage" signal |
| `1` | usage / configuration error too (no active epoch, unreadable board) — these raise `click.ClickException`, which also exits `1` |

**The `9` in the design docs is aspirational, not implemented.** The shipped
CLI exits `1` on a critical finding (and `1` on a config error), so you cannot
distinguish "degenerate" from "bad usage" on the code alone — read the printed
report. A CI wrapper branches on non-zero plus the report text:

```sh
.venv/bin/zicato health --workspace .zicato; rc=$?
if [ "$rc" -ne 0 ]; then echo "health critical or usage error — read the report above"; fi
```

Note: a `warning`-only report exits `0` — health only fails the process on
`critical`. For a programmatic warning/critical distinction, read the raw
`.zicato/epochs/{epoch}/loop_health/round_{NNN}.json` report's `overall` field.

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

## `evolve` stops itself on a degenerate loop (on by default)

For unattended runs you do not need a flag: `zicato evolve` **stops itself**
the first time it sees a `critical` health finding whose cause is *sustained*
degeneracy, so a degenerate epoch doesn't burn the rest of the budget. This is
the orchestrator's `stop_on_degenerate_health` behaviour, **on by default**
(`orchestrator.py:2982`); the loop halts cleanly — state fully written — and the
terminal round records `stop_reason == "degenerate_health"` (`evolve.py:775`).

A single tied round or a mere `no_expectations` notice does **not** stop the
loop — only provably-wasted compute does (the N-consecutive / shared-constant
trigger, or a majority-non-differentiating board). There is **no
`--stop-on-degenerate` CLI flag** — it is not opt-in, it is the default
behaviour; the opt-out lives at the API level (`stop_on_degenerate_health=False`
on `evolve_n_rounds`), not on the CLI. Confirm the flag surface against
`zicato evolve --help` (the design docs drift). Per project policy, never start
a live `evolve` yourself — verify via the test suite (`test_orchestrator_health.py`)
and the on-disk report files.

Critical findings also fire a bannered orchestrator warning to stderr and a
`loop_health_critical` SSE event that turns the dashboard's loop-health panel
red — see [zicato-watch-dashboard](../zicato-watch-dashboard/SKILL.md).

## Guardrails

- Cite only flags present in real `--help`. `--round` / `--format` are
  doc-only (not on `zicato health`); there is no `--stop-on-degenerate` evolve
  flag — degenerate-stop is on by default, not a flag.
- Never launch a live `evolve` to test health — read the on-disk
  `loop_health/round_{NNN}.json` reports instead.
