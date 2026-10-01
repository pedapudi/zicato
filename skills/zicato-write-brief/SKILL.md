---
name: zicato-write-brief
description: Author or refine a zicato proposer brief (brief.md) — the epoch goal, mutation budget/constraints, and the `Forbidden edits` list of mutation ids. Use when steering what the proposer is allowed to change for an epoch, or before opening a new epoch.
---

# Authoring the proposer brief (`brief.md`)

The **proposer brief** is the operator's instructions to the proposer: what to
optimise this epoch, which mutation points to favour, and which it may not
touch. The proposer consults it before generating each candidate.

Canonical filename: **`brief.md`**, unless `contract.brief_path` in
`.zicato/config.json` (set by `zicato epoch register --brief`) names another
file. It is one of the three live contract files next to the workspace — `board.jsonl`, `brief.md`,
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
**finish an epoch's rounds before rewriting its brief.** Only line endings,
trailing whitespace and leading or trailing blank lines are ignored; any other
edit changes the hash. To refine the brief,
expect (and want) the epoch to roll. Use `--no-auto-epoch` only when you mean
to error out instead of rolling.

## Structure

Free-form Markdown: the whole text reaches the proposer verbatim. Two
headings are also parsed structurally — `Forbidden edits` (enforced) and
`Preferred edits` (read as guidance) — and `## Goal` feeds the displayed epoch
objective. Mirror the worked example
(`examples/zicato_examples/target_1_presentation/rubric.md`):

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

## Forbidden edits
<Mutation ids the proposer may NOT touch this epoch, one bullet each, id in
 backticks. An empty section is fine.>
- `coordinator_files_not_found_routing` — held fixed while the researcher changes are measured

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

## The `## Forbidden edits` section

This is the mutation-side pin: a list of mutation ids the proposer is barred
from editing this epoch. Use it to **freeze** a span a prior generation got
right, or to keep the proposer off a span you are changing by hand. A patch
that touches a listed id is refused before it is applied.

**The parser's rule is exact, and a mistake fails silently** (the section then
forbids nothing):

- The heading text must be `Forbidden edits`, matched case-insensitively at
  any heading depth (`# Forbidden edits`, `## Forbidden edits`, …). A heading
  of `## Forbidden`, `## Forbidden ids` or `## Do not edit` is ordinary prose.
- The section runs to the next heading of **any** depth, so a sub-heading
  inside it ends it.
- Only bullet lines (`-`, `*` or `+`) are read. In each bullet, every
  backticked token is an id; a bullet with no backticks falls back to
  single- or double-quoted tokens. Unmarked words are ignored.
- `Preferred edits` follows the same heading and bullet rule.

The ids must match real mutation points — confirm them against the audit CLI:

```sh
PY=$ZICATO/.venv/bin/python   # ZICATO is your zicato checkout
$PY -m zicato.cli inspect mutations --workspace .zicato   # lists every mutable id
```

An empty `## Forbidden edits` section (or one with no bullets, e.g.
`None.`) is normal for a baseline epoch. As an
epoch lineage matures, move stabilised ids here so later rounds explore
elsewhere.

> The brief pins the *mutation* surface; the scoring side of pinning ("this
> entry must keep passing") is implicit via pass-rate monotonicity in
> `scoring.json` — see `zicato-tune-scoring`.

## Mutation budget / constraints

State any per-round edit budget and constraints in `## Goal` / `## Style`
(e.g. "prefer one focused edit per round", "specialist instructions should be
terse and imperative", "preserve double-backtick tool references so the LLM can
resolve them to tool names"). The brief is advisory text to the proposer rather than a
hard validator — be explicit and concrete.

## Workflow

1. Run an epoch's rounds against the current brief.
2. Read the journal/analysis: did promoted generations align with your intent?
3. Refine `brief.md` — sharpen the goal, re-rank preferred edits, add stabilised
   ids to `## Forbidden edits`.
4. Next `evolve` auto-rolls a fresh epoch on the changed brief; run again.

## A good brief

- A `## Goal` that names the **dominant failure mode** rather than a wish list.
- `## Preferred edits` ranked by where signal actually lives (usually
  specialist instructions before tool descriptions).
- A `## Forbidden edits` list that grows as the lineage matures.
- A `## Style` section concrete enough that two proposers would rewrite a span
  the same way.
