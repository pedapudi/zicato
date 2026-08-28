# zicato: intent, architecture, determinism, and the proposer

_Produced by a multi-agent read of the zicato codebase — six independently verified sections, assembled and de-duplicated by an editor pass. Every code snippet below is copied verbatim from the file named in its caption._

_Analysis date: 2026-07-01_

## Executive abstract

zicato is a self-improving **meta-harness** for any system whose behaviour can be measured — multi-agent systems are its founding and primary use case, not its definition. It wraps a system you already built, runs it against a frozen board of tasks, reduces each run's structured runtime telemetry (a `goldfive.v1.Event` stream) into a typed per-run loss profile, aggregates those into recurring failure **patterns**, asks an LLM **proposer** to emit typed **patches** against an annotated mutation surface, and then runs a **tournament** that promotes the candidate generation only when it wins by a configured margin without regressing. The architecture is a Python orchestration loop (`src/zicato/`, ~85k LOC across 235 files) whose safety is enforced out-of-band by an independent Rust supervisor (`crates/supervisor/`, ~14.5k LOC) that never shares memory with the loop and only reads atomic state files. The codebase is large less because it does many things than because it is written to be *legible and self-auditing*: an AST/token census puts only ~47% of `src/zicato/` lines at executable code, with ~40% docstrings and comments (31% + 9%) carrying the design rationale inline, and nearly every capability ships as a byte-identical-by-default opt-in wrapped in defense-in-depth checks. That prose is mostly merited — but a [code-quality audit](#code-quality-is-the-verbosity-merited) found the *reducible* debt is structural, not textual: three god-functions (~3,000 lines between them), duplicated evolve/dashboard logic, and a handful of dead symbols.

