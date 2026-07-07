# 07 — Runtime & Durability

> **Covers.** How zicato persists everything it persists: the files-canonical /
> index-derived doctrine, the full store inventory, the atomic-write contract,
> the git-backed generation store, snapshot GC, the runtime state files the
> Rust supervisor reads, the workspace lock, crash-resume, the control-file
> protocol, the durable per-round event log (`RoundLog`), record format
> versioning, and the infra-outage circuit + per-round token ledger.
>
> **Prerequisites.** 01-orientation.md (workspace layout, what an epoch /
> generation / round is), 02-architecture.md (orchestrator vs workers vs
> supervisor vs dashboard as separate OS processes), 03-contract-and-epochs.md
> (the contract hash, `experiment.json`, the journal). The supervisor's side
> of every file described here is 08-supervisor.md; the tests that pin these
> contracts are 11-testing.md.
>
> **Invariants introduced in this chapter.** Each is load-bearing; violating
> one is a data-corruption bug, not a style issue.
>
> | ID | Invariant |
> |----|-----------|
> | D1 | Files are canonical; the SQLite index is a derived, lag-only projection. Nothing may exist only in the index. |
> | D2 | Every dual-write into the index is best-effort: any failure is logged at `debug` and swallowed; a round/run is never failed by an index write. |
> | D3 | Every mutable JSON record write goes through the atomic-write helpers (`tmp` → `fsync` → `os.replace` → parent-dir `fsync`). Nobody bypasses them. |
> | D4 | Torn-tail tolerance exists ONLY for append-only JSONL logs. An unparseable *interior* line raises; a mutable JSON record is never partially readable. |
> | D5 | A generation store's unit of work is transactional: a child tree appears in full or not at all (`derive_generation` is all-or-nothing). |
> | D6 | Every per-run ephemeral checkout lives under a `ztw-snap-*` mkdtemp parent in the OS temp dir — the exact shape the supervisor's crash-reaper is allowed to delete. |
> | D7 | GC prunes source TREES only, never records; promoted / in-flight / lineage-unknown generations and the seed `v0` are never pruned. |
> | D8 | A generation is journaled and appended to `lineage.json` only AFTER its outcome is decided. An un-outcomed generation has no lineage/journal entry, so discarding it on resume cannot corrupt either. |
> | D9 | Process identity is `(pid, start_time)`, never a bare pid. Lock stealing and worker signalling both require the identity check. |
> | D10 | Exactly one process appends to a given event log (single-writer `seq`); consumed control commands are claimed exactly once and always archived, never silently deleted. |
> | D11 | RoundLog emission is best-effort: an emission failure must never fail a round. The canonical stores stay authoritative. |
> | D12 | A reader refuses a canonical record stamped with a `format_version` it cannot read; it never silently misinterprets a newer shape. An index database with a newer `user_version` is never re-stamped down. |

---

## 7.1 The persistence doctrine: files canonical, index derived

zicato's storage design is CQRS-shaped without the ceremony: the **write
side** is plain files under the workspace (`.zicato/`), and the **read side**
for cross-run analytics is a SQLite projection (`index.db`) that can always be
thrown away and rebuilt. The doctrine is stated at the top of the seam module:

```python
* **The SQLite index is NOT a backend.** :mod:`zicato.index` is the
  *derived read side* — rebuildable at any time from the canonical
  records. It does not implement this protocol and must not be routed
  through it; conflating the store-of-record with its derived index
  would couple two things that fail, scale, and evolve independently.
```
— `src/zicato/storage/base.py` (module docstring)

And restated on the index build side:

```python
Source-of-truth rule
--------------------
Everything ingested is *derived*. The files under ``.zicato/`` are
canonical; the index holds nothing that is not already on disk. That is
why :func:`rebuild_index` can drop and recreate the database with no
loss — it is purely a re-projection of the files.
```
— `src/zicato/index/ingest.py` (module docstring)

Three consequences an agent extending zicato must internalize:

1. **Every fact must land in a file first.** If your feature produces a new
   datum the dashboard or the analytics need, it gets a canonical file record
   (JSON via `StorageBackend`, or an append onto an existing JSONL log). The
   index row, if any, is a projection you add to `zicato/index/ingest.py`
   *afterwards* and derive from that file.
2. **The index may lag and may be missing.** Readers of `index.db` (the
   Python query layer, the Rust supervisor) must degrade to an empty/partial
   answer when the database is absent, stale, or mid-rebuild. See
   08-supervisor.md §"The read-only SQLite discipline" for the Rust side's
   contract.
3. **`zicato reindex` is the recovery story.** It is a drop-and-rebuild:
   delete `index.db`, walk every epoch / generation / run under `.zicato/`,
   re-derive every row. Because ingestion is upsert-idempotent (every write
   is `INSERT ... ON CONFLICT DO UPDATE` on the natural primary key), running
   it repeatedly is a no-op beyond the file drop.

### 7.1.1 The dual-write pattern — and its swallow-everything except clause

The live loop keeps the index current with **best-effort dual-writes** at two
sites, and both follow the same pattern. This is the canonical example; copy
it verbatim if you ever add a third:

```python
    try:
        from zicato.index.ingest import ingest_run  # noqa: PLC0415

        ingest_run(
            workspace_root,
            _index_db_path(workspace_root),
            epoch_id,
            generation_id,
            entry_id,
        )
    except ImportError:
        # The index sibling is not installed in this environment — the
        # loop runs fine without the live index.
        log.debug("zicato.index.ingest unavailable; skipping live index dual-write")
    except Exception as exc:  # noqa: BLE001 — index write is best-effort
        log.debug(
            "live index ingest_run skipped for %s/%s/%s: %s",
            epoch_id,
            generation_id,
            entry_id,
            exc,
        )
```
— `src/zicato/tournament/worker_transport.py`, `_ingest_run_into_index`

The two live sites are:

| Site | Symbol | Fires when | Rows touched |
|------|--------|-----------|--------------|
| Run settles | `zicato.tournament.worker_transport._ingest_run_into_index` (called by `zicato.tournament.runner`) | the run's `loss.json` has just been written | `runs`, `loss_profiles`, `metric_counts` |
| Experiment written / outcome updated | `zicato.orchestrator._ingest_experiment_into_index` | `experiment.json` is written, and again when its outcome lands | `experiments`, `patches`, `tournaments` |

Read the except clauses carefully — this is the doctrine's teeth:

- **The import is lazy** (`from zicato.index.ingest import ...` inside the
  `try`). A workspace without the index module still evolves.
- **`except Exception` swallows everything** — schema mismatch, disk full,
  a locked database, a bug in your new ingest column. All of it is logged at
  `debug` and dropped, because the canonical file was already written and
  `zicato reindex` can always reconstruct the row (invariant D2).
- The one thing the pattern does *not* protect is the index itself lying:
  that is why the *reader* side has its own version tripwires (§7.11 and
  08-supervisor.md).

> ⛔ NEVER let an index write failure propagate into the evolve loop, a
> tournament runner, or a worker. If you add an ingest call and it can raise
> past its `try`, you have made an optional projection load-bearing —
> invariant D2 is violated and a corrupt `index.db` can now halt evolution.

> ⛔ NEVER write a datum into `index.db` that has no canonical file source.
> `rebuild_index` walks files; a row only you produced live will silently
> vanish on the next `zicato reindex`, and the vanish will look like a bug in
> someone else's code.

> ✅ ALWAYS make new ingest writes upsert-idempotent (`ON CONFLICT DO
> UPDATE` keyed on the natural primary key). The live path and the rebuild
> path both hit the same rows; a bare `INSERT` breaks the second writer.

There is exactly ONE deliberate non-swallowed failure inside the index write
path, and it is a refusal, not a crash of the loop (the outer dual-write
`except Exception` still catches it and degrades):

```python
    current = read_schema_version(conn)
    if current > SCHEMA_VERSION:
        raise IndexSchemaNewerError(
            f"index database schema is v{current}, newer than this build's "
            f"v{SCHEMA_VERSION}; refusing to re-stamp it down. Upgrade "
            "zicato, or delete the index database and run `zicato reindex` "
            "(the index is derived — a rebuild loses nothing)."
        )
```
— `src/zicato/index/schema.py`, `apply_schema`

An older writer must never re-stamp a newer database DOWN (invariant D12).
The recovery is always cheap because of D1: delete the file, `zicato
reindex`.

### 7.1.2 `zicato reindex` — the drop-and-rebuild

```
$ zicato reindex
```

Backed by `zicato.index.ingest.rebuild_index` (see
`src/zicato/cli/commands/reindex.py`): drops `index.db`, applies the schema
(`zicato.index.schema.apply_schema`, which stamps `SCHEMA_VERSION` into both
`PRAGMA user_version` and the one-row `schema_meta` table), then walks the
workspace re-deriving every row through zicato's own canonical readers
(`load_lineage`, `read_experiment`, `read_loss_profile`, `iter_epochs`) — the
index never re-implements a parse a canonical module already owns.

`SCHEMA_VERSION` currently equals `10` (`src/zicato/index/schema.py`); the
Rust supervisor pins the same number as `EXPECTED_SCHEMA_VERSION` in
`crates/supervisor/src/index_db.rs`, with a test on each side that fails if
they drift. If you change the index schema, you are changing a **two-language
contract** — see 08-supervisor.md §"The read-only SQLite discipline" and the
REINDEX-DUMP parity gate in 11-testing.md §"Parity gates one by one".

---

