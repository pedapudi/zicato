---
name: zicato-triage-stuck-loop
description: Diagnose a zicato evolve loop that is not improving — many consecutive rejects or a flat scalar. Use when the loop runs but stops promoting; this is a decision tree that distinguishes a degenerate evaluation from a too-hard board, an over-constrained Forbidden set, or a stuck proposer, and prescribes the contract edit.
---

# zicato triage stuck-loop (the loop runs but does not improve)

Symptom: `evolve` keeps running but nothing promotes — many consecutive
rejects (it may stop at `--max-consecutive-rejections`, default 3) or a scalar
that will not move. A running loop that produces no learning is a robustness
failure, not a no-op. Work the tree below.

> Guardrails: every command here is read-only and spends no LLM budget. Do
> **not** kick off a live `evolve` / `propose` / `tournament` to "see if it
> helps" without the operator's explicit go-ahead (AGENTS.md rule 1). Use
> `.venv/bin/zicato`; never `uv sync`.

## Step 0 — always start here: is the evaluation even toothless?

The most common cause of a stuck loop is a **degenerate evaluation** — the loop
cannot tell generations apart, so nothing *can* promote. Run the health check
first; it directly answers this.

```sh
Z=.venv/bin/zicato
$Z health                 # current epoch; add --epoch <id> to target one
```

Exit codes: `0` = no `critical` finding (`ok`/`info`/even `warning`); `1` = a
`critical` finding is present (`raise SystemExit(1)`; there is no bespoke
"degenerate" code — and a config error like "no active epoch" also exits `1`).
Each finding carries a `severity` and a concrete `remedy` — read them; the
detector tells you the fix. The detectors that bear on a stuck loop
(`docs/design/LOOP-HEALTH.md` §3; more ship than these — overfitting,
pre-flight, and infra detectors also file findings):

| Detector | Fires when | Points you at |
|---|---|---|
| Degenerate scoring | The last `scoring_window` tournaments ALL came back with `\|Δscalar\| ≤ scoring_epsilon` (`critical` — the only detector that trips the breaker below) | The scoring contract or the board — there is no signal to optimise. |
| Non-differentiating board entries | An entry's `drift_loss` is identical across every generation it ran under (`warning`, one finding per entry) | Those entries — they add cost but no discrimination. |
| Flat drift signal | The `drift:`-namespace metric count is zero across every run in the epoch (`warning`) | Telemetry / expectations — the harness emits no usable drift. |
| No expectations | Board entries carry no Predicate/Rubric | The board — drift-loss-only is weak signal (severity `info`). |
| Dead judge | A board-declared judge never fired in any run of the epoch (`warning`) | That judge — mis-wired or unreachable, so it is coverage on paper only. |
| Stalled loop | The trailing run of `rejected` decisions reaches `stalled_rejects` (`warning`, no co-firing detector required) | Whatever else is firing; if nothing is, work branches B–D. |

If `health` is `warning`/`critical`, the firing detector + its remedy *is* your
diagnosis. Jump to the matching branch below. If `health` is `ok`/`info`, the
evaluation has teeth — the loop is stuck for a non-degenerate reason; continue
to Step 1.

Two stops to keep straight when the loop ended early on its own.
`--max-consecutive-rejections` (default 3) is the unproductive-loop stop.
Separately, the orchestrator's default-on breaker stops the loop with reason
`degenerate_health` after **2 consecutive rounds** whose health came back
`critical` — `warning`-only rounds never trip it, and there is no CLI flag for
it. A refuse-recommended contract pre-flight is not a third stop under the
defaults: it files `preflight_signal_below_floor` at `warning` under
`runtime.preflight_gate = "warn"` and so cannot halt anything; only
`preflight_gate = "refuse"` grades it `critical`, and that mode refuses at
pre-flight before any round runs.

## Step 1 — branch on what `health` (and the index) shows

### A. Degenerate scoring / flat drift / non-differentiating entries → fix the contract
The board cannot separate generations, or scoring collapses to a constant.
Editing `board.jsonl` / `brief.md` / `scoring.json` changes the evaluation
contract, so the next `evolve` auto-rolls a fresh epoch — which is what you
want. Hand-editing the *frozen* per-epoch board with the `board` subcommands
instead keeps the epoch but resets its pattern history (AGENTS.md rule 5).

- **Flat scalar across generations** → the board has no discriminating entries
  or scoring weights are degenerate. Tighten/replace board entries so good and
  bad harnesses score differently; check `scoring.json` weights and
  `per_judge_weights`. Authoring help: design the new entries/expectations, then
  use `zicato-design-experiment` for the next hypothesis once signal returns.
