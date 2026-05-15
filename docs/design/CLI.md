# CLI reference

zicato is operated through a single CLI binary, `zicato`. Every
component documented elsewhere is surfaced through at least one
subcommand. This document is the exhaustive reference: every
subcommand, every flag, exit codes, output formats.

The CLI is the primary interface. There is no v0 web UI; harmonograf
exists for the live run view.

## 1. Conventions

- Subcommands form a verb-noun shape: `zicato board add`,
  `zicato epoch close`, `zicato patch apply`.
- All subcommands accept `--workspace <path>` to override the
  default `.zicato/` workspace.
- All subcommands that need an instance accept `--instance <id>` to
  select a non-default zicato instance (for target 3 — see
  [DOGFOOD-TARGETS.md](DOGFOOD-TARGETS.md)).
- All subcommands accept `--format json|text` (default `text`) for
  scriptable output.
- All subcommands accept `--verbose / -v` and `--quiet / -q`.

## 2. Exit codes

| Code | Meaning |
|---|---|
| `0` | Success. |
| `1` | Generic failure. |
| `2` | Usage error (missing flag, invalid value). |
| `3` | Configuration error (missing workspace, missing registration, missing source roots). |
| `4` | Schema violation (board entry rejected, experiment hypothesis malformed). |
| `5` | Validator rejection (patch failed V1-V7 in [MUTATION-SURFACE.md](MUTATION-SURFACE.md)). |
| `6` | Tournament rejection (candidate failed promotion gate). Subcommand returns 6 with a successful informational output — useful in scripts that decide what to do next. |
| `7` | Wall-clock budget exhausted. |
| `8` | Collusion check failed (the two `call_llm` callables are identical). |

The non-zero codes are designed so a shell pipeline can branch on
the precise failure shape (`zicato evolve` returning `6` means "no
promotion"; the operator's wrapper script can treat that as a normal
outcome and try again next round).

## 3. Subcommand reference

### 3.1 `zicato init`

Initialize a new workspace.

```
zicato init [--workspace <path>] [--instance <id>]
```

- Creates the workspace directory (default `.zicato/`).
- Writes `.zicato/config.json` with the instance id and default
  paths.
- Creates an initial epoch named `initial` with an empty
  `board.jsonl`, a starter `rubric.md` template, and a default
  `scoring.json`.

Idempotent: re-running `zicato init` on an existing workspace is a
no-op (with a warning).

Exit codes: `0`, `2`, `3`.

### 3.2 `zicato register`

Register an inner harness adapter.

```
zicato register
    --adk <module_or_file>:<symbol>
    [--mutable-tree <path>]...
    [--call-llm <dotted_path>]
    [--auxiliary-call-llm <dotted_path>]
    [--harness-model <model_id>]
    [--auxiliary-model <model_id>]
```

Flags:

| Flag | Required | Meaning |
|---|---|---|
| `--adk path:symbol` | yes (for ADK) | Adapter selector + entry point. `path` is a Python file or module; `symbol` is the root agent factory. |
| `--mutable-tree <path>` | repeatable, at least one | Source root the mutation enumerator should walk. Repeat for multiple roots (target 2 — see [DOGFOOD-TARGETS.md](DOGFOOD-TARGETS.md)). |
| `--call-llm <dotted_path>` | yes | Dotted path to the `harness_call_llm` callable. |
| `--auxiliary-call-llm <dotted_path>` | yes | Dotted path to the `auxiliary_call_llm` callable. |
| `--harness-model <id>` | no | Default model for `harness_call_llm`. |
| `--auxiliary-model <id>` | no | Default model for `auxiliary_call_llm`. |

The registration is persisted to `.zicato/config.json` and used by
every subsequent subcommand.

The two-callable check runs at register time. If
`--call-llm == --auxiliary-call-llm` AND no distinct model
override is provided, the command exits with code `8`.

Exit codes: `0`, `2`, `3`, `8`.

### 3.3 `zicato board`

Manage board entries in the current epoch.

#### 3.3.1 `zicato board add`

```
zicato board add --entry <json_file> [--force]
```

Reads one JSON board entry from `<json_file>` and appends it to the
current epoch's `board.jsonl`. The entry is validated against the
schema in [BOARD-FORMAT.md](BOARD-FORMAT.md) §7 before being written.

