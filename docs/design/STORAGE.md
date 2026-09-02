# Storage

This document is the settled design for zicato's persistence. It
answers two questions:

1. **Which mechanism carries each kind of data.** One table states the
   mechanism and the reason for it (§2), so the answer lives in one
   place rather than being split between this document and
   `ANALYTICAL-INDEX.md`.
2. **How the record-level and generation-level storage seams divide the
   work.** The `StorageBackend` abstract base class is record-level and
   maps a key to a blob; generation snapshots are directory trees, and
   the git backend is shaped around commits and worktrees. §4 resolves
   that division and §5 states what the implementation does.

The `GenerationStore` protocol that carries the generation-level seam
is shipped, and both backends behind it are shipped. `zicato init`
selects the git-backed implementation (`GitGenerationStore`); the
directory-snapshot implementation is the supported fallback for
workspaces without Git. zicato does not migrate a directory-backed
workspace onto Git. Operators can inspect the private generation
repository with ordinary Git commands; the shipped command-line
interface has no `zicato repo` command family.

## 1. The shape of zicato's persistence

zicato persists five distinct kinds of data. They do not share an
access pattern, a durability requirement, or a query shape, so they do
not share a substrate. Conflating them would be wrong for at least
three of the five.

| # | Data kind | What it is |
|---|---|---|
| 1 | **Runtime state** | The orchestrator's live state, read by the supervisor and the dashboard: `heartbeat.json`, `lock.json`, `active_tournament.json`, the per-run `active_runs/*.json`, and the control-protocol flag files. |
| 2 | **Telemetry** | The `goldfive.v1.Event` stream of each tournament run — one `events.jsonl` per run. |
| 3 | **Generation source trees** | The post-apply inner-harness source at each generation: a tagged commit under Git or `generations/vN/snapshot/` under the directory backend. |
| 4 | **Lineage / experiments / journals** | The typed evolutionary record: `experiment.json` + per-patch files, `journal.md`, `lineage.json`, per-epoch `config.json` / `board.jsonl` / `scoring.json` / `brief.md`, cached `gen_score.json`. |
| 5 | **The analytical index** | The relational projection answering cross-run `GROUP BY` / `JOIN` questions: `.zicato/index.db`. |

## 2. The settled mechanism for each kind

> **The store of record stays files-canonical.** zicato runs N board
> entries as concurrent isolated subprocesses, and each owns its own
> file, so the store of record is lock-free and crash-isolated. A
> misbehaving run can corrupt exactly one file. No mechanism below
> collapses the store of record into a single shared database, which
> would bring back the multi-writer contention and failure coupling
> that subprocess isolation exists to remove.

| Data kind | Mechanism | Why this mechanism |
|---|---|---|
| **1. Runtime state** | **Files** — one JSON record per file, through `StorageBackend` (the file backend). | Each record is written independently by a different process: the orchestrator, or a per-run worker. One file per record is the lock-free, crash-isolated shape. A database here would serialise independent writers behind one lock for no query benefit, because runtime state is read by key and never joined. |
| **2. Telemetry** | **JSONL** — `events.jsonl`, one append-only file per run, written by goldfive's `JSONLPersistenceSink`. | The access pattern is append while running, tail for the log panel, stream over server-sent events, and replay once in the reducer. JSONL suits all four. Events are never queried across runs; the reduced `LossProfile` is, and that goes in the index. A row-per-event table would add write contention during the run for no query benefit. The format is goldfive's; zicato consumes it and does not re-schematize it. |
| **3. Generation source trees** | **Git or directory snapshots, selected explicitly by config.** Behind the `GenerationStore` protocol (§5) either way. | The data is intrinsically file-shaped. `zicato init` selects git, whose object store deduplicates unchanged blobs. The directory backend remains a supported full-`copytree` implementation. Both keep the source tree code-only via the shared artifact-exclusion policy (`snapshot_scope`, §5.2.1). |
| **4. Lineage / experiments / journals** | **Files** — JSON records + per-patch JSON files + markdown, through `StorageBackend` (the file backend). | These are the typed canonical record. They are small, human-readable in a pager, diffable, and edited at generation granularity by a single writer (the orchestrator) per epoch. Files keep them inspectable and keep the store of record uniform with runtime state. They are *projected* into the index (kind 5) for cross-run queries. |
| **5. The analytical index** | **A real database — SQLite today, DuckDB an evaluated option (§6).** Derived, disposable, rebuilt from kinds 1-4. | A relational index is the right shape for cross-run aggregates ("loss across runs × generations × epochs"). This is the one place a database fits. It is **never canonical** — `zicato repair index` reconstructs it from the files, so it holds no information that is not already on disk. |

