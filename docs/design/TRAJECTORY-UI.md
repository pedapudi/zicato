# Imported trace views and suggestion provenance

The Traces view renders imported trajectories as timeline strips with mined
episodes and reconstructed conversations. Read-only trace and suggestion
provenance readers connect an episode to the evaluation artifact it motivated.
These views do not edit the board or publish a contract. The importer, miner,
and synthesis pipeline are specified in
[`TRAJECTORY-BOOTSTRAP.md`](TRAJECTORY-BOOTSTRAP.md).

Companion to [`TRAJECTORY-BOOTSTRAP.md`](TRAJECTORY-BOOTSTRAP.md) (the engine
contract — the persisted `ImportedTrace`/suggestion/episode shapes this UI
reads), [`CONSOLE-DESIGN-LANGUAGE.md`](CONSOLE-DESIGN-LANGUAGE.md) +
[`DESIGN-LANGUAGE.md`](DESIGN-LANGUAGE.md) (the house visual language these
surfaces are built from), and [`EVAL-VIEW.md`](EVAL-VIEW.md) (the Evals
matrix of measured board entries).

## 1. Trajectory strips show imported traces

Each trace is rendered as a compact horizontal figure showing turns, tool
calls, budget use and adverse signals. The trace list and detail view share
this SVG representation, with a designed height of about 120 pixels.

### 1.1 What the strip draws (three rows + a shaded ground)

The strip is a fit-to-width SVG (`width:100%` + `viewBox` +
`preserveAspectRatio`, §4.2 of the design language — **no** fixed pixel width,
**no** pan/zoom) composed of three stacked lanes over a shaded budget ground.
**Fit-to-width, but SENSIBLY SIZED**: like every aspect-locked house hero, it
caps `max-width` (`svg.dn-strip-hero`, and the `svg.` type qualifier is
load-bearing — a bare class cap loses to `.dn-panel > svg`) at the viewBox width
so the figure renders 1:1 at its designed ~120 px height with unscaled mono
text. `max-width: 100%` is NOT a cap: it let the strip balloon to ~2100 × 355 px
on a wide pane, scaling every glyph and label ~3× with it. Never a `max-height`
(it would shear the aspect-locked scale). The lanes:

- **The turn lane** (~48 px) — the reconstructed conversation as **alternating
  lane marks**: a user mark then an agent mark, laid end-to-end, **each mark's
  horizontal extent ∝ `sqrt` of that turn's text length under one global,
  capped scale** (§3.4 — a long agent answer is a wider mark; a terse user
  prompt a narrower one, but no single turn may take more than a quarter of the
  lane). This is the `sparkbar` / staircase convention (word-sized marks on a
  shared baseline, `svg.js:792`) applied to turns instead of losses.
  **Bounded rather than filled, and this is load-bearing.** A mark is a bar of at most **40 % of
  the lane height**, straddling a **mid-lane baseline** — a user turn rises
  above it, an agent turn drops below it (that side is the alternation you read
  at a glance) — with a **≥1 px gap** to its neighbour wherever the extent
  affords one (§3.4: past that density the gap degrades before the mark does).
  Both sides ride the
  neutral `--v2-ink-soft` token at a **reduced `fill-opacity`** (the house
  large-area treatment, as `.dn-spark-band` / `.dn-strip-budget` tint their
  regions), the two distinguished by a density step and never by `good`/`bad`,
  because a turn is not a verdict (the cardinal rule, design-language §2.1). A
  raw `--v2-ink` fill over a mark-sized area is forbidden: a full-lane slab of
  foreground ink fuses a two-turn trace into one solid near-black block in a
  light theme, which is the failure the geometry assertions in
  `traces.test.mjs` pin against.
- **The signal row** (~24 px) — the trace's adverse signals as **house
  caution/bad tone ticks**: an error cascade / abort pattern is a `--v2-bad`
  `✕` tick, a retry loop is a `--v2-caution` `↻` tick, a budget blowout is a
  `--v2-caution` `⏱` tick (the budget-exceeded glyph, design-language §2.2), a
  transfer churn is a neutral `--v2-ink-soft` `⇄` tick. **Honesty (load-bearing):
  the reduced `DialectSignals` carries aggregate counts rather than
  per-event positions** (TELEMETRY-DIALECTS.md — the reducer folds a trace to
  counts).
  So signal ticks are a **labelled cluster in a dedicated signal lane, evenly
  distributed by index — they do NOT claim a real timeline position.** The
  count rides the tick's label (`3 tool errors`). This is stated in the
  strip-model (`signals[].positioned: false`), so introducing a
  positioned-event reducer is an explicit change on both sides rather than a
  silent claim of precision the data does not carry.
