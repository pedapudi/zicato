<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/brand/zicato-lockup-dark.svg">
  <img alt="zicato" src="docs/brand/zicato-lockup-light.svg" width="420">
</picture>

**A self-improving harness for any system you can measure.**

</div>

# zicato

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

Model-backed adapters use named `target` and `evaluation` engines; a target is
adapter-defined and may consume no LLM at all. Advanced judge, emulator,
builder, and proposer overrides and execution capabilities are documented in
[`MODEL-CONFIG.md`](docs/design/MODEL-CONFIG.md).

Goldfive owns plans; zicato owns the prompts and structure that *produce* the
plans. The two are complementary: goldfive handles "this run wandered, replan
this run", zicato handles "this kind of run keeps wandering the same way,
rewrite the harness".

## Status

Alpha. Design and surface are under active iteration — the public API will
break. The first reference adapter targets Google ADK (the framework goldfive
itself wires deepest into). The design is **framework-agnostic at its core**:
the `HarnessAdapter` protocol asks only for `load`, `mutable_subpaths`, and
`mutation_points`, and the loaded harness only for
`run(entry, sinks, config) -> RunResult` — nothing in it mentions agents. A
workspace declares a non-ADK harness with `adapter.kind = "import"`, which
imports an operator-supplied `module:callable` factory. Shipped concrete
adapters are ADK-only so far; LangChain and plain-callable adapters land
after it.

The worked example is `examples/zicato_examples/target_0_convergence`: a
deterministic policy adapter with **no LLM anywhere**, whose mutable surface
is a module-level string constant, driven through `kind = "import"`. The
whole loop — propose, apply, run, reduce, gate — runs against it in CI.

**goldfive is an optional extra** — `pip install zicato[goldfive]`. The core
import surface is goldfive-free: `import zicato`, `import zicato.core`,
board load/save, and the CLI all work without it, because
`src/zicato/core/drift_kinds.py` carries a string mirror of goldfive's
`DriftKind` / `DriftSeverity` vocabulary rather than importing the upstream
enums. What you lose without the extra: the ADK adapter path, the built-in
and custom in-run process judges, and the default `goldfive` telemetry
dialect. (`tests/test_no_goldfive_import.py` proves the property against an
interpreter that cannot import goldfive, so it does not rot.)

The base install keeps the evolve loop and canonical JSONL telemetry while
leaving operator interfaces optional. Install `zicato[observability]` for the
dashboard, builder route, terminal renderer, and live execution telemetry, or
`zicato[all]` for every shipped runtime feature. See
[`INSTALL-PROFILES.md`](docs/design/INSTALL-PROFILES.md) for the smaller
interface-specific profiles and degraded behavior.

That is an install-time fact, not a constraint on your target. Which
telemetry zicato consumes is chosen per epoch by `scoring.json`'s
`telemetry_dialect`: the default `goldfive` dialect is the only one that
yields drift kinds and plan revisions, while `adk_events` and `transcript`
read a harness that never runs under goldfive at all. Under `transcript`
the drift term is structurally zero, the drift knobs go inert (zicato warns
rather than failing), and scoring falls back to predicates plus optional
in-run judges.

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
- [`docs/design/MUTATION-SURFACE.md`](docs/design/MUTATION-SURFACE.md) — annotated mutation points: span, region, and file markers in Python and in any allowlisted text file, AST resolution, the `MutationPoint` shape, validator constraints, the `zicato inspect mutations` audit CLI.
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
- [`docs/design/CONVERSATION-EXECUTION.md`](docs/design/CONVERSATION-EXECUTION.md) — the inline execution outline beneath conversation turns: explicit agent branches, turn-scoped tool observations, fidelity rules, live digest behavior, and the boundary with the full Harmonograf trace.
- [`docs/design/TUI.md`](docs/design/TUI.md) — `zicato tui`, the Console in the terminal: a read-only, keyboard-driven review surface over the *same* served payloads the browser dashboard renders (a second renderer, never a second brain — enforced by an import contract and a shared render cross-pin against `ui.js`), the lens set (Home / Standings / Instrument built; Candidate / Board / Health designed and deferred), glyph microtypography (braille sparklines, shared-scale CI whiskers, the round lifeline), the two-gate repaint discipline (the SSE `seq` cursor outside, a content digest inside), the four-absence vocabulary, and the explicit render-conformance list of what defers and what stays in the browser.
- [`docs/design/PROPOSER.md`](docs/design/PROPOSER.md) — the proposer as a first-class contract input: the default tool-using ADK agent (skill-composed is the explicit opt-in), the read-only proposer tool registry, the board-anonymized train-slice-only failure-mode feedback channel (`outcome_summarizer_spec`), and why a proposer/skills change rolls the epoch.
- [`docs/design/ROBUSTNESS.md`](docs/design/ROBUSTNESS.md) — the six-layer defense model (asyncio timeouts → cancellation → subprocess workers → watchdog → circuit breaker → atomic writes), what each layer catches, failure-mode tables, the GIL discussion that makes subprocess isolation non-negotiable, phasing.
- [`docs/design/LOOP-HEALTH.md`](docs/design/LOOP-HEALTH.md) — loop-health diagnostics: detecting a running-but-meaningless loop (a degenerate, toothless evaluation), the five detectors and severities, the `LoopHealth` report, the `zicato health` CLI, and how the orchestrator surfaces critical findings.
- [`docs/design/STORAGE.md`](docs/design/STORAGE.md) — the pluggable `StorageBackend` (file + memory backends) and the `GenerationStore` protocol with both directory and git backends shipping; the v0 directory-snapshot layout; the three-storage-concerns split; and the still-roadmap operator git CLI (`zicato repo` / `log` / `diff` / `show` / `bisect` / `blame`, `workspace migrate-to-git`).
- [`docs/design/ANALYTICAL-INDEX.md`](docs/design/ANALYTICAL-INDEX.md) — the `.zicato/index.db` SQLite analytical index: why cross-run views are queries not file-walks, the files-canonical / index-derived discipline, `zicato repair index`, and the eleven-table schema (SCHEMA_VERSION 12, incl. the visibility-rating `generations.elo*` columns).
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
