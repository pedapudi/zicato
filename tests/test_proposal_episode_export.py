"""Foe's static page for one proposal episode: written, served, and degraded.

Foe renders a finished episode to one self-contained HTML page, and zicato
writes that page beside the episode's log when the episode settles so an
operator can read the round at Foe's own depth. The page is an artifact
rather than a service: nothing serves it while the round runs, no record
points at a URL, and the dashboard reaches it by the same coordinates it
reaches the episode's native transcript by.

Writing it is best effort. The proofs below hold both halves of that: a
settled episode has a page, and an episode whose export fails ends the
round exactly as it would have without one.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from tests._foe_support import call_turn, fake_foe_binary, return_turn
from tests.test_proposer_foe_agent import _EDITED_FILE, _HYPOTHESIS, Workspace, _edit
from zicato.dashboard.server import create_app
from zicato.proposer import episode_export
from zicato.proposer.episode_export import EXPORT_FILENAME, export_command, write_episode_export
from zicato.query.paths import WorkspacePaths
from zicato.query.transcript_view import (
    build_proposal_episode_export,
    read_proposal_episode_export,
)
from zicato.workspace import WorkspaceLayout

EPOCH = "e1"
GENERATION = "v1"

#: The two turns that carry one proposal to a settled episode: an edit of
#: the working copy, then the hypothesis that explains it.
_PROPOSING_TURNS = [call_turn(_edit(_EDITED_FILE)), return_turn(_HYPOTHESIS)]

#: One ``episode/start`` line, which is all a served workspace needs: the
#: readers resolve an episode by its log, and only the dashboard tests below
#: read one, never the reconstruction.
_MINIMAL_LOG = (
    json.dumps(
        {
            "seq": 0,
            "time": 1_724_200_000_000,
            "version": 3,
            "type": "episode/start",
            "data": {"id": "ep_propose_v1", "contract": {"name": "proposer"}},
        }
    )
    + "\n"
)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _under(root: Path) -> set[str]:
    """Every file under ``root``, named relative to it."""
    return {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}


def _proposing_workspace(directory: Path) -> Workspace:
    directory.mkdir(parents=True, exist_ok=True)
    return Workspace(directory, _PROPOSING_TURNS)


# ---------------------------------------------------------------------------
# The page is written when the episode settles
# ---------------------------------------------------------------------------


def test_a_settled_episode_has_a_page_beside_its_log(tmp_path: Path) -> None:
    workspace = _proposing_workspace(tmp_path)
    asyncio.run(workspace.agent().propose(workspace.context()))

    directory = workspace.episode_log()
    page = directory / EXPORT_FILENAME
    assert page.is_file(), sorted(p.name for p in directory.iterdir())
    assert (directory / "episode.jsonl").is_file()
    assert page.read_text(encoding="utf-8").startswith("<!doctype html>")


def test_the_page_is_rendered_from_that_episode_s_own_log(tmp_path: Path) -> None:
    """The page names the episode whose directory it sits in."""
    workspace = _proposing_workspace(tmp_path)
    asyncio.run(workspace.agent().propose(workspace.context()))

    directory = workspace.episode_log()
    first = json.loads((directory / "episode.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert first["data"]["id"] in (directory / EXPORT_FILENAME).read_text(encoding="utf-8")


def test_the_export_is_the_configured_binary_over_the_episode_directory(
    tmp_path: Path,
) -> None:
    """One spelling drives the render and the caption alike."""
    binary = fake_foe_binary(tmp_path / "bin")
    assert export_command(binary, tmp_path / "episodes" / "v1") == [
        str(binary),
        "view",
        str(tmp_path / "episodes" / "v1"),
    ]


def test_the_round_gains_the_page_and_no_other_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two identical proposals, one with no export, differ by one file.

    The export writes a page and nothing else: no round-log record, no
    second copy of the episode's events, and no URL anywhere.
    """
    with_page = _proposing_workspace(tmp_path / "with-export")
    asyncio.run(with_page.agent().propose(with_page.context()))

    async def render_nothing(binary: object, episode_dir: object) -> None:
        return None

    monkeypatch.setattr(episode_export, "write_episode_export", render_nothing)
    without = _proposing_workspace(tmp_path / "without-export")
    asyncio.run(without.agent().propose(without.context()))

    assert _under(with_page.root) - _under(without.root) == {
        str(Path("epochs") / EPOCH / "episodes" / GENERATION / EXPORT_FILENAME)
    }
    assert _under(without.root) - _under(with_page.root) == set()


