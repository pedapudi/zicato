# CLI reference

> **Generated doc.** This file is generated to match `zicato --help` and
> should be regenerated whenever the CLI changes. The live `--help` output
> is the source of truth; if this document and the binary ever disagree,
> trust `zicato --help` / `zicato help <command>`.
>
> *Last reconciled against the live `--help` on 2026-07-29.* Verified the
> full command set (`init`, `evolve`, and the advanced/debugging group:
> `analyze-telemetry`, `board`, `builder`, `config`, `dashboard`, `epoch`,
> `health`, `help`, `logs`, `mutations`, `propose`, `reflect`, `regenerate-report`,
> `register`, `reindex`, `reindex-generations`, `repair-epoch-goals`,
> `repair-judge-losses`, `repair-tournament-fk`, `repair-v0-baseline`,
> `tournament`) and every option/default below by running
> `uv run zicato <command> --help`. No phantom commands exist (there is no
> `epochs` or `workspace` command — the group is `epoch`, singular), and
> every `repair-*` / `reindex-*` name is the full, un-truncated id.

`zicato` is a self-improving harness for multi-agent systems. It wraps an
inner multi-agent harness in an **evolve loop**: it proposes a small change,
runs a scored tournament between the parent and the child, and keeps the
winner — round after round.

## Setup

Install the project (and its dev tooling) with:

```
uv sync --all-extras
```

Always pass `--all-extras`. A bare `uv sync` removes the dev tooling
(pytest, mypy, ruff, and uv itself) from `.venv`.

Run the CLI through uv:

```
uv run zicato --help
uv run zicato help <command>      # equivalent to: zicato <command> --help
```

## Environment variables

