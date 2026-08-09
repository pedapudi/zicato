"""Default-off proof + behaviour pins for target_1's measurement mode.

``ZICATO_TARGET1_MEASUREMENT_MODE`` turns the presentation board from a
puzzle into an instrument: write/read/find collapse onto one canonical
directory, an ``after_model_callback`` salvages a deck the developer only
described, and every write is snapshotted. Each of those would dissolve
part of the board's designed difficulty, so the switch is off by default.

``target_1`` is a dogfood target, so "off by default" is not a comment to
be taken on trust — and none of the gates that look like they would catch
a regression here actually can:

* the parity CONTRACT-HASH gate canonicalizes the mutable-tree PATH
  STRINGS ("agent") and the entrypoint as a literal dotted string. No
  hash in the contract canon covers this file's contents (the only
  agent-source hash there belongs to the proposer dir, and the fixture
  passes ``proposer_path=None``);
* the MOCK-GOLDEN and reindex goldens copy the real ``agent/`` tree but
  run a stubbed harness, so the tools below never execute during capture
  and no target_1 output reaches either golden;
* the mutation-marker suite parses the file for ``# zicato:mutable``
  lines. Unmarked code is invisible to it, and there is no closed-set pin
  on the id list — only a floor and membership checks.

Every one of those is blind to unmarked, default-off code by
construction. This module is the coverage.

The first half is the proof, in the repo's two byte-identity idioms.
Transcribed-reference (cf. ``tests/test_scoring_seams.py``): the
``_ref_*`` functions below are the LITERAL pre-measurement-mode formulas,
and with the switch off the shipped tools must agree with them exactly
and leave no extra byte on disk. Same-call A/B (cf.
``tests/test_scoring_diff_complexity.py::test_aggregate_byte_identical_when_off``):
exercise the paths the mode would change, with it off, and assert the
return shapes gain no new keys. The second half pins what the mode does
when it IS on, including the two behaviours that were wrong when the mode
first landed.

Every test pins the environment explicitly rather than inheriting it: an
operator with the variable exported in their shell must not be able to
change what this suite proves.
"""

from __future__ import annotations

import json
import os
import types
from pathlib import Path
from typing import Any

import pytest

from zicato_examples.target_1_presentation.agent import agent as A

pytestmark = pytest.mark.usefixtures("_pinned_env")

_DECK = ("index.html", "styles.css", "script.js")


# ---------------------------------------------------------------------------
# Fixtures — a private output base per test, and an explicitly pinned switch.
# ---------------------------------------------------------------------------