# ---------------------------------------------------------------------------
# A failed export leaves the round alone
# ---------------------------------------------------------------------------


def test_a_failing_export_leaves_the_round_and_its_log_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The forced failure: the round settles with the same experiment.

    The reference run and the failing run script the same turns, so their
    experiments are comparable field for field. What differs is the export,
    which raises instead of rendering.
    """
    reference = _proposing_workspace(tmp_path / "reference")
    expected = asyncio.run(reference.agent().propose(reference.context()))

    async def refuse(binary: object, episode_dir: object) -> None:
        raise OSError("the export could not run")

    monkeypatch.setattr(episode_export, "write_episode_export", refuse)
    workspace = _proposing_workspace(tmp_path / "failing")
    experiment = asyncio.run(workspace.agent().propose(workspace.context()))

    assert experiment.hypothesis.core_idea == expected.hypothesis.core_idea
    assert [(p.mutation_id, p.new_content) for p in experiment.patches] == [
        (p.mutation_id, p.new_content) for p in expected.patches
    ]
    directory = workspace.episode_log()
    assert (directory / "episode.jsonl").is_file()
    assert not (directory / EXPORT_FILENAME).exists()


@pytest.mark.parametrize(
    ("what", "prepare"),
    [
        ("a directory with no log to render", lambda d: d.mkdir(parents=True)),
        ("a log holding no events", lambda d: _write(d / "episode.jsonl", "")),
    ],
)
def test_an_export_the_binary_refuses_writes_no_page(
    tmp_path: Path, what: str, prepare: Callable[[Path], object]
) -> None:
    binary = fake_foe_binary(tmp_path / "bin")
    directory = tmp_path / "episodes" / GENERATION
    prepare(directory)

    assert asyncio.run(write_episode_export(binary, directory)) is None, what
    assert not (directory / EXPORT_FILENAME).exists()


def test_a_binary_that_cannot_be_run_writes_no_page(tmp_path: Path) -> None:
    directory = _write(
        tmp_path / "episodes" / GENERATION / "episode.jsonl",
        '{"seq":0,"type":"episode/start","data":{"id":"ep"}}\n',
    ).parent

    assert asyncio.run(write_episode_export(tmp_path / "absent-binary", directory)) is None
    assert not (directory / EXPORT_FILENAME).exists()


# ---------------------------------------------------------------------------
# What the dashboard reads
# ---------------------------------------------------------------------------


def _served_workspace(tmp_path: Path, *, with_page: bool) -> Path:
    """A ``.zicato/`` holding one proposal episode, with or without its page."""
    root = tmp_path / ".zicato"
    directory = WorkspaceLayout.from_root(root).proposal_episode_dir(EPOCH, GENERATION)
    _write(directory / "episode.jsonl", _MINIMAL_LOG)
    if with_page:
        _write(directory / EXPORT_FILENAME, "<!doctype html>\n<html><body>episode</body></html>\n")
    return root


def test_the_reader_reports_a_page_that_exists(tmp_path: Path) -> None:
    paths = WorkspacePaths(_served_workspace(tmp_path, with_page=True))
    payload = build_proposal_episode_export(paths, EPOCH, GENERATION)

    assert payload["export_available"] is True
    assert payload["episode_log"].endswith(f"episodes/{GENERATION}/episode.jsonl")
    served = read_proposal_episode_export(paths, EPOCH, GENERATION)
    assert served is not None and "episode" in served


def test_a_candidate_with_no_page_reads_the_log_and_the_command_to_render_it(
    tmp_path: Path,
) -> None:
    paths = WorkspacePaths(_served_workspace(tmp_path, with_page=False))
    payload = build_proposal_episode_export(paths, EPOCH, GENERATION)

    assert payload["export_available"] is False
    episode_dir = Path(payload["episode_log"]).parent
    assert payload["command"] == f"foe view {episode_dir}"
    assert read_proposal_episode_export(paths, EPOCH, GENERATION) is None


def test_a_candidate_with_no_episode_at_all_names_nothing(tmp_path: Path) -> None:
    """A seed, or a workspace whose configured binary is still the placeholder."""
    paths = WorkspacePaths(_served_workspace(tmp_path, with_page=True))

    assert build_proposal_episode_export(paths, EPOCH, "v9") == {
        "epoch_id": EPOCH,
        "generation_id": "v9",
        "slot": None,
        "episode_log": "",
        "export_available": False,
        "command": "",
    }


def test_the_command_names_the_binary_the_workspace_configured(tmp_path: Path) -> None:
    root = _served_workspace(tmp_path, with_page=False)
    binary = tmp_path / "opt" / "foe"
    _write(
        root / "config.json",
        json.dumps(
            {
                "proposer": {
                    "binary": str(binary),
                    "model": {"provider": "exec", "model": "stand-in"},
                }
            }
        ),
    )
    payload = build_proposal_episode_export(WorkspacePaths(root), EPOCH, GENERATION)

    assert payload["command"].startswith(f"{binary} view ")


# ---------------------------------------------------------------------------
# The served route
# ---------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path, static_dir: Path) -> TestClient:
    return TestClient(
        create_app(_served_workspace(tmp_path, with_page=True), static_dir, read_only=True)
    )


def test_the_route_serves_the_page_as_html(client: TestClient) -> None:
    response = client.get(f"/api/generation/{EPOCH}/{GENERATION}/episode-export.html")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "episode" in response.text


def test_the_availability_route_answers_what_the_panel_branches_on(
    client: TestClient,
) -> None:
    payload = client.get(f"/api/generation/{EPOCH}/{GENERATION}/episode-export").json()

    assert payload["export_available"] is True
    assert payload["generation_id"] == GENERATION
    assert payload["slot"] is None


def test_a_candidate_with_no_page_is_a_404(tmp_path: Path, static_dir: Path) -> None:
    client = TestClient(
        create_app(_served_workspace(tmp_path, with_page=False), static_dir, read_only=True)
    )

    response = client.get(f"/api/generation/{EPOCH}/{GENERATION}/episode-export.html")
    assert response.status_code == 404


@pytest.mark.parametrize("coordinate", ["../../../etc", "..", "a/b", "with space"])
def test_the_route_refuses_a_coordinate_that_names_a_foreign_path(
    client: TestClient, coordinate: str
) -> None:
    """No request reaches a file outside the episode the coordinates name."""
    for url in (
        f"/api/generation/{EPOCH}/{coordinate}/episode-export.html",
        f"/api/generation/{coordinate}/{GENERATION}/episode-export.html",
    ):
        response = client.get(url)
        assert response.status_code in (400, 404), url
        assert "<!doctype html>" not in response.text.lower()


def test_a_page_outside_the_episode_directory_is_never_served(
    tmp_path: Path, static_dir: Path
) -> None:
    """The route resolves the file; the caller never names one."""
    root = _served_workspace(tmp_path, with_page=True)
    secret = _write(tmp_path / "secret.html", "<!doctype html>\n<p>not an episode</p>\n")
    client = TestClient(create_app(root, static_dir, read_only=True))

    for name in ("path", "file"):
        response = client.get(
            f"/api/generation/{EPOCH}/{GENERATION}/episode-export.html?{name}={secret}"
        )
        assert "not an episode" not in response.text