There are none to set. **No environment variable is a configuration knob**:
every operator knob is a CLI flag (each flag's `--help` names the config knob
it shadows) or a workspace `config.json` block (`health`, `runtime`, `models`,
`harmonograf_url`). The former `ZICATO_*` operator variables are deleted and
ignored. What zicato still deliberately touches in the environment is a small
set of process-boundary contracts — the per-run harness scratch-dir contract,
the internal harmonograf handoff, the operator-*named* credential variables,
goldfive's own timeout, and CI/test toggles — enumerated with role labels by
[`zicato config env`](#zicato-config-env).

## How evolve orchestrates everything

`evolve` is **self-orchestrating**. On every invocation it:

1. Resolves the evaluation contract — the board, the proposer brief, the
   scoring config, and the registered inner-harness identity — and hashes it.
2. Compares that hash to the current epoch. If the contract has drifted, it
   closes the old epoch and opens a fresh one before running (contract-hash
   auto-epoching, on by default).
3. Proposes one change, runs the tournament between parent and child,
   promotes the winner or rejects the child, and repeats for `--rounds`.
4. Launches the live dashboard and prints its URL.

Because of this, on the happy path you never run `register`, `propose`,
`tournament`, `reindex`, or `epoch` by hand — `evolve` drives them all
internally. Those commands still exist as **advanced / debugging** tools for
inspecting a workspace or driving a single step manually.

## Happy path (init + evolve)

For most operators the whole tool is two commands:

```
zicato init                          # once: scaffold ./.zicato/
zicato evolve \
    --harness-call-llm   my_pkg.llms:harness \
    --auxiliary-call-llm my_pkg.llms:aux \
    --rounds 4                       # propose -> tournament -> promote, x4
```

### `zicato init`

Scaffold a fresh `.zicato/` workspace (run once per project). Creates the
workspace directory if it doesn't exist, writes an empty lineage DAG
(`lineage.json`: `{"nodes": [], "edges": []}`), and writes `config.json`
containing `{instance_id, created_at}`. Also scaffolds the operator's live
`scoring.json` (next to the workspace, only when absent) with the full
recommended contract — racing field 4, replicates 2, the evidence gate
enabled explicitly. Refuses to overwrite an existing
workspace unless `--force` is passed (`--force` only rewrites `config.json` /
`lineage.json`; it does not delete epoch artifacts living alongside).

```
zicato init [OPTIONS]
```

| Option | Default | Meaning |
|---|---|---|
| `--workspace DIRECTORY` | `.zicato` | Workspace directory to create. |
| `--instance-id TEXT` | `default` | Logical instance identifier recorded in `config.json`. |
| `--force` | off | Overwrite `config.json` / `lineage.json` if the workspace already exists. |

### `zicato evolve`

Run the self-improvement loop — the single happy-path entry point. Resolves
the evaluation contract, auto-opens an epoch when that contract has changed,
then proposes / runs the tournament / promotes for `--rounds` rounds. The
dashboard is launched automatically and its URL is printed.

```
zicato evolve [OPTIONS]
```

| Option | Default | Meaning |
|---|---|---|
| `--workspace PATH` | `.zicato` | Path to the zicato workspace root (the directory `zicato init` made). |
| `--epoch TEXT` | current epoch | Epoch id. Defaults to the workspace's current epoch. Pinning an epoch skips auto-epoching entirely. |
| `--rounds INTEGER RANGE` | `1` (x>=1) | Number of evolve rounds to attempt. |
| `--mode [full\|fast]` | `fast` | `fast` = cache-first: every (generation, entry, replicate) board unit is evaluated at most once and reused across all pairings / rounds / structures; only cache misses run. On the gauntlet the champion is a frozen cached aggregate, so replicates reduce CHALLENGER-side noise only — repeated rounds are not independent draws of the contrast. `full` = bypass the cache and force a fresh evaluation of every unit, both sides (noise re-sampling / debugging). |
| `--harness-call-llm TEXT` | **required** | Dotted import path of the harness `call_llm` (e.g. `mymodule:harness`). |
| `--auxiliary-call-llm TEXT` | **required** | Dotted import path of the auxiliary `call_llm` (e.g. `mymodule:aux`). |
| `--max-consecutive-rejections INTEGER RANGE` | `3` (x>=1) | Stop early when this many rounds in a row are rejected. |
| `--max-wall-clock-seconds INTEGER RANGE` | unset (unbounded) | Total wall-clock budget for the whole evolve invocation, in seconds. The loop stops cleanly between rounds once the budget is spent; a single round that would overrun it is cancelled and recorded as aborted. Applies on top of each board entry's own `wall_clock_budget_seconds`. |
| `--parallelism INTEGER RANGE` | unset ⇒ `config.json`'s `runtime.parallelism`, else `4` (x>=1) | Maximum number of board units the tournament runner keeps in flight at once. Shadows the `runtime.parallelism` config knob; the flag wins over the workspace `config.json`. |
| `--harness-call-timeout-ms INTEGER RANGE` | unset ⇒ `1800000` (x>=1) | Per-LLM-call wall-clock budget, in milliseconds, for the inner harness agent's calls. Shadows the `runtime.harness_call_timeout_ms` config knob. An explicit `GOLDFIVE_AGENT_CALL_TIMEOUT_MS` still wins — an operator who tunes goldfive directly is not overridden. |
| `--aux-call-timeout FLOAT RANGE` | unset ⇒ `120` (x>0) | Per-call wall-clock budget, in seconds, for every auxiliary-LLM (proposer / judge / emulator / analysis) call. Shadows the `aux.call_timeout_s` config knob. |
| `--supervisor-binary PATH` | unset ⇒ bundled / dev-checkout build, then system `PATH` | Filesystem path to the `zicato-supervisor` watchdog binary. Shadows the `integration.supervisor_binary` config knob. |
| `--harmonograf-url TEXT` | unset ⇒ auto-launch a per-workspace harmonograf | URL of an external harmonograf server to stream this invocation's telemetry to. Shadows the `integration.harmonograf_url` config knob (also settable via the workspace `config.json`'s `harmonograf_url`; the flag wins). |
| `--tournament-structure [gauntlet\|single_elim\|double_elim\|swiss\|racing]` | unset ⇒ reads `scoring.json` (`gauntlet` when absent) | Set the per-epoch tournament structure. **Contract-mutating convenience**: it writes `{structure, params}` into the live `scoring.json` before the contract hash is computed, so it participates in the hash and auto-rolls the epoch if it differs — exactly equivalent to editing `scoring.json` by hand. |
| `--tournament-param KEY=VALUE` | — | Set one tournament params key (repeatable). VALUE is parsed as JSON when possible, else taken as a string. Only applied when `--tournament-structure` is also passed. |
| `--no-auto-epoch` | off | Disable contract-hash auto-epoching. With this flag, evolve errors out (instead of rolling the epoch) when the contract has drifted from the current epoch. |
| `--epoch-name TEXT` | `e{N}` scheme | Name for an auto-created epoch. Ignored when `--epoch` is passed or no new epoch is created. |
| `--no-dashboard` | off | Do not spawn the dashboard service (and the watchdog supervisor that guards it). evolve still runs the loop. |
| `--dashboard-port INTEGER RANGE` | `7892` (1–65535) | Port for the dashboard HTTP server (bound on `127.0.0.1`). |

## Advanced / debugging commands

These are off the happy path. `evolve` already orchestrates register /
propose / tournament / reindex / epoch internally; reach for these only to
inspect a workspace or drive one step manually.

### `zicato analyze-telemetry`

Advanced: run the decision-telemetry analyzer for the current epoch. `evolve`
runs the analyzer per round; use this to (re)generate an insight for an epoch
out of band.

```
zicato analyze-telemetry [OPTIONS]
```

| Option | Default | Meaning |
|---|---|---|
| `--workspace PATH` | `.zicato` | Path to the zicato workspace directory. |
| `--epoch TEXT` | current epoch | Epoch id. Defaults to the workspace's `current_epoch` file contents. |
| `--round INTEGER` | — | Round number for the output filename. Omit to write `insights/latest.md` instead of `insights/round_{N:04d}.md`. |

### `zicato board`

