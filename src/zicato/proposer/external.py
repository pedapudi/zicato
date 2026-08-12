"""The external-proposer seam: a dotted path to a non-ADK proposer agent.

:func:`~zicato.proposer.agent.build_proposer_agent` resolves three ways,
and all three end in an ADK agent or the text shim. A coding-agent
proposer — one that runs as its own process and investigates before it
emits — cannot go through either door. But
:class:`~zicato.proposer.agent.ProposerAgent` is a one-method protocol, so
it needs no new machinery: only a way to *name* an implementation and a
way to *hash* it.

This module is both.

* ``[runtime] proposer_agent = "pkg.module:Class"`` names the class.
  :func:`external_proposer_config` reads it off a workspace config and
  :func:`load_external_proposer_class` imports it, through the same
  :func:`zicato.import_path.import_dotted_path` the runtime factory uses
  for ``harness_call_llm`` / ``auxiliary_call_llm``.
* The class answers :meth:`ExternalProposerAgent.contract_identity` with
  its *causal surface* — the things that decide how it reasons — and
  :func:`external_identity_sha256` reduces that to one hex digest that
  :func:`zicato.epoch.contract._canon_proposer` folds into the contract
  hash. Configuring an external proposer therefore rolls the epoch, and so
  does editing the files it runs on.

What belongs in that identity is the question the seam exists to answer.
Version strings of the things we did not write (a coding agent's release,
an adapter package) and content hashes of the things we did (our own
extension files, edited in place, so they have no version to record). NOT
the model: a ``models.*`` role — and ``auxiliary_model`` with it — is
runtime infra that never rolls an epoch, and nothing in the contract hash
has ever named a model. The model-collusion hazard the external tier
introduces (an agent quietly falling back to its own configured default)
is closed where it actually happens, at launch, by threading the resolved
:attr:`~zicato.proposer.agent.ProposerContext.model` into the process and
refusing to start without it.

Configuration lives under ``runtime`` because that is where the workspace
keeps its dotted paths. The identity is what makes it contract.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from zicato.import_path import import_dotted_path

if TYPE_CHECKING:  # pragma: no cover - typing-only imports
    from zicato.core.types import Experiment, ProposerSkill, ProposerSpec
    from zicato.proposer.agent import ProposerContext

#: The workspace-config key, under ``runtime``, that names the class.
PROPOSER_AGENT_KEY = "proposer_agent"

#: Label used in ``import_dotted_path`` errors, so a bad path points the
#: operator at the field that produced it.
_IMPORT_LABEL = f"runtime.{PROPOSER_AGENT_KEY}"


@dataclass(frozen=True, slots=True)
class ExternalProposerConfig:
    """Everything an external proposer needs to resolve itself.

    Deliberately thin: the dotted path that named the class, the
    workspace root it may place per-challenger scratch state under, and
    the workspace's ``runtime`` block so an implementation can read its
    own knobs (``pi_bin``, for the pi agent) without a bespoke field per
    implementation.

    One builder — :func:`external_proposer_config` — produces this value
    for both the contract-hash path and the orchestrator's build path, so
    the identity that was hashed and the agent that runs are resolved from
    the same inputs by construction.

    ``options`` is NOT hashed wholesale. It carries unrelated runtime
    keys (the harness/auxiliary dotted paths among them), and a change to
    those is infra, not contract. An implementation folds in only what
    causally steers it — for the pi agent, the knob's *effect* (the
    resolved version of the binary it selects), not the knob's spelling.
    """

    dotted_path: str
    workspace_root: Path | None = None
    options: Mapping[str, str] = field(default_factory=dict)


class ExternalProposerAgent(Protocol):
    """A :class:`~zicato.proposer.agent.ProposerAgent` that is also hashable.

    An implementation is constructed with the keywords
    ``(spec=..., config=...)`` and must answer two things:

    * :meth:`contract_identity` — a classmethod returning the JSON-shaped
      causal surface described in this module's docstring. It is called at
      contract-hash time, on a machine that may never launch the agent, so
      it must not require a running process, a network, or credentials.
    * :meth:`propose` — the :class:`~zicato.proposer.agent.ProposerAgent`
      protocol itself, raising
      :class:`~zicato.proposer.proposer.ProposerError` when it cannot
      produce a schema-valid experiment within its budget.

    An implementation may also declare ``external_id``, a short label
    (``"pi"``) that spells the agent id ``external:pi``. Absent one, the
    dotted path is the label.
    """

    @classmethod
    def contract_identity(cls, config: ExternalProposerConfig) -> Mapping[str, Any]: ...

    async def propose(self, ctx: ProposerContext) -> Experiment: ...


def external_proposer_config(
    workspace_config: Mapping[str, Any],
    workspace_root: Path | None = None,
) -> ExternalProposerConfig | None:
    """Read ``runtime.proposer_agent`` off a workspace config.

    Returns ``None`` when no external proposer is configured — the
    overwhelmingly common case, and the one in which every downstream
    surface (the spec, the contract canon, the agent builder) is
    byte-identical to before this seam existed.
    """
    runtime = workspace_config.get("runtime")
    if not isinstance(runtime, Mapping):
        return None
    dotted = runtime.get(PROPOSER_AGENT_KEY)
    if not dotted:
        return None
    options = {str(k): str(v) for k, v in runtime.items() if isinstance(v, str)}
    return ExternalProposerConfig(
        dotted_path=str(dotted),
        workspace_root=workspace_root,
        options=options,
    )


def load_external_proposer_class(dotted_path: str) -> type[ExternalProposerAgent]:
    """Import the class named by ``runtime.proposer_agent``.

    Raises
    ------
    ValueError
        The path is malformed, the module cannot be imported, the
        attribute is absent, or the resolved object is not a class
        answering :meth:`ExternalProposerAgent.contract_identity`. The
        last check runs here rather than at first propose so a
        misconfiguration fails at contract-hash time, before a round
        starts.
    """
    obj = import_dotted_path(dotted_path, label=_IMPORT_LABEL)
    if not isinstance(obj, type):
        raise ValueError(
            f"{_IMPORT_LABEL}: {dotted_path!r} resolved to a {type(obj).__name__}, "
            "expected a class (see zicato.proposer.external.ExternalProposerAgent)"
        )
    if not hasattr(obj, "contract_identity"):
        raise ValueError(
            f"{_IMPORT_LABEL}: {dotted_path!r} names {obj.__name__}, which has no "
            "contract_identity() classmethod — without one the proposer cannot be "
            "folded into the contract hash (see zicato.proposer.external)"
        )
    return obj


def external_agent_id(cls: type[ExternalProposerAgent], config: ExternalProposerConfig) -> str:
    """The ``external:<label>`` agent id for a resolved external class."""
    label = getattr(cls, "external_id", "") or config.dotted_path
    return f"external:{label}"


def identity_sha256(identity: Mapping[str, Any]) -> str:
    """Hex SHA-256 of a canonicalized external-proposer causal surface.

    Serialized sorted-key so an implementation that builds its dict in a
    different order does not move the hash, and reduced to a single digest
    so the contract canon carries one opaque string per external proposer
    rather than an open-ended blob.
    """
    canon = json.dumps(dict(identity), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def resolve_external_spec(
    config: ExternalProposerConfig,
    *,
    skills: tuple[ProposerSkill, ...] = (),
) -> ProposerSpec:
    """Resolve an :class:`ExternalProposerConfig` into a hash-ready spec.

    The class is asked for its identity exactly once; the spec's ``tools``
    are read off that same mapping's ``tools`` key, so the sanctioned tool
    set and the hashed tool set cannot drift apart.

    ``skills`` are the proposer dir's skill modules when a workspace
    configures both (an external proposer may still be steered by
    ``proposers/<name>/skills/*.md`` — those are the hashed skills, and
    the reason such an agent turns its own runtime's skill system off).
    """
    from zicato.core.types import ProposerSpec  # noqa: PLC0415 - avoid an import cycle

    cls = load_external_proposer_class(config.dotted_path)
    identity = cls.contract_identity(config)
    return ProposerSpec(
        agent_id=external_agent_id(cls, config),
        tools=tuple(str(t) for t in identity.get("tools") or ()),
        skills=skills,
        agent_source_sha256=None,
        external_path=config.dotted_path,
        external_identity_sha256=identity_sha256(identity),
    )


__all__ = [
    "PROPOSER_AGENT_KEY",
    "ExternalProposerAgent",
    "ExternalProposerConfig",
    "external_agent_id",
    "external_proposer_config",
    "identity_sha256",
    "load_external_proposer_class",
    "resolve_external_spec",
]
