# Telemetry dialects

[TELEMETRY.md](TELEMETRY.md) describes ONE producer: a run emits a
drift-instrumented event stream, zicato captures it verbatim, and the
post-run reducer folds it into a typed `LossProfile`. That producer is
powerful but it assumes the inner harness runs under the
drift-instrumented ecosystem harness. Not every agent does.

This document generalises the PRODUCER without touching anything
downstream. The principle:

> **`LossProfile` is the convergence point.** A *dialect* is a named
> producer that turns a run's raw telemetry into the `LossProfile`
> inputs — drift-signal streams plus judge/outcome events. Everything
> downstream of the reducer (scoring, the promote gate, the analytical
> index, board reflection) reads `LossProfile` and NEVER knows which
> dialect produced it.

The reducer already proved this seam is narrow: the whole point of
`LossProfile` (TELEMETRY.md §3) is that "event schemas evolve upstream,
and we want a single narrow seam between *what the harness emits* and
*what zicato scores on*." A dialect is that seam made plural: several
producers, one `LossProfile`.

## 1. The dialect abstraction

A dialect is a deterministic function

```
reduce_<dialect>(events_jsonl_path, entry) -> DialectSignals
```

where `DialectSignals` is the flat bundle of raw signals the reducer
needs to build a `LossProfile`:

| Signal | Feeds |
|---|---|
| `drift_counts` | the drift term (`drift_loss`) + `metric_counts` under `drift:` |
| `plan_revisions` | the plan-revision term |
| `task_started` / `task_failed` | `task_failure_ratio` (the ×10 failure term) |
| `llm_call_count` | `cost:llm_calls` |
| `token_count` | `cost:tokens_spent` / `tokens_spent` |
| `agent_text_chars` | `output:chars` fallback |
| `run_id` / `adk_session_id` | identity + the harmonograf deep-link |
| `agent_turns` / `user_turns` | the multi-turn memory/context heuristics |
| `malformed_line_count` / `warnings` | surfaced as reduction warnings, never a crash |

`reduce_loss` selects the dialect, calls it, and then runs the SAME
dialect-agnostic tail it always did: the not-completed penalty, the
drift-loss dispatch (Seam 1), the pass/continuous-score derivation, the
generalised metric surface, and `LossProfile` assembly. The dialect
changes only *how the raw counts are produced*, and not how they are
scored.

## 2. Dialect 1 — `goldfive` (default)

The current path, unchanged and byte-identical. The most powerful
dialect: it consumes the full drift-instrument stream, so it is the only
dialect that can carry

- **in-process drift instruments** — the live reasoning-stream detectors
  (`looping_reasoning`, `plan_divergence`, `intent_divergence`,
  `confabulation_risk`, `reasoning_cluster_tightening`, …) that fire from
  inside the harness as the agent reasons;
- **custom process-judge drift** — a `JudgementEmitted` paired with a
  `DriftDetected` of kind `custom`, re-attributed to `custom:<judge_name>`
  and weighted through `per_judge_weights` (TELEMETRY.md §3.2.1);
- **collusion-guarded emulator introspection** — the multi-turn
  emulator's own lane (`zicato:emulator`), which the reducer's transcript
  reconstruction reads.

`goldfive` is the default and rides `_SCORING_OMIT_AT_DEFAULT_FIELDS`
omission (§5) so every existing contract hash is untouched.

## 3. Dialect 2 — `adk_events`

An agent-framework event-log JSONL: the kind of structured event trail a
generic ADK-style agent framework writes — one JSON object per line,
each a tool-call / tool-response / agent-transfer / error / model-usage
event. No drift instruments, no reasoning-stream telemetry — but a rich
enough behavioural trace that several loss-relevant signals fall out of
it directly.

### 3.1 Accepted event shape (tolerant)

Each line is a JSON object. The event kind is read from `type`
(fallbacks: `event_type`, `kind`). Recognised kinds and the fields the
reducer reads (each field name accepts a small set of aliases so the
dialect tolerates dialect-of-a-dialect naming):

