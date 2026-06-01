# Tournament structures — the `SelectionStrategy` abstraction

> **Status.** Design + implementation plan. **No code in this document is
> shipped.** It specifies a pluggable per-epoch tournament structure: the
> `SelectionStrategy` interface, the five concrete strategies, and the
> exact backend changes the implementation wave must make (with
> `file:line` references against the tree at the time of writing).

This document is the *how-to-build-it* companion to two existing docs:

- [`SELECTION.md §10`](SELECTION.md#10-configurable-per-epoch-tournament-structures)
  — the **decision theory**: why the gauntlet is the default, which
  structure approximates which best-arm / dueling-bandit mechanism, and
  the honest verdict (§8 there) that brackets are noise-fragile for
  zicato's few-expensive-noisy regime.
- [`TOURNAMENT.md §1.4`](TOURNAMENT.md#14-the-gauntlet-is-the-default-not-the-only-structure)
  — the **operational view**: the strategy-driven runner flow and the
  generalised dashboard bracket.

The defining constraint, repeated because it is load-bearing: **the
promote gate is unchanged.** `zicato.tournament.gate.evaluate_gate`
(`src/zicato/tournament/gate.py:168`) remains the per-duel accept/reject
test. The `SelectionStrategy` owns *scheduling + bracket bookkeeping +
champion-advance + intra-tournament stopping*; it never re-decides a
single duel. This keeps the per-task feasibility guarantee
(`SELECTION.md §1.4`) intact for every structure.

---

## 1. Where the strategy plugs into today's loop

The orchestrator round (`evolve_once`,
`src/zicato/orchestrator.py:386`) today hard-wires the gauntlet:

1. Resolve the single champion (`parent_id`) from the
   `current_generation` marker (`orchestrator.py:512`,
   `_resolve_current_generation` at `orchestrator.py:1558`).
2. Ask the proposer for **one** `Experiment`
   (`propose_experiment`, `src/zicato/proposer/proposer.py:79`,
   returning a single `Experiment`).
3. Apply it into **one** child snapshot (`next_id` via
   `_next_generation_id`, `orchestrator.py:1627`).
4. Run **one** paired board run — `run_tournament` (full,
   `src/zicato/tournament/runner.py:1482`) or `run_fast_mode`
   (`runner.py:1606`) — producing one `TournamentResult`
   (`runner.py:202`) whose `.outcome` is the `GateOutcome`.
5. Advance on `promoted`: `append_to_lineage` +
   `_set_current_generation` (`orchestrator.py:836-846`); else record
   the rejected generation (`orchestrator.py:847-858`).

The loop (`evolve_n_rounds`, `orchestrator.py:1014`) calls `evolve_once`
once per round and owns the **inter-round** stopping: `rounds`,
`max_consecutive_rejections` (`orchestrator.py:1263-1275`), the
loop-health breaker (`:1278`), and the wall-clock budget
(`:1169-1182`).

**The seam.** Steps 2–5 of `evolve_once` become *strategy-driven*. The
strategy decides how many challengers to request (step 2), which
duel(s) to run (step 4), and how each `GateOutcome` advances the bracket
(step 5). Steps 1 (resolve champion) and the inter-round stopping in
`evolve_n_rounds` stay **outside** the strategy — the §5 optimal-stopping
rule (`SELECTION.md §10.4`) must apply uniformly across structures, so it
stays at the `evolve_n_rounds` level.

---

## 2. The `SelectionStrategy` interface

A new module `src/zicato/selection/strategy.py` (new package
`src/zicato/selection/`) defines the ABC. The orchestrator constructs a
strategy per *tournament resolution* (one per evolve round for
`gauntlet`; one spanning the whole bracket for the others) from the
epoch's `tournament` config block (§4).

### 2.1 Value types (strategy-owned, gate-agnostic)

```python
@dataclass(frozen=True, slots=True)
class Contestant:
    """A generation in the field: the champion or a proposed challenger."""
    generation_id: str            # "v3", or a freshly-minted child id
    role: Literal["champion", "challenger"]
    snapshot_root: Path | None    # None until the experiment is applied
    experiment: Experiment | None # None for the champion (already on disk)

@dataclass(frozen=True, slots=True)
class Matchup:
    """A single duel the strategy wants run next."""
    matchup_id: str               # stable within the tournament
    left: Contestant              # by convention the incumbent/higher-seed
    right: Contestant
    board_subset: tuple[str, ...] | None  # None = full board; racing uses a slice
    replicates: int               # how many paired runs to average (>=1)

@dataclass(frozen=True, slots=True)
class MatchupResult:
    """A completed duel, handed back to the strategy."""
    matchup_id: str
    left_agg: dict[str, Any]      # aggregate_generation_score output
    right_agg: dict[str, Any]
    outcome: GateOutcome          # from evaluate_gate — UNCHANGED gate
    # outcome.decision is the gate's verdict treating `left` as parent,
    # `right` as child; the strategy interprets it per its own rules.
```

`Experiment`, `GateOutcome`, `aggregate_generation_score` and the
`dict[str, Any]` aggregate shape are reused verbatim from
`zicato.core`, `zicato.tournament.gate`, and
`zicato.tournament.scoring` — **no new gate, no new scoring.**

### 2.2 The ABC

```python
class SelectionStrategy(ABC):
    """Owns scheduling + bracket bookkeeping + champion-advance + stopping
    for ONE epoch's tournament structure. Stateful across matchups within
    a single tournament resolution; constructed fresh per resolution."""

    structure: ClassVar[str]   # "gauntlet" | "single_elim" | ... — the registry key

    @abstractmethod
    def field_size(self) -> int:
        """How many challengers the proposer must emit this round.
        gauntlet → 1; others → tournament.params.field_size."""

    @abstractmethod
    def seed(self, champion: Contestant, challengers: Sequence[Contestant]) -> None:
        """Initialise bracket state from the champion + the applied field.
        Called once, after the orchestrator has applied every challenger's
        patches into a snapshot."""

    @abstractmethod
    def next_matchups(self) -> Sequence[Matchup]:
        """The duel(s) to run next. May return >1 for parallel rounds
        (Swiss round, racing rung). Empty sequence ⇒ nothing schedulable
        right now (the caller then checks `resolved()`)."""

    @abstractmethod
    def record_result(self, result: MatchupResult) -> None:
        """Fold one completed duel's gate verdict into bracket state:
        advance/eliminate/seed. The ONLY place a GateOutcome is interpreted."""

    @abstractmethod
    def resolved(self) -> bool:
        """True once the tournament has a settled winner (no more duels)."""

    @abstractmethod
    def champion(self) -> SelectionDecision:
        """The crowned outcome once resolved(): which generation (if any)
        the orchestrator should promote, plus the audit trail."""

@dataclass(frozen=True, slots=True)
class SelectionDecision:
    promoted_generation_id: str | None  # None ⇒ champion stands
    decision: TournamentDecision        # "promoted" | "rejected" | "deferred"
    reason: str                         # human-readable; mirrors GateOutcome.reason
    matchups: tuple[MatchupResult, ...] # full bracket audit for the journal/dashboard
```

### 2.3 The driver (orchestrator-side, replaces steps 2–5)

```python
async def resolve_tournament(strategy, *, request_field, run_matchup) -> SelectionDecision:
    champion, challengers = await request_field(strategy.field_size())
    strategy.seed(champion, challengers)
    while not strategy.resolved():
        batch = strategy.next_matchups()
        if not batch:
            break
        # batch runs concurrently under the SAME parallelism semaphore
        results = await asyncio.gather(*(run_matchup(m) for m in batch))
        for r in results:
            strategy.record_result(r)
    return strategy.champion()
```

- `request_field(n)` resolves the champion `Contestant` (from
  `current_generation`) and asks the proposer for `n` experiments,
  applying each into a fresh `vN` snapshot (the existing apply/validate
  pipeline, `orchestrator.py` steps 7–9).
- `run_matchup(m)` runs the duel — a thin wrapper over `run_tournament`
  / `run_fast_mode` for a `champion-vs-challenger` pair, plus a new
  `challenger-vs-challenger` path the gate already supports (it just
  compares two aggregates; "parent" = `left`). For `replicates > 1` it
  runs the paired board `replicates` times and averages the per-entry
  losses before `aggregate_generation_score` (this is **§9 lever 1**,
  realised here as a strategy-requested knob).

The crucial property: `run_matchup` always ends in the unchanged
`evaluate_gate`. The strategy reads `MatchupResult.outcome.decision`; it
never re-implements the gate.

---

## 3. The five concrete strategies

Each lives in `src/zicato/selection/strategies/<name>.py`. The
one-line scheduling / advance / stopping summary, then the notes.

### 3.1 `gauntlet` (default — current behaviour, exactly)

- **field_size**: `1`.
- **schedule**: a single `Matchup(champion, the one challenger, full board, replicates)`.
- **advance**: `record_result` stores the one result; `champion()`
  returns `promoted_generation_id = challenger` iff
  `outcome.decision == "promoted"`, else `None` (champion stands).
- **stopping**: `resolved()` is true after the single result lands.

This is a faithful re-expression of today's `evolve_once` steps 2–5 —
the migration target is *byte-for-byte equivalent behaviour* with
`replicates = 1`. Maps to the degenerate single-replicate dueling bandit
(`SELECTION.md §6.3`).

### 3.2 `single_elim`

- **field_size**: `params.field_size` (a power of two after the
  champion's bye, or padded with byes).
- **schedule**: a bracket tree over the *challengers* only; each internal
  node is a `challenger-vs-challenger` `Matchup`. The champion enters as
  the top seed with a bye and meets the bracket survivor in the final
  `champion-vs-survivor` `Matchup`.
- **advance**: a node's winner is the side the gate prefers — for a
  challenger-vs-challenger node there is no incumbent, so the winner is
  the **lower-scalar** side (gate run with `left` as nominal parent;
  `outcome.delta_scalar < 0` ⇒ `right` wins, else `left`). The final
  node uses the real champion-vs-challenger gate: promote iff the
  survivor clears it.
- **stopping**: `resolved()` when one finalist remains and the final
  champion-gate node has a result.
- **noise**: `replicates ≥ 2` is **mandatory** (config default for this
  structure), per `SELECTION.md §2③/§8` — a strong candidate dies to one
  unlucky run otherwise. The structure doc must repeat that verdict.

### 3.3 `double_elim`

- **field_size**: `params.field_size`.
- **schedule**: winners' bracket as `single_elim`; a node-loser drops
  into a losers' bracket; a grand-final `Matchup` pits the winners'
  survivor against the losers' survivor.
- **advance**: same gate-derived per-node winner as `single_elim`; a
  generation is eliminated only on its *second* node loss. Final
  champion-gate decides promotion.
- **stopping**: `resolved()` when the losers' bracket is exhausted and
  the grand final has a result.
- **noise**: `SELECTION.md §8` is explicit that the "second life" is
  delivered more cheaply by replication; this structure is offered for
  completeness, and its config default should *also* set `replicates ≥ 2`
  rather than rely on the losers' bracket for robustness.

### 3.4 `swiss`

- **field_size**: `params.field_size`.
- **schedule**: `params.rounds_n` Swiss rounds; each round pairs
  generations of near-equal standing into duels (champion participates as
  a contestant). Standing = Copeland score (duels won), tie-broken by
  mean scalar.
- **advance**: each duel updates both sides' Copeland score from
  `outcome.delta_scalar`'s sign; no elimination.
- **stopping**: `resolved()` after `rounds_n` Swiss rounds; `champion()`
  promotes the top-standing generation **iff** it clears the
  champion-gate against the incumbent (so a Swiss winner that does not
  actually beat the reigning champion does not get crowned).
- **mapping**: Copeland identification (`SELECTION.md §6.2`); Swiss is
  non-adaptive racing (`SELECTION.md §7`). Per-pairing `replicates ≥ 2`
  is how it earns noise robustness.

### 3.5 `racing` (the endorsed bracket-shaped option)

- **field_size**: `params.field_size`.
- **schedule**: rung 0 duels every challenger against the champion on a
  board **subset** of size `params.rung0_board_size` (or
  `params.board_fraction`); after a rung, eliminate the worst
  `1 − 1/eta` by scalar; survivors re-duel on a larger slice; repeat
  until one survivor or the full board is consumed.
- **advance**: `record_result` accumulates per-rung scalars; elimination
  is by rank within the rung (not the gate) — this is best-arm ID, not a
  feasibility test. The gate is applied only at the **final** rung, on
  the full board, to the last survivor.
- **stopping**: `resolved()` when one survivor remains or the board is
  fully consumed; `champion()` promotes the survivor iff it clears the
  full-board champion-gate (and, recommended, a §9-lever-3 fresh-draw
  confirmation — out of scope for v1 of this feature).
- **mapping**: successive halving / best-arm identification
  (`SELECTION.md §2③`); the adaptive form of Swiss the synthesis
  (`SELECTION.md §9 lever 5`) converges on. Replication is **intrinsic**
  (escalating board slices = escalating sample), which is why this is the
  one bracket-shaped structure `SELECTION.md` endorses for zicato's
  regime.

### 3.6 Degeneracy and the registry

A `STRATEGY_REGISTRY: dict[str, type[SelectionStrategy]]` maps the
`structure` string to its class. Any structure constructed with
`field_size == 1` degrades to `gauntlet` semantics (one challenger, one
full-board duel) rather than erroring — the same graceful degeneracy
fast mode already uses when no champion cache exists
(`SELECTION.md §3.1`). An unknown `structure` string raises at config
load, listing the registry keys.

---

## 4. The shared `tournament` config contract

> **Owned by the data-model agent.** This section states the contract
> *this* design needs; the data-model agent owns the persisted schema,
> the journal/dashboard record shape, and the contract-hash treatment.

The epoch's frozen contract gains a `tournament` block:

```jsonc
// epochs/{id}/tournament.json  (or a "tournament" key in the epoch contract)
{
  "structure": "gauntlet",      // gauntlet | single_elim | double_elim | swiss | racing
  "params": {
    "field_size": 1,            // challengers/round; 1 ⇒ gauntlet degeneracy
    "replicates": 1,            // paired runs per duel, averaged (§9 lever 1)
    // single_elim / double_elim: { } (field_size + replicates suffice)
    // swiss:   { "rounds_n": 4 }
    // racing:  { "eta": 2, "board_fraction": 0.25, "rung0_board_size": null }
  }
}
```

- **Default**: absent block ⇒ `{structure: "gauntlet", params: {field_size: 1, replicates: 1}}`,
  so every existing epoch keeps today's behaviour with no migration.
- **Per-structure params**: `field_size`, `replicates` are universal;
  `swiss` adds `rounds_n`; `racing` adds `eta` + a board-subset schedule
  (`board_fraction` or explicit `rung0_board_size`). The config loader
  validates the params against the chosen `structure`.
- **Where it threads**: alongside board / brief / scoring as a
  per-epoch frozen artifact. It reads naturally onto
  `EpochConfig` (`src/zicato/core/types.py:1605`) as a new
  `tournament: TournamentSpec` field with a defaulting factory (mirrors
  how `scoring: ScoringWeights` is already a frozen sub-object).

### 4.0 Quickstart: configure a structure (operator-facing)

> **Status.** Unlike the rest of this document, this subsection describes
> *shipped* surface — the `tournament` block parses into `ScoringWeights`
> and the CLI flags below exist. For a runnable, no-live-LLM walkthrough
> against a real target, see the presentation example's
> [`RUN.md` → "Running a non-gauntlet tournament"](../../examples/zicato_examples/target_1_presentation/RUN.md)
> and its `scoring.racing.json`, exercised end-to-end by
> `tests/test_example_target_1_racing.py`.

Two equivalent ways to select a non-gauntlet structure for an epoch.

**1. Write the `tournament` block into `scoring.json` (authoritative).**
Add the block alongside the scoring weights, then open/roll the epoch
from that contract (`epoch new --scoring …` or just let `evolve` resolve
it). Example — racing with a four-challenger field:

```jsonc
{
  "promote_margin": 0.01,
  // … the usual scoring weights …
  "tournament": {
    "structure": "racing",        // gauntlet | single_elim | double_elim | swiss | racing
    "params": {
      "field_size": 4,            // challengers proposed per round (gauntlet ⇒ 1)
      "replicates": 2,            // paired runs per duel, averaged (§6 noise lever)
      "eta": 2,                   // racing: keep top 1/eta each rung
      "board_fraction": 0.4,      // racing: rung-0 board slice = ceil(fraction · |board|)
      "rung0_board_size": 0       // racing: 0 ⇒ derive rung-0 size from board_fraction
      // swiss instead adds: "rounds_n": 4
    }
  }
}
```

`field_size` is the universal knob — *how many challengers the proposer
must emit each round*; `gauntlet` fixes it at `1`. The racing strategy
additionally reads the board's entry ids from `params["board_ids"]` to
slice the rungs (the example contract lists them explicitly; see
`src/zicato/selection/strategies/racing.py`).

**2. Set it from `zicato evolve` flags (contract-mutating convenience).**

```bash
zicato evolve \
    --tournament-structure racing \
    --tournament-param field_size=4 \
    --tournament-param eta=2 \
    --tournament-param board_fraction=0.4 \
    --tournament-param replicates=2 \
    --harness-call-llm   my_pkg.llms:harness \
    --auxiliary-call-llm my_pkg.llms:aux \
    --rounds 2
```

`--tournament-structure` writes `{structure, params}` into the live
`scoring.json` *before* the contract hash is computed, so it participates
in the hash exactly like a hand edit. Each `--tournament-param KEY=VALUE`
is repeatable; `VALUE` is parsed as JSON when possible (so `field_size=4`
is the integer `4`), else taken as a string. Params are only applied when
`--tournament-structure` is also passed. (See `docs/design/CLI.md` for
the flag reference — advisory; trust `zicato evolve --help`.)

**Either way, changing the structure rolls the epoch.** Because the
`tournament` block is part of the frozen evaluation contract (§4.1), a
structure or param change is a contract-hash change: the next `evolve`
closes the current epoch and opens a fresh one, exactly as retuning
`promote_margin` does. A gauntlet champion and a racing champion are
selected under different rules and are not directly comparable, which is
precisely why the structure rolls the contract.

### 4.1 Is `tournament` part of the contract hash?

**Recommended: yes** — the structure changes *what a promotion means*, so
generations selected under different structures are not directly
comparable, exactly the rationale for the other contract components
(`src/zicato/epoch/contract.py:1-26`). Concretely: add a sixth canonical
component to `compute_contract_hash` (`contract.py:319`) and a
`"tournament"` key to `compute_component_hashes` (`contract.py:351`),
canonicalised the way `_canon_scoring` (`contract.py:236`) routes
`scoring.json` through `ScoringWeights` — round floats, sort keys,
serialize the fully-defaulted `TournamentSpec`. This makes changing the
structure auto-roll the epoch. **The data-model agent owns this
decision** — flagged here as the interface I depend on (§7).

---

## 5. Backend implementation plan (exact files)

Ordered so each step is independently testable. **No gate/scoring
changes** except the optional replication averaging, which is additive.

1. **New package `src/zicato/selection/`**
   - `strategy.py` — the ABC, `Contestant` / `Matchup` / `MatchupResult`
     / `SelectionDecision` value types (§2), and `resolve_tournament`
     driver (§2.3).
   - `registry.py` — `STRATEGY_REGISTRY` + `make_strategy(spec)` (§3.6).
   - `strategies/{gauntlet,single_elim,double_elim,swiss,racing}.py` —
     the five concrete classes (§3).
   - `__init__.py` — re-exports `SelectionStrategy`, `make_strategy`.

2. **`src/zicato/core/types.py`** — add `TournamentSpec`
   (frozen dataclass: `structure: str`, `params: Mapping[str, Any]`,
   with a defaulting helper `TournamentSpec.gauntlet()`), and add the
   `tournament: TournamentSpec` field to `EpochConfig`
   (`types.py:1605`, default-factory to the gauntlet spec so existing
   call sites and on-disk epochs stay valid). **Coordinate the exact
   dataclass shape with the data-model agent.**

3. **`src/zicato/tournament/runner.py`** — add two thin entry points
   beside `run_tournament` (`runner.py:1482`):
   - `run_matchup(...)` — runs one `Matchup` (champion-vs-challenger
     *or* challenger-vs-challenger; the gate already only needs two
     aggregates), honouring `board_subset` and `replicates`. Internally
     reuses `_run_board_units_full` (`runner.py:1268`) /
     `_run_board_units_fast` (`runner.py:1352`) and
     `aggregate_generation_score` (`runner.py:1580`), then the unchanged
     `_gate_with_regression` (`runner.py:1421`) → `evaluate_gate`.
   - `_run_replicated(...)` — for `replicates > 1`, run the paired board
     N times and average per-entry losses before aggregation (§9 lever
     1). Additive; `replicates == 1` is the current path.

4. **`src/zicato/orchestrator.py`** — refactor `evolve_once`
   (`orchestrator.py:386`):
   - Resolve champion as today (`:512`), then build the strategy via
     `make_strategy(epoch.tournament)`.
   - Replace steps 2–5 (propose one → apply one → `run_tournament` →
     advance, currently `:757-858`) with the §2.3 `resolve_tournament`
     driver: `request_field` wraps the existing
     propose+apply+validate pipeline (now called `field_size()` times);
     `run_matchup` wraps the new runner entry point.
   - The advance block (`:836-858`) now keys off
     `SelectionDecision.decision` / `.promoted_generation_id` instead of
     a single `TournamentResult.outcome`. Lineage / `current_generation`
     update logic is otherwise unchanged; **every** challenger in the
     field is recorded in lineage (promoted survivor on the spine, the
     rest as rejected/eliminated, mirroring `:847-858`).
   - `EvolveRoundOutcome` (`orchestrator.py:69`) gains the bracket audit
     (`SelectionDecision.matchups`) so the journal/dashboard can render
     the non-gauntlet shapes. **Record shape owned by the data-model
     agent.**
   - `evolve_n_rounds` (`orchestrator.py:1014`) is **unchanged** — the
     §5 inter-round stopping (`:1263-1294`) stays outside the strategy.

5. **`src/zicato/workspace_loader.py`** — add `load_current_tournament`
   beside `load_current_scoring` (`workspace_loader.py:129`), returning
   the epoch's frozen `TournamentSpec` (defaulting to gauntlet for epochs
   that predate the field).

6. **`src/zicato/epoch/contract.py`** — *if* the data-model agent agrees
   `tournament` is contract (§4.1): add `_canon_tournament`, extend
   `compute_contract_hash` (`contract.py:319`) and
   `compute_component_hashes` (`contract.py:351`), and add the path to
   `ContractInputs` (`contract.py:45`) + `resolve_contract_inputs`
   (`contract.py:371`).

7. **`src/zicato/epoch/lifecycle.py`** — thread `TournamentSpec` through
   epoch creation/serialization (it persists the frozen contract;
   `EpochConfig` write/read paths gain the new field). **Persisted-file
   shape owned by the data-model agent.**

8. **CLI** — `src/zicato/cli/commands/epoch.py` (epoch new) and
   `evolve.py` gain a `--tournament-structure` / `--field-size` surface
   (optional; the config block is authoritative). Out of scope for the
   first wave if the config block alone is wired.

### 5.1 What must persist

Per *tournament resolution* (per round), the strategy's settled state
must persist so the dashboard bracket (`TOURNAMENT.md §2`) and the
journal can render it:

- the `structure` + `params` actually used;
- every `MatchupResult` (the bracket audit — `left`/`right` generation
  ids, both aggregates, the `GateOutcome`, the rung/round it belonged
  to);
- the `SelectionDecision` (crowned generation, decision, reason).

For `gauntlet` this collapses to exactly today's single-`TournamentResult`
record, so the existing journal/index shape is the `field_size == 1`
special case. **The concrete persisted-record schema, the analytical-index
table changes, and the dashboard rendering are the data-model agent's
deliverable** (§7).

---

## 6. Composition with the gate, replication, and §5 stopping

- **Gate**: untouched. Every `Matchup` ends in `evaluate_gate`
  (`gate.py:168`). A challenger-vs-challenger duel feeds the two
  challenger aggregates in as `(parent, child)`; the strategy reads the
  sign of `delta_scalar` and never the feasibility rules for *ranking*
  (Rules 2/3 still fire, and a structure may choose to treat a
  feasibility-failing node-winner as eliminated — that is a strategy
  policy, not a gate change). The *final* champion-gate is the real,
  full three-rule test.
- **Replication (§9 lever 1)**: surfaced as `Matchup.replicates`, applied
  in `_run_replicated`. The gauntlet keeps `replicates = 1`; the bracket
  structures default to `≥ 2` because `SELECTION.md §8` makes replication,
  not bracket shape, the noise lever.
- **§5 optimal-stopping**: stays in `evolve_n_rounds`
  (`orchestrator.py:1263-1294`), outside the strategy. The strategy
  resolves the *intra-tournament* bracket; `evolve_n_rounds` decides
  whether the *next* round's field is worth the cost `c`
  (`SELECTION.md §10.4`). For `gauntlet` the two coincide (one duel per
  round).

---

## 7. Interface required FROM the data-model agent

This design depends on the data-model agent for the following shared
contract; everything else above is owned here.

1. **The `tournament` config block schema** — the on-disk shape of
   `{ structure, params }`, the per-structure `params` validation, and
   the `TournamentSpec` dataclass on `EpochConfig`
   (`src/zicato/core/types.py:1605`). This design assumes the §4 shape;
   the data-model agent owns the authoritative schema and its
   serialization in `epoch/lifecycle.py`.
2. **Whether `tournament` is part of the contract hash** (§4.1). This
   design recommends *yes* (a structure change rolls the epoch) and
   names the exact `contract.py` insertion points; the data-model agent
   makes the call and owns the canonicalization.
3. **The persisted bracket-record shape** (§5.1) — the journal entry and
   the analytical-index table(s) that store every `MatchupResult` + the
   `SelectionDecision`, generalising today's single-`TournamentResult`
   record. The dashboard bracket (`TOURNAMENT.md §2`) reads from this.
4. **The dashboard rendering** of non-gauntlet shapes (single-elim tree,
   Swiss standings, racing rung-ladder) — owned by the data-model /
   dashboard work; this design only guarantees the audit data (§5.1) is
   available.

In return, this design guarantees the data-model agent a stable
producer-side contract: every structure emits a `SelectionDecision` plus
a flat list of `MatchupResult`s, and `gauntlet` emits exactly one
`MatchupResult` so the existing single-matchup record is the
backwards-compatible special case.

---

## 8. Cross-references

| Topic | Document |
|---|---|
| Why the gauntlet is the default; per-structure decision theory; §8 anti-bracket verdict | [`SELECTION.md §10`](SELECTION.md#10-configurable-per-epoch-tournament-structures), [`SELECTION.md §8`](SELECTION.md#8-why-not-double-elimination-or-swiss-the-explicit-verdict) |
| The strategy-driven runner flow; generalised dashboard bracket | [`TOURNAMENT.md §1.4`](TOURNAMENT.md#14-the-gauntlet-is-the-default-not-the-only-structure) |
| The promote gate every structure consumes unchanged | [`SCORING.md §5`](SCORING.md#5-the-tournament-promotion-gate), `src/zicato/tournament/gate.py` |
| Replication (lever 1), multi-candidate field (lever 0), confirmation (lever 3) | [`SELECTION.md §9`](SELECTION.md#9-the-recommended-design) |
| The epoch as the frozen contract; auto-roll on contract change | [`EPOCHS-AND-JOURNALING.md`](EPOCHS-AND-JOURNALING.md), `src/zicato/epoch/contract.py` |
