"""Persisted proposer recommendations, their integrity checks, and atomic publication.

Reading accepts aggregate evidence and proposed file contents without running a
reflection pass or applying a remedy. Missing records are distinct from damaged
records; every reader uses the same coordinate and content checks.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from zicato.core.workspace import proposer_reflection_findings_path, proposer_reflections_dir
from zicato.epoch._storage import RecordError
from zicato.storage import atomic_write_json
from zicato.workspace import WorkspaceLayout, list_epoch_ids

SEVERITY_CRITICAL = "critical"
SEVERITY_WARNING = "warning"
SEVERITY_INFO = "info"


FORBIDDEN_KEYS: frozenset[str] = frozenset(
    {
        "entry_id",
        "entry_ids",
        "entries",
        "task",
        "task_text",
        "question",
        "prompt",
        "expected",
        "expected_output",
        "answer",
        "output",
        "transcript",
        "turns",
        "holdout",
        "holdout_entries",
        "attributable_regressions",
        "run_ref",
        "span",
        "evidence_span",
    }
)


class RedactionError(RecordError):
    """A record reached the persist boundary carrying board content."""


def assert_redacted(payload: Any, *, where: str = "record") -> None:
    """Refuse board identity or content keys at every depth of a record."""
    if isinstance(payload, dict):
        for key, value in payload.items():
            if str(key).lower() in FORBIDDEN_KEYS:
                raise RedactionError(
                    f"{where}: forbidden key {key!r}; only aggregate mechanism evidence is allowed"
                )
            assert_redacted(value, where=f"{where}.{key}")
    elif isinstance(payload, list | tuple):
        for index, value in enumerate(payload):
            assert_redacted(value, where=f"{where}[{index}]")


@dataclass(frozen=True, slots=True)
class ProposerRemedy:
    """A ready-to-apply edit to the proposer dir — the finding's remedy slot.

    ``relative_path`` is resolved against the proposer dir; ``new_text`` is the
    exact bytes the apply command writes; ``sha256`` digests them so an applied
    recommendation is verifiable afterwards. ``diff`` is the unified diff
    against what is on disk, for the operator to read before deciding.
    """

    kind: str
    relative_path: str
    new_text: str
    sha256: str
    diff: str = ""
    _json: str | None = field(default=None, repr=False, compare=False)

    def to_json(self) -> dict[str, Any]:
        return {
            **(json.loads(self._json) if self._json is not None else {}),
            "kind": self.kind,
            "relative_path": self.relative_path,
            "new_text": self.new_text,
            "sha256": self.sha256,
            "diff": self.diff,
        }

    @classmethod
    def from_json(cls, body: Any) -> ProposerRemedy:
        stored = _body(body, "proposer remedy")
        _strings(body, ("kind", "relative_path", "new_text", "sha256", "diff"))
        if body["kind"] not in {"skill_add", "skill_replace"}:
            raise RecordError("proposer remedy: unsupported kind")
        path = Path(body["relative_path"])
        if (
            not body["relative_path"]
            or path.is_absolute()
            or ".." in path.parts
            or not path.parts
            or "\x00" in body["relative_path"]
        ):
            raise RecordError("proposer remedy: path escapes the proposer dir or is empty")
        actual = hashlib.sha256(body["new_text"].encode("utf-8")).hexdigest()
        if body["sha256"] != actual:
            raise RecordError("proposer remedy: integrity check failed; text does not match sha256")
        return cls(
            **{key: body[key] for key in ("kind", "relative_path", "new_text", "sha256", "diff")},
            _json=stored,
        )


@dataclass(frozen=True, slots=True)
class ProposerFinding:
    """One recommendation, carrying the five-slot evidence convention.

    The slots are the same five board reflection's findings carry, read for a
    proposer: ``population`` (which proposals, which epochs), ``measured`` (the
    scorecard numbers that fired it), ``compared_against`` (the banded prior
    epochs or the base rate), ``remedy`` (the drafted diff — a real payload, not
    a prose suggestion), and ``remedy_safety`` (what the edit cannot affect).
    """

    finding_id: str
    severity: str
    title: str
    detail: str
    population: str
    measured: tuple[dict[str, Any], ...]
    compared_against: str
    remedy: ProposerRemedy | None
    remedy_safety: str
    _json: str | None = field(default=None, repr=False, compare=False)

    def to_json(self) -> dict[str, Any]:
        return {
            **(json.loads(self._json) if self._json is not None else {}),
            "finding_id": self.finding_id,
            "severity": self.severity,
            "title": self.title,
            "detail": self.detail,
            "population": self.population,
            "measured": [dict(m) for m in self.measured],
            "compared_against": self.compared_against,
            "remedy": self.remedy.to_json() if self.remedy is not None else None,
            "remedy_safety": self.remedy_safety,
        }

    @classmethod
    def from_json(cls, body: Any) -> ProposerFinding:
        stored = _body(body, "proposer finding")
        fields = (
            "finding_id",
            "severity",
            "title",
            "detail",
            "population",
            "compared_against",
            "remedy_safety",
        )
        _strings(body, fields)
        if not body["finding_id"] or body["severity"] not in {
            SEVERITY_CRITICAL,
            SEVERITY_WARNING,
            SEVERITY_INFO,
        }:
            raise RecordError("proposer finding: invalid identity or severity")
        measured = body.get("measured")
        if not isinstance(measured, list) or any(not isinstance(item, dict) for item in measured):
            raise RecordError("proposer finding: measured must be an array of objects")
        if "remedy" not in body:
            raise RecordError("proposer finding: missing remedy field")
        remedy = None if body["remedy"] is None else ProposerRemedy.from_json(body["remedy"])
        return cls(
            **{key: body[key] for key in fields},
            measured=tuple(measured),
            remedy=remedy,
            _json=stored,
        )


@dataclass(frozen=True, slots=True)
class ProposerReflection:
    """One persisted recommend-only pass."""

    reflection_id: str
    epoch_id: str
    created_at: str
    investigation_source: str
    findings: tuple[ProposerFinding, ...] = ()
    investigation: dict[str, Any] | None = None
    _json: str | None = field(default=None, repr=False, compare=False)

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            **(json.loads(self._json) if self._json is not None else {}),
            "reflection_id": self.reflection_id,
            "epoch_id": self.epoch_id,
            "created_at": self.created_at,
            "investigation_source": self.investigation_source,
            "findings": [f.to_json() for f in self.findings],
        }
        if self.investigation is not None:
            payload["investigation"] = dict(self.investigation)
        else:
            payload.pop("investigation", None)
        return payload

    @classmethod
    def from_json(cls, body: Any) -> ProposerReflection:
        stored = _body(body, "proposer reflection")
        fields = ("reflection_id", "epoch_id", "created_at", "investigation_source")
        _strings(body, fields)
        for key in ("reflection_id", "epoch_id"):
            value = body[key]
            if (
                not value
                or value in {".", ".."}
                or "/" in value
                or "\\" in value
                or "\x00" in value
            ):
                raise RecordError(f"proposer reflection: invalid {key}")
        raw = body.get("findings")
        if not isinstance(raw, list):
            raise RecordError("proposer reflection: findings must be an array")
        findings = tuple(ProposerFinding.from_json(item) for item in raw)
        if len({item.finding_id for item in findings}) != len(findings):
            raise RecordError("proposer reflection: duplicate finding identity")
        investigation = body.get("investigation")
        if "investigation" in body:
            if not isinstance(investigation, dict):
                raise RecordError("proposer reflection: investigation must be an object")
            if (
                investigation.get("epoch_id") != body["epoch_id"]
                or investigation.get("source") != body["investigation_source"]
            ):
                raise RecordError(
                    "proposer reflection: investigation identity disagrees with the record"
                )
            if (
                not isinstance(investigation.get("card"), dict)
                or not isinstance(investigation.get("history"), list)
                or any(not isinstance(item, dict) for item in investigation["history"])
            ):
                raise RecordError("proposer reflection: invalid investigation card or history")
        return cls(
            **{key: body[key] for key in fields},
            findings=findings,
            investigation=investigation,
            _json=stored,
        )


def _body(body: Any, name: str) -> str:
    if not isinstance(body, dict):
        raise RecordError(f"{name}: expected a JSON object")
    assert_redacted(body, where=name)
    try:
        return json.dumps(body, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise RecordError(f"{name}: invalid JSON value") from exc


def _strings(body: dict[str, Any], fields: tuple[str, ...]) -> None:
    for key in fields:
        if not isinstance(body.get(key), str):
            raise RecordError(f"proposer recommendation: {key} must be a string")


def read_reflection(
    workspace_root: Path, epoch_id: str, reflection_id: str
) -> ProposerReflection | None:
    """Read one accepted collection, returning None only for an absent file."""
    path = proposer_reflection_findings_path(workspace_root, epoch_id, reflection_id)
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, ValueError) as exc:
        raise RecordError(f"proposer reflection {path}: unreadable JSON") from exc
    record = ProposerReflection.from_json(body)
    if record.epoch_id != epoch_id or record.reflection_id != reflection_id:
        raise RecordError(
            f"proposer reflection {path}: record identity disagrees with its location"
        )
    return record


def write_reflection(workspace_root: Path, reflection: ProposerReflection) -> Path:
    """Validate the complete collection before atomically publishing its exact encoding."""
    payload = reflection.to_json()
    accepted = ProposerReflection.from_json(payload)
    path = proposer_reflection_findings_path(
        workspace_root, accepted.epoch_id, accepted.reflection_id
    )
    atomic_write_json(path, payload)
    return path


def epoch_ids_newest_first(workspace_root: Path) -> list[str]:
    """Enumerate record locations independently of epoch configuration readability."""
    return list(reversed(list_epoch_ids(WorkspaceLayout.from_root(workspace_root))))


def list_reflections(workspace_root: Path, epoch_id: str) -> list[ProposerReflection]:
    """Read every collection newest first; a malformed present collection refuses the list."""
    base = proposer_reflections_dir(workspace_root, epoch_id)
    if not base.is_dir():
        return []
    records = []
    for child in sorted(base.iterdir(), reverse=True):
        if child.is_dir():
            record = read_reflection(workspace_root, epoch_id, child.name)
            if record is not None:
                records.append(record)
    return records


def read_finding(
    workspace_root: Path, finding_id: str, *, epoch_id: str | None = None
) -> tuple[str, str, ProposerFinding] | None:
    """Resolve a content-stable finding ID to its newest accepted recorded copy."""
    epochs = [epoch_id] if epoch_id is not None else epoch_ids_newest_first(workspace_root)
    for eid in epochs:
        for record in list_reflections(workspace_root, eid):
            for finding in record.findings:
                if finding.finding_id == finding_id:
                    return eid, record.reflection_id, finding
    return None
