---
name: zicato-mutation-audit
description: Tier 2 run — audit the mutable surface of a registered inner harness with `zicato mutations` (per-id span/file points, --id glob, --kind, --show full, --format json, [forbidden] annotations) to decide and verify what the proposer may change before an evolve run. Use this when reviewing the mutation surface, confirming markers resolve, or checking which ids are off-limits.
---

# zicato mutation audit — what the proposer may change

`zicato mutations` lists the mutable spans in the registered inner harness. It
is off the happy path (`zicato evolve` enumerates these itself), but it is the
right tool to **audit and confirm** the surface before you trust a run: every
marker resolves cleanly, the right ids are exposed, and the off-limits ids are
excluded.

Always invoke via `.venv/bin/zicato`. Requires a registered workspace (see
`skills/zicato-bootstrap`). The hard rules cited here live in the repo-root
`AGENTS.md`.

## How the surface is defined

Each mutable location is a comment-form marker in the inner harness source:

```python
# zicato:mutable id="researcher_instruction" role="system_instruction"
RESEARCHER_INSTRUCTION = "You are a researcher. ..."          # span marker

# zicato:mutable file id="presentation_agent.prompts"         # file marker
```

- **span** marker — covers the binding / keyword-arg on the line below. The
  default, smallest unit (one instruction, one tool description, one routing
  template).
- **file** marker — in the file header region; the whole module is mutable.

A point's `id` is its stable handle; `kind` is `span` or `file`. See
[docs/design/MUTATION-SURFACE.md](../../docs/design/MUTATION-SURFACE.md).

## The audit commands

List the whole surface (human-readable table):

```sh
.venv/bin/zicato mutations --workspace .zicato
```

Each row is `id  kind  lines  file  preview`, with a footer like
`Total: 9 mutation point(s)  [span=9]  ~119 mutable line(s)`. Confirm: no
warnings, no duplicate ids, the count matches what you expect.

| Flag | Use |
|---|---|
| `--workspace PATH` | Workspace dir (default `.zicato`). |
| `--id TEXT` | Filter by id glob, e.g. `--id 'researcher_*'` or `--id '*_instruction'`. |
| `--kind span\|file` | Restrict the listing to one marker form. |
| `--show preview\|full` | `preview` (default) truncates content; `full` dumps the entire current text of each point. |
| `--format table\|json` | `table` (default) for humans; `json` emits the full `MutationPoint` shape. |

Inspect one role group in full, to read exactly what the proposer would rewrite:

```sh
.venv/bin/zicato mutations --workspace .zicato --id 'coordinator_*' --show full
```

Machine-readable dump (feed into review tooling, diff across registrations):

```sh
.venv/bin/zicato mutations --workspace .zicato --format json
```

The JSON gives per-point `id`, `kind`, `file`, `source_root`,
`line_start`/`line_end`, `content_hash`, `preview`, and `metadata` (e.g.
`{"role": "coordinator_routing"}`), plus a `summary` with `total`, `by_kind`,
and `mutable_lines`.

Note: the CLI exposes the flags above only. The design doc mentions a
`--root` filter that the real CLI does **not** implement — do not rely on it.

## Forbidden ids

An operator marks ids off-limits in the proposer brief's `## Forbidden` list
(validator constraint V5 — the post-apply validator rejects any patch touching
a forbidden id). `zicato mutations` surfaces those ids with a `[forbidden]`
annotation next to the kind, so this command is the place to sanity-check that
the right ids are excluded after you edit the brief.

Workflow:
1. Edit the proposer brief's `## Forbidden` list to add/remove ids.
2. Re-run `zicato mutations` and confirm the intended ids now show `[forbidden]`
   and the rest are still open.

## What success looks like

- Every marker resolves: the table prints with no warnings and no duplicate-id
  errors.
- The exposed ids are exactly the surface you intend the proposer to touch.
- Ids you marked in `## Forbidden` show `[forbidden]`; nothing off-limits is
  left open.

With the surface confirmed, hand off to `skills/zicato-evolve` to run the loop —
the proposer addresses patches only against the points you just verified.

## Reference

- [docs/design/MUTATION-SURFACE.md](../../docs/design/MUTATION-SURFACE.md) — marker syntax, AST resolution, the `MutationPoint` shape, validator constraints (incl. V5 / `## Forbidden`), the audit CLI.
- [docs/design/CLI.md](../../docs/design/CLI.md) — full CLI reference.
