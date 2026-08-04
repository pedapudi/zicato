# The Pareto frontier record

The promote gate keeps ONE generation per round and it picks that one by a
**weighted sum** (`docs/design/SCORING.md` §2). A weighted sum is a
projection: it collapses several axes onto a line, and everything that was
only visible off that line is lost. A challenger that halves cost while
giving up a sliver of rubric loses on the scalar, is rejected, and — today —
is never mentioned again. The information the loop paid for is discarded.

This document specifies a **record** of those candidates: for each epoch, the
set of settled candidates that beat the reigning champion on at least one
scoring axis and that nothing else on the record dominates.

> **The record changes nothing.** It never enters the gate, never enters
> selection, never enters the proposer's prompt, never moves the champion
> pointer. It is an observation written beside the champion lineage, in the
> same class as the Elo fold ("visibility, never the gate") and the RoundLog.
> §8 lists the things it deliberately does NOT do and why they are separate,
> gated work.

## 1. Why this needs no knob

Every input the frontier needs already exists in the frozen epoch contract,
and reusing them is what keeps the feature honest:

| Input | Source | Meaning |
|---|---|---|
| the axes | keys of `ScoringWeights.namespace_weights` with a non-zero weight | what the operator declared they are optimizing |
| the direction | the SIGN of each weight | `rubric:` is negative, so a higher raw rubric is better |
| the units | `namespace_aggregates` — already weight-multiplied | scalar points, so one threshold serves axes whose raw units differ by orders of magnitude |
| the threshold | `ScoringWeights.promote_margin` | `tournament/calibration.py` fits it to an A/A noise floor, so "better" means the same thing here as at the gate |
| the reset | the epoch | an epoch is a frozen contract; comparing across contracts is meaningless |
| the abuse control | `ScoringWeights.namespace_monotonicity` | §4 — the reason a degenerate cut-everything candidate cannot land on the record |

A new knob would have to be tuned against a noise floor nobody has measured
for it. There is none.

`aggregate_namespaced_metrics()` multiplies each namespace's per-run mean by
its signed weight *before* returning it, which is the single fact that makes
this cheap: the sign is already folded in, so **every axis is uniformly
lower-is-better** and the comparison never has to branch on direction. This
is the same property `tournament/gate.py`'s Rule 3 already relies on.

### 1.1 Why it ships default-on

The issue that proposed this suggested shipping step 1 disabled. That caution
was aimed at the steps that would let a frontier member *influence* the loop
(§8) — a frontier is genuinely easier to abuse than a scalar once something
reads it. Nothing reads it here. A record with no consumer has no abuse
surface, and a default-off record produces no evidence about whether the
frontier is worth having, which is exactly the question the next steps need
answered. Both precedents in this repo for "derived, read-only, never gates"
— the Bradley–Terry Elo fold and the RoundLog — ship on.

## 2. Axes and dominance

### 2.1 The axis set

```python
def frontier_axes(weights: ScoringWeights) -> tuple[str, ...]
```

The sorted namespace keys of `weights.namespace_weights` whose weight is not
zero. Under the defaults that is `("cost:", "drift:", "latency:", "rubric:",
"schema:")`.

`output:` is **not** an axis. Its default weight is `0.0`, and a zero weight
has neither a sign nor a scale — there is no direction in which "more output
characters" is better or worse, which is precisely why the operator set it to
zero. Making it an axis would need a per-axis noise floor, which is separate
work (§8).

`latency:` **is** an axis by weight (`0.0001`) but is empty in practice: the
telemetry reducer never fills the `latency:` namespace, even though
`MetricCount` documents `latency:p95_turn_ms` and `LossProfile.runtime_ms`
already holds a usable number. `aggregate_namespaced_metrics()` promotes
known-but-absent namespaces to `0.0`, so the axis is present and constant —
it can never separate two candidates, and it can never wrongly separate them
either. Filling it is deliberately **not** done here: the `0.0001` weight
would start contributing to the scalar and move every score in the workspace.
That is a scoring change, and it rolls the epoch. Registered in §8.

So the first useful axis set is effectively `drift:`, `cost:`, `rubric:`, and
`schema:`.

### 2.2 Axis values

```python
def axis_values(aggregate: Mapping[str, Any], axes: Sequence[str]) -> dict[str, float]
```

