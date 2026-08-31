# Trajectory bootstrap — eval synthesis from foreign agent traces

> **Status.** Built and in the tree. This document specifies how zicato turns
> a directory of foreign agent traces into drafted board entries: the trace
> importer and the imported-trace miner source (§2 to §4 and §9), the
> bootstrap synthesis tier that drafts an entry from an episode (§5), and the
> `reflect suggest --from-trajectories` wiring that runs the chain and
> persists its output (§6). The seam signatures the three call across are
> stated once, in §7. Everything is recommend-only: nothing here auto-edits a
> sealed contract, and every path terminates at a builder draft the operator
> seals.

Companion to [`EVAL-SYNTHESIS.md`](EVAL-SYNTHESIS.md) (this feeds the same
suggestion, admission and inbox path as an additional source for that engine
rather than as a second engine), [`TELEMETRY-DIALECTS.md`](TELEMETRY-DIALECTS.md) (the
`LossProfile`-convergence dialects this program reduces foreign traces
through), and [`BOARD-FORMAT.md`](BOARD-FORMAT.md) (the entry / predicate /
judge schema the drafted artifacts obey).

## 1. Thesis — the on-ramp inversion

Eval synthesis (EVAL-SYNTHESIS.md) improves the instrument from **what the
first loop observes** — it mines episodes from a running evolve loop's
persisted artifacts (`loss.json`, the adjudicated corpus, the hypothesis
ledger). That presupposes a loop: an epoch, generations, a board, champions
that have already run.

Trajectory bootstrap **inverts the on-ramp**. It builds a *measured draft
board from traces before any loop exists*:

> Point zicato at a directory of foreign trace files — production logs from
> **any** agent, captured with zero zicato involvement — and it mines
> signal-episodes, drafts board entries with drafted expectations, and feeds
> them into the same admission → suggestion → inbox surface eval synthesis
> already ships. The operator starts from a measured draft board rather than
> a blank one.

This is the cold-start answer eval synthesis structurally cannot give: eval
synthesis needs a reign to mine; bootstrap needs only a folder of traces.

### 1.1 The goldfive-optional principle (load-bearing)

The operator's standing principle, verbatim:

> **In general, goldfive should be optional for zicato.**

Foreign **ADK-style** event logs and **bare transcripts** are first-class
equals of goldfive traces here rather than a degraded fallback. The importer
works with **zero goldfive artifacts in sight**: no `events.jsonl` in the
goldfive proto shape, no goldfive install, no drift instrument. This is
already structurally true one layer down — TELEMETRY-DIALECTS.md made
`LossProfile` the convergence point and shipped two non-goldfive producers
(`reduce_adk_events`, `reduce_transcript`) that reduce a foreign trace to the
**same** `DialectSignals` bundle a goldfive run folds to. Bootstrap stands on
that seam: it reduces each foreign trace through the **existing** dialect
reducer for its sniffed format and mines the resulting signals. The
zero-goldfive path is pinned by a test (§9): the whole
import → mine → synthesise → persist → apply chain runs with no goldfive
artifact anywhere.

### 1.2 Relationship to eval synthesis

Bootstrap adds one miner source and one synthesis tier, and reuses
everything else eval synthesis built:

- the ranked `MinedEpisode` shape and the total-order ranking (`mining.py`);
- the internal `Suggestion` → surface `Suggestion` bridge (`synthesis.py`);
- the persisted `suggestions.json` shape + the admission render
  (`suggestions.py`);
