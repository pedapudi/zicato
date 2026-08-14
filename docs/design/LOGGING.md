# Structured logging

Until now zicato had exactly one line of logging *configuration* in the
whole tree — `logging.basicConfig(level=logging.WARNING)` inside the
tournament worker's `main()`. The orchestrator itself installed nothing:
every `log.warning(...)` / `log.info(...)` a `zicato.*` logger emitted was
handled by the stdlib's last-resort handler and written to whatever
terminal happened to launch `zicato evolve` — then lost the moment the
terminal scrolled or the run was launched detached. Worker records went to
the same place through the inherited stderr, interleaved and unattributable.

This document defines a real **log surface**: one structured, append-only
JSONL stream per evolve invocation, written under the workspace, read back
through a single query-layer reader that the CLI *and* the dashboard both
consume. It is observability, never an input.

> **The one invariant.** Logs are an OBSERVABILITY sink. Nothing in
> scoring, the promote gate, the journal, the analytical index, or any
> other decision path may read a log stream back. The scalar, the gate
> verdict, the journal, and the epoch record are computed only from the
> canonical run artifacts (`loss.json`, `events.jsonl`, `experiment.json`,
> …). A log record can be dropped, truncated, or replayed with zero
> behavioural consequence — that is the property that lets the stream be
> lossy-tolerant (see §2) and prunable (see §4). If a value matters to a
> decision it lives in an artifact, not in the log.

## 1. The stream

One file per `zicato evolve` (and per `zicato inspect reflection run`, the other
long-running command) invocation:

```
.zicato/logs/<utc-stamp>-<pid>.jsonl
```

* `<utc-stamp>` is `YYYYMMDDTHHMMSSZ` (UTC, second resolution), captured
  when the entrypoint installs the stream. It sorts lexically = chronologically.
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
tail) is skipped, never fatal — exactly the discipline `query/run_log.py`
already applies to the goldfive event tail. New optional keys can be added
to the record over time without a version bump.

### Component and context

`component` is simply the logger name, so **zero call sites change**: an
existing `logging.getLogger("zicato.orchestrator").warning(...)` is
captured verbatim, its `component` being `zicato.orchestrator`. The
handler structures whatever the stdlib hands it.

The optional `epoch_id` / `generation_id` / `run_id` come from a
process-local `contextvars` binding (`bind_log_context(...)`), NOT from
per-call `extra=` (which would be a call-site change). A `logging.Filter`
copies the currently-bound context onto every record as it is emitted.
Two natural bind points already know the full context and set it without
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
3. attaches a `JsonlStreamHandler` to the **`zicato` logger** (not the
   root logger) at a configurable floor level (default `INFO`).

Attaching to `zicato` rather than `root` means the stream captures every
`zicato.*` child logger by propagation while ignoring third-party chatter
(goldfive internals, `httpx`, `asyncio`) that would otherwise flood the
file. The floor level gates capture; the reader re-filters on read.

Because it is a stdlib `Handler`, **every** existing `log.*` call is
captured with no edit — the handler is the single structuring point.

### Worker processes (the subprocess boundary)

Every tournament run executes in its own `python -m
zicato._tournament_worker` subprocess (ROBUSTNESS.md L3). Its records must
reach the same stream. Two mechanisms were possible:

1. **stderr-wrapping** — pipe each worker's stderr and have the parent
   re-emit each line as a record. Rejected: the transport spawns workers
   with *inherited* stdio (`create_subprocess_exec(..., env=worker_env)`,
   no `stdout=`/`stderr=` redirection), and the parent already juggles a
   budget-bounded `wait_for` per worker; adding a per-worker pipe-drain
   task is real concurrency surface, and it would re-encode already-
   structured records as opaque text.
2. **shared-file append** (chosen) — the worker appends structured records
   to the *same* invocation stream file. The path crosses the boundary in
   the args file (`log_stream_path`), exactly like the sibling sidecar
   paths the transport already threads (`loss_path`, `sink_events_path`,
   `result_path`). The worker's `main()` installs the same
   `JsonlStreamHandler` pointed at that path in append (`"a"`) mode and
   binds its run context.

