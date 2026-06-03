"""Copy + patch the inner-harness source tree.

The applier never mutates ``source_root`` — it always materialises a fresh
copy at ``target_root`` first, then resolves every :class:`Patch` against
that fresh tree. This isolation is load-bearing: an experiment that fails
mid-apply must not leave the parent generation's snapshot half-rewritten,
and the operator must always be able to diff parent vs child cleanly.

Atomicity
---------

:func:`apply_patches` is **all-or-nothing**: it runs the deterministic
:func:`~zicato.mutation.validator.validate_patches` pre-check against the
freshly-copied tree first and, if any patch is malformed, raises before a
single edit lands and removes the copied tree so generation lineage stays
append-only. The raw best-effort-sequential behaviour is still reachable
through :func:`apply_patches_unchecked` for the rare caller that has
already validated the batch itself.
"""

from __future__ import annotations

import ast
import shutil
import textwrap
from collections.abc import Callable
from pathlib import Path

from zicato.core.types import MutationPoint, Patch
from zicato.epoch.snapshot_scope import copytree_ignore
from zicato.mutation.enumerator import enumerate_mutations
from zicato.mutation.markers import is_end_marker, parse_marker_line
from zicato.mutation.validator import validate_patches

#: A ``shutil.copytree``-compatible ``ignore`` callable. When a caller
#: passes ``None`` the applier falls back to the shared snapshot-scope
#: policy (:func:`zicato.epoch.snapshot_scope.copytree_ignore`) so the
#: copied child tree is code-only — run artifacts never propagate into a
#: derived generation.
CopytreeIgnore = Callable[[str, list[str]], set[str]]


def _write_text_writable(file_path: Path, content: str) -> None:
    """Write ``content`` to ``file_path``, restoring user-write first.

    The copied child tree inherits file modes from ``source_root``. When
    the parent snapshot is read-only (mode ``0o444``, as happens for
    immutable / archived mutable trees), the inherited copies are also
    read-only and ``Path.write_text`` raises ``PermissionError``. Add the
    owner-write bit before writing so a patch can land on top of a
    read-only snapshot. The chmod is guarded for existence — every patch
    op edits a file that already exists, but staying defensive keeps the
    helper safe for any future create path.
    """

    if file_path.exists():
        file_path.chmod(file_path.stat().st_mode | 0o200)
    file_path.write_text(content, encoding="utf-8")


def _format_numeric(value: float) -> str:
    """Render a numeric replacement value back into Python source.

    The applier prefers an integer rendering when ``value`` is an exact
    integer to avoid spurious ``1.0`` -> ``1`` diffs; otherwise ``repr``
    gives a stable round-trippable string.
    """

    if value == int(value):
        return str(int(value))
    return repr(value)


def _resolve_marker_line(file_path: Path, mutation_id: str) -> int | None:
    """Return the 1-indexed line of the marker comment for ``mutation_id``.

    Used by numeric/enum patches that need to find the target constant
    even though the enumerator's string-only resolution does not see it.
    """

    text = file_path.read_text(encoding="utf-8")
    for idx, line in enumerate(text.splitlines()):
        parsed = parse_marker_line(line)
        if parsed is not None and parsed.id == mutation_id:
            return idx + 1
    return None


def _find_constant_after(
    file_path: Path,
    marker_line: int,
    want_numeric: bool,
) -> ast.Constant | None:
    """Find the nearest constant (numeric or string) starting after ``marker_line``.

    ``want_numeric=True`` filters to numeric constants; ``False`` filters
    to string constants. Returns the AST node so the caller can read
    ``col_offset`` / ``end_col_offset`` for an in-place rewrite.
    """

    text = file_path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    best: ast.Constant | None = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant):
            continue
        if want_numeric:
            if not isinstance(node.value, int | float) or isinstance(node.value, bool):
                continue
        else:
            if not isinstance(node.value, str):
                continue
        if node.lineno <= marker_line:
            continue
        if best is None or (
            node.lineno < best.lineno
            or (node.lineno == best.lineno and node.col_offset < best.col_offset)
        ):
            best = node
    return best


