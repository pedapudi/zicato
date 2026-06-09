"""Pluggable scoring seams.

zicato's scoring has two stages that historically absorbed core edits when a
new scoring *shape* was needed: per-run drift reduction (Seam 1, in the
killable worker) and per-generation scalar synthesis (Seam 2, in the
orchestrator). This package extracts each as a named pure function over a
read-only typed context, routed through a small dispatcher, so later phases
(declarative transforms, dotted-spec plugins) plug in additively rather than
by editing the seams.

PHASE 1 (this package as introduced) is a pure refactor: the dispatchers
compute the extracted built-in formula and return it with a ``"builtin"``
provenance marker. No behaviour change.

Public surface
--------------
* :class:`~zicato.scoring.api.DriftContext` /
  :class:`~zicato.scoring.api.ScalarContext` — the frozen seam contexts.
* :func:`~zicato.scoring.builtins.builtin_drift_loss` /
  :func:`~zicato.scoring.builtins.builtin_scalar` — the extracted defaults.
* :func:`~zicato.scoring.dispatch.resolve_drift_loss` /
  :func:`~zicato.scoring.dispatch.resolve_scalar` — the dispatchers the live
  paths call.
"""

from __future__ import annotations

from zicato.scoring.api import (
    PROVENANCE_BUILTIN,
    DriftContext,
    ScalarContext,
    ScoringProvenance,
)
from zicato.scoring.builtins import builtin_drift_loss, builtin_scalar
from zicato.scoring.dispatch import resolve_drift_loss, resolve_scalar
from zicato.scoring.plugins import (
    apply_drift_reducer,
    apply_scalar_fn,
    resolve_plugin_source,
    spec_with_source_hash,
)
from zicato.scoring.transforms import (
    TransformSpec,
    TransformSpecError,
    apply_transform,
    transform_op_names,
    validate_transform_spec,
)

__all__ = [
    "DriftContext",
    "ScalarContext",
    "ScoringProvenance",
    "PROVENANCE_BUILTIN",
    "builtin_drift_loss",
    "builtin_scalar",
    "resolve_drift_loss",
    "resolve_scalar",
    "apply_drift_reducer",
    "apply_scalar_fn",
    "resolve_plugin_source",
    "spec_with_source_hash",
    "TransformSpec",
    "TransformSpecError",
    "apply_transform",
    "validate_transform_spec",
    "transform_op_names",
]
