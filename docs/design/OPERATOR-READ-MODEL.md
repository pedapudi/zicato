# Operator read model

`zicato.query` owns the business projections rendered by operator interfaces.
Canonical workspace files remain the source of truth; the analytical index is
only a rebuildable accelerator. HTTP handlers serialize query results, while
the browser and terminal render served decisions rather than reconstructing
them.

## Contracts

Wire spellings are stable and declared with `TypedDict` payloads in
`zicato.query.contracts`. Readers return those types directly. Contracts are
added at the query boundary, not in the HTTP driver, so every renderer shares
them. Optional keys mean genuinely unavailable information, not an alternate
spelling.

The first declared envelope is the runtime snapshot and its liveness block.
Liveness carries `state`, optional timestamps, and `epoch_id` while live. The
server folds the clock and active scope; clients compare the served epoch id
with the viewed epoch. A missing epoch id retains the legacy single-epoch
tolerance.

## Read rules

- A decision is derived once in `zicato.query` and serialized unchanged.
- Readers prefer canonical files when promotion or lineage truth is involved.
- Index absence or staleness degrades to canonical reads, never a competing
  verdict.
- Composite views read each workspace source once and pass the result through
  their component builders.
- The supervisor reads only facts required for watchdog, notary, containment,
  liveness, and parity serving; it does not derive promotion truth.
- No-op updates retain digest equality so neither renderer rebuilds.

Additional endpoint payloads should acquire explicit contracts as their query
builders are consolidated. A contract addition must preserve current JSON keys
and update both renderer fixtures.
