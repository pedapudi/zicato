# Infra aborts counted per run when replicates fold

| Measurement | Delta |
|---|---:|
| Total | +207 |
| Production | +55 |
| Production logic | +21 |

The replicate scheduler counts infra-aborted runs before the fold and records
the count on the tournament result, and the fold takes its single-run fields
from the first replicate with an infra abort (issue #572). The other lines
are the tests and the documentation.
