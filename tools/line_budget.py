"""Report and enforce the repository's simplification budgets.

Three measurements are taken over the tracked files: ``total`` newline counts,
the ``production`` subset of them, and the ``production_logic`` lines that
execute. Each is enforced against a limit in ``.line-budget.json`` and each is
also reported per subsystem, where a subsystem's three numbers partition the
repository-wide ones, so the per-subsystem logic column sums to the enforced
logic count. A subsystem's prose share is the share of its production lines
that do not execute: ``1 - production_logic / production``.

The logic counters reach Python, JavaScript, and Rust. CSS and HTML hold no
counter, so every line of a CSS or HTML file counts as executable, in the
per-subsystem view as in the enforced measurement: the console's stylesheet is
measured by its newline count in both. Extending a counter to a language moves
the enforced measurement, so it is a change to the measurement contract in
``docs/design/LINE-BUDGET.md`` rather than to one view of it.

``--history`` walks the first-parent chain of a ref and reports the
production-logic series per subsystem. Its cache is content-addressed: a blob's
counts are keyed by the blob id and the file suffix, a commit's tallies by its
id, and the whole cache by a digest of this module's source, so a change to a
counter discards it.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import re
import subprocess
import sys
import tokenize
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Iterator
from dataclasses import asdict, astuple, dataclass
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / ".line-budget.json"
LEDGER_PATH = "docs/design/LINE-BUDGET.md"
LEDGER = ROOT / LEDGER_PATH
LEDGER_HEADING = "## Deliberate increases"
SUBSYSTEM_HEADING = "## Production logic by subsystem"
SUBSYSTEM_TABLE_HEADER = "| Subsystem | Total | Production | Production logic | Prose share |"
HISTORY_CACHE = ROOT / ".cache" / "line_budget_history.json"
MEASUREMENTS = ("total", "production", "production_logic")
# The summary table's row labels, in the order the table lists them, against the
# measurement each names in the config.
SUMMARY_LABELS = (
    ("Total", "total"),
    ("Production", "production"),
    ("Production logic", "production_logic"),
)
# A summary row: the label, the baseline, the enforced limit, and their signed
# difference.
SUMMARY_ROW = re.compile(
    r"^\| (?P<label>Total|Production|Production logic) \| (?P<baseline>[\d,]+) \| "
    r"(?P<limit>[\d,]+) \| (?P<difference>[+-][\d,]+) \|$",
    re.MULTILINE,
)
# A ledger row's first cell: the change's name, then the measurement it moves.
LEDGER_LABEL = re.compile(r"(?P<label>.+) \((?P<measurement>total|production|production logic)\)")
LOCKFILES = {"Cargo.lock", "uv.lock", "package-lock.json", "npm-shrinkwrap.json"}
# The paths the budget does not count at all, and the reason each one holds no
# implementation that simplifying the repository could reach. Everything else
# tracked by git counts, so a file lands here only on one of these grounds.
EXCLUDED_FROM_BUDGET = (
    # Screenshots kept as the record of a console review. They are binary, so
    # the newline count of one is an artifact of how the image compressed.
    "artifacts/visual-inspection/",
    # Rebuilt from the slide sources by docs/presentation/build.py: the deck
    # viewer with the slides re-inlined, the printed deck, and the contact
    # sheet. Editing one is undone by the next build.
    "docs/presentation/index.html",
    "docs/presentation/zicato-deck.pdf",
    "docs/presentation/contact-sheet.png",
    # The deck's hand-drawn source art. Its SVG path data measures how much is
    # drawn on a slide, which no simplification of the repository changes.
    "docs/presentation/slides/",
    # Captured payloads that tests replay as recorded evidence of what a
    # producer emitted: the reader-parity snapshot, the trace-view fixtures,
    # the endpoint-route recording with the label-to-URL probe map that keys
    # it, and the served elimination folds over the round lists the browser
    # suite declares. Shortening one to save lines would destroy the record.
    "src/zicato/dashboard/static/test/fixtures/trace_view/",
    "tests/data/reader_parity_snapshot.json",
    "tests/data/endpoint_route_snapshot.json",
    "tests/data/endpoint_route_probes.json",
    "tests/data/elim_states_cases.json",
    "tests/data/elim_states_served.json",
)
ASSET_SUFFIXES = {".ico", ".pdf", ".png", ".svg", ".woff2"}
DOCSTRING_HOLDERS = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
# Opens a Rust raw string: an optional byte marker, the raw marker, and the
# hashes whose count the matching close repeats.
RUST_RAW_STRING = re.compile(r'b?r(#*)"')


@dataclass(frozen=True)
class Lines:
    """The three measurements over one subsystem's files."""

    total: int
    production: int
    production_logic: int

    @property
    def prose_share(self) -> float | None:
        """The share of production lines that do not execute; None with no production."""
        if not self.production:
            return None
        return 1 - self.production_logic / self.production


