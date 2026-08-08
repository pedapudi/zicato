# The eval-centric view — the board as the measurement instrument

> **STATUS — DESIGNED. Reader foundation SHIPPING with this doc; the three
> views build FROM this doc.** This is the whole program's execution
> contract: the four workstreams below (§5) are specified so three sibling
> view-agents can build in parallel with nothing but this file and the
> reader payloads it fixes (§3). Read-side only — no contract surface, no
> parity artifact moves.

## 1. Thesis and the three lenses

Zicato's UI is **candidate-centric**: every surface answers "how did this
generation do?" — the lineage DAG, the candidate dossier, the per-matchup
grid, the trajectory. That is the optimizer's view. It hides the question a
measurement scientist asks first: **is the instrument any good?** The board
is not scenery around the tournament — it *is* the measurement apparatus,
and each entry is a channel with its own noise, discrimination, redundancy,
and cost. This program transposes the matrix: rows are **entries** (the
instrument), columns are **candidates** (what the instrument measured).

Three lenses, each a view (§5):

1. **OUTCOMES** — the entries × candidates matrix. One cell = how candidate
   *c* scored on entry *e*. The transpose of the candidate dossier's
   board-breakdown. (WS-MATRIX.)
2. **INSTRUMENT QUALITY** — every eval as a measurement channel: its A/A
   noise (flip rate), its discrimination (does it ever separate two
   candidates?), its redundancy (does another eval say the same thing?),
   its cost (runtime). The generalization of the noise-floor / MDE
   doctrine to the *per-entry* grain. (WS-DOSSIER per-entry + WS-HEALTH
   epoch-wide.)
3. **LIFECYCLE** — rotation, holdout membership, retirement: which slice an
   entry lives in, when it is played, whether it still earns its keep.
   (WS-HEALTH + the row flagging that threads through all three.)

### Relationship to what exists (named files)

This program **reuses readers and adds two**; it invents no new persistence.

