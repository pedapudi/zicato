"""The console controller: the repaint discipline, navigation, addressing.

The repaint guard is the headline. The browser's rule — never rebuild DOM on a
no-op heartbeat — is stated here as counters: a refresh that finds identical
content must repaint zero times and patch zero rows, and a refresh that finds
ONE changed number must patch only the rows that carry it.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from tests.tui_fixture import CHALLENGER, EPOCH, PAYLOADS, live_payloads
from zicato.tui.client import SnapshotClient
from zicato.tui.console import Console
from zicato.tui.routes import BROWSER_ONLY, LENSES, Route, parse_route


class MutableClient(SnapshotClient):
    """A snapshot client whose payloads can be edited between refreshes."""

    def replace(self, path: str, payload: Any) -> None:
        self._payloads[path] = payload

    def snapshot(self, path: str) -> Any:
        return deepcopy(self._payloads[path])


def console(state: str = "settled", **kwargs: Any) -> Console:
    payloads = deepcopy(PAYLOADS if state == "settled" else live_payloads())
    return Console(client=MutableClient(payloads), **kwargs)


# ---------------------------------------------------------------------------
# The repaint discipline
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("lens", [lens for lens in LENSES])
def test_a_no_op_heartbeat_patches_zero_cells(lens: str) -> None:
    """THE guard. An unchanged payload must not touch a single row.

    A TUI that flickers on every SSE tick has failed the same way the dashboard
    once did. This is asserted per lens, because the rot always starts in the
    one lens whose digest quietly folded a timestamp.
    """
    c = console(route=Route(lens=lens, params={"epoch": EPOCH, "gen": CHALLENGER}))
    assert c.refresh() is True  # first paint
    first_patches = c.row_patches

    for _ in range(20):
        assert c.refresh() is False
    assert c.repaints == 1
    assert c.noops == 20
    assert c.row_patches == first_patches


def test_a_live_pipeline_tick_with_no_movement_is_still_a_no_op() -> None:
    """``generated_at`` moves on every beat and paints nothing — so it must not
    enter the digest. This is the exact shape of the original browser bug."""
    c = console("live", route=Route(lens="home", params={"epoch": EPOCH}))
    c.refresh()
    pipeline = c.client.snapshot("/api/live/pipeline")
    pipeline["generated_at"] = "2026-07-04T11:59:59Z"
    c.client.replace("/api/live/pipeline", pipeline)
    assert c.refresh() is False
    assert c.repaints == 1


def test_one_changed_number_patches_only_the_rows_that_carry_it() -> None:
    c = console(route=Route(lens="home", params={"epoch": EPOCH}))
    c.refresh()
    baseline = c.row_patches

    cost = c.client.snapshot(f"/api/epoch/{EPOCH}/cost")
    cost["cost_per_promotion_ms"] = 3_000_000
    c.client.replace(f"/api/epoch/{EPOCH}/cost", cost)

    assert c.refresh() is True
    assert c.row_patches - baseline == 1


def test_a_new_phase_repaints_the_lifeline() -> None:
    c = console("live", route=Route(lens="home", params={"epoch": EPOCH}))
    c.refresh()
    before = c.repaints
    pipeline = c.client.snapshot("/api/live/pipeline")
    pipeline["active_step"] = "gate"
    pipeline["phase"] = "gate"
    for step in pipeline["steps"]:
        step["state"] = "running" if step["id"] == "gate" else "done"
    c.client.replace("/api/live/pipeline", pipeline)
    assert c.refresh() is True
    assert c.repaints == before + 1


def test_row_identity_is_view_unique_not_block_unique() -> None:
    """Six tables all have a ``head`` row; the diff must not confuse them."""
    c = console(route=Route(lens="candidate", params={"epoch": EPOCH, "gen": CHALLENGER}))
    c.refresh()
    assert c.view is not None
    keys = [key for key, _ in c.view.lines()]
    assert len(keys) == len(set(keys))
    assert sum(1 for k in keys if k.endswith(":head")) > 1


def test_refresh_uses_one_snapshot_per_pass() -> None:
    """Each pass re-fetches; a lens fetching a path twice must not see two states."""
    c = console(route=Route(lens="home", params={"epoch": EPOCH}))
    c.refresh()
    first = len(c.client.requested)
    c.refresh()
    assert len(c.client.requested) == first  # begin_pass cleared the log, same count


# ---------------------------------------------------------------------------
# Navigation
# ---------------------------------------------------------------------------


def test_cursor_clamps_and_never_wraps() -> None:
    c = console(route=Route(lens="standings", params={"epoch": EPOCH}))
    c.refresh()
    c.move(-5)
    assert c.cursor == 0
    c.move(500)
    assert c.cursor == len(c.rows) - 1
    c.move(1)
    assert c.cursor == len(c.rows) - 1


def test_drill_opens_the_candidate_and_back_returns() -> None:
    c = console(route=Route(lens="standings", params={"epoch": EPOCH}))
    c.refresh()
    row = next(r for r in c.rows if (r.action or "").startswith("candidate/"))
    c.cursor = c.rows.index(row)
    assert c.drill() is None
    assert c.route.lens == "candidate"
    assert c.route.params["gen"] == row.action.split("/", 1)[1]
    assert c.route.params["epoch"] == EPOCH  # the epoch travels with the drill
    assert c.back() is True
    assert c.route.lens == "standings"
    assert c.back() is False


def test_drill_on_a_command_row_parks_it_rather_than_running_it() -> None:
    """The console never executes anything itself — the app owns the shell."""
    c = console(route=Route(lens="instrument", params={"epoch": EPOCH}))
    c.refresh()
    row = next(r for r in c.rows if (r.action or "").startswith("!"))
    c.cursor = c.rows.index(row)
    command = c.drill()
    assert command == c.pending_command == "zicato reflect apply refl-2026-07-04 f-dead-entry"
    assert c.route.lens == "instrument"  # no navigation happened
    assert c.take_command() == command
    assert c.take_command() is None  # handed over exactly once


def test_jump_keeps_the_epoch() -> None:
    c = console(route=Route(lens="home", params={"epoch": EPOCH}))
    c.jump(4)
    assert c.route.lens == "board"
    assert c.route.params == {"epoch": EPOCH}
    c.jump(99)
    assert c.route.lens == "board"


def test_filter_narrows_the_selectable_rows() -> None:
    c = console("live", route=Route(lens="standings", params={"epoch": EPOCH}))
    c.refresh()
    total = len(c.rows)
    c.set_filter("v6")
    assert 0 < len(c.rows) < total
    assert all("v6" in r.text for r in c.rows)
    c.set_filter("")
    assert len(c.rows) == total


def test_resize_invalidates_the_cached_view() -> None:
    """Width is a render input the digest cannot see, so it forces a repaint."""
    c = console(route=Route(lens="standings", params={"epoch": EPOCH}))
    c.refresh()
    assert c.refresh() is False
    c.resize(80)
    assert c.refresh() is True


# ---------------------------------------------------------------------------
# Addressing — one scheme for both surfaces
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "lens", "params"),
    [
        ("", "home", {}),
        ("/", "home", {}),
        ("#/", "home", {}),
        ("/logs", "health", {}),
        ("standings", "standings", {}),
        ("candidate/v4", "candidate", {"gen": "v4"}),
        ("instrument/refl-1", "instrument", {"reflection": "refl-1"}),
        (f"/e/{EPOCH}", "home", {"epoch": EPOCH}),
        (f"#/e/{EPOCH}/gens", "standings", {"epoch": EPOCH}),
        (f"/e/{EPOCH}/gen/v4", "candidate", {"epoch": EPOCH, "gen": "v4"}),
        (f"/e/{EPOCH}/boards", "board", {"epoch": EPOCH}),
        (f"/e/{EPOCH}/board/plan-long-1", "board", {"epoch": EPOCH, "entry": "plan-long-1"}),
        (
            f"/e/{EPOCH}/instrument/refl-1/judge_a/run:1",
            "instrument",
            {"epoch": EPOCH, "reflection": "refl-1", "judge": "judge_a", "run_ref": "run:1"},
        ),
    ],
)
def test_browser_hash_paths_resolve_to_the_same_lens(
    path: str, lens: str, params: dict[str, str]
) -> None:
    route = parse_route(path)
    assert (route.lens, route.params) == (lens, params)


@pytest.mark.parametrize("view", sorted(BROWSER_ONLY))
def test_browser_only_views_land_somewhere_and_say_so(view: str) -> None:
    """An unrendered surface is DECLARED, never silently absent."""
    route = parse_route(f"/e/{EPOCH}/{view}") if view != "settings" else parse_route(f"/{view}")
    assert route.unsupported == view
    assert route.lens in {lens for lens in LENSES}


def test_route_round_trips_to_a_browser_path() -> None:
    for path in (f"/e/{EPOCH}/gen/v4", f"/e/{EPOCH}/gens", "/logs"):
        assert parse_route(parse_route(path).to_path()) == parse_route(path)


def test_compare_suffix_params_are_ignored_not_misread() -> None:
    """The browser's ``~cmp=`` target has no terminal lens; the path still works."""
    route = parse_route(f"/e/{EPOCH}/gen/v4~cmp=v3")
    assert (route.lens, route.params) == ("candidate", {"epoch": EPOCH, "gen": "v4"})