@dataclass(frozen=True)
class Report:
    files: int
    lines: int
    production_files: int
    production_lines: int
    production_logic_lines: int
    languages: dict[str, int]
    subsystems: dict[str, Lines]


def _git(*args: str, cwd: Path = ROOT) -> bytes:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True).stdout


def _paths(ref: str | None, cwd: Path) -> list[str]:
    command = ("ls-tree", "-r", "--name-only", ref) if ref else ("ls-files",)
    return _git(*command, cwd=cwd).decode().splitlines()


def _content(path: str, ref: str | None, cwd: Path) -> bytes:
    return _git("show", f"{ref}:{path}", cwd=cwd) if ref else (cwd / path).read_bytes()


def _tree(ref: str, cwd: Path) -> Iterator[tuple[str, str]]:
    """Yield each tracked file at a ref as its blob id and its path."""
    for entry in _git("ls-tree", "-r", "-z", ref, cwd=cwd).decode().split("\0"):
        if entry:
            meta, path = entry.split("\t", 1)
            yield meta.split()[2], path


def _excluded(path: str) -> bool:
    name = PurePosixPath(path).name
    return (
        PurePosixPath(path).suffix.lower() in {".md", ".markdown"}
        or name in LOCKFILES
        or any(path == item or path.startswith(item) for item in EXCLUDED_FROM_BUDGET)
    )


def _production(path: str) -> bool:
    suffix = PurePosixPath(path).suffix.lower()
    if suffix in ASSET_SUFFIXES or "/test/" in path or "/tests/" in path:
        return False
    return (
        path.startswith("src/zicato/")
        or (path.startswith("crates/") and "/src/" in path)
        or path.startswith("integrations/")
        or path == "hatch_build.py"
    )


def _language(path: str) -> str:
    suffix = PurePosixPath(path).suffix.lower()
    return {
        ".css": "CSS",
        ".html": "HTML",
        ".js": "JavaScript",
        ".mjs": "JavaScript",
        ".py": "Python",
        ".rs": "Rust",
        ".sh": "Shell",
        ".sql": "SQL",
        ".ts": "TypeScript",
    }.get(suffix, suffix.removeprefix(".").upper() or "Other")


def _python_logic(source: str) -> int:
    """Count Python lines that are neither blank, comment-only, nor docstring.

    A docstring's own lines are prose; a comment sharing a line with code
    leaves that line executable. Unparseable source falls back to every
    non-blank line, which never undercounts.
    """
    lines = source.splitlines()
    prose: set[int] = set()
    try:
        tree = ast.parse(source)
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (SyntaxError, ValueError, tokenize.TokenError):
        return sum(1 for line in lines if line.strip())
    for node in ast.walk(tree):
        if not isinstance(node, DOCSTRING_HOLDERS) or not node.body:
            continue
        head = node.body[0]
        if not isinstance(head, ast.Expr) or not isinstance(head.value, ast.Constant):
            continue
        if not isinstance(head.value.value, str):
            continue
        shared = bool(lines[head.lineno - 1][: head.col_offset].strip())
        prose.update(range(head.lineno + shared, (head.end_lineno or head.lineno) + 1))
    for token in tokens:
        row, column = token.start
        if token.type == tokenize.COMMENT and not lines[row - 1][:column].strip():
            prose.add(row)
    return sum(1 for row, line in enumerate(lines, 1) if line.strip() and row not in prose)


