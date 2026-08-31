# 06 — Tournament & Selection

> **Covers.** How zicato turns a proposed challenger into a promote/reject
> decision. The **board unit** and the reserved replicate ladder. The
> universal, structure-agnostic **unit cache** — its choke point, its
> per-replicate slot map, the averaging fold, and the two bug cases that
> shaped it (replicate-cache clobbering, and evidence-gate replicate-slot
> reuse). **The worker boundary** as a formal spec: closures rejected,
> module-level callables with reset-able module state, the scrubbed
> environment, config-pin threading, the ephemeral-checkout contract, and the
> args-file protocol. The run lifecycle (`_run_single`) and the board-unit
> schedulers (`_run_replicated`, the full/budgeted/fast fan-outs, and the
> per-round token ledger). The promote **gate** (`evaluate_gate`), which is
> the per-duel decider, and its holdout confirmation. The four public runner
> entry points. The **selection layer**: the `SelectionStrategy` contract,
> `evaluate_tournament`'s structure-agnostic walk, the five strategies, the
> Bradley–Terry evidence pre-gate with its dead-letter queue, and the
> cycle-robust winner resolvers, which only ever *propose*. The **placebo**
> control arm. Ends in two recipes — *Add a tournament structure* and *Make a
> harness adapter*.
>
> **Prerequisites.**
>
> - 02-architecture.md §3 (`evolve_once`) — where the tournament sits in a round.
> - 03-contract-and-epochs.md §3.7 — computing the contract hash, and why a
>   structure, replicates, or gate knob rolls the epoch.
> - 04-evaluation-statistics.md §1.3 (the per-generation scalar the gate
>   compares), §4 (A/A noise-floor calibration), and §5 (the Ladder-mediated
>   holdout).
> - 05-proposer.md §"5.6 Best-of-N" — where the challenger tree comes from, and
>   the agreement between that tree and the persisted experiment record that
>   this chapter depends on.
> - 07-runtime-and-durability.md §"`checkout_ephemeral`" and §"The atomic-write
>   contract" — the per-run tree, and how `loss.json` is written.
>
> **Invariants introduced in this chapter.** Each is load-bearing: breaking
> one corrupts a score or promotes an unsafe challenger. The `ID` column is
> the locator other documents cite; the prose of this chapter, and every
> chapter that cites these rules, uses the `Name`.
>
> | ID | Name | Invariant |
> |----|------|-----------|
> | T1 | the evaluate-once rule | A **board unit** `(generation_id, entry_id, replicate_index)` is immutable under a fixed contract and is evaluated AT MOST ONCE. `_run_unit_cache_first` is the single choke point every unit — champion, challenger, screen, evidence replicate — routes through. |
> | T2 | the canonical-replicate-slot rule | Replicate 0 is the canonical `runs/<entry>/loss.json`; replicate r>0 is the sibling `loss.r<r>.json`. Nothing may write one replicate's sample onto another replicate's slot. |
> | T3 | the cache-only-budget-exhaustion rule | Only a wall-clock-budget exhaustion is cache-eligible. An **infra abort** (`parent_kill` / `gone_no_result` / `nonzero_exit:{code}` / `prepare_failed` / `result_unreadable`) is NEVER persisted, so a transient blip cannot poison a unit's score for the epoch. |
> | T4 | the importable-worker-callable rule | Every callable that crosses the worker boundary is a **module-level (or class-attribute) importable object**. A closure-local callable is rejected at spawn time (`_callable_dotted_path`) rather than surfacing later as an opaque worker failure. |
> | T5 | the config-pins-not-environment rule | Contract and config flags cross the worker boundary through **config pins in the args file** rather than through the environment. A scrubbed worker environment carries only the process-essential keys plus the declared `api_key_env` names. |
> | T6 | the gate-is-the-per-duel-decider rule | `evaluate_gate` is the one accept/reject test for a duel. A `SelectionStrategy` reads a `GateOutcome` and interprets it per its own bracket/Swiss/racing rules; it never re-implements or re-runs the gate. |
> | T7 | the only-promotion-advances-the-champion rule | The champion pointer advances ONLY on a `"promoted"` `SelectionDecision`. Every layer above the gate (the Bradley–Terry pre-gate, the resolvers, the placebo) can only HOLD a promotion; none can force one. |
> | T8 | the disjoint-reserved-bases rule | The reserved replicate bases are pairwise disjoint (duels `0..`, calibration `1000`, preflight `2000`, screen `3000`/`3001`, evidence `4000`) so an auxiliary draw can neither read nor clobber a canonical replicate slot. |
> | T9 | the mounted-tree-matches-the-chosen-candidate rule | The child snapshot the tournament mounts is derived from the patches of the experiment the round persists. The enforcing seam is 05-proposer.md §"5.6.5 Mounting the chosen candidate". |
> | T10 | the distinct-draws-only rule | The Bradley–Terry audit only ever accumulates DISTINCT draws; a duplicate matchup id is refused, because identical data re-presented to the fit separates confidence intervals by repetition alone. |
> | T11 | the placebo-never-crowns rule | The placebo arm is a real lineage child scored by the unchanged gate, but it NEVER advances the champion pointer and is split out of the optimization-stream health detectors. |

---

## 6.0 Map of the subsystem

Two packages. A **duel** is one scored comparison of two generations over the
board. `zicato.tournament` runs duels and decides them; `zicato.selection` wraps
duels in a structure and crowns a winner. The gate is the seam: the tournament
owns it, and the selection layer only *reads* its verdict.

