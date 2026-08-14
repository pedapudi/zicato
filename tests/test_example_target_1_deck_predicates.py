"""The presentation board grades captured deck artifacts, not the reply."""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from zicato.core.types import ArtifactSet
from zicato.tournament.artifacts import capture_run_artifacts
from zicato_examples.target_1_presentation.predicates import (
    avoids_offtopic_raccoons,
    deck_files,
    has_slide_titles,
    has_structured_outline,
    mentions_transformers,
    mentions_waffles,
    wrote_presentation_file,
)


@dataclass
class _Result:
    final_output: str = ""
    transcript: tuple[str, ...] = ()
    artifacts: ArtifactSet | None = None


Capture = Callable[..., _Result]


def _deck_html(title: str, slides: list[str]) -> str:
    sections = "\n".join(
        f'  <section class="slide" id="slide-{n}"><h2>{text}</h2></section>'
        for n, text in enumerate(slides, start=1)
    )
    return (
        "<!DOCTYPE html>\n<html>\n<head>\n"
        f"  <title>{title}</title>\n"
        '  <link rel="stylesheet" href="styles.css">\n'
        "</head>\n<body>\n"
        f"{sections}\n"
        '  <script src="script.js"></script>\n'
        "</body>\n</html>\n"
    )


def _write_deck(root: Path, slug: str, html: str) -> None:
    directory = root / "output" / slug
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "index.html").write_text(html)
    (directory / "styles.css").write_text(".slide { display: none; }\n")
    (directory / "script.js").write_text("function next() {}\n")


@pytest.fixture
def scratch(tmp_path: Path) -> Path:
    root = tmp_path / "scratch"
    root.mkdir()
    return root


@pytest.fixture
def captured(tmp_path: Path) -> Capture:
    sequence = iter(range(100))

    def make(
        scratch: Path,
        *,
        final_output: str = "",
        transcript: tuple[str, ...] = (),
        max_files: int = 1_000,
    ) -> _Result:
        artifacts = capture_run_artifacts(
            scratch,
            tmp_path / f"run-{next(sequence)}" / "loss.json",
            max_files=max_files,
        )
        return _Result(final_output=final_output, transcript=transcript, artifacts=artifacts)

    return make


_WAFFLE_DECK = _deck_html(
    "The Wonderful World of Waffles",
    [
        "Introduction: what a waffle is",
        "A short history of the waffle iron",
        "Belgian versus American waffles",
        "Why waffles still matter",
    ],
)

_TERSE_REPLY = "The presentation has been created and reviewed. No critical issues remain."


def test_narrated_slides_without_a_file_do_not_pass(scratch: Path, captured: Capture) -> None:
    narration = (
        "Slide 1: Waffles — a brief introduction.\n"
        "Slide 2: A short history of waffles.\n"
        "Slide 3: Belgian vs American waffles.\n"
    )
    result = captured(scratch, final_output=narration)

    assert wrote_presentation_file(result) is False
    assert mentions_waffles(result) is False
    assert has_slide_titles(result) is False


def test_written_deck_passes_despite_a_terse_reply(scratch: Path, captured: Capture) -> None:
    _write_deck(scratch, "waffles", _WAFFLE_DECK)
    result = captured(scratch, final_output=_TERSE_REPLY)

    assert wrote_presentation_file(result) is True
    assert mentions_waffles(result) is True
    assert has_slide_titles(result) is True
    assert has_structured_outline(result) is True


def test_deck_is_found_under_any_slug(scratch: Path, captured: Capture) -> None:
    _write_deck(scratch, "the_wonderful_world_of_waffles_presentation", _WAFFLE_DECK)

    assert mentions_waffles(captured(scratch)) is True


def test_topical_predicates_read_the_deck_not_the_reply(scratch: Path, captured: Capture) -> None:
    _write_deck(scratch, "waffles", _WAFFLE_DECK)
    result = captured(scratch, final_output="Here are your slides on transformers and attention.")

    assert mentions_transformers(result) is False
    assert mentions_waffles(result) is True


def test_unlinked_html_is_not_a_usable_deck(scratch: Path, captured: Capture) -> None:
    unlinked = _WAFFLE_DECK.replace('<link rel="stylesheet" href="styles.css">', "").replace(
        '<script src="script.js"></script>', ""
    )
    _write_deck(scratch, "waffles", unlinked)

    assert wrote_presentation_file(captured(scratch)) is False