Mid-epoch additions emit a warning and require `--force` because
they change the evaluation contract:

```
$ zicato board add --entry new_entry.json
ERROR: cannot add an entry mid-epoch (contract change). Either:
  - zicato epoch new <name>   then add the entry there
  - rerun with --force        to override (will degrade pattern history)
```

Exit codes: `0`, `2`, `4`.

#### 3.3.2 `zicato board list`

```
zicato board list [--tag <tag>]... [--format json|text]
```

Renders every entry in the current epoch's board. The `--tag` flag
filters to entries that have all named tags.

Text format:

```
id                            kind                    weight  tags
short_solar                   single_turn             1.0     easy, presentation
long_solar_with_constraints   single_turn             1.5     medium, presentation, long
contradictory_brief           single_turn             1.0     hard, ambiguous
revision_dialog               multi_turn_scripted     1.0     multi-turn, revision
expert_review                 multi_turn_emulated     1.0     multi-turn, emulated, expert

5 entries (3 single-turn, 1 scripted multi-turn, 1 emulated multi-turn)
```

JSON format is one object per entry, the same shape that
`board.jsonl` carries.

Exit codes: `0`, `2`.

#### 3.3.3 `zicato board remove`

```
zicato board remove --id <entry_id> [--force]
```

Removes the entry by id. Same mid-epoch protection as `add`.

Exit codes: `0`, `2`, `4`.

### 3.4 `zicato mutations`

Audit the current mutation surface.

```
zicato mutations
    [--id <glob>]
    [--kind span|file]
    [--show full]
    [--root <path>]
    [--format json|text]
```

Walks every registered source root, calls `mutation_points()`, and
renders the result.

Flags:

| Flag | Meaning |
|---|---|
| `--id <glob>` | Filter by id glob (`researcher.*` matches `researcher.instruction`, `researcher.description`, etc.). |
| `--kind span\|file` | Filter by marker form. |
| `--show full` | Print the full `current_text` instead of a preview. Default is a 64-char preview with `...` truncation. |
| `--root <path>` | Restrict to one registered source root. |
| `--format json` | Emit JSON (the full `MutationPoint` shape) instead of text. |

