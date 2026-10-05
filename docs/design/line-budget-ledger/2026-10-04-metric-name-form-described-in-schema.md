# Metric name form described in the proposal schema

| Measurement | Delta |
|---|---:|
| Total | +10 |
| Production | +10 |
| Production logic | +10 |

The `metric_name` property of the proposal schema in
`src/zicato/proposer/structured.py` gains a `description` stating the
`<namespace>:<metric>` form the parser requires; the one-line property
literal becomes an eleven-line dictionary, and the counter treats each line of
the literal as executable.