Reads `aggregate["namespace_aggregates"][ns]` for each axis — the
weight-multiplied, sign-folded value the gate already compares. A namespace
absent from the aggregate, or carrying a non-finite value, is **omitted**
rather than defaulted to zero: a missing measurement is not a measurement of
zero, and fabricating one would invent a dominance relation. This mirrors
`regressed_namespaces()`, which skips a namespace it cannot see on both
sides.

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
dominates  = better_any and not worse_any
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
with nothing in common are *incomparable*, not dominant.

The margin band is what makes this a **weak** dominance test rather than a
knife-edge one. Inside `±margin` the two candidates are tied on that axis,
because `promote_margin` is exactly the width at which the loop has agreed a
difference is not noise. A candidate that is a hair better on every axis
therefore dominates nothing, and a candidate that is a hair worse on one axis
is not thereby saved from being dominated.

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
  "axes": ["cost:", "drift:", "latency:", "rubric:", "schema:"],
  "margin": 0.01,
  "champion_generation_id": "v7",
  "updated_round": 9,
  "members": [
    {
      "generation_id": "v5",
      "round_admitted": 6,
      "champion_generation_id": "v4",
      "scalar": 0.5130,
      "axis_values": {
        "cost:": 1.284, "drift:": 0.310, "latency:": 0.0,
        "rubric:": -0.780, "schema:": 0.0
      },
      "beats_champion_on": ["cost:"]
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
  last evaluated against; on a member it is the champion it was admitted
  against. Keeping both means a member never silently re-attributes its
  provenance to a champion that did not exist when it was admitted.
- `members` is sorted by `generation_id`; `retired` by
  `(round_retired, generation_id)`. Stable order, so the file diffs cleanly.
- `axes` and `margin` are echoed from the contract so the file is
  self-describing — a reader does not need `scoring.json` to interpret it.
- `format_version` follows the canonical-record discipline
  (`zicato.epoch._storage.check_record_format`): absent means version 1, a
  higher version is refused rather than misread.
- **A missing file is an empty frontier, never an error.** Every workspace
  written before this feature reads back as `members: [], retired: []`.

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
   calibration probe, not a candidate. It is a no-op re-emission of the
   champion, so it would sit on the frontier as a permanent tie and
   contaminate exactly the record whose job is to hold interesting
   candidates. The multi-challenger path fields the placebo *inside* the
   slate, so this check is load-bearing, not theoretical.

   The rule has a second half on the REFERENCE side: a placebo may not be
   the *champion* either. The gate can crown the arm — that is exactly what
   the CRITICAL `placebo_promoted` health finding exists to catch — and when
   it does, `update_frontier` refuses the whole round rather than measuring
   against a no-op copy of the champion. Otherwise every admission that
   round would attribute its `champion_generation_id` to a generation that
   exists only to test the gate, and real members would be retired against
   a reference the loop is already alarming about.
3. **No namespace regression against the champion.** Evaluated by the gate's
   own logic on the default-on namespaces (`rubric:`, `schema:` per
   `_default_namespace_monotonicity`). A candidate whose rubric collapsed or
   that introduced schema failures is refused however well it did on cost.
4. **It beats the champion by at least `margin` on at least one axis.** A
   candidate that ties or loses everywhere carries no information the
   champion does not already carry. (This also subsumes "the champion does
   not dominate it": beating the champion on an axis by `margin` makes the
   champion `margin`-worse there, so `worse_any` holds for the champion.)
5. **No current member dominates it.**

On admission, any current member the newcomer dominates is retired.

The champion is the **reference**, not a member. The record's whole claim is
"here is what was better than the champion somewhere", which needs the
champion to sit outside the set being compared.

### 4.1 Reuse of the gate's logic, not a copy

Rule 3's namespace check is promoted from `tournament/gate.py`'s private
`_regressed_namespaces()` to a public `regressed_namespaces()` in the same
module, with the private name retained as an alias so no call site changes
behavior. The gate keeps calling the same function it always did; the
frontier calls it too. There is no second implementation of "did this
namespace regress", so the two can never drift apart.

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

Total, deterministic, no I/O. Two passes:

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
a reason can end up pointing at a generation that is no longer on the
frontier. That is intended: `retired` is an append-only evidence trail, not
an index, and the referent is always still findable in the same file (nothing
is ever deleted) or in the epoch's lineage.

Nothing is ever deleted. A member that leaves the frontier moves to `retired`
with the round and the reason, because the interesting question later is not
only "what is on the frontier" but "what was on it and what displaced it".
This is the same evidence discipline the archive-on-overwrite rule applies to
`gen_score.history.jsonl`.

A **promotion keeps the frontier.** The epoch's contract has not changed, so
the earlier measurements are still comparable; only the reference point moved,
which is what the retire pass handles. An **epoch roll starts empty** — a new
contract means new axes, a new margin, and scores that are not comparable to
the old ones.

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
artifact, which is what the `epoch/` package owns.

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

One best-effort call at the **round-settle seam** on each of the two evolve
pipelines, placed after the decision is final (post holdout confirmation,
post integrity block, post operator override) so the champion it evaluates
against is the champion the round actually ended with:

- **gauntlet** (`orchestrator.evolve_once`), after `_finalize_generation` and
  beside the `decision_recorded` emission. Champion is the promoted
  generation on a promote, else the incumbent; aggregates are
  `tournament_result.parent_agg` / `child_agg`, the same two dicts the gate
  decided on and `_cache_gen_score` persists.
- **multi-challenger** (`orchestrator._evolve_multi_challenger`), beside its
  own `decision_recorded` emission. Per-generation aggregates accumulate in
  `_run_matchup` exactly where `_cache_gen_score` already writes them, and
  only when `cache_scores` is set — so an evidence-gate replicate duel cannot
  overwrite the round-scored aggregate the record reads.

Failure of the recorder can never fail a round. It follows the emission
discipline the live index dual-write established: the canonical stores stay
authoritative, and any exception is logged at `debug` and swallowed. The
infra-outage deferral path returns before the seam — a round whose verdict is
meaningless contributes no members.

## 7. What it surfaces

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
```

It joins `EVENT_TYPES`, the `RoundEvent` union, and folds into
`RoundRecord.frontier_updates`. Every log written before the event existed
folds identically — an unknown token already reads back as a raw envelope,
and the new field defaults to empty.

**An additive analytical-index table** (`SCHEMA_VERSION` 12 → 13), following
the `reflections` precedent: a whole new table materialised by the
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
`zicato reindex` re-derives every row from `pareto_frontier.json`, and the
frontier is fully readable with no index at all. A best-effort incremental
upsert runs at the settle seam so a live index does not go stale, mirroring
`ingest_reflection`.

**No UI and no new CLI command in this pass.** `zicato epoch list` is
untouched, the epoch view API payload is unchanged, and no dashboard code is
touched. A payload field with no renderer is a render-conformance violation
waiting to happen, and a `zicato frontier` subcommand moves the `CLI-HELP`
parity golden for a surface nothing has asked for yet. The reader functions
in §5.2 are the supported way to inspect the record today. Both surfaces are
registered in §8.

## 8. Deliberately not built

Everything below would make something *read* the frontier. That is where the
abuse surface in §4 stops being hypothetical, so each is generator-arsenal
work: it ships default-off with an operator-characteristic proof that it
helps, per the campaign philosophy (`docs/design/CAMPAIGN.md`).

- **Show the frontier to the proposer, in bands, beside the genealogy pool.**
  The first consumer, and the first thing that can be gamed — a proposer that
  learns "cut cost and land on the record" optimizes for the record.
- **Recombine pairs from the frontier.** The largest expected gain:
  `mint_recombined_experiment()` already merges two past candidates, and
  frontier members are good parents by construction, since each wins where
  the other loses (merge the cheapest with the most accurate). It is also the
  step that turns the record into lineage, so it needs the record's evidence
  first.
- **Steer one best-of-N slate sample at an under-served axis.** Changes what
  gets proposed; needs a measured under-service signal, which the record is
  the thing that would produce.
- **Make `output:` an axis.** Needs a per-axis noise floor, since a zero
  weight has no sign and no scale.
- **Fill the `latency:` namespace from `LossProfile.runtime_ms`.** A scoring
  change at the `0.0001` default weight: it moves every scalar in the
  workspace and rolls the epoch. Worth doing — it is the axis most likely to
  actually separate candidates, since `drift:` and `cost:` tend to move
  together — but on its own terms, not smuggled in behind a record.
- **A `zicato frontier <epoch>` command and an epoch-view surface.** Cheap,
  but they are the operator-facing half of the issue's second motivation
  (letting a human pick "80% of the accuracy at 10% of the cost"), which
  deserves its own design pass rather than a bare JSON dump.

## 9. Known assumption

**Cross-round comparability.** `rotate_holdout` defaults to `True`, so two
rounds may score their candidates against different board entries. A frontier
that spans rounds assumes those raw values are comparable. The assumption is
probably safe — `promote_margin` is fitted to absorb exactly that noise, and
the scalar comparison the gate already performs carries the identical
exposure — but nobody has confirmed it, and the record is the artifact that
would let someone confirm it. It is written down here rather than resolved.
