"""Cheap, stdlib-only structural checks for non-Python mutable files.

The mutation surface spans any marker-annotated text file, not only
``*.py`` (see :mod:`zicato.mutation.enumerator`). Python files get a real
post-apply gate — :func:`ast.parse` over the whole snapshot — because a
non-parsing module makes the snapshot unimportable. Nothing equivalent
exists for "text": a markdown prompt or a shell script has no cheap,
dependency-free notion of "still valid".

One format is the exception, and only because the parser is already in the
standard library and costs nothing to run: ``.toml`` (:mod:`tomllib`).
This module is the single registry for that check so the applier's gate
and the contract pre-flight's synthetic degradation cannot drift apart —
pre-flight MUST be able to produce a worsening that still passes the gate,
or its probe would be rejected as a malformed patch instead of measured as
a degradation.

Scope, stated honestly
----------------------

This is **best-effort**, not a safety guarantee:

* Only ``.toml`` is checked. YAML is absent because no YAML parser is a
  zicato runtime dependency, and adding one to gate a best-effort check is
  not a trade worth making. **JSON** is absent for a sharper reason: JSON
  has no comment syntax, so a strict ``.json`` file cannot host a marker
  in the first place (which is why ``.json`` is not in
  :data:`~zicato.mutation.enumerator.TEXT_FILE_SUFFIXES` either) — and the
  comment-bearing dialects ``.jsonc`` / ``.json5``, which CAN host one,
  are by construction not parseable by :func:`json.loads`. A gate there
  would reject every valid file.
* The check answers "does this file still parse", never "does this file
  still mean the right thing". A patch that turns ``timeout = 30`` into
  ``timeout = 0`` passes.
* The applier runs the check only against *whole-file* (``kind="file"``)
  patches on files that parsed BEFORE the patch landed — see
  :func:`zicato.mutation.applier._format_gate_baseline` and
  :func:`zicato.mutation.applier.apply_patches`. A region
  (``kind="code"``) patch rewrites a fragment whose syntactic
  self-containment is the operator's marker placement, not the patch's
  doing, so attributing a parse failure there to the proposer would
  reject legitimate edits.
"""

from __future__ import annotations

import tomllib
from collections.abc import Callable
from pathlib import Path

#: Parser per checkable suffix. Each callable raises on malformed input.
_CHECKERS: dict[str, Callable[[str], object]] = {
    ".toml": tomllib.loads,
}

#: Content that is *inert but still parseable* per checkable suffix. The
#: contract pre-flight's whole-file degradation uses this so its synthetic
#: worsening probe survives the applier's format gate — reversing a TOML
#: document (pre-flight's generic degradation) would not. Every suffix in
#: :data:`_CHECKERS` MUST have an entry here; ``test_mutation_text_files``
#: pins that.
FORMAT_NEUTRAL_CONTENT: dict[str, str] = {
    ".toml": "# degraded by contract pre-flight (synthetic worsening probe)\n",
}


def is_format_checked(path: Path) -> bool:
    """Return ``True`` iff this module can structurally check ``path``."""

    return path.suffix in _CHECKERS


def format_problem(path: Path, text: str) -> str | None:
    """Return a problem string when ``text`` is malformed for ``path``'s format.

    Returns ``None`` when the format parses, when the suffix has no
    registered checker, or when the check is not applicable. Never raises:
    a checker that fails in an unexpected way is reported as a problem
    string like any other malformed input.
    """

    checker = _CHECKERS.get(path.suffix)
    if checker is None:
        return None
    try:
        checker(text)
    except Exception as exc:  # noqa: BLE001 — any parser failure is a problem
        return f"{path}: {path.suffix} no longer parses ({type(exc).__name__}: {exc})"
    return None


def file_format_problem(path: Path) -> str | None:
    """Read ``path`` and return its format problem, if any.

    An unreadable / undecodable file is itself reported as a problem so a
    patch that turned a checkable file into binary garbage does not slip
    through as "no check ran".
    """

    if not is_format_checked(path):
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return f"{path}: could not be read post-apply ({exc})"
    except UnicodeDecodeError as exc:
        return f"{path}: is no longer valid UTF-8 ({exc})"
    return format_problem(path, text)


__all__ = [
    "FORMAT_NEUTRAL_CONTENT",
    "file_format_problem",
    "format_problem",
    "is_format_checked",
]
