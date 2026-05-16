# Storage

This document describes zicato's storage layer. It is a two-stage
design: v0 ships directory-backed storage that is already
implemented; v0+1 (and beyond) ships a git-backed alternative
that brings native diff/log/blame tooling to every generation.

The two backends share an interface that does not yet exist as
a Python protocol — see §2 for why the abstraction is being
deferred until the git backend is implemented.

This document covers:

- The v0 directory layout (shipping).
- Why the `GenerationStore` protocol is being deferred.
- The git-backed roadmap (G0-G10) with scope, deliverable,
  dependencies, and effort estimates.
- The migration tooling.
- The new operator commands enabled by git.

## 1. v0 — directory-backed (shipping)

The v0 storage layer is documented in
[EPOCHS-AND-JOURNALING.md](EPOCHS-AND-JOURNALING.md) §2; this
section is a brief recap so this document is self-contained.

```
.zicato/
  config.json
  lineage.json                       # cross-epoch generation DAG
  epochs/
    {epoch_id}/
      board.jsonl
      proposer_brief.md
      scoring.json
      generations/
        v0/
          snapshot/                  # full source tree
          runs/{entry_id}/{events.jsonl, loss.json}
          gen_score.json
        v1/
          snapshot/
          experiment.json            # hypothesis + patch_ids + outcome
          patches/{patch_id}.json    # one file per patch
          runs/{entry_id}/{events.jsonl, loss.json}
          gen_score.json
        ...
      patterns/
        round_NNN.json
      journal.md
      analysis.md
      analysis.html
      current_generation
  runtime/                           # see RUNTIME.md
```

### 1.1 Per-patch file layout

Patches live as **separate files** under each generation's
`patches/` directory:

```
generations/v1/
  experiment.json              # body carries patch_ids: [...]
  patches/
    be4c8de0b5234ec4a8d8db4e8af3f8f0.json
    1f29c6a2e9e44ad99c4f55c9f7df0a3e.json
```

Each patch file:

```json
{
  "id": "be4c8de0b5234ec4a8d8db4e8af3f8f0",
  "mutation_id": "researcher.instruction",
  "op": "replace",
  "new_content": "...",
  "new_numeric": null,
  "new_enum": null,
  "rationale": "tighter wording to require citations"
}
```

The body of `experiment.json` only references patches by id.
This keeps `experiment.json` small (operator-readable in a
terminal pager) and gives one file per patch when an operator
wants to inspect a specific change.

### 1.2 Write order

Per-patch files are written **first**; `experiment.json` is
written **last**. A partial write (crash between the two
phases) leaves orphan patch files behind, which are harmless —
no reader picks them up because `experiment.json`'s
`patch_ids` list is the authoritative source. Writing in the
other order would leave a dangling reference to a missing patch
file, which IS harmful.

In-memory `Experiment.patches` remains a `tuple[Patch, ...]`
regardless. Only the on-disk shape splits; every write helper
round-trips back to the same tuple of dataclasses on read.

### 1.3 Disk cost

Per generation, the snapshot dominates:

| Artifact | Typical size |
|---|---|
| `snapshot/` (full source tree) | 5-50 MB for a multi-agent system; v0 stores the full tree per generation |
| `runs/{entry_id}/events.jsonl` | 100KB-2MB per run, depending on agent verbosity |
| `runs/{entry_id}/loss.json` | <1KB |
| `experiment.json` | 1-5KB |
| `patches/*.json` | 1-10KB per patch |
| `gen_score.json` | 1-2KB |

For an epoch with 10 entries on the board and 20 generations,
that's roughly:

```
20 generations × (50MB snapshot + 10 × 1MB events) ≈ 1.2GB per epoch
```

This is acceptable for v0's target — prompts-mostly inner
harnesses with moderate-length lineages. For longer lineages
(target 3, zicato optimising zicato, where a single experiment
might run 50+ generations across many epochs), the disk cost
becomes a concern and the git backend's blob dedup pays off.

### 1.4 Legacy inline-patches form

Workspaces produced before the per-patch layout landed used an
inline `patches: [{...}, ...]` array directly on
`experiment.json`. The read helper transparently accepts that
old shape for backward compatibility; new writes always use the
per-patch layout. The
`zicato.epoch.migrate.migrate_inline_to_perpatch` utility
converts an old generation in place when the operator wants a
clean conversion.

## 2. The `GenerationStore` protocol — deferred

