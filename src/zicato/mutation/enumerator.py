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
* If a span marker has no string literal beneath it, the marker
  contributes no point. Operators get feedback through the audit CLI when
  an id is missing from the listing, and any caller that needs the fact
  structurally (rather than out of the log) wraps the walk in
  :func:`collect_unbound_span_markers`.

Numeric / enum mutation points
------------------------------

The marker syntax targets a string literal by default. For numeric- or
enum-typed mutation points the operator still annotates the assignment
that holds the value; the applier (not the enumerator) is responsible for
the type-specific rewrite logic (see :mod:`zicato.mutation.applier`).

Non-Python files
----------------

Markers are not a Python-only surface. Any file whose suffix the
contract's syntax table declares (see :mod:`zicato.mutation.markers`) is
walked for markers written under that suffix's comment leaders, so a
markdown prompt or a YAML config carries mutable surface directly, with
no Python module in between.

Two of the three marker forms carry over; one does not:

* ``:file`` — identical. The whole file is one point.
* ``:code`` — identical. The point is the lines strictly BETWEEN the
  opening marker and its ``:end`` sentinel. This is the workhorse form
  for non-Python files, and the reason the surface stays safe without an
  AST: the region has an explicit begin AND an explicit end, both owned
  by the operator and both outside the mutable range, so a patch is
  bounded by construction rather than by a resolution heuristic.
* bare span — **not supported**, and not silently dropped. A span marker
  binds to "the nearest string literal beneath", which only means
  something to a parser. Rather than invent a line-shaped approximation
  (whose ``replace`` would eat the surrounding ``key:`` of a YAML entry),
  a bare span marker in a non-Python file is skipped with a warning that
  names the file, the line, the id, and the ``:code`` form to use
  instead.

