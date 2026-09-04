"""Guard: every scanned source file stays TEXT, so grep-shaped pins can see it.

A large share of this repo's completeness enforcement is grep-shaped. The
knob registry scans ``views/builder.js`` for ``runOp('<op>', {…})`` rows and
``test/builder.test.mjs`` for arg-level assertions
(``tests/test_knob_registry.py``); the GUI-coverage pin scans the same tree
for a control per builder op (``tests/test_builder_gui_coverage.py``); the
render-conformance sweeps that catch a served-but-unread field are grep
sweeps too.

Every one of those silently passes on a file grep declines to read. A single
NUL byte is enough: grep classifies the file as binary and reports nothing —
not an error, not a warning, just no matches, which reads exactly like
"no violations found". ``views/board.js`` carried a raw NUL for a
1011-line file (a cache key spelled with the literal control byte instead of
the ``'\\x00'`` escape, whose runtime value is identical), so every
grep-based check over that file had been vacuous. The escape is the fix; this
is the guard that keeps it one.

Deliberately narrow: NUL specifically, because that is what flips grep's
binary heuristic and therefore what turns a pin vacuous. This is not a
general encoding-style rule — legitimate non-ASCII text (the em dashes and
box-drawing characters this codebase's comments are full of) is untouched.
"""

from __future__ import annotations

from pathlib import Path

import zicato.dashboard as _dashboard_pkg

# Deliberately NOT resolved: an installed tree may stage the package as
# symlinks, and resolving would walk into whatever tree they point at
# instead of scanning the tree that is actually served.
STATIC_DIR = Path(str(_dashboard_pkg.__file__)).parent / "static"

#: Every text source under the dashboard's static tree. Deliberately the
#: WHOLE tree rather than the two directories today's pins happen to scan
#: (``js/`` and ``test/``): the guard should not need updating each time a
#: pin widens, and a file the pins skip today is one they may scan
#: tomorrow. This picks up the top-level ``console.js`` / ``index.html`` and
#: the ``css/`` stylesheets as well.
_SCANNED_SUFFIXES = (".js", ".mjs", ".css", ".html")


def _scanned_files() -> list[Path]:
    return sorted(
        path
        for path in STATIC_DIR.rglob("*")
        if path.is_file() and path.suffix in _SCANNED_SUFFIXES
    )


def test_the_scan_finds_files_to_check() -> None:
    """Sanity: the walk resolves, so the guard below is not vacuous itself.

    (A guard against vacuous guards. The failure mode being defended
    against is precisely "the check reported nothing and that looked like
    success", so the walk must be shown to have work to do.)
    """
    files = _scanned_files()
    assert len(files) > 50, f"the scanned-source walk found only {len(files)} files"
    names = {path.name for path in files}
    # One from each corner of the tree: a view, the file that carried the
    # byte, a node test, a stylesheet, and a top-level entry point.
    assert {
        "builder.js",
        "board.js",
        "builder.test.mjs",
        "console.css",
        "console.js",
    } <= names


def test_no_scanned_source_carries_a_nul_byte() -> None:
    """No scanned source may contain a NUL, which makes grep skip the file.

    A skipped file yields no matches, and no matches is indistinguishable
    from no violations — so one control byte silently disarms every
    grep-shaped pin over that file at once. Spell the value as an escape
    (``'\\x00'``) rather than embedding the byte: the string is identical at
    runtime and the file stays greppable.
    """
    offenders = []
    for path in _scanned_files():
        data = path.read_bytes()
        if b"\x00" in data:
            line = data[: data.index(b"\x00")].count(b"\n") + 1
            offenders.append(f"{path.relative_to(STATIC_DIR)}:{line}")
    assert not offenders, (
        "scanned source file(s) carry a raw NUL byte, which makes grep treat "
        f"them as binary and report NO matches: {offenders}. Every grep-shaped "
        "pin over these files is vacuous until this is fixed. Spell the byte "
        r"as the '\x00' escape — identical at runtime, and the file stays text."
    )
