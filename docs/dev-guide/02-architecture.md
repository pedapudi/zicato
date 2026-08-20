# 02 — Architecture: one round, end to end, twice

> **Covers:** the process topology · `evolve_n_rounds` (the loop, its circuit breakers, resume, control protocol) · `evolve_once` step by step on the **gauntlet** path · the **multi-challenger field** path through `resolve_tournament` · the canonical RoundLog event sequence · the data-type flow table (who constructs, who consumes, where persisted) · the extracted-seam inventory and the seam rule · where the Rust supervisor sits.
> **Prerequisites:** chapter 01 (vocabulary, Golden Rules).
> **Invariants introduced:** [round-steps-live-in-seams] [epoch-cumulative-round-numbering] [train-selects-holdout-confirms] [outcomes-then-invariant-then-lineage] [crowning-invariant] [single-round-semaphore] [deferral-is-not-rejection] [late-binding-through-orchestrator] [progress-seq-advances-on-transitions-only] [best-effort-vs-load-bearing]

This chapter narrates one evolve round exactly as the code at this tree
runs it — post-decomposition, meaning after the orchestrator's god-function
era was broken into named seams under `src/zicato/evolve/`. Read it with
`src/zicato/evolve/gauntlet.py`, `src/zicato/evolve/field.py`, and
`src/zicato/evolve/loop.py` open. Every
step names the symbol that owns it; if you cannot find a step's symbol,
the code has moved and this chapter needs an erratum.

The decomposition contract is specified in
`docs/design/ROUND-PIPELINE.md`. The prepare phase creates an immutable
`zicato.evolve.generation_phase.RoundSession`; that module also owns champion,
snapshot, next-id, and mutable-tree coordinates. Import those operations from
their owner directly—there are intentionally no orchestrator forwarding seams.
The gauntlet and field drivers each retain one ordered asynchronous entry point;
supporting concerns live in sub-1,000-line owners. Do not split a driver solely
to meet a file-size aesthetic: extract only a phase with a typed result that
actually shortens the driver's live-local set.

---

## 1. The process topology

One `zicato evolve` invocation is FIVE kinds of process:

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
   │ worker  (ONE per run; L3       │   │ runtime/     │  │ server (in-proc, │
   │ isolation; killable)           │   │ heartbeat,   │  │ free localhost   │
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
`zicato.orchestrator`. Signature-stable; the CLI's `zicato evolve` is a thin
shell over it. Loop collaborators are imported from their owning modules;
tests patch those owners directly.

### 2.1 Startup, in order

1. **Stop-reason plumbing.** `stop_reason_out` (optional caller list)
   receives exactly one symbolic terminal string: `"completed"`,
   `"consecutive_rejections"`, `"degenerate_health"`,
   `"wall_clock_budget_between_rounds"`, or
   `"wall_clock_budget_mid_round"`.
2. **Mandatory workspace gate.** The loop calls
   `zicato.check.require_workspace_valid(...)` before auto-epoching or any
   model call. It checks the live contract when no explicit epoch is pinned,
   reconstructs the adapter through the same worker-spec seam as tournament
   workers — under the same environment a worker would be given — and
   enumerates the adapter-scoped snapshot under the contract's mutation
   syntax. `evolve_once` gates itself the same way, because it is exported
   and spends a full round on its own; the loop passes it
   `workspace_checked=True` so a multi-round invocation pays once. Library
   callers and the CLI therefore share the same spend boundaries;
   `--dry-run` runs the same validators before exiting.

   The gate makes no model call, which is what keeps it mandatory: a check
   needing the network would refuse every offline workspace, every fixture,
   and the parity capture. So the half of role checking that needs a round
   trip — is the credential *accepted*, does the model id *exist*, does the
   callable return a `str` — lives in `check/reachability.py` and runs on
   `evolve --dry-run` alone, after the offline validators have passed. It
   sends one short fixed request per configured `models.<role>`, building
   each role's callable through `models_config.lazy_text_call_llm`, the same
   seam `_tournament_worker._resolve_role_call_llm` uses, so whatever
   authentication the spec implies (a named `api_key_env`, or the ambient
   credentials a keyless endpoint spec relies on) is exercised rather than
   assumed. Each role is bounded by `ROLE_TIMEOUT_S` and reported on its own
   line — roles fail separately and have different remedies — and any role
   that does not answer makes the dry run exit nonzero. A workspace
   configuring no role is told nothing was probed, which is not the same
   answer as reachable.

   Findings come in two severities. A finding that proves the round cannot
   produce a valid measurement raises `WorkspaceCheckError`. A finding that
   proves only that something declared contributes nothing — a stale tree
   path, a span marker binding to no literal — is advisory: reported and
   logged, never a refusal, because those workspaces run correctly today.
   The severity of a code is fixed in `check.validators.ADVISORY_CODES`.
3. **Contract-hash auto-epoching, ONCE.** When `epoch_id is None` and
   `auto_epoch` is true, `_orch.ensure_epoch_for_contract(...)` resolves
   (and, on drift, rolls) the epoch; the resolved id is pinned for every
   round of this invocation so the loop never re-rolls mid-flight. An
   explicit `epoch_id` skips auto-rolling entirely — an explicit target
   always wins. (Mechanics: 03-contract-and-epochs.md §"epoch lifecycle".)
4. **Workspace lock.** `acquire_workspace_lock(workspace_root,
   instance_id)` — two concurrent orchestrators must not share a
   workspace. Released in the `finally`.
5. **Conservative crash-resume reconciliation, ONCE.**
   `prepare_resume(workspace_root, epoch_id)`
   (`src/zicato/runtime/resume.py`) runs right after the lock and before
   any new work: it clears stale runtime state from a prior dead evolve
   and, if the prior run died mid-tournament with completed board units
   on disk, returns a `ResumePlan` that resumes that generation in place.
   On ANY ambiguity it discards the partial generation. A clean workspace
   yields the no-op plan; the plan is consumed by the FIRST round only
   (`resume_plan = None` after round one).
6. **Progress log cleared.** `progress_log.clear_log(...)` so this
   invocation's `seq` starts from 1 — "a stale tail must never read as
   live progress". Then `HeartbeatBeater(workspace_root, instance_id,
   interval_s=2.0)` starts.
7. **Harmonograf + meta-loop emitter.**
   `_orch._resolve_or_launch_harmonograf(...)` returns the console URL
   plus a shutdown handle (auto-launched in-process unless the workspace
   configures an external URL); `_build_meta_loop_emitter_safe(...)`
   builds the goldfive emitter for zicato's OWN LLM calls (proposer,
   judges, analyzer) — degraded installs get a no-op emitter. Both are
   torn down in the `finally` block, emitter first (a sink flushing to
   the console wants the server still up).
8. **First genuine transition.** `LOOP_START` appended to the progress
   log; its `seq` stamped onto the heartbeat.

> ⚠️ **TRAP** — the progress log's monotonic `seq` advances ONLY on
> genuine transitions (`LOOP_START`, `ROUND_START`, `PROPOSE`,
> `TOURNAMENT_START`, `TOURNAMENT_SETTLE`, `PROMOTE`/`REJECT`, terminal
> `SETTLED`/`STOPPED`), never on the heartbeat timer. That is the whole
> point: a reader distinguishes "slow but alive between transitions" from
> "stalled" by whether `seq` moves. If you add a loop phase, append a
> transition for it via `_append_progress_seq`; if you make the heartbeat
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
| `WallClockBudgetPolicy` | total elapsed ≥ `max_wall_clock_seconds` | off (`None` = unbounded) | enforced BOTH between rounds (clean stop) and within a round (`asyncio.wait_for` with the *remaining* budget; the cancelled round becomes a synthetic `"wall_clock_budget"` rejection via `_budget_aborted_outcome`) |

The within-round guard is a Layer-1 `asyncio.wait_for` — it pre-empts
only *cooperative* async work. A round wedged in a blocking call is NOT
killed here; that is the subprocess layer's and the supervisor's job
(`docs/design/ROBUSTNESS.md`; 08-supervisor.md).

**The fourth exit that is not a stop:** a round returning
`DEFERRED_INFRA_DECISION` (`"deferred_infra"`) — the endpoint-outage
circuit (see §3.10) — deliberately bypasses BOTH stop policies:

> a deferral is evidence about the endpoint, not about the experiment
> stream, so it must neither count toward consecutive rejections nor
> reset/advance the degenerate-health streak.
> *(src/zicato/evolve/loop.py, the deferral branch comment)*

Instead the loop backs off exponentially (`infra_backoff_base_s`
doubling to `infra_backoff_cap_s`, knobs read once per invocation via
`_infra_backoff_knobs`), re-runs `prepare_resume` so the deferred
generation resumes in place if any unit completed, and continues.

> ⛔ **NEVER** map a new "the round could not be judged" condition onto
> `"rejected"`. Rejection feeds the consecutive-rejection breaker and
> burns the experiment. Follow the `deferred_infra` pattern: a distinct
> symbolic decision, nothing journaled, the experiment left un-outcomed
> for resume.

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
    (the historical behaviour for a brand-new epoch, where the first round is 0).

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
a new epoch). Note the closure detail: the per-round `_run_round` inner
function binds `_epoch_id: str | None = epoch_id` as a DEFAULT ARGUMENT
so a mid-iteration reassignment is captured by value, not late-bound — a
classic Python trap the code comments on explicitly.

> ⚠️ TRAP — count MINTED generations only. The seed is carried, not minted,
> and it still persists `round_index: 0` (`write_seed_experiment` builds it with
> the `Experiment.round_index` default). Counting it made a seeded-but-unrun
> epoch — a pre-flight refusal, a crash before the field landed, a budget stop —
> start its first real field at 1, and the round timeline then rendered the
> seed's own bucket as a phantom round 0 in which the carried champion defends
> an empty field. Writer and reader both identify the seed the same way: it is
> the PARENTLESS generation.

### 2.4 The between-rounds operator safe point

Before scheduling each round, in this order
(`src/zicato/evolve/loop.py`, the control-protocol block):

1. `block_while_paused(workspace_root)` — a `pause_epoch` control file
   blocks scheduling until the operator clears it.
2. `claim_skip_round(...)` — a STALE skip flag between rounds is drained
   as a no-op (there is no in-flight round to abort); a LIVE skip is
   claimed at the top of `evolve_once` instead, aborting that round
   cleanly.
3. `claim_rubric_replacement(...)` — an operator-provided new proposer
   brief is a CONTRACT EDIT, never a silent in-place patch:
   `_apply_rubric_replacement` writes the payload to the LIVE brief path
   (the same one `resolve_contract_inputs` hashes) and re-runs
   `ensure_epoch_for_contract`, which rolls the epoch. The rolled id is
   re-pinned for all subsequent rounds.

After each round, best-effort: the progressive `analysis.html` refresh
(`regenerate_in_progress_html`) so file:// readers see the latest lineage
without the dashboard.

### 2.5 Teardown — the `finally` block's order is deliberate

Whatever way the loop exits (completed, breaker, budget, exception,
Ctrl-C), the `finally` in `evolve_n_rounds` runs, in this order:

1. `_mark_run_terminal(workspace_root)` — the defensive terminal-state
   write: flip any lingering active-tournament envelope out of
   `phase="running"` so a normally-ended run never reads as a live
   tournament, even inside the heartbeat freshness window (a SIGKILL
   still can't self-clean; the frontend freshness gate covers that
   residue).
2. `beater.stop()` — the heartbeat task ends; the file stops advancing.
3. `release_workspace_lock(lock)` — another orchestrator may now start.
4. meta-loop emitter `close()` — BEFORE the harmonograf shutdown,
   because a sink flushing its final buffer to the gRPC console wants
   the server still up. Best-effort.
5. `harmonograf_handle.shutdown()` — unconditional, so a crashed evolve
   still tears the embedded server down. Best-effort.

> ⚠️ **TRAP** — if you add a resource with loop lifetime, register its
> teardown in this block and think about WHERE: anything that writes to
> the harmonograf console goes before step 5; anything that touches
> workspace files goes before step 3 (the lock is your mutual
> exclusion); anything the dashboard reads as "live" goes before step 2
> or it will briefly read as alive-and-frozen.

---

## 3. `evolve_once` — the gauntlet path

`evolve_once` (`src/zicato/evolve/gauntlet.py`) is ONE round. Its docstring
enumerates the classic thirteen steps; the code has grown numbered
sub-steps (0, 2a, 5a′, 10b″ …) between them. This section walks the real
sequence. The gauntlet (field size 1 — one champion, one challenger, one
full-board duel) is the default and the back-compat baseline; §4 covers
what changes when the structure widens the field.

```
evolve_once ─┬─ 0   claim_skip_round (safe abort point)
             ├─ 1   load workspace config, board(+meta), scoring, brief
             ├─ 1b  resolve proposer (spec + agent) ── wrap best-of-N
             ├─ 0b  open RoundLog: round_opened{contract_hash}
             ├─ 1c  tournament_spec ← weights.tournament_structure
             ├─ 1d  adapter + RuntimeConfig (+ per-round token ledger)
             ├─ 2   ensure v0 baseline; resolve parent (champion)
             ├─ 2a  A/A noise-floor calibration        (opt-in, idempotent)
             ├─ 2a′ contract pre-flight                (opt-in, idempotent)
             ├─ 2b  margin-vs-noise-floor warning      (round 0 only)
             ├─ 3   enumerate_mutations(parent snapshot)  [+ snapshot dump]
             ├─ 4   split_board → TRAIN/HOLDOUT; parent losses (train only)
             │      detect_patterns(train)
             ├─ 5   loss summary · 5a failure profile · 5a″ process exemplars
             ├─ 5a′ build screen-runner closure        (opt-in, one per round)
             ├─ 5b  make_strategy ──► field_size() > 1 ? ──► §4 (field path)
             ├─ 6   mint next_id (or reuse resume id); build post-apply
             │      validator; load prior experiments (memory)
             ├─ 6r  resume short-circuit (reuse persisted experiment) or
             │      _propose_child (best-of-N → screen → critique → validate)
             ├─ 7   check_patch_manifest_and_forbidden
             ├─ 9   validation errors? ──► _persist_rejected_round ──► return
             ├─ 10  write_experiment (outcome=None) + index dual-write
             │      run_fast_mode | run_tournament  (replicates, force_fresh)
             ├─ 10a infra circuit ──► _defer_round_infra_outage ──► return
             ├─ 10a′ cache gen_score.json ×2; RoundLog: units + gate (+holdout)
             ├─ 10b _gauntlet_decision_from_result (SelectionStrategy)
             ├─ 10b′ _confirm_gauntlet_promotion (evidence pre-gate, opt-in)
             ├─ 10b″ _integrity_block_reason (opt-in blocking modes)
             ├─ 10c claim_gate_override (operator force-promote/reject)
             ├─ 11  build OutcomeRecord ──► _finalize_generation
             │      RoundLog: decision_recorded
             ├─ 13b placebo arm duel (opt-in cadence)
             ├─ 14+ _round_epilogue (health · analyzer · epoch report)
             └─ ret EvolveRoundOutcome; RoundLog: round_closed
