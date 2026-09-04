"""Dotted-spec scoring PLUGINS (Seam 1 ``drift_reducer`` / Seam 2 ``scalar_fn``)
plus the ONE source-hashing mechanism every grading plugin shares.

zicato's two scoring seams route through the dispatchers in
:mod:`zicato.scoring.dispatch`, where the declarative transforms reshape the
built-in result. This module supplies the tier above those transforms: an
optional **operator-owned, contract-referenced** plugin per seam that WRAPS
the transformed-or-built-in value with arbitrary pure logic the declarative
registry cannot express — an F-beta score, a cost-aware penalty, or a
harmonic-looping curve as a roughly ten-line operator plugin (issue #19).

Design constraints:

* **Pure / deterministic / no-LLM / no-I/O / no-wall-clock.** A scoring plugin
  is a pure function over a frozen context, so re-scoring an epoch is
  reproducible. Unlike judges there is no evaluation callable to pass.
* **Same importer as predicates / judges.** Resolution goes through
  :func:`zicato.import_path.import_dotted_path`, so ``pkg.mod:fn`` and
  ``pkg.mod.fn`` resolve identically everywhere — and Seam 1's resolution works
  in the killable worker subprocess (it is just an import).
* **A plugin wraps the default rather than replacing it.** The context
  carries the built-in (or transformed) value as ``ctx.builtin_*``, so the
  plugin starts from the default. The dispatcher builds a context whose
  ``builtin_*`` is the POST-TRANSFORM value, so a plugin composes ON TOP of
  the declarative shape.
* **Fail-open (mirrors ``evaluate_judges``).** A plugin that raises, or returns
  a non-finite / non-numeric value, falls back to the pre-plugin value, logs at
  WARNING, and records the fallback in the provenance token so it is visible,
  never silent. There is **no wall-clock timeout**: these are declared pure
  CPU functions, a Python pure function cannot be cleanly interrupted, and a
  timeout would contradict the no-wall-clock contract. The try/except plus
  the finite-check IS the guard.

Source-hashing
--------------
The contract hash must roll when a plugin's BODY is edited, and not only when
its dotted-spec STRING changes; otherwise editing the looping curve in an
operator plugin would silently re-score an epoch. :func:`spec_with_source_hash` resolves
a dotted spec to the module that defines it and folds a SHA-256 of that module's
source into the canonical form. It is the ONE mechanism every grading plugin
shares: the contract canonicalizer applies it uniformly to the scoring
``scalar_fn`` / ``drift_reducer`` AND to the board's predicates / judges and the
``outcome_summarizer_spec``.
"""

from __future__ import annotations

import hashlib
import inspect
import logging
import math
from typing import TYPE_CHECKING, Any

from zicato.import_path import import_dotted_path

if TYPE_CHECKING:  # pragma: no cover — typing-only import
    from zicato.scoring.api import DriftContext, ScalarContext

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Source-hashing — the ONE mechanism every grading plugin shares.
# ---------------------------------------------------------------------------


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def resolve_plugin_source(dotted: str) -> str | None:
    """Return the SHA-256 of the source MODULE that defines ``dotted``.

    Resolves the dotted spec via the shared importer
    (:func:`zicato.import_path.import_dotted_path`), then hashes the source of
    the module the resolved object lives in. Returns ``None`` (rather than
    raising) when the spec is empty, cannot be resolved, or has no inspectable
    source — the caller folds ``None`` in as "no source hash available", so a
    transient resolution failure degrades the hash gracefully instead of
    crashing contract construction.

    The MODULE source is hashed (not just the function body) so an edit to a
    helper the plugin calls — a private ``_linear_looping`` next to it — also
    rolls the epoch. That is the conservative choice: it can over-roll (an
    unrelated edit elsewhere in the same module rolls the epoch) but never
    under-rolls (a behaviour-changing edit the plugin depends on is always
    caught). Operators keep grading plugins in a dedicated module so
    this is tight.
    """
    if not dotted:
        return None
    try:
        obj = import_dotted_path(dotted, label="scoring plugin")
    except ValueError:
        # Unresolvable at hash time (a not-yet-written plugin, a typo). The
        # spec STRING still participates in the hash via the caller, so the
        # contract is not silently identical — only the source component is
        # absent until the plugin exists.
        _log.warning(
            "scoring: could not resolve plugin %r to hash its source; "
            "the contract hash folds in the spec string only",
            dotted,
        )
        return None
    module = inspect.getmodule(obj)
    if module is None:
        return None
    try:
        source = inspect.getsource(module)
    except (OSError, TypeError):
        # A C-extension / builtin / dynamically-constructed module has no
        # inspectable source. Fall back to the spec string only.
        _log.warning(
            "scoring: plugin %r resolves to a module with no inspectable "
            "source; the contract hash folds in the spec string only",
            dotted,
        )
        return None
    return _sha256(source)


