# 02 — Architecture: one round, end to end

> **Covers:** the process topology · `evolve_n_rounds` (invocation ownership, the loop, its circuit breakers, resume, control protocol) · `evolve_once` round preparation step by step · the shared field pipeline every tournament structure runs through `evolve_field_round` · the canonical RoundLog event sequence · the data-type flow table (who constructs, who consumes, where persisted) · the extracted-seam inventory and the seam rule · where the Rust supervisor sits.
> **Prerequisites:** chapter 01 (vocabulary, Golden Rules).
> **Invariants introduced:** [round-steps-live-in-seams] [epoch-cumulative-round-numbering] [train-selects-holdout-confirms] [validate-the-decision-before-committing-it] [crowning-invariant] [single-round-semaphore] [deferral-is-not-rejection] [patch-the-owning-module] [progress-seq-advances-on-transitions-only] [best-effort-vs-load-bearing]

This chapter walks one evolve round as the code in this tree runs it. The
round pipeline is a set of named seams under `src/zicato/evolve/`. Read the
chapter with `src/zicato/evolve/round_entry.py`, `src/zicato/evolve/field.py`
and the phase modules its facade calls
(`field_candidates.py`, `field_execution.py`, `gate.py`, `settlement.py`),
and `src/zicato/evolve/loop.py` open. Every step names the symbol that owns
it; if you cannot find a step's symbol, the code has moved and this chapter
needs an erratum.

`docs/design/ROUND-PIPELINE.md` specifies the seam contract. The prepare
phase creates an immutable
`zicato.evolve.generation_phase.PreparedRound`, which `evolve_field_round`
wraps in a `FieldRound`; that module also owns the champion, snapshot,
next-id, and mutable-tree helpers. Import those operations from
their owner directly — the orchestrator holds no forwarding seams for them.
The gauntlet and field drivers each expose one ordered asynchronous entry
point; supporting concerns live in owner modules under 1,000 lines. Do not
split a driver to meet a file-size target: extract only a phase with a typed
result that shortens the set of local variables the driver keeps live.

---

## 1. The process topology

One `zicato evolve` invocation involves FIVE kinds of process:

```
                        ┌────────────────────────────────────────────┐
                        │  zicato evolve  (the ORCHESTRATOR process)  │
                        │  evolve_n_rounds → evolve_once per round    │
                        │  single writer of the workspace             │
                        └───────┬───────────────┬───────────┬────────┘
          spawns per board unit │               │ writes     │ auto-launches
                                ▼               ▼            ▼
   ┌────────────────────────────────┐   ┌──────────────┐  ┌──────────────────┐
   │ python -m zicato._tournament_  │   │ .zicato/     │  │ harmonograf      │
   │ worker  (ONE per run; in its   │   │ runtime/     │  │ server (in-proc, │
   │ own OS process; killable)      │   │ heartbeat,   │  │ free localhost   │
   │ chdirs into an ephemeral       │   │ progress log,│  │ port) — per-run  │
   │ generation checkout            │   │ active runs/ │  │ execution view   │
   └────────────────────────────────┘   │ tournament,  │  └──────────────────┘
                                        │ control files│
   ┌────────────────────────────────┐   └──────┬───────┘  ┌──────────────────┐
   │ zicato-supervisor (RUST,       │◄─────────┤ reads    │ dashboard service │
   │ separate binary, :7920)        │          └─────────►│ (Python/Starlette,│
   │ watchdog: kills wedged /       │                     │ :7892) SSE over   │
   │ over-deadline worker pids;     │                     │ the same files    │
   │ alarm-only integrity notary    │                     └──────────────────┘
   └────────────────────────────────┘
```

The coupling discipline: the orchestrator is the ONLY writer of the
workspace (enforced by `acquire_workspace_lock`,
`src/zicato/runtime/lock.py`); the supervisor and the dashboard couple to
it exclusively through the state FILES under `.zicato/runtime/` and the
store-of-record tree under `epochs/` — never through an API into the
orchestrator process. Operator actions flow the other way through
control FILES (`src/zicato/runtime/control_consumer.py`), claimed by the
orchestrator at defined safe points. This file-mediated topology is why a
wedged Python event loop can still be killed (the supervisor is its own
OS process) and why the dashboard can render a run that already crashed.

---

## 2. `evolve_n_rounds` — the loop around the round

`evolve_n_rounds` lives in `src/zicato/evolve/loop.py` and is exported from
`zicato.orchestrator`. Its signature is stable, and the CLI's `zicato evolve`
is a thin shell over it. The public function only opens
`validated_invocation` (`src/zicato/evolve/invocation.py`) and hands the
resulting `InvocationContext` to the private loop body `_evolve_n_rounds`.
`evolve_once` has the same shape: it opens its own `validated_invocation`
and delegates to `_evolve_once`. The loop body calls `_evolve_once`
directly, so a multi-round invocation acquires ownership and validates the
workspace once. Loop collaborators are imported from their owning modules
inside the function body; tests patch those owners directly.

### 2.1 Startup, in order

1. **Stop-reason plumbing.** `stop_reason_out` (optional caller list)
   receives exactly one symbolic terminal string: `"completed"`,
   `"consecutive_rejections"`, `"degenerate_health"`,
   `"preflight_refused"`, `"wall_clock_budget_between_rounds"`, or
   `"wall_clock_budget_mid_round"`. `rounds <= 0` returns immediately
   with `"completed"`.
2. **Ownership first — `validated_invocation`.** Before any validation or
   execution write it acquires the workspace lock
   (`acquire_workspace_lock(workspace_root, instance_id)`,
   `src/zicato/runtime/lock.py`) — two concurrent orchestrators must not
   share a workspace. Under the lock it finishes any interrupted contract
   or epoch publication (`recover_contract_publication`,
   `recover_epoch_publication`), reads the workspace `config.json` once,
   resolves the invocation's configuration (`resolve_configuration`), and
   resolves an already-running harmonograf endpoint if one is configured
   or recorded.
3. **Mandatory workspace gate.** Still inside `validated_invocation`, and
   before auto-epoching or any model call, `zicato.check.require_workspace_valid`
   runs: against the live contract (`live_contract=True`) when no epoch is
   pinned, or through `InvocationContext.select_epoch` against the pinned
   epoch's captured `execution.json`. It reconstructs the adapter through
   the same worker-spec path as tournament workers — under the same
   environment a worker would be given — and enumerates the adapter-scoped
   snapshot under the contract's mutation syntax. Library callers and the
   CLI therefore share the same spend boundary; `--dry-run` runs the same
   validators before exiting.

   The gate makes no model call, which is what keeps it mandatory: a check
   needing the network would refuse every offline workspace, every fixture,
   and the parity capture. So the half of role checking that needs a round
   trip — is the credential *accepted*, does the model id *exist*, does the
   callable return a `str` — lives in `check/reachability.py` and runs on
   `evolve --dry-run` alone, after the offline validators have passed. It
   sends one short fixed request per configured `models.<role>`, building
   each role's callable through `models_config.lazy_text_call_llm`, the same
   path `_tournament_worker._resolve_role_call_llm` uses, so whatever
   authentication the spec implies (a named `api_key_env`, or the ambient
   credentials a keyless endpoint spec relies on) is exercised rather than
   assumed. Each role is bounded by `ROLE_TIMEOUT_S` and reported on its own
   line — roles fail separately and have different remedies — and any role
   that does not answer makes the dry run exit nonzero. A workspace
   configuring no role is told nothing was probed, which is not the same
   answer as reachable.

   Findings come in two severities. A finding that proves the round cannot
   produce a valid measurement raises `WorkspaceCheckError`. A finding that
   proves only that the round will measure less than the operator most
   likely intended — a stale tree path, a span marker binding to no
   literal, a board whose entries mostly carry no expectation — is
   advisory: reported and logged, never a refusal, because those
   workspaces still produce valid measurements. The severity of a code is fixed in
   `check.validators.ADVISORY_CODES`.

   The board-coverage advisory (`no_expectations`) is the one finding the
   gate shares with the loop-health report. Its rule — which entries count
   as ungraded, and the `health.no_expectations_fraction` their fraction
   must exceed — is declared once, in
   `zicato.board.expectation_coverage.measure_expectation_coverage`, and
   read by both the validator and `health.diagnostics`. The gate says it
   before the first round is paid for; the health report says it again
   once rounds have run.
4. **Runtime binding.** `_evolve_n_rounds` builds the `RuntimeConfig`
   once (`make_runtime_config`, then `bind_runtime_to_epoch` when an epoch
   is already selected) and records which callable serves each model role
   (`execution_roles_for_runtime`). It then installs the invocation's
   structured log stream under `.zicato/logs/`.
5. **Crash-resume reconciliation, then contract-hash auto-epoching, ONCE.**
   When `epoch_id is None`, `prepare_resume` (`src/zicato/runtime/resume.py`)
   first reconciles the CURRENT epoch under the lock, so contract drift
   cannot close it or seed a new epoch from its promoted head while a
   receiptless field is still on disk. `ensure_epoch_for_contract` then
   resolves (and, on drift, rolls) the epoch; its `before_contract_roll`
   hook discards an in-place resume that the roll would make incomparable.
   If the resolved epoch differs from the one reconciled, `prepare_resume`
   runs again on the new epoch. The resolved id is pinned for every round
   of this invocation so the loop never re-rolls mid-flight. An explicit
   `epoch_id` skips auto-rolling entirely — an explicit target always wins
   — and is reconciled directly. (Mechanics: 03-contract-and-epochs.md
   §3.8, "The epoch lifecycle".)

   `prepare_resume` clears stale runtime state from a prior dead evolve. If
   that prior run died mid-tournament with completed board units on disk,
   it returns a `ResumePlan` that resumes that generation in place. On ANY
   ambiguity it discards the partial generation. A clean workspace yields
   the no-op plan; the plan is consumed by the FIRST round only
   (`resume_plan = None` after round one).
6. **Epoch binding.** `invocation.select_epoch(epoch_id)` binds the
   verified epoch (and re-runs the workspace gate against its captured
   execution contract). The contract's mutation syntax table is installed
   (`install_syntax_table`), dialect-capability warnings are logged, and
   the effective concurrency is logged.
7. **Index preflight.** `index_preflight` rebuilds or heals the derived
   index from canonical records, best-effort, after recovery has finished
   all canonical writes and before proposer memory reads the index.
8. **Progress log cleared.** `progress_log.clear_log(writer)` so this
   invocation's `seq` starts from 1 — "a stale tail must never read as
   live progress".
9. **Harmonograf, meta-loop emitter, heartbeat.**
   `_resolve_or_launch_harmonograf(...)` returns the console URL plus a
   shutdown handle (auto-launched in-process unless the workspace
   configures an external URL); `_build_meta_loop_emitter_safe(...)`
   builds the goldfive emitter for zicato's OWN LLM calls (proposer,
   judges, analyzer) — degraded installs get a no-op emitter. Then
   `HeartbeatBeater(workspace_root, instance_id, interval_s=2.0)` is
   created. Each registers its teardown on the invocation's resource stack
   (§2.5).
10. **First genuine transition.** `LOOP_START` is appended to the progress
    log through `_record_progress`, and its `seq` is stamped onto the
    heartbeat.

> ⚠️ **TRAP** — the progress log's monotonic `seq` advances ONLY on
> genuine transitions (`LOOP_START`, `ROUND_START`, `PROPOSE`,
> `TOURNAMENT_START`, `TOURNAMENT_SETTLE`, `PROMOTE`/`REJECT`, terminal
> `SETTLED`/`STOPPED`), never on the heartbeat timer. A reader
> distinguishes "slow but alive between transitions" from "stalled" by
> whether `seq` moves. If you add a loop phase, append a
> transition for it via `_record_progress` (or `_beat(..., progress=…)`);
> if you make the heartbeat
> bump `seq`, you have destroyed the liveness signal.

### 2.2 The three stop policies + the infra deferral

The loop's circuit breakers are small policy objects constructed once per
invocation (`src/zicato/evolve/loop.py`):

```python
class ConsecutiveRejectionPolicy:
    """Stop after ``limit`` rejected rounds in a row.

    A promotion resets the run; ``limit <= 0`` is treated as "never stop
    early" by the caller (which normalises it to ``rounds + 1`` before
    constructing this policy), so this object always sees a positive limit.
    """

    reason = "consecutive_rejections"

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._streak = 0

    def observe(self, *, promoted: bool) -> bool:
        """Record a round's promotion verdict; return ``True`` to stop."""
        if promoted:
            self._streak = 0
            return False
        self._streak += 1
        return self._streak >= self._limit
```
*(src/zicato/evolve/loop.py, `ConsecutiveRejectionPolicy`)*

| Policy | Fires when | Default | Rationale |
|---|---|---|---|
| `ConsecutiveRejectionPolicy` | `max_consecutive_rejections` rejected rounds in a row (default 3) | on | the proposer is stuck; the operator should inspect the brief/patterns before spending more LLM calls |
| `DegenerateHealthPolicy` | `_DEGENERATE_HEALTH_STOP_THRESHOLD = 2` consecutive CRITICAL loop-health rounds | on (`stop_on_degenerate_health=True`) | two CRITICAL rounds in a row means the loop is producing no usable signal (e.g. degenerate scoring); one could be a transient |
| `WallClockBudgetPolicy` | total elapsed ≥ `max_wall_clock_seconds` | off (`None` = unbounded) | enforced BOTH between rounds (clean stop) and within a round (`asyncio.timeout` with the *remaining* budget; only expiry of that deadline turns the cancelled round into a synthetic `"wall_clock_budget"` rejection via `_budget_aborted_outcome` — any other timeout propagates) |

The within-round guard is an `asyncio.timeout` — the per-call and
per-budget timeout layer of the robustness stack — so it pre-empts only
*cooperative* async work. A round wedged in a blocking call is NOT killed
here; killing it is the job of the subprocess worker boundary (§5) and of
the supervisor (`docs/design/ROBUSTNESS.md`; 08-supervisor.md).

