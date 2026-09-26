# Tournament data model — configurable per-epoch structures

> **Status.** This document is the as-built reference for the storage
> and interface half of configurable per-epoch tournament structures.
> The runtime record (`runtime/state.py::ActiveTournament`), the
> selection records (`selection/strategy.py`), and the dashboard
> projection (`evolve/dashboard_projection.py`) cite its section numbers
> from their source comments, so the numbering is stable. The pairing,
> elimination and racing-cut algorithms that drive each structure, and the
> decision theory under them, are specified in [`SELECTION.md`](SELECTION.md),
> [`TOURNAMENT.md`](TOURNAMENT.md), and
> [`TOURNAMENT-STRUCTURES.md`](TOURNAMENT-STRUCTURES.md). The
> `tournament` config block of §1 is the shared contract between the two
> halves: its field name (`tournament`) and its shape
> (`{structure, params}`) are the same in both specifications.

The tournament structure is a per-epoch configurable choice —
`racing` (the default), `gauntlet`, and, under the
`experimental.tournament_structures` opt-in, `single_elim`,
`double_elim` and `swiss`. The gauntlet is a king-of-the-hill contest:
one reigning champion, one challenger per round, and the promote gate
(see [`TOURNAMENT.md`](TOURNAMENT.md) §1 and [`SELECTION.md`](SELECTION.md)
§3). Its record shape — two sides per board entry, `parent` and `child`
— is the shape every other structure generalizes.

This document specifies the schema, the persistence, the API surface,
and the console rendering.

---

## 1. The epoch-contract `tournament` config block

### 1.1 Shape

The structure is part of the **evaluation contract** — generations
selected under a gauntlet are not directly comparable to generations
selected under a Swiss tournament, so a structure change must roll the
epoch (§4). It is configured as a single block:

```jsonc
{
  "structure": "racing",     // gauntlet|racing; single_elim|double_elim|swiss under the opt-in
  "params": { /* structure-specific, see §1.3 */ }
}
```

- `structure` — a closed enum string. The five values are
  `"gauntlet"`, `"single_elim"`, `"double_elim"`, `"swiss"`,
  `"racing"` (`VALID_TOURNAMENT_STRUCTURES` in `core/tournament.py`).
  `TournamentStructure.__post_init__` rejects an unknown value with a
  message listing the valid tokens.
- `params` — a structure-specific JSON object. Absent ⇒ `{}` ⇒ every
  param takes its strategy default.

### 1.2 Where it lives on disk

The block lives in **`scoring.json`** under a top-level `tournament`
key rather than in a file of its own, for two reasons.

- `scoring.json` is already a frozen contract component (see
  [`EPOCHS-AND-JOURNALING.md`](EPOCHS-AND-JOURNALING.md) §10.1). The
  selection *gate thresholds* (`promote_margin`,
  `pass_rate_monotonicity`, the `namespace_monotonicity` flags) already
  live there. The tournament *structure* is the same kind of
  thing — "how the crowning decision is made" — so it belongs in the
  same document, and it factors into the contract hash with no
  separate plumbing in `resolve_contract_inputs`.
- A separate `tournament.json` would need its own contract component,
  canonicalizer, registration flag, and frozen-copy path.

The `tournament` block is **modeled in `ScoringWeights`** as the
`tournament_structure` field (persisted under the key `tournament`), a
frozen `TournamentStructure` dataclass built by the
`_default_tournament_structure` factory — the same pattern as
`namespace_weights` / `namespace_monotonicity`. The frozen copy under
`epochs/{id}/scoring.json` carries it; the live operator-side
`scoring.json` carries it; both canonicalize identically (§4).

### 1.3 Per-structure `params`

Each structure interprets `params` differently. The **key names** here
are the shared contract with the selection-logic design; the **semantics**
(how a value drives pairing and cuts) are specified in
[`TOURNAMENT-STRUCTURES.md`](TOURNAMENT-STRUCTURES.md). Each strategy
class declares the keys it accepts (`parameter_names`) and refuses any
other key with an `unsupported tournament parameters` error.

| `structure` | `params` keys (with strategy defaults) | Notes |
|---|---|---|
| every structure | `replicates` (structure default), `promote_confidence_threshold` (unset), `promote_confidence_replicates` (32 when a threshold is set) | `replicates` is the per-duel replicate count: `2` by default, `1` for racing, and derived from the measured noise floor when unset (`selection/replicates.py`). A threshold in `(0, 1)` enables the evidence gate; `promote_confidence_replicates` is its confirmation-duel budget. |
| `gauntlet` | no further keys | One challenger, one full-board duel. |
| `racing` | `field_size` (2), `eta` (2), `board_fraction` (0.25), `rung0_board_size` (0 ⇒ use the fraction), `board_ids` (the epoch board), `slice_schedule` (`"prefix"` or `"shuffled_v1"`), `matchup_budget_seconds`, `final_rung_budget_seconds` | Successive halving. Each rung duels every surviving challenger against the champion on a board slice, keeps the best `1/eta`, and grows the slice by `eta`. The loop injects `noise_floor_delta_std` when the epoch has a measured floor. |
| `single_elim` | `field_size` (2) | Bracket over the round's candidate field; the champion meets the bracket survivor in the final. |
| `double_elim` | `field_size` (2) | Adds a losers' bracket; the grand final pits the two brackets' survivors. |
| `swiss` | `field_size` (2), `rounds_n` (4) | Fixed number of rounds; each round pairs candidates of similar standing. |

