# CLI reference

zicato is operated through a single CLI binary, `zicato`. Every
component documented elsewhere is surfaced through at least one
subcommand. This document is the exhaustive reference: every
subcommand, every flag, exit codes, output formats.

The CLI is the primary interface. There is no v0 web UI; harmonograf
exists for the live run view.

## 0. The evolve-centric happy path

The CLI is **evolve-centric**. The day-to-day workflow is two
commands:

```
zicato init       # scaffold a workspace and the contract files
zicato evolve     # run the meta-loop; auto-epochs on contract change
```

`zicato evolve` (§3.11) is the orchestrator. It runs the per-entry
runs, the analysis, the proposal, the patch apply, the tournament,
and the journaling — and it **auto-epochs**: it hashes the evaluation
contract (board + proposer brief + scoring + harness identity) and
rolls a fresh epoch automatically whenever the operator has edited
any of those. So the authoring loop is: edit `board.jsonl` and
`brief.md`, run `zicato evolve`, and the epoch rolls itself.

Every other subcommand in this reference — `zicato board`,
`zicato analyze`, `zicato propose`, `zicato patch apply`,
`zicato tournament`, `zicato epoch`, and the rest — is an
**advanced / debug** tool: a way to drive one stage of the loop in
isolation, inspect intermediate state, or take manual control. They
are fully specified below, but a first-time operator does not need
them; `init` + `evolve` is the path.

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
| `9` | Loop-health degeneracy (`zicato health` found a `warning`/`critical` report; `zicato evolve --stop-on-degenerate` stopped on sustained degeneracy). |

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
  `board.jsonl`, a starter `brief.md` template, and a
  default `scoring.json`.

Idempotent: re-running `zicato init` on an existing workspace is a
no-op (with a warning).

Exit codes: `0`, `2`, `3`.

### 3.2 `zicato register`

Register an inner harness adapter.

```
zicato register
    --adk <module_or_file>:<symbol>
    [--mutable-tree <path>]...
    [--board <path>]
    [--brief <path>]
    [--scoring <path>]
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
| `--board <path>` | no | Canonical `board.jsonl` source path. Defaults to `<workspace_parent>/board.jsonl`. |
| `--brief <path>` | no | Canonical `brief.md` source path. Defaults to `<workspace_parent>/brief.md`. |
| `--scoring <path>` | no | Canonical `scoring.json` source path. Defaults to `<workspace_parent>/scoring.json`. |
| `--call-llm <dotted_path>` | yes | Dotted path to the `harness_call_llm` callable. |
| `--auxiliary-call-llm <dotted_path>` | yes | Dotted path to the `auxiliary_call_llm` callable. |
| `--harness-model <id>` | no | Default model for `harness_call_llm`. |
| `--auxiliary-model <id>` | no | Default model for `auxiliary_call_llm`. |

The registration is persisted to `.zicato/config.json` and used by
every subsequent subcommand.

The `--board` / `--brief` / `--scoring` paths are the
operator's *live, editable* copies of the evaluation contract. They
are recorded under the `contract` key in `config.json` and read back
on every `zicato evolve` for contract-hash auto-epoching (see
[EPOCHS-AND-JOURNALING.md](EPOCHS-AND-JOURNALING.md) §10). On epoch
creation / roll they are frozen (copied) into `epochs/{id}/`.

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
§7. Forbidden ids (those in the proposer brief's `## Forbidden`
section) are rendered with a `[forbidden]` annotation.

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

