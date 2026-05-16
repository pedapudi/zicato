# Changelog

All notable changes to zicato are recorded here. Format roughly follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Storage
- Settled the storage design end-to-end and rewrote `docs/design/STORAGE.md`
  around it. The five data kinds get five fits: runtime state and the
  lineage/experiment/journal records → files (one record per file,
  through `StorageBackend`); telemetry → JSONL (goldfive's format,
  unchanged); generation source trees → a pluggable `GenerationStore`
  (directory snapshots today, git on the roadmap); the cross-run
  analytical index → a real DB (`index.db`, SQLite — DuckDB evaluated
  and deferred until scan latency is felt). The store of record stays
  files-canonical so concurrent subprocess runs stay lock-free and
  crash-isolated.
- Resolved the record-level vs generation-level seam fork: `StorageBackend`
  stays record-level (key→blob); generation source trees get a separate
  peer seam, `GenerationStore`, at the `epoch/` domain layer — not an
  extension of `StorageBackend`. Forcing source-tree transactions into
  the record ABC would make it carry domain vocabulary and stop being an
  honest storage seam.
- `zicato.epoch.genstore` — the `GenerationStore` protocol plus the
  shipped `DirectoryGenerationStore` (the existing directory-snapshot
  mechanism, byte-for-byte). `derive_generation` is the generation-level
  transaction boundary the record seam could not express: copy the
  parent tree, apply the patch set all-or-nothing, return the child
  snapshot root. The orchestrator's snapshot / baseline-seed / patch-apply
  paths now route through this seam — the single site a git backend
  would later be substituted.
- `epoch/` record I/O migrated onto `StorageBackend` (via the new
  `zicato.epoch._storage` adapter, mirroring `runtime/._storage`).
  `experiment.json`, the per-patch files, `lineage.json`, per-epoch
  `config.json` / `scoring.json`, and `journal.md` are now written with
  the same `.tmp` + `fsync` + rename atomicity the runtime layer already
  had — a crash mid-write can no longer leave a truncated record. No
  on-disk bytes moved; every `epoch/` signature is unchanged.
- Continuous indexing promoted from an add-on to the stated design: the
  orchestrator's live dual-write keeps `index.db` current as the loop
  runs; `zicato reindex` is the batch rebuild / repair path. The
  canonical-file-first ordering rule is documented as load-bearing.

### Robustness
- `zicato evolve --max-wall-clock-seconds <S>` — a total wall-clock
  budget for the whole evolve invocation. Previously only individual
  board entries had a `wall_clock_budget_seconds`; an N-entry × M-round
  invocation had no aggregate ceiling. The budget is enforced between
  rounds (the loop stops cleanly once it is spent) and within a round
  (a round that would overrun it is cancelled via `asyncio.wait_for`
  and recorded as an aborted round). It applies on top of — not
  instead of — each entry's per-entry budget. Also reads the
  `ZICATO_MAX_WALL_CLOCK_SECONDS` environment variable. When the loop
  stops on this budget, the `evolve` summary says so explicitly.

## [0.3.0] — 2026-05-15

Observability + analytics release. zicato grows a real dashboard, a
SQLite analytical index, a tournament/competition view distinct from
goldfive's execution view, and loop-health diagnostics. First real run
against a live model landed in this cycle.

### Live dashboard + supervisor
- `zicato-supervisor` Rust binary: watchdog (state-file monitoring +
  SIGTERM→SIGKILL escalation) + HTTP/SSE dashboard server, auto-spawned
  by `zicato evolve`.
- Multi-view dashboard (vanilla HTML/CSS/JS, bundled in the binary):
  **Overview**, **Tree** (cross-epoch lineage graph), **Tournament**
  (the bracket), **Epoch** (the contract). Fragment-routed; SSE-live.
- `/api/epoch` — the epoch's full evaluation contract (board, rubric,
  scoring, registered harness, mutation surface).
- `/api/tournaments` + `/api/tournaments/:id` — the gauntlet bracket and
  full per-matchup detail. `/api/health-report` — loop-health findings.
- Static assets served at document root (fixes the relative-path 404
  that rendered the dashboard unstyled).

### SQLite analytical index
- `.zicato/index.db` — a derived, queryable index of cross-run data
  (epochs / generations / experiments / patches / runs / loss_profiles /
  metric_counts / tournaments). Files stay canonical; the index is
  rebuildable via `zicato reindex` and dual-written live by the
  orchestrator. The Rust supervisor reads it via rusqlite.
- Generation source trees are NOT in SQLite (git, per the roadmap);
  per-run event capture stays JSONL. SQLite is the analytical layer only.

