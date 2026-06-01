# CLI reference

zicato is operated through a single CLI binary, `zicato`. Every
component documented elsewhere is surfaced through at least one
subcommand. This document is the exhaustive reference: every
subcommand, every flag, exit codes, output formats.

The CLI is the primary interface. There is no v0 web UI; harmonograf
exists for the live run view.

> **Advisory.** This document is a design reference and can drift from
> the shipped binary. When the two disagree, **`zicato --help` is
> authoritative.** Flags marked *(DESIGN, not yet implemented)* — e.g.
> `evolve --tournament-structure` (§3.11) — are specified here ahead of
> implementation and will not appear in `zicato --help` until they ship.

## 0. The evolve-centric happy path

The CLI is **evolve-centric**. The day-to-day workflow is two
commands:

```
zicato init       # scaffold a workspace and the contract files
zicato evolve     # run the meta-loop; auto-epochs on contract change
```

`zicato evolve` (§3.11) is the orchestrator. It runs the per-entry
runs, the analysis, the proposal, the patch apply (internal to the
loop), the tournament, and the journaling — and it **auto-epochs**:
it hashes the evaluation contract (board + proposer brief + scoring +
harness identity) and rolls a fresh epoch automatically whenever the
operator has edited any of those. So the authoring loop is: edit
`board.jsonl` and `brief.md`, run `zicato evolve`, and the epoch rolls
itself.

Every other subcommand in this reference — `zicato board`,
`zicato analyze-telemetry`, `zicato propose`, `zicato tournament`,
`zicato epoch`, and the rest — is an **advanced / debug** tool: a way
to drive one stage of the loop in isolation, inspect intermediate
state, or take manual control. They are fully specified below, but a
first-time operator does not need them; `init` + `evolve` is the path.

> Note: there is no standalone `zicato run` or `zicato patch apply`
> command. Running a single board entry and applying an experiment's
> patches both happen *inside* `tournament` / `evolve`; they are not
> exposed as top-level verbs. `zicato propose` writes its
> `experiment.json` straight into the next generation directory (it
> does not apply patches or run anything).

## 1. Conventions

- Subcommands form a verb-noun shape: `zicato board add`,
  `zicato epoch close`.
- Almost every subcommand accepts `--workspace <path>` to override the
  default `.zicato/` workspace (the global default is `.zicato`).
- The instance id is recorded once at `zicato init` time via
  `--instance-id` and stored in `config.json`; there is no per-command
  `--instance` selector (the per-instance multi-workspace story for
  target 3 — see [DOGFOOD-TARGETS.md](DOGFOOD-TARGETS.md) — is not yet
  surfaced on the CLI).
- A `--format` flag exists only where noted (today: `zicato mutations
  --format table|json`). There is **no** global `--format`,
  `--verbose/-v`, `--quiet/-q`, or `--no-color` flag — those are
  planned conventions, not yet shipped.

## 2. Exit codes

The shipped CLI is a [click](https://click.palletsprojects.com/)
application and uses click's standard exit semantics. It does **not**
emit the fine-grained `3`–`9` code space an earlier draft of this doc
described; treat the table below as authoritative.

| Code | Meaning |
|---|---|
| `0` | Success. |
| `1` | Generic failure. Every operational error surfaces here: a `click.ClickException` (missing workspace / registration, unparseable config, schema-invalid board entry, proposer giving up after `--max-retries`, the two-callable collusion guard tripping as a `RuntimeError`, a tournament that errors out), and a `zicato health` run whose overall severity is `warning`/`critical`. |
| `2` | Usage error — click's own code for a missing required option, an unknown flag, or an out-of-range value (`UsageError` / `BadParameter`). |

Notes on what is **not** a distinct code today:

- A tournament **rejection** (candidate failed the promote gate) is a
  *successful* run as far as the process is concerned: `zicato
  tournament` and a rejecting `evolve` round still exit `0`. The
  promote/reject decision is read from the printed output and the
  recorded `outcome`, not from the exit code. (A dedicated
  non-promotion code is a planned convenience for wrapper scripts — see
  §6 — but is not shipped.)
- Wall-clock-budget exhaustion, validator (V1–V7) rejection, and the
  collusion guard do not have their own exit codes; they surface as the
  generic `1` (or, for budget, as a recorded *aborted* round inside a
  loop that itself exits `0`).

## 3. Subcommand reference

### 3.1 `zicato init`

Initialize a new workspace.

```
zicato init [--workspace <path>] [--instance-id <id>] [--force]
```

- Creates the workspace directory if it doesn't exist (default
  `.zicato/`).
- Writes an empty lineage DAG `lineage.json` as `{"nodes": [],
  "edges": []}`.
- Writes `config.json` containing `{instance_id, created_at}` — the
  instance id comes from `--instance-id` (default `default`).