Reads the current epoch's proposer brief and the patterns produced by
the most recent `analyze` run (or the round named by `--from-round`),
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
zicato epoch new <name> [--from-board <jsonl_path>] [--brief <md_path>]
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
| `--brief <md_path>` | Seed the new epoch's `brief.md` from a file. Default is to inherit. |

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
    [--epoch <epoch_id>]
    [--no-auto-epoch]
    [--epoch-name <name>]
    [--stop-on-reject]
    [--stop-on-no-improvement]
    [--stop-on-degenerate]
    [--max-wall-clock-seconds <S>]
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
| `--mode full\|fast` | `fast` | Tournament mode (see §3.9). `fast` (default) re-scores the champion only when it has no cached aggregate yet; `full` re-runs both generations every round. |
| `--epoch <epoch_id>` | current epoch | Run against a specific epoch. Passing this **skips contract-hash auto-epoching entirely** — the explicit target wins. |
| `--no-auto-epoch` | off (auto-epoch ON) | Disable contract-hash auto-epoching. With this flag, `evolve` errors out when the evaluation contract has drifted from the current epoch instead of rolling. See [EPOCHS-AND-JOURNALING.md](EPOCHS-AND-JOURNALING.md) §10. |
| `--epoch-name <name>` | the `e{N}` scheme | Name for an epoch `evolve` auto-creates (first epoch on a fresh workspace, or the new epoch after a roll). Ignored when `--epoch` is passed or no new epoch is created. |
| `--stop-on-reject` | off | Halt the loop after the first reject. |
| `--stop-on-no-improvement` | off | Halt the loop after K consecutive rounds with no promote (K defaults to 3). |
| `--stop-on-degenerate` | off | Halt the loop the first time loop-health diagnostics report sustained degeneracy (a toothless evaluation). Exits with code `9`. See [LOOP-HEALTH.md](LOOP-HEALTH.md) §6.2. |
| `--max-wall-clock-seconds <S>` | unset (unbounded) | Total wall-clock budget, in seconds, for the **whole** `evolve` invocation. The loop stops cleanly between rounds once the budget is spent, and a single round that would overrun it is cancelled and recorded as an aborted round. This is a ceiling on the *aggregate* — it applies on top of, and does not replace, each board entry's own `wall_clock_budget_seconds`. Both are L1 `asyncio.wait_for` guards: they bound cooperative async work only, not a wedged blocking call (see [ROBUSTNESS.md](ROBUSTNESS.md) §2.1). Also reads the `ZICATO_MAX_WALL_CLOCK_SECONDS` environment variable; an explicit flag wins over the env var. When the loop stops on this budget, the final summary says so explicitly. |
| `--no-dashboard` | off | Do not spawn the supervisor binary. Skips both the watchdog and the live dashboard. CI scripts that want predictable noise sometimes use this; the trade-off is no automatic worker-stall escalation. See [RUNTIME.md](RUNTIME.md) §3 and [DASHBOARD.md](DASHBOARD.md) §2.1. |
| `--dashboard-port <port>` | `7892` | Bind the dashboard's HTTP server to a specific port. If taken, fails — the auto +1 retry only applies to the default. |
| `--dashboard-bind <addr>` | `127.0.0.1` | Bind address for the dashboard. `0.0.0.0` exposes the dashboard to the LAN with no built-in auth; put a reverse proxy in front of it if you do this. |

Exit codes: `0` if all rounds ran cleanly (regardless of how many
promoted vs rejected), `1` for run-level failures, `2`/`3` for
usage/config, `9` if `--stop-on-degenerate` halted the loop on
sustained loop-health degeneracy.

`evolve` is the right command to leave running overnight for a
calibration epoch. A wrapper script that watches its output and
notifies the operator on completion is a reasonable habit.

Auto-spawn behaviour: in the absence of `--no-dashboard`, `evolve`
spawns `zicato-supervisor` as a subprocess and prints the
dashboard URL to stdout. The supervisor exits when `evolve`
exits. See [DASHBOARD.md](DASHBOARD.md) §2 for the auto-spawn
contract and §3.18 (`zicato dashboard --read-only` / `--daemon`)
for when the operator wants the dashboard detached from a
specific `evolve` invocation.

### 3.12 `zicato reindex`

Rebuild the `.zicato/index.db` analytical index from the
filesystem.

```
zicato reindex [--epoch <id>] [--verify]
```

The index is a derived, fully-rebuildable SQLite projection of
the workspace's canonical files (see
[ANALYTICAL-INDEX.md](ANALYTICAL-INDEX.md)). With no flags,
`reindex` acquires the workspace lock, drops every table,
recreates the schema for the current zicato version, and
re-derives every row by walking `lineage.json` and every epoch's
`gen_score.json` / `experiment.json` / `patches/*.json` /
`runs/*/loss.json`.

Flags:

| Flag | Meaning |
|---|---|
| `--epoch <id>` | Reindex only the named epoch's rows; leave other epochs untouched. Useful after hand-editing one epoch's files. |
| `--verify` | Do not rebuild. Walk the filesystem and the index in parallel and report any disagreement (or any canonical file with no index row). The integrity check; CI for the dogfood targets runs it. |

