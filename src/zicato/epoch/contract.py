"""Contract hashing for auto-epoching.

An epoch is the unit of *evaluation contract*. Five things make up that
contract:

1. The board — test inputs + expectations + judges (``board.jsonl``).
2. The proposer brief — operator steering text (``brief.md``).
3. The scoring — weights + gate thresholds (``scoring.json``).
4. The registered inner-harness IDENTITY — the ``--adk`` entrypoint
   string plus the sorted list of ``--mutable-tree`` paths.
5. The proposer — the agent identity, its tools, and the skill modules
   under a configured ``proposers/<name>/`` dir (or the built-in default
   proposer when none is configured).

A change to any of these means generations on either side of the change
are no longer directly comparable, so the epoch must roll. The inner
harness's *source content* is deliberately NOT part of the contract —
that is exactly what zicato mutates within an epoch.

This module reduces the contract components to a single
``sha256`` hex digest. The hash is *canonicalized* so spurious edits
(whitespace, board-entry reordering, float-formatting noise) do not
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
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("zicato.epoch.contract")

#: Separator between the canonical component forms before hashing.
#: Chosen to be a byte sequence that cannot appear in any canonical
#: component (a NUL plus a marker word).
_SEP = "\x00--zicato-contract-component--\x00"


@dataclass(frozen=True, slots=True)
class ContractInputs:
    """The four contract components, resolved to concrete inputs.

    Fields
    ------
    board_path:
        Filesystem path to the live ``board.jsonl``.
    brief_path:
        Filesystem path to the live proposer brief (``brief.md``).
    scoring_path:
        Filesystem path to the live ``scoring.json``.
    entrypoint:
        The registered ``--adk`` entrypoint string, verbatim.
    mutable_trees:
        The registered ``--mutable-tree`` paths. Stored as a tuple of
        strings; :func:`compute_contract_hash` sorts and absolutises
        them so order and relative/absolute spelling do not matter.
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
    #: Location of the proposer dir (``proposers/<name>/``) frozen for
    #: the epoch, or ``None`` for the built-in default proposer. ``None``
    #: by default so existing construction sites keep working.
    proposer_path: Path | None = None


# ---------------------------------------------------------------------------
# Per-component canonicalization
# ---------------------------------------------------------------------------


def _canon_board(board_path: Path) -> str:
    """Canonical form of the board: semantic content only, id-sorted.

    Loads the board through :func:`zicato.board.jsonl.load_board` so the
    canonical form is the validated, parsed shape — not the raw bytes.
    Entries are sorted by id and each is serialized to a sorted-key JSON
    dict. Reordering rows or reformatting the JSONL leaves the hash
    unchanged; editing an entry's input/expectation/weight changes it.

    Beyond the per-entry rows the board also carries two *board-level*
    pieces of contract: the configured ``judges`` and the board-level
    ``disable_drift`` flag. Both are canonicalized here so swapping a
    judge — or toggling drift scoring off — correctly rolls the epoch.
    They are read defensively (see :func:`_canon_board_meta`) so a board
    that predates those fields still hashes deterministically.
    """
    if not board_path.exists():
        log.warning(
            "contract: board file %s is missing; hashing it as empty",
            board_path,
        )
        return ""
    from zicato.board.jsonl import _entry_to_dict, load_board  # noqa: PLC0415

    entries = load_board(board_path)
    canon_entries = [
        json.dumps(
            _fold_entry_grading_source(_entry_to_dict(entry)),
            sort_keys=True,
            ensure_ascii=False,
        )
        for entry in sorted(entries, key=lambda e: e.id)
    ]
    meta = _canon_board_meta(board_path)
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
    no plugin canonicalizes byte-for-byte as before. Operates on a copy of the
    nested dicts so the round-trip serializer (``_entry_to_dict``) is unaffected.
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


