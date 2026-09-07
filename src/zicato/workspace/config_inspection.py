"""Configuration output generated from the authored domain declarations."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from zicato.core.configuration import dataclass_schema, dataclass_to_jsonable
from zicato.core.scoring_config import ScoringWeights
from zicato.core.settings import InvocationOverlay, resolve_configuration
from zicato.workspace.config_io import read_workspace_config
from zicato.workspace.config_schema import WorkspaceDeclaration


def configuration_schemas() -> dict[str, dict[str, Any]]:
    """Editor schemas with the persisted path, scope, and explanation of every field."""
    declarations = {"config.json": WorkspaceDeclaration, "scoring.json": ScoringWeights}
    result = {}
    for filename, owner in declarations.items():
        schema = dataclass_schema(owner)
        scope = "evaluation-contract" if filename == "scoring.json" else "operational"
        _describe(schema, filename, "", scope, scope == "evaluation-contract")
        result[filename] = {"$schema": "https://json-schema.org/draft/2020-12/schema", **schema}
    return result


def _describe(
    schema: dict[str, Any], filename: str, prefix: str, scope: str, rolls_epoch: bool
) -> None:
    scope = schema.get("x-scope", scope)
    rolls_epoch = schema.get("x-rolls-epoch", rolls_epoch)
    for name, declared in schema.get("properties", {}).items():
        path = f"{prefix}.{name}" if prefix else name
        declared.setdefault("x-scope", scope)
        declared.setdefault("x-rolls-epoch", rolls_epoch)
        declared.setdefault("x-secret-reference", False)
        declared.setdefault("x-cli", None)
        declared["x-required"] = name in schema.get("required", [])
        declared["x-file"] = filename
        declared["x-path"] = path
        _describe(declared, filename, path, scope, rolls_epoch)
    for alternative in schema.get("anyOf", []):
        _describe(alternative, filename, prefix, scope, rolls_epoch)
    nested = schema.get("additionalProperties")
    if isinstance(nested, dict) and nested:
        _describe(nested, filename, f"{prefix}.<name>", scope, rolls_epoch)


def configuration_fields() -> dict[str, dict[str, Any]]:
    """Index declared fields; scoring paths use a scoring prefix to identify their file."""
    result = {}

    def visit(schema: dict[str, Any], prefix: str) -> None:
        for child in schema.get("properties", {}).values():
            result[prefix + child["x-path"]] = child
            visit(child, prefix)
        for alternative in schema.get("anyOf", []):
            visit(alternative, prefix)
        nested = schema.get("additionalProperties")
        if isinstance(nested, dict):
            visit(nested, prefix)

    for filename, schema in configuration_schemas().items():
        visit(schema, "scoring." if filename == "scoring.json" else "")
    return result


def configuration_scaffold(*, complete: bool = False) -> dict[str, Any]:
    """Emit valid defaults without inventing executable paths or model connections."""
    if not complete:
        return {"config.json": {}, "scoring.json": {}}
    return {
        "config.json": dataclass_to_jsonable(WorkspaceDeclaration()),
        "scoring.json": dataclass_to_jsonable(ScoringWeights()),
    }


def _leaves(value: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    result = {}
    for key, item in value.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(item, Mapping) and item:
            result.update(_leaves(item, path))
        else:
            result[path] = item
    return result


def effective_configuration(
    workspace_root: Path, *, overlay: InvocationOverlay | None = None
) -> dict[str, dict[str, Any]]:
    """Resolve authored values and optional invocation settings without starting services."""
    from zicato.core.scoring_config import scoring_weights_from_dict  # noqa: PLC0415

    workspace = read_workspace_config(workspace_root)
    declaration = workspace.values
    resolved = resolve_configuration(workspace.raw, overlay=overlay)
    values = _leaves(dataclass_to_jsonable(declaration))
    authored = _leaves(workspace.raw)
    result = {
        path: {"value": value, "source": "workspace" if path in authored else "default"}
        for path, value in values.items()
    }
    result.update(resolved.effective_settings())
    scoring_path = Path(declaration.contract.scoring_path or "scoring.json")
    if not scoring_path.is_absolute():
        scoring_path = workspace_root.resolve().parent / scoring_path
    raw_scoring = json.loads(scoring_path.read_text()) if scoring_path.exists() else {}
    scoring = scoring_weights_from_dict(raw_scoring)
    authored_scoring = _leaves(raw_scoring)
    result.update(
        {
            f"scoring.{path}": {
                "value": value,
                "source": "workspace" if path in authored_scoring else "default",
            }
            for path, value in _leaves(dataclass_to_jsonable(scoring)).items()
        }
    )
    return result


def configuration_reference() -> str:
    """Render the same field inventory as a Markdown configuration reference."""
    lines = ["# Configuration reference", "", "Generated from the authored field declarations.", ""]
    for name, declared in configuration_fields().items():
        accepted = {
            key: value
            for key, value in declared.items()
            if key
            in {
                "type",
                "enum",
                "minimum",
                "maximum",
                "exclusiveMinimum",
                "exclusiveMaximum",
            }
        }
        if "anyOf" in declared:
            accepted["anyOf"] = [
                {key: value for key, value in alternative.items() if key in {"type", "enum"}}
                for alternative in declared["anyOf"]
            ]
        default = (
            json.dumps(declared["default"], sort_keys=True) if "default" in declared else "required"
        )
        lines.extend(
            [
                f"## `{name}`",
                "",
                declared.get("description", ""),
                "",
                f"File: `{declared['x-file']}`. Scope: `{declared['x-scope']}`. "
                f"Rolls epoch: `{declared['x-rolls-epoch']}`. "
                f"Secret reference: `{declared['x-secret-reference']}`.",
                f"Default: `{default}`. CLI: `{declared['x-cli'] or 'none'}`.",
                f"Accepted JSON: `{json.dumps(accepted, sort_keys=True)}`.",
                "",
            ]
        )
    return "\n".join(lines)
