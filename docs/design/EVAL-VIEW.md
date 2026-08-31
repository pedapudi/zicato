# The eval-centric view — the board as the measurement instrument

> **Status: implemented.** The readers in `src/zicato/query/eval_view.py`,
> the epoch eval endpoints, the Evals matrix view with its instrument-quality
> panel, and the per-entry dossier on the board view are all built. Two
> extensions are designed and not built; §7 names them. Everything here reads
> existing data: no contract surface changes, and no parity artifact moves.

## 1. Thesis and the three lenses

Zicato's UI is **candidate-centric**: every surface answers how a given
generation did — the lineage DAG, the candidate dossier, the per-matchup
grid, the trajectory. That is the optimizer's view. It leaves out the
question a measurement scientist asks first, which is whether the instrument
is any good. The board is the measurement apparatus, and each entry is a
channel with its own noise, discrimination, redundancy, and cost. The
surfaces here transpose the matrix: rows are **entries** (the instrument),
columns are **candidates** (what the instrument measured).

Three lenses, each a view (§5):

1. **OUTCOMES** — the entries × candidates matrix. One cell = how candidate
   *c* scored on entry *e*. The transpose of the candidate dossier's
   board-breakdown. (The Evals matrix view.)
2. **INSTRUMENT QUALITY** — every eval as a measurement channel with four
   properties. Its noise is the flip rate when the champion is duelled
   against itself. Its discrimination is whether it ever separates two
   candidates, its redundancy whether another eval says the same thing, and
   its cost the runtime. This
   takes the noise-floor and minimum-detectable-effect doctrine down to the
   *per-entry* grain. (The per-entry dossier covers one entry; the
   instrument-quality panel covers the epoch.)
3. **LIFECYCLE** — rotation, holdout membership, retirement: which slice an
   entry lives in, when it is played, whether it still earns its keep. (The
   instrument-quality panel, plus the row flagging that threads through all
   three lenses.)

### Relationship to what exists (named files)

These surfaces reuse the readers below and add new ones in a single module;
they invent no new persistence.