The recommended contract that `zicato init` scaffolds, and a
`scoring.json` with no `tournament` key, use `racing` with
`field_size` 4, `eta` 2, `board_fraction` 0.4, `replicates` 2,
`promote_confidence_threshold` 0.8, and `promote_confidence_replicates`
32. The three experimental structures also receive `rating` and
`resolver` from `experimental.standing_rating` and
`experimental.resolver` (see [`SELECTION-THEORY.md`](SELECTION-THEORY.md));
writing either key in `tournament.params` directly is refused.

The data model **stores and round-trips** `params` verbatim as a JSON
object (`Mapping[str, Any]`); it does NOT type each structure's params
into its own dataclass. Keeping `params` an opaque mapping means the
selection layer can add a param without a data-model change.

### 1.4 Validation and defaulting

- **Default.** A `scoring.json` with no `tournament` key resolves to the
  recommended racing specification of §1.3.
- **Structure validation.** `structure` must be one of the five tokens.
  An experimental token is refused at contract load unless
  `experimental.tournament_structures` is `true`; the refusal names that
  key.
- **Params validation.** The data-model layer validates that `params`
  is a JSON object and that `replicates`, `promote_confidence_threshold`
  and `promote_confidence_replicates` lie in their declared ranges
  (`TOURNAMENT_PARAM_CONSTRAINTS`). Every other key is validated by the
  **selection layer**, which owns the algorithm that reads it: strategy
  construction refuses an unsupported key and range-checks the rest.

---

## 2. The generalized persisted tournament record

### 2.1 Execution records progress and completed results

The live runtime record, `ActiveTournament`, publishes competitors, matches,
standings, and board progress through `runtime/active_tournament.events.jsonl`.
The runner supplies measurements and the tournament strategy supplies pairing
and elimination state.

A completed round publishes its results in
`epochs/<epoch>/rounds/<round>/field_settlement.json`. The round record contains
candidate outcomes, the primary promoted generation
(`primary_promoted_generation_id`), the complete tournament structure under
`field_tournament_record`, and explanations of gate results under
`gate_results`. A two-candidate tournament records its actual match and
standings through the same path as a larger field. While a round runs, the
same structure record is also written to
`epochs/<epoch>/tournaments/field-<first_challenger>.json` with
`state: "in_progress"`; once the round is committed, readers take the settled
copy from the round record.

The SQLite `tournaments` table is a derived projection. Experiment readers
combine the proposal with its committed round outcome. Dashboard readers use
those shared readers to present decisions, comparisons, and visualizations.

### 2.2 The live `ActiveTournament`

The structure fields of `ActiveTournament` beside the two-side fields
(every structure field decodes to a default when a payload omits it):

```jsonc
{
  "tournament_id": "tourn_e3_v4",
  "epoch_id": "2026-06-01_e3",
  "parent_generation_id": "v3",   // gauntlet: the champion
  "child_generation_id": "v4",    // gauntlet: the lone challenger
  "started_at": "...",
  "phase": "running",             // running|completed|aborted
  "round_index": 0, "total_rounds": 1,
  "entries": [ /* per-(entry × side) rows, see §2.3 */ ],
  "partial_champion_agg": { ... }, "partial_challenger_agg": { ... },

  // ── the structure envelope ──
  "structure": "swiss",           // the epoch's tournament.structure; "gauntlet" when absent
  "structure_params": { "rounds_n": 4 },
  "competitors": [                 // the candidate field this tournament ranks
    { "generation_id": "v3", "seed": 1, "role": "champion" },
    { "generation_id": "v4", "seed": 2, "role": "challenger" },
    { "generation_id": "v5", "seed": 3, "role": "challenger" }
  ],
  "rounds": [ /* per-structure round/rung/bracket state, see §2.4 */ ],
  "gen_states": [ /* elimination structures only, see §2.4 */ ],
  "standings": [ /* current ranking, see §2.5 */ ],
  "field_status": [                // every challenger the proposer attempted
    { "generation_id": "v4", "status": "applied", "reason": "", "seed": 2 }
  ],

  // ── live projected standing per in-flight competitor, see §2.5.1 ──
  "projected": {
    "v4": { "scalar": 0.42, "boards_done": 3, "boards_total": 8, "pass_rate": 0.9 }
  }
}
```

