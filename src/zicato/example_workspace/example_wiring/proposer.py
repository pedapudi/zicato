"""The proposer this example runs: one defect removed per round.

A proposer is asked, once per candidate, to turn what is known about the
current champion into a concrete edit plus the prediction that edit is
betting on. zicato's supported proposer is a Foe episode, configured in
the workspace's ``proposer`` block; ``runtime.proposer_agent`` is the
other door, naming a class of the operator's own. This example takes that
door, because a class needs no binary, no credential, and no network, so
a first round runs on a machine that has none of them.

What it does is mechanical: read the style policy out of the mutation
point it is handed, drop the first seeded defect token still there, and
predict the board entry that token was suppressing will start passing.
Once every seeded token is gone it proposes an inert token instead, which
scores identically to the champion and is correctly rejected — the loop
then stops on consecutive rejections rather than editing forever.

A real proposer reads far more of the context it is given: the failure
patterns, the loss summary, the prior experiments and what each of them
scored. Those are the fields of
:class:`~zicato.proposer.agent.ProposerContext`, and reading them is the
difference between a script and a proposer.

The trust boundary is stated in ``docs/design/PROPOSER.md``: a class
named here runs inside the loop's own process with its permissions.
"""

from __future__ import annotations

import ast
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from zicato.core.experiment import Experiment, HypothesisSpec
from zicato.core.mutation import Patch
from zicato.mutation import replacement_source
from zicato.proposer.proposer import ProposerError

#: The mutation point this proposer edits — the id on the
#: ``# zicato:mutable`` marker in the system under test.
POLICY_MUTATION_ID = "style_rules"

#: The seeded defects, in the order they are removed, each paired with
#: the board entry it suppresses. Removing one is a prediction that that
#: entry starts passing, which is what the hypothesis below claims and
#: the tournament then confirms or refutes.
DEFECTS: tuple[tuple[str, str], ...] = (
    ("omit-summary", "ends_with_a_summary"),
    ("skip-citations", "attributes_its_claim"),
    ("verbose-prose", "stays_concise"),
)

#: Proposed once the seeded defects are gone. It suppresses nothing, so
#: it ties the champion's score and the gate rejects it.
INERT_TOKEN = "active-voice"


def read_policy(point: Any) -> list[str]:
    """Return the style tokens a mutation point currently holds.

    A span point is reported by whole lines, so its ``content`` is the
    statement around the string — ``STYLE_RULES = "..."`` — while a patch
    replaces the string itself. :func:`replacement_source` is the
    conversion between the two units: it returns the literal as source,
    which :func:`ast.literal_eval` turns into the value. A proposer that
    edited ``point.content`` directly would nest the statement inside its
    own string, and the applier would write that back to the tree.
    """
    policy = ast.literal_eval(replacement_source(point))
    return [token.strip() for token in str(policy).split(";") if token.strip()]


class OneDefectPerRound:
    """Removes one seeded defect token per round, then stops improving."""

    #: Names this proposer in lineage records and in the contract hash.
    external_id = "example_one_defect_per_round"

    def __init__(self, *, spec: Any, config: Any) -> None:
        self.spec = spec
        self.config = config

    @classmethod
    def contract_identity(cls, config: Any) -> Mapping[str, Any]:
        """What about this proposer decides how it reasons.

        Folded into the epoch's contract hash, so changing any of it rolls
        the epoch and the generations proposed before the change are not
        compared against the ones after. A proposer whose behavior comes
        from files it reads reports their content hashes here; this one's
        behavior is the token order below, so that is what it reports.
        """
        del config
        return {"kind": cls.external_id, "removes": [token for token, _ in DEFECTS]}

    async def propose(self, ctx: Any) -> Experiment:
        """Emit this round's edit and the prediction it is betting on."""
        points = {point.id: point for point in ctx.mutations}
        point = points.get(POLICY_MUTATION_ID)
        if point is None:
            raise ValueError(
                f"no mutation point {POLICY_MUTATION_ID!r} in this generation; the "
                f"# zicato:mutable marker in system_under_test/__init__.py is what "
                f"declares it (found: {sorted(points)})"
            )

        tokens = read_policy(point)
        for token, entry_id in DEFECTS:
            if token in tokens:
                tokens.remove(token)
                core_idea = f"Remove the {token} rule from the writing policy."
                why = (
                    f"{token} is what suppresses the feature the {entry_id} board "
                    f"entry grades, so removing it should turn that entry from fail "
                    f"to pass and leave every other entry where it is."
                )
                pass_rate_delta = "+0.20 to +0.30"
                break
        else:
            tokens.append(INERT_TOKEN)
            core_idea = f"Add the {INERT_TOKEN} rule to the writing policy."
            why = (
                "Every seeded defect is gone, so there is nothing left to remove. "
                f"{INERT_TOKEN} suppresses no feature, so this should score exactly "
                "as the champion does and be rejected."
            )
            pass_rate_delta = "0.00"

        # ``new_content`` is the string the policy becomes. The applier
        # replaces the string-literal node and quotes plain prose itself,
        # so the assignment around the literal survives the edit.
        patch = Patch(
            id=uuid.uuid4().hex,
            mutation_id=POLICY_MUTATION_ID,
            op="replace",
            new_content="; ".join(tokens),
            new_numeric=None,
            new_enum=None,
            rationale=core_idea,
        )
        experiment = Experiment(
            id=f"exp_{ctx.epoch_id}_{ctx.new_generation_id}",
            epoch_id=ctx.epoch_id,
            generation_id=ctx.new_generation_id,
            parent_generation_id=ctx.parent_generation_id,
            proposed_at=datetime.now(UTC).isoformat(),
            hypothesis=HypothesisSpec(
                core_idea=core_idea,
                modulating=(POLICY_MUTATION_ID,),
                why=why,
                expected_drift_movements=(),
                expected_pass_rate_delta=pass_rate_delta,
            ),
            patches=(patch,),
            outcome=None,
        )

        # Every proposer applies its own candidate before returning it.
        # ``ctx.validate_experiment`` writes the child generation snapshot
        # and reports what the patch set did wrong — an edit that misses
        # its anchor, touches a mutation point the hypothesis did not
        # declare, or leaves a Python file unparseable. The round mounts
        # the snapshot this call produced, so a proposer that skips it has
        # returned an experiment with no tree to evaluate. A real proposer
        # revises and calls again, up to ``ctx.max_retries`` times.
        if ctx.validate_experiment is not None:
            findings = await ctx.validate_experiment(experiment)
            if findings:
                raise ProposerError(findings)
        return experiment


__all__ = [
    "DEFECTS",
    "INERT_TOKEN",
    "POLICY_MUTATION_ID",
    "OneDefectPerRound",
    "read_policy",
]