It does **not** create an `initial` epoch or scaffold any
`board.jsonl` / `brief.md` / `scoring.json` — those are the operator's
live contract files (see `register`, §3.2) and are frozen into an
epoch only when one is opened (by `zicato evolve`'s auto-epoching or by
`zicato epoch new`).

`init` is **not** idempotent: it refuses to overwrite an existing
workspace unless `--force` is passed, and `--force` only rewrites
`config.json` / `lineage.json` (it does not delete epoch artifacts
living alongside).

Exit codes: `0`; `2` on a usage error (e.g. attempting to overwrite an
existing workspace without `--force`, which is raised as a click
`UsageError`).

### 3.2 `zicato register`

Record the adapter entrypoint, mutable trees, and contract paths.

This is an **advanced** command: `zicato evolve` resolves the contract
itself. Run `register` by hand only to pin the contract source paths up
front, or to point the workspace at a different agent / brief.

```
zicato register
    --adk <module.path>:<symbol>
    [--mutable-tree <path>]...
    [--board <path>]
    [--brief <path>]
    [--scoring <path>]
    [--workspace <path>]
```

Flags:

| Flag | Required | Meaning |
|---|---|---|
| `--adk path:symbol` | yes | Adapter entrypoint in `module.path:agent_symbol` form. |
| `--mutable-tree <path>` | repeatable | Source root the proposer is allowed to mutate. Repeat for multiple roots (target 2 — see [DOGFOOD-TARGETS.md](DOGFOOD-TARGETS.md)). |
| `--board <path>` | no | Canonical `board.jsonl` source path. Defaults to `<workspace_parent>/board.jsonl`. |
| `--brief <path>` | no | Canonical proposer-brief (`brief.md`) source path. Defaults to `<workspace_parent>/brief.md`. |
| `--scoring <path>` | no | Canonical `scoring.json` source path. Defaults to `<workspace_parent>/scoring.json`. |
| `--workspace <path>` | no | Workspace directory to update. Default `.zicato`. |

`register` **merges** into the existing `config.json` rather than
replacing it, so any keys `zicato init` wrote (`instance_id`,
`created_at`) are preserved.

The `--board` / `--brief` / `--scoring` paths are the operator's
*live, editable* copies of the evaluation contract. They are stored
under the `contract` key in `config.json` and read back on every
`zicato evolve` for contract-hash auto-epoching (see
[EPOCHS-AND-JOURNALING.md](EPOCHS-AND-JOURNALING.md) §10). On epoch
creation / roll they are frozen (copied) into `epochs/{id}/`.

Note: the two `call_llm` callables are **not** registered here — they
are passed at run time as `zicato evolve --harness-call-llm` /
`--auxiliary-call-llm` (§3.11). The two-callable collusion guard is
enforced when the runtime is constructed (it raises a `RuntimeError`,
surfacing as exit `1`), not at `register` time.

Exit codes: `0`; `2` for a usage error (e.g. a malformed `--adk`
selector, raised as `BadParameter`/`UsageError`).

### 3.3 `zicato board`

Manage board entries in the current epoch.

The board is part of the evaluation contract, and `zicato evolve`
rolls the epoch when the live board changes — use this group only to
inspect or hand-edit a frozen board.

#### 3.3.1 `zicato board add`

```
zicato board add [--workspace <path>] <entry_path>
```

Reads one JSON board entry from the positional `<entry_path>` and
appends it to the current epoch's `board.jsonl`. The entry is
validated against the schema in [BOARD-FORMAT.md](BOARD-FORMAT.md) §7
before being written; an invalid entry is rejected as a
`ClickException`.

There is no `--entry` flag (the path is positional) and no `--force` /
mid-epoch-acknowledgment flag on the shipped command — the
contract-change protection lives in `evolve`'s auto-epoching, not in
`board add`.

Exit codes: `0`; `1` if the file is unreadable, not a JSON object, or
fails entry validation.

#### 3.3.2 `zicato board list`

```
zicato board list [--workspace <path>]
```

Renders every entry in the current epoch's board. There is no `--tag`
filter and no `--format` flag today; output is human-readable text.

Exit codes: `0`; `1` on error (e.g. no board found).

#### 3.3.3 `zicato board remove`

```
zicato board remove [--workspace <path>] <entry_id>
```

Removes the entry named by the positional `<entry_id>` from the
current epoch's board. No `--force` flag.

Exit codes: `0`; `1` on error.

### 3.4 `zicato mutations`

Audit the current mutation surface.

```
zicato mutations
    [--workspace <path>]
    [--id <glob>]
    [--kind span|file]
    [--show preview|full]
    [--format table|json]
```

Walks every registered source root, calls `mutation_points()`, and
renders the result.

Flags:

| Flag | Default | Meaning |
|---|---|---|
| `--workspace <path>` | `.zicato` | Workspace directory. |
| `--id <glob>` | — | Filter mutation points by id glob (e.g. `researcher_*`). |
| `--kind span\|file` | — | Restrict the listing to one mutation kind. |
| `--show preview\|full` | `preview` | `preview` truncates content previews; `full` dumps full content. |
| `--format table\|json` | `table` | Output format (`json` emits the full `MutationPoint` shape). |

Text output is described in [MUTATION-SURFACE.md](MUTATION-SURFACE.md)
§7. (There is no `--root` flag — all registered roots are walked.)

Exit codes: `0`; `1` on a configuration error (e.g. no registration in
`config.json`, raised as a `ClickException`).

### 3.5 No standalone `run` or `patch apply`

There is **no** `zicato run` command and **no** `zicato patch apply`
command. Running a board entry against one generation and applying an
experiment's patches into a candidate snapshot both happen *inside*
the tournament / evolve machinery — they are not exposed as top-level
verbs. To exercise either step manually, run a single `zicato
tournament PARENT CHILD` (§3.8) or one `zicato propose` (§3.7) round.

### 3.6 `zicato analyze-telemetry`

Run the decision-telemetry analyzer for an epoch.

```
zicato analyze-telemetry
    [--workspace <path>]
    [--epoch <id>]
    [--round <N>]
```

Advanced / off the happy path — `zicato evolve` runs the analyzer per
round. Use this to (re)generate an insight for an epoch out of band.

Flags:

| Flag | Default | Meaning |
|---|---|---|
| `--workspace <path>` | `.zicato` | Workspace directory. |
| `--epoch <id>` | current epoch | Epoch id. Defaults to the workspace's `current_epoch` marker. |
| `--round <N>` | — | Round number for the output filename. With it, writes `insights/round_{N:04d}.md`; omit it to write `insights/latest.md` instead. |

This command supersedes the earlier-drafted `zicato analyze` /
`zicato analysis show`. It does not take a `--generation` flag and it
writes insight markdown (not a `patterns/round_{NNN}.json` file —
pattern detection runs inside `propose` / `evolve`).

Exit codes: `0`; `1` on a configuration error (e.g. no active epoch,
raised as a `ClickException`).

### 3.7 `zicato propose`

Generate one `Experiment` for the next generation.

```
zicato propose
    [--workspace <path>]
    [--epoch <id>]
    [--patterns-from <path>]
    [--max-retries <N>]
```

Advanced / off the happy path — `zicato evolve` proposes on every
round. Run this by hand only to produce and inspect a single
experiment without running the tournament.

Reads the current epoch's proposer brief and the run patterns
(detectors are run fresh unless `--patterns-from` points at a Patterns
JSON file), calls the proposer (using the auxiliary callable),
validates the output against the experiment schema (see
[EPOCHS-AND-JOURNALING.md](EPOCHS-AND-JOURNALING.md) §3), and writes
`experiment.json` **directly into the next generation directory** —
there is no `--output` flag; the proposer owns where the experiment
lands.

Flags:

| Flag | Default | Meaning |
|---|---|---|
| `--workspace <path>` | `.zicato` | Workspace directory. |
| `--epoch <id>` | current epoch | Epoch id. Defaults to the `current_epoch` marker. |
| `--patterns-from <path>` | — | Path to a Patterns JSON file. If absent, the detectors are run fresh. |
| `--max-retries <N>` | `2` (range 0–10) | How many times to ask the proposer to fix a malformed response. |

When the proposer exhausts `--max-retries` on schema-invalid output,
the command fails as a `ClickException` (exit `1`).

Exit codes: `0`; `1` on a configuration error or an unrecoverable
schema violation; `2` if `--max-retries` is out of range.

### 3.8 `zicato tournament`

Run the tournament between two generations.

```
zicato tournament <parent> <child>
    [--workspace <path>]
    [--epoch <id>]
    [--mode full|fast]
    [--skip-regression]
```

`<parent>` and `<child>` are **positional** generation ids (e.g. `v3
v4`). The default mode is **`full`** — re-run the whole board against
both the parent and the child, compute the score for each, and decide
promote/reject. `--mode fast` runs the child against the parent's
historical aggregate instead of re-running the parent. See
[SCORING.md](SCORING.md) §7.

`--skip-regression` skips the regression-suite gate even when it is
enabled in `scoring.json`.

There is no `--no-record-outcome` flag on the shipped command; the
tournament records its outcome as part of running.

Exit codes: `0` whether the child is promoted **or** rejected — a
rejection is a normal, successful run (read the decision from the
output / recorded `outcome`, not the exit code); `1` on error (bad
generation pair, wiring failure); `2` for a usage error.

