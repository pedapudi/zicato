"""Measure the console's implementation the way `docs/design/CONSOLE-SIMPLIFICATION.md` does.

The console is the browser dashboard under ``src/zicato/dashboard/static``.
Each subcommand is one of the passes that document's findings rest on, so
a number in it can be re-derived after the tree moves:

``clones``
    Token-shingle clone finder over the JavaScript modules. Every line is
    normalised (whitespace stripped, string literals and numbers collapsed,
    blank, comment-only and brace-only lines dropped), every window of
    ``--window`` consecutive lines is hashed, and a window seen in two places
    is a hit. Overlapping hits merge into maximal runs. ``--loose`` maps every
    identifier that is not a JavaScript keyword to one placeholder.
``exports``
    For every export of one module (``svg.js`` by default), the number of
    call sites in the other modules, whether the module uses it itself, and
    how many test lines name it.
``routes``
    Every ``/api/`` path the dashboard serves against every ``/api/`` string
    the browser console and the terminal console read, both normalised so a
    path parameter and a template hole compare equal.
``css``
    The five-source liveness pass over the stylesheet: every class in a
    selector is looked up in the JavaScript string literals, ``index.html``,
    the Python sources, the dynamic class prefixes the JavaScript assembles
    (``'dn-turn-' + role``, `` `dt-glyph-${kind}` ``), and the test files. A
    class found in none of the first four is unreferenced; a rule whose
    every selector names an unreferenced class can never match and is dead.
    Test references are reported separately, because a test that names a
    class the console never emits pins nothing.
``assertions``
    Every assertion line of the browser tests by what it names: a class
    literal, a DOM property, a ``querySelector`` call, or none of these.

Every subcommand prints a table and, with ``--json``, one JSON object whose
keys are stable for the test in ``tools/test_console_measure.py``. The tool
reads the tree only and uses the standard library only.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = Path("src/zicato/dashboard/static")
JS_DIR = STATIC / "js"
TEST_DIR = STATIC / "test"
ENTRY = STATIC / "console.js"
INDEX_HTML = STATIC / "index.html"
STYLESHEETS = (STATIC / "css" / "console.css", STATIC / "style.css")
DASHBOARD_PY = Path("src/zicato/dashboard")
TUI_DIR = Path("src/zicato/tui")
PY_ROOT = Path("src/zicato")

CLASS_TOKEN = re.compile(r"[A-Za-z_][\w-]*")
# A dynamic prefix names a console class family (`dn-` or `dt-`) and at least
# one more segment: `'dn-turn-' + role` completes a class, `'dn-' + kind` names
# every class in the family and explains nothing.
DYNAMIC_PREFIX = re.compile(r"(?:dn|dt)-[\w-]+-")
CLASS_FAMILY = re.compile(r"(?:dn|dt)-")

JS_KEYWORDS = frozenset(
    "await break case catch class const continue debugger default delete do else "
    "export extends false finally for function if import in instanceof let new null "
    "return super switch this throw true try typeof undefined var void while with "
    "yield of async static get set".split()
)
# A `/` after one of these characters (or after one of the keywords below) opens
# a regular-expression literal rather than a division.
REGEX_PRECEDERS = set("(,=:[!&|?{};+-*%<>~^")
REGEX_KEYWORDS = frozenset("return typeof case do else in of throw".split())


# ── file walking ─────────────────────────────────────────────────────────


def _js_modules(root: Path) -> list[Path]:
    files = sorted((root / JS_DIR).rglob("*.js"))
    entry = root / ENTRY
    if entry.exists():
        files.append(entry)
    return files


def _test_files(root: Path) -> list[Path]:
    return sorted((root / TEST_DIR).rglob("*.mjs"))


def _rel(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


# ── JavaScript string literals ───────────────────────────────────────────


@dataclass
class JsString:
    """One string literal: its text with template holes replaced by ``${}``."""

    text: str
    line: int
    holes: int = 0
    # Text of every expression inside a template hole, for the prefix rule.
    hole_exprs: list[str] = field(default_factory=list)
    # The source that follows the closing quote, for the `'x-' + y` rule.
    following: str = ""


def js_strings(source: str) -> list[JsString]:
    """Every string literal in a JavaScript source, comments and regexes skipped."""
    out: list[JsString] = []
    i, n, line = 0, len(source), 1
    last_sig = ""  # the last significant character, for the regex rule
    last_word = ""

    def scan_template(start: int, depth_line: int) -> tuple[int, JsString]:
        """Scan a template literal from the backtick at ``start``; return the index after it."""
        j = start + 1
        text: list[str] = []
        holes = 0
        exprs: list[str] = []
        cur_line = depth_line
        while j < n:
            c = source[j]
            if c == "\\":
                text.append(source[j : j + 2])
                j += 2
                continue
            if c == "`":
                return j + 1, JsString("".join(text), depth_line, holes, exprs)
            if c == "$" and source.startswith("${", j):
                depth = 1
                k = j + 2
                expr_start = k
                while k < n and depth:
                    ch = source[k]
                    if ch == "`":
                        k, inner = scan_template(k, cur_line)
                        out.append(inner)
                        continue
                    if ch in "'\"":
                        k, inner = scan_quoted(k, cur_line)
                        out.append(inner)
                        continue
                    if ch == "{":
                        depth += 1
                    elif ch == "}":
                        depth -= 1
                    elif ch == "\n":
                        cur_line += 1
                    k += 1
                exprs.append(source[expr_start : k - 1])
                text.append("${}")
                holes += 1
                j = k
                continue
            if c == "\n":
                cur_line += 1
            text.append(c)
            j += 1
        return n, JsString("".join(text), depth_line, holes, exprs)

    def scan_quoted(start: int, at_line: int) -> tuple[int, JsString]:
        quote = source[start]
        j = start + 1
        text: list[str] = []
        while j < n:
            c = source[j]
            if c == "\\":
                text.append(source[j : j + 2])
                j += 2
                continue
            if c == quote:
                return j + 1, JsString("".join(text), at_line)
            if c == "\n":
                break
            text.append(c)
            j += 1
        return j, JsString("".join(text), at_line)

    while i < n:
        c = source[i]
        if c == "\n":
            line += 1
            i += 1
            continue
        if c == "/" and source.startswith("//", i):
            end = source.find("\n", i)
            i = n if end < 0 else end
            continue
        if c == "/" and source.startswith("/*", i):
            end = source.find("*/", i + 2)
            end = n if end < 0 else end + 2
            line += source.count("\n", i, end)
            i = end
            continue
        if c in "'\"":
            i, item = scan_quoted(i, line)
            item.following = source[i : i + 12]
            out.append(item)
            last_sig, last_word = "'", ""
            continue
        if c == "`":
            i, item = scan_template(i, line)
            line = item.line + item.text.count("\n") + sum(e.count("\n") for e in item.hole_exprs)
            item.following = source[i : i + 12]
            out.append(item)
            last_sig, last_word = "'", ""
            continue
        if c == "/" and (
            last_sig == "" or last_sig in REGEX_PRECEDERS or last_word in REGEX_KEYWORDS
        ):
            j = i + 1
            in_class = False
            while j < n and source[j] != "\n":
                ch = source[j]
                if ch == "\\":
                    j += 2
                    continue
                if ch == "[":
                    in_class = True
                elif ch == "]":
                    in_class = False
                elif ch == "/" and not in_class:
                    break
                j += 1
            i = j + 1
            last_sig, last_word = ")", ""
            continue
        if c.isalnum() or c in "_$":
            m = re.match(r"[\w$]+", source[i:])
            word = m.group(0) if m else c
            i += len(word)
            last_word = word if word in REGEX_KEYWORDS else ""
            last_sig = "a"
            continue
        if not c.isspace():
            last_sig = c
            last_word = ""
        i += 1
    return out


def _class_tokens(text: str) -> set[str]:
    return set(CLASS_TOKEN.findall(text))


def _dynamic_prefixes(strings: Iterable[JsString]) -> tuple[set[str], set[str]]:
    """Class prefixes the JavaScript completes at run time.

    A literal whose last whitespace-separated token ends in ``-`` and is
    followed by ``+`` (``'dn-turn dn-turn-' + role``) or a template hole
    (`` `dt-glyph-${kind}` ``) yields that token as a prefix. A token that is
    only a family name (``dn-`` or ``dt-``, as in `` `dn-pill dn-${verdict}` ``)
    is returned separately: it names every class of the family, so it explains
    a class only together with the value vocabulary the caller supplies.
    """
    prefixes: set[str] = set()
    families: set[str] = set()
    for s in strings:
        parts = s.text.split("${}")
        for idx, part in enumerate(parts):
            if not part.endswith("-"):
                continue
            last = part.split()[-1] if part.split() else ""
            followed_by_hole = idx < len(parts) - 1
            followed_by_plus = idx == len(parts) - 1 and s.following.lstrip().startswith("+")
            if not (followed_by_hole or followed_by_plus):
                continue
            if DYNAMIC_PREFIX.fullmatch(last):
                prefixes.add(last)
            elif CLASS_FAMILY.fullmatch(last):
                families.add(last)
    return prefixes, families


# ── stylesheet parsing ───────────────────────────────────────────────────


@dataclass
class CssRule:
    file: str
    start: int  # 1-based line of the first selector
    end: int  # 1-based line of the closing brace
    selectors: list[str]
    classes: set[str]


def _strip_css_comments(text: str) -> str:
    """Blank out comments in place so line numbers survive."""

    def blank(m: re.Match[str]) -> str:
        return re.sub(r"[^\n]", " ", m.group(0))

    return re.sub(r"/\*.*?\*/", blank, text, flags=re.S)


def css_rules(text: str, file: str) -> list[CssRule]:
    """Every style rule (selector list plus declaration block) in a stylesheet.

    Conditional at-rules (``@media``, ``@supports``) are descended into;
    ``@font-face`` and ``@keyframes`` blocks hold no selectors and are skipped.
    """
    clean = _strip_css_comments(text)
    rules: list[CssRule] = []
    i, n = 0, len(clean)
    depth_stack: list[str] = []  # 'cond' for a descended at-rule

    def line_of(pos: int) -> int:
        return clean.count("\n", 0, pos) + 1

    while i < n:
        c = clean[i]
        if c.isspace():
            i += 1
            continue
        if c == "}":
            if depth_stack:
                depth_stack.pop()
            i += 1
            continue
        brace = clean.find("{", i)
        if brace < 0:
            break
        head = clean[i:brace].strip()
        if head.startswith("@"):
            name = head.split(None, 1)[0].lower()
            if name in ("@media", "@supports", "@layer", "@container"):
                depth_stack.append("cond")
                i = brace + 1
                continue
            # a block with no selectors: skip it whole, nested braces included
            depth = 0
            j = brace
            while j < n:
                if clean[j] == "{":
                    depth += 1
                elif clean[j] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            i = j + 1
            continue
        close = clean.find("}", brace)
        if close < 0:
            break
        selectors = [s.strip() for s in head.split(",") if s.strip()]
        classes = set()
        for sel in selectors:
            classes.update(m.group(1) for m in re.finditer(r"\.([A-Za-z_][\w-]*)", sel))
        rules.append(CssRule(file, line_of(i), line_of(close), selectors, classes))
        i = close + 1
    return rules


def _selector_classes(sel: str) -> set[str]:
    return {m.group(1) for m in re.finditer(r"\.([A-Za-z_][\w-]*)", sel)}


# ── the css subcommand ───────────────────────────────────────────────────


def measure_css(root: Path) -> dict[str, object]:
    js_refs: set[str] = set()
    all_js_strings: list[JsString] = []
    for path in _js_modules(root):
        strings = js_strings(path.read_text())
        all_js_strings.extend(strings)
        for s in strings:
            js_refs.update(_class_tokens(s.text))
    html_refs = (
        _class_tokens((root / INDEX_HTML).read_text()) if (root / INDEX_HTML).exists() else set()
    )
    py_refs: set[str] = set()
    for path in sorted((root / PY_ROOT).rglob("*.py")):
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                py_refs.update(_class_tokens(node.value))
    test_refs: set[str] = set()
    for path in _test_files(root):
        for s in js_strings(path.read_text()):
            test_refs.update(_class_tokens(s.text))
    prefixes, families = _dynamic_prefixes(all_js_strings)
    # The value vocabulary a family-only site can complete: every string
    # literal that is one bare word (`'pending'`, `'deferred'`).
    words = {s.text for s in all_js_strings if re.fullmatch(r"[a-z][\w]*", s.text)}

    rules: list[CssRule] = []
    for sheet in STYLESHEETS:
        if (root / sheet).exists():
            rules.extend(css_rules((root / sheet).read_text(), sheet.as_posix()))
    classes = sorted({c for r in rules for c in r.classes})
    static_refs = js_refs | html_refs | py_refs
    no_static = [c for c in classes if c not in static_refs]
    by_prefix = [
        c
        for c in no_static
        if any(c.startswith(p) and c != p for p in prefixes)
        or any(c.startswith(f) and c[len(f) :] in words for f in families)
    ]
    dead = [c for c in no_static if c not in by_prefix]
    dead_set = set(dead)
    test_only = [c for c in dead if c in test_refs]
    dead_rules = [
        r
        for r in rules
        if r.selectors and all(_selector_classes(s) & dead_set for s in r.selectors)
    ]
    dead_rule_lines = sum(r.end - r.start + 1 for r in dead_rules)
    js_site_counts: Counter[str] = Counter()
    for s in all_js_strings:
        for c in _class_tokens(s.text):
            js_site_counts[c] += 1
    single_site = sum(1 for c in classes if js_site_counts.get(c) == 1)
    return {
        "distinct_classes": len(classes),
        "rules": len(rules),
        "no_static_reference": len(no_static),
        "explained_by_prefix": len(by_prefix),
        "unreferenced": len(dead),
        "unreferenced_test_only": len(test_only),
        "dead_rules": len(dead_rules),
        "dead_rule_lines": dead_rule_lines,
        "single_js_site": single_site,
        "prefixes": sorted(prefixes),
        "families": sorted(families),
        "unreferenced_classes": dead,
        "test_only_classes": test_only,
        "dead_rule_spans": [
            {"file": r.file, "start": r.start, "end": r.end, "selectors": r.selectors}
            for r in dead_rules
        ],
    }


def print_css(result: dict[str, object]) -> None:
    rows = [
        ("distinct class selectors", result["distinct_classes"]),
        ("rules", result["rules"]),
        ("classes with no static reference in JS, HTML or Python", result["no_static_reference"]),
        ("of those, explained by a dynamic prefix", result["explained_by_prefix"]),
        ("classes with no static and no dynamic reference", result["unreferenced"]),
        ("of those, named only by a test", result["unreferenced_test_only"]),
        ("rules made only of unreferenced classes", result["dead_rules"]),
        ("lines those rules hold", result["dead_rule_lines"]),
        ("classes referenced from one JS site only", result["single_js_site"]),
    ]
    for label, value in rows:
        print(f"{label:<58} {value:>6}")
    print(f"\ndynamic prefixes ({len(result['prefixes'])}): {' '.join(result['prefixes'])}")  # type: ignore[arg-type]
    print(f"family-only prefixes completed by a value: {' '.join(result['families']) or 'none'}")  # type: ignore[arg-type]
    print("\nunreferenced classes:")
    for c in result["unreferenced_classes"]:  # type: ignore[union-attr]
        mark = "  (test-only)" if c in result["test_only_classes"] else ""  # type: ignore[operator]
        print(f"  {c}{mark}")
    print("\ndead rules:")
    for span in result["dead_rule_spans"]:  # type: ignore[union-attr]
        print(f"  {span['file']}:{span['start']}-{span['end']}  {', '.join(span['selectors'])}")


# ── the exports subcommand ───────────────────────────────────────────────


def _exports_of(source: str) -> list[str]:
    names: list[str] = []
    for m in re.finditer(
        r"^export\s+(?:async\s+)?(?:function\*?|const|let|var|class)\s+([A-Za-z_$][\w$]*)",
        source,
        re.M,
    ):
        names.append(m.group(1))
    for m in re.finditer(r"^export\s*\{([^}]*)\}", source, re.M):
        for part in m.group(1).split(","):
            part = part.strip()
            if part:
                names.append(part.split(" as ")[-1].strip())
    return names


def measure_exports(root: Path, module: Path) -> dict[str, object]:
    source = (root / module).read_text()
    names = _exports_of(source)
    stem = module.stem
    modules = [p for p in _js_modules(root) if p != root / module]
    outside: Counter[str] = Counter()
    modules_using: dict[str, set[str]] = defaultdict(set)
    for path in modules:
        text = path.read_text()
        text_no_comments = re.sub(r"/\*.*?\*/|//[^\n]*", "", text, flags=re.S)
        aliases = set(
            re.findall(
                r"import\s+\*\s+as\s+(\w+)\s+from\s+['\"][^'\"]*" + re.escape(stem) + r"\.js['\"]",
                text,
            )
        )
        named: set[str] = set()
        for m in re.finditer(
            r"import\s*\{([^}]*)\}\s*from\s*['\"][^'\"]*" + re.escape(stem) + r"\.js['\"]", text
        ):
            named.update(
                p.strip().split(" as ")[-1].strip() for p in m.group(1).split(",") if p.strip()
            )
        body = re.sub(r"import[^;]*;", "", text_no_comments)
        for name in names:
            count = 0
            for alias in aliases:
                count += len(
                    re.findall(r"\b" + re.escape(alias) + r"\." + re.escape(name) + r"\b", body)
                )
            if name in named:
                count += len(re.findall(r"(?<![.\w$])" + re.escape(name) + r"\b", body))
            if count:
                outside[name] += count
                modules_using[name].add(_rel(root, path))
    own = re.sub(r"/\*.*?\*/|//[^\n]*", "", source, flags=re.S)
    inside: dict[str, bool] = {}
    for name in names:
        uses = len(re.findall(r"(?<![.\w$])" + re.escape(name) + r"\b", own))
        inside[name] = uses > 1  # the declaration itself is one
    tests: Counter[str] = Counter()
    for path in _test_files(root):
        text = re.sub(r"/\*.*?\*/|//[^\n]*", "", path.read_text(), flags=re.S)
        for name in names:
            tests[name] += len(re.findall(r"(?<![\w$])" + re.escape(name) + r"\b", text))
    rows = [
        {
            "name": name,
            "outside": outside.get(name, 0),
            "modules": sorted(modules_using.get(name, ())),
            "inside": inside[name],
            "tests": tests.get(name, 0),
        }
        for name in names
    ]
    dead = [r["name"] for r in rows if not r["outside"] and not r["inside"]]
    return {"module": module.as_posix(), "exports": len(names), "dead": dead, "rows": rows}


def print_exports(result: dict[str, object]) -> None:
    print(f"{result['module']}: {result['exports']} exports")
    print(f"{'export':<28} {'outside':>7} {'inside':>6} {'tests':>5}  modules")
    for r in sorted(result["rows"], key=lambda r: (r["outside"], r["name"])):  # type: ignore[call-overload]
        print(
            f"{r['name']:<28} {r['outside']:>7} {str(r['inside']).lower():>6} "
            f"{r['tests']:>5}  {len(r['modules'])}"
        )
    print(f"\nno caller outside and unused inside: {', '.join(result['dead']) or 'none'}")  # type: ignore[arg-type]


# ── the routes subcommand ────────────────────────────────────────────────


def _normalise_route(path: str) -> str:
    path = path.split("?")[0]
    path = path.replace("${}", "{x}")
    return re.sub(r"\{[^}]*\}", "{x}", path)


def _route_matches(route: str, read: str) -> bool:
    """A read literal names a served route when the segments agree; a hole matches one segment."""
    a, b = route.split("/"), read.split("/")
    return len(a) == len(b) and all(x == y or y == "{x}" for x, y in zip(a, b, strict=True))


CONCAT_PIECE = re.compile(r"""\s*\+\s*(?:'([^']*)'|"([^"]*)"|([^+;,'"]+))""")
ROUTE_LITERAL = re.compile(r"""['"](/api/[^'"]*|/events)['"]""")


