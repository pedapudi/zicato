"""Inspect proposal quality from the round logs."""

from __future__ import annotations

import json
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
        "Proposal episodes by outcome",
    ]
    # Every kind is shown, including the ones this epoch never reached, so a
    # zero reads as "did not happen" rather than as a missing row.
    for kind in ("completed", "blocked", "exhausted", "errored"):
        lines.append(f"  {kind:<14}{card.episode_outcomes.get(kind, 0)}")
    for code, count in sorted(card.blocked_codes.items()):
        lines.append(f"    blocked: {code:<34}{count}")
    for limit, count in sorted(card.exhausted_limits.items()):
        lines.append(f"    exhausted: {limit:<32}{count}")
    lines += [
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
    short_help="Advanced: inspect proposal quality.",
)
def proposer_grp() -> None:
    """Inspect proposal quality by epoch."""


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


__all__ = ["proposer_grp"]
