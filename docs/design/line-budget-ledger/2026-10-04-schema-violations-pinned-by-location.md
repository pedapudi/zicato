# Schema violations pinned by location

| Measurement | Delta |
|---|---:|
| Total | +6 |
| Production | 0 |
| Production logic | 0 |

Two parser tests in `tests/test_proposer_structured.py` match the location
of an empty-array violation instead of the schema library's wording, and
each carries a comment stating why; the added lines are those comments.
