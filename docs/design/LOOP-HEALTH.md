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
- The five detectors and their severities (§3).
- The `LoopHealth` report (§4).
- The `zicato health` CLI (§5).
- How the orchestrator surfaces critical findings (§6).

## 1. The incident that motivated this

During an early dogfood run, an epoch produced this:

```
v0  gen_score = 1.000000
v1  gen_score = 1.000000
```

Both generations scored **exactly** `1.000000`. Not "close" —
identical to six decimal places. v1's patches had been applied,
the tournament had run the full board against both sides, the
gate had evaluated, v1 had been rejected on `insufficient_margin`,
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

Loop health runs a fixed set of detectors after every round (and
on demand via `zicato health`). Each detector inspects the
round's runs, scores, and the epoch-so-far history, and either
stays silent or emits a `LoopHealthFinding` with a severity.

### 3.1 Degenerate scoring

**What it catches.** The motivating incident (§1): parent and
candidate produce identical (or near-identical) generation
scores, round after round, because the scoring is not
distinguishing them.

**Signal.** For a round, `|candidate.score - parent.score|` is
below a degeneracy floor (`degenerate_score_epsilon`, default
`1e-6`). One such round is `info` — scores can legitimately tie.
The detector escalates on *sustained* degeneracy: N consecutive
rounds (default 3) where every score delta is below the floor is
`critical`, because at that point the evaluation has demonstrably
failed to produce signal across multiple distinct candidates.

A second, sharper sub-signal: if **every** generation in the
epoch carries the *exact same* `score` value (e.g. all
`1.000000`), that is `critical` immediately, no waiting for three
rounds — a single shared constant across the whole lineage is the
fingerprint of the §1 bug.

### 3.2 Non-differentiating board entries

**What it catches.** Individual board entries that always return
the same `drift_loss` and `pass_fail` for every generation,
parent and candidate alike. Such an entry contributes a constant
to every score and therefore *cannot* move a tournament — it is
dead weight on the board.

**Signal.** For each `entry_id`, look across every run of that
entry in the epoch. If `drift_loss` has zero variance (every run
identical) AND `pass_fail` never flips, the entry is
non-differentiating. A handful of non-differentiating entries on
a large board is `warning` (the board has some slack); a board
where the *majority* of entries are non-differentiating is
`critical` (the board as a whole cannot distinguish generations).

This detector is the per-entry version of degenerate scoring: it
localises *which* entries are the dead weight, so the operator
knows what to fix.

### 3.3 Flat drift signal

**What it catches.** The drift telemetry — zicato's primary loss
signal — is not moving at all across the epoch. Either the inner
harness genuinely produces no drift (possible but rare), or drift
detection is misconfigured / not wired, or the board's tasks are
too easy to provoke any drift.

**Signal.** Aggregate `drift_counts_by_kind` across all runs in
the epoch. If the total drift count is zero across every run, or
every drift kind has zero variance across rounds (the same flat
counts every round), the drift signal is flat. Flat drift makes
the `w_drift` term in the score a constant, which collapses
scoring to pass-rate-only without the operator having chosen
that. Severity: `warning` if drift is low-but-nonzero and static;
`critical` if drift is identically zero across the whole epoch
(strongly suggests the goldfive telemetry is not actually
reaching the reducer).

### 3.4 No expectations