- `structure` / `structure_params` — copied from the resolved epoch
  contract at tournament start so a reader never has to re-resolve
  `scoring.json`. Default `"gauntlet"` / `{}`.
- `competitors` — the full candidate field, each with a `seed`
  (seeding order) and a `role` (`"champion"` is the protected incumbent;
  `"challenger"` everyone else). Default `[]`.
- `rounds` — the per-structure progression (§2.4). Default `[]`.
- `gen_states` — per-generation advancement and elimination for the
  elimination structures (§2.4). Absent otherwise.
- `standings` — the live ranking (§2.5). Default `[]`.
- `field_status` — the minting outcome for every challenger the
  proposer attempted this round (`status` is `"applied"` or
  `"rejected"`), so a field where every challenger failed reads as
  "N proposed · 0 applied". Default `[]`.
- `projected` — the **live projected standing** per in-flight competitor
  (§2.5.1). Default `{}`.

For `structure == "gauntlet"` the runner writes `parent_generation_id` /
`child_generation_id`, and those two fields describe the crowning duel
for every structure.

### 2.3 The per-entry row — generalized `side`

`ActiveTournamentEntry` (`runtime/state.py`) is keyed on
`(entry_id, side)`, and `side` is a `str`. Two kinds of row share the
list:

- **Board-unit rows.** Every duel runs each board entry under both of
  its sides, so a duel writes one row per `(entry_id, side)` with `side`
  `"parent"` (the champion or left competitor) or `"child"` (the
  challenger or right competitor), and `match_id` naming the duel.
- **Field rows** (non-gauntlet structures). The live projection adds one
  row per competitor, with the competitor's `generation_id` as
  `entry_id` and its role (`"champion"` or `"challenger"`) as `side`.
  The console's field funnel groups on these rows; their `status` and
  `loss_summary` follow the competitor's standing.

The `match_id` field names the duel a board-unit row belongs to (a
candidate may appear in several duels across the stages of a Swiss or
racing tournament):

```jsonc
{
  "entry_id": "research_basic",
  "side": "child",               // board-unit row: "parent"|"child"; field row: a role
  "match_id": "r2_m1",           // the duel's matchup id
  "status": "completed",
  "started_at": "...", "completed_at": "...",
  "loss_summary": { ... }, "drift_count_snapshot": { ... },
  "adk_session_id": "..."
}
```

- `match_id` — the scheduling strategy's matchup id: `"gauntlet"` for
  the gauntlet's one duel, the §2.4 ids for the other structures, and
  `"holdout-confirm"` for the holdout confirmation. Default `""`.

`update_tournament_entry(writer, entry_id, side, **updates)` keys on
`(entry_id, side)` for every structure.

### 2.4 `rounds` — per-structure progression

`rounds` is a list of round objects; the `structure` field decides how
to read each. The shape is a **tagged union** keyed on the same
`structure` value:

**Common to all** — one round object (a `RoundRecord`, serialized by
`_serialise_rounds` in `evolve/dashboard_projection.py` for the live
envelope, the settled envelope, and the durable record alike):
```jsonc
{
  "stage_index": 0,             // the stage WITHIN one tournament (bracket round, Swiss round, racing rung)
  "label": "Rung 0",            // human label for the UI
  "matches": [ { /* per-match, below */ } ]
}
```

`stage_index` is a different axis from a generation's `round_index`,
which is the evolve round the generation was born in. Readers also
accept `round_index` as the key.

**A match** (the unit a bracket node / Swiss pairing / racing rung
evaluates) generalizes the single champion-vs-challenger comparison:
```jsonc
{
  "match_id": "r1_m0",
  "competitors": ["v4", "v5"],     // generation ids in this match (2 for a duel; N for a racing rung)
  "winner": "v5",                   // generation id, or null when the match crowned no side
  "decision": "promoted",           // TournamentDecision: promoted|rejected|deferred; "" while pending
  "delta_scalar": -0.12,            // the duel's scalar delta; null for an N-way rung
  "bracket_slot": "WB-R1-0",        // elimination brackets only; "" otherwise
  "bye": false,                     // true when a competitor advanced without playing
  "survivors": [], "cut": [],       // racing rungs only
  "board_fraction": null,           // racing rungs only
  "pending": false,                 // true for a scheduled, unresolved match in the live envelope
  "live_progress": {}               // racing rungs in flight: per-lane board progress
}
```

Per-structure use of `rounds`:

- **gauntlet** — one round, one match: `competitors: [champion,
  challenger]`. This is the canonical shape every other structure
  degenerates to.
- **single_elim** — `rounds[k]` is bracket round *k*; `matches[]` are
  that round's pairings; `match_id` and `bracket_slot` are
  `"WB-R{k}-{n}"`; a `bye:true` match has a single competitor. The
  crowning duel against the champion is `"final"`.
