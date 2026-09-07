"""Malformed authored edits fail before the draft can lose their original types."""

from __future__ import annotations

import pytest

from zicato.contract_draft import operations as ops
from zicato.contract_draft.draft import TournamentDraft


@pytest.mark.parametrize(
    ("operation", "arguments"),
    [
        ("set_screening", {"entries": True}),
        ("set_screening", {"entries": 2.8}),
        ("set_screening", {"veto_only": "false"}),
        ("set_proposer_quality", {"best_of_n": "4"}),
        ("set_gate", {"namespace_monotonicity": {"judge:": "false"}}),
        ("set_gate", {"regression_test_command": ["pytest", 7]}),
        ("set_weights", {"per_judge_weights": {"quality": "0.5"}}),
        ("set_namespace_weights", {"namespace_weights": {"judge:": True}}),
        ("set_holdout", {"ladder": {"budget": 2.8}}),
        ("set_holdout", {"ladder": {"enabled": "false"}}),
        ("set_experimental", {"recombine": True, "process_exemplars": False}),
        ("set_experimental", {"cross_epoch_memory": "false"}),
        ("set_experimental", {"max_generations_per_contract": 2.5}),
        ("set_experimental", {"genealogy": 4, "standing_rating": "unknown"}),
    ],
)
def test_library_rejects_types_before_mutating_draft(operation, arguments):
    draft = TournamentDraft()
    before = draft.to_dict()
    with pytest.raises(ValueError):
        getattr(ops, operation)(draft, **arguments)
    assert draft.to_dict() == before


def test_explicit_clear_values_and_numeric_json_keep_their_meaning():
    draft = TournamentDraft()
    ops.set_gate(draft, holdout_margin=0.5)
    ops.set_gate(draft, holdout_margin=-1)
    assert draft.scoring.holdout_margin is None
    ops.set_experimental(draft, max_generations_per_contract=4)
    ops.set_experimental(draft, max_generations_per_contract=0)
    assert draft.scoring.experimental.max_generations_per_contract is None
    ops.set_weights(draft, per_judge_weights={"quality": 2})
    assert draft.scoring.per_judge_weights == {"quality": 2.0}