**The exit that is not a stop.** A round returning
`DEFERRED_INFRA_DECISION` (`"deferred_infra"`) — the endpoint-outage
circuit (see §3.10) — bypasses both streak-counting stop policies:

> a deferral is evidence about the endpoint, not about the experiment
> stream, so it must neither count toward consecutive rejections nor
> reset/advance the degenerate-health streak.
> *(src/zicato/evolve/loop.py, the deferral branch comment)*

Instead the loop backs off exponentially (`infra_backoff_base_s`
doubling to `infra_backoff_cap_s`, both read once per invocation from the
resolved runtime configuration; no sleep follows the final round), re-runs
`prepare_resume` so the deferred
generation resumes in place if any unit completed, and continues.

> ⛔ **NEVER** map a new "the round could not be judged" condition onto
> `"rejected"`. Rejection feeds the consecutive-rejection breaker and
> spends the round's experiment. Follow the `deferred_infra` pattern: a
> distinct symbolic decision, nothing journaled, the experiment left
> un-outcomed for resume.

### 2.3 Round numbering is epoch-cumulative

The loop counter `round_idx` (`range(rounds)`) is invocation-local and
used ONLY for "round X of N" log messages. The PERSISTED
`epoch_round_index` continues the epoch's existing numbering:

```python
def _epoch_round_base(workspace_root: Path, epoch_id: str | None) -> int:
    """The next ``round_index`` for ``epoch_id`` — one past its highest
    already-persisted round.

    Re-running ``evolve`` on an EXISTING (un-rolled) epoch must CONTINUE that
    epoch's round numbering rather than restart at 0. The loop counter is
    invocation-local (``range(rounds)``), but ``round_index`` is persisted on
    each generation and the dashboard groups generations by it — so a restart
    collides the new field with the prior invocation's rounds in one bucket
    (the "v9 lands in Round 0 next to v1–v4" bug). Returns
    ``max(persisted round_index) + 1``, or ``0`` for a fresh / unreadable epoch
    (the answer for a brand-new epoch, whose first round is 0).

    A PARENTLESS generation is skipped: the epoch's seed is CARRIED (copied from
    the registered trees, or from a rolled predecessor's promoted head), never
    minted by a round, so it does not represent a round already spent. It still
    persists ``round_index: 0`` — ``write_seed_experiment`` builds it with the
    ``Experiment.round_index`` default — so counting it started a seeded-but-unrun
    epoch's first real field at 1 and left a phantom round 0 in the timeline. This
    is the same rule the round-timeline reader uses to identify the seed (the
    parentless generation), so writer and reader agree on what a round is.
    """
```
*(src/zicato/evolve/loop.py, `_epoch_round_base`)*

When a mid-loop `rubric_replacement` rolls the epoch (§2.4), the base is
recomputed for the fresh epoch (restarting at 0 there is correct — it IS
a new epoch). One closure detail matters: the per-round `_run_round` inner
function binds `_epoch_id: str | None = epoch_id` as a DEFAULT ARGUMENT, so
a mid-iteration reassignment is captured by value rather than late-bound.
The code comments on that Python trap in place.

> ⚠️ TRAP — count MINTED generations only. The seed is carried rather than
> minted, and it still persists `round_index: 0` (`write_seed_experiment` builds
> it with the `Experiment.round_index` default). Counting the seed makes a
> seeded-but-unrun epoch start its first real field at 1. Such an epoch is one
> halted by a pre-flight refusal, by a crash before the field landed, or by a
> budget stop. The round timeline then renders the seed's own bucket as a
> phantom round 0 in which the carried champion defends an empty field. Writer
> and reader both identify the seed the same way: it is the PARENTLESS
> generation.

### 2.4 The between-rounds operator safe point

Before scheduling each round, in this order
(`src/zicato/evolve/loop.py`, the control-protocol block):

1. `block_while_paused(workspace_root)` — a `pause_epoch` control file
   blocks scheduling until the operator clears it.
2. `claim_skip_round(...)` — a STALE skip flag between rounds is drained
   as a no-op (there is no in-flight round to abort); a LIVE skip is
   claimed in `_evolve_once` instead (§3.1), aborting that round
   cleanly.
3. `claim_rubric_replacement(...)` — an operator-provided new proposer
   brief is a CONTRACT EDIT, never a silent in-place patch:
   `_apply_rubric_replacement` waits for worker cleanup, publishes the
   payload to the LIVE brief through the typed contract operations
   (`operations.set_brief` then `operations.apply` under the invocation's
   writer), and re-runs `ensure_epoch_for_contract`, which rolls the epoch.
   The loop binds the rolled epoch with
   `invocation.select_epoch(epoch_id, intentional_roll=True)` and re-pins it
   for all subsequent rounds.

After each round, best-effort: the epoch-report refresh
(`regenerate_in_progress_html`, which delegates to the analyzer's
deterministic regeneration) so file:// readers see the latest lineage
without the dashboard.

### 2.5 Teardown — the invocation's resource stack

Whatever way the loop exits (completed, breaker, budget, exception,
Ctrl-C), `validated_invocation` closes the invocation. Teardown is an
`AsyncExitStack` (`InvocationContext.resources`) that unwinds in reverse
registration order, and cleanup is shielded so a second cancellation cannot
cut it short:

1. `drain_worker_cleanup(workspace_root)` — worker ownership finishes first:
   no board-unit subprocess outlives the invocation that spawned it.
2. `_mark_run_terminal(writer)` — the defensive terminal-state write: flip
   any lingering active-tournament envelope out of `phase="running"` so a
   normally-ended run never reads as a live tournament, even inside the
   heartbeat freshness window (a SIGKILL still cannot self-clean; the
   frontend freshness gate covers that residue).
3. `beater.stop()` — the heartbeat task ends; the file stops advancing.
4. meta-loop emitter `close()` — BEFORE the harmonograf shutdown, because a
   sink flushing its final buffer to the gRPC console wants the server
   still up.
5. `harmonograf_handle.shutdown()` — unconditional, so a crashed evolve
   still tears the embedded server down.
6. The driver-import scope closes.
7. `_repair_index` — a final best-effort `index_preflight` projects the
   settled records after every producer has closed; a failure logs that
   index repair is required and leaves canonical records authoritative.
8. The structured log stream closes, so diagnostics cover the final
   projection attempt.
9. `release_workspace_lock(writer)` — another orchestrator may now start.

> ⚠️ **TRAP** — if you add a resource with invocation lifetime, register
> its teardown on `invocation.resources` at the point where it is created,
> and choose that point with care: the stack unwinds in reverse. Anything
> that writes to the harmonograf console must close before the server
> shuts down. Anything that touches workspace files must close before the
> lock is released, because the lock is the mutual exclusion. Anything the
> dashboard reads as "live" must close before the heartbeat stops, or it
> will briefly read as alive-and-frozen.

---

## 3. `evolve_once` — round preparation and dispatch

`evolve_once` (`src/zicato/evolve/round_entry.py`) is the public one-round
entry point; the loop calls its body `_evolve_once` under the loop's own
invocation. `_evolve_once` prepares one round: it binds the epoch's frozen
evaluation inputs, constructs the configured selection strategy, and passes
a typed `PreparedRound` to the shared evaluation and settlement pipeline in
`src/zicato/evolve/field.py`. The gauntlet is the one-candidate strategy.
Every strategy uses the same execution tail. The numbered `# --- N. …`
comments in `_evolve_once` are the step names this section uses.

```
_evolve_once ─┬─ select the epoch; bind its captured execution contract (step 1)
              ├─ claim_skip_round (safe abort point, step 0)
              ├─ board, scoring, brief, proposer spec from the execution contract
              ├─ open RoundLog with the frozen contract hash (step 0b)
              ├─ build adapter, RuntimeConfig, per-round token ledger;
              │  wrap the proposer with best-of-N
              ├─ ensure the baseline and resolve the champion (step 2)
              ├─ A/A calibration, contract pre-flight, margin check,
              │  replicates in effect (steps 2a–2c)
              ├─ enumerate mutation points (step 3)
              ├─ split train/holdout; patterns, loss summary, failure profile,
              │  process exemplars (steps 4–5a)
              ├─ screen runner, candidate history, calibration summary
              ├─ make_strategy(...) and construct PreparedRound (step 5b)
              └─ evolve_field_round(prepared, resume_plan)

evolve_field_round  (a facade; each line below is one named phase function)
              ├─ assemble_candidate_field   produce the batch, settle an empty field
              ├─ execute_field_tournament   open the records, drive the strategy
              ├─ resolve_field_verdict      holdout, integrity checks, overrides
              └─ settle_field_round         record, commit, publish, and close
                   ├─ _build_field_settlement      one OutcomeRecord per challenger
                   ├─ _commit_field_settlement     commit the round record, promotion hook
                   ├─ _publish_field_observations  frontier row, settled live envelope
                   └─ _close_field_round           placebo control, epilogue, summary
```

### 3.1 Step 0 — the skip safe point

Once the epoch is resolved and bound, and before any proposer call or
tournament write, a pending `skip_round` control flag aborts the round:
`_skipped_round_outcome` fabricates a rejection-shaped outcome and the loop
moves on. The flag is consumed (archived to `control_log/`) so it fires once.

### 3.2 Step 1 — workspace, contract artifacts, proposer

The round resolves its epoch (the explicit id, else the invocation's bound
execution contract, else the `current_epoch` marker) and calls
`invocation.select_epoch`, which returns the `EpochExecutionContract` the
round reads everything from. Nothing is re-read from the live contract
files: `execution_contract.board_with_meta` returns `(board, disable_drift,
judge_only)` — the board-level meta rides everywhere the board goes — and
`execution_contract.scoring`, `.brief`, and `.proposer_spec` complete the
frozen contract view.

One subtlety computed right here and threaded far:
`_declared_custom_judge_names(board, weights)` — the union of every
`JudgeSpec.name` on every entry plus every `per_judge_weights` key. A
custom judge emits under the single `"custom"` drift kind on the
goldfive side, but a proposer hypothesis may still target it as a
`drift:<judge_name>` metric. This set is what lets the hypothesis
validator accept a declared judge name while still rejecting a drift kind
no judge declares. Forget to thread it into a new propose
site and every hypothesis touching a custom judge starts bouncing with
"unknown drift kind".

The epoch's proposer is resolved once per round from the captured spec:

- `build_proposer_agent(execution_contract.proposer_spec,
  external_config=execution_contract.external_proposer)` yields the
  `ProposerAgent` (05-proposer.md §5.1 lists what it can return);
- after the `RuntimeConfig` is built (§3.4),
  `wrap_with_proposer_quality(agent, weights.proposer_quality, …)`
  interposes the best-of-N + critique wrapper, routing slate sampling to
  the breadth callable and critique to the depth callable the config
  carries. A contract pinning `best_of_n: 1` gets the agent back
  UNCHANGED — the single-sample path.

Every candidate slot in the round reuses this same agent, so a configured
proposer's skills shape every challenger identically.

### 3.3 Step 0b — the durable RoundLog opens

`_RoundLogEmitter(workspace_root, epoch_id, round_index)` wraps
`RoundLog` (`src/zicato/epoch/round_log.py`) with best-effort emission —
"a log failure can never fail the round." The first event stamps the
frozen contract hash:

```python
    round_log = _RoundLogEmitter(workspace_root, resolved_epoch_id, round_index)
    round_log.emit("round_opened", {"contract_hash": _epoch_cfg.contract_hash or ""})
```
*(src/zicato/evolve/round_entry.py, `_evolve_once` step 0b)*

The event vocabulary is CLOSED and typed — one frozen dataclass per
transition, registered in `EVENT_TYPES`
(`src/zicato/epoch/round_log.py`): `round_opened`, `proposal_attempted`,
`proposal_episode_settled`, `candidate_sampled`, `candidate_screened`,
`critique_selected`, `experiment_minted`, `patches_applied`,
`harness_loaded`, `validation_failed`, `unit_completed`, `gate_evaluated`,
`holdout_released`, `evidence_replicated`, `decision_recorded`,
`frontier_updated`, `round_closed`. An unknown token reads back as a raw
envelope (typed payload `None`) rather than failing the fold. The log is
append-only, single-writer, `seq` gap-free, and torn-tail tolerant (an
unparseable LAST line is a crash artifact and skipped; an unparseable
INTERIOR line raises — someone bypassed the writer).

> ✅ **ALWAYS** emit a RoundLog event when you add a round step that
> makes or records a decision. The RoundLog is the round's
> store-of-record trace; a decision that leaves no event is invisible to
> `fold_round_record`, to the dashboard forensics, and to anyone debugging
> the round later.

### 3.4 Structure, adapter, runtime config, token ledger

`tournament_spec = weights.tournament_structure` — read off the frozen
weights so it is in lockstep with the contract hash. The adapter comes from
`adapter_factory.make_adapter_from_config` over the execution contract's
adapter configuration. The `RuntimeConfig` is the invocation's (built once
by the loop, §2.1 step 4), or `runtime_factory.make_runtime_config` for a
standalone `evolve_once`; `bind_runtime_to_epoch` binds it to the epoch's
captured execution roles. The round then records the settings in force,
with each value's source, onto the heartbeat (`effective_settings`).
When `config.max_tokens_per_round > 0`, a fresh `RoundTokenLedger` is minted
and rebound onto the config via `dataclasses.replace` — every scheduler
seam that already receives the config (the board-unit schedulers, the
screen, evidence replicate duels) shares one tally with zero signature
changes; knob off (default 0) binds nothing.

The optional `ScoringWeights.goldfive` object follows a separate path. It is
frozen contract data rather than a runtime knob. The worker exposes it as
`RuntimeConfig.goldfive` only when the adapter's worker specification declares
the `"goldfive"` integration. Zicato preserves the JSON mapping; the lazy
bridge in `src/zicato/integrations/goldfive.py` delegates its schema, defaults,
normalization, capability checks, credential resolution, and runtime
construction to Goldfive's `RuntimeConfigDocument` API.