def _javascript_logic(source: str) -> int:
    """Count JavaScript lines that are neither blank nor comment-only.

    Comment openers are stripped from the left of each line, so a line keeping
    any other text is executable. A line beginning with a string literal that
    contains a comment opener is the one shape this reads as a comment.
    """
    count = 0
    inside = False
    for line in source.splitlines():
        text = line.strip()
        while text:
            if inside:
                end = text.find("*/")
                inside = end < 0
                text = "" if inside else text[end + 2 :].strip()
            elif text.startswith("//"):
                text = ""
            elif text.startswith("/*"):
                inside = True
                text = text[2:]
            else:
                break
        count += bool(text)
    return count


def _rust_string_start(line: str, index: int) -> tuple[int, str, bool] | None:
    """Return a string literal's opener width, closing delimiter, and escape rule."""
    if line[index] == '"':
        return 1, '"', True
    previous = line[index - 1] if index else ""
    if line[index] in "br" and not (previous.isalnum() or previous == "_"):
        match = RUST_RAW_STRING.match(line, index)
        if match:
            return len(match.group()), '"' + match.group(1), False
    return None


def _rust_code_lines(source: str) -> Iterator[str]:
    """Yield each Rust line with its comments removed and its string bodies blanked.

    Line comments (``//``, ``///``, ``//!``) and block comments are dropped;
    block comments nest and may end before code on the same line. A string
    literal keeps one quote and loses its body, so a brace inside one cannot be
    read as structure: regular and byte strings honour backslash escapes, raw
    strings their hash-delimited form. Character literals pass through as
    written, which is faithful unless one holds a quote or a comment opener.
    """
    comments = 0
    closing = ""
    escapes = False
    for line in source.splitlines():
        kept: list[str] = []
        index = 0
        while index < len(line):
            rest = line[index:]
            if comments:
                comments += rest.startswith("/*") - rest.startswith("*/")
                index += 2 if rest.startswith(("/*", "*/")) else 1
            elif closing:
                if escapes and rest.startswith("\\"):
                    index += 2
                elif rest.startswith(closing):
                    index += len(closing)
                    closing = ""
                else:
                    index += 1
            elif rest.startswith("//"):
                break
            elif rest.startswith("/*"):
                comments = 1
                index += 2
            elif (start := _rust_string_start(line, index)) is not None:
                width, closing, escapes = start
                kept.append('"')
                index += width
            else:
                kept.append(line[index])
                index += 1
        yield "".join(kept)


def _rust_logic(source: str) -> int:
    """Count Rust lines that are neither blank, comment-only, nor part of a test item.

    Comments are stripped first, so a comment sharing a line with code leaves
    that line executable. An item carrying ``#[cfg(test)]`` — every one in this
    repository is ``mod tests { … }`` — drops entirely, attribute line and
    closing brace included: brace depth follows the item to its close, over
    text whose string bodies are blanked so a brace inside a literal cannot
    move that depth. Nothing else is stripped, so attributes, ``use``
    declarations, and a lone ``}`` all count as executable.
    """
    count = depth = 0
    test_depth: int | None = None
    braced = False
    for code in _rust_code_lines(source):
        text = code.strip()
        if test_depth is None and "#[cfg(test)]" in text:
            test_depth = depth
        elif test_depth is None:
            count += bool(text)
        depth += code.count("{") - code.count("}")
        if test_depth is not None:
            braced = braced or "{" in code
            if depth <= test_depth and (braced or text.endswith(";")):
                test_depth, braced = None, False
    return count


LOGIC_COUNTERS: dict[str, Callable[[str], int]] = {
    ".js": _javascript_logic,
    ".mjs": _javascript_logic,
    ".py": _python_logic,
    ".rs": _rust_logic,
}


