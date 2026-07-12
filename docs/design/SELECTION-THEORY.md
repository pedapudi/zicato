# Selection theory — winner-resolution & rating under noise

> **Status.** RESEARCH / DESIGN NOTE. **Nothing in this document is
> implemented.** It is a literature survey of tournament-solution,
> social-choice, and selection-under-noise methods, mapped onto zicato's
> regime, with a ranked recommendation. The recommendation is framed as
> *future* work: the endorsed methods become new **`tournament.params`**
> (a `resolver` knob and a `rating` knob) layered *underneath* the five
> existing structures — **not** new top-level structures. No source,
> config schema, or test in the tree changes because of this note.

This is the companion to two shipped docs:

- [`SELECTION.md`](SELECTION.md) — the decision theory of the promote
  gate and the king-of-the-hill gauntlet (the *why* of the existing
  loop).
- [`TOURNAMENT-STRUCTURES.md`](TOURNAMENT-STRUCTURES.md) — the
  `SelectionStrategy` abstraction and the five shipped structures
  (gauntlet, swiss, single_elim, double_elim, racing).

Those two answer *which duels run* (scheduling). This note answers two
different questions that sit *beneath* scheduling:

1. **Winner resolution** — given a matrix of completed duels (possibly
   cyclic, because the loss is noisy), *which contestant wins?*
2. **Rating** — given noisy and replicated duels, *what is each
   contestant's underlying strength, and how confident are we?*

---

## 1. The reframe: zicato's gap is not more brackets

The five shipped structures are all **schedulers**: they decide which
pairwise duels to run and in what order. They differ in their schedule,
not in how they read the results. Once duels complete, every structure
collapses the matrix the same naive way — Copeland count (swiss),
single-survivor knockout (the elim brackets), or rank-by-scalar within a
rung (racing).

That collapse is the weak link, for two reasons specific to zicato:

- **The loss is noisy and absolute.** Each duel yields a scalar loss
  (lower = better) measured by running a candidate over the board. Two
  re-runs of the *same* pair can disagree. So the duel matrix can be
  **non-transitive**: A beats B, B beats C, C beats A — a Condorcet
  cycle that is, in zicato's case, very often a *noise artifact* rather
  than a genuine rock-paper-scissors structure.
- **The field is small and the runs are expensive.** Typical fields are
  single-digit (`field_size` defaults to 2–8). We can afford a
  polynomial-time resolver over the matrix many times over; we *cannot*
  afford to ignore the margins (the loss gaps), which carry most of the
  signal in a small noisy field.

So the missing layers are:

- A **winner-resolution layer** — turn a (possibly cyclic) duel matrix
  into a single proposed winner, principled under cycles.
- A **rating layer** — read noisy/replicated duels into per-contestant
  strengths *with confidence intervals*, so we know *where* to spend the
  next replication.

Both slot **underneath** the existing structures. A swiss or round-robin
scheduler produces the matrix; the resolver reads it; the rating layer
informs how many replicates each duel deserves. Crucially:

> **Every method here only PROPOSES a winner. The promote gate still
> owns promotion.** The protected-incumbent invariant
> ([`SELECTION.md`](SELECTION.md), [`TOURNAMENT-STRUCTURES.md`](TOURNAMENT-STRUCTURES.md))
> is untouched: a resolver names an internal leader; that leader is then
> run through the *unchanged* champion-gate against the incumbent, and is
> promoted only if it clears `promote_margin` with no per-task
> regression. A resolver can crown the wrong leader and the worst case is
> a wasted confirmation duel — never an unsafe promotion.

---

## 2. The operating rule: replicate first, resolve second

The single most important consequence of the noise reframe:

> **Replicate first, resolve second.** Most cycles zicato will ever see
> are noise artifacts that replication dissolves. Spend the replication
> budget to *collapse* the matrix toward transitivity before reaching
> for any cycle-resolver; invoke a resolver only on the **residual**
> cycle that survives replication.

This inverts the usual social-choice framing, where the ballot matrix is
*given* and a cycle is a genuine feature of voter preferences. In zicato
the matrix is *measured*, and a cycle is first-and-foremost evidence that
two contestants are close enough that one more sample would likely order
them. The rating layer (§7) is what tells us this: when two contestants'
strength confidence intervals overlap, the duel between them is the one
to replicate. Only when the CIs are *separated* and the cycle *persists*
is it a real preference cycle, and only then does the resolver earn its
keep.

