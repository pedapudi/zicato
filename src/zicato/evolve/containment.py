"""Python-side diff-containment check — the supervisor's rule surface, pre-finalize.

The Rust supervisor's integrity notary re-hashes child snapshots
OUT-OF-BAND and alarms when a mutation escaped the registered mutable
surface (``crates/supervisor/src/diff_containment.rs`` — alarm-only by
design). This module is the IN-BAND twin on the exact same rule surface,
consulted by the orchestrator immediately before finalizing a promotion
when the contract opts into
:attr:`~zicato.core.scoring_config.ScoringWeights.block_on_containment_violation`:
every file OUTSIDE the registered mutable trees must be byte-identical
parent↔child; a changed / added / deleted out-of-bounds file is an
out-of-bounds mutation.

Semantics mirrored from the supervisor (kept in lockstep deliberately):

* A snapshot copies each registered mutable tree under its BASENAME, so
  the in-bounds surface keyed against snapshot-relative paths is the set
  of those basenames (``mutable_basenames``). An entry with an empty
  basename is ignored.
* When ``mutable_trees`` is empty the surface is the WHOLE snapshot —
  everything is in-bounds, the check is trivially contained.
* v1 granularity is the COARSE file-level check: any out-of-bounds file
  that differs is a violation (line-range tightening is the documented
  follow-up there, not here).
* FAIL-OPEN: an unreadable snapshot or parent yields a ``skipped_reason``
  (the attestation cannot be made), never a violation; an unreadable
  individual file is skipped from the hash map. Symlinks are not
  followed and do not participate as files.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


@dataclass(frozen=True, slots=True)
class ContainmentViolation:
    """One out-of-bounds file difference: a mutation that escaped the sandbox.

    ``path`` is the differing file's forward-slash path RELATIVE to the
    snapshot root; ``kind`` is ``"changed"`` (present in both, content
    differs), ``"added"`` (child only), or ``"deleted"`` (parent only).
    """

    path: str
    kind: str


@dataclass(frozen=True, slots=True)
class ContainmentReport:
    """The attestation for one parent→child snapshot pair.

    ``contained`` is ``True`` when every out-of-bounds file is
    byte-identical parent↔child — including the fail-open skip case
    (``skipped_reason`` set), which is NOT a violation.
    """

    contained: bool
    violations: tuple[ContainmentViolation, ...] = ()
    skipped_reason: str | None = None


def mutable_basenames(mutable_trees: Iterable[str]) -> frozenset[str]:
    """The registered trees' basenames — the in-bounds surface inside a snapshot.

    Mirrors the supervisor's ``mutable_basenames``: each registered tree is
    copied under its basename, an empty basename cannot name a real
    subtree and is dropped, and an EMPTY result means the whole snapshot
    is mutable (nothing can be out-of-bounds).
    """
    names: set[str] = set()
    for tree in mutable_trees:
        name = Path(str(tree)).name
        if name:
            names.add(name)
    return frozenset(names)


def _is_in_bounds(rel: PurePosixPath, basenames: frozenset[str]) -> bool:
    """Whether a snapshot-relative path lies inside the mutable surface.

    In-bounds iff its FIRST path component is a mutable basename; an empty
    ``basenames`` set makes the whole snapshot mutable. A path with no
    leading component is treated as out-of-bounds to be safe (mirroring
    the supervisor's defensive branch).
    """
    if not basenames:
        return True
    parts = rel.parts
    if not parts:
        return False
    return parts[0] in basenames


def _hash_tree(root: Path) -> dict[str, str] | None:
    """Map snapshot-relative posix path → sha256 hex for every regular file.

    ``None`` for a non-directory root (the caller records a skip rather
    than a spurious all-deleted diff). An unreadable file is skipped
    (fail-open — it simply does not participate in the diff); symlinks
    are not followed and never hashed as files.
    """
    if not root.is_dir():
        return None
    out: dict[str, str] = {}
    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
        for filename in filenames:
            abs_path = Path(dirpath) / filename
            if abs_path.is_symlink() or not abs_path.is_file():
                continue
            try:
                data = abs_path.read_bytes()
            except OSError:
                continue
            rel = PurePosixPath(abs_path.relative_to(root).as_posix())
            out[str(rel)] = hashlib.sha256(data).hexdigest()
    return out


def check_containment(
    parent_root: Path,
    child_root: Path,
    mutable_trees: Iterable[str],
) -> ContainmentReport:
    """Compute the out-of-bounds diff between a parent and child snapshot.

    ``parent_root`` / ``child_root`` are the two ``.../snapshot/``
    directories; ``mutable_trees`` are the registered mutable-tree paths
    (their basenames name the in-bounds surface). Returns a clean
    ``contained`` report, the ordered out-of-bounds violations, or a
    fail-open skip when either snapshot is unreadable.
    """
    parent_hashes = _hash_tree(Path(parent_root))
    if parent_hashes is None:
        return ContainmentReport(
            contained=True,
            skipped_reason=f"parent snapshot unreadable: {parent_root}",
        )
    child_hashes = _hash_tree(Path(child_root))
    if child_hashes is None:
        return ContainmentReport(
            contained=True,
            skipped_reason=f"child snapshot unreadable: {child_root}",
        )

    basenames = mutable_basenames(mutable_trees)
    violations: list[ContainmentViolation] = []
    for rel in sorted(set(parent_hashes) | set(child_hashes)):
        # Only OUT-OF-BOUNDS files matter: an in-bounds file may freely
        # differ (it is the mutation surface) — the whole point of the check.
        if _is_in_bounds(PurePosixPath(rel), basenames):
            continue
        in_parent = parent_hashes.get(rel)
        in_child = child_hashes.get(rel)
        if in_parent is not None and in_child is not None:
            if in_parent == in_child:
                continue  # byte-identical — fine.
            kind = "changed"
        elif in_child is not None:
            kind = "added"
        else:
            kind = "deleted"
        violations.append(ContainmentViolation(path=rel, kind=kind))

    return ContainmentReport(contained=not violations, violations=tuple(violations))


def containment_reason(report: ContainmentReport) -> str:
    """Render a violating report as the promotion-refusal reason string.

    The symbolic ``containment_violation`` prefix plus each out-of-bounds
    file with its diff kind, capped at the first few for legibility (the
    full report is derivable by re-running the check / the supervisor's
    scan).
    """
    shown = [f"{v.kind}: {v.path}" for v in report.violations[:5]]
    more = len(report.violations) - len(shown)
    suffix = f" (+{more} more)" if more > 0 else ""
    return (
        "containment_violation: child mutated "
        f"{len(report.violations)} file(s) outside the registered mutable "
        "trees — " + "; ".join(shown) + suffix
    )


__all__ = [
    "ContainmentReport",
    "ContainmentViolation",
    "check_containment",
    "containment_reason",
    "mutable_basenames",
]