- **The budget ground** — a shaded region behind the lanes whose fill ∝ the
  fraction of the cost ceiling the trace reached
  (`max(tokens/MAX_TOKENS, llm_calls/MAX_LLM_CALLS)`, clamped to 1.0), tinted
  `--v2-caution-soft`; a trace that crossed a ceiling shades the whole ground
  and flags `over: true`, so the cost budget is read as an area rather than
  a number.
- **The episode overlay** (~24 px) — each mined episode is a **bracketed span**
  drawn over the strip with its kind glyph: a **signal** episode anchors to its
  matching signal tick (`anchor: "signal"`); the **behavioral** episode (a
  clean conversation) brackets the whole lane (`anchor: "lane"`, `x0:0 → x1:1`).
  The bracket carries the episode's tone + glyph; **clicking an episode focuses
  its suggestion, and clicking a suggestion focuses its episode** — the
  provenance chain (trace region → episode → suggestion) made visible. This is
  the whole point of the surface: you can *trace a drafted board entry back to
  the region of foreign behaviour that motivated it*.

### 1.2 The reused grammar (name the primitives — no new vocabulary)

The strip invents **no new colour vocabulary and no new figure** — it composes
the shipped primitives. Named, so a sibling cannot re-implement the fit math
(the "one bug, ~30 times" clip family the primitives already killed,
`svg.js:157`):

| need | reused primitive (`svg.js`) |
| --- | --- |
| size a turn mark / label to its box without clipping the viewBox | `fitInto` / `fitLabel` / `edgeText` + `textPx` / `CHAR_EM` (`:173`–`:238`) — the ONE mono char-width model |
| the word-sized mark-on-a-baseline lane | the `sparkbar` staircase convention (`:792`) |
| a per-point trend, where a strip degrades to a scalar sparkline | `sparkline` (`:408`, its `markers` / `minSpan` flags for a few-mark trace) |
| fit-to-width, no pan/zoom | the `applyResponsive` / `viewBox` contract (`:253`, design-language §4.2) |
| the crown / status glyphs, defined ONCE | `CROWN` (`:21`) and the shared `↑ ✕ ○ ⏱` mark table (design-language §4.2) |
| the transient hovercard for a mark's detail | `hov(node, tip)` → `hovercard.js` (design-language §4.3) — outside the digest-gated render |
| the stable figure-opts digest for gating | `digestOpts` (`:89`) |

Colour is **only** the six ROLE tokens + the secondary set (design-language
§2): `--v2-ink` / `--v2-ink-soft` (turns), `--v2-bad` (error/abort), `--v2-caution`
(retry/budget), `--v2-accent` (the focused episode/suggestion highlight — the
*one* structural highlight, used sparingly). No hardcoded hex, no new token.

### 1.3 The server pre-computes the strip and the browser only draws it

The **strip-model is server-derived** — the reader emits a fully pre-computed
render model (normalized mark positions and sizes, signal ticks with tone
and glyph, the budget fill fraction, episode spans with anchors) so **the
browser draws straight from it and never derives domain math**. This is the
server-computes-client-renders rule the query layer holds everywhere
(EVAL-VIEW.md). Every position/size is a normalized `[0,1]` float rounded to 4
decimals (byte-stable); the JS multiplies by its pane width. This is the same
discipline the racing/gauntlet figures already follow (a pre-computed model,
`digestOpts`-gated) — the strip joins it.

## 2. The Traces surface

### 2.1 The Traces view

A top-level shell view — a hash route plus a tree node under the epoch,
beside Evals — rendering `build_trace_list` and `build_trace_detail`. It is
**digest-gated** (a no-op SSE heartbeat produces zero DOM — the recurring
flashing-render bug class, design-language §6) and **own-container scroll** (a
long trace list / long conversation scrolls inside its own `dn-table-scroll`
host; the page body never scrolls horizontally, §5).

- **The trace list** — one row per imported trace: the **per-trace strip**
  (the §1 figure, list size), the `source_file` + `dialect` as a `dn-faint`
  caption (metadata is a caption, never a chip — design-language §4.4), and a
  compact turn/signal/episode summary (`6 turns · 3 signals · 2 episodes`).
  Rows are ordered by the reader (episode count desc, then source_file) so the
  richest traces lead. `dialect` is **not** a chip (it is not semantic verdict
  state) — it is caption text.
