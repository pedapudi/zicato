"""``AttributeError`` conflated with a missing symbol, at every loader.

A loader that wraps ``getattr(module, name)`` in a bare
``except AttributeError`` cannot tell two different failures apart. With
a PEP-562 module-level ``__getattr__`` the attribute access IS the
construction, so an ``AttributeError`` raised while BUILDING the object —
a renamed attribute in a dependency, a wrong import path — reads as the
author having forgotten to define the symbol at all. Without care both
produce a byte-identical message, and only ``__cause__``, which the
operator never sees, tells them apart. ``zicato.import_path`` was the
worst of them, re-raising ``from None`` and destroying the traceback.

Every loader now routes its caught ``AttributeError`` through
:func:`zicato.import_path.explain_attribute_error`, which distinguishes a
genuine absence from a failure raised inside the access itself, and
``import_path`` chains ``from exc``. The guards below hold that at the
dotted-path loader — the one the proposer seam, the runtime factory and
the adapter factory all resolve through — and at the adversarial and
judge-builder resolvers.

``adapters/adk.py`` has no direct guard here: reaching its ``getattr``
needs a snapshot-backed ``ADKHarnessAdapter.load``, which is far more
setup than the shared helper's own coverage justifies. It keeps raising
``AttributeError`` as its type so the subprocess worker's catch semantics
are unchanged.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

#: A module whose ``agent`` symbol is provided LAZILY and whose
#: construction raises ``AttributeError`` for a reason that has nothing to do
#: with the symbol being absent.
_LAZY_AGENT_PY = textwrap.dedent(
    """
    import types


    def __getattr__(name: str):
        if name == "agent":
            dependency = types.SimpleNamespace()
            # The dependency renamed this attribute — a real construction
            # failure, NOT a missing 'agent' symbol.
            return dependency.build_llm_agent()
        raise AttributeError(name)
    """
)

#: A module that genuinely forgot to define ``agent``.
_MISSING_AGENT_PY = "SOMETHING_ELSE = 1\n"


def test_import_dotted_path_preserves_the_attribute_error_cause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``import_dotted_path`` must chain the ``AttributeError`` it swallowed.

    It is the shared ``module:symbol`` resolver behind the subprocess worker's
    adapter reconstruction and the ``adapter.kind == "import"`` factory, so a
    construction failure there is reported as "has no attribute" with no
    traceback at all.
    """
    module_dir = tmp_path / "pkg"
    module_dir.mkdir()
    (module_dir / "lazy_mod.py").write_text(_LAZY_AGENT_PY, encoding="utf-8")
    monkeypatch.syspath_prepend(str(module_dir))

    from zicato.import_path import import_dotted_path

    with pytest.raises(ValueError) as excinfo:
        import_dotted_path("lazy_mod:agent", label="issue127")

    assert isinstance(excinfo.value.__cause__, AttributeError)
    assert "build_llm_agent" in str(excinfo.value)


def _lazy_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str) -> str:
    """Put a lazily-constructing module named ``name`` on ``sys.path``."""
    root = tmp_path / f"{name}_root"
    root.mkdir()
    (root / f"{name}.py").write_text(_LAZY_AGENT_PY, encoding="utf-8")
    (root / f"{name}_missing.py").write_text(_MISSING_AGENT_PY, encoding="utf-8")
    monkeypatch.syspath_prepend(str(root))
    return name


def test_adversarial_resolver_separates_construction_from_absence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``resolve_adversarial_agent`` must not blame a construction failure on absence."""
    from zicato.synthetic.adversarial import (
        AdversarialResolutionError,
        resolve_adversarial_agent,
    )

    name = _lazy_module(tmp_path, monkeypatch, "adv127")

    with pytest.raises(AdversarialResolutionError) as lazy:
        resolve_adversarial_agent(f"{name}:agent")
    assert "has no attribute 'agent'" not in str(lazy.value)
    assert "build_llm_agent" in str(lazy.value)

    with pytest.raises(AdversarialResolutionError) as missing:
        resolve_adversarial_agent(f"{name}_missing:agent")
    assert "has no attribute 'agent'" in str(missing.value)


def test_judge_builder_resolver_separates_construction_from_absence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``judge_runtime._resolve_dotted_path`` keeps the same distinction."""
    from zicato.judge_runtime.builder import _resolve_dotted_path

    name = _lazy_module(tmp_path, monkeypatch, "judge127")

    with pytest.raises(AttributeError) as lazy:
        _resolve_dotted_path(f"{name}:agent")
    assert "has no attribute 'agent'" not in str(lazy.value)
    assert "build_llm_agent" in str(lazy.value)

    with pytest.raises(AttributeError) as missing:
        _resolve_dotted_path(f"{name}_missing:agent")
    assert "has no attribute 'agent'" in str(missing.value)


def test_explain_attribute_error_passes_through_custom_getattr_prose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``__getattr__`` raising prose of its own must not have it overwritten.

    CPython stamps ``.name``/``.obj`` onto any ``AttributeError`` escaping
    ``__getattr__``, so those two signals alone would misread this as a
    plain absence and discard what the module was trying to say.
    """
    from zicato.import_path import explain_attribute_error

    root = tmp_path / "prose_root"
    root.mkdir()
    (root / "prose127.py").write_text(
        "def __getattr__(name):\n"
        "    raise AttributeError('the config backend moved to zicato.settings')\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(root))

    import prose127  # type: ignore[import-not-found]

    with pytest.raises(AttributeError) as excinfo:
        prose127.agent  # noqa: B018

    detail = explain_attribute_error(prose127, "agent", excinfo.value)
    assert detail is not None
    assert "the config backend moved to zicato.settings" in detail