| `type` | Fields read | Aliases tolerated |
|---|---|---|
| `tool_call` | `tool`, `args` | `name` / `tool_name`; `arguments` / `input` |
| `tool_response` | `status`, `error` | `is_error`; a truthy `error` |
| `agent_transfer` | (counted) | `transfer` |
| `error` | (counted) | `exception` |
| `model_usage` | `input_tokens`, `output_tokens` | `total_tokens` / `tokens`; nested `usage` |
| `agent_message` | `text` | `content` / `message`; role `assistant` |
| `user_message` | `text` | `content` / `message`; role `user` |
| `run_start` / any event | `run_id`, `session_id` | `runId` / `sessionId` / `invocation_id` |

Tolerance rules (honest, never-crash):

- an **unknown `type`** is skipped — even when the event carries a
  `role` field (a forward-compat reasoning/log event must not inflate the
  output envelope or mint phantom transcript turns); only the explicit
  message types (`agent_message` / `user_message`), a generic `message`,
  or a **typeless** bare-transcript line (`{"role": …, "content": …}`)
  route to the transcript;
- a **malformed line** (not JSON, or not a JSON object) is counted into
  `malformed_line_count` and surfaced as a reduction warning rather than
  raised as an exception, the way the goldfive fallback path skips
  unparseable lines (TELEMETRY.md §2.3);
- a recognised event **missing a field** contributes nothing for that
  field (a `model_usage` with no token keys adds `1` to `llm_call_count`
  and `0` tokens).

### 3.2 The signal table

How each event-log signal derives, and the drift-vocabulary signal it
maps to. Every drift-style row is a `DriftCount(kind, severity, count)`
that folds through the SAME `severity_weights × per_kind_weights × count`
machinery a goldfive drift instrument folds through, so an operator tunes
an `adk_events` contract with the same knobs.

| Event-log signal | Derivation | Maps to | Severity | Rationale |
|---|---|---|---|---|
| tool invocations | count of `tool_call` | `task_started` | — | denominator of the failure ratio |
| tool failures | count of `tool_response` with error status | `task_failed` | — | numerator of `task_failure_ratio`, the `failure:tasks` channel member |
| errors / exceptions | count of `error` events | `DriftCount("tool_error", …)` | critical | a surfaced execution error is worst-severity drift |
| retry loops | a `tool_call` whose `(tool, args)` repeats an earlier `tool_call` | `DriftCount("looping_tool_call", …)`, one per repeat | warning | the canonical "same tool, same args" loop |
| transfer churn | count of `agent_transfer` | `DriftCount("agent_transfer", …)` | info | excessive handoffs are weak-signal drift (info-weighted so churn shows without swamping) |
| model usage | count of `model_usage`; sum of input+output tokens | `llm_call_count` → `cost:llm_calls`; `token_count` → `cost:tokens_spent` | — | the cost envelope |
| output size | summed `agent_message` text length | `agent_text_chars` → `output:chars` | — | the output envelope |
| turn / latency envelope | `agent_message` / `user_message` order | `agent_turns` / `user_turns` → `turns_completed`, memory/context heuristics | — | multi-turn shape signals |
| identity | first non-empty `run_id` / `session_id` | `run_id` / `adk_session_id` | — | the harmonograf deep-link (best-effort; empty when absent) |

The retry-loop rule spans the whole log: a `(tool, args)` pair counts when
it repeats any earlier `tool_call`, not only the immediately preceding one,
so a retry that brackets a failed response still counts. `args` are compared
by their canonical JSON (`sort_keys=True`) so key ordering does not
change the verdict.

### 3.3 Capability tier — what `adk_events` CANNOT provide

Honest tiers matter more than a long signal table. Relative to
`goldfive`, an event log is strictly weaker:

- **No in-process drift instruments.** There is no reasoning-stream
  telemetry in an event log, so none of the live detectors
  (`looping_reasoning`, `plan_divergence`, `intent_divergence`,
  `confabulation_risk`, `goal_drift`, …) can fire. `adk_events` sees
  *behaviour* (tools, transfers, errors), never *reasoning*.
