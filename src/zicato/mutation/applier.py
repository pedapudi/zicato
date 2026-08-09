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

One condition, one exception type
---------------------------------

"This patch set cannot be applied" is a single logical condition, and on
the checked :func:`apply_patches` surface it always raises ``ValueError``:
the pre-check, the apply-time missing-anchor / vanished-marker sites (an
earlier patch in the batch can erase a later patch's anchor — the
re-enumeration then drops it) and the post-apply syntax gate. Signalling
it through two types across the generation-level transaction boundary in
:mod:`zicato.evolve.round` is what turned a rejectable candidate into an
aborted run in issue #83. :func:`apply_patches_unchecked` keeps the legacy
``KeyError`` for that case as the INTERNAL unchecked contract.
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
from zicato.mutation.markers import (
    MarkerSyntax,
    is_end_marker,
    marker_syntax_for,
    parse_marker_line,
)
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

    The line is parsed under the file's own comment grammar
    (:func:`~zicato.mutation.markers.marker_syntax_for`), so a marker in a
    markdown or YAML file resolves the same way a ``#``-led Python marker
    does.
    """

    syntax = marker_syntax_for(file_path)
    text = file_path.read_text(encoding="utf-8")
    for idx, line in enumerate(text.splitlines()):
        parsed = parse_marker_line(line, syntax=syntax)
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


def _resolve_string_literal_node(
    file_path: Path,
    marker_line: int,
) -> ast.Constant | None:
    """Find the exact string-literal node a span marker binds to.

    Mirrors the enumerator's resolution rule
    (:func:`zicato.mutation.enumerator._resolve_span_for_marker`): the
    span marker binds to the string literal whose source line is the
    *smallest* line strictly greater than ``marker_line``. The enumerator
    works in whole lines and so loses the literal's COLUMN offsets; this
    helper re-parses the file and returns the AST node itself so the
    applier can rewrite exactly the literal's character span — preserving
    everything else on the line (the ``NAME =`` assignment target, a
    ``kwarg=`` name, the enclosing parens, a leading ``+`` concat operator,
    a trailing comma). Replacing only the node is what keeps a
    simple-assignment span (``NAME = "..."``) from losing its target when
    the replacement is multi-line.

    Returns ``None`` when the file does not parse or carries no string
    literal after the marker — the caller then falls back to the
    line-based path (which still produces a valid edit for the
    whole-file / non-Python cases).
    """

    text = file_path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    best: ast.Constant | None = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if node.lineno <= marker_line:
            continue
        # Smallest line strictly greater than the marker, breaking ties on
        # column so a single line carrying two literals resolves the
        # left-most — identical to the enumerator's ``line_start`` ordering.
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


def _is_python_string_literal_source(text: str) -> bool:
    """Confirm ``text`` is *actually* well-formed Python string-literal source.

    :func:`_looks_like_python_string_literal` is a cheap first-character
    heuristic — it returns ``True`` for any prose that merely *starts* with
    a quote (``"leading quote, never closed``). Taking the verbatim-preserve
    branch on that alone writes the prose out unchanged and corrupts the
    file. This is the real gate: the (dedented, parenthesised) content must
    parse in ``eval`` mode to a single string expression — either a plain
    string constant (:class:`ast.Constant` whose value is ``str``) or an
    f-string (:class:`ast.JoinedStr`). Parenthesising lets implicitly
    concatenated / multi-line literals (the ``+``-joined and adjacent-literal
    span shapes) parse as one expression. Anything else (an assignment echo
    like ``ROSTER = "..."``, a bare unterminated literal, arbitrary prose)
    fails here and the caller falls through to the safe wrap path.
    """
    stripped = textwrap.dedent(text).strip()
    if not stripped:
        return False
    try:
        tree = ast.parse(f"({stripped})", mode="eval")
    except SyntaxError:
        return False
    node = tree.body
    if isinstance(node, ast.JoinedStr):
        return True
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _quote_as_python_string(content: str, indent: str) -> str:
    """Wrap ``content`` as a Python string literal whose VALUE is ``content``.

    The indent prefix is the leading whitespace of the original span's
    first line so the produced source slots into the original
    syntactic position (kwarg value, return expression, parenthesised
    concat, etc.) without disturbing the surrounding code's indent.

    The wrap is **value-preserving**: the literal the applier writes must
    evaluate back to exactly ``content``. Two hazards make a naive
    ``\"\"\"{content}\"\"\"`` wrong:

    * **Delimiter fusing** — a leading/trailing quote, or an embedded
      triple-quote run, fuses with the delimiter and produces an
      unterminated-string ``SyntaxError`` (issue #11). A triple delimiter
      is only safe when its triple sequence is absent from ``content`` and
      ``content`` neither starts nor ends with that quote char.
    * **Escape interpretation** — a backslash in ``content`` (``\\t`` in a
      Windows path, ``\\d`` in an embedded regex) would be read as an
      *escape sequence* inside a non-raw literal, silently changing the
      string's value and emitting a ``SyntaxWarning`` for unknown escapes
      (the ``\\d`` warning issue #38 observed). A non-raw triple-quote
      therefore only preserves the value when ``content`` has no backslash.

    So a triple-quote wrap is used only when ``content`` is both
    fuse-free AND backslash-free. Otherwise we fall back to ``repr()``,
    which is always a valid single-line literal that evaluates back to the
    exact ``content`` regardless of quotes, backslashes, or newlines —
    correctness over the triple-quote's readability.
    """
    has_backslash = "\\" in content
    if not has_backslash:
        for triple in ('"""', "'''"):
            q = triple[0]
            if triple not in content and not content.startswith(q) and not content.endswith(q):
                return f"{indent}{triple}{content}{triple}"
    return f"{indent}{repr(content)}"


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


def _strip_literal_first_line_indent(literal: str) -> str:
    """Drop the leading whitespace from a literal's first line only.

    Column-precise span surgery slots the replacement literal in right
    after the assignment target / kwarg name (``NAME = `` already sits in
    the ``before`` slice), so the literal's first line MUST NOT carry its
    own indent — the surrounding source already positions it. Continuation
    lines keep their relative indentation verbatim (a deliberately indented
    docstring body is preserved). Idempotent on a literal whose first line
    already has no leading whitespace.
    """

    raw_lines = literal.splitlines(keepends=True)
    if not raw_lines:
        return literal
    out = [raw_lines[0].lstrip(" \t")]
    out.extend(raw_lines[1:])
    return "".join(out)


def _build_literal_replacement(new_content: str) -> str:
    """Produce valid Python string-literal source for a ``.py`` span replace.

    The returned text is a bare literal (no leading indent — the caller
    slots it in column-precise, right after the surrounding ``NAME =`` /
    ``kwarg=`` / ``(`` prefix that already occupies the line). Two paths:

    * **Preserve** ``new_content`` verbatim when BOTH the cheap
      first-character heuristic AND a real parse confirm it is well-formed
      string-literal source (a plain string, an f-string, an
      implicitly-concatenated multi-line literal). The first line's own
      indent is stripped so the literal anchors to its syntactic column;
      continuation lines are left untouched.
    * **Wrap** ``new_content`` as a collision-proof triple-quoted literal
      otherwise (raw prose, an assignment echo, a stray-quote payload).
      The wrap is guaranteed to parse — see :func:`_quote_as_python_string`.

    Either way the result is a single Python expression that can replace a
    string-literal node's exact character span without breaking the line.
    """

    if _looks_like_python_string_literal(new_content) and _is_python_string_literal_source(
        new_content
    ):
        return _strip_literal_first_line_indent(new_content)
    return _quote_as_python_string(new_content, "")


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


def _reindent_code_region(
    new_content: str,
    indent: str,
    *,
    syntax: MarkerSyntax = "python",
) -> str:
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

    ``syntax`` selects the comment grammar the marker-stripping step
    recognises, so a proposer that echoes a markdown region's
    ``<!-- zicato:mutable:end -->`` back inside its replacement has that
    line dropped exactly as a Python ``# zicato:mutable:end`` would be.
    Step 1 is the enforcement point for "the marker lines are OUTSIDE the
    mutable region": the region body can never contain a marker, so the
    proposer cannot delete, move, or duplicate its own anchors. Step 3's
    dedent-then-re-anchor preserves RELATIVE indentation, which is what
    keeps a nested YAML/JSON region structurally intact and not flattened.
    """
    raw_lines = new_content.splitlines()
    # Drop any marker lines the proposer echoed back — the body is the
    # region's interior only; the markers are owned by the surrounding
    # file and re-emitted by the applier.
    kept = [
        line
        for line in raw_lines
        if parse_marker_line(line, syntax=syntax) is None and not is_end_marker(line, syntax=syntax)
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
    For code-kind points (a ``zicato:mutable:code`` region) the body
    lines between the markers are replaced verbatim — the region is the
    file's own content, so ``new_content`` is fully-formed text for that
    file's format and the applier must not wrap it as a string literal.
    In a ``.py`` file that content is real Python control flow, and the
    proposer is responsible for producing a block that parses in place
    (the post-apply syntax check is the backstop); in a markdown prompt or
    a YAML config it is prose / config lines, for which there is no cheap
    dependency-free notion of "still valid" and so no gate. Either way the
    ``:code`` / ``:end`` marker lines sit OUTSIDE the replaced range, so
    the rewrite is bounded by the operator's own anchors and the mutation
    id keeps resolving afterwards.
    For span-kind points the policy depends on the file's syntax:

    * For ``.py`` files, the span is a Python string-literal expression.
      The applier replaces *exactly* the literal node's character span —
      not the whole line — so the surrounding ``NAME =`` assignment
      target, ``kwarg=`` name, enclosing parens, leading ``+`` concat
      operator and trailing comma all survive verbatim (issue #38: a
      whole-line replace of a simple ``NAME = "..."`` assignment dropped
      the ``NAME =`` target whenever the replacement was multi-line). The
      replacement literal is either ``new_content`` preserved verbatim
      (when it already parses as string-literal source) or wrapped as a
      collision-proof triple-quoted literal. The wrap step is what makes
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

    if point.kind == "span" and point.file.suffix == ".py":
        # Column-precise span surgery. The enumerator binds the marker to
        # a string-literal AST node but reports only WHOLE LINES, so a
        # naive line-replace of a simple ``NAME = "..."`` assignment would
        # overwrite the ``NAME =`` target along with the literal (issue
        # #38). Re-resolve the exact node and replace only its character
        # span, leaving the rest of the line (assignment target, kwarg
        # name, parens, ``+`` operator, trailing comma) untouched.
        marker_line = _resolve_marker_line(point.file, point.id)
        node = (
            _resolve_string_literal_node(point.file, marker_line)
            if marker_line is not None
            else None
        )
        if node is not None:
            literal = _build_literal_replacement(new_content)
            # Multi-line literals (a ``\"\"\"...\"\"\"`` docstring) keep
            # their continuation lines verbatim; the node already sits at
            # its syntactic column, so no first-line indent is added.
            _replace_node_text(point.file, node, literal)
            return
        # The node could not be re-resolved (an unparseable file, or a
        # marker with no literal beneath it). Fall through to the
        # line-based path below, which still produces a deterministic edit.

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
        middle = _reindent_code_region(new_content, indent, syntax=marker_syntax_for(point.file))
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
        # Preserve verbatim only when BOTH the cheap first-char heuristic
        # AND a real parse confirm the content is well-formed string-literal
        # source. The heuristic alone returns True for prose that merely
        # starts with a quote; preserving that verbatim writes an
        # unterminated literal. When the parse gate fails we fall through to
        # the (collision-proof) wrap path, which always yields valid source.
        if _looks_like_python_string_literal(new_content) and _is_python_string_literal_source(
            new_content
        ):
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
    4. Otherwise apply every patch in order against the copied tree.

    Because the whole batch is validated before the first edit lands, the
    deterministic guarantee holds: an edit can only ever land at a valid,
    enumerated ``# zicato:mutable`` point, and a batch with one bad patch
    applies none.

    The pre-check cannot catch every bad patch set, and it is not meant
    to. The sequential apply re-enumerates between patches, so an EARLIER
    patch can erase the anchor a LATER patch resolved against at
    pre-check time (a file-kind ``whole`` replace that drops the markers
    below it, say). That is still a bad patch set, and on THIS — the
    checked, production — surface it raises ``ValueError`` like every
    other apply-time rejection, with the copied tree removed. One
    logical condition ("this patch set cannot be applied") therefore
    reaches callers as exactly ONE exception type, which is what lets the
    generation-level transaction boundary in :mod:`zicato.evolve.round`
    degrade it to a rejected challenger instead of aborting the run
    (issue #83).

    Callers that genuinely need the legacy best-effort-sequential
    behaviour (no atomic pre-check, missing anchors as ``KeyError``) can
    call :func:`apply_patches_unchecked` directly.

    Raises
    ------
    FileExistsError
        When ``target_root`` already exists.
    ValueError
        When the patch set fails :func:`validate_patches` — the error
        message enumerates every problem found; when a patch's anchor no
        longer resolves at apply time (erased by an earlier patch in the
        same batch); or when the post-apply syntax gate finds a touched
        ``.py`` file unparseable. The copied tree is removed before the
        exception propagates in every case.
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

    # Apply sequentially. Missing-anchor / vanished-marker sites raise
    # ``ValueError`` here (not the internal ``KeyError``) so the checked
    # surface signals every bad-patch-set condition with ONE type; remove
    # the copied tree so a mid-batch rejection leaves nothing half-applied.
    try:
        _apply_patches_into_tree(target_root, patches, missing_anchor_error=ValueError)
    except ValueError:
        shutil.rmtree(target_root, ignore_errors=True)
        raise

    # Post-apply syntax gate: attribute corruption to the round that
    # PRODUCED it. A malformed proposer patch must degrade into a rejected
    # challenger here, not silently write an unparseable snapshot that
    # crashes the *next* generation's enumeration one round later. We
    # re-parse every ``.py`` file the batch touched; if any no longer
    # parses, remove the copied tree (keeping lineage append-only) and
    # raise so the caller records a single rejection.
    syntax_problems = _post_apply_syntax_problems(target_root, patches)
    if syntax_problems:
        shutil.rmtree(target_root, ignore_errors=True)
        raise ValueError(
            "apply_patches: refusing to promote snapshot; "
            f"{len(syntax_problems)} post-apply syntax problem(s): " + "; ".join(syntax_problems)
        )


def _touched_py_files(target_root: Path, patches: list[Patch]) -> set[Path]:
    """Return the ``.py`` files under ``target_root`` the batch touched.

    Resolved by re-enumerating the applied tree and mapping each patched
    ``mutation_id`` back to its file. A corrupted file enumerates to zero
    points (the enumerator drops it on ``SyntaxError``), so an id that no
    longer resolves is itself a signal the file broke — the post-apply
    parse pass below catches it from the other direction by walking the
    whole tree, but we keep this targeted set for a precise error message.
    """

    points = enumerate_mutations([target_root])
    by_id = {p.id: p for p in points}
    touched: set[Path] = set()
    for patch in patches:
        point = by_id.get(patch.mutation_id)
        if point is not None and point.file.suffix == ".py":
            touched.add(point.file.resolve())
    return touched


def _post_apply_syntax_problems(target_root: Path, patches: list[Patch]) -> list[str]:
    """Return one problem string per ``.py`` file the batch left unparseable.

    Walks every ``.py`` file under ``target_root`` and re-parses it. Walking
    the whole tree (not only the files we can map a still-resolving id back
    to) is deliberate: a span replace that corrupted a file makes that
    file's ids vanish from the enumeration, so a mapping-only check would
    miss exactly the failure this guard exists to catch.
    """

    target_root = Path(target_root).resolve()
    touched = _touched_py_files(target_root, patches)
    problems: list[str] = []
    for py_file in sorted(target_root.rglob("*.py")):
        try:
            text = py_file.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            ast.parse(text)
        except SyntaxError as exc:
            tag = " (touched)" if py_file.resolve() in touched else ""
            problems.append(f"{py_file}{tag}: {exc}")
    return problems


def apply_patches_unchecked(
    source_root: Path,
    patches: list[Patch],
    target_root: Path,
) -> None:
    """Materialise a child snapshot and apply ``patches`` best-effort.

    This is the **legacy, non-atomic, INTERNAL unchecked** apply path,
    preserved for callers that have already pre-validated the patch set
    themselves (e.g. via
    :func:`~zicato.mutation.validator.validate_patches`). Prefer
    :func:`apply_patches`, which validates the batch and is all-or-nothing.

    Its ``KeyError``-on-missing-anchor contract is deliberate and is NOT
    the production signal: an unresolvable anchor here is a caller
    contract breach (the caller promised it pre-validated), so it surfaces
    as a distinct type from the ``ValueError`` "rejectable patch set"
    class. The checked surface — :func:`apply_patches` — CONVERTS every
    such site to ``ValueError`` (issue #83), so nothing on the evolve
    path ever sees a ``KeyError`` from the applier. Do not "unify" this
    function's contract with the checked one; the two pins in
    ``tests/test_mutation_applier.py`` hold it.

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


def _apply_patches_into_tree(
    target_root: Path,
    patches: list[Patch],
    *,
    missing_anchor_error: type[Exception] = KeyError,
) -> None:
    """Apply ``patches`` in order against an already-materialised tree.

    The shared, best-effort-sequential core of :func:`apply_patches` and
    :func:`apply_patches_unchecked`. ``target_root`` must already exist
    (the callers handle the copy + their respective pre-checks).

    ``missing_anchor_error`` is the exception type raised when a patch's
    ``mutation_id`` no longer resolves in the (re-)enumerated tree, or when
    its marker line has vanished. It exists because the two callers have
    deliberately different contracts for that ONE condition:
    :func:`apply_patches` passes ``ValueError`` (a rejectable patch set on
    the checked, production path — issue #83) while
    :func:`apply_patches_unchecked` keeps the legacy ``KeyError``.
    Payload/op mismatches are ``ValueError`` on both paths.
    """

    points = enumerate_mutations([target_root])
    index = _build_index(points)

    for patch in patches:
        if patch.op == "replace":
            if patch.new_content is None:
                raise ValueError(f"Patch {patch.id!r}: op=replace requires new_content")
            point = index.get(patch.mutation_id)
            if point is None:
                raise missing_anchor_error(
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
                raise missing_anchor_error(
                    f"Patch {patch.id!r}: mutation_id {patch.mutation_id!r} not found "
                    f"in target_root {target_root}"
                )
            marker_line = _resolve_marker_line(point.file, patch.mutation_id)
            if marker_line is None:
                raise missing_anchor_error(
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
                raise missing_anchor_error(
                    f"Patch {patch.id!r}: mutation_id {patch.mutation_id!r} not found "
                    f"in target_root {target_root}"
                )
            marker_line = _resolve_marker_line(point.file, patch.mutation_id)
            if marker_line is None:
                raise missing_anchor_error(
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
