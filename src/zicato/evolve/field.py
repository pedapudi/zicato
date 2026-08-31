"""Evaluate and durably settle one strategy-driven evolve round.

The round runs as a fixed sequence of named phases, one per lifecycle step
the execution plan serves (``zicato.query.execution_plan.ROUND_STEPS``):

* propose and apply — :mod:`zicato.evolve.field_candidates`;
* run — :mod:`zicato.evolve.field_execution`;
* gate — :func:`zicato.evolve.gate.resolve_field_verdict`;
* decide — :func:`zicato.evolve.settlement.settle_field_round`.

Two of them can end the round early, and both return an outcome that is
already persisted: a field where no candidate applied, and a round the
endpoint-outage circuit deferred.
"""

from __future__ import annotations

import logging
from typing import Any

from zicato.evolve import generation_phase
from zicato.evolve.field_candidates import assemble_candidate_field
from zicato.evolve.field_execution import execute_field_tournament
from zicato.evolve.gate import resolve_field_verdict
from zicato.evolve.generation_phase import FieldRound
from zicato.evolve.propose_apply import _propose_and_apply_challenger
from zicato.evolve.round_api import EvolveRoundOutcome
from zicato.evolve.round_reporting import _RoundLogEmitter
from zicato.evolve.settlement import settle_field_round

log = logging.getLogger("zicato.orchestrator")


def _open_field_round(prepared: generation_phase.PreparedRound) -> FieldRound:
    """Expand one round's prepared state into the names its phases read.

    Narration — a rejected round's summary sentence, the round epilogue —
    describes what the round did rather than generating a candidate, so it is
    auxiliary work.  It therefore runs on the auxiliary callable and the
    auxiliary model id; the two must name the same endpoint, or a workspace
    with a dedicated proposer engine would send the auxiliary callable a
    model id it does not serve.  The proposer callable is picked separately,
    where a candidate is actually proposed
    (:mod:`zicato.evolve.candidate_batch`).

    A direct caller (tests) may not thread the opened round-log emitter, so
    one is bound here when it is absent and every emission stays uniformly
    best-effort.  ``evolve_once``, the production caller, passes the emitter
    it already opened the round on.
    """

    return FieldRound(
        prepared=prepared,
        round_log=prepared.round_log
        or _RoundLogEmitter(prepared.workspace_root, prepared.epoch_id, prepared.round_index),
        workspace_root=prepared.workspace_root,
        workspace_config=prepared.workspace_config,
        epoch_id=prepared.epoch_id,
        round_index=prepared.round_index,
        total_rounds=prepared.total_rounds,
        parent_id=prepared.parent_generation.id,
        adapter=prepared.adapter,
        config=prepared.config,
        weights=prepared.weights,
        board=list(prepared.board),
        train_board=list(prepared.train_board),
        tournament_spec=prepared.tournament_spec,
        strategy=prepared.strategy,
        mutations=list(prepared.mutations),
        disable_drift=prepared.disable_drift,
        judge_only=prepared.judge_only,
        fast_mode=prepared.fast_mode,
        beater=prepared.beater,
        meta_loop_emitter=prepared.meta_loop_emitter,
        auxiliary_call_llm=prepared.config.auxiliary_call_llm,
        auxiliary_model=str(prepared.workspace_config.get("auxiliary_model", "")),
        field_size=prepared.strategy.field_size(),
    )


async def evolve_field_round(
    prepared: generation_phase.PreparedRound,
    *,
    resume_plan: Any = None,
) -> EvolveRoundOutcome:
    """Run one evolve round under the configured selection strategy.

    Candidate production uses :meth:`SelectionStrategy.field_size`; the
    one-challenger gauntlet requests one candidate and wider structures
    request their declared field. Every scheduled matchup runs through
    :func:`zicato.tournament.runner.run_matchup` and the promotion gate. The
    strategy consumes each gate verdict without re-deciding it. Optional
    Bradley--Terry evidence and holdout confirmation may withhold a crown.
    Settlement then records every applied challenger, advances the primary
    champion when one was promoted, and persists the tournament audit.

    ``fast_mode`` is the runtime cache-first evaluation knob (the ``--mode
    fast`` setting), threaded identically to ``disable_drift`` and
    ``judge_only``. When set, every matchup resolves both competitors through
    the replicate-keyed unit cache and runs only missing slots (see
    :func:`zicato.tournament.runner.run_matchup`). Fast mode is therefore
    structure-independent: it composes with racing, Swiss, elimination, and
    gauntlet matchups without replaying one draw as another replicate. The
    resolved champion-eval mode is recorded in the journal for provenance; it
    is not a contract input, so flipping fast↔full does not roll the epoch.
    """

    field_round = _open_field_round(prepared)
    candidates = await assemble_candidate_field(
        field_round,
        resume_plan=resume_plan,
        # Preserve the public monkeypatch anchor used by integration tests.
        produce_one=_propose_and_apply_challenger,
    )
    if isinstance(candidates, EvolveRoundOutcome):
        return candidates

    execution = await execute_field_tournament(field_round, candidates)
    if isinstance(execution, EvolveRoundOutcome):
        return execution

    verdict = await resolve_field_verdict(field_round, candidates, execution.decision)
    return await settle_field_round(field_round, candidates, execution, verdict)
