"""``GitGenerationStore`` — the git-backed :class:`GenerationStore` backend.

This is the roadmap's v0+1 generation-store backend
(``docs/design/STORAGE.md`` §7), a drop-in implementation of the
:class:`~zicato.epoch.genstore.GenerationStore` protocol and the
**default** backend: it dedups blobs across a lineage and its worktree
checkout *is* the isolated per-run tree, removing both the per-generation
and per-run ``copytree`` the directory backend pays.
:class:`~zicato.epoch.genstore.DirectoryGenerationStore` stays
config-selectable for a no-git environment
(``generation_source_backend: "directory"``). ``zicato init`` writes the git
selection explicitly for a new workspace.

Why git, and why a private workspace repo
-----------------------------------------
The directory backend is a full ``copytree`` per generation. For a long
lineage — target 3 (zicato optimising zicato) can run 50+ generations —
that is 50+ near-identical trees on disk. Git's content-addressed object
store collapses that: a module unchanged across 20 generations is **one
blob**, referenced by 20 commits. The motivating directory-backend
problem (disk cost of long lineages) simply does not exist for git.

The repo is **entirely private to zicato** — it lives at
``{workspace_root}/repo/`` (i.e. inside ``.zicato/``), and the user's
own outer repository is never touched. It is a normal, non-bare git
repository; zicato is its only writer.

The domain → git mapping
------------------------
* **Workspace** → one git repository (``{workspace_root}/repo/``). One
  repo, not one-per-epoch: cross-epoch ``diff``/``log`` and cross-epoch
  blob dedup both want a single object store.
* **Epoch** → a branch, ``epoch/{epoch_id}``. An epoch's generations are
  a commit chain on its branch.
* **Generation** → a commit, tagged ``epoch/{epoch_id}/{generation_id}``
  (e.g. ``epoch/2026-05-18_e1/v3``). The tag is the stable handle; the
  branch head moves as generations are appended.
* **Commit context** → the deriving commit's message carries a redundant,
  operator-readable copy of the lineage coordinates and patches. Canonical
  patch reads always use ``StorageBackend`` records.
* **Parallel tournament runs** → a ``git worktree`` checked out at the
  generation's tag. This *replaces* the directory backend's per-run
  ``copytree`` ephemeral snapshot with a git-native isolated checkout.

:meth:`snapshot_path` calculates a worktree location without I/O.
:meth:`materialize_snapshot` checks out the worktree when a caller needs a
real on-disk source tree.

Why shell out to the ``git`` CLI
--------------------------------
This module drives git through :mod:`subprocess` rather than adding a
library dependency (``pygit2``/``GitPython``). Reasons:

* **No new dependency.** ``pygit2`` is a C-extension binding to
  ``libgit2`` — a build-time burden and an ABI surface. ``GitPython``
  itself shells out to the CLI under the hood. zicato already shells out
  to external tools; the ``git`` CLI is ubiquitous and stable.
* **The operations are coarse.** Generation granularity is whole-tree
  commits and tags — a handful of plumbing commands. There is no
  fine-grained object manipulation that would benefit from an in-process
  library.
* **Debuggability.** Every state change is a command an operator can run
  by hand against ``{workspace_root}/repo/`` to reproduce or inspect.

The artifact exclusion
----------------------
The generation repo carries a ``.gitignore`` written from
:func:`zicato.epoch.snapshot_scope.gitignore_lines`, so run artifacts
(``output/``, caches) never enter a commit — the git-backend equivalent
of the directory backend's ``copytree(ignore=...)``.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Iterable, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

from zicato.core.types import Patch
from zicato.epoch.genstore import (
    EPHEMERAL_SCRATCH_DIRNAME,
    EPHEMERAL_SNAPSHOT_PREFIX,
    GIT_BACKEND,
    GIT_REPO_DIRNAME,
    GIT_WORKTREES_DIRNAME,
    EphemeralCheckout,
    TreeEntry,
    discard_ephemeral_parent,
    source_tree_bytes,
)
from zicato.epoch.snapshot_scope import gitignore_lines, is_artifact

#: Sentinel line separating the human commit subject from the machine
#: metadata block in a generation commit message. Everything after this
#: line is a JSON object; everything before is free-form.
_META_SENTINEL = "---zicato-meta---"

#: Identity used for generation commits. The generation repo is private
#: to zicato and has exactly one writer, so a fixed identity is correct —
#: it is never a person, and it must not carry a vendor name.
_GIT_AUTHOR_NAME = "zicato"
_GIT_AUTHOR_EMAIL = "zicato@localhost"

log = logging.getLogger(__name__)

#: Top-level basenames that are git-administrative and must never be laid
#: into a generation tree. A *git worktree* (what :meth:`materialize_snapshot`
#: returns) carries a ``.git`` pointer file at its root; the worktree's
#: own ``.gitignore`` is the repo's artifact-exclusion file, re-supplied
#: from ``zicato-root``. Both appear in a worktree's ``iterdir()`` and
#: would otherwise be copied when one generation's worktree seeds the
#: next (a contract roll), corrupting the new commit. The directory
#: backend never produced these, so this guard is git-backend-specific.
_GIT_ADMIN_BASENAMES = frozenset({".git", ".gitignore"})

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
_REPO_WORKTREE_LOCKS_GUARD = threading.Lock()


def _worktree_admin_lock(repo: Path) -> threading.Lock:
    """Return the process-wide worktree-admin lock for one repo path."""
    key = str(Path(repo).resolve())
    with _REPO_WORKTREE_LOCKS_GUARD:
        return _REPO_WORKTREE_LOCKS.setdefault(key, threading.Lock())


class GitCommandError(RuntimeError):
    """A ``git`` subprocess exited non-zero.

    Carries the argv, exit code, and captured stderr so a failure
    surfaces an actionable message rather than an opaque
    :class:`subprocess.CalledProcessError`.
    """

    def __init__(self, argv: Sequence[str], returncode: int, stderr: str) -> None:
        self.argv = list(argv)
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(f"git {' '.join(argv)} exited {returncode}: {stderr.strip()}")


class GitGenerationStore:
    """The git-backed :class:`~zicato.epoch.genstore.GenerationStore`.

    Conforms to the :class:`~zicato.epoch.genstore.GenerationStore`
    protocol structurally — it is a drop-in second backend, selected at
    :func:`zicato.epoch.genstore.default_generation_store` off the
    workspace ``generation_source_backend`` config knob.

    Construction is cheap and side-effect free; the git repository is
    created lazily on the first write (:meth:`seed_generation`).

    Parameters
    ----------
    workspace_root:
        The ``.zicato/`` directory. The private generation repo lives at
        ``workspace_root / "repo"``; worktrees for parallel runs live
        under ``workspace_root / "repo-worktrees"``.
    """

    #: Subdirectory of the workspace holding the private generation repo.
    #: The private repository directory inside the workspace.
    REPO_DIRNAME = GIT_REPO_DIRNAME
    #: Subdirectory holding materialised per-generation worktrees.
    WORKTREES_DIRNAME = GIT_WORKTREES_DIRNAME

    def __init__(self, workspace_root: Path) -> None:
        self._workspace_root = Path(workspace_root)
        self._repo = self._workspace_root / self.REPO_DIRNAME
        self._worktrees = self._workspace_root / self.WORKTREES_DIRNAME

    @property
    def workspace_root(self) -> Path:
        """The workspace directory the generation repo is rooted under."""
        return self._workspace_root

    @property
    def backend_name(self) -> str:
        """Return the workspace config name for this backend."""
        return GIT_BACKEND

    @property
    def repo_path(self) -> Path:
        """The private git repository holding every generation."""
        return self._repo

    # ------------------------------------------------------------------
    # git plumbing
    # ------------------------------------------------------------------

    def _git(self, *args: str, cwd: Path | None = None) -> str:
        """Run a ``git`` subprocess in the repo and return its stdout.

        Raises :class:`GitCommandError` on a non-zero exit so callers get
        an actionable failure rather than a silent wrong result.
        """
        argv = ["git", *args]
        proc = subprocess.run(  # noqa: S603 — argv is fixed-shape, never shell
            argv,
            cwd=str(cwd or self._repo),
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise GitCommandError(args, proc.returncode, proc.stderr)
        return proc.stdout

    def _git_ok(self, *args: str, cwd: Path | None = None) -> bool:
        """Run a ``git`` subprocess, returning ``True`` iff it exited zero."""
        try:
            self._git(*args, cwd=cwd)
        except GitCommandError:
            return False
        return True

    def _ensure_repo(self) -> None:
        """Create the private generation repo if it does not exist yet.

        The repo is initialised with an ``initial`` orphan branch that
        carries only the ``.gitignore`` — every epoch branch is created
        from that root so the artifact-exclusion ``.gitignore`` is shared
        and cross-epoch ``diff`` has a common base.
        """
        if (self._repo / ".git").exists():
            return
        self._repo.mkdir(parents=True, exist_ok=True)
        self._git("init", "--initial-branch", "zicato-root", ".")
        self._configure_identity()
        gitignore = self._repo / ".gitignore"
        gitignore.write_text("\n".join(gitignore_lines()) + "\n", encoding="utf-8")
        self._git("add", ".gitignore")
        self._commit("zicato: generation repository root")

    def _configure_identity(self) -> None:
        """Pin a fixed committer identity local to the generation repo."""
        self._git("config", "user.name", _GIT_AUTHOR_NAME)
        self._git("config", "user.email", _GIT_AUTHOR_EMAIL)
        # A private, single-writer repo: GPG signing would only ever fail.
        self._git("config", "commit.gpgsign", "false")

    def _commit(self, message: str) -> str:
        """Commit the staged index and return the new commit's full hash.

        ``--allow-empty`` is intentional: a derived child generation can be
        byte-identical to its parent (a proposer may propose a patch that
        sets a value to what it already is, or two distinct mutation points
        that cancel out). The directory backend records that as a normal
        generation; the git backend must too, rather than aborting with
        "nothing to commit". Every generation is a commit even when its
        tree did not change — the lineage is the commit chain.
        """
        self._git("commit", "--no-verify", "--allow-empty-message", "--allow-empty", "-m", message)
        return self._git("rev-parse", "HEAD").strip()

    # ------------------------------------------------------------------
    # ref / tag naming — the domain → git mapping
    # ------------------------------------------------------------------

    @staticmethod
    def _epoch_branch(epoch_id: str) -> str:
        """Branch name for an epoch — its generations are a commit chain here."""
        return f"epoch/{epoch_id}"

    @staticmethod
    def _generation_tag(epoch_id: str, generation_id: str) -> str:
        """Tag name for a generation commit — the stable per-generation handle."""
        return f"epoch/{epoch_id}/{generation_id}"

    def _worktree_path(self, epoch_id: str, generation_id: str) -> Path:
        """On-disk path of a generation's materialised worktree."""
        return self._worktrees / epoch_id / generation_id

    # ------------------------------------------------------------------
    # GenerationStore protocol — coordinate queries
    # ------------------------------------------------------------------

    def snapshot_path(self, epoch_id: str, generation_id: str) -> Path:
        """Return the generation's worktree path without materialising it."""
        return self._worktree_path(epoch_id, generation_id)

    def materialize_snapshot(self, epoch_id: str, generation_id: str) -> Path:
        """Check out a generation's tag into its reusable local worktree."""
        if not self.has_generation(epoch_id, generation_id):
            raise FileNotFoundError(
                f"generation {epoch_id}/{generation_id} has no commit in the generation repo"
            )
        # Route EVERY materialised read through the locked materialiser — even
        # when a worktree directory already exists. The old unlocked
        # ``if wt.is_dir(): return wt`` fast path was unsafe under concurrency:
        # ``git worktree add`` creates the target directory BEFORE it checks
        # the files out, so a sibling that probed ``wt.is_dir()`` mid-add got
        # ``True`` and copied a HALF-populated tree. _materialise_worktree does
        # its is_dir re-check + validity verify under the worktree-admin lock,
        # so it either fast-returns an already-valid worktree (the warm path
        # stays cheap) or serialises the add — never handing back a tree an
        # add is still populating.
        return self._materialise_worktree(epoch_id, generation_id)

    def _materialise_worktree(self, epoch_id: str, generation_id: str) -> Path:
        """Check out a generation's tag into its reusable local worktree.

        This worktree serves orchestrator and source-browser reads. Tournament
        runs use :meth:`checkout_ephemeral` instead, so runtime writes never
        reach this reusable tree. Both kinds of checkout share Git's object
        store rather than copying an existing directory snapshot.
        """
        wt = self._worktree_path(epoch_id, generation_id)
        wt.parent.mkdir(parents=True, exist_ok=True)
        tag = self._generation_tag(epoch_id, generation_id)
        with _worktree_admin_lock(self._repo):
            # Re-check for an existing valid worktree INSIDE the lock before
            # adding. :meth:`materialize_snapshot` routes every materialised read here
            # (it no longer has its own unlocked ``if wt.is_dir(): return wt``
            # fast path), so two concurrent cold-store materialisations of the
            # same generation both reach this point. ``git worktree add`` on an
            # already-populated directory fails, so the guard only holds if it
            # lives under the admin lock: the first caller adds the worktree,
            # and every sibling that acquires the lock afterward finds a valid
            # worktree parked at ``tag`` and simply reuses it (the benign
            # already-exists case).
            if wt.is_dir():
                if self._is_worktree_at(wt, tag):
                    return wt
                # A directory exists but is not a live worktree at this ref
                # (a crashed half-add, or a stale/corrupt tree). Remove it so
                # the add below starts clean; the following prune collects any
                # orphaned registration.
                shutil.rmtree(wt, ignore_errors=True)
            # Prune any stale registration first (a crashed run can leave
            # a worktree entry whose directory is gone).
            self._git("worktree", "prune")
            self._git("worktree", "add", "--detach", "--force", str(wt), tag)
        return wt

    def _is_worktree_at(self, wt: Path, tag: str) -> bool:
        """True iff ``wt`` is a live git worktree checked out at ``tag``'s commit.

        Used by :meth:`_materialise_worktree` to tolerate the benign
        concurrent already-materialised case: a sibling caller may have
        created the worktree between the unlocked materialization probe and
        this caller acquiring the admin lock. The directory is only reused
        when it is a real worktree detached at exactly the commit ``tag``
        names — a stale, partial, or wrong-ref tree returns ``False`` and gets
        rebuilt.
        """
        try:
            head = self._git("rev-parse", "HEAD", cwd=wt).strip()
            want = self._git("rev-parse", f"{tag}^{{commit}}").strip()
        except GitCommandError:
            return False
        return bool(head) and head == want

    def checkout_ephemeral(
        self,
        epoch_id: str,
        generation_id: str,
        run_id: str,
    ) -> EphemeralCheckout:
        """Check the generation's tag out into a fresh PER-RUN ``git worktree``.

        Design decision — per-run worktree vs shared worktree
        ------------------------------------------------------
        Two candidate designs were benchmarked (2026-07-01, tmpfs-free
        local disk, ``git worktree add --detach`` from a tag, 16
        threads):

        * **(a) per-run ``git worktree add --detach``** (CHOSEN) — one
          isolated checkout per run. Cost on a 60-file/~120 KB tree:
          16 concurrent adds total 14–22 ms (per-add mean 7.6–10.5 ms,
          max 20 ms under contention; 6.4 ms serial). On a
          500-file/~2 MB tree: 16 concurrent adds total ~41 ms (per-add
          mean ~28 ms, max 36 ms; 19 ms serial). git's internal repo
          lock serialises the ref/administrative step only, so 16-way
          contention costs under 1.5× a serial add — and the add is
          3–18× FASTER than the ``shutil.copytree`` the directory
          backend pays for the same tree (16 concurrent copies: ~70 ms
          / ~525 ms respectively).
        * **(b) shared per-generation worktree + per-run scratch
          discipline** — zero per-run cost, but concurrent sibling runs
          of the SAME generation share one tree, so a stray write that
          ignores the scratch dir contaminates every concurrent
          sibling's measurement AND persists into the shared worktree
          for the rest of the epoch (a contract roll seeds the next
          epoch's ``v0`` from the promoted head's worktree, so a
          non-artifact stray file would even cross epochs). Lineage
          itself is immune under git — children derive from the COMMIT,
          not the worktree — but measurement isolation is exactly what
          the ephemeral snapshot exists for.

        At ≤ ~30 ms per run against runs that take seconds-to-minutes,
        (a)'s isolation-parity with the shipped behaviour wins outright;
        (b)'s only advantage is a cost that is already negligible.

        Mechanics: the worktree is created INSIDE a ``ztw-snap-{run_id}-*``
        mkdtemp parent under the OS temp dir — the exact shape the
        supervisor's crash-reaper (``reap.rs::reapable_snapshot_root``)
        GCs — named with the generation id so the basename matches the
        shared worktree :meth:`materialize_snapshot` returns. A per-run
        ``run-scratch`` sibling preserves the
        :data:`~zicato.epoch.snapshot_scope.SCRATCH_DIR_ENV` contract.

        The checkout is immediately DETACHED from the repo: the
        worktree's ``.git`` pointer file is unlinked and the registration
        pruned right after the add, leaving the run a plain throwaway
        tree. That gives byte parity with the directory backend's view
        (whose ``copytree`` filter skips ``.git``), keeps the worker from
        reaching back into the private repo through the pointer, and
        means NO cleanup path depends on git state — the run-end cleanup
        (and the supervisor's crash-reaper) just remove the ``ztw-snap-*``
        parent. The prune→add→detach→prune window is serialised per repo
        by a process-local lock (:func:`_worktree_admin_lock`): git's own
        repo lock covers each COMMAND, but a prune overlapping a
        concurrent sibling's multi-command add window can collect the
        half-registered admin entry. A registration orphaned by a crash
        inside the window is collected by the next prune-before-add here
        or in :meth:`_materialise_worktree`.

        A third candidate — per-run ``git archive <tag>`` extracted via
        :mod:`tarfile` (no shared admin state at all) — was benchmarked
        and rejected: serial cost is ~1.5–2.5× a worktree add (9.5 ms vs
        6.4 ms / 48 ms vs 19 ms on the small/large trees) and the
        Python-side extraction serialises catastrophically under threads
        (16 concurrent: ~163 ms / ~1.36 s total vs 14–41 ms for adds).
        """
        if not self.has_generation(epoch_id, generation_id):
            raise FileNotFoundError(
                f"checkout_ephemeral: generation {epoch_id}/{generation_id} "
                f"has no commit in the generation repo"
            )
        parent = Path(tempfile.mkdtemp(prefix=f"{EPHEMERAL_SNAPSHOT_PREFIX}{run_id}-"))
        # Keep the shared worktree's basename (the generation id) so any
        # path the agent derives from ``__file__`` looks the same as it
        # would under the canonical checkout.
        working_dir = parent / self._worktree_path(epoch_id, generation_id).name
        tag = self._generation_tag(epoch_id, generation_id)
        try:
            with _worktree_admin_lock(self._repo):
                # Prune stale registrations from crashed runs first, then
                # check out and immediately detach (see docstring). The
                # lock serialises the whole admin window; the measured
                # cost per checkout is milliseconds (docstring).
                self._git("worktree", "prune")
                self._git("worktree", "add", "--detach", "--force", str(working_dir), tag)
                (working_dir / ".git").unlink(missing_ok=True)
                self._git("worktree", "prune")
        except GitCommandError as exc:
            discard_ephemeral_parent(parent)
            # OSError so callers' degrade-to-aborted-run handling (which
            # already covers the directory backend's copytree failures)
            # applies unchanged.
            raise OSError(
                f"checkout_ephemeral: worktree add failed for " f"{epoch_id}/{generation_id}: {exc}"
            ) from exc
        scratch_dir = parent / EPHEMERAL_SCRATCH_DIRNAME
        scratch_dir.mkdir()

        def _cleanup() -> None:
            discard_ephemeral_parent(parent)

        return EphemeralCheckout(working_dir=working_dir, scratch_dir=scratch_dir, cleanup=_cleanup)

    def has_generation(self, epoch_id: str, generation_id: str) -> bool:
        """Return ``True`` iff a generation commit/tag exists for the coordinate."""
        if not (self._repo / ".git").exists():
            return False
        tag = self._generation_tag(epoch_id, generation_id)
        return self._git_ok("rev-parse", "--verify", "--quiet", f"refs/tags/{tag}")

    def list_generations(self, epoch_id: str) -> list[str]:
        """Return every materialised generation id under an epoch, sorted.

        Reads the epoch's generation tags (``epoch/{epoch_id}/{id}``) and
        returns the bare generation ids. Sorted lexicographically for a
        deterministic order, matching the directory backend.
        """
        if not (self._repo / ".git").exists():
            return []
        prefix = f"epoch/{epoch_id}/"
        out = self._git("tag", "--list", f"{prefix}*", "--format=%(refname:short)")
        ids: list[str] = []
        for line in out.splitlines():
            line = line.strip()
            if line.startswith(prefix):
                ids.append(line[len(prefix) :])
        return sorted(ids)

    # ------------------------------------------------------------------
    # GenerationStore protocol — generation-level transactions
    # ------------------------------------------------------------------

    def seed_generation(
        self,
        epoch_id: str,
        generation_id: str,
        sources: Iterable[Path],
    ) -> Path:
        """Materialise a seed generation as the first commit on an epoch branch.

        Each source tree is copied (artifacts excluded) into a clean
        epoch branch checkout under its own basename, committed, and
        tagged. The epoch branch is created from the repo's
        ``zicato-root`` so it inherits the artifact-exclusion
        ``.gitignore``. Returns the generation's worktree path.

        A source whose basename is git-administrative (``.git`` — a
        worktree carries a ``.git`` *pointer file*, the directory backend's
        snapshot never did) or the repo's own ``.gitignore`` is skipped:
        these arise when seeding a v0 from a *git worktree* (a contract
        roll seeds the next epoch from the previous epoch's promoted-head
        worktree, whose ``iterdir()`` includes ``.git``/``.gitignore``).
        Copying the worktree's ``.git`` pointer into a new generation would
        make ``git add`` resolve it as a foreign repository and abort; the
        seed ``.gitignore`` is already laid down by ``zicato-root``.
        """
        self._ensure_repo()
        sources = [s for s in sources if Path(s).name not in _GIT_ADMIN_BASENAMES]
        for raw in sources:
            source = Path(raw).resolve()
            if not source.exists():
                raise FileNotFoundError(
                    f"seed_generation: source tree {source} does not exist on disk"
                )

        branch = self._epoch_branch(epoch_id)
        if not self._git_ok("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"):
            # New epoch branch from the shared root commit.
            self._git("branch", branch, "zicato-root")
        self._git("checkout", branch)

        # Lay the source trees into the repo working dir, artifacts
        # filtered. The .gitignore from zicato-root is already present.
        for raw in sources:
            source = Path(raw).resolve()
            target = self._repo / source.name
            if target.exists():
                shutil.rmtree(target) if target.is_dir() else target.unlink()
            if source.is_file():
                shutil.copy2(source, target)
            else:
                shutil.copytree(source, target, ignore=_artifact_ignore)

        self._git("add", "-A")
        message = self._format_commit_message(
            epoch_id, generation_id, parent_generation_id=None, patches=()
        )
        self._commit(message)
        self._tag_generation(epoch_id, generation_id)
        return self.materialize_snapshot(epoch_id, generation_id)

    def derive_generation(
        self,
        epoch_id: str,
        parent_generation_id: str,
        child_generation_id: str,
        patches: Sequence[Patch],
    ) -> Path:
        """Derive a child generation as a new commit on the epoch branch.

        The parent generation's tree is checked out, the patch set is
        applied all-or-nothing (via
        :func:`zicato.mutation.applier.apply_patches`, into a temp tree
        the result of which replaces the working tree), and the result
        is committed and tagged. The patch metadata travels in the
        commit message after the ``---zicato-meta---`` sentinel.

        Raises :class:`FileNotFoundError` when the parent has no commit,
        and :class:`ValueError` when the patch set fails validation — in
        which case nothing is committed or tagged.
        """
        from zicato.mutation.applier import apply_patches  # noqa: PLC0415

        if not self.has_generation(epoch_id, parent_generation_id):
            raise FileNotFoundError(
                f"derive_generation: parent generation {epoch_id}/"
                f"{parent_generation_id} has no commit in the generation repo"
            )

        branch = self._epoch_branch(epoch_id)
        parent_tag = self._generation_tag(epoch_id, parent_generation_id)
        # Position the epoch branch at the parent so the child commit
        # parents the parent generation — the lineage is the commit DAG.
        self._git("checkout", branch)
        self._git("reset", "--hard", parent_tag)

        # apply_patches refuses to overwrite an existing target, so build
        # the child tree in a scratch path next to the repo, then swap it
        # into the working tree.
        scratch = self._workspace_root / ".derive-scratch"
        if scratch.exists():
            shutil.rmtree(scratch)
        try:
            # Source is the parent's worktree (a clean checkout of the
            # parent tree). apply_patches validates the whole batch and
            # raises ValueError without leaving a partial tree.
            parent_root = self.materialize_snapshot(epoch_id, parent_generation_id)
            apply_patches(
                source_root=parent_root,
                patches=list(patches),
                target_root=scratch,
            )
            # Replace the repo working tree (minus .git) with the
            # patched tree, then commit.
            self._replace_working_tree(scratch)
        finally:
            if scratch.exists():
                shutil.rmtree(scratch, ignore_errors=True)

        self._git("add", "-A")
        message = self._format_commit_message(
            epoch_id,
            child_generation_id,
            parent_generation_id=parent_generation_id,
            patches=tuple(patches),
        )
        self._commit(message)
        self._tag_generation(epoch_id, child_generation_id)
        # A RE-derive of the same child id (a proposer retry after failed
        # post-apply validation, the best-of-N chosen-candidate re-derive, a
        # crash-resume re-validate) moves the tag to the fresh commit — but a
        # worktree materialised by an EARLIER attempt stays detached at the
        # old commit, so ``materialize_snapshot`` would hand back a stale tree that
        # no longer matches the commit just derived (the directory backend
        # clears + rebuilds the child tree instead, so only this backend
        # needs the refresh). Drop the stale checkout; ``materialize_snapshot``
        # below re-materialises it from the moved tag (its ``worktree add``
        # path prunes the orphaned registration first).
        stale_worktree = self._worktree_path(epoch_id, child_generation_id)
        if stale_worktree.is_dir():
            shutil.rmtree(stale_worktree, ignore_errors=True)
        return self.materialize_snapshot(epoch_id, child_generation_id)

    def derive_scratch(
        self,
        epoch_id: str,
        parent_generation_id: str,
        patches: Sequence[Patch],
        scratch_root: Path,
    ) -> Path:
        """Apply ``patches`` to the parent tree into ``scratch_root``, off-repo.

        The concurrency-safe scratch derive: it reads the parent generation's
        materialised worktree (a clean, read-only checkout —
        :meth:`materialize_snapshot`) and applies the patch set all-or-nothing into
        the caller-owned ``scratch_root`` via
        :func:`zicato.mutation.applier.apply_patches`. It touches NO shared
        git state — no ``git checkout``/``reset``/``commit``/``tag``, no
        epoch-branch working-tree replace, no ``.derive-scratch`` — so
        concurrent slot derives never contend on the repo (each writes a
        disjoint ``scratch_root``; the parent worktree is read-only). Because
        nothing enters the object store or the tag namespace, a scratch tree
        is invisible to :meth:`list_generations`, the GC, the reindex and the
        dashboard readers (all of which enumerate ``epoch/{id}/*`` tags).

        The parent worktree is materialised on first read, and that
        materialisation is idempotent: :meth:`_materialise_worktree`
        re-checks for an existing valid worktree *inside* the process
        worktree-admin lock before running ``git worktree add``, so
        concurrent cold-store derives of the same parent never race on the
        add — the first one checks it out and the rest reuse it. Callers that
        fan out concurrently should still pre-warm it once (the round's
        scratch-validator factory does) so the concurrent derives find it
        already present and only read.

        Raises :class:`FileNotFoundError` when the parent has no commit and
        :class:`ValueError` when the patch set fails validation (no scratch
        tree is left behind).
        """
        from zicato.mutation.applier import apply_patches  # noqa: PLC0415

        if not self.has_generation(epoch_id, parent_generation_id):
            raise FileNotFoundError(
                f"derive_scratch: parent generation {epoch_id}/"
                f"{parent_generation_id} has no commit in the generation repo"
            )
        parent_root = self.materialize_snapshot(epoch_id, parent_generation_id)
        scratch_root = Path(scratch_root)
        if scratch_root.exists():
            shutil.rmtree(scratch_root)
        scratch_root.parent.mkdir(parents=True, exist_ok=True)
        apply_patches(
            source_root=parent_root,
            patches=list(patches),
            target_root=scratch_root,
        )
        return scratch_root

    def _replace_working_tree(self, new_tree: Path) -> None:
        """Overwrite the repo working tree (preserving ``.git``) with ``new_tree``."""
        for child in self._repo.iterdir():
            if child.name == ".git":
                continue
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
        for child in new_tree.iterdir():
            target = self._repo / child.name
            if child.is_dir():
                shutil.copytree(child, target, ignore=_artifact_ignore)
            else:
                shutil.copy2(child, target)
        # The .gitignore was wiped with the rest of the tree; restore it.
        gitignore = self._repo / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text("\n".join(gitignore_lines()) + "\n", encoding="utf-8")

    def _tag_generation(self, epoch_id: str, generation_id: str) -> None:
        """Tag the current ``HEAD`` as a generation; replace a stale tag."""
        tag = self._generation_tag(epoch_id, generation_id)
        # A retried round may have left a tag on a failed attempt; force.
        self._git("tag", "-f", tag, "HEAD")

    # ------------------------------------------------------------------
    # Commit-message metadata block
    # ------------------------------------------------------------------

    def _format_commit_message(
        self,
        epoch_id: str,
        generation_id: str,
        *,
        parent_generation_id: str | None,
        patches: Sequence[Patch],
    ) -> str:
        """Build a generation commit message with the embedded metadata block.

        The human-readable subject names the generation; the machine
        block after :data:`_META_SENTINEL` is a redundant, operator-readable
        copy of the derivation context. Canonical patch reads use the generic
        record backend, so pruning a tag never removes patch history.
        """
        subject = f"zicato: {epoch_id}/{generation_id}"
        meta = {
            "epoch_id": epoch_id,
            "generation_id": generation_id,
            "parent_generation_id": parent_generation_id,
            "patches": [asdict(p) for p in patches],
        }
        return f"{subject}\n\n{_META_SENTINEL}\n{json.dumps(meta, indent=2)}\n"

    def _read_commit_meta(self, ref: str) -> dict[str, Any] | None:
        """Parse the metadata block out of a generation commit's message."""
        body = self._git("log", "-1", "--format=%B", ref)
        if _META_SENTINEL not in body:
            return None
        _, _, raw = body.partition(_META_SENTINEL)
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None

    # ------------------------------------------------------------------
    # GenerationStore protocol — read surface (dashboard)
    # ------------------------------------------------------------------

    def list_tree(self, epoch_id: str, generation_id: str) -> list[TreeEntry]:
        """List a generation's tree from its commit, via ``git ls-tree``.

        Reads the tree object directly — no worktree is materialised —
        so the dashboard can browse any generation cheaply. Directory
        entries are synthesised from the file paths so the dashboard
        gets the same shape the directory backend returns.
        """
        if not self.has_generation(epoch_id, generation_id):
            raise FileNotFoundError(
                f"list_tree: generation {epoch_id}/{generation_id} has no "
                f"commit in the generation repo"
            )
        tag = self._generation_tag(epoch_id, generation_id)
        # -r recurse, -l long (gives blob size), -z NUL-terminated.
        out = self._git("ls-tree", "-r", "-l", "-z", tag)
        files: list[TreeEntry] = []
        dirs: set[str] = set()
        for record in out.split("\0"):
            if not record:
                continue
            # "<mode> <type> <hash> <size>\t<path>"
            meta, _, path = record.partition("\t")
            if not path:
                continue
            if path == ".gitignore" or any(is_artifact(part) for part in path.split("/")):
                continue
            fields = meta.split()
            size = 0
            if len(fields) >= 4 and fields[3].isdigit():
                size = int(fields[3])
            files.append(TreeEntry(path=path, is_dir=False, size=size))
            # Synthesise every parent directory.
            parts = path.split("/")
            for i in range(1, len(parts)):
                dirs.add("/".join(parts[:i]))
        entries = [TreeEntry(path=d, is_dir=True, size=0) for d in dirs]
        entries.extend(files)
        return sorted(entries, key=lambda e: e.path)

    def read_file(self, epoch_id: str, generation_id: str, rel_path: str) -> bytes:
        """Return one file's bytes from a generation commit, via ``git show``.

        Reads the blob straight from the object store — no worktree
        checkout. Rejects ``..`` traversal in ``rel_path``.
        """
        if not self.has_generation(epoch_id, generation_id):
            raise FileNotFoundError(
                f"read_file: generation {epoch_id}/{generation_id} has no "
                f"commit in the generation repo"
            )
        norm = rel_path.replace("\\", "/").strip("/")
        if not norm or ".." in norm.split("/"):
            raise ValueError(f"read_file: rel_path {rel_path!r} escapes the generation tree")
        tag = self._generation_tag(epoch_id, generation_id)
        argv = ["git", "show", f"{tag}:{norm}"]
        proc = subprocess.run(  # noqa: S603 — fixed-shape argv
            argv,
            cwd=str(self._repo),
            capture_output=True,
            check=False,
        )
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", "replace")
            if "does not exist" in err or "exists on disk, but not in" in err:
                raise FileNotFoundError(
                    f"read_file: {rel_path!r} not found in {epoch_id}/{generation_id}"
                )
            if "is a directory" in err.lower() or "Not a blob" in err:
                raise ValueError(f"read_file: {rel_path!r} is a directory, not a file")
            raise GitCommandError(argv[1:], proc.returncode, err)
        return proc.stdout

    def parent_generation_id(self, epoch_id: str, generation_id: str) -> str | None:
        """Read a generation's parent generation id from its commit metadata.

        The lineage coordinate travels in the deriving commit's message
        (:meth:`_format_commit_message`); this reads it straight back with
        one ``git log`` — read-only, no worktree, no checkout. Returns
        ``None`` for a seed generation (its metadata records no parent)
        or a commit whose metadata block is missing/unparseable.

        Raises :class:`FileNotFoundError` when the generation has no
        commit, matching the other read-surface methods.
        """
        if not self.has_generation(epoch_id, generation_id):
            raise FileNotFoundError(
                f"parent_generation_id: generation {epoch_id}/{generation_id} "
                f"has no commit in the generation repo"
            )
        meta = self._read_commit_meta(self._generation_tag(epoch_id, generation_id))
        if not meta:
            return None
        parent = meta.get("parent_generation_id")
        return parent if isinstance(parent, str) and parent else None

    def diff_generations(
        self, epoch_id: str, from_generation_id: str, to_generation_id: str
    ) -> str:
        """Unified diff between two generation commits — nearly free reads.

        Runs a single read-only ``git diff`` between the two generations'
        tags: git diffs the tree OBJECTS, so no worktree is materialised
        and nothing in the repo changes. ``--no-ext-diff`` / ``--no-color``
        pin the output to plain unified-diff text regardless of any
        ambient git configuration. Returns the empty string when the two
        trees are byte-identical (a derived child can legitimately equal
        its parent — see :meth:`_commit`).

        Raises :class:`FileNotFoundError` when either generation has no
        commit, matching the other read-surface methods.
        """
        for gid in (from_generation_id, to_generation_id):
            if not self.has_generation(epoch_id, gid):
                raise FileNotFoundError(
                    f"diff_generations: generation {epoch_id}/{gid} has no "
                    f"commit in the generation repo"
                )
        from_tag = self._generation_tag(epoch_id, from_generation_id)
        to_tag = self._generation_tag(epoch_id, to_generation_id)
        return self._git(
            "diff",
            "--no-color",
            "--no-ext-diff",
            f"refs/tags/{from_tag}",
            f"refs/tags/{to_tag}",
        )

    def prune_generations(
        self,
        epoch_id: str,
        generation_ids: Sequence[str],
        *,
        dry_run: bool,
    ) -> int:
        """Delete selected generation tags and worktrees while preserving records."""
        selected = [
            (
                self._generation_tag(epoch_id, generation_id),
                self.snapshot_path(epoch_id, generation_id),
            )
            for generation_id in generation_ids
        ]
        reclaimed = sum(source_tree_bytes(worktree) for _, worktree in selected)
        if dry_run:
            return reclaimed
        with _worktree_admin_lock(self._repo):
            for tag, worktree in selected:
                if worktree.is_dir():
                    shutil.rmtree(worktree, ignore_errors=True)
                try:
                    self._git("tag", "-d", tag)
                except GitCommandError as exc:
                    log.warning("generation source prune: tag delete failed for %s: %s", tag, exc)
            try:
                self._git("worktree", "prune")
            except GitCommandError as exc:
                log.warning("generation source prune: worktree prune failed: %s", exc)
        if selected:
            try:
                self._git("gc", "--auto")
            except GitCommandError as exc:
                log.warning("generation source prune: git gc failed: %s", exc)
        return reclaimed


def _artifact_ignore(src: str, names: list[str]) -> set[str]:
    """A ``shutil.copytree`` ``ignore`` skipping snapshot-scope artifacts.

    The git backend's copies (seed source layout, working-tree swap)
    honour the same artifact-exclusion policy the ``.gitignore`` does —
    belt and suspenders, so an artifact never reaches the index even
    transiently.
    """
    from zicato.epoch.snapshot_scope import copytree_ignore  # noqa: PLC0415

    return copytree_ignore()(src, names)


__all__ = [
    "GitGenerationStore",
    "GitCommandError",
]
