"""Capture deterministic tournament execution for comparison with saved results.

The presentation example supplies the board, scoring contract, annotated source,
and deterministic proposer. Test adapters supply measured results without paid
model calls. Each configuration executes the real propose, apply, tournament,
holdout confirmation, and promotion sequence.

The configurations cover racing, gauntlet, Swiss, single elimination, and
double elimination. Racing and gauntlet also exercise measurement reuse.
A two-round racing configuration verifies that the promoted candidate supplies
the next round's incumbent and source. Every configuration has a nonempty
holdout so confirmation remains exercised.

Capture includes all generation scores and measurements, hypotheses and resolved
outcomes, round events, completed tournament structures, resolved lineage, and
the current champion. Completed round records supply outcomes and structures;
the capture uses the same canonical readers as other framework consumers.

Only timestamps, temporary paths, dates, and generated identifiers are normalized.
Scores, predicates, decisions, and match results must match the saved JSON.
ZICATO_PARITY_UPDATE=1 replaces saved results; reviewers must inspect those
changes independently of the passing capture.
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

#: The calendar date every capture pins ``epoch.lifecycle._today`` to. The
#: epoch id embeds it, and the holdout split is salted by the epoch id, so an
#: unpinned date would move each lane's board slice — and with it every
#: captured artifact — from one calendar day to the next.
PINNED_CAPTURE_DATE = "2026-01-01"

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
            epoch_name="t1-gauntlet-cached",
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
            epoch_name="t1-single-elim-bracket",
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


def example_dir() -> Path:
    """The vendored example target the captures bootstrap every lane from."""
    import zicato_examples.target_1_presentation as target_1

    return Path(target_1.__file__).resolve().parent


def lane_epoch_id(lane: Lane) -> str:
    """The epoch id ``lane`` runs under.

    The id is what salts the lane's holdout split, so it decides which board
    entries the lane holds back. Composed here the way
    ``epoch.lifecycle._make_epoch_id`` composes it, against the pinned date.
    """
    from zicato.epoch.lifecycle import _slugify

    return f"{PINNED_CAPTURE_DATE}_{_slugify(lane.epoch_name)}"


def lane_board_split(lane: Lane) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The ``(train_ids, holdout_ids)`` ``lane``'s board splits into.

    Reads the lane's own contract and the example board through the same
    loader and the same pure split rule the round uses, so the answer is the
    partition the capture will actually run under.
    """
    from zicato.board.jsonl import load_board
    from zicato.board.split import rotation_seed, split_board
    from zicato.epoch.lifecycle import _scoring_from_dict

    directory = example_dir()
    scoring_path = directory / lane.scoring_filename
    weights = _scoring_from_dict(json.loads(scoring_path.read_text(encoding="utf-8")))
    board = load_board(directory / "board.jsonl")
    epoch_id = lane_epoch_id(lane)
    return split_board(
        board, weights.overfitting, seed=rotation_seed(weights.overfitting, epoch_id)
    )


def _contract_replicates(scoring_path: Path) -> int:
    """How many replicate slots one duel of this contract runs per unit.

    A fast round only reuses the champion outright when EVERY requested
    slot is already cached, so the pre-seed has to know the contract's own
    replicate count rather than assume one.
    """
    from zicato.epoch.lifecycle import _scoring_from_dict
    from zicato.selection import make_strategy

    weights = _scoring_from_dict(json.loads(scoring_path.read_text(encoding="utf-8")))
    return int(
        make_strategy(
            weights.tournament_structure,
            experimental=weights.experimental,
        ).replicates()
    )


def _read_json_norm(path: Path, tmp_root: str) -> object | None:
    if not path.exists():
        return None
    return normalize_obj(json.loads(path.read_text(encoding="utf-8")), tmp_root=tmp_root)


def _collect_artifacts(workspace: Path, epoch_id: str) -> dict[str, object]:
    """Read measurements and resolved decisions, normalizing incidental identities."""
    from zicato.epoch.journal import read_experiment_body
    from zicato.epoch.lineage import load_lineage
    from zicato.evolve.generation_phase import current_generation
    from zicato.tournament.records import field_tournament_records

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

        body = read_experiment_body(workspace, epoch_id, gid)
        exp = normalize_obj(body, tmp_root=tmp_root) if body is not None else None
        if exp is not None:
            experiments[gid] = exp

        # Capture every persisted measurement, including independent draws.
        for loss_path in sorted(gen_dir.rglob("loss.*.r*.json")):
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

    field_tournaments = {
        f"field-{record.tournament_id.rsplit(':', 1)[-1]}.json": normalize_obj(
            record.to_dict(), tmp_root=tmp_root
        )
        for record in field_tournament_records(workspace, epoch_id)
    }
    lineage = normalize_obj(load_lineage(workspace).to_dict(), tmp_root=tmp_root)
    current_gen = current_generation(workspace, epoch_id)

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

    # Replicate the autouse fixture from tests/conftest.py that the racing
    # test relies on but that does not fire here (this module lives outside
    # tests/, so that conftest's autouse fixtures are not applied): neuter
    # the harmonograf auto-launch so evolve takes its JSONL-only telemetry
    # branch instead of spawning a real in-process server.
    #
    # The proposal runtime needs no fixture: the example workspace declares
    # the stand-in one in its own config.json, exactly as every other
    # fixture workspace does, so the captured lane runs a real episode per
    # candidate against the stand-in binary.
    import zicato.evolve.lifecycle_services as _lifecycle_services
    from tests._orchestrator_harness import (
        install_stub_adapter_factory,
        install_telemetry_stubs,
        target_call_llm,
    )
    from tests._stub_adapter import stub_adapter_pythonpath
    from tests.test_example_target_1_racing import (
        EXAMPLE_DIR,
        _install_caching_telemetry_stubs,
        _preseed_champion_cache,
        bootstrap_example_workspace,
    )
    from zicato_examples.target_1_presentation import mocks as example_mocks

    def _no_launch(workspace_root: Path, configuration) -> tuple[str, object]:
        del workspace_root, configuration
        return "", _lifecycle_services._NoopShutdownHandle()

    monkeypatch.setattr(_lifecycle_services, "_resolve_or_launch_harmonograf", _no_launch)

    # 3) Pin the epoch-id date. ``_make_epoch_id`` stamps ``datetime.now(UTC)``
    #    into the epoch id, and that id is returned by ``rotation_seed`` to seed
    #    the holdout split — so a lane's board slice (and therefore every
    #    captured artifact) shifts from one calendar day to the next.
    #    Freezing the date makes every golden date-stable; ``normalize.py`` still
    #    collapses the (now-constant) date prefix to ``<DATE>``.
    import zicato.epoch.lifecycle as _lifecycle_mod

    monkeypatch.setattr(_lifecycle_mod, "_today", lambda: PINNED_CAPTURE_DATE)

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
    install_stub_adapter_factory(monkeypatch, bypass_workspace_gate=False)
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
        install_telemetry_stubs(
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
            target_call_llm=target_call_llm,
            evaluation_call_llm=example_mocks.aux_llm,
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
    """Check contiguous rounds, recorded promotions, and incumbent carryover."""
    round_logs = artifacts["round_logs"]
    assert isinstance(round_logs, dict)
    assert sorted(round_logs) == [f"{n}/round_log.jsonl" for n in range(lane.rounds)]
    assert artifacts["current_generation"] == lane.crowned_generation_ids[-1]

    snapshots = artifacts["field_tournaments"]
    assert isinstance(snapshots, dict)
    assert len(snapshots) == lane.rounds

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
