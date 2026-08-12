"""epoch_view — extracted from the former dashboard state_reader monolith (pure move)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from zicato.query.board_scan import board_entry_id, iter_board_rows
from zicato.query.decisions import (
    experiment_decision,
    promoted_tristate,
    stamp_experiment_decision,
)
from zicato.query.paths import (
    WorkspacePaths,
    _is_finite,
    _natural_key,
    _preview,
    _read_json_value,
    _resolve_epoch_id,
    coerce_float,
    layout_of,
)
from zicato.workspace import iter_epochs

# ---------------------------------------------------------------------------
# Epoch view
# ---------------------------------------------------------------------------


def _board_input_preview(entry: dict[str, Any]) -> str | None:
    text = entry.get("input")
    if isinstance(text, str):
        return _preview(text)
    turns = entry.get("turns")
    if isinstance(turns, list):
        for turn in turns:
            if isinstance(turn, str):
                return _preview(turn)
            if isinstance(turn, dict):
                for key in ("input", "text", "content"):
                    val = turn.get(key)
                    if isinstance(val, str):
                        return _preview(val)
    persona = entry.get("persona")
    if isinstance(persona, dict):
        goal = persona.get("goal")
        if isinstance(goal, str):
            return _preview(goal)
    goal = entry.get("goal")
    if isinstance(goal, str):
        return _preview(goal)
    return None


def _parse_board(path: Path) -> list[dict[str, Any]] | None:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        return None
    entries: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        # The board's first JSONL line is a `board_meta` header object
        # (it carries `disable_drift`, not an entry's fields). Skip it
        # so it does not surface as a spurious all-`—` board row.
        if obj.get("board_meta") is True:
            continue
        expectation = obj.get("expectation")
        expectation_kind = expectation.get("kind") if isinstance(expectation, dict) else None
        budget = obj.get("wall_clock_budget_seconds")
        if budget is None:
            budget = obj.get("budget_s")
        tags = obj.get("tags")
        tags_list = [t for t in tags if isinstance(t, str)] if isinstance(tags, list) else []
        entries.append(
            {
                # ONE spelling on the wire: `entry_id` (the board JSONL's own
                # `id` is an input-format detail, not a payload field).
                "entry_id": obj.get("id"),
                "kind": obj.get("kind"),
                "input_preview": _board_input_preview(obj),
                "expectation_kind": expectation_kind if isinstance(expectation_kind, str) else None,
                "budget_s": coerce_float(budget),
                "weight": float(obj["weight"])
                if isinstance(obj.get("weight"), int | float)
                else None,
                "tags": tags_list,
            }
        )
    return entries


def _parse_board_meta(path: Path) -> dict[str, Any] | None:
    """The board's optional leading ``board_meta`` header, normalized.

    BOARD-FORMAT §1.0: a board MAY open with a ``{"board_meta": true, ...}``
    line carrying the board-wide ``disable_drift`` suppression list and the
    ``judge_only`` flag; when present it MUST be the first line, so only the
    first non-blank line is examined. Returns ``None`` when the header is
    absent, unreadable, or **fully default** (an empty suppression list AND
    ``judge_only`` false) — the writer emits no header at all for a default
    board, so a default header and no header describe the SAME board, and
    omitting the key keeps every such epoch's payload byte-identical to the
    read that predates this block.

    A sibling of :func:`_parse_board` rather than a second return value: that
    reader's shape is shared with ``judge_view`` and the ``zicato.query``
    export surface.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return None
        if not isinstance(obj, dict) or obj.get("board_meta") is not True:
            return None  # the first line is an entry ⇒ this board has no header
        kinds = obj.get("disable_drift")
        disable_drift = [k for k in kinds if isinstance(k, str)] if isinstance(kinds, list) else []
        judge_only = obj.get("judge_only") is True
        if not disable_drift and not judge_only:
            return None
        return {"disable_drift": disable_drift, "judge_only": judge_only}
    return None


def _parse_board_judges(path: Path) -> dict[str, list[dict[str, Any]]] | None:
    """The PROCESS judges each board entry declares, keyed by entry id.

    BOARD-FORMAT §1.3: an entry MAY carry ``judges: [{name, mode, body,
    severity}]`` — the custom judges the adapter appends to the built-in set
    for that entry's runs. Returns ``{entry_id: [{name, mode, severity}]}``
    for every entry declaring at least one usable judge, and ``None`` when
    the board declares none — the key is then omitted, keeping every
    judge-free epoch's payload byte-identical to the read that predates this
    block (the :func:`_parse_board_meta` precedent).

    ``body`` is NOT projected for an inline judge: it is the criterion
    PROMPT, and a roster is not a place to publish prompts. A python judge's
    body is a dotted import path — the callable's identity rather than prose
    — so it rides along as ``path``, which is what tells two python judges
    apart on screen.

    A SIBLING of :func:`_parse_board` rather than a widening of it: that
    reader's row shape is shared with ``judge_view`` and the
    ``zicato.query`` export surface, and neither wants a new key.
    """
    by_entry: dict[str, list[dict[str, Any]]] = {}
    for row in iter_board_rows(path):
        entry_id = board_entry_id(row)
        raw = row.get("judges")
        if entry_id is None or entry_id in by_entry or not isinstance(raw, list):
            continue
        judges: list[dict[str, Any]] = []
        for spec in raw:
            if not isinstance(spec, dict):
                continue
            name = spec.get("name")
            if not isinstance(name, str) or not name:
                continue  # the name IS the judge's identity; nothing to show without it
            mode = spec.get("mode")
            mode = mode if isinstance(mode, str) and mode else None
            severity = spec.get("severity")
            judge: dict[str, Any] = {
                "name": name,
                "mode": mode,
                "severity": severity if isinstance(severity, str) and severity else None,
            }
            body = spec.get("body")
            if mode == "python" and isinstance(body, str) and body:
                judge["path"] = body
            judges.append(judge)
        if judges:
            by_entry[entry_id] = judges
    return by_entry or None


