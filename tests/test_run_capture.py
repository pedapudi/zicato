"""Tests for the board-reflection capture fix (result.json + judge_io.jsonl).

Two run artifacts close BOARD-REFLECTION.md's capture gap:

* ``result.json`` — the worker persists each run's user-facing
  :class:`~zicato.core.RunResult` beside ``loss.json``, replicate-slotted
  (``result.r{n}.json``), atomic, and STRICTLY best-effort;
* ``judge_io.jsonl`` — an append-only sidecar retaining every inline
  judge ``evaluate`` call's verbatim I/O, emitted through the
  :mod:`zicato.judge_runtime.io_capture` sink seam.

Both are governed by always-on-with-opt-out RuntimeConfig knobs
(``persist_run_results`` / ``persist_judge_io``) that are runtime-only
and never contract-hashed. The load-bearing invariants pinned here:

* the worker writes result.json on a clean exit AND on its own
  cooperative budget abort (the synthesized RunResult persists too);
* replicate slotting mirrors the loss slotting exactly;
* with both knobs OFF the worker writes NO new files and its loss.json
  is byte-identical to a knobs-on run's (the byte-identical proof);
* every text field is clipped with the ``clipped`` flag set;
* the judge sink records one line per evaluate call, with the sha256 of
  the UNCLIPPED input and the scripted raw response verbatim;
* an unwritable capture path never changes loss.json or the exit code
  (the best-effort proof);
* readers tolerate missing / old-version / garbage files;
* the test-retest path emits judge_io when a sink-wired live judge is
  replayed (free coverage from the shared builder seam).
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from zicato.core import RunResult
from zicato.core.workspace import events_jsonl_path, loss_profile_path, run_result_path
from zicato.judge_runtime.io_capture import (
    JUDGE_IO_CLIP_CHARS,
    JUDGE_IO_CLIP_MARKER,
    JUDGE_IO_FORMAT_VERSION,
    JudgeIOFileSink,
    build_judge_io_record,
    judge_io_path_for_loss,
    read_judge_io,
)
from zicato.tournament.unit_cache import (
    RUN_RESULT_CLIP_CHARS,
    RUN_RESULT_CLIP_MARKER,
    RUN_RESULT_FORMAT_VERSION,
    read_run_result,
    run_result_to_payload,
    unit_result_path,
)

# ---------------------------------------------------------------------------
# Pure path math — replicate slotting mirrors the loss slotting
# ---------------------------------------------------------------------------


def test_unit_result_path_mirrors_loss_slotting() -> None:
    """loss.json -> result.json; loss.r{n}.json -> result.r{n}.json."""
    base = Path("/ws/epochs/e0/generations/v1/runs/entry_a")
    assert unit_result_path(base / "loss.json") == base / "result.json"
    assert unit_result_path(base / "loss.r1.json") == base / "result.r1.json"
    assert unit_result_path(base / "loss.r4000.json") == base / "result.r4000.json"


def test_judge_io_path_mirrors_loss_slotting() -> None:
    """loss.json -> judge_io.jsonl; loss.r{n}.json -> judge_io.r{n}.jsonl."""
    base = Path("/ws/epochs/e0/generations/v1/runs/entry_a")
    assert judge_io_path_for_loss(base / "loss.json") == base / "judge_io.jsonl"
    assert judge_io_path_for_loss(base / "loss.r2.json") == base / "judge_io.r2.jsonl"


def test_run_result_path_is_the_canonical_slot(tmp_path: Path) -> None:
    """The core read twin resolves to run_dir/result.json (replicate 0)."""
    got = run_result_path(tmp_path, "e0", "v1", "entry_a")
    assert got == loss_profile_path(tmp_path, "e0", "v1", "entry_a").with_name("result.json")


# ---------------------------------------------------------------------------
# result.json payload — round-trip, clip guard, tolerant reader
# ---------------------------------------------------------------------------


def _run_result(**overrides: Any) -> RunResult:
    fields: dict[str, Any] = {
        "run_id": "v1--entry_a",
        "entry_id": "entry_a",
        "final_output": "the answer",
        "transcript": ("turn one", "the answer"),
        "runtime_ms": 123,
        "aborted": False,
        "abort_reason": "",
    }
    fields.update(overrides)
    return RunResult(**fields)


def test_run_result_payload_round_trips(tmp_path: Path) -> None:
    """Write the payload atomically; the tolerant reader returns it intact."""
    from zicato.storage import atomic_write_json

    payload = run_result_to_payload(_run_result())
    assert payload["format_version"] == RUN_RESULT_FORMAT_VERSION
    assert payload["clipped"] is False
    path = tmp_path / "result.json"
    atomic_write_json(path, payload)
    assert read_run_result(path) == payload
    # No stray .tmp survives the atomic write.
    assert list(tmp_path.iterdir()) == [path]


def test_run_result_clip_guard() -> None:
    """Oversized turns / final_output are clipped, marked, and flagged."""
    big = "x" * (RUN_RESULT_CLIP_CHARS + 10)
    payload = run_result_to_payload(_run_result(final_output=big, transcript=("small", big)))
    assert payload["clipped"] is True
    assert payload["final_output"].endswith(RUN_RESULT_CLIP_MARKER)
    assert len(payload["final_output"]) == RUN_RESULT_CLIP_CHARS + len(RUN_RESULT_CLIP_MARKER)
    assert payload["transcript"][0] == "small"
    assert payload["transcript"][1].endswith(RUN_RESULT_CLIP_MARKER)


def test_read_run_result_tolerates_defects(tmp_path: Path) -> None:
    """Missing / garbage / non-object / wrong-format files all read as None."""
    assert read_run_result(tmp_path / "absent.json") is None

    garbage = tmp_path / "garbage.json"
    garbage.write_text("{not json", encoding="utf-8")
    assert read_run_result(garbage) is None

    non_object = tmp_path / "array.json"
    non_object.write_text("[1, 2]", encoding="utf-8")
    assert read_run_result(non_object) is None

    unstamped = tmp_path / "unstamped.json"
    unstamped.write_text(json.dumps({"run_id": "x"}), encoding="utf-8")
    assert read_run_result(unstamped) is None

    future = tmp_path / "future.json"
    future.write_text(json.dumps({"format_version": 2, "run_id": "x"}), encoding="utf-8")
    assert read_run_result(future) is None


# ---------------------------------------------------------------------------
# judge_io records — sha256, clip, one line per call, tolerant reader
# ---------------------------------------------------------------------------


def test_judge_io_record_sha256_is_of_the_unclipped_input() -> None:
    """reasoning_sha256 hashes the FULL text even when the stored copy clips."""
    big = "r" * (JUDGE_IO_CLIP_CHARS + 5)
    record = build_judge_io_record(
        judge_name="j",
        call_index=0,
        reasoning_text=big,
        transcript_window=("w",),
        raw_response="OK",
        drift_emitted=False,
        kind="",
        severity="",
        detail="",
        ts="2026-07-10T00:00:00+00:00",
    )
    assert record["input"]["reasoning_sha256"] == hashlib.sha256(big.encode()).hexdigest()
    assert record["input"]["clipped"] is True
    assert record["input"]["reasoning_text"].endswith(JUDGE_IO_CLIP_MARKER)
    assert record["format_version"] == JUDGE_IO_FORMAT_VERSION
    assert record["ts"] == "2026-07-10T00:00:00+00:00"


def test_judge_io_file_sink_one_line_per_call(tmp_path: Path) -> None:
    """Two record() calls -> two lines with call_index 0 and 1."""
    sink = JudgeIOFileSink(tmp_path / "judge_io.jsonl")
    for response in ("VIOLATION bad", "OK fine"):
        sink.record(
            "j",
            reasoning_text="the reasoning",
            transcript_window=("t1", "t2"),
            raw_response=response,
            drift_emitted=response.startswith("VIOLATION"),
            kind="custom" if response.startswith("VIOLATION") else "",
            severity="warning" if response.startswith("VIOLATION") else "",
            detail="crit: bad" if response.startswith("VIOLATION") else "",
        )
    records = read_judge_io(sink.path)
    assert [r["call_index"] for r in records] == [0, 1]
    assert records[0]["raw_response"] == "VIOLATION bad"
    assert records[0]["verdict"]["drift_emitted"] is True
    assert records[1]["verdict"] == {
        "drift_emitted": False,
        "kind": "",
        "severity": "",
        "detail": "",
    }
    expected_sha = hashlib.sha256(b"the reasoning").hexdigest()
    assert all(r["input"]["reasoning_sha256"] == expected_sha for r in records)


def test_read_judge_io_tolerates_defects(tmp_path: Path) -> None:
    """Missing file -> []; garbage / old-version / non-object lines skipped."""
    assert read_judge_io(tmp_path / "absent.jsonl") == []

    path = tmp_path / "judge_io.jsonl"
    good = build_judge_io_record(
        judge_name="j",
        call_index=0,
        reasoning_text="r",
        transcript_window=(),
        raw_response="OK",
        drift_emitted=False,
        kind="",
        severity="",
        detail="",
    )
    lines = [
        "{torn line",
        json.dumps([1, 2]),
        json.dumps({"format_version": 99, "judge_name": "future"}),
        json.dumps({"judge_name": "unstamped"}),
        json.dumps(good),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    records = read_judge_io(path)
    assert len(records) == 1
    assert records[0]["judge_name"] == "j"


def test_judge_io_file_sink_swallows_unwritable_path(tmp_path: Path) -> None:
    """A sink pointed at a directory logs-and-continues; record never raises."""
    blocked = tmp_path / "judge_io.jsonl"
    blocked.mkdir()
    sink = JudgeIOFileSink(blocked)
    sink.record(
        "j",
        reasoning_text="r",
        transcript_window=(),
        raw_response="OK",
        drift_emitted=False,
        kind="",
        severity="",
        detail="",
    )  # must not raise
    assert read_judge_io(blocked / "nothing.jsonl") == []


# ---------------------------------------------------------------------------
# The inline judge emits through the sink (the real capture path)
# ---------------------------------------------------------------------------


class _MemorySink:
    """Sink double capturing record() kwargs in call order."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def record(self, judge_name: str, **kwargs: Any) -> None:
        self.calls.append({"judge_name": judge_name, **kwargs})