- **double_elim** — same, with `"WB-"` (winners') and `"LB-"` (losers')
  prefixes; the grand final against the champion is `"GF"`.
- **swiss** — `rounds[k]` is Swiss round *k*; `matches[]` are that
  round's pairings, with `match_id` `"r{k}_m{n}"` and no `bracket_slot`;
  the crowning duel is `"swiss-final"`.
- **racing** — `rounds[k]` is rung *k*; **one match per rung**
  (`match_id` `"rung{k}"`) whose `competitors` is the surviving field
  at that rung, `winner` is null (a rung does not crown, it cuts), and
  the rung fields carry the cut:
  ```jsonc
  { "match_id": "rung1", "competitors": ["v4","v5","v6","v7"],
    "survivors": ["v4","v6"],         // who advances to the next rung
    "cut": ["v5","v7"],               // who is eliminated at this rung
    "board_fraction": 0.5 }           // fraction of the board this rung evaluated
  ```
  The individual rung duels run as `"rung{k}_m{n}"`, and the crowning
  duel is `"racing-final"`.

For the two elimination structures the durable record also carries
diagram analysis computed when the record is written
(`tournament/structure.py::attach_elim_states`): each match gains a
`loser`, each round a `bracket_side` (`"WB"` or `"LB"`), and the record a
`gen_states` list with, per generation, `played_rounds`,
`advanced_rounds`, `lost_rounds`, `eliminated_at_round`,
`side_by_round`, `lb_entry_round`, and `projected`.

### 2.5 `standings` — the live ranking

A flat ranking the dashboard renders as a leaderboard. Always
derivable, always present once any run settles:
```jsonc
[
  { "generation_id": "v5", "rank": 1, "scalar": 0.41, "wins": 2, "losses": 0, "status": "alive", "role": "challenger" },
  { "generation_id": "v3", "rank": 2, "scalar": 0.44, "wins": 1, "losses": 1, "status": "alive", "role": "champion" },
  { "generation_id": "v4", "rank": 3, "scalar": 0.52, "wins": 0, "losses": 2, "status": "eliminated", "role": "challenger" }
]
```
- `status ∈ {"alive", "eliminated", "champion"}`; `role ∈
  {"champion", "challenger"}`. The strategy records
  standings for every tournament, including the two gauntlet competitors.
  Swiss and racing views use the standings alongside recorded matches.
- `wins` / `losses` are meaningful for bracket / Swiss; for racing they
  may be `0` and the UI reads survival from `rounds[].cut`.

**Live projected overlay (optional, in-flight only).** While a competitor
is being evaluated, the orchestrator's live publish overlays the projected
fields (§2.5.1) onto its standing row, and re-ranks per the per-structure
rule below. A settled row carries none of these:

| Overlay field | Meaning |
|---|---|
| `in_flight` | `true` for a competitor in a still-pending match; absent/`false` for a settled row. |
| `projected_scalar` | the running aggregate scalar over boards-so-far (lower is better) — the dashboard renders it as `~<value>` with a "proj" badge. |
| `boards_done` / `boards_total` | the scored-board progress, driving a projected sub-bar distinct from the time-progress bar. |

### 2.5.1 `projected` — the live projected standing map

`ActiveTournament.projected` is `{generation_id: {scalar, boards_done,
boards_total, pass_rate}}`, written by the runner's `_IncrementalScorer`
(`tournament/scheduling.py`) the instant each board unit settles
(alongside `partial_*_agg`), via `update_tournament_projected`. The value
is the same running `aggregate_generation_score` over the boards settled
so far for that competitor, with the boards-so-far / boards-total
progress folded in. Default `{}` (no projection before the first board
settles).

**Ranking during execution.** The orchestrator combines partial scores with
the strategy's standings. It substitutes a partial scalar only for a competitor
whose evaluation is running. The dashboard displays the published order:

- `single_elim` / `double_elim` / `racing` — **scalar rank.** The
  projected scalar replaces the in-flight row's (still-zero) scalar in the
  sort, so an in-flight leader bubbles up live.
- `swiss` — **NEVER project Copeland points.** A half-finished duel has
  crowned no winner; the points-rank is authoritative. The projected
  scalar only nudges the mean-scalar TIEBREAK among rows on equal wins,
  and the in-flight pairing is marked visually. The standings are never
  re-ranked on points by a projection.
- `gauntlet` — the projected delta (challenger − champion) reads on the
  two-row view; no multi-competitor standings to re-rank.

### 2.6 Completed results in the index and experiment readers

The index and experiment readers project committed round results into the
following forms:

**(a) The SQLite `tournaments` table** (`index/schema.py`) holds two
kinds of row. A **crowning row**, keyed
`"{epoch}:{parent}->{child}"`, is written per resolved candidate from
its outcome; a **field row**, keyed `"{epoch}:field:{first_challenger}"`,
is written per tournament from its structure record. The columns:

| Column | Type | Meaning |
|---|---|---|
| `tournament_id` | `TEXT` | the row key above |
| `epoch_id` | `TEXT` | the owning epoch |
| `parent_generation_id`, `child_generation_id` | `TEXT` | the crowning pair |
| `decision`, `rejection_reason` | `TEXT` | the crowning verdict |
| `parent_scalar`, `child_scalar`, `delta_scalar` | `REAL` | the crowning scalars |
| `ran_at` | `TEXT` | when the tournament ran |
| `structure` | `TEXT` | the epoch's `tournament.structure` |
| `structure_params_json` | `TEXT` | the verbatim `params` JSON |
| `competitors_json` | `TEXT` | the candidate field |
| `rounds_json` | `TEXT` | the settled `rounds` (§2.4) |
| `standings_json` | `TEXT` | the final `standings` (§2.5) |
| `field_status_json` | `TEXT` | the minting outcome per attempted challenger |
| `champion_eval_mode` | `TEXT` | whether the champion side was re-run or read from cache |
| `champion_run_ref` | `TEXT` | where the champion's measurements live |

The per-matchup columns describe the **crowning** match for every
structure — the match that decided who becomes the new champion — so a
reader that only knows the gauntlet shape still gets a coherent
champion-vs-challenger answer; a structure-aware reader reads
`rounds_json` / `standings_json` for the full bracket. Per-match detail
that needs to be queryable is reconstructable from the `runs` /
`loss_profiles` tables, which carry `tournament_id` and
`generation_id`, so no per-match table exists.

**(b) `OutcomeRecord`** (`core/experiment.py`, persisted per candidate
in the round's `field_settlement.json` and joined to `experiment.json`
by the experiment reader). It describes one generation's *outcome
within its tournament*: the deltas (`pass_rate_delta`,
`drift_loss_delta`, `scalar_score_delta`), `tournament_decision`,
`rejection_reason`, `metric_movements`, and these structure fields:

```python
structure: str = "gauntlet"
final_rank: int | None = None          # the generation's rank in standings
eliminated_in_round: int | None = None  # bracket/racing: the stage it was cut; None if it survived
match_record: tuple[MatchOutcome, ...] = ()  # per-match results this generation played
```
where `MatchOutcome` is the frozen dataclass `{ match_id: str,
opponent: str, won: bool, delta_scalar: float }` (a gauntlet leaves it
empty). The record also carries `champion_eval_mode`, the holdout
fields (`holdout`, `train_loss`, `holdout_loss`, `generalization_gap`),
the operator-override fields (`operator_override`,
`operator_override_reason`), and the evidence-gate block (`evidence`).
`tournament_decision` is the crowning verdict for THIS generation (did
it become champion).

### 2.7 Storage routing — nothing new

All of the above rides on the **existing storage seams**:

- The live `ActiveTournament` is reconstructed from
  `runtime/active_tournament.events.jsonl` via the `StorageBackend`
  (`runtime/_storage.py`). The structure fields are ordinary keys in
  the same `to_dict` / `from_dict`.
- The settled `OutcomeRecord` values and the structure record live in
  the round's `field_settlement.json`, written through the
  `StorageBackend` by `epoch/settlement_receipt.py`. The in-progress
  structure record is written by `tournament/records.py`.
- The `tournaments` table is derived by `index/ingest.py`. An index
  whose schema version differs from the build's is refused until
  `zicato repair index` rebuilds it from the canonical files.

### 2.8 Absent fields

| Reader / data | Behaviour when a field is absent |
|---|---|
| `ActiveTournament.from_dict` | `structure` ⇒ `"gauntlet"`; `competitors` / `rounds` / `standings` / `field_status` ⇒ `[]`; `projected` ⇒ `{}`; `gen_states` stays absent. |
| `ActiveTournamentEntry.from_dict` | `match_id` ⇒ `""`. |
| `OutcomeRecord` | `structure` ⇒ `"gauntlet"`; rank / stage ⇒ `None`; `match_record` ⇒ `()`. |
| Epoch `scoring.json` with no `tournament` key | ⇒ the recommended racing specification (§1.4). The Epoch view omits its `tournament` block for such a file (§3.1). |

---

## 3. The dashboard API

The dashboard renders the configured structure rather than an
illustrative topology (see [`TOURNAMENT.md`](TOURNAMENT.md) §2). Two
changes carry it: the structure is exposed on the existing
epoch/tournament endpoints, and one endpoint serves the full structure
state.

### 3.1 Structure fields on the epoch, bracket, and live endpoints

- **`GET /api/epoch`** (`build_epoch_view`, `query/epoch_view.py`) —
  carries a `tournament` block echoing the epoch's structure:
  ```jsonc
  "tournament": { "structure": "swiss", "params": { "rounds_n": 4 } }
  ```
  Read from the epoch's frozen `scoring.json`. When the file has no
  `tournament` key the block is omitted. This lets the Epoch view name
  the structure without a second fetch.