def _parse_mutations(path: Path) -> list[dict[str, Any]] | None:
    value = _read_json_value(path)
    if not isinstance(value, list):
        return None
    out: list[dict[str, Any]] = []
    for m in value:
        if not isinstance(m, dict):
            continue
        start = m.get("line_start")
        end = m.get("line_end")
        start_i = int(start) if isinstance(start, int | float) else None
        end_i = int(end) if isinstance(end, int | float) else None
        if start_i is not None and end_i is not None:
            lines = str(start_i) if start_i == end_i else f"{start_i}-{end_i}"
        elif start_i is not None:
            lines = str(start_i)
        elif end_i is not None:
            lines = str(end_i)
        else:
            lines = None
        content = m.get("content")
        out.append(
            {
                "id": m.get("id"),
                "kind": m.get("kind"),
                "file": m.get("file"),
                "lines": lines,
                "preview": _preview(content) if isinstance(content, str) else None,
            }
        )
    return out


def _read_harness(paths: WorkspacePaths) -> dict[str, Any] | None:
    cfg = _read_json_value(paths.root / "config.json")
    if not isinstance(cfg, dict):
        return None
    adapter = cfg.get("adapter")
    adapter = adapter if isinstance(adapter, dict) else {}
    entrypoint = adapter.get("entrypoint") or cfg.get("adk_entrypoint") or cfg.get("entrypoint")
    trees = adapter.get("mutable_trees")
    if trees is None:
        trees = cfg.get("mutable_trees")
    mutable_trees = [t for t in trees if isinstance(t, str)] if isinstance(trees, list) else []
    return {
        "entrypoint": entrypoint if isinstance(entrypoint, str) else None,
        "mutable_trees": mutable_trees,
    }


def _read_text_best_effort(path: Path) -> str:
    """Best-effort UTF-8 text read; any error -> empty string."""
    try:
        return path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return ""


def _read_epoch_brief(epoch_dir: Path) -> str:
    """The proposer brief text for an epoch.

    ``brief.md`` post-rename; ``rubric.md`` is the legacy name and is
    read as a fallback so pre-rename epochs still resolve. Any read
    error degrades to an empty string.
    """
    for name in ("brief.md", "rubric.md"):
        try:
            return (epoch_dir / name).read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        except OSError:
            break
    return ""


def _distill_brief_goal(brief: str) -> str | None:
    """Distil a short goal summary from a proposer brief.

    The brief carries a ``## Goal`` section (see the dogfood targets'
    ``brief.md``). The summary is that section's FIRST PROSE PARAGRAPH —
    the operator's statement of what the epoch is reaching for — length
    capped by :func:`_preview` like every other preview in this module.

    Markdown source is hard-wrapped, so a paragraph is generally several
    physical lines: every shipped brief wraps its goal at ~74 columns, and
    the shipped ``target_1`` brief even splits a hyphenated word across the
    break. Reading only the first physical line therefore rendered the
    dashboard's objective callout truncated mid-sentence — sometimes
    mid-word at a dangling hyphen (issue #107). Consecutive prose lines are
    accumulated instead, stopping at the first line that opens a new block:
    a blank line, a heading, a list item (bulleted OR numbered), a
    blockquote, or a code fence. Lines are joined with a single space,
    EXCEPT after a trailing hyphen, where the wrap split one word and the
    halves are joined directly (``multi-`` + ``agent`` → ``multi-agent``).

    A leading list-item or sub-heading line is still skipped, so the
    summary reads as prose rather than a bullet fragment. Note this is a
    paragraph, not necessarily one sentence — the goal paragraph's own
    trailing clause ("… Specifically:") is part of what the operator
    wrote and is kept. Returns ``None`` when the brief has no ``Goal``
    section or no prose line within it.

    Display-only: nothing here feeds scoring, gating or promotion.

    :func:`zicato.analyzer.report_data._distill_brief_goal` delegates here
    so the publication masthead names the same goal the dashboard shows.
    """
    if not brief:
        return None
    lines = brief.replace("\r\n", "\n").split("\n")
    in_goal = False
    paragraph: list[str] = []
    for raw in lines:
        line = raw.strip()
        heading = line.lstrip("#").strip() if line.startswith("#") else None
        if heading is not None:
            if in_goal:
                # A later heading closes the Goal section (and any
                # paragraph accumulating inside it).
                break
            if heading.lower() == "goal":
                in_goal = True
            continue
        if not in_goal:
            continue
        # Skip list items / blockquotes / code fences — the summary should
        # read as prose, not a bullet fragment. Once a paragraph has
        # started, such a line CLOSES it rather than being skipped over:
        # the paragraph ends where the next block begins.
        if not line or _opens_a_block(line):
            if paragraph:
                break
            continue
        paragraph.append(line)
    if not paragraph:
        return None
    return _preview(_join_wrapped(paragraph))


