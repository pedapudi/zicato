"""Finding acceptance is shared by inspection, projection, and draft application."""

import json
from dataclasses import replace

import pytest

from tests._reflection_support import finding_body
from tests.test_reflection_index_v11 import EPOCH, REFL, _write_reflection_files
from zicato.core.workspace import reflection_findings_path
from zicato.epoch._storage import RecordError
from zicato.index import query as iq
from zicato.index.ingest import ingest_reflection
from zicato.query.paths import WorkspacePaths
from zicato.query.reflection_view import build_reflection_summary
from zicato.reflection import findings as owner
from zicato.reflection.apply import apply_finding_to_draft, find_finding


def _body():
    return {
        "reflection_id": REFL,
        "findings": [
            finding_body(
                {
                    "finding_id": "finding",
                    "evidence": [{"count": 0, "scalar": 0.0}],
                    "proposed_op": {"op": "set_gate", "args": {"promote_margin": 0.5}},
                }
            )
        ],
    }


def test_finding_bytes_and_typed_changes_preserve_nested_numbers(tmp_path):
    body = _body()
    body["extra"] = {"kept": 0}
    body["findings"][0]["extra"] = {"kept": 0}
    record = owner.Findings.from_json(body)
    path = owner.write_findings(tmp_path, EPOCH, record)
    assert path.read_bytes() == json.dumps(body, indent=2, sort_keys=True).encode()
    accepted = owner.read_findings(tmp_path, EPOCH, REFL)
    assert accepted is not None
    changed = replace(accepted, items=(replace(accepted.items[0], title="Review criterion"),))
    owner.write_findings(tmp_path, EPOCH, changed)
    stored = json.loads(path.read_text())
    assert stored["findings"][0]["title"] == "Review criterion"
    assert stored["findings"][0]["extra"] == {"kept": 0}
    assert type(stored["findings"][0]["evidence"][0]["count"]) is int
    assert type(stored["findings"][0]["evidence"][0]["scalar"]) is float


@pytest.mark.parametrize("defect", ["severity", "evidence", "operation", "duplicate", "bare list"])
def test_invalid_findings_refuse_the_collection(defect):
    body = _body()
    if defect == "severity":
        body["findings"][0]["severity"] = []
    elif defect == "evidence":
        body["findings"][0]["evidence"] = {"count": 0}
    elif defect == "operation":
        body["findings"][0]["proposed_op"]["args"] = []
    elif defect == "duplicate":
        body["findings"].append(body["findings"][0])
    else:
        body = body["findings"]
    with pytest.raises(RecordError):
        owner.Findings.from_json(body)


def test_invalid_collection_refuses_before_draft_creation(tmp_path, monkeypatch):
    from zicato.contract_draft.draft import DraftStore

    body = _body()
    body["findings"].append("corrupt member")
    path = reflection_findings_path(tmp_path, EPOCH, REFL)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(body))

    def refuse_fork(*args, **kwargs):
        pytest.fail("corrupt finding collection reached draft creation")

    monkeypatch.setattr(DraftStore, "fork", refuse_fork)
    with pytest.raises(RecordError):
        apply_finding_to_draft(
            workspace_root=tmp_path, epoch_id=EPOCH, reflection_id=REFL, finding_id="finding"
        )


def test_findings_absence_empty_corruption_and_index_refusal(tmp_path):
    assert owner.read_findings(tmp_path, EPOCH, REFL) is None
    _write_reflection_files(tmp_path, findings=[{"finding_id": "finding"}])
    ingest_reflection(tmp_path, None, EPOCH, REFL)
    paths = WorkspacePaths(tmp_path)
    before = iq.reflection_row(paths.index_db, REFL)
    owner.write_findings(tmp_path, EPOCH, owner.Findings(REFL, ()))
    assert build_reflection_summary(paths, REFL)["findings"] == []
    path = reflection_findings_path(tmp_path, EPOCH, REFL)
    path.write_text("null")
    assert build_reflection_summary(paths, REFL)["unreadable"] is True
    with pytest.raises(RecordError):
        ingest_reflection(tmp_path, None, EPOCH, REFL)
    assert iq.reflection_row(paths.index_db, REFL) == before
    with pytest.raises(RecordError):
        find_finding(tmp_path, EPOCH, REFL, "finding")


def test_finding_mark_failure_prevents_canonical_replacement(tmp_path, monkeypatch):
    record = owner.Findings.from_json(_body())
    path = owner.write_findings(tmp_path, EPOCH, record)
    before = path.read_bytes()

    def fail_mark(*args):
        raise OSError("revision unavailable")

    monkeypatch.setattr(owner, "mark_epoch_changed", fail_mark)
    with pytest.raises(OSError, match="revision unavailable"):
        owner.write_findings(tmp_path, EPOCH, replace(record, items=()))
    assert path.read_bytes() == before
