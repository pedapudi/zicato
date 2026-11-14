# The Pareto frontier record

The promote gate keeps ONE generation per round and it picks that one by a
**weighted sum** (`docs/design/SCORING.md` §2). A weighted sum is a
projection: it collapses several axes onto a line, and everything that was
only visible off that line is lost. A challenger that halves cost while
giving up a sliver of rubric loses on the scalar, is rejected, and — without
a record — is never mentioned again. The information the loop paid for is discarded.

This document specifies a **record** of those candidates: for each epoch, the
set of settled candidates that beat the reigning champion on at least one
scoring axis and that nothing else on the record dominates.

> **The record decides nothing.** It never enters the gate, never enters
> selection, never moves the champion pointer. By default it has two
> readers. The operator's trade-off table (§7) prints it at epoch close and
> in `analysis.md`. The confirmation step (§6.1) re-measures one member
> against the current champion and refreshes or retires it. Two opt-in
> consumers, both off by default, let the record influence what is
> PROPOSED: the recombination slot can draw its parents from confirmed
> members (§6.2), and the proposer can be told which scored dimensions no
> confirmed member improves (§6.3). Every candidate they lead to is gated
> against the reigning champion like any other. §8 lists what the record
> does NOT do and why each of those is separate, gated work.

## 1. Why this needs no knob

Every input the frontier needs already exists in the frozen epoch contract,
and reusing them is what keeps the feature honest:

| Input | Source | Meaning |
|---|---|---|
| the namespace axes | keys of `ScoringWeights.namespace_weights` with a non-zero weight | what the operator declared they are optimizing |
| the dimension axes (only with a consumer enabled, §2.1) | the judges and scored metrics inside those namespaces, ranked by the contract's weights | the scored targets the proposer is shown, so the record separates candidates a namespace total hides |
| the direction | the SIGN of each weight | `rubric:` is negative, so a higher raw rubric is better |
| the units | `namespace_aggregates`, and the same product split per metric — already weight-multiplied | scalar points, so one threshold serves axes whose raw units differ by orders of magnitude |
| the threshold | `ScoringWeights.promote_margin` | `tournament/calibration.py` fits it to an A/A noise floor, so "better" means the same thing here as at the gate |
| the reset | the epoch | an epoch is a frozen contract; comparing across contracts is meaningless |
| the abuse control | `ScoringWeights.namespace_monotonicity` | §4 — the reason a degenerate cut-everything candidate cannot land on the record |

A new knob would have to be tuned against a noise floor nobody has measured
for it. There is none. The one constant the feature adds,
`FRONTIER_DIMENSION_CAP`, bounds the axis count and is not configurable.

`aggregate_namespaced_metrics()` multiplies each namespace's per-run mean by
its signed weight *before* returning it, which is the single fact that makes
this cheap: the sign is already folded in, so **every axis is uniformly
lower-is-better** and the comparison never has to branch on direction. The
per-namespace monotonicity rule in `tournament/gate.py` relies on the same
property.

### 1.1 Why it ships default-on

The case for shipping a frontier disabled applies to the steps that would
let a frontier member *influence* the loop: a frontier is easier to
abuse than a scalar once the loop reads it. Those steps (§6.2, §6.3) are
off by default. With them off the loop does not read this record: its
readers are a report for the operator and a confirmation step with no
caller, so it has no abuse surface, and a default-off record
produces no evidence about whether the frontier is worth having, which is
the question the next steps need answered. Both precedents in this repo for "derived, read-only, never gates"
— the Bradley–Terry Elo fold and the RoundLog — ship on.

## 2. Axes and dominance

### 2.1 The axis set

```python
def frontier_axes(
    weights: ScoringWeights, aggregate: Mapping[str, Any] | None = None
) -> tuple[str, ...]
```

Two kinds of axis, namespaces first.

**Namespace axes.** The sorted namespace keys of `weights.namespace_weights`
whose weight is not zero. Under the defaults that is `("cost:", "drift:",
"failure:", "judge:", "latency:", "rubric:", "schema:")`.

`output:` and `runtime:` are **not** axes. Their default weight is `0.0`, and a zero weight
has neither a sign nor a scale — there is no direction in which "more output
characters" is better or worse, which is why the defaults set it to
zero. An operator who gives `runtime:` (the whole-run duration channel,
`runtime:seconds`) a non-zero weight makes it an axis. Making it an axis would need a per-axis noise floor, which is separate
work (§8).

`latency:` **is** an axis by weight (`0.0001`) but is empty in practice: the
telemetry reducer never fills the `latency:` namespace, even though
`MetricCount` documents `latency:p95_turn_ms`. The run duration is reported
separately as `runtime:seconds` in the zero-weight `runtime:` namespace. `aggregate_namespaced_metrics()` promotes
known-but-absent namespaces to `0.0`, so the axis is present and constant —
it can never separate two candidates, and it can never wrongly separate them
either. Filling it is **not** done here: the `0.0001` weight
would start contributing to the scalar and move every score in the workspace.
That is a scoring change, and it rolls the epoch. Registered in §8.

