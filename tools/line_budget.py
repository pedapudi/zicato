"""Report and enforce the repository's simplification budgets."""

from __future__ import annotations

import argparse
import ast
import io
import json
import re
import subprocess
import sys
import tokenize
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from dataclasses import asdict, astuple, dataclass
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / ".line-budget.json"
LEDGER_PATH = "docs/design/LINE-BUDGET.md"
LEDGER = ROOT / LEDGER_PATH
LEDGER_HEADING = "## Deliberate increases"
MEASUREMENTS = ("total", "production", "production_logic")
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
    # producer emitted. Shortening one to save lines would destroy the record.
    "src/zicato/dashboard/static/test/fixtures/trace_view/",
    "tests/data/reader_parity_snapshot.json",
)
ASSET_SUFFIXES = {".ico", ".pdf", ".png", ".svg", ".woff2"}
DOCSTRING_HOLDERS = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
# Opens a Rust raw string: an optional byte marker, the raw marker, and the
# hashes whose count the matching close repeats.
RUST_RAW_STRING = re.compile(r'b?r(#*)"')


@dataclass(frozen=True)
class Report:
    files: int
    lines: int
    production_files: int
    production_lines: int
    production_logic_lines: int
    languages: dict[str, int]
    subsystems: dict[str, int]


def _git(*args: str, cwd: Path = ROOT) -> bytes:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True).stdout


def _paths(ref: str | None, cwd: Path) -> list[str]:
    command = ("ls-tree", "-r", "--name-only", ref) if ref else ("ls-files",)
    return _git(*command, cwd=cwd).decode().splitlines()


def _content(path: str, ref: str | None, cwd: Path) -> bytes:
    return _git("show", f"{ref}:{path}", cwd=cwd) if ref else (cwd / path).read_bytes()


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


def measure(ref: str | None = None, cwd: Path = ROOT) -> Report:
    languages: Counter[str] = Counter()
    subsystems: Counter[str] = Counter()
    files = lines = production_files = production_lines = production_logic_lines = 0
    for path in _paths(ref, cwd):
        if _excluded(path):
            continue
        try:
            data = _content(path, ref, cwd)
        except FileNotFoundError:
            continue
        count = data.count(b"\n")
        files += 1
        lines += count
        languages[_language(path)] += count
        subsystems[_subsystem(path)] += count
        if _production(path):
            production_files += 1
            production_lines += count
            production_logic_lines += _logic(path, data, count)
    return Report(
        files,
        lines,
        production_files,
        production_lines,
        production_logic_lines,
        dict(languages.most_common()),
        dict(subsystems.most_common()),
    )


def _format_rows(rows: Iterable[tuple[str, int]]) -> str:
    return "\n".join(f"  {name:<28} {count:>9,}" for name, count in rows)


def render(report: Report) -> str:
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
            _format_rows(report.subsystems.items()),
        )
    )


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

    With ``base_text``, a fifth rule makes the table append-only: every row the
    base revision records must still be present with the same label,
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
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    if args.check_ledger:
        base = _content(LEDGER_PATH, args.base, ROOT).decode() if args.base else None
        errors = check_ledger(LEDGER.read_text(), base)
        subject = "line-budget ledger"
    else:
        report = measure(args.ref)
        print(json.dumps(asdict(report), indent=2) if args.as_json else render(report))
        errors = check(report) if args.check else []
        subject = "line budget"
    if errors:
        message = f"{subject} failed:\n" + "\n".join(f"  {error}" for error in errors)
        print(message, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