def _logic(path: str, data: bytes, lines: int) -> int:
    """Count executable lines, keeping the raw count for file types with no counter."""
    counter = LOGIC_COUNTERS.get(PurePosixPath(path).suffix.lower())
    if counter is None:
        return lines
    try:
        return counter(data.decode())
    except UnicodeDecodeError:
        return lines


def _subsystem(path: str) -> str:
    parts = PurePosixPath(path).parts
    if parts[:2] == ("src", "zicato"):
        return "/".join(parts[:3]) if len(parts) > 2 else "src/zicato"
    if parts[0] in {"crates", "integrations"} and len(parts) > 1:
        return "/".join(parts[:2])
    return parts[0]


def _entries(ref: str | None, cwd: Path) -> Iterator[tuple[str, int, int]]:
    """Yield each counted file's path, newline count, and executable-line count.

    The logic count is taken for production files only; other files yield 0,
    which :func:`_summarize` never adds.
    """
    for path in _paths(ref, cwd):
        if _excluded(path):
            continue
        try:
            data = _content(path, ref, cwd)
        except FileNotFoundError:
            continue
        count = data.count(b"\n")
        yield path, count, _logic(path, data, count) if _production(path) else 0


def _rank(item: tuple[str, Lines]) -> tuple[int, int, str]:
    """Sort key: production logic descending, then total descending, then name."""
    name, lines = item
    return -lines.production_logic, -lines.total, name


def _summarize(entries: Iterable[tuple[str, int, int]]) -> Report:
    languages: Counter[str] = Counter()
    tallies: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
    files = lines = production_files = production_lines = production_logic_lines = 0
    for path, count, logic in entries:
        files += 1
        lines += count
        languages[_language(path)] += count
        tally = tallies[_subsystem(path)]
        tally[0] += count
        if _production(path):
            production_files += 1
            production_lines += count
            production_logic_lines += logic
            tally[1] += count
            tally[2] += logic
    subsystems = {name: Lines(*tally) for name, tally in tallies.items()}
    return Report(
        files,
        lines,
        production_files,
        production_lines,
        production_logic_lines,
        dict(languages.most_common()),
        dict(sorted(subsystems.items(), key=_rank)),
    )


def measure(ref: str | None = None, cwd: Path = ROOT) -> Report:
    return _summarize(_entries(ref, cwd))


def _format_rows(rows: Iterable[tuple[str, int]]) -> str:
    return "\n".join(f"  {name:<28} {count:>9,}" for name, count in rows)


def render(report: Report) -> str:
    by_total = sorted(report.subsystems.items(), key=lambda item: -item[1].total)
    return "\n".join(
        (
            f"total             {report.lines:>9,} lines  {report.files:>5,} files",
            f"production        {report.production_lines:>9,} lines  "
            f"{report.production_files:>5,} files",
            f"production logic  {report.production_logic_lines:>9,} lines  "
            f"{report.production_files:>5,} files",
            "by language",
            _format_rows(report.languages.items()),
            "by subsystem",
            _format_rows((name, lines.total) for name, lines in by_total),
        )
    )


def _share(lines: Lines) -> str:
    return "" if lines.prose_share is None else f"{lines.prose_share:.1%}"


def report_json(report: Report) -> dict[str, Any]:
    """The report as JSON data, each subsystem carrying its prose share."""
    payload = asdict(report)
    payload["subsystems"] = {
        name: {
            **asdict(lines),
            "prose_share": None if lines.prose_share is None else round(lines.prose_share, 3),
        }
        for name, lines in report.subsystems.items()
    }
    return payload