- **Candidate dossier board-breakdown table** —
  `zicato.query.judge_view.build_per_entry_for_generation`
  (`src/zicato/query/judge_view.py:180`), rendered by the board dossier in
  `src/zicato/dashboard/static/js/views/board.js`. That is ONE column of our
  matrix (a single candidate's entries). The per-entry dossier upgrades that
  page.
- **Per-matchup entry grids** — `build_matchup_grid` /
  `/api/matchup-grid` (`src/zicato/dashboard/endpoints.py:776`). A single
  matchup's two-candidate slice of the matrix.
- **The passive entry×candidate matrix** —
  `zicato.query.reflection_view.entry_candidate_matrix`
  (`src/zicato/query/reflection_view.py:508`) already folds mean `drift_loss`
  per (entry, candidate) straight off the index. `build_eval_matrix` computes
  the same fold and adds evidence, pass-ratio, holdout flagging, and
  calibration (§3). `entry_candidate_matrix` stays in place — the Instrument
  lens' passive tier imports it — and the richer reader sits beside it.
- **Board-status / rotation surface** —
  `zicato.query.epoch_view.compute_board_split`
  (`src/zicato/query/epoch_view.py:527`) + the board-status view
  `src/zicato/dashboard/static/js/views/boardstatus.js`. The
  instrument-quality panel adjudicates against this surface (§5).
- **Reflection scorecards** — `zicato.query.reflection_view`
  (summary / scorecards / x-ray) rendered by the Instrument lens
  `src/zicato/dashboard/static/js/views/instrument.js`. The dossier and the
  instrument-quality panel link into these (findings, redundancy) rather than
  recomputing them.
- **The noise floor and the smallest effect a run can detect** (the minimum
  detectable effect) — `docs/design/CAMPAIGN.md` §3 (the two-sample formula),
  `docs/design/OVERFITTING.md` (holdout), and the runtime calibration that
  duels the champion against itself, `zicato.tournament.calibration`
  (`src/zicato/tournament/calibration.py`). §4 lifts the §3 formula to a live
  ladder.

## 2. Data bindings (every binding tree-verified)

The **backbone is the `loss_profiles` index table** — one row per run.
Verified columns (`src/zicato/index/schema.py:132`):

```
run_id · epoch_id · generation_id · entry_id · drift_loss · pass_fail
runtime_ms · wall_clock_budget_exceeded · loss_json · tournament_id
match_id · cached · source_epoch · source_run · abort_cause
```

**The primary key.** `run_id` is the **primary key** (`schema.py:133`) and
the reducer's default is `run_id = "{generation_id}:{entry.id}"`
(`reducer.py:1190`); ingest upserts `ON CONFLICT(run_id) DO UPDATE` with
`match_id = COALESCE(...)` (`ingest.py:346`). So there is **one
`loss_profiles` row per `(generation_id, entry_id)`**, and its
`match_id`/`tournament_id` are last-wins at ingest. Two consequences follow,
and they drive the bindings below:

* **A row count is not a replicate count** — it is always 1. Cell EVIDENCE
  must come from the on-disk replicate files (§2.1) rather than `len(rows)`.
* **"Same-match_id row pairs" cannot exist** — the table can never hold two
  rows for one `(gen, entry)`, so discrimination CANNOT be derived from
  `loss_profiles` (§2.3). It binds to the durable matchup records instead.

The matrix is an **indexed query** over this table for the axes and cell
membership; it adds no store. Read helpers already exist
(`src/zicato/index/query.py`): `loss_profiles_for_generation` (`:228`),
`loss_profiles_for_tournament` (`:281`), `generations_for_epoch` (`:184`,
carries `round_index` / `elo` / `elo_se`), `tournaments_for_epoch` (`:909`),
`elo_for_epoch` (`:940`). All are missing-index-tolerant (return `[]`). The
per-cell EVIDENCE and DISCRIMINATION quantities read the durable **files**
(`generations/<gen>/runs/<entry>/loss*.json`) — index-free by design.

Each derived quantity, with the binding it reads:

### 2.1 The matrix cell (entry × candidate)
The `loss_profiles` row for `(entry_id, generation_id)` supplies cell
MEMBERSHIP, meaning which candidate columns an entry appears in, plus
`cached` / `latest_run_id`. The row's `drift_loss` / `pass_fail` and its
continuous `score` / `metrics` are fallback values; they are parsed from the
`loss_json` blob the same way `build_per_entry_for_generation` parses it
(`judge_view.py:255`). But **the replicate count and the evidence come from the
durable replicate FILES rather than the row count**, which is always 1 — see
the primary key above.

**The evidence binding (`_cell_replicate_draws`, `eval_view.py`).** For each
cell the reader takes the `loss.json` (replicate 0) and the `loss.r<N>.json`
siblings that exist under `generations/<gen>/runs/<entry>/`. It keeps only the
replicate ranges that count as fresh evidence for that cell: the **duel
replicates `[0, 1000)`** (replicate 0 canonical, plus the low duel slots the
holdout-ladder confirmation re-runs reuse) and the **evidence-gate draws
`[4000, 5000)`** (`EVIDENCE_REPLICATE_BASE`). Four ranges are EXCLUDED, because each
is a different measurement rather than this cell's evidence. The
champion-against-itself **calibration `[1000, 2000)`** is the champion
noise-floor trace and feeds the flip badge (§2.2). The contract
**pre-flight `[2000, 3000)`** and the pre-tournament candidate **screen
`[3000, 4000)`** are veto probes. **Reflection `[5000, …)`** is a
meta-evaluation of the judges. `pass_ratio` / `pass_fail`
/ `drift_loss` / `score` are averaged over those same qualifying draws; the
cell falls back to the single index row only when the `runs/` dir was pruned.
`cached` / `source_epoch` / `source_run` mark a carried-over champion result
(a materialised fast-mode reuse — schema v6, `schema.py:320`), so the view
renders it as scored-but-cached and never double-counts it.

### 2.2 Per-entry flip rate (calibration) — THE TRACE
The calibration that duels the champion against itself (`measure_noise_floor`,
`src/zicato/tournament/calibration.py:129`) duels the champion against
itself `runs=K` times (default 5, `DEFAULT_CALIBRATION_RUNS`). Each draw
evaluates the **full board** through `_run_board_units_fast` on replicate
index `CALIBRATION_REPLICATE_BASE + draw` (base **1000**) with
`match_id="aa-calibration:{draw}"`. The runner persists each board unit's
result per replicate: `_unit_loss_path`
(`src/zicato/tournament/unit_cache.py:106`) maps replicate `r>0` to
`epochs/<epoch>/generations/<gen>/runs/<entry>/loss.r<r>.json` (replicate 0
is the canonical `loss.json`).

**A constraint worth stating outright:** these replicate files are **NOT
ingested** into `loss_profiles`. `_ingest_run_into`
(`src/zicato/index/ingest.py:933`) reads a single `loss_profile_path`
(replicate 0) per entry, and
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
   (non-`None`) draws — the fraction of self-duel draws whose verdict flipped
   away from the majority. `None` when fewer than two usable draws exist.

Absent `noise_floor`, or missing replicate files → **flip rate unmeasured**
(never 0; §4).

### 2.3 Discrimination (the reign's settled matchups)
An entry discriminates a matchup when the two competitors' verdicts differ
on it. **This CANNOT bind to `loss_profiles` same-`match_id` pairs** — the
primary key forbids two rows per `(gen, entry)`, so those pairs never exist.
Bind instead to the **durable matchup records** — the same source the recombination builder trusts
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
§5, the instrument-quality panel). The grid reads are pooled one per matchup
(the reign is bounded); the instrument-quality panel and the dossier share
this one map so they always agree, and the reader runs in the endpoint
threadpool, off the event loop (§5). A gauntlet
where the champion faces three challengers yields
`discrimination_pairs = 3`.

