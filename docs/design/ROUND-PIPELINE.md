# Round pipeline decomposition

One evolve round runs as a pipeline of five phases, each with a named owner
module rather than a set of orchestrator callbacks:

1. **Prepare** resolves the frozen epoch inputs, runtime dependencies, parent
   generation, mutable surface, and the tournament strategy.
2. **Propose and apply** produces the candidate field: one validated child
   tree per slot the strategy requests, inside the restricted visibility
   envelope.
3. **Run** executes every matchup the strategy schedules through the
   board-unit runner and the promotion gate.
4. **Gate** resolves the field verdict: holdout confirmation of a crowning,
   containment and integrity checks, and operator overrides.
5. **Decide** records the settlement receipt, then outcomes, lineage and the
   champion pointer, advancing the champion only on a settled promotion, and
   refreshes derived projections.

`PreparedRound` (`zicato.evolve.generation_phase`) is the immutable handoff
created during prepare. It carries round identity and already-resolved
dependencies; it does not own mutable results. `FieldRound` wraps it with the
field size and evaluation settings derived from it. Phase outputs remain
explicit values, so evaluation and persistence do not communicate through
hidden session state.

## Module ownership

`zicato.orchestrator` is a 14-line module that re-exports the public entry
points (`evolve_once`, `evolve_n_rounds`, `ensure_epoch_for_contract`,
`EvolveRoundOutcome`). It owns no phase logic. The executable round is divided
by behavior:

| Owner | Responsibility |
|---|---|
| `zicato.evolve.round_entry` | round entry point: validates workspace ownership, prepares the round, builds the strategy, and hands the `PreparedRound` to the field pipeline |
| `zicato.evolve.field` | runs the four post-prepare phases in order |
| `zicato.evolve.field_candidates` | propose and apply: assembles the candidate field through `zicato.evolve.candidate_batch` |
| `zicato.evolve.field_execution` | run: opens the tournament envelopes and runs every scheduled matchup |
| `zicato.evolve.gate` | gate: holdout confirmation, overrides, and integrity blocks |
| `zicato.evolve.settlement` | decide: builds and commits the settlement, publishes views, closes the round |
| `zicato.evolve.decision_support` | shared decision inputs and outcome shaping |
| `zicato.evolve.round_prepare` | calibration, preflight, and health assessment |
| `zicato.evolve.round_baseline` | mutation snapshots and baseline lifecycle |
| `zicato.evolve.round_reporting` | round log, health inputs, and report regeneration |
| `zicato.evolve.persist` | terminal outcomes that never enter a tournament, and the shared round epilogue |

Every tournament structure, including the one-challenger gauntlet, enters the
same pipeline. The strategy decides only the field width
(`SelectionStrategy.field_size`) and the matchup topology. Field width one
differs from a wider field in two rules, both stated in
`zicato.evolve.field_candidates`: a single slot that exhausted its proposer
retries settles as a validation-rejection round, and the random-baseline
placebo arm runs as a separate duel after settlement because a one-slot slate
has no room for it.

The phase modules are not generic helper containers. Each owns one step of
the round, and their control flow preserves the visible order of heartbeat
transitions, `RoundLog` events, cache writes, gate evaluation, and settlement.
Moving arbitrary line ranges into generic helpers would reduce file size
without reducing state or coupling. A further extraction is justified only
when it introduces a typed phase result that removes locals from a phase.
Forwarding the same argument set through a new helper does not qualify.

## Generation phase boundary

`zicato.evolve.generation_phase` owns generation coordinates: champion
resolution, safe abort-time parent lookup, next-id allocation, snapshot-store
resolution, and mutable-tree resolution. Callers import that owner directly.

The champion is the one recorded by the epoch's settlement receipts
(`zicato.epoch.settlement_receipt.recorded_champion`); an epoch with no
recorded champion has no baseline yet, and resolution raises
`FileNotFoundError`. Next-id allocation reads the generation ids on disk.
Snapshot paths always resolve through the configured generation store, and
mutable subpaths fall back to the whole snapshot only when an adapter declares
none.

## Structural constraint

Every phase owner stays below 1,000 lines, owns one step, and exposes named
values rather than mutable collections of callbacks. Every structural change
must keep the convergence known-answer test
(`tests/test_convergence_known_answer.py`) and the decision-procedure power
test (`tests/test_decision_procedure_power.py`) passing unchanged.

Terminal round output is `EvolveRoundOutcome` (`zicato.evolve.round_api`).
Intermediate records (`CandidateField`, `FieldExecution`, `FieldVerdict`,
`RoundSettlement`, `_AppliedChallenger`) are dataclasses owned by the phase
that produces them. Dictionaries remain only at serialization and external
payload boundaries.
