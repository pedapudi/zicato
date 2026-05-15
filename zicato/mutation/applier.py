"""Copy + patch the inner-harness source tree.

The applier never mutates ``source_root`` — it always materialises a fresh
copy at ``target_root`` first, then resolves every :class:`Patch` against
that fresh tree. This isolation is load-bearing: an experiment that fails
mid-apply must not leave the parent generation's snapshot half-rewritten,
and the operator must always be able to diff parent vs child cleanly.
"""

from __future__ import annotations

import ast
import shutil
from pathlib import Path

from zicato.core.types import MutationPoint, Patch
from zicato.mutation.enumerator import enumerate_mutations
from zicato.mutation.markers import parse_marker_line


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
            if not isinstance(node.value, (int, float)) or isinstance(node.value, bool):
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
    file_path.write_text(before + new_text + after, encoding="utf-8")


def _apply_span_replace(point: MutationPoint, new_content: str) -> None:
    """Replace a span point's content with ``new_content``."""

    text = point.file.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    if point.kind == "file":
        point.file.write_text(new_content, encoding="utf-8")
        return
    before = "".join(lines[: point.line_start - 1])
    after = "".join(lines[point.line_end :])
    middle = new_content
    if not middle.endswith("\n"):
        # Preserve the trailing newline that the original span almost
        # certainly had — joining without it would merge the span with
        # the line below in the new file.
        if "".join(lines[point.line_start - 1 : point.line_end]).endswith("\n"):
            middle = middle + "\n"
    point.file.write_text(before + middle + after, encoding="utf-8")


def _build_index(points: list[MutationPoint]) -> dict[str, MutationPoint]:
    index: dict[str, MutationPoint] = {}
    for p in points:
        index[p.id] = p
    return index


def apply_patches(
    source_root: Path,
    patches: list[Patch],
    target_root: Path,
) -> None:
    """Materialise a child snapshot and apply ``patches`` to it.

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
       applied even if a later patch raises. Callers that want atomic
       application should pre-validate the patch set against an
       enumeration before calling.
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
            f"apply_patches: target_root {target_root} already exists; refusing to overwrite"
        )
    shutil.copytree(source_root, target_root)

    points = enumerate_mutations([target_root])
    index = _build_index(points)

    for patch in patches:
        if patch.op == "replace":
            if patch.new_content is None:
                raise ValueError(
                    f"Patch {patch.id!r}: op=replace requires new_content"
                )
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
                raise ValueError(
                    f"Patch {patch.id!r}: op=set_numeric requires new_numeric"
                )
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
                raise ValueError(
                    f"Patch {patch.id!r}: op=set_enum requires new_enum"
                )
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


__all__ = ["apply_patches"]
