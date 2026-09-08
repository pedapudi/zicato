"""The pre-registered reflection run plan (``plan.json``).

Sound experiment design opens with **pre-registration**: before spending a
byte of budget, write down the candidates, entries, replicate count,
adjudicator, and checks the run will take. This mirrors zicato's mandatory
pre-run hypothesis discipline — ``zicato inspect reflection --pre-register`` writes the
plan and STOPS for review, so the loss can never be p-hacked to whatever the
run happened to show (BOARD-REFLECTION.md §"the protocol").

:class:`ReflectionPlan` is the frozen value object and ``plan.json`` is its
round-trip form. The plan carries a monotone **executed** flag: a
pre-registered plan is written with ``executed=False`` and STOPS; a later
invocation loads it, runs the corpus, and re-writes it with
:meth:`ReflectionPlan.mark_executed` set — the stop/resume seam that keeps a
pre-registration honest: the operator reviews the frozen plan, then executes
that plan and no other.

Timestamps are **injected**, never read from the wall clock here, so the
``reflection_id`` is deterministic under test: :func:`make_reflection_id`
derives ``refl-{compact_ts}-{8hex}`` from a caller-supplied ``created_at`` and
an optional seed token (absent ⇒ a fresh random suffix).

Storage lives under ``epochs/{epoch_id}/reflections/{reflection_id}/plan.json``
(:func:`zicato.core.workspace.reflection_plan_path`).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from zicato.core.measurement import (
    MeasurementPurpose,
    measurement_range,
    validate_measurement_interval,
)
from zicato.epoch._storage import RecordError, check_record_format
from zicato.storage import atomic_write_json
from zicato.workspace.projection import mark_epoch_changed

#: ``format_version`` stamped onto every ``plan.json``. A reader rejects any
#: other version (absent / older / newer / garbage) by raising — a plan is a
#: pre-registration contract rather than a best-effort artifact, so a version it
#: cannot vouch for must never be silently reinterpreted.
PLAN_FORMAT_VERSION: int = 1

#: The full check vocabulary a plan may request. The reliability,
#: discrimination and coverage checks (pillars 1-2) are implemented here; the
#: adjudication-dependent checks (judge-audit, coherence, decomposition) are
#: recorded in the plan and consumed by the adjudication engine.
CHECK_JUDGE_AUDIT: str = "judge-audit"
CHECK_RELIABILITY: str = "reliability"
CHECK_COHERENCE: str = "coherence"
CHECK_DECOMPOSITION: str = "decomposition"
CHECK_DISCRIMINATION: str = "discrimination"
CHECK_COVERAGE: str = "coverage"

#: The default check set (``--checks`` absent) — every check, per the CLI spec.
DEFAULT_CHECKS: tuple[str, ...] = (
    CHECK_JUDGE_AUDIT,
    CHECK_RELIABILITY,
    CHECK_COHERENCE,
    CHECK_DECOMPOSITION,
    CHECK_DISCRIMINATION,
    CHECK_COVERAGE,
)

#: The two corpus cadences: ``"active"`` spends budget to produce fresh draws
#: at the reserved replicate base; ``"passive"`` references the lineage's
#: existing run artifacts with zero LLM budget.
MODE_ACTIVE: str = "active"
MODE_PASSIVE: str = "passive"


def _compact_ts(created_at: str) -> str:
    """Compact an injected ISO timestamp to the ``reflection_id`` stem.

    Keeps only the digits of ``created_at`` (``2026-07-01T00:00:00+00:00`` →
    ``20260701000000``), truncated to 14 characters. Pure and deterministic —
    the ``reflection_id`` is stable across processes given the same
    ``created_at``, so a re-run of a pre-registered plan resolves the same
    storage directory.
    """
    digits = "".join(ch for ch in created_at if ch.isdigit())
    return (digits[:14] or "00000000000000").ljust(14, "0")


def make_reflection_id(created_at: str, *, token: str | None = None) -> str:
    """Build a ``refl-{compact_ts}-{8hex}`` id from an INJECTED timestamp.

    ``token`` (a seed) is hashed to a deterministic 8-hex suffix so a test can
    pin the whole id; absent, a fresh ``uuid4`` suffix is used. No wall-clock
    read happens here — determinism under test is by construction.
    """
    ts = _compact_ts(created_at)
    if token is None:
        suffix = uuid4().hex[:8]
    else:
        suffix = hashlib.sha256(token.encode("utf-8")).hexdigest()[:8]
    return f"refl-{ts}-{suffix}"


@dataclass(frozen=True, slots=True)
class ReflectionPlan:
    """One board-reflection run's pre-registered plan.

    Fields
    ------
    reflection_id:
        ``refl-{ts}-{8hex}`` — the storage key and the bootstrap RNG seed
        (:func:`zicato.reflection.analysis.decision_flip_probability` seeds
        from it, so the headline reliability number is reproducible).
    epoch_id:
        The sealed contract (epoch) under validation.
    candidates:
        The generation ids in the candidate spread (champion + a lineage
        slice) that supply the discrimination signal.
    entries:
        The board entry ids the run covers (default: the whole board).
    replicates:
        K — the number of active draws per (candidate, entry) unit. The
        passive tier reads whatever replicate slots already exist.
    adjudicator_model:
        The independent meta-judge model string (pillar 3, later phase);
        ``None`` for the reliability/coverage-only cheap tier.
    checks:
        The subset of :data:`DEFAULT_CHECKS` this run requested.
    mode:
        :data:`MODE_ACTIVE` or :data:`MODE_PASSIVE`.
    pre_registered:
        ``True`` when the plan was written by ``--pre-register`` (written +
        stopped for review before any budget was spent).
    executed:
        Monotone: ``False`` until the corpus run completes, then flipped by
        :meth:`mark_executed`. The stop/resume seam.
    created_at:
        The injected ISO-8601 timestamp the ``reflection_id`` derived from.
    format_version:
        :data:`PLAN_FORMAT_VERSION`.
    """

    reflection_id: str
    epoch_id: str
    candidates: tuple[str, ...]
    entries: tuple[str, ...]
    replicates: int
    adjudicator_model: str | None
    checks: tuple[str, ...]
    mode: str
    pre_registered: bool
    executed: bool
    created_at: str
    format_version: int = PLAN_FORMAT_VERSION
    _json: str | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        validate_measurement_interval(
            measurement_range(MeasurementPurpose.REFLECTION).start,
            self.replicates,
            allow_empty=self.mode == MODE_PASSIVE,
        )

    def to_json(self) -> dict[str, Any]:
        """Return stored fields unchanged, or encode a freshly constructed plan."""
        fields = {
            "format_version": self.format_version,
            "reflection_id": self.reflection_id,
            "epoch_id": self.epoch_id,
            "candidates": list(self.candidates),
            "entries": list(self.entries),
            "replicates": self.replicates,
            "adjudicator_model": self.adjudicator_model,
            "checks": list(self.checks),
            "mode": self.mode,
            "pre_registered": self.pre_registered,
            "executed": self.executed,
            "created_at": self.created_at,
        }
        if self._json is None:
            return fields
        stored: dict[str, Any] = json.loads(self._json)
        defaults = {
            "candidates": [],
            "entries": [],
            "checks": [],
            "mode": MODE_ACTIVE,
            "pre_registered": False,
            "executed": False,
            "created_at": "",
        }
        stored.update(
            (key, value)
            for key, value in fields.items()
            if key in stored or value != defaults.get(key)
        )
        return stored

    @classmethod
    def from_json(cls, data: Any) -> ReflectionPlan:
        """Accept one plan without coercing flags or filling stored omissions."""
        if not isinstance(data, dict):
            raise RecordError("reflection plan: expected a JSON object")
        check_record_format(data, "reflection plan", expected_version=PLAN_FORMAT_VERSION)
        for name in ("reflection_id", "epoch_id"):
            if not isinstance(data.get(name), str) or not data[name]:
                raise RecordError(f"reflection plan: {name} must be a nonempty string")
        for name in ("created_at", "mode"):
            if name in data and not isinstance(data[name], str):
                raise RecordError(f"reflection plan: {name} must be a string")
        mode = data.get("mode", MODE_ACTIVE)
        if mode not in (MODE_ACTIVE, MODE_PASSIVE):
            raise RecordError(f"reflection plan: unknown mode {mode!r}")
        for name in ("candidates", "entries", "checks"):
            if name in data and (
                not isinstance(data[name], list)
                or any(not isinstance(value, str) or not value for value in data[name])
            ):
                raise RecordError(f"reflection plan: {name} must be an array of nonempty strings")
        replicates = data.get("replicates")
        if isinstance(replicates, bool) or not isinstance(replicates, int) or replicates < 1:
            raise RecordError("reflection plan: replicates must be a positive integer")
        for name in ("pre_registered", "executed"):
            if name in data and not isinstance(data[name], bool):
                raise RecordError(f"reflection plan: {name} must be a boolean")
        model = data.get("adjudicator_model")
        if model is not None and not isinstance(model, str):
            raise RecordError("reflection plan: adjudicator_model must be a string or null")
        try:
            encoded = json.dumps(data, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise RecordError(f"reflection plan: invalid JSON value: {exc}") from exc
        return cls(
            reflection_id=data["reflection_id"],
            epoch_id=data["epoch_id"],
            candidates=tuple(data.get("candidates", ())),
            entries=tuple(data.get("entries", ())),
            replicates=replicates,
            adjudicator_model=model,
            checks=tuple(data.get("checks", ())),
            mode=mode,
            pre_registered=data.get("pre_registered", False),
            executed=data.get("executed", False),
            created_at=data.get("created_at", ""),
            _json=encoded,
        )

    def mark_executed(self) -> ReflectionPlan:
        """Set the completion flag while retaining other recorded fields."""
        return self.from_json(dict(self.to_json(), executed=True))


def new_plan(
    *,
    epoch_id: str,
    candidates: tuple[str, ...] | list[str],
    entries: tuple[str, ...] | list[str],
    replicates: int,
    created_at: str,
    adjudicator_model: str | None = None,
    checks: tuple[str, ...] | list[str] = DEFAULT_CHECKS,
    mode: str = MODE_ACTIVE,
    pre_registered: bool = False,
    token: str | None = None,
    reflection_id: str | None = None,
) -> ReflectionPlan:
    """Construct a fresh (un-executed) :class:`ReflectionPlan`.

    ``reflection_id`` may be supplied verbatim (a resume) or derived from the
    injected ``created_at`` + optional ``token`` seed via
    :func:`make_reflection_id`. The plan is always born ``executed=False``.
    """
    rid = reflection_id or make_reflection_id(created_at, token=token)
    return ReflectionPlan(
        reflection_id=rid,
        epoch_id=epoch_id,
        candidates=tuple(candidates),
        entries=tuple(entries),
        replicates=replicates,
        adjudicator_model=adjudicator_model,
        checks=tuple(checks),
        mode=mode,
        pre_registered=pre_registered,
        executed=False,
        created_at=created_at,
    )


def write_plan(workspace_root: Path, plan: ReflectionPlan) -> Path:
    """Validate and durably publish the plan after marking its index projection."""
    from zicato.core.workspace import reflection_plan_path  # noqa: PLC0415

    body = ReflectionPlan.from_json(plan.to_json()).to_json()
    path = reflection_plan_path(workspace_root, plan.epoch_id, plan.reflection_id)
    prior = read_plan(workspace_root, plan.epoch_id, plan.reflection_id)
    if prior is None or json.dumps(prior.to_json(), sort_keys=True) != json.dumps(
        body, sort_keys=True
    ):
        mark_epoch_changed(workspace_root, plan.epoch_id)
        atomic_write_json(path, body)
    return path


def read_plan(workspace_root: Path, epoch_id: str, reflection_id: str) -> ReflectionPlan | None:
    """Return an accepted plan, or ``None`` only when its file is absent."""
    from zicato.core.workspace import reflection_plan_path  # noqa: PLC0415

    path = reflection_plan_path(workspace_root, epoch_id, reflection_id)
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise RecordError(f"reflection plan {path}: {exc}") from exc
    plan = ReflectionPlan.from_json(body)
    if plan.epoch_id != epoch_id or plan.reflection_id != reflection_id:
        raise RecordError(f"reflection plan {path}: recorded identity does not match its location")
    return plan


__all__ = [
    "CHECK_COHERENCE",
    "CHECK_COVERAGE",
    "CHECK_DECOMPOSITION",
    "CHECK_DISCRIMINATION",
    "CHECK_JUDGE_AUDIT",
    "CHECK_RELIABILITY",
    "DEFAULT_CHECKS",
    "MODE_ACTIVE",
    "MODE_PASSIVE",
    "PLAN_FORMAT_VERSION",
    "ReflectionPlan",
    "make_reflection_id",
    "new_plan",
    "read_plan",
    "write_plan",
]