def _replace_node_text(
    file_path: Path,
    node: ast.expr | ast.stmt,
    new_text: str,
) -> None:
    """Replace the substring covered by ``node`` with ``new_text``."""

    text = file_path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    line_start = node.lineno
    line_end = node.end_lineno or line_start
    col_start = node.col_offset
    col_end = node.end_col_offset
    if col_end is None:
        # Fall back to end-of-line if the parser didn't give us a column.
        col_end = len(lines[line_end - 1].rstrip("\n").rstrip("\r"))

    # Compute the absolute byte offsets in the joined string by walking
    # the line list. Using character offsets is fine because Python's
    # AST exposes column offsets in characters for ``ast.parse``-d source.
    before = "".join(lines[: line_start - 1]) + lines[line_start - 1][:col_start]
    after = lines[line_end - 1][col_end:] + "".join(lines[line_end:])
    _write_text_writable(file_path, before + new_text + after)


def _looks_like_python_string_literal(text: str) -> bool:
    """Heuristic: does ``text`` already look like a Python source-form
    string literal (quoted, possibly triple-quoted, possibly prefixed)?

    Used to decide whether ``new_content`` came in as raw prose (in
    which case the applier must wrap it as a literal) or as fully-formed
    Python source (in which case the applier should preserve it
    verbatim). The check is intentionally permissive — false positives
    on this branch only mean the applier writes the operator's
    pre-formatted source unchanged.
    """
    stripped = text.lstrip()
    # Skip optional string prefixes: r, R, b, B, u, U, f, F (and combos).
    i = 0
    while i < len(stripped) and i < 2 and stripped[i] in "rRbBuUfF":
        i += 1
    rest = stripped[i:]
    return rest.startswith(('"""', "'''", '"', "'"))


def _quote_as_python_string(content: str, indent: str) -> str:
    """Wrap ``content`` as a Python triple-quoted string literal.

    The indent prefix is the leading whitespace of the original span's
    first line so the produced source slots into the original
    syntactic position (kwarg value, return expression, parenthesised
    concat, etc.) without disturbing the surrounding code's indent.

    Triple double-quotes are used unless ``content`` itself contains a
    triple-double-quote sequence, in which case we fall back to triple
    single-quotes. When both collide we hard-escape one — rare enough
    that the fallback's mild ugliness is acceptable.
    """
    if '"""' not in content:
        return f'{indent}"""{content}"""'
    if "'''" not in content:
        return f"{indent}'''{content}'''"
    # Both triple-quote forms appear in the content; escape one.
    escaped = content.replace('"""', '\\"\\"\\"')
    return f'{indent}"""{escaped}"""'


def _literal_line_prefix(text: str) -> str:
    """Return the first-line anchor prefix for a span replacement.

    Normally a span's replacement is re-anchored to the first non-empty
    line's leading whitespace (its indent). The one exception is an
    explicit ``+``-joined concatenation — the form pointed sub-clauses use
    to keep
    adjacent clauses as separate AST literals, e.g.::

        + "Pass the ``topic`` ..."

    The enumerator slices the span out by whole lines, so its first line
    carries that leading ``+`` operator. Dropping it on a replace would
    merge the clause into its neighbour (adjacent literals implicitly
    concatenate) and the span's own marker would stop resolving. So when
    the span's first non-empty line opens with a ``+`` continuation
    operator, the prefix (indent + ``+`` + the gap up to the opening
    quote) is preserved verbatim. For every other span shape — kwarg
    value, docstring, plain assignment — this returns exactly the leading
    indent, byte-identical to the historical behaviour.
    """
    for line in text.splitlines():
        stripped = line.lstrip()
        if not stripped:
            continue
        indent = line[: len(line) - len(stripped)]
        if not stripped.startswith("+"):
            return indent
        # Leading ``+`` continuation: keep everything up to the opening
        # quote so the operator survives the replace.
        quote = len(line)
        for i, ch in enumerate(line):
            if ch in "\"'":
                quote = i
                break
        return line[:quote]
    return ""


