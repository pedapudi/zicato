"""The envelope schema is shared with the supervisor; hold the two equal.

Two services read a run's ``events.jsonl``: the Python reader in
:mod:`zicato.telemetry.event_log` and the Rust supervisor's
``run_log.rs``. Each carries its own list of the top-level names that
belong to the envelope and so can never name the payload case. If the two
lists drift apart, one service reports an event kind the other does not,
and nothing fails until someone notices a panel disagreeing with a loss
profile. This reads the Rust list out of its source and holds it equal to
the Python one.

The Rust list spells both twins of each proto field, because it matches
against the raw line; the Python reader normalises the line first and so
spells each name once. Folding the Rust list through the same casing rule
is what makes the two comparable.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from zicato.telemetry.event_log import ENVELOPE_KEYS, to_snake

_RUN_LOG_RS = Path(__file__).resolve().parents[1] / "crates" / "supervisor" / "src" / "run_log.rs"

_DECLARATION = re.compile(
    r"const ENVELOPE_KEYS:\s*&\[&str\]\s*=\s*&\[(?P<body>.*?)\];",
    re.DOTALL,
)


def _rust_envelope_keys() -> frozenset[str]:
    """The supervisor's envelope names, read out of its source."""
    source = _RUN_LOG_RS.read_text(encoding="utf-8")
    match = _DECLARATION.search(source)
    assert match is not None, (
        f"{_RUN_LOG_RS} no longer declares `const ENVELOPE_KEYS: &[&str]`. "
        "The two readers still have to agree; update this test to read the "
        "declaration's new form rather than deleting it."
    )
    return frozenset(re.findall(r'"([^"]+)"', match.group("body")))


def test_the_supervisor_declares_an_envelope_list() -> None:
    keys = _rust_envelope_keys()
    assert len(keys) >= len(ENVELOPE_KEYS)


def test_both_readers_hold_the_same_envelope_schema() -> None:
    """Every Rust name folds onto a Python one, and none is missing.

    The comparison is on the folded form because the two readers spell the
    set differently by design, not because either is approximate.
    """
    folded = {to_snake(key) for key in _rust_envelope_keys()}
    assert folded == set(ENVELOPE_KEYS)


def test_the_rust_list_spells_both_twins_of_every_proto_field() -> None:
    """A name the supervisor reads raw needs both spellings to be found.

    ``seq``, ``kind`` and ``payload`` are single-word names whose two
    spellings coincide, so only the multi-word proto fields are checked.
    """
    keys = _rust_envelope_keys()
    for snake in ("emitted_at", "event_id", "run_id", "session_id"):
        head, _, tail = snake.partition("_")
        camel = head + tail.capitalize()
        assert snake in keys, f"{snake} missing from the supervisor's envelope list"
        assert camel in keys, f"{camel} missing from the supervisor's envelope list"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("driftDetected", "drift_detected"),
        ("goldfiveLlmCallStart", "goldfive_llm_call_start"),
        ("already_snake", "already_snake"),
        ("target4Score", "target4_score"),
        ("HTTPServer", "httpserver"),
    ],
)
def test_the_casing_rule_matches_the_supervisor_s_own_cases(name: str, expected: str) -> None:
    """The conversions the supervisor's `to_snake` unit tests state.

    ``run_log.rs`` implements the same character loop; these are the
    conversions both must produce for an event kind to have one spelling
    whichever service rendered it.
    """
    assert to_snake(name) == expected
