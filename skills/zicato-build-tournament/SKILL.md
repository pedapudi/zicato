---
name: zicato-build-tournament
description: The tournament-builder copilot's guide to helping an operator assemble a whole zicato evaluation contract through a GUI — structure, field_size/replicates, per-structure params, the board & train/holdout split, the proposer, and the promote gate — as a DRAFT edited through tools and applied only on confirmation. Use when a copilot is walking an operator through building a tournament. Teaches the consequence-forward discipline: every choice carries a COST (board-runs) and a CONTRACT impact (applying rolls the epoch), so always surface cost + the epoch-roll before apply. Defers structure choice to zicato-design-tournament-structure, board craft to zicato-build-board, holdout detail to OVERFITTING.md, proposer to zicato-design-proposer, gate weights to SCORING.md.
---

# Building a zicato tournament (copilot guide)

This skill is for the **tournament-builder copilot** — the agent that sits
beside an operator in the builder GUI and helps them assemble a whole
evaluation **contract**: a tournament structure, its params, the board and its
holdout split, the proposer, and the promote gate. It is the *workflow* skill;
it does NOT restate the design knowledge it composes. Defer:

- **Which structure, and its params** → [`zicato-design-tournament-structure`](../zicato-design-tournament-structure/SKILL.md)
  (gauntlet / swiss / single_elim / double_elim / racing; `field_size`,
  `replicates`, `rounds_n`, `eta`, `board_fraction`; the contract-roll rule).
- **The board, judges, and weighted loss** → the sibling
  [`zicato-build-board`](../zicato-build-board/SKILL.md).
- **The train/holdout split, the Ladder budget, leakage** →
  [OVERFITTING.md](../../docs/design/OVERFITTING.md).
- **The proposer (skill-composed default vs custom ADK agent)** →
  [`zicato-design-proposer`](../zicato-design-proposer/SKILL.md).
- **The gate (promote_margin, monotonicity) and the scalar** →
  [SCORING.md](../../docs/design/SCORING.md).

Design docs: [TOURNAMENT-STRUCTURES.md](../../docs/design/TOURNAMENT-STRUCTURES.md),
[EPOCHS-AND-JOURNALING.md](../../docs/design/EPOCHS-AND-JOURNALING.md). See
[`../AGENTS.md`](../AGENTS.md) for the operating rules every skill assumes.

## The one discipline: be consequence-forward

Every builder choice has TWO consequences the operator cannot see from the
control alone. Surface BOTH on every change — never let the operator `apply`
a draft without having seen them:

1. **COST — how many board-runs this contract will spend per round.** Runs are
   the expensive unit (each is a full inner-harness execution under the
   wall-clock budget). A rough model:

   ```
   board_runs_per_round ≈ field_size × replicates × duels_or_rungs
                          + holdout_confirm_runs
   ```

   where `duels_or_rungs` is the structure's schedule: a gauntlet is one duel;
   swiss is `rounds_n` pairings; an elim bracket is its bracket depth; racing
   is its rung count. The `holdout_confirm_runs` term is the extra runs the
   gate spends re-scoring the winner on the held-out slice (see "Board &
   holdout" below). **`replicates` defaults per structure** when the operator
   leaves it unset — swiss/single_elim/double_elim default to `2`,
   gauntlet/racing to `1` — and `estimate_cost` reads those same per-structure
   defaults from one source of truth (`selection.registry`), so the number it
   shows matches what the run will actually spend even before the operator sets
   `replicates`. Always defer the exact schedule arithmetic to
   `zicato-design-tournament-structure`; the copilot's job is to call
   `estimate_cost` and *show the number* before the operator commits.

2. **CONTRACT — applying the draft ROLLS THE EPOCH.** The structure, its
   params, the board, the proposer, the holdout split, and the scoring weights
   are all folded into the epoch's frozen contract hash. Changing ANY of them
   and applying closes the current epoch and opens a fresh one — generations
   selected under different rules are not directly comparable, so they must not
   share a lineage. This is the same roll the structure skill, the board skills,
   and the proposer skill each describe; the builder just makes it a single
   `apply`. **Say "this rolls the epoch" out loud before every apply.**

The copilot that surfaces cost and the epoch-roll on every change lets the
operator make an informed trade; the copilot that hides them produces surprise
bills and orphaned epochs.

## The decision walkthrough

Walk the operator through the contract in dependency order. At each step show
the running cost estimate and note whether the choice is contract-affecting
(all of these are).

### 1. Pick the structure

Defer the whole decision to
[`zicato-design-tournament-structure`](../zicato-design-tournament-structure/SKILL.md).
The short version the copilot offers: **start at gauntlet** (one champion, one
challenger, one duel); reach for a field-structure only once the proposer
emits multiple challengers worth comparing; use `racing` for a large field on
a budget, `swiss` when the operator wants a leaderboard, `single_elim` for a
quick winner. Set it with `set_structure`.

### 2. Set field_size and replicates — the noise lever

`field_size` is how many challengers the proposer must emit per round;
`replicates` is paired board-runs averaged per duel. **`replicates` — not
bracket shape — is the noise lever**: when a duel's verdict flips run-to-run,
raise `replicates` before reaching for a fancier structure. Both multiply the
cost directly (`field_size × replicates × …`), so this is where the operator
first feels the bill. Set them with `set_param field_size=…` /
`set_param replicates=…`; the per-structure defaults and rationale live in the
structure skill.

### 3. Set the per-structure params

`rounds_n` (swiss), `eta` / `board_fraction` / `rung0_board_size` (racing) —
each shifts cost and signal. Defer their meaning and defaults to the structure
skill. The copilot's contribution is to recompute `estimate_cost` after each
`set_param` so the operator sees the marginal cost of, say, adding a swiss
round.

### 4. The board & the holdout split

The board is the set of tasks every challenger is scored on (design its
entries/judges/weights in the sibling
[`zicato-build-board`](../zicato-build-board/SKILL.md)). For the *tournament*
builder, the load-bearing board decision is the **train/holdout split**:

- A slice of the board is tagged `holdout` and **withheld from the loop's
  promotion signal** during the round; the gate confirms the winner on it
  *afterward*. The point is to catch a challenger that won by memorising /
  over-fitting the train slice — its train score improves but its holdout
  score does not (the **generalization gap**). Default the holdout to
  **~30%** of the board.
- The holdout costs runs: the winning challenger is re-scored on the holdout
  entries (the `holdout_confirm_runs` term above), and replicated holdout
  confirmation under a Ladder/Thresholdout budget costs more. The whole
  cost/leakage/budget story — why the holdout is query-budgeted, what leakage
  restriction means, how the generalization gap is read — is in
  [OVERFITTING.md](../../docs/design/OVERFITTING.md). Defer the detail there;
  in the builder, just set the split and surface its cost.

Set it with `set_holdout` (the fraction and/or the explicit `holdout`-tagged
ids). Changing the split rolls the epoch like any board change.

### 5. The proposer

Defer to [`zicato-design-proposer`](../zicato-design-proposer/SKILL.md). The
**default** (no proposer dir) is already a **tool-using ADK agent** — it reads
the world (greps the mutable surface, reads the snapshot/journal) while it
reasons, so the copilot offers customization only when there is a reason. The
two opt-ins: a **skill-composed text shim** (drop `skills/*.md`, no code — the
contract-clean lever for pure *reasoning* changes, but it drops the default's
tools) versus a **custom ADK agent** (its own `model=` + the read-only tool
registry — when the operator wants to own the model or curate the tool subset
while keeping tools). Remind the operator of the Design-A model rule: a custom
proposer's model must differ from the harness model. Set it with `set_proposer`.
Editing the proposer or any of its skills rolls the epoch.

### 6. The gate

Defer the gate rules and the scalar to [SCORING.md](../../docs/design/SCORING.md):
the three-rule gate (scalar margin → pass-rate monotonicity → namespace
monotonicity), `promote_margin` as the noise floor a promotion must clear, and
the weighted loss. The builder's gate controls are `promote_margin` and the
monotonicity flags; the loss *weights* are set in the board builder via
`set_weights`. Raise `promote_margin` on a noisy board (e.g. heavy emulated
entries) so jitter doesn't flip a promotion. The pass-rate check also has a
**scope** (`pass_rate_monotonicity_scope`): `per_entry` (default — reject if any
champion-passed entry flips to fail, right for invariant/regression boards) vs
`aggregate` (reject only when the overall pass-rate drops, right for sampled
boards where a single noisy entry flip shouldn't veto a strictly-better
challenger). Disable the check entirely with `pass_rate_monotonicity=False`
(there is no `off` scope value).

## The copilot's operating contract — DRAFT, then apply

The builder edits a **draft contract**, never the live epoch directly. The
copilot's tools (conceptual builder surface):

| Tool | Edits the draft… |
|---|---|
| `set_structure` | the tournament structure |
| `set_param KEY=VALUE` | one structure param (`field_size`, `replicates`, `rounds_n`, `eta`, …) |
| `set_holdout` | the train/holdout split (fraction and/or `holdout`-tagged ids) |
| `set_proposer` | the proposer dir / tier |
| `set_weights` | the scoring weights and gate thresholds (also surfaced in `zicato-build-board`) |
| `estimate_cost` | (read-only) returns board-runs-per-round for the current draft |
| `validate` | (read-only) checks the draft resolves (structure valid, params well-typed, holdout tags exist, proposer imports) |
| `apply` | freezes the draft into the contract — **ROLLS THE EPOCH** |

The loop the copilot runs on every operator request:

1. **Edit the draft** with the matching `set_*` tool.
2. **Show the diff/preview** — what changed, the new `estimate_cost`, and any
   `validate` warning.
3. **State the consequences** — the new per-round cost AND "applying this rolls
   the epoch."
4. **`apply` only on explicit confirmation.** Never apply silently; never apply
   a draft that fails `validate`.

Two hard rules for the copilot:

- **It NEVER starts a live `zicato evolve`.** Building and applying a contract
  is design-time work; launching the loop is the operator's separate, explicit
  go-ahead (the live-run gate — see [`../AGENTS.md`](../AGENTS.md) and
  `zicato-evolve`). The builder produces a ready contract; it does not run it.
- **It batches contract changes.** Because every contract edit rolls the epoch
  on apply, the copilot collects the operator's structure + board + holdout +
  proposer + gate changes into ONE draft and applies once, rather than rolling
  the epoch on each tweak.

## A good build session

- **Start at gauntlet + a small discriminating board.** Add field-structure,
  replicates, and a holdout only once there is a real field and a real
  over-fitting risk to defend against.
- **Show cost before every commit.** The operator should never be surprised by
  the board-run bill; `estimate_cost` is cheap and read-only — call it freely.
- **Treat the holdout as the over-fitting insurance**, not a free add — it
  costs confirmation runs and has its own query budget (OVERFITTING.md). Default
  ~30%, adjust to the board's size and noise.
- **Say "this rolls the epoch" before every apply**, and batch changes so the
  epoch rolls once.
- **Never launch the loop from the builder.** Hand the operator a validated,
  cost-estimated contract; the live run is their explicit call.
