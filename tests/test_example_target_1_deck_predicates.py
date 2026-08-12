"""The target_1_presentation board grades the deck on disk, not the reply.

The deliverable this target produces is a rendered webpage the agent writes
through ``write_webpage``; the agent's closing chat message is a report
*about* it. Grading the message instead of the file is invisible to every
real improvement in the deck, and it is wrong in both directions — a run
that wrote a good deck and summarised it tersely fails, and a run that wrote
no file at all but narrated slide titles passes.

Both directions are pinned below. The predicates resolve the run's output
root from ``ZICATO_RUN_SCRATCH_DIR`` (the contract the tournament worker
exports), so these tests point that variable at a tmp directory and lay out
decks by hand — no model, no agent tree, no live call.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from pathlib import Path

import pytest

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
    """Minimal ``RunResult`` stand-in carrying just the transcript surface."""

    final_output: str = ""
    transcript: tuple[str, ...] = field(default_factory=tuple)


def _deck_html(title: str, slides: list[str]) -> str:
    """A deck in the shape the web_developer is instructed to emit."""
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


def _write_deck(root: Path, slug: str, html: str) -> Path:
    """Lay a three-file deck down under ``<root>/output/<slug>/``."""
    directory = root / "output" / slug
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "index.html").write_text(html)
    (directory / "styles.css").write_text(".slide { display: none; }\n")
    (directory / "script.js").write_text("function next() {}\n")
    return directory


@pytest.fixture
def scratch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the run-output contract at a fresh per-test scratch directory."""
    root = tmp_path / "scratch"
    root.mkdir()
    monkeypatch.setenv("ZICATO_RUN_SCRATCH_DIR", str(root))
    return root


_WAFFLE_DECK = _deck_html(
    "The Wonderful World of Waffles",
    [
        "Introduction: what a waffle is",
        "A short history of the waffle iron",
        "Belgian versus American waffles",
        "Why waffles still matter",
    ],
)

# What the coordinator actually reports back: a short confirmation that
# names neither the topic nor a single slide. Observed shape — the June
# runs' scored replies ran ~150 characters.
_TERSE_REPLY = "The presentation has been created and reviewed. No critical issues remain."


def test_narrated_slides_without_a_file_do_not_pass(scratch: Path) -> None:
    """A reply that lists slides but wrote no deck must FAIL.

    The failure this pins: the agent describes a deck in prose, never calls
    ``write_webpage``, and the board scores it as a success because the word
    it looks for is in the narration.
    """
    narration = (
        "Slide 1: Waffles — a brief introduction.\n"
        "Slide 2: A short history of waffles.\n"
        "Slide 3: Belgian vs American waffles.\n"
    )
    result = _Result(final_output=narration)

    assert wrote_presentation_file(result) is False
    assert mentions_waffles(result) is False
    assert has_slide_titles(result) is False


def test_written_deck_passes_despite_a_terse_reply(scratch: Path) -> None:
    """A real deck on disk must PASS even when the reply says nothing.

    The mirror failure: the run did everything right, and the board scored
    the coordinator's one-line confirmation instead of the artifact.
    """
    _write_deck(scratch, "waffles", _WAFFLE_DECK)
    result = _Result(final_output=_TERSE_REPLY)

    assert wrote_presentation_file(result) is True
    assert mentions_waffles(result) is True
    assert has_slide_titles(result) is True
    assert has_structured_outline(result) is True


def test_deck_is_found_under_any_slug(scratch: Path) -> None:
    """Detection does not depend on the directory name the agent chose.

    WHERE under the output root the deck landed is the file-findability
    judge's business — the write/read slug agreement is this board's
    designed difficulty and is graded as process drift. Whether a usable
    deck exists at all is graded here, so an embellished slug must not
    make a real deck invisible.
    """
    _write_deck(scratch, "the_wonderful_world_of_waffles_presentation", _WAFFLE_DECK)

    assert mentions_waffles(_Result()) is True