- **The trace detail** — the **full strip** (hero size) over the
  **reconstructed conversation**, rendered with the **transcript turn
  vocabulary** the run-level conversation diff already speaks (the
  `{role, text}` turn rows the transcript view emits, `transcript_view.py`) — user turns and agent
  turns as alternating speaker rows. The strip's **episode anchors** cross-link
  into the conversation: focusing an episode scrolls/highlights nothing it
  cannot honestly point at (aggregate signals have no turn span — the bracket
  sits in the signal lane), while the behavioral episode highlights the whole
  conversation. A `dn-faint` line states the honest reconstruction note (turns
  are the reducer's reconstruction; user/agent sides are zipped by index — the
  reduced signals carry the two sides as separate ordered tuples rather than
  an interleaved log).
- **Navigation lives in the shell** (design-language §4.4) — the Traces route
  + the tree node, never an internal nav rail.

## 3. The reader contracts (LITERAL shapes — copy verbatim, do not reinterpret)

Three readers live in **`src/zicato/query/trace_view.py`**. They are
server-derived, snake_case on the wire, and degrade cold — the three house
invariants of the query layer: the server computes and the client renders,
each field has one spelling and one encoding on the wire, and every reader is
best-effort (EVAL-VIEW.md).
They are **reflection-scoped** (the persisted `imported/*.json` +
`suggestions.json` live under a mint-mode reflection dir,
TRAJECTORY-BOOTSTRAP.md §3.2). Each resolves the owning epoch from the
`reflection_id` the way `reflection_view._resolve_epoch` does (index-first,
then a tree walk for a plan-less mint dir), reads the persisted imported
traces (`trace_import.read_imported_traces`), and reads the persisted
suggestions (`suggestions.read_suggestions`).

**No engine behind a read (load-bearing).** `reflection.mining` is the
pipeline's engine and itself reads back through the query layer
(`query.reflection_view` among others), so the readers do **not** re-run the
miner. They derive the **episode overlays from the
persisted suggestions** — each bootstrap suggestion's provenance already carries
`source_episodes` (the episode ids), `source_refs = [source_file, signal_kind]`,
and the `foreign_source` block (TRAJECTORY-BOOTSTRAP.md §5.3), so the trace →
episode → suggestion chain reconstructs from the persisted output alone; the
trace figure (lane / signals / budget) reads the reduced `DialectSignals` off
the persisted `ImportedTrace`. This reads the REAL pipeline output and adds no
engine coupling. (Consequence: the overlays show the **drafted** episodes, the
ones that became suggestions, which is the provenance chain this surface is
about; the raw per-signal counts still ride `signal_counts` for the full
telemetry.) Every read is best-effort: an unknown/cold reflection, an unknown
trace/suggestion, or a malformed record degrades to a same-shape payload with
`found: false` — never a raise, never a fabricated number.

**Which endpoint idiom serves them.** All three do blocking file I/O (reading
`imported/*.json` + `suggestions.json`), so each is declared
`off_event_loop=True` in the read-endpoint table (`endpoints.py`,
`READ_ENDPOINTS`) — the reads run in the threadpool and never stall the event
loop — behind an `_is_safe_id` degrade. The three routes are:
`GET /api/reflection/{reflection_id}/traces`,
`GET /api/reflection/{reflection_id}/trace/{trace_id}`,
`GET /api/reflection/{reflection_id}/suggestion/{suggestion_id}/provenance`.
Exported from `zicato.query.__init__`.

### 3.1 `build_trace_list(paths, reflection_id) -> dict` — copy verbatim

