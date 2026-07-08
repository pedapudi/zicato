"""Deterministic mock-evolve capture for the parity oracle (MOCK-GOLDEN gate).

This runs the SAME deterministic, no-live-LLM racing evolve that
``tests/test_example_target_1_racing.py`` drives — the real
``target_1_presentation`` example contract (board + ``scoring.racing.json``
+ annotated ``agent/`` tree + the example's ``mocks.aux_llm`` proposer),
under the racing (successive-halving) structure, with the inner harness +
loss reducer mocked exactly as the orchestrator test suite mocks them.

It then collects the produced ``.zicato`` artifacts — every generation's
``gen_score.json`` (the per-generation SCORE: scalar + components + the
per-board-entry drift_loss / score / pass_fail), every ``experiment.json``
(the hypothesis + the tournament ``outcome`` / per-match audit), any
per-run ``loss.json``, and the workspace ``lineage.json`` — normalizes the
handful of wall-clock / tmp-path / date / uuid fields (see ``normalize.py``)
and emits ONE canonical JSON document. With ``ZICATO_PARITY_UPDATE=1`` it
writes that document to the golden; otherwise it asserts byte-identity
against the committed golden.

Why this is the strongest single end-to-end gate
-------------------------------------------------
Unlike the unit suite, this exercises the full orchestrated path —
propose N challengers off v0, apply the real proposer patches against the
real mutation markers, run the racing rungs + cuts on board slices, crown
a survivor through the champion gate, and persist the whole audit — and
freezes the EXACT serialized bytes of every decision artifact. A refactor
that changes any loss, any scalar, any decision, any id, any structural
field, or any serialization detail moves these bytes and fails the gate.

Note on artifact names: the task brief names ``loss.json`` and
``gen_score.json``. Under this racing/directory-backend mock the
per-generation score is persisted as ``gen_score.json`` (the canonical
home of the score: scalar, scalar_components, per_entry drift_loss /
pass_fail / score, namespace_aggregates, pass_rate). No per-run
``loss.json`` is written on this path, so the ``losses`` map is captured
but empty here; the collector still globs for any ``loss.json`` so the
gate covers it if a future change starts writing them.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

# tools/parity/lib -> repo root is three parents up.
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT / "tools" / "parity" / "lib") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "tools" / "parity" / "lib"))

from normalize import normalize_obj  # noqa: E402

GOLDEN_PATH = _REPO_ROOT / "tools" / "parity" / "golden" / "mock_evolve_racing.json"


def _read_json_norm(path: Path, tmp_root: str) -> object | None:
    if not path.exists():
        return None
    return normalize_obj(json.loads(path.read_text(encoding="utf-8")), tmp_root=tmp_root)


def _collect_artifacts(workspace: Path, epoch_id: str) -> dict[str, object]:
    """Read every decision artifact the mock evolve persisted, normalized.

    Returns a single canonical dict keyed by generation id so the golden is
    a flat, diffable map. The tmp workspace root is passed to the normalizer
    so embedded absolute paths collapse to ``<TMP>`` and the date-prefixed
    epoch id collapses to ``<DATE>_...``.

    Artifacts captured per generation:

    * ``gen_score.json`` — the canonical per-generation SCORE: the scalar,
      its per-namespace components, the per-board-entry drift_loss / score /
      pass_fail, the pass rate, and the drift-loss mean. This is the single
      richest behavioral surface in the run; a refactor that moves any loss,
      weight, aggregate, or pass predicate moves these bytes.
    * ``experiment.json`` — the hypothesis + the tournament ``outcome`` /
      per-match audit (train_loss, scalar_score_delta, match_record, final
      rank, decision).
    * ``loss.json`` (if the run wrote any per-run ones) — the raw reducer
      LossProfile.
    """
    tmp_root = str(workspace.resolve())
    gens_dir = workspace / "epochs" / epoch_id / "generations"

    experiments: dict[str, object] = {}
    gen_scores: dict[str, object] = {}
    losses: dict[str, object] = {}

    for gen_dir in sorted(p for p in gens_dir.iterdir() if p.is_dir()):
        gid = gen_dir.name

        score = _read_json_norm(gen_dir / "gen_score.json", tmp_root)
        if score is not None:
            gen_scores[gid] = score

        exp = _read_json_norm(gen_dir / "experiment.json", tmp_root)
        if exp is not None:
            experiments[gid] = exp

        # Per-run loss.json, wherever the run wrote it under the generation.
        for loss_path in sorted(gen_dir.rglob("loss.json")):
            key = str(loss_path.relative_to(gens_dir))
            losses[key] = _read_json_norm(loss_path, tmp_root)

    lineage = _read_json_norm(workspace / "lineage.json", tmp_root)

    current_gen_path = workspace / "epochs" / epoch_id / "current_generation"
    current_gen = (
        current_gen_path.read_text(encoding="utf-8").strip() if current_gen_path.exists() else None
    )

    return {
        "current_generation": current_gen,
        "gen_scores": gen_scores,
        "experiments": experiments,
        "losses": losses,
        "lineage": lineage,
    }


def drive_mock_evolve(monkeypatch, tmp_path: Path) -> tuple[Path, str]:
    """Run the deterministic racing mock evolve; return (workspace, epoch_id).

    The shared engine behind both the MOCK-GOLDEN gate (which reads the
    persisted artifacts) and the REINDEX-DUMP gate (which rebuilds the
    SQLite index from this same on-disk workspace and dumps it).

    Reuses the exact harness mocks + bootstrap from the racing example test
    so the captured behavior is identical to what the unit suite asserts.
    """
    # Ensure the repo's tests/ package is importable for the shared helpers.
    if str(_REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(_REPO_ROOT))

    from tests.test_example_target_1_racing import (
        _CHALLENGER_IDS,
        _bootstrap_racing_workspace,
        _make_example_aux_responder,
    )
    from tests.test_orchestrator import (
        _harness_call_llm,
        _install_stub_adapter_factory,
        _install_telemetry_stubs,
    )

    # Replicate the two autouse fixtures from tests/conftest.py that the
    # racing test relies on but that do not fire here (this module lives
    # outside tests/, so that conftest's autouse fixtures are not applied).
    #
    # 1) Pin the builtin-default proposer to the text-shim engine driven by
    #    the stubbed auxiliary callable — otherwise the default proposer is
    #    the live ADK tool agent and tries to reach a real model (no key).
    # 2) Neuter the harmonograf auto-launch so evolve takes its JSONL-only
    #    telemetry branch instead of spawning a real in-process server.
    from zicato.core.types import ProposerSpec
    from zicato.proposer import agent as _proposer_agent_mod

    _real_build = _proposer_agent_mod.build_proposer_agent

    def _build_proposer(spec: ProposerSpec, proposer_path: Path | None = None) -> object:
        if spec == ProposerSpec.default():
            return _proposer_agent_mod.DefaultProposerAgent(spec)
        return _real_build(spec, proposer_path)

    monkeypatch.setattr(_proposer_agent_mod, "build_proposer_agent", _build_proposer)

    import zicato.orchestrator as _orchestrator_mod

    def _no_launch(workspace_root: Path) -> tuple[str, object]:
        del workspace_root
        return "", _orchestrator_mod._NoopShutdownHandle()

    monkeypatch.setattr(_orchestrator_mod, "_resolve_or_launch_harmonograf", _no_launch)

    # 3) Pin the epoch-id date. ``_make_epoch_id`` stamps ``datetime.now(UTC)``
    #    into the epoch id, and that id is returned by ``rotation_seed`` to seed
    #    the holdout split — so the racing rung's board slice (and therefore
    #    every captured artifact) shifts from one calendar day to the next.
    #    Freezing the date makes both goldens date-stable; ``normalize.py`` still
    #    collapses the (now-constant) date prefix to ``<DATE>``.
    import zicato.epoch.lifecycle as _lifecycle_mod

    monkeypatch.setattr(_lifecycle_mod, "_today", lambda: "2026-01-01")

    workspace, epoch_id = _bootstrap_racing_workspace(tmp_path)
    _install_stub_adapter_factory(monkeypatch)
    _install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 0.4, "v2": 0.8, "v3": 1.2, "v4": 1.6},
        canned_pass_by_gen={gid: True for gid in ("v0", *_CHALLENGER_IDS)},
    )

    from zicato.orchestrator import evolve_once

    outcome = asyncio.run(
        evolve_once(
            workspace_root=workspace,
            epoch_id=epoch_id,
            harness_call_llm=_harness_call_llm,
            auxiliary_call_llm=_make_example_aux_responder(),
        )
    )
    # Sanity: the same crowning the unit test asserts. If this ever drifts,
    # the artifact diff will already have failed, but assert here too so a
    # broken capture is obvious.
    assert outcome.tournament_decision == "promoted"
    assert outcome.proposed_generation_id == "v1"

    return workspace, epoch_id


def run_mock_evolve(monkeypatch, tmp_path: Path) -> dict[str, object]:
    """Drive the deterministic racing mock evolve and return its artifacts."""
    workspace, epoch_id = drive_mock_evolve(monkeypatch, tmp_path)
    return _collect_artifacts(workspace, epoch_id)
