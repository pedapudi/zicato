# 05 — The Proposer

> **Covers:** the entire proposer subsystem — how a proposal resolves
> behind `ProposerAgent`, every field of `ProposerContext`, the episode loop
> (edit a working copy, verify it, return a hypothesis), the structured-output
> schema and its two-pass validation, best-of-N sampling + screening + critique
> + the tree/record agreement invariant, the round-log vocabulary the propose
> step emits, the **restricted-visibility envelope** as a formal spec, the
> proposer's tool surface, and experiment memory (same-epoch + cross-epoch).
>
> **Prerequisites:** 02-architecture.md §3 (where the propose step sits in a
> round — §3.9 is the propose step itself), 03-contract-and-epochs.md §3.2.6
> (why editing the proposer rolls the epoch), 04-evaluation-statistics.md §5
> (the Ladder budget over the holdout). The split itself is
> `zicato.board.split` under `docs/design/OVERFITTING.md` §3; everything the
> proposer sees is train-slice-only.
>
> **Invariants you must not break (each is expanded below):**
> 1. **The proposer never sees the holdout, an entry id, task text, or a raw
>    per-entry outcome.** Every channel into the prompt is banded, aggregated,
>    anonymized, or redacted at the render boundary (§5.8).
> 2. **The mounted child tree must match the CHOSEN experiment** — Cases 6
>    and 7 of 12-bug-casebook.md (§5.6.5).
> 3. **A screen can veto, never rank; and it can never fail a propose step**
>    (§5.6.2).
> 4. **The evidence the proposer sees is assembled by the caller** — the
>    proposer never reads the index or the board itself; the orchestrator
>    assembles every channel and threads it on the context, and
>    `evidence_from_context` projects it rather than re-deriving it.
> 5. **`ProposerError` is the only failure contract.** Every agent
>    implementation raises it when it produced no experiment; `ProposerBlocked`
>    and `ProposerExhausted` are subclasses, so a call site written for the
>    base class handles all three (rejected-round journaling, narrower fields).
> 6. **Byte-identical-at-default.** Every optional channel renders the empty
>    string / omits its section when unused, so a contract that does not opt in
>    produces byte-identical prompts (and an unchanged contract hash).

---

## 5.0 Map of the subsystem

| File | What lives there | Approx. size |
|---|---|---|
| `src/zicato/proposer/agent.py` | `ProposerContext` (the frozen call-time bundle), the `ProposerAgent` protocol, `build_proposer_agent` (the resolver) | — |
| `src/zicato/proposer/foe_agent.py` | `FoeProposerAgent` — one Foe episode per candidate: the host tools, the episode lifecycle, the outcome-to-experiment conversion, the contract identity | — |
| `src/zicato/proposer/foe_request.py` | `build_request` — the ONE request builder, for the loop and the CLI: the charter, the sanctioned tool list, the hypothesis schema, `render_evidence` / `render_task` | — |
| `src/zicato/proposer/foe_config.py` | The typed `proposer` block (binary, budget, model, viewer) and the refusals for a removed runtime's configuration | — |
| `src/zicato/proposer/foe_scratch.py` | The disposable working copy, and the projection that reads it back as a patch set over the declared mutation points | — |
| `src/zicato/proposer/external.py` | The seam that NAMES an implementation and HASHES it — `external_proposer_config`, `resolve_external_spec` | — |
| `src/zicato/proposer/proposer.py` | The four episode outcomes (`ProposerError`, `ProposerBlocked`, `ProposerExhausted`) and `ExperimentValidator` | — |
| `src/zicato/proposer/prompts.py` | Every block renderer; ALL banding/aggregation happens here (`_aggregate_pattern_detail`, `_bucket_scalar_delta`, `_band_rate`, `_band_quality`, …) | — |
| `src/zicato/proposer/structured.py` | `EXPERIMENT_JSON_SCHEMA`, `parse_experiment_json` (two-pass validation), `extract_json_object` (5-stage salvage), `ExperimentParseError`, `PostApplyValidationError` | 880 lines |
| `src/zicato/proposer/best_of_n.py` | `BestOfNProposerAgent` (slate sampling, screen, revise, critique, heuristic, `_mount_chosen`), `CandidateScreenResult`, `ScreenRunner`, `wrap_with_proposer_quality` | 1743 lines |
| `src/zicato/proposer/hints.py` | `EDIT_CLASS_HINTS`, `FAILURE_MODE_HINTS`, `hint_for_slot`, `dominant_failure_mode` — the per-slot slate diversifier | 256 lines |
| `src/zicato/proposer/tools.py` | `mutation_usage` (a host tool) and `grep_mutable`, the sandboxed search behind it | — |
| `src/zicato/proposer/tool_context.py` | `ProposerToolContext`, `bind_proposer_tool_context` — the contextvar seam both host tools read | — |
| `src/zicato/proposer/brief.py` | `ProposerBrief` / `load_brief` / `enforce_forbidden` — the operator's `brief.md` parser | 217 lines |
| `src/zicato/proposer/skills.py` | `resolve_proposer_spec`, `load_proposer_skills`, `normalize_skill_body`, `parse_frontmatter` | 169 lines |
| `src/zicato/core/proposer.py` | `ProposerSpec` / `ProposerSkill` — the hash-ready proposer identity types | — |
| `src/zicato/epoch/screen.py` | The candidate-screen engine (`run_candidate_screen`, `select_screen_entries`, `ScreenPanel`, `SCREEN_REPLICATE_BASE`) | 468 lines |
| `src/zicato/analyzer/process_exemplars.py` | The four-rule redaction machine (§5.8.3) for the opt-in process-exemplar channel | 678 lines |
| `src/zicato/index/query.py` | `prior_experiments_for_epoch` (experiment memory), `mutation_point_track_record` (fertility map) | — |
| `src/zicato/epoch/round_log.py` | The round-log event vocabulary the propose step emits into | 974 lines |
| `src/zicato/evolve/` | The wiring, spread across the round pipeline: `propose_apply.py` (`_propose_child`, `_propose_and_apply_challenger`), `round_context.py` (`_build_candidate_screen_runner`, `_build_recombination_pair`, `_build_genealogy_items`), `decision_support.py` (`_render_failure_profile`, `_render_process_exemplars_block`, `_render_loss_summary`), `ingest.py` (`_load_prior_experiments`, `_load_mutation_track_records`). `src/zicato/orchestrator.py` is a 14-line re-export facade over `evolve_once` / `evolve_n_rounds` | — |

Orchestrator call topology, per round:

```
evolve_once (evolve/round_entry.py)
 ├─ enumerate_mutations → split_board(train/holdout) → detect_patterns(TRAIN)
 ├─ _render_loss_summary(TRAIN losses)
 ├─ _render_failure_profile(TRAIN losses, weights)          # banded block or ""
 ├─ _render_process_exemplars_block(...)                    # redacted block or ""
 ├─ _build_candidate_screen_runner(...)                     # closure or None
 ├─ _load_prior_experiments(...)                            # memory digest or []
 └─ propose site(s):
     gauntlet: _propose_child(proposer_agent, ProposerContext(...))
     field:    _propose_and_apply_challenger × field_size   # (also via _propose_child)
                └─ prior_experiments = prior + tuple(siblings)   # in-flight cohort
```

The agent itself is built ONCE per evolve invocation:
`build_proposer_agent(spec, proposer_path, external_config)` wrapped by
`wrap_with_proposer_quality(inner, weights.proposer_quality)`.

> ⚠️ TRAP — a new `ProposerContext` field must be threaded at BOTH propose sites.
> `_propose_child` (`src/zicato/evolve/propose_apply.py`) is the single shared
> context-builder both pipelines call, so that a new field cannot reach one
> path only. If you add a field, add it to `_propose_child`'s signature and
> to BOTH of its callers (the gauntlet inline block and
> `_propose_and_apply_challenger`), or the field silently defaults on one
> pipeline.

---

## 5.1 How a proposal resolves behind `ProposerAgent`

Two things decide it, and they live in different files.

A workspace's `config.json` says **how it proposes**: the typed `proposer`
block (`src/zicato/proposer/foe_config.py`) names the Foe binary its
episodes run, the budget they run under, the model Foe's transport calls,
and the viewer policy. An epoch's `proposers/<name>/` directory says
**how the proposer is steered**: `skills/*.md`, whose bodies are hashed
into the contract. The directory holds nothing executable.

`external_proposer_config` (`src/zicato/proposer/external.py`) reads the
first, `resolve_proposer_spec` (`skills.py`) reads the second, and
`resolve_external_spec` joins them into a hash-ready `ProposerSpec`.
`build_proposer_agent` (`agent.py`) turns that spec into a runnable
agent:

| Condition | Agent returned | Model |
|---|---|---|
| a declared `proposer` block | `FoeProposerAgent(spec=…, config=…)` — one Foe episode per candidate | the `proposer.model` block's, called by Foe's own transport |
| `runtime.proposer_agent` names a class | that class, constructed `(spec=…, config=…)` | the operator's business |
| neither | **`ValueError`** naming the block to write | — |

```python
# src/zicato/proposer/agent.py — build_proposer_agent (tail)
    refuse_removed_proposer_directory(proposer_path)

    if spec.external_path is None:
        raise ValueError(
            "the epoch's proposer spec names no proposal runtime: the workspace "
            "declares no `proposer` block and binds no runtime.proposer_agent "
            "class. Add a `proposer` block naming the Foe binary this workspace "
            "proposes with (see docs/design/PROPOSER.md)"
        )
    ...
    cls = load_external_proposer_class(spec.external_path)
    return cls(spec=spec, config=external_config)
```

> ⛔ NEVER add a fallback here. A proposer decides what every generation of
> an epoch will be; choosing one silently would make two epochs
> incomparable without saying so. The refusal is the feature.

> ⚠️ TRAP — the identity that was HASHED and the agent that RUNS must be
> resolved from one reading of the workspace. `epoch/contract.py` and
> `evolve/round_entry.py` both call `external_proposer_config`, and both pass
> the result on: the contract hash folds `resolve_external_spec`'s digest,
> and the round constructs the class from the same binding. Resolving them
> separately is how a contract comes to describe a proposer that is not the
> one running.

### 5.1.1 What a workspace that declared nothing still does

`ProposerSpec.default()` — `agent_id = "builtin:default"`, no tools, no
skills — still exists and still canonicalizes. That is deliberate: a
contract is more than its proposer, so an epoch whose workspace declares
no proposal runtime still HASHES, on a machine with no Foe binary
present. What it cannot do is open a round.

That split is what keeps `foe plan` — which `contract_identity` shells
out to — off the path of every contract read. Only a workspace that
declares a runtime pays for asking the binary its fingerprint, and the
answer is memoized per binary and document for the life of the process.

### 5.1.2 The episode, in detail

One `propose()` is one Foe episode, in its own process:

1. `scratch_working_copy(generation_root)` mints a disposable writable
   copy of the parent snapshot under `ztw-pscratch-*`, removed on every
   exit path. The snapshot itself is never mounted writable.
2. `build_episode_tools` binds the two host tools to THIS round's
   snapshot, manifest and copy. `mutation_usage` delegates to the
   read-only registry; `validate_patches` projects the copy and lints it.