def _reindent_python_literal(literal: str, indent: str) -> str:
    """Re-anchor a Python string-literal span to ``indent``.

    A ``span`` replace targets a string literal that sits at a fixed
    syntactic position — a function-body docstring, a kwarg value, a
    parenthesised concat. The literal's *first* line must carry exactly
    the original span's leading indent; emitting it at any other indent
    produces an ``IndentationError`` (a docstring one level too deep, a
    dedented continuation line, etc.). A proposer working from a
    truncated preview cannot reliably reproduce that indent, so the
    applier owns it: the literal is dedented to a common baseline and
    re-anchored to ``indent``.

    The transform is whitespace-only and idempotent — re-applying it to
    an already-correctly-indented literal is a no-op. Lines that are
    blank (after stripping) are emitted empty so trailing whitespace is
    not introduced inside the literal body.
    """

    raw_lines = literal.splitlines(keepends=True)
    if not raw_lines:
        return literal
    # Strip the existing leading whitespace from the first line; the
    # common indent of the *continuation* lines is preserved verbatim so
    # the literal's own internal structure (a deliberately indented
    # docstring body, say) is not flattened.
    first = raw_lines[0]
    first_stripped = first.lstrip(" \t")
    out = [indent + first_stripped]
    out.extend(raw_lines[1:])
    return "".join(out)


def _region_base_indent(original_body: str) -> str:
    """Return the leading-whitespace indent of a ``:code`` region's body.

    The region body is the verbatim source between the ``:code`` and
    ``:end`` markers, sliced out by whole lines. Every line of that body
    sits at (or deeper than) one common base indent — the indent of the
    enclosing suite. That base indent is the anchor the applier MUST
    re-attach the replacement to: the marker comments around the region
    carry the same indent, so a replacement written at any other column
    leaves the region body mis-aligned with the ``:end`` marker / the
    statements that follow and the file stops parsing.

    Returns the indent of the first non-blank body line. An all-blank
    body (or empty body) returns the empty string.
    """
    for line in original_body.splitlines():
        stripped = line.lstrip(" \t")
        if stripped:
            return line[: len(line) - len(stripped)]
    return ""


def _reindent_code_region(new_content: str, indent: str) -> str:
    """Re-anchor a ``:code`` replacement body to ``indent``.

    A ``:code`` region is real Python control flow that the proposer
    rewrites verbatim. A proposer working from a truncated preview cannot
    reliably reproduce the region's exact leading indent — it routinely
    emits the new block at column 0, or one level too deep, or with the
    surrounding marker comments accidentally included. Any of those leaves
    the snapshot unparseable, which in turn makes the file fail to
    re-enumerate (the mutation id "vanishes") and reads as having dropped
    every top-level import (a syntax error yields an empty import set).

    The applier owns the indent so a ``:code`` replace can never shift the
    block off its column. The transform:

    1. Drops any marker lines (the opening ``:code`` marker or the closing
       ``:end`` sentinel) the proposer mistakenly echoed back — only the
       region BODY belongs between the markers.
    2. Dedents the remaining lines to a common baseline
       (:func:`textwrap.dedent`) so the proposer's own relative indentation
       (nested ``if`` / ``for`` bodies) is preserved.
    3. Re-anchors every non-blank line to ``indent``.

    Blank lines are emitted empty so no trailing whitespace is introduced.
    The transform is idempotent: a body that already sits at ``indent``
    with no stray markers round-trips unchanged.
    """
    raw_lines = new_content.splitlines()
    # Drop any marker lines the proposer echoed back — the body is the
    # region's interior only; the markers are owned by the surrounding
    # file and re-emitted by the applier.
    kept = [
        line for line in raw_lines if parse_marker_line(line) is None and not is_end_marker(line)
    ]
    if not kept:
        return ""
    dedented = textwrap.dedent("\n".join(kept))
    out: list[str] = []
    for line in dedented.splitlines():
        if line.strip():
            out.append(indent + line)
        else:
            out.append("")
    return "\n".join(out) + "\n"


