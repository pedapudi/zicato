# Engineering review of zicato

> **Status:** dated review record of revision
> `3bddf6424e5b13287fa9adaee34e73a97cb62457`, written 2026-09-05. Its
> findings were filed as issues [#476](https://github.com/pedapudi/zicato/issues/476) through
> [#495](https://github.com/pedapudi/zicato/issues/495), and all twenty are closed as
> completed. Source links point at the reviewed revision, so each cited line
> number matches the code the review describes; the tree has changed since.

Review date: 2026-09-05. Source revision: `3bddf6424e5b13287fa9adaee34e73a97cb62457`.
Audience: maintainers familiar with Python, asynchronous execution, and automated evaluation.
Status: findings and recommendations; no implementation changes accompany this review.

The highest-value work is to make execution identity, contract ownership, and
promotion evidence consistent across the loop. Several failures reproduced in
this review come from two parts of the system representing the same operation
differently. Consolidating those representations can improve correctness and
remove production code together.

Eight parallel reviews covered orchestration, evaluation statistics, proposal
generation, persistence, process supervision, operator interfaces, query and
presentation code, and engineering infrastructure. Reviewers inspected source
and tests, checked the development guides against the implementation, and ran
isolated probes. No live model evaluations were performed.

An **epoch** records a fixed evaluation contract: tasks, scoring, proposal
instructions, and execution identities. A **generation** is a candidate source
snapshot. A **round** proposes candidates, evaluates them, and records whether
one replaces the champion. Those distinctions remain useful internally; most
operators need fewer of them to configure and assess an improvement attempt.

**The refactoring campaign has established useful foundations**

The implementation has one shared round pipeline, durable settlement receipts,
a workspace layout owner, shared record readers, a scoring-field registry, and
an independent process supervisor. The CLI already has eleven top-level
commands. Several console simplifications and experimental tournament gating
are implemented. Repeating older proposals to split the orchestrator or move
readers out of the dashboard would miss the remaining problems.

The source-size measurement passes its configured limits:

| Measurement | Recorded baseline | Reviewed revision | Difference |
| --- | ---: | ---: | ---: |
| Production lines | 197,702 | 204,337 | +6,635 |
| Production logic lines | 110,276 | 113,515 | +3,239 |
| Counted repository lines | 408,661 | 471,880 | +63,219 |

These are the repository's measurements, with its exclusions and language
counters. They do not establish that every added line is unnecessary. They do
show that restructuring has not produced a net reduction against the recorded
baseline. The dashboard and query packages together contain 38,252 production
logic lines, approximately 34% of the total. See
[the measurement implementation](https://github.com/pedapudi/zicato/blob/3bddf6424e5b13287fa9adaee34e73a97cb62457/tools/line_budget.py) and
[the configured baseline](https://github.com/pedapudi/zicato/blob/3bddf6424e5b13287fa9adaee34e73a97cb62457/.line-budget.json).

The production counter exceeds the logic counter by 90,822 lines. That
difference includes comments, docstrings, blank lines, and language-specific
counting effects. Moving repeated design explanations into one maintained
document can improve readability, but a smaller comment count does not prove
that the implementation became simpler.

**Give every process one complete lifecycle**

- **Some supervisor actions bypass process identity checks.** Deadline handling
  verifies both process ID and recorded start time. Explicit kill requests use
  only liveness; stalled-run handling also omits the start-time check. A stale
  record carrying an intentionally wrong start token still caused the
  supervisor to terminate the reviewer's owned fixture process. Orphan reaping
  uses the same incomplete kill-request helper. Centralize the identity check
  immediately before every signal. Source:
  [watchdog.rs:770](https://github.com/pedapudi/zicato/blob/3bddf6424e5b13287fa9adaee34e73a97cb62457/crates/supervisor/src/watchdog.rs#L770),
  [watchdog.rs:793](https://github.com/pedapudi/zicato/blob/3bddf6424e5b13287fa9adaee34e73a97cb62457/crates/supervisor/src/watchdog.rs#L793), and
  [watchdog.rs:1203](https://github.com/pedapudi/zicato/blob/3bddf6424e5b13287fa9adaee34e73a97cb62457/crates/supervisor/src/watchdog.rs#L1203).

- **Cancellation removes supervision before the worker exits.** The tournament
  runner handles timeout around the child wait but does not handle task
  cancellation. Unconditional cleanup releases the concurrency permit, removes
  the checkout, and deletes the active-run record. An isolated cancellation
  probe left the worker alive with both its record and checkout absent. Make
  cancellation terminate and await the owned worker before releasing those
  resources. Source:
  [runner.py:634](https://github.com/pedapudi/zicato/blob/3bddf6424e5b13287fa9adaee34e73a97cb62457/src/zicato/tournament/runner.py#L634) and
  [runner.py:787](https://github.com/pedapudi/zicato/blob/3bddf6424e5b13287fa9adaee34e73a97cb62457/src/zicato/tournament/runner.py#L787).

- **Leader death is mistaken for process-group completion.** Escalation stops
  when the group leader exits. A descendant that ignored the initial
  termination signal survived beyond twice the configured grace period in an
  owned-process probe. Track the owned process group through escalation and
  verify descendant termination. The existing group test checks the leader's
  death. Source: [signal.rs:239](https://github.com/pedapudi/zicato/blob/3bddf6424e5b13287fa9adaee34e73a97cb62457/crates/supervisor/src/signal.rs#L239).

- **Concurrent proposal slots overwrite one supervision record.** Proposal
  episodes identify active runs using only epoch and candidate. Concurrent
  sampling slots share those identifiers; the first finisher removes the
  record used by the remaining episodes. Include slot and attempt identity in
  the lifecycle record, and remove a record only if its owner still matches.
  Source: [foe_agent.py:302](https://github.com/pedapudi/zicato/blob/3bddf6424e5b13287fa9adaee34e73a97cb62457/src/zicato/proposer/foe_agent.py#L302).

- **Escalations delay unrelated worker deadlines.** The supervisor awaits each
  escalation inside its run scan. Three simultaneously overdue workers with a
  one-second grace exited after 1.02, 2.03, and 3.04 seconds in an isolated
  probe. Advance independent termination state for every owned process each
  tick, with duplicate escalation prevention. Source:
  [watchdog.rs:1109](https://github.com/pedapudi/zicato/blob/3bddf6424e5b13287fa9adaee34e73a97cb62457/crates/supervisor/src/watchdog.rs#L1109).

Use one owned-process abstraction for launch, registration, cancellation,
termination, waiting, and cleanup. Keep process enforcement independent of the
Python event loop. Add tests for cancellation, mismatched start tokens,
descendants that ignore termination, and concurrent proposal slots.

**Resolve one contract for execution and a separate contract for editing**

- **Explicit epoch execution mixes contracts.** The round entry point resolves
  the requested epoch but loads tasks, scoring, and the brief through readers
  tied to the current-epoch marker. A two-epoch probe validated one board and a
  promotion margin of 0.01, then loaded the other board, its brief, and margin
  0.7 for execution. Resolve every execution input from one epoch-specific
  contract value. Source:
  [round_entry.py:181](https://github.com/pedapudi/zicato/blob/3bddf6424e5b13287fa9adaee34e73a97cb62457/src/zicato/evolve/round_entry.py#L181) and
  [round_entry.py:210](https://github.com/pedapudi/zicato/blob/3bddf6424e5b13287fa9adaee34e73a97cb62457/src/zicato/evolve/round_entry.py#L210).

- **The builder can erase pending live edits.** Its draft loader reads frozen
  epoch files, while Apply writes the live contract files. A temporary fixture
  added a live task, changed the brief, and set margin 0.73 after freezing the
  epoch. Opening the builder and applying an unrelated weight change removed
  the task and restored the frozen brief and margin 0.01. Its preview reported
  no differences before that change. Load editing drafts from live inputs and
  reject Apply when their base revision changed. Source:
  [draft.py:160](https://github.com/pedapudi/zicato/blob/3bddf6424e5b13287fa9adaee34e73a97cb62457/src/zicato/contract_draft/draft.py#L160) and
  [operations.py:2189](https://github.com/pedapudi/zicato/blob/3bddf6424e5b13287fa9adaee34e73a97cb62457/src/zicato/contract_draft/operations.py#L2189).

- **Malformed settings silently become different settings.** The scoring
  decoder ignores unknown keys and coerces scalar types. Reproduced examples:
  `promote_mragin: 0.7` leaves the default margin at 0.01; `enabled: "false"`
  enables overfitting protection; `best_of_n: 2.8` becomes 2. A malformed nested
  object can become its defaults. Validate authored inputs strictly through the
  existing field registry. Keep any necessary historical decoding separate.
  Source: [contract_serde.py:102](https://github.com/pedapudi/zicato/blob/3bddf6424e5b13287fa9adaee34e73a97cb62457/src/zicato/epoch/contract_serde.py#L102).

- **Dry-run can modify the live scoring contract.** Tournament overrides are
  persisted before the dry-run branch. An offline example's dry-run changed
  its tournament structure even though validation subsequently failed. Build
  an in-memory override for validation and persist only on an executing path.
  Source: [evolve.py:885](https://github.com/pedapudi/zicato/blob/3bddf6424e5b13287fa9adaee34e73a97cb62457/src/zicato/cli/commands/evolve.py#L885).

- **An epoch retains live proposal-skill paths.** Epoch creation stores the
  source path rather than a frozen copy of its resolved instructions. Each
  round reloads those files, while an active multi-round invocation skips
  repeated contract validation. Editing a skill between rounds can therefore
  change proposal instructions under one recorded contract. Fresh explicit-epoch
  validation does detect drift; that protection does not close the active
  invocation gap. Freeze resolved instructions and bind them to the invocation.
  Source: [lifecycle.py:604](https://github.com/pedapudi/zicato/blob/3bddf6424e5b13287fa9adaee34e73a97cb62457/src/zicato/epoch/lifecycle.py#L604),
  [round_entry.py:225](https://github.com/pedapudi/zicato/blob/3bddf6424e5b13287fa9adaee34e73a97cb62457/src/zicato/evolve/round_entry.py#L225), and
  [loop.py:860](https://github.com/pedapudi/zicato/blob/3bddf6424e5b13287fa9adaee34e73a97cb62457/src/zicato/evolve/loop.py#L860).

One typed frozen contract should supply validation, proposal preparation, and
evaluation. An editable contract should carry its source revision and proposed
changes. A shared frozen contract could remove manual copying between the
round-input records. `PreparedRound` and `FieldRound` share nineteen field names,
with extensive use of `Any`; consolidate fields whose meanings and lifetimes
match. Source:
[generation_phase.py:13](https://github.com/pedapudi/zicato/blob/3bddf6424e5b13287fa9adaee34e73a97cb62457/src/zicato/evolve/generation_phase.py#L13) and
[field.py:52](https://github.com/pedapudi/zicato/blob/3bddf6424e5b13287fa9adaee34e73a97cb62457/src/zicato/evolve/field.py#L52).

The generated editable scoring file contains 96 lines, 34 top-level keys, and
66 leaf values; the registry declares 58 knobs. Emit the necessary choices and
explicit deviations in editable configuration, while keeping complete resolved
values in frozen evaluation records. Prove that sparse and expanded inputs
produce identical scoring and contract hashes. Present tasks, allowed edits,
success criteria, and spending limits before advanced controls.

The supported example also requires an extra `PYTHONPATH` export for its
project-local driver. Record the driver import root during initialization and
pass it consistently to validation and execution. Test the example from another
working directory without that environment variable, while proving mutable
target imports still come from the selected generation. Source:
[example_scaffold.py:100](https://github.com/pedapudi/zicato/blob/3bddf6424e5b13287fa9adaee34e73a97cb62457/src/zicato/cli/example_scaffold.py#L100) and
[init.py:127](https://github.com/pedapudi/zicato/blob/3bddf6424e5b13287fa9adaee34e73a97cb62457/src/zicato/cli/commands/init.py#L127).

**Make confirmation produce a complete evidence decision**

- **Requested evidence confirmation can permit promotion without enough
  evidence.** With one apparent win followed by five distinct tied draws, the
  confirmation driver exhausted its budget, returned the original promotion,
  and returned no evidence record. The additional draws disappeared from its
  returned audit. The behavior is explicitly implemented; changing it requires
  changing the confirmation contract. Insufficient evidence should retain the
  champion and record the observations and reason. Source:
  [driver.py:354](https://github.com/pedapudi/zicato/blob/3bddf6424e5b13287fa9adaee34e73a97cb62457/src/zicato/selection/driver.py#L354).

- **Rating uncertainty does not shrink correctly for the comparison.** The
  Bradley–Terry model estimates strengths from pairwise wins. The implementation
  retains their shared location uncertainty in marginal standard errors, then
  compares strengths without covariance. At an unchanged 80% win rate,
  increasing observations from 100 to 10,000 moved the reported probability of
  being stronger only from 0.907 to 0.917. Estimate uncertainty in the strength
  difference, including covariance or an identifiable constrained fit. Source:
  [rating.py:185](https://github.com/pedapudi/zicato/blob/3bddf6424e5b13287fa9adaee34e73a97cb62457/src/zicato/selection/rating.py#L185) and
  [rating.py:485](https://github.com/pedapudi/zicato/blob/3bddf6424e5b13287fa9adaee34e73a97cb62457/src/zicato/selection/rating.py#L485).

- **The recommended scaffold can exhaust confirmation even after unanimous
  wins.** A real racing strategy with synthetic results and only one applied
  candidate remained deferred after the initial win plus 32 fresh unanimous
  confirmation wins. The same pair's clearance changed when other contestants
  were present. Partial candidate fields are accepted in production. Treat this
  as a consequence of the uncertainty calculation and validate the corrected
  defaults jointly. Source:
  [evidence_gate.py:256](https://github.com/pedapudi/zicato/blob/3bddf6424e5b13287fa9adaee34e73a97cb62457/src/zicato/selection/evidence_gate.py#L256),
  [scoring_config.py:1546](https://github.com/pedapudi/zicato/blob/3bddf6424e5b13287fa9adaee34e73a97cb62457/src/zicato/core/scoring_config.py#L1546), and
  [field_candidates.py:369](https://github.com/pedapudi/zicato/blob/3bddf6424e5b13287fa9adaee34e73a97cb62457/src/zicato/evolve/field_candidates.py#L369).

- **A task that never ran can become a cached failure.** Scheduling-budget
  exhaustion synthesizes and persists a cacheable `budget_exhausted` result.
  A subsequent cache-first evaluation with a fresh, uncapped runtime launched
  zero workers and reused that zero-runtime failure. Separate invocation-level
  scheduling status from observed task outcomes. Source:
  [scheduling.py:482](https://github.com/pedapudi/zicato/blob/3bddf6424e5b13287fa9adaee34e73a97cb62457/src/zicato/tournament/scheduling.py#L482),
  [scheduling.py:1114](https://github.com/pedapudi/zicato/blob/3bddf6424e5b13287fa9adaee34e73a97cb62457/src/zicato/tournament/scheduling.py#L1114), and
  [unit_cache.py:89](https://github.com/pedapudi/zicato/blob/3bddf6424e5b13287fa9adaee34e73a97cb62457/src/zicato/tournament/unit_cache.py#L89).

- **Holdout exhaustion permits training-only promotion.** A holdout is a task
  subset hidden from candidate generation and reserved for confirmation. The
  implemented policy returns the training decision after exhausting its
  holdout-query allowance. Withholding a holdout result can also leave a
  training promotion intact after a holdout rejection. This is intentional
  policy, supported by code and existing tests, rather than a reproduced
  implementation accident. Specify whether promotion requires holdout evidence;
  if it does, exhaustion must withhold promotion until the evaluation contract
  is refreshed. Source:
  [runner.py:1683](https://github.com/pedapudi/zicato/blob/3bddf6424e5b13287fa9adaee34e73a97cb62457/src/zicato/tournament/runner.py#L1683) and
  [governance.py:501](https://github.com/pedapudi/zicato/blob/3bddf6424e5b13287fa9adaee34e73a97cb62457/src/zicato/tournament/governance.py#L501).

- **Measurement purpose is encoded in unenforced numeric ranges.** Ordinary
  replicate indices can reach calibration's reserved index 1000; sufficiently
  large confirmation budgets reach reflection's range. The ordinary replicate
  count has a minimum but no maximum; confirmation budgets also lack a maximum.
  Give cache keys an
  explicit measurement purpose and draw number. Until migration, validate
  bounds. This finding follows from key arithmetic; no large evaluation was
  launched. Source:
  [scheduling.py:1613](https://github.com/pedapudi/zicato/blob/3bddf6424e5b13287fa9adaee34e73a97cb62457/src/zicato/tournament/scheduling.py#L1613) and
  [unit_cache.py:160](https://github.com/pedapudi/zicato/blob/3bddf6424e5b13287fa9adaee34e73a97cb62457/src/zicato/tournament/unit_cache.py#L160).

- **Experimental ranking can discard a tied leader.** With two unresolved
  leaders that both beat a third contestant, the dominance-set helper returns
  only the first leader. The resolver then removes the other before ranking.
  Retain unresolved competitors and test missing comparisons and input-order
  permutations. This affects opt-in experimental structures. Source:
  [resolve.py:176](https://github.com/pedapudi/zicato/blob/3bddf6424e5b13287fa9adaee34e73a97cb62457/src/zicato/selection/resolve.py#L176).

Keep ranking, evidence collection, and promotion authorization explicit. Every
confirmation attempt should return observations, spend, status, and a reason,
including ties and insufficient evidence. Statistical corrections require
measuring false-promotion rates and detection power under seeded noise. Existing
numeric expectations must change only with a documented statistical reason.

**Validate the candidate and its feedback through one policy**

- **Restricted feedback exposes task identities.** Production pattern
  detectors put task and board-entry identifiers into summary text. Restricted
  rendering prints that summary verbatim and removes only selected detail
  keys. A real detector-to-render probe preserved both private identifiers.
  Existing prompt tests use an identity-free invented summary. Build an
  aggregate feedback representation using allowed fields, and render summaries
  from that representation. Test every actual detector output. Source:
  [prompts.py:65](https://github.com/pedapudi/zicato/blob/3bddf6424e5b13287fa9adaee34e73a97cb62457/src/zicato/proposer/prompts.py#L65),
  [prompts.py:125](https://github.com/pedapudi/zicato/blob/3bddf6424e5b13287fa9adaee34e73a97cb62457/src/zicato/proposer/prompts.py#L125), and
  [detectors.py:384](https://github.com/pedapudi/zicato/blob/3bddf6424e5b13287fa9adaee34e73a97cb62457/src/zicato/patterns/detectors.py#L384).

- **Accepted scratch edits can differ from the evaluated child.** Changed-line
  ownership accepts an assignment-name change beside an editable string, but
  the resulting patch retains only the string literal. A probe edited
  `PROMPT = "..."` into `RENAMED = "answer concisely"`; applying the accepted
  patch produced `PROMPT = "answer concisely"`. Validate by reconstructing the
  child from the patches and comparing it with the accepted scratch tree,
  subject to explicit normalization rules. Source:
  [foe_scratch.py:112](https://github.com/pedapudi/zicato/blob/3bddf6424e5b13287fa9adaee34e73a97cb62457/src/zicato/proposer/foe_scratch.py#L112).

- **Forbidden edits are rejected too late for episode repair.** The proposal
  host can verify and return a patch for an identifier in `forbidden_ids`.
  The outer round rejects it after the episode finishes. Pass one mutation
  policy through episode verification and final patch conversion, retaining
  the execution-side check. Source:
  [foe_agent.py](https://github.com/pedapudi/zicato/blob/3bddf6424e5b13287fa9adaee34e73a97cb62457/src/zicato/proposer/foe_agent.py) and
  [round.py:350](https://github.com/pedapudi/zicato/blob/3bddf6424e5b13287fa9adaee34e73a97cb62457/src/zicato/evolve/round.py#L350).

The accepted candidate should carry its reconstructed tree identity, validated
patches, and policy result together. The source snapshot evaluated by workers
and committed on promotion must be the same accepted candidate. This reduces
repair failures and addresses the repeated candidate-state bugs in the casebook.

**Publish complete durable objects and detect changed records**

- **Atomic writes share one temporary filename and assume a complete write.**
  Two coordinated writers exposed an empty published file; one writer then
  raised `FileNotFoundError`. Injecting a short write published truncated JSON.
  These probes establish a failure of the helper's atomicity guarantee;
  production reachability of the conflicting writes was not established.
  Use a unique temporary file per writer and complete the payload before
  synchronization and rename. Preserve directory synchronization and explicit
  single-writer rules for compound operations. Source:
  [_atomic.py:39](https://github.com/pedapudi/zicato/blob/3bddf6424e5b13287fa9adaee34e73a97cb62457/src/zicato/storage/_atomic.py#L39) and
  [_atomic.py:77](https://github.com/pedapudi/zicato/blob/3bddf6424e5b13287fa9adaee34e73a97cb62457/src/zicato/storage/_atomic.py#L77).

- **Failed epoch creation can close the usable epoch.** Creating an epoch with
  invalid name `!!!` closed the existing epoch before rejecting the name. The
  marker still identified that closed epoch. Other materialization failures
  occur after the same destructive ordering. Validate and stage the complete
  replacement before publishing its identity and closing its predecessor.
  Source: [lifecycle.py:499](https://github.com/pedapudi/zicato/blob/3bddf6424e5b13287fa9adaee34e73a97cb62457/src/zicato/epoch/lifecycle.py#L499).

- **Failed directory snapshot creation publishes a partial generation.** If
  one source copies successfully and a later source is missing, the operation
  raises but generation discovery reports the partial snapshot as present.
  Baseline preparation then skips completing the seed and its bookkeeping on
  retry. Stage outside the discoverable generation directories and publish only
  after all required sources are validated and copied. Recovery must also
  complete any interrupted baseline bookkeeping. Source:
  [genstore.py:665](https://github.com/pedapudi/zicato/blob/3bddf6424e5b13287fa9adaee34e73a97cb62457/src/zicato/epoch/genstore.py#L665) and
  [round_baseline.py:153](https://github.com/pedapudi/zicato/blob/3bddf6424e5b13287fa9adaee34e73a97cb62457/src/zicato/evolve/round_baseline.py#L153).

- **Index repair misses changed records when counts stay constant.** After
  canonical epoch closure, an isolated fixture's index still recorded
  `closed=0`; validation and healing both reported no work. The cursor tracks
  counts and misses some updates to existing files. Include content revision
  signals for mutable canonical records, and test failed derived writes
  followed by repair. Source:
  [ingest.py:1359](https://github.com/pedapudi/zicato/blob/3bddf6424e5b13287fa9adaee34e73a97cb62457/src/zicato/index/ingest.py#L1359).

Use the existing durable settlement-receipt approach as a model for compound
publication. Extract receipt decoding into a dependency-neutral record owner;
storage and index readers should not import round replay orchestration merely
to understand a receipt.

**Serve complete, correctly scoped views with an explicit content revision**

- **Terminal Home joins generations without epoch identity.** Two epochs each
  containing `v1` caused the later epoch's champion to display the earlier
  epoch's rejected verdict and rating 1200 instead of promoted and 1800. Serve
  the full champion record with its epoch overview, and scope remaining
  lookups by both identifiers. Source:
  [home.py:231](https://github.com/pedapudi/zicato/blob/3bddf6424e5b13287fa9adaee34e73a97cb62457/src/zicato/tui/lenses/home.py#L231).

- **Content changes can be discarded as unchanged progress.** A real epoch
  change carrying the same execution sequence triggered zero environment
  requests in a browser probe. Execution progress and workspace content
  revision have different meanings. Use content changes to invalidate reads
  and retain digest-based suppression of identical DOM updates. Source:
  [sse.py:239](https://github.com/pedapudi/zicato/blob/3bddf6424e5b13287fa9adaee34e73a97cb62457/src/zicato/dashboard/sse.py#L239) and
  [sse.js:108](https://github.com/pedapudi/zicato/blob/3bddf6424e5b13287fa9adaee34e73a97cb62457/src/zicato/dashboard/static/js/core/sse.js#L108).

- **Blocking reads run inside asynchronous handlers.** Forty of 59 table-based
  reads execute inline. An injected 150 ms environment read delayed a 10 ms
  timer to 151 ms. Terminal refresh also performs synchronous HTTP calls.
  Offload synchronous reads by default and apply completed terminal views on
  the UI thread. This probe demonstrates blocking. It does not estimate
  production latency.
  Source: [endpoints.py:986](https://github.com/pedapudi/zicato/blob/3bddf6424e5b13287fa9adaee34e73a97cb62457/src/zicato/dashboard/endpoints.py#L986) and
  [app.py:139](https://github.com/pedapudi/zicato/blob/3bddf6424e5b13287fa9adaee34e73a97cb62457/src/zicato/tui/app.py#L139).

- **Composite responses repeatedly read the same files.** On a small standard
  fixture, one candidate dossier read its experiment six times, scoring four
  times, and epoch configuration three times. Request-scoped inputs would avoid
  repeated parsing and inconsistent versions of the same record within one
  response. This alone would not make independent files transactional. Source:
  [candidate_view.py:190](https://github.com/pedapudi/zicato/blob/3bddf6424e5b13287fa9adaee34e73a97cb62457/src/zicato/query/candidate_view.py#L190).

- **Endpoint reuse does not verify workspace identity.** Terminal attachment
  accepted a healthy service reporting another workspace. Compare the returned
  workspace with the requested one before accepting a persisted endpoint.
  Separately, cache transient read failures with bounded retry behavior:
  a failed reflection request remained cached as `null` after service recovery.
  Source: [service.py:142](https://github.com/pedapudi/zicato/blob/3bddf6424e5b13287fa9adaee34e73a97cb62457/src/zicato/tui/service.py#L142) and
  [data.js:42](https://github.com/pedapudi/zicato/blob/3bddf6424e5b13287fa9adaee34e73a97cb62457/src/zicato/dashboard/static/js/data.js#L42).

The product opportunity is progressive disclosure. The primary view can answer
what changed, whether improvement is supported, what it cost, and what action is
available. Detailed tournament structures, individual ratings, calibration,
replication, and reflection records remain available where they explain those
answers. This is a design recommendation; this review did not measure user
completion rates or propose deleting audit evidence.

**Make verification enforce the properties the campaign claims**

- **Parity can report success without verification.** An unknown `--only`
  selector ran no gates and returned success. A checker process exiting 127
  also passed the type-check gate because the script counted diagnostic text
  and ignored exit status. Validate selectors, require a nonempty gate set,
  and preserve subprocess failure status. Source:
  [parity.sh:100](https://github.com/pedapudi/zicato/blob/3bddf6424e5b13287fa9adaee34e73a97cb62457/tools/parity.sh#L100),
  [parity.sh:183](https://github.com/pedapudi/zicato/blob/3bddf6424e5b13287fa9adaee34e73a97cb62457/tools/parity.sh#L183), and
  [parity.sh:202](https://github.com/pedapudi/zicato/blob/3bddf6424e5b13287fa9adaee34e73a97cb62457/tools/parity.sh#L202).

- **The recommended feature combination needs an integrated test.** The
  ordinary scaffold combines three proposal samples, screening, a four-candidate
  racing field, and evidence confirmation. The reviewed convergence and parity
  fixtures reduce sampling to one; scaffold tests assert configuration values
  without executing that combination. Add a known-answer integration profile
  constructed from the recommended defaults, using deterministic responders
  and real workers. Assert accepted source identity, screen exclusions,
  promotion decisions, and recovery under cancellation. Source:
  [scoring_config.py:1546](https://github.com/pedapudi/zicato/blob/3bddf6424e5b13287fa9adaee34e73a97cb62457/src/zicato/core/scoring_config.py#L1546),
  [test_scaffold_contract.py](https://github.com/pedapudi/zicato/blob/3bddf6424e5b13287fa9adaee34e73a97cb62457/tests/test_scaffold_contract.py), and
  [test_convergence_known_answer.py](https://github.com/pedapudi/zicato/blob/3bddf6424e5b13287fa9adaee34e73a97cb62457/tests/test_convergence_known_answer.py).

- **Public mutation entry points do not share invocation ownership.** A
  separate process held the workspace lock while direct `evolve_once` still
  wrote a round-open event. Both public loop entry points should acquire one
  invocation context; the internal round executor should require it. An
  independent probe also showed that an immediate inner `TimeoutError` was
  reported as exhaustion of a 3,600-second invocation budget. Distinguish the
  enclosing deadline's expiration from an operation's own failure. Source:
  [round_entry.py:67](https://github.com/pedapudi/zicato/blob/3bddf6424e5b13287fa9adaee34e73a97cb62457/src/zicato/evolve/round_entry.py#L67),
  [loop.py:609](https://github.com/pedapudi/zicato/blob/3bddf6424e5b13287fa9adaee34e73a97cb62457/src/zicato/evolve/loop.py#L609), and
  [loop.py:890](https://github.com/pedapudi/zicato/blob/3bddf6424e5b13287fa9adaee34e73a97cb62457/src/zicato/evolve/loop.py#L890).

- **Dependency and verification declarations need completeness checks.** The
  internal library boundary exempts seven packages because their dependencies
  cross it. Separately, `zicato.example_workspace` is absent from every import
  contract's source list. A shared brief parser also creates a reverse
  dependency from reports into query code. Use one exhaustive package-role
  inventory, move record parsers below orchestration, and check that every
  production namespace is assigned. Generate verification instructions from
  one executable check definition. Several guides still describe removed
  pipelines, outdated command discovery, and an explicit `pytest tests/`
  invocation as the quick tier. Source:
  [pyproject.toml:493](https://github.com/pedapudi/zicato/blob/3bddf6424e5b13287fa9adaee34e73a97cb62457/pyproject.toml#L493),
  [report_data.py:555](https://github.com/pedapudi/zicato/blob/3bddf6424e5b13287fa9adaee34e73a97cb62457/src/zicato/analyzer/report_data.py#L555), and
  [Makefile:48](https://github.com/pedapudi/zicato/blob/3bddf6424e5b13287fa9adaee34e73a97cb62457/Makefile#L48).

**Implementation order and acceptance criteria**

1. Repair process identity checks, cancellation cleanup, atomic publication,
   builder lost edits, and restricted feedback. Each change needs the failing
   boundary probe as a regression test.
2. Correct evidence confirmation, comparison uncertainty, and cache admission.
   Keep the statistical design and measured operating characteristics together.
   Evaluate full and partially applied candidate fields under the same settings.
3. Introduce the frozen contract and invocation owners while removing duplicated
   round inputs, current-marker lookups inside execution, and repeated policy
   coercion. Preserve contract hashes for inputs whose meaning has not changed.
4. Consolidate request inputs and served views. Check cross-epoch identity,
   freshness after execution stops, responsiveness, and zero DOM changes on
   identical content.
5. Reduce production size through deleted responsibilities and representations.
   Record net production and logic changes per workstream, plus user-facing
   choices removed. Require a specific reason for accepted growth.

No treatment met the acceptance criteria in either of the two valid recorded
feature campaigns. Those results support keeping unproven proposal features
experimental and testing simpler configurations; they do not prove the features
are ineffective for every target. Further effectiveness measurements should
retain a known defect to repair, an unchanged-system control, complete round
accounting, and an independent assessment of improvement. Source:
[the campaign record](https://github.com/pedapudi/zicato/blob/3bddf6424e5b13287fa9adaee34e73a97cb62457/docs/design/CAMPAIGN.md).

**Validation and limits**

The existing convergence and decision-procedure suites passed: 17 tests in
113.73 seconds. The source-size check passed. Reviewers also ran focused
subsystem suites and temporary-directory or owned-process probes. Passing
existing suites does not contradict the findings: several triggering conditions
are absent from those suites, and some problematic behaviors are explicitly
encoded in existing tests.

This inspection did not perform merge validation. The complete Python,
browser, Rust, type, and parity ladders were not all rerun. No live effectiveness
claim follows from the deterministic probes. All process probes were confined
to captured fixture processes, which were terminated and reaped afterward.
