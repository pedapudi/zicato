# Round pipeline decomposition

One evolve round is a phase pipeline, not a bag of orchestrator callbacks:

1. **Prepare** resolves the frozen epoch inputs, runtime dependencies, parent
   generation, and mutable surface.
2. **Propose and apply** produces a validated child tree inside the restricted
   visibility envelope.
3. **Evaluate** runs either the gauntlet strategy or a multi-challenger field.
4. **Decide** applies the statistical gate, holdout confirmation, containment,
   and operator controls.
5. **Persist** records outcomes before lineage, advances the champion only on a
   settled promotion, and refreshes derived projections.

`RoundSession` is the immutable handoff created during prepare. It contains
round identity and already-resolved dependencies; it does not own mutable
results. Phase outputs remain explicit values so evaluation and persistence do
not communicate through hidden session state.

## Module ownership

`zicato.orchestrator` is the 118-line dispatch surface. It owns no phase logic.
The executable round is divided by behavior:

| Owner | Responsibility |
|---|---|
| `zicato.evolve.gauntlet` | single-challenger round strategy |
| `zicato.evolve.field` | multi-challenger field strategy |
| `zicato.evolve.decision_support` | shared decision inputs and outcome shaping |
| `zicato.evolve.round_prepare` | calibration, preflight, and health assessment |
| `zicato.evolve.round_baseline` | mutation snapshots and baseline lifecycle |
| `zicato.evolve.round_reporting` | round log, health inputs, and report regeneration |
| `zicato.evolve.persist` | outcome, lineage, marker, and journal ordering |

The gauntlet dispatches to the field strategy only after the resolved selection
strategy requests a field wider than one. The field module never imports the
gauntlet module, so the two structures cannot accidentally share mutable
strategy state. Shared behavior is imported from a narrow owner instead of
copied between the strategies.

The two strategy modules are deliberately not generic phase containers. Each
has one asynchronous entry point and owns one complete tournament structure.
Their long control flows preserve the visible order of heartbeat transitions,
RoundLog events, cache writes, gate evaluation, and settlement. Moving arbitrary
line ranges into generic helpers would reduce file size without reducing state
or coupling. A further extraction is justified only when it introduces a typed
phase result that removes locals from the strategy, not when it merely forwards
the same argument set.

## Generation phase boundary

`zicato.evolve.generation_phase` owns generation coordinates: champion-marker
resolution, safe abort-time parent lookup, next-id allocation, snapshot-store
resolution, and mutable-tree rebasing. Callers import that owner directly.
There are no orchestrator aliases or monkeypatch forwarding seams.

The fallback champion rule is unchanged: a non-empty marker wins; without one,
the greatest generation directory under the established ordering wins. Next-id
allocation considers only `vN` identifiers. Snapshot paths always resolve via
the configured generation store, and mutable subpaths fall back to the whole
snapshot only when an adapter declares none.

## Structural constraint

The dispatcher stays below 1,000 lines and owns no business decisions. Large
tournament strategies may remain separate because combining their different
tails would hide the crowning and persistence invariants. Supporting phase
owners stay below 1,000 lines and expose named values rather than mutable bags
of callbacks. Every structural change must preserve the exact convergence and
decision-procedure oracles.

`RoundSession` is the typed prepare result shared with generation-coordinate
helpers. Terminal strategy output is `EvolveRoundOutcome`; intermediate
cross-strategy records (`_AppliedChallenger`, `_CrowningHoldout`) are immutable
dataclasses owned by propose/apply and gate respectively. Dictionaries remain
only at serialization and external payload boundaries.