def _concatenated_routes(source: str) -> Iterator[str]:
    """Routes assembled by `+` on one line, each non-literal piece read as a hole."""
    clean = re.sub(r"/\*.*?\*/|//[^\n]*", "", source, flags=re.S)
    for line in clean.splitlines():
        for m in ROUTE_LITERAL.finditer(line):
            text = m.group(1)
            pos = m.end()
            while True:
                piece = CONCAT_PIECE.match(line, pos)
                if not piece:
                    break
                lit = piece.group(1) if piece.group(1) is not None else piece.group(2)
                text += lit if lit is not None else "${}"
                pos = piece.end()
            yield text


def _served_routes(root: Path) -> set[str]:
    routes: set[str] = set()
    for name in ("server.py", "endpoints.py"):
        path = root / DASHBOARD_PY / name
        if not path.exists():
            continue
        for m in re.finditer(r"\"(/api/[^\"]+)\"", path.read_text()):
            routes.add(_normalise_route(m.group(1)))
        if '"/events"' in path.read_text():
            routes.add("/events")
    return routes


def _python_route_strings(path: Path) -> Iterator[str]:
    try:
        tree = ast.parse(path.read_text())
    except SyntaxError:
        return
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.value
        elif isinstance(node, ast.JoinedStr):
            parts = []
            for v in node.values:
                if isinstance(v, ast.Constant):
                    parts.append(str(v.value))
                else:
                    parts.append("${}")
            yield "".join(parts)


