# Generation Isolation & Delta Materialization

> **Status. SUPERSEDED — retained as a decision record; do not build from
> it.** Written 2026-06-13 as a forward-looking proposal to replace the full
> per-generation `copytree` with a host-capability-selected materialization
> strategy (overlayfs · reflink-CoW · copytree fallback, optionally git-backed).
> The problem it names is **real and has since been solved** — but by the
> option this note relegated to an *optional, storage-only* layer (option D,
> git), not by the kernel/filesystem strategies it recommended. The superseding
> design is [`STORAGE.md`](STORAGE.md) §7 (the git-backed generation store,
> now the **default** backend) plus the per-run `checkout_ephemeral` seam it
> carries. **No overlayfs, reflink, or hardlink layer is planned or will be
> built.** §0 records what shipped and why; the comparative analysis (from
> "The materialization strategies" onward) is kept because it is the record of
> *which alternatives were considered and rejected*, and because two of its
> own hazards were the ones the shipped design had to design around.
>
> Superseding / companion reading: [`STORAGE.md`](STORAGE.md) §7,
> [`ROBUSTNESS.md`](ROBUSTNESS.md) (subprocess-isolated runs),
> [`ARCHITECTURE.md`](ARCHITECTURE.md).
>
> Provenance note: an earlier branch audit in
> [`ZICATO-SYSTEM-ANALYSIS.md`](ZICATO-SYSTEM-ANALYSIS.md) recorded this
> document's branch as a pre-merge snapshot of an *already-merged* design
> doc. That was wrong — it had never been merged. It lands here, reheadered,
> so the places that already cite `GENERATION-ISOLATION.md` by name resolve
> to a document that tells the truth about today's tree.

## 0. What shipped instead (the as-built answer)

Everything after this section is the June 2026 proposal, corrected in place
where it asserts anything about the tree. Read this section first; it is the
part that is true.

**The problem was real.** Per-generation full-tree copies did dominate disk
for a long lineage, and per-run isolation *was* being paid as a redundant
second `copytree` on top of a checkout the git backend had already
materialised. Both are fixed.

**The fix was git, at two layers.**

1. **Storage layer — the generation store is content-addressed by default.**
   `GitGenerationStore` (`zicato.epoch.git_genstore`) stores each generation
   as a commit tagged `epoch/{epoch_id}/{generation_id}` on an `epoch/{id}`
   branch. `zicato init` records `generation_source_backend: "git"`
   explicitly. `zicato.epoch.genstore.default_generation_store` validates
   the configured value and never infers a backend from workspace contents
   ([`STORAGE.md`](STORAGE.md) §5.2).
   `DirectoryGenerationStore` remains the dependency-free fallback under
   `generation_source_backend: "directory"`. Git's object store *is* the delta
   representation this note wanted: a module unchanged across 20 generations
   is one blob referenced by 20 commits, not 20 copies. See
   [`STORAGE.md`](STORAGE.md) §7.1–§7.2.

2. **Execution layer — per-run isolation moved behind the store protocol.**
   `GenerationStore.checkout_ephemeral(epoch_id, generation_id, run_id)`
   returns an `EphemeralCheckout(working_dir, scratch_dir, cleanup)`. The
   directory backend implements it with the historical copy
   (`copy_checkout_ephemeral`); the git backend implements it as
   `git worktree add --detach` from the generation tag. The tournament
   transport (`zicato.tournament.worker_transport._checkout_run_snapshot`)
   now *routes* to the store when the store owns the generation, and falls
   back to `copy_checkout_ephemeral` only for a store-unmanaged generation
   (an ad-hoc caller pointing `snapshot_root` at an arbitrary tree). A git
   run no longer pays a worktree checkout *and* a full copy.

