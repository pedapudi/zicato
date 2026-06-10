# zicato — cleaner reimplementation roadmap (behavior-preserving)

> **Status: proposal / design note — not yet implemented.** Captures a
> behavior-preserving reimplementation roadmap (Part I) and a target data-model
> & storage design (Part II), informed by a three-domain audit of the codebase.
> Companion to [`ARCHITECTURE.md`](ARCHITECTURE.md), [`STORAGE.md`](STORAGE.md),
> and [`TOURNAMENT-DATA-MODEL.md`](TOURNAMENT-DATA-MODEL.md).

## Context

`src/zicato/` is ~73k LOC of Python across ~24 modules, backed by 165 test files.
It works and ships, but a three-domain agent audit (engine/lifecycle,
evaluation, observability/delivery) found the complexity is concentrated in a
handful of god-objects and a thick layer of accidental duplication, with the
type checker largely disabled by `Any` signatures and `getattr`-over-`Any`
access. The goal of this program is a **significantly more understandable,
modular, simpler, less error-prone** implementation that **retains 100% of
features with zero user-visible difference**.

Strategy: **staged in-place refactor**, not a rewrite. Every step is
behavior-preserving and merges only when the full test suite + golden-output
parity checks pass. There is no long "red" period and parity is guaranteed at
every commit.

Scope: a phased roadmap and target design to pick from later. Nothing here is
implemented yet.

### The cross-cutting signal (found independently by all three auditors)

1. **God-objects** dominate complexity: `dashboard/state_reader.py` (5467),
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
   over `Any` (33 `Any` in orchestrator alone); a renamed field fails at runtime,
   not at type-check.
5. **~40 blanket `except Exception  # noqa: BLE001`** swallow-everything blocks
   (29 in orchestrator) that hide systematic data-integrity drift.
6. **Two near-duplicate evolve pipelines** (gauntlet `evolve_once` vs
   `_evolve_multi_challenger`) with verbatim-duplicated mutation-id/forbidden-id
   checks and triplicated validation closures.
7. **Concrete dead code**: stale root `zicato/` pyc tree (138 files, 0 source),
   unwired 340-LOC control protocol, duplicate `telemetry/scoring.py`, abandoned
   dashboard "Variant A", legacy on-disk aliases/shims.

---

## The parity harness (build first; gates every phase)

This is the safety net that makes a behavior-preserving refactor possible.
Build it in Phase 0 and run it at the end of every sub-step.

- **Unit/integration suite**: `uv run pytest` (xdist-parallel; suite already runs
  ~18s). Must stay green at every commit. (Remember: `uv sync --all-extras`.)
- **Static gates**: `mypy` + `ruff` via pre-commit (`make install-hooks`). The
  refactor should *strengthen* mypy (fewer `Any`), so treat new type errors as
  signal, not noise.
- **Golden-output parity** against the deterministic mock target
  `examples/zicato_examples/target_1_presentation` (per AGENTS.md rule 1 — no
  live LLM runs). For a fixed seed, snapshot and byte/structurally diff before vs
  after each step:
  - `.zicato/**/loss.json`, `gen_score.json`, `experiment.json`, `lineage.json`
  - the SQLite `index.db` rows (export to a stable text dump; the index is a
    derived projection so it must reproduce exactly after `zicato reindex`)
  - analyzer report markdown + HTML, `report_figures` SVG output
  - dashboard JSON envelopes (`/api/state`, `/api/epoch`, `/api/tournaments`,
    `/api/round/.../gate`, per-judge endpoints) captured from a fixture workspace
  - `zicato <command> --help` text for every command (the CLI is the contract)
- **Rule of thumb**: a step that changes any golden output is, by definition, not
  behavior-preserving — investigate before merging.

---

## Phase 0 — Safety net + zero-risk cruft removal

Mechanical, independently shippable, no behavior change.

- Stand up the parity harness above; commit the golden baselines.
- Delete the stale root `zicato/` tree (138 `.pyc`-only files; can shadow the
  real `src/zicato/` in some tooling). *(All three auditors flagged this.)*
