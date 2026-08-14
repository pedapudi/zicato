"""Declared wire contracts for the shared operator read model."""

from __future__ import annotations

from typing import Any, NotRequired, TypedDict


class LivenessPayload(TypedDict):
    state: str
    last_heartbeat: NotRequired[str]
    ended_at: NotRequired[str]
    epoch_id: NotRequired[str]


class SnapshotPayload(TypedDict):
    heartbeat: dict[str, Any] | None
    liveness: LivenessPayload
    lock: dict[str, Any] | None
    active_runs: list[dict[str, Any]]
    active_tournament: dict[str, Any] | None
    lineage: Any
    epoch_id: str | None
    epoch: dict[str, Any]
    paused: bool
    generated_at: str
