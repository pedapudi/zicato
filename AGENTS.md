# AGENTS.md

Operating guide for an AI agent working on or with **zicato** — the
self-improving meta-harness for multi-agent systems. Read this before
touching the repo or driving a workspace. The human-facing overview is
[`README.md`](README.md); the full design lives under
[`docs/design/`](docs/design/) (start with `ARCHITECTURE.md`).

> **If you are CHANGING zicato's own code**, the authoritative reference is the
> **development guide** at [`docs/dev-guide/`](docs/dev-guide/) — a 14-chapter,
> code-grounded book (start at
> [`docs/dev-guide/00-INDEX.md`](docs/dev-guide/00-INDEX.md), or the
> [`zicato-dev-guide`](skills/zicato-dev-guide/SKILL.md) skill for the doorway).
> It carries the 10 Golden Rules, the per-surface chapter map, the pre-commit
> verification ladder, the master invariant index, and the ten shipped bugs as
> teaching cases. The "Hard rules for agents" section below is the short form;
> the guide is the long form and is kept honest against the code.

## What zicato is, in one paragraph

zicato wraps an inner multi-agent harness in an **evolve loop**: it
proposes a small structured edit to the harness (an agent instruction,
a tool description, a planner template, a role scope), runs a scored
**tournament** between the parent (champion) and the child (challenger)
across a **board** of tasks, derives a scalar **loss** from runtime
drift telemetry plus per-task pass/fail predicates, and promotes the
child only when it beats the gate. Rounds group into **generations**;
generations group into **epochs**; an epoch is defined by its
**evaluation contract** (board + proposer brief + scoring +
inner-harness identity + the proposer itself) and by a **goal**. Change
the contract and the next `zicato evolve` auto-rolls a fresh epoch.

The whole tool, for most operators, is two commands:

```sh
zicato init      # scaffold ./.zicato/ once
zicato evolve    # the single happy-path entry point to the loop
```

Everything else (`board`, `propose`, `tournament`, `epoch`, `reindex`,
`mutations`, `health`, `builder`, `dashboard`, …) is an advanced / debug
tool for driving one stage in isolation or opening a view on the
workspace. `evolve` orchestrates the loop for you.

## Vocabulary (load-bearing)

- **epoch** — a sealed evaluation contract + a goal; houses many generations.
- **generation** (`v0`, `v1`, …) — one candidate snapshot of the inner harness; houses many board runs.
- **run** — one board entry executed against one generation; emits `events.jsonl` + `loss.json`.
- **round** — one propose → apply → tournament → promote/reject cycle.
- **champion / challenger** — the tournament roles (the pair being compared). **parent / child** — the same pair named by lineage. Use champion/challenger for tournament framing, parent/child for lineage.
- **experiment** — the artifact carrying a mandatory **hypothesis** (written before the run), the patches, and the **outcome** (written after).
- **mutation point** — a span or file the proposer may edit, marked `# zicato:mutable id="..."`. `zicato mutations` audits the surface.
- **scalar / loss** — lower is better; a weighted drift-derived loss plus per-task pass/fail. Per-judge drift folds in weighted by `judge_name`.
- **proposer brief** — the operator's brief to the proposer (`brief.md`): the goal, constraints, and `## Forbidden` mutation ids.

Full glossary: [`docs/design/VOCABULARY.md`](docs/design/VOCABULARY.md).

## Skills

Agent-driven workflows for operating zicato live under
[`skills/`](skills/) — one directory per skill, each a `SKILL.md`.
They are the recommended way to exercise the self-improvement loop:
they encode the right command sequence, the artifacts to read, and the
guardrails. See [`skills/README.md`](skills/README.md) for the catalog.
To make them available to an agent/coding-assistant session, symlink or copy a
skill into `.claude/skills/<name>/`.

## Hard rules for agents

These override convenience. Violating them is a defect.

1. **Gate live evolve runs.** Never start a live `zicato evolve` (one
   that calls real LLMs / spends budget) without the user's explicit
   go-ahead. Verify changes with the test suite and the deterministic
   mock target (`examples/zicato_examples/target_1_presentation`), not
   live runs.
2. **`uv sync --all-extras` — always.** A bare `uv sync` drops the dev
   tooling (pytest, mypy, ruff, even `uv`) from `.venv/`. Use
   `make install` (which wraps it) or `uv sync --all-extras`.
3. **Report the dashboard URL.** Every `evolve` launch enables the
   dashboard; surface its URL to the operator (default
   `http://127.0.0.1:7892`, override with `--dashboard-port`). The
   dashboard binds `127.0.0.1` only — there is no LAN-expose flag in
   the shipped CLI, so it is local by default.
4. **The filesystem is canonical; the index is derived.** `.zicato/`
   JSONL/JSON files are the source of truth; `index.db` is a
   rebuildable SQLite projection. Never hand-edit the index; after a
   hand-edit of a canonical file, run `zicato reindex`.
5. **Contract edits roll epochs.** Editing `board.jsonl`, `brief.md`,
   `scoring.json`, the registered harness, or the proposer (a
   `proposers/<name>/` dir or one of its skills) changes the evaluation
   contract — the next `evolve` auto-epochs (use `--no-auto-epoch` to
   make a drifted contract an error instead). Editing a live board mid-
   epoch therefore rolls the epoch and resets pattern history; reach for
   the `board` subcommands only to inspect or hand-edit a frozen board.
6. **Mandatory hypothesis.** Every experiment carries a hypothesis
   written *before* the run and an outcome written *after*. Do not
   backfill a hypothesis to match a result.

## Repo conventions

- Python is managed by `uv`; the CLI entry point is `zicato.cli:main`
  (Click, auto-discovered subcommands under `zicato/cli/commands/`).
- Run the suite with `uv run pytest`; lint/type with the pre-commit
  hooks (`make install-hooks`).
- The CLI is the contract — trust `zicato <command> --help` over the
  design docs when they disagree (the docs drift). Every flag in
  [`docs/design/CLI.md`](docs/design/CLI.md) should match a real option;
  if it does not, the doc is stale.
