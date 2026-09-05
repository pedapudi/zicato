"""A round whose proposals all died on the transport measured nothing.

Recording that as a rejection does not merely file a wrong decision: it
burns a generation id and journals an experiment whose stated cause --
"failed parsing or post-apply validation" -- never happened. On a campaign
that fabricates "the proposer could not produce a valid patch set" records
in the ledger an operator reads to judge the proposer, and the round counts
toward the consecutive-rejection breaker that stops the loop.

These pin the classification in both directions, because a classifier that
swallows real rejections is worse than the bug it fixes, and they pin the
settlement the classification routes to.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from zicato.evolve import field_candidates
from zicato.evolve.candidate_batch import CandidateRejection
from zicato.evolve.generation_phase import FieldRound
from zicato.evolve.persist import deferred_infra_proposer_outage, infrastructure_outage
from zicato.evolve.round_api import DEFERRED_INFRA_DECISION
from zicato.proposer.proposer import ProposerError

# Every string below is a REAL emitter template, because the classifier is
# anchored to those templates and a hand-written approximation of one would
# pass a test the production prose fails. Sources:
# ``proposer/foe_agent.py`` (the episode boundary, the sole runtime's),
# ``proposer/best_of_n.py`` (the auxiliary-call boundary), and
# ``proposer/best_of_n.py``'s slate-slot tag in front of either.
_EXPIRED_CREDENTIAL = (
    "the proposal episode failed: model request failed: RefreshError: "
    "Reauthentication is needed. Please run `gcloud auth application-default "
    "login` to reauthenticate."
)
_FORBIDDEN_PROJECT = (
    "slot 0: the proposal episode failed: PermissionDenied: 403 Forbidden: "
    "Permission denied on resource project some-project."
)
_ENDPOINT_UNAVAILABLE = "the proposal episode failed: 503 Service Unavailable"
_SEVERED_CONNECTION = (
    "the proposal episode could not start: ConnectionResetError: connection reset by peer"
)
_AUXILIARY_CALL = "evaluation LLM call raised AuthenticationError: invalid api key"

# Post-response content rejections: the endpoint answered, and what came
# back was judged. These quote model output and child-snapshot validator
# findings, so they are the text the classifier must never scan.
_A_BAD_PATCH_SET = "patches failed post-apply validation: edit outside mutation point p1"
_A_BAD_HYPOTHESIS = "ExperimentParseError: no parseable hypothesis in the response"


@pytest.mark.parametrize(
    "attempts",
    [
        pytest.param([_EXPIRED_CREDENTIAL], id="expired-credential"),
        pytest.param([_FORBIDDEN_PROJECT], id="forbidden-project"),
        pytest.param([_ENDPOINT_UNAVAILABLE], id="endpoint-refused-the-request"),
        pytest.param([_SEVERED_CONNECTION], id="episode-never-started"),
        pytest.param([_AUXILIARY_CALL], id="auxiliary-call-boundary"),
        pytest.param([_EXPIRED_CREDENTIAL] * 3, id="every-slot-same-outage"),
        pytest.param([_EXPIRED_CREDENTIAL, _FORBIDDEN_PROJECT], id="mixed-outages"),
    ],
)
def test_an_all_transport_trail_is_an_outage(attempts: list[str]) -> None:
    assert infrastructure_outage(attempts) is True


@pytest.mark.parametrize(
    "attempts",
    [
        pytest.param([_A_BAD_PATCH_SET], id="a-bad-patch-set"),
        pytest.param([_A_BAD_HYPOTHESIS], id="an-unparseable-experiment"),
        pytest.param([_A_BAD_PATCH_SET, _A_BAD_HYPOTHESIS], id="two-bad-proposals"),
    ],
)
def test_a_proposal_failure_still_rejects(attempts: list[str]) -> None:
    """The round is about the proposals, so it must keep rejecting."""
    assert infrastructure_outage(attempts) is False


@pytest.mark.parametrize(
    "finding",
    [
        pytest.param(
            "patches failed post-apply validation: connection refused "
            "while importing target/net.py",
            id="the-child-snapshot-refused-a-connection",
        ),
        pytest.param(
            "patches violate proposer-brief forbidden-edits list: api_key_header",
            id="a-mutation-point-named-for-a-credential",
        ),
        pytest.param(
            'ExperimentParseError: "service unavailable" is not in the declared ' "enum domain",
            id="an-enum-domain-quoting-an-outage-phrase",
        ),
    ],
)
def test_a_challengers_own_words_are_never_an_outage(finding: str) -> None:
    """The false positive the call-boundary anchor exists to make impossible.

    A challenger that breaks networking code, edits a mutation point named
    for a credential, or declares an enum listing an outage phrase puts a
    marker substring into a proposal error. Deferring on that would discard a
    real measurement of a degraded arm, and it would do so more often for the
    arms that emit more invalid patches.
    """
    assert infrastructure_outage([finding]) is False


def test_a_timed_out_attempt_is_not_an_outage() -> None:
    """One attempt timing out is not proof the endpoint was never reached.

    ``zicato.epoch.round_integrity`` excludes bare timeout prose from its
    vocabulary for that reason, and this path reads the same vocabulary, so
    the two cannot disagree about the same trail.
    """
    assert infrastructure_outage(["evaluation LLM call timed out after 120.0s"]) is False


def test_one_healthy_slot_among_outages_is_not_an_outage() -> None:
    """The rule is over EVERY attempt rather than any one of them.

    A slot that lost its connection while the others proposed badly leaves a
    round that DID measure something; deferring it would discard a real
    rejection and let the same bad proposal come back unchanged.
    """
    assert infrastructure_outage([_EXPIRED_CREDENTIAL, _A_BAD_PATCH_SET]) is False


def test_an_empty_trail_is_not_an_outage() -> None:
    """Nothing says it was one, so the existing rejection path is honest."""
    assert infrastructure_outage([]) is False


# ---------------------------------------------------------------------------
# What the classification routes to.
# ---------------------------------------------------------------------------


class _RecordingRoundLog:
    def __init__(self) -> None:
        self.emitted: list[tuple[str, dict[str, Any]]] = []

    def emit(self, token: str, fields: dict[str, Any] | None = None, *_: Any) -> None:
        self.emitted.append((token, dict(fields or {})))


def test_the_deferral_leaves_nothing_behind(tmp_path: Path) -> None:
    """No experiment, no generation, no journal entry -- the id stays free.

    The whole point of deferring rather than rejecting: the next attempt
    reuses the generation id, and the ledger an operator reads to judge the
    proposer carries no record of a proposal that was never made.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    round_log = _RecordingRoundLog()

    outcome = deferred_infra_proposer_outage(
        workspace_root=workspace,
        epoch_id="epoch_1",
        parent_id="v0",
        next_id="v1",
        attempts=[_EXPIRED_CREDENTIAL],
        round_index=1,
        beater=None,
        round_log=round_log,
    )

    assert outcome.tournament_decision == DEFERRED_INFRA_DECISION
    # Empty rather than ``v1``: nothing on disk carries that id, so naming
    # it would point the loop's heartbeat and the resume reconciliation at a
    # generation that does not exist.
    assert outcome.proposed_generation_id == ""
    assert "Reauthentication is needed" in outcome.rejection_reason
    assert list(workspace.iterdir()) == []
    assert [token for token, _ in round_log.emitted] == ["decision_recorded", "round_closed"]
    decision = round_log.emitted[0][1]
    assert decision["decision"] == DEFERRED_INFRA_DECISION
    assert decision["provenance"]["promoted_generation_id"] is None