```

### 3.1 Step 0 — the skip safe point

A pending `skip_round` control flag aborts the round before any proposer
call or tournament write: `_skipped_round_outcome` fabricates a
rejection-shaped outcome and the loop moves on. The flag is consumed
(archived to `control_log/`) so it fires once.

### 3.2 Step 1 — workspace, contract artifacts, proposer

`load_current_board_with_meta` returns `(board, disable_drift,
judge_only)` — the board-level meta rides everywhere the board goes.
`load_current_scoring` and `load_current_brief` complete the frozen
contract view.

One subtlety computed right here and threaded far:
`_declared_custom_judge_names(board, weights)` — the union of every
`JudgeSpec.name` on every entry plus every `per_judge_weights` key. A
custom judge emits under the single `"custom"` drift kind on the
goldfive side, but a proposer hypothesis may still target it as a
`drift:<judge_name>` metric; this set is what lets the hypothesis
validator accept a declared judge name while still rejecting a
genuinely-unknown drift kind. Forget to thread it into a new propose
site and every hypothesis touching a custom judge starts bouncing with
"unknown drift kind".

Then the epoch's proposer is resolved ONCE per invocation:

- `load_epoch(...)` → the frozen `proposer_path` off `EpochConfig`;
- `resolve_proposer_spec(proposer_path)` reads the skill files once —
  never inside the retry loop;
- `build_proposer_agent(spec, proposer_path=...)` yields the
  `ProposerAgent` (built-in single-shot when `proposer_path is None`);
- `wrap_with_proposer_quality(agent, weights.proposer_quality)`
  interposes the best-of-N + critique wrapper. A contract pinning
  `best_of_n: 1` gets the agent back UNCHANGED — the historical
  single-sample path.

Both the gauntlet and the field path reuse this same agent, so a
configured proposer's skills shape every challenger identically.

### 3.3 Step 0b — the durable RoundLog opens

`_RoundLogEmitter(workspace_root, epoch_id, round_index)` wraps
`RoundLog` (`src/zicato/epoch/round_log.py`) with best-effort emission —
"a log failure can never fail the round." The first event stamps the
frozen contract hash:

```python
round_log = _RoundLogEmitter(workspace_root, resolved_epoch_id, round_index)
round_log.emit("round_opened", {"contract_hash": _epoch_cfg.contract_hash or ""})
```
*(src/zicato/evolve/gauntlet.py, `evolve_once` step 0b)*

The event vocabulary is CLOSED and typed — one frozen dataclass per
transition, registered in `EVENT_TYPES`
(`src/zicato/epoch/round_log.py`): `round_opened`, `proposal_attempted`,
`candidate_sampled`, `candidate_screened`, `critique_selected`,
`experiment_minted`, `patches_applied`, `validation_failed`,
`unit_completed`, `gate_evaluated`, `holdout_released`,
`evidence_replicated`, `decision_recorded`, `round_closed`. Unknown
tokens read back as raw envelopes so a newer writer's log still folds on
an older reader. The log is append-only, single-writer, `seq` gap-free,
and torn-tail tolerant (an unparseable LAST line is a crash artifact and
skipped; an unparseable INTERIOR line raises — someone bypassed the
writer).

> ✅ **ALWAYS** emit a RoundLog event when you add a round step that
> makes or records a decision. The RoundLog is the round's
> store-of-record trace; a decision that leaves no event is invisible to
> `fold_round_record`, the dashboard forensics, and future you.

### 3.4 Steps 1c–1d — structure, adapter, runtime config, token ledger

`tournament_spec = weights.tournament_structure` — read off the loaded
(frozen) weights so it is in lockstep with the contract hash. Adapter and
`RuntimeConfig` come from the factories
(`adapter_factory.make_adapter_from_config`,
`runtime_factory.make_runtime_config`). When
`config.max_tokens_per_round > 0`, a fresh `RoundTokenLedger` is minted
and rebound onto the config via `dataclasses.replace` — every scheduler
seam that already receives the config (full/fast schedulers, the screen,
evidence replicate duels) shares one tally with zero signature changes;
knob off (default 0) binds nothing.

### 3.5 Step 2 — baseline and parent

`_ensure_baseline_snapshot` materializes `v0` from the registered mutable
trees if the epoch has no generations yet (byte-for-byte copy of the
operator's source; on a contract roll it seeds from the previous epoch's
promoted head via the roll-seed marker — see 03-contract-and-epochs.md).
`_resolve_current_generation` reads the per-epoch `current_generation`
marker; the parent `Generation` is constructed with `promoted=True`.

Then two idempotent, opt-in epoch-open measurements, each persisted onto
`EpochConfig` (never hashed): `_maybe_calibrate_noise_floor`
(config.json `"calibrate_noise_floor": K` — champion vs itself K times
at replicate base 1000) and `_maybe_contract_preflight`
(`"contract_preflight": K` — A/A floor plus degradation signal against a
degraded copy, replicate base 2000; recommend-only). On round 0 only,
`_warn_margin_below_noise_floor` says loudly when `promote_margin` sits
inside the measured floor.

Both measurements are SERIAL and front-loaded: K draws, each a full pass
over the board, before the round's first duel, and `--parallelism` does
not shorten them. The calibration therefore owns the heartbeat while it
runs — `CALIBRATION_PHASE` plus a `{done}/{K}` suffix restamped per
settled draw (see the phase vocabulary in §6) — because the round's own
phase over a null tournament is exactly the shape a wedged round has.

### 3.6 Steps 3–5 — mutations, the split, patterns, the proposer's view

`enumerate_mutations(_resolve_mutable_trees(adapter, parent_snapshot))` —
zero mutation points is a hard `RuntimeError` ("did the adapter declare
its mutable_trees?"). `_dump_mutations_snapshot` writes the surface for
the dashboard, best-effort.

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

    train_seed = rotation_seed(weights.overfitting, resolved_epoch_id)
    train_ids, _holdout_ids = split_board(board, weights.overfitting, seed=train_seed)
```
*(src/zicato/evolve/gauntlet.py, `evolve_once` step 4 — excerpt)*

