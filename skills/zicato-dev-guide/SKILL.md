---
name: zicato-dev-guide
description: A developer's navigational map of the zicato codebase — the high-level design goals, where every concern lives (orchestrator, selection strategies, tournament runner + subprocess worker, runtime state, storage, telemetry, epoch, the Variant-T dashboard), the data/control flow for an evolve run and for the live dashboard, the sharp edges that cost real time (subprocess weights-transport gap, the four distinct timeout mechanisms, dashboard CSS/cache pitfalls, pre-commit no-op), and how to build/test/verify locally. Use when you are CHANGING zicato itself (not operating a workspace) and need to find the right file fast or avoid a known trap. The operator workflows live in the other skills; this is the contributor's orientation.
---

# zicato developer guide

This is the contributor's map — for someone editing zicato's *own* code, not
operating a workspace. The operator skills (`zicato-evolve`,
`zicato-design-boards`, …) teach how to *run* the loop; this teaches where the
loop *lives* and where the bodies are buried. Start with
[`../../AGENTS.md`](../../AGENTS.md) (the hard rules) and
[`docs/design/ARCHITECTURE.md`](../../docs/design/ARCHITECTURE.md) (the full
picture); this skill is the fast index over both, kept honest against the code.

**When docs and code disagree, the code wins** — and this guide flags the
disagreements it found. `docs/design/CLI.md` is a *generated* doc and drifts;
trust `zicato --help` over it. The exit-code table in `AGENTS.md`/`CLI.md`
(6=reject, 7=wall-clock, 9=degeneracy) is not literally implemented in the CLI
commands today — they raise Click exceptions (exit 1); `health` is the one that
calls `raise SystemExit(1)` (`cli/commands/health.py:241`). Don't script on the
fine-grained codes without re-checking.

## Design goals (what the architecture is *for*)

- **Evolve an agent against a contract.** The hierarchy is epoch ⊃ generations
  ⊃ runs, with a *round* = one propose→apply→tournament→promote/reject cycle.
  An **epoch is a sealed evaluation contract** (board + proposer brief +
  scoring + inner-harness identity + proposer); editing any of those rolls a
  fresh epoch via the contract hash (`epoch/contract.py`,
  `compute_contract_hash`). The proposer dir is itself part of the hash
  (`contract.py:66-84`) — editing a proposer skill rolls the epoch.
- **Tournament structure is configurable per epoch.** A `SelectionStrategy`
  owns scheduling/bracket/advance/stopping for ONE epoch's structure; a thin
  driver loop steps it until `resolved()`. Gauntlet is the default;
  single/double-elim, swiss, and racing (successive halving) are registered
  alongside (`selection/registry.py`). The **promote gate is unchanged across
  every structure** — the strategy reads the gate verdict, never re-decides a
  duel (`selection/strategy.py:9-15`).
