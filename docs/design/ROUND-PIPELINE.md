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

## Completion target

The orchestrator should become a dispatcher below 1,000 lines. Extraction must
leave no replacement module above that threshold and must preserve the exact
convergence and decision-procedure oracles. Remaining cohesive owners are the
field evaluation strategy, round health/reporting, and baseline lifecycle. A
move is complete only after its callers import the new owner directly and the
old symbol is deleted.
