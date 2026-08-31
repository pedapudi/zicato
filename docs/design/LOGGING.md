# Structured logging

zicato writes one structured, append-only JSONL stream per `zicato evolve`
invocation, under the workspace. Orchestrator and tournament-worker records
share that one file, and each record carries the epoch, generation, and run
it belongs to wherever that context is bound. A single query-layer reader
parses the stream, and both the CLI and the dashboard consume it through that
reader.

> **The one invariant.** Logs are an observability sink. Nothing in
> scoring, the promote gate, the journal, the analytical index, or any
> other decision path may read a log stream back. The scalar, the gate
> verdict, the journal, and the epoch record are computed only from the
> canonical run artifacts (`loss.json`, `events.jsonl`, `experiment.json`,
> …). A log record can be dropped, truncated, or replayed with zero
> behavioural consequence, which is the property that lets the stream be
> lossy-tolerant (see §2) and prunable (see §4). A value that matters to a
> decision lives in an artifact rather than in the log.

## 1. The stream

One file per `zicato evolve` (and per `zicato inspect reflection run`, the other
long-running command) invocation:

```
.zicato/logs/<utc-stamp>-<pid>.jsonl
```

* `<utc-stamp>` is `YYYYMMDDTHHMMSSZ` (UTC, second resolution), captured
  when the entrypoint installs the stream. Lexical order over stamps is
  chronological order.
* `<pid>` is the orchestrator process id. The stamp alone is not unique
  enough (two invocations can start in the same second); `stamp + pid`
  is. The pid also lets a reader tie a stream to a still-live process.

The directory `.zicato/logs/` hangs directly off the workspace root, a
sibling of `runtime/` and `epochs/`, and is created on demand. It is not
part of any contract hash and is never read by the epoch machinery.

### Record shape

One JSON object per line, newline-terminated:

```json
{"ts":"2026-07-12T08:40:01.123Z","level":"WARNING","component":"zicato.tournament.runner","epoch_id":"e3","generation_id":"g5","run_id":"g5--faq_smoke","message":"run g5--faq_smoke exceeded budget+grace (330s); requesting supervisor kill","fields":{"budget_s":300}}
```

| field           | required | source                                                            |
|-----------------|----------|-------------------------------------------------------------------|
| `ts`            | yes      | record creation time, ISO-8601 UTC, millisecond resolution        |
| `level`         | yes      | stdlib level name (`DEBUG`/`INFO`/`WARNING`/`ERROR`/`CRITICAL`)    |
| `component`     | yes      | the logger name (`record.name`) — e.g. `zicato.orchestrator`      |
| `message`       | yes      | the fully-formatted record message                                |
| `epoch_id`      | no       | bound run context (omitted when unbound)                          |
| `generation_id` | no       | bound run context                                                 |
| `run_id`        | no       | bound run context                                                 |
| `fields`        | no       | a structured `extra={"fields": {...}}` dict, if the call site set one |

**Additive-tolerant by construction.** A reader parses each line as JSON
and reads the keys it knows; an unknown key is ignored, an absent optional
key defaults to "unbound". A malformed line (a torn write, a truncated
tail) is skipped and never fatal, the same discipline `query/run_log.py`
applies to the goldfive event tail. New optional keys can be added to the
record without a version bump.

### Component and context

`component` is the logger name, so no call site needs an edit: a
`logging.getLogger("zicato.orchestrator").warning(...)` is captured
verbatim, with `component` set to `zicato.orchestrator`. The handler
structures whatever the stdlib hands it.

The optional `epoch_id` / `generation_id` / `run_id` come from a
process-local `contextvars` binding (`bind_log_context(...)`) rather than
from a per-call `extra=`, which would require editing every call site. A
`logging.Filter` copies the currently-bound context onto every record as it
is emitted. Two bind points know the full context and set it without
touching any log statement:

* the **worker** binds `(epoch_id, generation_id, run_id)` in `main()`
  from its args file — so every worker record is fully attributed;
* the **round loop** binds `epoch_id` for the duration of a round.

An unbound field is omitted from the record (never emitted as `null`).

## 2. How records reach the stream

### Orchestrator process

The evolve entrypoint (`evolve_n_rounds`) calls
`install_log_stream(workspace_root)` before it does any work and removes
the handler in its `finally`. Installation:

1. resolves the stream path (§1),
2. prunes old streams (§4),
3. attaches a `JsonlStreamHandler` to the **`zicato` logger** rather than
   the root logger, at a configurable floor level (default `INFO`).

Attaching to `zicato` rather than `root` means the stream captures every
`zicato.*` child logger by propagation while ignoring third-party chatter
(goldfive internals, `httpx`, `asyncio`) that would otherwise flood the
file. The floor level gates capture; the reader re-filters on read.

Because it is a stdlib `Handler`, every `log.*` call is captured without an
edit; the handler is the single structuring point.

### Worker processes (the subprocess boundary)

Every tournament run executes in its own `python -m
zicato._tournament_worker` subprocess, the subprocess worker boundary among
the robustness layers (ROBUSTNESS.md). A worker's records reach the same
stream by shared-file append: the worker appends structured records to the
same invocation stream file. The path crosses the process boundary in the
args file (`log_stream_path`), like the sibling sidecar paths the transport
already threads (`loss_path`, `sink_events_path`, `result_path`). The
worker's `main()` installs the same `JsonlStreamHandler` pointed at that
path in append (`"a"`) mode and binds its run context.

The parent does not pipe each worker's stderr and re-emit its lines as
records. The transport spawns workers with inherited stdio
(`create_subprocess_exec(..., env=worker_env)`, with no `stdout=` or
`stderr=` redirection), and the parent already runs a budget-bounded
`wait_for` per worker. A per-worker pipe-drain task would add concurrency
surface, and it would re-encode already-structured records as opaque text.

**Why shared-append is safe.** On **Linux**, a `write()` to a regular
file opened `O_APPEND` (which `open(path, "a")` sets) is serialized under
the inode lock: the kernel takes the file's `i_rwsem` for the whole
seek-to-end-plus-write, so concurrent appenders never interleave within a
single `write()`. The guarantee holds for writes of **arbitrary size**.
`PIPE_BUF` is a different guarantee and does not apply: it bounds atomicity
on pipes and FIFOs at 4096 bytes, and a large traceback record exceeds
that, so it would not justify torn-free large lines. Because each record is
emitted by a single `write()` (one `logging` handler `emit` produces one
line plus its terminator), the default handful of parallel workers
(`runtime.parallelism`, default 4) interleave safely at whole-line
granularity, and no line is torn. A probe confirms this: 6 processes
writing mixed records (some around 40 KB, well past `PIPE_BUF`) produced
**zero torn lines** on both ext4 and tmpfs.

The guarantee is Linux-specific rather than portable POSIX. The standard
does not mandate whole-`write()` atomicity for regular files, and NFS is
excluded, because its `O_APPEND` is emulated client-side and races. The
streams live on the local workspace filesystem, where the guarantee holds.
Shared-append also keeps **exactly one file per invocation**, which makes
retention a file count (§4) and the reader a single-file read (§3). A
per-worker sibling file would multiply files per invocation, blur the "keep
last N invocations" count, and force the reader to merge-sort N streams for
one logical view.

The worker also calls `logging.basicConfig(level=WARNING)`, which puts
records on stderr for a human watching a foreground run; the JSONL handler
is additional. When no `log_stream_path` is supplied (an ad-hoc or test
drive), the worker installs no file handler and writes to stderr only.

## 3. The read path — files-canonical

The files are canonical. Exactly one reader
(`zicato.query.log_stream`) parses them, and the CLI and the dashboard
both call it — there is no second parser.