---

## 3. Set solutions (polynomial — candidates to BUILD or already shipped)

A *tournament solution* maps a duel matrix to a **set** of acceptable
winners. All of the following are polynomial-time and therefore tractable
at any field size zicato will ever run. (See the Handbook of
Computational Social Choice, ch. 3, "Tournament Solutions," for the
formal treatment cited throughout this section.)

### 3.1 Condorcet winner

**Definition.** A contestant who beats *every* other contestant
head-to-head. If one exists it is unique and is the unambiguous winner;
every sensible method below returns it when it exists.

**Tractability.** Trivially polynomial (O(n²) — one pass over the
matrix).

**In zicato's regime.** When replication has done its job, the field
usually *has* a Condorcet winner: one challenger that genuinely beats all
others. The whole point of a resolver is to behave gracefully when it
does *not*.

**Verdict.** **BUILD (implicitly).** Not a method to select — it is the
fast path every resolver below already collapses to. Check for it first
(O(n²)); if present, skip the resolver entirely.

### 3.2 Smith set (top cycle)

**Definition.** The smallest non-empty set of contestants who, as a
group, beat everyone outside the set. Equivalently, the top "tier" of the
dominance relation: no one outside the Smith set beats anyone inside it.
When a Condorcet winner exists, the Smith set is exactly that one
contestant.

**Tractability.** Polynomial (O(n²) via the condensation of the dominance
graph into strongly-connected components, then take the top component).

**In zicato's regime.** Cheap and load-bearing as a **prune**: the
champion can only ever be among the Smith set, so any contestant outside
it is provably dominated and need not be considered for promotion. This
shrinks the field a downstream resolver must reason about, often to a
single element.

**Verdict.** **BUILD (as a front-end prune).** Run it first, O(n²); pass
only the Smith set to the resolver. Recommendation §8 below.

### 3.3 Schwartz set (GETCHA / top set)

**Definition.** The union of the *minimal* dominant sets — the minimal
sets from which nothing outside beats anything inside. Closely related to
the Smith set; they coincide whenever there are no pairwise ties, which
is essentially always true for zicato (a real-valued loss gap is
generically non-zero).

**Tractability.** Polynomial (same SCC machinery as Smith).

**In zicato's regime.** Because exact ties in a continuous loss are
measure-zero, the Schwartz set and the Smith set are the same set in
practice. It buys nothing over Smith here.

**Verdict.** **SKIP (subsumed by Smith).** Mentioned for completeness;
under continuous losses it is the Smith set.

### 3.4 Copeland

**Definition.** Score each contestant by the number of duels it wins
(optionally minus duels it loses); the winner is the highest score. A
simple, transparent count.

**Tractability.** Polynomial (O(n²)).

**In zicato's regime.** **Already shipped** — this is exactly how the
`swiss` structure ranks its field (standing = Copeland score = duels won,
tie-broken by mean scalar; see
[`TOURNAMENT-STRUCTURES.md`](TOURNAMENT-STRUCTURES.md) §3.4). Its
weakness is that it is **margin-blind**: a 0.001 win and a 0.5 win both
count as one. In a small noisy field that throws away most of the signal,
and it is sensitive to clones (adding near-duplicate weak candidates can
shift the count).

**Verdict.** **ALREADY BUILT (swiss).** Keep it as the cheapest baseline
resolver, but it is dominated by the margin-aware methods (§5) for
zicato's loss-gap-rich regime.

### 3.5 Uncovered set (Landau set)

**Definition.** Drop every contestant that is "covered" — A covers B if A
beats B *and* A beats everyone B beats. The uncovered set is what
survives. Every uncovered contestant reaches every other in at most two
steps (the "two-step" or kings property).

**Tractability.** Polynomial (O(n²·…), matrix-multiplication-bounded).

**In zicato's regime.** A reasonable refinement of Smith, but for the
tiny fields zicato runs it rarely cuts more than Smith already did, and
it is still margin-blind. Useful conceptually (it guarantees the winner
is a "king"), marginal in practice.

**Verdict.** **SKIP (low marginal value).** Polynomial and harmless, but
Smith + a margin-aware resolver dominates it for our field sizes.

### 3.6 Bipartisan set

