"""WS-INGEST — the foreign-trajectory importer + the imported-trace miner source.

TRAJECTORY-BOOTSTRAP.md §2/§3/§4. Known-answer tests over REAL-shaped fixtures
(the three formats as they actually look — a goldfive envelope+oneof log, an
ADK-style event log, a bare transcript, a malformed-line file, an
ambiguous-format file): the deterministic sniffing table, per-format import,
the signal-episodes with ranking pins, the tolerant degrades, determinism, and
the goldfive-optional proof (the whole path runs with zero goldfive artifact).
"""

from __future__ import annotations

from pathlib import Path

from zicato.core import DIALECT_ADK_EVENTS, DIALECT_GOLDFIVE, DIALECT_TRANSCRIPT
from zicato.reflection import mining as m
from zicato.reflection.trace_import import (
    ImportedTrace,
    import_trace_file,
    import_trajectories,
    read_imported_traces,
    sniff_dialect,
    write_imported_traces,
)

FIXTURES = Path(__file__).parent / "fixtures" / "trajectories"
FIXTURES_EMPTY = Path(__file__).parent / "fixtures" / "trajectories_empty"
FIXTURES_GF_FREE = Path(__file__).parent / "fixtures" / "trajectories_goldfive_free"


def _by_file(traces: list[ImportedTrace]) -> dict[str, ImportedTrace]:
    return {t.source_file: t for t in traces}


def _signal_kinds(episodes: list[m.MinedEpisode]) -> set[str]:
    return {e.evidence.get("signal_kind", "") for e in episodes}


# ---------------------------------------------------------------------------
# sniffing table (TRAJECTORY-BOOTSTRAP.md §2.2)
# ---------------------------------------------------------------------------


def test_sniff_table_each_format() -> None:
    assert sniff_dialect(FIXTURES / "goldfive_run.jsonl") == DIALECT_GOLDFIVE
    assert sniff_dialect(FIXTURES / "adk_run.jsonl") == DIALECT_ADK_EVENTS
    assert sniff_dialect(FIXTURES / "transcript_run.jsonl") == DIALECT_TRANSCRIPT


def test_sniff_ambiguous_resolves_to_adk() -> None:
    # Transcript lines + one behavioral tool_call ⇒ adk_events (precedence rule 2).
    assert sniff_dialect(FIXTURES / "ambiguous.jsonl") == DIALECT_ADK_EVENTS


def test_sniff_malformed_falls_to_transcript() -> None:
    # A non-JSON line + a JSON-array line are counted, not crashed; the readable
    # role/content lines make it a transcript.
    assert sniff_dialect(FIXTURES / "malformed.jsonl") == DIALECT_TRANSCRIPT


def test_sniff_empty_or_missing_is_transcript_floor(tmp_path: Path) -> None:
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    assert sniff_dialect(empty) == DIALECT_TRANSCRIPT
    assert sniff_dialect(tmp_path / "does_not_exist.jsonl") == DIALECT_TRANSCRIPT


def test_sniff_is_order_independent(tmp_path: Path) -> None:
    a = tmp_path / "a.jsonl"
    a.write_text(
        '{"role":"user","content":"hi"}\n{"type":"model_usage","input_tokens":5}\n',
        encoding="utf-8",
    )
    b = tmp_path / "b.jsonl"
    b.write_text(
        '{"type":"model_usage","input_tokens":5}\n{"role":"user","content":"hi"}\n',
        encoding="utf-8",
    )
    assert sniff_dialect(a) == sniff_dialect(b) == DIALECT_ADK_EVENTS


# ---------------------------------------------------------------------------
# per-format import known-answers (TRAJECTORY-BOOTSTRAP.md §3)
# ---------------------------------------------------------------------------


def test_import_adk_known_answer() -> None:
    trace = import_trace_file(FIXTURES / "adk_run.jsonl")
    assert trace.dialect == DIALECT_ADK_EVENTS
    assert trace.trace_id.startswith("trace-")
    s = trace.signals
    # tool_call x2, one repeated ⇒ one looping_tool_call; two error responses ⇒
    # task_failed 2/2; three transfers ⇒ agent_transfer info 3; one error event.
    drift = {(dc.kind, dc.severity): dc.count for dc in s.drift_counts}
    assert drift[("tool_error", "critical")] == 1
    assert drift[("looping_tool_call", "warning")] == 1
    assert drift[("agent_transfer", "info")] == 3
    assert (s.task_started, s.task_failed) == (2, 2)
    assert s.token_count == 1000
    assert s.user_turns and "Lisbon" in s.user_turns[0]
    assert trace.malformed_line_count == 0