`build_log_view(paths, *, limit, level, after, invocation)` mirrors the
shape of `build_run_log`:

* `invocation` selects the stream: `"latest"` (default), or a specific
  `<stamp>-<pid>` id. The view also returns the full `invocations` roster
  (newest first) so a caller can offer a picker.
* `level` filters to records at or above a threshold name.
* `limit` tails the last N matching records (the initial paint). The
  initial tail is a **bounded reverse block-read from EOF** — the reader
  block-reads backward until it has `limit` complete lines or hits a 4 MiB
  byte budget (`_TAIL_BYTE_BUDGET`), so it never reads the whole file. On a
  large stream (measured: 250 MB) this is the difference between a
  full-file `read_text` on every follow tick / SSE beat and a fixed few-KB
  read.
* `after` is a monotone **byte-offset cursor**: the reader `seek`s to it
  and reads FORWARD, returning only the records appended since — so a
  follower appends instead of re-rendering. The view returns the file's
  EOF byte offset so the caller passes it back as the next `after`.
  Append-only files make the byte offset a sound cursor (an offset never
  points into rewritten bytes), and unlike a line index it needs no scan
  from the top to resolve.

An absent `logs/` directory, an empty directory, and an unreadable file
all degrade to an empty view (`records: []`, `cursor: null`) — never an
error. `WorkspacePaths` grows one property, `logs`, for the directory.

## 4. Retention

Bounded, pruned at install time. Before a new stream is created,
`install_log_stream` deletes the oldest stream files so that **at most
`N = 20`** invocation logs remain (the new one becomes the 20th). Twenty
invocations is enough history to compare a bad run against the last few
good ones without letting the directory grow without bound; a long-lived
workspace that runs `evolve` daily keeps roughly three weeks of streams.
Pruning is best-effort — a delete failure is logged at debug and never
blocks the run. `N` is a module constant (`MAX_RETAINED_INVOCATIONS`),
documented here as the single source of truth.

Because a worker only ever *appends to the current* invocation's file
(never creates its own), retention counts whole invocations, and no prune
can ever race a live worker writing a sibling file (there are none).

## 5. Surfaces

### `zicato inspect logs`

```
zicato inspect logs [--workspace .zicato] [--invocation latest|<id>]
            [--level INFO] [--limit 200] [--follow]
```

Reads the query-layer reader and prints one formatted line per record
(`<ts> <LEVEL> <component> <context> <message>`). `--follow` poll-tails
the selected stream, advancing the line cursor. A workspace with no logs
prints nothing and exits 0; silence is the honest report of an empty
stream.

### Dashboard operator-log pane

A workspace-level `#/logs` view is a peer of the builder and settings
surfaces. Logs are per-invocation, so the view is not epoch-scoped. It
reads the `/api/logs` endpoint backed by the same reader, and renders the
tail as quiet mono rows, level-coloured via the `--v2-*` tone tokens, with
level-filter chips and an invocation picker, and no decorative chrome.

The refresh driven by server-sent events is **digest-gated**: the pane
folds its records into a content digest and repaints via `gatedSwap`, so a
no-op heartbeat (identical digest) rebuilds zero DOM. This is the
repository-wide digest-gated rendering rule. A workspace with no logs shows
an honest empty state.

## 6. Preflight capability warnings

`dialect_capability_warnings(weights)` (telemetry/dialects.py) is a pure
function of the contract's `ScoringWeights`. It flags, for example, drift
knobs that are inert under a drift-incapable telemetry dialect.

The warning describes a property of the contract rather than of a run, so
it is emitted **once per invocation** at contract load, beside the
epoch-open preflight machinery (the noise-aware, default-on `warn` contract
preflight), through `log.warning`. It therefore reaches both the structured
stream and the operator's console. The reducer does not emit it per board
unit: the warning carries no run-specific information, so one emission per
entry × replicate × generation would be duplication and would bloat every
worker's slice of the shared stream.
