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

:func:`score_synthetic_entry` is the tournament worker's single entry
point: it applies the matcher for the entry's kind to the run's closed
event log and conjoins the result with the entry's explicit expectation.

Both matchers read the file through
:mod:`zicato.telemetry.event_log`, so either wire shape of a drift event
resolves to the same payload case. Field VALUES are still normalised
here, because a drift kind and a severity reach disk as proto enum names
(``"DRIFT_OFF_TOPIC"`` / ``"DRIFT_SEVERITY_WARNING"``) or as the bare
lowercase strings a hand-written fixture uses.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from zicato.core.types import BoardEntry, ExpectationKind, ExpectationResult, RuntimeConfig
from zicato.telemetry.event_log import read_event_log

#: Revision of the worker's grading of synthetic entries. The epoch contract
#: folds it into the canonical form of each synthetic board entry, so bumping
#: it rolls every epoch whose board holds a synthetic entry, and no other.
#: Bump it whenever :func:`score_synthetic_entry` or a matcher it calls changes
#: which runs pass.
SYNTHETIC_GRADING_REVISION = 1

# Severities the matchers treat as "this counts" — warning and critical.
# INFO is filtered out everywhere because it is observational by design.
_SCORING_SEVERITIES: frozenset[str] = frozenset({"warning", "critical"})


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
    for record in read_event_log(events_jsonl_path).records:
        if record.case != "drift_detected":
            continue
        kind = _canonical_kind(record.payload.get("kind"))
        severity = _canonical_severity(record.payload.get("severity"))
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
            kind=ExpectationKind.PREDICATE,
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
            kind=ExpectationKind.PREDICATE,
            passed=False,
            detail="missing required drift kinds: " + ", ".join(missing),
        )
    return ExpectationResult(
        kind=ExpectationKind.PREDICATE,
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
            kind=ExpectationKind.PREDICATE,
            passed=False,
            detail=f"clean run produced scoring drift: {rendered}",
        )
    if info_count:
        return ExpectationResult(
            kind=ExpectationKind.PREDICATE,
            passed=True,
            detail=f"clean run: {info_count} info-severity drift event(s) tolerated",
        )
    return ExpectationResult(
        kind=ExpectationKind.PREDICATE,
        passed=True,
        detail="clean run: no drift events",
    )


async def score_synthetic_entry(
    entry: BoardEntry,
    events_jsonl_path: Path,
    explicit: ExpectationResult | None,
    config: RuntimeConfig,
) -> ExpectationResult | None:
    """Return a synthetic entry's verdict: its drift requirement AND its expectation.

    A ``synthetic_adversarial`` entry must show every one of its
    ``required_drift_kinds`` (:func:`evaluate_required_drift`); a
    ``synthetic_clean`` entry must show no warning or critical drift
    (:func:`evaluate_no_drift`). ``events_jsonl_path`` must be the run's
    closed event log. ``explicit`` is the verdict of the entry's own
    ``expectation`` (``None`` when it declares none); the entry passes only
    when both parts pass, and ``detail`` names the part that failed. A
    failed drift requirement scores ``0.0``; otherwise the explicit
    verdict keeps its kind, score, and metrics. Any other entry kind
    returns ``explicit`` unchanged.
    """
    if entry.kind == "synthetic_adversarial":
        drift = await evaluate_required_drift(
            events_jsonl_path, list(entry.required_drift_kinds or ()), config
        )
    elif entry.kind == "synthetic_clean":
        drift = await evaluate_no_drift(events_jsonl_path, config)
    else:
        return explicit
    if explicit is None:
        return drift
    verdicts = {True: "passed", False: "failed"}
    detail = (
        f"drift requirement {verdicts[drift.passed]}: {drift.detail}; "
        f"expectation {verdicts[explicit.passed]}: {explicit.detail}"
    )
    if not drift.passed:
        return replace(explicit, passed=False, detail=detail, score=0.0)
    return replace(explicit, detail=detail)
