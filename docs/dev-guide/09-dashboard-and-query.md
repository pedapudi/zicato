# 09 — The Dashboard & the Query Layer

> **Covers.** The whole read/serve surface: the `zicato.query` read-model
> library (every reader module — what it builds, its payload shape, its
> degrade behaviour), the standalone Starlette dashboard service
> (`server.py` / `endpoints.py` / `sse.py` / `transcript.py` /
> `static_assets.py`), and the browser bundle under
> `dashboard/static/js/` (the SSE spine, the router/shell, the views, the
> `svg.js` figure grammar, `livestatus.js`, the pipeline stepper, controls).
> Two doctrines run through all of it: **server authority** (the client
> renders, it never computes) and **digest-gated rendering** (a no-op
> heartbeat rebuilds zero DOM).
>
> **Prerequisites.** 02-architecture.md (orchestrator vs dashboard as
> separate OS processes), 07-runtime-and-durability.md §7.1 (files
> canonical / index derived), §7.6 (the runtime state files this layer
> reads), §7.10 (the RoundLog whose fold the round timeline renders),
> 08-supervisor.md §"The read-only SQLite discipline" (the Rust twin of
> every reader here). 04-evaluation-statistics.md §"Train/holdout split"
> and §"The noise floor" ground the uncertainty-honest verdicts of §9.8.
>
> **Invariants introduced in this chapter.** Each is load-bearing; a
> violation is a correctness or a data-integrity bug, not a style nit.
>
> | ID | Invariant |
> |----|-----------|
> | DQ1 | **The server computes; the client renders.** No join, classification, or decision the server owns is re-derived on the client. Bug #4 (the client champion-scan) is the canonical breach. |
> | DQ2 | **One spelling per field on the wire.** `entry_id`, `generation_id`, `ts` (int ms epoch), `pass_fail` (`true`/`false`/`null`), `promoted` (tri-state `true`/`false`/`null`). No aliases, no bare ints the client re-interprets, no default-`false` for an undecided promotion. |
> | DQ3 | **Every reader is best-effort.** A missing / never-built / transiently-torn input degrades to an empty-or-`None` shape (often with a `note`), never raises. No endpoint built on `zicato.query` returns a 500. |
> | DQ4 | **The query layer is library code and never imports the dashboard.** The import-linter contract "the query layer stays dashboard-free" pins it; the dashboard is a driver on top. |
> | DQ5 | **SSE frames carry change-kinds + `seq` + `terminal` ONLY — never content.** A `state_change` is a signal to fetch, not a payload. |
> | DQ6 | **A no-op heartbeat rebuilds ZERO DOM.** The client drops a repeat-`seq` frame with no fetch; a view folds a content digest (timestamps excluded) and swaps only on a real change. Node tests assert DOM-node identity across a re-serve. |
> | DQ7 | **Verdicts are honest about the noise floor.** Movement inside the measured A/A floor reads `no_signal` ("no detectable signal"), never "plateaued" or "improving". |
> | DQ8 | **Every new GET null-degrades on the Rust supervisor.** A payload the Rust reader does not serve returns `null`/empty; the client paints the honest empty state, never a spinner or a crash. |
> | DQ9 | **Controls gate on `read_only:false`; a destructive control takes a two-step confirm.** A control write forces an explicit refresh — it does not advance the progress `seq`, so the no-op-skip gate would otherwise stall the readback. |
> | DQ10 | **`current_champion` is the reigning spine end** — the LAST promoted generation — never the first-scored or the highest-scored; a decision surface names its `deciding_rule`. |
> | DQ11 | **A payload-shape change is a clean break.** Server and client change in the same commit, client-side coalescers are deleted, the node `mock_server` parity pin and the goldens are updated together. |
> | DQ12 | **An id path param is validated by `_is_safe_id` before it touches the workspace.** A malformed coordinate degrades to the empty shape at HTTP 200 — never a 500, never a traversal. |
> | DQ13 | **Every JSON GET has a declared query contract.** `query.contracts.ENDPOINT_PAYLOADS` is the exhaustive inventory. |
> | DQ14 | **Lineage owns topology.** `lineage.json` alone supplies parent and tri-state promotion; experiment outcomes are journal detail. |
> | DQ15 | **Composite readers share walks.** `build_environment` performs one lineage walk and hands its scoped feed to the epoch and trajectory builders. |

---

## 9.0 Map of the subsystem

Two packages plus a browser bundle. The **library** is `zicato.query`
(pure read-model assembly); the **driver** is `zicato.dashboard` (the HTTP
server + the JS). Nothing in the library knows the driver exists.

| File | What lives there | Approx. size |
|---|---|---|
| `src/zicato/query/__init__.py` | the package face — re-exports every reader; the "library, not driver" module docstring | 341 lines |
| `src/zicato/query/paths.py` | `WorkspacePaths` (the `.zicato/` layout), the coercers `coerce_float` / `to_snake` / `_opt_bool`, `_resolve_epoch_id` (the traversal guard), epoch enumeration re-exports | 274 lines |
| `src/zicato/query/decisions.py` | THE one decision classifier: `canonical_decision`, `promoted_tristate`, `stamp_experiment_decision`, `PROMOTED_DECISIONS` | 94 lines |
| `src/zicato/query/contracts.py` | typed envelopes and the exhaustive JSON endpoint registry | — |
| `src/zicato/query/_sqlite.py` | `_open_index` (read-only `mode=ro`), `_query` (swallow-to-`[]`), `_opt_json`, `_IndexAbsent` | 41 lines |
| `src/zicato/query/runtime_view.py` | `build_snapshot`, `read_heartbeat_dict` (the `ts` int-ms stamp), `normalize_entry_status` (the four-bucket canon), `read_active_runs_view`, `read_paused` | 438 lines |
| `src/zicato/query/loop_view.py` | `build_optimization_trajectory` (the uncertainty-honest verdict), `build_tournament_cost`, `build_round_pipeline` + `PIPELINE_STEPS` (the server-owned stepper projection) | 435 lines |
| `src/zicato/query/racing_view.py` | `build_racing_field` — the racing ladder JOINED server-side (the ex-`reconstructRacing` hoist) | 301 lines |
| `src/zicato/query/rounds_view.py` | `build_round_timeline` — the round spine + loss-floor waterfall JOINED server-side (the ex-`rounds.js` four-endpoint join) | 329 lines |
| `src/zicato/query/reflection_view.py` | `list_reflections`, `build_reflection_summary` (four-pillar bill of health), `build_judge_scorecards`, `build_adjudication_xray` (transcript + judge verdict + meta-judge record), `entry_candidate_matrix` (reflection-independent, off the index loss tables) — the Instrument-lens feed (BOARD-REFLECTION.md R4). Index-first, file-fallback; stays dashboard-free by reading `result.json` / `judge_io` for the x-ray rather than the events-preview reconstructor | ~430 lines |
| `src/zicato/query/epoch_view.py` | `build_epoch_view`, `build_environment`'s epoch slice, `_current_champion` (reigning spine end), `build_workspace_view`, `compute_board_split` | 35 KB |
| `src/zicato/query/gate_view.py` | `build_gate_breakdown` (+ `deciding_rule`), `build_score_trajectory`, `build_health_report`, `build_rating_view`, `build_drift_movements` | 56 KB |
| `src/zicato/query/tournament_view.py` | `build_bracket`, `build_tournament_structure`, `build_matchup_detail`, `build_matchup_grid` | 51 KB |
| `src/zicato/query/{judge,hypothesis,lineage,events_index,run_log}_view.py` | per-judge matrices, hypothesis/calibration accuracy, lineage feed, `/api/environment` coalescer + meta-loop ledger, the run-log tail. `judge_view.build_per_entry_for_generation` serves the dossier; its `facet_scores` block comes from `eval_view.facet_scores_for_generation` | — |
| `src/zicato/query/board_scan.py` | `iter_board_rows` + the `board_entry_id` / `board_entry_tags` guards — the tolerant raw `board.jsonl` walk shared by the judge-name union and the facet-tag read. Per-ROW degrade: `load_board` VALIDATES, so one stale entry would blank a whole read model | ~75 lines |
| `src/zicato/dashboard/server.py` | `create_app` (routes + `read_only`), `run` (port walk + harmonograf), static serving with ETag revalidation | 575 lines |
| `src/zicato/dashboard/endpoints.py` | `make_endpoints` (the per-surface factories), `_is_safe_id` / `_is_safe_tournament_id`, the control POST handlers | 62 KB |
| `src/zicato/dashboard/sse.py` | `ChangeBroker` (coalescing file watcher), `sse_event_stream`, `_classify`, `_progress_signal` | 398 lines |
| `src/zicato/dashboard/transcript.py` | `reconstruct_transcript` — one goldfive `events.jsonl` → an ordered `Transcript` | 31 KB |
| `src/zicato/dashboard/static_assets.py` | `resolve_static_dir` — the bundle-resolution seam | 50 lines |
| `src/zicato/dashboard/static/js/core/` | `sse.js` (the seq gate), `api.js` (`postControl`), `state.js` (`noteProgress`, `AppState`), `dom.js`, `bus.js` | — |
| `src/zicato/dashboard/static/js/` | `router.js`, `shell.js` (dispatch + chrome + loop controls), `live.js` (the live engine + `pipelineStepper`), `livestatus.js` (the four run-states), `data.js` (null-degrading accessors), `svg.js` (the figure grammar), `ui.js` (`gatedSwap`) | — |
| `src/zicato/dashboard/static/js/views/` | one module per page: `home.js`, `epoch.js`, `gens.js`, `candidate.js`, `board(s).js`, `mutations.js`, `instrument.js` (the board-reflection lens — landing / bill-of-health / judge-audit / x-ray), `diff.js`, … each an `async render(host, ctx, params)` | — |

Two orientation facts before anything else:

- **The dashboard is a separate OS process.** `zicato evolve` spawns it
  for the lifetime of a loop; `zicato dashboard` runs it standalone over a
  finished workspace (a post-mortem). It reads the same `.zicato/` files
  the orchestrator and the Rust supervisor read — it is one of three
  independent readers of the runtime state (07-runtime-and-durability.md
  §7.6).