#: A markdown line that opens a block other than a paragraph: a bulleted
#: item (``-``/``*``/``+``), a blockquote (``>``), a fenced code block
#: (``` or ``~~~``), or an ordered item (``1.`` / ``1)``). Bulleted items
#: alone are not enough: a numbered goal list joined as prose reads as one
#: run-on sentence ("1. first 2. second").
_BLOCK_OPENER_RE = re.compile(r"^(?:[-*+>]|```|~~~|\d+[.)]\s)")


def _opens_a_block(line: str) -> bool:
    """True when a stripped brief line begins a non-paragraph block."""
    return _BLOCK_OPENER_RE.match(line) is not None


def _join_wrapped(lines: list[str]) -> str:
    """Reassemble hard-wrapped prose lines into one line.

    Joins with a single space, except where the previous line ends in a
    hyphen that a word character precedes — there the wrap split a single
    word, so the halves are joined directly (``multi-`` + ``agent``). The
    word-character guard is what keeps a hyphen used as PUNCTUATION
    ("reaching for this -" + "namely speed") space-joined; so is a
    trailing em-dash-style double hyphen (``--``).
    """
    out = ""
    for line in lines:
        if not out:
            out = line
        elif _splits_a_word(out):
            out += line
        else:
            out += " " + line
    return out


def _splits_a_word(text: str) -> bool:
    """True when ``text`` ends mid-word at a hard-wrap hyphen.

    A single trailing hyphen preceded by a word character; a ``--`` tail is
    punctuation, and a hyphen preceded by whitespace is a dash the operator
    typed, not a wrap.
    """
    return len(text) >= 2 and text.endswith("-") and not text.endswith("--") and text[-2].isalnum()


def build_epochs_summary(paths: WorkspacePaths) -> list[dict[str, Any]]:
    """One row per epoch on disk: ``{epoch_id, goal}``.

    ``goal`` is a one-line summary distilled from that epoch's proposer
    brief (its ``## Goal`` section), or ``None`` when the brief is
    absent or carries no goal. Epochs are listed in the canonical
    timestamp-first order (the single ordering authority) so the Overview's
    epochs table can annotate each row with what the epoch is trying to
    accomplish without a per-epoch ``/api/epoch`` fetch.
    """
    out: list[dict[str, Any]] = []
    for epoch in iter_epochs(layout_of(paths)):
        goal = _distill_brief_goal(_read_epoch_brief(epoch.directory))
        out.append({"epoch_id": epoch.id, "goal": goal})
    return out


def _read_epoch_experiments(epoch_dir: Path) -> list[dict[str, Any]]:
    """Walk ``generations/*/experiment.json`` for the epoch.

    Returns a list of experiment records, one per generation that has an
    ``experiment.json``, sorted by generation id. Each record carries the
    raw ``experiment.json`` fields plus a ``patch_content`` mapping from
    mutation id to the raw patch dict (from ``patches/*.json``) so the
    frontend can render diffs without a second round-trip.

    Every record is stamped with the CANONICAL decision surface — a
    ``decision`` token (``promoted`` / ``rejected`` / ``deferred`` /
    ``None`` while in flight) and a tri-state ``promoted`` — via the ONE
    shared classifier (:mod:`zicato.query.decisions`), so the
    frontend renders decisions verbatim and never re-classifies the raw
    nested ``outcome``.
    """
    gens_dir = epoch_dir / "generations"
    if not gens_dir.is_dir():
        return []
    experiments: list[dict[str, Any]] = []
    for gen_dir in sorted(gens_dir.iterdir(), key=lambda p: _natural_key(p.name)):
        if not gen_dir.is_dir():
            continue
        exp = _read_json_value(gen_dir / "experiment.json")
        if not isinstance(exp, dict):
            continue
        # Collect patches keyed by mutation_id so the render layer can
        # display the diff alongside the hypothesis.
        patches: dict[str, Any] = {}
        patches_dir = gen_dir / "patches"
        if patches_dir.is_dir():
            for patch_file in sorted(patches_dir.iterdir()):
                if patch_file.suffix != ".json":
                    continue
                patch = _read_json_value(patch_file)
                if not isinstance(patch, dict):
                    continue
                mutation_id = patch.get("mutation_id")
                if isinstance(mutation_id, str) and mutation_id:
                    patches[mutation_id] = patch
        record = dict(exp)
        # Always stamp generation_id from the directory name so the
        # frontend can key on it even when the JSON omits it.
        record["generation_id"] = gen_dir.name
        record["patches"] = patches
        # The canonical decision surface: ``decision`` + tri-state
        # ``promoted``, stamped by the shared classifier so this feed can
        # never disagree with the lineage view.
        stamp_experiment_decision(record)
        experiments.append(record)
    return experiments


