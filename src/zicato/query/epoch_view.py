"""Epoch contract, generation, and decision projections for workspace views."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from zicato.board.jsonl import load_board_rows
from zicato.epoch._storage import RecordError
from zicato.mutation.inventory import read_mutation_inventory
from zicato.proposer.brief import brief_goal, load_epoch_brief
from zicato.query.board_scan import board_entry_id
from zicato.query.decisions import (
    stamp_experiment_decision,
)
from zicato.query.inputs import EpochInputs, capture_generations
from zicato.query.paths import (
    WorkspacePaths,
    _is_finite,
    _preview,
    _resolve_epoch_id,
    coerce_float,
    layout_of,
)
from zicato.query.promoted_head import champion_history, current_champion
from zicato.workspace import WorkspaceLayout, iter_epochs
from zicato.workspace.config_io import read_workspace_config

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
            if isinstance(turn, dict) and isinstance(turn.get("user"), str):
                return _preview(turn["user"])
    persona = entry.get("user_persona")
    if isinstance(persona, dict):
        goal = persona.get("goal")
        if isinstance(goal, str):
            return _preview(goal)
    return None


def _project_board(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for obj in rows:
        if obj.get("board_meta") is True:
            continue
        expectation = obj.get("expectation")
        expectation_kind = expectation.get("kind") if isinstance(expectation, dict) else None
        budget = obj.get("wall_clock_budget_seconds")
        tags = obj.get("tags")
        tags_list = [t for t in tags if isinstance(t, str)] if isinstance(tags, list) else []
        entries.append(
            {
                # ONE spelling on the wire: `entry_id` (the board JSONL's own
                # `id` is an input-format detail rather than a payload field).
                "entry_id": obj.get("id"),
                "kind": obj.get("kind"),
                "input_preview": _board_input_preview(obj),
                "expectation_kind": expectation_kind if isinstance(expectation_kind, str) else None,
                "wall_clock_budget_seconds": coerce_float(budget),
                "weight": float(obj["weight"])
                if isinstance(obj.get("weight"), int | float)
                else None,
                "tags": tags_list,
            }
        )
    return entries


def _project_board_meta(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Project accepted metadata; omit the default suppression and judge mode."""
    if not rows or rows[0].get("board_meta") is not True:
        return None
    header = rows[0]
    disable_drift = header.get("disable_drift", [])
    judge_only = header.get("judge_only", False)
    if not disable_drift and not judge_only:
        return None
    return {"disable_drift": disable_drift, "judge_only": judge_only}


def _project_board_judges(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]] | None:
    """Project accepted judge identities without publishing inline prompts."""
    by_entry: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
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


def _project_mutations(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "id": row["id"],
            "kind": row["kind"],
            "file": row["file"],
            "lines": str(row["line_start"])
            if row["line_start"] == row["line_end"]
            else f"{row['line_start']}-{row['line_end']}",
            "preview": _preview(row["content"]),
        }
        for row in rows
    ]


def _read_harness(paths: WorkspacePaths) -> dict[str, Any] | None:
    """The registered system under test, from the WORKSPACE's live ``config.json``.

    The one live-config read left on an epoch-scoped payload, and it is
    deliberate rather than overlooked (issue #194 §6's sweep). An epoch
    freezes its board, brief and scoring into ``epochs/{id}/``, but it
    never records the harness itself — ``contract_components.json`` keeps
    only the SHA of the entrypoint and mutable-tree list. So for a closed
    epoch there is no record to prefer, and the choice is between the
    workspace's current harness and nothing.

    It stays live because nothing renders it: no dashboard view and no
    TUI surface reads ``view["harness"]``, so no operator can be misled
    by it today. Making it honest means recording the harness in the
    epoch's own config at open time (a writer change), after which this
    reader prefers the frozen copy — not flagging drift on a payload key
    that has no reader.
    """
    try:
        loaded = read_workspace_config(paths.root)
    except (OSError, ValueError):
        return None
    if not loaded.exists:
        return None
    cfg = loaded.raw
    adapter = cfg.get("adapter")
    adapter = adapter if isinstance(adapter, dict) else {}
    entrypoint = adapter.get("entrypoint")
    trees = adapter.get("mutable_trees")
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
    """Read accepted epoch guidance; absence is empty, corruption is explicit."""
    try:
        return load_epoch_brief(epoch_dir).text
    except FileNotFoundError:
        return ""


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
        row: dict[str, Any] = {"epoch_id": epoch.id, "goal": None}
        try:
            paragraph = brief_goal(_read_epoch_brief(epoch.directory))
            row["goal"] = _preview(paragraph) if paragraph else None
        except RecordError as exc:
            row["unreadable"] = str(exc)
        out.append(row)
    return out