- **There are TWO servers that speak the same wire.** This Python service
  and the Rust supervisor (08-supervisor.md) both serve the dashboard
  bundle and both answer the read APIs. The JS cannot tell which one it is
  talking to, which is the whole reason for DQ8 (every new GET must
  null-degrade the way the Rust side will serve it).

---

## 9.1 The library / driver split — `query` is a library, `dashboard` is a driver

The single most load-bearing structural fact about this subsystem: the
readers are **library code** with no dashboard dependency. The package
docstring states it and the import-linter enforces it.

```python
"""The workspace query layer: read-only ``.zicato/`` state assembly.

Library code, not driver code: these readers turn the on-disk workspace
(runtime state files, the SQLite analytical index, epoch records) into
the JSON view shapes any consumer can render. The dashboard server is
the primary consumer today, but the layer has no dashboard dependency —
:mod:`zicato.query` must never import :mod:`zicato.dashboard` (enforced
by the import-linter contracts).
"""
```
— `src/zicato/query/__init__.py` (module docstring)

The enforcement is a forbidden-import contract (see 11-testing.md §"The
import contracts"):

```
[[tool.importlinter.contracts]]
name = "the query layer stays dashboard-free"
type = "forbidden"
source_modules = ["zicato.query"]
forbidden_modules = ["zicato.dashboard"]
```
— `pyproject.toml`

**Why this matters (the query-layer hoist).** `zicato.query` was once a single
monolithic `dashboard/state_reader.py` module — a driver-internal helper.
It was hoisted OUT of the driver and split into per-view submodules so
that (1) the Rust supervisor's read layer has a Python peer to keep parity
with, (2) tests can exercise the read model without booting a server, and
(3) the readers can never accrete an HTTP concern by accident. The
`__init__.py` re-exports every name the split produced so a consumer still
writes `from zicato.query import build_epoch_view` — the split is
invisible at the call site, the boundary is not.

> ⛔ NEVER import `starlette`, `Request`, `JSONResponse`, or anything under
> `zicato.dashboard` from a `zicato.query` module. A reader returns plain
> Python (`dict` / `list` / scalars); the endpoint wraps it in a
> `JSONResponse`. The moment a reader knows about HTTP, DQ4 is broken and
> `lint-imports` reds — and the Rust supervisor loses the Python peer it
> keeps parity with.

> ✅ ALWAYS add a new reader to `zicato.query` (a per-view submodule) and
> re-export it from `__init__.py`'s import block AND `__all__`. The endpoint
> in `zicato.dashboard.endpoints` is a one-line wrapper over it. If you find
> yourself writing workspace-reading logic inside `endpoints.py`, you have
> put library code in the driver — move it down.

The dashboard driver itself has exactly two declared driver→driver edges
(`cli → dashboard` for launch/static-resolution, `dashboard → builder` for
the mounted builder routes); every other cross-driver import is forbidden
(§9.4, 11-testing.md §"The import contracts"). The endpoints module's own
docstring restates the split from the top:

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

## 9.2 The server-authority doctrine

The client is a **renderer**. Every classification, every join across
records, every "which one is the champion" decision is computed on the
server, serialized once, and rendered verbatim. This is not an
aesthetic preference — it is the fix for a whole bug class (bug #4) and
the reason the two servers (Python + Rust) can agree.

### 9.2.1 Bug #4 — the client champion-scan (first vs reigning)

The canonical breach: a JS view walked the generations list itself and
picked "the champion" as the FIRST generation it found with a promotion,
rather than the LAST — the *reigning* champion is the END of the promoted
spine, not its root. The two disagree the moment an epoch promotes more
than once. The fix moved the walk server-side, into one function, and the
client reads its answer:

```python
def _current_champion(experiments: list[dict[str, Any]]) -> str | None:
    """The REIGNING champion generation id, or ``None``.

    Walks the promoted champion spine (the same chain
    ``_champion_lineage`` builds) and returns its LAST id — the reigning
    champion, never the first promotion.
    """
```
— `src/zicato/query/epoch_view.py`, `_current_champion` (docstring)

> ⛔ NEVER re-derive "the champion", "the winner", "the latest generation",
> or a decision on the client from a list the server already ordered. This
> is DQ1 and it is bug #4. If the client needs to know the reigning champion,
> the server ships `current_champion`; if it needs the deciding rule, the
> server ships `deciding_rule`. The client's job is to draw the answer, not
> compute it.

> ⚠️ TRAP — "first with a promotion" and "reigning champion" read
> identically on any epoch that has promoted exactly once, so a client-side
> scan LOOKS correct in every single-promotion fixture. Bug #4 only surfaces
> on a multi-promotion epoch — which is exactly the epoch an operator cares
> about. A regression test for a champion-selection change MUST use a
> two-promotion lineage (see 11-testing.md §"Write a regression test").

### 9.2.2 The one decision classifier — `decisions.py`

Every payload that names a tournament decision funnels through ONE module
so the wire vocabulary is single-valued and the client never re-classifies.

```python
"""decisions — THE one experiment-decision classifier the dashboard serves.

Every payload that names a tournament decision funnels through here so the
server ships ONE canonical vocabulary and the frontend never re-classifies:
"""
```
— `src/zicato/query/decisions.py` (module docstring)

The classifier maps every legacy spelling onto the canonical wire token and
refuses to guess an unknown one:

```python
def canonical_decision(raw: str | None) -> str | None:
    """Map a recorded decision token onto the canonical wire vocabulary.

    ``promoted`` / ``rejected`` / ``deferred`` for every known spelling;
    an unknown token passes through lowercased (never guessed into a
    verdict); ``None`` / empty stays ``None`` (no decision recorded).
    """
    if raw is None:
        return None
    tok = raw.strip().lower()
    if not tok:
        return None
    if tok in PROMOTED_DECISIONS:
        return "promoted"
    if tok in REJECTED_DECISIONS:
        return "rejected"
    if tok in DEFERRED_DECISIONS:
        return "deferred"
    return tok
```
— `src/zicato/query/decisions.py`, `canonical_decision`

`PROMOTED_DECISIONS = frozenset({"promoted", "promote", "accepted",
"accept", "win", "won"})` — the legacy spellings older workspaces recorded,
all collapsed to the one canonical token `"promoted"`. An unknown token is
lowercased and passed through, never coerced into a verdict — an
un-recognized string is a data question, not a decision the classifier is
allowed to invent.

### 9.2.3 The tri-state `promoted` stamp — and the Class-B bug

`promoted` on the wire is a **tri-state**: `true`, `false`, or `null`. The
`null` case is load-bearing and its own bug class:

```python
def promoted_tristate(raw: str | None) -> bool | None:
    """The tri-state ``promoted`` stamp for a recorded decision token.

    ``None`` when no decision is recorded (in-flight / never raced) —
    NEVER a default ``False`` (the Class-B bug); else exactly the boolean
    the lineage view derives (``token in PROMOTED_DECISIONS``).
    """
    if raw is None:
        return None
    tok = raw.strip().lower()
    if not tok:
        return None
    return tok in PROMOTED_DECISIONS
```
— `src/zicato/query/decisions.py`, `promoted_tristate`

> ⛔ NEVER default `promoted` to `False` for a generation with no recorded
> decision. An in-flight or never-raced challenger is `promoted: null`, not
> `promoted: false` — the "Class-B bug" is exactly the collapse of "not yet
> decided" into "rejected". A `false` says the gate ran and said no; a `null`
> says the gate has not run. The dashboard colours those differently (a
> pending accent vs a rejection tone), and a placebo/soft-reject audit reads
> them differently too.

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

`experiment_decision` is the one reader of the raw shape (a bare-string
`outcome` IS the decision; a dict `outcome` carries it under `decision` /
`tournament_decision` / `verdict`) so the "where is the decision written"
knowledge lives in one place too.

### 9.2.4 The schema canon — one spelling per field (DQ2)

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
> and a coalescer the client has to grow — DQ2 exists to keep both at zero.

### 9.2.5 Served joins — the ex-client reconstructions

Three payloads used to be assembled by the client walking several
endpoints and stitching id strings. All three are now JOINED on the server,
served as ONE payload, and the client reads them whole. Each move deleted a
class of client/server drift.

**Racing field (ex-`reconstructRacing`).** The frontend used to parse
`{epoch}:{champ}->{chall}` id strings client-side to rebuild the rung
ladder. That join is now `build_racing_field`:

```python
"""racing_view — the settled racing-field payload, served (not fabricated).

A racing tournament is persisted as ONE record PER CHALLENGER (the durable
RoundRecord lands in a later phase); the rung/gate ladder the dashboard
renders therefore has to be JOINED out of those per-challenger records. The
frontend used to do that join itself (``reconstructRacing`` parsed
``{epoch}:{champ}->{chall}`` id strings client-side); this reader is that
join moved server-side, so the client reads ONE settled racing-field payload
and never re-derives rungs / survivors / the crowned winner.
"""
```
— `src/zicato/query/racing_view.py` (module docstring)

**Round timeline (ex-`rounds.js` four-endpoint join).** The client used to
JOIN `/api/epoch` + `/api/lineage` + `/api/score-trajectory` +
`/api/tournaments` to reconstruct the round model. Now the server does it
and the client overlays only the LIVE in-flight round from its SSE state:

```python
"""rounds_view — the epoch ROUND TIMELINE, served (not joined client-side).

The frontend used to derive this model by JOINING four endpoints
(``/api/epoch`` + ``/api/lineage`` + ``/api/score-trajectory`` +
``/api/tournaments``) in ``rounds.js``. This reader is that join moved
server-side: ``GET /api/epoch/{id}/round-timeline`` serves the SETTLED
rounds + the loss-floor waterfall; the client only overlays the LIVE
in-flight round from its SSE state (a live overlay, not a re-derivation).
"""
```
— `src/zicato/query/rounds_view.py` (module docstring)

**Pipeline projection (ex-phase-string parsing).** The client used to parse
the heartbeat `phase` string to decide propose→apply→run→gate position.
That inference is now server-side (§9.11); the JS renders the verdict
verbatim.

**Elim gen-states (ex-`elimFlow` per-render derivation).** The bracket
figures — `svg.js` `elimFlow` (the lane-flow read) and its `elimRadial`
twin (the concentric-ring read; single_elim's primary + double_elim's
optional toggle) — each used to
derive the whole elimination model *per render*: re-sorting the mis-ordered
`winners.concat(losers)` columns their caller handed them, de-duplicating
backend-duplicated matches, classifying every loss as an *elimination* vs a
winners→losers *drop* (the second life), and carrying five defensive guards
against phantom eliminations in an under-specified payload. That was a DQ1
breach — the client computing a domain conclusion. `derive_elim_states(rounds)`
is that fold moved server-side, served as a top-level `gen_states` join:

```python
def derive_elim_states(rounds: Any) -> dict[str, Any]:
    """The SERVER-SIDE elim fold — the model the bracket figures render.

    ... this fold is it, moved server-side, so every consumer (Python
    service, Rust supervisor, the node mock) serves ONE identical model.
    Ported line-for-line into ``crates/supervisor/src/elim_states.rs`` — the
    shared fixture ``tests/data/elim_states_fixture.json`` pins the two folds
    together.

    Output ``{"rounds": [...], "gen_states": [...]}``:
    * ``rounds`` — PRE-SORTED by round index; every round gains
      ``bracket_side`` (WB/LB) and its matches are DEDUPED + gain ``loser``.
    * ``gen_states`` — one record per competitor: ``{generation_id,
      played_rounds, advanced_rounds, lost_rounds, eliminated_at_round,
      side_by_round, lb_entry_round, projected}``. The elimination-vs-drop
      rule is the client's, verbatim.
    """
```
— `src/zicato/query/tournament_view.py`

This one is the model example of the doctrine's hardest form: the fold is
served by BOTH servers, so it ships **twice** — the Python `derive_elim_states`
and the Rust `elim_states.rs` port — pinned isomorphic by the shared
`tests/data/elim_states_fixture.json` (a Python↔Rust parity fixture, not a
client golden). A client-side re-derivation fallback is *forbidden*: BOTH
bracket renderers — `elimFlow` (lane-flow) and `elimRadial` (concentric
rings) — now render `gen_states` verbatim, having dropped the ~100 derivation
lines + the caller re-sort + the dedupe + the elimination-vs-drop pass + the
phantom-✕ guards. Every one of those guards existed only because the payload
was under-specified; serving the model retired the whole family at once
(12-bug-casebook.md). (`elimRadial` was briefly cut as a redundant figure and
then restored by operator veto — it kept its served-model reads across the
round-trip, so the "server computes, client renders" contract holds for both.)

The tell that a join moved server-side but the CLIENT still cross-checks it
is the node `mock_server.mjs`, which re-derives the two served joins from
fixture maps "exactly as the Python readers do" — and pins that any
divergence is a bug in the mock, never grounds to re-derive in prod
(§9.16, 11-testing.md §"Node conventions"):

```javascript
// The prod frontend no longer joins rounds / racing ladders client-side: the
// server serves them (`build_round_timeline` / `build_racing_field`, pinned by
// tests/test_dashboard_racing_and_rounds.py). ...
// It is TEST-ONLY scaffolding — nothing
// under js/ imports it — and any behavioural divergence from the Python
// readers is a bug in THIS file, never grounds to re-derive in prod code.
```
— `src/zicato/dashboard/static/js/test/mock_server.mjs` (header)

> ✅ ALWAYS move a multi-record join to the server the moment the client
> starts stitching ids or walking more than one endpoint's payload to build a
> view. The three joins above each deleted a client/server drift class. The
> server's answer is the one both the Python and the Rust servers can produce
> identically; a client-side join is a fourth implementation nobody keeps in
> sync.

### 9.2.6 `deciding_rule` — a decision surface names its rule

When the server serves a gate verdict, it also serves the RULE that fired,
so the client renders "why" without re-running the gate logic. The gate
breakdown carries `deciding_rule` — `None` until a rule fires, then the
name of the rule that decided:

```python
    base["deciding_rule"] = fired_rule
```
— `src/zicato/query/gate_view.py`, `build_gate_breakdown` (tail)

The empty/degraded gate shape carries `"deciding_rule": None` so the field
is always present with one spelling (DQ2), and the client shows "no rule
fired yet" from the `null` rather than inferring it.

---

## 9.3 The reader library API

Every reader is a pure function `build_*(paths, ...) -> dict|list`.
`paths` is a `WorkspacePaths` (the `.zicato/` layout object). They share
one contract — best-effort degradation (DQ3) — and one small set of
primitives.

### 9.3.1 The best-effort degrade contract (DQ3)

The package docstring states it once for everyone:

> Every function here is best-effort: a missing or transiently-truncated
> file degrades to an empty / `None` value rather than raising, so no
> endpoint built on top of this ever returns a 500.

Every reader realises it the same way: catch the failure, return the
same-shaped empty payload, and (where useful) attach a `note` naming the
reason so the UI can say *why* it is empty rather than spin. The
loop-view is the model — note the two distinct degrade notes:

```python
    try:
        traj = optimization_trajectory(paths.index_db, epoch_id)
    except IndexUnavailableError:
        return _empty_trajectory(paths, epoch_id, "index not built; run zicato repair index")
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
> DQ3 contract) but it is the one place ruff's `BLE001` fires; every reader
> carries the `# noqa: BLE001 — best-effort` marker so the blanket-except is
> a documented decision, not an accident. Do not "tighten" it to a specific
> exception type — a never-built index, a torn read, and a schema-newer
> database must ALL degrade, and you cannot enumerate every failure a
> future SQLite/file layout can throw.

### 9.3.2 `WorkspacePaths` and the traversal guard

`WorkspacePaths(root)` (`src/zicato/query/paths.py`) is the typed
`.zicato/` layout — `root` is the `.zicato` directory itself, with
properties for every file the readers touch (`heartbeat`, `lock`,
`active_runs_dir`, `active_tournament_log`, `progress_log`, `control_dir`,
`lineage`, `index_db`, `epochs`). It carries the resolved persistent
`harmonograf_url` the dashboard process injected at startup (§9.4).

The one security-relevant helper is `_resolve_epoch_id`, which validates a
`?epoch=<id>` against the on-disk epoch set and rejects a path-unsafe value
so a `?epoch=../foo` cannot escape the workspace:

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
    if epoch_id not in list_epoch_ids(paths):
        raise ValueError(f"unknown epoch id: {epoch_id!r}")
    return epoch_id
```
— `src/zicato/query/paths.py`, `_resolve_epoch_id`

This is the SECOND line of defence behind `_is_safe_id` in the endpoint
(§9.5); the endpoint rejects a malformed coordinate before it reaches the
reader, and the reader re-validates against the actual epoch set.

### 9.3.3 The coercers — `coerce_float`, `to_snake`

`coerce_float` is THE numeric payload coercer — it replaced dozens of
inline `float(x) if isinstance(x, int|float) else None` copies, and it
excludes bools on purpose (a stray `True` is not a scalar):

```python
def coerce_float(value: Any) -> float | None:
    """``float(value)`` for a real number, else ``None``.

    THE one numeric payload coercer (bools excluded — a stray ``True`` is
    not a scalar). Replaces the dozens of inline
    ``float(x) if isinstance(x, int | float) else None`` copies.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)
```
— `src/zicato/query/paths.py`, `coerce_float`

`to_snake` normalizes a `camelCase`/`PascalCase` key to `snake_case`, and
it is one half of a two-language contract — it **mirrors the Rust
`run_log::to_snake`** so event kinds key on ONE stable vocabulary across
both servers:

```python
def to_snake(name: str) -> str:
    """Convert a ``camelCase`` / ``PascalCase`` identifier to ``snake_case``.

    Idempotent on input already in snake_case. Mirrors the Rust
    ``run_log::to_snake`` so event kinds key on one stable vocabulary
    (the zicato#1 normalization).
    """
```
— `src/zicato/query/paths.py`, `to_snake` (docstring)

> ⚠️ TRAP — `to_snake` has a Rust twin. If you change how a goldfive event
> key is normalized on the Python side, the Rust supervisor's run-log tailer
> keys on a different vocabulary and the two dashboards show different event
> kinds for the same file. Change both, or neither. The transcript
> reconstructor (`dashboard/transcript.py`) reuses this exact helper for the
> same reason — one normalization, three consumers.

### 9.3.4 The read-only index open

Every SQLite read opens the index **read-only** and swallows a query error
to an empty result, so a mid-rebuild or newer-schema database can never
raise into a reader:

```python
def _open_index(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise _IndexAbsent
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _query(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> list[sqlite3.Row]:
    try:
        return list(conn.execute(sql, params))
    except sqlite3.Error:
        return []
```
— `src/zicato/query/_sqlite.py`

`_IndexAbsent` is raised on a missing file so a reader can distinguish
"never built" (attach the `run zicato repair index` note) from "unreadable"
(attach the generic note) — the two degrade notes in §9.3.1.

### 9.3.5 The composite reads

Two readers coalesce the whole environment so the client fetches once, not
six times:

- **`build_snapshot(paths)`** — the `/api/state` snapshot AND the opening
  SSE `snapshot` frame. It composes heartbeat + lock + active runs + active
  tournament + lineage + epoch view + `paused`, each independently
  best-effort:

```python
    return {
        "heartbeat": read_heartbeat_dict(paths),
        "lock": read_lock_dict(paths),
        "active_runs": read_active_runs_view(paths),
        "active_tournament": read_active_tournament_dict(paths),
        "lineage": _read_json_value(paths.lineage),
        "epoch_id": read_current_epoch(paths),
        "epoch": build_epoch_view(paths),
        "paused": read_paused(paths),
        "generated_at": _iso(_utc_now()),
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
> add a hot-path read, do not touch a watched file — DQ5's ordering depends
> on it (`read_active_runs_view`'s docstring is the standing warning).

The per-view readers each own one surface; the ones the rest of this
chapter leans on:

| Reader | Serves | Shape (abbrev.) | Degrade |
|---|---|---|---|
| `build_optimization_trajectory` | `/api/epoch/{id}/trajectory` | `{points, promotion_rate, plateaued, verdict, recent_movement, noise_floor}` | empty shape + `note`; floor still attached (§9.8) |
| `build_tournament_cost` | `/api/epoch/{id}/cost` | `{per_matchup, total_runtime_ms, cost_per_promotion_ms}` | empty shape + `note` |
| `build_round_pipeline` | `/api/live/pipeline` | `{running, stale, phase, steps[], active_step, decision, in_flight}` | every input degrades independently (§9.11) |
| `build_racing_field` | `/api/epoch/{id}/racing-field` | `{present, structure, rounds[], standings, champion_lineage}` | `{present: false}` (§9.2.5) |
| `build_round_timeline` | `/api/epoch/{id}/round-timeline` | `{rounds[], waterfall[]}` | empty rounds list |
| `build_per_entry_for_generation` | `/api/generation/{e}/{g}/per-entry` | `{tournament_id, mean_score, facet_scores, entries[]}`; `facet_scores` is `{facets: {name: {scalar, mean_score, scored_count, entry_count, ran_count}}, overall}` — the candidate re-aggregated per `facet:` board tag at the epoch's frozen weights, so a facet scalar is comparable to the `overall` row | `{facets: {}, overall: null}` (always present) |
| `build_snapshot` | `/api/state`, SSE `snapshot` | see above | each field independently `None` |
| `read_active_runs_view` | `/api/active-runs` | `[{run_id, progress, elapsed_seconds, budget_seconds, …}]` | `[]` |
| `list_reflections` (`query/reflection_view.py`) | `/api/reflections[?epoch=]` | `{reflections:[{reflection_id, epoch_id, created_at, mode, executed, noise_floor_max_abs_delta, decision_flip_p, n_findings, n_judges}]}` | `{reflections: []}` |
| `build_reflection_summary` | `/api/reflection/{id}/summary` | `{found, pillars:{reliability, discrimination, validity, calibration}, findings[], fidelity_tiers}` | `found: false` same-shape empty |
| `build_judge_scorecards` | `/api/reflection/{id}/scorecards` | `{judges:[{judge_name, tp/fp/fn/tn, ambiguous, precision, recall, f1, disagreement_rate, self_consistency_kappa, exercised, redundant_with}]}` | `{judges: []}` |
| `build_adjudication_xray` | `/api/reflection/{id}/xray/{judge}/{run_ref}` | `{found, transcript:{fidelity, turns[]}, judge_verdict, adjudication}` | `found: false` + `fidelity: unavailable` |

---

## 9.4 The dashboard server — `server.py`

`create_app(workspace_root, static_dir, *, read_only=True, harmonograf_url="")`
builds the Starlette ASGI app. The whole server is a route table over the
`make_endpoints` handler dict plus the SSE stream plus static serving. The
GET routes are always available; the POST control routes answer `403` when
`read_only=True` (the standalone default).

```python
    paths = _resolve_workspace(workspace_root, harmonograf_url=harmonograf_url)
    static_dir = Path(static_dir)
    started = time.monotonic()
    broker = ChangeBroker(paths)

    handlers = make_endpoints(paths, read_only=read_only, started=started)
```
— `src/zicato/dashboard/server.py`, `create_app`

The `read_only` flag is the single write-gate seam. `create_app(...,
read_only=True)` (the default, and what `zicato dashboard` uses standalone)
returns `403` from every control POST; `run(..., read_only=False)` (what a
live `zicato evolve` spawns) enables them. The GET surface and the SSE
stream are identical either way — a post-mortem dashboard reads everything,
it just cannot drive the loop.

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

Path traversal is rejected before the file is read (`candidate.resolve()`
must stay under `static_dir.resolve()`), and a missing bundle falls back to
a `_PLACEHOLDER_HTML` page that still lists the working JSON endpoints — so
an operator whose wheel shipped without the JS still sees something useful.

### 9.4.2 Port walk, endpoint publication, harmonograf

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

Every route wires a `handlers[...]` entry from `make_endpoints`. The shape
is uniform: coordinate path params (`{epoch_id}`, `{generation_id}`,
`{entry_id}`, `{run_id}`, `{tournament_id}`), the `/events` SSE stream, the
control POSTs (`methods=["POST"]`), the builder + settings routes spliced
in before the catch-all, and a `serve_fallback` last so `index.html`'s
root-relative references resolve. The catch-all MUST stay last:

```python
    # Any unmatched GET is treated as a request for a bundled asset so
    # index.html's root-relative references resolve. MUST stay last.
    routes.append(Route("/{path:path}", serve_fallback))
```
— `src/zicato/dashboard/server.py`, `create_app`

> ⛔ NEVER add a route AFTER the `/{path:path}` catch-all. Starlette matches
> in order; anything after the fallback is dead. A new API route goes into
> the `routes` list above the builder/settings splice; the fallback is the
> terminal.

The **client** hash-route grammar (`router.js` `parseRoute`/`href`, one entry
per `VIEWS` member) mirrors the same coordinate nesting under `#/e/<epochId>/`:

| Hash route | View | Renders |
|---|---|---|
| `#/e/<id>/gens` · `/gen/<gen>[/<entry>]` · `/gen/<gen>/diff[/<mutId>]` | `gens` / `candidate` / `diff` | generations, the candidate dossier, the patch diff |
| `#/e/<id>/boards` · `/board/<entry>[/<gen>]` | `boards` / `board` | the board trellis / one board + inline transcript |
| `#/e/<id>/mutations[/<mutId>[/<gen>]]` | `mutations` | the mutation surface + side-by-side diff |
| `#/e/<id>/instrument[/<reflectionId>[/<judge>[/<runRef>]]]` | `instrument` | board-reflection: landing → bill of health + judge audit → adjudication x-ray (the `run_ref`'s `:` is `enc()`'d into the last leg) |
| `#/e/<id>/paper` | `publication` | the ACM publication |

A new view registers in FOUR places (the `instrument` lens is the worked
example): `router.js` (`VIEWS` + `parseRoute`/`href`/`up`/`crumbTrail`),
`shell.js` (`RENDERERS`), a `views/<name>.js` module, and — when it hangs off
the epoch — a `tree.js` leaf gated on a cheap model flag (the Instrument node
shows only when `byEpoch[id].hasReflections`, folded from ONE workspace-wide
`/api/reflections` read in `buildTreeModel`).

---

## 9.5 The endpoint factories & `_is_safe_id` — `endpoints.py`

`make_endpoints(paths, *, read_only, started)` composes eight per-surface
factories into one `name -> handler` dict:

```python
    handlers: dict[str, Any] = {}
    handlers.update(_make_state_endpoints(paths, read_only=read_only, started=started))
    handlers.update(_make_epoch_endpoints(paths))
    handlers.update(_make_judge_run_endpoints(paths))
    handlers.update(_make_live_endpoints(paths))
    handlers.update(_make_tournament_endpoints(paths))
    handlers.update(_make_files_endpoints(paths))
    handlers.update(_make_conversation_endpoints(paths))
    handlers.update(_make_control_endpoints(paths, read_only=read_only))
    return handlers
```
— `src/zicato/dashboard/endpoints.py`, `make_endpoints`

Each factory closes over `paths` and returns a small dict of async
handlers. A handler is thin by construction: validate the coordinate, then
wrap the reader in a `JSONResponse`. This is now the WHOLE surface: the seven
endpoint blobs that used to assemble a payload inline in `endpoints.py`
(`_build_matchup_conversations`, the `api_conversation` resolver chain,
`api_run_transcript`, the journal file-reads, …) were hoisted into `query`
readers — `conversations_view.py`, `transcript_view.py`, `journal_view.py` (+
their homes) — each with a DQ3 degrade+shape test, leaving `endpoints.py` a
sheet of validate-then-delegate one-liners (~1451→~1000 lines) and the readers
sharing the ONE `open_index_ro` connection discipline (`query/_sqlite.py`; §9.3).
`_make_live_endpoints` is the model — `api_live_pipeline` is one line over
`build_round_pipeline`:

```python
    async def api_live_pipeline(_request: Request) -> JSONResponse:
        """The authoritative propose→apply→run→gate pipeline projection.

        ``GET /api/live/pipeline``. The server owns the phase-string
        inference the stepper renders — see ``build_round_pipeline``.
        """
        return JSONResponse(query.build_round_pipeline(paths))
```
— `src/zicato/dashboard/endpoints.py`, `_make_live_endpoints`

### 9.5.1 `_is_safe_id` — the coordinate guard (DQ12)

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

A tournament id carries the ingester's `{epoch}:{parent}->{child}` form, so
`:` and `->` are legal there — `_is_safe_tournament_id` widens the alphabet
to admit those two separators while still blocking `..` and `/`. Use the
tournament validator ONLY for a tournament id; every other coordinate uses
the strict `_is_safe_id`.

### 9.5.2 Degrade-to-200, never 500 or traversal

A malformed coordinate does not 500 and does not raise — it returns the
reader's EMPTY shape at HTTP 200, matching every other coordinate handler
so the client's degrade path is uniform:

```python
    async def api_epoch_trajectory(request: Request) -> JSONResponse:
        """Promoted-lineage trajectory + promotion rate + honest verdict.

        ``GET /api/epoch/{epoch_id}/trajectory``. A malformed id degrades
        to the empty trajectory shape (HTTP 200), matching every other
        coordinate handler.
        """
        epoch_id = request.path_params["epoch_id"]
        if not _is_safe_id(epoch_id):
            return JSONResponse(
                {
                    "epoch_id": epoch_id,
                    "points": [],
                    "promotion_rate": None,
                    "promoted_count": 0,
                    "challenger_count": 0,
                    "plateaued": False,
                    "verdict": None,
                    "recent_movement": None,
                    "noise_floor": None,
                },
                status_code=200,
            )
        return JSONResponse(query.build_optimization_trajectory(paths, epoch_id))
```
— `src/zicato/dashboard/endpoints.py`, `_make_epoch_endpoints`

The `?epoch=<id>` scoping param is validated the same way but via
`_epoch_query`, which raises `_BadEpoch` on a path-unsafe value so the
handler can answer `404 {"error": "unknown epoch"}` before touching the
workspace. An id-carrying scoped read returns 404 (a genuinely unknown
epoch); a coordinate PATH param degrades to the empty shape at 200 (a
malformed drill-down should still render an empty panel, not a broken page).

> ⛔ NEVER let a coordinate reach a reader unvalidated, and never answer a
> malformed coordinate with a 500. DQ12: `_is_safe_id` first, then the reader
> re-validates against the on-disk set (`_resolve_epoch_id`, §9.3.2). The
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
accepted no-op, `removed: false`); `kill/{run_id}`, `promote/{gen}`,
`reject/{gen}` write one marker per target; `brief` writes the payload
body to `rubric_replacement.txt` (the protocol name is kept even though the
UI label is "brief"). A promote/reject carries the override provenance
(`epoch`/`tournament_id`/`structure`/`reason`) additively so a FIELD
override's readback names which round it targeted; the gauntlet consumer
reads only `reason`.

> ⛔ NEVER make a control endpoint DELETE the source command or signal a
> worker pid directly. The dashboard WRITES a marker; the orchestrator (or
> the Rust supervisor for `kill_runs/`) consumes it and archives the audit
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
``/api/environment`` fetch — this is what stops the old flashing /
self-DoS where every file write fanned out into a fresh wave of
per-endpoint polls.
"""
```
— `src/zicato/dashboard/sse.py` (module docstring)

### 9.6.1 The wire vocabulary — change-kinds + seq + terminal ONLY (DQ5)

The `state_change` frame carries the coalesced set of changed `kind`
regions plus the orchestrator's true-liveness `seq` and `terminal` marker
— and NOTHING ELSE. It never carries the changed data:

```python
        seq, terminal = _progress_signal(self.paths)
        self._emit(
            {
                "event": "state_change",
                "data": {
                    "type": "state_change",
                    "kind": kinds[0] if len(kinds) == 1 else "multiple",
                    "kinds": kinds,
                    "seq": seq,
                    "terminal": terminal,
                    "ts": _now_iso(),
                },
            }
        )
```
— `src/zicato/dashboard/sse.py`, `_flush_state_change`

`_progress_signal` reads the true liveness cursor off the orchestrator
progress event log — `seq` advances only on a genuine transition (never on
the heartbeat timer), and `terminal` distinguishes a cleanly-ended loop
from a stalled one. It is best-effort: a never-run workspace or a torn read
degrades to `(0, False)` and never raises into the hot path.

> ⛔ NEVER put a payload on a `state_change` frame. The frame's job is to say
> "something in region X changed; fetch if you care" — the client does ONE
> `/api/environment` read in response. If you ship the changed data on the
> frame, you have (a) reintroduced the fan-out, (b) coupled the SSE writer to
> every payload shape, and (c) made the frame unable to coalesce (two
> different payloads cannot merge). DQ5: the frame is a signal; the data is a
> GET.

### 9.6.2 Coalescing — the anti-flash debounce

A burst of file writes accumulates changed `kind`s into a pending set and
arms one debounced flush; the flush, `_COALESCE_WINDOW_S = 0.25` later,
emits ONE `state_change` for every kind seen in the window. `_classify`
maps a changed path to a `kind` region (`heartbeat` / `lock` /
`active_tournament` / `progress` / `lineage` / `epoch` / `active_runs` /
`control` / `unknown`) matching the Rust `watcher::ChangeKind`
serialization. A `.tmp` atomic-write intermediate is pure noise and is
dropped before classification.

The opening `snapshot` frame carries the same `seq`/`terminal` pair so a
freshly-connected client has the liveness cursor before any `state_change`
arrives:

```python
        seq, terminal = _progress_signal(paths)
        yield _format_sse(
            "snapshot",
            {"type": "snapshot", "data": snapshot, "seq": seq, "terminal": terminal},
        )
```
— `src/zicato/dashboard/sse.py`, `sse_event_stream`

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
enough that the fix is a formal checklist, not a habit. The spec has five
layers; a live surface must satisfy ALL of them.

### 9.7.1 The bug class, stated

An orchestrator can touch the runtime tree many times a second. Naively,
each touch → an SSE frame → a re-render → a DOM rebuild → lost click
handlers, reset scroll, a visible flash, and (historically) a fan-out of
per-endpoint polls that self-DoSed the server. Every layer below exists to
turn a stream of beats into ZERO DOM writes unless the CONTENT actually
changed. This is DQ6.

> ⚠️ TRAP — this bug is invisible in a screenshot and invisible to a unit
> test that renders once. It only shows on a LIVE surface under a beat
> stream: the panel flickers, the log resets scroll, a hovercard closes
> mid-read. That is why the discipline is enforced by DOM-node-identity
> assertions in the node suite (§9.7.5), not by "it looks fine".

### 9.7.2 Layer 1 — the SSE frame ships no content (server)

Covered in §9.6: the `state_change` frame carries change-kinds + `seq` +
`terminal` only. A payload on the frame would defeat every layer below it,
because two content-bearing frames cannot coalesce and a content-bearing
frame forces a render. Layer 1 is DQ5; it is the server's contribution to
the render discipline.

### 9.7.3 Layer 2 — the client seq no-op-skip gate

The client drops a repeat-`seq` frame with NO fetch and NO state touched. A
coalesced beat that re-emits the same `seq` is a true no-op; only a genuine
`seq` advance (or a rollover = restarted log) refreshes:

```javascript
  _sse.addEventListener('state_change', (ev) => {
    // THE SEQ NO-OP-SKIP GATE. Refresh ONLY on a genuine seq advance (or a
    // rollover = restarted log); a repeat seq (a coalesced no-op beat)
    // writes ZERO DOM — no fetch, no state touched. A frame with no seq
    // (pre-RUNTIME-V2) degrades to the legacy always-refresh path.
    let frame = null;
    try { frame = ev && ev.data != null ? JSON.parse(ev.data) : null; }
    catch { frame = null; }
    if (frame && typeof frame === 'object' && 'seq' in frame) {
      const verdict = state.noteProgress(frame.seq, frame.terminal);
      if (verdict.advanced || verdict.rollover) {
        state._changed();
        refreshAfterEvent();
      }
      return;
    }
    refreshAfterEvent();
  });
```
— `src/zicato/dashboard/static/js/core/sse.js`

`state.noteProgress(seq, terminal)` is the pure cursor: the first `seq` ever
is adopted and counts as an advance (a fresh load must paint); a strictly
greater `seq` advances; a smaller `seq` is a rollover (the log was cleared
on a fresh boot); a repeat `seq` is a no-op that moves nothing:

```javascript
  noteProgress(seq, terminal, now = Date.now()) {
    if (typeof seq !== 'number' || !isFinite(seq)) {
      return { advanced: false, rollover: false, present: false };
    }
    const prev = this.lastSeq;
    let advanced = false;
    let rollover = false;
    if (prev < 0) {
      advanced = true;              // first seq ever — a fresh load must paint.
    } else if (seq > prev) {
      advanced = true;
    } else if (seq < prev) {
      rollover = true;              // log cleared + restarted (seq begins at 1).
    }
    if (advanced || rollover) {
      this.lastSeq = seq;
      this.lastSeqAdvanceAt = now;
    }
    if (typeof terminal === 'boolean') this.terminal = terminal;
    return { advanced, rollover, present: true };
  }
```
— `src/zicato/dashboard/static/js/core/state.js`, `noteProgress`

A frame with NO `seq` (a pre-RUNTIME-V2 / legacy server) degrades to the
always-refresh path — the gate is additive, an old server just refreshes
every beat as before.

### 9.7.4 Layer 3 — views fetch-in-render, fold a content digest, `gatedSwap`

A view is `async render(host, ctx, params)`. It FETCHES its data (via the
null-degrading `data.js` accessors), folds a **content digest** of ONLY the
structural/content fields (timestamps and heartbeat fields EXCLUDED, floats
rounded to their rendered precision), and calls `gatedSwap(host, digest,
build)`. `gatedSwap` writes DOM only when the digest differs from the one
this host last painted:

```javascript
export function gatedSwap(host, digest, build) {
  if (!host) return false;
  const next = String(digest);
  if (host.getAttribute('data-t-digest') === next && host.firstChild) return false;
  clearChildren(host);
  const built = build();
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
    rows: rows.map((r) => [r.epoch_id, r.generation_count || 0, r.promoted_count || 0,
      svg.isNum(r.best_scalar) ? r.best_scalar.toFixed(3) : null, !!r.closed,
      (trajByEpoch.get(r.epoch_id) || []).map((v) => v.toFixed(3))]),
    // the loop-communication stats are content-gated on their own rounded fold
    // so a no-op heartbeat (identical rates/verdicts/costs) churns no DOM.
    loop: rows.map((r) => loopStatsDigest(loopByEpoch.get(r.epoch_id), costByEpoch.get(r.epoch_id))),
    ledger: svg.metaLoopLedgerDigest({ epochs: ledger, currentEpochId: current }),
    calib: calib ? svg.calibrationTrendDigest(calib) : null,
    health: health ? (Array.isArray(health.findings) ? health.findings.length : 0) : -1,
  });

  gatedSwap(host, digest, () => {
```
— `src/zicato/dashboard/static/js/views/home.js`, `render`

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

> ✅ ALWAYS round a folded float to its rendered precision (`.toFixed(3)` for
> a scalar shown to 3 dp). A raw float flips the digest on a change below the
> visible precision — a repaint the operator cannot even see. The `svg.js`
> figure digests do the same (`metaLoopLedgerDigest` quantizes the floor to
> 3 dp — "a fraction moving past 2dp flips it"). Round WHAT the figure draws.

The heavier chrome surfaces (the tree sidebar, the breadcrumb, the
loop-control cluster) apply the same discipline INLINE — each keeps its
own `_last*Digest` and returns without touching DOM when it matches:

```javascript
  const digest = treeDigest(model, route, _toggles, live);
  if (digest === _lastTreeDigest && _treeHost.firstChild) return;
  _lastTreeDigest = digest;
```
— `src/zicato/dashboard/static/js/shell.js`, `renderTree`

The upstream chrome guard that keeps a beat from even reaching a rebuild is
`onStateChanged`'s live-data signature: only a real membership/status change
in the generations set busts the drill-down caches and forces a tree
rebuild; a no-op beat leaves the signature equal and busts nothing:

```javascript
  const sig = liveDataSignature();
  if (sig !== _lastLiveSig) {
    _lastLiveSig = sig;
    invalidateLive();
    _lastTreeDigest = null;   // force renderTree to rebuild off the fresh cache
  }
```
— `src/zicato/dashboard/static/js/shell.js`, `onStateChanged`

`liveDataSignature` (in `data.js`) is signed off the gen SET (id +
tri-state status + birth-round + epoch), id-sorted so it is order-
independent — the digest philosophy applied to cache invalidation.

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
— `src/zicato/dashboard/static/js/test/pipeline_stepper.test.mjs`

The companion assertion is on the DIGEST function itself — an identical
projection folds to a byte-identical digest, an advance flips it:

```javascript
  assertEqual(live.pipelineStepperDigest(pipeFixture()), live.pipelineStepperDigest(pipeFixture()),
    'a re-served identical projection is byte-identical (zero DOM)');
  // ...an advance flips the digest...
  assertEqual(live.pipelineStepperDigest(null), 'none', 'a null read folds to the stable none');
```
— `src/zicato/dashboard/static/js/test/pipeline_stepper.test.mjs`

`seq_render_gate.test.mjs` is the render-discipline BACKBONE suite — it
pins `state.noteProgress` (advance / repeat-no-op / rollover / absent-seq
degrade), the `core/sse.js` seq skip gate (a non-advancing frame issues NO
fetch), the four run-states, and the chrome pill's zero-DOM no-op beat.

> ✅ ALWAYS add a "no-op re-serve keeps node identity" assertion when you add
> a live surface. `assert(host.firstChild === first)` after a second
> identical update is the ONE test that catches a stray timestamp in a digest
> or a missing gate. A test that only asserts "it renders the right content"
> passes even when the surface flashes on every beat.

### 9.7.6 The formal checklist

A live surface ships only if it ticks every box:

1. **Server frame is a signal, not content.** The change flows through
   `state_change` (kinds + seq + terminal); the data is a GET. (DQ5)
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
   a refresh; a legacy server with no seq degrades to always-refresh.

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
them. Three durable rules, each load-bearing for "one console, not a fleet of
mini-apps":

- **Tags/chips are ONLY for a semantic state the console already pills** — a
  `verdict` or a `severity` (the `chip`/`verdictPill` family). Everything else is
  text. A metric, a count, a relation, a model name is **not** semantic state, so
  it never earns a chip.
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
> caption; and the ONE surviving pill is the adjudication verdict. Nav rode the
> routes + tree, never a lens-local rail. See
> `docs/design/CONSOLE-DESIGN-LANGUAGE.md` and BOARD-REFLECTION.md §"UI language".

---

## 9.8 Uncertainty-honest rendering — verdicts relative to the noise floor

A dashboard that says "plateaued" or "improving" when the movement is
smaller than the measurement noise is LYING to the operator. The read model
computes verdicts relative to the epoch's measured A/A noise floor and
reports "no detectable signal" when the movement fits inside it (DQ7).

`build_optimization_trajectory` is the worked case. It joins the raw
plateau flag with the epoch's measured `noise_floor` and picks the honest
word:

```python
    floor = _epoch_noise_floor(paths, epoch_id)
    if not traj.plateaued:
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
— `src/zicato/query/loop_view.py`, `build_optimization_trajectory`

The three verdict words and their meaning:

| `verdict` | When | What it tells the operator |
|---|---|---|
| `improving` | not plateaued (real movement across the trailing window) | the loop is making measurable progress |
| `plateaued` | plateaued AND the window's movement is resolvable ABOVE the floor (or no floor was measured) | genuinely flat — the loop found a real local plateau |
| `no_signal` | plateaued AND the window's whole movement sits at/below the measured floor | the data cannot distinguish this from an A/A re-roll of the same generation |

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
> checking it against the measured noise floor. This is DQ7. A movement of
> 0.003 on a floor of 0.66 is not a plateau and not an improvement — it is no
> signal. The proposer's own memory bands round-over-round deltas for exactly
> this reason (05-proposer.md §5.8.6); the dashboard must not un-band them by
> asserting a verdict the measurement cannot support.

> ⚠️ TRAP — "no floor measured yet" is NOT "no signal". When `noise_floor` is
> `None` (an epoch that never ran the A/A calibration), the verdict falls
> back to `plateaued`/`improving` on the raw flag — the honest thing to say
> when you have no floor is the raw observation, not a fabricated "no signal".
> Only a MEASURED floor that the movement fits inside earns `no_signal`.

---

## 9.9 The `svg.js` figure grammar

`svg.js` is a dependency-free SVG data-viz primitive library — one home for
"size text to its box" and ~50 figure builders, each paired with a `*Digest`
function so the figure participates in the render discipline (§9.7). It is
byte-large (4300+ lines) but its public surface is only inline
`export const` / `export function` (no aggregate export block).

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
no-op heartbeat diffs a string instead of the SVG. There used to be ~8
hand-written per-figure `*Digest` functions (~130 LOC) that each re-implemented
the same fold by hand — round to rendered precision, drop timestamps, sort keys.
`digestOpts(opts, omit)` is that fold, generic and in ONE place; the ~7
surviving `*Digest` exports (`racingScalarTrackDigest`, `gauntletFieldBarsDigest`,
`radarSilhouetteDigest`, `proposingDigest`, `diversityMatrixDigest`,
`metaLoopLedgerDigest`, `calibrationTrendDigest`) are now thin wrappers that add
only their own load-bearing normalization (a namespace prefix, an
absent-vs-empty collapse) and an `omit` list.

```javascript
// ── digestOpts — the SINGLE generic figure-opts digest (U5) ───────────
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
> including positional ones (a tick's index, not just its value). If the
> figure moves a mark when a value's RANK changes but the fold sees only
> the value, a rank change that leaves the value equal will not regate the
> DOM and the figure lies. `metaLoopLedgerDigest` folds `champion_index` (the
> tick position) for exactly this reason — `digestOpts` gives it the fold, the
> wrapper decides WHAT to feed it.

### 9.9.4 The shared view-composition builders — `ui.js`

The figure grammar lives in `svg.js`; the DOM-composition grammar the views
share lives in `ui.js`. These builders each fold a copy-paste class the views
used to hand-roll; adopting one is the default, hand-rolling is the exception a
review should question.

| builder | folds | notes |
|---|---|---|
| `renderView(host, ctx, spec)` | the ~11-view opening: first-paint placeholder (`loading()`), optional `await D.epoch` + no-epoch gate, an optional secondary `guard`, digest fold, `gatedSwap` | a view whose flow genuinely diverges (parallel-fused fetches, multiple hosts, a non-epoch gate, conditional sub-render dispatch) keeps its hand scaffold |
| `dataTable(spec)` | the ~14 hand-rolled `thead`/`tbody` scaffolds | per-cell `{class,text}` / `{el}` / `{title}`; conditional columns + cells via `filter(Boolean)`; row-level `class`/`dataset`/`style`/`onClick`. `deltaCell(v)` is the sign-coloured Δ cell |
| `chip(cls, word)` / `pill(cls, word)` | the inline `dn-chip` / `dn-pill` spans | `pill` is the custom-word sibling of `verdictPill` (which derives its own label) |
| `hovercardBody(...children)` | the 7 `dn-hc-body` wrappers | accepts a single array too (the `lines`-array sites) |
| `truncate(s, n)` | the four clip/shorten copies (dag / candidate / boardstatus) | the ONE string-truncate; `svg.fmt`/`fmtSigned`/`isNum` stay the numeric home, re-exported from `ui.js` |
| `emptyState(parent, w, h, label)` | the ~13 centred "no data yet" SVG placeholders | `svg.js`-side (a figure primitive, U5) |

> ✅ ALWAYS reach for the shared builder first. The `dn-`/`dt-` class names are
> STABLE (the class-literal test refs route around, they do not churn), so a
> builder that emits the same classes is a drop-in. A genuinely divergent site
> is extracted with an explicit option/parameter, never by papering over the
> difference — and if it still resists a faithful extraction it is LEFT and
> listed, never forced into a subtle render break. The geometry/null-semantics
> `svg.js` helpers deferred for exactly that reason (`champBench`, `gateOf`,
> `progressSubBar`, `scalarOf`+padded-extent, the edge-clamp→`edgeText`/`fitInto`
> migration, `structure.js`'s `gateState` machine) are the standing worked
> example: `champId` alone is `o.championId ? …` at some sites and
> `o.championId != null ? …` at others — a single helper would silently
> mis-handle a `'0'`/`0` id.

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
    // legacy degrade — derive from the timestamp verdict (byte-identical).
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
DEAD. The mirror of the server-side gate: `loop_view._STALE_HEARTBEAT_S =
30.0` and `_IDLE_HEADS` mirror `STALE_HEARTBEAT_MS` and `IDLE_PHASES` so the
two liveness reads agree.

> ⛔ NEVER key liveness on the heartbeat TIMESTAMP alone. The timestamp
> ages on a slow LLM call (false stall) and keeps stamping on a wedged loop
> (false alive) — see 07-runtime-and-durability.md §7.6.1 for the same lesson
> server-side. The `seq` cursor is the true liveness signal; the timestamp is
> the DEAD/STALLED split (is the process still pulsing?), not the LIVE test.

The token→CSS mapping lives in the chrome (`shell.js`), which patches one
`dt-rs-<state>` class per state — `livestatus.js` emits only the lowercase
token, keeping the colour decision in CSS:

```javascript
    patchClass(_runStateEl, 'dt-rs-live', word ? status.runState === 'live' : false);
    patchClass(_runStateEl, 'dt-rs-stalled', word ? status.runState === 'stalled' : false);
    patchClass(_runStateEl, 'dt-rs-settled', word ? status.runState === 'settled' : false);
    patchClass(_runStateEl, 'dt-rs-dead', word ? status.runState === 'dead' : false);
```
— `src/zicato/dashboard/static/js/shell.js`, `renderStatus`

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
"the single place the phase-string vocabulary is decoded for the pipeline
display — the JS renders the verdict verbatim". `build_round_pipeline`
projects it from the live tournament fold + heartbeat + active-runs count,
staleness-gated exactly like the frontend.

The JS renderer is a straight transcription — one pip per server step, the
active step's detail beside it, the decision word once the round settles.
It owns NONE of the vocabulary:

```javascript
// A compact propose → apply → run → gate stepper rendering the SERVER's
// authoritative /api/live/pipeline projection VERBATIM — the reader owns
// the phase-string inference; this builder never re-derives loop position
// from phase tokens. ...
export function pipelineStepper(pipe) {
  const steps = (pipe && Array.isArray(pipe.steps)) ? pipe.steps : [];
  const wrap = el('div', { class: 'dt-pipe', role: 'img', 'aria-label': 'round pipeline' });
  steps.forEach((s, i) => {
    if (!s || !s.id) return;
    const state = (s.state === 'done' || s.state === 'active') ? s.state : 'pending';
    ...
```
— `src/zicato/dashboard/static/js/live.js`, `pipelineStepper`

The node test pins the verbatim rendering and the digest gate — the server
order is rendered verbatim, the active step's detail renders, a done/pending
step's does not, and an identical re-serve keeps DOM node identity (§9.7.5).

> ⛔ NEVER teach the JS stepper a phase token. If a new phase should advance
> the stepper, add it to `_project_pipeline` (server-side) and the JS renders
> the new `state` automatically. The moment the JS parses `phase` to decide
> which pip is active, you have a second inference the Rust supervisor cannot
> match and a re-derivation that violates DQ1. `data.js::livePipeline`
> null-degrades on a server that does not serve `/api/live/pipeline` (the
> Rust supervisor), so the stepper simply omits — never guesses.

> ⚠️ TRAP — do not conflate the propose→apply→run→gate PIPELINE stepper with
> the RUNG stepper (`live.js::rungStepper`), which shows one pip per
> rung/round of a live tournament — structural progress, not a stage
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
// POST a control marker (pause / skip-round / kill / promote / reject /
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

### 9.12.1 Read-only gating (DQ9)

The loop-control cluster renders ONLY when the workspace is writable
(`state.health.read_only === false`) and the loop is alive-or-paused — it is
hidden read-only, never a disabled-but-visible control at the loop level:

```javascript
  const canControl = !!(state.health && state.health.read_only === false);
  const serverPaused = !!(state.heartbeat && state.heartbeat.paused);
  // The optimistic override retires the moment the server agrees with it.
  if (_pausedOverride != null && serverPaused === _pausedOverride) _pausedOverride = null;
  const paused = _pausedOverride != null ? _pausedOverride : serverPaused;
  const show = canControl && (!!(status && status.alive) || paused);
```
— `src/zicato/dashboard/static/js/shell.js`, `renderLoopControls`

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
    patchText(skip, '⏭ skip round');
    skip.classList.remove('dt-loopctl-armed');
  };
  skip.addEventListener('click', () => {
    if (!armed) {
      armed = true;
      patchText(skip, 'confirm skip?');
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
direction buttons (promote ↑ / reject ✕) plus cancel, never a one-click
force-decision. Both surfaces short-circuit to a spent/disabled state when
an override is already recorded or the round has settled.

### 9.12.3 Paused readback — the explicit-refresh (DQ9)

A control write does NOT advance the orchestrator progress `seq`, so the SSE
no-op-skip gate (§9.7.3) would DROP its `state_change` and the paused
readback would lag. The fix: after a successful POST, stamp an optimistic
override and force an explicit `loadEnvironment()` so the readback converges
and the button flips promptly:

```javascript
async function fireLoopControl(action, body, pausedAfter) {
  let res = { ok: false, status: 0 };
  try { res = await postControl(action, body); } catch (err) { res = { ok: false, status: 0 }; }
  if (res.ok && pausedAfter != null) _pausedOverride = pausedAfter;
  // A control write does not advance the orchestrator progress seq, so the
  // SSE no-op-skip gate drops its state_change — refresh explicitly so the
  // paused readback converges (and the button flips) promptly.
  try { await loadEnvironment(); } catch (err) { /* transient — next beat retries */ }
  _lastLoopCtlDigest = null;
  renderStatus();
}
```
— `src/zicato/dashboard/static/js/shell.js`, `fireLoopControl`

The `_pausedOverride` is optimistic and self-retiring: `renderLoopControls`
clears it the moment `serverPaused === _pausedOverride` (the server agreed),
so a raced or stale override can never stick. The paused state itself rides
on the heartbeat payload (`readers/runtime_view.py::read_paused` →
`heartbeat.paused`) so every runtime read carries it without a second fetch.

> ⚠️ TRAP — this is the one place the digest-gated no-op-skip gate works
> AGAINST you. A control write is a real user intent but not an orchestrator
> transition, so it does not bump `seq`, so the gate correctly (for its own
> purpose) drops the frame. The explicit `loadEnvironment()` after every
> control POST is not redundant — it is what makes a control feel responsive
> under a discipline built to ignore no-op beats. Omit it and the pause
> button appears to do nothing for up to a poll interval.

---

## 9.13 The Rust-supervisor null-degradation duty (DQ8)

Every read API is served by TWO servers — this Python service and the Rust
supervisor (08-supervisor.md). The JS cannot tell which one answered. So
every new GET carries a duty: the client must render an honest empty state
when the endpoint returns `null`/empty, because the Rust supervisor may not
serve it (yet, or at all). The client accessors bake this in — an absent
endpoint degrades to `null`, and the view omits the panel:

```javascript
export async function livePipeline() {
  try { return await fetchJson('/api/live/pipeline'); } catch (err) { return null; }
}
```
— `src/zicato/dashboard/static/js/data.js`, `livePipeline`

`home.js` reads the loop-communication endpoints (`trajectory`, `cost`)
this way and states the duty in a comment: "Both null-degrade (absent
endpoint on the Rust supervisor) → the stats are simply omitted." The
pipeline stepper does the same — `updatePipeline(null)` leaves the host
empty (§9.7.5's node test asserts it: "a null read (Rust supervisor) leaves
the head unchanged").

> ✅ ALWAYS write a new GET's client accessor to null-degrade AND write the
> view to render an honest empty state on `null`. When you add
> `/api/epoch/{id}/newthing`, assume the Rust supervisor does not serve it:
> the accessor returns `null` on a 404, the panel omits or shows "unavailable"
> — never a spinner, never a crash. DQ8. The reciprocal duty is on the Rust
> side (08-supervisor.md §"When a Python payload change requires Rust
> parity"): a payload SHAPE change that the client depends on must land in
> both servers in lock-step, or the two dashboards skew.

> ⚠️ TRAP — the failure mode of skipping DQ8 is invisible on the Python
> service (where you develop) and only shows against the Rust supervisor
> (where operators often run). A new panel that works perfectly in your
> `zicato dashboard` session throws `Cannot read property 'x' of null` under
> the supervisor. Test the accessor's `null` path in the node suite; you
> cannot rely on hitting the Rust server locally.

---

## 9.14 The client read layer — `data.js` accessors, caching, transcripts

`data.js` is the drill-down read layer: a small set of cached,
failure-tolerant GETs. The mechanism is `cachedJson` — a failed read is
cached as `null` so a view paints an honest "unavailable" rather than
spinning forever, and a later `invalidate()` retries:

```javascript
export async function cachedJson(path) {
  if (_cache.has(path)) return _cache.get(path);
  try {
    const data = await fetchJson(path);
    _cache.set(path, data);
    return data;
  } catch (err) {
    // A transient failure is cached as null so the view paints an honest
    // "unavailable" rather than spinning forever; a later invalidate() retries.
    _cache.set(path, null);
    return null;
  }
}
```
— `src/zicato/dashboard/static/js/data.js`, `cachedJson`

Drill-down payloads are immutable for a COMPLETED generation, so caching
avoids re-fetching on every SSE-driven re-render. `invalidateLive()` busts
the keys that can change while a run is live; it fires on a VIEW change AND
(via `liveDataSignature`, §9.7.4) when a new candidate lands mid-round — the
"under-render fix": a new candidate that surfaced by SSE refreshed AppState
but not the cached drill-downs, so the tree digests never flipped and the
operator had to hard-refresh. `invalidateRunTranscript` busts just the two
cache keys a LIVE transcript flows through so a running candidate's
transcript re-reads as new turns land, while the transcript host stays
digest-gated on content (`transcriptDigest`) so a re-read that yields no new
turn is still a no-op repaint — scroll preserved.

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

`dashboard/transcript.py::reconstruct_transcript` turns one goldfive
`events.jsonl` into an ordered `Transcript` (turns + margin annotations). It
is pure (the only I/O is the file the caller hands in) and tolerant — a
malformed/truncated line is skipped, a missing file yields an empty
transcript, mirroring the reducer's plain-JSON fallback and the supervisor's
run-log tailer that parse the same growing file. It handles both goldfive
envelope shapes (camelCase persistence-sink keys and the reducer's
normalized `{kind, payload, ...}`), reusing `to_snake` (§9.3.3) for key
normalization so the transcript speaks the one stable vocabulary. The
endpoints import it behind a guarded `try/except` so the whole server still
starts if it is unavailable in a stripped install.

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
with a `note` (DQ3), never raises:

```python
def build_promotion_cadence(paths: WorkspacePaths, epoch_id: str) -> dict[str, Any]:
    try:
        rows = _cadence_rows(paths.index_db, epoch_id)   # or off the lineage
    except IndexUnavailableError:
        return {"epoch_id": epoch_id, "cadence": [], "note": "index not built; run zicato repair index"}
    except Exception:  # noqa: BLE001 — best-effort, mirrors sibling readers
        return {"epoch_id": epoch_id, "cadence": [], "note": "index unreadable"}
    return {"epoch_id": epoch_id, "cadence": rows}
```

Coerce every numeric with `coerce_float` and every pass flag with
`_opt_bool`; classify any decision token through `decisions.canonical_decision`
/ `promoted_tristate`. Emit ONE spelling per field (DQ2). Do NOT re-derive
"the champion" — read `_current_champion` if you need it (DQ1/DQ10).

**Step 2 — Export it from the package face.** Add the name to the import
block AND `__all__` in `src/zicato/query/__init__.py`. This is what makes
`query.build_promotion_cadence` resolve in the endpoint and keeps the split
invisible (§9.1).

**Step 3 — The endpoint factory + `_is_safe_id`.** Add the handler to the
relevant per-surface factory in `endpoints.py` (here `_make_epoch_endpoints`).
Validate the coordinate FIRST and degrade a malformed one to the reader's
empty shape at HTTP 200 (DQ12):

```python
    async def api_epoch_promotion_cadence(request: Request) -> JSONResponse:
        """``GET /api/epoch/{epoch_id}/promotion-cadence``. Malformed id ⇒ empty (200)."""
        epoch_id = request.path_params["epoch_id"]
        if not _is_safe_id(epoch_id):
            return JSONResponse({"epoch_id": epoch_id, "cadence": []}, status_code=200)
        return JSONResponse(query.build_promotion_cadence(paths, epoch_id))
```

Return it in the factory's dict (`"api_epoch_promotion_cadence": ...`).

**Step 4 — The server route.** Wire it into the `routes` list in
`server.py`, ABOVE the catch-all fallback (§9.4.3):

```python
        Route(
            "/api/epoch/{epoch_id}/promotion-cadence",
            handlers["api_epoch_promotion_cadence"],
        ),
```

**Step 5 — The client accessor, null-degrading (DQ8).** Add a thin cached
accessor to `data.js`, and write it to degrade to `null` on an absent
endpoint (the Rust supervisor may not serve it):

```javascript
export async function promotionCadence(epochId) {
  return cachedJson(`/api/epoch/${enc(epochId)}/promotion-cadence`);
}
```

Add its `/api/epoch` prefix to `invalidateLive()`'s bust list if it can
change while a run is live.

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
fold THAT (§9.9.3), not the raw data.

**Step 7 — The node behaviour test with a no-op assertion (DQ6).** Add a
`*.test.mjs` that renders the panel, captures `host.firstChild`, re-serves
the SAME payload, and asserts node identity:

```javascript
  view.render(host, ctx, { epochId: 'e0' });
  const first = host.firstChild;
  view.render(host, ctx, { epochId: 'e0' });   // identical re-serve
  assert(host.firstChild === first, 'a no-op re-serve keeps DOM node identity');
```

Also assert the null path renders an honest empty state (DQ8) and, if the
panel derives from a served join, extend `mock_server.mjs` to mirror the new
reader (§9.16).

**Step 8 — The Rust degradation check (DQ8).** Confirm the client renders
correctly when the endpoint 404s. Either the accessor's `null` path (tested
in step 7) covers it, or — if the datum should ALSO surface under the
supervisor — mirror the payload in the Rust route and its `state.rs` serde
(08-supervisor.md §"When a Python payload change requires Rust parity"),
keeping `EXPECTED_SCHEMA_VERSION` and the field spellings in lock-step.

**Step 9 — The reader unit test.** In `tests/` add a Python test that
builds a fixture workspace, calls `build_promotion_cadence`, and asserts the
shape AND the degrade path (a never-built index ⇒ the empty shape + note; a
malformed epoch ⇒ empty, no raise). This is the DQ3 pin.

**Verify**

```bash
uv run pytest tests/test_dashboard_endpoints.py tests/test_promotion_cadence.py -q
uv run lint-imports              # the reader must not import the dashboard (DQ4)
make node-test                   # the no-op / null-degrade node assertions
uv run mypy src/zicato/
```

If you skipped step 1's degrade, a never-built index 500s the endpoint
(DQ3). If you skipped step 5's null-degrade, the panel throws under the Rust
supervisor (DQ8). If you skipped step 7's node assertion, the panel flashes
on every beat and nothing in CI notices (§9.7.1).

---

## 9.16 Recipe: change a payload shape (the clean break)

Changing an existing payload's shape is a **clean break**, not a
back-compat dance — server and client change in the SAME commit, every
client-side coalescer is DELETED, and the pins are updated together (DQ11).
The temptation is to add the new field and leave the old one, then teach the
client to read either. That is exactly the alias-growth DQ2 forbids.

**Step 1 — Change the reader.** Rename/reshape the field in the `zicato.query`
reader. Emit ONE spelling (DQ2). If you are replacing `won_by` with a
tri-state `promoted`, remove `won_by` — do not ship both.

**Step 2 — Change the client, delete the coalescer.** Update every view/
accessor that read the old shape to read the new one, and DELETE any
`x.newKey ?? x.oldKey` alias-coalescing you find. A coalescer is the client
compensating for a wobbly wire; the clean break removes the wobble, so the
coalescer must go too. The `livestatus.js::heartbeatTs` comment ("the four
alternate keys are DELETED") is the model — deleting the aliases IS the fix.

**Step 3 — Update the node `mock_server` parity pin.** If the payload is one
of the two SERVED JOINS (round-timeline / racing-field), update
`test/mock_server.mjs` to mirror the new reader shape — it re-derives the
served join from fixtures "exactly as the Python readers do", so a shape
change there is a required, matching edit (its own rule: a divergence is a
bug in the mock, never grounds to re-derive in prod):

```javascript
// this module PLAYS THE SERVER: it derives the two served payloads from a fixture
// map exactly as the Python readers do. It is TEST-ONLY scaffolding — nothing
// under js/ imports it — and any behavioural divergence from the Python
// readers is a bug in THIS file, never grounds to re-derive in prod code.
```
— `src/zicato/dashboard/static/js/test/mock_server.mjs`

**Step 4 — Update the goldens.** If the payload is captured by a parity
golden (the MOCK-GOLDEN gate freezes `gen_score.json` / `experiment.json` /
`lineage.json`; the REINDEX-DUMP gate freezes the index projection), the
shape change legitimately reds those gates. Re-capture with
`ZICATO_PARITY_UPDATE=1` (11-testing.md §"The parity gates") AND state the
behavioural reason in the commit — a golden update is a claim that the new
bytes are correct, never a rubber-stamp.

**Step 5 — Rust parity, if the client depends on the field.** A shape change
the JS reads must land in BOTH servers in the same commit or the two
dashboards skew (DQ8/DQ11). Update the Rust route's serde
(`crates/supervisor/src/...`) to emit the same spelling.

**Verify**

```bash
uv run pytest tests/ -q -k "dashboard or query or the_changed_payload"
make node-test                                 # mock_server + view tests agree
bash tools/parity.sh --only MOCK-GOLDEN --only REINDEX-DUMP   # re-capture if legit
cargo test -p zicato-supervisor                # Rust parity, if applicable
```

> ⛔ NEVER ship a payload change as "add the new field, keep the old one for
> a release". That grows an alias (DQ2), forces the client to coalesce
> (which then never gets removed), and lets the two servers disagree about
> which field is authoritative. The workspace files are canonical and
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
- 08-supervisor.md §"The read-only SQLite discipline" — the Rust twin of
  every reader here; §"When a Python payload change requires Rust parity" —
  the reciprocal of DQ8/DQ11; §"Warn-only heartbeat" — the seq-vs-timestamp
  liveness the four run-states mirror.
- 04-evaluation-statistics.md §"The noise floor" — where the measured A/A
  floor §9.8 reads comes from; §"Train/holdout split" — the board-status
  surface (`compute_board_split` / `boardStatusDigest`).
- 05-proposer.md §5.7 — the round-log vocabulary the proposing tracker
  renders; §5.8.6 — the banding the dashboard must not un-band (DQ7).
- 06-tournament-and-selection.md — where gate verdicts, the racing rungs,
  and `deciding_rule` come from before the readers join them.
- 11-testing.md §"Node conventions" — the digest / no-op / DOM-node-identity
  discipline as a test contract; §"The parity gates" — MOCK-GOLDEN /
  REINDEX-DUMP; §"The import contracts" — the query-stays-dashboard-free pin.
- 12-bug-casebook.md §"Bug #4" — the client champion-scan (first vs
  reigning) behind DQ1.

---

## 9.18 Test map for the subsystem

Where to add (and what will catch) a regression, by concern:

| Concern | Tests |
|---|---|
| decision classifier: canonical token + tri-state + Class-B `null` | `tests/test_dashboard_decision_surface.py` |
| reader degrade (missing index ⇒ empty + note; malformed epoch ⇒ empty) | `tests/test_dashboard_loop_view.py`, per-reader `tests/test_*_view*.py` |
| coercers: `coerce_float` bool-exclusion, `to_snake` Rust parity, `_opt_bool` | `tests/test_query_paths.py` (+ the transcript/aggregator suites for `to_snake`) |
| entry-status four-bucket canon + `status_raw` preservation | `tests/test_dashboard_*runtime*` / the runtime-view suite |
| `_is_safe_id` / degrade-to-200 / `?epoch=` 404 | `tests/test_dashboard_endpoints.py` |
| the served joins (round-timeline / racing-field) match the client mock | `tests/test_dashboard_racing_and_rounds.py` + `test/mock_server.mjs` |
| SSE frame shape (kinds + seq + terminal only), coalescing, ordering | `tests/test_dashboard_sse*.py`, node `live_protocol.test.mjs` |
| the uncertainty-honest verdict (`no_signal` vs `plateaued`) | `tests/test_dashboard_loop_view.py` |
| digest-gated render: no-op DOM identity, seq skip gate, four run-states | node `seq_render_gate.test.mjs`, `pipeline_stepper.test.mjs` |
| the pipeline projection (`_project_pipeline`) | `tests/test_dashboard_loop_view.py` (pure inference) + `pipeline_stepper.test.mjs` |
| controls: read-only 403, two-step confirm, paused readback | node `loop_controls.test.mjs`, `override_taxonomy.test.mjs`, `tests/test_dashboard_gate_endpoint.py` |
| `_current_champion` reigning-spine (bug #4 regression, two-promotion lineage) | `tests/test_dashboard_lineage_ordering.py` / the epoch-view suite |
| the whole Node behaviour suite (digest / no-op / mock parity) | `src/zicato/dashboard/static/test/run-all.mjs` via `make node-test` |
| the query layer stays dashboard-free (DQ4) | `uv run lint-imports` (the import-linter contract) |
