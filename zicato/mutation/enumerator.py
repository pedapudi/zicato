"""Walk source roots and produce :class:`MutationPoint` instances.

The enumerator is the single source of truth about what's mutable. The
applier and the audit CLI both re-enter through :func:`enumerate_mutations`
rather than caching an enumeration — line numbers drift as patches land,
and a stale cache is the easiest way to clobber the wrong span.

Resolution rules
----------------

* A ``# zicato:mutable id="..."`` comment binds to the nearest string
  literal whose source line is greater than the comment's line. The bound
  literal may be the value of an assignment (``FOO = "..."``), a keyword
  argument (``instruction="..."``), a positional argument
  (``LlmAgent("...")``), or any other expression context — the enumerator
  does not care, it just needs a string-valued AST node it can resolve to
  a concrete span.
* A ``# zicato:mutable:file id="..."`` comment declares the whole file
  mutable; the span is the entire file (1..N lines).
* If a span marker has no string literal beneath it, the marker is
  silently ignored. (Operators get feedback through the audit CLI when an
  id is missing from the listing.)

Numeric / enum mutation points
------------------------------

The marker syntax targets a string literal by default. For numeric- or
enum-typed mutation points the operator still annotates the assignment
that holds the value; the applier (not the enumerator) is responsible for
the type-specific rewrite logic (see :mod:`zicato.mutation.applier`).
"""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path

from zicato.core.types import MutationPoint
from zicato.mutation.markers import parse_marker_line


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _collect_literal_spans(tree: ast.AST) -> list[tuple[int, int, ast.AST]]:
    """Return ``(line_start, line_end, node)`` for every string-literal
    span in the tree.

    The list is unsorted; the caller indexes into it by line.
    """

    spans: list[tuple[int, int, ast.AST]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            line_start = node.lineno
            line_end = getattr(node, "end_lineno", line_start) or line_start
            spans.append((line_start, line_end, node))
    return spans


def _resolve_span_for_marker(
    marker_line: int,
    literal_spans: list[tuple[int, int, ast.AST]],
) -> tuple[int, int] | None:
    """Find the nearest string-literal span starting after ``marker_line``."""

    best: tuple[int, int] | None = None
    for line_start, line_end, _node in literal_spans:
        if line_start <= marker_line:
            continue
        if best is None or line_start < best[0]:
            best = (line_start, line_end)
    return best


def _enumerate_file(
    file_path: Path, source_root: Path
) -> list[MutationPoint]:
    """Enumerate mutation points within a single Python file."""

    try:
        text = file_path.read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        tree = ast.parse(text)
    except SyntaxError:
        # Unparseable files are skipped — the validator catches this kind
        # of breakage post-apply; pre-apply enumeration is best-effort.
        return []

    lines = text.splitlines(keepends=True)
    literal_spans = _collect_literal_spans(tree)
    points: list[MutationPoint] = []

    # Walk markers in source order so file-level (top-of-module) markers
    # are emitted before span markers within the same file.
    for idx, raw_line in enumerate(lines):
        parsed = parse_marker_line(raw_line.rstrip("\n").rstrip("\r"))
        if parsed is None:
            continue
        marker_line = idx + 1  # 1-indexed
        if parsed.is_file:
            content = text
            points.append(
                MutationPoint(
                    id=parsed.id,
                    kind="file",
                    file=file_path,
                    source_root=source_root,
                    line_start=1,
                    line_end=len(lines) if lines else 1,
                    content=content,
                    content_hash=_content_hash(content),
                    metadata=dict(parsed.metadata),
                )
            )
            continue
        span = _resolve_span_for_marker(marker_line, literal_spans)
        if span is None:
            continue
        line_start, line_end = span
        # Slice the literal's lines out of the source. ast line numbers
        # are 1-indexed, end-inclusive.
        span_text = "".join(lines[line_start - 1 : line_end])
        points.append(
            MutationPoint(
                id=parsed.id,
                kind="span",
                file=file_path,
                source_root=source_root,
                line_start=line_start,
                line_end=line_end,
                content=span_text,
                content_hash=_content_hash(span_text),
                metadata=dict(parsed.metadata),
            )
        )
    return points


def enumerate_mutations(source_roots: list[Path]) -> list[MutationPoint]:
    """Walk Python files under each source root, return all mutation points.

    Results are sorted by ``(source_root, file, line_start)`` for
    determinism. Callers can rely on that order across runs as long as
    the source tree's content is unchanged.

    Parameters
    ----------
    source_roots:
        Source-root directories to walk. Each is normalised via
        :meth:`Path.resolve` so the returned mutation points carry
        absolute paths.
    """

    out: list[MutationPoint] = []
    for raw_root in source_roots:
        root = Path(raw_root).resolve()
        if not root.exists():
            continue
        if root.is_file() and root.suffix == ".py":
            out.extend(_enumerate_file(root, root.parent))
            continue
        for py_file in sorted(root.rglob("*.py")):
            out.extend(_enumerate_file(py_file.resolve(), root))
    out.sort(key=lambda p: (str(p.source_root), str(p.file), p.line_start))
    return out


__all__ = ["enumerate_mutations"]
