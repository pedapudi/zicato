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
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / ".line-budget.json"
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", help="measure a commit instead of the worktree")
    parser.add_argument("--check", action="store_true", help="enforce configured limits")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    report = measure(args.ref)
    print(json.dumps(asdict(report), indent=2) if args.as_json else render(report))
    errors = check(report) if args.check else []
    if errors:
        message = "line budget failed:\n" + "\n".join(f"  {error}" for error in errors)
        print(message, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