### 3.10 `zicato epoch`

Manage epochs.

#### 3.10.1 `zicato epoch new`

```
zicato epoch new <name>
    --board <jsonl_path>
    --brief <md_path>          # alias: --rubric (legacy)
    [--scoring <json_path>]
    [--goal <text>]
    [--workspace <path>]
```

Creates a new epoch named `<name>` and makes it current. The supplied
contract files (`--board`, `--brief`, optional `--scoring`) are both
**frozen** into the epoch directory AND published as the workspace's
live contract (recorded in `config.json` under `contract`), so a
subsequent `zicato evolve` resolves the same contract and continues
this epoch rather than spuriously rolling.

If a previous epoch is still open it is auto-closed first (the
auto-close emits a stub `analysis.md` — no auxiliary LLM is wired
through the CLI yet). See
[EPOCHS-AND-JOURNALING.md](EPOCHS-AND-JOURNALING.md) §5.1.

Flags:

| Flag | Required | Meaning |
|---|---|---|
| `--board <jsonl_path>` | yes | Board to freeze into the epoch and adopt as the live contract board. |
| `--brief <md_path>` | yes | Proposer brief to freeze + adopt. `--rubric` is accepted as a legacy alias. |
| `--scoring <json_path>` | no | `scoring.json`; defaults applied if absent. When given, frozen + adopted. |
| `--goal <text>` | no | Free-form statement of *why* this epoch exists. Persisted into `config.json` and surfaced in the analyzer report header. When omitted and stdin is a TTY the operator is prompted for one line; in non-TTY contexts it defaults to the empty string. |

Note: `--board` and `--brief` are **required** and the legacy
`--from-board` flag does not exist (the brief seed is `--brief` /
`--rubric`).

Exit codes: `0`; `2` for a usage error (e.g. missing required
`--board` / `--brief`).

#### 3.10.2 `zicato epoch close`

```
zicato epoch close [<epoch_id>] [--workspace <path>]
```

Closes an epoch and (best-effort) writes its `analysis.md`. If
`<epoch_id>` is omitted, the current epoch is closed. The analysis pass
runs only if an auxiliary LLM has been configured; until then this
writes a stub `analysis.md` the operator can regenerate later (see
`regenerate-report`, §3.12.2). There is no `--focus` flag.

Exit codes: `0`; `1` on error.

#### 3.10.3 `zicato epoch list`

```
zicato epoch list [--workspace <path>]
```

Lists every epoch in the workspace as a markdown table. There is no
`--format` flag. See
[EPOCHS-AND-JOURNALING.md](EPOCHS-AND-JOURNALING.md) §6.

Exit codes: `0`; `1` on error.

#### 3.10.4 `zicato epoch switch`

```
zicato epoch switch <epoch_id> [--workspace <path>]
```

Points the workspace's `current_epoch` marker at `<epoch_id>`. Useful
for revisiting an older closed epoch.

Exit codes: `0`; `1` on error.

#### 3.10.5 `zicato epoch set-goal`

```
zicato epoch set-goal --epoch <id> --goal <text> [--workspace <path>]
```

Sets (or overwrites) the `goal` field on an existing epoch and
re-ingests its index row. Designed for the contract-hash auto-roll
case: when `zicato evolve` opens a new epoch mid-run there is no
opportunity to prompt the operator, so the goal lands as an empty
string plus a warning recommending this command later.

Idempotent — writes the supplied goal into the epoch's `config.json`
and refreshes the `epochs.goal` index column; the rest of the index is
left alone (use `zicato reindex` for a full rebuild). Both `--epoch`
and `--goal` are required.

Exit codes: `0`; `2` for a usage error (missing required option).

### 3.11 `zicato evolve`

The orchestrator. One command, many rounds.

```
zicato evolve
    --harness-call-llm <dotted_path>     # required
    --auxiliary-call-llm <dotted_path>   # required
    [--workspace <path>]
    [--epoch <epoch_id>]
    [--rounds <N>]
    [--mode full|fast]
    [--max-consecutive-rejections <N>]
    [--max-wall-clock-seconds <S>]
    [--no-auto-epoch]
    [--epoch-name <name>]
    [--tournament-structure gauntlet|single_elim|double_elim|swiss|racing]   # shipped
    [--tournament-param KEY=VALUE]                                            # shipped, repeatable
    [--no-dashboard]
    [--dashboard-port <port>]
```

Runs the full meta-loop for `--rounds N` rounds. Each round, internally
(no standalone subcommands): runs every board entry against the current
parent, analyzes, proposes an experiment, applies its patches into a
candidate snapshot, runs the candidate, and runs the tournament to
decide. On promote the candidate becomes the new parent; on reject the
parent stays and the next round proposes again.

