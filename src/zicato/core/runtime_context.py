"""Typed run dependencies passed to owned workers and inherited nested children."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from zicato.core.configuration import authored_dataclass_from_json, dataclass_to_jsonable
from zicato.core.run_context import RunContext


@dataclass(frozen=True, slots=True)
class TelemetryEndpoints:
    """Browser and native telemetry addresses selected for one invocation."""

    web_url: str = ""
    grpc_target: str = ""

    def __post_init__(self) -> None:
        if self.grpc_target and not self.web_url:
            raise ValueError("a native telemetry endpoint requires its browser URL")


@dataclass(frozen=True, slots=True)
class WorkerRuntimeContext:
    """Operational addresses and run coordinates that never enter evaluation hashes."""

    telemetry: TelemetryEndpoints = TelemetryEndpoints()
    run: RunContext | None = None

    def to_json(self) -> dict[str, Any]:
        return dataclass_to_jsonable(self)

    @classmethod
    def from_json(cls, raw: object) -> WorkerRuntimeContext:
        return authored_dataclass_from_json(cls, raw, path="runtime_context")