### Tournament view — competition, not execution
- The dashboard Tournament view is a king-of-the-hill **bracket**: a
  champion lineage (winners spine) + discarded challengers, each matchup
  parent-vs-child over the board.
- Per-matchup detail: hypothesis, patches, per-entry A/B grid
  (improved/regressed/flat), scalar breakdown, gate verdict + reasoning.
- `zicato/tournament/detail.py` analytics: bracket assembly, A/B grid,
  hypothesis ledger (proposer calibration with explicit sign+magnitude
  match semantics), optimization trajectory + plateau detection,
  mutation heat map (win-correlation), tournament cost.
- Architectural split: harmonograf renders the *execution view* (the
  temporal trace of one run); the zicato dashboard owns the *competition
  view*. A per-run drill-down links the two.

### Loop-health diagnostics
- `zicato/health/` — detects a toothless evaluation loop: degenerate
  scoring (consecutive zero-Δ tournaments), non-differentiating board
  entries, flat drift signal, missing expectations, stalled loop.
- `zicato health` CLI; the orchestrator writes a per-round health
  report, logs a loud warning on a critical finding, and stops the loop
  after sustained degeneracy (`evolve --stop-on-degenerate`).
- Motivated by the first real run — v0 and v1 scored identically
  (1.000000); the loop produced zero optimization signal and that was
  only discoverable by manual inspection.

### Runtime + telemetry
- Orchestrator writes a populated heartbeat (epoch / generation / round
  / phase); per-run progress is bumped on each goldfive event so
  dashboard run cards animate.
- `mutations.json` dumped per epoch so the dashboard can show the
  mutation surface.
- `HarmonografSink` attached when `ZICATO_HARMONOGRAF_URL` is set —
  runs stream live to a harmonograf server; the dashboard deep-links
  each run to its harmonograf session.
- Progressive `analysis.html` — regenerated after every generation.