**Why shared-append is safe.** On **Linux**, a `write()` to a regular
file opened `O_APPEND` (which `open(path, "a")` sets) is serialized under
the inode lock: the kernel takes the file's `i_rwsem` for the whole
seek-to-end-plus-write, so concurrent appenders never interleave within a
single `write()` — and this holds for **arbitrary sizes**, not just small
ones. (`PIPE_BUF`, sometimes cited here, is the wrong guarantee: it is a
*pipe/FIFO* atomicity bound of 4096 bytes, and a big traceback record
easily exceeds it — so it would NOT justify torn-free large lines.)
Because each record is emitted by a single `write()` (one `logging`
handler `emit` → one line + terminator), the default handful of parallel
workers (`runtime.parallelism`, default 4) interleave safely at whole-line
granularity — never a torn line. The probe evidence matches: 6 processes
writing mixed records (some ~40 KB, well past `PIPE_BUF`) produced **zero
torn lines** on both ext4 and tmpfs.

This is **Linux-specific**, not portable POSIX: the standard does not
mandate whole-`write()` atomicity for regular files, and **NFS is
explicitly out** (its `O_APPEND` is emulated client-side and races). The
streams live on the local workspace filesystem, where the guarantee
holds. Shared-append also keeps **exactly one file per invocation**, which
makes retention trivial (count files, §4) and the reader trivial (read one
file, §3). A per-worker sibling file would multiply files per invocation,
blur the "keep last N invocations" count, and force the reader to
merge-sort N streams for one logical view.

The worker keeps its existing `logging.basicConfig(level=WARNING)` (stderr
for a human watching a foreground run); the JSONL handler is *additional*.
When no `log_stream_path` is supplied (an ad-hoc / test drive), the worker
behaves exactly as before — no file handler, stderr only.

## 3. The read path — files-canonical

The files are canonical. Exactly one reader
(`zicato.query.log_stream`) parses them, and the CLI and the dashboard
both call it — there is no second parser.

`build_log_view(paths, *, limit, level, after, invocation)` mirrors the
shape of `build_run_log` deliberately:

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
the selected stream, advancing the line cursor. An empty / no-logs
workspace prints nothing and exits 0 (honest silence, not an error).

### Dashboard operator-log pane

A workspace-level `#/logs` view (a peer of the builder / settings
surfaces, not epoch-scoped — logs are per-invocation) reads a new
`/api/logs` endpoint backed by the SAME reader. It renders the tail as
quiet mono rows, level-coloured via the existing `--v2-*` tone tokens,
with level-filter chips and an invocation picker — no decorative chrome.

The SSE-driven refresh is **digest-gated**: the pane folds its records
into a content digest and repaints via `gatedSwap`, so a no-op heartbeat
beat (identical digest) rebuilds ZERO DOM — the house render-discipline
rule (feedback: digest-gated dashboard renders). An empty workspace shows
an honest empty state.

## 6. Preflight capability warnings (relocation)

`dialect_capability_warnings(weights)` (telemetry/dialects.py) is a pure
function of the contract's `ScoringWeights` — it flags, e.g., drift knobs
that are inert under a drift-incapable telemetry dialect. It used to be
emitted inside the reducer, per board-unit, *inside the killable worker*:
repetitive (once per entry × replicate × generation) and — before this
work — invisible (worker records were not captured at all).

It is a **contract** property, not a run property, so it is surfaced
**once per invocation** at contract-load, beside the epoch-open preflight
machinery (the noise-aware, default-on `warn` contract pre-flight), via
`log.warning` — which now lands in the structured stream *and* the
operator's console. The per-entry reducer emission is **dropped**: it
carried no run-specific information, so re-emitting it N times per worker
was pure duplication that would now also bloat every worker's slice of the
shared stream. One authoritative surface at the contract-load preflight
replaces N invisible ones.
