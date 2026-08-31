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
| Total | 408,547 | 441,476 | -32,929 |
| Production | 197,588 | 203,802 | -6,214 |

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
| Dry-run reachability probe (total) | 418,360 | +506 | 418,866 | Issue #264: the probe module, its dry-run wiring, and the stub-driven per-role coverage including the keyless-spec and bounded-timeout pins. |
| Dry-run reachability probe (production) | 201,427 | +221 | 201,648 | Issue #264: the per-role bounded round trip through the worker's construction seam, the per-role report renderer, and the dry-run report-and-exit wiring. |
| Measurement bands on the execution plan (total) | 418,866 | +661 | 419,527 | Issue #241: the band steps with their attribution anchors, the shared complement walk, and the set-based coverage audit. |
| Measurement bands on the execution plan (production) | 201,648 | +453 | 202,101 | Issue #241: measurement_band/measurement_draw nodes, the screen-round anchor, and the partitioned enumerations. |
| Process-identity reaping of in-flight records (total) | 419,527 | +210 | 419,737 | Issue #270: the host-locality predicate, the identity gate in fresh_run_count, and the dead-child / off-host / no-identity-fields regressions. |
| Process-identity reaping of in-flight records (production) | 202,101 | +77 | 202,178 | Issue #270: the lock-derived host-locality proof and the per-record identity check the tally composes with the staleness window. |
| Replicate-aware judge repair (total) | 419,737 | +234 | 419,971 | Issue #265: the shared slot walk with its records/evidence split, per-slot transcript pairing, and the idempotence and outcome-count regressions. |
| Replicate-aware judge repair (production) | 202,178 | +93 | 202,271 | Issue #265: persisted_loss_slots beside the evidence walk and the per-slot repair loop with honest outcome counts. |
| Calibration phase and progress (total) | 419,971 | +439 | 420,410 | Issue #175: the calibrating phase with draw progress, the epoch-open pipeline step, the cost line, and the phase/render regressions. |
| Calibration phase and progress (production) | 202,271 | +150 | 202,421 | Issue #175: the beater threading, the on_draw seam, the epoch-open projection, and its two renderers. |
| De-centered scalar channels (total) | 420,410 | +624 | 421,034 | Issue #262: the derived judge/failure/runtime channels, the identity and refusal pins, and the seam test's rewritten second implementation. |
| De-centered scalar channels (production) | 202,421 | +121 | 202,542 | Issue #262: the two failure-channel contract fields with their load-time invariant, the channel derivation on LossProfile, the within-channel resolver, and the replicate fold of the run-outcome fields — net of the deleted drift_weight/runtime_weight fields, the not-completed penalty helper, the reducer's dead kind-multiplier, and the runner's inline abort arithmetic. |
| Per-epoch champion pointer in the tree (total) | 421,034 | +141 | 421,175 | Issue #280: the multi-epoch `buildTreeModel` crown regressions (each epoch crowns its own champion; a pointerless epoch stamps neither flag), the per-epoch scoped read, and the memoized closed-epoch pointer. |
| Per-epoch champion pointer in the tree (production) | 202,542 | +47 | 202,589 | Issue #280: the `?epoch=`-scoped champion read per epoch node, replacing the single bare-read pointer gated on the contract epoch, plus the data-layer `closedEpochChampion` memo that keeps a closed epoch off the live-bust re-fan. |
| Field-round champion from the role tag (total) | 421,175 | +530 | 421,705 | Issue #284: the tagged-champion resolver with its fallback chain, the nullable eval-mode provenance, and the five round-timeline regressions. |
| Field-round champion from the role tag (production) | 202,589 | +75 | 202,664 | Issue #284: _field_champion reading the recorded role, the per-round metadata borrow, and the relocated NULL-means-full default. |
| The seed is not a round (total) | 421,705 | +133 | 421,838 | Issue #286: the parentage-tested seed skip on both sides, the re-armed malformed-stamp pin, and the phantom-round regressions. |
| The seed is not a round (production) | 202,664 | +21 | 202,685 | Issue #286: the writer's parentless skip and the reader's parentage guard. |
| Critic choice and rationale on the round log (total) | 421,838 | +419 | 422,257 | Issue #279: the two-line critic ask with its mutation-proofed prompt pin, the slate summary on both transports, the missing-rationale bind, and the restored payload pin. |
| Critic choice and rationale on the round log (production) | 202,685 | +133 | 202,818 | Issue #279: the rationale-threading parse and emit path shared by both transports, and the simplified pi wrapper. |
| One-classifier seed decision (total) | 422,257 | +24 | 422,281 | Issue #291: the cross-feed probe pinning that the epoch and lineage payloads serve the identical (promoted, decision, decision_label) triple for every generation, seed included. |
| One-classifier seed decision (production) | 202,818 | +3 | 202,821 | Issue #291: the copy-off-the-lineage-node invariant comment at the recompute site that produced the disagreement, net of the deleted local derivation and of the shortened best-of-N mount docstrings. |
| Pre-flight phase and stable delegation pin (total) | 422,281 | +452 | 422,733 | Issues #276 + #260: the pre-flight phase token with combined probe progress, the epoch-open step table, and the deterministic reaping pin with its load-tolerant window. |
| Pre-flight phase and stable delegation pin (production) | 202,821 | +139 | 202,960 | Issues #276 + #260: the beater threading with restore-on-refusal, probe_selection_bounds, and the generalized epoch-open projection. |
| Live execution plan and the served per-run in-flight verdict (total) | 422,733 | +1,218 | 423,951 | Issue #241: the live plan builder with its liveness gate, active-path projection and run-scope placement, the per-row `fresh` verdict shared with the tally, the client's consumption of it with its absent-field fallback, and the 23 plan / 4 verdict / 4 node regressions plus the re-captured parity snapshot. |
| Live execution plan and the served per-run in-flight verdict (production) | 202,960 | +626 | 203,586 | Issue #241: query/live_execution_plan.py, its endpoint, route and payload contract, and the per-record predicate `fresh_run_count` and `read_active_runs_view` now share. |
| Recorded promoted head and the collapsed slate's selection (total) | 423,951 | +645 | 424,596 | Issues #287 + #292: the one promoted-head reader with its source order, the sole-candidate selection event, and the multi-promote, byte-identity, and pointer regressions. |
| Recorded promoted head and the collapsed slate's selection (production) | 203,586 | +224 | 203,810 | Issues #287 + #292: promoted_head.py, the gate/spine/champion consumers, and the shared-builder emission on the collapsed slate. |
| Gauntlet and fast-mode golden lanes (total) | 423,921 | +5,651 | 429,572 | Issue #310: three additional mock-evolve captures (gauntlet full, gauntlet fast, racing fast) and the round-log plus field-tournament sections now captured in all four, which together are 4,474 of the delta; plus the source-backend configuration boundary — its store guard, the repair command, the degrade paths in the two dashboard readers, and their regressions. The captures are the coverage: before them the unified round's single-challenger branches and the whole of fast mode executed in no golden. |
| Gauntlet and fast-mode golden lanes (production) | 202,884 | +502 | 203,386 | Issue #310: the knob-versus-disk guard and its evidence readers in genstore.py, `zicato repair generation-source-backend`, the `--reset-lineage` gate on `init --force`, the store-optional degrade in dashboard/filetree.py and dashboard/mutations.py, the one shipped `ReplicateDuel` implementation lifted into selection/driver.py, and the restored fast-mode asymmetry warning. |
| Exact scalar aggregation (total) | 429,572 | +205 | 429,777 | Issue #310: the six-case exactness suite with its two witnesses, the `math.fsum` conversions and their stated invariant, the interpreter-independent reference implementations in the scoring-seam suite, and the parity job's Python matrix. |
| Exact scalar aggregation (production) | 203,386 | +36 | 203,422 | Issue #310: `math.fsum` in place of the builtin sum and of running float accumulators across the seven modules on the served-scalar path, plus the invariant stated in the aggregation module's docstring. |
| Multi-round and non-racing golden lanes (total) | 429,777 | +7,773 | 437,550 | Issue #316: four additional mock-evolve captures — a two-round racing run and one each for the swiss, single-elimination, and double-elimination structures — which are 7,506 of the delta; plus their three contract variants, the lane table's round-count axis with its persisted carry-over assertions, and the four gates. The captures are the coverage: before them no golden ran a second round against a crowned parent, and the three non-racing registries executed in no golden at all. |
| Scoped round-log events (total) | 437,550 | +3,926 | 441,476 | Issue #307 step 1: 3,161 of the delta is the eight mock-evolve goldens, which gain a `scope` object per round-log record and lose no line; the remaining 765 are the `RoundEventScope` envelope with its coordinate vocabulary and grouping key, the emitter's third scope argument across the propose and duel call sites, the two registry-correspondence tests holding the step table equal to the plan's, and the round-trip, forward-compatibility, slate-scope and duel-wiring regressions. |
| Scoped round-log events (production) | 203,422 | +380 | 203,802 | Issue #307 step 1: scope serialization, decoding and attribute promotion in `round_log.py`, the scope argument on `_RoundLogEmitter.emit` / `_emit_tournament_units` / `_emit_gate_evaluated`, the shared `_duel_scope` builder, the declared stepless-token set, and the field, persist and best-of-N call sites. |