Advanced: manage the per-epoch `board.jsonl` file. The board is part of the
evaluation contract, and `evolve` rolls the epoch when the live board
changes — use this group only to inspect or hand-edit a frozen board.

```
zicato board COMMAND [ARGS]...
```

Commands: `add`, `audit`, `judges`, `list`, `preflight`, `remove`.

#### `zicato board add`

Append one validated board entry from a JSON file to the current epoch.

```
zicato board add [OPTIONS] ENTRY_PATH
```

| Option | Default | Meaning |
|---|---|---|
| `--workspace TEXT` | `.zicato` | Path to the zicato workspace root. |

#### `zicato board audit`

Measure the evaluation's A/A noise floor and record it on the epoch.

Runs the current champion against ITSELF `--runs` times (fresh draws through
the same board-unit workers every duel uses) and reports the `delta_scalar`
spread — the smallest difference the board can actually resolve. The measured
floor is persisted onto the epoch record (`config.json`'s `noise_floor`
field) so `zicato evolve` can warn when `promote_margin` is below it while
the evidence gate is off.

```
zicato board audit [OPTIONS]
```

| Option | Default | Meaning |
|---|---|---|
| `--workspace TEXT` | `.zicato` | Path to the zicato workspace root. |
| `--epoch TEXT` | current epoch | Epoch to audit. |
| `--runs INTEGER RANGE` | `5` (>=2) | How many independent A/A draws of the champion to take. |
| `--harness-call-llm TEXT` | required | Dotted import path of the harness call_llm (e.g. `mymodule:harness`). |
| `--auxiliary-call-llm TEXT` | required | Dotted import path of the auxiliary call_llm (e.g. `mymodule:aux`). |

#### `zicato board judges`

List the board's declared process judges; optionally retest them.

Without `--test-retest`: print every judge the board declares (name, mode,
severity, criterion/dotted-path). With `--test-retest`: build each judge
through the same runtime bridge real runs use and judge ONE frozen
transcript `--retest-k` times; report the per-judge test-retest
disagreement rate. A judge that disagrees with itself on identical input
injects pure noise into every `custom:<judge_name>` drift count it
produces — the fix is a lower `per_judge_weights` entry or a sharper
criterion. Recommend-only; nothing is gated.

```
zicato board judges [OPTIONS]
```

