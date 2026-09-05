---
name: zicato-design-tournament-structure
description: Choose and configure a zicato per-epoch tournament structure — gauntlet (default), swiss, single_elim, double_elim, racing — and its params (field_size, replicates, swiss rounds_n, racing eta/board_fraction/rung0_board_size). Use when an epoch has more than one challenger to select among, when a single-challenger gauntlet is too noisy, or when picking the best of a large candidate field cheaply; explains the decision guide, the noise/incumbent design principles, the scoring.json `tournament` block, and that changing it rolls the epoch.
---

# Designing a zicato tournament structure

The **tournament** is how zicato turns a field of proposed challengers into
at most one promotion. The default is the **gauntlet**: one champion, one
challenger, one full-board duel, promote-on-gate — the historical
king-of-the-hill loop. When the proposer can emit *several* challengers per
round, or when a single duel is too noisy to trust, an epoch can select a
different **structure**: `racing`, or — with `experimental.tournament_structures`
set to `true` in `scoring.json` — the experimental `swiss`, `single_elim`, or
`double_elim`.

The structure is part of the **evaluation contract** (it is a field of
`ScoringWeights`, folded into the contract hash). Changing the structure or
any of its params **rolls the epoch** — see "Changing the structure rolls the
epoch" below. Sibling skills — the design companions:
`zicato-design-boards` (a board discriminating enough that the field actually
separates) and `zicato-design-judges` (what the loss measures); and the
operational/loop skills: `zicato-tune-scoring` (the gate + loss weights this
consumes), `zicato-author-board` (the board the field is scored on),
`zicato-manage-epochs-and-rounds` (the round model this lives in),
`zicato-evolve` (the loop that runs it), `zicato-analyze-epoch` (reading the
standings/bracket afterward). Spec:
[TOURNAMENT-STRUCTURES.md](../../docs/design/TOURNAMENT-STRUCTURES.md),
[SELECTION.md](../../docs/design/SELECTION.md).