class _RaisingSink:
    """Sink double that always raises — the capture-never-affects-verdict pin."""

    def record(self, judge_name: str, **kwargs: Any) -> None:
        raise RuntimeError("capture exploded")


def _scripted_aux(responses: list[str]) -> Any:
    it = iter(responses)

    async def aux(system: str, user: str, model: str) -> str:
        del system, user, model
        return next(it)

    return aux


def _judge_ctx(reasoning: str, window: tuple[str, ...] = ()) -> Any:
    from goldfive.judges import JudgeContext

    return JudgeContext(reasoning_text=reasoning, transcript=window or (reasoning,))


@pytest.mark.asyncio
async def test_inline_judge_emits_one_record_per_call() -> None:
    """Firing AND silent verdicts both land, with the raw response verbatim."""
    from zicato.judge_runtime.builder import _InlineCriterionJudge

    sink = _MemorySink()
    judge = _InlineCriterionJudge(
        name="crit",
        criterion="no fabricated numbers",
        severity="critical",
        aux_call_llm=_scripted_aux(["VIOLATION made up a figure", "OK grounded"]),
        io_sink=sink,
    )
    ctx = _judge_ctx("claims revenue tripled", ("earlier turn", "claims revenue tripled"))

    fired = await judge.evaluate(ctx)
    silent = await judge.evaluate(ctx)

    assert fired.drift_emitted is True
    assert silent.drift_emitted is False
    assert len(sink.calls) == 2
    first, second = sink.calls
    assert first["raw_response"] == "VIOLATION made up a figure"
    assert first["drift_emitted"] is True
    assert first["severity"] == "critical"
    assert "no fabricated numbers" in first["detail"]
    assert first["reasoning_text"] == "claims revenue tripled"
    assert first["transcript_window"] == ("earlier turn", "claims revenue tripled")
    assert second["raw_response"] == "OK grounded"
    assert second["drift_emitted"] is False
    assert second["kind"] == ""
    assert second["severity"] == ""
    assert second["detail"] == ""


