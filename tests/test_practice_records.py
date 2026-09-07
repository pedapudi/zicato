"""Practice records retain observations and reject contradictory verdict summaries."""

import json
from dataclasses import replace

import pytest

from zicato.core.workspace import reflection_practices_path
from zicato.epoch._storage import RecordError
from zicato.query.paths import WorkspacePaths
from zicato.query.reflection_view import build_practice_review
from zicato.reflection import practices as owner


def _review():
    return owner.PracticeReview(
        (
            owner.PracticeCheck(
                check_id="oracle_mix",
                verdict="sound",
                headline="Structured oracles are present.",
                evidence={"count": 0, "scalar": 0.0},
                rationale="Structured judgments distinguish candidate behavior.",
            ),
        )
    )


def test_practice_bytes_typed_edits_and_verdict_counts(tmp_path):
    body = _review().to_json()
    body["extra"] = {"retained": 0}
    body["checks"][0]["extra"] = {"retained": 0}
    review = owner.PracticeReview.from_json(body)
    path = owner.write_practice_review(tmp_path, "epoch", "reflection", review)
    assert path.read_bytes() == json.dumps(body, indent=2, sort_keys=True).encode()
    decoded = owner.read_practice_review(tmp_path, "epoch", "reflection")
    assert decoded is not None
    updated = replace(decoded, checks=(replace(decoded.checks[0], verdict="attend"),))
    owner.write_practice_review(tmp_path, "epoch", "reflection", updated)
    stored = json.loads(path.read_text())
    assert stored["verdict_counts"] == {"sound": 0, "attend": 1, "unsound": 0, "unmeasured": 0}
    assert stored["checks"][0]["extra"] == {"retained": 0}
    assert type(stored["checks"][0]["evidence"]["count"]) is int
    assert type(stored["checks"][0]["evidence"]["scalar"]) is float


@pytest.mark.parametrize("defect", ["count", "boolean count", "unmeasured", "duplicate"])
def test_practice_record_rejects_inconsistent_facts(defect):
    body = _review().to_json()
    if defect == "count":
        body["verdict_counts"]["sound"] = 2
    elif defect == "boolean count":
        body["verdict_counts"]["sound"] = True
    elif defect == "unmeasured":
        body["checks"][0]["verdict"] = "unmeasured"
    else:
        body["checks"].append(body["checks"][0])
        body["verdict_counts"]["sound"] = 2
    with pytest.raises(RecordError):
        owner.PracticeReview.from_json(body)


def test_corrupt_practice_summary_is_unreadable_in_query(tmp_path):
    path = reflection_practices_path(tmp_path, "epoch", "reflection")
    path.parent.mkdir(parents=True)
    body = _review().to_json()
    body["verdict_counts"]["unsound"] = 1
    path.write_text(json.dumps(body))
    view = build_practice_review(WorkspacePaths(tmp_path), "reflection")
    assert view["found"] is False
    assert view["unreadable"] is True
    assert "verdict_counts" in view["note"]


def test_absent_empty_and_null_practice_records(tmp_path):
    assert owner.read_practice_review(tmp_path, "epoch", "reflection") is None
    path = owner.write_practice_review(tmp_path, "epoch", "reflection", owner.PracticeReview())
    view = build_practice_review(WorkspacePaths(tmp_path), "reflection")
    assert view["found"] is True and view["checks"] == []
    path.write_text("null")
    with pytest.raises(RecordError):
        owner.read_practice_review(tmp_path, "epoch", "reflection")
