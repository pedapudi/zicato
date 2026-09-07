"""Accepted judge adjudication facts and their durable JSON representation."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from zicato.epoch._storage import RecordError, check_record_format
from zicato.reflection.corpus import FIDELITY_PREVIEW, FIDELITY_RESULT, FIDELITY_VERBATIM
from zicato.storage import atomic_write_json

ADJUDICATION_FORMAT_VERSION: int = 1

# Verdict vocabulary (the confusion-matrix cells + the excluded-from-rates pile).
VERDICT_TP: str = "TP"
VERDICT_FP: str = "FP"
VERDICT_FN: str = "FN"
VERDICT_TN: str = "TN"
VERDICT_AMBIGUOUS: str = "ambiguous"

# Observed / adjudicated vocabulary.
OBSERVED_FIRED: str = "fired"
OBSERVED_SILENT: str = "silent"
ADJUDICATED_SHOULD_FIRE: str = "should_fire"
ADJUDICATED_SHOULD_BE_SILENT: str = "should_be_silent"
ADJUDICATED_AMBIGUOUS: str = "ambiguous"


@dataclass(frozen=True, slots=True)
class JudgeAdjudication:
    """One judge decision and its independently adjudicated verdict.

    ``observed`` records the judge's decision; ``adjudicated`` records the
    independent conclusion. Their join determines ``verdict``. Fidelity and
    protocol fields identify the evidence used, while ``raw_response`` retains
    the response text when a verdict could not be parsed.
    """

    judge_name: str
    run_ref: str
    observed: str
    adjudicated: str
    verdict: str
    severity_match: bool | None
    evidence_span: str
    meta_judge_rationale: str
    meta_judge_model: str
    adjudicator_self_agreement: float | None
    operator_confirmed: bool | None
    fidelity: str
    prompt_version: int
    k_adj: int
    raw_response: str | None = None
    _json: str | None = field(default=None, repr=False, compare=False)

    def to_json(self) -> dict[str, Any]:
        """The persisted ``adjudication/{judge}/{run_ref}.json`` shape."""
        fields = {
            "format_version": ADJUDICATION_FORMAT_VERSION,
            "judge_name": self.judge_name,
            "run_ref": self.run_ref,
            "observed": self.observed,
            "adjudicated": self.adjudicated,
            "verdict": self.verdict,
            "severity_match": self.severity_match,
            "evidence_span": self.evidence_span,
            "meta_judge_rationale": self.meta_judge_rationale,
            "meta_judge_model": self.meta_judge_model,
            "adjudicator_self_agreement": self.adjudicator_self_agreement,
            "operator_confirmed": self.operator_confirmed,
            "fidelity": self.fidelity,
            "prompt_version": self.prompt_version,
            "k_adj": self.k_adj,
            "raw_response": self.raw_response,
        }
        if self._json is None:
            return fields
        stored: dict[str, Any] = json.loads(self._json)
        defaults = {
            "evidence_span": "",
            "meta_judge_rationale": "",
            "meta_judge_model": "",
            "fidelity": FIDELITY_PREVIEW,
            "prompt_version": 0,
            "k_adj": 0,
        }
        stored.update(
            (key, value)
            for key, value in fields.items()
            if key in stored or value != defaults.get(key)
        )
        return stored

    @classmethod
    def from_json(cls, data: Any) -> JudgeAdjudication:
        """Accept one verdict, retaining absent protocol fields as stale evidence."""
        if not isinstance(data, dict):
            raise RecordError("adjudication: expected a JSON object")
        check_record_format(
            data, "adjudication", expected_version=ADJUDICATION_FORMAT_VERSION, allow_missing=False
        )
        for key in ("judge_name", "run_ref"):
            if not isinstance(data.get(key), str) or not data[key]:
                raise RecordError(f"adjudication: {key} must be a nonempty string")
        vocabularies = {
            "observed": (OBSERVED_FIRED, OBSERVED_SILENT),
            "adjudicated": (
                ADJUDICATED_SHOULD_FIRE,
                ADJUDICATED_SHOULD_BE_SILENT,
                ADJUDICATED_AMBIGUOUS,
            ),
            "verdict": (VERDICT_TP, VERDICT_FP, VERDICT_FN, VERDICT_TN, VERDICT_AMBIGUOUS),
        }
        for key, values in vocabularies.items():
            if data.get(key) not in values:
                raise RecordError(f"adjudication: invalid {key} {data.get(key)!r}")
        if data["verdict"] != classify_verdict(data["observed"], data["adjudicated"]):
            raise RecordError("adjudication: verdict disagrees with observed and adjudicated")
        for key in ("evidence_span", "meta_judge_rationale", "meta_judge_model"):
            if key in data and not isinstance(data[key], str):
                raise RecordError(f"adjudication: {key} must be a string")
        for key in ("severity_match", "operator_confirmed"):
            if data.get(key) is not None and not isinstance(data[key], bool):
                raise RecordError(f"adjudication: {key} must be a boolean or null")
        agreement = data.get("adjudicator_self_agreement")
        if agreement is not None and (
            isinstance(agreement, bool)
            or not isinstance(agreement, int | float)
            or not math.isfinite(agreement)
            or not 0 <= agreement <= 1
        ):
            raise RecordError("adjudication: adjudicator_self_agreement must be in [0, 1] or null")
        fidelity = data.get("fidelity", FIDELITY_PREVIEW)
        if fidelity not in (FIDELITY_PREVIEW, FIDELITY_RESULT, FIDELITY_VERBATIM):
            raise RecordError(f"adjudication: invalid fidelity {fidelity!r}")
        for key in ("prompt_version", "k_adj"):
            value = data.get(key, 0)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise RecordError(f"adjudication: {key} must be a nonnegative integer")
        if data.get("raw_response") is not None and not isinstance(data["raw_response"], str):
            raise RecordError("adjudication: raw_response must be a string or null")
        try:
            encoded = json.dumps(data, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise RecordError(f"adjudication: invalid JSON value: {exc}") from exc
        return cls(
            judge_name=data["judge_name"],
            run_ref=data["run_ref"],
            observed=data["observed"],
            adjudicated=data["adjudicated"],
            verdict=data["verdict"],
            severity_match=data.get("severity_match"),
            evidence_span=data.get("evidence_span", ""),
            meta_judge_rationale=data.get("meta_judge_rationale", ""),
            meta_judge_model=data.get("meta_judge_model", ""),
            adjudicator_self_agreement=agreement,
            operator_confirmed=data.get("operator_confirmed"),
            fidelity=fidelity,
            prompt_version=data.get("prompt_version", 0),
            k_adj=data.get("k_adj", 0),
            raw_response=data.get("raw_response"),
            _json=encoded,
        )


def read_adjudication(path: Path) -> JudgeAdjudication | None:
    """Read accepted verdict facts; only an absent file is a cache miss."""
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise RecordError(f"adjudication {path}: {exc}") from exc
    record = JudgeAdjudication.from_json(body)
    if record.judge_name != path.parent.name or record.run_ref != path.stem:
        raise RecordError(f"adjudication {path}: recorded identity does not match its location")
    return record


def write_adjudication(path: Path, adjudication: JudgeAdjudication) -> Path:
    """Validate and durably publish one verdict without changing recorded fields."""
    record = JudgeAdjudication.from_json(adjudication.to_json())
    if record.judge_name != path.parent.name or record.run_ref != path.stem:
        raise RecordError(f"adjudication {path}: recorded identity does not match its location")
    atomic_write_json(path, record.to_json())
    return path


def classify_verdict(observed: str, adjudicated: str) -> str:
    """Join observed × adjudicated into the confusion-matrix verdict."""
    if adjudicated == ADJUDICATED_AMBIGUOUS:
        return VERDICT_AMBIGUOUS
    if observed == OBSERVED_FIRED:
        return VERDICT_TP if adjudicated == ADJUDICATED_SHOULD_FIRE else VERDICT_FP
    return VERDICT_FN if adjudicated == ADJUDICATED_SHOULD_FIRE else VERDICT_TN
