# Storage

This document is the settled design for zicato's persistence. It
supersedes the earlier two-stage memo (a v0 directory layout plus a
deferred git roadmap). The earlier memo described a real plan but left
two things unresolved that this document closes:

1. **Which mechanism each kind of data uses** — stated here as a single
   table with reasons (§2), not scattered across this and
   `ANALYTICAL-INDEX.md`.
2. **The record-level vs generation-level seam fork** — the
   `StorageBackend` ABC is record-level (key→blob); generation
   snapshots are directory trees and a future git backend is
   commit/worktree-shaped. §4 resolves this fork explicitly and §5
   states what the implementation does about it now.

The git-backed generation store is still on the roadmap (§7). It is a
large, multi-week effort and is deliberately *not* implemented yet —
but the design no longer leaves the seam question open while waiting
for it. The `GenerationStore` protocol that a git backend would
implement is extracted **now**, with the directory backend behind it,
so the git backend is later a second implementation of a known
protocol rather than a refactor of unprotocoled code.

## 1. The shape of zicato's persistence

zicato persists five distinct kinds of data. They do not share an
access pattern, a durability requirement, or a query shape, so they do
not share a substrate. Conflating them would be wrong for at least
three of the five.

| # | Data kind | What it is |
|---|---|---|
| 1 | **Runtime state** | The orchestrator's live state the supervisor + dashboard read: `heartbeat.json`, `lock.json`, `active_tournament.json`, per-run `active_runs/*.json`, control-protocol flag files. |
| 2 | **Telemetry** | The `goldfive.v1.Event` stream of each tournament run — one `events.jsonl` per run. |
| 3 | **Generation source trees** | The post-apply inner-harness source at each generation (`generations/vN/snapshot/`). |
| 4 | **Lineage / experiments / journals** | The typed evolutionary record: `experiment.json` + per-patch files, `journal.md`, `lineage.json`, per-epoch `config.json` / `board.jsonl` / `scoring.json` / `brief.md`, cached `gen_score.json`. |
| 5 | **The analytical index** | The relational projection answering cross-run `GROUP BY` / `JOIN` questions: `.zicato/index.db`. |

## 2. The settled mechanism for each kind

> **The store of record stays files-canonical.** zicato runs N board
> entries as concurrent isolated subprocesses; each owns its own file,
> so the store of record is lock-free and crash-isolated. A misbehaving
> run's blast radius is exactly one file. No design below collapses the
> store of record into a single shared database — that would
> reintroduce the multi-writer contention and failure-coupling that
> subprocess isolation exists to remove.

| Data kind | Mechanism | Why this mechanism |
|---|---|---|
| **1. Runtime state** | **Files** — one JSON record per file, through `StorageBackend` (the file backend). | Each record is independently written by a different process (orchestrator, per-run workers). One-file-per-record is the lock-free, crash-isolated shape. A DB here would serialise independent writers behind one lock for zero query benefit — runtime state is read by key, never joined. |
| **2. Telemetry** | **JSONL** — `events.jsonl`, one append-only file per run, written by goldfive's `JSONLPersistenceSink`. | The access pattern is append-while-running, tail-for-the-log-panel, stream-to-SSE, replay-once-in-the-reducer. JSONL wins every one. Events are never queried *across* runs — the reduced `LossProfile` is (and that goes in the index). A row-per-event table would add write contention during the run for no query benefit. This is **goldfive's format**; zicato consumes it and does not re-schematize it. |
| **3. Generation source trees** | **Directory snapshots today; git on the roadmap (§7).** Behind the `GenerationStore` protocol (§5) either way. | The data is intrinsically file-shaped. The directory backend is a full `copytree` per generation — correct, simple, and what ships. A git backend's payoff is real (blob dedup across generations, `diff`/`log`/`blame`/`bisect` for free) but it is a multi-week effort; it is a *second* `GenerationStore` implementation, not a different design. |
| **4. Lineage / experiments / journals** | **Files** — JSON records + per-patch JSON files + markdown, through `StorageBackend` (the file backend). | These are the typed canonical record. They are small, human-readable in a pager, diffable, and edited at generation granularity by a single writer (the orchestrator) per epoch. Files keep them inspectable and keep the store of record uniform with runtime state. They are *projected* into the index (kind 5) for cross-run queries. |
| **5. The analytical index** | **A real database — SQLite today, DuckDB an evaluated option (§6).** Derived, disposable, rebuilt from kinds 1-4. | A relational index is exactly the right shape for cross-run aggregates ("loss across runs × generations × epochs"). This is the one place a real DB genuinely fits. It is **never canonical** — `zicato reindex` reconstructs it from the files, so it carries no information not already on disk. |