def render_report(report: Report) -> str:
    """A plain table of every subsystem in descending order of production logic."""
    width = max(len(name) for name in report.subsystems) if report.subsystems else 9
    rows = [
        f"{'subsystem':<{width}}  {'total':>9}  {'production':>10}  "
        f"{'production logic':>16}  {'prose share':>11}"
    ]
    for name, lines in report.subsystems.items():
        rows.append(
            f"{name:<{width}}  {lines.total:>9,}  {lines.production:>10,}  "
            f"{lines.production_logic:>16,}  {_share(lines):>11}"
        )
    return "\n".join(rows)


def render_subsystem_table(report: Report) -> str:
    """The ledger's per-subsystem table: every subsystem holding production files."""
    rows = [SUBSYSTEM_TABLE_HEADER, "|---|---:|---:|---:|---:|"]
    for name, lines in report.subsystems.items():
        if lines.production:
            rows.append(
                f"| {name} | {lines.total:,} | {lines.production:,} | "
                f"{lines.production_logic:,} | {_share(lines)} |"
            )
    return "\n".join(rows)


def _subsystem_section(text: str) -> tuple[int, int]:
    """The span of the per-subsystem table in the ledger document, header row included."""
    start = text.find(SUBSYSTEM_HEADING)
    if start < 0:
        raise ValueError(f"{LEDGER_PATH} has no '{SUBSYSTEM_HEADING}' section")
    end = text.find("\n## ", start + len(SUBSYSTEM_HEADING))
    end = len(text) if end < 0 else end
    table = text.find(SUBSYSTEM_TABLE_HEADER, start, end)
    if table < 0:
        return end, end
    lines = text[table:end].split("\n")
    rows = 0
    while rows < len(lines) and lines[rows].startswith("|"):
        rows += 1
    return table, table + len("\n".join(lines[:rows]))


def write_summary(text: str, report: Report) -> str:
    """Return the ledger document with its per-subsystem table rewritten from the report."""
    start, end = _subsystem_section(text)
    table = render_subsystem_table(report)
    if start == end:
        table = "\n" + table + "\n"
    return text[:start] + table + text[end:]


def _table_rows(table: str) -> dict[str, str]:
    rows = {}
    for line in table.splitlines()[2:]:
        name, _, cells = line.strip().strip("|").strip().partition(" | ")
        rows[name] = cells
    return rows


def check_subsystem_table(text: str, report: Report) -> list[str]:
    """Check that the ledger's per-subsystem table states what the report measures.

    A row whose numbers differ, a subsystem with no row, and a row for a
    subsystem the report does not measure are each an error, so the table
    cannot fall behind the tree it describes. ``--write-summary`` rewrites it.
    """
    start, end = _subsystem_section(text)
    expected = render_subsystem_table(report)
    found = text[start:end]
    if found == expected:
        return []
    errors = []
    stated, measured = _table_rows(found), _table_rows(expected)
    for name, cells in measured.items():
        if name not in stated:
            errors.append(f"subsystem table: no row for {name}, which measures {cells}")
        elif stated[name] != cells:
            errors.append(
                f"subsystem table, {name}: states {stated[name]}, but the tree measures {cells}"
            )
    errors += [
        f"subsystem table, {name}: no production files measure under that name"
        for name in stated
        if name not in measured
    ]
    if not errors:
        errors.append("subsystem table: the rows are out of order")
    return errors + ["run `python tools/line_budget.py --write-summary` to rewrite the table"]


@dataclass(frozen=True)
class Point:
    """One commit on the walked chain and its per-subsystem measurements."""

    sha: str
    date: str
    subject: str
    subsystems: dict[str, Lines]


def _cached_entries(
    sha: str, blobs: dict[str, list[int]], cwd: Path
) -> Iterator[tuple[str, int, int]]:
    """Yield a commit's counted files, reading only the blobs the cache has not seen.

    The logic count is taken for every file here, so a blob's entry is valid
    wherever the file is later classified.
    """
    for blob, path in _tree(sha, cwd):
        if _excluded(path):
            continue
        key = f"{blob} {PurePosixPath(path).suffix.lower()}"
        if key not in blobs:
            data = _content(path, sha, cwd)
            count = data.count(b"\n")
            blobs[key] = [count, _logic(path, data, count)]
        count, logic = blobs[key]
        yield path, count, logic