@pytest.mark.asyncio
async def test_inline_judge_verdict_survives_a_raising_sink() -> None:
    """A capture failure is swallowed; the verdict is exactly the no-sink one."""
    from zicato.judge_runtime.builder import _InlineCriterionJudge

    def make(sink: Any) -> Any:
        return _InlineCriterionJudge(
            name="crit",
            criterion="c",
            severity="warning",
            aux_call_llm=_scripted_aux(["VIOLATION nope"]),
            io_sink=sink,
        )

    with_raising = await make(_RaisingSink()).evaluate(_judge_ctx("r"))
    without = await make(None).evaluate(_judge_ctx("r"))
    assert with_raising == without
    assert with_raising.drift_emitted is True


@pytest.mark.asyncio
async def test_inline_judge_skips_capture_on_empty_reasoning() -> None:
    """No LLM call happened -> no record (the early-return path)."""
    from zicato.judge_runtime.builder import _InlineCriterionJudge

    sink = _MemorySink()
    judge = _InlineCriterionJudge(
        name="crit",
        criterion="c",
        severity="warning",
        aux_call_llm=_scripted_aux([]),
        io_sink=sink,
    )
    verdict = await judge.evaluate(_judge_ctx(""))
    assert verdict.drift_emitted is False
    assert sink.calls == []


