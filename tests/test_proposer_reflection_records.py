"""Canonical recommendation acceptance before listing, preview, or application."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from zicato.epoch._storage import RecordError
from zicato.proposer.apply_recommendation import ApplyError, apply_recommendation
from zicato.proposer.reflection import pending_recommendations
from zicato.proposer.reflection_records import (
    ProposerFinding,
    ProposerReflection,
    ProposerRemedy,
    list_reflections,
    read_reflection,
    write_reflection,
)
from zicato.query.paths import WorkspacePaths
from zicato.query.proposer_view import build_proposer_recommendations


def _reflection() -> ProposerReflection:
    text = "Keep the declared import block.\n"
    remedy = ProposerRemedy(
        "skill_add", "skills/imports.md", text, hashlib.sha256(text.encode()).hexdigest()
    )
    finding = ProposerFinding(
        "prec-imports",
        "warning",
        "Preserve imports",
        "Repeated import failures",
        "six proposals",
        ({"failures": 6, "rate": 1.0},),
        "prior failure band ~30%",
        remedy,
        "Only proposer guidance changes",
    )
    return ProposerReflection(
        "prefl-check", "e1", "2024-01-02T00:00:00+00:00", "scorecard", (finding,)
    )


def test_absent_empty_and_malformed_are_distinct(tmp_path: Path) -> None:
    assert read_reflection(tmp_path, "e1", "prefl-check") is None
    assert list_reflections(tmp_path, "e1") == []
    record = replace(_reflection(), findings=())
    path = write_reflection(tmp_path, record)
    assert read_reflection(tmp_path, "e1", "prefl-check") == record
    assert pending_recommendations(tmp_path) == []
    path.write_text("null")
    with pytest.raises(RecordError, match="JSON object"):
        list_reflections(tmp_path, "e1")
    assert build_proposer_recommendations(WorkspacePaths(tmp_path))["unreadable"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("severity", "urgent"),
        ("finding_id", 1),
        ("measured", [None]),
        ("remedy", []),
    ],
)
def test_malformed_member_refuses_the_whole_collection(
    tmp_path: Path, field: str, value: Any
) -> None:
    path = write_reflection(tmp_path, _reflection())
    body = json.loads(path.read_text())
    damaged = {**body["findings"][0], "finding_id": "prec-damaged", field: value}
    body["findings"].append(damaged)
    path.write_text(json.dumps(body))
    with pytest.raises(RecordError):
        pending_recommendations(tmp_path)
    view = build_proposer_recommendations(WorkspacePaths(tmp_path))
    assert view["found"] is False and view["pending"] == [] and view["unreadable"]


def test_integrity_redaction_and_location_apply_before_any_write(tmp_path: Path) -> None:
    record = _reflection()
    path = write_reflection(tmp_path, record)
    original = json.loads(path.read_text())
    wrong_digest = json.loads(path.read_text())
    wrong_digest["findings"][0]["remedy"]["new_text"] += "changed"
    leaked = {**original, "metadata": {"entry_id": "hidden-board-entry"}}
    relocated = {**original, "epoch_id": "elsewhere"}
    duplicate = {**original, "findings": original["findings"] * 2}
    for body in (wrong_digest, leaked, relocated, duplicate):
        path.write_text(json.dumps(body))
        with pytest.raises(ApplyError):
            apply_recommendation(
                tmp_path, "prec-imports", proposer_path=tmp_path / "proposer", epoch_id="e1"
            )
        assert not (tmp_path / "proposer").exists()


def test_typed_edits_override_stored_values_without_losing_extensions(tmp_path: Path) -> None:
    body = _reflection().to_json()
    body["metadata"] = {"weight": 1}
    body["findings"][0]["extra"] = {"weight": 1.0}
    body["findings"][0]["remedy"]["extra"] = "preserve"
    decoded = ProposerReflection.from_json(body)
    assert json.dumps(decoded.to_json(), sort_keys=True) == json.dumps(body, sort_keys=True)
    finding = decoded.findings[0]
    assert finding.remedy is not None
    text = "Check every import.\n"
    remedy = replace(
        finding.remedy, new_text=text, sha256=hashlib.sha256(text.encode()).hexdigest()
    )
    changed = replace(decoded, findings=(replace(finding, title="Check imports", remedy=remedy),))
    written = json.loads(write_reflection(tmp_path, changed).read_text())
    assert written["findings"][0]["title"] == "Check imports"
    assert written["findings"][0]["remedy"]["new_text"] == text
    assert written["findings"][0]["remedy"]["extra"] == "preserve"
    assert type(written["metadata"]["weight"]) is int
    assert type(written["findings"][0]["extra"]["weight"]) is float
    assert "investigation" not in written
    invalid = replace(
        changed,
        findings=(replace(changed.findings[0], remedy=replace(remedy, new_text="unhashed")),),
    )
    path = write_reflection(tmp_path, changed)
    before = path.read_bytes()
    with pytest.raises(RecordError, match="integrity check"):
        write_reflection(tmp_path, invalid)
    assert path.read_bytes() == before


def test_owner_import_does_not_load_reflection_or_application() -> None:
    probe = (
        "import sys; import zicato.proposer.reflection_records; "
        "assert 'zicato.proposer.reflection' not in sys.modules; "
        "assert 'zicato.proposer.apply_recommendation' not in sys.modules"
    )
    subprocess.run([sys.executable, "-c", probe], check=True)


def test_apply_rechecks_filesystem_containment_after_record_acceptance(tmp_path: Path) -> None:
    write_reflection(tmp_path, _reflection())
    proposer = tmp_path / "proposer"
    outside = tmp_path / "outside"
    proposer.mkdir()
    outside.mkdir()
    (proposer / "skills").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ApplyError, match="symbolic link"):
        apply_recommendation(tmp_path, "prec-imports", proposer_path=proposer, epoch_id="e1")
    assert list(outside.iterdir()) == []
