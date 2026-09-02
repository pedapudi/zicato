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
agent-specific. The contract asks for three things: an **adapter** that the
runner can reconstruct, one or more **mutable trees** of source the proposer may
edit, and a **board** of tasks with typed expectations. Anything that fits that
shape can be the target — a library, a prompt set, or a rule engine. The adapter
driver may sit outside every mutable tree, which lets the driver remain fixed
while Zicato evolves its dependency.

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
break. The first reference adapter targets Google ADK. The design is
**framework-agnostic at its core**:
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

**Goldfive is an optional integration.** `pip install zicato[goldfive]`
installs its event runtime for any harness adapter that declares the
capability. An optional `scoring.json` object carries Goldfive configuration as
ordinary JSON; Goldfive's public configuration-document API owns its schema,
defaults, validation, capability checks, and runtime construction. Zicato
loads that API only for a Goldfive-enabled contract.

The core import surface remains Goldfive-free: `import zicato`, `import
zicato.core`, board load/save, and the CLI all work without it.
`src/zicato/core/drift_kinds.py` carries a string mirror of Goldfive's
`DriftKind` and `DriftSeverity` vocabulary, so core code need not import the
upstream enums. Without the extra, an adapter cannot use Goldfive's runtime or
in-run process judges. The built-in Google ADK adapter is a separate
composition: install `zicato[adk]` to get both ADK and its Goldfive
capabilities. `tests/test_no_goldfive_import.py` verifies this import boundary
against an interpreter that cannot import Goldfive.

The base install keeps the evolve loop and canonical JSONL telemetry while
leaving operator interfaces optional. Install `zicato[observability]` for the
dashboard, builder route, terminal renderer, and live execution telemetry, or
`zicato[all]` for every shipped runtime feature. See
[`INSTALL-PROFILES.md`](docs/design/INSTALL-PROFILES.md) for the smaller
interface-specific profiles and degraded behavior.

That is an install-time fact and places no constraint on your target. Which
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

## How the hidden holdout protects promotion

Repeated optimization can overfit a fixed task board even when every score is
measured correctly. Each proposed edit depends on results from earlier edits,
so the board gradually becomes training data for the improvement loop.

zicato limits that feedback with a hidden holdout slice. Tournament selection
and the ordinary promotion gate use the training slice. When a challenger would
become champion, zicato compares the champion and challenger on the holdout.
The challenger confirms when it does not meaningfully regress there. Holdout
confirmation does not require a second improvement; it checks that the
training improvement generalizes.

Each final comparison against the hidden slice is an **adaptive holdout
query**. The word *query* comes from statistics: the optimization process asks
the hidden data one question about a candidate chosen using previous results.
One holdout comparison counts as one query even when it runs several board
entries and makes several model or tool calls. Model calls, tokens, wall-clock
limits, and database operations have separate accounting.

The **Ladder** governor limits what repeated queries reveal. It releases a new
confirmation result only when the training improvement clears its threshold,
and it charges each holdout consultation against an epoch-level query budget.
A withheld result still consumes one query because zicato inspected the hidden
data. When the budget is exhausted, no further holdout result affects the
decision; the training verdict stands.

