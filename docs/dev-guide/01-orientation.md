# 01 — Orientation

> **Covers:** what zicato is and where it sits in its ecosystem · the complete load-bearing vocabulary, each term anchored to the file that owns it · the repo map (every package, its public face, and its import rules) · **the Golden Rules** — the ten non-negotiable invariants that keep the repo healthy · your first hour, command by command.
> **Prerequisites:** none — this is the entry chapter.
> **Invariants introduced:** [G1 vendor rule] [G2 uv-sync-all-extras] [G3 gate-live-runs] [G4 oracles-green] [G5 parity-and-contracts] [G6 omit-at-default] [G7 replicate-base-ledger] [G8 restricted-visibility] [G9 module-level-callables] [G10 digest-gated-render] [files-canonical-index-derived] [contract-edits-roll-epochs] [hypothesis-before-run]

This guide is written for coding agents extending zicato. It assumes zero
prior context: every acronym is expanded on first use, every rule states
what breaks when you violate it, and every claim is anchored to a symbol
and a file you can open. When this guide and the code disagree, the code
wins — file an erratum, do not guess.

---

## 1. What zicato is

zicato wraps a system you already have and turns it into the **inner
harness** of a learning loop. It runs that harness against a **board** of
tasks, watches what goes wrong via structured runtime telemetry, and
rewrites the harness so the next **generation** goes less wrong. The loop
is evolutionary: propose a small structured edit, run a scored
**tournament** between the incumbent (champion) and the edited copy
(challenger), and promote the challenger only when it beats a statistical
gate.

Multi-agent systems are the founding and primary use case — a coordinator
plus specialists, a deep sub-agent tree, a single LLM (Large Language
Model) agent, any shape — and the only shipped concrete adapter targets
Google ADK. The loop itself is not agent-specific: it needs an entrypoint
it can drive, one or more mutable source trees, and a board that scores
each run. When you are changing zicato's code, assume the target is
"some tree of files with an evaluation contract"; an agent tree is one
instance of that, not the definition.

The one-paragraph version, verbatim from the repo's own agent guide:

> zicato wraps an inner harness in an **evolve loop**: it proposes a
> small structured edit to the harness (for the primary agent use case:
> an agent instruction, a tool description, a planner template, a role
> scope — in general, any annotated mutation point), runs a scored
> **tournament** between the parent (champion) and the child (challenger)
> across a **board** of tasks, derives a scalar **loss** from runtime
> drift telemetry plus per-task pass/fail predicates, and promotes the
> child only when it beats the gate. Rounds group into **generations**;
> generations group into **epochs**; an epoch is defined by its
> **evaluation contract** (board + proposer brief + scoring +
> inner-harness identity + the proposer itself) and by a **goal**. Change
> the contract and the next `zicato evolve` auto-rolls a fresh epoch.
>
> — `AGENTS.md` §"What zicato is, in one paragraph"

### 1.1 The ecosystem: goldfive, harmonograf, zicato

zicato is the third member of an ecosystem. All three consume the same
typed event stream; they differ in *cadence* — how much history each one
reasons over.

| Layer | Owner | Cadence | What it does |
|---|---|---|---|
| Single-turn refine (replan in response to drift) | **goldfive** | within one run | Orchestration scaffolding: goals, plans, per-turn drift analysis, an intervention ladder. Emits the typed `goldfive.v1.Event` stream that names *what went wrong* in a run. |
| Operator-driven steering | **harmonograf** | within one run | The observability + HCI (Human–Computer Interaction) console: Gantt, graph, trajectory, intervention history. Renders the goldfive stream live; lets operators steer. |
| **Inner-harness rewrites across runs** | **zicato** | **across generations** | The meta-loop: aggregates drift into **loss patterns** across many runs, proposes structured edits to the inner harness, runs tournaments, promotes the patches that reduce loss. |

The cadence split is the load-bearing distinction. Goldfive handles "this
run wandered, replan *this run*"; zicato handles "this *kind* of run keeps
wandering the same way, rewrite the harness." Goldfive owns plans; zicato
owns the prompts and structure that *produce* the plans. Never blur this:
a change that makes zicato react within a single run belongs in goldfive,
not here.

Concretely, the coupling points are:

- **Telemetry in:** zicato captures goldfive's `goldfive.v1.Event` stream
  via goldfive's `JSONLPersistenceSink` — one `events.jsonl` per run
  (`src/zicato/telemetry/sink.py`). There is no zicato-specific event
  sink; the post-run reducer (`src/zicato/telemetry/reducer.py`) walks
  that JSONL and emits a `LossProfile` to `loss.json`.
- **Console out:** every `zicato evolve` auto-launches an in-process
  harmonograf server bound to a free localhost port
  (`src/zicato/telemetry/harmonograf_supervisor.py`, resolved through
  `_resolve_or_launch_harmonograf` in `zicato.evolve.lifecycle_services`),
  so per-run execution is watchable live. zicato's own meta-loop LLM calls
  (proposer, judges, analyzer) land on the same timeline through the
  meta-loop emitter (`_build_meta_loop_emitter_safe`).
- **Model-agnostic by construction:** zicato calls LLMs only through the
  narrow `CallLLM = Callable[[str, str, str], Awaitable[str]]` shape —
  `(system, user, model) -> response` — defined in
  `src/zicato/core/runtime.py`. No vendor SDK is imported by the library.
  zicato never inspects or switches on the `model` string.

### 1.2 The whole tool, for most operators

```sh
zicato init      # scaffold ./.zicato/ once
zicato evolve    # the single happy-path entry point to the loop
```

Everything else (`board`, `propose`, `tournament`, `epoch`, `reindex`,
`mutations`, `health`, `builder`, `dashboard`, …) is an advanced or debug
tool for driving one stage in isolation. `evolve` orchestrates the whole
loop: it is a thin CLI shell over `evolve_n_rounds`
(`src/zicato/evolve/loop.py`) which calls `evolve_once`
(`src/zicato/orchestrator.py`) up to N times. Chapter 02 walks one round
end to end.

### 1.3 Library first, three drivers on top

zicato is a **library first** — the surface declared in
`src/zicato/__init__.py` (lazy re-exports; importing `zicato` stays cheap
so `zicato --help` is fast). Three *drivers* sit on top and consume that
surface:

- `zicato.cli` — the `zicato` command line (Click; auto-discovered
  subcommands under `src/zicato/cli/commands/`).
- `zicato.dashboard` — the dashboard HTTP server (Starlette + SSE
  (Server-Sent Events)).
- `zicato.builder` — the tournament-builder GUI backend (deterministic,
  no LLM, no frontend).

Library packages never import the drivers; the only allowed driver→driver
edges are `cli → dashboard` and `dashboard → builder`. These are not
conventions — they are machine-enforced import-linter contracts in
`pyproject.toml` (`[tool.importlinter]`), run by `uv run lint-imports`.
See §3 (repo map) for the per-package rules and Golden Rule G5.

---

## 2. Vocabulary

Every term below is load-bearing: it appears in code, on disk, or in the
dashboard with exactly this meaning. For each term you get the
definition, where it lives in code, and the file that owns it. When two
terms name the same thing from different angles (champion/parent), that
is called out explicitly. Skim now; return constantly.

### 2.0 Quick reference: term → owning type → owning file

Use this table to jump; the subsections below carry the definitions.

