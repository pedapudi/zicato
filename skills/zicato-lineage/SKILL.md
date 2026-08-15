---
name: zicato-lineage
description: Read how the harness got here across epochs and generations — list epochs, read lineage.json, distinguish parent/child (lineage) from champion/challenger (tournament role), and drive the dashboard's side-by-side conversation diff for a board entry between two generations. Use this when you need the cross-epoch tree, not a single round's verdict.
---

# zicato lineage — read the tree across epochs and generations

The **lineage** is how the target evolved: a shallow DAG of epochs joined
by baselining edges. Within an epoch the *promoted* generations form a linear
spine; a multi-challenger round also mints sibling challengers that all fork
from the same champion, so the epoch is a fan whose winners are that spine.

Use this skill to answer "how did the harness get here?". For one round's
verdict see `skills/zicato-tournament-forensics`; for an epoch's retrospective
see `skills/zicato-analyze-epoch`; for the live UI see `skills/zicato-watch-dashboard`.

Always call the CLI from the project venv: `.venv/bin/zicato ...`. See
[AGENTS.md](../../AGENTS.md). Read-only.

## Two naming axes — do not conflate them

The same pair of generations gets named two ways depending on framing
([VOCABULARY.md](../../docs/design/VOCABULARY.md),
[TOURNAMENT.md §1.1](../../docs/design/TOURNAMENT.md#11-king-of-the-hill)):

- **parent / child** — the **lineage** axis. The child generation `vN+1` was
  forked from its parent `vN`. This is the structural edge in `lineage.json`
  (`generations[].parent_id`).
- **champion / challenger** — the **tournament** axis. The champion is the
  reigning best, the challenger the candidate trying to unseat it. In the
  **gauntlet** (the default) every matchup is champion-vs-challenger = parent-vs-child,
  which is why the two axes collapse there. The non-gauntlet structures also
  play **challenger-vs-challenger** nodes (elim brackets, swiss pairings, racing
  rungs) — those have no incumbent, so the winner is just the better side and
  the pair is *not* a lineage edge.

So on a gauntlet crowning, "champion = parent" and "challenger = child" name the
*same pair* from two angles. Standardize **champion/challenger** for tournament
framing, **parent/child** for lineage framing — and never read a
challenger-vs-challenger match as a lineage relation.

## 1. List every epoch

```sh
.venv/bin/zicato epoch list --workspace .zicato
```

Renders `lineage.json` as a markdown table: one row per epoch with
`started_at`, `closed_at` (`(open)` if still running), the count of `promoted`
and `rejected` generations, and the `parent` (the epoch it baselined off, or
`(root)`). This is the fastest cross-epoch overview.

## 2. Read lineage.json directly

`.zicato/lineage.json` is the canonical cross-epoch DAG — one file, all epochs
([EPOCHS-AND-JOURNALING.md §6](../../docs/design/EPOCHS-AND-JOURNALING.md#6-lineage)):

```json
{
  "format_version": 1,
  "epochs": [
    {
      "id": "2026-04-01_initial",
      "name": "initial",
      "started_at": "...",
      "closed_at": "",
      "v0_parent": null,
      "generations": [
        {"id": "v0", "parent_id": null, "promoted": true, "created_at": "...",
         "round_index": 0, "rejection_reason": "",
         "parent_scalar": null, "child_scalar": null, "delta_scalar": null},
        {"id": "v2", "parent_id": "v1", "promoted": false, "created_at": "...",
         "round_index": 2,
         "rejection_reason": "insufficient improvement: 0.7328 vs 0.7601 (margin 0.0200)",
         "parent_scalar": 0.7601, "child_scalar": 0.7328, "delta_scalar": -0.0273}
      ]
    }
  ]
}
```

Read it:

```sh
.venv/bin/python -c "import json; print(json.dumps(json.load(open('.zicato/lineage.json')), indent=2))"
```

Key fields:

- `v0_parent` — the cross-epoch baselining edge. A fresh epoch's `v0` is the
  promoted head of its predecessor; `v0_parent` records that link
  ([EPOCHS-AND-JOURNALING.md §10.5](../../docs/design/EPOCHS-AND-JOURNALING.md#105-baselining-a-rolled-epoch)).
- `generations[].promoted` — `true` generations are on the **winners' spine**;
  `false` (excluding `v0`) are discarded challengers — recoverable, inspectable,
  off-spine. `null` means in-flight: the tournament has not settled, so the
  generation counts toward neither the promoted nor the rejected column.
- `generations[].parent_id` — the lineage edge (`null` for `v0`).
- `generations[].round_index` — the evolve round that MINTED the generation
  (its birth round). Set once and never re-stamped, so a champion that defends
  for ten rounds keeps the round it was born in.
- `generations[].rejection_reason` — the gate's own phrasing for why this
  generation was cut, e.g. `"insufficient improvement: 0.7328 vs 0.7188
  (margin 0.0200)"`. Non-empty **only** when `promoted` is `false`: an empty
  reason means promoted or in-flight, matching every other persisted surface,
  so never infer rejection from a non-empty reason on a `null` node — infer it
  from `promoted is False`.
- `generations[].parent_scalar` / `child_scalar` / `delta_scalar` — the settling
  duel's two scalars and their difference (`child - parent`). `null` when
  unrecorded — never `0.0`, which is a legal measurement, so absent and
  zero-scoring are distinguishable.
- `format_version` — the record-format stamp (currently `1`). A record stamped
  HIGHER than this build understands is refused loudly rather than misread; a
  record with no stamp is read as version 1.

There is **no `zicato lineage` subcommand** in the shipped CLI (nor in
[CLI.md](../../docs/design/CLI.md)); `epoch list` is the rendered view and
`lineage.json` is the structured source.

### Recombined children have provenance the DAG does not carry

A mechanically **recombined** challenger merges the patch sets of two *rejected*
complementary siblings. Its `lineage.json` `parent_id` is still the single
champion it was patched onto — the two donors are recorded only on the
generation's `experiment.json` as `recombined_from` (a 2-tuple of generation ids,
ascending; the key is **omitted entirely** on ordinary experiments, so its
absence means "not recombined"). Its `hypothesis.core_idea` also carries a
`[recombined]` display prefix, but read the field, never the prefix. So: to
reconstruct where a generation's *content* came from, check `recombined_from`
alongside the lineage edge.

## 3. Bracket vs tree — what is scoped to what

- **Within an epoch**: the **bracket** — a king-of-the-hill gauntlet, the
  winners' spine `v0 → v1 → v2 → ...`, comparable matchups, the §4 analytics.
  Scoped to one epoch because the board / brief / scoring are frozen there.
- **Across epochs**: the **tree** — each epoch's spine joined to the next by a
  dashed baselining edge (`v0_parent`). A challenger in `e1` and a champion in
  `e0` were judged against *different contracts*, so a cross-epoch "matchup"
  would compare numbers that do not mean the same thing.

The bracket answers "who won, this epoch?"; the tree answers "how did the
harness get here, across all epochs?"
([TOURNAMENT.md §6](../../docs/design/TOURNAMENT.md#6-cross-epoch-the-bracket-is-per-epoch-the-tree-links-epochs)).

## 4. Side-by-side conversation diff for a board entry

To compare *how two generations actually behaved* on the same board entry, use
the dashboard's compare picker (an L2/L4 feature; see `skills/zicato-watch-dashboard`).
The dashboard is the **competition view**; harmonograf is the **execution
view**, linked by a per-run drill-down
([TOURNAMENT.md §5](../../docs/design/TOURNAMENT.md#5-the-harmonograf-split)):

1. Launch the dashboard for the workspace (`skills/zicato-watch-dashboard` — `evolve`
   auto-spawns it, or `zicato dashboard` serves an existing workspace). Default
   URL `http://127.0.0.1:7892`.
2. Navigate **epoch → generations → the candidate** you care about.
3. Use the **"compare with…"** picker on the candidate detail. It splits the
   same pane into two columns (side A the candidate, side B the comparison) and
   encodes the choice as `cmp` in the route, so the comparison deep-links.
   Nothing is compared until you pick — there is no default second side, so
   choose the parent explicitly to read the champion/challenger pair.
4. Each side's per-board scoring drills into the individual run and its
   transcript. A **harmonograf** deep-link to the full turn-by-turn execution
   trace is rendered only while a run is LIVE (zicato's auto-launched
   harmonograf dies with the run, so the link is gated on liveness, not merely
   on a known URL). The competition view never renders a turn timeline; the
   execution view never renders a bracket — the drill-down stitches them.

The diff is *champion side vs challenger side* of the same board entry, so it is
exactly the parent/child (= champion/challenger) pair from the lineage edge,
viewed as two conversations.

## Guardrails

- venv-only (`.venv/bin/zicato`); never bare `uv sync` (use `--all-extras`).
- `lineage.json` is canonical and reconstructible; never hand-edit it.
- Do NOT start a live `evolve` to populate lineage — read what is already there.
- Every `evolve` launch reports the dashboard URL; view it from the host, no
  `--dashboard-bind` needed.

## See also

- [EPOCHS-AND-JOURNALING.md](../../docs/design/EPOCHS-AND-JOURNALING.md) §6 — lineage; §10.5 — baselining a rolled epoch.
- [TOURNAMENT.md](../../docs/design/TOURNAMENT.md) §1, §5, §6 — gauntlet, harmonograf split, bracket vs tree.
- [VOCABULARY.md](../../docs/design/VOCABULARY.md) — generation, lineage, tournament.
- `skills/zicato-watch-dashboard` — the live UI, the L2/L4 compare picker.
- `skills/zicato-tournament-forensics` — explain one matchup's verdict.