3. **A third materialization path this note did not anticipate:
   off-namespace scratch derivation.** `GenerationStore.derive_scratch`
   applies a patch set to a parent tree all-or-nothing into a caller-owned
   temp root, creating **no** commit, tag, branch, or `generations/` entry —
   so a scratch tree is provably invisible to `list_generations`, the GC, the
   reindex, the lineage reader, and every dashboard reader. That is what
   makes the best-of-N slate gatherable: each candidate slot validates into
   its own disjoint scratch root, and the one `derive_generation` into the
   canonical `next_id` tree — the shared step that used to block the gather —
   happens exactly once, for the winner, in
   `BestOfNProposerAgent._mount_chosen` (`zicato.proposer.best_of_n`, through
   the round's shared `validate_experiment` hook,
   `zicato.evolve.round.build_post_apply_validator`). This is "cheap delta per
   candidate" achieved by *not entering the namespace* rather than by a
   filesystem trick.

**Why the recommended strategies were not built.** Once (1) removed the
storage cost and (2) removed the redundant per-run copy, an overlayfs or
reflink layer would have been a *third* isolation mechanism buying nothing:

- Measurement decided it. `git worktree add --detach` was benchmarked at
  **3–18× faster than the equivalent `shutil.copytree`** (16 concurrent
  adds: ~14–22 ms on a 60-file/~120 KB tree, ~41 ms on a 500-file/~2 MB
  tree, versus ~70 ms / ~525 ms for the copies). Against runs measured in
  seconds-to-minutes, sub-30 ms materialization is not a cost worth a kernel
  dependency. The measurements and the rejected alternatives are recorded in
  `GitGenerationStore.checkout_ephemeral`'s docstring.
- The portability tax was unacceptable for the primary recommendation.
  Overlay is Linux-only and wants either `fuse-overlayfs` or a privileged
  mount; reflink needs a specific filesystem and `cp --reflink=auto`
  *silently* degrades to a full copy where it is absent. Both would have
  needed a capability probe plus the copytree fallback anyway — i.e. all the
  fallback complexity, for a win already delivered.
- The containment argument was answered differently. The Goals section below
  wanted structural read-only containment. Git supplies it at the layer that
  matters: a child derives from the **commit**, never from a worktree, so a
  runtime write inside a checkout cannot reach the canonical tree or a
  sibling — pinned by
  `tests/test_genstore_conformance.py::test_checkout_ephemeral_stray_write_never_reaches_canonical`
  and `test_checkout_ephemeral_materialises_isolated_tree`, both parametrised
  over both backends.

**Where the shipped design had to design around this note's own hazards.**
Two of the cautions below turned out to be load-bearing:

- Strategy D warned that *"a live worktree shares `.git` → a worker could
  `git checkout` a sibling/parent or read history"*, and proposed a detached
  `git archive` export instead. The hazard was real; the proposed remedy was
  benchmarked and **rejected** (serial cost ~1.5–2.5× a worktree add, and the
  Python-side `tarfile` extraction serialises catastrophically under threads —
  16 concurrent: ~163 ms / ~1.36 s versus 14–41 ms for adds). The shipped
  answer keeps the worktree but **detaches it immediately**: the `.git`
  pointer file is unlinked and the registration pruned right after the add,
  leaving a plain throwaway tree with no path back into the private repo and
  no cleanup path that depends on git state. The `prune → add → detach →
  prune` window is serialised per repo by `_worktree_admin_lock`, because
  git's own repo lock covers each *command* and a concurrent prune can
  otherwise collect a half-registered entry (raced in
  `tests/test_git_genstore.py`).
- The Risks section flagged teardown discipline. That is owned two ways: the
  `ztw-snap-*` mkdtemp parent placement in the OS temp dir
  (`EPHEMERAL_SNAPSHOT_PREFIX`) is a *contract on every backend*, matched by
  the Rust supervisor's crash reaper (`crates/supervisor/src/reap.rs`,
  `SNAPSHOT_PREFIX` / `reapable_snapshot_root`) — so a crashed run's tree is
  reaped even if `cleanup()` never runs; and long-lineage disk growth is
  reclaimed by the snapshot GC this note did not propose at all
  (`zicato.epoch.gc`, operator surface `zicato epoch gc`,
  `tests/test_epoch_gc.py`), which prunes settled *rejected* generations'
  source trees — tags plus worktrees under git, `snapshot/` directories under
  the directory backend — and never touches a record.

