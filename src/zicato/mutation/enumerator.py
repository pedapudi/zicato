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

Non-Python files
----------------

Markers are not a Python-only surface. Any *text* file in a mutable tree
whose suffix is in :data:`TEXT_FILE_SUFFIXES` (or whose name is in
:data:`TEXT_FILE_NAMES`) is walked for markers written under the
``"text"`` comment grammar — see :mod:`zicato.mutation.markers`. A
markdown prompt, a YAML config, a shell script, a JSON fixture can each
carry mutable surface directly, with no Python module in between.

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

Discovery is an **extension allowlist**, not a content sniff. The
enumerator re-runs on every applied patch, so the walk has to stay cheap;
an allowlist decides without opening the file, and it cannot wander into
a binary at all. The cost is that an operator with an unusual extension
must add it to :data:`TEXT_FILE_SUFFIXES` — a visible, reviewable edit,
which is the right failure mode for a surface that decides what an LLM
may rewrite.
"""

from __future__ import annotations

import ast
import hashlib
import logging
from collections.abc import Callable
from pathlib import Path

from zicato.core.types import MutationPoint
from zicato.mutation.markers import (
    MarkerSyntax,
    is_end_marker,
    is_grading_marker,
    parse_marker_line,
)

_log = logging.getLogger(__name__)

#: Suffixes the native marker pass walks in addition to ``*.py``. Curated
#: rather than exhaustive: every entry is a plain-text format with a
#: conventional line- or block-comment syntax one of the
#: :data:`~zicato.mutation.markers.TEXT_COMMENT_LEADERS` covers. Formats
#: with no comment syntax at all (``.csv``) are excluded — a marker could
#: not be written in them without corrupting the data.
TEXT_FILE_SUFFIXES: frozenset[str] = frozenset(
    {
        # Prose / prompts
        ".md",
        ".markdown",
        ".rst",
        ".txt",
        # Config / data
        ".yaml",
        ".yml",
        ".toml",
        # Strict ``.json`` / ``.jsonl`` are ABSENT and must stay absent:
        # JSON has no comment syntax, so any marker line an operator could
        # write would itself invalidate the document. The comment-bearing
        # dialects do work.
        ".jsonc",
        ".json5",
        ".ini",
        ".cfg",
        ".conf",
        ".properties",
        ".env",
        # Templates
        ".j2",
        ".jinja",
        ".jinja2",
        ".tmpl",
        ".mustache",
        ".handlebars",
        # Shell / infra
        ".sh",
        ".bash",
        ".zsh",
        ".fish",
        ".tf",
        ".tfvars",
        # Markup / web
        ".html",
        ".htm",
        ".xml",
        ".svg",
        ".css",
        ".scss",
        ".less",
        # Other languages an inner harness may carry
        ".js",
        ".mjs",
        ".cjs",
        ".jsx",
        ".ts",
        ".tsx",
        ".go",
        ".rs",
        ".java",
        ".kt",
        ".rb",
        ".pl",
        ".lua",
        ".c",
        ".h",
        ".cpp",
        ".hpp",
        ".sql",
        ".proto",
        ".graphql",
    }
)

#: Extension-less file NAMES the text pass walks. Deliberately tiny — the
#: allowlist exists so the walk never has to guess, and every entry here
#: is a name with exactly one conventional meaning.
TEXT_FILE_NAMES: frozenset[str] = frozenset({"Dockerfile", "Makefile"})

#: Directory names the TEXT pass refuses to descend into. Vendored and
#: generated trees hold thousands of ``.json`` files that no operator ever
#: marks, and the enumerator re-runs after every applied patch.
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
#: megabyte ``.jsonl`` dataset in a mutable tree is data, not surface, and
#: reading it on every re-enumeration would dominate the apply loop.
MAX_TEXT_FILE_BYTES = 2_000_000


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

    # Lines that fall INSIDE a string literal don't count as marker
    # comments — they're documentation / examples written by the
    # operator. A marker comment must be a real ``#`` comment on a code
    # line, not text that happens to start with ``#`` inside a docstring.
    literal_line_set: set[int] = set()
    for line_start, line_end, _node in literal_spans:
        for line_no in range(line_start, line_end + 1):
            literal_line_set.add(line_no)

    # Operator-grading guard (issue #19 phase 3): a file declaring itself
    # operator-owned grading code — predicates, judges, or the scoring
    # ``scalar_fn`` / ``drift_reducer`` plugins — via a ``# zicato:grading``
    # sentinel is skipped WHOLESALE. The proposer never gets to rewrite the
    # operator's grading, so such a file contributes ZERO mutation points even
    # if it also carries ``# zicato:mutable`` markers. The sentinel is honoured
    # only on a real code line (not inside a docstring example), mirroring how
    # mutable markers are resolved.
    for line_idx, raw_line in enumerate(lines):
        if line_idx + 1 in literal_line_set:
            continue
        if is_grading_marker(raw_line.rstrip("\n").rstrip("\r")):
            return []

    return _scan_markers(
        text=text,
        lines=lines,
        file_path=file_path,
        source_root=source_root,
        syntax="python",
        ignored_lines=literal_line_set,
        span_resolver=lambda marker_line: _resolve_span_for_marker(marker_line, literal_spans),
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
            idx += 1
            continue
        span = span_resolver(marker_line)
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


def is_text_mutation_candidate(path: Path) -> bool:
    """Return ``True`` iff the text pass should walk ``path`` for markers.

    Pure predicate over the path — it never opens the file. ``.py`` is
    excluded because the Python pass owns it.
    """

    if path.suffix == ".py":
        return False
    if path.suffix:
        return path.suffix in TEXT_FILE_SUFFIXES
    return path.name in TEXT_FILE_NAMES


def _enumerate_text_file(file_path: Path, source_root: Path) -> list[MutationPoint]:
    """Enumerate mutation points within a single non-Python text file.

    Mirrors :func:`_enumerate_file` minus everything that needs a parser:
    no AST, so no string-literal exclusion set and no span binding. The
    ``:file`` and ``:code`` forms behave identically to their Python
    counterparts, and the ``# zicato:grading`` wholesale-skip sentinel is
    honoured the same way.

    Returns ``[]`` for a file that is too large, unreadable, not valid
    UTF-8, or that contains a NUL byte. The last two are a belt-and-braces
    guard behind the suffix allowlist: a ``.json`` that is actually a
    binary blob should contribute nothing rather than raise.
    """

    try:
        if file_path.stat().st_size > MAX_TEXT_FILE_BYTES:
            _log.debug(
                "enumerator: skipping %s for the text pass (larger than %d bytes)",
                file_path,
                MAX_TEXT_FILE_BYTES,
            )
            return []
        text = file_path.read_text(encoding="utf-8")
    except OSError:
        return []
    except UnicodeDecodeError:
        _log.debug("enumerator: skipping %s for the text pass (not valid UTF-8)", file_path)
        return []
    if "\x00" in text:
        _log.debug("enumerator: skipping %s for the text pass (contains NUL bytes)", file_path)
        return []

    lines = text.splitlines(keepends=True)

    # Operator-grading guard, same contract as the Python pass: a file
    # declaring itself operator-owned grading contributes ZERO points. A
    # text file has no docstrings, so there is no example-in-a-literal
    # exemption to apply — every line counts.
    for raw_line in lines:
        if is_grading_marker(raw_line.rstrip("\n").rstrip("\r"), syntax="text"):
            return []

    return _scan_markers(
        text=text,
        lines=lines,
        file_path=file_path,
        source_root=source_root,
        syntax="text",
        ignored_lines=set(),
        span_resolver=None,
    )


def _text_pass_files(root: Path) -> list[Path]:
    """Return the text-pass candidates under ``root``, in sorted order.

    Prunes :data:`TEXT_SCAN_SKIP_DIRS` anywhere in the relative path. The
    prune is text-pass-only — see that constant's note.
    """

    out: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not is_text_mutation_candidate(path):
            continue
        try:
            rel_parts = path.relative_to(root).parts[:-1]
        except ValueError:  # pragma: no cover — rglob results are under root
            rel_parts = ()
        if any(part in TEXT_SCAN_SKIP_DIRS for part in rel_parts):
            continue
        if not path.is_file():
            continue
        out.append(path)
    return out


def enumerate_mutations(source_roots: list[Path]) -> list[MutationPoint]:
    """Walk source roots and return every mutation point we can find.

    Three discovery passes run for each root:

    * The Python marker pass — walks ``*.py`` files for
      ``# zicato:mutable`` comment markers and binds span markers to the
      nearest string literal (the historical behaviour, unchanged).
    * The text marker pass — walks every other allowlisted text file
      (:data:`TEXT_FILE_SUFFIXES` / :data:`TEXT_FILE_NAMES`) for the same
      markers written under any conventional comment leader. ``:file`` and
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
            if root.suffix == ".py":
                out.extend(_enumerate_file(root, root.parent))
            elif is_text_mutation_candidate(root):
                out.extend(_enumerate_text_file(root, root.parent))
            else:
                _log.warning(
                    "enumerator: source root %s is a file whose suffix is not walkable "
                    "(neither .py nor an allowlisted text suffix); it contributes no "
                    "mutation points",
                    root,
                )
            continue
        for py_file in sorted(root.rglob("*.py")):
            out.extend(_enumerate_file(py_file.resolve(), root))
        for text_file in _text_pass_files(root):
            out.extend(_enumerate_text_file(text_file.resolve(), root))

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
    "TEXT_FILE_NAMES",
    "TEXT_FILE_SUFFIXES",
    "TEXT_SCAN_SKIP_DIRS",
    "enumerate_mutations",
    "is_text_mutation_candidate",
]