The principle behind the table: a database only for the derived
cross-run index; files for every canonical record; JSONL for the
append-only event capture; and Git or directory snapshots for
generation source trees. Each substrate fits one access pattern, and
none is forced to do another's job.

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

This seam is record-level by design: key to blob, read, write, list,
delete, and append-to-stream. It is not an object-relational mapper and
knows nothing about epochs or generations. §4 explains why.

The runtime domain (`zicato.runtime`) routes every state read and write
through `StorageBackend` via the `zicato.runtime._storage` adapter. The
lineage, experiment, and journal domain (`zicato.epoch.journal`,
`zicato.epoch.lineage`, `zicato.epoch.lifecycle`) routes through it too
(§5.1).

## 4. The resolved fork: record-level seam vs generation-level seam

A real design tension runs between the two units of storage. The
`StorageBackend` abstract base class is record-level. Generation source
trees (kind 3) are directory trees, and the git backend's natural unit
is a commit, a ref, or a checked-out worktree. `write_json(key, data)`
cannot express "commit generation v3 with this experiment metadata".
Two ways of closing the gap are available:

- **Extend `StorageBackend`** with generation-level transaction
  boundaries, adding `write_generation(epoch, gen, tree)` and
  companion methods to the same base class.
- **Keep `StorageBackend` record-level**, and put generation and epoch
  operations in a separate seam in the `epoch/` layer.

The design keeps `StorageBackend` record-level, for four reasons.

