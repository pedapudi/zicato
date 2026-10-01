"""Report and enforce the repository's simplification budgets.

Three measurements are taken over the tracked files: ``total`` newline counts,
the ``production`` subset of them, and the ``production_logic`` lines that
execute. Each is enforced against a limit: the starting limit
``.line-budget.json`` holds plus the deltas the ledger entries under
``docs/design/line-budget-ledger/`` record. Each is also reported per
subsystem, where a subsystem's three numbers partition the repository-wide
ones, so the per-subsystem logic column sums to the enforced logic count. A
subsystem's prose share is the share of its production lines that do not
execute: ``1 - production_logic / production``.

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
from dataclasses import asdict, astuple, dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / ".line-budget.json"
LEDGER_PATH = "docs/design/LINE-BUDGET.md"
LEDGER = ROOT / LEDGER_PATH
# One Markdown file per change: its name, the delta it moves each measurement
# by, and the reason. Independent changes add different files, so no two of
# them edit the same line.
ENTRIES_PATH = "docs/design/line-budget-ledger"
ENTRY_NAME = re.compile(r"\d{4}-\d{2}-\d{2}-[a-z0-9]+(?:-[a-z0-9]+)*\.md")
ENTRY_TABLE = ("| Measurement | Delta |", "|---|---:|")
ENTRY_ROW = re.compile(
    r"\| (?P<label>Total|Production|Production logic) \| (?P<delta>[+-]?[\d,]+) \|"
)
# The closed table of changes recorded with running totals. Its rows are not
# re-derived; the digest pins their labels, measurements, and numbers, so a
# reason may be reworded and nothing else may change.
HISTORY_HEADING = "## Changes recorded with running totals"
HISTORY_DIGEST = "a306b5f689f8cd53892bf29b3b7ad2ec7173a99aa93fddc1120b17959177ee8b"
HISTORY_CACHE = ROOT / ".cache" / "line_budget_history.json"
MEASUREMENTS = ("total", "production", "production_logic")
# The summary table's row labels, in the order the table lists them, against the
# measurement each names in the config.
SUMMARY_LABELS = (
    ("Total", "total"),
    ("Production", "production"),
    ("Production logic", "production_logic"),
)
# A summary row: the label, the baseline, the starting limit, and their signed
# difference.
SUMMARY_ROW = re.compile(
    r"^\| (?P<label>Total|Production|Production logic) \| (?P<baseline>[\d,]+) \| "
    r"(?P<limit>[\d,]+) \| (?P<difference>[+-][\d,]+) \|$",
    re.MULTILINE,
)
# A closed-table row's first cell: the change's name, then the measurement it moved.
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


def render(report: Report, limits: dict[str, int]) -> str:
    by_total = sorted(report.subsystems.items(), key=lambda item: -item[1].total)
    return "\n".join(
        (
            f"total             {report.lines:>9,} lines  {report.files:>5,} files  "
            f"limit {limits['total']:>9,}",
            f"production        {report.production_lines:>9,} lines  "
            f"{report.production_files:>5,} files  limit {limits['production']:>9,}",
            f"production logic  {report.production_logic_lines:>9,} lines  "
            f"{report.production_files:>5,} files  limit {limits['production_logic']:>9,}",
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


def _measured(report: Report) -> dict[str, int]:
    return {
        "total": report.lines,
        "production": report.production_lines,
        "production_logic": report.production_logic_lines,
    }


def check(report: Report, limits: dict[str, int]) -> list[str]:
    """Name every measurement the report puts above its enforced limit."""
    errors = []
    for key, actual in _measured(report).items():
        ceiling = limits[key]
        if actual > ceiling:
            errors.append(f"{key}: {actual:,} exceeds {ceiling:,} by {actual - ceiling:,}")
    return errors


@dataclass(frozen=True)
class LedgerRow:
    """One row of the closed table: the measurement it moved, and from what to what."""

    label: str
    measurement: str
    previous: int
    delta: int
    new: int

    def __str__(self) -> str:
        return f"{self.label} ({self.measurement}) {self.previous:,} {self.delta:+,} {self.new:,}"


@dataclass(frozen=True)
class Entry:
    """One change's movement of the three limits, in ``MEASUREMENTS`` order, and why.

    ``name`` is the entry's file name; ``title`` names the change. The reason
    is free to be reworded, so it takes no part in comparing entries.
    """

    name: str
    title: str
    deltas: tuple[int, ...]
    reason: str = field(compare=False)

    def __str__(self) -> str:
        moves = ", ".join(
            f"{label.lower()} {delta:+,}"
            for (label, _), delta in zip(SUMMARY_LABELS, self.deltas, strict=True)
        )
        return f"{self.name} ({moves})"


def _ledger_number(cell: str) -> int:
    return int(cell.replace(",", "").replace("+", ""))


def parse_history(text: str) -> tuple[list[LedgerRow], list[str]]:
    """Read the closed table into rows, naming any row that will not parse."""
    rows: list[LedgerRow] = []
    errors: list[str] = []
    section = text.partition(HISTORY_HEADING)[2].partition("\n## ")[0]
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
        errors.append(f"no rows found under '{HISTORY_HEADING}'")
    return rows, errors


def history_digest(rows: Iterable[LedgerRow]) -> str:
    return hashlib.sha256("\n".join(map(str, rows)).encode()).hexdigest()


def check_history(text: str, config: dict[str, Any]) -> list[str]:
    """Check that the closed table is unchanged and that the starting limits continue it.

    Its rows must hash to ``HISTORY_DIGEST``, and each starting limit in
    ``.line-budget.json`` must equal the value the table's last row for that
    measurement reaches, so neither can move a limit without an entry.
    """
    rows, errors = parse_history(text)
    if history_digest(rows) != HISTORY_DIGEST:
        errors.append(
            f"the table under '{HISTORY_HEADING}' is closed and its rows changed; "
            f"record a change as a file under {ENTRIES_PATH}/"
        )
    reached = {row.measurement: row.new for row in rows}
    for measurement in MEASUREMENTS:
        start = int(config["starting_limits"][measurement])
        if reached.get(measurement) != start:
            errors.append(
                f"{measurement}: .line-budget.json starts the limit at {start:,}, "
                f"but the closed table ends at {reached.get(measurement, 0):,}"
            )
    return errors


def check_summary(text: str, config: dict[str, Any]) -> list[str]:
    """Check that the summary table states the starting limits the config holds.

    The table reports each measurement's baseline, its starting limit, and the
    difference between them. Two rules hold it to the config:

    1. Each row's starting limit equals the configured one.
    2. Each row's last column equals its limit minus its baseline.

    A missing or unreadable row is an error in itself, so deleting the table
    is not a way to pass.
    """
    errors: list[str] = []
    found = {match["label"]: match for match in SUMMARY_ROW.finditer(text)}
    for label, measurement in SUMMARY_LABELS:
        row = found.get(label)
        if row is None:
            errors.append(f"summary table: no readable '{label}' row")
            continue
        baseline = _ledger_number(row["baseline"])
        limit = _ledger_number(row["limit"])
        difference = int(row["difference"].replace(",", ""))
        configured = int(config["starting_limits"][measurement])
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


def parse_entry(name: str, text: str) -> tuple[Entry | None, list[str]]:
    """Read one entry file: a ``# `` title, the three-row delta table, and a reason."""
    errors = []
    if not ENTRY_NAME.fullmatch(name):
        errors.append(f"{name}: an entry file is named YYYY-MM-DD-words-joined-by-hyphens.md")
    lines = text.splitlines()
    title = lines[0].removeprefix("# ").strip() if lines and lines[0].startswith("# ") else ""
    if not title:
        errors.append(f"{name}: the first line must be '# ' followed by the change's name")
    deltas: dict[str, int] = {}
    reason = []
    for line in lines[1:]:
        row = ENTRY_ROW.fullmatch(line.strip())
        if row and row["label"] not in deltas:
            deltas[row["label"]] = _ledger_number(row["delta"])
        elif line.startswith("|") and line.strip() not in ENTRY_TABLE:
            errors.append(f"{name}: unreadable or repeated row: {line.strip()}")
        elif not line.startswith("|") and line.strip():
            reason.append(line.strip())
    missing = [label for label, _ in SUMMARY_LABELS if label not in deltas]
    if missing:
        errors.append(f"{name}: no delta row for {', '.join(missing)}")
    if not reason:
        errors.append(f"{name}: no reason follows the table")
    if errors:
        return None, errors
    ordered = tuple(deltas[label] for label, _ in SUMMARY_LABELS)
    return Entry(name, title, ordered, " ".join(reason)), []