def _field_round(**overrides: Any) -> Any:
    """A stand-in for :class:`FieldRound` carrying only the read attributes.

    The names are checked against the real dataclass, so a rename that would
    break the settlement fails here rather than passing against a stub that
    kept the old name.
    """
    declared = {field.name for field in dataclasses.fields(FieldRound)}
    unknown = set(overrides) - declared
    assert not unknown, f"not FieldRound attributes: {sorted(unknown)}"
    attributes: dict[str, Any] = {
        "epoch_id": "epoch_1",
        "parent_id": "v0",
        "round_index": 1,
        "total_rounds": 1,
        "beater": None,
        "round_log": _RecordingRoundLog(),
        "tournament_spec": SimpleNamespace(structure="gauntlet", params={}),
    }
    attributes.update(overrides)
    return SimpleNamespace(**attributes)


def _rejection(generation_id: str, attempts: list[str] | None) -> CandidateRejection:
    return CandidateRejection(
        generation_id=generation_id,
        reason="",
        status={"generation_id": generation_id, "status": "rejected"},
        proposer_error=ProposerError(attempts) if attempts is not None else None,
    )


@pytest.mark.parametrize("field_size", [1, 3], ids=["gauntlet", "multi-challenger"])
@pytest.mark.asyncio
async def test_a_field_that_only_hit_the_transport_defers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field_size: int,
) -> None:
    """Every field shape, because the harm does not depend on the size.

    A wider field settles without writing an experiment either way, but its
    rejection still counts toward the consecutive-rejection breaker and still
    reads back as evidence about the proposer.
    """
    published: list[list[dict[str, Any]]] = []
    monkeypatch.setattr(
        field_candidates,
        "_publish_proposing_field",
        lambda _round, *, tournament_id, field_status: published.append(field_status),
    )
    monkeypatch.setattr(
        field_candidates,
        "_persist_rejected_round",
        _fail_if_called("the rejection tail must not run for an outage"),
    )

    rejections = tuple(_rejection(f"v{i + 1}", [_EXPIRED_CREDENTIAL]) for i in range(field_size))
    outcome = await field_candidates._settle_field_that_produced_nothing(
        _field_round(
            workspace_root=tmp_path,
            field_size=field_size,
        ),
        base_id="v1",
        rejections=rejections,
        field_status=[dict(rejection.status) for rejection in rejections],
    )

    assert outcome.tournament_decision == DEFERRED_INFRA_DECISION
    # The slots are still published: the deferral writes no experiment, so
    # the forming field is the only surface that shows what each slot hit.
    assert len(published) == 1