- **No custom process-judge drift.** Custom judges are goldfive
  `JudgementEmitted`/`DriftDetected` pairs; an event log carries no
  judgements, so `custom:<judge_name>` drift is never produced and the
  whole `judge:` channel — its coefficient and `per_judge_weights` alike
  — is inert under this dialect (§4 warns on both).
- **No collusion-guarded emulator introspection.** The emulator lane and
  its answer-leak guard are goldfive-side; `adk_events` has no visibility
  into them.
- **`plan_revisions` is `0`** unless a framework happens to emit a
  plan-revision event (not in the recognised set) — the drift-side plan
  term is effectively absent.

`adk_events` is the right dialect for a real, non-instrumented agent that
nonetheless writes a structured trace: it recovers the failure/cost/loop
envelope, which is most of what a tournament needs to rank candidates,
while being explicit that the reasoning-quality signal is missing.

## 4. Tier 3 — `transcript` (the floor)

No telemetry at all. The input is a bare transcript JSONL (lines of
`{"role": "user"|"assistant", "content": "…"}`), or nothing. The
`transcript` dialect produces:

- **no drift** (`drift_counts == ()`), **no task counts**, **no tokens**;
- `agent_turns` / `user_turns` reconstructed from the transcript (so the
  zicato-derived multi-turn *feature* signals, memory-failure and
  context-loss, still work, because they are features rather than loss)
  and
  `output:chars` from the assistant text.

Scoring degrades to **predicates + optional in-run judges only**.

### 4.1 The degrade decision: explicit zero-drift, no renormalization

The drift channel is structurally `0.0` under `transcript`: with
`drift_counts == ()` and `plan_revisions == 0`, `builtin_drift_loss`
returns `0.0`, so the `drift` component is `namespace_weights["drift:"]
× 0 = 0`. The `judge:` channel is likewise structurally empty. The
scalar reduces to the pass/miss term (`pass_weight × (1 - mean_score)`)
plus the channels a transcript CAN still populate: `failure:` (a run
that crashed or was killed did so regardless of dialect), `runtime:`,
and `output:chars`.

We take the **explicit zero-drift stance** and do **not** renormalize the
weights. Justification:

- The dialect is pinned per epoch (§5). Scalars are never compared across
  dialects — `promote_margin` is applied to scalar *deltas within one
  contract*. A pure pass-term scalar is already a valid, monotone,
  lower-is-better axis; the gate works unchanged.
- Renormalizing (e.g. rescaling `pass_weight` to "absorb" the missing
  drift budget) would silently move the magnitude the `promote_margin`
  noise threshold is calibrated against, turning an honest structural
  absence into a hidden re-tuning of the gate. Zero-drift keeps the pass
  term meaning what it means under any other dialect.

So a `transcript` contract is a pass/predicate-only tournament. That is a
real, useful mode (invariant/regression boards with no telemetry), and it
degrades *honestly*: the operator sees a scalar with no drift component
rather than a scalar that pretends drift was measured.

### 4.2 Config-validation story (warn-or-refuse, preflight house style)

A contract can *ask for* drift it cannot get — e.g. a `transcript`
dialect with a non-default `namespace_weights["drift:"]`, a populated
`per_kind_weights` / `per_judge_weights`, a `drift_kind_aggregation`, or
a `drift_reducer` plugin. Those knobs would silently do nothing. Following the preflight
house style ([PREFLIGHT / board-reflection](OVERFITTING.md) — default
*warn*, recommend-only; opt-in *refuse*):

- **Refuse (fail-fast) only for a genuine config error:** an *unknown*
  dialect name is rejected at contract load in
  `ScoringWeights.__post_init__`, the way an unknown transform op is.
- **Warn (recommend-only) for a capability mismatch:**
  `dialect_capability_warnings(weights)` is a pure function returning the
  human-readable list of "this knob is inert under this dialect" findings
  (the drift-shaping knobs under `transcript`; the judge-channel knobs
  under either, since neither carries judgements). The `failure:` and
  `runtime:` channels are never reported inert, because run outcome and
  wall-clock are facts of the harness rather than of the telemetry
  stream. The reducer logs them at
  `warning` when it resolves a non-`goldfive` dialect. It does not refuse
  the run — a drift-weighted transcript contract still scores correctly
  (the drift term is just zero); the warning tells the operator their
  tuning is a no-op.

