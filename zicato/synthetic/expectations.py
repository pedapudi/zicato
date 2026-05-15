"""Expectation matchers for synthetic board entries.

Two matchers, both shaped as ``async`` functions that replay a
JSONL event stream and return an
:class:`zicato.core.types.ExpectationResult`:

* :func:`evaluate_required_drift` — used by ``synthetic_adversarial``
  entries. Pass iff every drift kind in ``required_kinds`` appears at
  least once with severity in {warning, critical}. INFO drift does
  not count toward satisfying a required kind (those are
  observational; the steerer emits them on healthy runs too and would
  trivially satisfy any requirement).
* :func:`evaluate_no_drift` — used by ``synthetic_clean`` entries.
  Pass iff zero drift events with severity in {warning, critical}
  appear. INFO drift is tolerated.

JSONL parsing is intentionally schema-light. Goldfive writes its
events through ``google.protobuf.json_format.MessageToJson`` which
produces camelCase keys like ``driftDetected`` with nested ``kind``
and ``severity`` enum-string fields (e.g. ``"DRIFT_OFF_TOPIC"`` /
``"DRIFT_SEVERITY_WARNING"``). We accept both that canonical form and
a snake_case fallback (``drift_detected``, lower-case kind strings) so
zicato-internal tests can hand-write fixtures without invoking
protobuf machinery.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from zicato.core.types import ExpectationResult, RuntimeConfig


# Severities the matchers treat as "this counts" — warning and critical.
# INFO is filtered out everywhere because it is observational by design.
_SCORING_SEVERITIES: frozenset[str] = frozenset({"warning", "critical"})


def _iter_jsonl_records(path: Path) -> Iterable[dict[str, Any]]:
    """Yield dict records from a JSONL file, skipping blanks and bad lines.

    The replay path is deliberately forgiving: a malformed line is
    skipped rather than aborting the replay because an aborted replay
    biases the expectation result (a critical drift on the last line
    could be silently dropped if the line before it was malformed).
    """
    if not path.exists():
        return
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                yield record


def _extract_drift_payload(record: dict[str, Any]) -> dict[str, Any] | None:
    """Return the drift-detected payload dict, or ``None`` if this is not a drift event.

    Handles both the protobuf-JSON form (``"driftDetected": {...}``)
    and the zicato-internal snake_case form (``"drift_detected": {...}``).
    """
    payload = record.get("driftDetected") or record.get("drift_detected")
    if isinstance(payload, dict):
        return payload
    return None


def _canonical_kind(value: Any) -> str:
    """Normalize a drift-kind string to the lowercase wire form.

    Accepts goldfive's proto-enum spellings (``"DRIFT_OFF_TOPIC"``),
    the bare lowercase form zicato uses internally (``"off_topic"``),
    or mixed-case spellings from hand-written fixtures. The lowercase
    bare form is what :mod:`zicato.core.drift_kinds` registers.
    """
    if not isinstance(value, str):
        return ""
    s = value.strip()
    if not s:
        return ""
    upper = s.upper()
    if upper.startswith("DRIFT_KIND_"):
        return upper[len("DRIFT_KIND_") :].lower()
    if upper.startswith("DRIFT_"):
        return upper[len("DRIFT_") :].lower()
    return s.lower()


def _canonical_severity(value: Any) -> str:
    """Normalize a severity to one of ``{"info", "warning", "critical", ""}``.

    Mirrors :func:`_canonical_kind` for the severity enum. Unknown
    spellings collapse to the empty string and are filtered out by
    the matcher (treated as non-scoring rather than raising — the
    replay path is forgiving by design).
    """
    if not isinstance(value, str):
        return ""
    s = value.strip()
    if not s:
        return ""
    upper = s.upper()
    if upper.startswith("DRIFT_SEVERITY_"):
        bare = upper[len("DRIFT_SEVERITY_") :].lower()
    elif upper.startswith("SEVERITY_"):
        bare = upper[len("SEVERITY_") :].lower()
    else:
        bare = s.lower()
    if bare in {"info", "warning", "critical"}:
        return bare
    return ""


def _drift_observations(
    events_jsonl_path: Path,
) -> list[tuple[str, str]]:
    """Walk ``events_jsonl_path`` and return ``(kind, severity)`` tuples.

    One tuple per ``DriftDetected`` event found in the file. Both
    fields are normalized to the lowercase bare form. Unknown / empty
    kinds or severities are still emitted (as empty strings) so the
    caller's filtering logic owns the policy of what counts.
    """
    observations: list[tuple[str, str]] = []
    for record in _iter_jsonl_records(events_jsonl_path):
        payload = _extract_drift_payload(record)
        if payload is None:
            continue
        kind = _canonical_kind(payload.get("kind"))
        severity = _canonical_severity(payload.get("severity"))
        observations.append((kind, severity))
    return observations


async def evaluate_required_drift(
    events_jsonl_path: Path,
    required_kinds: list[str],
    config: RuntimeConfig,
) -> ExpectationResult:
    """Pass iff every kind in ``required_kinds`` fired at least once.

    "Fired" means a ``DriftDetected`` event was emitted with that
    canonical lowercase kind AND a severity in
    ``{warning, critical}``. INFO drift does not satisfy a
    requirement — INFO is observational and would trivially make
    every adversarial-entry expectation pass.

    Parameters
    ----------
    events_jsonl_path:
        Path to the goldfive event JSONL produced by the run.
    required_kinds:
        Drift kinds that MUST appear at scoring severity. The list
        is treated as a set; duplicates collapse.
    config:
        Reserved for forward compatibility (e.g. per-entry severity
        overrides driven by :class:`ScoringWeights`). Not currently
        used; accepted so the API matches the no-drift matcher and
        future extensions don't need an awkward optional kwarg.

    Returns
    -------
    ExpectationResult
        ``kind="predicate"``. ``passed`` is true iff every required
        kind was observed at scoring severity. ``detail`` lists the
        missing kinds when ``passed`` is false, or names the
        satisfied kinds when ``passed`` is true.
    """
    del config  # reserved; see docstring
    required = {k.strip().lower() for k in required_kinds if isinstance(k, str) and k.strip()}
    if not required:
        return ExpectationResult(
            kind="predicate",
            passed=False,
            detail="required_drift_kinds was empty",
        )

    observed: set[str] = set()
    for kind, severity in _drift_observations(events_jsonl_path):
        if severity in _SCORING_SEVERITIES and kind:
            observed.add(kind)

    missing = sorted(required - observed)
    if missing:
        return ExpectationResult(
            kind="predicate",
            passed=False,
            detail="missing required drift kinds: " + ", ".join(missing),
        )
    return ExpectationResult(
        kind="predicate",
        passed=True,
        detail="all required drift kinds observed: " + ", ".join(sorted(required)),
    )


async def evaluate_no_drift(
    events_jsonl_path: Path,
    config: RuntimeConfig,
) -> ExpectationResult:
    """Pass iff zero drift events with severity in ``{warning, critical}``.

    INFO drift is tolerated: the steerer emits it routinely on healthy
    runs as part of its observation paths, and counting it would fail
    every well-behaved clean entry.

    Parameters
    ----------
    events_jsonl_path:
        Path to the goldfive event JSONL produced by the run.
    config:
        Reserved for forward compatibility. Not currently used.

    Returns
    -------
    ExpectationResult
        ``kind="predicate"``. ``passed`` is true iff no warning/critical
        drift fired. ``detail`` enumerates the offending events on
        failure (kind + severity, lowest-cost diagnostic) or notes the
        clean run on success.
    """
    del config  # reserved; see docstring
    offending: list[tuple[str, str]] = []
    info_count = 0
    for kind, severity in _drift_observations(events_jsonl_path):
        if severity in _SCORING_SEVERITIES:
            offending.append((kind, severity))
        elif severity == "info":
            info_count += 1

    if offending:
        rendered = ", ".join(f"{kind or '<unknown>'}@{sev}" for kind, sev in offending)
        return ExpectationResult(
            kind="predicate",
            passed=False,
            detail=f"clean run produced scoring drift: {rendered}",
        )
    if info_count:
        return ExpectationResult(
            kind="predicate",
            passed=True,
            detail=f"clean run: {info_count} info-severity drift event(s) tolerated",
        )
    return ExpectationResult(
        kind="predicate",
        passed=True,
        detail="clean run: no drift events",
    )