def measure_routes(root: Path) -> dict[str, object]:
    served = _served_routes(root)
    browser: set[str] = set()
    for path in _js_modules(root):
        text = path.read_text()
        for s in js_strings(text):
            for m in re.finditer(r"(/api/[^\s'\"`]*|/events\b)", s.text):
                browser.add(_normalise_route(m.group(1)))
        browser.update(_normalise_route(r) for r in _concatenated_routes(text))
    tui: set[str] = set()
    for path in sorted((root / TUI_DIR).rglob("*.py")):
        for value in _python_route_strings(path):
            for m in re.finditer(r"(/api/[^\s'\"]*|/events\b)", value):
                tui.add(_normalise_route(m.group(1)))

    def matches(route: str, reads: set[str]) -> bool:
        return any(_route_matches(route, r) for r in reads)

    read_by_browser = sorted(r for r in served if matches(r, browser))
    read_by_tui = sorted(r for r in served if matches(r, tui))
    unread = sorted(r for r in served if r not in read_by_browser and r not in read_by_tui)
    return {
        "served": len(served),
        "read_by_browser": len(read_by_browser),
        "read_by_tui": len(read_by_tui),
        "unread": len(unread),
        "unread_routes": unread,
        "tui_routes": read_by_tui,
    }


def print_routes(result: dict[str, object]) -> None:
    print(f"served routes                  {result['served']:>4}")
    print(f"read by the browser console    {result['read_by_browser']:>4}")
    print(f"read by the terminal console   {result['read_by_tui']:>4}")
    print(f"read by neither                {result['unread']:>4}")
    print("\nterminal console reads:")
    for r in result["tui_routes"]:  # type: ignore[union-attr]
        print(f"  {r}")
    print("\nread by neither:")
    for r in result["unread_routes"]:  # type: ignore[union-attr]
        print(f"  {r}")