def _read_epoch_experiments(
    layout: WorkspaceLayout,
    epoch_id: str,
    lineage: dict[str, dict[str, Any]] | None = None,
    *,
    inputs: EpochInputs | None = None,
) -> list[dict[str, Any]]:
    """The epoch's per-generation experiment records, in round-number order.

    Returns one record per generation that has an ``experiment.json``. Each
    carries the record's stored fields plus a ``patches`` mapping from
    mutation id to the patch record the experiment references, so the
    frontend can render diffs without a second round-trip.

    Every record is stamped with the CANONICAL decision surface — a
    ``decision`` token (``promoted`` / ``rejected`` / ``deferred`` /
    ``None`` while in flight) and a tri-state ``promoted`` — via the ONE
    shared classifier (:mod:`zicato.query.decisions`), so the
    frontend renders decisions verbatim and never re-classifies the raw
    nested ``outcome``.
    """
    experiments: list[dict[str, Any]] = []
    generations = (
        inputs.generations
        if inputs is not None
        else capture_generations(WorkspacePaths(layout.root), epoch_id)
    )
    for generation_id, captured in generations.items():
        if captured.unreadable is not None:
            # Degrade at this boundary rather than failing the endpoint: the
            # generation still appears, carrying the reason its fields are
            # missing instead of a row of blanks the reader cannot account
            # for. ``stamp_experiment_decision`` reads the absent outcome as
            # in-flight, which is the correct unknown here.
            record: dict[str, Any] = {
                "generation_id": generation_id,
                "patches": {},
                "unreadable": captured.unreadable,
            }
            stamp_experiment_decision(record)
            experiments.append(record)
            continue
        body = captured.body.copy()
        if body is None:
            continue
        record = dict(body)
        # Always stamp generation_id from the directory name so the
        # frontend can key on it even when the record names another.
        record["generation_id"] = generation_id
        # Patches keyed by mutation_id so the render layer can display the
        # diff alongside the hypothesis. They come off the record's own
        # patch reader, so only the patches the experiment references are
        # served — an orphan file left by a crash between the two write
        # phases is not one of them.
        record["patches"] = captured.patches.copy()
        # The canonical decision surface: ``decision`` + tri-state
        # ``promoted``, stamped by the shared classifier so this feed can
        # never disagree with the lineage view.
        stamp_experiment_decision(record)
        node = lineage.get(generation_id) if lineage is not None else None
        if node is not None:
            # INVARIANT: this feed and /api/lineage serve the IDENTICAL
            # (promoted, decision, decision_label) triple for every
            # generation. So copy all three off the lineage node, which
            # already carries ``decision_surface``'s output — never derive
            # one here from ``promoted``: the seed is promoted yet faced no
            # gate, so a local derivation reads it as a win it never raced.
            promoted = node.get("promoted")
            record["promoted"] = promoted if isinstance(promoted, bool) else None
            record["decision"] = node.get("decision")
            record["decision_label"] = node.get("decision_label")
        experiments.append(record)
    return experiments