- **Specific dead entries** → the index names them. Replace or harden those
  entries' expectations.
- **Identically-zero drift** → the harness/telemetry emits no drift, or entries
  have no expectations. Add Predicate/Rubric expectations; confirm the adapter
  emits the goldfive event stream.

Confirm with read-only SQL (see `zicato-index-ops`):

```sql
-- Is the scalar actually flat across generations?
sqlite3 -readonly .zicato/index.db "
  SELECT child_generation_id, decision, delta_scalar, rejection_reason
  FROM tournaments WHERE epoch_id='e3' ORDER BY ran_at;"

-- Which entries never vary across generations (non-differentiating)?
sqlite3 -readonly .zicato/index.db "
  SELECT entry_id,
         COUNT(DISTINCT ROUND(drift_loss,4)) AS distinct_losses,
         COUNT(*) AS runs
  FROM loss_profiles WHERE epoch_id='e3'
  GROUP BY entry_id ORDER BY distinct_losses ASC;"
```

### B. `health` is ok/info but the board is simply too hard → relax the goal/board
The evaluation discriminates, but every challenger loses on the gate (rejects
with real, non-flat `delta_scalar`, often pass-rate regressions). The board may
demand a leap no single small mutation can clear.

```sql
sqlite3 -readonly .zicato/index.db "
  SELECT child_generation_id, decision, parent_scalar, child_scalar,
         delta_scalar, rejection_reason
  FROM tournaments WHERE epoch_id='e3' AND decision='rejected' ORDER BY ran_at;"
```

If rejections cite pass-rate monotonicity failures on a few brutal entries,
split the goal into smaller epochs, or relax/stage the hardest board entries so
incremental progress can clear the gate. This is a contract edit → it rolls the
epoch.

### C. Over-constrained `## Forbidden` → the proposer has no room to move
If the patterns point at a mutation point the brief forbids, the proposer is
boxed in: it can see the problem but cannot touch the fix. Check what is allowed
vs forbidden:

```sh
$Z mutations --show preview      # the full mutable surface
# then read the epoch's brief.md and compare its `## Forbidden` list to where
# loss concentrates (Step 0 / index hot-metrics query).
```

If the high-loss mutation point is in `## Forbidden`, the remedy is an operator
decision: either un-forbid it in `brief.md` (a contract/steering edit) or accept
that this loss is out of scope for the epoch. Removing an id from `## Forbidden`
changes the mutation contract and rolls the epoch.

### D. Proposer stuck in a rut → re-steer via the brief
`health` is ok, the board discriminates, `## Forbidden` is reasonable — but the
proposer keeps proposing near-identical changes to the same point and they keep
rejecting. Look for repetition:

```sql
sqlite3 -readonly .zicato/index.db "
  SELECT generation_id, hypothesis_core_idea, tournament_decision
  FROM experiments WHERE epoch_id='e3' ORDER BY generation_id;"

sqlite3 -readonly .zicato/index.db "
  SELECT mutation_id, COUNT(*) n FROM patches WHERE epoch_id='e3'
  GROUP BY mutation_id ORDER BY n DESC;"
```

If the same `mutation_id` / `core_idea` recurs across rejects, edit `brief.md`
to steer the proposer elsewhere (the brief is steering, not contract — text
edits that do **not** change `## Forbidden` do not roll the epoch). Point it at
an under-explored mutation point or a different drift kind, then design the next
move with `zicato-design-experiment`.

## Quick reference

1. `zicato health` — degenerate evaluation? (the #1 cause)
2. Flat/zero/dead-entry signal → fix the **board/scoring** contract (rolls epoch).
3. Real rejects, board too hard → relax the **goal/board** (rolls epoch).
4. Fix is forbidden → revisit `## Forbidden` in **brief.md** (rolls epoch).
5. Proposer repeating itself → re-steer **brief.md** text (does not roll epoch).

## See also

- `docs/design/LOOP-HEALTH.md` — the detectors, severities, `LoopHealth` report.
- `docs/design/SCORING.md` — the promotion gate (drift margin + pass-rate
  monotonicity) the rejects are failing.
- `docs/design/EPOCHS-AND-JOURNALING.md` — contract = board + brief + scoring;
  what rolls an epoch and what does not (§ on the proposer brief / `## Forbidden`).
- sibling skills: `zicato-index-ops` (the queries), `zicato-design-experiment`
  (the next hypothesis once signal returns), `zicato-step-loop` (drive a manual
  round to inspect one rejection).