### 2.4 Holdout membership (the split)
There is **no persisted split record** — membership is *computed*, and it
must match the gate's own computation. Bind to `zicato.board.split.split_board`
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
`split_board` so the flagged holdout is byte-exact with the gate rather than
with the board-status approximation.

### 2.5 The noise floor and the inputs to the detectable-effect ladder
The scalar floor is `noise_floor.max_abs_delta` on `config.json`
(`margin_below_floor`, `calibration.py:233`). The minimum-detectable-effect
ladder (§4) is computed from that floor and the epoch's realised replicate
count, and adds no persistence.

### 2.6 Reflection findings
Redundancy clusters and judge findings come from the reflection readers
(`reflection_view.build_reflection_summary` / `build_judge_scorecards`,
whose `redundant_with` is the corpus redundancy). The dossier and the
instrument-quality panel **link** into them; they do not recompute reflection
analysis.

## 3. The reader contracts

Two readers land in `src/zicato/query/eval_view.py`, plus
`facet_scores_for_generation` (§3.4). They obey the same house invariants as
the sibling readers. The server derives every quantity and the view renders
it without computing domain math. Payload fields are snake_case, with one
spelling and one encoding each. A cold index, an unknown entry, or absent
calibration degrades to a *same-shape* payload with `found: False`, never a
raise and never a bare int on the wire (`_opt_bool` / `coerce_float` from
`zicato.query.paths`).

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
  applies only when two or more qualifying replicate files exist on disk.

Column ordering: `(round_index ?? +inf, created_at, generation_id)`.
`promoted` is **tri-state**: the canonical
`promoted_tristate(experiment_decision(experiment.json))` — `true` (won the
gate), `false` (lost), `null` (in-flight or never raced). The index
`promoted` bool is the fallback ONLY when the experiment record is
unreadable. A readable-but-undecided experiment stays `null`; an undecided
promotion must never collapse into a `false`. `champion_spine` marks the
promoted spine (`promoted === true` only). Row ordering: board order; each
row flags `slice` (train/holdout via §2.4), its per-entry `flip_rate` (§2.2),
and the `calibration_generation` the flip rate rides on, so a flip rate
measured against an older champion than the current spine tip reads as stale
in the badge.

### 3.2 `build_eval_dossier(paths, epoch_id, entry_id) -> dict`
The per-entry INSTRUMENT-QUALITY lens (one entry across all candidates).