- **Scoring is a weighted, drift-derived scalar (lower=better), pluggable at
  two seams (#19).** The built-in formula is
  `scalar = drift_weight·drift_loss_mean + pass_weight·(1−mean_score)`, but the
  LIVE path is NOT `telemetry/scoring.py::combined_scalar` (that helper is now
  **test-only** parity). The live scalar runs through the seam architecture in
  `zicato/scoring/` (`api` typed contexts · `builtins` extracted formulas ·
  `transforms` registry · `plugins` dotted-spec · `dispatch` the single seam):
  - **Seam 1 — per-run drift loss:** `telemetry/reducer.py compute_drift_loss`
    builds a `DriftContext` and calls `scoring/dispatch.py::resolve_drift_loss`
    (declarative `drift_kind_aggregation` + dotted `drift_reducer`). Runs in the
    killable worker.
  - **Seam 2 — per-gen scalar:** `tournament/scoring.py::aggregate_generation_score`
    builds a `ScalarContext` and calls `scoring/dispatch.py::resolve_scalar`
    (declarative `pass_transform` + dotted `scalar_fn`). Runs in the orchestrator.
  Each composes **built-in → transform → plugin**, plugins fail-open to the
  built-in (logged + recorded in `scoring_provenance`), and a plugin BODY edit
  is source-hashed into the contract (rolls the epoch). **Scoring hazard:**
  `drift_loss_mean` is *unbounded* while `(1−mean_score)` is in [0,1], so with
  the shipped equal defaults a noisy/large drift term can swamp the pass term
  and invert the ranking — the exact red flag `zicato-audit-board` hunts for.
  Per-judge weighting (`per_judge_weights`/`default_judge_weight`) folds into
  `drift_loss` inside the reducer (`telemetry/reducer.py compute_drift_loss`,
  `telemetry/scoring.py per_judge_loss`).
- **The proposer is a first-class contract input.** The DEFAULT (no proposer
  dir) is a **tool-using ADK agent** in `builtin_default` mode — it owns the
  full read-only tool registry and runs on ADK's own `Runner`, bound to the
  auxiliary model (`proposer/agent.py build_proposer_agent`). The two opt-in
  customizations: a **skill-composed text-shim** (drop `skills/*.md`, no code —
  loses tools) or a **custom `agent.py`** owning its own `model=`. The selector
  is `build_proposer_agent` (`agent.py:173-239`), NOT `adk_agent.py`'s module
  docstring (which narrates the historical text-shim-default and is stale). A
  **board-anonymized, train-slice-only, bucketed failure-mode feedback channel**
  (`render_failure_mode_profile`, `orchestrator.py:3756-3779`) feeds the proposer
  what fails without leaking entry identity, extensible via the
  `outcome_summarizer_spec` operator hook. See `docs/design/PROPOSER.md`.
- **Telemetry: the per-run `events.jsonl` is the source of truth.** zicato does
  not invent a wire format — it captures goldfive's `Event` stream verbatim and
  reduces it post-run to a typed `LossProfile` (`loss.json`). The SQLite
  `index.db` is a *derived* projection and lags. (Details:
  `zicato-read-telemetry`.)
- **Storage is a pluggable `StorageBackend`** (`storage/`): `files` (canonical,
  default) + `memory` (tests); a `git` backend is roadmap
  (`storage/factory.py`, `DEFAULT_BACKEND = "files"`).
- **The dashboard is a separate concern.** The live UI is "Variant T"
  (internally; user-facing "Console"), served static from disk with **no
  bundle/build step** — `app_T.js` injects the stylesheet via `import.meta.url`
  (see below). The home view carries a **cross-epoch meta-loop ledger**
  (`views/home.js` → `svg.metaLoopLedgerDigest`); **settings is a routed
  right-side DRAWER overlay**, not a full page (`shell.js:664-670`), and its
  Contract tab reuses the builder's live preview; the **tournament builder is
  its own first-class view** (`#/builder`, `views/builder.js`, + a settings
  launcher + a `zicato builder` CLI command, `cli/commands/builder.py`). One
  shared resolver `resolveNonGauntletSt` (`views/structure.js:404`) unifies the
  racing/non-gauntlet model across epoch/gens/candidate views (convergence +
  digest-gating preserved).
- **goldfive + harmonograf are external pinned-git deps**, not vendored
  (`pyproject.toml [tool.uv.sources]`: goldfive at a pinned rev,
  harmonograf-client/server from the harmonograf monorepo). **Never edit
  goldfive from a zicato change** — the scoring reducer lives *in* zicato
  (`telemetry/reducer.py`); the runtime/steerer lives in goldfive.

## Map of the codebase (`src/zicato/`)

| Path | What it owns |
|---|---|
| `orchestrator.py` (~4.5k lines) | The meta-loop: `evolve_once`, `evolve_n_rounds`, field request, the `resolve_tournament` wiring, and **all** `active_tournament` publish/settle/clear methods (`_publish_active_tournament`, `_settle_active_tournament`, `_publish_live_structure`). The total-evolve wall-clock budget lives here too. |
| `selection/strategy.py` | The `SelectionStrategy` ABC + value types (`Contestant`, `Matchup`, `MatchupResult`, `SelectionDecision`, `RoundRecord`/`MatchRecord`, `Standing`). The live-projection hooks (`live_rounds`/`live_standings`/`_pending_round`) that make the in-flight and settled envelopes byte-compatible. |
| `selection/driver.py` | `resolve_tournament` — the thin async loop: `request_field` → `seed` → (`next_matchups`→`gather(run_matchup)`→`record_result`)* → `champion()`. `on_progress` fires after each batch is scheduled (the live-publish hook). |
| `selection/strategies/*.py` | The five concrete structures: `gauntlet`, `single_elim`, `double_elim`, `swiss`, `racing`. `field_size==1` degrades any structure to gauntlet semantics (`registry.py`). |
| `tournament/runner.py` | Runs one duel: spawns a subprocess **worker per board-run**, escalates SIGTERM→SIGKILL on overrun, aggregates, ends in the unchanged `evaluate_gate`. Serialises the run via `_weights_spec`/`_entry_to_dict`/`_role_worker_spec`. |
| `_tournament_worker.py` | The L3 subprocess that executes ONE run in its own OS process. Writes its OWN pid to `active_runs/{run_id}.json`, drives the entry under goldfive, reduces loss, writes `loss.json` + a result file. Deliberately killable. |
| `tournament/gate.py` | `evaluate_gate` — the per-duel accept/reject (the `promote_margin` band + pass-rate/namespace monotonicity + holdout). Structure-independent. |
| `tournament/ladder.py`, `regression.py`, `detail.py`, `scoring.py` | Thresholdout ladder (overfitting), optional regression-test gate, forensics detail, and `scoring.py::aggregate_generation_score` — the LIVE Seam-2 entry (builds a `ScalarContext`, calls `scoring/dispatch.py::resolve_scalar`). |
| `scoring/` (#19) | The pluggable-scoring seam package: `api` (frozen `DriftContext`/`ScalarContext` + provenance token) · `builtins` (extracted default formulas) · `transforms` (declarative registry + fail-fast validation) · `plugins` (dotted-spec resolution, source-hashing, fail-open) · `dispatch` (`resolve_drift_loss`/`resolve_scalar`, composes built-in→transform→plugin). |
| `telemetry/reducer.py` | `reduce_loss` + `compute_drift_loss` — the **one** place with both drift counts and weights; the LIVE Seam-1 entry (builds a `DriftContext`, calls `scoring/dispatch.py::resolve_drift_loss`). `telemetry/scoring.py::combined_scalar` is a **test-only** parity reference, NOT the live path. |
| `telemetry/sink.py`, `terminal_event.py`, `meta_loop.py`, `harmonograf_supervisor.py` | JSONL + harmonograf sinks, the terminal-frame invariant, the meta-loop session emitter, the auto-launched harmonograf server. |
| `runtime/state.py` + `paths.py` | The control-file protocol: `heartbeat.json`, `active_runs/{run_id}.json`, `active_tournament.json`, `dashboard.json`, `lock.json`. **Every write is atomic** (`.tmp`+fsync+`os.replace`, `runtime/_atomic.py`); readers tolerate missing files. |
| `runtime/heartbeat.py`, `control.py`, `lock.py` | The orchestrator/run heartbeat beaters, the dashboard control-command channel, the exclusive workspace lock. |
| `epoch/` | Contract + hash (`contract.py`), lifecycle/auto-epoch, journal, lineage, analysis/html report, generation store (`genstore.py` + `git_genstore.py`), snapshot scope. |
| `storage/` | `StorageBackend` base + `files`/`memory` backends + `factory.py`. |
| `dashboard/` | The standalone Starlette service (`server.py`, `sse.py`, `state_reader.py`, `endpoints.py`) + the static UI (see next section). Static is served `no-cache` **plus an ETag** so 304-revalidation avoids re-downloads without ever serving stale bytes (`server.py:165-180`). |
| `builder/` | The tournament-builder backend — the draft-contract operations + the chat copilot's tool surface (`operations.py`, `copilot.py`, `copilot_tools.py`). The cost estimator uses **per-structure default replicates** (swiss/elim=2, gauntlet/racing=1) from `selection/registry.py STRUCTURE_DEFAULT_REPLICATES` (the single source of truth, derived from each strategy's `_default_replicates`), mirrored in `builder/preview` JS. |
| `cli/commands/*.py` | Click subcommands, auto-discovered; entry point `zicato.cli:main`. Includes `builder` (launch the standalone tournament builder). |
| `crates/supervisor/` | The Rust watchdog (heartbeat staleness + run staleness + per-run deadline kill). |

## Control + data flow: one evolve round

1. `evolve_n_rounds` (orchestrator) pins the epoch (contract-hash auto-epoch
   runs ONCE before the loop), then drives each round under the optional
   total wall-clock budget.
2. Per round: `resolve_tournament(strategy, request_field, run_matchup,
   on_progress)` (`selection/driver.py`).
   - `request_field(n)` resolves the champion + applies `n` challenger
     experiments into fresh snapshots.
   - `strategy.seed(...)` → loop of `next_matchups()` → `gather(run_matchup)`
     → `record_result(...)` until `resolved()`.
   - Each `run_matchup` runs the duel via `tournament/runner.py`, which spawns
     a **subprocess worker per board-run**, then ends in `evaluate_gate`. The
     strategy interprets the gate verdict; it never re-runs the gate.
3. `strategy.champion()` returns the `SelectionDecision`; the orchestrator
   promotes (or stands the champion) and writes the journal/lineage.

**Parallelism:** `parallelism` = how many board *units* run at once, NOT how
many subprocesses — full mode runs the parent + child of a unit concurrently,
so P units ⇒ up to ~2·P worker processes alive (`tournament/runner.py:13-43`).

## Control + data flow: the live dashboard

The convergence guarantee is that the **settled** render via the recorded path
must be byte-identical to the **live** render via the streaming path. The chain:

```
SelectionStrategy.live_rounds()/live_standings()   (selection/strategy.py)
  → orchestrator publish methods (_publish_active_tournament / _publish_live_structure)
  → runtime/state.py atomic write → .zicato/runtime/active_tournament.json
  → dashboard SSE (dashboard/sse.py watches the file)
  → js/core/state.js (debounced) → digest-gated render
  → js/variants/T/views/structure.js resolveNonGauntletSt   (the ONE shared
       racing/non-gauntlet model resolver, used by epoch/gens/candidate)
  → js/variants/T/live.js buildLiveModel / normalizeStructure
  → js/variants/T/svg.js figure builders
```

`on_progress` fires right after a batch is scheduled (the pending matchups
carry `winner=""`/`pending=True`), so the bracket/funnel/ladder exists live
with `winner: null` before the long matchup runs (`driver.py:46-88`,
`strategy.py:329-390`). The publish path is **best-effort** — a publish failure
must never abort a resolution.

## Sharp edges (verified against the code, with corrections)

- **Subprocess weights-transport symmetry — the canonical "silent default"
  trap.** `_weights_spec` (`tournament/runner.py`) and `_weights_from_args`
  (`_tournament_worker.py`) MUST stay symmetric: a `ScoringWeights` field
  present in-process but missing from BOTH the spec and the from-args side is
  silently reconstructed at its dataclass default in the worker — no error, just
  wrong scoring. This bit twice: `per_judge_weights`/`default_judge_weight` were
  dropped (so per-judge weighting silently fell back to `1.0`), and later
  `pass_rate_monotonicity_scope` would have desynced the worker-side gate the
  same way. BOTH are now carried on both sides and guarded by round-trip tests
  in `tests/test_subprocess_workers.py`
  (`test_per_judge_weights_survive_worker_serialize_deserialize`,
  `test_pass_rate_monotonicity_scope_survives_worker_serialize_deserialize`).
  Pattern to internalize: when you add a `ScoringWeights` field, add it to BOTH
  transport sides AND a worker serialize→JSON→deserialize round-trip test — the
  in-process reducer tests will pass while the production subprocess path is
  silently wrong.
- **Five distinct timeout mechanisms — know which kills a process vs cancels in-process.**
  1. *Per-board wall-clock budget* — the worker's own cooperative
     `asyncio.wait_for(timeout=entry.wall_clock_budget_seconds)`
     (`_tournament_worker.py:590`); the parent runner wraps it in a
     `wait_for(budget+GRACE)` and escalates SIGTERM→(5s)→SIGKILL
     (`runner.py:_PARENT_BUDGET_GRACE_S=30`, `_SIGTERM_TO_SIGKILL_GRACE_S=5`).
  2. *The Rust supervisor* — **not heartbeat-only.** It has THREE triggers:
     heartbeat staleness (kills the *orchestrator*), run staleness
     (`last_progress` not advancing; a far backstop ≈ 2× budget), AND a
     **per-run wall-clock deadline kill** (`decide_run_deadline`, the *primary*
     run-kill trigger, on by default, `--run-deadline-kill-disabled` to off)
     — `crates/supervisor/src/watchdog.rs:1-53`. **Correction:** the supervisor
     DOES kill a slow run once it passes its deadline; it is not "staleness-
     only" and does not require the run to be wedged.
  3. *The aux/judge call budget* — `aux_call_timeout_s` wraps each aux-LLM
     call in `asyncio.wait_for` in-process; no process kill (`aux_timeout.py`).
  4. *The total-evolve wall-clock budget* — `max_wall_clock_seconds` in
     `evolve_n_rounds` stops cleanly **between rounds** and cancels a round
     **within** it via Layer-1 `asyncio.wait_for` (cooperative only — a wedged
     blocking/CPU round needs the L3 worker layer)
     (`orchestrator.py:2982-3005`). There is a per-evolve total cap + a
     within-round cancel here.
  5. *Per-matchup / final-rung wall-clock budget (racing)* — opt-in
     `matchup_budget_seconds` (all rungs) and `final_rung_budget_seconds` (the
     final crowning duel) stamped onto `Matchup` and enforced in `run_matchup`:
     once a duel's wall-clock is spent it stops launching board units and
     records the rest as budget-exceeded (partial aggregate). Default unset =
     uncapped (byte-identical to before). This closes the gap where a racing
     FINAL — full board × replicates × both sides, each board only bounded by
     its own per-board budget — could grind for hours with no aggregate cap.
- **`uv sync --all-extras` ALWAYS.** A bare `uv sync` prunes the dev tooling
  (pytest, mypy, ruff, even uv) from `.venv` — the dev deps live behind extras
  (`pyproject.toml [project.optional-dependencies] dev`). Use `make install`.
- **The model provider (litellm) is transitive, not direct.** Nothing in
  zicato (pyproject, uv.lock, or src) references `litellm` — model-spec roles
  build ADK's `LiteLlm` (`models_config.py:293-307`), which pulls `litellm`
  transitively via the `adk` extra (`google-adk`). A bare `uv run`/`uv sync`
  that drops extras can leave a live run unable to reach the provider; run live
  work against the already-synced env (`.venv/bin/python -m zicato …`) after
  `uv sync --all-extras`.
- **Pre-commit no-op on the first commit.** The `ruff-format` hook
  (`.pre-commit-config.yaml`) reformats staged files and aborts the commit, so
  the FIRST `git commit` can be a silent no-op (a following `git merge` then
  says "Already up to date"). Remedy: `git add -A && git commit` a second time.
- **Dashboard: no CSS bundle/rebuild step.** `app_T.js` injects
  `css/variants/T/console4.css` via
  `new URL('./css/variants/T/console4.css', import.meta.url).href`
  (`static/app_T.js:35-41`). Edit the CSS file in place; there is nothing to
  build. Everything visual lives under `js/variants/T/**` + `css/variants/T/**`.
- **Dashboard: verifying a CSS change needs a full document reload.** The SPA
  injects the stylesheet once on document load; hash-route navigation does NOT
  re-inject it, so you see stale CSS. Static is served `Cache-Control: no-cache`
  **plus an ETag** (`"{mtime_ns:x}-{size:x}"`) so a revalidation returns a
  bodyless 304 when the file is unchanged but the moment a file is edited its
  ETag flips and the browser re-downloads — `no-cache` is deliberately kept (a
  plain long-lived cache would serve stale CSS/JS), the ETag is the no-redownload
  optimization (`dashboard/server.py:140,165-180`). That only bites on a fresh
  document load / fresh dashboard process — hard-reload the page to verify a CSS
  edit.
- **Dashboard CSS sizing: only max-WIDTH is shear-safe.** Hero figures are
  aspect-locked (inline `aspect-ratio` == viewBox) with
  `preserveAspectRatio:'none'`. A `max-height` clamps height while width follows
  the aspect → it shears the SVG; cap with `max-width` only
  (`css/variants/T/console4.css`). **And specificity bites:** the containment
  rule `.dn-figpane > svg { max-width:100% }` carries an svg type (specificity
  1,2,1) and out-ranks a bare `.dn-*-hero` cap (1,2,0), which silently clobbers
  the cap to 100% of the panel and balloons the figure (this is what oversized
  the racing funnel on `/gens`). The fix: hero caps MUST be written
  `svg.dn-*-hero { max-width: … }` so the type qualifier ties the specificity
  and wins on source order (commit `d30ec4e`). Keep the `svg.` prefix when
  adding any new hero cap.
- **Dashboard render discipline: digest-gate, never repaint on a no-op beat.**
  Live surfaces are digest-gated on the live *content* so the DOM rebuilds only
  on a real change, never on a no-op SSE heartbeat — the recurring
  flashing/refresh bug class (`js/variants/T/data.js` `liveDataSignature` +
  the per-figure digests in `live.js`/`views/structure.js`).
  Preserve this when adding any SSE-driven view.
- **`active_tournament.json` is read-modify-write from two writers.** The
  orchestrator's full-envelope republish PRESERVES the runner-written live
  fields across a republish (`orchestrator.py:_publish_active_tournament`,
  ~L2247) — do not clobber them.
- **Process hygiene.** Killing dashboards/workers with a broad
  `pkill -f zicato…` can match the caller's own shell (self-kill, exit 144).
  Kill by explicit PID (the worker stamps its own pid into
  `active_runs/{run_id}.json`) or a tight pattern. Stale dashboards drift ports
  (7892→7893→…); read the actually-bound port from
  `.zicato/runtime/dashboard.json`.

## Build, test, verify locally

```sh
make install        # uv sync --all-extras  (NEVER bare `uv sync`)
make check          # ruff check + mypy src/zicato/ + pytest
make test           # uv run pytest tests/
uv run pytest tests/test_<area>.py -q     # one area
```

Dashboard JS tests run under `node` (skipped if node is absent), driven by
`tests/test_dashboard_js.py` → `static/test/run-all.mjs`:

```sh
node src/zicato/dashboard/static/test/run-all.mjs; echo "exit=$?"
```

**Test-runner footgun (documented in the runner itself):** each `*.test.mjs`
file prints its own "X passed, Y failed" line, so the final tail line is just
the LAST file's count — a green tail can hide a failing file. **Trust the
EXIT CODE** (and the honest `TOTAL:` line the runner now prints at the end)
(`run-all.mjs:8-13`).

Rust supervisor:

```sh
make supervisor        # cargo build --release -p zicato-supervisor
make supervisor-check   # cargo fmt --check + clippy -D warnings + cargo test
```

**Verify changes with tests + the deterministic mock target**
(`examples/zicato_examples/target_1_presentation`), **never a live run** —
gating live `evolve` runs is a hard rule (`AGENTS.md`). The filesystem under
`.zicato/` is canonical; `index.db` is derived (`zicato reindex` to rebuild).

## Cross-links

- Run the loop / operate a workspace → `zicato-evolve`,
  `zicato-manage-epochs-and-rounds`.
- Trace a run's telemetry / harmonograf sessions → `zicato-read-telemetry`.
- Trust a board / scalar before believing a verdict → `zicato-audit-board`,
  `zicato-tune-scoring`, `zicato-diagnose-health`.
- Choose/configure a tournament structure → `zicato-design-tournament-structure`,
  `zicato-configure-tournament`.
- Read/drive the live dashboard → `zicato-watch-dashboard`.
- The mutable surface the proposer may edit → `zicato-mutation-audit`.
