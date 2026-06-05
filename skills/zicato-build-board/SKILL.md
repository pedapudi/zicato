---
name: zicato-build-board
description: The board-builder copilot's deep board-craft guide — designing board entries, judges, and the weighted loss through a GUI as a DRAFT applied on confirmation. Use when a copilot is helping an operator design a board, declare judges, shape the loss to express an objective, hold out a slice, or fix a toothless eval. Covers entries (single/multi-turn, expectations, tags incl. holdout, weight), judges (declared/in-run, judge_name, per_judge_weights, board_meta, the collusion-guarded emulator), and how the loss knobs combine to express an objective. Defers the exact scalar formula to SCORING.md, the schema to BOARD-FORMAT.md, anti-overfitting to OVERFITTING.md, discrimination diagnosis to LOOP-HEALTH.md.
---

# Building a zicato board (copilot guide)

This skill is for the **board-builder copilot** — the agent helping an operator
design a board through the builder GUI: its entries, its judges, and the
weighted loss that turns runs into the tournament scalar. It is the deep
board-craft skill, grounded in the design docs but framed as the builder
workflow. It is the board sibling of
[`zicato-build-tournament`](../zicato-build-tournament/SKILL.md) (the
whole-contract walkthrough — structure, holdout split, proposer, gate). Defer:

- **The on-disk JSONL schema** (every field, validation, `RunResult`) →
  [BOARD-FORMAT.md](../../docs/design/BOARD-FORMAT.md).
- **The exact scalar formula and the gate** →
  [SCORING.md](../../docs/design/SCORING.md).
- **The train/holdout split, the Ladder budget, leakage** →
  [OVERFITTING.md](../../docs/design/OVERFITTING.md).
- **Diagnosing a board that can't discriminate** →
  [LOOP-HEALTH.md](../../docs/design/LOOP-HEALTH.md) and
  [`zicato-diagnose-health`](../zicato-diagnose-health/SKILL.md).

The design-principles siblings remain the authority on *what belongs* on a
board ([`zicato-design-boards`](../zicato-design-boards/SKILL.md)) and *what to
measure* ([`zicato-design-judges`](../zicato-design-judges/SKILL.md)); this
skill is how the copilot *operates the builder* over those principles. See
[`../AGENTS.md`](../AGENTS.md) for the operating rules.

## Board entries — the unit the copilot edits

Each board entry is one task the inner harness runs. Three live kinds; pick the
cheapest that carries the behavior's signal (full table and cost/noise trade in
[`zicato-design-boards`](../zicato-design-boards/SKILL.md)):

- **`single_turn`** — one `input` message → the agent's final reply. Cheapest,
  fully deterministic input. The default; reach for it first.
- **`multi_turn_scripted`** — a fixed list of user `turns` replayed verbatim.
  Cheap and deterministic; use it to regression-test a known revision sequence.
- **`multi_turn_emulated`** — an aux-LLM `user_persona` plays the user, reacting
  to the agent. Expensive and stochastic; reserve it for behaviors that only
  emerge when the user *pushes back*.

Every entry carries an envelope the copilot's tools edit:

- **`input` / `turns` / `user_persona`** — the per-kind task content.
- **`expectation`** — the entry's single typed **OUTCOME** check, evaluated
  post-hoc on the run's product. Two families compile to it: **Predicate**
  (deterministic — `predicate` dotted path, `expected_text`, `regex`,
  `json_schema`) and **Rubric** (LLM-graded, runs on the aux callable, carries
  `threshold`/`scale`; `threshold: null` = advisory). An entry has at most one
  expectation; absent → the entry is scored on drift loss alone. `reads`
  selects the slice graded (`final_output` vs `conversation_end`). Prefer a
  deterministic matcher whenever the behavior is code-checkable — it adds zero
  noise to pass-rate.
