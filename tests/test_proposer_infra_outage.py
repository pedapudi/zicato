"""A round whose proposals all died on the transport measured nothing.

Recording that as a rejection does not merely file a wrong decision: it
burns a generation id and journals an experiment whose stated cause --
"failed parsing or post-apply validation" -- never happened. On a campaign
that fabricates "the proposer could not produce a valid patch set" records
in the ledger an operator reads to judge the proposer.

These pin the classification in both directions, because a classifier that
swallows real rejections is worse than the bug it fixes.
"""

from __future__ import annotations

import pytest

from zicato.evolve.persist import infrastructure_outage

_EXPIRED_CREDENTIAL = (
    "slot 0: the proposal episode failed: model request failed: RefreshError: "
    "Reauthentication is needed. Please run `gcloud auth application-default "
    "login` to reauthenticate."
)
_FORBIDDEN_PROJECT = (
    "model-x: HTTP 403: Permission denied on resource project some-project."
)
_A_BAD_PATCH_SET = (
    "attempt 1: patches failed post-apply validation: edit outside mutation point p1"
)
_A_BAD_HYPOTHESIS = "attempt 2: the episode returned no parseable experiment"


@pytest.mark.parametrize(
    "attempts",
    [
        pytest.param([_EXPIRED_CREDENTIAL], id="expired-credential"),
        pytest.param([_FORBIDDEN_PROJECT], id="forbidden-project"),
        pytest.param([_EXPIRED_CREDENTIAL] * 3, id="every-slot-same-outage"),
        pytest.param([_EXPIRED_CREDENTIAL, _FORBIDDEN_PROJECT], id="mixed-outages"),
        pytest.param(["model-x: HTTP 503: backend unavailable"], id="endpoint-5xx"),
        pytest.param(["connection reset by peer"], id="severed-connection"),
        pytest.param(["read timed out"], id="stalled-endpoint"),
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


def test_one_healthy_slot_among_outages_is_not_an_outage() -> None:
    """ALL of them, not any.

    A slot that lost its connection while the others proposed badly leaves a
    round that DID measure something; deferring it would discard a real
    rejection and let the same bad proposal come back unchanged.
    """
    assert infrastructure_outage([_EXPIRED_CREDENTIAL, _A_BAD_PATCH_SET]) is False


def test_an_empty_trail_is_not_an_outage() -> None:
    """Nothing says it was one, so the existing rejection path is honest."""
    assert infrastructure_outage([]) is False
