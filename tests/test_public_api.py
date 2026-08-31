"""Tests for the ``zicato`` public-API facade.

Two properties are pinned:

1. Every declared facade name resolves, and resolves to the *same
   object* as its home module — the facade is a pure re-export layer,
   never a fork.
2. ``import zicato`` is cheap: the lazy ``__getattr__`` means the heavy
   library modules are NOT imported until a facade name is touched. The
   CLI's fast ``--help`` path depends on this.
"""

from __future__ import annotations

import importlib
import json
import subprocess
import sys

import pytest

import zicato


def test_every_facade_name_resolves_to_its_home_module() -> None:
    """Each export is identical to the attribute at its declared home."""
    for name, (module_name, attr) in zicato._EXPORTS.items():
        exported = getattr(zicato, name)
        home = importlib.import_module(module_name)
        expected = home if attr is None else getattr(home, attr)
        assert exported is expected, (
            f"zicato.{name} is not the object at {module_name}"
            f"{'' if attr is None else '.' + attr}"
        )


def test_all_lists_exactly_the_declared_surface() -> None:
    assert set(zicato.__all__) == {"__version__", *zicato._EXPORTS}
    # __dir__ surfaces every export for discoverability.
    assert set(zicato._EXPORTS) <= set(dir(zicato))


def test_root_surface_is_deliberately_small() -> None:
    assert set(zicato._EXPORTS) == {
        "CallLLM",
        "EvolveRoundOutcome",
        "HarnessAdapter",
        "RunnableHarness",
        "ScoringWeights",
        "ZicatoConfig",
        "evolve_n_rounds",
        "evolve_once",
        "load_board",
        "load_config",
        "load_workspace_config",
        "recommended_scaffold_weights",
    }


def test_the_loop_names_all_route_through_the_dispatch_surface() -> None:
    """One home for all three loop exports.

    :mod:`zicato.orchestrator` is the public dispatch surface over the round
    pipeline. A facade entry naming a module inside :mod:`zicato.evolve` would
    pin the module a function is defined in, which the pipeline is free to
    change; the dispatch surface is what the package root promises.
    """
    for name in ("EvolveRoundOutcome", "evolve_n_rounds", "evolve_once"):
        assert zicato._EXPORTS[name][0] == "zicato.orchestrator", name


def test_unknown_attribute_raises_attribute_error() -> None:
    with pytest.raises(AttributeError, match="no_such_name"):
        zicato.no_such_name  # noqa: B018


def test_import_zicato_does_not_pull_heavy_modules() -> None:
    """``import zicato`` must not import any zicato submodule.

    The fast ``--help`` path (a documented CLI property) rests on the
    root package staying free of eager imports; the facade is lazy on
    purpose. Run in a fresh interpreter so this test cannot be fooled
    by modules other tests already imported.
    """
    code = (
        "import json, sys\n"
        "import zicato\n"
        "loaded = sorted(m for m in sys.modules if m.startswith('zicato'))\n"
        "print(json.dumps(loaded))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )
    loaded = json.loads(out.stdout)
    assert loaded == ["zicato"], f"import zicato eagerly pulled: {loaded}"


def test_facade_access_imports_only_the_touched_slice() -> None:
    """Touching one light name must not drag in the orchestrator."""
    code = (
        "import json, sys\n"
        "import zicato\n"
        "_ = zicato.ScoringWeights\n"
        "heavy = [m for m in ('zicato.orchestrator', 'zicato.evolve.loop',"
        " 'zicato.dashboard', 'zicato.cli', 'zicato.builder')"
        " if m in sys.modules]\n"
        "print(json.dumps(heavy))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(out.stdout) == []
