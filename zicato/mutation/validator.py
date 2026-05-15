"""Post-apply checks that gate a child snapshot's acceptance.

The validator is intentionally narrow: it does NOT score, score-deltas, or
otherwise reason about the experiment. It answers one question: "did the
applier produce a snapshot that the runner can safely mount?" Concrete
failures are returned as strings rather than raised so the tournament can
log them as a single rejection record per experiment.

Checks performed
----------------

1. Every Python file under ``target_root`` that was touched by at least
   one patch still parses (``ast.parse`` succeeds).
2. Every patch's ``mutation_id`` still resolves to a :class:`MutationPoint`
   in a fresh enumeration of ``target_root``.
3. For every patch whose pre-apply mutation point declared a
   ``required_placeholders`` metadata key, the patched content must
   contain each named placeholder. The placeholders are interpreted as
   exact substrings (the operator is expected to spell them with the
   surrounding braces, e.g. ``{drift_kind}``).
4. Top-level ``import``/``from``-imports in every patched file are
   preserved — the set of top-level import statements after apply must
   be a superset of the set before. A proposer is allowed to ADD imports
   but not silently remove them; the validator catches the latter.
"""

from __future__ import annotations

import ast
from pathlib import Path

from zicato.core.types import MutationPoint, Patch
from zicato.mutation.enumerator import enumerate_mutations


def _toplevel_imports(file_path: Path) -> set[str]:
    """Return a canonical-string set of top-level imports in ``file_path``.

    Returns an empty set when the file is unparseable; the parser-check
    runs separately and will already have flagged the syntax error.
    """

    try:
        text = file_path.read_text(encoding="utf-8")
    except OSError:
        return set()
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return set()
    out: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                key = f"import {alias.name}"
                if alias.asname:
                    key += f" as {alias.asname}"
                out.add(key)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            level = "." * node.level
            for alias in node.names:
                key = f"from {level}{module} import {alias.name}"
                if alias.asname:
                    key += f" as {alias.asname}"
                out.add(key)
    return out


def validate_post_apply(
    target_root: Path,
    patches: list[Patch],
    pre_apply_mutations: list[MutationPoint],
) -> list[str]:
    """Validate ``target_root`` after applying ``patches``.

    Returns an empty list on success; each entry is a human-readable
    error string describing one problem. Callers (the tournament runner)
    typically refuse to promote a snapshot with any non-empty error list.
    """

    target_root = Path(target_root).resolve()
    errors: list[str] = []
    pre_by_id: dict[str, MutationPoint] = {p.id: p for p in pre_apply_mutations}

    # Files we expect to have been touched. We re-enumerate to learn
    # which files the patched mutation ids now live in (post-apply line
    # numbers don't matter here; only the file-of-record does).
    post_mutations = enumerate_mutations([target_root])
    post_by_id: dict[str, MutationPoint] = {p.id: p for p in post_mutations}
    touched_files: set[Path] = set()
    for patch in patches:
        post_point = post_by_id.get(patch.mutation_id)
        if post_point is not None:
            # Post-apply enumeration already gave us the absolute path
            # inside ``target_root`` — use it directly. Translating
            # through ``relative_to(source_root)`` re-joined to
            # ``target_root`` only works when post-apply ``source_root``
            # equals ``target_root``, which is not the case for the
            # manifest-bridged surface (its ``source_root`` is the
            # goldfive worktree subdir under the snapshot).
            touched_files.add(post_point.file.resolve())
            continue
        pre_point = pre_by_id.get(patch.mutation_id)
        if pre_point is not None:
            # Pre-apply fallback: translate the pre-apply path into the
            # post-apply tree.
            try:
                rel = pre_point.file.relative_to(pre_point.source_root)
                touched_files.add((target_root / rel).resolve())
            except ValueError:
                touched_files.add(pre_point.file)

    # Check 1: every touched Python file still parses. Non-Python files
    # (markdown prompt bodies under a manifest-bridged surface, plain
    # text, etc.) are checked for existence and readability but not for
    # Python syntax — they are not Python and parsing them with
    # :func:`ast.parse` produces false positives.
    for file_path in touched_files:
        if not file_path.exists():
            errors.append(f"Touched file {file_path} does not exist post-apply")
            continue
        try:
            text = file_path.read_text(encoding="utf-8")
        except OSError as exc:
            errors.append(f"Could not read {file_path}: {exc}")
            continue
        if file_path.suffix != ".py":
            continue
        try:
            ast.parse(text)
        except SyntaxError as exc:
            errors.append(f"Post-apply syntax error in {file_path}: {exc}")

    # Check 2: every patch's mutation id still resolves.
    for patch in patches:
        if patch.mutation_id not in post_by_id:
            errors.append(
                f"Patch {patch.id!r}: mutation_id {patch.mutation_id!r} no longer "
                f"resolves in target_root"
            )

    # Check 3: required placeholders survive.
    for patch in patches:
        pre = pre_by_id.get(patch.mutation_id)
        if pre is None:
            continue
        required = pre.metadata.get("required_placeholders")
        if not required:
            continue
        placeholders = [p.strip() for p in required.split(",") if p.strip()]
        post = post_by_id.get(patch.mutation_id)
        if post is None:
            continue
        for placeholder in placeholders:
            if placeholder not in post.content:
                errors.append(
                    f"Patch {patch.id!r}: required placeholder {placeholder!r} "
                    f"missing from post-apply content of {patch.mutation_id!r}"
                )

    # Check 4: top-level imports survive. Only meaningful for ``.py``
    # files — non-Python touched files (markdown prompts etc.) have no
    # import statements to preserve.
    for file_path in touched_files:
        if file_path.suffix != ".py":
            continue
        # Find the pre-apply equivalent of this file by mapping back
        # through any pre_apply mutation that points at it.
        pre_file: Path | None = None
        try:
            rel_to_target = file_path.relative_to(target_root)
        except ValueError:
            rel_to_target = None
        if rel_to_target is not None:
            for pre_point in pre_apply_mutations:
                try:
                    pre_rel = pre_point.file.relative_to(pre_point.source_root)
                except ValueError:
                    continue
                if pre_rel == rel_to_target:
                    pre_file = pre_point.file
                    break
        if pre_file is None or not pre_file.exists():
            continue
        pre_imports = _toplevel_imports(pre_file)
        post_imports = _toplevel_imports(file_path)
        missing = pre_imports - post_imports
        if missing:
            missing_str = ", ".join(sorted(missing))
            errors.append(
                f"Post-apply file {file_path} dropped top-level imports: {missing_str}"
            )

    return errors


def check_forbidden_ids(
    patches: list[Patch], forbidden_ids: list[str]
) -> list[str]:
    """Return one error string per patch that targets a forbidden id."""

    forbidden = set(forbidden_ids)
    errors: list[str] = []
    for patch in patches:
        if patch.mutation_id in forbidden:
            errors.append(
                f"Patch {patch.id!r}: mutation_id {patch.mutation_id!r} is in the "
                f"forbidden set and may not be patched"
            )
    return errors


__all__ = ["validate_post_apply", "check_forbidden_ids"]