The two `call_llm` callables are passed **here** (not at `register`
time): `--harness-call-llm` and `--auxiliary-call-llm` are both
required and given as dotted import paths (e.g. `my_pkg.llms:harness`).

Flags:

| Flag | Default | Meaning |
|---|---|---|
| `--harness-call-llm <path>` | required | Dotted import path of the harness `call_llm`. |
| `--auxiliary-call-llm <path>` | required | Dotted import path of the auxiliary `call_llm`. |
| `--workspace <path>` | `.zicato` | Workspace root (the directory `zicato init` made). |
| `--epoch <epoch_id>` | current epoch | Run against a specific epoch. Passing this **skips auto-epoching entirely** — the explicit target wins. |
| `--rounds <N>` | `1` (x≥1) | How many evolve rounds to attempt. |
| `--mode full\|fast` | `fast` | `fast` (default) = child vs the champion's cached aggregate, re-scoring the champion only when no cache exists yet; `full` = re-run both parent and child every round. |
| `--max-consecutive-rejections <N>` | `3` (x≥1) | Stop early when this many rounds in a row are rejected. (This is the real early-stop knob — there is no `--stop-on-reject` / `--stop-on-no-improvement` / `--stop-on-degenerate`.) |
| `--max-wall-clock-seconds <S>` | unset (unbounded) | Total wall-clock budget, in seconds, for the **whole** `evolve` invocation. The loop stops cleanly between rounds once the budget is spent, and a single round that would overrun it is cancelled and recorded as an aborted round. Applies on top of each board entry's own `wall_clock_budget_seconds`. Env var: `ZICATO_MAX_WALL_CLOCK_SECONDS` (explicit flag wins). See [ROBUSTNESS.md](ROBUSTNESS.md) §2.1. |
| `--no-auto-epoch` | off (auto-epoch ON) | Disable contract-hash auto-epoching: `evolve` errors out (instead of rolling) when the evaluation contract has drifted from the current epoch. See [EPOCHS-AND-JOURNALING.md](EPOCHS-AND-JOURNALING.md) §10. |
| `--epoch-name <name>` | the `e{N}` scheme | Name for an auto-created epoch. Ignored when `--epoch` is passed or no new epoch is created. |
| `--tournament-structure <s>` *(shipped; advisory — trust `zicato evolve --help`)* | unset ⇒ `scoring.json` ⇒ `gauntlet` | The per-epoch tournament structure: `gauntlet` (default) / `single_elim` / `double_elim` / `swiss` / `racing`. **This is a contract-mutating convenience**: it writes `{structure, params}` into the live `scoring.json` *before* the contract hash is computed, so it is exactly equivalent to editing `scoring.json` by hand — changing it rolls the epoch via auto-epoching. For a runnable, no-live-LLM walkthrough see the presentation example's [`RUN.md` → "Running a non-gauntlet tournament"](../../examples/zicato_examples/target_1_presentation/RUN.md) and the [`TOURNAMENT-STRUCTURES.md` §4.0 quickstart](TOURNAMENT-STRUCTURES.md). See also [TOURNAMENT-DATA-MODEL.md](TOURNAMENT-DATA-MODEL.md) §5 and [EPOCHS-AND-JOURNALING.md](EPOCHS-AND-JOURNALING.md) §9. |
| `--tournament-param KEY=VALUE` *(shipped, repeatable; advisory)* | none | Sets one structure `params` key (e.g. `field_size=4`, `eta=2`, `board_fraction=0.4` for racing; `rounds_n=6` for swiss). Value is parsed as JSON if possible (so `field_size=4` is the integer `4`), else taken as a string. Writes into the same `tournament.params` block in `scoring.json`; contract-affecting, same caveat as above. Only applied when `--tournament-structure` is also passed. For racing, `board_ids` is **OPTIONAL** — it defaults to the epoch's full board, so `--tournament-structure racing` slices the board without listing any ids; pass `--tournament-param board_ids='["id1", "id2"]'` only to race on a subset (an explicit list overrides the default). |
| `--no-dashboard` | off | Do not spawn the dashboard service (and the watchdog supervisor that guards it). The loop still runs. See [RUNTIME.md](RUNTIME.md) §3 and [DASHBOARD.md](DASHBOARD.md) §2.1. |
| `--dashboard-port <port>` | `7892` (1–65535) | Port for the dashboard HTTP server, bound on `127.0.0.1`. |

There is **no** `--dashboard-bind` flag: the dashboard always binds
`127.0.0.1` under `evolve`. (Binding a non-loopback address is only
possible via the standalone `zicato dashboard --host`, §3.18.)

Exit codes: `0` whether rounds promoted or rejected (a rejection is a
normal outcome, not a failure); `1` on a run-level / configuration
failure (including the two-callable collusion guard); `2` for a usage
error. There is no dedicated degeneracy exit code today.