- the `reflect apply` → builder-draft seam (`apply.py`);
- the operator-only envelope (EVAL-SYNTHESIS.md §7 — foreign traces are
  operator-side artifacts and NEVER enter the proposer's prompt).

A bootstrap suggestion is indistinguishable, on the surface, from an
eval-synthesis suggestion except for its provenance block (§5) naming a
foreign source. The inbox, the admission stats, and the apply path are
unchanged.

## 2. The accepted trace formats + format sniffing

Three formats, each tree-verified against an **existing** dialect reducer
(TELEMETRY-DIALECTS.md). The importer sniffs each file independently and
routes it to that dialect's producer.

| Format | Reducer (verified) | Shape |
|---|---|---|
| **goldfive** `events.jsonl` | `reducer._goldfive_signals` (`reducer.py:827`) | goldfive `Event` dicts — envelope keys (`event_id` / `run_id` / `sequence` / `emitted_at` / `session_id`, or their camelCase twins) + exactly one **nested-dict** payload under a oneof key (`drift_detected`, `run_started`, `goldfive_llm_call_end`, `judgement_emitted`, …). Verified against `reducer._payload` (`reducer.py:255`). |
| **adk_events** `events.jsonl` | `dialects.reduce_adk_events` (`dialects.py:288`) | flat objects, one per line, each a `type` (aliases `event_type` / `kind`) drawn from the ADK vocabulary: `tool_call`, `tool_response`, `agent_transfer` (`transfer`), `error` (`exception`), `model_usage`, `agent_message`, `user_message`, `run_start`. Verified against the §3.2 signal table + `dialects._event_type`. |
| **transcript** (the floor) | `dialects.reduce_transcript` (`dialects.py:402`) | flat objects with a `role` (`user` / `assistant` / `agent` / `human` / `model` / `system`) + a text field (`content` / `text` / `message`), **no** `type`. Verified against `dialects._transcript_lines` / `_message_role`. |

### 2.1 The synthetic-placeholder finding (verified)

Every dialect producer has the signature `(events_jsonl_path, entry:
BoardEntry) -> DialectSignals`, but **none of the three reads `entry` in its
body** — verified by inspection: `reduce_adk_events` (`dialects.py:288`),
`reduce_transcript` (`dialects.py:402`), and `_goldfive_signals`
(`reducer.py:827`) reference only `events_jsonl_path`; `entry` is present
solely to satisfy the `DialectReducer` Protocol (`dialects.py:117`). A foreign
trace has no board entry, so the importer satisfies the parameter with a
**synthetic placeholder** — `BoardEntry(id="__imported__", kind="single_turn",
wall_clock_budget_seconds=1, input="")` — which the producer never inspects.
This is documented here so a future reducer that *does* start reading `entry`
(an entry-kind branch, say) is an explicit, reviewed change rather than a
silent break of the importer.

**Record-size cap (persisted turns).** A foreign trace may carry an unbounded
turn. The *persisted* `ImportedTrace` (§3.1) head-caps each reconstructed turn
(`user_turns` / `agent_turns`) to a documented ceiling (`_PERSISTED_TURN_CHARS`
= 4000 chars) with an elision marker — consistent with the evidence caps
(`opening_user_turn` is already sliced to 2000, `mining.py`). A runaway turn
therefore never balloons an `imported/{trace_id}.json`. The cap is applied in
`_signals_to_json`, so it also flows into the content-hash `trace_id` (still
deterministic); the *in-memory* signals keep full turns for same-run
reconstruction.

### 2.2 The sniffing rules (deterministic, documented)

For each file: read every non-empty line tolerantly (a line that is not JSON,
or is JSON but not an object, is **counted** into `malformed_line_count` and
skipped — never raised, mirroring `dialects._iter_json_objects`). Classify
each parseable object by its **strongest signal**:

- **goldfive-signal** — carries a goldfive envelope marker key (`event_id` /
  `eventId` / `sequence` / `emitted_at` / `emittedAt`) **and** exactly one
  nested-dict payload value (the `_payload` oneof shape).
- **adk-behavioral-signal** — its `type` / `event_type` / `kind` is a
  *behavioral* ADK event: `tool_call`, `tool_response`, `agent_transfer`,
  `transfer`, `error`, `exception`, `model_usage`, `run_start`. (The message
  types `agent_message` / `user_message` are **not** counted here — a
  message-only log is a transcript wearing type tags, see below.)
- **transcript-signal** — a `role` + a text field, no recognised `type`, not
  goldfive-shaped.

The file's dialect is decided by a **fixed precedence** (strongest producer
first — resolves every ambiguity deterministically):

1. any goldfive-signal line ⇒ **goldfive** (the envelope+oneof shape is
   unmistakable and only goldfive emits it);
2. else any adk-behavioral-signal line ⇒ **adk_events** (a single
   `tool_call` / `model_usage` / `error` promotes the whole file to the richer
   dialect — a trace that logs behaviour is scored on the dialect that can
   read it);
3. else any transcript-signal line (or any `*_message` line) ⇒ **transcript**;
4. else (no classifiable line — empty or all-malformed) ⇒ **transcript**, the
   floor, with the record flagged empty.

**The ambiguous case, resolved by rule 2.** A file mixing bare
`{"role":"user","content":…}` transcript lines with a single
`{"type":"tool_call",…}` line sniffs as **adk_events**: any behavioral event
present wins over the transcript floor. A file of pure role/content lines (no
behavioral event, no goldfive envelope) sniffs as **transcript**. This is
deterministic and documented; the KAT fixtures (§9) pin both the mixed
(ambiguous) file and the pure-transcript file.

Sniffing is pure over the file's parseable lines and independent of line
order, so re-sniffing the committed fixture always yields the same dialect.

## 3. The import model

A trace directory → a list of `ImportedTrace` records, one per trace file,
each carrying the reduced signals, the reconstructed conversation, and source
provenance.

### 3.1 The `ImportedTrace` record