def _canon_board_meta(board_path: Path) -> str:
    """Canonical form of the board-level ``judges`` + ``disable_drift``.

    The board carries two pieces of contract beyond its entry rows: the
    list of configured judges and a board-level ``disable_drift`` flag
    (both introduced alongside multi-judge scoring). This helper reduces
    them to a stable, sorted-key JSON string.

    The board-level fields are resolved defensively — the loader API for
    them is owned by :mod:`zicato.board` and is reconciled at
    integration time:

    * If :mod:`zicato.board.jsonl` exposes a ``load_board_meta`` callable
      it is used directly.
    * Otherwise the raw JSONL is scanned for a board-level object (a line
      that carries ``judges`` / ``disable_drift`` but no entry ``id``).
    * A board with neither canonicalizes to the empty-meta form, so
      boards written before these fields existed keep a stable hash.
    """
    judges: object = []
    disable_drift = False
    judge_only = False

    from zicato.board import jsonl as _board_jsonl  # noqa: PLC0415

    loader = getattr(_board_jsonl, "load_board_meta", None)
    if callable(loader):
        try:
            meta = loader(board_path)
        except Exception:  # noqa: BLE001 — defensive: board API may evolve
            meta = None
        if meta is not None:
            judges = _meta_get(meta, "judges", [])
            disable_drift = bool(_meta_get(meta, "disable_drift", False))
            judge_only = bool(_meta_get(meta, "judge_only", False))
    else:
        judges, disable_drift, judge_only = _scan_raw_board_meta(board_path)

    canon: dict[str, object] = {
        "judges": _canon_judges(judges),
        "disable_drift": bool(disable_drift),
    }
    # ``judge_only`` is folded into the contract hash so flipping it opens
    # a new epoch. It is added ONLY when True so a board that never set it
    # — every board written before the flag existed — hashes byte-for-byte
    # identically to before, keeping stored epoch hashes stable.
    if judge_only:
        canon["judge_only"] = True
    return json.dumps(canon, sort_keys=True, ensure_ascii=False)


def _meta_get(meta: object, key: str, default: object) -> object:
    """Read ``key`` off a board-meta object that may be a dict or struct."""
    if isinstance(meta, Mapping):
        return meta.get(key, default)
    return getattr(meta, key, default)


def _scan_raw_board_meta(board_path: Path) -> tuple[object, bool, bool]:
    """Best-effort scan for a board-level metadata object in raw JSONL.

    A board-level object is a JSON line carrying ``judges`` and/or
    ``disable_drift`` / ``judge_only`` but no entry ``id`` (entry rows
    always have one). Returns ``([], False, False)`` when no such line
    exists.
    """
    judges: object = []
    disable_drift = False
    judge_only = False
    try:
        text = board_path.read_text(encoding="utf-8")
    except OSError:
        return judges, disable_drift, judge_only
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict) or "id" in payload:
            continue
        if "judges" in payload:
            judges = payload["judges"]
        if "disable_drift" in payload:
            disable_drift = bool(payload["disable_drift"])
        if "judge_only" in payload:
            judge_only = bool(payload["judge_only"])
    return judges, disable_drift, judge_only