def compute_epoch_delta_summary(
    experiments: list[dict[str, Any]],
    champions: list[str],
) -> dict[str, float | None]:
    """Sum recorded deltas for selected champions and for all measured candidates.

    The champion total requires two measured primary promotions. The gross
    total includes rejected candidates and candidates retained for recombination.
    Missing or non-finite measurements contribute to neither total.
    """
    deltas: dict[str, float] = {}
    for experiment in experiments:
        if not isinstance(experiment, dict):
            continue
        generation_id = experiment.get("generation_id")
        outcome = experiment.get("outcome")
        if not isinstance(generation_id, str) or not isinstance(outcome, dict):
            continue
        delta = outcome.get("scalar_score_delta")
        if isinstance(delta, int | float) and _is_finite(delta):
            deltas[generation_id] = float(delta)
    selected = [deltas[generation_id] for generation_id in champions if generation_id in deltas]
    return {
        "champion_spine": sum(selected) if len(selected) >= 2 else None,
        "gross": sum(deltas.values()) if deltas else None,
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

    The train/holdout split surface freezes its config on
    :class:`~zicato.core.types.ScoringWeights.overfitting` and serializes
    it into ``scoring.json`` under an ``"overfitting"`` key. The dashboard
    reads it back DEFENSIVELY. The block is optional: an epoch that left
    the holdout disabled, or whose record predates the block, carries no
    key. Every field is read with a type guard, and an unreadable or
    absent block degrades to ``None``, so the caller renders a clean
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


def build_epoch_view(
    paths: WorkspacePaths,
    epoch_id: str | None = None,
    *,
    lineage_view: dict[str, Any] | None = None,
) -> dict[str, Any]:
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
    * ``analysis_html_available`` — ``True`` when ``analysis.html``
      exists on disk; the frontend can link directly to
      ``/api/epoch/{id}/analysis.html``.
    """
    epoch_id = _resolve_epoch_id(paths, epoch_id)
    if epoch_id is None:
        return {"epoch_id": None}

    epoch_dir = layout_of(paths).epoch_dir(epoch_id)
    inputs = EpochInputs.capture(paths, epoch_id)
    view: dict[str, Any] = {"epoch_id": epoch_id}

    cfg = inputs.config.copy()
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

    try:
        board_rows = load_board_rows(epoch_dir / "board.jsonl")
    except RecordError as exc:
        board_rows = None
        view["unreadable"] = str(exc)
    board = _project_board(board_rows) if board_rows is not None else None
    if board is not None:
        view["board"] = board

    try:
        view["brief"] = _read_epoch_brief(epoch_dir)
    except RecordError as exc:
        view["brief"] = ""
        view["unreadable"] = f"{view['unreadable']}; {exc}" if "unreadable" in view else str(exc)

    scoring = inputs.scoring.copy()
    if scoring is not None:
        view["scoring"] = scoring

    # Train/holdout split. Computed SERVER-SIDE from
    # the board entries + the frozen ``overfitting`` block on scoring.json,
    # so the frontend gets the SAME slices the gate plays without re-deriving
    # the deterministic selection. Always present (every entry reads as
    # ``train`` with ``configured: False`` when no holdout is configured).
    view["board_split"] = compute_board_split(
        board if board is not None else [], _overfitting_block_from_scoring(scoring)
    )

    # Board-level ``board_meta`` header (BOARD-FORMAT §1.0): the drift kinds
    # suppressed for every entry + the judge-only flag. The header contributes
    # to the contract hash, so a runtime view that drops
    # it draws a board that is scored differently from the one it shows.
    # Omitted — like the ``tournament`` block below — when the header is absent
    # or fully default, which is byte-identical to the pre-block read.
    board_meta = _project_board_meta(board_rows or [])
    if board_meta is not None:
        view["board_meta"] = board_meta

    # Per-entry PROCESS judges (BOARD-FORMAT §1.3), keyed by entry id — the
    # custom half of what the board page's Judges panel shows. Same omit-when-
    # absent discipline as the header above: a board whose entries declare no
    # judges reads byte-identical to the pre-block payload.
    board_judges = _project_board_judges(board_rows or [])
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
    try:
        view["mutations"] = _project_mutations(
            read_mutation_inventory(epoch_dir / "mutations.json") or []
        )
    except RecordError as exc:
        view["mutations"] = []
        view["unreadable"] = f"{view['unreadable']}; {exc}" if "unreadable" in view else str(exc)

    # Experiment log: per-generation hypothesis + outcome + patch content,
    # each stamped with the canonical ``decision`` + tri-state ``promoted``.
    from zicato.query.lineage_view import build_lineage_view  # noqa: PLC0415

    if lineage_view is None:
        lineage_view = build_lineage_view(paths, epoch_id, include_ratings=False, inputs=inputs)
    lineage = {
        node["generation_id"]: node
        for node in lineage_view.get("generations", [])
        if isinstance(node, dict)
        and isinstance(node.get("generation_id"), str)
        and node.get("epoch_id") == epoch_id
    }
    view["experiments"] = _read_epoch_experiments(
        layout_of(paths), epoch_id, lineage, inputs=inputs
    )

    # Completed rounds name the primary champion; ancestry records parent relationships.
    view["current_champion"] = current_champion(paths, epoch_id)
    champion = lineage.get(view["current_champion"])
    view["champion_record"] = dict(champion) if champion is not None else None
    if champion is not None:
        from zicato.query.ratings import RATING_FIELDS, rating_by_generation  # noqa: PLC0415

        rating = rating_by_generation(paths, epoch_id).get((epoch_id, view["current_champion"]), {})
        view["champion_record"].update({field: rating.get(field) for field in RATING_FIELDS})

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
    view["delta_scalar_summary"] = compute_epoch_delta_summary(
        view["experiments"], champion_history(paths, epoch_id)
    )

    # Journal: epoch-level markdown log of hypothesis+outcome rounds.
    from zicato.epoch.journal import render_journal_section

    view["journal"] = "".join(
        render_journal_section(body)
        for generation_id, captured in inputs.generations.items()
        if generation_id != "v0" and (body := captured.body.copy()) is not None
    )

    # Frozen goal — Task #178's first-class field on EpochConfig and
    # the index ``epochs.goal`` column. The index is best-effort; on a
    # never-indexed workspace fall back to the goal recorded in
    # ``config.json`` (the canonical durable copy). The brief-distilled
    # fallback covers an epoch whose ``config.json`` carries no goal field.
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
        distilled = brief_goal(view.get("brief") or "")
        if distilled:
            goal_text = _preview(distilled)
    view["goal"] = goal_text

    # Analysis: the post-epoch analysis report.
    analysis_md = _read_text_best_effort(epoch_dir / "analysis.md")
    view["analysis_md"] = analysis_md
    view["analysis_html_available"] = (epoch_dir / "analysis.html").is_file()

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
    """Serve the published report without re-reading measurements or rendering figures."""
    directory = paths.epochs / epoch_id

    def text(name: str) -> str:
        try:
            return (directory / name).read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    return {
        "epoch_id": epoch_id,
        "analysis_md": text("analysis.md"),
        "analysis_html_inline": text("analysis.fragment.html"),
        "analysis_html_available": (directory / "analysis.html").is_file(),
    }
