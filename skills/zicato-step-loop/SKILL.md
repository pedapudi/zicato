---
name: zicato-step-loop
description: Drive the zicato evolve loop one stage at a time (propose → tournament) for inspecting or debugging a single round. Use only when you need to look inside what `zicato evolve` does internally; for normal operation run `zicato evolve`.
---

# zicato step-loop (manual round, for debug only)

`zicato evolve` is the happy path: it auto-resolves the contract, auto-opens an
epoch, then runs `analyze-telemetry → propose → (apply) → tournament → promote`
for `--rounds` rounds. This skill drives those stages **by hand**, one at a
time, so you can inspect each artifact between stages. Reach for it only when
debugging a single round — never as the normal way to run zicato.

> Guardrail: `propose` and `tournament` call real LLMs and spend budget. Treat
> them as live runs — get the operator's explicit go-ahead before invoking them
> (AGENTS.md rule 1). Everything in the "inspect" steps below is read-only and
> safe. Use `.venv/bin/zicato`; never `uv sync` mid-task.

## The real command names (verify before you script)

Older plans described `zicato run`, `zicato analyze`, and `zicato patch apply`
as separate steps. **Those never shipped**, and `docs/design/CLI.md` no longer
claims them — it is a generated reference reconciled against `--help`. If you
inherited a script or a note using the old names, translate it:

| Old form | Real CLI | Notes |
|---|---|---|
| `zicato run --generation vN --entry <id> [--tail]` | *(none)* | No standalone runner. Runs happen *inside* `tournament` / `evolve`, which execute every board entry against each generation. |
| `zicato analyze` | `zicato analyze-telemetry` | Decision-telemetry analyzer for the current epoch. |
| `zicato propose --output <file>` | `zicato propose` | No `--output`; it writes `experiment.json` into the next generation dir itself. |
| `zicato patch apply --experiment <file> --as vN` | *(none)* | No separate apply step. `propose` creates the candidate generation and writes its experiment in one shot; `tournament` / `evolve` apply patches internally. |
| `zicato tournament vN vM` | `zicato tournament PARENT CHILD` | Positional generation ids. |

Always confirm with `.venv/bin/zicato <cmd> --help` before relying on a flag.

## The manual round, stage by stage

```sh
Z=.venv/bin/zicato

# 0. Inspect the surface the proposer may touch (read-only, no LLM).
$Z mutations --show preview                 # add --format json to script it

# 1. Analyzer: (re)build the decision-telemetry insight for this epoch.
#    Writes insights/round_{N:04d}.md (round_0007.md for --round 7), or
#    insights/latest.md when --round is omitted.
$Z analyze-telemetry --round 7              # spends no proposer budget

# 2. Propose: generate ONE Experiment for the next generation. (LLM — gated.)
#    Writes generations/vN+1/experiment.json + per-patch files. No --output.
$Z propose                                  # uses freshly-run detectors
$Z propose --patterns-from path/to/patterns.json   # or pin a patterns file

# 3. (No separate apply.) Confirm the candidate generation now exists and read
#    its hypothesis before scoring it.
$Z epoch list
sed -n '1,40p' .zicato/epochs/<epoch>/generations/vN+1/experiment.json

# 4. Tournament: score PARENT vs CHILD and decide promote/reject. (LLM — gated.)
$Z tournament v3 v4                         # full: re-runs both generations
$Z tournament v3 v4 --mode fast             # child vs parent's cached aggregate
$Z tournament v3 v4 --skip-regression       # bypass the regression gate
```

**`tournament` does NOT encode the verdict in its exit code.** It prints a JSON
result payload and exits `0` for both promote and reject; a usage/config problem
raises a `click.ClickException` (exit `1`). There is no fine-grained verdict
code (no exit `6`=reject) — read the `decision` in the printed JSON, do not
branch on the exit code for promote-vs-reject.

## What each stage leaves on disk (the inspection points)

All paths are under `.zicato/epochs/<epoch>/`:

- `insights/round_{N:04d}.md` — analyzer output (stage 1).
- `generations/vN/experiment.json` — hypothesis (written **before** scoring) +
  `patch_ids` + the `outcome` block (written **after** the tournament).
- `generations/vN/patches/*.json` — one file per patch.
- `generations/vN/runs/<entry>/{events.jsonl,loss.json}` — per-entry telemetry.
  `loss.json` is replicate 0; further replicates land in sibling
  `loss.r<N>.json` (the default `replicates` is 2, so expect them).
- `rounds/<round>/round_log.jsonl` — the round's durable typed event log
  (contract hash → proposal → apply → units → gate → recorded decision).
- `journal.md` — appended at each promote/reject.

Read these between stages rather than re-running. After any hand-edit of a
canonical file, run `zicato reindex` so `index.db` re-derives (see
`zicato-index-ops`).

## When NOT to use this skill

- Normal operation → run `zicato evolve` (it orchestrates all of the above and
  launches the dashboard; report its URL, default `http://127.0.0.1:7892`).
- Loop not improving → `zicato-triage-stuck-loop`.
- Formulating the pre-run hypothesis → `zicato-design-experiment`.

## See also

- `docs/design/CLI.md` — full subcommand reference and exit codes.
- `docs/design/EPOCHS-AND-JOURNALING.md` — the `Experiment` artifact + journal.
- `docs/design/SCORING.md` — what the tournament gate decides.
- `docs/design/MUTATION-SURFACE.md` — what `mutations` enumerates.
- sibling skills: `zicato-index-ops`, `zicato-design-experiment`,
  `zicato-triage-stuck-loop`.
