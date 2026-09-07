"""Captured execution inputs for one explicitly selected evaluation epoch."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from zicato.core.drift_kinds import DriftKind
from zicato.core.types import BoardEntry, ProposerSkill, ProposerSpec, ScoringWeights
from zicato.epoch.contract import ContractInputs, compute_recorded_contract_hash
from zicato.proposer.brief import ProposerBrief
from zicato.proposer.external import ExternalProposerConfig


class ExecutionContractError(ValueError):
    """The selected epoch cannot reproduce its recorded execution contract."""


def _object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ExecutionContractError(f"execution contract {name} must be an object")
    return value


def _strings(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ExecutionContractError(f"execution contract {name} must be a list of strings")
    return tuple(value)


def capture_execution_bindings(inputs: ContractInputs) -> tuple[bytes, ProposerSpec]:
    """Resolve skills once, retaining the same value supplied to canonical hashing."""
    from zicato.driver_imports import driver_import_scope
    from zicato.proposer.foe_config import refuse_removed_proposer_directory
    from zicato.proposer.skills import resolve_proposer_spec

    refuse_removed_proposer_directory(inputs.proposer_path)
    with driver_import_scope(inputs.driver_imports):
        spec = resolve_proposer_spec(inputs.proposer_path, inputs.external_proposer)
    external = inputs.external_proposer
    body = {
        "format": 1,
        "entrypoint": inputs.entrypoint,
        "mutable_trees": inputs.mutable_trees,
        "mutable_tree_identities": inputs.mutable_tree_identities,
        "adapter_spec": inputs.adapter_spec,
        "adapter_source_specs": inputs.adapter_source_specs,
        "adapter_declaration": inputs.adapter_declaration,
        "proposer_static_checks": inputs.proposer_static_checks,
        "proposer_spec": asdict(spec),
        "external_proposer": None
        if external is None
        else {
            "dotted_path": external.dotted_path,
            "options": dict(external.options),
            "workspace_config": dict(external.workspace_config),
        },
    }
    return (json.dumps(body, sort_keys=True, ensure_ascii=False) + "\n").encode(), spec


@dataclass(frozen=True, slots=True)
class EpochExecutionContract:
    """Immutable captured bytes, decoded afresh at mutable domain boundaries.

    Paths identify the selected epoch's storage, never the current-epoch marker.
    Runtime budgets, concurrency, and services belong to the invocation's
    operational configuration rather than this evaluation contract.
    """

    workspace_root: Path
    epoch_id: str
    contract_hash: str
    board_bytes: bytes
    brief_bytes: bytes
    scoring_bytes: bytes
    bindings_bytes: bytes

    def _bindings(self) -> dict[str, Any]:
        body = _object(json.loads(self.bindings_bytes), "bindings")
        if type(body.get("format")) is not int or body["format"] != 1:
            raise ExecutionContractError("unsupported execution contract format")
        optional = {"mutable_tree_identities"}
        if set(body) - optional != {
            "format",
            "entrypoint",
            "mutable_trees",
            "adapter_spec",
            "adapter_source_specs",
            "adapter_declaration",
            "proposer_static_checks",
            "proposer_spec",
            "external_proposer",
        }:
            raise ExecutionContractError(
                "execution contract has missing or unrecognized declarations"
            )
        for name in ("adapter_spec", "adapter_declaration"):
            if body[name] is not None:
                _object(body[name], name)
        return body

    @property
    def board_with_meta(self) -> tuple[list[BoardEntry], tuple[DriftKind, ...], bool]:
        from zicato.board.jsonl import parse_board_with_meta

        return parse_board_with_meta(
            self.board_bytes.decode(), source=f"epoch {self.epoch_id} board"
        )

    @property
    def scoring(self) -> ScoringWeights:
        from zicato.workspace_loader import historical_scoring_weights_from_dict

        return historical_scoring_weights_from_dict(self.raw_scoring)

    @property
    def raw_scoring(self) -> dict[str, Any]:
        return _object(json.loads(self.scoring_bytes), "scoring")

    @property
    def brief(self) -> ProposerBrief:
        from zicato.proposer.brief import parse_brief

        return parse_brief(self.brief_bytes.decode())

    @property
    def proposer_spec(self) -> ProposerSpec:
        raw = _object(self._bindings().get("proposer_spec"), "proposer_spec")
        skills = raw.get("skills")
        if not isinstance(skills, list):
            raise ExecutionContractError("execution contract skills must be a list")
        parsed = []
        for item in skills:
            skill = _object(item, "skill")
            if set(skill) != {"name", "description", "body"} or any(
                not isinstance(value, str) for value in skill.values()
            ):
                raise ExecutionContractError(
                    "execution contract skill requires name, description and body"
                )
            parsed.append(ProposerSkill(**skill))
        for name in ("agent_id", "external_path", "external_identity_sha256"):
            value = raw.get(name)
            if not isinstance(value, str) and not (name != "agent_id" and value is None):
                raise ExecutionContractError(f"invalid execution contract proposer {name}")
        return ProposerSpec(
            agent_id=raw["agent_id"],
            tools=_strings(raw.get("tools"), "tools"),
            skills=tuple(parsed),
            external_path=raw.get("external_path"),
            external_identity_sha256=raw.get("external_identity_sha256"),
        )

    @property
    def external_proposer(self) -> ExternalProposerConfig | None:
        raw = self._bindings().get("external_proposer")
        if raw is None:
            return None
        body = _object(raw, "external_proposer")
        options = _object(body.get("options"), "external proposer options")
        if not isinstance(body.get("dotted_path"), str) or any(
            not isinstance(value, str) for value in options.values()
        ):
            raise ExecutionContractError("invalid external proposer declaration")
        return ExternalProposerConfig(
            dotted_path=body["dotted_path"],
            workspace_root=self.workspace_root,
            options=options,
            workspace_config=_object(body.get("workspace_config"), "proposer configuration"),
            static_checks=self.static_checks,
            adapter_configuration_json=json.dumps(self.adapter_configuration).encode(),
        )

    @property
    def static_checks(self) -> tuple[str, ...]:
        return _strings(self._bindings().get("proposer_static_checks"), "proposer_static_checks")

    @property
    def adapter_configuration(self) -> dict[str, Any]:
        body = self._bindings()
        trees = body.get("mutable_tree_identities")
        result = {
            "adk_entrypoint": body["entrypoint"],
            "mutable_trees": trees if trees is not None else list(self.mutable_trees),
        }
        if body.get("adapter_declaration") is not None:
            result["adapter"] = _object(body["adapter_declaration"], "adapter_declaration")
        elif body.get("adapter_spec") is not None:
            result["adapter"] = _object(body["adapter_spec"], "adapter_spec")
        return result

    @property
    def mutable_trees(self) -> tuple[str, ...]:
        return _strings(self._bindings().get("mutable_trees"), "mutable_trees")

    def _inputs(self) -> ContractInputs:
        from zicato.core.adapter_config import DriverImportContext
        from zicato.core.workspace import epoch_dir

        directory = epoch_dir(self.workspace_root, self.epoch_id)
        body = self._bindings()
        if not isinstance(body.get("entrypoint"), str):
            raise ExecutionContractError("execution contract entrypoint must be a string")
        return ContractInputs(
            board_path=directory / "board.jsonl",
            brief_path=directory / "brief.md",
            scoring_path=directory / "scoring.json",
            entrypoint=body["entrypoint"],
            mutable_trees=self.mutable_trees,
            mutable_tree_identities=(
                _strings(body["mutable_tree_identities"], "mutable_tree_identities")
                if body.get("mutable_tree_identities") is not None
                else None
            ),
            driver_imports=DriverImportContext.from_config(
                self.adapter_configuration, self.workspace_root
            ),
            adapter_spec=body.get("adapter_spec"),
            adapter_source_specs=_strings(body.get("adapter_source_specs"), "adapter_source_specs"),
            adapter_declaration=body.get("adapter_declaration"),
            external_proposer=self.external_proposer,
            proposer_static_checks=self.static_checks,
        )

    def verify_implementation(self) -> None:
        """Refuse changed executable dependencies before another round can spend."""
        from zicato.core.adapter_config import DriverImportContext
        from zicato.driver_imports import driver_import_scope

        with driver_import_scope(
            DriverImportContext.from_config(self.adapter_configuration, self.workspace_root)
        ):
            self._verify_implementation()

    def _verify_implementation(self) -> None:
        from zicato.proposer.external import resolve_external_spec

        spec = self.proposer_spec
        external = self.external_proposer
        if (external is None) != (spec.external_path is None):
            raise ExecutionContractError(
                "execution contract lacks a consistent proposer declaration"
            )
        # Identity receives the declaration that was hashed. Validation inputs
        # are supplied separately for execution and have their own components.
        identity_config = (
            None
            if external is None
            else replace(external, static_checks=None, adapter_configuration_json=None)
        )
        if (
            identity_config is not None
            and resolve_external_spec(identity_config, skills=spec.skills) != spec
        ):
            raise ExecutionContractError(
                f"epoch {self.epoch_id}: proposer implementation differs from its recorded "
                "identity; "
                "restore that implementation or create an epoch for the changed contract"
            )
        inputs = self._inputs()
        for path, captured in (
            (inputs.board_path, self.board_bytes),
            (inputs.brief_path, self.brief_bytes),
            (inputs.scoring_path, self.scoring_bytes),
        ):
            if path.read_bytes() != captured:
                raise ExecutionContractError(f"epoch {self.epoch_id}: retained {path.name} changed")
        if compute_recorded_contract_hash(inputs, proposer_spec=spec) != self.contract_hash:
            raise ExecutionContractError(
                f"epoch {self.epoch_id}: retained inputs or executable dependencies do not match "
                "the recorded contract; restore them or create an epoch for the changed contract"
            )


def load_epoch_execution_contract(
    workspace_root: Path, epoch_id: str, *, workspace_config: Mapping[str, Any]
) -> EpochExecutionContract:
    """Bind a selected epoch, verifying historical reconstruction before execution."""
    from zicato.core.workspace import epoch_dir
    from zicato.epoch.contract import resolve_contract_inputs
    from zicato.epoch.lifecycle import load_epoch

    cfg = load_epoch(workspace_root, epoch_id)
    if not cfg.contract_hash:
        raise ExecutionContractError(
            f"epoch {epoch_id} has no recorded contract identity; create an epoch before executing"
        )
    directory = epoch_dir(workspace_root, epoch_id)
    path = directory / "execution.json"
    if path.exists():
        bindings = path.read_bytes()
    else:
        # Reconstruction is read-only and accepted only by the full recorded
        # identity below. Missing skill bytes cannot be replaced by edited skills.
        inputs = replace(
            resolve_contract_inputs(workspace_root, workspace_config=workspace_config),
            proposer_path=cfg.proposer_path,
        )
        bindings, _spec = capture_execution_bindings(inputs)
    selected = EpochExecutionContract(
        workspace_root=workspace_root,
        epoch_id=epoch_id,
        contract_hash=cfg.contract_hash,
        board_bytes=(directory / "board.jsonl").read_bytes(),
        brief_bytes=(directory / "brief.md").read_bytes(),
        scoring_bytes=(directory / "scoring.json").read_bytes(),
        bindings_bytes=bindings,
    )
    selected.verify_implementation()
    return selected