## 7.2 The store inventory

`docs/design/STORAGE.md` §1 defines five data kinds; the running system has
grown a few more durable artifacts on top (round logs, the supervisor ledger,
control audit logs). This table is the complete inventory an agent needs when
deciding *where a new datum belongs* and *what happens to it in a crash*.

| Store | Location (under `.zicato/` unless noted) | Canonical or derived | Writer | Readers | Crash semantics |
|---|---|---|---|---|---|
| Generation source trees | git backend (default): commits in `repo/`, tags `epoch/{epoch}/{gen}`, materialised worktrees in `repo-worktrees/`; directory backend: `epochs/{e}/generations/{g}/snapshot/` | Canonical | Orchestrator via `GenerationStore` (`seed_generation` / `derive_generation`) | Workers (via `checkout_ephemeral`), dashboard file browser, diff-containment scans | Transactional: child appears in full or not at all (D5). A half-derived attempt is cleared on retry; resume discards an un-outcomed generation it cannot vouch for. |
| Lineage / experiments / journal | `lineage.json`, `epochs/{e}/generations/{g}/experiment.json` + `patches/*.json`, `epochs/{e}/journal.md`, per-epoch `config.json` / `scoring.json` / `board.jsonl` / `brief.md`, cached `gen_score.json` | Canonical (the typed evolutionary record) | Orchestrator only, via `zicato.epoch.journal` / `lineage` / `lifecycle` routed through `StorageBackend` | Everything: resume, index ingest, dashboard, analyzer, GC's safety floor | Atomic per record (D3). `lineage.json` / journal are appended only post-outcome (D8), so an interrupted round leaves them untouched. |
| Per-run records | `epochs/{e}/generations/{g}/runs/{entry}/loss.json` | Canonical — and the board-unit cache: keyed `(generation, entry, replicate)` | The run's worker subprocess | Tournament runner (cache hits), reducer, index ingest, resume (`_has_any_loss`) | Atomic write; a completed unit survives any crash and is a permanent cache HIT for resume. |
| Telemetry | `epochs/.../runs/{entry}/events.jsonl` (one per run) | Canonical event capture (goldfive's format) | goldfive `JSONLPersistenceSink` inside the worker | Reducer (once), dashboard log panel, harmonograf | Append-only; the reducer tolerates a torn tail (D4). |
| Runtime state | `runtime/heartbeat.json`, `runtime/lock.json`, `runtime/active_runs/*.json`, `runtime/active_tournament.events.jsonl`, `runtime/progress.events.jsonl`, `runtime/dashboard.json`, `runtime/inconclusive/*.json` | Canonical but EPHEMERAL — describes the live process, not history | Orchestrator + each run's worker (own file each) | Rust supervisor, dashboard, `prepare_resume` (which deletes it) | Discarded wholesale on restart by `clear_runtime_state`; the supervisor treats absence as "never booted". |
| Control protocol | `runtime/control/` (flags, targeted files, payload file), `runtime/control_log/` (audit sidecars) | Canonical commands + canonical audit trail | Dashboard / CLI / operator `touch` write; orchestrator consumes; supervisor writes `kill_requests/` markers on POST | Orchestrator safe-points; supervisor's kill loop | Claim-once move semantics; consume writes the audit log BEFORE deleting the source, so a crash mid-consume duplicates observably rather than losing (D10). |
| RoundLog | `epochs/{e}/rounds/{n}/round_log.jsonl` | Canonical durable trace of one round's decisions (but emission is best-effort — D11) | Orchestrator's `_RoundLogEmitter` (single writer) | `fold_round_record` consumers: dashboard round timeline, tests, post-hoc analysis | Append-only, torn-tail tolerant (D4); survives resume (it lives under `epochs/`, never under `runtime/`). |
| SQLite index | `index.db` | **Derived** — the only non-canonical store | Live dual-writes + `zicato reindex` | Python query layer (`zicato.query`), Rust supervisor (read-only) | Disposable. Delete + `zicato reindex` is always safe. |
| Supervisor audit ledger | `<--ledger-dir>/audit_ledger.jsonl` — deliberately OUTSIDE the orchestrator's trees | Canonical, supervisor-owned (the orchestrator must not be able to rewrite it) | Rust supervisor only (`AuditLedger::append`) | `/statusz`, `/api/audit/verify`, operators | Hash-chained; torn tail truncated at open; fsync per append. See 08-supervisor.md §"The hash-chained ledger". |
| Ephemeral checkouts | `${TMPDIR}/ztw-snap-{run_id}-*/` | Neither — throwaway working copies | `GenerationStore.checkout_ephemeral` | The one worker that mounted it | Discarded on clean run-end; orphans reaped by the supervisor's prefix-guarded crash-GC (D6). |

Two placement rules fall out of the table:

> ✅ ALWAYS put durable per-round / per-generation facts under `epochs/`
> (they survive resume) and live process state under `runtime/` (it is
> deleted by `clear_runtime_state` on every restart). Choosing the wrong tree
> is the difference between "record" and "will silently vanish on resume".

> ⚠️ TRAP: `active_tournament.json` still has a path helper
> (`zicato.runtime.paths.active_tournament_path`) but is a LEGACY snapshot —
> the live producer writes the event log
> (`active_tournament.events.jsonl`) and readers fold it
> (`zicato.runtime.tournament_log.fold_active_tournament`), falling back to
> the snapshot only when no log exists. New code must never write the
> snapshot file.

---

## 7.3 The atomic-write contract

There is exactly one definition of "atomic file write" in zicato, and it
lives in `src/zicato/storage/_atomic.py`. Its public face is re-exported by
`zicato.storage` (`atomic_write_json`, `atomic_write_text`, `atomic_claim`,
`read_json`); a lint ban (`TID251`, see 11-testing.md §"Import contracts")
keeps everyone off the private module path.

The pattern, verbatim from the module that owns it:

```python
The pattern is:

1. Ensure the parent directory exists.
2. Write the full payload to ``path.with_suffix(path.suffix + ".tmp")``.
3. ``fsync`` the temporary file (durability of contents).
4. :func:`os.replace` it onto the final path (atomic on POSIX and
   Windows for files on the same filesystem).
5. ``fsync`` the parent DIRECTORY (durability of the rename itself —
   without it a power loss can forget the directory entry even though
   the file's blocks reached disk, leaving the OLD file, or none).
```
— `src/zicato/storage/_atomic.py` (module docstring)

Step 5 is the part naive reimplementations forget: `os.replace` mutates the
*directory*, and on POSIX the directory entry must itself be fsynced for the
rename to survive power loss. `_fsync_dir` is called unconditionally after
every rename (best-effort where the platform cannot open a directory fd).

The guarantee this buys, and which every reader in the system leans on:

```python
The goal is a hard guarantee: no reader ever observes a half-written
file. A crash mid-write leaves the on-disk file either untouched (at the
previous content) or fully replaced with the new content; never a
truncated mix.
```
— `src/zicato/storage/_atomic.py` (module docstring)

### 7.3.1 Who is allowed to bypass it: NOBODY

Every mutable JSON record — heartbeat, active runs, lock, experiment,
lineage, epoch config, control files — is written through
`StorageBackend.write_json` / `write_text`, which the file backend
(`zicato.storage.files.FileStorageBackend`) implements on these helpers. The
read side enforces the contract by *refusing to be lenient*:

```python
    Does NOT swallow JSON-decode errors — a malformed state file is a
    real bug and propagating the :class:`json.JSONDecodeError` lets the
    caller log it loudly. The atomic-write discipline above is
    specifically designed so the on-disk file is never partial; if it
    IS partial something has bypassed the helpers and we want to know.
```
— `src/zicato/storage/_atomic.py`, `read_json`

> ⛔ NEVER write a state/record file with `path.write_text(json.dumps(...))`
> or `open(...).write(...)`. That was the historical bug class this module
> exists to close (`src/zicato/epoch/_storage.py` documents the migration:
> "a crash mid-write could leave a truncated `experiment.json` /
> `lineage.json` / `config.json`"). If you find yourself needing a new
> record, compose a key helper in the domain's `_storage.py` and call
> `backend.write_json`.

> ⚠️ TRAP: a *missing* file is a valid state everywhere (`read_json` returns
> `None`; every consumer treats "not yet written" as legitimate). A
> *malformed* file must raise. Do not "fix" a `JSONDecodeError` in a reader
> by returning `None` — you would be masking a writer that bypassed the seam.

### 7.3.2 `atomic_claim` — the claim-once primitive

`atomic_claim(src, dst)` is `os.rename` used as a synchronisation point:
exactly one racing caller wins the rename; every loser gets
`FileNotFoundError` and is told `False`. It is the mechanism behind
`zicato.runtime.channel.CommandQueue.claim()` (each queued command fires for
one and only one consumer) and its semantics include a durability detail that
matters:

```python
    After a successful claim BOTH parent directories are fsynced: the
    destination's so the claim survives power loss, and the source's so
    the removal does too — otherwise a crash could resurrect the source
    entry and let an already-claimed command fire twice.
```
— `src/zicato/storage/_atomic.py`, `atomic_claim`

### 7.3.3 Torn-tail tolerance is for APPEND-ONLY logs only

Invariant D4. Append-only JSONL streams cannot use rename-replace (they grow
in place), so their crash mode is different: a crash mid-append can leave one
torn final line, and readers of *exactly these files* tolerate it. The
complete list of torn-tail-tolerant stores:

| Log | Reader that tolerates the tail | Writer repair |
|---|---|---|
| Per-run `events.jsonl` (telemetry) | the reducer skips an unparseable last line | none (one writer, then read-once) |
| `epochs/{e}/rounds/{n}/round_log.jsonl` | `RoundLog.read` skips an unparseable LAST line only | `RoundLog.append` truncates a torn tail before appending (`_truncate_torn_tail`) |
| `runtime/active_tournament.events.jsonl` + `runtime/progress.events.jsonl` | the Python fold and the Rust supervisor's fold (which counts torn lines into `FoldDiagnostics` for `/statusz`) | cleared wholesale on resume |
| Supervisor `audit_ledger.jsonl` | `verify_chain` after `repair_torn_tail` | `AuditLedger::open` truncates the torn tail before verifying/chaining |

And the half of the invariant that keeps this from becoming general
sloppiness — an interior tear is never tolerated:

```python
        An unparseable LAST line is skipped (a crash mid-append); an
        unparseable INTERIOR line raises :class:`ValueError` — under the
        append-only single-writer invariant only the tail can be torn, so
        interior corruption means something bypassed the writer and must
        surface rather than silently dropping history.
```
— `src/zicato/epoch/round_log.py`, `RoundLog.read`

> ⛔ NEVER extend torn-tail tolerance to a mutable JSON record, and never
> extend it to interior lines of a log. The tolerance is a theorem about
> append-only single-writer files ("only the tail can be torn"), not a
> general error-handling posture.

---

## 7.4 The generation store — and the git backend in depth

Generation source trees are the one data kind that is *not* record-shaped, so
they get their own seam: the `GenerationStore` protocol
(`src/zicato/epoch/genstore.py`), a **peer abstraction at the domain layer**,
deliberately not a `StorageBackend` subtype (the reasoning is
`docs/design/STORAGE.md` §4 and the module docstring). Two backends:

| Backend | Selected by | A generation is | Derive cost | Per-run checkout |
|---|---|---|---|---|
| `GitGenerationStore` (`src/zicato/epoch/git_genstore.py`) | `storage_backend: "git"` in workspace `config.json` — **the default**, including for a missing/blank knob (`DEFAULT_STORAGE_BACKEND = "git"`) | a commit on branch `epoch/{epoch_id}`, tagged `epoch/{epoch_id}/{generation_id}` | checkout parent + apply patches + commit (blobs dedup across the lineage) | detached `git worktree` in a `ztw-snap-*` temp parent |
| `DirectoryGenerationStore` (`src/zicato/epoch/genstore.py`) | `storage_backend: "directory"` | `generations/{id}/snapshot/` directory | full `copytree` of the parent + patch apply | artifact-filtered `copytree` (`copy_checkout_ephemeral`) |

The single construction seam is `default_generation_store(workspace_root)` —
never construct a backend directly in loop code; everything flows through
that factory so the knob works.

The protocol's shape is small on purpose: coordinate queries
(`snapshot_root`, `has_generation`, `list_generations`), two transactions
(`seed_generation`, `derive_generation`), the per-run checkout
(`checkout_ephemeral`), and a read-only dashboard surface (`list_tree`,
`read_file`, `list_patches`). Anything record-shaped (`experiment.json`,
`gen_score.json`) is explicitly NOT here.

The cross-backend contract is pinned by a conformance suite: every test in
`tests/test_genstore_conformance.py` runs against BOTH backends. If you touch
either backend, that suite is your first gate (see 11-testing.md §"The
genstore conformance suite + session-template fixtures").

### 7.4.1 The domain → git mapping

```python
* **Workspace** → one git repository (``{workspace_root}/repo/``). One
  repo, not one-per-epoch: cross-epoch ``diff``/``log`` and cross-epoch
  blob dedup both want a single object store.
* **Epoch** → a branch, ``epoch/{epoch_id}``. An epoch's generations are
  a commit chain on its branch.
* **Generation** → a commit, tagged ``epoch/{epoch_id}/{generation_id}``
  (e.g. ``epoch/2026-05-18_e1/v3``). The tag is the stable handle; the
  branch head moves as generations are appended.
* **Patch metadata** → the deriving commit's message, after a
  ``---zicato-meta---`` sentinel line, as a JSON block. Visible in plain
  ``git log``, parsed back by :meth:`list_patches`.
```
— `src/zicato/epoch/git_genstore.py` (module docstring)

Facts an agent needs before touching this module:

- **The repo is private to zicato.** It lives at `{workspace_root}/repo/`
  *inside* `.zicato/`; the user's outer repository is never touched. It has a
  fixed committer identity (`_GIT_AUTHOR_NAME = "zicato"`,
  `zicato@localhost`) — deliberately not a person and, per the repo-wide
  rule, never a vendor name.
- **It shells out to the `git` CLI** (no `pygit2`/`GitPython` dependency).
  Every mutation is a command an operator can replay by hand against
  `repo/`; failures surface as `GitCommandError` carrying argv + exit code +
  stderr.
- **Commits may be empty.** `_commit` passes `--allow-empty` because a
  derived child can legitimately be byte-identical to its parent (a patch
  that sets a value to what it already is). Every generation is a commit even
  when its tree did not change — the lineage IS the commit chain.
- **The meta sentinel is a parsing contract.** `_format_commit_message`
  writes the human subject (`zicato: {epoch}/{gen}`), a blank line, the
  `---zicato-meta---` sentinel, then a JSON object with `epoch_id`,
  `generation_id`, `parent_generation_id`, and the full `patches` list.
  `_read_commit_meta` partitions on the sentinel and `json.loads` the rest.
  If you add a metadata field, add it to the JSON block — never invent a
  second sentinel or pack data into the subject line.
- **Artifact exclusion is belt-and-braces.** The repo root carries a
  `.gitignore` generated from `zicato.epoch.snapshot_scope.gitignore_lines`,
  AND every copy into the working tree is filtered through
  `_artifact_ignore` — run artifacts (`output/`, caches) must never reach a
  commit even transiently.

### 7.4.2 Worktree materialisation, and the stale-worktree lesson

`snapshot_root` under git is not pure path math (unlike the directory
backend): handing a worker a real tree requires a checkout, so
`_materialise_worktree` creates a detached worktree at the generation's tag
under `repo-worktrees/{epoch}/{gen}` and reuses it thereafter.

The trap this created — and the fix now baked into `derive_generation` — is
the **stale-worktree re-derive bug** (see 12-bug-casebook.md):

```python
        # A RE-derive of the same child id (a proposer retry after failed
        # post-apply validation, the best-of-N chosen-candidate re-derive, a
        # crash-resume re-validate) moves the tag to the fresh commit — but a
        # worktree materialised by an EARLIER attempt stays detached at the
        # old commit, so ``snapshot_root`` would hand back a stale tree that
        # no longer matches the commit just derived (the directory backend
        # clears + rebuilds the child tree instead, so only this backend
        # needs the refresh). Drop the stale checkout; ``snapshot_root``
        # below re-materialises it from the moved tag (its ``worktree add``
        # path prunes the orphaned registration first).
        stale_worktree = self._worktree_path(epoch_id, child_generation_id)
        if stale_worktree.is_dir():
            shutil.rmtree(stale_worktree, ignore_errors=True)
```
— `src/zicato/epoch/git_genstore.py`, `derive_generation`

The general lesson: **a tag is a moving handle; a worktree is a frozen
checkout.** Any code path that can move a generation tag (`_tag_generation`
uses `tag -f` precisely because retries move tags) must ask whether a
materialised worktree is now lying about that generation's content.

> ⚠️ TRAP: the directory backend never exhibits this class of bug (it clears
> and rebuilds the child tree on every derive), so a test that only runs the
> directory backend will be green while the git default is broken. This is
> exactly why the conformance suite parametrises over both backends — write
> your genstore tests there, not in a single-backend file.

### 7.4.3 `checkout_ephemeral` — per-run detached worktrees, and the prune-vs-add lock

A tournament worker is never pointed at the canonical tree: a stray runtime
write into it would accumulate across the whole lineage. Every run mounts a
throwaway checkout instead, with a contract shared by all backends
(`GenerationStore.checkout_ephemeral` docstring):

- the checkout lives under a fresh `ztw-snap-{run_id}-*` mkdtemp parent in
  the OS temp dir (`EPHEMERAL_SNAPSHOT_PREFIX = "ztw-snap-"`) — the exact
  shape the supervisor's crash-reaper GCs (invariant D6; the Rust twin is
  `SNAPSHOT_PREFIX` in `crates/supervisor/src/reap.rs`);
- `working_dir`'s basename equals the canonical `snapshot_root`'s basename,
  so `__file__`-derived paths inside the agent look identical either way;
- a sibling `run-scratch` dir (`EPHEMERAL_SCRATCH_DIRNAME`) is created for
  the `SCRATCH_DIR_ENV` contract — run output routed OUTSIDE the source tree;
- concurrent checkouts of the SAME generation are mutually isolated;
- `cleanup()` is idempotent, best-effort, and crash-safety does NOT depend on
  it (the reaper handles orphans).

Under git the checkout is a per-run `git worktree add --detach` immediately
followed by unlinking the worktree's `.git` pointer file and pruning the
registration — the run is left with a plain throwaway tree, no path back into
the private repo, and no cleanup that depends on git state. The per-run cost
was benchmarked at milliseconds and is 3–18× cheaper than the directory
backend's `copytree` (the full benchmark record lives in the
`checkout_ephemeral` docstring — read it before proposing a "faster"
alternative; `git archive`+tarfile was measured and rejected).

The concurrency hazard here is the **prune-vs-add race** (see
12-bug-casebook.md). git's own repo lock serialises individual commands, but
`worktree prune` overlapping a *sibling's* multi-command add window can
collect the half-registered admin entry:

```python
#: Per-repo locks serialising worktree ADMIN mutations (``worktree add`` /
#: ``worktree prune``) within this process. git's own repo lock serialises
#: the individual commands, but a PRUNE overlapping a concurrent ADD's
#: multi-command window can collect the sibling's half-registered admin
#: entry (observed: ``fatal: Invalid path .../.git/worktrees/<name>``).
#: Production checkouts already run sequentially on the orchestrator's
#: event-loop thread — and the workspace runtime lock guarantees a single
#: orchestrator per workspace — so a process-local lock is sufficient to
#: make the protocol's concurrent-checkout contract hold for threaded
#: callers too. Keyed by resolved repo path; the registry itself is tiny
#: (one entry per workspace this process touches).
_REPO_WORKTREE_LOCKS: dict[str, threading.Lock] = {}
```
— `src/zicato/epoch/git_genstore.py`

Every prune→add(→detach→prune) window is wrapped in
`_worktree_admin_lock(self._repo)` — in `_materialise_worktree` AND in
`checkout_ephemeral`.

> ⛔ NEVER call `self._git("worktree", ...)` outside the
> `_worktree_admin_lock` context. A single unlocked prune reintroduces the
> race for every concurrent checkout in the process.

> ✅ ALWAYS keep the `ztw-snap-` prefix and temp-dir placement if you touch
> any ephemeral-checkout code path, in ANY backend. Both properties are
> load-bearing for the supervisor's crash-GC prefix guard
> (`reap.rs::reapable_snapshot_root` refuses anything else) — see
> 08-supervisor.md §"Confirmed-dead-only reaping".

### 7.4.4 The derive-generation scratch flow

`derive_generation` under git cannot apply patches in place (the applier
refuses to overwrite an existing target), so it uses a scratch swap:

1. `git checkout epoch/{epoch}` + `git reset --hard <parent-tag>` — position
   the branch at the parent so the child commit parents the parent
   generation (the lineage is the commit DAG).
2. `apply_patches(source_root=<parent worktree>, target_root=<workspace>/.derive-scratch)`
   — the applier validates the whole batch first and raises `ValueError`
   without leaving a partial tree (this is what makes D5 hold).
3. `_replace_working_tree(scratch)` — wipe the repo working tree (preserving
   `.git`), copy the patched tree in (artifact-filtered), restore the
   `.gitignore`.
4. `git add -A`, `_commit(message-with-meta-block)`, `_tag_generation`
   (`tag -f`).
5. Drop any stale worktree for the child id (§7.4.2), then return
   `snapshot_root` (which re-materialises from the moved tag).

The `finally` block removes `.derive-scratch` regardless of outcome, so a
failed validation leaves neither a scratch tree nor a commit nor a tag —
all-or-nothing end to end.

### 7.4.5 `list_patches` and the journal fallback after GC

The dashboard's patch/mutation views read `list_patches`. Under git the
patches come from the commit's meta block — but GC (§7.5) deletes a pruned
generation's tag, which makes `has_generation` false. The reader falls back
to the journal record, which GC never touches:

```python
        A generation whose tag is GONE — pruned by
        :func:`zicato.epoch.gc.prune_generations` — falls back to the
        journal's ``experiment.json`` record (the same source the
        directory backend reads), which GC never touches. The dashboard's
        patch/mutation views therefore keep rendering a pruned
        generation's patch metadata even after its source tree is
        collected.
```
— `src/zicato/epoch/git_genstore.py`, `list_patches`

This is the doctrine of §7.2 in miniature: the tree is disposable, the
*record* (`experiment.json`) is canonical, and every reader must keep working
from records alone.

---

## 7.5 GC / retention — `zicato epoch gc`

`zicato.epoch.gc.prune_generations` reclaims the disk held by settled-
rejected generations' source trees. What "prune" means per backend, and the
non-negotiable floor, are both stated at the top of the module:

```python
Exactly one of two policies selects the prune set; BOTH share a safety
floor that is never pruned:

* generations whose lineage decision is ``promoted`` (the champion
  chain — the epoch's actual history),
* generations still IN FLIGHT (lineage ``promoted`` is ``null``),
* generations with no lineage record at all (unknown ⇒ conservative),
* the epoch's seed ``v0``.
```
— `src/zicato/epoch/gc.py` (module docstring)

Why each floor entry exists:

| Never pruned | Why |
|---|---|
| Promoted generations | They are the champion chain — the epoch's actual history; the next round derives from the promoted head, and a contract roll seeds the next epoch's `v0` from it. |
| In-flight (`promoted: null`) | The tournament that will decide them has not settled; pruning would delete a tree a worker may be about to mount. |
| No lineage record at all | Unknown ⇒ conservative. A generation the lineage has never heard of might be mid-mint. |
| The seed `v0` | The epoch's baseline — the anchor every diff and every re-derive ultimately resolves against. |

The only prune-eligible class is a generation whose lineage decision is
explicitly `False` (settled-rejected). Per backend:

- **Directory:** remove `generations/{id}/snapshot/` only; `experiment.json`,
  `gen_score.json`, and `runs/` telemetry survive — the generation stays
  fully analysable (invariant D7).
- **Git:** delete the generation tag + its materialised worktree, then
  `git worktree prune` + `git gc --auto`. A rejected commit is reachable
  ONLY through its tag (the branch was reset back to the promoted parent
  before the next derive), so dropping the tag makes it collectable; a
  promoted commit is a branch ancestor and stays reachable regardless —
  consistent, since promoted generations are never pruned anyway.

**Dry-run discipline.** `prune_generations(..., dry_run=True)` is the
default, and the CLI mirrors it:

```
zicato epoch gc [<epoch_id>] (--keep-last <n> | --keep-promoted-only) [--apply]
```

Without `--apply` the command prints the plan (`PruneReport`: examined /
kept / pruned / bytes) and touches nothing. Exactly one policy flag is
required — passing both, or neither, is a `UsageError` in the CLI and a
`ValueError` in the library.

There is also an opt-in epoch-close hook: a `storage_gc` block in the
workspace `config.json` (`{"on_epoch_close": true, "keep_last_n": N}` or
`{"on_epoch_close": true, "keep_promoted_only": true}`) makes
`maybe_prune_on_epoch_close` run a real prune as an epoch closes. It is
best-effort by design — closing an epoch must never fail because GC
hiccuped; every error is logged and swallowed.

> ✅ ALWAYS run `zicato epoch gc` without `--apply` first and read the
> `kept` / `pruned` sets. The report is cheap; the prune is not reversible
> for the directory backend (git's objects linger until reflogs expire, but
> do not count on it).

> ⚠️ TRAP: idempotency of a re-run relies on `store.has_generation` — under
> the directory backend a previously-pruned generation still *enumerates*
> (its record directory survives by design) but has no tree left, and must
> be reported as neither kept nor pruned. If you change enumeration
> semantics in a backend, re-check `prune_generations`' candidate filter.

---

## 7.6 The runtime state files the supervisor reads

Everything under `.zicato/runtime/` is the typed surface of
`src/zicato/runtime/state.py` (dataclasses + load/save helpers). The Rust
supervisor reads the same files through mirrored serde structs in
`crates/supervisor/src/state.rs`, every field `#[serde(default)]` so a
Python-side addition never crashes an older supervisor. The path map is
`src/zicato/runtime/paths.py`; the storage keys are
`src/zicato/runtime/_storage.py`.

### 7.6.1 `heartbeat.json` — liveness, seq-vs-timestamp, and the paused flag

The `Heartbeat` dataclass carries the orchestrator's pid, instance id,
`started_at` / `last_heartbeat` timestamps, lineage coordinates
(`epoch_id` / `generation_id`), the free-form `phase` string, `round_index` /
`round_started_at`, harmonograf deep-link fields — and the one field whose
semantics you must not get wrong, `seq`:

```python
    seq:
        The orchestrator's TRUE liveness cursor (RUNTIME-V2 Phase 4): the
        tail ``seq`` of the progress event log
        (:mod:`zicato.runtime.progress_log`) at the last genuine
        transition. Unlike ``last_heartbeat`` — which the beater thread
        bumps on a timer regardless of progress — this advances ONLY when
        the evolve loop appends a real transition (round start, propose,
        apply, tournament start/settle, gate, promote/reject). A watchdog
        keyed on ``seq`` advancing avoids the timestamp signal's
        false-positive (a slow LLM call ages the stamp) and false-negative
        (a wedged loop whose beater keeps stamping ``now()`` reads alive).
```
— `src/zicato/runtime/state.py`, `Heartbeat`

The division of labour:

- `HeartbeatBeater` (`src/zicato/runtime/heartbeat.py`) is an asyncio task
  that rewrites the whole snapshot every ~2s with a fresh `last_heartbeat`.
  Crucially, the timer bump **re-writes the same `seq`** — it carries the
  snapshot forward; only an explicit `beater.update(seq=...)` at a genuine
  transition moves it. That asymmetry is what makes seq-change age the true
  liveness signal.
- `zicato.runtime.progress_log` is the single-writer append-only event log
  (`runtime/progress.events.jsonl`, built on `channel.EventLog`) whose tail
  `seq` the loop stamps into the heartbeat. A terminal `SETTLED`-class event
  distinguishes "cleanly finished" from "stalled" (`tail_is_terminal`).
- The supervisor's `SeqLiveness` tracker consumes this: seq present → age
  since the last seq *change*; seq absent (legacy heartbeat) → timestamp-age
  fallback. Warn-only either way — see 08-supervisor.md §"Warn-only
  heartbeat".

The **paused flag is not a heartbeat field**: pause state is the presence of
the `runtime/control/pause_epoch` flag file (`is_paused` in
`zicato.runtime.control`), and the supervisor/dashboard read it from there
(the Rust watcher registers `control_dir()/pause_epoch` for change events;
the resume endpoint unlinks it). While paused, the orchestrator sits in
`block_while_paused` polling the flag — the heartbeat timer keeps stamping
(the process is alive), and `seq` legitimately freezes. A watchdog that
killed on frozen seq would kill every paused run; that is one of the reasons
the heartbeat path is warn-only by construction.

### 7.6.2 `active_runs/{run_id}.json` — the per-run record

One file per in-flight run, written by the run's own worker subprocess (not
the orchestrator), which is what lets the supervisor police a single wedged
run without touching anything else. The schema (`ActiveRun` in
`src/zicato/runtime/state.py`):