| File | What lives there | Approx. size |
|---|---|---|
| `src/zicato/tournament/runner.py` | The four public entry points (`run_tournament` full A/B, `run_fast_mode`, `run_matchup`, `confirm_crowning_holdout`), `_run_single` (the run lifecycle — the test suite's monkeypatch anchor), `_gate_with_regression`, `TournamentResult`, the progress-bumping sink | 1636 lines |
| `src/zicato/tournament/scheduling.py` | The board-unit schedulers: `_run_replicated`, `_run_board_units_full` / `_run_board_units_full_budgeted` / `_run_board_units_fast`, `_run_unit_cache_first` (the cache-first choke point), `_run_full_board_unit`, `_IncrementalScorer`, `_effective_unit_semaphore`, `_token_budget_spent` | 1196 lines |
| `src/zicato/tournament/unit_cache.py` | The per-unit loss cache + provenance: `_unit_loss_path`, `_resolve_cached_unit`, `_persist_unit_loss`, `_skipped_unit_loss`, `_average_losses`, `_UnitProvenance` | 288 lines |
| `src/zicato/tournament/worker_transport.py` | The process boundary: `_adapter_spec`, `_role_worker_spec` + `_callable_dotted_path`, `_scrubbed_worker_env` + `_api_key_env_names`, `_config_pins`, `_checkout_run_snapshot`, `_aborted_loss_profile`, `_weights_spec`, `_entry_to_dict`, the `_stamp_*` context threaders, `_terminate_worker` | 923 lines |
| `src/zicato/_tournament_worker.py` | The subprocess worker: the args-file protocol (`_load_args`), `_build_adapter`, `_drive_session`, `_evaluate_expectation`, the config re-pin, the abort provenance stamp, `main` | 838 lines |
| `src/zicato/tournament/gate.py` | `evaluate_gate` (the three rungs), `GateOutcome`, `holdout_confirms`, `diff_size_evidence`, the tolerance constants | 566 lines |
| `src/zicato/selection/strategy.py` | The `SelectionStrategy` ABC + the value types (`Contestant`, `Matchup`, `MatchupResult`, `SelectionDecision`, `Standing`, `RoundRecord`, `MatchRecord`), `pending_match_record`, `rung_for_match_id` | 564 lines |
| `src/zicato/selection/driver.py` | `resolve_tournament` (the structure-agnostic walk), `confirm_promotion_with_evidence` (the BT defer→replicate→inconclusive loop) | 400 lines |
| `src/zicato/selection/registry.py` | `STRATEGY_REGISTRY`, `make_strategy`, `STRUCTURE_DEFAULT_REPLICATES`, `default_replicates_for` | 110 lines |
| `src/zicato/selection/strategies/*.py` | `gauntlet` (164), `single_elim` (413), `double_elim` (474), `swiss` (413), `racing` (467) | — |
| `src/zicato/selection/evidence_gate.py` | The Bradley–Terry pre-gate: `evidence_verdict`, `closest_ci_duel`, `EVIDENCE_REPLICATE_BASE`, `MIN_CREDIBLE_DUELS`, `read_promote_confidence_threshold`, `rating_block` | 502 lines |
| `src/zicato/selection/resolve.py` | The cycle-robust winner resolvers (propose-only): `condorcet_check`, `smith_set`, `ranked_pairs`, `copeland_order`, `resolve_leader`, `build_matrix` | 383 lines |
| `src/zicato/selection/dead_letter.py` | `InconclusiveRecord`, `record_inconclusive`, `read_inconclusive`, `list_inconclusive` | 122 lines |
| `src/zicato/evolve/placebo.py` | `build_placebo_experiment`, `derive_placebo_snapshot`, `placebo_round_due`, `PLACEBO_HYPOTHESIS_MARKER` | 220 lines |
| `src/zicato/evolve/field.py` | The round facade: `_open_field_round` and the four phase calls | 123 lines |
| `src/zicato/evolve/field_candidates.py` | `assemble_candidate_field`, `CandidateField`, the proposing publish, the empty-field settlement, the placebo slot | 383 lines |
| `src/zicato/evolve/field_execution.py` | `execute_field_tournament`, `run_field_matchup`, `request_field`, `publish_live_structure`, `record_inconclusive_duel`, `FieldExecution` | 556 lines |
| `src/zicato/evolve/gate.py` | `resolve_field_verdict`, `_confirm_crowning_on_holdout`, `_apply_field_overrides`, `_integrity_block_reason`, `_resolve_round_champion_mode` | 549 lines |
| `src/zicato/evolve/settlement.py` | `settle_field_round` and its four private steps, `RoundSettlement`, `ordered_promotions` | 671 lines |
| `src/zicato/evolve/propose_apply.py` | `_mint_placebo_challenger`, `_maybe_run_placebo_arm_gauntlet`, `_propose_and_apply_challenger` | 775 lines |

Two facts about the file layout matter before you edit anything:

> ⚠️ TRAP — `runner.py` re-exports the entire public surface of
> `scheduling.py`, `unit_cache.py`, and `worker_transport.py` (three big
> `from … import …  # noqa: F401` blocks). The re-export is intentional: the
> test suite reaches `_unit_loss_path`, `_average_losses`, `_adapter_spec`,
> `_terminate_worker`, the timeout constants — everything — through
> `zicato.tournament.runner`, and it **monkeypatches them there**. `_run_single`
> stays in `runner.py` for that reason: it is the one anchor the whole
> suite swaps. The schedulers resolve `_run_single` by *attribute access on the
> runner module object* (`runner._run_single`) rather than a bound import, so a
> `monkeypatch.setattr(runner, "_run_single", …)` reaches them. If you move a
> helper, re-export it from `runner.py` or you silently break dozens of tests.

> ✅ ALWAYS keep new tournament helpers importable from `zicato.tournament.runner`.
> The stable import path IS the contract with the test suite; the physical file
> split is a readability choice and is invisible to callers by design.

### 6.0.1 Call topology, per round

`evolve_field_round` is a facade. It expands `PreparedRound` into a `FieldRound`
— the round's coordinates, contract inputs, and runtime seams under the names
its phases use — and then calls four phase functions in order. The phase names
follow the lifecycle steps the execution plan serves
(`zicato.query.execution_plan.ROUND_STEPS`: propose, apply, run, gate, decide),
so a round's code and a round's served tree name the same steps.

```
evolve_once (orchestrator.py)
 ├─ PreparedRound
 └─ evolve_field_round (evolve/field.py)          # facade over the phases below
      ├─ assemble_candidate_field                 # propose + apply
      │    (evolve/field_candidates.py) → CandidateField | terminal outcome
      │    └─ produce_candidate_batch(strategy.field_size())
      ├─ execute_field_tournament                 # run
      │    (evolve/field_execution.py) → FieldExecution | terminal outcome
      │    └─ evaluate_tournament(strategy, request_field=…, run_matchup=…)
 │         ├─ request_field(strategy.field_size())  # return applied challengers
 │         ├─ strategy.seed(champion, challengers)
 │         └─ loop: strategy.next_matchups() → gather(run_matchup(m)…) → record_result
 │              run_field_matchup (evolve/field_execution.py)
 │               └─ run_matchup (runner.py)  →  _run_replicated (scheduling.py)
 │                   └─ _run_board_units_full × replicates
 │                        └─ _run_full_board_unit  (champion ‖ challenger)
 │                             └─ _run_unit_cache_first  ← the cache choke point
 │                                  ├─ HIT: _resolve_cached_unit  (no run)
 │                                  └─ MISS: _run_single → worker → _persist_unit_loss
 │                   └─ aggregate_generation_score → _gate_with_regression → evaluate_gate
 │         └─ (opt) confirm_promotion_with_evidence  # BT pre-gate + defer→replicate
      ├─ resolve_field_verdict (evolve/gate.py)   # gate → FieldVerdict
      │    └─ confirm_crowning_holdout → integrity blocks → operator overrides
      └─ settle_field_round (evolve/settlement.py)  # decide → EvolveRoundOutcome
           ├─ _record_field_tournament   # frontier row, envelope, durable record
           ├─ _build_field_settlement    # one OutcomeRecord per challenger
           ├─ _commit_field_settlement   # outcomes, lineage, marker, journal
           └─ _close_field_round         # epilogue, round close, summary
                └─ (opt) _maybe_run_placebo_arm_gauntlet  # never advances champion
```

Two phases can end the round early, and each returns an outcome that is already
persisted: a field in which no candidate applied, and a round the endpoint-outage
circuit deferred. Every other phase runs on the post-holdout, post-integrity,
post-override truth, so no durable store can describe a crowning the champion
pointer contradicts.

The gauntlet is the one-matchup case of this topology:
`GauntletStrategy` schedules exactly one champion-versus-challenger matchup.
It uses the same driver, canonical runner, and evidence gate as every wider
structure, and the same settlement steps afterwards (persist the outcome, the
lineage entry, the champion marker, and the journal record). Field width one
differs in two rules, both stated in `evolve/field_candidates.py`: a
single slot that exhausted its proposer retries settles as a
validation-rejection round, and the random-baseline placebo arm runs as a
separate duel after settlement instead of riding inside the slate.

---

## 6.1 The board unit and the reserved replicate ladder

Everything in this chapter is built on ONE quantum. From the module docstring
that owns it:

```python
A **board unit** is the atomic, contract-fixed quantum
``(generation_id, board_entry_id, replicate_index)``. Under a fixed
contract its result is immutable, so it must be evaluated AT MOST ONCE
and reused everywhere — every pairing, every round, every structure, the
gate, and later evolve rounds.
```
— `src/zicato/tournament/unit_cache.py` (module docstring)

The harness session has that scope and no wider: one generation × entry ×
replicate. It never spans the board. A workflow that intentionally needs
state across several turns is represented as one compound entry, whose turns
share the run session; separate entries and replicates remain isolated.

Read that literally. A generation is immutable and belongs to exactly one
epoch/contract; a board entry is fixed by the contract; a replicate index
selects one noise draw. So the tuple names a value that can be computed once
and cached forever *within the epoch* — and a different contract is a fresh
epoch with fresh generation ids, a natural cache miss (no cross-contract
reuse). This is **the evaluate-once rule**, and it is why the champion is scored once per
epoch instead of once per round, why a competitor's board run is reused across
every pairing of a swiss/elim field, and why crash-resume is nearly free.

### 6.1.1 The reserved replicate ladder

The replicate index is not only "which noise draw" — it is also a **namespace**.
Several subsystems need to run *extra* draws of a pair without reading or
clobbering the canonical sample the tournament already scored. They each get a
reserved base far above the duel range, so the per-unit cache slots can never
collide. This table is the single source of truth; memorize it before you add
any new replicated evaluation:

| Base | Constant | Owner | Why reserved |
|---|---|---|---|
| `0..` | (none — the natural range) | Real tournament duels + the `replicates` knob | Replicate `i` of a duel is slot `i`; the canonical `loss.json` is slot 0 |
| `1000` | `CALIBRATION_REPLICATE_BASE` (`zicato.tournament.calibration`) | A/A calibration draws — the champion re-run against itself to measure the noise floor | An A/A pair re-run of the champion against itself must not touch a real duel's slots — see 04-evaluation-statistics.md §4 |
| `2000..2999` | `PREFLIGHT_REPLICATE_BASE` + probe ordinal, width `PREFLIGHT_REPLICATE_SPAN` (`zicato.epoch.preflight`) | Contract pre-flight | A dry-run of the contract before the first real round; probe `j` of the degradation-signal sample draws at `2000 + j` (issue #106), and the sample may never outgrow the block |
| `3000` / `3001` | `SCREEN_REPLICATE_BASE` (+1 confirm) (`zicato.epoch.screen`) | The pre-tournament candidate screen | The best-of-N screen tries out candidates on an ephemeral tree; its confirm-before-veto re-run is `3001` — see 05-proposer.md §"5.6.2 The candidate SCREEN" |
| `4000` | `EVIDENCE_REPLICATE_BASE` (`zicato.selection.evidence_gate`) | The Bradley–Terry pre-gate's evidence duels | Each Bradley–Terry replicate draws BOTH sides fresh; a replay at slot 0 would shrink the fit's standard error by repetition (fast mode) or clobber the child's canonical `loss.json` (full mode) — the evidence-gate replicate-slot reuse case, `12-bug-casebook.md` case 8 |

The evidence-gate constant carries the whole ladder in its own docstring, which
is the canonical statement of **the disjoint-reserved-bases rule**:

```python
#: Reserved far above every sibling base so the slots can never collide:
#: real duel replicates count up from 0, A/A calibration draws at 1000
#: (:data:`zicato.tournament.calibration.CALIBRATION_REPLICATE_BASE`), the
#: contract pre-flight at 2000
#: (:data:`zicato.epoch.preflight.PREFLIGHT_REPLICATE_BASE`), and the
#: pre-tournament candidate screen at 3000
#: (:data:`zicato.epoch.screen.SCREEN_REPLICATE_BASE`; its
#: confirm-before-veto re-run at 3001).
EVIDENCE_REPLICATE_BASE: int = 4000
```
— `src/zicato/selection/evidence_gate.py`

> ⛔ NEVER add a new replicated evaluation that draws at slot 0 or at an
> already-reserved base. Pick a fresh base ≥ 5000, add it to this table, and
> add its constant next to the others. A collision means an auxiliary draw
> either *reads* a canonical sample it should not (silent contamination) or
> *writes over* one that crash-resume and `zicato repair index` key on. That is
> the class of bug the evidence-gate replicate-slot reuse case documents
> (`12-bug-casebook.md` case 8).

### 6.1.2 The replicate index reaches the harness through `context`

A seeded/deterministic harness derives its per-run noise from stable
identifiers, and the replicate index is the one identifier that distinguishes
the N otherwise-identical paired runs. It travels to the harness the same way
`generation_id`, `disable_drift`, and `judge_only` do — stamped onto each
`BoardEntry.context`, the ONE per-entry channel that survives the full runner →
args-file → subprocess-worker → `validate_board_entry` → adapter round-trip:

```python
def _stamp_replicate_index(
    board: list[BoardEntry],
    replicate_index: int,
) -> list[BoardEntry]:
    ...
    if replicate_index <= 0:
        return board
    stamped: list[BoardEntry] = []
    for entry in board:
        context = dict(entry.context)
        context[_REPLICATE_INDEX_CONTEXT_KEY] = str(replicate_index)
        stamped.append(replace(entry, context=context))
    return stamped
```
— `src/zicato/tournament/worker_transport.py`, `_stamp_replicate_index`

`replicate_index == 0` returns the board **unchanged**, preserving object
identity. Every single-replicate path — the gauntlet, the seed scoring,
replicate 0 of a replicated matchup — is therefore byte-identical to the same
path with no key stamped. A reader treats an absent key as replicate 0
(`_entry_replicate_index`). The stamping is done ONCE per replicate pass, by
`_run_replicated` (§6.5). The key must actually *reach* the harness rather than
being dropped at some boundary; a boundary that drops it is the A/A calibration
false-zero-floor case (`12-bug-casebook.md` case 3). See §6.15's worked adapter,
whose noise draw depends on the key.

> ⚠️ TRAP — `context` is a `dict[str, str]`: every stamped value is a decimal
> string, and a reader must coerce (`int(raw or 0)`) and tolerate a malformed
> value as 0 rather than raising inside a scoring run. `_entry_replicate_index`
> is the reference reader.

---

## 6.2 The unit cache — in full

The cache is not an optimization bolted onto the runner; it is the *evaluator*.
Every board unit of every structure flows through one function, and that
function is where the cache lives.

### 6.2.1 The choke point: `_run_unit_cache_first`

```python
async def _run_unit_cache_first(
    ...
    force_fresh: bool = False,
    provenance: dict[str, _UnitProvenance] | None = None,
) -> LossProfile:
    if not force_fresh:
        cached = _resolve_cached_unit(...)
        if cached is not None:
            _record_provenance(provenance, generation.id, cached=True)
            return cached

    loss = await _run_single(...)
    if config.token_ledger is not None:
        config.token_ledger.add(loss.tokens_spent)
    ...
    if is_infra_abort_cause(loss.abort_cause):
        log.info("run %s/%s r%d aborted by infra (%s); NOT caching ...", ...)
    else:
        _persist_unit_loss(...)
    _record_provenance(provenance, generation.id, cached=False)
    return loss
```
— `src/zicato/tournament/scheduling.py`, `_run_unit_cache_first` (abridged)

Its docstring states the universality plainly:

```python
    The single choke point through which EVERY board unit — champion and
    challenger, every structure (gauntlet / racing / swiss / elim /
    round-robin), every round — is evaluated. Before executing the unit
    it consults :func:`_resolve_cached_unit`:

    * HIT → the persisted per-replicate result is reused; ``_run_single``
      is NOT called (no agent run);
    * MISS → ``_run_single`` runs the unit once, and the result is
      persisted via :func:`_persist_unit_loss` so the next need is a hit.
```
— `src/zicato/tournament/scheduling.py`, `_run_unit_cache_first`

Three consequences an extender leans on:

1. **The cache is always-on.** `force_fresh` (the `--mode full` semantics) is
   the *only* bypass of the read; the default (`fast`) is simply "do not force
   fresh". Fast mode is not a separate code path — it is the cache turned on.
2. **A HIT spends nothing.** No agent run, no subprocess, no token. That is why
   the per-round token ledger's `.add(loss.tokens_spent)` is guarded by "only a
   fresh run" (§6.5.3): a cache hit above the `add` cannot double-count.
3. **Provenance is recorded either way** so the journal's cached-vs-fresh
   accounting (`_UnitProvenance`) is honest, and the champion-eval mode
   (`fast` / `fast-degraded` / `full`) can be derived from the LEFT side's
   tally (§6.5.2).

### 6.2.2 The per-replicate slot map, and the replicate-cache clobbering case

```python
    Replicate 0 maps to the canonical ``runs/<entry>/loss.json`` the
    worker writes (back-compat: existing caches, the seed champion's
    full-board scoring, and every single-replicate run land there).
    Replicate r>0 maps to a sibling ``runs/<entry>/loss.r<r>.json`` so
    the additional noise samples cache per replicate without colliding
    with the canonical file. The directory is the same per-entry run
    directory either way; only the filename varies by replicate.
```
— `src/zicato/tournament/unit_cache.py`, `_unit_loss_path`

```python
    canonical = loss_profile_path(workspace_root, epoch_id, generation_id, entry_id)
    if replicate_index <= 0:
        return canonical
    return canonical.with_name(f"loss.r{replicate_index}.json")
```
— `src/zicato/tournament/unit_cache.py`, `_unit_loss_path` (tail)

This tail is what closes the replicate-cache clobbering case
(`12-bug-casebook.md` case 1). The worker always writes *its own* replicate's
loss to the slot the runner hands it, and the runner computes that slot from
`_entry_replicate_index(entry)` — the stamped index. Without the sibling-file
scheme every replicate's worker writes `loss.json`. Replicate 5's worker then
silently overwrites the canonical replicate-0 sample that the cache, `zicato
repair index`, and crash-resume all key on, and a "replicated" duel scores the
*last* draw at slot 0 rather than an average. **The canonical-replicate-slot
rule** states the requirement directly: one replicate's write must never land on
another replicate's slot.

> ⛔ NEVER derive the loss path from anything but `_unit_loss_path` with the
> run's actual replicate index. A new caller that writes a run's loss "to
> `loss.json`" directly reintroduces the clobbering the moment that caller runs
> under replication. `_run_single` reads the index off the entry
> (`_entry_replicate_index(entry)`) so the worker never has to know its own
> replicate number.

### 6.2.3 Reads, writes, and the unreadable-is-a-miss rule

`_resolve_cached_unit` returns the cached `LossProfile` on a HIT or `None` on a
MISS. An **unreadable file is a miss rather than a crash**:

```python
    if not path.exists():
        return None
    try:
        return reducer_module.read_loss_profile(path)
    except (OSError, KeyError, ValueError, json.JSONDecodeError):
        return None
```
— `src/zicato/tournament/unit_cache.py`, `_resolve_cached_unit` (tail)

The caller re-runs the unit and re-persists, so the next need is a hit. This is
the same "missing/corrupt is a valid state, re-derive it" posture the runtime
layer takes for state files (07-runtime-and-durability.md §"The atomic-write
contract"), with one asymmetry: a *cache* file is derived and disposable, so an
unreadable one degrades to a miss, while a *canonical record* is neither derived
nor disposable, so an unreadable one raises. `_persist_unit_loss` is best-effort in the other
direction: a write failure degrades the next lookup to another (correct) MISS
rather than aborting the tournament.

### 6.2.4 Infra aborts are never cached

After the evaluate-once and canonical-replicate-slot rules, the cache rule that
matters most is **the cache-only-budget-exhaustion rule**. From
`_run_unit_cache_first`, verbatim:

```python
    # Do NOT cache an INFRA abort (a parent/supervisor kill or a worker
    # crash). Persisting its worst-case loss would make it a permanent cache
    # HIT for the rest of the epoch, poisoning this unit's score off a single
    # transient blip — only ``--mode full`` would ever re-attempt it. A
    # genuine wall-clock-budget exhaustion IS cached (re-running re-hits the
    # same budget), and a cleanly-reduced run (no abort_cause) always is.
    # Skipping the persist leaves the next need a correct MISS, so re-running
    # re-attempts the unit. The provenance still counts it as a fresh (run,
    # not reused) evaluation so the journal's fast/full accounting is honest.
    if is_infra_abort_cause(loss.abort_cause):
        log.info(...)
    else:
        _persist_unit_loss(...)
```
— `src/zicato/tournament/scheduling.py`, `_run_unit_cache_first`

The `abort_cause` field (`zicato.core.LossProfile.abort_cause`) is the whole
mechanism. It is one of:

| `abort_cause` | Meaning | Cache-eligible? | Stamped by |
|---|---|---|---|
| `None` | a clean run (no abort) | YES (always) | the worker's reducer path |
| `BUDGET_ABORT_CAUSE` (`"wall_clock_budget"`) | the worker's own cooperative `asyncio.wait_for` fired | YES — re-running re-hits the same budget | the worker (`_run`, on `budget_exceeded`) |
| `parent_kill` | the parent SIGTERM/SIGKILLed a wedged worker | NO (infra) | `_run_single` on parent kill |
| `gone_no_result` | the worker vanished (supervisor kill), no result file | NO (infra) | `_run_single` |
| `nonzero_exit:{code}` | the worker exited non-zero | NO (infra) | `_run_single` |
| `prepare_failed` | the run could not even be prepared for a subprocess | NO (infra) | `_run_single` |
| `result_unreadable` | the worker "finished" but its `loss.json` was unreadable | NO (infra) | `_run_single` |

`is_infra_abort_cause` (`zicato.core`) is the predicate that separates the two
cacheable causes (`None`, `BUDGET_ABORT_CAUSE`) from the five infra ones. The
evidence-gate replicate-slot reuse case (`12-bug-casebook.md` case 8) is the
same shape one layer up: there an auxiliary draw poisons a rating fit by reusing
a sample, and here a transient blip would poison a unit's score by being cached
as a permanent worst-case hit. Both are answered by keeping a non-signal out of
a slot.

> ⚠️ TRAP — the `_skipped_unit_loss` path (a unit a spent budget never
> launched) DOES cache, because it uses `abort_cause=BUDGET_ABORT_CAUSE`: a
> budget skip is a budget exhaustion, the one cacheable abort cause
> (`unit_cache.py`, `_skipped_unit_loss`). Do not "unify" the skip synthesis
> with the infra-abort synthesis; they cache differently by design.

### 6.2.5 Averaging replicates: `_average_losses`

When a matchup runs R>1 replicates, the per-entry losses are folded to one map
BEFORE aggregation. This is the replication primitive, and the invariant it
carries is load-bearing: **scoring never sees the individual replicates, so a
field the fold does not aggregate is DISCARDED rather than merely left
unaveraged.**

```python
        out[entry_id] = _replace(
            profiles[0],
            drift_loss=mean_drift,
            pass_fail=majority_pass,
            score=_mean_over_present([p.score for p in profiles]),
            metrics=_mean_metrics(profiles),
            metric_counts=_mean_metric_counts(profiles),
            tokens_spent=round(sum(p.tokens_spent for p in profiles) / n),
            output_chars=round(sum(p.output_chars for p in profiles) / n),
            schema_failures=round(sum(p.schema_failures for p in profiles) / n),
            per_judge_loss=_mean_per_judge_loss(profiles),
        )
```
— `src/zicato/tournament/unit_cache.py`, `_average_losses`

The rule: a field the scalar or the gate reads is aggregated; a field neither
reads carries the representative replicate (slot 0), and the docstring names
every pass-through with the reason it may be one. `dataclasses.replace` keeps
the profile shape intact, so a field added to `LossProfile` later defaults to
pass-through. A new field's treatment is therefore justified in that docstring
rather than in this chapter.

Three design choices that matter for the gate:

- the mean over `drift_loss` is what makes replication a genuine noise hedge
  (one paired run is one noise draw);
- the mean over `score` is what makes it a hedge on the axis that actually
  decides the duel. `entry_score` reads `score` BEFORE `pass_fail`, and the
  reducer populates `score` whenever an expectation fired, since a bool matcher
  yields `1.0` or `0.0` too. An unfolded `score` would therefore leave
  `mean_score`, the whole outcome term of the scalar, computed from slot 0
  alone;
- the **strict**-majority vote (`true_count * 2 > len`) keeps a flaky entry
  from "passing" on a coin flip: a 1-of-2 split resolves to `False`. This vote
  is display-only for the scalar, because `entry_score` returns the folded
  `score` before it can consult the vote; it still drives the binary `pass_rate`
  and the gate's `pass_fail` fallback for score-less aggregates.

The namespace-bearing counters (`metric_counts` and the three int scalars) are
meaned with an absent-bucket-contributes-zero divisor — the same per-run-mean
model `aggregate_namespaced_metrics` applies — so the namespace aggregate over
the fold equals the aggregate over the replicates it folded. Full field-by-field
treatment: ch.04 §7.1.

### 6.2.6 Provenance and the `champion_eval_mode`

`_UnitProvenance` is a frozen `(cached, fresh)` tally per generation, folded by
`_record_provenance`. It is **never a contract input** — pure runtime
provenance so the journal can attribute how much a fast round reused. The
runner derives `TournamentResult.champion_eval_mode` from the LEFT side's tally
(§6.5.2): `"fast"` when every left unit was cached, `"fast-degraded"` when at
least one had to run live, `"full"` when fast was not requested. It carries no
weight in the gate and is not folded into the contract hash.

---

## 6.3 THE WORKER BOUNDARY — formal spec

The worker boundary is the most subtle part of the tournament, and the part most
easily broken with no visible symptom. Every tournament run executes in its
**own OS process** — a `python -m zicato._tournament_worker` subprocess — for three
reasons stated in the transport module's header:

```python
# Every tournament run now executes in its OWN OS process: a
# ``python -m zicato._tournament_worker`` subprocess. The motivation is
# hard-enforcement of the per-run wall-clock budget. A run wedged inside
# the orchestrator process used to be un-killable without killing the
# whole ``evolve``; isolated in a subprocess it can be SIGTERM'd then
# SIGKILL'd by this parent — and, independently, by the supervisor
# watchdog keyed on the worker's own pid in ``active_runs/{run_id}.json``.
#
# A free side benefit: the Python-module-caching problem (two
# generations' source loaded into one interpreter, ``sys.modules``
# handing back the wrong one) disappears — each worker imports exactly
# one generation snapshot and then exits.
```
— `src/zicato/tournament/worker_transport.py`

The boundary is a JSON args file the parent writes and the worker re-parses.
Nothing but JSON-serializable data crosses it. Six sub-contracts make that work
(§6.3.1 to §6.3.6), and the args file that carries them is specified in §6.3.7.
Each sub-contract is enforced in code, and breaking any of them is how a
boundary-safe run turns into an opaque failure.

### 6.3.1 Closures are rejected — module-level callables only

The worker re-imports the harness / LLM callables from dotted paths. A callable
must therefore be a re-importable object; a closure cannot be. `_callable_dotted_path`
refuses one *at spawn time* with a clear error rather than letting the worker
fail opaquely:

```python
def _callable_dotted_path(fn: Any) -> str:
    module = getattr(fn, "__module__", None)
    qualname = getattr(fn, "__qualname__", None) or getattr(fn, "__name__", None)
    if not module or not qualname:
        raise ValueError(
            f"cannot derive an import path for callable {fn!r}: it has no __module__/__qualname__"
        )
    if "<locals>" in qualname:
        raise ValueError(
            f"callable {module}:{qualname} is defined inside a function "
            "(closure-local) and cannot be re-imported by a subprocess "
            "worker; pass a module-level callable instead"
        )
    return f"{module}:{qualname}"
```
— `src/zicato/tournament/worker_transport.py`, `_callable_dotted_path`

This is **the importable-worker-callable rule**. The `<locals>` check is the
guard: a closure's `__qualname__` contains `<locals>`. `_run_single` catches the
`ValueError` and records the run as `prepare_failed`, an infra abort that the
cache-only-budget-exhaustion rule keeps out of the cache, so a mis-wired
proposer callable degrades one run instead of crashing the tournament.

**The reset() pattern for module state.** Because a callable is imported fresh
in a bare interpreter, it cannot carry per-round state through a closure. Any
harness that needs cross-run state (a client, a cache) must hold it as
*module-level* state and expose a way to reset it between generations. The
proposer tools solve the analogous problem with a `ContextVar` bound per run
(05-proposer.md §"5.9.1 The contextvar binding"); a harness solves it by
reading its inputs from the args-file-threaded `context` and its own generation
snapshot, never from ambient process state. The test-fixture reference for the
worker path is `tests/_best_of_n_slate_support.py` (a module-level scripted
adapter plus a module-level scripted critic): module-level callables with
resettable module state, the pattern to copy for any deterministic harness a
subprocess must re-import.

> ⛔ NEVER pass a `lambda`, a `functools.partial` over a local, or a
> closure-captured method as a harness or role callable in a path that reaches
> the tournament. It will raise `ValueError` at spawn on the happy path — and
> if you "route around" the check, the worker fails to import it with a far
> less actionable error.

### 6.3.2 The scrubbed environment, and why flags do not cross through it

By default the worker inherits the orchestrator's full environment (byte-for-
byte unchanged). When the operator sets `scrub_worker_env`, the worker instead
gets a MINIMAL explicit env, because a mutated worker running proposer-patched
code could otherwise read every credential in the process env:

```python
_WORKER_ESSENTIAL_ENV_KEYS: tuple[str, ...] = (
    # Tool/interpreter discovery + working dirs.
    "PATH",
    "HOME",
    "TMPDIR",
    "TMP",
    "TEMP",
    # Python import path (the worker imports zicato + any dotted-path role).
    "PYTHONPATH",
    "PYTHONHOME",
    "VIRTUAL_ENV",
    # Deterministic text handling — locale changes can shift formatting and
    # default codecs, which would otherwise perturb run output.
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    # Windows interpreter bootstrap (no-op on POSIX, where the key is unset).
    "SYSTEMROOT",
    "SYSTEMDRIVE",
)
```
— `src/zicato/tournament/worker_transport.py`

The scrubbed env is composed of exactly three things: these essential keys, the
`api_key_env` NAMEs every configured model role legitimately needs
(`_api_key_env_names` — names, never secret values), and any operator-named
`worker_env_passthrough` keys. Each is copied from the source env ONLY if
present — an unset key is omitted, never invented.

The credential-threading is the subtle half. A model-spec role resolves its
credential by reading `os.environ[api_key_env]` **in the worker**, so the
scrubbed env must keep those named variables, while the secret VALUE never
crosses the boundary. The worker re-resolves it in its own interpreter
(`_resolve_role_call_llm`, §6.3.4).

**And this is the load-bearing part for a contract author: a flag crosses the
boundary through a config pin rather than through the environment.** CLI flags that shadow typed-config knobs
(`--harness-call-timeout-ms`, `--aux-call-timeout`, …) are pinned process-wide
via `zicato.config.pin_overrides`; some are consumed *inside* the worker, so
they must cross. They travel in the args file — a snapshot taken by
`_config_pins()` — and the worker re-pins them at startup:

```python
    # Re-pin the orchestrator's process-pinned config overrides (CLI
    # flags such as --harness-call-timeout-ms / --aux-call-timeout) in
    # THIS fresh interpreter, before anything calls load_config(). The
    # pins travelled in the args file — the flag-to-config bridge across
    # the worker subprocess boundary; no environment variable involved.
    # An absent / empty key (a legacy args file, or no flags pinned)
    # leaves the worker on its own defaults.
    config_pins = args.get("config_pins")
    if config_pins:
        from zicato.config import pin_overrides
        pin_overrides(config_pins)
```
— `src/zicato/_tournament_worker.py`, `_run`

> ⛔ NEVER thread a new worker-consumed flag through an environment variable.
> The env is scrubbable by contract, so an env-threaded flag silently
> disappears the moment an operator sets `scrub_worker_env`. Add it to the
> config-pin snapshot (`_config_pins`) and re-pin it in the worker — that is
> the ONE flag-to-worker bridge, and it survives the scrub because it rides the
> args file. This is **the config-pins-not-environment rule**.

### 6.3.3 The adapter spec — `worker_spec()` wins, ADK is the fallback shape

A harness adapter is serialized by `_adapter_spec`, and the resolution order is
the extensibility contract for non-ADK harnesses:

```python
    worker_spec = getattr(adapter, "worker_spec", None)
    if callable(worker_spec):
        spec = worker_spec()
        if isinstance(spec, dict):
            return spec
        raise ValueError(...)

    name = getattr(adapter, "name", None)
    entrypoint = getattr(adapter, "_entrypoint", None)
    if name != "adk" or not entrypoint:
        raise ValueError(...)
    trees = [str(Path(p)) for p in getattr(adapter, "mutable_trees", []) or []]
    return {"kind": "adk", "entrypoint": str(entrypoint), "mutable_trees": trees}
```
— `src/zicato/tournament/worker_transport.py`, `_adapter_spec` (abridged)

The order is:

- If the adapter exposes a `worker_spec()` method, its dict is used verbatim.
  The adapter knows best how to make itself re-constructible in a subprocess,
  and this is the hook §6.15 uses.
- Otherwise the ADK shape is recognized by `name == "adk"` plus the private
  `_entrypoint` attribute and the public `mutable_trees` list.

If neither path applies, `_adapter_spec` raises `ValueError` and `_run_single`
records the run as `prepare_failed`. The worker reconstructs from the spec via
`_build_adapter`, which understands two `kind`s:

```python
    kind = spec.get("kind")
    if kind == "adk":
        from zicato.adapters.adk import ADKHarnessAdapter
        entrypoint = str(spec["entrypoint"])
        raw_trees = spec.get("mutable_trees") or []
        trees = [Path(t) for t in raw_trees] if raw_trees else None
        return ADKHarnessAdapter(entrypoint=entrypoint, mutable_trees=trees)
    if kind == "import":
        factory = _import_callable(str(spec["factory"]))
        return factory(*spec.get("args", []))
    raise ValueError(f"worker cannot reconstruct adapter kind {kind!r}")
```
— `src/zicato/_tournament_worker.py`, `_build_adapter`

The `"import"` shape (`{"kind": "import", "factory": "module:callable", "args": […]}`)
is the generic non-ADK path: a module-level factory dotted path, called with
optional positional `args`. The example harness (§6.15) uses this shape. Note
the round-trip: the factory path is re-imported, so — by the same rule as
§6.3.1 — the factory must be a module-level callable, and its `args` must be
JSON-serializable.

### 6.3.4 The role worker spec — dotted vs models_role

Each LLM role (harness / auxiliary / judge) is serialized by `_role_worker_spec`
into one of two shapes:

```python
    spec = models.role(role)
    if not spec.is_empty:
        return {"models_role": spec.to_worker_spec()}
    return {"dotted": _callable_dotted_path(fallback_callable)}
```
— `src/zicato/tournament/worker_transport.py`, `_role_worker_spec`

- `{"dotted": "module:qualname"}` — an unconfigured role: the resolved
  callable's re-importable path (subject to the closure check of §6.3.1).
- `{"models_role": {…}}` — the selected named engine's secret-free spec. The
  worker resolves role inheritance before transport. It then re-resolves the
  engine with `resolve_text_call_llm` in its own interpreter, reading any
  `api_key_env` from the worker's OWN `os.environ`. That is how a model-spec
  role reaches the worker at all, since its resolved callable is a **closure**
  and cannot cross the boundary. The worker side:

```python
    dotted = spec.get("dotted")
    if dotted:
        return _import_callable(str(dotted))
    raw_role = spec.get("models_role")
    if isinstance(raw_role, dict):
        from zicato.models_config import resolve_text_call_llm, role_spec_from_dict
        return resolve_text_call_llm(role_spec_from_dict(raw_role), role=role)
```
— `src/zicato/_tournament_worker.py`, `_resolve_role_call_llm` (tail)

This is how a role escapes the closure ban: a role whose live callable is a
closure is transported as its *declarative spec* and rebuilt on the far side.
The user-emulator role crosses by the same path, so an explicit smaller emulator
engine cannot collapse back onto the evaluation engine in a subprocess.

A reasoning-aware callable (`zicato.reasoning`) follows the dotted branch when
it is operator-provided. Define it with the module-level decorator form, so that
`_callable_dotted_path` sees the decorated name rather than an inner closure,
and the worker imports that name. The wrapper's raw backend must expose separate answer
and private-reasoning channels plus a real reasoning-control switch; flattened
text cannot be repaired after crossing `CallLLM`. The complete contract and a
worker-safe example live in `docs/design/REASONING-AWARE-CALLS.md`.

### 6.3.5 The weights serde — one field-enumerating round-trip

The scoring weights cross through a single serde:

```python
    Thin delegator to :meth:`ScoringWeights.to_json` — the SINGLE,
    field-enumerating serde shared by this writer and the worker's reader
    ... Replacing the former hand-aligned field
    list with one ``dataclasses.fields()``-driven serde means adding a field
    can no longer silently desync the worker into scoring under defaults — the
    documented ``per_judge_weights`` / ``pass_rate_monotonicity_scope`` /
    ``drift_kind_aggregation`` desync class.
```
— `src/zicato/tournament/worker_transport.py`, `_weights_spec`

The worker's `_weights_from_args` delegates to `ScoringWeights.from_json`, the
exact inverse. The alternative the serde replaces is two hand-aligned field
lists, one on each side: under that arrangement a newly added weight silently
takes its default on the worker side, so the worker scores under the wrong
contract while the orchestrator believes it threaded the field.

> ✅ ALWAYS add a new `ScoringWeights` field to the dataclass and let the
> `dataclasses.fields()`-driven `to_json`/`from_json` carry it. NEVER hand-add
> a field to a serialization list on one side of the boundary — that is the
> desync class this serde closes, and it produces the worst kind of bug: a
> silently-wrong scalar that still looks like a real number.

### 6.3.6 The ephemeral checkout contract

A worker is **never** pointed at the canonical generation source tree — a stray
runtime write into it would accumulate across the whole lineage and eventually
exhaust the disk. Every run mounts a throwaway checkout, materialized by the
workspace's `GenerationStore`:

```python
    Routing: when the workspace's :class:`~zicato.epoch.genstore
    .GenerationStore` owns this generation ..., the
    checkout is delegated to
    :meth:`~zicato.epoch.genstore.GenerationStore.checkout_ephemeral` —
    the directory backend copies, the git backend checks out a per-run
    worktree (measurably cheaper). A store-unmanaged generation ... falls
    back to the same ``copytree`` mechanism the directory backend uses ...
```
— `src/zicato/tournament/worker_transport.py`, `_checkout_run_snapshot`

Two properties are load-bearing far beyond this module:

- **`ztw-snap-*` prefix + temp-dir placement.** The checkout lives under a
  fresh `ztw-snap-{run_id}-*` mkdtemp parent in the OS temp dir
  (`EPHEMERAL_SNAPSHOT_PREFIX`, re-exported here). This is the *exact* shape the
  supervisor's crash-reaper is allowed to delete — a two-language contract
  (07-runtime-and-durability.md §"`checkout_ephemeral`"; 08-supervisor.md
  §"confirmed-dead-only reaping and the `ztw-snap-` contract"). Change the
  prefix or move the checkout out of the temp dir and crashed runs leak disk
  forever.
- **The scratch dir + `SCRATCH_DIR_ENV`.** The checkout carries a sibling
  `run-scratch` dir; the worker exports its path as `SCRATCH_DIR_ENV` so a
  target routes run output OUTSIDE its own source tree. A stray write that
  ignores the scratch dir still only pollutes the *throwaway* checkout — a
  belt-and-braces second layer. When the harness returns, the worker captures
  regular files from this tree into the canonical run directory before either
  grading or checkout cleanup. Filenames need not be known in advance.

The worker mounts the checkout's `working_dir` (whose basename equals the
canonical `snapshot_root`'s basename, so `__file__`-derived paths inside the
agent look identical either way), exports the scratch dir, and drives the entry.
`_run_single` discards the whole `ztw-snap-*` parent in its `finally` — on a
clean exit, an abort, or a crash — and crash-safety does not depend on that
cleanup (the reaper handles orphans).

### 6.3.7 The args-file protocol, top to bottom

The complete args-file shape (one run), from the worker's own `_load_args`
docstring:

```python
        {
          "workspace_root": "<abs path to .zicato dir>",
          "epoch_id": "<epoch id>",
          "generation_id": "<generation id>",
          "snapshot_root": "<abs path to a per-run code-snapshot working copy>",
          "scratch_dir": "<abs path to a per-run scratch dir OUTSIDE the snapshot>",
          "entry": { ...BoardEntry as a dict (validate_board_entry shape)... },
          "adapter": {
            "kind": "adk",
            "entrypoint": "module.path:agent_symbol",
            "mutable_trees": ["<abs path>", ...]
          },
          "harness_role":   {"dotted": "pkg.module:callable"} | {"models_role": {...}},
          "auxiliary_role": {"dotted": "pkg.module:callable"} | {"models_role": {...}},
          "judge_role":     {"dotted": "pkg.module:callable"} | {"models_role": {...}},
          "sink_events_path": "<abs path to events.jsonl>",
          "loss_path": "<abs path to loss.json>",
          "result_path": "<abs path the worker writes its result JSON to>",
          "instance_id": "default",
          "seed": null,
          "harmonograf_url": ""
        }
```
— `src/zicato/_tournament_worker.py`, `_load_args`

The parent (`_run_single`) additionally threads `weights` (§6.3.5) and
`config_pins` (§6.3.2), and stamps the `generation_id` onto the serialized
entry's `context` (so a session mounted on a throwaway snapshot can still
identify which generation it is measuring — §6.15). The worker's lifecycle,
in `_run`:

1. Re-pin config (§6.3.2), export the scratch dir.
2. `validate_board_entry(args["entry"])`, resolve the three role callables.
3. **Write `active_runs/{run_id}.json` with the worker's OWN pid** +
   `pid_start_time` + `pgid` + `snapshot_path`. This is what the subprocess
   worker boundary buys: the run's worker pid, rather than the orchestrator's,
   lands here, so the supervisor can SIGKILL this one run by this one pid
   (08-supervisor.md §"pid-safety").
4. Start the `RunHeartbeatBeater` daemon thread — it bumps `last_progress`
   every ~3s and keeps beating through GIL-releasing LLM waits, so the
   supervisor's staleness watchdog does not false-positive on a slow model call.
5. Build sinks (`JSONLPersistenceSink` + optional harmonograf), stamping the
   latter with exact epoch/tournament/matchup/generation/entry/side/replicate
   session labels for filtered operator navigation; build the
   adapter, `session = adapter.load(snapshot_root)`. `load` fails CLOSED when a
   MUTABLE TREE could not be what runs (issue #110): every registered tree's
   top-level name must resolve under `snapshot_root` — already imported, or
   `find_spec`-resolvable there — and an entrypoint that lives inside a tree
   must have its `module.__file__` under it too. (An entrypoint outside every
   tree is the legitimate dependency shape and carries no such assert.) The
   worker then records the resolved `__file__` in
   `generations/{gen}/harness_load.json` — the only process that knows it.
   After the run it appends the per-tree verdicts to the same file from
   `sys.modules`: a tree imported from outside the snapshot FAILS the unit, and
   a tree no unit ever imported becomes the `tree_never_imported` loop-health
   WARNING. The orchestrator (the round log's single writer) folds the record
   into one `harness_loaded` event per generation.
6. Drive the entry under the worker's OWN cooperative `asyncio.wait_for(budget)`
   — the first of three defence lines (§6.4).
7. Close the sinks, then call `capture_run_artifacts(scratch_dir, loss_path)`.
   Capture sorts relative paths, copies only regular files without following
   symlinks, hashes the bytes, atomically replaces the replicate's artifact
   tree, and writes its manifest. It attaches the resulting `ArtifactSet` to
   `RunResult` before `evaluate_expectation`, so a predicate can grade
   arbitrary produced files. Capture is bounded at 1,000 files and 100 MiB per
   run; skipped entries and truncation are explicit manifest data.
8. Evaluate the expectation, `reduce_loss` → `loss.json`, stamp the abort
   provenance (`abort_cause=BUDGET_ABORT_CAUSE` on a budget abort), write the
   result file (atomically, tmp→fsync→replace), remove the `active_runs` file
   on a clean exit.

The result file the parent reads back:

```python
        {
          "schema": "zicato.tournament_worker.result/1",
          "run_result": { ...RunResult dict... } | null,
          "loss_profile_path": "<abs path to loss.json>",
          "runtime_ms": <int>,
          "aborted": <bool>,
          "abort_reason": "<symbolic reason or empty string>"
        }
```
— `src/zicato/_tournament_worker.py`, `_write_result`

> ⚠️ TRAP — the worker is killable by design, so "process gone + no result
> file" is a NORMAL outcome rather than a crash. The parent treats a
> missing/corrupt result file as an aborted run (§6.4). Do not add a code path
> that raises when the result file is absent — you would turn every supervisor
> kill into a tournament-aborting exception.

---

## 6.4 `_run_single` — the run lifecycle

`_run_single` (`runner.py`) is the one function that spawns a worker and turns
its exit into a `LossProfile`. It is the test suite's monkeypatch anchor
(§6.0), so it stays in `runner.py` while the rest of the tournament code lives
in sibling modules. Its seven-step sequence, and where each abort cause is
stamped:

1. **Ephemeral checkout** of the generation's snapshot (§6.3.6); a failure here
   → `prepare_failed`.
2. **Serialize** the args file (entry + adapter spec + role specs + weights +
   config pins + the ephemeral `snapshot_root`/`scratch_dir`). A serialization
   failure (a closure, a non-ADK adapter with no `worker_spec`) → `prepare_failed`.
3. **Spawn** `python -m zicato._tournament_worker <args>` with
   `start_new_session=True` (the worker leads its own process group, so the
   supervisor can group-kill grandchildren) and the composed env (§6.3.2).
4. **Wait**, bounded by `budget + _PARENT_BUDGET_GRACE_S` (30s).
5. **On parent timeout**, request a supervisor kill and wait; escalate as a
   last resort. → `parent_kill`.
6. **On clean exit**, read the result file → the `LossProfile`. A worker that
   exited non-zero (`nonzero_exit:{code}`), vanished (`gone_no_result`), or
   wrote an unreadable loss (`result_unreadable`) is ALSO an aborted run.
7. **Always clean up**: discard the checkout, remove temp files, clear any
   kill-request marker, remove a leaked `active_runs` file, fold the loss into
   the live tournament record.

### 6.4.1 The three-line kill defence, and delegating to the supervisor

The over-budget kill path does NOT escalate SIGTERM/SIGKILL itself in the normal
case — it delegates to the supervisor, the single escalator, to avoid a
parent↔supervisor race over the same pid:

```python
        except TimeoutError:
            killed_by_parent = True
            log.warning(
                "run %s exceeded budget+grace (%.0fs); requesting supervisor kill", ...)
            if rt is not None:
                state_mod, _ = rt
                try:
                    state_mod.request_worker_kill(workspace_root, run_id)
                except Exception as exc:
                    log.debug("run %s: kill-request write failed: %s", run_id, exc)
            try:
                await asyncio.wait_for(proc.wait(), timeout=config.supervisor_kill_wait_s)
            except TimeoutError:
                log.warning(
                    "run %s: supervisor did not reap the worker within %.0fs; "
                    "parent escalating as a last resort", ...)
                await _terminate_worker(proc)
```
— `src/zicato/tournament/runner.py`, `_run_single`

The three lines of defence, in order:

1. The worker's own cooperative `asyncio.wait_for(budget)`, which aborts the run
   cleanly with a budget-exceeded loss.
2. The parent's `wait_for(budget + 30s)`, then a supervisor kill request, then a
   bounded `supervisor_kill_wait_s` wait (default 20s).
3. The parent's own last-resort `_terminate_worker` (SIGTERM → 5s grace →
   SIGKILL). It fires ONLY after the whole supervisor window has elapsed with
   the worker still alive, so it never races a healthy supervisor.

See 08-supervisor.md §"The kill-request single-escalator handshake" for the
other half.

### 6.4.2 The abort-cause decision tree

```python
            if killed_by_parent:
                abort_cause = "parent_kill"
            elif result is None:
                abort_cause = "gone_no_result"
                log.info("run %s: worker gone with no result file ...", run_id)
            else:
                abort_cause = f"nonzero_exit:{proc.returncode}"
                log.info("run %s: worker exited %s ...", run_id, proc.returncode)
```
— `src/zicato/tournament/runner.py`, `_run_single`

`killed_by_parent` is checked FIRST because a parent kill can also leave a
non-zero returncode — the parent-kill provenance is the more specific and the
more actionable one (it distinguishes an honest agent infinite-loop from a
watchdog over-firing). All five infra causes flow into `_aborted_loss_profile`
(worst-case loss, `wall_clock_budget_exceeded=True`) and are NOT cached, under
the cache-only-budget-exhaustion rule.

> ⚠️ TRAP — a killed worker's `events.jsonl` almost always lacks a terminal
> lifecycle frame (it was SIGKILLed mid-call). `_run_single` appends a
> `run_aborted` line directly (`ensure_run_aborted_event`) so the dashboard
> renders an honest "timed out" panel rather than a misleading "in progress"
> cue. This is a no-op when a terminal frame is already present.

### 6.4.3 Failure-modes catalog — what the tournament logs mean

What you will observe when a tournament misbehaves, and what each observation
means. Every one of these is a NORMAL, handled outcome — none
should abort a tournament; if one does, that is the bug.

| Observation (log line) | Level | What happened | Owner |
|---|---|---|---|
| `run … exceeded budget+grace (Ns); requesting supervisor kill` | WARNING | the worker's own cooperative budget did not fire; the parent asks the supervisor to escalate | `_run_single` |
| `run …: supervisor did not reap the worker within Ns; parent escalating as a last resort` | WARNING | no supervisor attached (ad-hoc run) or it died; the parent's own SIGTERM→grace→SIGKILL fired | `_run_single` |
| `run …: worker gone with no result file (supervisor kill or crash); recording aborted run` | INFO | `gone_no_result` — the supervisor SIGKILLed a wedged worker past its deadline; a normal aborted run | `_run_single` |
| `run …: worker exited N; recording aborted run` | INFO | `nonzero_exit:N` — the worker process crashed; an aborted run rather than a tournament crash | `_run_single` |
| `run … could not be prepared for a subprocess: …` | WARNING | `prepare_failed` — a closure-local callable (§6.3.1), a non-ADK adapter with no `worker_spec`, or a disk-full checkout | `_run_single` |
| `run …: worker result loss.json unreadable: …` | WARNING | `result_unreadable` — the worker "finished" but its `loss.json` was corrupt; aborted | `_run_single` |
| `run …/… rN aborted by infra (…); NOT caching — re-running will re-attempt the unit` | INFO | an infra abort was NOT persisted, under the cache-only-budget-exhaustion rule — the next need is a correct MISS | `_run_unit_cache_first` |
| `matchup …: per-round token budget reached; skipped k/N board unit(s) …` | WARNING | the token ledger latched; remaining units recorded as budget-exceeded for both sides | `_run_board_units_full` |
| `matchup …: budget (wall-clock deadline or round token cap) reached after k/N board units; skipped m …` | WARNING | the matchup wall-clock cap (or token cap) tripped between batches | `_run_board_units_full_budgeted` |
| `matchup …: per-round token budget reached after k/N replicate slot(s); settling with the completed replicates` | WARNING | replication stopped scheduling further slots; the completed replicates average as-is | `_run_replicated` |
| `evidence pre-gate: replicate duel returned an already-audited draw (matchup_id …) — not appended …` | WARNING | the Bradley–Terry duplicate guard fired, under the distinct-draws-only rule — a replicate runner returned a duplicate draw | `confirm_promotion_with_evidence` |
| `random-baseline placebo … was PROMOTED by the gate …` | WARNING | the placebo alarm — the gate promoted a no-op; the CRITICAL `placebo_promoted` health finding will fire; the champion pointer was NOT advanced, under the placebo-never-crowns rule | `_maybe_run_placebo_arm_gauntlet` |

> ✅ ALWAYS treat a new tournament failure you introduce as a *logged, aborted
> run or a logged skip*, never a raised exception that escapes to the round.
> Subprocess isolation and the abort-cause taxonomy exist together so that one
> wedged or broken run degrades to one worst-case `LossProfile` and the
> tournament aggregates on. A `raise` that reaches `resolve_tournament` takes
> down the whole evolve round.

---

## 6.5 The board-unit schedulers

The schedulers own the concurrency fan-out — the "tournament hall" — and thread
every unit through the cache choke point. The unit of scheduling is a **board
unit** (one per board entry), and `config.parallelism` counts board units rather
than subprocesses: in full mode each admitted unit runs champion + challenger
concurrently, so `parallelism` units mean up to `2 × parallelism` run
subprocesses alive at once.

### 6.5.1 `_run_replicated` — the replication loop + index stamping

`_run_replicated` (`scheduling.py`) is the entry point `run_matchup` calls. It
runs the paired board `replicates` times, each on its own cache slot, and
averages:

```python
    replicate_index = replicate_base + replicate_offset
    left_losses, right_losses = await _run_board_units_full(
        ...
        board=_stamp_replicate_index(board, replicate_index),
        ...
        replicate_index=replicate_index,
        force_fresh=force_fresh,
        provenance=provenance,
        matchup_deadline=matchup_deadline,
        unit_semaphore=unit_semaphore,
    )
    runs.append((left_losses, right_losses))
```
— `src/zicato/tournament/scheduling.py`, `_run_replicated`

Each replicate offset keys a distinct slot (`replicate_base + replicate_offset`,
under the disjoint-reserved-bases rule), and the board is
`_stamp_replicate_index`-ed once per pass (§6.1.2).
Because each slot is cache-first, requesting R replicates when r<R already exist
runs only the missing `R − r` — replication is incremental. `replicate_base`
defaults to 0 (every tournament matchup), and the evidence pre-gate passes the
reserved `EVIDENCE_REPLICATE_BASE` so its extra draws never touch canonical
slots.

### 6.5.2 The champion-eval mode is decided PRE-run

```python
    if force_fresh:
        mode = "full"
    else:
        left_fully_cached = all(
            _resolve_cached_unit(..., generation_id=left_gen.id, ...,
                                 replicate_index=replicate_base + r) is not None
            for r in range(replicate_count)
            for entry in board
        )
        mode = "fast" if left_fully_cached else "fast-degraded"
```
— `src/zicato/tournament/scheduling.py`, `_run_replicated`

The snapshot is taken **before** any unit runs, because a MISS re-persists
immediately — reading the provenance afterward would always look cached. `full`
= fast not requested; `fast` = every left (champion) unit already cached from a
prior round / its seed; `fast-degraded` = fast requested but at least one left
unit had to run live. Only the LEFT side's provenance drives the label.

### 6.5.3 The per-round token ledger (opt-in, latching)

The `max_tokens_per_round` budget is a between-unit check, never mid-unit. The
schedulers consult `_token_budget_spent(config)` and stop *launching* once
spent:

```python
def _token_budget_spent(config: RuntimeConfig) -> bool:
    ledger = config.token_ledger
    if ledger is None:
        return False
    return bool(ledger.check_and_clip())
```
— `src/zicato/tournament/scheduling.py`, `_token_budget_spent`

`None` (the default) is always `False` with no ledger consulted, so a workspace
that has not opted in follows the same path as one with no ledger at all. A spent budget latches the ledger's
`clipped` flag (the health finding the orchestrator reads) and each remaining
unit is recorded as a budget-exceeded loss for BOTH sides (never one side of a
pair — see the `_skip_unit_side` calls in `_run_board_units_full`). Token
accounting is folded at the ONE choke point every fresh run passes through
(`_run_unit_cache_first`: `config.token_ledger.add(loss.tokens_spent)` — only
on a fresh run, so a cache hit spends nothing).

### 6.5.4 The matchup wall-clock cap and the budgeted scheduler

`matchup_budget_seconds` is a separate axis: an opt-in cap on the duel's TOTAL
board-unit wall-clock, distinct from a single entry's
`wall_clock_budget_seconds`. It bounds the failure mode where each unit is
individually under budget but their sum grinds for hours (a racing final rung).
When set, `_run_board_units_full_budgeted` launches units in board order,
`parallelism` at a time, checking the deadline (and the token budget) between
batches; once tripped, every remaining unit is a budget-exceeded loss via
`_skip_unit_side` and the cut is LOGGED at WARNING — never silently truncated.

The three schedulers share `_skip_unit_side`, whose one subtlety is that a
budget **never clobbers a good result**: a unit already in the cache costs no
wall-clock, so it is reused verbatim; only a genuine MISS is synthesized as a
budget-exceeded skip.

### 6.5.5 `_IncrementalScorer` — the live climbing standing

Each board unit calls `scorer.record(...)` the instant its runs settle, on the
same concurrency fan-out as the runs rather than in a batch after every board
finishes, so the dashboard sees the server-side `scalar` climb as the round runs. The
accumulators are lists guarded by an `asyncio.Lock` (an explicit critical
section for the read-modify-recompute-persist), and every state write is
strictly best-effort: incremental scoring must never abort a run. Under racing,
this scorer is per-DUEL; the racing STRATEGY owns the per-lane rung topology and
the orchestrator overlays the per-duel projected map onto it (a note the
scorer's own docstring spells out).

### 6.5.6 `_run_full_board_unit` — the concurrent unit, and its isolation

A full-mode board unit runs its champion and challenger **simultaneously** —
two `_run_single` coroutines under one `asyncio.gather` — and the two are safely
concurrent because everything about a run is per-`run_id`:

```python
    parent_result, child_result = await asyncio.gather(
        _run_unit_cache_first(..., generation=parent_gen, ..., side=Side.PARENT, ...),
        _run_unit_cache_first(..., generation=child_gen, ..., side=Side.CHILD, ...),
        return_exceptions=True,
    )
    # Surface a champion-side failure first, then a challenger-side one —
    # both runs have already settled (their workers + cleanup finished).
    if isinstance(parent_result, BaseException):
        raise parent_result
    if isinstance(child_result, BaseException):
        raise child_result
```
— `src/zicato/tournament/scheduling.py`, `_run_full_board_unit`

Two properties are load-bearing. **`return_exceptions=True`** keeps a failing
side from cancelling its in-flight sibling mid-subprocess (which would orphan a
worker and skip its `finally` cleanup); both sides are allowed to finish, and
only then is a champion-side failure — then a challenger-side one — re-raised.
And **nothing is shared** between the two sides: each `_run_single` spawns its
own subprocess worker, each pointed at its own distinct `ztw-snap-*` ephemeral
checkout, each writing a distinct `run_id` (`run_id_for_unit`, and the two
generations differ), so the snapshot checkout, the `active_runs` file, and the
`loss.json` are all per-side. This is why `parallelism` counts board units rather
than subprocesses: one full-mode unit is TWO concurrent workers.

> ⛔ The run id names a board unit — `(generation, entry, replicate)` — rather
> than a `(generation, entry)` pair. Build it ONLY through
> `zicato.core.workspace.run_id_for_unit`; replicate 0 returns
> `{generation_id}--{entry_id}` and `r>0` prefixes that with a reserved
> `r{index}.` marker. It keys the `active_runs` record, the supervisor's
> kill-request marker, and the run's telemetry span, so two units sharing an id
> share those artifacts and the later writer wins (issue #250). A hand-rolled
> f-string at a call site is how two units come to share an id. Every generation
> id is `v{n}` (`next_generation_id`), so no replicate-0 id can begin `r` +
> digits + `.`: the two namespaces are disjoint without reserving any board
> entry id.

The `scorer.record(...)` fold happens the instant BOTH runs of a unit settle,
BEFORE the unit returns — so a finished board's score materialises while sibling
boards are still running (§6.5.5). Folding is skipped when a side raised (the
failing unit is re-raised to the caller as a hard tournament error instead).

---

## 6.6 The gate — THE per-duel decider

`evaluate_gate` (`gate.py`) is the single accept/reject test for one duel —
**the gate-is-the-per-duel-decider rule**. Everything above it (every structure,
the Bradley–Terry pre-gate, the resolvers) reads its `GateOutcome` and
interprets it; nothing re-implements it. It applies three rungs in order, then
an optional holdout confirmation.

### 6.6.1 The scalar-margin rung

The scalar is a LOSS (lower is better), so a promotion needs the child's loss to
drop by at least `promote_margin`:

```python
    if child_scalar > parent_scalar - weights.promote_margin:
        if delta_scalar > 0.0:
            verdict = (
                f"challenger regressed: loss rose by {delta_scalar:.6f} "
                f"(champion {parent_scalar:.6f} -> challenger {child_scalar:.6f}); "
                f"a promotion needs the loss to drop by at least "
                f"{weights.promote_margin:.6f}"
            )
        else:
            improvement = -delta_scalar
            verdict = (
                f"insufficient improvement: loss fell by only "
                f"{improvement:.6f} ...")
        return GateOutcome(decision=TournamentDecision.REJECTED, reason=verdict, ...)
```
— `src/zicato/tournament/gate.py`, `evaluate_gate`

The reason distinguishes a child that improved-but-not-enough (`insufficient
improvement`) from one that got outright worse (`challenger regressed`), and
always states the real child-minus-parent delta. `promote_margin`'s default
(`DEFAULT_PROMOTE_MARGIN = 0.01`) is calibrated above the measured A/A noise
floor (04-evaluation-statistics.md §4). The supervisor's promotion-gate notary
re-derives this rung out of band (08-supervisor.md §"Promotion gatekeeping").

### 6.6.2 The pass-rate-monotonicity rung, and its scope

When `pass_rate_monotonicity` is on, the SCOPE decides what "regressed" means:

| Scope | Rule | Right for |
|---|---|---|
| `per_entry` (default) | for every entry the parent SCORED, the child's continuous score may not drop below the parent's by more than `PER_ENTRY_SCORE_MONOTONICITY_TOLERANCE` (0.02). A bool entry the parent passed (score 1.0) must still pass. | invariant / regression-suite boards |
| `aggregate` | reject only when the child's OVERALL `mean_score` fell below the parent's by more than `PASS_RATE_MONOTONICITY_TOLERANCE` (1e-9). The child may trade individual entries. | sampled evaluation boards where per-entry pass/fail is noisy |

Switching the rung off is expressed by `pass_rate_monotonicity=False` rather
than by a third scope value, so an existing contract's canonical form is
unchanged. Both branches share one reason-builder
(`_pass_rate_regression_reason`) so the gate and the holdout stay symmetric. The
continuous-score reader (`_row_score`) is the seam that keeps this rung
identical to a plain pass/fail rule on an all-bool board: a score-less row falls
back to the `pass_fail` bit.

### 6.6.3 The per-namespace-monotonicity rung

For each flagged namespace, the child's *weighted* aggregate may not have moved
in the "worse" direction. Because `aggregate_namespaced_metrics` already folds
the sign of the namespace weight into the aggregate, the check reduces to
`child_weighted > parent_weighted + tolerance` — the weight has turned every
namespace into a unified lower-is-better axis. A zero-weight namespace is
skipped (its direction is undefined). Every regressing namespace is named in the
reason.

### 6.6.4 The holdout confirmation (applied last)

If all three train rungs would promote AND the caller supplied a holdout slice,
the win must also *confirm* on the holdout — the challenger merely must not
regress:

```python
    if child_scalar - parent_scalar > weights.promote_margin:
        return (
            f"holdout_not_confirmed: holdout loss rose by "
            f"{child_scalar - parent_scalar:.6f} ...")
```
— `src/zicato/tournament/gate.py`, `_holdout_confirms`

The asymmetry is intentional: the holdout is never asked to clear the margin in
the *improving* direction, so a train-measured win that merely holds flat on the
holdout counts as a confirmation rather than a failure. This is what makes the
holdout a guard against board-memorization rather than a second, stricter
promotion bar. A failed confirmation is just another `reject` (reason
`holdout_not_confirmed`); the champion stands, and the incumbent stays protected
in the sense that only a gate promotion can displace it. Passing `None` for both holdout arguments (a small board,
or the split disabled) skips the step entirely, leaving the three train rungs as
the whole decision. See 04-evaluation-statistics.md §5 for the split mechanics
and the Ladder budget that governs *when* the confirmation counts.

> ⛔ NEVER report holdout-side deltas on the `GateOutcome`. The deltas are
> ALWAYS the train-side deltas (`evaluate_gate` computes them from
> `parent_agg`/`child_agg`), so the journal's evidence shape is identical
> whether or not a holdout was consulted. The holdout is confirmation-only; it
> never becomes the generation's reported score.

### 6.6.5 The regression-suite pre-gate

`_gate_with_regression` (`runner.py`) prefixes `evaluate_gate` with a HARD
regression-suite check when `regression_gate_enabled`: the child snapshot's own
test suite runs as a subprocess BEFORE scoring, and any failure forces
`"rejected"` regardless of how strongly the child improved on drift/pass-rate —
"a patch that breaks the snapshot's own tests cannot promote even when its
scoring signal looks perfect."

---

## 6.7 Runner entry points

| Entry point | Runs | Champion side | Holdout | Used by |
|---|---|---|---|---|
| `run_matchup` | one duel between any two generations | `left` is the nominal parent; fast mode resolves both sides cache-first | separate confirmation after strategy resolution | every production strategy |
| `confirm_crowning_holdout` | one extra holdout-slice duel | reused under fast mode | Ladder-mediated confirmation | every eligible crown |
| `run_tournament` | full paired A/B with integrated holdout | configurable cache policy | Ladder-mediated confirmation | standalone and debug callers |
| `run_fast_mode` | challenger only, against an aggregate supplied from an earlier evaluation | aggregate reused wholesale | none | standalone inline keep/discard callers |

All four return a `TournamentResult` (`runner.py`) — a frozen, JSON-serializable
value the orchestrator journals. Its fields, and which are RUNTIME provenance
(never a contract input, never in the contract hash):

| Field | Meaning | Class |
|---|---|---|
| `parent_generation_id` / `child_generation_id` | the two generations (`left`/`right` for `run_matchup`) | identity |
| `parent_agg` / `child_agg` | the two `aggregate_generation_score` dicts (TRAIN-slice) | evidence |
| `outcome` | the `GateOutcome` — decision + reason + train-side deltas | evidence |
| `per_entry_losses` | `entry_id -> (parent_loss, child_loss)` for journaling | evidence |
| `champion_eval_mode` | `"full"` / `"fast"` / `"fast-degraded"` — how the champion side was evaluated | provenance |
| `unit_provenance` | per-generation `(cached, fresh)` tally for THIS duel | provenance |
| `holdout` | the Ladder/holdout evidence block (`None` when no holdout consulted) | evidence |
| `holdout_child_scalar` | the challenger's holdout-slice scalar for the generalization gap (`None` when no holdout) | evidence |

The provenance fields "carry no weight in the gate and are not folded into the
contract hash" — they exist purely so the journal can attribute champion sample
freshness + cost per duel.

### 6.7.1 `run_tournament` — standalone full A/B

`run_tournament` runs both sides concurrently per entry. The child is
force-fresh by default, because a freshly proposed generation has no prior
evaluation. The CHAMPION is cache-read by default, because it is immutable
within the epoch and can be reused from a prior round or from its seed; the two
defaults are the `champion_force_fresh=False`, `force_fresh=True` split. The
train-slice scalar gates and steers; the holdout is threaded
separately through the Ladder governor (`_ladder_mediated_outcome`). The
`force_fresh=False` override is the crash-resume path: the per-unit `loss.json`
of an interrupted round IS the cache, so completed units cache-HIT and only
unfinished entries re-run.

### 6.7.2 `run_matchup` — canonical production duel

The structure-agnostic duel: it runs one `Matchup` between `left_gen` and
`right_gen` (champion-vs-challenger OR challenger-vs-challenger — the gate only
needs two aggregates and treats `left` as the nominal parent), honours a
`board_subset` (racing rungs) and `replicates`, then aggregates and runs the
SAME `_gate_with_regression` → `evaluate_gate`. It returns a `TournamentResult`
whose `parent_*` describe `left` and `child_*` describe `right`, so a strategy
reads `outcome.decision` and `outcome.delta_scalar` directly.
`match_id` is threaded down to every run so each persisted `LossProfile` (and
the index rows) is tagged with the matchup it ran within — per-rung attribution
in the dashboard.

### 6.7.3 `confirm_crowning_holdout` — holdout through structures

A strategy resolves a leader on the train slice and identifies the final
champion-gate duel against the reigning champion. This function adds the
shared Ladder-mediated holdout confirmation: split the
board, run ONE extra holdout-slice duel (`board_subset=holdout_ids`), feed the
train verdict + train/holdout aggregates through the shared
`_ladder_mediated_outcome` at the shared per-epoch `LadderState`. An empty
holdout returns `(train_outcome, None, None)` immediately.

### 6.7.4 One default gauntlet round, end to end (worked trace)

The default strategy requests one challenger and schedules one matchup. The
same shared pipeline used by wider structures handles it:

```
evolve_once
  strategy = make_strategy(spec, board_ids)             # GauntletStrategy
  prepared = PreparedRound(..., strategy=strategy)
  evolve_field_round(prepared)
    assemble_candidate_field
      candidate_batch = produce_candidate_batch(prepared, 1)
    execute_field_tournament
      evaluation = evaluate_tournament(
          strategy,
          request_field=applied candidate batch,
          run_matchup=canonical board-unit runner)
        strategy schedules Matchup("gauntlet", replicates=2)
        run_matchup
          ├─ resolve both competitors at replicate slots 0 and 1
          ├─ fast: cache hits are reused and missing slots execute
          ├─ full: both competitors execute freshly
          ├─ _average_losses per entry
          └─ aggregate_generation_score → evaluate_gate
        optional confirm_promotion_with_evidence
          └─ fresh paired duels at EVIDENCE_REPLICATE_BASE + j
    resolve_field_verdict
      optional confirm_crowning_holdout
      integrity checks and operator overrides
    settle_field_round
      _record_field_tournament: frontier row, live envelope, durable record
      _build_field_settlement → RoundSettlement(...)
      _commit_field_settlement
        persist outcome → invariant → lineage → champion marker → journal
      _close_field_round
        optional placebo duel; never advances champion
        epilogue, round close, and the round summary
```

Important variants:

- `replicates: 1` uses only slot zero.
- `--mode fast` reuses every existing `(generation, entry, replicate)` slot
  and executes missing slots for either competitor. Fast gauntlet evaluation
  still runs the whole board and does not release a holdout.
- `--mode full` forces both competitors fresh.
- a conservative crash resume enables cache reads so completed units are not
  repeated.
- wider structures change only the candidate count and scheduled matchup
  topology.

---

## 6.8 The selection layer — the strategy contract

`zicato.selection` wraps duels in a *structure* and crowns a winner. The
`SelectionStrategy` ABC owns *scheduling + bracket bookkeeping + champion-
advance + intra-tournament stopping* for ONE epoch's tournament. The defining
constraint is **the gate-is-the-per-duel-decider rule**, stated at the top of
the module:

```python
The defining constraint — load-bearing across every structure — is that
the **promote gate is unchanged**. ``zicato.tournament.gate.evaluate_gate``
remains the per-duel accept/reject test. The strategy NEVER re-decides a
single duel: it reads the gate's verdict (``MatchupResult.outcome``) and
interprets it per its own bracket/Swiss/racing rules. This keeps the
per-task feasibility guarantee intact for every structure.
```
— `src/zicato/selection/strategy.py` (module docstring)

### 6.8.1 The value types

| Type | Role |
|---|---|
| `Contestant` | a generation in the field: `generation_id`, `role` (`"champion"` protected incumbent / `"challenger"`), `snapshot_root` (None until applied), `experiment` (None for the champion) |
| `Matchup` | one duel to run next: `matchup_id`, `left`/`right`, `board_subset` (None ⇒ full board), `replicates`, `stage_index`, `bracket_slot`, `matchup_budget_seconds` |
| `MatchupResult` | a completed duel: `left_agg`/`right_agg`, the UNCHANGED `outcome: GateOutcome`, `left_id`/`right_id`; helpers `left_scalar()`/`right_scalar()`/`lower_scalar_id()` |
| `SelectionDecision` | the crowned outcome: `promoted_generation_id` (None ⇒ champion stands), `decision`, `reason`, the flat `matchups` audit, `crowning_matchup_id`, `standings` |
| `Standing` | one contestant's final position: `rank`, `scalar`, `wins`/`losses`, `status` (`alive`/`eliminated`/`champion`), `role` |
| `RoundRecord` / `MatchRecord` | the persisted per-round/per-match shape the dashboard renders; `MatchRecord.pending` + `live_progress` carry the in-flight view |

Two subtleties the recipe (§6.14) depends on. First, the gate treats `left` as
the nominal parent — so a challenger-vs-challenger bracket node (no incumbent)
picks its winner by `lower_scalar_id()`:

```python
    def lower_scalar_id(self) -> str:
        if self.outcome.delta_scalar < 0.0:
            return self.right_id
        return self.left_id
```
— `src/zicato/selection/strategy.py`, `MatchupResult.lower_scalar_id`

`outcome.delta_scalar` is `right − left`, so a negative delta means `right` is
better; ties keep `left` (the higher seed) as the no-improvement convention.

Second, `stage_index` and `round_index` name two different axes and must not be
confused. `stage_index` is the WITHIN-tournament stage: a bracket round, a Swiss
round, or a racing rung INSIDE one evolve round. A generation's `round_index` is
the OUTER evolve (epoch-child) round it was born in. The persisted JSON key is
`stage_index`; a reader also accepts `round_index` in the same position, so a
workspace written under that key still loads.

### 6.8.2 The lifecycle and the base's live-projection hooks

```python
    1. :meth:`field_size` — how many challengers to request.
    2. :meth:`seed` — initialise bracket state from the applied field.
    3. loop: :meth:`next_matchups` → run them → :meth:`record_result`
       until :meth:`resolved`.
    4. :meth:`champion` — the crowned :class:`SelectionDecision`.
```
— `src/zicato/selection/strategy.py`, `SelectionStrategy`

The base provides one shared live-projection surface so every structure's
in-flight `active_tournament` envelope is byte-compatible with its settled one:
`live_rounds()` = `rounds()` + (when a round is mid-flight) a single
`RoundRecord` from the per-strategy `_pending_round()` hook, whose matches carry
`winner=""` + `pending=True` (built by the shared `pending_match_record`);
`live_standings()` = the per-strategy `_live_standings()` hook. A structure that
schedules nothing pending (the gauntlet) yields exactly `rounds()` — no
special-casing in the orchestrator.

### 6.8.3 The default-replicates single source of truth

`_default_replicates` is a `ClassVar` on the base (`2` — the noise-aware
posture) that racing overrides to `1`. It is the single source of truth for the
strategy's `__init__`, each scheduled `Matchup`, the public `replicates()`
diagnostic, and the builder cost estimator via
`STRUCTURE_DEFAULT_REPLICATES`. The cost meter therefore prices the same
replicate count that execution schedules.

---

## 6.9 `evaluate_tournament` — the structure-independent walk

The driver (`selection/driver.py`) owns strategy progression and optional
evidence confirmation. Candidate access and matchup execution are injected, so
the driver is fully unit-testable with synthetic stubs:

```python
    champion, challengers = await request_field(strategy.field_size())
    strategy.seed(champion, list(challengers))
    while not strategy.resolved():
        batch = strategy.next_matchups()
        if not batch:
            break
        if on_progress is not None:
            on_progress(strategy)
        results = await asyncio.gather(*(run_matchup(m) for m in batch))
        for result in results:
            strategy.record_result(result)

    decision = strategy.champion()
    if pre_gate is None:
        return TournamentEvaluation(decision)
    confirmed, evidence = await confirm_promotion_with_evidence(...)
    return TournamentEvaluation(confirmed, evidence)
```
— `src/zicato/selection/driver.py`, `evaluate_tournament`

Two facts a strategy author relies on: **(1)** a batch runs under
`asyncio.gather` — a Swiss round or racing rung returns multiple matchups and
they run concurrently; the cross-matchup concurrency cap is a single shared
`unit_semaphore` threaded inside `run_matchup` (§6.5) rather than the driver's
concern.
**(2)** `on_progress(strategy)` is called right after a batch is scheduled (so
`strategy.live_rounds()` carries the in-flight matchups), letting the
orchestrator publish the live bracket/ladder WHILE the round runs, with
`winner=null`.

---

## 6.10 The five strategies

`make_strategy(spec, board_ids)` (`registry.py`) maps a structure token to a
fresh strategy. An unknown token raises with the valid keys listed
(defence-in-depth — the config loader already validated at load time). Any
structure constructed with `field_size == 1` degrades to gauntlet semantics
organically.

| Structure | Shape | `_default_replicates` | Notable params | Maps to (SELECTION.md) |
|---|---|---|---|---|
| `gauntlet` | 1 champion, 1 challenger, 1 full-board duel, promote-on-gate | 2 | `replicates` | degenerate single-replicate dueling bandit §6.3 |
| `single_elim` | single-elimination bracket over the field, then a champion-gate | 2 | `field_size`, `replicates` | knockout identification |
| `double_elim` | winners'/losers' bracket, then a champion-gate | 2 | `field_size`, `replicates` | double-knockout |
| `swiss` | `rounds_n` non-eliminating Swiss rounds by Copeland standing, then a champion-gate of the leader | 2 | `rounds_n`, `field_size`, `resolver`, `rating` | Copeland identification §6.2 |
| `racing` | successive-halving rungs on escalating board slices, then a final full-board champion-gate | 1 | `eta`, `board_fraction`, `rung0_board_size`, `field_size` | successive-halving / best-arm §7 |

### 6.10.1 The gauntlet as the reference implementation

The gauntlet is the minimal strategy — read it first. It schedules exactly one
matchup and reads the gate verdict verbatim:

```python
    def next_matchups(self) -> Sequence[Matchup]:
        if self._scheduled or self._champion is None or self._challenger is None:
            return ()
        self._scheduled = True
        return (
            Matchup(
                matchup_id="gauntlet",
                left=self._champion,
                right=self._challenger,
                board_subset=None,
                replicates=self._replicates,
                stage_index=0,
            ),
        )
```
— `src/zicato/selection/strategies/gauntlet.py`, `next_matchups`

```python
    def champion(self) -> SelectionDecision:
        ...
        outcome = self._result.outcome
        promoted = outcome.decision == "promoted"
        return SelectionDecision(
            promoted_generation_id=self._challenger.generation_id if promoted else None,
            decision=outcome.decision,
            reason=outcome.reason,
            matchups=(self._result,),
            crowning_matchup_id=self._result.matchup_id,
            standings=self._standings(promoted),
        )
```
— `src/zicato/selection/strategies/gauntlet.py`, `champion`

Note it NEVER re-decides: `promoted` is read straight off `outcome.decision`,
and `promoted_generation_id` is `None` when the gate rejected. That is the
gate-is-the-per-duel-decider rule and the only-promotion-advances-the-champion
rule in their simplest form. The gauntlet is permitted to leave `rounds()`
empty, and emits the canonical one-round shape anyway — the shape every other
structure degenerates to.

### 6.10.2 Swiss — the champion-gate confirmation and the internal resolver

Swiss shows the general non-gauntlet pattern: play the field, order by standing,
then confirm the LEADER against the champion with the unchanged gate:

```python
    def _maybe_final(self) -> Sequence[Matchup]:
        ...
        leader_id = self._pick_leader()
        if leader_id is None:
            self._final_scheduled = True
            return ()
        self._leader = self._by_id[leader_id]
        self._final_scheduled = True
        return (
            Matchup(
                matchup_id=self._final_match_id,
                left=self._champion,
                right=self._leader,
                replicates=self._replicates,
                stage_index=self._stage_index,
            ),
        )
```
— `src/zicato/selection/strategies/swiss.py`, `_maybe_final`

The crucial invariant: `_pick_leader` always names a NON-champion challenger.
The optional `resolver` knob (§6.12) only *proposes* an internal leader from the
duel matrix; if it names the champion or yields nothing, the default top-non-
champion standing pick wins. The resolver only proposes, and the unchanged
champion gate still decides promotion: that is **the
only-promotion-advances-the-champion rule** made concrete. The optional
uncertainty guard (`apply_uncertainty_guard`) can add a promotion-blocking
`deferred`, but can never force a promote. Standing is the Copeland score (duels
won) tie-broken by mean scalar; a bye is a free Copeland point.

### 6.10.3 Racing — board slices and per-lane live progress

Racing is the most distinct structure and the one whose `Matchup` fields
(`board_subset`, `matchup_budget_seconds`) exist for it: successive-halving over
escalating board slices. Each rung runs its duels on a `_rung_board_subset()`
slice (a fraction of the board that grows by `eta` per rung), eliminates the
worst by RANK-within-rung — best-arm identification rather than a gate verdict —
and only the FINAL full-board rung applies the champion gate. It pins `_default_replicates =
1` because its replication is intrinsic to the escalating slices, and it owns
the authoritative per-lane `live_progress` topology (§6.5.5). Its `match_id`
forms (`rung0_m2`, `racing-final`) are what `rung_for_match_id` projects to the
dashboard's rung labels. The board slice grows by `eta` per rung
(`_rung_board_size`: `base * eta**rung`, capped at the full board), the cut
keeps the top `1/eta` by rung scalar (`_apply_cut`: `keep_n = max(1,
floor(len/eta))`), and `_is_final_rung` fires when the slice reaches the full
board or one challenger remains — the only rung the gate touches.

### 6.10.4 Single/double elimination — the bracket and the champion-gate final

The elimination brackets show the second general non-gauntlet pattern: run a
bracket over the *challengers* (the champion sits out as the protected top seed
with a bye), then a final champion-vs-survivor duel that uses the real gate. The
key is that a challenger-vs-challenger node has NO incumbent, so its winner is
the side the gate prefers (the lower scalar) rather than a promote/reject
verdict:

```python
        left, right = pair
        winner_id = result.lower_scalar_id()
        winner = left if winner_id == left.generation_id else right
        loser = right if winner is left else left
        self._wins[winner_id] = self._wins.get(winner_id, 0) + 1
        self._losses[loser.generation_id] = self._losses.get(loser.generation_id, 0) + 1
        self._eliminated_round[loser.generation_id] = self._stage_index
        self._current_round.append(winner)
```
— `src/zicato/selection/strategies/single_elim.py`, `record_result`

Only the FINAL node (`_maybe_final` → `Matchup(left=champion, right=survivor,
bracket_slot="final")`) applies all three gate rungs against the reigning
champion, which the strategy docstring states as "only the final node is a true
three-rule feasibility test". The bracket slots are named `WB-R{stage}-{slot}`
(winners' bracket), and `_flush_round` labels each `"Bracket round N"` rather
than a bare `"Round N"`, so the within-tournament stage never reads as the outer
evolve round on the dashboard. Double-elim adds a losers' bracket
on the same skeleton. `replicates >= 2` is the recommended default here — "a
strong candidate dies to one unlucky run otherwise" — which is why
`_default_replicates` is 2 (§6.8.3).

### 6.10.5 The opt-in rating / resolver / uncertainty-guard layer

Three `TournamentStructure.params` knobs re-order a non-gauntlet structure's
INTERNAL standings/leader pick and can add a promotion-blocking defer, while
never touching the gate itself. All three are opt-in and cost **zero new board
runs**: they are derived entirely from the audit the strategy already
accumulated. The glue is `zicato.selection.standings_ext`, which reads the knobs
and converts the flat `MatchupResult` audit into the inputs the pure layers
consume (`audit_duels` → BT outcomes; `audit_matrix` → the resolver matrix).

| Knob | Values | Effect | Reads |
|---|---|---|---|
| `rating` | `bradley_terry` | order the standings by fitted latent strength (`rating_order`) instead of Copeland/scalar; unrated contestants sort after rated ones | `zicato.selection.rating.fit_bradley_terry` |
| `resolver` | `copeland` / `ranked_pairs` | propose the internal leader from the duel matrix (Condorcet fast path → Smith prune → the resolver) instead of the top standing | `zicato.selection.resolve.resolve_leader` (§6.12) |
| `uncertainty_gate` | a float in `(0,1)` | DEFER a gate-promotion whose `P(theta_child > theta_parent)` fails to clear the bar — the crowning win is within rating noise | `apply_uncertainty_guard` |

The rating backbone (`rating.py`) is a pure Bradley–Terry maximum-likelihood fit
over the pairwise outcomes — a convex problem with a single global optimum, solved by a
small pure-Python Newton step with an L2 ridge prior. The prior is what keeps
the translation-invariant likelihood identifiable AND keeps a contestant with a
perfect/empty record at a finite strength; it also guarantees the Fisher
information is positive-definite, so the standard error is always finite. That
SE is the operational payoff: `prob_stronger(theta_a, se_a, theta_b, se_b)`
treats the two strengths as independent normals and returns `P(a > b)` — the
quantity both the `uncertainty_gate` guard and the full BT pre-gate (§6.11)
threshold. The uncertainty guard's whole contract, from the code:

```python
    The guard can ONLY block a promotion; it never turns a reject into a
    promote. So the protected-incumbent invariant strictly strengthens — the
    worst case is a deferred (not promoted) challenger.
```
— `src/zicato/selection/standings_ext.py`, `apply_uncertainty_guard`

Note the two uncertainty layers: `uncertainty_gate` (a single-shot yes/no guard
in `standings_ext`) vs the full BT pre-gate of §6.11 (a defer→replicate→refit
schedule with a genuine `inconclusive` terminal). The pre-gate is the raised,
richer form; both share `fit_bradley_terry`/`prob_stronger`, and both can only
ever HOLD a promotion.

> ⚠️ TRAP — `resolver`, `rating`, and `uncertainty_gate` all read defensively
> from the opaque `params` map: an absent, unrecognised, or out-of-range value
> returns `None` and leaves the strategy on its unchanged default path. Keep
> that discipline in any new knob — `params` is operator-supplied, so a knob
> that raises on a bad value turns a typo into a crashed tournament.

---

## 6.11 The Bradley–Terry evidence pre-gate + dead-letter

The pre-gate (`evidence_gate.py`, driven from `driver.py`) is an opt-in device
that crowns on accumulated evidence rather than on a single point estimate. It
is **off by default**, and it buys soundness at the cost of power. Its own
docstring is blunt about the tradeoff:

```python
* :func:`evidence_verdict` fits BT over the strategy's already-measured duel
  audit and returns one of three verdicts for the crowning pair:

  - ``"promoted"`` — ``P(theta_child > theta_champion) >= threshold`` AND the
    two rating CIs are *separated* (no overlap). Crown on evidence.
  - ``"deferred"`` — the probability bar is unmet OR the CIs still overlap, and
    there is replicate budget left to spend. Hold and replicate.
  - ``"inconclusive"`` — the budget is exhausted and the CIs still overlap. A
    terminal state recorded in the dead-letter queue
    (:mod:`zicato.selection.dead_letter`); nothing is silently dropped.
```
— `src/zicato/selection/evidence_gate.py` (module docstring)

The pre-gate is consulted only on a gate `"promoted"`; a reject or defer passes
straight through. **The pre-gate can hold a promotion and can never force one**, under
the only-promotion-advances-the-champion rule, which the pre-gate therefore
strictly strengthens. A fit is trusted only above
`MIN_CREDIBLE_DUELS = 3` resolved duels for the pair (the Fisher-information SE
blows up below that); below the minimum the verdict is `credible=False` and the
gate's own decision stands.

### 6.11.1 The defer→replicate→inconclusive loop, and the duplicate refusal

The crowning confirmation used by every selection strategy is
`confirm_promotion_with_evidence`. Its loop refits after each replicate and
refuses duplicate draws:

```python
        extra = await replicate_duel(candidate.left_id, candidate.right_id)
        replicates_spent += 1
        if extra.matchup_id in seen_matchup_ids:
            log.warning(
                "evidence pre-gate: replicate duel returned an already-audited "
                "draw (matchup_id %r) — not appended to the Bradley--Terry "
                "audit; identical data must never separate CIs",
                extra.matchup_id,
            )
            continue
        seen_matchup_ids.add(extra.matchup_id)
        audit.append(extra)
```
— `src/zicato/selection/driver.py`, `confirm_promotion_with_evidence`

This is **the distinct-draws-only rule**, and the centre of the evidence-gate
replicate-slot reuse case (`12-bug-casebook.md` case 8). Each evidence replicate runs
the crowning pair at `EVIDENCE_REPLICATE_BASE + j` (both sides drawn fresh,
never a cache replay), encoding that index in the matchup id
(`bt-replicate:r{index}:{left}:{right}`). The `ReplicateDuel` CONTRACT is that
every call returns an INDEPENDENT fresh draw under a matchup id unique within
the audit. The driver refuses to append a result whose id already appears —
identical data re-presented to the fit would shrink the BT standard error by
repetition alone, letting duplicate duels "separate" CIs without new evidence.
The spend is counted regardless (the budget bounds duels RUN), so a runner that
keeps replaying one draw cannot loop forever.

Two terminal folds (`_finalize`): a credible `"promoted"` keeps the crown; an
`"inconclusive"` maps to the closed enum's `DEFERRED` token ("kept for analysis,
lineage head unchanged"), fires `on_inconclusive`, and the champion stands. A
`credible=False` pass-through returns the original decision verbatim.

### 6.11.2 The dead-letter queue

An inconclusive terminal is not silently dropped: `record_inconclusive`
(`dead_letter.py`) writes one `runtime/inconclusive/<gen>.json` record (atomic,
via `atomic_write_json`) carrying the final `gate.rating` block + the per-refit
CI history + the reason, so an operator and the dashboard can see which
challenger could neither be crowned nor cleanly rejected, and on what evidence.
It is an additive runtime artifact: it exists ONLY on a run that opted into the
pre-gate AND reached the inconclusive state, so every other run's runtime tree
holds no such record. `read_inconclusive` /
`list_inconclusive` are tolerant readers (absent ⇒ `None`/`[]`).

> ⚠️ TRAP — the pre-gate's `threshold` and `replicate_budget` live in the opaque
> `TournamentStructure.params` map rather than on `ScoringWeights`, because an
> absent param adds nothing to the contract's canonical form. Adding the
> pre-gate to the codebase therefore rolls no existing epoch's hash, and a
> contract that does not opt in leaves the whole parity surface unchanged
> (`read_promote_confidence_threshold` returns `None`).

---

## 6.12 `resolve.py` — winner resolution over a duel matrix (propose-only)

When a structure's field is a noisy, possibly-**cyclic** duel matrix, the
resolvers (`resolve.py`) turn it into a single proposed winner, principled under
cycles. Every function here is **pure** and **propose-only** — its output only
ever proposes an internal leader; the unchanged champion-gate still owns
promotion:

```python
Every function is **pure** — it reads a frozen matrix and returns a value;
no strategy state, no IO, no external numerical dependency. The output only
ever *proposes* an internal leader; the unchanged champion-gate still owns
promotion. A resolver may name the wrong leader and the worst case is a
wasted confirmation duel — never an unsafe promotion.
```
— `src/zicato/selection/resolve.py` (module docstring)

| Function | What it computes | Cost |
|---|---|---|
| `condorcet_check` | the contestant who beats every other head-to-head, or `None` | O(n²) fast path every method collapses to |
| `smith_set` | the smallest dominant set (top cycle) — a front prune | O(n²)-ish |
| `ranked_pairs` | Tideman's margin-sorted lock/skip, with an auditable `trace` of which edges were locked and which were skipped to avoid a cycle | polynomial |
| `copeland_order` | best-first by Copeland score (wins − losses) | O(n²) |
| `resolve_leader` | the dispatch: Condorcet fast path → Smith prune → `ranked_pairs` or `copeland` | — |

`build_matrix` aggregates replicated and conflicting verdicts by **net margin**:
a pairing both sides have "won" nets to whichever accumulated the larger total
margin, so the strongest, most-separated verdicts dominate a noisy measurement.
A pairing that nets to zero is recorded as *no edge* — an honest "unresolved tie" the resolvers treat
as a missing comparison. `resolve_leader` is what a strategy's `resolver` param
(§6.10.2) routes through; the returned leader is always fed to the champion gate
before any promotion. This is the layer SELECTION-THEORY.md §5 describes; the
rating/Bradley–Terry layer (§6.11) sits above it.

> ⛔ NEVER let a resolver's output promote anything directly. The resolvers name
> a *leader to confirm* rather than a *generation to crown*. Every strategy runs the
> leader through the unchanged champion gate; the worst a wrong resolver
> proposal can cost is one wasted confirmation duel. This is the
> only-promotion-advances-the-champion rule applied to the resolvers.

---

## 6.13 The placebo duel

The placebo arm (`evolve/placebo.py`, wired in `orchestrator.py`) is the
random-baseline control of the anti-overfitting program
(`docs/design/OVERFITTING.md`). Every Nth round
(`overfitting.random_baseline_every_n`, default off) the orchestrator fields ONE
extra challenger whose patch is a **semantics-preserving no-op**: the first
enumerated mutation point's current value re-emitted unchanged. The baseline
tree behaves identically to the champion, so under a working decision procedure
the gate MUST reject it, because no improvement can clear `promote_margin`
between identical behaviours. The arm therefore *measures the gate itself*. A
rejected placebo is evidence that the gate discriminates; a **promoted** placebo
raises the CRITICAL `placebo_promoted` loop-health finding.

### 6.13.1 The placebo arm never advances the champion

On the gauntlet path the placebo is an EXTRA scheduled duel *after* the round.
Its id is `{vN}-placebo`, outside the `vN` form, so round numbering and id
minting are untouched, and its lineage record is ALWAYS a dead branch:

```python
        # Lineage: ALWAYS a dead branch. Even a (pathological) promoted
        # verdict never advances the champion pointer — the arm measures
        # the gate; the alarm is the health finding, not a crowning.
        append_to_lineage(
            workspace_root,
            epoch_id,
            replace(challenger.generation, promoted=False),
            parent_id=parent_id,
        )
```
— `src/zicato/orchestrator.py`, `_maybe_run_placebo_arm_gauntlet`

On a multi-challenger field the placebo enters as one extra slate slot (id
`v{base_n + field_n}`) and flows through the unchanged strategy + gate, but the
same rule holds: it never crowns. The whole arm is best-effort — any failure
never aborts the round.

### 6.13.2 The placebo is filtered out of the optimization stream

A placebo experiment's hypothesis `core_idea` is prefixed with
`PLACEBO_HYPOTHESIS_MARKER = "[placebo:random-baseline]"`, and the loop-health
detectors split it out so an always-rejected control fielded every Nth round is
never read as a stall, a flat-scoring window, or a mined-out contract:

```python
    placebo_experiments = [exp for exp in experiments if _is_placebo_experiment(exp)]
    if placebo_experiments:
        experiments = [exp for exp in experiments if not _is_placebo_experiment(exp)]
    findings: list[HealthFinding] = []
    findings.extend(detect_degenerate_scoring(experiments, health))
    ...
    findings.extend(detect_placebo_promoted(placebo_experiments))
```
— `src/zicato/health/diagnostics.py` (abridged)

The split feeds one detector the real stream never sees
(`detect_placebo_promoted`, the gate-discrimination alarm), and with the knob
off there are no placebo records and the split is the identity.

> ⛔ NEVER route the placebo through the champion-advance path, and NEVER let it
> into the optimization-stream detectors. Both would defeat the arm: a promoted
> placebo that advanced the pointer would crown noise, and a placebo counted as
> a real experiment would poison every stalled-loop and flat-scoring finding.
> The marker prefix and the dead-branch lineage are the two enforcement points
> for the placebo-never-crowns rule.

---

## 6.14 Recipe: Add a tournament structure

Scenario: you want a new `"round_robin"` structure — every contestant duels
every other, Copeland-ranked, then a champion-gate of the leader. Nine steps.

**Step 1 — the strategy class.** Add `src/zicato/selection/strategies/round_robin.py`
subclassing `SelectionStrategy`. Set the two ClassVars and resolve `replicates`
in `__init__` against the base default:

```python
class RoundRobinStrategy(SelectionStrategy):
    structure = "round_robin"
    _default_replicates = 2          # inherit the noise-aware default

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        super().__init__(params)
        self._replicates = max(1, _param_int(self.params, "replicates", self._default_replicates))
        ...
```

Implement the six abstract methods (`field_size`, `seed`, `next_matchups`,
`record_result`, `resolved`, `champion`). Copy the gauntlet for the trivial
parts and the swiss for the champion-gate-of-the-leader pattern (§6.10.2). Two
rules you cannot break:

- **Read the gate verdict; never re-decide.** Interpret
  `result.outcome.decision`, `result.outcome.delta_scalar`, and
  `result.lower_scalar_id()`; do not compute your own accept/reject. This is the
  gate-is-the-per-duel-decider rule.
- **The crowning duel's `left` is the champion and `right` is the leader**, and
  `_pick_leader` must return a non-champion, so a resolver only proposes. This
  is the only-promotion-advances-the-champion rule.

**Step 2 — register it.** Add it to `STRATEGY_REGISTRY` in `registry.py`:

```python
STRATEGY_REGISTRY: dict[str, type[SelectionStrategy]] = {
    GauntletStrategy.structure: GauntletStrategy,
    ...
    RoundRobinStrategy.structure: RoundRobinStrategy,
}
```

`STRUCTURE_DEFAULT_REPLICATES` derives from each class's `_default_replicates`
automatically — you get the default-replicates single source of truth for free
(§6.8.3). Also add `"round_robin"` to
`zicato.core.types.VALID_TOURNAMENT_STRUCTURES` so the config loader validates
the token at load time.

**Step 3 — default replicates.** If your structure's replication is intrinsic
(like racing's escalating slices), pin `_default_replicates = 1`; otherwise
inherit `2`. Do NOT hardcode a replicate count anywhere else — every consumer
reads the ClassVar (§6.8.3).

**Step 4 — builder paramSpecs.** Add your params (`replicates`, and any
structure-specific knob) to the builder's per-structure paramSpec table
(`model.js::paramSpecsFor`) so the GUI can render honest fields and the CLI can
document them. The full-coverage rule for a new knob names every place a knob
must appear (10-builder-cli-library.md §10.7). An unspecced param is invisible
to operators.

**Step 5 — the cost meter.** The builder cost estimator multiplies board size ×
`field_size` × `default_replicates_for(structure)` × the structure's own
matchup count. If your structure schedules a non-obvious number of duels
(round-robin is `n·(n−1)/2` + 1 crowning), teach the estimator your matchup
count. An estimator that does not know your matchup count under-reports the
cost, which is the failure `STRUCTURE_DEFAULT_REPLICATES` closes for the
replicate factor — see `registry.py`'s comment on the swiss/elim default of 2.
Give each new cost line a draft fixture in
`tests/test_builder_cost_envelope_correspondence.py`, whose coverage assertion
reds until one reaches the line.

**Step 6 — the gate-owns-decisions invariant.** Re-read your `record_result` and
`champion`, and assert in a test that with a gate that always REJECTS your
`champion()` returns `promoted_generation_id=None`, and that with a gate that
PROMOTES the leader it returns the leader's id. Nothing else may promote.

**Step 7 — the strategy's own tests.** Nothing picks a new structure up
automatically: no test is parametrized over all structures. Write cases in the
three files that drive each strategy directly —
`tests/test_selection_strategies.py` (the strategy itself),
`tests/test_orchestrator_selection.py` (its orchestrator wiring), and
`tests/test_live_tournament_structure.py` (its in-flight projection). Cover at
least: `field_size() >= 1`; that `seed` followed by the
`next_matchups`/`record_result` loop terminates; that `resolved()` eventually
returns true; that `champion()` returns a well-formed `SelectionDecision`; and
that the live-projection hooks (`live_rounds`/`live_standings`) produce shapes
compatible with the settled ones.

**Step 8 — the dashboard structure-view.** A structure renders its
`rounds()`, `standings`, and `_pending_round()` in the dashboard's bracket view.
Emit `RoundRecord`/`MatchRecord` with stable `stage_index`, `label`, and
`bracket_slot` so the renderer groups matches correctly, and use
`pending_match_record` for the in-flight round so live and settled envelopes
match. The server-side fold the bracket figures render is
09-dashboard-and-query.md §9.2.5.

**Step 9 — holdout through the structure.** If your structure resolves a leader
and identifies its crowning matchup, the shared round pipeline applies
`confirm_crowning_holdout` (§6.7.3). Test the structure's crowning id and
train/holdout behavior; do not add a structure-specific confirmation path.

**Verify**

```bash
uv run pytest tests/ -q -k "selection or strategy or conformance"
uv run pytest tests/test_selection_strategies.py tests/test_orchestrator_selection.py -q
uv run zicato --help | grep -A2 structure                # the token is documented
```

---

## 6.15 Recipe: Make a harness adapter

Scenario: you want a deterministic, LLM-free harness so the full evolve loop
(propose → apply → subprocess tournament worker → reduce → gate) is exercised
with a scalar that is an exact, hand-computable function of the code under
evaluation. The worked example lives at
`examples/zicato_examples/target_0_convergence/harness.py`
(`DeterministicPolicyAdapter`); mirror it.

**Step 1 — the `RunnableHarness` session shape.** Your adapter's `load(generation_root)`
returns a *session* implementing the rich `run(entry, sinks, config) -> RunResult`
shape (the worker dispatches on the signature; a two-argument
`run(entry, sink_path)` stub is detected by parameter name). Emit real goldfive lifecycle frames through
`sinks` so the REAL reducer computes the loss from a real events file — no
telemetry stubs:

```python
    async def run(self, entry: Any, sinks: Any, config: Any) -> RunResult:
        started = time.monotonic()
        run_id = _run_identifier(entry)

        policy_path = self._generation_root / POLICY_RELPATH
        try:
            policy_source = policy_path.read_text(encoding="utf-8")
        except OSError:
            policy_source = ""
        tokens = self._measured_tokens(
            entry, config, parse_style_tokens(policy_source), policy_source
        )
        final_output = synthesize_output(str(getattr(entry, "input", "") or ""), tokens)
```
— `examples/zicato_examples/target_0_convergence/harness.py`, `_PolicySession.run`

**Step 2 — read YOUR OWN `generation_root`.** This is the single most important
adapter rule. `load` captures the generation root it is handed — the worker's
per-run ephemeral snapshot copy — and the session reads the code under
evaluation from THAT root, so the output is a pure function of the generation
being measured:

```python
    def load(self, generation_root: Path) -> _PolicySession:
        return _PolicySession(generation_root)
```
— `examples/zicato_examples/target_0_convergence/harness.py`, `DeterministicPolicyAdapter.load`

The example parses `agent/policy.py` from its own root with `ast` (NEVER imports
it — the snapshot is untrusted, proposer-patched code) and synthesizes output
from the remaining defect tokens. Reading from anywhere else — a hardcoded path,
`__file__`, ambient state — measures the wrong generation.

> ⛔ NEVER import the snapshot under evaluation. It is proposer-patched,
> possibly-broken code; a destructive patch that survives validation must still
> score (badly) rather than crash the worker. Parse it (`ast.parse`), read its files,
> or shell out to it in a subprocess — but do not `import` it into the worker's
> own interpreter.

**Step 3 — the `worker_spec()`.** Because the worker reconstructs the adapter in
a bare interpreter, expose `worker_spec()` returning a re-buildable spec — the
`"import"` shape with a MODULE-LEVEL factory (§6.3.3):

```python
    def worker_spec(self) -> dict[str, Any]:
        return {
            "kind": "import",
            "factory": "zicato_examples.target_0_convergence.harness:make_adapter",
        }
```
— `examples/zicato_examples/target_0_convergence/harness.py`, `DeterministicPolicyAdapter.worker_spec`

`make_adapter` is a module-level factory, as the importable-worker-callable rule
requires. The noisy variant carries its sigma through the spec's `args` payload,
so the subprocess reconstructs an adapter with the SAME configuration: the noise
level travels in the adapter's own declaration rather than in ambient process
state. This is the same shape `zicato.adapter_factory.make_adapter_from_config`
accepts in `config.json`, so the workspace declares the adapter honestly and the
worker rebuilds the identical object.

**Step 4 — goldfive frame emission.** Emit `run_started` → one `drift_detected`
per defect → `run_completed` through the sink list, guarded on goldfive being
importable so the adapter degrades (no frames, zero drift) in a stripped
environment. The example constructs frames directly on the proto so the
`kind`/`severity` land on the wire exactly as the reducer's normalizer expects:

```python
    evt = new_event(run_id, sequence)
    evt.drift_detected.kind = types_pb2.DriftKind.Value("DRIFT_KIND_UNEXPECTED_OUTPUT")
    evt.drift_detected.severity = types_pb2.DriftSeverity.Value("DRIFT_SEVERITY_INFO")
    evt.drift_detected.detail = f"planted defect token: {token}"
    return evt
```
— `examples/zicato_examples/target_0_convergence/harness.py`, `_drift_event`

**Step 5 — scratch-dir discipline.** If your harness writes any runtime output,
route it to `SCRATCH_DIR_ENV` (the per-run scratch dir the worker exports —
§6.3.6), never next to your own code. The ephemeral checkout is the belt; the
scratch dir is the braces — but a well-behaved adapter uses the scratch dir so
nothing ever lands in the snapshot copy at all.

**Step 6 — a stable run id, and the noise seed from stable identifiers.** A
seeded or deterministic harness must derive its per-run identity and noise from
STABLE identifiers rather than from the ephemeral snapshot path, which is a
throwaway `ztw-snap-*` name. Recover the generation id and replicate index from the
`entry.context` keys the runner stamped (§6.1.2), and build a run id unique per
`(generation, entry, replicate)`:

```python
    context = dict(getattr(entry, "context", {}) or {})
    generation = str(context.get(GENERATION_ID_CONTEXT_KEY, "") or "")
    ...
    parts = ["conv"]
    if generation:
        parts.append(generation)
    parts.append(str(entry.id))
    if replicate:
        parts.append(f"r{replicate}")
    return "-".join(parts)
```
— `examples/zicato_examples/target_0_convergence/harness.py`, `_run_identifier`

An id built from the entry alone, such as `conv-<entry>`, repeats across
generations and replicates, so the index's `runs` rows (PRIMARY KEY `run_id`)
silently overwrite one another as the lineage advances and only the last
generation's runs survive. Recovering the generation and the replicate makes the
id a pure function of the run's coordinate. The noisy session
(`_NoisyPolicySession._measured_tokens`) seeds its random number generator from
`stable_noise_seed(workspace_seed, generation, entry_id, replicate_index)`. That
seed is the whole reason `_stamp_replicate_index` exists (§6.1.2), and it is the
axis of the A/A calibration false-zero-floor case (`12-bug-casebook.md` case 3):
if the replicate index never reaches the harness, every replicate draws the
identical sample and the measured noise collapses to zero.

**Verify**

```bash
uv run pytest tests/ -q -k "convergence or adapter"
# Drive one real subprocess round end-to-end (the deterministic policy has a
# hand-computable scalar, so a wrong mounted tree is detected by arithmetic):
uv run pytest tests/test_best_of_n_tree_integrity.py -q
```

---

## 6.16 Cross-references

- 04-evaluation-statistics.md — the scalar the gate compares, the A/A noise
  floor `promote_margin` is calibrated against, the train/holdout split and the
  Ladder budget that governs when a holdout confirmation counts.
- 05-proposer.md — where the challenger tree comes from. §"5.6.5 Mounting the
  chosen candidate" is the seam that enforces the
  mounted-tree-matches-the-chosen-candidate rule; §"5.6.2 The candidate SCREEN"
  owns replicate base 3000.
- 07-runtime-and-durability.md — `checkout_ephemeral` and the `ztw-snap-`
  contract; the atomic-write contract behind `loss.json`; the store inventory's
  per-run cache row; the generation store `derive_generation` all-or-nothing.
- 08-supervisor.md — the kill-request single-escalator handshake (`_run_single`'s
  supervisor delegation); confirmed-dead-only reaping of `ztw-snap-*` orphans;
  the promotion-gate notary that re-derives the scalar-margin rung out of band.
- 10-builder-cli-library.md — the structure paramSpecs and the cost meter the
  §6.14 recipe threads.
- 11-testing.md — the genstore conformance suite;
  `tests/test_best_of_n_tree_integrity.py` (the subprocess-worker end-to-end
  test).
- 12-bug-casebook.md — the cases this chapter's invariants close:
  - the replicate-cache clobbering case (case 1), closed by the
    canonical-replicate-slot rule;
  - the A/A calibration false-zero-floor case (case 3), which §6.15 traces to
    the replicate seed reaching the harness;
  - the best-of-N and field-path tree mismatch cases (cases 6 and 7), closed by
    the mounted-tree-matches-the-chosen-candidate rule;
  - the evidence-gate replicate-slot reuse case (case 8), closed by the
    disjoint-reserved-bases and distinct-draws-only rules;
  - the git re-derive and contract-hash checkout-path cases (cases 9 and 10),
    which the ephemeral-checkout path touches.
- `docs/design/TOURNAMENT-STRUCTURES.md`, `docs/design/SELECTION.md`,
  `docs/design/SELECTION-THEORY.md` — the design record for the strategy layer,
  the resolvers, and the rating tier.
