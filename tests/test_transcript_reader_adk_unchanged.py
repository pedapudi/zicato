"""The ADK reader path answers byte for byte what it answered before.

``reconstruct_transcript`` gained a second reader for the Foe episode log,
and the Goldfive/ADK event stream is the format it must leave alone. Each
fixture under ``tests/fixtures/adk_transcripts/`` is one ADK event stream,
and ``tests/data/adk_transcript_reconstruction.json`` holds the payload the
reader produced for it before the second reader existed. The three streams
between them cover a fully resolved invocation tree, a topology carrying an
orphaned parent and a parent cycle, and a multi-run file whose final line is
torn mid-write.

A difference in any field of any payload — turns, annotations, execution
nodes, ``unresolved_ids``, the completion flag — fails the comparison.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zicato.query.transcript_reconstruction import reconstruct_transcript

FIXTURES = Path(__file__).parent / "fixtures" / "adk_transcripts"
RECORDED = Path(__file__).parent / "data" / "adk_transcript_reconstruction.json"


def _recorded() -> dict[str, dict]:
    return json.loads(RECORDED.read_text(encoding="utf-8"))


def test_every_recorded_stream_still_has_a_fixture() -> None:
    """The comparison covers the whole corpus, so neither side can drift alone."""
    assert sorted(_recorded()) == sorted(path.name for path in FIXTURES.glob("*.jsonl"))


@pytest.mark.parametrize("name", sorted(_recorded()))
def test_adk_reconstruction_is_unchanged(name: str) -> None:
    assert reconstruct_transcript(FIXTURES / name).to_dict() == _recorded()[name]


def test_the_corpus_exercises_resolved_and_unresolved_topologies() -> None:
    """Guard the corpus itself: an all-``exact`` corpus would prove less.

    ``unresolved_ids`` is the field an execution-topology change is most
    likely to move, so at least one stream must produce a non-empty one and
    at least one must produce an empty one.
    """
    fidelities = {
        name: (payload["execution"]["fidelity"], tuple(payload["execution"]["unresolved_ids"]))
        for name, payload in _recorded().items()
    }
    assert fidelities["nested_agents.jsonl"] == ("exact", ())
    assert fidelities["unresolved_topology.jsonl"] == (
        "partial",
        ("inv-orphan", "inv-loop-one", "inv-loop-two"),
    )


def test_a_torn_final_line_still_reports_an_incomplete_transcript() -> None:
    """The tolerance a growing file depends on, pinned on the real corpus."""
    payload = reconstruct_transcript(FIXTURES / "multi_run_annotated.jsonl").to_dict()
    assert payload["complete"] is False
    assert [turn["run_index"] for turn in payload["turns"]] == [1, 1, 2, 2, 2]
