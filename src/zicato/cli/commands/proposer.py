"""``zicato proposer`` — the proposer's own scorecard, reflection, and apply gate.

ADVANCED / DEBUGGING — off the happy path. ``zicato evolve`` never runs any of
this; it is the operator's instrument for improving the PROPOSER, one level up
from the loop that improves the target.

Four subcommands, auto-discovered (:mod:`zicato.cli.discovery` mounts this
group with zero wiring elsewhere):

* ``zicato proposer scorecard`` — the per-epoch proposal-quality table and its
  trend, folded from round logs the loop already wrote. Pure read.
* ``zicato proposer reflect`` — the recommend-only pass: diagnose the
  scorecard, persist findings whose remedy is a ready-to-apply edit to the
  proposer dir. It NEVER applies one.
* ``zicato proposer recommendations`` — the pending queue (everything drafted
  and not yet applied), which is also what the epoch boundary prints.
* ``zicato proposer apply-recommendation`` — the explicit operator gate. This
  is the ONLY path that writes into the proposer dir, and doing so rolls the
  contract hash, so the next ``evolve`` opens a fresh epoch.

The four invariants this command surface enforces (issue #169): never
mid-epoch, never self-applied, redacted evidence only, every accepted edit
hashed.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import click

from zicato.proposer.scorecard import (
    MIN_SAMPLE_N,
    ProposerScorecard,
    read_epoch_scorecard,
    read_scorecard_trend,
)


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


def _rate(value: dict[str, Any] | None) -> str:
    """Render a rate cell as ``value (k/n)``, ``—`` for null, ``?`` when provisional.

    The sample count is never dropped and a null never renders as ``0`` — the
    two honesty rules the scorecard's whole shape exists to keep.
    """
    if not value:
        return "—"
    n = int(value.get("n", 0) or 0)
    k = int(value.get("k", 0) or 0)
    raw = value.get("value")
    if raw is None:
        return f"— (0/{n})" if n else "— (n=0)"
    mark = "?" if value.get("provisional") else ""
    return f"{float(raw) * 100:.0f}%{mark} ({k}/{n})"


def _num(value: Any, digits: int = 3) -> str:
    """Format an optional number; ``—`` for a null (never ``0``)."""
    if value is None:
        return "—"
    if isinstance(value, int | float):
        return f"{float(value):.{digits}f}"
    return str(value)


def _render_trend(cards: list[ProposerScorecard]) -> str:
    """The per-epoch trend table — one row per epoch, oldest first."""
    header = (
        f"{'epoch':<22}  {'proposer':<18}  {'rnds':>4}  {'promote':>14}  "
        f"{'valid-fail':>14}  {'screen-veto':>14}  {'margin':>9}"
    )
    lines = [header, "-" * len(header)]
    for card in cards:
        payload = card.to_json()
        lines.append(
            f"{card.epoch_id:<22}  {(card.proposer_agent_id or '—'):<18}  "
            f"{card.rounds:>4}  {_rate(payload['promote_rate']):>14}  "
            f"{_rate(payload['validation_failure_rate']):>14}  "
            f"{_rate(payload['screen_veto_rate']):>14}  "
            f"{_num(card.margins.achieved_median):>9}"
        )
    return "\n".join(lines)


def _render_card(card: ProposerScorecard) -> str:
    """The one-epoch detail block — the checks, the gate, the cost, the sites."""
    payload = card.to_json()
    skills = ", ".join(card.proposer_skills) or "(no skills)"
    lines = [
        f"Proposer scorecard · epoch {card.epoch_id}",
        f"  proposer      {card.proposer_agent_id or '—'} · {skills}",
        f"  rounds        {card.rounds} ({card.rounds_complete} complete)"
        f" · {card.proposals} proposal attempt(s)",
        "",
        "Validator failures per proposal attempt",
    ]
    for code, rate in sorted(payload["validator_failure_rates"].items()):
        lines.append(f"  {code:<14}{_rate(rate)}")
    lines += [
        f"  {'any check':<14}{_rate(payload['validation_failure_rate'])}",
        "",
        "Screen and revision",
        f"  {'screen veto':<14}{_rate(payload['screen_veto_rate'])}",
        f"  {'revise wins':<14}{_rate(payload['revision_success_rate'])}",
        "",
        "Gate margins of children that reached the gate",
        f"  achieved      median {_num(card.margins.achieved_median)}"
        f" · min {_num(card.margins.achieved_min)} · max {_num(card.margins.achieved_max)}",
        f"  headroom      median {_num(card.margins.headroom_median)}",
        f"  sample        n={card.margins.n}"
        f"{' (provisional)' if card.margins.provisional else ''}"
        f" · {card.margins.unmeasured} gate(s) recorded no scalars",
        "",
        "Cost per accepted proposal",
        f"  accepted      {card.cost.accepted}",
        f"  attempts      {_num(card.cost.attempts_per_acceptance, 2)}"
        f" (of {card.cost.proposal_attempts})",
        f"  board units   {_num(card.cost.units_per_acceptance, 2)}"
        f" (of {card.cost.board_units})",
    ]
    if card.mutation_sites:
        lines += ["", "Mutation sites (worst promote rate first)"]
        for site in card.mutation_sites:
            lines.append(f"  {site.mutation_id:<40}  {_rate(site.promote_rate.to_json())}")
    lines += [
        "",
        f"Rates over fewer than {MIN_SAMPLE_N} samples are marked '?' (provisional). "
        "A '—' is NOT zero — it means nothing was observed.",
    ]
    return "\n".join(lines)


@click.group(
    name="proposer",
    short_help="Advanced: score, diagnose, and improve the proposer itself.",
)
def proposer_grp() -> None:
    """Advanced: the proposer's scorecard and its recommend-only self-reflection.

    The evolve loop improves the TARGET. This command surface improves the
    PROPOSER — it reads the proposal-quality signals the loop already records,
    diagnoses mechanism-level weaknesses, and drafts edits to the proposer dir
    for the operator to apply at an epoch boundary.

    Nothing here ever applies an edit on its own, and nothing here runs
    mid-epoch: the proposer is frozen for its epoch, so a change to it is
    structurally an epoch-boundary event.
    """


@proposer_grp.command("scorecard", short_help="Per-epoch proposal quality + the trend.")
@click.option("--workspace", default=".zicato", show_default=True, help="Workspace root.")
@click.option("--epoch", "epoch_id", default=None, help="Epoch to detail (default: current).")
@click.option(
    "--trend/--no-trend",
    default=True,
    show_default=True,
    help="Also print the per-epoch trend table above the detail.",
)
@click.option(
    "--limit",
    default=10,
    show_default=True,
    type=click.IntRange(min=1),
    help="How many of the most recent epochs the trend covers.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the raw card dicts.")
def scorecard_cmd(
    workspace: str,
    epoch_id: str | None,
    trend: bool,
    limit: int,
    as_json: bool,
) -> None:
    """Render the proposer scorecard for one epoch, plus the cross-epoch trend.

    A pure read over the round logs, epoch configs, and experiments the loop
    already wrote — it starts no runs and spends no budget.
    """
    workspace_root, resolved_epoch = _resolve_workspace_epoch(workspace, epoch_id)
    card = read_epoch_scorecard(workspace_root, resolved_epoch)
    cards = read_scorecard_trend(workspace_root, limit=limit) if trend else []

    if as_json:
        click.echo(
            json.dumps(
                {"epoch": card.to_json(), "trend": [c.to_json() for c in cards]},
                indent=2,
                sort_keys=True,
            )
        )
        return
    if cards:
        click.echo(_render_trend(cards))
        click.echo("")
    click.echo(_render_card(card))


def _resolve_proposer_path(workspace_root: Path, epoch_id: str) -> Path | None:
    """The LIVE proposer dir a remedy would be written into, or ``None``.

    Read from the workspace config (``contract.proposer_path``) rather than
    from the epoch record: the epoch record pins the proposer that RAN, while a
    remedy is applied to the operator's live, editable copy — which is what the
    next epoch will freeze. They are usually the same directory, and when they
    are not, the live one is the correct target.
    """
    config_path = workspace_root / "config.json"
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    raw = (data.get("contract") or {}).get("proposer_path")
    if raw:
        return Path(str(raw))
    from zicato.epoch.lifecycle import load_epoch  # noqa: PLC0415

    try:
        return load_epoch(workspace_root, epoch_id).proposer_path
    except (OSError, ValueError, FileNotFoundError):
        return None


def _render_findings(reflection: Any) -> str:
    """Render one pass's findings the way ``zicato reflect`` renders its own."""
    if not reflection.findings:
        return (
            f"Proposer reflection {reflection.reflection_id} · epoch {reflection.epoch_id}\n"
            "  No findings — every measured rate is either healthy or too thinly sampled "
            "to draft a contract change against."
        )
    lines = [
        f"Proposer reflection {reflection.reflection_id} · epoch {reflection.epoch_id}",
        f"  substrate: {reflection.investigation_source}",
        "",
    ]
    for finding in reflection.findings:
        lines += [
            f"[{finding.severity}] {finding.finding_id}  {finding.title}",
            f"  population:       {finding.population}",
            f"  measured:         {json.dumps([dict(m) for m in finding.measured])}",
            f"  compared against: {finding.compared_against}",
        ]
        if finding.remedy is not None:
            lines.append(
                f"  recommendation:   {finding.remedy.kind} {finding.remedy.relative_path} "
                f"(sha256 {finding.remedy.sha256[:12]})"
            )
        else:
            lines.append("  recommendation:   none — this finding is for you to read, not apply")
        lines += [f"  remedy safety:    {finding.remedy_safety}", f"  {finding.detail}", ""]
    return "\n".join(lines)