def _current_champion(experiments: list[dict[str, Any]]) -> str | None:
    """The epoch's REIGNING champion, from the stamped experiment records.

    Walks the promoted champion spine (the same chain
    :func:`compute_epoch_delta_summary` and the tournament view's
    ``_champion_lineage`` build) and returns its LAST id — the reigning
    generation. When nothing is promoted yet, the parentless seed (round
    0's incoming champion) is the champion; ``None`` when the epoch has
    no generations at all.
    """
    by_gen: dict[str, dict[str, Any]] = {}
    promoted: set[str] = set()
    for exp in experiments:
        gid = exp.get("generation_id")
        if not isinstance(gid, str) or not gid:
            continue
        by_gen[gid] = exp
        if exp.get("promoted") is True:
            promoted.add(gid)

    if promoted:
        child_of: dict[str, str] = {}
        roots: list[str] = []
        for gid in sorted(promoted):
            parent = by_gen[gid].get("parent_generation_id")
            if isinstance(parent, str) and parent in promoted:
                child_of.setdefault(parent, gid)
            else:
                roots.append(gid)
        if roots:
            cur = roots[0]
            seen = {cur}
            while cur in child_of and child_of[cur] not in seen:
                cur = child_of[cur]
                seen.add(cur)
            return cur

    # No promotion yet — the seed (parentless) generation is the champion.
    for gid in sorted(by_gen, key=_natural_key):
        parent = by_gen[gid].get("parent_generation_id")
        if not isinstance(parent, str) or not parent:
            return gid
    return None


def compute_epoch_delta_summary(
    experiments: list[dict[str, Any]],
) -> dict[str, float | None]:
    """Aggregate per-experiment ``scalar_score_delta`` for the Epoch view.

    Two numbers fall out of one walk over the per-generation experiment
    records:

    * ``champion_spine`` — the sum of ``scalar_score_delta`` across the
      promoted lineage only, i.e. the meta-loop's actual progress.
      Computed by walking the parent → child chain through promoted
      generations (the same shape :func:`_champion_lineage` and the
      analyzer's ``_promoted_lineage`` build). ``None`` when the spine
      has fewer than two promoted generations — a single promotion is
      the default first-tournament outcome and does not yet read as
      meta-loop progress, so the caller renders it as a "—" tile.
    * ``gross`` — the sum across **every** experiment that carries a
      finite delta, promoted or not. This is the historical "net" tile
      and is kept as a secondary signal. ``None`` when no experiment
      carries a finite delta.

    Both fields are best-effort: a malformed entry (non-dict outcome,
    non-numeric delta, missing ids) is silently skipped, never raised.
    The meta-loop's progress is the spine sum; ``gross`` includes
    rejected experiments and is therefore the wrong headline for
    framing whether the epoch is moving the loss in the right direction.
    """
    # Per-generation deltas + a parent → child map confined to promoted
    # generations. We use the experiment record's `parent_generation_id`
    # for the edge so the walk does not depend on the SQLite index being
    # rebuilt (the analyzer's `_promoted_lineage` reads the same field).
    by_gen: dict[str, dict[str, Any]] = {}
    promoted_set: set[str] = set()
    gross_total = 0.0
    gross_have = False
    for exp in experiments:
        if not isinstance(exp, dict):
            continue
        gid = exp.get("generation_id")
        if not isinstance(gid, str) or not gid:
            continue
        by_gen[gid] = exp
        outcome = exp.get("outcome")
        if isinstance(outcome, dict):
            ds = outcome.get("scalar_score_delta")
            if isinstance(ds, int | float) and _is_finite(ds):
                gross_total += float(ds)
                gross_have = True
            if promoted_tristate(experiment_decision(exp)) is True:
                promoted_set.add(gid)

    # Edges among promoted generations only. A promoted child whose
    # parent is *not* promoted (or is missing) is a spine root.
    child_of: dict[str, str] = {}
    roots: list[str] = []
    for gid in promoted_set:
        exp = by_gen[gid]
        parent = exp.get("parent_generation_id")
        if isinstance(parent, str) and parent in promoted_set:
            # First-wins so a duplicated edge does not push later
            # promotions off the chain.
            child_of.setdefault(parent, gid)
        else:
            roots.append(gid)

    # Walk one spine. When the workspace records multiple promotion
    # roots (e.g. a re-seeded epoch), the sorted-first id is the spine
    # we report on — matching :func:`_champion_lineage`. The total is
    # the sum of `scalar_score_delta` for every promoted hop the spine
    # walks. The tile reads "—" when the spine has zero or one promoted
    # generation: a single promotion is the default first-tournament
    # outcome (parent → first child), not yet meta-loop progress.
    chain: list[str] = []
    if roots:
        chain = [sorted(roots)[0]]
        seen = {chain[0]}
        cur = chain[0]
        while cur in child_of:
            nxt = child_of[cur]
            if nxt in seen:
                break
            chain.append(nxt)
            seen.add(nxt)
            cur = nxt
    spine_total = 0.0
    if len(chain) >= 2:
        for gid in chain:
            outcome = by_gen[gid].get("outcome")
            if not isinstance(outcome, dict):
                continue
            ds = outcome.get("scalar_score_delta")
            if isinstance(ds, int | float) and _is_finite(ds):
                spine_total += float(ds)

    return {
        "champion_spine": spine_total if len(chain) >= 2 else None,
        "gross": gross_total if gross_have else None,
    }