Everything the proposer will see is computed from the TRAIN slice only:
`_load_parent_losses` (the champion's per-entry loss profiles),
`detect_patterns` over a `DetectorInput` of those losses + train entries
+ events paths, `_render_loss_summary`, `_render_failure_profile`
(bucketed outcome marginals; empty slice renders the EMPTY string — the
"omit this section" sentinel), and `_render_process_exemplars_block`
(opt-in, redacted, best-effort, empty string when off/failed). This is
Golden Rule G8 in action — the invariant is enforced HERE, at
computation time, not just at prompt-render time.

### 3.7 Step 5a′ — the screen-runner closure

`_build_candidate_screen_runner` returns `None` — and therefore no screen
callable even exists on the propose path — unless the contract opts in
(`proposer_quality.screen_entries > 0` AND `best_of_n > 1`). When built,
ONE closure per round binds one deterministic rotating TRAIN panel
(`select_screen_entries` over the champion's replicate-0 baseline; the
holdout is never eligible) so every propose site this round screens on
the same panel, and stamps a `screening:r{round}` heartbeat phase so the
stall detector attributes the wall-clock honestly.

### 3.8 Step 5b — structure dispatch

```python
    strategy = make_strategy(tournament_spec, board_ids=[e.id for e in board])
    if strategy.field_size() > 1:
        return await _evolve_multi_challenger(...)
```
*(src/zicato/evolve/gauntlet.py, `evolve_once` step 5b — excerpt)*

Board-aware structures (racing) get the epoch's entry ids as default
`board_ids`; board-agnostic ones ignore them. Field size 1 falls through
to the gauntlet steps below, byte-for-byte the historical path.

### 3.9 Step 6 — propose (or resume)

Generation id minting: `_next_generation_id` — EXCEPT when this is the
resumed round, where the plan's `resume_generation_id` is reused (its
directory exists; a fresh id would orphan the completed `loss.json`
units). The proposer's post-apply validation hook is built by the shared
seam:

- `build_post_apply_validator` (`src/zicato/evolve/round.py`) — the
  `validate_experiment` hook the proposer agent calls on EVERY attempt:
  beat `applying`, derive the child snapshot all-or-nothing from the
  candidate's patches through the `GenerationStore`
  (`default_generation_store`), record it in `last_child_snapshot`, run
  `validate_post_apply`. A destructive patch is thereby a *retryable*
  feedback class (the validator strings go back into the proposer's next
  attempt) inside the same bounded `max_proposer_retries` budget — it
  used to cost a whole wasted tournament round.
- Experiment memory: `_load_prior_experiments` (best-effort; empty list
  on a stale index) — the "## What's already been tried" digest.

**Experiment memory, precisely.** `_load_prior_experiments` reads the
SETTLED cross-round digest for this epoch off the SQLite index —
best-effort: a missing/stale index yields an empty list and the proposer
simply runs without the `## What's already been tried` section. The
digest is curated, not a dump: capped at
`EXPERIMENT_MEMORY_MAX_ENTRIES = 12` (`src/zicato/core/experiment.py` —
"wins are never dropped by the cap; the sharpest recent rejections fill
the remainder"), each entry a `PriorExperiment` (core idea, modulating
ids, decision, banded Δscalar under restricted visibility, and the
diagnostic `prediction_accuracy`). With
`experiment_memory.cross_epoch: true` (contract knob, omit-at-default),
settled experiments from PRIOR epochs sharing the current
`contract_hash` are appended — marked `same_contract=False`, Δscalar
omitted (the number does not transfer), and admitted only into budget
left after same-epoch entries. Experiments under a DIFFERENT contract
hash are never surfaced regardless. On the field path the caller
concatenates this settled digest with the round's in-flight siblings
(decision `"in_flight"`) so challenger k diversifies away from
challengers 0..k−1.

**The resume short-circuit (step 6r).** When the plan resumes in place
for exactly this generation, the persisted experiment is reused verbatim
rather than re-proposed — the proposer is non-deterministic, and a fresh
proposal would invalidate the on-disk unit cache. The SAME validate hook
still runs once so the snapshot is re-derived idempotently from the
persisted patches; if re-validation fails (the parent tree changed
underneath), the round falls back to proposing fresh — "never score
against a tree we cannot rebuild."

Otherwise `_propose_child` builds the `ProposerContext` — the ONE propose
shape both pipelines share (previously two near-identical inline blocks,
which meant a new context field could land on one path only) — and calls
the agent. Inside the wrapper, per propose-step: N `candidate_sampled`
draws (each slot with a distinct edit-class hint), the optional guarded
screen (`candidate_screened` events; veto-first; one bounded revise
re-sample if all-vetoed), `critique_selected`, then the validate hook.
On success `_propose_child` emits `proposal_attempted` (empty errors),
`experiment_minted`, `patches_applied`, and stamps the authoritative
evolve `round_index` onto the experiment. On `ProposerError`, one
`proposal_attempted{errors}` per failed attempt is emitted and the error
propagates to the rejected tail.

**What the proposer sees — the `ProposerContext` inventory.** Every
input crossing into the propose step is enumerated here because this IS
the restricted-visibility boundary (Golden Rule G8): adding a context
field means adding a proposer-visible channel, which needs the envelope
argument written down. The fields as `_propose_child` populates them
(`src/zicato/proposer/agent.py` owns the dataclass):

| `ProposerContext` field | Populated from | Envelope status |
|---|---|---|
| `epoch_id`, `parent_generation_id`, `new_generation_id` | round coordinates | identity of the HARNESS, not the board — safe |
| `patterns` | `detect_patterns` over TRAIN losses | aggregated to counts/rates under `restrict_visibility` |
| `mutations` | `enumerate_mutations` on the parent snapshot | code spans — unrelated to the board split, passed whole |
| `brief_text`, `forbidden_ids` | the frozen proposer brief | operator-authored — the steering channel |
| `current_loss_summary` | `_render_loss_summary` (train) | one line of means — no identities |
| `failure_profile` | `_render_failure_profile` (train) | bucketed, board-anonymous marginals; `""` = omit |
| `process_exemplars` | `_render_process_exemplars_block` | opt-in; mechanically redacted; `""` = omit |
| `prior_experiments` | `_load_prior_experiments` (+ in-flight siblings on the field path) | banded Δscalar under restriction; capped at 12 |
| `mutation_track_records` | `_load_mutation_track_records` (index; best-effort `{}`) | per-mutation-point fertility counts — no entries |
| `custom_judge_names` | `_declared_custom_judge_names` | names only |
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
> and process exemplars are the worked examples of exactly this).

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
4. **Select** (`_select_best` / `_select_over`): the auxiliary-LLM
   self-critique when `critique_enabled` (scored against a quality bar
   — grounded in a tool call? targets a real failure mode? minimal
   diff?), else the deterministic heuristic (smallest diff targeting an
   observed failure mode). Survivors' banded panel counts feed the
   selection only as a LATE tiebreak, and not at all under
   `screen_veto_only`. `critique_selected{index, reason, slate,
   rationale}` — the mode, a per-candidate summary of the whole slate
   (core idea + mutation ids), and, when a critic chose, its one-line
   reason. Both selection routes (the aux critic and pi's in-session
   `select_candidate` tool) write the identical shape.
5. **Align the tree** (`_align_child_tree`) — the step you must not
   forget exists. Every slate sample's post-apply validation derived
   the SAME fixed child snapshot in place (each attempt clears the
   previous tree), so after N samples the on-disk tree belongs to the
   LAST-validated candidate while the critic may have chosen an
   EARLIER one. Before the fix, the tournament scored — and the field
   path diversity-judged — a tree that was not the experiment on
   record. The fix: when `chosen != last_validated`, run the validate
   hook once more on the chosen candidate (the idempotent
   clear-and-reapply); on unexpected findings, fall back to the
   last-validated candidate (restoring its tree with one more hook
   call) and stamp `:revalidate-fallback` onto the selection mode so
   the round log records why the critic's pick was not returned. Both
   pipelines are covered e2e by
   `tests/test_best_of_n_tree_integrity.py`.

> ⛔ **NEVER** decouple "the experiment we persist" from "the tree we
> mount". Any new selection/mutation of the slate AFTER validation must
> re-run the validate hook on whatever it returns — that hook is the
> only thing that makes tree and record agree.

### 3.10 Steps 7–10a — manifest check, rejected tail, the tournament, the infra circuit

`check_patch_manifest_and_forbidden` (`src/zicato/evolve/round.py`)
cross-checks every patch's `mutation_id` against the re-enumerated
manifest and the brief's `## Forbidden` ids — `RuntimeError` on either.

If validation failed (or the proposer exhausted retries),
`_persist_rejected_round` is the tail: the experiment is written with a
rejected `OutcomeRecord` whose reason is symbolic
(`"validation_failed: …"` vs `"proposer_retries_exhausted: …"`), folded
through `_finalize_generation` with NO lineage entry (the generation
never earned one), `validation_failed` + `decision_recorded` land on the
RoundLog, and `_round_epilogue` still runs (minus the analyzer, which
this tail historically skipped) so a stuck loop surfaces on the dashboard
even when nothing ever reaches a tournament.

Otherwise: `write_experiment` persists the experiment with
`outcome=None` (the index dual-write folds it in so the dashboard sees
the in-progress generation), and the duel runs:

- **fast mode with a cache** — `run_fast_mode` against
  `_load_historical_aggregate` (the champion's cached `gen_score.json`).
  First round of a fresh epoch has no cache: fast degrades to one full
  A/B round that seeds it — which is what makes `--mode fast` safe as a
  default.
- **full mode** — `run_tournament` with the noise knobs threaded:
  `replicates=strategy.replicates()` (per-duel replication, averaged
  before the gate; default 2 for the gauntlet), `child_diff_size` (the
  opt-in parsimony term; exactly absent at weight 0), and the cache
  semantics captured in one load-bearing expression:

```python
            champion_force_fresh=(not fast_mode) and resumed_experiment is None,
            ...
            force_fresh=resumed_experiment is None,
```
*(src/zicato/evolve/gauntlet.py, `evolve_once` step 10 — excerpt)*

`--mode full` re-samples BOTH sides for noise; a resumed round
cache-reads both sides so the interrupted round's completed units are
HITs and resume stays nearly free. (Full semantics: `run_tournament`'s
docstring, `src/zicato/tournament/runner.py`.)

**The aggregate dict — the shape everything downstream reads.** Both
sides of every duel are reduced by `aggregate_generation_score`
(`src/zicato/tournament/scoring.py`) into a plain JSON-shaped dict —
deliberately a dict, not a dataclass, because it is cached to
`gen_score.json`, crossed into envelopes, and consumed by the gate, the
strategies, the dashboard, and fast mode as-is. Its keys are therefore a
wire contract:

| Key | Meaning |
|---|---|
| `drift_loss_mean` | mean per-run drift loss over the entries scored |
| `pass_rate` | binary pass fraction over entries WITH an expectation (empty ⇒ 1.0) |
| `mean_score` | the UNIFORM continuous outcome axis — equals `pass_rate` byte-for-byte on an all-bool board (the back-compat proof is in the source comment); the scalar's pass component and the gate's `aggregate` scope read THIS |
| `per_entry` | `{entry_id: {drift_loss, pass_fail, score}}` — the gate's `per_entry` scope reads `score` |
| `namespace_aggregates` | weight-multiplied per-namespace means (`cost:`, `latency:`, `rubric:`, `schema:`, …) — already sign-folded so lower is uniformly worse-to-better comparable |
| `scalar_components` | the display/gate breakdown: `drift`, `pass`, one entry per non-drift namespace, plus `diff_complexity` ONLY when opted in (the key is never written at the default — the omit-at-default idea applied to a runtime artifact) |
| `scalar` | the lower-is-better number the gate compares, synthesized through the Seam-2 dispatcher (`resolve_scalar`), byte-identical to `sum(scalar_components.values())` for the builtin path |

> ⚠️ **TRAP** — empty input aggregates to `scalar=0.0`, `pass_rate=1.0`
> ("nothing to compare", which the gate treats as a tie). If you build a
> new evaluation path, an accidentally-empty loss list does not error —
> it produces a plausible-looking no-op aggregate. Assert non-empty at
> your call site.

**Fast mode, precisely.** `run_fast_mode` evaluates ONLY the challenger
and compares it against the champion's cached whole-board aggregate
(`_load_historical_aggregate` reading `gen_score.json`). That is why
step 10a′ caches BOTH sides after every settled duel
(`_cache_gen_score` ×2): the promoted child of this round is the
champion whose cache next round's fast duel reads. Two consequences
worth memorizing: (a) the diff-complexity term applies on the full A/B
path only — fast mode never re-derives the challenger scalar through
that seam; (b) on the field path the round-level provenance is resolved
from the CHAMPION's cached-vs-fresh unit tally alone
(`_resolve_round_champion_mode` → `full` / `fast` / `fast-degraded`) —
challengers always run fresh, so their tally says nothing about reuse.

**Step 10a — the endpoint-outage circuit.** BEFORE anything downstream
consumes the duel: when `config.infra_abort_round_threshold >= 1` and
`_count_infra_aborted_runs(tournament_result)` (counts
`is_infra_abort_cause` losses — worker crashes and kills, never genuine
budget exhaustion; cache-reused units can never contribute) reaches it,
`_defer_round_infra_outage` settles the round as `deferred_infra`:
no `gen_score.json` caching (a mostly-aborted aggregate would poison fast
mode), no strategy routing, no outcome/lineage/journal — the experiment
stays un-outcomed on disk, exactly the shape `prepare_resume`
reconciles. The health report carries the `infra_outage` WARNING.

### 3.11 Steps 10a′–10c — verdict routing, confirmation, integrity, override

On a settled duel: `_cache_gen_score` for BOTH sides (fast-mode reuse),
then the RoundLog trio — `_emit_tournament_units` (aggregate
`unit_completed` events), `_emit_gate_evaluated`, and `holdout_released`
when the runner consulted a holdout — then the `TOURNAMENT_SETTLE`
progress transition.

**10b — the verdict flows through the strategy even on the gauntlet.**
`_gauntlet_decision_from_result` feeds the already-run duel's
`GateOutcome` into a fresh `GauntletStrategy`
(seed → next_matchups → record_result → champion) and reads its
`SelectionDecision`. The strategy never re-decides the duel; this exists
so the *decision shape* is uniform and swappable across structures.

**10b′ — evidence-gate confirmation.** `_confirm_gauntlet_promotion`:
when the contract sets `promote_confidence_threshold` (scaffolds do; the
bare default is off), a train-promote is confirmed by the SAME
Bradley–Terry defer→replicate→inconclusive adjudication the field driver
runs (`confirm_promotion_with_evidence`,
`src/zicato/selection/driver.py`), BEFORE anything persists. The
pre-gate can only HOLD a promotion, never force one — a reject/defer
passes through untouched. While the verdict defers, one extra
crowning-pair duel per replicate is spent through the SAME `run_matchup`
+ gate every other duel uses, on the TRAIN slice, at the reserved slots:

```python
        nonlocal evidence_replicates_run
        replicate_slot = EVIDENCE_REPLICATE_BASE + evidence_replicates_run
        evidence_replicates_run += 1
        matchup_id = f"bt-replicate:r{replicate_slot}:{left_id}:{right_id}"
```
*(src/zicato/orchestrator.py, `_confirm_gauntlet_promotion._replicate_duel` — excerpt)*

The reserved slot is a natural cache MISS the first time (a fresh draw of
BOTH sides) and an idempotent HIT on a resumed confirm — the A/A
calibration's reserved-index discipline, applied to evidence (Golden Rule
G7). Non-separating CIs within the budget leave the champion standing:
the decision goes terminally inconclusive, the duel is recorded to the
dead-letter queue (`record_inconclusive`), and the journaled `evidence`
block carries the rating CIs plus the full `ci_history` trail. Each
refit's CI state also lands as an `evidence_replicated` RoundLog event.

**10b″ — opt-in integrity blocking.** `_integrity_block_reason` guards a
GATE-DECIDED promotion only (never an operator force-promote): (a) diff
containment — every file outside the registered mutable trees must be
byte-identical parent↔child (`zicato.evolve.containment`, mirroring
`crates/supervisor/src/diff_containment.rs`; fail-open on unreadable
snapshots); (b) gate-contradiction re-derivation
(`delta_scalar <= -promote_margin`, the supervisor's
`promotion_gate.rs check_row` semantics applied pre-persist). Both
default OFF — the shipped baseline is the supervisor's alarm-only
posture.

**10c — the operator gate override.** `claim_gate_override(workspace,
next_id)` at the one safe point (gate settled, nothing persisted). An
override is NEVER a silent flip: `operator_override` +
`operator_override_reason` are stamped onto the `OutcomeRecord`, and a
forced reject carries `"operator override: …"` in `rejection_reason`.

### 3.12 Steps 11–16 — persist, placebo, epilogue

The `OutcomeRecord` is assembled with every runtime-evidence field
(deltas, structure, `champion_eval_mode` from the runner's authoritative
provenance, the holdout block, `train_loss`/`holdout_loss`/
`generalization_gap`, the evidence block) and flows through the ONE write
pipeline:

```python
    The ONE write pipeline every round tail flows through:

    1. ``update_experiment_outcome`` — the :class:`OutcomeRecord` lands on
       ``experiment.json`` (the canonical record; a field present on the
       record can no longer be dropped by one tail's hand-rolled copy);
    2. live SQLite index dual-write (best-effort, never aborts the round);
    3. optional lineage upsert (``lineage_generation`` — ``None`` for a
       validation-rejected round that never entered lineage, and for the
       multi-challenger loop which defers lineage until after its crowning
       invariant checks);
    4. optional champion-marker advance (``advance_current_generation`` —
       the gauntlet's on-promotion step, sequenced between lineage and
       journal exactly as the inline tail wrote them);
    5. optional journal append (``journal=False`` lets the multi-challenger
       path keep its all-outcomes-then-all-journals order).
```
*(src/zicato/orchestrator.py, `_finalize_generation` docstring)*

A rejected generation IS recorded in lineage (a dead branch, visible in
`zicato epoch list`); the `current_generation` marker advances only on
promotion. `decision_recorded` lands on the RoundLog with full
provenance (structure, reason, override flags, parent + promoted ids).

**The holdout block and the generalization fields.** When the runner
consulted a holdout, `TournamentResult.holdout` carries the
Ladder-mediated evidence block — a plain JSON dict with the stable shape
documented at `holdout_record` (`src/zicato/tournament/ladder.py`) and
journaled verbatim under `OutcomeRecord.holdout`:

| Key | Meaning |
|---|---|
| `confirmed` | `True`/`False`/`None` — the released confirmation bit (`None` when the Ladder withheld a release) |
| `train_scalar` / `holdout_scalar` | the crowning duel's two slice scalars |
| `ladder_released` | whether the release rule fired this round |
| `ladder_budget_total` / `ladder_budget_remaining` | the per-epoch holdout-query budget state |
| `threshold` | the train-improvement bar the release rule applied |

Alongside it, `_generalization_fields` pairs the child's TRAIN-slice
scalar (the score that gated it) with its HOLDOUT-slice scalar
(`TournamentResult.holdout_child_scalar`, deliberately decoupled from
the Ladder's release semantics so the gap is measurable whenever a
holdout exists) into `train_loss` / `holdout_loss` /
`generalization_gap` — positive gap = holdout worse than train, the
memorization signature the health detector reads off the champion
lineage. All of these are RUNTIME evidence, never contract inputs.

**13b — the placebo arm.** `_maybe_run_placebo_arm_gauntlet` runs one
EXTRA scheduled duel on the opt-in cadence
(`overfitting.random_baseline_every_n`): champion vs a
semantics-preserving no-op copy of itself. It runs BEFORE the health
assessment so a promoted placebo raises its CRITICAL finding in THIS
round's report; it never advances the champion.

**14–16 — `_round_epilogue`.** The shared end-of-round tail — loop-health
assessment persisted to `epochs/{epoch}/health/round_{N}.json` (CRITICAL
no-signal warning to stderr), the decision-telemetry analyzer (writes
`insights/round_{N}.md` for the NEXT round's proposer, grounded in the
real mutation-id list so the LLM cannot hallucinate targets), and the
epoch analysis report regeneration. "Near-verbatim duplicated across the
gauntlet and multi-challenger paths before extraction; now both call here
so a new epilogue step can never land on one pipeline only." Every step
best-effort by contract.

Final heartbeat (`PROMOTE`/`REJECT` progress transition),
`round_closed`, and the `EvolveRoundOutcome` returns.

---

## 4. `_evolve_multi_challenger` — the field path

When `strategy.field_size() > 1`, `evolve_once` hands everything it has
computed (mutations, patterns, summaries, the screen runner, the
proposer agent, the open RoundLog) to `_evolve_multi_challenger`
(`src/zicato/evolve/field.py`). Steps 1–5 of §3 are SHARED — the field
path re-derives only the train split (it receives the raw board). What
follows is what differs.

### 4.0 The structure shapes, in one table

06-tournament-and-selection.md owns the theory; what the field path
needs from each structure is only its scheduling shape
(`src/zicato/selection/` registry + strategies; params live in
`TournamentStructure.params` and fold into the contract hash):

| Structure | `field_size` | Shape | Key params |
|---|---|---|---|
| `gauntlet` | 1 | one champion-vs-challenger duel; promote-on-gate (never reaches this path) | `replicates` (default 2) |
| `single_elim` / `double_elim` | bracket | challenger-vs-challenger nodes (winner = `lower_scalar_id()`), then champion-gate crowning | `field_size`, `replicates` |
| `swiss` | N | `rounds_n` swiss pairings, then crowning | `field_size`, `rounds_n`, `replicates` |
| `racing` | N | escalating board-slice rungs cut the field (`board_subset` per rung); a rung CUTS, it does not crown; final full-train crowning duel | `field_size`, `eta`, `board_fraction`, optional `matchup_budget_seconds`, `promote_confidence_threshold`/`promote_confidence_replicates` |

All structures end the same way: ONE crowning champion-gate duel whose
`GateOutcome` decides promotion — which is why the holdout confirmation
and the evidence pre-gate bolt onto "the crowning matchup" uniformly.

### 4.1 The train/holdout rule, restated for structures

The structure's internal matchups — swiss rounds, elim nodes, racing
rungs, INCLUDING the final champion-gate duel that decides promotion —
score on the TRAIN slice only. The holdout is never consumed to *pick*
the leader; the full board is retained solely for the ONE Ladder-mediated
holdout confirmation after resolution. Empty holdout ⇒ train IS the full
board ⇒ byte-identical to whole-board behaviour. This is the
[train-selects-holdout-confirms] invariant; the gauntlet obeys the same
rule through `run_tournament`'s internal split handling.

