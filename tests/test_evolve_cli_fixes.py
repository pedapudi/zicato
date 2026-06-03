"""Regression tests for two rough edges in the evolve-centric CLI.

Fix 1 — contract-path divergence. ``zicato epoch new`` froze the
supplied board / brief / scoring into the epoch directory but never
published them to the conventional contract source location, so a
subsequent ``zicato evolve`` (which resolves the live contract via
:func:`zicato.epoch.contract.resolve_contract_inputs`) failed with
"board file ... is missing". These tests assert the explicit
``init → register → epoch new → evolve`` flow resolves the contract,
and that the epoch ``epoch new`` created is not spuriously rolled by
the first ``evolve``.

Fix 2 — dashboard port reporting. ``zicato evolve`` printed a guessed
dashboard URL that pointed at the watchdog supervisor (the two used to
share a default port; the dashboard walked ``+1`` when it found the
port taken). These tests assert the dashboard's actually-bound port is
read back from ``runtime/dashboard.json`` and that the supervisor and
dashboard defaults are distinct.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from click.testing import CliRunner

from zicato.cli.commands.epoch import new_cmd
from zicato.cli.commands.evolve import (
    _DASHBOARD_HOST,
    _read_dashboard_endpoint,
    _report_dashboard_url,
)
from zicato.cli.commands.init import init_cmd
from zicato.cli.commands.register import register_cmd
from zicato.dashboard.server import _publish_endpoint
from zicato.epoch.contract import compute_contract_hash, resolve_contract_inputs
from zicato.epoch.lifecycle import current_epoch_id, load_epoch
from zicato.runtime.paths import dashboard_endpoint_path

# ---------------------------------------------------------------------------
# Fix 1 — contract-path divergence
# ---------------------------------------------------------------------------


def _bootstrap_explicit_flow(
    tmp_path: Path, scoring_extra: dict | None = None
) -> tuple[Path, Path, Path, Path]:
    """Run ``init`` + ``register`` and stage operator contract files.

    The contract files are deliberately staged in a directory that is
    NOT the conventional location next to the workspace, so the test
    actually exercises ``epoch new`` publishing them to the canonical
    place. Returns ``(workspace, board, brief, scoring)``.

    ``scoring_extra`` is merged into the written ``scoring.json`` so a
    caller can exercise extra contract surface (e.g. a ``tournament``
    block) without duplicating the bootstrap.
    """
    workspace = tmp_path / ".zicato"

    # Operator's source files live somewhere unrelated to the workspace.
    sources = tmp_path / "operator_inputs"
    sources.mkdir()
    board = sources / "myboard.jsonl"
    brief = sources / "mybrief.md"
    scoring = sources / "myscoring.json"
    board.write_text(
        json.dumps(
            {
                "id": "entry_a",
                "kind": "single_turn",
                "wall_clock_budget_seconds": 60,
                "input": "hello",
            }
        )
        + "\n"
    )
    brief.write_text("# Brief\n- Be careful.\n")
    scoring_doc: dict = {"drift_weight": 1.0, "pass_weight": 1.0}
    if scoring_extra:
        scoring_doc.update(scoring_extra)
    scoring.write_text(json.dumps(scoring_doc))

    # The mutable source tree.
    agent = tmp_path / "agent"
    agent.mkdir()
    (agent / "agent.py").write_text('# zicato:mutable id="greeting"\nGREETING = "hello"\n')

    runner = CliRunner()
    init_res = runner.invoke(init_cmd, ["--workspace", str(workspace)])
    assert init_res.exit_code == 0, init_res.output
    reg_res = runner.invoke(
        register_cmd,
        [
            "--workspace",
            str(workspace),
            "--adk",
            "pkg.mod:agent",
            "--mutable-tree",
            str(agent),
        ],
    )
    assert reg_res.exit_code == 0, reg_res.output
    return workspace, board, brief, scoring


def test_epoch_new_publishes_contract_so_evolve_can_resolve_it(tmp_path: Path) -> None:
    """After ``epoch new`` the live contract resolves — no "missing" error.

    This is the core of Fix 1: ``init → register → epoch new`` then
    ``zicato evolve``'s contract resolution must find the board / brief
    / scoring instead of failing because they only exist inside the
    epoch directory.
    """
    workspace, board, brief, scoring = _bootstrap_explicit_flow(tmp_path)

    runner = CliRunner()
    new_res = runner.invoke(
        new_cmd,
        [
            "t1",
            "--workspace",
            str(workspace),
            "--board",
            str(board),
            "--brief",
            str(brief),
            "--scoring",
            str(scoring),
        ],
    )
    assert new_res.exit_code == 0, new_res.output

    # The live contract `zicato evolve` resolves must point at files
    # that actually exist (the bug: they only lived in epochs/{id}/).
    inputs = resolve_contract_inputs(workspace)
    assert inputs.board_path.exists(), f"board missing at {inputs.board_path}"
    assert inputs.brief_path.exists(), f"brief missing at {inputs.brief_path}"
    assert inputs.scoring_path.exists(), f"scoring missing at {inputs.scoring_path}"
    # The registered inner-harness identity still resolves too.
    assert inputs.entrypoint == "pkg.mod:agent"
    assert inputs.mutable_trees


def test_epoch_new_then_evolve_does_not_spuriously_roll_the_epoch(
    tmp_path: Path,
) -> None:
    """The epoch ``epoch new`` created survives the first ``evolve``.

    ``zicato evolve`` rolls the epoch when the live contract hash
    differs from the epoch's stored hash. ``epoch new`` must freeze the
    epoch with a hash derived from the *same* bytes (board / brief /
    scoring AND the registered entrypoint + mutable trees) that a later
    ``evolve`` derives from the live files — otherwise the very first
    ``evolve`` would needlessly roll.
    """
    workspace, board, brief, scoring = _bootstrap_explicit_flow(tmp_path)

    runner = CliRunner()
    new_res = runner.invoke(
        new_cmd,
        [
            "t1",
            "--workspace",
            str(workspace),
            "--board",
            str(board),
            "--brief",
            str(brief),
            "--scoring",
            str(scoring),
        ],
    )
    assert new_res.exit_code == 0, new_res.output

    epoch_id = current_epoch_id(workspace)
    assert epoch_id is not None
    stored_hash = load_epoch(workspace, epoch_id).contract_hash

    # The hash `evolve` would compute from the resolved live contract.
    live_hash = compute_contract_hash(resolve_contract_inputs(workspace))
    assert stored_hash == live_hash, (
        "epoch new froze a contract hash that differs from the live "
        "contract — evolve would spuriously roll the epoch"
    )


def test_epoch_new_with_tournament_block_does_not_spuriously_roll(
    tmp_path: Path,
) -> None:
    """A ``tournament`` block in scoring must not trigger an auto-roll.

    Regression for #6: ``epoch new`` loaded the operator's scoring with a
    field-by-field reader that dropped the ``tournament`` block, freezing
    the epoch under the gauntlet default. ``evolve`` re-derives the live
    contract through the shared scoring loader, which DOES honour the
    block — so the frozen hash (gauntlet) differed from the live hash
    (the real structure) and the very first ``evolve`` rolled the epoch.

    Both paths must now produce a byte-identical contract hash.
    """
    workspace, board, brief, scoring = _bootstrap_explicit_flow(
        tmp_path,
        scoring_extra={
            "tournament": {
                "structure": "racing",
                "params": {"rungs": [4, 2, 1], "eta": 3.0, "board_fraction": 0.5},
            }
        },
    )

    runner = CliRunner()
    new_res = runner.invoke(
        new_cmd,
        [
            "t1",
            "--workspace",
            str(workspace),
            "--board",
            str(board),
            "--brief",
            str(brief),
            "--scoring",
            str(scoring),
        ],
    )
    assert new_res.exit_code == 0, new_res.output

    epoch_id = current_epoch_id(workspace)
    assert epoch_id is not None
    stored_hash = load_epoch(workspace, epoch_id).contract_hash

    # The hash ``evolve`` would recompute from the resolved live contract.
    live_hash = compute_contract_hash(resolve_contract_inputs(workspace))
    assert stored_hash == live_hash, (
        "epoch new with a tournament block froze a contract hash that "
        "differs from the live contract — evolve would spuriously roll"
    )

    # And the frozen epoch must actually carry the tournament structure,
    # not the gauntlet default the old loader fell back to.
    assert load_epoch(workspace, epoch_id).scoring.tournament_structure.structure == "racing"

    # End-to-end: the evolve entry hook keeps the epoch (no roll).
    from zicato.orchestrator import ensure_epoch_for_contract

    async def _aux(_system: str, _user: str, _model: str) -> str:
        return "stub analysis"

    resolved = asyncio.run(ensure_epoch_for_contract(workspace, auto_epoch=True, aux_call_llm=_aux))
    assert resolved == epoch_id
    assert current_epoch_id(workspace) == epoch_id


def test_epoch_new_then_ensure_epoch_for_contract_keeps_the_epoch(
    tmp_path: Path,
) -> None:
    """``ensure_epoch_for_contract`` (the evolve entry hook) keeps the epoch.

    Exercises the actual code path ``zicato evolve`` runs before the
    loop: it must resolve the contract and return the epoch ``epoch
    new`` created, without rolling.
    """
    workspace, board, brief, scoring = _bootstrap_explicit_flow(tmp_path)

    runner = CliRunner()
    new_res = runner.invoke(
        new_cmd,
        [
            "t1",
            "--workspace",
            str(workspace),
            "--board",
            str(board),
            "--brief",
            str(brief),
            "--scoring",
            str(scoring),
        ],
    )
    assert new_res.exit_code == 0, new_res.output
    epoch_before = current_epoch_id(workspace)

    from zicato.orchestrator import ensure_epoch_for_contract

    async def _aux(_system: str, _user: str, _model: str) -> str:
        return "stub analysis"

    resolved = asyncio.run(ensure_epoch_for_contract(workspace, auto_epoch=True, aux_call_llm=_aux))
    assert resolved == epoch_before
    assert current_epoch_id(workspace) == epoch_before


def test_epoch_new_streamlined_flow_files_already_in_place(tmp_path: Path) -> None:
    """``epoch new`` is a no-op copy when the files already sit canonically.

    In the streamlined flow the operator edits the live contract files
    in place (next to the workspace). Passing those same paths to
    ``epoch new`` must not error — the publish step detects source ==
    target and skips the copy — and the contract still resolves.
    """
    workspace = tmp_path / ".zicato"
    # Contract files at the conventional location: next to the workspace.
    board = tmp_path / "board.jsonl"
    brief = tmp_path / "brief.md"
    scoring = tmp_path / "scoring.json"
    board.write_text(
        json.dumps(
            {
                "id": "entry_a",
                "kind": "single_turn",
                "wall_clock_budget_seconds": 60,
                "input": "hello",
            }
        )
        + "\n"
    )
    brief.write_text("# Brief\n- steer\n")
    scoring.write_text(json.dumps({"drift_weight": 1.0, "pass_weight": 1.0}))
    agent = tmp_path / "agent"
    agent.mkdir()
    (agent / "agent.py").write_text('# zicato:mutable id="g"\nG = "hi"\n')

    runner = CliRunner()
    assert runner.invoke(init_cmd, ["--workspace", str(workspace)]).exit_code == 0
    assert (
        runner.invoke(
            register_cmd,
            [
                "--workspace",
                str(workspace),
                "--adk",
                "pkg.mod:agent",
                "--mutable-tree",
                str(agent),
            ],
        ).exit_code
        == 0
    )
    new_res = runner.invoke(
        new_cmd,
        [
            "t1",
            "--workspace",
            str(workspace),
            "--board",
            str(board),
            "--brief",
            str(brief),
            "--scoring",
            str(scoring),
        ],
    )
    assert new_res.exit_code == 0, new_res.output
    inputs = resolve_contract_inputs(workspace)
    assert inputs.board_path.exists()
    assert inputs.brief_path.exists()
    assert inputs.scoring_path.exists()


# ---------------------------------------------------------------------------
# Fix 2 — dashboard port reporting
# ---------------------------------------------------------------------------


def test_supervisor_and_dashboard_have_distinct_default_ports() -> None:
    """The watchdog supervisor and dashboard default to different ports.

    A shared default is what made the dashboard walk ``+1`` and the
    reported URL point at the wrong server. The dashboard's Python
    default is 7892; the supervisor's Rust default must not equal it,
    and the two ``+1`` walk ranges must be disjoint.
    """
    # The dashboard's preferred port — assert the ``python -m
    # zicato.dashboard`` argparse default directly from its source.
    # Resolve through the installed package so the src/ layout move
    # does not break the path.
    import zicato.dashboard as _dashboard_pkg

    main_py = Path(_dashboard_pkg.__file__).resolve().parent / "__main__.py"
    main_py_text = main_py.read_text(encoding="utf-8")
    assert '"--port", type=int, default=7892' in main_py_text
    dashboard_default = 7892

    # The supervisor's default lives in the Rust CLI; assert it via the
    # source so the two defaults cannot silently re-converge. The crate
    # lives at crates/supervisor under the Cargo workspace.
    main_rs = Path(__file__).resolve().parents[1] / "crates" / "supervisor" / "src" / "main.rs"
    text = main_rs.read_text(encoding="utf-8")
    assert (
        "default_value_t = 7892" not in text
    ), "supervisor must not default to the dashboard's 7892 port"
    assert "default_value_t = 7920" in text, "supervisor default port changed unexpectedly"
    # Walk ranges (10 wide each) must not overlap: 7892..7902 vs 7920..7930.
    assert dashboard_default + 10 < 7920


def test_dashboard_publishes_and_evolve_reads_back_bound_port(tmp_path: Path) -> None:
    """The dashboard records its bound port; evolve reads it back.

    This is the readback contract Fix 2 relies on: the dashboard writes
    ``runtime/dashboard.json`` with the port it actually bound, and
    ``zicato evolve`` reads that file rather than guessing.
    """
    workspace = tmp_path / ".zicato"
    workspace.mkdir()

    # The dashboard walked to 7895 (preferred 7892 was taken).
    _publish_endpoint(workspace, "127.0.0.1", 7895)
    endpoint = dashboard_endpoint_path(workspace)
    assert endpoint.exists()
    payload = json.loads(endpoint.read_text())
    assert payload == {"host": "127.0.0.1", "port": 7895}

    host, port = _read_dashboard_endpoint(endpoint)
    assert host == "127.0.0.1"
    assert port == 7895


def test_read_dashboard_endpoint_tolerates_missing_or_partial_file(
    tmp_path: Path,
) -> None:
    """A missing / empty / mid-write endpoint file reads back as no-port."""
    endpoint = tmp_path / "dashboard.json"

    # Absent.
    host, port = _read_dashboard_endpoint(endpoint)
    assert host == _DASHBOARD_HOST
    assert port is None

    # Mid-write / unparseable.
    endpoint.write_text("{not valid json")
    host, port = _read_dashboard_endpoint(endpoint)
    assert port is None

    # Parseable but missing the port key.
    endpoint.write_text(json.dumps({"host": "127.0.0.1"}))
    host, port = _read_dashboard_endpoint(endpoint)
    assert port is None


def test_report_dashboard_url_prints_actual_bound_port(tmp_path: Path) -> None:
    """``_report_dashboard_url`` prints the port read back from disk.

    Drives the reporter against a live (fake) process and a
    pre-published endpoint file: it must print the real bound port,
    distinct from the preferred port it was given.
    """
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    # Dashboard bound 7893 even though 7892 was the preferred port.
    _publish_endpoint(workspace, "127.0.0.1", 7893)

    class _LiveProc:
        returncode = None

    captured: list[str] = []

    import click

    def _spy_echo(message: str = "", **_kwargs: object) -> None:
        captured.append(message)

    original_echo = click.echo
    click.echo = _spy_echo  # type: ignore[assignment]
    try:
        asyncio.run(
            _report_dashboard_url(
                workspace, preferred_port=7892, proc=_LiveProc(), timeout_seconds=2.0
            )
        )
    finally:
        click.echo = original_echo  # type: ignore[assignment]

    joined = "\n".join(captured)
    assert "Dashboard: http://127.0.0.1:7893" in joined
    # The preferred port must NOT be the one reported.
    assert "7892" not in joined


def test_report_dashboard_url_noop_when_dashboard_failed_to_spawn(
    tmp_path: Path,
) -> None:
    """When the dashboard process is ``None``, nothing is printed."""
    workspace = tmp_path / ".zicato"
    workspace.mkdir()

    captured: list[str] = []
    import click

    original_echo = click.echo
    click.echo = lambda message="", **_kw: captured.append(message)  # type: ignore[assignment,misc]
    try:
        asyncio.run(_report_dashboard_url(workspace, preferred_port=7892, proc=None))
    finally:
        click.echo = original_echo  # type: ignore[assignment]

    assert captured == []


def test_report_dashboard_url_falls_back_when_endpoint_never_appears(
    tmp_path: Path,
) -> None:
    """A timed-out readback still reports a best-guess URL, marked unconfirmed."""
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    # No endpoint file is ever written.

    class _LiveProc:
        returncode = None

    captured: list[str] = []
    import click

    original_echo = click.echo
    click.echo = lambda message="", **_kw: captured.append(message)  # type: ignore[assignment,misc]
    try:
        asyncio.run(
            _report_dashboard_url(
                workspace,
                preferred_port=7892,
                proc=_LiveProc(),
                timeout_seconds=0.3,
            )
        )
    finally:
        click.echo = original_echo  # type: ignore[assignment]

    joined = "\n".join(captured)
    assert "http://127.0.0.1:7892" in joined
    assert "unconfirmed" in joined
