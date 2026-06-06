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
import logging
from pathlib import Path

from zicato.core.types import MutationPoint
from zicato.mutation.markers import is_end_marker, parse_marker_line

_log = logging.getLogger(__name__)


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


def _enumerate_file(file_path: Path, source_root: Path) -> list[MutationPoint]:
    """Enumerate mutation points within a single Python file."""

    try:
        text = file_path.read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        # Unparseable files are skipped — the validator catches this kind
        # of breakage post-apply; pre-apply enumeration is best-effort.
        # Log it, though: silently returning ``[]`` is what turns a local
        # snapshot corruption into a confusing crash one round later (the
        # next ``derive_generation`` finds the file's ids missing and raises
        # ``KeyError``). The warning attributes the drop to the real cause.
        _log.warning(
            "enumerator: dropping unparseable file %s (SyntaxError: %s); "
            "its mutation ids will not resolve",
            file_path,
            exc,
        )
        return []

    lines = text.splitlines(keepends=True)
    literal_spans = _collect_literal_spans(tree)
    points: list[MutationPoint] = []

    # Lines that fall INSIDE a string literal don't count as marker
    # comments — they're documentation / examples written by the
    # operator. A marker comment must be a real ``#`` comment on a code
    # line, not text that happens to start with ``#`` inside a docstring.
    literal_line_set: set[int] = set()
    for line_start, line_end, _node in literal_spans:
        for line_no in range(line_start, line_end + 1):
            literal_line_set.add(line_no)

    # Walk markers in source order so file-level (top-of-module) markers
    # are emitted before span markers within the same file. The index is
    # walked explicitly (not ``enumerate``) so a ``:code`` region can
    # advance the cursor past its body in one step.
    idx = 0
    n = len(lines)
    while idx < n:
        raw_line = lines[idx]
        marker_line = idx + 1  # 1-indexed
        stripped = raw_line.rstrip("\n").rstrip("\r")
        parsed = parse_marker_line(stripped)
        if parsed is None:
            idx += 1
            continue
        if marker_line in literal_line_set:
            # Inside a docstring / module-level string literal — operators
            # use this for marker-syntax examples; treat as documentation.
            idx += 1
            continue
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
            idx += 1
            continue
        if parsed.is_code:
            # Pointed code region: scan forward for the matching
            # ``# zicato:mutable:end`` sentinel. The region BODY is the
            # lines strictly between the opening and closing markers
            # (exclusive of both); the proposer rewrites that block
            # verbatim. An unterminated region (no ``:end`` before EOF)
            # is silently dropped — the audit CLI surfaces the missing id
            # the same way a dangling span marker is dropped.
            end_idx: int | None = None
            for scan in range(idx + 1, n):
                if is_end_marker(lines[scan].rstrip("\n").rstrip("\r")):
                    end_idx = scan
                    break
            if end_idx is None:
                idx += 1
                continue
            body_start = idx + 2  # 1-indexed line after the opening marker
            body_end = end_idx  # 1-indexed line before the closing marker
            if body_end < body_start:
                # Empty region (``:code`` immediately followed by
                # ``:end``) — nothing mutable; skip past the sentinel.
                idx = end_idx + 1
                continue
            code_text = "".join(lines[body_start - 1 : body_end])
            points.append(
                MutationPoint(
                    id=parsed.id,
                    kind="code",
                    file=file_path,
                    source_root=source_root,
                    line_start=body_start,
                    line_end=body_end,
                    content=code_text,
                    content_hash=_content_hash(code_text),
                    metadata=dict(parsed.metadata),
                )
            )
            # Advance past the whole region (including the sentinel) so
            # any markers that happen to sit inside the body are not
            # re-interpreted as independent points.
            idx = end_idx + 1
            continue
        span = _resolve_span_for_marker(marker_line, literal_spans)
        if span is None:
            idx += 1
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
        idx += 1
    return points


def enumerate_mutations(source_roots: list[Path]) -> list[MutationPoint]:
    """Walk source roots and return every mutation point we can find.

    Two discovery passes run for each root:

    * The native marker pass — walks ``*.py`` files for
      ``# zicato:mutable`` comment markers and binds them to the nearest
      string literal (the historical behaviour).
    * The manifest bridge — if the root looks like a goldfive worktree
      (carries an ``optimization/manifest.toml``), the bridge in
      :mod:`zicato.synthetic.manifest_bridge` translates each manifest
      entry into a :class:`MutationPoint`. This is how target 2 of the
      dogfood plan exposes goldfive's prompt + threshold surface
      without sprinkling zicato-specific markers through the upstream
      tree.

    Results are sorted by ``(source_root, file, line_start, id)`` for
    determinism. Callers can rely on that order across runs as long
    as the source tree's content is unchanged.

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

    # Manifest bridge — additive, runs after the native marker pass.
    # Imported here (not at module top) so the bridge's tomllib /
    # synthetic-package dependencies stay out of the import path for
    # operators who only ever use the marker form.
    try:
        from zicato.synthetic.manifest_bridge import (  # noqa: PLC0415
            enumerate_manifest_points,
        )
    except ImportError:
        # The bridge module is sibling-package; if it ever moves and
        # the import fails, the enumerator should still return the
        # marker-discovered points rather than crashing.
        pass
    else:
        out.extend(enumerate_manifest_points(source_roots))

    out.sort(key=lambda p: (str(p.source_root), str(p.file), p.line_start, p.id))
    return out


__all__ = ["enumerate_mutations"]
