"""The admission pipeline for eval synthesis (generative reflection).

The instrument's second loop (EVAL-SYNTHESIS.md) turns observed behaviour into
drafted instrument changes; admission is the step that attaches **measured
operating characteristics** to a drafted suggestion *before the operator ever
sees it* — the "evals are hypotheses too" move made mechanical (§1, §5). Four
recommend-only probes ride the suggestion; thresholds are the operator's read
on the suggestion surface, never a silent auto-reject here:

* **(a) EXECUTION** — the champion runs the drafted entry through the REAL
  board-unit runner (:func:`zicato.tournament.scheduling._run_board_units_fast`,
  the same path the candidate screen / calibration / corpus draws compose),
  confirming it executes and produces a ``LossProfile``.
* **(b) A/A NOISE** — K replicate draws of the champion against itself on JUST
  the drafted entry, at the NEW reserved base :data:`SYNTHESIS_REPLICATE_BASE`
  (``6000``), folded to a per-entry flip rate via the pure
  :func:`zicato.query.eval_view.flip_rate` helper. Draw 0 IS the execution
  probe (a shared reserved cache slot), so the two stages spend one run
  between them.
* **(c) DISCRIMINATION** — the drafted entry runs against a spread of recent
  SETTLED candidates (the reign's settled matchups, re-derived from records —
  the recombination builder's reconstruction precedent) and we count how many
  ``(champion, challenger)`` pairs it separates (the MATCHUP-RECORD method,
  EVAL-VIEW.md §2.3 — never ``loss_profiles`` pairs).
* **(d) LEAKAGE / COLLUSION** — the §4 rotation rule (a coverage / judge /
  harder-variant suggestion must land in the incoming-rotation slice the
  motivating proposer has not seen — :func:`zicato.board.split.split_board`
  membership), the emulator collusion guard (a ``multi_turn_emulated`` draft
  opts in through its :class:`~zicato.core.board.UserPersona`, and
  :func:`zicato.core.workspace.assert_distinct_callables` must hold), and the
  §4 self-preference rule (a judge suggestion whose expected answer and judge
  share a model family is FLAGGED, never silently admitted).

**Plan vs spend.** The public entry point :func:`admit_suggestion` takes an
explicit ``spend: bool``. In ``spend=False`` (plan) mode it computes every
stage's board-run cost up front (:func:`estimate_cost`) and runs NOTHING — zero
board runs — returning the cost with every live stage ``unmeasured``. Only
``spend=True`` executes the probes, which need the operator's explicit
go-ahead because they spend live budget; the suggestion surface's CLI gates
them. The whole pipeline is fully testable against the fixture/mock tier:
the probes drive the real runner over a seeded ``_run_single`` (the cascade-OC /
power-harness precedent), so every statistic has a known-answer test with zero
live spend.

**Honest degrades.** A stage that cannot run — a cold workspace, no settled
candidates, an unreconstructable tree, an aborted draw — stamps ``measured:
false`` (``unmeasured``) rather than fabricating a number. Admission MEASURES
and stamps; it never auto-rejects.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zicato.core import BoardEntry, Generation, JudgeSpec, RuntimeConfig, ScoringWeights
from zicato.query.eval_view import flip_rate
from zicato.reflection.mining import (
    HINT_COVERAGE_ENTRY,
    HINT_HARDER_VARIANT,
    HINT_JUDGE,
    HINT_REGRESSION_ENTRY,
    HINT_RUBRIC_REVISION,
)

#: Replicate-index base for eval-synthesis admission probes — the A/A noise
#: draws (``SYNTHESIS_REPLICATE_BASE + j``) and the discrimination-probe draws
#: (each candidate side at :data:`SYNTHESIS_REPLICATE_BASE`, keyed distinct by
#: generation, so cross-generation draws never collide). This is the NEXT FREE
#: base in the reserved-replicate ledger (dev-guide ``04-evaluation-statistics.md
#: §8.1``: 5000 is claimed by board reflection, 6000 is next free; CASCADE.md §6
#: confirms 6000). Reserved far above every sibling base so the per-unit cache
#: slots can never collide with — or pre-seed — a slot another owner reads: real
#: duel replicates count up from 0, A/A calibration at 1000
#: (:data:`zicato.tournament.calibration.CALIBRATION_REPLICATE_BASE`), contract
#: pre-flight across 2000..2999 (:data:`zicato.epoch.preflight.PREFLIGHT_REPLICATE_BASE`
#: + :data:`~zicato.epoch.preflight.PREFLIGHT_REPLICATE_SPAN`),
#: candidate screen at 3000 (:data:`zicato.epoch.screen.SCREEN_REPLICATE_BASE`),
#: evidence gate at 4000 (:data:`zicato.selection.evidence_gate.EVIDENCE_REPLICATE_BASE`),
#: board reflection at 5000 (:data:`zicato.reflection.corpus.REFLECTION_REPLICATE_BASE`).
#: Draws are STAMPED and KEYED with the same index (the §7.3 same-number rule),
#: so a seeded harness draws fresh per slot and the canonical r0 ``loss.json``
#: is never touched (proven by ``test_admission_probes_never_touch_r0``).
SYNTHESIS_REPLICATE_BASE: int = 6000

#: Default A/A noise draws for the flip-rate measurement. Five draws give a
#: readable flip rate without burning a round's budget (mirrors
#: :data:`zicato.tournament.calibration.DEFAULT_CALIBRATION_RUNS`). Draw 0 is the
#: execution probe, so the noise + execution stages cost K runs between them.
DEFAULT_NOISE_RUNS: int = 5

#: Default number of recent settled candidates the discrimination probe spans.
#: Each contributes a ``(champion, challenger)`` pair, so the probe costs up to
#: ``2 * DEFAULT_DISCRIMINATION_CANDIDATES`` board runs.
DEFAULT_DISCRIMINATION_CANDIDATES: int = 5

_UNMEASURED = "unmeasured"


# ---------------------------------------------------------------------------
# Inputs / outputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AdmissionRequest:
    """A drafted suggestion presented to admission (EVAL-SYNTHESIS.md §3 / §5).

    Synthesis builds this from a synthesised suggestion; admission never authors
    a draft. The pipeline stays decoupled from the ``Suggestion``
    dataclass by binding only to the drafted artifact + the §4 provenance a
    suggestion always carries.

    Fields
    ------
    entry:
        The drafted :class:`~zicato.core.board.BoardEntry` — the executable
        artifact every suggestion type carries (a judge / rubric suggestion
        still attaches its :class:`~zicato.core.board.JudgeSpec` to an entry,
        the ``add_judge(draft, entry_id, judge)`` seam).
    suggestion_type:
        Which §3 suggestion this is (a ``mining.HINT_*`` token). Drives the §4
        rotation rule: regression MAY target train; coverage / judge /
        harder-variant DEFAULT to the incoming rotation slice.
    target_slice:
        The declared target — ``"incoming_rotation"`` or ``"train"`` (§4).
    judge:
        The drafted judge spec, for a judge / rubric suggestion (else ``None``).
    expected_answer_family, judge_family:
        Model-family tokens for the §4 self-preference check. When both are set
        and equal the suggestion is self-graded → flagged (never rejected).
    source_lineage_ids:
        The generations that motivated the suggestion (provenance; rides the
        record for the operator's trace-back).
    """

    entry: BoardEntry
    suggestion_type: str
    target_slice: str = "incoming_rotation"
    judge: JudgeSpec | None = None
    expected_answer_family: str | None = None
    judge_family: str | None = None
    source_lineage_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AdmissionCost:
    """Up-front board-run cost of the admission probes (the ``estimate`` tier).

    ``execution_units`` is the single champion-on-entry run that proves
    executability; it is A/A noise draw 0 (a shared reserved cache slot), so it
    is folded into ``noise_units`` and NOT re-counted in ``total_units``.
    """

    execution_units: int
    noise_units: int
    discrimination_units: int
    total_units: int

    def to_json(self) -> dict[str, int]:
        return {
            "execution_units": self.execution_units,
            "noise_units": self.noise_units,
            "discrimination_units": self.discrimination_units,
            "total_units": self.total_units,
        }


@dataclass(frozen=True, slots=True)
class AdmissionRecord:
    """The measured operating characteristics that ride a suggestion (§5).

    Every stage carries a ``measured`` bit; an unmeasured stage degrades to
    ``unmeasured`` rather than a fabricated number. :meth:`to_json` renders both
    the doc's structured shape (``execution`` / ``noise`` / ``discrimination`` /
    ``leakage``) and the flat conveniences the surface reads (``executed`` /
    ``flip_rate`` / ``leakage_checked``).
    """

    execution: dict[str, Any]
    noise: dict[str, Any]
    discrimination: dict[str, Any]
    leakage: dict[str, Any]
    cost: AdmissionCost
    spent: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "executed": bool(self.execution.get("ran", False)),
            "flip_rate": self.noise.get("flip_rate"),
            "discrimination": {
                "separated": self.discrimination.get("separated", 0),
                "pairs": self.discrimination.get("pairs", 0),
                "measured": bool(self.discrimination.get("measured", False)),
            },
            "leakage_checked": bool(self.leakage.get("checked", False)),
            "execution": dict(self.execution),
            "noise": dict(self.noise),
            "leakage": dict(self.leakage),
            "cost": self.cost.to_json(),
            "spent": self.spent,
        }


# ---------------------------------------------------------------------------
# Cost estimate (the plan tier — reads records, spends NOTHING)
# ---------------------------------------------------------------------------


def estimate_cost(
    *,
    experiments: list[dict[str, Any]],
    noise_runs: int = DEFAULT_NOISE_RUNS,
    discrimination_candidates: int = DEFAULT_DISCRIMINATION_CANDIDATES,
) -> AdmissionCost:
    """Board-run cost of the probes, computed from records — no board runs (§5).

    ``execution`` = 1 (noise draw 0, shared); ``noise`` = ``noise_runs`` (the
    A/A series, which includes the execution draw); ``discrimination`` =
    ``2 * n`` where ``n`` is the settled-matchup count capped at
    ``discrimination_candidates``. ``total`` folds execution into noise, so it
    is ``noise_runs + 2 * n``.
    """
    n = min(len(_settled_matchups(experiments)), max(0, discrimination_candidates))
    runs = max(0, noise_runs)
    return AdmissionCost(
        execution_units=1,
        noise_units=runs,
        discrimination_units=2 * n,
        total_units=runs + 2 * n,
    )


# ---------------------------------------------------------------------------
# The public entry point
# ---------------------------------------------------------------------------


async def admit_suggestion(
    request: AdmissionRequest,
    *,
    champion: Generation,
    board: list[BoardEntry],
    experiments: list[dict[str, Any]],
    weights: ScoringWeights,
    config: RuntimeConfig,
    adapter: Any,
    workspace_root: Path,
    epoch_id: str,
    spend: bool,
    noise_runs: int = DEFAULT_NOISE_RUNS,
    discrimination_candidates: int = DEFAULT_DISCRIMINATION_CANDIDATES,
) -> AdmissionRecord:
    """Measure a drafted suggestion's operating characteristics (EVAL-SYNTHESIS.md §5).

    With ``spend=False`` (plan): compute :func:`estimate_cost`, run the free
    LEAKAGE check (pure — no board runs), and return every live stage as
    ``unmeasured``. NOTHING executes — asserted by
    ``test_admission_plan_mode_runs_nothing`` (zero ``_run_single`` calls).

    With ``spend=True``: run the execution / noise / discrimination probes
    through the real board-unit runner at :data:`SYNTHESIS_REPLICATE_BASE`,
    measure the flip rate + discrimination, and stamp them onto the record. A
    stage that cannot run degrades to ``unmeasured``; the pipeline never raises
    on a probe failure and never auto-rejects.
    """
    cost = estimate_cost(
        experiments=experiments,
        noise_runs=noise_runs,
        discrimination_candidates=discrimination_candidates,
    )
    leakage = _leakage_check(request, board=board, weights=weights, epoch_id=epoch_id)

    if not spend:
        return AdmissionRecord(
            execution=_unmeasured_execution(),
            noise=_unmeasured_noise(),
            discrimination=_unmeasured_discrimination(),
            leakage=leakage,
            cost=cost,
            spent=False,
        )

    execution, noise = await _execution_and_noise(
        request.entry,
        champion=champion,
        weights=weights,
        config=config,
        adapter=adapter,
        workspace_root=workspace_root,
        epoch_id=epoch_id,
        noise_runs=noise_runs,
    )
    # An entry that cannot execute cleanly on the champion is not spent against
    # the discrimination candidates — discrimination of a non-executing draft is
    # meaningless (and the doc rejects a non-executing draft at execution, §5.1).
    if execution.get("ran"):
        discrimination = await _discrimination_probe(
            request.entry,
            experiments=experiments,
            weights=weights,
            config=config,
            adapter=adapter,
            workspace_root=workspace_root,
            epoch_id=epoch_id,
            discrimination_candidates=discrimination_candidates,
        )
    else:
        discrimination = _unmeasured_discrimination()
    return AdmissionRecord(
        execution=execution,
        noise=noise,
        discrimination=discrimination,
        leakage=leakage,
        cost=cost,
        spent=True,
    )


# ---------------------------------------------------------------------------
# The surface bridge — the sync seam the suggestion surface and the CLI call
# into (§5 / §6)
# ---------------------------------------------------------------------------


async def _noop_harness(system: str, user: str, model: str) -> str:  # pragma: no cover
    """Placeholder harness callable for the probe RuntimeConfig.

    The admission probes drive the board-unit runner, which invokes the ADAPTER,
    not this callable; ``make_runtime_config`` merely requires a callable when no
    ``models.*`` role is configured (mirrors ``reflect run``'s adjudication). A
    DISTINCT placeholder per role keeps ``assert_distinct_callables`` honest.
    """
    return ""


async def _noop_auxiliary(system: str, user: str, model: str) -> str:  # pragma: no cover
    """Placeholder auxiliary callable for the probe RuntimeConfig — see :func:`_noop_harness`."""
    return ""


def admit(
    suggestions: list[Any],
    *,
    probe: bool = False,
    workspace_root: Path,
    epoch_id: str,
) -> list[Any]:
    """The sync admission seam: stamp admission records onto surface suggestions (§5).

    The callable :func:`zicato.reflection.suggestions.resolve_admit` late-binds
    and the CLI drives under ``--probe``. It resolves the same corpus context
    ``reflect run`` builds — the epoch board / scoring / experiments, and (only
    when spending) the champion / :class:`RuntimeConfig` / adapter — builds an
    :class:`AdmissionRequest` per suggestion (populating the §4 self-preference
    families from provenance), runs :func:`admit_suggestion` with
    ``spend=probe``, and folds ``AdmissionRecord.to_json()`` into each
    suggestion's ``admission`` field.

    ``probe=False`` runs NOTHING live — :func:`admit_suggestion` computes the
    cost estimate + the pure leakage check and returns every live stage
    ``unmeasured`` (no champion / adapter is even resolved). ``probe=True`` is the
    endpoint-gated spend the CLI gates behind ``--probe``.
    """
    import asyncio  # noqa: PLC0415

    from zicato.reflection import suggestions as surface  # noqa: PLC0415

    sugs = [surface._as_suggestion(s) for s in suggestions]
    board = _load_epoch_board_entries(workspace_root, epoch_id)
    weights = _load_epoch_weights(workspace_root, epoch_id)
    experiments = _load_epoch_experiments(workspace_root, epoch_id)
    aux_family, judge_family = _model_families(workspace_root)

    champion: Generation | None = None
    config: RuntimeConfig | None = None
    adapter: Any = None
    if probe:
        champion, config, adapter = _probe_context(workspace_root, epoch_id)

    out: list[Any] = []
    for s in sugs:
        request = _request_from_suggestion(
            s, board=board, aux_family=aux_family, judge_family=judge_family
        )
        if request is None:
            out.append(s)  # no executable draft (a rubric revision) — leave unmeasured
            continue
        record = _run(
            asyncio,
            admit_suggestion(
                request,
                champion=champion or _placeholder_generation(workspace_root, epoch_id),
                board=board,
                experiments=experiments,
                weights=weights,
                config=config or _placeholder_config(workspace_root),
                adapter=adapter,
                workspace_root=workspace_root,
                epoch_id=epoch_id,
                spend=probe,
            ),
        )
        out.append(_with_admission(s, record.to_json()))
    return out


def _run(asyncio_mod: Any, coro: Any) -> AdmissionRecord:
    return asyncio_mod.run(coro)  # type: ignore[no-any-return]


def _with_admission(suggestion: Any, admission: dict[str, Any]) -> Any:
    """Return ``suggestion`` with its ``admission`` field replaced (surface shape)."""
    import dataclasses  # noqa: PLC0415

    return dataclasses.replace(suggestion, admission=admission)


def _request_from_suggestion(
    suggestion: Any,
    *,
    board: list[BoardEntry],
    aux_family: str | None,
    judge_family: str | None,
) -> AdmissionRequest | None:
    """Build an :class:`AdmissionRequest` from a surface suggestion (§3 / §5).

    * an ENTRY suggestion (regression / coverage / harder variant) carries the
      drafted entry as the executable artifact;
    * a JUDGE suggestion carries the target board entry with the drafted judge
      attached (the entry admission executes; the judge fires in-run);
    * a RUBRIC revision has no executable draft (it edits an existing judge) →
      ``None`` (left unmeasured).

    Populates the §4 self-preference families (SHOULD-FIX-B): ``judge_family``
    from the configured judge model when a judge will grade, ``expected_answer_family``
    from the synthesising aux model when the suggestion is LLM-drafted — the
    provenance knows which tier authored it. ``None`` only when unknowable
    (a mechanical draft pins recorded data, so no model authored it).
    """
    from zicato.core import validate_board_entry  # noqa: PLC0415

    stype = str(getattr(suggestion, "suggestion_type", ""))
    artifact_kind = str(getattr(suggestion, "artifact_kind", ""))
    draft = getattr(suggestion, "draft_artifact", {}) or {}
    provenance = getattr(suggestion, "provenance", {}) or {}
    target_slice = str(getattr(suggestion, "target_slice", "incoming_rotation"))
    llm_drafted = provenance.get("synthesizer") == "llm" or stype in (
        HINT_COVERAGE_ENTRY,
        HINT_JUDGE,
    )
    expected_answer_family = aux_family if llm_drafted else None

    if artifact_kind == "board_entry":
        try:
            entry = validate_board_entry(draft)
        except (KeyError, ValueError, TypeError):
            return None
        return AdmissionRequest(
            entry=entry,
            suggestion_type=stype,
            target_slice=target_slice,
            expected_answer_family=expected_answer_family,
            judge_family=None,
            source_lineage_ids=tuple(provenance.get("source_lineage_ids", []) or ()),
        )

    if artifact_kind == "judge":
        judge = _judge_from_json(draft)
        host = _host_entry_for_judge(suggestion, board)
        if judge is None or host is None:
            return None
        import dataclasses  # noqa: PLC0415

        hosted = dataclasses.replace(host, judges=(*host.judges, judge))
        return AdmissionRequest(
            entry=hosted,
            suggestion_type=stype,
            target_slice=target_slice,
            judge=judge,
            expected_answer_family=expected_answer_family,
            judge_family=judge_family,
            source_lineage_ids=tuple(provenance.get("source_lineage_ids", []) or ()),
        )

    return None  # rubric revision (or an unknown kind) — no executable draft


def _judge_from_json(draft: dict[str, Any]) -> JudgeSpec | None:
    from zicato.core import JudgeMode  # noqa: PLC0415
    from zicato.core.drift_kinds import DriftSeverity  # noqa: PLC0415

    try:
        return JudgeSpec(
            name=str(draft["name"]),
            mode=JudgeMode(str(draft.get("mode", "inline"))),
            body=str(draft["body"]),
            severity=DriftSeverity(str(draft.get("severity", "warning"))),
        )
    except (KeyError, ValueError, TypeError):
        return None


def _host_entry_for_judge(suggestion: Any, board: list[BoardEntry]) -> BoardEntry | None:
    """The board entry a judge suggestion attaches to (from its ``add_judge`` op)."""
    op = getattr(suggestion, "proposed_op", None)
    entry_id = None
    if isinstance(op, dict):
        entry_id = (op.get("args") or {}).get("entry_id")
    if not entry_id:
        return None
    for entry in board:
        if entry.id == entry_id:
            return entry
    return None


def _model_families(workspace_root: Path) -> tuple[str | None, str | None]:
    """``(aux_family, judge_family)`` tokens from the workspace model config (§4).

    A "family" is the model identity the self-preference check compares — the
    model string (or the dotted call_llm path) configured for the role. Absent
    config ⇒ ``(None, None)`` (the check stays honest: an unknowable family never
    flags a false self-preference).
    """
    try:
        from zicato import workspace_loader  # noqa: PLC0415
        from zicato.models_config import load_models_config  # noqa: PLC0415

        cfg = workspace_loader.load_workspace_config(workspace_root)
        models = load_models_config(cfg)
    except Exception:  # noqa: BLE001 — no/!bad config → no families
        return None, None
    return _role_family(models.auxiliary), _role_family(models.judge)


def _role_family(spec: Any) -> str | None:
    """The family token for a role spec — its model string or call_llm path."""
    model = getattr(spec, "model", None)
    if model:
        return str(model)
    call_llm = getattr(spec, "call_llm", None)
    return str(call_llm) if call_llm else None


def _load_epoch_board_entries(workspace_root: Path, epoch_id: str) -> list[BoardEntry]:
    from zicato.board.jsonl import load_board_with_meta  # noqa: PLC0415
    from zicato.core.workspace import board_path  # noqa: PLC0415

    try:
        board, _disable_drift, _judge_only = load_board_with_meta(
            board_path(workspace_root, epoch_id)
        )
    except (OSError, ValueError, KeyError):
        return []
    return list(board)


def _load_epoch_weights(workspace_root: Path, epoch_id: str) -> ScoringWeights:
    from zicato.core.workspace import scoring_path  # noqa: PLC0415

    try:
        import json  # noqa: PLC0415

        from zicato.workspace_loader import scoring_weights_from_dict  # noqa: PLC0415

        raw = json.loads(scoring_path(workspace_root, epoch_id).read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return scoring_weights_from_dict(raw)
    except Exception:  # noqa: BLE001 — a missing / bad scoring.json → defaults
        pass
    return ScoringWeights()


def _load_epoch_experiments(workspace_root: Path, epoch_id: str) -> list[dict[str, Any]]:
    from zicato.query.epoch_view import _read_epoch_experiments  # noqa: PLC0415
    from zicato.query.paths import WorkspacePaths, layout_of  # noqa: PLC0415

    try:
        return _read_epoch_experiments(
            layout_of(WorkspacePaths(workspace_root)).epoch_dir(epoch_id)
        )
    except Exception:  # noqa: BLE001
        return []


def _probe_context(
    workspace_root: Path, epoch_id: str
) -> tuple[Generation | None, RuntimeConfig, Any]:
    """Resolve the champion / config / adapter for the live probes (§5).

    Mirrors ``reflect run`` / the tournament CLI's reconstruction: the champion
    generation off its on-disk snapshot, a :class:`RuntimeConfig` from the
    workspace config (with placeholder callables — the adapter is what the runner
    spends), and the adapter from ``config.json``. A missing champion / adapter
    degrades to a placeholder so the pipeline still runs its honest degrades.
    """
    from zicato import adapter_factory, runtime_factory, workspace_loader  # noqa: PLC0415

    champion = _resolve_champion(workspace_root, epoch_id)
    try:
        cfg = workspace_loader.load_workspace_config(workspace_root)
        config = runtime_factory.make_runtime_config(
            cfg,
            workspace_root=workspace_root,
            harness_call_llm=_noop_harness,
            auxiliary_call_llm=_noop_auxiliary,
        )
    except (ValueError, OSError, FileNotFoundError):
        config = _placeholder_config(workspace_root)
    try:
        adapter = adapter_factory.make_adapter_from_config(
            workspace_loader.load_workspace_config(workspace_root)
        )
    except (ValueError, OSError, FileNotFoundError, ImportError):
        adapter = object()
    return champion, config, adapter


def _resolve_champion(workspace_root: Path, epoch_id: str) -> Generation | None:
    from zicato.evolve.generation_phase import current_generation  # noqa: PLC0415

    try:
        champion_id = current_generation(workspace_root, epoch_id)
    except (FileNotFoundError, ValueError):
        return None
    if not champion_id:
        return None
    return _reconstruct_generation(workspace_root, epoch_id, champion_id)


def _placeholder_generation(workspace_root: Path, epoch_id: str) -> Generation:
    """A stand-in champion for PLAN mode — never executed (spend=False runs nothing)."""
    return Generation(
        id="__admission_plan__",
        epoch_id=epoch_id,
        parent_id=None,
        snapshot_root=workspace_root,
        created_at="",
    )


def _placeholder_config(workspace_root: Path) -> RuntimeConfig:
    """A stand-in config for PLAN mode — never invoked (spend=False runs nothing)."""
    return RuntimeConfig(
        instance_id="default",
        workspace_root=workspace_root,
        harness_call_llm=_noop_harness,
        auxiliary_call_llm=_noop_auxiliary,
    )


# ---------------------------------------------------------------------------
# (a) + (b) execution + A/A noise at the reserved base 6000
# ---------------------------------------------------------------------------


async def _execution_and_noise(
    entry: BoardEntry,
    *,
    champion: Generation,
    weights: ScoringWeights,
    config: RuntimeConfig,
    adapter: Any,
    workspace_root: Path,
    epoch_id: str,
    noise_runs: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run the champion against itself on JUST ``entry``, K draws at 6000 + j.

    Draw 0 IS the execution probe. Each draw's pass/fail bit feeds the pure
    :func:`flip_rate`; fewer than two usable draws leave the flip rate
    ``unmeasured`` (an honest ``None``, never a ``0.0`` lie). An aborted /
    unexecutable draw 0 marks ``execution.ran = false`` loudly, but the pipeline
    keeps its footing.
    """
    try:
        entry.validate()
    except Exception as exc:  # noqa: BLE001 — an invalid draft is a loud non-execution
        return (
            {"ran": False, "aborted": False, "measured": True, "reason": f"invalid draft: {exc}"},
            _unmeasured_noise(),
        )

    bits: list[bool | None] = []
    execution: dict[str, Any] | None = None
    for draw in range(max(2, noise_runs)):
        replicate_index = SYNTHESIS_REPLICATE_BASE + draw
        try:
            loss = await _run_entry(
                entry,
                generation=champion,
                weights=weights,
                config=config,
                adapter=adapter,
                workspace_root=workspace_root,
                epoch_id=epoch_id,
                replicate_index=replicate_index,
                match_id=f"admission-noise:{draw}",
            )
        except Exception as exc:  # noqa: BLE001 — a runner failure degrades, never crashes
            if draw == 0:
                execution = {
                    "ran": False,
                    "aborted": False,
                    "measured": True,
                    "reason": f"runner error: {exc}",
                }
            break
        if draw == 0:
            execution = _execution_from_loss(loss)
            if loss is None or _is_aborted(loss):
                # A draw-0 abort means the entry did not execute cleanly; stop
                # the A/A series (a floor built on an abort is not a floor).
                break
        if loss is not None and not _is_aborted(loss):
            bits.append(loss.pass_fail)

    if execution is None:  # noise_runs < 1 guard — nothing ran
        execution = {"ran": False, "aborted": False, "measured": True, "reason": "no draws"}

    rate = flip_rate(bits)
    measured = rate is not None
    noise = {
        "flip_rate": rate,
        "runs": len(bits),
        "measured": measured,
        "base": SYNTHESIS_REPLICATE_BASE,
        "note": None if measured else _UNMEASURED,
    }
    return execution, noise


def _execution_from_loss(loss: Any) -> dict[str, Any]:
    if loss is None:
        return {"ran": False, "aborted": False, "measured": True, "reason": "no loss profile"}
    aborted = _is_aborted(loss)
    return {"ran": not aborted, "aborted": aborted, "measured": True, "reason": None}


# ---------------------------------------------------------------------------
# (c) discrimination probe — the matchup-record method (EVAL-VIEW.md §2.3)
# ---------------------------------------------------------------------------


async def _discrimination_probe(
    entry: BoardEntry,
    *,
    experiments: list[dict[str, Any]],
    weights: ScoringWeights,
    config: RuntimeConfig,
    adapter: Any,
    workspace_root: Path,
    epoch_id: str,
    discrimination_candidates: int,
) -> dict[str, Any]:
    """Run ``entry`` against recent settled ``(champion, challenger)`` pairs.

    For each of the most recent ``discrimination_candidates`` settled matchups,
    reconstruct BOTH generations' snapshot trees (the recombination builder's
    reconstruction precedent) and run the drafted entry on each side; a pair is
    *compared* when both sides produce a usable verdict and *separated* when the
    two verdicts differ. No settled candidate / no reconstructable tree degrades
    to ``unmeasured`` (``separated = 0, pairs = 0``) — an honest zero, never a
    fabricated separation.
    """
    matchups = _settled_matchups(experiments)
    if not matchups:
        return _unmeasured_discrimination()
    recent = matchups[-max(0, discrimination_candidates) :] if discrimination_candidates else []
    if not recent:
        return _unmeasured_discrimination()

    pairs = 0
    separated = 0
    for champ_id, child_id in recent:
        champ_pf = await _run_side(
            entry, champ_id, weights, config, adapter, workspace_root, epoch_id, "champ"
        )
        child_pf = await _run_side(
            entry, child_id, weights, config, adapter, workspace_root, epoch_id, "child"
        )
        if champ_pf is None or child_pf is None:
            continue  # a side we could not measure is not a comparison
        pairs += 1
        if champ_pf != child_pf:
            separated += 1

    if pairs == 0:
        return _unmeasured_discrimination()
    return {"separated": separated, "pairs": pairs, "measured": True, "note": None}


async def _run_side(
    entry: BoardEntry,
    generation_id: str,
    weights: ScoringWeights,
    config: RuntimeConfig,
    adapter: Any,
    workspace_root: Path,
    epoch_id: str,
    side: str,
) -> bool | None:
    """Run ``entry`` on one reconstructed generation; return its pass/fail bit.

    Degrades to ``None`` when the tree cannot be reconstructed, the runner
    fails, or the draw aborts — the side is then not counted as a comparison.
    """
    generation = _reconstruct_generation(workspace_root, epoch_id, generation_id)
    if generation is None:
        return None
    try:
        loss = await _run_entry(
            entry,
            generation=generation,
            weights=weights,
            config=config,
            adapter=adapter,
            workspace_root=workspace_root,
            epoch_id=epoch_id,
            replicate_index=SYNTHESIS_REPLICATE_BASE,
            match_id=f"admission-discrimination:{side}:{generation_id}",
        )
    except Exception:  # noqa: BLE001 — a runner failure is a no-signal side, never a crash
        return None
    if loss is None or _is_aborted(loss):
        return None
    pf = loss.pass_fail
    return None if pf is None else bool(pf)


def _reconstruct_generation(
    workspace_root: Path, epoch_id: str, generation_id: str
) -> Generation | None:
    """A :class:`Generation` over an on-disk snapshot, or ``None`` when absent.

    Mirrors the tournament CLI's ``_build_generation`` reconstruction: the
    snapshot lives under ``generations/{id}/snapshot``. A missing snapshot
    (a pruned / never-materialised tree) degrades to ``None``.
    """
    from zicato.core.workspace import generation_dir  # noqa: PLC0415

    try:
        snapshot_root = generation_dir(workspace_root, epoch_id, generation_id) / "snapshot"
        if not snapshot_root.exists():
            return None
        return Generation(
            id=generation_id,
            epoch_id=epoch_id,
            parent_id=None,
            snapshot_root=snapshot_root.resolve(),
            created_at="",
        )
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# (d) leakage / collusion checks (pure — no board runs)
# ---------------------------------------------------------------------------


def _leakage_check(
    request: AdmissionRequest,
    *,
    board: list[BoardEntry],
    weights: ScoringWeights,
    epoch_id: str,
) -> dict[str, Any]:
    """The §4 rotation rule + emulator collusion guard + self-preference flag.

    * **Rotation** — a coverage / judge / harder-variant suggestion must land in
      the incoming-rotation (holdout) slice the motivating proposer has not
      seen; a regression entry MAY target train; a rubric revision edits an
      existing judge (no new slice). Bound to :func:`split_board` membership,
      the canonical holdout binding.
    * **Emulator guard** — a ``multi_turn_emulated`` draft opts into the
      collusion guard through its :class:`UserPersona` (the only runtime input
      the emulator sees), and the harness / auxiliary callables must be distinct
      (:func:`assert_distinct_callables`).
    * **Self-preference** — the expected answer and its judge must not share a
      model family; a match is FLAGGED, never rejected.
    """
    from zicato.board.split import rotation_seed, split_board  # noqa: PLC0415

    entry = request.entry
    cfg = weights.overfitting
    seed = rotation_seed(cfg, epoch_id)
    train_ids, holdout_ids = split_board([*board, entry], cfg, seed=seed)
    in_holdout = entry.id in holdout_ids
    actual_slice = "incoming_rotation" if in_holdout else "train" if entry.id in train_ids else None

    target_slice_ok = _rotation_ok(request.suggestion_type, in_holdout)
    emulator_guard_ok = _emulator_guard_ok(entry)
    self_preference_flag = bool(
        request.expected_answer_family
        and request.judge_family
        and request.expected_answer_family == request.judge_family
    )
    return {
        "checked": True,
        "target_slice": request.target_slice,
        "actual_slice": actual_slice,
        "target_slice_ok": target_slice_ok,
        "emulator_guard_ok": emulator_guard_ok,
        "self_preference_flag": self_preference_flag,
    }


def _rotation_ok(suggestion_type: str, in_holdout: bool) -> bool:
    """Whether the drafted entry lands where §4 requires for its suggestion type."""
    if suggestion_type == HINT_REGRESSION_ENTRY:
        return True  # a regression test pinning a past failure MAY target train
    if suggestion_type == HINT_RUBRIC_REVISION:
        return True  # edits an existing judge — no new slice
    if suggestion_type in (HINT_COVERAGE_ENTRY, HINT_JUDGE, HINT_HARDER_VARIANT):
        return in_holdout  # must be blind to the motivating proposer
    return in_holdout


def _emulator_guard_ok(entry: BoardEntry) -> bool:
    """The emulator collusion guard for a ``multi_turn_emulated`` draft.

    A non-emulated entry has no emulator, so the guard is vacuously satisfied. An
    emulated draft opts in structurally through its :class:`UserPersona` (the
    collusion guard: the persona is the ONLY runtime input the emulator sees).
    An emulated draft missing that persona would leak internal state to the
    emulator — the guard fails.
    """
    if entry.kind != "multi_turn_emulated":
        return True
    return entry.user_persona is not None


# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------


async def _run_entry(
    entry: BoardEntry,
    *,
    generation: Generation,
    weights: ScoringWeights,
    config: RuntimeConfig,
    adapter: Any,
    workspace_root: Path,
    epoch_id: str,
    replicate_index: int,
    match_id: str,
) -> Any:
    """Run one drafted entry on one generation through the real board-unit runner.

    Stamps the reserved replicate index onto the entry (the §7.3 same-number
    rule: a seeded harness draws fresh per slot) and keys the per-unit cache with
    it, as the calibration / screen / corpus draws do. Returns the
    entry's :class:`LossProfile`, or ``None`` when the runner produced none.
    """
    from zicato.tournament.scheduling import _run_board_units_fast  # noqa: PLC0415
    from zicato.tournament.worker_transport import _stamp_replicate_index  # noqa: PLC0415

    losses = await _run_board_units_fast(
        adapter=adapter,
        child_gen=generation,
        board=_stamp_replicate_index([entry], replicate_index),
        weights=weights,
        config=config,
        workspace_root=workspace_root,
        epoch_id=epoch_id,
        match_id=match_id,
        replicate_index=replicate_index,
    )
    return losses.get(entry.id)


def _is_aborted(loss: Any) -> bool:
    """Whether a draw aborted (any cause) — an aborted draw is no clean signal."""
    return bool(getattr(loss, "aborted", False) or getattr(loss, "abort_cause", None))


def _settled_matchups(experiments: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """The reign's settled ``(champion, challenger)`` matchups, in record order.

    Reuses :func:`zicato.query.eval_view._reign_matchups` so the discrimination
    probe spans the same pairs the instrument panel measures discrimination
    over (EVAL-VIEW.md §2.3, the MATCHUP-RECORD binding). Tolerant: a malformed
    experiments list degrades to no matchups.
    """
    from zicato.query.eval_view import _reign_matchups  # noqa: PLC0415

    try:
        return list(_reign_matchups(experiments))
    except Exception:  # noqa: BLE001
        return []


def _unmeasured_execution() -> dict[str, Any]:
    return {"ran": False, "aborted": False, "measured": False, "reason": _UNMEASURED}


def _unmeasured_noise() -> dict[str, Any]:
    return {
        "flip_rate": None,
        "runs": 0,
        "measured": False,
        "base": SYNTHESIS_REPLICATE_BASE,
        "note": _UNMEASURED,
    }


def _unmeasured_discrimination() -> dict[str, Any]:
    return {"separated": 0, "pairs": 0, "measured": False, "note": _UNMEASURED}


__all__ = [
    "DEFAULT_DISCRIMINATION_CANDIDATES",
    "DEFAULT_NOISE_RUNS",
    "SYNTHESIS_REPLICATE_BASE",
    "AdmissionCost",
    "AdmissionRecord",
    "AdmissionRequest",
    "admit",
    "admit_suggestion",
    "estimate_cost",
]
