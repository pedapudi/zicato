# Operator read model

`zicato.query` owns the business projections rendered by operator interfaces.
Canonical workspace files remain the source of truth; the analytical index is
only a rebuildable accelerator. HTTP handlers serialize query results, while
the browser and terminal render served decisions rather than reconstructing
them.

## Contracts

Wire spellings are stable and declared with `TypedDict` payloads in
`zicato.query.contracts`. `ENDPOINT_PAYLOADS` inventories every JSON GET and
assigns it an object, collection, detail, or runtime contract. Contracts live
at the query boundary, not in the HTTP driver. Optional keys mean genuinely
unavailable information, not an alternate spelling.

The first declared envelope is the runtime snapshot and its liveness block.
Liveness carries `state`, optional timestamps, and `epoch_id` while live. The
server folds the clock and active scope; clients compare the served epoch id
with the viewed epoch. A missing epoch id retains the legacy single-epoch
tolerance.

## Read rules

- A decision is derived once in `zicato.query` and serialized unchanged.
- `lineage.json` alone owns generation parentage and tri-state promotion.
  Experiment outcomes are journal detail, never a topology fallback.
- Index absence or staleness degrades to canonical reads, never a competing
  verdict.
- Composite views read each workspace source once and pass the result through
  their component builders. In particular, `build_environment` scopes its one
  lineage feed for `build_epoch_view` instead of walking generations again.
- The supervisor serves operational state, liveness, controls, and parity
  views. Analytical projections belong to the Python query service.
- No-op updates retain digest equality so neither renderer rebuilds.

A new JSON endpoint must enter `ENDPOINT_PAYLOADS` in the same change. A
contract addition must preserve current JSON keys and update renderer fixtures.