### 3.5 Step 2 — baseline and parent

`_ensure_baseline_snapshot` materializes `v0` from the registered mutable
trees if the epoch has no generations yet (byte-for-byte copy of the
operator's source; on a contract roll it seeds from the previous epoch's
promoted head via the roll-seed marker — see 03-contract-and-epochs.md).
A resumed round keeps the parent its persisted experiment recorded;
otherwise `generation_phase.current_generation` returns the primary
generation from the most recent committed promotion, or `v0` before any
promotion. The parent `Generation` is constructed with `promoted=True`.

Then two idempotent epoch-open measurements, each persisted onto
`EpochConfig` (never hashed). `_maybe_calibrate_noise_floor` is opt-in
(config.json `"calibrate_noise_floor": K` — K champion draws under the
`calibration` purpose). `_maybe_contract_preflight` runs unless
`runtime.preflight_gate` is `"off"`: an A/A floor plus the degradation
signal against a degraded copy, under the `contract_preflight` purpose.
Its verdict warns by default; under `preflight_gate="refuse"` a refuse
verdict raises `PreflightRefusedError`, which stops the loop with
`"preflight_refused"` before any round spends budget. On round 0 only,
`_warn_margin_below_noise_floor` warns when `promote_margin` sits inside
the measured floor. `_resolve_replicates_in_effect` then fixes the
replicate count every duel of the epoch runs — the contract's pinned
value, else the smallest count whose minimum detectable effect at the
measured floor is within `promote_margin`, else the structure's default —
and stamps it, with its source, onto the effective-settings record.

Both measurements are SERIAL and front-loaded: K draws, each a full pass
over the board, before the round's first duel (the pre-flight adds one
pass per degraded probe), and `--parallelism` does not shorten them. Each
therefore owns the heartbeat while it runs, stamping
`CALIBRATION_PHASE` or `PREFLIGHT_PHASE` plus a `{done}/{total}` suffix
restamped per settled unit, and restoring the round's phase in a
`finally` (see the phase vocabulary in §6). Without that stamp the round
would show its own phase over a null tournament, which is the same shape
a wedged round has. Both log their whole expected cost before spending
the first draw.

### 3.6 Steps 3–5 — mutations, the split, patterns, the proposer's view

`enumerate_mutations(generation_phase.mutable_trees(adapter,
parent_gen.snapshot_root))` — zero mutation points is a hard `RuntimeError`
("did the adapter declare its mutable_trees?").
`write_mutation_inventory` publishes the enumeration to the epoch's
`mutations.json`, best-effort: a publication failure is logged and does not
abort execution.

Then the anti-overfitting boundary — worth reading verbatim because
every downstream proposer input flows through it:

```python
    # --- 4. Patterns ---
    # The proposer + detectors + loss summary see the TRAIN slice ONLY
    # (OVERFITTING.md §11.1, §12 #1): the holdout's per-entry behaviour is
    # never surfaced to the proposer, so it cannot be memorized. When the
    # board is too small to split (the default-safe degrade), the train
    # slice IS the full board and every downstream artifact is byte-
    # identical to the pre-split behaviour. The mutation manifest (code
    # spans) is unrelated to the split and is left untouched.
    from zicato.board.split import rotation_seed, split_board  # noqa: PLC0415

    # Thread the epoch id as the rotation seed (OVERFITTING.md §12 #6) so the
    # holdout slice is stable within this epoch but rotates across epochs.
    # ``rotation_seed`` returns ``None`` (the unseeded, byte-identical split)
    # when ``rotate_holdout`` is off.
    train_seed = rotation_seed(weights.overfitting, resolved_epoch_id)
    train_ids, _holdout_ids = split_board(board, weights.overfitting, seed=train_seed)
```
*(src/zicato/evolve/round_entry.py, `_evolve_once` step 4 — excerpt)*

