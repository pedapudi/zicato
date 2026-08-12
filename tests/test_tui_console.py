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
from zicato.tui.routes import BROWSER_ONLY, DEFERRED, LENSES, UNSHIPPED, Route, parse_route


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


@pytest.mark.parametrize("lens", list(LENSES))
def test_row_identity_is_view_unique_not_block_unique(lens: str) -> None:
    """Every block synthesises a ``@title``/``@gap`` line and tables share
    a ``head`` key, so identity must be qualified by block — otherwise a
    repaint matches one table's header against another's and misses changes."""
    c = console("live", route=Route(lens=lens, params={"epoch": EPOCH}))
    c.refresh()
    assert c.view is not None
    keys = [key for key, _ in c.view.lines()]
    assert len(keys) == len(set(keys)), "line keys must be unique across the view"
    assert sum(1 for k in keys if k.endswith(":@title")) >= 1


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


def test_drill_opens_a_reflection_and_back_returns() -> None:
    c = console(route=Route(lens="instrument", params={"epoch": EPOCH}))
    c.refresh()
    row = next(r for r in c.rows if (r.action or "").startswith("instrument/"))
    c.cursor = c.rows.index(row)
    c.drill()
    assert c.route.lens == "instrument"
    assert c.route.params["reflection"] == row.action.split("/", 1)[1]
    assert c.route.params["epoch"] == EPOCH  # the epoch travels with the drill
    assert c.back() is True
    assert c.back() is False


def test_no_lens_offers_a_drill_into_a_deferred_lens() -> None:
    """A drill that lands back where it started is worse than no drill.

    It clears the back stack and tells the operator nothing. With Candidate
    deferred, the standings row IS the terminal evidence — the drawer carries
    the detail the dossier would have.
    """
    for lens in LENSES:
        c = console("live", route=Route(lens=lens, params={"epoch": EPOCH}))
        c.refresh()
        for row in c.rows:
            if row.action is None:
                continue
            assert parse_route(row.action).unsupported is None, (lens, row.key)


def test_the_console_has_no_way_to_execute_anything() -> None:
    """Read-only by construction: every action is a route, never a command."""
    c = console(route=Route(lens="instrument", params={"epoch": EPOCH}))
    c.refresh()
    assert not hasattr(c, "pending_command")
    for row in c.rows:
        assert not (row.action or "").startswith("!")


def test_jump_keeps_the_epoch() -> None:
    c = console(route=Route(lens="home", params={"epoch": EPOCH}))
    c.jump(3)
    assert c.route.lens == "instrument"
    assert c.route.params == {"epoch": EPOCH}
    c.jump(99)  # past the rail — ignored, not a crash or a wrap
    assert c.route.lens == "instrument"


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
        ("standings", "standings", {}),
        ("instrument/refl-1", "instrument", {"reflection": "refl-1"}),
        (f"/e/{EPOCH}", "home", {"epoch": EPOCH}),
        (f"#/e/{EPOCH}/gens", "standings", {"epoch": EPOCH}),
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
    assert route.unsupported is None


@pytest.mark.parametrize("view", sorted(UNSHIPPED))
def test_every_unshipped_view_lands_somewhere_and_admits_it(view: str) -> None:
    """The render-conformance rule in code.

    An address the operator can type ALWAYS resolves to a lens that exists,
    and always says what it could not give them. Silently landing on Home
    without a word would be the failure mode this rule exists to prevent.
    """
    route = parse_route(view)
    assert route.unsupported == view, "the route must name what is missing"
    assert route.lens in LENSES, "it must land on a lens this build actually ships"


def test_a_deferred_candidate_address_still_carries_its_generation() -> None:
    """`/e/<epoch>/gen/v4` lands on the standings row it would have opened."""
    route = parse_route(f"/e/{EPOCH}/gen/v4")
    assert route.lens == "standings"
    assert route.params == {"epoch": EPOCH, "gen": "v4"}
    assert route.unsupported == "candidate"
    assert parse_route("candidate/v4").params == {"gen": "v4"}


def test_deferred_and_browser_only_are_different_promises() -> None:
    """One set is coming back; the other never will. Don't merge them."""
    assert not set(DEFERRED) & set(BROWSER_ONLY)
    assert "candidate" in DEFERRED and "builder" in BROWSER_ONLY


def test_route_round_trips_to_a_browser_path() -> None:
    for path in (f"/e/{EPOCH}/gens", f"/e/{EPOCH}/instrument/r1", "/"):
        assert parse_route(parse_route(path).to_path()).lens == parse_route(path).lens


def test_compare_suffix_params_are_ignored_not_misread() -> None:
    """The browser's ``~cmp=`` target has no terminal lens; the path still works."""
    route = parse_route(f"/e/{EPOCH}/gen/v4~cmp=v3")
    assert (route.lens, route.params) == ("standings", {"epoch": EPOCH, "gen": "v4"})


# ---------------------------------------------------------------------------
# The OUTER (seq) gate — a no-op beat must not cost a single request
# ---------------------------------------------------------------------------


def test_a_repeated_seq_costs_zero_fetches() -> None:
    """THE outer guard. The digest gate is the fallback, not the first line.

    Without this the console refetches the whole workspace on every file touch
    and only then decides not to repaint — correct on screen, wasteful on the
    wire, and exactly what the served ``seq`` cursor exists to prevent.
    """
    c = console(route=Route(lens="home", params={"epoch": EPOCH}))
    assert c.note_progress(4, False) is True
    c.refresh()
    before = len(c.client.requested)

    for _ in range(15):
        assert c.note_progress(4, False) is False
    assert len(c.client.requested) == before, "a repeated seq must not fetch"
    assert c.skipped_frames == 15
    assert c.repaints == 1


def test_an_advancing_seq_is_work() -> None:
    c = console(route=Route(lens="home", params={"epoch": EPOCH}))
    c.note_progress(4, False)
    assert c.note_progress(5, False) is True
    assert c.last_seq == 5
    assert c.skipped_frames == 0


def test_a_backwards_seq_forces_a_full_reapply() -> None:
    """A fresh evolve boot restarts seq at 1; the screen must follow the NEW run.

    Treating the low seq as "no progress" would freeze the console on the
    finished run for the entire length of the next one.
    """
    c = console(route=Route(lens="home", params={"epoch": EPOCH}))
    c.note_progress(90, True)
    c.refresh()
    assert c.note_progress(1, False) is True
    assert c.last_seq == 1, "the cursor must adopt the LOW value, not keep 90"
    assert c.view is None, "a rollover forces a full re-apply"
    assert c.terminal is False


def test_an_absent_seq_degrades_to_always_refresh() -> None:
    """A pre-RUNTIME-V2 server gives no cursor: never skip on a guess."""
    c = console(route=Route(lens="home", params={"epoch": EPOCH}))
    for value in (None, "3", float("nan")):
        assert c.note_progress(value, None) is True
    assert c.skipped_frames == 0


def test_terminal_rides_along_without_gating() -> None:
    """`terminal` distinguishes settled from stalled; it never skips a frame."""
    c = console(route=Route(lens="home", params={"epoch": EPOCH}))
    c.note_progress(3, False)
    assert c.terminal is False
    c.note_progress(4, True)
    assert c.terminal is True
    # A repeat still carries the flag even though the frame does no work.
    assert c.note_progress(4, False) is False
    assert c.terminal is False
