# Ledger entries record each change's own delta

| Measurement | Delta |
|---|---:|
| Total | +453 |
| Production | 0 |
| Production logic | 0 |

The line-budget tool reads one entry file per change and derives each limit
from the starting limit plus the recorded deltas. The check requires each
limit to equal the tree's measurement and holds the entries, the starting
limits, and the closed table of running totals to their values at the fork
point. The added lines are the entry parser, those checks, and an end-to-end
test module covering independent merges, counting-rule changes, and merges
whose logic counts do not add up. Removing the committed per-subsystem table
and its writer offsets part of the addition.

The entry is one line below this change's own movement of +454 total lines.
The closed table's last total row ends at 470,525 while the limit before this
change was 470,524, because one change lowered the limit by a line without a
row. The starting limit takes the closed table's value, and this entry removes
that line of unused room.