`evolve` is the right command to leave running overnight for a
calibration epoch. A wrapper script that watches its output and
notifies the operator on completion is a reasonable habit.

Auto-spawn behaviour: in the absence of `--no-dashboard`, `evolve`
spawns the supervisor as a subprocess and prints the dashboard URL to
stdout (bound on `127.0.0.1`, default port `7892`). The supervisor
exits when `evolve` exits. See [DASHBOARD.md](DASHBOARD.md) §2 for the
auto-spawn contract.

### 3.12 `zicato reindex`

Rebuild the `.zicato/index.db` analytical index from the
filesystem.

```
zicato reindex [--workspace <path>]
```

The index is a derived, fully-rebuildable SQLite projection of
the workspace's canonical files (see
[ANALYTICAL-INDEX.md](ANALYTICAL-INDEX.md)). It drops `index.db` and
re-derives every row from the canonical files under the workspace,
then prints a summary of how many epochs, generations, and runs were
indexed.

The shipped command takes **only** `--workspace`. There is no
`--epoch` (scope to one epoch) and no `--verify` (integrity check)
flag today — for targeted repairs, use the narrower commands below
(`reindex-generations`, `repair-*`).

In normal operation the orchestrator keeps the index current, so an
explicit `reindex` is rarely needed — it is the correctness backstop
(after a crash, after a hand-edit, after a version bump).

Exit codes: `0` on a clean rebuild; `1` on error.

#### 3.12.1 `zicato reindex-generations`

```
zicato reindex-generations [--workspace <path>]
```

Reconciles **only** the `generations` table against disk (parent +
promoted). Targeted repair for workspaces whose `generations` rows
were written by a buggy live dual-write (`parent_generation_id` NULL,
`promoted` clamped to 0 on every row except the seed). Walks
`lineage.json` and every `experiment.json` and rewrites only the
`parent_generation_id` and `promoted` flag of each row; the rest of
the index is left alone (use `reindex` for a full rebuild). Idempotent
and read-only against workspace files.

Exit codes: `0`; `1` on error.

#### 3.12.2 `zicato regenerate-report`

```
zicato regenerate-report [--workspace <path>] [--epoch <id>] [--no-llm]
```

Re-renders an epoch's `analysis.md` (and `analysis.html`) from the
current on-disk data — the supported replacement for the
earlier-drafted `zicato analysis show`. Off the happy path; `evolve`
regenerates the report after every round. Use this to repair an
existing epoch whose report was written by a buggy older orchestrator.

Idempotent and read-only against everything except `analysis.md` /
`analysis.html`. `--no-llm` skips the auxiliary-LLM prose pass and
substitutes placeholders; the deterministic figures + tables are
re-rendered regardless. `--epoch` defaults to the `current_epoch`
marker.

Exit codes: `0`; `1` on error.

#### 3.12.3 The `repair-*` family

A set of one-shot migration / backfill helpers for older workspaces.
All are idempotent; all default to `--workspace .zicato`.

| Command | What it backfills |
|---|---|
| `zicato repair-epoch-goals` | Walks every epoch on disk and adds an empty `goal` where missing (renders as "no goal recorded"); refreshes the `epochs.goal` index column. For a *real* goal value on one epoch, use `zicato epoch set-goal` (§3.10.5). |
| `zicato repair-judge-losses` | Re-derives `per_judge_loss` for every run from its `drift_counts` (or, for older runs, the events JSONL) and rewrites `loss.json`. Takes `--reingest / --no-reingest` (default `--reingest`) to re-ingest each rewritten run into `index.db` so the `judge_losses` table is populated without a full `reindex`. |
| `zicato repair-tournament-fk` | Backfills schema-v2 cross-cutting FKs on an existing index: `tournament_id` on `runs` / `loss_profiles` and `parent_epoch_id` on `epochs`. Read-only against workspace files — only the SQLite index is mutated. |
| `zicato repair-v0-baseline` | Walks every epoch (or the one named by `--epoch`) and writes a synthetic v0 `experiment.json` seed marker into any `generations/v0/` directory that lacks one. Workspaces created by a fresh `evolve` already carry it and are left alone. |

Exit codes (all four): `0`; `1` on error.

### 3.13 `zicato health`

Report whether the evolve loop has real optimization signal for the
current epoch.

```
zicato health [--workspace <path>] [--epoch <id>]
```

Loop-health diagnostics (see [LOOP-HEALTH.md](LOOP-HEALTH.md)) detect a
*running but meaningless* loop — flat scoring, dead board entries,
inert drift, a stalled proposer — and print them as findings. The
shipped command takes **only** `--workspace` and `--epoch`; there is no
`--round` (read a stored past report) and no `--format` flag today.

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

