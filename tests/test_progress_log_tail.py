"""The progress-log tail reads the last record without reading the whole log."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zicato.runtime import progress_log
from zicato.runtime.paths import progress_log_path
from zicato.storage.files import FileStorageBackend, last_jsonl_record


def _forbid_forward_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(self: FileStorageBackend, key: str) -> None:
        raise AssertionError(f"{key} was read from its start")

    monkeypatch.setattr(FileStorageBackend, "read_jsonl", refuse)


def test_large_progress_log_tail_reads_from_the_end(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A 200,000-event log yields its last event without a forward read."""
    path = progress_log_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 200_000
    with path.open("w", encoding="utf-8") as stream:
        for seq in range(1, count + 1):
            event = {"seq": seq, "ts": "2026-10-04T00:00:00Z", "type": "UnitSettled"}
            stream.write(json.dumps(event) + "\n")
        stream.write(
            json.dumps({"seq": count + 1, "ts": "2026-10-04T00:00:01Z", "type": "Settled"}) + "\n"
        )
    _forbid_forward_reads(monkeypatch)

    assert progress_log.tail_seq(tmp_path) == count + 1
    assert progress_log.tail_is_terminal(tmp_path)


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (b"", None),
        (b"\n\n  \n", None),
        (b'{"a": 1}\n{"a": 2}\n\n\n', {"a": 2}),
        (b'{"a": 1}\n  {"a": 3}  \n', {"a": 3}),
        (
            b'{"a": 1}\n' + json.dumps({"long": "x" * 50_000}).encode() + b"\n",
            {"long": "x" * 50_000},
        ),
        ('{"a": "é"}\n{"a": "ü"}\n'.encode(), {"a": "ü"}),
    ],
)
def test_last_record_matches_a_forward_read(
    tmp_path: Path, content: bytes, expected: object
) -> None:
    """Blank lines, surrounding spaces, a line longer than one block, multibyte text."""
    path = tmp_path / "s.jsonl"
    path.write_bytes(content)
    assert last_jsonl_record(path) == expected
    records = [json.loads(line) for line in content.decode().splitlines() if line.strip()]
    assert (records[-1] if records else None) == expected


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (b'{"a": 1}', None),
        (b'{"a": 1}\n{"a": ', {"a": 1}),
        (b'{"a": 1}\n{"a": 2}', {"a": 1}),
        (b'{"a": 1}\n\n{"a": 2', {"a": 1}),
        (b'{"a": 1}\n' + b"x" * 20_000, {"a": 1}),
    ],
)
def test_unterminated_final_line_is_ignored(
    tmp_path: Path, content: bytes, expected: object
) -> None:
    """Bytes after the last newline are an append in progress or a torn write."""
    path = tmp_path / "s.jsonl"
    path.write_bytes(content)
    assert last_jsonl_record(path) == expected


def test_absent_file_has_no_last_record(tmp_path: Path) -> None:
    assert last_jsonl_record(tmp_path / "absent.jsonl") is None


def test_progress_tail_during_an_append_reads_the_previous_event(tmp_path: Path) -> None:
    """A reader that races a progress append sees the last complete event."""
    path = progress_log_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"seq": 7, "ts": "2026-10-04T00:00:00Z", "type": "UnitSettled"})
        + "\n"
        + '{"seq": 8, "ts": "2026-10-04T00:00:01Z", "ty',
        encoding="utf-8",
    )
    assert progress_log.tail_seq(tmp_path) == 7