So the useful default namespace axes are `cost:`, `drift:`, `failure:`,
`judge:`, `rubric:`, and `schema:`.

**Dimension axes — only with a consumer enabled.** Under a contract that
enables neither consumer of the record (`experimental.recombine_pool:
"frontier"`, §6.2, or `experimental.frontier_steering`, §6.3), the axis set
is the namespace axes above and nothing else, and the record holds what it
held before the dimensions existed. `uses_dimension_axes(weights)` is that
test. The reason is the noise cost stated at the end of this section: every
axis is a chance to be admitted by noise, and a finer axis earns that cost
only when something uses it.

Namespaces are coarse. Two rubric metrics that move in
opposite directions leave `rubric:` unchanged, and a candidate that fixes one
judge while another slips looks the same as the champion on `judge:`. So with
a consumer enabled the
axis set also holds the dimensions inside each weighted namespace: one per
judge (`judge:<name>`) and one per scored metric (`rubric:slide_structure`,
`cost:tokens_spent`, `failure:tasks`, …).

`aggregate_dimension_metrics()` (`tournament/scoring.py`) computes them from
the same loss profiles and by the same formula as the namespace aggregate,
split per metric name: the namespace weight times the per-run mean of the
within-channel-weighted count. A namespace's dimensions therefore sum to its
aggregate, every dimension is in scalar points and lower-is-better, and the
one `promote_margin` threshold applies to it unchanged. The gate does not
read these values and `gen_score.json` does not carry them: the round's
recorder adds them to the aggregates it offers the record
(`frontier_aggregates()`, §6).

The metric names come from the data, because only the data knows which
metrics a board reports, so `frontier_axes` reads them from the reference
champion's aggregate. Which of them become axes is decided by the contract
alone:

- a dimension's rank is the magnitude of its contract weight — the namespace
  weight times the per-judge weight for a judge, times `task_failure_weight`
  or `not_completed_weight` for the two `failure:` members, times `1.0` for
  any other metric — then its name;
- at most `FRONTIER_DIMENSION_CAP` (8) dimensions are kept;
- a namespace with a single dimension contributes none, because that
  dimension is the namespace axis;
- with no reference aggregate, or one that carries no dimensions, the axis
  set is the namespace axes.

`drift:` has no dimension axes. Drift reaches the scalar as one per-run
`drift_loss`, reduced upstream from severity and kind weights, plan
revisions, and a clamp, and optionally by a scoring plugin, so a drift
kind's share of the scalar is not recoverable from the aggregate. Giving the
drift kinds axes needs a per-kind decomposition beside
`builtin_drift_loss`, and it would not hold under a drift plugin (§8).

Under the default weights with a consumer enabled, the dimension axes are,
in order, `failure:not_completed` (weight 50) and `failure:tasks` (10), which
every run reports; then the board's `schema:` metrics (5); then its judges
and `rubric:` metrics (1 each, so by name, judges first); then its `cost:`
metrics (0.001) — each namespace only where it reports two or more, and the
first eight in all.

More axes mean more chances for a candidate to clear the margin against the
champion on one of them by noise. A simulated candidate identical to the
champion, with independent per-axis noise equal to the margin, was admitted
25% of the time on the namespace axes and 43.5% with eight dimensions added.
That is why the dimensions are off without a consumer, why they are capped,
and why §6.1 re-measures a member before anything uses it.

### 2.2 Axis values

```python
def axis_values(aggregate: Mapping[str, Any], axes: Sequence[str]) -> dict[str, float]
```

Reads `aggregate["namespace_aggregates"][ns]` for a namespace axis — the
weight-multiplied, sign-folded value the gate already compares — and
`aggregate["dimension_aggregates"][name]` for a dimension axis. An axis
absent from the aggregate, or carrying a non-finite value, is **omitted**
rather than defaulted to zero: a missing measurement is not a measurement of
zero, and fabricating one would invent a dominance relation. This mirrors
`regressed_namespaces()`, which skips a namespace it cannot see on both
sides. A metric the champion reports and a candidate does not is therefore
neither a win nor a loss for the candidate.

### 2.3 Dominance

```python
def dominates(
    left: Mapping[str, float],
    right: Mapping[str, float],
    *,
    margin: float,
) -> bool
```

`left` dominates `right` when it is **at least `margin` better on at least
one axis** and **not `margin`-or-more worse on any**. In the uniform
lower-is-better view:

```
better_any = any(right[ns] - left[ns] >= margin and left[ns] < right[ns]  for ns in shared)
worse_any  = any(left[ns] - right[ns] >= margin and right[ns] < left[ns]  for ns in shared)
dominates  = better_any and not worse_any   # the code returns False on the first worse axis
```

Each limb requires a *strict* difference on top of clearing the margin. For
every `margin > 0` that conjunct is implied by the inequality beside it and
changes nothing; it exists because `promote_margin` is not validated
positive, and at `margin == 0` a bare `>= margin` is satisfied by an exact
tie. That would make the relation reflexive and symmetric on identical
points — neither of which a partial order may be — and would let a tie on
the worse limb veto a candidate that is strictly better somewhere and equal
everywhere else. With the conjuncts, `margin == 0` degrades cleanly to
strict Pareto dominance.

`shared` is the axes both sides carry a finite value for; an axis only one
side has is skipped, so it neither creates nor blocks a dominance relation.
With no shared axes the result is `False` in both directions — two candidates
with nothing in common are *incomparable* rather than dominant.

The margin band is what makes this a **weak** dominance test rather than one
that turns on an arbitrarily small difference. Inside `±margin` the two
candidates are tied on that axis, because `promote_margin` is the width at
which the loop treats a difference as more than noise. A candidate that is
better on every axis by less than the margin therefore dominates nothing, and
a candidate that is worse on one axis by less than the margin is not thereby
saved from being dominated.

```python
def beats_on(
    candidate: Mapping[str, float],
    reference: Mapping[str, float],
    *,
    margin: float,
) -> tuple[str, ...]
```

The sorted axes on which `candidate` is at least `margin` better than
`reference`. This is both the admission test against the champion (§4) and
the recorded provenance for why a member is on the record.

## 3. What the record holds

`epochs/{epoch}/pareto_frontier.json`, written atomically
(`zicato.storage.atomic_write_json` — `.tmp` + `fsync` + `os.replace`, the
one definition of an atomic write in zicato).

```json
{
  "format_version": 1,
  "epoch_id": "epoch-2026-08-02T00-00-00Z",
  "axes": ["cost:", "drift:", "failure:", "judge:", "latency:", "rubric:", "schema:",
           "failure:not_completed", "failure:tasks", "rubric:slide_structure", "rubric:tone"],
  "margin": 0.01,
  "champion_generation_id": "v7",
  "updated_round": 9,
  "members": [
    {
      "generation_id": "v5",
      "round_admitted": 6,
      "champion_generation_id": "v7",
      "scalar": 0.5130,
      "axis_values": {
        "cost:": 1.284, "drift:": 0.310, "failure:": 0.0, "judge:": 0.12,
        "latency:": 0.0, "rubric:": -0.780, "schema:": 0.0,
        "failure:not_completed": 0.0, "failure:tasks": 0.0,
        "rubric:slide_structure": -0.41, "rubric:tone": -0.37
      },
      "beats_champion_on": ["cost:"],
      "beats_champion_by": {"cost:": 0.642},
      "confirmed_round": 9
    }
  ],
  "retired": [
    {
      "generation_id": "v3",
      "round_admitted": 4,
      "round_retired": 9,
      "reason": "dominated_by:v5",
      "champion_generation_id": "v2",
      "scalar": 0.6021,
      "axis_values": { "...": 0.0 },
      "beats_champion_on": ["cost:"]
    }
  ]
}
```

Notes on the shape:

- `champion_generation_id` at the top level is the champion the frontier was
  last evaluated against at a settle; on a member it is the champion that
  member's values were last measured against — the one it was admitted
  against, until a confirmation (§6.1) re-measures it against a later one.
  Keeping both means a member never silently re-attributes its provenance to
  a champion it was never compared with.
- `beats_champion_by` is the gain on each axis of `beats_champion_on`, in
  scalar points: the champion's value minus the member's.
- `confirmed_round` is the round a fresh measurement last confirmed the
  member. It is absent while the member's values are the tournament draws
  that selected it.
- `beats_champion_by` and `confirmed_round` are written only when they hold a
  value, and a record without them reads as empty and unconfirmed. A record
  written before the dimension axes existed lists namespace axes only; its
  members are compared with later candidates on the axes both carry. The
  format version is unchanged.
- `members` is sorted by `generation_id`; `retired` by
  `(round_retired, generation_id)`. Stable order, so the file diffs cleanly.
- `axes` and `margin` are echoed from the contract so the file is
  self-describing — a reader does not need `scoring.json` to interpret it.
- `format_version` follows the canonical-record discipline
  (`zicato.epoch._storage.check_record_format`): the present record must carry
  the owner's supported integer version. Missing, Boolean, floating-point,
  and incompatible stamps are refused.
- **A missing file is an empty frontier.** A present malformed file is refused;
  it cannot be substituted with `members: [], retired: []`.

## 4. Admission — the control that makes the record safe