**Corollary — one non-goal became a shipped mechanism.** The Non-goals
section excluded enforcement of *"did the mutation escape its sandbox"* as an
OS-sandbox concern. It shipped anyway, as auditing rather than confinement:
the supervisor re-hashes every child snapshot out-of-band
(`crates/supervisor/src/diff_containment.rs`) and alarms on a write outside
the registered `mutable_trees`; an opt-in, default-off in-band twin
(`ScoringWeights.block_on_containment_violation`, enforced pre-persist in
`orchestrator._integrity_block_reason`, mirrored in `evolve/gate.py`) flips a
violating promotion to REJECTED. The note's claim that an overlay `upper`
layer would make this "trivial and exact" is therefore moot: the check
exists, works on a plain tree walk, and needs no overlay to be cheap enough.

**Consequence for the backlog.** The tracking issue — #50, "Generation
isolation + delta materialization (replace per-generation copytree)" — is
answered by the above and should be **closed as superseded**, not implemented.
Nothing in the P1–P4 phasing below is scheduled.

---

*Everything from here on is the original 2026-06-13 note. Its factual
assertions about the tree were true when written and are now historical; each
stale premise is marked inline. The trade-off analysis stands on its own and
is why the record is kept.*

## Why this exists

> **Historical premise — no longer the shipped behaviour.** Under the default
> git backend a generation is a commit (blob-deduped, no copy) and a run
> mounts a `git worktree`, so neither copy described in the next paragraph is
> paid. It is accurate only for `generation_source_backend: "directory"`, the explicit
> no-git fallback — and even there the *second* copy is now the backend's own
> `checkout_ephemeral`, not a transport-layer duplicate of it.

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

*(As-built status noted per goal; §0 has the detail.)*

1. **Containment (structural, not advisory).** A generation's mutation and its
   execution writes are confined to *that generation's own layer*. It cannot
   modify its parent snapshot, a sibling generation, or canonical `.zicato/`
   state. The parent base is read-only by construction. — **MET, differently:**
   the read-only base is the immutable *commit*, not a read-only mount; a child
   derives from the commit and never from a worktree.
2. **Cheap delta.** Per-generation materialization cost ≈ O(changed files/blocks),
   not O(whole tree) — in both disk and wall-clock. The persisted generation
   artifact is the delta. — **MET for storage** (git blob dedup; the persisted
   artifact *is* a delta against the parent commit's tree). **Not met, and
   deliberately not pursued, for the per-run on-disk tree**, which stays a full
   checkout — measured 3–18× cheaper than the copy it replaced, which settled
   the question.
3. **Graceful fallback.** Degrade to a correct (if slower) path where the host
   filesystem/kernel lacks a fast mechanism. Full copy is the floor, always
   available. — **MET:** `generation_source_backend: "directory"` is the no-`git`
   fallback, and `copy_checkout_ephemeral` is still the per-run mechanism for it
   and for store-unmanaged generations.
4. **Behaviour-preserving.** The tree the worker *sees* and the artifact the store
   *persists* are byte-identical to today's copytree result. Delta is an
   implementation detail of **how** the tree is built, never **what** it contains
   — the contract hash, lineage, promotion semantics, and the parity goldens are
   untouched. — **MET, and enforced:** the git checkout is detached precisely to
   give byte parity with the directory backend's view (whose `copytree_ignore`
   filter skips `.git`); the artifact-exclusion set is shared across backends by
   `zicato.epoch.snapshot_scope` (`copytree_ignore` for copies, `gitignore_lines`
   for commits); and both backends are held to one observable contract by
   `tests/test_genstore_conformance.py`.

## Non-goals

- Credential/secret isolation, network egress control (tracked separately;
  env-scrub stays the baseline).
- Changing the logical generation model (lineage DAG, promotion, contract hash).
- Hard adversarial OS confinement of *arbitrary escape* (absolute-path writes
  outside the tree, syscalls) — that is the OS-sandbox layer (mount namespaces /
  Landlock), which composes with this but is driven by supervisor spawn-side
  ownership and is out of scope here. — *See §0's corollary: the* detection *half
  of this shipped as the supervisor's diff-containment attestation. The*
  confinement *half remains unbuilt and out of scope.*