@proposer_grp.command(
    "reflect",
    short_help="Recommend-only: diagnose the proposer and draft edits to it.",
)
@click.option("--workspace", default=".zicato", show_default=True, help="Workspace root.")
@click.option("--epoch", "epoch_id", default=None, help="Epoch to reflect on (default: current).")
@click.option(
    "--persist/--dry-run",
    default=True,
    show_default=True,
    help="Write the findings record, or derive and print it without touching disk.",
)
@click.option(
    "--draft-with-llm",
    "draft_spec",
    default=None,
    help=(
        "Dotted path to an auxiliary call_llm used to REFINE each remedy's prose. "
        "Optional — the drafted edits are complete without it."
    ),
)
@click.option("--model", default="", help="Model name passed to --draft-with-llm's callable.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the raw record.")
def reflect_cmd(
    workspace: str,
    epoch_id: str | None,
    persist: bool,
    draft_spec: str | None,
    model: str,
    as_json: bool,
) -> None:
    """Diagnose the proposer from its own scorecard and draft edits to its skills.

    Recommend-only, in the strong sense: this command has no code path to
    applying anything. It reads aggregate mechanism evidence — never board
    content — and writes findings whose remedy is a ready-to-apply diff. The
    operator applies one, at an epoch boundary, with
    `zicato proposer apply-recommendation`.
    """
    import asyncio  # noqa: PLC0415

    from zicato.proposer.reflection import draft_remedy, reflect, write_reflection  # noqa: PLC0415

    workspace_root, resolved_epoch = _resolve_workspace_epoch(workspace, epoch_id)
    proposer_path = _resolve_proposer_path(workspace_root, resolved_epoch)
    reflection = reflect(workspace_root, resolved_epoch, proposer_path=proposer_path)

    if draft_spec:
        from zicato.import_path import import_dotted_path  # noqa: PLC0415

        call_llm = import_dotted_path(draft_spec)
        if not callable(call_llm):
            raise click.ClickException(f"--draft-with-llm {draft_spec!r} is not callable")

        async def _redraft() -> tuple[Any, ...]:
            return tuple(
                [
                    await draft_remedy(
                        f, call_llm=call_llm, model=model, proposer_path=proposer_path
                    )
                    for f in reflection.findings
                ]
            )

        reflection = replace(reflection, findings=asyncio.run(_redraft()))

    if persist:
        path = write_reflection(workspace_root, reflection)
    else:
        path = None

    if as_json:
        click.echo(json.dumps(reflection.to_json(), indent=2, sort_keys=True))
        return
    click.echo(_render_findings(reflection))
    if path is not None:
        click.echo(f"Persisted to {path}")
    else:
        click.echo("Dry run — nothing was written.")