@pytest.fixture
def _pinned_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralize any inherited measurement-mode setting.

    Tests that want the mode on set it themselves. Without this an operator
    who exported the variable would silently flip every default-off
    assertion below into a measurement-mode assertion.
    """
    monkeypatch.delenv("ZICATO_TARGET1_MEASUREMENT_MODE", raising=False)


@pytest.fixture
def base(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the target's output base at a private scratch directory."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setenv("ZICATO_RUN_SCRATCH_DIR", str(scratch))
    return scratch / "output"


@pytest.fixture
def measuring(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZICATO_TARGET1_MEASUREMENT_MODE", "1")


def _tree(root: Path) -> dict[str, str]:
    """Every file under ``root``, relative path to contents."""
    return {str(p.relative_to(root)): p.read_text() for p in sorted(root.rglob("*")) if p.is_file()}


def _response(text: str, *, tool_call: str | None = None) -> Any:
    """A minimal stand-in for the ADK ``LlmResponse`` the callback reads."""
    call = types.SimpleNamespace(name=tool_call) if tool_call else None
    part = types.SimpleNamespace(text=text, function_call=call)
    return types.SimpleNamespace(content=types.SimpleNamespace(parts=[part]))


# ---------------------------------------------------------------------------
# Reference implementations — the LITERAL pre-measurement-mode behaviour,
# transcribed. With the switch off the shipped tools must match these.
# ---------------------------------------------------------------------------


def _ref_write_webpage(base_dir: Path, topic: str, html: str, css: str, js: str) -> str:
    slug = topic.lower().replace(" ", "_").replace("/", "_")
    output_dir = base_dir / slug
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "index.html").write_text(html)
    (output_dir / "styles.css").write_text(css)
    (output_dir / "script.js").write_text(js)
    return f"Successfully created presentation on '{topic}' at {output_dir}"


def _ref_read_presentation_files(base_dir: Path, topic: str) -> dict[str, str]:
    slug = topic.lower().replace(" ", "_").replace("/", "_")
    output_dir = base_dir / slug
    files: dict[str, str] = {}
    for name in _DECK:
        path = output_dir / name
        try:
            files[name] = path.read_text()
        except OSError as e:
            files[name] = f"<error reading {path}: {e}>"
    return files


# ---------------------------------------------------------------------------
# The switch itself.
# ---------------------------------------------------------------------------


def test_measurement_mode_is_off_when_the_variable_is_absent(base: Path) -> None:
    assert A.measurement_mode() is False


@pytest.mark.parametrize("value", ["0", "", "true", "TRUE", "yes", "on", "2", " 1"])
def test_only_the_exact_string_1_arms_the_mode(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """Anything other than ``1`` leaves the board in its shipped state.

    The switch is deliberately strict rather than truthy: a half-set
    variable must fail closed, back to the board's designed difficulty,
    never silently into instrument mode.
    """
    monkeypatch.setenv("ZICATO_TARGET1_MEASUREMENT_MODE", value)
    assert A.measurement_mode() is False


def test_the_exact_string_1_arms_the_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZICATO_TARGET1_MEASUREMENT_MODE", "1")
    assert A.measurement_mode() is True


# ---------------------------------------------------------------------------
# Default-off proof.
# ---------------------------------------------------------------------------


def test_write_is_byte_identical_to_the_reference_when_off(base: Path, tmp_path: Path) -> None:
    """Same return string, same paths, same bytes, no extra files."""
    topic = "Quantum Computing/Basics"
    html, css, js = "<html>deck</html>", "body{}", "let x=1;"

    got = A.write_webpage(topic, html, css, js)

    ref_base = tmp_path / "ref"
    ref_base.mkdir()
    want = _ref_write_webpage(ref_base, topic, html, css, js)

    assert got == want.replace(str(ref_base), str(base))
    assert _tree(base) == _tree(ref_base)


def test_read_is_byte_identical_to_the_reference_when_off(base: Path, tmp_path: Path) -> None:
    """Including the ``<error reading ...>`` shape on the miss path."""
    A.write_webpage("Photosynthesis", "<html>p</html>", "css", "js")
    ref_base = tmp_path / "ref"
    ref_base.mkdir()
    _ref_write_webpage(ref_base, "Photosynthesis", "<html>p</html>", "css", "js")

    for topic in ("Photosynthesis", "Photosynthesis Presentation"):
        got = A.read_presentation_files(topic)
        want = _ref_read_presentation_files(ref_base, topic)
        assert got == {k: v.replace(str(ref_base), str(base)) for k, v in want.items()}, topic


@pytest.mark.parametrize(
    "topic",
    ["Photosynthesis", "Quantum Computing", "A/B Testing", "Mixed CASE/And Spaces", ""],
)
def test_the_mutable_path_helpers_are_untouched_when_off(base: Path, topic: str) -> None:
    """The slug rule and the slug-to-directory rule, pinned directly.

    These two are the ``zicato:mutable:code`` regions the proposer edits to
    solve the board. Measurement mode routes around them rather than
    through them, so a guard placed one level too deep — inside
    ``_slugify_topic`` rather than at the call sites — would change the
    surface the proposer is scored on while every gate stayed green.
    """
    ref_slug = topic.lower().replace(" ", "_").replace("/", "_")

    assert A._slugify_topic(topic) == ref_slug
    # os.path.join, not Path division: the two disagree on an empty slug
    # (a trailing separator versus none), and the literal formula is join.
    assert A._topic_output_dir(topic) == os.path.join(str(base), ref_slug)


def test_the_tool_return_shapes_gain_no_new_keys_when_off(base: Path) -> None:
    """Exactly the historical keys, on both the hit and the miss path.

    The measurement branch builds the finder's hit result separately, so
    the two constructions can drift apart without any assertion on the
    contents catching it — a stray key reaches the debugger agent as a
    changed tool contract.
    """
    A.write_webpage("Photosynthesis Presentation", "<html>p</html>", "css", "js")

    assert set(A.read_presentation_files("Photosynthesis")) == set(_DECK)
    assert set(A.find_presentation_files("Photosynthesis")) == {
        "found",
        "directory",
        "files",
    }
    assert set(A.find_presentation_files("Mitochondria")) == {"found", "candidates"}


def test_the_designed_slug_mismatch_still_fails_when_off(base: Path) -> None:
    """The board's whole difficulty, pinned.

    The developer writes under an embellished slug and the reviewer reads
    the bare one, so all three files come back as read errors. If a change
    ever makes this pass, the board has stopped measuring what it exists to
    measure — and measurement mode is exactly such a change, which is why
    it is off here.
    """
    A.write_webpage("Photosynthesis Presentation", "<html>p</html>", "css", "js")

    files = A.read_presentation_files("Photosynthesis")

    assert all(v.startswith("<error reading ") for v in files.values())


def test_the_fuzzy_finder_is_unchanged_when_off(base: Path) -> None:
    """Suffix-stripping recovery still works, and a miss still lists candidates."""
    A.write_webpage("Photosynthesis Presentation", "<html>p</html>", "css", "js")

    found = A.find_presentation_files("Photosynthesis")
    assert found["found"] is True
    assert Path(str(found["directory"])).name == "photosynthesis_presentation"
    assert found["files"]["index.html"] == "<html>p</html>"

    missed = A.find_presentation_files("Mitochondria")
    assert missed["found"] is False
    assert missed["candidates"] == ["photosynthesis_presentation"]


def test_no_measurement_artifact_is_written_when_off(base: Path) -> None:
    """Not the canonical dir, not the history, not either marker.

    The parity MOCK-GOLDEN gate and the analyzer both read this tree; a
    stray byte from a default-off feature is a golden-drift bug waiting to
    happen.
    """
    A.write_webpage("Photosynthesis", "<html>p</html>", "css", "js")
    A.read_presentation_files("Photosynthesis")
    A.find_presentation_files("Photosynthesis")
    A.snapshot_deck("h", "c", "j")
    A.salvage_deck_from_response(None, _response('{"html_content":"X"}'))

    names = {p.name for p in base.rglob("*")}
    assert "presentation" not in names
    assert "deck_history" not in names
    assert "MEASUREMENT_MODE" not in names
    assert ".written_by_write_webpage" not in names
    assert set(_tree(base)) == {f"photosynthesis/{n}" for n in _DECK}


def test_the_developer_agent_takes_no_extra_kwargs_when_off(base: Path) -> None:
    """No ``output_schema``, no salvage callback — the shipped tree."""
    tree = A.build_agent_tree("openai/gpt-4o-mini")
    developer = _developer(tree)

    assert developer.output_schema is None
    assert not developer.canonical_after_model_callbacks
    assert set(_tree(base)) == set()


def _developer(tree: Any) -> Any:
    for tool in tree.tools:
        agent = getattr(tool, "agent", None)
        if agent is not None and agent.name == "web_developer_agent":
            return agent
    raise AssertionError("web_developer_agent not found in the tree")


# ---------------------------------------------------------------------------
# What the mode does when it is on.
# ---------------------------------------------------------------------------


def test_write_read_and_find_all_agree_when_on(base: Path, measuring: None) -> None:
    """The point of the mode: no topic string can make a run unscoreable."""
    A.write_webpage("Quantum Computing", "<html>q</html>", "css", "js")

    assert A.read_presentation_files("a completely different topic") == {
        "index.html": "<html>q</html>",
        "styles.css": "css",
        "script.js": "js",
    }

    found = A.find_presentation_files("a completely different topic")
    assert found["found"] is True
    assert Path(str(found["directory"])) == base / "presentation"
    assert found["files"]["index.html"] == "<html>q</html>"


def test_the_finder_does_not_report_the_canonical_deck_as_missing(
    base: Path, measuring: None
) -> None:
    """Regression guard.

    The first cut left ``find_presentation_files`` on the fuzzy match while
    pinning write and read to ``presentation/``. The match compares the
    topic slug against the directory name, and the canonical name is
    ``presentation`` for every topic, so the finder answered ``found:
    False`` for a deck sitting in plain sight — and handed the debugger a
    candidate list whose entries included the ``deck_history`` bookkeeping
    directory.
    """
    A.write_webpage("Quantum Computing", "<html>q</html>", "css", "js")

    found = A.find_presentation_files("Quantum Computing")

    assert found["found"] is True
    assert "candidates" not in found


def test_salvage_persists_a_deck_the_model_only_described(base: Path, measuring: None) -> None:
    deck = {"html_content": "<h1>V1</h1>", "css_content": "c", "js_content": "j"}
    A.salvage_deck_from_response(None, _response(json.dumps(deck)))

    assert (base / "presentation" / "index.html").read_text() == "<h1>V1</h1>"
    assert (base / "deck_history" / "turn_0" / "index.html").read_text() == "<h1>V1</h1>"


def test_salvage_lets_a_later_turn_revise_its_own_earlier_salvage(
    base: Path, measuring: None
) -> None:
    """Regression guard.

    The first cut skipped whenever ``index.html`` was non-empty, which also
    caught the callback's OWN output: the artifact froze at the first
    salvaged turn and ``deck_history`` never grew past ``turn_0``. That
    silently defeats the reason the history exists — "did turn N+1 keep
    what turn N built?" is unanswerable when turn N+1 was never recorded.
    """
    for body in ("<h1>V1</h1>", "<h1>V2</h1>", "<h1>V3</h1>"):
        A.salvage_deck_from_response(None, _response(json.dumps({"html_content": body})))

    assert (base / "presentation" / "index.html").read_text() == "<h1>V3</h1>"
    assert sorted(p.name for p in (base / "deck_history").iterdir()) == [
        "turn_0",
        "turn_1",
        "turn_2",
    ]


def test_salvage_never_clobbers_a_real_write_webpage_result(base: Path, measuring: None) -> None:
    """The invariant the freeze was trying to protect, kept without the freeze."""
    A.write_webpage("Quantum Computing", "<html>REAL</html>", "css", "js")

    A.salvage_deck_from_response(None, _response(json.dumps({"html_content": "<h1>prose</h1>"})))

    assert (base / "presentation" / "index.html").read_text() == "<html>REAL</html>"


def test_salvage_stands_aside_for_a_write_webpage_call_in_flight(
    base: Path, measuring: None
) -> None:
    A.salvage_deck_from_response(
        None,
        _response(json.dumps({"html_content": "<h1>X</h1>"}), tool_call="write_webpage"),
    )

    assert not (base / "presentation" / "index.html").exists()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("```html\n<h1>fenced</h1>\n```", "<h1>fenced</h1>"),
        ("prose\n<!DOCTYPE html><body>raw</body></html>\ntrailing", None),
        ('here is your deck:\n{"html_content": "<h1>lead-in</h1>"}', "<h1>lead-in</h1>"),
    ],
)
def test_salvage_reads_the_fallback_shapes(
    base: Path, measuring: None, text: str, expected: str | None
) -> None:
    A.salvage_deck_from_response(None, _response(text))

    written = (base / "presentation" / "index.html").read_text()
    if expected is None:
        assert written.startswith("<!DOCTYPE html>") and written.endswith("</html>")
    else:
        assert written == expected


def test_a_response_with_no_deck_in_it_writes_nothing(base: Path, measuring: None) -> None:
    for text in ("", "   ", "I could not build the deck.", "```css\nbody{}\n```"):
        A.salvage_deck_from_response(None, _response(text))

    assert not (base / "presentation" / "index.html").exists()


def test_the_run_records_that_measurement_mode_was_on(base: Path, measuring: None) -> None:
    """Salvage guarantees a deck exists however the pipeline failed, so an
    artifact tree from this mode cannot be read as evidence about file
    findability. Nothing else in the tree would say so."""
    A.build_agent_tree("openai/gpt-4o-mini")

    note = (base / "MEASUREMENT_MODE").read_text()
    assert "ZICATO_TARGET1_MEASUREMENT_MODE=1" in note
    assert "file-findability" in note


def test_every_run_gets_the_note_not_just_the_first_in_the_process(
    base: Path, measuring: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The note is written per run, not once per process.

    ``root_agent`` is cached, so ``build_agent_tree`` runs once however
    many board entries a worker process serves. A note written only at
    tree-build time would land in the first run's output base and leave
    every later run's tree looking like a normal run's.
    """
    A.build_agent_tree("openai/gpt-4o-mini")
    assert (base / "MEASUREMENT_MODE").exists()

    next_run = tmp_path / "scratch2"
    next_run.mkdir()
    monkeypatch.setenv("ZICATO_RUN_SCRATCH_DIR", str(next_run))
    A.write_webpage("Quantum Computing", "<html>q</html>", "css", "js")

    assert (next_run / "output" / "MEASUREMENT_MODE").exists()