# ── the assertions subcommand ────────────────────────────────────────────

ASSERT_CALL = re.compile(r"\bassert(?:Equal|Deep)?\s*\(")
CLASS_LITERAL = re.compile(r"['\"`][^'\"`]*\b(?:dn|dt)-[\w-]+")
DOM_PROPERTY = re.compile(
    r"\b(?:textContent|innerHTML|getAttribute|hasAttribute|tagName|classList|dataset|attributes)\b"
)


def classify_assertion(line: str) -> str:
    if CLASS_LITERAL.search(line):
        return "class_literal"
    if DOM_PROPERTY.search(line):
        return "dom_property"
    if "querySelector" in line:
        return "query_selector"
    return "other"


def measure_assertions(root: Path) -> dict[str, object]:
    counts: Counter[str] = Counter()
    files = 0
    for path in _test_files(root):
        if not path.name.endswith(".test.mjs"):
            continue
        files += 1
        for line in path.read_text().splitlines():
            if ASSERT_CALL.search(line):
                counts[classify_assertion(line)] += 1
    total = sum(counts.values())
    return {
        "files": files,
        "assertions": total,
        "class_literal": counts["class_literal"],
        "dom_property": counts["dom_property"],
        "query_selector": counts["query_selector"],
        "other": counts["other"],
    }