Everything the proposer will see is computed from the TRAIN slice only:
`_load_parent_losses` (the champion's per-entry loss profiles),
`detect_patterns` over a `DetectorInput` of those losses + train entries
+ events paths, `build_metric_priorities` and its banded render (what the
contract scores, without the raw weights), `_render_loss_summary`,
`_render_failure_profile` (bucketed outcome marginals; empty slice renders
the EMPTY string — the "omit this section" sentinel), and
`_render_process_exemplars_block` (opt-in, redacted, best-effort, empty
string when off/failed). The restricted-visibility envelope
(`01-orientation.md §4`) is enforced here, at computation time, rather
than only at prompt-render time.

The round also writes the unrestricted patterns to
`rounds/{round}/patterns.json` through `write_round_patterns`
(`src/zicato/epoch/round_patterns.py`), best-effort like the mutation
inventory. The close-of-epoch retrospective reads every round's record; no
proposer input reads it.

### 3.7 Step 5a′ — the screen-runner closure and candidate history

`_build_candidate_screen_runner` returns `None` — and therefore no screen
callable even exists on the propose path — unless the contract opts in
(`proposer_quality.screen_entries > 0` AND `best_of_n > 1`). When built,
ONE closure per round binds one deterministic rotating TRAIN panel
(`select_screen_entries` over the champion's tournament draw-zero baseline; the
holdout is never eligible), so every propose site this round screens on
the same panel. The closure also stamps a `screening:r{round}` heartbeat
phase, so the stall detector attributes the wall-clock honestly.

`_build_candidate_history` assembles the opt-in recombination pair and
genealogy items, and `_build_calibration_summary` the opt-in banded
critic-calibration summary (`experimental.calibration_feedback`); each is
`None` or empty when its knob is off.

### 3.8 Step 5b — structure dispatch

```python
    strategy = make_strategy(
        tournament_spec,
        board_ids=[e.id for e in train_board],
        replicates=replicate_setting.replicates,
        noise_floor_delta_std=replicate_setting.delta_std,
        experimental=weights.experimental,
    )
    prepared = generation_phase.PreparedRound(..., strategy=strategy, ...)
    return await evolve_field_round(prepared, resume_plan=resume_plan)
```
*(src/zicato/evolve/round_entry.py, `_evolve_once` step 5b — excerpt)*

Board-aware structures (racing) get the train entry ids as default
`board_ids`; board-agnostic ones ignore them. All field widths use the same
evaluation and settlement function.

### 3.9 Step 6 — propose (or resume)

`assemble_candidate_field` (`src/zicato/evolve/field_candidates.py`) asks
`produce_candidate_batch` (`src/zicato/evolve/candidate_batch.py`) for
`strategy.field_size()` candidates; ids are minted from
`next_generation_id`. A one-candidate resume may reuse the plan's
`resume_generation_id`; a fresh id would orphan completed measurement files.
Each slot runs `_propose_and_apply_challenger`
(`src/zicato/evolve/propose_apply.py`, §4.2), which first captures the
parent's `MutationPolicy` (the permitted mutation points and forbidden ids)
and writes it under the parent's `mutation-policies/`. The proposer's
post-apply validation hook is built by the shared seam:

- `build_post_apply_validator` (`src/zicato/evolve/round.py`) — the
  `validate_experiment` hook the proposer agent calls on EVERY attempt:
  beat `applying`, derive the child snapshot all-or-nothing from the
  candidate's patches through the `GenerationStore`
  (`default_generation_store`), record it in `last_child_snapshot`, run
  `validate_post_apply`. A destructive patch is thereby a *retryable*
  feedback class (the validator strings go back into the proposer's next
  attempt) inside the same bounded `max_proposer_retries` budget, so it
  costs one retry rather than a whole tournament round.
- Experiment memory: `_load_prior_experiments` (best-effort; empty list
  on a stale index) — the "## What's already been tried" digest.

**What experiment memory carries.** `_load_prior_experiments` reads the
SETTLED cross-round digest for this epoch off the SQLite index —
best-effort: a missing/stale index yields an empty list and the proposer
simply runs without the `## What's already been tried` section. The
digest is curated rather than a dump. It is capped at
`EXPERIMENT_MEMORY_MAX_ENTRIES = 12` (`src/zicato/core/experiment.py` —
"wins are never dropped by the cap; the sharpest recent rejections fill
the remainder"). Each entry is a `PriorExperiment`: core idea, modulating
ids, decision, banded Δscalar under restricted visibility, and the
diagnostic `prediction_accuracy`. With
`experimental.cross_epoch_memory: true` (a field in the complete scoring contract),
settled experiments from PRIOR epochs sharing the current
`contract_hash` are appended — marked `same_contract=False`, Δscalar
omitted (the number does not transfer), and admitted only into budget
left after same-epoch entries. Experiments under a DIFFERENT contract
hash are never surfaced regardless. On the field path the caller
concatenates this settled digest with the round's in-flight siblings
(decision `"in_flight"`) so challenger k diversifies away from
challengers 0..k−1.

**The resume short-circuit.** When the plan resumes in place
for exactly this generation, the persisted experiment is reused verbatim
rather than re-proposed — the proposer is non-deterministic, and a fresh
proposal would invalidate the on-disk unit cache. The SAME validate hook
still runs once so the snapshot is re-derived idempotently from the
persisted patches; if re-validation fails (the parent tree changed
underneath), the round falls back to proposing fresh — "never score
against a tree we cannot rebuild."

Otherwise `_propose_child` builds the one `ProposerContext` shape used by
every candidate slot and calls the agent. Inside the wrapper, per propose-step:
N `candidate_sampled`
draws (each slot with a distinct edit-class hint), the optional guarded
screen (`candidate_screened` events; veto-first; one bounded revise
re-sample if all-vetoed), `critique_selected`, then the validate hook.
`_propose_child` then runs the validate hook once more itself: a returned
experiment does not prove that a custom proposer called the hook, or that
the returned patches are the ones it checked. On success it emits
`proposal_attempted` (empty errors), `proposal_episode_settled{completed}`,
`experiment_minted`, `patches_applied`, and stamps the authoritative
evolve `round_index` onto the experiment. On `ProposerError`, one
`proposal_attempted{errors}` per failed attempt plus one
`proposal_episode_settled{kind, code, message}` recording how the episode
ended are emitted, and the error propagates: the slot is rejected (§4.2).

**What the proposer sees — the `ProposerContext` inventory.** Every
input crossing into the propose step is enumerated here because this is
the restricted-visibility envelope (`01-orientation.md §4`): adding a
context field means adding a proposer-visible channel, which needs the
envelope argument written down. The fields as `_propose_child` populates them
(`src/zicato/proposer/agent.py` owns the dataclass):

| `ProposerContext` field | Populated from | Envelope status |
|---|---|---|
| `epoch_id`, `parent_generation_id`, `new_generation_id` | round coordinates | identity of the HARNESS rather than the board — safe |
| `patterns` | `detect_patterns` over TRAIN losses | aggregated to counts/rates under `restrict_visibility` |
| `mutations` | `enumerate_mutations` on the parent snapshot | code spans — unrelated to the board split, passed whole |
| `brief_text`, `forbidden_ids` | the frozen proposer brief | operator-authored — the steering channel |
| `current_loss_summary` | `_render_loss_summary` (train) | one line of means — no identities |
| `failure_profile` | `_render_failure_profile` (train) | bucketed, board-anonymous marginals; `""` = omit |
| `process_exemplars` | `_render_process_exemplars_block` | opt-in; mechanically redacted; `""` = omit |
| `prior_experiments` | `_load_prior_experiments` (+ in-flight siblings on the field path) | banded Δscalar under restriction; capped at 12 |
| `mutation_track_records` | `_load_mutation_track_records` (index; best-effort `{}`) | per-mutation-point fertility counts — no entries |
| `custom_judge_names` | `_declared_custom_judge_names` | names only |
| `metric_priorities` | `build_metric_priorities` + `render_metric_priorities_block` | banded priorities; raw weights never cross |
| `genealogy`, `recombine_pair` | `_build_candidate_history` (opt-in) | lineage summaries and a parent pair — no entries |
| `calibration` | `_build_calibration_summary` (opt-in) | banded, aggregate counts of the proposer's own past predictions |
| `workspace_root`, `writer`, `generation_root`, `mutation_policy` | the round's workspace owner, the parent snapshot, the captured `MutationPolicy` | where the episode's working copy comes from and what it may touch |
| `scratch_validator_factory` | `build_scratch_validator_factory` | a private scratch tree per slate slot |
| `sample_hint`, `slot_index`, `revise_feedback` | set per slot by the best-of-N wrapper | edit-class hint, slot number, counts-only veto or validation feedback |
| `aux_call_llm`, `model`, `max_retries` | runtime plumbing | — |
| `validate_experiment` | `build_post_apply_validator` | the retryable apply+validate hook |
| `restrict_visibility` | `weights.overfitting.restrict_proposer_visibility` | the envelope master switch (default on) |
| `screen_candidates` | `_build_candidate_screen_runner` | counts-only feedback channel; `None` = off |
| `round_event_emitter` | `round_log.emit` | write-only tracing; carries nothing back |
| `meta_loop_emitter` | loop-level goldfive emitter | write-only tracing |

> ⚠️ **TRAP** — nothing in this table carries a board-entry id, task
> text, or holdout-derived value. If your new field cannot make that
> claim in its docstring, it does not go on `ProposerContext` — put the
> raw signal behind an aggregating renderer first (the failure profile
> and the process-exemplars block are the worked examples of that move).

**Inside the best-of-N wrapper.** `BestOfNProposerAgent.propose`
(`src/zicato/proposer/best_of_n.py`) is the propose-step state machine
when `best_of_n > 1` (a `best_of_n: 1` contract short-circuits to a
single inner `propose` with NO critique and NO extra work):

1. **Sample the slate.** N independent inner `propose` calls, each slot
   carrying a distinct edit-class hint (`hint_for_slot`,
   `src/zicato/proposer/hints.py` — the hints steer the slots toward
   different edit strategies instead of re-rolling one). A slot the
   inner proposer cannot produce narrows the slate; an EMPTY slate
   falls back to one final inner `propose` so the step never silently
   yields nothing. One `candidate_sampled{i,n}` event per draw.
2. **Screen, guarded** (`_screen_slate`; only when the orchestrator
   threaded a `screen_candidates` runner): each candidate runs the
   round's fixed train panel; a confirmed pass-flip on a
   champion-passing entry or a budget abort VETOES it
   (`candidate_screened{vetoed, confirmed}`). Any screen failure
   degrades to an unscreened selection — screening can never fail a
   propose.
3. **Revise, bounded** (`_revise_all_vetoed`): an all-vetoed slate takes
   exactly ONE feedback-informed re-sample — the counts-only veto
   summary rides `ProposerContext.revise_feedback`, the same slot a
   validation failure uses on retry — screens the replacement guarded,
   and returns it if it survives. A vetoed replacement degrades to
   critic-over-all. No new config knob: the revise rides
   `screen_entries > 0`.
4. **Select** (`_select_best` / `_select_over`): the evaluation-LLM
   self-critique when `critique_enabled` (scored against a quality bar
   — grounded in a tool call? targets a real failure mode? minimal
   diff?), else the deterministic heuristic (smallest diff targeting an
   observed failure mode). Survivors' banded panel counts feed the
   selection only as a LATE tiebreak, and not at all under
   `screen_veto_only`. `critique_selected{index, reason, slate,
   rationale}` — the mode, a per-candidate summary of the whole slate
   (core idea + mutation ids), and, when a critic chose, its one-line
   reason. The critic and the deterministic heuristic write the identical
   shape, so a reader cannot tell which route chose from the event's form.
5. **Mount the chosen candidate** (`_mount_chosen`) — the step easiest to
   overlook. Each slate slot validates into its OWN scratch
   tree (`GenerationStore.derive_scratch`, leased per slot by
   `build_scratch_validator_factory`); a scratch tree never enters the
   generation namespace and is discarded with the slot, so nothing has
   been derived into the round's real `next_id` when the selection
   ends. After selection the wrapper therefore derives the CHOSEN
   candidate into `next_id` exactly once, through the round's shared
   validate hook. That single derive is what makes the mounted tree and
   the persisted experiment the same artifact, and it is what populates
   `last_child_snapshot["path"]` for the caller. There is no shared
   tree to fall back to, so an unexpected finding here (the parent tree
   changed underneath the slate) raises the standard `ProposerError`.
   Every field width is covered e2e by
   `tests/test_best_of_n_tree_integrity.py`.

> ⛔ **NEVER** decouple "the experiment we persist" from "the tree we
> mount". Every return path out of the best-of-N wrapper — a new
> tiebreak, a new degrade, a second sampling pass — must funnel through
> `_mount_chosen`, because that derive is the only thing that makes
> tree and record agree.

### 3.10 Manifest check, the rejected tail, the matchup, the infra circuit

After a candidate returns, `check_patch_manifest_and_forbidden`
(`src/zicato/evolve/round.py`) cross-checks every patch's `mutation_id`
against the re-enumerated manifest and the brief's `## Forbidden edits` ids and
raises `BadPatchSetError` (a `ValueError`) on either.

If the only slot of a one-candidate field exhausted its proposer retries,
`_settle_field_that_produced_nothing` (`src/zicato/evolve/field_candidates.py`)
settles the round through `_persist_rejected_round`
(`src/zicato/evolve/persist.py`). The experiment is written with a
rejected `OutcomeRecord` whose reason is symbolic
(`"validation_failed: …"` vs `"proposer_retries_exhausted: …"`) and
folded through `_finalize_generation` with NO lineage entry, because the
generation never earned one. `validation_failed` and `decision_recorded`
land on the RoundLog. `_round_epilogue` still runs, minus the analyzer
that this tail skips, so a stuck loop surfaces on the dashboard even when
nothing ever reaches a tournament. A field whose every failed attempt was a
transport failure defers instead (`deferred_infra_proposer_outage`): the
endpoint failed, and the proposer produced nothing to judge. Wider
all-failed fields settle as
described in §4.2.

Otherwise each applied challenger is persisted (§4.2) and the strategy's
matchups run. Every scheduled matchup, the gauntlet's single duel included,
goes through `run_field_matchup` (`src/zicato/evolve/field_execution.py`),
which calls `run_matchup` (`src/zicato/tournament/runner.py`) on the train
board with the matchup's `replicates`, both sides' diff sizes (the opt-in
parsimony term), and `fast=prepared.fast_mode or candidates.resume_cache`.
It caches both sides' aggregates to `gen_score.json` (with a
`gen_score.history.jsonl` line per write) unless `cache_scores=False`, and
emits the matchup's units and gate verdict onto the RoundLog.

**The aggregate dict — the shape everything downstream reads.** Both
sides of every duel are reduced by `aggregate_generation_score`
(`src/zicato/tournament/scoring.py`) into a plain JSON-shaped dict. It is
a dict rather than a dataclass because it is cached to `gen_score.json`,
crossed into envelopes, and consumed as-is by the gate, the strategies,
the dashboard, and fast mode. Its keys are therefore a wire contract:

| Key | Meaning |
|---|---|
| `drift_loss_mean` | mean per-run drift loss over the entries scored |
| `pass_rate` | binary pass fraction over entries WITH an expectation (empty ⇒ 1.0) |
| `mean_score` | the UNIFORM continuous outcome axis — equals `pass_rate` byte-for-byte on an all-bool board (the back-compat proof is in the source comment); the scalar's pass component and the gate's `aggregate` scope read THIS |
| `per_entry` | `{entry_id: {drift_loss, pass_fail, score}}` — the gate's `per_entry` scope reads `score` |
| `namespace_aggregates` | weight-multiplied per-namespace means (`cost:`, `latency:`, `rubric:`, `schema:`, …) — already sign-folded so lower is uniformly worse-to-better comparable |
| `scalar_components` | the display/gate breakdown: `drift`, `pass`, one entry per non-drift namespace, plus `diff_complexity` ONLY when opted in (the display key is absent when the term is disabled; configuration identity includes the setting) |
| `scalar` | the lower-is-better number the gate compares, synthesized through the Seam-2 dispatcher (`resolve_scalar`), byte-identical to `sum(scalar_components.values())` for the builtin path |

> ⚠️ **TRAP** — empty input aggregates to `scalar=0.0`, `pass_rate=1.0`
> ("nothing to compare", which the gate treats as a tie). If you build a
> new evaluation path, an accidentally-empty loss list does not error —
> it produces a plausible-looking no-op aggregate. Assert non-empty at
> your call site.

**Fast mode.** Every matchup uses the cache-first board-unit
evaluator. Its key is `(generation, entry, purpose, draw, base_seed)`, so both competitors
reuse completed slots and execute only missing slots. A requested replicate
is never synthesized by replaying another slot. Full mode forces both sides
fresh; a conservative crash resume enables cache reads even when full mode
was requested. Diff-complexity is supplied independently for the left and
right candidates and is applied in both modes. Round-level
`champion_eval_mode` is derived only from the reigning champion's unit
provenance: `full`, `fast`, or `fast-degraded`.

**The endpoint-outage circuit.** This runs inside `run_field_matchup`,
BEFORE anything downstream consumes the matchup. When
`config.infra_abort_round_threshold >= 1`, the round's running
`_count_infra_aborted_runs` tally across its matchups is checked after each
one; reaching the threshold raises `_InfrastructureRoundDeferred`, and
`execute_field_tournament` settles the round through
`_defer_round_infra_outage` as `deferred_infra`. The counter counts
`is_infra_abort_cause` losses — worker crashes and kills, never genuine
budget exhaustion — and a cache-reused unit can never contribute to it.
The tripping matchup caches no `gen_score.json` (a mostly-aborted aggregate
would poison fast mode), nothing further is routed to the strategy, and no
outcome or journal entry is written, so the round's experiments stay
un-outcomed on disk, exactly the shape `prepare_resume` reconciles. The
health report carries the `infra_outage` WARNING.

### 3.11 Tournament evaluation, evidence, integrity, and overrides

`evaluate_tournament` seeds every strategy, runs its scheduled matchups
through `run_matchup`, and returns a typed `TournamentEvaluation`. The
gauntlet schedules one duel; wider structures schedule their bracket, Swiss,
or racing topology. A strategy consumes the gate verdict and never re-decides
the duel.

When the contract sets `promote_confidence_threshold`, the driver calls
`confirm_promotion_with_evidence` before settlement. The pre-gate can only
withhold a promotion. While the verdict is unresolved, the driver runs fresh
crowning-pair duels on the train slice using the `evidence_confirmation`
purpose. `make_evidence_replicate_duel` creates
`MeasurementDraw(MeasurementPurpose.CONFIRMATION, replicates_run)`, advances
the local draw counter, and passes the identity as `first_measurement`.
It also sets `cache_scores=False` so a confirmation aggregate cannot replace
the tournament score.

Each additional confirmation requires an independent measurement from both
sides. A resumed request may reuse its matching completed measurement;
replaying that measurement must not add another observation to the fit.
The measurement identity rule is in `01-orientation.md §4`, G7.
A verdict still unconfirmed when the replicate budget is spent leaves the
champion standing: the decision goes terminally
inconclusive, the duel is recorded to the
dead-letter queue (`record_inconclusive`), and the journaled `evidence`
block carries the rating CIs plus the full `ci_history` trail. Each
refit's CI state also lands as an `evidence_replicated` RoundLog event.

**Opt-in integrity blocking.** `_integrity_block_reason` guards a
GATE-DECIDED promotion only (never an operator force-promote): (a) diff
containment — every file outside the registered mutable trees must be
byte-identical parent↔child (`zicato.evolve.containment`, mirroring
`crates/supervisor/src/diff_containment.rs`; fail-open on unreadable
snapshots); (b) gate-contradiction re-derivation
(`delta_scalar <= -promote_margin`, the supervisor's
`promotion_gate.rs check_row` semantics applied pre-persist). Both
default OFF, so the default posture matches the supervisor's alarm-only
stance.

**Operator gate overrides.** `resolve_field_verdict` claims them with
`claim_field_gate_overrides(workspace, field_candidate_ids)` at its one safe
point (evaluation settled, nothing persisted); §4.4 describes the
re-resolution. An override is NEVER a silent flip: `operator_override` +
`operator_override_reason` are stamped onto the `OutcomeRecord`, and a
forced reject carries `"operator override: …"` in `rejection_reason`.

### 3.12 Persist, placebo, epilogue

`_build_field_settlement` assembles one `OutcomeRecord` per applied
challenger with every runtime-evidence field: deltas, structure, the
round's `champion_eval_mode`, holdout evidence, train and holdout loss, the
generalization gap, and the statistical-evidence block. A resolved
tournament records every candidate outcome and the complete tournament in
one round record, `rounds/{round}/field_settlement.json`. Committing that
record publishes all outcomes and the primary promoted generation together.
Readers derive experiment outcomes, lineage status, the champion, and the
journal from the committed record. Index refresh follows publication. A
validation or proposal failure before tournament execution uses
`_finalize_generation` to record the rejection directly in the proposal
file and refresh its index row.

A rejected generation remains visible in lineage and `zicato epoch list`.
The champion changes only when a committed round names a primary promotion.
The RoundLog event `decision_recorded` carries the structure, reason, override
flags, parent id, and promoted ids.

**The holdout block and the generalization fields.** When holdout
confirmation ran for the crowning challenger, the verdict carries the
Ladder-mediated evidence block — a plain JSON dict with the stable shape
built by `holdout_record` (`src/zicato/tournament/ladder.py`) and
journaled verbatim under `OutcomeRecord.holdout`:

| Key | Meaning |
|---|---|
| `confirmation_status` | `satisfied`, `failed`, `incomplete`, or `disabled` — only a released confirmation satisfies the candidate |
| `reason` | why the status holds; never reveals an unreleased negative result |
| `confirmed` | `True`/`False`/`None` — the confirmation bit (a withheld release may repeat a historical value) |
| `train_scalar` / `holdout_scalar` | the crowning duel's two slice scalars |
| `holdout_consulted` / `ladder_released` | whether the holdout was queried, and whether the release rule fired |
| `ladder_budget_total` / `ladder_budget_before_query` / `ladder_budget_remaining` / `ladder_query_reserved` | the per-epoch holdout-query budget accounting |
| `threshold` | the train-improvement bar the release rule applied |

Alongside it, `_generalization_fields_from_scalars`
(`src/zicato/evolve/decision_support.py`) pairs the crowning challenger's
TRAIN-slice scalar (the score that gated it) with its HOLDOUT-slice scalar
into `train_loss`, `holdout_loss`, and `generalization_gap`. The
holdout-slice scalar is decoupled from the Ladder's release semantics, so
the gap is measurable whenever a holdout exists. A positive gap means the
holdout scored worse than the train slice, the memorization signature the
health detector reads off the champion lineage. All of these are RUNTIME
evidence, never contract inputs.

**The placebo arm.** On the opt-in cadence
(`experimental.random_baseline_every_n`) a wider field carries one
placebo challenger inside its slate (§4.2). A one-challenger field has no
room for it, so `_close_field_round` runs
`_maybe_run_placebo_arm_gauntlet` — one EXTRA duel, champion vs a
semantics-preserving no-op copy of itself — after settlement and BEFORE
the health assessment, so a promoted placebo raises its CRITICAL finding
in THIS round's report. The placebo never advances the champion.

**`_round_epilogue`.** The shared end-of-round tail — loop-health
assessment persisted to `epochs/{epoch}/health/round_{N}.json` (CRITICAL
no-signal warning to stderr), the decision-telemetry analyzer (reads only
the training slice's runs and writes `insights/round_{NNNN}.md`, which the
NEXT round's proposal evidence carries; the prompt is grounded in the real
mutation-id list so the model cannot hallucinate targets), and the
epoch analysis report regeneration. Every settled round and the rejected
tail call this one tail, so a new epilogue step can never land on one path
only. Every step is best-effort by contract.

Final heartbeat (`PROMOTE`/`REJECT` progress transition),
`round_closed`, and the `EvolveRoundOutcome` returns.

---

## 4. `evolve_field_round` — shared evaluation and settlement

`evolve_once` passes a `PreparedRound` to `evolve_field_round`
(`src/zicato/evolve/field.py`) for every selection strategy. Field width
changes the number of candidates and scheduled matchups. Every width retains
the same execution pipeline.

`evolve_field_round` retains the `PreparedRound` inside a `FieldRound` with
the derived parent identifier, field size, and evaluation settings
(`src/zicato/evolve/generation_phase.py`). Phases read shared inputs through
the prepared value. Its board slices and mutation inventory remain tuples;
APIs requiring lists receive local copies. The field opener resolves an absent
round log once on the prepared value. Four phase functions run in order:

| Phase | Module | Returns |
|---|---|---|
| `assemble_candidate_field` | `evolve/field_candidates.py` | `CandidateField`, or a terminal outcome when nothing applied |
| `execute_field_tournament` | `evolve/field_execution.py` | `FieldExecution`, or a terminal outcome when the outage circuit deferred |
| `resolve_field_verdict` | `evolve/gate.py` | `FieldVerdict` |
| `settle_field_round` | `evolve/settlement.py` | `EvolveRoundOutcome` |

`settle_field_round` builds one `OutcomeRecord` per applied challenger,
commits a replayable settlement, publishes the observational frontier and
live tournament views, then closes the round. Publication changes the
complete round record from pending to committed before refreshing the index.
Startup can finish interrupted publication or index refresh without
evaluating the tournament again.

The phase names follow the lifecycle steps the execution plan serves
(`zicato.query.execution_plan.ROUND_STEPS`: propose, apply, run, gate,
decide), so a round's code and a round's served tree name the same steps.

### 4.0 The structure shapes, in one table

06-tournament-and-selection.md owns the theory; evaluation needs only each
structure's scheduling shape
(`src/zicato/selection/` registry + strategies; params live in
`TournamentStructure.params` and fold into the contract hash):

| Structure | `field_size` | Shape | Key params |
|---|---|---|---|
| `gauntlet` | 1 | one champion-vs-challenger duel; promote-on-gate | `replicates` (unset: 2) |
| `single_elim` / `double_elim` (experimental) | bracket | challenger-vs-challenger nodes (winner = `lower_scalar_id()`), then champion-gate crowning | `field_size`, `replicates` |
| `swiss` (experimental) | N | `rounds_n` swiss pairings, then crowning | `field_size`, `rounds_n`, `replicates` |
| `racing` | N | escalating board-slice rungs cut the field (`board_subset` per rung); a rung CUTS, it does not crown; final full-train crowning duel | `field_size`, `eta`, `board_fraction`, `replicates` (unset: 1), optional `matchup_budget_seconds`, `promote_confidence_threshold`/`promote_confidence_replicates` |

A scoring document that omits the tournament block gets racing with
`field_size` 4, `eta` 2, `board_fraction` 0.4, `replicates` 2,
`promote_confidence_threshold` 0.8, and `promote_confidence_replicates` 32
(`_default_tournament_structure`, `src/zicato/core/tournament.py`).

All structures end the same way: ONE crowning champion-gate duel whose
`GateOutcome` decides promotion — which is why the holdout confirmation
and the evidence pre-gate bolt onto "the crowning matchup" uniformly.

### 4.1 The train/holdout rule, restated for structures

Internal matchups — the gauntlet duel, Swiss rounds, elimination nodes, and
racing rungs — score on the train slice when holdout confirmation is active.
The holdout is never consumed to pick the leader. Empty holdout means train is
the full board. Every structure and both evaluation modes use the same
train-selects, holdout-confirms procedure; fast mode only lets the holdout
confirmation reuse completed measurements.

### 4.2 Minting the field

Ids are minted monotonically from `next_generation_id`'s base
(`v{base_n + offset}`) so every challenger gets a distinct id even when
a proposer attempt fails before deriving a snapshot. For each of the
`field_size()` slots, `produce_candidate_batch` calls
`_propose_and_apply_challenger`:

1. beats `proposing:…` and publishes a live `"proposing"` field-status
   record BEFORE the LLM call (`on_status` → `_publish_proposing_slot`
   → the `ActiveTournament` envelope in phase `PROPOSING`) — the
   dashboard's proposing tracker shows each slot enter the field live;
2. builds the same `build_post_apply_validator` hook and calls the same
   `_propose_child`;
3. on `ProposerError`: returns a `CandidateAttempt` with no challenger,
   a `"rejected"` status carrying the FULL per-attempt failure list
   (`attempt_reasons`), and the error itself — a failed slot narrows the
   field, never crashes the round;
4. on success: `check_patch_manifest_and_forbidden`, then — critically —
   a PENDING lineage append BEFORE the experiment is written, then
   `write_experiment` (outcome=None), the containment manifest
   (`write_containment_manifest`, the parent-bound byte-range evidence),
   and the index dual-write:

```python
    # Lineage is the creation commit marker. Write the pending node before
    # experiment.json so recovery can identify every applied field sibling
    # without inferring membership from directory order. A crash after this
    # marker can discard the complete field; a crash before it leaves source
    # that no canonical record names, which startup prunes through the store
    # (:func:`zicato.runtime.resume._discard_unrecorded_source`).
    append_to_lineage(workspace_root, epoch_id, child_gen, parent_id=parent_id, pending=True)
    write_experiment(workspace_root, epoch_id, next_id, experiment)
```
*(src/zicato/evolve/propose_apply.py, `_propose_and_apply_challenger` — excerpt)*

The pending node reads as `promoted=null` ("racing" on the dashboard), never
`false`: a challenger that applied but has not been crowned or cut is not a
dead branch. The committed round record supplies its settled decision.

**Field diversity.** The accept/soft-reject verdict is PURE
(`_mint_challenger_field` → `_FieldMintDecision`), separated from its
persistence I/O so the branches are unit-testable:

- `reject_duplicate` — exact duplicate of an in-flight sibling (same
  modulating id-set + core idea) would collapse the field;
- `reject_overlap` — opt-in (`config.diversity_tolerance`, a RUNTIME
  knob): Jaccard overlap of mutation-id sets with an already-ACCEPTED
  sibling strictly above the tolerance;
- `accept` — the challenger joins; its `PriorExperiment` (decision
  `"in_flight"`) is appended to `siblings` so challenger k sees the
  hypotheses of challengers 0..k−1 and can diversify away from them.

A soft-rejected slot is not just dropped: `_persist_soft_reject`
(`src/zicato/evolve/candidate_batch.py`) writes a terminal REJECTED
outcome onto its already-persisted `experiment.json` (reason
`field_diversity_duplicate` or `field_diversity_overlap`, followed by the
detail), marks its lineage node rejected, and refreshes its index row, so
the canonical record, the lineage tree, and the live dashboard view agree —
never a stale "pending".

An all-failed field settles in `_settle_field_that_produced_nothing`. A
trail made only of transport failures defers (`deferred_infra`); a
one-candidate field whose slot exhausted its retries takes the rejected
tail (§3.10); any other all-failed field returns a clean rejection-shaped
`EvolveRoundOutcome` ("multi-challenger field: no challenger applied
cleanly", plus a per-slot failure breakdown) — after persisting the
field-status so the dashboard reads "N proposed · 0 applied", and after
`decision_recorded` + `round_closed`.

**The placebo slot.** On the opt-in cadence, a field wider than one gets
ONE extra slot appended LAST (`_append_placebo_arm`), and it flows through
the unchanged strategy and gate like any challenger (a one-challenger field
runs its placebo as a separate duel instead, §3.12). It is appended after the all-failed early return, so a
fully-failed field keeps its rejection-shaped outcome, and appended last
so sibling diversity and `first_challenger_id` are untouched.

### 4.3 The seams the driver runs on

`evaluate_tournament` (`src/zicato/selection/driver.py`) owns scheduling;
`resolve_tournament` is the decision-only wrapper over it. The loop is four
steps, verbatim from `resolve_tournament`'s docstring:

```python
    1. ``request_field(strategy.field_size())`` resolves the champion and
       the applied challenger field.
    2. ``strategy.seed(...)`` initialises bracket state.
    3. Loop: ``strategy.next_matchups()`` → run the batch concurrently →
       ``strategy.record_result(...)`` for each, until
       ``strategy.resolved()`` or the strategy schedules nothing.
    4. Return ``strategy.champion()``.
```
*(src/zicato/selection/driver.py, `resolve_tournament` — excerpt)*

Each batch fans out under one `asyncio.gather`; `on_progress` fires
right after a batch is scheduled (the strategy's pending set is
populated, so `live_rounds()` carries the in-flight matchups with
`winner: null`). Publishing before the matchups run is what makes the live
bracket exist during the round rather than only after it. When confirmation is
required, `confirm_promotion_with_evidence` holds a `"promoted"` decision while
`replicate_duel` measures the selected champion and challenger. The fit uses
only independent confirmation draws; promotion requires the adjusted
strength-difference bound to clear the gate.
`execute_field_tournament` supplies each seam as a module-level function
bound to the round with `functools.partial`, so nothing on the driver's
contract reads a shared local:

- **`request_field`** — hands the strategy the champion `Contestant` +
  the applied challengers (with snapshots + experiments).
- **`run_field_matchup`** — one duel via `run_matchup`
  (`src/zicato/tournament/runner.py`). The strategy⇄orchestrator contract
  is the `Matchup` dataclass (`src/zicato/selection/strategy.py`) — every
  field a strategy can use to shape a duel:

  | `Matchup` field | Meaning | Default |
  |---|---|---|
  | `matchup_id` | stable id linking the result back to the bracket node / Swiss pairing / racing rung | required |
  | `left`, `right` | the contestants; by convention `left` is the incumbent/higher seed — the gate treats `left` as nominal parent | required |
  | `board_subset` | a racing rung's entry-id slice; `None` = full (train) board | `None` |
  | `replicates` | paired board runs averaged before scoring; the unpinned default is 2 for gauntlet/bracket/Swiss (replication rather than bracket shape is the noise lever), racing pins 1 | `1` |
  | `stage_index` | the WITHIN-tournament stage — never confuse with the evolve `round_index` | `0` |
  | `bracket_slot` | elim bracket position (`"WB-R1-0"`); empty otherwise | `""` |
  | `matchup_budget_seconds` | wall-clock cap on the matchup's TOTAL board-unit execution — distinct from the per-entry budget; catches "each unit under budget, the sum grinds for hours" | `None` |

  For challenger-vs-challenger nodes (no incumbent), the winner is
  `MatchupResult.lower_scalar_id()` — `delta_scalar` is `right − left`,
  negative means `right` is better, and ties keep `left`, the higher seed,
  because a tie counts as no improvement.

  `run_field_matchup` runs under the round-shared semaphore:

```python
    # One semaphore for the whole round. A strategy may schedule several
    # matchups concurrently (the driver fans the batch out under one
    # ``asyncio.gather``). Without a shared gate each matchup would mint its
    # own ``Semaphore(parallelism)``, so N concurrent matchups could run
    # ``N × parallelism`` board units at once — overshooting the operator's
    # parallelism intent and the LLM endpoint's concurrency.
    unit_semaphore = asyncio.Semaphore(max(1, int(field_round.config.parallelism)))
```
*(src/zicato/evolve/field_execution.py, `execute_field_tournament` — excerpt)*

  Each matchup scores on the applicable train board (a racing rung's
  `board_subset` is intersected inside `run_matchup`), caches both sides'
  aggregates, and emits units plus the gate verdict onto the RoundLog. The
  cache-first evaluator may reuse any competitor's existing unit. Only the
  reigning champion's cached-vs-fresh tally determines the round-level
  `champion_eval_mode` (`_resolve_round_champion_mode`).
- **`publish_live_structure`** (`on_progress`) — every scheduled batch
  republishes the live envelope with settled rounds + the in-flight round
  (`winner: null, pending: true`) + standings-so-far, through the SAME
  `_serialise_rounds`/`_serialise_standings` the settle path uses. This is
  what lets the bracket exist DURING the run instead of "being seeded"
  until settle. Best-effort.
- **`make_evidence_replicate_duel` + `record_inconclusive_duel`** (only when
  `promote_confidence_threshold` is set) — the evidence pre-gate's extra
  crowning-pair duels with distinct local draws under `evidence_confirmation` (with `cache_scores=False` so a single-draw aggregate never overwrites the round-scored `gen_score.json`), and the dead-letter record + `evidence_replicated`
  trail for an unresolved crowning.

**Durable record opens BEFORE resolution.** `_open_tournament_envelopes`
publishes the live envelope, appends the `TOURNAMENT_START` progress
transition, and calls `_open_field_tournament`, which writes
`tournaments/field-{first challenger}.json` in `in_progress` state as soon
as the field is minted: the runtime `active_tournament` envelope is
EPHEMERAL (cleared on crash, overwritten next round); only the durable
record is queryable by the index and external consumers. The settle write
upserts the same `tournament_id` to `settled` — open + settle compose
idempotently so a resume that re-opens an existing record neither
duplicates nor corrupts it. A failure mid-resolution clears the live
envelope (`_clear_active_tournament`) so the dashboard never shows a
stuck tournament, then re-raises.

### 4.4 After resolution: holdout, integrity, overrides, invariants

**Holdout confirmation.** `_confirm_crowning_on_holdout` (pure decision
shape; the I/O is the injected `confirm_fn =
confirm_crowning_holdout`): a `promoted` crowning duel must ALSO confirm
on the holdout through the shared Ladder machinery and per-epoch
`ladder_state.json` budget. A released
non-confirmation flips the crowning promote to a holdout reject, and a
withheld or incomplete confirmation defers it — either way the champion
stands and `reason_override` carries the cause. The champion side
is resolved defensively (left by convention, but a right-seeded champion
still confirms correctly), and the crowning challenger's TRAIN scalar is
paired with its HOLDOUT scalar so the generalization gap is measured on
the same duel. `holdout_released` lands on the RoundLog.

**Integrity blocking** checks the crowned child's snapshot and the crowning
duel's champion-oriented delta
(`crowning_delta_scalar` — note the orientation normalization: the gate
treats LEFT as parent, so a right-seeded champion flips the sign).

**Operator overrides.** A round may contain one or several candidates, so
`claim_field_gate_overrides(workspace, field_candidate_ids)` may target a
non-winner, the leader, or SEVERAL candidates. The re-resolution is PURE
(`_apply_field_overrides`):

- `promoted_ids` contains every promoted candidate. Without overrides, it
  contains the selected winner or is empty.
- `promoted_id` identifies the primary generation used as the next parent.
  It is the selected leader if that candidate remains promoted; otherwise,
  it is the promoted candidate with the lowest scalar. It is `None` when
  no candidate is promoted.
- `override_provenance` records each candidate's operator override.
- `effective_decision` is the decision after holdout confirmation and
  operator overrides. The round record, live tournament view, and
  `decision_recorded` event report that decision.

**Write order + the crowning invariant.** Settlement validates the complete
decision before any canonical write, including the bracket-to-champion
agreement:

```python
    bracket_promoted = settlement.decision.decision == "promoted"
    if bracket_promoted != (settlement.primary_promoted_generation_id is not None):
        raise RuntimeError(
            "crowning invariant violated: settled bracket decision "
            f"{settlement.decision.decision!r} (promoted_generation_id="
            f"{settlement.decision.promoted_generation_id!r}) disagrees with the "
            "champion to be crowned "
            f"({settlement.primary_promoted_generation_id!r}); refusing to persist a "
            "bracket the champion pointer / lineage contradict"
        )
```
*(src/zicato/evolve/settlement.py, `_assert_crowning_agrees` — excerpt)*

The promoted id must name an applied challenger. The complete decision is
written to `rounds/{round}/field_settlement.json` in pending state. Recovery
validates candidate identities, the common parent, outcomes, and agreement
between the tournament decision and primary promoted generation.

1. Atomically change the record to `state="committed"`, publishing all
   candidate outcomes and the primary promoted generation together.
2. Refresh the affected derived-index rows and record `succeeded` or
   `repair_required`. An index failure preserves the committed decision.

Experiment and lineage readers combine proposals and ancestry with committed
outcomes. Champion selection reads the primary promotion; journal rendering
uses the accepted experiments. These views require no separate decision writes.

The retained record tracks promotion-hook delivery as `not_applicable`,
`pending`, `succeeded`, `failed`, or `delivery_unknown`. The last state is
persisted before invoking the external hook; startup never retries a call
whose delivery is unknown.

**The round summary comes from the crowning matchup.** The returned
`EvolveRoundOutcome`'s scalars are resolved from the crowning duel
(champion side against the leader that reached the gate) rather than from
`_first_aggregate_for`'s standings average or a child-defaults-to-parent
fallback. Either of those reports delta 0.0 on a rejection even when the
gate measured a real regression. On a rejection,
`proposed_generation_id` names the LEADING challenger the reason is about
rather than an arbitrary `applied[0]`.

---

## 5. Inside one board unit — the worker anatomy

Every matchup bottoms out in the same primitive: `_run_single`
(`src/zicato/tournament/worker_execution.py`) runs ONE entry under ONE
generation in an isolated subprocess. This is the subprocess worker
boundary — the robustness layer that contains a wedged or pathological
evaluation — and the documented monkeypatch anchor the test suite stubs
(`tests/_orchestrator_harness.py` swaps exactly
`worker_execution._run_single`). Its docstring is the sequence contract:

```python
    1. Make a per-run **ephemeral checkout** of the generation's code
       snapshot (materialised by the workspace's generation store into a
       system-temp directory — a ``copytree`` under the directory
       backend, a per-run ``git worktree`` under the git backend) and
       point the worker at THAT, never at the canonical source tree.
    2. Serialise the run's inputs (entry, adapter spec, call_llm dotted
       paths, scoring weights, sink/loss/result paths, and the ephemeral
       ``snapshot_root``) to a temp args file.
    3. Spawn ``python -m zicato._tournament_worker <args-file>`` via
       :func:`asyncio.create_subprocess_exec`. The worker stamps its OWN
       pid into ``active_runs/{run_id}.json`` so the supervisor can kill
       it individually.
    4. ``await asyncio.wait_for(proc.wait(), budget + GRACE)``. The
       worker's own cooperative budget normally fires first; the parent's
       wait_for is the second line of defence.
    5. On parent timeout: SIGTERM -> (grace) -> SIGKILL the worker, then
       synthesise an aborted :class:`LossProfile`.
```
*(src/zicato/tournament/worker_execution.py, `_run_single` docstring — excerpt)*

Steps 6–7 complete the contract: a worker that exited non-zero, OR a
missing/corrupt result file (e.g. the SUPERVISOR SIGKILLed a wedged
worker), is ALSO an aborted run — not a crash; the tournament continues
to the next entry either way. Cleanup releases the spawn permit, the
ephemeral checkout, and the protocol files only after the worker's
process group has exited; cancellation waits through that bounded
teardown, and a termination that cannot be confirmed retains ownership for
`retry_worker_cleanup`.

Unpack the load-bearing pieces:

**The ephemeral checkout.** The worker never touches the canonical
generation tree. `_checkout_run_snapshot`
(`src/zicato/tournament/worker_transport.py`) asks the
`GenerationStore` for an isolated working copy (an
`EphemeralCheckout`, prefix `EPHEMERAL_SNAPSHOT_PREFIX`); any runtime
write the inner agent makes near its own code lands in the throwaway
copy, so `derive_generation` never carries runtime droppings forward
into a child generation. Discard is best-effort
(`_discard_run_snapshot`).

**The wire.** Everything the worker needs crosses as JSON in the args
file — the entry (`_entry_to_dict`, with the board-level
`disable_drift`/`judge_only` stamped onto entry context by
`_stamp_disable_drift`/`_stamp_judge_only`), the adapter spec
(`adapter_worker_spec` — the same `config.json` block the factory reconstructs
from), the model roles as the captured execution-role documents
(`config.execution_roles`, or `execution_roles_for_runtime` in
`src/zicato/models_config.py`, which names a runtime callable by
`_callable_dotted_path` — the module-level-callable rule), the selected
operational settings (`_configuration_spec`), and the FULL scoring weights
(`_weights_spec` → `ScoringWeights.to_json`). Seam 1 scoring
(per-run drift reduction, including any `drift_reducer` plugin and
`drift_kind_aggregation` transform) runs INSIDE the worker, which is why
the weights must cross complete. A dropped field means the worker scores
under defaults while the orchestrator believes otherwise (the
`per_judge_weights` desync class; 03-contract-and-epochs.md
§"Serializer completeness").

An optional Goldfive object crosses in the same scoring document. Zicato does
not deserialize it into a parallel tree of Zicato dataclasses. The worker keeps
the immutable JSON mapping until a Goldfive-consuming adapter invokes the lazy
integration bridge. A generic adapter and a contract without the object never
import Goldfive through this path.

**Inside the worker** (`src/zicato/_tournament_worker.py`): rebuild the
adapter and weights from the spec, attach the per-run goldfive
`JSONLPersistenceSink` (plus the harmonograf live sink when the worker's
runtime context contains telemetry endpoints), `chdir` into the ephemeral checkout, drive
`RunnableHarness.run(entry, sinks, config)`, then reduce
`events.{purpose}.r{draw}.jsonl` → `LossProfile` → `loss.{purpose}.r{draw}.json` in the selected seed directory and write the result file. Runtime run identities include generation, entry, purpose, local draw, and seed; the index’s `runs` table uses the run id as its primary key.

**After the wait:** the runner stamps `match_id` onto the settled
`LossProfile` and rewrites the matching measurement loss file with the tag, so that a later full `zicato repair index` reconstructs the same provenance from that file. It then keys the ActiveTournament grid update on `(entry_id,
side)` and dual-writes the run into the SQLite index
(`_ingest_run_into_index`, best-effort). Each entry has TWO rows, one per
side; keying on `entry_id` alone lands parent transitions on the child
row.

**The cache above it.** `_run_single` sits under the per-unit cache keyed
by generation, entry, purpose, local draw, and base seed. A hit reuses a
complete matching measurement; a miss executes the unit. Infrastructure-aborted
profiles cannot satisfy cache reads. Forced remeasurement retains previous
attempts. Calibration, preflight, screening, and confirmation address this
cache with explicit purposes, and readers distinguish modified-source probes
from measurements of the recorded generation's own source.



> ⛔ **NEVER** bypass `_run_single` to evaluate a generation "quickly"
> in-process. Everything above — isolation, budgets, kill-ability,
> telemetry capture, cache coherence, index provenance — exists at this
> boundary. In-process evaluation is only legitimate inside tests that
> stub `worker_execution._run_single` and say so (the power oracle is one), and
> for the screen/preflight paths that already route
> through the same runner machinery.

---

## 6. The observability surface of one round

Every phase of a round announces itself on three planes. When you add a
step, wire all three: a step on none of them is invisible to the operator,
to the supervisor, and to later forensics.

**Plane 1 — heartbeat `phase` strings** (`_beat` /
`HeartbeatBeater.update`; read by the dashboard header and the
supervisor's staleness logic). The emitted vocabulary:

| Phase string | Emitted at |
|---|---|
| `evolve_n_rounds:start` | loop boot (epoch resolved, lock held) |
| `evolve_once:round_{N}` | round scheduled (loop side) |
| `evolve_once:calibrating_noise_floor:{done}/{K}` | the epoch-open A/A calibration, restamped per settled draw |
| `evolve_once:contract_preflight:{done}/{total}` | the epoch-open contract pre-flight, restamped per settled A/A draw and degraded probe (`total` is the ceiling — an early-settling verdict ends below it) |
| `proposing:round_{N}:{vX}` | before each candidate slot's proposer call |
| `screening:r{N}` | the candidate screen's panel runs |
| `applying:round_{N}:{vX}` | inside `build_post_apply_validator` per attempt |
| `tournament:round_{N}:{matchup_id}` | each scheduled matchup's start |
| `deferred_infra:round_{N}:{vX}` | the infra deferral tail |
| `infra_backoff:round_{N}:{delay}s` | the loop's backoff sleep |
| `done:round_{N}:{tournament_id}:{decision}` / `done:round_{N}:{vX}:rejected` | round settled / the rejected tail |
| `after_round_{N}:{decision}` | loop-side post-round stamp |
| `evolve_n_rounds:done` / `evolve_n_rounds:budget_exhausted` | terminal |

**Plane 2 — progress-log transitions** (`src/zicato/runtime/progress_log.py`;
the TRUE liveness signal): `LOOP_START`, `ROUND_START`, `PROPOSE`,
`TOURNAMENT_START`, `TOURNAMENT_SETTLE`, `PROMOTE`/`REJECT`, terminal
`SETTLED` (completed) / `STOPPED` (budget or breaker — still a CLEAN
end; a STALLED run is a frozen `seq` with no terminal event). `_beat`
couples planes 1 and 2: passing `progress=` (with the `progress_writer`
that owns the workspace) appends the transition and stamps its `seq` onto
the same heartbeat update.

**Plane 3 — durable traces**: the RoundLog (§8), the live
`ActiveTournament` envelope + the durable `tournaments/field-*.json`
record (§4.3), the committed round record
(`rounds/{N}/field_settlement.json`), the health report
(`epochs/{e}/health/round_{N}.json`), and the analyzer insights. The
journal is rendered from the experiment records rather than written.

The teardown path is part of this surface: `_mark_run_terminal` (on the
invocation's resource stack, §2.5) flips any lingering active-tournament envelope out of
`phase="running"` so a normally-ended run never reads as a live
tournament — a SIGKILL still cannot self-clean, which the frontend's
heartbeat-freshness gate covers.

---

## 7. What the round does on each failure class

Each row names where a failure is detected, what the round does about it,
what it leaves on disk, and the invariant that makes the outcome safe.

| Failure | Detection point | What the round does | Durable footprint | Invariant |
|---|---|---|---|---|
| Proposer's working copy does not read back as a valid patch set | the episode's `validate_patches` completion rule | the findings go back to the model, up to `max_proposer_retries` turns | `proposal_episode_settled{kind, code, message}` | the repair is inside the episode; a spent budget ends it blocked |
| Patch set fails post-apply validation on every retry | `build_post_apply_validator` → `ProposerError` | one-candidate field: `_persist_rejected_round` (reason `proposer_retries_exhausted: …`); wider field: the slot narrows the field | rejected `experiment.json` + `validation_failed` + `decision_recorded` (one candidate); rejected field-status (wider field) | invalid proposals remain visible with their rejection reason |
| Patch targets a forbidden / stale mutation id | `check_patch_manifest_and_forbidden` | `BadPatchSetError` (a `ValueError`) propagates — a hard programming or contract error rather than a retryable one; a bad patch set raises ONE exception class across the whole apply path | none beyond the raise | the hypothesis's `modulating` set is the ONLY thing patches may touch |
| One board run exceeds its wall-clock budget | worker cooperative budget → parent `wait_for` → supervisor deadline (three layers) | SIGTERM→grace→SIGKILL; synthesised aborted `LossProfile` (`BUDGET_ABORT_CAUSE`); scored worst-case for that entry | the aborted profile (tagged, never cache-persisted for infra causes) | the tournament continues; one entry cannot wedge a duel |
| Worker crashes / result file missing or corrupt | `_run_single` step 6 | ALSO an aborted run — not a crash; continue | aborted profile with an infra `abort_cause` | `is_infra_abort_cause` distinguishes infra from genuine budget exhaustion |
| Whole endpoint down (many infra aborts) | `_count_infra_aborted_runs` ≥ `infra_abort_round_threshold` (opt-in) | `_defer_round_infra_outage`: verdict discarded, nothing journaled, experiment left un-outcomed | `decision_recorded{deferred_infra}` + health `infra_outage` WARNING; NO gen_score caches | deferral ≠ rejection; resume reconciles the un-outcomed experiment |
| Orchestrator dies mid-tournament | next invocation's `prepare_resume` | resume-in-place when self-consistent + ≥1 unit done (reuse experiment, cache-HIT done units); discard on ANY ambiguity | the interrupted round's partial units stay valid cache | "never score against a tree we cannot rebuild"; cold start is byte-identical |
| Round would blow the invocation's total budget | `WallClockBudgetPolicy` via `asyncio.timeout` | round cancelled; synthetic `wall_clock_budget` rejection; loop stops | the synthetic outcome in the return list | cooperative-only guard; the subprocess worker boundary and the supervisor cover wedges |
| Holdout does not confirm a train win | `confirm_crowning_holdout` / `_confirm_crowning_on_holdout` | promote flipped to reject; champion stands; reason `holdout_not_confirmed` carried | `holdout_released{confirmed: false}` + the holdout block on the OutcomeRecord | train selects, holdout confirms; Ladder budget charged |
| Evidence CIs never separate | the pre-gate's replicate budget exhausts | terminally inconclusive; champion stands | dead-letter record + `evidence_replicated` trail + journaled `evidence` block | a promotion needs confirmation draws whose adjusted strength-difference interval lies above zero |
| Settled bracket contradicts the champion pointer | the crowning-invariant checks | loud `RuntimeError` BEFORE any canonical write | the raise itself (nothing corrupt persisted) | the complete decision is validated before it is committed |
| Live-envelope / RoundLog / index / report write fails | each `best_effort(...)` wrapper | logged at debug, round unaffected | possibly-missing observational artifact | best-effort is for observational writes ONLY |
| Operator forces a verdict | `claim_field_gate_overrides` at the verdict's safe point | verdict replaced, NEVER silently: `operator_override(_reason)` stamped | override provenance on record + RoundLog + field record | an override is always recorded and never a silent flip |

---

## 8. The canonical RoundLog event sequence

The convergence oracle pins the exact per-round event sequence for the
deterministic gauntlet contract (best_of_n pinned to 1 ⇒ no
`candidate_sampled`/`critique_selected`; 5-entry board below the split
floor ⇒ no holdout events; pre-gate off ⇒ no `evidence_replicated`; the
import-kind adapter reports no harness-load provenance ⇒ no
`harness_loaded`):

```python
        types = [e.type for e in events]
        assert types == (
            [
                "round_opened",
                "proposal_attempted",
                "proposal_episode_settled",
                "experiment_minted",
                "patches_applied",
            ]
            + ["unit_completed"] * (2 * BOARD_SIZE)
            + ["gate_evaluated", "decision_recorded", "round_closed"]
        ), f"round {round_index}: {types}"
```
*(tests/test_convergence_known_answer.py, `test_gauntlet_converges_to_known_floor`)*

For the 5-entry example: `round_opened` → `proposal_attempted` →
`proposal_episode_settled` → `experiment_minted` → `patches_applied` → 10 ×
`unit_completed` (one per (entry, side)) → `gate_evaluated` →
`decision_recorded` → `round_closed` — 18 events, with `seq` running `1..18`
gap-free and `fold_round_record(events).complete` true. The racing test
extends it: four proposals (one per field slot), rung-by-rung
`unit_completed` + `gate_evaluated`, and a `decision_recorded` whose
provenance carries `structure: "racing"`, `promoted_generation_id`,
`promoted_generation_ids`, and `overrides: {}`.

The fully-populated grammar (every optional feature on) per round is:

```
round_opened{contract_hash}
( proposal_attempted{errors}*                          # failed attempts, if any
  [ candidate_sampled{i,n,revise?,recombined?} × best_of_n ]   # slate sampling
  [ candidate_screened{index,vetoed,confirmed,…} × slate ]
  [ critique_selected{index,reason,slate,rationale} ]
  proposal_attempted{}  proposal_episode_settled{kind,…}
  experiment_minted  patches_applied ) × (field slots)  # per applied challenger
[ validation_failed{findings} ]                        # the rejected tail only
unit_completed{entry,replicate,side} × (units run)
[ harness_loaded{generation_id,…} ]                    # per generation, when the adapter reports it
gate_evaluated{rule_fired,decision} × (matchups)
[ holdout_released{confirmed} ]
[ evidence_replicated{ci_state} × refits ]
decision_recorded{decision,provenance}
[ frontier_updated{admitted,retired,…} ]               # when the Pareto frontier changed
round_closed
```

Use this grammar when adding events: a new event type goes into
`EVENT_TYPES` + the `RoundEvent` union + `fold_round_record`
(`src/zicato/epoch/round_log.py`), with a dataclass default for every
field so a log written without the field decodes identically — the
`CandidateSampled.revise` and `.recombined` fields are the worked examples
of an additive event field.

### 8.1 A worked trace: round 1 of the convergence example

The first round (`round_index` 0) of
`examples/zicato_examples/target_0_convergence` — the deterministic
convergence recipe, chapter 01 §5.4 — made concrete. Setup: epoch freshly
created from the pinned contract (gauntlet, `best_of_n: 1`,
`replicates: 1`, `promote_confidence_threshold: null` so no pre-gate;
5-entry board, below the split floor so train = full board); the seeded
policy carries defect tokens; the scripted proposal's first policy
(`GAUNTLET_POLICIES["v1"]` in the example's `mocks.py`) removes `omit-summary`.

1. **Baseline.** `_ensure_baseline_snapshot` seeds `v0` from the
   registered `agent/` tree through the git genstore —
   `.zicato/repo/.git` now exists with `v0` tagged.
   `generation_phase.current_generation` returns `v0`.
2. **Propose.** `next_generation_id` mints `v1`. The Foe stand-in's
   episode writes the `v1` policy into its working copy, and the
   projection reads it back as experiment `exp_{epoch}_v1`: hypothesis
   `modulating=("style_rules",)`, one `Patch` re-emitting the policy
   minus one token. The validate hook derives `v1`'s snapshot from
   `v0` + patch (git commit, tag `v1`), `validate_post_apply` passes.
   RoundLog so far: `round_opened`, `proposal_attempted{}`,
   `proposal_episode_settled{completed}`, `experiment_minted{exp_…_v1}`,
   `patches_applied{v1}`.
3. **Tournament.** The gauntlet schedules one matchup; `run_field_matchup`
   calls `run_matchup`, which runs 5 board units × 2 sides =
   10 subprocess workers (bounded by `parallelism`). Each worker
   checks out an ephemeral copy of its side's tree, runs the
   deterministic harness, and reduces to its measurement loss file: every v0 run
   carries 3 info-drift frames (drift_loss 3.0), every v1 run 2.
   Champion aggregate: `drift_loss_mean=3.0, mean_score=0.4 (2/5),
   scalar=3.6`. Challenger: `2.0 + 0.6 = 2.4`. Ten `unit_completed`
   events land (entry × side).
4. **Gate.** `evaluate_gate`: `delta_scalar = −1.2 ≤ −promote_margin`;
   no champion-passed entry flipped (v1 strictly adds a pass); no
   guarded namespace regressed → `promoted`. `gate_evaluated` lands.
5. **Persist.** `gen_score.json` is cached for v0 and v1. A field-settlement
   intent records the resolved `OutcomeRecord` with decision `promoted`,
   `scalar_score_delta=-1.2`, `champion_eval_mode="full"`, and no holdout
   block. Committing the round publishes its outcome and primary promotion.
   Lineage and journal readers derive their views from the committed record,
   and the index is refreshed afterward.
   `decision_recorded` and
   `round_closed` complete the log — 18 events, `seq` 1..18.
6. **Epilogue.** `health/round_0.json` written (no CRITICAL findings —
   the planted defects differentiate, so `degenerate_scoring` stays
   silent); `analysis.html` regenerates; the loop's reject streak
   resets; round 2 begins against champion `v1`.

Round 2 is the negative control: the scripted proposer ADDS a token,
the gate measures `delta_scalar = +1.2`, rejects with a
"challenger regressed" reason, `v2` reads as a dead branch — and
`current_generation` still returns `v1`. Every number above is
asserted, to the float, in `test_gauntlet_converges_to_known_floor`.

---

## 9. The data-type flow

Who constructs each type, who consumes it, and where it persists. All
types frozen (`frozen=True, slots=True`); state transitions go through
`dataclasses.replace`.

| Type (owner file) | Fields that matter | Constructed by | Consumed by | Persisted at |
|---|---|---|---|---|
| `Experiment` (`core/experiment.py`) | experiment id, ancestry coordinates, proposal time, hypothesis, patches, outcome, birth round | proposer; the accepted reader supplies committed outcomes | validation, tournament execution, journal, index, dashboard | Proposal and patch files retain authored inputs. Tournament outcomes come from the committed round record; rejection before tournament execution updates the proposal file. |
| `HypothesisSpec` (`core/experiment.py`) | `core_idea`, `modulating` (the ONLY ids the patches may touch), `why`, expected drift/metric movements, `expected_pass_rate_delta`, `risks` | the proposer LLM, schema-validated with bounded retries | manifest check, diversity signatures, experiment memory, journal one-liners, hypothesis ledger | inside `experiment.json` |
| `Patch` (`core/mutation.py`) | `mutation_id`, op kind, payload | the proposer | applier (`derive_generation` through the genstore), validator, diff-complexity | `patches/{id}.json` |
| `Generation` (`core/epoch.py`) | `id`, `parent_id`, `snapshot_root`, `promoted`, `round_index` | orchestrator; the parent comes from committed promotions or baseline `v0` | runner, lineage, generation store | Ancestry in `lineage.json`; promotion status from committed outcomes; candidate source in the configured Git or directory generation store. |
| `LossProfile` (`core/loss.py`) | `drift_counts`, `pass_fail`, continuous `score`, `metric_counts` (namespaced), `runtime_ms`, `abort_cause`, `tokens_spent` | the reducer (`telemetry/reducer.py`) inside the worker path, per run | scoring aggregation, gate, detectors, screen, health, failure profile | `runs/{entry}/seed-{seed}/loss.{purpose}.r{draw}.json` (the per-unit cache reads it) + index `runs` table |
| `GateOutcome` (`tournament/gate.py`) | `decision`, `reason` (names the rule that fired), `delta_scalar`, `delta_pass_rate` | `evaluate_gate` at the end of every duel | strategies (read, never re-decide), evidence gate, RoundLog `gate_evaluated`, OutcomeRecord deltas | inside `TournamentResult` / `MatchupResult`; not standalone |
| `TournamentResult` (`tournament/runner.py`) | both aggregates, `outcome`, `per_entry_losses`, `champion_eval_mode`, `unit_provenance`, `holdout`, `holdout_child_scalar` | `run_matchup`; standalone debug APIs also return it | canonical matchup closure, infra counter, aggregate caching | aggregates cached as `gen_score.json`; the rest is projected into `MatchupResult` and `OutcomeRecord` |
| `MatchupResult` (`selection/strategy.py`) | matchup id, left/right ids + aggs, `outcome`, `stage_index`, `bracket_slot` | `run_field_matchup` from a `TournamentResult` | strategies (`record_result`), `SelectionDecision.matchups`, standings, match records | inside the durable field record (`_serialise_rounds`) |
| `SelectionDecision` (`selection/strategy.py`) | `promoted_generation_id`, `decision`, `reason`, `matchups`, `crowning_matchup_id`, `standings` | the strategy (`champion()`), re-written by holdout/override re-resolution into `effective_decision` | field tail (outcomes, lineage, envelopes), round summary | the settled field record + `ActiveTournament` |
| `TournamentEvaluation` (`selection/driver.py`) | strategy decision plus optional `EvidenceResolution` | `evaluate_tournament` | holdout confirmation and settlement construction | not persisted directly; its parts enter the tournament and outcome records |
| `CandidateBatch` (`evolve/candidate_batch.py`) | incumbent, requested width, applied challengers, rejected slots, field status, resume provenance | `produce_candidate_batch` | strategy seeding and round evaluation | experiments and soft rejections persist independently; the typed batch does not |
| `RoundSettlement` (`evolve/settlement.py`) | effective decision, primary and additional promotions, candidate outcomes, champion scalar, evidence, tournament metadata | `_build_field_settlement` after confirmation and overrides | construction of the replayable settlement receipt | its constituent facts remain in `rounds/{round}/field_settlement.json` after commit; the typed value does not persist |
| `OutcomeRecord` (`core/experiment.py`) | decision + reason, deltas, `structure`/`final_rank`/`match_record`, `champion_eval_mode`, `holdout` block, `train_loss`/`holdout_loss`/`generalization_gap`, `operator_override(+reason)`, `evidence` | settlement construction plus rejected/soft-reject tails | journal, index, dashboard decision surface, gap detector | completed tournaments persist in committed round records; rejection before tournament execution uses `_finalize_generation` |
| `PriorExperiment` (`core/experiment.py`) | `core_idea`, `modulating`, `decision` (incl. `"in_flight"`), banded delta, `same_contract`, `prediction_accuracy` | `_load_prior_experiments` (index) + the field loop (siblings) | the proposer's memory section | never persisted — a render-time projection |
| `EvolveRoundOutcome` (`evolve/round_api.py`) | parent/child ids, decision (incl. `deferred_infra`), reason, scalars + delta, health summary/critical | every `evolve_once` return path | `evolve_n_rounds` stop policies, the CLI summary | not persisted (the experiment and round records carry the durable truth) |
| `ResumePlan` (`runtime/resume.py`) | `classification`, `resumes_in_place`, `resume_generation_id`, `resume_experiment` | `prepare_resume` at loop start / after a deferral | the parent choice (§3.5), the resume short-circuit (§3.9), cache-read decisions | derived from the workspace; not persisted |
| `Standing` (`selection/strategy.py`) | `generation_id`, `rank`, `scalar`, wins/losses, `status`, `role` | the strategy's standings view | dashboard leaderboard, `final_rank` on OutcomeRecords | inside the settled field record |
| `_AppliedChallenger` (`evolve/propose_apply.py`, private) | generation id + snapshot + experiment + `Generation` | `_propose_and_apply_challenger` | candidate batch, strategy seeding, settlement | not persisted (its parts are) |
| `_CrowningHoldout` (`evolve/gate.py`, private) | post-holdout promoted id, reason override, holdout block, train/holdout scalar pair, champion-oriented `crowning_delta_scalar` | `_confirm_crowning_on_holdout` (pure) | override re-resolution, integrity block, OutcomeRecord stamping | not persisted (its parts are) |
| `_FieldMintDecision` (`evolve/propose_apply.py`, private) | `action` ∈ accept / reject_duplicate / reject_overlap, overlap + peer index | `_mint_challenger_field` (pure) | the field loop's soft-reject branches | not persisted (soft-reject reasons land on experiment.json) |
| `GateOverride` (`runtime/control_consumer.py`) | forced `decision`, operator `reason` | the operator via control files; claimed at safe points | override application + provenance stamping | archived to `control_log/`; provenance on records |

Proposal files, ancestry, committed round records, and measurements provide
the durable inputs for readers. The index and rendered journal derive their
content from those records. Runtime envelopes describe live progress and are
cleared on restart. A completed tournament therefore needs a retained round
record containing its decisions and structure so its visualizations remain
available after the process exits.

---

## 10. The extracted-seam inventory

The rule for placing a new round step:

> ✅ **ALWAYS** put a new round step into the shared preparation,
> candidate-production, evaluation, or settlement seam. ⛔ **NEVER** add a
> structure-specific execution tail. Tournament structures vary through
> `SelectionStrategy`; operational behavior stays in the shared pipeline.

The seams, and what each owns:

| Seam | Home | Owns | Shared by |
|---|---|---|---|
| `validated_invocation`, `InvocationContext` | `evolve/invocation.py` | workspace ownership, publication recovery, configuration, the pre-spend gate, epoch binding, and ordered teardown | both public entry points |
| `evolve_n_rounds` + stop policies | `evolve/loop.py` | the loop, circuit breakers, budget, backoff, control safe points, progress log lifecycle | (the loop itself) |
| `ensure_epoch_for_contract`, `_create_epoch_from_contract`, `_promoted_head_snapshot`, component-hash bookkeeping | `evolve/epoching.py` | the roll-at-evolve-time decision (03 covers it) | loop start, rubric replacement |
| `PreparedRound` construction | `evolve/round_entry.py`, type in `evolve/generation_phase.py` | frozen inputs shared by every field slot and matchup | every strategy |
| `produce_candidate_batch` | `evolve/candidate_batch.py` | field width, proposal/apply admission, diversity, rejection, resume provenance | every strategy |
| `build_post_apply_validator` | `evolve/round.py` | propose-time apply+validate hook (beat → derive all-or-nothing → validate) | every candidate slot |
| `check_patch_manifest_and_forbidden` | `evolve/round.py` | manifest + forbidden-ids cross-check | every candidate slot |
| `_propose_child` | `evolve/propose_apply.py` | the one `ProposerContext` build + propose + RoundLog proposal events + round-index stamp | candidate production |
| `evaluate_tournament`, `confirm_promotion_with_evidence` | `selection/driver.py` | strategy progression and Bradley–Terry confirmation | every strategy |
| `run_matchup` | `tournament/runner.py` | board selection, paired replication, cache policy, aggregation, promotion gate | every scheduled matchup |
| `RoundSettlement`, `CandidateSettlement` | `evolve/settlement.py` | typed boundary between resolved evaluation and settlement-intent construction | every settled round |
| Round publication and recovery | `evolve/settlement_recovery.py` | validate and commit the complete round record, refresh the derived index, and retain external hook delivery status | every settled round and startup recovery |
| `_finalize_generation` | `evolve/persist.py` | direct outcome and index write when no tournament settlement receipt exists | rejected tails before tournament execution |
| `_round_epilogue` | `evolve/persist.py` | health + analyzer + report regeneration | every completed round plus rejected tail |
| `_persist_rejected_round` | `evolve/persist.py` | one-candidate proposer-exhaustion tail | a one-candidate field whose slot failed |
| `_defer_round_infra_outage` | `evolve/decision_support.py` | deferral tail with no outcome or journal write | every strategy |
| `_mint_challenger_field`, `_apply_field_overrides`, `_confirm_crowning_on_holdout` | `evolve/propose_apply.py`, `evolve/gate.py` | diversity, overrides, and holdout re-resolution | every applicable round |
| `_integrity_block_reason` | `evolve/gate.py` | opt-in diff-containment and gate-contradiction block | every strategy |
| `_RoundLogEmitter`, `_emit_tournament_units`, `_emit_gate_evaluated` | `evolve/round_reporting.py` | best-effort RoundLog emission | every strategy |
| lifecycle services (`_beat`, `_record_progress`, `_now_iso`, `_resolve_or_launch_harmonograf`, `_build_meta_loop_emitter_safe`, the no-op shutdown handle) | `evolve/lifecycle_services.py` | heartbeat/harmonograf/emitter plumbing | loop and round pipeline |
| placebo minting + cadence | `evolve/placebo.py` + `_mint_placebo_challenger`/`_maybe_run_placebo_arm_gauntlet` | control arms | strategy-specific cadence through the shared tail |
| dashboard projection (`_publish_active_tournament`, `_settle_active_tournament`, `_clear_active_tournament`, `_open_field_tournament`, `_serialise_rounds/standings`, `_mark_run_terminal`) | `evolve/dashboard_projection.py` | live-envelope and durable tournament-record writes | round pipeline + loop teardown |

Two mechanical rules keep the seams honest:

- **Import the owner.** Tests and internal callers import the phase module that
  owns a seam. The dispatcher exposes the public round entry points; it is not
  a registry for private helpers.
- **Pure decision / I/O split.** Where a decision has more than one
  branch worth testing, the decision is a pure function
  (`_mint_challenger_field`, `_apply_field_overrides`,
  `_confirm_crowning_on_holdout` with `confirm_fn` injected) and the
  call site owns the writes. New multi-branch logic follows this shape
  or it will only ever be covered by e2e tests.

---

## 11. Where the Rust supervisor sits

Summary only — 08-supervisor.md is the deep dive.

The supervisor (`crates/supervisor/`, binary `zicato-supervisor`,
default `127.0.0.1:7920` — it walks a port range disjoint from the
dashboard's 7892, so the two never contend) is a SEPARATE OS PROCESS spawned by
the `zicato evolve` command (`_maybe_spawn_supervisor`). Its entire coupling to Python is read-only file I/O plus
signals:

- **What it reads:** the `.zicato/runtime/` state files — `heartbeat.json`
  (staleness), the active-run records (each carrying `started_at`,
  `last_progress`, the worker `pid`, and a `deadline = started_at +
  wall_clock_budget_seconds`), the kill-request markers — plus, for the
  alarm-only notary scans, generation snapshots and settled outcomes.
- **What it can kill:** worker pids. Two independent triggers per its
  `watchdog.rs` header — deadline ("when `now` passes that deadline the
  watchdog SIGTERM→SIGKILLs the run's worker pid. Because the supervisor
  is its own OS process this holds even when the orchestrator's event
  loop is wedged") and run-staleness (`last_progress` not advancing).
  The kill decisions are pure functions of `(state, now, thresholds)`.
- **What it never does:** decide tournaments, write canonical records, or
  block a promotion. Its integrity surfaces (`diff_containment.rs`,
  `promotion_gate.rs`) are alarm-only findings on `/statusz`; the opt-in
  IN-BAND blocking twins live in Python
  (`_integrity_block_reason`, §3.11) so that the supervisor stays a pure
  observer.
- **The handshake on kills:** when a supervisor is reachable, the
  tournament parent delegates termination of an over-budget worker by
  writing a kill-request marker and waits up to
  `RuntimeConfig.supervisor_kill_wait_s` (default 20.0 s) for the
  supervisor to confirm the group is gone. If delegation does not confirm,
  a bounded fallback signals the worker's captured process identity itself
  (08-supervisor.md §8.10). Without a supervisor, that wait is the
  abort-latency floor (tests shrink it).

> ⚠️ **TRAP** — if you add a new long-running worker kind, it must write
> an active-run record with a pid and progress timestamps, or the
> supervisor cannot see it and a wedge in it is unkillable-by-watchdog.
> The record shape is `src/zicato/runtime/state.py`.

---

## 12. The concurrency model of one round

Know what runs in parallel: every unit of parallelism here consumes
concurrency at the model endpoint.

- **The orchestrator is one asyncio event loop, single-threaded.** All
  round bookkeeping (RoundLog appends, lineage writes, ledger mutations)
  happens on it with no awaits between read and write —
  `RoundTokenLedger` says so explicitly ("Single-threaded by design:
  mutations happen on the orchestrator's event loop with no awaits
  between read and write", `src/zicato/core/runtime.py`). If you
  introduce an `await` into an atomic read-modify-write of
  shared round state, you have introduced a race.
- **Board units fan out under a semaphore.** `RuntimeConfig.parallelism`
  (default 4) bounds in-flight board units. A unit can run both competitors
  concurrently, so the subprocess ceiling is `2 × parallelism`. Fast mode
  resolves each side from the cache first and lowers the active count whenever
  either unit is already present.
- **A field round shares ONE semaphore.** The driver gathers a whole
  matchup batch concurrently; without the round-level
  `unit_semaphore`, N concurrent matchups would each mint their
  own `Semaphore(parallelism)` and run `N × parallelism` units at once
  (§4.3). Any new evaluation channel inside a round must accept and use
  the caller's semaphore rather than mint its own.
- **Workers are OS processes rather than threads.** The GIL (Global Interpreter
  Lock) discussion in `docs/design/ROBUSTNESS.md` is why: a CPU-wedged
  or C-extension-blocked evaluation cannot be pre-empted in-process, so
  isolation must be at the OS-process boundary to be killable.
- **The per-round token ledger clips launches rather than work already in
  flight.** Schedulers consult `check_and_clip()` at every would-launch
  point; work already in flight completes. Un-launched units record the same
  budget-exceeded losses a matchup-deadline trip synthesizes.

---

## 13. The monkeypatch surface (for test authors)

Tests replace expensive operations on their owning modules. When an operation
moves, update its test imports and patches. Production modules do not retain
private re-exports or reverse imports solely to preserve test patch locations:

| Anchor | What stubbing it gives you | Used by |
|---|---|---|
| `zicato.evolve.round_entry._evolve_once` / `zicato.evolve.epoching.ensure_epoch_for_contract` / `zicato.runtime.control_consumer.block_while_paused` / `zicato.evolve.lifecycle_services._resolve_or_launch_harmonograf` | loop-level tests with fabricated round outcomes — the loop body imports each from its owner at call time | the evolve-loop and invocation tests |
| `zicato.tournament.worker_execution._run_single` | in-process evaluation under the REAL scheduling/replicate/cache/gate machinery — "the test suite's documented monkeypatch anchor" | the power oracle, tournament tests, `tests/_orchestrator_harness.py` |
| `zicato.evolve.loop._sleep_for_backoff` | no real sleeps in backoff tests — "a seam so tests can stub it" | infra-circuit tests |
| `zicato.evolve.round_entry.time` | the clock seam `round_entry` keeps importable (`import time  # noqa: F401 — kept as the ``orch.time`` clock seam`) | budget tests |
| the conftest autouse fixtures | mutation-syntax-table isolation, the harmonograf launch stub, and the session-scoped dashboard reaper — the only stubs the convergence oracle inherits | everything |

> ✅ **ALWAYS** prefer the deterministic example harnesses
> (`zicato_examples.target_0_convergence.harness`, its `NoisyPolicyAdapter`,
> the scripted mocks) over new hand-rolled stubs. They run through the
> REAL worker boundary, and their noise model is seeded from stable
> coordinates, so a "rate" in a test is a deterministic function of the
> chosen seed — calibrated documentation rather than a flaky statistic.
> `tests/test_decision_procedure_power.py`'s docstring states this as
> policy.

---

## 14. What to internalize before you edit

1. **One pipeline, shared seams.** Find the seam before you write a
   line; §10's table is the map. If your step must run for every strategy
   and no seam fits, extract one into its owning phase module.
2. **Every safe point is explicit.** Operator control claims happen at
   named points (between rounds; step 0; the verdict's post-holdout
   override claim). Do not add a control effect anywhere else — a
   mid-tournament flag claim races the writes.
3. **Commit the decision before updating derived views.** A completed
   tournament commits one round record containing all outcomes, tournament
   details, and the primary promoted generation. Index refresh follows that
   publication. Proposal rejection before tournament execution uses
   `_finalize_generation` to record the outcome directly in the proposal file.
4. **Best-effort is a two-sided contract** (chapter 01 §6). Round-fatal
   steps raise; observational steps are wrapped; each new step declares
   which it is.
5. **The oracle pins all of this.** After any change in this chapter's
   territory, `uv run pytest tests/test_convergence_known_answer.py -q`
   — the decision script, the exact scalars, the artifacts, the round-log
   grammar, the index rows, and champion selection are all asserted
   there. Green is necessary but not sufficient on its own; a red result
   is always caused by your change.
