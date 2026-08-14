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

The root exposes exactly five direct commands and six advanced namespaces:

| Root command | Purpose |
|---|---|
| `init` | Scaffold a workspace. |
| `evolve` | Resolve the contract and run the loop. |
| `dashboard` | Serve the browser view; `--view builder` opens contract authoring. |
| `tui` | Review the workspace in a terminal. |
| `health` | Report whether the loop has useful optimization signal. |
| `board` | Inspect or edit a frozen board. |
| `epoch` | Inspect or force evaluation-contract boundaries. |
| `proposer` | Generate candidates and improve the proposer. |
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
  apply-recommendation | propose | recommendations | reflect | scorecard
tournament
  run
inspect
  environment | logs | mutations | reflection | telemetry
repair
  epoch-goals | generations | index | judge-losses | report |
  tournament-fk | v0-baseline
```

`inspect reflection` retains its `run`, `practices`, `suggest`, `report`, and
`apply` operations. See the generated help record for their full flags.

## Moved commands

| Capability | Command |
|---|---|
| Register the harness and contract paths | `zicato epoch register` |
| Generate one candidate | `zicato proposer propose` |
| Run one isolated comparison | `zicato tournament run PARENT CHILD` |
| Audit mutation points | `zicato inspect mutations` |
| Read structured logs | `zicato inspect logs` |
| Describe process-boundary environment variables | `zicato inspect environment` |
| Analyze decision telemetry | `zicato inspect telemetry` |
| Run board reflection | `zicato inspect reflection` |
| Rebuild the analytical index | `zicato repair index` |
| Reconcile generation rows | `zicato repair generations` |
| Regenerate an epoch report | `zicato repair report` |
| Run targeted migrations | `zicato repair epoch-goals`, `judge-losses`, `tournament-fk`, or `v0-baseline` |

The standalone builder launcher was removed. The same view is served by:

```sh
zicato dashboard --view builder
```

## Design rules

- `evolve` remains self-orchestrating; advanced commands are debugging and
  recovery tools, not required setup steps.
- Canonical artifact formats and command behavior stay unchanged when a command
  moves.
- The root is explicitly assembled. Adding a module under `cli/commands` does
  not publish a new command accidentally.
- The dashboard builder focus uses the dashboard server and loopback default;
  it does not duplicate launch plumbing.
- Any CLI change regenerates `tools/parity/golden/cli_help.txt` and updates this
  document and every command-bearing operator skill in the same change.

## Verification

```sh
uv run python tools/parity/lib/cli_help.py --update
uv run pytest tests/test_cli*.py -q
bash tools/parity.sh
```