def _canon_dotted_spec(spec: str) -> dict[str, object]:
    """Canonical form of ONE operator-grading dotted spec, with source hash.

    The single source-hashing mechanism every grading plugin shares (issue #19
    cross-cutting #1): expands a dotted spec into
    ``{"spec": <dotted>, "source_sha256": <hash-or-null>}`` via
    :func:`zicato.scoring.plugins.spec_with_source_hash`, so editing the
    resolved plugin's BODY rolls the contract hash, not only swapping the spec
    string. Applied uniformly to the scoring ``scalar_fn`` / ``drift_reducer`` /
    ``outcome_summarizer_spec`` AND the board predicates / judges.

    An empty / non-string spec expands to ``{"spec": "", "source_sha256":
    null}`` — byte-identical to "no plugin" — so a board / contract that names
    no plugin canonicalizes exactly as before this alignment.
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
    non-python judge (inline criterion) is hashed verbatim as before.
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
    """Canonical form of the scoring config: float-rounded, key-sorted.

    Parses ``scoring.json`` into a fully-defaulted
    :class:`zicato.core.types.ScoringWeights` and serializes *that*,
    rather than the raw JSON. This matters for stability: the operator's
    live ``scoring.json`` is commonly a partial document (only the
    fields they care about), while the per-epoch frozen copy is the
    full serialized form. Routing both through ``ScoringWeights`` makes
    the two canonicalize identically, so an epoch's stored hash matches
    the hash re-derived from the live file.

    Every float is rounded to 6 decimal places (so ``0.1`` and
    ``0.10000000001`` collapse) and keys are sorted. Reformatting the
    JSON or float-precision noise does not move the hash; changing a
    weight does.
    """
    if not scoring_path.exists():
        log.warning(
            "contract: scoring file %s is missing; hashing it as empty",
            scoring_path,
        )
        return ""
    from zicato.workspace_loader import _scoring_weights_from_dict  # noqa: PLC0415

    raw = json.loads(scoring_path.read_text(encoding="utf-8"))
    weights = _scoring_weights_from_dict(raw)
    return json.dumps(_round_floats(_scoring_to_canon(weights)), sort_keys=True)


#: ``ScoringWeights`` fields that carry a dotted-spec pointing at an operator
#: GRADING plugin (resolved by the shared importer). The canonicalizer expands
#: each into ``{"spec": ..., "source_sha256": ...}`` via
#: :func:`zicato.scoring.plugins.spec_with_source_hash` so editing the plugin
#: BODY rolls the epoch, not only swapping the spec string — the ONE
#: source-hashing mechanism every grading plugin (scoring + predicates + judges)
#: shares (issue #19 cross-cutting #1). An empty string expands to a null source
#: hash, so a contract with no plugin canonicalizes identically to before.
_SCORING_PLUGIN_SPEC_FIELDS: frozenset[str] = frozenset(
    {"scalar_fn", "drift_reducer", "outcome_summarizer_spec"}
)

#: ``ScoringWeights`` fields that are OMITTED from the canonical scoring dict
#: when they hold their dataclass default. A field listed here was added AFTER
#: the parity goldens were captured; emitting it unconditionally would inject a
#: new key into the scoring hash and roll EVERY existing epoch (and turn the
#: CONTRACT-HASH parity gate red) the moment the field exists. Omitting it at
#: the default keeps an unset contract byte-identical to one that predates the
#: field, while a NON-default value still appears in the canonical form and
#: rolls the epoch — exactly like any other weight change. Only purely-additive,
#: default-off fields belong here.
_SCORING_OMIT_AT_DEFAULT_FIELDS: frozenset[str] = frozenset(
    {
        "diff_complexity_weight",
        # Opt-in cross-epoch experiment memory (EXPERIMENT-MEMORY.md §3.4):
        # the nested ``ExperimentMemoryConfig`` compares by value against
        # its ``default_factory()`` instance, so an all-default block is
        # omitted (no retroactive roll) while any opt-in rolls the epoch.
        "experiment_memory",
        # Opt-in random-baseline (placebo) challenger cadence
        # (OVERFITTING.md #7). Lives on the nested ``OverfittingConfig`` —
        # the canonicalizer recurses through this same field-name set, so
        # the nested field is omitted at its 0 default (no retroactive
        # roll) and a non-zero cadence rolls the epoch.
        "random_baseline_every_n",
        # Opt-in integrity BLOCKING modes (default OFF — alarm-only parity
        # with the supervisor's notary). Omitted at their False default so
        # existing epochs never roll; opting either on selects champions
        # under a stricter rule and rolls the epoch, which is correct.
        "block_on_containment_violation",
        "block_on_gate_contradiction",
    }
)


def _scoring_to_canon(weights: object) -> dict[str, object]:
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
            # Resolve the field's default (plain default, or default_factory)
            # and skip the key entirely while the value matches it, so the
            # canonical form is byte-identical to a pre-field contract.
            if f.default is not MISSING:
                default_value: object = f.default
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
            out[f.name] = _scoring_to_canon(value)
        elif hasattr(value, "items"):
            out[f.name] = {k: _canon_value(v) for k, v in value.items()}
        elif isinstance(value, tuple):
            out[f.name] = [_canon_value(v) for v in value]
        else:
            out[f.name] = value
    return out


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


def _round_floats(value: object) -> object:
    """Recursively round every float in a JSON-shaped value to 6 dp."""
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, dict):
        return {k: _round_floats(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_round_floats(v) for v in value]
    return value


def _canon_entrypoint(entrypoint: str) -> str:
    """Canonical form of the entrypoint: the string verbatim."""
    return entrypoint


def _canon_mutable_trees(mutable_trees: tuple[str, ...]) -> str:
    """Canonical form of the mutable trees: sorted absolute path strings.

    Each path is resolved to an absolute string and the result is
    sorted, so the registration order and any relative/absolute spelling
    differences do not move the hash. Adding or removing a tree does.
    """
    resolved = sorted(str(Path(p).resolve()) for p in mutable_trees)
    return "\n".join(resolved)


def _canon_proposer(proposer_path: Path | None) -> str:
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
    * ``agent_source_sha256`` — SHA-256 of a custom ``agent.py`` (or
      ``null``), so editing the custom agent rolls the epoch.

    The built-in default produces a stable canonical string, so a
    workspace that never configures a proposer keeps a stable hash.
    """
    from zicato.proposer.skills import (  # noqa: PLC0415
        normalize_skill_body,
        resolve_proposer_spec,
    )

    spec = resolve_proposer_spec(proposer_path)
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
        "agent_source_sha256": spec.agent_source_sha256,
    }
    return json.dumps(canon, sort_keys=True, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


def compute_contract_hash(inputs: ContractInputs) -> str:
    """Return the ``sha256`` hex digest of the canonicalized contract.

    Canonicalization (so spurious edits don't roll the epoch):

    * **board** — :func:`zicato.board.jsonl.load_board`, sort entries by
      id, serialize each to a sorted-key JSON dict, join. The board-level
      ``judges`` and ``disable_drift`` are folded in too. Semantic
      content only.
    * **brief** — read text, normalize line endings to ``\\n``, strip
      trailing whitespace per line, strip leading/trailing blank lines.
    * **scoring** — ``json.load``, round every float to 6 decimal
      places, ``json.dumps(sort_keys=True)``.
    * **entrypoint** — the string verbatim.
    * **mutable_trees** — sorted tuple of absolute path strings.
    * **proposer** — the resolved :class:`ProposerSpec` (agent id, sorted
      tools, per-skill normalized-body hashes sorted by name, custom
      ``agent.py`` source hash), serialized sorted-key.

    The canonical forms are concatenated with a NUL-delimited
    separator and hashed. Missing files are treated as the empty string
    for that component (so a board-less workspace still hashes
    deterministically) — a warning is logged when that happens.
    """
    components = [
        _canon_board(inputs.board_path),
        _canon_brief(inputs.brief_path),
        _canon_scoring(inputs.scoring_path),
        _canon_entrypoint(inputs.entrypoint),
        _canon_mutable_trees(inputs.mutable_trees),
        _canon_proposer(inputs.proposer_path),
    ]
    joined = _SEP.join(components)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def compute_component_hashes(inputs: ContractInputs) -> dict[str, str]:
    """Return a per-component ``sha256`` hex digest.

    Used by the auto-roll path to report *which* contract component
    changed. The keys are ``"board"``, ``"brief"``, ``"scoring"``,
    ``"entrypoint"``, ``"mutable_trees"``, ``"proposer"``.
    """
    return {
        "board": _sha(_canon_board(inputs.board_path)),
        "brief": _sha(_canon_brief(inputs.brief_path)),
        "scoring": _sha(_canon_scoring(inputs.scoring_path)),
        "entrypoint": _sha(_canon_entrypoint(inputs.entrypoint)),
        "mutable_trees": _sha(_canon_mutable_trees(inputs.mutable_trees)),
        "proposer": _sha(_canon_proposer(inputs.proposer_path)),
    }


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def resolve_contract_inputs(workspace_root: Path) -> ContractInputs:
    """Resolve the contract inputs for a workspace from ``config.json``.

    Reads ``{workspace_root}/config.json``, then resolves:

    * ``contract.board_path`` / ``contract.brief_path`` /
      ``contract.scoring_path`` — the canonical contract source paths
      recorded by ``zicato register``. The proposer-brief path is also
      accepted under its legacy ``contract.rubric_path`` key so
      workspaces registered before the rename keep resolving. When the
      ``contract`` key is absent (a workspace registered before
      auto-epoching landed) the default convention is used:
      ``<workspace_root>/board.jsonl``, ``brief.md``, ``scoring.json``
      relative to the workspace root's parent (the operator's working
      directory).
    * ``adk_entrypoint`` — the registered adapter entrypoint.
    * ``mutable_trees`` — the registered source roots.

    Raises
    ------
    FileNotFoundError
        When ``config.json`` is missing. The message suggests running
        ``zicato register``.
    """
    config_path = workspace_root / "config.json"
    if not config_path.exists():
        raise FileNotFoundError(
            f"no config.json under {workspace_root}; run "
            f"`zicato register` to record the evaluation contract before "
            "evolving"
        )
    config = json.loads(config_path.read_text(encoding="utf-8"))

    contract = config.get("contract") or {}
    board_path = Path(
        contract.get("board_path") or _default_contract_path(workspace_root, "board.jsonl")
    )
    # ``brief_path`` is the current key; ``rubric_path`` is the legacy
    # name kept readable so pre-rename workspaces still resolve.
    brief_path = Path(
        contract.get("brief_path")
        or contract.get("rubric_path")
        or _default_brief_path(workspace_root)
    )
    scoring_path = Path(
        contract.get("scoring_path") or _default_contract_path(workspace_root, "scoring.json")
    )

    entrypoint = str(config.get("adk_entrypoint", ""))
    raw_trees = config.get("mutable_trees") or config.get("source_roots") or []
    mutable_trees = tuple(str(t) for t in raw_trees)

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

    return ContractInputs(
        board_path=board_path,
        brief_path=brief_path,
        scoring_path=scoring_path,
        entrypoint=entrypoint,
        mutable_trees=mutable_trees,
        proposer_path=proposer_path,
    )


def default_contract_paths(workspace_root: Path) -> dict[str, Path | None]:
    """Return the default canonical contract source paths for a workspace.

    The convention is ``<workspace_root_parent>/board.jsonl``,
    ``brief.md``, ``scoring.json`` — the operator's live, editable
    copies sitting alongside the ``.zicato/`` directory. ``zicato
    register`` records these in ``config.json`` so subsequent commands
    do not have to re-derive them.

    The proposer-brief default is returned under both ``brief_path``
    (the current key) and ``rubric_path`` (a legacy alias) so callers
    that have not yet adopted the rename keep resolving.

    The ``proposer_path`` default is ``None`` — no proposer dir, i.e. the
    built-in default proposer. A workspace opts into a proposer dir by
    setting ``contract.proposer_path`` explicitly.
    """
    brief_default = Path(_default_brief_path(workspace_root))
    return {
        "board_path": Path(_default_contract_path(workspace_root, "board.jsonl")),
        "brief_path": brief_default,
        "rubric_path": brief_default,
        "scoring_path": Path(_default_contract_path(workspace_root, "scoring.json")),
        "proposer_path": None,
    }


def _default_brief_path(workspace_root: Path) -> str:
    """The conventional location of the operator's live proposer brief.

    Prefers ``brief.md`` next to the ``.zicato/`` directory. When that
    file is absent but a legacy ``rubric.md`` exists in the same place,
    the legacy file wins so workspaces created before the rename keep
    resolving without an operator-side file rename.
    """
    brief = workspace_root.parent / "brief.md"
    if not brief.exists():
        legacy = workspace_root.parent / "rubric.md"
        if legacy.exists():
            return str(legacy.resolve())
    return str(brief.resolve())


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
    "resolve_contract_inputs",
    "default_contract_paths",
]