| Option | Default | Meaning |
|---|---|---|
| `--workspace TEXT` | `.zicato` | Path to the zicato workspace root. |
| `--epoch TEXT` | current epoch | Epoch whose board to inspect. |
| `--test-retest` | off | Judge a frozen transcript k times per judge and report disagreement. |
| `--retest-k INTEGER RANGE` | `3` (>=2) | How many times each judge re-judges the same frozen transcript. |
| `--threshold FLOAT RANGE` | `0.25` (0..1) | Pairwise disagreement rate above which a judge is flagged noisy. |
| `--transcript FILE` | synthetic fixture | Frozen transcript file to re-judge (e.g. a settled reasoning trace saved from a prior run's events). |
| `--auxiliary-call-llm TEXT` | — | Dotted import path of the judge/aux call_llm (e.g. `mymodule:aux`). Required with `--test-retest` — inline judges are LLM-backed. |

#### `zicato board list`

List the entries in the current epoch's board.

```
zicato board list [OPTIONS]
```

| Option | Default | Meaning |
|---|---|---|
| `--workspace TEXT` | `.zicato` | Path to the zicato workspace root. |

#### `zicato board preflight`

Measure the contract's noise floor AND achievable signal; verdict.

Board-reflection v1. Two measurements: (a) the A/A noise floor — the
champion duels ITSELF `--runs` times (same draws `zicato board audit`
takes); (b) the scripted-perturbation duels — the champion vs
deliberately-degraded ephemeral copies of itself (a deterministic,
role-diverse sample of mutation points blanked/scrambled in scratch trees;
the real lineage is never touched), reporting the MAX signal so one inert
point cannot veto a healthy contract. Verdicts: REFUSE-recommended when the
achievable signal is at/below the floor; WARN when every probe scored
identically (a saturated contract — the 1.000000 signature); INERT when the
probes moved nothing while the A/A draws varied (the signal is unmeasured,
not zero — pick a representative point); OK otherwise. Also asserts the
promote_margin window `noise < margin < achievable` and names the side that
failed. Recommend-only — never gates. The verdict persists onto the epoch
record and flows into the per-round health report.

```
zicato board preflight [OPTIONS]
```

| Option | Default | Meaning |
|---|---|---|
| `--workspace TEXT` | `.zicato` | Path to the zicato workspace root. |
| `--epoch TEXT` | current epoch | Epoch to pre-flight. |
| `--runs INTEGER RANGE` | `5` (>=2) | How many independent A/A draws of the champion to take. |
| `--degrade-mutation-id TEXT` | automatic sample | Degrade exactly this mutation point instead of the automatic role-diverse sample (use when you know which point carries the contract's signal). |
| `--probe-points INTEGER RANGE` | `runtime.preflight_probe_points` (>=1) | Ceiling on how many mutation points the automatic sample degrades. Probing stops early once the verdict is settled, so this rarely costs the full count. |
| `--harness-call-llm TEXT` | required | Dotted import path of the harness call_llm (e.g. `mymodule:harness`). |
| `--auxiliary-call-llm TEXT` | required | Dotted import path of the auxiliary call_llm (e.g. `mymodule:aux`). |

#### `zicato board remove`

Remove an entry by id from the current epoch's board.

```
zicato board remove [OPTIONS] ENTRY_ID
```

| Option | Default | Meaning |
|---|---|---|
| `--workspace TEXT` | `.zicato` | Path to the zicato workspace root. |

### `zicato builder`

Launch the dashboard focused on the tournament builder. Boots the same
dashboard service `zicato dashboard` runs, against the given workspace, and
prints the builder deep-link (`http://127.0.0.1:<port>/#/builder`) so the
browser opens on the builder rather than the environment overview. The server
runs in the foreground until interrupted (Ctrl-C). The bind address is fixed
at the loopback `127.0.0.1` — the dashboard is a local inspection surface,
never exposed on a routable interface (the same rule `zicato dashboard` /
`zicato evolve` honour). The builder is also reachable inside any running
dashboard via the top-bar ⚙ Settings entry.

```
zicato builder [OPTIONS]
```

| Option | Default | Meaning |
|---|---|---|
| `--workspace PATH` | `.zicato` | Path to the zicato workspace root to serve. |
| `--dashboard-port INTEGER RANGE` | `7892` (1–65535) | Port for the dashboard HTTP server (bound on `127.0.0.1`). |
| `--static-dir DIRECTORY` | unset ⇒ the bundled `zicato/dashboard/static` | Filesystem path to the dashboard static-asset directory. Shadows the `dashboard.static_dir` config knob. |

### `zicato config`

Introspect zicato's configuration surface. Operator knobs are CLI flags and
workspace `config.json` blocks — not environment variables. The subcommands
here make that surface discoverable without grepping the tree.

```
zicato config [OPTIONS] COMMAND [ARGS]...
```

#### `zicato config env`

List the environment variables zicato deliberately touches. Since the env-var
rationalization, **no environment variable is a configuration knob**. What
remains — and is printed here, grouped by role — is the small merited set of
process-boundary contracts:

* **harness-contract** — `ZICATO_RUN_SCRATCH_DIR`: set *by* the tournament
  worker *for* the inner harness; the per-run scratch directory run output
  must land in.
* **internal-handoff** — `ZICATO_HARMONOGRAF_URL` / `ZICATO_HARMONOGRAF_GRPC`:
  set (and restored) by the evolve loop's harmonograf auto-launch so
  downstream re-resolvers — including worker subprocesses — discover the
  launched console. Not operator knobs; `--harmonograf-url` and the
  `config.json` `harmonograf_url` key outrank them.
* **secrets-boundary** — the operator-*named* `api_key_env` variables from the
  `config.json` `models` block, plus the `runtime.worker_env_passthrough`
  allowlist: credentials stay in the environment, never in files.
* **external-integration** — `GOLDFIVE_AGENT_CALL_TIMEOUT_MS`: goldfive's own
  knob; when set, zicato defers to it instead of `--harness-call-timeout-ms`.
* **test-toggle** — `ZICATO_SKIP_HOOK_CHECK`, `ZICATO_PARITY_UPDATE`: CI/test
  switches, never read on an operator path.

```
zicato config env [--json]
```

| Option | Default | Meaning |
|---|---|---|
| `--json` | off | Emit the set as a JSON array instead of grouped text. |

### `zicato dashboard`

Serve the dashboard for an existing workspace over HTTP. Point it at any
workspace — a completed epoch for a post-mortem, or a workspace some other
`zicato evolve` is currently driving — and open the printed URL in a browser.
The server runs in the foreground until interrupted (Ctrl-C). `evolve`
auto-spawns the dashboard, so you only need this command for a standalone
view.

```
zicato dashboard [OPTIONS]
```

| Option | Default | Meaning |
|---|---|---|
| `--workspace PATH` | `.zicato` | Path to the zicato workspace root to serve. |
| `--host TEXT` | `127.0.0.1` | Host/bind address for the dashboard HTTP server. |
| `--port INTEGER RANGE` | `7892` (1–65535) | Port for the dashboard HTTP server. |
| `--static-dir DIRECTORY` | unset ⇒ the bundled `zicato/dashboard/static` | Filesystem path to the dashboard static-asset directory. Shadows the `dashboard.static_dir` config knob. |

### `zicato epoch`

Advanced: manage zicato epochs — the unit of evaluation contract. `evolve`
opens, closes, and rolls epochs on its own whenever the evaluation contract
changes. Use this group only to inspect epochs (`epoch list`) or to force an
epoch boundary by hand.

```
zicato epoch COMMAND [ARGS]...
```

Commands: `close`, `gc`, `list`, `new`, `set-goal`, `switch`.

#### `zicato epoch new`

Create a new epoch and make it current. If a previous epoch is still open it
is auto-closed first. The supplied contract files are both frozen into the
epoch directory AND published as the workspace's live contract (recorded in
`config.json` under `contract`).

```
zicato epoch new [OPTIONS] NAME
```

| Option | Default | Meaning |
|---|---|---|
| `--workspace TEXT` | `.zicato` | Path to the zicato workspace directory. |
| `--board FILE` | **required** | Path to a `board.jsonl`. Frozen into the epoch and adopted as the workspace's live contract board. |
| `--brief, --rubric FILE` | **required** | Path to a proposer brief (`brief.md`). Frozen into the epoch and adopted as the workspace's live contract brief. `--rubric` is accepted as a legacy alias. |
| `--scoring FILE` | defaults applied if absent | Path to `scoring.json`. When given, frozen into the epoch and adopted as the live contract scoring. |
| `--goal TEXT` | prompted (TTY) / empty (non-TTY) | Free-form statement of why this epoch exists. Persisted into `config.json` and surfaced in the analyzer report header. |

#### `zicato epoch close`

Close an epoch and (best-effort) generate `analysis.md`. When `EPOCH_ID` is
omitted, the current epoch is closed. The analysis pass runs only if an
auxiliary LLM has been configured; until then it writes a stub `analysis.md`.

```
zicato epoch close [OPTIONS] [EPOCH_ID]
```

| Option | Default | Meaning |
|---|---|---|
| `--workspace TEXT` | `.zicato` | Path to the zicato workspace directory. |

#### `zicato epoch gc`

Prune generation SOURCE TREES under an epoch; records survive. Reclaims the
disk held by settled-rejected generations' source trees (directory-backend
snapshot dirs; git-backend tags + worktrees, whose commits then become
collectable). Never touches `lineage.json`, the journal, experiment/score
records, or run telemetry. Promoted generations, in-flight generations, and
the seed `v0` are never pruned. Dry-run by default; pass `--apply` to
execute. When `EPOCH_ID` is omitted, the current epoch is targeted.

