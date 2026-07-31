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

Pins marked ``xfail(strict=True)`` must fail today; the unmarked pin is a
regression guard for behaviour that is already correct and must stay so.
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


@pytest.mark.xfail(
    strict=True,
    reason="issue #127: adk_agent.py:324 reports a construction-time "
    "AttributeError as 'has no agent symbol', pointing debugging at the wrong file",
)
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


@pytest.mark.xfail(
    strict=True,
    reason="issue #127 sibling: import_path.py:85 re-raises 'from None', "
    "destroying the traceback of a construction-time AttributeError",
)
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