def test_topical_predicates_read_the_deck_not_the_reply(scratch: Path) -> None:
    """An off-topic deck fails even when the reply names the topic."""
    _write_deck(scratch, "waffles", _WAFFLE_DECK)
    result = _Result(final_output="Here are your slides on transformers and attention.")

    assert mentions_transformers(result) is False
    assert mentions_waffles(result) is True


def test_unlinked_html_is_not_a_usable_deck(scratch: Path) -> None:
    """The stylesheet / script links the developer MUST emit are graded.

    ``web_developer_instruction`` requires both references "so the files
    are connected properly". A page that renders none of its own CSS or JS
    is not the deliverable, however good its markup.
    """
    unlinked = _WAFFLE_DECK.replace('<link rel="stylesheet" href="styles.css">', "").replace(
        '<script src="script.js"></script>', ""
    )
    _write_deck(scratch, "waffles", unlinked)

    assert wrote_presentation_file(_Result()) is False


def test_thin_deck_fails_the_slide_count(scratch: Path) -> None:
    """Below three slides the artifact is too thin to be a deck."""
    _write_deck(scratch, "waffles", _deck_html("Waffles", ["Only one slide"]))

    assert wrote_presentation_file(_Result()) is True
    assert has_slide_titles(_Result()) is False


def test_newest_deck_wins_when_the_run_wrote_more_than_one(scratch: Path) -> None:
    """A run that wrote under two slugs is graded on what it ended with.

    Grading the union would credit a run for a deck it had already
    abandoned; the deliverable is the one standing at the end of the run.
    """
    first = _write_deck(scratch, "waffles", _WAFFLE_DECK)
    second = _write_deck(
        scratch,
        "transformers",
        _deck_html("Transformers", ["Attention", "Encoders", "Applications"]),
    )
    os.utime(first / "index.html", (1_700_000_000, 1_700_000_000))
    os.utime(second / "index.html", (1_700_000_100, 1_700_000_100))

    assert mentions_transformers(_Result()) is True
    assert mentions_waffles(_Result()) is False


def test_the_shipped_board_entry_grades_the_deck(scratch: Path) -> None:
    """The whole seam, driven from ``board.jsonl``, not from an import.

    Loads the real ``waffles_single`` entry and evaluates its expectation
    through :func:`zicato.board.matchers.evaluate_expectation` — the same
    call the tournament worker makes, in the same process that carries
    ``ZICATO_RUN_SCRATCH_DIR``. Guards the dotted path in the board
    against drifting away from the predicate that backs it.
    """
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
    result = _Result(final_output=_TERSE_REPLY)

    verdict = asyncio.run(evaluate_expectation(entry.expectation, result))
    assert verdict.passed is False

    _write_deck(scratch, "waffles", _WAFFLE_DECK)
    verdict = asyncio.run(evaluate_expectation(entry.expectation, result))
    assert verdict.passed is True


def test_raccoon_drift_is_graded_on_the_deck_and_never_vacuously(scratch: Path) -> None:
    """The negative predicate needs a deck before it can credit suppression."""
    assert avoids_offtopic_raccoons(_Result()) is False

    _write_deck(scratch, "waffles", _WAFFLE_DECK)
    assert avoids_offtopic_raccoons(_Result()) is True

    drifted = _deck_html("Waffles", ["Waffles", "History", "Raccoons like waffles too"])
    _write_deck(scratch, "waffles", drifted)
    assert avoids_offtopic_raccoons(_Result()) is False


def test_no_output_root_is_a_clean_failure(scratch: Path) -> None:
    """A run whose tools never fired fails without raising."""
    assert deck_files() == {}
    assert wrote_presentation_file(_Result()) is False
    assert mentions_waffles(_Result()) is False
    assert has_slide_titles(_Result()) is False
    assert has_structured_outline(_Result()) is False
    # Robustness: a result missing every attribute must not raise either.
    assert mentions_waffles(object()) is False