A natural impulse when introducing a second storage backend is
to extract a protocol up front:

```python
# DON'T DO THIS YET
class GenerationStore(Protocol):
    def write_snapshot(self, epoch_id: str, gen_id: str, files: dict[str, bytes]) -> None: ...
    def read_snapshot(self, epoch_id: str, gen_id: str) -> dict[str, bytes]: ...
    def write_experiment(self, epoch_id: str, gen_id: str, exp: Experiment) -> None: ...
    def read_experiment(self, epoch_id: str, gen_id: str) -> Experiment: ...
    # ... etc, one method per artifact kind
```

This is being **deliberately deferred** until the git backend is
implemented. Two reasons:

### 2.1 Preemptive abstraction risk

The directory backend has methods that don't fit a git backend
naturally:

- `read_snapshot()` returns a `dict[str, bytes]` — but the git
  backend's natural shape is to give you a checked-out tree on
  disk, not a dict.
- `write_snapshot()` takes a dict of files — but the git
  backend's natural shape is to take a directory and `git add`
  it.
- `read_experiment()` returns the full `Experiment` — but the
  git backend stores half of it in commit metadata and the
  other half in commit message body.

Designing the protocol before having a working git backend means
the protocol shape would be inevitably wrong, and the directory
backend would have to be reshaped to match an abstraction it
didn't need.

### 2.2 Refactoring under known constraints is cheap

When the git backend ships:

1. Implement the git backend as a new module (no protocol yet).
2. Find the boundary: what does the git backend do that the
   directory backend does, but differently?
3. Extract a protocol from that observed shape — not from
   speculation.
4. Refactor the directory backend to implement the protocol.
5. Switch consumers to the protocol.

The refactor is bounded — the directory backend already works,
and the git backend is the only new thing. The protocol is
written **once** with two known implementations driving it.

### 2.3 Consumer modules that need to be refactored

When the protocol lands, these are the consumer modules that
will be refactored to depend on it:

| Module | Current behavior | Refactor |
|---|---|---|
| `zicato.epoch.lifecycle` | Reads/writes directories directly | Depend on protocol; protocol has `epoch_create`, `epoch_close` |
| `zicato.tournament.runner` | Spawns workers pointed at directory paths | Worker still uses paths (it doesn't care); orchestrator obtains paths from protocol |
| `zicato.mutation.applier` | Writes snapshot files | Depend on protocol's `write_snapshot` |
| `zicato.epoch.journal` | Appends to `journal.md` | Depend on protocol's `append_journal` |
| `zicato.epoch.analysis` | Writes `analysis.md` and `analysis.html` | Depend on protocol's `write_analysis_artifacts` |
| `zicato.cli.journal_show` | Reads `journal.md` | Depend on protocol's `read_journal` |

The refactor is **localized**: most of the loop doesn't care
which backend is in use. Workers read snapshots from a path; the
path provider can be either backend. The runner doesn't see the
protocol at all.

## 3. The git-backed roadmap (v0+1)

The git backend ships as a sequence of phases, G0 through G10.
Each phase has a scope, a deliverable, dependencies, and an
effort estimate. They sequence; G2 depends on G1, G3 depends on
G2, etc. Some phases can be parallelized (noted).

### 3.1 Design at a glance

```
.zicato/
  config.json
  runtime/                           # unchanged
  epochs/                            # PARTIALLY MIGRATED
    {epoch_id}/
      board.jsonl                    # unchanged (small, frequently edited)
      proposer_brief.md              # unchanged
      scoring.json                   # unchanged
      patterns/                      # unchanged (per-round detector output)
      journal.md                     # unchanged
      analysis.md                    # unchanged
      analysis.html                  # unchanged
      current_generation             # unchanged
      # NB: no generations/ directory anymore; lives in git
  repo/                              # NEW — private git repo
    .git/
      HEAD
      refs/heads/epoch/{epoch_id}/main      # branch per epoch
      refs/tags/epoch/{epoch_id}/v{N}       # tag per generation
      refs/tags/epoch/{epoch_id}/v{N}-rejected
  worktrees/                         # NEW — checkouts for parallel runs
    {run_id}/
      <agent source tree>
  lineage.json                       # unchanged
```

Key shapes:

- **One git repo for the whole workspace** (NOT per-epoch).
- **Each epoch is a set of refs** namespaced under
  `epoch/{epoch_id}/`:
  - `epoch/{id}/main` — branch: head of the promoted lineage
  - `epoch/{id}/v0`, `v1`, `v3`, ... — tags for promoted
    generations
  - `epoch/{id}/v{N}-rejected` — tags for rejected attempts
    (recoverable)
- **Commit messages carry experiment metadata** with a
  `---zicato-meta---` sentinel followed by the structured
  Experiment payload (hypothesis + patches + outcome). Plain
  `git log` and `git diff` show the raw changes; tools parse
  the meta block for the typed view.
- **Cross-epoch parentage via normal commits.** Each epoch's
  `v0` is a regular commit whose parent is whatever HEAD
  pointed at when the previous epoch closed. If the operator
  made changes between epochs, those appear naturally as the
  v0 diff. If they made none, the v0 commit is empty (or we
  just tag the previous HEAD as the new epoch's v0). **No
  `--orphan` branches.** The "two unrelated harnesses in the
  same workspace" case is handled by `zicato workspace new`
  instead.

### 3.2 G0 — Spike: prove git can hold a single generation

**Scope.** Write a script that takes a v0 snapshot directory
plus its `experiment.json` and produces an equivalent git
commit. Verify `git log -p` shows the expected diff against
the parent generation.

**Deliverable.** `tools/spike_git_backend.py` — a one-off
script; not part of the shipped CLI. Demonstrates feasibility
and surfaces the unknowns (commit-message size limits, how
binary blobs in the snapshot are handled, the right way to
encode `---zicato-meta---`).

**Dependencies.** None — this is the first phase.

**Effort.** 1-2 days.

**Acceptance.** A 10-generation toy epoch can be replayed from
an existing directory backend into a git backend; `git log
epoch/test/main` shows 10 commits with parseable meta blocks;
`git diff epoch/test/v1 epoch/test/v2` shows the expected
mutation diff.

### 3.3 G1 — Repo bootstrap + first commit

**Scope.** `zicato workspace migrate-to-git` (or
`zicato repo init` on a fresh workspace) creates
`.zicato/repo/` as a bare-ish private git repo (working tree
not used for orchestration; checkouts go through worktrees in
G3). Imports the current epoch's v0 snapshot as
`epoch/{epoch_id}/v0`.

**Deliverable.** CLI command + the repo initialiser module.

**Dependencies.** G0.

**Effort.** 2-3 days.

**Acceptance.**

```
$ zicato workspace migrate-to-git
[migrate] reading current epoch: initial
[migrate] importing v0 snapshot ... done (357 files, 8.4MB)
[migrate] tagging as epoch/initial/v0 ... done
[migrate] writing .zicato/repo/HEAD ... done

$ git -C .zicato/repo log --all --oneline
abc1234 (tag: epoch/initial/v0) v0: epoch initial baseline
```

### 3.4 G2 — Commits per generation; experiment meta in messages

**Scope.** Apply a new generation's patches as a normal git
commit. Commit message body carries:

```
v3: tighten researcher prompt for citations

---zicato-meta---
{
  "kind": "experiment",
  "epoch": "hardened_research",
  "generation": "v3",
  "round": 3,
  "hypothesis": { ... },
  "patch_ids": [ ... ]
}
---zicato-meta-end---
```

After the tournament's outcome lands, an amended commit (or a
separate `outcome` commit — TBD; see G2 design notes) appends
the outcome block to the same meta JSON.

**Deliverable.** `zicato.storage.git_backend.commit_generation`
plus the message format spec. The `---zicato-meta---` sentinel
parser is a tiny module reused by `zicato repo show` (G6).

**Dependencies.** G1.

**Effort.** 3-4 days. The amend-vs-separate-commit question is
the only design unknown; a spike on the side will resolve it
before this phase ships.

**Acceptance.** `git -C .zicato/repo log epoch/initial/main`
shows the full lineage as commits, each carrying parseable
meta; `zicato repo show v3` renders the hypothesis + outcome
just like the directory backend would render `experiment.json`.

### 3.5 G3 — Worktrees for parallel tournament runs

**Scope.** Tournament runs no longer need their own snapshot
directories. Each worker gets a `git worktree add
.zicato/worktrees/{run_id} <ref>` for parallel checkouts;
cleaned up after run.

**Deliverable.** `zicato.storage.git_backend.checkout_for_run`
+ `release_after_run`. Worker startup path changes:

```python
# v0+1 worker
worktree_path = git_backend.checkout_for_run(
    epoch=epoch_id,
    generation=generation_ref,
    run_id=run_id,
)
try:
    adapter = HarnessAdapter.load(worktree_path)
    result = await adapter.run_entry(entry, sinks=[...])
finally:
    git_backend.release_after_run(run_id)
```

**Dependencies.** G2.

**Effort.** 3-4 days. The shutdown path (worker crashes mid-run
and the worktree directory is left behind) is the tricky bit;
needs a cleanup pass in the resume protocol.

**Acceptance.** Two workers can run concurrently against
different generations on the same workspace without
contention; `git -C .zicato/repo worktree list` shows the
expected entries; after a crashed run, `zicato repo gc --prune`
recovers the orphan worktrees.

### 3.6 G4 — Cross-epoch parentage (no orphan branches)

**Scope.** When `zicato epoch new <name>` is called:

1. The previous epoch's final promoted generation is identified
   (the `epoch/{prev}/main` HEAD).
2. A new commit is created on a new branch
   `epoch/{new}/main`, parented to the previous epoch's HEAD.
3. If the operator made changes between epochs (`zicato register
   --rerun` or similar), those changes ARE the new commit's
   diff. If they made no changes, the commit is empty (`git
   commit --allow-empty`).
4. The new commit is tagged as `epoch/{new}/v0`.

**Deliverable.** Refactored `epoch new` flow + the empty-commit
strategy.

**Dependencies.** G2.

**Effort.** 2 days.

**Acceptance.** `git log --all --oneline --graph` shows a
contiguous DAG across epochs; `git diff epoch/initial/main
epoch/hardened_research/main` shows the cumulative changes.

### 3.7 G5 — Migration tooling

**Scope.** `zicato workspace migrate-to-git`:

1. Pre-flight checks (workspace is locked, no in-flight
   `evolve`, no rejected generations newer than the latest
   promote that the operator hasn't reviewed).
2. Walks every epoch's `generations/` directory; per
   generation:
   - For v0: import the snapshot as a commit on a new branch.
   - For vN (N ≥ 1): apply the patches to the worktree, commit
     with the meta block.
3. Removes `generations/` from each epoch directory (data is
   now in git).
4. Writes `.zicato/config.json` with `storage_backend: "git"`.

The migration is idempotent on failure: re-running picks up
where it left off.

**Deliverable.** The full CLI command plus the migration
module.

**Dependencies.** G4.

**Effort.** 4-5 days. Effort is in edge cases — generations
where the snapshot has untracked changes (a botched manual
edit), generations where the patches don't apply cleanly to the
parent (storage bug, but real), generations where
`experiment.json` is malformed. Each case has a recovery path.

**Acceptance.** A representative workspace with multiple
epochs migrates cleanly; `zicato journal show` on the
post-migration workspace renders the same content as
pre-migration; `zicato repo log` shows the full lineage as git
commits.

### 3.8 G6 — Operator commands: `zicato repo`, `zicato log`, `zicato diff`, `zicato show`

**Scope.** Thin CLI wrappers over git that surface
zicato-meaningful views:

```
zicato repo                  # print path to .zicato/repo/
zicato repo gc               # garbage-collect rejected branches older than N
zicato repo init             # initialize (called by migrate-to-git)
zicato log                   # like git log; filters out non-experiment commits
zicato log --epoch <name>    # log for one epoch
zicato diff <gen-a> <gen-b>  # like git diff but resolves zicato gen refs
zicato show <gen-id>         # show the experiment (hypothesis + outcome) plus the patch diff
```

`zicato show v3` is the key one — it renders both:

```
$ zicato show v3
Generation v3 — epoch hardened_research — round 3

Hypothesis
  core_idea: Tighten researcher's system prompt for citations
  ...

Outcome
  drift_loss_delta: -0.18
  pass_rate_delta:  +0.05
  decision:         promote

Diff against parent v2:
diff --git a/researcher/agent.py b/researcher/agent.py
...
```

**Deliverable.** A handful of CLI subcommands; each is a thin
wrapper around `git log`, `git diff`, `git show` plus
meta-block parsing.

**Dependencies.** G2 (meta-block parser).

**Effort.** 3-4 days.

**Acceptance.** Each command produces the expected output on a
test workspace; output is stable enough for scripting.

### 3.9 G7 — Subprocess worker module caching

**Scope.** Workers currently re-import the adapter module per
run (cold ~100-500ms). Once git backend lands, a worker pool
becomes attractive: keep a pool of warm Python interpreters,
hand each a worktree path + entry to run, reuse for the next
run.

The git backend makes this easier than v0 — workers no longer
each need their own snapshot directory; they each need a
worktree, which is cheap.

**Deliverable.** `zicato.tournament.worker_pool` plus the
orchestrator-side dispatch logic.

**Dependencies.** G3 (worktrees).

**Effort.** 4-5 days. The hard parts are:

- Worker death and pool replenishment.
- Adapter state contamination between runs in the same worker
  (must verify the adapter cleanly restarts between runs; if
  not, fall back to fresh-worker-per-run for that adapter).
- Inter-worker isolation (one worker's pathological run
  should not affect another worker's run — but they're in the
  same pool, so this needs care).

**Acceptance.** A 10-entry tournament completes faster with
the pool than without (target: 30%+ reduction in total
wall-clock).

### 3.10 G8 — `zicato bisect`, `zicato blame`

**Scope.** Git superpowers as zicato operator surface:

- `zicato bisect <gen-good> <gen-bad>` — find which generation
  introduced a regression on a specific entry or drift kind.
  Powered by `git bisect` + a per-step run.
- `zicato blame <file:line>` — for a line in the inner-harness
  source, find the generation that last touched it and the
  hypothesis behind that change.

**Deliverable.** Two CLI subcommands.

**Dependencies.** G6.

**Effort.** 3 days.

**Acceptance.** On a workspace where v5 introduced a regression
that v8 hadn't caught yet, `zicato bisect v3 v8 --entry
contradictory_brief` correctly identifies v5 as the culprit
generation.

### 3.11 G9 — Cross-epoch operator views

**Scope.** Now that all generations live in one repo, queries
that span epochs become trivial:

- `zicato journal show --since-epoch initial` — render journals
  across multiple epochs.
- `zicato log --grep CONFABULATION_RISK` — find every
  generation whose hypothesis mentioned `CONFABULATION_RISK`.
- `zicato lineage --format mermaid` — render the cross-epoch
  DAG as a Mermaid diagram.

**Deliverable.** Cross-epoch extensions to existing commands.

**Dependencies.** G6.

**Effort.** 2-3 days.

**Acceptance.** Each command produces expected output on a
multi-epoch test workspace.

### 3.12 G10 — `GenerationStore` protocol extraction

**Scope.** With both backends implemented and consumers
working with each, extract the protocol. Refactor consumers to
depend on the protocol rather than picking a backend directly.
This is the **rationalization phase** — making the two
backends interoperable instead of just coexisting.

**Deliverable.** `zicato.storage.protocol.GenerationStore` plus
refactored consumers.

**Dependencies.** G2, G3, G4 (the substantive git work), and
the directory backend (which has existed since v0).

**Effort.** 4-6 days. Effort is in finding the right method
boundary — small enough that both backends implement it
naturally, large enough that the consumer code is simple.

**Acceptance.** Every consumer in the table in §2.3 depends on
the protocol; both backends pass the same test suite; an
operator can choose backend at `zicato init` time (`--storage
git` or `--storage directory`).

## 4. Total effort + sequencing

| Phase | Effort (days) | Dependencies | Parallelizable? |
|---|---|---|---|
| G0 (spike) | 1-2 | — | — |
| G1 (repo bootstrap) | 2-3 | G0 | — |
| G2 (commits + meta) | 3-4 | G1 | — |
| G3 (worktrees) | 3-4 | G2 | parallel with G4 |
| G4 (cross-epoch parentage) | 2 | G2 | parallel with G3 |
| G5 (migration) | 4-5 | G4 | — |
| G6 (operator commands) | 3-4 | G2 | parallel with G5 |
| G7 (worker pool) | 4-5 | G3 | post-G5 |
| G8 (bisect/blame) | 3 | G6 | post-G5 |
| G9 (cross-epoch views) | 2-3 | G6 | post-G5 |
| G10 (protocol extraction) | 4-6 | G2, G3, G4 | end of the line |

Total: ~30-40 days of focused work. The sequencing puts the
substantive git work (G0-G5) on the critical path; the
ergonomic additions (G6-G10) parallelize once the foundation
is in.

**Recommended order.** G0 → G1 → G2 → G3 / G4 (parallel) → G5
→ G6 / G7 / G8 / G9 (parallel) → G10.

## 5. Why git, why one repo, why these refs

This section names alternatives considered and rejected.

### 5.1 Why git at all

Alternatives:

- **Custom binary delta store.** Smaller than git for prompt
  changes; gives nothing else.
- **SQLite blobs.** Smaller; no tooling.
- **Plain directories** (v0). Works; doesn't dedupe across
  generations; no diff/log/blame.

Why git wins: native tooling for the most common operator
questions ("what changed in v5?", "what's the diff between v3
and v8?", "which generation last touched this line?"). Every
shop has git installed. The data is intrinsically file-shaped
and git is the file-shaped versioner. No new investment in
domain-specific tooling.

### 5.2 Why one repo for the workspace, not one per epoch

Alternatives:

- **One repo per epoch.** Cleaner isolation; no cross-epoch
  diff/log natively.

Why one wins:

- Cross-epoch diff (`git diff epoch/e0/main epoch/e1/main`)
  works out of the box.
- Blob-level dedup across epochs — the same prompt module
  unchanged across 5 epochs is one blob.
- Worktrees from any branch in one repo are cheap; worktrees
  across repos are not a thing.
- The user's outer harness repo is untouched; `.zicato/repo/`
  is an entirely private internal repo, so the "polluting the
  user's git history" concern doesn't apply.

### 5.3 Why branches per epoch + tags per generation

Alternatives:

- **One long branch with everything.** No isolation; rejected
  generations clutter `git log`.
- **One branch per generation.** Branch namespace explosion;
  hard to reason about.

Why branches + tags:

- Branches partition epochs cleanly. `git log epoch/initial/main`
  is exactly that epoch.
- Tags name immutable promoted generations. `epoch/initial/v3`
  always means that generation forever.
- Rejected generations live under `epoch/initial/v4-rejected`
  — separate tag namespace, doesn't clutter the main lineage
  but is recoverable.

### 5.4 Why not orphan branches for new epochs

We considered: every new epoch starts with `git checkout
--orphan` so the new epoch's history is entirely separate.

Rejected because the operator's mental model is "every epoch
continues from the previous one". Orphan branches break
`git diff epoch/e0 epoch/e1`. The empty-commit-as-bridge model
(§3.6 / G4) gives the same logical structure with continuous
history.

The "two unrelated harnesses in the same workspace" case (rare;
target 3 nesting could create it) is handled by `zicato
workspace new` instead — that creates a separate workspace
with a separate `.zicato/repo/`.

### 5.5 Why meta in commit messages, not in `notes/` or `tags`

We considered:

- `git notes add` per commit. Notes have known UX issues (don't
  show in default `git log`, don't fetch with the branch).
- Tag annotations carrying the meta block. Tags can carry text
  but the relationship between tag and commit is one-to-one,
  so this doesn't fit "outcome amended after commit".

Why commit-message body:

- Visible in default `git log`.
- Transports with the commit (fetch/push, mirror, etc.).
- Easy to parse with a sentinel-delimited block.
- Limit (subject + body): well under git's 64KB practical
  comfort zone for our payload size.

The trade-off is that commit messages are normally short. We
warn operators in the docs that `git log` output is verbose by
design; they can use `--oneline` or `zicato log` (which strips
the meta block from the output) for the compact view.

## 6. v0 → v0+1 migration semantics

`zicato workspace migrate-to-git` is the one-shot conversion.
It is **destructive but reversible**: a `--dry-run` walks the
conversion without changing disk; without `--dry-run`, the
command updates the workspace in place. A pre-migration backup
in `.zicato/migrations/<ts>.tar.gz` is created automatically.

```
$ zicato workspace migrate-to-git --dry-run
[migrate dry-run] would import 23 generations across 3 epochs
[migrate dry-run] estimated post-migration disk: 124MB (currently 2.1GB; 94% reduction)
[migrate dry-run] would remove .zicato/epochs/*/generations/
[migrate dry-run] would create .zicato/repo/, .zicato/worktrees/
[migrate dry-run] would update .zicato/config.json: storage_backend = "git"
[migrate dry-run] reversibility: pre-migration backup will be written to .zicato/migrations/2026-05-14T13:00:00.tar.gz
```

The migration runs in a single atomic step (succeed → repo is
ready; fail → repo half-written but workspace untouched
because we writes to a staging directory first).

After migration:

- `zicato evolve` writes new generations directly to git.
- `zicato show v0` of any pre-existing generation produces
  identical output (the meta-block format is canonical
  regardless of when the generation was created).
- `git log` walks the lineage.

Reverting is supported but lossy: a pre-migration backup is in
`.zicato/migrations/`; restoring it puts the workspace back to
the directory layout. New generations created post-migration
would be lost unless explicitly exported first.

## 7. Three storage concerns

zicato has **three** distinct storage concerns, and they get
three different substrates. Conflating them — "just put it all in
one store" — would be wrong for at least two of the three. This
section draws the lines so the boundaries are explicit; the same
split is laid out from the index side in
[ANALYTICAL-INDEX.md §5](ANALYTICAL-INDEX.md#5-where-sqlite-is-and-is-not-used).

| Concern | What it is | Substrate | Why this substrate |
|---|---|---|---|
| **Generation source trees** | The post-apply inner-harness source at each generation. | **git** (v0+1; directory snapshots in v0). This document. | The data is intrinsically file-shaped; git is the file-shaped versioner and gives `diff` / `log` / `blame` / `bisect` for free (§5.1). SQLite blobs would be smaller and give *no tooling*. |
| **Per-run event capture** | The `goldfive.v1.Event` stream of each tournament run. | **`events.jsonl`** — one append-only line-delimited file per run. See [TELEMETRY.md](TELEMETRY.md). | The access pattern is append-while-running, tail-for-the-log-panel, stream-to-SSE, replay-once-in-the-reducer. An append-only JSONL file wins every one of those; a row-per-event table would add write contention during the run for no query benefit (events are never queried *across* runs — the reduced `LossProfile` is). |
| **Cross-run analytical views** | The relational projection that answers `GROUP BY` / `JOIN` questions across many generations. | **`.zicato/index.db`** — a SQLite analytical index. See [ANALYTICAL-INDEX.md](ANALYTICAL-INDEX.md). | A relational index is exactly the right shape for cross-run aggregates. It is **derived**, never canonical: fully rebuildable from the two stores above via `zicato reindex`. |

The principle: **git for the source trees, JSONL for the event
capture, SQLite only for the derived cross-run index.** Each
substrate fits one access pattern; none is forced to do another's
job.

A note on layering: the analytical index does not *replace* the
storage layer in this document — it *projects* it. The
`index.db`'s `runs.events_path` column points *at* an
`events.jsonl` file; it does not contain the events. The
`generations` and `experiments` index tables are derived from the
canonical `gen_score.json` / `experiment.json` files (directory
backend) or their git equivalents (v0+1 backend). The storage
layer stays the source of truth under either backend; the index
is a fast read sidecar on top of whichever backend is active.

The v0 "no SQLite, no embedded DB" framing in
[RATIONALE.md §7](RATIONALE.md#7-why-filesystem-layout-not-sqlite)
refers specifically to the **canonical** layer — every artifact
that *is* the source of truth stays a human-readable file. That
position is unchanged. The analytical index is the *index
sidecar* that the same rationale anticipated ("when pattern
queries become a bottleneck, the right move is to add an index
sidecar ... regenerable from the filesystem"). It is the
permitted, derived use of SQLite — not a contradiction of the
filesystem-native rule.

## 8. Cross-references

| Topic | Document |
|---|---|
| Where `experiment.json` and per-generation directories fit | [EPOCHS-AND-JOURNALING.md](EPOCHS-AND-JOURNALING.md) |
| The `.zicato/runtime/` layer (unchanged across backends) | [RUNTIME.md](RUNTIME.md) |
| Worker subprocess design (pool comes in G7) | [ROBUSTNESS.md](ROBUSTNESS.md) §2.3 |
| `MutationPoint.id` references that patches carry | [MUTATION-SURFACE.md](MUTATION-SURFACE.md) |
| Per-run event capture — `events.jsonl`, one file per run | [TELEMETRY.md](TELEMETRY.md) |
| The `.zicato/index.db` SQLite analytical index — schema, discipline, `reindex` | [ANALYTICAL-INDEX.md](ANALYTICAL-INDEX.md) |
| CLI surface (`zicato repo`, `zicato log`, `zicato diff`, `zicato reindex`, etc.) | [CLI.md](CLI.md) |
| Why filesystem-native (canonical layer is no SQLite) | [RATIONALE.md](RATIONALE.md) §7 |