The deterministic guarantees are concentrated in the supervisor and the Python single-writer state contract: warn-only heartbeats (the watchdog can never kill the orchestrator), pid-reuse-proof signalling, an untrusted-and-clamped run deadline, a tamper-evident hash-chained audit ledger, diff-containment re-hashing of every child snapshot, an independent re-derivation of the promotion gate, atomic tmp→fsync→rename writes, a pid-start-time-checked workspace lock, and a conservative crash-resume protocol that discards on any ambiguity. The single highest-leverage improvement is to the proposer: its per-slot generation step is still one i.i.d. sample. A [branch audit](#branch-audit-main-is-the-union-of-the-merged-feature-work) (added after the first draft) found that **all three of the `FUNCTIONALITY-RECOMMENDATIONS.md §4` proposer levers are already implemented and merged into `main`** (from `feat/proposer-quality`): best-of-N + self-critique, hypothesis prediction-accuracy grading, and the field-diversity constraint. The genuine headline recommendations are therefore narrower than "build them": (a) **enable** the already-shipped-but-default-off best-of-N + self-critique wrapper (`best_of_n = 1` today — a scoring-contract flip that rolls the epoch), and (b) make the already-computed hypothesis prediction-accuracy signal **actionable** — today it is graded post-tournament and shown to the proposer as an advisory low/medium/high band but is never used to bias best-of-N selection or any gate. Both stay inside the existing overfitting-restricted context channels.

## Table of contents

1. [What zicato is for](#1-what-zicato-is-for)
2. [Architecture (as implemented)](#2-architecture-as-implemented)
3. [How a candidate is judged](#3-how-a-candidate-is-judged)
4. [Why it is so verbose](#4-why-it-is-so-verbose)
5. [Deterministic guarantees (the supervisor)](#5-deterministic-guarantees-the-supervisor)
6. [The proposer: structure and how to improve it](#6-the-proposer-structure-and-how-to-improve-it)
- [Branch audit (main is the union of the merged feature work)](#branch-audit-main-is-the-union-of-the-merged-feature-work)
- [Code quality: is the verbosity merited?](#code-quality-is-the-verbosity-merited)
- [Persistence: is the approach sensible? (and how to simplify it)](#persistence-is-the-approach-sensible-and-how-to-simplify-it)
- [The frontend/data-model boundary (and how to simplify it)](#the-frontenddata-model-boundary-and-how-to-simplify-it)
- [Empirical effectiveness: before and after](#empirical-effectiveness-before-and-after)
- [Summary of recommendations](#summary-of-recommendations)

---

## 1. What zicato is for

**Thesis (one sentence).** zicato is a self-improving *meta-harness* for any system you can measure — agent systems being the primary use case: it wraps a system you already built, runs it against a fixed board of tasks, watches what goes wrong via structured runtime telemetry, and rewrites the system so the next generation goes less wrong.

**What it actually does.** zicato treats your existing multi-agent system — any shape at all — as the **inner harness** of a learning loop and improves it *between* runs rather than during them.

README.md:14-27
```
zicato wraps a file-based system you already have and turns it into the **inner
harness** of a learning loop. It runs your system against a board of tasks,
scores each run against a per-epoch evaluation contract, and rewrites the source
so the next generation goes less wrong.

Multi-agent systems are the founding and primary use case — a coordinator +
specialists, a deep sub-agent tree, a single LlmAgent, whatever shape — and the
shipped reference adapter targets Google ADK. But nothing in the loop is
agent-specific. The contract asks for three things: an **entrypoint** the runner
can drive, one or more **mutable trees** of source the proposer may edit, and a
**board** of tasks with typed expectations. Anything that fits that shape can be
the target — a library, a prompt set, a rule engine — and the entrypoint may sit
*outside* every mutable tree, which is how you evolve a dependency while the
driver holds still.
```

### Place in the goldfive / harmonograf ecosystem

zicato is the third member of a three-library stack, and the split is a **cadence** split, not a feature split. goldfive is the *inner* orchestration harness that makes drift legible within a single run (goals, plans, per-turn drift analysis, an intervention ladder) and emits a typed `goldfive.v1.Event` stream. harmonograf is the live observability + steering console over that same stream. zicato is the **meta-loop**: it consumes the identical telemetry stream but *across many runs*, and it is the only one of the three allowed to reach across runs and to reach into the agent's own source. The clean division of ownership is explicit — goldfive owns the plan a run produces; zicato owns the prompts and structure that *produce* plans.

README.md:42-45
```
Goldfive owns plans; zicato owns the prompts and structure that *produce* the
plans. The two are complementary: goldfive handles "this run wandered, replan
this run", zicato handles "this kind of run keeps wandering the same way,
rewrite the harness".
```

Concretely, the three own non-overlapping layers: single-turn replan-on-drift is goldfive's job (within one run); operator steering is harmonograf's (within one run); **inner-harness rewrites across runs** are zicato's (across generations). `docs/design/ARCHITECTURE.md:56-67` states the invariant bluntly: "goldfive must never reach across runs; harmonograf must never reach into the agent's source. zicato is the only thing that does either."

### The learning loop: board -> drift telemetry -> loss patterns -> structured edits -> tournament -> promote

The loop is a numbered pipeline. A frozen board of tasks drives runs of the inner harness; each run's goldfive event stream is reduced to a typed per-run **loss profile**; loss profiles aggregate across runs into **patterns** (recurring failure shapes); patterns drive typed **patches** to an annotated mutation surface; a **tournament** compares parent vs. candidate on the frozen board; and a candidate is promoted only if it wins by a margin without regressing pass-rate.

docs/design/ARCHITECTURE.md:21-37
```
Across many runs of that inner harness, zicato:

1. Captures structured runtime telemetry (the `goldfive.v1.Event`
   stream the inner harness emits).
2. Reduces that telemetry to a typed **loss profile** per run.
3. Aggregates loss profiles across runs into **patterns** — recurring
   shapes of failure (e.g. "the research specialist keeps hallucinating
   sources when the question is short", "the coordinator delegates to
   the writer before the researcher has reported").
4. Proposes typed **patches** to the inner harness's annotated mutation
   surface (specialist instructions, coordinator routing strings, tool
   descriptions, planner templates, judge prompts — never free-form
   source edits).
5. Runs **tournaments** between the parent and the candidate generation
   against a frozen board of tasks.
6. Promotes the candidate when it wins by a configured margin and does
   not regress on pre-existing pass-rate.
```

Two design commitments make this loop legible rather than a black-box optimizer. First, edits are confined to an **annotated mutation surface** — only spans/files the operator marked `# zicato:mutable` are editable, so the search is "improve these strings," never "rewrite the agent" (`docs/design/RATIONALE.md:13-38`). Second, every proposal is a structured **Experiment = hypothesis + patches**, with the hypothesis (`core_idea`, `why`, `expected_drift_movements`, `risks`, …) recorded *before* the run and matched against actuals *after*, so the journal captures the proposer's reasoning, not just what scored (`docs/design/RATIONALE.md:82-107`). **Epochs** group generations under a frozen evaluation contract (board + proposer brief's `## Forbidden` list + scoring weights) so within-epoch comparison is precise while operator contract changes become explicit epoch boundaries (`docs/design/VOCABULARY.md:83-90`).

### What is deliberately pluggable

zicato is intentionally agnostic at three seams so it never couples to a vendor, a framework, or a storage mechanism:

- **Model-agnostic `call_llm`.** Every LLM call — proposer, analysis, emulator, LLM-graded rubrics — routes through a single narrow caller-supplied callable; the core imports no vendor SDK. The runtime carries *two* such callables (harness vs. auxiliary), and they must be distinct as a collusion guard.

src/zicato/core/runtime.py:19-26
```
#: The model-agnostic LLM-call shape used everywhere in zicato.
#:
#: Mirrors goldfive's call_llm surface: ``(system, user, model) ->
#: response``. The ``model`` parameter is a free-form string the caller
#: passes through; concrete implementations interpret it (route to a
#: provider, look up credentials, etc.). Zicato never inspects or
#: switches on ``model``.
CallLLM = Callable[[str, str, str], Awaitable[str]]
```

- **Framework-agnostic `HarnessAdapter`.** The inner harness is a black box behind a small `runtime_checkable` Protocol pair — `RunnableHarness.run(entry, sinks, config)` plus adapter `load()` / `mutation_points()` / `mutable_subpaths()` (`src/zicato/adapters/base.py:40-146`). Google ADK is the first concrete adapter; plain-callable and LangChain are planned. The runner "doesn't care" what shape the agent is (`src/zicato/adapters/base.py:18`).

- **Pluggable record storage.** Record persistence goes through a `StorageBackend` ABC — a keyed, atomic JSON/JSONL store — so tests can substitute an in-memory backend and future record stores can be added without changing domain readers, while files stay canonical in production (`src/zicato/storage/base.py:1-10`). Generation source trees use the separate `GenerationStore` protocol. The derived SQLite analytical index is explicitly *not* a backend — it is a rebuildable read side, kept separate so store-of-record and index evolve independently (`src/zicato/storage/base.py:31-35`).

The `src/zicato/__init__.py` header restates the one-liner and notes the public surface is intentionally still empty in this pre-alpha (`__version__ = "0.3.0"`), consistent with the README's "Alpha … the public API will break" stance.

---

## 2. Architecture (as implemented)

Zicato is a meta-loop: it proposes edits to an inner multi-agent harness, runs a tournament to decide whether the edit is an improvement, and journals the result. The integration point is `src/zicato/orchestrator.py` — `evolve_once` runs exactly one generation (one champion-vs-challenger round), and `evolve_n_rounds` (moved into `src/zicato/evolve/loop.py`) drives it up to N times with circuit-breakers. `src/zicato/__init__.py` deliberately exports nothing (`__version__ = "0.3.0"`); the public surface is the submodule tree, and the frozen dataclasses in `src/zicato/core/` are the contract every subsystem imports.

### Subsystem decomposition and data flow

```
                         evolve_n_rounds  (evolve/loop.py)
                         ├─ acquire workspace lock + start HeartbeatBeater
                         ├─ ensure_epoch_for_contract  (contract-hash auto-epoch, once)
                         ├─ prepare_resume  (conservative crash-resume plan)
                         └─ for round in range(rounds):     [StopPolicy set:
                              evolve_once(...)                consecutive-reject /
                                                              degenerate-health /
                                                              wall-clock-budget]
   ┌───────────────────────────── evolve_once  (orchestrator.py) ───────────────────────────┐
   │ 1. workspace_loader.load_*   -> board (+board_meta disable_drift/judge_only),           │
   │    load_epoch                    ScoringWeights (weights.tournament_structure), brief    │
   │ 2. adapter_factory / runtime_factory -> adapter, RuntimeConfig                           │
   │ 3. _ensure_baseline_snapshot + _resolve_current_generation -> parent Generation (v_n)    │
   │ 4. mutation.enumerator.enumerate_mutations(mutable_trees) -> [MutationPoint]             │
   │ 5. board.split.split_board  -> TRAIN slice only (holdout hidden from proposer)           │
   │    telemetry.reducer.read_loss_profile -> parent LossProfiles                            │
   │    patterns.detectors.detect_patterns -> [Pattern];  _render_loss_summary/_failure_...   │
   │ 6. selection.registry.make_strategy(spec) -> field_size()==1 gauntlet | >1 multi-chal.   │
   │ 7. proposer.agent.build_proposer_agent -> ProposerAgent.propose(ProposerContext)         │
   │       -> Experiment(hypothesis: HypothesisSpec, patches: (Patch,...), outcome=None)      │
   │ 8. build_post_apply_validator -> mutation.applier.apply_patches (fresh snapshot)         │
   │       + validate_post_apply  (retryable feedback into the proposer)                      │
   │ 9. check_patch_manifest_and_forbidden (patch mutation_ids ⊆ enumerated manifest)         │
   │10. tournament.runner.run_tournament | run_fast_mode  -> TournamentResult                 │
   │       (per board unit: python -m zicato._tournament_worker subprocess, isolated)         │
   │11. gate.evaluate_gate + ladder-mediated holdout -> GateOutcome;                          │
   │       selection strategy -> SelectionDecision; operator gate-override                    │
   │12. epoch.update_experiment_outcome (OutcomeRecord) + index dual-write (SQLite)           │
   │13. on promote: append_to_lineage + set_current_generation;  append_journal_entry         │
   │14. health.assess_loop_health; analyzer + epoch-report regen (best-effort)                │
   └──────────────────────────────────────────────────────────────────────────────────────┘
        writes .zicato/runtime/*  (state.py: Heartbeat, ActiveTournament, active_runs/)
                         ▲ reads (separate OS process, no shared memory)
        crates/supervisor  (Rust) — watchdog + /statusz + dashboard; SIGTERM→SIGKILL
```

The modules map onto that flow: **core** (frozen dataclasses / contract types), **workspace** + **epoch** (contract loading, lineage, journal, generation store), **mutation** (enumerate points, apply patches, validate), **patterns** (cross-run loss detectors), **proposer** (skill-composed / ADK-native agents), **selection** (tournament-structure strategies), **tournament** (runner, gate, scoring, ladder/holdout, subprocess worker transport), **scoring** (aggregate + diff-complexity), **telemetry** (goldfive JSONL sink + reducer), **runtime** (lock, heartbeat, state files, control protocol, resume, progress log), **health**/**analyzer**/**index**/**dashboard** (observability), and **storage** (pluggable file/memory backend under runtime state).

### The proposer boundary

The proposer is invoked through a resolved `ProposerAgent` and a `ProposerContext` that carries the round's entire input surface — enumerated mutation manifest, detected patterns, the (TRAIN-only) loss summary + failure profile, the brief, prior-experiment memory, and a `validate_experiment` hook the proposer re-tries against:

```python
# src/zicato/orchestrator.py:616-638
        try:
            experiment = await proposer_agent.propose(
                ProposerContext(
                    epoch_id=resolved_epoch_id,
                    parent_generation_id=parent_id,
                    new_generation_id=next_id,
                    patterns=tuple(patterns),
                    mutations=tuple(mutations),
                    brief_text=brief.text,
                    current_loss_summary=loss_summary,
                    aux_call_llm=auxiliary_call_llm,
                    model=str(workspace_config.get("auxiliary_model", "")),
                    max_retries=max_proposer_retries,
                    forbidden_ids=brief.forbidden_ids,
                    workspace_root=workspace_root,
                    validate_experiment=_validate_experiment_post_apply,
                    meta_loop_emitter=meta_loop_emitter,
                    custom_judge_names=custom_judge_names,
                    prior_experiments=tuple(prior),
                    restrict_visibility=weights.overfitting.restrict_proposer_visibility,
                    failure_profile=failure_profile,
                )
            )
```

### Applying a patch into a fresh snapshot

The applier never mutates the parent tree. `apply_patches` copies the parent snapshot into a fresh target tree, runs a deterministic all-or-nothing validation pre-check, applies the batch, then re-parses every touched `.py` file so a syntax-corrupting patch is attributed to the round that produced it rather than crashing the next generation's enumeration. (This `copytree` is the applier *primitive*, shared by both generation backends — not the storage mechanism itself. Under the **default git backend** the target is an ephemeral scratch tree that is then committed and tagged; only under the directory *fallback* is it the persisted `generations/{child}/snapshot/`. See [Persistence](#persistence-is-the-approach-sensible-and-how-to-simplify-it).)

```python
# src/zicato/mutation/applier.py:653-675
    source_root = Path(source_root).resolve()
    target_root = Path(target_root).resolve()
    if target_root.exists():
        raise FileExistsError(
            f"apply_patches: target_root {target_root} already exists; refusing to overwrite"
        )
    shutil.copytree(source_root, target_root, ignore=ignore or copytree_ignore())

    # Atomic pre-validation: enumerate the freshly-copied tree and check
    # every patch up front. The copied tree has identical content to
    # ``source_root``, so its enumeration is the surface the subsequent
    # apply will resolve against.
    problems = validate_patches(patches, source_root=target_root)
    if problems:
        # Refuse the whole batch — remove the copied tree so generation
        # lineage stays append-only and nothing is left half-applied.
        shutil.rmtree(target_root, ignore_errors=True)
        raise ValueError(
            "apply_patches: refusing to apply patch set; "
            f"{len(problems)} validation problem(s): " + "; ".join(problems)
        )

    _apply_patches_into_tree(target_root, patches)
```

### Tournament, scoring, and the gate

`run_tournament` schedules one **board unit** per board entry, each running the champion (parent) and challenger (child) concurrently in isolated `python -m zicato._tournament_worker` subprocesses against per-run ephemeral snapshot copies. It aggregates each side into a scalar via `aggregate_generation_score` (the TRAIN slice gates; the holdout is confirmation-only), then decides through the regression-prefixed gate and the ladder/holdout governor, returning a `TournamentResult`:

```python
# src/zicato/tournament/runner.py:1078-1093
    train_outcome = await _gate_with_regression(
        parent_agg=parent_agg,
        child_agg=child_agg,
        child_snapshot_root=child_gen.snapshot_root,
        weights=weights,
    )
    outcome, holdout_block = _ladder_mediated_outcome(
        train_outcome=train_outcome,
        parent_agg=parent_agg,
        child_agg=child_agg,
        holdout_parent_agg=holdout_parent_agg,
        holdout_child_agg=holdout_child_agg,
        weights=weights,
        workspace_root=workspace_root,
        epoch_id=epoch_id,
    )
```

The gate's scalar is "lower-is-better": a child promotes only when `child.scalar <= parent.scalar - promote_margin` and it does not trip pass-rate / per-namespace monotonicity or the hard regression gate (`src/zicato/tournament/gate.py:1-60`).

### Decide and persist

The orchestrator routes the duel's `GateOutcome` through the `SelectionStrategy` (gauntlet reproduces king-of-the-hill), applies any operator gate-override, and folds the deltas into an `OutcomeRecord` that is written back onto the `Experiment`:

```python
# src/zicato/orchestrator.py:926-940
    bookkeeping_decision = "promoted" if decision == "promoted" else "rejected"
    parent_scalar = float(tournament_result.parent_agg.get("scalar", 0.0))
    child_scalar = float(tournament_result.child_agg.get("scalar", 0.0))
    _gen_fields = _generalization_fields(child_scalar, tournament_result)
    outcome_record = OutcomeRecord(
        ran_at=_now_iso(),
        drift_movements=(),  # detailed per-kind movements out-of-scope for v0
        pass_rate_delta=tournament_result.outcome.delta_pass_rate,
        drift_loss_delta=(
            float(tournament_result.child_agg.get("drift_loss_mean", 0.0))
            - float(tournament_result.parent_agg.get("drift_loss_mean", 0.0))
        ),
        scalar_score_delta=tournament_result.outcome.delta_scalar,
        tournament_decision=decision,
        rejection_reason=override_reason,
```

### The journaling unit

The `Experiment` is the unit that flows through the whole pipeline: constructed by the proposer with `outcome=None`, finalized by the tournament with its `OutcomeRecord`, and appended to the journal. A promotion also updates lineage and the per-epoch `current_generation` marker so the next round's parent resolves to it:

```python
# src/zicato/core/experiment.py:415-423
    id: str
    epoch_id: str
    generation_id: str
    parent_generation_id: str | None
    proposed_at: str
    hypothesis: HypothesisSpec
    patches: tuple[Patch, ...]
    outcome: OutcomeRecord | None
    round_index: int = 0
```

`evolve_once` returns an `EvolveRoundOutcome` (parent/child ids, `"promoted"`/`"rejected"`, scalars + delta, loop-health summary), which the loop in `evolve/loop.py` feeds to its `ConsecutiveRejectionPolicy` / `DegenerateHealthPolicy` / `WallClockBudgetPolicy` circuit-breakers.

### Where the Rust supervisor sits

The supervisor (`crates/supervisor/`) is a **separate OS process** with no shared memory — it only reads the atomic JSON state files the Python loop writes under `.zicato/runtime/` (`Heartbeat`, `ActiveTournament`, `active_runs/{run_id}.json`; see `src/zicato/runtime/state.py`). Because it is independent, it can enforce per-run wall-clock deadlines and SIGTERM→SIGKILLs a wedged worker even when the orchestrator's own asyncio loop is stuck — the third line of defence behind the worker's cooperative budget and the parent's `asyncio.wait_for`:

```rust
// crates/supervisor/src/watchdog.rs:8-15
//! Deadline enforcement is a first-class, default-on trigger: every
//! board-entry run carries a `deadline` (`started_at +
//! wall_clock_budget_seconds`). When `now` passes that deadline the
//! watchdog SIGTERM→SIGKILLs the run's worker pid. Because the supervisor
//! is its own OS process this holds even when the orchestrator's event
//! loop is wedged. Run-staleness (`last_progress` not advancing) is a
//! separate, complementary trigger — a run can be killed for stalling OR
//! for blowing its wall-clock budget.
```

The same binary serves the watchdog `/statusz` surface and (unless `--no-dashboard`) the analytical API/SSE dashboard, on a port range (7920-7930) disjoint from the Python dashboard service (7892-7902), per `crates/supervisor/src/main.rs`.

---

## 3. How a candidate is judged

Judging a candidate generation is a four-stage pipeline: **board run → per-run `LossProfile` → per-generation scalar → promote gate**, wrapped in a **selection strategy** (the tournament) and a **Ladder-mediated holdout** that turns the gate verdict into a final promote/reject. The load-bearing invariant across all of it (`src/zicato/selection/strategy.py:10-15`) is that `evaluate_gate` is the single per-duel accept/reject test — every tournament structure *reads* its verdict but never re-decides a duel.

### 1. A board run produces a `LossProfile`; a weighted drift-loss is derived from it

The unit of work is a **board unit** (one per board entry). Each entry runs under a generation in its own subprocess worker, which writes a `loss.json`; the reducer reads it back as a `LossProfile` (`src/zicato/core/loss.py:216-323`). That profile is flat and JSON-round-trippable: per `(kind, severity)` drift counts, `plan_revisions`, `task_failure_ratio`, `runtime_ms`, `wall_clock_budget_exceeded`, an `expectation_result` (the entry's Predicate/Rubric outcome), a continuous `score` in `[0,1]`, a `pass_fail` bit, and a pre-computed scalar `drift_loss`.

The per-run `drift_loss` is a **weighted, drift-derived** quantity. Its formula lives in one place (imported by both the orchestrator and the killable worker so the two can never diverge):

src/zicato/scoring/builtins.py:66-97
```python
def builtin_drift_loss(
    drift_counts: tuple[DriftCount, ...],
    plan_revisions: int,
    task_failure_ratio: float,
    runtime_ms: int,
    weights: ScoringWeights,
) -> float:
    """The built-in per-run drift-loss formula (Seam 1).

    Byte-identical to ``zicato.telemetry.reducer.compute_drift_loss``::

        loss = sum(severity_weights[c.severity] * kind_mult(c.kind) * c.count
                   for c in drift_counts)
             + plan_revision_weight * plan_revisions
             + 10.0 * task_failure_ratio
             + runtime_weight * (runtime_ms / 1000.0)

    clamped to ``max(0.0, loss)``. The not-completed heavy term and the
    ``task_failure_ratio`` floor are applied by the reducer AROUND this call
    (they are reducer policy, not part of the per-run drift formula), so they
    stay where they are — this function is the inner formula only.
    """
    sev_w = weights.severity_weights
    loss = 0.0
    for c in drift_counts:
        sev_mult = sev_w.get(c.severity, 0.0)
        kind_mult = _kind_multiplier(c.kind, weights)
        loss += sev_mult * kind_mult * c.count
    loss += weights.plan_revision_weight * plan_revisions
    loss += _TASK_FAILURE_RATIO_MULTIPLIER * task_failure_ratio
    loss += weights.runtime_weight * (runtime_ms / 1000.0)
    return max(0.0, float(loss))
```

Each drift event is weighted by severity (default `info=1, warning=3, critical=10`, `_default_severity_weights` at `src/zicato/core/scoring_config.py:264-271`) times a kind/judge multiplier. Custom in-run **process judges** attribute their drift under `custom:<judge_name>`, so `_kind_multiplier` routes them through `per_judge_weights` (falling back to `default_judge_weight`) — this is where the per-judge weighting in the scoring memo lands (`src/zicato/scoring/builtins.py:38-63`).

### 2. Per-entry losses aggregate into the scalar that gates promotion

`aggregate_generation_score` (`src/zicato/tournament/scoring.py:245-407`) collapses the list of per-entry `LossProfile`s for one generation into a comparable dict: `drift_loss_mean`, `pass_rate`, a uniform continuous `mean_score` (mean of each entry's `entry_score`, which maps a bool to `1.0/0.0` and clamps a continuous score to `[0,1]`), per-namespace weighted aggregates, and the combined `scalar`. The scalar itself is the multi-objective, lower-is-better composition — **weighted drift term + a `(1 - mean_score)` pass term + one term per non-drift namespace**:

src/zicato/scoring/builtins.py:146-163
```python
    drift_component = weights.drift_weight * drift_loss_mean
    pass_component = weights.pass_weight * (1.0 - mean_score)
    scalar_components: dict[str, float] = {
        "drift": drift_component,
        "pass": pass_component,
    }
    for ns, value in namespace_aggregates.items():
        if ns == "drift:":
            continue
        component_name = ns[:-1] if ns.endswith(":") else ns
        scalar_components[component_name] = value
    # Parsimony / MDL term, appended LAST and only when opted in. The guard is
    # the byte-identical-when-off contract: at the 0.0 default (or with no diff
    # size) the key is never written, so `sum(...)` is unchanged.
    diff_component = diff_complexity_component(weights, diff_size)
    if diff_component is not None:
        scalar_components["diff_complexity"] = diff_component
    return sum(scalar_components.values())
```

Namespaces (`drift:`, `cost:`, `latency:`, `rubric:`, `schema:`, `output:`) carry signed weights so "higher is worse" (drift/cost/schema, positive) and "higher is better" (rubric, negative) collapse onto one lower-is-better axis (`_default_namespace_weights`, `src/zicato/core/scoring_config.py:274-303`). An opt-in `diff_complexity` (parsimony/MDL) term is added *only* to the challenger and only when `diff_complexity_weight > 0`, so the default scalar is byte-identical to a contract without it.

### 3. The gate: three train-slice rules + a holdout confirmation

The gate/threshold lives in `evaluate_gate` (`src/zicato/tournament/gate.py:411-522`). The **promotion threshold is `ScoringWeights.promote_margin`** (default `0.01`, `src/zicato/core/scoring_config.py:487-489`). Rule 1 is the scalar-margin bar: the child's loss must drop by at least `promote_margin`, else it is rejected as either a near-miss or an outright regression:

src/zicato/tournament/gate.py:449-473
```python
    if child_scalar > parent_scalar - weights.promote_margin:
        # delta_scalar = child - parent. Positive => child's loss rose
        # (worse); zero/negative => child improved or tied but by less
        # than the promotion threshold.
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
                f"{improvement:.6f} (champion {parent_scalar:.6f} -> "
                f"challenger {child_scalar:.6f}); a promotion needs a drop "
                f"of at least {weights.promote_margin:.6f}"
            )
        return GateOutcome(
            decision=TournamentDecision.REJECTED,
            reason=verdict,
            delta_scalar=delta_scalar,
            delta_pass_rate=delta_pass_rate,
        )
```

Rule 2 is **pass-rate monotonicity** (on by default), scoped either `per_entry` (any champion-passed entry that flips to fail rejects) or `aggregate` (only the overall `mean_score` may not drop). Rule 3 is **per-namespace monotonicity**: any namespace flagged in `namespace_monotonicity` (default `rubric:`, `schema:`) that regresses in its "worse" direction rejects, even when the combined scalar improved (`gate.py:479-501`). Before any of this, when `regression_gate_enabled` is set, `_gate_with_regression` (`src/zicato/tournament/runner.py:838-882`) shells out to the child snapshot's own test suite as a **hard gate** — a failing suite rejects regardless of drift/pass movement.

If the three train rules would promote and a held-out board slice exists, the win must **also confirm on the holdout**: `_holdout_confirms` (`gate.py:342-391`) rejects only if the challenger's holdout loss *rose* past `promote_margin` or the holdout shows a pass-rate regression. It is deliberately asymmetric — the holdout is never asked to clear the margin in the improving direction, so it guards against board-memorization rather than acting as a second, stricter bar.

### 4. The tournament and how a winner resolves to promote/reject

The per-epoch structure is a `ScoringWeights.tournament_structure` field (gauntlet by default); the registry maps the token to a `SelectionStrategy` — **gauntlet, single_elim, double_elim, swiss, racing** (`src/zicato/selection/registry.py:28-34`). `resolve_tournament` (`src/zicato/selection/driver.py:109-173`) walks any strategy uniformly: request the challenger field, seed, then loop `next_matchups()` → run each duel via `run_matchup` (which ends in the *same* `evaluate_gate`, `src/zicato/tournament/runner.py:1410-1415`) → `record_result()` until `resolved()`, then return `champion()`. The **default gauntlet** is one champion vs one challenger, one full-board duel, promote-on-gate — and it never re-implements the gate, it just reads `outcome.decision`:

src/zicato/selection/strategies/gauntlet.py:78-95
```python
    def champion(self) -> SelectionDecision:
        if self._result is None or self._challenger is None:
            # No duel ran (degenerate); the champion stands.
            return SelectionDecision(
                promoted_generation_id=None,
                decision=TournamentDecision.REJECTED,
                reason="no challenger duel ran",
            )
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

Multi-contestant structures interpret the same `GateOutcome` per their bracket/Swiss/racing rules. When a field has cycles (noisy losses can produce them), a cycle-robust winner-resolution layer (`src/zicato/selection/resolve.py`) — Condorcet fast-path, Smith-set prune, Tideman Ranked Pairs, Copeland — proposes an *internal* leader over a net-margin matrix, but it only ever proposes: the unchanged champion gate still owns promotion. There is also an opt-in Bradley–Terry evidence pre-gate (`driver.py:176-312`) that can *hold* a promotion (defer→replicate→`inconclusive`) but can never force one.

### 5. Folding the holdout back in: the final decision

For the gauntlet, `run_tournament` (`src/zicato/tournament/runner.py:1078-1093`) runs `_gate_with_regression` on the **train slice** and then passes that verdict through `_ladder_mediated_outcome`, which governs how often the reused holdout may be queried (Blum–Hardt Ladder: a release only when the *train* improvement clears the threshold, charged against a per-epoch budget). A **released non-confirmation is the only thing that flips a train-promote into a holdout reject**; a confirmation, a withheld query, or an exhausted budget ("champion stands") leaves the train promote intact:

src/zicato/tournament/governance.py:300-308
```python
    if release.released and not raw_confirmed:
        final = GateOutcome(
            decision=TournamentDecision.REJECTED,
            reason=raw_reason,
            delta_scalar=train_outcome.delta_scalar,
            delta_pass_rate=train_outcome.delta_pass_rate,
        )
    else:
        final = train_outcome
```

The resulting `GateOutcome.decision` (`promoted` / `rejected` / `deferred`) plus its deltas, holdout block, and generalization-gap scalars flow up as a `TournamentResult` (`runner.py:252-311`) — the protected-incumbent invariant means any non-promote leaves the champion standing.

---

## 4. Why it is so verbose

zicato is large — roughly 85k lines of Python across 235 files under `src/zicato/`, plus ~14.5k lines of Rust in `crates/supervisor/`. But raw size understates the texture. An AST/token census of `src/zicato/` finds that only **~47% of lines are executable code**; **~31% are docstrings and ~9% are comments** (40% prose-in-source), with the remaining ~13% blank. The verbosity is not accidental — it is four deliberate disciplines, each trading lines for the legibility a *self-modifying* system needs. (Whether that trade is *merited*, and where it tips into reducible debt, is audited in a [dedicated section below](#code-quality-is-the-verbosity-merited).)

**1. Docstrings carry the design rationale, not just the API.** A field can be a one-token declaration and still carry a paragraph explaining why it exists, what turns it on, and which design memo governs it. `ProposerContext.restrict_visibility` is a plain `bool` with six lines of comment tying it to the overfitting program:

src/zicato/proposer/agent.py:101-107
```python
    #: When ``True`` (the default-on
    #: :attr:`~zicato.core.types.OverfittingConfig.restrict_proposer_visibility`
    #: posture, set by the orchestrator from the epoch's scoring config),
    #: the assembled prompt aggregates per-entry pattern identities to
    #: counts/rates and coarsens experiment-memory Δscalar to buckets
    #: (OVERFITTING.md §11). ``False`` renders the verbatim prompt.
    restrict_visibility: bool = False
```

The same pattern scales up: `propose_experiment` (`src/zicato/proposer/proposer.py:85-247`) is ~160 lines of function signature and per-parameter docstring wrapping a ~180-line body, because each of its many keyword inputs (`restrict_visibility`, `failure_profile`, `prior_experiments`, `skills`, `meta_loop_emitter`, …) documents its default-is-a-no-op contract inline. Reading a single function tells you the whole contract without cross-referencing the design docs.

**2. Additive, byte-identical-by-default contract discipline.** Almost every capability lands as an opt-in that is provably a no-op at its default, and the code both *says so* in a comment and enforces it structurally — often by not even interposing an object. The best-of-N proposer wrapper is the archetype: at the default `best_of_n <= 1` it returns the inner agent unchanged, so a contract that never opts in behaves byte-for-byte as before the feature existed:

src/zicato/proposer/best_of_n.py:286-300
```python
def wrap_with_proposer_quality(
    inner: ProposerAgent, config: ProposerQualityConfig
) -> ProposerAgent:
    """Interpose best-of-N + self-critique only when an operator opts in.

    Returns ``inner`` UNCHANGED when ``config.best_of_n <= 1`` (the default),
    so a contract that does not opt in pays nothing and behaves
    byte-identically — there is not even a wrapper object in the call path.
    Otherwise wraps ``inner`` in a :class:`BestOfNProposerAgent`. The
    orchestrator calls this once per evolve invocation, right after it builds
    the epoch's proposer agent.
    """
    if config.best_of_n <= 1:
        return inner
    return BestOfNProposerAgent(inner=inner, config=config)
```

The same idiom recurs across the scorer (the `diff_complexity` term is appended only when opted in — `builtins.py:146-163`, Section 3), the reducer, and the overfitting levers. Each such feature costs a short guard plus a comment asserting the invariant; multiplied across dozens of levers, that is a large fraction of the "extra" lines — and it is what lets an operator upgrade zicato without silently changing every epoch already on disk.

**3. Defense in depth means layered, redundant checks — each spelled out.** A self-editing loop cannot trust any single validator, so the same fact is verified at multiple layers, and each layer is written out explicitly. Parsing the proposer's output is itself a *two-pass* validation (a JSON-Schema shape pass plus a local cross-check pass the schema cannot express — `src/zicato/proposer/structured.py:1-27`); the applier pre-validates the whole batch and then re-parses every touched file (Section 2); and the Rust supervisor independently re-derives the promotion gate and re-hashes each child snapshot against its parent (Section 5). None of these layers is individually large, but they are all present, all commented with what could go wrong without them, and they compound.

**4. Two parallel proposer paths, pluggable seams, and frozen contract types.** The proposer ships *two* full implementations (a single-shot text-shim engine and a native tool-using ADK agent) behind one `ProposerAgent` protocol, with every `google.adk` import kept lazy so the default path never forces the optional extra (`src/zicato/proposer/adk_agent.py:1-51`). The three deliberately pluggable seams (`CallLLM`, `HarnessAdapter`, `StorageBackend` — Section 1) each add a protocol/ABC plus at least one concrete implementation. And the `src/zicato/core/` frozen dataclasses that every subsystem imports as its contract are exhaustively field-documented because they are the API surface between subsystems. In short: zicato is verbose the way a spec-with-executable-tests is verbose — the prose *is* the spec, co-located with the code it governs, which is exactly what a system that rewrites code needs to stay auditable.

---

## 5. Deterministic guarantees (the supervisor)

zicato's self-improvement loop is made safe by a strict division of labor between two processes. The **Rust supervisor** (`crates/supervisor/`) is a separate OS process — a watchdog + integrity notary — that runs two independent `tokio` loops (`heartbeat_loop`, `runs_loop`, wired in `main.rs:285-322`). Every escalation decision is a *pure function* of `(state, now, thresholds)` (`decide_heartbeat`, `decide_run`, `decide_run_deadline`, `resolve_kill_target`), so the safety logic is unit-testable and holds even when the orchestrator's asyncio loop is wedged. The **Python runtime** (`src/zicato/runtime/`) owns the single-writer state contract: atomic writes, the workspace lock, the heartbeat, and the conservative crash-resume protocol. The Rust side never writes the orchestrator's trees; the Python side never signals workers. Below, the guarantees are separated by which process enforces them.

### The Rust watchdog (out-of-band enforcement)

**1. The watchdog never kills the orchestrator (warn-only heartbeat).** `HeartbeatAction` has no `Kill` variant *by construction*; a stale — even "deeply stale" — heartbeat only raises warning severity. Restart is delegated to an out-of-band process supervisor.

`crates/supervisor/src/watchdog.rs:377-388`
```rust
fn classify_age(age_secs: u64, t: &Thresholds) -> HeartbeatAction {
    // Deep-stale: past the former kill threshold. We do NOT kill — we
    // escalate the warning's severity and leave the restart decision to
    // the operator / process supervisor.
    if age_secs >= t.heartbeat_stale_kill.as_secs() {
        return HeartbeatAction::Stale;
    }
    if age_secs >= t.heartbeat_stale_warn.as_secs() {
        return HeartbeatAction::Warn;
    }
    HeartbeatAction::Nothing
}
```
*What could go wrong without it:* the watchdog could destroy in-flight tournament work by killing the very loop it exists to protect during a legitimately slow LLM call, a GC pause, or a debugger stop.

**2. Pid-safety guard — the watchdog only ever signals a real run worker.** Every kill path (`decide_run_deadline`, `decide_run_kill_request`, the staleness `Kill`) funnels through this guard before any signal.

`crates/supervisor/src/watchdog.rs:622-638`
```rust
pub fn is_signalable_run_pid(pid: i32, protected: &HashSet<i32>) -> bool {
    // pid 0 addresses the whole process group; pid 1 is init. Neither is a
    // run worker, and signalling them would be catastrophic.
    if pid <= 1 {
        return false;
    }
    // Never signal ourselves.
    if pid == std::process::id() as i32 {
        return false;
    }
    // Never signal the orchestrator (heartbeat pid) or any other pid the
    // caller has explicitly fenced off.
    if protected.contains(&pid) {
        return false;
    }
    true
}
```
The `protected` set is rebuilt every tick from the heartbeat's pid (`runs_loop`, `watchdog.rs:1035-1039`), so the orchestrator is never treated as a worker. *What could go wrong without it:* a stray pid `0`/`1` in a state file would signal the whole process group or `init`; a recycled sentinel could hit the supervisor or orchestrator itself.

**3. The orchestrator-written deadline is untrusted and clamped.** The per-run `deadline` field is written by the orchestrator (the thing being policed); a far-future value would silently disable the watchdog. `effective_deadline` caps the enforced cutoff at `started_at + max_run_seconds` (default 6h, `main.rs:87-88`).

`crates/supervisor/src/watchdog.rs:710-724`
```rust
pub fn effective_deadline(
    run: &crate::state::ActiveRun,
    max_run_seconds: Duration,
) -> Option<DateTime<Utc>> {
    let written = run.deadline?;
    let Some(started) = run.started_at else {
        // No anchor → cannot clamp; honour the written deadline.
        return Some(written);
    };
    let ceiling = chrono::Duration::from_std(max_run_seconds)
        .map(|d| started + d)
        // An absurd max_run_seconds that overflows chrono → no clamp.
        .unwrap_or(written);
    Some(written.min(ceiling))
}
```
`decide_run_deadline` (`watchdog.rs:746-781`) then does SIGTERM → wait `run_kill_grace` → SIGKILL. This is the *primary* kill trigger; run-staleness (`decide_run`, `watchdog.rs:578-604`, kill at `2×wall_clock_budget_seconds`) is a complementary backstop. *What could go wrong without it:* one bad (or accidentally huge) deadline write disables wall-clock enforcement for that run entirely.

**4. Pid-reuse-proof identity — never signal (or declare dead) a recycled pid.** A pid number alone is not an identity; the supervisor pairs it with the process start time (read from `/proc/<pid>/stat` field 22, `signal.rs:71-83`). Every liveness-sensitive decision — the deadline kill, the reaper's dead-orchestrator check, the divergence auditor's "stuck worker" — goes through `is_same_process`.

`crates/supervisor/src/signal.rs:120-133`
```rust
pub fn is_same_process(pid: i32, expected_start_time: Option<f64>) -> bool {
    if !is_alive(pid) {
        return false;
    }
    let Some(expected) = expected_start_time else {
        return true;
    };
    match pid_start_time(pid) {
        // Both readings are integer-valued tick counts, so exact float
        // equality is the right comparison (no tolerance needed).
        Some(current) => current == expected,
        None => true,
    }
}
```
The fallbacks are deliberately conservative: an absent recorded token or an unreadable current token degrades to bare liveness rather than manufacturing a false mismatch. *What could go wrong without it:* after a worker exits and the kernel reissues its pid, the watchdog could SIGKILL an innocent unrelated process.

**5. Confirmed-dead (not merely slow) orchestrator reaping.** Only when the orchestrator's own reaper can never run does the supervisor step in to group-kill orphaned workers, GC their ephemeral snapshots, and finalize state files (`reap_dead_orchestrator_runs`, `watchdog.rs:898-946`). The gate is an *identity* check, not a stale timestamp.

`crates/supervisor/src/reap.rs:49-58`
```rust
pub fn decide_orchestrator_dead(heartbeat: Option<&Heartbeat>) -> bool {
    let Some(hb) = heartbeat else {
        return false;
    };
    let Some(pid) = hb.pid else {
        return false;
    };
    // No recorded orchestrator start-time token yet → None (bare liveness).
    !signal::is_same_process(pid, None)
}
```
*What could go wrong without it:* a slow-but-alive orchestrator (whose pid stays alive no matter how stale its timestamp) would have its in-flight workers reaped out from under it.

The reaping worker-kill is itself upgraded to a **whole-process-group** kill only after a second layer of guards: `resolve_kill_target` (`watchdog.rs:660-682`) negates a pgid *only* when it equals the vetted worker's own group (`pgid == pid`, since workers are spawned `start_new_session`) and passes `is_negatable_pgid` (`signal.rs:152-157`, which refuses `pgid <= 1` and the supervisor's / orchestrator's own pgids gathered by `protected_pgids`). *What could go wrong without it:* a group-kill (`kill(-pgid, …)`) into a protected or malformed group would take down the supervisor or orchestrator.

The snapshot GC is prefix-guarded: `reapable_snapshot_root` (`reap.rs:90-135`) walks ancestors for the first `ztw-snap-*` directory and enforces that it is a *strict descendant of the system temp dir* with the right basename, refusing empty/relative/outside paths — so a malformed or hostile `snapshot_path` record can never `rmtree` an arbitrary tree.

**6. Tamper-evident, hash-chained audit ledger.** The orchestrator can rewrite its own `.zicato/` state, so the supervisor keeps an independent append-only JSONL ledger (opt-in via `--ledger-dir`, placed outside the orchestrator's trees). Each record's `digest = SHA-256(seq ‖ prev ‖ ts ‖ kind ‖ payload)` and `prev` links to the predecessor's digest; `verify_chain` re-reads the file from scratch and recomputes everything.

`crates/supervisor/src/ledger.rs:314-347`
```rust
        // seq must be contiguous from 0.
        if rec.seq != expected_seq {
            return VerifyReport {
                intact: false,
                records,
                first_break_seq: Some(rec.seq),
                break_reason: Some(format!(
                    "seq discontinuity: expected {expected_seq}, found {}",
                    rec.seq
                )),
            };
        }
        // prev must link to the previous record's digest (or genesis).
        if rec.prev != expected_prev {
            return VerifyReport {
                intact: false,
                records,
                first_break_seq: Some(rec.seq),
                break_reason: Some(format!("seq {} prev-link broken", rec.seq)),
            };
        }
        // The recorded digest must recompute over the record's own fields.
        let recomputed = compute_digest(rec.seq, &rec.prev, &rec.ts, rec.kind, &rec.payload);
        if recomputed != rec.digest {
            return VerifyReport {
                intact: false,
                records,
                first_break_seq: Some(rec.seq),
                break_reason: Some(format!("seq {} digest mismatch (record altered)", rec.seq)),
            };
        }
        expected_prev = rec.digest;
        expected_seq = rec.seq + 1;
        records += 1;
```
Editing a payload, dropping a record, or reordering breaks the chain at a pinned `seq` (proven by the `editing_a_payload_breaks_the_chain` / `dropping_a_record_breaks_the_chain` tests, `ledger.rs:503-539`). Watchdog escalations, observed promote/reject transitions, and contract-hash changes are all stamped in (`record_action`, `TransitionObserver`), and a restart resumes the same chain (`seed_tail`, `ledger.rs:167-186`). Note the append is best-effort-durable: on I/O failure the in-memory tail is *not* advanced so the next append retries the same `seq`/`prev` rather than chaining onto a record that never reached disk (`append`, `ledger.rs:225-235`). *What could go wrong without it:* the audited process could silently edit its own history with no detectable trace.

**7. Diff containment — candidate edits cannot escape the declared mutation surface.** For each materialised child generation the supervisor independently re-hashes the child snapshot against its parent and asserts every file *outside* the registered mutable trees is byte-identical.

`crates/supervisor/src/diff_containment.rs:229-246`
```rust
    for rel in all_paths {
        // Only OUT-OF-BOUNDS files matter: an in-bounds file may freely differ
        // (it is the mutation surface). This is the whole point of the check.
        if is_in_bounds(rel, &basenames) {
            continue;
        }
        let kind = match (parent_hashes.get(rel), child_hashes.get(rel)) {
            (Some(p), Some(c)) if p == c => continue, // byte-identical — fine.
            (Some(_), Some(_)) => DiffKind::Changed,
            (None, Some(_)) => DiffKind::Added,
            (Some(_), None) => DiffKind::Deleted,
            (None, None) => continue, // unreachable: rel came from one of them.
        };
        violations.push(Violation {
            path: rel_key(rel),
            kind,
        });
    }
```
It is fail-open-to-alarm: an unreadable/missing snapshot is *skipped* (never a false quarantine), and a positive violation writes a durable quarantine finding into the epoch health dir plus a ledger alert (`run_diff_containment_scan`, `watchdog.rs:127-165`). Alarm-only in v1 (this out-of-band supervisor scan does not block a promotion; the opt-in in-band twin `ScoringWeights.block_on_containment_violation` — default off — is the one that rejects, see `_integrity_block_reason`). *What could go wrong without it:* a proposer (or a bug) could mutate files outside its sandbox — vendored code, sibling agents, the harness itself — undetected.

**8. Promotion gate + divergence audit (integrity-notary records #3/#4).** `check_row` (`promotion_gate.rs:101-141`) re-applies the gate's scalar rule independently — a `promoted` decision is supported iff `delta_scalar <= -promote_margin` (loss dropped by at least the margin); a promotion the recorded scores don't support is a hard contradiction. It is direction-precise (only the "promoted-but-scores-don't-support-it" direction is alarmed) and fail-open (a row with no scalar evidence is skipped). The divergence auditor (`divergence.rs:77-254`) joins the canonical directory-derived lineage against the SQLite index and flags `promoted_divergence`, `parent_divergence`, `contract_hash_divergence`, malformed hashes, and `stuck_in_flight_generation` (a dead-worker, never-resolved generation past an age threshold, itself gated on `is_same_process`). *What could go wrong without them:* a recorded promotion could contradict its own evidence, or a stale/corrupt analytical index could silently diverge from the canonical lineage.

### The Python runtime (single-writer state contract)

**9. Atomic writes — no reader ever observes a half-written state file.** Every state file (`heartbeat.json`, each `active_runs/*.json`, the lock, ledger sidecars) is written tmp → `fsync` → `os.replace`.

`src/zicato/storage/_atomic.py:47-62`
```python
def atomic_write_text(path: Path, content: str) -> None:
    """Atomically replace ``path`` with ``content``.

    Creates parent directories as needed. ``fsync`` flushes the temp
    file's content before the rename so a crash after the rename
    cannot leave an empty file behind.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_path(path)
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.write(fd, content.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
```
This is the shared contract the Rust reader depends on: `list_active_runs` skips leftover `.tmp` artifacts (`state.py:364-366`), and `read_json` deliberately does *not* swallow decode errors — a partial file would signal something bypassed the helpers. *What could go wrong without it:* a crash mid-write, or a reader racing a writer, would see a truncated JSON file and either crash or act on corrupt state.

**10. Single-writer workspace lock with pid-reuse-proof stealing.** Only one orchestrator may write `runtime/`. The lock records `(pid, start_time, instance_id)`; a stale lock is stolen only after `is_same_process` proves the prior owner is genuinely gone.

`src/zicato/runtime/lock.py:282-298`
```python
        # Different pid, OR same pid number but a different process
        # (recycled). Is the recorded owner still the same live process?
        if is_same_process(prior.pid, prior.start_time):
            raise WorkspaceLockHeld(
                f"workspace {workspace_root} locked by live pid {prior.pid} "
                f"(instance {prior.instance_id!r}, acquired {prior.acquired_at})"
            )
        if not steal_stale:
            raise WorkspaceLockHeld(
                f"workspace {workspace_root} locked by stale pid {prior.pid} "
                f"(instance {prior.instance_id!r}, acquired {prior.acquired_at}); "
                "refusing to steal with steal_stale=False"
            )
        # Fall through to overwrite: the prior owner is gone (or its pid
        # was reused by an unrelated process — the start-time mismatch
        # proves it is not the lock owner, so stealing is correct).
```
`release_workspace_lock` (`lock.py:310-327`) only deletes a lock still bearing the caller's own `(pid, instance_id)`, so it never stomps a successor that stole it after a crash. *What could go wrong without it:* two orchestrators writing the same workspace corrupt the lineage/journal; and without the start-time check a recycled pid either makes a dead owner's lock look alive (permanent refusal to start) or lets a live lock be stolen.

**11. Conservative crash-resume — discard on any ambiguity.** After a mid-tournament kill, `prepare_resume` (`resume.py:222-351`) first clears the volatile `runtime/` state (`clear_runtime_state`), then classifies the latest un-outcomed generation. It resumes *in place* only in the single provably-safe case (readable un-outcomed experiment + applied snapshot + ≥1 cached `loss.json`); every ambiguous case discards the generation directory and re-proposes.

`src/zicato/runtime/resume.py:306-334`
```python
    # The experiment is readable and un-outcomed: an interrupted round.
    # Decide resume-in-place vs discard from the snapshot + loss markers.
    snapshot_present = (_generations_root(workspace_root, epoch_id) / latest / "snapshot").is_dir()
    if not snapshot_present:
        # Proposed-but-not-applied (or applier crashed mid-derive). No
        # snapshot means no board unit can have run; discard and re-run
        # the round fresh rather than re-deriving from a possibly-partial
        # state (conservative).
        log.warning(
            "resume: generation %s was proposed but has no applied snapshot; "
            "discarding and re-running the round fresh",
            latest,
        )
        _discard_generation(workspace_root, epoch_id, latest)
        return ResumePlan(discarded_generation_id=latest, classification="discard_unapplied")

    if not _has_any_loss(workspace_root, epoch_id, latest):
        # Applied-but-not-running: the snapshot exists but no board unit
        # finished, so there is no cached work to save. Discarding and
        # re-running is byte-identical to "start the tournament from
        # scratch" and keeps the path simple (the orchestrator never has
        # to special-case an experiment with zero cached units).
        log.warning(
            "resume: generation %s was applied but no board unit completed; "
            "discarding and re-running the round fresh",
            latest,
        )
        _discard_generation(workspace_root, epoch_id, latest)
        return ResumePlan(discarded_generation_id=latest, classification="discard_no_progress")
```
This is corruption-free by construction: a generation is appended to `lineage.json` and journaled only *after* its outcome is decided, so an un-outcomed generation has no append-only record and discarding it touches nothing durable. The load-bearing invariant (`resume.py:31-43`) is that the unit cache key `(generation_id, entry_id, replicate)` excludes the patch set, so resume-in-place reuses the *persisted* `experiment.json` + patches rather than re-proposing (the proposer is non-deterministic). *What could go wrong without it:* reusing cached `loss.json` units under freshly re-proposed (different) patches would be silent, stale-but-cached lineage corruption.

### Two runtime handshakes worth naming

**Single-escalator kill handshake.** The Python parent never signals a worker directly. To kill an over-budget worker it writes a `control/kill_requests/{run_id}` marker (`request_worker_kill`, `state.py:389-406`); the supervisor's trigger-0 branch (`watchdog.rs:1092-1135`) is the *sole* SIGTERM→grace→SIGKILL escalator and clears the marker after. *What could go wrong without it:* a parent↔supervisor race over the same pid could double-signal or hit a recycled pid.

**True-liveness `seq` cursor.** The heartbeat timer bumps `last_heartbeat` on a schedule even when the loop is wedged; the `seq` field (`Heartbeat.seq`, `state.py:141-155`) advances *only* on a genuine loop transition (the timer re-writes the same `seq`, `heartbeat.py:141-148`). `SeqLiveness` (`watchdog.rs:429-559`) measures staleness from the last seq *change*, catching a wedged loop whose fresh timestamp would otherwise read as alive — still warn-only. The claim-once control primitive (`atomic_claim` via `os.rename`, `storage/_atomic.py:76-101`) similarly guarantees a pending command fires for exactly one racing consumer. *What could go wrong without the seq cursor:* a hung orchestrator whose beater thread keeps stamping `now()` would never be flagged.

---

## 6. The proposer: structure and how to improve it

The proposer is the LLM-authoring stage of the loop — the only place a new candidate is *invented*. Given a generation's loss patterns, the mutation manifest, the operator brief, prior-experiment memory, and a bucketed failure-mode profile, it produces one `Experiment` = a schema-validated `HypothesisSpec` joined with a tuple of concrete `Patch`es (`src/zicato/proposer/__init__.py:1-32`). Everything downstream (apply → tournament → gate) merely *judges* what the proposer emits, so proposal quality is the loop's dominant lever.

### Three resolution paths behind one protocol

The orchestrator asks `build_proposer_agent` for a `ProposerAgent`, and the resolved spec selects one of three implementations — all satisfying the same `async def propose(ctx) -> Experiment` protocol:

src/zicato/proposer/agent.py:223-239
```python
    if spec.agent_source_sha256 is not None:
        if proposer_path is None:
            raise ValueError(
                "spec declares a custom proposer agent (agent_source_sha256 is "
                "set) but no proposer_path was supplied to load "
                "proposers/<name>/agent.py from"
            )
        return ADKProposerAgent(spec=spec, proposer_path=proposer_path)

    if spec == ProposerSpec.default():
        # The DEFAULT proposer: a tool-using ADK agent bound to the
        # auxiliary model at propose time. No proposer dir was configured.
        return ADKProposerAgent(spec=spec, builtin_default=True)

    # A configured proposer dir with skills but no custom agent.py — the
    # skill-composed single-shot engine, the explicit opt-in.
    return DefaultProposerAgent(spec)
```

The **default** (no proposer dir configured) is a native ADK tool-using agent that runs on ADK's own `Runner` with its own `model=` — *not* the auxiliary text shim, which is text-in/text-out and cannot express function calls (`src/zicato/proposer/adk_agent.py:6-33`). Configuring a proposer dir with `skills/*.md` but no `agent.py` is the *explicit opt-in* into the single-shot `DefaultProposerAgent` text-shim engine; dropping an author-owned `agent.py` into the dir yields a fully custom ADK agent. A proposer dir (or an edited skill) folds into the contract hash and rolls the epoch (`src/zicato/proposer/skills.py:15-21`).

### The engine loop: compose → call → parse → enforce → validate → retry

Both the text-shim engine (`propose_experiment`) and the ADK agent share one bounded-retry shape: render prompts, call the model, parse to a typed `Experiment`, enforce the brief's forbidden-id list, then run the caller's optional post-apply validation hook — and feed any failure back as concrete feedback into the next attempt, all inside a `max_retries + 1` budget:

src/zicato/proposer/proposer.py:406-431
```python
        # Post-parse validation hook — the experiment is well-formed and
        # forbidden-id-clean, but its patches may still break the child
        # snapshot once applied (a dropped import, a syntax error, a
        # vanished marker). Treat a non-empty finding list exactly like
        # a parse error: feed the concrete validator strings back and
        # retry, within the same bounded budget.
        if validate_experiment is not None:
            try:
                post_apply_errors = await validate_experiment(experiment)
            except PostApplyValidationError as exc:
                post_apply_errors = exc.errors
            if post_apply_errors:
                err = "patches failed post-apply validation: " + "; ".join(post_apply_errors)
                attempt_errors.append(err)
                feedback = err
                # Well-formed JSON whose patches broke the snapshot — a
                # content failure, not a shape one. The validator findings
                # in the feedback string are the actionable signal; no
                # prior-output echo / empty framing.
                feedback_prior_output = ""
                feedback_was_empty = False
                continue

        return experiment

    raise ProposerError(attempt_errors)
```

The retry loop is deliberately *repair-aware*: a shape failure echoes the prior raw output back so the model can see the stray fence/prose it emitted, whereas a content failure (forbidden id, or a patch that broke the snapshot) feeds the concrete finding without the echo. The post-apply hook is what makes a destructive patch cost one retry instead of a wasted tournament round — the orchestrator supplies a hook that applies the patch set to a fresh child snapshot and runs `validate_post_apply`.

### The patch contract and its two-pass validation

The proposer's output is validated in two passes (`src/zicato/proposer/structured.py:1-27`): a JSON-Schema *shape* pass, then a local *cross-check* pass the schema cannot express — every `mutation_id` must resolve in the live manifest, each op discriminates which `new_*` field is required (and `set_numeric`/`set_enum` are range/domain-checked against the `MutationPoint` metadata), and each drift kind must be a registered goldfive kind. The patch half of the schema is compact and load-bearing:

src/zicato/proposer/structured.py:164-181
```python
        "patches": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["mutation_id", "op", "rationale"],
                "properties": {
                    "mutation_id": {"type": "string", "minLength": 1},
                    "op": {"enum": ["replace", "set_numeric", "set_enum"]},
                    "new_content": {"type": "string"},
                    "new_numeric": {"type": "number"},
                    "new_enum": {"type": "string"},
                    "rationale": {"type": "string", "minLength": 1},
                },
            },
        },
    },
}
```

The `HypothesisSpec` half additionally requires falsifiable predictions — `expected_drift_movements` / `expected_metric_movements` (direction + magnitude enums) and `expected_pass_rate_delta`. These *are* graded against actuals after a tournament settles: `grade_hypothesis_predictions` (`src/zicato/tournament/detail.py:1184`) joins the expected movements against the realised outcome by sign and range-normalised magnitude bucket, and the fraction is folded back into experiment memory as `PriorExperiment.prediction_accuracy` (`src/zicato/index/query.py:485,601`) and surfaced to the proposer as a banded `prediction:low|medium|high` annotation (`src/zicato/proposer/prompts.py:530,588`). What it is *not* is **actionable**: the band is displayed but never used to bias best-of-N selection, weight proposals, or gate anything (a repo-wide grep finds zero non-display readers of `prediction_accuracy`). That advisory-only status — not a missing implementation — is the real gap the recommendations below address. *(This paragraph was corrected after a [branch audit](#branch-audit-main-is-the-union-of-the-merged-feature-work); the first draft, scoped to the `proposer/` module, wrongly stated the predictions were "never compared to actuals" and missed the grader living under `tournament/` + `index/`.)*

### Read-only grounding tools

A tool-using proposer reasons *while reading the world*. The default agent is bound to a fixed read-only tool registry:

src/zicato/proposer/tools.py:344-350
```python
DEFAULT_PROPOSER_TOOLS = (
    list_mutation_points,
    read_mutable_file,
    grep_mutable,
    read_journal,
    read_insights,
)
```

These read the *parent* snapshot, the epoch journal, and the analyzer insights — never write (a writing tool would corrupt the tree the round is about to patch and break the applier's content-hash guard). The per-round context is passed via a `contextvars.ContextVar` bound around each agent run so concurrent challengers never leak context into one another, and `read_mutable_file` / `grep_mutable` reject any path that escapes the mutable subtrees (absolute paths, `..` traversal — `tools.py:178-207`). The proposer runs on its *own* model; because it is not the auxiliary callable, the hard `is`-identity collusion guard does not apply, so a soft WARNING is logged when the proposer model string trivially equals the auxiliary model (`adk_agent.py:319-347`).

### The one structural weakness: single-sample generation

The proposer's biggest untapped quality reservoir is that each generation slot is still **one i.i.d. sample** — the retry loop only fires on *invalid* output, so a valid-but-mediocre proposal is never reconsidered. A best-of-N + self-critique wrapper *exists and is merged* but is default-off (`ProposerQualityConfig.best_of_n: int = 1`, `critique_enabled: bool = True` at `src/zicato/core/scoring_config.py:241-242`); at the default it is a transparent pass-through:

src/zicato/proposer/best_of_n.py:188-217
```python
    async def propose(self, ctx: ProposerContext) -> Experiment:
        n = self.config.best_of_n
        if n <= 1:
            # Byte-identical to today: one inner sample, no critique.
            return await self.inner.propose(ctx)

        candidates: list[Experiment] = []
        last_error: ProposerError | None = None
        for _sample in range(n):
            try:
                candidates.append(await self.inner.propose(ctx))
            except ProposerError as exc:
                # A candidate the inner proposer could not produce simply
                # narrows the slate; remember the error so an all-failed
                # slate can re-raise the real failure.
                last_error = exc

        if not candidates:
            # The whole slate failed — surface the inner failure exactly as a
            # single propose would (the caller's rejected-outcome path handles
            # it). ``last_error`` is set because n >= 2 means the loop ran.
            if last_error is not None:
                raise last_error
            raise ProposerError(["best-of-N produced no candidates"])  # pragma: no cover

        if len(candidates) == 1:
            return candidates[0]

        chosen = await self._select_best(candidates, ctx)
        return candidates[chosen]
```

When enabled, the self-critique pass sees **only** the same restricted, overfitting-safe context the proposer saw (aggregated train-slice patterns, banded experiment memory, bucketed failure profile — never the holdout); a flaky critic falls back to a deterministic heuristic (grounded-in-an-observed-failure-mode first, then minimal diff, then stable order — `best_of_n.py:65-96`).

### How to improve it (ranked)

Grounded in `docs/design/FUNCTIONALITY-RECOMMENDATIONS.md §4`, and reconciled against `main` (the three §4 levers all landed via `feat/proposer-quality` — see the [branch audit](#branch-audit-main-is-the-union-of-the-merged-feature-work)), the ranked levers — all of which stay inside the existing overfitting-restricted context channels — are:

1. **Enable best-of-N + self-critique (top lever — built, default-off).** The wrapper is merged but defaults to a single sample (`best_of_n = 1`). Sampling N and critiquing against the quality bar (grounded in a tool call? targets a real failure mode? minimal diff?) is the biggest generation-quality win and needs no new code — only a non-default `best_of_n` in the scoring contract (which correctly rolls the epoch, since a self-critiquing proposer proposes *differently*).
2. **Make hypothesis prediction-accuracy actionable (built, but advisory-only).** The grading already exists — `grade_hypothesis_predictions` scores each settled hypothesis against actuals and folds a banded `prediction_accuracy` into experiment memory, shown to the proposer as a `prediction:low|medium|high` annotation. The remaining work is to *use* the signal: e.g. bias best-of-N selection toward candidates whose hypothesis shape has historically been well-calibrated, or down-weight experiment-memory entries from chronically over-confident lineages. Keep it out of the promotion gate (diagnostic discipline), but let it shape *generation*.
3. **Add deliberate sampling diversity within a best-of-N slate (genuinely unbuilt).** The field-diversity constraint that already exists (`_duplicates_inflight_sibling`, `orchestrator.py`) only de-duplicates *across* in-flight multi-challenger siblings by `(modulating set, core_idea)`; the N samples *inside* one best-of-N slot are still i.i.d. draws from the same prompt. Vary them deliberately — distinct temperatures, distinct edit-class hints, or a "propose a structurally different fix" instruction per sample — so the critique pass chooses among genuinely different strategies rather than N paraphrases.
4. **Targeted failure-mode → edit-class prompting.** The failure profile already classifies modes (over-retrieves / misses / empty / looping); inject a mode-specific instruction instead of leaving the proposer to infer the remedy.
5. **Richer mutation tooling.** The default registry is read-only and lean (`list_mutation_points, read_mutable_file, grep_mutable, read_journal, read_insights`); add a `read_parent_diff` (what the last promotion changed) and a `mutation_usage` (where an id's value is referenced) tool, plus a soft "ground before proposing" nudge.

---

## Branch audit (main is the union of the merged feature work)

This section was added after the first draft, in response to "check the branches for potential improvements." The finding is that **`main` already contains the substantive feature work** — the ~82 branches are overwhelmingly *merged* history, not a reservoir of unlanded improvements.

**Method.** For every local and remote branch, `git rev-list --count main..<branch>` (commits on the branch not yet in `main`) and its inverse. Every `feat/*` and hardening `fix/*` branch that maps to this document's themes reports **ahead 0** — i.e. fully merged, with `main` 130–260 commits *past* it:

| Branch | Ahead of main | Theme it lands | Status on `main` |
|---|---|---|---|
| `feat/proposer-quality` | 0 | best-of-N + self-critique, prediction-accuracy, field diversity | merged (`cf381ee`) |
| `feat/supervisor-hardening-and-evidence` | 0 | watchdog / evidence surface | merged |
| `feat/resume-protocol` | 0 | conservative crash-resume | merged |
| `fix/watchdog-no-orchestrator-kill` | 0 | warn-only watchdog | merged |
| `feat/selection-rating-resolution` | 0 | opt-in Bradley–Terry rating + winner-resolution | merged (default-off) |
| `feat/elo-analytics` | 0 | Elo fold over the match ledger | merged (read-only) |
| `fix/mutation-edit-robustness` | 0 | robust span/literal edits | merged |
| `feat/execution-efficiency` | 0 | worker kill escalator | merged |
| `fix/tournament-execution-hardening` | 0 | abort-cause / no-cache-on-infra-abort | merged |

The only branches *ahead* of `main` are 1-commit pre-merge snapshots of already-merged design docs (`docs/reimplementation-design`, `docs/generation-isolation`, `docs/board-reflection`, `docs/functionality-recommendations`) and two tiny refactor/fix branches (`refactor/split-report-figures` +2, `fix/issue-16-inflight-round` +1) whose content is superseded. **There is no unmerged branch carrying a substantive improvement.** *(Correction, 2026-07: `docs/generation-isolation` was **not** a snapshot of an already-merged doc — it carried a design note `main` had never had. It has since landed, reheadered as a superseded decision record: [GENERATION-ISOLATION.md](GENERATION-ISOLATION.md). The audit's conclusion is unaffected — the note's recommendation was rejected, so the branch carried no unbuilt improvement — but the "already-merged" characterisation was wrong.)*

**Consequence for this document.** The "improvements" worth acting on are therefore *not* on branches waiting to be pulled — they are (a) the merged-but-**default-off** opt-ins already in the tree, and (b) the genuinely-unbuilt items in the design backlog. In particular, this reconciliation **corrected the proposer section**: all three `FUNCTIONALITY-RECOMMENDATIONS.md §4` levers (best-of-N + self-critique, hypothesis prediction-accuracy grading, field-diversity constraint) are implemented and merged — the first draft, scoped to `src/zicato/proposer/`, missed the grader under `tournament/detail.py` + `index/query.py` and the diversity hook in `orchestrator.py`. The live opt-in surface an operator can turn on today (each rolls the epoch by folding into the contract hash) includes: `proposer_quality.best_of_n > 1`, the Bradley–Terry **rating** layer (`params["rating"]` θ-rank standings), the *winner-resolution* **resolver** (`params["resolver"]` — Ranked Pairs behind a Smith-set prune, `selection/resolve.py`; only the maximal-lottery resolver remains unbuilt), and the read-only Elo analytics fold (`src/zicato/index/elo.py`, also PR #90 — a BT MLE mapped onto the Elo scale). PR #90 additionally merged the recombination slot, the genealogy channel, and the ensemble proposer roles.

Genuinely-**unbuilt** items remaining in the backlog (per `OVERFITTING.md` / `FUNCTIONALITY-RECOMMENDATIONS.md`): deliberate intra-slate sampling diversity. (Turning the alarm-only diff-containment / promotion-gate-contradiction checks into hard blocks has since SHIPPED as opt-in, default-off in-band blocking twins — `ScoringWeights.block_on_containment_violation` and `block_on_gate_contradiction`, enforced pre-persist in `orchestrator._integrity_block_reason` (mirror in `evolve/gate.py`): when on, a violating promotion is flipped to REJECTED with a `containment_violation` / `gate_contradiction` reason instead of promoted-with-alarm, tested in `tests/test_integrity_blocking.py`; the supervisor's out-of-band scan stays alarm-only.) (Diff-complexity regularization — OVERFITTING #4 — and the random-baseline challenger — OVERFITTING #7 — have since SHIPPED: diff-complexity #4 in FULL — both the **loss term** (`diff_complexity_weight`) and the complexity-*ceiling* half (`diff_complexity_ceiling`, a Rule-0 reject in `tournament/gate.py`), both default-off — and the random-baseline as the opt-in placebo arm.)

## Code quality: is the verbosity merited?

This section (added after the first draft, in response to "what about the code-quality opportunities — is the verbosity merited?") is grounded in a metrics census plus three parallel deep-read audits of the biggest offenders.

### The measured shape

| Signal | Value | Read |
|---|---|---|
| `src/zicato/` size | 85,226 lines / 235 files / 2,081 functions | — |
| Executable code | **46.8%** | the rest is prose + blank |
| Docstrings / comments / blank | **31.4% / 8.6% / 13.2%** | 40% of the file is natural-language prose |
| Functions > 100 lines / > 60 lines | **81 / 210** | the long tail is real |
| Largest functions | `_evolve_multi_challenger` **1,181L / cx~113**, `make_endpoints` **966L / cx~111**, `evolve_once` **881L / cx~43** | three functions ≈ 3,000 lines |
| Rough clone rate (repeated 6-line windows) | **~15.9%** of logic lines | concentrated in `epoch.py`, selection strategies, CLI scaffolding, dashboard readers |
| Test : source | **77,222 : 85,226 ≈ 0.9 : 1** | a strength, and inflates total LOC |
| Dashboard footprint | **~30% of non-test source** (11.2k py + ~28k prod JS/CSS) — largest single subsystem | |
| Defensive "byte-identical / no-op / LOAD-BEARING" comments | **~331** (180 `byte-identical`, 105 `no-op`, 12 `LOAD-BEARING`) | mixed: a merited core + a thinnable habit |

### Verdict: **the verbosity is ~80% merited**

The **prose is mostly load-bearing, not padding.** In a system that *rewrites its own code*, the docstrings *are* the spec, co-located with the code they govern: overfitting boundaries ("the proposer/critic never sees the holdout"), the idempotent crash-resume protocol, and genuinely bit-level invariants a future editor would silently break — e.g. the deliberately non-associative summation order in `src/zicato/scoring/builtins.py:115` ("float addition is not associative … the dict-then-`sum` shape is load-bearing for the byte-identical guarantee"). The **tests (~0.9:1)** and the **~14.5k-line Rust safety layer** legitimately account for much of the bulk, and the **~40k-line dashboard frontend is live, tested code** — the A–W bake-off losers were archived at git tags (`dashboard-bakeoff-2026-06-01`), not shipped; only ~341 JS lines are actually dead.

The **~20% that is *not* merited is structural debt, not prose**, and it is concentrated and namable:

1. **Three god-functions carry disproportionate complexity.** `_evolve_multi_challenger` (1,181L, cx~113) and `evolve_once` (881L) in `orchestrator.py`, and `make_endpoints` (966L, 54 handlers) in `dashboard/endpoints.py`, are only *end-to-end* testable — their interesting branches (diversity soft-reject, holdout demotion, the crowning-invariant `raise`) can't be reached by a unit test today. This is a correctness risk, not just an aesthetic one.
2. **The single- vs multi-challenger evolve paths duplicate the entire back half of the pipeline.** `orchestrator.py` runs `propose → apply → validate → score → finalize → lineage → journal → epilogue` twice with ~250 near-identical lines, plus a *third* divergent persist tail for the rejection branch — so a new `OutcomeRecord` field added in one tail can silently be dropped in another.
3. **The dashboard readers never hoisted their shared helpers.** The same `float(x) if isinstance(x, int|float) else None` coercion appears **51 times** across five ~1,000-line readers with no shared helper (`readers/paths.py` is the natural home).
4. **Small, safe deletions.** ~6 dead private functions, 341 lines of dead JS, and a 2.2 MB local `./zicato/` `.pyc` graveyard (orphaned by the May src/-layout migration — untracked/gitignored, so `rm -rf ./zicato` is zero-risk).
5. **A thinnable comment habit.** ~77 of the 331 defensive comments are per-branch "…byte-identical to today" tags that restate the `if x is not None:` guard they sit above; the ~12 `LOAD-BEARING` markers and the bit-level rationales should stay verbatim.

None of this needs rearchitecting. The whole list is a bounded, behavior-preserving cleanup that would remove ~500–800 lines and convert several end-to-end-only branches into unit-testable helpers.

### Concrete opportunities (ranked, all file:line-grounded)

**Decomposition (correctness + testability — do first):**
- Extract the duplicated evolve tails in `src/zicato/orchestrator.py` into shared helpers: `_finalize_generation(...)` (`:924–1003` ⟷ `:2513–2597`), `_round_epilogue(...)` (`:1005–1055` ⟷ `:2599–2623`), `_propose_child(...)` (`:616–657` ⟷ `:1261–1320`), and `_persist_rejected_round(...)` (`:687–752`). Removes ~250 duplicated lines and the field-drift risk across the three persist tails.
- Pull the *pure decisions* out of `_evolve_multi_challenger` so they're unit-testable without a live round: `_mint_challenger_field(...)` (`:1709–1862`), `_apply_field_overrides(...)` (`:2277–2366`), `_confirm_crowning_on_holdout(...)` (`:2202–2258`).
- Decompose `make_endpoints` (`src/zicato/dashboard/endpoints.py:118`) into per-surface factories; cleanest first seam is the control cluster (`:894–1016`) → `control_endpoints(ctx)`; collapse the near-identical `control_pause`/`control_skip_round` (`:955–963` / `:965–973`).

**Deduplication:**
- Add `coerce_float` / `coerce_numeric_dict` to `src/zicato/dashboard/readers/paths.py`; replaces 51 inline coercions (judge_view 15×, tournament_view 13×, epoch_view 8×, gate_view 7×, run_log 4×). ~80–100 lines.
- `tournament_view.py`: extract `_build_structure_dict(...)` (`:822–832` / `:857–867` / `:924–934`, 3× identical but the `"source"` value) and `_normalize_scalar_pair(...)` (`:697–707` / `:889–897`).

**Deletion (zero-risk):**
- `rm -rf ./zicato` (2.2 MB orphaned `.pyc`, untracked). Delete dead JS `js/core/router.js` (101L) + `js/core/hypothesis_block.js` (240L). Remove 6 dead private fns: `_decision_color` (`epoch/html_report.py:112`), `_render_drift_table` (`html_report.py:978`), `_generation_decision_color` (`analyzer/svg/palette.py:73`), `_heatmap_color` (`svg/heatmap.py:149`), `_has_tests_dir` (`tournament/regression.py:99`), and — pending an author check — `_is_valid_agent_name` (`telemetry/harmonograf_supervisor.py:833`).

**Prose hygiene (low priority):**
- Thin the ~77 repetitive "byte-identical to today" per-branch tags by hoisting the "defaults leave the path unchanged" invariant to one line in each function's docstring; keep every `LOAD-BEARING` marker and bit-level rationale. Back the "byte-identical when off" claims with tests (many already are) so the prose can't drift from behavior.

## Persistence: is the approach sensible? (and how to simplify it)

**Verdict: sensible, with caveats.** The claims here are verified against the
implementation and its cross-backend conformance tests. New workspaces explicitly
select Git; directory snapshots remain a supported fallback.

### The spine: CQRS, verified in code

The organizing principle is a disciplined **command/query split — files are the store of record; everything queryable is a derived, disposable projection.** This holds in the code, not just the prose:

- **New workspaces explicitly select the content-addressed git backend** — a generation is a commit on an epoch branch, tagged, materialised for a run as a `git worktree`; the object store deduplicates unchanged blobs across a lineage.

```python
# src/zicato/epoch/genstore.py
    backend = resolve_generation_store_backend(workspace_root)
    if backend == "git":
        from zicato.epoch.git_genstore import GitGenerationStore  # noqa: PLC0415
        return GitGenerationStore(workspace_root)
    return DirectoryGenerationStore(workspace_root)
```

- **The SQLite index is a pure read-model that can only lag, never lead.** Every call site writes the canonical file first, then does a best-effort index ingest that swallows all exceptions — so a phantom index row is structurally impossible and the lag heals on the next ingest/`reindex`:

```python
# src/zicato/orchestrator.py:3283-3289
    except ImportError:
        log.debug("zicato.index.ingest unavailable; skipping live index dual-write")
    except Exception as exc:  # noqa: BLE001 — index write is best-effort
        log.debug("live index ingest_experiment skipped for %s/%s: %s", epoch_id, generation_id, exc)
```

The store topology is coherent — six stores with disjoint roles: generation trees (canonical, git) · lineage/experiments/journal JSON (canonical decisions) · runtime state files (derived/ephemeral, discarded and rebuilt on resume) · JSONL telemetry (canonical drift facts, its own root) · SQLite index (derived, rebuildable via `reindex`) · the opt-in hash-chained ledger (advisory, never a gate). Two canonical roots feed the index — the telemetry chain (`events.jsonl → reducer → loss.json`) and the decision chain (`experiment.json`) — and they own disjoint facts, so this is separation, not divergence. The atomic-write primitive (tmp → `fsync(fd)` → `os.replace`) is defined once and reused everywhere; the Rust supervisor opens the *same* SQLite file `READ_ONLY` with a pinned schema-version tripwire; migrations are additive/idempotent/versioned. None of this needs redesign.

### Durability and retention

Atomic record writes fsync the temporary file, replace the destination, and
fsync the parent directory. Append-only JSONL readers tolerate only a torn
final line. Snapshot GC prunes rejected generation source trees behind
`GenerationStore.prune_generations` and preserves experiment, patch, score,
lineage, journal, and run records. Git worktree administration remains under
the repository lock, including pruning.

### Resulting simplification

The store *count* is not the problem — each store has a distinct consumer. The
structural duplication has been removed at their boundary:

- Per-run isolation lives behind `GenerationStore.checkout_ephemeral`. The
  directory backend copies; the Git backend creates a detached per-run worktree;
  the worker transport only retains a copy fallback for store-unmanaged library
  callers.
- Pure `snapshot_path` calculation is separate from I/O-performing
  `materialize_snapshot`, so callers cannot accidentally create worktrees while
  computing coordinates.
- `GenerationStore` owns source derivation, browsing, diffs, checkout, and
  pruning. Experiments and patches are read through `StorageBackend`, including
  after source pruning. Commit metadata is an operator-readable redundant copy,
  never a second canonical record.
- The rejected overlay/reflink materialization design remains only as a decision
  record. Git blob dedup and worktree isolation already provide the required
  storage and execution properties.

The two generation backends remain justified: both conform to one protocol, and
the directory implementation supports hosts where a private Git repository is
unwanted. `lineage.json` is not redundant with the Git DAG: Git records source
parentage, while lineage and experiment records own tournament decisions.

## The frontend/data-model boundary (and how to simplify it)

**Verdict: the frontend meaningfully compensates for server/data-model gaps** — though less than the raw grep counts imply (most `Math.`/`toFixed` is legitimate SVG geometry and display formatting).

### Root cause: notify-only SSE + rawish payloads → the client re-derives

The SSE stream ships **no data** — only which regions changed plus a liveness cursor:

```python
# src/zicato/dashboard/sse.py:255-262
                "data": {
                    "type": "state_change",
                    "kind": kinds[0] if len(kinds) == 1 else "multiple",
                    "kinds": kinds,
                    "seq": seq,
                    "terminal": terminal,
```

That is a fine design *in itself* (thin "something changed, re-fetch" signal + authoritative REST readers). The problem is one layer out: the readers compute authoritative values but the REST payloads don't consistently *carry* them, so every view re-fetches and re-derives. This produces three classes:

1. **Correctness risk — the client can disagree with server truth.** The JS re-implements the promotion classifier as a *substring* match, divergent from the server's canonical set:

```javascript
// src/zicato/dashboard/static/js/ui.js:422  — recognises only promot/reject/defer
  if (raw.includes('promot')) return 'promoted';
```
```python
# src/zicato/dashboard/readers/lineage_view.py:21  — the canonical set
_PROMOTED_DECISIONS = frozenset({"promoted", "promote", "accepted", "accept", "win", "won"})
```

   If the server ever emits `accepted`/`win`/`won`, the client silently disagrees. Likewise the JS `gauntletModel` gate fallback (`structure.js:2321-2323`) is a **single scalar-margin** test (`scalar < championScalar - promoteMargin`), while the server gate evaluates *"regression suite → scalar margin → pass-rate monotonicity → namespace monotonicity"* (`gate_view.py:734`) — a challenger that improves scalar but regresses a predicate is `failed` server-side, `cleared` in the client fallback. And champion identity is re-scanned client-side across several views (`genList.find(g => g.promoted) || genList.find(g => !g.parent)`), so a wrong pick corrupts the whole candidate dossier.
2. **Structural gaps — the server ships no pre-shaped payload, so the client fabricates authoritative outcomes.** `reconstructRacing` joins per-challenger rows into a rung ladder by **parsing roles out of `{epoch}:{champ}->{chall}` id strings** (there is no racing field record), and the entire cross-round epoch timeline/waterfall is stitched from four endpoints (there is no round-timeline reader).
3. **Drift risk — duplicated logic/constants.** A decision→color palette that "mirrors `html_report.py`", `DEFAULT_MARGIN = 0.05` in JS vs `0.01` server-side, the Copeland/successive-halving formulas hard-coded in the view, and the gate margin/regressed-predicate **scraped from a free-text `rule.detail` string with regex**.

Underneath, **loose data-model typing forces client coercion** — `promoted` in two shapes, `pass_fail` number-or-boolean, the board id under three field names (`entry_id | board_entry_id | entry`), a heartbeat that guesses sec-vs-ms — the frontend mirror of the 51 server-side numeric coercions from the code-quality audit.

### How to simplify it

One architectural move — **push authority and shape to the server; let the client render, not compute** — collapses all three classes:

1. **Stamp the canonical decision surface onto the payloads.** Add `decision` + `promoted` per generation, a `current_champion` pointer, and `deciding_rule` on the gate; add an epoch-scoped generations feed. → deletes `normaliseDecision`, the champion re-scan across ~5 views, the deciding-rule re-inference, and the free-text margin/predicate scraping.
2. **Serve the structural shapes the client currently fabricates** — a settled racing-ladder record (like the other structures) and a round-timeline/waterfall endpoint. → deletes `reconstructRacing` (~200L) and most of `rounds.js` (479L), the two largest client joins.
3. **Canonicalize the loose schema** — one spelling per field, a single typed heartbeat timestamp, `promoted` in one shape. → deletes the client coercion family.
4. **Then drop the client "mirror" gate/standings models.** They exist partly to keep the *in-flight* view honest before a round settles; once the readers project authoritative **live** state (the runtime already has the tournament event log to do it), the `gauntlet/elim/swiss/racing` model re-derivations can read rather than recompute.

Rough magnitude: **~800–1500 lines of client re-derivation removable**, plus the elimination of the entire *client-can-disagree-with-server* bug class. The unifying insight across this section and [Persistence](#persistence-is-the-approach-sensible-and-how-to-simplify-it): storage already applies the right discipline (derive read-models server-side from canonical files — CQRS); the dashboard *violates* it by re-deriving authoritative decisions in the view. Making the dashboard payloads as authoritative-and-derived as the SQLite index is the same principle applied one layer out.

## Empirical effectiveness: before and after

*(Added at the close of the improvement program's Phase 1; the sections above describe the pre-Phase-1 state.)*

**Before.** zicato had no in-repo evidence its loop could improve anything: the one documented live run was a null result ("v0 and v1 scored identically (1.000000); zero optimization signal" — and structurally so: the target's mock discarded the `system` prompt, so no mutation could ever change behavior). No known-answer convergence test existed, and the default decision procedure was noise-blind (point-estimate gate, fixed uncalibrated margin, replicates=1, the Bradley–Terry evidence gate structurally unreachable under the default gauntlet).

**After (Phase 1, PRs #63–#66).**

- **The loop provably converges** (#64): a planted-defect target driven through the *full real pipeline* (propose → apply → validate → subprocess workers → gate → persist, git-backend default, zero tournament monkeypatches) goes v0 scalar 3.6 → **promoted 2.4** → negative-control **rejected** → **promoted to the exact 1.2 floor, bit-for-bit**, with health detectors quiet — in CI, ~14s.
- **The decision procedure's operating characteristics are measured, in CI** (#65): A/A noise floor 0.598 (σ=0.22 harness); the naive default (margin < floor, no gate) promotes **pure noise 20/60** while the evidence gate blocks **60/60**; power at a small true effect (0.56× floor): **0.92 effective vs 0.25 naive** on identical seeds. Measured limit: the BT gate separates CIs only after **~37 unbroken wins** — it is a *soundness* device; *power* must be bought with per-duel replication. This finding redirected the defaults (gate opt-in + scaffolded, not silently on — which would have frozen the loop).
- **The defaults are now noise-aware** (#66): evidence gate reachable under every structure including the gauntlet crowning duel; A/A noise-floor calibration (`zicato board audit`, epoch record, evolve-start warning when margin < floor); replicates 1→2, best_of_n 1→3 (self-critique on), holdout split floor 8→6; `zicato init`/builder scaffold the full recommended contract.
- **Two real bugs surfaced by the harness work**: the worker's canonical `loss.json` doubled as replicate 0's cache slot, silently clobbered by later replicates (fixed in #65); and a 20s hard-coded no-supervisor abort wait (now a `RuntimeConfig` knob, #63).
- **Suite economics** (#63): 56s → ~21s full, 14.4s fast lane — the standing oracle is cheap enough to run always.

Still unproven (endpoint-gated backlog): behavior with a *real* proposer/judges — the live convergence run, real noise-floor measurement, judge test-retest calibration, and the racing×BT×best-of-N shakeout await a serving model endpoint.

## Summary of recommendations

**Proposer (highest leverage — this is where loop quality is won):**

1. **Enable best-of-N + self-critique.** Already implemented (`src/zicato/proposer/best_of_n.py`) and overfitting-safe, but default-off (`best_of_n = 1`). Setting `best_of_n > 1` in the scoring contract is the single biggest, lowest-cost proposal-quality win; the retry loop today only reconsiders *invalid* output, never a valid-but-mediocre one.
2. **Make hypothesis prediction-accuracy actionable.** The grading exists (`grade_hypothesis_predictions` → banded `PriorExperiment.prediction_accuracy`) but is advisory-only — displayed to the proposer, never used to bias selection. Wire it into best-of-N selection / experiment-memory weighting; keep it out of the promotion gate.
3. **Add deliberate diversity *within* a best-of-N slate** (distinct temperatures / edit-class hints per sample — the existing `_duplicates_inflight_sibling` constraint only de-dupes *across* multi-challenger siblings, not the i.i.d. samples inside one slot) and **inject failure-mode-specific edit-class prompts** from the already-computed failure profile; optionally add `read_parent_diff` / `mutation_usage` grounding tools.

**Determinism / safety (already strong — observations, not gaps):**

- The safety model is sound by construction: the watchdog is warn-only toward the orchestrator (no `Kill` variant), every signal is pid-start-time-checked against reuse, the orchestrator-written deadline is clamped, and reaping is gated on a *confirmed-dead* identity check rather than a stale timestamp. The independent hash-chained ledger, diff-containment re-hash, and re-derived promotion gate make the loop self-auditing.
- Two integrity checks began **alarm-only in v1** — diff-containment violations and the promotion-gate contradiction check, whose out-of-band supervisor scan *records* findings but does not *block* a promotion. Each has since gained an opt-in, default-off IN-BAND blocking twin (`ScoringWeights.block_on_containment_violation` / `block_on_gate_contradiction`): when enabled, the orchestrator re-checks the same rule surface pre-persist (`_integrity_block_reason`) and flips a violating promotion to REJECTED with an honest reason, instead of promoting-with-alarm. Default-off keeps the shipped alarm-only posture (the supervisor's scan stays the out-of-band notary); turning the block on is the policy decision, no new mechanism. See `tests/test_integrity_blocking.py`.
- The crash-resume protocol is conservative by design (discard on any ambiguity; resume-in-place only when experiment + snapshot + ≥1 cached loss all agree), which trades a little re-run cost for zero-corruption lineage — the right default.

**Verbosity (a feature, with a caveat):**

- The ~85k-line Python core is only ~47% executable code; ~40% is docstrings and comments carrying the design rationale inline. For a system that rewrites code, this co-location of spec-with-code is the right trade — it is what keeps a self-modifying loop auditable. The caveat is maintenance drift: because the same invariant (e.g. "byte-identical when off") is asserted in prose across dozens of opt-in wrappers, those assertions should be backed by tests (several already are) rather than trusted as comments, so the prose cannot silently diverge from behavior. See the [code-quality audit](#code-quality-is-the-verbosity-merited) for the concrete, reducible debt (god-functions, duplication, dead symbols) — the prose is *not* where the fat is.

**Code quality (verbosity is ~80% merited; the reducible ~20% is structural, not prose):**

1. **Decompose the three god-functions.** `_evolve_multi_challenger` (1,181L/cx~113) + `evolve_once` (881L) in `orchestrator.py` and `make_endpoints` (966L) in `dashboard/endpoints.py` are only end-to-end testable. Extract the duplicated evolve tails (`_finalize_generation`, `_round_epilogue`, `_propose_child`, `_persist_rejected_round` — ~250 lines removed and the three divergent persist tails unified) and split `make_endpoints` into per-surface factories. Highest value: this converts end-to-end-only branches into unit-testable helpers.
2. **Hoist the duplicated helpers.** Add `coerce_float`/`coerce_numeric_dict` to `dashboard/readers/paths.py` (kills 51 inline copies) and factor `tournament_view.py`'s 3× structure-dict / 2× scalar-pair builders. (~15.9% of logic lines sit in a repeated 6-line window.)
3. **Delete the zero-risk residue.** `rm -rf ./zicato` (2.2 MB orphaned `.pyc`), 341 lines of dead JS, and ~6 dead private functions. Thin the ~77 "byte-identical to today" per-branch comment tags (keep every `LOAD-BEARING` marker).

**Persistence (sensible spine; consolidation complete):**

1. Per-run isolation is behind `GenerationStore.checkout_ephemeral`; Git no
   longer pays an additional directory copy in the worker transport.
2. The overlay/reflink materialization proposal is retained only as a rejected
   design record; no third mechanism is planned.
3. Atomic replacement fsyncs the parent directory, and snapshot retention prunes
   source trees behind the store protocol while preserving every record.

**Frontend / data-model boundary (push authority server-side):**

1. **Stamp the canonical decision surface** — `decision` + `promoted` + `current_champion` + `deciding_rule` onto the epoch/experiment and gate payloads, plus an epoch-scoped generations feed. Retires `normaliseDecision` (which diverges from the server's promotion set), the champion re-scan, and the one-rule client gate that can disagree with the server's multi-rule gate.
2. **Serve the shapes the client fabricates** — a settled racing-ladder record and a round-timeline endpoint — deleting `reconstructRacing` and most of `rounds.js` (the two largest client joins, both of which currently invent authoritative outcomes in the view).
3. **Canonicalize the loose schema** (one field spelling, a typed heartbeat) to delete the client coercion family. Net: ~800–1500 lines of client re-derivation removed and the *client-can-disagree-with-server* bug class eliminated — the same CQRS discipline storage already uses, applied one layer out.

_Note to the reader of this assembly: sections 1-3 and 5 are reproduced verbatim from independently verified drafts; sections 4 and 6 were reconstructed by the editor directly from the cited source files (`src/zicato/proposer/*`, `src/zicato/core/scoring_config.py`, `docs/design/FUNCTIONALITY-RECOMMENDATIONS.md`) because their verified drafts were not supplied — every code snippet in them is copied verbatim from the captioned file and line range._