> **Two different "rounds".** An OUTER evolve round (`--rounds N`) runs ONE
> tournament. A non-gauntlet tournament has its own INNER rounds (swiss
> `rounds_n`, an elim bracket's rounds, a racing rung). Be explicit about
> which you mean — they are unrelated counters.

## The load-bearing invariant: the GATE, rather than the bracket, protects the incumbent

Every structure consumes the **same, unchanged** promote gate
(`zicato.tournament.gate.evaluate_gate`). The bracket/Swiss/racing logic only
*schedules* duels and *interprets* each duel's verdict — it never re-decides
a duel. Two consequences drive every design choice:

- **The champion is carried, never knocked out by the bracket.** A challenger
  is promoted only by clearing the real champion-gate against the reigning
  champion. Swiss/elim/racing crown an internal *leader/survivor*, then run
  ONE final champion-gate duel; if the leader does not actually beat the
  incumbent, the champion stands. Bracket position never promotes anyone.
- **Replication — not bracket shape — is the noise lever.** Loss is a noisy
  absolute measurement. The robust way to trust a duel is to run it more
  times (`replicates`) rather than to give a candidate a "second life" in a losers'
  bracket. `replicates = 2` is the base default every structure inherits —
  gauntlet included, the noise-aware default. (`racing` is the exception: it
  gets replication intrinsically from escalating board slices, so it pins
  `1`.) Pin `"replicates": 1` for the historical single-run duel, which is
  what deterministic harnesses do.

## Decision guide — which structure, when

This is candidate selection under **noisy, expensive, absolute-loss**
evaluation with a protected incumbent. Map the situation to a structure:

| Situation | Structure | Why |
|---|---|---|
| One challenger per round; cheapest possible 1-vs-1 | **gauntlet** | The default. One full-board duel per replicate, promote-on-gate. Raise `replicates` above its default 2 if the verdict is still too noisy — no structure change needed. |
| A field, and you want a full RANKING in few duels | **swiss** (experimental) | Needs `experimental.tournament_structures = true`. Fixed `rounds_n` Swiss rounds rank the whole field by Copeland (duels won); no elimination, so every candidate is rated. Cheap, non-adaptive. |
| A field, and you only need the single best (knockout) | **single_elim** (experimental) | Needs the same opt-in. A bracket over the challengers halves the field each round; the survivor faces the champion. Fewer duels than swiss, but loses the full ranking. |
| Same, but you want a "second chance" against an upset | **double_elim** (experimental) | Needs the same opt-in. Winners' + losers' bracket; eliminated only on the SECOND node loss. Offered for completeness — prefer raising `replicates` on `single_elim` (cheaper, more robust). |
| A LARGE field, pick the best cheaply, noise-robust | **racing** | Successive halving: cheap rung-0 duels on a board SLICE cut the worst by `eta`; survivors re-duel on larger slices. Trades board coverage for cheapness. The one bracket-shaped structure endorsed for zicato's regime. |

Rules of thumb:
- **More than one challenger but a tight budget** → `racing` (it never wastes
  full-board runs on obvious losers).
- **You want to *report* a leaderboard of the field** → `swiss`, after
  setting `experimental.tournament_structures = true`.
- **You just want a winner from a small field** → `racing` with a small
  field; `single_elim` needs the same opt-in and has no measured case at
  that size.
- **You distrust the gauntlet's verdict** → stay on `gauntlet` and raise
  `replicates` past 2. That is strictly cheaper than switching to a bracket.

## The params (read these off the strategy code rather than docs/design/CLI.md)

`field_size` and `replicates` are universal; the rest are per-structure.
Defaults below are the strategy constructors' real defaults.

| Param | Structures | Default | Meaning |
|---|---|---|---|
| `field_size` | all | `1` (gauntlet fixes it), else `2` | How many challengers the proposer must emit this round. `field_size == 1` degrades ANY structure to gauntlet semantics organically. |
| `replicates` | all | `2` (the base default; `1` for racing) | Paired board runs averaged before scoring a duel (`>= 1`). The NOISE lever. Also honoured in fast mode, but on the CHALLENGER side only — the champion stays one cached draw, so fast-mode replication halves the noise rather than removing it. |
| `rounds_n` | swiss | `4` | Number of Swiss rounds (the INNER rounds). Each round re-pairs near-equal standings; the leader then faces the champion gate. |
| `eta` | racing | `2` (clamped `>= 2`) | Halving factor. Each rung keeps the top `floor(alive / eta)` by scalar and cuts the rest. |
| `board_fraction` | racing | `0.25` | Rung-0 board slice = `ceil(board_fraction × board size)`; the slice grows by `eta` each rung until it reaches the full board (the final rung). |
| `rung0_board_size` | racing | `0` | Explicit rung-0 slice size in entries. `0` ⇒ derive it from `board_fraction`. |
| `board_ids` | racing | full epoch board (auto-injected) | OPTIONAL. The board entry ids to slice. Omit it — the orchestrator defaults it to the whole epoch board. Pass an explicit list ONLY to race on a subset. |
| `matchup_budget_seconds` | racing | unset (uncapped) | OPTIONAL. Wall-clock cap on EVERY duel's total board-unit time. Once spent the duel stops launching units and records the rest as budget-exceeded (a partial aggregate). The grind guard for a racing run. |
| `final_rung_budget_seconds` | racing | unset (uncapped) | OPTIONAL. Overrides `matchup_budget_seconds` for the FINAL rung only — the crowning duel that runs the full board × replicates × both sides, the pathological grind case. |

Notes that bite:
- A **racing rung CUTS, it does not crown.** Elimination at a rung is by RANK
  on that rung's board slice (best-arm identification) rather than the gate. The gate
  runs exactly once — at the final rung, on the FULL board, against the last
  survivor.
- A **swiss/elim leader is confirmed rather than crowned.** After the inner rounds,
  the top-standing/surviving challenger plays one champion-gate duel; only
  that duel can promote.
- `double_elim`'s "second life" is implemented as a single-elim over the
  accumulated winners'-bracket losers (a documented simplification); every
  generation still dies on its second loss and the grand-final winner still
  faces the champion gate. Prefer `replicates` over relying on it.

## Configure it — the `scoring.json` `tournament` block (authoritative)

Add a `tournament` block alongside the scoring weights. This is the canonical
form; the CLI flags below just write into it.

```jsonc
{
  "promote_margin": 0.01,
  // … the usual drift-loss weights / per_judge_weights / predicates …
  "tournament": {
    "structure": "racing",      // gauntlet | racing; the experimental three need the opt-in
    "params": {
      "field_size": 4,          // challengers proposed per round (gauntlet ⇒ 1)
      "replicates": 2,          // paired runs per duel, averaged — the noise lever
      "eta": 2,                 // racing: keep top 1/eta each rung
      "board_fraction": 0.4,    // racing: rung-0 slice = ceil(0.4 · |board|)
      "rung0_board_size": 0     // racing: 0 ⇒ derive rung-0 size from board_fraction
      // swiss instead adds: "rounds_n": 4
      // single_elim / double_elim: field_size + replicates suffice
    }
  }
}
```

An absent `tournament` block ⇒ `{structure: "gauntlet", params: {}}` — every
existing epoch keeps today's behaviour with no migration.

## Configure it — `zicato evolve` flags (contract-mutating convenience)

Derive the exact surface from `zicato evolve --help` (the design docs are
stale). As of writing the flags are:

```bash
zicato evolve \
    --tournament-structure racing \
    --tournament-param field_size=4 \
    --tournament-param eta=2 \
    --tournament-param board_fraction=0.4 \
    --tournament-param replicates=2 \
    --rounds 2
```

- `--tournament-structure {gauntlet|racing}`
  writes `{structure, params}` into the live `scoring.json` BEFORE the
  contract hash is computed, so it participates in the hash like a hand edit.
- `--tournament-param KEY=VALUE` is repeatable; `VALUE` is parsed as JSON when
  possible (so `field_size=4` is the integer `4`), else taken as a string.
  Params are applied ONLY when `--tournament-structure` is also passed.
- There is **no** `--field-size` flag — set it via `--tournament-param
  field_size=N`.

## Changing the structure rolls the epoch

The `tournament` block is part of the frozen evaluation contract, so a
structure or param change is a contract-hash change. The next `evolve` closes
the current epoch and opens a fresh one (exactly as retuning `promote_margin`
does), unless you pass `--no-auto-epoch` to error instead. This is by design:
a gauntlet champion and a racing champion are selected under different rules
and are **not directly comparable**, so they must not share an epoch's
lineage. See `zicato-analyze-epoch` and
[EPOCHS-AND-JOURNALING.md](../../docs/design/EPOCHS-AND-JOURNALING.md).

## Winner-resolution & rating (beyond Copeland)

By default swiss collapses its duel matrix with **Copeland** (count of duels
won), which is margin-blind, and a noisy loss can leave the matrix **cyclic**
(A>B, B>C, C>A). Two **opt-in** `tournament.params` knobs — read by `swiss`,
`single_elim` and `double_elim` only — now sit over that
([SELECTION-THEORY.md](../../docs/design/SELECTION-THEORY.md)):

| Param | Values | Effect |
|---|---|---|
| `resolver` | `ranked_pairs` \| `copeland` | Re-picks the INTERNAL leader from the net-margin matrix: Condorcet fast path, then Smith-set prune, then Ranked Pairs (recommended) or Copeland order. |
| `rating` | `bradley_terry` | Fits BT strengths from the audited duels for standings + the `P(θ_child > θ_parent)` uncertainty it needs. |

Both are derived from already-measured duel data (the gate's
`delta_scalar` and the two side scalars), so they cost **zero new board runs**;
absent or set to `none` they leave each structure's existing pick
byte-identical. Maximal lotteries remain unimplemented.

Neither knob holds a promotion. Requiring confidence before a crowning promote
is the evidence gate's job — `promote_confidence_threshold` plus
`promote_confidence_replicates`, which apply to every structure and buy the
confidence with extra replicates rather than only refusing the crown.

The one operating rule to remember now: **replicate first, resolve second.**
Most cycles zicato sees are noise artifacts that replication dissolves; only
invoke a cycle-resolver on the residual cycle that survives replication. And
any such resolver only *proposes* a leader — the champion-gate still owns
promotion, so the protected-incumbent invariant is untouched.

## Worked examples

**Noisy gauntlet → just replicate harder (no structure change).** The verdict
still flips run-to-run at the default 2 replicates. Stay on `gauntlet` and
raise it:

```jsonc
{"tournament": {"structure": "gauntlet", "params": {"replicates": 3}}}
```

**Four candidates, want a leaderboard.** Rank the field in three Swiss rounds,
then gate the leader:

```jsonc
{"tournament": {"structure": "swiss",
  "params": {"field_size": 4, "rounds_n": 3, "replicates": 2}}}
```

**Eight candidates, tight budget, pick the best.** Race them: rung 0 duels all
eight on 25% of the board, keep the top half, grow the slice, repeat; only the
final survivor sees the full board + the gate:

```jsonc
{"tournament": {"structure": "racing",
  "params": {"field_size": 8, "eta": 2, "board_fraction": 0.25}}}
```

## A good tournament design

- **Start at gauntlet.** Only adopt a field-structure once the proposer
  actually emits multiple challengers worth comparing.
- **Reach for `replicates` before bracket shape** when the problem is noise —
  it is the honest, cheaper lever.
- **Use `racing` for a field.** `swiss`, `single_elim` and `double_elim` are
  experimental: they need `experimental.tournament_structures = true`, and
  none has a measured case at zicato's field size.
- **Let `field_size == 1` degrade gracefully** — every structure collapses to
  a single full-board duel, so a misconfigured field never errors out.
- **Never start a live `zicato evolve` to test a structure without the
  operator's explicit go-ahead.** Verify config + behaviour via the test
  suite (e.g. the presentation example's racing test) instead.
