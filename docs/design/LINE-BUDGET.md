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
| Total | 408,547 | 408,138 | 409 |
| Production | 197,588 | 196,434 | 1,154 |

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
| Execution-tree error-visibility regressions (total) | 408,018 | +32 | 408,050 | Renderer tests pinning failed/cancelled styling on branch, leaf, and tool nodes and live error repaint of the run rail; the accompanying recursive rail signature reduced production by 2 (ratcheted to 196,418). |
| Lexical static-file guard (total) | 408,050 | +86 | 408,136 | Issue #231: first coverage of the static guard — a traversal-refusal test and a symlink-staged bundle test — plus non-emptiness floors on the package-tree walks the structural pins depend on. |
| Lexical static-file guard (production) | 196,418 | +16 | 196,434 | Issue #231: lexical normalize-and-reject in `_serve_static` and the unresolved-first relative path in `_rel_file`. |
| Parity macOS bash 3.2 compatibility (total) | 408,136 | +2 | 408,138 | Empty-array-safe expansions plus a glob-safe comma-list split in tools/parity.sh; the ladder now runs on a stock macOS shell. |
