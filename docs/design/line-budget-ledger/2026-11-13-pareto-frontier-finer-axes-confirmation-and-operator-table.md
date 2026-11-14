# The Pareto frontier gains finer axes, a confirmation step, an operator table, and two opt-in consumers

| Measurement | Delta |
|---|---:|
| Total | +2,307 |
| Production | +830 |
| Production logic | +531 |

The frontier record compares candidates on up to eight judges and scored
metrics inside the weighted namespaces as well as on the namespaces, in the
same scalar points. One function in the scoring module splits a namespace
aggregate per metric by the formula the namespace aggregate already uses, and
the round's recorder adds the result to the aggregates it offers the record,
so `gen_score.json` and the gate are unchanged. A confirmation step
re-measures one member against the current champion through the existing
matchup runner, at the evidence-confirmation purpose and through the unit
cache, and refreshes or retires the member. `analysis.md` and `zicato epoch
close` print the members as a table. The record gains two
optional member fields and the `frontier_updated` event three; no command,
measurement purpose, or index column is added. 
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

A review of the change then narrowed it: the dimension axes apply only under
a contract that enables one of the two settings, a confirmation requires a
recorded win to replicate, the frontier pool keeps the current-reign
predicate, steering names only dimensions the champion can improve on, and
the trade-off table is not printed on an epoch roll. Those corrections
removed 9 executable lines and added 153 lines of tests.
