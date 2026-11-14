# Optional-failure reporter bound before the guarded block

| Measurement | Delta |
|---|---:|
| Total | +55 |
| Production | +4 |
| Production logic | 0 |

`_emit_harness_loaded` imports `report_optional_failure` before the guarded
block instead of inside it, with a three-line comment; the remaining 51 lines
are the regression test that refuses one import in the block and requires the
handler to report rather than raise `NameError`.