def read_entries(ref: str | None = None, cwd: Path = ROOT) -> tuple[list[Entry], list[str]]:
    """Every ledger entry in the worktree or at a ref, in file-name order.

    The ledger directory holds only entry files; a directory inside it is an
    error rather than a file that fails to parse.
    """
    files: dict[str, str] = {}
    directories: list[str] = []
    if ref:
        listing = _git("ls-tree", "-z", ref, f"{ENTRIES_PATH}/", cwd=cwd).decode()
        for item in filter(None, listing.split("\0")):
            meta, path = item.split("\t", 1)
            name = PurePosixPath(path).name
            if meta.split()[1] == "tree":
                directories.append(name)
            else:
                files[name] = _content(path, ref, cwd).decode()
    else:
        directory = cwd / ENTRIES_PATH
        for child in sorted(directory.iterdir()) if directory.is_dir() else []:
            if child.is_dir():
                directories.append(child.name)
            else:
                files[child.name] = child.read_text()
    entries: list[Entry] = []
    errors = [
        f"{ENTRIES_PATH}/{name}: the ledger holds only entry files" for name in sorted(directories)
    ]
    for name in sorted(files):
        entry, problems = parse_entry(name, files[name])
        errors += problems
        if entry is not None:
            entries.append(entry)
    return entries, errors