A frontier is easier to abuse than a scalar. Today `drift: 1.0` dwarfs
`cost: 0.001`, so a candidate cannot win by gutting cost at the expense of
quality. Drop the weighted sum and that protection goes with it: a candidate
that cuts tokens by 90% and produces nothing of value is dominated by nobody.

`namespace_monotonicity` is the control, and it is mandatory from the first
line of this feature rather than deferred to the step that first reads the
record. A candidate is admitted only when **all** of the following hold:

1. **Settled.** It carries a real aggregate from a completed duel. This is
   structural: the recorder is called at the round-settle seam only, and a
   candidate with no finite axis value is refused.
2. **Not a placebo.** A random-baseline arm (`PLACEBO_HYPOTHESIS_MARKER`,
   detected by `zicato.evolve.placebo.is_placebo_experiment`) is a
   calibration probe rather than a candidate. It is a no-op re-emission of the
   champion, so it would sit on the frontier as a permanent tie and
   contaminate the record whose job is to hold interesting
   candidates. The multi-challenger path fields the placebo *inside* the
   slate, so this check is load-bearing rather than theoretical.

   The rule has a second half on the REFERENCE side: a placebo may not be
   the *champion* either. The gate can crown the arm, which is what
   the CRITICAL `placebo_promoted` health finding exists to catch, and when
   it does, `update_frontier` refuses the whole round rather than measuring
   against a no-op copy of the champion. Otherwise every admission that
   round would attribute its `champion_generation_id` to a generation that
   exists only to test the gate, and real members would be retired against
   a reference the loop is already alarming about.
3. **No namespace regression against the champion.** Evaluated by the gate's
   own logic on the default-on namespaces (`rubric:`, `schema:` per
   `_default_namespace_monotonicity`). A candidate whose rubric collapsed or
   that introduced schema failures is refused however well it did on cost.
   The rule reads namespaces only, so a win on one dimension of `rubric:`
   does not excuse a regression of `rubric:` as a whole.
4. **It beats the champion by at least `margin` on at least one axis.** A
   candidate that ties or loses everywhere carries no information the
   champion does not already carry. (This also subsumes "the champion does
   not dominate it": beating the champion on an axis by `margin` makes the
   champion `margin`-worse there, so `worse_any` holds for the champion.)
5. **No current member dominates it.**

On admission, any current member the newcomer dominates is retired.

The champion is the **reference** rather than a member. The record's whole claim is
"here is what was better than the champion somewhere", which needs the
champion to sit outside the set being compared.

### 4.1 The gate's own namespace check, reused

The per-namespace monotonicity check is the public `regressed_namespaces()`
in `tournament/gate.py`. The gate and the frontier call that one function. There is no
second implementation of "did this namespace regress", so the two cannot
drift apart.

## 5. The update, as a pure function

```python
def update_frontier(
    frontier: ParetoFrontier,
    *,
    champion: FrontierCandidate,
    candidates: Sequence[FrontierCandidate],
    weights: ScoringWeights,
    round_index: int,
) -> FrontierUpdate
```

Total, deterministic, no I/O. The axis set is
`frontier_axes(weights, champion.aggregate)`. Two passes:

**Retire pass** (against the champion as it stands *after* the round's
decision):

Tested in this order, first match wins — which matters, because a champion
that improved on every axis both dominates the member *and* out-runs it on
a monotonicity namespace, and only one reason is recorded:

| # | condition | reason recorded |
|---|---|---|
| 1 | the member IS now the champion | `promoted` |
| 2 | the member regresses a monotonicity namespace vs the champion | `monotonicity_regression` |
| 3 | the champion dominates the member | `dominated_by_champion` |

**Admit pass**, over the round's candidates in `generation_id` order, applying
§4. Each admission may retire members with reason `dominated_by:{gid}`.

`dominated_by:{gid}` names a MEMBER, and a member can itself retire later, so
a reason can end up pointing at a generation that has itself left the
frontier. That is intended: `retired` is an append-only evidence trail
rather than an index, and the referent is always still findable in the same
file (nothing is ever deleted) or in the epoch's lineage.

Nothing is ever deleted. A member that leaves the frontier moves to `retired`
with the round and the reason, because the interesting question later is not
only "what is on the frontier" but "what was on it and what displaced it".
This is the same evidence discipline the archive-on-overwrite rule applies to
`gen_score.history.jsonl`.

A **promotion keeps the frontier.** The epoch's contract has not changed, so
the earlier measurements are still comparable; only the reference point moved,
which is what the retire pass handles. An **epoch roll starts empty** — a
fresh contract means fresh axes, a fresh margin, and scores that are not
comparable to the preceding epoch's.

### 5.1 Types