| Field | Meaning | Consumer that depends on it |
|---|---|---|
| `run_id` | unique run id | everything |
| `pid` | the WORKER's own pid (`os.getpid()` stamped by the worker) | supervisor kill paths |
| `pid_start_time` | the worker's `/proc` start-time token — pid-reuse immunity (D9) | `signal::is_same_process` in the supervisor |
| `pgid` | the worker's own process group (spawned with `start_new_session`, so `pgid == pid`) | group-kill upgrade (`resolve_kill_target`) |
| `started_at`, `last_progress` | ISO-8601 UTC; `last_progress` is bumped every ~3s by `RunHeartbeatBeater` (a daemon thread that keeps beating through GIL-releasing LLM waits) | staleness trigger `decide_run` |
| `wall_clock_budget_seconds`, `deadline` | the promised budget and the absolute deadline (`started_at + budget`) | deadline trigger `decide_run_deadline` — but note the supervisor treats the written deadline as UNTRUSTED and clamps it (08-supervisor.md §"Untrusted clamped deadlines") |
| `events_jsonl_path` | the run's telemetry file | dashboard drill-down |
| `entry_id`, `generation_id`, `epoch_id` | lineage coordinates | dashboard, reaper |
| `snapshot_path` | the run's `ztw-snap-*` ephemeral checkout — recorded so the supervisor can GC it if the orchestrator dies mid-run | `reap_orphaned_snapshot` |

