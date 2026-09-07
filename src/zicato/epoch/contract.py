"""Contract hashing for auto-epoching.

An epoch is the unit of *evaluation contract*. Six things make up that
contract:

1. The board — test inputs + expectations + judges (``board.jsonl``).
2. The proposer brief — operator steering text (``brief.md``).
3. The scoring — weights + gate thresholds (``scoring.json``).
4. The Zicato evaluator revision — an explicit revision number for changes
   that alter measurement or tournament-decision semantics.
5. The registered system-under-test identity — the validated adapter worker
   specification, the operator's declared adapter block, the adapter
   implementation outside the mutable surface, and the sorted list of
   mutable source-tree paths.
6. The proposer — the agent identity, its tools, and the skill modules
   under a configured ``proposers/<name>/`` dir (or the built-in default
   proposer when none is configured).

A change to any of these makes generations on either side of the change
incomparable, so the epoch must roll. The system under test's *source content* is
NOT part of the contract, because that is what zicato mutates within an epoch.

This module reduces the contract components to a single
``sha256`` hex digest. The hash is *canonicalized* so spurious edits
(whitespace, board-entry reordering, equivalent number spellings) do not
trigger a roll — only semantic changes do.

The hash is computed at epoch-creation time and stored on
:class:`zicato.core.types.EpochConfig`; the orchestrator recomputes it
on every ``evolve`` and rolls the epoch when it drifts. See
``docs/design/EPOCHS-AND-JOURNALING.md`` for the full mechanism.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, cast

from zicato.core.adapter_config import (
    DriverImportContext,
    adapter_declaration,
    registered_mutable_trees,
)
from zicato.core.scoring_config import omit_at_default_fields
from zicato.driver_imports import driver_import_scope, with_workspace_imports
from zicato.epoch._storage import RecordError
from zicato.storage._atomic import atomic_write_text
from zicato.workspace.config_io import read_workspace_config

if TYPE_CHECKING:  # pragma: no cover - typing-only import
    from zicato.core.types import ProposerSpec
    from zicato.proposer.external import ExternalProposerConfig

log = logging.getLogger("zicato.epoch.contract")

# Bump only when Zicato's evaluator protocol changes the meaning of a run,
# loss, or tournament decision. This intentionally excludes presentation,
# query, dashboard, and integration-specific code.
ZICATO_EVALUATOR_REVISION = 2

#: Separator between the canonical component forms before hashing.
#: Chosen to be a byte sequence that cannot appear in any canonical
#: component (a NUL plus a marker word).
_SEP = "\x00--zicato-contract-component--\x00"


@dataclass(frozen=True, slots=True)
class ContractInputs:
    """The contract inputs supplied by the workspace.

    Fields
    ------
    board_path:
        Filesystem path to the live ``board.jsonl``.
    brief_path:
        Filesystem path to the live proposer brief (``brief.md``).
    scoring_path:
        Filesystem path to the live ``scoring.json``.
    entrypoint:
        The registered ADK entrypoint string used when a caller constructs
        inputs directly instead of supplying ``adapter_spec``.
    mutable_trees:
        Physical source directories that identify mutable implementation
        modules. Production resolution also captures the authored source names
        in ``mutable_tree_identities``. Direct callers omitting that field
        retain normalized path identity because no workspace base is available.
    proposer_path:
        Location of the proposer dir (``proposers/<name>/``) the epoch
        steers with, or ``None`` for the built-in default proposer.
        :func:`compute_contract_hash` resolves it to a
        :class:`zicato.core.types.ProposerSpec` and folds the agent id,
        tools, skill bodies, and any custom ``agent.py`` source into the
        hash, so configuring a proposer dir — or editing a skill — rolls
        the epoch. ``None`` (the builtin) canonicalizes to a stable form.
    """

    board_path: Path
    brief_path: Path
    scoring_path: Path
    entrypoint: str
    mutable_trees: tuple[str, ...]
    #: Validated worker reconstruction spec for the selected harness adapter.
    #: ``None`` uses the direct-construction ADK identity in ``entrypoint``.
    adapter_spec: Mapping[str, Any] | None = None
    driver_imports: DriverImportContext = DriverImportContext()
    #: Captured registration identity, separate from physical source locations.
    mutable_tree_identities: tuple[str, ...] | None = None
    #: Dotted implementations whose module source affects adapter behavior.
    adapter_source_specs: tuple[str, ...] = ()
    #: The operator's declared ``adapter`` block from ``config.json``, or
    #: ``None`` when the workspace declares none. An adapter built by
    #: operator code reports its own ``worker_spec()``, which need not echo
    #: what the operator declared, so every declared field the worker
    #: document does not already carry is hashed in its own right — see
    #: :func:`_canon_adapter_declaration`.
    adapter_declaration: Mapping[str, Any] | None = None
    #: Location of the proposer dir (``proposers/<name>/``) frozen for
    #: the epoch, or ``None`` for the built-in default proposer. ``None``
    #: by default so existing construction sites keep working.
    proposer_path: Path | None = None
    #: The resolved ``runtime.proposer_agent`` external proposer, or
    #: ``None`` (the default, and every workspace that configures none).
    #: :func:`_canon_proposer` folds its causal-surface digest in, so
    #: naming an external agent — or upgrading the runtime it launches —
    #: rolls the epoch.
    external_proposer: ExternalProposerConfig | None = None
    #: The tier-2 static-check names the proposer holds its own draft patches
    #: to (``contract.proposer_static_checks`` in ``config.json``; see
    #: :func:`zicato.proposer.validate.declared_static_checks`). This is
    #: contract rather than configuration: changing which checks the proposer
    #: must satisfy changes which patches it accepts from itself, hence what it
    #: proposes. ``()`` — the default, and every workspace that never
    #: configures the feature — is OMITTED from the canonical form, so the
    #: proposer component hashes byte-identically to before this field existed.
    proposer_static_checks: tuple[str, ...] = ()
    #: Canonical worker-role documents captured before execution.
    execution_roles: bytes | None = None


# ---------------------------------------------------------------------------
# Per-component canonicalization
# ---------------------------------------------------------------------------


def component_hashes_from_payload(raw: object) -> dict[str, str]:
    """Accept the recorded component map without restricting component names."""
    if not isinstance(raw, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in raw.items()
    ):
        raise ValueError("expected a JSON object of string component hashes")
    return raw


def read_component_hashes(path: Path) -> dict[str, str] | None:
    """Preserve absence and refuse malformed present component hashes."""
    try:
        return component_hashes_from_payload(json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise RecordError(f"{path}: {exc}") from exc


def write_component_hashes(path: Path, hashes: dict[str, str]) -> None:
    """Publish the existing sorted component map without recalculating identity."""
    component_hashes_from_payload(hashes)
    atomic_write_text(path, json.dumps(hashes, indent=2, sort_keys=True) + "\n")


def _canon_board(board_path: Path) -> str:
    """Canonical form of the board: semantic content only, id-sorted.

    Loads the board through :func:`zicato.board.jsonl.load_board` so the
    canonical form is the validated, parsed shape — not the raw bytes.
    Entries are sorted by id and each is serialized to a sorted-key JSON
    dict. Reordering rows or reformatting the JSONL leaves the hash
    unchanged; editing an entry's input/expectation/weight changes it.

    Beyond the per-entry rows the board also carries two *board-level*
    pieces of contract: the configured ``judges`` and the board-level
    ``disable_drift`` kind list. Both are canonicalized here so swapping
    a judge — or changing which drift kinds are disarmed — correctly
    rolls the epoch.
    Entries and metadata come from one accepted board document. Missing
    metadata retains its default canonical form.
    """
    if not board_path.exists():
        log.warning(
            "contract: board file %s is missing; hashing it as empty",
            board_path,
        )
        return ""
    from zicato.board.jsonl import entry_to_dict, load_board_document  # noqa: PLC0415

    try:
        board = load_board_document(board_path)
    except RecordError as exc:
        raise ValueError(str(exc)) from exc
    if board is None:
        return ""
    entries = board.entries
    canon_entries = [
        json.dumps(
            _fold_entry_grading_source(entry_to_dict(entry)),
            sort_keys=True,
            ensure_ascii=False,
        )
        for entry in sorted(entries, key=lambda e: e.id)
    ]
    meta = _canon_board_meta(board.rows)
    # Prepend the board-level metadata line so it participates in the
    # hash; the leading marker keeps it from colliding with an entry row.
    return "\n".join(["\x00board-meta\x00" + meta, *canon_entries])


def _fold_entry_grading_source(entry: dict[str, object]) -> dict[str, object]:
    """Fold the source hash of an entry's operator-grading dotted specs in.

    Augments the serialized entry dict so editing a referenced PREDICATE or
    PYTHON-mode per-entry JUDGE's source — not only swapping its dotted string —
    rolls the contract hash (issue #19 cross-cutting #1, the ONE source-hashing
    mechanism, aligned with the scoring plugins):

    * an ``expectation`` of ``kind == "predicate"`` has a dotted ``spec``; a
      ``"spec_source"`` key is added carrying its source hash;
    * each per-entry ``judges`` entry with ``mode == "python"`` has a dotted
      ``body``; a ``"body_source"`` key is added carrying its source hash.

    Non-predicate expectations (text / regex / json_schema / rubric specs are not
    dotted plugins) and inline judges are left untouched, so a board that names
    no plugin canonicalizes to the same bytes either way. Operates on a copy of the
    nested dicts so the round-trip serializer (``entry_to_dict``) is unaffected.
    """
    out = dict(entry)
    exp = out.get("expectation")
    if isinstance(exp, Mapping) and exp.get("kind") == "predicate":
        spec = exp.get("spec")
        new_exp = dict(exp)
        new_exp["spec_source"] = _canon_dotted_spec(spec if isinstance(spec, str) else "")
        out["expectation"] = new_exp
    judges = out.get("judges")
    if isinstance(judges, list):
        new_judges: list[object] = []
        for j in judges:
            if isinstance(j, Mapping) and j.get("mode") == "python":
                nj = dict(j)
                body = nj.get("body")
                nj["body_source"] = _canon_dotted_spec(body if isinstance(body, str) else "")
                new_judges.append(nj)
            else:
                new_judges.append(j)
        out["judges"] = new_judges
    return out


def _canon_disable_drift(raw: object) -> object:
    """Reduce the board's ``disable_drift`` to the sorted kind SET.

    Each named kind disarms the built-in judge that emits it
    (:mod:`zicato.judge_runtime.disable`), so which kinds are named
    decides which judges are armed — and the hash must see them. Tokens
    are reduced to their wire form via
    :func:`~zicato.judge_runtime.disable.kind_to_wire_string`, so
    ``DriftKind`` members and bare strings agree; declaration order and
    repeats are no-ops.

    The empty set canonicalizes to ``False``, not ``[]`` — the
    omit-at-default discipline (§3.4) at the value level. ``false`` is
    the byte this form has always carried for "nothing disabled", so a
    board that disables nothing keeps the hash it already has.
    """
    from zicato.judge_runtime.disable import kind_to_wire_string  # noqa: PLC0415

    if not isinstance(raw, list | tuple):
        return bool(raw)
    return sorted({kind_to_wire_string(kind) for kind in raw}) or False


def _canon_board_meta(rows: list[dict[str, Any]]) -> str:
    """Canonical metadata from the same accepted board as its entry rows."""
    header = rows[0] if rows and rows[0].get("board_meta") is True and "id" not in rows[0] else {}
    canon: dict[str, object] = {
        "judges": _canon_judges(header.get("judges", [])),
        "disable_drift": _canon_disable_drift(header.get("disable_drift", ())),
    }
    # Omit false to preserve the canonical form of boards without this flag.
    if header.get("judge_only", False):
        canon["judge_only"] = True
    return json.dumps(canon, sort_keys=True, ensure_ascii=False)


def _canon_dotted_spec(spec: str) -> dict[str, object]:
    """Canonical form of ONE operator-grading dotted spec, with source hash.

    The single source-hashing mechanism every grading plugin shares (issue #19
    cross-cutting #1): expands a dotted spec into
    ``{"spec": <dotted>, "source_sha256": <hash-or-null>}`` via
    :func:`zicato.scoring.plugins.spec_with_source_hash`, so editing the
    resolved plugin's BODY rolls the contract hash, and not only swapping the
    spec string does. Applied uniformly to the scoring ``scalar_fn`` / ``drift_reducer`` /
    ``outcome_summarizer_spec`` AND the board predicates / judges.

    An empty / non-string spec expands to ``{"spec": "", "source_sha256":
    null}``, which is byte-identical to "no plugin", so a board or contract
    that names no plugin is unaffected by the expansion.
    """
    from zicato.scoring.plugins import spec_with_source_hash  # noqa: PLC0415

    if not isinstance(spec, str) or not spec:
        return {"spec": "", "source_sha256": None}
    return dict(spec_with_source_hash(spec))


def _canon_judges(judges: object) -> object:
    """Reduce the board's ``judges`` to a stable, order-independent form.

    Each judge is normalized to a sorted-key dict; the list is then
    sorted by its serialized form so judge declaration order does not
    move the hash. Adding, removing, or editing a judge does.

    A PYTHON-mode judge (``mode == "python"``) points its ``body`` at an
    operator dotted spec; that body is expanded via :func:`_canon_dotted_spec`
    so editing the judge's SOURCE — not only swapping the dotted string — rolls
    the epoch (issue #19 cross-cutting #1, the ONE source-hashing mechanism). A
    non-python judge (inline criterion) is hashed verbatim.
    """
    if not isinstance(judges, list | tuple):
        return judges
    normalized: list[object] = []
    for judge in judges:
        if isinstance(judge, Mapping):
            norm = {str(k): judge[k] for k in sorted(judge, key=str)}
            if norm.get("mode") == "python" and isinstance(norm.get("body"), str):
                norm["body_source"] = _canon_dotted_spec(norm["body"])
            normalized.append(norm)
        else:
            normalized.append(judge)
    normalized.sort(key=lambda j: json.dumps(j, sort_keys=True, ensure_ascii=False, default=str))
    return normalized


def _canon_brief(brief_path: Path) -> str:
    """Canonical form of the proposer brief: line-ending + ws normalized.

    Normalizes line endings to ``\\n``, strips trailing whitespace per
    line, and strips leading/trailing blank lines. Whitespace-only edits
    (re-indenting, CRLF churn, trailing-newline changes) do not move the
    hash; editing the actual prose does.
    """
    if not brief_path.exists():
        log.warning(
            "contract: proposer-brief file %s is missing; hashing it as empty",
            brief_path,
        )
        return ""
    text = brief_path.read_text(encoding="utf-8")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    # Strip leading / trailing blank lines.
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def _canon_scoring(scoring_path: Path) -> str:
    """Canonical form of the scoring config: fully defaulted and key-sorted.

    Parses ``scoring.json`` into a fully-defaulted
    :class:`zicato.core.types.ScoringWeights` and serializes *that*,
    rather than the raw JSON. This matters for stability: the operator's
    live ``scoring.json`` is commonly a partial document (only the
    fields they care about), while the per-epoch frozen copy is the
    full serialized form. Routing both through ``ScoringWeights`` makes
    the two canonicalize identically, so an epoch's stored hash matches
    the hash re-derived from the live file.

    Keys are sorted. Equivalent JSON number spellings canonicalize to the same
    parsed Python value, while every distinct runtime value moves the hash.
    This is required for thresholds: even a small numeric change can alter a
    detector decision at its boundary.
    """
    if not scoring_path.exists():
        log.warning(
            "contract: scoring file %s is missing; hashing it as empty",
            scoring_path,
        )
        return ""
    return canonical_scoring_json(scoring_path.read_text(encoding="utf-8"))


def canonical_scoring_json(text: str) -> str:
    """Canonicalize captured scoring with the same rules as a contract file."""
    from zicato.workspace_loader import scoring_weights_from_dict  # noqa: PLC0415

    raw = json.loads(text)
    weights = scoring_weights_from_dict(raw)
    return json.dumps(scoring_contract_to_canon(weights), sort_keys=True)


def _canon_recorded_scoring(scoring_path: Path) -> str:
    """Retain recorded scoring identity while runtime decoding migrates its meaning.

    Frozen records containing the retired increment hashed both the increment
    and the unmodified threshold. Restore those values only in this read path;
    authored contracts must use the admitted schema.
    """
    from zicato.workspace_loader import historical_scoring_weights_from_dict  # noqa: PLC0415

    raw = json.loads(scoring_path.read_text(encoding="utf-8"))
    weights = historical_scoring_weights_from_dict(raw)
    canon = cast("dict[str, Any]", scoring_contract_to_canon(weights))
    overfitting = raw.get("overfitting")
    ladder = overfitting.get("ladder") if isinstance(overfitting, Mapping) else None
    if isinstance(ladder, Mapping) and "noise_scale" in ladder:
        target = canon["overfitting"]
        assert isinstance(target, dict)
        target = target["ladder"]
        assert isinstance(target, dict)
        threshold = ladder.get("threshold")
        target["threshold"] = None if threshold is None else float(threshold)
        target["noise_scale"] = float(ladder["noise_scale"])
    tournament = raw.get("tournament")
    params = tournament.get("params") if isinstance(tournament, Mapping) else None
    if isinstance(params, Mapping):
        # Historical strategy parameters hashed their supplied disabled tokens.
        canon["tournament_structure"]["params"].update(
            {key: params[key] for key in ("rating", "resolver") if key in params}
        )
    return json.dumps(canon, sort_keys=True)


#: ``ScoringWeights`` fields that carry a dotted-spec pointing at an operator
#: GRADING plugin (resolved by the shared importer). The canonicalizer expands
#: each into ``{"spec": ..., "source_sha256": ...}`` via
#: :func:`zicato.scoring.plugins.spec_with_source_hash` so editing the plugin
#: BODY rolls the epoch, and not only swapping the spec string does — the ONE
#: source-hashing mechanism every grading plugin (scoring + predicates + judges)
#: shares (issue #19 cross-cutting #1). An empty string expands to a null source
#: hash, so a contract with no plugin canonicalizes identically to before.
_SCORING_PLUGIN_SPEC_FIELDS: frozenset[str] = frozenset(
    {"scalar_fn", "drift_reducer", "outcome_summarizer_spec"}
)

#: ``ScoringWeights`` (+ nested config) fields OMITTED from the canonical
#: scoring dict when they hold their dataclass default — DERIVED from field
#: metadata at import time; no generated source is checked in.
_SCORING_OMIT_AT_DEFAULT_FIELDS: frozenset[str] = omit_at_default_fields()


def scoring_to_canon(weights: object) -> dict[str, object]:
    """Reduce a :class:`ScoringWeights` to a plain JSON-shaped dict.

    Every public field is included so the canonical form is complete
    and independent of which fields the operator spelled out in their
    ``scoring.json`` — EXCEPT the purely-additive, default-off fields in
    :data:`_SCORING_OMIT_AT_DEFAULT_FIELDS`, which are omitted while they hold
    their default so a contract that predates the field hashes identically (an
    opt-in field cannot retroactively roll every existing epoch). A
    non-default value reintroduces the key and rolls the epoch normally.

    The dotted-spec GRADING-plugin fields (:data:`_SCORING_PLUGIN_SPEC_FIELDS`)
    are NOT folded in as bare strings: each is expanded to
    ``{"spec": ..., "source_sha256": ...}`` so editing the resolved plugin's
    source rolls the contract hash (issue #19 cross-cutting #1). This shares the
    SAME mechanism the board predicates / judges use (see
    :func:`_canon_dotted_spec`).
    """
    from dataclasses import MISSING, fields, is_dataclass

    out: dict[str, object] = {}
    for f in fields(weights):  # type: ignore[arg-type]
        value = getattr(weights, f.name)
        if f.name in _SCORING_OMIT_AT_DEFAULT_FIELDS:
            # Omission is a persisted-format rule. An authored default may
            # change while the value omitted from archived identity stays fixed.
            if "canonical_default" in f.metadata:
                default_value: object = f.metadata["canonical_default"]
            elif f.default is not MISSING:
                default_value = f.default
            elif f.default_factory is not MISSING:
                default_value = f.default_factory()
            else:
                default_value = object()  # no default ⇒ never matches; always emit
            if value == default_value:
                continue
        if f.name in _SCORING_PLUGIN_SPEC_FIELDS:
            out[f.name] = _canon_dotted_spec(value if isinstance(value, str) else "")
        elif is_dataclass(value) and not isinstance(value, type):
            # A nested frozen dataclass field (e.g. the tournament
            # structure). Recurse so it canonicalizes structurally —
            # its `params` mapping is dict-ified, lists become lists —
            # rather than leaking an unserializable object into the
            # hash input. This is what folds the tournament structure
            # into the scoring contract automatically (§4 of the data
            # model design): switching structures or bumping a param
            # changes this canonical form and rolls the epoch.
            out[f.name] = scoring_to_canon(value)
        elif hasattr(value, "items"):
            out[f.name] = {k: _canon_value(v) for k, v in value.items()}
        elif isinstance(value, tuple):
            out[f.name] = [_canon_value(v) for v in value]
        else:
            out[f.name] = value
    return out


def scoring_contract_to_canon(weights: object) -> dict[str, object]:
    """Add system-owned evaluator identity to typed scoring configuration."""
    from dataclasses import fields

    out = cast("dict[str, Any]", scoring_to_canon(weights))
    features = getattr(weights, "experimental", None)
    if features is not None:
        values = out.pop("experimental", {})
        for declared in fields(features):
            path = declared.metadata.get("recorded_path")
            if path is None:
                if declared.name in values:
                    out.setdefault("experimental", {})[declared.name] = values[declared.name]
                continue
            if declared.name not in values and declared.name != "max_generations_per_contract":
                continue
            parts = path.split(".")
            if parts[0] == "tournament":
                parts[0] = "tournament_structure"
            target = out
            for part in parts[:-1]:
                target = target.setdefault(part, {})
            target[parts[-1]] = _canon_value(getattr(features, declared.name))
        # Editor grouping and retired settings do not change existing hash bytes.
        out["overfitting"]["ladder"]["noise_scale"] = 0.0
    if getattr(weights, "goldfive", None) is None:
        return out
    from zicato.integrations.goldfive import normalize_config  # noqa: PLC0415

    goldfive = out.get("goldfive")
    if not isinstance(goldfive, dict):  # pragma: no cover - structural guard
        raise TypeError("Goldfive scoring configuration did not canonicalize to an object")
    normalized = normalize_config(goldfive)
    identity = evaluation_implementation_identity(weights)
    normalized["implementation_identity"] = {
        "goldfive_version": identity["goldfive_version"],
        "zicato_integration_revision": identity["zicato_goldfive_integration_revision"],
    }
    out["goldfive"] = normalized
    return out


def evaluation_implementation_identity(weights: object) -> dict[str, str | int]:
    """Return the system implementations whose behavior is part of an epoch."""
    identity: dict[str, str | int] = {
        "zicato_evaluator_revision": ZICATO_EVALUATOR_REVISION,
    }
    if getattr(weights, "goldfive", None) is None:
        return identity
    from zicato.integrations.goldfive import (  # noqa: PLC0415
        GOLDFIVE_IMPLEMENTATION_VERSION,
        ZICATO_GOLDFIVE_INTEGRATION_REVISION,
    )

    identity.update(
        goldfive_version=GOLDFIVE_IMPLEMENTATION_VERSION,
        zicato_goldfive_integration_revision=ZICATO_GOLDFIVE_INTEGRATION_REVISION,
    )
    return identity


def _canon_value(value: object) -> object:
    """Canonicalize a single mapping/sequence value for the scoring hash.

    Used for the values inside a nested mapping (e.g. the tournament
    ``params`` object, which may itself carry nested dicts / lists such
    as ``racing.rungs``). Plain scalars pass through; nested mappings and
    sequences are normalised recursively so the canonical form is stable
    and JSON-serializable.
    """
    if isinstance(value, Mapping):
        return {k: _canon_value(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_canon_value(v) for v in value]
    return value


def _canon_adapter(inputs: ContractInputs) -> str:
    """Canonical adapter reconstruction identity plus implementation source.

    Three pieces make up the effective harness identity:

    * ``worker_spec`` — the validated document a tournament worker rebuilds
      the adapter from, with ``mutable_trees`` removed (they carry their own
      contract component) and integration names sorted;
    * ``implementation_sources`` — a source hash per dotted implementation
      that drives the adapter and sits outside the mutable trees;
    * ``declaration`` — whatever the operator's declared ``adapter`` block
      states that the worker document does not (see
      :func:`_canon_adapter_declaration`).
    """
    if inputs.adapter_spec is None:
        spec = _canon_adapter_document({"kind": "adk", "entrypoint": inputs.entrypoint})
    else:
        spec = _canon_adapter_document(inputs.adapter_spec)

    source_specs = inputs.adapter_source_specs or (
        (inputs.entrypoint,) if inputs.entrypoint else ()
    )
    sources = [
        _canon_dotted_spec(dotted)
        for dotted in dict.fromkeys(source_specs)
        if not _dotted_spec_is_within_mutable_trees(dotted, inputs.mutable_trees)
    ]
    canon: dict[str, object] = {"worker_spec": spec, "implementation_sources": sources}
    if inputs.execution_roles is not None:
        roles = json.loads(inputs.execution_roles)
        if roles:
            canon["execution_roles"] = roles
            implementations = []
            for _role, document in sorted(roles.items()):
                dotted = document["models_role"].get("call_llm")
                if dotted and not _dotted_spec_is_within_mutable_trees(
                    dotted, inputs.mutable_trees
                ):
                    implementations.append(_canon_dotted_spec(dotted))
                for name in ("model_factory", "client_factory"):
                    dotted = document.get("transport", {}).get(name)
                    if dotted:
                        implementations.append(_canon_dotted_spec(dotted))
            canon["execution_sources"] = implementations
    if inputs.adapter_declaration is not None:
        declared = _canon_adapter_document(inputs.adapter_declaration)
        _require_declared_factory_source(declared, sources)
        declaration = _canon_adapter_declaration(declared, spec)
        if declaration:
            canon["declaration"] = declaration
    return json.dumps(canon, sort_keys=True, separators=(",", ":"))


def _canon_adapter_document(document: Mapping[str, Any]) -> dict[str, object]:
    """Normalize one adapter document — a worker spec or a declared block.

    Values are normalized recursively and the object keys sort at
    serialization, so key order and equivalent JSON number spellings are
    no-ops. ``mutable_trees`` is dropped because the declared source roots
    are their own contract component, and integration names are sorted so
    their declaration order is a no-op as well.
    """
    canon = {str(key): _canon_value(value) for key, value in document.items()}
    for key in ("mutable_trees", "import_roots", "stock_grading_confirmed"):
        canon.pop(key, None)
    for key, default in (("entrypoint", None), ("factory", None), ("args", []), ("options", {})):
        if canon.get(key) == default:
            canon.pop(key, None)
    integrations = canon.get("integrations")
    if isinstance(integrations, list) and all(isinstance(name, str) for name in integrations):
        canon["integrations"] = sorted(integrations)
    return canon


def _canon_adapter_declaration(
    declared: Mapping[str, object],
    worker_spec: Mapping[str, object],
) -> dict[str, object]:
    """Return what the declared adapter block states that the worker document does not.

    ``worker_spec()`` belongs to the adapter, so an adapter built by operator
    code decides for itself how much of its construction to report. One that
    reports a constant document leaves the contract blind to the factory path
    and constructor arguments it was actually built from, and an operator
    could then swap either — a different harness — with the contract
    standing. Every declared field the worker document does not already carry
    is therefore hashed in its own right.

    A field the worker document repeats verbatim is dropped, because a change
    to it moves the worker document anyway. That keeps this an addition
    rather than a re-hash: an adapter whose worker document mirrors what the
    operator declared, which includes every ADK registration, contributes an
    empty residue and keeps the adapter component it already had.
    """
    return {
        key: value
        for key, value in declared.items()
        if key not in worker_spec or worker_spec[key] != value
    }


def _require_declared_factory_source(
    declaration: Mapping[str, object],
    sources: list[dict[str, object]],
) -> None:
    """Refuse to hash a declared factory whose implementation is unreadable.

    The declared dotted path is only a name. Two workspaces naming the same
    factory over different implementations of it are running different
    harnesses, so the contract records a hash of the defining module's
    source alongside the name. When that hash is unavailable — the module
    does not import from here, or has no inspectable source — the adapter
    component would silently carry the name alone and let an implementation
    change pass as the same contract. Raise instead, so the operator fixes
    the workspace rather than accumulating generations that are not
    comparable.

    A factory inside the registered mutable trees is exempt: that source is
    generation content Zicato itself rewrites, so it is excluded from
    ``sources`` upstream and there is no entry to check.
    """
    factory = declaration.get("factory")
    if not isinstance(factory, str) or not factory:
        return
    for source in sources:
        if source.get("spec") != factory:
            continue
        if source.get("source_sha256") is None:
            raise ValueError(
                f"adapter factory {factory!r} could not be resolved to hashable "
                "source, so the evaluation contract cannot record which harness "
                "implementation it names. Make the factory's module importable "
                "from the workspace before evolving."
            )
        return


def _dotted_spec_is_within_mutable_trees(
    dotted: str,
    mutable_trees: tuple[str, ...],
) -> bool:
    """Whether a dotted implementation belongs to generation source."""
    import importlib.util

    module_name = dotted.partition(":")[0].strip()
    if not module_name:
        return False
    try:
        found = importlib.util.find_spec(module_name)
    except (ImportError, ModuleNotFoundError, ValueError):
        return False
    if found is None or found.origin is None:
        return False
    origin = Path(found.origin).resolve()
    for raw_tree in mutable_trees:
        tree = Path(raw_tree).resolve()
        if origin == tree or (tree.is_dir() and origin.is_relative_to(tree)):
            return True
    return False


def _canon_evaluator_revision() -> str:
    """Return the explicit revision of Zicato's measurement semantics."""
    return str(ZICATO_EVALUATOR_REVISION)


def _canon_mutable_trees(mutable_trees: tuple[str, ...]) -> str:
    """Canonical form of the mutable trees: sorted normalized path strings.

    The identity being hashed is *which subtrees of the target are mutable* — a
    property of the registration rather than of where the checkout happens to
    live. Paths are normalized (``.``/``..`` segments and separators collapsed,
    POSIX-rendered) but NEVER resolved against the filesystem: resolving folded
    the process cwd and the absolute checkout path into the hash, so the same
    workspace hashed differently when run from a different directory — or after
    being moved — and spuriously rolled its epoch. Registration order does not
    move the hash (sorted); adding or removing a tree does.
    """
    normalized = sorted(PurePosixPath(os.path.normpath(p)).as_posix() for p in mutable_trees)
    return "\n".join(normalized)


def _canon_proposer(
    proposer_path: Path | None,
    external: ExternalProposerConfig | None = None,
    static_checks: tuple[str, ...] = (),
    *,
    proposer_spec: ProposerSpec | None = None,
) -> str:
    """Canonical form of the proposer: agent identity + skills + tools.

    Resolves the proposer dir (or ``None`` ⇒ the built-in default) to a
    :class:`zicato.core.types.ProposerSpec` via
    :func:`zicato.proposer.skills.resolve_proposer_spec`, then reduces it
    to a sorted-key JSON string:

    * ``agent_id`` — ``"builtin:default"`` or ``"dir:<name>"``;
    * ``tools`` — the tool names, sorted;
    * ``skills`` — ``[{"name": ..., "sha256": <hash of the normalized
      body>}]``, sorted by name. Skill bodies are normalized exactly like
      the proposer brief, so a whitespace-only skill edit does not move the
      hash; a semantic edit (or adding / removing / renaming a skill) does;
    * ``validate_static_checks`` — the sorted tier-2 static-check names
      from ``static_checks``, present ONLY when non-empty. Declaring or
      changing the set the proposer must satisfy before it will emit a
      patch rolls the epoch; the empty default omits the key entirely, so
      every workspace that never configures it hashes byte-identically to
      before the field existed.

    An ``external`` proposer (``runtime.proposer_agent``) adds ONE more
    key, ``external``, carrying the dotted path plus the digest of the
    agent's causal surface (:mod:`zicato.proposer.external`). The key is
    added only when an external proposer is configured, so every workspace
    that configures none canonicalizes byte-identically to before this
    seam existed — and its contract hash does not move.

    The built-in default produces a stable canonical string, so a
    workspace that never configures a proposer keeps a stable hash.
    """
    from zicato.proposer.skills import (  # noqa: PLC0415
        normalize_skill_body,
        resolve_proposer_spec,
    )

    spec = proposer_spec or resolve_proposer_spec(proposer_path, external)
    skills = sorted(
        (
            {"name": skill.name, "sha256": _sha(normalize_skill_body(skill.body))}
            for skill in spec.skills
        ),
        key=lambda s: s["name"],
    )
    canon: dict[str, object] = {
        "agent_id": spec.agent_id,
        "tools": sorted(spec.tools),
        "skills": skills,
    }
    if spec.external_path is not None:
        canon["external"] = {
            "path": spec.external_path,
            "identity_sha256": spec.external_identity_sha256,
        }
    # Omit-at-default: the key is absent for every workspace that declares
    # no static checks, so their canonical string — and hash — is unchanged.
    if static_checks:
        canon["validate_static_checks"] = sorted(static_checks)
    return json.dumps(canon, sort_keys=True, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


def compute_contract_hash(
    inputs: ContractInputs, *, proposer_spec: ProposerSpec | None = None
) -> str:
    """Hash captured inputs within their declared driver import locations."""
    with driver_import_scope(inputs.driver_imports):
        return _compute_contract_hash(inputs, proposer_spec=proposer_spec)


def _compute_contract_hash(
    inputs: ContractInputs, *, proposer_spec: ProposerSpec | None = None
) -> str:
    """Return the ``sha256`` hex digest of the canonicalized contract.

    Canonicalization (so spurious edits don't roll the epoch):

    * **board** — :func:`zicato.board.jsonl.load_board`, sort entries by
      id, serialize each to a sorted-key JSON dict, join. The board-level
      ``judges`` and ``disable_drift`` are folded in too. Semantic
      content only.
    * **brief** — read text, normalize line endings to ``\\n``, strip
      trailing whitespace per line, strip leading/trailing blank lines.
    * **scoring** — parse through the fully defaulted typed configuration and
      ``json.dumps(sort_keys=True)`` without discarding runtime precision.
    * **evaluator revision** — the explicit revision of Zicato's measurement
      and tournament-decision semantics.
    * **adapter** — the validated worker reconstruction spec, source hashes
      for the adapter factory or ADK harness module, and whatever the
      operator's declared adapter block states that the spec does not.
    * **mutable_trees** — sorted tuple of NORMALIZED path strings
      (`os.path.normpath` + POSIX; never filesystem-resolved, so the
      hash does not depend on the process cwd or checkout — bug #10).
    * **proposer** — the resolved :class:`ProposerSpec` (agent id, sorted
      tools, per-skill normalized-body hashes sorted by name, custom
      ``agent.py`` source hash), serialized sorted-key.

    The canonical forms are concatenated with a NUL-delimited
    separator and hashed. Missing files are treated as the empty string
    for that component (so a board-less workspace still hashes
    deterministically) — a warning is logged when that happens.
    """
    return _hash_contract(inputs, _canon_scoring(inputs.scoring_path), proposer_spec)


def compute_recorded_contract_hash(
    inputs: ContractInputs, *, proposer_spec: ProposerSpec | None = None
) -> str:
    """Hash saved settings through the same stable representation as authored settings."""
    return _hash_contract(inputs, _canon_recorded_scoring(inputs.scoring_path), proposer_spec)


def _hash_contract(inputs: ContractInputs, scoring: str, proposer_spec: ProposerSpec | None) -> str:
    components = [
        _canon_board(inputs.board_path),
        _canon_brief(inputs.brief_path),
        scoring,
        _canon_evaluator_revision(),
        _canon_adapter(inputs),
        _canon_mutable_trees(
            inputs.mutable_tree_identities
            if inputs.mutable_tree_identities is not None
            else inputs.mutable_trees
        ),
        _canon_proposer(
            inputs.proposer_path,
            inputs.external_proposer,
            inputs.proposer_static_checks,
            proposer_spec=proposer_spec,
        ),
    ]
    joined = _SEP.join(components)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def compute_component_hashes(
    inputs: ContractInputs, *, proposer_spec: ProposerSpec | None = None
) -> dict[str, str]:
    """Hash captured inputs within their declared driver import locations."""
    with driver_import_scope(inputs.driver_imports):
        return _compute_component_hashes(inputs, proposer_spec=proposer_spec)


def _compute_component_hashes(
    inputs: ContractInputs, *, proposer_spec: ProposerSpec | None = None
) -> dict[str, str]:
    """Return a per-component ``sha256`` hex digest.

    Used by the auto-roll path to report *which* contract component
    changed. The keys are ``"board"``, ``"brief"``, ``"scoring"``,
    ``"evaluator_revision"``, ``"adapter"``, ``"mutable_trees"``, and
    ``"proposer"``.
    """
    return {
        "board": _sha(_canon_board(inputs.board_path)),
        "brief": _sha(_canon_brief(inputs.brief_path)),
        "scoring": _sha(_canon_scoring(inputs.scoring_path)),
        "evaluator_revision": _sha(_canon_evaluator_revision()),
        "adapter": _sha(_canon_adapter(inputs)),
        "mutable_trees": _sha(
            _canon_mutable_trees(
                inputs.mutable_tree_identities
                if inputs.mutable_tree_identities is not None
                else inputs.mutable_trees
            )
        ),
        "proposer": _sha(
            _canon_proposer(
                inputs.proposer_path,
                inputs.external_proposer,
                inputs.proposer_static_checks,
                proposer_spec=proposer_spec,
            )
        ),
    }


def compute_proposer_hash(inputs: ContractInputs) -> str:
    """Fingerprint the resolved proposer with the evaluation contract's canonicalizer."""
    return _sha(
        _canon_proposer(
            inputs.proposer_path, inputs.external_proposer, inputs.proposer_static_checks
        )
    )


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@with_workspace_imports
def resolve_contract_inputs(
    workspace_root: Path,
    *,
    workspace_config: Mapping[str, Any] | None = None,
    execution_roles: bytes | None = None,
) -> ContractInputs:
    """Resolve the contract inputs for a workspace from ``config.json``.

    Reads ``{workspace_root}/config.json``, then resolves:

    * ``contract.board_path`` / ``contract.brief_path`` /
      ``contract.scoring_path`` — the canonical contract source paths
      recorded by ``zicato epoch register``. The proposer-brief path is also
      accepted under the older ``contract.rubric_path`` key, so a workspace
      registered under that name keeps resolving. When the
      ``contract`` key is absent (a workspace registered before
      auto-epoching landed) the default convention is used:
      ``<workspace_root>/board.jsonl``, ``brief.md``, ``scoring.json``
      relative to the workspace root's parent (the operator's working
      directory).
    * ``adk_entrypoint`` — the registered adapter entrypoint.
    * ``mutable_trees`` — the registered source roots.
    * ``runtime.proposer_agent`` — the optional external proposer
      (:func:`zicato.proposer.external.external_proposer_config`).

    Raises
    ------
    FileNotFoundError
        When ``config.json`` is missing. The message suggests running
        ``zicato epoch register``.
    """
    from zicato.workspace.contract_publication import (
        assert_contract_publication_complete,  # noqa: PLC0415
    )

    assert_contract_publication_complete(workspace_root)
    remedy = "run `zicato epoch register` to record the evaluation contract before evolving"
    config = (
        read_workspace_config(workspace_root).require(remedy).raw
        if workspace_config is None
        else workspace_config
    )
    contract = config.get("contract", {})
    if not isinstance(contract, Mapping):
        raise ValueError("workspace contract must be an object")
    board_path = Path(
        contract.get("board_path") or _default_contract_path(workspace_root, "board.jsonl")
    )
    # ``brief_path`` is the current key; ``rubric_path`` is the older name,
    # still read so a workspace registered under it resolves.
    brief_path = Path(
        contract.get("brief_path")
        or contract.get("rubric_path")
        or _default_contract_path(workspace_root, "brief.md")
    )
    scoring_path = Path(
        contract.get("scoring_path") or _default_contract_path(workspace_root, "scoring.json")
    )

    from zicato.adapter_factory import make_adapter_from_config  # noqa: PLC0415
    from zicato.tournament.worker_transport import adapter_worker_spec  # noqa: PLC0415

    adapter_block = config.get("adapter")
    has_adapter = isinstance(adapter_block, Mapping) or bool(config.get("adk_entrypoint"))
    worker_spec: dict[str, Any] | None
    if has_adapter:
        adapter = make_adapter_from_config(config, workspace_root=workspace_root)
        adapter_block = adapter_declaration(config).document()
        worker_spec = adapter_worker_spec(adapter)
    else:
        worker_spec = None
    entrypoint = str((worker_spec or {}).get("entrypoint") or config.get("adk_entrypoint", ""))
    mutable_trees = tuple(str(tree) for tree in registered_mutable_trees(config, workspace_root))
    source_specs: list[str] = []
    if isinstance(adapter_block, Mapping) and isinstance(adapter_block.get("factory"), str):
        source_specs.append(str(adapter_block["factory"]))
    worker_factory = (worker_spec or {}).get("factory")
    if isinstance(worker_factory, str):
        source_specs.append(worker_factory)
    if entrypoint:
        source_specs.append(entrypoint)

    # ``contract.proposer_path`` is optional — absent ⇒ the built-in
    # default proposer (``None``). Relative spellings are resolved like
    # the other contract paths, against the operator's project root (the
    # workspace's parent).
    raw_proposer = contract.get("proposer_path")
    proposer_path: Path | None
    if raw_proposer:
        proposer_path = Path(raw_proposer)
        if not proposer_path.is_absolute():
            proposer_path = (workspace_root.parent / proposer_path).resolve()
    else:
        proposer_path = None

    # ``runtime.proposer_agent`` is optional — absent ⇒ no external
    # proposer and a canonical form byte-identical to before this seam.
    from zicato.models_config import capture_execution_roles  # noqa: PLC0415
    from zicato.proposer.external import external_proposer_config  # noqa: PLC0415

    # ``contract.proposer_static_checks`` is read through the validator's
    # own resolver so the reader that HASHES the set and the reader that
    # RUNS it can never disagree about which names are declared.
    from zicato.proposer.validate import declared_static_checks  # noqa: PLC0415

    return ContractInputs(
        board_path=board_path,
        brief_path=brief_path,
        scoring_path=scoring_path,
        entrypoint=entrypoint,
        mutable_trees=mutable_trees,
        mutable_tree_identities=tuple(
            str(tree)
            for tree in (
                adapter_block["mutable_trees"]
                if isinstance(adapter_block, Mapping) and "mutable_trees" in adapter_block
                else config.get("mutable_trees") or config.get("source_roots") or ()
            )
        ),
        adapter_spec=worker_spec,
        driver_imports=DriverImportContext.from_config(config, workspace_root),
        adapter_source_specs=tuple(dict.fromkeys(source_specs)),
        adapter_declaration=adapter_block if isinstance(adapter_block, Mapping) else None,
        proposer_path=proposer_path,
        external_proposer=external_proposer_config(config, workspace_root),
        proposer_static_checks=declared_static_checks(workspace_root, workspace_config=config),
        execution_roles=(
            capture_execution_roles(config) if execution_roles is None else execution_roles
        ),
    )


def default_contract_paths(workspace_root: Path) -> dict[str, Path | None]:
    """Return the default canonical contract source paths for a workspace.

    The convention is ``<workspace_root_parent>/board.jsonl``,
    ``brief.md``, ``scoring.json`` — the operator's live, editable
    copies sitting alongside the ``.zicato/`` directory. ``zicato
    register`` records these in ``config.json`` so subsequent commands
    do not have to re-derive them.

    The proposer-brief default is returned under both ``brief_path``
    (the current key) and ``rubric_path`` (the older alias) so a caller
    reading either key resolves.

    The ``proposer_path`` default is ``None`` — no proposer dir, i.e. the
    built-in default proposer. A workspace opts into a proposer dir by
    setting ``contract.proposer_path`` explicitly.
    """
    brief_default = Path(_default_contract_path(workspace_root, "brief.md"))
    return {
        "board_path": Path(_default_contract_path(workspace_root, "board.jsonl")),
        "brief_path": brief_default,
        "rubric_path": brief_default,
        "scoring_path": Path(_default_contract_path(workspace_root, "scoring.json")),
        "proposer_path": None,
    }


def _default_contract_path(workspace_root: Path, filename: str) -> str:
    """The conventional location of a contract file for a workspace.

    The convention is *next to* the ``.zicato/`` directory — i.e. in
    the operator's project root — not inside the workspace, so the
    operator's live copies are not confused with the per-epoch frozen
    copies under ``epochs/{id}/``.
    """
    return str((workspace_root.parent / filename).resolve())


__all__ = [
    "ContractInputs",
    "compute_contract_hash",
    "compute_component_hashes",
    "evaluation_implementation_identity",
    "resolve_contract_inputs",
    "default_contract_paths",
    "scoring_contract_to_canon",
    "scoring_to_canon",
]