- Delete dead code with no production consumer:
  - `telemetry/scoring.py::aggregate_generation_score` / `combined_scalar`
    (live path is `tournament/scoring.py`; the duplicate even has a divergent
    return type — a latent footgun).
  - dashboard "Variant A": `static/variant_A_preview.html` (loads a non-existent
    `app_A.js`); collapse the dead `static/js/variants/T/` indirection (only T
    ever shipped).
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
  structure** so tournament-structure/overfitting config no longer have to live
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
  /`_overlay_*`/`_persist_field_tournament` (`:2311-2864`); pure presentation that
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
`svg/palette.py` (one palette, shared with `epoch/html_report.py` instead of
hard-copied) + one module per figure family. `report.py` (1401) → isolate the
markdown→HTML engine (`markdown_to_html:350`) into `report/markdown.py` and the
inline CSS (`_paper_css:1161`) into a data file.

**3e. `adapters/adk.py` (1068).** Extract the ~240-LOC judge-only steering
machinery (`:334-496`) into `judge_runtime/judge_only.py` (it is goldfive-steering
knowledge, not adapter knowledge). Collapse the 3 near-identical per-kind drivers
+ 3× inline `_PerTurnCaller` into one `_goldfive_call` helper + one caller.

**3f. proposer.** `prompts.py` (849) → `templates.py` (constants) + `render.py`
(block renderers) + `visibility.py` (the `_bucket_scalar_delta`/`_band_*`
overfitting-coarsening logic that isn't templating) + a typed `UserPrompt`
section-list assembler (replaces `render_user_prompt`'s 13-arg fixed-order string
prepends — golden-prompt tests required to prove byte-parity). `structured.py`
(743) → `salvage.py` (LLM-response text recovery) + `validate.py` (experiment
schema/semantic validation).

**3g. mutation + index.** `mutation/applier.py` (733) → extract the ~10
Python-literal-surgery helpers into `mutation/literal_surgery.py`. Move
`synthetic/manifest_bridge.py` into `mutation/` and make `enumerator._content_hash`
public, breaking the mutation↔synthetic circular lazy-import. `index/ingest.py`
(1753) → `ingest/upserts.py` + `ingest/rebuild.py` + `ingest/repairs.py` (the
`backfill_*`/`repair_*` one-shot migrations).

---

## Phase 4 — Boundary cleanups + invariant enforcement

Lower-frequency, higher-judgment items; do after the structure is clean.

- **Converge the two HTML report generators** (`analyzer/report.py` and
  `epoch/html_report.py`, 1270) that render overlapping epoch views and force the
  palette hard-copy.
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
- Schedule removal of legacy on-disk aliases behind a one-time migration:
  `brief.md`↔`rubric.md`, `--brief`/`--rubric`, `adk_entrypoint`,
  `round_index`/`stage_index`, `partial_*_agg`, the empty-string `contract_hash`
  "legacy, never rolls" sentinel.

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
type-check, not at runtime). Phase 3 god-objects are mutually independent and can
be parallelized across agents/PRs. Phase 4 is judgment-heavy cleanup that benefits
from the clean structure underneath.

## Verification (every phase)

Run the parity harness (top of doc) before merging any step: `uv run pytest`
green, mypy/ruff clean (and ideally *stronger* than before), and golden-output
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

---

# Part II — Data model & storage system: target design

This is the concrete target for Phase 1c/1d and Phase 2a of the roadmap above.
It is what the data model and storage system *should be*. It preserves every
on-disk artifact and every behavior; it changes only how the code is shaped
around them. A one-time `zicato migrate` (below) normalizes legacy on-disk forms
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
   joins `"epochs"`/filenames by hand — not the orchestrator, not the dashboard.
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
  loss.py           LossProfile, ExpectationResult, JudgeLoss, RunRecord,
                    RunResult.
  scoring_config.py ScoringWeights DECOMPOSED into nested typed groups:
                    DriftScoring (drift_weight, severity_weights, per_kind,
                    per_judge, default_judge_weight),
                    PassScoring (pass_weight, plan_revision_weight,
                    runtime_weight, pass_transform),
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
                    core/tournament.py records, not dict[str,Any]. This deletes
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
  the only place legacy on-disk keys are mapped (`tournament_structure`→`tournament`
  is the sole alias that *survives* migration because it is the persisted contract
  form; all others are removed by `zicato migrate`).