```
zicato epoch gc [OPTIONS] [EPOCH_ID]
```

| Option | Default | Meaning |
|---|---|---|
| `--workspace TEXT` | `.zicato` | Path to the zicato workspace directory. |
| `--keep-last INTEGER` | unset | Keep the N newest generations in addition to the always-kept set (promoted chain, in-flight generations, v0); prune older settled-rejected trees. Exactly one of `--keep-last` / `--keep-promoted-only` is required. |
| `--keep-promoted-only` | off | Keep only the always-kept set; prune every settled-rejected generation's source tree. |
| `--apply` | off | Actually prune. Without this flag the command is a DRY RUN that prints the plan and removes nothing. |

#### `zicato epoch list`

List every epoch in the workspace as a markdown table.

```
zicato epoch list [OPTIONS]
```

| Option | Default | Meaning |
|---|---|---|
| `--workspace TEXT` | `.zicato` | Path to the zicato workspace directory. |

#### `zicato epoch switch`

Point the workspace's `current_epoch` marker at `EPOCH_ID`.

```
zicato epoch switch [OPTIONS] EPOCH_ID
```

| Option | Default | Meaning |
|---|---|---|
| `--workspace TEXT` | `.zicato` | Path to the zicato workspace directory. |

#### `zicato epoch set-goal`

Set the goal on an existing epoch and re-ingest its index row. Designed for
the contract-hash auto-roll case, where `evolve` opens a new epoch mid-run
with no opportunity to prompt the operator. Idempotent — writes the supplied
goal into `config.json` and refreshes the `epochs.goal` index column (use
`zicato reindex` for a full rebuild).

```
zicato epoch set-goal [OPTIONS]
```

| Option | Default | Meaning |
|---|---|---|
| `--epoch TEXT` | **required** | The epoch id to mutate. |
| `--goal TEXT` | **required** | The free-form goal text to write into the epoch's `config.json`. |
| `--workspace TEXT` | `.zicato` | Path to the zicato workspace directory. |

### `zicato health`

Report on the evolve loop's optimization signal for the current epoch.
Detects toothless evaluations — flat scoring, dead board entries, inert
drift, a stalled proposer — and prints them as findings. Exits non-zero when
any critical finding is present.

```
zicato health [OPTIONS]
```

| Option | Default | Meaning |
|---|---|---|
| `--workspace PATH` | `.zicato` | Path to the zicato workspace directory. |
| `--epoch TEXT` | current epoch | Epoch id. Defaults to the workspace's `current_epoch` file contents. |

### `zicato help`

Show help for zicato, or for one `COMMAND`. `zicato help` is an explicit alias
for `zicato --help`; `zicato help evolve` is equivalent to
`zicato evolve --help`.

```
zicato help [COMMAND]
```

### `zicato logs`

Advanced: tail the structured operator-log stream for one invocation. Every
`evolve` / `reflect run` invocation writes one JSONL stream under
`.zicato/logs/<stamp>-<pid>.jsonl` (see `docs/design/LOGGING.md`); this reads
it back through the same query-layer reader the dashboard log pane uses — the
files are canonical. A workspace with no logs prints nothing and exits 0.