1. **The base class's honesty is load-bearing.** `StorageBackend`'s own
   docstring commits it to being "honest to zicato's model … not an
   ORM and not a relational schema." A `write_generation(epoch, gen,
   …)` method makes the base class carry zicato's domain vocabulary of
   epochs, generations, and experiments. Every backend would then have
   to implement domain operations in place of storage operations. The
   file backend would grow a method it implements as a `copytree`,
   which leaks domain logic into the storage seam.

2. **The two seams have different units.** A record store's unit is a
   key-to-blob pair, and atomicity is per record. A generation store's
   unit is a whole source tree, and atomicity means the generation
   appears or it does not. A git backend delivers the second with a
   commit; the directory backend delivers it with a `copytree` into a
   fresh directory. Forcing both units through one base class contorts
   one of them.

3. **The git backend would still not fit.** Even with the extended base
   class, the git backend's natural read shape is a checked-out
   worktree on disk rather than a `dict[str, bytes]`, and experiment
   and patch metadata stay in the separate record store. Extending the
   base class does not make the git backend a `StorageBackend`; it
   moves the impedance mismatch into the base class.

4. **The record-level seam has consumers that fit it.** Runtime state
   and the experiment, journal, and lineage records are shaped as key
   to blob. Extending the base class for the generation case would
   complicate the seam those consumers depend on, in order to
   accommodate a consumer that does not fit it anyway.

The result is two seams at two layers.

- **`StorageBackend`** (record-level, `zicato.storage`) — kinds 1 and
  4. Backends: files, memory, (future) anything else record-shaped.
- **`GenerationStore`** (generation-level, `zicato.epoch.genstore`) —
  kind 3. Backends: directory snapshots and Git commits/worktrees.

`GenerationStore` is not a subtype of `StorageBackend` and does not
route through it. It is a peer abstraction at the domain layer, where
epoch and generation vocabulary belongs.

## 5. What the implementation does

The implementation has four parts: the `epoch/` records on
`StorageBackend` (§5.1), the `GenerationStore` protocol with both
backends behind it, directory and git (§5.2), continuous indexing
(§5.3), and generalized tournament records (§5.4). The git backend
(§7) is the selection `zicato init` writes; the directory backend is
available when Git is unwanted.

### 5.1 `epoch/` records on `StorageBackend`

`zicato.epoch.journal`, `zicato.epoch.lineage`, and the config writes in
`zicato.epoch.lifecycle` route every record read and write through
`StorageBackend`, via a `zicato.epoch._storage` adapter that mirrors
`zicato.runtime._storage`:

- one `backend_for(workspace_root)` seam,
- `*_key` helpers turning an `(epoch, generation, …)` coordinate into a
  logical storage key.

Every public `epoch/` function takes `workspace_root: Path` as its
first argument, so the routing is invisible to callers. What the
routing buys is uniform atomicity. `experiment.json`, `lineage.json`,
`config.json`, `scoring.json`, the journal, and the per-patch files all
go through the same `.tmp`, `fsync`, and rename discipline the runtime
layer uses, so a crash mid-write cannot leave any of them truncated.
The on-disk layout is byte-identical to a direct write.

Candidate creation first records the applied generation as a pending lineage
node (`promoted=null`), then writes the patch records, and writes
`experiment.json` last. The pending lineage node is the cleanup commit marker:
a crash before it leaves source-only residue that the current lineage-based
recovery cannot identify, while a crash after it gives resume enough
coordinates to discard the complete candidate field. Writing patch records
before `experiment.json` prevents a dangling `patch_ids` reference.

A resolved field round spans several atomic records. Before updating the first
experiment outcome, zicato writes
`epochs/{epoch}/rounds/{round}/field_settlement.json`. The pending receipt
contains the final candidate outcomes and the complete settled bracket. Replay
derives lineage facts from the candidate experiments and outcomes; it derives
structure, decision, and reason from the bracket. The receipt separately stores
the primary promoted generation and requires exact agreement with the bracket,
because several candidates may have promoted outcomes while exactly one may
advance the champion marker.
Startup can replay the fixed commit order without running a matchup or gate
again. Replay writes outcomes, settled lineage, the champion marker, journal
entries, and the canonical bracket before refreshing the derived index as one
reported operation. Journal sections carry a stable settlement identity, so
replay does not duplicate them. Completion changes the same full record to
`state="committed"`; zicato retains it instead of replacing it with a
tombstone or deleting it.

The retained receipt reports the derived-index result as `succeeded`,
`repair_required`, or `repaired`. A failed grouped projection leaves every
canonical record committed and instructs a full `zicato repair index`; a
successful rebuild changes `repair_required` to `repaired` while preserving
the original exception type. The receipt reports the post-promotion hook as
`not_applicable`, `pending`, `succeeded`, `failed`, or `delivery_unknown`.
The live caller writes `delivery_unknown` before invoking an external hook.
Recovery never retries an unknown delivery, which preserves the hook's
at-most-once contract.

### 5.2 `GenerationStore` protocol; both backends behind it

`zicato.epoch.genstore` defines the `GenerationStore` protocol — the
generation-level seam from §4. Two implementations ship:
`DirectoryGenerationStore` (the directory-snapshot mechanism) and
`GitGenerationStore` (the backend selected by `zicato init` — §7). The
protocol:

```python
class GenerationStore(Protocol):
    @property
    def backend_name(self) -> str: ...
    def snapshot_path(self, epoch_id, generation_id) -> Path: ...
    def materialize_snapshot(self, epoch_id, generation_id) -> Path: ...
    def has_generation(self, epoch_id, generation_id) -> bool: ...
    def list_generations(self, epoch_id) -> list[str]: ...
    def seed_generation(self, epoch_id, generation_id, sources) -> Path: ...
    def derive_generation(self, epoch_id, parent_generation_id,
                          child_generation_id, patches) -> Path: ...
    # read surface — the dashboard file-tree / file-browser API
    def list_tree(self, epoch_id, generation_id) -> list[TreeEntry]: ...
    def read_file(self, epoch_id, generation_id, rel_path) -> bytes: ...
    def diff_generations(self, epoch_id, from_generation_id,
                         to_generation_id) -> str: ...
    def prune_generations(self, epoch_id, generation_ids,
                          *, dry_run) -> int: ...