#: The closed enum of tournament structures (TOURNAMENT-DATA-MODEL.md §1.1).
#: A reader uses this only to normalize an unknown token to the gauntlet
#: default — semantics live with the selection agent.
_TOURNAMENT_STRUCTURES: tuple[str, ...] = (
    "gauntlet",
    "single_elim",
    "double_elim",
    "swiss",
    "racing",
)


def _normalize_structure(value: Any) -> str:
    """Map an opaque ``structure`` token to a known one, else ``gauntlet``."""
    if isinstance(value, str) and value in _TOURNAMENT_STRUCTURES:
        return value
    return "gauntlet"


def _tournament_block_from_scoring(scoring: Any) -> dict[str, Any] | None:
    """Extract the ``{structure, params}`` block from a frozen scoring dict.

    Returns ``None`` when ``scoring`` carries no ``tournament`` key (so the
    Epoch view omits the block and the frontend falls back to gauntlet —
    byte-identical to pre-feature reads). When present, an unknown
    structure token degrades to ``"gauntlet"`` and a non-object ``params``
    degrades to ``{}`` (the data model treats per-key validation as the
    selection agent's job, §1.4).
    """
    if not isinstance(scoring, dict):
        return None
    raw = scoring.get("tournament")
    if not isinstance(raw, dict):
        return None
    params = raw.get("params")
    return {
        "structure": _normalize_structure(raw.get("structure")),
        "params": params if isinstance(params, dict) else {},
    }


def _overfitting_block_from_scoring(scoring: Any) -> dict[str, Any] | None:
    """Extract the ``overfitting`` block from a frozen scoring dict.

    The overfitting Phase A surface freezes its config on
    :class:`~zicato.core.types.ScoringWeights.overfitting` and serializes
    it into ``scoring.json`` under an ``"overfitting"`` key. The dashboard
    reads it back DEFENSIVELY: the block is optional (an epoch that
    predates the feature — or one that left the holdout disabled — carries
    no key), every field is read with a type guard, and an unreadable /
    absent block degrades to ``None`` so the caller renders a clean
    "no holdout configured" state rather than crashing.

    Returns a normalized ``{enabled, holdout_fraction, holdout_tags,
    seed}`` dict when a usable block is present, else ``None``.
    """
    if not isinstance(scoring, dict):
        return None
    raw = scoring.get("overfitting")
    if not isinstance(raw, dict):
        return None
    enabled = raw.get("enabled")
    # An explicit `enabled: false` means the operator turned the holdout
    # off — surface it as "configured but disabled" rather than absent.
    enabled = bool(enabled) if isinstance(enabled, bool) else True
    frac = raw.get("holdout_fraction")
    holdout_fraction = (
        float(frac) if isinstance(frac, int | float) and not isinstance(frac, bool) else 0.0
    )
    # Clamp to a sane [0, 1] — a malformed fraction must never select a
    # negative / >100% slice.
    holdout_fraction = max(0.0, min(1.0, holdout_fraction))
    raw_tags = raw.get("holdout_tags")
    holdout_tags = [t for t in raw_tags if isinstance(t, str)] if isinstance(raw_tags, list) else []
    seed_raw = raw.get("seed")
    seed = int(seed_raw) if isinstance(seed_raw, int) and not isinstance(seed_raw, bool) else 0
    return {
        "enabled": enabled,
        "holdout_fraction": holdout_fraction,
        "holdout_tags": holdout_tags,
        "seed": seed,
    }


def _stable_unit(entry_id: str, seed: int) -> float:
    """A deterministic value in ``[0, 1)`` keyed on ``(entry_id, seed)``.

    Seed-stable and platform-independent (a SHA-256 digest of the keyed
    string, NOT Python's salted ``hash()``), so the dashboard's
    server-side split is reproducible across processes and matches the
    runtime's own deterministic hold-out selection.
    """
    import hashlib  # noqa: PLC0415 — local, used only by the split

    h = hashlib.sha256(f"{seed}:{entry_id}".encode()).hexdigest()
    # Take the leading 52 bits (13 hex chars) → a uniform [0, 1).
    return int(h[:13], 16) / float(1 << 52)


