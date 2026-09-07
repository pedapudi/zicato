"""Suggestion consumers share strict collection acceptance and preserve stored facts."""

import json
from dataclasses import replace

import pytest

from tests._workspace_support import workspace, write_epoch
from zicato.builder.api import _read_suggestions_feed
from zicato.core.workspace import reflection_suggestions_path
from zicato.epoch._storage import RecordError
from zicato.query.paths import WorkspacePaths
from zicato.query.trace_view import (
    build_suggestion_provenance,
    build_trace_detail,
    build_trace_list,
)
from zicato.reflection import suggestions as owner
from zicato.reflection.apply import find_suggestion


def _suggestion():
    return owner.Suggestion(
        suggestion_id="suggestion",
        suggestion_type="regression_entry",
        artifact_kind="board_entry",
        subject="entry",
        summary="Retain a regression task.",
        rationale="The candidate failed this task.",
        target_slice="train",
        draft_artifact={"id": "entry"},
        admission={"count": 0, "rate": 0.0},
    )


def test_suggestion_bytes_ranking_and_typed_edits(tmp_path):
    raw = _suggestion().to_json()
    raw["extra"] = {"retained": 0}
    record = owner.Suggestion.from_json(raw)
    second = replace(record, suggestion_id="earlier", severity_rank=2)
    path = owner.write_suggestions(tmp_path, "epoch", "reflection", [record, second])
    expected = {"reflection_id": "reflection", "suggestions": [second.to_json(), record.to_json()]}
    assert path.read_bytes() == json.dumps(expected, indent=2, sort_keys=True).encode()
    decoded = owner.read_suggestions(tmp_path, "epoch", "reflection")
    assert [suggestion.suggestion_id for suggestion in decoded] == ["earlier", "suggestion"]
    updated = replace(decoded[0], summary="Review this regression.")
    owner.write_suggestions(tmp_path, "epoch", "reflection", [updated])
    body = json.loads(path.read_text())["suggestions"][0]
    assert body["summary"] == "Review this regression."
    assert body["extra"] == {"retained": 0}
    assert type(body["admission"]["count"]) is int
    assert type(body["admission"]["rate"]) is float


@pytest.mark.parametrize("defect", ["rank", "artifact", "kind", "admission"])
def test_suggestion_codec_refuses_coercion(defect):
    body = _suggestion().to_json()
    if defect == "rank":
        body["severity_rank"] = True
    elif defect == "artifact":
        body["draft_artifact"] = []
    elif defect == "kind":
        body["artifact_kind"] = "unknown"
    else:
        body["admission"] = "unmeasured"
    with pytest.raises(RecordError):
        owner.Suggestion.from_json(body)


def test_malformed_collection_cannot_expose_one_suggestion(tmp_path):
    path = owner.write_suggestions(tmp_path, "epoch", "reflection", [_suggestion()])
    body = json.loads(path.read_text())
    body["suggestions"].append("malformed member")
    path.write_text(json.dumps(body))
    with pytest.raises(RecordError):
        find_suggestion(tmp_path, "epoch", "reflection", "suggestion")


def test_null_suggestions_are_unreadable_in_inbox_and_trace_views(tmp_path):
    layout = workspace(tmp_path)
    write_epoch(layout, "epoch", current=True)
    path = reflection_suggestions_path(layout.root, "epoch", "reflection")
    path.parent.mkdir(parents=True)
    path.write_text("null")
    assert _read_suggestions_feed(layout.root)["unreadable"]
    paths = WorkspacePaths(layout.root)
    for view in (
        build_trace_list(paths, "reflection"),
        build_trace_detail(paths, "reflection", "trace"),
        build_suggestion_provenance(paths, "reflection", "suggestion"),
    ):
        assert view["found"] is False and view["unreadable"]


def test_suggestion_collection_identity_and_absence(tmp_path):
    assert owner.read_suggestions(tmp_path, "epoch", "reflection") == []
    path = owner.write_suggestions(tmp_path, "epoch", "reflection", [])
    assert owner.read_suggestions(tmp_path, "epoch", "reflection") == []
    path.write_text(json.dumps({"reflection_id": "other", "suggestions": []}))
    with pytest.raises(RecordError, match="identity"):
        owner.read_suggestions(tmp_path, "epoch", "reflection")