@proposer_grp.command(
    "recommendations",
    short_help="The pending queue: drafted, carries a diff, not yet applied.",
)
@click.option("--workspace", default=".zicato", show_default=True, help="Workspace root.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the raw records.")
def recommendations_cmd(workspace: str, as_json: bool) -> None:
    """List every drafted recommendation that has not been applied.

    This is the same list the epoch boundary prints — the boundary is when
    applying one is free, because the epoch is rolling anyway.
    """
    from zicato.proposer.reflection import (  # noqa: PLC0415
        pending_recommendations,
        render_recommendation_lines,
    )

    workspace_root = Path(workspace).resolve()
    pending = pending_recommendations(workspace_root)
    if as_json:
        click.echo(json.dumps({"pending": pending}, indent=2, sort_keys=True))
        return
    if not pending:
        click.echo(
            "No pending proposer recommendations. Run `zicato proposer reflect` to draft some."
        )
        return
    click.echo("\n".join(render_recommendation_lines(pending)))


@proposer_grp.command(
    "apply-recommendation",
    short_help="Write one drafted edit into the proposer dir (rolls the epoch).",
)
@click.argument("recommendation_id")
@click.option("--workspace", default=".zicato", show_default=True, help="Workspace root.")
@click.option("--epoch", "epoch_id", default=None, help="Epoch owning the recommendation.")
@click.option(
    "--proposer-path",
    "proposer_override",
    default=None,
    help="Proposer dir to write into (default: the workspace's registered one).",
)
@click.option(
    "--show-diff/--no-show-diff",
    default=True,
    show_default=True,
    help="Print the remedy's unified diff before writing.",
)
def apply_recommendation_cmd(
    recommendation_id: str,
    workspace: str,
    epoch_id: str | None,
    proposer_override: str | None,
    show_diff: bool,
) -> None:
    """Apply one recommendation's drafted edit to the proposer dir.

    This is the operator's gate — the only path that writes a recommendation
    into the proposer. Because the proposer dir folds into the contract hash,
    the edit is contract drift: the next `zicato evolve` closes the current
    epoch and opens a fresh one before proposing anything, and that new epoch's
    record carries this recommendation id.
    """
    from zicato.proposer.apply_recommendation import (  # noqa: PLC0415
        ApplyError,
        apply_recommendation,
    )
    from zicato.proposer.reflection import read_finding  # noqa: PLC0415

    workspace_root, resolved_epoch = _resolve_workspace_epoch(workspace, epoch_id)
    if proposer_override:
        proposer_path: Path | None = Path(proposer_override).resolve()
    else:
        proposer_path = _resolve_proposer_path(workspace_root, resolved_epoch)
    if proposer_path is None:
        raise click.ClickException(
            "this workspace has no proposer dir (it runs the built-in default proposer), "
            "so there is nothing to write a skill into. Create one and register it with "
            "`zicato register --proposer-path PATH`, or pass --proposer-path here."
        )

    if show_diff:
        located = read_finding(workspace_root, recommendation_id, epoch_id=epoch_id)
        diff = ((located[2].get("remedy") or {}) if located else {}).get("diff")
        if diff:
            click.echo(diff)

    try:
        applied = apply_recommendation(
            workspace_root,
            recommendation_id,
            proposer_path=proposer_path,
            epoch_id=epoch_id,
        )
    except ApplyError as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(
        f"Applied {applied.recommendation_id} ({applied.kind}) → {applied.path}\n"
        f"  sha256 {applied.sha256}\n"
        "  The proposer dir is a hashed contract input, so this edit rolls the epoch: the "
        "next `zicato evolve` opens a fresh one and stamps this id into its record."
    )


__all__ = ["proposer_grp"]
