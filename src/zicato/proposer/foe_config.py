"""The workspace's ``proposer`` block: one typed object, strictly validated.

Zicato's proposer is a Foe episode, and everything a workspace decides
about that episode is declared in one block of its ``config.json``::

    "proposer": {
      "binary": "/usr/local/bin/foe",
      "budget": {"model_calls": 12, "seconds": 900,
                 "input_tokens": 400000, "output_tokens": 60000},
      "model": {"provider": "example", "model": "example-model",
                "options": {"api_key_file": "/home/me/.config/foe/key.json"}},
      "viewer": "off"
    }

Four decisions, and no fifth. The **binary** is the Foe build the episode
runs, named by absolute path because the episode's grants are absolute
and a relative one would mean different things to the loop and to a
worker. The **budget** bounds the episode in Foe's own dimensions and is
part of what Foe fingerprints, so raising it rolls the epoch. The
**model** block is what Foe's built-in transport calls; its credential is
a file Foe reads, per ``foe/docs/models.md``, never an environment
variable this package invents, and model selection rolls no epoch. The
**viewer** decides when a finished episode's trajectory is served for an
operator to read.

The block carries no instructions: those are the epoch's proposer brief
and its skills, which the contract already hashes, assembled by
:mod:`zicato.proposer.foe_request`.

Validation is strict and names the removal. A workspace still carrying
the configuration of a retired proposer — the coding-agent integration's
binary, an ADK or native proposer class, a ``proposers/<name>/agent.py``
module — is refused with the key, what replaced it, and where to read
about the change, rather than silently falling back. What the seam still accepts is
an operator's own ``ExternalProposerAgent`` class, which is a different
thing from a removed built-in and is named in ``docs/design/PROPOSER.md``
with the trust boundary it runs under.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from zicato.proposer.external import DEFAULT_PROPOSER_AGENT, UNSET_BINARY

#: The ``config.json`` key holding everything below.
PROPOSER_BLOCK_KEY = "proposer"

#: What a viewer policy may say. ``off`` serves nothing; ``on-failure``
#: serves an episode that did not complete, which is the one an operator
#: reads; ``always`` serves every episode.
VIEWER_POLICIES: tuple[str, ...] = ("off", "on-failure", "always")

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
class FoeBudget:
    """What one proposal episode may spend, in Foe's budget dimensions.

    ``model_calls`` is the only required dimension because Foe requires
    it; the rest are unlimited when omitted, which ``foe/docs/config.md``
    states. ``seconds`` is the deadline every episode below the root
    shares, and the bound the supervisor watchdog enforces from outside
    when a process outlives it.

    The budget participates in Foe's contract fingerprint, so changing any
    dimension rolls the epoch. That is the intended reading: a proposer
    with twelve model calls investigates differently from one with three.
    """

    model_calls: int = 12
    seconds: int | None = 900
    input_tokens: int | None = None
    output_tokens: int | None = None

    def validate(self) -> None:
        if self.model_calls < 1:
            raise ProposerConfigError("proposer.budget.model_calls must be >= 1")
        for name in ("seconds", "input_tokens", "output_tokens"):
            value = getattr(self, name)
            if value is not None and value < 1:
                raise ProposerConfigError(f"proposer.budget.{name} must be >= 1 when set")


@dataclass(frozen=True, slots=True)
class FoeModelRole:
    """The ``model`` block Foe's built-in transport calls.

    ``options`` carries the provider-specific flat strings
    ``foe/docs/models.md`` lists — ``api_key_file``, ``base_url``,
    ``reasoning_effort``, and the rest. A credential is always a FILE Foe
    reads: this package neither reads nor forwards a credential, and it
    defines no environment variable of its own.

    Model selection is runtime infrastructure and never rolls an epoch,
    matching the standing rule that keeps every ``models.*`` role out of
    the contract hash. Foe excludes the block from its fingerprint for the
    same reason.
    """

    provider: str
    model: str
    options: Mapping[str, str] = field(default_factory=dict)

    def validate(self) -> None:
        if not self.provider:
            raise ProposerConfigError("proposer.model.provider must name a Foe provider")
        if not self.model:
            raise ProposerConfigError("proposer.model.model must name a model")


@dataclass(frozen=True, slots=True)
class FoeProposerConfig:
    """Everything a workspace decides about its proposal episodes."""

    binary: Path
    model: FoeModelRole
    budget: FoeBudget = field(default_factory=FoeBudget)
    viewer: str = "off"
    #: The workspace this configuration was read from. The episode places
    #: its log directory and its scratch trees relative to it.
    workspace_root: Path | None = None

    def validate(self) -> None:
        if not self.binary.is_absolute():
            raise ProposerConfigError(
                f"proposer.binary must be an absolute path, got {str(self.binary)!r}"
            )
        if self.viewer not in VIEWER_POLICIES:
            raise ProposerConfigError(
                f"proposer.viewer must be one of {', '.join(VIEWER_POLICIES)}, "
                f"got {self.viewer!r}"
            )
        self.model.validate()
        self.budget.validate()


def _mapping(block: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = block.get(key)
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ProposerConfigError(f"proposer.{key} must be an object")
    return value


def _optional_int(block: Mapping[str, Any], key: str, where: str) -> int | None:
    value = block.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProposerConfigError(f"{where}.{key} must be an integer")
    return value


def scaffold_proposer_block() -> dict[str, Any]:
    """The ``proposer`` block a freshly initialized workspace carries.

    Complete but not yet runnable: every decision is spelled out with the
    documented default so an operator edits rather than researches, and
    :data:`UNSET_BINARY` marks the one field only they can fill.
    """
    defaults = FoeBudget()
    return {
        "binary": UNSET_BINARY,
        "budget": {
            "model_calls": defaults.model_calls,
            "seconds": defaults.seconds,
        },
        "model": {
            "provider": "<foe provider>",
            "model": "<model id>",
            "options": {"api_key_file": "<absolute path of the credential file Foe reads>"},
        },
        "viewer": "off",
        "_guide": {
            "binary": "absolute path of the foe binary this workspace's episodes run",
            "budget": "what one proposal episode may spend; raising any dimension rolls the epoch",
            "model": (
                "the provider and model Foe's transport calls; the credential is a FILE "
                "Foe reads, named under options, never an environment variable"
            ),
            "viewer": f"when a finished episode is served: {', '.join(VIEWER_POLICIES)}",
        },
    }


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
    binary = str(block.get("binary") or "")
    if not binary:
        raise ProposerConfigError(
            "proposer.binary: name the absolute path of the Foe binary this "
            "workspace runs its proposal episodes with"
        )
    model_block = _mapping(block, "model")
    if not model_block:
        raise ProposerConfigError(
            "proposer.model: name the Foe provider and model the episode "
            "calls; a credential is a file Foe reads, named under `options`"
        )
    options_block = _mapping(model_block, "options")
    budget_block = _mapping(block, "budget")
    defaults = FoeBudget()
    config = FoeProposerConfig(
        binary=Path(binary).expanduser(),
        model=FoeModelRole(
            provider=str(model_block.get("provider") or ""),
            model=str(model_block.get("model") or ""),
            options={str(k): str(v) for k, v in options_block.items()},
        ),
        budget=FoeBudget(
            # A declared dimension is taken as declared, including a zero
            # the validator then refuses; only an ABSENT key falls back,
            # so a bound cannot be widened by writing a value that reads
            # as false.
            model_calls=(
                _optional_int(budget_block, "model_calls", "proposer.budget")
                if budget_block.get("model_calls") is not None
                else defaults.model_calls
            )
            or 0,
            seconds=(
                _optional_int(budget_block, "seconds", "proposer.budget")
                if "seconds" in budget_block
                else defaults.seconds
            ),
            input_tokens=_optional_int(budget_block, "input_tokens", "proposer.budget"),
            output_tokens=_optional_int(budget_block, "output_tokens", "proposer.budget"),
        ),
        viewer=str(block.get("viewer") or "off"),
        workspace_root=workspace_root,
    )
    config.validate()
    return config


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