def compute_board_split(
    board: list[dict[str, Any]], overfitting: dict[str, Any] | None
) -> dict[str, Any]:
    """Server-side train/holdout split for one epoch's board.

    Mirrors the runtime's ``board.split.split_board`` selection so the
    dashboard names the SAME slices the gate plays: an entry is HELD OUT
    when (a) its tags intersect the configured ``holdout_tags``, or (b)
    it falls in the deterministic ``holdout_fraction`` tail of a stable
    per-entry hash. Everything else is TRAIN (played every round, the only
    slice the proposer sees).

    Returns ``{configured, enabled, holdout_fraction, holdout_tags,
    entries:[{entry_id, slice, tag?, weight?}], train_count,
    holdout_count, total}``. When no usable overfitting block is present
    (or it is disabled) every entry reads as ``train`` and ``configured``
    is ``False`` — the honest "no holdout" state the frontend renders
    without crashing.
    """
    entries_out: list[dict[str, Any]] = []
    enabled = bool(overfitting and overfitting.get("enabled"))
    frac = float(overfitting["holdout_fraction"]) if overfitting else 0.0
    tags = set(overfitting["holdout_tags"]) if overfitting else set()
    seed = int(overfitting["seed"]) if overfitting else 0
    configured = overfitting is not None and (frac > 0.0 or bool(tags))

    # Resolve which non-tag entries fall in the fraction tail. Tag-held
    # entries are removed from the pool first; the fraction applies to the
    # WHOLE board (matching the runtime's "fraction of the board" framing),
    # so the count is floor(total * fraction), drawn by stable-hash order.
    rows: list[tuple[str, dict[str, Any]]] = []
    for b in board:
        eid = b.get("entry_id")
        if eid is None:
            continue
        rows.append((str(eid), b))

    tag_held: set[str] = set()
    if enabled and tags:
        for eid, b in rows:
            entry_tags = b.get("tags")
            if isinstance(entry_tags, list) and tags.intersection(
                t for t in entry_tags if isinstance(t, str)
            ):
                tag_held.add(eid)

    frac_held: set[str] = set()
    if enabled and frac > 0.0:
        total = len(rows)
        want = int(total * frac)
        if want > 0:
            # Order the NOT-already-tag-held pool by stable hash; the tail
            # `want` entries are held out (deterministic, seed-stable).
            pool = [eid for eid, _ in rows if eid not in tag_held]
            pool.sort(key=lambda e: (_stable_unit(e, seed), e))
            # Tag-held entries already count toward the target; only top up
            # to `want` total held entries via the fraction.
            need = max(0, want - len(tag_held))
            for eid in pool[len(pool) - need :] if need else []:
                frac_held.add(eid)

    train_count = 0
    holdout_count = 0
    for eid, b in rows:
        held = enabled and (eid in tag_held or eid in frac_held)
        slice_name = "holdout" if held else "train"
        if held:
            holdout_count += 1
        else:
            train_count += 1
        row: dict[str, Any] = {"entry_id": eid, "slice": slice_name}
        # The matching holdout TAG (why-held-out provenance for the popover),
        # present only for a tag-held entry.
        if eid in tag_held:
            entry_tags = b.get("tags")
            match = next(
                (t for t in (entry_tags or []) if isinstance(t, str) and t in tags),
                None,
            )
            if match is not None:
                row["tag"] = match
        weight = b.get("weight")
        if isinstance(weight, int | float) and not isinstance(weight, bool):
            row["weight"] = float(weight)
        entries_out.append(row)

    return {
        "configured": configured,
        "enabled": enabled,
        "holdout_fraction": frac,
        "holdout_tags": sorted(tags),
        "entries": entries_out,
        "train_count": train_count,
        "holdout_count": holdout_count,
        "total": len(rows),
    }


