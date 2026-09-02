"""How much of a board grades what it runs, and when that is too little.

An entry's ``expectation`` is the pass/fail half of the loss: an entry
without one contributes drift loss alone. A board where most entries
carry no expectation still works, because drift loss needs no ground
truth (SCORING.md §1), and is also a common accident in which the
operator meant to attach expectations and did not. Zicato reports it at
both moments an operator can act on it:

* :func:`zicato.check.validators.board_expectation_coverage` reports it
  as an advisory in the pre-spend workspace gate, from the board file
  alone, before a round is paid for;
* :func:`zicato.health.diagnostics.detect_no_expectations` reports it as
  an informational loop-health finding once rounds have run.

This module owns the rule both of them consume: which entries count as
ungraded, what fraction of the board they are, the threshold that
fraction must exceed, and the structured payload the finding carries.
Neither surface re-derives any of it, so the two cannot disagree about
the same board, and an operator who retunes the threshold in the
workspace ``health`` block moves both at once.

The rule is pure. Given the same entries and the same threshold it
returns the same measurement, and it reads no file and no clock.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from zicato.config import HealthConfig, load_config
from zicato.core.types import BoardEntry


@dataclass(frozen=True, slots=True)
class ExpectationCoverage:
    """The ungraded fraction of one board and the threshold it is judged by."""

    #: Entries on the board.
    total: int
    #: Ids of the entries whose ``expectation`` is ``None``, sorted.
    ungraded_ids: tuple[str, ...]
    #: Ungraded entries as a fraction of ``total``; ``0.0`` on an empty board.
    fraction: float
    #: The fraction :attr:`reportable` requires ``fraction`` to exceed.
    threshold: float

    @property
    def reportable(self) -> bool:
        """Whether the ungraded fraction is strictly over the threshold.

        Always ``False`` for a board with no entries at all. That board
        has nothing to say about coverage, and the pre-spend gate already
        stops on it as ``empty_board``.
        """
        return self.total > 0 and self.fraction > self.threshold

    def finding_detail(self) -> dict[str, Any]:
        """The JSON-friendly payload both surfaces attach to their finding."""
        return {
            "entries_without_expectation": len(self.ungraded_ids),
            "total_entries": self.total,
            "fraction": self.fraction,
            "threshold": self.threshold,
            "entry_ids_without_expectation": list(self.ungraded_ids),
        }


def measure_expectation_coverage(
    entries: Sequence[BoardEntry], config: HealthConfig | None = None
) -> ExpectationCoverage:
    """Measure ``entries`` against ``no_expectations_fraction``.

    ``config`` defaults to the process configuration's health block via
    :func:`zicato.config.load_config` — the same resolution the loop-health
    detectors apply when a caller threads no config in. Callers holding a
    workspace-parsed :class:`~zicato.config.HealthConfig`
    (:func:`zicato.config.health_config_from_workspace`) pass it here so
    the operator's tuned threshold is the one applied.
    """
    threshold = (config if config is not None else load_config().health).no_expectations_fraction
    ungraded = tuple(sorted(entry.id for entry in entries if entry.expectation is None))
    total = len(entries)
    return ExpectationCoverage(
        total=total,
        ungraded_ids=ungraded,
        fraction=len(ungraded) / total if total else 0.0,
        threshold=threshold,
    )


__all__ = ["ExpectationCoverage", "measure_expectation_coverage"]
