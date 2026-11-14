# Outcome feedback and reason codes in the experiment memory

| Measurement | Delta |
|---|---:|
| Total | +1,531 |
| Production | +548 |
| Production logic | +368 |

Two changes to the proposer's experiment memory share this entry.

A restricted memory now renders a rejection's reason code instead of the
stored reason, which can name holdout entries. The added lines are the table
mapping each stored reason to its code and the function that applies it.

The opt-in `experimental.outcome_feedback` records, at settlement, each
candidate's per-metric movements, training-task pass changes and the standard
error of its paired scalar difference, and renders them banded. The added
lines are the settlement function, three scoring helpers, the reader that
bands the recorded values, the renderer with its closed set of printable
metric names, the two test files, and the encoder that omits the four
new outcome fields when they are unset. The recombination pair selector now
calls the shared pass-change helper in place of its own loop. The grader, the
magnitude buckets and the per-entry scorer are reused unchanged.