**Definition.** The support of the unique Nash equilibrium of the
symmetric zero-sum "tournament game" played on the *unweighted* dominance
matrix (each entry +1/−1/0). A theoretically elegant refinement: it is a
subset of the uncovered set and has strong axiomatic properties.

**Tractability.** Polynomial (a linear program).

**In zicato's regime.** It uses only the *sign* of each duel, discarding
the loss margins — the very signal zicato has in abundance. Its
*weighted* generalization is precisely **maximal lotteries** (§6), which
keeps the margins. So the bipartisan set is the margin-blind special case
of the randomized method we actually want.

**Verdict.** **SKIP in favor of its weighted form.** Maximal lotteries
(§6) generalize it and use zicato's margins.

---

## 4. NP-hard set solutions (SKIP — explained)

These tournament solutions are theoretically attractive but
computationally intractable, and for zicato they buy nothing the
polynomial methods above do not.

### 4.1 Slater set

**Definition.** The Slater ranking is the total order that disagrees with
the fewest pairwise duels (minimum number of "upsets" to make the matrix
transitive); the Slater set is the set of possible Slater winners.

**Tractability.** **NP-hard.** Finding the minimum-disagreement order is
equivalent to the linear-ordering / minimum-feedback-arc-set problem.

**Verdict.** **SKIP.** NP-hard, margin-blind (it counts upsets, not
gaps), and for zicato's tiny residual cycles it returns essentially what
Ranked Pairs returns at polynomial cost. No practical gain.

### 4.2 Banks set

**Definition.** The set of winners of *maximal transitive subsets* built
greedily — the top elements of all maximal "chains" you can grow from the
dominance relation.

**Tractability.** Deciding membership is **NP-hard** (though a single
Banks winner can be found in polynomial time, enumerating the set is
hard).

**Verdict.** **SKIP.** Intractable to characterize, margin-blind, and
without a clean operational story for a protected-incumbent loop.

### 4.3 Tournament Equilibrium Set (TEQ)

**Definition.** A recursive, self-referential refinement: TEQ of a
tournament is defined via the TEQ of its dominant subsets. It was the
finest "well-behaved" tournament solution conjectured to satisfy a strong
stability axiom.

**Tractability.** **NP-hard**, and worse: Brandt and co-authors
**disproved Schwartz's conjecture** that TEQ is well-behaved, exhibiting
a counterexample tournament (a 24-vertex construction). Because the
conjecture fails, TEQ **loses the monotonicity / stability property**
that motivated it in the first place.

**Verdict.** **SKIP, emphatically.** NP-hard *and* now known not to have
the axiomatic guarantees that were its only reason to prefer it. There is
no regime in which zicato should use TEQ.

### 4.4 Minimal Covering Set

**Definition.** The (unique) smallest set that is "stable" under the
covering relation — a covering-relation analogue of the bipartisan set's
stability.

**Tractability.** **NP-hard** to compute in general.

**Verdict.** **SKIP.** Its tractable cousin — the bipartisan set / maximal
lotteries — gives the same flavor of guarantee at polynomial cost while
using margins.

---

## 5. Ranking / Condorcet-completion methods (the resolver tier)

These take the duel matrix and produce a full ranking (and thus a
winner) that is **Condorcet-consistent** — it returns the Condorcet
winner whenever one exists. Critically, the two polynomial members use
**margins** (zicato's loss gaps), and both are **cloneproof** (immune to
near-duplicate candidates), which the count-based Copeland is not.

### 5.1 Kemeny–Young

**Definition.** The ranking that maximizes pairwise agreement summed over
all pairs (equivalently, minimizes total Kendall-tau disagreement,
margin-weighted). The "maximum-likelihood" ranking under a classic noise
model.

**Tractability.** **NP-hard** (it is the weighted minimum-feedback-arc-set
problem). Exactly solvable only for very small n.

**In zicato's regime.** For a *tiny residual* field (say ≤ 6 after the
Smith prune) it is exactly solvable by brute force and is statistically
principled. But "at scale" it is intractable, and Ranked Pairs / Schulze
give a very similar answer in polynomial time.

**Verdict.** **SKIP at scale** (use only as an optional exact tie-break on
a Smith set of ≤ ~6, if ever). Do not make it the default resolver.

### 5.2 Ranked Pairs (Tideman)