```python
@dataclass(frozen=True, slots=True)
class FrontierMember:
    generation_id: str
    round_admitted: int
    champion_generation_id: str
    axis_values: Mapping[str, float]
    beats_champion_on: tuple[str, ...]
    scalar: float | None = None
    beats_champion_by: Mapping[str, float] = {}
    confirmed_round: int | None = None

@dataclass(frozen=True, slots=True)
class RetiredMember:
    member: FrontierMember
    round_retired: int
    reason: str

@dataclass(frozen=True, slots=True)
class ParetoFrontier:
    epoch_id: str
    axes: tuple[str, ...] = ()
    margin: float = 0.0
    champion_generation_id: str = ""
    updated_round: int = 0
    members: tuple[FrontierMember, ...] = ()
    retired: tuple[RetiredMember, ...] = ()

@dataclass(frozen=True, slots=True)
class FrontierCandidate:
    generation_id: str
    aggregate: Mapping[str, Any]
    is_placebo: bool = False

@dataclass(frozen=True, slots=True)
class FrontierUpdate:
    frontier: ParetoFrontier
    admitted: tuple[str, ...] = ()
    retired: tuple[str, ...] = ()
    confirmed: tuple[str, ...] = ()

    @property
    def changed(self) -> bool: ...
```

### 5.2 Persistence

```python
def frontier_path(workspace_root: Path, epoch_id: str) -> Path
def load_frontier(workspace_root: Path, epoch_id: str) -> ParetoFrontier
def save_frontier(workspace_root: Path, epoch_id: str, frontier: ParetoFrontier) -> None
def frontier_to_dict(frontier: ParetoFrontier) -> dict[str, Any]
def frontier_from_dict(body: Mapping[str, Any], *, epoch_id: str) -> ParetoFrontier

def record_frontier(
    workspace_root: Path,
    epoch_id: str,
    *,
    champion: FrontierCandidate,
    candidates: Sequence[FrontierCandidate],
    weights: ScoringWeights,
    round_index: int,
) -> FrontierUpdate
```

`record_frontier` is load → `update_frontier` → save-only-if-changed. All of
this lives in `zicato/epoch/pareto.py`: the record is a per-epoch canonical
artifact, which is what the `epoch/` package owns. The same module holds the
pure half of confirmation (`confirm_member`, §6.1) and the operator table
(`render_frontier_tradeoffs`, `frontier_tradeoffs_section`, §7).

## 6. Where it is wired

```python
# zicato/evolve/pareto.py
def record_round_frontier(
    *,
    workspace_root: Path,
    epoch_id: str,
    round_index: int,
    weights: Any,
    champion_generation_id: str,
    aggregates: Mapping[str, Mapping[str, Any]],
    placebo_generation_ids: Collection[str] = (),
    round_log: Any | None = None,
) -> None
```

One best-effort call from `_publish_field_observations`
(`zicato/evolve/settlement.py`), which runs after the round's settlement is
committed — after holdout confirmation, integrity checks, and operator
overrides. The frontier therefore uses the champion that the round
commits (the promoted generation, else the incumbent). Per-generation
aggregates accumulate in `run_field_matchup` (`zicato/evolve/field_execution.py`)
where `write_gen_score` writes them. `frontier_aggregates()`
(`zicato/evolve/pareto.py`) adds each side's dimension contributions to the
copy the record reads, computed from that duel's train-slice loss profiles;
a side whose profiles do not cover exactly the entries its aggregate scored
gets none, and keeps its namespace axes. Evidence-gate replicate duels set
`cache_scores=False`, so they cannot overwrite the round-scored aggregate.

Failure of the recorder can never fail a round. It follows the emission
discipline the live index dual-write established: the canonical stores stay
authoritative, and any exception is logged at `debug` and swallowed. The
infra-outage deferral path returns before the seam — a round whose verdict is
meaningless contributes no members.

### 6.1 Confirm before use

A member is on the record because the tournament draws that scored it beat a
champion on some axis. Those draws were selected for that, so they overstate
the member, and the more axes there are the more often one of them clears
the margin by noise. After a promotion the member's values also describe a
comparison with a champion that has been replaced: the settle-time retire
pass re-tests the stored values against the new champion, and measures
nothing.

```python
# zicato/evolve/pareto.py
async def confirm_frontier_member(
    *,
    champion: Generation,
    member: Generation,
    board: Sequence[BoardEntry],     # the epoch's FULL board; split here
    weights: ScoringWeights,
    workspace_root: Path,
    epoch_id: str,
    round_index: int,
    round_log: Any | None = None,
    **matchup: Any,                  # run_matchup's adapter, config, writer, ...
) -> FrontierMember | None

# zicato/epoch/pareto.py
def confirm_member(
    frontier: ParetoFrontier,
    *,
    champion: FrontierCandidate,
    member: FrontierCandidate,
    weights: ScoringWeights,
    round_index: int,
) -> FrontierUpdate
```

