"""The authored workspace root, composed from its domain declarations."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from zicato.core.adapter_config import AdapterDeclaration
from zicato.core.configuration import authored_dataclass_from_json
from zicato.core.constraints import KnobConstraint
from zicato.core.proposer_config import ProposerDeclaration
from zicato.core.settings import RuntimeDeclaration, ZicatoConfig
from zicato.models_config import ModelDeclarations


@dataclass(frozen=True, slots=True)
class ContractSources:
    """Locations and validation rules of the live evaluation contract.

    Fields
    ------
    board_path:
        Board file. Null selects board.jsonl beside the workspace directory.
    brief_path:
        Proposer brief file. Null selects brief.md beside the workspace directory.
    scoring_path:
        Scoring file. Null selects scoring.json beside the workspace directory.
    proposer_path:
        Directory containing proposer skills. Null uses the configured proposer.
    proposer_static_checks:
        Static checks that each proposed patch must satisfy before evaluation.
    """

    board_path: str | None = None
    brief_path: str | None = None
    scoring_path: str | None = None
    proposer_path: str | None = None
    proposer_static_checks: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SnapshotRetention:
    """Optional cleanup of rejected generation source trees when an epoch closes.

    Fields
    ------
    on_epoch_close:
        Apply the configured source-retention policy when an epoch closes.
    keep_last_n:
        Keep this many recent source trees as well as every protected generation.
    keep_promoted_only:
        Keep protected generations only; takes precedence over keep_last_n.
        Promoted, unclassified, and unfinished generations remain protected.
    """

    on_epoch_close: bool = False
    keep_last_n: int | None = field(
        default=None, metadata={"constraint": KnobConstraint(minimum=1, allow_none=True)}
    )
    keep_promoted_only: bool = False


@dataclass(frozen=True, slots=True)
class WorkspaceDeclaration(ZicatoConfig):
    """Persisted workspace configuration, including operational and contract inputs.

    Fields
    ------
    runtime:
        Runtime controls and external proposer selection.
    instance_id:
        Workspace label recorded by initialization.
    created_at:
        Initialization timestamp, recorded as an ISO date and time string.
    generation_source_backend:
        Store holding candidate source trees. Empty requires an explicit store
        repair before source execution; initialized workspaces record their store.
    adapter:
        Harness construction, mutable source trees, and fixed import locations.
        Null describes a workspace awaiting harness registration.
    models:
        Named model engines and role assignments; credentials remain references.
    proposer:
        Bounded proposal executable and its model connection. Null selects an
        explicitly configured external proposer class, when one is present.
    contract:
        Live board, brief, scoring, and proposer skill locations.
    calibrate_noise_floor:
        Number of unchanged-system draws that estimate the epoch's noise floor.
        Null skips an explicit calibration request.
    contract_preflight:
        Explicit unchanged-system draw count for contract preflight. Null uses
        the runtime preflight mode and its default draw count.
    storage_gc:
        Optional retention policy for rejected generation source trees.
    """

    runtime: RuntimeDeclaration = RuntimeDeclaration()
    instance_id: str = "default"
    created_at: str = ""
    generation_source_backend: Literal["", "git", "directory"] = ""
    adapter: AdapterDeclaration | None = field(
        default=None, metadata={"scope": "evaluation-contract", "rolls_epoch": True}
    )
    models: ModelDeclarations = field(default_factory=ModelDeclarations)
    proposer: ProposerDeclaration | None = field(
        default=None, metadata={"scope": "evaluation-contract", "rolls_epoch": True}
    )
    contract: ContractSources = field(
        default_factory=ContractSources,
        metadata={"scope": "evaluation-contract", "rolls_epoch": True},
    )
    calibrate_noise_floor: int | None = field(
        default=None, metadata={"constraint": KnobConstraint(minimum=2, allow_none=True)}
    )
    contract_preflight: int | None = field(
        default=None, metadata={"constraint": KnobConstraint(minimum=2, allow_none=True)}
    )
    storage_gc: SnapshotRetention = field(default_factory=SnapshotRetention)


def workspace_declaration(raw: object) -> WorkspaceDeclaration:
    """Reject malformed authored values before any factory or command reads them."""
    return authored_dataclass_from_json(WorkspaceDeclaration, raw, path="config")
