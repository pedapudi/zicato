# 09 — The Dashboard & the Query Layer

> **Covers.** The whole read/serve surface: the `zicato.query` read-model
> library (every reader module — what it builds, its payload shape, its
> degrade behaviour), the standalone Starlette dashboard service
> (`server.py` / `endpoints.py` / `sse.py` / `settings_api.py` /
> `static_assets.py`), and the browser bundle under
> `dashboard/static/js/` (the SSE spine, the router/shell, the views, the
> `svg.js` figure grammar, `livestatus.js`, the pipeline stepper, controls).
> Execution records decisions and tournament progress. Shared framework
> readers assemble those records and compute requested analyses; the browser
> handles layout, filtering, navigation, and animation. An unchanged heartbeat
> leaves the rendered DOM intact.
>
> **Prerequisites.** 02-architecture.md (orchestrator vs dashboard as
> separate OS processes), 07-runtime-and-durability.md §7.1 (files
> canonical / index derived), §7.6 (the runtime state files this layer
> reads), §7.10 (the RoundLog whose fold the round timeline renders),
> 08-supervisor.md §8.9 and §8.15 (the Rust supervisor's read-only index
> discipline and its reader). 04-evaluation-statistics.md §3 (the noise
> doctrine) and §4 (A/A noise-floor calibration) ground the
> uncertainty-honest verdicts of §9.8.
>
> **Invariants introduced in this chapter.** Each is load-bearing: a violation
> is a correctness or data-integrity bug rather than a style question. The ID is
> the locator other documents cite; the Name is what prose uses.
>
> | ID | Name | Invariant |
> |----|------|-----------|
> | DQ1 | execution records decisions; the dashboard presents them | **Execution records decisions and their explanations.** Shared framework readers serve recorded results and reusable analysis. The dashboard presents those results without applying selection policy. |
> | DQ2 | one spelling per wire field | **One spelling per field on the wire.** `entry_id`, `generation_id`, `ts` (int ms epoch), `pass_fail` (`true`/`false`/`null`), `promoted` (tri-state `true`/`false`/`null`). No aliases, no bare ints the client re-interprets, no default-`false` for an undecided promotion. |
> | DQ3 | every reader is best-effort | **Every reader is best-effort.** A missing / never-built / transiently-torn input degrades to an empty-or-`None` shape (often with a `note`), never raises. No endpoint built on `zicato.query` returns a 500. |
> | DQ4 | the query layer is library code | **The query layer is library code and never imports the dashboard.** The import-linter contract "the query layer stays dashboard-free" pins it; the dashboard is a driver on top. |
> | DQ5 | change-signals carry no content | **SSE change frames carry changed regions, content revision and progress metadata.** A `state_change` is a signal to fetch rather than a payload. |
> | DQ6 | a no-op heartbeat rebuilds zero DOM | **A no-op heartbeat rebuilds ZERO DOM.** The client skips unchanged content revisions and progress cursors; a view folds a content digest (timestamps excluded) and swaps only on a real change. Node tests assert DOM-node identity across a re-serve. |
> | DQ7 | verdicts are honest about the noise floor | **Verdicts are honest about the noise floor.** Movement inside the measured A/A floor reads `no_signal` ("no detectable signal"), never "plateaued" or "improving". |
> | DQ8 | a failed read null-degrades | **Every new GET null-degrades.** When a read fails or the endpoint is absent, the client accessor returns `null` and the view paints the honest empty state, never a spinner or a crash. |
> | DQ9 | controls gate on writability | **Controls gate on `read_only:false`; a destructive control takes a two-step confirm.** A successful control write requests immediate readback; content revision also makes the change visible to other clients. |
> | DQ10 | completed rounds identify the champion | **`current_champion` is the most recent champion named by a committed round**, or the baseline before any promotion. A gate explanation includes its recorded `deciding_rule`. |
> | DQ11 | a payload-shape change is a clean break | **A payload-shape change is a clean break.** Server and client change in the same commit, client-side coalescers are deleted, and the node suite's recorded responses and the goldens are re-recorded together. |
> | DQ12 | validate an id before it touches the workspace | **An id path param is validated by `_is_safe_id` before it touches the workspace.** A malformed coordinate degrades to the empty shape at HTTP 200 — never a 500, never a traversal. |
> | DQ13 | every JSON GET has a declared contract | **Every JSON GET has a declared query contract.** `query.contracts.ENDPOINT_PAYLOADS` is the exhaustive inventory. |
> | DQ14 | ancestry and round results have separate owners | **`lineage.json` records parent relationships; committed rounds record tournament outcomes.** The lineage reader combines them to serve promotion status, including `null` for an undecided candidate. |
> | DQ15 | composite readers share walks | **Composite readers share walks.** `build_round_timeline` performs one lineage walk and hands its scoped feed to the trajectory builder; `build_environment` walks the lineage once and serves the feed verbatim. |

---

## 9.0 Map of the subsystem

The reusable reader library, `zicato.query`, assembles workspace records
for reports, command-line tools, and the dashboard. The dashboard package,
`zicato.dashboard`, provides HTTP routes and the browser interface. The reader
library does not depend on that interface.

Selection policy runs during execution. A completed round records the gate
explanations and tournament structure that readers present. Analyses such as
score trends and comparisons between judges may run on demand in shared
framework functions; callers should reuse those functions. Browser code
controls layout and interaction.

