"""Report and enforce the repository's simplification budgets."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / ".line-budget.json"
LOCKFILES = {"Cargo.lock", "uv.lock", "package-lock.json", "npm-shrinkwrap.json"}
GENERATED = (
    "docs/presentation/contact-sheet.png",
    "docs/presentation/index.html",
    "docs/presentation/slides/",
    "docs/presentation/zicato-deck.pdf",
    "src/zicato/dashboard/static/app_T.js",
    "src/zicato/dashboard/static/test/fixtures/trace_view/",
    "tests/data/reader_parity_snapshot.json",
)
ASSET_SUFFIXES = {".ico", ".pdf", ".png", ".svg", ".woff2"}


@dataclass(frozen=True)
class Report:
    files: int
    lines: int
    production_files: int
    production_lines: int
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
        or any(path == item or path.startswith(item) for item in GENERATED)
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
    files = lines = production_files = production_lines = 0
    for path in _paths(ref, cwd):
        if _excluded(path):
            continue
        try:
            count = _content(path, ref, cwd).count(b"\n")
        except FileNotFoundError:
            continue
        files += 1
        lines += count
        languages[_language(path)] += count
        subsystems[_subsystem(path)] += count
        if _production(path):
            production_files += 1
            production_lines += count
    return Report(
        files,
        lines,
        production_files,
        production_lines,
        dict(languages.most_common()),
        dict(subsystems.most_common()),
    )


def _format_rows(rows: Iterable[tuple[str, int]]) -> str:
    return "\n".join(f"  {name:<28} {count:>9,}" for name, count in rows)


def render(report: Report) -> str:
    return "\n".join(
        (
            f"total       {report.lines:>9,} lines  {report.files:>5,} files",
            f"production  {report.production_lines:>9,} lines  "
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