In normal operation the orchestrator dual-writes the index live,
so an explicit `reindex` is rarely needed — it is the correctness
backstop (after a crash, after a hand-edit, after a version
bump). `reindex --verify` always reports clean in healthy
operation; a non-clean result is either a benign behind-index
(a plain `reindex` fixes it) or a real dual-write bug.

```
$ zicato reindex
[reindex] workspace: /home/op/myagent/.zicato
[reindex] dropping + recreating 9 tables (schema v3)
[reindex] epoch initial          : 8 generations, 80 runs, 312 patches
[reindex] epoch 2026-05-15_e1    : 5 generations, 50 runs, 191 patches
[reindex] indexed 13 generations across 2 epochs in 1.4s
```

Exit codes: `0` on a clean rebuild, or on a clean `--verify`; `1`
when `--verify` finds index drift; `2`/`3` for usage/config.

### 3.13 `zicato health`

Run loop-health diagnostics and print the `LoopHealth` report.

```
zicato health [--epoch <id>] [--round <N>] [--format json|text]
```

Loop-health diagnostics (see [LOOP-HEALTH.md](LOOP-HEALTH.md))
detect a *running but meaningless* loop — a degenerate evaluation
that cannot distinguish any candidate. With no flags, `health`
runs every detector against the current epoch's full history and
prints the report for the latest round.

Flags:

| Flag | Meaning |
|---|---|
| `--epoch <id>` | Target a non-current epoch. |
| `--round <N>` | Print the stored report for a specific past round (read straight from `loop_health/round_{NNN}.json`; no recomputation). |
| `--format json` | Emit the `LoopHealth` object verbatim for scripting. |

```
$ zicato health
loop health — epoch 2026-05-15_e1 — round 7 — OVERALL: CRITICAL

  [critical] degenerate_scoring
    v0..v7 all carry gen_score = 1.000000; the evaluation has
    produced zero score variance across 8 generations.
    → Inspect scoring.json weights and the per-entry loss.json
      files; a board where every entry scores identically
      cannot drive a tournament.

  [info] no_expectations
    No board entry carries any expectations; scoring is running on
    drift loss alone.

1 critical, 0 warning, 1 info.
```

A CI / scheduled-run wrapper pairs `zicato health` with
`zicato evolve` so a degenerate epoch is caught the next morning
without an operator eyeballing the journal.

Exit codes: `0` when the report's `overall` is `ok` or `info`;
`9` when `overall` is `warning` or `critical` (a distinct code so
a wrapper can branch on "the loop is degenerate"); `2`/`3` for
usage/config.

### 3.14 `zicato journal show`

```
zicato journal show [--epoch <name>] [--since round=<N>]
```

Renders `journal.md` for the named epoch (default current).
`--since round=N` slices to entries from round N onward.

Exit codes: `0`, `2`, `3`.

### 3.15 `zicato analysis show`

```
zicato analysis show [--epoch <name>]
```

Renders `analysis.md` for the named epoch (default current).
Errors if the named epoch is not closed (no `analysis.md` yet).

Exit codes: `0`, `2`, `3`.

### 3.16 `zicato status` (v1.1+)

Print a snapshot of the runtime state.

```
zicato status [--format json|text]
```

Reads `.zicato/runtime/` directly — does NOT require the
supervisor to be running. Output:

```
$ zicato status
workspace        /home/op/myagent/.zicato
instance_id      default
lock             held by pid 84321 (zicato evolve, started 00:08:42 ago)
heartbeat        fresh (1.2s old) — phase=tournament round=4
supervisor       running on :7892 (pid 84358)
dashboard        http://localhost:7892/
active tournament
  round 4 — v4 → v5 (3 / 10 entries done, 2 in flight, 5 queued)
active runs
  e4f2_long_solar_candidate          v5  long_solar       agent_running  73%
  e4f2_contradictory_brief_parent    v4  contradictory…   adapter_init   12%
```

`--format json` emits the full state-snapshot object (the same
shape as `GET /api/state` on the dashboard — see
[DASHBOARD.md](DASHBOARD.md) §6.1) for scripting.

`zicato status` is useful when:

- The dashboard is unreachable (supervisor wedged or
  unreachable from the operator's terminal).
- A wrapper script wants to know whether an `evolve` is in
  flight before launching another.
- Debugging a stale lock (`zicato status` reports the lock
  holder's PID so the operator can confirm whether it's a real
  conflict or stale state).

Exit codes: `0` when state is readable; `3` when the workspace
doesn't exist or `.zicato/runtime/` is missing.

### 3.17 `zicato kill <run_id>` (v1.1+)

Manual force-kill of an in-flight tournament run.

```
zicato kill <run_id> [--timeout <seconds>]
```

Writes `.zicato/runtime/control/kill_runs/<run_id>`. The
orchestrator picks it up at the next safe-point check (within
~500ms in v1.1; `kill` is a high-priority command). The
orchestrator forwards SIGTERM to the worker; the supervisor's
automatic escalation runs in parallel as a backstop.

`--timeout` controls how long `zicato kill` waits for
confirmation before returning. Default is 30 seconds. If the
run is still alive after the timeout, the command exits with
code 1 — the operator may want to investigate why the
escalation isn't progressing (a SIGKILL-resistant pathology is
exceptionally rare but possible; see
[ROBUSTNESS.md](ROBUSTNESS.md) §3.6).

`<run_id>` is the run identifier shown in `zicato status` or
on the dashboard's active runs list.

Exit codes: `0` on confirmed kill; `1` on timeout; `2` on
usage; `3` if the named run is not active; `4` if the workspace
is not running an `evolve` (no orchestrator to consume the
control file).

### 3.18 `zicato dashboard` (v1.2+; standalone modes)

Launch the supervisor binary in a standalone mode (not tied to
an `evolve` invocation).

```
zicato dashboard --read-only [--epoch <name>] [--port <port>] [--bind <addr>]
zicato dashboard --daemon    [--port <port>] [--bind <addr>]
```

The auto-spawn case (the dashboard launched by `zicato evolve`)
does NOT require this command. `zicato dashboard` is only for
two scenarios:

| Mode | Use case |
|---|---|
| `--read-only` | Post-mortem inspection of a completed epoch. Reads only committed state in `.zicato/epochs/`; no runtime/ interaction. No control surface (all POST endpoints disabled). |
| `--daemon` | Long-running CI scenarios where the dashboard should outlive a specific `evolve` invocation. Uses `.zicato/runtime/supervisor.pid` to ensure only one daemon at a time. Picks up successive `evolve` invocations automatically. |

Flags:

| Flag | Default | Meaning |
|---|---|---|
| `--read-only` | (mode flag) | Post-mortem mode. Disables all control endpoints. Mutually exclusive with `--daemon`. |
| `--daemon` | (mode flag) | Daemon mode. Mutually exclusive with `--read-only`. |
| `--epoch <name>` | current | (read-only only) Which epoch's snapshot to render. |
| `--port <port>` | `7892` | Same as `zicato evolve --dashboard-port`. |
| `--bind <addr>` | `127.0.0.1` | Same as `zicato evolve --dashboard-bind`. |