@pytest.mark.asyncio
async def test_test_retest_emits_judge_io_when_sink_wired(tmp_path: Path) -> None:
    """The test-retest replay rides the same builder seam: k calls -> k lines."""
    from dataclasses import dataclass

    from zicato.judge_runtime.builder import judge_spec_to_goldfive
    from zicato.judge_runtime.reliability import test_retest

    @dataclass
    class _Spec:
        name: str = "retest_judge"
        mode: str = "inline"
        body: str = "stays on topic"
        severity: str = "warning"

    sink = JudgeIOFileSink(tmp_path / "judge_io.jsonl")
    aux = _scripted_aux(["OK a", "VIOLATION b", "OK c"])
    live = judge_spec_to_goldfive(_Spec(), aux, io_sink=sink)
    frozen = "the one frozen transcript"

    result = await test_retest(live, frozen, aux, k=3)

    assert result.k == 3
    records = read_judge_io(sink.path)
    assert len(records) == 3
    assert [r["call_index"] for r in records] == [0, 1, 2]
    assert [r["verdict"]["drift_emitted"] for r in records] == [False, True, False]
    expected_sha = hashlib.sha256(frozen.encode()).hexdigest()
    assert all(r["input"]["reasoning_sha256"] == expected_sha for r in records)


def test_assemble_judges_threads_the_sink() -> None:
    """assemble_judges passes io_sink into every custom inline judge."""
    from dataclasses import dataclass

    from zicato.judge_runtime import assemble_judges

    @dataclass
    class _Spec:
        name: str = "crit"
        mode: str = "inline"
        body: str = "criterion text"
        severity: str = "warning"

    sink = _MemorySink()
    judges = assemble_judges(
        entry_judges=[_Spec()],
        disable_drift=None,
        aux_call_llm=_scripted_aux([]),
        io_sink=sink,
    )
    custom = [j for j in judges if getattr(j, "name", "") == "crit"]
    assert len(custom) == 1
    assert custom[0]._io_sink is sink  # noqa: SLF001 — the seam under test


# ---------------------------------------------------------------------------
# Worker end-to-end — subprocess tests (the L3 boundary is the coverage)
# ---------------------------------------------------------------------------


def _worker_env() -> dict[str, str]:
    env = dict(os.environ)
    root = str(Path(__file__).resolve().parent.parent)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = root if not existing else f"{root}{os.pathsep}{existing}"
    return env


def _spawn_worker(args_path: Path) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(  # noqa: S603
        [sys.executable, "-m", "zicato._tournament_worker", str(args_path)],
        env=_worker_env(),
        timeout=60,
        check=False,
    )