```
zicato logs [OPTIONS]
```

| Option | Default | Meaning |
|---|---|---|
| `--workspace PATH` | `.zicato` | Path to the zicato workspace root. |
| `--invocation TEXT` | `latest` | Which stream to read: `latest` (newest) or a specific `<stamp>-<pid>` id (list them with `--list`). |
| `--level [DEBUG\|INFO\|WARNING\|ERROR\|CRITICAL]` | — | Show only records at or above this level. Unset shows everything captured. |
| `--limit INTEGER` | `200` | Tail at most this many records. |
| `-f, --follow` | off | Poll-tail the stream, printing new records as they land (Ctrl-C to stop). |
| `--list` | off | List the available invocation streams (newest first) and exit. |

### `zicato mutations`

Advanced: list the mutable spans in the registered inner harness. `evolve`
enumerates these itself; use this to audit what the proposer is allowed to
change.

```
zicato mutations [OPTIONS]
```

| Option | Default | Meaning |
|---|---|---|
| `--workspace PATH` | `.zicato` | Path to the zicato workspace directory. |
| `--id TEXT` | — | Filter mutation points by id glob (e.g. `researcher_*`). |
| `--kind [span\|file\|code]` | — | Restrict the listing to one mutation kind. |
| `--show [preview\|full]` | `preview` | Truncate content previews (`preview`) or dump full content (`full`). |
| `--format [table\|json]` | `table` | Output format. |

### `zicato propose`

Advanced: generate one `Experiment` for the next generation. `evolve`
proposes on every round; run this by hand only to produce and inspect a
single experiment without running the tournament.

```
zicato propose [OPTIONS]
```

| Option | Default | Meaning |
|---|---|---|
| `--workspace PATH` | `.zicato` | Path to the zicato workspace directory. |
| `--epoch TEXT` | current epoch | Epoch id. Defaults to the workspace's `current_epoch` file contents. |
| `--patterns-from PATH` | — | Path to a Patterns JSON file. If absent, detectors are run fresh. |
| `--max-retries INTEGER RANGE` | `2` (0–10) | How many times to ask the proposer to fix a malformed response. |

### `zicato regenerate-report`

Advanced: re-render an epoch's `analysis.md` from the current on-disk files.
`evolve` regenerates the report after every round; use this to repair an
existing epoch whose report was written by a buggy older orchestrator. The
command is idempotent and read-only against everything except `analysis.md` /
`analysis.html`.

```
zicato regenerate-report [OPTIONS]
```

| Option | Default | Meaning |
|---|---|---|
| `--workspace PATH` | `.zicato` | Path to the zicato workspace root (either the project dir or the `.zicato/` dir). |
| `--epoch TEXT` | current epoch | Epoch id. Defaults to the workspace's `current_epoch` marker. |
| `--no-llm` | off | Skip the auxiliary-LLM prose pass and substitute placeholders. The deterministic data sections (figures, tables, scores) are still re-rendered. |

### `zicato register`

Advanced: record the adapter entrypoint, mutable trees, and contract paths.
`evolve` resolves the contract itself; run `register` by hand only to pin the
contract source paths up front, or to point the workspace at a different agent
/ brief. Merges into the existing `config.json` rather than replacing it, so
the keys `zicato init` wrote (`instance_id`, `created_at`) are preserved. The
canonical contract source paths default to the conventional location
alongside the workspace, are stored under the `contract` key, and are read
back by contract-hash auto-epoching on every `evolve`.

`--proposer-path` is optional and stored under the same `contract` key as
`contract.proposer_path` (absolutised). It is itself a contract input —
configuring a proposer dir, or editing one of its skills, rolls the epoch on
the next `evolve`. Omitting the flag leaves the key unset, which resolves to
the built-in default proposer. See [PROPOSER.md](PROPOSER.md).

```
zicato register [OPTIONS]
```