| File | What lives there | Lines |
|---|---|---|
| `src/zicato/query/__init__.py` | the package face — re-exports the readers production code calls (the endpoint table, the SSE snapshot builder, the `logs` command); `__all__` is the supported surface | 215 |
| `src/zicato/query/paths.py` | `WorkspacePaths` (the `.zicato/` layout), `read_current_epoch`, `list_epoch_ids`, `layout_of`, the coercers `coerce_float` / `finite_float` / `_opt_bool`, `_resolve_epoch_id` (the traversal guard) | 252 |
| `src/zicato/query/decisions.py` | the one decision projection: `experiment_decision`, `canonical_decision`, `promoted_tristate`, `decision_surface`, `stamp_experiment_decision`, all validated against `core.tournament.TournamentDecision` | 44 |
| `src/zicato/query/contracts.py` | typed envelopes and the exhaustive JSON endpoint registry `ENDPOINT_PAYLOADS` | 187 |
| `src/zicato/query/_sqlite.py` | `open_index_ro` / `open_index_ro_or_none` (read-only `mode=ro` through `index.query.open_index`), `_query` (swallow-to-`[]`), `_opt_json`, `_IndexAbsent`, `INDEX_NOT_BUILT_NOTE` | 107 |
| `src/zicato/query/inputs.py` | `EpochInputs` / `GenerationInputs` — the per-response captured inputs (§ "Inputs shared within a response") | 97 |
| `src/zicato/query/runtime_view.py` | `build_snapshot`, `derive_liveness`, `read_heartbeat_dict` (the `ts` int-ms stamp), `read_effective_settings`, `normalize_entry_status` (the four-bucket canon), `read_active_runs_view`, `read_paused` | 831 |
| `src/zicato/query/loop_view.py` | `build_optimization_trajectory` (the uncertainty-honest verdict), `build_tournament_cost`, `build_round_pipeline` + `PIPELINE_STEPS` (the server-owned stepper projection) | 545 |
| `src/zicato/query/racing_view.py` | `build_racing_field` — the most recent recorded racing field tournament of an epoch, served from its record | 39 |
| `src/zicato/query/rounds_view.py` | `build_round_timeline` — the recorded rounds, the in-flight field and the loss-floor waterfall | 356 |
| `src/zicato/query/promoted_head.py` | `current_champion`, `champion_history`, `read_recorded_heads`, `head_of_round` — the champion each committed round names | 71 |
| `src/zicato/query/file_view.py` | `build_file_index`, `build_generation_tree`, `read_generation_file`, `build_generation_patches`, `build_generation_diff` — a generation's source tree, one file of it, its patch set, and its diff against its parent, read through the `GenerationStore` protocol; when a tree is gone the diff carries the patched spans reconstructed from records, with `provenance` saying so | 515 |
| `src/zicato/query/mutation_view.py` | `build_mutation_index`, `build_mutation_detail`, `reconstructed_spans` — an epoch's mutation surface from the `v0` tree or, when the tree is gone, from the frozen `mutations.json`, and one site's content in every generation whose patch touched it | 786 |
| `src/zicato/query/reflection_view.py` | `list_reflections`, `build_reflection_summary` (four-pillar bill of health), `build_judge_scorecards`, `build_practice_review`, `build_adjudication_xray` (transcript + judge verdict + meta-judge record), `entry_candidate_matrix` (reflection-independent, off the index loss tables) — the Instrument-lens feed. Index-first, file-fallback; the x-ray reads `result.json` / `judge_io` rather than re-running the adjudicator's events-preview reconstruction | 590 |
| `src/zicato/query/trace_view.py` | the trajectory-bootstrap traces of a reflection: `build_trace_list`, `build_trace_detail`, `build_suggestion_provenance` | 809 |
| `src/zicato/query/epoch_view.py` | `build_epoch_view`, `build_epochs_summary`, `compute_board_split`, `build_epoch_analysis`, `read_epoch_analysis_html` | 793 |
| `src/zicato/query/gate_view.py` | `build_gate_breakdown` (+ `deciding_rule`), `build_score_trajectory`, `build_health_report`, `build_rating_view`, `build_drift_movements` | 1052 |
| `src/zicato/query/tournament_view.py` | `build_bracket`, `build_tournament_structure`, `build_matchup_detail`, `build_matchup_grid` | 991 |
| `src/zicato/query/candidate_view.py` | `build_candidate_dossier` — the candidate page's one composed read | 457 |
| `src/zicato/query/eval_view.py` | the board-as-instrument reads: `build_eval_matrix`, `build_eval_dossier`, `build_eval_health`, `facet_scores_for_generation` | 1527 |
| `src/zicato/query/execution_plan.py` / `live_execution_plan.py` | `build_execution_plan` (the loop as one served tree) and `build_live_execution_plan` / `build_live_pipeline` (the running epoch's plan and stepper) | 1402 / 558 |
| `src/zicato/query/{judge,hypothesis,lineage,ledger,transcript,conversations,journal,proposer}_view.py`, `events_index.py`, `run_log.py`, `log_stream.py`, `judge_roster.py`, `replicate_scores.py`, `ratings.py`, `board_scan.py` | per-judge matrices and `build_environment` (`judge_view`), hypothesis/calibration accuracy, the lineage feed, the experiments ledger, run transcripts and episode exports, matchup conversations, the served journal, the proposer scorecard, run and events lookup plus `build_workspace_view` / `build_meta_loop_ledger` (`events_index`), the run-log tail, the operator log view, the armed judge roster, replicate measurements, rating triples, and board-row projections. `judge_view.build_per_entry_for_generation` serves the dossier; its `facet_scores` block comes from `eval_view.facet_scores_for_generation` | — |
| `src/zicato/query/transcript_reconstruction.py` | `reconstruct_transcript` — one goldfive `events.jsonl` or one Foe `episode.jsonl` → an ordered `Transcript` | 921 |
| `src/zicato/query/foe_episode.py` | Reads proposal episode events and their conversation contributions; request messages remain recorded facts | 192 |
| `src/zicato/board/jsonl.py` | `load_board_document` owns whole-file board acceptance; query projections share its accepted entries and source rows. | — |
| `src/zicato/mutation/inventory.py` | `read_mutation_inventory` accepts the recorded seven-field enumeration and preserves extensions; malformed present inventories carry a refusal into query views and prevent report publication. | — |
| `src/zicato/epoch/contract.py` | `read_component_hashes` accepts one string-to-string mapping for checks, epoch rollover and query projections; future component names remain recorded. | — |
| `src/zicato/dashboard/server.py` | `create_app` (routes + `read_only`), `run` (port walk + harmonograf + `read_only=False`), static serving with ETag revalidation | 546 |
| `src/zicato/dashboard/endpoints.py` | `READ_ENDPOINTS` (the read-route table), `make_endpoints` (the table plus the hand-written factories), `_is_safe_id` / `COORDINATE_GUARDS`, the control POST handlers | 1488 |
| `src/zicato/dashboard/settings_api.py` | `settings_routes` — the secret-safe `GET /settings/models` read of the model-engine configuration | 56 |
| `src/zicato/dashboard/sse.py` | `ChangeBroker` (coalescing file watcher), `sse_event_stream`, `_classify`, `_progress_signal` | 430 |
| `src/zicato/dashboard/static_assets.py` | `resolve_static_dir` — the bundle-resolution seam | 50 |
| `src/zicato/dashboard/static/js/core/` | `sse.js` (the seq gate), `api.js` (`postControl`), `state.js` (`noteProgress`, `AppState`), `prefs.js`, `harmonograf.js`, `dom.js`, `bus.js` | — |
| `src/zicato/dashboard/static/js/` | `router.js`, `shell.js` (dispatch + chrome + loop controls), `live.js` (the live engine + `pipelineStepper`), `livestatus.js` (the four run-states), `data.js` (null-degrading accessors), `svg.js` (the figure grammar), `ui.js` (`gatedSwap`) | — |
| `src/zicato/dashboard/static/js/views/` | one module per page: `home.js`, `epoch.js`, `gens.js`, `candidate.js`, `board(s).js`, `evals.js`, `mutations.js`, `instrument.js` (the board-reflection lens — landing / bill-of-health / judge-audit / x-ray), `traces.js`, `diff.js`, `logs.js`, `publication.js`, `settings.js`, each an `async render(host, ctx, params)`; `structure.js`, `boardstatus.js` and `ledger.js` are panels the epoch page composes | — |
| `src/zicato/dashboard/static/js/panels/` | page sections a view imports and mounts into hosts it owns, with no route: `evals_health.js` (the evals page's instrument-health strip and section) | — |


Two orientation facts before anything else:

- **The dashboard is a separate OS process.** `zicato evolve` spawns it
  for the lifetime of a loop; `zicato dashboard` runs it standalone over a
  finished workspace (a post-mortem). It reads the same `.zicato/` files
  the orchestrator and the Rust supervisor read — it is one of three
  independent readers of the runtime state (07-runtime-and-durability.md
  §7.6).
- **This service is the only dashboard server.** The Rust supervisor
  (08-supervisor.md) serves only its watchdog status routes, so every
  dashboard read API and the browser bundle come from here.

---

Saved round health is owned by `health.diagnostics.LoopHealth`, including its
epoch, round, assessment timestamps and derived summary. Readers verify recorded
coordinates against the selected file. An absent report retains the empty state;
a malformed present report returns `healthy: null` with an `unreadable` reason.
Accepted extension fields and historical metadata omissions survive serialization.
The browser renders that reason and includes finding content in its repaint digest.

The bound dashboard address in `runtime/dashboard.json` is owned by
`runtime.state.DashboardEndpoint`. The service publishes it atomically and the
launching command uses the same decoder. An absent or malformed convenience
record remains unavailable; it does not establish service readiness.

## 9.1 The library / driver split — `query` is a library, `dashboard` is a driver

The single most load-bearing structural fact about this subsystem: the
readers are **library code** with no dashboard dependency. The package
docstring states it and the import-linter enforces it.

```python
"""The workspace query layer: read-only ``.zicato/`` state assembly.

Library code: these readers turn the on-disk workspace
(runtime state files, the SQLite analytical index, epoch records) into
the JSON view shapes any consumer can render. The dashboard server is
the primary consumer today, but the layer has no dashboard dependency —
:mod:`zicato.query` must never import :mod:`zicato.dashboard` (enforced
by the import-linter contracts).
...
"""
```
— `src/zicato/query/__init__.py` (module docstring)

The enforcement is a forbidden-import contract (see 11-testing.md §11.8):

```
[[tool.zicato.importlinter.contracts]]
name = "the query layer stays dashboard-free"
type = "forbidden"
source_modules = ["zicato.query"]
forbidden_modules = ["zicato.dashboard"]
```
— `pyproject.toml`

**Why this matters.** The readers live outside the driver, in per-view
submodules, so that (1) tests and the CLI can exercise the read model
without booting a server, and (2) the readers cannot accrete an HTTP concern
by accident. `__init__.py` re-exports the readers production code calls, so
the endpoint table writes `query.build_epoch_view`; a name absent from
`__all__` is imported from the submodule that defines it.

> ⛔ NEVER import `starlette`, `Request`, `JSONResponse`, or anything under
> `zicato.dashboard` from a `zicato.query` module. A reader returns plain
> Python (`dict` / `list` / scalars); the endpoint wraps it in a
> `JSONResponse`. The moment a reader knows about HTTP, the query layer has stopped being library code and
> `make import-lint` reds.

> ✅ ALWAYS add a new reader to `zicato.query` (a per-view submodule) and
> re-export it from `__init__.py`'s import block AND `__all__`. The endpoint
> in `zicato.dashboard.endpoints` is a one-line wrapper over it. If you find
> yourself writing workspace-reading logic inside `endpoints.py`, you have
> put library code in the driver — move it down.

The one declared driver-to-driver edge is `cli → dashboard` (launch and
static-asset resolution); the dashboard must not import the CLI (§9.4,
11-testing.md §11.8). The endpoints module's own docstring restates the split
from the top:

```python
"""HTTP route handlers for the dashboard service.

Each handler reads the live ``.zicato/`` workspace through
:mod:`zicato.query` and returns a JSON shape the
dashboard front-end consumes. ``/api/environment`` is the consolidated
read of the whole environment; the granular per-section endpoints are
kept alongside it.
"""
```
— `src/zicato/dashboard/endpoints.py` (module docstring)

---

### Inputs shared within a response

The environment response captures the current epoch marker, heartbeat,
active runs, lock, active tournament, progress-log tail, and clock once.
Workspace identity and liveness use those observations. The state response
uses the same runtime capture and preserves an absent epoch instead of
resolving the marker again while assembling its contract.

The epoch overview and candidate dossier capture the selected epoch's
config, scoring, and generation records in `query.inputs.EpochInputs`.
The journal owner returns each accepted stored experiment body together
with the patches that body declares. Component builders receive explicit
inputs and independent copies of mutable JSON values. Each captured value is
encoded once as immutable JSON and decoded separately for each consumer.
Captures retain absence and read errors for the response lifetime. They are neither a
cross-file transaction nor a process cache; the next request reads again.

The polled epoch response carries report Markdown and standalone HTML
availability. The publication view fetches `/api/epoch/{epoch_id}/analysis`,
whose `analysis_html_inline` field reads the saved `analysis.fragment.html`.
The query performs no report assembly, measurement gathering, or figure rendering.

The report writer combines measured data with authored sections stored in
`analysis.prose.json`, then publishes Markdown and both HTML forms. The browser
displays the saved fragment and adds interactive tournament figures and
per-matchup tables. An absent fragment produces an unpublished notice while
available tournament figures remain visible. The redraw digest includes the
full text and figure inputs, so equal-length corrections update the display.

Generation identity includes the epoch. The overview serves
`champion_record` with its epoch, generation, recorded decision, and rating
uncertainty. Terminal Home renders that record and includes its decision
and rating in the content digest. A missing index leaves rating fields
null. Proposal lookup with a named epoch returns no episode when that
epoch has none, even if another epoch holds the same generation name.
Entry selection and rating inclusion remain explicit view arguments.

## 9.2 The server-authority doctrine

The client is a **renderer**. Every classification, every join across
records, every "which one is the champion" decision is computed on the
server, serialized once, and rendered verbatim. This is not an
aesthetic preference — it is the fix for a whole bug class (the client
champion-scan case, `12-bug-casebook.md` case 4) and
the reason the two servers (Python + Rust) can agree.

### 9.2.1 Bug #4 — the client champion-scan (first vs reigning)

A browser scan once selected the first promoted candidate instead of the
reigning champion. Promotion flags also cannot identify the primary candidate
when one round retains several promising candidates.

The completed round record names the primary promotion. The framework reads
committed rounds in order through `recorded_champions` in
`src/zicato/epoch/settlement_receipt.py`. The history starts at the baseline,
`v0`; the last recorded primary is the reigning champion. Query readers and
reports use these identities directly. The browser receives `current_champion`
and draws the recorded answer.

The candidate comparison supplies the unweighted mean of paired task score
deltas, the number of compared tasks, and pass counts. These are observations,
separate from the weighted aggregate and statistical checks used for promotion.
The gate tooltip displays the evaluator's recorded decision, rule, and reason.
Live completion bars use published completion counts; activity within a running
task does not count as another completed board entry.

> ⛔ NEVER re-derive "the champion", "the winner", "the latest generation",
> or a decision on the client from a list the server already ordered. This
> is server-computes-client-renders, and the client champion-scan case (`12-bug-casebook.md` case 4) is what breaks it. If the client needs to know the reigning champion,
> the server ships `current_champion`; if it needs the deciding rule, the
> server ships `deciding_rule`. The client's job is to draw the answer, not
> compute it.

> ⚠️ TRAP — "first with a promotion" and "reigning champion" read
> identically on any epoch that has promoted exactly once, so a client-side
> scan LOOKS correct in every single-promotion fixture. Bug #4 only surfaces
> on a multi-promotion epoch — which is exactly the epoch an operator cares
> about. A regression test for a champion-selection change MUST use a
> two-promotion lineage (see 11-testing.md §11.15), and
> when the two promotions are SIBLINGS the head is not derivable from the
> lineage flags at all — it is the one the runner recorded
> (`src/zicato/query/promoted_head.py`; the fixture is
> `tests/test_dashboard_promoted_head.py`).

> ⚠️ TRAP — the same bug wears a SECOND costume: the client reads the
> server's answer, but reads it for the WRONG EPOCH. A bare `D.epoch()` /
> `D.bracket()` / `D.scoreTrajectory()` answers for the CURRENT epoch, so a
> surface that paints more than one epoch must take the `?epoch=<id>`-scoped
> read PER epoch node. `buildTreeModel` held ONE bare-read champion pointer and
> gated the crown on "is this the contract epoch". That gate tests CURRENT, not
> closed, so it marked every OTHER epoch's reigning champion a FORMER champion
> and crowned nothing there. Deciding "this epoch has no champion" is still a
> client decision. A multi-epoch fixture is the only thing that catches it:
> with one epoch the bare read and the scoped read are the same payload.

### 9.2.2 The one decision projection — `decisions.py`

Every payload that names a tournament decision funnels through ONE module
so the wire vocabulary is single-valued and the client never re-classifies.
The vocabulary itself belongs to execution: `core.tournament.TournamentDecision`
is a `StrEnum` with exactly `promoted`, `rejected` and `deferred`, and a
recorded token outside it is refused rather than guessed.

```python
def experiment_decision(exp: dict[str, Any]) -> str | None:
    """Read the recorded outcome decision, or None before an outcome exists."""
    return recorded_decision_token(exp.get("outcome"))


def canonical_decision(raw: str | None) -> str | None:
    """Validate a recorded decision against the tournament's supported tokens."""
    return TournamentDecision(raw).value if raw is not None else None
```
— `src/zicato/query/decisions.py`

`experiment_decision` is the one reader of the raw shape: it delegates to
`core.tournament.recorded_decision_token`, which reads `outcome` as an object
carrying `tournament_decision` (or `null` before the outcome exists) and
raises on any other shape. So "where is the decision written" lives in one
place, and a malformed record surfaces as an error the reader's best-effort
degrade handles rather than as an invented verdict.

### 9.2.3 The tri-state `promoted` stamp

`promoted` on the wire is a **tri-state**: `true`, `false`, or `null`. The
`null` case is its own bug class:

```python
def promoted_tristate(raw: str | None) -> bool | None:
    """Preserve absent decisions and identify a recorded promotion."""
    decision = canonical_decision(raw)
    return decision == TournamentDecision.PROMOTED if decision is not None else None
```
— `src/zicato/query/decisions.py`, `promoted_tristate`

> ⛔ NEVER default `promoted` to `False` for a generation with no recorded
> decision. An in-flight or never-raced challenger is `promoted: null`, not
> `promoted: false` — collapsing "not yet decided" into "rejected" is the
> defect this stamp exists to prevent. A `false` says the gate ran and said
> no; a `null` says the gate has not run. The dashboard colours those
> differently (a pending accent vs a rejection tone), and a placebo or
> soft-reject audit reads them differently too.

The server stamps both the canonical token and the tri-state onto every
experiment record it serves, in place, so no consumer re-classifies:

```python
def stamp_experiment_decision(record: dict[str, Any]) -> None:
    """Stamp ``decision`` (canonical token) + ``promoted`` (tri-state) in place."""
    raw = experiment_decision(record)
    record["decision"] = canonical_decision(raw)
    record["promoted"] = promoted_tristate(raw)
```
— `src/zicato/query/decisions.py`, `stamp_experiment_decision`

`decision_surface(parent, promoted)` gives a lineage node its token and
renderer label: `baseline` ("seed (v0)") for a parentless node, `promoted`,
`rejected`, or `pending` ("undecided") for a `null` stamp.

### 9.2.4 The schema canon — one spelling per field

The wire has ONE spelling for each field, chosen so the client never
re-interprets or coalesces. Four canonical spellings, each with a single
enforcing helper:

| Field | Wire shape | Enforced by | The rule |
|---|---|---|---|
| `pass_fail` | `true` / `false` / `null` — never `0`/`1` | `paths._opt_bool` | the SQLite index stores 0/1 ints, `loss.json` stores real bools; every payload emits a JSON boolean |
| `ts` (liveness) | integer **milliseconds** epoch, or `null` | `runtime_view._heartbeat_ts_ms` | one typed field; no ISO parsing, no sec-vs-ms magnitude guessing, no alternate keys on the client |
| `promoted` | tri-state `true`/`false`/`null` | `decisions.promoted_tristate` | §9.2.3 |
| entry `status` | one of `queued`/`running`/`done`/`failed` | `runtime_view.normalize_entry_status` | every producer spelling collapses to four buckets at the single read site |

The `pass_fail` coercer's own docstring states the rule:

```python
def _opt_bool(value: Any) -> bool | None:
    """Coerce a stored pass/fail flag to a JSON boolean (or ``None``).

    ONE spelling on the wire: the SQLite index stores 0/1 ints, loss.json
    stores real booleans — every payload emits ``true`` / ``false`` /
    ``null``, never a bare int the frontend has to re-interpret.
    """
    if value is None:
        return None
    return bool(value)
```
— `src/zicato/query/paths.py`, `_opt_bool`

The `ts` timestamp is stamped server-side from the ageable
`last_heartbeat`, and the client reads THAT field alone:

```python
    # THE one typed liveness timestamp: `ts`, integer MILLISECONDS since the
    # epoch, stamped server-side from the ageable `last_heartbeat`. The
    # frontend ages the heartbeat off THIS field alone — no ISO parsing, no
    # sec-vs-ms magnitude guessing, no alternate keys.
    out["ts"] = _heartbeat_ts_ms(out["last_heartbeat"])
```
— `src/zicato/query/runtime_view.py`, `read_heartbeat_dict`

The client half of the same contract, verbatim — the "alternate keys are
DELETED" line is the anti-alias rule made real:

```javascript
// The heartbeat's ONE typed liveness timestamp: `ts`, integer MILLISECONDS
// since the epoch, stamped SERVER-SIDE (both the Python reader and the Rust
// supervisor derive it from `last_heartbeat`). The old sec-vs-ms magnitude
// guessing + the four alternate keys are DELETED — a heartbeat without a
// numeric `ts` has no ageable timestamp and reads STALE, never fresh.
function heartbeatTs(hb) {
  const v = hb ? hb.ts : null;
  return (typeof v === 'number' && isFinite(v)) ? v : NaN;
}
```
— `src/zicato/dashboard/static/js/livestatus.js`, `heartbeatTs`

The entry-status canon is the four-bucket collapse at the single read site,
so a run the orchestrator wrote as `completed` can never fall through a
`status === 'done'` client comparison and paint as `queued`:

```python
_ENTRY_STATUS_CANONICAL = {
    "queued": "queued",
    "pending": "queued",
    "running": "running",
    "in_progress": "running",
    "active": "running",
    "done": "done",
    "complete": "done",
    "completed": "done",
    "finished": "done",
    "cached": "done",
    "failed": "failed",
    "fail": "failed",
    "error": "failed",
    "aborted": "failed",
}
```
— `src/zicato/query/runtime_view.py`, `_ENTRY_STATUS_CANONICAL` (excerpt)

`normalize_entry_status` maps any producer's spelling to one of the four,
degrading an unknown/absent value to `"queued"` (the safe pre-start
default), and `_normalize_tournament_statuses` preserves the producer's
exact spelling as `status_raw` alongside so a post-mortem can still tell
`aborted` from `error`.

> ⛔ NEVER add a second spelling for a field the client reads. If a new
> producer writes `completed`, add it to `_ENTRY_STATUS_CANONICAL`, do NOT
> teach the client a `status === 'completed'` branch. If the index stores a
> new pass flag as an int, route it through `_opt_bool`, do NOT emit the int.
> Every alias you let onto the wire is a place the two servers can disagree
> and a coalescer the client has to grow. One spelling per wire field keeps both at zero.

### 9.2.5 Served joins — the server owns every cross-endpoint join

Four payloads each require stitching several records together. The server
performs every one of those joins and serves the result as a single payload,
and the client reads it whole. Holding the join on one side is what removes a
class of client-versus-server drift.

**The racing field.** Execution records each field tournament, rungs and
final comparison included, in one field-tournament record.
`build_racing_field` selects the epoch's most recent racing record (by
`ran_at`) from `tournament.records.field_tournament_records` and serves it
whole, adding `present: true`, `source: "record"`, and `champion_lineage`
from `champion_history`. It infers no winner:

```python
def build_racing_field(
    paths: WorkspacePaths, epoch_id: str | None = None, *, inputs: EpochInputs | None = None
) -> dict[str, Any]:
    """Serve recorded rungs and the final comparison without inferring a winner."""
```
— `src/zicato/query/racing_view.py`

An epoch with no racing record serves `{epoch_id, present: false}`; a
malformed record adds its `unreadable` reason.

**The round timeline.** The round model combines the lineage feed, the
score trajectory, the bracket's recorded tournaments, the recorded heads and
the active tournament. `GET /api/epoch/{epoch_id}/round-timeline` serves that
join for both settled and in-flight rounds, and the client only renames
fields for the renderer:

```python
"""The epoch's recorded rounds, active proposal status and loss-floor waterfall.

An experiment's integer birth-round stamp assigns its generation to a field.
The seed and each promoted champion carry forward into later fields. Lineage
supplies parentage and promotion decisions; tournament records identify the
champion and gate winner within each field.
...
"""
```
— `src/zicato/query/rounds_view.py` (module docstring)

Each round's `champion` is `{id, scalar, eval_mode, run_ref, from_record}`.
`build_bracket` takes the `id` from the field-tournament record's
`champion_generation_id`, and the scalar and `eval_mode` (cached or re-run)
from that round's committed settlement receipt. Before the receipt commits,
the round's defender is known but its evaluation is not, so `scalar` and
`eval_mode` are `None` and the tree renders plain "defends". The timeline
also carries the champion forward from each round's recorded head
(`head_of_round`); when the record and the carried champion disagree, the
reader logs the disagreement and serves the record.

> ⚠️ TRAP — one champion defends several rounds, so a reader that searches
> every round for "a row naming this champion" finds its EARLIEST defence and
> reports that round's scalar and cached-versus-fresh mode. Key the champion's
> evaluation to the round being rendered. A regression test for this needs a
> round AFTER a promotion; with a single round the bug is invisible.

**The pipeline projection.** The propose→apply→run→gate position is inferred
server-side from the heartbeat `phase` string (§9.11), and the JS renders the
resulting verdict verbatim.

**Recorded elimination results.** Tournament execution publishes ordered rounds,
per-match losers, bracket sides, and each candidate's progression in `gen_states`.
The publication helper is `tournament/structure.py::attach_elim_states`.
Both live updates and completed tournament records include its output. The
query service serves those recorded values. It does not sort matches, remove
duplicates, or infer eliminations.

In double elimination, a first loss in the winners' bracket leaves a candidate
eligible for the losers' bracket, including before that match is scheduled.
A loss in the losers' bracket ends participation. The final match records its
loser separately from the champion promotion decision. The radial diagram
renders these facts directly. Tests exercise publication against the recorded
brackets in `tests/data/elim_states_cases.json` and `elim_states_served.json`.

**Recorded measurements.** Candidate task rows, matchup grids and task views
share measurement reading and validation. Ordinary task rows use the candidate's
selected seed. Uncertainty summaries use accepted tournament and confirmation
draws. Diagnostic probes remain separate. These rows are available before an
index is built; the index still supplies derived ratings and historical judge
aggregates whose scope includes more than the selected ordinary measurements.

The candidate page's per-candidate reads are one such join as well:
`query.build_candidate_dossier` (`src/zicato/query/candidate_view.py`) calls
the per-entry, scorecard, episode, grid, gate, comparison, drill-down and
racing-field readers and serves the result on
`/api/epoch/{epoch_id}/candidate/{generation_id}`, so `views/candidate.js`
reads one payload per candidate and recomputes no verdict.

The dossier compares the parent declared by canonical lineage, the captured
experiment, and its settlement receipt before composing parent-dependent
comparisons. Parent identity includes the epoch: `source:v0` and `selected:v0`
are different candidates. A conflict or unreadable authority produces
`parent_inconsistency` and suppresses gates, matchup grids, and hypothesis
comparisons. The candidate's own records remain available for inspection.
Clients display that reason without selecting a parent themselves. External
baseline ancestry remains visible through `parent` and `parent_epoch_id`;
a baseline experiment may record no parent within its own evaluation contract.

The node suite derives none of these joins either. `static/test/recorded.mjs`
serves the responses `tests/data/endpoint_route_snapshot.json` records over
the workspaces `tests/_console_scenarios.py` writes, keyed by the URL in
`tests/data/endpoint_route_probes.json`, and serves the elimination fold from
`tests/data/elim_states_served.json`; a browser test therefore renders a join
a Python endpoint produced (§9.16 step 3, 11-testing.md §11.9.3).

> ✅ ALWAYS move a multi-record join to the server the moment the client
> starts stitching ids or walking more than one endpoint's payload to build a
> view. The three joins above each deleted a client/server drift class. The
> server's answer is the one both the Python and the Rust servers can produce
> identically; a client-side join is a fourth implementation nobody keeps in
> sync.

### 9.2.6 Execution records the explanation of each gate result

The numerical evaluator, `tournament.gate.evaluate_gate`, records each rule's
result as it applies the rule. The runner adds the actual regression-suite
result. The resulting `GateOutcome.explanation` includes the decision, reason,
rule results, scalar margin, compared scalars, and differences between scores.

Round publication stores these explanations in `field_settlement.json` under
`gate_results`, alongside the compared training aggregates and the identities
of both candidates. The gate reader, `query.gate_view.build_gate_breakdown`,
selects the recorded comparison for the requested pair. It does not call the
evaluator. A candidate's final recorded outcome also supplies the effect of
later confirmation, integrity checks, or an operator override.

The response's `deciding_rule` identifies the recorded rule that prevented
promotion. A successful gate or an unavailable explanation has
`deciding_rule: null`; the decision and record availability distinguish those
cases. Missing records leave `rules` empty. Available scores may still support
live progress displays, but cannot establish a gate result.

### 9.2.7 Tournament diagrams use recorded matches and standings

Every executed tournament records its competitors, matches, results, and
standings, including a tournament with one champion and one challenger.
Completed rounds supply the authoritative structure. Runtime records supply
progress during execution, and SQLite stores a derived copy for queries.

The structure reader, `query.tournament_view.build_tournament_structure`,
serves a complete tournament or selects its recorded matches for a requested
candidate pair. Missing structure remains unavailable; per-run loss files do
not establish pairing, elimination, or promotion. The browser renders the
served bracket, racing ladder, standings, and candidate details.

---

## 9.3 The reader library API

Every reader is a pure function `build_*(paths, ...) -> dict|list`.
`paths` is a `WorkspacePaths` (the `.zicato/` layout object). They share
one contract — every reader is best-effort — and one small set of
primitives.

### 9.3.1 The best-effort degrade contract

The package docstring states it once for everyone:

> Every function here is best-effort: a missing or transiently-truncated
> file degrades to an empty / `None` value rather than raising, so no
> endpoint built on top of this ever returns a 500.

View builders catch unavailable input and preserve the response shape.
A missing board remains absent; a malformed present board is refused in full
with an `unreadable` reason. Execution, hashing, workspace readers and views
share `board.jsonl` acceptance. Epoch and search responses project entries,
metadata and judges from one accepted observation, preserving source omissions
and extensions. The brief owner reads only `brief.md`; a retired filename
cannot supply missing guidance.

The loop view also explains unavailable input:

```python
    try:
        traj = optimization_trajectory(paths.index_db, epoch_id)
    except IndexUnavailableError:
        return _empty_trajectory(paths, epoch_id, INDEX_NOT_BUILT_NOTE)
    except Exception:  # noqa: BLE001 — best-effort, mirrors sibling readers
        return _empty_trajectory(paths, epoch_id, "index unreadable")
```
— `src/zicato/query/loop_view.py`, `build_optimization_trajectory`

> ✅ ALWAYS return the SAME shape from the degrade path as from the happy
> path — same keys, same types, empty values. `_empty_trajectory` carries
> `points: []`, `promotion_rate: None`, `verdict: None`, AND the measured
> noise floor (which is read off the epoch config, independent of the index,
> so it survives a degraded read). A view that reads `payload.points.length`
> must never hit `undefined` because the reader shortened its shape on
> failure — that is how a "best-effort" reader turns into a client crash.

> ⚠️ TRAP — a bare `except Exception` in a reader is CORRECT here (it is the
> best-effort contract) but it is the one place ruff's `BLE001` fires; every reader
> carries the `# noqa: BLE001 — best-effort` marker so the blanket-except is
> a documented decision rather than an accident. Do not "tighten" it to a specific
> exception type — a never-built index, a torn read, and a schema-newer
> database must ALL degrade, and you cannot enumerate every failure a
> future SQLite/file layout can throw.

### 9.3.2 The record enumerations — `zicato.workspace.reads`

Almost every reader in this chapter starts by asking which records to read:
which generations the epoch minted, which board entries left a run, which
rounds ran. None of them answers that itself. `zicato.workspace.epochs` holds
the epoch enumeration and `zicato.workspace.reads` the other three, each with
the one order it carries:

| Reader | Question | Order |
|---|---|---|
| `iter_epochs(layout)` / `list_epoch_ids(layout)` | which epochs exist | recorded creation time, numeric-aware id as tiebreaker |
| `generation_ids(layout, epoch_id)` | which generations have a record | numeric-aware: `v2` before `v10` |
| `run_entry_ids(layout, epoch_id, generation_id)` | which board entries have a run record | numeric-aware: `t2` before `t10` |
| `round_indices(layout, epoch_id)` | which rounds have a directory | ascending integer |

`layout` is a `WorkspaceLayout` — `layout_of(paths)` converts the
`WorkspacePaths` a reader is handed. The enumerations read through
`StorageBackend.list_namespaces`, the listing for records stored as a
directory of files; 07-runtime-and-durability.md §7.3.4 has the storage side
and the reason `list_keys` cannot answer.

Two behaviours matter when composing a view on top of them. A record
directory with no readable leaf file is still enumerated, because the
directory is what makes the record exist — so a generation from an
interrupted round appears in `generation_ids` and drops out of
`epoch/journal.py`'s `read_epoch_experiments`, which is what lets a view
distinguish "in flight" from "never proposed". And an epoch or generation id that could not be a legal
storage key enumerates nothing rather than resolving to a path outside the
workspace, which is a third line of defence behind the two below.

> ⛔ NEVER walk `generations/`, `runs/` or `rounds/` from a view. The
> orderings diverged exactly this way before consolidation, and
> `tests/test_record_enumeration_single_owner.py` now fails the commit that
> reintroduces one.

### 9.3.3 `WorkspacePaths` and the traversal guard

`WorkspacePaths(root)` (`src/zicato/query/paths.py`) is the typed
`.zicato/` layout — `root` is the `.zicato` directory itself, with
properties for every file the readers touch (`heartbeat`, `lock`,
`active_runs_dir`, `active_tournament_log`, `progress_log`, `control_dir`,
`lineage`, `index_db`, `epochs`). It carries the resolved persistent
`harmonograf_url` the dashboard process injected at startup (§9.4).

The one security-relevant helper is `_resolve_epoch_id`. `None` resolves to
the current epoch; a given `?epoch=<id>` must be a single path component
naming an existing epoch directory, so a `?epoch=../foo` cannot escape the
workspace:

```python
    if (
        not isinstance(epoch_id, str)
        or not epoch_id
        or "/" in epoch_id
        or "\\" in epoch_id
        or epoch_id in (".", "..")
        or "\x00" in epoch_id
    ):
        raise ValueError(f"invalid epoch id: {epoch_id!r}")
    if not layout_of(paths).epoch_dir(epoch_id).is_dir():
        raise ValueError(f"unknown epoch id: {epoch_id!r}")
    return epoch_id
```
— `src/zicato/query/paths.py`, `_resolve_epoch_id`

This is the SECOND line of defence behind `_is_safe_id` in the endpoint
(§9.5); the endpoint rejects a malformed coordinate before it reaches the
reader, and the reader re-validates against the actual epoch set.

### 9.3.4 The coercer — `coerce_float`

`coerce_float` is THE numeric payload coercer, and it excludes bools (a
stray `True` is not a scalar); `finite_float` additionally drops NaN and
infinities:

```python
def coerce_float(value: Any) -> float | None:
    """``float(value)`` for a real number, else ``None``.

    THE one numeric payload coercer, with bools excluded because a stray
    ``True`` is not a scalar. Every reader coerces through this function rather
    than an inline ``float(x) if isinstance(x, int | float) else None``.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)
```
— `src/zicato/query/paths.py`, `coerce_float`

The other normalization the readers depend on lives outside this package.
`zicato.telemetry.event_log.to_snake` folds a `camelCase`/`PascalCase`
event key to `snake_case`, so event kinds key on ONE stable vocabulary
across every reader:

```python
def to_snake(name: str) -> str:
    """Convert a ``camelCase`` / ``PascalCase`` identifier to ``snake_case``.

    An underscore goes before each uppercase ASCII letter that follows a
    lowercase letter or a digit; every other character is copied through with
    uppercase folded down. Input already in snake_case is unchanged, and the
    conversion is idempotent, so a file mixing both spellings normalizes to
    one vocabulary.
    ...
    """
```
— `src/zicato/telemetry/event_log.py`, `to_snake` (docstring)

> ⚠️ TRAP — every event-log consumer keys on `to_snake`'s output. The
> transcript reconstructor (`query/transcript_reconstruction.py`) reuses this
> exact helper, so a change to the rule renames event kinds in every consumer
> at once and moves the recorded responses and goldens that carry them.

### 9.3.5 The read-only index open

Every SQLite read goes through `open_index_ro` (or its best-effort variant
`open_index_ro_or_none`), which opens the index **read-only** through
`index.query.open_index` — URI `mode=ro`, a schema-version check, the
`sqlite3.Row` factory and a busy timeout — and guarantees the close. A
query error folds to an empty result, so a mid-rebuild database cannot raise
into a reader:

```python
def _query(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> list[sqlite3.Row]:
    try:
        return list(conn.execute(sql, params))
    except sqlite3.Error:
        return []
```
— `src/zicato/query/_sqlite.py`

A missing index file raises `_IndexAbsent`, so a reader can distinguish
"never built" (attach `INDEX_NOT_BUILT_NOTE`, "index not built; run zicato
repair index", through `with_index_not_built_note`) from "unreadable" (attach
the generic note) — the two degrade notes in §9.3.1. Never
`sqlite3.connect()` an index path directly in a reader: a bare connect
defaults to write mode and contends with the ingest writer.

### 9.3.6 The composite reads

Two readers coalesce the whole environment so the client fetches once, not
six times:

- **`build_snapshot(paths)`** — the `/api/state` snapshot AND the opening
  SSE `snapshot` frame. It captures the runtime inputs once
  (`RuntimeInputs`) and composes heartbeat + liveness + lock + active runs +
  active tournament + lineage + epoch view + `paused` from that one capture:

```python
    return {
        "heartbeat": inputs.heartbeat.copy(),
        "liveness": derive_liveness(paths, inputs=inputs),
        "lock": inputs.lock.copy(),
        "active_runs": inputs.active_runs.copy(),
        "active_tournament": inputs.tournament.copy(),
        "lineage": read_lineage_dict(paths),
        "epoch_id": inputs.epoch_id,
        "epoch": build_epoch_view(paths, inputs.epoch_id)
        if inputs.epoch_id is not None
        else {"epoch_id": None},
        "paused": read_paused(paths),
        "generated_at": _iso(inputs.now),
    }
```
— `src/zicato/query/runtime_view.py`, `build_snapshot`

- **`build_environment(paths, run_log_limit=...)`** — the `/api/environment`
  single coalesced read the client refreshes the whole view from (§9.6). It
  is the consolidation that lets one `state_change` frame trigger ONE fetch
  instead of a wave of per-endpoint polls.

> ⚠️ TRAP — `read_active_runs_view` and `build_snapshot` run in the SSE hot
> path (on every connection). They deliberately do NOT open any run's
> `events.jsonl` (e.g. to read an `adk_session_id`): opening that file
> trips the filesystem watchdog and emits a spurious `run_log` frame BEFORE
> the expected `state_change`, breaking SSE ordering. The session id is read
> off the persisted `loss.json` instead, on a non-hot-path endpoint. If you
> add a hot-path read, do not touch a watched file — the change-signal ordering depends
> on it (`read_active_runs_view`'s docstring is the standing warning).

The per-view readers each own one surface; the ones the rest of this
chapter leans on:

| Reader | Serves | Shape (abbrev.) | Degrade |
|---|---|---|---|
| `build_optimization_trajectory` | `/api/epoch/{id}/trajectory` | `{points, promotion_rate, plateaued, verdict, recent_movement, noise_floor}` | empty shape + `note`; floor still attached (§9.8) |
| `build_tournament_cost` | `/api/epoch/{id}/cost` | `{per_matchup, total_runtime_ms, cost_per_promotion_ms}` | empty shape + `note` |
| `build_live_pipeline` (`query/live_execution_plan.py`) | `/api/live/pipeline` | `{running, stale, liveness, phase, epoch_id, round_index, steps[], epoch_open_step, active_step, decision, in_flight}` — the verdict `build_round_pipeline` decodes, projected out of the SAME read of the running epoch that serves the live execution plan, so the two surfaces cannot report different phases or different counts of the same records | every input degrades independently (§9.11); an unreadable phase serves `{}`, which the stepper reads as no stepper |
| `build_racing_field` | `/api/epoch/{id}/racing-field` | the field-tournament record (`tournament_id`, `state`, `structure`, `structure_params`, `competitors`, `rounds[]`, `standings`, `champion_generation_id`, `promoted_generation_id`, `decision`, …) plus `present`, `source`, `champion_lineage` | `{epoch_id, present: false}`, plus `unreadable` for a malformed record (§9.2.5) |
| `build_round_timeline` | `/api/epoch/{id}/round-timeline` | `{rounds[], waterfall[]}` | empty rounds list |
| `build_execution_plan` | library only | `{board:{digest, entry_count}, stages[]}` — the loop as one tree: baseline + per-round propose/apply/run/gate/decide steps from the round log, work units from the per-unit loss files (never from the log's `unit_completed` aggregate), plus one `measurement_band` step per stage for the reserved ranges that are not a cell's evidence (calibration, the pre-flight's deliberately-degraded probes, the candidate screen, reflection, admission, and anything `unclaimed`), each node stating `status` and `exact`/`partial` provenance | empty stages list + `note` |
| `build_live_execution_plan` | library only | the durable plan for the epoch the heartbeat names, plus `liveness`, an `active` flag on every node, and `overlay: {in_flight, placed, unplaced, other_epoch, active_path, phase, round_index, note}` — the active path is the round plus the step `build_round_pipeline` names (never a second decoding of the phase), and each still-beating `active_runs` record becomes a `running` `board_entry_run` keyed `run:<run_id>` under its candidate's sweep, or under the `run_scope` stage when the plan cannot place it | durable plan + empty overlay when not `live`; empty plan shape + `note` on failure |
| `build_per_entry_for_generation` | `/api/generation/{e}/{g}/per-entry` | `{tournament_id, mean_score, facet_scores, entries[]}`; `facet_scores` is `{facets: {name: {scalar, mean_score, scored_count, entry_count, ran_count}}, overall}` — the candidate re-aggregated per `facet:` board tag at the epoch's frozen weights, so a facet scalar is comparable to the `overall` row | `{facets: {}, overall: null}` (always present) |
| `build_snapshot` | `/api/state`, SSE `snapshot` | see above | each field independently `None` |
| `read_active_runs_view` | `/api/active-runs` | `[{run_id, progress, elapsed_seconds, budget_seconds, last_progress_ts, fresh, …}]`; `fresh` is the server's per-row in-flight verdict — both of `fresh_run_count`'s gates, so the tally is the count of `fresh` rows | `[]` |
| `read_effective_settings` | `/api/config` | `{recorded_at, pid, instance_id, settings}` where `settings` is the OPEN map `{name: {value, source}}` keyed by each knob's dotted configuration name, `source` naming the tier that set it (the dataclass default, the workspace `config.json`, a pinned CLI flag, the host's CPU count; for `tournament.replicates`, the frozen contract, the measured noise floor, or the structure default). Read off the heartbeat record the loop stamped it on, so the served value is the one in force rather than a second reading of the same files | `null` when the workspace holds no run record; a record written before the map existed serves it empty |
| `list_reflections` (`query/reflection_view.py`) | `/api/reflections[?epoch=]` | `{reflections:[{reflection_id, epoch_id, created_at, mode, executed, noise_floor_max_abs_delta, decision_flip_p, n_findings, n_judges}]}` | `{reflections: []}` |
| `build_reflection_summary` | `/api/reflection/{id}/summary` | `{found, pillars:{reliability, discrimination, validity, calibration}, findings[], fidelity_tiers}` | `found: false` same-shape empty |
| `build_judge_scorecards` | `/api/reflection/{id}/scorecards` | `{judges:[{judge_name, tp/fp/fn/tn, ambiguous, precision, recall, f1, disagreement_rate, self_consistency_kappa, exercised, redundant_with}]}` | `{judges: []}` |
| `build_adjudication_xray` | `/api/reflection/{id}/xray/{judge}/{run_ref}` | `{found, transcript:{fidelity, turns[]}, judge_verdict, adjudication}` | `found: false` + `fidelity: unavailable` |

Execution event nodes carry the candidate and comparison coordinates recorded
in the round log. Their identifiers use the record's sequence number, so
interleaving or losing an event cannot assign its neighbor to another candidate.
Proposal summaries group by the recorded generation, and each released holdout
bit keeps its own generation. Missing required scope fields produce `partial`
provenance and a reason. Unknown scope extensions are excluded from the response.
The live reader overlays these facts in memory; it writes no execution records.

### 9.3.7 The shared canonical aggregations — `zicato.workspace.aggregates`

Some quantities are computed from the canonical files by a reader in this
chapter AND by the analysis-report gatherer
(`src/zicato/analyzer/report_data.py`), which walks the same tree to build the
epoch report. Four such quantities are defined once, in
`zicato.workspace.aggregates`, and both sides call that definition. Two of the
four are reached through a pair of functions, one that decodes a record and one
that aggregates the decoded rows, which is why the table has five:

| Function | Quantity | Consumers |
|---|---|---|
| `judge_loss_rows(loss)` | the per-judge attribution rows one run's loss profile records | `build_per_judge_for_entry`, the report's per-judge totals |
| `per_judge_loss_totals(layout, epoch_id, generation_id)` | those rows' weighted loss, summed across a generation's runs | the report's per-judge totals |
| `read_board_entries(layout, epoch_id)` | an epoch's board as validated entries plus its `disable_drift` header | `eval_view._load_board_entries`, the report's board section |
| `cumulative_scalars(steps)` | the cumulative scalar along a lineage, from the per-generation deltas | `epoch/analysis._scalar_trajectory`, the report's trajectory |
| `read_round_log(root, epoch_id, index)` / `read_round_records(layout, epoch_id)` | one round's events and whether the log read cleanly; every settled round folded into a record | `execution_plan._read_round_events`, the report's round records |

Two aggregations of one measurement can disagree. The per-judge pair did: the
reducer keys drift it could not pair with a judge under the empty judge name,
the report totals that bucket under a label of its own, and the per-entry table
drops it because that table names judges. The rows are decoded once now, and
each consumer states in place what it does with the bucket.

> ⛔ NEVER give an index-backed reader a filesystem fallback while putting it
> on one of these. The filesystem is canonical and `index.db` is derived, so
> the aggregations live on the files — but `build_per_judge_for_generation`
> and `build_score_trajectory` answer from the index, and a workspace that was
> never indexed must keep reporting nothing rather than quietly acquiring a
> second source with different numbers. Adding a fallback is a behaviour
> change with its own decision, never a side effect of sharing a reader.

---

## 9.4 The dashboard server — `server.py`

`create_app(workspace_root, static_dir, *, read_only=True, harmonograf_url="")`
builds the Starlette ASGI app. The whole server is a route table over the
`make_endpoints` handler dict plus the SSE stream plus static serving. The
GET routes are always available; the POST control routes answer `403` when
`read_only=True`, the `create_app` default that tests and embedders get.

```python
    paths = _resolve_workspace(workspace_root, harmonograf_url=harmonograf_url)
    static_dir = Path(static_dir)
    started = time.monotonic()
    broker = ChangeBroker(paths)

    handlers = make_endpoints(paths, read_only=read_only, started=started)
```
— `src/zicato/dashboard/server.py`, `create_app`

The `read_only` flag is the single write-gate. `create_app(...,
read_only=True)` returns `403` from every control POST. `run(...)` — the
entry point of both `zicato dashboard` and the `python -m zicato.dashboard`
child a live `zicato evolve` spawns — builds the app with `read_only=False`,
so a launched dashboard serves the controls. A control writes a marker file
that only a running orchestrator consumes, so over a finished workspace a
control has nothing to act on. The GET surface and the SSE stream are
identical either way.

### 9.4.1 Static serving — the stale-asset guard

The bundle is served straight off disk and iterated on live, and the asset
URLs carry no version hash to bust. A plain cache would serve stale CSS/JS
after an edit; a plain no-cache would re-download the whole bundle on every
load. The server threads the needle with `no-cache` + a cheap ETag:

```python
            # The dashboard is served straight off disk and iterated on live, and
            # the asset URLs carry no version/hash to bust — so a plain cache
            # would serve stale CSS/JS. Instead keep `no-cache` (the browser
            # REVALIDATES on every load, so an edit always reaches it) but attach
            # a validator: an ETag/Last-Modified derived from the file's identity
            # (mtime-ns + size, a cheap stat). When the asset is unchanged the
            # revalidation returns a bodyless 304 — no re-download — and the
            # moment a file is edited its ETag changes and the browser gets a
            # fresh 200. Caching efficiency without the stale-asset bug.
            st = candidate.stat()
            etag = f'"{st.st_mtime_ns:x}-{st.st_size:x}"'
```
— `src/zicato/dashboard/server.py`, `_serve_static`

Path traversal is rejected LEXICALLY before the file is read: the
requested name is `os.path.normpath`-ed and refused when it is absolute,
contains a NUL, or climbs out with `..`. The candidate is never resolved
through symlinks, so a bundle staged as symlinks into another tree still
serves. A missing bundle falls back to
a `_PLACEHOLDER_HTML` page that still lists the working JSON endpoints — so
an operator whose wheel shipped without the JS still sees something useful.

### 9.4.2 Port walk, endpoint publication, harmonograf

Dashboard entry points use `dashboard.static_dir` from accepted workspace
configuration. Relative configured paths resolve against the workspace parent;
an empty value selects the bundled assets. The standalone `--static-dir` flag
takes precedence and resolves relative paths against the caller's directory.
Evolve carries its resolved absolute asset path to the dashboard subprocess,
including the bundled default, so startup does not reread the live setting.
The module entry point resolves workspace configuration only when no path is
carried. Missing assets retain the placeholder page and asset-request errors.

`run(workspace_root, host, port, static_dir)` binds the port, walking `+1`
up to ten times if it is taken (`_pick_port` — the probe socket deliberately
does NOT set `SO_REUSEADDR` so a genuinely-bound port reads as occupied),
then records the host/port it actually bound to in `runtime/dashboard.json`
via `_publish_endpoint` — so a parent `zicato evolve` that spawned the
service as a subprocess can read the real URL back rather than assuming the
requested port. It also reuses-or-launches the persistent per-workspace
harmonograf server so a standalone/post-mortem dashboard can deep-link into
persisted sessions (§9.14 has the readback side).

> ⚠️ TRAP — the definitive dashboard URL is printed by `run()` AFTER the port
> walk, because `_pick_port` may have walked off the requested port. The CLI
> command modules deliberately do NOT pre-print the URL. If you add a startup
> banner, print it from `run()` with `bound_port`, never from the command
> with the requested port — an operator who copies the wrong URL lands on a
> different service.

### 9.4.3 The route table

The read routes served by one query-library call are spliced in straight
from the table that declares them, keyed by their own path:

```python
        *[Route(entry.path, handlers[entry.path]) for entry in READ_ENDPOINTS],
```
— `src/zicato/dashboard/server.py`, `create_app`

Every other route wires a named `handlers[...]` entry from `make_endpoints`.
The shape is uniform: coordinate path params (`{epoch_id}`,
`{generation_id}`, `{entry_id}`, `{run_id}`, `{tournament_id}`), the
`/events` SSE stream, the control POSTs (`methods=["POST"]`), the
`/settings/models` route (`settings_api.settings_routes`) spliced in before the
catch-all, and a `serve_fallback` last
so `index.html`'s root-relative references resolve. The catch-all MUST stay
last:

```python
    # Any unmatched GET is treated as a request for a bundled asset so
    # index.html's root-relative references resolve. MUST stay last.
    routes.append(Route("/{path:path}", serve_fallback))
```
— `src/zicato/dashboard/server.py`, `create_app`

> ⛔ NEVER add a route AFTER the `/{path:path}` catch-all. Starlette matches
> in order; anything after the fallback is dead. A new API route goes into
> the `routes` list before the settings routes and fallback.

The **client** hash-route grammar (`router.js` `parseRoute`/`href`, one entry
per `VIEWS` member) mirrors the same coordinate nesting under `#/e/<epochId>/`
(`home` and `epoch` are the bare `#/` and `#/e/<epochId>`):

| Hash route | View | Renders |
|---|---|---|
| `#/e/<id>/gens` · `/gen/<gen>[/<entry>]` · `/gen/<gen>/diff[/<mutId>]` | `gens` / `candidate` / `diff` | generations, the candidate dossier, the patch diff |
| `#/e/<id>/boards` · `/board/<entry>[/<gen>]` | `boards` / `board` | the board trellis / one board + inline transcript |
| `#/e/<id>/mutations[/<mutId>[/<gen>]]` | `mutations` | the mutation surface + side-by-side diff |
| `#/e/<id>/instrument[/<reflectionId>[/<judge>[/<runRef>]]]` | `instrument` | board-reflection: landing → bill of health + judge audit → adjudication x-ray (the `run_ref`'s `:` is `enc()`'d into the last leg) |
| `#/e/<id>/evals` | `evals` | the entries × candidates matrix (the board-as-instrument outcomes lens) |
| `#/e/<id>/traces[/<reflectionId>[/<traceId>]]` | `traces` | a reflection's imported trajectories: landing → trace list → trace detail |
| `#/e/<id>/paper` (also `publication`, `report`) | `publication` | the epoch's published analysis report |
| `#/logs` · `#/settings` | `logs` / `settings` | the workspace-level operator-log pane; the Contract / Models / Appearance settings |

A new view registers in FOUR places (the `instrument` lens is the worked
example): `router.js` (`VIEWS` + `parseRoute`/`href`/`up`/`crumbTrail`),
`shell.js` (`RENDERERS`), a `views/<name>.js` module, and — when it hangs off
the epoch — a `tree.js` leaf gated on a cheap model flag (the Instrument node
shows only when `byEpoch[id].hasReflections`, folded from ONE workspace-wide
`/api/reflections` read in `buildTreeModel`).

---

## 9.5 The read-endpoint table & `_is_safe_id` — `endpoints.py`

A dashboard read route is almost always the same handler: reject an unsafe
coordinate, call one `query` reader, wrap the result in a `JSONResponse`.
Such routes differ only in their path, their coordinates, the reader, and
the canned shape a rejected coordinate gets, so each is one row of one table,
`READ_ENDPOINTS` (56 rows), built by one factory:

```python
    ReadEndpoint(
        path="/api/epoch/{epoch_id}/racing-field",
        reader=query.build_racing_field,
        serves=(
            "The settled racing-field ladder for one epoch, joined server-side "
            "into one rung/gate payload the front-end never reconstructs. "
            "``present: false`` when the epoch has no racing records."
        ),
        params=("epoch_id",),
        degrade=_echo(present=False),
    ),
```
— `src/zicato/dashboard/endpoints.py`, `READ_ENDPOINTS`

A row states the route, the reader behind it (called as
`reader(paths, *coordinates)`, so `params`, and `query` after it, are written
in the reader's argument order), what the route serves, the degrade a rejected coordinate
answers with and at which status, how the optional `?epoch=` scope is
handled. Every synchronous reader runs in the threadpool after coordinate
and scope validation. Handwritten handlers follow the same rule, including
report, transcript and environment reads. Each reader creates, uses and closes
its connections and request inputs inside the worker.

Three of the degrade forms are worth naming. `_echo(...)` repeats the
route's coordinates under their own names — optionally renamed, as the gate
does for `champion_id` → `champion` — and then the fixed fields, in the
order they are written, because key order is part of the shape. `_fixed(...)`
is the degrade that names no coordinate. Six rows take their degrade from
the reader's own empty-shape helper (`eval_view._empty_matrix`,
`candidate_view._empty_dossier` and their siblings), so the route and the
reader cannot drift apart.

`make_endpoints(paths, *, read_only, started)` composes the table with the
five hand-written surfaces — the reads whose query parameters shape the
response, the two epoch documents served as markdown and HTML, the proposal
episode export, the transcripts, and the control POSTs:

```python
    handlers: dict[str, Any] = {}
    handlers.update(_make_read_endpoints(paths))
    handlers.update(_make_state_endpoints(paths, read_only=read_only, started=started))
    handlers.update(_make_epoch_document_endpoints(paths))
    handlers.update(_make_proposal_episode_endpoints(paths))
    handlers.update(_make_conversation_endpoints(paths))
    handlers.update(_make_control_endpoints(paths, read_only=read_only))
    return handlers
```
— `src/zicato/dashboard/endpoints.py`, `make_endpoints`

The table handlers are keyed by ROUTE PATH; the hand-written ones keep their
function names as keys.

> ⛔ Put a new read route that calls one reader in the table rather than in
> a factory. The hand-written factories are for a route whose handler needs
> a second reader, a query parameter that changes the response beyond naming
> a coordinate (a row's `query` field carries one that only names a
> coordinate, as `/api/files/{epoch_id}/{generation_id}/content` takes
> `?path=`), or a media type other than JSON. The route module imports
> nothing of `zicato` beyond `zicato.query`, and
> `tests/test_dashboard_endpoint_table.py` holds every GET route outside a
> declared hand-written set to being a table row, so the dashboard package
> holds no workspace reader.

`tests/test_dashboard_endpoint_table.py` enforces three things about the
table: every row's path has an `ENDPOINT_PAYLOADS` entry and every declared
payload belongs to a bound route (both directions); every row's degrade
satisfies the field types its declared payload contract names; and every row
serves, byte for byte and in key order, what it served before the table
existed — a recorded response per route over the standard fixture workspace,
for a coordinate the fixture holds and for a coordinate the guard rejects.

Payload assembly belongs to the readers, including for the hand-written
routes: matchup conversations (`build_matchup_conversations`), the
conversation and run-transcript reads, and the journal documents live in
`query` readers — `conversations_view.py`, `transcript_view.py`,
`journal_view.py` — each with a best-effort degrade-and-shape test, and all
of them share the ONE `open_index_ro` connection discipline
(`query/_sqlite.py`; §9.3).

A board run's measurements live under one `(epoch, generation, entry)` run
directory, one artifact per measurement, named
`<seed qualifier>/<artifact>.<purpose>.r<draw>.<ext>`
(`core.measurement.measurement_artifact_path`). The transcript reader
(`events_index.resolve_transcript_events`) resolves one of them: a runtime
`run` id selects its exact seed and draw, a `match` id selects the capture
whose paired `loss.json` names that matchup, an unmatched supplied id resolves
nothing, and with no selector the generation's score selects ordinary draw
zero. The transcript routes expose `?run=` and `?match=`. The per-judge route
always reads the entry's ordinary draw zero; `build_per_judge_for_entry`
accepts a `run_id` or `measurement` keyword that no route passes. The
Goldfive `runId` inside an event stream is a separate identity and must not
be fabricated from the runtime run id in fixtures. The workspace-wide
Goldfive-run-id lookup caches each event file by identity, modification time
and size: a pure append reuses an already discovered id without a workspace
scan or stream reopen, while a new, replaced or truncated file, or one
that held no id when last read, is reparsed.

### 9.5.1 `_is_safe_id` — the coordinate guard

Any path param that becomes a workspace coordinate is validated by
`_is_safe_id` BEFORE it touches the filesystem — a conservative allow-list
that rejects traversal, separators, and spaces, and mirrors the Rust
`routes::is_safe_id`:

```python
# Conservative id validator: rejects path-traversal, separators, spaces.
# Mirrors the Rust ``routes::is_safe_id``.
_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,200}$")


def _is_safe_id(value: str) -> bool:
    return bool(value) and value not in (".", "..") and _SAFE_ID.match(value) is not None
```
— `src/zicato/dashboard/endpoints.py`

Two coordinates carry a separator the strict alphabet rejects. A tournament
id carries the ingester's `{epoch}:{parent}->{child}` form, so
`_is_safe_tournament_id` admits `:` and `->`; an adjudication `run_ref`
carries `{candidate}:{entry}:r{n}`, so `_is_safe_run_ref` admits `:`. Both
still block `..` and `/`.

The coordinate's NAME decides which guard it gets, rather than the route
it appears in, because the alphabet an id is admitted by belongs to the kind
of id it is:

```python
COORDINATE_GUARDS: Final[Mapping[str, Callable[[str], bool]]] = {
    "tournament_id": _is_safe_tournament_id,
    "run_ref": _is_safe_run_ref,
}
```
— `src/zicato/dashboard/endpoints.py`

Every name absent from that map takes the strict `_is_safe_id`. Use a wide
validator ONLY for the coordinate it was written for.

### 9.5.2 Degrade-to-200, never 500 or traversal

A malformed coordinate does not 500 and does not raise — it returns the
reader's EMPTY shape at HTTP 200, matching every other coordinate handler
so the client's degrade path is uniform. The shape is the table row's
`degrade`, and the status its `degrade_status`:

```python
    ReadEndpoint(
        path="/api/epoch/{epoch_id}/trajectory",
        reader=query.build_optimization_trajectory,
        serves=(
            "The promoted-lineage trajectory for one epoch, with the promotion "
            "rate and the honest plateau verdict."
        ),
        params=("epoch_id",),
        degrade=_echo(
            points=[],
            promotion_rate=None,
            promoted_count=0,
            challenger_count=0,
            settled_count=0,
            plateaued=False,
            plateau_measurable=False,
            verdict=None,
            recent_movement=None,
            noise_floor=None,
        ),
    ),
```
— `src/zicato/dashboard/endpoints.py`, `READ_ENDPOINTS`

The `?epoch=<id>` scoping param is validated the same way but via
`_epoch_query`, which raises `_BadEpoch` on a path-unsafe value so the
handler can answer `404 {"error": "unknown epoch"}` before touching the
workspace. An id-carrying scoped read returns 404 (a genuinely unknown
epoch); a coordinate PATH param degrades to the empty shape at 200 (a
malformed drill-down should still render an empty panel rather than a
broken page). The three ways a route can treat that param are named on the
row, because they are three different promises to the caller:

| `epoch_scope` | A malformed `?epoch=` | An epoch the workspace does not hold |
|---|---|---|
| `SCOPE_REJECT_UNKNOWN_EPOCH` | the degrade | the degrade (the reader raises; both answer alike) |
| `SCOPE_REJECT_MALFORMED_EPOCH` | the degrade | whatever the reader makes of it |
| `SCOPE_IGNORE_MALFORMED_EPOCH` | read as no scope — the current epoch | whatever the reader makes of it |

> ⛔ NEVER let a coordinate reach a reader unvalidated, and never answer a
> malformed coordinate with a 500. Validate the id before it touches the workspace: `_is_safe_id` first, then the reader
> re-validates against the on-disk set (`_resolve_epoch_id`, §9.3.3). The
> degrade shape MUST match the reader's own empty shape byte-for-byte so the
> client cannot tell a malformed-coordinate empty from a genuinely-empty one
> — both paint the same honest empty panel.

### 9.5.3 The control POSTs — the read-only gate

`_make_control_endpoints` builds the POST surface. Every handler opens with
the read-only guard and, on success, writes a marker file into
`runtime/control/` (the file-based control protocol the orchestrator
consumes — 07-runtime-and-durability.md §7.9):

```python
    def _forbidden_if_read_only() -> JSONResponse | None:
        if read_only:
            return JSONResponse({"error": "dashboard is read-only"}, status_code=403)
        return None
```
— `src/zicato/dashboard/endpoints.py`, `_make_control_endpoints`

The command surface: `pause`/`skip-round` are reason-stamped flag files
(one shared `_flag_control` factory); `resume` is a plain unlink of the
`pause_epoch` flag (idempotent — resuming an unpaused workspace is an
accepted no-op, `removed: false`); `promote/{gen}` and `reject/{gen}`
write one marker per target; `brief` writes the payload
body to `rubric_replacement.txt` (the protocol name is kept even though the
UI label is "brief"). A promote/reject carries the override provenance
(`epoch`/`tournament_id`/`structure`/`reason`) additively so a FIELD
override's readback names which round it targeted; the gauntlet consumer
reads only `reason`.

> ⛔ NEVER make a control endpoint DELETE the source command or signal a
> worker pid directly. The dashboard WRITES a marker; the orchestrator consumes it and archives the audit
> record. `resume` unlinking `pause_epoch` is the ONE legitimate bare unlink,
> because the orchestrator archives the pause episode itself
> (07-runtime-and-durability.md §7.9). If you add a control, write a marker —
> do not reach into the runtime state.

---

## 9.6 The SSE broker — `sse.py`

The SSE stream is the live channel, and it is the single most important
piece of the digest-gated rendering spec (§9.7): it ships **signals**, not
content. The module docstring states the whole contract, including the bug
it closes:

```python
"""Server-sent-events broker for the dashboard service.

``state_change`` notifications are *coalesced*: a burst of file writes
(the orchestrator can touch the runtime tree many times a second) is
debounced into a single ``state_change`` frame carrying the set of
changed ``kind`` regions. The dashboard reacts with ONE coalesced
``/api/environment`` fetch. Without the coalescing, every file write fans
out into a fresh wave of per-endpoint polls, which both floods the server
and makes the view flash.
...
"""
```
— `src/zicato/dashboard/sse.py` (module docstring)

### 9.6.1 Content revision and progress metadata

A `state_change` carries `kind`, `kinds`, `content_revision`, `seq`,
`terminal` and `ts`. The changed records remain behind the GET endpoints.
`content_revision` is a broker-local invalidation counter: observed content
mutations advance it, including epoch and reflection rewrites after a run
stops. It is not a record version or a rendering digest.

The progress sequence `seq` advances on orchestrator transitions. The
`terminal` flag distinguishes a finished loop from a stalled one. Heartbeat
and progress notifications do not advance the content revision; writes to
canonical records do. File-open and file-close notifications and directory
modification noise are ignored, so reading a record cannot trigger a refresh
feedback loop.

The broker reads progress metadata in a worker. A failed progress read
returns `(0, False)`; content invalidation remains independent of that result.

### 9.6.2 Coalescing — the anti-flash debounce

A burst of file writes accumulates changed `kind`s into a pending set and
arms one debounced flush; the flush, `_COALESCE_WINDOW_S = 0.25` later,
emits ONE `state_change` for every kind seen in the window. `_classify`
maps a changed path to a `kind` region (`heartbeat` / `lock` /
`active_tournament` / `progress` / `lineage` / `epoch` / `active_runs` /
`control` / `unknown`) matching the Rust `watcher::ChangeKind`
serialization. A `.tmp` atomic-write intermediate is pure noise and is
dropped before classification.

The opening `snapshot` carries the same metadata beside its initial payload.
The broker captures `content_revision` before constructing the snapshot in a
worker. A mutation during construction therefore has a newer revision and
causes a follow-up refresh. Snapshot construction, progress reads, polling
scans and watcher shutdown run outside the event loop. The polling fallback
compares file identity, nanosecond modification time and size, including
removed files.

The `run_log` frame is the one prompt (non-coalesced) emit — an
`events.jsonl` growth drives the live conversation stream, so it fires
immediately with `{events_path, size}` rather than waiting for the debounce.

### 9.6.3 Fan-out and slow-client safety

`ChangeBroker` fans one watch backend out to many SSE clients; each
subscriber gets its own bounded queue (`maxsize=256`) so a slow client
never blocks the watcher or a sibling — a full queue drops rather than
blocks:

```python
    def _emit(self, payload: dict[str, Any]) -> None:
        """Push one payload to every subscriber, dropping on a full queue."""
        for q in list(self._subscribers):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                # Slow client: drop rather than block the watcher.
                pass
```
— `src/zicato/dashboard/sse.py`, `ChangeBroker._emit`

The watch layer prefers `watchdog` when importable and falls back to a
periodic poll loop otherwise; either way the broker exposes the same async
iterator so the rest of the server is backend-agnostic. A dropped
`state_change` is harmless — the next real transition re-emits, and the
client's coalesced fetch reads the current state regardless of how many
frames it missed.

---

## 9.7 THE DIGEST-GATED RENDERING SPEC

This is the chapter's most-broken discipline and its most important. The
recurring bug it closes — call it **the render-bug class** — is a live
surface that flashes, thrashes, resets scroll, or self-DoSes because a
no-op SSE beat rebuilt DOM that did not change. It has recurred often
enough that the fix is a formal checklist rather than a habit. The spec has five
layers; a live surface must satisfy ALL of them.

### 9.7.1 The bug class, stated

An orchestrator can touch the runtime tree many times a second. Naively,
each touch → an SSE frame → a re-render → a DOM rebuild → lost click
handlers, reset scroll, a visible flash, and, without coalescing, a fan-out
of per-endpoint polls that self-DoSes the server. Every layer below exists to
turn a stream of beats into ZERO DOM writes unless the CONTENT actually
changed. This is the no-op-heartbeat-rebuilds-zero-DOM rule.

> ⚠️ TRAP — this bug is invisible in a screenshot and invisible to a unit
> test that renders once. It only shows on a LIVE surface under a beat
> stream: the panel flickers, the log resets scroll, a hovercard closes
> mid-read. That is why the discipline is enforced by DOM-node-identity
> assertions in the node suite (§9.7.5) rather than by inspection.

### 9.7.2 Layer 1 — the SSE frame ships no content (server)

Covered in §9.6: the `state_change` frame carries changed regions, content
revision and progress metadata. A payload on the frame would defeat every layer below it,
because two content-bearing frames cannot coalesce and a content-bearing
frame forces a render. Layer 1 is the change-signals-carry-no-content rule; it is the server's contribution to
the render discipline.

### 9.7.3 Content invalidation before rendering

The browser requests an environment refresh when content revision changes or
progress advances or restarts. A repeated heartbeat with unchanged metadata
requires no read. Servers without revision metadata use changed regions as
the invalidation signal; frames without a progress cursor still refresh.

The client keeps one environment request in flight and coalesces later
signals into one pending request. It acknowledges the captured revision only
after a successful response is applied. Failures retain the pending request
and retry with exponential delays capped at 30 seconds. A reconnect or a
replacement snapshot prevents an earlier environment response from being
applied to the replacement state.

Content invalidation clears affected resource caches, including individual
reflection summaries. Generation membership need not change. View digests
still decide whether the resulting payload changes the DOM.

### 9.7.4 Layer 3 — views fetch-in-render, fold a content digest, `gatedSwap`

A view is `async render(host, ctx, params)`. It FETCHES its data (via the
null-degrading `data.js` accessors), folds a **content digest** of ONLY the
structural/content fields (changing request timestamps and heartbeat fields
excluded), and calls `gatedSwap(host, digest,
build)`. `gatedSwap` writes DOM only when the digest differs from the one
this host last painted:

```javascript
export function gatedSwap(host, digest, build) {
  if (!host) return false;
  const next = String(digest);
  if (host.getAttribute('data-t-digest') === next && host.firstChild) return false;
  const built = build();
  clearChildren(host);
  const nodes = Array.isArray(built) ? built : [built];
  for (const n of nodes) { if (n) host.appendChild(n); }
  host.setAttribute('data-t-digest', next);
  return true;
}
```
— `src/zicato/dashboard/static/js/ui.js`, `gatedSwap`

The digest fold is the load-bearing craft. `home.js` is the model — every
value is rounded (`.toFixed(3)`), heavy figures delegate to their own
builder digest, and NO timestamp is folded in:

```javascript
  const digest = JSON.stringify({
    live, cur: current,
    rows: rows.map((r, i) => [r.epoch_id, r.generation_count || 0, r.promoted_count || 0,
      svg.isNum(r.best_scalar) ? r.best_scalar.toFixed(3) : null, !!r.closed,
      r.best_generation_id == null ? null : String(r.best_generation_id),
      (trajByEpoch.get(r.epoch_id) || []).map((v) => v.toFixed(3)),
      goalModelDigest(goals[i])]),
    // the loop-communication stats are content-gated on their own rounded fold
    // so a no-op heartbeat (identical rates/verdicts/costs) churns no DOM.
    loop: rows.map((r) => loopStatsDigest(loopByEpoch.get(r.epoch_id), costByEpoch.get(r.epoch_id))),
    ledger: svg.metaLoopLedgerDigest({ epochs: ledger, currentEpochId: current }),
    calib: calib ? svg.calibrationTrendDigest(calib) : null,
    health: health ? {
      epoch: health.epoch_id, healthy: health.healthy, unreadable: health.unreadable,
      findings: (Array.isArray(health.findings) ? health.findings : []).map((f) => [
        f.code, f.severity, f.summary,
      ]),
    } : null,
  });

  gatedSwap(host, digest, () => {
```
— `src/zicato/dashboard/static/js/views/home.js`, `render` (comments trimmed)

The `ui.js` docstring names the exclusion rule outright — the thing a weaker
agent gets wrong is folding a timestamp into the digest, which makes every
beat flip it:

```javascript
// A view computes a stable digest of ONLY its structural/content data
// (timestamps / heartbeat fields EXCLUDED), then calls gatedSwap(host, digest,
// build). If the digest equals the one this host last painted AND the host
// still has children, NOTHING is written — a steady heartbeat re-dispatch is a
// true no-op and the screen cannot flash.
```
— `src/zicato/dashboard/static/js/ui.js` (gatedSwap header)

> ⛔ NEVER fold `last_heartbeat`, `generated_at`, `ts`, an elapsed-seconds, or
> any wall-clock field into a content digest. Those advance on every beat, so
> folding one makes the digest flip on every beat, so `gatedSwap` rebuilds on
> every beat — you have re-created the exact bug the digest exists to prevent.
> A digest folds WHAT is rendered (scalars rounded to display precision,
> ids, tri-state flags, counts), never WHEN.

Publication, patch diff, and mutation views compare their complete persisted
display inputs. Comparing text lengths or counts misses corrections that retain
the same size. Their responses contain saved records and no changing
request clock. Maps are converted to entries so their contents participate in comparison.
The browser tests check both visible corrections and unchanged element identity.

Figures that compare selected numeric values round them to their rendered
precision. For example, `metaLoopLedgerDigest` rounds the displayed floor to
three decimal places. Do not shorten source text or reports to their lengths.

The heavier chrome surfaces (the tree sidebar, the breadcrumb, the
loop-control cluster) apply the same discipline INLINE — each keeps its
own `_last*Digest` and returns without touching DOM when it matches:

```javascript
  const digest = treeDigest(model, route, _toggles, live);
  if (digest === _lastTreeDigest && _treeHost.firstChild) return;
  buildTree(_treeHost, model, route, _toggles, _ctx, (key) => {
```
— `src/zicato/dashboard/static/js/shell.js`, `renderTree`

The upstream chrome guard that keeps a beat from even reaching a rebuild is
`onStateChanged`'s live-data signature includes generation membership,
status and content invalidation. A changed signature clears resource caches;
the tree's rendered content digest still decides whether to rebuild:

```javascript
  const sig = liveDataSignature();
  if (sig !== _lastLiveSig) {
    _lastLiveSig = sig;
    invalidateLive();
  }
```
— `src/zicato/dashboard/static/js/shell.js`, `onStateChanged`

`liveDataSignature` (in `data.js`) is signed off the gen SET (id +
tri-state status + birth-round + epoch), id-sorted so it is order-
independent. It also includes the successfully applied content invalidation
counter, so a canonical rewrite refreshes resources without forcing a repaint.

### 9.7.5 Layer 4 — DOM-node-identity assertions in node tests

The discipline is enforced by tests that assert a re-serve of the SAME
payload keeps the SAME DOM node — `host.firstChild === first`. This is the
only way to prove "zero DOM" mechanically. The pipeline-stepper suite is the
model:

```javascript
  ctl.updatePipeline(pipeFixture());
  assertEqual(allByClass(host, 'dt-pipe-step').length, 4, 'a live projection renders the stepper');
  const first = host.firstChild;

  // a steady heartbeat re-serving the SAME projection must write ZERO DOM.
  ctl.updatePipeline(pipeFixture());
  assert(host.firstChild === first, 'identical re-serve keeps DOM node identity (no rebuild)');

  // an advance repaints (new node, new states).
  const advanced = pipeFixture();
  advanced.steps[2].state = 'done';
  advanced.steps[3].state = 'active';
  ctl.updatePipeline(advanced);
  assert(host.firstChild !== first, 'a genuine advance rebuilds the stepper');
```
— `src/zicato/dashboard/static/test/pipeline_stepper.test.mjs`

The companion assertion is on the DIGEST function itself — an identical
projection folds to a byte-identical digest, an advance flips it:

```javascript
  assertEqual(live.pipelineStepperDigest(pipeFixture()), live.pipelineStepperDigest(pipeFixture()),
    'a re-served identical projection is byte-identical (zero DOM)');
  // ...an advance flips the digest...
  assertEqual(live.pipelineStepperDigest(null), 'none', 'a null read folds to the stable none');
```
— `src/zicato/dashboard/static/test/pipeline_stepper.test.mjs`

`seq_render_gate.test.mjs` is the render-discipline BACKBONE suite — it
pins `state.noteProgress` (advance / repeat-no-op / rollover / absent-seq
degrade), the `core/sse.js` refresh gate (unchanged metadata issues NO
fetch), the four run-states, and the chrome pill's zero-DOM no-op beat.

> ✅ ALWAYS add a "no-op re-serve keeps node identity" assertion when you add
> a live surface. `assert(host.firstChild === first)` after a second
> identical update is the ONE test that catches a stray timestamp in a digest
> or a missing gate. A test that only asserts "it renders the right content"
> passes even when the surface flashes on every beat.

### 9.7.6 The formal checklist

A live surface ships only if it ticks every box:

1. **The server frame is a signal.** The change flows through
   `state_change` (kinds, content revision, seq and terminal); the data is a GET.
2. **Fetch-in-render.** The view fetches its own data in `render()` via a
   null-degrading `data.js` accessor; it does not read a frame payload.
3. **Content digest, timestamps excluded.** The digest folds WHAT is drawn
   (rounded scalars, ids, tri-state flags, counts), never WHEN.
4. **`gatedSwap` (or an inline `_lastDigest` guard).** DOM is written only
   when the digest differs and the host has children.
5. **Heavy figures fold their own builder digest.** Delegate to
   `svg.*Digest` so a no-op beat does not rebuild the heaviest node.
6. **No-op node-identity test.** A node test asserts `host.firstChild ===
   first` across a re-serve of the same payload.
7. **Rollover + absent-seq handled.** A restarted log (backwards seq) forces
   a refresh; a server that stamps no seq degrades to always-refresh.

> ⛔ NEVER `container.innerHTML = ...` on a live surface, and never
> unconditionally `clearChildren` + rebuild in a `render()` that a beat
> re-runs. Both re-create the render-bug class. Route every DOM write through
> `gatedSwap` / `mount` / `reconcileList` so an unchanged node is untouched.
> The activity-log drawer is the one deliberately append-only surface (new
> rows prepended, survivors untouched) — it too never rebuilds.

### 9.7.7 The console-grammar discipline — reuse grammars, don't invent chrome

Render discipline (§9.7) keeps a view from *flashing*; this rule keeps a view
from *drifting off the design language*. A new surface speaks the console's
existing grammars — it does not bolt a fresh component vocabulary on beside
them. Five durable rules, each load-bearing for "one console rather than a fleet of
mini-apps":

- **No pill, tag or badge.** A semantic state — a `verdict`, a `severity`, a
  row's role — is plain text in its tone colour, led by a drawn icon where it
  has one (the `verdictLabel` / `stateLabel` / `flagLabel` family). A metric, a
  count, a relation, a model name is **not** semantic state, so it stays
  uncoloured text.
- **No accent left rail.** A selected item renders its name in the accent
  colour; no container or selection carries a coloured left edge.
- **Sans for chrome, mono for data.** The top bar, the tree, buttons, headings
  and prose resolve to `--v2-sans`; only data, code, ids and key names take
  `--v2-mono`. `test/interface_rules.test.mjs` pins these three rules.
- **Metadata is a caption.** Fidelity tier, adjudicator model, prompt version,
  self-agreement, a verdict tally — all ride ONE `dn-faint` caption line under
  the relevant figure or section, never a per-row tag (which would read as
  semantic state it is not).
- **Navigation lives in the shell** — the hash router's routes and the tree
  sidebar. A view never grows an internal navigation rail of its own; every
  surface is reached the way every other view is reached.

> 🧭 The motivating case is the **Instrument-lens rework** (board reflection).
> The first cut imported the generated-UI idiosyncrasies the operator flagged —
> an internal left rail and overly-extensive per-row tags (a bespoke severity
> chip per finding, redundancy/conflict chip strips, boxed evidence chips, a
> metadata KV strip). The rework deleted all of it: findings and the practice
> review became the loop-health findings panel's quiet verdict-led rows (a tone
> glyph + a headline + a `dn-faint` rationale); the judge scorecards rendered
> rates as the `dn-stat` idiom and the redundancy/conflict relations as one faint
> inline sentence; evidence became inline x-ray links; metadata collapsed to a
> caption; and the ONE coloured state word is the adjudication verdict. Nav rode the
> routes + tree, never a lens-local rail. See
> `docs/design/CONSOLE-DESIGN-LANGUAGE.md` and BOARD-REFLECTION.md §"UI — the Instrument lens".

---

## 9.8 Uncertainty-honest rendering — verdicts relative to the noise floor

A dashboard that says "plateaued" or "improving" when the movement is
smaller than the measurement noise is LYING to the operator. The read model
computes verdicts relative to the epoch's measured A/A noise floor and
reports "no detectable signal" when the movement fits inside it.

`build_optimization_trajectory` is the worked case. It joins the raw
plateau flag with the epoch's measured `noise_floor` and picks the honest
word:

```python
    stuck_no_promotions = traj.settled_count >= 1 and traj.promoted_count == 0
    if stuck_no_promotions:
        verdict = "no_signal" if floor is not None else "stalled"
    elif not traj.plateaued and len(traj.points) < 2:
        verdict = "warming_up"
    elif not traj.plateaued:
        verdict = "improving"
    elif (
        floor is not None
        and recent_movement is not None
        and recent_movement <= float(floor["max_abs_delta"])
    ):
        # The window's whole movement fits inside the measured A/A spread:
        # "plateaued" would overstate the measurement — there is simply no
        # detectable signal above the noise floor.
        verdict = "no_signal"
    else:
        verdict = "plateaued"
```
— `src/zicato/query/loop_view.py`, `build_optimization_trajectory` (comments trimmed)

The five verdict words and their meaning:

| `verdict` | When | What it tells the operator |
|---|---|---|
| `no_signal` | challengers SETTLED and none promoted with a measured floor, OR plateaued with the window's whole movement at/below the measured floor | the data cannot distinguish this from an A/A re-roll of the same generation |
| `stalled` | challengers SETTLED and none promoted, with NO floor measured | the loop is not promoting; how far it is from the noise is unmeasured |
| `warming_up` | nothing settled yet — the promoted spine is the seed alone | too early to judge |
| `improving` | not plateaued, with at least two points on the promoted spine | the loop is making measurable progress |
| `plateaued` | plateaued AND the window's movement is resolvable ABOVE the floor (or no floor was measured) | flat — the loop found a real local plateau |

The stall verdicts count `settled_count` (challengers a tournament has
decided), never `challenger_count`: a challenger still racing has decided
nothing, and reading it as a stall would alarm on a run's first round.

The reader's docstring is the design source, and it is the thing to protect
when you touch this code — "claiming 'plateaued' (or 'improving') would
overstate what was measured":

```python
* :func:`build_optimization_trajectory` — the promoted-lineage scalar
  trajectory + promotion rate + an UNCERTAINTY-HONEST verdict. ... a
  "plateaued" flag whose recent scalar movement sits BELOW the measured
  floor is reported as ``no_signal`` — the loop cannot distinguish that
  movement from a re-roll of the same generation, so claiming "plateaued"
  (or "improving") would overstate what was measured.
```
— `src/zicato/query/loop_view.py` (module docstring)

`_epoch_noise_floor` reads the measured floor straight off
`epochs/<id>/config.json` — INDEPENDENT of the SQLite index — so the floor
is still attached even on a degraded read (a never-built index still shows
the operator the measured noise band).

The visual half of the same doctrine is `svg.js`'s noise band — the
`sparkline` shades the measured A/A band so scalar movement INSIDE it reads
honestly as indistinguishable from a re-roll:

```javascript
  // OPT-IN measured-noise band: `noiseBand: {center, half}` shades the
  // horizontal [center−half, center+half] band (the epoch's measured A/A noise
  // floor around the champion floor) so scalar movement INSIDE the band reads
  // honestly as indistinguishable from a re-roll of the same generation. The
  // y-domain widens to keep the whole band in frame.
```
— `src/zicato/dashboard/static/js/svg.js`, `sparkline`

The band's hovercard says it in operator language: "movement inside this
band is indistinguishable from a re-roll (±<half>)". The y-domain is
widened so the whole band stays framed — the honest rendering is not
allowed to be cropped out of view.

> ⛔ NEVER render a "plateaued" / "converged" / "improving" verdict without
> checking it against the measured noise floor. Verdicts are honest about the noise floor. A movement of
> 0.003 on a floor of 0.66 is not a plateau and not an improvement — it is no
> signal. The proposer's own memory bands round-over-round deltas for exactly
> this reason (05-proposer.md §5.8.6); the dashboard must not un-band them by
> asserting a verdict the measurement cannot support.

> ⚠️ TRAP — "no floor measured yet" is NOT "no signal". When `noise_floor` is
> `None` (an epoch that never ran the A/A calibration), the verdict falls
> back to `stalled`/`plateaued`/`improving` on the raw observation — the
> honest thing to say when you have no floor is the raw observation rather
> than a fabricated "no signal".
> Only a MEASURED floor that the movement fits inside earns `no_signal`.

---

## 9.9 The `svg.js` figure grammar

`svg.js` is a dependency-free SVG data-viz primitive library — one home for
"size text to its box" and the figure builders. Its 51 exports are inline
`export const` / `export function` declarations (no aggregate export block)
across 4,087 lines; the heavy figures (`elimRadial`, `metaLoopLedger`,
`calibrationTrend`, the racing and gauntlet tracks, among nine) have a
`*Digest` companion so the figure participates in the render discipline
(§9.7).

### 9.9.1 The text-fitting primitives — the ONE clip-fix home

The recurring dashboard clip/collision family (a start-anchored label whose
guessed char-cap exceeds its column and gets clipped by
`preserveAspectRatio`) came from every figure re-implementing the same fit
math by hand — one bug, ~30 times. Three primitives centralise it:

- **`fitLabel(s, maxPx, fontPx, opts)`** — truncate to a PIXEL budget (not a
  raw char count), head-truncate by default or middle-truncate
  (`opts.mid`) to keep the discriminating tail. Returns `''` when not even
  one char + ellipsis fits, so a caller drops the label on a too-narrow
  band:

```javascript
export function fitLabel(s, maxPx, fontPx, opts) {
  const str = s == null ? '' : String(s);
  const fpx = isNum(fontPx) ? fontPx : DEFAULT_FONT_PX;
  const per = fpx * CHAR_EM;
  if (!isNum(maxPx) || maxPx <= 0 || per <= 0) return '';
  const budget = Math.floor(maxPx / per);
  if (str.length <= budget) return str;
  if (budget < 1) return '';
  return (opts && opts.mid) ? midLabel(str, budget) : shortLabel(str, budget);
}
```
— `src/zicato/dashboard/static/js/svg.js`, `fitLabel`

- **`edgeText(o)`** — build a `<text>` whose FULL rendered extent stays
  inside `[pad, viewW − pad]` by clamping x AND flipping the anchor inward
  near an edge. It does NOT truncate (call `fitLabel` first).
- **`fitInto(o)`** — `fitLabel` THEN `edgeText`: the common "fit this column
  AND never clip the viewBox" case in one call.

`CHAR_EM ≈ 0.6` is the one mono char-width model every figure MEASURES from
instead of re-guessing a cap. The whole point: a figure cannot re-introduce
the clip because it never guesses a char count — it measures pixels.

> ⛔ NEVER re-implement label truncation inside a figure builder with a
> hardcoded char cap (`s.slice(0, 12)`). That is the exact bug `fitLabel`
> exists to delete, ~30 times over. Measure with `fitLabel`/`fitInto`; a
> too-narrow band drops the label (empty string), it does not clip it.

### 9.9.2 Degenerate cardinality — the single-point guard

There is no single `degenerateAxis` function; the grammar handles the
0-point / 1-point / single-category case INLINE per figure, always the same
way: 0 items → an honest placeholder, 1 item → a centred dot (never a
zero-width axis or a "line to nowhere"). `calibrationTrend` states the
contract explicitly:

```javascript
// DEGRADES: 0 points → an honest placeholder; a single point → a centred dot.
```
— `src/zicato/dashboard/static/js/svg.js`, `calibrationTrend`

The `sparkline` single-point path is the pattern — a lone finite point has
no x-spread, so it renders as a centred dot (a hair larger, so it reads as
an intentional dot) and the path is skipped entirely rather than drawing a
degenerate line. `extent` opens a `±0.5` window when `lo === hi`, and
`scale` guards a zero-width domain with `d1 - d0 || 1` — the numeric
degeneracy is handled at the scale level too.

> ✅ ALWAYS give a new figure builder its 0-point and 1-point degrade
> up front. A dashboard renders a brand-new epoch with one generation and a
> just-started run constantly; a figure that assumes ≥2 points draws a broken
> axis on the exact screen an operator watches a run START on. `swissOverview`
> ("a SINGLE round has no horizontal travel: center the lone column"),
> `roundTimeline`, and `calibrationTrend` are the worked precedents.

### 9.9.3 The `digestOpts` convention — ONE generic figure-opts fold

Every heavy figure participates in the render discipline (§9.7): a view gates
the figure swap on a `*Digest` that folds ONLY what the figure draws, so a
no-op heartbeat diffs a string instead of the SVG. Every figure needs the same
fold — round to rendered precision, drop timestamps, sort keys — so
`digestOpts(opts, omit)` holds it once and generically, rather than each figure
carrying its own copy. The nine `*Digest` exports in `svg.js`
(`trajectoryStripDigest`, `racingScalarTrackDigest`, `gauntletFieldBarsDigest`,
`elimRadialDigest`, `radarSilhouetteDigest`, `proposingDigest`,
`diversityMatrixDigest`, `metaLoopLedgerDigest`, `calibrationTrendDigest`) all
fold through `digestOpts`; each adds only its own normalization (a namespace
prefix, an absent-vs-empty collapse, the served readouts it paints) and an
`omit` list.

```javascript
// ── digestOpts — the single generic figure-opts digest ─────────────────
//   * FUNCTIONS ARE DROPPED — figure opts carry per-render callbacks
//     (onCompetitor / onClick / onRound, a heatmap `value` accessor). A fresh
//     closure every render would flip the digest on every beat; dropping them
//     is the rule that keeps the gate quiet.
//   * KEY-SORTED so object key order never perturbs the string.
//   * a non-integer finite number rounds to 3dp — sub-precision jitter (a
//     re-derived scalar wobbling in the 4th place) must NOT flip the digest.
//   * NaN / undefined → null (a stable, JSON-safe sentinel; ±Infinity too).
//   * `omit` names TOP-LEVEL opts keys to exclude (mode flags / volatile
//     fields a given figure's fold deliberately ignored).
export function digestOpts(opts, omit = []) { ... }
```
— `src/zicato/dashboard/static/js/svg.js`, `digestOpts`

The **drop-functions** rule is the one that is easy to miss and load-bearing:
figure opts carry per-render callbacks and mode accessors; a fresh closure each
render would flip the digest every beat and defeat the gate. Because the fold is
now generic, that rule is written once and every figure inherits it.

The governing property, stated for `metaLoopLedger`: "the figure is a pure
function of the model — the live (in-flight, dashed) and the settled render
are byte-identical for the same row data". Purity is what makes the digest
sound: two byte-identical renders MUST fold to the same digest, so a view gates
on the figure's `*Digest` and a no-op heartbeat churns no DOM.

> ⚠️ TRAP — a figure's fold must include EVERY field the figure draws,
> including positional ones (a tick's index as well as its value). If the
> figure moves a mark when a value's RANK changes but the fold sees only
> the value, a rank change that leaves the value equal will not regate the
> DOM and the figure lies. `metaLoopLedgerDigest` folds `champion_index` (the
> tick position) for exactly this reason — `digestOpts` gives it the fold, the
> wrapper decides WHAT to feed it.

### 9.9.4 The shared view-composition builders — `ui.js`

The figure grammar lives in `svg.js`; the DOM-composition grammar the views
share lives in `ui.js`. These builders each fold a copy-paste class the views
hand-rolls; adopting one is the default, and hand-rolling is the exception a
review should question.

| builder | folds | notes |
|---|---|---|
| `renderView(host, ctx, spec)` | the ~11-view opening: first-paint placeholder (`loading()`), optional `await D.epoch` + no-epoch gate, an optional secondary `guard`, digest fold, `gatedSwap` | a view whose flow genuinely diverges (parallel-fused fetches, multiple hosts, a non-epoch gate, conditional sub-render dispatch) keeps its hand scaffold |
| `dataTable(spec)` | the ~14 hand-rolled `thead`/`tbody` scaffolds | per-cell `{class,text}` / `{el}` / `{title}`; conditional columns + cells via `filter(Boolean)`; row-level `class`/`dataset`/`style`/`onClick`. `deltaCell(v)` is the sign-coloured Δ cell |
| `flagLabel(cls, word)` / `stateLabel(cls, word)` | the inline `dn-flag` / `dn-state` spans | `stateLabel` is the custom-word sibling of `verdictLabel` (which derives its own label and leads with the decision's icon) |
| `hovercardBody(...children)` | the 7 `dn-hc-body` wrappers | accepts a single array too (the `lines`-array sites) |
| `truncate(s, n)` | the four clip/shorten copies (dag / candidate / boardstatus) | the ONE string-truncate; `svg.fmt`/`fmtSigned`/`isNum` stay the numeric home, re-exported from `ui.js` |
| `emptyState(parent, w, h, label)` | the ~13 centred "no data yet" SVG placeholders | `svg.js`-side (a figure primitive) |

> ✅ ALWAYS reach for the shared builder first. The `dn-`/`dt-` class names are
> STABLE (the class-literal test refs route around, they do not churn), so a
> builder that emits the same classes is a drop-in. A genuinely divergent site
> is extracted with an explicit option/parameter, never by papering over the
> difference — and if it still resists a faithful extraction it is LEFT and
> listed, never forced into a subtle render break. The geometry and
> null-semantics code that stays site-local for that reason (`scalarOf` with a
> padded extent, `tournament_model.js`'s `gateState` machine, the champion-id
> tests) is the standing worked example: a champion id is tested as
> `model.championId ? …` at some sites and `r.championId != null ? …` at
> others — a single helper would silently mis-handle a `'0'`/`0` id.

---

## 9.10 `livestatus.js` — the four run-states

`livestatus.js` folds three live read signals (the heartbeat `phase`, the
active-runs array, the active-tournament `phase`) into ONE structure-
agnostic verdict. It is dependency-free and takes raw payload values (never
AppState) so it unit-tests without a DOM. The bug it fixed: the chrome
status pill was gauntlet-shaped — it only lit off `state.activeTournament`
(which only the gauntlet path populates), so a live racing/swiss/elim run
read "nothing running".

The four run-states are a frozen enum, lowercased so the chrome class is
`dt-rs-<state>`:

```javascript
export const RUN_STATE = Object.freeze({
  LIVE: 'live', STALLED: 'stalled', SETTLED: 'settled', DEAD: 'dead',
});
```
— `src/zicato/dashboard/static/js/livestatus.js`

`deriveLiveStatus` computes them off the progress `seq` cursor, NOT the
heartbeat timestamp — because a wedged loop whose beater keeps stamping
`now()` would read alive on a timestamp but has a frozen `seq`:

```javascript
  let runState;
  if (terminal === true) {
    runState = RUN_STATE.SETTLED;
  } else if (!seqKnown) {
    // no seq cursor: derive from the timestamp verdict instead.
    runState = running ? RUN_STATE.LIVE
      : (heartbeatStale ? RUN_STATE.DEAD : RUN_STATE.SETTLED);
  } else if (seqAdvancingFresh) {
    runState = RUN_STATE.LIVE;
  } else if (pulsing) {
    runState = RUN_STATE.STALLED;
  } else {
    runState = RUN_STATE.DEAD;
  }
```
— `src/zicato/dashboard/static/js/livestatus.js`, `deriveLiveStatus`

The state meanings and the two budgets:

| State | When | Chrome |
|---|---|---|
| `SETTLED` | a terminal progress marker (cleanly ended) — authoritative | idle |
| `LIVE` | `seq` advanced within `SEQ_STALL_BUDGET_MS` (90 s) — genuine progress | live pill |
| `STALLED` | no advance within budget, but the heartbeat still pulses (or a run is in flight) | "alive, no progress" |
| `DEAD` | no advance within budget AND no fresh heartbeat | frozen / dead |

Two staleness windows: `STALE_HEARTBEAT_MS = 30_000` (a heartbeat older than
this is not fresh) and `SEQ_STALL_BUDGET_MS = 90_000` (a `seq` unchanged
longer than this reads STALLED). The seq budget is deliberately LONGER than
the heartbeat window — a frozen-`seq` run whose heartbeat still pulses is
STALLED (alive, no progress); only once the heartbeat ALSO freezes is it
DEAD. The server-side liveness derivation mirrors the client:
`runtime_view.STALE_HEARTBEAT_S = 30.0` and `IDLE_PHASE_TOKENS` match
`STALE_HEARTBEAT_MS` and `IDLE_PHASES`, so the two liveness reads agree.

> ⛔ NEVER key liveness on the heartbeat TIMESTAMP alone. The timestamp
> ages on a slow LLM call (false stall) and keeps stamping on a wedged loop
> (false alive) — see 07-runtime-and-durability.md §7.6.1 for the same lesson
> server-side. The `seq` cursor is the true liveness signal; the timestamp is
> the DEAD/STALLED split (whether the process is still pulsing) rather than the LIVE test.

The token→CSS mapping lives in the chrome (`shell.js`), which patches one
`dt-rs-<state>` class per state — `livestatus.js` emits only the lowercase
token, keeping the colour decision in CSS:

```javascript
    patchClass(_runStateEl, 'dt-rs-live', word ? rs === 'live' : false);
    patchClass(_runStateEl, 'dt-rs-stalled', word ? rs === 'stalled' : false);
    patchClass(_runStateEl, 'dt-rs-settled', word ? rs === 'settled' : false);
    patchClass(_runStateEl, 'dt-rs-dead', word ? rs === 'dead' : false);
```
— `src/zicato/dashboard/static/js/shell.js`, `renderStatus`

The same render picks the top bar's one status mark through
`statusMark(transportBroken, runState)`. The mark folds the event-stream
connection and the run verdict into one drawing from `js/icons.js`: a broken
connection returns `offline` (a dashed circle) whatever the last verdict was,
because that verdict stops being current when the stream drops. Otherwise
LIVE and STALLED draw a filled circle, SETTLED an open circle, DEAD and
INTERRUPTED a struck circle, and a workspace with no recorded run a dashed
circle (`idle`). The returned `key` becomes the mark's `data-state`, which
the stylesheet colours; the returned `label` becomes its `aria-label`.

The module also owns `structureStatusLabel` (the ONE structure-aware
standings mapper — elim→"in bracket", swiss→"playing", racing→"racing",
else→"alive" — so a non-racing tournament never borrows racing vocabulary)
and `staleLabel(ageMs)` ("last seen Ns ago").

---

## 9.11 The pipeline stepper — server-projected, rendered verbatim

The propose→apply→run→gate stepper is the cleanest single example of the
server-authority doctrine: the SERVER owns the phase-string inference, the
CLIENT renders the projection verbatim and never re-derives loop position
from a phase token.

The stage vocabulary is SERVER-side, in `loop_view.py`:

```python
#: The four pipeline steps, in loop order.
PIPELINE_STEPS: tuple[tuple[str, str], ...] = (
    ("propose", "propose"),
    ("apply", "apply"),
    ("run", "run"),
    ("gate", "gate"),
)
```
— `src/zicato/query/loop_view.py`, `PIPELINE_STEPS`

`_project_pipeline` is the pure, unit-testable inference that decodes the
phase-string vocabulary (`proposing:… / tournament:… / done:… /
after_round_…`) into `(steps, active_step, decision)` — each step
`{id, label, state, detail}` with `state ∈ pending | active | done`. It is
the single place the phase-string vocabulary is decoded for the pipeline
display, and the JS renders the verdict verbatim. `build_round_pipeline`
projects it from the live tournament fold + heartbeat + active-runs count,
staleness-gated the way the frontend gates.

The pipeline endpoint and the library's live plan share one calculation.
`build_live_surfaces` (`live_execution_plan.py`) reads the workspace once,
calls `build_round_pipeline`, and projects that verdict onto plan nodes.
`/api/live/pipeline` serves the pipeline projection. The execution-plan
readers remain library functions; the dashboard exposes no plan route.
The shared reader runs in the threadpool, leaving the event loop available
to sibling requests while it scans per-unit files.

The JS renderer is a straight transcription — one pip per server step, the
active step's detail beside it, the decision word once the round settles.
It owns NONE of the vocabulary:

```javascript
// ... Its label and detail are server-owned,
// so a further epoch-open step renders here with no change on this side.
// Pure: builds detached DOM.
export function pipelineStepper(pipe) {
  const steps = (pipe && Array.isArray(pipe.steps)) ? pipe.steps : [];
  const open = (pipe && pipe.epoch_open_step) ? pipe.epoch_open_step : null;
  const wrap = el('div', { class: 'dt-pipe', role: 'img', 'aria-label': 'round pipeline' });
  if (open && open.id) {
    ...
```

An epoch-open step (the noise-floor calibration, the contract pre-flight)
arrives as `epoch_open_step` and leads the strip as the active element while
the four round steps sit pending.
— `src/zicato/dashboard/static/js/live.js`, `pipelineStepper`

The node test pins the verbatim rendering and the digest gate — the server
order is rendered verbatim, the active step's detail renders, a done/pending
step's does not, and an identical re-serve keeps DOM node identity (§9.7.5).

> ⛔ NEVER teach the JS stepper a phase token. If a new phase should advance
> the stepper, add it to `_project_pipeline` (server-side) and the JS renders
> the new `state` automatically. The moment the JS parses `phase` to decide
> which pip is active, you have a second inference that breaks server
> authority. `data.js::livePipeline` null-degrades when `/api/live/pipeline`
> fails, so the stepper simply omits — never guesses.

> ⚠️ TRAP — do not conflate the propose→apply→run→gate PIPELINE stepper with
> the RUNG stepper (`live.js::rungStepper`), which shows one pip per
> rung/round of a live tournament — structural progress rather than a stage
> pipeline. They look similar and share the pip idiom; they answer different
> questions.

---

## 9.12 Controls wiring — `postControl`, read-only gating, two-step confirm

The control affordances (pause / resume / skip-round in the chrome;
force-promote / force-reject per challenger in the structure view) all
flow through `postControl` (or `postFieldOverride`) in `core/api.js`, which
POSTs the marker and surfaces a `403` for a read-only workspace to the
caller:

```javascript
// POST a control marker (pause / skip-round / promote / reject /
// brief). Read-only workspaces answer 403 — surfaced to the caller.
export async function postControl(action, body) {
  const res = await fetch('/api/control/' + action, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  });
  let payload = null;
  try { payload = await res.json(); } catch { /* empty body */ }
  return { ok: res.ok, status: res.status, payload };
}
```
— `src/zicato/dashboard/static/js/core/api.js`, `postControl`

### 9.12.1 Read-only gating

The loop-control cluster renders ONLY when the workspace is writable
(`state.health.read_only === false`) and the loop is live or paused — it is
hidden read-only, never a disabled-but-visible control at the loop level. A
paused loop stays reachable because pausing blocks the orchestrator thread,
its heartbeat ages into `interrupted`, and resume must not disappear with it:

```javascript
  const canControl = !!(state.health && state.health.read_only === false);
  const serverPaused = !!(state.heartbeat && state.heartbeat.paused);
  // The optimistic override retires the moment the server agrees with it.
  if (_pausedOverride != null && serverPaused === _pausedOverride) _pausedOverride = null;
  const paused = _pausedOverride != null ? _pausedOverride : serverPaused;
  const show = canControl && (!!(liveness && liveness.live) || paused);
```
— `src/zicato/dashboard/static/js/shell.js`, `renderLoopControls` (comments trimmed)

(The per-challenger override cell in `ui.js::overrideControlCell` renders a
DISABLED, visible control read-only — a field override is a per-row
affordance where a greyed button is clearer than an absent one; the loop
controls are chrome-level and hide.)

### 9.12.2 The two-step confirm

`skip-round` is destructive-ish (it aborts the in-flight round like a
budget cut), so it takes a two-step confirm — first click arms
("confirm skip?"), second click fires, and an armed button auto-disarms
after 4 s:

```javascript
  let armed = false;
  let timer = null;
  const disarm = () => {
    armed = false;
    if (timer != null) { clearTimeout(timer); timer = null; }
    patchIconLabel(skip, 'skip', 'skip round');
    skip.classList.remove('dt-loopctl-armed');
  };
  skip.addEventListener('click', () => {
    if (!armed) {
      armed = true;
      patchIconLabel(skip, null, 'confirm skip?');
      skip.classList.add('dt-loopctl-armed');
      timer = setTimeout(disarm, 4000);
      return;
    }
    disarm();
    if (o.onSkip) o.onSkip();
  });
```
— `src/zicato/dashboard/static/js/shell.js`, `buildLoopControls`

The per-challenger override (`overrideControlCell`) uses the same
arm→confirm idiom but a richer one — arming reveals a reason input plus
direction buttons (promote with the up icon / reject with the fail icon) plus cancel, never a one-click
force-decision. Both surfaces short-circuit to a spent/disabled state when
an override is already recorded or the round has settled.

### 9.12.3 Paused readback — the explicit refresh

A successful control POST requests an immediate environment read so the
button reflects the result promptly. The broker's content revision also
invalidates other connected clients after the control record changes.

```javascript
async function fireLoopControl(action, body, pausedAfter) {
  let res = { ok: false, status: 0 };
  try { res = await postControl(action, body); } catch (err) { res = { ok: false, status: 0 }; }
  if (res.ok && pausedAfter != null) _pausedOverride = pausedAfter;
  // Read back the control result without waiting for the SSE debounce.
  try { await loadEnvironment(); } catch (err) { /* transient — next beat retries */ }
  _lastLoopCtlDigest = null;
  renderStatus();
}
```
— `src/zicato/dashboard/static/js/shell.js`, `fireLoopControl`

The `_pausedOverride` is optimistic and self-retiring: `renderLoopControls`
clears it the moment `serverPaused === _pausedOverride` (the server agreed),
so a raced or stale override can never stick. The paused state itself rides
on the heartbeat payload (`query/runtime_view.py::read_paused` →
`heartbeat.paused`) so every runtime read carries it without a second fetch.


---

## 9.13 The null-degradation duty

Every new GET carries a duty: the client must render an honest empty state
when the endpoint returns `null`/empty or the read fails. A read fails when
the index is absent, when the server errors, and when a browser tab built
against a newer bundle talks to a service that lacks the endpoint. The client
accessors bake this in — a failed read degrades to `null`, and the view omits
the panel:

```javascript
export async function livePipeline() {
  try { return await fetchJson('/api/live/pipeline'); } catch (err) { return null; }
}
```
— `src/zicato/dashboard/static/js/data.js`, `livePipeline`

`home.js` reads the loop-communication endpoints (`trajectory`, `cost`)
this way, so a failed read omits the stats. The pipeline stepper does the
same — `updatePipeline(null)` leaves the host empty (§9.7.5's node test
asserts it).

> ✅ ALWAYS write a new GET's client accessor to null-degrade AND write the
> view to render an honest empty state on `null`. When you add
> `/api/epoch/{id}/newthing`, the accessor returns `null` on a 404 or a
> failed request, and the panel omits or shows "unavailable" — never a
> spinner, never a crash.

> ⚠️ TRAP — the failure mode of skipping the null-degrade is invisible in a
> healthy development session, where every read succeeds. A new panel that
> works in your `zicato dashboard` session throws `Cannot read property 'x'
> of null` the first time its read fails. Test the accessor's `null` path in
> the node suite.

---

## 9.14 The client read layer — `data.js` accessors, caching, transcripts

`data.js` shares one in-flight promise per resource URL. A failed request
resolves to `null` and becomes eligible for retry after one second; concurrent
callers share the same failure during that delay. A successful response that
contains `null` remains cached until invalidation.

Invalidation removes the cache entry immediately. A detached promise can
complete for callers already holding it, but cannot restore or overwrite the
cache. Content changes invalidate both reflection lists and individual
reflection records. `invalidateRunTranscript` limits transcript refreshes to
the affected run; content digests preserve DOM identity when a re-read yields
the same rendered values.

The SSE spine reads through ONE consolidated endpoint (`/api/environment`)
and refreshes on a single coalesced poll — it does not fan out to
per-section endpoints and does not poll on a tight timer:

```javascript
// The dashboard reads the whole environment through ONE consolidated
// endpoint (/api/environment) and refreshes on a single coalesced poll.
// It does NOT fan out to many per-section endpoints and does NOT poll
// on a tight timer. Drill-downs use the lazy per-resource endpoints.
```
— `src/zicato/dashboard/static/js/core/api.js` (module docstring)

### 9.14.1 Transcript reconstruction

`query/transcript_reconstruction.py::reconstruct_transcript` turns one run's
event file into an ordered `Transcript` (turns + margin annotations). It
is pure (the only I/O is the file the caller hands in) and tolerant — a
malformed/truncated line is skipped, a missing file yields an empty
transcript, mirroring the reducer's plain-JSON fallback and the supervisor's
run-log tailer that parse the same growing file. It is library code: the two
query readers that serve the conversation surfaces (`transcript_view`,
`conversations_view`) import it directly, as does the run_id endpoint.

The file's first line selects the reader. A goldfive `events.jsonl` takes the
ADK path, which handles both envelope shapes (camelCase persistence-sink keys
and the reducer's normalized `{kind, payload, ...}`), reusing `to_snake`
(§9.3.4) for key normalization so the transcript speaks the one stable
vocabulary. A file opening with `episode/start` at `seq` 0 is a Foe episode
log and takes the other path, through `query/foe_episode.py`. That reader
rewrites no key — a `data` payload holds tool arguments the model wrote — and
implements the derived-message rule the log format specifies, so an episode
always reconstructs at `fidelity: "exact"`. A proposal transcript is served
from an episode log and from no other source: `resolve_conversation` reads a
generation named without a board entry as a request for its proposal episode,
which `events_index.find_proposal_episode_log` resolves under the epoch's
`episodes/`.

One proposal episode has a second reading, which Foe owns. Foe renders a
finished episode log to one self-contained HTML page — every tool call in both
its rendered and its canonical form, the budget the episode consumed, sandbox
status, and the causality figure — and
`proposer/episode_export.py::write_episode_export` runs the workspace's own
`proposer.binary` as `foe view <the episode's directory>` when the episode
settles, writing the page as `episode.html` beside the log. The render is best
effort and bounded: a binary that cannot be run, a non-zero exit, empty output,
a timeout, or an unwritable directory each leave the log alone and the round
unaffected. Two routes carry it to the dossier. The first,
`/api/generation/{epoch}/{gen}/episode-export`, answers
`transcript_view.build_proposal_episode_export` — whether the candidate has a
page and, when it does not, the log's path and the command that renders one.
The second, `/api/generation/{epoch}/{gen}/episode-export.html`, serves the
page itself, resolved from the same coordinates rather than from any path a
caller supplies. The candidate view's proposal header links the first answer or
captions the second, so an operator reaches Foe's depth from the summary zicato
reads out of the same episode.

The transcript reader also owns the conversation execution outline. Turns carry
`activity_ids`; the top-level `execution` object carries the referenced nodes,
explicit roots, unresolved identifiers, and a fidelity value. Agent edges come
only from `invocation_id` and `parent_invocation_id`; statuses come from the
stated lifecycle events (`agent_invocation_completed`,
`invocation_boundary_exited`, `invocation_cancelled`). A delegation
observation nests under the delegating invocation its event names; without a
resolvable id it stays a turn-scoped tool root with `fidelity: "turn"`.
Missing parents and cycles stay
visible as unresolved records. The query reader also projects the durable
`artifacts.json` inventory as parentless run-scoped nodes. It never assigns a
file to an invocation without a recorded producer identifier.

`static/js/turns.js` follows these identifiers without deriving topology. An
unattached running root renders in a run-level rail until a conversation turn
owns it. Execution state is part of the per-turn digest, so a node status change
patches its owning turn while unrelated DOM nodes remain intact (G10). The full
contract and its current data limits are documented in
`docs/design/CONVERSATION-EXECUTION.md`.

---

## 9.15 Recipe: add a reader + endpoint + panel end-to-end

The flagship change class: surface a new datum on the dashboard. Every step
maps to a doctrine above; the ones agents skip are the ones that flash,
skew the two servers, or 500. Worked scenario: a per-epoch
`GET /api/epoch/{id}/promotion-cadence` returning
`{epoch_id, cadence: [{round_index, rounds_since_last_promote}], note?}`.

**Step 1 — The reader, in the library, with a degrade path.** Add
`build_promotion_cadence(paths, epoch_id)` to a `zicato.query` submodule
(a new `cadence_view.py`, or fold into `loop_view.py`). It is a pure
function returning a `dict`; it degrades to the SAME-shaped empty payload
with a `note`, never raises:

```python
def build_promotion_cadence(paths: WorkspacePaths, epoch_id: str) -> dict[str, Any]:
    try:
        rows = _cadence_rows(paths.index_db, epoch_id)   # or off the lineage
    except IndexUnavailableError:
        return {"epoch_id": epoch_id, "cadence": [], "note": INDEX_NOT_BUILT_NOTE}
    except Exception:  # noqa: BLE001 — best-effort, mirrors sibling readers
        return {"epoch_id": epoch_id, "cadence": [], "note": "index unreadable"}
    return {"epoch_id": epoch_id, "cadence": rows}
```

Coerce every numeric with `coerce_float` and every pass flag with
`_opt_bool`; classify any decision token through `decisions.canonical_decision`
/ `promoted_tristate`. Emit ONE spelling per field. Do NOT re-derive
"the champion" — read `current_champion` if you need it, because execution
records that decision and the query layer serves it.

**Step 2 — Export it from the package face.** Add the name to the import
block AND `__all__` in `src/zicato/query/__init__.py`. This is what makes
`query.build_promotion_cadence` resolve in the endpoint table (§9.1).

**Step 3 — The table row (§9.5).** Add one `ReadEndpoint` to
`READ_ENDPOINTS` in `endpoints.py`. The coordinate is validated before the
reader sees it, and a rejected one degrades to the reader's empty shape at
HTTP 200:

```python
    ReadEndpoint(
        path="/api/epoch/{epoch_id}/promotion-cadence",
        reader=query.build_promotion_cadence,
        serves="How many rounds each promotion took, in round order.",
        params=("epoch_id",),
        degrade=_echo(cadence=[]),
    ),
```

No handler is written and no route is added: `server.py` binds every row of
the table, and `make_endpoints` builds every row's handler. A route that
needs a second reader, a query parameter that changes the response, or a
media type other than JSON is a hand-written factory instead.

**Step 4 — The declared payload contract.** Add the route's path to
`ENDPOINT_PAYLOADS` in `query/contracts.py` under the envelope it serves.
`tests/test_dashboard_endpoint_table.py` fails a table row with no declared
payload, a declared payload with no route, and a degrade whose fields do not
match the types the contract names.

**Step 5 — The client accessor, null-degrading.** Add a thin cached
accessor to `data.js`, and write it to degrade to `null` on a failed read
(§9.13):

```javascript
export async function promotionCadence(epochId) {
  return cachedJson(`/api/epoch/${enc(epochId)}/promotion-cadence`);
}
```

`invalidateLive()` already drops every cached `/api/epoch…` key when live
data changes; a route under a prefix that list does not name needs its
prefix added if it can change while a run is live.

**Step 6 — The view panel with a digest fold (§9.7).** In the owning view's
`async render(host, ctx, params)`, fetch via the accessor, guard the null,
fold a content digest (rounded, timestamp-free), and `gatedSwap`:

```javascript
  const cad = await D.promotionCadence(epochId);
  const rows = (cad && Array.isArray(cad.cadence)) ? cad.cadence : [];
  // ...folded into the view's existing digest object:
  cadence: rows.map((r) => [r.round_index, r.rounds_since_last_promote]),
  // ...inside gatedSwap(host, digest, () => { ... build the panel ... })
```

If the panel is a heavy figure, give it a `*Digest` twin in `svg.js` and
fold THAT (§9.9.3) rather than the raw data.

**Step 7 — The node behaviour test with a no-op assertion.** Add a
`*.test.mjs` that renders the panel, captures `host.firstChild`, re-serves
the SAME payload, and asserts node identity:

```javascript
  view.render(host, ctx, { epochId: 'e0' });
  const first = host.firstChild;
  view.render(host, ctx, { epochId: 'e0' });   // identical re-serve
  assert(host.firstChild === first, 'a no-op re-serve keeps DOM node identity');
```

Also assert the null path renders an honest empty state and, if the
panel derives from a served join, record the join's response for the suite
(§9.16, step 3).

**Step 8 — The Rust degradation check.** Confirm the client renders
correctly when the endpoint 404s. Either the accessor's `null` path (tested
in step 7) covers it, or — if the datum should ALSO surface under the
supervisor — mirror the payload in the Rust route and its `state.rs` serde
(08-supervisor.md §8.12), keeping the field spellings, and
`index_db::EXPECTED_SCHEMA_VERSION` when the index schema changes, in
lock-step.

**Step 9 — The reader unit test.** In `tests/` add a Python test that
builds a fixture workspace, calls `build_promotion_cadence`, and asserts the
shape AND the degrade path (a never-built index ⇒ the empty shape + note; a
malformed epoch ⇒ empty, no raise). This is the best-effort-reader pin.

**Verify**

```bash
uv run pytest tests/test_dashboard_server.py tests/test_dashboard_endpoint_table.py tests/<your reader test>.py -q
make import-lint              # the reader must not import the dashboard
make node-test                   # the no-op / null-degrade node assertions
uv run mypy src/zicato/
```

If you skipped step 1's degrade, a never-built index 500s the endpoint
and raise. If you skipped step 5's null-degrade, the panel throws on the
first failed read. If you skipped step 7's node assertion, the panel flashes
on every beat and nothing in CI notices (§9.7.1).

---

## 9.16 Recipe: change a payload shape (the clean break)

Changing an existing payload's shape is a **clean break** rather than a
back-compat dance — server and client change in the SAME commit, every
client-side coalescer is DELETED, and the pins are updated together — a
payload-shape change is a clean break.
The temptation is to add the new field and leave the old one, then teach the
client to read either. That is the alias growth one-spelling-per-wire-field
forbids.

**Step 1 — Change the reader.** Rename/reshape the field in the `zicato.query`
reader. Emit ONE spelling. If you are replacing `won_by` with a
tri-state `promoted`, remove `won_by` — do not ship both.

**Step 2 — Change the client, delete the coalescer.** Update every view/
accessor that read the old shape to read the new one, and DELETE any
`x.newKey ?? x.oldKey` alias-coalescing you find. A coalescer is the client
compensating for a wobbly wire; the clean break removes the wobble, so the
coalescer must go too. The `livestatus.js::heartbeatTs` comment ("the four
alternate keys are DELETED") is the model — deleting the aliases IS the fix.

**Step 3 — Re-record the responses the node suite serves.** If the payload
is one a browser test renders, re-record it:

```bash
ZICATO_ENDPOINT_SNAPSHOT_UPDATE=1 uv run pytest -q \
    tests/test_dashboard_endpoint_table.py tests/test_tournament_view_elim_states.py
```

rewrites `tests/data/endpoint_route_snapshot.json`,
`tests/data/endpoint_route_probes.json` and
`tests/data/elim_states_served.json`, and `static/test/recorded.mjs` serves
the new bodies to the suite. A shape the recorded workspaces do not cover is
added as a scenario in `tests/_console_scenarios.py` with its probes in
`tests/_endpoint_snapshot_harness.py`, rather than written by hand in the
suite.

**Step 4 — Update the goldens.** If the payload is captured by a parity
golden (the MOCK-GOLDEN gate freezes `gen_score.json` / `experiment.json` /
`lineage.json`; the REINDEX-DUMP gate freezes the index projection), the
shape change legitimately reds those gates. Re-capture with
`ZICATO_PARITY_UPDATE=1` (11-testing.md §"The parity gates") AND state the
behavioural reason in the commit — a golden update is a claim that the new
bytes are correct, never a rubber-stamp.

**Verify**

```bash
uv run pytest tests/ -q -k "dashboard or query or the_changed_payload"
make node-test                                 # the views render the recorded responses
bash tools/parity.sh --only MOCK-GOLDEN --only REINDEX-DUMP   # re-capture if legit
```

> ⛔ NEVER ship a payload change as "add the new field, keep the old one for
> a release". That grows an alias, forces the client to coalesce
> (which then never gets removed), and leaves two fields that can disagree
> about which value is authoritative. The workspace files are canonical and
> rebuildable (07-runtime-and-durability.md §7.1) — there is no wire-format
> back-compat obligation to a client you ship in the same wheel. Break it
> clean, in one commit, pins and all.

> ⚠️ TRAP — a client coalescer (`x.a ?? x.b`) is a SILENT parity hazard: it
> makes the client tolerate a server that emits the wrong spelling, so a Rust
> route that never got the rename keeps "working" against the coalescing
> client while the Python route emits the new shape — and the bug only
> surfaces the day you delete the coalescer. Deleting coalescers in step 2 is
> what turns a latent skew into a loud, same-commit failure.

---

## 9.17 Cross-references

- 07-runtime-and-durability.md §7.1 — files canonical / index derived (why
  every reader degrades on a missing/stale index); §7.6 — the runtime state
  files `build_snapshot` reads; §7.9 — the control protocol the POST
  endpoints write into; §7.10 — the RoundLog fold behind the round timeline.
- 08-supervisor.md §8.9 and §8.15 — the Rust supervisor's read-only index
  discipline and its reader; §8.12 — the reciprocal of the null-degrade and
  clean-break rules. 07-runtime-and-durability.md §7.6.1 — the
  seq-versus-timestamp liveness the four run-states mirror.
- 04-evaluation-statistics.md §4 — where the measured A/A floor §9.8 reads
  comes from. `docs/design/EVAL-VIEW.md` covers the board-status surface
  (`compute_board_split` / `boardStatusDigest`).
- 05-proposer.md §5.7 — the round-log vocabulary the proposing tracker
  renders; §5.8.6 — the banding the dashboard must not un-band.
- 06-tournament-and-selection.md — where gate verdicts, the racing rungs,
  and `deciding_rule` come from before the readers join them.
- 11-testing.md §11.9 — the digest / no-op / DOM-node-identity
  discipline as a test contract; §"The parity gates" — MOCK-GOLDEN /
  REINDEX-DUMP; §"The import contracts" — the query-stays-dashboard-free pin.
- 12-bug-casebook.md case 4 (the client champion scan) — the client champion-scan (first vs
  reigning) behind server authority.

---

## 9.18 Test map for the subsystem

Where to add (and what will catch) a regression, by concern:

| Concern | Tests |
|---|---|
| decision projection: canonical token + tri-state `null` for an undecided candidate | `tests/test_dashboard_decision_surface.py` |
| reader degrade (missing index ⇒ empty + note; malformed epoch ⇒ empty) | `tests/test_dashboard_loop_view.py`, per-reader `tests/test_*_view*.py` |
| coercers: `coerce_float` bool-exclusion, `_opt_bool` | no dedicated suite — exercised only INDIRECTLY, through the reader suites that consume them. A direct unit test for `zicato/query/paths.py` is the standing gap here |
| entry-status four-bucket canon + `status_raw` preservation | `tests/test_dashboard_server.py` |
| `_is_safe_id` / degrade-to-200 / `?epoch=` 404 | `tests/test_dashboard_server.py` (+ `tests/test_issue_250_pins.py` for the `_is_safe_id`/Rust mirror) |
| the served joins (round-timeline / racing-field) reach the node suite as recorded responses | `tests/test_dashboard_endpoint_table.py` + `static/test/recorded.mjs` |
| a field round names the WINNER after a promotion | `tests/test_dashboard_racing_and_rounds.py::test_field_round_names_the_new_champion_after_a_promotion` |
| a field round's champion provenance: current round, and an unknown `eval_mode` before the round commits | `tests/test_dashboard_racing_and_rounds.py` (`…metadata_comes_from_that_round`, `…no_crowning_row_reports_an_unknown_eval_mode`) |
| SSE frame shape (kinds, content revision and progress metadata), coalescing, ordering | `tests/test_dashboard_server.py`, `tests/test_dashboard_refresh.py`, node `live_protocol.test.mjs` |
| the uncertainty-honest verdict (`no_signal` vs `plateaued`) | `tests/test_dashboard_loop_view.py` |
| digest-gated render: no-op DOM identity, seq skip gate, four run-states | node `seq_render_gate.test.mjs`, `pipeline_stepper.test.mjs` |
| the pipeline projection (`_project_pipeline`) | `tests/test_dashboard_loop_view.py` (pure inference) + `pipeline_stepper.test.mjs` |
| controls: read-only 403, two-step confirm, paused readback | node `loop_controls.test.mjs`, `override_taxonomy.test.mjs`, `tests/test_dashboard_gate_endpoint.py` |
| `current_champion` is the last committed round's champion (the client champion-scan regression, two-promotion lineage) | `tests/test_dashboard_decision_surface.py::test_current_champion_is_the_spine_end` (+ the seed fallback beside it) |
| which member of a promoted SET is the head — `gate.gen`, the round-timeline spine, and `current_champion` on a BRANCHING lineage | `tests/test_dashboard_promoted_head.py` |
| the tree crown per epoch (the same client champion-scan defect across epochs, multi-epoch fixture) | node `epoch_scoping.test.mjs` |
| the whole Node behaviour suite (digest / no-op / mock parity) | `src/zicato/dashboard/static/test/run-all.mjs` via `make node-test` |
| the query layer stays dashboard-free | `make import-lint` (the import-linter contract) |
