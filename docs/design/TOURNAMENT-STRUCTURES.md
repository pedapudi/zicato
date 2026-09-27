# Tournament structures — the `SelectionStrategy` abstraction

> **Status.** The `SelectionStrategy` interface, all five concrete
> strategies, the registries, the `tournament` config block, and the
> `--tournament-structure` / `--tournament-param` CLI surface are in the
> tree and exercised by the test suite. The persisted type is
> `TournamentStructure` (`src/zicato/core/tournament.py`), held on
> `ScoringWeights.tournament_structure`, and the loader validates the
> token against `VALID_TOURNAMENT_STRUCTURES`. Operator-facing
> configuration is in §4.0 and in the
> `zicato-design-tournament-structure` skill.

This document is the reference companion to two others:

- [`SELECTION.md §10`](SELECTION.md#10-configurable-per-epoch-tournament-structures)
  — the **decision theory**: why racing is the default, which structure
  approximates which best-arm / dueling-bandit mechanism, and the
  verdict in its §8 that the elimination and Swiss structures stay
  experimental for zicato's regime of few, expensive, noisy
  measurements.
- [`TOURNAMENT.md §1.4`](TOURNAMENT.md#14-five-structures-racing-is-the-default)
  — the **operational view**: the strategy-driven runner flow and the
  generalised dashboard bracket.

The defining constraint is that **the promote gate is unchanged.**
`zicato.tournament.gate.evaluate_gate` remains the per-duel
accept/reject test. The `SelectionStrategy` owns *scheduling + bracket
bookkeeping + champion-advance + intra-tournament stopping*; it never
re-decides a single duel. This keeps the per-task feasibility guarantee
(`SELECTION.md` §1, property 4) intact for every structure.

---

## 1. Where the strategy plugs into the round

Every structure, the gauntlet included, runs through one round pipeline
(`evolve_once` in `src/zicato/evolve/round_entry.py`, whose phases live
in `src/zicato/evolve/field.py`):

1. **Prepare.** Resolve the epoch's frozen contract, the champion (the
   baseline `v0`, or the primary promotion of the latest committed
   round settlement), and the strategy (`make_strategy`, with the
   epoch's resolved replicate count and noise floor).
2. **Propose and apply** (`evolve/field_candidates.py`). Ask the
   proposer for `strategy.field_size()` experiments and apply each into
   a fresh child snapshot. A field where no candidate applied ends the
   round.
3. **Run** (`evolve/field_execution.py`). Drive the strategy with the
   selection driver (`evaluate_tournament` in
   `src/zicato/selection/driver.py`, §2.3): each scheduled `Matchup` runs
   through `run_matchup` (`src/zicato/tournament/runner.py`), which ends
   in `evaluate_gate`. When the contract sets
   `params["promote_confidence_threshold"]`, the driver then confirms a
   crowning promotion with the evidence gate.
4. **Gate** (`resolve_field_verdict` in `evolve/gate.py`). Confirm the
   crowning duel on the holdout slice, apply operator overrides, and run
   the optional integrity checks.
5. **Decide** (`settle_field_round` in `evolve/settlement.py`). Build one
   `OutcomeRecord` per challenger and commit the round's settlement
   record, which advances the champion.

The loop (`evolve_n_rounds`, `src/zicato/evolve/loop.py`) calls
`evolve_once` once per round and owns the **inter-round** stopping:
`rounds`, `max_consecutive_rejections`, the loop-health breaker, and
the invocation wall-clock budget.

**The seam.** Steps 2 to 5 are strategy-driven. The strategy decides how
many challengers to request (step 2), which duels to run (step 3), and
how each `GateOutcome` advances the bracket (step 3). Resolving the
champion (step 1) and the inter-round stopping in `evolve_n_rounds` stay
**outside** the strategy, because an optimal-stopping rule
(`SELECTION.md §10.4`) would apply uniformly across structures.

---

## 2. The `SelectionStrategy` interface

`src/zicato/selection/strategy.py` defines the abstract base class. The
round pipeline constructs a fresh strategy per *tournament resolution*
(one per evolve round, for every structure) from the epoch's
`tournament` config block (§4).

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
    board_subset: tuple[str, ...] | None = None  # None = full board; racing slices
    replicates: int = 1           # paired runs averaged before scoring (>=1);
                                  # every strategy fills in its resolved count
                                  # (default 2; racing declares 1, §3)
    stage_index: int = 0          # bracket round / swiss round / racing rung
    bracket_slot: str = ""        # e.g. "WB-R1-0"; empty for non-bracket structures
    matchup_budget_seconds: float | None = None  # opt-in per-duel wall-clock cap
                                  # (racing's grind guard, §3.5); None = uncapped,
                                  # enforced by the worker's per-run cancellation

@dataclass(frozen=True, slots=True)
class MatchupResult:
    """A completed duel, handed back to the strategy."""
    matchup_id: str
    left_id: str                  # the two sides' generation ids (self-describing)
    right_id: str
    left_agg: dict[str, Any]      # aggregate_generation_score output
    right_agg: dict[str, Any]
    outcome: GateOutcome          # from evaluate_gate — UNCHANGED gate
    stage_index: int = 0
    bracket_slot: str = ""
    measurement_draw: MeasurementDraw | None = None  # set for evidence-confirmation duels
    # outcome.decision is the gate's verdict treating `left` as parent,
    # `right` as child; the strategy interprets it per its own rules.
    # `lower_scalar_id()` reads the sign of outcome.delta_scalar (= right-left)
    # to name the winner of a challenger-vs-challenger node.
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
    matchups: tuple[MatchupResult, ...] = ()  # full bracket audit (journal/dashboard)
    crowning_matchup_id: str = ""       # the duel that decided promotion
    standings: tuple[Standing, ...] = () # final best-first ranking (two rows for gauntlet)
```

The shipped strategy ABC also carries `rounds()` (settled per-round records
for the dashboard) and a live in-flight projection (`live_rounds()` /
`live_standings()`, built from the `_pending_round()` / `_live_standings()`
hooks) so the dashboard can render a tournament WHILE it runs. `Standing` and
`RoundRecord` / `MatchRecord` are the dashboard-shaped record types; see
`src/zicato/selection/strategy.py`.

### 2.3 The driver (`src/zicato/selection/driver.py`)

```python
async def evaluate_tournament(strategy, *, request_field, run_matchup,
                              on_progress=None, pre_gate=None,
                              replicate_duel=None, on_inconclusive=None):
    champion, challengers = await request_field(strategy.field_size())
    strategy.seed(champion, list(challengers))
    while not strategy.resolved():
        batch = strategy.next_matchups()
        if not batch:
            break
        if on_progress is not None:
            on_progress(strategy)          # publish the live structure
        results = await gather_owned(*(run_matchup(m) for m in batch))
        for r in results:
            strategy.record_result(r)
    decision = strategy.champion()
    if pre_gate is None:
        return TournamentEvaluation(decision)
    return TournamentEvaluation(*await confirm_promotion_with_evidence(...))
```

`resolve_tournament` is the same drive returning only the decision.

- `request_field(n)` returns the champion `Contestant` and the applied
  challenger field (the round's candidate field, built in step 2 of §1).
- `run_matchup(m)` runs the duel through `run_matchup` in
  `src/zicato/tournament/runner.py`, for a champion-vs-challenger or a
  challenger-vs-challenger pair (the gate compares two aggregates;
  "parent" = `left`). It honours `board_subset`, and for
  `replicates > 1` runs the paired board `replicates` times and averages
  the per-entry losses before `aggregate_generation_score`
  (`_run_replicated` in `src/zicato/tournament/scheduling.py`).
- `pre_gate` and `replicate_duel` implement the evidence gate: when the
  strategy crowns a promotion, the driver holds it until a
  Bradley–Terry fit over fresh confirmation duels of the crowning pair
  supports it, spending up to `promote_confidence_replicates` extra
  duels (`src/zicato/selection/evidence_gate.py`).

The crucial property: `run_matchup` always ends in the unchanged
`evaluate_gate`. The strategy reads `MatchupResult.outcome.decision`; it
never re-implements the gate.

---

## 3. The five concrete strategies

`gauntlet` and `racing` live in `src/zicato/selection/strategies/<name>.py`.
`single_elim`, `double_elim` and `swiss` live in
`src/zicato/selection/experimental/<name>.py` and resolve only when the
contract sets `experimental.tournament_structures` to `true`
(`SELECTION.md §8`). The one-line scheduling / advance / stopping summary,
then the notes.

The four structures that narrow a field of challengers — `single_elim`,
`double_elim`, `swiss`, `racing` — share one base, `ChampionGateStrategy`
(`src/zicato/selection/strategies/champion_gate.py`). It owns the ending
they have in common: a single crowning duel between the reigning champion
and the finalist the structure's own stages produced, and every view built
on that duel — the settled round records, the in-flight round, the live
standings, and the crowned `SelectionDecision`. A structure supplies its
own stage bookkeeping plus four descriptions of its final: the round label
it is recorded under, the contestant that reached it, the bracket slot it
occupies, and the within-tournament stage index it sits at. `gauntlet`
stands outside the base, because its single duel IS the champion gate.
`tests/test_selection_strategies.py` pins the correspondence, so a
structure added to the registry cannot fork those views again.

### Shared scoring defaults and explicit tournament specifications

Bare `ScoringWeights()` and an empty authored `scoring.json` select racing
with four candidates, halving factor two, an initial board fraction of 0.4,
and two ordinary draws per matchup. Candidate screening uses two entries.
Confirmation permits 32 fresh draws of the selected pair at threshold 0.8.
The scoring field's default factory (`_default_tournament_structure` in
`src/zicato/core/tournament.py`) defines the complete specification.

`zicato init` writes `{}`. `zicato inspect config --scaffold --complete`
displays all effective settings, and epoch records retain the complete
selected values.

| Authored tournament value | Effective specification |
|---|---|
| Omitted, `{}`, or `{"structure": "racing"}` | Complete default racing parameters |
| `{"params": {}}` | Racing with explicit empty parameters; confirmation disabled |
| `{"structure": "gauntlet"}` | Gauntlet with empty parameters; confirmation disabled |
| A supplied parameters object | Supplied values retained; omitted structure selects racing |

An authored confirmation threshold with no budget receives the shared
budget of 32. An explicit screening count of zero disables screening. An
explicit confirmation threshold of null or zero disables confirmation;
budget zero leaves a configured requirement incomplete.

The following table describes fallback behavior for explicit partial
tournament specifications. These fallbacks do not replace the complete
shared scoring default.

> **Param defaults at a glance** (read off the shipped strategy
> constructors — these are the authoritative defaults the
> `zicato-design-tournament-structure` skill tabulates):
>
> | structure | `field_size` | `replicates` | extra |
> |---|---|---|---|
> | `gauntlet` | `1` (fixed; the key is refused) | `2` | — |
> | `single_elim` | `2` | `2` | — |
> | `double_elim` | `2` | `2` | — |
> | `swiss` | `2` | `2` | `rounds_n=4` |
> | `racing` | `2` | `1` | `eta=2`, `board_fraction=0.25`, `rung0_board_size=0`, `slice_schedule="prefix"`, `matchup_budget_seconds`/`final_rung_budget_seconds` (opt-in) |
>
> Every structure also accepts `promote_confidence_threshold` and
> `promote_confidence_replicates` (the evidence gate), and a strategy
> refuses any key it does not declare.
>
> These `replicates` values are defaults rather than floors: an operator
> may set any value at or above `1`. The base default is `2`, because a
> duel decided by one paired run is decided by one noise draw. `racing`
> declares `1` because it replicates intrinsically through escalating board
> slices. A deterministic contract pins `"replicates": 1` so a duel is a
> single run. When the contract pins no `replicates` and the epoch carries
> a measured noise floor, the count in effect is sized from the floor
> against `promote_margin` and never falls below the default
> (`src/zicato/selection/replicates.py`;
> [SELECTION.md §9.1](SELECTION.md#91-the-measured-noise-floor-sizes-the-replicate-count-and-the-racing-cuts)).
>
> Each strategy declares its own `_default_replicates` class variable, and
> `default_replicates_for(structure)` in `src/zicato/selection/registry.py`
> reads it — the **single source of truth** for "the default replicates a
> structure runs when `params["replicates"]` is unset". A strategy
> resolves its own default in `__init__` against the same class
> variable, so the two can never disagree. The contract cost estimator
> reads the constructed strategy's count rather than assuming a flat
> `1`, so the cost meter matches the schedule a structure actually runs
> (gauntlet / swiss / single-elim / double-elim default to `2`, racing to
> `1`) — see §4.0 and [the contract cost estimator](../dev-guide/10-cli-and-configuration.md#103-estimating-evaluation-cost).

### 3.1 `gauntlet`

- **field_size**: `1`.
- **schedule**: a single `Matchup(champion, the one challenger, full board, replicates)`.
- **advance**: `record_result` stores the one result; `champion()`
  returns `promoted_generation_id = challenger` iff
  `outcome.decision == "promoted"`, else `None` (champion stands).
- **stopping**: `resolved()` is true after the single result lands.

It maps to the degenerate single-replicate dueling bandit
(`SELECTION.md §6.3`).

- **noise under `--mode fast`** (the `evolve` default): `run_matchup`
  under `fast=True` runs the challenger board `replicates` times and
  folds the per-entry losses, then compares the fold against the
  champion's **cached measurements** rather than drawing the champion
  again. So `replicates` reduces challenger-side noise
  only, and the contrast keeps one unreplicated side. Two consequences an
  operator has to price in: repeated *rounds* are not repeated *draws* of
  the contrast (the champion side is the same numbers every round, so
  round-to-round variation understates the true variance), and the
  contract-level power check's two-sample `sqrt(2/(k·n))` is therefore
  optimistic under this mode. `--mode full` re-samples both sides;
  independent draws of the whole contrast need separate runs and seeds.
  The branch logs the asymmetry whenever `replicates > 1` meets the fast
  path, so it is never silent.

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
- **noise**: `replicates ≥ 2` is the **config default** for this structure
  (an operator may override to `1`), per the single-elimination bracket
  family of `SELECTION.md` §2 and its §8 verdict — a strong
  candidate dies to one unlucky run otherwise.

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
- **noise**: `SELECTION.md §8` states that the "second life" is delivered
  more cheaply by replication. This structure is offered for completeness,
  and its config default also sets `replicates ≥ 2` rather than relying on
  the losers' bracket for robustness.
- **simplification**: the losers' bracket runs as a plain
  single-elimination over the accumulated winners'-bracket losers once the
  winners' bracket has a survivor, rather than as a fully seeded feed
  schedule between the two brackets. Every generation still gets exactly one
  second life, being eliminated on its second node loss; the grand final
  still pits the two survivors; and the crowning champion-gate is
  unchanged. See the module docstring in
  `src/zicato/selection/experimental/double_elim.py`.

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
  `params.slice_schedule` controls how that nested subset is ordered.
  `"prefix"` — the default everywhere, including new workspace scaffolds —
  takes the slice from the authored JSONL order, so an entry's row position
  decides whether it gets to eliminate a challenger.
  `"shuffled_v1"` (**opt-in**) instead derives a deterministic permutation
  from the sorted entry ids alone (SHA-256, never a process-global seed) and
  takes nested prefixes of it, so authored order cannot decide an early cut
  while resume and audit stay reproducible: the same board always yields the
  same permutation. The permutation is uniform over entries. It does not
  balance slices by tag or by `weight`, so a small slice can still
  under-represent a heavily weighted entry class.
- **advance**: `record_result` accumulates per-rung scalars, and
  elimination is by rank within the rung rather than by the gate, because a
  rung identifies the best arm rather than testing feasibility. The gate is
  applied only at the **final** rung, on the full board, to the last
  survivor.
- **stopping**: `resolved()` when one survivor remains or the board is
  fully consumed; `champion()` promotes the survivor only if it clears the
  full-board champion-gate. Winner's-curse confirmation on fresh draws
  (`SELECTION.md §9`) runs after the strategy, in the holdout
  confirmation and the evidence gate.
- **mapping**: successive halving and best-arm identification (the
  single-elimination bracket family of `SELECTION.md` §2); the adaptive
  form of Swiss that elitist iterated racing (`SELECTION.md §9`) converges
  on. Replication is **intrinsic**
  (escalating board slices = escalating sample), which is why this is the
  one bracket-shaped structure `SELECTION.md` endorses for zicato's
  regime.
- **rung resolution**: with a measured noise floor on the epoch
  (`params["noise_floor_delta_std"]`, which `make_strategy` injects), a rung
  cuts only what its sample resolves. A candidate whose gap to the cut line
  is below the minimum detectable effect at the rung's entries × replicates
  advances with the survivors, and the next rung's larger slice resolves
  it; a rung that cuts nobody still advances. Without a floor the cut is by
  rank alone
  ([SELECTION.md §9.1](SELECTION.md#91-the-measured-noise-floor-sizes-the-replicate-count-and-the-racing-cuts)).
- **grind guard (opt-in wall-clock budgets)**: two optional params cap a
  duel's total board-unit wall-clock. `matchup_budget_seconds` caps **every**
  duel; `final_rung_budget_seconds` overrides it for the final, full-board
  crowning duel specifically — the rung that runs the whole board ×
  `replicates` × both sides and is the pathological grinder (each board may
  be under its own per-board budget while their sum is unbounded). When
  `final_rung_budget_seconds` is unset the matchup budget applies to the final
  duel too; when **both** are unset no cap applies. The strategy threads
  these onto each scheduled `Matchup`
  (`Matchup.matchup_budget_seconds`). Once a matchup's running
  wall-clock total exceeds the cap, the runner launches no further board
  units and records the unlaunched units as budget-exceeded losses.
  See `src/zicato/selection/strategies/racing.py`.

### 3.6 Degeneracy and the registry

Two registries map the `structure` string to its class:
`STRATEGY_REGISTRY` holds `gauntlet` and `racing`, and
`EXPERIMENTAL_STRATEGY_REGISTRY` holds `single_elim`, `double_elim` and
`swiss`. `make_strategy` resolves an experimental token only when the
contract's `experimental.tournament_structures` flag is `true`; otherwise
it raises, naming the token and that key, as the contract loader, the
contract validator and `zicato evolve --tournament-structure` do. Any structure
constructed with `field_size == 1` degrades to `gauntlet` semantics (one
challenger, one full-board duel) rather than erroring — the same graceful
degeneracy fast mode uses when no champion cache exists
(`SELECTION.md §3.1`). An unknown `structure` string raises at config
load, listing the valid tokens.

---

## 4. The shared `tournament` config contract

> The contract below is what the loader, the strategies, and the contract
> hash implement.

The `tournament` block lives inside `scoring.json` (it
deserializes into `ScoringWeights.tournament_structure`, a frozen
`TournamentStructure` dataclass — `src/zicato/core/tournament.py`), and so folds
into the scoring component of the contract hash automatically. The block:

```jsonc
// scoring.json — alongside the scoring weights
"tournament": {
  "structure": "racing",        // gauntlet | racing; single_elim | double_elim | swiss under the opt-in
  "params": {
    // replicates is universal (defaults are per-structure, §3)
    // gauntlet:                {}
    // single_elim/double_elim: { "field_size": 4, "replicates": 2 }
    // swiss:                   { "field_size": 4, "rounds_n": 4, "replicates": 2 }
    // racing:                  { "field_size": 4, "eta": 2, "board_fraction": 0.4, "replicates": 2 }
  }
}
```

- **Default**: an absent block resolves to the complete racing
  specification of §3 (field size 4, `eta` 2, board fraction 0.4, two
  replicates, and the evidence gate at 0.8 with a budget of 32).
  `params` is stored and round-tripped verbatim as an opaque mapping; the
  data layer enforces that `structure` is one of
  `VALID_TOURNAMENT_STRUCTURES`, that `params` is a mapping, and that
  `replicates` and the two evidence-gate keys lie in range — every other
  key's semantics are owned by the strategy that reads it.
- **Per-structure params**: `replicates` and the evidence-gate keys are
  universal; every structure except the gauntlet adds `field_size`;
  `swiss` adds `rounds_n`; `racing` adds `eta`, a board-subset schedule
  (`board_fraction` or explicit `rung0_board_size`, `board_ids`,
  `slice_schedule`), and the two wall-clock budgets. The loader validates
  the `structure` token and the experimental opt-in; the strategy refuses
  an undeclared key.
- **Where it threads**: the structure is the
  `tournament_structure: TournamentStructure` field of `ScoringWeights`
  (`src/zicato/core/scoring_config.py`), so it serializes through `scoring.json` and
  is part of the scoring contract rather than being a separate
  `EpochConfig` field. Folding it into `ScoringWeights` is what makes a
  structure change roll the epoch through the scoring hash without extra
  plumbing.

### 4.0 Quickstart: configure a structure (operator-facing)

> For a runnable, no-live-LLM walkthrough
> against a real target, see the presentation example's
> [`RUN.md` → "Running a racing tournament"](../../examples/zicato_examples/target_1_presentation/RUN.md#running-a-racing-tournament)
> and its `scoring.racing.json`, exercised end-to-end by
> `tests/test_example_target_1_racing.py`.

Two equivalent ways to select or tune an epoch's structure.

**1. Write the `tournament` block into `scoring.json` (authoritative).**
Add the block alongside the scoring weights, then open/roll the epoch
from that contract (let `evolve` resolve it). Example — racing with a
four-challenger field and the optional racing keys spelled out:

```jsonc
{
  "promote_margin": 0.01,
  // … the usual scoring weights …
  "tournament": {
    "structure": "racing",        // gauntlet | racing; the experimental three need the opt-in (§3.6)
    "params": {
      "field_size": 4,            // challengers proposed per round (not accepted by gauntlet)
      "replicates": 2,            // paired runs per duel, averaged (§6 noise lever)
      "eta": 2,                   // racing: keep top 1/eta each rung
      "board_fraction": 0.4,      // racing: rung-0 board slice = ceil(fraction · |board|)
      "rung0_board_size": 0,      // racing: 0 ⇒ derive rung-0 size from board_fraction
      "slice_schedule": "shuffled_v1",       // racing: opt-in; omit for the default authored-order slices
      "matchup_budget_seconds": 300,      // racing: opt-in per-duel wall-clock cap (grind guard, §3.5)
      "final_rung_budget_seconds": 600    // racing: overrides the cap for the final crowning duel
      // swiss instead adds: "rounds_n": 4
    }
  }
}
```

`field_size` is *how many challengers the proposer must emit each
round*; `gauntlet` fixes it at `1` and refuses the key. The racing strategy
additionally reads the board's entry ids from `params["board_ids"]` to
slice the rungs. `board_ids` is **OPTIONAL**: when the contract omits it,
the orchestrator defaults it to the epoch's full board (injected centrally
in `zicato.selection.make_strategy`), so neither the JSON contract nor the
CLI-flag form below needs to list the ids. Pass an explicit `board_ids`
only to race on a *subset* of the board — an explicit list always
overrides the default (see `src/zicato/selection/strategies/racing.py`).

**2. Set it from `zicato evolve` flags (contract-mutating convenience).**

```bash
zicato evolve \
    --tournament-structure racing \
    --tournament-param field_size=4 \
    --tournament-param eta=2 \
    --tournament-param board_fraction=0.4 \
    --tournament-param replicates=2 \
    --rounds 2
```

`--tournament-structure` writes `{structure, params}` into the live
`scoring.json` *before* the contract hash is computed, so it participates
in the hash the way a hand edit does. Each `--tournament-param KEY=VALUE`
is repeatable; `VALUE` is parsed as JSON when possible (so `field_size=4`
is the integer `4`), else taken as a string, and the other params are
preserved. Either flag works alone. `--tournament-structure` keeps the
existing params the new structure accepts and drops the rest, so switching
a racing contract to `gauntlet` removes `field_size`, `eta` and
`board_fraction` and keeps `replicates` and the `promote_confidence_*`
keys. `--tournament-param` applies after the switch, and a key the new
structure does not accept is refused. `--dry-run` checks the edit without saving
it, and neither flag combines with `--epoch`. `zicato evolve --help` is
the authoritative flag reference, and [`CLI.md`](CLI.md) is generated
from it.

**Either way, changing the structure rolls the epoch.** Because the
`tournament` block is part of the frozen evaluation contract (§4.1), a
structure or param change is a contract-hash change: the next `evolve`
closes the current epoch and opens a fresh one, as retuning
`promote_margin` does. A gauntlet champion and a racing champion are
selected under different rules and are not directly comparable, which is
why the structure rolls the contract.

### 4.1 The `tournament` block is part of the contract hash

The structure changes *what a promotion means*, so generations
selected under different structures are not directly comparable, which is the same
rationale as for the other contract components. No new canonical component
was needed: because `tournament_structure` is a nested frozen
dataclass field of `ScoringWeights`, the scoring canonicaliser serializes
it with every other declared field (`scoring_to_canon` in
`src/zicato/epoch/contract.py`, through `dataclass_to_jsonable`),
including its `params` mapping. Switching structures or bumping any param changes the canonical
scoring form and rolls the epoch automatically.

---

## 5. Where the implementation lives

| Part | Location |
|---|---|
| The ABC, value types, `Standing` / `RoundRecord` / `MatchRecord` | `src/zicato/selection/strategy.py` |
| The driver and the evidence-gate confirmation | `src/zicato/selection/driver.py`, `src/zicato/selection/evidence_gate.py` |
| The registries, `make_strategy`, `default_replicates_for` | `src/zicato/selection/registry.py` |
| The replicate-count resolution | `src/zicato/selection/replicates.py` |
| `gauntlet`, `racing`, and the shared `ChampionGateStrategy` | `src/zicato/selection/strategies/` |
| `single_elim`, `double_elim`, `swiss` | `src/zicato/selection/experimental/` |
| The leader resolvers and standings rating | `src/zicato/selection/resolve.py`, `src/zicato/selection/standings_ext.py`, `src/zicato/selection/rating.py` |
| `TournamentStructure` and the structure constants | `src/zicato/core/tournament.py` |
| One duel: `run_matchup`, replication | `src/zicato/tournament/runner.py`, `src/zicato/tournament/scheduling.py` |
| The round phases | `src/zicato/evolve/field_candidates.py`, `field_execution.py`, `gate.py`, `settlement.py` |
| The `--tournament-structure` / `--tournament-param` flags | `src/zicato/cli/commands/evolve.py`, through `src/zicato/contract_draft/operations.py` |

### 5.1 What persists

Per *tournament resolution* (per round), the settled state persists so
the dashboard bracket (`TOURNAMENT.md §2`) and the journal can render
it: the `structure` + `params` actually used, the settled `rounds` and
`standings`, the field status, the crowning decision, and each
candidate's `OutcomeRecord` with its `match_record`. All of it lands in
the round's settlement record, `rounds/<n>/field_settlement.json`, and
the gate explanation of every duel lands in that record's
`gate_results`. A gauntlet persists through the same path with one
match. The concrete schema, the analytical-index table, and the
dashboard rendering are specified in
[`TOURNAMENT-DATA-MODEL.md`](TOURNAMENT-DATA-MODEL.md).

---

## 6. Composition with the gate, replication, and §5 stopping

- **Gate**: untouched. Every `Matchup` ends in `evaluate_gate`
  (`src/zicato/tournament/gate.py`). A challenger-vs-challenger duel feeds the two
  challenger aggregates in as `(parent, child)`; the strategy reads the
  sign of `delta_scalar` and never the feasibility rules for *ranking*.
  The pass-rate and per-namespace monotonicity rules still fire, and a
  structure may choose to treat a feasibility-failing node-winner as
  eliminated, which is a strategy policy rather than a gate change. The
  *final* champion-gate is the full three-rule test.
- **Replication** (`SELECTION.md §9`): surfaced as `Matchup.replicates` and applied
  by `_run_replicated` for every production strategy. Each requested slot is
  keyed by generation, board entry, and replicate for both competitors, and
  every path folds through `_average_losses`. The standalone `run_tournament`
  and `run_fast_mode` APIs behind `zicato tournament run` retain their own
  replication parameters. The gauntlet default is `2`, as it is for the
  bracket structures; only `racing` declares `1` because escalating board
  slices supply repeated evidence.
- **Stopping** (`SELECTION.md §10.4`): inter-round stopping stays in
  `evolve_n_rounds`, outside the strategy. The strategy resolves the
  *intra-tournament* bracket; `evolve_n_rounds` decides whether to run
  the next round. For `gauntlet` the two coincide (one duel per round).

---

## 7. The interface agreed with the data-model design

This document and [`TOURNAMENT-DATA-MODEL.md`](TOURNAMENT-DATA-MODEL.md)
divide ownership as follows:

1. **The `tournament` config block** — the `{structure, params}` shape
   and the `TournamentStructure` type are owned by the data model; the
   per-key semantics of `params` are owned by the strategies here (§3).
2. **The contract hash** — the block is part of the scoring contract
   component, so a structure or param change rolls the epoch (§4.1).
3. **The persisted bracket record** — owned by the data model. Every
   strategy emits a `SelectionDecision`, a flat list of
   `MatchupResult`s, and the `Standing` / `RoundRecord` records; the
   gauntlet emits exactly one `MatchupResult`, so the single-matchup
   record is the special case of the general one.
4. **The dashboard rendering** of the structures (single-elim tree,
   Swiss standings, racing rung ladder) — owned by the data-model and
   dashboard work; this design guarantees the audit data (§5.1).

---

## 8. Cross-references

| Topic | Document |
|---|---|
| Why racing is the default; per-structure decision theory; §8 experimental verdict | [`SELECTION.md §10`](SELECTION.md#10-configurable-per-epoch-tournament-structures), [`SELECTION.md §8`](SELECTION.md#8-single-elimination-double-elimination-and-swiss-are-experimental) |
| The strategy-driven runner flow; generalised dashboard bracket | [`TOURNAMENT.md §1.4`](TOURNAMENT.md#14-five-structures-racing-is-the-default) |
| The promote gate every structure consumes unchanged | [`SCORING.md §5`](SCORING.md#5-the-tournament-promotion-gate), `src/zicato/tournament/gate.py` |
| Replication, a multi-candidate field, and winner's-curse confirmation | [`SELECTION.md §9`](SELECTION.md#9-the-recommended-design) |
| The epoch as the frozen contract; auto-roll on contract change | [`EPOCHS-AND-JOURNALING.md`](EPOCHS-AND-JOURNALING.md), `src/zicato/epoch/contract.py` |
| Operator-facing: choosing + configuring a structure | `skills/zicato-design-tournament-structure/SKILL.md` |
| The Bradley-Terry rating layer under these structures: the opt-in `experimental.standing_rating` theta-rank standings and the visibility Elo fold, and the opt-in `experimental.resolver` winner resolution (Copeland or Ranked Pairs behind a Smith-set prune, `selection/resolve.py`). The maximal-lottery resolver is unbuilt. | [`SELECTION-THEORY.md`](SELECTION-THEORY.md) |