`confirm_frontier_member` is the step a consumer of the record calls for the
member it is about to use. Its one caller is the frontier recombination pool
(§6.2); with that knob off nothing calls it.

**The measurement.** It splits the board with `split_board` and runs only
TRAIN entries, so the holdout slice never reaches the record. It runs
champion and member through `run_matchup` — the board-unit runner every duel
uses — at draw 0 of the `evidence_confirmation` purpose, cache-first. That
purpose is the one the evidence gate's replicate duels use: its draws are
numbered separately from the tournament's, so a confirmation cannot be
answered by the tournament measurement that put the member on the record.

**The cost.** The unit cache is keyed by generation, entry, purpose, draw,
and seed, so each generation is measured once per epoch at that draw:

| Call | Board units run |
|---|---|
| first confirmation of a member | the member's train entries, plus the champion's unless the evidence gate already measured that champion at confirmation draw 0 |
| the same member against the same champion again | none — a member already confirmed against this champion is returned without scheduling anything |
| the same member after a promotion | the new champion's train entries only; none if the evidence gate confirmed that champion's own promotion |
| another member against a champion already measured | that member's train entries only |

The INFO line and the `frontier_updated` round-log event state the units run
and the units reused, whether the member was confirmed, retired, or not
measured completely:

```
frontier: epoch <id> round <n> — v5 against v7: confirmed; 3 board unit(s) run, 3 reused
```

**The decision** is `confirm_member`, pure. From the two fresh aggregates it
recomputes the member's axis values against the current champion, on the
axis set that champion's aggregate names.

| Fresh result | Effect |
|---|---|
| the member beats the champion by `promote_margin` on at least one axis it was RECORDED as winning (`beats_champion_on`), and regresses no monotonicity namespace | **refreshed**: fresh `axis_values` and `scalar`, the axes it now wins on and by how much, `champion_generation_id` set to this champion, `confirmed_round` set |
| otherwise | **retired** with reason `unconfirmed` |

A fresh win on an axis the member was not recorded as winning does not
confirm it. A member admitted on `cost:` whose fresh cost is worse and whose
fresh `judge:` happens to clear the margin has replicated nothing, and is
retired. Once a recorded win replicates, the refreshed `beats_champion_on`
lists every axis the fresh measurement wins.

Dominance between members is not re-run on a refresh: the other members'
values come from different draws.

`confirm_frontier_member` returns the refreshed member, or `None` when the
caller must not use the generation: it is not a member, it was retired, the
measurement was incomplete, or the confirmation raised. An incomplete
measurement or a failure leaves the record as it was.

One draw is a sample of one. It removes the selection bias of the tournament
draws; it does not make the member's advantage certain. The gate still
decides any promotion a consumer leads to (§8).

### 6.2 The frontier as the recombination pool

`experimental.recombine_pool` (`"rejects"` by default, or `"frontier"`)
chooses where the recombination slot (`docs/design/PROPOSER.md` §2.6) finds
its two parents. It has no effect unless `experimental.recombine` is on and
`best_of_n > 1`. At `"rejects"` the slot behaves as it did before the knob
existed. At `"frontier"`:

- **The pool is the epoch's frontier members**, and only them. With fewer
  than two usable members the round mints nothing. It does not fall back to
  the rejects pool: a round that might have used either pool could not be
  attributed to one in a comparison. Such a round says so at DEBUG.
- **A member is usable** when it passes every per-candidate predicate of the
  selector (rejected, proposed against the CURRENT champion, no placebo, not
  itself a mint, patches inside the current manifest) and
  `confirm_frontier_member` upholds it against that champion.
- **Members from an earlier reign are not recombination parents.** A
  member's own tree is its parent's tree plus its patch, and the mint applies
  that patch to the current champion. For a member proposed against an
  earlier champion those are different trees: its measured win can come from
  lacking a later promotion's edit, which the mint would not reproduce. Such
  members stay on the record and in the operator's table.
- **A pair** needs disjoint `beats_champion_on` sets, so each parent wins
  where the other does not. Pairs rank by the number of axes the two win
  between them; the selector's remaining keys then break ties unchanged —
  patch overlap, the per-entry cross-regression count from the train loss
  grid, summed rating, generation id. The tried-pair and patch-disjointness
  predicates hold as they do for the rejects pool: a pair whose mint was
  persisted, whatever its outcome, is never minted again, and a mint the
  screen vetoed before it was persisted may be minted again, exactly as in
  the rejects pool (`docs/design/PROPOSER.md` §2.6).