```jsonc
{
  "reflection_id": "refl-0001",
  "epoch_id": "e3",
  "found": true,                        // false + traces:[] on unknown/cold reflection
  "trace_count": 2,
  "traces": [
    {
      "trace_id": "trace-ab12cd34",
      "source_file": "prod-run-01.jsonl",
      "dialect": "adk_events",          // caption text, NOT a chip
      "turn_counts": { "user": 3, "agent": 3, "total": 6 },
      "signal_counts": {                // aggregate magnitudes off the reduced signals
        "tool_errors": 3, "task_started": 4, "task_failed": 2,
        "retry_loops": 1, "transfers": 0, "llm_calls": 21, "tokens": 42000
      },
      "episode_count": 2,
      "line_count": 128,
      "malformed_line_count": 0,
      "strip_model": {                  // the PRE-COMPUTED render model (§3.4) — JS draws, never derives
        "trace_id": "trace-ab12cd34",
        "dialect": "adk_events",
        "lane": {
          "turn_count": 6,
          "marks": [
            { "i": 0, "role": "user",  "x0": 0.0,    "x1": 0.0833, "size": 0.25, "chars": 512 },
            { "i": 1, "role": "agent", "x0": 0.0833, "x1": 0.4167, "size": 1.0,  "chars": 2048 }
          ]
        },
        "signals": [
          { "kind": "error_cascade", "tone": "bad",     "glyph": "✕", "count": 3, "label": "3 tool errors", "x": 0.3333, "positioned": false },
          { "kind": "retry_loop",    "tone": "caution", "glyph": "↻", "count": 1, "label": "1 retry loop", "x": 0.6667, "positioned": false }
        ],
        "budget": { "shaded": true, "fill": 0.42, "over": false, "tokens": 42000, "llm_calls": 21, "label": "21 calls · 42k tok" },
        "episodes": [
          { "episode_id": "ep-11aa22bb", "kind": "imported_signal", "signal_kind": "error_cascade",
            "tone": "bad", "glyph": "✕", "x0": 0.2833, "x1": 0.3833, "anchor": "signal",
            "severity_rank": 5, "suggestion_ids": ["sug-77ee88ff"] }
        ],
        "focus_episode_id": null        // set only on a provenance mini-strip (§3.3)
      }
    }
  ]
}
```

### 3.2 `build_trace_detail(paths, reflection_id, trace_id) -> dict` — copy verbatim

```jsonc
{
  "reflection_id": "refl-0001",
  "epoch_id": "e3",
  "found": true,                        // false + empties on unknown trace
  "trace_id": "trace-ab12cd34",
  "source_file": "prod-run-01.jsonl",
  "dialect": "adk_events",
  "line_count": 128,
  "malformed_line_count": 0,
  "signal_counts": { "tool_errors": 3, "task_started": 4, "task_failed": 2,
                     "retry_loops": 1, "transfers": 0, "llm_calls": 21, "tokens": 42000 },
  "strip_model": { "...": "the same §3.4 strip-model as the list row" },
  "turns": [                            // reconstructed conversation — the transcript turn vocabulary
    { "index": 0, "role": "user",  "text": "book me a flight to SFO", "chars": 22, "truncated": false },
    { "index": 1, "role": "agent", "text": "I'll search flights...",  "chars": 2048, "truncated": false }
  ],
  "reconstruction_note": "turns are the reducer's reconstruction; user and agent sides are zipped by index",
  "episodes": [                         // per-episode span + anchor + linked suggestion ids
    { "episode_id": "ep-11aa22bb", "episode_type": "imported_signal", "signal_kind": "error_cascade",
      "summary": "trace 'prod-run-01.jsonl' (adk_events) hit an error cascade — 3 tool error(s), 2/4 tool failure(s)",
      "severity_rank": 5, "tone": "bad", "glyph": "✕",
      "span": { "x0": 0.2833, "x1": 0.3833, "anchor": "signal" },
      "suggestion_ids": ["sug-77ee88ff"] }
  ]
}
```

### 3.3 `build_suggestion_provenance(paths, reflection_id, suggestion_id) -> dict` — copy verbatim

```jsonc
{
  "reflection_id": "refl-0001",
  "epoch_id": "e3",
  "found": true,                        // false + empties on unknown suggestion
  "suggestion_id": "sug-77ee88ff",
  "suggestion_type": "regression_entry",
  "subject": "trace-ab12cd34",
  "summary": "pin the reconstructed scenario that hit an error cascade",
  "target_slice": "train",
  "foreign_source": {                   // present iff a bootstrap suggestion; else null
    "kind": "trajectory_bootstrap", "dialect": "adk_events",
    "trace_id": "trace-ab12cd34", "source_file": "prod-run-01.jsonl"
  },
  "admission_viz": {                    // render-ready admission marks (BT-whisker/pip vocab)
    "measured": true,                   // false when synthesis ran plan-mode (no probe)
    "evidence_tier": "probed",          // "probed" (spent) | "planned" (unmeasured)
    "flip": { "measured": true, "rate": 0.2, "runs": 5, "over_ceiling": false, "ceiling": 0.25 },
    "discrimination": { "measured": true, "separated": 3, "pairs": 4 },
    "leakage_ok": true                  // false when a leakage flag fired (null when unchecked)
  },
  "episodes": [                         // the provenance chain: suggestion -> episodes -> trace segment
    { "episode_id": "ep-11aa22bb", "episode_type": "imported_signal", "signal_kind": "error_cascade",
      "summary": "trace 'prod-run-01.jsonl' (adk_events) hit an error cascade — 3 tool error(s), 2/4 tool failure(s)",
      "tone": "bad", "glyph": "✕", "severity_rank": 5,
      "trace_id": "trace-ab12cd34", "source_file": "prod-run-01.jsonl",
      "segment_strip_model": { "...": "the §3.4 strip-model for this trace, focus_episode_id = ep-11aa22bb" } }
  ]
}
```