### Harmonograf companion
- [pedapudi/harmonograf#292](https://github.com/pedapudi/harmonograf/pull/292)
  — a `harmonograf-replay` command ingests a zicato run's `events.jsonl`
  from disk, so any finished run is viewable in harmonograf.

### Design docs
- New: `TOURNAMENT.md`, `ANALYTICAL-INDEX.md`, `LOOP-HEALTH.md`,
  `RUNTIME.md`, `DASHBOARD.md`, `ROBUSTNESS.md`, `STORAGE.md`.
- Updated `ARCHITECTURE.md` / `CLI.md` / `EPOCHS-AND-JOURNALING.md`.

### Tests
- ~961 Python tests + the Rust supervisor suite (48 unit + 19
  integration). The 5 pre-existing environment-dependent failures
  (goldfive editable-install drift) are unchanged.

## [0.2.0] — 2026-05-15

Second alpha. Major surface expansion: drift-free objectives are
first-class, the board API has a friendly Python builder, multi-objective
scoring lands, regression-suite gating protects against breaking-change
patches, and zicato now consumes goldfive's decision telemetry through a
dedicated LLM analyzer.

### Drift-free metrics
- `MetricCount` replaces drift-only counts. Arbitrary namespaces:
  `drift:*`, `cost:*`, `latency:*`, `rubric:*`, `output:*`, `schema:*`.
- `LossProfile.metric_counts` carries the unified view alongside
  `drift_counts` for back-compat. New first-class fields:
  `tokens_spent`, `output_chars`, `schema_failures`.
- `HypothesisSpec.expected_metric_movements` accepted alongside
  `expected_drift_movements`. Proposer schema validates either form.
- `OutcomeRecord.metric_movements` records actual deltas per namespace.
- New detectors: `detect_metric_frequency(namespace=...)`,
  `detect_cost_outliers`, `detect_rubric_score_movement`. The original
  `detect_drift_kind_frequency` is now a thin wrapper.
- Analysis renderer: `render_metric_movement_table(namespace_filter=...)`
  with a "drift" filter producing byte-identical legacy output.

### Multi-objective scoring
- `ScoringWeights.namespace_weights` — per-namespace weights with a
  sign convention (positive = higher-is-worse; negative = higher-is-better;
  zero = excluded from scalar).
- `ScoringWeights.namespace_monotonicity` — per-namespace monotonicity
  flags. Regression on a tracked namespace hard-rejects the candidate
  regardless of overall scalar improvement.
- `aggregate_namespaced_metrics` + `scalar_components` in the
  generation aggregate.
- `evaluate_gate` adds rule 3: per-namespace monotonicity check.

### Regression-suite gate
- `zicato.tournament.regression.run_regression_suite` — asyncio
  subprocess pytest invoker with timeout + failed-id parsing.
- `ScoringWeights.regression_gate_enabled` / `regression_test_command`
  / `regression_timeout_s`. When enabled, any test failure in the
  candidate snapshot hard-rejects regardless of other metrics.
- `tournament --skip-regression` CLI flag.

### Friendlier board API
- `Board` + `Entry` programmatic builder. `Entry(id=..., input=...,
  evaluate=..., budget_s=...)` auto-detects the kind from arguments
  (`turns` → multi-turn-scripted, `persona` → multi-turn-emulated,
  `adversarial_agent_spec` → synthetic-adversarial, etc.).
- `Predicate` factory family: `Predicate.contains` / `.regex` /
  `.schema` / `.python`.
- `Rubric.judge(rubric_text, *, threshold, scale)` — built-in
  LLM-as-judge matcher. Operator supplies the rubric text only; no
  separate Python module needed.
- New expectation kind `"rubric"` with `evaluate_rubric_judge` runtime.
- `budget_s` JSONL field as a back-compat alias for
  `wall_clock_budget_seconds` (preferred on write, accepted on read).

### Decision-telemetry analyzer
- New `zicato.analyzer` module: `DecisionEventSummary` +
  `aggregate_decision_events` parse goldfive's new decision events
  (`SteeringDecisionMade`, `LadderTransitionDecided`,
  `DetectorDispatchOrdered`, `PolicyApplied`, `RetryBudgetSpent`).
- `analyze_epoch_telemetry` calls the auxiliary LLM with a structured
  prompt and writes
  `epochs/{epoch}/insights/round_{N}.md` with sections:
  Headline observations, Suspected over-intervention, Suspected
  under-intervention, Suggested next mutations.
- Orchestrator invokes the analyzer best-effort after every round;
  proposer embeds the latest insights into its user prompt so the next
  proposal is informed by the cross-run patterns.
- CLI: `zicato analyze-telemetry [--workspace ...] [--epoch ...] [--round N]`.

### Goldfive companion PRs (separate repo, awaiting merge)
- [pedapudi/goldfive#440](https://github.com/pedapudi/goldfive/pull/440)
  — manifest expansion from 31 → 60 entries; new proto events
  `LadderTransitionDecided`, `DetectorDispatchOrdered`, `PolicyApplied`,
  `RetryBudgetSpent` at tags 40-43.
- [pedapudi/goldfive#439](https://github.com/pedapudi/goldfive/pull/439)
  — pluggable `Judge` protocol with `JudgeContext` / `JudgeVerdict`;
  `goldfive.wrap(judges=[...])`; built-in detectors refactored as Judge
  instances; new `JudgementEmitted` proto event at tag 44.

### Tests
- 644 → 813 passing (+169). Same 5 pre-existing environment failures
  on `test_adapter_adk` / `test_synthetic_adversarial` /
  `test_telemetry_reducer_real_goldfive` are unchanged (all
  environment-dependent on goldfive-editable-install drift).

### Compatibility
- Every existing `LossProfile`, JSONL board, `expected_drift_movements`
  hypothesis, and tournament scoring config from 0.1 continues to work.
  The drift namespace is preserved verbatim; multi-objective scoring
  uses sensible defaults when namespace weights aren't configured.

## [0.1.0] — 2026-05-15

First public alpha. End-to-end evolve loop functioning against both
intended v0 dogfood targets. The library is usable; the API will still
break in patch releases until v0.2.

### Core loop
- `zicato init` / `register` / `epoch new` / `mutations` / `propose`
  / `tournament` / `evolve` / `epoch close` CLI surface auto-discovered
  via `zicato.cli.commands.*`.
- Annotated mutation surface (`# zicato:mutable` and
  `# zicato:mutable:file`) with AST resolution, applier, validator, and
  a `mutations` audit subcommand.
- Board format (single-turn, multi-turn-scripted, multi-turn-emulated,
  synthetic-adversarial, synthetic-clean) with five expectation kinds.
- Collusion-proof multi-turn emulator with the two-callable rule, sealed
  context construction, and a post-hoc answer-leak heuristic.
- Telemetry ingest via goldfive's `JSONLPersistenceSink` per run + a
  post-run reducer that produces `LossProfile`.
- Six pattern detectors (drift-kind frequency, hot tasks/agents,
  plan-revision instability, multi-turn memory failure, multi-turn
  context loss).
- LLM proposer with structured-output `Experiment` schema (hypothesis +
  patches) and retry-on-parse-failure.
- Tournament runner: full mode + fast inline keep/discard. Promote gate
  enforces a scalar margin AND strict pass-rate monotonicity.
- Epoch lifecycle with manual close + auto-close fallback; running
  `journal.md`; analysis pass at close producing both `analysis.md`
  (LLM-driven narrative) and a self-contained `analysis.html`
  (deterministic — inline SVG lineage, score trajectory, drift heatmap,
  per-experiment cards, dark-mode aware).
- Per-patch JSON files under `generations/v{N}/patches/{patch_id}.json`
  with `patch_ids` references in `experiment.json` (atomic write order:
  patches first, then experiment).

### v1.1 robustness
- `.zicato/runtime/` state file layer (heartbeat, active_runs,
  active_tournament, control, control_log, lock).
- Workspace lock with PID-aware stale-pid stealing.
- Heartbeat beater task lifecycle wired into `evolve_n_rounds`.
- Per-call `aux_call_llm` timeouts (`ZICATO_AUX_CALL_TIMEOUT`, default
  120s) at every auxiliary call site (proposer, judge, emulator,
  analysis).
- Progressive `analysis.html` regeneration after each generation (in
  addition to the final LLM-driven `analysis.md` at epoch close).
- Tournament runner publishes `active_tournament.json` + per-run
  `active_runs/{run_id}.json` for the live dashboard.

### Live dashboard
- Rust `zicato-supervisor` binary (single static binary, ~4.4 MB
  stripped). Auto-spawned by `zicato evolve`; URL printed to stdout.
  Opt out with `--no-dashboard`.
- Watchdog half: inotify-based file watching with SIGTERM → grace →
  SIGKILL signal escalation for stalled orchestrator + stalled runs.
- HTTP + Server-Sent Events server (default `:7892`). Endpoints:
  `/api/state`, `/api/lineage`, `/api/active-runs`,
  `/api/active-tournament`, `/api/heartbeat`, `/events`, `/api/health`,
  plus POST control endpoints (`/api/control/pause`, `…/kill/{id}`,
  `…/promote/{gen}`, `…/reject/{gen}`, `…/rubric`).
- Single-page browser UI (vanilla HTML/CSS/JS, no build step, ~77KB
  total) bundled with the binary via `include_dir!`. Includes lineage
  SVG, score trajectory, drift heatmap, **tournament status panel**
  with deterministic predicted-gate-verdict, active-runs strip, log
  tail, drill-down panels. Dark-mode aware. Action buttons present
  but disabled — v1.3 enables them via the control-file protocol.

### Examples
- `examples/target_1_presentation/` — vendored harmonograf reference
  agent with 9 mutation points + 7-entry board + rubric + scoring +
  deterministic mocks + `RUN.md`.
- `examples/target_2_goldfive_steering/` — adversarial board against
  goldfive's optimization surface (`pedapudi/goldfive` PR
  [#436](https://github.com/pedapudi/goldfive/pull/436)) with
  `LoopingAgent` / `HallucinatingAgent` / `RefusingAgent` etc., a
  `manifest_bridge` translating goldfive's optimization manifest into
  zicato MutationPoints, mocks + `RUN.md`.

### Design docs
- `ARCHITECTURE.md`, `MUTATION-SURFACE.md`, `BOARD-FORMAT.md`,
  `EPOCHS-AND-JOURNALING.md`, `TELEMETRY.md`, `SCORING.md`,
  `EMULATOR.md`, `DOGFOOD-TARGETS.md`, `CLI.md`, `RATIONALE.md`,
  `VOCABULARY.md`, `RUNTIME.md`, `DASHBOARD.md`, `ROBUSTNESS.md`,
  `STORAGE.md` — comprehensive coverage of every load-bearing decision.

### Tooling
- hatchling-based packaging; uv + pip-editable installs work.
- `pytest` + `pytest-asyncio` test suite (~640 passing).
- `ruff` + `mypy --strict` clean on the Python tree.
- `cargo build --release` + `cargo test` + `cargo clippy` clean on the
  Rust supervisor binary.
- GitHub Actions CI workflow over Python 3.11/3.12.
- `Makefile` with `install`, `test`, `lint`, `format`, `typecheck`,
  `check`, `supervisor`, `supervisor-test`, `install-supervisor`,
  `clean` targets.

### Known limitations (documented in RUN.md and the design docs)
- Mock-driven smoke runs produce byte-equal outputs across generations
  in v0; the tournament gate correctly rejects with "insufficient
  margin". Replace mocks with real LLMs for a meaningful loop.
- Multi-turn scripted/emulated drivers abort some examples with
  TypeErrors; the reducer treats those entries as zero-signal and the
  tournament continues. Cleanup is v1.0.1 work.
- Subprocess tournament workers (L3 robustness) and orchestrator
  control-file consumer (v1.3) are NOT yet implemented; see
  `docs/design/ROBUSTNESS.md` for the phasing plan.
- Git-backed generation storage (G1-G10) is documented but not yet
  implemented; v0 uses directory snapshots + per-patch JSON files.