The lifecycle: worker writes the file on start → beater bumps
`last_progress` → the file is removed on a clean run-end. If the orchestrator
dies, the files linger; `prepare_resume`'s `clear_runtime_state` removes them
on the next start, and the supervisor's confirmed-dead reaper finalizes them
if it gets there first.

### 7.6.3 The active-tournament event log and its fold

The live tournament view was historically a mutable `active_tournament.json`
that several writers read-modify-wrote — the lost-update race RUNTIME-V2
names. It is now a **single-writer append-only event log**
(`runtime/active_tournament.events.jsonl`,
`src/zicato/runtime/tournament_log.py`) with a four-token vocabulary:

| Event | Payload | Written by | Fold semantics |
|---|---|---|---|
| `Snapshot` | a FULL `ActiveTournament.to_dict()` envelope | the orchestrator's republish, the gauntlet runner's open, the settle | RESETS the fold — replay starts from the last `Snapshot` |
| `EntryUpdate` | `{entry_id, side, updates}` | the runner, per board-entry transition | overrides the first matching `(entry_id, side)` row |
| `PartialAggregate` | `{champion_agg?, challenger_agg?}` | the runner, per settled board unit | replaces the matching partial-aggregate field(s) |
| `ProjectedUpdate` | `{projected}` | the runner, per settled board unit | merges projected standings + folds `live_progress` lanes |