def _apply_span_replace(point: MutationPoint, new_content: str) -> None:
    """Replace a span point's content with ``new_content``.

    For file-kind points the new content overwrites the whole file.
    For code-kind points (a ``# zicato:mutable:code`` region) the body
    lines between the markers are replaced verbatim — the region is
    real Python control flow, so ``new_content`` is fully-formed source
    and the applier must not wrap it as a string literal. The proposer
    is responsible for producing a block that parses in place (the
    post-apply syntax check is the backstop).
    For span-kind points the policy depends on the file's syntax:

    * For ``.py`` files, the span is a Python string-literal expression
      and the applier either preserves ``new_content`` verbatim when it
      already looks like a Python source-form string literal, or wraps
      it as a triple-quoted literal. The wrap step is what makes
      "describe-the-new-string-as-prose" patches survive the post-apply
      syntax check.
    * For non-``.py`` files (markdown prompt bodies, plain text, etc.)
      the span is plain content; the applier writes ``new_content``
      verbatim with no Python-syntactic wrapping. This is the path the
      target-2 manifest bridge takes when a patch rewrites a prompt
      markdown body — wrapping the body in ``\"\"\"...\"\"\"`` would
      corrupt the file.
    """

    text = point.file.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    if point.kind == "file":
        _write_text_writable(point.file, new_content)
        return
    before = "".join(lines[: point.line_start - 1])
    after = "".join(lines[point.line_end :])

    if point.kind == "code":
        # Pointed code region: the body is verbatim source between the
        # markers. The proposer rewrites real Python control flow, but it
        # cannot reliably reproduce the region's leading indent (it works
        # from a preview and routinely emits the block at column 0, or one
        # level too deep, or with the surrounding marker comments echoed
        # back). The applier owns the indent: it strips any stray marker
        # lines, dedents the body to a common baseline, and re-anchors it
        # to the original region's indent so the replacement stays
        # syntactically valid in place and the region re-enumerates. Only
        # the BODY between the markers is rewritten; the ``:code`` /
        # ``:end`` markers (which live just outside the body span) are
        # untouched, so the mutation id keeps resolving.
        original_body = "".join(lines[point.line_start - 1 : point.line_end])
        indent = _region_base_indent(original_body)
        middle = _reindent_code_region(new_content, indent)
        _write_text_writable(point.file, before + middle + after)
        return

    if point.file.suffix == ".py":
        # The original span's first-line indent is the syntactic anchor
        # the replacement MUST sit at — the enumerator bound the span to
        # a string-literal node, so its first line opens a statement (a
        # docstring) or an expression at a fixed column. The proposer
        # may emit ``new_content`` at the wrong indent (working from a
        # preview, it cannot see the surrounding suite); the applier
        # re-anchors it so a span replace can never shift the literal
        # off its column and break the enclosing block's indentation.
        original_span = "".join(lines[point.line_start - 1 : point.line_end])
        indent = _literal_line_prefix(original_span)
        if _looks_like_python_string_literal(new_content):
            middle = _reindent_python_literal(new_content, indent)
        else:
            middle = _quote_as_python_string(new_content, indent)
    else:
        # Non-Python span: write the content verbatim. The enumerator
        # binds the span to a content range in the source file (e.g.
        # the prompt body inside a manifest-bridged markdown file);
        # syntactic wrapping would corrupt the surrounding format.
        middle = new_content

    if not middle.endswith("\n"):
        # Preserve the trailing newline that the original span almost
        # certainly had — joining without it would merge the span with
        # the line below in the new file.
        if "".join(lines[point.line_start - 1 : point.line_end]).endswith("\n"):
            middle = middle + "\n"
    _write_text_writable(point.file, before + middle + after)


def _build_index(points: list[MutationPoint]) -> dict[str, MutationPoint]:
    index: dict[str, MutationPoint] = {}
    for p in points:
        index[p.id] = p
    return index


