# Tournament data model — configurable per-epoch structures

> **Status.** This document is the as-built reference for the storage
> and interface half of configurable per-epoch tournament structures.
> The design was built as specified, and the runtime record
> (`runtime/state.py::ActiveTournament`) and the index `tournaments`
> table cite these section numbers from their source comments. Sections
> written as an implementation plan (§7) record the work that was carried
> out, and are kept because those section numbers are cited.
> The pairing, elimination and racing-cut algorithms that drive each
> structure, and the decision theory under them, are specified in
> [`SELECTION.md`](SELECTION.md), [`TOURNAMENT.md`](TOURNAMENT.md), and
> [`TOURNAMENT-STRUCTURES.md`](TOURNAMENT-STRUCTURES.md). The
> `tournament` config block of §1 is the shared contract between the two
> halves: its field name (`tournament`) and its shape
> (`{structure, params}`) must stay byte-identical across both
> specifications.
>
> The reader module these sections cite as `dashboard/state_reader.py` has
> since been split into the `zicato.query` package: the epoch view now lives
> in `query/epoch_view.py` and the bracket reader in `query/tournament_view.py`.
> Line numbers in the citations below predate that split and are not current.

The tournament structure is a per-epoch configurable choice —
`gauntlet` (the default), `single_elim`, `double_elim`, `swiss`,
`racing`. The gauntlet is a king-of-the-hill contest: one reigning
champion, one challenger per round, and a three-rule promote gate (see
[`TOURNAMENT.md`](TOURNAMENT.md) §1 and [`SELECTION.md`](SELECTION.md)
§3). Its record shape — two sides per board entry, `parent` and `child`
— is the shape every other structure generalizes, and generalizing it
had to leave the gauntlet record, the dashboard reads, and the
contract-hash machinery working unchanged.

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
  "structure": "gauntlet",   // gauntlet|single_elim|double_elim|swiss|racing
  "params": { /* structure-specific, see §1.3 */ }
}
```

- `structure` — a closed enum string. The five values are
  `"gauntlet"`, `"single_elim"`, `"double_elim"`, `"swiss"`,
  `"racing"`. Unknown values are rejected at validation time with a
  message listing the valid tokens (the same posture
  `_coerce_enum` takes for board enums in
  `src/zicato/core/types.py:577`).
- `params` — a structure-specific JSON object. Absent ⇒ `{}` ⇒ every
  param defaults. The defaults are chosen so that an operator who writes
  only `{"structure": "swiss"}` gets a sensible Swiss tournament.

### 1.2 Where it lives on disk

The block lives in **`scoring.json`** under a top-level `tournament`
key rather than in a file of its own, for two reasons.

- `scoring.json` is already a frozen contract component (it is one of
  the four things the contract hash reduces — see
  [`EPOCHS-AND-JOURNALING.md`](EPOCHS-AND-JOURNALING.md) §10.1). The
  selection *gate thresholds* (`promote_margin`,
  `pass_rate_monotonicity`, the `namespace_monotonicity` flags) already
  live there. The tournament *structure* is the same kind of
  thing — "how the crowning decision is made" — so it belongs in the
  same document. Putting it there means it factors into the contract
  hash with **zero new plumbing** in `resolve_contract_inputs`.
- A separate `tournament.json` would need a fifth contract component, a
  fifth canonicalizer, a fifth `register --tournament PATH` flag, and a
  fifth frozen-copy path. None of that earns its weight.

The new `tournament` block is **modeled in `ScoringWeights`** (see §5,
data-model plan) as a frozen `TournamentStructure` dataclass field —
mirroring how `namespace_weights` / `namespace_monotonicity` are modeled
fields with `_default_*` factories. The frozen copy under
`epochs/{id}/scoring.json` carries it; the live operator-side
`scoring.json` carries it; both canonicalize identically (§4).

### 1.3 Per-structure `params`

Each structure interprets `params` differently. The **field names** here
are the shared contract with the selection-logic design; the **semantics**
(how a value drives pairing and cuts) are specified in
[`TOURNAMENT-STRUCTURES.md`](TOURNAMENT-STRUCTURES.md). The
defaults below are the data-model's responsibility (what a partial
document fills in).

| `structure` | `params` keys (with defaults) | Notes |
|---|---|---|
| `gauntlet` | `{ "replications": 1 }` | The shipped behaviour. `replications=1` = run the board once per side (today's exact gauntlet). `>1` is the racing-flavoured replication §7 of SELECTION.md proposes; the data model carries it and the selection layer owns whether it is honored. |
| `single_elim` | `{ "seed_order": "scalar" \| "lineage" \| "as_listed", "bye_policy": "top_seed" \| "none" }` | Bracket over the round's candidate field. `seed_order` picks how candidates are seeded into slots; `bye_policy` says who gets a bye when the field is not a power of two. |
| `double_elim` | same as `single_elim`, plus `{ "grand_final_reset": true }` | Adds a losers' bracket. `grand_final_reset` = whether a losers'-bracket finalist must beat the winners'-bracket finalist twice. |
| `swiss` | `{ "rounds": 4, "pairing": "score_then_lineage", "tiebreak": ["buchholz", "scalar"] }` | Fixed number of rounds; each round pairs candidates of similar standing. |
| `racing` | `{ "rungs": [{ "fraction": 1.0, "keep": 0.5 }, ...], "min_survivors": 1 }` | Successive-halving / ASHA. Each rung evaluates survivors on a `fraction` of the board, keeps the best `keep` fraction; iterate down to `min_survivors`. |

Every default keeps the structure usable from a bare `{"structure":
"..."}`. The data model **stores and round-trips** `params` verbatim as
a JSON object (`Mapping[str, Any]`); it does NOT type each structure's
params into its own dataclass. Keeping `params` an opaque mapping means
the selection layer can add a param without a data-model change, which is
the forward-compatibility posture `BoardEntry.context` and
`Pattern.detail` already take in `core/types.py`.

### 1.4 Validation and defaulting

- **Default.** A `scoring.json` with no `tournament` key ⇒
  `{"structure": "gauntlet", "params": {}}`. This is the back-compat
  contract: every epoch on disk today, and every operator who never
  touches the knob, gets the gauntlet — byte-for-byte the current
  behaviour.
- **Structure validation.** `structure` must be one of the five tokens.
  The loader (`_scoring_weights_from_dict` /
  `_scoring_from_dict`) rejects an unknown token with a clear error.
- **Params validation.** The data-model layer validates only that
  `params` is a JSON object (a `Mapping`). Per-key validation (e.g.
  `swiss.rounds >= 1`, `racing.rungs` non-empty) belongs to the
  **selection layer**, which owns the algorithm that reads them. The
  split mirrors `BoardEntry`: the type layer enforces shape, the
  consumer enforces semantics.

---

## 2. The generalized persisted tournament record

### 2.1 Two persistence surfaces, generalized in lockstep

A tournament is persisted in two places, and the generalization covers
both:

1. **The live runtime record** — `ActiveTournament` in
   `src/zicato/runtime/state.py`, one
   `runtime/active_tournament.json` file the dashboard polls while a
   tournament is in flight. Its per-matchup core is two top-level
   generation ids (`parent_generation_id` / `child_generation_id`) and a
   flat `entries` list of `(entry_id, side)` rows where `side ∈
   {"parent", "child"}`.
2. **The settled record** — the `tournaments` table in the SQLite
   analytical index (`src/zicato/index/schema.py:137`) plus the
   per-generation `experiment.json` `outcome` block
   (`OutcomeRecord` in `core/types.py:1334`). Its per-matchup core is
   one `tournaments` row per champion-vs-challenger matchup with a
   single `decision` / `delta_scalar`.

The generalization adds a **structure-aware envelope** around the
per-matchup shape. The per-matchup fields are PRESERVED so a gauntlet
reads and writes unchanged; the envelope fields are ADDITIVE and default
to the gauntlet interpretation.

### 2.2 The live `ActiveTournament` — generalized

New top-level fields on `ActiveTournament` (all with back-compat
defaults so an old `active_tournament.json` loads unchanged):

```jsonc
{
  // ── existing fields, unchanged ──
  "tournament_id": "tourn_e3_v4",
  "epoch_id": "2026-06-01_e3",
  "parent_generation_id": "v3",   // gauntlet: the champion. other structures: "" (see below)
  "child_generation_id": "v4",    // gauntlet: the lone challenger. other structures: ""
  "started_at": "...",
  "phase": "running",             // running|completed|aborted
  "round_index": 0, "total_rounds": 1,
  "entries": [ /* per-(entry × side) rows, see §2.3 */ ],
  "partial_champion_agg": { ... }, "partial_challenger_agg": { ... },

  // ── NEW: the structure envelope ──
  "structure": "swiss",           // mirrors the epoch's tournament.structure; "gauntlet" for old files
  "structure_params": { "rounds": 4 },
  "competitors": [                 // the candidate field this tournament ranks
    { "generation_id": "v3", "seed": 1, "role": "champion" },
    { "generation_id": "v4", "seed": 2, "role": "challenger" },
    { "generation_id": "v5", "seed": 3, "role": "challenger" }
  ],
  "rounds": [ /* per-structure round/rung/bracket state, see §2.4 */ ],
  "standings": [ /* current ranking, see §2.5 */ ],

  // ── NEW: live projected standing per in-flight competitor, see §2.5.1 ──
  "projected": {
    "v4": { "scalar": 0.42, "boards_done": 3, "boards_total": 8, "pass_rate": 0.9 }
  }
}
```

- `structure` / `structure_params` — copied from the resolved epoch
  contract at tournament-start so a reader never has to re-resolve
  `scoring.json`. Default `"gauntlet"` / `{}`.
- `competitors` — the generalization of "two generations". A gauntlet
  has exactly two (the champion + the one challenger), so for a gauntlet
  this is derivable from the existing `parent_generation_id` /
  `child_generation_id` and a reader MAY ignore it. For every other
  structure it is the authoritative field — the full candidate field,
  each with a `seed` (seeding order) and a `role`
  (`"champion"` is the protected incumbent; `"challenger"` everyone
  else). Default `[]`.
- `rounds` — the per-structure progression (§2.4). Default `[]`.
- `standings` — the live ranking (§2.5). Default `[]`.
- `projected` — the **live projected standing** per in-flight competitor
  (§2.5.1). Default `{}`.

**Gauntlet back-compat invariant.** For `structure == "gauntlet"`, the
runner continues to write `parent_generation_id` /
`child_generation_id` unchanged, and `competitors` /
`rounds` / `standings` MAY be left empty — the dashboard's existing
gauntlet code path (`_build_matchup_conversations` in
`endpoints.py:892`, which reads `parent_generation_id` /
`child_generation_id`) keeps working untouched. New code paths read
`competitors` when `structure != "gauntlet"`.

### 2.3 The per-entry row — generalized `side`

`ActiveTournamentEntry` (`state.py:305`) is keyed on
`(entry_id, side)`, with `side ∈ {"parent", "child"}`. The
generalization **widens `side` to an opaque competitor key** without
changing its type (it stays `str`):

- For a **gauntlet**, `side` stays `"parent"` / `"child"` — unchanged.
- For every other structure, `side` becomes the **competitor's
  `generation_id`** (e.g. `"v5"`). A row is then "board entry `e` run
  under competitor `v5`". A bracket match between `v4` and `v5` on entry
  `e` produces two rows: `(e, "v4")` and `(e, "v5")`.

Two ADDITIVE fields disambiguate which match a row belongs to (a
candidate may appear in several matches across rounds of a Swiss /
double-elim run):

```jsonc
{
  "entry_id": "research_basic",
  "side": "v5",                  // gauntlet: "parent"|"child"; else: a generation_id
  "match_id": "r2_m1",           // NEW: which round/match this run is part of; "" for gauntlet
  "status": "completed",
  "started_at": "...", "completed_at": "...",
  "loss_summary": { ... }, "drift_count_snapshot": { ... },
  "adk_session_id": "..."
}
```

- `match_id` — links the row to a `rounds[].matches[]` entry (§2.4).
  Default `""` (gauntlet has one implicit match, so it needs no id).

`update_tournament_entry(workspace_root, entry_id, side, **updates)`
(`state.py:562`) already keys on `(entry_id, side)` — widening `side`'s
value domain requires **no signature change**. The runner passes the
competitor's generation id instead of `"parent"`/`"child"` for
non-gauntlet structures.

### 2.4 `rounds` — per-structure progression

`rounds` is a list of round objects; the `structure` field decides how
to read each. The shape is a **tagged union** keyed on the same
`structure` value:

**Common to all** — one round object:
```jsonc
{
  "round_index": 0,
  "label": "Round 1",          // human label for the UI
  "matches": [ { /* per-match, below */ } ]
}
```

**A match** (the unit a bracket node / Swiss pairing / racing rung
evaluates) generalizes the single champion-vs-challenger comparison:
```jsonc
{
  "match_id": "r1_m0",
  "competitors": ["v4", "v5"],     // generation ids in this match (2 for elim/swiss; N for a racing rung)
  "winner": "v5",                   // generation id, or "" while pending
  "decision": "promoted",           // reuses TournamentDecision: promoted|rejected|deferred; "" while pending
  "delta_scalar": -0.12,            // winner-vs-loser scalar delta (2-way); null for an N-way rung
  "bracket_slot": "WB-R1-0",        // single/double-elim only: winners'/losers' bracket position; "" otherwise
  "bye": false                      // true when a competitor advanced without playing
}
```

Per-structure use of `rounds`:

- **gauntlet** — `rounds` MAY be empty (back-compat). When populated,
  one round, one match: `competitors: [champion, challenger]`. This is
  the canonical shape every other structure degenerates to.
- **single_elim** — `rounds[k]` is bracket round *k*; `matches[]` are
  that round's pairings; `bracket_slot` is `"WB-R{k}-{n}"`; a `bye:true`
  match has a single competitor.
- **double_elim** — same, with `bracket_slot` prefixes `"WB-"`
  (winners') and `"LB-"` (losers'); the grand final is
  `"GF"` (+ `"GF-reset"` when `grand_final_reset` fires).
- **swiss** — `rounds[k]` is Swiss round *k*; `matches[]` are that
  round's pairings (no `bracket_slot`).
- **racing** — `rounds[k]` is rung *k*; **one match per rung** whose
  `competitors` is the full surviving field at that rung, `winner` is
  `""` (a rung does not crown, it cuts), and two ADDITIVE rung fields
  carry the cut:
  ```jsonc
  { "match_id": "rung1", "competitors": ["v4","v5","v6","v7"],
    "survivors": ["v4","v6"],         // who advances to the next rung
    "cut": ["v5","v7"],               // who is eliminated at this rung
    "board_fraction": 0.5 }           // fraction of the board this rung evaluated
  ```

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
- `status ∈ {"alive", "eliminated", "champion"}`. For a gauntlet,
  `standings` MAY be empty (the two-row view is enough); for a Swiss /
  racing run it is the primary UI surface.
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
the instant each board unit settles (alongside `partial_*_agg`), via
`update_tournament_projected`. The value is the SAME running
`aggregate_generation_score` over the boards settled so far for that
competitor, with the boards-so-far / boards-total progress folded in.
Default `{}` (no projection before the first board settles; old files
have no key and load empty).

**Per-structure ranking rule** (the dashboard + the orchestrator overlay
substitute the projected scalar into the EXISTING sort key for the
in-flight competitor ONLY; a settled competitor keeps its real value):

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

### 2.6 The settled record — `tournaments` table + `OutcomeRecord`

Two settled surfaces, generalized:

**(a) The SQLite `tournaments` table** (`index/schema.py:137`) gains
ADDITIVE columns (a v3 migration, mirroring the v2 column-add pattern at
`schema.py:194`):

| New column | Type | Meaning |
|---|---|---|
| `structure` | `TEXT` | the epoch's `tournament.structure`; `"gauntlet"` for back-fill |
| `structure_params_json` | `TEXT` | the verbatim `params` JSON |
| `competitors_json` | `TEXT` | the full candidate field as a JSON array of generation ids |
| `rounds_json` | `TEXT` | the settled `rounds` (§2.4) serialized |
| `standings_json` | `TEXT` | the final `standings` (§2.5) serialized |

The existing per-matchup columns (`parent_generation_id`,
`child_generation_id`, `decision`, `delta_scalar`, `rejection_reason`)
**stay** and continue to describe the **crowning** match — for every
structure, the match that decided who becomes the new champion. So a
reader that only knows the gauntlet shape (the existing
`build_bracket` / `build_matchup_detail`) still gets a coherent
champion-vs-challenger answer from the per-matchup columns; a
structure-aware reader joins in `rounds_json` / `standings_json` for the
full bracket.

For a non-2-way structure the **one tournament still has one
`tournaments` row** (keyed on `tournament_id`), with the bracket/Swiss
internals living in `rounds_json`. Per-match detail that needs to be
queryable (not just rendered) is reconstructable from the
`runs` / `loss_profiles` tables, which already carry `tournament_id`
(`schema.py:110,124`) and `generation_id`, so no per-match table is
needed.

**(b) `OutcomeRecord`** (`core/types.py:1334`, persisted in
`experiment.json`). It describes one generation's *outcome within its
tournament*. It is generalized with ADDITIVE fields (back-compat
defaults so existing journals deserialize):

```python
# additive fields on OutcomeRecord (all default so old JSON loads):
structure: str = "gauntlet"
final_rank: int | None = None          # the generation's rank in standings; None for a 2-way
eliminated_in_round: int | None = None  # bracket/racing: the round it was cut; None if it survived/won
match_record: tuple[MatchOutcome, ...] = ()  # per-match results this gen played (new small dataclass)
```
where `MatchOutcome` is a new frozen dataclass `{ match_id: str,
opponent: str, won: bool, delta_scalar: float }` (gauntlet leaves it
empty). `tournament_decision` keeps its existing meaning — the
crowning verdict for THIS generation (did it become / stay champion).

### 2.7 Storage routing — nothing new

All of the above rides on the **existing storage seams**:

- The live `ActiveTournament` is one JSON record at
  `runtime/active_tournament.json` via the `StorageBackend`
  (`runtime/_storage.py`). The new fields are just more keys in the
  same `to_dict` / `from_dict` — `base.py` / `files.py` /
  `memory.py` are untouched.
- The settled `OutcomeRecord` rides in `experiment.json` via the
  `epoch/_storage.py` keys — `write_experiment` /
  `_outcome_from_dict` (`journal.py:224`) gain the new fields. No new
  key helper, no new file.
- The `tournaments` table change is a schema migration in
  `index/schema.py` + an ingest change in `index/ingest.py`; the index
  is fully rebuildable (`zicato repair index`), so the migration is "drop and
  re-derive" on the rebuild path and an ADDITIVE column-add on the
  incremental-open path, the same pattern the v2 column-add used.

### 2.8 Back-compat summary

| Reader / data | Old gauntlet workspace behaviour |
|---|---|
| `ActiveTournament.from_dict` | missing `structure` ⇒ `"gauntlet"`; missing `competitors`/`rounds`/`standings` ⇒ `[]`; missing `projected` ⇒ `{}`; `parent`/`child_generation_id` still authoritative. |
| `ActiveTournamentEntry.from_dict` | missing `match_id` ⇒ `""`; `side` stays `"parent"`/`"child"`. |
| `OutcomeRecord` (journal) | missing `structure` ⇒ `"gauntlet"`; missing rank/round/`match_record` ⇒ `None`/`()`. `tournament_decision` unchanged. |
| `tournaments` table | incremental open adds the new TEXT columns as `NULL`; a full `reindex` populates them (`"gauntlet"` for runs that predate the feature). |
| `scoring.json` with no `tournament` key | ⇒ gauntlet (§1.4). |
| Dashboard gauntlet code paths | read the per-matchup fields only; untouched. |

No migration tool is required: every new field has a default that
reproduces the gauntlet, and the only stateful store (the SQLite index)
is rebuildable.

---

## 3. The dashboard API additions

The dashboard renders the configured structure rather than an
illustrative topology (see [`TOURNAMENT.md`](TOURNAMENT.md) §2). Two
changes carry it: the structure is exposed on the existing
epoch/tournament endpoints, and one endpoint serves the full structure
state.

### 3.1 Extended fields on existing endpoints (additive)

- **`GET /api/epoch`** (`build_epoch_view`, `query/epoch_view.py`) — add
  a `tournament` block echoing the epoch's resolved structure:
  ```jsonc
  "tournament": { "structure": "swiss", "params": { "rounds": 4 } }
  ```
  Read from the epoch's frozen `scoring.json`. Absent ⇒ omit (the
  frontend defaults to gauntlet). This lets the Epoch view name the
  structure without a second fetch.

- **`GET /api/tournaments`** (`build_bracket`, `query/tournament_view.py`) —
  add top-level `structure` and `structure_params`, and keep `matchups`
  / `champion_lineage` unchanged (the gauntlet shape).
  When the structure is non-gauntlet, ADD a `tournaments` field carrying
  the per-tournament settled `rounds_json` / `standings_json`:
  ```jsonc
  {
    "epoch_id": "...", "structure": "swiss",
    "champion_lineage": [ ... ],         // unchanged
    "matchups": [ ... ],                  // unchanged: crowning match per tournament
    "tournaments": [                       // NEW: structure-aware per-tournament state
      { "tournament_id": "tourn_e3_v4", "structure": "swiss",
        "competitors": ["v3","v4","v5"], "rounds": [ ... ], "standings": [ ... ] }
    ]
  }
  ```

- **`GET /api/active-tournament`** (`read_active_tournament_dict`,
  `state_reader.py:297`) — already returns the whole
  `ActiveTournament.to_dict()`, so the §2.2 new fields (`structure`,
  `competitors`, `rounds`, `standings`) surface **for free** once the
  runtime record carries them. The only change is in
  `_normalize_tournament_statuses` (`state_reader.py:245`), which must
  not assume `side ∈ {"parent","child"}` (§2.3) — it should pass through
  an unrecognised `side` (a generation id) untouched.

### 3.2 New endpoint — the structure state

`GET /api/tournament-structure/{epoch_id}/{tournament_id}` →
`build_tournament_structure(paths, epoch_id, tournament_id)` in
`state_reader.py`. The single read the UI uses to render a bracket /
standings / racing ladder for a settled tournament:

```jsonc
{
  "epoch_id": "2026-06-01_e3",
  "tournament_id": "tourn_e3_v4",
  "structure": "single_elim",
  "structure_params": { "seed_order": "scalar" },
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
  "source": "index" | "active" | "loss_files"
}
```

Resolution order (mirrors the existing `build_matchup_grid` fallback
chain at `state_reader.py:1731`): prefer the SQLite `tournaments` row's
`rounds_json` / `standings_json`; if the index is absent, read the live
`active_tournament.json`; if neither, reconstruct a degenerate
single-match view from the per-run `loss.json` files. A malformed id
degrades to an empty structure (HTTP 200), matching every other handler
in `endpoints.py`.

The endpoint is wired in `make_endpoints` (`endpoints.py:78`) under a new
`api_tournament_structure` handler and a `server.py` route
`/api/tournament-structure/{epoch_id}/{tournament_id}` with the same
`_is_safe_id` guards every coordinate handler uses.

### 3.3 The `/api/round/.../gate` endpoint

`build_gate_breakdown` (`endpoints.py:353`) is **per-match** and stays
unchanged — it already takes `(epoch_id, champion, challenger)`.
Under a bracket or Swiss structure the interface calls it once per match,
with that match's two `competitors`, the way the gauntlet interface calls
it once per round.

---

## 4. Contract-hash interaction

The `tournament` block is part of the **scoring** contract component
(§1.2), so it factors into the contract hash through the **existing**
`_canon_scoring` canonicalizer (`contract.py:236`) with one change:
`scoring_to_canon` already serializes *every public field* of
`ScoringWeights` via `dataclasses.fields`, so once
`ScoringWeights` carries a `tournament_structure` field (§5), it is
folded into the canonical form **automatically**. Recursive structural
normalization plus `json.dumps(sort_keys=True)` handle a nested
`{structure, params}` dict.

The one care point: the `params` mapping must canonicalize
order-independently. `json.dumps(sort_keys=True)` already sorts the
top-level and nested dict keys; the only non-deterministic case is a
list value whose order is semantically irrelevant (e.g.
`swiss.tiebreak`). The data model treats `params` **verbatim** (order
preserved). Order-insensitive params must be canonicalized by the
selection layer that defines them. This is stated here so the two halves
agree.

Consequence (the desired behaviour): **changing the structure or any
param rolls the epoch.** Switching `gauntlet → swiss`, or bumping
`swiss.rounds` from 4 to 6, changes `_canon_scoring`'s output, changes
the contract hash, and `evolve`'s auto-roll path
(`orchestrator.py:258`) closes the current epoch and opens a fresh one —
as it does for a `promote_margin` retune. The roll message
(`orchestrator.py:306`) already names the changed component as
`scoring`; no new component label is needed (the structure *is*
scoring). This is correct: a gauntlet champion and a Swiss champion are
not comparable, so they must live in different epochs.

`compute_component_hashes` (`contract.py:351`) needs no change — the
`scoring` component already covers it.

---

## 5. CLI / `RuntimeConfig` surface

### 5.1 The contract knob lives in `scoring.json`

Because the structure is a **frozen contract component**, the primary
way to set it is by editing `scoring.json` (the same way an operator
retunes `promote_margin`) and re-running `evolve` — auto-epoching rolls
the epoch. This is consistent with how every other contract knob is
set: there is no `--promote-margin` flag either.

### 5.2 `zicato evolve --tournament-structure` (convenience, contract-affecting)

For ergonomics, add **one advisory flag** to `evolve` that *writes the
structure into the contract before the hash is computed*:

```
zicato evolve --tournament-structure swiss [--tournament-param rounds=6] ...
```

- `--tournament-structure {gauntlet|single_elim|double_elim|swiss|racing}`
  — when present, the orchestrator writes `{"structure": <v>, "params":
  ...}` into the **live** `scoring.json` (the contract source) *before*
  `resolve_contract_inputs` runs, so it participates in the contract
  hash and triggers an auto-roll if it differs from the current epoch.
  Default: unset ⇒ read whatever `scoring.json` says ⇒ gauntlet.
- `--tournament-param KEY=VALUE` (repeatable) — sets one `params` key.
  Values are parsed as JSON-if-possible, else string.

This flag is a contract-mutating convenience rather than a
per-invocation runtime toggle; it is equivalent to editing
`scoring.json` by hand, and the help text says so. `zicato --help` is
the authoritative description of the flag, and
[`CLI.md`](CLI.md) is generated from it.

### 5.3 `RuntimeConfig` — no structural change

`RuntimeConfig` (`core/types.py:1775`) is the *runtime-side* binding:
workspace, the two `call_llm`s, `parallelism`, `seed`. The tournament
structure is a **contract** property rather than a runtime one, and it
lives on `EpochConfig.scoring` (the frozen `ScoringWeights`), which the
runner already receives. `RuntimeConfig` therefore gains **nothing**.
The runner reads `weights.tournament_structure` off the
`ScoringWeights` it is handed, and `_weights_spec` (`runner.py:581`)
carries the field so the subprocess worker sees it as well. Selection
itself happens in the orchestrator rather than the per-run worker.

---

## 6. The console rendering

The console is served from `src/zicato/dashboard/static/js/`. Its
match-ups view (`views/gens.js`) renders a "champion defends" banner and
a wrapping grid of one-challenger match cards for the gauntlet, and
branches on the configured structure for the others.

### 6.1 Data layer — `data.js`

One client method in `static/js/data.js` (alongside
`bracket()` / `gate()` at `data.js:77,125`):
```js
export function tournamentStructure(epochId, tournamentId) {
  return cachedJson(`/api/tournament-structure/${enc(epochId)}/${enc(tournamentId)}`);
}
// epoch() already returns the new `tournament` block (§3.1) — no new call needed.
```
The live-invalidation set in `invalidate()` (`data.js:39`) adds the
`/api/tournament-structure/` prefix so the structure refreshes as a
tournament runs.

### 6.2 The match-ups view — `views/gens.js`

`render` (`gens.js:28`) reads `ep.tournament.structure` (default
`"gauntlet"`) and branches:

- **`gauntlet`** — the champion-defends banner, the match-card grid and
  the roster table. This is the default path and must not regress.
- **`single_elim` / `double_elim`** — render a **bracket** from
  `rounds[]`: columns = rounds, nodes = matches (`competitors`, `winner`,
  `bracket_slot`), connector lines winners→next round. A losers' bracket
  renders as a second band for `double_elim`. Each match node links to
  the per-match candidate detail (`ctx.navigate('candidate', ...)`) and
  shows the gate's decisive driver via the existing per-match
  `D.gate(epoch, a, b)` call (the gate endpoint is per-match, §3.3).
- **`swiss`** — render a **standings table** (from `standings[]`) as the
  hero, plus a per-round pairings list (from `rounds[]`). Reuses the
  existing roster-table styling (`dn-board-table`).
- **`racing`** — render a **rung ladder**: one column per rung, each
  showing the surviving field and the `cut[]` from `rounds[].cut`, with
  eliminated candidates struck through. `board_fraction` is shown per
  rung so the operator sees the budget escalation.

The new render branches reuse `svg.js` primitives
(`sparkbar`, `genDots`) and the `ui.js` `verdictPill` / `section`
helpers; no new dependency. The view is gated by the existing
`gatedSwap(host, digest, ...)` pattern — the `digest` includes
`structure` + the settled `rounds` so the pane re-renders only on a real
change.

### 6.3 The epoch view — `views/epoch.js`

`epoch.js` (`epoch.js:24`) reads the new `ep.tournament` block (§3.1) and
adds a one-line **structure pill** to the epoch header
("Structure · Swiss (4 rounds)"). Its "slim reel" of rounds
(`epoch.js:52`) stays as-is for a gauntlet; for a non-gauntlet structure
the reel's stations are the structure's rounds (already round-ordered),
so the existing reel logic needs only to pull `rounds` from the new
`/api/tournament-structure` response instead of inferring them from
`matchups`.

### 6.4 Boards / candidate views — unchanged

`views/boards.js`, `views/board.js`, `views/candidate.js` are per-entry /
per-candidate and structure-agnostic — they read `/api/.../per-entry` and
`/api/.../per-judge`, which do not change. No edits.

---

## 7. Implementation plan — the files each part touches

### 7.1 Data model / config / contract

| File | Change |
|---|---|
| `src/zicato/core/types.py:1485` (`ScoringWeights`) | Add a `tournament_structure: TournamentStructure` field with a `_default_tournament_structure()` factory ⇒ `{"gauntlet", {}}`. Add a frozen `TournamentStructure` dataclass `{ structure: str, params: Mapping[str, Any] }` and a `MatchOutcome` dataclass (§2.6). Add to `__all__`. |
| `src/zicato/core/types.py:1334` (`OutcomeRecord`) | Add `structure`, `final_rank`, `eliminated_in_round`, `match_record` fields, all with back-compat defaults (§2.6). |
| `src/zicato/epoch/lifecycle.py:140` (`_scoring_from_dict`) + the workspace loader `_scoring_weights_from_dict` (referenced at `contract.py:259`) | Parse the `tournament` block into `TournamentStructure`; default to gauntlet; validate the `structure` token. |
| `src/zicato/epoch/lifecycle.py:183` (`_scoring_to_dict`) | Serialize `tournament_structure` back into the `tournament` key. |
| `src/zicato/epoch/contract.py:266` (`_scoring_to_canon`) | No change needed if the field is a plain dataclass field (it is folded automatically); add a unit test asserting a structure change moves the hash. |

### 7.2 Persistence — runtime + settled record

| File | Change |
|---|---|
| `src/zicato/runtime/state.py:450` (`ActiveTournament`) | Add `structure`, `structure_params`, `competitors`, `rounds`, `standings` fields + `to_dict` / `from_dict` with back-compat defaults (§2.2). |
| `src/zicato/runtime/state.py:305` (`ActiveTournamentEntry`) | Add `match_id` field (default `""`); document the widened `side` domain (§2.3). |
| `src/zicato/runtime/state.py:562` (`update_tournament_entry`) | No signature change; `side` value domain widens. Add helpers `update_tournament_round` / `update_standings` for the structure fields. |
| `src/zicato/epoch/journal.py:224` (`_outcome_from_dict`) + `write_experiment` (`journal.py:308`) | Read/write the new `OutcomeRecord` fields. |
| `src/zicato/index/schema.py:137` (`tournaments` DDL) + `_V2_ADDED_COLUMNS` (`schema.py:194`, becomes a v3 add) | Add the five TEXT columns (§2.6a); bump `PRAGMA user_version` to 3. |
| `src/zicato/index/ingest.py:107` | Populate the new columns from the resolved `OutcomeRecord` + the tournament record. |

### 7.3 Dashboard API

| File | Change |
|---|---|
| `src/zicato/dashboard/state_reader.py:893` (`build_epoch_view`) | Add the `tournament` block from frozen `scoring.json` (§3.1). |
| `src/zicato/dashboard/state_reader.py:1400` (`build_bracket`) | Add top-level `structure` / `structure_params` + the `tournaments` array (§3.1). |
| `src/zicato/dashboard/state_reader.py:245` (`_normalize_tournament_statuses`) | Stop assuming `side ∈ {"parent","child"}`; pass through an opaque competitor `side` (§3.1). |
| `src/zicato/dashboard/state_reader.py` (new `build_tournament_structure`) | The §3.2 reader, with the index→active→loss-files fallback chain. |
| `src/zicato/dashboard/endpoints.py:78` (`make_endpoints`) | Add `api_tournament_structure` handler + register it in the returned dict (`endpoints.py:831`). |
| `src/zicato/dashboard/server.py` | Add the `/api/tournament-structure/{epoch_id}/{tournament_id}` route. |

### 7.4 Console

| File | Change |
|---|---|
| `src/zicato/dashboard/static/js/data.js` | Add `tournamentStructure()`; add the prefix to `invalidate()`. |
| `src/zicato/dashboard/static/js/views/gens.js` | Branch on `ep.tournament.structure`; keep gauntlet path unchanged; add bracket / standings / racing-ladder renderers (§6.2). |
| `src/zicato/dashboard/static/js/views/epoch.js` | Add the structure pill; pull the reel's rounds from the structure response for non-gauntlet (§6.3). |
| `src/zicato/dashboard/static/js/svg.js` | (Optional) a small `bracketLines` SVG helper for the elim renderers; reuse existing primitives otherwise. |

### 7.5 CLI

| File | Change |
|---|---|
| `src/zicato/cli/commands/evolve.py:408` | Add `--tournament-structure` + repeatable `--tournament-param KEY=VALUE` Click options; write them into the live `scoring.json` before contract resolution (§5.2). |

### 7.6 What does NOT change

`storage/base.py`, `storage/files.py`, `storage/memory.py`,
`storage/_atomic.py` — the seam is structure-agnostic; the new fields are
just more JSON. `RuntimeConfig` — the structure is a contract property
rather than a runtime one (§5.3). The per-run subprocess worker
(`_tournament_worker.py`) — it runs ONE board entry under ONE generation;
which competitors are paired is the orchestrator's job. The gate
(`tournament/gate.py`) — it is per-match and already
champion-vs-challenger.

---

## 8. Cross-references

| Topic | Document |
|---|---|
| The selection algorithms that drive each structure (pairing, cuts, racing) | [SELECTION.md](SELECTION.md), [TOURNAMENT-STRUCTURES.md](TOURNAMENT-STRUCTURES.md) |
| The operational gauntlet view, the bracket, per-matchup analytics | [TOURNAMENT.md](TOURNAMENT.md) |
| The `tournament` config block in the epoch contract + contract-hash roll | [EPOCHS-AND-JOURNALING.md](EPOCHS-AND-JOURNALING.md) §10 |
| The generalized persisted record + storage seams + back-compat | [STORAGE.md](STORAGE.md) §5 |
| The `--tournament-structure` flag | `zicato --help`, and [CLI.md](CLI.md), which is generated from it |
| The scalar each match compares | [SCORING.md](SCORING.md) |