def enforced_limits(config: dict[str, Any], entries: Iterable[Entry]) -> dict[str, int]:
    """Each measurement's starting limit plus the deltas every entry records."""
    limits = {key: int(config["starting_limits"][key]) for key in MEASUREMENTS}
    for entry in entries:
        for key, delta in zip(MEASUREMENTS, entry.deltas, strict=True):
            limits[key] += delta
    return limits


def check_fork(
    cwd: Path, fork: str, entries: list[Entry], config: dict[str, Any], text: str
) -> list[str]:
    """Hold the entries, starting limits, and closed table to their values at the fork point.

    Every entry present at the fork point is still present with the same name,
    title, and deltas; its reason may be reworded. The starting limits and the
    closed table's rows equal the fork point's, so editing them together with
    ``HISTORY_DIGEST`` cannot move a limit. A fork point that predates the
    per-change ledger holds neither, and those two comparisons are skipped.
    """
    names = {entry.name: entry for entry in entries}
    errors = [
        f"{entry}: present at {fork[:12]} and missing or altered here"
        for entry in read_entries(fork, cwd)[0]
        if names.get(entry.name) != entry
    ]
    earlier = json.loads(_content(CONFIG.name, fork, cwd)).get("starting_limits")
    if earlier is not None and any(
        int(earlier[key]) != int(config["starting_limits"][key]) for key in MEASUREMENTS
    ):
        errors.append(
            f"starting limits: .line-budget.json differs from {fork[:12]}; "
            "a limit moves only by a ledger entry"
        )
    earlier_text = _content(LEDGER_PATH, fork, cwd).decode()
    if HISTORY_HEADING in earlier_text and parse_history(earlier_text)[0] != parse_history(text)[0]:
        errors.append(f"the closed table under '{HISTORY_HEADING}' differs from {fork[:12]}")
    return errors


def check_reconciled(
    limits: dict[str, int],
    measured: dict[str, int],
    added: list[Entry],
    base_gap: dict[str, int] | None = None,
    fork: str = "",
) -> list[str]:
    """Require each enforced limit to equal what the tree measures.

    Every commit on the base meets this rule, so a change meets it exactly when
    the entries it adds record its own movement. A change to the counting rules
    moves the measurement by the lines it exposes or hides, and records that.

    ``base_gap`` is how far the base's own measurement stands from its limits
    (measured minus limit). It is nonzero only after merges whose logic counts
    did not add up. The failure names it on a line of its own and leaves it out
    of the table printed for this change, so only one change records it; that
    change balances the tree and passes.
    """
    errors = [
        f"{key}: the limit is {limits[key]:,}, but the tree measures {measured[key]:,}"
        for key in MEASUREMENTS
        if limits[key] != measured[key]
    ]
    if not errors:
        return []
    gap = base_gap or dict.fromkeys(MEASUREMENTS, 0)
    if any(gap.values()):
        moves = ", ".join(f"{label.lower()} {gap[key]:+,}" for label, key in SUMMARY_LABELS)
        errors.append(
            f"the base {fork[:12]} is off its limits by {moves}; "
            "record that difference in a change of its own"
        )
    recorded = {
        key: sum(entry.deltas[index] for entry in added) for index, key in enumerate(MEASUREMENTS)
    }
    needed = {key: recorded[key] + measured[key] - limits[key] - gap[key] for key in MEASUREMENTS}
    if needed != recorded:
        table = "\n".join(f"    | {label} | {needed[key]:+,} |" for label, key in SUMMARY_LABELS)
        errors.append(
            f"record the change in a file under {ENTRIES_PATH}/ whose table states:\n{table}"
        )
    return errors


