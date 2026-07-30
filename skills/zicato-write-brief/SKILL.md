---
name: zicato-write-brief
description: Author or refine a zicato proposer brief (brief.md) — the epoch goal, mutation budget/constraints, and the Forbidden list of mutation ids. Use when steering what the proposer is allowed to change for an epoch, or before opening a new epoch.
---

# Authoring the proposer brief (`brief.md`)

The **proposer brief** is the operator's instructions to the proposer: what to
optimise this epoch, which mutation points to favour, and which it may not
touch. The proposer consults it before generating each candidate.

Canonical filename: **`brief.md`** (the legacy name `rubric.md` is still
accepted as a fallback, but write `brief.md`). It is one of the three live
contract files next to the workspace — `board.jsonl`, `brief.md`,
`scoring.json` — recorded in `.zicato/config.json` under `contract`. `evolve`
resolves the brief from there and freezes a per-epoch copy at
`.zicato/epochs/{epoch_id}/brief.md`. Sibling skills: `zicato-author-board`,
`zicato-tune-scoring`. See
[EPOCHS-AND-JOURNALING.md](../../docs/design/EPOCHS-AND-JOURNALING.md) and
[MUTATION-SURFACE.md](../../docs/design/MUTATION-SURFACE.md).

## The brief IS part of the evaluation contract

Editing `brief.md` changes the contract hash. On the next `evolve` (with
default auto-epoching) zicato closes the current epoch and opens a fresh one
before running — generations across epochs are not directly comparable. So:
**finish an epoch's rounds before rewriting its brief.** To refine the brief,
expect (and want) the epoch to roll. Use `--no-auto-epoch` only when you mean
to error out instead of rolling.

## Structure

Free-form Markdown, but the proposer keys off these sections. Mirror the worked
example (`examples/zicato_examples/target_1_presentation/rubric.md`):

```md
# Epoch e0 — <one-line epoch name>

## Goal
<Open with a PROSE paragraph naming the concrete behaviour this epoch is
 trying to improve — this paragraph is what zicato distils as the epoch's
 objective (see below). Then 2–5 bullets: the dominant failure mode it
 attacks, tied to board tags so the proposer can target the slice that
 matters.>

## Preferred edits
<Mutation ids the proposer should touch first — where the signal lives.>
- `researcher_instruction` — research_agent's system prompt
- `writer_instruction` — the presentation-builder's system prompt
- `coordinator_instruction` — routing logic and stage flow

## Secondary edits
<Fair game but lower priority — touch when a specific pattern fires.>
- `reviewer_instruction`, `debugger_instruction`
- tool descriptions (`write_webpage_tool_description`, …)

## Forbidden
<Mutation ids the proposer may NOT touch this epoch. Empty section is fine.>
None for the baseline epoch.

## Style
<Conventions for how spans get rewritten — terseness, what tool descriptions
 should and shouldn't say, tokens to preserve verbatim.>
```

## How the epoch objective is distilled from `## Goal`

When an epoch carries no explicit `goal` (`zicato epoch set-goal`), the
dashboard's objective callout and the publication masthead distil one from the
brief: the **first prose paragraph** inside `## Goal`, previewed at 120 chars.
Hard-wrapped lines are joined (hyphen-aware, so `multi-` + `agent` rejoins as
`multi-agent`), and the paragraph ends at the first blank line, heading, list
item, or code fence. A `## Goal` that opens straight into bullets therefore
distils **nothing** — lead with the prose sentence, and put the epoch's point
in its first ~120 characters. Display-only: nothing here feeds scoring or the
gate.

## The `## Forbidden` section

This is the mutation-side pin: a list of mutation ids the proposer is barred
from editing this epoch. Use it to **freeze** a span a prior generation got
right, or to keep the proposer off a span you are changing by hand. The ids
must match real mutation points — confirm them against the audit CLI:

```sh
PY=/home/sunil/git/zicato/.venv/bin/python
$PY -m zicato.cli mutations --workspace .zicato   # lists every mutable id
```

An empty `## Forbidden` (e.g. `None.`) is normal for a baseline epoch. As an
epoch lineage matures, move stabilised ids here so later rounds explore
elsewhere.

> The brief pins the *mutation* surface; the scoring side of pinning ("this
> entry must keep passing") is implicit via pass-rate monotonicity in
> `scoring.json` — see `zicato-tune-scoring`.

## Mutation budget / constraints

State any per-round edit budget and constraints in `## Goal` / `## Style`
(e.g. "prefer one focused edit per round", "specialist instructions should be
terse and imperative", "preserve double-backtick tool references so the LLM can
resolve them to tool names"). The brief is advisory text to the proposer, not a
hard validator — be explicit and concrete.

## Workflow

1. Run an epoch's rounds against the current brief.
2. Read the journal/analysis: did promoted generations align with your intent?
3. Refine `brief.md` — sharpen the goal, re-rank preferred edits, add stabilised
   ids to `## Forbidden`.
4. Next `evolve` auto-rolls a fresh epoch on the changed brief; run again.

## A good brief

- A `## Goal` that names the **dominant failure mode**, not a wish list.
- `## Preferred edits` ranked by where signal actually lives (usually
  specialist instructions before tool descriptions).
- A `## Forbidden` list that grows as the lineage matures.
- A `## Style` section concrete enough that two proposers would rewrite a span
  the same way.