**Definition.** Sort all pairwise duels by **margin**, strongest first.
"Lock in" each pairwise result in that order, *skipping* any that would
create a cycle with the results already locked. The resulting acyclic
relation has a unique source — the winner. Decisive, deterministic, and
fully auditable (you can read off *exactly which* duels were locked and
which were skipped).

**Tractability.** **Polynomial** (O(n² log n) to sort, O(n³)-ish to
lock).

**In zicato's regime.** This is the strongest fit. It is
Condorcet-consistent, **uses the loss margins directly** (the strongest
duels — the most separated, least likely to be noise — are locked first,
exactly the right priority for a noisy measurement), **cloneproof**, and
**monotone**. The lock/skip trace is an auditable artifact that maps
cleanly onto zicato's journal/dashboard ethos: every resolution is
explainable as "we trusted the most-separated duels and skipped the ones
that would have made a cycle."

**Verdict.** **BUILD — top recommendation.** This is the endorsed default
resolver (§8 #1).

### 5.3 Schulze (beatpath)

**Definition.** For each ordered pair (A, B) compute the **strongest
beatpath** — the path A → … → B whose *weakest* margin link is as strong
as possible. A is ranked above B if its strongest path to B beats B's
strongest path to A. Winner = the contestant whose beatpaths dominate.

**Tractability.** **Polynomial** (a Floyd–Warshall-style widest-path
computation, O(n³)).

**In zicato's regime.** Also excellent: Condorcet-consistent,
margin-aware, cloneproof, monotone. It tends to agree with Ranked Pairs;
the differences are rare and subtle. The reason to *prefer Ranked Pairs*
for zicato is **auditability**: Ranked Pairs' lock/skip trace is more
directly human-legible than a beatpath-strength matrix, and zicato values
an explainable promotion proposal. Schulze is the natural second choice
if a beatpath formulation ever proves more convenient.

**Verdict.** **BUILD-CAPABLE (second choice).** Offer as an alternate
`resolver` value; default to Ranked Pairs for auditability.

---

## 6. Randomized resolution — maximal lotteries

**Definition.** Treat the *margin matrix* as a symmetric zero-sum game
(the payoff of A over B is their loss-gap, signed) and compute its **Nash
equilibrium** — a probability distribution ("lottery") over contestants.
The result is a randomized winner. Its defining virtue: when a Condorcet
winner exists, the maximal lottery puts **all** its mass on that winner
(it degenerates to the deterministic correct answer); when there is a
genuine cycle, it spreads mass over the cycle in proportion to how the
margins balance.

**Tractability.** **Polynomial** (a linear program; it is the
margin-weighted generalization of the bipartisan set, §3.6).

**In zicato's regime.** This is the principled way to handle a cycle that
has **survived replication** — a *real* rock-paper-scissors structure,
not a noise artifact. Rather than force a deterministic (and arguably
arbitrary) pick among three mutually-cyclic contestants, it hands back a
distribution; zicato can sample it to choose which contestant to run
through the champion-gate, and the choice is game-theoretically optimal
in expectation. Because it collapses to the Condorcet winner whenever one
exists, it is *safe* to apply unconditionally — but it only differs from
the deterministic resolvers precisely on residual cycles, which is the
only place we want randomness.

**Verdict.** **BUILD — for residual cycles only** (§8 #3). Reach for it
*after* replication has failed to break the cycle, not before.

---

## 7. Rating from pairwise results (the noise backbone)

Set/ranking solutions read a *fixed* matrix. A rating model instead reads
the *raw, replicated* duel outcomes into per-contestant **latent
strengths with uncertainty** — which is what tells zicato *where to spend
its next replication*. This is the layer that makes "replicate first" (§2)
actionable.

### 7.1 Bradley–Terry

**Definition.** A statistical model: each contestant i has a latent
strength θᵢ, and the probability that i beats j is the logistic
σ(θᵢ − θⱼ). Fit the θ's by maximum likelihood over all observed (and
replicated) duels.

**Tractability.** **Convex MLE** — a single global optimum, solved
reliably and cheaply at zicato's field sizes. It **natively absorbs
replication** (each replicate is just another observation in the
likelihood) and **partial schedules** (not every pair need be played),
and it yields **confidence intervals** on each strength.

**In zicato's regime.** This is the rating backbone. The CIs are the
operational payoff: when two contestants' strength intervals **overlap**,
the duel between them is statistically unresolved and is exactly where the
next replication should go (CI-overlap-driven replication budgeting). When
the intervals **separate**, that pair is settled and further replication
there is wasted. It turns "replicate first" from a slogan into a
schedule. It also gracefully handles the small noisy field: with a
half-dozen contestants and a handful of replicates each, the MLE is
stable and the CIs are meaningful.

**Verdict.** **BUILD — the rating recommendation** (§8 #2). Drives
replication budgeting; its point estimates also give a clean
margin-bearing ranking that feeds the §5 resolvers.

> **Status — implemented: the BT rating fold.** The batch MLE
> (`src/zicato/selection/rating.py::fit_bradley_terry`) is now also the
> engine of the index-side visibility rating (`src/zicato/index/elo.py`):
> at every reindex/ingest it is re-fit over the de-duplicated persisted
> match ledger and written to `generations.elo` / `elo_se` / `elo_games`
> on the conventional Elo scale (`1500 + θ·400/ln 10`), replacing the
> earlier sequential margin-K approximation (§7.2's order-dependence and
> missing CIs were exactly its defects). Displayed in the standings /
> gens roster / candidate dossier; **visibility only — it never touches
> the gate or the selection path**. Known hole: racing intermediate rungs
> persist survivor/cut sets with no named pairwise winner, so rung cuts
> contribute zero games (the Plackett–Luce set-rating below remains
> future work).

### 7.2 Elo

**Definition.** An *online, incremental* update rule (each game nudges the
two players' ratings) that is, in the batch limit, a streaming
approximation to Bradley–Terry's logistic model.

**Tractability.** Trivial (a constant-time update per game), but it is
*order-dependent*, has a tunable K-factor, and gives **no native
uncertainty estimate**.

**In zicato's regime.** zicato evaluates in **batches** (a tournament
resolves a whole matrix at once), not in a long online stream, so Elo's
only advantage — incrementality — does not apply, while its
disadvantages (order sensitivity, no CIs) do. Bradley–Terry is the same
model fit properly.

**Verdict.** **SKIP (dominated by Bradley–Terry).** In a batch,
margin-rich regime, fit the MLE directly.

### 7.3 TrueSkill

**Definition.** A Bayesian rating system (skill modeled as a Gaussian,
updated by approximate message passing) designed for **multiplayer,
multi-team** matches and online play; it natively carries uncertainty.

**Tractability.** Tractable, but more machinery than zicato needs.

**In zicato's regime.** Its headline strengths — many-player free-for-alls
and online updating — are features zicato does not use (duels are
strictly pairwise, evaluation is batched). For pairwise batch data with
uncertainty, Bradley–Terry (optionally with a Bayesian prior) delivers
the same value with far less apparatus.

**Verdict.** **SKIP (over-engineered for pairwise batch).** Note it as the
method to revisit *only if* zicato ever runs genuine n-way (≥3
simultaneous contestants) board runs.

---

## 8. The recommendation (ranked, all FUTURE work)

Ranked, with the operating rule woven through. **None of this is
implemented.** Each becomes a `tournament.params` knob (§9), layered on a
round-robin/swiss scheduler — not a new top-level structure.

1. **Ranked Pairs (Tideman) as the winner-resolution layer over
   swiss/round-robin.** Deterministic, Condorcet-consistent, margin-aware,
   cloneproof, and *auditable* (the lock/skip trace explains every
   resolution). This is the default resolver.
2. **Bradley–Terry rating as the noise backbone.** Strengths + confidence
   intervals from replicated/partial duels; **CI overlap drives
   replication budgeting** (replicate the unresolved pairs, stop on the
   separated ones).
3. **Maximal lotteries for cycles that SURVIVE replication.** When a real
   (non-noise) cycle persists, return the Nash distribution over the cycle
   rather than an arbitrary deterministic pick; it degrades to the
   Condorcet winner whenever one exists, so it is safe by default.
4. **Smith-set prune in front (O(n²)).** Discard provably-dominated
   contestants before any resolver runs; often collapses the field to one.

**Explicitly skipped:**

- **Slater, Banks, TEQ, Kemeny-at-scale** — NP-hard, no practical gain
  over the polynomial margin-aware methods (and TEQ has lost its
  axiomatic justification, §4.3).
- **Markov/Elo and TrueSkill** — dominated by Bradley–Terry in zicato's
  **batch, margin-rich, strictly-pairwise** regime.

**The operating rule, restated:** **replicate first, resolve second.**
Most zicato cycles are noise artifacts that replication dissolves; only
invoke a cycle-resolver on the *residual*. And — the load-bearing safety
property — **every one of these only PROPOSES a winner; the gate still
owns promotion**, so the protected-incumbent invariant
([`SELECTION.md`](SELECTION.md)) is untouched.

---

## 9. How this would slot in (future config sketch — NOT implemented)

The endorsed methods are **resolvers and a rating model**, layered on the
existing schedulers — **not** new structures. The natural surface is two
new optional keys in the `tournament.params` block that swiss (or a new
round-robin) reads:

```jsonc
// FUTURE / SPECULATIVE — no loader, strategy, or test reads these today.
"tournament": {
  "structure": "swiss",            // an existing scheduler produces the matrix
  "params": {
    "field_size": 6,
    "replicates": 2,
    "resolver": "ranked_pairs",    // none | copeland | ranked_pairs | maximal_lottery
                                   //   copeland == today's swiss behaviour
    "rating": "bradley_terry"      // none | bradley_terry
                                   //   when set, CI overlap drives extra replication
  }
}
```

- `resolver` selects the §5/§6 winner-resolution layer; `copeland` is the
  current swiss behaviour (the backwards-compatible default), so adding
  the knob changes nothing until an operator opts in.
- `rating` selects the §7 backbone; `none` is today's behaviour.
- The Smith-set prune (§8 #4) would run unconditionally inside any
  non-`none` resolver — it is a cheap correctness/speed step, not an
  operator choice.
- **The champion-gate, the contract-hash treatment, and the
  `SelectionStrategy` seam are all unchanged.** A resolver/rating choice
  would fold into the contract hash exactly as the existing params do
  (it changes *what a promotion means*), and would roll the epoch on
  change — same rationale as
  [`TOURNAMENT-STRUCTURES.md`](TOURNAMENT-STRUCTURES.md) §4.1.

Again: **this section is a sketch of where the methods would attach.** No
such keys exist in the loader, the strategies, or the tests today.

---

## 10. Visual language (Console — design-only, NOT built)

> **Status.** As speculative as the rest of this note. The duel matrix is
> not even surfaced in the dashboard today. This section records *how each
> method would render* in the existing Console idiom, so the dashboard
> design travels with the math.

These methods all derive from one object — the **pairwise duel matrix** —
so the idiomatic answer is **one honest substrate plus a switchable
resolver lens**, not a gallery of unrelated charts. Every figure below
stays in the Console language: Tufte small-multiples and signed
dot-plots, diverging **green = improvement / red = regression**, the
**champion always a reference rule (never an ordinary mark)**, the **gate
margin always a shaded band**, direct labeling over legends, and all of it
computed server-side from the *settled* round so it renders once
(digest-gated — no rebuild on a no-op heartbeat). And every lens ends at
the same caption: *…which only PROPOSES a winner; the gate still decides.*

### 10.1 The substrate — the duel grid

A contestant×contestant **margin matrix**, champion pinned top-left behind
a reference rule, ordered by current standing. Each cell is a small
*signed dot* (Tufte, not a heavy heatmap fill): green = row beat column,
red = lost, size = |loss-margin|. The diagonal carries each contestant's
own loss tick. **Unplayed pairings** (racing/elim never run them) render
as faint hatch — honest about coverage. Every solution concept below is an
**overlay or derived small-multiple on this one grid**, switched via the
⌘K palette (`resolve with: Copeland · Ranked Pairs · maximal lottery`).

```
        chmp  c1   c2   c3   c4
 chmp │   ·   ●    ●    ○    ●      ● row beats col (green)
  c1  │   ○   ·    ●    ●    ●      ○ row loses    (red)
  c2  │   ○   ○    ·    ●    ◌      ◌ unplayed (faint hatch)
  c3  │   ●   ○    ○    ·    ●      size = |margin|
  c4  │   ○   ○    ▦    ○    ·
       └ champion reference rule
```

### 10.2 Set solutions — a cut on the reordered grid

- **Smith set / top cycle** → reorder so the dominating strongly-connected
  block is top-left, then draw a single horizontal **cut rule** (the
  racing survival-funnel idiom) labeled `Smith set · k of N`; everything
  below dims. The champion's position relative to the cut shows at a glance
  whether the incumbent is even in contention.
- **Uncovered set** → an annotation column, not a graph: each row gets a
  tiny `covered-by` count bar; uncovered rows (count 0) stay lit, covered
  rows dim with a caret pointing at their coverer. No node-link diagram —
  that would be chartjunk.

### 10.3 Bipartisan set + maximal lotteries — the probability lollipop

These share the zero-sum-game Nash mixed strategy, so **one figure covers
both**: a horizontal **lollipop dot-plot** where length = each
contestant's probability mass in the optimal lottery. The *support* (the
non-zero lollipops) **is** the bipartisan set. A Condorcet winner is
unmistakable — one full bar at p=1; a genuine cycle renders **as mass
shared across 2–3 contestants**, i.e. the figure shows the cycle directly.

```
maximal lottery (margin game)
 c2  ████████████████  .61   ← bipartisan-set support
 c1  ██████████        .39
chmp │·                .00   (champion reference rule)
 c3                    .00   ← excluded (covered)
 c4                    .00
       one full bar ⇒ Condorcet winner; spread ⇒ true cycle
```

### 10.4 Ranking methods — the lock-in waterfall

- **Ranked Pairs** → its algorithm *is* a waterfall, so reuse the
  loss-floor waterfall motif directly: duels sorted by margin, longest
  signed bar on top, each row a green/red magnitude bar with a state glyph
  — `✓ locked` or `⊘ skipped (would close a cycle)`. The **discarded edge
  is the auditable hero of the view**: the one row marked "dropped — weakest
  margin in the cycle." The resolved DAG's source is the proposed winner.

```
Ranked Pairs · lock-in order (by margin)
  c2 ▸ c4   ██████████ +.42  ✓ locked
  c1 ▸ c3   ███████    +.31  ✓ locked
  c2 ▸ c1   █████      +.22  ✓ locked
  c3 ▸ c2   ███        +.14  ⊘ skipped — closes c1▸c3▸c2▸c1
  ⇒ resolved winner: c2
```

- **Schulze** → the alternate lens on the *same* data: a small-multiple of
  **beatpath breadcrumbs**, one strip per rival (`c2 ▸ c4 ▸ c1`), strip
  width = path strength (weakest link). A ⌘K toggle off the Ranked-Pairs
  view, not its own panel.
- **Kemeny–Young** → just **reorder the duel grid into the Kemeny order**
  and light the residual upset cells *below the diagonal* in red; their
  count is the Kemeny distance being minimized. "The order that pushes red
  below the diagonal." Flagged niche (tiny fields only).

### 10.5 Bradley–Terry — the rating backbone: CI dot-plot + replication heat

A latent-**strength dot-plot with confidence whiskers**, champion as the
reference rule and the **gate margin as a shaded band** to its right that a
challenger must clear. The zicato-honest move: **overlapping CIs dim and
flag "not yet separated → replicate"**, and a thin adjacent column shows
replicate-count per contestant so you can see *where the next duel should
go*. This is the visual form of *replicate-first, resolve-second* (§2, §7).

```
Bradley–Terry strength  (— = 95% CI)        replicates
 c2     ├──●──┤                ░░gate░░          ██   6
 c1   ├───●───┤                                  ██   6
chmp ──────●──│ reference                        ████ 12
 c3  ├─────●─────┤  ⚠ overlaps c4 → replicate    ▪    2
 c4  ├────●────┤   ⚠                             ▪    2
                  └ separated dots ⇒ trustworthy order
```

### 10.6 What ties it together

One substrate (the duel grid); a **⌘K resolver toggle** that swaps the
overlay and re-highlights the proposed winner consistently; Copeland keeps
its existing swiss ladder; the new lenses are the lollipop
(bipartisan/lottery), the lock-in waterfall (Ranked Pairs/Schulze), the
grid-reorder (Smith/Kemeny), and the CI dot-plot (Bradley–Terry). The
champion is forever a reference rule and the gate forever a shaded band, so
the protected-incumbent invariant is **visible, not merely asserted**.

---

## 11. Summary table

| Method | Family | Tractable? | Condorcet-consistent? | zicato verdict |
|---|---|---|---|---|
| Condorcet winner | set (fast path) | P (O(n²)) | — (it *is* the winner) | BUILD (check first) |
| Smith set (top cycle) | set | P (O(n²)) | yes (= winner if one) | **BUILD — front prune** |
| Schwartz set | set | P | yes | SKIP (= Smith under continuous loss) |
| Copeland | set / count | P (O(n²)) | yes | **ALREADY BUILT (swiss)**; margin-blind |
| Uncovered (Landau) set | set | P | yes | SKIP (low marginal value, margin-blind) |
| Bipartisan set | set (LP) | P | yes | SKIP (margin-blind; use maximal lotteries) |
| Slater | set / ranking | **NP-hard** | yes | SKIP |
| Banks | set | **NP-hard** (membership) | yes | SKIP |
| TEQ | set | **NP-hard** + lost stability | yes | **SKIP, emphatically** |
| Minimal Covering Set | set | **NP-hard** | yes | SKIP |
| Kemeny–Young | ranking | **NP-hard** | yes | SKIP at scale |
| **Ranked Pairs (Tideman)** | ranking | **P** | **yes** | **BUILD — top resolver** |
| Schulze (beatpath) | ranking | P (O(n³)) | yes | BUILD-capable (2nd choice) |
| Maximal lotteries | randomized | P (LP) | yes (degrades to it) | **BUILD — residual cycles** |
| **Bradley–Terry** | rating | P (convex MLE) | — (rating, not a rule) | **BUILD — rating backbone** |
| Elo | rating | trivial (online) | — | SKIP (dominated by BT) |
| TrueSkill | rating | tractable | — | SKIP (over-engineered for pairwise batch) |

---

## 12. Citations

Authoritative sources for the methods above.

- **Tournament solutions (survey, ch. 3)** — Brandt, Brill, Harrenstein,
  *Handbook of Computational Social Choice*, ch. 3 "Tournament
  Solutions." <https://pub.dss.in.tum.de/brandt-research/tsolutions.pdf>
- **Condorcet winner** — <https://en.wikipedia.org/wiki/Condorcet_winner_criterion>
- **Smith set (top cycle)** — <https://en.wikipedia.org/wiki/Smith_set>
- **Copeland's method** — <https://en.wikipedia.org/wiki/Copeland%27s_method>
- **Uncovered set / Landau set** — <https://en.wikipedia.org/wiki/Landau_set>
- **Bipartisan set** — <https://en.wikipedia.org/wiki/Bipartisan_set>
- **Slater set / Slater ranking** — <https://en.wikipedia.org/wiki/Slater_determination_method>
- **Banks set** — covered in the Brandt/Brill/Harrenstein survey above
  (Banks section).
- **Tournament Equilibrium Set; disproof of Schwartz's conjecture** —
  Brandt et al., "A counterexample to a conjecture of Schwartz." See the
  TEQ section of <https://en.wikipedia.org/wiki/Tournament_solution> and
  the survey above.
- **Kemeny–Young method** — <https://en.wikipedia.org/wiki/Kemeny%E2%80%93Young_method>
- **Ranked Pairs (Tideman)** — <https://en.wikipedia.org/wiki/Ranked_pairs>
- **Schulze method (beatpath)** — <https://en.wikipedia.org/wiki/Schulze_method>
- **Maximal lotteries** — <https://en.wikipedia.org/wiki/Maximal_lotteries>
- **Bradley–Terry model** — <https://en.wikipedia.org/wiki/Bradley%E2%80%93Terry_model>
- **Elo rating system** — <https://en.wikipedia.org/wiki/Elo_rating_system>
- **TrueSkill** — <https://en.wikipedia.org/wiki/TrueSkill>

---

## 13. Cross-references

| Topic | Document |
|---|---|
| Why the gauntlet is the default; the promote gate; decision theory | [`SELECTION.md`](SELECTION.md) |
| The `SelectionStrategy` seam + the five shipped schedulers | [`TOURNAMENT-STRUCTURES.md`](TOURNAMENT-STRUCTURES.md) |
| How a run becomes the scalar loss these methods consume | [`SCORING.md`](SCORING.md) |
| Operator-facing: choosing + configuring a structure (and the replicate-first rule) | `skills/zicato-design-tournament-structure/SKILL.md` |