### 4.2 Minting the field

Ids are minted monotonically from `_next_generation_id`'s base
(`v{base_n + offset}`) so every challenger gets a distinct id even when
a proposer attempt fails before deriving a snapshot. For each of the
`field_size()` slots, `_propose_and_apply_challenger`:

1. beats `proposing:…` and publishes a live `"proposing"` field-status
   record BEFORE the LLM call (`on_status` → `_publish_proposing` →
   the `ActiveTournament` envelope in phase `PROPOSING`) — the
   dashboard's proposing tracker shows each slot enter the field live;
2. builds the same `build_post_apply_validator` hook and calls the same
   `_propose_child`;
3. on `ProposerError`: returns `None` plus a `"rejected"` status carrying
   the FULL per-attempt failure list (`attempt_reasons`) — a failed slot
   narrows the field, never crashes the round;
4. on success: `write_experiment` (outcome=None) + index dual-write,
   and — critically — a PENDING lineage append:

```python
    # The creation-time write is PENDING (promoted=null), NOT a dead branch
    # (promoted=False). The challenger has applied a snapshot but has not
    # been crowned or cut — it is still racing. ``promoted=False`` reads as
    # REJECTED, so a False default would render an in-flight racer as a dead
    # branch on /api/lineage while it is mid-tournament. Pending → null →
    # the dashboard maps it to "racing"; the settle-time append flips it to
    # the resolved bool.
    append_to_lineage(workspace_root, epoch_id, child_gen, parent_id=parent_id, pending=True)
```
*(src/zicato/orchestrator.py, `_propose_and_apply_challenger` — excerpt)*

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

A soft-rejected slot is not just dropped: `_persist_soft_reject` writes a
terminal REJECTED outcome onto its already-persisted `experiment.json`
(reason `"field_diversity_duplicate: …"` / `"field_diversity_overlap:
overlap 0.xxx with sibling vN exceeds diversity_tolerance 0.yyy"`) so
the canonical record and the lineage tree agree with the live hero —
never a stale "pending".