@pytest.mark.asyncio
async def test_a_field_whose_slot_proposed_badly_still_rejects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One judged proposal is a measurement, whatever else the field hit."""
    monkeypatch.setattr(
        field_candidates,
        "_publish_proposing_field",
        lambda *_args, **_kwargs: None,
    )
    outcome = await field_candidates._settle_field_that_produced_nothing(
        _field_round(
            workspace_root=tmp_path,
            field_size=2,
        ),
        base_id="v1",
        rejections=(
            _rejection("v1", [_EXPIRED_CREDENTIAL]),
            _rejection("v2", [_A_BAD_PATCH_SET]),
        ),
        field_status=[],
    )

    assert outcome.tournament_decision == "rejected"


@pytest.mark.asyncio
async def test_a_slot_with_no_attempt_trail_still_rejects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slot that failed before it reached a proposer says nothing about the endpoint.

    It carries no :class:`~zicato.proposer.proposer.ProposerError`, so there
    is no trail to read, and a round cannot be shown to have measured nothing
    on the strength of its siblings alone.
    """
    monkeypatch.setattr(
        field_candidates,
        "_publish_proposing_field",
        lambda *_args, **_kwargs: None,
    )
    outcome = await field_candidates._settle_field_that_produced_nothing(
        _field_round(
            workspace_root=tmp_path,
            field_size=2,
        ),
        base_id="v1",
        rejections=(
            _rejection("v1", [_EXPIRED_CREDENTIAL]),
            _rejection("v2", None),
        ),
        field_status=[],
    )

    assert outcome.tournament_decision == "rejected"


def _fail_if_called(message: str) -> Any:
    def _raise(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError(message)

    return _raise
