"""Resolve authored proposal settings and bind the invocation's workspace."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zicato.core.configuration import authored_dataclass_from_json, dataclass_to_jsonable
from zicato.core.proposer_config import VIEWER_POLICIES as VIEWER_POLICIES
from zicato.core.proposer_config import FoeBudget as FoeBudget
from zicato.core.proposer_config import FoeModelRole as FoeModelRole
from zicato.core.proposer_config import ProposerDeclaration
from zicato.proposer.external import DEFAULT_PROPOSER_AGENT, UNSET_BINARY

#: The ``config.json`` key holding everything below.
PROPOSER_BLOCK_KEY = "proposer"

#: Where the removed proposer runtimes were configured, and what each
#: message tells the operator to do instead. Read by
#: :func:`refuse_removed_proposer_configuration`, which is the single
#: place a retired configuration is named.
REMOVED_RUNTIME_KEYS: Mapping[str, str] = {
    "pi_bin": "the coding-agent proposer integration it configured was removed",
    "pi_integration_dir": "the coding-agent proposer integration it configured was removed",
}

#: The module namespace zicato's own proposer classes live in. A dotted
#: path into it that is not the Foe agent names a built-in runtime that
#: was removed, and is refused. An operator's own class lives outside this
#: namespace and is untouched, which is the whole distinction the retained
#: seam draws: it accepts an explicit class, never a removed built-in.
BUILT_IN_PROPOSER_NAMESPACE = "zicato.proposer."

#: Named in every refusal so an operator has one place to read.
_REPLACEMENT = (
    "zicato's only proposer runtime is Foe; declare it in the `proposer` "
    "block of the workspace config (see docs/design/PROPOSER.md)"
)


class ProposerConfigError(ValueError):
    """A workspace's proposer configuration cannot be used as written."""


@dataclass(frozen=True, slots=True)
class FoeProposerConfig(ProposerDeclaration):
    """Authored proposal settings bound to the workspace owning their artifacts."""

    workspace_root: Path | None = None


def scaffold_proposer_block() -> dict[str, Any]:
    """The ``proposer`` block a freshly initialized workspace carries.

    Complete but not yet runnable: every decision is spelled out with the
    documented default so an operator edits rather than researches, and
    :data:`UNSET_BINARY` marks the one field only they can fill.
    """
    block = dataclass_to_jsonable(
        ProposerDeclaration(
            binary=Path(UNSET_BINARY),
            model=FoeModelRole(provider="<model backend>", model="<model name>"),
        )
    )
    block.pop("_guide")
    return block


def load_foe_proposer_config(
    workspace_config: Mapping[str, Any],
    workspace_root: Path | None = None,
) -> FoeProposerConfig:
    """Read and validate the workspace's ``proposer`` block.

    Raises :class:`ProposerConfigError` naming the key and the rule for
    every shape the block can get wrong, including the retired keys
    :func:`refuse_removed_proposer_configuration` recognizes, so a
    workspace that cannot run a proposal episode says so before a round
    opens rather than at the first propose.
    """
    refuse_removed_proposer_configuration(workspace_config)
    block = workspace_config.get(PROPOSER_BLOCK_KEY)
    if not isinstance(block, Mapping):
        raise ProposerConfigError(
            "proposer: the workspace declares no `proposer` block, and " f"{_REPLACEMENT}"
        )
    try:
        declared = authored_dataclass_from_json(ProposerDeclaration, block, path="proposer")
    except ValueError as exc:
        raise ProposerConfigError(str(exc)) from exc
    return FoeProposerConfig(
        binary=declared.binary,
        model=declared.model,
        budget=declared.budget,
        viewer=declared.viewer,
        guide=declared.guide,
        workspace_root=workspace_root,
    )


def refuse_removed_proposer_configuration(workspace_config: Mapping[str, Any]) -> None:
    """Refuse a workspace still configured for a retired proposer runtime.

    The coding-agent, ADK and native proposer implementations were removed with
    Foe's adoption. A workspace carrying their configuration would
    otherwise run Foe while its file still described something else, so
    each retired key is refused by name with what replaced it. An operator
    class bound through ``runtime.proposer_agent`` is untouched: the seam
    accepts an explicit class, never a removed built-in.
    """
    runtime = workspace_config.get("runtime")
    if not isinstance(runtime, Mapping):
        return
    for key, removal in REMOVED_RUNTIME_KEYS.items():
        if runtime.get(key):
            raise ProposerConfigError(f"runtime.{key}: {removal}, and {_REPLACEMENT}")
    dotted = str(runtime.get("proposer_agent") or "")
    if dotted.startswith(BUILT_IN_PROPOSER_NAMESPACE) and dotted != DEFAULT_PROPOSER_AGENT:
        raise ProposerConfigError(
            f"runtime.proposer_agent: {dotted} names a built-in proposer runtime "
            f"that was removed, and {_REPLACEMENT}. An operator-supplied class of "
            "your own is still accepted here."
        )


def refuse_removed_proposer_directory(proposer_path: Path | None) -> None:
    """Refuse a proposer directory carrying an executable agent module.

    A ``proposers/<name>/`` directory is still how an epoch declares the
    proposer's skills. What it may not carry is ``agent.py``: custom
    proposer agents ran on a runtime that was removed, and a directory
    holding one describes a proposer that will not run.
    """
    if proposer_path is None:
        return
    module = proposer_path / "agent.py"
    if module.is_file():
        raise ProposerConfigError(
            f"{module}: custom proposer agent modules were removed, and {_REPLACEMENT}. "
            "The directory's skills/*.md still steer the proposer; delete agent.py."
        )


__all__ = [
    "PROPOSER_BLOCK_KEY",
    "UNSET_BINARY",
    "REMOVED_RUNTIME_KEYS",
    "BUILT_IN_PROPOSER_NAMESPACE",
    "VIEWER_POLICIES",
    "FoeBudget",
    "FoeModelRole",
    "FoeProposerConfig",
    "ProposerConfigError",
    "load_foe_proposer_config",
    "refuse_removed_proposer_configuration",
    "refuse_removed_proposer_directory",
    "scaffold_proposer_block",
]