- **Confirmation is spent on the best-ranked pair only.** The builder ranks
  on the record's values, confirms the first unconfirmed member of the best
  pair, takes the refreshed wins or drops a retired member, and ranks again.
  It stops when the best pair is fully confirmed, when no pair is left, or
  after `RECOMBINE_CONFIRM_MAX` (4) confirmations in the round. A round
  therefore runs at most four members' train entries plus the champion's,
  and usually two members'. Each confirmation writes its own
  `frontier_updated` event with the units run and reused (§6.1).
- **The child is an ordinary recombined candidate.** `RecombinationPair`,
  `mint_recombined_experiment`, the `recombined` selection mode, the screen,
  and the gate against the reigning champion are unchanged.

The builder is `_build_frontier_recombination_pair`
(`zicato/evolve/round_context.py`); the selector's frontier evidence is the
`wins` argument of `rank_pairs` (`zicato/epoch/recombine.py`).

**The import boundary.** `tests/test_pareto_frontier.py` pins that no module
under `tournament/`, `selection/`, or `proposer/` imports the record. That
pin stands, and it now states the rule the consumers follow: the record is
read in `evolve/`, and what crosses into those trees is a plain value — a
`RecombinationPair`, or a list of dimension names.

### 6.3 Steering

`experimental.frontier_steering` (off by default) adds one short block to
the proposer's scored-targets section:

```
No candidate measured against the current champion has yet improved: schema:x, judge:y.
A change that improves one of these covers ground the search has not.
```

`measured_dimensions` (`zicato/evolve/pareto.py`) starts from the round's
scored targets — each judge and each scored metric the contract weights, in
the contract's priority order — and keeps those the champion can improve on:
its train losses carry a finite contribution for the dimension, and that
contribution is not already at its floor (a positively weighted dimension at
zero, such as `failure:tasks` on a champion with no failures). A bare
namespace such as `latency:`, which the scored targets list only when no run
reported a metric in it, never qualifies. `unimproved_dimensions` then keeps
those no member improves. Only members
with a `confirmed_round` whose `champion_generation_id` is the current
champion count: a member still resting on its selection draws does not. A
dimension is improved when a member beats the champion on it, or on its
namespace where that member carries no separate axis for it. With no member
confirmed against the current champion, or no dimension left to name, the
block is absent.

`render_frontier_steering_block` (`zicato/proposer/prompts.py`) names at most
eight dimensions and carries nothing else: no generation id, no magnitude, no
count, no board entry. The names are ones the scored-targets section already
shows, and every value behind them is a train-slice measurement. Drift kinds
are not named, because they are not frontier axes (§2.1).

Confirmations come from §6.2. With steering on and the frontier pool off,
no member is ever confirmed and the block never appears.

## 7. What it surfaces

**The trade-off table.** `render_frontier_tradeoffs(frontier)` renders the
members as Markdown, and `frontier_tradeoffs_section(workspace_root,
epoch_id)` reads the canonical `pareto_frontier.json` through `load_frontier`
and renders it. An epoch with no members, or an unreadable record, renders
nothing.

```
## Trade-off candidates

| Candidate | Beats the champion on (scalar points gained) | Scalar | Measured against | Confirmed |
|---|---|---:|---|---|
| `v5` | `cost:` (0.642), `rubric:tone` (0.04) | 0.5130 | `v7` | confirmed (round 9) |
| `v8` | `judge:facts` (0.05) | 0.4710 | `v7` | unconfirmed (single measurement) |
```

Each row is one member: the axes it beats its reference champion on and the
scalar points it gains on each, its scalar, the champion those values were
measured against, and whether a fresh measurement has confirmed them. An
unconfirmed row rests on the single measurement that selected it and may be
noise; the table says so. A record that cannot be read or rendered, for any
reason, yields no table and one warning, and never fails the report or the
close. The table is printed in two places:

- `analysis.md` carries it as a section after the deterministic results, so
  the living draft shows it from the round a member is admitted, and
  `zicato repair report` regenerates it;
- `zicato epoch close` prints it after the close message.