Exit codes: `0` on clean exit (Ctrl-C, SIGTERM); `1` on
internal error; `2` on usage; `3` on configuration error
(workspace doesn't exist, port unavailable).

### 3.19 `zicato repo` (v0+1)

Print the path to the workspace's internal git repo.

```
zicato repo            # prints absolute path to .zicato/repo/
zicato repo gc         # garbage-collect rejected branches older than N
zicato repo init       # initialize (normally called by `workspace migrate-to-git`)
```

Only available after the workspace has been migrated to the
git backend; see [STORAGE.md](STORAGE.md) §3 for the migration
sequence.

Exit codes: `0`, `2`, `3`.

### 3.20 `zicato log` (v0+1)

Log of every generation in the workspace, surfacing the
zicato-meaningful view rather than raw git log.

```
zicato log [--epoch <name>] [--grep <pattern>] [--oneline] [--since-epoch <name>]
```

Thin wrapper over `git log` plus the `---zicato-meta---` block
parser. Filters out non-experiment commits (e.g. the
empty-commit epoch boundaries from [STORAGE.md](STORAGE.md) §3.6).

`--grep` matches against the experiment's `core_idea`, `why`,
or any `modulating` mutation id. `--since-epoch` walks the
git history back to the named epoch's first commit.

Exit codes: `0`, `2`, `3`.

### 3.21 `zicato diff <gen-a> <gen-b>` (v0+1)

Diff between two generations.

```
zicato diff <gen-a> <gen-b> [--stat]
```

`<gen-a>` and `<gen-b>` may be:

- Full generation ids (e.g. `initial:v3`).
- Short ids when unambiguous in the current epoch (e.g. `v3`).
- `HEAD` for the current promoted head.
- `HEAD~N` for N promotions ago.

Output is a unified diff (git's default). `--stat` produces
the short summary instead. The diff respects file-level
mutation markers — files outside the mutation surface should
never appear in a diff (and a warning is emitted if they do,
since that suggests storage corruption or a manual edit
outside the loop).

Exit codes: `0` on success; `2` on usage; `3` on unknown
generation id.

### 3.22 `zicato show <gen-id>` (v0+1)

Show one generation: experiment metadata + patch diff.

```
zicato show <gen-id>
```

Output combines `git show <commit>` (the patch diff) with the
parsed `---zicato-meta---` block (hypothesis + outcome) so the
operator sees both the *intent* and the *change* in one view.

Format example in [STORAGE.md](STORAGE.md) §3.8.

Exit codes: `0`, `2`, `3`.

### 3.23 `zicato bisect` (v0+1)

Find which generation introduced a regression.

```
zicato bisect <good-gen> <bad-gen>
    --entry <entry_id>
    [--metric drift_loss|pass_fail|<drift_kind>]
```

Powered by `git bisect`: at each bisect step, run the named
entry against the candidate generation and apply the metric to
determine "good" or "bad". Converges in O(log N) generations.

Exit codes: `0` on identification; `1` on inconclusive (the
metric was flat across the range); `2`/`3` for usage/config.

### 3.24 `zicato blame <file:line>` (v0+1)

For a line in the inner-harness source, identify the
generation that last touched it and the hypothesis behind that
change.

```
zicato blame <file>[:<line>]
```

Powered by `git blame` plus the meta-block parser. Output:

```
researcher/agent.py:42
  last touched in epoch hardened_research / generation v3 (round 3)
  hypothesis core_idea:
    "Tighten the researcher's system prompt for citations."
```

Exit codes: `0`, `2`, `3`.

### 3.25 `zicato workspace migrate-to-git` (v0+1)

One-shot conversion of a directory-backed workspace to the
git-backed storage layout.

```
zicato workspace migrate-to-git [--dry-run]
```

Pre-flight checks: no in-flight `evolve` (lock must be free),
no rejected generations newer than the latest promote that the
operator hasn't reviewed (configurable; this is for safety).
Then walks every epoch's `generations/` directory, importing
each generation into the new `.zicato/repo/` git repo with
correct cross-epoch parentage.

A pre-migration backup is written to
`.zicato/migrations/<ts>.tar.gz` automatically. The migration
is destructive (removes `generations/` from each epoch
directory after the import) but reversible by restoring the
backup.

`--dry-run` walks the migration without changing disk; useful
for sizing the post-migration footprint.

The full design is in [STORAGE.md](STORAGE.md) §6.

Exit codes: `0` on clean migration; `1` on failure (the
workspace is left in a recoverable state — the staging
directory is removed and the original `generations/` remain);
`2`/`3` for usage/config.

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
| Authoring boards — the Python builder, outcome/process checks | [BOARD-AUTHORING.md](BOARD-AUTHORING.md) |
| Experiment shape produced by `propose`, consumed by `patch apply` | [EPOCHS-AND-JOURNALING.md](EPOCHS-AND-JOURNALING.md) |
| `gen_score.json` shape produced by `tournament` | [SCORING.md](SCORING.md) |
| Two-callable check enforced at `register` | [EMULATOR.md](EMULATOR.md) |
| State files read by `zicato status`; supervisor binary auto-spawned by `evolve` | [RUNTIME.md](RUNTIME.md) |
| Dashboard panels, API, and standalone modes for `zicato dashboard` | [DASHBOARD.md](DASHBOARD.md) |
| Why subprocess workers, what `zicato kill` is hooked into | [ROBUSTNESS.md](ROBUSTNESS.md) |
| The git-backed roadmap behind `zicato repo` / `log` / `diff` / `show` / `bisect` / `blame` / `workspace migrate-to-git` | [STORAGE.md](STORAGE.md) |
| The analytical index `zicato reindex` rebuilds — schema, discipline | [ANALYTICAL-INDEX.md](ANALYTICAL-INDEX.md) |
| The loop-health diagnostics `zicato health` reports — detectors, severities | [LOOP-HEALTH.md](LOOP-HEALTH.md) |
| The tournament competition model behind `zicato tournament` | [TOURNAMENT.md](TOURNAMENT.md) |
| Why `evolve` defaults to rigorous tournament mode | [RATIONALE.md](RATIONALE.md) |
