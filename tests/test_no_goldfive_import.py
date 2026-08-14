"""Proof that zicato's core import surface does not need goldfive.

goldfive is an optional extra (``pip install zicato[goldfive]``). Nothing
on the path from ``import zicato`` through board load/save and
``zicato --help`` may reach ``import goldfive``.

A minimal venv would not prove that: the development environment installs all
extras, including dependencies that resolve goldfive transitively. Each test
therefore runs a child interpreter
whose ``sys.meta_path`` refuses ``goldfive``, installed before zicato is
imported — a module-scope ``from goldfive import ...`` added to a core
module turns these red with the offending traceback.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import tomllib
from pathlib import Path

import pytest

from zicato.core import drift_kinds

REPO_ROOT = Path(__file__).resolve().parents[1]

# Installed into the child's sys.meta_path (position 0) before zicato is
# imported. `find_spec` raising ImportError propagates out of the import
# statement exactly as a missing distribution would, and blocking
# "goldfive.*" too keeps a submodule import from sneaking past.
_BLOCKER = textwrap.dedent(
    """
    import sys

    class _BlockGoldfive:
        def find_spec(self, fullname, path=None, target=None):
            if fullname == "goldfive" or fullname.startswith("goldfive."):
                raise ImportError(f"goldfive is blocked in this interpreter ({fullname})")
            return None

    sys.meta_path.insert(0, _BlockGoldfive())
    for name in [m for m in sys.modules if m == "goldfive" or m.startswith("goldfive.")]:
        del sys.modules[name]
    """
)


def _run_without_goldfive(body: str) -> subprocess.CompletedProcess[str]:
    """Run *body* in a child interpreter that cannot import goldfive."""
    return subprocess.run(
        [sys.executable, "-c", _BLOCKER + textwrap.dedent(body)],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=120,
    )


def _ok(result: subprocess.CompletedProcess[str]) -> str:
    """Assert the child exited 0, surfacing its stderr on failure."""
    assert result.returncode == 0, (
        f"child interpreter failed without goldfive (exit {result.returncode})\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    return result.stdout


def test_blocker_actually_blocks() -> None:
    """The harness is only evidence if the blocker really blocks."""
    result = _run_without_goldfive(
        """
        try:
            import goldfive
        except ImportError as exc:
            print("blocked:", exc)
        else:
            raise AssertionError("goldfive imported despite the blocker")
        """
    )
    assert "blocked:" in _ok(result)


def test_core_modules_import_without_goldfive() -> None:
    """Every module on the core import surface resolves goldfive-free."""
    _ok(
        _run_without_goldfive(
            """
            import importlib, sys

            for name in [
                "zicato", "zicato.core", "zicato.board", "zicato.board.jsonl",
                "zicato.board.judges", "zicato.board.builder", "zicato.epoch",
                "zicato.storage", "zicato.workspace_loader", "zicato.cli",
            ]:
                importlib.import_module(name)
                assert "goldfive" not in sys.modules, f"{name} pulled in goldfive"
            """
        )
    )


def test_board_roundtrip_without_goldfive(tmp_path: Path) -> None:
    """A board builds, saves, loads, and re-saves byte-identically without goldfive.

    Covers the authoring surface an operator touches for a target that
    does not run under goldfive: ``Board`` / ``Entry`` / ``Predicate`` /
    ``Judge``, a board-level ``disable_drift`` header, and the JSONL
    reader/writer.
    """
    board = tmp_path / "board.jsonl"
    out = tmp_path / "roundtrip.jsonl"
    _ok(
        _run_without_goldfive(
            f"""
            from pathlib import Path
            from zicato.board import Board, Entry, Judge, Predicate
            from zicato.board.jsonl import load_board_with_meta, save_board
            from zicato.core import DriftKind, DriftSeverity

            b = Board(disable_drift=(DriftKind.OFF_TOPIC, DriftKind.TOOL_ERROR))
            b.add(
                Entry(
                    id="e1",
                    input="hello",
                    evaluate=Predicate.contains("hello"),
                    judges=[
                        Judge.custom("on_task", "stays on task", severity=DriftSeverity.WARNING)
                    ],
                )
            )
            b.save(Path({str(board)!r}))

            entries, disable_drift, judge_only = load_board_with_meta(Path({str(board)!r}))
            assert [e.id for e in entries] == ["e1"], entries
            assert entries[0].judges[0].severity == "warning", entries[0].judges
            assert [k.value for k in disable_drift] == ["off_topic", "tool_error"], disable_drift
            assert judge_only is False
            save_board(entries, Path({str(out)!r}), disable_drift=disable_drift)
            """
        )
    )
    assert out.read_text(encoding="utf-8") == board.read_text(encoding="utf-8")


def test_contract_hash_of_a_disable_drift_board_without_goldfive(tmp_path: Path) -> None:
    """Canonicalizing a board that names drift kinds must not reach goldfive.

    ``_canon_disable_drift`` normalizes each token through
    ``judge_runtime.disable.kind_to_wire_string`` — a module that *does*
    talk to goldfive, but only lazily and under ``TYPE_CHECKING``. The
    epoch contract is on the core import surface, so the normalizer must
    stay on the goldfive-free side of that line.
    """
    board = tmp_path / "board.jsonl"
    board.write_text(
        json.dumps({"board_meta": True, "disable_drift": ["tool_error", "agent_refusal"]})
        + "\n"
        + json.dumps(
            {"id": "e1", "kind": "single_turn", "wall_clock_budget_seconds": 60, "input": "hi"}
        )
        + "\n",
        encoding="utf-8",
    )
    stdout = _ok(
        _run_without_goldfive(
            f"""
            import sys
            from pathlib import Path
            from zicato.epoch.contract import _canon_board_meta

            canon = _canon_board_meta(Path({str(board)!r}))
            assert '"disable_drift": ["agent_refusal", "tool_error"]' in canon, canon
            assert "goldfive" not in sys.modules, "goldfive leaked into sys.modules"
            print("contract-canon-ok")
            """
        )
    )
    assert "contract-canon-ok" in stdout


def test_drift_vocabulary_available_without_goldfive() -> None:
    """The mirror keeps the FULL vocabulary — unknown tokens still raise."""
    stdout = _ok(
        _run_without_goldfive(
            """
            from zicato.core.drift_kinds import (
                GOLDFIVE_DRIFT_KINDS, DriftKind, DriftSeverity, is_drift_severity,
            )

            assert DriftKind("off_topic") is DriftKind.OFF_TOPIC
            assert DriftSeverity("critical") is DriftSeverity.CRITICAL
            assert is_drift_severity(DriftSeverity.WARNING)
            assert not is_drift_severity("warning")

            from zicato.board.jsonl import _coerce_disable_drift
            try:
                _coerce_disable_drift(["not_a_drift_kind"], "board.jsonl: line 1")
            except ValueError as exc:
                assert "unknown drift kind 'not_a_drift_kind'" in str(exc), exc
            else:
                raise AssertionError("unknown drift kind was accepted")

            print(len(GOLDFIVE_DRIFT_KINDS))
            """
        )
    )
    assert stdout.strip() == "41"


def test_cli_help_without_goldfive() -> None:
    """``zicato --help`` renders every subcommand with goldfive blocked.

    ``build_cli_root`` skips a command module that fails to import,
    logging a warning, so a root ``--help`` alone would still render with
    half the subcommands missing. Hence the command-count floor and the
    per-subcommand help.
    """
    stdout = _ok(
        _run_without_goldfive(
            """
            import click
            from zicato.cli.discovery import build_cli_root

            root = build_cli_root()
            ctx = click.Context(root, info_name="zicato", terminal_width=80)
            print(root.get_help(ctx))

            names = sorted(root.commands)
            assert len(names) >= 5, names
            for name in names:
                cmd = root.commands[name]
                sub = click.Context(cmd, parent=ctx, info_name=name, terminal_width=80)
                assert cmd.get_help(sub), name
            """
        )
    )
    assert "Usage:" in stdout


def test_goldfive_is_declared_as_an_extra() -> None:
    """The packaging half: the import proof alone would not catch a re-add."""
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    names = [d.split("[")[0].strip() for d in pyproject["project"]["dependencies"]]
    extras = pyproject["project"]["optional-dependencies"]

    assert "goldfive" not in names, "goldfive belongs in the `goldfive` extra"
    assert "goldfive" in extras["goldfive"]
    # The ADK adapter path is where goldfive is load-bearing.
    assert any(d.split("[")[0].strip() == "goldfive" for d in extras["adk"]), extras["adk"]


def test_base_install_excludes_observability_dependencies() -> None:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = pyproject["project"]
    base = {d.split("[")[0].strip() for d in project["dependencies"]}
    extras = project["optional-dependencies"]
    telemetry = {"harmonograf-client", "harmonograf-server"}

    assert base.isdisjoint(telemetry)
    assert telemetry <= {d.split("[")[0].strip() for d in extras["observability"]}
    assert telemetry <= {d.split("[")[0].strip() for d in extras["all"]}


def test_mirror_matches_goldfive() -> None:
    """The mirror must not skew from upstream: same names, values, ORDER.

    Order is observable — ``_coerce_disable_drift`` and ``_coerce_enum``
    render ``valid values are: ...`` by iterating the enum — so this pins
    the sequence, not just the set.
    """
    goldfive = pytest.importorskip("goldfive", reason="goldfive extra not installed")

    for name in ("DriftKind", "DriftSeverity"):
        upstream = getattr(goldfive, name)
        mine = getattr(drift_kinds, name)
        assert [(m.name, m.value) for m in mine] == [(m.name, m.value) for m in upstream], name