| Option | Default | Meaning |
|---|---|---|
| `--workspace DIRECTORY` | `.zicato` | Workspace directory to update. |
| `--adk TEXT` | **required** | Adapter entrypoint in `module.path:agent_symbol` form. Either inside a `--mutable-tree` (its TOP-LEVEL module is the tree's basename) or outside every tree, which is the dependency shape: the harness imports the mutable trees, and each tree is verified to have loaded from the generation snapshot per run instead. |
| `--mutable-tree PATH` | — (repeatable) | Source root the proposer is allowed to mutate. Its BASENAME must be the importable package name — the snapshot exposes each tree under its basename on `sys.path`. |
| `--board PATH` | `<workspace_parent>/board.jsonl` | Canonical `board.jsonl` path. |
| `--brief PATH` | `<workspace_parent>/brief.md` | Canonical proposer-brief path. |
| `--scoring PATH` | `<workspace_parent>/scoring.json` | Canonical `scoring.json` path. |
| `--proposer-path PATH` | — (builtin default proposer) | Proposer dir (`proposers/<name>/` — skills + optional `agent.py`). A contract input: configuring it (or editing a skill) rolls the epoch. |

### `zicato reflect`

Advanced: board reflection — Measurement System Analysis for the evaluation
contract itself. Runs the four-pillar analysis over an observation corpus and
emits ranked, evidence-linked findings, each carrying a proposed contract edit.
Diagnose-and-recommend only: running it never rolls the epoch (only sealing a
recommendation through the builder does). See
[BOARD-REFLECTION.md](BOARD-REFLECTION.md). Five subcommands:

```
zicato reflect run [OPTIONS]
zicato reflect practices [OPTIONS]
zicato reflect suggest [OPTIONS]
zicato reflect report REFLECTION_ID [OPTIONS]
zicato reflect apply REFLECTION_ID ITEM_ID [OPTIONS]
```

`reflect run` builds the observation corpus by REFERENCING the lineage's
already-persisted run artifacts (loss / result / judge_io) with zero LLM
budget; the only LLM spend is the independent meta-judge **adjudication**,
gated behind `--adjudicator-call-llm`. The default (adjudication requested)
REFUSES without an adjudicator callable — the live-run gate never silently
spends budget. `--pre-register` writes `plan.json` and stops; `--passive` and
`--no-llm-adjudication` run the cheap zero-LLM tier (reliability +
discrimination + coverage only).

| `reflect run` option | Default | Meaning |
|---|---|---|
| `--workspace TEXT` | `.zicato` | Workspace root. |
| `--epoch TEXT` | current epoch | Contract to validate. |
| `--candidate TEXT` | champion + lineage | Generation id in the candidate spread (repeatable). |
| `--entries TEXT` | whole board | Board entry id to cover (repeatable). |
| `--replicates INTEGER RANGE` | `3` (x>=1) | Replicate count recorded in the plan. |
| `--adjudicator-call-llm TEXT` | unset | Dotted import path of the independent meta-judge `call_llm` (ACTIVE mode; must differ from every judge model). |
| `--checks TEXT` | all | Comma-separated check subset. |
| `--no-llm-adjudication` | off | Cheap tier: reliability + discrimination + coverage only, zero LLM. |
| `--passive` | off | Ingest-only: reference existing lineage artifacts, zero LLM. |
| `--pre-register` | off | Write `plan.json` and STOP (review before spending). |
| `--k-adj INTEGER RANGE` | `1` (x>=1) | Adjudicator replication (self-agreement). |
| `--max-wall-clock-seconds INTEGER` | unset | Budget ceiling (recorded intent). |
| `--output TEXT` | stdout | Report destination. |

`reflect practices` runs the **practice review** — the narrative layer above the
four pillars — on the contract + operating history alone, WITHOUT a reflection
corpus (the instant, always-free tier). The checks that need a corpus or
scorecards report `unmeasured` honestly; `--json` emits the raw review. Accepts
`--workspace` and `--epoch`. (A full `reflect run` also persists the review as
`practices.json` and renders it in the report — there it can measure the
corpus-dependent checks too.)

`reflect suggest` is **generative reflection** (the instrument's second loop;
[EVAL-SYNTHESIS.md](EVAL-SYNTHESIS.md)): it mines **episodes** from the
candidate loop's observed behaviour (endpoint-free), synthesises **suggestions**
(a drafted board entry or judge, each carrying provenance), optionally
attaches **admission** statistics (flip rate, discrimination), and persists them
beside `findings.json` as `suggestions.json` — rendered by `reflect report` and
staged by `reflect apply`. The live admission probes SPEND real champion budget
and are **endpoint-gated**: they run ONLY under `--probe` (default OFF — plan
mode shows what they would spend, spending nothing). `--allow-llm` permits the
aux-metered LLM synthesis tier (judge / rubric drafting; default: mechanical
only). Recommend-only end to end. `--from-trajectories <dir>` **bootstraps** the
instrument from a directory of foreign agent trace files
([TRAJECTORY-BOOTSTRAP.md](TRAJECTORY-BOOTSTRAP.md)): the traces are imported
(format-sniffed + reduced through the existing dialect reducers), persisted under
the minted reflection dir, and mined ALONGSIDE the workspace episodes into one
ranked list. It is goldfive-optional — a trace dir with zero goldfive artifacts
still yields suggestions; an empty / missing dir prints an honest message and
exits 0.

| `reflect suggest` option | Default | Meaning |
|---|---|---|
| `--workspace TEXT` | `.zicato` | Workspace root. |
| `--epoch TEXT` | current epoch | Contract to mine. |
| `--reflection TEXT` | fresh id | Attach suggestions to this reflection id. |
| `--probe` | off | SPEND champion budget on the live admission probes (endpoint-gated). |
| `--allow-llm` | off | Permit LLM synthesis (judge/rubric drafting; aux-metered). |
| `--from-trajectories PATH` | unset | Bootstrap suggestions from a directory of foreign agent trace files (`*.jsonl`); goldfive-optional. |
| `--json` | off | Emit the raw suggestion dicts. |

`reflect report REFLECTION_ID` renders a stored reflection's report (Markdown,
or `--json` for the raw dict) — including the **eval suggestions** section when
`reflect suggest` has run. `reflect apply REFLECTION_ID ITEM_ID` forks a
**builder draft** from the live contract and stages the op named by a finding
(`find-…`) OR an eval suggestion (`sug-…`) onto it — an entry suggestion through
`add_board_entry`, a judge suggestion through `add_judge`. It never writes the
sealed contract; the operator reviews and seals through the builder, which is
the gated step that rolls the epoch. Both accept `--workspace` and `--epoch`.

