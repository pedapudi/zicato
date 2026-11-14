# The Pareto frontier gains finer axes, a confirmation step, an operator table, and two opt-in consumers

| Measurement | Delta |
|---|---:|
| Total | +2,154 |
| Production | +828 |
| Production logic | +540 |

The frontier record compares candidates on up to eight judges and scored
metrics inside the weighted namespaces as well as on the namespaces, in the
same scalar points. One function in the scoring module splits a namespace
aggregate per metric by the formula the namespace aggregate already uses, and
the round's recorder adds the result to the aggregates it offers the record,
so `gen_score.json` and the gate are unchanged. A confirmation step
re-measures one member against the current champion through the existing
matchup runner, at the evidence-confirmation purpose and through the unit
cache, and refreshes or retires the member. `analysis.md`, `zicato epoch
close`, and an epoch roll print the members as a table. The record gains two
optional member fields and the `frontier_updated` event three; no command,
measurement purpose, or index column is added. That part moves the total by
+1,150, production by +472, and production logic by +302.

Two experimental settings, both off by default, account for the rest.
`recombine_pool: "frontier"` draws the recombination slot's parents from
confirmed frontier members: the existing pair builder is split into the
helpers both pools share, the selector takes the members' win axes as
optional evidence, and one builder confirms the best-ranked pair's members,
at most four a round. `frontier_steering` appends one block to the proposer's
scored targets naming the dimensions no confirmed member improves. The mint,
the selection mode, and the gate are unchanged.

The added lines outside production are the tests and the eight lines the two
settings add to the reader snapshot.
