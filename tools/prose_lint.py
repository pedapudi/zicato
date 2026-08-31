"""Report prose that a reader outside the development history cannot decode.

Documentation, the README, docstrings, and comments are read by people who
have the tree and nothing else. Six constructions reliably break that: each
one carries a meaning that was obvious while the change was being made and
is unrecoverable afterwards. This tool finds them and reports them; it
rewrites nothing.

The rules, and why each one is a defect:

codename-label
    Invented short labels used as vocabulary — invariant identifiers,
    robustness layers, numbered phases and waves. The label stands for a
    concept whose definition lives in one project's memory, leaving the
    reader a token and no referent; name the concept instead. Standard
    collocations (a squared norm, a load balancer at a numbered network
    layer) pass, as does any token carrying an inline waiver.

narrated-history
    Wording about what the tree stopped doing rather than what it does,
    including a state of the code named only by an issue number. A document
    specifies the present; the history is in the log, and a reader who
    cannot see the earlier state can do nothing with a sentence about it.

antithesis-apposition
    A trailing clause defining the subject by contrast with an absent
    alternative. The reader cannot see what is being ruled out, and the
    fact that would have defined the subject directly is usually missing.

empty-intensifier
    Adverbs asserting conviction in place of information. Reported for
    review rather than as a failure, because a few uses are load-bearing.

appositive-verb-tag
    A trailing verb with no subject, tense, or result attached, reading as
    a claim that something was done without saying by what or to what.

bare-issue-reference
    An issue number standing where a statement belongs. The number is no
    description, and a reader without the tracker learns nothing from it.
    An issue number beside a plain statement passes.

What is read: Markdown files, and Python docstrings and comments, under the
scanned roots — the documentation tree, the README and CHANGELOG, the runtime
package, and the example, skill, and tool trees. Fenced blocks and backtick
spans are quoted material and are masked before matching; Python strings other
than docstrings stay unread. JavaScript comments are out of scope, so the
dashboard client is unread.

Two paths under the scanned roots are skipped, because neither can be fixed
where the hit appears. `docs/design/CLI.md` is generated from `zicato --help`,
so its text is help literals owned by the command definitions. The captured
bytes under `tools/parity/golden` are a record of what a run produced, and
editing one to satisfy a lint would destroy the evidence it exists to hold.

Waiver: put `prose-lint: allow <rule-id>` (several ids may be listed, or
`all`) on the offending line or the line above it.

Usage:

    python tools/prose_lint.py                       # report and fail on hits
    python tools/prose_lint.py --rule codename-label # one rule
    python tools/prose_lint.py --write-baseline tools/prose_lint_baseline.json
    python tools/prose_lint.py --baseline tools/prose_lint_baseline.json

The last form is the ratchet: it fails only where a rule's count rises
above the committed baseline, so the check can guard a tree whose backlog
is still being worked through.
"""

from __future__ import annotations

import argparse
import ast
import io
import json
import re
import sys
import tokenize
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PATHS = (
    "CHANGELOG.md",
    "README.md",
    "docs",
    "examples",
    "skills",
    "src/zicato",
    "tools",
)
# Generated help text and captured run evidence: a hit in either is owned
# somewhere else, so neither can be fixed where it appears.
EXCLUDED = ("docs/design/CLI.md", "tools/parity/golden")
REVIEW = "review"
FAILURE = "failure"
SAMPLE = 20  # hits printed per risen rule in ratchet mode

# Collocations in which a layer number is standard technical English rather
# than an invented label. Matched case-insensitively as substrings.
ALLOWED = (
    "l1/l2 penalty",
    "l1 regulari",
    "l2 norm",
    "l4 load balancer",
    "l7",
    "l1 cache",
    "l2 cache",
    "l3 cache",
)


@dataclass(frozen=True)
class Rule:
    """One construction, its pattern, and whether a hit fails the run."""

    name: str
    pattern: re.Pattern[str]
    severity: str