def test_import_transcript_known_answer() -> None:
    trace = import_trace_file(FIXTURES / "transcript_run.jsonl")
    assert trace.dialect == DIALECT_TRANSCRIPT
    # The floor: zero drift, reconstructed both sides.
    assert trace.signals.drift_counts == ()
    assert len(trace.user_turns) == 2
    assert len(trace.agent_turns) == 2
    assert trace.signals.agent_text_chars > 0


def test_import_goldfive_known_answer() -> None:
    trace = import_trace_file(FIXTURES / "goldfive_run.jsonl")
    assert trace.dialect == DIALECT_GOLDFIVE
    # task_started/failed both 2 (a total-failure abort pattern), one drift.
    assert (trace.signals.task_started, trace.signals.task_failed) == (2, 2)


def test_import_counts_malformed_never_crashes() -> None:
    trace = import_trace_file(FIXTURES / "malformed.jsonl")
    # two unparseable lines (a bare string + a JSON array), counted not raised.
    assert trace.malformed_line_count == 2
    assert trace.line_count == 4


def test_import_trajectories_sorted_and_complete() -> None:
    traces = import_trajectories(FIXTURES)
    names = [t.source_file for t in traces]
    assert names == sorted(names)
    assert set(names) == {
        "adk_run.jsonl",
        "ambiguous.jsonl",
        "goldfive_run.jsonl",
        "malformed.jsonl",
        "transcript_run.jsonl",
    }


# ---------------------------------------------------------------------------
# record round-trip + determinism
# ---------------------------------------------------------------------------


def test_imported_trace_json_round_trip() -> None:
    for trace in import_trajectories(FIXTURES):
        rebuilt = ImportedTrace.from_json(trace.to_json())
        assert rebuilt.trace_id == trace.trace_id
        assert rebuilt.dialect == trace.dialect
        assert rebuilt.signals == trace.signals
        assert rebuilt.user_turns == trace.user_turns


def test_reimport_is_idempotent() -> None:
    first = import_trajectories(FIXTURES)
    second = import_trajectories(FIXTURES)
    assert [t.trace_id for t in first] == [t.trace_id for t in second]
    assert [t.to_json() for t in first] == [t.to_json() for t in second]


# ---------------------------------------------------------------------------
# persistence
# ---------------------------------------------------------------------------


def test_write_read_imported_traces(tmp_path: Path) -> None:
    traces = import_trajectories(FIXTURES)
    write_imported_traces(tmp_path, "epoch-1", "refl-1", traces)
    back = read_imported_traces(tmp_path, "epoch-1", "refl-1")
    assert {t.trace_id for t in back} == {t.trace_id for t in traces}


def test_read_imported_traces_absent_is_empty(tmp_path: Path) -> None:
    assert read_imported_traces(tmp_path, "epoch-x", "refl-x") == []


# ---------------------------------------------------------------------------
# imported-trace episodes (TRAJECTORY-BOOTSTRAP.md §4)
# ---------------------------------------------------------------------------


def test_adk_trace_yields_signal_episodes() -> None:
    trace = import_trace_file(FIXTURES / "adk_run.jsonl")
    eps = m.imported_trace_episodes([trace])
    kinds = _signal_kinds(eps)
    # error cascade (+ abort pattern, ratio 1.0), retry loop, transfer churn.
    assert {"error_cascade", "abort_pattern", "retry_loop", "transfer_churn"} <= kinds
    assert all(e.suggestion_hint == m.HINT_BOOTSTRAP_ENTRY for e in eps)
    assert all(e.episode_type == m.EPISODE_IMPORTED_SIGNAL for e in eps)
    assert all(e.recency_key == 0 for e in eps)
    assert all(e.source_lineage_ids == () for e in eps)


def test_clean_transcript_yields_behavioral_episode() -> None:
    trace = import_trace_file(FIXTURES / "transcript_run.jsonl")
    eps = m.imported_trace_episodes([trace])
    assert len(eps) == 1
    assert eps[0].episode_type == m.EPISODE_IMPORTED_BEHAVIORAL
    assert eps[0].suggestion_hint == m.HINT_BOOTSTRAP_RUBRIC