- **Candidate dossier board-breakdown table** —
  `zicato.query.judge_view.build_per_entry_for_generation`
  (`src/zicato/query/judge_view.py:180`), rendered by the board dossier in
  `src/zicato/dashboard/static/js/views/board.js`. That is ONE column of our
  matrix (a single candidate's entries). WS-DOSSIER upgrades that page.
- **Per-matchup entry grids** — `build_matchup_grid` /
  `/api/matchup-grid` (`src/zicato/dashboard/endpoints.py:776`). A single
  matchup's two-candidate slice of the matrix.
- **The passive entry×candidate matrix** —
  `zicato.query.reflection_view.entry_candidate_matrix`
  (`src/zicato/query/reflection_view.py:508`) already folds mean `drift_loss`
  per (entry, candidate) straight off the index. It is the closest prior
  art and the seed of `build_eval_matrix`; the new reader supersedes it with
  evidence, pass-ratio, holdout flagging, and calibration (§3). We keep the
  old function (the Instrument lens' passive tier still imports it) and layer
  the richer reader beside it.
- **Board-status / rotation surface** —
  `zicato.query.epoch_view.compute_board_split`
  (`src/zicato/query/epoch_view.py:527`) + the board-status view
  `src/zicato/dashboard/static/js/views/boardstatus.js`. WS-HEALTH
  adjudicates against this surface (§5, WS-HEALTH).
- **Reflection scorecards** — `zicato.query.reflection_view`
  (summary / scorecards / x-ray) rendered by the Instrument lens
  `src/zicato/dashboard/static/js/views/instrument.js`. WS-DOSSIER and
  WS-HEALTH LINK into these (findings, redundancy) rather than recomputing.
- **The MDE / noise doctrine** — `docs/design/CAMPAIGN.md` §3 (two-sample
  MDE math), `docs/design/OVERFITTING.md` (holdout), and the runtime
  A/A calibration `zicato.tournament.calibration`
  (`src/zicato/tournament/calibration.py`). §4 lifts the §3 formula to a
  live MDE ladder.

## 2. Data bindings (every binding tree-verified)

The **backbone is the `loss_profiles` index table** — one row per run.
Verified columns (`src/zicato/index/schema.py:132`):

```
run_id · epoch_id · generation_id · entry_id · drift_loss · pass_fail
runtime_ms · wall_clock_budget_exceeded · loss_json · tournament_id
match_id · cached · source_epoch · source_run · abort_cause
```

**PK REALITY (internalize before touching this table).** `run_id` is the
**PRIMARY KEY** (`schema.py:133`) and the reducer's default is
`run_id = "{generation_id}:{entry.id}"` (`reducer.py:1190`); ingest upserts
`ON CONFLICT(run_id) DO UPDATE` with `match_id = COALESCE(...)`
(`ingest.py:346`). So there is **exactly ONE `loss_profiles` row per
`(generation_id, entry_id)`**, and its `match_id`/`tournament_id` are
LAST-WINS at ingest. Two consequences drive the corrected bindings below:

* **A row count is NOT a replicate count** — it is always 1. Cell EVIDENCE
  must come from the on-disk replicate files (§2.1), not `len(rows)`.
* **"Same-match_id row pairs" cannot exist** — the table can never hold two
  rows for one `(gen, entry)`, so discrimination CANNOT be derived from
  `loss_profiles` (§2.3). It binds to the durable matchup records instead.

The matrix is an **indexed query** over this table for the AXES + cell
membership, not a new store. Read helpers already exist
(`src/zicato/index/query.py`): `loss_profiles_for_generation` (`:228`),
`loss_profiles_for_tournament` (`:281`), `generations_for_epoch` (`:184`,
carries `round_index` / `elo` / `elo_se`), `tournaments_for_epoch` (`:909`),
`elo_for_epoch` (`:940`). All are missing-index-tolerant (return `[]`). The
per-cell EVIDENCE and DISCRIMINATION quantities read the durable **files**
(`generations/<gen>/runs/<entry>/loss*.json`) — index-free by design.

Each derived quantity and its exact, verified binding:

### 2.1 The matrix cell (entry × candidate)
The `loss_profiles` row for `(entry_id, generation_id)` supplies cell
MEMBERSHIP (which candidate columns an entry appears in) plus `cached` /
`latest_run_id`; `drift_loss` / `pass_fail` and the continuous `score` /
`metrics` (parsed from the `loss_json` blob exactly as
`build_per_entry_for_generation` does, `judge_view.py:255`) are its fallback
values. But **REPLICATE COUNT + EVIDENCE come from the durable replicate
FILES, not the row count** (which is always 1 — see the PK reality above).

**The evidence binding (`_cell_replicate_draws`, `eval_view.py`).** For each
cell we read the `loss.json` (replicate 0) + `loss.r<N>.json` siblings that
actually exist under `generations/<gen>/runs/<entry>/`, filtered to the
replicate ranges that count as fresh evidence FOR THE CELL: the **real duel
replicates `[0, 1000)`** (r0 canonical + the low duel slots the
holdout-ladder confirmation re-runs reuse) and the **evidence-gate draws
`[4000, 5000)`** (`EVIDENCE_REPLICATE_BASE`). EXCLUDED — a different
measurement, not this cell's evidence: A/A **calibration `[1000, 2000)`**
(the champion noise-floor trace; it feeds the flip badge, §2.2), the
contract **pre-flight `[2000, 3000)`**, the pre-tournament candidate
**screen `[3000, 4000)`** (an ephemeral veto probe), and **reflection
`[5000, …)`** (a meta-evaluation of the judges). `pass_ratio` / `pass_fail`
/ `drift_loss` / `score` are averaged over those same qualifying draws; the
cell falls back to the single index row only when the `runs/` dir was pruned.
`cached` / `source_epoch` / `source_run` mark a carried-over champion result
(a materialised fast-mode reuse — schema v6, `schema.py:320`), so the view
renders it as scored-but-cached and never double-counts it.

### 2.2 Per-entry flip rate (calibration) — THE TRACE
The A/A calibration (`measure_noise_floor`,
`src/zicato/tournament/calibration.py:129`) duels the champion against
itself `runs=K` times (default 5, `DEFAULT_CALIBRATION_RUNS`). Each draw
evaluates the **full board** through `_run_board_units_fast` on replicate
index `CALIBRATION_REPLICATE_BASE + draw` (base **1000**) with
`match_id="aa-calibration:{draw}"`. The runner persists each board unit's
result per replicate: `_unit_loss_path`
(`src/zicato/tournament/unit_cache.py:106`) maps replicate `r>0` to
`epochs/<epoch>/generations/<gen>/runs/<entry>/loss.r<r>.json` (replicate 0
is the canonical `loss.json`).

**Critical, verified gotcha:** these replicate files are **NOT ingested**
into `loss_profiles`. `_ingest_run_into` (`src/zicato/index/ingest.py:933`)
reads exactly one `loss_profile_path` (replicate 0) per entry, and
`_iter_run_entry_ids` (`:892`) walks only the per-entry `runs/` directories,
never the `loss.r<N>.json` siblings. So the index cannot supply per-entry
flip rates — the calibration draws live only on disk.

**The binding.** The persisted `NoiseFloor` (config.json's additive
`noise_floor` field, written by `set_epoch_noise_floor`,
`src/zicato/epoch/lifecycle.py:697`; shape `NoiseFloor.to_json`,
`calibration.py:98`) carries `generation_id` (the champion that duelled
itself) and `runs` (K). The reader:
1. reads `noise_floor` off `config.json` → `(champion_gen, K)`;
2. for each board entry, reads `loss.r<1000+i>.json` for `i in [0, K)` via
   `read_loss_profile` (the reducer's reader, `unit_cache` twin), taking
   each draw's `pass_fail`;
3. **per-entry flip rate** = `min(#pass, #fail) / n_usable` over the usable
   (non-`None`) draws — the fraction of A/A draws whose verdict flipped from
   the majority. `None` when fewer than two usable draws exist.

Absent `noise_floor`, or missing replicate files → **flip rate unmeasured**
(never 0; §4).

### 2.3 Discrimination (the reign's settled matchups)
An entry discriminates a matchup when the two competitors' verdicts differ
on it. **This CANNOT bind to `loss_profiles` same-`match_id` pairs** — the
PK forbids two rows per `(gen, entry)`, so those pairs never exist (the
original §2 premise was false). Bind instead to the **durable matchup
records** — the same source the recombination builder trusts
(`_build_recombination_pair`, `evolve/round_context.py`, via
`build_matchup_grid`).

**The binding (`_discrimination_by_entry`, `eval_view.py`).** Enumerate the
reign's SETTLED matchups from the experiment records
(`_read_epoch_experiments`): each challenger whose `experiment.json` recorded
a decision (it raced) is a matchup `(parent_generation_id → champion,
generation_id → challenger)`, deduped on the pair. For each matchup,
`build_matchup_grid(paths, epoch, champion, challenger)` reads BOTH sides'
per-entry `loss.json` (`entry_grid[].parent_pass` / `child_pass`, drift-free
pass bits). Per entry: a matchup is a **comparison** when both sides have a
usable verdict, and **discriminating** when the two differ. Folding the
per-entry `[(matchup_key, verdict), …]` through the pure `discrimination`
helper gives `rate = discriminating / comparisons` and
`discrimination_pairs = comparisons`. An entry that is always-pass or
always-fail across every matchup discriminates nothing (a **dead** channel;
§5 WS-HEALTH). The grid reads are POOLED one-per-matchup (the reign is
bounded); the health + dossier surfaces share this one map so they always
agree, and the reader runs in the endpoint threadpool (§5, F5). A gauntlet
where the champion faces three challengers yields
`discrimination_pairs = 3`.

### 2.4 Holdout membership (the split)
There is **no persisted split record** — membership is *computed* and must
match the gate exactly. Bind to `zicato.board.split.split_board`
(`src/zicato/board/split.py:73`) with `seed = rotation_seed(cfg, epoch_id)`
(`:123`) — the same call the gate makes
(`zicato.tournament.governance._holdout_aggs`, `governance.py:77`). Inputs:
the board (`board.jsonl` via `_parse_board`, `epoch_view.py:56`) and
`weights.overfitting` (the `overfitting` block on `scoring.json`,
`_overfitting_block_from_scoring`, `epoch_view.py:469`). Rule: an explicit
`holdout` tag wins; else a `sha256(seed\x00id)` hash-bucket below
`holdout_fraction·10⁶`. **Note (verified discrepancy):**
`epoch_view.compute_board_split` uses a *different, approximate* selection
(sorted-tail over a distinct hash) — the eval readers bind to the canonical
`split_board` so the flagged holdout is byte-exact with the gate, not the
board-status approximation.

### 2.5 The noise floor + MDE inputs
The scalar floor is `noise_floor.max_abs_delta` on `config.json`
(`margin_below_floor`, `calibration.py:233`). The MDE ladder (§4) is
computed from that floor and the epoch's realised replicate count — no new
persistence.

### 2.6 Reflection findings
Redundancy clusters and judge findings come from the reflection readers
(`reflection_view.build_reflection_summary` / `build_judge_scorecards`,
whose `redundant_with` is the corpus redundancy). WS-DOSSIER and WS-HEALTH
**link** into them; they do not recompute reflection analysis.

## 3. The reader contracts

Two readers land in `src/zicato/query/eval_view.py`, plus
`facet_scores_for_generation` (§3.4). House invariants they
obey (verified against the sibling readers): **DQ1** server-derived (the
view renders, never computes domain math); **DQ2** snake_case payloads;
**DQ3** cold-index / unknown-entry / no-calibration degrade to a
*same-shape* payload with `found: False` (never raise, never a bare int on
the wire — `_opt_bool` / `coerce_float` from `zicato.query.paths`).

### 3.1 `build_eval_matrix(paths, epoch_id) -> dict`
The OUTCOMES lens payload.

```jsonc
{
  "epoch_id": "e3",
  "found": true,                     // false + note on unknown/cold epoch
  "candidates": [                    // COLUMN ORDER: round order, then created_at
    { "generation_id": "g0", "round_index": 0,
      "promoted": true,              // TRISTATE: true | false | null (in-flight)
      "champion_spine": true,        // on the promoted-champion path (promoted===true)
      "elo": 1503.2, "elo_se": 44.1 }
  ],
  "entries": [                       // ROW ORDER: board order (board.jsonl)
    { "entry_id": "task_login", "slice": "holdout", "tag": "holdout",
      "flip_rate": 0.2, "flip_rate_measured": true,
      "calibration_runs": 5, "calibration_generation": "g0" }
  ],
  "cells": [                         // entries × candidates; a missing cell is null
    [ { "drift_loss": 0.31, "pass_ratio": 1.0, "pass_fail": true,
        "score": 0.88, "replicates": 2, "cached": false,
        "latest_run_id": "run_abc", "runtime_ms_mean": 41200.0,
        "evidence": "replicated" } ]
  ],
  "calibration": { "measured": true, "generation_id": "g0",
                   "runs": 5, "max_abs_delta": 0.06 }
}
```

Aggregation rules (the cell's **replicate draws**, §2.1 — the qualifying
`loss*.json` files, NOT the `loss_profiles` row, whose count is always 1):
- `replicates` = number of qualifying replicate FILES for that (gen, entry).
- `pass_ratio` = mean of the non-`None` `pass_fail` bits; `pass_fail` = the
  ratio's majority verdict (`None` when no bits).
- `drift_loss` / `score` / `runtime_ms_mean` = mean over the draws.
- `latest_run_id` = the last run id in `(entry_id, run_id)` order (the
  index' stable order — the index row supplies this).
- `cached` = **any** draw/row cached (a cell is cached if its result was
  carried over — never counted as a fresh measurement).
- `evidence` = `"none"` (0 draws) | `"single"` (1) | `"replicated"` (≥2).
  Drives §4 shading: a single-sample verdict renders faint. `"replicated"`
  is reachable exactly when ≥2 qualifying replicate files exist on disk.

Column ordering: `(round_index ?? +inf, created_at, generation_id)`.
`promoted` is **tri-state** (F1): the canonical
`promoted_tristate(experiment_decision(experiment.json))` — `true` (won the
gate), `false` (lost), `null` (in-flight / never raced); the index
`promoted` bool is the fallback ONLY when the experiment record is
unreadable (a readable-but-undecided experiment stays `null`, never a
collapsed `false` — decisions.py's "Class-B bug"). `champion_spine` marks
the promoted spine (`promoted === true` only). Row ordering: board order;
each row flags `slice` (train/holdout via §2.4), its per-entry `flip_rate`
(§2.2), and the `calibration_generation` the flip rate rides on (N4 — so a
flip rate measured on an OLDER champion than the current spine tip reads as
stale in the badge).

### 3.2 `build_eval_dossier(paths, epoch_id, entry_id) -> dict`
The per-entry INSTRUMENT-QUALITY lens (one entry across all candidates).

```jsonc
{
  "epoch_id": "e3", "entry_id": "task_login",
  "found": true,                     // false + note on unknown entry
  "slice": "holdout", "tag": "holdout",
  "instrument": {
    "flip_rate": 0.2, "flip_rate_measured": true, "calibration_runs": 5,
    "calibration_generation": "g0",                      // N4 (staleness)
    "discrimination": 0.75, "discrimination_pairs": 4,   // §2.3 (matchup records)
    "runtime_ms_mean": 41200.0, "runtime_ms_p50": 40100.0,
    "runtime_ms_max": 61000.0, "replicate_total": 12,
    "cached_share": 0.08
  },
  "trajectory": [                    // per candidate, round order (the spine)
    { "generation_id": "g0", "round_index": 0, "champion_spine": true,
      "drift_loss": 0.31, "pass_ratio": 1.0, "replicates": 2,
      "cached": false }
  ],
  "attribution": { "first_passed_by": "g2",   // first spine gen to pass it
                   "regressed_by": ["g5"] },   // spine gens that flipped pass→fail
  "reflection_findings": []          // links via reflection_view (may be empty)
}
```

`discrimination` / `discrimination_pairs` are the §2.3 matchup-record
binding (comparisons = both-sides settled matchups; the rate over those);
`flip_rate_measured` is `true` iff a real rate was computed (an entry with
<2 usable draws is unmeasured, never a fabricated 0 — F4). `trajectory`
orders candidates like the matrix columns and reads each cell's replicate
files (§2.1); `attribution` walks the champion spine in round order:
`first_passed_by` = first spine gen whose cell passed; `regressed_by` =
spine gens that flipped a prior pass to a fail. All degrade to `null` / `[]`
on absent data.

### 3.3 Pure analytics helpers (unit-tested, no I/O)
In `eval_view.py`, pure and independently tested:
- `flip_rate(pass_fail_draws) -> float | None` — §2.2.
- `discrimination(pairs) -> tuple[float | None, int]` — the pure grouped-pair
  fold §2.3 drives: over `(matchup_key, pass_fail)` items it returns
  `(rate, n_groups_with_both)`. (The DATA source is the matchup records, not
  `loss_profiles`; the fold itself is unchanged and independently tested.)
- `runtime_aggregates(values) -> {mean, p50, max}` — over non-`None` ms.
- `pass_ratio(bits) -> float | None`, `evidence_of(n) -> str`.

### 3.4 `facet_scores_for_generation(paths, epoch_id, generation_id) -> dict`

The FACET slice: one candidate re-aggregated per `facet:` board tag
(BOARD-FORMAT.md §1.4). Feeds both facet surfaces — the candidate
dossier's table (one candidate × every facet) and the per-board page's
(one entry's facets × every candidate). They share one vocabulary in
`static/js/facets.js` and differ only in orientation.

Returns `{facets: {name: {scalar, mean_score, scored_count,
entry_count, ran_count}}, overall: {...} | None}`. `entry_count` sizes the
slice from the BOARD and `ran_count` is how many of those produced a
profile — the scalar's own denominator. Each block is
`tournament.scoring.aggregate_generation_score` run over just that
slice's loss profiles at the epoch's FROZEN weights, so a facet's
`scalar` is the same quantity, in the same units and direction, as the
`overall` row — that comparability is the reader's whole purpose. It
belongs in this module because a facet is this module's own transpose:
rows are board entries grouped by the operator's ontology, the column is
one candidate.

Reads the persisted `loss.json` files rather than the index (the files
are canonical, so a completed generation is readable with no index) and
the epoch's `scoring.json`. Per DQ3, an unreadable board, absent run
files, or a malformed `scoring.json` degrade to `{facets: {}, overall:
None}` / the default weights — never a raise. Per DQ2, a slice nothing
scored reports `mean_score: null`, never a fabricated `0.0`.

DIAGNOSTIC ONLY: nothing here feeds the scalar the gate reads, the gate,
scheduling, or Pareto admission.

## 4. Statistical-honesty rules (the views MUST obey)

1. **Shade by evidence, not by verdict.** A cell's `evidence`
   (`none`/`single`/`replicated`) sets its weight: a single-sample verdict
   renders **faint** (`dn-faint`), a replicated one **firm**. A pass on one
   draw is a claim, not a result.
2. **A failure renders beside its entry's flip rate.** A fail on a
   20%-flip-rate entry is noise context, not a defect — the row badge
   carries `flip_rate` next to every verdict so the reader never reads a
   single red cell as truth.
3. **The MDE ladder states its formula + n.** Use the CAMPAIGN.md §3
   two-sample form: `MDE = (t_{α/2,df} + t_{β,df})·sd·√(2/n)`, with
   `sd ≈ floor` (`noise_floor.max_abs_delta`) and `n` = the epoch's realised
   per-arm replicate count. At `n=6, df=10, α=.05` this is **≈ 1.79·floor**
   (**≈ 1.55·floor** at α=.10) — the numbers CAMPAIGN.md §3 pins. The panel
   prints the formula, the measured floor, and the `n` it used — never a
   bare "MDE = x".
4. **No fabricated numbers.** Absent calibration ⇒ `flip_rate_measured:
   false` and the view prints **"flip rate unmeasured"**, NEVER `0.0`.
   Absent floor ⇒ the MDE ladder says "floor unmeasured", not a made-up
   bound. An entry present in the flip map but with a `null` rate (<2 usable
   draws) is **unmeasured** — the reader tests `flip.get(eid) is not None`,
   not `eid in flip` (F4), so all three readers agree. `null` is honest; a
   zero is a lie.
5. **The MDE honesty caveat at low n (N3).** The two-sample form's `√(2/n)`
   makes the MDE much LARGER at small replicate counts — at `n=2` it is
   several times the `n=6` illustration this doc uses (`df=2`, the t-values
   balloon). The panel already prints honestly: it states the actual `n`,
   `df`, and the resulting bound (never the `n=6` number as if universal), so
   an operator running at `replicates=2` sees the genuinely wider MDE, not a
   flattering one. The illustration here is a fixed reference point, not the
   served value.

## 5. The four workstreams

### WS-READ (this wave — foundation)
`src/zicato/query/eval_view.py` (§3) + endpoints
`/api/epoch/{id}/evals` and `/api/epoch/{id}/eval/{entry_id}` in
`_make_epoch_endpoints` (`endpoints.py:204`), routed in `server.py:200`
following the `_is_safe_id` degrade idiom. All THREE eval readers do blocking
file I/O (the pooled matchup grids + per-cell replicate files), so each
handler wraps its reader in `run_in_threadpool` (the `build_log_view`
precedent, `endpoints.py:176`, F5) — the reads never stall the event loop.
The malformed-id degrade returns the reader's own `_empty_matrix` /
`_empty_dossier` / `_empty_health` shape (single-sourced, N1) so the endpoint
and reader can never drift. Exported from `zicato.query.__init__`.

### WS-MATRIX (new top-level **Evals** view)
A new shell view (a new hash route + tree node) rendering `build_eval_matrix`.
- **Grid vocabulary `dn-mtx`** — a NEW `dn-`-namespaced grid class (the
  namespace is established: `dn-faint`/`dn-good`/`dn-bad`/`dn-stat`/
  `dn-board-table` all exist; `dn-mtx` joins it). No new *chip* vocabulary
  (§6).
- **Own-container scroll** — the wide matrix scrolls inside its own
  `dn-table-scroll` container (that class exists); the page body never
  scrolls horizontally.
- **Evidence shading** (§4.1) — cell opacity/weight from `cell.evidence`.
- **Filters** — failures-only, **flips-only**, holdout-only. **flips-only =
  a CROSS-COLUMN VERDICT CHANGE** (F6): a row is kept when some cell's
  verdict differs from the previous **non-null** column — "what did this
  candidate MOVE". This is the operator-intended signal in `rowHasFlip`
  (`views/evals.js`); it is **NOT** the entry-noise `flip_rate > 0` signal
  (that lives on the per-row flip badge instead — a channel can be noisy
  without any candidate changing its verdict, and a clean 0%-flip channel can
  still record a genuine fail→pass move). The two are deliberately distinct
  lenses.
- **Flip-rate row badges** (§4.2) — each entry row shows `flip_rate` (or
  "unmeasured") beside the entry id, with the `calibration_generation` in the
  badge tooltip (N4) so a stale (older-champion) flip rate is visible.
- **Decision column headers** — champion-spine columns marked (crown glyph
  from `svg.js` `CROWN`); `round_index` grouped. The decision pill is
  **tri-state** (F1): `promoted` → the shipped `promoted` pill, `rejected` →
  `rejected`, `null` (in-flight / never raced) → the shipped `pending`
  ("racing…") vocabulary — a null is NEVER collapsed to rejected, and the
  view digest folds a 3-state token so a `false`→`null` change repaints.
- **Cell click-through** — into the run transcript and a harmonograf
  deep-link via the existing helpers (the `harmonograf_url` on
  `WorkspacePaths` + the run-header / transcript endpoints the dossier
  already uses); `latest_run_id` is the anchor.

### WS-DOSSIER (the entry page upgrade)
Upgrade `src/zicato/dashboard/static/js/views/board.js` to render
`build_eval_dossier`:
- **Trajectory sparkline vs the champion spine** using the shipped
  `svg.js` `sparkline` grammar (`:408`).
- **ATTRIBUTION** — `first_passed_by` / `regressed_by` rendered as the
  quiet verdict-led rows the Instrument lens uses (a tone glyph + headline +
  `dn-faint` rationale — NOT a chip per row).
- **Instrument stats** — flip rate, discrimination, runtime aggregates in
  the `dn-stat` idiom (NOT labelled tags).
- **Reflection-finding links** — inline x-ray links into
  `reflection_view`, not recomputed.

### WS-HEALTH (the instrument panel)
Recommend-only; every finding links into `reflect` / `builder`.
- The **measured floor** + the **live MDE ladder** (§4.3).
- **Ranked noisy evals** — entries by descending `flip_rate`.
- **DEAD evals** — zero discrimination across the reign's settled matchups
  (§2.3): an entry that never separated any two candidates, read from the
  matchup records. A zero-discrimination channel is only DEAD **above** the
  minimum-comparisons honesty threshold (`_MIN_DISCRIMINATION_COMPARISONS =
  3`); below it the panel says "insufficient comparisons", never dead. The
  dossier and this panel share the one `_discrimination_by_entry` map, so
  they never disagree.
- **Redundancy clusters** — from the reflection corpus analysis
  (`reflection_view` `redundant_with`) *if cheaply reachable* (the
  reflection is already built); else **deferred, with an explicit note** —
  do not run a reflection to fill this panel.
- **Holdout budget spent** + **rotation cadence** — from the split (§2.4)
  and the Ladder summary (`_latest_holdout_summary`, `epoch_view.py:625`).

**Where WS-HEALTH lives (adjudicated).** It lives as a **strip + section
inside the new Evals view**, NOT a fourth top-level surface. The existing
board-status view (`boardstatus.js` + `compute_board_split`) owns the
*lifecycle* framing (train/holdout split, where/when each slice is played,
generalization gap). WS-HEALTH owns the *instrument-quality* framing (noise,
discrimination, MDE, dead evals). They **coexist**: board-status stays the
lifecycle home; WS-HEALTH links to it for the split detail rather than
re-rendering the strip. The one overlap — holdout membership — is served by
the SAME canonical `split_board` binding (§2.4) so the two surfaces never
disagree.

## 6. Render discipline + register

- **Quiet precision.** No new chip vocabulary; reuse `dn-faint` / `dn-good`
  / `dn-bad` / `dn-stat`. The one new class is the `dn-mtx` grid layout.
- **Digest-gated.** A no-op SSE heartbeat produces zero DOM (the recurring
  flashing-render bug class; `digestOpts`, `svg.js:89`). The matrix rebuilds
  only when its digest changes.
- **Own-container scroll.** The wide matrix scrolls in its own
  `dn-table-scroll`; the body never scrolls horizontally.
- **Bundle-envelope raise expected.** A new view grows the JS bundle; the
  house rationale (a read-only surface that pays for itself in operator
  time) covers the envelope bump — record it, don't fight it.
- **Read-side only.** No contract or parity exposure; nothing in
  `tools/parity.sh` moves.

## 7. Execution plan

1. **This wave (WS-READ):** this doc + `eval_view.py` (two readers + pure
   helpers) + endpoints + tests. Gates green (below).
2. **Three parallel view agents:** WS-MATRIX, WS-DOSSIER, WS-HEALTH build
   from §3/§5 — nothing else needed.
3. **Adversarial review:** point it at the **evidence-shading math** (§4.1 —
   does `single` ever render as firm?) and the **discrimination index**
   (§2.3 — are always-pass entries correctly dead, are cached rows excluded
   from discrimination pairs?).
4. **Fixes → integration ladder → PR.**

**Deferred + recorded (do NOT build this wave):**
- **Cross-epoch eval lifetime** — an entry's noise/discrimination *across*
  epochs. Wants the niche-archive design (a persisted per-entry lifetime
  record); the current bindings are all epoch-local. Deferred.
- **Epoch-to-epoch matrix diff** — "what changed in the instrument between
  e2 and e3". Deferred; needs the cross-epoch lifetime record above.