# Case matters for the label rule: lowercase "l1" or "d2" is a hex digit or an
# identifier far more often than a codename. The word rules ignore case.
RULES: tuple[Rule, ...] = tuple(
    Rule(name, re.compile(pattern, flags), severity)
    for name, pattern, severity, flags in (
        (
            "codename-label",
            r"\bWS-?[A-Z0-9]{1,3}\b|\bDQ[0-9]{1,2}\b|\bD[0-9]{1,2}\b|\bL[0-6]\b"
            r"|\bPhase [A-Z0-9]\b|\bWave-?[0-9]\b",
            FAILURE,
            re.NOFLAG,
        ),
        (
            "narrated-history",
            r"\b(no longer|used to|previously|originally|as before|formerly|historically)\b"
            r"|\bthe (old|new|previous|legacy|current|latest) (pipeline|system|version|design"
            r"|implementation|path|reader|writer|format|behaviou?r|shape)\b"
            r"|\b(pre|post|since|before|after)-?#[0-9]+\b",
            FAILURE,
            re.IGNORECASE,
        ),
        ("antithesis-apposition", r", not [a-z]", FAILURE, re.NOFLAG),
        (
            "empty-intensifier",
            r"\b(demonstrably|genuinely|deliberately|precisely|exactly)\b",
            REVIEW,
            re.IGNORECASE,
        ),
        (
            "appositive-verb-tag",
            r", (measured|scored|executed|verified|confirmed)[ .,:]",
            FAILURE,
            re.IGNORECASE,
        ),
        ("bare-issue-reference", r"^\s*#[0-9]+ |^\s*\(#[0-9]+\)\s*$", FAILURE, re.NOFLAG),
    )
)

CODE_SPAN = re.compile(r"`[^`]*`")
FENCE = re.compile(r"^\s*(```|~~~)")
WAIVER = re.compile(r"prose-lint:\s*allow\s+([A-Za-z0-9_,\- ]+)")
DOCSTRING_HOLDERS = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


@dataclass(frozen=True)
class Hit:
    """One matched construction, at a repository-relative path and line."""

    path: str
    line: int
    rule: str
    text: str


def _mask(text: str) -> str:
    """Blank out backtick spans, preserving length so columns still line up."""
    return CODE_SPAN.sub(lambda found: " " * len(found.group()), text)


def markdown_prose(source: str) -> list[tuple[int, str]]:
    """Number every prose line of a Markdown file, dropping fenced blocks."""
    prose: list[tuple[int, str]] = []
    fence = ""
    for number, line in enumerate(source.splitlines(), 1):
        marker = FENCE.match(line)
        if marker:
            token = marker.group(1)
            fence = "" if fence == token else (fence or token)
            continue
        if not fence:
            prose.append((number, _mask(line)))
    return prose


def python_prose(source: str) -> list[tuple[int, str]]:
    """Number the docstring and comment lines of a Python module."""
    lines = source.splitlines()
    prose: dict[int, str] = {}
    try:
        tree = ast.parse(source)
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (SyntaxError, tokenize.TokenError, IndentationError):
        return []
    for node in ast.walk(tree):
        if not isinstance(node, DOCSTRING_HOLDERS) or not node.body:
            continue
        head = node.body[0]
        if not isinstance(head, ast.Expr) or not isinstance(head.value, ast.Constant):
            continue
        if isinstance(head.value.value, str):
            span = range(head.lineno, (head.end_lineno or head.lineno) + 1)
            prose.update((number, lines[number - 1]) for number in span)
    for token in tokens:
        if token.type == tokenize.COMMENT:
            prose.setdefault(token.start[0], token.string.lstrip("#").lstrip())
    return [(number, _mask(prose[number])) for number in sorted(prose)]


def _allowed(text: str, span: tuple[int, int]) -> bool:
    """Say whether a label match sits inside a standard collocation."""
    lowered = text.lower()
    return any(
        found.start() <= span[0] and span[1] <= found.end()
        for phrase in ALLOWED
        for found in re.finditer(re.escape(phrase), lowered)
    )


def _waived(lines: Sequence[str], number: int, rule: str) -> bool:
    for index in (number - 1, number - 2):
        if not 0 <= index < len(lines):
            continue
        found = WAIVER.search(lines[index])
        if found and {rule, "all"} & set(re.split(r"[,\s]+", found.group(1))):
            return True
    return False