def test_the_developer_agent_carries_the_schema_and_callback_when_on(
    base: Path, measuring: None
) -> None:
    developer = _developer(A.build_agent_tree("openai/gpt-4o-mini"))

    assert developer.output_schema == A.DECK_OUTPUT_SCHEMA
    assert A.salvage_deck_from_response in developer.canonical_after_model_callbacks
    assert developer.tools, "the developer keeps write_webpage alongside the schema"


def test_the_write_marker_does_not_leak_into_the_deck_the_tools_return(
    base: Path, measuring: None
) -> None:
    """The provenance marker is bookkeeping, not part of the artifact.

    It is a dotfile rather than a directory so the finder cannot list it as
    a candidate, and the read tools name their three files explicitly.
    """
    A.write_webpage("Quantum Computing", "<html>q</html>", "css", "js")

    assert (base / "presentation" / ".written_by_write_webpage").is_file()
    assert set(A.read_presentation_files("anything")) == set(_DECK)
    assert set(A.find_presentation_files("anything")["files"]) == set(_DECK)


def test_snapshots_are_immutable_across_a_revision_sequence(base: Path, measuring: None) -> None:
    """Each turn's copy survives the next turn's overwrite of the deck dir."""
    for body in ("<h1>V1</h1>", "<h1>V2</h1>"):
        A.write_webpage("Quantum Computing", body, "css", "js")

    hist = base / "deck_history"
    assert (hist / "turn_0" / "index.html").read_text() == "<h1>V1</h1>"
    assert (hist / "turn_1" / "index.html").read_text() == "<h1>V2</h1>"
    assert (base / "presentation" / "index.html").read_text() == "<h1>V2</h1>"


