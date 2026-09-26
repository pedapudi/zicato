---
name: zicato-design-tournament-structure
description: Choose and configure a zicato per-epoch tournament structure — racing (the recommended default), gauntlet, or the experimental swiss, single_elim and double_elim — and its params (field_size, replicates, the evidence-gate pair, swiss rounds_n, racing eta/board_fraction/rung0_board_size). Use when an epoch has more than one challenger to select among, when a single-challenger gauntlet is too noisy, or when picking the best of a large candidate field cheaply; explains the decision guide, the noise/incumbent design principles, the scoring.json `tournament` block, and that changing it rolls the epoch.
---

# Designing a zicato tournament structure

The **tournament** is how zicato turns a field of proposed challengers into
at most one promotion. The recommended default — what an empty `scoring.json`
or one without a `tournament` block resolves to — is **racing** over a field of
four challengers with the evidence gate on. The **gauntlet** is the
single-challenger alternative: one champion, one challenger, one full-board
duel, promote-on-gate. With `experimental.tournament_structures` set to `true`
in `scoring.json`, an epoch may also select the experimental `swiss`,
`single_elim`, or `double_elim`.

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
| One challenger per round; cheapest possible 1-vs-1 | **gauntlet** | One full-board duel per replicate, promote-on-gate. Raise `replicates` above its default 2 if the verdict is still too noisy — no structure change needed. |
| A field, and you want a full RANKING in few duels | **swiss** (experimental) | Needs `experimental.tournament_structures = true`. Fixed `rounds_n` Swiss rounds rank the whole field by Copeland (duels won); no elimination, so every candidate is rated. Cheap, non-adaptive. |
| A field, and you only need the single best (knockout) | **single_elim** (experimental) | Needs the same opt-in. A bracket over the challengers halves the field each round; the survivor faces the champion. Fewer duels than swiss, but loses the full ranking. |
| Same, but you want a "second chance" against an upset | **double_elim** (experimental) | Needs the same opt-in. Winners' + losers' bracket; eliminated only on the SECOND node loss. Offered for completeness — prefer raising `replicates` on `single_elim` (cheaper, more robust). |
| A field, pick the best cheaply, noise-robust | **racing** | The recommended default. Successive halving: cheap rung-0 duels on a board SLICE cut the worst by `eta`; survivors re-duel on larger slices. Trades board coverage for cheapness. The one bracket-shaped structure endorsed for zicato's regime. |

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

Every structure accepts `replicates`, `promote_confidence_threshold` and
`promote_confidence_replicates`; the rest are per-structure, and a key the
selected structure does not accept is refused with the list of accepted keys.
Defaults below are the strategy constructors' defaults for a key the params
omit; the recommended contract an absent `tournament` block resolves to sets
`field_size 4`, `eta 2`, `board_fraction 0.4`, `replicates 2`,
`promote_confidence_threshold 0.8` and `promote_confidence_replicates 32`.

| Param | Structures | Default | Meaning |
|---|---|---|---|
| `field_size` | all but gauntlet | `2` | How many challengers the proposer must emit this round. The gauntlet always fields one and refuses the key. `field_size == 1` degrades any field structure to gauntlet semantics. |
| `replicates` | all | `2` (the base default; `1` for racing) | Paired board runs averaged before scoring a duel (`>= 1`). The NOISE lever. Also honoured in fast mode, but on the CHALLENGER side only — the champion stays one cached draw, so fast-mode replication halves the noise rather than removing it. |
| `promote_confidence_threshold` | all | unset (off) | The evidence gate: the probability the challenger is stronger than the champion that a crowning promotion must reach. Absent, `null` or `0` disables confirmation. |
| `promote_confidence_replicates` | all | `32` | The evidence gate's budget of extra confirmation draws; `0` permits none. |
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
      "field_size": 4,          // challengers proposed per round (not accepted by gauntlet)
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

An absent `tournament` block resolves to the recommended racing
specification listed above. Print what a workspace resolves to with
`zicato inspect config --effective --workspace .zicato` (the
`scoring.tournament.*` rows).

## Configure it — `zicato evolve` flags (contract-mutating convenience)

Derive the exact surface from `zicato evolve --help`:

```bash
zicato evolve \
    --tournament-structure racing \
    --tournament-param field_size=4 \
    --tournament-param eta=2 \
    --tournament-param board_fraction=0.4 \
    --tournament-param replicates=2 \
    --rounds 2
```

- `--tournament-structure {gauntlet|racing}` sets the structure and keeps
  the existing params. The validated edit is written into the live
  `scoring.json` BEFORE the contract hash is computed, so it participates in
  the hash like a hand edit.
- `--tournament-param KEY=VALUE` is repeatable and works with or without
  `--tournament-structure`; `VALUE` is parsed as JSON when possible (so
  `field_size=4` is the integer `4`), else taken as a string, and `KEY=null`
  removes the key. Switching racing to gauntlet therefore also needs
  `--tournament-param field_size=null --tournament-param eta=null
  --tournament-param board_fraction=null`, because the gauntlet refuses those
  keys.
- Neither flag combines with `--epoch`. Under `--dry-run` the edit is checked
  in memory and not saved.
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
(A>B, B>C, C>A). Two **opt-in** keys in `scoring.json`'s `experimental` block
sit over that; they reach `swiss`, `single_elim` and `double_elim` only
([SELECTION-THEORY.md](../../docs/design/SELECTION-THEORY.md)). Putting either
under `tournament.params` is refused with a message naming the
`experimental` key to use instead:

| Key | Values | Effect |
|---|---|---|
| `experimental.resolver` | `none` (default) \| `ranked_pairs` \| `copeland` | Re-picks the INTERNAL leader from the net-margin matrix: Condorcet fast path, then Smith-set prune, then Ranked Pairs (recommended) or Copeland order. |
| `experimental.standing_rating` | `none` (default) \| `bradley_terry` | Fits Bradley–Terry strengths from the audited duels for the standings. |

Both are derived from already-measured duel data (the gate's
`delta_scalar` and the two side scalars), so they cost **zero new board runs**;
absent or set to `none` they leave each structure's existing pick
byte-identical. Maximal lotteries are not implemented.

Neither knob holds a promotion. Requiring confidence before a crowning promote
is the evidence gate's job — `promote_confidence_threshold` plus
`promote_confidence_replicates`, which apply to every structure and buy the
confidence with extra replicates rather than only refusing the crown.

The one operating rule to remember: **replicate first, resolve second.**
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
{"experimental": {"tournament_structures": true},
 "tournament": {"structure": "swiss",
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

- **Keep the recommended racing contract** unless you have a reason to leave
  it; switch to `gauntlet` when the proposer can only produce one challenger
  worth comparing per round.
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