## The materialization strategies

> **Historical framing.** This section enumerates the candidates as they were
> weighed in June 2026; a 2026-07 verdict is recorded per strategy. The seam it
> proposes — "a materialization strategy seam *under* the store" — was **not**
> built. The store *itself* became the seam, via `checkout_ephemeral`.

The generation store is already a pluggable abstraction
(`DirectoryGenerationStore`, `GitGenerationStore`). This adds a **materialization
strategy** seam under it, chosen by host capability at runtime. The candidates,
with trade-offs:

### A. overlayfs — *recommended primary on Linux* → NOT BUILT

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
- **Verdict (2026-07): rejected.** Linux-only plus a privilege/FUSE dependency,
  for a materialization win strategy D already delivered at 3–18× under the
  status quo. The attestation synergy is moot — the parent↔child re-hash shipped
  and is not a bottleneck (`crates/supervisor/src/diff_containment.rs`).

### B. CoW reflinks / FS snapshots — *recommended where the FS supports it* → NOT BUILT

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
- **Verdict (2026-07): rejected.** Filesystem-conditional, and the silent
  `cp --reflink=auto` degradation (see Risks) means the win cannot be relied on
  without an explicit probe. Git blob dedup delivers the storage half
  unconditionally, on any filesystem.

### C. hardlink copy (`cp -al`) — *fallback, with a sharp caveat* → NOT BUILT

Copy the tree replacing file copies with hardlinks; unchanged files share inodes.

- **Cost:** cheap (link, no data copy); disk = shared inodes until a write.
- **Containment:** **weak / dangerous.** A tool that writes *in place* (truncate,
  `O_WRONLY` without unlink-first) mutates the **shared inode** → corrupts the
  parent *and* every sibling that links it. Safe **only** if every writer does
  copy-then-rename (which breaks the link first). The patch applier rewrites whole
  files (safe), but **arbitrary executed target code may not** — so this is unsafe
  for the execution phase.
- **Verdict:** acceptable for read-mostly material; **not** for the directory the
  target's code runs and writes in. *(2026-07: rejected outright — and there is
  no read-mostly layer that would want it.)*