Reduction is **via the existing dialect reducer** for the sniffed format
(§2) — the importer calls `reducer.dialect_producer(dialect)(events_path,
placeholder_entry)` and keeps the resulting `DialectSignals`. That bundle
already carries the reconstructed transcript (`agent_turns` / `user_turns`),
the drift counts, the task/cost counts, and the malformed-line count, so the
importer copies bytes from nowhere — it stores the reduced signals plus source
provenance.

```python
@dataclass(frozen=True, slots=True)
class ImportedTrace:
    trace_id: str            # "trace-{8hex}" — content hash, no wall-clock
    source_file: str         # basename of the source trace file (provenance)
    dialect: str             # sniffed: "goldfive" | "adk_events" | "transcript"
    signals: DialectSignals  # the existing dialect reducer's output
    line_count: int          # non-empty source lines seen
    malformed_line_count: int
```

- **`user_turns` / `agent_turns`** — the reconstructed conversation is read
  off `signals.user_turns` / `signals.agent_turns` (exposed as convenience
  properties on the record). The transcript-reconstruction path is the dialect
  reducer's own, unchanged.
- **`trace_id`** — `"trace-" + sha256(source_file | dialect |
  signal_digest)[:8]`, where `signal_digest` is a canonical
  (`sort_keys=True`) JSON of the reduced signal fields. Content-stable and
  **deterministic**: re-importing the same directory yields the same ids
  (idempotent import). **No wall-clock in the id.**

### 3.2 Persisted layout (suggest-minted-dir precedent)

The importer's in-memory function needs **no workspace** (goldfive-optional,
§1.1). When a `reflect suggest --from-trajectories` run persists (§6), the
records land under the **mint-mode reflection dir** eval-synthesis already
carved (EVAL-SYNTHESIS.md §6 "Mint-mode reflection dirs"):

```
.zicato/epochs/{epoch}/reflections/{reflection_id}/
  imported/{trace_id}.json      # one ImportedTrace per file
  suggestions.json              # the mint-mode output (existing)
```

Such a dir carries no `plan.json`, so `list_reflections` skips it and the
builder inbox scans `reflections/*/suggestions.json` directly — identical to
the eval-synthesis mint-mode contract. **Import timestamp policy:** the
records carry **no wall-clock field** and the ids are content hashes, so the
persisted layout is byte-stable across re-imports; GC of suggest-minted dirs
stays deferred (EVAL-SYNTHESIS.md §8), inherited unchanged.

## 4. The imported-trace miner source (this branch)

`reflection/mining.py` gains a **signal-driven** episode source over imported
traces — **no `pass_fail` / judge dependence** (a foreign trace has neither an
expectation nor an adjudicated corpus). It reads only the reduced
`DialectSignals`, so it works identically for all three dialects (a
`transcript` trace simply yields no drift-derived episodes — the honest
zero-drift stance, TELEMETRY-DIALECTS.md §4.1).

### 4.1 The signal-episode vocabulary (from the ADK drift vocabulary)

Each imported trace yields zero or more episodes, one per adverse signal
present. Bound to the §3.2 ADK drift vocabulary + the cost/failure counts:

| Signal episode | `DialectSignals` binding (verified) | Severity | Seeds |
|---|---|---|---|
| **error cascade** | `drift_counts[("tool_error","critical")]` ≥ `MIN_ERROR_CASCADE`, or `task_failure_ratio` ≥ `HIGH_FAILURE_RATIO` | high | bootstrap entry (absence: no tool-error cascade) |
| **abort pattern** | `task_started > 0 and task_failed == task_started` (every observed tool failed) | high | bootstrap entry (absence: completes without total failure) |
| **retry loop** | `drift_counts[("looping_tool_call","warning")]` ≥ `MIN_RETRY_LOOP` | mid | bootstrap entry (absence: no retry loop) |
| **budget blowout** | `token_count` ≥ `MAX_TOKENS` or `llm_call_count` ≥ `MAX_LLM_CALLS` | mid | bootstrap entry (absence: completes under a cost budget) |
| **transfer churn** | `drift_counts[("agent_transfer","info")]` ≥ `MIN_TRANSFER_CHURN` | low | bootstrap entry (absence: bounded transfers) |
| **behavioral** | no adverse signal **and** a substantive conversation (`len(user_turns) ≥ 1` and `agent_text_chars > 0`) | low | LLM-drafted rubric/predicate entry (aux seam) |

The drift kinds (`tool_error` / `looping_tool_call` / `agent_transfer`) are
the kinds `reduce_adk_events` emits (TELEMETRY-DIALECTS.md §3.2), so
the binding is to the shape the real reducer produces. Thresholds are
module-level constants (documented, tunable).

### 4.2 Episode shape + ranking

