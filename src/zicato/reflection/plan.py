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
pre-registration honest (you review the frozen plan, then execute exactly it).

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
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from uuid import uuid4

#: ``format_version`` stamped onto every ``plan.json``. A reader rejects any
#: other version (absent / older / newer / garbage) by raising — a plan is a
#: pre-registration contract, not a best-effort artifact, so a version it
#: cannot vouch for must never be silently reinterpreted.
PLAN_FORMAT_VERSION: int = 1

#: The full check vocabulary a plan may request. R2 implements the
#: reliability / discrimination / coverage subset (pillars 1-2); the
#: adjudication-dependent checks (judge-audit, coherence, decomposition) are
#: recorded in the plan but consumed by a later phase's engine.
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

    def to_json(self) -> dict[str, Any]:
        """The JSON shape persisted as ``plan.json`` (lists, not tuples)."""
        return {
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

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ReflectionPlan:
        """Rebuild a plan from its ``plan.json`` dict.

        Raises :class:`ValueError` on a ``format_version`` this reader does
        not own — a pre-registration must never be silently reinterpreted.
        """
        version = data.get("format_version")
        if version != PLAN_FORMAT_VERSION:
            raise ValueError(
                f"reflection plan.json format_version {version!r} is not "
                f"{PLAN_FORMAT_VERSION} — refusing to reinterpret a pre-registration"
            )
        return cls(
            reflection_id=str(data["reflection_id"]),
            epoch_id=str(data["epoch_id"]),
            candidates=tuple(str(c) for c in data.get("candidates", ())),
            entries=tuple(str(e) for e in data.get("entries", ())),
            replicates=int(data["replicates"]),
            adjudicator_model=(
                str(data["adjudicator_model"])
                if data.get("adjudicator_model") is not None
                else None
            ),
            checks=tuple(str(c) for c in data.get("checks", ())),
            mode=str(data.get("mode", MODE_ACTIVE)),
            pre_registered=bool(data.get("pre_registered", False)),
            executed=bool(data.get("executed", False)),
            created_at=str(data.get("created_at", "")),
        )

    def mark_executed(self) -> ReflectionPlan:
        """Return a copy with ``executed=True`` (the plan is frozen)."""
        return replace(self, executed=True)


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
        candidates=tuple(str(c) for c in candidates),
        entries=tuple(str(e) for e in entries),
        replicates=int(replicates),
        adjudicator_model=adjudicator_model,
        checks=tuple(str(c) for c in checks),
        mode=mode,
        pre_registered=pre_registered,
        executed=False,
        created_at=created_at,
    )


def write_plan(workspace_root: Path, plan: ReflectionPlan) -> Path:
    """Persist ``plan.json`` atomically; return its path.

    tmp + rename under the reflection's directory
    (:func:`zicato.core.workspace.reflection_plan_path`). Re-writing an
    executed plan over its pre-registered self is the normal resume path.
    """
    from zicato.core.workspace import reflection_plan_path  # noqa: PLC0415

    path = reflection_plan_path(workspace_root, plan.epoch_id, plan.reflection_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(plan.to_json(), indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)
    return path


def read_plan(workspace_root: Path, epoch_id: str, reflection_id: str) -> ReflectionPlan | None:
    """Load a persisted plan; ``None`` when the file is absent.

    A present-but-malformed / wrong-version file raises via
    :meth:`ReflectionPlan.from_json` — an unreadable pre-registration is an
    error the operator must see, not a silent skip.
    """
    from zicato.core.workspace import reflection_plan_path  # noqa: PLC0415

    path = reflection_plan_path(workspace_root, epoch_id, reflection_id)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    return ReflectionPlan.from_json(json.loads(raw))


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