def spec_with_source_hash(dotted: str) -> dict[str, str | None]:
    """Canonical contract-hash form of ONE grading dotted spec.

    Returns ``{"spec": <dotted>, "source_sha256": <hash-or-null>}`` — the spec
    string AND the SHA-256 of the resolved module's source. Folding THIS (rather
    than the bare ``dotted`` string) into the contract hash is what makes an
    edit to a plugin's BODY roll the epoch (issue #19 cross-cutting #1):
    swapping the spec string changes ``"spec"``, editing the plugin source
    changes ``"source_sha256"``, and either rolls the hash.

    Used uniformly for the scoring ``scalar_fn`` / ``drift_reducer`` AND the
    board predicates / judges / ``outcome_summarizer_spec`` so all grading
    plugins share ONE mechanism.
    """
    return {"spec": dotted, "source_sha256": resolve_plugin_source(dotted)}


# ---------------------------------------------------------------------------
# Fail-open invocation — mirrors ``evaluate_judges``' try/except + WARNING.
# ---------------------------------------------------------------------------


def _coerce_finite(value: object) -> float | None:
    """Return ``value`` as a finite float, or ``None`` if it cannot be.

    A non-numeric return (``str`` / ``None`` / object), a ``bool`` (a plugin
    must return a number rather than a flag — ``bool`` is an ``int`` subclass so it is
    rejected explicitly), or a non-finite (``NaN`` / ``inf``) all yield ``None``
    so the caller falls back to the documented default.
    """
    if isinstance(value, bool):
        return None
    if not isinstance(value, int | float):
        return None
    out = float(value)
    if not math.isfinite(out):
        return None
    return out


def _invoke_plugin(
    dotted: str,
    ctx: Any,
    *,
    fallback: float,
    pre_token: str,
    seam: str,
) -> tuple[float, str]:
    """Resolve + invoke a scoring plugin fail-open. Shared by both seams.

    Returns ``(value, provenance)``:

    * success → ``(plugin_value, "plugin:<seam>=<dotted>")``;
    * raise / non-finite / non-numeric return → ``(fallback, "<pre_token>
      (fallback: <reason>)")`` after a WARNING log.

    ``pre_token`` is the provenance of the PRE-plugin (transformed-or-builtin)
    value the plugin was composing on top of, so the fallback token records both
    that the plugin failed AND what value scoring fell back to. There is no
    timeout — see the module docstring.
    """
    try:
        fn = import_dotted_path(dotted, label=f"{seam} plugin")
    except ValueError as exc:
        _log.warning(
            "scoring: %s plugin %r could not be resolved (%s); "
            "falling back to the built-in value %r",
            seam,
            dotted,
            exc,
            fallback,
        )
        return fallback, f"{pre_token} (fallback: unresolved)"

    try:
        raw = fn(ctx)
    except Exception as exc:  # noqa: BLE001 — fail-open: a plugin must never crash the run.
        _log.warning(
            "scoring: %s plugin %r raised %s: %s; falling back to the " "built-in value %r",
            seam,
            dotted,
            type(exc).__name__,
            exc,
            fallback,
        )
        return fallback, f"{pre_token} (fallback: raised {type(exc).__name__})"

    value = _coerce_finite(raw)
    if value is None:
        _log.warning(
            "scoring: %s plugin %r returned a non-finite / non-numeric value "
            "%r; falling back to the built-in value %r",
            seam,
            dotted,
            raw,
            fallback,
        )
        return fallback, f"{pre_token} (fallback: non-finite return)"

    return value, f"plugin:{seam}={dotted}"


def apply_drift_reducer(
    dotted: str,
    ctx: DriftContext,
    *,
    pre_value: float,
    pre_token: str,
) -> tuple[float, str]:
    """Seam 1: invoke a ``drift_reducer`` plugin fail-open, composing on top.

    ``pre_value`` / ``pre_token`` are the post-transform (or built-in)
    drift loss + its provenance; the plugin sees that value as
    ``ctx.builtin_loss`` (the dispatcher rebuilt the context), so it wraps the
    declarative shape rather than the raw built-in. On failure it falls back to
    ``pre_value`` with a visible fallback token. Runs INSIDE the worker.
    """
    return _invoke_plugin(
        dotted, ctx, fallback=pre_value, pre_token=pre_token, seam="drift_reducer"
    )


def apply_scalar_fn(
    dotted: str,
    ctx: ScalarContext,
    *,
    pre_value: float,
    pre_token: str,
) -> tuple[float, str]:
    """Seam 2: invoke a ``scalar_fn`` plugin fail-open, composing on top.

    ``pre_value`` / ``pre_token`` are the post-transform (or built-in)
    scalar + its provenance; the plugin sees that value as ``ctx.builtin_scalar``
    (the dispatcher rebuilt the context), so it wraps the declarative shape. On
    failure it falls back to ``pre_value`` with a visible fallback token. Runs
    in the orchestrator.
    """
    return _invoke_plugin(dotted, ctx, fallback=pre_value, pre_token=pre_token, seam="scalar_fn")


__all__ = [
    "resolve_plugin_source",
    "spec_with_source_hash",
    "apply_drift_reducer",
    "apply_scalar_fn",
]
