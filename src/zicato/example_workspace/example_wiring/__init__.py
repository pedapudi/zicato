"""Everything the loop needs in order to measure the system under test.

Four modules, one per thing a zicato project has to supply, and the
workspace ``config.json`` names each by dotted path:

``adapter``
    Runs one board entry against one generation snapshot. Named by
    ``adapter.factory``.
``predicates``
    Grades a run's output. Named by each board entry's ``expectation``.
``proposer``
    Produces the next candidate edit. Named by ``runtime.proposer_agent``.
``models``
    The callables the ``target`` and ``evaluation`` model roles run on.
    Named under ``models.engines``.

None of it is mutated by the loop: this is the operator's measuring
apparatus, and a proposer that could rewrite its own grader would be
scoring itself.
"""

from __future__ import annotations

__all__: list[str] = []
