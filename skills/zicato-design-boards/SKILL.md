---
name: zicato-design-boards
description: "Design a discriminating zicato board: behavioral coverage, entries that separate champion from challenger, weighting, entry kinds, board_meta controls, emulator isolation, and one-session-per-run boundaries. Use when deciding what belongs on a board or when a loop runs clean but promotes nothing."
---

# Designing a zicato board

The **board** is the highest-leverage artifact you author. The evolve loop
can only discover what the board can measure: an edit that improves a behavior
no entry exercises is invisible, and an edit that wins on every entry equally
is indistinguishable from one that wins on none. The board, the judges, and
the scoring together are the **evaluation contract** — design the board for
*discrimination*, not just coverage.

This skill is the design-principles companion to the operational
[`zicato-author-board`](../zicato-author-board/SKILL.md) (the JSON schema, the
`board add`/`board list` commands, validation) and
[`zicato-design-judges`](../zicato-design-judges/SKILL.md) (the process-judge
and expectation companion). Author the syntax there; decide *what belongs*
here. When a finished board can't tell candidates apart, jump to
[`zicato-diagnose-health`](../zicato-diagnose-health/SKILL.md). Design docs:
[BOARD-FORMAT.md](../../docs/design/BOARD-FORMAT.md),
[BOARD-AUTHORING.md](../../docs/design/BOARD-AUTHORING.md),
[EMULATOR.md](../../docs/design/EMULATOR.md). See [`AGENTS.md`](../../AGENTS.md)
for the operating rules every skill assumes.

## The one principle: discrimination

A tournament promotes a challenger over the champion when its scalar is lower
by `promote_margin`. That decision is only meaningful if the board's entries
**move** between the two candidates. An entry that returns the same
`drift_loss` and the same expectation pass/fail for *every* generation is dead
weight — it can never break a tie, and a board made mostly of dead weight is a
**toothless loop** (`zicato health` fires one `non_differentiating_entry`
warning per such entry).

The failure modes to design against:

- **All-pass** — every entry trivially passes for champion and challenger
  alike. The pass-rate dimension is flat; the loop optimizes drift alone (or
  nothing). Usually a sign the expectations are too lenient (a `regex` that any
  plausible output matches) or the tasks are too easy.
- **All-fail** — no candidate ever passes. The proposer gets no gradient
  toward the behavior; pass-rate is pinned at zero. Usually a sign the task is
  beyond the current harness, or the predicate is over-specified.
- **Constant drift** — entries that emit identical drift regardless of the
  edit. `zicato health` fires `flat_drift_signal`; identically-zero drift
  across an epoch usually means telemetry isn't reaching the reducer, not that
  the harness is perfect.

A good entry sits in the **sensitive band**: the current champion is on the
edge — sometimes passing, sometimes drifting — so a real improvement shows up
as a measurable score delta. You discover the band empirically: run an epoch,
read the per-entry A/B grid (`zicato-tournament-forensics`), and cut or
re-tune entries that never moved.

## Grade the real artifact, not the agent's story about it

Discrimination is only real if every entry grades what the agent *actually
produced*. Two fidelity rules the board's design depends on:

- An **OUTCOME expectation** reads the agent's real output — `final_output` /
  `conversation_end`, or the written artifact — never a self-summary / proxy
  field (a `report_task_completed`, a "completed_results" blob). A proxy field
  lets a run *claim* success without doing the work, and the entry stops
  discriminating.
