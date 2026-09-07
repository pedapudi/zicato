from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

from zicato.tournament.artifacts import artifact_paths, capture_run_artifacts


def test_capture_inventories_unknown_nested_files_deterministically(tmp_path: Path) -> None:
    scratch = tmp_path / "scratch"
    (scratch / "z").mkdir(parents=True)
    (scratch / "z" / "page.html").write_text("<h1>result</h1>", encoding="utf-8")
    (scratch / "raw.bin").write_bytes(b"\x00\x01")

    captured = capture_run_artifacts(scratch, tmp_path / "run" / "loss.json")

    assert [file.path for file in captured.files] == ["raw.bin", "z/page.html"]
    assert captured.files[0].sha256 == hashlib.sha256(b"\x00\x01").hexdigest()
    assert captured.files[1].media_type == "text/html"
    assert (captured.root / "z" / "page.html").read_text(encoding="utf-8") == "<h1>result</h1>"
    manifest = json.loads(captured.manifest_path.read_text(encoding="utf-8"))
    assert [file["path"] for file in manifest["files"]] == ["raw.bin", "z/page.html"]
    assert not any(
        str(tmp_path) in value for value in captured.manifest_path.read_text().splitlines()
    )


def test_capture_is_replicate_keyed_and_replaces_stale_tree(tmp_path: Path) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (scratch / "first.txt").write_text("first", encoding="utf-8")
    loss = tmp_path / "loss.r2.json"
    first = capture_run_artifacts(scratch, loss)
    (scratch / "first.txt").unlink()
    (scratch / "second.txt").write_text("second", encoding="utf-8")
    second = capture_run_artifacts(scratch, loss)

    assert artifact_paths(loss) == (tmp_path / "artifacts.r2", tmp_path / "artifacts.r2.json")
    assert first.root == second.root
    assert not (second.root / "first.txt").exists()
    assert (second.root / "second.txt").exists()


def test_capture_bounds_are_deterministic(tmp_path: Path) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (scratch / "a.txt").write_text("aa", encoding="utf-8")
    (scratch / "b.txt").write_text("bb", encoding="utf-8")

    captured = capture_run_artifacts(scratch, tmp_path / "loss.json", max_files=1)
    manifest = json.loads(captured.manifest_path.read_text(encoding="utf-8"))

    assert [file.path for file in captured.files] == ["a.txt"]
    assert captured.truncated is True
    assert manifest["skipped"] == [{"path": "b.txt", "reason": "capture_limit"}]


def test_capture_does_not_follow_symlinks(tmp_path: Path) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    try:
        (scratch / "link.txt").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")

    captured = capture_run_artifacts(scratch, tmp_path / "loss.json")
    manifest = json.loads(captured.manifest_path.read_text(encoding="utf-8"))

    assert captured.files == ()
    assert manifest["skipped"] == [{"path": "link.txt", "reason": "unsupported_file_type"}]


def test_manifest_sync_failure_preserves_the_committed_record(tmp_path, monkeypatch):
    from zicato.tournament.artifacts import _write_manifest

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (scratch / "report.txt").write_text("report")
    captured = capture_run_artifacts(scratch, tmp_path / "loss.json")
    original = captured.manifest_path.read_bytes()
    assert stat.S_IMODE(captured.manifest_path.stat().st_mode) == 0o600
    payload = json.loads(original)
    payload["extension"] = "pending publication"

    def fail_sync(_descriptor):
        raise OSError("injected manifest sync failure")

    monkeypatch.setattr(os, "fsync", fail_sync)
    with pytest.raises(OSError, match="injected manifest sync failure"):
        _write_manifest(captured.manifest_path, payload)

    assert captured.manifest_path.read_bytes() == original
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("defect", ["path", "version", "size", "provenance", "symlink"])
def test_transcript_refuses_untrusted_artifact_manifest(tmp_path: Path, defect: str) -> None:
    from zicato.core.measurement import MeasurementDraw
    from zicato.query.transcript_view import _add_run_artifacts
    from zicato.telemetry.reducer import write_loss_profile
    from zicato.testing.fixtures import make_loss_profile

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (scratch / "report.txt").write_text("report", encoding="utf-8")
    loss_path = tmp_path / "run" / "loss.json"
    captured = capture_run_artifacts(scratch, loss_path)
    body = json.loads(captured.manifest_path.read_text())
    if defect == "path":
        body["files"][0]["path"] = "../outside.txt"
    elif defect == "version":
        body["format_version"] = True
    elif defect == "size":
        body["files"][0]["size"] = "6"
    elif defect == "provenance":
        write_loss_profile(
            make_loss_profile(
                run_id="selected", measurement=MeasurementDraw.from_index(0, base_seed=17)
            ),
            loss_path,
        )
        body.update(
            measurement=MeasurementDraw.from_index(0, base_seed=19).to_json(), run_id="other"
        )
    elif defect == "symlink":
        outside = tmp_path / "outside.txt"
        (captured.root / "report.txt").rename(outside)
        (captured.root / "report.txt").symlink_to(outside)
    captured.manifest_path.write_text(json.dumps(body), encoding="utf-8")
    raw_manifest = captured.manifest_path.read_bytes()
    payload = {"turns": [{"text": "retained"}]}

    _add_run_artifacts(payload, loss_path.with_name("events.jsonl"))

    assert not payload.get("execution", {}).get("nodes")
    assert "artifact manifest" in payload.get("error", "")
    assert payload["turns"] == [{"text": "retained"}]
    assert captured.manifest_path.read_bytes() == raw_manifest