3. `build_request` composes the contract — instructions (charter + the
   epoch's brief + its skills), the sanctioned tool list, the read grant
   (the snapshot) and the write grant (the copy), the budget, and the
   completion rule — plus the task, which is this round's evidence.
4. `foe.start_config` launches the binary. The episode's own pid is
   written to `active_runs` before its first model request, so the
   supervisor watchdog can end a wedged proposal by the same escalation
   it uses for a wedged tournament worker (§5.1.4).
5. The host polices the wall-clock deadline it gave Foe. Reaching it
   means the process did not honor its own budget, so the host that holds
   the pipe cancels it and reports `ProposerExhausted("seconds")`.
6. `_experiment_from` turns the outcome into an experiment or into a
   refusal (§5.1.3), projecting the working copy onto the declared
   mutation points to get the patch set.
7. The caller's post-apply hook (`ctx.validate_experiment`) runs last.

> ⚠️ TRAP — the projection reads a point back in the APPLIER's unit, not
> the enumerator's. For a `.py` span those differ: the enumerator reports
> whole lines (`PROMPT = """…"""`), the applier rewrites the literal node
> alone (`"""…"""`). `applier.replacement_source` converts between them.
> Skip it and every span proposal nests the statement inside its own
> literal — which is what the round-trip test in
> `tests/test_proposer_foe_scratch.py` applies its patches to catch.

### 5.1.3 The four endings

An episode ends **completed**, **blocked**, **exhausted**, or **failed**,
and each is first-class (`src/zicato/proposer/proposer.py`):

| Ending | Reaches the round as | What it means |
|---|---|---|
| completed | an `Experiment` | the copy became a valid patch set and the hypothesis explains it |
| blocked | `ProposerBlocked(code, message)` | the proposer found the task impossible, and the code says how |
| exhausted | `ProposerExhausted(limit)` | the budget ran out with work still in progress; `limit` names the dimension |
| failed | `ProposerError` | a crash or a protocol failure |

`ProposerBlocked` and `ProposerExhausted` subclass `ProposerError`, so a
handler written for the base class keeps working. Two blocked codes are
zicato's own reading of Foe's `verification-unsatisfiable`, refined by
looking at the copy: `edit-outside-mutation-point` when a change lies
outside every declared point, and `no-groundable-mutation-point` when the
episode changed nothing. Both are facts the copy can settle and the
runtime cannot.

A blocked or exhausted round spends no tournament budget. The round log
records the kind, the code and the message
(`proposal_episode_settled`), and the scorecard counts them.

### 5.1.4 The supervisor's reach

The episode registers an `ActiveRun` carrying Foe's own pid — not a
wrapper's — plus its process group, its deadline, and the path of its
episode log. There is no second Python worker around it, because two
lifecycle authorities over one subprocess make cancellation ambiguous
exactly when it matters.

**Tests:** `tests/test_proposer_agent.py` (what the builder resolves and
what it refuses), `tests/test_proposer_foe_agent.py` (the episode, all
four endings, the watchdog registration, the holdout exclusion),
`tests/test_proposer_foe_config.py` (the block, and the refusals),
`tests/test_proposer_foe_scratch.py` (the copy and the projection),
`tests/test_proposer_skills.py` (spec resolution + frontmatter +
normalization).

---

## 5.2 `ProposerContext` — every field

`ProposerContext` (`src/zicato/proposer/agent.py`) is a frozen dataclass — one
per proposer invocation, assembled by the orchestrator, consumed by whichever
agent implementation resolved. Iterable inputs are stored as **tuples** so a
context is a stable, re-readable value across retries (a generator would be
exhausted on attempt 1).

This is the table to consult before touching anything proposer-adjacent.
"Envelope class" is the restricted-visibility classification of §5.8:
**IDENTITY-FREE** (carries no board identity by construction), **BANDED**
(numeric signal coarsened at render), **AGGREGATED** (per-entry identities
folded to counts), **REDACTED** (mechanically scrubbed content), **SANITIZED**
(pattern details sanitized at render under `restrict_visibility`), or
**MACHINERY** (not prompt content at all).

| Field | Type / default | Who SETS it | Who READS it | Envelope class |
|---|---|---|---|---|
| `epoch_id` | `str` | orchestrator (resolved epoch) | prompt-independent: lineage coordinate for the minted `Experiment`; tools context; insights lookup | MACHINERY (never rendered into the prompt body) |
| `parent_generation_id` | `str` | orchestrator (current champion) | `Experiment.parent_generation_id`; `_resolve_generation_root` for the tools; meta-loop events | MACHINERY |
| `new_generation_id` | `str` | orchestrator (`_next_generation_id`, or the resume plan's reused id) | `Experiment.generation_id`; `Experiment.id = f"exp_{epoch}_{gen}"` | MACHINERY |
| `patterns` | `tuple[Pattern, ...]` | orchestrator: `detect_patterns` over the **TRAIN slice only** | `render_pattern_block` (prompt), `_targets_observed_failure` (best-of-N heuristic), exemplar anchors | SANITIZED under `restrict_visibility` (identity keys stripped → `entries_affected=N`); **train-only** |
| `mutations` | `tuple[MutationPoint, ...]` | orchestrator: `enumerate_mutations` over the adapter's mutable trees | `render_mutation_block` (prompt), `parse_experiment_json` cross-checks, the tool context (`mutation_usage`'s id check, `validate_patches`' pre-image guard) | IDENTITY-FREE (code spans, unrelated to the board split) |
| `brief_text` | `str` | orchestrator: `load_brief(brief.md).text` | `instruction_sections` → the episode's `70-brief` section (spliced verbatim) | IDENTITY-FREE (operator-authored) |
| `current_loss_summary` | `str` | orchestrator: `_render_loss_summary(TRAIN losses)` — one line: `drift_loss_mean=… over N runs, pass_rate=…` | user prompt `## Current loss summary` | AGGREGATED (board-wide means only; train-only) |
| `aux_call_llm` | `(system, user, model) -> Awaitable[str]` | orchestrator from `RuntimeConfig` | the best-of-N **critic** and the LLM recombination merge. NEVER the proposal episode | MACHINERY |
| `model` | `str = ""` | orchestrator: `workspace_config["auxiliary_model"]` | forwarded to `aux_call_llm`; the collusion smell-test. NOT the episode's model, which the `proposer` block names | MACHINERY |
| `max_retries` | `int = 2` | orchestrator (`max_proposer_retries`) | the episode's `verify_retries`: how many turns of verifier findings it gets before ending blocked | MACHINERY |
| `forbidden_ids` | `tuple[str, ...] = ()` | orchestrator: `brief.forbidden_ids` (parsed from `# Forbidden edits` bullets) | `enforce_forbidden` after the episode; re-checked post-propose by `check_patch_manifest_and_forbidden` | IDENTITY-FREE |
| `workspace_root` | `Path \| None = None` | orchestrator | the host tools' context root; where the episode log and the input capture land; `None` ⇒ the capture is a no-op | MACHINERY |
| `validate_experiment` | `ExperimentValidator \| None = None` | orchestrator: `build_post_apply_validator(...)` (`src/zicato/evolve/round.py`) | the agent, after the episode; best-of-N `_mount_chosen` / `_revalidate` — the shared hook is what mounts the canonical `next_id` tree | MACHINERY |
| `scratch_validator_factory` | `Callable[[], tuple[ExperimentValidator, Callable[[], None]]] \| None = None` | orchestrator: `build_scratch_validator_factory(...)` (`src/zicato/evolve/round.py`) | best-of-N only: one `(validate, cleanup)` lease per slate slot, each over its OWN disjoint scratch tree, so the slate can gather. `None` ⇒ the wrapper falls back to `validate_experiment` and runs the slate serially | MACHINERY |
| `meta_loop_emitter` | `MetaLoopEmitter \| None = None` | orchestrator (one per `evolve_n_rounds`) | `proposer_call_started`/`proposer_call_completed` bookends, one pair per EPISODE | MACHINERY (telemetry, best-effort) |
| `custom_judge_names` | `frozenset[str] \| None = None` | orchestrator: `_declared_custom_judge_names(board, weights)` (board `JudgeSpec.name` ∪ `per_judge_weights` keys) | `parse_experiment_json` (drift-metric validation) ONLY — permissive by design, including zero-weight judges, so the prompt-side priority filter can never turn an accepted movement into a burned retry; the prompt vocabulary comes from `metric_priorities` | IDENTITY-FREE (judge names are contract identity rather than board-entry identity) |
| `prior_experiments` | `tuple[PriorExperiment, ...] = ()` | orchestrator: `_load_prior_experiments` (+ the field loop appends in-flight `siblings`) | `render_prior_experiments_block` (prompt); `recent_prediction_accuracy` (best-of-N calibration) | BANDED under `restrict_visibility` (Δscalar bucketed; accuracy always banded); curated + capped at 12 (§5.10) |
| `restrict_visibility` | `bool = False` (context default) — **default-ON in production** via `weights.overfitting.restrict_proposer_visibility` | orchestrator from the contract | `render_evidence` → `render_pattern_block(restrict=…)`, `_render_prior_experiment_line(restrict=…)`; the best-of-N critic renders the SAME evidence | MACHINERY (the envelope switch itself) |
| `failure_profile` | `str = ""` | orchestrator: `_render_failure_profile(TRAIN losses, weights)` — pre-rendered, **already banded** | spliced as `## Failure-mode profile`; `hint_for_slot` parses its stable line shapes for the dominant mode | BANDED + AGGREGATED (every number through `_band_rate`/`_band_quality`; board-anonymous by construction) |
| `metric_priorities` | `str = ""` | orchestrator: `render_metric_priorities_block(build_metric_priorities(board, weights, losses))` — pre-rendered, **already banded** | replaces the flat vocabulary inside `## Valid expectation targets`; also threaded into the recombination merge prompt | BANDED (within-channel weight ratios only — the raw coefficients are the objective function and stay orchestrator-side, §5.8) |
| `process_exemplars` | `str = ""` | orchestrator: `_render_process_exemplars_block` — **opt-in** (`proposer_quality.process_exemplars > 0`), best-effort | spliced as `## Process exemplars` directly after the failure profile; also fed to the critic | REDACTED (the four redaction rules, §5.8.3); train-only; empty at default |
| `sample_hint` | `str = ""` | the **best-of-N wrapper** (`replace(ctx, sample_hint=hint_for_slot(i, n, profile))`) — never the orchestrator | `render_evidence` → `## Edit-class hint (this sample)` at the very top | IDENTITY-FREE (static instruction strings only) |
| `slot_index` | `int \| None = None` | the **best-of-N wrapper** (`replace(ctx, slot_index=sample)` in `_run_one_slot`) — never the orchestrator | the input capture (§5.5.1), the episode log's directory name, and the task's `## This episode` block, which is what tells one slot's episode from a sibling's | MACHINERY |
| `revise_feedback` | `str = ""` | the **best-of-N wrapper**, only on the ONE all-vetoed revise re-sample (`_render_revise_feedback`) | `render_evidence` → `## Why the previous attempt was set aside`, at the top of the task | AGGREGATED (composed solely of counts-only screen reason strings + static text) |
| `mutation_track_records` | `Mapping[str, MutationTrackRecord] \| None = None` | orchestrator: `_load_mutation_track_records` (best-effort index read, `{}` on failure) | `render_mutation_block(track_records=…)` — one banded advisory line per manifest entry; the `mutation_track_record` tool renders the same shape | BANDED + AGGREGATED ("experiments touching this point"; Δscalar bucketed; never causal) |
| `round_event_emitter` | `Callable[[str, dict], None] \| None = None` | orchestrator: `_RoundLogEmitter.emit` | best-of-N wrapper via `_emit_round_event` (guarded — a raising emitter never fails a propose) | MACHINERY |
| `screen_candidates` | `ScreenRunner \| None = None` | orchestrator: `_build_candidate_screen_runner` — ONE closure per round, only when `screen_entries > 0 AND best_of_n > 1` | best-of-N wrapper: `_screen_slate`, `_screen_replacement` | MACHINERY (its OUTPUT strings are AGGREGATED counts-only by the `CandidateScreenResult.reason` contract) |
| `recombine_pair` | `RecombinationPair \| None = None` | orchestrator: `_build_recombination_pair` — ONE selection per round at the screen-builder site, only when `proposer_quality.recombine AND best_of_n > 1`; `_recombine_pair_for_slot` threads it to the FIELD's slot-0 challenger only | best-of-N wrapper: the last slate slot mints its patch union (§5.6.11) instead of sampling the LLM | MACHINERY (carries counts + patches + hypothesis TEXT only — entry ids never leave the builder; the improved/regressed sets are intersected with the current TRAIN board inside `_build_recombination_pair` and discarded) |
| `genealogy` | `tuple[GenealogyItem, ...] = ()` | orchestrator: `_build_genealogy_items` — ONE sampling per round at the screen-builder site, only when `proposer_quality.genealogy > 0`; ALL best-of-N slots (and the critic) see the SAME items | `render_evidence` → `render_genealogy_block` → spliced as `## Candidate genealogy` directly above `## What's already been tried` (§5.6.13) | BANDED + REDACTED (whole-candidate outcomes through `_bucket_scalar_delta`; proposer-authored core ideas + capped diff excerpts; NO entry ids, NO per-entry results, NO exact deltas — candidate genealogy, never board data; empty at default) |

> ✅ ALWAYS give a new `ProposerContext` field a default that renders
> byte-identically when unset. That is not a style preference — it is the
> compatibility contract that lets every standalone caller (tests, the CLI
> `propose` command) keep producing identical tasks, and it keeps the
> contract hash honest (an absent channel adds nothing to any canonical
> form).

> ⛔ NEVER put raw per-entry material on the context "for the agent to filter
> later." The context IS the envelope boundary on the agent side: everything on
> it either carries no board identity or was banded/redacted **before** it was
> placed there (`failure_profile` and `process_exemplars` are pre-rendered
> strings for this reason — the agents only forward them). The one
> exception is `patterns`, whose identity keys survive on the object and are
> sanitized at the RENDER boundary under `restrict_visibility`; do not copy
> that pattern for new channels (see §5.8.7's checklist — pre-render instead).

---

## 5.3 The episode loop

`FoeProposerAgent.propose` (`src/zicato/proposer/foe_agent.py`) runs one
episode per call. Where the repair happens is the thing to hold onto: the
model fixes its own work *inside* the episode, and zicato's checks are
what remains outside it.

```
build_request(...)                 # instructions + task + grants + budget
foe.start_config(...)              # one process; its pid → active_runs
  ├─ the episode reads, greps, edits the copy
  ├─ validate_patches               # the completion rule: project + lint
  │    findings → back to the model, up to `verify_retries` turns
  │    retries spent → the episode ends BLOCKED
  └─ return {hypothesis}            # the model never drafts a patch document
project_onto_mutation_points(...)  # the copy IS the patch set
parse_experiment_json(...)         # two-pass (§5.4)
enforce_forbidden(...)             # the brief's forbidden-edits list
await ctx.validate_experiment(...) # the post-apply hook (§5.3.3)
```

`ctx.max_retries` becomes the episode's `verify_retries`. It bounds turns
inside one episode rather than whole re-proposals, which is why a broken
edit costs a turn instead of a round: the model sees the finding and the
file it wrote, and does not have to be re-sent the round's whole evidence.

### 5.3.1 Where each failure is caught, and by whom

| Failure | Caught by | The proposer sees | Reaches the round as |
|---|---|---|---|
| an edit outside every declared point | `validate_patches`, via the projection | the path and line range of each offender | a turn; past the retries, `ProposerBlocked("edit-outside-mutation-point")` |
| a copy that changed nothing | `validate_patches` | "change a declared point before returning, or report a block" | a turn; past the retries, `ProposerBlocked("no-groundable-mutation-point")` |
| a patch set that fails the linter (A1–A4) | `validate_patches`, via `zicato.proposer.validate` | one finding per problem | a turn; past the retries, blocked |
| a hypothesis predicting no movement | the runtime, at the value boundary (`HYPOTHESIS_SCHEMA`'s `anyOf`) | the schema violation | a turn |
| a hypothesis naming an undeclared judge | `parse_experiment_json`, after the episode | — | `ProposerError` |
| a patch touching a forbidden id | `enforce_forbidden`, after the episode | — | `ProposerError` |
| a patch that breaks the child snapshot | the post-apply hook, after the episode | — | `ProposerError` |
| the episode outliving its wall clock | the host holding the pipe | the cancel | `ProposerExhausted("seconds")` |
| the binary dying, or a transport error | `foe.Handle.wait` | — | `ProposerError` |

The pattern is worth stating: **a failure the model could still act on is
reported inside the episode; a failure it could not is raised after it.**
Moving one across that line changes what a round costs.

> ⚠️ TRAP — the schema and the parser must agree. `HYPOTHESIS_SCHEMA`
> states zicato's "at least one predicted movement" rule as an `anyOf`
> BECAUSE the runtime checks the returned value at the boundary: a
> hypothesis the runtime accepts and `parse_experiment_json` then rejects
> costs the whole episode rather than one turn. If you add a rule to the
> parser, ask whether the schema can carry it — `foe/docs/config.md` lists
> the subset the runtime enforces.

### 5.3.2 `revise_feedback` seeding

`ProposalEvidence.revise_feedback` renders a `## Why the previous attempt
was set aside` block at the TOP of the task — the screen-informed revise
(§5.6.4) seeds it with the slate's counts-only veto summary, so the one
bounded re-sample starts as a genuine repair turn. For every non-revise
call it is `""` and the block is omitted.

### 5.3.3 The post-apply validation hook (`validate_experiment`)

The hook type is `ExperimentValidator = Callable[[Experiment],
Awaitable[list[str]]]` — non-empty list ⇒ retryable failure. In production the
orchestrator supplies `build_post_apply_validator`
(`src/zicato/evolve/round.py`), which per attempt:

1. beats the `applying:round_{n}:{next_id}` heartbeat phase;
2. `genstore.derive_generation(...)` — copies the parent tree and applies the
   candidate's patch set **all-or-nothing** into the FIXED child snapshot
   coordinate, clearing any stale child tree from a prior attempt (a
   `derive_generation` `ValueError` — e.g. a patch that leaves a `.py` file
   unparseable — is returned as a single retryable finding, never raised);
3. records the derived tree in the caller's `last_child_snapshot["path"]` slot
   — the tree the tournament later mounts, so no second apply is needed;
4. runs `zicato.mutation.validator.validate_post_apply` and returns findings.

Why this exists: without the hook a destructive patch (a dropped import, a
vanished `# zicato:mutable` marker, broken syntax) costs an entire wasted
tournament round — applied, validated, and rejected with no retry. The hook
makes it cost **one bounded retry** with the concrete
validator strings fed back. The retry budget is **shared** with parse-error
retries, so the per-round wall-clock stays bounded.

> ⚠️ TRAP — the hook derives into the SAME on-disk child coordinate every
> attempt (that is what makes retries idempotent), so `next_id`'s tree is a
> single mutable slot that only ever holds ONE candidate's edits. Under
> best-of-N the slate keeps out of that slot entirely: each slot validates
> through a per-slot scratch lease (§5.3.3a) and the shared hook runs exactly
> once, on the chosen candidate, after selection (§5.6.5). That is what bugs
> Cases 6 and 7 cost the project — see 12-bug-casebook.md. If you write a new
> consumer of `last_child_snapshot["path"]`, reason about WHICH candidate's
> tree is mounted at the moment you read it: before `_mount_chosen` runs,
> the slot holds nothing this round wrote.

#### 5.3.3a The per-slot scratch validator (what the slate actually calls)

`build_scratch_validator_factory` (`src/zicato/evolve/round.py`) is the
concurrency-enabling sibling of the shared hook, threaded onto
`ProposerContext.scratch_validator_factory`. Each `factory()` call mints a
fresh `mkdtemp` parent (`ztw-slate-*`, in the OS temp dir — OUTSIDE the
workspace, so nothing under it can be mistaken for a canonical snapshot) and
returns a `(validate, cleanup)` lease:

- `validate` beats the same `applying` phase, then calls
  `genstore.derive_scratch(...)` — a plain `apply_patches` into the disjoint
  temp tree that NEVER enters the generation namespace (no commit, no tag, no
  branch or working-tree mutation), so no walker — records listing, lineage,
  GC, reindex, the dashboard readers — can enumerate it. A retry within the
  slot re-derives into the SAME scratch tree, the same idempotent
  clear-and-reapply the shared hook performs.
- `cleanup` idempotently removes the whole `ztw-slate-*` parent; a
  crash-leaked parent is swept at the start of the next round.

The parent generation's source tree is pre-warmed once by the factory, so the
concurrent slot derives find it materialized and only READ it — they race on
nothing. This factory never writes the canonical tree.

### 5.3.4 Budgets and telemetry

An episode is bounded twice, by the same number. `proposer.budget.seconds`
is the deadline Foe enforces from inside; the host polices the same
deadline from outside, because reaching it means the process did not honor
its own. The other dimensions (`model_calls`, `input_tokens`,
`output_tokens`) are Foe's alone, and all four are part of what Foe
fingerprints — raising one rolls the epoch, which is the intended reading:
a proposer with twelve model calls investigates differently from one with
three.

Each episode emits a paired `proposer_call_started` /
`proposer_call_completed` on the meta-loop session when an emitter is
wired (`invocation_id` correlates the pair; the completed outcome carries
how the episode ended: `"completed"`, `"blocked:{code}"`,
`"exhausted:{limit}"`, `"timeout"`, `"error:Failed"`). One episode is one
call from this side — the turns inside it are Foe's transcript — so the
pair brackets the episode rather than a model request. Every emit is
guarded: a misconfigured emitter cannot regress the proposer. The episode
also writes one durable input-capture record carrying the exact
instructions and task it ran under. Tests:
`tests/test_meta_loop_emitter.py`, `tests/test_proposer_input_capture.py`.

### 5.3.5 `ProposerError` — the one failure contract

`ProposerError.attempts` is the per-attempt error list in call order; the
message joins them (`attempt 1: …`), and `.outcome` carries the typed
ending (§5.1.3). `ProposerBlocked` and `ProposerExhausted` subclass it, so
every consumer below keeps working for all three. Consumers, all of which
you must keep working if you touch the shape:

- the gauntlet path folds it into a **rejected round**
  (`_rejected_proposer_experiment` + `_persist_rejected_round` — a clean
  append-only journal entry, never a crashed loop);
- the field path drops the slot and runs a **narrower field**, publishing the
  full `attempt_reasons` list to the dashboard's proposing tracker
  (`_short_reject_reason` / `_trim_reason` in
  `src/zicato/evolve/propose_apply.py`);
- the best-of-N wrapper re-raises the LAST inner error when the whole slate
  failed, so single-sample call sites see the identical contract;
- `_propose_child` emits one `proposal_attempted` round-log event **per failed
  attempt** off `exc.attempts`, plus one `proposal_episode_settled` carrying
  `exc.outcome`'s kind, code and message, before re-raising. The ENDING is
  recorded as its own fact rather than inferred from the last message,
  because a block, a spent budget and a crash want different remedies.

### 5.3.6 Failure-modes catalog — what the logs mean

What a weaker agent will actually observe when a propose step misbehaves, and
what each observation means. All of these are NORMAL, handled outcomes — none
should crash an evolve loop; if one does, that is the bug.

| Observation | What happened | Where handled |
|---|---|---|
| `proposer failed after N attempt(s): …` in a rejected round's journal entry | the episode produced no experiment; the trail says which of the four endings and why | gauntlet: `_persist_rejected_round`; the round journals `rejected` |
| `attempt k: schema violation at hypothesis/modulating: …` | shape failure at pass 1 — a hypothesis the runtime's own schema did not catch | §5.4.2 |
| `attempt k: patch[0]: unknown mutation_id '…'` | the model targeted an id not in the manifest — usually it hallucinated a plausible-sounding id or reused one from the memory digest that the current manifest does not carry | §5.4.3; also re-checked post-propose by `check_patch_manifest_and_forbidden`, which RAISES `ValueError` (a hard error — by then the proposer already validated, so a stale id means the manifest changed under the round) |
| `attempt k: patches violate proposer-brief forbidden-edits list: …` | the brief's `# Forbidden edits` section named the id; the retry feedback names the offending ids | §5.3.1 row 4 |
| `attempt k: patches failed post-apply validation: …` | the patch applied but broke the snapshot (dropped import / marker / syntax); `derive_generation` or `validate_post_apply` findings fed back | §5.3.3 |
| `attempt k: derive_generation rejected the patch set: …` | `apply_patches`' own post-apply syntax gate raised `ValueError` — surfaced as a single finding rather than crashing the loop | `build_post_apply_validator` step 2 |
| `proposal episode for … outlived its Ns budget; cancelling pid …` (WARNING) | the episode ignored the deadline Foe was given, so the host that holds the pipe ended it; the round sees `ProposerExhausted("seconds")` | §5.3.4 |
| `blocked (no-groundable-mutation-point): …` in a rejected round | the episode's verifier was never satisfied, or it reported a block itself; NO tournament budget was spent | §5.1.3 |
| `multi-challenger field: proposer could not produce a valid challenger for …; the field runs without it` (WARNING) | one field slot exhausted its budget; the strategy resolves over a narrower field; the dashboard shows the slot `rejected` with full `attempt_reasons` | `_propose_and_apply_challenger` |
| `candidate screen failed (…); selecting unscreened` (DEBUG) | the guarded screen degrade — runner raised or returned a malformed result; selection proceeded byte-identically to an unscreened round | §5.6.2 clause 3 |
| `screen-informed revise produced no replacement (…); degrading to critic-over-all` (DEBUG) | the `"unavailable"` revise outcome; nothing to restore — the failed revise wrote only its own scratch tree | §5.6.4 |
| `proposer agent model '…' equals the auxiliary model string; …` (WARNING) | the collusion smell test — advisory, operator responsibility | §5.1 |
| `prior_experiments_for_epoch skipped for …` / `mutation_point_track_record skipped …` (DEBUG) | best-effort index reads degraded; the prompt omits the section / manifest renders unannotated | §5.10.1 |
| `process-exemplar extraction skipped: …` (DEBUG) | the opt-in exemplar channel failed best-effort; prompt renders without the section | §5.8.3 |

### 5.3.7 Where `brief_text` and `forbidden_ids` come from — `brief.md`

`src/zicato/proposer/brief.py`. The proposer brief is the operator's running
guidance to the proposer — an **epoch-level** concept (one brief governs every
proposer call within an epoch) and a contract input (its normalized body is
hashed by `_canon_brief`; a semantic edit rolls the epoch). It is distinct
from the per-board-entry `Rubric`, which is an LLM-as-judge scorer for one
entry.

`load_brief(path)` — a MISSING file is a **hard `FileNotFoundError`**: "the
proposer cannot operate without operator guidance, even if that guidance is an
empty brief." Operators who want a permissive default commit a brief that says
so; there is no defaulting behaviour here.

Two specially-named sections carry structured signal:

| Section heading (case-insensitive, `#`–`######` accepted) | Effect | Extraction rules |
|---|---|---|
| `# Forbidden edits` | HARD: any mutation id mentioned in a bullet is refused by the proposer (`enforce_forbidden`) and re-checked by the runner before applying | bullets are `-`/`*`/`+`; backticked tokens win; if a bullet has none, single- then double-quoted tokens are accepted (editors that strip backticks); order of appearance preserved, de-duplicated |
| `# Preferred edits` | SOFT hint only — the proposer is encouraged to look there first, never constrained | same extraction; surfaced as `ProposerBrief.preferred_ids` |

`enforce_forbidden(patches, forbidden_ids)` is **strict equality** on
`Patch.mutation_id` — globbing is intentionally unsupported ("operators who
want to forbid a family of ids should enumerate them"). It returns
human-readable error strings, one per offending patch (empty list = clean),
which the engines feed back verbatim on retry.

The FULL brief text — everything outside the two structured sections included
— passes verbatim into the system prompt, so free-form operator prose reaches
the model.

> ⚠️ TRAP — the forbidden check is enforced in THREE places by design:
> (1) inside both proposer engines per attempt (retryable feedback), (2)
> post-propose by `check_patch_manifest_and_forbidden` (hard `ValueError` —
> defense in depth against an agent implementation that skipped step 1), and
> (3) the mutation validator's own `check_forbidden_ids` before apply. Do not
> remove any layer because "another one already checks" — each guards a
> different caller.

### 5.3.8 Skills and `ProposerSpec` resolution

`src/zicato/proposer/skills.py`. A skill is one
`proposers/<name>/skills/*.md` file: optional `---`-fenced frontmatter
(`name:` / `description:` — simple `key: value` lines, no nested YAML)
followed by a free-form markdown body. Missing frontmatter tolerated: `name`
falls back to the file stem, `description` to `""`, the whole text is the
body.

`load_proposer_skills(skills_dir)` discovers `*.md` **sorted by filename** (so
the result is independent of filesystem enumeration order / mtime — a
determinism requirement for the contract hash); a missing/non-dir `skills_dir`
yields `()`.

`resolve_proposer_spec(proposer_path)`:

```python
# src/zicato/proposer/skills.py — resolve_proposer_spec (tail)
    return ProposerSpec(agent_id=f"dir:{proposer_path.name}", tools=(), skills=skills)
```

A directory that still ships an `agent.py` resolves fine here — the skills
are still the epoch's, and the contract still hashes — but
`build_proposer_agent` refuses to build from it, naming the removed
runtime. The refusal is at build time so the operator learns before a
round opens rather than after.

`normalize_skill_body` mirrors the brief normalizer (`_canon_brief`): CRLF →
LF, per-line trailing-whitespace strip, leading/trailing blank lines dropped —
"what makes a cosmetic skill edit (re-indenting, CRLF churn, a trailing
newline) leave the contract hash unchanged while a semantic edit moves it."
The RENDERED skill body (`render_skills_block`) is the verbatim file body —
normalization applies only at hash time.

Rendering: `instruction_sections(brief, skills)`
(`src/zicato/proposer/foe_request.py`) puts the brief in section
`70-brief` and the skills in `80-skills`, so the runtime — which orders
sections lexicographically — shows the operator's goal before the
procedures for reaching it. The skills body is
`render_skills_block(skills)`, one `### <name> — <description>` heading +
body per skill. An empty skills tuple declares no section at all, so
there is nothing for the fingerprint to move on.

> ✅ ALWAYS run `tests/test_proposer_skills.py` and
> `tests/test_epoch_contract.py` together when touching skills: the first
> pins parsing/normalization, the second pins that whitespace-only edits do
> NOT roll the epoch while semantic edits DO. Breaking the second in either
> direction is severe: false rolls orphan lineages; missed rolls compare
> incomparable generations.

---

## 5.4 The structured-output schema and two-pass validation

The proposer must emit ONE JSON object with exactly two top-level keys:
`"hypothesis"` and `"patches"`. `parse_experiment_json`
(`src/zicato/proposer/structured.py`) lifts a raw model response into a typed
`Experiment`, or raises `ExperimentParseError` with a message engineered for
the retry prompt.

### 5.4.1 Salvage first: `extract_json_object`

Before any validation, the raw response goes through a 5-stage progressively
more aggressive recovery. Each stage is a fallback ONLY when the prior one
yielded nothing parseable, so a clean response is untouched (stage 1 alone
handles a well-formed response):

| Stage | What it tries | Rescues |
|---|---|---|
| 1 | `_strip_fences` (leading/trailing ```` ```json ```` fence) + direct `json.loads` | the clean path; a single outer fence |
| 2 | `_strip_reasoning_wrappers` (`<think>` / `<thinking>` / `<reasoning>`, case-insensitive, DOTALL) then retry stage 1 | `<think>…</think>{…}` |
| 3 | `_ANY_FENCE_RE` — a fenced block ANYWHERE in the buffer; experiment-shaped (`hypothesis` + `patches`) candidates preferred | JSON buried under prose |
| 4 | `_scan_brace_objects` — continuous string-literal-aware balanced-brace scan; experiment-shaped preferred | trailing commentary, prose preambles |
| 5 | `_scan_brace_objects_anchored` — restarts the string/depth state machine afresh at EVERY `{` | a lone quote or stray brace in a reasoning preamble that corrupts stage 4's continuous matcher |

From stage 3 onward the scan runs over the **reasoning-stripped** text so a
JSON-ish blob inside a `<think>` block cannot be mistaken for the answer.
A dangling `{` still yields nothing — unbalanced garbage is
rejected. An empty response and a salvage miss get **different** error
messages (`"empty response: …"` vs `"could not extract a JSON object …"`), so
the repair prompt can target the failure mode (the empty case triggers the
"skip all reasoning" variant, §5.3.1).

### 5.4.2 Pass 1 — the JSON schema

`EXPERIMENT_JSON_SCHEMA` (draft 2020-12) enforces required keys, types, and
enum domains. Direction enum: `decrease | increase | neutral |
decrease_or_neutral | increase_or_neutral`; magnitude: `small | medium |
large`. `additionalProperties` is left unset on most subobjects by design —
the proposer may attach commentary keys; the parser reads only documented keys
and ignores the rest. A violation renders the JSON-pointer path into the
error: `schema violation at hypothesis/modulating: …`.

> ⚠️ TRAP — the "at least one of `expected_drift_movements` /
> `expected_metric_movements`" rule is enforced by the PARSER rather than the schema
> (a JSON-Schema `anyOf` obscures error messages in the retry path). If you
> extend the hypothesis shape, follow that split: schema for shape, parser for
> anything whose error message a model must act on.

### 5.4.3 Pass 2 — cross-checks the schema cannot express

| Cross-check | Rule | Error shape |
|---|---|---|
| `patches[*].mutation_id` resolves | must be a key of the live `mutations_by_id` manifest | `patch[i]: unknown mutation_id '…' (must match an id from the supplied mutation manifest)` |
| op ⇄ `new_*` discrimination | see the op table below | `patch[i]: op='replace' requires a non-empty string 'new_content' field`, `… must not set 'new_numeric'`, … |
| `set_numeric` range | value inside any `min`/`max` in `MutationPoint.metadata`; malformed metadata fails OPEN (the applier re-checks) | `patch[i]: new_numeric=… below min=… for mutation '…'` |
| `set_enum` domain | value in the metadata's comma-separated `enum` domain (absent domain ⇒ any string) | `patch[i]: new_enum='…' not in declared enum domain […]` |
| `hypothesis.modulating` ids resolve | every listed id must exist in the manifest (the proposer MAY list ids it is not patching, but the journal must never lie about what was touched) | `hypothesis.modulating: id '…' does not match any known mutation point` |
| drift kinds | every `expected_drift_movements[i].kind` ∈ `GOLDFIVE_DRIFT_KINDS` | `…: unknown drift kind '…'` |
| drift-namespaced metric names | see §5.4.5 | the long "unknown drift kind … Declared board judges: …" teaching message |

**The patch-op table** (the exact discriminated union weaker agents get wrong
most often):

| `op` | REQUIRED field | FORBIDDEN fields | Extra gate |
|---|---|---|---|
| `replace` | `new_content` (non-empty **string**) | `new_numeric`, `new_enum` | for a span point, `new_content` is ONLY the replacement text of the one string literal — no signatures, no imports, no `# zicato:mutable` marker (the system prompt spells this out; the post-apply validator catches violations) |
| `set_numeric` | `new_numeric` (number) | `new_content`, `new_enum` | metadata `min`/`max` range check |
| `set_enum` | `new_enum` (non-empty string) | `new_content`, `new_numeric` | metadata `enum` domain check |

On success the parser mints
`Experiment(id=f"exp_{epoch_id}_{new_gen}", …, outcome=None,
proposed_at=<UTC now>)` with `patches` as a frozen tuple, each `Patch` given a
fresh `uuid4().hex` id. `outcome` stays `None` — the tournament fills it in.

### 5.4.4 `HypothesisSpec` — falsifiable predictions

The hypothesis object (`src/zicato/core/experiment.py::HypothesisSpec`)
carries: `core_idea` (one sentence), `modulating` (the targeted mutation ids —
non-empty), `why` (pattern-driven rationale), `expected_pass_rate_delta`
(free-text uncertainty band, e.g. `"+0.05 to +0.15"` — intentionally NOT a
number), `risks` (optional), and the two movement lists:

- `expected_drift_movements` — back-compat: registered goldfive drift kinds
  only (`{"kind": "off_topic", "direction": "decrease", "magnitude":
  "medium"}`);
- `expected_metric_movements` — the generalized namespaced path
  (`drift:off_topic`, `cost:tokens_spent`, `rubric:slide_structure`,
  `latency:p95_turn_ms`, `schema:failures`, or a declared judge's BARE name).

At least one list must be present and non-empty. Movements are the
**falsifiable** core of a hypothesis: they are graded after the tournament
(§5.4.6), so a hypothesis that predicts nothing concrete earns no calibration
credit.

### 5.4.5 Declared-judge metric names and the prefix normalizer

A custom board judge emits its goldfive signal under the single `"custom"`
drift kind but is addressed **by its own bare name** in a hypothesis. Models
that know the implementation detail naturally mangle this
(`drift:file_findability`, `custom:file_findability`,
`drift:custom:file_findability`). The validator strips the known prefixes
(`_JUDGE_METRIC_PREFIXES = ("drift:custom:", "drift:", "custom:")` — longest
first, repeatedly) and accepts the movement iff the recovered bare token is a
built-in drift kind OR a declared judge name; an unknown kind still
fails, with a teaching message that enumerates the declared judges and the
built-in kinds.

The prompt side keeps this in lockstep: `render_metric_targets_block`
(`src/zicato/proposer/prompts.py`) renders a `## Valid expectation targets`
section from the SAME `custom_judge_names` set the validator receives, telling
the model exactly which bare names and `drift:<kind>` forms will validate —
"the prompt and the gate agree by construction."

> ✅ ALWAYS thread `custom_judge_names` to BOTH `parse_experiment_json` and
> `render_evidence` from the same source
> (`_declared_custom_judge_names(board, weights)` in the orchestrator). If the
> two drift apart, the proposer is told a name that then fails validation —
> a retry-loop tax on every round. Tests:
> `tests/test_proposer_structured_metric_movements.py`.

### 5.4.6 The prediction-accuracy grading loop

Predictions are graded **after** an experiment settles, then fed back to
future proposals as an advisory calibration signal. The full loop:

1. The tournament writes the realized outcome (per-metric movements, deltas)
   onto `experiment.json`; the index dual-write mirrors it
   (`hypothesis_json` / `outcome_json` columns).
2. `prior_experiments_for_epoch` (`src/zicato/index/query.py`) grades each
   settled row via `_prediction_accuracy_for_row` →
   `zicato.tournament.detail.grade_hypothesis_predictions` (the SAME match
   semantics as the dashboard's hypothesis ledger; per-metric value ranges
   from `_metric_ranges_for_epoch` normalize realized movements into
   small/medium/large buckets). Result: `matches / predictions ∈ [0, 1]`, or
   `None` when nothing was graded. Best-effort — a bad row degrades to `None`,
   never raises.
3. `PriorExperiment.prediction_accuracy` carries the grade into the memory
   digest; `_render_prior_experiment_line` renders it **always banded**
   (`prediction:low|medium|high` via `_band_prediction_accuracy` — banded
   regardless of `restrict_visibility`, since it is a calibration meta-signal
   rather than a board number).
4. The best-of-N selection consumes it: `recent_prediction_accuracy(ctx)`
   means the digest's graded values; when the mean clears
   `CALIBRATION_TRUST_BAR = 0.6` the lineage has EARNED trust and
   prediction-bearing candidates rank ahead (heuristic key #2) / the critic
   gets the calibration note. Below the bar — or with no graded history — the
   term is **inert**, so a badly-calibrated proposer is never rewarded for
   confident guessing.

> ⛔ NEVER let prediction accuracy gate anything. It is ADVISORY ordering
> inside the best-of-N selection and a diagnostic band in the prompt — it must
> never veto a candidate, gate a promotion, or feed the tournament gate. The
> docstrings say "never a gate" at every consumer for a reason: grading is
> best-effort and the ranges are approximate.

---

## 5.5 The task — what the model actually sees

Assembled by `render_task` (`src/zicato/proposer/foe_request.py`), which is
the episode block plus `render_evidence`. Sections in top-to-bottom order:

| Order | Section | Present when | Source |
|---|---|---|---|
| 0 | `## This episode` — both tree roots, and the candidate (and slate slot) being produced | always | `render_episode_block` |
| 1 | `## Why the previous attempt was set aside` | `revise_feedback` non-empty (§5.3.2) | `_render_revise_feedback` |
| 2 | `## Edit-class hint (this sample)` | `sample_hint` non-empty (a best-of-N slot) | `hint_for_slot` |
| 3 | `## Recent telemetry insights` | insights file exists for the epoch | `load_latest_insights` |
| 4 | `## Failure-mode profile (this round, aggregate — train slice)` | `failure_profile` non-empty | `render_failure_mode_profile` |
| 5 | `## Process exemplars (train slice — redacted event windows)` + the redaction-contract banner | `process_exemplars` non-empty (opt-in) | `render_process_exemplars` |
| 6 | `## What's already been tried (this epoch — avoid repeating failures, build on wins)` | `prior_experiments` non-empty | `render_prior_experiments_block` |
| 7 | `## Current loss summary` | always | `current_loss_summary` |
| 8 | `## Valid expectation targets` | always | `render_metric_targets_block` — the priority-ordered body from `metric_priorities` when the caller supplied one, else the flat membership list |
| 9 | `## Patterns observed (advisory…)` | always (`"(no patterns detected …)"` when empty) | `render_pattern_block` |
| 10 | `## Mutation points (only these may change)` | always (`"(no mutation points available)"` when empty) | `render_mutation_block` (+ optional per-point track-record lines) |
| 11 | "Change the working copy now, verify it, and return your hypothesis." | always | `render_task` |

The INSTRUCTIONS are a different document, and a hashed one:
`instruction_sections` returns the charter (`CHARTER_SECTIONS` — what a
proposer is for, where it works, what it may change, how it verifies, what
it returns, when to report a block), then the epoch's brief, then its
skills. Every word of it is model-visible AND part of what Foe
fingerprints, so rewording a charter section rolls the epoch. That is the
intended reading; doing it by accident is what keeping the sections in one
constant prevents.

Two rendering rules worth internalizing:

- **Mutation content is shown in FULL** (`_render_content`): a `replace` must
  reproduce every byte of the span it is not changing, and "a truncated
  preview is exactly how a proposer ends up dropping the parts it cannot see."
  Only a pathological span past `_MUTATION_CONTENT_LIMIT_CHARS = 8000` is
  trimmed, with an explicit "do not emit a `replace` for this point without
  the full content" annotation.
- **Every optional section's empty sentinel is the empty string.** A renderer
  that returns `""` means "omit the section entirely" — that convention is
  what makes knob-off rounds byte-identical, and every new channel must follow
  it.

> ⚠️ TRAP — the task is not fingerprinted and the instructions are. A
> per-round value in the task (a path, a candidate id, this round's
> evidence) cannot move the proposer's contract identity; the same value in
> the instructions would move it every round and roll the epoch every time.
> That is why `## This episode` names the trees rather than the charter
> doing it. Any change here must run `tests/test_proposer_prompts.py`
> (section ordering, banding, the omit-at-default properties) and
> `tests/test_proposer_contract_identity.py` (what does and does not move
> the fingerprint).

### 5.5.1 Reading back what the proposer saw

The renderers are pure, but the channels they render from (patterns, the loss
summary, the prior-experiment digest, genealogy, calibration, the retry
feedback) are assembled per round and not otherwise persisted, so a past
round's prompt cannot be re-derived from the workspace. Every proposer LLM
call therefore writes its rendered input verbatim to
`epochs/{epoch_id}/proposer_inputs.jsonl` before the call is made
(`src/zicato/proposer/input_capture.py`; the path comes from
`proposer_inputs_path()` / `WorkspaceLayout.proposer_inputs()`, never a
spelled-out filename). Read it back with
`read_proposer_inputs(workspace_root, epoch_id)`, which yields records
oldest-first.

One line per call, at all three sites, tagged by `role`:

| `role` | Site | What the record holds |
|---|---|---|
| `proposal` | the proposal episode (`foe_agent.py`) | the joined instruction sections as `system`, the task as `user` — one record per EPISODE, since the turns inside it are Foe's own transcript |
| `critique` | best-of-N selection (`best_of_n.py`) | `_CRITIC_SYSTEM_PROMPT` + the critic's slate prompt |
| `recombine_merge` | the LLM merge slot (`best_of_n.py`) | `render_recombine_merge_prompt`'s two halves |

Each record also carries `ts`, the lineage coordinates (`epoch_id`,
`parent_generation_id`, `new_generation_id`), the `model` string, and the
`slot` index where the call belongs to a best-of-N slate.

> ✅ ALWAYS read proposal spend off this file rather than off a mock. One
> `ROLE_PROPOSAL` record is one episode, so
> `sum(1 for r in read_proposer_inputs(ws, epoch) if r["role"] == ROLE_PROPOSAL)`
> is what a round cost — the workspace's own account, which the recombination
> cost-neutrality tests measure against.

The episode's own transcript is a second, richer artifact: Foe writes it to
`epochs/{epoch_id}/episodes/{generation_id}[-{slot}]/episode.jsonl`, and it
carries every turn, tool call and result rather than just the opening
context.

Four properties to preserve when touching this:

- **Capture runs BEFORE the call.** The attempt that times out is the one
  whose input matters; the response path never runs for it.
- **The write is best-effort and never raises** (DEBUG log, round continues),
  and the reader tolerates an absent file and an unparseable FINAL line. An
  unparseable interior line raises — under the append-only writer only the
  tail can be torn.
- **The append is one `os.write()` on an `O_APPEND` fd under a process-local
  lock** keyed by path, because a best-of-N slate has several writers and a
  buffered text write is several syscalls. Do not route this through
  `StorageBackend.append_jsonl`: it skips the outer→inner `.zicato/` descent
  and its append is unlocked and buffered.
- **Capture is unconditional.** The file is a new at-rest location for
  board-derived content beside `brief.md` and `mutations.json`, and exposes
  nothing to the proposer that the proposer did not already receive, so the
  envelope of §5.8 is unaffected. It is not free: one proposal record against
  a 15-point manifest measures ~23 KB, so a default round (three slate slots
  plus the critique) writes on the order of 90 KB, and a 100-round epoch a few
  megabytes. Nothing prunes it — `zicato epoch gc` removes generation source
  trees only — so if that growth ever bites, the fix is an opt-OUT knob rather
  than a default-off flag: a diagnostic nobody enabled in advance is absent
  from exactly the round that needed it.

---

## 5.6 Best-of-N end-to-end

`wrap_with_proposer_quality(inner, config)`
(`src/zicato/proposer/best_of_n.py`) interposes `BestOfNProposerAgent` only
when `config.best_of_n > 1`; at `best_of_n <= 1` it returns `inner`
**unchanged** — not even a wrapper object in the call path. The DEFAULT
contract is `best_of_n = 3` with `critique_enabled = True`
(`ProposerQualityConfig`, `src/zicato/core/scoring_config.py`); pin
`"proposer_quality": {"best_of_n": 1}` for a single-sample proposer
(scripted/deterministic proposers do). Changing any knob rolls the
epoch — a proposer that samples a slate proposes under a different rule.

Every slot is its own episode, in its own process, with its own working
copy. That is what makes the slate genuinely independent — nothing is
carried between slots but the edit-class hint that tells them apart — and
what lets `_gather_slate` run them concurrently without a shared session to
serialize on. Fork slates, where a runtime branches one warm session into
N, are deliberately not built here (issue #301).

The full `propose` flow when N > 1:

```
outcomes = await _gather_slate(ctx, n)     # N slots CONCURRENTLY, each with
                                           # sample_hint=hint_for_slot(i, N, …)
                                           # and its OWN scratch tree (§5.3.3a)
for outcome in outcomes:                   # deterministic pass, SLOT order
    emit proposal_attempted {errors, slot_index}   # every slot that failed
    candidates.append(outcome.candidate)           # failures narrow the slate
    emit candidate_sampled {i, n}
if slate empty: raise one ProposerError carrying EVERY slot's attempts
if len == 1: await _mount_chosen(candidates, 0, ctx); return it (no critique, no screen)
screen_results = await _screen_slate(candidates, ctx)      # GUARDED; None = unscreened
survivors = non-vetoed indices (all-vetoed → the ONE revise pass, §5.6.4)
chosen, mode = selection (§5.6.6): sole-survivor | critique | heuristic
await _mount_chosen(candidates, chosen, ctx)   # §5.6.5 — the one canonical derive
emit critique_selected {index, reason=mode, slate, rationale}
return candidates[chosen]
```

### 5.6.1 Slate sampling and hint conditioning

Each slot gets a DISTINCT `sample_hint` stamped via `dataclasses.replace` so
the N samples explore different edit strategies rather than re-rolling one
idea. `hint_for_slot(sample_index, n, failure_profile)`
(`src/zicato/proposer/hints.py`) is pure and deterministic:

- **No dominant failure mode** (absent/empty/signal-free profile): the plain
  exploratory rotation `EDIT_CLASS_HINTS[i % 3]` — (1) smallest
  grounded fix, (2) structurally different mechanism than recent attempts,
  (3) target the highest-loss failure mode head-on.
- **Dominant mode present, slots `0..n-2`:** the mode's `FAILURE_MODE_HINTS`
  entry (`over_retrieval` / `misses` / `empty_terse` / `looping`) — the slate
  concentrates on the observed problem.
- **Dominant mode present, the LAST slot (`n-1`):** ALWAYS exploratory — it
  rotates over the remaining exploratory hints, so the slate never goes
  all-in on one reading of the profile; a mis-diagnosed dominant mode still
  leaves one candidate exploring freely.

`dominant_failure_mode` parses the RENDERED profile string (never the summary
object) — directional markers (`=> over-retrieves` / `=> misses relevant
items`) win; otherwise the banded rate tokens (`none` / `~N%` / `~all`) are
decoded and the strictly-largest positive rate wins with fixed-order
tie-breaking. Parsing the rendered string is an envelope property by design:
the mapping can never see a finer-grained number than the proposer itself does
(the proposer already sees that exact string). Tests:
`tests/test_proposer_hints.py`.

> ✅ ALWAYS keep hints STATIC instruction strings. No board entry id, no
> question text, no per-entry value may ever appear in a hint — the hints
> module's "Visibility discipline (LOAD-BEARING)" docstring is the contract.

A slot whose inner `propose` raises `ProposerError` simply **narrows the
slate** (the error is remembered); an all-failed slate re-raises the real
inner failure so callers see the identical single-sample contract.

### 5.6.2 The candidate SCREEN — veto-first semantics

Opt-in: `proposer_quality.screen_entries > 0` **AND** `best_of_n > 1`
(a single sample has no slate to screen). When off — the default — the
orchestrator does not even construct a screen callable
(`_build_candidate_screen_runner` returns `None`) and no screen runs.

The semantics are strictly **VETO-FIRST**, and this is a load-bearing
invariant with four clauses:

1. **The screen disqualifies; it never ranks.** The critic/heuristic still
   chooses among the survivors. The panel scalar a candidate earns is
   SELECTION-BIASED by construction (a small, champion-passing tryout panel
   chosen for the veto) — it may feed the selection only as a LATE tiebreak
   (suppressed entirely by `screen_veto_only: true`), is never journaled as
   evidence, and is never compared against tournament scalars.
2. **A veto can narrow but never empty the step.** An all-vetoed slate takes
   the one revise pass (§5.6.4), then degrades to critic-over-ALL.
3. **Screening can NEVER fail a propose** ("guarded-never-fails-a-propose").
   Every screen call is wrapped: a raising runner, a malformed result (wrong
   length), or a per-candidate engine failure degrades to unscreened /
   no-signal — logged at debug; the selection then runs over the whole slate.
4. **Confirm-before-veto.** A pass-flip (the candidate FAILS a panel entry the
   champion's replicate-0 baseline PASSES) does not veto on one observation:
   the flipped entries re-run ONCE at the reserved confirm slot
   (`SCREEN_REPLICATE_BASE + 1 = 3001`), and only a flip that flips TWICE
   vetoes. Under per-entry flip probability `p` (harness noise) the
   false-veto probability is bounded near `p²` per entry instead of `p`.
   A **budget abort** vetoes immediately with no confirm run — a wall-clock
   exhaustion is deterministic; re-running re-hits the same budget.
   `CandidateScreenResult.confirmed` is `True` only for a confirm-survived
   veto.

The engine (`src/zicato/epoch/screen.py::run_candidate_screen`) evaluates each
candidate on an **ephemeral** tree: `apply_patches` into a tempdir scratch —
NEVER `derive_generation`; the real lineage is untouched — under a phantom
generation id `{parent}-screen-r{round}c{i}` (can never match a real `v\d+`),
with the board stamped at `SCREEN_REPLICATE_BASE = 3000` so its unit-cache
slots can never collide with — or pre-seed — a real duel (see
06-tournament-and-selection.md §6.1.1). The phantom
`generations/{screen-id}` dir the unit cache creates is removed in a
`finally:` per candidate; `sweep_stale_screen_dirs` reaps crash leftovers at
entry (self-heal).

The panel itself (`select_screen_entries`) is pure and deterministic — no
clock, no RNG: champion-passing TRAIN entries ordered lexicographically into a
ring, round `r` reads `k` entries starting at `(r * k) % len(eligible)`, so
the panel ROTATES across rounds and no fixed slice is mined forever. Short
panels fill from non-passing train entries (crash-detection only — no passing
baseline means no flip is detectable); a cold start (no parent losses) is
all-fill. **The holdout is never eligible** — the caller passes the TRAIN
board only.

Outcome classification per candidate, in order of precedence:

| Observation | Classification |
|---|---|
| unit infra-aborted (`is_infra_abort_cause`) | NO SIGNAL — an infra blip never vetoes |
| unit budget-aborted (`BUDGET_ABORT_CAUSE` or clean budget-exceeded) | IMMEDIATE veto |
| candidate fails a `baseline_pass_ids` entry | pass-flip → confirm re-run at 3001 → veto iff it flips twice |
| everything else | clear; panel scalar aggregated over usable losses |

> ⛔ NEVER put an entry id in a screen result string. `CandidateScreenResult
> .reason` is COUNTS ONLY by that field's documented contract
> (`_summarize` emits `"vetoed: panel 4, pass-flips 2 (1 confirmed), …"`) —
> the string flows into the round log AND the (restricted-visibility) proposer
> prompt via the revise feedback, so an id here would breach the envelope in
> two places at once. Tests: `tests/test_candidate_screen.py` asserts the
> counts-only property alongside the veto classification.

### 5.6.3 Where the screen runner comes from

`_build_candidate_screen_runner` (`src/zicato/evolve/round_context.py`) builds ONE
closure per round binding: the rotating train panel (`select_screen_entries`
over the champion's replicate-0 baseline), the parent generation, the frozen
weights/config, and the round index. Every propose site this round — the
gauntlet's single challenger AND every slot of a multi-challenger field —
screens on the SAME panel. Each invocation beats a `screening:r{round}`
heartbeat phase first, so the stall detector attributes the extra
propose-step wall-clock honestly (the phase strings are
07-runtime-and-durability.md §7.6.1).

### 5.6.4 The all-vetoed REVISE pass (screen-informed, bounded to ONE)

Trigger discipline — the revise fires on exactly ONE screen verdict: **an
all-vetoed screened slate**, because that is the one state where proceeding is
*knowingly* wasteful (the step would send a known-vetoed candidate to a full
tournament round). Two states that do NOT trigger it:

- a cold-start slate whose survivors were merely crash-only screened
  (`ScreenPanel.baseline_pass_ids` empty — no pass-flip was ever detectable):
  a replacement would face the same crash-only panel and could earn no
  stronger signal than the survivors already hold;
- a no-signal survivor (screen error): that is the screen's own
  degrade-to-unscreened contract rather than evidence against the slate.

There is NO config knob — the revise rides `screen_entries > 0`, because a
contract that opted into paying for the screen has already accepted the
propose-step cost class, and the single re-sample is the cheapest recovery.

Mechanics (`BestOfNProposerAgent._revise_all_vetoed`):

1. `feedback = _render_revise_feedback(screen_results)` — composed EXCLUSIVELY
   of the per-candidate counts-only `reason` strings plus static instruction
   text ("Propose ONE different experiment … prefer a smaller, more
   conservative edit …"). Never an entry id — the envelope is untouched.
2. `replacement = await inner.propose(replace(ctx, revise_feedback=feedback))`
   — the seed lands in the FIRST attempt's `feedback` slot (§5.3.2). Exactly
   one revise per propose: this method never re-enters `propose`, never loops.
   It leases its own scratch validator exactly like a slate slot, so a failed
   revise cannot clobber any other candidate's tree.
3. The replacement is screened GUARDED (`_screen_replacement`; its
   `candidate_screened` event carries `revise: true` and `index` one past the
   original slate).
4. **The replacement is APPENDED to `candidates` whatever its own verdict** —
   so the selection can pick it, and so `_mount_chosen` can address it by
   slate index like any other candidate.

Outcomes:

| Return | Meaning | Resulting selection |
|---|---|---|
| `"chosen"` | replacement survived (or could not be screened — the guarded degrade) | the replacement is the pick, `selection_mode = "screen_revise_survivor"`, no critique call; it is mounted by the same unconditional `_mount_chosen` derive as any other pick |
| `"fallback"` | replacement itself vetoed | critic-over-ALL over the original slate, mode prefixed `screen_all_vetoed_after_revise:` |
| `"unavailable"` | inner proposer produced no replacement | degrade to critic-over-ALL (`screen_all_vetoed:` prefix). No tree restore is needed: the failed revise wrote only its own throwaway scratch tree, and the eventual pick is derived into `next_id` afterwards |

### 5.6.5 Mounting the chosen candidate — THE MOUNTED TREE MUST MATCH THE CHOSEN EXPERIMENT

The invariant, spelled out: **the on-disk child snapshot the tournament mounts
must be derived from the patches of the experiment the round persists.** If
they diverge, the tournament scores — and the journal/lineage/index record —
two different artifacts, and every downstream conclusion (gate verdict,
Δscalar, memory digest, dashboard diff) is attributed to the wrong code.

Why it can diverge at all: the round has exactly ONE canonical child
coordinate (`next_id`) and the slate has N candidates. Whatever last wrote
that coordinate owns it, and "last write" is not the selection's semantics.

**How it is enforced.** The slate never writes the canonical coordinate:
every slot validates into its own scratch tree (§5.3.3a), so when the
selection ends, `next_id` holds nothing this round wrote. The wrapper then
derives the chosen candidate into it exactly once, unconditionally, at the
one seam serving both pipelines (`BestOfNProposerAgent._mount_chosen`):

```python
# src/zicato/proposer/best_of_n.py — _mount_chosen (core)
        validate = ctx.validate_experiment
        if validate is None:
            return
        findings = await self._revalidate(validate, candidates[chosen])
        if findings:
            raise ProposerError([...])
```

That single hook call is the same idempotent clear-and-reapply a retry
performs, so tree and experiment agree by construction. `validate_experiment
is None` — a context with no derive hook — mounts nothing and returns. The chosen candidate validated cleanly in scratch
moments ago, so a finding here is unexpected (e.g. the parent tree changed
underneath the slate); there is no other candidate whose tree is mounted, so
there is nothing to fall back TO and the step raises the standard
`ProposerError` every call site already handles.

**What a divergence costs**, from the two recorded failures this seam exists
to prevent — the casebook holds the full anatomy of each:

- **Case 6** (12-bug-casebook.md): on the gauntlet path, the
  tournament mounted `last_child_snapshot["path"]` (the last-validated tree)
  while persisting the CHOSEN candidate's experiment — live on defaults
  (`best_of_n == 3`). The duel scored a tree that was not the experiment on
  record.
- **Case 7** — the field-diversity corollary (12-bug-casebook.md): the
  multi-challenger path additionally judges the CHOSEN hypothesis's
  diversity signature (`_diversity_signature` — the `modulating` id-set + the
  normalized core idea) to soft-reject duplicate siblings. With the mismatch,
  the field's diversity decision was made on hypothesis A while the mounted
  tree was candidate C's — two "distinct" siblings could race byte-identical
  trees (the e2e fixture `tests/_best_of_n_slate_support.py` plants exactly
  this: slot 2 of every slate is the same fabricate-metrics decoy, so a
  pre-fix run mounts the WRONG, identical trees for both arms).

> ⛔ NEVER return a candidate from `BestOfNProposerAgent.propose` without going
> through `_mount_chosen` — including the early returns (the sole survivor,
> the recombination short-circuit) and every degrade path. There is no
> "already mounted" case to except: a candidate that never went through
> `_mount_chosen` has NO tree in the generation namespace at all, so skipping
> it fails loudly rather than mounting the wrong tree.
> The e2e proof is `tests/test_best_of_n_tree_integrity.py`: real evolve
> rounds with subprocess workers, a scripted critic that always picks slot 0,
> and a known-answer scalar that detects a wrong mounted tree both by content
> and by arithmetic — run at `propose_parallelism` 1 AND 4, and asserting no
> scratch residue survives in the namespace or the temp dir.

### 5.6.6 Selection: critique, calibration-aware ranking, screen tiebreak

`_select_over` maps the surviving sub-slate through `_select_best` and maps
the chosen sub-index back to slate coordinates. Two selectors:

**The critic** (when `critique_enabled` and an aux callable exists): ONE cheap
LLM call. Its user prompt = the SAME restricted round context the proposer saw
— literally the same evidence, projected off the same context by
`evidence_from_context` and rendered by the same `render_evidence`, including
the failure profile and the redacted exemplar block — + the
compact candidate slate (`_render_candidate_slate`: index, core idea, targets,
per-patch op + rationale, diff size — the proposer's own outputs, already
inside the envelope) + the optional calibration note (§5.4.6) + the optional
counts-only `## Screen measurements` block. It answers with the integer index
ALONE on the first line, then ONE sentence naming the bar clause that decided
it; `_parse_critic_choice` returns `(index, rationale)`, scanning the first
line for the integer token and range-checking it. Any failure — raise,
timeout, unparseable, out-of-range — returns `(None, "")` and the selection
falls back to the heuristic, so a flaky critic never blocks the step.

> ✅ The parse accepts a BARE-INTEGER response as well as the index-plus-
> sentence form the prompt asks for. A single-line `2`, a fenced `2`, or prose
> carrying the index all select that index — they simply record no rationale.
> A first line with no digits falls back to scanning the whole response and
> keeps NO rationale, because the split that would have separated index from
> reason is the thing that did not hold. A rejected
> index discards the rationale with it: the sentence explains a CHOICE, so it
> must never land beside a heuristic pick.

> ⛔ The prompt asks for the bar CLAUSE and forbids the candidate NUMBER, and
> that is load-bearing rather than a style choice. `_select_over` hands the critic the
> SUB-slate of screen survivors, renumbered from 0 — its "candidate 1" is
> `survivor_indices[1]` rather than slate slot 1. The returned index is mapped back
> to slate coordinates, but free text cannot be mapped, so a sentence naming
> a number would point at the wrong row of the event's own `slate` field.

The rationale is capped at `RATIONALE_CAP` (240) characters with whitespace
collapsed, because it rides one `round_log.jsonl` line. The cap sits above the
200 the prompt asks for, so a compliant sentence is never clipped — it catches
runaway text, it does not enforce the ask. Both transports normalize through
the one shared `normalize_selection_rationale`, so their records are
interchangeable. Each slate entry's `core_idea` goes through the SAME
normalizer: it is unbounded model text (no `maxLength` in the proposer
schema), and bounding one of the payload's two text fields would leave the
round-log line unbounded anyway. The `slate` value is a TUPLE, matching the
declared `CritiqueSelected.slate` type — the decoder re-tuples top-level lists
on read, so emitting a list would leave a written event unequal to its own
decoded form.

The index and the rationale are parsed with DIFFERENT strictness: an
unusable index degrades the selection to the heuristic, while an unusable
rationale is dropped on its own. A note about a decision must never veto
the decision, and the rationale is recorded only after the index clears its
range check, so it never carries a sentence for a choice that was rejected.
It is PROVENANCE:
nothing in the loop reads it back, and truncation costs a reader the end of a
sentence and costs the step nothing. Its visibility envelope is the critic's
own — the critic can only paraphrase what it was shown, and it was shown
nothing the proposer had not already seen.

**The deterministic heuristic** (`_heuristic_best_index`) ranks by, in order:

1. **grounded** — touches a pattern-flagged mutation id
   (`_targets_observed_failure`, restricted train-slice patterns only);
2. **calibrated predictions** — iff `recent_prediction_accuracy >=
   CALIBRATION_TRUST_BAR`, prediction-bearing hypotheses rank ahead
   (otherwise the term is constant/inert);
3. **minimal diff** (`diff_char_size` — MDL parsimony);
4. **screen panel scalar** — only when screened AND not `screen_veto_only`;
   lower is better, `None` sorts last; inert otherwise;
5. **stable order** — earlier-sampled wins ties (deterministic for a fixed
   slate).

**Every `selection_mode` string** the wrapper can emit (round-log
`critique_selected.reason` — the loop-health vocabulary; the fold counts
screens/vetoes off the events, but the mode string is what a human greps for):

| `selection_mode` | Meaning for loop health |
|---|---|
| `critique` | the critic chose; normal healthy path |
| `heuristic` | critique disabled, no aux callable, or the critic failed/was unparseable — a persistent stream of these with `critique_enabled: true` means the aux endpoint or the critic prompt is broken |
| `recombined` | a non-vetoed mechanical recombination mint was chosen outright (§5.6.11), no critic call — a single winner captured two rejected complementary fixes; expected only under `proposer_quality.recombine` |
| `screen_sole_survivor` | the veto narrowed the slate to one; no critique call spent |
| `screen_revise_survivor` | an all-vetoed slate was rescued by the ONE revise re-sample |
| `screen_all_vetoed:critique` / `screen_all_vetoed:heuristic` | all-vetoed, revise UNAVAILABLE (inner proposer failed) — the step knowingly forwards a vetoed candidate; frequent occurrences mean the proposer cannot act on the veto feedback |
| `screen_all_vetoed_after_revise:critique` / `…:heuristic` | all-vetoed AND the revise replacement was itself vetoed — the round's edits are systematically regressing the panel; look at the brief/mutation surface |
| any of the above + `:revalidate-fallback` | the chosen candidate failed to re-derive and the last-validated one was returned instead — investigate: the parent tree changed mid-propose, or the applier is non-idempotent (this suffix should be near-zero in a healthy loop) |

### 5.6.7 What the critic must never see

The critic sits INSIDE the proposer's envelope: it sees only what the
proposer saw (same renderer, same flags) plus the candidates (the proposer's
own outputs). It never sees the holdout, never a per-entry identity, and its
screen block is counts-only. "It cannot widen what the proposer learns about
the board" is the module-docstring contract
(`src/zicato/proposer/best_of_n.py`, "Overfitting discipline
(LOAD-BEARING)"). Tests: `tests/test_proposer_best_of_n.py` pins the critic
prompt's restricted rendering; `tests/test_orchestrator_overfitting.py` pins
the orchestrator-side flag threading.

### 5.6.8 One default round's propose step, end to end (worked trace)

The full call sequence under stock defaults — no proposer dir,
`best_of_n = 3`, `critique_enabled = true`, `screen_entries = 0`,
`restrict_proposer_visibility = true`, `process_exemplars = 0` — annotated
with the file that owns each step. Read this once before your first proposer
change; every trap in this chapter appears in situ here.

```
evolve invocation start (once):
  external_proposer_config(config, root) → the workspace's proposer binding
  resolve_proposer_spec(None, binding)   → ProposerSpec(external:foe, …)  skills.py
  build_proposer_agent(spec, None, binding) → FoeProposerAgent(spec, config)
  wrap_with_proposer_quality(agent, q)   → BestOfNProposerAgent(inner=…, n=3)

per round (evolve_once, evolve/round_entry.py):
  mutations   = enumerate_mutations(adapter mutable trees)
  train split = split_board(board, overfitting, seed=rotation_seed(…, epoch))
  patterns    = detect_patterns(TRAIN losses/entries/events)
  loss_summary, failure_profile ("" or banded block), process_exemplars=""
  screen_candidates = None                       # screen_entries == 0
  prior = _load_prior_experiments(root, epoch)   # ≤12 curated entries or []
  next_id = _next_generation_id(…)               # e.g. "v7"
  beat "proposing:round_3:v7"
  validator = build_post_apply_validator(…, last_child_snapshot={})
  scratch   = build_scratch_validator_factory(…)   # per-slot leases

  _propose_child → BestOfNProposerAgent.propose(ctx):
    3 slots gather concurrently (propose_parallelism=4); slot 0 shown:
    slot 0: replace(ctx, sample_hint=hint_for_slot(0,3,profile),
                    validate_experiment=<its own scratch lease>)
      FoeProposerAgent.propose:
        scratch_working_copy(parent snapshot) → /tmp/ztw-pscratch-*   ← the EDIT tree
        build_episode_tools(…)  → mutation_usage + validate_patches, bound to this round
        request = build_request(config, brief, skills, evidence, read=snapshot,
                                write=copy, verify_retries=ctx.max_retries)
        capture the instructions + task → proposer_inputs.jsonl
        foe.start_config(…)   → one process; its pid → active_runs
            └ the episode edits the copy, calls validate_patches, returns
        project_onto_mutation_points(copy vs snapshot) → the patch set
        parse_experiment_json → enforce_forbidden → scratch validate(candidate)
            └ derive_scratch applies into /tmp/ztw-slate-*/child   ← slot 0's OWN tree
      the working copy and the scratch lease are both removed
    slots 1, 2: … each into its own disjoint scratch tree
    post-gather pass, SLOT order: emit candidate_sampled {i, n:3} ×3
    screen: None → unscreened
    _select_best: critic call over aux_call_llm (restricted context + slate)
      → picks index 0
    _mount_chosen(candidates, 0, ctx):
      → validator(candidates[0]) derives generations/v7/…    ← THE invariant:
        the round's FIRST and ONLY write to the canonical coordinate
    emit critique_selected {index:0, reason:"critique", slate, rationale}
    return candidates[0]

  back in evolve_once:
    check_patch_manifest_and_forbidden(experiment, mutations, brief.forbidden_ids)
    child_snapshot = last_child_snapshot["path"]   # now candidate 0's tree
    emit proposal_attempted{} / experiment_minted / patches_applied
    → tournament (06-tournament-and-selection.md)
```

Points where the trace changes under non-default knobs:

- `best_of_n: 1` → the wrapper is not even constructed; ONE
  `FoeProposerAgent.propose` straight through the shared
  `validate_experiment` hook — no slate events, no critique, no scratch, and
  the canonical tree is written by that one validation.
- a proposer dir with skills → the same agent, with an `80-skills` section
  in the episode's instructions and the skill bodies folded into the
  contract hash.
- `screen_entries: 4` → after slot 2, `_screen_slate` runs each candidate on
  the rotating 4-entry train panel at replicate 3000 (+3001 confirms);
  `candidate_screened` × 3 events; survivors feed selection; all-vetoed
  triggers the one revise.
- a resume-in-place round → the propose step is SKIPPED entirely: the
  persisted experiment is re-validated once through the same hook (idempotent
  re-derive) and reused verbatim — "the proposer is non-deterministic; a
  fresh proposal would invalidate the on-disk loss.json cache"
  (`evolve_once` step 6r).
- the field path (`field_size > 1`) → `_propose_and_apply_challenger` per
  slot; each success is persisted (`write_experiment` + index ingest +
  `append_to_lineage(pending=True)`) BEFORE the next slot proposes, and the
  next slot's context carries the sibling as in-flight memory.

### 5.6.9 `ProposerQualityConfig` — the knobs in one table

(`src/zicato/core/scoring_config.py`; all contract fields — non-default
values roll the epoch; `screen_entries`, `screen_veto_only`,
`process_exemplars`, `recombine`, `recombine_merge` and `genealogy` are
omitted-at-default from the canonical form so old epochs never roll
retroactively.)

| Knob | Default | Effect | Inert when |
|---|---|---|---|
| `best_of_n` | `3` | slate size per propose step; `1` = a single sample, no critique, no wrapper object | — |
| `critique_enabled` | `true` | the one cheap aux-LLM critic pass over the slate; `false` = deterministic heuristic only | `best_of_n == 1` |
| `screen_entries` | `0` (OFF) | tryout-panel size per candidate; `> 0` builds the per-round screen closure | `best_of_n == 1` (no slate to screen) |
| `screen_veto_only` | `false` | `true` suppresses BOTH screen tiebreak feeds (critic block + heuristic key) — the screen can only disqualify | `screen_entries == 0` |
| `process_exemplars` | `0` (OFF) | max redacted event windows spliced into the prompt per round | — |
| `recombine` | `false` (OFF) | the mechanical recombination slot (§5.6.11): the last slate slot MINTS the patch union of two rejected complementary challengers instead of sampling the LLM; cost-neutral (the mint replaces the slot's propose call) | `best_of_n == 1` (no slate slot to mint into) |
| `recombine_merge` | `"mechanical"` | how the slot composes the union (§5.6.11): `"mechanical"` mints the disjoint concatenation (no LLM call, `n−1` calls); `"llm"` issues one merge call and RELAXES disjointness so an OVERLAPPING pair can be merged (substitutes the slot's sample call, `n` calls) | `recombine` off (accept-and-inert) |
| `genealogy` | `0` (OFF) | the genealogy channel (§5.6.13): up to N candidate-lineage items (champion's promoted spine + diverse rejected reign candidates, banded outcomes) spliced into the prompt for in-context evolution; render-side only (cost meter untouched) | — |

And the sibling knobs this chapter leans on:
`overfitting.restrict_proposer_visibility` (default `true` — the §5.8 master
switch), `experiment_memory.cross_epoch` (default `false` — §5.10.3),
`overfitting.random_baseline_every_n` (the placebo arm — proposer-adjacent
but minted WITHOUT the proposer; see 06-tournament-and-selection.md §6.13).

### 5.6.10 `CandidateScreenResult` — the screen's output shape

(`src/zicato/proposer/best_of_n.py`; produced by `zicato.epoch.screen`,
consumed by the wrapper. Frozen + slotted.)

| Field | Meaning | Contract notes |
|---|---|---|
| `vetoed` | disqualified from slate selection | only two causes: a CONFIRMED pass-flip on a champion-passing panel entry, or a budget abort; an all-vetoed slate still selects (revise → critic-over-all) |
| `reason` | human-readable veto/clear summary | **COUNTS ONLY by contract** — never an entry id, never a question/output token; flows into the round log AND the restricted-visibility prompt via revise feedback |
| `scalar` | aggregate panel scalar (lower = better) or `None` (no usable signal) | SELECTION-BIASED by construction — advisory tiebreak only, never journaled as evidence, never compared to tournament scalars |
| `entries_screened` | panel size this candidate ran | `0` ⇒ "not screened (no signal)" in the critic block |
| `baseline_passes` | champion replicate-0 passes on the panel — the flip-eligible subset | `0` on a cold start ⇒ crash-only screening, and no revise trigger by design |
| `candidate_passes` | candidate's panel passes | counts only |
| `confirmed` | `True` iff the veto survived the confirm re-run (flipped twice) | immediate budget-abort vetoes carry `False` |

And the interposition seam, verbatim — note there is NO wrapper object at all
in the default-off case:

```python
# src/zicato/proposer/best_of_n.py
def wrap_with_proposer_quality(
    inner: ProposerAgent, config: ProposerQualityConfig
) -> ProposerAgent:
    if config.best_of_n <= 1:
        return inner
    return BestOfNProposerAgent(inner=inner, config=config)
```

### 5.6.11 The mechanical recombination slot

Opt-in (`proposer_quality.recombine` AND `best_of_n > 1`; default OFF —
byte-identical propose path when off). The mechanism, in one sentence: **a
single champion can only ever discount ONE challenger's fix — so when two
REJECTED challengers each fixed a DISTINCT slice of the board with
non-overlapping edits, the last slate slot mints the UNION of their patches
mechanically, and a non-vetoed mint is chosen outright.** A parsimony-biased
selector rejects each single fix; the union clears the gate that neither half
could.

**The two halves.** Selection is a pure engine
(`src/zicato/epoch/recombine.py`) fed by an IO builder
(`_build_recombination_pair` in `src/zicato/evolve/round_context.py`, run
ONCE per round at the screen-builder site); minting is a second pure function
(`src/zicato/proposer/recombine.py::mint_recombined_experiment`). The builder
threads DATA — a `RecombinationPair` on `ProposerContext.recombine_pair` (§5.2)
— never a callable, so the proposer stack stays IO-free. On the field path
`_recombine_pair_for_slot` gives the pair to the slot-0 challenger ONLY
(identical mints across a field would collapse under the diversity
soft-reject).

**The 8 eligibility predicates.** Five are per-candidate (`eligible_parents`;
#1–#4 and #6 — six checks, since #6 folds two), three are pair-level
(`rank_pairs`; #5/#7/#8):

1. **rejected** — not deferred (a live evidence loop is not a settled
   negative);
2. **current reign** — `parent_generation_id == round-start champion` (the
   parent pointer IS the staleness guard; a promotion empties the pool);
3. **non-placebo** — a random-baseline arm is never a real fix (marker check);
4. **non-recombined parent** — no chains in v1 (keeps provenance one level
   deep);
5. **pair not already tried** — dedup over persisted `recombined_from`
   frozensets (a round-SPENDING mint never re-mints; a vetoed, unpersisted one
   may retry);
6. **patches reconstructable + all mutation-ids in the current manifest** — an
   unreachable target cannot be applied; a patch-free candidate has nothing to
   contribute;
7. **disjoint patch mutation-id sets** (jaccard == 0) — REQUIRED, not
   preferred: the applier re-enumerates between patches and is LAST-WINS on a
   duplicate target, so an overlap would silently drop one side's edit;
8. **complementary improved sets** — both non-empty, neither ⊆ the other (each
   parent carries a distinct win the other lacks).

Cross-regression is NOT a predicate by design — it is a ranking penalty
(below), because per-entry single-sample verdicts are noisy (the screen's
confirm-before-veto lesson, §5.6.2). Any failure anywhere → `None` → the mint
is skipped and the slot samples the LLM as usual.

**The 4-key deterministic ranking** (`rank_pairs`; each level only breaks the
previous level's ties, so the pick is reproducible for any fixed pool in ANY
input order — the shuffled-pool order-independence pin):

1. combined TRAIN coverage DOWN (union of the two improved sets — the whole
   objective);
2. cross-regression penalty UP (union of the two regressed sets — fewer
   entries put at risk);
3. summed Elo DOWN (default-filled to `DEFAULT_ELO = 1500`, so it can only
   reorder within an evidence tie);
4. lexicographic `(gid_a, gid_b)` ascending — the total-order backstop.

**The mint + the `recombined` mode.** The minter applies patches A-then-B in
ascending-gid order (order-independent under disjointness; fixed for byte-stable
tests) with FRESH patch ids, `core_idea = "[recombined] {A[:80]} + {B[:80]}"`,
`modulating` = the union of PATCH ids (manifest-valid by predicate 6), and a
counts-only `why` (envelope-clean). In the last slot the wrapper mints instead
of sampling the LLM, still runs `enforce_forbidden` + the SAME validate hook,
and a NON-VETOED mint is chosen with `selection_mode = "recombined"` — **no
critic call**. This bypass is load-bearing: the deterministic heuristic's
minimal-diff key (§5.6.6, key 3) would systematically STARVE the union, whose
diff is larger than either parent's BY CONSTRUCTION — that parsimony bias is
exactly what the slot exists to overcome. The bypass is safe because the mint is
grounded in MEASURED per-entry evidence from two real tournament rounds, the
screen above can still veto it, and the unchanged evidence gate remains the sole
arbiter of promotion. A VETOED mint falls through to the ordinary slate paths
unchanged. The `tests/test_recombination_known_answer.py` starved-heuristic
test documents the failing alternative (heuristic-selection ⇒ the union never
wins).

**Cost-neutral.** The mint REPLACES the slot's auxiliary propose call — a
recombining round spends `best_of_n − 1` propose calls, NEVER more. The cost
meter is untouched; `estimate_cost`'s best-of-N line carries one sentence
noting this upper-bound (`src/zicato/contract_draft/operations.py`), and no `CostLine`
changes.

**The envelope.** `RecombinationPair` carries counts + patches + hypothesis
TEXT only — the improved/regressed entry-id sets are computed INSIDE
`_build_recombination_pair` (from `build_matchup_grid` per pool member) and
intersected with the current TRAIN board there, then discarded; entry ids never
reach the proposer stack. The holdout is never eligible (the `train_entry_ids`
filter), so this closes the holdout-leak and preserves context-is-the-envelope.

> ⚠️ **KNOWN NARROWING — pure drift-side complementary pairs are invisible.**
> The improved/regressed sets are PASS-FLIP sets (a champion-failing entry the
> challenger passes, and the inverse) rather than the matchup grid's drift-only
> `won_by`. Per-run drift folds every remaining defect into EVERY entry's loss,
> so a strictly-better challenger "wins" all entries on drift and two
> single-fix parents could never read as complementary — the pass bit is the
> per-entry signal a fix actually OWNS. The consequence: a pair whose
> improvements are PURELY drift-side (no pass flip — e.g. two independent
> verbosity fixes on an all-passing board) never recombines mechanically. This
> is by design: per-entry drift deltas are noisy single-sample verdicts (the
> same reason cross-regression is a ranking penalty rather than a filter).
> Such pairs
> remain reachable through the in-context genealogy channel (the LLM can merge
> the ideas itself), and a drift-delta-with-confirmation variant is a
> documented future seam. The rationale lives verbatim at the `KNOWN NARROWING`
> comment in `_build_recombination_pair`.

**Merge modes — `mechanical` (default) vs `llm`.**
`proposer_quality.recombine_merge` chooses HOW the slot composes the union
(design: PROPOSER.md §2.6.1; omit-at-default, `"llm"` rolls). `"mechanical"` is
everything above. `"llm"` instead issues ONE auxiliary merge call — the DEPTH
refinement role (`BestOfNProposerAgent._depth_call_llm`, exactly as the
self-critique call), so it SUBSTITUTES the slot's own sample call (cost:
`best_of_n` calls, the same as a recombine-off round, rather than `n−1`). The merge prompt
(`render_recombine_merge_prompt`) is rendered from the envelope-clean
`RecombinationPair` (both parents' patches, core ideas, BANDED whole-candidate
outcomes and counts-only complementarity — never an entry id); the response
flows through the NORMAL `parse_experiment_json` → `enforce_forbidden` →
validate path (`BestOfNProposerAgent._merge_recombined`), is stamped with the
same `recombined_from`, and a non-vetoed merge is chosen with the same
`selection_mode = "recombined"`. Any parse/validate failure DEGRADES to a fresh
sample (the mechanical mint's exact degrade). The ONLY selector change:
`rank_pairs(..., merge_mode="llm")` RELAXES predicate #7 (disjointness) for pair
SELECTION so an OVERLAPPING pair — which only an LLM can merge — is eligible,
and overlap becomes ranking key level 2 (prefer less overlap at equal
coverage); mechanical-mode survivors are disjoint by the #7 filter so their
overlap key is a constant 0 and the mechanical selection is unaffected.

**Seams noted, NOT built:** a chain depth cap; an index `recombined_from`
column; the scaffold default-on decision (needs live evidence); and a
`recombine_merge` distinction in the round log (the `recombined` flag already
tells consumers a mint happened — the mode is a contract-hash fact rather
than a per-candidate one). Tests: `tests/test_recombine_engine.py` (predicate +
ranking + order-independence + the relaxed-mode overlap ranking units),
`tests/test_recombination_known_answer.py` (the two-marker mechanical OC
full-loop: union minted round 3, `mode="recombined"`, promoted where
recombine-off stalls; `recombined_from == (v1, v2)`; the exact `n−1` aux-call
cost-neutrality counter; pair-dedup),
`tests/test_recombination_merge_known_answer.py` (the OVERLAP-pair `"llm"` OC:
mechanical mode mints nothing on the shared-target fixture, `"llm"` merges +
promotes, the `n`-call cost story, the garbage-response degrade).

### 5.6.12 The `new_content` style contract (what the system prompt demands)

`SYSTEM_PROMPT_TEMPLATE` (`src/zicato/proposer/prompts.py`) binds the model to
a formatting contract for `replace` payloads that downstream tooling relies
on. If you touch the template, preserve all of these — each maps to a real
consumer:

- for a **span** point, `new_content` is ONLY the inner replacement text of
  the one string literal — no function signature, no `import` lines, no
  `# zicato:mutable` marker, no other mutation points ("the harness owns the
  literal's quoting and indentation"). Violations drop imports/markers and
  the post-apply validator rejects the patch — one burned retry each;
- prose longer than ~120 chars must be broken into 80–100-char lines with
  real `\n` — "long unbroken single-line prompts are unreadable in the
  patch-diff view and are a known reviewer-friction point";
- break at natural boundaries, never mid-placeholder (`{agent_list}`) or
  mid-identifier; no leading/trailing blank line; no indentation (the applier
  re-anchors indentation when it splices);
- the response's first character MUST be `{` and last MUST be `}` — the
  clean path of `extract_json_object` depends on well-behaved output staying
  cheap.

---

### 5.6.13 The genealogy channel

Opt-in (`proposer_quality.genealogy > 0`; default `0` = OFF — byte-identical
propose path when off). The in-context analogue of AlphaEvolve's prompt
sampler: it feeds the proposer a redacted view of the current reign's
candidate LINEAGE so the LLM can evolve IN CONTEXT — extend a promoted line
or re-frame a rejected idea — reaching even the pure-drift-side complementary
pairs the mechanical recombination slot (§5.6.11) cannot see. Where that slot
merges two rejected fixes WITHOUT an LLM call, this channel hands the LLM the
raw material to merge them itself. The design + the normative redaction
contract are in **[PROPOSER.md §2.7](../design/PROPOSER.md)**;
`src/zicato/proposer/genealogy.py` is its mechanical enforcement.

**The two halves.** The sampler is a pure, deterministic function
(`sample_genealogy(records, ratings, k, *, champion_id)` — NO RNG, NO IO) fed
by an IO builder (`_build_genealogy_items` in
`src/zicato/evolve/round_context.py`,
run ONCE per round at the screen-builder site). The builder threads DATA — a
`tuple[GenealogyItem, ...]` on `ProposerContext.genealogy` (§5.2) rather than
a callable, so the proposer stack stays IO-free. Unlike the recombination
pair, the SAME items ride EVERY best-of-N slot (and the critic) — genealogy is
read-only context rather than a per-slot mint.

**What the sampler produces.** It partitions the reign's settled records:

- **Parents** — the champion's own promoted spine, built by walking the
  `parent_generation_id` chain backward from `champion_id` through the promoted
  records (`_champion_spine`), most-recent-first (the walk order), taking the
  first `k // 2`. An off-spine promotion — a promoted record NOT on the
  champion's chain — is excluded by construction; a missing/cyclic pointer ends
  the walk (a `visited` set + a pool-bound hop cap). When `champion_id` is
  `None` there is no anchor to walk from, so the spine falls back to the
  promoted records sorted most-recent-first by `round_index`. A short spine
  backfills its unused budget into inspirations.
- **Inspirations** — the REJECTED records (reign-scoped to `champion_id` — the
  recombination #2 reign guard), capped at `GENEALOGY_POOL_MAX` most-recent,
  then a **greedy max--min-Jaccard diversity walk** (`_greedy_dissimilar`):
  the seed is the best tie-break candidate (Elo down, then gid ascending), and
  each subsequent pick MAXIMIZES its minimum mutation-id-set distance
  (`1 - jaccard`) to the already-chosen set. Because the key is total, the
  selection is reproducible for any fixed pool in ANY input order (the
  shuffled-pool order-independence pin). Placebo arms are excluded from both.

Each `GenealogyItem` carries the proposer-authored `core_idea`, a
`PatchSummary` (targeted mutation ids + op kinds + a coarse size band + a
capped, head-truncated excerpt of the proposer's OWN diff text —
`_DIFF_EXCERPT_MAX`), a whole-candidate `banded_outcome`, and a static
`rationale`.

**The envelope.** Genealogy widens CANDIDATE lineage, never evaluation data.
The outcome is banded through the SAME `_bucket_scalar_delta`
(`improved`/`flat`/`regressed`) vocabulary the experiment memory uses — the
exact Δscalar never escapes. There is NO per-entry read anywhere in the
sampler, so there is no per-entry slice to leak: no entry id, no per-entry
result, no holdout-derived value can enter a channel that never looks at board
entries. The `GenealogyRecord`/`GenealogyItem` types have no field that could
carry an entry id (a structural pin in `tests/test_genealogy.py`). The
redaction (band + excerpt cap) is enforced IN the sampler and tested there,
not trusted to the caller.

**Cost.** Render-side only — the meter is untouched (the process-exemplars
precedent). `_build_genealogy_items` reads the durable records + one
best-effort Elo fold; ANY exception → `()` → a round with no genealogy block.

**Determinism = the leakage budget.** A byte-identical block round over round
(while the reign's candidate set is unchanged) re-presents nothing new.

**Seams noted, NOT built:** an LLM-guided merge behind the same channel
(rides the ensemble depth role); cross-reign genealogy retention; the scaffold
default-on decision (needs live evidence). Tests: `tests/test_genealogy.py`
(the greedy known-answer + order-independence, the parent/inspiration budget
split, the adversarial redaction fixtures, the byte-identical-at-default
render golden, and a seeded A/B power measurement — genealogy vs a recency
baseline, measuring the mergeable-pair rate, PRINTED with a no-regression
assert).

---

## 5.7 The round-log vocabulary of the propose step

The propose step traces itself into the round's durable event log
(`epochs/{epoch}/rounds/{round}/round_log.jsonl` —
`src/zicato/epoch/round_log.py`) through the `round_event_emitter` seam on the
context. The seam exists so the proposer stack never imports the log module;
the orchestrator threads `_RoundLogEmitter.emit`, and every emission is
**best-effort by contract** — `_emit_round_event` guards the call so a raising
emitter can never fail a propose step. The wire record keeps an event's
type-specific `payload` separate from its type-independent `scope` envelope.

Events the propose step emits, **in required order** within one propose:

| # | Event | Emitted by | Payload | Notes |
|---|---|---|---|---|
| 1..N | `candidate_sampled` | best-of-N wrapper, deterministic post-gather pass in SLOT order | `{i, n}` (`revise: false`); scope `{generation_id}` | a failed slot contributes a `proposal_attempted{errors, slot_index}` instead, so a sibling's success never discards its evidence (issue #141) |
| N+1..2N | `candidate_screened` | `_screen_slate`, one per candidate AFTER the whole slate settled | `{index, vetoed, confirmed, screen_summary{entries_screened, baseline_passes, candidate_passes, reason}, revise: false}`; scope `{generation_id}` | counts-only by the `reason` contract; absent entirely for an unscreened round |
| (opt) | `candidate_sampled` `{i: N, n, revise: true}` then `candidate_screened` `{index: N, …, revise: true}` | the ONE all-vetoed revise pass | the replacement's index is one past the original slate; both carry scope `{generation_id}` | additive fields with defaults — a log written without them decodes identically |
| last | `critique_selected` | the wrapper, after `_mount_chosen` | `{index, reason: selection_mode, slate: [{index, core_idea, mutation_ids}], rationale}`; scope `{generation_id}` | `index` is the FINAL slate index; both transports fill `slate`, and `rationale` is non-empty only when a critic chose. Emitted only once the chosen candidate's tree is mounted, so the event and the artifact cannot disagree |

Then, from `_propose_child` (outside the wrapper):

- on success: `proposal_attempted` (empty `errors` — one settled attempt;
  per-attempt fidelity lives in the failure path), `experiment_minted`
  `{experiment_id}`, `patches_applied` `{generation_id}` (the validate hook
  derived + validated the tree before the successful return, so the patches
  ARE applied by then);
- on `ProposerError`: one `proposal_attempted` `{errors: (msg,)}` **per failed
  attempt** off `exc.attempts`, then the error re-raises.

The fold (`fold_round_record`) reduces these into
`ProposalSession{attempts, errors, candidates_sampled, candidates_screened,
screen_vetoes, critique_index, critique_reason, experiment_ids}` — the shape
the dashboard and loop health read.

Every one of those events travels with a `RoundEventScope` — the log-wide
coordinate envelope (07-runtime-and-durability.md §7.10.2) — on the emitter's
third argument, and for the propose step it carries exactly one coordinate:
`generation_id`. That coordinate scopes the whole slate rather than any one
candidate: every challenger in a field round writes through the same emitter
while its candidate indexes restart at zero. A reader groups the three slate
event types by `envelope.scope.generation_id`, never by their positions in the
append-only file. A record written without a scope decodes as the empty scope,
so its slate cannot be split with certainty.

There is no ordinal coordinate to group by, because the payload's
own two numberings disagree: `candidate_sampled.i` is the slate SLOT, while
`candidate_screened.index` and `critique_selected.index` count the survivors
that reached the screen. They coincide only when no slot failed.

> ⚠️ TRAP — ordering is semantic rather than cosmetic. `candidate_screened` events
> come after ALL `candidate_sampled` events and before `critique_selected`
> because the screen runs between the slate settling and the selection; the
> `CandidateScreened` docstring pins this. A consumer (or a new emitter you
> add) that interleaves them breaks the dashboard's proposal-session
> reconstruction. Also: the log is append-only single-writer with a monotonic
> gap-free `seq`; never write it from a second process. Tests:
> `tests/test_round_log.py` (schema + fold + torn-tail),
> `tests/test_round_log_emission.py` (the evolve-path emission wiring and
> ordering).

---

## 5.8 The RESTRICTED-VISIBILITY ENVELOPE — formal spec

This is the chapter's most load-bearing section. The proposer is an optimizer
pointed at the evaluation board; anything it can see about individual board
entries, it can (and eventually will) special-case. The envelope is the formal
answer to "what may the proposer learn about the board?" (OVERFITTING.md §11
is the design source; this section maps every clause to its enforcing code and
test).

**The master switch** is
`OverfittingConfig.restrict_proposer_visibility` — **default `True`**
(`src/zicato/core/scoring_config.py`), part of the frozen contract (flipping
it rolls the epoch). It governs the two channels that carry sanitizable
identity (patterns, memory Δscalar). Every OTHER channel is
identity-free/banded **unconditionally, by construction** — they do not read
the flag because there is nothing to reveal even when it is off.

### 5.8.1 What may NEVER reach the proposer

| Forbidden material | Why | Where it is stopped |
|---|---|---|
| **Board entry ids** | the unit of special-casing: a named entry can be memorized and gamed | `_LEAKY_DETAIL_KEYS = {affected_entry_ids, entry_id, task_id, agent}` stripped by `_aggregate_pattern_detail`; the exemplar window-local anonymization and identity scrub (ids never emitted, scrubbed from free text); screen reasons counts-only; memory digest carries generation ids, never entry ids |
| **Task / question text** | the input to memorize | detectors never put raw inputs in `detail`; the exemplar field allowlist drops `run_started.goal_summary` ("goal_summary IS the task prompt") and the identity scrub removes quotations of it |
| **Model outputs** | answers to copy | the exemplar field allowlist (`run_completed` / `task_completed` / `task_progress` summaries "ARE model output"); outcome marginals carry rates only |
| **Anything holdout** | the holdout exists to detect memorization; showing it defeats the design | the orchestrator computes EVERY proposer input from the TRAIN slice (`split_board` + `rotation_seed`, step 4 of `evolve_once`); the exemplar extractor intersects pattern-named entries with `train_entry_ids` and "cannot widen the slice it is given"; `select_screen_entries` is handed the train board only |
| **Raw per-entry outcomes** (which entry passed/failed, per-entry scores) | the response surface to climb entry-by-entry | losses reach the proposer only through aggregates: the one-line loss summary, the banded failure profile, pattern counts |
| **Exact Δscalar / exact rates** (under the default posture) | the round-over-round response surface — lets the proposer climb the BOARD rather than true quality | `_bucket_scalar_delta` (improved/flat/regressed; flat band = 0.01 = the default promote margin), `_band_rate` (~10% steps), `_band_quality` (thirds) |

### 5.8.2 The permitted channels, one by one

Each row: what reaches the prompt, the transformation that makes it safe, the
enforcing code, and the test that pins it.

| Channel | Reaches the prompt as | Transformation | Enforcing code | Test |
|---|---|---|---|---|
| Detector **patterns** | kind, severity, summary, sanitized detail, affected **mutation** ids | identity detail keys dropped; `entries_affected=N` count substituted for the id list | `render_pattern_block(restrict=True)` → `_aggregate_pattern_detail` (`src/zicato/proposer/prompts.py`) | `tests/test_proposer_prompts.py` |
| **Experiment memory** | per-experiment verdict + bucketed Δscalar + targeted ids + core idea | `Δscalar=improved/flat/regressed` under restrict; cross-contract entries carry NO delta at all | `_render_prior_experiment_line(restrict=…)`, `_bucket_scalar_delta` | `tests/test_proposer_prior_experiments_block.py` |
| **Failure-mode profile** | banded recall/precision decomposition, banded failure-mode rates, banded pass-rate/score | every number through `_band_rate` / `_band_quality`; summary object carries marginal rates only (no id/question/output token) | `render_failure_mode_profile` + `aggregate_outcome_marginals` (`src/zicato/analyzer/outcome_marginals.py`) | `tests/test_outcome_marginals.py`, `tests/test_proposer_prompts.py` |
| **Operator outcome-marginals hook** | extra named banded rates (`- <name>: ~N% of runs`) | `run_operator_summarizer` sanitizes: numeric-only values, names filtered — the operator hook's output "cannot leak" | `src/zicato/analyzer/outcome_marginals.py::run_operator_summarizer` | `tests/test_outcome_marginals.py` |
| **Process exemplars** (opt-in) | redacted event windows: relative offsets, case names, allowlisted fields, `task-N` tokens | the four redaction rules (§5.8.3) | `src/zicato/analyzer/process_exemplars.py` | `tests/test_process_exemplars.py` (per-rule adversarial fixtures), `tests/test_process_exemplars_e2e.py` |
| **Outcome marginals under the sanitizer** | folded into the failure profile above | banding + board-anonymity by construction | as above | as above |
| **Fertility annotations** (mutation track records) | one advisory line per manifest point: `touched:N promoted:K/N Δscalar[best:… median:… worst:…] recent/stale (…; not causal)` | counts are experiment-level aggregates; deltas bucketed via `_bucket_scalar_delta`; recency a coarse flag; the HONESTY label ("experiments touching this point … not causal") is mandatory | `render_mutation_track_annotation`; the tool twin in `src/zicato/proposer/tools.py::mutation_track_record` | `tests/test_mutation_track_record.py` |
| **Static hints** | the per-slot edit-class hint | static instruction strings only — nothing to transform | `src/zicato/proposer/hints.py` (docstring contract) | `tests/test_proposer_hints.py` |
| **Loss summary** | one line of board-wide means | aggregation (mean drift loss, pass rate over N entries) | `_render_loss_summary` (`src/zicato/evolve/decision_support.py`) | `tests/test_orchestrator.py` |
| **Screen feedback** (revise + critic block) | counts-only veto/clear summaries | the `CandidateScreenResult.reason` counts-only contract; `_render_screen_note` / `_render_revise_feedback` compose only from it | `src/zicato/epoch/screen.py::_summarize`, `src/zicato/proposer/best_of_n.py` | `tests/test_candidate_screen.py`, `tests/test_proposer_best_of_n.py` |
| **Telemetry insights** | the analyzer's LLM-summarized markdown | produced by the analyzer (`src/zicato/analyzer/`) over the same round artifacts, under its own discipline | `load_latest_insights` | `tests/test_analyzer_insights.py` |
| **Mutation manifest** | full code-span content | code identity rather than board identity — unrelated to the split, and left untouched | `render_mutation_block` | — |

### 5.8.3 Process exemplars: the R1–R4 redaction rules

`docs/design/PROCESS-EXEMPLARS.md` is normative;
`src/zicato/analyzer/process_exemplars.py` is the mechanical enforcement —
**no LLM redactor, ever**. An exemplar is a ±3-event window
(`_WINDOW_RADIUS`) around one anchor event chosen for one detected pattern
("anchored on released information" — the pattern block already told the
proposer the failure shape; the exemplar adds only *mechanism*). Extraction is
deterministic and byte-stable (sorted entry order, first match wins, no RNG,
no wall clock), so re-presenting the block round over round leaks nothing new;
the leakage budget is ≤ `cap` windows per (champion, pattern-set) state.

| Rule | What it does | Implementation | Adversarial fixture behaviour the tests pin |
|---|---|---|---|
| **R1 — default-deny field allowlist** | every payload case not in `_FIELD_POLICY` renders as a bare case marker (offset + case name, NO fields): the window's SHAPE survives, its content does not. Listed cases enumerate `keep` (closed-vocabulary/structural/harness identity), `truncate` (free process text), `anonymize` (per-window id tokens), `plan_structure` (plans as `"N tasks, M edges"` counts — no titles) | `_CasePolicy`, `_FIELD_POLICY`, `_redact_event` | `run_started` (its `goal_summary` IS the task prompt), `run_completed`/`task_completed`/`task_progress`, and every LLM-call bookend are UNLISTED by design and must render field-less |
| **R2 — window-local anonymization** | task/invocation ids map to `task-1`, `task-2`, … **rebuilt per window** — "the same task keeps failing" stays visible inside a window, but nothing correlates across windows, rounds, or back to the board; entry ids are never emitted; offsets are anchor-relative (`0`), never absolute sequence numbers (absolute positions could fingerprint an entry) | `_WindowAnonymizer`, `ExemplarEvent.offset` | the same raw id in two windows gets independent tokens |
| **R3 — free-text truncation** | `truncate`-class fields capped at 160 chars, head 120 / tail 24 joined by ` … `; runs AFTER R4 so a scrubbed text can never re-form an identity string across the split | `_truncate_free_text` | a long drift `detail` keeps its head+tail, elided middle |
| **R4 — the identity corpus + scrub** | every DROPPED string value across the WHOLE file (≥ 12 chars — `_MIN_SCRUB_LEN`, so enum-ish strings don't mangle text) is substring-scrubbed out of every KEPT free-text value, longest-first; identity TOKENS (entry id, run/session/event ids, every raw task/invocation id) are scrubbed at ANY length on word boundaries; replacement is `[withheld]` | `_identity_corpus`, `_scrub_identity` | a drift detail that QUOTES the task prompt verbatim loses the quote mechanically — the defense-in-depth behind R1 |

Two more structural guarantees: the extractor only ever reads
`events.jsonl` files of the caller-supplied `train_entry_ids` under the
champion generation (pattern-named entries are intersected with the slice;
empty intersection ⇒ skipped) — "it never reads the board, so it cannot widen
the slice it is given"; and drift-kind/severity enum wire forms are normalized
to the canonical lowercase strings so the block speaks the pattern block's
vocabulary.

The channel is **opt-in and off by default** (`proposer_quality
.process_exemplars = 0`), and the scaffold does not set it: the screen is
evaluation-side, but exemplars widen the proposer-visibility channel, so the
operator opts in under the design doc's §5 harm-detection runbook. A non-zero
cap rolls the epoch. Extraction is best-effort — any failure renders `""` and
the round proceeds untouched (`_render_process_exemplars_block` wraps it in
`best_effort`).

### 5.8.4 Train-slice plumbing (where the slice is decided)

One place: `evolve_once` step 4 (`src/zicato/evolve/round_entry.py`).
`rotation_seed(weights.overfitting, epoch_id)` + `split_board(board, …)`
produce `train_ids`; `train_board` filters the board; the champion's
`losses` are loaded for train entries only. Everything downstream —
`detect_patterns`, `_render_loss_summary`, `_render_failure_profile`,
`_render_process_exemplars_block`, `select_screen_entries` — is fed that
slice. When the board is too small to split, the train slice IS the full
board and every artifact is computed over the whole board (the default-safe
degrade). The split mechanics are `zicato.board.split`
(`docs/design/OVERFITTING.md` §3); the Ladder budget over the holdout is
04-evaluation-statistics.md §5.

### 5.8.5 Who else lives inside the envelope

- the best-of-N **critic** (§5.6.7) — same renderer, same flags;
- the **hint conditioner** (§5.6.1) — parses the rendered banded profile;
- the **revise feedback** — counts-only screen reasons;
- the read-only **tools** (§5.9) — the mutable snapshot, the journal, the
  insights, and the banded track record; a tool-using proposer "learns
  nothing the annotated manifest would not already show."

### 5.8.6 Bands and buckets — the exact vocabulary

| Function | Input | Output vocabulary | Notes |
|---|---|---|---|
| `_bucket_scalar_delta` | experiment-level Δscalar (loss; negative = improvement) | `improved` / `flat` / `regressed` | flat band `±0.01` = the default promote margin, so a within-noise move reads `flat` |
| `_band_rate` | fraction of runs `[0,1]` | `none` / `~10%` … `~90%` / `~all` | nearest-10% rounding; a tiny-but-nonzero rate clamps UP to `~10%` so "rarely" never reads "never" |
| `_band_quality` | quality mean `[0,1]` | `low (~0.3)` / `medium (~0.6)` / `high (~0.9)` | thirds split |
| `_band_prediction_accuracy` | accuracy `[0,1]` | `low` / `medium` / `high` | ALWAYS applied, restrict or not |

### 5.8.7 The channel-author's checklist

Adding any new prompt-context channel? Work §5.11.2's recipe, whose gate is
this checklist. If you cannot tick every box, the channel does not ship:

1. **Banded/aggregated/redacted?** No exact per-entry value, no exact
   round-over-round number; reuse the §5.8.6 vocabulary.
2. **Train-only?** Sourced exclusively from the step-4 train slice; the
   holdout must be structurally unreachable rather than merely filtered late.
3. **Identity-free?** No entry id, task text, or model output can appear —
   including via quotation inside free text (the exemplar identity scrub is
   the precedent).
4. **Capped?** A hard ceiling on rendered size, so the channel cannot flood
   the context window or smuggle arbitrary content by volume.
5. **Empty-string sentinel at default?** Knob-off rounds byte-identical;
   contract canonical form untouched at default.
6. **Pre-rendered by its owner?** The orchestrator renders; the context
   carries an opaque string; the agents only forward (the
   `failure_profile`/`process_exemplars` pattern).
7. **Tested with adversarial identity fixtures?** A test that PLANTS an entry
   id / task text / holdout entry in the channel's raw inputs and asserts the
   rendered block does not contain it (`tests/test_process_exemplars.py` and
   `tests/test_proposer_prompts.py` are the models).
8. **On `ProposalEvidence`?** A channel reaches the model only through that
   dataclass and `evidence_from_context`'s projection. Adding it there is
   also what gives the critic parity for free: the critic renders the same
   evidence, so a channel the proposer sees is one the critic sees, and one
   it does not cannot arrive unbanded.

### 5.8.8 Envelope audit playbook

When reviewing ANY diff that touches proposer inputs, run these before
approving (they catch the classic leaks mechanically):

```bash
# 1. Who renders into the prompt? The only legitimate renderers live here:
grep -n "def render_" src/zicato/proposer/prompts.py

# 2. Did anything new start reading holdout ids? (the only legit consumers
#    of holdout_ids are the gate/ladder/tournament paths, never proposer-side)
grep -rn "holdout_ids\|_holdout" src/zicato/proposer/ src/zicato/analyzer/

# 3. Did a detector start putting raw input text into Pattern.detail?
#    (details must stay ids/counts/rates — the strip relies on it)
grep -rn "detail\[" src/zicato/patterns/detectors.py

# 4. Any new f-string interpolating entry ids near prompt assembly?
grep -rn "entry_id\|entry.id" src/zicato/proposer/ | grep -v test

# 5. Exemplar policy drift: every _FIELD_POLICY case must match
#    PROCESS-EXEMPLARS.md §3's table 1:1 (the doc is normative).
grep -n "_FIELD_POLICY" -A 60 src/zicato/analyzer/process_exemplars.py
```

Then the adversarial fixtures: `uv run pytest tests/test_process_exemplars.py
tests/test_proposer_prompts.py tests/test_outcome_marginals.py -q`. A clean
grep plus green fixtures is necessary but not sufficient — new channels still
walk §5.8.7 by hand.

---

## 5.9 The proposer's tool surface

What an episode may do is a closed list, asserted by name
(`SANCTIONED_TOOLS`, `src/zicato/proposer/foe_request.py`): `read`, `grep`,
`edit` and `block` are Foe's own built-ins, bounded by the grants; the two
host tools `mutation_usage` and `validate_patches` are answered by zicato
over the host protocol.

`src/zicato/proposer/tools.py` ships `mutation_usage` and `grep_mutable`,
the sandboxed search it is built from; `src/zicato/proposer/validate.py`
ships `validate_patches`. All three are plain module-level functions reading
a bound per-round context.

### 5.9.1 The contextvar binding

A tool function cannot carry per-round context as a bound argument: the
implementations are module-level and reused across every challenger. The
tools therefore read a module-level
`contextvars.ContextVar[ProposerToolContext | None]`. That plumbing lives in
`zicato/proposer/tool_context.py`, which is the import site: the split exists
so `validate.py` can reach the context without importing a sibling's whole
closure (see the callout at the end of §5.9.2). The
contextvar is what the episode's host tools set around each call via
`bind_proposer_tool_context(tool_ctx)` — set on entry, **reset to the prior
value on exit even on exception**. A `ContextVar` rather than a plain global
means concurrent challengers — each episode serviced on its own asyncio task
— never leak context into one another. A tool called with no bound context
raises a clear `RuntimeError` ("proposer tools may only be called from within
a bound proposer tool context") rather than returning a misleading empty
result.

`ProposerToolContext` carries four things:

- `workspace_root` — where `validate_patches` resolves the declared static
  checks and the load probe.
- `generation_root` — the PARENT snapshot `grep_mutable` walks and
  `validate_patches` re-enumerates a draft against. It is resolved via the
  generation store's pure path math in `_resolve_generation_root`.
- `mutations` — the round's manifest tuple.
- `epoch_id` and `generation_id` — the round's coordinates. No tool resolves
  anything from either one.

### 5.9.2 The tools

| Tool | Reads | Sandbox / caps | Failure behaviour |
|---|---|---|---|
| `mutation_usage(mutation_id)` | where the point's symbol (the trailing `__`-segment of its id) and short single-line literal value are referenced across the snapshot | delegates to `grep_mutable` with `re.escape`, so the containment guard + match cap apply unchanged | `ValueError` on an id outside the round's manifest |
| `validate_patches(patches_json)` | nothing — it WRITES the working copy's projected patch set into a throwaway `ztw-pvalidate-*` scratch copy of the parent snapshot and reports what broke, as `{"ok", "errors", "tiers"}` | three tiers, stopping at the first failure: structure (incl. the `content_hash` pre-image guard) + apply + A1–A4; the contract-declared static-check delta; the sandboxed `adapter.load` probe. Per-check timeouts (120s / 60s), output capped at 4 000 chars, scratch tree removed in a `finally` | `ValueError` on an argument that is not a usable patch array; a check that could not run is a NOTE (never `ok: false`) |

`grep_mutable(pattern)` is not itself served to an episode — Foe's own `grep`
is. It is the search `mutation_usage` is built from: a regex over every file
under the generation root, returning each hit as `path:line: text`. It skips
unreadable and binary files, caps the result at `_GREP_MATCH_LIMIT = 200`
matches and annotates the text when that cap is reached, and answers a
pattern that matches nowhere with the explicit `"(no matches)"` rather than a
blank string. An invalid regex raises `ValueError`.

Both host tools are built per episode in `zicato/proposer/foe_agent.py`
(`build_episode_tools`), which binds them to that round's context and answers
the episode's calls over the host protocol. So the containment guard has one
implementation, and the tool list is the only place the sanctioned surface is
decided. One binding per challenger: the context var is process-wide, so a
shared binding would cross-bind concurrent rounds.

**The searchable surface is the WHOLE snapshot** (read this before narrowing
it). `grep_mutable` walks `ctx.generation_root` rather than the declared
mutable subtrees inside it, so a mutable value's non-mutable CONSUMERS are
found too. Consider `runner.py` importing `agent/prompts.py`'s
`SYSTEM_PROMPT`: that line is what tells the proposer who reads the value it
is about to rewrite, and it sits outside the declared `agent` subtree.
Narrowing the walk to the declared subtrees loses it, and reaching such
consumers is the whole reason grounding works. What the proposer may CHANGE
stays narrow independently: patches are addressed by mutation id, and the
applier writes only what an id covers.

> ⛔ NEVER add a tool that writes to the GENERATION SNAPSHOT. "A proposer tool
> that mutated the snapshot would corrupt the very tree the round is about to
> patch" — and the round would then score a tree nobody derived.
>
> `validate_patches` is the one tool that writes anything, and it does not
> relax that rule: it writes only into a disposable scratch copy in the OS temp
> root, removed in a `finally`. The line it must also satisfy is the governing
> principle of [PROPOSER.md §2.10](../design/PROPOSER.md) — *the proposer may
> check its patch by any means that consumes no board data and produces no
> scores; it may never execute board entries* — pinned by the
> `"the proposer's patch validator has no path to the board"` import-linter
> contract and by the runtime closure test in
> `tests/test_proposer_validate.py`. Two structural details exist to keep that
> pin satisfiable, and must survive any refactor: the tier-3 probe lives in
> `zicato/proposer/_load_probe.py` and is reached by SPAWNING a subprocess (so
> the adapters stay on the forbidden list), and the contextvar plumbing lives
> in `zicato/proposer/tool_context.py` (so reaching `_active_context` does not
> pull a sibling module's whole closure into the property being proved).

**The pre-image guard is the only reader of `MutationPoint.content_hash`.**
The enumerator writes the field, the CLI and the dashboard render it, and the
applier does not read it — despite that field's docstring having long claimed
otherwise. Tier 1 of `validate_patches` is the one check that reads it: it
compares `content_hash` between the manifest bound on the tool context (what
the proposal was drafted against) and a fresh enumeration of the parent
snapshot, so a point rewritten under the proposer is caught while a fix is
still cheap.
`tests/test_proposer_validate.py::test_content_hash_has_exactly_one_reader`
pins that this stays the ONLY comparison site; plenty of mentions with zero
readers is how a docstring's claim goes unchecked.

### 5.9.3 What of the tool surface folds into the contract hash

`_canon_proposer` (`src/zicato/epoch/contract.py`) canonicalizes
`{agent_id, tools (sorted), skills (name + normalized-body sha)}`, plus an
`external` key carrying the implementing class and the digest of its causal
surface. For the Foe agent that digest is the RUNTIME's own contract
fingerprint, and Foe hashes every tool's name, description, instruction and
schema into it — so rewording `VALIDATE_PATCHES_DESCRIPTION` rolls the
epoch, and so does adding a tool to `SANCTIONED_TOOLS`.

That is the inversion worth understanding. Reconstructing the surface from
outside cannot see a description reworded inside the runtime; asking the
runtime closes the gap exactly. `ProposerSpec.tools` is still present in the
canonical form and is now read off the SAME identity mapping that is hashed
(`resolve_external_spec` takes it from `identity["tools"]`), so the
sanctioned set and the hashed set cannot drift apart.

What is deliberately NOT hashed is the resolved grant PATHS: only their
shape is. A round's snapshot and its working copy live at paths that differ
every round, and hashing them would roll the epoch on every round while
saying nothing about how the proposer reasons.

> ⚠️ TRAP — a new tool's OUTPUT must be inside the envelope (§5.8.7's
> checklist applies to tool outputs exactly as to task channels), and its
> DESCRIPTION is model-visible and hashed. The `mutation_usage` host tool is
> the worked precedent on both counts: it emits the same banded shape the
> manifest annotation does, and its description lives in one module-level
> constant so rewording it is a deliberate act.

---

## 5.10 Experiment memory

The `## What's already been tried` channel: settled cross-round history plus
this round's in-flight siblings, assembled by the ORCHESTRATOR (the proposer
stays a pure prompt-assembler and never reads the index itself).

### 5.10.1 Same-epoch: curation, cap, banding

`prior_experiments_for_epoch(db_path, epoch_id, max_entries=12, cross_epoch=…)`
(`src/zicato/index/query.py`; the cap constant is
`EXPERIMENT_MEMORY_MAX_ENTRIES = 12` in `src/zicato/core/experiment.py`):

- **settled rows only** (`tournament_decision IS NOT NULL`) — an unsettled
  row carries no learning signal and would surface the current round's own
  just-written, outcome-less experiment;
- **curation within the cap**: ALL `promoted` wins first (newest-first — wins
  are rare and high-value, never dropped while budget remains); then the K
  most recent `rejected` (K = remaining budget; recency gates the window),
  re-ranked within the window by sharpest regression first (most-negative
  Δscalar; a missing delta sorts least-sharp so a near-zero rejection falls
  off first); then `deferred` if budget remains;
- `modulating` ids lifted from `hypothesis_json` (empty tuple on any decode
  failure — never raise);
- `prediction_accuracy` graded per row (§5.4.6);
- the read is **best-effort end to end**: a missing `zicato.index` module, a
  never-built database, any exception ⇒ `[]` at debug level
  (`_load_prior_experiments`) and the prompt simply omits the section.
  `experiment.json` on disk stays canonical; `zicato repair index` rebuilds.

Rendering (`render_prior_experiments_block`) groups by decision into
advisory blocks — promoted ("build on these"), rejected ("do NOT re-propose
unless something changed"), deferred ("weak signal"), in-flight ("diversify
away from these") — and is explicitly advisory: "forbidden-ids remains the
only hard gate."

### 5.10.2 In-flight siblings (the field loop)

On a multi-challenger field, each successive slot's context gets
`prior_experiments = prior + tuple(siblings)` — the settled digest plus the
round's already-minted cohort as `decision="in_flight"` entries (no outcome,
no delta) — so challenger k+1 diversifies away from challengers 1..k. The
prompt renders them under "Proposed this round, not yet evaluated (diversify
away from these)". The hard duplicate/overlap enforcement is separate
(`_duplicates_inflight_sibling` / `_max_overlap_with_accepted` +
`diversity_tolerance` — a soft-reject decision in `_mint_challenger_field`),
but the memory channel is what gives the proposer the chance to diversify
BEFORE being rejected.

### 5.10.3 Cross-epoch memory (opt-in) and its separation rules

`experiment_memory.cross_epoch: true` (`ExperimentMemoryConfig` — a contract
field, omitted-at-default from the canonical form so old contracts never roll
retroactively). The rules, all enforced in
`_cross_contract_settled_rows` / `_cross_contract_entries`:

| Rule | Enforcement |
|---|---|
| only epochs sharing the CURRENT epoch's **non-empty** `contract_hash` | the SQL join on `epochs.contract_hash`; an epoch with an empty hash is never treated as transferable |
| a DIFFERENT contract hash is never surfaced, knob or no knob | same predicate |
| cross entries are clearly flagged | `same_contract=False`; rendered in their OWN separated block ("From PRIOR epochs under the same contract … directions only, deltas do not transfer") with epoch-tagged labels `epoch::generation` |
| **no numbers transfer** | `scalar_score_delta=None` forced at the reader ("the restricted-visibility envelope must not depend on the renderer"); `prediction_accuracy=None` (calibration is same-epoch diagnostics) |
| same-epoch history keeps priority | cross entries fill ONLY the budget same-epoch entries left — a busy epoch leaves no budget, which realizes the design's "only when same-epoch history is sparse" |
| curation mirrors same-epoch in miniature | promoted newest-first, then recent rejections, then deferred, bounded by the leftover budget |

Tests: `tests/test_index_prior_experiments.py` (curation/cap/cross-epoch),
`tests/test_proposer_prior_experiments.py` +
`tests/test_orchestrator_prior_experiments.py` (threading + sibling
injection), `tests/test_proposer_prior_experiments_block.py` (rendering +
banding).

---

## 5.11 Recipes

### 5.11.1 Recipe: add a proposer tool

Goal: give a proposal episode a new read-only grounding capability, served
as a host tool.

1. **Write the tool as a plain module-level function** in
   `src/zicato/proposer/tools.py`. Signature: JSON-friendly positional args,
   `-> str`. First line of the body: `ctx = _active_context()` — never accept
   the context as a parameter (the agent is constructed once; §5.9.1). The
   docstring is model-facing (the tool's description is built from it, and
   Foe HASHES that description): state what it returns, what raises, and the
   caps — and know that rewording it rolls the epoch.
2. **Sandbox every read.** Anything that touches the snapshot delegates to
   `grep_mutable` (the `mutation_usage` precedent — `re.escape` plus the
   existing containment guard and match cap for free) or resolves under
   `ctx.generation_root` and rejects anything that lands outside it.
   Anything that touches the workspace uses the canonical path helpers
   (`zicato.core.workspace`), never string-joined paths.
3. **Cap the output.** Add a module constant (`_<NAME>_LIMIT_CHARS`) and
   annotate truncation in the returned text (`grep_mutable`'s match cap is
   the pattern) — a runaway tool floods the agent's context window.
4. **Respect the envelope for the OUTPUT.** If the tool reads anything
   board-adjacent, its output must be banded/aggregated/identity-free
   (§5.8.7): band it through the vocabulary the prompt renderers already use
   so tool and evidence speak one shape, and label an aggregate that is not
   causal as not causal. A tool must never expose holdout material, per-entry
   outcomes, or task text.
5. **Make bad input an actionable error.** Unknown ids raise `ValueError`
   saying what a valid one would be (`"only ids in the round's manifest are
   valid"`) — the agent retries with a corrected call. Missing-but-legitimate
   state returns an explicit sentinel string (`""`, `"(no matches)"`), never
   an exception.
6. **Register it**: add the name to `SANCTIONED_TOOLS` and, for a host tool,
   build it in `build_episode_tools` with its description in a module-level
   constant beside `MUTATION_USAGE_DESCRIPTION`. Both are model-visible and
   hashed, so the registration IS a contract edit — say so in the changelog.
   If the proposer should be TOLD when to reach for it, extend the relevant
   `CHARTER_SECTIONS` entry with one sentence.
7. **Lazy-import anything heavy** inside the function body (`# noqa: PLC0415`
   — the module must stay importable without optional extras; every existing
   tool models this).
8. **Tests** in `tests/test_proposer_tools.py`: out-of-context
   `RuntimeError`; the happy path under `bind_proposer_tool_context`; the
   sandbox (a match planted outside the snapshot stays invisible); the cap
   (annotated truncation); the unknown-id error text; and — if
   board-adjacent — an adversarial identity fixture (§5.8.7 item 7).
9. **Verify**:
   ```bash
   uv sync --all-extras
   uv run pytest tests/test_proposer_tools.py \
       tests/test_proposer_foe_agent.py tests/test_proposer_contract_identity.py -x -q
   uv run ruff check src/zicato/proposer/tools.py && uv run mypy src/zicato/proposer/tools.py
   # prove the contract hash did NOT move for an unchanged workspace:
   uv run pytest tests/test_epoch_contract.py -q
   ```
   If you skipped step 5's `ValueError` wording, the agent gets a generic
   tool-error and burns retries guessing; if you skipped step 7, importing
   the tool module starts requiring an optional extra everywhere it is
   imported, and minimal environments break at import time.

### 5.11.2 Recipe: add a prompt-context channel WITHOUT violating the envelope

Goal: surface a new class of round evidence to the proposer. This is the
highest-risk change class in the subsystem — work §5.8.7's checklist as the
gate. The worked precedents, from simplest to fullest:
`failure_profile` (banded aggregate block), `mutation_track_records`
(banded per-manifest-point annotation), `process_exemplars` (opt-in redacted
content with its own design doc).

1. **Decide the envelope class first** (before any code): can the signal be
   expressed as counts/bands over the train slice? If it inherently needs
   per-entry identity or raw content, STOP — either redesign it as an
   aggregate, or follow the full PROCESS-EXEMPLARS path: a normative design
   doc, mechanical (never LLM) redaction rules each implemented by a named
   function with its own test, an opt-in contract knob, and a harm-detection
   runbook.
2. **Build the extractor/renderer next to its data** rather than in the proposer:
   analyzers in `src/zicato/analyzer/`, index reads in
   `src/zicato/index/query.py`. It must be a pure function of train-slice
   inputs, deterministic (no RNG/clock) so re-presentation leaks nothing new,
   and it returns `""` (or `{}`/`[]`) for every no-data/knob-off state.
3. **Band every number** with the §5.8.6 vocabulary — reuse `_band_rate` /
   `_band_quality` / `_bucket_scalar_delta` from
   `src/zicato/proposer/prompts.py`; do not invent new band edges without a
   reason you can defend in the design doc.
4. **Pre-render in the orchestrator** (`_render_<channel>` beside
   `_render_failure_profile`), wrapped in `best_effort` so a channel failure
   never aborts a round, gated on its knob so the off state does zero work.
5. **Thread it as an opaque string**: a new `ProposerContext` field with
   default `""` + docstring stating the envelope class; a matching
   `ProposalEvidence` field; and one line in `evidence_from_context`. That
   projection is also what gives the critic parity for free — it renders the
   same evidence. Add the context field to `_propose_child` and BOTH its call
   sites (§5.0 trap).
6. **Splice in `render_evidence`** with the empty-string sentinel and an
   explicit position (§5.5's table is the authority; update it in this
   guide). Put it in the TASK, never the instructions: a per-round value in
   the instructions would roll the epoch every round. If the content is
   redacted material, prepend a banner restating the redaction contract (the
   process-exemplars precedent) so the model reads it as anonymized
   mechanism rather than named evidence.
7. **Contract accounting**: if the channel changes what the proposer can
   learn, it needs a knob on `ProposerQualityConfig` (or `OverfittingConfig`),
   omitted-at-default from the canonical form (`epoch/contract.py`'s
   `_SCORING_OMIT_AT_DEFAULT_FIELDS` pattern) so existing epochs never roll
   retroactively — and a non-default value MUST roll the epoch. Add the field
   to `tests/test_contract_serializer_completeness.py`'s expectations.
8. **Tests — all four kinds or it does not ship**:
   - *byte-identical-at-default*: knob off ⇒ prompts byte-equal to the same
     round assembled without the field (`tests/test_proposer_prompts.py` has
     the pattern);
   - *banding*: exact inputs ⇒ exact banded output vocabulary;
   - *adversarial identity*: plant an entry id, task text, AND a
     holdout-entry artifact in the raw inputs; assert none survives to the
     rendered block (`tests/test_process_exemplars.py` per-rule fixtures are
     the model);
   - *train-slice*: hand the extractor a pattern/input naming a holdout
     entry; assert it is narrowed out (the exemplar extractor's
     intersect-with-train behaviour is the model).
9. **Verify**:
   ```bash
   uv run pytest tests/test_proposer_prompts.py tests/test_proposer_proposer.py \
       tests/test_proposer_best_of_n.py tests/test_epoch_contract.py \
       tests/test_contract_serializer_completeness.py -x -q
   uv run pytest tests/ -q -k "your_channel_name"
   ```
   If you skip the adversarial fixture, the first leak will be silent — the
   prompt is only ever seen by the model, and nothing else in CI reads it.
   If you skip contract accounting, either every existing epoch rolls on
   upgrade (canonical form grew) or a behaviour-changing knob fails to roll
   the epoch (comparability silently broken) — both are release blockers.

---

## 5.12 Cross-references

- 03-contract-and-epochs.md §3.2.6 — `_canon_proposer` and skill
  normalization; §3.4 — the omitted-at-default fields.
- 04-evaluation-statistics.md §5 — the Ladder budget that governs holdout
  queries; the slice everything in §5.8 hangs off is
  `docs/design/OVERFITTING.md` §3.
- 06-tournament-and-selection.md §6.1.1 — where the screen's 3000/3001 slots
  sit; §6.3 — why the screen's ephemeral trees and phantom dirs matter to the
  reaper.
- 07-runtime-and-durability.md §7.6.1 — the `proposing:`/`applying:`/
  `screening:` phases the propose step beats.
- 09-dashboard-and-query.md §9.11 — how the server-projected pipeline stepper
  renders what §5.7 emits.
- 12-bug-casebook.md Cases 6 and 7 — the tree/selection mismatch pair that
  §5.6.5's mount funnel exists to prevent.
- 13-recipes.md — the short-form index that points back at §5.11.

---

## 5.13 The data shapes the proposer speaks

Quick-reference field tables for the four value types every proposer change
touches. (Full definitions: `src/zicato/core/patterns.py`,
`src/zicato/core/mutation.py`, `src/zicato/core/experiment.py`.)

**`Pattern`** — one detector observation (advisory input):

| Field | Meaning | Envelope note |
|---|---|---|
| `id`, `kind`, `severity` | detector identity + class + weight | identity-free |
| `summary` | one human line | detectors keep it aggregate |
| `detail` | `dict[str, str]` of structured evidence | MAY carry `affected_entry_ids` / `entry_id` / `task_id` / `agent` — the keys `_aggregate_pattern_detail` strips under restrict |
| `affected_mutation_ids` | the manifest ids the detector suspects | drives `_targets_observed_failure` + the exemplar anchor narrowing |

**`MutationPoint`** — one valid patch target:

| Field | Meaning |
|---|---|
| `id` | stable id; the trailing `__`-segment names the marked symbol (what `mutation_usage` greps) |
| `kind` | `"span"` (one string literal) or `"file"` |
| `file`, `line_start`, `line_end` | location in the snapshot |
| `content` | the CURRENT full span/file content — rendered in full (§5.5) |
| `metadata` | constraint map: `min`/`max` (numeric range), `enum` (comma-separated domain), placeholders |
| `source_root` | the registered mutable tree the point came from; its basename re-bases into the snapshot |

**`Experiment`** — the proposer's output (also the journal's record):
`id` (`exp_{epoch}_{gen}`), `epoch_id`, `generation_id`,
`parent_generation_id`, `proposed_at` (UTC ISO), `hypothesis`
(`HypothesisSpec`, §5.4.4), `patches` (frozen tuple of `Patch`), `outcome`
(`None` until the tournament settles), `round_index` (stamped by
`_propose_child` from the EVOLVE round — the authoritative birth round;
the proposer's default is 0).

**`PriorExperiment`** — one memory-digest entry: `generation_id`, `epoch_id`,
`core_idea`, `modulating`, `decision`
(`promoted`/`rejected`/`deferred`/`in_flight`), `rejection_reason`,
`scalar_score_delta` (`None` for in-flight and cross-contract),
`same_contract`, `prediction_accuracy` (`None` when ungraded).

---

## 5.14 The standalone `zicato proposer propose` command

`src/zicato/cli/commands/propose.py` — ADVANCED / DEBUGGING, off the happy
path (`zicato evolve` proposes internally every round). Run it by hand to see
what the proposer does with a workspace's current evidence, without spending
a tournament on it.

It is the **same episode**: the command resolves the agent through
`build_proposer_agent` and hands it a `ProposerContext`, so the request it
builds is the request the loop builds. What differs is what it does around
that episode:

- the mutation manifest is enumerated from the parent generation's own
  SNAPSHOT (the tree the episode edits), the patterns come from
  `--patterns-from <file>` or fresh detectors, and the brief from the
  epoch's `brief.md` (falling back to `rubric.md` when only that exists);
- the round's per-round DERIVED channels are absent — the failure-mode
  profile, the metric priorities, the process exemplars, the genealogy
  sample and the calibration record. Each is computed by a round from the
  tournament state it is about to spend, and none is reconstructible
  outside one;
- there is **no post-apply validation hook** wired (no snapshot is derived),
  so a destructive patch that the round would bounce inside the episode
  survives here;
- the **best-of-N wrapper is not interposed**, so no slate, no screen and no
  critique run;
- the result lands in `epochs/<epoch>/proposals/<generation>.json` — a
  directory nothing in the loop reads. It is deliberately NOT the journal:
  writing through `write_experiment` would mint the generation the loop is
  about to mint, leaving a half-built generation in the epoch's own
  sequence.

Everything else about the loop is left alone, and
`tests/test_cli_propose.py` pins it: no board entry reaches the episode —
not the holdout's and not the train slice's — no tournament is opened, no
outcome or lineage generation is recorded, and every file the loop reads is
byte-identical afterwards.

> ⚠️ TRAP — do not conclude "my proposer change works" from `zicato proposer
> propose` alone. It exercises the episode but not the slate, the screen or
> the validate hook. The e2e truth is
> `tests/test_best_of_n_tree_integrity.py` +
> `tests/test_orchestrator_multi_challenger.py` — and never a live evolve run
> without the operator's explicit go-ahead.

---

## 5.15 Test map for the subsystem

Where to add (and what will catch) a regression, by concern:

| Concern | Tests |
|---|---|
| the episode: four endings, watchdog registration, holdout exclusion, budget | `tests/test_proposer_foe_agent.py` |
| the working copy and the projection that reads it back | `tests/test_proposer_foe_scratch.py` |
| the `proposer` block, and the refusals for a removed runtime | `tests/test_proposer_foe_config.py` |
| what moves the proposer's contract fingerprint, and what does not | `tests/test_proposer_contract_identity.py` |
| the outcome vocabulary and its round-log / scorecard readers | `tests/test_proposer_episode_outcomes.py` |
| salvage + two-pass validation, op discrimination, ranges/domains | `tests/test_proposer_structured.py` |
| judge-name normalization + metric movements | `tests/test_proposer_structured_metric_movements.py` |
| task sections, ordering, banding, omit-at-default | `tests/test_proposer_prompts.py` |
| brief parsing + forbidden enforcement | `tests/test_proposer_brief.py` |
| skills / frontmatter / spec resolution | `tests/test_proposer_skills.py` |
| what the builder resolves, and what it refuses | `tests/test_proposer_agent.py` |
| the standalone propose command, and what it must not touch | `tests/test_cli_propose.py` |
| tools: sandbox, caps, contextvar, error texts | `tests/test_proposer_tools.py` |
| best-of-N: slate, critique, heuristic, screen wiring, revise | `tests/test_proposer_best_of_n.py` |
| the tree/record agreement invariant, end to end with subprocess workers | `tests/test_best_of_n_tree_integrity.py` (+ the scripted slates in `tests/_best_of_n_slate_support.py`) |
| slot hints + dominant-mode parsing | `tests/test_proposer_hints.py` |
| screen engine: panel rotation, veto classes, confirm-before-veto, counts-only, phantom-dir hygiene | `tests/test_candidate_screen.py` |
| exemplar redaction rules (adversarial fixtures) + threading | `tests/test_process_exemplars.py`, `tests/test_process_exemplars_e2e.py` |
| outcome marginals + operator-hook sanitizer | `tests/test_outcome_marginals.py` |
| memory digest curation/cap/cross-epoch | `tests/test_index_prior_experiments.py` |
| memory threading + siblings + orchestrator wiring | `tests/test_orchestrator_prior_experiments.py`, `tests/test_proposer_prior_experiments.py` |
| fertility map | `tests/test_mutation_track_record.py` |
| restrict-visibility flag threading | `tests/test_orchestrator_overfitting.py` |
| round-log schema/fold + emission ordering | `tests/test_round_log.py`, `tests/test_round_log_emission.py` |
| meta-loop bookends | `tests/test_meta_loop_emitter.py` |
| aux timeout knob | `tests/test_aux_timeout.py` |
