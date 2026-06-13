# Generation Isolation & Delta Materialization

> Status: design / not yet implemented. Tracking issue: see the "generation
> isolation + delta materialization" issue.

## Why this exists

Every generation in zicato is materialized as a **full directory copy** of the
target tree — once when the generation snapshot is written by the generation
store, and again as the per-run *ephemeral* worker snapshot the tournament
worker executes against. The mutation is applied into that copy; the worker runs
the target's harness inside it; results are written next to it.

That works, but it is the wrong shape for two reasons the operator named
directly:

1. **It does not cheaply isolate.** We pay a full `copytree` *in order to* keep a
   generation's writes from touching its parent or siblings. The isolation is a
   side effect of the copy, not an enforced property — a write to an absolute
   path still escapes (the scratch-dir scope is advisory), and we pay O(whole
   tree) for the privilege.
2. **It does not scale.** Per-generation cost is O(size of the whole target),
   multiplied across challengers × board units × replicates × rounds. For a
   non-trivial target this dominates disk and wall-clock and grows with every
   epoch.

**The goal (operator's framing):** *contain the mutation and execution, and make
each generation cheaper by only copying the delta of what has changed.*

This document is about the **materialization strategy of the generation store +
the worker's execution sandbox** — how the on-disk tree a generation runs in is
built and confined. It is **not** about credential/secret handling or network
egress (those are a separate, lower-priority concern; the existing opt-in worker
env-scrub remains the baseline there).

## Goals

1. **Containment (structural, not advisory).** A generation's mutation and its
   execution writes are confined to *that generation's own layer*. It cannot
   modify its parent snapshot, a sibling generation, or canonical `.zicato/`
   state. The parent base is read-only by construction.
2. **Cheap delta.** Per-generation materialization cost ≈ O(changed files/blocks),
   not O(whole tree) — in both disk and wall-clock. The persisted generation
   artifact is the delta.
3. **Graceful fallback.** Degrade to a correct (if slower) path where the host
   filesystem/kernel lacks a fast mechanism. Full copy is the floor, always
   available.
4. **Behaviour-preserving.** The tree the worker *sees* and the artifact the store
   *persists* are byte-identical to today's copytree result. Delta is an
   implementation detail of **how** the tree is built, never **what** it contains
   — the contract hash, lineage, promotion semantics, and the parity goldens are
   untouched.

## Non-goals

- Credential/secret isolation, network egress control (tracked separately;
  env-scrub stays the baseline).
- Changing the logical generation model (lineage DAG, promotion, contract hash).
- Hard adversarial OS confinement of *arbitrary escape* (absolute-path writes
  outside the tree, syscalls) — that is the OS-sandbox layer (mount namespaces /
  Landlock), which composes with this but is driven by supervisor spawn-side
  ownership and is out of scope here.

## The materialization strategies

The generation store is already a pluggable abstraction
(`DirectoryGenerationStore`, `GitGenerationStore`). This adds a **materialization
strategy** seam under it, chosen by host capability at runtime. The candidates,
with trade-offs:

### A. overlayfs — *recommended primary on Linux*

Mount an overlay where `lowerdir` = the parent generation snapshot **(read-only)**,
`upperdir` = a fresh empty dir (this generation's delta), and the merged
mountpoint is the worker's working directory. The worker sees the full tree;
**every write lands only in `upper`.**

- **Cost:** near-zero to materialize (a mount, no copy). Disk = only the delta.
  Granularity is per-file (overlay copies-up a whole file on first write).
- **Containment:** **strong + structural** — `lowerdir` is read-only at the kernel
  level, so the worker *physically cannot* modify the parent or siblings. The
  `upper` layer **is** the parent→child diff.
- **Portability:** Linux only. Unprivileged via `fuse-overlayfs` or user
  namespaces (rootless); otherwise a privileged mount → ties into supervisor
  spawn-side ownership.
- **Teardown:** unmount; keep `upper` (the delta) as the persisted generation
  artifact, or discard for ephemeral runs.
- **Synergy:** because `upper` is literally the diff, the supervisor's
  diff-containment attestation (issues #47/#48) becomes **trivial and exact** —
  "did the mutation escape `mutable_trees`?" is a listing of the `upper` dir, no
  parent↔child tree walk required.

### B. CoW reflinks / FS snapshots — *recommended where the FS supports it*

`cp --reflink=auto` (or a btrfs/zfs subvolume / dataset snapshot) of the parent →
child. Unchanged blocks are shared copy-on-write; a write diverges only the
touched blocks.

- **Cost:** cheap copy (metadata + CoW). **Block-granular** delta — finer than
  overlay's whole-file copy-up.
- **Containment:** the parent is untouched because writes CoW-diverge — but this
  is containment *by divergence*, not *by read-only enforcement*. A careless
  absolute-path write outside the tree still escapes (needs the OS-sandbox layer
  for adversarial confinement). For correctness-isolation it is strong.
- **Portability:** needs a reflink-capable FS (btrfs, xfs-with-reflink, zfs,
  APFS). Detect at runtime; fall back if absent.
- **Teardown:** `rm` the child subvol/dir; CoW blocks free when the last ref drops.

### C. hardlink copy (`cp -al`) — *fallback, with a sharp caveat*

Copy the tree replacing file copies with hardlinks; unchanged files share inodes.

- **Cost:** cheap (link, no data copy); disk = shared inodes until a write.
- **Containment:** **weak / dangerous.** A tool that writes *in place* (truncate,
  `O_WRONLY` without unlink-first) mutates the **shared inode** → corrupts the
  parent *and* every sibling that links it. Safe **only** if every writer does
  copy-then-rename (which breaks the link first). The patch applier rewrites whole
  files (safe), but **arbitrary executed target code may not** — so this is unsafe
  for the execution phase.
- **Verdict:** acceptable for read-mostly material; **not** for the directory the
  target's code runs and writes in.

### D. git content-addressed store + worktree — *storage dedup, not on-disk delta*

One commit per generation in a content-addressed object store; materialize a run
via `git worktree add` or, preferably, `git archive <tree> | tar -x` into a clean
dir.

- **Cost:** storage is delta/dedup'd (git objects). **But the on-disk working tree
  is a full checkout** — every file written out. So this is *delta-in-storage,
  full-on-disk*; materialization is checkout cost, cheaper than copytree only when
  the checkout itself lands on a CoW filesystem.
- **Containment:** a **live worktree shares `.git`** → a worker could `git
  checkout` a sibling/parent or read history. Prefer a *detached* `git archive`
  export (no `.git`) for the execution copy. Parent *objects* are immutable;
  on-disk siblings are not auto-protected.
- **Portability:** portable; gives history/lineage-as-commits and cross-host
  transport for free.
- **Verdict:** the best **storage** layer (dedup, portability, an auditable
  per-generation commit) — combine it with overlay/reflink for the **on-disk
  execution** layer.

### E. full copytree — *the floor (status quo)*

Universal, simple, strong isolation, O(whole-tree) cost. Keep as the guaranteed
fallback when nothing faster is available.

## Trade-offs at a glance

| Strategy | Materialize cost | Delta granularity | Containment | Portability | Teardown | Diff = artifact? |
|---|---|---|---|---|---|---|
| **A. overlayfs** | ~0 (mount) | per-file | **strong (RO lower)** | Linux (rootless via fuse) | unmount | **yes (upper)** |
| **B. reflink/CoW** | cheap copy | **per-block** | strong (by divergence) | reflink FS only | rm | derivable |
| C. hardlink | cheap | per-file | **weak/unsafe for exec** | any POSIX | rm | no |
| D. git+worktree | checkout | storage-only | shares `.git` (use archive) | portable | rm + gc | commit |
| E. copytree | **O(tree)** | n/a (full) | strong (full copy) | universal | rm | no |

## Recommendation

Make materialization a **pluggable strategy on the GenerationStore, selected by
host capability at runtime**, with this preference order:

1. **overlayfs** (or `fuse-overlayfs` unprivileged) on Linux — best containment +
   cheapest + the delta *is* the diff.
2. **reflink/CoW** where the FS supports it — strong, block-granular, no special
   mount or privilege.
3. **full copytree** — universal fallback; always correct.

Optionally back the *store* with the **git content-addressed object store (D)** for
storage dedup, portability, and per-generation commits — orthogonal to the on-disk
layer choice.

The **containment guarantee** comes from the parent base being read-only (overlay
`lower` / immutable git objects / a read-only bind), so a generation cannot mutate
its ancestors or siblings. **Hard adversarial confinement** (absolute-path escapes)
is the OS-sandbox layer — separate, driven by supervisor spawn-side ownership —
which composes cleanly on top of any strategy here.

## Integration points

- **GenerationStore materialization** (`DirectoryGenerationStore` /
  `GitGenerationStore`) and the **worker ephemeral snapshot**
  (`tournament/worker_transport`) — replace the `copytree` with the selected
  strategy behind a stable seam.
- **Persisted artifact:** for overlay, the `upper` delta; for reflink/git, the
  diverged tree / commit. The *logical* artifact the store exposes (a snapshot dir
  / a commit ref) stays unchanged, so everything downstream (apply → run → score →
  promote → contract hash) is untouched.
- **Supervisor synergy:** an overlay `upper` layer is the exact parent→child diff,
  which makes the supervisor's diff-containment attestation (issue #48) precise and
  cheap, and feeds the promotion-veto work (issue #47).
- **Teardown / GC:** the supervisor's orphan-reaper + ephemeral-snapshot GC
  (PR #46) already removes leaked `ztw-snap-*` dirs on a confirmed orchestrator
  death; extend it to also **unmount leaked overlays / drop leaked subvolumes**, so
  a crash never leaves a mount or CoW subvol stranded.

## Risks

- **overlay/fuse availability + privilege** — rootless `fuse-overlayfs` vs a
  supervisor-spawned privileged mount. The capability probe must be reliable and
  the fallback automatic.
- **reflink detection correctness** — `cp --reflink=auto` silently falls back to a
  full copy on a non-reflink FS, defeating the purpose without erroring; probe the
  FS explicitly and record which strategy was used.
- **hardlink in-place-write corruption** — never use C for the execution layer.
- **teardown discipline** — leaked mounts/subvols accumulate; the supervisor GC
  must own this (above).
- **behaviour-preservation** — the materialized tree and the persisted artifact
  must be byte-identical to the copytree result. Gate every step on `parity.sh`
  (the deterministic mock target must produce identical goldens regardless of which
  strategy built the tree).

## Phasing

- **P1 — seam + fallback.** Introduce the pluggable materialization seam + host
  capability detection, with full `copytree` as the only implementation. Pure
  refactor, behaviour-identical, parity-gated.
- **P2 — reflink/CoW fast path.** Cheapest to add (no privilege, no mount); detect
  the FS and use it, else fall back.
- **P3 — overlayfs fast path.** Best containment; the privileged mount ties to
  supervisor spawn-side ownership (or `fuse-overlayfs` unprivileged). Wire the
  `upper` delta into the persisted artifact and the supervisor diff-containment
  check.
- **P4 — git object-store backing (optional).** Content-addressed dedup +
  portability + per-generation commits, under whichever on-disk strategy is active.