A CI / scheduled-run wrapper pairs `zicato health` with `zicato evolve`
so a degenerate epoch is caught the next morning without an operator
eyeballing the journal.

Exit codes: `0` when no critical finding is present; `1` (via
`SystemExit(1)`) when any critical finding is present. (This is a plain
non-zero exit, not the distinct `9` an earlier draft described — a
wrapper still branches on "the loop is degenerate", just on `1`.)

### 3.14 Reading the journal and analysis (no `journal` / `analysis` command)

There is **no** `zicato journal` or `zicato analysis` command group.
The artifacts are plain files on disk and are read directly:

- The running narrative is `epochs/{id}/journal.md` — `cat` it (or
  open it in the dashboard).
- The closeout report is `epochs/{id}/analysis.md`. To **re-render** it
  from the current on-disk data, use `zicato regenerate-report`
  (§3.12.2); to (re)run the decision-telemetry analyzer for an epoch,
  use `zicato analyze-telemetry` (§3.6).

(The earlier-drafted `zicato journal show` / `zicato analysis show`
verbs were never shipped.)

### 3.15 `zicato status` — *planned (v1.1+), not yet shipped*

> **Planned, not in the shipped CLI.** The shipped `zicato --help` tree
> exposes no `status` command. The design below is the roadmap target;
> until it lands, read `.zicato/runtime/` (or the dashboard) directly.

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

### 3.16 `zicato kill <run_id>` — *planned (v1.1+), not yet shipped*

> **Planned, not in the shipped CLI.** No `kill` command exists in the
> shipped `zicato --help` tree. The design below is the roadmap target.

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

### 3.17 `zicato dashboard`

Serve the dashboard for an existing workspace over HTTP, standalone
(not tied to an `evolve` invocation).

```
zicato dashboard [--workspace <path>] [--host <addr>] [--port <port>]
```

Point this at any workspace — a completed epoch for a post-mortem, or a
workspace some other `zicato evolve` is currently driving — and open
the printed URL in a browser. The server runs in the foreground until
interrupted (Ctrl-C). The auto-spawn case (the dashboard launched by
`zicato evolve`) does **not** require this command.

Flags:

| Flag | Default | Meaning |
|---|---|---|
| `--workspace <path>` | `.zicato` | Workspace root to serve. |
| `--host <addr>` | `127.0.0.1` | Host/bind address for the HTTP server. (This is the only place a non-loopback bind is reachable — `evolve` itself always binds `127.0.0.1`.) |
| `--port <port>` | `7892` (1–65535) | Port for the HTTP server. |