```jsonc
{
  "epoch_id": "e3", "entry_id": "task_login",
  "found": true,                     // false + note on unknown entry
  "slice": "holdout", "tag": "holdout",
  "instrument": {
    "flip_rate": 0.2, "flip_rate_measured": true, "calibration_runs": 5,
    "calibration_generation": "g0",                      // staleness (§2.2)
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
`flip_rate_measured` is `true` only when a real rate was computed; an entry
with fewer than two usable draws is unmeasured, never a fabricated 0.
`trajectory`
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

**The train slice only.** The scalar the gate compares —
and the one `gen_score.json` caches — is `governance._train_aggs`, so
holdout entries are excluded from every block here and `overall` IS the
candidate's headline number. Aggregating the whole board would put a
second, larger "candidate scalar" beside the gate's, identically
labelled; it would also make every dossier load an ungoverned holdout
query (OVERFITTING.md §4). The split is read through the module's own
`_holdout_ids`, so this reader and the per-entry `slice` badge can never
disagree about which entries are held out. A facet whose train slice is
empty because nothing RAN keeps its row with null numbers; one whose
entries are all HELD OUT reports no row.

Not threaded: the opt-in `diff_complexity` term, which the gate folds
into the challenger's aggregate from its diff size. At the default weight
of `0.0` nothing differs; under a non-zero weight a facet scalar omits a
per-candidate constant the headline scalar carries, because a diff is not
attributable to a board tag.

Reads the persisted `loss.json` files rather than the index (the files
are canonical, so a completed generation is readable with no index) and
the epoch's `scoring.json`. Because every reader is best-effort, an
unreadable board, absent run files, or a malformed `scoring.json` degrade to
`{facets: {}, overall: None}` and the default weights — never a raise.
Because no wire field carries a default stand-in, a slice nothing scored
reports `mean_score: null`, never a fabricated `0.0`.

DIAGNOSTIC ONLY: nothing here feeds the scalar the gate reads, the gate,
scheduling, or Pareto admission.

## 4. Statistical-honesty rules (the views MUST obey)

1. **Shade by evidence rather than by verdict.** A cell's `evidence`
   (`none`/`single`/`replicated`) sets its weight: a single-sample verdict
   renders **faint** (`dn-faint`), a replicated one **firm**. A pass on one
   draw is a claim rather than a result.
2. **A failure renders beside its entry's flip rate.** A fail on an entry
   whose flip rate is 20% is noise context rather than a defect. The row badge
   carries `flip_rate` next to every verdict, so the reader never takes a
   single red cell as truth.
3. **The minimum-detectable-effect ladder states its formula and its n.**
   Use the CAMPAIGN.md §3 two-sample form:
   `MDE = (t_{α/2,df} + t_{β,df})·sd·√(2/n)`, with `sd ≈ floor`
   (`noise_floor.max_abs_delta`) and `n` = the epoch's realised per-arm
   replicate count. At `n=6, df=10, α=.05` this is **≈ 1.79·floor**
   (**≈ 1.55·floor** at α=.10) — the numbers CAMPAIGN.md §3 pins. The panel
   prints the formula, the measured floor, and the `n` it used, never a bare
   minimum-detectable-effect figure on its own.
4. **No fabricated numbers.** Absent calibration ⇒ `flip_rate_measured:
   false` and the view prints **"flip rate unmeasured"**, NEVER `0.0`.
   Absent floor ⇒ the ladder says "floor unmeasured" rather than printing a
   made-up bound. An entry present in the flip map but with a `null` rate
   (fewer than two usable draws) is **unmeasured** — the reader tests
   `flip.get(eid) is not None` rather than `eid in flip`, so all three
   readers agree. A `null` reports the absence of a measurement; a `0.0`
   would report a measured result that does not exist.
5. **The low-n caveat on the minimum detectable effect.** The two-sample
   form's `√(2/n)` makes the bound much larger at small replicate counts: at
   `n=2` it is several times the `n=6` illustration used above, because
   `df=2` inflates the t-values. The panel states the actual `n`, the actual
   `df`, and the resulting bound, and never prints the `n=6` number as if it
   were universal, so an operator running at `replicates=2` sees the wider
   bound their own sample size implies. The illustration above is a fixed
   reference point; the panel serves the measured value.

## 5. The reader layer and the three views

### The readers and their endpoints
`src/zicato/query/eval_view.py` (§3) + endpoints
`/api/epoch/{id}/evals` and `/api/epoch/{id}/eval/{entry_id}` in
`_make_epoch_endpoints` (`endpoints.py:204`), routed in `server.py:200`
following the `_is_safe_id` degrade idiom. All three eval readers do blocking
file I/O (the pooled matchup grids and the per-cell replicate files), so each
handler wraps its reader in `run_in_threadpool` (the `build_log_view`
precedent, `endpoints.py:176`) and runs it off the event loop, where the
reads never stall it. The malformed-id degrade returns the reader's own
`_empty_matrix` / `_empty_dossier` / `_empty_health` shape; those constants
are single-sourced from the reader, so the endpoint and the reader cannot
drift apart. Exported from `zicato.query.__init__`.

### The Evals matrix view
A shell view (its own hash route and tree node) rendering `build_eval_matrix`.
- **Grid vocabulary `dn-mtx`** — a NEW `dn-`-namespaced grid class (the
  namespace is established: `dn-faint`/`dn-good`/`dn-bad`/`dn-stat`/
  `dn-board-table` all exist; `dn-mtx` joins it). No new *chip* vocabulary
  (§6).
- **Own-container scroll** — the wide matrix scrolls inside its own
  `dn-table-scroll` container (that class exists); the page body never
  scrolls horizontally.
- **Evidence shading** (§4.1) — cell opacity/weight from `cell.evidence`.
- **Filters** — failures-only, **flips-only**, holdout-only. **Flips-only
  means a cross-column verdict change**: a row is kept only when some cell's
  verdict differs from the previous **non-null** column, which shows what a
  candidate moved. That is the signal `rowHasFlip` (`views/evals.js`)
  computes. It is a different signal from the entry-noise `flip_rate > 0`,
  which lives on the per-row flip badge: a channel can be noisy without any
  candidate changing its verdict, and a channel with a 0% flip rate can still
  record a real fail→pass move. The two are distinct lenses.
- **Flip-rate row badges** (§4.2) — each entry row shows `flip_rate` (or
  "unmeasured") beside the entry id, with the `calibration_generation` in the
  badge tooltip, so a flip rate calibrated against an older champion than the
  current spine tip is visible as stale.
- **Decision column headers** — champion-spine columns marked (crown glyph
  from `svg.js` `CROWN`); `round_index` grouped. The decision pill is
  **tri-state**: `promoted` → the `promoted` pill, `rejected` → `rejected`,
  `null` (in-flight or never raced) → the `pending` ("racing…") vocabulary. A
  null is NEVER collapsed to rejected, and the view digest folds a three-state
  token so a `false`→`null` change repaints.
- **Cell click-through** — into the run transcript and a harmonograf
  deep-link via the existing helpers (the `harmonograf_url` on
  `WorkspacePaths` + the run-header / transcript endpoints the dossier
  already uses); `latest_run_id` is the anchor.

### The per-entry dossier
Upgrade `src/zicato/dashboard/static/js/views/board.js` to render
`build_eval_dossier`:
- **Trajectory sparkline vs the champion spine** using the shipped
  `svg.js` `sparkline` grammar (`:408`).
- **ATTRIBUTION** — `first_passed_by` / `regressed_by` rendered as the
  quiet verdict-led rows the Instrument lens uses (a tone glyph + headline +
  `dn-faint` rationale, and no chip per row).
- **Instrument stats** — flip rate, discrimination, runtime aggregates in
  the `dn-stat` idiom rather than labelled tags.
- **Reflection-finding links** — inline x-ray links into `reflection_view`
  rather than recomputed analysis.

### The instrument-quality panel
Recommend-only; every finding links into `reflect` / `builder`.
- The **measured floor** and the **live minimum-detectable-effect ladder**
  (§4.3).
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

**Where the instrument-quality panel lives.** It is a strip and a section
inside the Evals view rather than a fourth top-level surface. The board-status
view (`boardstatus.js` + `compute_board_split`) owns the *lifecycle* framing:
the train/holdout split, where and when each slice is played, and the
generalization gap. The instrument-quality panel owns the
*instrument-quality* framing: noise, discrimination, the minimum detectable
effect, and dead evals. They coexist. Board-status stays the lifecycle home,
and the panel links to it for the split detail rather than re-rendering the
strip. The one overlap, holdout membership, is served by the same canonical
`split_board` binding (§2.4), so the two surfaces never disagree.

## 6. Render discipline + register

- **Quiet precision.** No new chip vocabulary; reuse `dn-faint` / `dn-good`
  / `dn-bad` / `dn-stat`. The one new class is the `dn-mtx` grid layout.
- **Digest-gated.** A no-op server-sent-events heartbeat produces zero DOM (the recurring
  flashing-render bug class; `digestOpts`, `svg.js:89`). The matrix rebuilds
  only when its digest changes.
- **Own-container scroll.** The wide matrix scrolls in its own
  `dn-table-scroll`; the body never scrolls horizontally.
- **The bundle envelope rises.** A new view grows the JS bundle. A read-only
  surface that pays for itself in operator time justifies the raise, so record
  the new envelope.
- **Read-side only.** No contract or parity exposure; nothing in
  `tools/parity.sh` moves.

## 7. Review focus and unbuilt work

Two areas repay adversarial review. The first is the evidence-shading
arithmetic (§4.1): whether a `single` cell can ever render as firm. The second
is the discrimination index (§2.3): whether an always-pass entry is correctly
reported dead, and whether cached rows stay out of the discrimination pairs.

**Designed and not built:**
- **Cross-epoch eval lifetime** — an entry's noise and discrimination *across*
  epochs. This needs a persisted per-entry lifetime record; every binding
  described above is epoch-local.
- **Epoch-to-epoch matrix diff** — what changed in the instrument between one
  epoch and the next. This needs the cross-epoch lifetime record above.