**Adopting a member.** The text under the table states the one route that
exists. A promote override (`control/promote/<generation>`, written by the
dashboard's promote control) is claimed at the gate for a candidate of the
round in flight: the round settles with that candidate promoted, and its
`OutcomeRecord` carries `operator_override` and the reason. A candidate is
admitted to the record when its round settles, which is after that claim.
An override queued for a settled generation is never applied, because it
names no candidate of any later field, and it is archived when the epoch
rolls. No command promotes a settled member. Its patches are in
`epochs/{epoch}/generations/{id}/experiment.json`, and its source tree is in
the generation store. Giving a settled member a route back into the loop is
§8's alternate-parent step.

**One INFO log line per round, only when membership changed.** A round that
admits and retires nothing is silent.

```
frontier: epoch <id> round <n> — admitted v6; retired v3 (dominated_by:v6); size 2
```

**One additive RoundLog event**, also only on change:

```python
@dataclass(frozen=True, slots=True)
class FrontierUpdated:
    TYPE: ClassVar[str] = "frontier_updated"
    admitted: tuple[str, ...] = ()
    retired: tuple[str, ...] = ()
    size: int = 0
    confirmed: tuple[str, ...] = ()
    units_fresh: int = 0
    units_cached: int = 0
```

A confirmation (§6.1) emits the same event with `confirmed` or `retired`
naming the member and the two unit counts; a settle-time update leaves all
three at their defaults. It joins `EVENT_TYPES`, the `RoundEvent` union, and folds into
`RoundRecord.frontier_updates`. Every log written before the event existed
folds identically — an unknown token already reads back as a raw envelope,
and the new field defaults to empty.

**An additive analytical-index table** (`zicato/index/schema.py`), following
the `reflections` precedent: a whole table materialised by the
`CREATE TABLE IF NOT EXISTS` pass, so the in-place migration needs no column
ALTER.

```sql
CREATE TABLE IF NOT EXISTS pareto_frontier (
  epoch_id TEXT,
  generation_id TEXT,
  status TEXT,                    -- 'member' | 'retired'
  round_admitted INTEGER,
  round_retired INTEGER,
  retired_reason TEXT,
  champion_generation_id TEXT,
  scalar REAL,
  axis_values_json TEXT,
  beats_champion_on_json TEXT,
  PRIMARY KEY (epoch_id, generation_id)
)
```

The workspace file is canonical and the table is a pure projection of it:
`zicato repair index` re-derives every row from `pareto_frontier.json`, and the
frontier is fully readable with no index at all. A best-effort incremental
upsert runs at the settle seam and after a confirmation so a live index does
not go stale, mirroring `ingest_reflection`. The table has no reader: the
trade-off table reads the canonical file, and nothing queries the
projection except the row count `zicato repair index` reports.

**No UI and no new CLI command.** `zicato epoch list` is
untouched, the epoch view API payload is unchanged, and no dashboard code is
touched. A payload field with no renderer is a render-conformance violation
waiting to happen, and a `zicato frontier` subcommand moves the `CLI-HELP`
parity golden when `zicato epoch close` and `zicato repair report` already
show the table. The dashboard surface is registered in §8.

## 8. Not built

Two consumers are built and off by default: recombination from the frontier
(§6.2) and steering by unimproved dimension (§6.3). Neither has a measured
case (`docs/design/FEATURE-QUALIFICATION.md`).

Everything below would make the loop read the frontier in a further way.
That is where the abuse surface in §4 grows, so each is generator-arsenal
work: it ships default-off with an operating-characteristic proof that it
helps, per the campaign philosophy (`docs/design/CAMPAIGN.md`). Each calls
`confirm_frontier_member` (§6.1) on a member before using it, and none of
them promotes: the existing gate decides, against the reigning champion.

- **Show the members themselves to the proposer, in bands, beside the
  genealogy pool.** Steering (§6.3) names only what has not been improved. A
  proposer shown what landed on the record, and why, can learn to optimize
  for the record.
- **Propose from a member on a plateau.** After several rounds without a
  promotion, one slot of the field is proposed from a confirmed member's
  snapshot and still compared with the champion. This is also the route by
  which a settled member re-enters the loop (§7).
- **Dedicate one best-of-N slate sample to an unimproved dimension.**
  Steering (§6.3) informs every sample alike; reserving a slot changes the
  slate's composition.
- **Give the drift kinds axes.** Needs a per-kind decomposition of
  `drift_loss` in scalar points (§2.1).
- **Make `output:` an axis.** Needs a per-axis noise floor, since a zero
  weight has no sign and no scale.
- **Weight the `runtime:` namespace by default.** The run duration is
  already measured (`runtime:seconds`) at weight `0.0`. A default non-zero
  weight is a scoring change: it moves every scalar in the workspace and
  rolls the epoch. It is the axis most likely to separate candidates, since
  `drift:` and `cost:` tend to move together, but it belongs on its own
  terms rather than as a side effect of this record.
- **A dashboard surface, and a command that promotes a settled member.** The
  table is in `analysis.md` and the close output (§7). An epoch-view panel
  needs its own payload and renderer, and promoting a settled generation by
  hand needs a route through the gate's override claim that does not exist.

## 9. Known assumption

**Cross-round comparability.** `rotate_holdout` defaults to `True`, so two
rounds may score their candidates against different board entries. A frontier
that spans rounds assumes those raw values are comparable. The assumption is
probably safe — `promote_margin` is fitted to absorb that noise, and
the scalar comparison the gate already performs carries the identical
exposure — but nobody has confirmed it, and the record is the artifact that
would let someone confirm it. It is written down here rather than resolved.

A confirmation (§6.1) does not rest on it: it measures the member and the
current champion on the same train entries, at the same draw, in one
matchup. Dominance between members still does.