def test_the_deck_history_is_bounded_by_the_run_scratch_dir(
    base: Path, measuring: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """History accumulates per run, not across runs.

    Growth is unbounded in turns but the tree lives under
    ``ZICATO_RUN_SCRATCH_DIR``, which the tournament worker discards when
    the run ends — so a long campaign cannot accrete snapshots. Pinned
    because the same reasoning is what keeps run output out of the
    generation snapshot in the first place.
    """
    A.write_webpage("Quantum Computing", "<html>q</html>", "css", "js")
    assert len(list((base / "deck_history").iterdir())) == 1

    next_run = tmp_path / "scratch2"
    next_run.mkdir()
    monkeypatch.setenv("ZICATO_RUN_SCRATCH_DIR", str(next_run))
    A.write_webpage("Quantum Computing", "<html>q</html>", "css", "js")

    assert sorted(p.name for p in (next_run / "output" / "deck_history").iterdir()) == ["turn_0"]


def test_the_salvage_write_provenance_does_not_survive_the_run(
    base: Path, measuring: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The marker is on disk, not in a module global, so it cannot leak.

    A process-lifetime flag would be wrong here: the tournament worker runs
    many board entries, and a real write in one run would silently disarm
    salvage for every run after it in the same process.
    """
    A.write_webpage("Quantum Computing", "<html>REAL</html>", "css", "js")

    next_run = tmp_path / "scratch2"
    next_run.mkdir()
    monkeypatch.setenv("ZICATO_RUN_SCRATCH_DIR", str(next_run))
    A.salvage_deck_from_response(None, _response(json.dumps({"html_content": "<h1>S</h1>"})))

    assert (next_run / "output" / "presentation" / "index.html").read_text() == "<h1>S</h1>"


def test_the_measurement_helpers_touch_no_disk_at_all_when_off(base: Path) -> None:
    """They return before resolving the output base, so it is never created.

    ``_output_base`` has an ``mkdir`` in it. A guard placed after the base
    was resolved would still leave an empty ``output/`` behind in a run
    that never wrote anything — a real difference from the shipped board,
    however small.
    """
    A.snapshot_deck("h", "c", "j")
    assert A.salvage_deck_from_response(None, _response("{}")) is None

    assert not base.exists()
