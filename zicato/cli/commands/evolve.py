"""``zicato evolve`` — the single entry point to the self-improvement loop.

``evolve`` is the whole happy path past ``zicato init``. It is
self-orchestrating: the operator never runs ``register`` / ``propose``
/ ``tournament`` / ``reindex`` / ``epoch`` by hand — ``evolve`` drives
all of them internally.

Each invocation:

1. **Resolves the evaluation contract** via
   :func:`zicato.epoch.contract.resolve_contract_inputs` — the board,
   the proposer brief, the scoring config, and the registered
   inner-harness identity (entrypoint + mutable trees).
2. **Compares the contract hash to the current epoch.** On *any*
   change it closes the current epoch (writing its ``analysis.md``) and
   opens a fresh one carrying the new contract, before running. This is
   contract-hash auto-epoching; it is ON by default. ``--no-auto-epoch``
   makes a drifted contract a hard error instead; ``--epoch`` pins an
   explicit epoch and skips the check entirely.
3. **Runs the loop** for ``--rounds`` rounds. Each round proposes one
   experiment, applies it, runs the tournament, and either promotes or
   rejects the child generation.
4. **Launches the dashboard** and prints its URL. The dashboard service
   and the watchdog-only supervisor bind distinct default ports so they
   never contend; the reported URL is the dashboard's *actually-bound*
   port, read back from ``runtime/dashboard.json`` rather than assumed.

See :mod:`zicato.orchestrator` for the loop implementation.

Usage::

    zicato evolve --rounds 4 \\
        --harness-call-llm my_pkg.llms:harness_call_llm \\
        --auxiliary-call-llm my_pkg.llms:aux_call_llm

The ``--mode`` flag picks between full A/B tournaments and inline
fast-mode keep/discard. The default is ``full``; fast mode reads the
parent's cached aggregate from ``gen_score.json`` and requires that a
prior full-mode round wrote that file.

The two ``--*-call-llm`` options accept dotted import paths in either
``pkg.mod:attr`` or ``pkg.mod.attr`` form — the same convention the
runtime factory uses everywhere else in the tree.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import shutil
from pathlib import Path
from typing import Any

import click


def _resolve_supervisor_binary() -> Path | None:
    """Return the path to ``zicato-supervisor`` or ``None`` if unavailable.

    Resolution order:

    1. Environment override ``ZICATO_SUPERVISOR_BINARY`` (useful for tests
       that point at a sentinel script).
    2. The in-tree release build relative to this source file. This is
       the path produced by ``cargo build --release`` and is the default
       distribution mode for development checkouts.
    3. The system ``PATH`` (``zicato-supervisor`` installed globally).

    Returns ``None`` when nothing resolves — the caller prints a warning
    and proceeds without a dashboard.
    """
    import os  # noqa: PLC0415

    env_override = os.environ.get("ZICATO_SUPERVISOR_BINARY")
    if env_override:
        candidate = Path(env_override)
        if candidate.exists() and os.access(candidate, os.X_OK):
            return candidate

    # In-tree path: this file is at zicato/cli/commands/evolve.py; the
    # binary lives at <repo_root>/supervisor/target/release/zicato-supervisor.
    here = Path(__file__).resolve()
    in_tree = (
        here.parent.parent.parent.parent / "supervisor" / "target" / "release" / "zicato-supervisor"
    )
    if in_tree.exists() and os.access(in_tree, os.X_OK):
        return in_tree

    on_path = shutil.which("zicato-supervisor")
    if on_path:
        return Path(on_path)

    return None


async def _maybe_spawn_supervisor(
    workspace_root: Path,
    disabled: bool,
) -> asyncio.subprocess.Process | None:
    """Spawn the supervisor binary as a subprocess (or return ``None``).

    The supervisor is now a watchdog-only process: it is started with
    ``--no-dashboard`` so it runs the process-supervision loop and the
    always-on ``/statusz`` probe but does NOT serve the dashboard UI.
    The dashboard is served by the separate Python service spawned by
    :func:`_maybe_spawn_dashboard`.

    The binary's stdout/stderr are inherited from the parent so log
    output appears alongside ``zicato evolve``'s own messages. On
    failure-to-spawn the function still returns ``None`` and prints a
    warning — ``evolve`` continues without the watchdog rather than
    refusing to run.

    ``disabled`` mirrors ``--no-dashboard``: with the dashboard
    suppressed there is nothing for the watchdog to guard the lifecycle
    of, so the supervisor is not spawned either.
    """
    if disabled:
        return None
    binary = _resolve_supervisor_binary()
    if binary is None:
        click.echo(
            "warning: zicato-supervisor binary not found; watchdog disabled",
            err=True,
        )
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            str(binary),
            "--workspace",
            str(workspace_root),
            "--no-dashboard",
        )
    except (OSError, FileNotFoundError) as exc:
        click.echo(
            f"warning: failed to spawn zicato-supervisor ({exc}); watchdog disabled",
            err=True,
        )
        return None
    return proc


def _dashboard_spawn_argv(workspace_root: Path, host: str, port: int) -> list[str]:
    """Return the argv that launches the Python dashboard service.

    Spawned as ``python -m zicato.dashboard`` so the dashboard runs in
    its own isolated process — the same pattern as the supervisor
    subprocess, and the cleanest teardown story (kill the process, the
    HTTP server dies with it).

    The ``zicato.dashboard.__main__`` entry point is owned by a parallel
    workstream. It is expected to accept ``--workspace``, ``--host`` and
    ``--port`` and to call :func:`zicato.dashboard.server.run` with the
    bundled static directory resolved the same way
    :func:`zicato.cli.commands.dashboard.resolve_static_dir` resolves
    it. If that entry point is absent the spawn fails cleanly and
    ``evolve`` continues without a dashboard (see
    :func:`_maybe_spawn_dashboard`).
    """
    import sys  # noqa: PLC0415

    return [
        sys.executable,
        "-m",
        "zicato.dashboard",
        "--workspace",
        str(workspace_root),
        "--host",
        host,
        "--port",
        str(port),
    ]


#: Dashboard bind host. The operator views the dashboard from the same
#: host as the evolve loop, so loopback is correct — there is no
#: ``--dashboard-bind`` flag.
_DASHBOARD_HOST = "127.0.0.1"


def _dashboard_endpoint_file(workspace_root: Path) -> Path:
    """Path the dashboard service writes its actually-bound host/port to."""
    from zicato.runtime.paths import dashboard_endpoint_path  # noqa: PLC0415

    return dashboard_endpoint_path(workspace_root)


async def _maybe_spawn_dashboard(
    workspace_root: Path,
    port: int,
    disabled: bool,
) -> asyncio.subprocess.Process | None:
    """Spawn the Python dashboard service as a subprocess (or ``None``).

    This is the process that actually serves the dashboard UI — the
    primary dashboard link ``evolve`` reports to the operator. It binds
    ``127.0.0.1`` because the operator views it from the same host as
    the evolve loop.

    ``port`` is the *preferred* port; the dashboard walks ``+1`` from it
    if it is taken, so the port it ends up serving on is read back from
    ``runtime/dashboard.json`` (see :func:`_report_dashboard_url`). Any
    stale endpoint file from a previous run is removed here, before the
    spawn, so the readback cannot observe a leftover port.

    On failure-to-spawn the function returns ``None`` and prints a
    warning — ``evolve`` continues without a dashboard rather than
    refusing to run, exactly like the supervisor helper. The dashboard's
    URL is NOT printed here; :func:`_report_dashboard_url` prints it once
    the real bound port is known.
    """
    if disabled:
        return None
    # Drop a stale endpoint file so the post-spawn readback only ever
    # sees this run's dashboard.
    endpoint_file = _dashboard_endpoint_file(workspace_root)
    try:
        endpoint_file.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass
    argv = _dashboard_spawn_argv(workspace_root, _DASHBOARD_HOST, port)
    try:
        proc = await asyncio.create_subprocess_exec(*argv)
    except (OSError, FileNotFoundError) as exc:
        click.echo(
            f"warning: failed to spawn the dashboard service ({exc}); dashboard disabled",
            err=True,
        )
        return None
    return proc


async def _report_dashboard_url(
    workspace_root: Path,
    preferred_port: int,
    proc: asyncio.subprocess.Process | None,
    *,
    timeout_seconds: float = 10.0,
) -> None:
    """Print the dashboard's real URL, reading the bound port back.

    The dashboard service writes the host/port it actually bound to
    ``runtime/dashboard.json`` once its listener is up. evolve cannot
    know that port up front — the dashboard walks ``+1`` when the
    preferred port is taken — so this polls for that file and reports
    the URL it names.

    If ``proc`` is ``None`` (the dashboard failed to spawn) nothing is
    printed. If the endpoint file never appears within ``timeout_seconds``
    (a slow or wedged start) we fall back to the preferred port with a
    note that it is unconfirmed, so the operator still gets a best-guess
    link rather than silence.
    """
    if proc is None:
        return
    endpoint_file = _dashboard_endpoint_file(workspace_root)
    deadline = asyncio.get_event_loop().time() + timeout_seconds
    while asyncio.get_event_loop().time() < deadline:
        # If the dashboard process died, stop waiting — no URL to report.
        if proc.returncode is not None:
            click.echo(
                "warning: the dashboard service exited before binding a port",
                err=True,
            )
            return
        host, bound = _read_dashboard_endpoint(endpoint_file)
        if bound is not None:
            click.echo(f"Dashboard: http://{host}:{bound}")
            return
        await asyncio.sleep(0.1)
    # Timed out waiting for the endpoint file. Report a best-guess URL
    # rather than nothing, but make the uncertainty explicit.
    click.echo(
        f"Dashboard: http://{_DASHBOARD_HOST}:{preferred_port} "
        "(port unconfirmed — the dashboard did not report its bound port in time)"
    )


def _read_dashboard_endpoint(endpoint_file: Path) -> tuple[str, int | None]:
    """Read ``runtime/dashboard.json``; return ``(host, port-or-None)``.

    Returns ``(_DASHBOARD_HOST, None)`` when the file is absent, empty,
    mid-write (unparseable), or missing a port — every one of which the
    caller treats as "not ready yet" and keeps polling.
    """
    try:
        raw = endpoint_file.read_text(encoding="utf-8")
    except OSError:
        return _DASHBOARD_HOST, None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return _DASHBOARD_HOST, None
    if not isinstance(payload, dict):
        return _DASHBOARD_HOST, None
    port = payload.get("port")
    host = payload.get("host") or _DASHBOARD_HOST
    if not isinstance(port, int):
        return str(host), None
    return str(host), port


async def _terminate_child(proc: asyncio.subprocess.Process | None) -> None:
    """Shut down a previously-spawned child process; idempotent.

    Used to tear down both the watchdog supervisor and the Python
    dashboard service. Sends ``SIGTERM``, waits up to five seconds for a
    clean exit, then escalates to ``SIGKILL``.
    """
    if proc is None:
        return
    if proc.returncode is not None:
        return
    try:
        proc.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            return
        await proc.wait()


# Backwards-compatible alias: the supervisor-only teardown name is kept
# so existing callers / tests that import it keep working.
_terminate_supervisor = _terminate_child


def _import_callable(dotted: str, *, kind: str) -> Any:
    """Resolve ``pkg.mod:attr`` or ``pkg.mod.attr`` to a callable.

    Mirrors :func:`zicato.runtime_factory._import_callable`. Duplicated
    here so this CLI module imports stay small (the runtime factory is
    imported by the orchestrator anyway).
    """
    if ":" in dotted:
        module_path, _, attr = dotted.partition(":")
    else:
        module_path, _, attr = dotted.rpartition(".")
    if not module_path or not attr:
        raise click.BadParameter(
            f"{kind} dotted path {dotted!r} must be 'pkg.module.attr' or 'pkg.module:attr'"
        )
    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:
        raise click.BadParameter(f"{kind}: could not import module {module_path!r}: {exc}") from exc
    if not hasattr(module, attr):
        raise click.BadParameter(f"{kind}: module {module_path!r} has no attribute {attr!r}")
    fn = getattr(module, attr)
    if not callable(fn):
        raise click.BadParameter(
            f"{kind}: {dotted!r} resolved to {type(fn).__name__}, expected a callable"
        )
    return fn


@click.command(
    name="evolve",
    short_help="Resolve the contract, auto-open an epoch on any change, and run the loop.",
    epilog=(
        "\b\n"
        "Happy-path invocation:\n"
        "  zicato evolve \\\n"
        "      --harness-call-llm  my_pkg.llms:harness \\\n"
        "      --auxiliary-call-llm my_pkg.llms:aux \\\n"
        "      --rounds 4\n"
        "\n"
        "\b\n"
        "Auto-epoching:\n"
        "  By default evolve rolls the epoch whenever the evaluation\n"
        "  contract (board + proposer brief + scoring + inner-harness\n"
        "  identity) has drifted. Pass --no-auto-epoch to error on drift\n"
        "  instead, or --epoch ID to pin an epoch and skip the check.\n"
    ),
)
@click.option(
    "--workspace",
    default=".zicato",
    show_default=True,
    type=click.Path(),
    help="Path to the zicato workspace root (the directory `zicato init` made).",
)
@click.option(
    "--epoch",
    default=None,
    help="Epoch id. Defaults to the workspace's current epoch.",
)
@click.option(
    "--rounds",
    default=1,
    show_default=True,
    type=click.IntRange(min=1),
    help="Number of evolve rounds to attempt.",
)
@click.option(
    "--mode",
    type=click.Choice(["full", "fast"]),
    default="full",
    show_default=True,
    help="full = run both parent + child; fast = child vs cached parent aggregate.",
)
@click.option(
    "--harness-call-llm",
    "harness_dotted",
    required=True,
    help="Dotted import path of the harness call_llm (e.g. mymodule:harness).",
)
@click.option(
    "--auxiliary-call-llm",
    "auxiliary_dotted",
    required=True,
    help="Dotted import path of the auxiliary call_llm (e.g. mymodule:aux).",
)
@click.option(
    "--max-consecutive-rejections",
    default=3,
    show_default=True,
    type=click.IntRange(min=1),
    help="Stop early when this many rounds in a row are rejected.",
)
@click.option(
    "--max-wall-clock-seconds",
    default=None,
    type=click.IntRange(min=1),
    envvar="ZICATO_MAX_WALL_CLOCK_SECONDS",
    help=(
        "Total wall-clock budget for this whole evolve invocation, in "
        "seconds. The loop stops cleanly between rounds once the budget "
        "is spent, and a single round that would overrun it is cancelled "
        "and recorded as aborted. Unset (the default) leaves the loop "
        "unbounded. Applies on top of each board entry's own "
        "wall_clock_budget_seconds. Env var: ZICATO_MAX_WALL_CLOCK_SECONDS."
    ),
)
@click.option(
    "--no-auto-epoch",
    is_flag=True,
    default=False,
    help=(
        "Disable contract-hash auto-epoching. With this flag, evolve "
        "errors out (instead of rolling the epoch) when the evaluation "
        "contract has drifted from the current epoch."
    ),
)
@click.option(
    "--epoch-name",
    default=None,
    help=(
        "Name for an auto-created epoch (default: the e{N} scheme). "
        "Ignored when --epoch is passed or no new epoch is created."
    ),
)
@click.option(
    "--no-dashboard",
    is_flag=True,
    default=False,
    help=(
        "Do not spawn the dashboard service (and the watchdog "
        "supervisor that guards it). evolve still runs the loop."
    ),
)
@click.option(
    "--dashboard-port",
    default=7892,
    show_default=True,
    type=click.IntRange(min=1, max=65535),
    help="Port for the dashboard HTTP server (bound on 127.0.0.1).",
)
def evolve_cmd(
    workspace: str,
    epoch: str | None,
    rounds: int,
    mode: str,
    harness_dotted: str,
    auxiliary_dotted: str,
    max_consecutive_rejections: int,
    max_wall_clock_seconds: int | None,
    no_auto_epoch: bool,
    epoch_name: str | None,
    no_dashboard: bool,
    dashboard_port: int,
) -> None:
    """Run the self-improvement loop — the single happy-path entry point.

    `evolve` is self-orchestrating: it resolves the evaluation
    contract, auto-opens an epoch when that contract has changed, then
    proposes / runs the tournament / promotes for --rounds rounds. You
    do not run `register`, `propose`, `tournament`, `reindex`, or
    `epoch` by hand — evolve drives them.

    By default, contract-hash auto-epoching is ON: when the evaluation
    contract (board / proposer brief / scoring / inner-harness
    identity) has drifted, evolve closes the current epoch and opens a
    fresh one before running. Pass --no-auto-epoch for the strict
    behaviour (error on drift instead of rolling). --epoch skips
    auto-epoching entirely and pins an explicit epoch.

    The dashboard is launched automatically and its URL is printed.
    """
    workspace_root = Path(workspace).resolve()

    harness_call_llm = _import_callable(harness_dotted, kind="harness_call_llm")
    auxiliary_call_llm = _import_callable(auxiliary_dotted, kind="auxiliary_call_llm")

    # Lazy import — the orchestrator is heavy. We keep it out of
    # `zicato --help` time.
    from zicato.orchestrator import evolve_n_rounds  # noqa: PLC0415

    # ``evolve_n_rounds`` appends a single symbolic terminal-reason
    # string here so the summary below can name exactly why the loop
    # ended.
    stop_reason_out: list[str] = []

    async def _run() -> list[Any]:
        # The supervisor is now watchdog-only (spawned with
        # --no-dashboard); the dashboard UI is served by the separate
        # Python dashboard service. Both are children of this evolve
        # process and both are torn down on exit.
        #
        # The two bind distinct default ports (the watchdog supervisor
        # on its own default, the dashboard on --dashboard-port) so
        # neither walks onto the other's port. The dashboard's URL is
        # reported only after reading the port it actually bound back
        # from runtime/dashboard.json — never assumed.
        sup = await _maybe_spawn_supervisor(
            workspace_root,
            disabled=no_dashboard,
        )
        dash = await _maybe_spawn_dashboard(
            workspace_root,
            dashboard_port,
            disabled=no_dashboard,
        )
        await _report_dashboard_url(workspace_root, dashboard_port, dash)
        try:
            return await evolve_n_rounds(
                rounds=rounds,
                workspace_root=workspace_root,
                epoch_id=epoch,
                harness_call_llm=harness_call_llm,
                auxiliary_call_llm=auxiliary_call_llm,
                fast_mode=(mode == "fast"),
                max_consecutive_rejections=max_consecutive_rejections,
                max_wall_clock_seconds=max_wall_clock_seconds,
                auto_epoch=not no_auto_epoch,
                epoch_name=epoch_name,
                stop_reason_out=stop_reason_out,
            )
        finally:
            # Tear down both children. Tear the dashboard down first so
            # its port is freed before the watchdog notices it is gone.
            await _terminate_child(dash)
            await _terminate_child(sup)

    try:
        outcomes = asyncio.run(_run())
    except (FileNotFoundError, RuntimeError) as exc:
        # FileNotFoundError: missing config / epoch marker.
        # RuntimeError: contract drift under --no-auto-epoch, or a
        # missing baseline. Both are operator-actionable; surface them
        # as a clean CLI error rather than a traceback.
        raise click.ClickException(str(exc)) from exc

    # Final summary line — say explicitly why the loop ended. The
    # total wall-clock budget stop is called out distinctly from "all
    # rounds done" and from the consecutive-reject early-stop.
    stop_reason = stop_reason_out[0] if stop_reason_out else "completed"
    ran = len(outcomes)
    if stop_reason == "wall_clock_budget_between_rounds":
        click.echo(
            f"evolve: stopped on the total wall-clock budget of "
            f"{max_wall_clock_seconds}s — ran {ran} of {rounds} requested "
            f"rounds before the budget was spent.",
            err=True,
        )
    elif stop_reason == "wall_clock_budget_mid_round":
        click.echo(
            f"evolve: stopped on the total wall-clock budget of "
            f"{max_wall_clock_seconds}s — round {ran} was cancelled "
            f"mid-flight (recorded as aborted) because finishing it would "
            f"have overrun the budget; ran {ran} of {rounds} requested rounds.",
            err=True,
        )
    elif stop_reason == "consecutive_rejections":
        click.echo(
            f"evolve: stopped early after {max_consecutive_rejections} "
            f"consecutive rejections — ran {ran} of {rounds} requested rounds.",
            err=True,
        )
    elif stop_reason == "degenerate_health":
        click.echo(
            f"evolve: stopped early on a degenerate loop-health finding — "
            f"ran {ran} of {rounds} requested rounds.",
            err=True,
        )
    else:
        click.echo(f"evolve: completed all {ran} requested rounds.", err=True)

    payload = [
        {
            "parent_generation_id": o.parent_generation_id,
            "proposed_generation_id": o.proposed_generation_id,
            "tournament_decision": o.tournament_decision,
            "rejection_reason": o.rejection_reason,
            "parent_scalar": o.parent_scalar,
            "child_scalar": o.child_scalar,
            "delta_scalar": o.delta_scalar,
        }
        for o in outcomes
    ]
    click.echo(json.dumps(payload, indent=2, sort_keys=True))


__all__ = ["evolve_cmd"]