- **`GET /api/tournaments`** (`build_bracket`, `query/tournament_view.py`) —
  carries top-level `structure` and `structure_params`, the
  `champion_lineage`, the `matchups` ladder read from the index's
  crowning rows, and a `tournaments` array holding each recorded
  structure record (§2.1) plus a `champion` object (`id`, `scalar`,
  `eval_mode`, `run_ref`):
  ```jsonc
  {
    "epoch_id": "...", "structure": "swiss", "structure_params": { "rounds_n": 4 },
    "champion_lineage": [ ... ],
    "matchups": [ ... ],                  // crowning match per candidate
    "tournaments": [                       // structure-aware per-tournament state
      { "tournament_id": "2026-06-01_e3:field:v4", "structure": "swiss",
        "competitors": [ ... ], "rounds": [ ... ], "standings": [ ... ],
        "champion": { "id": "v3", "scalar": 0.44, "eval_mode": "full", "run_ref": "..." } }
    ]
  }
  ```
  When no record names a structure, `structure` falls back to the
  epoch's frozen `scoring.json`.

- **`GET /api/active-tournament`** (`read_active_tournament_dict`,
  `query/runtime_view.py`) — returns the whole
  `ActiveTournament.to_dict()`, so the §2.2 structure fields surface
  directly. `_normalize_tournament_statuses` rewrites each entry's
  status to its canonical bucket (keeping the original in `status_raw`)
  and passes an opaque competitor `side` (a generation id) through
  unchanged.

### 3.2 The endpoint for tournament structure

`GET /api/tournament-structure/{epoch_id}/{tournament_id}` calls
`build_tournament_structure` in `query/tournament_view.py`. The response
supplies the matches and standings used to render tournament brackets,
standings, and racing ladders:

```jsonc
{
  "epoch_id": "2026-06-01_e3",
  "tournament_id": "tourn_e3_v4",
  "structure": "single_elim",
  "structure_params": { "field_size": 2 },
  "competitors": [
    { "generation_id": "v3", "seed": 1, "role": "champion" },
    { "generation_id": "v4", "seed": 2, "role": "challenger" }
  ],
  "rounds": [
    { "round_index": 0, "label": "Semifinal",
      "matches": [
        { "match_id": "WB-R0-0", "competitors": ["v3","v5"], "winner": "v3",
          "decision": "rejected", "delta_scalar": 0.08, "bracket_slot": "WB-R0-0", "bye": false }
      ] }
  ],
  "standings": [ { "generation_id": "v3", "rank": 1, "scalar": 0.41, "status": "champion" } ],
  "field_status": [ ... ],
  "source": "record" | "active" | "unavailable"
}
```

The reader consults recorded tournament structures first, then the live
active tournament. A completed round is authoritative for its actual
matches and standings; the elimination structures also carry
`gen_states` (§2.4), and the response is enriched with field-diversity
and standings-rating data. A request for a candidate pair selects that pair's
recorded matches from the tournament. Missing structure returns an empty
response; loss files alone cannot establish a bracket or a winner.

The handler validates epoch and tournament identifiers before reading the
workspace. Invalid coordinates return the empty response at HTTP 200;
a reader failure returns the endpoint's degraded envelope, whose
`source` is `"loss_files"`.

### 3.3 The `/api/round/.../gate` endpoint

The gate reader, `query.gate_view.build_gate_breakdown`, takes an epoch and
two candidate identities. It reads the corresponding `gate_results` entry
from the committed round. That entry records the rule results, explanation,
scalar margin, and training aggregates compared during execution. The runner
records the actual regression-suite result as part of that explanation.

The reader displays recorded decisions without calling `evaluate_gate`.
When no explanation is recorded, `rules` is empty and `deciding_rule` is null.
Candidate outcomes can still explain rejection before evaluation or a later
confirmation or operator decision. Bracket and Swiss views request the same
endpoint for each recorded pair.

---

## 4. Contract-hash interaction

The `tournament` block is part of the **scoring** contract component
(§1.2), so it factors into the contract hash through the scoring
canonicalizer (`_canon_scoring` in `epoch/contract.py`).
`scoring_to_canon` serializes *every declared field* of `ScoringWeights`
under its persisted key (`dataclass_to_jsonable`), so the
`tournament_structure` field is folded into the canonical form with no
special case, and `json.dumps(sort_keys=True)` orders the nested
`{structure, params}` object.

The one care point: the `params` mapping must canonicalize
order-independently. `json.dumps(sort_keys=True)` already sorts the
top-level and nested dict keys; the only non-deterministic case is a
list value whose order is semantically irrelevant (e.g.
`swiss.tiebreak`). The data model treats `params` **verbatim** (order
preserved). Order-insensitive params must be canonicalized by the
selection layer that defines them. This is stated here so the two halves
agree.