def apply_patches(
    source_root: Path,
    patches: list[Patch],
    target_root: Path,
    *,
    ignore: CopytreeIgnore | None = None,
) -> None:
    """Materialise a child snapshot and atomically apply ``patches`` to it.

    This is the default, **all-or-nothing** apply path. Behaviour:

    1. Recursively copy ``source_root`` to ``target_root``, skipping run
       artifacts (``output/``, caches — see
       :mod:`zicato.epoch.snapshot_scope`). ``target_root`` must not
       already exist; the applier refuses to overwrite an existing tree
       to keep generation lineage append-only. ``ignore`` overrides the
       default snapshot-scope filter when a caller needs a wider skip
       set.
    2. Run the deterministic
       :func:`~zicato.mutation.validator.validate_patches` pre-check
       against the freshly-copied tree. Every patch is checked up front:
       its ``mutation_id`` must resolve to a real enumerated point, its
       ``op`` must be compatible with its payload, and its ``op`` must be
       compatible with the target point's ``kind``.
    3. If any patch fails validation, remove the copied tree and raise —
       so a malformed batch leaves *nothing* half-applied.
    4. Otherwise delegate to :func:`apply_patches_unchecked`, which
       applies every patch in order against the copied tree.

    Because the whole batch is validated before the first edit lands, the
    deterministic guarantee holds: an edit can only ever land at a valid,
    enumerated ``# zicato:mutable`` point, and a batch with one bad patch
    applies none.

    Callers that genuinely need the legacy best-effort-sequential
    behaviour (no atomic pre-check) can call :func:`apply_patches_unchecked`
    directly.

    Raises
    ------
    FileExistsError
        When ``target_root`` already exists.
    ValueError
        When the patch set fails :func:`validate_patches` — the error
        message enumerates every problem found. The copied tree is
        removed before the exception propagates.
    """

    source_root = Path(source_root).resolve()
    target_root = Path(target_root).resolve()
    if target_root.exists():
        raise FileExistsError(
            f"apply_patches: target_root {target_root} already exists; refusing to overwrite"
        )
    shutil.copytree(source_root, target_root, ignore=ignore or copytree_ignore())

    # Atomic pre-validation: enumerate the freshly-copied tree and check
    # every patch up front. The copied tree has identical content to
    # ``source_root``, so its enumeration is the surface the subsequent
    # apply will resolve against.
    problems = validate_patches(patches, source_root=target_root)
    if problems:
        # Refuse the whole batch — remove the copied tree so generation
        # lineage stays append-only and nothing is left half-applied.
        shutil.rmtree(target_root, ignore_errors=True)
        raise ValueError(
            "apply_patches: refusing to apply patch set; "
            f"{len(problems)} validation problem(s): " + "; ".join(problems)
        )

    _apply_patches_into_tree(target_root, patches)


def apply_patches_unchecked(
    source_root: Path,
    patches: list[Patch],
    target_root: Path,
) -> None:
    """Materialise a child snapshot and apply ``patches`` best-effort.

    This is the **legacy, non-atomic** apply path, preserved for callers
    that have already pre-validated the patch set themselves (e.g. via
    :func:`~zicato.mutation.validator.validate_patches`). Prefer
    :func:`apply_patches`, which validates the batch and is all-or-nothing.

    Behavior
    --------
    1. Recursively copy ``source_root`` to ``target_root``. ``target_root``
       must not already exist; the applier refuses to overwrite an
       existing tree to keep generation lineage append-only.
    2. Re-enumerate mutations from ``target_root`` (line numbers in the
       enumerated points reference the freshly-copied tree, not the
       original).
    3. For each patch, look up its target mutation point by id. Raise
       :class:`KeyError` when the id is unknown. The whole-batch apply
       is best-effort sequential — earlier patches that succeeded stay
       applied even if a later patch raises.
    4. Dispatch by op:

       * ``replace``: rewrite span content (or whole-file content for a
         file-kind point) with ``new_content``.
       * ``set_numeric``: find the nearest numeric constant after the
         marker line and replace it with ``new_numeric``.
       * ``set_enum``: find the nearest string constant after the marker
         line and replace it with the quoted form of ``new_enum``.

    Raises
    ------
    FileExistsError
        When ``target_root`` already exists.
    KeyError
        When a patch's ``mutation_id`` does not resolve.
    ValueError
        When a patch's op is incompatible with its payload (e.g. a
        ``set_numeric`` patch without a ``new_numeric`` value).
    """

    source_root = Path(source_root).resolve()
    target_root = Path(target_root).resolve()
    if target_root.exists():
        raise FileExistsError(
            f"apply_patches_unchecked: target_root {target_root} already "
            f"exists; refusing to overwrite"
        )
    shutil.copytree(source_root, target_root)
    _apply_patches_into_tree(target_root, patches)


