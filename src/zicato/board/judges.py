r"""Programmatic factory helpers for :class:`~zicato.core.JudgeSpec`.

Where :class:`zicato.board.predicates.Predicate` and
:class:`zicato.board.predicates.Rubric` are the OUTCOME-check families —
they grade a finished run — :class:`Judge` is the PROCESS-check family.
A process judge observes how a run unfolds *while it is still running*
and surfaces its verdict as a goldfive judge signal.

The module exposes a single namespaced factory class, :class:`Judge`,
modelled on :class:`~zicato.board.predicates.Predicate`: a class of
``staticmethod`` factories, never instantiated. Each factory returns a
fully-formed :class:`~zicato.core.JudgeSpec` ready to attach to a
:class:`~zicato.core.BoardEntry` via its ``judges`` field::

    Judge.custom("stays_on_task", "The agent never abandons the user's "
                 "stated goal.", severity=DriftSeverity.WARNING)
    Judge.python("no_pii", "myproject.judges.pii_guard",
                 severity=DriftSeverity.CRITICAL)

The ``name`` is mandatory and becomes goldfive's ``judge_name``, so it is
validated to be a stable slug-like identifier. ``severity`` is a
:class:`~zicato.core.DriftSeverity` member — a typed choice, never a bare
string. That enum is zicato's string mirror of ``goldfive.DriftSeverity``
(same names, values, and order), so boards written against either symbol
are accepted and produce the same wire token; the mirror is what keeps
these types importable without the optional ``goldfive`` extra.
"""

from __future__ import annotations

import re

from zicato.core.drift_kinds import DriftSeverity, is_drift_severity
from zicato.core.types import JudgeMode, JudgeSpec

# A judge name becomes goldfive's ``judge_name``; it must be a stable,
# filesystem- and wire-safe slug. Lowercase alphanumerics, underscores,
# and hyphens; must start with an alphanumeric. Kept deliberately strict
# so a typo (a stray space, an uppercase letter) fails loudly at
# authoring time rather than producing an unstable judge identity.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def _validate_name(name: str) -> str:
    """Validate a judge ``name`` is a stable slug-like identifier.

    Returns the name unchanged on success. Raises :class:`ValueError`
    with a precise message otherwise — the name is the judge's stable
    identity (goldfive's ``judge_name``) so a malformed one is a hard
    authoring error, not something to silently coerce.
    """
    if not isinstance(name, str) or not name:
        raise ValueError("Judge: 'name' is mandatory and must be a non-empty string")
    if not _NAME_RE.match(name):
        raise ValueError(
            f"Judge: name {name!r} is not a valid slug; use lowercase letters, "
            "digits, underscores, and hyphens, starting with a letter or digit"
        )
    return name


class Judge:
    """Static factory helpers for PROCESS checks (:class:`~zicato.core.JudgeSpec`).

    Two classmethods cover the two judge modes:

    * :meth:`custom` — an inline natural-language criterion.
    * :meth:`python` — a dotted import path to a Python process-judge
      callable.

    Both return a fully-formed :class:`~zicato.core.JudgeSpec`. The class
    itself is never instantiated; the helpers are ``staticmethod``s for
    the namespacing, mirroring :class:`zicato.board.predicates.Predicate`.
    """

    def __new__(cls) -> Judge:  # pragma: no cover — defensive
        raise TypeError("Judge is a namespace of static helpers; do not instantiate.")

    @staticmethod
    def custom(name: str, criterion: str, *, severity: DriftSeverity) -> JudgeSpec:
        """Build an inline-mode :class:`~zicato.core.JudgeSpec`.

        Parameters
        ----------
        name:
            Stable slug-like identifier for the judge — becomes
            goldfive's ``judge_name``. Mandatory; validated (lowercase
            alphanumerics, underscores, hyphens).
        criterion:
            The natural-language process criterion the judge evaluates
            the run against. Free-form prose; embedded verbatim by the
            runtime side. Must be non-empty.
        severity:
            Goldfive drift severity an adverse verdict is reported at.

        Returns
        -------
        JudgeSpec
            A :attr:`~zicato.core.JudgeMode.INLINE`-mode spec whose
            ``body`` is ``criterion``.
        """
        validated = _validate_name(name)
        if not isinstance(criterion, str) or not criterion.strip():
            raise ValueError(f"Judge.custom: 'criterion' for judge {validated!r} must be non-empty")
        if not is_drift_severity(severity):
            raise ValueError(
                f"Judge.custom: 'severity' for judge {validated!r} must be a "
                f"goldfive.DriftSeverity, got {type(severity).__name__}"
            )
        return JudgeSpec(name=validated, mode=JudgeMode.INLINE, body=criterion, severity=severity)

    @staticmethod
    def python(name: str, dotted_path: str, *, severity: DriftSeverity) -> JudgeSpec:
        """Build a python-mode :class:`~zicato.core.JudgeSpec`.

        Parameters
        ----------
        name:
            Stable slug-like identifier for the judge — becomes
            goldfive's ``judge_name``. Mandatory; validated.
        dotted_path:
            Dotted import path to a Python process-judge callable. Must
            carry a module component (``pkg.mod.attr``).
        severity:
            Goldfive drift severity an adverse verdict is reported at.

        Returns
        -------
        JudgeSpec
            A :attr:`~zicato.core.JudgeMode.PYTHON`-mode spec whose
            ``body`` is ``dotted_path``.
        """
        validated = _validate_name(name)
        if not isinstance(dotted_path, str) or "." not in dotted_path:
            raise ValueError(
                f"Judge.python: 'dotted_path' for judge {validated!r} must be a "
                "dotted path with a module component, e.g. 'pkg.mod.attr'"
            )
        if not is_drift_severity(severity):
            raise ValueError(
                f"Judge.python: 'severity' for judge {validated!r} must be a "
                f"goldfive.DriftSeverity, got {type(severity).__name__}"
            )
        return JudgeSpec(name=validated, mode=JudgeMode.PYTHON, body=dotted_path, severity=severity)


__all__ = ["Judge"]
