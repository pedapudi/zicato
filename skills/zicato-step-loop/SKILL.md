---
name: zicato-step-loop
description: Drive the zicato evolve loop's stages one at a time for inspecting or debugging a round — rebuild the decision-telemetry insight, run one proposal episode without a tournament, and re-score an existing champion/challenger pair. Use only when you need to look inside what `zicato evolve` does internally; for normal operation run `zicato evolve`.
---

# zicato step-loop (stage by stage, for debug only)

`zicato evolve` is the happy path: it auto-resolves the contract, auto-opens an
epoch, then runs `analyze → propose → apply → tournament → promote` for
`--rounds` rounds. This skill runs the stages that have their own commands
**by hand**, one at a time, so you can inspect each artifact between them.
Reach for it only when debugging — never as the normal way to run zicato.

> Guardrail: `proposer propose` and `tournament run` call real LLMs and spend
> budget. Treat them as live runs — get the operator's explicit go-ahead before
> invoking them (AGENTS.md rule 1). Everything in the "inspect" steps below is
> read-only and safe. Use `.venv/bin/zicato`; never `uv sync` mid-task.

## The real command names (verify before you script)

No `zicato run`, `zicato analyze`, or `zicato patch apply` command exists;
`docs/design/CLI.md` is generated from `--help` and does not claim them. A
script or note that uses one of those names translates as follows:

| Name in the script | Real CLI | Notes |
|---|---|---|
| `zicato run --generation vN --entry <id>` | *(none)* | No standalone runner. Runs happen *inside* `tournament run` / `evolve`, which execute every board entry against each generation. |
| `zicato analyze` | `zicato inspect telemetry` | Decision-telemetry analyzer for an epoch. |
| `zicato propose --output <file>` | `zicato proposer propose` | No `--output`; it writes the experiment to `epochs/<epoch>/proposals/<vN>.json`. |
| `zicato patch apply --experiment <file> --as vN` | *(none)* | Only `evolve` applies patches and mints a generation. A proposal written by `proposer propose` is never applied. |
| `zicato tournament vN vM` | `zicato tournament run PARENT CHILD` | Positional generation ids of generations that already exist. |

Always confirm with `.venv/bin/zicato <cmd> --help` before relying on a flag.

## The stages, one at a time

```sh
Z=.venv/bin/zicato

# 0. Inspect the surface the proposer may touch (read-only, no LLM).
$Z inspect mutations --show preview           # add --format json to script it

# 1. Analyzer: (re)build the decision-telemetry insight for this epoch.
#    Writes insights/round_{N:04d}.md (round_0007.md for --round 7), or
#    insights/latest.md when --round is omitted. The highest-numbered round
#    file is what the next proposal episode receives; latest.md is not.
$Z inspect telemetry --round 7                # spends no proposer budget

# 2. Propose: run ONE proposal episode against the current champion. (LLM — gated.)
#    Writes epochs/<epoch>/proposals/<vN+1>.json and nothing else: no
#    generation, no lineage entry, no tournament, no outcome.
$Z proposer propose                           # uses freshly-run detectors
$Z proposer propose --patterns-from path/to/patterns.json   # or pin a patterns file

# 3. Read the proposed hypothesis and patches before deciding anything.
jq '.hypothesis, .patches' .zicato/epochs/<epoch>/proposals/<vN+1>.json

# 4. Tournament: re-score an EXISTING generation pair. (LLM — gated.)
$Z tournament run v3 v4                       # full (default here): re-runs both sides
$Z tournament run v3 v4 --mode fast           # child vs the parent's cached aggregate
$Z tournament run v3 v4 --skip-regression     # bypass the regression-suite gate
$Z tournament run v3 v4 --replicates 1        # force a replicate count for this call
```

`proposer propose` runs the same episode a round runs, but it assembles only
part of the round's context: the brief and skills, the mutation manifest, the
loss patterns and summary, the declared judge names and the experiment-memory
digest. The per-round derived channels (failure-mode profile, metric
priorities, process exemplars, genealogy, calibration record) are absent.

**`tournament run` does NOT encode the verdict in its exit code.** It prints a
JSON result — `parent_generation_id`, `child_generation_id`, `parent_agg`,
`child_agg`, `champion_eval_mode`, `per_entry_losses`, and the gate's
`outcome` (`decision`, `reason`, `delta_scalar`, `delta_pass_rate`,
`attributable_regressions`, `explanation`) — and exits `0` for promote,
reject and defer alike. A usage or configuration problem exits `1`. Read
`.outcome.decision`; do not branch on the exit code for promote-vs-reject.

## What the loop leaves on disk (the inspection points)

All paths are under `.zicato/epochs/<epoch>/`:

- `insights/round_{N:04d}.md` — analyzer output.
- `generations/vN/experiment.json` — the hypothesis (written **before**
  scoring) and `patch_ids`.
- `generations/vN/patches/*.json` — one file per patch.
- `generations/vN/runs/<entry>/seed-<seed>/` — per-entry measurements:
  `loss.<purpose>.r<draw>.json`, `result.<purpose>.r<draw>.json`, and, for a
  goldfive-instrumented adapter, `events.<purpose>.r<draw>.jsonl`. Tournament
  draws use the purpose `tournament`; with the default `replicates` of 2
  expect `r0` and `r1`.
- `rounds/<round>/round_log.jsonl` — the round's durable typed event log
  (contract hash → proposal → apply → units → gate → recorded decision).
- `rounds/<round>/field_settlement.json` — the settled outcome of every
  candidate in the round.
- `proposals/<vN>.json` — experiments written by `proposer propose` only.

Read these between stages rather than re-running. After any hand-edit of a
canonical file, run `zicato repair index` so `index.db` re-derives (see
`zicato-index-ops`).

## When NOT to use this skill

- Normal operation → run `zicato evolve` (it orchestrates all of the above and
  launches the dashboard; report its URL, default `http://127.0.0.1:7892`).
- Loop not improving → `zicato-triage-stuck-loop`.
- Formulating the pre-run hypothesis → `zicato-design-experiment`.

## See also

- `docs/design/CLI.md` — full subcommand reference.
- `docs/design/EPOCHS-AND-JOURNALING.md` — the `Experiment` artifact + journal.
- `docs/design/SCORING.md` — what the tournament gate decides.
- `docs/design/MUTATION-SURFACE.md` — what `inspect mutations` enumerates.
- sibling skills: `zicato-index-ops`, `zicato-design-experiment`,
  `zicato-triage-stuck-loop`.