def test_manifest_codec_preserves_extensions_and_refuses_incomplete_inventory(
    tmp_path: Path,
) -> None:
    from zicato.core.measurement import MeasurementDraw
    from zicato.tournament.artifacts import artifact_manifest_from_payload, read_artifact_manifest

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (scratch / "a.txt").write_bytes(b"a")
    (scratch / "b.txt").write_bytes(b"b")
    loss_path = tmp_path / "loss.json"
    assert read_artifact_manifest(loss_path) is None
    captured = capture_run_artifacts(scratch, loss_path, max_files=1)
    body = json.loads(captured.manifest_path.read_text())
    body["extension"] = {"label": ["retained", None, 2.5]}
    body["files"][0]["extension"] = True
    body["skipped"][0]["extension"] = 3
    captured.manifest_path.write_text(json.dumps(body), encoding="utf-8")
    assert artifact_manifest_from_payload(body) == body
    assert read_artifact_manifest(loss_path) == body
    original = captured.manifest_path.read_bytes()
    with pytest.raises(ValueError, match="conflicts"):
        capture_run_artifacts(
            scratch, loss_path, measurement=MeasurementDraw.from_index(2), run_id="wrong-slot"
        )
    assert captured.manifest_path.read_bytes() == original
    body["total_bytes"] += 1
    with pytest.raises(ValueError, match="total"):
        artifact_manifest_from_payload(body)
    assert captured.manifest_path.read_bytes() == original
    captured.manifest_path.write_bytes(b'{"format_version":')
    with pytest.raises(ValueError, match="unreadable artifact manifest"):
        read_artifact_manifest(loss_path)
    assert captured.manifest_path.read_bytes() == b'{"format_version":'


def test_artifact_reader_selects_replicate_and_retained_attempt_provenance(tmp_path: Path) -> None:
    from zicato.core.measurement import MeasurementDraw
    from zicato.query.transcript_view import _add_run_artifacts
    from zicato.telemetry.reducer import write_loss_profile
    from zicato.testing.fixtures import make_loss_profile
    from zicato.tournament.artifacts import archive_unit_artifacts, read_artifact_manifest

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    loss_paths = []
    for index, name in [(0, "ordinary.txt"), (2, "replicate.txt")]:
        (scratch / name).write_text(name, encoding="utf-8")
        loss_path = tmp_path / "seed-17" / ("loss.json" if index == 0 else "loss.r2.json")
        draw = MeasurementDraw.from_index(index, base_seed=17)
        loss = make_loss_profile(run_id=f"selected-{index}", measurement=draw)
        captured = capture_run_artifacts(scratch, loss_path, measurement=draw, run_id=loss.run_id)
        write_loss_profile(loss, loss_path)
        events = loss_path.with_name("events.jsonl" if index == 0 else "events.r2.jsonl")
        events.write_bytes(b"{}\n")
        payload = {}
        _add_run_artifacts(payload, events)
        assert [item["name"] for item in payload["execution"]["nodes"]] == [name]
        assert read_artifact_manifest(loss_path, expected=loss)["files"][0]["path"] == name
        (scratch / name).unlink()
        loss_paths.append(loss_path)
    original = captured.manifest_path.read_bytes()
    archive = archive_unit_artifacts(loss_paths[1])
    assert archive is not None
    archived_loss = archive / "loss.r2.json"
    assert read_artifact_manifest(archived_loss, expected=loss)["run_id"] == "selected-2"
    wrong = make_loss_profile(
        run_id="selected-2", measurement=MeasurementDraw.from_index(2, base_seed=19)
    )
    with pytest.raises(ValueError, match="provenance"):
        read_artifact_manifest(archived_loss, expected=wrong)
    assert (archive / "artifacts.r2.json").read_bytes() == original
    payload = {}
    _add_run_artifacts(payload, archive / "events.r2.jsonl")
    assert [item["name"] for item in payload["execution"]["nodes"]] == ["replicate.txt"]
    assert artifact_paths(loss_paths[0])[1].exists()


@pytest.mark.parametrize("loss_bytes", [None, b"{"])
def test_transcript_distinguishes_absent_and_invalid_paired_loss(
    tmp_path: Path, loss_bytes: bytes | None
) -> None:
    from zicato.core.measurement import MeasurementDraw
    from zicato.query.paths import WorkspacePaths
    from zicato.query.transcript_view import build_run_transcript, build_run_transcript_delta

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (scratch / "report.txt").write_bytes(b"report")
    loss_path = tmp_path / "epochs/e1/generations/v0/runs/task/loss.json"
    captured = capture_run_artifacts(
        scratch, loss_path, measurement=MeasurementDraw.from_index(0, base_seed=17), run_id="run"
    )
    manifest_bytes = captured.manifest_path.read_bytes()
    loss_path.with_name("events.jsonl").write_text(
        json.dumps({"kind": "task_completed", "payload": {"summary": "retained turn"}}) + "\n"
    )
    if loss_bytes is not None:
        loss_path.write_bytes(loss_bytes)

    for reader in (build_run_transcript, build_run_transcript_delta):
        payload = reader(WorkspacePaths(tmp_path), "e1", "v0", "task")
        assert payload["turns"][0]["text"] == "retained turn"
        artifacts = [node for node in payload["execution"]["nodes"] if node["kind"] == "artifact"]
        if loss_bytes is None:
            assert "error" not in payload
            assert [node["name"] for node in artifacts] == ["report.txt"]
        else:
            assert artifacts == []
            assert "paired loss unavailable" in payload["error"]
            assert str(loss_path) in payload["error"]
        if "verbatim_available" in payload:
            assert payload["verbatim_available"] is False
    assert captured.manifest_path.read_bytes() == manifest_bytes
    assert (captured.root / "report.txt").read_bytes() == b"report"
    assert not loss_path.exists() if loss_bytes is None else loss_path.read_bytes() == loss_bytes