Text output is described in [MUTATION-SURFACE.md](MUTATION-SURFACE.md)
§7. Forbidden ids (those in the rubric's `## Forbidden` section) are
rendered with a `[forbidden]` annotation.

Exit codes: `0`, `2`, `3`.

### 3.5 `zicato run`

Run one entry against one generation.

```
zicato run
    --generation v<N>
    --entry <entry_id>
    [--tail]
```

Loads the generation's snapshot, instantiates the adapter, runs the
entry (single-turn, multi-turn scripted, or multi-turn emulated as
declared in the board), writes `events.jsonl` and `loss.json` to
the standard path.

If `--tail` is set, the runner ALSO attaches an in-process accumulator
sink that prints a live drift-count summary to stderr. The JSONL
file is still written (canonical record); the tail is ergonomic.

Exit codes: `0` on a successful run (`RunCompleted`), `7` on
wall-clock budget exhaustion (`RunAborted` with that reason), `1`
for other run failures.

### 3.6 `zicato analyze`

Aggregate the current epoch's loss profiles into patterns.

```
zicato analyze [--generation v<N>]
```

Walks every `loss.json` in the current epoch (or for one
generation if `--generation` is given), runs the pattern detectors,
and writes `.zicato/epochs/{epoch}/patterns/round_{NNN}.json`.

The output is the input to `zicato propose`. Run `analyze` after
every generation's runs to produce the patterns the proposer reads.

Exit codes: `0`, `2`, `3`.

### 3.7 `zicato propose`

Run the proposer; emit an `Experiment`.

```
zicato propose --output <file> [--from-round <NNN>]
```

Reads the current epoch's rubric and the patterns produced by the
most recent `analyze` run (or the round named by `--from-round`),
calls the proposer (using `auxiliary_call_llm`), validates the
output against the experiment schema (see
[EPOCHS-AND-JOURNALING.md](EPOCHS-AND-JOURNALING.md) §3), and writes
the experiment to `<file>`.

The proposer is given one retry on schema violations. A second
violation exits with `4`.

Exit codes: `0`, `2`, `3`, `4`.

### 3.8 `zicato patch apply`

Apply an experiment's patches to a new candidate snapshot.

```
zicato patch apply --experiment <file> --as v<N>
```

Reads the experiment JSON from `<file>`, validates every patch against
the constraints in [MUTATION-SURFACE.md](MUTATION-SURFACE.md) §6,
creates the new generation directory, writes the candidate snapshot
under `generations/v<N>/snapshot/`, and writes
`generations/v<N>/experiment.json` and
`generations/v<N>/patches_applied.json`.

`<N>` should be one more than the current parent generation. The CLI
errors if `<N>` already exists; the operator must choose a fresh
number.

Exit codes: `0`, `2`, `3`, `4`, `5`.

### 3.9 `zicato tournament`

Run the tournament between two generations.

```
zicato tournament v<N> v<M>
    [--mode tournament|fast]
    [--no-record-outcome]
```

The default mode is `tournament` — re-run the whole board against
both `v<N>` (the parent) and `v<M>` (the candidate), compute
`gen_score.json` for each, and decide promote/reject.

`--mode fast` reuses the parent's existing `gen_score.json` instead
of re-running. See [SCORING.md](SCORING.md) §7.

By default the tournament records the outcome to the candidate's
`experiment.json` and appends to `journal.md`. `--no-record-outcome`
runs the comparison without writing — useful for sanity-checking
the gate before committing.

Exit codes: `0` on promote, `6` on reject, `2`/`3` for usage/config.

### 3.10 `zicato epoch`

Manage epochs.

#### 3.10.1 `zicato epoch new`

```
zicato epoch new <name> [--from-board <jsonl_path>] [--from-rubric <md_path>]
```

Creates a new epoch named `<name>`. The new epoch's `v0` snapshot is
the final promoted generation of the previous epoch (or the
register-time source if this is the first epoch after `init`).

If the previous epoch was not closed manually, `epoch new` auto-closes
it with a warning (see
[EPOCHS-AND-JOURNALING.md](EPOCHS-AND-JOURNALING.md) §5.1).

Flags:

| Flag | Meaning |
|---|---|
| `--from-board <jsonl_path>` | Seed the new epoch's `board.jsonl` from a file. Default is to inherit from the previous epoch. |
| `--from-rubric <md_path>` | Seed the new epoch's `rubric.md` from a file. Default is to inherit. |

Exit codes: `0`, `2`, `3`.

#### 3.10.2 `zicato epoch close`

```
zicato epoch close [<name>] [--focus <text>]
```

Closes an epoch. Runs the analysis pass and writes `analysis.md`.

If `<name>` is omitted, closes the current epoch. `--focus` passes a
free-text directive to the analysis pass ("focus on what patterns
remain unresolved").

Exit codes: `0`, `2`, `3`.

#### 3.10.3 `zicato epoch list`

```
zicato epoch list [--format json|text]
```

Renders `lineage.json` as a table (text) or as JSON. See
[EPOCHS-AND-JOURNALING.md](EPOCHS-AND-JOURNALING.md) §6.

Exit codes: `0`, `2`.

#### 3.10.4 `zicato epoch switch`

```
zicato epoch switch <name>
```

Switches the workspace's "current epoch" to `<name>`. Useful for
revisiting an older closed epoch (e.g. to render its analysis).

Switching to a closed epoch does NOT reopen it; the workspace is
read-only on the closed epoch's directory. Run commands that try to
write (e.g. `analyze`, `propose`, `run`) error.

Exit codes: `0`, `2`, `3`.

### 3.11 `zicato evolve`

The orchestrator. One command, many rounds.

```
zicato evolve
    [--rounds <N>]
    [--mode tournament|fast]
    [--stop-on-reject]
    [--stop-on-no-improvement]
```

Runs the full meta-loop for `--rounds N` rounds:

1. `zicato run` on every board entry against the current parent (if
   the parent's runs are not already cached for this epoch).
2. `zicato analyze` to update patterns.
3. `zicato propose` to produce an experiment.
4. `zicato patch apply` to create the candidate snapshot.
5. `zicato run` on every board entry against the candidate.
6. `zicato tournament` to decide.
7. On promote, the candidate becomes the new parent; on reject, the
   parent stays.
8. Loop back to step 2 unless a stop condition fired.

Flags:

| Flag | Default | Meaning |
|---|---|---|
| `--rounds <N>` | `1` | How many rounds to attempt. |
| `--mode tournament\|fast` | `tournament` | Tournament mode (see §3.9). |
| `--stop-on-reject` | off | Halt the loop after the first reject. |
| `--stop-on-no-improvement` | off | Halt the loop after K consecutive rounds with no promote (K defaults to 3). |

Exit codes: `0` if all rounds ran cleanly (regardless of how many
promoted vs rejected), `1` for run-level failures, `2`/`3` for
usage/config.

`evolve` is the right command to leave running overnight for a
calibration epoch. A wrapper script that watches its output and
notifies the operator on completion is a reasonable habit.

### 3.12 `zicato journal show`

```
zicato journal show [--epoch <name>] [--since round=<N>]
```

Renders `journal.md` for the named epoch (default current).
`--since round=N` slices to entries from round N onward.

Exit codes: `0`, `2`, `3`.

### 3.13 `zicato analysis show`

```
zicato analysis show [--epoch <name>]
```

Renders `analysis.md` for the named epoch (default current).
Errors if the named epoch is not closed (no `analysis.md` yet).

Exit codes: `0`, `2`, `3`.

## 4. Output formats

### 4.1 Text format

Default. Human-readable. Designed to fit a terminal width of 100
columns; long fields wrap.

Color and bold are used sparingly:

- Promotion / acceptance is rendered without color.
- Rejection / failure / error is rendered in red.
- Warnings (auto-close on epoch new, mid-epoch board edits) are
  rendered in yellow.

A `--no-color` flag disables color globally.

### 4.2 JSON format

`--format json` emits one JSON object per logical record. The shape
matches the on-disk shape where there is one, or a per-subcommand
shape documented in this file.

JSON output is line-delimited where appropriate (e.g.
`zicato board list --format json` emits one entry per line — the
same shape as `board.jsonl`).

## 5. Configuration file

`.zicato/config.json` is created by `zicato init` and updated by
`zicato register`. It carries:

```json
{
  "instance_id": "default",
  "workspace_root": ".zicato",
  "current_epoch": "initial",
  "adapter": {
    "kind": "adk",
    "entry": "myproj.agents:root_agent",
    "mutable_trees": [
      "myproj/agents"
    ]
  },
  "call_llm": {
    "harness": "myproj.llm:harness_call_llm",
    "auxiliary": "myproj.llm:auxiliary_call_llm",
    "harness_model": "<model id>",
    "auxiliary_model": "<model id>"
  },
  "created_at": "2026-04-01T10:00:00Z"
}
```

The two `call_llm` entries are dotted paths. Resolution happens at
run time; the operator is responsible for ensuring the paths import.

`current_epoch` is updated by `zicato epoch switch` and
`zicato epoch new`.

## 6. Scripting hints

A typical CI / scheduled-run wrapper:

```bash
#!/usr/bin/env bash
set -euo pipefail

zicato evolve --rounds 5 --stop-on-no-improvement
ec=$?

case "$ec" in
    0)  echo "round complete"; zicato journal show --since round=$(date +%j) ;;
    6)  echo "no promotions this batch — keep going next time" ;;
    7)  echo "wall-clock exhausted on an entry; consider raising budgets" ;;
    *)  echo "unexpected failure: $ec"; exit "$ec" ;;
esac
```

The exit codes are designed for this — the script branches cleanly
on the meaningful outcomes.

## 7. Cross-references

| Topic | Document |
|---|---|
| Registration semantics, `mutation_points()` over multiple roots | [MUTATION-SURFACE.md](MUTATION-SURFACE.md) |
| Board entry schema accepted by `board add` | [BOARD-FORMAT.md](BOARD-FORMAT.md) |
| Experiment shape produced by `propose`, consumed by `patch apply` | [EPOCHS-AND-JOURNALING.md](EPOCHS-AND-JOURNALING.md) |
| `gen_score.json` shape produced by `tournament` | [SCORING.md](SCORING.md) |
| Two-callable check enforced at `register` | [EMULATOR.md](EMULATOR.md) |
| Why `evolve` defaults to rigorous tournament mode | [RATIONALE.md](RATIONALE.md) |