def scan(source: str, path: str, rules: Sequence[Rule]) -> list[Hit]:
    """Report every hit in one file's prose, honouring allowlist and waivers."""
    lines = source.splitlines()
    prose = python_prose(source) if path.endswith(".py") else markdown_prose(source)
    hits: list[Hit] = []
    for number, text in prose:
        for rule in rules:
            for found in rule.pattern.finditer(text):
                if rule.name == "codename-label" and _allowed(text, found.span()):
                    continue
                if _waived(lines, number, rule.name):
                    continue
                hits.append(Hit(path, number, rule.name, found.group().strip()))
    return hits


def collect(paths: Iterable[str], root: Path) -> list[Path]:
    """Expand the scanned roots into Markdown and Python files, less EXCLUDED."""
    files: set[Path] = set()
    for item in paths:
        target = root / item
        if target.is_file():
            files.add(target)
            continue
        for suffix in ("*.md", "*.py"):
            files.update(target.rglob(suffix))
    skipped = tuple(root / item for item in EXCLUDED)
    return sorted(path for path in files if not any(path.is_relative_to(x) for x in skipped))


def run(paths: Iterable[str], rules: Sequence[Rule], root: Path = ROOT) -> list[Hit]:
    hits: list[Hit] = []
    for path in collect(paths, root):
        named = path.relative_to(root) if path.is_relative_to(root) else path
        hits += scan(path.read_text(encoding="utf-8"), named.as_posix(), rules)
    return sorted(hits, key=lambda hit: (hit.path, hit.line, hit.rule))


def tally(hits: Iterable[Hit], rules: Sequence[Rule]) -> dict[str, int]:
    counted = Counter(hit.rule for hit in hits)
    return {rule.name: counted.get(rule.name, 0) for rule in rules}


def ratchet(counts: dict[str, int], baseline: dict[str, int]) -> dict[str, str]:
    """Describe every rule whose count rose above its committed baseline."""
    over = {}
    for name, count in counts.items():
        ceiling = int(baseline.get(name, 0))
        if count > ceiling:
            over[name] = f"{name}: {count} above the baseline {ceiling} by {count - ceiling}"
    return over


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Report prose constructions that need the development history."
    )
    names = [rule.name for rule in RULES]
    parser.add_argument("--paths", nargs="+", default=list(DEFAULT_PATHS))
    parser.add_argument("--rule", action="append", choices=names, help="repeatable")
    parser.add_argument("--baseline", help="fail only where a count rose above this file")
    parser.add_argument("--write-baseline", help="write the current counts to this file")
    args = parser.parse_args(argv)

    rules = [rule for rule in RULES if not args.rule or rule.name in args.rule]
    hits = run(args.paths, rules)
    counts = tally(hits, rules)

    if args.write_baseline:
        Path(args.write_baseline).write_text(json.dumps(counts, indent=2) + "\n")

    baseline = json.loads(Path(args.baseline).read_text()) if args.baseline else {}
    over = ratchet(counts, baseline) if args.baseline else {}
    # The plain report prints everything. The ratchet prints a sample of the
    # rules that rose, because a rule with a standing backlog would otherwise
    # bury the run in hits that were already there.
    loud, cap = (set(over), SAMPLE) if args.baseline else (set(counts), len(hits) + 1)
    shown: Counter[str] = Counter()
    for hit in hits:
        if hit.rule in loud and shown[hit.rule] < cap:
            shown[hit.rule] += 1
            print(f"{hit.path}:{hit.line}: {hit.rule} {hit.text}")

    width = max(len(name) for name in counts)
    for name, count in counts.items():
        limit = f"  (baseline {baseline.get(name, 0)})" if args.baseline else ""
        severity = next(rule.severity for rule in rules if rule.name == name)
        print(f"{name:<{width}}  {count:>5}  {severity}{limit}")

    if over:
        report = "\n".join(f"  {line}" for line in over.values())
        hint = "  scan your own files: --paths <path> [--rule <rule-id>]"
        print(f"prose lint failed:\n{report}\n{hint}", file=sys.stderr)
        return 1
    failing = sum(counts[rule.name] for rule in rules if rule.severity == FAILURE)
    quiet = args.baseline or args.write_baseline
    return 1 if failing and not quiet else 0


if __name__ == "__main__":
    raise SystemExit(main())
