# zicato reimplementation roadmap

> **Status: a plan.** This document holds a behavior-preserving refactoring
> roadmap for `src/zicato/` and the target data model and storage design that
> roadmap aims at. Sections that record work already built say so in place.
> For the system as it stands, read the development guide under
> `docs/dev-guide/`.
> Companion to [`ARCHITECTURE.md`](ARCHITECTURE.md), [`STORAGE.md`](STORAGE.md),
> and [`TOURNAMENT-DATA-MODEL.md`](TOURNAMENT-DATA-MODEL.md).

## Context

`src/zicato/` is ~73k LOC of Python across ~24 modules, backed by 165 test files.
It works and ships. Its complexity is concentrated in a handful of modules that
have accreted unrelated responsibilities (god objects) and in a thick layer of
accidental duplication, and the type checker is largely disabled by `Any`
signatures and `getattr`-over-`Any` access. The goal of this program is a
**significantly more understandable, modular, simpler, less error-prone**
implementation that **retains 100% of features with zero user-visible
difference**.

Strategy: a **staged in-place refactor** rather than a rewrite. Every step is
behavior-preserving and merges only when the full test suite + golden-output
parity checks pass. There is no long "red" period and parity is guaranteed at
every commit.

Scope: a staged roadmap and target design to pick from later. Nothing here is
implemented yet.

### The cross-cutting signal

Three separate audits — of the engine and lifecycle, of evaluation, and of
observability and delivery — each reached the same seven conclusions.

1. **Oversized modules** dominate complexity: `dashboard/state_reader.py` (5467),
   `orchestrator.py` (4622), `tournament/runner.py` (3292), `core/types.py`
   (2688), `index/ingest.py` (1753), `analyzer/report_figures.py` (1710),
   `analyzer/report.py` (1401), `telemetry/reducer.py` (1333),
   `adapters/adk.py` (1068).
2. **No shared "read canonical `.zicato/` → typed objects" layer.** dashboard,
   analyzer, telemetry and index each re-parse the same files (board.jsonl,
   experiment.json, loss.json, gen_score.json, events.jsonl). Root cause of most
   duplication and of the canonical-vs-derived disagreement risk.
3. **Pervasive micro-duplication**: drift-kind/severity normalization (7+
   copies), event-envelope unwrap (5×), `_now_iso` (5×), camel→snake (4×),
   LLM-JSON fence-strip (2×), dotted-path import (3×), atomic-write/JSON-read
   helpers (several), `_index_db_path` (2×), `_resolve_harmonograf_url` (2×).
4. **Type-safety holes**: stringly-typed dispatch + `getattr(x, "f", default)`
   over `Any` (33 `Any` in orchestrator alone); a renamed field fails at runtime
   rather than at type-check time.
5. **~40 blanket `except Exception  # noqa: BLE001`** swallow-everything blocks
   (29 in orchestrator) that hide systematic data-integrity drift.
6. **Two near-duplicate evolve pipelines** (gauntlet `evolve_once` vs
   `_evolve_multi_challenger`) with verbatim-duplicated mutation-id/forbidden-id
   checks and triplicated validation closures.
7. **Concrete dead code**: stale root `zicato/` pyc tree (138 files, 0 source),
   unwired 340-LOC control protocol, duplicate `telemetry/scoring.py`, an
   abandoned dashboard preview page, and shims that tolerate older on-disk key
   spellings.

---

## The parity harness (build first; gates every phase)

This is the safety net that makes a behavior-preserving refactor possible.
Build it in the first stage and run it at the end of every sub-step.

- **Unit/integration suite**: `uv run pytest -m "not node and not cascade_oc"`
  (xdist-parallel; both tiers, about seven minutes on twelve cores — a bare
  `uv run pytest` is the default tier alone, about a minute and three
  quarters). Must stay green at every commit. (Remember:
  `uv sync --all-extras`.)
- **Static gates**: `mypy` + `ruff` via pre-commit (`make install-hooks`). The
  refactor should *strengthen* mypy (fewer `Any`), so treat a new type error as
  a finding to act on rather than as noise to suppress.
- **Golden-output parity** against the deterministic mock target
  `examples/zicato_examples/target_1_presentation` (per AGENTS.md rule 1 — no
  live LLM runs). For a fixed seed, snapshot and byte/structurally diff before vs
  after each step:
  - `.zicato/**/loss.json`, `gen_score.json`, `experiment.json`, `lineage.json`
  - the SQLite `index.db` rows (export to a stable text dump; the index is a
    derived projection, so it must reproduce byte for byte after
    `zicato repair index`)
  - analyzer report markdown + HTML, `report_figures` SVG output
  - dashboard JSON envelopes (`/api/state`, `/api/epoch`, `/api/tournaments`,
    `/api/round/.../gate`, per-judge endpoints) captured from a fixture workspace
  - `zicato <command> --help` text for every command (the CLI is the contract)
- **Rule of thumb**: a step that changes any golden output has changed behavior
  by definition — investigate before merging.

---

## The safety net and zero-risk cruft removal

The first stage. Mechanical, independently shippable, no behavior change.

- Stand up the parity harness above; commit the golden baselines.
- Delete the stale root `zicato/` tree (138 `.pyc`-only files; can shadow the
  real `src/zicato/` in some tooling). All three audits flagged this.
- Delete dead code with no production consumer:
  - `telemetry/scoring.py::aggregate_generation_score` / `combined_scalar`
    (live path is `tournament/scoring.py`; the duplicate even has a divergent
    return type — a latent hazard).
  - **As landed:** the abandoned dashboard preview page and the per-variant
    directory indirection are both gone. The console layout is the only one
    shipped, so its modules sit directly under
    `src/zicato/dashboard/static/js/` and its stylesheet is
    `src/zicato/dashboard/static/css/console.css`.
  - no-op guard `state_reader.py:1450` (`isinstance(row.keys(), object)`);
    redundant duplicate `version`/`build` field (`endpoints.py:130,135`);
    intentionally-unused `weights` params (`tournament/scoring.py:117`,
    `telemetry/scoring.py:67`).
- Reconcile the stale `scoring/dispatch.py` "INERT/PHASE-2-HOOK" docstring with
  the code, which already calls the transforms/plugins.

**Decision point (carry to user at execution time):** the control-command
protocol `runtime/control.py` (~340 LOC, exported from `runtime/__init__.py`) has
**no production caller** — its docstring's "the orchestrator polls
`list_pending_commands`" is aspirational. Dashboard `POST /api/control/*`
endpoints exist but the loop never consumes them. Either **wire it** into the
round loop at safe points or **delete it**. (Recommend confirming with operator
before deleting, since the dashboard exposes the buttons.)

---

## Phase 1 — Shared foundations (dedup + the typed canonical-read layer)

The highest-leverage architectural work. Unblocks the god-object splits because
each split otherwise re-touches the same duplicated helpers.

**1a. `zicato/util/` — one home for the scattered primitives.** Replace every
copy with a single implementation:
- `time.py::now_iso` (replaces 5 copies: `orchestrator.py:3404`,
  `epoch/lifecycle.py:108`, `tournament/runner.py:358`, `dashboard/endpoints.py:75`,
  `dashboard/sse.py:317`)
- `llm_json.py::strip_fences` + `extract_json_object` (replaces
  `proposer/structured.py:220` and `board/rubric.py:56`; also used by the inline
  judge + rubric parsers)
- `dotted_path.py` — reuse the existing `zicato/import_path.py::import_dotted_path`
  for `judge_runtime/builder.py:289` and `synthetic/adversarial.py:62`
- `floats.py` — one finiteness/NaN/inf guard (replaces `_is_finite`,
  `_opt_score`, `_coerce_float`, …)