def test_signal_episode_ids_are_unique_per_signal_kind() -> None:
    trace = import_trace_file(FIXTURES / "adk_run.jsonl")
    eps = m.imported_trace_episodes([trace])
    ids = [e.episode_id for e in eps]
    assert len(ids) == len(set(ids))  # no collision across signal kinds on one trace


def test_episode_evidence_carries_reconstruction_pointer() -> None:
    trace = import_trace_file(FIXTURES / "adk_run.jsonl")
    ep = m.imported_trace_episodes([trace])[0]
    assert ep.evidence["trace_id"] == trace.trace_id
    assert ep.evidence["source_file"] == "adk_run.jsonl"
    assert ep.evidence["dialect"] == DIALECT_ADK_EVENTS
    assert "Lisbon" in ep.evidence["opening_user_turn"]


def test_imported_episodes_are_deterministically_ranked() -> None:
    traces = import_trajectories(FIXTURES)
    ranked_a = m.rank_episodes(m.imported_trace_episodes(traces))
    ranked_b = m.rank_episodes(m.imported_trace_episodes(list(reversed(traces))))
    assert [e.episode_id for e in ranked_a] == [e.episode_id for e in ranked_b]
    # severity is descending (an error cascade outranks transfer churn).
    sevs = [e.severity_rank for e in ranked_a]
    assert sevs == sorted(sevs, reverse=True)


def test_empty_traces_yield_no_episodes() -> None:
    assert m.imported_trace_episodes([]) == []


# ---------------------------------------------------------------------------
# tolerant degrades
# ---------------------------------------------------------------------------


def test_empty_directory_imports_nothing() -> None:
    assert import_trajectories(FIXTURES_EMPTY) == []


def test_missing_directory_imports_nothing(tmp_path: Path) -> None:
    assert import_trajectories(tmp_path / "no_such_dir") == []


def test_non_jsonl_files_are_ignored(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("ignore me", encoding="utf-8")
    (tmp_path / "a.jsonl").write_text('{"role":"user","content":"hi"}\n', encoding="utf-8")
    traces = import_trajectories(tmp_path)
    assert [t.source_file for t in traces] == ["a.jsonl"]


# ---------------------------------------------------------------------------
# the goldfive-optional proof (TRAJECTORY-BOOTSTRAP.md §1.1 / §9)
# ---------------------------------------------------------------------------


def test_goldfive_optional_end_to_end() -> None:
    # A directory with ZERO goldfive artifacts (only adk + transcript) runs the
    # whole import → episode path and produces episodes.
    traces = import_trajectories(FIXTURES_GF_FREE)
    assert traces
    assert all(t.dialect in (DIALECT_ADK_EVENTS, DIALECT_TRANSCRIPT) for t in traces)
    eps = m.imported_trace_episodes(traces)
    assert eps
    assert any(e.suggestion_hint == m.HINT_BOOTSTRAP_ENTRY for e in eps)
    assert any(e.suggestion_hint == m.HINT_BOOTSTRAP_RUBRIC for e in eps)


def test_mine_episodes_folds_imported_with_absent_epoch(tmp_path: Path) -> None:
    # A cold / absent workspace must NOT suppress imported episodes: mine_episodes
    # mines them first + unconditionally (the goldfive-optional path).
    from zicato.query.paths import WorkspacePaths

    traces = import_trajectories(FIXTURES_GF_FREE)
    paths = WorkspacePaths(tmp_path)
    episodes = m.mine_episodes(paths, None, imported_traces=traces)
    assert episodes
    assert all(
        e.episode_type in (m.EPISODE_IMPORTED_SIGNAL, m.EPISODE_IMPORTED_BEHAVIORAL)
        for e in episodes
    )
    # ranking is total + descending by severity.
    sevs = [e.severity_rank for e in episodes]
    assert sevs == sorted(sevs, reverse=True)


def test_mine_episodes_default_no_traces_is_unchanged(tmp_path: Path) -> None:
    from zicato.query.paths import WorkspacePaths

    paths = WorkspacePaths(tmp_path)
    assert m.mine_episodes(paths, None) == []
