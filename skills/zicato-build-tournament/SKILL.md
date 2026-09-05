---
name: zicato-build-tournament
description: The tournament-builder copilot's guide to helping an operator assemble a whole zicato evaluation contract through a GUI — structure, field_size/replicates, per-structure params, the board & train/holdout split, the overfitting block, the proposer (quality/screen/memory), the weights, and the promote gate incl. the evidence gate — as a DRAFT edited through tools and applied only on confirmation. Use when a copilot is walking an operator through building a tournament. Teaches the consequence-forward discipline (every choice carries a COST in board-runs and a CONTRACT impact — applying rolls the epoch) AND the statistical-soundness discipline (the margin must clear the measured noise floor; preflight the draft before burning rounds). Defers structure choice to zicato-design-tournament-structure, board craft to zicato-build-board, holdout detail to OVERFITTING.md, proposer to zicato-design-proposer, gate weights to SCORING.md.
---

# Building a zicato tournament (copilot guide)

The copilot's model comes exclusively from `models.roles.builder` (falling
back to the named `evaluation` engine). `builder.json` selects only builder
skills and theme; do not put model, endpoint, credential, or `call_llm` fields
there. If no builder engine resolves, chat is disabled while the deterministic
form remains available.

This skill is for the **tournament-builder copilot** — the agent that sits
beside an operator in the builder GUI and helps them assemble a whole
evaluation **contract**: a tournament structure, its params, the board and its
holdout split, the proposer, and the promote gate. It is the *workflow* skill;
it does NOT restate the design knowledge it composes. Defer:

- **Which structure, and its params** → [`zicato-design-tournament-structure`](../zicato-design-tournament-structure/SKILL.md)
  (gauntlet / racing, or the experimental swiss / single_elim / double_elim
  under `experimental.tournament_structures`; `field_size`,
  `replicates`, `rounds_n`, `eta`, `board_fraction`; the contract-roll rule).
- **The board, judges, and weighted loss** → the sibling
  [`zicato-build-board`](../zicato-build-board/SKILL.md).
- **The train/holdout split, the Ladder budget, leakage** →
  [OVERFITTING.md](../../docs/design/OVERFITTING.md).
