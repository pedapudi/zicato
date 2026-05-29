---
name: zicato-lineage
description: Read how the harness got here across epochs and generations — list epochs, read lineage.json, distinguish parent/child (lineage) from champion/challenger (tournament role), and drive the dashboard's side-by-side conversation diff for a board entry between two generations. Use this when you need the cross-epoch tree, not a single round's verdict.
---

# zicato lineage — read the tree across epochs and generations

The **lineage** is how the inner harness evolved: a shallow DAG of epochs, each
holding a linear chain of generations, joined across epochs by a baselining
edge. Use this skill to answer "how did the harness get here?". For one round's
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
- **champion / challenger** — the **tournament** axis. In a matchup the
  champion is the reigning best (always the parent), the challenger is the
  candidate (always the child). Every matchup is parent-vs-child; there is never
  a child-vs-child matchup.

So "champion = parent" and "challenger = child" name the *same pair* from two
angles. Standardize **champion/challenger** for tournament framing,
**parent/child** for lineage framing.

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
  "epochs": [
    {
      "id": "2026-04-01_initial",
      "name": "initial",
      "started_at": "...",
      "closed_at": "",
      "v0_parent": null,
      "generations": [
        {"id": "v0", "parent_id": null, "promoted": true,  "created_at": "..."},
        {"id": "v1", "parent_id": "v0", "promoted": true,  "created_at": "..."},
        {"id": "v2", "parent_id": "v1", "promoted": false, "created_at": "..."}
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
  off-spine.
- `generations[].parent_id` — the lineage edge (`null` for `v0`).

There is **no `zicato lineage` subcommand** in the shipped CLI; `epoch list` is
the rendered view and `lineage.json` is the structured source. (The
[CLI.md](../../docs/design/CLI.md) `zicato lineage` reference is aspirational.)

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
2. Open the **Tournament view → matchup detail** for the round, or the
   **Lineage** view and pick the two generation nodes to compare.
3. In the **per-entry A/B grid**, pick the board entry — the picker syncs
   selection to the URL hash and defaults the comparison to the parent
   generation.
4. Each cell drills into that one run; "open in harmonograf →" hands off to the
   run's `events.jsonl` for the full turn-by-turn trace (Gantt, drift, the
   intervention history). The competition view never renders a turn timeline;
   the execution view never renders a bracket — the drill-down stitches them.

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