### D. git content-addressed store + worktree — *storage dedup, not on-disk delta* → BUILT (this is the shipped design)

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
- **Verdict (2026-07): chosen for BOTH layers, with two corrections to the
  analysis above.** (i) The "cheaper than copytree only on a CoW filesystem"
  caveat was **wrong** — measured on tmpfs-free local disk (no CoW), `git
  worktree add --detach` is 3–18× faster than the equivalent `copytree`, and
  git's repo lock serialises only the ref/administrative step, so 16-way
  contention costs under 1.5× a serial add. That measurement is what made
  overlay and reflink unnecessary. (ii) The `git archive` preference was
  **benchmarked and
  rejected**; the shipped design gets the same "no `.git`" property by unlinking
  the worktree's `.git` pointer and pruning the registration immediately after
  the add. Both are recorded in `GitGenerationStore.checkout_ephemeral`, which
  also records the rejection of the shared-per-generation-worktree alternative
  (zero per-run cost, but concurrent sibling runs would contaminate each other's
  measurement and the contamination would persist into the next epoch's seed).

### E. full copytree — *the floor (status quo)* → RETAINED as the fallback

Universal, simple, strong isolation, O(whole-tree) cost. Keep as the guaranteed
fallback when nothing faster is available. *(2026-07: exactly what happened —
`copy_checkout_ephemeral` plus `DirectoryGenerationStore`, selected by
`generation_source_backend: "directory"` or by a store-unmanaged generation.)*

## Trade-offs at a glance

| Strategy | Materialize cost | Delta granularity | Containment | Portability | Teardown | Diff = artifact? | As-built |
|---|---|---|---|---|---|---|---|
| **A. overlayfs** | ~0 (mount) | per-file | **strong (RO lower)** | Linux (rootless via fuse) | unmount | **yes (upper)** | rejected |
| **B. reflink/CoW** | cheap copy | **per-block** | strong (by divergence) | reflink FS only | rm | derivable | rejected |
| C. hardlink | cheap | per-file | **weak/unsafe for exec** | any POSIX | rm | no | rejected |
| D. git+worktree | checkout — *measured 3–18× cheaper than copytree* | storage-only | commit is the RO base; checkout detached | portable | rm + gc | commit | **shipped, default** |
| E. copytree | **O(tree)** | n/a (full) | strong (full copy) | universal | rm | no | **retained as fallback** |

## Recommendation

> **Superseded.** This preference order was **not** adopted. The shipped order
> is: git generation store (default) → directory store with
> `copy_checkout_ephemeral` (the no-`git` fallback). There is no
> host-capability probe and no third tier. See §0.

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

*The one clause of the paragraph above that survived is the decisive one:
"immutable git objects" as the read-only parent base. The optional item became
the mandatory one, and the mandatory ones were dropped.*

## Integration points

> **As-built.** The seams named here were the right ones; what landed at each is
> recorded inline.

- **GenerationStore materialization** (`DirectoryGenerationStore` /
  `GitGenerationStore`) and the **worker ephemeral snapshot**
  (`tournament/worker_transport`) — replace the `copytree` with the selected
  strategy behind a stable seam. → **DONE, as
  `GenerationStore.checkout_ephemeral`.** The stable seam is the store protocol
  itself; `worker_transport._checkout_run_snapshot` delegates to it when the
  store owns the generation and otherwise falls back to
  `copy_checkout_ephemeral`. The `EphemeralCheckout` triple deliberately carries
  `scratch_dir` alongside `working_dir` because scratch placement is
  backend-owned: both live under one crash-reapable `ztw-snap-*` parent, so a
  single `cleanup()` (or the supervisor's reaper) removes both.
- **Persisted artifact:** for overlay, the `upper` delta; for reflink/git, the
  diverged tree / commit. The *logical* artifact the store exposes (a snapshot dir
  / a commit ref) stays unchanged, so everything downstream (apply → run → score →
  promote → contract hash) is untouched. → **DONE for the git case:** the
  artifact is the tagged commit, downstream is untouched, `snapshot_path`
  calculates the worktree location without I/O, and `materialize_snapshot`
  checks it out on demand.
- **Supervisor synergy:** an overlay `upper` layer is the exact parent→child diff,
  which makes the supervisor's diff-containment attestation (issue #48) precise and
  cheap, and feeds the promotion-veto work (issue #47). → **MOOT.** The
  attestation shipped as a snapshot re-hash
  (`crates/supervisor/src/diff_containment.rs`) and needs no overlay. Issues #47
  (alarm-only → promotion-veto enforcement) and #48 (tighten the coarse file-set
  check to inside-site line ranges) remain open on their own merits, independent
  of this note — neither now depends on an overlay layer.
- **Teardown / GC:** the supervisor's orphan-reaper + ephemeral-snapshot GC
  already removes leaked `ztw-snap-*` dirs on a confirmed orchestrator death;
  extend it to also **unmount leaked overlays / drop leaked subvolumes**, so a
  crash never leaves a mount or CoW subvol stranded. → **No extension needed**
  (nothing to unmount). The reaper is `crates/supervisor/src/reap.rs`
  (`SNAPSHOT_PREFIX` / `reapable_snapshot_root`), and the `ztw-snap-*`
  mkdtemp-in-the-OS-temp-dir placement is now a *contract on every backend*
  (`EPHEMERAL_SNAPSHOT_PREFIX`'s docstring states it; the conformance suite
  asserts the reapable parent shape). Long-lineage disk growth is instead
  handled by the retention GC this note did not propose: `zicato.epoch.gc` /
  `zicato epoch gc`, which prunes settled rejected generations' trees (tag +
  worktree under git, `snapshot/` under directory) and never a record.

## Risks

*(Retrospective: the first two risks are why A and B were dropped; the third
never applied; the fourth and fifth were addressed.)*

- **overlay/fuse availability + privilege** — rootless `fuse-overlayfs` vs a
  supervisor-spawned privileged mount. The capability probe must be reliable and
  the fallback automatic. → *Decisive against A.*
- **reflink detection correctness** — `cp --reflink=auto` silently falls back to a
  full copy on a non-reflink FS, defeating the purpose without erroring; probe the
  FS explicitly and record which strategy was used. → *Decisive against B: a
  performance feature that fails silently is worse than no feature.*
- **hardlink in-place-write corruption** — never use C for the execution layer.
  → *C was never built.*
- **teardown discipline** — leaked mounts/subvols accumulate; the supervisor GC
  must own this (above). → *Addressed for the shipped shape: prefix-guarded crash
  reaping (`reap.rs`) plus a `worktree prune` before every add, serialised by
  `_worktree_admin_lock` so a prune cannot collect a concurrent sibling's
  half-registered admin entry.*
- **behaviour-preservation** — the materialized tree and the persisted artifact
  must be byte-identical to the copytree result. Gate every step on `parity.sh`
  (the deterministic mock target must produce identical goldens regardless of which
  strategy built the tree). → *Held to, by two gates: `tools/parity.sh` for the
  end-to-end goldens, and `tests/test_genstore_conformance.py` — every protocol
  test parametrised over both backends, so a backend that diverges observably
  fails there. Git-specific behaviour is pinned by `tests/test_git_genstore.py`.*

## Phasing

> **Not scheduled — recorded for completeness.** None of P1–P4 will be built;
> the corresponding as-built work is noted per phase.

- **P1 — seam + fallback.** Introduce the pluggable materialization seam + host
  capability detection, with full `copytree` as the only implementation. Pure
  refactor, behaviour-identical, parity-gated. → *Landed in a different shape:
  the seam is `GenerationStore.checkout_ephemeral`, the "capability detection" is
  the explicit `generation_source_backend` knob — no probing of host
  capabilities or workspace evidence — and the fallback is
  `copy_checkout_ephemeral`.*
- **P2 — reflink/CoW fast path.** Cheapest to add (no privilege, no mount); detect
  the FS and use it, else fall back. → *Dropped (strategy B).*
- **P3 — overlayfs fast path.** Best containment; the privileged mount ties to
  supervisor spawn-side ownership (or `fuse-overlayfs` unprivileged). Wire the
  `upper` delta into the persisted artifact and the supervisor diff-containment
  check. → *Dropped (strategy A).*
- **P4 — git object-store backing (optional).** Content-addressed dedup +
  portability + per-generation commits, under whichever on-disk strategy is active.
  → *Built, and promoted from "optional P4" to **the whole answer** and the
  default backend. [`STORAGE.md`](STORAGE.md) §7.*

## Cross-references

| Topic | Document |
|---|---|
| **The superseding design** — the git-backed generation store, the domain→git mapping, the design-review record, parity | [STORAGE.md](STORAGE.md) §7 |
| Where the `GenerationStore` protocol and both backends sit in the persistence design | [STORAGE.md](STORAGE.md) §5.2 |
| The artifact-exclusion set shared by `copytree_ignore` / `gitignore_lines` | [STORAGE.md](STORAGE.md) §5.2.1 |
| Subprocess-isolated tournament runs — why per-run isolation exists at all | [ROBUSTNESS.md](ROBUSTNESS.md) |
| The supervisor's diff-containment attestation and the promotion-gate notary | [ZICATO-SYSTEM-ANALYSIS.md](ZICATO-SYSTEM-ANALYSIS.md) |
| `experiment.json`, per-generation directories, the contract hash and `mutable_trees` | [EPOCHS-AND-JOURNALING.md](EPOCHS-AND-JOURNALING.md) |
| The best-of-N slate that `derive_scratch` exists to serve | [PROPOSER.md](PROPOSER.md) |
| Operator surface (`zicato epoch gc`) | [CLI.md](CLI.md) |