An all-failed field returns a clean rejection-shaped
`EvolveRoundOutcome` ("multi-challenger field: no challenger applied
cleanly") — after persisting the field-status so the dashboard reads
"N proposed · 0 applied", and after `decision_recorded` +
`round_closed`.

**The placebo slot.** On the opt-in cadence, ONE extra slot is appended
LAST (after the all-failed early return, so a fully-failed field keeps
its historical outcome; and last so sibling diversity and
`first_challenger_id` are untouched): the placebo flows through the
unchanged strategy + gate like any challenger.

### 4.3 The two closures the driver runs on

`resolve_tournament` (`src/zicato/selection/driver.py`) owns scheduling.
Its loop is four steps, verbatim from its docstring:

```python
    1. ``request_field(strategy.field_size())`` resolves the champion and
       the applied challenger field.
    2. ``strategy.seed(...)`` initialises bracket state.
    3. Loop: ``strategy.next_matchups()`` → run the batch concurrently →
       ``strategy.record_result(...)`` for each, until
       ``strategy.resolved()`` or the strategy schedules nothing.
    4. Return ``strategy.champion()``.
```
*(src/zicato/selection/driver.py, `resolve_tournament` docstring — excerpt)*

Each batch fans out under one `asyncio.gather`; `on_progress` fires
right after a batch is scheduled (the strategy's pending set is
populated, so `live_rounds()` carries the in-flight matchups with
`winner: null`) — publish-before-run is what makes the live bracket
exist during, not after. When a `pre_gate` is supplied, a `"promoted"`
decision is held through the defer→replicate loop
(`_apply_pre_gate` → the closest-CI duel via `replicate_duel`, refit,
recheck) before it is returned. The orchestrator supplies the closures:

- **`_request_field`** — hands the strategy the champion `Contestant` +
  the applied challengers (with snapshots + experiments).
- **`_run_matchup`** — one duel via `run_matchup`
  (`src/zicato/tournament/runner.py`). The strategy⇄orchestrator contract
  is the `Matchup` dataclass (`src/zicato/selection/strategy.py`) — every
  field a strategy can use to shape a duel:

  | `Matchup` field | Meaning | Default |
  |---|---|---|
  | `matchup_id` | stable id linking the result back to the bracket node / Swiss pairing / racing rung | required |
  | `left`, `right` | the contestants; by convention `left` is the incumbent/higher seed — the gate treats `left` as nominal parent | required |
  | `board_subset` | a racing rung's entry-id slice; `None` = full (train) board | `None` |
  | `replicates` | paired board runs averaged before scoring; the unpinned default is 2 for gauntlet/bracket/Swiss ("replication, not bracket shape, is the noise lever"), racing pins 1 | `1` |
  | `stage_index` | the WITHIN-tournament stage — never confuse with the evolve `round_index` | `0` |
  | `bracket_slot` | elim bracket position (`"WB-R1-0"`); empty otherwise | `""` |
  | `matchup_budget_seconds` | wall-clock cap on the matchup's TOTAL board-unit execution — distinct from the per-entry budget; catches "each unit under budget, the sum grinds for hours" | `None` |

  For challenger-vs-challenger nodes (no incumbent), the winner is
  `MatchupResult.lower_scalar_id()` — `delta_scalar` is `right − left`,
  negative means `right` is better, and ties keep `left` (the higher
  seed, the historical no-improvement convention).

  `_run_matchup` runs under the round-shared semaphore:

```python
    # --- Cross-matchup concurrency cap. A non-gauntlet structure schedules
    # SEVERAL matchups of a round concurrently (the driver fans the batch out
    # under one ``asyncio.gather``). Without a shared gate each matchup would
    # mint its own ``Semaphore(parallelism)``, so N concurrent matchups could
    # run ``N × parallelism`` board units at once — overshooting the operator's
    # parallelism intent and the LLM endpoint's concurrency. One semaphore,
    # created here per round and handed to every ``run_matchup``, makes the
    # whole round draw from ONE global cap.
    round_unit_semaphore = asyncio.Semaphore(max(1, int(config.parallelism)))
```
*(src/zicato/evolve/field.py, `evolve_field_round` — excerpt)*

  Each matchup scores on the train board (a racing rung's `board_subset`
  is intersected inside `run_matchup`), caches both sides' aggregates
  (`_cache_gen_score` — mirroring the gauntlet), emits units + gate onto
  the RoundLog, and accumulates the CHAMPION's cached-vs-fresh unit tally
  (`unit_provenance`) — challengers always run fresh, so only the
  champion's tally is meaningful for the round-level
  `champion_eval_mode` (`_resolve_round_champion_mode`).
- **`_publish_live_structure`** (`on_progress`) — every scheduled batch
  republishes the live envelope with settled rounds + the in-flight round
  (`winner: null, pending: true`) + standings-so-far, through the SAME
  `_serialise_rounds`/`_serialise_standings` the settle path uses, with
  the runner's authoritative per-lane `projected` map overlaid
  (`_overlay_projected_live_progress` /
  `_overlay_projected_standings`). This is what lets the bracket exist
  DURING the run instead of "being seeded" until settle. Best-effort.
- **`_replicate_duel` + `_on_inconclusive`** (only when
  `promote_confidence_threshold` is set) — the evidence pre-gate's extra
  crowning-pair duels at RESERVED replicate slots
  (`EVIDENCE_REPLICATE_BASE + j`, with `cache_scores=False` so a
  single-draw aggregate never overwrites the round-scored
  `gen_score.json`), and the dead-letter record + `evidence_replicated`
  trail for an unresolved crowning.

**Durable record opens BEFORE resolution.** `_persist_field_tournament`
writes `tournaments/field-{…}.json` in `in_progress` state as soon as the
field is minted (issue #16): the runtime `active_tournament` envelope is
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
on the holdout through the SAME Ladder machinery + the SAME per-epoch
`ladder_state.json` budget the gauntlet uses. A released
non-confirmation flips the crowning promote to a holdout reject — the
champion stands, `reason_override` carries the cause. The champion side
is resolved defensively (left by convention, but a right-seeded champion
still confirms correctly), and the crowning challenger's TRAIN scalar is
paired with its HOLDOUT scalar so the generalization gap is measured on
the same duel. `holdout_released` lands on the RoundLog.

**Integrity blocking** mirrors the gauntlet's 10b″ against the crowned
child's snapshot and the crowning duel's champion-oriented delta
(`crowning_delta_scalar` — note the orientation normalization: the gate
treats LEFT as parent, so a right-seeded champion flips the sign).

**Field overrides.** Unlike the gauntlet's single in-flight generation, a
field round resolves a whole slate, so
`claim_field_gate_overrides(workspace, field_candidate_ids)` may target a
non-winner, the leader, or SEVERAL candidates. The re-resolution is PURE
(`_apply_field_overrides`):

```python
    * ``promoted_ids`` — the (possibly multi-element) promoted SET. With no
      override it is exactly ``{promoted_id}`` (or empty), so the
      single-promotion path is byte-identical.
    * ``promoted_id`` — the PRIMARY head that advances
      ``current_generation``. The originally-crowned leader if it survived;
      otherwise the lowest-scalar operator-promoted candidate (mirroring the
      gate's lower-scalar-wins convention); ``None`` when every leader was
      force-rejected (the champion stands).
    * ``override_provenance`` — the per-generation override-status readback
      for the durable field record (never a silent flip).
    * ``effective_decision`` — the EFFECTIVE crowning verdict the workspace
      will actually commit: the post-confirmation/post-override truth every
      durable store must describe (issue #20).
```
*(src/zicato/orchestrator.py, `_apply_field_overrides` docstring — excerpt)*

Everything durable — the settled live envelope
(`_settle_active_tournament`, RETAINED with `phase="completed"` unlike
the gauntlet's cleared transient), the durable field record, the
RoundLog `decision_recorded` — is written with the HOLDOUT-RESOLVED,
POST-OVERRIDE `effective_decision`, so no store ever shows a crown the
champion pointer contradicts.

**Write order + the crowning invariant.** The field tail is strictly
ordered — [outcomes-then-invariant-then-lineage]:

1. every challenger's `OutcomeRecord` persists via
   `_finalize_generation(..., journal=False)` (crowned = in the promoted
   set; the crowning challenger carries the holdout block + gap fields;
   every dead branch carries the strategy's reason, its `final_rank`, and
   its `match_record`);
2. THEN the crowning invariant is checked loudly:

```python
    _bracket_promoted = effective_decision.decision == "promoted"
    if _bracket_promoted != (promoted_id is not None):
        raise RuntimeError(
            "crowning invariant violated: settled bracket decision "
            f"{effective_decision.decision!r} (promoted_generation_id="
            f"{effective_decision.promoted_generation_id!r}) disagrees with the "
            f"champion to be crowned ({promoted_id!r}); refusing to persist a "
            "bracket the champion pointer / lineage contradict"
        )
```
*(src/zicato/evolve/field.py, `evolve_field_round` — excerpt)*

   (plus: the promoted id must name a challenger that actually applied
   this round);
3. THEN lineage upserts every challenger (promoted set → `promoted=True`
   on the spine, everyone else a dead branch), the champion marker
   advances to the PRIMARY head — and is RE-READ: a marker write that did
   not stick (read-only workspace) raises rather than diverging silently;
4. THEN the journal, one entry per challenger.

> ⛔ **NEVER** reorder this tail or insert a write between steps 1 and 3
> that could fail after lineage moved. The whole point is that an
> invariant violation aborts BEFORE any lineage write — a settled bracket
> and a champion pointer that disagree is the class of silent corruption
> issue #20 existed to kill.

**The round summary comes from the crowning matchup.** The returned
`EvolveRoundOutcome`'s scalars are resolved from the crowning duel
(champion side vs the leader that reached the gate), NOT from
`_first_aggregate_for`'s standings average and NOT from a
child-defaults-to-parent fallback — which used to report delta 0.0 on a
rejection even though the gate measured a real regression (issue #10 in
the code comment). On a rejection, `proposed_generation_id` is the
LEADING challenger the reason is about, not an arbitrary `applied[0]`.

---

## 5. Inside one board unit — the worker anatomy

Both pipelines bottom out in the same primitive: `_run_single`
(`src/zicato/tournament/runner.py`) runs ONE entry under ONE generation
in an isolated subprocess. This is the L3 robustness layer and the
documented monkeypatch anchor the test suite stubs
(`tests/_subprocess_worker_support.py` swaps exactly
`runner._run_single`). Its docstring is the sequence contract:

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
*(src/zicato/tournament/runner.py, `_run_single` docstring — excerpt)*

Steps 6–7 complete the contract: a worker that exited non-zero, OR a
missing/corrupt result file (e.g. the SUPERVISOR SIGKILLed a wedged
worker), is ALSO an aborted run — not a crash; the tournament continues
to the next entry either way. Cleanup always runs: the ephemeral
checkout, the temp args/result files, and the worker's `active_runs`
record if the kill prevented self-removal.

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
(`_adapter_spec` — the same `config.json` block the factory reconstructs
from), the LLM roles as dotted paths (`_role_worker_spec` →
`_callable_dotted_path`, Golden Rule G9), and the FULL scoring weights
(`_weights_spec` → `ScoringWeights.to_json`). Seam 1 scoring
(per-run drift reduction, including any `drift_reducer` plugin and
`drift_kind_aggregation` transform) runs INSIDE the worker, which is
exactly why the weights must cross complete — a dropped field means the
worker scores under defaults while the orchestrator believes otherwise
(the historical `per_judge_weights` desync class;
03-contract-and-epochs.md §"serializer completeness").

**Inside the worker** (`src/zicato/_tournament_worker.py`): rebuild the
adapter and weights from the spec, attach the per-run goldfive
`JSONLPersistenceSink` (plus the harmonograf live sink when
`ZICATO_HARMONOGRAF_URL` is set — the loop exports the auto-launched
URL), `chdir` into the ephemeral checkout, drive
`RunnableHarness.run(entry, sinks, config)`, then reduce
`events.jsonl` → `LossProfile` → `loss.json` and write the result file.
The run id derives from the run's stable coordinate — per-generation
unique (`conv-<generation>-<entry>` in the example harness; the index's
`runs` table primary-keys on it).

**After the wait:** the runner stamps `match_id` onto the settled
`LossProfile` AND rewrites `loss.json` with the tag (so a later full
`zicato repair index`, which re-reads `loss.json`, re-derives the same
provenance), keys the ActiveTournament grid update on `(entry_id,
side)` — each entry has TWO rows, one per side; keying on `entry_id`
alone lands parent transitions on the child row — and dual-writes the
run into the SQLite index (`_ingest_run_into_index`, best-effort).

**The cache above it.** `_run_single` sits under the per-unit cache
keyed `(generation, entry, replicate)`: a HIT reuses the persisted
`loss.json`; a MISS runs. Infra-aborted profiles are never persisted to
the cache (which is why `_count_infra_aborted_runs` reflects only THIS
round's live failures). Every reserved-base subsystem (calibration,
preflight, screen, evidence) is just this cache addressed at its
reserved slots.

> ⛔ **NEVER** bypass `_run_single` to evaluate a generation "quickly"
> in-process. Everything above — isolation, budgets, kill-ability,
> telemetry capture, cache coherence, index provenance — exists at this
> boundary. In-process evaluation is only legitimate inside tests that
> deliberately stub `runner._run_single` (the power oracle does this and
> says so), and for the screen/preflight paths that already route
> through the same runner machinery.

---

## 6. The observability surface of one round

Every phase of a round announces itself on three planes. When you add a
step, wire all three or your step is invisible to the operator, the
supervisor, or the forensics — pick which failure you want, or wire them.

**Plane 1 — heartbeat `phase` strings** (`_beat` /
`HeartbeatBeater.update`; read by the dashboard header and the
supervisor's staleness logic). The vocabulary as emitted today:

| Phase string | Emitted at |
|---|---|
| `evolve_n_rounds:start` | loop boot (epoch resolved, lock held) |
| `evolve_once:round_{N}` | round scheduled (loop side) |
| `evolve_once:calibrating_noise_floor:{done}/{K}` | the epoch-open A/A calibration, restamped per settled draw |
| `proposing:round_{N}:{vX}` | before the proposer call (both paths) |
| `screening:r{N}` | the candidate screen's panel runs |
| `applying:…` | inside `build_post_apply_validator` per attempt |
| `tournament:round_{N}:{vX}` / `tournament:round_{N}:{matchup_id}` | duel start (gauntlet / per-matchup) |
| `deferred_infra:round_{N}:{vX}` | the infra deferral tail |
| `infra_backoff:round_{N}:{delay}s` | the loop's backoff sleep |
| `done:round_{N}:{vX}:{decision}` / `done:round_{N}:{tournament_id}:{decision}` | round settled |
| `after_round_{N}:{decision}` | loop-side post-round stamp |
| `evolve_n_rounds:done` / `evolve_n_rounds:budget_exhausted` | terminal |

**Plane 2 — progress-log transitions** (`src/zicato/runtime/progress_log.py`;
the TRUE liveness signal): `LOOP_START`, `ROUND_START`, `PROPOSE`,
`TOURNAMENT_START`, `TOURNAMENT_SETTLE`, `PROMOTE`/`REJECT`, terminal
`SETTLED` (completed) / `STOPPED` (budget or breaker — still a CLEAN
end; a STALLED run is a frozen `seq` with no terminal event). `_beat`
couples planes 1 and 2: passing `progress=` appends the transition and
stamps its `seq` onto the same heartbeat update.

**Plane 3 — durable traces**: the RoundLog (§8), the live
`ActiveTournament` envelope + the durable `tournaments/field-*.json`
record (§4.3), the health report
(`epochs/{e}/health/round_{N}.json`), the analyzer insights, and the
journal.

The teardown path is part of this surface: `_mark_run_terminal` (in the
loop's `finally`) flips any lingering active-tournament envelope out of
`phase="running"` so a normally-ended run never reads as a live
tournament — a SIGKILL still can't self-clean, which the frontend's
heartbeat-freshness gate covers.

---

## 7. The failure atlas

What actually happens on each failure class, and the invariant that
makes it safe. This table is the difference between debugging a round
and guessing.

| Failure | Detection point | What the round does | Durable footprint | Invariant |
|---|---|---|---|---|
| Proposer returns unparseable / schema-invalid output | `propose_experiment`'s parse+validate loop | retry with the error fed back, up to `max_proposer_retries` | `proposal_attempted{errors}` per attempt | bounded retries; the budget is shared with post-apply failures |
| Patch set fails post-apply validation on every retry | `build_post_apply_validator` → `ProposerError` | gauntlet: `_persist_rejected_round` (reason `proposer_retries_exhausted: …`); field: the slot narrows the field | rejected `experiment.json` + `validation_failed` + `decision_recorded` (gauntlet); rejected field-status (field) | a destructive proposer never crashes the loop; the journal stays append-only |
| Patch targets a forbidden / stale mutation id | `check_patch_manifest_and_forbidden` | `ValueError` propagates — this is a hard programming/contract error, not a retryable; the type is `ValueError` so that "bad patch set" is ONE exception class across the whole apply path (issue #83), not the severity | none beyond the raise | the hypothesis's `modulating` set is the ONLY thing patches may touch |
| One board run exceeds its wall-clock budget | worker cooperative budget → parent `wait_for` → supervisor deadline (three layers) | SIGTERM→grace→SIGKILL; synthesised aborted `LossProfile` (`BUDGET_ABORT_CAUSE`); scored worst-case for that entry | the aborted profile (tagged, never cache-persisted for infra causes) | the tournament continues; one entry cannot wedge a duel |
| Worker crashes / result file missing or corrupt | `_run_single` step 6 | ALSO an aborted run — not a crash; continue | aborted profile with an infra `abort_cause` | `is_infra_abort_cause` distinguishes infra from genuine budget exhaustion |
| Whole endpoint down (many infra aborts) | `_count_infra_aborted_runs` ≥ `infra_abort_round_threshold` (opt-in) | `_defer_round_infra_outage`: verdict discarded, nothing journaled, experiment left un-outcomed | `decision_recorded{deferred_infra}` + health `infra_outage` WARNING; NO gen_score caches | deferral ≠ rejection; resume reconciles the un-outcomed experiment |
| Orchestrator dies mid-tournament | next invocation's `prepare_resume` | resume-in-place when self-consistent + ≥1 unit done (reuse experiment, cache-HIT done units); discard on ANY ambiguity | the interrupted round's partial units stay valid cache | "never score against a tree we cannot rebuild"; cold start is byte-identical |
| Round would blow the invocation's total budget | `WallClockBudgetPolicy` via `asyncio.wait_for` | round cancelled; synthetic `wall_clock_budget` rejection; loop stops | the synthetic outcome in the return list | cooperative-only guard; L3/supervisor covers wedges |
| Holdout does not confirm a train win | `confirm_crowning_holdout` / `_confirm_crowning_on_holdout` | promote flipped to reject; champion stands; reason `holdout_not_confirmed` carried | `holdout_released{confirmed: false}` + the holdout block on the OutcomeRecord | train selects, holdout confirms; Ladder budget charged |
| Evidence CIs never separate | the pre-gate's replicate budget exhausts | terminally inconclusive; champion stands | dead-letter record + `evidence_replicated` trail + journaled `evidence` block | a promotion needs evidence; noise cannot manufacture ~37 straight wins |
| Settled bracket contradicts the champion pointer | the crowning-invariant checks | loud `RuntimeError` BEFORE lineage writes | the raise itself (nothing corrupt persisted) | outcomes-then-invariant-then-lineage ordering |
| Live-envelope / RoundLog / index / report write fails | each `best_effort(...)` wrapper | logged at debug, round unaffected | possibly-missing observational artifact | best-effort is for observational writes ONLY |
| Operator forces a verdict | `claim_gate_override` / `claim_field_gate_overrides` at the safe points | verdict replaced, NEVER silently: `operator_override(_reason)` stamped | override provenance on record + RoundLog + field record | overrides are recorded prerogative, not silent flips |

---

## 8. The canonical RoundLog event sequence

The convergence oracle pins the exact per-round event sequence for the
deterministic gauntlet contract (best_of_n pinned to 1 ⇒ no
`candidate_sampled`/`critique_selected`; 5-entry board below the split
floor ⇒ no holdout events; pre-gate off ⇒ no `evidence_replicated`):

```python
        types = [e.type for e in events]
        assert types == (
            ["round_opened", "proposal_attempted", "experiment_minted", "patches_applied"]
            + ["unit_completed"] * (2 * BOARD_SIZE)
            + ["gate_evaluated", "decision_recorded", "round_closed"]
        ), f"round {round_index}: {types}"
```
*(tests/test_convergence_known_answer.py, `test_gauntlet_converges_to_known_floor`)*

So for the shipped 5-entry example: `round_opened` →
`proposal_attempted` → `experiment_minted` → `patches_applied` → 10 ×
`unit_completed` (one per (entry, side)) → `gate_evaluated` →
`decision_recorded` → `round_closed`, with `seq` exactly `1..N` gap-free
and `fold_round_record(events).complete` true. The racing test extends
it: 4 × the proposal triplet (one per field slot), rung-by-rung
`unit_completed` + `gate_evaluated`, and a `decision_recorded` whose
provenance carries `structure: "racing"`, `promoted_generation_ids`, and
`overrides: {}`.

The fully-populated grammar (every optional feature on) per round is:

```
round_opened{contract_hash}
( proposal_attempted{errors}* )                       # failed attempts, if any
  [ candidate_sampled{i,n,revise?} × best_of_n ]      # slate sampling
  [ candidate_screened{index,vetoed,confirmed,…} × slate ]
  [ critique_selected{index,reason} ]
proposal_attempted{}  experiment_minted  patches_applied     # per applied challenger
[ validation_failed{findings} ]                        # the rejected tail only
unit_completed{entry,replicate,side} × (units run)
gate_evaluated{rule_fired,decision} × (matchups)
[ holdout_released{confirmed} ]
[ evidence_replicated{ci_state} × refits ]
decision_recorded{decision,provenance}
round_closed
```

Use this grammar when adding events: a new event type goes into
`EVENT_TYPES` + the `RoundEvent` union + `fold_round_record`
(`src/zicato/epoch/round_log.py`), with a dataclass default for every
field so pre-feature logs decode identically — the `CandidateSampled
.revise` field is the worked example of an additive event field.

### 8.1 A worked trace: round 1 of the convergence example

Abstract walkthroughs lie by omission; here is round 1 of
`examples/zicato_examples/target_0_convergence` (the deterministic
target — chapter 01 §5.4) made concrete. Setup: epoch freshly created
from the pinned contract (`best_of_n: 1`, `replicates: 1`, no pre-gate;
5-entry board, below the split floor so train = full board); the seeded
policy carries three defect tokens; the scripted proposer's first
payload removes `omit-summary`.

1. **Baseline.** `_ensure_baseline_snapshot` seeds `v0` from the
   registered `agent/` tree through the git genstore —
   `.zicato/repo/.git` now exists with `v0` tagged.
   `current_generation` reads `v0`.
2. **Propose.** `_next_generation_id` mints `v1`. The scripted
   `aux_llm` returns experiment `exp_{epoch}_v1`: hypothesis
   `modulating=("style_rules",)`, one `Patch` re-emitting the policy
   minus one token. The validate hook derives `v1`'s snapshot from
   `v0` + patch (git commit, tag `v1`), `validate_post_apply` passes.
   RoundLog so far: `round_opened`, `proposal_attempted{}`,
   `experiment_minted{exp_…_v1}`, `patches_applied{v1}`.
3. **Tournament.** `run_tournament` schedules 5 board units × 2 sides =
   10 subprocess workers (bounded by `parallelism`). Each worker
   checks out an ephemeral copy of its side's tree, runs the
   deterministic harness, and reduces to `loss.json`: every v0 run
   carries 3 info-drift frames (drift_loss 3.0), every v1 run 2.
   Champion aggregate: `drift_loss_mean=3.0, mean_score=0.4 (2/5),
   scalar=3.6`. Challenger: `2.0 + 0.6 = 2.4`. Ten `unit_completed`
   events land (entry × side).
4. **Gate.** `evaluate_gate`: `delta_scalar = −1.2 ≤ −promote_margin`;
   no champion-passed entry flipped (v1 strictly adds a pass); no
   guarded namespace regressed → `promoted`. `gate_evaluated` lands.
5. **Persist.** `gen_score.json` cached for v0 AND v1; the
   `OutcomeRecord` (decision `promoted`, `scalar_score_delta=-1.2`,
   `champion_eval_mode="full"`, no holdout block) folds through
   `_finalize_generation`: `experiment.json` gains its outcome, the
   index refreshes, lineage upserts `v1 {parent: v0, promoted: true}`,
   `current_generation` advances to `v1`, journal gains
   `## v1 — <core idea>`. `decision_recorded` +
   `round_closed` complete the log — 18 events, `seq` 1..18.
6. **Epilogue.** `health/round_1.json` written (no CRITICAL findings —
   the planted defects differentiate, so `degenerate_scoring` stays
   silent); `analysis.html` regenerates; the loop's reject streak
   resets; round 2 begins against champion `v1`.

Round 2 is the negative control: the scripted proposer ADDS a token,
the gate measures `delta_scalar = +1.2`, rejects with a
"challenger regressed" reason, lineage records `v2` as a dead branch —
and `current_generation` still reads `v1`. Every number above is
asserted, to the float, in `test_gauntlet_converges_to_known_floor`.

---

## 9. The data-type flow

Who constructs each type, who consumes it, and where it persists. All
types frozen (`frozen=True, slots=True`); state transitions go through
`dataclasses.replace`.

| Type (owner file) | Fields that matter | Constructed by | Consumed by | Persisted at |
|---|---|---|---|---|
| `Experiment` (`core/experiment.py`) | `id` (`exp_{epoch}_{gen}`), lineage coordinates, `proposed_at`, `hypothesis`, `patches`, `outcome` (None until settled), `round_index` (birth round) | the proposer (`propose_experiment` → agent → `_propose_child` stamps `round_index`) | applier/validator, tournament tails, journal, index, dashboard | `epochs/{e}/generations/{g}/experiment.json` (+ `patches/{id}.json`) via `write_experiment` / `update_experiment_outcome` |
| `HypothesisSpec` (`core/experiment.py`) | `core_idea`, `modulating` (the ONLY ids the patches may touch), `why`, expected drift/metric movements, `expected_pass_rate_delta`, `risks` | the proposer LLM, schema-validated with bounded retries | manifest check, diversity signatures, experiment memory, journal one-liners, hypothesis ledger | inside `experiment.json` |
| `Patch` (`core/mutation.py`) | `mutation_id`, op kind, payload | the proposer | applier (`derive_generation` through the genstore), validator, diff-complexity | `patches/{id}.json` |
| `Generation` (`core/epoch.py`) | `id`, `parent_id`, `snapshot_root`, `promoted`, `round_index` (birth round — never re-stamped) | orchestrator (parent from the marker; child at mint) | runner (mounts `snapshot_root`), lineage, genstore | `lineage.json` nodes + the genstore (git tag/commit per generation) |
| `LossProfile` (`core/loss.py`) | `drift_counts`, `pass_fail`, continuous `score`, `metric_counts` (namespaced), `runtime_ms`, `abort_cause`, `tokens_spent` | the reducer (`telemetry/reducer.py`) inside the worker path, per run | scoring aggregation, gate, detectors, screen, health, failure profile | `runs/{…}/loss.json` (the per-unit cache keys on it) + index `runs` table |
| `GateOutcome` (`tournament/gate.py`) | `decision`, `reason` (names the rule that fired), `delta_scalar`, `delta_pass_rate` | `evaluate_gate` at the end of every duel | strategies (read, never re-decide), evidence gate, RoundLog `gate_evaluated`, OutcomeRecord deltas | inside `TournamentResult` / `MatchupResult`; not standalone |
| `TournamentResult` (`tournament/runner.py`) | both aggregates, `outcome`, `per_entry_losses`, `champion_eval_mode`, `unit_provenance`, `holdout`, `holdout_child_scalar` | `run_tournament` / `run_fast_mode` / `run_matchup` | gauntlet tail (10a–11), `_gauntlet_decision_from_result`, infra counter, gen_score caching | aggregates cached as `gen_score.json`; the rest flows into `OutcomeRecord` |
| `MatchupResult` (`selection/strategy.py`) | matchup id, left/right ids + aggs, `outcome`, `stage_index`, `bracket_slot` | `_run_matchup` from a `TournamentResult` | strategies (`record_result`), `SelectionDecision.matchups`, standings, match records | inside the durable field record (`_serialise_rounds`) |
| `SelectionDecision` (`selection/strategy.py`) | `promoted_generation_id`, `decision`, `reason`, `matchups`, `crowning_matchup_id`, `standings` | the strategy (`champion()`), re-written by holdout/override re-resolution into `effective_decision` | field tail (outcomes, lineage, envelopes), round summary | the settled field record + `ActiveTournament` |
| `OutcomeRecord` (`core/experiment.py`) | decision + reason, deltas, `structure`/`final_rank`/`match_record`, `champion_eval_mode`, `holdout` block, `train_loss`/`holdout_loss`/`generalization_gap`, `operator_override(+reason)`, `evidence` | the round tails (gauntlet step 11; field per-challenger loop; rejected/soft-reject tails) | journal, index, dashboard decision surface, gap detector | onto `experiment.json` via `_finalize_generation` → `update_experiment_outcome` |
| `PriorExperiment` (`core/experiment.py`) | `core_idea`, `modulating`, `decision` (incl. `"in_flight"`), banded delta, `same_contract`, `prediction_accuracy` | `_load_prior_experiments` (index) + the field loop (siblings) | the proposer's memory section | never persisted — a render-time projection |
| `EvolveRoundOutcome` (`evolve/round_api.py`) | parent/child ids, decision (incl. `deferred_infra`), reason, scalars + delta, health summary/critical | every `evolve_once` return path | `evolve_n_rounds` stop policies, the CLI summary | not persisted (the journal/experiment carry the durable truth) |
| `ResumePlan` (`runtime/resume.py`) | `classification`, `resumes_in_place`, `resume_generation_id`, `resume_experiment` | `prepare_resume` at loop start / after a deferral | `evolve_once` steps 6/6r, cache-read decisions | derived from the workspace; not persisted |
| `Standing` (`selection/strategy.py`) | `generation_id`, `rank`, `scalar`, wins/losses, `status`, `role` | the strategy's standings view | dashboard leaderboard, `final_rank` on OutcomeRecords | inside the settled field record |
| `_AppliedChallenger` (`evolve/propose_apply.py`, private) | generation id + snapshot + experiment + `Generation` | `_propose_and_apply_challenger` | the field loop (`by_id`, lineage, outcomes) | not persisted (its parts are) |
| `_CrowningHoldout` (`evolve/gate.py`, private) | post-holdout promoted id, reason override, holdout block, train/holdout scalar pair, champion-oriented `crowning_delta_scalar` | `_confirm_crowning_on_holdout` (pure) | override re-resolution, integrity block, OutcomeRecord stamping | not persisted (its parts are) |
| `_FieldMintDecision` (`evolve/propose_apply.py`, private) | `action` ∈ accept / reject_duplicate / reject_overlap, overlap + peer index | `_mint_challenger_field` (pure) | the field loop's soft-reject branches | not persisted (soft-reject reasons land on experiment.json) |
| `GateOverride` (`runtime/control_consumer.py`) | forced `decision`, operator `reason` | the operator via control files; claimed at safe points | override application + provenance stamping | archived to `control_log/`; provenance on records |

Reading the table column-wise gives you the three persistence planes:
the **canonical record plane** (`experiment.json`, `lineage.json`,
`journal.md`, RoundLog — under `epochs/`), the **cache plane**
(`gen_score.json`, per-unit `loss.json` slots — reconstructible), and
the **ephemeral plane** (`runtime/` envelopes — cleared on crash). A new
field must pick its plane deliberately; a decision that only lives on the
ephemeral plane does not exist (that was the pre-#16 field-tournament
bug: a completed swiss epoch rendered blank from the index alone).

---

## 10. The extracted-seam inventory

The decomposition rule this codebase converged on, and the one you must
follow:

> ✅ **ALWAYS** put a NEW round step into an existing seam or extract a
> new one. ⛔ **NEVER** copy a shared step into both `evolve_once` and
> `evolve_field_round`. The two pipelines share
> steps by CALLING THE SAME FUNCTION — the god-function era proved that
> "the same code, twice, inline" guarantees the two copies drift (the
> epilogue and the propose plumbing were both near-verbatim duplicated
> before extraction, and features landed on one path only).

The seams, and what each owns:

| Seam | Home | Owns | Shared by |
|---|---|---|---|
| `evolve_n_rounds` + stop policies | `evolve/loop.py` | the loop, circuit breakers, budget, backoff, control safe points, progress log lifecycle | (the loop itself) |
| `ensure_epoch_for_contract`, `_create_epoch_from_contract`, `_promoted_head_snapshot`, component-hash bookkeeping | `evolve/epoching.py` | the roll-at-evolve-time decision (03 covers it) | loop start, rubric replacement |
| `build_post_apply_validator` | `evolve/round.py` | the propose-time apply+validate hook (beat → derive all-or-nothing → validate; retryable findings) | gauntlet step 6, every field slot |
| `check_patch_manifest_and_forbidden` | `evolve/round.py` | manifest + forbidden-ids cross-check | both pipelines |
| `_propose_child` | `evolve/propose_apply.py` | the ONE `ProposerContext` build + propose + RoundLog proposal events + round-index stamp | gauntlet, `_propose_and_apply_challenger` |
| `_build_candidate_screen_runner` | `evolve/round_context.py` | the per-round screen closure (one panel per round) | both pipelines via `ProposerContext.screen_candidates` |
| `_finalize_generation` | `evolve/persist.py` | the ONE outcome→index→lineage→marker→journal write pipeline | every round tail (gauntlet, field, rejected, soft-reject) |
| `_round_epilogue` | `evolve/persist.py` | health + analyzer + report regeneration | both pipelines + the rejected tail (`run_analyzer=False`) |
| `_persist_rejected_round` | `evolve/persist.py` | the validation-reject tail | gauntlet (the field's equivalent is per-slot narrowing) |
| `_defer_round_infra_outage` | `evolve/decision_support.py` | the deferral tail (no caches, no journal, health WARNING) | gauntlet (field-side infra handling rides run_matchup losses) |
| `_mint_challenger_field`, `_apply_field_overrides`, `_confirm_crowning_on_holdout` | `evolve/propose_apply.py`, `evolve/gate.py` | the PURE field decisions (diversity, overrides, holdout re-resolution) — I/O stays at the call site | field path; unit-testable without e2e |
| `_gauntlet_decision_from_result`, `_confirm_gauntlet_promotion`, `_integrity_block_reason` | `evolve/gate.py` | verdict routing, evidence confirmation, opt-in blocking | gauntlet (field has driver-native equivalents) |
| `_RoundLogEmitter`, `_emit_tournament_units`, `_emit_gate_evaluated` | `evolve/round_reporting.py` | best-effort RoundLog emission | both pipelines |
| lifecycle services (`_beat`, `_now_iso`, `_resolve_or_launch_harmonograf`, `_build_meta_loop_emitter_safe`, env restorer, launch handles) | `evolve/lifecycle_services.py` | heartbeat/harmonograf/emitter plumbing | loop + both pipelines |
| placebo minting + cadence | `evolve/placebo.py` + `_mint_placebo_challenger`/`_maybe_run_placebo_arm_gauntlet` | the control arm | both pipelines |
| containment check | `evolve/containment.py` | the Python mirror of the supervisor's diff-containment rule | `_integrity_block_reason` (both pipelines) |
| dashboard projection (`_publish_active_tournament`, `_settle_active_tournament`, `_persist_field_tournament`, `_serialise_rounds/standings`, overlays, `_mark_run_terminal`) | `evolve/dashboard_projection.py` | every live-envelope + durable-record write | field path + loop teardown |

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
default `127.0.0.1:7920` — a walk range disjoint from the dashboard's
7892 so the two never contend) is a SEPARATE OS PROCESS spawned by
`zicato evolve`. Its entire coupling to Python is read-only file I/O plus
signals:

- **What it reads:** the `.zicato/runtime/` state files — `heartbeat.json`
  (staleness), the active-run records (each carrying `started_at`,
  `last_progress`, the worker `pid`, and a `deadline = started_at +
  wall_clock_budget_seconds`), the tournament envelope — plus, for the
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
  (`_integrity_block_reason`, §3.11) precisely so the supervisor can stay
  a pure observer.
- **The handshake on kills:** the tournament parent waits
  `RuntimeConfig.supervisor_kill_wait_s` (default 20.0 s) for the
  supervisor to escalate-kill an over-budget worker before falling back
  to its own SIGTERM→grace→SIGKILL — the supervisor is the single
  escalator when present; without one, that window is the abort-latency
  floor (tests shrink it).

> ⚠️ **TRAP** — if you add a new long-running worker kind, it must write
> an active-run record with a pid and progress timestamps, or the
> supervisor cannot see it and a wedge in it is unkillable-by-watchdog.
> The record shape is `src/zicato/runtime/state.py`.

---

## 12. The concurrency model of one round

Know exactly what runs in parallel, because every unit of parallelism
here is somebody's rate limit:

- **The orchestrator is one asyncio event loop, single-threaded.** All
  round bookkeeping (RoundLog appends, lineage writes, ledger mutations)
  happens on it with no awaits between read and write —
  `RoundTokenLedger` says so explicitly ("Single-threaded by design:
  mutations happen on the orchestrator's event loop with no awaits
  between read and write", `src/zicato/core/runtime.py`). If you
  introduce an `await` inside what used to be an atomic
  read-modify-write of shared round state, you have introduced a race.
- **Board units fan out under a semaphore.** `RuntimeConfig.parallelism`
  (default 4) bounds in-flight board units. In FULL mode a unit runs
  parent + child concurrently, so the true subprocess ceiling is
  `2 × parallelism`; fast mode runs only the child, so `parallelism`.
- **A field round shares ONE semaphore.** The driver gathers a whole
  matchup batch concurrently; without the round-level
  `round_unit_semaphore`, N concurrent matchups would each mint their
  own `Semaphore(parallelism)` and run `N × parallelism` units at once
  (§4.3). Any new evaluation channel inside a round must accept and use
  the caller's semaphore, not mint its own.
- **Workers are processes, not threads.** The GIL (Global Interpreter
  Lock) discussion in `docs/design/ROBUSTNESS.md` is why: a CPU-wedged
  or C-extension-blocked evaluation cannot be pre-empted in-process, so
  isolation must be at the OS-process boundary to be killable.
- **The per-round token ledger clips launches, not flights.** Schedulers
  consult `check_and_clip()` at every would-launch point; work already
  in flight completes. Un-launched units record the same
  budget-exceeded losses a matchup-deadline trip synthesizes.

---

## 13. The monkeypatch surface (for test authors)

The suite patches the loop at DOCUMENTED anchors only. Use these; do not
invent new ones (and if you move one, keep the name importable at its
old path — chapter 01 §6's late-binding trap):

| Anchor | What stubbing it gives you | Used by |
|---|---|---|
| `orch.evolve_once` / `orch.ensure_epoch_for_contract` / `orch.block_while_paused` / `orch._resolve_or_launch_harmonograf` | loop-level tests with fabricated round outcomes | the evolve-loop tests |
| `runner._run_single` | in-process evaluation under the REAL scheduling/replicate/cache/gate machinery — "the test suite's documented monkeypatch anchor" | the power oracle, tournament tests, `tests/_subprocess_worker_support.py` |
| `zicato.evolve.loop._sleep_for_backoff` | no real sleeps in backoff tests — "a seam so tests can stub it" | infra-circuit tests |
| `orch.time` | the clock seam kept importable on the orchestrator (`import time  # noqa: F401 — kept as the ``orch.time`` clock seam`) | budget tests |
| the conftest autouse pair | default-proposer text shim + harmonograf launch stub — the ONLY stubs the convergence oracle allows itself | everything |

> ✅ **ALWAYS** prefer the deterministic example harnesses
> (`zicato_examples.target_0_convergence.harness`, its `NoisyPolicyAdapter`,
> the scripted mocks) over new hand-rolled stubs: they run through the
> REAL worker boundary, and their noise model is seeded from stable
> coordinates so "rates" in tests are deterministic functions of chosen
> seeds — calibrated documentation, not flaky statistics
> (`tests/test_decision_procedure_power.py`'s docstring states this as
> policy).

---

## 14. What to internalize before you edit

1. **Two pipelines, shared seams.** Find the seam before you write a
   line; §10's table is the map. If your step must run on both paths and
   no seam fits, extract one and re-export it.
2. **Every safe point is explicit.** Operator control claims happen at
   named points (between rounds; step 0; step 10c; the field's
   post-holdout claim). Do not add a control effect anywhere else — a
   mid-tournament flag claim races the writes.
3. **Persistence order is semantics.** experiment → index → (invariant)
   → lineage → marker → journal. `_finalize_generation`'s docstring is
   the contract; the field path's deferred-lineage variant exists for the
   crowning invariant.
4. **Best-effort is a two-sided contract** (chapter 01 §6). Round-fatal
   steps raise; observational steps are wrapped; each new step declares
   which it is.
5. **The oracle pins all of this.** After any change in this chapter's
   territory, `uv run pytest tests/test_convergence_known_answer.py -q`
   — the decision script, the exact scalars, the artifacts, the round-log
   grammar, the index rows, and the marker semantics are all asserted
   there. Green is necessary, not sufficient; red is always yours.