def test_thin_deck_fails_the_slide_count(scratch: Path, captured: Capture) -> None:
    _write_deck(scratch, "waffles", _deck_html("Waffles", ["Only one slide"]))

    result = captured(scratch)
    assert wrote_presentation_file(result) is True
    assert has_slide_titles(result) is False


def test_multiple_direct_decks_fail_closed(scratch: Path, captured: Capture) -> None:
    _write_deck(scratch, "waffles", _WAFFLE_DECK)
    _write_deck(
        scratch,
        "transformers",
        _deck_html("Transformers", ["Attention", "Encoders", "Applications"]),
    )

    result = captured(scratch)
    assert deck_files(result) == {}
    assert mentions_transformers(result) is False
    assert mentions_waffles(result) is False


def test_the_shipped_board_entry_grades_the_deck(scratch: Path, captured: Capture) -> None:
    from zicato.board.jsonl import load_board
    from zicato.board.matchers import evaluate_expectation

    board_path = (
        Path(__file__).resolve().parents[1]
        / "examples"
        / "zicato_examples"
        / "target_1_presentation"
        / "board.jsonl"
    )
    entry = next(e for e in load_board(board_path) if e.id == "waffles_single")
    assert entry.expectation is not None
    result = captured(scratch, final_output=_TERSE_REPLY)

    verdict = asyncio.run(evaluate_expectation(entry.expectation, result))
    assert verdict.passed is False

    _write_deck(scratch, "waffles", _WAFFLE_DECK)
    result = captured(scratch, final_output=_TERSE_REPLY)
    verdict = asyncio.run(evaluate_expectation(entry.expectation, result))
    assert verdict.passed is True


def test_raccoon_drift_is_graded_on_the_deck_and_never_vacuously(
    scratch: Path, captured: Capture
) -> None:
    assert avoids_offtopic_raccoons(captured(scratch)) is False

    _write_deck(scratch, "waffles", _WAFFLE_DECK)
    assert avoids_offtopic_raccoons(captured(scratch)) is True

    drifted = _deck_html("Waffles", ["Waffles", "History", "Raccoons like waffles too"])
    _write_deck(scratch, "waffles", drifted)
    assert avoids_offtopic_raccoons(captured(scratch)) is False


def test_only_inventoried_artifacts_are_gradeable(
    scratch: Path,
    captured: Capture,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    absent = _Result()
    assert deck_files(absent) == {}
    assert not any(
        predicate(absent)
        for predicate in (
            wrote_presentation_file,
            mentions_waffles,
            has_slide_titles,
            has_structured_outline,
        )
    )
    assert mentions_waffles(object()) is False

    uninventoried = captured(scratch)
    assert uninventoried.artifacts is not None
    _write_deck(uninventoried.artifacts.root, "waffles", _WAFFLE_DECK)
    assert wrote_presentation_file(uninventoried) is False

    _write_deck(scratch, "waffles", _WAFFLE_DECK)
    result = captured(scratch)
    decoy = tmp_path / "decoy"
    _write_deck(decoy, "transformers", _deck_html("Transformers", ["A", "B", "C"]))
    monkeypatch.setenv("ZICATO_RUN_SCRATCH_DIR", str(decoy))

    assert mentions_waffles(result) is True
    assert mentions_transformers(result) is False
    truncated = captured(scratch, max_files=0)
    assert truncated.artifacts is not None and truncated.artifacts.truncated
    assert wrote_presentation_file(truncated) is False


def test_durable_artifacts_grade_after_scratch_cleanup(scratch: Path, captured: Capture) -> None:
    _write_deck(scratch, "waffles", _WAFFLE_DECK)
    result = captured(scratch)
    shutil.rmtree(scratch)

    assert mentions_waffles(result) is True


def test_measurement_history_is_not_a_competing_deck(scratch: Path, captured: Capture) -> None:
    _write_deck(scratch, "presentation", _WAFFLE_DECK)
    history = scratch / "output" / "deck_history" / "turn_1"
    history.mkdir(parents=True)
    for name in ("index.html", "styles.css", "script.js"):
        (history / name).write_text("transformer attention")
    result = captured(scratch)

    assert mentions_waffles(result) is True
    assert mentions_transformers(result) is False