def _apply_patches_into_tree(target_root: Path, patches: list[Patch]) -> None:
    """Apply ``patches`` in order against an already-materialised tree.

    The shared, best-effort-sequential core of :func:`apply_patches` and
    :func:`apply_patches_unchecked`. ``target_root`` must already exist
    (the callers handle the copy + their respective pre-checks).
    """

    points = enumerate_mutations([target_root])
    index = _build_index(points)

    for patch in patches:
        if patch.op == "replace":
            if patch.new_content is None:
                raise ValueError(f"Patch {patch.id!r}: op=replace requires new_content")
            point = index.get(patch.mutation_id)
            if point is None:
                raise KeyError(
                    f"Patch {patch.id!r}: mutation_id {patch.mutation_id!r} not found "
                    f"in target_root {target_root}"
                )
            _apply_span_replace(point, patch.new_content)
            # Re-enumerate so subsequent patches see updated line numbers.
            points = enumerate_mutations([target_root])
            index = _build_index(points)
        elif patch.op == "set_numeric":
            if patch.new_numeric is None:
                raise ValueError(f"Patch {patch.id!r}: op=set_numeric requires new_numeric")
            point = index.get(patch.mutation_id)
            if point is None:
                raise KeyError(
                    f"Patch {patch.id!r}: mutation_id {patch.mutation_id!r} not found "
                    f"in target_root {target_root}"
                )
            marker_line = _resolve_marker_line(point.file, patch.mutation_id)
            if marker_line is None:
                raise KeyError(
                    f"Patch {patch.id!r}: marker for {patch.mutation_id!r} "
                    f"vanished in target_root"
                )
            # For numeric replacement, ignore the string-literal span the
            # enumerator bound to and locate the numeric constant directly.
            node = _find_constant_after(point.file, marker_line, want_numeric=True)
            if node is None:
                raise ValueError(
                    f"Patch {patch.id!r}: no numeric constant found after marker "
                    f"for {patch.mutation_id!r} in {point.file}"
                )
            _replace_node_text(point.file, node, _format_numeric(patch.new_numeric))
            points = enumerate_mutations([target_root])
            index = _build_index(points)
        elif patch.op == "set_enum":
            if patch.new_enum is None:
                raise ValueError(f"Patch {patch.id!r}: op=set_enum requires new_enum")
            point = index.get(patch.mutation_id)
            if point is None:
                raise KeyError(
                    f"Patch {patch.id!r}: mutation_id {patch.mutation_id!r} not found "
                    f"in target_root {target_root}"
                )
            marker_line = _resolve_marker_line(point.file, patch.mutation_id)
            if marker_line is None:
                raise KeyError(
                    f"Patch {patch.id!r}: marker for {patch.mutation_id!r} "
                    f"vanished in target_root"
                )
            node = _find_constant_after(point.file, marker_line, want_numeric=False)
            if node is None:
                raise ValueError(
                    f"Patch {patch.id!r}: no string constant found after marker "
                    f"for {patch.mutation_id!r} in {point.file}"
                )
            _replace_node_text(point.file, node, repr(patch.new_enum))
            points = enumerate_mutations([target_root])
            index = _build_index(points)
        else:  # pragma: no cover — Literal-typed; defensive
            raise ValueError(f"Patch {patch.id!r}: unknown op {patch.op!r}")


__all__ = ["apply_patches", "apply_patches_unchecked"]