**What it catches.** No board entry carries an expectation
predicate, so `pass_fail` is `None` everywhere and the pass-rate
side of scoring contributes nothing. The loop is then running on
drift loss alone — which is *valid* (see
[SCORING.md §1](SCORING.md#1-why-both-signals): drift loss works
without ground truth) but is also a common accident, where the
operator *meant* to attach expectations and forgot.

**Signal.** Count board entries with an expectation. Zero is the
trigger. Severity is `info`, not `warning` — drift-loss-only is a
supported mode, so this is a *notice* ("you are running without
ground-truth pass/fail; that is fine if intentional"), not an
alarm. It exists because the §1-style silent degeneracy is much
more likely when half the scoring signal is structurally absent,
and the operator should be reminded they opted into that.

### 3.5 Stalled loop

**What it catches.** The loop is making rounds but the lineage is
not advancing — no promotions for a long stretch — *combined
with* a loop-health reason to believe the eval is at fault rather
than the proposer.

**Signal.** This detector is the bridge to the L5 circuit
breaker. On its own, "no promotions for K rounds" is L5's
territory. The loop-health stalled detector fires when no
promotions for K rounds **and** at least one other detector
(degenerate scoring, non-differentiating entries, flat drift) is
also firing in the same window. That conjunction is the strong
signal: the loop is not promoting *and* there is a structural
reason it *cannot*. Severity: `critical`. When the loop is
stalled but no other detector fires, loop health stays silent and
lets L5 own it — the eval looks fine; the proposer is just not
winning.

### 3.6 Severity summary

| Detector | `info` | `warning` | `critical` |
|---|---|---|---|
| Degenerate scoring | one round of tied scores | — | N consecutive degenerate rounds; or one shared constant score across the whole lineage |
| Non-differentiating entries | — | a minority of entries are dead weight | the majority of entries are dead weight |
| Flat drift signal | — | drift low and static | drift identically zero across the epoch |
| No expectations | no entry has an expectation | — | — |
| Stalled loop | — | — | no promotions for K rounds AND another detector firing |

Severities mean:

- **`info`** — a notice. Surfaced in the report and the
  dashboard panel; never interrupts the loop.
- **`warning`** — something is degrading the loop's
  discriminating power but it is not yet meaningless. Surfaced;
  logged loudly; the loop continues.
- **`critical`** — the loop is, or is about to become,
  meaningless. Surfaced as a loud orchestrator warning (§6); the
  trigger for the optional early-stop.

## 4. The `LoopHealth` report

The detectors' output is collected into a typed `LoopHealth`
report — one per round, and one produced on demand by
`zicato health`.

```json
{
  "epoch_id": "2026-05-15_e1",
  "round": 7,
  "computed_at": "2026-05-15T14:22:00Z",
  "overall": "critical",
  "findings": [
    {
      "detector": "degenerate_scoring",
      "severity": "critical",
      "summary": "v0..v7 all carry gen_score = 1.000000; the evaluation has produced zero score variance across 8 generations.",
      "evidence": {
        "generations": ["v0", "v1", "v2", "v3", "v4", "v5", "v6", "v7"],
        "shared_score": 1.0,
        "consecutive_degenerate_rounds": 7
      },
      "remedy": "Inspect scoring.json weights and the per-entry loss.json files; a board where every entry scores identically cannot drive a tournament."
    },
    {
      "detector": "non_differentiating_entries",
      "severity": "critical",
      "summary": "9 of 10 board entries return identical drift_loss and pass_fail for every generation.",
      "evidence": {
        "non_differentiating": ["short_solar", "long_solar", "..."],
        "differentiating": ["contradictory_brief"],
        "board_size": 10
      },
      "remedy": "These entries are dead weight. Either replace them with tasks that provoke variable behaviour, or check that the adapter is actually running distinct generations."
    },
    {
      "detector": "no_expectations",
      "severity": "info",
      "summary": "No board entry carries an expectation; scoring is running on drift loss alone.",
      "evidence": {"entries_with_expectation": 0, "board_size": 10},
      "remedy": "If pass/fail ground truth was intended, attach expectations (see BOARD-FORMAT.md). Drift-loss-only is supported but is half the signal."
    }
  ]
}
```

Fields:

| Field | Meaning |
|---|---|
| `epoch_id`, `round` | Which round this report covers. |
| `computed_at` | Timestamp. |
| `overall` | The max severity across all findings (`ok` if no findings). |
| `findings` | One `LoopHealthFinding` per firing detector. |
| `findings[].detector` | The detector name (§3). |
| `findings[].severity` | `info` / `warning` / `critical`. |
| `findings[].summary` | One-sentence human-readable rendering. |
| `findings[].evidence` | Structured data backing the finding — exact generations, entry ids, counts — so the operator can verify it. |
| `findings[].remedy` | A concrete next step. Detectors never just complain; each carries the operator's fix. |

The per-round report is written to
`.zicato/epochs/{epoch}/loop_health/round_{NNN}.json` next to the
`patterns/` directory. It is a canonical file like every other
artifact, and it is projected into the analytical index (a
`loop_health` concern can be added to the index schema; see
[ANALYTICAL-INDEX.md](ANALYTICAL-INDEX.md)) so the dashboard's
loop-health panel ([DASHBOARD.md](DASHBOARD.md)) can render the
epoch's health trajectory without a file-walk.

## 5. The `zicato health` CLI

`zicato health` runs the detectors on demand and prints the
`LoopHealth` report.

```
zicato health [--epoch <id>] [--round <N>] [--format json|text]
```

- With no flags, runs every detector against the current epoch's
  full history and prints the report for the latest round.
- `--round <N>` prints the stored report for a specific past
  round (read straight from
  `loop_health/round_{NNN}.json` — no recomputation).
- `--epoch <id>` targets a non-current epoch.
- `--format json` emits the `LoopHealth` object verbatim for
  scripting.

Text output:

```
$ zicato health
loop health — epoch 2026-05-15_e1 — round 7 — OVERALL: CRITICAL

  [critical] degenerate_scoring
    v0..v7 all carry gen_score = 1.000000; the evaluation has
    produced zero score variance across 8 generations.
    → Inspect scoring.json weights and the per-entry loss.json
      files; a board where every entry scores identically
      cannot drive a tournament.

  [critical] non_differentiating_entries
    9 of 10 board entries return identical drift_loss and
    pass_fail for every generation.
    → These entries are dead weight. Replace them, or check the
      adapter is running distinct generations.

  [info] no_expectations
    No board entry carries an expectation; scoring is running on
    drift loss alone.
    → Attach expectations if pass/fail ground truth was intended.

2 critical, 0 warning, 1 info.
```

Exit codes follow the CLI convention (see [CLI.md](CLI.md)):

| Code | When |
|---|---|
| `0` | Report produced; `overall` is `ok` or `info`. |
| `9` | Report produced; `overall` is `warning` or `critical`. A distinct non-zero code so a CI / wrapper script can branch on "the loop is degenerate" exactly the way it branches on the other meaningful outcomes. |
| `2` / `3` | Usage / configuration error. |

A CI wrapper that runs `zicato evolve` overnight pairs it with
`zicato health` so a degenerate epoch is caught the next morning
without an operator eyeballing the journal — the exact manual
step that the §1 incident depended on.

## 6. How the orchestrator surfaces critical findings

Loop health is computed inside the round loop, right after the
journal entry is written (round mechanics step 8 in
[EPOCHS-AND-JOURNALING.md §8](EPOCHS-AND-JOURNALING.md#8-round-mechanics);
loop health slots in as a step alongside the pattern detectors).
Two surfacing behaviours follow.

### 6.1 Loud warning on critical

When a round's `LoopHealth` comes back `critical`, the
orchestrator does not let it scroll past as a normal log line. It
emits a bannered warning to stderr:

```
╔══════════════════════════════════════════════════════════════════╗
║  LOOP HEALTH: CRITICAL                                            ║
║                                                                   ║
║  degenerate_scoring — v0..v7 all carry gen_score = 1.000000.      ║
║  The evaluation has produced zero score variance across 8         ║
║  generations. The tournament cannot distinguish candidates;       ║
║  rounds are being run but no optimisation signal exists.          ║
║                                                                   ║
║  → zicato health   for the full report and remedies.             ║
╚══════════════════════════════════════════════════════════════════╝
```

The same finding is broadcast as an SSE event to the live
dashboard (a `loop_health_critical` event kind — see
[DASHBOARD.md](DASHBOARD.md)), where the loop-health panel turns
red. The point of the banner and the panel is that the §1 failure
mode can never again depend on an operator happening to notice a
suspicious number — the system says it out loud.

A `warning`-level report logs a single prominent (but
un-bannered) line; an `info`-level report logs a quiet notice.
Only `critical` gets the banner.

### 6.2 Optional early-stop on sustained degeneracy

A loud warning is enough if an operator is watching. For
unattended runs (the overnight calibration epoch), zicato offers
an opt-in early-stop:

```
zicato evolve --rounds 20 --stop-on-degenerate
```

With `--stop-on-degenerate`, the orchestrator halts the loop —
cleanly, with the current epoch's state fully written — the first
time it sees a `critical` loop-health report whose cause is
*sustained* degeneracy (the degenerate-scoring detector's
N-consecutive-rounds or shared-constant trigger, or a
majority-non-differentiating board). `evolve` exits with code
`9`, the same code `zicato health` uses, so a wrapper script
treats "stopped because degenerate" identically to "checked and
found degenerate".

The early-stop is **opt-in, not default**, for the same reason
the L5 circuit breaker is opt-in
([ROBUSTNESS.md §2.5](ROBUSTNESS.md#25-l5-consecutive-bad-circuit-breaker)):
zicato does not unilaterally decide an operator's loop is not
worth continuing. A single degenerate round can be noise; a
genuinely intended drift-loss-only board with low-variance tasks
is not *wrong*, just hard to optimise. The operator who passes
`--stop-on-degenerate` is stating "if the eval has gone
toothless, do not burn the rest of my budget" — and that is the
right person to make that call. Without the flag, the loop keeps
running and the banner keeps firing; the operator decides.

Not every `critical` finding triggers the early-stop — only
sustained degeneracy does. A `no_expectations` finding is only
`info` and never stops anything. A single `critical` round of
tied scores warns but does not stop (it might be noise). The
early-stop is reserved for the cases where continuing is
*provably* wasted compute.

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
