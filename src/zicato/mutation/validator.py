"""Validation checks that gate a child snapshot's acceptance.

The validator is intentionally narrow: it does NOT score, score-deltas, or
otherwise reason about the experiment. It answers two questions:

* :func:`validate_patches` — *before* application: "is this patch set
  internally well-formed against the mutation surface — does every patch
  target a real enumerated point with an op the point can satisfy?"
* :func:`validate_post_apply` — *after* application: "did the applier
  produce a snapshot that the runner can safely mount?"

Concrete failures are returned as strings rather than raised so the
tournament can log them as a single rejection record per experiment.

Pre-apply checks (:func:`validate_patches`)
-------------------------------------------

1. Every patch's ``mutation_id`` resolves to a :class:`MutationPoint` in
   the enumeration of the mutation surface.
2. The patch's ``op`` is compatible with its payload — ``replace``
   carries ``new_content``, ``set_numeric`` carries ``new_numeric``,
   ``set_enum`` carries ``new_enum``; and no foreign payload field is set.
3. The patch's ``op`` is compatible with its target point's ``kind`` —
   ``set_numeric`` / ``set_enum`` only make sense against a ``span``
   point (they locate a constant after the marker); ``file``-kind points
   only accept ``replace``.

Post-apply checks (:func:`validate_post_apply`)
-----------------------------------------------

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

#: Per-op required / forbidden payload fields and the mutation-point kinds
#: each op can target. ``payload`` is the :class:`Patch` attribute the op
#: consumes; ``kinds`` is the set of :data:`~zicato.core.types.MutationKind`
#: values the op can be applied to. The applier in
#: :mod:`zicato.mutation.applier` is the source of truth these rules
#: mirror — ``replace`` works on any point, while ``set_numeric`` /
#: ``set_enum`` locate a constant after the marker and so require a
#: ``span`` point.
_OP_RULES: dict[str, dict[str, object]] = {
    "replace": {"payload": "new_content", "kinds": frozenset({"span", "file"})},
    "set_numeric": {"payload": "new_numeric", "kinds": frozenset({"span"})},
    "set_enum": {"payload": "new_enum", "kinds": frozenset({"span"})},
}

#: All payload fields a :class:`Patch` can carry. Used to flag a patch
#: that sets a payload field foreign to its op (e.g. a ``replace`` patch
#: that also populates ``new_numeric``).
_ALL_PAYLOAD_FIELDS = ("new_content", "new_numeric", "new_enum")


def validate_patches(
    patches: list[Patch],
    *,
    source_root: Path | None = None,
    enumeration: list[MutationPoint] | None = None,
) -> list[str]:
    """Deterministically pre-validate a patch set against the surface.

    This is a non-LLM, side-effect-free check run *before* application so
    the applier can refuse a malformed batch as a whole rather than
    leaving earlier patches half-applied. It is the deterministic
    guarantee that an edit can only ever land at a valid, enumerated
    ``# zicato:mutable`` point.

    Exactly one of ``source_root`` / ``enumeration`` must be supplied:

    * ``source_root`` — a directory (or ``.py`` file) to enumerate the
      mutation surface from via :func:`enumerate_mutations`.
    * ``enumeration`` — an already-computed list of mutation points
      (callers that have just enumerated the surface for another reason
      can pass it straight through and skip the re-walk).

    Every patch is checked; the return value lists *all* problems found
    rather than stopping at the first. A well-formed batch returns an
    empty list. The checks are:

    1. ``mutation_id`` resolves to an enumerated :class:`MutationPoint`.
    2. ``op`` is compatible with the patch payload — the op's required
       payload field is populated and no foreign payload field is set.
    3. ``op`` is compatible with the target point's ``kind``.

    Raises
    ------
    ValueError
        When neither or both of ``source_root`` / ``enumeration`` are
        supplied — the caller must pick exactly one surface source.
    """

    if (source_root is None) == (enumeration is None):
        raise ValueError("validate_patches: pass exactly one of source_root / enumeration")

    if enumeration is None:
        assert source_root is not None  # narrowed by the guard above
        points = enumerate_mutations([Path(source_root)])
    else:
        points = enumeration
    index: dict[str, MutationPoint] = {p.id: p for p in points}

    errors: list[str] = []
    for patch in patches:
        rule = _OP_RULES.get(patch.op)
        if rule is None:
            # ``op`` is Literal-typed, but a JSON-constructed Patch can
            # smuggle in an unknown string; flag it rather than trust it.
            errors.append(
                f"Patch {patch.id!r}: unknown op {patch.op!r} "
                f"(expected one of {sorted(_OP_RULES)})"
            )
            continue

        # Check 2a: the op's required payload field must be populated.
        required_field = str(rule["payload"])
        if getattr(patch, required_field) is None:
            errors.append(f"Patch {patch.id!r}: op={patch.op} requires {required_field}")
        # Check 2b: no foreign payload field may be set.
        for field_name in _ALL_PAYLOAD_FIELDS:
            if field_name == required_field:
                continue
            if getattr(patch, field_name) is not None:
                errors.append(
                    f"Patch {patch.id!r}: op={patch.op} must not set "
                    f"{field_name} (only {required_field} applies)"
                )

        # Check 1: the mutation id must resolve to an enumerated point.
        point = index.get(patch.mutation_id)
        if point is None:
            errors.append(
                f"Patch {patch.id!r}: mutation_id {patch.mutation_id!r} does "
                f"not resolve to an enumerated mutation point"
            )
            continue

        # Check 3: the op must be compatible with the point's kind.
        compatible_kinds = rule["kinds"]
        assert isinstance(compatible_kinds, frozenset)  # see _OP_RULES
        if point.kind not in compatible_kinds:
            errors.append(
                f"Patch {patch.id!r}: op={patch.op} is incompatible with "
                f"mutation point {patch.mutation_id!r} of kind {point.kind!r} "
                f"(op={patch.op} requires kind in {sorted(compatible_kinds)})"
            )

    return errors


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
            errors.append(f"Post-apply file {file_path} dropped top-level imports: {missing_str}")

    return errors


def check_forbidden_ids(patches: list[Patch], forbidden_ids: list[str]) -> list[str]:
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


__all__ = ["validate_patches", "validate_post_apply", "check_forbidden_ids"]