- camel→snake — one implementation (replaces `state_reader.to_snake:152`,
  `reducer._camel_to_snake:246`, `transcript._snake_deep:65`, `aggregator`)

**1b. `zicato/core/` event + drift normalization (single authority).**
- one `DriftKind`/`Severity` normalizer (replaces 7+ copies in `reducer`,
  `index/ingest`, `epoch/analysis`, `transcript`, `adapters/adk`,
  `synthetic/expectations`, `judge_runtime/disable`)
- one event-envelope unwrap `kind_and_payload()` (replaces 5 copies)
- one camel/snake-tolerant `iter_events(run)` reader (replaces the 4 readers in
  `reducer`, `index/ingest`, `state_reader._tail_events`, `transcript`)

**1c. `zicato/workspace/` — typed canonical-read layer.** The missing seam.
A package that owns reading the canonical `.zicato/` files into typed domain
objects: `read_board(epoch)`, `read_experiments(epoch)`, `read_loss(gen,entry)`,
`read_gen_score(...)`, `iter_events(run)`, `promoted_spine(generations)`, plus
the `WorkspaceLayout` path math currently re-implemented in `orchestrator.py`
(`_snapshot_root`, `_next_generation_id`, `_current_generation_marker`,
`_resolve_current_generation`). Consumers — dashboard, analyzer, telemetry,
index — switch to these typed readers.
- Make the index a **pure projection**: delete
  `index/ingest._drift_counts_from_events` and re-derive `metric_counts` from the
  reducer's canonical `loss.json` instead of independently re-tallying events.
  Removes the canonical-vs-derived disagreement hazard. Verify via byte-identical
  `reindex` of a fixture workspace.

**1d. Unify the storage seam.** Fold `epoch/_storage.py` + `runtime/_storage.py`
+ `runtime/_atomic.py` into the single `storage/` backend; one `atomic_write_text`
(replaces `orchestrator._atomic_write_text:4250`). Consolidate the duplicated
`_index_db_path` / `_resolve_harmonograf_url` into shared homes.

---

## Phase 2 — Types + error-handling hardening

Make the type checker do the work the comments currently do.

**2a. Split `core/types.py` (2688)** into `core/board.py` (BoardEntry +
validation + Expectation/JudgeSpec), `core/loss.py` (LossProfile/DriftCount/…),
`core/lineage.py` (Generation/Epoch/Experiment/Hypothesis/Outcome),
`core/scoring_config.py` (ScoringWeights), `core/runtime.py` (RuntimeConfig).
Re-export from `core/__init__` to keep import paths stable.
- Decompose the ~25-field `ScoringWeights` into nested typed groups
  (`DriftScoring`, `PassScoring`, `RegressionGate`, `NamespaceScoring`,
  `ScoringPlugins`). Make the **contract canonicalizer walk the nested
  structure** so tournament-structure and overfitting config need not live
  on `ScoringWeights` purely to fold into the contract hash.

**2b. Enums replace stringly-typed dispatch**: `TournamentDecision`
(promoted/rejected/deferred), `Side` (parent/child), `RunStatus`, `Phase`,
plus the `DriftKind`/`Severity` from 1b. Replace `getattr(x, "field", default)`
over `Any` (21× orchestrator, 9× runner, the `adapters/adk.py` `getattr` guards)
with direct typed access on the result objects that already exist
(`TournamentResult`, `SelectionDecision`).

**2c. One `best_effort(label)` context manager** replaces the ~40 hand-rolled
`except Exception  # noqa: BLE001` blocks, **preserving** the never-abort-the-round
behavior but adding a failure counter surfaced in loop-health, so a systematically
broken dashboard/index write becomes visible instead of silent.

---

## Phase 3 — God-object decompositions

Each is an independent, separately-gated sub-effort. Order them after Phase 1/2
so they land on shared utilities and typed objects. Public entry-point signatures
stay identical; internals move behind them.

**3a. `orchestrator.py` (4622) → `evolve/` package.**
- `evolve/round.py` — one `Round` pipeline (Propose→Apply→Validate→Score→Persist
  →Journal) shared by gauntlet and multi-challenger; the **scheduler** (gauntlet
  vs `SelectionStrategy`) is injected. Collapses the two duplicated pipelines and
  the triplicated mutation-id/forbidden-id/validation logic (`:804-818` ≡
  `:1383-1394`).
- `evolve/loop.py` — `evolve_n_rounds` + the 3 circuit breakers as a small
  `StopPolicy` set. `evolve/epoching.py` — contract-hash auto-epoching.
- `evolve/dashboard_projection.py` — the ~550 lines of `_publish_*`/`_serialise_*`
  /`_overlay_*`/`_open_field_tournament` (`:2311-2864`); pure presentation that
  does not belong in the loop driver.
- `evolve/lifecycle_services.py` — harmonograf launch + env-restore + meta-loop
  emitter. Move path math to `workspace/WorkspaceLayout` (Phase 1c).
- Target: orchestrator becomes a ~400-line driver wiring injected services.

**3b. `tournament/runner.py` (3292) → split by concern.**
- `worker_transport.py` (subprocess spawn/kill/serialize/read — the process
  boundary), `scheduling.py` (one parameterized board-unit scheduler; fast/full/
  budgeted/cache-first become flags, not 6 functions), `unit_cache.py`,
  `governance.py` (gate + Ladder + regression), `api.py` (thin
  `run_tournament`/`run_fast_mode`/`run_matchup` — collapse toward one entry +
  mode enum).

**3c. `dashboard/state_reader.py` (5467) → `dashboard/readers/` package.**
Pure move (no logic change), re-export shim for back-compat. Modules:
`paths`, `runtime_view`, `epoch_view`, `lineage_view`, `tournament_view`,
`gate_view`, `judge_view`, `run_log`, `events_index`, `search/identity`, and a
single `_sqlite.py` access seam (today the file opens `index.db` directly in
several places, one bypassing the `mode=ro` guard at `:3918`). Simplify
`build_gate_breakdown` (`:4550`, ~330 lines) to ask `tournament/gate.py` for
per-rule statuses instead of re-encoding the short-circuit order.

**3d. analyzer figures + report.** `report_figures.py` (1710) → `svg/primitives.py`
(axis/bar/marker/legend + `_esc`/`_fmt_*`, dedup vs `report_sections.py:38-48`) +
`svg/palette.py` (the one decision palette, owned there) + one module per
figure family. `report.py` (1401) → isolate the
markdown→HTML engine (`markdown_to_html:350`) into `report/markdown.py` and the
inline CSS (`_paper_css:1161`) into a data file.

**3e. `adapters/adk.py` (1068).** Extract the ~240-LOC judge-only steering
machinery (`:334-496`) into `judge_runtime/judge_only.py` (it is goldfive-steering
knowledge rather than adapter knowledge). Collapse the 3 near-identical per-kind drivers
+ 3× inline `_PerTurnCaller` into one `_goldfive_call` helper + one caller.

