# Metric name form described in the proposal schema

| Measurement | Delta |
|---|---:|
| Total | +85 |
| Production | +11 |
| Production logic | +11 |

The `metric_name` property of the proposal schema in
`src/zicato/proposer/structured.py` gains a `description` stating the
`<namespace>:<metric>` form the parser requires; the one-line property
literal becomes an eleven-line dictionary, and the counter treats each line of
the literal as executable. The pass-rate section of the scored-targets block in
`src/zicato/proposer/prompts.py` grows by one line to send the pass-rate
forecast to `expected_pass_rate_delta` instead of offering `pass_rate` as a
movement name, which the parser refuses. The remaining 74 lines are a test in
`tests/test_metric_priorities.py` that parses every metric name the targets
block offers.