A hard *refuse* gate on capability mismatch (mirroring the preflight
`refuse` mode) is a natural follow-up but is intentionally not wired this
wave — the default posture across zicato is recommend-only.

## 5. Contract mechanics

The dialect is part of the **evaluation contract** — changing it selects
champions under a different measurement rule, so it must roll the epoch.
It lives as a field on `ScoringWeights`:

```python
telemetry_dialect: str = "goldfive"
```

`ScoringWeights` is already the evaluation-contract carrier for every
scoring-adjacent property (`tournament_structure`, `overfitting`,
`proposer_quality`, the declarative transforms and dotted-spec plugins),
and it is already threaded into `reduce_loss` as `weights` — so the
dialect selection reaches both the orchestrator and the killable worker
through the SAME field-enumerating serde (`to_json` / `from_json`) that
carries `drift_reducer` across the worker boundary, with zero new
plumbing. The worker reduces under the contract's dialect and never under
a guessed default.

**Omit-at-default.** `telemetry_dialect` is listed in
`epoch/contract.py::_SCORING_OMIT_AT_DEFAULT_FIELDS`, so while it holds
its `"goldfive"` default the scoring canonical form omits the key
entirely, so a contract that leaves the dialect at its default hashes
byte-identically to one that does not carry the key at all, and no epoch
rolls (the contract-hash parity gate stays green). A non-default dialect
(`adk_events` or `transcript`) reintroduces the key and rolls the epoch,
as any other weight change does. The contract pins the dialect in both
directions: setting it rolls, and reverting it to `goldfive` rolls back
to the original hash.

## 6. Determinism

Every dialect is a **deterministic re-reduction of a durable file**
(TELEMETRY.md §7 discipline): given the same JSONL and the same inputs,
a re-run of the reducer yields a byte-identical `LossProfile`. This holds
per dialect:

- `goldfive` — unchanged; the JSONL is the canonical record.
- `adk_events` — the walk is order-deterministic; the retry-loop rule
  keys on canonical (`sort_keys=True`) `args` JSON so key ordering in the
  source cannot flip a verdict; `drift_counts` is `sorted()` before it is
  frozen, so map-iteration order never leaks.
- `transcript` — a pure function of the transcript lines.

Because dialects are pure re-reductions, a captured event log plus its
known-answer `LossProfile` is a permanent regression fixture: re-reducing
the committed fixture must reproduce the committed numbers to the bit.

## 7. Out of scope this wave (follow-ups)

- **A hard `refuse` gate** on capability mismatch (§4.2).
- **Additional dialects.** The registry is open; a new dialect is a new
  `reduce_<name>` producer plus a registry entry plus a KAT fixture.

The **GUI / builder surface** is now built (it was the deferred follow-up
here). The dialect is exposed as the `set_telemetry_dialect` builder op —
declared through the field's `_knob(builder_op=…, builder_arg="dialect")`
metadata and wired (guard-driven, `test_knob_registry.py`) through the API
dispatch, the copilot tool, and a `<select>` row in the builder's Weights
panel. The row renders the selected dialect's capability tier (§2 / §3.3 /
§4) inline as a quiet caption, and changing the dialect rolls the epoch like
any scoring change (a non-default value reintroduces the omitted contract
key). It is still the first knob added under the declarative-knob-registry
discipline (REIMPLEMENTATION.md Finding 3).

## 8. Cross-references

| Topic | Document |
|---|---|
| The single-producer telemetry path + `LossProfile` shape | [TELEMETRY.md](TELEMETRY.md) |
| The drift-loss scalar formula | [SCORING.md](SCORING.md) |
| Contract hashing + omit-at-default | `epoch/contract.py` |
| Preflight warn/refuse house style | [OVERFITTING.md](OVERFITTING.md) / `epoch/preflight.py` |