def _chain(since: str, ref: str, cwd: Path) -> list[tuple[str, str, str]]:
    """The first-parent commits from ``since`` to ``ref``, oldest first, with date and subject.

    Membership is read off the chain itself rather than through ``since..ref``,
    so a ``since`` that a merge brought in from a side branch is refused rather
    than walked past to the root.
    """
    log = _git("log", "--first-parent", "--format=%H%x09%cs%x09%s", ref, cwd=cwd).decode()
    commits = [tuple(line.split("\t", 2)) for line in log.splitlines()]
    full = _git("rev-parse", "--verify", f"{since}^{{commit}}", cwd=cwd).decode().strip()
    index = next((i for i, commit in enumerate(commits) if commit[0] == full), None)
    if index is None:
        raise ValueError(f"{since} is not on the first-parent chain of {ref}")
    return [(sha, date, subject) for sha, date, subject in reversed(commits[: index + 1])]


def _tool_digest() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16]


def history(
    since: str, ref: str, cwd: Path = ROOT, cache_path: Path = HISTORY_CACHE
) -> list[Point]:
    """Measure every first-parent commit from ``since`` to ``ref``, through the cache.

    The cache holds per-blob counts and per-commit tallies, both content-
    addressed, under a digest of this module's source; a cache written by a
    different version of the counters is discarded whole.
    """
    digest = _tool_digest()
    cache: dict[str, Any] = {"tool": digest, "blobs": {}, "commits": {}}
    if cache_path.exists():
        stored = json.loads(cache_path.read_text())
        if stored.get("tool") == digest:
            cache = stored
    points = []
    try:
        for sha, date, subject in _chain(since, ref, cwd):
            if sha not in cache["commits"]:
                report = _summarize(_cached_entries(sha, cache["blobs"], cwd))
                cache["commits"][sha] = {
                    name: astuple(lines) for name, lines in report.subsystems.items()
                }
            subsystems = {name: Lines(*tally) for name, tally in cache["commits"][sha].items()}
            points.append(Point(sha, date, subject, subsystems))
    finally:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(cache))
    return points


def _logic_at(point: Point, name: str) -> int:
    return point.subsystems[name].production_logic if name in point.subsystems else 0


def render_history(points: list[Point], subsystem: str | None = None) -> str:
    """One row per commit: its id, date, the enforced logic count, each subsystem, the subject.

    Without a subsystem, the columns are the subsystems holding production
    logic at the last commit, in the order of that commit's values; the names
    drop the ``src/zicato/`` prefix to keep the row readable.
    """
    last = points[-1].subsystems if points else {}
    names = [subsystem] if subsystem else [n for n, lines in last.items() if lines.production_logic]
    heads = [name.removeprefix("src/zicato/") for name in names]
    widths = [max(len(head), 7) for head in heads]
    header = "commit    date        all logic  " + "  ".join(
        f"{head:>{width}}" for head, width in zip(heads, widths, strict=True)
    )
    rows = [header.rstrip() + "  subject"]
    for point in points:
        total = sum(lines.production_logic for lines in point.subsystems.values())
        cells = [
            f"{_logic_at(point, name):>{width},}" for name, width in zip(names, widths, strict=True)
        ]
        rows.append(
            f"{point.sha[:8]}  {point.date}  {total:>9,}  "
            + "  ".join(cells)
            + f"  {point.subject}"
        )
    return "\n".join(rows)


def check(report: Report, config_path: Path = CONFIG) -> list[str]:
    config = json.loads(config_path.read_text())
    limits = config["limits"]
    errors = []
    for key, actual in (
        ("total", report.lines),
        ("production", report.production_lines),
        ("production_logic", report.production_logic_lines),
    ):
        ceiling = int(limits[key])
        if actual > ceiling:
            errors.append(f"{key}: {actual:,} exceeds {ceiling:,} by {actual - ceiling:,}")
    return errors


