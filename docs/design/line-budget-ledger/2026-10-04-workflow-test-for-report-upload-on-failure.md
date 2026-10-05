# Workflow test for report upload on failure

| Measurement | Delta |
|---|---:|
| Total | +29 |
| Production | 0 |
| Production logic | 0 |

`tools/test_workflows.py` gains a test that every step running
`tools/verify.py` writes a report directory and that a later step in the same
job uploads that directory under `if: always()`, so a failed check still
publishes its report.
