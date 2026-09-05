"""Declared wire contracts for the shared operator read model."""

from __future__ import annotations

from typing import Any, Final, NotRequired, TypedDict


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


class ObjectPayload(TypedDict, total=False):
    """Common best-effort JSON object envelope.

    Endpoint-specific records remain JSON values, but their top-level shape is
    declared here instead of being an undocumented ``dict[str, Any]`` at the
    HTTP boundary. Keys are optional because every reader is best-effort and may
    answer with a partial or empty read.
    """

    epoch_id: str | None
    generation_id: str | None
    entry_id: str | None
    tournament_id: str | None
    reflection_id: str | None
    run_id: str | None
    note: str
    error: str
    decision: str | None
    promoted: bool | None
    generated_at: str
    source: str
    items: list[dict[str, Any]]


class CollectionPayload(ObjectPayload, total=False):
    generations: list[dict[str, Any]]
    epochs: list[dict[str, Any]]
    entries: list[dict[str, Any]]
    judges: list[dict[str, Any]]
    tournaments: list[dict[str, Any]]
    reflections: list[dict[str, Any]]
    traces: list[dict[str, Any]]
    runs: list[dict[str, Any]]
    events: list[dict[str, Any]]
    points: list[dict[str, Any]]
    rounds: list[dict[str, Any]]
    findings: list[dict[str, Any]]
    recommendations: list[dict[str, Any]]


class ExecutionPlanPayload(ObjectPayload, total=False):
    """The epoch's loop as one tree of stages, steps, and work units.

    ``stages`` holds the recursive plan nodes (:meth:`~zicato.query.PlanNode.payload`);
    ``board`` carries the frozen board's digest and entry count, which is what
    a client needs to fetch the board ONCE instead of receiving a copy of each
    entry on every unit node.
    """

    board: dict[str, Any]
    stages: list[dict[str, Any]]


class LiveExecutionPlanPayload(ExecutionPlanPayload, total=False):
    """The running epoch's plan, plus the overlay describing what runs now.

    ``liveness`` is the served tri-state the whole overlay gates on;
    ``overlay`` carries the active path, the in-flight partition, and the
    phase the path was derived from. The ``stages`` nodes are the durable
    ones with an added ``active`` flag.
    """

    liveness: LivenessPayload
    overlay: dict[str, Any]


class DetailPayload(ObjectPayload, total=False):
    # A ``summary`` is a nested record on the payloads that carry one, and a
    # one-line human-readable string on the payloads whose summary IS that
    # line (a suggestion, a run-log event). Both spellings are declared.
    summary: dict[str, Any] | str
    contract: dict[str, Any]
    gate: dict[str, Any]
    # A decision surface names its two sides either as an id or as the
    # record behind it, depending on how much of the pair the reader
    # resolved: the gate breakdown, the matchup grid and the per-judge
    # comparison put the generation id here, while the round timeline puts
    # the resolved record. Both spellings, and the unresolved side, are
    # declared rather than one of them.
    champion: dict[str, Any] | str | None
    challenger: dict[str, Any] | str | None
    transcript: dict[str, Any]
    rows: list[dict[str, Any]]
    turns: list[dict[str, Any]]
    annotations: list[dict[str, Any]]


class ProposalEpisodeExportPayload(ObjectPayload, total=False):
    """Whether one candidate has Foe's static episode page, and how to get it.

    ``export_available`` is what the proposer panel branches on. The other
    two fields are what it shows when there is no page: ``episode_log`` is
    the log on disk, and ``command`` renders it by hand with the same
    binary the episode ran. Both are empty for a candidate with no proposal
    episode at all. ``slot`` names one best-of-N slate slot, and is null
    when the caller named the generation alone.
    """

    slot: int | None
    episode_log: str
    export_available: bool
    command: str