The default budget is an operational limit rather than a universal statistical
calibration. Board size, noise, feedback visibility, and reuse across epochs
still determine how much confidence the holdout supports. See
[`OVERFITTING.md`](docs/design/OVERFITTING.md#what-query-budget-means) for the
accounting rules, configuration guidance, and known enforcement limitation.

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
- [`docs/design/GOLDFIVE-CONFIG.md`](docs/design/GOLDFIVE-CONFIG.md) — how an adapter opts into Goldfive, how Zicato delegates schema and runtime construction to Goldfive, and how named credential variables cross the worker boundary without entering contract files.
- [`docs/design/OVERFITTING.md`](docs/design/OVERFITTING.md) — why repeated adaptive evaluation overfits a fixed board, how the train/holdout split works, what one holdout query means, and how the Ladder query budget limits feedback from the hidden slice.
- [`docs/design/TOURNAMENT.md`](docs/design/TOURNAMENT.md) — the competition model: the king-of-the-hill gauntlet (champion vs successive challengers), the dashboard Tournament view (bracket + per-matchup detail), the tournament-detail analytics (verdict transparency, per-entry A/B grid, hypothesis ledger, optimization trajectory, mutation heat map, cost), and the harmonograf split — execution view vs competition view.
- [`docs/design/SELECTION.md`](docs/design/SELECTION.md) — the decision theory under the tournament: how reinforcement-learning gating, racing, and bracket schedulers make the champion-versus-challenger decision; why zicato's gauntlet is a degenerate elitist iterated race; why brackets (single and double elimination, Swiss) are the wrong primitive here; and the ordered path to replication-based racing (a paired significance gate, winner's-curse confirmation, a trust-region step bound). Diagrams and cited sources.
- [`docs/design/EMULATOR.md`](docs/design/EMULATOR.md) — the multi-turn user emulator: the two-callable rule (hard error on identity match), sealed context construction, answer-leak heuristic, audit-trail spans.
- [`docs/design/DOGFOOD-TARGETS.md`](docs/design/DOGFOOD-TARGETS.md) — the three targets zicato is aimed at in order (a presentation agent, then goldfive's steering layer, then zicato itself) and the design commitments each one forces before it can be attempted.
- [`docs/design/RUNTIME.md`](docs/design/RUNTIME.md) — `.zicato/runtime/` state file layout, the two processes `zicato evolve` auto-spawns (a Rust watchdog supervisor on :7920 and a separate Python dashboard service on :7892), heartbeat protocol, signal escalation, single-writer concurrency model.
- [`docs/design/DASHBOARD.md`](docs/design/DASHBOARD.md) — the live console for an in-flight epoch: Starlette HTTP + SSE architecture, the home view's cross-epoch meta-loop ledger, the live racing hero (full-width scalar track + rung stepper, champion-gate rows, WHAT'S RUNNING / LIVE ACTIVITY), the first-class tournament **Builder** view (`#/builder`) and the routed Settings drawer (Contract tab reuses the builder's live preview), per-entry continuous score + precision/recall, the full GET API surface, and the control-file protocol for operator actions.
- [`docs/design/CONVERSATION-EXECUTION.md`](docs/design/CONVERSATION-EXECUTION.md) — the inline execution outline beneath conversation turns: explicit agent branches, turn-scoped tool observations, fidelity rules, live digest behavior, and the boundary with the full Harmonograf trace.
- [`docs/design/TUI.md`](docs/design/TUI.md) — `zicato tui`, the Console in the terminal: a read-only, keyboard-driven review surface over the *same* served payloads the browser dashboard renders (a second renderer over the server's decisions, which computes none of its own — enforced by an import contract and a shared render cross-pin against `ui.js`), the lens set (Home / Standings / Instrument built; Candidate / Board / Health designed and deferred), glyph microtypography (braille sparklines, shared-scale CI whiskers, the round lifeline), the two-gate repaint discipline (the SSE `seq` cursor outside, a content digest inside), the four-absence vocabulary, and the explicit render-conformance list of what defers and what stays in the browser.
- [`docs/design/PROPOSER.md`](docs/design/PROPOSER.md) — the proposer as a first-class contract input: the default tool-using ADK agent (skill-composed is the explicit opt-in), the read-only proposer tool registry, the board-anonymized train-slice-only failure-mode feedback channel (`outcome_summarizer_spec`), and why a proposer/skills change rolls the epoch.
- [`docs/design/ROBUSTNESS.md`](docs/design/ROBUSTNESS.md) — the six-layer defense model (per-call timeouts → structured cancellation → the subprocess worker boundary → the orchestrator watchdog → the consecutive-bad circuit breaker → atomic writes plus resume markers), what each layer catches, failure-mode tables, and the GIL discussion that makes subprocess isolation load-bearing.
- [`docs/design/LOOP-HEALTH.md`](docs/design/LOOP-HEALTH.md) — loop-health diagnostics: detecting a running-but-meaningless loop (a degenerate, toothless evaluation), the five detectors and severities, the `LoopHealth` report, the `zicato health` CLI, and how the orchestrator surfaces critical findings.
- [`docs/design/STORAGE.md`](docs/design/STORAGE.md) — the pluggable `StorageBackend` (file and memory backends) and the `GenerationStore` protocol with both directory and git backends shipping; the directory-snapshot layout; the three-storage-concerns split; and the operator git CLI that remains on the roadmap (`zicato repo` / `log` / `diff` / `show` / `bisect` / `blame`, `workspace migrate-to-git`).
- [`docs/design/ANALYTICAL-INDEX.md`](docs/design/ANALYTICAL-INDEX.md) — the `.zicato/index.db` SQLite analytical index: why cross-run views are queries rather than file walks, the files-canonical and index-derived discipline, `zicato repair index`, and the fourteen-table schema (`SCHEMA_VERSION` 14, including the visibility-rating `generations.elo*` columns).
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