Consequence (the desired behaviour): **changing the structure or any
param rolls the epoch.** Switching `racing → gauntlet`, or raising
`field_size` from 4 to 6, changes `_canon_scoring`'s output, changes
the contract hash, and `evolve`'s auto-roll path closes the current
epoch and opens a fresh one — as it does for a `promote_margin` retune.
The roll names the changed component as `scoring`, because
`compute_component_hashes` (`epoch/contract.py`) hashes the structure
inside that component. This is correct: a gauntlet champion and a
racing champion are selected under different rules, so they live in
different epochs.

---

## 5. CLI / `RuntimeConfig` surface

### 5.1 The contract knob lives in `scoring.json`

Because the structure is a **frozen contract component**, the primary
way to set it is by editing `scoring.json` (the same way an operator
retunes `promote_margin`) and re-running `evolve` — auto-epoching rolls
the epoch. This is consistent with how every other contract knob is
set: there is no `--promote-margin` flag either.

### 5.2 `zicato evolve --tournament-structure` (convenience, contract-affecting)

For ergonomics, `evolve` has **two flags** that *edit the structure in
the contract before the hash is computed*:

```
zicato evolve --tournament-structure racing [--tournament-param field_size=6] ...
```

- `--tournament-structure {gauntlet|racing}` — writes the structure
  into the **live** `scoring.json` (the contract source) before contract
  resolution, so it participates in the contract hash and rolls the
  epoch if it differs from the current one. Unset ⇒ read whatever
  `scoring.json` says (racing when absent). The experimental structures
  are selected in `scoring.json` alongside
  `experimental.tournament_structures = true`.
- `--tournament-param KEY=VALUE` (repeatable) — sets one `params` key and
  preserves the others. Values are parsed as JSON when possible, else
  taken as a string.

Both flags are checked in memory under `--dry-run` and cannot be
combined with `--epoch`. They are contract-mutating conveniences rather
than per-invocation runtime toggles, equivalent to editing
`scoring.json` by hand. `zicato evolve --help` is the authoritative
description, and [`CLI.md`](CLI.md) is generated from it.

### 5.3 `RuntimeConfig` — no structural change

`RuntimeConfig` (`core/runtime.py`) is the *runtime-side* binding:
workspace, the model callables, `parallelism`, `seed`. The tournament
structure is a **contract** property rather than a runtime one, and it
lives on the frozen `ScoringWeights`, which the runner receives.
`RuntimeConfig` carries no structure field. The evolve loop builds the
strategy from `weights.tournament_structure` (`make_strategy`), and
`_weights_spec` (`tournament/worker_transport.py`) serializes the
complete weights so the subprocess worker sees the same contract.
Selection itself happens in the orchestrator rather than the per-run
worker.

---

## 6. The console rendering

The console is served from `src/zicato/dashboard/static/js/`. Three
modules carry the structure views:

- **`data.js`** — `tournamentStructure(epochId, tournamentId)` fetches
  `/api/tournament-structure/{epoch_id}/{tournament_id}`, and the
  live-invalidation set includes the `/api/tournament-structure/`
  prefix so the structure refreshes as a tournament runs. `epoch()`
  already returns the `tournament` block (§3.1).
- **`tournament_model.js`** — pure models over the served payloads: the
  normalized structure, the resolver every page reads a non-gauntlet
  structure through, the per-structure models the figures draw
  (elimination bracket, Swiss ladder, racing rungs, gauntlet field), and
  the digests the gated swaps compare. It builds no DOM.
- **`views/structure.js`** — renders those models: the structure pill,
  the bracket, the Swiss ladder, the racing rung ladder, the gauntlet
  field bars, the standings table, and the field-diversity ribbon.

### 6.1 The Rounds view — `views/gens.js`

The Rounds page reads the epoch's structure (the `tournament` block, or
the live structure while the epoch runs) and branches:

- **`gauntlet`** — the champion-defends banner, the match-card grid and
  the roster table.