def _base_gap(cwd: Path, fork: str) -> dict[str, int] | None:
    """The fork point's measurement minus its limits, or None when it cannot be read alike.

    The fork point is measured only when ``tools/line_budget.py`` is unchanged
    since then, so both sides are counted by one set of rules, and only when it
    holds starting limits.
    """
    tool = PurePosixPath("tools", Path(__file__).name).as_posix()
    if _content(tool, fork, cwd) != (cwd / tool).read_bytes():
        return None
    config = json.loads(_content(CONFIG.name, fork, cwd))
    if "starting_limits" not in config:
        return None
    limits = enforced_limits(config, read_entries(fork, cwd)[0])
    measured = _measured(measure(fork, cwd))
    return {key: measured[key] - limits[key] for key in MEASUREMENTS}


def _fork_point(cwd: Path, base: str) -> str:
    result = subprocess.run(
        ["git", "merge-base", base, "HEAD"], cwd=cwd, capture_output=True, text=True
    )
    if result.returncode != 0:
        raise ValueError(
            f"--base {base} is not a commit sharing history with HEAD in this clone; "
            "fetch it or name another base"
        )
    return result.stdout.strip()


def check_ledger(cwd: Path = ROOT, base: str | None = None) -> list[str]:
    """Check the entries, the closed table, and the summary table in a worktree.

    Each enforced limit must equal the worktree's measurement. With ``base``,
    the fork point of ``HEAD`` and ``base`` supplies three things:
    :func:`check_fork` compares the entries, starting limits, and closed table
    with it; the entries absent there are the ones the failure attributes to
    this change; and its own measurement against its limits, which
    :func:`check_reconciled` reports apart from this change's table. Two
    changes that record their own deltas never need each other's numbers.
    """
    text = (cwd / LEDGER_PATH).read_text()
    config = json.loads((cwd / CONFIG.name).read_text())
    entries, errors = read_entries(None, cwd)
    errors += check_history(text, config) + check_summary(text, config)
    added, gap, fork = entries, None, ""
    if base is not None:
        try:
            fork = _fork_point(cwd, base)
        except ValueError as error:
            return [*errors, str(error)]
        errors += check_fork(cwd, fork, entries, config, text)
        known = {entry.name for entry in read_entries(fork, cwd)[0]}
        added = [entry for entry in entries if entry.name not in known]
        gap = _base_gap(cwd, fork)
    measured = _measured(measure(None, cwd))
    return errors + check_reconciled(enforced_limits(config, entries), measured, added, gap, fork)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", help="measure a commit instead of the worktree")
    parser.add_argument("--check", action="store_true", help="enforce the limits")
    parser.add_argument(
        "--check-ledger",
        action="store_true",
        help="check the ledger entries and the tables in the policy document instead of measuring",
    )
    parser.add_argument(
        "--base",
        help="with --check-ledger, read the fork point of HEAD and this ref: its entries, "
        "starting limits, and closed table must be unchanged, entries absent there are this "
        "change's, and its own measurement against its limits is reported apart",
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help="print every subsystem's three counts and prose share, by production logic",
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
        errors = check_ledger(ROOT, args.base)
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
        config = json.loads(_content(CONFIG.name, args.ref, ROOT))
        entries, entry_errors = read_entries(args.ref)
        limits = enforced_limits(config, entries)
        if args.as_json:
            print(json.dumps(report_json(report), indent=2))
        elif args.report:
            print(render_report(report))
        else:
            print(render(report, limits))
        errors = entry_errors + check(report, limits) if args.check else []
        subject = "line budget"
    if errors:
        message = f"{subject} failed:\n" + "\n".join(f"  {error}" for error in errors)
        print(message, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
