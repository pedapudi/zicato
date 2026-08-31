"""Declarative transform registry — the code-free 90% of pluggable scoring.

The two scoring seams (:mod:`zicato.scoring.dispatch`) historically absorbed a
core edit every time a new scoring *shape* was needed: a non-linear recall
curve (``pass_exponent``), a diminishing-returns drift aggregation (the
harmonic ``looping_reasoning`` special-case), a cap, a clamp. Each was a
bespoke field + a formula edit in shared core, and an unconditional one (the
harmonic case) silently changed scoring for *every* operator.

This module replaces that pattern with a small registry of **named, pure,
parameterized one-dimensional shapes** that the contract composes
declaratively in ``scoring.json``::

    "pass_transform":  { "op": "pow", "exponent": 2.0 }
    "drift_kind_aggregation": {
        "looping_reasoning": { "op": "harmonic" },
        "off_topic":         { "op": "cap", "max": 5 }
    }

A :data:`TransformSpec` is a plain ``{"op": "<name>", ...params}`` dict — fully
serializable, so it folds into the frozen contract hash for free (the
field-enumerating serde already covers new ``ScoringWeights`` fields) and is
trivially reproducible.

Design constraints
------------------
* **Single op per slot.** Each spec names exactly one op; there is NO pipeline
  / composition syntax. Arbitrary multi-step logic is Phase 3's job (the
  dotted-spec ``scalar_fn`` / ``drift_reducer`` plugins), not the registry's.
* **Pure / deterministic / no-LLM / no-I/O / no-wall-clock.** Every transform
  is a closed-form function of its ``value`` + params.
* **Validation is fail-fast at CONTRACT LOAD, never mid-scoring.**
  :func:`validate_transform_spec` rejects unknown ops and non-finite / invalid
  params at construction time (``ScoringWeights.__post_init__`` calls it), so a
  transform can never produce a ``NaN`` / ``inf`` partway through a run and
  silently poison a scalar. By the time :func:`apply_transform` runs, the spec
  is already known-good.
* **Neutral default = ``linear`` (identity).** An absent ``pass_transform`` and
  an absent ``drift_kind_aggregation`` entry both mean ``linear``, i.e. today's
  built-in shape unchanged.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from typing import Any

#: A declarative transform: ``{"op": "<name>", ...params}``. A plain JSON
#: dict so it serialises natively into ``scoring.json`` + the contract hash.
TransformSpec = Mapping[str, Any]


# ---------------------------------------------------------------------------
# The named shapes. Each is ``(value, **params) -> value`` — pure, total on a
# validated spec (validation having already rejected params that could make
# the body raise / go non-finite).
# ---------------------------------------------------------------------------


def _linear(value: float) -> float:
    """Identity — the neutral shape. ``linear(x) == x``."""
    return value


def _pow(value: float, *, exponent: float) -> float:
    """``value ** exponent``. The replacement for the retired ``pass_exponent``.

    ``pow(x, 2.0)`` reproduces the quadratic-recall miss term
    ``(1 - mean_score) ** 2`` exactly. (A stray ``pass_exponent`` config key is
    rejected at load — see ``workspace_loader._reject_retired_scoring_keys``.)
    """
    return float(value**exponent)


def _harmonic(value: float) -> float:
    """Diminishing-returns over an integer count: ``1 + 1/2 + … + 1/n``.

    ``value`` is a count ``n`` (truncated to an int floor; a fractional count
    is not meaningful for this shape). ``harmonic(0) == 0`` (empty sum). This
    reproduces the old unconditional ``looping_reasoning`` special-case the
    reducer used to carry — now opt-in per contract via
    ``drift_kind_aggregation: {"looping_reasoning": {"op": "harmonic"}}``.
    """
    n = int(value)
    if n <= 0:
        return 0.0
    return math.fsum(1.0 / k for k in range(1, n + 1))


def _cap(value: float, *, max: float) -> float:  # noqa: A002 — "max" is the spec param name
    """Upper-bound: ``min(value, max)``. A one-sided ceiling on a contribution."""
    return float(min(value, max))


def _clip(value: float, *, lo: float, hi: float) -> float:
    """Clamp into ``[lo, hi]``. A two-sided bound."""
    return float(min(max(value, lo), hi))


def _log1p(value: float) -> float:
    """``log(1 + value)`` — gentle diminishing returns on a non-negative value.

    Defined for ``value > -1``; validation cannot know the runtime value, so
    the dispatcher only feeds this non-negative aggregates / counts (always
    ``> -1``). Pure closed form, never raises on those.
    """
    return float(math.log1p(value))


# ---------------------------------------------------------------------------
# Registry + per-op required/optional param schema (drives validation).
# ---------------------------------------------------------------------------

#: ``op name -> implementation``.
_REGISTRY: dict[str, Callable[..., float]] = {
    "linear": _linear,
    "pow": _pow,
    "harmonic": _harmonic,
    "cap": _cap,
    "clip": _clip,
    "log1p": _log1p,
}

#: ``op name -> required finite-float param names``. Every param a transform
#: takes is a finite float; listing them here lets one validation loop reject
#: a missing / non-finite / non-numeric param uniformly with a clear message.
_REQUIRED_PARAMS: dict[str, tuple[str, ...]] = {
    "linear": (),
    "pow": ("exponent",),
    "harmonic": (),
    "cap": ("max",),
    "clip": ("lo", "hi"),
    "log1p": (),
}


def transform_op_names() -> tuple[str, ...]:
    """The set of registered op names (sorted, for stable error messages)."""
    return tuple(sorted(_REGISTRY))


class TransformSpecError(ValueError):
    """A transform spec is malformed — raised at CONTRACT LOAD, never mid-run.

    A subclass of :class:`ValueError` so the contract-construction path can
    treat it like any other field-validation error; carrying its own type lets
    callers / tests assert it is specifically a transform problem.
    """


def validate_transform_spec(spec: object) -> None:
    """Reject a malformed transform spec, fail-fast, at contract-load time.

    Enforced invariants (any violation raises :class:`TransformSpecError`):

    * ``spec`` is a mapping carrying a string ``"op"``;
    * ``op`` names a registered transform;
    * every REQUIRED param for that op is present, numeric, and FINITE
      (no ``NaN`` / ``inf`` — those are the values that would otherwise
      surface as a poisoned scalar mid-scoring);
    * ``clip`` additionally requires ``lo <= hi``;
    * no UNKNOWN params are supplied (a typo'd param name — e.g.
      ``{"op":"pow","exponant":2}`` — is rejected loudly rather than silently
      defaulting).

    Runs from :meth:`ScoringWeights.__post_init__`, so by the time
    :func:`apply_transform` executes the spec is already known-good and the
    transform is total — it never produces a ``NaN`` partway through a run.
    """
    if not isinstance(spec, Mapping):
        raise TransformSpecError(
            f"transform spec must be a mapping like {{'op': ...}}, got {spec!r}"
        )
    op = spec.get("op")
    if not isinstance(op, str):
        raise TransformSpecError(f"transform spec is missing a string 'op': {dict(spec)!r}")
    if op not in _REGISTRY:
        raise TransformSpecError(
            f"unknown transform op {op!r}; known ops: {', '.join(transform_op_names())}"
        )
    required = _REQUIRED_PARAMS[op]
    supplied = {k for k in spec if k != "op"}
    unknown = supplied - set(required)
    if unknown:
        raise TransformSpecError(
            f"transform op {op!r} got unexpected param(s) {sorted(unknown)!r}; "
            f"it accepts {list(required)!r}"
        )
    for name in required:
        if name not in spec:
            raise TransformSpecError(f"transform op {op!r} requires param {name!r}")
        raw = spec[name]
        if isinstance(raw, bool) or not isinstance(raw, int | float):
            raise TransformSpecError(
                f"transform op {op!r} param {name!r} must be a finite number, got {raw!r}"
            )
        if not math.isfinite(float(raw)):
            raise TransformSpecError(
                f"transform op {op!r} param {name!r} must be finite, got {raw!r}"
            )
    if op == "clip" and float(spec["lo"]) > float(spec["hi"]):
        raise TransformSpecError(
            f"transform op 'clip' requires lo <= hi, got lo={spec['lo']!r} hi={spec['hi']!r}"
        )


def apply_transform(spec: TransformSpec, value: float) -> float:
    """Apply a (PRE-VALIDATED) transform spec to ``value``.

    Assumes :func:`validate_transform_spec` has already accepted ``spec`` at
    contract load — so this stays a thin, total dispatch with no re-validation
    on the hot scoring path. Params are passed through verbatim (minus ``op``).
    """
    op = spec["op"]
    fn = _REGISTRY[op]
    params = {k: v for k, v in spec.items() if k != "op"}
    return float(fn(value, **params))


def is_neutral(spec: TransformSpec | None) -> bool:
    """True iff ``spec`` is the neutral identity (absent or ``linear``).

    Lets the dispatchers short-circuit to the byte-identical built-in path +
    ``"builtin"`` provenance when no real reshaping was requested, so a
    contract that does not opt into transforms scores exactly as today.
    """
    return spec is None or spec.get("op") == "linear"


__all__ = [
    "TransformSpec",
    "TransformSpecError",
    "apply_transform",
    "validate_transform_spec",
    "transform_op_names",
    "is_neutral",
]
