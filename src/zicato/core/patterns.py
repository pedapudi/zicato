"""Pattern type: a loss-pattern observation surfaced by a detector.

Split out of :mod:`zicato.core.types`; re-exported from there and from
:mod:`zicato.core` so existing import paths keep working.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Pattern:
    """A loss-pattern observation surfaced by a pattern detector.

    Patterns are the bridge from raw :class:`LossProfile` instances to
    proposer-actionable observations. Detectors produce a list of
    :class:`Pattern` objects each generation; the proposer reads the
    list and decides which to address. :attr:`kind` is open-ended (a
    bare string rather than a Literal) so new detector kinds can be added
    without breaking the schema.

    Fields
    ------
    id:
        Stable pattern identifier within a generation.
    kind:
        Detector-defined kind string. Conventional values include
        ``"drift_kind_frequency"`` (one drift kind dominates),
        ``"hot_task"`` (one task id drifts disproportionately often),
        ``"hot_agent"`` (one agent id is overrepresented in drift
        sources). New detectors register new kinds without coordinating
        with the type module.
    summary:
        One-line human-readable description for the journal.
    detail:
        Kind-specific structured payload. String-valued for JSON
        cleanliness; consumers (the proposer) parse known fields per
        kind and ignore the rest.
    affected_mutation_ids:
        Suggested mutation points the proposer might target if it
        chooses to address this pattern. The proposer is not required
        to act on the suggestion — patterns are advisory, not
        prescriptive.
    severity:
        Detector-assigned severity. Same scale as drift severity for
        consistency in journal rendering.
    """

    id: str
    kind: str
    summary: str
    detail: Mapping[str, str]
    affected_mutation_ids: tuple[str, ...] = ()
    severity: Literal["info", "warning", "critical"] = "info"
