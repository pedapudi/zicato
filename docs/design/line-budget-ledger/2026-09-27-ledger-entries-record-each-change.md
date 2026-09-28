# Ledger entries record each change's own delta

| Measurement | Delta |
|---|---:|
| Total | +127 |
| Production | 0 |
| Production logic | 0 |

The line-budget tool reads one entry file per change and derives each limit
from the starting limit plus the recorded deltas. The check compares a
change's entries with the tree's movement since its fork point. The added
lines are the entry parser, the fork-point comparison, the digest check on
the closed table of running totals, and an end-to-end test module that merges
two independently recorded branches. Removing the committed per-subsystem
table and its writer offsets part of the addition.