**3f. proposer.** `prompts.py` → `render.py` (block renderers) +
`visibility.py` (the `_bucket_scalar_delta`/`_band_*` overfitting-coarsening
logic that isn't templating). `structured.py` (743) → `salvage.py`
(LLM-response text recovery) + `validate.py` (experiment schema/semantic
validation).

**3g. mutation + index.** `mutation/applier.py` (733) → extract the ~10
Python-literal-surgery helpers into `mutation/literal_surgery.py`. Move
`synthetic/manifest_bridge.py` into `mutation/` and make `enumerator._content_hash`
public, breaking the mutation↔synthetic circular lazy-import. `index/ingest.py`
(1753) → `ingest/upserts.py` + `ingest/rebuild.py` + `ingest/repairs.py` (the
`backfill_*`/`repair_*` one-shot migrations).

---

## Phase 4 — Boundary cleanups + invariant enforcement

Lower-frequency, higher-judgment items; do after the structure is clean.

- **One HTML report generator** — built. `analyzer/report.py` renders
  `analysis.html` at every epoch lifecycle phase, from the `analysis.md`
  beside it; the second generator that rendered an overlapping epoch view
  under `epoch/` is deleted, and the palette hard-copy it forced with it.
- **Fix the synthetic-kind dispatch boundary**: today `adapters/adk.py` handles
  `single_turn`/`multi_turn_*` but `_tournament_worker.py:348` routes
  `synthetic_*` around the adapter. Pick one — all kinds through
  `RunnableHarness.run`, or an explicitly-documented non-adapter path.
- **Replace the `entry.context` stringly-typed `disable_drift`/`judge_only` seam**
  (hand-synced string keys, `== "true"` parse) with a typed per-entry channel.
- **Enforce assumed invariants as validators** (additive; only fire on
  already-broken input): duplicate mutation-id detection in the enumerator;
  board-unique judge-name detection in `assemble_judges`.
- **Relocate `telemetry/harmonograf_supervisor.py`** (process/lifecycle, not
  telemetry) next to the other supervisor concerns in `runtime/`.
- Schedule removal of the older on-disk key spellings behind a one-time
  migration:
  `brief.md`↔`rubric.md`, `--brief`/`--rubric`, `adk_entrypoint`,
  `round_index`/`stage_index`, `partial_*_agg`, and the empty-string
  `contract_hash` sentinel that marks a contract which never rolls.

---

## Critical files (most-touched)

- `src/zicato/orchestrator.py`, `src/zicato/tournament/runner.py`,
  `src/zicato/core/types.py`, `src/zicato/dashboard/state_reader.py`
  (the four god-objects)
- New: `src/zicato/util/`, `src/zicato/workspace/`, `src/zicato/evolve/`,
  `src/zicato/dashboard/readers/`
- `src/zicato/index/ingest.py`, `src/zicato/analyzer/report_figures.py`,
  `src/zicato/analyzer/report.py`, `src/zicato/adapters/adk.py`,
  `src/zicato/proposer/prompts.py`, `src/zicato/proposer/structured.py`,
  `src/zicato/mutation/applier.py`

## Sequencing rationale

Phase 0 (cruft) is free and shrinks the surface. Phase 1 (foundations) must
precede Phase 3 — splitting a god-object before the shared utils exist means each
split re-touches the same duplicated helpers and the canonical-read parsing.
Phase 2 (types/enums) makes the Phase 3 moves safe (a renamed field fails at
type-check rather than at runtime). Phase 3 god-objects are mutually
independent and can be split across separate pull requests. Phase 4 is judgment-heavy cleanup that benefits
from the clean structure underneath.

## Verification (every phase)

Run the parity harness (top of doc) before merging any step:
`uv run pytest -m "not node and not cascade_oc"` green, mypy/ruff clean (and ideally *stronger* than before), and golden-output
diffs empty for loss/gen_score/experiment/index-dump/report-HTML/report-SVG/
dashboard-JSON/`--help` against the `target_1_presentation` mock fixture. No live
`zicato evolve` runs (AGENTS.md rule 1). A non-empty golden diff means the step
was not behavior-preserving — fix before merge.

## Out of scope / non-goals

- No feature additions, removals, or behavior changes. The end user must not be
  able to tell any difference.
- No live-LLM runs as verification.
- The Rust supervisor crate (`crates/`, `src/`) is not part of this Python
  refactor program (touch only if a Phase-4 boundary move requires it).
  *(Finding 4 below is the one exception the review calls out: reader
  unification is a decided design direction that crosses this boundary on
  purpose.)*

---

# Structural findings — 2026-07 review

> **Status unchanged: still plan-only / not-yet-implemented.** A structural
> review (2026-07-12) revisited the god-object roadmap above against the
> code as it stands today and produced five findings that *sharpen the
> plan* — none is built. They compose with Part I: findings 1–3 refine
> Phase 3a (the `orchestrator.py` split), finding 4 refines Phase 1c/4, and
> finding 5 names an observability layer the whole pipeline emits into.
>
> **Sequencing intent** for the five findings, each still gated by the
> parity harness: concurrency (finding 1), then the knob registry
> (finding 3), then pipeline decomposition (finding 2). Reader unification
> (finding 4) is decided in design and sequenced with the Phase 4 boundary
> work. The log stream (finding 5) is specified in
> [`LOGGING.md`](LOGGING.md), and this document only fixes its seat in the
> layer cake.
>
> Note: `orchestrator.py` measures about 6,300 lines against Part I's
> census figure of 4,622, so the god-object figures above are a lower bound
> rather than a current count.

## Finding 1 — Propose-phase serialization (concurrency with deterministic post-ordering)

**Observed.** Two propose loops issue their LLM round-trips strictly
serially, even though the board-entry *runs* they later feed are already
concurrent:

- The **field loop** — `orchestrator.py` `_evolve_multi_challenger`'s
  `for offset in range(field_n):` — `await`s `_propose_and_apply_challenger`
  one challenger at a time. Each slot is minted with an offset-ordered
  identity: `next_id = f"v{base_n + offset}"` and `seed = offset + 2`
  (champion is seed 1; challengers follow in mint order).
- The **best-of-N slate loop** — `proposer/best_of_n.py`
  `BestOfNProposerAgent.propose`'s `for sample in range(n):` — `await`s
  `self._sample_slot(...)` per sample (the last slot may instead `await`
  `self._merge_recombined`/`self._mint_recombined`), emitting a
  `candidate_sampled` event per slot.
- By contrast, board-entry **runs** already fan out: `tournament/runner.py`
  schedules each `(side, entry, replicate)` subprocess worker under an
  `asyncio.Semaphore` sized from `RuntimeConfig.parallelism`. Concurrency is
  a solved pattern in the run layer and simply absent from the propose
  layer.

**Target.** The two loops are NOT symmetric, and conflating them is the
trap: the slate loop is embarrassingly parallel; the field loop is
*sequential* by design, and gathering it changes behavior.

- **Slate loop (best-of-N) — safe concurrency.** The N samples are
  independent: each slot varies only by a deterministic per-slot
  hint (`hints.hint_for_slot` edit-class + `hints.strategy_for_slot`
  framing, both keyed off `(slot, round)`) and the shared round-start
  context; no slot reads another slot's output. Collect them with
  `asyncio.gather` under a propose-side cap, then apply results in a
  **deterministic second pass** that emits the `candidate_sampled` events
  and appends candidates in slot order. Byte-stable goldens hold because the
  only ordering-sensitive surface here is the event `seq`, and the ordered
  pass restores it.
- **Field loop — sibling-conditioned, so NOT free to gather.** Challenger
  *k*'s prompt is threaded `prior_experiments=prior + tuple(siblings)`
  (`orchestrator.py:2095`), where `siblings` (`:1993`) accumulates one
  `PriorExperiment` per already-*applied* challenger `0..k-1` this round —
  sequential sibling-conditioning is an INTENTIONAL diversity property
  (`_evolve_multi_challenger`'s docstring: "each challenger diversifies away
  from … its just-proposed cohort"). A blind `gather` erases it. The honest
  option set:
  - **(a) Keep the field loop serial** (sibling-conditioning preserved).
    Slate-level concurrency inside each challenger still cuts propose
    wall-clock by ~`best_of_n`, so this is not "no speed-up." This is the
    only option under which the byte-stable-goldens promise holds.
  - **(b) Batched gather** — propose in batches of `w`, re-conditioning
    the sibling digest between batches. A tunable diversity/latency trade
    (`w=1` = option a, `w=field_n` = option c); goldens move, so it needs
    its own parity story.
  - **(c) Drop sibling-conditioning entirely** and gather the whole field —
    a DECLARED behavior change (the field's diversity pressure falls back to
    whatever `_mint_challenger_field` soft-rejects catch post-hoc), with its
    own parity/measurement story proving the field does not collapse.
- **Recommended default: (a) plus slate concurrency.** It is the latency
  win with zero behavior change and a green byte-level golden. Treat (b) as
  the measured follow-up if slate concurrency alone is insufficient. The
  byte-stable-goldens promise below holds **only for (a)**; (b) and (c) are
  measured changes rather than parity-preserving ones.

**Backpressure (nested fan-out).** Propose concurrency is two-level: a field
of `field_n` challengers, each running a `best_of_n` slate — so under any
option where the field also fans out (b/c) the in-flight propose count is
`field_n × best_of_n`, not `field_n`. The two levels must share ONE budget,
not two independent caps that multiply. Default to a single shared
`propose_parallelism` semaphore threaded through both the field gather and
the slate gather; an explicit two-level budget (a field cap and a per-field
slate cap) is the alternative, but the single shared cap is the simpler
default and the one an implementer should reach for first.

**Ordering-sensitive surfaces an implementer MUST protect** (each verified
in the tree; all must be driven from the post-gather ordered pass, never
from gather-completion order):

1. **Generation-id and seed minting are deterministic and happen before the
   gather.**
   `next_id = f"v{base_n + offset}"` and `seed = offset + 2` are computed
   from `offset` *before* any propose runs, so the `competitors_meta` seed
   order (champion = 1, challengers 2, 3, … in mint order) is already fixed
   and cannot be perturbed by completion order. Each id also names a
   distinct `generations/{id}/snapshot/` directory, so the *applies* never
   race on directories either. The only shared-artifact writes that DO race
   are `index.db` (`_ingest_experiment_into_index`) and `lineage.json`
   (`append_to_lineage`) — see items 2, 6, and 7.
2. **Journal / experiment index write order.** The per-slot persistence —
   `_ingest_experiment_into_index(workspace_root, epoch_id, generation_id)`,
   the `OutcomeRecord` writes for soft-rejected slots, and the
   `_publish_proposing` status callbacks — must fire in slot order so
   `experiment.json`/index rows land in a stable sequence.
3. **RoundLog event sequence.** `epoch/round_log.py` is a single-writer,
   append-only, `seq`-derived-from-tail log (`seq` = 1 for the first event,
   `+1` per append). The `round_emitter=round_log` appends —
   including best-of-N's `candidate_sampled` events carrying `{"i": sample,
   "n": n}` — must be appended in slot order, or the gap-free monotonic
   `seq` becomes a function of scheduling.
4. **The round-log fold.** Downstream consumers fold the sequenced RoundLog
   by `seq`; because `seq` is the machine ordering key, an interleaving-
   dependent append order silently reorders the fold's output.
5. **SSE progress `seq` — an INDEPENDENT writer, not RoundLog's.**
   `dashboard/sse.py`'s progress cursor (`_progress_signal` →
   `progress_log.tail_seq`, `sse.py:62,81`) is the TRUE liveness signal the
   dashboard diffs against — but it reads `zicato.runtime.progress_log`, a
   SEPARATE append-only log from `epoch/round_log.py`, whose per-slot
   `PROPOSE` beat is emitted INSIDE the propose coroutine
   (`orchestrator.py:~1651`). It therefore needs its OWN deterministic-order
   treatment in the ordered pass; it does not simply "inherit RoundLog's."
6. **Shared `lineage.json` write.** `append_to_lineage(…, pending=True)`
   (inside `_propose_and_apply_challenger` — since the Finding-2 extraction,
   `evolve/propose_apply.py`) upserts
   the in-flight node into the one shared `lineage.json`; two concurrent
   coroutines would interleave that read-modify-write. It must move to the
   ordered apply pass.
7. **`_mint_challenger_field` accumulator state.** `sibling_signatures` and
   `accepted_mutation_sets` (`:1999`, `:2012`) grow in mint order, and slot
   *k*'s soft-reject decision (exact-duplicate + Jaccard-overlap against
   `0..k-1`) is a semantic dependency on the earlier slots rather than a
   log-ordering one. It belongs to the ordered pass and — like the
   sibling-conditioning above — cannot be reproduced by a blind gather.
8. **The slate loop's `validate_experiment` hook is a shared-DIRECTORY
   write (the derive transaction), NOT just an event-ordering surface.**
   Every slot's `inner.propose` runs `ctx.validate_experiment`
   (`evolve/round.py::build_post_apply_validator._validate`), which calls
   `genstore.derive_generation(epoch_id, parent_id, child_generation_id=next_id, …)`
   — an `rmtree` + `apply_patches` against the ONE shared
   `generations/{next_id}/snapshot/` (directory backend) or the ONE shared
   epoch-branch working tree, tag, and `.derive-scratch` path (git backend)
   — plus the shared `last_child_snapshot["path"]` mutation, which the
   post-selection alignment step then read as the LAST-validated
   candidate's tree. Two concurrent slots deriving into the same `next_id`
   would corrupt the tree, make validation findings scheduling-dependent,
   and turn `tests/test_best_of_n_tree_integrity.py` flaky. So — UNLIKE the
   field loop, whose `v{base_n+offset}` ids already name disjoint
   directories (item 1) — the slate was NOT gatherable as-is: the shared
   derive transaction is the slate's version of the field's structural
   precondition, and had to be split (below) before the gather was sound.

**Structural precondition (the slate split).** The slate loop's blocker
mirrors the field loop's: side effects (here the shared `next_id` derive)
run *inside* the coroutine, so "gather, then finalize in order" is
unreachable until the derive is split off. The split commit 2 implements:

- **Per-slot scratch derivation.** Each slate slot validates into its OWN
  throwaway scratch child tree (a `mkdtemp` parent, cleaned in `try/finally`
  including on propose failure/degrade), NOT the shared `next_id` tree, via
  a new `GenerationStore.derive_scratch(epoch_id, parent_generation_id,
  patches, scratch_root)` — a pure `apply_patches` into a disjoint temp
  dir that NEVER enters the generation namespace (no commit, no tag, no
  branch/working-tree mutation), so it is provably invisible to every
  walker (records listing, lineage, GC, reindex, the dashboard readers all
  enumerate tags/`generations/` — never a temp dir). Two slots' scratches
  are fully disjoint, so the derive races on nothing. The scratch validator
  is built once per round beside `build_post_apply_validator`
  (`evolve/round.py::build_scratch_validator_factory`) and threaded onto
  `ProposerContext.scratch_validator_factory`; the wrapper allocates one
  scratch validator per slot.
- **One unconditional post-selection derive.** After the critique/selection
  pass picks the winner, the chosen candidate is ALWAYS derived into the
  real `next_id` (via the round's own `build_post_apply_validator` hook),
  guaranteeing the mounted tree == the chosen candidate
  (`BestOfNProposerAgent._mount_chosen`). It supersedes the earlier
  conditional alignment step, which re-derived only when the chosen
  candidate was not the last-validated one and kept restore bookkeeping for
  the shared tree: with per-slot scratch there is no shared last-validated
  tree to align against, so the funnel simplifies to one always-runs final
  derive that raises the standard `ProposerError` when the chosen candidate
  cannot be re-derived. The recombination-mint / llm-merge slots and their
  degrade paths ride the same per-slot scratch mechanics.
- **The gather.** With the slots now write-disjoint, the N samples run
  concurrently under a `RuntimeConfig.propose_parallelism` semaphore
  (runtime-only — never part of the scoring canonical form; default 4;
  `propose_parallelism=1` reproduces serial behavior byte-identically).
  The `candidate_sampled` round events, candidates-list order, and any
  pinned logging are all emitted in SLOT order in the deterministic
  post-gather pass; per-slot failures are captured per-slot so one slot
  failing never loses the others. The `applying` progress BEATS are the
  one exception: each slot's `_beat` fires MID-gather, in completion
  order, from inside its scratch validator — not in the post-gather pass.
  This is benign because every slot beats the identical phase string
  (`applying:round_{i}:{next_id}`, keyed on the shared child id with no
  slot identity), so a completion-ordered sequence of these beats is
  indistinguishable from a slot-ordered one to any observer.

**Structural precondition (the field split).** None of the above is reachable
while `_propose_and_apply_challenger` runs its side effects *inside* the
coroutine: today it both proposes AND ingests
(`_ingest_experiment_into_index`, `:1751`), writes the pending lineage node
(`:1777`), and feeds the mint accumulators — so "gather, then emit in order"
is unreachable until it is first SPLIT into a pure-propose half (gatherable,
no shared-state writes) and an apply/persist/ingest half (ordered, deferred
to the second pass). That split is Finding 2's stage decomposition
arriving early; concurrency cannot land cleanly before it.

This finding **composes with finding 2**: the deterministic post-ordering
pass is the "apply / persist / ingest" stages of the pipeline below,
and "which stages may overlap" (here: propose may fan out, apply/persist may
not) becomes a declared property of the stage graph.

**Owning the sequencing trade-off.** Landing Finding 1 *before* Finding 2
means writing the gather, the ordered second pass, and the propose/apply
split above INTO the monolith, then relocating that logic when the pipeline
is decomposed. This is deliberate: concurrency goes first because it is the
urgent latency win, and the parity harness makes the later lift safe — the
gather and ordered-pass logic is *lifted* into the stage graph rather than
rewritten,
so decomposition inherits a structure that already names its concurrency
boundary. (The knob registry, Finding 3, sequences second and stays
orthogonal to both.)

## Finding 2 — The orchestrator monolith → an explicit typed round pipeline

**Observed.** `orchestrator.py` (~6,300 lines) drives the round *and*
accretes a per-program IO builder for every feature added to the loop:
`_build_candidate_screen_runner` (screening), `_build_recombination_pair`
(recombination), `_build_genealogy_items` (genealogy channel),
`_build_events_paths`, … each constructed inline at the top of the round and
threaded down through the propose call as an opaque callable. New programs
append another builder and another positional argument; the loop driver
carries the union of every program's construction logic (~150+ `Any`
annotations remain in the file — 154 at last count).

**Target (sharpens Phase 3a's `evolve/round.py`).** An **explicit typed
round pipeline** with named stages, each declaring its inputs and outputs:

```
propose → apply → screen → schedule → gate → persist → ingest
```

- Each stage is a small typed unit (a dataclass of inputs → a dataclass of
  outputs) rather than a step buried in a 6k-line function. The two evolve
  pipelines (gauntlet `evolve_once` vs `_evolve_multi_challenger`) share the
  one stage sequence with the scheduler injected (as Phase 3a already
  proposes).
- **Per-round context builders live beside their consuming stage** rather
  than at
  the top of the driver: `_build_candidate_screen_runner` moves next to the
  `screen` stage, `_build_recombination_pair` and `_build_genealogy_items`
  next to `propose`. Adding a program adds a builder in one place — its
  stage — instead of another argument on the driver's signature.
- The **stage graph is where overlap is declared.** Finding 1's answer
  ("propose may fan out; apply/persist/ingest run in slot order") is a
  property of the graph edges rather than an implementation detail hidden in
  a for-
  loop. The graph is the single place a future reviewer reads to learn which
  stages are concurrency-safe.

**As landed (behavior-preserving first cut).** The decomposition shipped as a
sequence of *verbatim-relocation* commits that carve the stages out of the
driver as sibling modules under `src/zicato/evolve/`, without yet changing the
round's shape. Five stage modules now exist, each a small cohesive unit of the
pipeline above:

- **`ingest.py`** — the live SQLite analytical-index IO (`_index_db_path`,
  `_ingest_experiment_into_index`, `_load_prior_experiments`,
  `_load_mutation_track_records`, `_cache_gen_score`).
- **`persist.py`** — the terminal write funnel + round tail
  (`_finalize_generation`, `_round_epilogue`, `_persist_rejected_round`, and
  the two synthetic reject/skip outcome builders).
- **`gate.py`** — holdout confirmation, operator override re-resolution,
  champion-evaluation provenance, and integrity checks. Strategy progression
  and Bradley–Terry evidence confirmation live in `selection/driver.py`.
- **`round_context.py`** — the pre-propose ("screen") context builders that
  assemble the proposer-context inputs once per round
  (`_build_candidate_screen_runner`, `_build_recombination_pair`,
  `_build_genealogy_items`, `_build_calibration_summary`) — the builders the
  Target above wanted moved "beside their consuming stage."
- **`propose_apply.py`** — the propose → apply → admit stage: `_propose_child`
  (the shared propose shape), `_propose_and_apply_challenger` (the field path's
  propose/validate/derive/persist pipeline), the random-baseline placebo arm
  (`_mint_placebo_challenger`, `_maybe_run_placebo_arm_gauntlet`), the
  applied-child record + tracker-reason helpers (`_AppliedChallenger`,
  `_trim_reason`, `_short_reject_reason`), and the pure field-diversity
  accept/soft-reject decision (`_mint_challenger_field`, `_FieldMintDecision`,
  and the `_diversity_signature` / `_duplicates_inflight_sibling` /
  `_max_overlap_with_accepted` companions).

Each module keeps the `zicato.orchestrator` logger name, imports its stable
collaborators directly, and resolves back-edges into the driver as lazy
call-time imports through the orchestrator module object; the orchestrator
re-exports the externally-referenced names so callers and tests are unaffected.
`evolve_once` prepares a typed `PreparedRound` and `evolve_field_round`
sequences candidate production, strategy evaluation, evidence and holdout
confirmation, and typed `RoundSettlement` persistence for every structure.

**Remaining: the `schedule` closure-lift.** The one stage from the Target
sequence still living inside the driver is **`schedule`** — the matchup-dispatch
logic that runs the field/gauntlet duels. In the multi-challenger path it is the
`_run_matchup` **nested closure** inside `_evolve_multi_challenger` (it captures
the round's adapter, board, weights, config, emitters, and the live-standings
publish seam from the enclosing frame); in `evolve_once` it is the gauntlet's
single-duel dispatch. Extracting it is **not** a verbatim relocation like the
five stages above: it requires *lifting nested closures out of their enclosing
frame* — every captured variable becomes an explicit parameter (or a small typed
"schedule context" dataclass), and the live-publish/standings-overlay callbacks
have to be threaded back in as injected seams. That is a genuine
shape-changing refactor, and it is deferred: its marginal value is
low now that the per-round builders and the propose/apply/persist/gate/ingest
stages all have homes, and doing it verbatim is impossible, so it is best done
together with Finding 1's propose-side gather (which already needs
`_propose_and_apply_challenger` split into a pure-propose half and an
apply/persist half — see Finding 1's closing note) rather than as a standalone
relocation.

## Finding 3 — The knob tax → declarative field metadata

**Observed (traced end-to-end for the `genealogy` knob).** Adding one
proposer/scoring knob touches a fixed set of hand-maintained registries.
For `ProposerQualityConfig.genealogy` (`core/scoring_config.py`), the review
found the same field mirrored across **seven** sites:

1. **The dataclass field** — `core/scoring_config.py`
   `ProposerQualityConfig` (`genealogy: int = 0`).
2. **The omit-at-default set** — `epoch/contract.py`
   `_SCORING_OMIT_AT_DEFAULT_FIELDS` (adds `"genealogy"` so a default value
   never rolls the contract hash).
3. **The serializer-completeness guard table** —
   `tests/test_contract_serializer_completeness.py`'s per-field non-default
   value map (`"genealogy": 4`), which the `_all_fields_nondefault` /
   round-trip tests iterate to prove no field is silently dropped.
4. **The builder op** — `builder/operations.py::set_proposer_quality`
   (`genealogy: int | None = None` parameter + its validation/apply block).
5. **The API dispatch** — `builder/api.py`
   (`genealogy=_opt_int(args, "genealogy")`).
6. **The copilot mirror** — `builder/copilot_tools.py::set_proposer_quality`
   (the duplicated tool signature the chat copilot drives).
7. **The GUI row + node test** — the builder settings row in
   `dashboard/static/js/views/builder.js` (title/body/`numInput` →
   `runOp('set_proposer_quality', { genealogy })`) and its assertion in
   `dashboard/static/test/builder.test.mjs` (posts
   `set_proposer_quality {genealogy:4}`).

Miss any one and the knob half-works silently: e.g. omitting site 2 rolls
every existing epoch's contract hash; omitting site 6 leaves the copilot
unable to set it. `recombine_merge` traces to the identical seven sites.

**Target.** Drive the mechanical registries from **declarative field
metadata** on the dataclass — `dataclasses.field(metadata={...})` carrying
the omit-at-default flag, the builder-op arg spec (type, bounds,
epoch-rolling), and the GUI row descriptor. Generate the omit-list, the
builder-op/copilot signatures, and the builder-row scaffold from that
metadata so a new knob is *one* field declaration. **Retain the existing
guard tables as the enforcement net** rather than the source: `contract_serde.py`
is already field-enumerating (it derives from `dataclasses.fields()` and so
covers new fields automatically), and
`test_contract_serializer_completeness.py` stays as the red-on-drift check
that a generated table and the dataclass never disagree.

## Finding 4 — Dual reader implementations (Python `query/` + Rust supervisor)

**Observed.** The same index/canonical payloads are decoded twice:

- **Python** — `src/zicato/query/` (`epoch_view.py`, `tournament_view.py`,
  `lineage_view.py`, `gate_view.py`, `racing_view.py`, `ratings.py`, … over
  `query/_sqlite.py`) serves the dashboard/analyzer read surface.
- **Rust** — `crates/supervisor/src/` (`epoch.rs`, `elim_states.rs`,
  `tournaments.rs`, `index_db.rs`, …) re-implements the same reads for the
  `/statusz` + dashboard-API surface that must survive an orchestrator
  wedge.

The duplication is **only** that subset of index-projection views. Most of
the 26-file crate is NOT a `query/` duplicate but crash-survival and
integrity infrastructure with no Python equivalent: `reader.rs`'s in-flight
lineage node (`reader.rs:55` the live active-tournament event log; `:304`
the tri-state in-flight generation the Tree needs), `run_log.rs`'s live
`events.jsonl` tail, `divergence.rs`'s dead-worker/dead-pid audit
(`divergence.rs:23`), `ledger.rs` + `diff_containment.rs` (the
tamper-evident integrity notary), `signal.rs` (POSIX `/proc` liveness), and
`statusz.rs`/`watchdog.rs` (heartbeat freshness + escalation). These exist
*because* the index — and the orchestrator writing it — can be stale when
the loop is wedged.

The two are held together only by the reader-parity goldens
(`tests/test_dashboard_reader_parity.py` + `tests/_reader_parity_harness.py`)
— and **the seam has already failed once**: the Rust reader pinned its
expected schema version to a **hardcoded literal**, so when Python bumped
`SCHEMA_VERSION` v10→v11→v12 the guard never fired and the supervisor's
read-only surface **silently served empty for two schema generations**. The
fix (now in `crates/supervisor/src/index_db.rs`,
`expected_schema_version_is_pinned_to_python`) parses `SCHEMA_VERSION`
straight out of `src/zicato/index/schema.py` so drift reds one suite or the
other. That fix hardens the *detector*; it does not remove the *duplication*
that made the failure possible.

**Options.**

- **(A) Supervisor serves from the index only.** Constrain the Rust surface
  to a thin, schema-guarded projection of `index.db` (the read it already
  does well) and delete any Rust re-derivation of canonical-file facts;
  Python `query/` remains the one place canonical files are interpreted.
  Keeps two decoders but shrinks the Rust one to a single, guarded shape.
  **Carve-out:** this applies ONLY to the reader-parity dashboard read-views
  that overlap Python `query/` (epoch / tournament / lineage / gate / racing
  / ratings). It must NOT touch the liveness/integrity surface named in
  *Observed* — `reader.rs`'s in-flight lineage node, `run_log.rs`'s live
  telemetry tail, `divergence.rs`'s dead-pid audit, `ledger.rs`,
  `diff_containment.rs`, `signal.rs`//proc, and `statusz.rs`/watchdog
  heartbeat — which reads canonical files directly *by design*, so
  it can still report when the index (and the orchestrator writing it) has
  gone stale. Folding those into an index-only projection would delete the
  crash-survivability the supervisor exists for.
- **(B) One implementation owns views, one caller.** Pick a single owner for
  each view payload (Python `query/` is the natural owner — it changes with
  the schema) and have the supervisor call *it* rather than re-decode: either
  Rust shells to a `zicato` read subcommand, or the view payloads are frozen
  as a versioned read contract the supervisor consumes verbatim.

**Recommendation.** Option **A** for the near term — it is the smallest move
that removes the class of bug that already bit (Rust re-tallying a fact a
canonical file settled), keeps the supervisor's crash-survivability property
(it still reads the file directly), and leaves the schema-version pin as the
single enforced seam. Treat option B (collapsing to one decoder) as the
end-state once finding 1/2's `workspace/readers` layer (Phase 1c) exists to
be the one owner. **Migration safety:** the reader-parity goldens are the
gate for either move — they must stay green byte-for-byte across the change,
and the `SCHEMA_VERSION`-parsing pin (both directions) must remain the CI
tripwire so a schema bump can never again ship a silently-empty supervisor.
This is the deliberate exception to the "Rust crate is out of scope" non-goal
above.

## Finding 5 — Logging insertion point (the observability layer)

**Observed.** The tree has one `logging.basicConfig`
(`_tournament_worker.py`, at `WARNING`); there is no structured per-run log
stream. Operators reconstruct a run's story from `events.jsonl` + the
RoundLog, neither of which is a general diagnostic log.

**Target (reference, do not re-design).** The structured per-run log stream
is a first-class artifact beside `events.jsonl`, specified in
[`LOGGING.md`](LOGGING.md). This document does not design that stream; its
job is to fix *where it sits*. The log stream is the **observability layer
that every stage of finding 2's pipeline emits into**. In the layer cake it
is a cross-cutting sink beneath the stage graph — propose, apply, screen,
schedule, gate, persist and ingest each emit structured records into it —
and it is written through the one storage seam and atomic writer (Part II),
so it is crash-safe like the canonical artifacts.

## Confirmed keep-as-is (the validated structure)

The review also confirmed the following as **correct and load-bearing** —
the reimplementation must **preserve** them, not "clean them up":

- **Subprocess kill-isolation + the Rust watchdog.** Each `(side, entry,
  replicate)` is an isolated subprocess killable from outside; a separate
  Rust supervisor enforces deadlines/staleness. Cage-vs-payload is right.
- **asyncio + `Semaphore(parallelism)` run concurrency** in
  `tournament/runner.py` — the run-layer fan-out finding 1 wants the propose
  layer to match.
- **WAL index: files-canonical, index-derived, full-rebuildable.** `.zicato/`
  is truth; `index.db` is a pure projection re-derivable by `reindex`. (This
  is also Part II design principle 1.)
- **Pure-core / IO-builder-at-the-edge.** Pure decision functions with IO
  constructed at the boundary — the shape finding 2 makes *explicit* as a
  stage graph rather than removes.
- **Contract-hash epoch rolling.** A contract change rolls the epoch via the
  canonicalized hash; the omit-at-default discipline (finding 3) exists to
  keep that hash stable for default knobs, and it must be preserved
  unchanged.
- **seq-driven SSE.** The monotonic `seq` liveness cursor
  (`dashboard/sse.py`) is the right change-detection primitive; finding 1
  protects its determinism rather than replacing it.

---

# Part II — Data model & storage system: target design

This is the concrete target for Phase 1c/1d and Phase 2a of the roadmap above.
It is what the data model and storage system *should be*. It preserves every
on-disk artifact and every behavior; it changes only how the code is shaped
around them. A one-time `zicato migrate` (below) normalizes the older
on-disk forms
so the alias-tolerance code can be deleted.

## Design principles

1. **One canonical truth, one projection.** The `.zicato/` filesystem is the
   only source of truth. `index.db` is a *pure projection* — it never re-derives
   a fact a canonical file already settled. Every disagreement risk in the audit
   traces back to a violation of this; the design removes the violations.
2. **One serializer.** Every persisted object is a frozen dataclass round-tripped
   by a single declarative serde. No hand-written `to_dict`/`from_dict`, no
   `dataclasses.asdict` in one place and a hand reader in another. Eliminates the
   silent-field-drop class (e.g. journal dropping `expected_metric_movements`).
3. **One layout authority.** All path math lives in `WorkspaceLayout`. No module
   joins `"epochs"` and filenames by hand: the orchestrator does not, and
   neither does the dashboard.
4. **One write seam, one atomic primitive.** Every canonical write goes through
   the `StorageBackend` seam, which uses the single fsync-ing atomic writer.
   The two weaker forks are deleted.
5. **Make illegal states unrepresentable.** Closed string sets become enums;
   identity strings become typed IDs; "absent" becomes `None`, never `""`.
   The type checker does the work the ~40 defensive comments do today.
6. **No semantic change.** IDs, file formats, JSON keys, and the contract hash
   are byte-identical after migration. `NewType` IDs and enums (StrEnum) erase to
   the same wire strings, so golden output is unchanged.

## Target data model — module map (splits `core/types.py`)

`core/` becomes a package of cohesive modules; `core/__init__.py` re-exports every
public name so import paths stay stable through the staged refactor.

```
core/
  ids.py            NewType EpochId, GenerationId, RunId, EntryId, MatchId,
                    TournamentId, PatchId, MutationId. Erased at runtime → zero
                    behavior change; kills the stringly-typed key confusion and
                    the empty-string sentinels (absent ID = None, not "").
  enums.py          ALL closed-set values in one place as StrEnum (subclass str,
                    JSON-identical): ExpectationKind, OutputScope, JudgeMode,
                    MutationKind, PatchOpKind, BoardEntryKind, DriftDirection,
                    DriftMagnitude, TournamentDecision, Side(parent/child),
                    RunStatus(queued/running/completed/aborted),
                    TournamentPhase(running/completed/aborted),
                    TournamentStructureKind(gauntlet/single_elim/double_elim/
                    swiss/racing), ChampionEvalMode(full/fast/fast-degraded),
                    ScoringProvenanceKind, Severity(info/warning/critical),
                    MetricSeverity, StandingStatus, HealthCode, PriorDecision
                    (TournamentDecision + in_flight).
  drift.py          DriftKind registry (the 41 GOLDFIVE_DRIFT_KINDS) + the ONE
                    validate/normalize fn (replaces the 7+ copies); DriftCount,
                    MetricCount.
  board.py          BoardEntry (kind-discriminated; consider a tagged union of
                    SingleTurn/ScriptedMultiTurn/EmulatedMultiTurn/Synthetic
                    subtypes instead of one struct with optional fields),
                    Expectation, JudgeSpec, UserPersona, ScriptedTurn, BoardMeta
                    (disable_drift/judge_only as typed fields — see storage §6).
  loss.py           LossProfile, ExpectationResult, JudgeLoss, RunResult.
  scoring_config.py ScoringWeights DECOMPOSED into nested typed groups:
                    DriftScoring (severity_weights, per_kind,
                    plan_revision_weight),
                    JudgeScoring (per_judge, default_judge_weight),
                    FailureScoring (task_failure_weight,
                    not_completed_weight),
                    PassScoring (pass_weight, pass_transform),
                    PromoteGate (promote_margin, pass_rate_monotonicity[+scope]),
                    RegressionGate (enabled, command, timeout_s),
                    NamespaceScoring (namespace_weights, namespace_monotonicity,
                    drift_kind_aggregation),
                    ScoringPlugins (outcome_summarizer_spec, drift_reducer,
                    scalar_fn),
                    TournamentStructure, OverfittingConfig→LadderConfig.
  lineage.py        Epoch (NEW typed record), EpochContract (today's EpochConfig,
                    renamed), Generation, Lineage (a TYPED DAG replacing the
                    untyped lineage.json dicts), Experiment, HypothesisSpec,
                    OutcomeRecord, Patch, ExpectedDriftMovement/MetricMovement,
                    DriftMovementActual/MetricMovementActual, MatchOutcome,
                    PriorExperiment.
  tournament.py     Contestant, Matchup, MatchupResult, SelectionDecision,
                    Standing, RoundRecord, MatchRecord, GateOutcome
                    (moved out of selection/strategy.py into the shared model so
                    runtime/state.py can use them — see next).
  runtime.py        RuntimeConfig, Heartbeat, ActiveRun, ActiveTournamentEntry,
                    ActiveTournament — the last now TYPED: its competitors/rounds/
                    standings/field_status/projected fields use the
                    core/tournament.py records rather than dict[str,Any]. Deletes
                    the 31 Any in runtime/state.py and the getattr-over-Any
                    LossProfile projection (the helper imports core/loss directly;
                    the "avoid importing core" workaround is removed because the
                    shared types now live below both).
  patterns.py       Pattern, DetectorInput.
  health.py         HealthFinding, LoopHealth.
  proposer.py       ProposerSkill, ProposerSpec, ProposerContext, ProposerBrief.
  serde.py          THE single declarative (de)serializer (see below).
```

### The single serializer (`core/serde.py`)

Generalize the existing field-enumerating `epoch/contract_serde.py` (already
proven for the scoring dataclasses) into the *one* serde for every persisted
frozen dataclass:

- `to_jsonable(obj)` / `from_jsonable(cls, data)` walk `dataclasses.fields()`,
  recurse into nested dataclasses, tuples, and mappings, and render enums/IDs as
  their string value. Deterministic key order, float rounding where the contract
  hash needs it.
- **One alias table** (`_LEGACY_ALIASES`) applied at the single read boundary —
  the only place older on-disk keys are mapped (`tournament_structure`→`tournament`
  is the sole alias that *survives* migration because it is the persisted contract
  form; all others are removed by `zicato migrate`).
- Replaces: the hand-written `Heartbeat/ActiveRun/ActiveTournament*.to_dict`
  (`runtime/state.py`), the hand-written `LossProfile` serde (`reducer.py`), the
  hand-written `Experiment/Hypothesis/Outcome/EpochConfig` serde (`epoch/journal.py`,
  `epoch/lifecycle.py`), and `board/jsonl.py`'s hand serde (board keeps its JSONL
  line framing + `board_meta` header, but each line body serializes via this).
- **Closes the silent-drop bug**: because serde is field-complete by construction,
  `expected_metric_movements` and `metric_movements` cannot be dropped.

### Identity & sentinels

- Typed IDs via `NewType` (runtime-erased, so wire-identical). Functions that
  take `epoch_id`/`generation_id`/… get real types instead of bare `str`.
- Replace the load-bearing empty-string sentinels with `None`/enum:
  `parent_generation_id: GenerationId | None` (seed = None, not `""`);
  `match_id: MatchId | None`; `contract_hash: str | None` (None = pre-hash epoch
  — the never-rolls rule for an unhashed contract becomes an explicit `None`
  check rather than a magic `""`, which removes the hazard of a corrupted
  hash reading as unhashed);
  `champion_eval_mode: ChampionEvalMode`. The migration writes these forms; the
  serde maps old→new on read during the transition.

## Target storage system

### Layer cake (top depends only on the layer below)

```
core/ (types + serde)            pure data, no I/O
   ▲
storage/  (StorageBackend)       keyed atomic JSON/JSONL record store
   ▲          + atomic.py        the ONE fsync-ing atomic writer
workspace/                       layout.py  = ALL path math (single source)
   │                             readers.py = typed canonical reads → core objects
   │                             writers.py = typed canonical writes → seam+atomic
   ▼
index/ (projection)   telemetry/ (reducer)   analyzer/   dashboard/readers/
        — every one of these consumes workspace/, never raw open()/Path joins —
```

### Storage seam (collapses 3 bridges + 1 shim → 1)

- **One `StorageBackend` ABC** (`storage/backend.py`) + `FileStorageBackend` +
  `InMemoryStorageBackend` (unchanged interface; already clean and conformance-
  tested). **One `workspace_backend(workspace_root, *, start)`** — done: the
  duplicate `backend_for` in `epoch/_storage.py` and `runtime/_storage.py` is
  deleted and `storage/factory.py` holds the sole construction path.
- **Per-domain key helpers stay** but become thin key-namespace modules
  (`storage/keys/runtime.py`, `storage/keys/epoch.py`) over the one constructor.
- **Delete `runtime/_atomic.py`** (pure back-compat shim); its one external caller
  (`dashboard/state_reader.py`) moves to `workspace/readers`.
- **One atomic primitive** (`storage/atomic.py`, fsync + tmp + replace). Delete
  `orchestrator._atomic_write_text` (no fsync) and route `reducer.write_loss_profile`
  (no tmp/rename/fsync) through the seam. This makes `loss.json` — the hottest,
  most-ingested artifact — crash-safe, removing the "torn write → silent row loss"
  failure mode.
- **Bring the canonical telemetry artifacts inside the seam.** Today `loss.json`,
  `gen_score.json`, the `field-*.json` snapshots, and the orchestrator markers
  bypass it. `workspace/writers.py` writes all of them through the backend so the
  seam actually encloses the data the index projects from.

### Layout authority (`workspace/layout.py`)

`WorkspaceLayout` owns every path the audit found scattered: the marker files
(`current_generation`, `v0_seed_from`, `contract_components.json`), `gen_score.json`,
`index.db`, the field-tournament dir, and the *entire dashboard read surface*
(which today re-derives layout with literal filename joins at dozens of sites).
`core/workspace.py` is the seed of this; the work is to make it the *only* path
source and delete the parallel copies in `orchestrator.py` and `dashboard/`.

### Typed canonical read/write layer (`workspace/readers.py`, `writers.py`)

The seam every consumer shares (this is roadmap Phase 1c, concretized):
`read_board(epoch)`, `read_lineage()`, `read_epoch(epoch)`, `read_experiment(gen)`,
`read_loss(gen, entry[, replicate])`, `read_gen_score(gen)`, `iter_events(run)`
(the ONE camel/snake-tolerant reader, replacing 4), `promoted_spine(epoch)`.
Each returns `core/` domain objects via `core/serde`. Writers are the symmetric
set, all through the storage seam. dashboard, analyzer, telemetry, and index stop
re-parsing files and stop re-implementing normalization.

### Index = pure projection (`index/project.py`, `index/query.py`)

- **Delete `_drift_counts_from_events`** and every other re-derivation. The index
  is built *only* from the typed readers' output: `loss_profiles`/`judge_losses`/
  `metric_counts` from `LossProfile`; `experiments`/`patches`/`tournaments` from
  `Experiment`/`OutcomeRecord`; generations/promotion from the typed `Lineage`.
- **Single source of truth for promotion:** the typed `Lineage` is authoritative.
  `experiment.json`'s outcome is descriptive; the index projects `promoted`/
  `parent_generation_id` from `Lineage` rather than by re-testing
  `experiment.json`.
  Fix the dual-write ordering so `Lineage` is written before/with the experiment
  outcome (today the index trusts `experiment.json` *because* lineage lands later).
- **Project `gen_score.json` absolute scalars into the index** (new
  `generation_scores` rows or columns) so `tournaments.parent_scalar/child_scalar`
  are populated and the dashboard/analyzer stop reading files for absolutes —
  closing the "permanent projection gap."
- **One read seam:** `index/query.py` is the *only* way to read the index.
  Delete `state_reader._open_index`/`_query` (the private `mode=ro` connection)
  and the inline SQL; the dashboard reads via `query.py`. `tournament/detail.py`
  and `cli` likewise.
- **Keep** the additive-migration + `PRAGMA user_version` scheme (it is clean and
  the index is rebuildable). **Decision point:** enabling real `FOREIGN KEY`
  constraints (`runs.run_id` ← `loss_profiles`, `epochs.parent_epoch_id`, …) would
  make the logical FKs enforced — recommended for a clean rebuildable projection,
  but the Rust supervisor mirrors the DDL and reads the file directly, so confirm
  the DDL change with the supervisor owner before enabling.

### Generation source trees and generation records

The `GenerationStore` protocol with `DirectoryGenerationStore` (copytree per gen)
and `GitGenerationStore` (content-addressed commits, worktree per run) is a good
abstraction and stays. It owns source path calculation, explicit local
materialization, seed/derive/scratch transactions, isolated checkout, browsing,
source diffs, and source pruning. Patch and experiment records remain behind the
generic `StorageBackend` abstraction. `generation_source_backend` is mandatory on
initialized workspaces; backend resolution never scans workspace contents.

### Versioning and clean workspace formats

Canonical records carry `format_version`; the SQLite projection carries its own
schema version. Old workspaces are outside the reimplementation's compatibility
contract. Each supported format has one spelling and one typed shape.

- Refuse missing or unsupported canonical format versions.
- Delete obsolete alias and shape-tolerance code as each owning surface is
  consolidated.
- Rebuild `index.db` from canonical files after any derived-schema change.

## How this maps to the roadmap & how it's verified

- Phase 1c = `workspace/` (layout + readers/writers) + index-as-projection.
- Phase 1d = the storage-seam collapse + single atomic primitive.
- Phase 2a = the `core/` split + `core/serde` + enums + typed IDs + typed
  `ActiveTournament`.
- Alias-code deletion follows the owning reader/writer consolidation and is gated
  like every other behavior change.

**Parity proof specific to this design:** because the serde, enums, and IDs are
wire-identical, the strongest check is byte-level: for the `target_1_presentation`
fixture, every canonical file written before vs after must be byte-identical
(modulo the deliberate migration normalization, which is applied to the baseline
too), the `index.db` text dump after `reindex` must match, and the contract hash
for an unchanged contract must not move (guards against an accidental
canonicalization change rolling an epoch). Run the full suite + mypy (which should
report materially fewer `Any`/`getattr` escapes than before) at each step.

## Open decision points (defer to execution)

1. **Index FK enforcement** — enable real `FOREIGN KEY` constraints vs keep
   logical-only (Rust supervisor mirrors the DDL; confirm before changing).
2. **`BoardEntry` shape** — one struct with optional kind-fields (status quo,
   minimal churn) vs a tagged union of per-kind subtypes (cleaner, more invasive).
3. **Promotion source of truth** — confirm `Lineage` (recommended) over
   `experiment.json` as authoritative, and re-order the dual-write accordingly.
