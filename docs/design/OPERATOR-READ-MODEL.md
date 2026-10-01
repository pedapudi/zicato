# Operator read model

`zicato.query` owns the business projections rendered by operator interfaces.
Canonical workspace files remain the source of truth; the analytical index is
only a rebuildable accelerator. HTTP handlers serialize query results, while
the browser renders served decisions rather than reconstructing them.

## Contracts

Wire spellings are stable and declared with `TypedDict` payloads in
`zicato.query.contracts`. `ENDPOINT_PAYLOADS` inventories every JSON GET and
assigns it a collection, detail, or proposal-episode-export contract; a
correspondence test fails the build when a row of the dashboard's
`READ_ENDPOINTS` table has no entry. Contracts live
at the query boundary rather than in the HTTP driver. An optional key means the
information is unavailable; it is never a second spelling of a key that is
already declared.

`SnapshotPayload` declares the runtime snapshot and `LivenessPayload` its
liveness block. Liveness carries `state`, optional timestamps, and `epoch_id` while live. The
server folds the clock and active scope; clients compare the served epoch id
with the viewed epoch. A liveness block that carries no epoch id is read as a
single-epoch workspace.

## Read rules

- A decision is derived once in `zicato.query` and serialized unchanged as
  `decision` plus its presentation-ready `decision_label`. Candidate axes,
  lineage, epoch feeds, and browser views consume those fields.
- `lineage.json` alone owns generation parentage and tri-state promotion.
  Experiment outcomes are journal detail, never a topology fallback.
- Index absence or staleness degrades to canonical reads, or to an empty
  result carrying a `note`, never a competing verdict.
- Composite views read each workspace source once and pass the result through
  their component builders. `build_environment` and `build_snapshot`, for
  example, capture the runtime inputs once (`RuntimeInputs.capture`) and hand
  that capture to the workspace-identity, liveness, and run-log builders.
- The round timeline owns settled and in-flight rounds, embedded tournament
  records, projected standings, and gate state. The browser does not join an
  active envelope or infer a carried champion.
- Whether an in-flight run record still counts is decided once, per record,
  on the server, and served on the record as `fresh`. One of the two gates
  behind that verdict asks whether the worker process still exists, which only
  the worker's own host can answer. A client consumes the served verdict, and
  reaches for the timestamps only against a server that sends no `fresh` field.
- Live projections mark and populate only while the served liveness verdict
  reads live. A workspace that has stopped still serves its durable structure
  with the present-tense layer withheld, so post-mortem reads stay honest.
- The supervisor serves operational state, liveness, controls, and parity
  views. Analytical projections belong to the Python query service.
- No-op updates retain digest equality so the console does not rebuild.

A new JSON endpoint must enter `ENDPOINT_PAYLOADS` in the same change. A
contract addition must preserve current JSON keys and update renderer fixtures.