Discovery is the **declared suffix table** rather than a content sniff. The
enumerator re-runs on every applied patch, so the walk has to stay cheap;
a suffix decides without opening the file, and it cannot wander into a
binary at all. The cost is that an operator with an unusual extension
must declare it in the contract's ``mutation_surface`` table — a visible,
epoch-rolling edit, which is the right failure mode for a surface that
decides what an LLM may rewrite.
"""

from __future__ import annotations

import ast
import hashlib
import logging
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

from zicato.core.types import MutationPoint
from zicato.mutation.markers import (
    PYTHON_SUFFIX,
    MarkerSyntax,
    active_syntax_table,
    is_end_marker,
    is_grading_marker,
    marker_syntax_for,
    parse_marker_line,
)

_log = logging.getLogger(__name__)

#: Directory names the TEXT pass refuses to descend into. Vendored and
#: generated trees hold files no operator ever marks, and the enumerator
#: re-runs after every applied patch.
#:
#: This prune is scoped to the text pass ON PURPOSE. Applying it to the
#: ``*.py`` pass would change which Python files enumerate (a marker under
#: a vendored ``.venv/`` resolves today), so the Python walk stays exactly
#: as wide as it has always been.
TEXT_SCAN_SKIP_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        ".tox",
        "site-packages",
        "dist",
        "build",
        "target",
        ".zicato",
    }
)

#: Text files larger than this are skipped by the text pass. A multi-
#: megabyte ``.jsonl`` dataset in a mutable tree is data rather than surface, and
#: reading it on every re-enumeration would dominate the apply loop.
MAX_TEXT_FILE_BYTES = 2_000_000


@dataclass(frozen=True, slots=True)
class UnboundSpanMarker:
    """A span marker that resolved to no literal, so it yields no point.

    ``reason`` is one of ``"not_a_python_file"`` (a bare span marker in a
    file with no AST to bind to) or ``"no_string_literal"`` (a Python file
    where no string literal follows the marker). Both leave a file that
    still looks annotated while contributing nothing to the surface.
    """

    id: str
    file: Path
    line: int
    reason: str


_unbound_span_markers: ContextVar[list[UnboundSpanMarker] | None] = ContextVar(
    "zicato_unbound_span_markers", default=None
)


@contextmanager
def collect_unbound_span_markers() -> Iterator[list[UnboundSpanMarker]]:
    """Collect the unbound span markers seen by walks inside this block.

    The enumerator names these defects in its log, and a log is the wrong
    place for a caller that has to make a decision about them. The list
    yielded here is filled as the walk proceeds and is complete when the
    block exits. Collection is per-context, so concurrent walks in
    different tasks do not see each other's markers.
    """
    collected: list[UnboundSpanMarker] = []
    token = _unbound_span_markers.set(collected)
    try:
        yield collected
    finally:
        _unbound_span_markers.reset(token)


def _record_unbound_span_marker(marker: UnboundSpanMarker) -> None:
    """Add ``marker`` to the active collection, if any caller asked for one."""
    collected = _unbound_span_markers.get()
    if collected is not None:
        collected.append(marker)


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


def _python_context(
    file_path: Path,
    text: str,
) -> tuple[set[int], Callable[[int], tuple[int, int] | None]] | None:
    """Return ``(literal_lines, span_resolver)`` for a Python source file.

    The Python *specialization* of the walk, and the only part of
    enumeration that needs a parser. ``literal_lines`` are the lines
    covered by a string literal — a marker written there is a docstring
    example rather than a declaration. ``span_resolver`` binds a span marker to
    the nearest literal beneath it.

    Returns ``None`` when the file does not parse, which drops it from
    enumeration. Logging that is what keeps a corrupted snapshot from
    surfacing one round later as a ``KeyError`` out of
    ``derive_generation`` instead of at its real cause.
    """

    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        _log.warning(
            "enumerator: dropping unparseable file %s (SyntaxError: %s); "
            "its mutation ids will not resolve",
            file_path,
            exc,
        )
        return None

    literal_spans = _collect_literal_spans(tree)
    literal_lines: set[int] = set()
    for line_start, line_end, _node in literal_spans:
        literal_lines.update(range(line_start, line_end + 1))
    return literal_lines, lambda line: _resolve_span_for_marker(line, literal_spans)


def _enumerate_file(
    file_path: Path, source_root: Path, syntax: MarkerSyntax
) -> list[MutationPoint]:
    """Enumerate mutation points within one file of any supported type.

    ONE pipeline: read, parse under the syntax the caller resolved from
    the file's suffix, apply the grading guard, scan for markers. Python
    is a specialization of it rather than a parallel path — it contributes
    the two things only a parser can supply (:func:`_python_context`) and
    shares everything else, so a text file is simply the case where there
    is no parser context and therefore no span form.
    """

    try:
        if not syntax.is_python and file_path.stat().st_size > MAX_TEXT_FILE_BYTES:
            # A multi-megabyte file in a mutable tree is data rather than surface,
            # and enumeration re-runs after every applied patch.
            _log.debug("enumerator: skipping %s (over %d bytes)", file_path, MAX_TEXT_FILE_BYTES)
            return []
        text = file_path.read_text(encoding="utf-8")
    except OSError:
        return []
    except UnicodeDecodeError:
        # Belt and braces behind the declared suffix: a ``.md`` that is
        # really a binary blob contributes nothing rather than raising.
        _log.debug("enumerator: skipping %s (not valid UTF-8)", file_path)
        return []

    ignored_lines: set[int] = set()
    span_resolver: Callable[[int], tuple[int, int] | None] | None = None
    if syntax.is_python:
        context = _python_context(file_path, text)
        if context is None:
            return []
        ignored_lines, span_resolver = context

    lines = text.splitlines(keepends=True)

    # Operator-grading guard (issue #19): a file declaring itself
    # operator-owned grading — predicates, judges, or the scoring
    # ``scalar_fn`` / ``drift_reducer`` plugins — via a ``zicato:grading``
    # sentinel is skipped WHOLESALE, contributing ZERO points even if it
    # also carries mutable markers. The proposer never rewrites the
    # operator's grading. Honoured only outside a string literal, mirroring
    # how mutable markers resolve.
    for line_idx, raw_line in enumerate(lines):
        if line_idx + 1 in ignored_lines:
            continue
        if is_grading_marker(raw_line.rstrip("\n").rstrip("\r"), syntax=syntax):
            return []

    return _scan_markers(
        text=text,
        lines=lines,
        file_path=file_path,
        source_root=source_root,
        syntax=syntax,
        ignored_lines=ignored_lines,
        span_resolver=span_resolver,
    )


def _scan_markers(
    *,
    text: str,
    lines: list[str],
    file_path: Path,
    source_root: Path,
    syntax: MarkerSyntax,
    ignored_lines: set[int],
    span_resolver: Callable[[int], tuple[int, int] | None] | None,
) -> list[MutationPoint]:
    """Walk ``lines`` for markers and build one point per resolved marker.

    The single marker walk, shared by the Python pass (which passes the
    file's string-literal line set as ``ignored_lines`` and an AST-backed
    ``span_resolver``) and the text pass (which passes an empty set and
    ``span_resolver=None``, because a text file has no literal to bind a
    bare span marker to).

    ``ignored_lines`` are 1-indexed source lines on which a marker is
    treated as documentation rather than as a marker — for Python, the
    lines covered by a string literal, so a docstring showing the marker
    syntax does not declare surface.

    ``span_resolver`` maps a marker's 1-indexed line to the inclusive
    ``(line_start, line_end)`` of the span it binds to, or ``None`` when
    nothing binds. Passing ``span_resolver=None`` means "this file kind
    has no span form at all", which is reported to the operator rather
    than dropped silently.
    """

    points: list[MutationPoint] = []

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
        parsed = parse_marker_line(stripped, syntax=syntax)
        if parsed is None:
            idx += 1
            continue
        if marker_line in ignored_lines:
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
                if is_end_marker(lines[scan].rstrip("\n").rstrip("\r"), syntax=syntax):
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
        if span_resolver is None:
            # A bare span marker in a file with no AST to bind to. Warn
            # rather than drop: "the nearest string literal beneath" has
            # no meaning here, and inventing a line-shaped stand-in would
            # let a ``replace`` swallow the surrounding ``key:`` of the
            # very line the operator meant to expose. Name the fix.
            _log.warning(
                "enumerator: %s:%d declares span marker id=%r, but span markers bind to a "
                "Python string literal and %s is not a Python file; use the region form "
                "(zicato:mutable:code ... zicato:mutable:end) or zicato:mutable:file instead. "
                "This marker contributes no mutation point.",
                file_path,
                marker_line,
                parsed.id,
                file_path.name,
            )
            _record_unbound_span_marker(
                UnboundSpanMarker(
                    id=parsed.id,
                    file=file_path,
                    line=marker_line,
                    reason="not_a_python_file",
                )
            )
            idx += 1
            continue
        span = span_resolver(marker_line)
        if span is None:
            # A span marker in a Python file with no string literal beneath
            # it. Silent in the log by design — an operator mid-edit hits
            # this constantly — but recorded structurally so a caller that
            # must decide about it does not have to parse prose.
            _record_unbound_span_marker(
                UnboundSpanMarker(
                    id=parsed.id,
                    file=file_path,
                    line=marker_line,
                    reason="no_string_literal",
                )
            )
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


def is_text_mutation_candidate(
    path: Path, *, table: Mapping[str, MarkerSyntax] | None = None
) -> bool:
    """Return ``True`` iff the text pass should walk ``path`` for markers.

    A pure predicate over the path — it never opens the file, which is the
    whole point of deciding by suffix. ``.py`` is excluded because the
    Python walk owns it. ``table`` defaults to the process's active syntax
    table.
    """

    return path.suffix != PYTHON_SUFFIX and marker_syntax_for(path, table=table) is not None


def _text_pass_files(
    root: Path, table: Mapping[str, MarkerSyntax]
) -> list[tuple[Path, MarkerSyntax]]:
    """Return the text-pass candidates under ``root`` with their syntax, sorted.

    Prunes :data:`TEXT_SCAN_SKIP_DIRS` anywhere in the relative path. The
    prune is text-pass-only — see that constant's note.
    """

    out: list[tuple[Path, MarkerSyntax]] = []
    for path in sorted(root.rglob("*")):
        syntax = marker_syntax_for(path, table=table)
        if syntax is None or syntax.is_python:
            continue
        try:
            rel_parts = path.relative_to(root).parts[:-1]
        except ValueError:  # pragma: no cover — rglob results are under root
            rel_parts = ()
        if any(part in TEXT_SCAN_SKIP_DIRS for part in rel_parts):
            continue
        if not path.is_file():
            continue
        out.append((path, syntax))
    return out


def enumerate_mutations(source_roots: list[Path]) -> list[MutationPoint]:
    """Walk source roots and return every mutation point we can find.

    Three discovery passes run for each root:

    * The Python marker pass — walks ``*.py`` files for
      ``# zicato:mutable`` comment markers and binds span markers to the
      nearest string literal (the historical behaviour, unchanged).
    * The text marker pass — walks every other file whose suffix the
      contract's syntax table declares, for the same markers written
      under that file type's comment leaders. ``:file`` and
      ``:code`` resolve; a bare span marker warns, because it has no
      literal to bind to. See this module's docstring.
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
        Source-root directories to walk. A root may also be a single
        file. Each is normalised via :meth:`Path.resolve` so the returned
        mutation points carry absolute paths.
    """

    table = active_syntax_table()
    python_syntax = table[PYTHON_SUFFIX]
    out: list[MutationPoint] = []
    for raw_root in source_roots:
        root = Path(raw_root).resolve()
        if not root.exists():
            continue
        if root.is_file():
            # Single-file root. Before non-Python files were walkable, a
            # root whose suffix was not ``.py`` fell through to the
            # directory branch, whose ``rglob`` on a file yields nothing —
            # so it enumerated to zero points in total silence. Now the
            # only unhandled case is a suffix the text pass does not
            # recognise, and that is worth saying out loud: it is
            # invariably an operator who expected the file to be surface.
            root_syntax = marker_syntax_for(root, table=table)
            if root_syntax is not None:
                out.extend(_enumerate_file(root, root.parent, root_syntax))
            else:
                _log.warning(
                    "enumerator: source root %s is a file whose suffix is not walkable "
                    "(neither .py nor a suffix the mutation_surface table declares); it "
                    "contributes no mutation points",
                    root,
                )
            continue
        for py_file in sorted(root.rglob("*.py")):
            out.extend(_enumerate_file(py_file.resolve(), root, python_syntax))
        for text_file, syntax in _text_pass_files(root, table):
            out.extend(_enumerate_file(text_file.resolve(), root, syntax))

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


__all__ = [
    "MAX_TEXT_FILE_BYTES",
    "TEXT_SCAN_SKIP_DIRS",
    "UnboundSpanMarker",
    "collect_unbound_span_markers",
    "enumerate_mutations",
    "is_text_mutation_candidate",
]
