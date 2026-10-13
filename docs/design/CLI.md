# CLI contract

The command tree is intentionally static. `zicato --help` is authoritative;
`tools/parity/golden/cli_help.txt` is the generated, exhaustive record of every
command, option, default, and help string.

## Product surface

Most operators use two commands:

```sh
zicato init
zicato evolve
```

The root exposes four direct commands and six advanced namespaces:

| Root command | Purpose |
|---|---|
| `init` | Scaffold a workspace. |
| `evolve` | Resolve the contract and run the loop. |
| `dashboard` | Serve the browser view. |
| `health` | Report whether the loop has useful optimization signal. |
| `board` | Inspect or edit a frozen board. |
| `epoch` | Inspect or force evaluation-contract boundaries. |
| `proposer` | Generate candidates or read the proposer scorecard. |
| `tournament` | Run an isolated tournament operation. |
| `inspect` | Read workspace state and derived analysis. |
| `repair` | Rebuild derived data or repair legacy artifacts. |

There is no compatibility alias layer. A capability has one command location.

## Advanced hierarchy

```text
board
  add | audit | judges | list | preflight | remove
epoch
  close | gc | list | new | register | rounds | set-goal | switch
proposer
  checkout | propose | scorecard | submit
tournament
  run
inspect
  config | environment | logs | mutations | reflection | setup | telemetry
repair
  generation-source-backend | generations | index | report | v0-baseline
```

`inspect reflection` provides `run`, `practices`, `suggest`, and `report`.
Suggestions are reports for operator review. See the generated help record
for the full flags.

## Command locations for common tasks

| Capability | Command |
|---|---|
| Register the harness and contract paths | `zicato epoch register` |
| Validate setup without model requests | `zicato inspect setup` |
| Generate one candidate | `zicato proposer propose` |
| Copy the champion for an operator candidate | `zicato proposer checkout --into DIR` |
| Queue operator candidates as the next round | `zicato proposer submit --from DIR [--from DIR ...] --author NAME [--exclusive]` |
| Run one isolated comparison | `zicato tournament run PARENT CHILD` |
| Audit mutation points | `zicato inspect mutations` |
| Read structured logs | `zicato inspect logs` |
| Describe process-boundary environment variables | `zicato inspect environment` |
| Analyze decision telemetry | `zicato inspect telemetry` |
| Run board reflection | `zicato inspect reflection` |
| Rebuild the analytical index | `zicato repair index` |
| Reconcile generation rows | `zicato repair generations` |
| Regenerate an epoch report | `zicato repair report` |
| Backfill the synthetic `v0` experiment marker | `zicato repair v0-baseline` |
| Set the generation source backend on an existing workspace | `zicato repair generation-source-backend --backend <git\|directory>` |

## Design rules

- `evolve` remains self-orchestrating. The advanced commands are debugging
  and recovery tools, and no setup step requires them.
- `proposer checkout` and `proposer submit` take no workspace lock and
  administer no git worktrees, so they can run while `evolve` runs. `submit`
  only queues a round of candidates, at most one per field slot; the next
  round mints, measures, and settles them like proposer candidates. Both
  print one JSON object, also when they refuse, and then exit 1. The
  operator guide is `skills/zicato-submit-candidates`.
- Every `evolve` invocation runs the workspace validators at the public spend
  boundary before auto-epoching or model spend, and refuses to start on any
  finding that would make the round unmeasurable. `--dry-run` runs the same
  validators, reports board and mutation-surface sizes, sends one short
  reachability request per configured model role once the validators pass,
  and exits without starting the loop. A role that does not answer makes the
  dry run exit nonzero.
- Canonical artifact formats and command behavior stay unchanged when a command
  moves.
- The root is explicitly assembled. Adding a module under `cli/commands` does
  not publish a new command accidentally.
- Any CLI change regenerates `tools/parity/golden/cli_help.txt` and updates this
  document and every command-bearing operator skill in the same change.

## Verification

```sh
uv run python tools/parity/lib/cli_help.py --update
uv run pytest tests/test_cli*.py -q
bash tools/parity.sh
```