Each episode is the **existing** `MinedEpisode` (verified fields:
`episode_id`, `episode_type`, `subject`, `summary`, `severity_rank`,
`recency_key`, `coverage_key`, `suggestion_hint`, `evidence`, plus the §4
provenance fields — `mining.py:113`). Imported episodes set:

- `episode_type` — `EPISODE_IMPORTED_SIGNAL` (the signal rows) or
  `EPISODE_IMPORTED_BEHAVIORAL` (the behavioral row);
- `subject` — the `trace_id`;
- `suggestion_hint` — **new**: `HINT_BOOTSTRAP_ENTRY` (signal episodes,
  mechanical) or `HINT_BOOTSTRAP_RUBRIC` (behavioral, LLM-drafted);
- `recency_key` — `0` (a foreign trace has no generation lineage);
- `coverage_key` — the signal magnitude (the cascade / loop / churn count), so
  a bigger cascade outranks a smaller one on the tiebreak;
- `evidence` — `{signal_kind, dialect, source_file, count, is_multi_turn,
  n_user_turns, n_agent_turns, opening_user_turn}` — everything the bootstrap
  synthesiser (§5) needs to draft an entry, plus the reconstruction pointer;
- `source_refs` — `(source_file, signal_kind)` (foreign provenance);
- `source_lineage_ids` — `()`.

The episode id keys on `(episode_type, trace_id, signal_kind)` so two signal
kinds on one trace never collide. Episodes fold into `mine_episodes`'s
**existing** total order (`rank_episodes`, unchanged): `(−severity_rank,
−recency_key, −coverage_key, episode_id)`. With zero workspace episodes (the
goldfive-optional path) the order reduces to severity → coverage → id — total
and byte-stable.

`mine_episodes` gains a keyword-only `imported_traces` parameter (§7); it
mines imported episodes **first and unconditionally** (so a cold / absent
epoch never suppresses them), then folds in the workspace episodes when an
epoch resolves, then ranks the union.

## 5. The bootstrap synthesis tier

The bootstrap synthesis tier turns a bootstrap episode into a drafted
board entry, plugged into the **existing** `synthesis.py` routing by the new
hints (§4.2). The internal `Suggestion` → surface `Suggestion` bridge
(`synthesis.py:864` `synthesize`) is reused verbatim; only the drafting is new.

### 5.1 Entry INPUT — reconstructed from the trace

- **Single-turn trace** (`len(user_turns) ≤ 1`): a `single_turn` entry whose
  `input` is the reconstructed **opening user turn**
  (`episode.evidence["opening_user_turn"]`, read off the trace's
  `user_turns[0]`; empty ⇒ a synthesised neutral opener, flagged in
  provenance). The reconstructed `input` is **head-capped** to a documented
  ceiling (`_BOOTSTRAP_INPUT_CHARS` = 4000 chars) with an elision marker,
  consistent with the persisted-turn cap (§2.1) and the evidence caps; a
  truncation stamps the `input_head_capped` reconstruction flag into provenance.
- **Multi-turn trace** (`len(user_turns) > 1`): a **`multi_turn_emulated`**
  entry (BOARD-FORMAT.md §2.3) — the emulator persona is **scripted from the
  recorded user side**: `UserPersona(goal=<derived from the opening turn>,
  constraints=<"replay the recorded user's turns and intent" + a compact
  digest of the recorded `user_turns`>, stop_when=<"the agent has addressed
  the user's goal">)`, `max_turns = len(user_turns) + 1`. The recorded user
  side becomes the persona brief rather than a verbatim script, because a
  `multi_turn_scripted` entry would over-fit the exact wording while the
  emulated kind carries the *intent*. (Verified entry/persona shape: `UserPersona`
  `board.py:190`, `multi_turn_emulated` requires `user_persona` + `max_turns`,
  BOARD-FORMAT.md §2.3.)

### 5.2 EXPECTATIONS — drafted, honest about what BOARD-FORMAT can express

**The honesty constraint (verified).** A board `expectation` is a post-hoc
matcher over a `RunResult` — `final_output` + `transcript` + `runtime_ms` +
`aborted` (BOARD-FORMAT.md §2.1). It **cannot** see drift counts, tool-error
cascades, or retry loops, because those are *drift and process* properties
rather than output properties. So an "absence predicate" that claims to check "no tool-error
cascade" as an `expectation` would be **dishonest** (it would inspect output
text that carries no such signal). The verified predicate vocabulary is
`predicate` (dotted path over `RunResult`), `expected_text`, `regex`,
`json_schema`, `rubric` (BOARD-FORMAT.md §3.1–§3.5). Binding each signal
episode honestly:

| Signal | Honest binding |
|---|---|
| **abort pattern / error cascade / retry loop / transfer churn** | These are **drift-signal** properties, invisible to a `RunResult` matcher. The bootstrap entry pins the reconstructed **INPUT** and carries the property as a **process signal**, two honest ways: (a) leave the `expectation` **absent** — BOARD-FORMAT.md §3 explicitly supports drift-loss-only scoring — so the re-run's own drift term measures the recurrence; and/or (b) draft an **inline `Judge`** (`Judge.custom`, BOARD-FORMAT.md §4) whose criterion names the observed failure ("the agent must not enter a tool-retry loop"). The entry is tagged `bootstrap:<signal_kind>`. **No fabricated output-predicate.** |
| **budget blowout** | Bind to the entry's own **`wall_clock_budget_seconds`** ceiling — a real, structural budget check (BOARD-FORMAT.md §1.2: over-budget ⇒ worst-case). The bootstrap entry sets a tightened budget derived from the observed cost; no expectation needed. |
| **completes-under-budget** | A `predicate`-kind expectation is honest here: a dotted-path callable over `RunResult` in a small `zicato.reflection.bootstrap_predicates` library, which the entry references by path (bodies are never serialised, BOARD-FORMAT.md §3.1). The callable is more than a bare `not run_result.aborted`, because an abort is the candidate's failure only when it is not an infrastructure or harness abort. So `not_aborted` gates on `is_infra_abort_cause`, the same distinction screening and preflight draw: a parent-kill, worker-crash or unreadable-result abort returns `True`, since none of those is the candidate's fault, while a wall-clock-budget abort returns `False`. This predicate is a second surface signal rather than the sole guard, because an over-budget re-run is already scored worst-case by the entry's own `wall_clock_budget_seconds` ceiling (BOARD-FORMAT.md §1.2). Deferring to `is_infra_abort_cause` also stops it double-penalising a transient harness blip that the worst-case budget path correctly does not cache. |

- **Behavioral episodes** → an **LLM-drafted** `rubric` or `predicate` behind
  the **aux seam** (`CallLLM`, never the harness callable — EVAL-SYNTHESIS.md
  §7), tolerant-parsed and loader-validated the way
  `synthesis._coverage_entry_suggestion` already does (`synthesis.py:620`). A
  parse/validation failure drops that one suggestion with a logged reason.

**All artifacts loader-round-tripped.** Every drafted entry passes
`synthesis._entry_reject_reason` (real `save_board` / `load_board` round-trip,
`synthesis.py:246`) and every drafted judge `synthesis._judge_reject_reason`
before it surfaces — a draft the loader would reject never ships.

### 5.3 Provenance + target-slice policy

The provenance block is the eval-synthesis §4 shape with a **foreign-source**
extension:

```jsonc
"provenance": {
  "miner_version": "eval-synth/1",
  "synth_version": "eval-synth/1",
  "source_episodes": ["<episode_id>", ...],
  "source_refs": ["<source_file>", "<signal_kind>"],
  "source_lineage_ids": [],                 // foreign trace — no generations
  "suggestion_type": "regression_entry",    // or coverage_entry
  "target_slice": "train",                  // see below
  "foreign_source": {                       // the bootstrap extension
    "kind": "trajectory_bootstrap",
    "dialect": "adk_events",
    "trace_id": "trace-...",
    "source_file": "prod-run-01.jsonl"
  }
}
```