Exit codes: `0` on clean exit (Ctrl-C / SIGTERM); `1` on a
configuration error (e.g. workspace doesn't exist).

#### 3.17.1 Standalone `--read-only` / `--daemon` modes — *planned (v1.2+), not yet shipped*

> **Planned, not in the shipped CLI.** The shipped `dashboard` command
> exposes only `--workspace` / `--host` / `--port` (above). The
> `--read-only` and `--daemon` modes described here are roadmap
> targets; they are **not** currently flags on `zicato dashboard`.

| Mode | Use case |
|---|---|
| `--read-only` | Post-mortem inspection of a completed epoch. Reads only committed state in `.zicato/epochs/`; no `runtime/` interaction. No control surface (all POST endpoints disabled). |
| `--daemon` | Long-running CI scenarios where the dashboard should outlive a specific `evolve` invocation. Uses `.zicato/runtime/supervisor.pid` to ensure only one daemon at a time. Picks up successive `evolve` invocations automatically. |

> **The git-backed command family below (§3.18–§3.24) is planned
> (v0+1) and not in the shipped CLI.** It lands only after a workspace
> is migrated to the git storage backend (see [STORAGE.md](STORAGE.md)
> §3). None of `repo` / `log` / `diff` / `show` / `bisect` / `blame` /
> `workspace migrate-to-git` appear in the shipped `zicato --help` tree
> today; the designs below are the roadmap.

### 3.18 `zicato repo` — *planned (v0+1)*

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

### 3.19 `zicato log` — *planned (v0+1)*

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

### 3.20 `zicato diff <gen-a> <gen-b>` — *planned (v0+1)*

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

### 3.21 `zicato show <gen-id>` — *planned (v0+1)*

Show one generation: experiment metadata + patch diff.

```
zicato show <gen-id>
```

Output combines `git show <commit>` (the patch diff) with the
parsed `---zicato-meta---` block (hypothesis + outcome) so the
operator sees both the *intent* and the *change* in one view.

Format example in [STORAGE.md](STORAGE.md) §3.8.

Exit codes: `0`, `2`, `3`.

### 3.22 `zicato bisect` — *planned (v0+1)*

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

### 3.23 `zicato blame <file:line>` — *planned (v0+1)*

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

### 3.24 `zicato workspace migrate-to-git` — *planned (v0+1)*

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

The default for every command: human-readable text on stdout, errors
on stderr (click renders `ClickException` / `UsageError`). A
`--no-color` flag and a global text-vs-JSON toggle are planned
conventions but are **not** shipped today.

### 4.2 JSON format

There is no global `--format` flag. The only command that offers JSON
output today is `zicato mutations --format table|json` (§3.4), which
emits the full `MutationPoint` shape. Other commands render text only;
to consume structured data, read the canonical on-disk files (e.g.
`board.jsonl`, `experiment.json`, `loss.json`) or query the analytical
index directly.

## 5. Configuration file

`.zicato/config.json` is created by `zicato init` and merged into by
`zicato register`. `init` writes the minimal seed:

```json
{
  "instance_id": "default",
  "created_at": "2026-04-01T10:00:00Z"
}
```

`zicato register` (§3.2) merges in the adapter entrypoint, the mutable
trees, and the canonical **contract** source paths (board / brief /
scoring) under a `contract` key — preserving the keys `init` already
wrote. `zicato epoch set-goal` and the auto-epoching machinery persist
the per-epoch `goal`.

Note: the two `call_llm` callables are **not** stored in `config.json`
— they are passed at run time as `zicato evolve --harness-call-llm` /
`--auxiliary-call-llm` (§3.11). The current-epoch marker is tracked
separately (the workspace's `current_epoch` file / marker), updated by
`zicato epoch switch` / `zicato epoch new` and by auto-epoching.

The empty lineage DAG `lineage.json` (`{"nodes": [], "edges": []}`) is
also written by `init` and grows as epochs and generations land.

## 6. Scripting hints

A typical CI / scheduled-run wrapper. Because the shipped CLI uses only
exit codes `0` (success, *including* a tournament rejection), `1`
(failure / degenerate health), and `2` (usage), a wrapper that wants to
distinguish "no promotion this round" reads the printed output or the
recorded `outcome` rather than branching on a dedicated code:

```bash
#!/usr/bin/env bash
set -euo pipefail

# evolve exits 0 whether or not anything promoted; --max-consecutive-rejections
# bounds a fruitless run.
zicato evolve \
    --harness-call-llm  my_pkg.llms:harness \
    --auxiliary-call-llm my_pkg.llms:aux \
    --rounds 5 --max-consecutive-rejections 3
ec=$?

case "$ec" in
    0)  echo "evolve finished; inspect the journal"; cat .zicato/epochs/*/journal.md ;;
    *)  echo "evolve failed (exit $ec) — check the health report"; zicato health || true ;;
esac
```

(The fine-grained `6`/`7`/`9` exit codes an earlier draft relied on are
not emitted; see §2.)

## 7. Cross-references

| Topic | Document |
|---|---|
| Registration semantics, `mutation_points()` over multiple roots | [MUTATION-SURFACE.md](MUTATION-SURFACE.md) |
| Board entry schema accepted by `board add` | [BOARD-FORMAT.md](BOARD-FORMAT.md) |
| Authoring boards — the Python builder, outcome/process checks | [BOARD-AUTHORING.md](BOARD-AUTHORING.md) |
| Experiment shape produced by `propose`, applied inside `tournament` / `evolve` | [EPOCHS-AND-JOURNALING.md](EPOCHS-AND-JOURNALING.md) |
| The per-generation score shape produced by `tournament` | [SCORING.md](SCORING.md) |
| The two-callable collusion guard (enforced at runtime construction, not `register`) | [EMULATOR.md](EMULATOR.md) |
| State files under `runtime/` (read by the planned `zicato status`); supervisor binary auto-spawned by `evolve` | [RUNTIME.md](RUNTIME.md) |
| Dashboard panels, API, and the planned standalone modes for `zicato dashboard` | [DASHBOARD.md](DASHBOARD.md) |
| Why subprocess workers, what the planned `zicato kill` is hooked into | [ROBUSTNESS.md](ROBUSTNESS.md) |
| The git-backed roadmap behind `zicato repo` / `log` / `diff` / `show` / `bisect` / `blame` / `workspace migrate-to-git` | [STORAGE.md](STORAGE.md) |
| The analytical index `zicato reindex` rebuilds — schema, discipline | [ANALYTICAL-INDEX.md](ANALYTICAL-INDEX.md) |
| The loop-health diagnostics `zicato health` reports — detectors, severities | [LOOP-HEALTH.md](LOOP-HEALTH.md) |
| The tournament competition model behind `zicato tournament` | [TOURNAMENT.md](TOURNAMENT.md) |
| The tournament `full` vs `fast` trade-off (`evolve` defaults to `fast`; standalone `tournament` defaults to `full`) | [RATIONALE.md](RATIONALE.md) |