### `zicato reindex`

Advanced: rebuild the SQLite analytical index from workspace files. `evolve`
keeps the index current. Drops `index.db` and re-derives every row from the
canonical files under the workspace, then prints a summary of how many epochs,
generations, and runs were indexed.

```
zicato reindex [OPTIONS]
```

| Option | Default | Meaning |
|---|---|---|
| `--workspace TEXT` | `.zicato` | Path to the zicato workspace directory. |

### `zicato reindex-generations`

Advanced: reconcile only the `generations` table from disk. Targeted repair
for workspaces whose `generations` rows were written by a buggy live
dual-write. Walks `lineage.json` + every `experiment.json` and rewrites only
the `parent_generation_id` and `promoted` flag of each `generations` row; the
rest of the index is left alone (use `zicato reindex` for a full rebuild).
Idempotent and read-only against workspace files.

```
zicato reindex-generations [OPTIONS]
```

| Option | Default | Meaning |
|---|---|---|
| `--workspace TEXT` | `.zicato` | Path to the zicato workspace directory. |

### `zicato repair-epoch-goals`

Walk every epoch on disk and add an empty `goal` where missing. Targeted
migration helper for workspaces whose per-epoch `config.json` files were
written before the `goal` field landed. Defaults missing goals to the empty
string and refreshes the `epochs.goal` index column to match. Idempotent and
read-only against epochs that already have a goal value. For a real goal value
on an individual epoch, see `zicato epoch set-goal`.

```
zicato repair-epoch-goals [OPTIONS]
```

| Option | Default | Meaning |
|---|---|---|
| `--workspace TEXT` | `.zicato` | Path to the zicato workspace directory. |

### `zicato repair-judge-losses`

Advanced: backfill `per_judge_loss` into existing runs. Walks every run on
disk, re-derives the per-judge weighted-loss attribution from `drift_counts`
(or, for older runs, from the events JSONL), and rewrites `loss.json` with the
populated `per_judge_loss` field. Idempotent.

```
zicato repair-judge-losses [OPTIONS]
```

| Option | Default | Meaning |
|---|---|---|
| `--workspace TEXT` | `.zicato` | Path to the zicato workspace directory. |
| `--reingest / --no-reingest` | `reingest` | Re-ingest each rewritten run into `index.db` so the `judge_losses` table is populated without a full `zicato reindex`. |

### `zicato repair-tournament-fk`

Advanced: backfill schema-v2 cross-cutting FKs on an existing index. Schema v2
added a `tournament_id` column to `runs` and `loss_profiles` plus a
`parent_epoch_id` column on `epochs`. New writes populate them automatically;
this command repairs v1-era rows by walking every epoch in `lineage.json` and
every `experiment.json` on disk. Idempotent and read-only against workspace
files — only the SQLite index is mutated.

```
zicato repair-tournament-fk [OPTIONS]
```

| Option | Default | Meaning |
|---|---|---|
| `--workspace TEXT` | `.zicato` | Path to the zicato workspace directory. |

### `zicato repair-v0-baseline`

Advanced: backfill the synthetic v0 `experiment.json` marker. Walks every
epoch under the workspace (or the one named by `--epoch`) and, for each that
has a `generations/v0/` directory without an `experiment.json`, writes a
synthetic seed marker. Idempotent — fresh `evolve` workspaces already carry it
and are left alone.

```
zicato repair-v0-baseline [OPTIONS]
```

| Option | Default | Meaning |
|---|---|---|
| `--workspace TEXT` | `.zicato` | Path to the zicato workspace directory. |
| `--epoch TEXT` | every epoch | Restrict the backfill to a single epoch id. |

### `zicato tournament`

Advanced: run a tournament between `PARENT` and `CHILD` generations in
isolation. `evolve` runs the tournament every round; use this only to re-score
a specific generation pair.

```
zicato tournament [OPTIONS] PARENT CHILD
```

| Option | Default | Meaning |
|---|---|---|
| `--workspace TEXT` | `.zicato` | Path to the zicato workspace root. |
| `--epoch TEXT` | current epoch | Epoch id. Defaults to the workspace's current epoch. |
| `--mode [full\|fast]` | `full` | `full` = run both generations; `fast` = child vs parent's historical aggregate. |
| `--skip-regression` | off | Skip the regression-suite gate even when enabled in scoring. |
