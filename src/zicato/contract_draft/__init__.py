"""A draft of an evaluation contract and the edits that can be applied to it.

A :class:`~zicato.contract_draft.draft.TournamentDraft` is the editable
working copy of a whole evaluation contract — the scoring weights, the board
with its judges and predicates, the proposer brief and the proposer
directory — and a :class:`~zicato.contract_draft.draft.DraftStore` keys one
draft per session so two editing sessions never tread on each other.

Every editable change flows through an operation in
:mod:`zicato.contract_draft.operations`, which mutates the draft in place and
returns a structured patch describing what changed. The read operations
estimate the cost of running the drafted contract and validate it;
:func:`zicato.contract_draft.operations.apply` writes a confirmed draft to
the workspace and lets the auto-epoch machinery roll the epoch on the next
resolve, or returns a dry-run preview that writes nothing.

Nothing here serves a request or calls a model. The builder driver
(:mod:`zicato.builder`) serves this package over HTTP and drives the same
draft from its copilot, and :mod:`zicato.reflection` stages a finding's
proposed edit on a draft it never seals.
"""