def _write_args(
    args_path: Path,
    *,
    workspace: Path,
    adapter_factory: str,
    result_path: Path,
    loss_path: Path | None = None,
    budget_s: int = 60,
    knobs: dict[str, Any] | None = None,
) -> Path:
    """Write a worker args file; returns the loss path it points at.

    ``knobs`` merges extra top-level keys (the capture knobs); OMITTED
    keys exercise the legacy-args default (capture ON).
    """
    gen_snap = workspace / "snap" / "v0"
    gen_snap.mkdir(parents=True, exist_ok=True)
    sink_path = events_jsonl_path(workspace, "e0", "v0", "entry_a")
    loss = loss_path
    if loss is None:
        loss = loss_profile_path(workspace, "e0", "v0", "entry_a")
    payload: dict[str, Any] = {
        "workspace_root": str(workspace),
        "epoch_id": "e0",
        "generation_id": "v0",
        "snapshot_root": str(gen_snap),
        "entry": {
            "id": "entry_a",
            "kind": "single_turn",
            "wall_clock_budget_seconds": budget_s,
            "input": "hello",
        },
        "adapter": {"kind": "import", "factory": adapter_factory},
        "harness_role": {"dotted": "tests._subprocess_worker_support:harness_call_llm"},
        "auxiliary_role": {"dotted": "tests._subprocess_worker_support:auxiliary_call_llm"},
        "sink_events_path": str(sink_path),
        "loss_path": str(loss),
        "result_path": str(result_path),
        "instance_id": "test",
        "seed": None,
        "harmonograf_url": "",
        "weights": {},
    }
    payload.update(knobs or {})
    args_path.write_text(json.dumps(payload), encoding="utf-8")
    return loss


@pytest.mark.slow
@pytest.mark.integration
def test_worker_writes_result_json_and_judge_io_on_clean_exit(tmp_path: Path) -> None:
    """Default knobs (a legacy args file with NO knob keys): both artifacts land."""
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    loss_path = _write_args(
        tmp_path / "args.json",
        workspace=workspace,
        adapter_factory="tests._subprocess_worker_support:make_completing_adapter",
        result_path=tmp_path / "worker_result.json",
    )
    proc = _spawn_worker(tmp_path / "args.json")
    assert proc.returncode == 0

    captured = read_run_result(unit_result_path(loss_path))
    assert captured is not None
    assert captured["format_version"] == 1
    assert captured["entry_id"] == "entry_a"
    assert captured["final_output"] == "final answer text"
    assert captured["transcript"] == ["intermediate turn", "final answer text"]
    assert captured["aborted"] is False
    assert captured["clipped"] is False

    # The worker bound a live sink onto the config; the (stub) session
    # recorded one scripted judge call through it, landing beside loss.json.
    records = read_judge_io(judge_io_path_for_loss(loss_path))
    assert len(records) == 1
    assert records[0]["judge_name"] == "stub_judge"
    assert records[0]["raw_response"] == "OK looks fine"

    # No half-written .tmp survives in the run dir.
    assert not list(loss_path.parent.glob("*.tmp"))


@pytest.mark.slow
@pytest.mark.integration
def test_worker_persists_synthesized_run_result_on_budget_abort(tmp_path: Path) -> None:
    """The cooperative-budget path persists its synthesized RunResult too."""
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    loss_path = _write_args(
        tmp_path / "args.json",
        workspace=workspace,
        adapter_factory="tests._subprocess_worker_support:make_cooperative_adapter",
        result_path=tmp_path / "worker_result.json",
        budget_s=1,
    )
    proc = _spawn_worker(tmp_path / "args.json")
    assert proc.returncode == 0, "a self-aborted worker still exits cleanly"

    captured = read_run_result(unit_result_path(loss_path))
    assert captured is not None
    assert captured["aborted"] is True
    assert captured["abort_reason"] == "wall_clock_budget"
    assert captured["final_output"] == ""
    assert captured["transcript"] == []
    assert captured["run_id"] == "v0--entry_a"


@pytest.mark.slow
@pytest.mark.integration
def test_worker_replicate_slot_gets_replicate_named_artifacts(tmp_path: Path) -> None:
    """A loss.r2.json unit writes result.r2.json + judge_io.r2.jsonl."""
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    canonical = loss_profile_path(workspace, "e0", "v0", "entry_a")
    loss_path = _write_args(
        tmp_path / "args.json",
        workspace=workspace,
        adapter_factory="tests._subprocess_worker_support:make_completing_adapter",
        result_path=tmp_path / "worker_result.json",
        loss_path=canonical.with_name("loss.r2.json"),
    )
    proc = _spawn_worker(tmp_path / "args.json")
    assert proc.returncode == 0

    run_dir = loss_path.parent
    assert (run_dir / "result.r2.json").exists()
    assert (run_dir / "judge_io.r2.jsonl").exists()
    assert not (run_dir / "result.json").exists()
    assert not (run_dir / "judge_io.jsonl").exists()
    assert read_run_result(run_dir / "result.r2.json") is not None