The principle, unchanged from the earlier memo and now stated as the
spine of the design: **a real DB only for the derived cross-run index;
files for every canonical record; JSONL for the append-only event
capture; git (eventually) for the generation source trees.** Each
substrate fits one access pattern; none is forced to do another's job.

## 3. `StorageBackend` — the record-level seam (shipped)

`zicato.storage.StorageBackend` is the abstraction kinds **1 and 4**
go through. It is a keyed, atomic JSON/JSONL/text record store:

```
read_json / write_json      read_text / write_text
exists / delete             list_keys
append_jsonl / read_jsonl
```

A *key* is a `/`-separated logical path (`"runtime/heartbeat.json"`,
`"epochs/2026-05-16_e1/generations/v3/experiment.json"`). The file
backend (`FileStorageBackend`) resolves a key to `root / key` and
writes through the `.tmp` + `fsync` + `os.replace` atomic discipline.
The in-memory backend (`InMemoryStorageBackend`) mirrors the same
observable semantics in dicts, for tests. The cross-backend contract is
pinned by `tests/test_storage_conformance.py`.

This seam is deliberately **record-level**: key→blob, read/write/list/
delete, append-to-stream. It is not an ORM and does not know what an
epoch or a generation is. That is a feature — see §4.

The runtime domain (`zicato.runtime`) routes every state read/write
through `StorageBackend` via the `zicato.runtime._storage` adapter. As
of this design the lineage/experiment/journal domain
(`zicato.epoch.journal`, `zicato.epoch.lineage`, `zicato.epoch.lifecycle`)
routes through it too — see §5.1.

## 4. The resolved fork: record-level seam vs generation-level seam

The earlier memo named, but did not resolve, a real design tension. The
`StorageBackend` ABC is record-level. Generation source trees (kind 3)
are directory trees, and a future git backend's natural unit is a
commit / a ref / a checked-out worktree. `write_json(key, data)` cannot
express *"commit generation v3 with this experiment metadata"*. There
were two ways to close this:

- **(a) Extend `StorageBackend`** with generation-level transaction
  boundaries — add `write_generation(epoch, gen, tree, meta)` and
  friends to the same ABC.
- **(b) Keep `StorageBackend` record-level**, and put generation/epoch
  operations in a *separate* seam in the `epoch/` layer.

**This design picks (b).** Reasons:

1. **The ABC's honesty is load-bearing.** `StorageBackend`'s own
   docstring commits it to being "honest to zicato's model … not an
   ORM and not a relational schema." A `write_generation(epoch, gen,
   …)` method makes the ABC carry zicato's *domain vocabulary* —
   epochs, generations, experiments. Every backend would then have to
   implement domain operations, not storage operations. The file
   backend would grow a method it implements as "copytree plus write
   some JSON" — domain logic leaking into the storage seam.

2. **The two seams have genuinely different units.** A record store's
   unit is a key→blob pair; atomicity is per-record. A generation
   store's unit is *a whole source tree plus its typed metadata*;
   atomicity is "the generation appears, or it does not." A git
   backend delivers the second with a commit; the directory backend
   delivers it with a `copytree` into a fresh directory. Forcing both
   units through one ABC means one of them is always contorted.

3. **The git backend would still not fit.** Even with (a), the git
   backend's natural read shape is "give me a checked-out worktree on
   disk," not "give me a `dict[str, bytes]`," and its experiment
   metadata lives in a commit message, not a JSON blob. (a) does not
   actually make the git backend a `StorageBackend`; it just moves the
   impedance mismatch into the ABC.

4. **The record-level seam already has a real consumer that fits it.**
   Runtime state and the experiment/journal/lineage records *are*
   key→blob shaped. Extending the ABC for the generation case would
   complicate the seam that those well-fitting consumers depend on, to
   accommodate a consumer that does not fit it anyway.

So: **two seams, at two layers.**

- **`StorageBackend`** (record-level, `zicato.storage`) — kinds 1 and
  4. Backends: files, memory, (future) anything else record-shaped.
- **`GenerationStore`** (generation-level, `zicato.epoch.genstore`) —
  kind 3. Backends: the directory backend today, a git backend later.

`GenerationStore` is **not** a subtype of `StorageBackend` and does not
route through it. It is a peer abstraction at the domain layer, exactly
where epoch/generation vocabulary belongs.

## 5. What the implementation does now

Three concrete changes ship with this design. The git backend (§7) does
*not* — it is correctly scoped as a separate, later effort.

### 5.1 `epoch/` migrated onto `StorageBackend`

`zicato.epoch.journal`, `zicato.epoch.lineage`, and the config writes in
`zicato.epoch.lifecycle` previously did direct, partly non-atomic file
I/O (`path.write_text(json.dumps(...))` — a write with no `.tmp` +
`fsync` + `os.replace`, so a crash mid-write could leave a truncated
`experiment.json` / `lineage.json` / `config.json`). They now route
every record read/write through `StorageBackend`, via a
`zicato.epoch._storage` adapter that mirrors `zicato.runtime._storage`:

- one `backend_for(workspace_root)` seam,
- `*_key` helpers turning an `(epoch, generation, …)` coordinate into a
  logical storage key.

Every public `epoch/` function keeps its `workspace_root: Path`-first
signature unchanged — a caller cannot tell. The win is uniform
**atomicity**: `experiment.json`, `lineage.json`, `config.json`,
`scoring.json`, the journal, and the per-patch files are now all
crash-safe with the same `.tmp` + `fsync` + rename discipline the
runtime layer already had. The on-disk layout is byte-identical.

The per-patch write order is preserved: patch files first, then
`experiment.json` last, so a crash between phases leaves harmless
orphan patch files rather than a dangling `patch_ids` reference.

### 5.2 `GenerationStore` protocol extracted; directory backend behind it

`zicato.epoch.genstore` defines the `GenerationStore` protocol — the
generation-level seam from §4 — and ships one implementation,
`DirectoryGenerationStore`, which is the existing directory-snapshot
mechanism, byte-for-byte:

```python
class GenerationStore(Protocol):
    def snapshot_root(self, epoch_id, generation_id) -> Path: ...
    def has_generation(self, epoch_id, generation_id) -> bool: ...
    def list_generations(self, epoch_id) -> list[str]: ...
    def seed_generation(self, epoch_id, generation_id, sources) -> Path: ...
    def derive_generation(self, epoch_id, parent_generation_id,
                          child_generation_id, patches) -> Path: ...