**Target-slice policy — bootstrap entries default to `train`.** Justified
against EVAL-SYNTHESIS.md §4's rotation rule. That rule sends coverage / judge
/ harder-variant suggestions to the **incoming rotation** slice because a
*proposer that motivated them could otherwise optimise against the new channel
before it is measured* — the collusion path. **A bootstrap entry has no
motivating proposer**: it is sourced from a *foreign* trace the evolve loop
**never saw** and no zicato proposer produced. The contamination premise of
the rotation rule is therefore absent — there is no lineage to leak to, no
proposer that has seen the trace. This is a **weaker contamination case** than
even a regression entry (which at least pins a scenario the loop ran).
Bootstrap entries thus default to **`train`** (like regression entries,
EVAL-SYNTHESIS.md §4's stated exception), and admission's leakage check (§4 of
eval-synthesis) trivially passes because `source_lineage_ids` is empty — there
is no proposer whose slice could have been seen. The operator may still route
a bootstrap entry into rotation from the inbox; the *default* is train because
the collusion hazard the default guards against does not exist for a foreign
trace.

**The self-trace caveat.** The rotation-routing
freedom above assumes the trace is *foreign* — from an agent zicato does not
evolve. In the **dogfood** case, where the trace comes from the *same* agent
zicato evolves, that assumption breaks: the champion has effectively already
seen the scenario, so an entry bootstrapped from it and promoted to
rotation/holdout is a **false generalization signal** — a holdout is meant to
measure transfer to *unseen* scenarios, and a self-trace scenario is not unseen.
Such entries must stay in **`train`** (a regression suite — the correct home for
"do not regress on this behaviour I already ran"). There is **no reliable
self-detection** (a foreign vs. self trace is indistinguishable from the
directory alone — it depends on operator knowledge of the source), so the
caveat cannot be enforced mechanically; instead it is surfaced where the
operator decides slice routing: the caveat text is embedded in the **suggestion
rationale of every bootstrap entry** (one sentence, `_SELF_TRACE_CAVEAT`), so an
operator reading a draft in the inbox sees the warning before promoting it out
of train.

## 6. The `reflect suggest --from-trajectories` wiring

> Two details of the seam: the CLI passes `imported_traces=` to the
> `synthesize` seam only when the flag is set, so a non-bootstrap caller hits
> the base signature; and `suggestions.SynthesizeSeam`, the Protocol the CLI
> type-checks against, carries the `imported_traces` keyword with an empty
> default as the typed mirror of §7's extended `synthesize`.

- **`reflect suggest --from-trajectories <dir>`** — a new flag on the existing
  `reflect suggest` mode (`cli/commands/reflect.py`), composing with the
  existing flags (`--probe` / `--allow-llm` / `--epoch` / `--json`). When set,
  the CLI:
  1. `import_trajectories(<dir>)` → `list[ImportedTrace]` (§7); persists them
     under the minted reflection dir (`write_imported_traces`);
  2. mines **both** sources when both exist — `mine_episodes(paths, epoch,
     imported_traces=traces)` folds imported + workspace episodes into one
     ranked list (§4.2). With no epoch / empty workspace the imported episodes
     stand alone (the goldfive-optional path);
  3. synthesises via the existing `synthesize(...)` seam extended with
     `imported_traces=` (§7) so the bootstrap tier can reach the
     reconstructions; admission unchanged (foreign provenance rendered, the
     leakage check trivially green per §5.3);
  4. persists `suggestions.json` + the `imported/` records; renders the table.
- **Admission unchanged.** The four probes (EVAL-SYNTHESIS.md §5) run as-is; a
  bootstrap suggestion's empty `source_lineage_ids` makes the leakage check a
  clean pass, and the foreign-source block renders in the inbox row.
- **The inbox renders foreign-source provenance** — the suggestion row shows
  `dialect` + `source_file` as a quiet caption beside the admission banner (the
  findings-panel treatment, EVAL-SYNTHESIS.md §6), so the operator sees a
  suggestion came from `prod-run-01.jsonl` in the `adk_events` dialect rather
  than from a champion's own reign.
- **The command reference** — [`CLI.md`](CLI.md) is generated from
  `zicato --help`, so it carries the `--from-trajectories` row, and
  `tools/parity/golden/cli_help.txt` is captured to match.
- **The un-mocked composition test** (§9) is
  `tests/test_trajectory_bootstrap_composition.py`. It is capability-guarded
  on the §7 bootstrap symbols (`synthesize_bootstrap_suggestions` and
  `synthesize(imported_traces=)`), uses no resolver monkeypatching, and
  carries the goldfive-optional whole-chain assertion of §9.

## 7. Literal seam signatures — copy verbatim, do not reinterpret

> These are the seams the importer, the synthesis tier and the CLI wiring
> call across. The signatures and dataclass fields below are authoritative;
> do not re-derive them from prose elsewhere in this document.

**The importer — `src/zicato/reflection/trace_import.py`:**

```python
@dataclass(frozen=True, slots=True)
class ImportedTrace:
    trace_id: str
    source_file: str
    dialect: str
    signals: DialectSignals          # zicato.telemetry.dialects.DialectSignals
    line_count: int
    malformed_line_count: int

    @property
    def user_turns(self) -> tuple[str, ...]: ...    # -> self.signals.user_turns
    @property
    def agent_turns(self) -> tuple[str, ...]: ...    # -> self.signals.agent_turns
    def to_json(self) -> dict[str, Any]: ...
    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "ImportedTrace": ...

def sniff_dialect(path: Path) -> str: ...
    # -> one of "goldfive" | "adk_events" | "transcript"; deterministic (§2.2)

def import_trace_file(path: Path) -> ImportedTrace: ...
    # sniff + reduce (via the existing dialect reducer) + reconstruct; no workspace

def import_trajectories(trace_dir: Path) -> list[ImportedTrace]: ...
    # every *.jsonl in trace_dir, sorted by filename; unreadable file skipped;
    # empty dir -> []. Pure over the filesystem; ZERO workspace needed.

def write_imported_traces(
    workspace_root: Path, epoch_id: str, reflection_id: str,
    traces: list[ImportedTrace],
) -> Path: ...                        # writes imported/{trace_id}.json; returns the dir

def read_imported_traces(
    workspace_root: Path, epoch_id: str, reflection_id: str,
) -> list[ImportedTrace]: ...        # tolerant: absence/defect -> []
```

**The dialect accessor — `src/zicato/telemetry/reducer.py`:**

```python
def dialect_producer(dialect: str) -> DialectReducer: ...
    # public alias for _resolve_dialect_producer (fail-open to goldfive)
```

**The miner source — `src/zicato/reflection/mining.py`:**

```python
# new episode-type + hint constants
EPISODE_IMPORTED_SIGNAL: str = "imported_signal"
EPISODE_IMPORTED_BEHAVIORAL: str = "imported_behavioral"
HINT_BOOTSTRAP_ENTRY: str = "bootstrap_entry"
HINT_BOOTSTRAP_RUBRIC: str = "bootstrap_rubric"

def imported_trace_episodes(traces: Sequence[ImportedTrace]) -> list[MinedEpisode]: ...
    # pure; signal-driven per §4; no pass_fail/judge dependence

def mine_episodes(
    paths: Any,
    epoch_id: str | None = None,
    *,
    imported_traces: Sequence[ImportedTrace] = (),
) -> list[MinedEpisode]: ...
    # imported episodes mined FIRST + unconditionally, then workspace episodes
    # folded in when an epoch resolves; ranked union returned
```

**The synthesis tier — `src/zicato/reflection/synthesis.py`:**

```python
def synthesize_bootstrap_suggestions(
    episodes: Sequence[MinedEpisode],
    *,
    traces_by_id: Mapping[str, ImportedTrace],
    aux_call_llm: CallLLM | None = None,
) -> list["Suggestion"]: ...          # internal synthesis.Suggestion objects

# and the sync surface seam extended (default keeps the existing callers valid):
def synthesize(
    episodes: Sequence[MinedEpisode],
    *,
    allow_llm: bool = False,
    workspace_root: Path | None = None,
    epoch_id: str | None = None,
    imported_traces: Sequence[ImportedTrace] = (),
) -> list[Any]: ...                   # surface Suggestion objects (unchanged shape)
```

**The surface `Suggestion` a bootstrap entry produces** (the existing
`reflection.suggestions.Suggestion`, fields verbatim — `suggestions.py:72`):

```python
Suggestion(
    suggestion_id=<sug-8hex>,                    # existing content-stable id
    suggestion_type="regression_entry",           # entry-shaped; or "coverage_entry"
    artifact_kind="board_entry",                  # ARTIFACT_BOARD_ENTRY
    subject=<trace_id>,
    summary=<one line>,
    rationale=<why, naming the foreign source>,
    target_slice="train",                         # §5.3 default
    draft_artifact=<entry_to_dict(entry), budget_s normalised>,   # the drafted BoardEntry JSON
    proposed_op={"op": "add_board_entry", "args": {"entry": <draft_artifact>}},
    provenance=<§5.3 block with foreign_source>,
    admission=None,                               # until --probe
    severity_rank=<episode.severity_rank>,
    recency_key=0,
    coverage_key=<episode.coverage_key>,
)
```

## 8. Where the parts live, the injection surface, and what is deferred

The importer is `src/zicato/reflection/trace_import.py`; the imported-trace
miner source is the `imported_trace_episodes` addition to
`reflection/mining.py`, folded into `mine_episodes`; the public
`reducer.dialect_producer` accessor exposes the dialect producers to both. The
bootstrap synthesis tier lives in `reflection/synthesis.py` alongside the
`bootstrap_predicates` library, and the CLI wiring is the
`--from-trajectories` flag on `reflect suggest`.

### 8.1 The prompt-injection surface

Bootstrap reconstructs board artifacts from **untrusted foreign text** — a trace
directory the operator curates but does not author. That recorded text flows
into several places that are **LLM-instruction space at eval time**, so it is a
prompt-injection surface and is disclosed here explicitly:

- **entry `input`** — the reconstructed opening user turn *is* the prompt the
  agent-under-test receives, which is the point of an eval entry; it is
  untrusted by construction, as any adversarial board entry is.
- **judge bodies** — a drift-signal entry's inline `Judge` criterion is a
  static string (`_BOOTSTRAP_JUDGE_CRITERION`) rather than trace-derived, so it
  carries no foreign text. A behavioral entry's rubric is LLM-*drafted*, then loader-validated.
- **emulator persona** (`goal` / `constraints`) and the **aux rubric prompt** —
  these place recorded text into the *emulator*/*aux* LLM's instruction space at
  eval time. A recorded turn like "SYSTEM OVERRIDE: ignore your persona…" would
  otherwise land as a live instruction. These are now wrapped in an explicit
  **untrusted-data frame** (`_fence_recorded`, `_TRACE_DATA_FRAME`): the recorded
  turns sit inside a clearly-fenced block prefixed by a never-follow
  instruction. Drift-signal emulated entries carry no answer-leak-guard
  expectation, and the fence is what keeps raw undelimited foreign text out of
  the emulator's instruction space.

**Delimiting reduces, it does not eliminate, the risk.** A determined injection
can still attempt to break the fence. The standing mitigations remain: the
operator **curates the trace directory** (only pointing zicato at traces they
trust as a source) **and reviews every drafted suggestion** in the inbox before
sealing it (recommend-only, nothing auto-edits a sealed contract, §1). Full
trace anonymisation / injection-scrubbing stays deferred (below).

**Deferred and recorded:**

- **Judge-folding, one emulated entry per trace** — when a single trace fires
  several drift-signal episodes, the tier emits one near-duplicate bootstrap
  entry per `(trace, signal_kind)` pair, each re-running the same reconstructed
  scenario with a different inline judge. Folding those judges into one
  emulated entry, with the same input and the judge tuple unioned, would cut
  re-run cost. It composes on top of the per-signal drafting and needs a
  per-trace grouping pass ahead of `synthesize_bootstrap_suggestions`.

- **Live LLM expectation drafting** — real aux-endpoint rubric/predicate
  drafting for behavioral episodes (endpoint-gated, EVAL-SYNTHESIS.md §7); the
  fixture/scripted-callable tier is built.
- **Streaming or incremental import** — re-importing only new or changed
  trace files. The content-hash ids already make a full re-import idempotent,
  so this is an optimisation rather than a correctness need.
- **Trace anonymisation** — scrubbing personal data from a reconstructed
  conversation before it becomes a board entry. The operator curating the
  trace directory is the current control.
- **Cross-trace episode folding** — one episode spanning a signal seen across
  many traces. The tier emits one episode per `(trace, signal_kind)` pair, and
  folding them needs the same cross-source lifetime record EVAL-SYNTHESIS.md
  §8 defers.

## 9. The importer and the miner source

`src/zicato/reflection/trace_import.py` — the importer per §2/§3 (deterministic
sniffing, dialect-reducer reuse via the synthetic placeholder, the `imported/`
persisted layout, content-hash ids) + `reflection/mining.py` gains
`imported_trace_episodes` per §4, folded into `mine_episodes`'s ranking.

**Fixtures are real-shaped** (the standing rule) — authored as the three
formats actually look:

- a **goldfive** `events.jsonl` (the envelope+oneof shape copied from the test
  corpus — `run_started` / `drift_detected` / `goldfive_llm_call_end`);
- an **adk_events** log (the §3.2 vocabulary — `tool_call` / `tool_response`
  with error status / `agent_transfer` / `model_usage` / `error`);
- a **bare transcript** (`{"role":…, "content":…}` lines);
- a **malformed-line** file (a non-JSON line + a JSON-non-object line, counted
  not crashed);
- an **ambiguous-format** file (transcript lines + one behavioral event —
  sniffing decides `adk_events` deterministically per §2.2).

**Tests:** the sniffing table (each format + ambiguous + malformed + empty);
per-format import known-answers (the reduced signals + reconstruction);
the imported episodes with ranking pins; degrades (empty dir, unreadable
file); determinism (re-import ⇒ identical ids + records); and **the
goldfive-optional proof** — the whole `import → imported_trace_episodes` path
runs and produces episodes with **no goldfive artifact anywhere** (a
transcript / adk_events dir, no goldfive install assumed). **The un-mocked composition test**
(`tests/test_trajectory_bootstrap_composition.py`, capability-guarded on the
§7 bootstrap symbols) closes the chain: a real foreign-trace fixture directory
runs through the real `import_trajectories`, the real `mine_episodes`, the
real `synthesize`, a persisted `suggestions.json` carrying a non-empty
`draft_artifact` and `proposed_op`, and a real `reflect apply` into a builder
draft. No resolver monkeypatching.

## 10. Cross-references

| Topic | Document |
|---|---|
| The UI visualising these traces + the board being created | [`TRAJECTORY-UI.md`](TRAJECTORY-UI.md) |
| The suggestion / admission / inbox engine this feeds | [`EVAL-SYNTHESIS.md`](EVAL-SYNTHESIS.md) |
| `LossProfile` convergence + the three dialect producers | [`TELEMETRY-DIALECTS.md`](TELEMETRY-DIALECTS.md) |
| The entry / predicate / judge schema the drafts obey | [`BOARD-FORMAT.md`](BOARD-FORMAT.md) |
| The emulator persona a multi-turn bootstrap entry scripts | [`EMULATOR.md`](EMULATOR.md) |
| The rotation / holdout split §5.3 argues from | [`OVERFITTING.md`](OVERFITTING.md) |
