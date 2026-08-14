from __future__ import annotations

import hashlib
import json
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