- **every other structure** — the structure pill and `renderStructure`
  from `views/structure.js`: a **bracket** for `single_elim` /
  `double_elim` (a losers' band for `double_elim`), a **standings
  ladder** with per-round pairings for `swiss`, and a **rung ladder**
  for `racing` showing each rung's field, `cut[]`, and
  `board_fraction`. Match nodes link to the candidate page and read the
  per-pair gate endpoint (§3.3).

Each render is gated by `gatedSwap(host, digest, ...)`, with a digest
that includes the structure and the settled `rounds`, so the pane
re-renders only on a real change.

### 6.2 The epoch view — `views/epoch.js`

`epoch.js` reads the `tournament` block (§3.1), adds the structure pill
to the epoch header, and for a non-gauntlet structure draws the round
reel from the recorded structure (`/api/tournament-structure`) rather
than from `matchups`.

### 6.3 Boards and candidate views — structure-agnostic

`views/boards.js`, `views/board.js`, and `views/candidate.js` are
per-entry / per-candidate and read the per-entry and per-judge
endpoints, which carry no structure fields.

---

## 7. Where each part lives

### 7.1 Data model / config / contract

| File | Part |
|---|---|
| `src/zicato/core/tournament.py` | `TournamentStructure`, `VALID_TOURNAMENT_STRUCTURES`, `EXPERIMENTAL_TOURNAMENT_STRUCTURES`, `TOURNAMENT_PARAM_CONSTRAINTS`, `MatchOutcome`, `TournamentDecision`, and the default racing specification. |
| `src/zicato/core/scoring_config.py` | The `ScoringWeights.tournament_structure` field (persisted as `tournament`) and the load-time refusal of an experimental structure without the opt-in. |
| `src/zicato/core/experiment.py` | `OutcomeRecord` with the structure fields (§2.6). |
| `src/zicato/epoch/contract.py` | The scoring canonicalizer that folds the block into the contract hash (§4). |

### 7.2 Persistence — runtime and settled records

| File | Part |
|---|---|
| `src/zicato/runtime/state.py` | `ActiveTournament`, `ActiveTournamentEntry`, `update_tournament_entry`, `update_tournament_projected` (§2.2–§2.5.1). |
| `src/zicato/selection/strategy.py` | `RoundRecord`, `MatchRecord`, `Standing` (§2.4, §2.5). |
| `src/zicato/evolve/dashboard_projection.py` | `_serialise_rounds` / `_serialise_standings` and the in-progress structure record. |
| `src/zicato/tournament/records.py` | The structure record (`field_tournament_record`), its decoder, and the in-progress file under `tournaments/`. |
| `src/zicato/tournament/structure.py` | `attach_elim_states`, the elimination diagram analysis (§2.4). |
| `src/zicato/epoch/settlement_receipt.py` | The round record `field_settlement.json` (§2.1). |
| `src/zicato/index/schema.py`, `src/zicato/index/ingest.py` | The `tournaments` table and its crowning and field rows (§2.6). |

### 7.3 Dashboard API

| File | Part |
|---|---|
| `src/zicato/query/epoch_view.py` | `build_epoch_view` and its `tournament` block (§3.1). |
| `src/zicato/query/tournament_view.py` | `build_bracket` and `build_tournament_structure` (§3.1, §3.2). |
| `src/zicato/query/runtime_view.py` | `read_active_tournament_dict` and `_normalize_tournament_statuses` (§3.1). |
| `src/zicato/query/gate_view.py` | `build_gate_breakdown` (§3.3). |
| `src/zicato/dashboard/endpoints.py`, `src/zicato/dashboard/server.py` | The endpoint declarations and routes. |

### 7.4 Console

| File | Part |
|---|---|
| `src/zicato/dashboard/static/js/data.js` | `tournamentStructure()` and its invalidation prefix. |
| `src/zicato/dashboard/static/js/tournament_model.js` | The structure models. |
| `src/zicato/dashboard/static/js/views/structure.js` | The structure renderers. |
| `src/zicato/dashboard/static/js/views/gens.js`, `views/epoch.js` | The Rounds page and the epoch header (§6.1, §6.2). |

### 7.5 CLI

| File | Part |
|---|---|
| `src/zicato/cli/commands/evolve.py` | `--tournament-structure` and the repeatable `--tournament-param KEY=VALUE` (§5.2). |

### 7.6 Structure-agnostic parts

`storage/base.py`, `storage/files.py`, `storage/memory.py` — the record
seam carries the structure fields as ordinary JSON. `RuntimeConfig` —
the structure is a contract property (§5.3). The per-run subprocess
worker (`_tournament_worker.py`) runs ONE board entry under ONE
generation; which competitors are paired is the orchestrator's job. The
gate (`tournament/gate.py`) is per-match and champion-vs-challenger.

---

## 8. Cross-references

| Topic | Document |
|---|---|
| The selection algorithms that drive each structure (pairing, cuts, racing) | [SELECTION.md](SELECTION.md), [TOURNAMENT-STRUCTURES.md](TOURNAMENT-STRUCTURES.md) |
| The operational gauntlet view, the bracket, per-matchup analytics | [TOURNAMENT.md](TOURNAMENT.md) |
| The `tournament` config block in the epoch contract + contract-hash roll | [EPOCHS-AND-JOURNALING.md](EPOCHS-AND-JOURNALING.md) §10 |
| The generalized persisted record and the storage seams | [STORAGE.md](STORAGE.md) §5 |
| The `--tournament-structure` flag | `zicato --help`, and [CLI.md](CLI.md), which is generated from it |
| The scalar each match compares | [SCORING.md](SCORING.md) |
