"""Deterministic mock-evolve capture for the parity oracle (MOCK-GOLDEN gates).

This runs a deterministic, no-live-LLM evolve of the real
``target_1_presentation`` example contract (board + a scoring contract +
annotated ``agent/`` tree + the example's ``mocks.aux_llm`` proposer), with
the inner harness + loss reducer mocked exactly as the orchestrator test
suite mocks them. It is the same drive
``tests/test_example_target_1_racing.py`` performs, generalised over the
three axes that select which production branches execute: the tournament
structure the frozen contract declares, the runtime mode, and how many
rounds the invocation runs.

Lanes
-----
:data:`LANES` names one capture per (structure, mode, round count) triple,
each with its own golden. The lanes are not interchangeable — they run
different code:

* ``racing_full`` — a four-challenger racing field under ``--mode full``.
  The multi-challenger rungs, cuts, and crowning duel.
* ``gauntlet_full`` — one challenger under ``--mode full``. The
  ``field_n == 1`` branches: the full-board (not train-slice) selector and
  the crowning holdout confirmation a single-challenger full round still
  runs.
* ``gauntlet_fast`` — one challenger under ``--mode fast``. The cache-first
  slot resolution, and the one place holdout confirmation is skipped
  (``field_n == 1 and fast_mode``).
* ``racing_fast`` — a four-challenger field under ``--mode fast``, where
  every rung resolves both competitors through the unit cache.
* ``two_round_racing`` — the racing field under ``--mode full`` for TWO
  rounds. The only lane where a round runs against a parent that a
  previous round crowned, so it is the only one that pins the between-round
  carry-over: the promoted-head pointer advancing off the seeded ``v0``,
  the crowned generation defending as champion in the next round, that
  generation's patched snapshot supplying the next round's mutable
  surface, the epoch's round directories numbering on from ``0``, and the
  second round's settled snapshot recording the first round's winner as
  its champion.
* ``swiss_full`` — a four-challenger Swiss field under ``--mode full``:
  fixed-round pairings over champion + challengers, Copeland standings,
  and a final champion-gate confirmation of the leader.
* ``single_elim_full`` — a four-challenger single-elimination bracket under
  ``--mode full``: challenger-vs-challenger nodes with no incumbent, then
  the champion-vs-survivor final.
* ``double_elim_full`` — a four-challenger double-elimination field under
  ``--mode full``: winners' bracket, losers' bracket, grand final, then the
  champion gate.

The last three structures reach the unified round pipeline through
registries that no other lane exercises end to end.

It then collects the produced ``.zicato`` artifacts:

* every generation's ``gen_score.json`` — scalar, components, and the
  per-board-entry drift_loss / score / pass_fail;
* every ``experiment.json`` — the hypothesis, the tournament ``outcome``,
  and the per-match audit;
* any per-run ``loss.json``, and each round's ``round_log.jsonl``;
* each settled field-tournament snapshot, carrying the round's recorded
  ``promoted_generation_id`` / ``champion_generation_id``, the head the
  dashboard serves;
* the workspace ``lineage.json``.

It normalizes the wall-clock / tmp-path / date / uuid fields (see
``normalize.py``) and emits ONE canonical JSON document. With
``ZICATO_PARITY_UPDATE=1`` it writes that document to the golden; otherwise
it asserts byte-identity against the committed golden.

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
from typing import NamedTuple

# tools/parity/lib -> repo root is three parents up.
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT / "tools" / "parity" / "lib") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "tools" / "parity" / "lib"))

from normalize import normalize_obj  # noqa: E402

_GOLDEN_DIR = _REPO_ROOT / "tools" / "parity" / "golden"

#: The racing full-mode golden, kept under its original name so the gate
#: that has always guarded it keeps guarding the same file.
GOLDEN_PATH = _GOLDEN_DIR / "mock_evolve_racing.json"


class Lane(NamedTuple):
    """One capture configuration and the golden it is compared against.

    ``scoring_filename`` names the example contract file that decides the
    tournament structure; ``fast_mode`` is the runtime ``--mode`` setting;
    ``rounds`` is how many evolve rounds the invocation runs.
    ``minted_generation_ids`` lists every generation the lane produces
    across all its rounds, needed to can a loss and a pass verdict per
    generation before the run starts.
    ``crowned_generation_ids`` is the generation crowned in each round, in
    round order, so the capture can assert the champion pointer advanced
    the way the lane exists to pin.
    """

    name: str
    scoring_filename: str
    fast_mode: bool
    rounds: int
    minted_generation_ids: tuple[str, ...]
    crowned_generation_ids: tuple[str, ...]
    golden_filename: str
    epoch_name: str

    @property
    def golden_path(self) -> Path:
        return _GOLDEN_DIR / self.golden_filename


#: Every captured lane, keyed by name. Each drives production branches no
#: other lane reaches; see the module docstring. The lane names are also the
#: ``-k`` selectors ``tools/parity.sh`` runs each gate with, so no name may
#: be a substring of another.
LANES: dict[str, Lane] = {
    lane.name: lane
    for lane in (
        Lane(
            name="racing_full",
            scoring_filename="scoring.racing.json",
            fast_mode=False,
            rounds=1,
            minted_generation_ids=("v1", "v2", "v3", "v4"),
            crowned_generation_ids=("v1",),
            # The original golden's filename, kept so the gate that has
            # always guarded this capture keeps guarding the same file.
            golden_filename="mock_evolve_racing.json",
            # The original capture's epoch name, kept for the same reason as
            # the golden filename: the epoch id is stamped into every
            # captured artifact, so renaming it would rewrite this golden
            # wholesale and hide any real drift in the noise.
            epoch_name="t1-racing",
        ),
        Lane(
            name="gauntlet_full",
            scoring_filename="scoring.json",
            fast_mode=False,
            rounds=1,
            minted_generation_ids=("v1",),
            crowned_generation_ids=("v1",),
            golden_filename="mock_evolve_gauntlet_full.json",
            epoch_name="t1-gauntlet",
        ),
        Lane(
            name="gauntlet_fast",
            scoring_filename="scoring.json",
            fast_mode=True,
            rounds=1,
            minted_generation_ids=("v1",),
            crowned_generation_ids=("v1",),
            golden_filename="mock_evolve_gauntlet_fast.json",
            epoch_name="t1-gauntlet-fast",
        ),
        Lane(
            name="racing_fast",
            scoring_filename="scoring.racing.json",
            fast_mode=True,
            rounds=1,
            minted_generation_ids=("v1", "v2", "v3", "v4"),
            crowned_generation_ids=("v1",),
            golden_filename="mock_evolve_racing_fast.json",
            epoch_name="t1-racing-fast",
        ),
        Lane(
            name="two_round_racing",
            scoring_filename="scoring.racing.json",
            fast_mode=False,
            rounds=2,
            # Round 0 mints v1..v4 off the seeded v0; round 1 mints v5..v8
            # off the generation round 0 crowned.
            minted_generation_ids=("v1", "v2", "v3", "v4", "v5", "v6", "v7", "v8"),
            crowned_generation_ids=("v1", "v5"),
            golden_filename="mock_evolve_two_round_racing.json",
            epoch_name="t1-racing-two-round",
        ),
        Lane(
            name="swiss_full",
            scoring_filename="scoring.swiss.json",
            fast_mode=False,
            rounds=1,
            minted_generation_ids=("v1", "v2", "v3", "v4"),
            crowned_generation_ids=("v1",),
            golden_filename="mock_evolve_swiss_full.json",
            epoch_name="t1-swiss",
        ),
        Lane(
            name="single_elim_full",
            scoring_filename="scoring.single_elim.json",
            fast_mode=False,
            rounds=1,
            minted_generation_ids=("v1", "v2", "v3", "v4"),
            crowned_generation_ids=("v1",),
            golden_filename="mock_evolve_single_elim_full.json",
            epoch_name="t1-single-elim",
        ),
        Lane(
            name="double_elim_full",
            scoring_filename="scoring.double_elim.json",
            fast_mode=False,
            rounds=1,
            minted_generation_ids=("v1", "v2", "v3", "v4"),
            crowned_generation_ids=("v1",),
            golden_filename="mock_evolve_double_elim_full.json",
            epoch_name="t1-double-elim",
        ),
    )
}


def _contract_replicates(scoring_path: Path) -> int:
    """How many replicate slots one duel of this contract runs per unit.

    A fast round only reuses the champion outright when EVERY requested
    slot is already cached, so the pre-seed has to know the contract's own
    replicate count rather than assume one.
    """
    from zicato.epoch.lifecycle import _scoring_from_dict
    from zicato.selection import make_strategy

    weights = _scoring_from_dict(json.loads(scoring_path.read_text(encoding="utf-8")))
    return int(make_strategy(weights.tournament_structure).replicates())


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

    And two per-round artifacts, which are not generation-scoped:

    * ``rounds/{n}/round_log.jsonl`` — the round's ordered event log
      (proposal attempts, patches applied, gate evaluated, holdout
      released, decision recorded). It is the record the execution-plan and
      proposer-scorecard readers fold, so a refactor that stops emitting an
      event, or reorders them, moves these lines.
    * ``tournaments/field-*.json`` — the settled field-tournament snapshot,
      which carries the round's recorded ``promoted_generation_id`` and
      ``champion_generation_id``: the head the dashboard serves and the
      index re-derives. Nothing else on disk states the promoted head as a
      decided fact rather than a derivation.
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

    epoch_dir = workspace / "epochs" / epoch_id

    round_logs: dict[str, object] = {}
    for log_path in sorted((epoch_dir / "rounds").rglob("round_log.jsonl")):
        key = str(log_path.relative_to(epoch_dir / "rounds"))
        round_logs[key] = [
            normalize_obj(json.loads(line), tmp_root=tmp_root)
            for line in log_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    field_tournaments: dict[str, object] = {}
    tournaments_dir = epoch_dir / "tournaments"
    if tournaments_dir.is_dir():
        for snapshot_path in sorted(tournaments_dir.glob("field-*.json")):
            field_tournaments[snapshot_path.name] = _read_json_norm(snapshot_path, tmp_root)

    lineage = _read_json_norm(workspace / "lineage.json", tmp_root)

    current_gen_path = epoch_dir / "current_generation"
    current_gen = (
        current_gen_path.read_text(encoding="utf-8").strip() if current_gen_path.exists() else None
    )

    return {
        "current_generation": current_gen,
        "gen_scores": gen_scores,
        "experiments": experiments,
        "losses": losses,
        "round_logs": round_logs,
        "field_tournaments": field_tournaments,
        "lineage": lineage,
    }


def drive_mock_evolve(
    monkeypatch, tmp_path: Path, lane: Lane = LANES["racing_full"]
) -> tuple[Path, str]:
    """Run one lane's deterministic mock evolve; return (workspace, epoch_id).

    Drives ``lane.rounds`` rounds through :func:`zicato.orchestrator.evolve_n_rounds`.

    The shared engine behind the MOCK-GOLDEN gates (which read the persisted
    artifacts) and the REINDEX-DUMP gate (which rebuilds the SQLite index
    from this same on-disk workspace and dumps it). REINDEX-DUMP takes the
    default lane, so its golden is a projection of the single-round racing
    full-mode workspace.

    Reuses the harness mocks + bootstrap from the example test so the
    captured behavior is identical to what the unit suite asserts.
    """
    # Ensure the repo's tests/ package is importable for the shared helpers.
    if str(_REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(_REPO_ROOT))

    from tests._stub_adapter import stub_adapter_pythonpath
    from tests.test_example_target_1_racing import (
        EXAMPLE_DIR,
        _install_caching_telemetry_stubs,
        _make_example_aux_responder,
        _preseed_champion_cache,
        bootstrap_example_workspace,
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

    def _build_proposer(
        spec: ProposerSpec,
        proposer_path: Path | None = None,
        external_config: object = None,
    ) -> object:
        if spec == ProposerSpec.default():
            return _proposer_agent_mod.DefaultProposerAgent(spec)
        return _real_build(spec, proposer_path, external_config)  # type: ignore[arg-type]

    monkeypatch.setattr(_proposer_agent_mod, "build_proposer_agent", _build_proposer)

    import zicato.evolve.lifecycle_services as _lifecycle_services

    def _no_launch(workspace_root: Path) -> tuple[str, object]:
        del workspace_root
        return "", _lifecycle_services._NoopShutdownHandle()

    monkeypatch.setattr(_lifecycle_services, "_resolve_or_launch_harmonograf", _no_launch)

    # 3) Pin the epoch-id date. ``_make_epoch_id`` stamps ``datetime.now(UTC)``
    #    into the epoch id, and that id is returned by ``rotation_seed`` to seed
    #    the holdout split — so the racing rung's board slice (and therefore
    #    every captured artifact) shifts from one calendar day to the next.
    #    Freezing the date makes both goldens date-stable; ``normalize.py`` still
    #    collapses the (now-constant) date prefix to ``<DATE>``.
    import zicato.epoch.lifecycle as _lifecycle_mod

    monkeypatch.setattr(_lifecycle_mod, "_today", lambda: "2026-01-01")

    workspace, epoch_id = bootstrap_example_workspace(
        tmp_path,
        scoring_path=EXAMPLE_DIR / lane.scoring_filename,
        epoch_name=lane.epoch_name,
    )
    # 4) Run through the REAL pre-spend workspace gate rather than patching
    #    it out. This capture drives ``evolve_n_rounds``, which gates itself
    #    once per invocation, so a byte-identical result across a change to
    #    the gate is only meaningful if the gate actually ran. Satisfying it
    #    costs one thing: the gate rebuilds the adapter in a subprocess the
    #    way a tournament worker does, so that subprocess must be able to
    #    import the stub adapter's module wherever this capture was started
    #    from.
    monkeypatch.setenv("PYTHONPATH", stub_adapter_pythonpath())
    _install_stub_adapter_factory(monkeypatch, bypass_workspace_gate=False)
    # Strictly-descending challenger losses: v1 is the best arm, so it
    # survives every racing rung and clears the champion gate — and it is
    # also the single challenger a gauntlet lane mints. A lane whose field
    # is smaller simply leaves the later canned losses unused.
    #
    # v5..v8 are the field a SECOND round mints off the crowned v1. Their
    # losses are strictly below v1's, and descending among themselves, so
    # the second round reaches the same decisive shape as the first: v5 is
    # the best arm, it survives the rungs, and it unseats the reigning
    # champion. Without a loss below the incumbent's, a second round could
    # only ever reject, and the lane would pin nothing about the promoted
    # head advancing twice. Single-round lanes never mint these ids, so the
    # extra entries are never read on those lanes.
    canned_loss_by_gen = {
        "v0": 2.0,
        "v1": 0.4,
        "v2": 0.8,
        "v3": 1.2,
        "v4": 1.6,
        "v5": 0.10,
        "v6": 0.20,
        "v7": 0.25,
        "v8": 0.30,
    }
    canned_pass_by_gen = {gid: True for gid in ("v0", *lane.minted_generation_ids)}

    if lane.fast_mode:
        # A fast round's whole point is reusing the champion's already-scored
        # per-board units. The default telemetry stub makes every cache read
        # raise, which silently degrades fast mode to full — so a fast lane
        # would capture the full-mode path under a fast-mode name. These two
        # calls give the lane a real cache: the champion's per-board
        # ``loss.json`` for every replicate slot a prior full round would have
        # written, and a reducer stub that actually persists and reads back.
        # The pre-seed must run BEFORE the stub swap (it imports the real
        # writer).
        _preseed_champion_cache(
            workspace,
            epoch_id,
            champion_id="v0",
            drift_loss=canned_loss_by_gen["v0"],
            pass_fail=True,
            replicates=_contract_replicates(EXAMPLE_DIR / lane.scoring_filename),
        )
        _install_caching_telemetry_stubs(
            monkeypatch,
            canned_loss_by_gen=canned_loss_by_gen,
            canned_pass_by_gen=canned_pass_by_gen,
        )
    else:
        _install_telemetry_stubs(
            monkeypatch,
            canned_loss_by_gen=canned_loss_by_gen,
            canned_pass_by_gen=canned_pass_by_gen,
        )

    from zicato.orchestrator import evolve_n_rounds

    # Every lane runs through the multi-round loop, single-round lanes
    # included: for ``rounds=1`` the loop's artifacts are byte-identical to
    # a bare ``evolve_once`` (the four original goldens are unchanged by the
    # switch), so one drive covers both and ``rounds`` stays an ordinary
    # lane parameter rather than a second code path.
    outcomes = asyncio.run(
        evolve_n_rounds(
            rounds=lane.rounds,
            workspace_root=workspace,
            epoch_id=epoch_id,
            harness_call_llm=_harness_call_llm,
            auxiliary_call_llm=_make_example_aux_responder(),
            fast_mode=lane.fast_mode,
        )
    )
    # Sanity: the crownings this lane exists to capture. If any of them ever
    # drifts, the artifact diff will already have failed, but assert here
    # too so a broken capture is obvious.
    assert len(outcomes) == lane.rounds
    assert [o.tournament_decision for o in outcomes] == ["promoted"] * lane.rounds
    assert tuple(o.proposed_generation_id for o in outcomes) == lane.crowned_generation_ids
    # The champion pointer advanced: round 0 defends the seeded ``v0``, and
    # every later round defends the generation the previous round crowned.
    # This is the between-round carry-over the multi-round lane exists for;
    # on a single-round lane it degenerates to "the parent was v0".
    assert tuple(o.parent_generation_id for o in outcomes) == (
        "v0",
        *lane.crowned_generation_ids[:-1],
    )

    return workspace, epoch_id


def _assert_round_carryover(artifacts: dict[str, object], lane: Lane) -> None:
    """Assert the persisted per-round record matches the lane's round count.

    The in-memory outcomes are already checked in :func:`drive_mock_evolve`;
    this checks the same carry-over as it was WRITTEN DOWN, which is what
    the golden freezes and what every downstream reader (dashboard, index,
    execution plan) actually consumes:

    * the epoch holds one ``round_log.jsonl`` per round, numbered from 0
      with no gaps — the round-numbering continuity only a second round
      can break;
    * the promoted head named by the last round's settled field snapshot is
      the workspace's current generation;
    * each round's settled field snapshot records the PREVIOUS round's
      crowned generation as its champion.

    A gauntlet lane writes no ``field-*.json`` snapshot (that path settles
    elsewhere), so the snapshot checks apply only where snapshots exist.
    """
    round_logs = artifacts["round_logs"]
    assert isinstance(round_logs, dict)
    assert sorted(round_logs) == [f"{n}/round_log.jsonl" for n in range(lane.rounds)]
    assert artifacts["current_generation"] == lane.crowned_generation_ids[-1]

    snapshots = artifacts["field_tournaments"]
    assert isinstance(snapshots, dict)
    if not snapshots:
        return

    expected_champions = ("v0", *lane.crowned_generation_ids[:-1])
    for crowned, champion in zip(lane.crowned_generation_ids, expected_champions, strict=True):
        snapshot = snapshots[f"field-{crowned}.json"]
        assert isinstance(snapshot, dict)
        assert snapshot["promoted_generation_id"] == crowned
        assert snapshot["champion_generation_id"] == champion


def run_mock_evolve(monkeypatch, tmp_path: Path, lane: Lane) -> dict[str, object]:
    """Drive one lane's deterministic mock evolve and return its artifacts."""
    workspace, epoch_id = drive_mock_evolve(monkeypatch, tmp_path, lane)
    artifacts = _collect_artifacts(workspace, epoch_id)
    _assert_round_carryover(artifacts, lane)
    return artifacts