- Replaces: the hand-written `Heartbeat/ActiveRun/ActiveTournament*.to_dict`
  (`runtime/state.py`), the hand-written `LossProfile` serde (`reducer.py`), the
  hand-written `Experiment/Hypothesis/Outcome/EpochConfig` serde (`epoch/journal.py`,
  `epoch/lifecycle.py`), and `board/jsonl.py`'s hand serde (board keeps its JSONL
  line framing + `board_meta` header, but each line body serializes via this).
- **Closes the silent-drop bug**: because serde is field-complete by construction,
  `expected_metric_movements`/`metric_movements` can no longer be dropped.

### Identity & sentinels

- Typed IDs via `NewType` (runtime-erased, so wire-identical). Functions that
  take `epoch_id`/`generation_id`/… get real types instead of bare `str`.
- Replace the load-bearing empty-string sentinels with `None`/enum:
  `parent_generation_id: GenerationId | None` (seed = None, not `""`);
  `match_id: MatchId | None`; `contract_hash: str | None` (None = pre-hash epoch
  — the "legacy, never rolls" rule becomes an explicit `None` check, not a magic
  `""`, removing the "corrupted hash reads as legacy" hazard);
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
  tested). **One `backend_for(workspace_root)`** (delete the duplicate in
  `epoch/_storage.py` and `runtime/_storage.py`).
- **Per-domain key helpers stay** but become thin key-namespace modules
  (`storage/keys/runtime.py`, `storage/keys/epoch.py`) over the one `backend_for`.
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
  `parent_generation_id` from `Lineage`, not by re-testing `experiment.json`.
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

### Generation store (keep — already clean)

The `GenerationStore` protocol with `DirectoryGenerationStore` (copytree per gen)
and `GitGenerationStore` (content-addressed commits, worktree per run) is a good
abstraction and stays. Only change: its path math routes through `WorkspaceLayout`
like everything else. The `storage_backend` config knob (directory|git) is the one
legitimate backend choice and is retained.

### Versioning & the one-time migration

Today canonical files carry **no** version and the code tolerates every legacy
alias forever (`brief.md`↔`rubric.md`, `budget_s`↔`wall_clock_budget_seconds`,
`round_index`↔`stage_index`, `partial_*_agg`, `source_roots`↔`mutable_trees`,
`rubric_path`, the `lineage.json` `{nodes,edges}` seed shape, empty-string
sentinels). Target:

- Add `schema_version` to the workspace `config.json`.
- Ship `zicato migrate` (one-shot, idempotent): rewrites every canonical file to
  the canonical form (normalizes aliases, fills sentinels→None, converts the
  lineage seed shape to the typed DAG), bumps `schema_version`, then `reindex`.
- **Then delete all the alias-tolerance code.** The serde keeps exactly one alias
  (`tournament_structure`→`tournament`, the persisted contract key) and the
  hard-reject guards (`fires_on`, `pass_exponent`) which are correct as-is.

## How this maps to the roadmap & how it's verified

- Phase 1c = `workspace/` (layout + readers/writers) + index-as-projection.
- Phase 1d = the storage-seam collapse + single atomic primitive.
- Phase 2a = the `core/` split + `core/serde` + enums + typed IDs + typed
  `ActiveTournament`.
- `zicato migrate` + alias-code deletion is a late step (after readers/writers and
  serde land), gated like everything else.

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
3. **`migrate` rollout** — in-place rewrite vs write-new-and-swap; and whether to
   keep a read-only compatibility shim for one release before deleting alias code.
4. **Promotion source of truth** — confirm `Lineage` (recommended) over
   `experiment.json` as authoritative, and re-order the dual-write accordingly.