def print_assertions(result: dict[str, object]) -> None:
    total = int(result["assertions"])  # type: ignore[call-overload]
    print(f"{result['files']} test files, {total} assertion lines")
    for key, label in (
        ("class_literal", "a dn-/dt- class literal"),
        ("dom_property", "a DOM property"),
        ("query_selector", "querySelector inside the assertion"),
        ("other", "none of these"),
    ):
        n = int(result[key])  # type: ignore[call-overload]
        share = 100 * n / total if total else 0
        print(f"  {label:<40} {n:>6}  {share:5.1f} %")


# ── the clones subcommand ────────────────────────────────────────────────

STRING_LITERAL = re.compile(r"'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"|`(?:\\.|[^`\\])*`")
NUMBER = re.compile(r"\b\d+(?:\.\d+)?\b")
IDENT = re.compile(r"\b[A-Za-z_$][\w$]*\b")


def normalise_line(line: str, loose: bool) -> str | None:
    s = line.strip()
    if not s or s.startswith("//") or s.startswith("/*") or s.startswith("*"):
        return None
    s = STRING_LITERAL.sub('""', s)
    s = NUMBER.sub("0", s)
    s = re.sub(r"\s+", "", s)
    if not s.strip("{}();,"):
        return None
    if loose:
        s = IDENT.sub(lambda m: m.group(0) if m.group(0) in JS_KEYWORDS else "_", s)
    return s


def _merge_runs(
    runs: list[tuple[str, int, int, str, int, int]],
) -> list[tuple[str, int, int, str, int, int]]:
    """Merge diagonal runs of one file pair whose two intervals both overlap.

    A self-repeating passage (a list of similar lines) matches itself at
    every offset; those diagonals cover one region and count as one run,
    sized by the union of the duplicate side.
    """
    merged: list[tuple[str, int, int, str, int, int]] = []
    for fa, a, length, fb, b0, b1 in sorted(runs):
        a0, a1 = a, a + length
        for idx, (ga, ma0, ma1, gb, mb0, mb1) in enumerate(merged):
            if (ga, gb) == (fa, fb) and a0 < ma1 and ma0 < a1 and b0 < mb1 and mb0 < b1:
                merged[idx] = (fa, min(a0, ma0), max(a1, ma1), fb, min(b0, mb0), max(b1, mb1))
                break
        else:
            merged.append((fa, a0, a1, fb, b0, b1))
    return merged


def measure_clones(root: Path, window: int, loose: bool) -> dict[str, object]:
    lines_by_file: dict[str, list[tuple[int, str]]] = {}
    for path in _js_modules(root):
        kept = []
        for no, raw in enumerate(path.read_text().splitlines(), 1):
            norm = normalise_line(raw, loose)
            if norm is not None:
                kept.append((no, norm))
        lines_by_file[_rel(root, path)] = kept
    windows: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for file, kept in lines_by_file.items():
        for idx in range(len(kept) - window + 1):
            digest = hashlib.blake2b(
                "\n".join(t for _, t in kept[idx : idx + window]).encode(), digest_size=16
            ).hexdigest()
            windows[digest].append((file, idx))
    # pair each later occurrence with the first; merge consecutive windows into runs
    pair_windows: dict[tuple[str, str], list[tuple[int, int]]] = defaultdict(list)
    for occurrences in windows.values():
        if len(occurrences) < 2:
            continue
        first = occurrences[0]
        for other in occurrences[1:]:
            pair_windows[(first[0], other[0])].append((first[1], other[1]))
    runs: list[tuple[str, int, int, str, int, int]] = []
    for (fa, fb), starts in pair_windows.items():
        starts.sort()
        run_a, run_b, length = starts[0][0], starts[0][1], window
        for a, b in starts[1:]:
            if a == run_a + length - window + 1 and b == run_b + length - window + 1:
                length += 1
            else:
                runs.append((fa, run_a, length, fb, run_b, run_b + length))
                run_a, run_b, length = a, b, window
        runs.append((fa, run_a, length, fb, run_b, run_b + length))
    merged = _merge_runs(runs)
    duplicate_lines = sum(b1 - b0 for _, _, _, _, b0, b1 in merged)
    per_file: Counter[str] = Counter()
    for fa, a0, a1, fb, b0, b1 in merged:
        per_file[fa] += a1 - a0
        per_file[fb] += b1 - b0
    detail = []
    for fa, a0, a1, fb, b0, b1 in sorted(merged, key=lambda r: -(r[5] - r[4])):
        la = lines_by_file[fa][a0][0], lines_by_file[fa][a1 - 1][0]
        lb = lines_by_file[fb][b0][0], lines_by_file[fb][b1 - 1][0]
        detail.append(
            {"a": f"{fa}:{la[0]}-{la[1]}", "b": f"{fb}:{lb[0]}-{lb[1]}", "lines": b1 - b0}
        )
    return {
        "window": window,
        "loose": loose,
        "pairs": len(merged),
        "duplicate_lines": duplicate_lines,
        "per_file": dict(per_file.most_common()),
        "runs": detail,
    }


def print_clones(result: dict[str, object]) -> None:
    mode = "identifiers replaced" if result["loose"] else "exact text"
    print(
        f"{mode}, window {result['window']}: {result['pairs']} clone pairs, "
        f"{result['duplicate_lines']} lines in the duplicate copies"
    )
    print("\nper file (pair-ends):")
    for file, n in list(result["per_file"].items())[:12]:  # type: ignore[union-attr]
        print(f"  {n:>5}  {file}")
    print("\nlongest runs:")
    for r in result["runs"][:15]:  # type: ignore[index]
        print(f"  {r['lines']:>4}  {r['a']}  ~  {r['b']}")


# ── entry point ──────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--root", type=Path, default=ROOT, help="repository root to measure")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", dest="as_json", help="print one JSON object")
    sub = parser.add_subparsers(dest="pass_name", required=True)
    clones = sub.add_parser(
        "clones", parents=[common], help="token-shingle clone finder over the browser code"
    )
    clones.add_argument("--window", type=int, default=8)
    clones.add_argument("--loose", action="store_true", help="map identifiers to one placeholder")
    exports = sub.add_parser(
        "exports", parents=[common], help="call sites per export of one module"
    )
    exports.add_argument("--module", type=Path, default=JS_DIR / "svg.js")
    sub.add_parser(
        "routes", parents=[common], help="served routes against the routes the consoles read"
    )
    sub.add_parser("css", parents=[common], help="five-source class liveness over the stylesheet")
    sub.add_parser(
        "assertions", parents=[common], help="browser-test assertion lines by what they name"
    )
    args = parser.parse_args(argv)
    root = args.root.resolve()
    if args.pass_name == "clones":
        result = measure_clones(root, args.window, args.loose)
        printer = print_clones
    elif args.pass_name == "exports":
        result = measure_exports(root, args.module)
        printer = print_exports
    elif args.pass_name == "routes":
        result = measure_routes(root)
        printer = print_routes
    elif args.pass_name == "css":
        result = measure_css(root)
        printer = print_css
    else:
        result = measure_assertions(root)
        printer = print_assertions
    if args.as_json:
        json.dump(result, sys.stdout, indent=1)
        print()
    else:
        printer(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