@pytest.mark.slow
@pytest.mark.integration
def test_worker_knobs_off_writes_no_new_files_and_identical_loss(tmp_path: Path) -> None:
    """The scored-loss pin: knobs OFF adds no files, and every scored field of
    loss.json matches a knobs-ON run's (the stub run is deterministic, so the
    artifacts are the ONLY delta the knobs may introduce). The run's wall-clock
    span is excluded — it measures the execution, not the knobs."""
    on_ws = tmp_path / "on" / ".zicato"
    off_ws = tmp_path / "off" / ".zicato"
    on_ws.mkdir(parents=True)
    off_ws.mkdir(parents=True)

    on_loss = _write_args(
        tmp_path / "on_args.json",
        workspace=on_ws,
        adapter_factory="tests._subprocess_worker_support:make_completing_adapter",
        result_path=tmp_path / "on_result.json",
        knobs={"persist_run_results": True, "persist_judge_io": True},
    )
    off_loss = _write_args(
        tmp_path / "off_args.json",
        workspace=off_ws,
        adapter_factory="tests._subprocess_worker_support:make_completing_adapter",
        result_path=tmp_path / "off_result.json",
        knobs={"persist_run_results": False, "persist_judge_io": False},
    )
    assert _spawn_worker(tmp_path / "on_args.json").returncode == 0
    assert _spawn_worker(tmp_path / "off_args.json").returncode == 0

    on_files = sorted(p.name for p in on_loss.parent.iterdir())
    off_files = sorted(p.name for p in off_loss.parent.iterdir())
    assert "result.json" in on_files
    assert "judge_io.jsonl" in on_files
    # Knobs off: the run dir holds EXACTLY the pre-capture file set.
    assert off_files == sorted(set(on_files) - {"result.json", "judge_io.jsonl"})
    # ... and the loss the run scored is identical either way, apart from the
    # wall-clock span, which is a measurement of the run rather than an effect
    # of the knobs and so differs between any two executions.
    on_profile = json.loads(on_loss.read_text(encoding="utf-8"))
    off_profile = json.loads(off_loss.read_text(encoding="utf-8"))
    for profile in (on_profile, off_profile):
        assert profile.pop("started_at")
        assert profile.pop("ended_at")
    assert off_profile == on_profile
    # The worker-result file the parent reads back is shape-identical too
    # (the loss path differs only by the per-test workspace prefix).
    on_result = json.loads((tmp_path / "on_result.json").read_text(encoding="utf-8"))
    off_result = json.loads((tmp_path / "off_result.json").read_text(encoding="utf-8"))
    assert on_result.pop("loss_profile_path") == str(on_loss)
    assert off_result.pop("loss_profile_path") == str(off_loss)
    assert on_result == off_result


@pytest.mark.slow
@pytest.mark.integration
def test_worker_capture_failure_is_best_effort(tmp_path: Path) -> None:
    """Unwritable capture paths: loss.json + exit code + result file unchanged."""
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    loss_path = _write_args(
        tmp_path / "args.json",
        workspace=workspace,
        adapter_factory="tests._subprocess_worker_support:make_completing_adapter",
        result_path=tmp_path / "worker_result.json",
    )
    # Occupy BOTH capture paths with directories so the atomic rename and
    # the sidecar append each fail with OSError inside the worker.
    unit_result_path(loss_path).mkdir(parents=True)
    judge_io_path_for_loss(loss_path).mkdir(parents=True)

    proc = _spawn_worker(tmp_path / "args.json")
    assert proc.returncode == 0, "capture failures must never fail the worker"

    assert loss_path.exists(), "loss.json still written"
    result = json.loads((tmp_path / "worker_result.json").read_text(encoding="utf-8"))
    assert result["schema"] == "zicato.tournament_worker.result/1"
    assert result["aborted"] is False
    # The blocked capture paths read back as absent, tolerantly.
    assert read_run_result(unit_result_path(loss_path)) is None