Every state transition is one atomic append — never a read-modify-write — so
concurrent writers cannot lose each other's updates. Readers call
`read_active_tournament(workspace_root)`
(`zicato.runtime.state`), which folds the log via
`tournament_log.fold_active_tournament` and falls back to the legacy snapshot
only when no log exists. The fold **shares the merge helpers with the old
snapshot writer** (`_fold_projected_into_live_progress`, `_fold_one_lane` in
`state.py`) so the folded view is byte-identical to what the
read-modify-write produced — producer/consumer parity by shared code, not by
reimplementation.

Two behavioural details encoded in `_fold_one_lane` that dashboards depend
on (both anti-flash / anti-thrash measures — change them and the UI regresses):

- a lane mutates ONLY when a *rounded* value actually changes (the dashboard
  digest-gates renders on rounded scalars + integer board counts);
- the champion lane keeps its strategy-seeded `projected_scalar` benchmark
  and its `boards_done` only ever grows to the most-progressed duel — N
  concurrent duels all write the champion lane, and last-writer-wins would
  thrash it.

> ⛔ NEVER add a writer that rewrites the folded view or the legacy snapshot.
> If your feature needs to publish tournament state, add an event type to
> `tournament_log` (one atomic append) and teach the fold — the single-writer
> `seq` discipline (D10) is what keeps the dashboard's render gating and the
> supervisor's fold diagnostics honest.

> ⚠️ TRAP: `update_tournament_entry` validates override *names* eagerly
> against `ActiveTournamentEntry.__dataclass_fields__` so a producer typo
> raises at the call site — but the override is *applied* only in the fold.
> If you add a field to `ActiveTournamentEntry`, the eager validation accepts
> it automatically; what you must hand-check is `to_dict`/`from_dict`
> round-tripping and the default that keeps an old log decoding (§7.13
> recipe).

---

## 7.7 The workspace lock

Only one orchestrator may write under `.zicato/runtime/` at a time. The lock
(`src/zicato/runtime/lock.py`) is deliberately a **pid-based JSON file**
(`runtime/lock.json`), not `fcntl.flock`: the supervisor is a separate
process, possibly a different language, and pid-based locks survive non-clean
exits recoverably.

The stealing rules are a decision matrix over process identity, and identity
is `(pid, start_time)` — invariant D9:

```python
    Behavior matrix (assume my pid = ``self``):

    * No lock file → write a fresh lock, return :class:`WorkspaceLock`.
    * Lock file with pid == ``self`` → idempotent re-acquisition. The
      existing acquisition timestamp is preserved (so the caller can
      log "first acquired at..." across retries).
    * Lock file with pid != ``self`` and that pid is alive → raise
      :class:`WorkspaceLockHeld`.
    * Lock file with pid != ``self`` and that pid is dead → if
      ``steal_stale`` is true, overwrite with a fresh lock and return.
      If false, raise :class:`WorkspaceLockHeld`.
```
— `src/zicato/runtime/lock.py`, `acquire_workspace_lock`

"Alive" in that matrix is `is_same_process(prior.pid, prior.start_time)`, not
bare `os.kill(pid, 0)` liveness. `pid_start_time` reads Linux
`/proc/<pid>/stat` field 22 (with a psutil fallback off-Linux) as an opaque
equality token; `is_same_process` then applies a conservative matrix — when
identity cannot be proven either way, it refuses to steal:

- pid dead → not the same process (steal is allowed);
- pid alive, no recorded start time (legacy lock) → treat as alive (do not
  steal);
- pid alive, current start time unreadable → treat as alive (cannot
  *disprove* identity);
- both known → equal or not.

Even same-pid re-acquisition runs the identity check, guarding the
pathological case where *our own* pid number equals a prior owner's recycled
pid. Release (`release_workspace_lock`) is guarded too: it deletes the file
only if the on-disk lock still matches this caller's `(pid, instance_id)` —
otherwise a successor stole it after a crash and overwriting would corrupt
the successor's state.

> ✅ ALWAYS reuse `is_pid_alive` / `pid_start_time` / `is_same_process` from
> `zicato.runtime.lock` for any new liveness decision on the Python side.
> The Rust supervisor has byte-equivalent twins in
> `crates/supervisor/src/signal.rs`; the two must keep agreeing on what the
> start-time token means (Linux: `/proc` field 22 as a float).

---

## 7.8 Crash-resume in full

`zicato.runtime.resume.prepare_resume(workspace_root, epoch_id)` runs once at
`evolve` start, after the lock is held and before the round loop. The design
rule is stated first and governs every branch:

```python
The single design rule is **conservatism** (RUNTIME.md §4.2): *when it
cannot tell exactly what state things are in, it discards the partial
work and re-runs.* The cost of a wasted re-run is one round; the cost of
a wrong inference is journal / lineage corruption.
```
— `src/zicato/runtime/resume.py` (module docstring)

### 7.8.1 Why resume is nearly free — replicate-slot-aware unit caching

A board unit is cached by `(generation_id, entry_id, replicate)` — its
`loss.json` on disk IS the cache (`zicato.tournament.runner`'s
`_resolve_cached_unit`). A generation under a fixed contract is immutable, so
a completed unit's `loss.json` is a permanent cache HIT. Resuming a
tournament is therefore mostly "re-enter the loop and let the cache hit the
done units": only entries (and replicate slots) with no `loss.json` yet
re-run. Replicated contracts get this for free — the replicate index is part
of the cache key, so a crash after replicate 1 of 2 re-runs only replicate 2.