- A **PROCESS judge** that grades the deliverable reads the **tool-call
  ledger** (goldfive's `ctx.session_state.recent_events`, `kind ==
  "tool_observed"`), never the model's reasoning narration. goldfive dispatches
  custom judges only at *reasoning* observation points, so a judge that scans
  the transcript for tool names grades the story the agent told, not the tool
  round-trips it ran — and fires on chatter that merely mentions a tool.

Both are the same trap: a board that grades narration rewards a candidate that
*describes* doing the work over one that does it. Design entries so the signal
comes from the artifact/ledger; the mechanics are in
[`zicato-design-judges`](../zicato-design-judges/SKILL.md) and
[`zicato-audit-board`](../zicato-audit-board/SKILL.md).

## Continuous scores sharpen a wide-band entry

An OUTCOME expectation may return a **continuous** per-entry score (a float in
`[0,1]` — an F1, a similarity, a partial-credit rubric) plus an optional
`metrics` decomposition, instead of a bare pass/fail. Reach for a scored entry
when a behavior is *graded*, not binary — "retrieved 3 of 5 expected facts" is a
0.6, and that 0.6-vs-0.4 delta discriminates two candidates a pass/fail bit
would call a tie. A plain `bool` matcher still leaves the entry binary
(`score=None` → the reducer derives `1.0`/`0.0`), so a binary board is
unchanged. The score and `metrics` are carried to `loss.json` and feed the
proposer's failure-mode profile; the *weighting/formula* over them is owned by
[`zicato-tune-scoring`](../zicato-tune-scoring/SKILL.md).

## Coverage: span the behaviors, not the inputs

Coverage means *the behaviors you would regress on*, not *the space of inputs*.
A board with fifty near-identical single-turn prompts has one behavior's worth
of signal and fifty times the cost. Instead, enumerate the distinct behaviors
the harness must hold — task completion, revision handling, refusal
discipline, no-fabrication, tool-use hygiene — and put **one or two
discriminating entries on each**. Tag them consistently (`tags`) so pattern
detectors and the proposer brief can target a slice, and so
`zicato-tournament-forensics` can show you which behavior class regressed.

The board is frozen per epoch (the contract hash includes it). Design for the
*question this epoch asks*: a tight board aimed at the behavior the brief
targets discriminates harder than a sprawling one that dilutes the signal.

## The three live entry kinds — and when each

| Kind | Cost / determinism | Signal it carries | Use when |
|---|---|---|---|
| `single_turn` | cheapest, fully deterministic input | one prompt → final reply | smoke tests; a crisp behavior you can assert on the final output; a pass/fail predicate exists |
| `multi_turn_scripted` | cheap, deterministic (fixed user turns replayed verbatim) | how the harness handles a *known* revision sequence | regression-testing a multi-step interaction whose user side never needs to react to the agent |
| `multi_turn_emulated` | expensive, stochastic (aux-LLM plays a persona) | realistic dialog — the persona pushes back, reacts, demands | the strongest behaviors (handling vague feedback, resisting fabrication under pressure). Up-weight these |

Rules of thumb:

- Reach for `single_turn` first — it is the cheapest discriminator and most
  behaviors decompose into one. Add multi-turn only when the behavior is
  *intrinsically* about the conversation unfolding.
- Prefer `multi_turn_scripted` over `multi_turn_emulated` whenever the user
  side is fixed: scripted is deterministic and free of emulator noise, so its
  score deltas are pure signal. Reach for emulated only when the value is in
  the user *reacting* to the agent (you cannot script "push back until the deck
  is concrete").
- `multi_turn_emulated` carries the richest signal and the most noise. Up-
  weight it (`weight > 1.0`) so it dominates the aggregate, but expect run-to-
  run variance — set `promote_margin` above that noise floor
  (`zicato-tune-scoring`) so emulator jitter doesn't flip promotions.

(`synthetic_adversarial` / `synthetic_clean` are reserved forward-compat kinds
for the goldfive-as-target dogfood plan — a known-bad agent the steerer must
notice via `required_drift_kinds`. Not part of the v0 authoring surface; ignore
them unless you are explicitly building that target.)

## Weighting: spend signal where it matters

`weight` (default `1.0`) is the entry's pull on the aggregate. Up-weight the
entries whose behavior you most care about defending this epoch, and the
high-signal emulated entries; leave smoke tests at `1.0`. Weight is *relative
importance*, not difficulty — do not up-weight a hard entry hoping to "push"
the proposer; a hard entry that nobody passes is all-fail dead weight no matter
its weight. Earn discrimination by tuning the *expectation*, then weight what
discriminates.

## The `board_meta` header — `disable_drift` and `judge_only`

An optional first line carries board-wide settings (omit it entirely when both
are default):

```jsonc
{"board_meta": true, "disable_drift": ["user_steer", "user_pause"], "judge_only": true}
```

- **`disable_drift`** turns off goldfive's built-in detectors for the named
  drift kinds across every entry. Use it when a drift kind is *expected and
  benign* for this harness (e.g. you script `user_steer` turns, so steer-drift
  is noise, not signal). Tokens are the short lowercase goldfive `DriftKind`
  values (`tool_error`, `looping_reasoning`, `confabulation_risk`,
  `user_steer`, …); an unknown token is a hard validation error listing the
  valid set. This suppresses *built-in* detectors only — to remove a *custom*
  judge, delete it from the entry's `judges`, never via `disable_drift`.
- **`judge_only`** (board-wide) selects judge-only evaluation: goldfive still
  *judges* the wrapped agent (drift detectors and your process judges stay
  armed) but does **zero steering** — no goal-derivation call, no replanning,
  no drift-triggered refine. Use it when the question this epoch asks is "how
  does the bare harness behave", measured without the steerer intervening — or
  when you are evolving the steerer-free path itself. Default `false` (steering
  on). See [`zicato-design-judges`](../zicato-design-judges/SKILL.md) for what
  the judges then measure.

## Emulator isolation and session scope

A `multi_turn_emulated` entry runs its user persona through the
`user_emulator` role, which inherits `evaluation`, never `target`. Use an
explicit `user_emulator` engine override when emulator cost or capability
should differ from judging and proposing. Target and emulator engines must be
isolated: shared state could leak the answer the entry is supposed to test.
By construction the emulator sees *only* the persona
(`goal` / `constraints` / `stop_when`) and the user-facing transcript —
never the entry's expectation or harness internals. The design implication for
you: write the persona to *withhold the answer* (`constraints: "Never tell the
agent the exact answer you want"`), so the entry tests whether the harness can
*reach* the goal, not whether it can read it off the persona.

The board is a collection of independent runs, not a session. Each generation
× entry × replicate receives a fresh target session and, when applicable,
a separate emulator session. A multi-turn entry may preserve state across its
own turns. If cross-turn persistence is the behavior being measured, encode
the whole workflow as one compound entry; never obtain it by sharing a session
between entries or replicates.

## Smells — a board that can't tell candidates apart

| Smell | Likely cause | Fix |
|---|---|---|
| Loop runs clean, promotes nothing, journal full of ties | board is mostly dead weight | run `zicato health`; cut/retune every entry it names in a `non_differentiating_entry` finding |
| Two generations score *identically* round after round | nothing on the board discriminates them | `degenerate_scoring` detector; tighten expectations or add a sensitive-band entry |
| Pass-rate pinned at 1.0 | all-pass — expectations too lenient | tighten predicates/regex; add a harder discriminating entry |
| Pass-rate pinned at 0.0 | all-fail — task beyond the harness, or predicate over-specified | simplify the task or relax the predicate to the sensitive band |
| Drift identically zero across the epoch | telemetry not reaching the reducer (not a clean harness) | `flat_drift_signal`; check the telemetry path (`zicato-read-telemetry`) before trusting it |
| Emulated entry's score swings wildly run to run | emulator noise dominates the delta | raise `promote_margin` above the noise; or convert to `multi_turn_scripted` if the user side can be fixed |

All of these are exactly what [`zicato-diagnose-health`](../zicato-diagnose-health/SKILL.md)
detects — design the board to pass `zicato health` *before* trusting a
tournament verdict.

## Mid-epoch protection

The board is part of the frozen per-epoch contract; do not hand-edit a live
board mid-epoch — it changes the contract hash and corrupts the round counter
and pattern history. Design between epochs: edit the live `board.jsonl`, then
the next `evolve` auto-rolls into a fresh epoch (`--no-auto-epoch` to error on
drift instead). See [`zicato-author-board`](../zicato-author-board/SKILL.md)
for the mechanics.