- **`tags`** — operator labels pattern detectors and the brief slice on. The
  **`holdout` tag is load-bearing**: it marks the entries withheld from the
  promotion signal and confirmed afterward (the train/holdout split — see "Hold
  out a slice" below).
- **`weight`** — the entry's pull on the aggregate (default `1.0`).

Defer the exact field shapes and validation to
[BOARD-FORMAT.md](../../docs/design/BOARD-FORMAT.md).

## Judges — the in-run PROCESS axis

Where an `expectation` asks "is the *product* right?", a **judge** asks "is the
*process* clean?" — it watches the live reasoning stream and, on an adverse
verdict, emits a goldfive `custom` drift tagged with the judge's name. The two
axes are independent and combine into the scalar under separate coefficients
(pass-rate vs drift-loss), so a behavior you care about on both gets both an
expectation and a judge. Decide the axis with
[`zicato-design-judges`](../zicato-design-judges/SKILL.md).

What the copilot sets on each judge:

- **`name`** — a stable, board-unique slug. It becomes goldfive's `judge_name`,
  and **`per_judge_weights` keys on it** — choose it deliberately, because the
  loss weighting reaches the judge by this name.
- **`mode`** — `inline` (a natural-language criterion graded by the aux LLM) or
  `python` (a dotted path to a deterministic process-judge callable).
- **`body`** — the criterion text (`inline`) or the dotted path (`python`).
- **`severity`** — `info` / `warning` / `critical`; scales how hard a violation
  weighs (default `1 / 3 / 10` via `severity_weights`).

Two more board-wide judge facts the copilot surfaces via the `board_meta`
header (set when non-default):

- **`disable_drift`** — suppress goldfive's *built-in* detectors by kind, board-
  wide (e.g. you script `user_steer` turns, so steer-drift is benign noise).
  Custom judges are removed by deleting them from `judges`, never here.
- **`judge_only`** — keep goldfive *judging* but turn off all steering. Use it
  to measure the bare, un-steered harness, or when evolving the steerer-free
  path; your judges and expectations then carry the entire signal.

**The collusion guard (design constraint the copilot must respect):** every
grader — rubric matcher, inline/aux judge, the user-emulator — runs on the
**auxiliary** callable, distinct from the **harness** callable that runs the
system under test (zicato hard-errors on an identity match). The emulator sees
only the persona + the user-facing transcript, never the entry's expectation.
The implication for board craft: write personas to *withhold the answer* (so
the entry tests whether the harness can *reach* the goal, not read it off the
persona), and never author a judge that reaches into harness state — judge only
the observable stream.

## The weighted loss — shape it to express an objective

The tournament scalar is a weighted combination of a **drift-loss** term and a
**pass-rate** term. The exact formula, the three-rule gate, and the defaults
live in [SCORING.md](../../docs/design/SCORING.md) — do not restate it. What the
copilot teaches is how to *shape* the loss with the knobs to express what the
operator means by "better":

| Knob | Keys on | Use it to express… |
|---|---|---|
| `drift_weight` / `pass_weight` | the two top-level axes | whether cleanliness or correctness dominates (`pass_weight = 0` ignores expectations; `drift_weight = 0` scores on pass-rate alone) |
| `severity_weights` | `info` / `warning` / `critical` | how much worse a critical is than an info |
| `per_kind_weights` | built-in `DriftKind` token | "this harness's `off_topic` matters 2× the others" |
| `per_judge_weights` | judge `name` | telling *custom judges apart* — they all share the `custom` kind, so this is the only per-judge lever |
| `default_judge_weight` | — | the fallback for a custom judge absent from `per_judge_weights` |
| `namespace_weights` | namespace prefix (`drift:`, `cost:`, `rubric:`, …) | the multi-objective scalar — signed coefficients fold each namespace into the scalar |
| pass-rate monotonicity | the gate | "a challenger may never break an entry the champion passed" |

Worked example of *shaping*: to say "I care most that the agent stays on
topic, and I will not promote anything that breaks a passing entry," the copilot
sets `per_kind_weights["off_topic"] = 2.0` (elevate that drift kind), leaves the
other kinds at the uniform default, and leaves pass-rate monotonicity on (the
gate then hard-rejects any pass-rate regression). To weight a single custom
judge — say `cite_before_metric` is a real defect but `ack_before_edit` is a
nicety — set `per_judge_weights = {"cite_before_metric": 2.0, "ack_before_edit":
0.5}`. The copilot's job is to translate the operator's objective into the
*minimal* set of knob changes and show the resulting loss shape; the numeric
calibration loop (run an epoch, check the journal, retune) is in
[`zicato-tune-scoring`](../zicato-tune-scoring/SKILL.md).

## Designing a discriminating board

A board only earns its cost if its entries **separate champion from
challenger**. The copilot designs for discrimination, not just coverage:

- **Avoid the toothless eval.** An entry that returns the same drift loss and
  the same pass/fail for *every* generation is dead weight — it can never break
  a tie. The failure modes are **all-pass** (expectations too lenient), **all-
  fail** (task beyond the harness, or predicate over-specified), and **constant
  drift** (often telemetry not reaching the reducer). A good entry sits in the
  *sensitive band*: the champion is on the edge, so a real improvement shows up
  as a score delta. The copilot should cross-reference
  [LOOP-HEALTH.md](../../docs/design/LOOP-HEALTH.md) /
  [`zicato-diagnose-health`](../zicato-diagnose-health/SKILL.md) and steer the
  operator to design a board that passes `zicato health` *before* trusting a
  verdict.
- **Span behaviors, not inputs.** Fifty near-identical prompts carry one
  behavior's signal at fifty times the cost; enumerate the distinct behaviors
  to defend and put one or two discriminating entries on each.

## Hold out a slice — don't let the proposer memorise the board

Because the board is the *whole* signal the proposer optimises against, a
challenger can win by **memorising the board** rather than generalising — its
score on the entries it has seen improves while its true quality does not. The
defenses the copilot builds in:

- **Hold out a slice.** Tag ~30% of entries `holdout`; they are withheld from
  the round's promotion signal and the gate confirms the winner on them
  afterward. A challenger that won on train but does not hold up on the holdout
  has a **generalization gap** — it over-fit. Set the split with `set_holdout`.
- **Don't over-specify.** An entry pinned to an exact string or a brittle regex
  is easy to satisfy in a way that doesn't generalise; prefer a `predicate` or
  a `rubric` that captures the *behavior*, not the surface form.

The full anti-overfitting design — why the holdout is query-budgeted (Ladder /
Thresholdout), what leakage restriction means, how the generalization gap is
read — is in [OVERFITTING.md](../../docs/design/OVERFITTING.md). The builder's
job is to make the split easy to set and to remind the operator it is the
insurance against a board the proposer can game.

## The copilot's operating contract — DRAFT, then apply

The board builder edits a **draft board**, never the live epoch's board
directly. The copilot's tools (conceptual):

| Tool | Edits the draft… |
|---|---|
| `edit_board_entry` | add / edit / remove one entry (input, expectation, tags, weight) |
| `add_judge` | declare a process judge on an entry (name, mode, body, severity) |
| `set_weights` | the loss knobs above + the gate thresholds |
| `set_holdout` | the train/holdout split (`holdout`-tagged ids and/or fraction) |
| `validate` | (read-only) re-runs board validation (ids unique + filesystem-safe, per-kind fields present, expectation `reads` valid for the kind, judge slugs unique, `disable_drift` tokens resolve) |

The loop on every operator request:

1. **Edit the draft** with the matching tool.
2. **Preview** — show the changed entry / judge / weight and any `validate`
   warning (a leniency smell, a duplicate id, an unanchored regex).
3. **State the consequence** — the board is part of the frozen contract, so
   applying it **rolls the epoch** (generations answering a different board are
   not comparable). Batch board changes so the epoch rolls once.
4. **Apply only on explicit confirmation**, and never apply a draft that fails
   `validate`.

The board copilot, like the tournament copilot, **never starts a live `zicato
evolve`** — it produces a validated board, and the live run is the operator's
separate explicit go-ahead (see [`../AGENTS.md`](../AGENTS.md)).

## A good board build

- **Design for discrimination first.** A board that can't tell candidates apart
  is worse than a small one that can — cut dead-weight entries, target the
  sensitive band, run `zicato health`.
- **Put the behavior on the right axis.** Product correctness → an
  `expectation`; trajectory quality → a `judge`. Both when you care about both.
- **Shape the loss to the objective with the *minimal* knob change**, then hand
  the numeric calibration to `zicato-tune-scoring`.
- **Hold out ~30%** and prefer behavior-capturing matchers over brittle exact
  strings, so the proposer can't memorise its way to a promotion.
- **Batch edits and apply once** — every board change rolls the epoch.