### 3.4 The strip-model (`build_strip_model`, pure — unit-tested, deterministic)

A **pure** function `build_strip_model(trace, episodes, suggestions_by_episode)
-> dict` in `trace_view.py`, independently unit-tested (no I/O, deterministic,
byte-stable). It is the one place the render math lives. The shape is the
`strip_model` object in §3.1. The computation:

- **Lane marks — the COMPRESSIVE, CAPPED extent scale.** Zip `user_turns` /
  `agent_turns` by index into the alternating sequence `[u0, a0, u1, a1, …]` (a
  trailing unmatched turn is appended) and lay them end-to-end from `x0 = 0`.
  Each mark's extent is **proportional to `sqrt(chars + 1)`** under one
  global scale:

  ```
  w[k]    = sqrt(chars[k] + 1)
  scale   = min(LANE_EXTENT_CAP / max(w), 1 / Σw)     # cap, then saturation fit
  x1[k]   = x0[k] + w[k] * scale                      # x0[k] = x1[k-1]
  ```

  with `LANE_EXTENT_CAP = 0.25` (exported from `trace_view`). `sqrt` compresses
  honestly and monotonically: a 4096-char answer is 8 times the width of a
  64-char prompt rather than 64 times. Because the scale is a single scalar,
  the extent ratios are preserved and nothing is flattened or redistributed.
  The first term caps the widest mark at a quarter of the lane; the second
  stops the run from overflowing it. **The lane is therefore a capacity that
  a trace fills partially, rather than a share of a fixed whole.**
  A 2-turn trace reads as two proportioned bars over a mostly-empty lane (the
  empty room is itself the honest signal "this trace has two turns"), a
  many-turn trace saturates and tiles the lane (only then is the final
  `x1` pinned to `1.0`), and a 500-turn trace still resolves as a comb (the
  figure floors a mark at 0.75 px and degrades the gap rather than the mark).
  A raw `chars / Σchars` share would force every lane to tile regardless of
  turn count, which is the near-black-block failure above. `size` =
  `chars / max_chars` (the tallest mark = 1.0) and is a HEIGHT hint only: the
  figure maps it onto a bar of at most 40 % of the lane height (§1.1), never a
  full-lane slab. A zero-char trace ⇒ even spacing AND `size` 0.0 — a text-free
  lane saturates, so a height claim there would tile it with maximum bars for the
  least informative input. Rounded to 4 decimals.
- **Signals.** One entry per adverse signal present, in a fixed kind order
  (error_cascade, abort_pattern, retry_loop, budget_blowout, transfer_churn),
  each with its tone + glyph (§3.5) + count + label. `x` is evenly distributed
  `(k+1)/(n+1)` — `positioned: false` (aggregate, no real position).
- **Budget.** `fill = round(min(1.0, max(tokens/MAX_TOKENS, llm_calls/MAX_LLM_CALLS)), 4)`;
  `over = tokens >= MAX_TOKENS or llm_calls >= MAX_LLM_CALLS`; `shaded = fill > 0`.
  (`MAX_TOKENS` / `MAX_LLM_CALLS` mirror the `mining` module constants locally —
  the query layer cannot import `mining`, per the dashboard-free rule above.)
- **Episodes.** A **signal** episode (`imported_signal`) anchors to its matching
  signal tick: `x0/x1` = the tick's `x ± 0.05` (clamped `[0,1]`),
  `anchor: "signal"`. The **behavioral** episode (`imported_behavioral`) spans
  the lane: `x0: 0.0, x1: 1.0, anchor: "lane"`. Each carries its tone/glyph +
  `severity_rank` + the linked `suggestion_ids` (from
  `suggestions_by_episode`). `focus_episode_id` is `None` here; the provenance
  reader sets it when emitting a mini-strip.

