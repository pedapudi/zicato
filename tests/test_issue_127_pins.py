"""Triage pins for issue #127 — ``AttributeError`` conflated with a missing symbol.

``ADKProposerAgent._load_agent`` wraps ``getattr(module, "agent")`` in a bare
``except AttributeError`` and reports "has no 'agent' symbol"
(``src/zicato/proposer/adk_agent.py:322-326``). With a PEP-562 module-level
``__getattr__`` the attribute access IS the construction, so an
``AttributeError`` raised while BUILDING the agent — a renamed attribute in a
dependency, a wrong import path — is reported as an authoring mistake in the
proposer's own ``agent.py``. Both cases currently produce a byte-identical
message; only ``__cause__`` (which the operator never sees) tells them apart.

The same conflation recurs at four sibling loaders. ``zicato.import_path``
is the worst of them because it re-raises ``from None``, actively discarding
the original traceback:

* ``src/zicato/import_path.py:85`` — ``from None`` (traceback destroyed)
* ``src/zicato/adapters/adk.py:1908`` — harness entrypoint, chains ``from exc``
* ``src/zicato/synthetic/adversarial.py:126`` — chains ``from exc``
* ``src/zicato/judge_runtime/builder.py:369`` — chains ``from exc``

FIXED: all five sites now route their caught ``AttributeError`` through
``zicato.import_path.explain_attribute_error``, which distinguishes a
genuine absence from a failure raised inside the access itself, and
``import_path`` chains ``from exc``. Every test below is a live guard —
the ``xfail`` markers were removed when the fix landed.

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

from zicato.core.types import ProposerSpec
from zicato.proposer.adk_agent import ADKProposerAgent
from zicato.proposer.proposer import ProposerError

_SPEC = ProposerSpec(agent_id="issue127", tools=(), skills=(), agent_source_sha256=None)

#: A proposer dir whose ``agent`` symbol is provided LAZILY and whose
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

#: A proposer dir that genuinely forgot to define ``agent``.
_MISSING_AGENT_PY = "SOMETHING_ELSE = 1\n"


def _proposer_dir(tmp_path: Path, name: str, body: str) -> Path:
    d = tmp_path / name
    d.mkdir()
    (d / "agent.py").write_text(body, encoding="utf-8")
    return d


def _load_error(proposer_path: Path) -> ProposerError:
    agent = ADKProposerAgent(_SPEC, proposer_path=proposer_path)
    with pytest.raises(ProposerError) as excinfo:
        agent._load_agent(None)
    return excinfo.value


def test_lazy_construction_failure_is_not_blamed_on_a_missing_symbol(
    tmp_path: Path,
) -> None:
    """A lazily-built ``agent`` that fails to construct must say so.

    The message an operator reads must not assert the symbol is absent when
    the module provides it lazily and the CONSTRUCTION is what raised.
    """
    err = _load_error(_proposer_dir(tmp_path, "lazy", _LAZY_AGENT_PY))
    message = "\n".join(err.attempts)

    assert "has no 'agent' symbol" not in message
    # The underlying failure must be legible in what the operator is shown.
    assert "build_llm_agent" in message


def test_genuinely_missing_symbol_still_reports_a_missing_symbol(
    tmp_path: Path,
) -> None:
    """Regression guard: the true missing-symbol case keeps its message.

    Fixing the conflation must not blur the case the message was written for.
    """
    err = _load_error(_proposer_dir(tmp_path, "missing", _MISSING_AGENT_PY))
    message = "\n".join(err.attempts)

    assert "has no 'agent' symbol" in message


def test_both_cases_chain_the_original_attribute_error(tmp_path: Path) -> None:
    """Regression guard: ``__cause__`` is preserved on this loader today.

    Issue #127 claims the chaining is discarded; on THIS site it is not
    (``raise ... from exc``). The discarding site is ``import_path.py``,
    pinned separately below.
    """
    lazy = _load_error(_proposer_dir(tmp_path, "lazy2", _LAZY_AGENT_PY))
    missing = _load_error(_proposer_dir(tmp_path, "missing2", _MISSING_AGENT_PY))

    assert isinstance(lazy.__cause__, AttributeError)
    assert isinstance(missing.__cause__, AttributeError)


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
