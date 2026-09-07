"""Authored declarations for bounded proposal episodes."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from zicato.core.configuration import ConfigurationError
from zicato.core.constraints import KnobConstraint, validate_knobs

VIEWER_POLICIES: tuple[str, ...] = ("off", "on-failure", "always")


@dataclass(frozen=True, slots=True)
class FoeBudget:
    """Spending limits for one proposal episode.

    Fields
    ------
    model_calls:
        Maximum model calls in the episode. At least one is required.
    seconds:
        Episode deadline in seconds. Null leaves the deadline unbounded.
    input_tokens:
        Maximum input tokens. Null leaves input tokens unbounded.
    output_tokens:
        Maximum output tokens. Null leaves output tokens unbounded.
    """

    model_calls: int = field(default=12, metadata={"constraint": KnobConstraint(minimum=1)})
    seconds: int | None = field(
        default=900, metadata={"constraint": KnobConstraint(minimum=1, allow_none=True)}
    )
    input_tokens: int | None = field(
        default=None, metadata={"constraint": KnobConstraint(minimum=1, allow_none=True)}
    )
    output_tokens: int | None = field(
        default=None, metadata={"constraint": KnobConstraint(minimum=1, allow_none=True)}
    )

    def validate(self) -> None:
        validate_knobs(self)


@dataclass(frozen=True, slots=True)
class FoeModelRole:
    """Model selection passed to the proposal episode's external runtime.

    Fields
    ------
    provider:
        Model backend name understood by the external runtime.
    model:
        Model name understood by the selected backend.
    options:
        Backend-specific string options. Credential options contain references;
        the external runtime resolves credentials at launch.
    """

    provider: str
    model: str
    options: Mapping[str, str] = field(default_factory=dict, metadata={"secret_reference": True})

    def validate(self) -> None:
        for name in ("provider", "model"):
            if not getattr(self, name):
                raise ConfigurationError(f"proposer.model.{name}", "value", "must be non-empty")


@dataclass(frozen=True, slots=True)
class ProposerDeclaration:
    """Persisted proposal settings, independent of an invocation's workspace.

    Fields
    ------
    binary:
        Absolute path of the executable that runs proposal episodes.
    model:
        Model connection and credential references passed to that executable.
    budget:
        Limits that bound how much evidence a proposal episode can gather.
        Changing them rolls the epoch.
    viewer:
        Serve no trajectories, failed episodes, or every episode.
    guide:
        Explanatory JSON retained from scaffolded configurations; unused by execution.
    """

    binary: Path = field(metadata={"scope": "evaluation-contract", "rolls_epoch": True})
    model: FoeModelRole = field(metadata={"scope": "operational", "rolls_epoch": False})
    budget: FoeBudget = field(
        default_factory=FoeBudget, metadata={"scope": "evaluation-contract", "rolls_epoch": True}
    )
    viewer: str = field(
        default="off",
        metadata={
            "scope": "operational",
            "rolls_epoch": False,
            "constraint": KnobConstraint(choices=VIEWER_POLICIES),
        },
    )
    guide: Any = field(
        default=None,
        metadata={"persisted_name": "_guide", "scope": "operational", "rolls_epoch": False},
    )

    def validate(self) -> None:
        if not self.binary.is_absolute():
            raise ConfigurationError("proposer.binary", "value", "must be an absolute path")
        validate_knobs(self)
        self.model.validate()
        self.budget.validate()

    def __post_init__(self) -> None:
        self.validate()
