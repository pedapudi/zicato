"""JSON schema + parser for proposer-emitted experiments.

The proposer's response is a JSON object with two top-level keys —
``hypothesis`` and ``patches`` — that this module validates and lifts
into a typed :class:`zicato.core.types.Experiment`.

Validation runs in two passes:

1. **Shape pass** (:func:`jsonschema.validate`). Enforces required keys,
   field types, enum domains. Catches the common LLM mistakes — extra
   keys, missing fields, wrong nesting — at the cheapest possible layer.
2. **Cross-check pass** (this module's local logic). Re-checks the parts
   the JSON schema cannot express:
   * every ``patches[*].mutation_id`` resolves in the live mutation
     manifest the orchestrator passed in;
   * the patch op discriminates which ``new_*`` field is required and
     forbids the other two; ``set_numeric`` further requires the value
     to fall inside any ``min`` / ``max`` range the
     :class:`MutationPoint.metadata` declared; ``set_enum`` requires the
     value to appear in the metadata's declared enum domain;
   * drift-kind strings inside ``expected_drift_movements`` are
     registered goldfive kinds (defense in depth — the schema bounds the
     direction / magnitude domains but not the kind set).

Either pass failing raises :class:`ExperimentParseError`. The
orchestrator catches it, appends the message to the next user prompt,
and retries.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import jsonschema

from zicato.core.drift_kinds import GOLDFIVE_DRIFT_KINDS
from zicato.core.types import (
    ExpectedDriftMovement,
    ExpectedMetricMovement,
    Experiment,
    HypothesisSpec,
    MutationPoint,
    Patch,
)

#: JSON Schema for the proposer's structured response.
#:
#: Top-level keys ``hypothesis`` and ``patches`` are both required; the
#: rest of the schema enforces field types and enum domains. ``risks``
#: on the hypothesis is optional; the dataclass defaults it to the
#: empty string when absent.
#:
#: NOTE: ``additionalProperties`` is not set on most subobjects to give
#: the proposer some slack to attach commentary fields the schema author
#: hadn't anticipated. The parser only reads documented keys; unknown
#: keys are silently ignored.
_DIRECTION_ENUM = [
    "decrease",
    "increase",
    "neutral",
    "decrease_or_neutral",
    "increase_or_neutral",
]
_MAGNITUDE_ENUM = ["small", "medium", "large"]

EXPERIMENT_JSON_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["hypothesis", "patches"],
    "properties": {
        "hypothesis": {
            "type": "object",
            # ``expected_drift_movements`` and ``expected_metric_movements``
            # are interchangeable; at least one must be present. Schema-
            # side we require ``core_idea`` / ``modulating`` / ``why`` /
            # ``expected_pass_rate_delta`` only — the "at least one
            # movements field" rule is enforced by the parser since
            # JSON Schema's ``anyOf`` predicates obscure error messages
            # in the proposer-retry path.
            "required": [
                "core_idea",
                "modulating",
                "why",
                "expected_pass_rate_delta",
            ],
            "properties": {
                "core_idea": {"type": "string", "minLength": 1},
                "modulating": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                    "minItems": 1,
                },
                "why": {"type": "string", "minLength": 1},
                "expected_drift_movements": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["kind", "direction", "magnitude"],
                        "properties": {
                            "kind": {"type": "string", "minLength": 1},
                            "direction": {"enum": _DIRECTION_ENUM},
                            "magnitude": {"enum": _MAGNITUDE_ENUM},
                        },
                    },
                },
                "expected_metric_movements": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["metric_name", "direction", "magnitude"],
                        "properties": {
                            "metric_name": {"type": "string", "minLength": 1},
                            "direction": {"enum": _DIRECTION_ENUM},
                            "magnitude": {"enum": _MAGNITUDE_ENUM},
                        },
                    },
                },
                "expected_pass_rate_delta": {"type": "string", "minLength": 1},
                "risks": {"type": "string"},
            },
        },
        "patches": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["mutation_id", "op", "rationale"],
                "properties": {
                    "mutation_id": {"type": "string", "minLength": 1},
                    "op": {"enum": ["replace", "set_numeric", "set_enum"]},
                    "new_content": {"type": "string"},
                    "new_numeric": {"type": "number"},
                    "new_enum": {"type": "string"},
                    "rationale": {"type": "string", "minLength": 1},
                },
            },
        },
    },
}


class ExperimentParseError(ValueError):
    """Raised when a proposer response cannot be parsed into an :class:`Experiment`.

    The message is intentionally specific enough to feed back to the
    proposer on retry. Callers should NOT mutate the message before
    appending it to the next user prompt — the wording is tuned to
    elicit a corrected response.
    """


class PostApplyValidationError(ValueError):
    """Raised when a parsed experiment's patches break the snapshot.

    Unlike :class:`ExperimentParseError`, the proposer response was a
    well-formed :class:`Experiment` — the failure surfaced only *after*
    the patch set was applied to the child snapshot (a dropped import, a
    syntax error, a vanished ``# zicato:mutable`` marker). The validator
    findings are carried verbatim so the proposer-retry path can feed
    them back as concrete, actionable feedback alongside parse errors.

    The :attr:`errors` attribute is the raw per-problem string list from
    :func:`zicato.mutation.validator.validate_post_apply`; the exception
    message joins them for human display.
    """

    def __init__(self, errors: list[str]) -> None:
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


#: Markdown code-fence stripping. Some models wrap their JSON in
#: ```` ```json ... ``` ```` despite explicit instructions; we strip the
#: outer fence before handing the body to :func:`json.loads`. The regex
#: requires the fence to be the FIRST and LAST non-whitespace tokens —
#: an inline code fence inside narrative prose would never legitimately
#: open at the start of the buffer, so this is unambiguous.
_FENCE_RE = re.compile(r"^\s*```(?:json|JSON)?\s*\n(?P<body>.*?)\n```\s*$", re.DOTALL)


def _strip_fences(response_text: str) -> str:
    """Remove a leading/trailing markdown code fence if present.

    Idempotent on already-clean JSON: when the regex doesn't match, the
    input is returned unchanged.
    """

    m = _FENCE_RE.match(response_text)
    if m is not None:
        return m.group("body")
    return response_text


def _validate_op_fields(patch_dict: Mapping[str, Any], idx: int) -> None:
    """Cross-check that the right ``new_*`` field is populated for the op."""

    op = patch_dict["op"]
    has_content = "new_content" in patch_dict
    has_numeric = "new_numeric" in patch_dict
    has_enum = "new_enum" in patch_dict

    if op == "replace":
        if not has_content or not isinstance(patch_dict["new_content"], str):
            raise ExperimentParseError(
                f"patch[{idx}]: op='replace' requires a non-empty string 'new_content' field"
            )
        if not patch_dict["new_content"]:
            raise ExperimentParseError(
                f"patch[{idx}]: op='replace' requires 'new_content' to be non-empty"
            )
        if has_numeric:
            raise ExperimentParseError(f"patch[{idx}]: op='replace' must not set 'new_numeric'")
        if has_enum:
            raise ExperimentParseError(f"patch[{idx}]: op='replace' must not set 'new_enum'")
    elif op == "set_numeric":
        if not has_numeric:
            raise ExperimentParseError(
                f"patch[{idx}]: op='set_numeric' requires a numeric 'new_numeric' field"
            )
        if has_content:
            raise ExperimentParseError(f"patch[{idx}]: op='set_numeric' must not set 'new_content'")
        if has_enum:
            raise ExperimentParseError(f"patch[{idx}]: op='set_numeric' must not set 'new_enum'")
    elif op == "set_enum":
        if not has_enum or not isinstance(patch_dict["new_enum"], str):
            raise ExperimentParseError(
                f"patch[{idx}]: op='set_enum' requires a non-empty string 'new_enum' field"
            )
        if not patch_dict["new_enum"]:
            raise ExperimentParseError(
                f"patch[{idx}]: op='set_enum' requires 'new_enum' to be non-empty"
            )
        if has_content:
            raise ExperimentParseError(f"patch[{idx}]: op='set_enum' must not set 'new_content'")
        if has_numeric:
            raise ExperimentParseError(f"patch[{idx}]: op='set_enum' must not set 'new_numeric'")
    else:  # pragma: no cover — JSON schema enum already gates this
        raise ExperimentParseError(f"patch[{idx}]: unknown op {op!r}")


def _validate_numeric_range(patch_dict: Mapping[str, Any], mp: MutationPoint, idx: int) -> None:
    """Reject ``set_numeric`` patches whose value falls outside the metadata range."""

    value = float(patch_dict["new_numeric"])
    metadata = mp.metadata
    if "min" in metadata:
        try:
            lo = float(metadata["min"])
        except (TypeError, ValueError):
            lo = None  # malformed metadata — fail open, the applier re-checks
        if lo is not None and value < lo:
            raise ExperimentParseError(
                f"patch[{idx}]: new_numeric={value} below min={lo} for mutation {mp.id!r}"
            )
    if "max" in metadata:
        try:
            hi = float(metadata["max"])
        except (TypeError, ValueError):
            hi = None
        if hi is not None and value > hi:
            raise ExperimentParseError(
                f"patch[{idx}]: new_numeric={value} above max={hi} for mutation {mp.id!r}"
            )


def _validate_enum_domain(patch_dict: Mapping[str, Any], mp: MutationPoint, idx: int) -> None:
    """Reject ``set_enum`` patches whose value is not in the metadata domain."""

    domain_raw = mp.metadata.get("enum")
    if not domain_raw:
        return  # metadata didn't declare a closed domain — accept any string
    domain = {tok.strip() for tok in domain_raw.split(",") if tok.strip()}
    value = patch_dict["new_enum"]
    if domain and value not in domain:
        raise ExperimentParseError(
            f"patch[{idx}]: new_enum={value!r} not in declared enum domain "
            f"{sorted(domain)!r} for mutation {mp.id!r}"
        )


def parse_experiment_json(
    response_text: str,
    epoch_id: str,
    parent_gen: str,
    new_gen: str,
    mutations_by_id: Mapping[str, MutationPoint],
    custom_judge_names: frozenset[str] | None = None,
) -> Experiment:
    """Lift a raw proposer response into a typed :class:`Experiment`.

    Parameters
    ----------
    response_text:
        The model's raw response. May contain a leading / trailing
        markdown code fence; the parser strips it before
        :func:`json.loads`.
    epoch_id:
        The epoch this experiment belongs to (lineage coordinate).
    parent_gen:
        The generation this experiment is challenging.
    new_gen:
        The generation id assigned to the child this experiment will
        produce.
    mutations_by_id:
        Live mutation-point manifest — used to validate that each
        ``patches[*].mutation_id`` resolves, and to range-check
        ``set_numeric`` values against per-mutation metadata.
    custom_judge_names:
        Names of the custom judges declared on the active board /
        ``per_judge_weights``. A ``drift:<name>`` metric in
        ``expected_metric_movements`` validates when ``<name>`` is either
        a built-in goldfive :class:`DriftKind` or one of these declared
        judge names — a custom judge emits its signal under the
        ``"custom"`` drift kind but is addressed by its own name in a
        hypothesis. ``None`` (the default) is treated as the empty set,
        so callers that don't thread the contract's judges keep the
        built-in-only behaviour.

    Returns
    -------
    Experiment
        With :attr:`hypothesis` populated, :attr:`patches` as a frozen
        tuple, :attr:`outcome` set to ``None`` (the tournament fills it
        in after the run), and :attr:`proposed_at` stamped to the
        wall-clock UTC time of parse.

    Raises
    ------
    ExperimentParseError
        On any structural problem. The message is suitable for echoing
        back to the proposer on retry.
    """

    cleaned = _strip_fences(response_text).strip()
    if not cleaned:
        raise ExperimentParseError(
            "empty response: expected a JSON object with 'hypothesis' and 'patches' keys"
        )

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ExperimentParseError(
            f"response is not valid JSON: {exc.msg} at line {exc.lineno} col {exc.colno}"
        ) from exc

    if not isinstance(data, dict):
        raise ExperimentParseError(
            f"response must be a JSON object at the top level, got {type(data).__name__}"
        )

    try:
        jsonschema.validate(instance=data, schema=EXPERIMENT_JSON_SCHEMA)
    except jsonschema.ValidationError as exc:
        # Render the path so the proposer can fix the exact field.
        path = "/".join(str(p) for p in exc.absolute_path) or "<root>"
        raise ExperimentParseError(f"schema violation at {path}: {exc.message}") from exc

    hyp_dict = data["hypothesis"]
    # Either expected_drift_movements OR expected_metric_movements must
    # be present; both are accepted and merged. expected_drift_movements
    # is the back-compat path (drift kinds only); expected_metric_movements
    # is the generalised namespaced path (drift / cost / rubric / ...).
    raw_drift_movements = hyp_dict.get("expected_drift_movements", [])
    raw_metric_movements = hyp_dict.get("expected_metric_movements", [])
    if not raw_drift_movements and not raw_metric_movements:
        raise ExperimentParseError(
            "hypothesis: at least one of 'expected_drift_movements' or "
            "'expected_metric_movements' must be present and non-empty"
        )

    drift_movements: list[ExpectedDriftMovement] = []
    for i, mv in enumerate(raw_drift_movements):
        kind = mv["kind"]
        if kind not in GOLDFIVE_DRIFT_KINDS:
            raise ExperimentParseError(
                f"hypothesis.expected_drift_movements[{i}]: unknown drift kind {kind!r}"
            )
        drift_movements.append(
            ExpectedDriftMovement(
                kind=kind,
                direction=mv["direction"],
                magnitude=mv["magnitude"],
            )
        )

    judge_names = custom_judge_names or frozenset()
    metric_movements: list[ExpectedMetricMovement] = []
    for i, mv in enumerate(raw_metric_movements):
        metric_name = mv["metric_name"]
        # Validate drift-namespace metric names against the registered
        # goldfive kind set AND the board's declared custom judges
        # (defense in depth — the schema bounds the direction/magnitude
        # domains but not the kind set). A custom judge emits its signal
        # under the single ``"custom"`` goldfive drift kind, but a
        # hypothesis addresses it by its own ``judge_name`` (e.g.
        # ``drift:file_findability``), so a declared judge name is a valid
        # ``drift:`` metric even though it is not a built-in DriftKind. A
        # name that is neither a built-in kind nor a declared judge is
        # still rejected. Other namespaces are accepted as-is; the
        # convention is namespace-prefixed names but we don't lock down
        # the namespace registry here so harnesses can add new namespaces
        # freely.
        if metric_name.startswith("drift:"):
            bare = metric_name[len("drift:") :]
            if bare not in GOLDFIVE_DRIFT_KINDS and bare not in judge_names:
                raise ExperimentParseError(
                    f"hypothesis.expected_metric_movements[{i}]: unknown drift "
                    f"kind {bare!r} in metric_name {metric_name!r} "
                    f"(not a built-in drift kind and not a declared board judge)"
                )
        metric_movements.append(
            ExpectedMetricMovement(
                metric_name=metric_name,
                direction=mv["direction"],
                magnitude=mv["magnitude"],
            )
        )

    raw_modulating = list(hyp_dict["modulating"])
    # Cross-check every modulating id resolves in the manifest. The
    # proposer is allowed to LIST ids in `modulating` that it isn't
    # patching this round — but every id MUST exist in the manifest, or
    # the journal will be lying about what was touched.
    for ident in raw_modulating:
        if ident not in mutations_by_id:
            raise ExperimentParseError(
                f"hypothesis.modulating: id {ident!r} does not match any known mutation point"
            )

    hypothesis = HypothesisSpec(
        core_idea=hyp_dict["core_idea"],
        modulating=tuple(raw_modulating),
        why=hyp_dict["why"],
        expected_drift_movements=tuple(drift_movements),
        expected_pass_rate_delta=hyp_dict["expected_pass_rate_delta"],
        risks=hyp_dict.get("risks", ""),
        expected_metric_movements=tuple(metric_movements),
    )

    raw_patches = data["patches"]
    patches: list[Patch] = []
    for i, p_dict in enumerate(raw_patches):
        mutation_id = p_dict["mutation_id"]
        mp = mutations_by_id.get(mutation_id)
        if mp is None:
            raise ExperimentParseError(
                f"patch[{i}]: unknown mutation_id {mutation_id!r} "
                "(must match an id from the supplied mutation manifest)"
            )
        _validate_op_fields(p_dict, i)
        op = p_dict["op"]
        if op == "set_numeric":
            _validate_numeric_range(p_dict, mp, i)
        elif op == "set_enum":
            _validate_enum_domain(p_dict, mp, i)
        patches.append(
            Patch(
                id=uuid.uuid4().hex,
                mutation_id=mutation_id,
                op=op,
                new_content=p_dict.get("new_content") if op == "replace" else None,
                new_numeric=(float(p_dict["new_numeric"]) if op == "set_numeric" else None),
                new_enum=p_dict.get("new_enum") if op == "set_enum" else None,
                rationale=p_dict["rationale"],
            )
        )

    return Experiment(
        id=f"exp_{epoch_id}_{new_gen}",
        epoch_id=epoch_id,
        generation_id=new_gen,
        parent_generation_id=parent_gen,
        proposed_at=datetime.now(UTC).isoformat(),
        hypothesis=hypothesis,
        patches=tuple(patches),
        outcome=None,
    )


__all__ = [
    "EXPERIMENT_JSON_SCHEMA",
    "ExperimentParseError",
    "PostApplyValidationError",
    "parse_experiment_json",
]