The load-bearing caveat: the unit cache key does **not** include the patch
set. Reusing cached units is only sound if the *same patches* produced the
snapshot they ran against — which is exactly why resume-in-place reuses the
**persisted** `experiment.json` + patches rather than re-proposing (the
proposer is non-deterministic; a fresh proposal would silently pair stale
cached losses with a different snapshot).

### 7.8.2 The two phases and the classification table

Phase 1 — `clear_runtime_state`: delete `heartbeat.json`, the
active-tournament event log AND its legacy snapshot, and every
`active_runs/*.json`. The workspace lock is not touched (acquisition already
stole any stale one). Best-effort; an unlink race never aborts startup.

Phase 2 — classify the highest un-outcomed `vN`:

```python
    ===============================================  ====================
    On-disk state of the un-outcomed latest gen      Action
    ===============================================  ====================
    experiment readable + snapshot/ + >=1 loss.json  resume in place
    experiment readable + snapshot/ + 0 loss.json    discard (re-run)
    experiment readable + no snapshot/               discard (re-run)
    experiment present but unreadable / outcome set  discard (garbled)
    no experiment.json                               discard (partial)
    ===============================================  ====================
```
— `src/zicato/runtime/resume.py`, `prepare_resume`

The `ResumePlan.classification` tokens, exhaustively:

| Classification | Meaning | Disposition |
|---|---|---|
| `clean` | no generation beyond `v0`, or the latest already has a committed outcome | next round runs byte-identically to a cold start |
| `resume_tournament` | readable un-outcomed experiment + applied `snapshot/` + ≥1 `loss.json` | resume in place: reuse the persisted experiment (do NOT re-propose), re-derive the snapshot from the persisted patches (idempotent), let the unit cache HIT |
| `discard_unapplied` | experiment readable but no `snapshot/` | discard, re-run fresh |
| `discard_no_progress` | applied but zero completed units | discard (byte-identical to starting the tournament from scratch, and keeps the loop free of a zero-cache special case) |
| `discard_garbled` | `experiment.json` exists but is unreadable / inconsistent | discard, re-propose |
| `discard_partial_proposal` | no `experiment.json` at all | discard, re-propose |

Note carefully what is NOT in this table: `deferred_infra` is not a resume
classification. It is a *round decision* (§7.12) that deliberately leaves the
experiment **un-outcomed** so that this very table handles the next start —
a deferred round with cached units resumes in place; one with none discards
cleanly.

### 7.8.3 The invariant that makes discard safe

Invariant D8, in the module's own words:

```python
Lineage / journal safety
------------------------
A generation is appended to ``lineage.json`` and journaled only at the
very end of one evolve round, *after* its outcome is decided. An
un-outcomed generation therefore has NO lineage or journal entry, so
both discarding it and resuming it leave those append-only records
untouched. This is what makes the protocol corruption-free by
construction.
```
— `src/zicato/runtime/resume.py` (module docstring)

> ⛔ NEVER write a lineage or journal entry for a generation before its
> outcome is decided — not "provisionally", not "for the dashboard". The
> moment an un-outcomed generation has an append-only record, `\_discard\_generation`
> stops being safe and the whole resume protocol inherits a corruption mode.
> If you need pre-outcome visibility, use the runtime tree (§7.6) — it is
> deleted on resume by design.

Scope note: this first cut covers the **gauntlet** path. A multi-challenger
field (swiss / elim / racing) under interruption is treated conservatively —
in-flight challengers are discarded and the round re-runs. If you extend
in-place resume through the non-gauntlet structures, the classification table
and D8 are the contract you must preserve.

---

## 7.9 The control protocol

`src/zicato/runtime/control.py` (transport) + `control_consumer.py` (evolve
semantics). Small files under `.zicato/runtime/control/` request operator
actions; the orchestrator polls at safe points and consumes each with an
audit record. Files, not HTTP, on purpose: the supervisor is a separate
process (possibly another language), crash-safety is trivial (an interrupted
consume leaves the request in place and the next poll retries), and an
operator can `touch .zicato/runtime/control/pause_epoch` in an emergency.

### 7.9.1 The command surface

| Command | On-disk shape | Written by | Consumed at | Effect |
|---|---|---|---|---|
| `pause_epoch` | flag file `control/pause_epoch` (optional JSON body `{"reason", "ts"}`) | dashboard POST `/api/control/pause` (Python service and Rust supervisor both), CLI, bare `touch` | `block_while_paused` — between rounds, and polled until cleared | scheduling held; resume = deleting the flag (`/api/control/resume` unlinks it — never a queued command) |
| `skip_round` | flag file `control/skip_round` | dashboard / CLI | `claim_skip_round` at the top of `evolve_once` | round aborts cleanly, exactly like a wall-clock budget cut; a *between-rounds* stale skip is drained as a no-op |
| `kill_runs/<run_id>` | one file per target under `control/kill_runs/` | dashboard POST `/api/control/kill/:run_id`; ALSO the Python parent writes here via `request_worker_kill` | the **Rust supervisor's** runs loop — not the orchestrator | the single-escalator kill handshake (see 08-supervisor.md §"The kill-request single-escalator handshake"); the supervisor clears the marker after escalating |
| `promote/<gen_id>` / `reject/<gen_id>` | one file per target | dashboard / CLI | `claim_gate_override` at the gate (gauntlet) / `claim_field_gate_overrides` (field structures) | overrides the gate's verdict for the *matching* in-flight generation; recorded explicitly as an operator override in the OutcomeRecord/journal, never silently |
| `rubric_replacement.txt` | one payload file whose body IS the new brief text | dashboard / CLI | `claim_rubric_replacement` between rounds | a contract edit — the payload is written to the live brief and contract-hash auto-epoching rolls the epoch |

Consumption always goes through `consume_command`, whose crash-ordering is
the protocol's honesty guarantee:

```python
    The order is "write log, then delete source" so a crash mid-consume
    leaves both copies present (the orchestrator re-reads, the audit log
    has one extra entry — both observable, neither lost) rather than
    deleting without recording.
```
— `src/zicato/runtime/control.py`, `consume_command`

The audit sidecar lands in `control_log/` as
`{iso-ts}_{name}[_{arg}].json` with the command, arg, payload, `source`
(`"orchestrator"` for the loop's own consumer — `CONSUMER_SOURCE` in
`control_consumer.py` — vs `"dashboard"`/`"cli"` for manual consumes), and a
freeform `reason`.

### 7.9.2 Semantics an agent must not break

- **Target matching is exact.** `claim_gate_override` claims only a command
  whose `arg` equals the round's in-flight generation id; a stale override
  aimed at a different generation is left pending so it cannot mis-fire.
- **Promote beats reject, and the loser is drained.** When both target the
  same generation, the promote is honoured and the reject is also consumed
  (archived as "superseded") so it cannot fire on a later round —
  deterministic and recorded.
- **Epoch rolls drain stale overrides.** Generation ids restart at `v0` per
  epoch, so `drain_stale_gate_overrides` archives every pending
  promote/reject when the epoch rolls (with a reason) — otherwise an
  override aimed at the old epoch's `v3` would fire on the new epoch's `v3`.
  Pause/skip flags and a pending rubric replacement survive the roll by
  design.
- **The pause episode is archived exactly once**, after the flag clears,
  with how long scheduling was held and the operator's reason — the audit
  trail records that scheduling WAS held, never silently.

> ✅ ALWAYS archive (consume with a reason) rather than delete a command
> file, in any new consumer path. `control_log/` is the reconstruction
> record for every operator action that changed a decision; a bare unlink
> breaks it. The one legitimate bare unlink in the system is the resume
> gesture removing `pause_epoch` — because the *orchestrator* archives the
> pause episode itself.