### 3.5 The signal → tone/glyph table (one source, no new colour vocabulary)

| signal_kind | tone (→ token) | glyph |
| --- | --- | --- |
| `error_cascade` | `bad` (`--v2-bad`) | `✕` |
| `abort_pattern` | `bad` (`--v2-bad`) | `✕` |
| `retry_loop` | `caution` (`--v2-caution`) | `↻` |
| `budget_blowout` | `caution` (`--v2-caution`) | `⏱` |
| `transfer_churn` | `neutral` (`--v2-ink-soft`) | `⇄` |
| `behavioral` | `neutral` (`--v2-ink-soft`) | `○` |

The focused episode/suggestion highlight is `--v2-accent` (the one structural
highlight). Nothing here is a new token; all six + the secondary set are the
design-language §2 contract.

## 4. Render discipline + register (the house rules, restated for the siblings)

- **Keep provenance separate from measurement.** A suggested artifact is not
  a scored board entry. Admission results describe the suggestion; missing
  measurements remain unknown.

### 4.1 The real-payload composition check

**The node tests for the Traces view render from
payloads produced by the real readers over a seeded workspace, never from
hand-authored mock shapes.** A fixture generator makes this possible:

- **`tools/gen_trace_view_fixtures.py`** (a small, dependency-light script)
  seeds a temp workspace with a foreign-trace directory (the three dialects +
  the ambiguous + the malformed file — the same real-shaped fixtures
  TRAJECTORY-BOOTSTRAP.md §9 pins), runs the **REAL** pipeline —
  `import_trajectories` → `write_imported_traces` → `mine_episodes` →
  `synthesize` (mechanical tiers, no LLM) → `write_suggestions` — then calls the
  **REAL** `build_trace_list` / `build_trace_detail` /
  `build_suggestion_provenance` and writes their payloads verbatim as JSON
  fixtures under `src/zicato/dashboard/static/test/fixtures/trace_view/`
  (`list.json`, `detail.json`, `provenance.json`).
- The **node suite** renders the strip and trace detail from those fixtures.
  A mismatch between reader fields and browser consumption fails the test.
  The generator's `--check` mode verifies byte stability because the readers
  are deterministic.

## 5. Where the parts live, and what is deferred

The readers, the pure `build_strip_model`, and the pure helpers are in
`src/zicato/query/trace_view.py`, exported from `zicato.query.__init__`. The
three endpoints sit in the reflection-endpoints block, each wrapping its
reader in `run_in_threadpool` behind an `_is_safe_id` degrade, and are routed
in `server.py`. The Traces view is `views/traces.js`, drawing the strip from
the `svg.js` primitives. The fixture generator and the captured fixtures back the
node render tests of §4.1.

**Deferred and recorded:**

- **Live-updating strips during import** — re-rendering a strip as a trace
  streams in. Import is a batch operation, turning a folder into records; a
  live strip needs an incremental import event stream, which is itself
  deferred (TRAJECTORY-BOOTSTRAP.md §8.1).
- **Harmonograf cross-links for imported traces** — deep-linking a trace's
  strip into a harmonograf span. A foreign trace has no zicato run id / session
  id in the general case (`DialectSignals.run_id` / `adk_session_id` are empty
  for a bare transcript), so the cross-link is conditional and partial;
  deferred until the harmonograf-for-foreign-traces seam is designed.
- **Positioned signal events** — a reducer that emits per-event positions
  rather than aggregate counts would let signal ticks sit at their real
  timeline moment. The strip-model already flags `positioned:false`, and a
  positioned reducer would be an explicit change on both sides.

## 6. Cross-references

| Topic | Document |
| --- | --- |
| The engine contract (ImportedTrace / suggestion / episode shapes) | [`TRAJECTORY-BOOTSTRAP.md`](TRAJECTORY-BOOTSTRAP.md) |
| The house visual language (tokens, figures, render discipline) | [`CONSOLE-DESIGN-LANGUAGE.md`](CONSOLE-DESIGN-LANGUAGE.md) · [`DESIGN-LANGUAGE.md`](DESIGN-LANGUAGE.md) |
| The Evals matrix of measured board entries | [`EVAL-VIEW.md`](EVAL-VIEW.md) |
| The suggestion and admission engine | [`EVAL-SYNTHESIS.md`](EVAL-SYNTHESIS.md) |
| The reduced `DialectSignals` the strip reads | [`TELEMETRY-DIALECTS.md`](TELEMETRY-DIALECTS.md) |
