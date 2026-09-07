"""Capture editable inputs and recover a validated contract publication."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from zicato.epoch.contract import (
    ContractInputs,
    canonical_scoring_json,
    compute_contract_hash,
    compute_proposer_hash,
    default_contract_paths,
    resolve_contract_inputs,
)
from zicato.storage import atomic_write_json, atomic_write_text
from zicato.workspace.config_io import WorkspaceConfig, read_workspace_config
from zicato.workspace.contract_publication import (
    assert_contract_publication_complete,
    contract_publication_path,
    decode_contract_publication,
    read_contract_publication,
    require_contract_revision,
)

if TYPE_CHECKING:
    from zicato.runtime.lock import WorkspaceLock

_COMPONENTS = ("board", "brief", "scoring", "config")


def _text(path: Path) -> str | None:
    try:
        return path.read_bytes().decode("utf-8")
    except FileNotFoundError:
        return None


def _digest(text: str | None) -> str | None:
    return None if text is None else hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ContractSourceFile:
    component: str
    path: Path
    text: str | None

    @property
    def digest(self) -> str | None:
        return _digest(self.text)


@dataclass(frozen=True, slots=True)
class ContractSource:
    """The exact editable bytes and proposal identity from which a draft started."""

    workspace_root: Path
    config: WorkspaceConfig
    inputs: ContractInputs
    files: tuple[ContractSourceFile, ...]
    proposer_hash: str

    def file(self, component: str) -> ContractSourceFile:
        return next(file for file in self.files if file.component == component)

    def require_unchanged(self, workspace_root: Path) -> None:
        if workspace_root.resolve() != self.workspace_root:
            raise ValueError("draft belongs to another workspace")
        assert_contract_publication_complete(workspace_root)
        changed = [
            file.component for file in self.files if _digest(_text(file.path)) != file.digest
        ]
        if compute_proposer_hash(self.inputs) != self.proposer_hash:
            changed.append("proposer")
        if changed:
            raise ValueError(
                "editable contract changed since draft creation: " + ", ".join(changed)
            )


def capture_contract_source(workspace_root: Path) -> ContractSource:
    """Capture live files once and reject changes across the capture."""
    workspace_root = workspace_root.resolve()
    revision = assert_contract_publication_complete(workspace_root)
    config = read_workspace_config(workspace_root)
    config_text = _text(config.path)
    if (json.loads(config_text) if config_text is not None else {}) != config.raw:
        raise ValueError("workspace config changed during reading; reload the live contract")
    if config.exists:
        inputs = resolve_contract_inputs(workspace_root)
    else:
        defaults = default_contract_paths(workspace_root)
        inputs = ContractInputs(
            board_path=Path(str(defaults["board_path"])),
            brief_path=Path(str(defaults["brief_path"])),
            scoring_path=Path(str(defaults["scoring_path"])),
            entrypoint="",
            mutable_trees=(),
        )
    paths = (inputs.board_path, inputs.brief_path, inputs.scoring_path, config.path)
    source = ContractSource(
        workspace_root,
        config,
        inputs,
        tuple(
            ContractSourceFile(
                component, path.resolve(), config_text if component == "config" else _text(path)
            )
            for component, path in zip(_COMPONENTS, paths, strict=True)
        ),
        compute_proposer_hash(inputs),
    )
    source.require_unchanged(workspace_root)
    require_contract_revision(workspace_root, revision)
    return source


def publish_contract(
    source: ContractSource,
    accepted: dict[str, str],
    *,
    writer: WorkspaceLock,
) -> None:
    """Persist accepted bytes once, then publish them under mutation ownership."""
    payload = prepare_contract_publication(source, accepted, writer=writer)
    publish_prepared_contract(source.workspace_root, payload, writer=writer)


def prepare_contract_publication(
    source: ContractSource,
    accepted: dict[str, str],
    *,
    writer: WorkspaceLock,
) -> str:
    """Validate and serialize accepted writes without changing live files."""
    from zicato.runtime.lock import validate_workspace_lock  # noqa: PLC0415

    validate_workspace_lock(writer, source.workspace_root)
    source.require_unchanged(source.workspace_root)
    if set(accepted) != set(_COMPONENTS):
        raise ValueError("contract publication requires board, brief, scoring, and config")
    if len({file.path for file in source.files}) != len(_COMPONENTS):
        raise ValueError("contract components must use distinct file paths")
    record = {
        "version": 1,
        "revision": str(uuid4()),
        "state": "pending",
        "writes": [
            {
                "component": file.component,
                "path": str(file.path),
                "expected": file.digest,
                "text": accepted[file.component],
                "accepted_sha256": _digest(accepted[file.component]),
            }
            for file in source.files
        ],
    }
    payload = json.dumps(record, sort_keys=True) + "\n"
    validate_prepared_contract(source.workspace_root, payload, writer=writer)
    return payload


def _validated_writes(
    workspace_root: Path, record: dict[str, Any]
) -> list[tuple[str, Path, str, str | None]]:
    writes = record.get("writes")
    if not isinstance(writes, list) or len(writes) != len(_COMPONENTS):
        raise ValueError("invalid contract publication writes")
    parsed: list[tuple[str, Path, str, str | None]] = []
    for component, write in zip(_COMPONENTS, writes, strict=True):
        if not isinstance(write, dict) or write.get("component") != component:
            raise ValueError("invalid contract publication component")
        path, text, expected = write.get("path"), write.get("text"), write.get("expected")
        if not isinstance(path, str) or not Path(path).is_absolute() or not isinstance(text, str):
            raise ValueError("invalid contract publication path or accepted bytes")
        if Path(path).resolve() != Path(path):
            raise ValueError("contract publication destination changed its resolved path")
        if write.get("accepted_sha256") != _digest(text):
            raise ValueError("contract publication accepted bytes do not match their digest")
        if expected is not None and (
            not isinstance(expected, str)
            or len(expected) != 64
            or any(c not in "0123456789abcdef" for c in expected)
        ):
            raise ValueError("invalid contract publication source digest")
        parsed.append((component, Path(path), text, expected))
    if parsed[-1][1] != read_workspace_config(workspace_root).path.resolve():
        raise ValueError("contract publication config belongs to another workspace")
    if len({path for _, path, _, _ in parsed}) != len(_COMPONENTS):
        raise ValueError("contract publication repeats a destination")
    accepted_config = json.loads(parsed[-1][2])
    contract = accepted_config.get("contract") if isinstance(accepted_config, dict) else None
    if not isinstance(contract, dict):
        raise ValueError("contract publication has no accepted contract paths")
    for component, path, _, _ in parsed[:-1]:
        declared = contract.get(f"{component}_path")
        if component == "brief":
            declared = declared or contract.get("rubric_path")
        if not isinstance(declared, str) or Path(declared) != path:
            raise ValueError(f"contract publication {component} destination differs from config")
    return parsed


def _require_sources(
    parsed: list[tuple[str, Path, str, str | None]], *, completed: bool = False
) -> None:
    conflicts = [
        component
        for component, path, text, expected in parsed
        if _digest(_text(path)) not in ({_digest(text)} if completed else {expected, _digest(text)})
    ]
    if conflicts:
        raise ValueError(
            "contract publication conflicts with edited files: " + ", ".join(conflicts)
        )


def validate_prepared_contract(
    workspace_root: Path,
    payload: str,
    *,
    writer: WorkspaceLock,
    frozen_directory: Path | None = None,
    frozen_contract_hash: str | None = None,
) -> dict[str, Any]:
    """Accept retained writes only for this workspace and unchanged destinations."""
    from zicato.runtime.lock import validate_workspace_lock  # noqa: PLC0415

    validate_workspace_lock(writer, workspace_root)
    record = decode_contract_publication(payload, contract_publication_path(workspace_root))
    if record["state"] != "pending":
        raise ValueError("prepared contract publication must contain pending writes")
    parsed = _validated_writes(workspace_root, record)
    if frozen_directory is not None:
        names = {"board": "board.jsonl", "brief": "brief.md", "scoring": "scoring.json"}
        for component, _, text, _ in parsed[:-1]:
            frozen = (frozen_directory / names[component]).read_bytes()
            matches = (
                canonical_scoring_json(frozen.decode("utf-8")) == canonical_scoring_json(text)
                if component == "scoring"
                else frozen == text.encode("utf-8")
            )
            if not matches:
                raise ValueError(f"contract adoption {component} differs from the frozen epoch")
        accepted_inputs = resolve_contract_inputs(
            workspace_root, workspace_config=json.loads(parsed[-1][2])
        )
        retained_inputs = replace(
            accepted_inputs,
            board_path=frozen_directory / "board.jsonl",
            brief_path=frozen_directory / "brief.md",
            scoring_path=frozen_directory / "scoring.json",
        )
        if compute_contract_hash(retained_inputs) != frozen_contract_hash:
            raise ValueError("contract adoption identity differs from the frozen epoch")
    current = read_contract_publication(workspace_root)
    if current is not None and current["state"] == "pending" and current != record:
        raise ValueError("another contract publication is pending")
    completed = bool(
        current is not None
        and current["revision"] == record["revision"]
        and current["state"] == "complete"
    )
    _require_sources(parsed, completed=completed)
    return record


def publish_prepared_contract(workspace_root: Path, payload: str, *, writer: WorkspaceLock) -> None:
    """Publish retained accepted bytes or recognize their completed revision."""
    record = validate_prepared_contract(workspace_root, payload, writer=writer)
    current = read_contract_publication(workspace_root)
    if current is not None and current["revision"] == record["revision"]:
        if current["state"] == "complete":
            return
    else:
        atomic_write_json(contract_publication_path(workspace_root), record)
    recover_contract_publication(workspace_root, writer=writer)


def recover_contract_publication(workspace_root: Path, *, writer: WorkspaceLock) -> bool:
    """Replay an interrupted edit without overwriting an intervening file edit."""
    from zicato.runtime.lock import validate_workspace_lock  # noqa: PLC0415

    validate_workspace_lock(writer, workspace_root)
    record = read_contract_publication(workspace_root)
    if record is None or record["state"] == "complete":
        return False
    parsed = _validated_writes(workspace_root, record)
    _require_sources(parsed)
    for _, path, text, _ in parsed:
        if _text(path) != text:
            atomic_write_text(path, text, mode=0o666)
    atomic_write_json(
        contract_publication_path(workspace_root),
        {"version": 1, "revision": record["revision"], "state": "complete"},
    )
    return True