@dataclass(frozen=True)
class LedgerRow:
    """One deliberate increase: which measurement it moved, and from what to what."""

    label: str
    measurement: str
    previous: int
    delta: int
    new: int

    def __str__(self) -> str:
        return f"{self.label} ({self.measurement}) {self.previous:,} {self.delta:+,} {self.new:,}"


def _ledger_number(cell: str) -> int:
    return int(cell.replace(",", "").replace("+", ""))


def parse_ledger(text: str) -> tuple[list[LedgerRow], list[str]]:
    """Read the deliberate-increases table into rows, naming any row that will not parse.

    A row is its label, the measurement the label's parenthetical names, and the
    three numbers. The reason cell is dropped, so rewording one leaves the row
    unchanged.
    """
    rows: list[LedgerRow] = []
    errors: list[str] = []
    section = text.partition(LEDGER_HEADING)[2].partition("\n## ")[0]
    for line in section.splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if cells[0] == "Change" or set(cells[0]) <= set("-:"):
            continue
        named = LEDGER_LABEL.fullmatch(cells[0])
        if len(cells) != 5 or named is None:
            errors.append(f"unreadable row: {line.strip()}")
            continue
        try:
            numbers = [_ledger_number(cell) for cell in cells[1:4]]
        except ValueError:
            errors.append(f"unreadable numbers: {cells[0]}")
            continue
        measurement = named["measurement"].replace(" ", "_")
        rows.append(LedgerRow(named["label"], measurement, *numbers))
    if not rows:
        errors.append(f"no rows found under '{LEDGER_HEADING}'")
    return rows, errors


def _dropped_rows(rows: list[LedgerRow], base_text: str) -> list[str]:
    """Name every base-revision row that is absent from the working tree's ledger."""
    present = {astuple(row) for row in rows}
    base = parse_ledger(base_text)[0]
    return [
        f"{row}: present in the base ledger and missing here"
        for row in base
        if astuple(row) not in present
    ]


def check_summary(text: str, config_path: Path = CONFIG) -> list[str]:
    """Check that the summary table states the limits the config holds.

    The table above the ledger reports each measurement's baseline, its
    enforced limit, and the difference between them, and the prose beside it
    says those limits are the ones ``.line-budget.json`` holds. Nothing made
    that true: a change that moved the config could leave the table behind,
    and twice did. Two rules close it, both read against the config:

    1. Each row's enforced limit equals the configured limit.
    2. Each row's last column equals its limit minus its baseline.

    A missing or unreadable row is an error in itself, so deleting the table
    is not a way to pass.
    """
    errors: list[str] = []
    config = json.loads(config_path.read_text())
    found = {match["label"]: match for match in SUMMARY_ROW.finditer(text)}
    for label, measurement in SUMMARY_LABELS:
        row = found.get(label)
        if row is None:
            errors.append(f"summary table: no readable '{label}' row")
            continue
        baseline = _ledger_number(row["baseline"])
        limit = _ledger_number(row["limit"])
        difference = int(row["difference"].replace(",", ""))
        configured = int(config["limits"][measurement])
        if limit != configured:
            errors.append(
                f"summary table, {label}: states the limit {limit:,}, "
                f"but .line-budget.json holds {configured:,}"
            )
        if difference != limit - baseline:
            errors.append(
                f"summary table, {label}: the last column states {difference:+,}, "
                f"but the limit minus the baseline is {limit - baseline:+,}"
            )
    return errors