def _latest_holdout_summary(experiments: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The most recent decision's ``holdout`` ladder summary, defensively.

    Each per-decision record (``experiment.json``) may carry a ``holdout``
    block written by the gate's confirmation step:
    ``{confirmed, train_scalar, holdout_scalar, ladder_released,
    ladder_budget_total, ladder_budget_remaining, threshold}``. The block
    is OPTIONAL (absent until the ``#2`` Ladder lands, and ``null`` when a
    decision had no holdout step). This walks the experiments newest-first
    and returns the first usable, type-guarded block — or ``None`` when no
    decision recorded one yet, the frontend's "after a run" empty state.
    """
    for exp in reversed(experiments):
        if not isinstance(exp, dict):
            continue
        raw = exp.get("holdout")
        if not isinstance(raw, dict):
            continue

        def _num(key: str) -> float | None:
            v = raw.get(key)  # noqa: B023 — raw is the loop's current record
            return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else None

        def _int(key: str) -> int | None:
            v = raw.get(key)  # noqa: B023 — raw is the loop's current record
            return int(v) if isinstance(v, int) and not isinstance(v, bool) else None

        confirmed = raw.get("confirmed")
        return {
            "generation_id": exp.get("generation_id"),
            "confirmed": confirmed if isinstance(confirmed, bool) else None,
            "train_scalar": _num("train_scalar"),
            "holdout_scalar": _num("holdout_scalar"),
            "ladder_released": bool(raw.get("ladder_released")),
            "ladder_budget_total": _int("ladder_budget_total"),
            "ladder_budget_remaining": _int("ladder_budget_remaining"),
            "threshold": _num("threshold"),
        }
    return None


def build_epoch_view(paths: WorkspacePaths, epoch_id: str | None = None) -> dict[str, Any]:
    """An epoch's full evaluation contract.

    ``epoch_id`` defaults to the CURRENT epoch (unchanged behaviour); given a
    validated id, the view resolves THAT epoch instead — the only true fix for
    viewing a non-current epoch from the dashboard.

    Matches the Rust ``epoch::build_epoch_view`` shape: no current epoch
    yields ``{"epoch_id": null}``; every other component degrades to
    empty / ``null``.

    Extended fields (added for the experiment-log / journal / analysis
    panels in the Epoch view):

    * ``experiments`` — list of per-generation experiment records, each
      carrying hypothesis, outcome, and inline patch content so the
      frontend can render {hypothesis → exact change → outcome} in one
      place without a second fetch.
    * ``delta_scalar_summary`` — ``{champion_spine, gross}`` aggregates
      over the per-experiment ``scalar_score_delta``. The spine number
      is the meta-loop's actual progress (sum across promoted hops);
      the gross number sums every experiment and is the wrong headline
      for framing meta-loop direction. Either field is ``None`` when
      no experiment of the relevant kind carries a finite delta.
    * ``journal`` — ``journal.md`` text (empty string when absent).
    * ``analysis_md`` — ``analysis.md`` text (empty string when absent).
    * ``analysis_html_inline`` — ALWAYS the empty string on this view.
      The paper-styled fragment is rendered by
      :func:`build_epoch_analysis` on its own route; rendering it here
      too put a full report render on the most frequently polled
      payload for a field nothing read off it. The key is kept because
      dropping it would change the payload shape.
    * ``analysis_html_available`` — ``True`` when ``analysis.html``
      exists on disk; the frontend can link directly to
      ``/api/epoch/{id}/analysis.html``.
    """
    epoch_id = _resolve_epoch_id(paths, epoch_id)
    if epoch_id is None:
        return {"epoch_id": None}

    epoch_dir = layout_of(paths).epoch_dir(epoch_id)
    view: dict[str, Any] = {"epoch_id": epoch_id}

    cfg = _read_json_value(epoch_dir / "config.json")
    if isinstance(cfg, dict):
        if isinstance(cfg.get("contract_hash"), str):
            view["contract_hash"] = cfg["contract_hash"]
        if isinstance(cfg.get("created_at"), str):
            view["created_at"] = cfg["created_at"]
        if isinstance(cfg.get("closed"), bool):
            view["closed"] = cfg["closed"]

    harness = _read_harness(paths)
    if harness is not None:
        view["harness"] = harness

    board = _parse_board(epoch_dir / "board.jsonl")
    if board is not None:
        view["board"] = board

    # Proposer brief: ``brief.md`` post-rename; ``rubric.md`` is the
    # legacy name (read as a fallback). Any read error -> empty string.
    view["brief"] = _read_epoch_brief(epoch_dir)

    scoring = _read_json_value(epoch_dir / "scoring.json")
    if scoring is not None:
        view["scoring"] = scoring

    # Train/holdout split (overfitting Phase A). Computed SERVER-SIDE from
    # the board entries + the frozen ``overfitting`` block on scoring.json,
    # so the frontend gets the SAME slices the gate plays without re-deriving
    # the deterministic selection. Always present (every entry reads as
    # ``train`` with ``configured: False`` when no holdout is configured).
    view["board_split"] = compute_board_split(
        board if board is not None else [], _overfitting_block_from_scoring(scoring)
    )

    # Board-level ``board_meta`` header (BOARD-FORMAT §1.0): the drift kinds
    # suppressed for every entry + the judge-only flag. It is authored in the
    # builder and folds into the contract hash, so a runtime surface that drops
    # it draws a board that is scored differently from the one it shows.
    # Omitted — like the ``tournament`` block below — when the header is absent
    # or fully default, which is byte-identical to the pre-block read.
    board_meta = _parse_board_meta(epoch_dir / "board.jsonl")
    if board_meta is not None:
        view["board_meta"] = board_meta

    # Per-entry PROCESS judges (BOARD-FORMAT §1.3), keyed by entry id — the
    # custom half of what the board page's Judges panel shows. Same omit-when-
    # absent discipline as the header above: a board whose entries declare no
    # judges reads byte-identical to the pre-block payload.
    board_judges = _parse_board_judges(epoch_dir / "board.jsonl")
    if board_judges is not None:
        view["board_judges"] = board_judges

    # Tournament structure block (TOURNAMENT-DATA-MODEL.md §3.1). Echo the
    # epoch's resolved ``{structure, params}`` from the frozen
    # ``scoring.json`` so the Epoch view can name the structure without a
    # second fetch. Absent ⇒ default to gauntlet (the frontend's default),
    # so an epoch that predates the feature still reports a coherent
    # structure rather than omitting the block.
    tournament_block = _tournament_block_from_scoring(scoring)
    if tournament_block is not None:
        view["tournament"] = tournament_block
    # else: omit — the frontend defaults to gauntlet (§3.1). Keeping the
    # block absent for a scoring.json that predates the feature preserves
    # byte-identical reads for every gauntlet epoch on disk today.

    # mutations.json is optional; absent -> empty list (never null).
    mutations = _parse_mutations(epoch_dir / "mutations.json")
    view["mutations"] = mutations if mutations is not None else []

    # Experiment log: per-generation hypothesis + outcome + patch content,
    # each stamped with the canonical ``decision`` + tri-state ``promoted``.
    view["experiments"] = _read_epoch_experiments(epoch_dir)

    # The REIGNING champion — the end of the promoted spine (or the seed
    # while nothing is promoted). The ONE champion pointer the frontend
    # reads instead of re-scanning the generation list.
    view["current_champion"] = _current_champion(view["experiments"])

    # Holdout ladder summary — the latest decision's ``holdout`` block
    # (ladder budget + train/holdout scalars). Read defensively from the
    # per-decision records; ``None`` until a decision records one (the
    # frontend's "after a run" empty state).
    view["holdout"] = _latest_holdout_summary(view["experiments"])

    # Δscalar aggregates — the Epoch header's headline number. The
    # champion-spine sum frames meta-loop progress (promoted hops only);
    # the gross sum across *every* experiment is kept as a secondary
    # signal but is the wrong number to lead with (it includes rejected
    # challengers, which never enter the lineage).
    view["delta_scalar_summary"] = compute_epoch_delta_summary(view["experiments"])

    # Journal: epoch-level markdown log of hypothesis+outcome rounds.
    view["journal"] = _read_text_best_effort(epoch_dir / "journal.md")

    # Frozen goal — Task #178's first-class field on EpochConfig and
    # the index ``epochs.goal`` column. The index is best-effort; on a
    # never-indexed workspace fall back to the goal recorded in
    # ``config.json`` (the canonical durable copy). Brief-distilled
    # fallback is preserved for legacy epochs whose ``config.json``
    # predates the field.
    goal_text = ""
    if isinstance(cfg, dict):
        raw_goal = cfg.get("goal")
        if isinstance(raw_goal, str):
            goal_text = raw_goal.strip()
    if not goal_text:
        try:
            from zicato.index.query import all_epochs as _all_epochs  # noqa: PLC0415

            for row in _all_epochs(paths.index_db):
                if row["epoch_id"] == epoch_id and "goal" in row.keys():
                    raw = row["goal"]
                    if isinstance(raw, str):
                        goal_text = raw.strip()
                    break
        except Exception:  # noqa: BLE001 — best-effort
            goal_text = ""
    if not goal_text:
        # Last resort: distill from the brief's ``## Goal`` heading.
        distilled = _distill_brief_goal(view.get("brief") or "")
        if distilled:
            goal_text = distilled
    view["goal"] = goal_text

    # Analysis: the post-epoch analysis report.
    analysis_md = _read_text_best_effort(epoch_dir / "analysis.md")
    view["analysis_md"] = analysis_md
    view["analysis_html_available"] = (epoch_dir / "analysis.html").is_file()
    # The paper-styled fragment is rendered by ``build_epoch_analysis`` on the
    # DEDICATED ``/api/epoch/{id}/analysis`` route — the one the publication
    # view actually fetches — and NOT here.
    #
    # It used to be rendered here too, on the plain ``/api/epoch`` read, which
    # meant a full ``gather_epoch_report_data`` + report render on the most
    # frequently polled payload in the dashboard, for a field no client ever
    # read off THIS view. The key stays (its absence would be a payload-shape
    # change, and every reader-parity fixture pins it) but it is now always the
    # empty string here, which is what those fixtures already record. A caller
    # that wants the fragment asks the route whose job it is.
    view["analysis_html_inline"] = ""

    return view


def read_epoch_analysis_html(paths: WorkspacePaths, epoch_id: str) -> str | None:
    """Return the raw HTML of the analysis report, or ``None`` when absent.

    Used by the ``GET /api/epoch/{id}/analysis.html`` endpoint so the
    dashboard can embed or link the self-contained analysis report.
    """
    path = layout_of(paths).analysis_html(epoch_id)
    try:
        return path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return None


def build_epoch_analysis(paths: WorkspacePaths, epoch_id: str) -> dict[str, Any]:
    """``GET /api/epoch/{id}/analysis`` — the analysis report payload.

    Returns ``{epoch_id, analysis_md, analysis_html_inline,
    analysis_html_available}``. ``analysis_html_inline`` is the
    paper-styled HTML fragment (self-contained inline CSS, inline SVG
    figures) the dashboard drops directly into the Epoch view's
    Analysis section — same renderer as the standalone ``analysis.html``
    so both surfaces look like a paper. The raw markdown ``analysis_md``
    is the FALLBACK, rendered client-side when the inline HTML is empty.

    That order was inverted here for as long as the fields existed, and
    the description was wrong in the direction that hid a cost: the
    dashboard read only ``analysis_md`` and re-rendered markdown itself,
    so the expensive server render this docstring called the primary path
    was built and discarded on every request. The reader now prefers the
    inline HTML, which is what makes the render worth doing.

    Best-effort (DQ3): a missing ``analysis.md`` reads as the empty
    string; a failed fragment render degrades to ``""``; never raises.
    """
    analysis_md_path = paths.epochs / epoch_id / "analysis.md"
    analysis_html_path = paths.epochs / epoch_id / "analysis.html"
    try:
        analysis_md = analysis_md_path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        analysis_md = ""

    analysis_html_inline = ""
    if analysis_md.strip():
        try:
            from zicato.analyzer.report import render_report_html_fragment  # noqa: PLC0415
            from zicato.analyzer.report_data import gather_epoch_report_data  # noqa: PLC0415

            data = gather_epoch_report_data(paths.root, epoch_id)
            analysis_html_inline = render_report_html_fragment(epoch_id, analysis_md, data=data)
        except Exception:  # noqa: BLE001 — fragment is best-effort
            analysis_html_inline = ""

    return {
        "epoch_id": epoch_id,
        "analysis_md": analysis_md,
        "analysis_html_inline": analysis_html_inline,
        "analysis_html_available": analysis_html_path.is_file(),
    }
