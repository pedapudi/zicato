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

The `GenerationStore` protocol that resolves that fork is shipped, and
**both** backends behind it are shipped: the directory-snapshot
backend (the default and always-available fallback) and the
git-backed backend (`GitGenerationStore`, selected by config — §7).
The directory backend remains the default. What is *not* yet
shipped — and is correctly marked as roadmap below — is the operator
CLI surface over the git store (`zicato repo` / `log` / `diff` /
`show` / `bisect` / `blame`) and the `zicato workspace migrate-to-git`
converter for an existing directory-backed workspace (§7.5). The git
*backend* is usable today from a fresh `storage_backend: "git"`
workspace; the git *CLI commands* are the deferred follow-up.

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
| **3. Generation source trees** | **Directory snapshots (default) or git — selected by config.** Behind the `GenerationStore` protocol (§5) either way. | The data is intrinsically file-shaped. The directory backend is a full `copytree` per generation — correct, simple, the default. The git backend (§7) adds blob dedup across generations and `diff`/`log`/`blame`/`bisect` for free; it is a *second* `GenerationStore` implementation behind the same protocol, not a different design. Both keep the source tree code-only via the shared artifact-exclusion policy (`snapshot_scope`, §5.2.1). |
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

Four concrete changes ship: the `epoch/` migration onto
`StorageBackend` (§5.1), the `GenerationStore` protocol with **both**
backends behind it — directory and git (§5.2), and the continuous
indexing design (§5.3). The git backend (§7) ships as the second
`GenerationStore`; what remains a separate, later effort is the
operator CLI over the git store and the `migrate-to-git` converter
(§7.5).

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

### 5.2 `GenerationStore` protocol; both backends behind it

`zicato.epoch.genstore` defines the `GenerationStore` protocol — the
generation-level seam from §4. Two implementations now ship:
`DirectoryGenerationStore` (the directory-snapshot mechanism, the
default) and `GitGenerationStore` (the git backend — §7, no longer
roadmap-only). The protocol:

```python
class GenerationStore(Protocol):
    def snapshot_root(self, epoch_id, generation_id) -> Path: ...
    def has_generation(self, epoch_id, generation_id) -> bool: ...
    def list_generations(self, epoch_id) -> list[str]: ...
    def seed_generation(self, epoch_id, generation_id, sources) -> Path: ...
    def derive_generation(self, epoch_id, parent_generation_id,
                          child_generation_id, patches) -> Path: ...
    # read surface — the dashboard file-tree / file-browser API
    def list_tree(self, epoch_id, generation_id) -> list[TreeEntry]: ...
    def read_file(self, epoch_id, generation_id, rel_path) -> bytes: ...
    def list_patches(self, epoch_id, generation_id) -> PatchRecord: ...
```

`derive_generation` is the generation-level transaction boundary the
record seam could not express: it derives the child from the parent's
tree, applies the patch set all-or-nothing (via
`zicato.mutation.applier.apply_patches`, whose pre-validation already
makes it atomic), and returns the child snapshot root — child
generation appears or it does not.

`snapshot_root` returns a real on-disk path, because the orchestrator
and the subprocess workers genuinely need a path (the worker `chdir`s
into a snapshot and loads the adapter from it). The directory backend
returns the snapshot directory directly; the git backend satisfies the
same contract by materialising a `git worktree` and returning *its*
path — which is exactly why §4 rejected forcing this through
`StorageBackend`'s `read_json`-shaped surface.

The `list_tree` / `read_file` / `list_patches` read surface is the
backend-neutral API the dashboard Files view consumes (§7.4). It is
*read-only* — generation mutation goes only through `seed_generation` /
`derive_generation`.

The orchestrator's snapshot helpers go through a `GenerationStore`;
`default_generation_store(workspace_root)` is the single construction
seam, selecting the backend off `config.json`'s `storage_backend` knob.

#### 5.2.1 The mutable surface is code-only — artifact exclusion

A generation source tree must be **code-only**. The presentation-agent
target writes its rendered webpage under an `output/` directory *inside
its own source directory*; without a filter, every `copytree` that
derives a child generation copies that `output/` forward, and it
compounds generation over generation until the disk is exhausted (a
real failure this design closes).

`zicato.epoch.snapshot_scope` is the single artifact-exclusion policy
both backends consult. It declares an artifact-name set (`output/`,
`__pycache__`, the lint/type caches, nested VCS / dependency
directories), an `is_artifact(path)` predicate, a
`copytree`-compatible `ignore` callable, and `.gitignore` line
generation. The directory backend passes the ignore callable to every
`shutil.copytree`; the git backend writes the `.gitignore` lines into
the generation repo so the same names never enter a commit.