```

`derive_generation` is the generation-level transaction boundary the
record seam cannot express. It derives the child from the parent's
tree, applies the patch set all-or-nothing through
`zicato.mutation.applier.apply_patches`, whose pre-validation makes the
apply atomic, and returns the child snapshot root. The child generation
appears in full or not at all.

`snapshot_path` performs pure coordinate-to-path calculation.
`materialize_snapshot` returns a usable local tree and performs any required
checkout. The split makes I/O explicit for the git backend while preserving
the real-path contract the subprocess worker needs.

The `list_tree`, `read_file`, and `diff_generations` surface is source-only.
Patch and experiment reads use `StorageBackend`; source pruning cannot remove
those records. `diff_generations` is one shared rendering over every backend
(§7.4), so the diff text a reader receives is a function of the two trees
alone. Source mutation goes through `seed_generation`,
`derive_generation`, and the retention-controlled `prune_generations` method.
Pruning accepts the complete selected batch so the Git implementation can
remove every tag and worktree under one administration lock and run repository
maintenance once.

The orchestrator's snapshot helpers go through a `GenerationStore`;
`default_generation_store(workspace_root)` is the single construction
seam. It selects the backend through
`resolve_generation_store_backend(workspace_root)`. Every initialized
workspace must define `generation_source_backend` as `git` or `directory`.
Missing, blank, malformed, and unknown values raise. The resolver does not
scan `repo/.git`, generation records, or snapshot directories for evidence.

`zicato init` writes `generation_source_backend` into a new workspace's
`config.json`, so a workspace created today is decided by rule 1 forever
and a later change of default cannot re-interpret it.

#### 5.2.1 The mutable surface is code-only — artifact exclusion

A generation source tree must be code-only. The presentation-agent
target writes its rendered webpage under an `output/` directory inside
its own source directory. Without a filter, every `copytree` that
derives a child generation copies that `output/` forward, and the copy
compounds generation over generation until the disk is exhausted. That
failure has been observed, and the exclusion policy below closes it.

`zicato.epoch.snapshot_scope` is the single artifact-exclusion policy
both backends consult. It declares an artifact-name set (`output/`,
`__pycache__`, the lint/type caches, nested VCS / dependency
directories), an `is_artifact(path)` predicate, a
`copytree`-compatible `ignore` callable, and `.gitignore` line
generation. The directory backend passes the ignore callable to every
`shutil.copytree`; the git backend writes the `.gitignore` lines into
the generation repo so the same names never enter a commit.

Run output is routed elsewhere rather than only excluded from the copy.
The tournament runner creates a per-run scratch directory outside every
snapshot and exports it to the inner harness through the
`ZICATO_RUN_SCRATCH_DIR` environment variable, and a target writes its
run output there. After the harness returns and before outcome grading,
the worker deterministically inventories every regular file beneath
that directory, copies it into the canonical run directory, and
attaches the typed inventory to `RunResult.artifacts`. The contract is
the output discovered on disk, rather than a list of filenames declared
on the board. Symlinks are never followed, and file-count and byte
bounds are recorded as truncation rather than silently changing the
inventory.

Replicate zero persists `artifacts/` plus `artifacts.json` beside `loss.json`;
replicate `rN` uses `artifacts.rN/` plus `artifacts.rN.json`. The manifest has
sorted relative paths, sizes, media types, and content hashes, with no absolute
scratch paths or timestamps. It therefore survives scratch cleanup and is both
grader-readable and reproducible from the filesystem source of truth.

The adapter contract also carries snapshot hygiene. `HarnessAdapter`
declares `run_output_names`, the extra artifact names, and
`mutable_subpaths(generation_root)`, the narrowed mutable surface the
mutation enumerator walks. Support code stays in the snapshot so the
worker can execute it, while staying outside the proposer's editable
surface.

### 5.3 The analytical index: continuous indexing

The index supports both `rebuild_index`, the batch path behind `zicato
reindex`, and the incremental `ingest_run` and `ingest_experiment`. The
orchestrator calls the incremental path live as a best-effort
dual-write. Continuous indexing is the design rather than an add-on:

- The orchestrator's dual-write maintains the index continuously as the
  loop runs, so the dashboard never shows stale analytics mid-epoch.
- `zicato repair index` is the batch rebuild and repair path: drop the
  index and reconstruct it from the files. It is the recovery mechanism
  for an index that is behind or corrupt, and it is not the normal
  update path.
- One ordering rule makes the non-transactional dual-write safe, and it
  is load-bearing: **the canonical file is written first, the index row
  second.** A crash between the two leaves the index behind the files,
  which self-heals when the next `ingest_*` or `reindex` catches up.
  The ordering makes the opposite state, a phantom index row ahead of
  the files, impossible.

Continuous indexing requires no schema change. §6 records the SQLite
versus DuckDB evaluation and why SQLite stays.

### 5.4 The generalized tournament record (configurable structures)

> **Status.** Shipped. Full spec:
> [TOURNAMENT-DATA-MODEL.md](TOURNAMENT-DATA-MODEL.md) §2.

Tournament persistence supports the king-of-the-hill gauntlet and the
configurable field structures `single_elim`, `double_elim`, `swiss`,
and `racing`. Every persisted field is additive, so the gauntlet shape
stays compact. The generalization touches three records and adds no new
storage mechanism; it rides on the seams described in §5.1 and §5.2.

**(a) The live runtime record** — `runtime/active_tournament.json`
(`ActiveTournament`), one JSON record via `StorageBackend`. It carries a
**structure envelope** alongside the existing two-side fields:

- `structure` / `structure_params` — copied from the epoch contract at
  tournament start (default `"gauntlet"` / `{}`).
- `competitors` — the full candidate field (generation id + seed +
  role), the generalization of "two generations". A gauntlet's two
  competitors stay derivable from `parent_generation_id` /
  `child_generation_id`.
- `rounds` — a tagged-union list of per-round / per-rung / per-bracket
  match state (one shape per structure; the gauntlet degenerates to one
  round, one match).
- `standings` — a flat live ranking (`generation_id`, `rank`, `scalar`,
  `wins`/`losses`, `status`).

The per-entry rows (`ActiveTournamentEntry`) keep their `(entry_id,
side)` key. `side` accepts an opaque competitor key (a generation id)
for non-gauntlet structures as well as `"parent"` and `"child"`, and
each row carries a `match_id` (default `""`). One
`update_tournament_entry` signature serves every structure. `base.py`,
`files.py`, and `memory.py` need no structure-specific behavior,
because the fields are ordinary JSON on the same `to_dict` and
`from_dict` path.

**(b) The settled per-generation outcome** — `experiment.json`'s
`outcome` block (`OutcomeRecord`), persisted via `epoch/_storage.py`. It
carries `structure`, `final_rank`, `eliminated_in_round`, and a
per-generation `match_record`. `write_experiment` /
`_outcome_from_dict` read/write them; no new key helper, no new file.

**(c) The settled index row** — the SQLite `tournaments` table. It carries
five additive `TEXT` columns — `structure`, `structure_params_json`,
`competitors_json`, `rounds_json`, and `standings_json` — as a version
3 column add, following the same pattern as the version 2 column add
(`_V2_ADDED_COLUMNS`). The per-matchup columns remain and describe the
crowning match for every structure, so a reader that understands only
gauntlets still gets a coherent answer. The index is derived and
rebuildable, so the schema move is a drop and re-derive on the
`reindex` path and an incremental column add otherwise, under the same
self-healing dual-write ordering rule (§5.3).

**Compatibility.** Every added field defaults to the gauntlet
interpretation, and the index, the only stateful store involved, is
rebuildable. No migration tool is needed: a workspace holding only
gauntlet tournaments loads and renders correctly.

## 6. The analytical index database: SQLite vs DuckDB

`index.db` is a derived, disposable analytical index, rebuilt from the
files. The choice of engine is therefore low-stakes and reversible: it
can change without touching a single canonical byte. The evaluation
below settles on SQLite.

**What DuckDB would buy.** DuckDB is columnar and vectorised. On
analytical scans — the mean `drift_loss` per drift kind across every
generation of every epoch, or the loss distribution of every aborted
run — it is materially faster than SQLite's row store. Its SQL dialect
also suits that work better, with `PIVOT`, richer aggregates, and
native list and struct columns.

**Why SQLite stays.** Four reasons:

1. **Scale.** The index is small. A large workspace is multiple epochs
   × hundreds of generations × tens of runs — low tens of thousands of
   rows. SQLite scans that in single-digit milliseconds. DuckDB's
   columnar advantage shows at millions-plus rows; the index is three
   orders of magnitude short of where the engine choice would be felt.

2. **The schema is a cross-language contract.** `zicato.index.schema`
   is consumed by the Rust supervisor (`crates/supervisor/src/index_db.rs`,
   via `rusqlite`) and the dashboard analytics. Moving to DuckDB means
   re-tooling the Rust reader (`rusqlite` → a DuckDB crate) and
   re-validating that contract. That is real work, justified only by a
   real bottleneck — and there is none (point 1).

3. **A `reindex` takes seconds.** The recovery path's cost is
   `O(total artifacts)` file reads, paid once. The file reads, rather
   than the engine, are the bottleneck in a rebuild.

4. **Concurrency posture is solved on SQLite.** The index is opened
   WAL-mode so the supervisor and dashboard read concurrently with the
   orchestrator's dual-writes. DuckDB's single-writer/multi-reader
   story is different enough to need re-validation against the live
   dual-write path.

**The trigger to revisit.** If cross-run analytics grows until an
operator feels the scan latency — concretely, if a `reindex` or a
dashboard analytics query crosses about one second on a real workspace
— DuckDB becomes the right move. Because `index.db` is derived and
disposable, the migration is to change `zicato.index`, re-tool the Rust
reader, raise `SCHEMA_VERSION`, and ship. No canonical data moves. The
option stays open, and is taken only on a measured need.

## 7. The git-backed generation store (`GitGenerationStore`)

The git backend for kind 3, generation source trees, ships in
`zicato.epoch.git_genstore` as a second `GenerationStore`
implementation, and is the backend `zicato init` writes.
`DirectoryGenerationStore` is the always-available fallback for
workspaces without Git, selected by
`generation_source_backend: "directory"`.

> **How delta materialization is handled.** The git object store is the
> delta representation, because it deduplicates blobs across
> generations, and the per-run `git worktree` that
> `checkout_ephemeral` creates is the cheap isolated materialization.
> No overlay-filesystem or reflink layer is planned.
> [GENERATION-ISOLATION.md](GENERATION-ISOLATION.md) is a decision
> record for that choice: it holds the comparative analysis of the
> rejected alternatives (an overlay filesystem, reflink copy-on-write,
> hardlinks, and `git archive`) and the measurements that settled it.

### 7.1 Why git, and the payoff

- **Blob deduplication.** A prompt module that stays the same across 20
  generations is one git blob referenced by 20 commits, rather than 20
  `copytree` copies. Long lineages make this matter: the dogfood target
  in which zicato optimises zicato can run 50 or more generations, and
  the directory backend's disk cost over such a lineage is the
  motivating problem. Git's content-addressed object store removes it.
- **Native tooling.** `git diff`, `git log`, `git blame`, `git bisect`
  on `{workspace}/repo/` answer the common operator questions for free.
- **Cheap parallel checkouts.** `git worktree` per tournament run
  replaces the directory backend's per-run `copytree` ephemeral
  snapshot — git shares the object store; only the working files are
  materialised.

### 7.2 The domain → git mapping

| zicato concept | git construct | Why |
|---|---|---|
| **Workspace** | One repository, `{workspace_root}/repo/` | One repository for the whole workspace rather than one per epoch: cross-epoch `diff` and `log`, and cross-epoch blob deduplication, all want a single object store. The repository is private to zicato and the user's outer repository is untouched. |
| **Epoch** | A branch, `epoch/{epoch_id}` | An epoch's generations are a commit chain on its branch. A branch is the natural "sequence of related commits" unit. |
| **Generation** | A commit, tagged `epoch/{epoch_id}/{generation_id}` | The commit is the immutable tree; the tag is the stable handle. The branch head moves as generations are appended; the tags do not. |
| **Generation source parentage** | The commit DAG | A child generation commit parents its source parent. Promotion decisions remain canonical in lineage and experiment records. |
| **Commit context** | A redundant JSON block after the commit message's `---zicato-meta---` sentinel | Plain `git log` remains useful to operators. Canonical patch and experiment reads use `StorageBackend` records. |
| **Parallel tournament run** | A per-run `git worktree` checked out at the generation tag (`checkout_ephemeral`) | Isolated, cheap per-run checkout; a runtime write inside it never touches the commit. Replaces the directory backend's per-run `copytree` (`copy_checkout_ephemeral`). |

A repo-root orphan branch `zicato-root` carries only the
artifact-exclusion `.gitignore`; every epoch branch is created from it,
so the `.gitignore` is shared and cross-epoch `diff` has a common base.

### 7.3 Design-review record — decisions and rejected alternatives

This subsection records the decisions behind the mapping in §7.2 and
the alternatives that were rejected.

- **Branch name `epoch/{id}`.** A proposal for `epoch/{id}/main`, with
  per-epoch ref namespaces, was rejected. An epoch has exactly one
  lineage branch, so the `/main` suffix carries no information.
- **Generation tag `epoch/{id}/{gen}`.** A tag rather than a branch is
  correct, because a generation is immutable once created and a tag is
  git's immutable-handle construct.
- **Tags for rejected generations (`…-rejected`).** Deferred. The
  `derive_generation` contract is all-or-nothing, so a rejected
  attempt — a patch set that fails to apply — never produces a commit
  to tag. A generation that was materialised but scored worse and was
  not promoted records a promotion decision, which belongs in the
  lineage and experiment records; tagging it `-rejected` would
  duplicate that record in the storage layer. The hook can be added if
  a concrete recovery workflow needs it.
- **Cross-epoch parentage.** A proposal to parent a new epoch's `v0`
  commit to the previous epoch's promoted head was adjusted. The
  backend creates each epoch branch from `zicato-root`, and cross-epoch
  seeding is handled one layer up by the orchestrator's `v0_seed_from`
  marker, which hands `seed_generation` the predecessor's tree. Keeping
  cross-epoch lineage in the orchestrator, where the promotion decision
  lives, keeps the git backend's contract identical to the directory
  backend's, which the parity conformance suite requires.
- **Shell out to the `git` command-line interface.** `pygit2` is a
  C-extension binding to `libgit2`, which carries a build burden and an
  application binary interface surface, and `GitPython` shells out to
  the command-line interface itself. The generation-granularity
  operations are coarse — whole-tree commits, tags, worktrees, and a
  handful of plumbing commands — with no fine-grained object
  manipulation an in-process library would help with. Shelling out adds
  no dependency, and every state change is a command an operator can
  reproduce by hand.
- **Explicit path and materialization operations.** `snapshot_path`
  returns the would-be worktree location without I/O.
  `materialize_snapshot` checks out the generation tag and returns a
  usable tree.
- **Commit identity.** A fixed `zicato <zicato@localhost>` identity. The
  repository is private and single-writer, so the committer is never a
  person. GPG signing is disabled, since it could only ever fail.

### 7.4 The dashboard read surface

`list_tree` and `read_file` are backend-neutral source reads. The git backend
serves them straight from the object store (`git ls-tree`, `git show`) without
a worktree checkout; the directory backend walks the snapshot directory.
Patch views read `experiment.json` and per-patch records through
`StorageBackend`, independently of the source backend.

`diff_generations` is rendered rather than delegated. Each backend reads the
two generations' whole source trees — the git backend with one `git archive`
per tree, straight from the object store; the directory backend by walking the
snapshot — and both hand the pair to `render_source_diff`
(`zicato.epoch.genstore`), which renders the unified-diff text. The git backend
does not shell out to `git diff` for this, because git's output carries blob
hashes, file modes, and rename detection the directory backend cannot
reproduce. The proposer puts this text in its prompt, and the epoch contract
hash does not fold the source backend, so a backend-dependent rendering would
make the proposer's input depend on how a workspace stores its source.
`render_source_diff` states the format;
`tests/test_genstore_conformance.py` pins it identical across backends.

### 7.5 Workspace format

Existing workspaces without `generation_source_backend` are outside the
supported format. Configure a fresh workspace explicitly rather than inferring
or migrating a source backend from its contents.

### 7.6 Parity

`GitGenerationStore` is held to the directory backend's observable
contract byte for byte: `tests/test_genstore_conformance.py`
parametrises every protocol test over both backends. Git-specific
behaviour — the mapping from domain concepts to git constructs, blob
deduplication, the commit metadata block, worktree materialisation, and
explicit configuration selection — is pinned by
`tests/test_git_genstore.py`.

## 8. Workspace compatibility

Record storage routes through `StorageBackend` without changing any
canonical record path. The generation source-backend key is a
workspace-format break, made on purpose:

- Routing `epoch/` records through `StorageBackend` (§5.1) is internal.
  `experiment.json`, `lineage.json`, `config.json`, the journal, and
  the per-patch files land at the same paths with the same content, and
  every write is atomic.
- A new workspace carries an explicit `generation_source_backend` value.
- A workspace missing that key is refused. zicato neither infers a
  source backend nor migrates a directory-backed workspace to git.
- The index (§5.3) is derived and disposable, so there is nothing to
  migrate.

There is no migration utility for the inline `patches: [...]` array
form of `experiment.json`. The `read_experiment` reader accepts both
on-disk shapes transparently, so no operator needs one. Canonical JSON
records — `experiment.json`, the epoch `config.json`, and
`lineage.json` — carry an explicit `format_version: 1` stamp at write.
A reader treats an absent stamp as version 1, so a record written
without one still loads, and refuses a newer incompatible version with
a clear error. No reader sniffs a record's shape to decide how to
parse it.


## 9. Cross-references

| Topic | Document |
|---|---|
| `experiment.json`, per-generation directories, journals, epochs | [EPOCHS-AND-JOURNALING.md](EPOCHS-AND-JOURNALING.md) |
| The `.zicato/runtime/` layer — runtime state, the other `StorageBackend` consumer | [RUNTIME.md](RUNTIME.md) |
| Per-run event capture — `events.jsonl`, goldfive's `JSONLPersistenceSink` | [TELEMETRY.md](TELEMETRY.md) |
| The `.zicato/index.db` analytical index — schema, dual-write discipline, `reindex` | [ANALYTICAL-INDEX.md](ANALYTICAL-INDEX.md) |
| Subprocess-isolated tournament runs (why the store of record is files-canonical) | [ROBUSTNESS.md](ROBUSTNESS.md) |
| `MutationPoint.id` references that patches carry | [MUTATION-SURFACE.md](MUTATION-SURFACE.md) |
| CLI surface, including `zicato repair index` | [CLI.md](CLI.md) |
| Why the canonical layer is filesystem-native | [RATIONALE.md](RATIONALE.md) §7 |
| The generalized tournament record for configurable structures (§5.4) | [TOURNAMENT-DATA-MODEL.md](TOURNAMENT-DATA-MODEL.md) |
| The rejected overlayfs/reflink/hardlink materialization alternatives §7 supersedes | [GENERATION-ISOLATION.md](GENERATION-ISOLATION.md) |
