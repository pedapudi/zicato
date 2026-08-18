# Line budgets

Line count is a deliberately blunt proxy for structural simplicity. The
simplification program constrains both tracked non-documentation lines and the
production subset, so deleting tests cannot conceal runtime growth. Detailed
measurement and verification instructions live in
[`docs/dev-guide/11-testing.md`](../dev-guide/11-testing.md#line-budget-gate).

## Measurement contract

The total counts newline characters in tracked files, excluding Markdown,
dependency lockfiles, and a fixed list of generated artifacts. Production is
the subset under the runtime package, crate source directories, integrations,
and the build hook; tests and assets remain outside that subtotal. The checker
owns the exact classifications.

The baseline and final ratchet use the same metric:

| Measurement | Baseline (`f9052dd`) | Current limit | Reduction |
|---|---:|---:|---:|
| Total | 408,547 | 408,018 | 529 |
| Production | 197,588 | 196,420 | 1,168 |

The earlier raw count of 425,755 included lockfiles and generated artifacts and
is retained in `.line-budget.json` for provenance; it is not the enforced
metric.

## Ratchet policy

There is no temporary allowance. A change exceeding either limit fails. A
deliberate increase must update the limit and record the previous value, signed
delta, new value, issue, and reason in this document. Reductions ratchet both
machine limits directly to the new measured totals.

Minification, concatenation, moving implementation into excluded paths,
checked-in generated replacements, or weakening tests do not qualify as
simplification. Any classification change receives the same review as a budget
increase.

## Deliberate increases

| Change | Previous | Delta | New | Reason |
|---|---:|---:|---:|---|
| Durable run-artifact capture (total) | 407,445 | +399 | 407,844 | Issue #12: deterministic inventory, persistence, grading contract, and regression coverage for emitted files. |
| Durable run-artifact capture (production) | 196,526 | +235 | 196,761 | Issue #12: bounded capture implementation and typed artifact surface. |
| Harmonograf web-port readiness (total) | 407,791 | +21 | 407,812 | Launch handle must not be returned before its web listener accepts; fixes the alternating connection-refused e2e failures. |
| Harmonograf web-port readiness (production) | 196,361 | +21 | 196,382 | Bounded accept-poll in the launcher; degrades to the JSONL-only handle on timeout. |
| Harmonograf readiness hardening (total) | 407,812 | +67 | 407,879 | Timeout path now stops the launched server before returning the no-op handle; /healthz replaces the TCP probe; two deterministic timeout regressions. |
| Harmonograf readiness hardening (production) | 196,382 | +6 | 196,388 | Handle-first construction and shutdown-on-timeout in the launcher. |
| Execution-tree stated statuses and delegation nesting (total) | 407,860 | +158 | 408,018 | Statuses from invocation boundary-exit and cancel events; delegation observations nest under the delegating invocation's stated id; regression coverage for deep agent/tool mixtures. |
| Execution-tree stated statuses and delegation nesting (production) | 196,369 | +51 | 196,420 | Boundary-exit and cancel status handling plus the explicit delegation parent edge in the transcript reader. |