> ⚠️ TRAP: `kill_runs/` has TWO producers (dashboard POST and the Python
> parent's `request_worker_kill`) but exactly ONE consumer — the Rust
> supervisor. The orchestrator must never signal a worker pid itself; that
> is the whole point of the handshake (no parent↔supervisor race over the
> same pid). If you add a "kill" feature, write the marker; do not import
> `os.kill`.

Also in this package: `zicato.runtime.channel.CommandQueue` — the
generalised many-writer/claim-once queue built on `atomic_claim`, with
`pending/` and `archive/` prefixes. The directory-tree control protocol above
predates it; new producer-consumer channels should use `CommandQueue` or
`EventLog` (`channel.py` is Phase 1 of RUNTIME-V2 and exists precisely so
nobody hand-rolls a new file protocol).

---

## 7.10 RoundLog — the durable store-of-record for one round

`src/zicato/epoch/round_log.py` defines the ONE durable, replayable record of
an evolve round: a typed, sequenced JSONL log at
`epochs/{epoch}/rounds/{round}/round_log.jsonl`, plus the fold that reduces
it to a `RoundRecord`. It lives under `epochs/` — not `runtime/` — precisely
so it survives crashes and resume (§7.2's placement rule).

### 7.10.1 Event schema and `seq`

Each line is `{"seq": N, "ts": "...", "type": "<token>", "payload": {...}}`.
`seq` starts at 1 and increments by exactly 1 per append, derived from the
current tail under the single-writer contract (only the orchestrator driving
the round appends). The closed vocabulary — one frozen dataclass per event,
registered in `EVENT_TYPES` keyed by its `TYPE` wire token:

| Wire token | Dataclass | Carries |
|---|---|---|
| `round_opened` | `RoundOpened` | `contract_hash` |
| `proposal_attempted` | `ProposalAttempted` | `errors` tuple (empty on success) |
| `candidate_sampled` | `CandidateSampled` | `i`, `n`, `revise` |
| `candidate_screened` | `CandidateScreened` | `index`, `vetoed`, `confirmed`, counts-only `screen_summary`, `revise` |
| `critique_selected` | `CritiqueSelected` | `index`, `reason` |
| `experiment_minted` | `ExperimentMinted` | `experiment_id` |
| `patches_applied` | `PatchesApplied` | `generation_id` |
| `validation_failed` | `ValidationFailed` | `findings` tuple |
| `unit_completed` | `UnitCompleted` | `entry_id`, `replicate`, `side` |
| `gate_evaluated` | `GateEvaluated` | `rule_fired`, `decision` |
| `holdout_released` | `HoldoutReleased` | `confirmed` |
| `evidence_replicated` | `EvidenceReplicated` | `ci_state` trace row |
| `decision_recorded` | `DecisionRecorded` | `decision`, `provenance` dict |
| `round_closed` | `RoundClosed` | — |

Forward compatibility is built into the decoder: an unknown wire token reads
back as a raw envelope with typed `event = None` (the fold skips it); extra
payload keys are dropped; missing keys take dataclass defaults; JSON's
list-vs-tuple round-trip is patched up in `_decode_event`.

### 7.10.2 Torn-tail truncation on the write path

`RoundLog.append` repairs before it sequences:

```python
        ``seq`` is the last PARSEABLE event's ``seq`` plus one (``1`` for
        an empty/absent log) — a torn tail contributes nothing, so a
        writer resuming after a crash continues the monotonic sequence.
        Before appending, a file that does not end in a newline (the torn
        tail a crash mid-append leaves) is TRUNCATED back to its last
        complete line: the partial record was never a complete event (its
        append never finished), so dropping it is the honest repair — and
        it can never concatenate with this append or read back later as
        interior corruption.
```
— `src/zicato/epoch/round_log.py`, `RoundLog.append`

This is the same tail discipline as the supervisor ledger's
`repair_torn_tail` (08-supervisor.md) — repair the tail on the WRITE side so
readers never see dead bytes merge into a fresh event, and so the interior-
corruption rule (D4) stays a real invariant rather than a hope.

### 7.10.3 The fold

`fold_round_record(events)` is pure and total over any prefix of a valid
log: a mid-round crash leaves a foldable partial record (`closed=False`,
`RoundRecord.complete` false), unknown event types are ignored, later
single-valued events win (a second `decision_recorded` overwrites the
first — the last word is the record), and trail-shaped fields (`units`,
`gates`, `evidence_trail`) accumulate in order. The output `RoundRecord`
carries the round's whole arc: proposal session tallies, applied generation
ids, validation findings, completed units, gate verdicts, the holdout bit,
the evidence trail, and the terminal decision + provenance.

### 7.10.4 Emission is best-effort — the never-fail-a-round rule

The orchestrator emits through `_RoundLogEmitter`
(`src/zicato/orchestrator.py`), and its posture is invariant D11:

```python
class _RoundLogEmitter:
    """Best-effort appender onto one round's durable RoundLog (WS8).

    Emission failures must NEVER fail a round — the live index dual-write
    (:func:`_ingest_experiment_into_index`) is the precedent: the canonical
    stores (``experiment.json``, lineage, journal) stay authoritative and
    the event log is a derived, replayable trace. A bind failure degrades
    to a permanent no-op emitter; every append failure is logged at
    ``debug`` and swallowed.
```
— `src/zicato/orchestrator.py`, `_RoundLogEmitter`

`emit(type_token, fields)` resolves the dataclass through `EVENT_TYPES`; an
unknown token is silently dropped (vocabulary skew must never crash a
producer). The same string-token seam serves the proposer-side callback
(`ProposerContext.round_event_emitter`) so both sides share one signature.

> ⚠️ TRAP: the RoundLog is simultaneously "durable store-of-record" (its
> reader semantics, its placement under `epochs/`) and "best-effort at
> emission". These do not contradict: what lands in the log is trustworthy
> and replayable; whether a given event landed is not guaranteed. Consumers
> must therefore treat the log as evidence, never as the *only* carrier of a
> decision — the decision itself always also lands in the canonical records
> (`experiment.json` outcome, lineage, journal). If your feature makes a
> decision, persist it canonically first, then emit.

---

## 7.11 `format_version` — refuse-on-newer for canonical records

Canonical JSON records (`experiment.json`, per-epoch `config.json`,
`lineage.json`) are stamped at write time with
`RECORD_FORMAT_VERSION` (currently `1`,
`src/zicato/epoch/_storage.py`), and every reader runs
`check_record_format` before interpreting the body:

```python
def check_record_format(body: dict[str, object], record_name: str) -> None:
    """Refuse a canonical record whose ``format_version`` this build cannot read.

    ``body`` is the parsed JSON record; ``record_name`` names it in the
    error (e.g. ``"experiment.json"``). Absent ⇒ version 1 (pre-stamp
    records — accepted this release); equal ⇒ fine; anything else raises
    :class:`RecordFormatError` with the upgrade guidance.
    """
```
— `src/zicato/epoch/_storage.py`

The semantics, spelled out:

- **Absent key ⇒ version 1** this release, so every pre-stamp workspace and
  fixture keeps reading.
- **Equal ⇒ fine.**
- **Anything else ⇒ `RecordFormatError`** — the record was written by an
  incompatible (likely newer) zicato; refusing beats silently misreading a
  shape this build cannot promise to interpret (invariant D12).
- **There are NO migration shims.** Bumping the constant is a deliberate
  format break, not a routine version tick.

The same refuse-on-newer stance appears twice more in the system, one per
derived store: `IndexSchemaNewerError` for `index.db` (§7.1.1 — an older
writer never re-stamps a newer database down), and the Rust supervisor's
`EXPECTED_SCHEMA_VERSION` mismatch → `StaleSchema` degrade
(08-supervisor.md). Additive evolution is the default everywhere else:
dataclass fields get defaults, serde fields get `#[serde(default)]`, JSONL
event payloads tolerate unknown keys.

> ✅ ALWAYS prefer an additive, defaulted field over a `format_version` bump.
> The bump exists for the day a shape genuinely cannot be read by an old
> reader — it is the emergency brake, not the turn signal.

---

## 7.12 The infra-outage circuit and the round token ledger

Two runtime guards protect a round from burning budget against broken
infrastructure. Both are knobs on `RuntimeConfig`
(`src/zicato/core/runtime.py`), both default OFF (`0` = disabled), and both
are validated non-negative in `__post_init__`.

### 7.12.1 The endpoint-outage circuit (`infra_abort_round_threshold`)

**Knob.** `RuntimeConfig.infra_abort_round_threshold: int = 0` — the number
of infra-aborted runs in one round at which the circuit trips; `0` disables.

**What counts as an infra abort.** `zicato.core.loss.is_infra_abort_cause`:
a `LossProfile.abort_cause` that is set and is NOT the genuine
`BUDGET_ABORT_CAUSE` wall-clock exhaustion — i.e. a parent/supervisor kill, a
worker crash, a prepare failure, or an unreadable result. A cleanly-reduced
run (empty/`None` cause) is never one.

**Enforcement point.** After the tournament settles in `evolve_once`
(`src/zicato/orchestrator.py`): `_count_infra_aborted_runs(tournament_result)`
is compared against the threshold, and on a trip the round is settled by
`_defer_round_infra_outage` with decision `DEFERRED_INFRA_DECISION`
(`"deferred_infra"`).

**What the deferral deliberately does NOT do** — this is the resume
interaction, and it is load-bearing:

```python
    Deliberately does NOT: cache either side's ``gen_score.json`` (a
    mostly-aborted aggregate would poison fast mode), route the gate
    verdict through the strategy, write an outcome / lineage / journal
    entry, or advance anything. The ``experiment.json`` persisted before
    the tournament stays UN-OUTCOMED — the exact on-disk shape the
    conservative crash-resume (:func:`zicato.runtime.resume.prepare_resume`)
    already reconciles: with at least one completed unit's cached
    ``loss.json`` the round resumes in place (the cache HITs the done
    units), with none it discards cleanly and re-proposes.
```
— `src/zicato/orchestrator.py`, `_defer_round_infra_outage`

So a deferred round costs almost nothing to retry: the next `evolve` start
(or the loop's own continuation) flows through §7.8's table, cache-hits every
unit that DID complete, and re-runs only what the outage ate. The deferral is
visible on every surface: a `decision_recorded` RoundLog event with
`decision: "deferred_infra"` and a provenance block carrying the counts, a
`infra_outage` WARNING finding in the round's health report
(`zicato.health.diagnostics.detect_infra_outage`), and a heartbeat phase of
`deferred_infra:round_{n}:{gen}`.

### 7.12.2 The per-round token ledger (`max_tokens_per_round`)

**Knobs.** `RuntimeConfig.max_tokens_per_round: int = 0` (0 = off) plus the
round-scoped mutable `RuntimeConfig.token_ledger: RoundTokenLedger | None`.

**Minting.** The orchestrator mints a FRESH `RoundTokenLedger` per round and
rebinds it onto the round's config via `dataclasses.replace` — so every
runner seam that already receives the config (full/fast board-unit
schedulers, the candidate screen, evidence-gate replicate duels) shares one
tally with no signature changes. The ledger is never read from persisted
config (`src/zicato/runtime_factory.py` documents this); it exists only for
the round.

**Accounting + enforcement.** Every FRESH board-unit run folds its
`LossProfile.tokens_spent` in via `ledger.add(...)`; cache hits spend and add
nothing. The schedulers call `check_and_clip()` at every would-launch point
(between board units / replicate slots) and stop scheduling once the budget
is spent; the latched `clipped` flag is how the orchestrator learns the
round was token-clipped (surfaced as a health finding via
`detect_token_budget_clip`) without threading a result back through the
runner stack.

**Honesty caveat, verbatim:**

```python
    Token counts are OPPORTUNISTIC by the ``cost:`` namespace's contract
    (a harness without token-accounting middleware reports 0), so a
    ledger can only ever under-count — the budget is a best-effort
    guard, never a hard metering guarantee.
```
— `src/zicato/core/runtime.py`, `RoundTokenLedger`

**Resume interaction.** None to speak of, by construction: the ledger is
round-scoped and in-memory. A resumed round mints a fresh ledger; the cached
units it cache-hits add nothing (they are not fresh runs), so a resume never
double-counts spend.

> ⚠️ TRAP: `RoundTokenLedger` is single-threaded by design — mutations happen
> on the orchestrator's event loop with no awaits between read and write. If
> you move accounting into a thread or across an `await`, you have introduced
> a lost-update race the class explicitly does not defend against.

---

## 7.13 Recipe: add a runtime state field

Scenario: the loop needs to expose a new live datum — say a
`current_board_size: int` on the heartbeat — to the supervisor and the
dashboard. Follow every step; the ones agents skip are the ones that page an
operator.

**Step 1 — Schema (Python dataclass).** Add the field to `Heartbeat` in
`src/zicato/runtime/state.py` with a safe default, and thread it through BOTH
serializers:

```python
    current_board_size: int = 0          # in the dataclass, defaulted

    # in to_dict():
    "current_board_size": self.current_board_size,

    # in from_dict():
    current_board_size=int(d.get("current_board_size", 0)),
```

The `from_dict` default is the back-compat contract: a heartbeat written
before the field existed must read back with the safe default (`seq` is the
in-tree precedent — read its comment). Choose the default to mean "unknown /
not recorded", never a plausible live value.

**Step 2 — Python writer.** Extend `HeartbeatBeater.update` in
`src/zicato/runtime/heartbeat.py` with a `current_board_size: int | None =
None` keyword and the matching `replace(...)` line (the `None`-means-
unchanged pipe is the pattern for every field there), then have the
orchestrator pass it at the transition where the value changes. Do NOT write
the file directly — the beater owns the snapshot; a direct
`write_heartbeat` from loop code would be overwritten by the next timer
bump.

**Step 3 — Rust reader tolerance.** The supervisor deserializes
`heartbeat.json` through `crates/supervisor/src/state.rs`, where every field
is `#[serde(default)]`:

```rust
/// `.zicato/runtime/heartbeat.json`
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct Heartbeat {
    #[serde(default)]
    pub pid: Option<i32>,
```
— `crates/supervisor/src/state.rs`

An *unknown extra field is already tolerated* (serde ignores it), so an
older supervisor keeps working — that half is free. But if the supervisor
should *surface* the field (on `/statusz` or an API payload), add an
`#[serde(default)] pub current_board_size: Option<u64>` mirror and wire it
into the route/statusz view — and remember the heartbeat-ts lesson: when a
payload's *shape or semantics* change (not just an addition), the Python
service and the Rust route must change in the same commit, or the two
dashboards skew (see 08-supervisor.md §"When a Python payload change
requires Rust parity").

**Step 4 — Dashboard readback.** The Python dashboard's runtime view reads
heartbeats through `zicato.query` (`src/zicato/query/runtime_view.py` /
`loop_view.py`) and serves them via `src/zicato/dashboard/endpoints.py`.
Surface the field there with ONE spelling — the schema clean-break rule: one
spelling per field across every payload, no alias coalescing on the client
(the JS contracts are documented in
`src/zicato/dashboard/static/js/CONTRACTS.md`). If the frontend renders it,
add the node-suite assertion (11-testing.md §"Node suite conventions") that a
no-op beat does not churn DOM for it.

**Step 5 — Golden / parity impact.** Runtime state is deliberately masked or
absent from most goldens (timestamps are normalized to `<TS>` by
`tools/parity/lib/normalize.py`), but check anyway:

- if any dashboard payload fixture in `tests/` or
  `src/zicato/dashboard/static/test/` pins the heartbeat shape, extend it
  with the canonical spelling (never weaken an existing assertion);
- run the parity harness — a new field that leaks into a captured surface
  reds MOCK-GOLDEN, and that red is information, not noise.

**Step 6 — Tests.** Round-trip test in `tests/` (write via
`write_heartbeat`, read via `read_heartbeat`, assert the field; then read a
dict WITHOUT the key and assert the default), plus a Rust
`cargo test -p zicato-supervisor` run to prove deserialization of a payload
carrying the new field.

**Verify**

```bash
uv run pytest tests/test_runtime_state.py tests/test_runtime_heartbeat.py -q
uv run mypy src/zicato/
cargo test -p zicato-supervisor
bash tools/parity.sh --only MOCK-GOLDEN
```

---

## 7.14 Recipe: add a RoundLog event type

Scenario: your feature makes a new per-round decision — say a placebo-arm
draw — and it must be replayable from the round's durable trace.

**Step 1 — Dataclass.** In `src/zicato/epoch/round_log.py`, add a frozen
slotted dataclass with a `TYPE` ClassVar wire token and defaults on EVERY
field (the decoder fills missing keys from defaults — that is what lets an
old log decode on a new reader, and a new log skip-decode on an old one):

```python
@dataclass(frozen=True, slots=True)
class PlaceboDrawn:
    """A placebo arm was drawn for this round."""

    TYPE: ClassVar[str] = "placebo_drawn"
    arm: str = ""
    seed: int = 0
```

Payload fields must be JSON-clean (str/int/float/bool/dict/tuple — tuples
round-trip as lists and are re-tupled by `_decode_event`). A counts-only
discipline applies to anything derived from the board: look at
`CandidateScreened.screen_summary` ("NEVER an entry id") — round logs are
long-lived and read by surfaces that must not leak holdout information.

**Step 2 — Register in `EVENT_TYPES` and the union.** Add the class to the
`EVENT_TYPES` comprehension's tuple AND to the `RoundEvent` union alias AND
to `__all__`. Miss the first and the emitter silently drops your token
(unknown-token = no-op by design); miss the second and mypy flags every
typed consumer.

**Step 3 — Emitter token.** Emission goes through the best-effort emitter —
one call at the decision site in `src/zicato/orchestrator.py` (or via the
proposer-side `round_event_emitter` callback if it originates in the
proposer):

```python
round_log.emit("placebo_drawn", {"arm": arm_name, "seed": seed})
```

Never construct/append a `RoundLog` directly from loop code and never let
emission raise (D11): `_RoundLogEmitter.emit` already swallows — keep it that
way by passing only pre-computed, exception-free payload values (compute the
dict *outside* any `getattr` chain that could throw, or mirror
`_emit_tournament_units`' defensive shape).

**Step 4 — Fold.** Extend `fold_round_record`: add an accumulator variable,
an `elif isinstance(event, PlaceboDrawn):` branch, and (if consumers need
it) a field on `RoundRecord` — with a default, so an old log folds into the
new record shape unchanged. Decide deliberately whether the field is
last-write-wins (like `decision`) or a trail (like `evidence_trail`), and
say which in the docstring.

**Step 5 — Ordering + round-trip test.** In `tests/test_round_log.py`,
following the file's existing patterns:

- append your event among others and assert `seq` continues gap-free;
- assert the wire line decodes back to the typed dataclass
  (`RoundLog(...).read()[i].event == PlaceboDrawn(...)`);
- assert forward-compat: hand-write a line with your token plus an EXTRA
  payload key and assert it still decodes (extra keys dropped);
- assert the fold: a log containing your event folds to the expected
  `RoundRecord` field, and a log WITHOUT it folds to the default;
- if ordering relative to existing events matters to a consumer (e.g. it
  must land before `decision_recorded`), pin that with an emission-order
  test at the orchestrator seam, not just a fold test.

**Verify**

```bash
uv run pytest tests/test_round_log.py -q
uv run pytest tests/ -q -k "round_log or evolve"   # emitter wiring didn't regress
uv run mypy src/zicato/
```

---

## 7.15 Cross-references

- 08-supervisor.md — the consumer of every runtime file in §7.6; the
  single-escalator kill handshake; the `ztw-snap-` reaping contract; the
  read-only index discipline.
- 11-testing.md — the genstore conformance suite, the storage conformance
  suite (`tests/test_storage_conformance.py` pins the `StorageBackend`
  atomic contract cross-backend), the parity gates that pin the surfaces
  this chapter's stores feed.
- 12-bug-casebook.md — the stale-worktree re-derive bug, the worktree
  prune-vs-add race, and the rest of the durability incidents referenced
  here.
- 03-contract-and-epochs.md — what the contract hash covers and why a
  rubric replacement rolls the epoch instead of patching in place.
- 06-tournament-and-selection.md — where gate verdicts and evidence
  replicates come from before they land in the RoundLog.
- `docs/design/STORAGE.md` — the full design record (the five kinds, the
  seam fork, the git-backend design review); `docs/design/RUNTIME.md` +
  `docs/design/RUNTIME-V2.md` — the runtime state and channel migrations.
