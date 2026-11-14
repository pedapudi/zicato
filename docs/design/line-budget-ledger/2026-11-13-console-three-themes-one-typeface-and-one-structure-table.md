# Console: three themes, one typeface, and one structure-figure table

| Measurement | Delta |
|---|---:|
| Total | -1,103 |
| Production | -935 |
| Production logic | -759 |

The console's sixteen colour themes become three and its twelve typeface
pairings become one, so thirteen theme token blocks, twelve typeface rules,
the typeface picker and its popover styles are deleted. The picker behaviour
that two pickers shared is folded into the one that remains. One table maps a
tournament structure and a surface to its figure builder, one function words a
structure label, and one helper picks the settled tournament record, each
replacing a copy held at several sites. Six exports that nothing mounted are
deleted. The stylesheet falls by 470 lines and the JavaScript by 465; the
browser tests fall by 168 lines, the tests of the removed themes and typefaces
less the tests added for the table, the label and the helper.
