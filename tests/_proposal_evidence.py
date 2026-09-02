"""Render the evidence a proposal episode is given, by channel name.

Each optional evidence channel — the failure-mode profile, the process
exemplars, the genealogy sample, the calibration record, the track-record
annotations — has one property its tests exist to pin: it is omitted at
its default, and when it is present it appears banded and in its place.
Those tests are about the channel, not about how a request is assembled,
so they name the channel and let this helper build the
:class:`~zicato.proposer.foe_request.ProposalEvidence` around it.

The result is the evidence half of the episode's task, which is also
exactly what the best-of-N critic is shown: one renderer, so a channel
that is banded for the proposer cannot arrive unbanded at the critic.
"""

from __future__ import annotations

from typing import Any

from zicato.proposer.foe_request import ProposalEvidence, render_evidence


def render_proposal_evidence(
    *,
    current_loss_summary: str = "",
    patterns: Any = (),
    mutations: Any = (),
    prior_experiments: Any = (),
    custom_judge_names: Any = (),
    genealogy: Any = (),
    **channels: Any,
) -> str:
    """The rendered evidence for one round, from its channels.

    The iterable arguments are accepted in whatever form a test finds
    convenient and normalized here, so a test can pass a list where the
    dataclass wants a tuple.
    """
    return render_evidence(
        ProposalEvidence(
            loss_summary=current_loss_summary,
            patterns=tuple(patterns),
            mutations=tuple(mutations),
            prior_experiments=tuple(prior_experiments),
            custom_judge_names=tuple(sorted(custom_judge_names)),
            genealogy=tuple(genealogy),
            **channels,
        )
    )


__all__ = ["render_proposal_evidence"]