# Every JSON GET has one declared boundary contract. Routes that share an
# envelope share one type, so each wire spelling is declared here once rather
# than in a per-route wrapper.
ENDPOINT_PAYLOADS: Final[dict[str, type[Any]]] = {
    path: CollectionPayload
    for path in (
        "/api/active-runs",
        "/api/calibration-trend",
        "/api/lineage",
        "/api/logs",
        "/api/reflections",
        "/api/run-log",
        "/api/score-trajectory",
        "/api/search",
        "/api/tournaments",
        "/api/workspace",
        "/api/epoch/{epoch_id}/evals",
        "/api/epoch/{epoch_id}/judge-roster",
        "/api/epoch/{epoch_id}/per-judge-trend",
        "/api/epoch/{epoch_id}/round-timeline",
        "/api/generation/{epoch_id}/{generation_id}/per-entry",
        "/api/generation/{epoch_id}/{generation_id}/per-judge",
        "/api/reflection/{reflection_id}/scorecards",
        "/api/reflection/{reflection_id}/traces",
    )
}
ENDPOINT_PAYLOADS.update(
    {
        path: DetailPayload
        for path in (
            "/api/active-tournament",
            "/api/config",
            "/api/contract-diff/{epoch_id}",
            "/api/conversation/{run_id}",
            "/api/drift-movements/{generation_id}",
            "/api/environment",
            "/api/epoch",
            "/api/epoch/{epoch_id}/analysis",
            "/api/epoch/{epoch_id}/candidate/{generation_id}",
            "/api/epoch/{epoch_id}/cost",
            "/api/epoch/{epoch_id}/eval-health",
            "/api/epoch/{epoch_id}/eval/{entry_id}",
            "/api/epoch/{epoch_id}/experiments-ledger",
            "/api/epoch/{epoch_id}/journal",
            "/api/epoch/{epoch_id}/racing-field",
            "/api/epoch/{epoch_id}/trajectory",
            "/api/files",
            "/api/files/{epoch_id}/{generation_id}/content",
            "/api/files/{epoch_id}/{generation_id}/diff",
            "/api/files/{epoch_id}/{generation_id}/patches",
            "/api/files/{epoch_id}/{generation_id}/tree",
            "/api/health",
            "/api/health-report",
            "/api/heartbeat",
            "/api/hypothesis-accuracy/{epoch_id}/{generation_id}",
            "/api/live/pipeline",
            "/api/matchup-grid/{epoch_id}/{champion_id}/{challenger_id}",
            "/api/matchup/{entry_id}/conversations",
            "/api/mutations/{epoch_id}",
            "/api/mutations/{epoch_id}/{mutation_id}",
            "/api/proposer/recommendations",
            "/api/proposer/scorecard",
            "/api/reflection/{reflection_id}/practices",
            "/api/reflection/{reflection_id}/suggestion/{suggestion_id}/provenance",
            "/api/reflection/{reflection_id}/summary",
            "/api/reflection/{reflection_id}/trace/{trace_id}",
            "/api/reflection/{reflection_id}/xray/{judge_name}/{run_ref}",
            "/api/round/{epoch_id}/{champion_id}/{challenger_id}/gate",
            "/api/round/{epoch_id}/{champion_id}/{challenger_id}/per-judge-comparison",
            "/api/run/{epoch_id}/{generation_id}/{entry_id}/expectations",
            "/api/run/{epoch_id}/{generation_id}/{entry_id}/header",
            "/api/run/{epoch_id}/{generation_id}/{entry_id}/per-judge",
            "/api/run/{epoch_id}/{generation_id}/{entry_id}/transcript",
            "/api/run/{epoch_id}/{generation_id}/{entry_id}/transcript/delta",
            "/api/run/{run_id}/per-judge",
            "/api/state",
            "/api/tournament-structure/{epoch_id}/{tournament_id}",
            "/api/tournaments/{generation_id}",
        )
    }
)
ENDPOINT_PAYLOADS["/api/generation/{epoch_id}/{generation_id}/episode-export"] = (
    ProposalEpisodeExportPayload
)
ENDPOINT_PAYLOADS["/api/epoch/{epoch_id}/execution-plan"] = ExecutionPlanPayload
ENDPOINT_PAYLOADS["/api/live/execution-plan"] = LiveExecutionPlanPayload
