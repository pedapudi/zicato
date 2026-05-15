# Changelog

All notable changes to zicato are recorded here. Format roughly follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

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
