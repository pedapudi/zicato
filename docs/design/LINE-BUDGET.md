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

| Measurement | Baseline (`f9052dd`) | Current limit | Net reduction |
|---|---:|---:|---:|
| Total | 408,547 | 418,360 | -9,813 |
| Production | 197,588 | 201,427 | -3,839 |

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
| Pi shipped-asset root (total) | 408,138 | +12 | 408,150 | Issue #238: the recurrence guard — a test pinning that the launcher leaves shipped-asset resolution to pi, and the live envelope lane rebuilt through the real env builder. |
| Pi shipped-asset root (production) | 196,434 | +5 | 196,439 | Issue #238: the env-builder docstring stating which root is process state, so the variable is not re-added. |
| Proposer input capture (total) | 408,150 | +750 | 408,900 | Issue #244: the locked append-only writer and tolerant reader, four capture sites, and the concurrency, retry, and degrade regressions. |
| Proposer input capture (production) | 196,439 | +286 | 196,725 | Issue #244: the capture module, per-site wiring, the slot coordinate, and the path accessors. |
| Proposer baseline losses and metric priorities (total) | 408,900 | +1,013 | 409,913 | Issues #243 and #247: the shared reserved-base filter and its allow-list regression, the calibration-band fallback and its degraded-probe and holdout regressions, the priority renderer, and the contract-priority suite. |
| Proposer baseline losses and metric priorities (production) | 196,725 | +587 | 197,312 | Issues #243 and #247: the own-code draw filter, the folded calibration read, the priority resolver and renderer, the calibration span guard, and the per-site wiring of the rendered block. |
| Unit provenance (total) | 409,913 | +642 | 410,555 | Issues #242 + #245: attempt-slot records, the wall-clock span, the attributed not-completed penalty, and the provenance regression module. |
| Unit provenance (production) | 197,312 | +163 | 197,475 | Issues #242 + #245: the three OUTPUT-only loss fields, worker stamping, attempt recording, and the carried-champion attempt guard. |
| Patch diff against the recorded parent (total) | 410,555 | +981 | 411,536 | Issue #253: the lineage-resolved baseline with a pickable base and context expansion, plus the truncation, per-column-room, record-vs-tree, and reconstruction-caption regressions. |
| Patch diff against the recorded parent (production) | 197,475 | +383 | 197,858 | Issue #253: the diff view's baseline resolution, the base picker, the expansion machinery, and the reconstructed_against flag on mutation detail. |
| Per-entry outcomes on the configured signal (total) | 411,536 | +1,073 | 412,609 | Issue #246: the served decided_by resolver, the shared replicate-score enumeration, the six-surface client sweep, and the signal regressions. |
| Per-entry outcomes on the configured signal (production) | 197,858 | +530 | 198,388 | Issue #246: delta_score/score_se/drift_present on the matchup grid and per-entry readers plus the channel-aware figure. |
| Pre-spend workspace gate (total) | 412,609 | +2,297 | 414,906 | Issue #240: the check package (shared workspace view, validators, report), the gate at both spend boundaries, the structural unbound-span-marker collector, the reconstructible stub adapter the parity capture now runs the gate against, and the severity, probe-environment, probe-timeout, model-role, and advisory-tier regressions. |
| Pre-spend workspace gate (production) | 198,388 | +1,240 | 199,628 | Issue #240: the validators and their lazily-built workspace view, the shared `duplicate_mutation_ids` helper, `make_adapter_from_spec`, the process-group-bounded adapter probe, and the enumerator's unbound-marker collector. |
| Replicate-keyed run identity (total) | 414,906 | +727 | 415,633 | Issue #250: replicate-suffixed run ids and events files, the single-producer args payload, the per-replicate transcript readers, and the collision/telemetry pins. |
| Replicate-keyed run identity (production) | 199,628 | +310 | 199,938 | Issue #250: the legible reserved-prefix id, per-replicate sink paths, and the any_unit_transcript reader shared by the three proposer-channel consumers. |
| Execution-plan reader (total) | 415,633 | +1,793 | 417,426 | Issue #241 (partial): the epoch execution-plan builder, its endpoint, the indexed replicate walk, and the plan regressions incl. the on-disk audit. |
| Execution-plan reader (production) | 199,938 | +1,091 | 201,029 | Issue #241 (partial): query/execution_plan.py, the endpoint registration, and the shared indexed enumerator. |
| Replicate-slot overlap (total) | 417,426 | +860 | 418,286 | Issue #251: entry-chained slot overlap behind the no-budget predicate, the shared scorer, and the deterministic utilisation/Rule-B/budget-path regressions. |
| Replicate-slot overlap (production) | 201,029 | +378 | 201,407 | Issue #251: the overlap predicate, entry chains, the two path wrappers, and the fast-mode shared semaphore. |
| Fresh in-flight tally (total) | 418,286 | +74 | 418,360 | Issue #268: the shared fresh_run_count helper, the aged pipeline tally, and the two-reader agreement pins. |
| Fresh in-flight tally (production) | 201,407 | +20 | 201,427 | Issue #268: fresh_run_count in runtime_view, shared by derive_liveness and the round pipeline. |