- **The proposer (the workspace's runtime block, and its skills)** →
  [`zicato-design-proposer`](../zicato-design-proposer/SKILL.md).
- **The gate (promote_margin, monotonicity) and the scalar** →
  [SCORING.md](../../docs/design/SCORING.md).

Design docs: [TOURNAMENT-STRUCTURES.md](../../docs/design/TOURNAMENT-STRUCTURES.md),
[EPOCHS-AND-JOURNALING.md](../../docs/design/EPOCHS-AND-JOURNALING.md). See
[`../../AGENTS.md`](../../AGENTS.md) for the operating rules every skill assumes.

## The one discipline: be consequence-forward

Every builder choice has TWO consequences the operator cannot see from the
control alone. Surface BOTH on every change — never let the operator `apply`
a draft without having seen them:

1. **COST — how many board-runs this contract will spend per round.** Runs are
   the expensive unit (each is a full target execution under the
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
   leaves it unset — the noise-aware base default is `2` (gauntlet, swiss, and
   both elim brackets inherit it); only `racing` pins `1`, because its
   replication is intrinsic to the escalating board slices — and `estimate_cost`
   reads those same per-structure defaults from one source of truth
   (`selection.registry`), so the number it shows matches what the run will
   actually spend even before the operator sets `replicates`. A deterministic
   harness can pin `"replicates": 1` for the historical single-run duel. Always
   defer the exact schedule arithmetic to
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
emits multiple challengers worth comparing; use `racing` for a field. `swiss`,
`single_elim` and `double_elim` are experimental and need
`set_experimental(tournament_structures=True)` first. Set the structure with
`set_structure`.

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

Defer to [`zicato-design-proposer`](../zicato-design-proposer/SKILL.md). Every
candidate is a **Foe episode**, declared in the workspace's own `proposer`
block: it reads the parent snapshot, edits a disposable copy of it, checks
its own work, and returns the hypothesis that explains the change. The
copilot's one proposer lever is therefore the **proposer directory** — drop
`skills/*.md` to steer how it reasons (grounding rules, house style, a
checklist) without changing what it may do, which the grants and the tool
list decide. Remind the operator of the model rule: the episode's model
should differ from the optional target-side model. Set it with
`set_proposer`; named engine overrides live under `models.roles.proposer`.
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

## Statistical soundness — the knobs that decide whether a verdict means anything

Cost and the epoch-roll are the *visible* consequences. The invisible one is
whether the contract can **out-signal its own noise** — a contract that cannot
produces verdicts decided by re-roll jitter, and every "win" is suspect. Four
knob families carry this, and the copilot should teach them honestly:

- **The margin must clear the measured noise floor.** `promote_margin` is a
  noise threshold: a duel decided by the margin alone cannot distinguish a real
  improvement from an A/A re-roll unless the margin exceeds the measured
  `max |Δ|` of the champion duelling itself. Run the **`preflight`** tool (or
  `zicato board preflight` / `zicato board audit` on the CLI) to measure the
  floor and the degradation signal; `validate` then flags a margin at/below the
  floor at `refuse` severity whenever the evidence gate is off. Recommend-only
  — but a REFUSE verdict means "fix the noise or the board before burning
  rounds", not "click apply anyway".
- **The evidence gate buys soundness, not power.** The Bradley–Terry pre-gate
  (`promote_confidence_threshold` + `promote_confidence_replicates`, set via
  `set_param`) refuses to crown until P(challenger > champion) clears the bar
  AND the rating CIs separate. It stops noise-crownings; it does NOT make a
  weak signal strong. CI separation on a near-tie needs replicated evidence —
  give it a realistic budget (the scaffold uses 32) and price it: each
  replicate is a fresh board sweep for BOTH contestants, so the meter's
  `crowning-confirm` line (~budget × 2 × board, per confirmed crowning) is
  usually the largest term on the bill.
- **The screen is veto-first tryouts, not a ranking.** `set_screening` runs
  each best-of-N slate candidate on a small rotating train panel BEFORE
  selection and disqualifies only confirmed catastrophic regressions; the
  critic still chooses among the survivors. It cheaply removes obvious losers
  from the field — it never nudges the ordering unless the operator lets it
  (`veto_only=false`).
- **The placebo is the gate-discrimination control.** `random_baseline_every_n`
  (via `set_holdout`) fields one deliberately no-op challenger every N rounds.
  The gate MUST reject it — a promoted placebo is the alarm that the gate
  cannot tell signal from noise and recent promotions are suspect. Cheap
  insurance (one amortized duel every N rounds) on any long run.
- **Process exemplars widen what the proposer may see — opt in with eyes
  open.** `set_proposer_quality(process_exemplars=N)` shows the proposer up
  to N mechanically-redacted event windows per round (how a detected failure
  *unfolds* — the wandering plan, the looping tool call), extracted from the
  champion's train-slice telemetry. Redaction is enforced in code (no entry
  ids, no task text, no model outputs), the extraction is free (read-side
  only — no cost-meter impact), and the epoch rolls on any change — but
  unlike the screen this knob touches the overfitting boundary directly,
  which is why the scaffold leaves it at 0. Be honest with the operator:
  enable it only under the harm-detection runbook in
  `docs/design/PROCESS-EXEMPLARS.md` §5 — keep the placebo arm on, watch the
  `generalization_gap` health finding every round, and set the knob back to
  0 (rolling the epoch, rotating the holdout) if the gap widens while train
  keeps improving.

## The copilot's operating contract — DRAFT, then apply

The builder edits a **draft contract**, never the live epoch directly. The
copilot's tools (conceptual builder surface):

| Tool | Edits the draft… |
|---|---|
| `set_structure` | the tournament structure |
| `set_param KEY=VALUE` | one structure param (`field_size`, `replicates`, `rounds_n`, `eta`, …) — also the evidence gate (`promote_confidence_threshold`, `promote_confidence_replicates`; value `null` removes a key) |
| `set_holdout` | the whole anti-overfitting block: the split (fraction and/or `holdout`-tagged ids), the Ladder governor (`ladder: {enabled, threshold, budget, noise_scale}`), `rotate_holdout`, `min_board_size_for_split`, `restrict_proposer_visibility`, the placebo cadence (`random_baseline_every_n`), the refresh ceiling (`max_generations_per_contract`; 0 clears) |
| `set_proposer` | the proposer dir / tier |
| `set_proposer_quality` | best-of-N slate size + the self-critique pass (`best_of_n`, `critique_enabled`) + the opt-in redacted process-exemplar channel (`process_exemplars`; 0 = off — see the soundness note above) |
| `set_screening` | the pre-tournament candidate screen (`entries`, `veto_only`) — composes with `set_proposer_quality` on the same block |
| `set_experiment_memory` | cross-epoch experiment-memory transfer (`cross_epoch`) |
| `set_weights` | the loss-shaping weights — the scalars (`pass_weight`, `default_judge_weight`, `plan_revision_weight`, `task_failure_weight`, `not_completed_weight`) and the wholesale mappings (`severity_weights`, `per_kind_weights`, `per_judge_weights`); the per-channel coefficients are `set_namespace_weights`; also surfaced in `zicato-build-board` |
| `set_namespace_weights` | the multi-objective namespace coefficients + the `diff_complexity_weight` parsimony term |
| `set_gate` | `promote_margin`, pass-rate monotonicity (+ its scope), `namespace_monotonicity`, the integrity blocks (`block_on_containment_violation`, `block_on_gate_contradiction`), and the regression-suite pre-gate (`regression_gate_enabled` / `_test_command` / `_timeout_s`) |
| `edit_board_entry` / `remove_board_entry` / `add_judge` / `remove_judge` / `set_board_meta` / `set_brief` | the board + its meta header + brief (deep craft in `zicato-build-board`); all reachable from the GUI's board editor too — see the note below |
| `estimate_cost` | (read-only) board-runs-per-round for the current draft, incl. the evidence-gate confirm budget, screen runs, best-of-N evaluation calls, and the placebo line |
| `validate` | (read-only) advisory warnings, incl. the statistical margin-vs-noise-floor rule (`refuse` severity when a measured floor is known and the evidence gate is off) |
| `preflight` | (read-only, spends the small K-draw measurement budget) measures the DRAFT contract's A/A noise floor + degradation signal against the registered target; verdict `ok`/`warn` (saturated — every probe scored identically)/`inert` (the probes moved nothing while the A/A draws varied — the achievable signal is UNMEASURED because the probe was inert; re-probe a representative point)/`refuse`, plus a WARNING-class margin-window check naming which side of the floor/signal pair the margin fell outside of (the upper comparison is degradation headroom and does not bound improvement — issue #119), plus a holdout-feasibility note when the board is split. Recommend-only; degrades honestly when the workspace has no registered target. CLI equivalents: `zicato board preflight`, `zicato board audit` |
| `preview_apply` | (read-only) dry-run: the diff, the predicted contract hash, the cost |
| `fork` / `switch` / `list_drafts` | NAMED draft slots — the fork/compare lifecycle. `fork name` snapshots the working draft into a slot and binds the session to it; `switch` moves between slots with their state intact. Iterating on contract variants never writes anything — only `apply` does |
| `revert_to_live` / `undo` | the restore lifecycle. `revert_to_live` discards the session's edits and restores the running contract in place (GUI: the slot strip's two-click Reset-to-live); `undo` steps back one write op off a bounded per-session history shared by the form and the chat (GUI: the slot strip's Undo, which surfaces a "nothing to undo" note on an empty history) |
| `compare name_a name_b` | (read-only) keyed diff between any two drafts (`session` = the working draft, `live` = the running contract, or a slot name): differing contract-canonical scoring keys with both values, board ids added/removed/changed, brief, proposer |
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
  go-ahead (the live-run gate — see [`../../AGENTS.md`](../../AGENTS.md) and
  `zicato-evolve`). The builder produces a ready contract; it does not run it.
- **It batches contract changes.** Because every contract edit rolls the epoch
  on apply, the copilot collects the operator's structure + board + holdout +
  proposer + gate changes into ONE draft and applies once, rather than rolling
  the epoch on each tweak.

**The direct GUI.** Every write/lifecycle op above also has a form control in the
builder view — the weight mappings (severity / per-kind / per-judge, each posting
the WHOLE mapping) with add-key rows for new judge/namespace keys, the gate's
namespace-monotonicity map, the overfitting refresh ceiling, the proposer picker
(discovered dirs + no-directory + free-text path), and the slot strip's
reset/undo. GUI coverage is machine-pinned by `tests/test_builder_gui_coverage.py`
(the one standing exception is `add_judge`, which the board editor covers through
the whole-entry `edit_board_entry` round-trip). The board is authored through a
full **board editor** (the flagship GUI): a per-entry inline accordion covering
the whole schema for its kind, an expectation sub-form, a judges list editor, a
board-meta panel (`disable_drift` + `judge_only`), a paste-JSONL import, and a
proposer-brief editor — each driving the SAME op as the copilot tool. The forms
carry no client validation twin; a server `ValueError` renders verbatim inline.
Deep board craft lives in `zicato-build-board`.

## A good build session

- **Start at gauntlet + a small discriminating board.** Add field-structure,
  replicates, and a holdout only once there is a real field and a real
  over-fitting risk to defend against.
- **Preflight before the first run.** One `preflight` call answers whether the
  contract can out-signal its noise at all; a `refuse` verdict caught in the
  builder is rounds not wasted.
- **Show cost before every commit.** The operator should never be surprised by
  the board-run bill; `estimate_cost` is cheap and read-only — call it freely.
  With the evidence gate on, point at the `crowning-confirm` line explicitly —
  it is usually the biggest number on the meter.
- **Treat the holdout as the over-fitting insurance**, not a free add — it
  costs confirmation runs and has its own query budget (OVERFITTING.md). Default
  ~30%, adjust to the board's size and noise.
- **Say "this rolls the epoch" before every apply**, and batch changes so the
  epoch rolls once.
- **Never launch the loop from the builder.** Hand the operator a validated,
  cost-estimated contract; the live run is their explicit call.