Run output is *not* merely excluded from the copy — it is **routed
elsewhere**. The tournament runner creates a per-run scratch directory
outside every snapshot and exports it to the inner harness via the
`ZICATO_RUN_SCRATCH_DIR` environment variable; a target writes its run
output there. The adapter contract carries this: `HarnessAdapter`
declares `run_output_names` (extra artifact names) and
`mutable_subpaths(generation_root)` (the narrowed mutable surface the
mutation enumerator walks — support code stays in the snapshot for the
worker to execute, but is not part of the proposer's editable surface).

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

### 5.4 The generalized tournament record (configurable structures)

> **Status.** DESIGN (not yet implemented). Full spec:
> [TOURNAMENT-DATA-MODEL.md](TOURNAMENT-DATA-MODEL.md) §2.

Today a tournament's persistence assumes the king-of-the-hill gauntlet:
exactly one champion vs one challenger. Making the per-epoch structure
configurable (`gauntlet` / `single_elim` / `double_elim` / `swiss` /
`racing`) generalizes the persisted record **additively**, so the
gauntlet shape stays byte-identical and old workspaces keep loading.
The generalization touches three records and adds **zero new storage
mechanism** — it rides entirely on the existing seams (§5.1–§5.2).

**(a) The live runtime record** — `runtime/active_tournament.json`
(`ActiveTournament`), one JSON record via `StorageBackend`. It gains a
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
side)` key; `side` **widens** from `{"parent","child"}` to an opaque
competitor key (a generation id) for non-gauntlet structures, and gains
a `match_id` (default `""`). `update_tournament_entry`'s signature is
unchanged. `base.py` / `files.py` / `memory.py` are **untouched** — the
new fields are just more JSON in the same `to_dict` / `from_dict`.

**(b) The settled per-generation outcome** — `experiment.json`'s
`outcome` block (`OutcomeRecord`), persisted via `epoch/_storage.py`. It
gains `structure`, `final_rank`, `eliminated_in_round`, and a
per-generation `match_record`. `write_experiment` /
`_outcome_from_dict` read/write them; no new key helper, no new file.

**(c) The settled index row** — the SQLite `tournaments` table. It gains
five ADDITIVE `TEXT` columns (`structure`, `structure_params_json`,
`competitors_json`, `rounds_json`, `standings_json`) as a **v3 column
add**, exactly the v2 pattern (`_V2_ADDED_COLUMNS`). The existing
per-matchup columns stay and describe the **crowning** match for every
structure, so a gauntlet-only reader still gets a coherent answer. The
index is derived and rebuildable, so the migration is "drop + re-derive"
on the `reindex` path and an incremental column-add otherwise — the same
self-healing dual-write ordering rule (§5.3) applies.

**Back-compat.** Every new field defaults to the gauntlet
interpretation; the only stateful store (the index) is rebuildable. No
migration tool is needed — a gauntlet workspace written before the
feature loads and renders unchanged.

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
   is consumed by the Rust supervisor (`crates/supervisor/src/index_db.rs`,
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

## 7. The git-backed generation store (`GitGenerationStore`)

The git backend for kind 3 (generation source trees) **ships**, in
`zicato.epoch.git_genstore`, as a drop-in second `GenerationStore`
implementation. `DirectoryGenerationStore` stays the default and the
always-available fallback; the git backend is selected off
`config.json`'s `storage_backend: "git"` knob, resolved at one seam,
`default_generation_store`.

### 7.1 Why git, and the payoff

- **Blob dedup.** A prompt module unchanged across 20 generations is
  *one* git blob, referenced by 20 commits — not 20 `copytree` copies.
  For long lineages (target 3, zicato optimising zicato, can run 50+
  generations) the directory backend's disk cost is the motivating
  problem; git's content-addressed object store removes it.
- **Native tooling.** `git diff`, `git log`, `git blame`, `git bisect`
  on `{workspace}/repo/` answer the common operator questions for free.
- **Cheap parallel checkouts.** `git worktree` per tournament run
  replaces the directory backend's per-run `copytree` ephemeral
  snapshot — git shares the object store; only the working files are
  materialised.

### 7.2 The domain → git mapping

| zicato concept | git construct | Why |
|---|---|---|
| **Workspace** | One repository, `{workspace_root}/repo/` | One repo, not one-per-epoch: cross-epoch `diff`/`log` and cross-epoch blob dedup both want a single object store. Private to zicato; the user's outer repo is untouched. |
| **Epoch** | A branch, `epoch/{epoch_id}` | An epoch's generations are a commit chain on its branch. A branch is the natural "sequence of related commits" unit. |
| **Generation** | A commit, tagged `epoch/{epoch_id}/{generation_id}` | The commit is the immutable tree; the tag is the stable handle. The branch head moves as generations are appended; the tags do not. |
| **Generation lineage** | The commit DAG | A child generation commit parents its parent generation — `git log` *is* the lineage. |
| **Patch set** | The deriving commit's message, after a `---zicato-meta---` sentinel, as a JSON block | Patch metadata travels *with* the commit — visible in plain `git log`, transported by any fetch/push, parsed back by `list_patches`. |
| **Parallel tournament run** | A `git worktree` checked out at the generation tag | Isolated, cheap per-run checkout; a runtime write inside it never touches the commit. Replaces `_make_ephemeral_snapshot`'s `copytree`. |

A repo-root orphan branch `zicato-root` carries only the
artifact-exclusion `.gitignore`; every epoch branch is created from it,
so the `.gitignore` is shared and cross-epoch `diff` has a common base.

### 7.3 Design-review record — decisions and rejected alternatives

The roadmap's earlier sketch proposed *epoch ref-namespaces* and an
`epoch/{id}/main` branch name. The shipped design adjusts it:

- **Branch name `epoch/{id}`, not `epoch/{id}/main`.** The `/main`
  suffix bought nothing — an epoch has exactly one lineage branch, so
  the extra path component was noise. *Rejected.*
- **Generation tag `epoch/{id}/{gen}`.** Kept. A tag (not a branch) is
  correct: a generation is immutable once created, and tags are exactly
  git's immutable-handle construct.
- **Rejected-generation tags (`…-rejected`).** *Deferred.* The current
  `derive_generation` contract is all-or-nothing — a rejected *attempt*
  (a failed patch apply) never produces a commit at all, so there is
  nothing to tag. A rejected-but-materialised generation (a child that
  scored worse and was not promoted) is a *promotion* decision recorded
  in lineage/experiment records, not a storage-layer concern; tagging
  it `-rejected` would duplicate that record in the wrong layer. The
  hook can be added if a concrete recovery workflow needs it.
- **Cross-epoch parentage.** The roadmap wanted a new epoch's `v0`
  parented to the previous epoch's promoted head. The shipped backend
  creates each epoch branch from `zicato-root`; cross-epoch seeding is
  still handled one layer up by the orchestrator's `v0_seed_from`
  marker, which hands `seed_generation` the predecessor's tree. Keeping
  cross-epoch lineage in the orchestrator (where the promotion decision
  lives) rather than the storage backend keeps the backend's contract
  identical to the directory backend's — important for the parity
  conformance suite. *Adjusted from the roadmap; recorded here.*
- **Shell out to the `git` CLI, no new dependency.** `pygit2` is a
  C-extension binding to `libgit2` (build burden, ABI surface);
  `GitPython` shells out to the CLI itself. The generation-granularity
  operations are coarse — whole-tree commits, tags, worktrees, a
  handful of plumbing commands — with no fine-grained object
  manipulation that an in-process library would help. Shelling out adds
  no dependency and every state change is a command an operator can
  reproduce by hand. *Decision: subprocess to the `git` CLI.*
- **Worktree as `snapshot_root`.** `snapshot_root` materialises a
  worktree on demand and returns its path. An unmaterialised coordinate
  returns the would-be path without creating anything, matching the
  directory backend's "pure coordinate → path" contract for the
  not-yet-existing case.
- **Commit identity.** A fixed `zicato <zicato@localhost>` identity —
  the repo is private and single-writer, so the committer is never a
  person. GPG signing is disabled (it could only ever fail).

### 7.4 The dashboard read surface

`list_tree` / `read_file` / `list_patches` are backend-neutral. The git
backend serves them straight from the object store (`git ls-tree`,
`git show`, the commit metadata block) — *no worktree checkout* — so
the dashboard Files view browses any generation cheaply. The directory
backend walks the snapshot directory. The dashboard
(`zicato/dashboard/filetree.py`) consumes only the protocol, so the
Files view is identical for both backends.

### 7.5 Migration (still roadmap)

Migration from a directory-snapshot workspace to a git-backed one — a
one-shot `zicato workspace migrate-to-git` that imports each existing
`generations/vN/snapshot/` as a commit — remains a roadmap item. It is
not needed to *use* the git backend (a fresh workspace can be
`storage_backend: "git"` from `zicato init`); it is only needed to
*convert an existing* directory-backed workspace. That converter is its
own follow-up.

### 7.6 Parity

`GitGenerationStore` is held to the directory backend's exact
observable contract: `tests/test_genstore_conformance.py` parametrises
every protocol test over both backends. Git-specific behaviour (the
domain → git mapping, blob dedup, the commit metadata block, worktree
materialisation, config-knob selection) is pinned by
`tests/test_git_genstore.py`.

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
| The generalized tournament record for configurable structures (§5.4) | [TOURNAMENT-DATA-MODEL.md](TOURNAMENT-DATA-MODEL.md) |
