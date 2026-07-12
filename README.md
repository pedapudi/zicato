<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/brand/zicato-lockup-dark.svg">
  <img alt="zicato" src="docs/brand/zicato-lockup-light.svg" width="420">
</picture>

**A self-improving harness for multi-agent systems.**

</div>

# zicato

zicato wraps a multi-agent system you already have — a coordinator + specialists,
a deep sub-agent tree, a single LlmAgent, whatever shape — and turns it into the
**inner harness** of a learning loop. It runs your system against a board of
tasks, watches what goes wrong via structured runtime telemetry, and rewrites
the inner harness so the next generation goes less wrong.

zicato is the third member of an ecosystem:

- **[goldfive](https://github.com/pedapudi/goldfive)** — orchestration scaffolding:
  goals, plans, per-turn drift analysis, an intervention ladder. Emits a typed
  event stream (`goldfive.v1.Event`) that names *what went wrong* in a run.
- **[harmonograf](https://github.com/pedapudi/harmonograf)** — the observability
  + HCI console: Gantt, graph, trajectory, intervention history. Renders the
  goldfive stream live and lets operators steer.
- **zicato** — the meta-loop: same telemetry stream, but consumed across many
  runs. zicato aggregates drift into **loss patterns**, proposes structured
  edits to the inner harness (agent instructions, tool descriptions, planner
  templates, role scopes), runs tournaments, and promotes the patches that
  reduce loss.

## Where this fits

| Layer | Owner | Cadence |
|---|---|---|
| Single-turn refine (replan in response to drift) | goldfive | within one run |
| Operator-driven steering | harmonograf | within one run |
| **Inner-harness rewrites across runs** | **zicato** | **across generations** |

Goldfive owns plans; zicato owns the prompts and structure that *produce* the
plans. The two are complementary: goldfive handles "this run wandered, replan
this run", zicato handles "this kind of run keeps wandering the same way,
rewrite the harness".

## Status

Alpha. Design and surface are under active iteration — the public API will
break. The first reference adapter targets Google ADK (the framework goldfive
itself wires deepest into). The design is **framework-agnostic at its core**:
any inner harness that fronts a `HarnessAdapter` and emits goldfive telemetry
can participate. LangChain and plain-callable adapters land after ADK.

## Model-agnostic

zicato calls LLMs only through a narrow `call_llm(system, user, model) -> str`
callable supplied by the caller. No vendor SDK is imported by the library
itself; bring whatever model you want.

## Development setup

```sh
uv sync --all-extras   # install package + dev tooling (ruff, mypy, pytest, pre-commit, ...)
make install-hooks     # equivalent to `uv run pre-commit install`
```

`uv sync --all-extras` always — bare `uv sync` will drop the dev extras from
`.venv/`. `make install-hooks` writes a `.git/hooks/pre-commit` shim that runs
the project's own pre-commit (from `.venv/`) so `git commit` checks match
`uv run pre-commit run --all-files`.

## Design docs

The full design lives under [`docs/design/`](docs/design/). Read
`ARCHITECTURE.md` first; everything else assumes it.

- [`docs/design/ARCHITECTURE.md`](docs/design/ARCHITECTURE.md) — top-level: what zicato is, the meta-loop diagram, every component, the cadence comparison against goldfive and harmonograf.
- [`docs/design/MUTATION-SURFACE.md`](docs/design/MUTATION-SURFACE.md) — annotated mutation points: span and file markers, AST resolution, the `MutationPoint` shape, validator constraints, the `zicato mutations` audit CLI.
- [`docs/design/BOARD-FORMAT.md`](docs/design/BOARD-FORMAT.md) — JSONL board entry schema: common fields, the three entry kinds (single-turn, multi-turn scripted, multi-turn emulated), the five expectation kinds.
- [`docs/design/EPOCHS-AND-JOURNALING.md`](docs/design/EPOCHS-AND-JOURNALING.md) — epoch lifecycle, the `Experiment` artifact (hypothesis + patches + outcome), `journal.md` and the closing analysis pass, cross-epoch lineage.
- [`docs/design/TELEMETRY.md`](docs/design/TELEMETRY.md) — capturing goldfive's `goldfive.v1.Event` stream via its `JSONLPersistenceSink`, the post-run reducer, the `LossProfile` shape, the emulator's `zicato:emulator` audit lane.
- [`docs/design/SCORING.md`](docs/design/SCORING.md) — the weighted drift-loss formula, the pass-rate side, the tournament promotion gate (margin on drift + strict monotonicity on pass-rate), fast mode.
- [`docs/design/TOURNAMENT.md`](docs/design/TOURNAMENT.md) — the competition model: the king-of-the-hill gauntlet (champion vs successive challengers), the dashboard Tournament view (bracket + per-matchup detail), the tournament-detail analytics (verdict transparency, per-entry A/B grid, hypothesis ledger, optimization trajectory, mutation heat map, cost), and the harmonograf split — execution view vs competition view.
- [`docs/design/SELECTION.md`](docs/design/SELECTION.md) — the decision theory under the tournament: how RL gating, racing, and bracket schedulers make the champion-vs-challenger decision; why zicato's gauntlet is a degenerate elitist iterated race; why brackets (single/double-elim, Swiss) are the wrong primitive here; and the phased path to replication-based racing (paired significance gate, winner's-curse confirmation, trust-region step bound). Diagrams + cited sources.
- [`docs/design/EMULATOR.md`](docs/design/EMULATOR.md) — the multi-turn user emulator: the two-callable rule (hard error on identity match), sealed context construction, answer-leak heuristic, audit-trail spans.
- [`docs/design/DOGFOOD-TARGETS.md`](docs/design/DOGFOOD-TARGETS.md) — the three targets (presentation agent v0; goldfive's steering v0+1; zicato itself v0+2) and the v0 design commitments they force.
- [`docs/design/RUNTIME.md`](docs/design/RUNTIME.md) — `.zicato/runtime/` state file layout, the two processes `zicato evolve` auto-spawns (a Rust watchdog supervisor on :7920 and a separate Python dashboard service on :7892), heartbeat protocol, signal escalation, single-writer concurrency model.
- [`docs/design/DASHBOARD.md`](docs/design/DASHBOARD.md) — the live console for an in-flight epoch: Starlette HTTP + SSE architecture, the home view's cross-epoch meta-loop ledger, the live racing hero (full-width scalar track + rung stepper, champion-gate rows, WHAT'S RUNNING / LIVE ACTIVITY), the first-class tournament **Builder** view (`#/builder`) and the routed Settings drawer (Contract tab reuses the builder's live preview), per-entry continuous score + precision/recall, the full GET API surface, and the control-file protocol for operator actions.
- [`docs/design/PROPOSER.md`](docs/design/PROPOSER.md) — the proposer as a first-class contract input: the default tool-using ADK agent (skill-composed is the explicit opt-in), the read-only proposer tool registry, the board-anonymized train-slice-only failure-mode feedback channel (`outcome_summarizer_spec`), and why a proposer/skills change rolls the epoch.
- [`docs/design/ROBUSTNESS.md`](docs/design/ROBUSTNESS.md) — the six-layer defense model (asyncio timeouts → cancellation → subprocess workers → watchdog → circuit breaker → atomic writes), what each layer catches, failure-mode tables, the GIL discussion that makes subprocess isolation non-negotiable, phasing.
- [`docs/design/LOOP-HEALTH.md`](docs/design/LOOP-HEALTH.md) — loop-health diagnostics: detecting a running-but-meaningless loop (a degenerate, toothless evaluation), the five detectors and severities, the `LoopHealth` report, the `zicato health` CLI, and how the orchestrator surfaces critical findings.
- [`docs/design/STORAGE.md`](docs/design/STORAGE.md) — the pluggable `StorageBackend` (file + memory backends) and the `GenerationStore` protocol with both directory and git backends shipping; the v0 directory-snapshot layout; the three-storage-concerns split; and the still-roadmap operator git CLI (`zicato repo` / `log` / `diff` / `show` / `bisect` / `blame`, `workspace migrate-to-git`).
- [`docs/design/ANALYTICAL-INDEX.md`](docs/design/ANALYTICAL-INDEX.md) — the `.zicato/index.db` SQLite analytical index: why cross-run views are queries not file-walks, the files-canonical / index-derived discipline, `zicato reindex`, and the eleven-table schema (SCHEMA_VERSION 12, incl. the visibility-rating `generations.elo*` columns).
- [`docs/design/CLI.md`](docs/design/CLI.md) — full CLI reference: every subcommand, every flag, exit codes, scripting hints.
- [`docs/design/RATIONALE.md`](docs/design/RATIONALE.md) — the "why" behind every major decision: annotated mutation points, per-epoch contract, mandatory hypothesis, collusion-proof emulator, drift taxonomy as features.
- [`docs/design/VOCABULARY.md`](docs/design/VOCABULARY.md) — glossary of load-bearing terms (epoch, generation, run, round, experiment, hypothesis, outcome, loss profile, pattern, tournament, lineage, rubric).

## Brand

The mark, wordmark, lockups, tile, and favicons live in
[`docs/brand/`](docs/brand/) — see [`docs/brand/README.md`](docs/brand/README.md)
for the construction story (golden logarithmic spiral · damped-sine sparkline ·
one plucked-note accent), the color tokens, and the theme-adaptive rule
(`currentColor` ink + a `--zicato-accent` custom property).

## License

Apache-2.0. See [LICENSE](LICENSE).
