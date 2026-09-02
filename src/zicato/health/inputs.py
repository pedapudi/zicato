"""Pure workspace readers that feed :func:`zicato.health.diagnostics.assess_loop_health`.

Each function here reads one already-persisted workspace record — the epoch's
pre-flight verdict, its measured noise floor, a generation's mutated-tree
import gaps, the operator's pre-flight gate mode — and shapes it into the
argument :func:`~zicato.health.diagnostics.assess_loop_health` expects.

They live here (not in :mod:`zicato.orchestrator`) so both the orchestrator's
per-round loop-health assessment AND the standalone ``zicato health`` CLI can
call the exact same readers instead of drifting apart: the orchestrator wants
these findings live, every round; the CLI wants them for a point-in-time
report run after the fact. Before this module existed the readers were
private to the orchestrator, so the CLI could only ever report the subset of
detectors fed by losses / experiments / board — the pre-flight, noise-floor,
and mutated-tree-import findings were invisible outside a live run.

Every reader is best-effort like the rest of the health path: a missing or
unreadable record degrades to the detector-silencing default rather than
raising, because a health-input read must never be the thing that aborts a
round or fails the CLI.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from zicato.health.diagnostics import SettlementReceiptAttention
from zicato.workspace import WorkspaceLayout, generation_ids

log = logging.getLogger("zicato.health.inputs")

__all__ = [
    "SettlementReceiptAttention",
    "epoch_noise_floor_inputs",
    "epoch_preflight_record",
    "epoch_settlement_receipt_attention",
    "epoch_tree_import_gaps",
    "str_tuple",
    "workspace_preflight_gate",
]


def epoch_settlement_receipt_attention(
    workspace_root: Path,
    epoch_id: str,
) -> SettlementReceiptAttention:
    """Scan retained settlement receipts once for all health conditions."""
    from zicato.evolve.settlement_recovery import scan_field_settlement_receipts  # noqa: PLC0415

    try:
        receipts, corruptions = scan_field_settlement_receipts(workspace_root, epoch_id)
    except Exception as exc:  # noqa: BLE001 — corruption is itself a health input
        log.debug("settlement receipts unreadable for %s: %s", epoch_id, exc)
        return SettlementReceiptAttention(
            corruptions=(
                {
                    "epoch_id": epoch_id,
                    "exception_type": type(exc).__name__,
                    "message": str(exc),
                },
            )
        )

    deliveries: list[dict[str, Any]] = []
    repairs: list[dict[str, Any]] = []
    for raw in receipts:
        if raw.get("state") != "committed":
            continue
        hook = raw.get("promotion_hook")
        if isinstance(hook, dict) and hook.get("state") == "delivery_unknown":
            field_record = raw.get("field_tournament_record")
            generation_id = (
                str(field_record.get("promoted_generation_id", ""))
                if isinstance(field_record, dict)
                else next(
                    (
                        str(candidate.get("generation_id", ""))
                        for candidate in raw.get("candidates", [])
                        if isinstance(candidate, dict)
                        and isinstance(candidate.get("outcome"), dict)
                        and candidate["outcome"].get("tournament_decision") == "promoted"
                    ),
                    "",
                )
            )
            deliveries.append(
                {
                    "settlement_id": str(raw.get("settlement_id", "")),
                    "round_index": raw.get("round_index"),
                    "generation_id": generation_id,
                    "adapter_name": str(hook.get("adapter_name", "")),
                }
            )
        projection = raw.get("index_projection")
        if isinstance(projection, dict) and projection.get("state") == "repair_required":
            repairs.append(
                {
                    "settlement_id": str(raw.get("settlement_id", "")),
                    "round_index": raw.get("round_index"),
                    "error_type": str(projection.get("error_type", "")),
                }
            )
    return SettlementReceiptAttention(tuple(deliveries), tuple(repairs), corruptions)


def str_tuple(raw: Any) -> tuple[str, ...]:
    """Coerce a JSON list of names to a tuple of non-empty strings.

    Tolerant by design — the records this reads are written by another process
    and a malformed / absent field must degrade to "nothing recorded", never
    raise into a round.
    """
    if not isinstance(raw, list):
        return ()
    return tuple(str(item) for item in raw if str(item))


def epoch_tree_import_gaps(workspace_root: Path, epoch_id: str) -> dict[str, tuple[str, ...]]:
    """Per-generation mutable trees NO unit of that generation ever imported.

    Reads each generation's ``harness_load.json`` — the worker-written record of
    what the generation actually loaded — and returns
    ``{generation_id: (tree_basename, ...)}`` for the generations with a
    non-empty gap. Threaded into
    :func:`zicato.health.diagnostics.detect_tree_never_imported`, which turns
    each entry into a WARNING: that generation's mutations to those trees
    cannot have been under test (issue #110's original shape — an installed
    entrypoint that never imports the mutated tree at all).

    Best-effort like every other health input: a missing / unreadable record
    contributes nothing.
    """
    from zicato.storage import read_json  # noqa: PLC0415

    gaps: dict[str, tuple[str, ...]] = {}
    layout = WorkspaceLayout.from_root(workspace_root)
    for generation_id in generation_ids(layout, epoch_id):
        try:
            record = read_json(layout.harness_load(epoch_id, generation_id))
        except Exception as exc:  # noqa: BLE001 — health inputs are best-effort
            log.debug("harness-load record unreadable for %s: %s", generation_id, exc)
            continue
        never_imported = str_tuple((record or {}).get("trees_never_imported"))
        if never_imported:
            gaps[generation_id] = never_imported
    return gaps


def epoch_noise_floor_inputs(
    workspace_root: Path, epoch_id: str
) -> tuple[dict[str, Any] | None, float | None, bool]:
    """Read the epoch's ``(noise_floor, promote_margin, evidence_gate_on)``.

    Threaded into :func:`zicato.health.diagnostics.detect_margin_below_noise_floor`
    so the per-round health report can warn when the contract's margin sits
    inside measured A/A noise. Best-effort: an unreadable epoch record yields
    ``(None, None, True)``, which keeps that detector silent.
    """
    from zicato.epoch.lifecycle import load_epoch  # noqa: PLC0415
    from zicato.selection.evidence_gate import (  # noqa: PLC0415
        read_promote_confidence_threshold,
    )

    try:
        cfg = load_epoch(workspace_root, epoch_id)
    except Exception:  # noqa: BLE001 — health inputs are best-effort
        return None, None, True
    gate_on = read_promote_confidence_threshold(cfg.scoring.tournament_structure.params) is not None
    return cfg.noise_floor, float(cfg.scoring.promote_margin), gate_on


def epoch_preflight_record(workspace_root: Path, epoch_id: str) -> dict[str, Any] | None:
    """Read the epoch's persisted contract pre-flight verdict, if any.

    Threaded into :func:`zicato.health.diagnostics.detect_preflight_verdict`
    so a REFUSE/saturation verdict stays visible in every round's health
    report. Best-effort: an unreadable epoch record yields ``None``, which
    keeps that detector silent.
    """
    from zicato.epoch.lifecycle import load_epoch  # noqa: PLC0415

    try:
        cfg = load_epoch(workspace_root, epoch_id)
    except Exception:  # noqa: BLE001 — health inputs are best-effort
        return None
    return cfg.preflight


def workspace_preflight_gate(workspace_root: Path) -> str:
    """Resolve ``runtime.preflight_gate`` for the health assessment.

    Threaded into :func:`zicato.health.diagnostics.detect_preflight_verdict`,
    which grades a persisted pre-flight REFUSAL ``critical`` only under the
    hard gate: under the default ``"warn"`` a critical would re-fire from the
    persisted record every round and trip the loop's degenerate-health
    breaker, silently converting the mode the operator chose into
    ``"refuse"``.

    Read from the workspace ``config.json``'s ``runtime`` block rather than
    from a live :class:`~zicato.core.runtime.RuntimeConfig` because that block
    is the knob's ONLY source (:func:`zicato.runtime_factory.make_runtime_config`
    reads it there and nowhere else) and because the health tail also runs on
    paths — a resume, the deferred-infra round, the standalone CLI — that
    hold no config object.
    Best-effort like the rest of the health path: anything unreadable yields
    the recommend-only default, never the severity that can stop a loop.
    """
    from zicato.core.runtime import PREFLIGHT_GATE_DEFAULT  # noqa: PLC0415

    try:
        from zicato.workspace.config_io import read_workspace_config  # noqa: PLC0415

        runtime_block = read_workspace_config(workspace_root).runtime
        return str(runtime_block.get("preflight_gate", PREFLIGHT_GATE_DEFAULT))
    except Exception as exc:  # noqa: BLE001 — health inputs are best-effort
        log.debug("preflight gate mode unavailable (%s); assuming the default", exc)
        return PREFLIGHT_GATE_DEFAULT
