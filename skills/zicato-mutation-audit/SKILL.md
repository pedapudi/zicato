---
name: zicato-mutation-audit
description: Tier 2 run — audit the mutable surface of a registered target with `zicato inspect mutations` (per-id span/file/code points, --id glob, --kind, --show full, --format json, [forbidden] annotations) to decide and verify what the proposer may change before an evolve run. Use this when reviewing the mutation surface, confirming markers resolve, or checking which ids are off-limits.
---

# zicato mutation audit — what the proposer may change

`zicato inspect mutations` lists the mutable spans in the registered target. It
is off the happy path (`zicato evolve` enumerates these itself), but it is the
right tool to **audit and confirm** the surface before you trust a run: every
marker resolves cleanly, the right ids are exposed, and the off-limits ids are
excluded.

Always invoke via `.venv/bin/zicato`. Requires a registered workspace (see
`skills/zicato-bootstrap`). The hard rules cited here live in the repo-root
`AGENTS.md`.

## How the surface is defined

Each mutable location is a comment-form marker in the target source:

```python
# zicato:mutable id="researcher_instruction" role="system_instruction"
RESEARCHER_INSTRUCTION = "You are a researcher. ..."          # span marker

# zicato:mutable:file id="presentation_agent.prompts"        # file marker

# zicato:mutable:code id="write_slug_logic"                  # code-region open
slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
# zicato:mutable:end                                         # code-region close
```

- **span** marker — covers the binding / keyword-arg on the line below. The
  default, smallest unit (one instruction, one tool description, one routing
  template).
- **file** marker (`# zicato:mutable:file`) — in the file header region; the
  whole module is mutable as one unit.
- **code** marker (`# zicato:mutable:code` … `# zicato:mutable:end`) — a
  pointed code REGION: the verbatim source lines *between* the opening marker
  and the `:end` sentinel (control flow, not a string literal). Exposes a
  block (e.g. a tool's slugify / path logic) as mutable without handing the
  proposer the whole module. The `:end` sentinel carries no id/metadata.

A point's `id` is its stable handle; `kind` is `span`, `file`, or `code`. See
[docs/design/MUTATION-SURFACE.md](../../docs/design/MUTATION-SURFACE.md).

The optional `role="…"` metadata is **load-bearing beyond documentation**: the
epoch-open pre-flight samples probe points **round-robin across declared roles**
so its achievable-signal measurement spans the harness instead of hammering one
kind of point (points with no `role` fall back to grouping by `kind`). Annotating
roles is how you get a representative pre-flight verdict — see
`skills/zicato-evolve`.

> **Not a mutation point: the proposer's failure-mode feedback channel.** The
> proposer now receives a board-anonymized, train-slice-only *outcome-marginal
> failure profile*, optionally extended by a `scoring.json`
> `outcome_summarizer_spec` hook. That hook is a **scoring contract field** (an
> operator input to the *evaluation*, hashed into the contract — changing it
> rolls the epoch), NOT a mutable harness span. It never appears in `zicato
> mutations`, and the proposer cannot edit it — it only *reads* the profile it
> produces. Do not look for it in the mutation surface.

## The audit commands

List the whole surface (human-readable table):

```sh
.venv/bin/zicato inspect mutations --workspace .zicato
```

Each row is `id  kind  lines  file  preview`, with a footer like
`Total: 9 mutation point(s)  [span=9]  ~119 mutable line(s)`. Confirm: no
warnings, no duplicate ids, the count matches what you expect.

| Flag | Use |
|---|---|
| `--workspace PATH` | Workspace dir (default `.zicato`). |
| `--id TEXT` | Filter by id glob, e.g. `--id 'researcher_*'` or `--id '*_instruction'`. |
| `--kind span\|file\|code` | Restrict the listing to one marker form. |
| `--show preview\|full` | `preview` (default) truncates content; `full` dumps the entire current text of each point. |
| `--format table\|json` | `table` (default) for humans; `json` emits the full `MutationPoint` shape. |

Inspect one role group in full, to read exactly what the proposer would rewrite:

```sh
.venv/bin/zicato inspect mutations --workspace .zicato --id 'coordinator_*' --show full
```

Machine-readable dump (feed into review tooling, diff across registrations):

```sh
.venv/bin/zicato inspect mutations --workspace .zicato --format json
```

The JSON gives per-point `id`, `kind`, `file`, `source_root`,
`line_start`/`line_end`, `content_hash`, `preview`, and `metadata` (e.g.
`{"role": "coordinator_routing"}`), plus a `summary` with `total`, `by_kind`,
and `mutable_lines`.

Note: the CLI exposes the flags above only. There is no `--root` filter — the
listing always covers every registered source root; filter with `--id` when you
want a subset.

## Forbidden ids

An operator marks ids off-limits in the proposer brief's `## Forbidden` list.
Enforcement is mechanical and lives on the patch path, not in this listing:
`check_forbidden_ids` (`mutation/validator.py`) / `enforce_forbidden`
(`proposer/brief.py`) reject any patch whose `mutation_id` is in the set, matching
on the **literal id**.

`zicato inspect mutations` does **not** read the brief and does **not** annotate
forbidden ids — its rows are the whole enumerated surface either way. What it is
good for is getting the id spellings exactly right:

1. Run `zicato inspect mutations` (or `--id '<glob>'`) and copy the id verbatim out of
   the listing.
2. Paste it into the brief's `## Forbidden` list — a typo'd id silently forbids
   nothing, and nothing in the listing will tell you.
3. Editing the brief rolls the epoch (the brief is a contract component).

## What success looks like

- Every marker resolves: the table prints with no warnings and no duplicate-id
  errors.
- The exposed ids are exactly the surface you intend the proposer to touch.
- Every id you put in the brief's `## Forbidden` list appears — spelled
  identically — in this listing.

With the surface confirmed, hand off to `skills/zicato-evolve` to run the loop —
the proposer addresses patches only against the points you just verified.

## Reference

- [docs/design/MUTATION-SURFACE.md](../../docs/design/MUTATION-SURFACE.md) — marker syntax, AST resolution, the `MutationPoint` shape, validator constraints (incl. V5 / `## Forbidden`), the audit CLI.
- [docs/design/CLI.md](../../docs/design/CLI.md) — full CLI reference.