def check_ledger(text: str, base_text: str | None = None, config_path: Path = CONFIG) -> list[str]:
    """Check the deliberate-increases ledger's arithmetic, chaining, and completeness.

    Four rules hold over the table, the first three read from the ledger alone:

    1. Every row's previous value plus its signed delta equals its new value.
    2. Each row's parenthetical names one of the three measurements.
    3. Within one measurement, a row starts no higher than the value the
       preceding row for that measurement reached. A start below it is a
       reduction, which ratchets the limit with no row of its own; a start above
       it means a row was dropped or invented.
    4. The last row for a measurement stands at or above that measurement's
       enforced limit in ``.line-budget.json``. An increase sets the limit to
       the value its row states, so where the last recorded event is an
       increase the two are equal, and a reduction can only carry the limit
       further down. A last row below the limit therefore means the increase
       that raised the limit to where it stands was never written down. The
       logic rows sit above their limit by a second amount the ledger explains:
       a ``production_logic`` value in the table is measured over a definition
       reaching only Python and JavaScript.

    :func:`check_summary` adds two more over the summary table above the
    ledger, so a change that moves a limit in ``.line-budget.json`` cannot
    leave that table stating the old one.

    With ``base_text``, a further rule makes the ledger append-only: every row
    the base revision records must still be present with the same label,
    measurement, and numbers. The reason cell is free to be reworded.
    """
    rows, errors = parse_ledger(text)
    limits = json.loads(config_path.read_text())["limits"]
    reached: dict[str, int] = {}
    for row in rows:
        if row.previous + row.delta != row.new:
            errors.append(f"{row}: the sum is {row.previous + row.delta:,}")
        standing = reached.get(row.measurement)
        if standing is not None and row.previous > standing:
            errors.append(f"{row}: starts above the {standing:,} the preceding row reached")
        reached[row.measurement] = row.new
    for measurement in MEASUREMENTS:
        ceiling = int(limits[measurement])
        if measurement not in reached:
            errors.append(f"{measurement}: no row records it")
        elif reached[measurement] < ceiling:
            errors.append(
                f"{measurement}: the last row reaches {reached[measurement]:,}, "
                f"below the enforced {ceiling:,}"
            )
    errors += check_summary(text, config_path)
    return errors + (_dropped_rows(rows, base_text) if base_text is not None else [])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", help="measure a commit instead of the worktree")
    parser.add_argument("--check", action="store_true", help="enforce configured limits")
    parser.add_argument(
        "--check-ledger",
        action="store_true",
        help="check the deliberate-increases ledger instead of measuring",
    )
    parser.add_argument("--base", help="ref whose ledger rows must all still be present")
    parser.add_argument(
        "--report",
        action="store_true",
        help="print every subsystem's three counts and prose share, by production logic",
    )
    parser.add_argument(
        "--write-summary",
        action="store_true",
        help=f"rewrite the per-subsystem table in {LEDGER_PATH} from the measurement",
    )
    parser.add_argument(
        "--history",
        action="store_true",
        help="print the production-logic series per subsystem along first-parent commits",
    )
    parser.add_argument("--since", help="oldest commit of the walk (default: the baseline ref)")
    parser.add_argument("--subsystem", help="restrict the history to one subsystem")
    parser.add_argument("--cache", type=Path, default=HISTORY_CACHE, help="history cache file")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    if args.check_ledger:
        base = _content(LEDGER_PATH, args.base, ROOT).decode() if args.base else None
        text = LEDGER.read_text()
        errors = check_ledger(text, base) + check_subsystem_table(text, measure(args.ref))
        subject = "line-budget ledger"
    elif args.history:
        since = args.since or json.loads(CONFIG.read_text())["baseline"]["ref"]
        try:
            points = history(since, args.ref or "HEAD", cache_path=args.cache)
        except ValueError as error:
            print(f"line-budget history failed:\n  {error}", file=sys.stderr)
            return 1
        if args.as_json:
            print(json.dumps([asdict(point) for point in points], indent=2))
        else:
            print(render_history(points, args.subsystem))
        return 0
    else:
        report = measure(args.ref)
        if args.write_summary:
            LEDGER.write_text(write_summary(LEDGER.read_text(), report))
        if args.as_json:
            print(json.dumps(report_json(report), indent=2))
        elif args.report:
            print(render_report(report))
        else:
            print(render(report))
        errors = check(report) if args.check else []
        subject = "line budget"
    if errors:
        message = f"{subject} failed:\n" + "\n".join(f"  {error}" for error in errors)
        print(message, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
