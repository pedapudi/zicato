"""Canonical scorecards preserve bytes and refuse unavailable or contradictory evidence."""

import json
from dataclasses import replace

import pytest

from tests._reflection_support import scorecard_body
from tests.test_reflection_index_v11 import EPOCH, REFL, _write_reflection_files
from zicato.core.workspace import reflection_scorecards_path
from zicato.epoch._storage import RecordError
from zicato.index import query as iq
from zicato.index.ingest import ingest_reflection
from zicato.query.paths import WorkspacePaths
from zicato.query.reflection_view import build_judge_scorecards
from zicato.reflection import scorecards as owner


def _body():
    return {"reflection_id": REFL, "scorecards": [scorecard_body({"judge_name": "j", "tp": 2})]}


def test_scorecard_bytes_and_typed_edits_preserve_recorded_numbers(tmp_path):
    body = _body()
    body["scorecards"][0]["precision"] = 1
    body["extra"] = {"retained": 0}
    body["scorecards"][0]["extra"] = {"retained": 0}
    record = owner.Scorecards.from_json(body)
    path = owner.write_scorecards(tmp_path, EPOCH, record)
    assert path.read_bytes() == json.dumps(body, indent=2, sort_keys=True).encode()
    accepted = owner.read_scorecards(tmp_path, EPOCH, REFL)
    assert accepted is not None
    assert type(accepted.cards[0].precision) is int
    changed = replace(accepted, cards=(replace(accepted.cards[0], recommendation="Review judge"),))
    owner.write_scorecards(tmp_path, EPOCH, changed)
    assert json.loads(path.read_text())["scorecards"][0]["recommendation"] == "Review judge"
    assert json.loads(path.read_text())["extra"] == {"retained": 0}


@pytest.mark.parametrize(
    "defect", ["bool count", "count sum", "nonfinite", "duplicate", "bare list"]
)
def test_malformed_scorecards_refuse_the_whole_collection(defect):
    body = _body()
    if defect == "bool count":
        body["scorecards"][0]["tp"] = True
    elif defect == "count sum":
        body["scorecards"][0]["n_decisions"] = 3
    elif defect == "nonfinite":
        body["scorecards"][0]["precision"] = float("nan")
    elif defect == "duplicate":
        body["scorecards"].append(body["scorecards"][0])
    else:
        body = body["scorecards"]
    with pytest.raises(RecordError):
        owner.Scorecards.from_json(body)


def test_scorecard_mark_failure_prevents_publication(tmp_path, monkeypatch):
    record = owner.Scorecards.from_json(_body())
    path = owner.write_scorecards(tmp_path, EPOCH, record)
    prior = path.read_bytes()

    def fail_mark(*args):
        raise OSError("revision unavailable")

    monkeypatch.setattr(owner, "mark_epoch_changed", fail_mark)
    with pytest.raises(OSError, match="revision unavailable"):
        owner.write_scorecards(tmp_path, EPOCH, replace(record, cards=()))
    assert path.read_bytes() == prior


def test_absent_empty_and_corrupt_scorecards_cannot_reuse_index(tmp_path):
    _write_reflection_files(tmp_path, scorecards=[{"judge_name": "j", "tp": 2}])
    ingest_reflection(tmp_path, None, EPOCH, REFL)
    paths = WorkspacePaths(tmp_path)
    assert iq.judge_scorecards_for_reflection(paths.index_db, REFL)
    path = reflection_scorecards_path(tmp_path, EPOCH, REFL)
    for body in ({"reflection_id": REFL, "scorecards": []}, None):
        path.write_text(json.dumps(body))
        view = build_judge_scorecards(paths, REFL)
        assert view["judges"] == []
        if body is None:
            assert "unreadable" in view
            with pytest.raises(RecordError):
                ingest_reflection(tmp_path, None, EPOCH, REFL)
    assert iq.judge_scorecards_for_reflection(paths.index_db, REFL)
    # The canonical replacement and indexed row are retained before absence is tested.
    prior = path.read_bytes()
    assert prior == b"null"
    path.unlink()
    assert owner.read_scorecards(tmp_path, EPOCH, REFL) is None
    assert build_judge_scorecards(paths, REFL)["judges"] == []


def test_scorecard_identity_must_match_its_location(tmp_path):
    record = owner.Scorecards.from_json(_body())
    path = owner.write_scorecards(tmp_path, EPOCH, record)
    body = record.to_json()
    body["reflection_id"] = "other"
    path.write_text(json.dumps(body))
    with pytest.raises(RecordError, match="identity"):
        owner.read_scorecards(tmp_path, EPOCH, REFL)