| Term | Owning symbol | Owning file |
|---|---|---|
| workspace | `WorkspaceLayout` | `src/zicato/workspace/` |
| epoch | `EpochConfig` | `src/zicato/core/epoch.py` (type) / `src/zicato/epoch/lifecycle.py` (lifecycle) |
| contract / contract hash | `ContractInputs`, `compute_contract_hash` | `src/zicato/epoch/contract.py` |
| generation | `Generation` | `src/zicato/core/epoch.py` |
| generation source tree | `GenerationStore` | `src/zicato/epoch/genstore.py` (+ `git_genstore.py`, the default) |
| round (evolve round) | `Generation.round_index` / `Experiment.round_index` | `src/zicato/core/` |
| stage (inner round) | `RoundRecord.stage_index` | `src/zicato/selection/strategy.py` |
| run | `RunRecord` / `LossProfile` | `src/zicato/core/lineage.py`, `src/zicato/core/loss.py` |
| board / board entry | `BoardEntry` | `src/zicato/core/board.py` (type) / `src/zicato/board/jsonl.py` (I/O) |
| expectation / predicate / rubric | `Expectation`, `ExpectationKind` | `src/zicato/core/board.py` |
| judge | `JudgeSpec` | `src/zicato/core/board.py` (type) / `src/zicato/judge_runtime/` (bridge) |
| drift kinds / severities | `DriftKind`, `DriftSeverity`, `GOLDFIVE_DRIFT_KINDS` | `src/zicato/core/drift_kinds.py` — zicato's string mirror of goldfive's enums (goldfive is an optional extra) |
| proposer brief | `ProposerBrief` | `src/zicato/proposer/brief.py` |
| scoring | `ScoringWeights` | `src/zicato/core/scoring_config.py` |
| mutation point / patch | `MutationPoint`, `Patch` | `src/zicato/core/mutation.py` / `src/zicato/mutation/` |
| loss profile | `LossProfile` | `src/zicato/core/loss.py` (type) / `src/zicato/telemetry/reducer.py` (producer) |
| scalar | `aggregate_generation_score` | `src/zicato/tournament/scoring.py` |
| gate | `evaluate_gate`, `GateOutcome` | `src/zicato/tournament/gate.py` |
| pattern | `Pattern`, `detect_patterns` | `src/zicato/core/patterns.py`, `src/zicato/patterns/detectors.py` |
| failure profile | `_render_failure_profile` | `src/zicato/orchestrator.py` (+ `analyzer/outcome_marginals.py`) |
| process exemplars | `extract_process_exemplars` | `src/zicato/analyzer/process_exemplars.py` |
| noise floor | `NoiseFloor`, `CALIBRATION_REPLICATE_BASE` | `src/zicato/tournament/calibration.py` |
| preflight | `PreflightReport`, `PREFLIGHT_REPLICATE_BASE` | `src/zicato/epoch/preflight.py` |
| tournament (runner) | `run_tournament`, `run_matchup`, `TournamentResult` | `src/zicato/tournament/runner.py` |
| tournament structure | `TournamentStructure` | `src/zicato/core/tournament.py` |
| selection strategy / decision | `SelectionStrategy`, `SelectionDecision` | `src/zicato/selection/strategy.py` |
| resolve / drive a field | `resolve_tournament`, `EvidencePreGate` | `src/zicato/selection/driver.py` |
| evidence gate | `EVIDENCE_REPLICATE_BASE`, `rating_block` | `src/zicato/selection/evidence_gate.py` |
| holdout/train split | `split_board`, `rotation_seed` | `src/zicato/board/split.py` |
| facet slice (display only) | `FACET_TAG_PREFIX`, `facet_scores_for_generation` | `src/zicato/query/eval_view.py` |
| Ladder | `LadderConfig` / `holdout_record` | `src/zicato/core/scoring_config.py` / `src/zicato/tournament/ladder.py` |
| screen | `run_candidate_screen`, `SCREEN_REPLICATE_BASE` | `src/zicato/epoch/screen.py` |
| slate / best-of-N | `BestOfNProposerAgent`, `wrap_with_proposer_quality` | `src/zicato/proposer/best_of_n.py` |
| placebo | `placebo_round_due`, `PLACEBO_HYPOTHESIS_MARKER` | `src/zicato/evolve/placebo.py`, `src/zicato/core/experiment.py` |
| experiment / hypothesis / outcome | `Experiment`, `HypothesisSpec`, `OutcomeRecord` | `src/zicato/core/experiment.py` |
| experiment memory | `PriorExperiment`, `EXPERIMENT_MEMORY_MAX_ENTRIES` | `src/zicato/core/experiment.py` |
| journal | `append_journal_entry` | `src/zicato/epoch/journal.py` |
| lineage | `append_to_lineage`, `load_lineage` | `src/zicato/epoch/lineage.py` |
| RoundLog | `RoundLog`, `fold_round_record` | `src/zicato/epoch/round_log.py` |
| index | `rebuild_index` | `src/zicato/index/` |
| heartbeat / progress log | `HeartbeatBeater` / `progress_log` | `src/zicato/runtime/heartbeat.py`, `src/zicato/runtime/progress_log.py` |
| control protocol | `claim_gate_override`, `claim_skip_round`, … | `src/zicato/runtime/control_consumer.py` |
| crash resume | `prepare_resume`, `ResumePlan` | `src/zicato/runtime/resume.py` |
| deferral | `DEFERRED_INFRA_DECISION` | `src/zicato/orchestrator.py` |
| adapter | `HarnessAdapter` | `src/zicato/adapters/` |
| runtime knobs | `RuntimeConfig`, `RoundTokenLedger` | `src/zicato/core/runtime.py` |
| round outcome | `EvolveRoundOutcome` | `src/zicato/orchestrator.py` |
| worker boundary | `_callable_dotted_path`, `_weights_spec` | `src/zicato/tournament/worker_transport.py` |
| record-format guard | `RECORD_FORMAT_VERSION`, `check_record_format` | `src/zicato/epoch/_storage.py` |

### 2.1 The container hierarchy: workspace → epoch → round → generation → run

**workspace** — the `.zicato/` directory that holds everything zicato
persists for one target: `config.json` (the registration record —
adapter, entrypoint, mutable trees, model roles), `current_epoch` marker,
`lineage.json`, `index.db`, `epochs/`, `runtime/`, `repo/` (the git
generation store). Path math is owned by `WorkspaceLayout` in
`src/zicato/workspace/` (typed canonical reads, the single
epoch-enumeration authority) and `src/zicato/core/workspace.py` (the
older per-generation path helpers). The filesystem is canonical; the
index is derived (see G-rules).

**epoch** — a sealed **evaluation contract** plus a **goal**; houses many
generations. Created by `new_epoch`, closed by `close_epoch` /
`close_epoch_async` (`src/zicato/epoch/lifecycle.py`). The frozen record
is `EpochConfig` (`src/zicato/core/epoch.py`), persisted as
`epochs/{id}/config.json` next to the frozen `board.jsonl`, `brief.md`,
`scoring.json`. The epoch's identity is its `contract_hash`
(`src/zicato/epoch/contract.py`); change any contract input and the next
`evolve` auto-rolls a fresh epoch (`ensure_epoch_for_contract` in
`src/zicato/evolve/epoching.py`). Chapter 03 is entirely about this.

**generation** (`v0`, `v1`, …) — one candidate snapshot of the inner
harness's source tree; houses many board runs. Typed as `Generation`
(`src/zicato/core/epoch.py`): id, epoch, parent, `snapshot_root`,
`promoted` flag, and `round_index` (the evolve round that *minted* it —
its birth round; a champion carried into later rounds keeps its birth
round, it is never re-stamped). The source trees themselves live behind
the `GenerationStore` seam (`src/zicato/epoch/genstore.py`) with the git
backend (`git_genstore.py`) as the shipped default: a generation is a
commit/tag in the workspace-private `repo/`.

**round** — one propose → apply → tournament → promote/reject cycle. Two
distinct axes both used to be called "round"; the overload was killed:

- The **evolve round** (`round_index` on `Generation` / `Experiment`) is
  the outer, epoch-cumulative axis. Zero-based. Re-running `evolve` on an
  existing epoch CONTINUES its numbering — `_epoch_round_base` in
  `src/zicato/evolve/loop.py` computes `max(persisted round_index) + 1`.
  > ⚠️ **TRAP** — before this fix, a re-run restarted at 0 and the
  > dashboard collided the new field into the old bucket ("v9 lands in
  > Round 0 next to v1–v4"). If you ever mint a generation, thread the
  > epoch-cumulative round index, never the invocation-local loop counter.
- The **stage index** (`stage_index` on the selection layer's
  `RoundRecord`, `src/zicato/selection/strategy.py`) is the
  WITHIN-tournament axis: a bracket round, Swiss round, or racing rung
  inside one evolve round. The persisted JSON key is `stage_index`;
  readers still accept the legacy `round_index` key for old workspaces.

The unqualified word "round" in this guide always means the evolve round.

**run** — one board entry executed against one generation; emits
`events.jsonl` (goldfive telemetry) + `loss.json` (the reduced
`LossProfile`). Run records live under
`epochs/{epoch}/generations/{gen}/runs/…`; path math in
`src/zicato/core/workspace.py` (`loss_profile_path` and friends).

**board unit** — the scheduling unit of a tournament: one board entry
evaluated for one duel, typically as a champion run + challenger run
pair. `RuntimeConfig.parallelism` bounds how many board units are in
flight (`src/zicato/core/runtime.py`) — in full mode each unit runs its
two sides concurrently, so size the LLM endpoint against
`2 × parallelism`.

### 2.2 The contract side

**contract / evaluation contract** — the six components whose canonical
hash IS the epoch's identity: (1) the board, (2) the proposer brief,
(3) the scoring config, (4) the registered inner-harness entrypoint,
(5) the registered mutable-tree paths, (6) the proposer (agent identity +
tools + skill bodies). Owned by `src/zicato/epoch/contract.py`
(`ContractInputs`, `compute_contract_hash`, `compute_component_hashes`).
The inner harness's *source content* is deliberately NOT part of the
contract — that is exactly what zicato mutates within an epoch.

**board** — the frozen set of evaluation tasks for an epoch, a JSONL
file (one JSON object per line). Loaded by
`src/zicato/board/jsonl.py` (`load_board`, `load_board_with_meta`);
authored through the typed API in `src/zicato/board/` (`Predicate`,
`Rubric`, `Judge` builders). The board may carry a board-level metadata
line (no `id` field) with `judges`, `disable_drift`, `judge_only` — all
folded into the contract hash (`_canon_board_meta` in
`src/zicato/epoch/contract.py`).

**board entry** — one task on the board: `BoardEntry`
(`src/zicato/core/board.py`), a discriminated union over three kinds
(single-turn, multi-turn scripted, multi-turn emulated), each carrying an
optional typed **expectation** and per-entry **judges**, plus
`wall_clock_budget_seconds` (the per-run hard budget) and free-form
`context`.

**expectation** — the OUTCOME check graded after a run: `Expectation`
(`src/zicato/core/board.py`) with `ExpectationKind` in
`{text, regex, json_schema, predicate, rubric}`. A **predicate** is a
deterministic matcher (dotted-spec Python function; its *source* is
hashed into the contract — see 03-contract-and-epochs.md §"source-hash
folding"). A **rubric** is LLM-as-judge grading of the outcome.

**judge** — the PROCESS check observed while a run is in flight:
`JudgeSpec` (`src/zicato/core/board.py`) — a name, a mode (`"inline"`
natural-language criterion, or `"python"` dotted spec), a severity.
`src/zicato/judge_runtime/` bridges declarative specs into live goldfive
judges. A judge's findings emit under the single `"custom"` drift kind,
weighted per judge name via `ScoringWeights.per_judge_weights`.

**drift** — goldfive's taxonomy of "what went wrong in-flight":
typed drift kinds (`src/zicato/core/drift_kinds.py` registers the valid
strings) at severities info/warning/critical. Drift events are the raw
material of zicato's loss.

**proposer brief** — the operator's brief *to the proposer* (`brief.md`):
the goal, constraints, and a `## Forbidden` section listing mutation ids
the proposer must not touch. Parsed by `src/zicato/proposer/brief.py`;
normalized (whitespace/line-ending-insensitive) into the contract hash by
`_canon_brief`. Historical name: "epoch rubric" / `rubric.md` — legacy
keys and file names are still accepted on read
(`resolve_contract_inputs`, `_default_brief_path`).

**scoring / `ScoringWeights`** — the frozen weight set that turns loss
into a scalar and gates promotion: `src/zicato/core/scoring_config.py`.
Nested config blocks ride on it and therefore fold into the contract hash
automatically (the canonicalizer recurses into nested dataclasses):
`TournamentStructure`, `OverfittingConfig` (+ `LadderConfig`),
`ProposerQualityConfig`, `ExperimentMemoryConfig`.

**mutation point** — a span, a bracketed region, or a whole file the
proposer may edit, marked in the target's source with a
`zicato:mutable id="..."` comment. The marker may sit in a `.py` file
(where a span binds to the string literal beneath it) or in any
allowlisted text file — markdown, YAML, TOML, shell — where the `:code`
region and `:file` forms apply and any conventional comment leader is
accepted. Typed as `MutationPoint`; enumerated by `enumerate_mutations`
(`src/zicato/mutation/enumerator.py`); audited by `zicato inspect mutations`.
The set of *registered mutable trees* (which subtrees are mutable at all)
is contract identity; the *content* of those trees is not.

**patch** — one concrete edit the proposer wants applied:
`Patch` (`src/zicato/core/mutation.py`), addressed to a `mutation_id`.
Applied by the mutation applier into a fresh child snapshot; validated by
`validate_post_apply` (`src/zicato/mutation/validator.py`) — syntax still
parses, every id still resolves, markers preserved.

### 2.3 The measurement side

**loss profile** — the reducer's per-run output, `LossProfile`
(`src/zicato/core/loss.py`): drift counts per (kind, severity), plan
revisions, task-failure ratio, runtime, expectation result, `pass_fail`,
the continuous `score`, namespaced `metric_counts` (drift:/cost:/latency:/
rubric:/output:/schema:), `tokens_spent`, `abort_cause`. Scoring and
pattern detection consume `LossProfile`s only — they never re-read raw
events, which lets event schemas evolve upstream without touching
scoring.

**scalar** — the per-generation lower-is-better number the gate compares:
weighted drift loss + `pass_weight × (1 − mean_score)` + namespace terms,
aggregated by `aggregate_generation_score`
(`src/zicato/tournament/scoring.py`) under the epoch's `ScoringWeights`,
with pluggable seams in `src/zicato/scoring/` (declarative transforms +
dotted-spec plugins). The exact arithmetic is chapter 04's subject.

**gate** — the promote decision for one duel: `evaluate_gate`
(`src/zicato/tournament/gate.py`) returns a `GateOutcome`. Three rules in
order: scalar margin (`promote_margin`), pass-rate monotonicity
(per-entry or aggregate scope), per-namespace monotonicity. The gate is
reused verbatim by every tournament structure — strategies never
re-decide a duel.

**pattern** — a cross-run statistical finding over a window of loss
profiles ("this drift kind dominates", "this entry disproportionately
fails"): `Pattern` (`src/zicato/core/patterns.py`), emitted by the
detectors in `src/zicato/patterns/detectors.py`. Patterns flow
proposer-ward only and never carry executable code.

**failure profile** — the board-anonymous, bucketed outcome-marginal
block rendered into the proposer prompt (`_render_failure_profile` in
`src/zicato/orchestrator.py`, backed by
`src/zicato/analyzer/outcome_marginals.py`). Counts and bands only —
never an entry id.

**process exemplars** — the opt-in, mechanically-redacted event windows
(±3 events around an anchor drift) extracted from the champion's
train-slice `events.jsonl` so the proposer sees HOW a failure unfolds,
never WHICH entry it unfolded on. Knob:
`proposer_quality.process_exemplars` (default 0 = off). Extractor:
`src/zicato/analyzer/process_exemplars.py`; renderer:
`render_process_exemplars` (`src/zicato/proposer/prompts.py`);
orchestrator seam: `_render_process_exemplars_block`. Design doc:
`docs/design/PROCESS-EXEMPLARS.md`.

**noise floor** — the measured A/A spread: duel the champion against
*itself* K times and record the scalar spread
(`src/zicato/tournament/calibration.py`, `NoiseFloor`). A RUNTIME
measurement persisted onto `EpochConfig.noise_floor` — never hashed,
never rolls the epoch. A `promote_margin` inside the floor cannot
distinguish a real improvement from a re-roll
(`_warn_margin_below_noise_floor`).

**preflight** — the opt-in contract pre-flight: measure the A/A floor AND
the degradation signal (champion vs a deliberately-degraded copy) and
record an `ok`/`warn`/`refuse` verdict (`src/zicato/epoch/preflight.py`).
Recommend-only; persisted onto `EpochConfig.preflight`; never hashed.

### 2.4 The competition side

**champion / challenger** vs **parent / child** — the SAME pair, named by
role vs by lineage. Use champion/challenger for tournament framing
(who defends, who challenges); parent/child for lineage framing (who was
forked from whom). This is a repo-wide terminology standard; mixing the
frames in one sentence is a review comment waiting to happen.

**tournament** — the scored comparison that decides promotion. The
runner is `src/zicato/tournament/runner.py`: `run_tournament` (the full
A/B gauntlet duel), `run_fast_mode` (challenger vs the champion's cached
aggregate), `run_matchup` (one duel of a multi-challenger structure),
`confirm_crowning_holdout` (the holdout confirmation of a winning duel).
Result type: `TournamentResult`.

**tournament structure / `SelectionStrategy`** — the per-epoch shape of
competition: gauntlet (default), single_elim, double_elim, swiss, racing.
Configured as `ScoringWeights.tournament_structure`
(`TournamentStructure`, `src/zicato/core/tournament.py`) so it folds into
the contract hash. The strategy abstraction lives in
`src/zicato/selection/` (`SelectionStrategy`, `resolve_tournament`,
`make_strategy`); it owns scheduling, bracket bookkeeping, and
intra-tournament stopping — the gate stays the per-duel acceptance test.

**field** — the N challengers a non-gauntlet structure proposes per round
(`field_size` param; the gauntlet is field size 1). Minted by
`_evolve_multi_challenger` in `src/zicato/orchestrator.py`.

**crowning** — the final champion-gate duel of a resolved structure, and
the resulting decision. `SelectionDecision`
(`src/zicato/selection/strategy.py`) carries
`promoted_generation_id`, `crowning_matchup_id`, the full matchup audit,
and standings. The **crowning invariant**: the settled bracket and the
champion pointer must agree — checked loudly in
`_evolve_multi_challenger` before any lineage write.

**replicate** — one repeated evaluation of the same (generation, entry)
under a distinct cache slot, used to average out noise. The per-duel
`replicates` knob (structure param; default 2 for gauntlet/elim/swiss,
1 for racing) averages paired runs before the gate, on EVERY runner
entry point — including `run_fast_mode`, where only the challenger side
is drawn again (ch.04 §7.4). Replicate indices form a **reserved
ledger** — see Golden Rule G7.

**evidence gate / pre-gate** — the opt-in Bradley–Terry confirmation of a
crowning promote (`promote_confidence_threshold` structure param):
defer → replicate → promote/inconclusive, with confidence intervals that
must separate. `src/zicato/selection/evidence_gate.py` +
`EvidencePreGate` / driver in `src/zicato/selection/driver.py`;
inconclusive terminals land in the dead-letter queue
(`src/zicato/selection/dead_letter.py`). Soundness device, not a power
device — see the measured ~37-consecutive-wins fact in
`tests/test_decision_procedure_power.py` and 06-tournament-and-selection.md.

**facet** — a named diagnostic slice of the board, declared by tagging
entries `facet:{name}` (the second reserved tag after `holdout`).
`query.eval_view.facet_scores_for_generation` re-aggregates a candidate
over each slice at the epoch's frozen weights, so a facet's `scalar` is
comparable to the candidate's own. DISPLAY ONLY — nothing is persisted
and no decision reads it (EVAL-VIEW.md §3.4, BOARD-FORMAT.md §1.4).

**holdout / train split** — the anti-overfitting board partition
(`split_board`, `rotation_seed` in `src/zicato/board/split.py`).
Selection happens on the TRAIN slice only; the holdout is
confirmation-only, mediated by the **Ladder** (Blum & Hardt-style
release rule + per-epoch query budget, `src/zicato/tournament/ladder.py`,
configured by `LadderConfig`). A board too small to split degrades to
"train = full board", byte-identical to pre-split behaviour.

**screen (candidate screening / tryouts)** — the opt-in pre-tournament
veto: each best-of-N slate candidate runs a small rotating train panel
(`proposer_quality.screen_entries`) and a confirmed catastrophic
regression (pass-flip on a champion-passing entry, or a budget abort) is
vetoed before selection. Veto-first: it disqualifies, never ranks.
`src/zicato/epoch/screen.py` (`run_candidate_screen`,
`select_screen_entries`, `SCREEN_REPLICATE_BASE = 3000`).

**slate / candidate (best-of-N)** — the `best_of_n` sampled candidate
experiments per propose-step (default 3), each slot steered by a distinct
edit-class hint, with a self-critique pass selecting the winner.
`src/zicato/proposer/best_of_n.py` (`BestOfNProposerAgent`,
`wrap_with_proposer_quality`); hints in `src/zicato/proposer/hints.py`.

**placebo** — the opt-in random-baseline challenger
(`overfitting.random_baseline_every_n`): every Nth round, one extra
challenger whose patch is a semantics-preserving no-op. The gate MUST
reject it; a promoted placebo raises the CRITICAL `placebo_promoted`
health finding (gate discrimination is broken). Minted by
`src/zicato/evolve/placebo.py` + `_mint_placebo_challenger`
(`src/zicato/orchestrator.py`); the marker constant
`PLACEBO_HYPOTHESIS_MARKER` lives in `src/zicato/core/experiment.py`.

**fast mode / `champion_eval_mode`** — `--mode fast` reuses the
champion's cached per-board scalars instead of re-running the immutable
champion. It is the champion side that is skipped, NOT replication: the
challenger board still runs `replicates` times and folds. The resolved
provenance (`"full"` / `"fast"` / `"fast-degraded"`) is journaled on the
`OutcomeRecord` — it is RUNTIME provenance, never a contract input;
flipping fast↔full does not roll the epoch.

### 2.5 The record side

**experiment** — the journaling unit: `Experiment`
(`src/zicato/core/experiment.py`) = a mandatory **hypothesis** written
BEFORE the run + the **patches** + the **outcome** written after.
Persisted as `epochs/{epoch}/generations/{gen}/experiment.json` plus one
`patches/{id}.json` per patch (`src/zicato/epoch/journal.py`:
`write_experiment`, `update_experiment_outcome`, `read_experiment`).

**hypothesis** — `HypothesisSpec` (`src/zicato/core/experiment.py`):
`core_idea` (one sentence), `modulating` (the mutation ids the patches
must address — the applier verifies), `why`, expected drift/metric
movements, `expected_pass_rate_delta`, `risks`. Mandatory and structured;
schema-invalid proposer output is rejected and retried.

> ⛔ **NEVER** backfill a hypothesis to match a result. The hypothesis is
> written before the run and the outcome after — that pairing is the
> journal's core epistemic unit. Backfilling silently converts the
> journal from an experiment log into a rationalization log.

**outcome / `OutcomeRecord`** — the post-run record joined onto the
experiment (`src/zicato/core/experiment.py`): deltas, the decision, the
rejection reason, plus the additive runtime-evidence fields (holdout
block, train/holdout loss + generalization gap, operator-override flags,
evidence-gate resolution, structure/rank/match record,
`champion_eval_mode`). Every additive field defaults so old journals
deserialize unchanged.

**experiment memory** — the curated "## What's already been tried" digest
the proposer sees: `PriorExperiment` entries (settled history via the
index + in-flight siblings in a field round), capped at
`EXPERIMENT_MEMORY_MAX_ENTRIES = 12`, banded under restricted visibility.
Assembled by `_load_prior_experiments` (`src/zicato/orchestrator.py`);
opt-in cross-epoch transfer via `ExperimentMemoryConfig.cross_epoch`
(same contract hash only). Design: `docs/design/EXPERIMENT-MEMORY.md`.

**journal** — the append-only human narrative per epoch
(`epochs/{epoch}/journal.md`), one `## vN — <core idea>` section per
experiment (`append_journal_entry`, `src/zicato/epoch/journal.py`).

**lineage** — the cross-epoch DAG in one atomic `lineage.json`
(`src/zicato/epoch/lineage.py`). Generations carry a **promoted
tri-state**: `true` (promoted), `false` (rejected dead branch), `null`
(pending — applied but still racing). See 03-contract-and-epochs.md
§"lineage.json semantics".

**RoundLog** — the per-round durable event log at
`epochs/{epoch}/rounds/{round}/round_log.jsonl`
(`src/zicato/epoch/round_log.py`): a typed, sequenced, append-only,
torn-tail-tolerant JSONL of the round's full arc (open → proposal →
apply/validate → units → gate/holdout/evidence → decision → close), plus
`fold_round_record` reducing it to a `RoundRecord`. The canonical event
sequence is pinned by the convergence oracle and reproduced in
02-architecture.md §"the canonical event sequence".

**index** — the derived SQLite analytical index `.zicato/index.db`
(`src/zicato/index/`): schema + ingest + query. Rebuildable at any time
by `zicato repair index`; dual-written live (best-effort) as rounds run. Files
are canonical; the index is a projection.

**heartbeat / progress log** — the liveness surface under
`.zicato/runtime/`: `HeartbeatBeater` writes `heartbeat.json` every ~2s
(`src/zicato/runtime/heartbeat.py`); the orchestrator progress event log
(`src/zicato/runtime/progress_log.py`) advances a monotonic `seq` only on
GENUINE transitions (LOOP_START, ROUND_START, PROPOSE, TOURNAMENT_START,
TOURNAMENT_SETTLE, PROMOTE/REJECT, SETTLED/STOPPED) — never on the timer
— so a reader can tell live-and-working from wedged.

**deferral (`deferred_infra`)** — the endpoint-outage circuit's verdict
(`DEFERRED_INFRA_DECISION` in `src/zicato/orchestrator.py`): when a
round's INFRA-aborted run count reaches
`RuntimeConfig.infra_abort_round_threshold`, the round defers instead of
burning the experiment — nothing is journaled, the experiment persists
un-outcomed (the exact shape crash-resume reconciles), and the loop backs
off exponentially. Distinct from `"rejected"` on purpose: it must not
count toward the consecutive-rejection breaker.

**adapter** — the pluggable "how do we run one generation against one
board entry" seam: `HarnessAdapter` protocol (`src/zicato/adapters/`),
with the ADK (Agent Development Kit — Google's agent framework) adapter
as the reference implementation and a generic `kind: "import"` factory
block in `config.json` for anything else.

**RuntimeConfig** — the runtime-side binding (`src/zicato/core/runtime.py`):
the two LLM callables, parallelism, worker-env scrubbing, diversity
tolerance, infra circuit knobs, per-round token budget. RUNTIME knobs
never fold into the contract hash and never roll epochs — the mirror
image of contract knobs. The decision procedure for "which kind is my new
knob?" is the closing recipe of 03-contract-and-epochs.md.

**EvolveRoundOutcome** — one round's summary returned by `evolve_once`
(`src/zicato/orchestrator.py`): parent/child ids, the decision, the
rejection reason, both scalars and the delta, plus the health summary.

### 2.6 The workspace on disk

You will spend a lot of time reading `.zicato/` trees. The canonical
shape (assembled from `src/zicato/epoch/lifecycle.py`'s module docstring,
`src/zicato/epoch/round_log.py`, and the workspace path helpers):

```
{project}/
  board.jsonl                    # the operator's LIVE contract files sit
  brief.md                       #   NEXT TO .zicato/, in the project root —
  scoring.json                   #   never inside it (resolve_contract_inputs)
  .zicato/                       # the workspace root
    config.json                  # registration: adapter, entrypoint,
                                 #   mutable_trees, contract paths, models
    current_epoch                # marker file, single line = epoch id
    lineage.json                 # the cross-epoch DAG (atomic rewrites)
    index.db                     # DERIVED SQLite index (rebuildable)
    repo/                        # the git generation store (default backend)
    runtime/                     # heartbeat.json, active runs/tournament,
                                 #   progress log, control files (EPHEMERAL)
    epochs/
      {epoch_id}/
        board.jsonl              # frozen board
        brief.md                 # frozen proposer brief
        scoring.json             # frozen serialized ScoringWeights
        config.json              # EpochConfig (hash, goal, noise_floor, …)
        contract_components.json # per-component sub-hashes (roll diagnosis)
        current_generation       # the promoted-head marker
        journal.md               # appended per experiment
        analysis.md / .html      # the (re)generated epoch report
        health/round_{N}.json    # per-round loop-health reports
        insights/round_{N}.md    # analyzer output the proposer reads back
        rounds/{N}/round_log.jsonl   # the durable per-round event log
        generations/
          {vN}/
            experiment.json      # hypothesis + patches + outcome
            patches/{id}.json    # one record per patch
            gen_score.json       # cached aggregate (fast-mode reuse)
            runs/... events.jsonl + loss.json per board entry
```

Two orientation rules fall straight out of this tree. First, the
operator's live contract files sit in the project root NEXT TO
`.zicato/`, while every epoch holds its own frozen copies — editing a
frozen copy is archaeology vandalism; editing a live copy is a contract
change that rolls the epoch (both wrong unless intended). Second,
`runtime/` is ephemeral (cleared by resume/crash cleanup, overwritten
per round) while `epochs/` is the store of record — never put anything
you need to survive the run under `runtime/`.

---

## 3. The repo map

Layout: `src/zicato/` (the Python package, src-layout),
`crates/supervisor/` (the Rust watchdog), `examples/` (a separate
`zicato-examples` distribution, uv workspace member), `skills/`
(agent-driven operating workflows), `tools/parity/` (the
behavior-preserving refactor oracle), `docs/design/` (the design corpus),
`tests/` (2800+ tests).

Import rules below reflect the machine-enforced contracts in
`pyproject.toml [tool.importlinter]`:

```toml
[[tool.importlinter.contracts]]
name = "the library must not import the drivers (cli / dashboard / builder)"
type = "forbidden"
```
*(pyproject.toml, `[tool.importlinter]` — run `uv run lint-imports`)*

Unless stated otherwise, "must never import" below means "forbidden by
those contracts"; every library package is forbidden from importing
`zicato.cli`, `zicato.dashboard`, `zicato.builder`.

### 3.1 The library packages (`src/zicato/…`)

**`core/`** — the foundational frozen dataclasses every other module
imports: board types, loss types, experiment/hypothesis/outcome, epoch/
generation, scoring config, tournament types, mutation types, proposer
spec, runtime config, plus workspace path math and the drift-kind
registry. Public face: `from zicato.core import X` (re-exported through
`core/types.py`, whose namespace also anchors the contract-serde
annotation resolver — see 03-contract-and-epochs.md). Imports nothing
domain-heavy; everything imports it. Never put behaviour here beyond
validation — core is types.

**`orchestrator.py`** (module, not a package) — the integration point:
`evolve_once`, `_evolve_multi_challenger`, and the round-tail helpers.
Heavier siblings are imported lazily inside function bodies to keep
`zicato --help` fast. The test suite monkeypatches names on this module
object (`orch.evolve_once`, `orch.ensure_epoch_for_contract`, …) — which
is why collaborators are resolved through the module object at call time
(late binding). Chapter 02 is the walkthrough.

**`evolve/`** — evolve-loop internals split out of the orchestrator:
`loop.py` (`evolve_n_rounds` + the `StopPolicy` circuit breakers),
`epoching.py` (contract-hash auto-epoching), `round.py` (the shared
propose-time seams `build_post_apply_validator` /
`check_patch_manifest_and_forbidden`), `lifecycle_services.py`
(heartbeat/harmonograf/meta-loop plumbing), `placebo.py`,
`containment.py` (diff containment mirroring the supervisor's Rust
check), `dashboard_projection.py` (the ActiveTournament envelope +
durable field-tournament records). Everything here is re-exported from
`zicato.orchestrator` where callers expect it.

**`epoch/`** — the epoch domain: `lifecycle.py` (new/close/list/switch/
load + the frozen-contract writes), `contract.py` (the hash),
`contract_serde.py` (field-enumerating serializer), `journal.py`,
`lineage.py`, `analysis.py` + `html_report.py` (the at-close and
progressive reports), `round_log.py`, `screen.py`, `preflight.py`,
`genstore.py` + `git_genstore.py` (the generation-tree seam), `gc.py`,
`_storage.py` (record-format guard + storage keys). Owns the
`format_version` discipline (`RECORD_FORMAT_VERSION`).

**`tournament/`** — scoring aggregation (`scoring.py`), the gate
(`gate.py`), the runner (`runner.py` — `run_tournament`, `run_fast_mode`,
`run_matchup`, `confirm_crowning_holdout`, and the documented test-suite
monkeypatch anchor `_run_single`), the worker transport
(`worker_transport.py` — wire specs, ephemeral checkouts, the
module-level-callable rule), the unit cache, the Ladder (`ladder.py`),
A/A calibration (`calibration.py`), and tournament-detail analytics
(`detail.py`).

**`selection/`** — the structure layer: `strategy.py` (`SelectionStrategy`,
`Contestant`/`Matchup`/`MatchupResult`/`SelectionDecision`/`Standing`),
`registry.py` (`make_strategy`), the concrete strategies, `driver.py`
(`resolve_tournament` + `EvidencePreGate` orchestration),
`evidence_gate.py` (Bradley–Terry fit + `EVIDENCE_REPLICATE_BASE`),
`dead_letter.py`, `diversity.py` (`jaccard`). The gate is imported from
`tournament/`; strategies never re-decide a duel.

**`proposer/`** — the LLM-driven half: `proposer.py`
(`propose_experiment`, `ProposerError`), `agent.py` (`ProposerAgent`,
`ProposerContext`, `build_proposer_agent`), `best_of_n.py` (slate +
critique + screen + revise wrapper), `prompts.py` (the restricted-
visibility render boundary), `skills.py` (proposer-dir resolution +
skill hashing), `hints.py`, `brief.py`, `tools.py` / `adk_agent.py`
(the tool-using ADK-native proposer). The proposer is a first-class
contract input — see `docs/design/PROPOSER.md`.

**`mutation/`** — the annotation-driven mutation surface: `markers.py`
(the marker grammar, in its `"python"` and `"text"` comment syntaxes),
enumerator (one walk over every marker-carrying file, with Python
specialized by an AST context that supplies the docstring-line set and
the span resolver), applier (patches → child tree), validator (post-apply
checks). The `zicato inspect mutations` CLI audits it.

**`board/`** — the typed board-authoring API (Predicate/Rubric/Judge
builders), the JSONL loader/saver, and `split.py` (the train/holdout
split + rotation seed).

**`patterns/`** — the cross-run detectors (`detect_patterns`,
`ALL_DETECTORS`, `DetectorInput`). Small, statistical, explainable; one
`Pattern` per finding.

**`telemetry/`** — sink wiring (goldfive `JSONLPersistenceSink` per run),
the post-run reducer (`events.jsonl` → `LossProfile`), and the
harmonograf supervisor (auto-launch + handle).

**`scoring/`** — the pluggable scoring seams: Seam 1 (per-run drift
reduction, runs INSIDE the killable worker) and Seam 2 (per-generation
scalar synthesis, runs in the orchestrator), declarative transforms
(`transforms.py`), dotted-spec plugins (`plugins.py`,
`spec_with_source_hash` — the ONE source-hashing mechanism the contract
reuses), `diff_complexity.py`, `builtins.py`.

**`emulator/`** — the collusion-proof multi-turn user emulator: sealed
context construction, the two-callable identity rule, the answer-leak
heuristic, the `zicato:emulator` audit lane.

**`judge_runtime/`** — turns declarative `JudgeSpec`s into live goldfive
judges (inline → LLM-as-judge on the auxiliary callable; python → dotted
import).

**`runtime/`** — the `.zicato/runtime/` state layer: paths, typed state
dataclasses (heartbeat, active runs, `ActiveTournament`,
`TournamentPhase`), `heartbeat.py`, `progress_log.py`, `lock.py`
(the workspace single-writer lock), `resume.py` (conservative
crash-resume), `control_consumer.py` (the operator control-file protocol:
pause, skip_round, gate overrides, rubric replacement), `channel.py`
(the append-only EventLog discipline). The orchestrator writes here; the
Rust supervisor and dashboard read here.

**`storage/`** — the record-level `StorageBackend` seam (file + memory
backends; atomic writes). Files remain the canonical store; the seam
makes the mechanism swappable. Its private `_atomic` module is
package-internal (enforced by a ruff banned-api rule) — everyone else
goes through the public `zicato.storage` face.

**`index/`** — the SQLite analytical index: `schema.py` (DDL +
schema_version), `ingest.py` (rebuild + incremental), `query.py`,
`elo.py`. Derived, never canonical.

**`health/`** — loop-health diagnostics (`assess_loop_health`,
`LoopHealth`, the detectors incl. `detect_placebo_promoted` and the
generalization-gap detector) + the `zicato health` CLI backend. Imported
lazily and best-effort by the orchestrator.

**`analyzer/`** — decision-telemetry insights (`insights/round_{N}.md`
read back by the next round's proposer), the epoch analysis report
(`analysis.md`/`analysis.html`), outcome marginals, and the
process-exemplar extractor.

**`query/`** — the read-only workspace query layer (the former dashboard
state_reader, split per view). Library code: it must never import
`zicato.dashboard` (a dedicated import-linter contract pins exactly
this). Every function is best-effort over possibly-torn files.

**`workspace/`** (package) and **`workspace_loader.py`** — the typed
canonical-read layer (`WorkspaceLayout`, `iter_epochs`/`list_epoch_ids` —
the single epoch-ordering authority, `read_experiments`) and the
config/board/scoring/brief loaders (`load_workspace_config`,
`load_current_board_with_meta`, `scoring_weights_from_dict`).

**`adapters/`, `adapter_factory.py`, `runtime_factory.py`,
`models_config.py`, `config.py`, `import_path.py`, `aux_timeout.py`** —
adapter construction from `config.json`, `RuntimeConfig` construction
(named model engines, inherited roles, `build_adk_model`), dotted-import
resolution, the auxiliary call timeout wrapper. The common configuration is
two engines: `target` supplies an optional target LLM to a model-capable
adapter, while `evaluation` serves internal work. The target itself is
adapter-defined and may consume no model. `judge`, `user_emulator`, `builder`,
and `proposer` inherit evaluation;
`proposer_generate` / `proposer_review` may override the base proposer. See
`docs/design/MODEL-CONFIG.md` for the schema and noun definitions.

**`synthetic/`** — synthetic adversarial/clean board-entry support for
dogfood target 2 (steering goldfive itself, where drift-count as loss
would be circular).

**`testing/`** — deterministic `CallLLM` doubles, replay helpers, and
fixture factories for every core dataclass. Import in tests only.

**`util/`** — dependency-free cross-cutting helpers, most importantly
`best_effort` (the log-and-swallow context manager the round's
non-critical writes all use) and `iso_time`.

**`_tournament_worker.py`** — the `python -m zicato._tournament_worker`
subprocess entry point (the L3 isolation layer). Everything it needs
crosses the wire as JSON + dotted import paths — see Golden Rule G9.

### 3.2 The drivers

**`cli/`** — Click root + auto-discovered subcommands
(`cli/commands/*.py`, one file per command; a broken plugin module logs
and is skipped). The CLI is the contract: trust `zicato <cmd> --help`
over any design doc. `docs/design/CLI.md` is a GENERATED artifact —
regenerate it from `--help` on CLI changes. May import the dashboard
(launch + static resolution); must not import the builder directly
(reaches it only transitively through `dashboard.server`'s mount —
`allow_indirect_imports = true` on that contract).

**`dashboard/`** — the Starlette server, endpoints, SSE broker
(`sse.py` — coalesced `state_change` frames), transcript reconstructor,
and the static JS bundle (`static/js/…`) with its Node behaviour suite
(`static/test/`, run by `make node-test`). Must never import the CLI.
Render discipline is Golden Rule G10.

**`builder/`** — the deterministic tournament-builder backend:
`config.py` (builder.json), `draft.py` (`TournamentDraft`/`DraftStore`),
`operations.py` (the one place each contract edit is implemented —
`set_structure`, `set_gate`, `set_screening`, …, plus `estimate_cost`,
`validate`, `preflight`, `apply`), `api.py` (REST routes the dashboard
mounts), `copilot_tools.py` (the chat copilot's tool surface). Must not
import cli or dashboard.

### 3.3 Outside `src/`

**`crates/supervisor/`** — the Rust watchdog + status server
(`zicato-supervisor`, default port 7920, bind 127.0.0.1). Reads the
runtime state files (heartbeat, active runs) as its ONLY coupling to the
Python side; kills wedged or over-deadline worker pids
(SIGTERM→grace→SIGKILL, `watchdog.rs`, `signal.rs`); runs the alarm-only
integrity notary scans (`diff_containment.rs`, `promotion_gate.rs`).
Built by `make supervisor`; bundled into the wheel by `hatch_build.py`.
It is a separate OS process on purpose: it survives a wedged Python event
loop. Summary in 02-architecture.md §"where the supervisor sits"; deep
dive in 08-supervisor.md.

**`examples/`** — the `zicato-examples` distribution (uv workspace
member; installed editable by `uv sync --all-extras`; never shipped in
the zicato wheel). The dogfood targets:
`target_0_convergence` (the deterministic planted-defect target — the
sanctioned no-LLM e2e vehicle, see G3 and its `RUN.md`),
`target_1_presentation`, `target_2_goldfive_steering`, plus
`proposer_with_tools`.

**`skills/`** — one directory per operator skill, each a `SKILL.md`
encoding the right command sequence + guardrails (catalog in
`skills/README.md`; `zicato-manage-epochs-and-rounds` is the designated
"read this first"; `zicato-dev-guide` is the contributor-facing map).
These are for agents *operating* zicato; this guide is for agents
*extending* it.

**`tools/parity/` + `tools/parity.sh`** — the behavior-preserving
refactor oracle: six gates (PYTEST, CONTRACT-HASH, CLI-HELP,
REINDEX-DUMP, MOCK-GOLDEN, MYPY) diffing fresh artifacts against
committed goldens. Golden Rule G5.

**`docs/design/`** — the design corpus (~40 documents). Start with
`ARCHITECTURE.md`. Design docs can drift; code and `--help` are
canonical. `docs/design/CLI.md` is generated. This guide
(`docs/dev-guide/`) cites design docs for *rationale* and code for
*facts*.

**`tests/`** — the suite (2800+). Default run is parallel
(`-n auto`) and excludes the Node shim (`-m 'not node'`). Markers:
`slow` (real-subprocess/server tests whose runtime IS the coverage),
`integration`, `node`. The fast lane is
`uv run pytest -m "not slow and not node"` — note a command-line `-m`
REPLACES the pyproject default, hence both terms. See 11-testing.md.

---

## 4. The Golden Rules

These are the most important pages in this guide. Each rule states the
rule, the incident or bug class behind it, and the exact verification.
Violating any of these is a defect even when the tests stay green.

### G1 — The vendor rule: nothing in git references the model vendor

> ⛔ **NEVER** let any committed byte — source, docs, commit messages,
> commit trailers, branch names, goldens — name the model vendor or its
> model families. No attribution trailers of any kind
> (`Co-Authored-By: …` naming a model/vendor, session-link trailers,
> "generated with" footers).

**Why.** This is a durable repo rule set by the operator. It is also why
this guide's own verification below assembles its grep pattern at
runtime — the checker must not itself contain the reference.

**How to verify** (run before proposing any commit; both greps must
print `0`):

```sh
# Assemble the two forbidden name stems at runtime so the check itself
# never spells them (the stems below are deliberately incomplete):
pat="$(printf 'c%s|a%s' 'laude' 'nthropic')"

# 1. Your commits' messages, authors, and trailers:
git log --format='%an <%ae>%n%B' origin/main..HEAD | grep -icE "$pat"

# 2. Your diff's content:
git diff origin/main...HEAD | grep -icE "$pat"

# 3. No attribution trailers at all:
git log --format='%B' origin/main..HEAD | grep -ic 'co-authored-by'
```

**Failure mode when skipped.** The reference lands in permanent history;
scrubbing it later means rewriting published history. Treat a hit as a
hard stop, not a cleanup task.

### G2 — `uv sync --all-extras`, always

> ✅ **ALWAYS** install with `uv sync --all-extras` (or `make install`,
> which wraps it). ⛔ **NEVER** run bare `uv sync` in this repo.

**Why.** A bare `uv sync` removes everything not in the default
dependency set — which in this repo means it DELETES the dev tooling from
`.venv/`: pytest, mypy, ruff, pre-commit, import-linter, the dashboard
test deps, and the editable `zicato-examples` workspace member. The
README says it in one line:

```
`uv sync --all-extras` always — bare `uv sync` will drop the dev extras from
`.venv/`.
```
*(README.md §"Development setup")*

**Failure mode when skipped.** The next `uv run pytest` fails with
`Failed to spawn: pytest — No such file or directory`, or —
worse — imports of `zicato_examples.*` fail inside spawned tournament
worker subprocesses and e2e tests abort confusingly mid-tournament. (This
exact spawn failure occurs on any fresh worktree that runs a test before
syncing — sync first, then test.)

**Verify:** `uv run pytest --version && uv run mypy --version` both
resolve from `.venv/`.

### G3 — Never start a live model run without explicit operator go-ahead

> ⛔ **NEVER** start a `zicato evolve` that calls real LLM endpoints (or
> otherwise spends model budget) unless the operator has explicitly said
> to. Verification is done with the test suite and the deterministic
> scripted targets — never with live runs.

**Why.** Live runs spend real money and real rate-limit budget, and an
agent-initiated one is indistinguishable from a runaway loop. The repo
gives you a fully sanctioned substitute: the **deterministic scripted
e2e** in `examples/zicato_examples/target_0_convergence/` — "no LLM
exists anywhere — the harness is deterministic, the proposer is a script
— and the loop provably converges" (its `RUN.md`). It exercises the FULL
shipped loop: real propose → apply → validate → subprocess tournament
workers → reduce → gate → persist, under the default git generation
store. Its CI-runnable form is `tests/test_convergence_known_answer.py`
(Golden Rule G4). `target_1_presentation` is the deterministic mock
target for adapter-level work.

**Corollary** (from `AGENTS.md`): when the operator DOES authorize a live
run, every launch enables the dashboard and you report its URL (default
`http://127.0.0.1:7892`).

**Verify:** your change's e2e evidence cites `RUN.md` steps or oracle
tests, never a live endpoint.

### G4 — The two oracles must be green before ANY commit is proposed

> ✅ **ALWAYS** run both oracle suites before proposing a commit, no
> matter how unrelated your change feels:
>
> ```sh
> uv run pytest tests/test_convergence_known_answer.py -q
> uv run pytest tests/test_decision_procedure_power.py -q
> ```

**Why.** These two files are the repo's end-to-end truth anchors:

- `tests/test_convergence_known_answer.py` — the known-answer convergence
  harness: the FULL loop, no tournament stubs, real subprocess workers,
  the default git generation store, converging on an exact,
  hand-computable scalar floor (`v0 = 3.6 → v1 = 2.4 → v2 = 3.6 rejected
  → v3 = 1.2`, the floor) with a `promoted → rejected → promoted`
  decision script and a pinned round-log event sequence. It covers the
  gauntlet AND a real racing multi-challenger round.
- `tests/test_decision_procedure_power.py` — Tier 2: the decision
  procedure's OPERATING CHARACTERISTICS under seeded noise (margin gate,
  replication, monotonicity scope, the Bradley–Terry pre-gate), with
  deterministic seeded trials. It pins measured facts other chapters
  cite (e.g., the pre-gate's ~37-consecutive-wins CI-separation
  requirement).

Almost any behavioural regression in the loop — scoring arithmetic,
cache-slot collisions, gate semantics, event ordering, resume, lineage —
turns one of them red. They are cheap relative to what they catch.

**Failure mode when skipped.** A green unit-test run hides an integration
break; the next contributor bisects YOUR commit out of a red oracle.

### G5 — Parity gates, import contracts, and the node suite

> ✅ **ALWAYS** keep these three gates green alongside pytest:
>
> ```sh
> bash tools/parity.sh        # the behavior-preserving refactor oracle
> uv run lint-imports         # the library/driver import contracts
> make node-test              # the dashboard JS behaviour suite
> ```

**Why — parity.** `tools/parity.sh` runs six gates and diffs freshly
computed artifacts against committed goldens under
`tools/parity/golden/`:

```
#   PYTEST         the full test suite (2800+ tests) — the primary
#                  behavioral characterization. Must pass.
#   CONTRACT-HASH  the epoch contract hash (+ per-component hashes) for a
#                  fixed fixture contract is byte-identical to the golden.
#   CLI-HELP       `zicato --help` and every subcommand `--help` is
#                  byte-identical to the golden.
#   REINDEX-DUMP   the SQLite index, rebuilt from a fixture workspace and
#                  dumped to stable text, is byte-identical to the golden.
#   MOCK-GOLDEN    a deterministic, no-live-LLM racing mock evolve produces
#                  gen_score.json / experiment.json / loss.json / lineage.json
#                  artifacts byte-identical (after masking wall-clock noise)
#                  to the golden.
#   MYPY           the mypy error count is not worse than the committed
#                  baseline (a refactor should reduce it).
```
*(tools/parity.sh, header comment)*

The CONTRACT-HASH gate in particular is the repo's tripwire for the
epoch-roll bug class (see G6 and 03-contract-and-epochs.md). It caught a
real one: `_canon_mutable_trees` used to filesystem-resolve paths,
folding the process cwd into the hash — "golden red in every checkout but
the capture one" (commit `fix(contract): the hash identifies the
contract, not the checkout`). A red gate after your change means you
moved observable behaviour: either that was the point (update the golden
WITH a CHANGELOG entry) or it is a bug.

**Why — import contracts.** The library/driver split (§1.3) only holds
because `uv run lint-imports` enforces it. If you add an import from a
library package to a driver, the contract fails; do not "fix" it by
editing the contract — restructure the dependency (usually: the thing you
want belongs in `zicato.query` or a library seam).

**Why — node.** The dashboard's JS behaviour tests run standalone under
node (`src/zicato/dashboard/static/test/run-all.mjs`); the default pytest
run deliberately excludes the in-pytest shim (`-m 'not node'`) so
`make node-test` is the canonical run. Frontend regressions are invisible
to pytest.

### G6 — Omit-at-default contract discipline

> ⚠️ **TRAP** — adding a field to `ScoringWeights` (or any nested
> contract dataclass) and emitting it unconditionally in the canonical
> form ROLLS EVERY EXISTING EPOCH the moment your code ships, and turns
> the parity CONTRACT-HASH gate red. A purely-additive, default-off field
> MUST be registered in `_SCORING_OMIT_AT_DEFAULT_FIELDS`
> (`src/zicato/epoch/contract.py`) so it is omitted from the hash while
> it holds its default.

**Why.** The contract hash must be byte-stable across zicato upgrades for
an unchanged contract. The canonicalizer enumerates every dataclass field
— so a new key changes the canonical JSON, changes the hash, and the next
`evolve` on every workspace auto-rolls its epoch "because you upgraded",
resetting pattern history and severing comparability for no operator
action. The omit-at-default set is the escape: the key only appears once
the operator sets a non-default value — at which point rolling IS
correct (they changed the contract). Current members:
`diff_complexity_weight`, `experiment_memory`, `random_baseline_every_n`,
`block_on_containment_violation`, `block_on_gate_contradiction`,
`screen_entries`, `screen_veto_only`, `process_exemplars`.

The full discipline — including when a default-ON field is the right call
and must ship with a CHANGELOG "epochs will roll" notice instead — is
03-contract-and-epochs.md §"omit-at-default", with the flagship
add-a-knob recipe. The serializer-completeness guard
(`tests/test_contract_serializer_completeness.py`) will fail on any new
contract field until you register a non-default value for it — that
failure is the checklist working, not an obstacle.

**Verify:** `bash tools/parity.sh --only CONTRACT-HASH` and
`uv run pytest tests/test_epoch_contract.py tests/test_contract_serializer_completeness.py -q`.

### G7 — The reserved replicate-base ledger

> ⛔ **NEVER** schedule board-unit work at an ad-hoc replicate index. The
> replicate axis is a partitioned ledger; claiming a base without
> registering it in the cross-referenced constants corrupts the per-unit
> cache for everyone.

The ledger, as documented at its anchor constant:

| Base | Owner | Constant (file) |
|---|---|---|
| `0..` | real tournament duels | (implicit — duel replicates count up from 0) |
| `1000` | A/A noise-floor calibration | `CALIBRATION_REPLICATE_BASE` (`src/zicato/tournament/calibration.py`) |
| `2000 + j` | contract pre-flight degraded draw, probe `j` | `PREFLIGHT_REPLICATE_BASE` / `PREFLIGHT_REPLICATE_SPAN` (`src/zicato/epoch/preflight.py`) |
| `3000` / `3001` | candidate screen / its confirm-before-veto re-run | `SCREEN_REPLICATE_BASE` (`src/zicato/epoch/screen.py`) |
| `4000` | evidence-gate replicate duels | `EVIDENCE_REPLICATE_BASE` (`src/zicato/selection/evidence_gate.py`) |

**Why.** The per-unit cache is keyed `(generation, entry, replicate)`.
The anchor constant's own comment explains the two failure directions:

```python
#: Replicate-index base for the pre-gate's evidence duels. Evidence replicate
#: ``j`` runs the crowning pair at replicate index ``EVIDENCE_REPLICATE_BASE
#: + j`` — a RESERVED per-unit cache slot — so each replicate draws BOTH
#: sides (champion AND challenger) fresh instead of replaying the canonical
#: replicate-0 sample the tournament already scored: identical data repeated
#: through the fit would shrink the Bradley--Terry SE by repetition alone
#: (fast mode), and a force-fresh re-run at slot 0 would clobber the child's
#: canonical ``loss.json`` that reindex/crash-resume key on (full mode).
EVIDENCE_REPLICATE_BASE: int = 4000
```
*(src/zicato/selection/evidence_gate.py, `EVIDENCE_REPLICATE_BASE` — excerpt)*

Colliding with `0..` either replays a cached sample as if it were a fresh
draw (statistically corrupt: repetition masquerading as evidence) or
clobbers the canonical `loss.json` that reindex and crash-resume key on
(record corruption). Colliding with another reserved base cross-poisons
two subsystems' caches. If you add a new evaluation channel, claim a new
base far from the others AND cross-reference it in every sibling
constant's docstring — that mutual documentation is the registration.

**Verify:** `grep -rn "REPLICATE_BASE" src/zicato` — every base constant
must list every other.

### G8 — The restricted-visibility envelope

> ⛔ **NEVER** let anything entry-identifying reach the proposer — no
> entry ids, no task text, no per-entry pass/fail identities, and never
> anything derived from the HOLDOUT slice. Everything the proposer (and
> its critic, and its screen feedback) sees is aggregated, banded, and
> board-anonymous.

**Why.** The proposer is an adaptive optimizer; whatever it can see, it
can memorize. The overfitting program (`docs/design/OVERFITTING.md` §11)
draws the envelope at the render boundary
(`src/zicato/proposer/prompts.py`, under the
`overfitting.restrict_proposer_visibility` flag, default on): detector
patterns are aggregated to counts/rates, experiment-memory Δscalar is
coarsened to improved/flat/regressed bands, the failure profile is
bucketed marginals, the operator summarizer's output is sanitized and
banded by zicato before splicing, screen results carry COUNTS ONLY, and
process exemplars are mechanically redacted (rules enforced in code in
`src/zicato/analyzer/process_exemplars.py`, never by an LLM). The
best-of-N critic is inside the same envelope by construction — it renders
through the same `render_user_prompt` under the same flag
(`src/zicato/proposer/best_of_n.py` §"Overfitting discipline").

**Failure mode when broken.** The loop overfits the board: train scores
climb, holdout confirmation starts flipping crowns
(`holdout_not_confirmed`), the generalization-gap detector fires, and
every "win" since the leak is suspect. This is the hardest bug class to
un-ship because the damage is in the promoted lineage, not the code.

**Verify:** any new proposer-visible surface must state, in its
docstring, what it aggregates/bands and why it cannot carry an entry
identity — and needs a test asserting no entry id appears in the rendered
block (grep the prompt for every board entry id; see the existing
patterns in `tests/` for `restrict_visibility`).

### G9 — Module-level callables only across the worker boundary

> ✅ **ALWAYS** define anything that crosses into a tournament worker —
> harness/auxiliary/judge `call_llm` callables, adapter factories,
> scoring plugins, predicates, python-mode judges — as a module-level (or
> class-attribute) object importable by dotted path. ⛔ **NEVER** pass a
> closure-local callable.

**Why.** Every tournament run executes in its own OS process
(`python -m zicato._tournament_worker` — the L3 robustness layer). The
worker re-imports callables from `module:qualname` dotted paths built by
`_callable_dotted_path`:

```python
    if "<locals>" in qualname:
        raise ValueError(
            f"callable {module}:{qualname} is defined inside a function "
            "(closure-local) and cannot be re-imported by a subprocess "
            "worker; pass a module-level callable instead"
        )
    return f"{module}:{qualname}"
```
*(src/zicato/tournament/worker_transport.py, `_callable_dotted_path`)*

A closure has `<locals>` in its `__qualname__` and cannot be re-imported;
the transport surfaces that as a clear `ValueError` at spawn time
precisely because the alternative — an opaque worker crash — cost real
debugging time. The same logic governs configuration: everything the
worker needs crosses as JSON (`ScoringWeights.to_json`/`from_json`, the
`_weights_spec` boundary) — a field that does not cross leaves the worker
scoring under defaults while the orchestrator believes otherwise (the
historical `per_judge_weights` desync class; see 03 §"serializer
completeness" and 07-runtime-and-durability.md).

**Verify:** tests that stub the worker use the documented anchor
`runner._run_single` (see `tests/_subprocess_worker_support.py`), and at
least one test in your change exercises the REAL subprocess path if you
touched anything on the wire.

### G10 — Digest-gated rendering in the dashboard

> ⛔ **NEVER** rebuild DOM (Document Object Model) on a no-op SSE
> heartbeat. Every dashboard pane computes a stable digest of its derived
> view state and re-renders ONLY when the digest changes.

**Why.** This is the root fix for the recurring flashing/self-refreshing
dashboard bug class. The pipeline is engineered end to end for it: the
SSE broker coalesces write bursts into a single `state_change` frame
(250 ms window, `_COALESCE_WINDOW_S` in `src/zicato/dashboard/sse.py` —
"this is what stops the old flashing / self-DoS where every file write
fanned out into a fresh wave of per-endpoint polls"), and every frontend
pane is digest-gated (`src/zicato/dashboard/static/js/livestatus.js`:
"a steady heartbeat ping writes ZERO DOM"; same discipline in `tree.js`,
`shell.js`, `compare.js`; overlays like `hovercard.js` render OUTSIDE
digest-gated panels so they cannot trigger repaint loops).

**Rules when touching the frontend:** fold any state that should repaint
into the pane's digest (e.g. `tree.js` folds the live-row set in);
re-stamp static chrome only on mount, never per beat; reset a pane's
digest when its host is rebuilt (a stale digest must not skip the first
paint — `shell.js` `mountShell`). The full checklist lives in
`src/zicato/dashboard/static/js/CONTRACTS.md` and
09-dashboard-and-query.md.

**Verify:** `make node-test`, plus manual check: with a live evolve
paused, the dashboard's DOM mutation count under a steady heartbeat must
be zero (the node suite asserts the digest behaviour for the core panes).

### The three standing operational rules

Not numbered because they are operator-facing (from `AGENTS.md`), but you
will hit them constantly:

- **Files are canonical; the index is derived.** Never hand-edit
  `index.db`; after hand-editing a canonical file, run `zicato repair index`.
- **Contract edits roll epochs.** Editing `board.jsonl`, `brief.md`,
  `scoring.json`, the registered harness identity, or the proposer dir
  mid-epoch rolls the epoch on the next `evolve` (that is the design —
  see 03-contract-and-epochs.md). Use the `board` subcommands to inspect
  frozen boards, not to casually edit live ones.
- **Mandatory hypothesis, written before, outcome after.** Already
  covered under the vocabulary entry — repeated here because it is also
  an operating rule for any tooling you build on top of experiments.

---

## 5. Your first hour

Everything below is copy-pasteable from the repo root. Times are rough.

### 5.1 Set up (5 min)

```sh
git clone <the-repo> zicato && cd zicato
uv sync --all-extras        # G2 — NEVER bare `uv sync`
make install-hooks          # pre-commit shim into .git/hooks/
```

`uv sync --all-extras` installs zicato editable, all extras (adk,
dashboard, dev), and the `zicato-examples` workspace member — the
examples MUST resolve as `zicato_examples.*` from anywhere, including
inside spawned tournament worker subprocesses.

Sanity:

```sh
uv run zicato --help        # fast — the lazy-import discipline at work
uv run pytest --version
```

### 5.2 Run the fast lane (5–10 min)

```sh
uv run pytest -m "not slow and not node" -q
```

This drops the real-subprocess/real-server `slow` tests and the node
shim. Remember: a command-line `-m` REPLACES the pyproject default
`-m 'not node'`, which is why the expression carries both terms. The full
default suite (`uv run pytest`) fans out with `-n auto`; use `-n0` for a
serial debug run of one test.

### 5.3 Run the oracles (G4) and the other gates (10–20 min)

```sh
uv run pytest tests/test_convergence_known_answer.py -q
uv run pytest tests/test_decision_procedure_power.py -q
uv run lint-imports
make node-test
bash tools/parity.sh        # the full six-gate oracle (slowest; runs PYTEST too)
```

If you are iterating, `bash tools/parity.sh --skip PYTEST` runs just the
golden gates; `--only CONTRACT-HASH` is the one to reach for whenever you
touch anything near `ScoringWeights` or `epoch/contract.py`.

### 5.4 Run one deterministic e2e (10 min)

The sanctioned no-endpoint end-to-end (G3) is target_0. Follow
`examples/zicato_examples/target_0_convergence/RUN.md` verbatim; the
skeleton is:

```sh
rm -rf /tmp/zicato-smoke-t0 && mkdir -p /tmp/zicato-smoke-t0 && cd /tmp/zicato-smoke-t0
PY=<repo>/.venv/bin/python

# 1. Bootstrap the workspace.
$PY -m zicato.cli init --workspace .zicato

# 2. Declare the deterministic import-kind adapter + mutable tree +
#    skills-only proposer dir into .zicato/config.json (RUN.md has the
#    exact python snippet), copy the example board/brief/scoring next to
#    the workspace, then:
$PY -m zicato.cli evolve --rounds 3 ...   # per RUN.md — scripted, no LLM
```

What you should observe — this IS the known answer:

- decisions `promoted → rejected → promoted`, generations `v1, v2, v3`;
- champion scalar `3.6 → 2.4` (v1), the negative control v2 rejected at
  `3.6`, the floor `1.2` at v3;
- `epochs/{epoch}/generations/v{1,2,3}/experiment.json` each with one
  patch against mutation id `style_rules` and the right outcome;
- `lineage.json` with v2 as a dead branch (`promoted: false`) and the
  `current_generation` marker reading `v3`;
- one `epochs/{epoch}/rounds/{N}/round_log.jsonl` per round whose event
  sequence matches the canonical list in 02-architecture.md.

If any of that differs, your tree is not sane — stop and bisect before
writing code.

### 5.5 Orient in the code (rest of the hour)

Read, in order:

1. `src/zicato/orchestrator.py` — just the module docstring and
   `evolve_once`'s docstring (the 13 numbered steps).
2. `src/zicato/epoch/contract.py` — the module docstring (the six
   components and the canonicalization promise).
3. `tests/test_convergence_known_answer.py` — top-of-file docstring +
   `test_gauntlet_converges_to_known_floor` — this is the loop's
   behaviour, executable.
4. Then chapter 02 of this guide with the orchestrator open in a split.

> ✅ **ALWAYS** re-run §5.3's commands before proposing any commit, and
> G1's vendor scan on your own diff. That is the whole pre-commit ritual:
> oracles, parity, import contracts, node, vendor scan.

---

## 6. First-day traps

A grab-bag of mistakes that cost previous contributors real time. Each
one is grounded in a guard that exists in the code precisely because the
mistake happened.

> ⚠️ **TRAP — the two-callable rule.** `RuntimeConfig.harness_call_llm`
> and `RuntimeConfig.auxiliary_call_llm` MUST be identity-distinct
> callables. The harness callable is what the inner harness runs on; the
> auxiliary callable drives every zicato-internal consumer (emulator,
> proposer, judges, analysis). If they are the same object, the emulator
> can trivially collude with the inner harness through shared state —
> the emulator drives a hard error on identity match
> (`src/zicato/emulator/`), and `assert_distinct_callables`
> (`src/zicato/core/workspace.py`) is the construction-site check the
> runner re-runs at startup. In tests, two separately-defined
> module-level functions, not two references to one.

> ⚠️ **TRAP — `-m` on the pytest command line REPLACES the default.**
> The pyproject default is `-m 'not node'`. Passing `-m slow` silently
> re-includes the node shim; the fast lane must always be spelled
> `-m "not slow and not node"`. (pyproject.toml
> `[tool.pytest.ini_options]` documents this.)

> ⚠️ **TRAP — best-effort is a contract, not a habit.** The round's
> non-critical writes (round log, index dual-write, dashboard envelopes,
> progress log, analysis regeneration) are wrapped in
> `best_effort(...)` (`src/zicato/util/`) so a log/render failure can
> NEVER fail the round. If you add a round step: decide explicitly
> whether it is load-bearing (let it raise) or observational (wrap it),
> and say which in the docstring. An observational step that can raise
> is a latent round-killer; a load-bearing step that is swallowed is a
> silent corruption vector.

> ⚠️ **TRAP — lazy imports in the orchestrator are deliberate.** Heavy
> siblings are imported inside function bodies (`# noqa: PLC0415`) so
> `zicato --help` stays fast on installs without the runtime extras.
> Do not "clean up" a function-local import to module level in
> `orchestrator.py`, `cli/`, or `epoch/lifecycle.py` without checking
> what it drags in at import time.

> ⚠️ **TRAP — the orchestrator's late binding is load-bearing for
> tests.** `evolve_n_rounds` resolves `evolve_once`,
> `ensure_epoch_for_contract`, `block_while_paused` and friends through
> the `zicato.orchestrator` module OBJECT at call time, exactly so the
> suite's `monkeypatch.setattr(orch, "evolve_once", ...)` works. If you
> move a helper out of the orchestrator, re-export it from
> `zicato.orchestrator` (see `zicato.evolve.epoching` — every name is
> re-exported) or you break the documented patch points.

> ⚠️ **TRAP — champion cache semantics differ by mode.** In full mode
> the champion is cache-read by default because it is immutable within
> an epoch (`run_tournament`'s champion cache-read note); `--mode full`
> from the orchestrator force-freshes it for noise re-sampling — EXCEPT
> on a crash-resumed round, which must cache-read or resume stops being
> nearly free (`champion_force_fresh=(not fast_mode) and
> resumed_experiment is None` in `evolve_once`). If you touch runner
> caching, re-read that expression and its comment first.

> ⚠️ **TRAP — generation ids restart at `v0` every epoch.** Anything
> keyed by a bare generation id (an operator gate override, a cache, a
> dashboard deep link) is ambiguous across epochs. This is why the
> auto-roll drains pending gate overrides
> (`drain_stale_gate_overrides` in
> `src/zicato/evolve/epoching.py::ensure_epoch_for_contract`) — a
> pending "promote v3" would otherwise fire on the NEW epoch's v3. Key
> new artifacts by `(epoch_id, generation_id)`, never by `vN` alone.

> ⚠️ **TRAP — run ids must be stable per (generation, entry).** The
> index's `runs` table keys on `run_id`; a harness that derives run ids
> from the entry alone silently overwrites each generation's rows with
> the next one's (the convergence oracle pins the fix: ids shaped
> `conv-<generation>-<entry>`). If you write an adapter, derive run ids
> from the full stable coordinate.

Where to next: 02-architecture.md for the round walkthrough,
03-contract-and-epochs.md before touching ANY config surface,
11-testing.md for the suite's conventions, 12-bug-casebook.md for the
war stories behind these traps.