```

`derive_generation` is the generation-level transaction boundary the
record seam could not express: it copies the parent's snapshot, applies
the patch set all-or-nothing (via `zicato.mutation.applier.apply_patches`,
whose pre-validation already makes it atomic), and returns the child
snapshot root — child generation appears or it does not.

The directory backend's `snapshot_root` returns a real on-disk path,
because the orchestrator and the subprocess workers genuinely need a
path (the worker `chdir`s into a snapshot and loads the adapter from
it). This is the read shape a *git* backend would satisfy with a
worktree checkout — which is exactly why §4 rejected forcing this
through `StorageBackend`'s `read_json`-shaped surface. The protocol is
written so a git backend is a drop-in second implementation.

The orchestrator's snapshot helpers (`_snapshot_root`,
`_ensure_baseline_snapshot`'s copy logic, the `apply_patches` call) are
refactored to go through a `GenerationStore`. The default is
`DirectoryGenerationStore`; the seam is now the single place a git
backend would be substituted.

### 5.3 The analytical index: continuous indexing made the design

The index already supports both `rebuild_index` (batch, backs `zicato
reindex`) and `ingest_run` / `ingest_experiment` (incremental). The
orchestrator already calls the incremental path live (best-effort
dual-write). This design **promotes continuous indexing from an
add-on to the stated design**:

- The index is **continuously maintained** by the orchestrator's
  dual-write as the loop runs — the dashboard never shows stale
  analytics mid-epoch.
- `zicato reindex` is the **batch rebuild / repair** path: drop and
  reconstruct from the files. It is the recovery mechanism (a behind
  or corrupt index), not the normal update path.
- The ordering rule that makes the non-transactional dual-write safe
  is unchanged and now documented as load-bearing: **the canonical
  file is written first, the index row second.** A crash between them
  leaves the index *behind* the files (self-healing — the next
  `ingest_*` or `reindex` catches up), never *ahead* (a phantom row,
  which the ordering rule makes impossible).

No schema change ships with this design — see §6 for the SQLite vs
DuckDB evaluation and why SQLite stays for now.

## 6. The analytical index database: SQLite vs DuckDB

`index.db` is a **derived, disposable** analytical index, rebuilt from
the files. The choice of engine is therefore low-stakes and reversible
— it can change without touching a single canonical byte. This section
records the evaluation; the decision is **stay on SQLite for now**.

**What DuckDB would buy.** DuckDB is columnar and vectorised; for
analytical scans — "mean `drift_loss` per drift-kind across every
generation of every epoch," "the loss distribution of every aborted
run" — it is materially faster than SQLite's row store, and its SQL
dialect is friendlier for that work (`PIVOT`, richer aggregates, native
list/struct columns).

**Why SQLite stays, for now.**

1. **Scale.** The index is small. A large workspace is multiple epochs
   × hundreds of generations × tens of runs — low tens of thousands of
   rows. SQLite scans that in single-digit milliseconds. DuckDB's
   columnar advantage shows at millions-plus rows; the index is three
   orders of magnitude short of where the engine choice would be felt.

2. **The schema is a cross-language contract.** `zicato.index.schema`
   is consumed by the Rust supervisor (`supervisor/src/index_db.rs`,
   via `rusqlite`) and the dashboard analytics. Moving to DuckDB means
   re-tooling the Rust reader (`rusqlite` → a DuckDB crate) and
   re-validating that contract. That is real work, justified only by a
   real bottleneck — and there is none (point 1).

3. **`reindex` is already seconds.** The recovery path's cost is
   `O(total artifacts)` file reads, paid once. The engine is not the
   bottleneck in a rebuild; the file reads are.

4. **Concurrency posture is solved on SQLite.** The index is opened
   WAL-mode so the supervisor and dashboard read concurrently with the
   orchestrator's dual-writes. DuckDB's single-writer/multi-reader
   story is different enough to need re-validation against the live
   dual-write path.

**The trigger to revisit.** If cross-run analytics grows to where scan
latency is felt by an operator — concretely, if `reindex` or a
dashboard analytics query crosses ~1s on a real workspace — DuckDB
becomes the right move, and the derived-and-disposable nature of
`index.db` means the migration is: change `zicato.index`, re-tool the
Rust reader, bump `SCHEMA_VERSION`, ship. No canonical data moves. The
door is deliberately left open; it is just not walked through on
speculation.

## 7. The git-backed generation store (roadmap, not shipped)

A git backend for kind 3 (generation source trees) remains the
roadmap. Its payoff is real and unchanged from the earlier memo:

- **Blob dedup.** A prompt module unchanged across 20 generations is
  one git blob, not 20 `copytree` copies. For long lineages (target 3
  — zicato optimising zicato — can run 50+ generations) the directory
  backend's disk cost is the motivating problem.
- **Native tooling.** `git diff v3 v8`, `git log`, `git blame`,
  `git bisect` answer the most common operator questions for free.
- **Cheap parallel checkouts.** `git worktree` per tournament run
  instead of a per-run snapshot directory.

The design for it is settled and is the right design — it is just a
multi-week effort that is correctly *not* bundled into this storage
pass:

- **One git repo for the whole workspace** at `.zicato/repo/` (not one
  per epoch — cross-epoch `diff`/`log` and cross-epoch blob dedup both
  want one repo). The user's outer repo is untouched; `.zicato/repo/`
  is entirely private.
- **One branch per epoch** (`epoch/{id}/main`), **one tag per
  generation** (`epoch/{id}/v{N}`, and `…/v{N}-rejected` for rejected
  attempts — recoverable, off the main lineage).
- **Experiment metadata in the commit message**, behind a
  `---zicato-meta---` sentinel block. Visible in plain `git log`,
  transports with fetch/push, trivially parsed.
- **Cross-epoch parentage via normal commits** — a new epoch's `v0` is
  parented to the previous epoch's promoted head. No `--orphan`
  branches; the operator's mental model is "each epoch continues from
  the last."

Because §4/§5 extracted the `GenerationStore` protocol now, the git
backend lands as **`GitGenerationStore implements GenerationStore`** —
a second implementation of a known, tested protocol, selected by config
at `zicato init` time — not as a refactor of unprotocoled code. The
directory backend stays the default and the always-available fallback.

Migration from a directory-snapshot workspace to a git-backed one is a
one-shot `zicato workspace migrate-to-git`: walk every epoch's
`generations/`, import each generation as a commit on its epoch branch,
remove the `generations/` directories once the repo is built, flip
`config.json`'s `storage_backend` to `"git"`. It writes to a staging
location first so a failure leaves the workspace untouched, and a
pre-migration backup is kept under `.zicato/migrations/`.

That work is its own roadmap item. This document settles the design so
that item is an implementation, not a redesign.

## 8. Migration of existing workspaces

This storage pass changes *no on-disk bytes* and therefore needs **no
data migration**:

- The `epoch/` → `StorageBackend` move (§5.1) is an internal routing
  change; `experiment.json`, `lineage.json`, `config.json`, the
  journal, and per-patch files land at the same paths with the same
  content. The only observable change is that writes are now atomic.
- The `GenerationStore` extraction (§5.2) wraps the existing
  directory-snapshot mechanism; `generations/vN/snapshot/` is
  unchanged.
- The index (§5.3) is derived and disposable; nothing to migrate.

The one pre-existing migration utility,
`zicato.epoch.migrate.migrate_inline_to_perpatch` (legacy inline
`patches: [...]` array → per-patch files), is unchanged and remains the
opportunistic converter for pre-per-patch workspaces. The
`read_experiment` reader still transparently accepts both shapes, so no
operator is forced to run it.

The directory-snapshot → git migration (`migrate-to-git`) ships only
when the git backend ships (§7).

## 9. Cross-references

| Topic | Document |
|---|---|
| `experiment.json`, per-generation directories, journals, epochs | [EPOCHS-AND-JOURNALING.md](EPOCHS-AND-JOURNALING.md) |
| The `.zicato/runtime/` layer — runtime state, the other `StorageBackend` consumer | [RUNTIME.md](RUNTIME.md) |
| Per-run event capture — `events.jsonl`, goldfive's `JSONLPersistenceSink` | [TELEMETRY.md](TELEMETRY.md) |
| The `.zicato/index.db` analytical index — schema, dual-write discipline, `reindex` | [ANALYTICAL-INDEX.md](ANALYTICAL-INDEX.md) |
| Subprocess-isolated tournament runs (why the store of record is files-canonical) | [ROBUSTNESS.md](ROBUSTNESS.md) |
| `MutationPoint.id` references that patches carry | [MUTATION-SURFACE.md](MUTATION-SURFACE.md) |
| CLI surface (`zicato reindex`, and the future `zicato repo` / `log` / `diff`) | [CLI.md](CLI.md) |
| Why the canonical layer is filesystem-native | [RATIONALE.md](RATIONALE.md) §7 |
