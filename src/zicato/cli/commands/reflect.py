"""``zicato inspect reflection`` — board-reflection CLI (Measurement System Analysis).

ADVANCED / DEBUGGING — off the happy path. ``zicato evolve`` runs the cheap
contract pre-flight (Board-reflection v1) automatically; ``zicato inspect reflection`` is
the deep, operator-driven validation of the evaluation contract itself: it runs
the four-pillar analysis over an observation corpus and emits ranked,
evidence-linked findings, each carrying a proposed contract edit
(BOARD-REFLECTION.md, the design of record).

Three subcommands, auto-discovered (:mod:`zicato.cli.discovery` mounts this
group with zero wiring elsewhere):

* ``zicato inspect reflection run`` — build the corpus, analyse it, adjudicate (when a
  meta-judge is supplied), and persist the reflection. ``--pre-register`` writes
  the plan and STOPS; ``--passive`` / ``--no-llm-adjudication`` run the cheap
  zero-LLM tier (reliability + discrimination + coverage only); the default
  (adjudication requested) REFUSES without ``--adjudicator-call-llm`` — the
  live-run gate never silently spends budget.
* ``zicato inspect reflection report`` — render a stored reflection's report (Markdown, or
  ``--json`` for the raw dict).
* ``zicato inspect reflection apply`` — carry a finding's proposed edit to a BUILDER DRAFT
  (never the sealed contract); the operator seals it through the builder, which
  is the gated step that rolls the epoch.

Running reflection never rolls the epoch — it is measurement rather than evolution.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import json
from pathlib import Path
from typing import Any

import click

from zicato.core.workspace import (
    reflection_dir,
    reflection_findings_path,
    reflection_practices_path,
    reflection_scorecards_path,
)

# The live-run gate message: an ACTIVE reflection — one that would spend
# meta-judge (adjudicator) budget — is refused unless the operator supplies an
# explicit adjudicator callable. Reflection never silently spends budget.
_LIVE_RUN_GATE_MSG = (
    "reflect run requested adjudication (ACTIVE mode) but no --adjudicator-call-llm "
    "was supplied. Adjudication spends live meta-judge budget, and the live-run gate "
    "forbids spending it without an explicit callable. Either pass "
    "--adjudicator-call-llm DOTTED_PATH (an independent meta-judge, distinct from "
    "every judge model), or run the cheap zero-LLM tier with --no-llm-adjudication "
    "(reliability + discrimination + coverage) or --passive (ingest-only)."
)


def _now_iso() -> str:
    """UTC ISO-8601 timestamp (injected into the plan for a stable id)."""
    return _dt.datetime.now(_dt.UTC).isoformat()


async def _noop_target(system: str, user: str, model: str) -> str:  # pragma: no cover
    """Placeholder target callable — reflection adjudication never runs it.

    Adjudication re-reads the ALREADY-persisted transcripts and calls only the
    independent adjudicator; the target/evaluation surfaces are never invoked.
    A distinct placeholder satisfies ``make_runtime_config``'s required-callable
    contract (and keeps the independence guard's identity comparison honest)
    without wiring a live endpoint.
    """
    return ""


async def _noop_evaluation(system: str, user: str, model: str) -> str:  # pragma: no cover
    """Placeholder evaluation callable — see :func:`_noop_target`."""
    return ""


def _resolve_workspace_epoch(workspace: str, epoch_id: str | None) -> tuple[Path, str]:
    """Resolve ``(workspace_root, epoch_id)``; raise a ClickException on failure."""
    from zicato.epoch.lifecycle import current_epoch_id  # noqa: PLC0415

    workspace_root = Path(workspace).resolve()
    resolved = epoch_id or current_epoch_id(workspace_root)
    if not resolved:
        raise click.ClickException(
            f"no current epoch under {workspace_root}; run `zicato evolve` "
            "(or `zicato epoch new`) first, or pass --epoch"
        )
    return workspace_root, resolved


def _resolve_candidates(
    workspace_root: Path,
    epoch_id: str,
    explicit: tuple[str, ...],
) -> tuple[list[str], str | None, str | None]:
    """Resolve ``(candidates, champion_id, parent_id)`` for the plan.

    An explicit ``--candidate`` set wins verbatim (champion/parent are then the
    first two, best-effort). The default is the champion plus the epoch's
    lineage slice (every generation on disk, champion first), with the
    champion's lineage parent surfaced for the decision-flip pillar.
    """
    from zicato.epoch.lineage import load_lineage  # noqa: PLC0415
    from zicato.evolve.generation_phase import current_generation  # noqa: PLC0415

    champion_id: str | None = None
    try:
        champion_id = current_generation(workspace_root, epoch_id)
    except (FileNotFoundError, ValueError):
        champion_id = None

    # Parent of the champion from lineage (for the decision-flip pair).
    parent_id: str | None = None
    epoch_gens: list[str] = []
    try:
        lineage = load_lineage(workspace_root)
        for entry in lineage.get("epochs", []):
            if entry.get("id") != epoch_id:
                continue
            for g in entry.get("generations", []):
                gid = g.get("id")
                if isinstance(gid, str):
                    epoch_gens.append(gid)
                    if gid == champion_id:
                        raw_parent = g.get("parent_id")
                        parent_id = raw_parent if isinstance(raw_parent, str) else None
            break
    except (OSError, json.JSONDecodeError):
        pass

    if explicit:
        candidates = list(explicit)
        champ = candidates[0]
        par = candidates[1] if len(candidates) > 1 else parent_id
        return candidates, champ, par

    # Default: champion first, then the rest of the lineage slice.
    ordered: list[str] = []
    if champion_id:
        ordered.append(champion_id)
    for gid in epoch_gens:
        if gid not in ordered:
            ordered.append(gid)
    if not ordered:
        raise click.ClickException(
            f"no generations found under epoch {epoch_id!r}; run at least one "
            "`zicato evolve` round (or seed a baseline) before reflecting"
        )
    return ordered, champion_id, parent_id


def _load_epoch_experiments(workspace_root: Path, epoch_id: str) -> list[dict[str, Any]]:
    """Every generation's ``experiment.json`` under the epoch, lineage order (oldest first).

    The operating-history input to the practice review — the same raw-dict shape
    the loop-health detectors accept. Unparseable / missing files are skipped.
    """
    from zicato.core.workspace import experiment_json_path  # noqa: PLC0415
    from zicato.epoch.lineage import load_lineage  # noqa: PLC0415

    gen_ids: list[str] = []
    try:
        lineage = load_lineage(workspace_root)
        for entry in lineage.get("epochs", []):
            if entry.get("id") != epoch_id:
                continue
            for g in entry.get("generations", []):
                gid = g.get("id")
                if isinstance(gid, str):
                    gen_ids.append(gid)
            break
    except (OSError, json.JSONDecodeError):
        pass

    out: list[dict[str, Any]] = []
    for gid in gen_ids:
        path = experiment_json_path(workspace_root, epoch_id, gid)
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(d, dict):
            d.setdefault("generation_id", gid)
            out.append(d)
    return out


def _board_entries_and_judges(board_file: Path) -> tuple[list[Any], list[str], list[str]]:
    """Return ``(board_entries, entry_ids, judge_names)`` for the epoch board."""
    from zicato.board.jsonl import load_board_with_meta  # noqa: PLC0415
    from zicato.judge_runtime.reliability import declared_judge_specs  # noqa: PLC0415

    board, _disable_drift, _judge_only = load_board_with_meta(board_file)
    entry_ids = [e.id for e in board]
    judge_names = sorted({spec.name for spec in declared_judge_specs(board)})
    return board, entry_ids, judge_names


def _build_bill_of_health(
    *,
    corpus: list[Any],
    reflection_id: str,
    parent_id: str | None,
    child_id: str | None,
    promote_margin: float,
    noise_floor: dict[str, Any] | None,
    preflight: dict[str, Any] | None,
    board_kinds: list[str],
    board_judges: list[str],
    scorecards: list[Any],
) -> dict[str, Any]:
    """Compute the four-pillar bill of health (pure analysis over the corpus)."""
    from zicato.reflection import analysis  # noqa: PLC0415

    reliability = analysis.noise_floor_summary(
        corpus=corpus, epoch_noise_floor=noise_floor, epoch_preflight=preflight
    )
    flip: dict[str, Any] | None = None
    if parent_id and child_id and parent_id != child_id:
        flip = analysis.decision_flip_probability(
            corpus=corpus,
            reflection_id=reflection_id,
            parent_id=parent_id,
            child_id=child_id,
            promote_margin=promote_margin,
        )
    reliability["decision_flip"] = flip

    differentiation = analysis.entry_differentiation(corpus=corpus)
    redundancy = analysis.redundancy_clusters(corpus=corpus)
    cover = analysis.coverage(corpus=corpus, board_kinds=board_kinds, board_judges=board_judges)
    discrimination = {
        "entry_differentiation": differentiation,
        "redundancy": redundancy,
        "coverage": cover,
    }

    # Validity: aggregate F1 across scorecards (None when no adjudication ran).
    f1s = [c.f1 for c in scorecards if c.f1 is not None]
    validity = {
        "n_judges": len(scorecards),
        "aggregate_f1": (sum(f1s) / len(f1s)) if f1s else None,
        "untested_judges": cover.get("untested_judges", []),
    }

    floor_max_abs = reliability.get("noise_floor_max_abs_delta")
    floor_delta_std = reliability.get("noise_floor_delta_std")
    calibration = {
        "promote_margin": promote_margin,
        "noise_floor_max_abs_delta": floor_max_abs,
        "noise_floor_delta_std": floor_delta_std,
        "margin_clears_floor": (
            None if floor_max_abs is None else promote_margin >= float(floor_max_abs)
        ),
    }

    return {
        "reflection_id": reflection_id,
        "noise_floor_max_abs_delta": floor_max_abs,
        "noise_floor_delta_std": floor_delta_std,
        "decision_flip_p": (flip.get("p_flip") if isinstance(flip, dict) else None),
        "pillars": {
            "reliability": reliability,
            "discrimination": discrimination,
            "validity": validity,
            "calibration": calibration,
        },
        "fidelity_tiers": reliability.get("fidelity_tiers", []),
    }


def _write_json(path: Path, payload: Any) -> None:
    """Atomically write ``payload`` as pretty JSON (tmp + rename)."""
    import os  # noqa: PLC0415

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


@click.group(
    name="reflect",
    short_help="Advanced: validate the evaluation contract (board reflection).",
)
@click.pass_context
def reflect_grp(ctx: click.Context) -> None:
    """Advanced: board reflection — Measurement System Analysis for the contract.

    Off the happy path. Reflection runs the board's evaluations and analyses
    the observed behavior to validate and tune the contract itself — debug
    judges, calibrate the promote margin, prune redundant entries. It is
    diagnose-and-recommend only: it never edits the contract, so running it
    never rolls the epoch. See BOARD-REFLECTION.md.
    """
    ctx.ensure_object(dict)


@reflect_grp.command(
    "run",
    short_help="Build + analyse an observation corpus; emit ranked findings.",
)
@click.option("--workspace", default=".zicato", show_default=True, help="Workspace root.")
@click.option("--epoch", "epoch_id", default=None, help="Contract to validate (default: current).")
@click.option(
    "--candidate",
    "candidates",
    multiple=True,
    help="Generation id in the candidate spread (repeatable; default: champion + lineage).",
)
@click.option(
    "--entries",
    "entries",
    multiple=True,
    help="Board entry id to cover (repeatable; default: the whole board).",
)
@click.option("--replicates", default=3, show_default=True, type=click.IntRange(min=1))
@click.option(
    "--adjudicator-call-llm",
    "adjudicator_dotted",
    default=None,
    help="Dotted import path of the independent meta-judge call_llm (ACTIVE mode).",
)
@click.option(
    "--checks",
    default=None,
    help="Comma-separated check subset (default: all).",
)
@click.option(
    "--no-llm-adjudication",
    "no_llm_adjudication",
    is_flag=True,
    default=False,
    help="Cheap tier: reliability + discrimination + coverage only, zero LLM.",
)
@click.option(
    "--passive",
    "passive",
    is_flag=True,
    default=False,
    help="Ingest-only: reference existing lineage artifacts, zero LLM.",
)
@click.option(
    "--pre-register",
    "pre_register",
    is_flag=True,
    default=False,
    help="Write plan.json and STOP (review before spending).",
)
@click.option("--k-adj", default=1, show_default=True, type=click.IntRange(min=1))
@click.option("--max-wall-clock-seconds", "max_wall_clock_seconds", default=None, type=int)
@click.option("--output", "output_path", default=None, help="Report destination (default: stdout).")
def run_cmd(
    workspace: str,
    epoch_id: str | None,
    candidates: tuple[str, ...],
    entries: tuple[str, ...],
    replicates: int,
    adjudicator_dotted: str | None,
    checks: str | None,
    no_llm_adjudication: bool,
    passive: bool,
    pre_register: bool,
    k_adj: int,
    max_wall_clock_seconds: int | None,
    output_path: str | None,
) -> None:
    """Build the observation corpus, analyse it, adjudicate, and persist.

    The corpus is assembled by REFERENCING the lineage's already-persisted run
    artifacts (loss / result / judge_io) with zero LLM budget; the only LLM
    spend is the independent meta-judge adjudication, gated behind
    --adjudicator-call-llm. --pre-register writes the plan and stops;
    --passive / --no-llm-adjudication run the cheap zero-LLM tier.
    """
    from zicato.core.workspace import board_path as _board_path  # noqa: PLC0415
    from zicato.epoch.lifecycle import load_epoch  # noqa: PLC0415
    from zicato.reflection import plan as plan_mod  # noqa: PLC0415

    _ = max_wall_clock_seconds  # recorded intent; no fresh board runs in this tier
    workspace_root, resolved_epoch = _resolve_workspace_epoch(workspace, epoch_id)
    try:
        epoch_cfg = load_epoch(workspace_root, resolved_epoch)
    except FileNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc
    board_file = _board_path(workspace_root, resolved_epoch)
    if not board_file.exists():
        raise click.ClickException(f"no board at {board_file}")
    board, board_entry_ids, board_judges = _board_entries_and_judges(board_file)
    weights = epoch_cfg.scoring
    promote_margin = float(getattr(weights, "promote_margin", 0.0))

    candidate_list, champion_id, parent_id = _resolve_candidates(
        workspace_root, resolved_epoch, candidates
    )
    entry_list = list(entries) if entries else board_entry_ids

    # Check set + mode.
    if checks:
        requested = tuple(c.strip() for c in checks.split(",") if c.strip())
    else:
        requested = plan_mod.DEFAULT_CHECKS
    mode = plan_mod.MODE_PASSIVE if passive else plan_mod.MODE_ACTIVE
    adjudication_requested = (
        not passive and not no_llm_adjudication and plan_mod.CHECK_JUDGE_AUDIT in requested
    )

    created_at = _now_iso()
    reflection_plan = plan_mod.new_plan(
        epoch_id=resolved_epoch,
        candidates=candidate_list,
        entries=entry_list,
        replicates=replicates,
        created_at=created_at,
        adjudicator_model=adjudicator_dotted,
        checks=requested,
        mode=mode,
        pre_registered=pre_register,
    )
    reflection_id = reflection_plan.reflection_id

    # --pre-register: write the plan and STOP before spending anything.
    if pre_register:
        path = plan_mod.write_plan(workspace_root, reflection_plan)
        click.echo(f"pre-registered plan for {reflection_id} -> {path}")
        click.echo("review it, then re-run without --pre-register to execute.")
        return

    # Live-run gate: adjudication requested but no callable ⇒ REFUSE.
    if adjudication_requested and not adjudicator_dotted:
        raise click.ClickException(_LIVE_RUN_GATE_MSG)

    # Structured operator-log stream (LOGGING.md) for this reflect
    # invocation — the other long-running command. Installed here (past the
    # refuse gate, so a refused run writes no stream) and closed in the
    # finally below. Best-effort; a logging-setup failure never fails the run.
    from zicato.logging_stream import install_log_stream, set_log_context  # noqa: PLC0415
    from zicato.workspace.config_io import read_workspace_config  # noqa: PLC0415

    log_level = read_workspace_config(workspace_root).runtime.get("log_level", "INFO")
    _log_stream = install_log_stream(workspace_root, level=str(log_level))
    set_log_context(epoch_id=resolved_epoch)
    # Contract-load preflight: surface the telemetry-dialect capability
    # warnings ONCE for this invocation — the SAME single seam evolve uses
    # (evolve.loop.emit_dialect_capability_warnings), so a `reflect run`
    # tuning a drift-derived loss under a drift-incapable dialect is warned
    # too. Best-effort; a warning-emit failure never fails the run.
    from zicato.evolve.loop import emit_dialect_capability_warnings  # noqa: PLC0415
    from zicato.util import best_effort  # noqa: PLC0415

    with best_effort("dialect-capability preflight warnings"):
        emit_dialect_capability_warnings(workspace_root)
    try:
        _reflect_execute(
            workspace_root=workspace_root,
            resolved_epoch=resolved_epoch,
            reflection_plan=reflection_plan,
            reflection_id=reflection_id,
            plan_mod=plan_mod,
            candidate_list=candidate_list,
            entry_list=entry_list,
            weights=weights,
            adjudication_requested=adjudication_requested,
            adjudicator_dotted=adjudicator_dotted,
            board_judges=board_judges,
            k_adj=k_adj,
            epoch_cfg=epoch_cfg,
            parent_id=parent_id,
            champion_id=champion_id,
            promote_margin=promote_margin,
            board=board,
            output_path=output_path,
        )
    finally:
        with contextlib.suppress(Exception):
            _log_stream.close()


def _reflect_execute(
    *,
    workspace_root: Path,
    resolved_epoch: str,
    reflection_plan: Any,
    reflection_id: str,
    plan_mod: Any,
    candidate_list: list[str],
    entry_list: list[str],
    weights: Any,
    adjudication_requested: bool,
    adjudicator_dotted: str | None,
    board_judges: list[str],
    k_adj: int,
    epoch_cfg: Any,
    parent_id: str | None,
    champion_id: str | None,
    promote_margin: float,
    board: list[Any],
    output_path: str | None,
) -> None:
    """The reflect-run execution body (corpus → adjudicate → persist → report).

    Extracted from ``run_cmd`` so the operator-log stream can wrap it in a
    single ``try/finally`` — behaviour is otherwise identical to the
    inlined body it replaced.
    """
    # Persist the plan up front (the reflection directory is created here).
    plan_mod.write_plan(workspace_root, reflection_plan)

    # --- Build the corpus (zero LLM — references existing lineage) ----------
    from zicato.reflection import corpus as corpus_mod  # noqa: PLC0415

    corpus = corpus_mod.ingest_lineage(
        workspace_root=workspace_root,
        epoch_id=resolved_epoch,
        reflection_id=reflection_id,
        candidates=candidate_list,
        entries=entry_list,
        weights=weights,
    )
    corpus_mod.write_corpus(workspace_root, resolved_epoch, reflection_id, corpus)

    # --- Adjudicate (the only LLM step; ACTIVE mode only) -------------------
    scorecards: list[Any] = []
    adjudications: list[Any] = []
    if adjudication_requested:
        adjudications, scorecards = _run_adjudication(
            workspace_root=workspace_root,
            epoch_id=resolved_epoch,
            reflection_id=reflection_id,
            corpus=corpus,
            adjudicator_dotted=adjudicator_dotted or "",
            board_judges=board_judges,
            k_adj=k_adj,
        )
    else:
        click.echo(
            "cheap tier: reliability + discrimination + coverage only "
            "(no adjudication — pass --adjudicator-call-llm for the judge audit).",
            err=True,
        )

    # --- Bill of health + findings ------------------------------------------
    from zicato.reflection import findings as findings_mod  # noqa: PLC0415

    noise_floor = getattr(epoch_cfg, "noise_floor", None)
    preflight = getattr(epoch_cfg, "preflight", None)
    summary = _build_bill_of_health(
        corpus=corpus,
        reflection_id=reflection_id,
        parent_id=parent_id,
        child_id=champion_id,
        promote_margin=promote_margin,
        noise_floor=noise_floor,
        preflight=preflight,
        board_kinds=[f"custom:{name}" for name in board_judges],
        board_judges=board_judges,
        scorecards=scorecards,
    )

    floor_max_abs = summary.get("noise_floor_max_abs_delta")
    floor_delta_std = summary.get("noise_floor_delta_std")
    derived = findings_mod.derive_findings(
        scorecards=scorecards,
        adjudications=adjudications,
        promote_margin=promote_margin,
        noise_floor_max_abs_delta=(float(floor_max_abs) if floor_max_abs is not None else None),
        noise_floor_delta_std=(float(floor_delta_std) if floor_delta_std is not None else None),
        workspace_root=workspace_root,
        epoch_id=resolved_epoch,
        reflection_id=reflection_id,
    )

    # --- Persist scorecards / findings / summary; mark executed -------------
    _write_json(
        reflection_scorecards_path(workspace_root, resolved_epoch, reflection_id),
        {"reflection_id": reflection_id, "scorecards": [c.to_json() for c in scorecards]},
    )
    _write_json(
        reflection_findings_path(workspace_root, resolved_epoch, reflection_id),
        {"reflection_id": reflection_id, "findings": [f.to_json() for f in derived]},
    )
    _write_json(
        reflection_dir(workspace_root, resolved_epoch, reflection_id) / "summary.json",
        summary,
    )

    # --- Practice review (free: pure contract + history + corpus read) ------
    from zicato.reflection import practices as practices_mod  # noqa: PLC0415

    experiments = _load_epoch_experiments(workspace_root, resolved_epoch)
    review = practices_mod.review_practices(
        board_entries=board,
        board_meta=None,
        weights=weights,
        epoch_cfg=epoch_cfg,
        experiments=experiments,
        scorecards=[c.to_json() for c in scorecards],
        corpus_stats=practices_mod.summarize_corpus(corpus),
        noise_floor=noise_floor,
        preflight=preflight,
    )
    _write_json(
        reflection_practices_path(workspace_root, resolved_epoch, reflection_id),
        review.to_json(),
    )

    plan_mod.write_plan(workspace_root, reflection_plan.mark_executed())

    # --- Project into the index (best-effort) -------------------------------
    try:
        from zicato.index.ingest import ingest_reflection  # noqa: PLC0415

        ingest_reflection(workspace_root, None, resolved_epoch, reflection_id)
    except Exception as exc:  # noqa: BLE001 — index is a projection; never fatal
        click.echo(
            f"warning: index projection failed ({exc}); run `zicato repair index`.", err=True
        )

    # --- Report -------------------------------------------------------------
    report_md = _render_report_md(
        summary,
        [c.to_json() for c in scorecards],
        [f.to_json() for f in derived],
        review.to_json().get("checks", []),
    )
    if output_path:
        Path(output_path).write_text(report_md, encoding="utf-8")
        click.echo(f"reflection {reflection_id} complete -> {output_path}")
    else:
        click.echo(report_md)


def _run_adjudication(
    *,
    workspace_root: Path,
    epoch_id: str,
    reflection_id: str,
    corpus: list[Any],
    adjudicator_dotted: str,
    board_judges: list[str],
    k_adj: int,
) -> tuple[list[Any], list[Any]]:
    """Import the adjudicator callable, adjudicate the corpus, fold scorecards."""
    import asyncio  # noqa: PLC0415
    import dataclasses  # noqa: PLC0415

    from zicato import runtime_factory, workspace_loader  # noqa: PLC0415
    from zicato.cli.commands.evolve import _import_callable  # noqa: PLC0415
    from zicato.reflection import scorecards as scorecards_mod  # noqa: PLC0415
    from zicato.reflection.adjudicator import adjudicate_corpus  # noqa: PLC0415

    adjudicator_call_llm = _import_callable(adjudicator_dotted, kind="adjudicator_call_llm")
    workspace_config = workspace_loader.load_workspace_config(workspace_root)
    # Placeholder harness/aux: adjudication re-reads persisted transcripts and
    # calls only the adjudicator, so the harness surface is never invoked — but
    # make_runtime_config requires a callable when no models.* role is configured.
    config = runtime_factory.make_runtime_config(
        workspace_config,
        workspace_root=workspace_root,
        target_call_llm=_noop_target,
        evaluation_call_llm=_noop_evaluation,
    )
    config = dataclasses.replace(config, adjudicator_call_llm=adjudicator_call_llm)

    try:
        adjudications = asyncio.run(
            adjudicate_corpus(
                corpus=corpus,
                config=config,
                epoch_id=epoch_id,
                reflection_id=reflection_id,
                adjudicator_model=adjudicator_dotted,
                workspace_root=workspace_root,
                judge_models=tuple(board_judges),
                k_adj=k_adj,
            )
        )
    except RuntimeError as exc:
        # The HARD independence guard (adjudicator IS a judge callable) surfaces here.
        raise click.ClickException(f"adjudication refused: {exc}") from exc

    cards = scorecards_mod.build_scorecards(adjudications=adjudications, corpus=corpus)
    return adjudications, cards


_VERDICT_LABEL = {
    "sound": "SOUND",
    "attend": "ATTEND",
    "unsound": "UNSOUND",
    "unmeasured": "UNMEASURED",
}


#: Longest rendering of a single ``evidence`` value on the practice
#: section's evidence line; longer values are clipped with an ellipsis and
#: read in full from the reflection JSON.
_EVIDENCE_VALUE_CLIP = 80


def _format_evidence(evidence: dict[str, Any]) -> str:
    """Render a :class:`~zicato.reflection.practices.PracticeCheck` evidence dict.

    One ``key=value`` line. Scalars render bare; lists and dicts go
    through ``json.dumps`` and are clipped, since the evidence dicts hold
    the odd long list (``expectation_kinds``, ``term_contributions``) whose
    tail is better read from the JSON than wrapped across the report.
    """
    parts: list[str] = []
    for key, value in evidence.items():
        text = value if isinstance(value, str) else json.dumps(value, default=str)
        if len(text) > _EVIDENCE_VALUE_CLIP:
            text = text[: _EVIDENCE_VALUE_CLIP - 1].rstrip() + "…"
        parts.append(f"{key}={text}")
    return ", ".join(parts)


def _render_practice_section(practices: list[dict[str, Any]]) -> list[str]:
    """The 'Practice review' section: affirmations first, then deficiencies, then unmeasured.

    Affirmations (``sound``) lead — the doc's editorial stance is that sound
    practice teaches as much as a deficiency flag, so the operator reads what
    they are doing right before the deficiencies land against that baseline.
    Then ``unsound`` above ``attend`` (worst-first), then ``unmeasured`` with the
    input each needs.

    Each check's ``evidence`` dict is rendered too. It is the measured half
    of the convention this very report documents — the numbers behind the
    headline, including the ``recommended_promote_margin`` /
    ``recommendation_raises_margin`` pair that explains why a check
    proposed no remedy — and it was collected, serialised into the
    reflection JSON, and then left out of the document an operator reads
    (issue #129).
    """
    lines: list[str] = ["## Practice review"]
    if not practices:
        lines.append("(no practice review)")
        lines.append("")
        return lines
    counts: dict[str, int] = {}
    for c in practices:
        counts[str(c.get("verdict"))] = counts.get(str(c.get("verdict")), 0) + 1
    lines.append(
        "verdicts: "
        + ", ".join(f"{counts.get(v, 0)} {v}" for v in ("sound", "attend", "unsound", "unmeasured"))
    )
    lines.append("")
    band = {"sound": 0, "unsound": 1, "attend": 2, "unmeasured": 3}
    order = {c.get("check_id"): i for i, c in enumerate(practices)}
    ranked = sorted(
        practices,
        key=lambda c: (band.get(str(c.get("verdict")), 9), order.get(c.get("check_id"), 99)),
    )
    for c in ranked:
        verdict = str(c.get("verdict"))
        label = _VERDICT_LABEL.get(verdict, verdict.upper())
        lines.append(f"### [{label}] {c.get('check_id')}")
        lines.append(str(c.get("headline", "")))
        lines.append(f"- why it matters: {c.get('rationale', '')}")
        evidence = c.get("evidence")
        if isinstance(evidence, dict) and evidence:
            lines.append(f"- evidence: {_format_evidence(evidence)}")
        reason = c.get("unmeasured_reason")
        if reason:
            lines.append(f"- missing input: {reason}")
        op = c.get("proposed_op")
        if op:
            lines.append(f"- proposed op: `{op.get('op')}` {json.dumps(op.get('args', {}))}")
        lines.append("")
    return lines


def _discrimination_summary(discrimination: dict[str, Any]) -> str:
    """One line for the DISCRIMINATION pillar: does the board separate anything?

    The pillar carries three lists (per-entry differentiation, redundancy
    clusters, coverage); the bill of health needs their SHAPE, and the lists
    themselves stay in ``summary.json`` for anything that wants them.

    Entries observed under fewer than two candidates report
    ``differentiates=None`` and are counted UNDECIDABLE rather than folded into
    either side — "this entry is flat" and "this entry was never compared" are
    different findings, and averaging them together would report a thin corpus
    as a healthy board. An empty pillar renders ``unmeasured``, never ``0/0``.
    """
    entries = (discrimination.get("entry_differentiation") or {}).get("entries") or []
    if not entries:
        return "unmeasured (no entries in the corpus)"
    decided = [e for e in entries if e.get("differentiates") is not None]
    undecidable = len(entries) - len(decided)
    if decided:
        moving = sum(1 for e in decided if e.get("differentiates"))
        parts = [f"{moving}/{len(decided)} entries differentiate"]
        if undecidable:
            parts.append(f"{undecidable} undecidable (<2 candidates)")
    else:
        parts = [f"unmeasured ({undecidable} entries, none seen under 2+ candidates)"]
    redundant = (discrimination.get("redundancy") or {}).get("redundant_clusters") or []
    parts.append(f"{len(redundant)} redundant cluster(s)")
    uncovered = (discrimination.get("coverage") or {}).get("uncovered_kinds") or []
    parts.append(f"{len(uncovered)} uncovered drift kind(s)")
    return ", ".join(parts)


def _render_report_md(
    summary: dict[str, Any],
    scorecards: list[dict[str, Any]],
    findings: list[dict[str, Any]],
    practices: list[dict[str, Any]] | None = None,
) -> str:
    """Render the analyzer-style Markdown report (practices + findings + scorecards + pillars)."""
    lines: list[str] = []
    rid = summary.get("reflection_id", "?")
    lines.append(f"# Reflection report — {rid}")
    lines.append("")
    if practices:
        lines.extend(_render_practice_section(practices))

    # Pillar summary.
    pillars = summary.get("pillars") or {}
    floor = summary.get("noise_floor_max_abs_delta")
    floor_std = summary.get("noise_floor_delta_std")
    flip = summary.get("decision_flip_p")
    lines.append("## Bill of health")
    # BOTH floor statistics, each labelled with what it is good for. The range
    # (``max |delta|``) is a RANGE: its expectation grows without bound in the
    # calibration draw count, so a margin sized from it rises as the
    # measurement improves — the backwards behaviour issue #112 names.
    # ``delta_std`` estimates the dispersion of exactly the delta the promote
    # gate thresholds and sharpens with more draws, which is why
    # ``recommended_promote_margin`` prefers it. Printing the range alone made
    # this report show only the statistic the code itself says not to act on.
    lines.append(
        f"- noise floor (max |delta|, K-inflated): "
        f"{floor if floor is not None else 'unmeasured'}"
    )
    lines.append(
        f"- noise floor (delta std, draw-count-stable): "
        f"{floor_std if floor_std is not None else 'unmeasured'}"
    )
    if flip is not None:
        lines.append(f"- P(gate decision flips): {flip}")
    else:
        # The bootstrap was undefined (too few replicates, or no
        # observations); surface the reason rather than a fabricated 0.0.
        reason = ((pillars.get("reliability") or {}).get("decision_flip") or {}).get("reason")
        detail = f" ({reason})" if reason else ""
        lines.append(f"- P(gate decision flips): n/a{detail}")

    # CALIBRATION. The margin and the floor are a single reading: a margin
    # at or below the floor cannot tell a promotion from a re-roll of the same
    # generation, and that verdict is already computed in the pillar.
    calibration = pillars.get("calibration", {}) or {}
    margin = calibration.get("promote_margin")
    lines.append(f"- promote margin: {margin if margin is not None else 'unmeasured'}")
    clears = calibration.get("margin_clears_floor")
    if clears is None:
        clears_text = "unmeasured (no floor on record)"
    elif clears:
        clears_text = "yes"
    else:
        clears_text = "NO — promotions cannot be distinguished from re-rolls"
    lines.append(f"- margin clears floor: {clears_text}")

    lines.append(
        f"- discrimination: {_discrimination_summary(pillars.get('discrimination') or {})}"
    )

    validity = pillars.get("validity") or {}
    agg_f1 = validity.get("aggregate_f1")
    agg_f1_str = agg_f1 if agg_f1 is not None else "n/a (no adjudication)"
    lines.append(f"- aggregate judge F1: {agg_f1_str}")
    untested = validity.get("untested_judges", [])
    if untested:
        lines.append(f"- untested judges: {', '.join(untested)}")
    lines.append("")

    # Ranked findings.
    lines.append(f"## Findings ({len(findings)})")
    if not findings:
        lines.append("(none)")
    for f in findings:
        lines.append(f"### [{f.get('severity')}] {f.get('title')}")
        lines.append(f.get("detail", ""))
        rec = f.get("recommendation")
        if rec:
            lines.append(f"- recommendation: {rec}")
        op = f.get("proposed_op")
        if op:
            lines.append(f"- proposed op: `{op.get('op')}` {json.dumps(op.get('args', {}))}")
            lines.append(
                f"- apply with: `zicato inspect reflection apply {rid} {f.get('finding_id')}`"
            )
        for ev in f.get("evidence", []):
            span = str(ev.get("span") or "")[:80]
            # The chip's OWN verdict and judge, never inferred from the
            # finding's title. A finding titled "fires falsely" can carry an FN
            # span (the pile a judge is scored on and the pile a finding is
            # named for are chosen separately), so reading the verdict off the
            # title MISLABELS the evidence — which is exactly what printing
            # ``run_ref — span`` alone forced the reader to do. Both fields ride
            # on every chip (``reflection/findings.py::_evidence``).
            verdict = str(ev.get("verdict") or "?").upper()
            judge_name = ev.get("judge_name") or "?"
            lines.append(f"  - evidence: [{verdict}] {judge_name} {ev.get('run_ref')} — {span}")
        lines.append("")

    # Per-judge scorecard table.
    lines.append("## Judge scorecards")
    if not scorecards:
        lines.append("(no adjudication ran)")
    else:
        # ``fpr``, ``severity_accuracy`` and ``disagreement_rate`` are tracked
        # APART from precision/recall by construction (``scorecards.py``): a
        # judge can score a clean f1 and still fire at the wrong severity, or
        # disagree with itself across replicates of the same decision. Dropping
        # those columns hid the two failure modes the module separates them to
        # expose. ``n_decisions`` is NOT a column: the invariant
        # ``tp + fp + fn + tn + ambiguous == n_decisions`` holds per row, so it
        # would be arithmetic the reader can already do.
        lines.append(
            "| judge | TP | FP | FN | TN | amb | precision | recall | f1 | FPR | "
            "sev acc | disagree | κ | exercised |"
        )
        lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
        for c in scorecards:
            exercised = "yes" if c.get("exercised") else "no"
            lines.append(
                f"| {c.get('judge_name')} | {c.get('tp')} | {c.get('fp')} | {c.get('fn')} | "
                f"{c.get('tn')} | {c.get('ambiguous')} | {_fmt(c.get('precision'))} | "
                f"{_fmt(c.get('recall'))} | {_fmt(c.get('f1'))} | {_fmt(c.get('fpr'))} | "
                f"{_fmt(c.get('severity_accuracy'))} | {_fmt(c.get('disagreement_rate'))} | "
                f"{_fmt(c.get('self_consistency_kappa'))} | {exercised} |"
            )
        # The per-judge remedies, BENEATH the table rather than as another
        # column — a recommendation is a sentence, and the table is already
        # thirteen columns of numbers. Listed only for judges that carry one,
        # so a clean audit adds no empty section.
        remedies = [(c.get("judge_name"), c.get("recommendation")) for c in scorecards]
        remedies = [(name, text) for name, text in remedies if text]
        if remedies:
            lines.append("")
            lines.append("Recommendations:")
            for name, text in remedies:
                lines.append(f"- `{name}` — {text}")
    lines.append("")
    return "\n".join(lines)


def _fmt(value: Any) -> str:
    """Format an optional float for the report table."""
    if value is None:
        return "—"
    if isinstance(value, int | float):
        return f"{float(value):.3f}"
    return str(value)


@reflect_grp.command("report", short_help="Render a stored reflection's report.")
@click.argument("reflection_id")
@click.option("--workspace", default=".zicato", show_default=True, help="Workspace root.")
@click.option("--epoch", "epoch_id", default=None, help="Epoch owning the reflection.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the raw summary dict.")
def report_cmd(reflection_id: str, workspace: str, epoch_id: str | None, as_json: bool) -> None:
    """Render a stored reflection report from its scorecards + findings + summary."""
    workspace_root = Path(workspace).resolve()
    resolved_epoch = _resolve_reflection_epoch(workspace_root, reflection_id, epoch_id)
    if resolved_epoch is None:
        raise click.ClickException(f"no reflection {reflection_id!r} found under {workspace_root}")

    summary = _load_json_or(
        reflection_dir(workspace_root, resolved_epoch, reflection_id) / "summary.json", {}
    )
    scorecards = _load_json_or(
        reflection_scorecards_path(workspace_root, resolved_epoch, reflection_id), {}
    )
    findings = _load_json_or(
        reflection_findings_path(workspace_root, resolved_epoch, reflection_id), {}
    )
    practices = _load_json_or(
        reflection_practices_path(workspace_root, resolved_epoch, reflection_id), {}
    )
    cards = scorecards.get("scorecards", []) if isinstance(scorecards, dict) else []
    finds = findings.get("findings", []) if isinstance(findings, dict) else []
    checks = practices.get("checks", []) if isinstance(practices, dict) else []

    from zicato.reflection.suggestions import (  # noqa: PLC0415
        read_suggestions,
        render_suggestions_md,
    )

    suggestions = read_suggestions(workspace_root, resolved_epoch, reflection_id)

    if as_json:
        click.echo(
            json.dumps(
                {
                    "summary": summary,
                    "scorecards": cards,
                    "findings": finds,
                    "practices": checks,
                    "suggestions": [s.to_json() for s in suggestions],
                },
                indent=2,
                sort_keys=True,
            )
        )
        return
    report = _render_report_md(summary if isinstance(summary, dict) else {}, cards, finds, checks)
    if suggestions:
        report = report + "\n" + "\n".join(render_suggestions_md(suggestions))
    click.echo(report)


@reflect_grp.command(
    "practices",
    short_help="Cheap tier: the practice review over the contract + history (no corpus).",
)
@click.option("--workspace", default=".zicato", show_default=True, help="Workspace root.")
@click.option("--epoch", "epoch_id", default=None, help="Contract to review (default: current).")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the raw review dict.")
def practices_cmd(workspace: str, epoch_id: str | None, as_json: bool) -> None:
    """Run the practice review WITHOUT a reflection corpus — contract + history only.

    The instant, always-free tier: a pure read over the epoch's board / scoring /
    lineage. The checks that need a reflection corpus or scorecards
    (``loss_monoculture`` / ``judge_criterion_quality`` / ``weight_revisit``)
    honestly report ``unmeasured`` naming the missing input — run ``zicato
    reflect run`` for those. Nothing is persisted (there is no reflection id);
    the review is printed.
    """
    from zicato.core.workspace import board_path as _board_path  # noqa: PLC0415
    from zicato.epoch.lifecycle import load_epoch  # noqa: PLC0415
    from zicato.reflection import practices as practices_mod  # noqa: PLC0415

    workspace_root, resolved_epoch = _resolve_workspace_epoch(workspace, epoch_id)
    try:
        epoch_cfg = load_epoch(workspace_root, resolved_epoch)
    except FileNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc
    board_file = _board_path(workspace_root, resolved_epoch)
    if not board_file.exists():
        raise click.ClickException(f"no board at {board_file}")
    board, _entry_ids, _judges = _board_entries_and_judges(board_file)

    review = practices_mod.review_practices(
        board_entries=board,
        board_meta=None,
        weights=epoch_cfg.scoring,
        epoch_cfg=epoch_cfg,
        experiments=_load_epoch_experiments(workspace_root, resolved_epoch),
        scorecards=None,
        corpus_stats=None,
        noise_floor=getattr(epoch_cfg, "noise_floor", None),
        preflight=getattr(epoch_cfg, "preflight", None),
    )
    payload = review.to_json()
    if as_json:
        click.echo(json.dumps(payload, indent=2, sort_keys=True))
        return
    click.echo("\n".join(_render_practice_section(payload.get("checks", []))))


@reflect_grp.command(
    "suggest",
    short_help="Synthesise measured eval suggestions from mined episodes (generative reflection).",
)
@click.option("--workspace", default=".zicato", show_default=True, help="Workspace root.")
@click.option("--epoch", "epoch_id", default=None, help="Contract to mine (default: current).")
@click.option(
    "--reflection",
    "reflection_id",
    default=None,
    help="Attach suggestions to this reflection id (default: mint a fresh one).",
)
@click.option(
    "--probe",
    is_flag=True,
    default=False,
    help="SPEND champion budget on the live admission probes (endpoint-gated; default OFF).",
)
@click.option(
    "--allow-llm",
    "allow_llm",
    is_flag=True,
    default=False,
    help="Permit LLM synthesis (judge/rubric drafting; aux-metered). Default: mechanical only.",
)
@click.option(
    "--from-trajectories",
    "from_trajectories",
    default=None,
    type=click.Path(),
    help="Bootstrap suggestions from a directory of foreign agent trace files "
    "(*.jsonl; TRAJECTORY-BOOTSTRAP.md). Imports + mines them ALONGSIDE the "
    "workspace episodes; goldfive-optional.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit raw suggestion dicts.")
def suggest_cmd(
    workspace: str,
    epoch_id: str | None,
    reflection_id: str | None,
    probe: bool,
    allow_llm: bool,
    from_trajectories: str | None,
    as_json: bool,
) -> None:
    """Mine episodes, synthesise suggestions, (optionally) admission-stamp, persist.

    The eval-synthesis surface (EVAL-SYNTHESIS.md §6) runs three stages: episode
    mining extracts episodes without calling an endpoint, suggestion synthesis
    drafts a suggestion from each, and admission measurement scores them. The
    live admission probes SPEND real champion budget and are endpoint-gated —
    they run ONLY under ``--probe`` (default OFF: plan-mode shows what they would
    spend, spending nothing). Suggestions persist beside ``findings.json`` and
    render through ``zicato inspect reflection report``. Recommend-only: apply stages a
    builder draft, never the sealed contract.

    ``--from-trajectories <dir>`` bootstraps the instrument from a directory of
    foreign agent trace files (TRAJECTORY-BOOTSTRAP.md §6): the traces are
    imported (format-sniffed + reduced through the existing dialect reducers),
    persisted under the minted reflection dir, and mined ALONGSIDE the workspace
    episodes into one ranked list. The bootstrap tier drafts board entries whose
    provenance names the foreign source. It is goldfive-optional — a trace dir
    with zero goldfive artifacts still yields suggestions.
    """
    from zicato.reflection import suggestions as sug_mod  # noqa: PLC0415
    from zicato.reflection.mining import mine_episodes  # noqa: PLC0415

    workspace_root, resolved_epoch = _resolve_workspace_epoch(workspace, epoch_id)
    paths = _paths_for(workspace_root)

    rid = reflection_id or _mint_reflection_id(resolved_epoch)

    imported_traces: list[Any] = []
    if from_trajectories is not None:
        from zicato.reflection.trace_import import (  # noqa: PLC0415
            import_trajectories,
            write_imported_traces,
        )

        imported_traces = import_trajectories(Path(from_trajectories))
        if not imported_traces:
            click.echo(
                f"no importable *.jsonl traces under {from_trajectories!r} "
                "(empty or missing directory); nothing to bootstrap.",
            )
            return
        write_imported_traces(workspace_root, resolved_epoch, rid, imported_traces)
        click.echo(
            f"imported {len(imported_traces)} foreign trace(s) from {from_trajectories!r} "
            f"under reflection {rid}",
            err=True,
        )

    episodes = mine_episodes(paths, resolved_epoch, imported_traces=imported_traces)
    click.echo(f"mined {len(episodes)} episode(s) from epoch {resolved_epoch!r}", err=True)

    synthesize = sug_mod.resolve_synthesize()
    if synthesize is None:
        raise click.ClickException(
            "no synthesis seam available (reflection.synthesis.synthesize is not "
            "importable). Mining ran; suggestion drafting needs the synthesiser."
        )
    if imported_traces:
        # The bootstrap tier needs the reconstructions to draft entries (§7's
        # extended shim); the seam gains ``imported_traces=`` at integration.
        raw_suggestions = synthesize(
            episodes,
            allow_llm=allow_llm,
            workspace_root=workspace_root,
            epoch_id=resolved_epoch,
            imported_traces=imported_traces,
        )
    else:
        raw_suggestions = synthesize(
            episodes, allow_llm=allow_llm, workspace_root=workspace_root, epoch_id=resolved_epoch
        )
    suggestions = [sug_mod._as_suggestion(s) for s in raw_suggestions]

    if probe:
        admit = sug_mod.resolve_admit()
        if admit is None:
            click.echo(
                "warning: --probe requested but no admission-measurement seam is "
                "available; persisting UNMEASURED suggestions.",
                err=True,
            )
        else:
            click.echo(
                "--probe: spending champion budget on the live admission probes "
                f"(A/A noise at base {sug_mod.SYNTHESIS_REPLICATE_BASE}).",
                err=True,
            )
            admitted = admit(
                suggestions, probe=True, workspace_root=workspace_root, epoch_id=resolved_epoch
            )
            suggestions = [sug_mod._as_suggestion(s) for s in admitted]
    else:
        cost = sug_mod.plan_cost(suggestions)
        click.echo(f"plan mode (no probe spent): {cost['note']}", err=True)

    sug_mod.write_suggestions(workspace_root, resolved_epoch, rid, suggestions)

    if as_json:
        click.echo(
            json.dumps(
                {"reflection_id": rid, "suggestions": [s.to_json() for s in suggestions]},
                indent=2,
                sort_keys=True,
            )
        )
        return
    click.echo(f"persisted {len(suggestions)} suggestion(s) under reflection {rid}")
    click.echo(sug_mod.render_suggestions_table(suggestions))
    click.echo(
        f"review: `zicato inspect reflection report {rid}`; "
        f"stage: `zicato inspect reflection apply {rid} <suggestion_id>`"
    )


def _paths_for(workspace_root: Path) -> Any:
    """The query ``paths`` bundle mining reads from."""
    from zicato.query.paths import WorkspacePaths  # noqa: PLC0415

    return WorkspacePaths(workspace_root)


def _mint_reflection_id(epoch_id: str) -> str:
    """A fresh ``refl-…`` id for a suggest-only reflection directory."""
    from zicato.reflection.plan import make_reflection_id  # noqa: PLC0415

    return make_reflection_id(_now_iso())


@reflect_grp.command("apply", short_help="Carry a finding/suggestion edit to a builder draft.")
@click.argument("reflection_id")
@click.argument("item_id")
@click.option("--workspace", default=".zicato", show_default=True, help="Workspace root.")
@click.option("--epoch", "epoch_id", default=None, help="Epoch owning the reflection.")
def apply_cmd(reflection_id: str, item_id: str, workspace: str, epoch_id: str | None) -> None:
    """Fork a builder draft from the live contract and stage a finding's or
    suggestion's op.

    ``item_id`` is a finding id (``find-…``) or an eval-suggestion id
    (``sug-…``). NEVER writes the sealed contract — the operator reviews the
    staged draft and seals it through the builder, which is the gated step that
    rolls the epoch.
    """
    from zicato.reflection.apply import (  # noqa: PLC0415
        FindingNotActionableError,
        FindingNotFoundError,
        SuggestionNotFoundError,
        apply_finding_to_draft,
        apply_suggestion_to_draft,
    )

    workspace_root = Path(workspace).resolve()
    resolved_epoch = _resolve_reflection_epoch(workspace_root, reflection_id, epoch_id)
    if resolved_epoch is None:
        raise click.ClickException(f"no reflection {reflection_id!r} found under {workspace_root}")

    # A suggestion id (sug-) stages through the suggestion seam; anything else is
    # a finding. Both fork a draft and never touch the sealed contract.
    if item_id.startswith("sug-"):
        try:
            applied_s = apply_suggestion_to_draft(
                workspace_root=workspace_root,
                epoch_id=resolved_epoch,
                reflection_id=reflection_id,
                suggestion_id=item_id,
            )
        except SuggestionNotFoundError as exc:
            raise click.ClickException(str(exc)) from exc
        except FindingNotActionableError as exc:
            raise click.ClickException(str(exc)) from exc
        _echo_applied(
            f"suggestion {item_id} ({applied_s.suggestion_type})",
            applied_s.slot_name,
            applied_s.op,
            applied_s.args,
            applied_s.diff,
        )
        return

    try:
        applied = apply_finding_to_draft(
            workspace_root=workspace_root,
            epoch_id=resolved_epoch,
            reflection_id=reflection_id,
            finding_id=item_id,
        )
    except FindingNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc
    except FindingNotActionableError as exc:
        raise click.ClickException(str(exc)) from exc
    _echo_applied(f"finding {item_id}", applied.slot_name, applied.op, applied.args, applied.diff)


def _echo_applied(
    label: str, slot_name: str, op: str, args: dict[str, Any], diff: dict[str, Any]
) -> None:
    """Print the staged-onto-draft confirmation shared by finding + suggestion apply."""
    click.echo(f"staged {label} onto builder draft slot {slot_name!r}")
    click.echo(f"  op: {op} {json.dumps(args)}")
    changed = diff.get("changed_components") or diff.get("components") or []
    if changed:
        click.echo(f"  draft now differs from live in: {changed}")
    click.echo(
        "next: open the tournament builder, review the "
        f"{slot_name!r} draft, and apply it there — rolling the epoch is "
        "the builder's gated step (reflect never writes the sealed contract)."
    )


def _resolve_reflection_epoch(
    workspace_root: Path,
    reflection_id: str,
    epoch_id: str | None,
) -> str | None:
    """Find which epoch owns ``reflection_id`` (explicit, else walk the tree)."""
    from zicato.core.workspace import reflection_dir as _rdir  # noqa: PLC0415
    from zicato.epoch.lifecycle import list_epochs  # noqa: PLC0415

    if epoch_id:
        return epoch_id if _rdir(workspace_root, epoch_id, reflection_id).is_dir() else None
    try:
        for cfg in list_epochs(workspace_root):
            if _rdir(workspace_root, cfg.id, reflection_id).is_dir():
                return cfg.id
    except (OSError, FileNotFoundError):
        return None
    return None


def _load_json_or(path: Path, default: Any) -> Any:
    """Read + parse a JSON file, or return ``default`` on any defect."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return default


__all__ = ["reflect_grp"]
