"""Outcome-marginal aggregation for the proposer failure-signal channel.

Capability 2 of issue #18. The proposer historically saw only a coarsened
``Δscalar`` plus an LLM digest of goldfive *decision* telemetry — it never
saw a summary of *outcome* failure modes (over-retrieval vs misses vs empty
answers), so it could not target *why* answers were wrong, only that a
scalar moved.

This module computes, over a list of per-entry :class:`LossProfile`-shaped
results, a set of **outcome MARGINALS**: board-wide rates (% of runs) for
generic failure modes the core analyzer can derive on its own, plus — when
Capability 1's per-entry ``metrics`` carry ``precision`` / ``recall`` — the
recall-vs-precision decomposition. The result is an aggregate only: no
per-entry join, no entry id, no question text, no output token survives into
the summary.

The design invariant (the whole point — issue #18): feed the MARGINAL,
never the JOINT. The proposer may learn aggregate PROPERTIES OF THE AGENT'S
BEHAVIOUR ("over-retrieves 40% of runs") but must never be able to
reconstruct any board entry, question, or specific output. Three safeguards
are honoured, every one reusing existing machinery:

* **Train slice only.** The caller (the orchestrator) passes the
  *train-slice* losses it already loaded — never the holdout, never a
  rotated-in holdout entry (it threads the same ``split_board`` /
  ``rotation_seed`` partition it uses for the patterns + loss summary).
  This module never reads the board or the filesystem, so it cannot widen
  the slice it is given.
* **Bucketed / coarsened.** Every rendered number is banded
  (:func:`zicato.proposer.prompts.render_failure_mode_profile`), mirroring
  ``_bucket_scalar_delta`` (OVERFITTING.md §11.4) so no round-over-round
  response surface leaks. The raw rates this module returns are reduced to
  bands at the render boundary.
* **Identity-free.** Only marginal rates are produced. There is no entry
  id, question text, or output token anywhere in the summary — by
  construction, since this module reads only the scalar / count fields of
  each profile.

An OPTIONAL operator summarizer hook (item 8) can contribute additional
marginals: it receives the train-slice per-entry results and returns a
STRUCTURED aggregate — a ``{marginal_name: numeric_rate}`` dict, NOT prose —
so zicato can ENFORCE bucketing + anonymity on its output. A free-text
summary would be an un-auditable leak vector and is rejected:
:func:`sanitize_operator_marginals` strips anything non-numeric or
identifying before the operator's marginals are merged.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from zicato.import_path import import_dotted_path

#: An output shorter than this many characters is counted as an EMPTY answer
#: — the agent returned essentially nothing. A generic, board-agnostic
#: threshold: a handful of characters cannot be a real answer to any board
#: question, so this needs no per-board tuning and reveals nothing about
#: any specific entry.
_EMPTY_OUTPUT_CHARS = 1

#: An output at or below this many characters (but above empty) is counted
#: as a TERSE answer — present but implausibly short for a substantive
#: response. Generic and board-agnostic; the marginal is a RATE across runs,
#: so the threshold never surfaces any single entry's length.
_TERSE_OUTPUT_CHARS = 40

#: Precision below this is counted as OVER-RETRIEVAL — the agent returned
#: items that were not relevant (the documented precision-collapse failure).
#: Matches the issue's worked example (``precision < 0.5``).
_OVER_RETRIEVAL_PRECISION = 0.5

#: Drift kinds that mark a LOOPING run. The reducer records both the
#: tool-call and reasoning loop kinds; either one firing at all (count > 0)
#: flags the run as looping for the marginal.
_LOOPING_DRIFT_KINDS: frozenset[str] = frozenset({"looping_reasoning", "looping_tool_call"})

#: Metric keys the recall/precision decomposition reads off each entry's
#: Capability-1 ``metrics`` mapping. Only these two drive the decomposition
#: line; any other metric a scorer happens to carry is ignored here (the
#: operator summarizer hook is the seam for board-specific metrics).
_RECALL_KEY = "recall"
_PRECISION_KEY = "precision"


@dataclass(frozen=True, slots=True)
class OutcomeMarginalSummary:
    """Aggregate outcome marginals over one train-slice of runs.

    Every field is a board-wide RATE or MEAN — never a per-entry value, an
    entry id, a question, or an output token. The summary is the only thing
    that flows toward the proposer prompt, and it is rendered through a
    banding step (:func:`zicato.proposer.prompts.render_failure_mode_profile`)
    that coarsens each number, so no exact per-run value reaches the model.

    Fields
    ------
    n_runs:
        How many train-slice runs contributed. ``0`` means the slice was
        empty (a baseline round with no parent telemetry) — the renderer
        emits no profile section, so the proposer prompt stays
        byte-identical to today.
    empty_rate / terse_rate:
        Fraction of runs whose output was empty / terse (short). Generic,
        board-agnostic failure modes derived from ``output_chars`` alone.
    looping_rate:
        Fraction of runs that recorded any looping drift event.
    pass_rate:
        Fraction of runs that passed, over the runs that carried a
        pass/fail verdict (``None`` when no run did). The binary
        back-compat signal.
    mean_score:
        Mean continuous per-entry score over the runs that carried one
        (``None`` when none did). The Capability-1 continuous quality.
    recall_mean / precision_mean:
        Mean recall / precision over the runs whose Capability-1 ``metrics``
        carried that key (``None`` when none did). The decomposition the
        proposer needs to tell over-retrieval (precision down) from misses
        (recall down) apart.
    over_retrieval_rate:
        Fraction of runs (over those carrying a ``precision`` metric) whose
        precision fell below :data:`_OVER_RETRIEVAL_PRECISION` — the
        over-retrieval marginal from the issue's example.
    operator_marginals:
        Extra marginals contributed by the optional operator summarizer
        hook, already SANITIZED (see :func:`sanitize_operator_marginals`):
        a ``{name: numeric_rate}`` mapping of board-specific aggregates
        (e.g. ``{"no_table_ids": 0.1}``). Every value is a finite float;
        every name is a short identifier-like token. Anything the operator
        tried to return that was non-numeric or identity-bearing has
        already been stripped, so this mapping is safe to band + render.
    """

    n_runs: int = 0
    empty_rate: float = 0.0
    terse_rate: float = 0.0
    looping_rate: float = 0.0
    pass_rate: float | None = None
    mean_score: float | None = None
    recall_mean: float | None = None
    precision_mean: float | None = None
    over_retrieval_rate: float | None = None
    operator_marginals: Mapping[str, float] = field(default_factory=dict)

    def is_empty(self) -> bool:
        """True when there is nothing to surface to the proposer.

        An empty slice (no runs) carries no signal; the renderer treats
        this as the sentinel for "omit the failure-mode profile section
        entirely", so the proposer prompt is byte-identical to today.
        """
        return self.n_runs == 0


def _looping(loss: Any) -> bool:
    """True iff this run recorded any looping drift event."""
    for dc in getattr(loss, "drift_counts", ()) or ():
        kind = getattr(dc, "kind", "")
        count = getattr(dc, "count", 0)
        if kind in _LOOPING_DRIFT_KINDS and count:
            return True
    return False


def _finite_metric(metrics: Any, key: str) -> float | None:
    """Read a finite float ``metrics[key]`` off a per-entry mapping, else ``None``."""
    if not isinstance(metrics, Mapping):
        return None
    raw = metrics.get(key)
    if raw is None:
        return None
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(val):
        return None
    return val


def aggregate_outcome_marginals(
    losses: Iterable[Any],
    *,
    operator_marginals: Mapping[str, float] | None = None,
) -> OutcomeMarginalSummary:
    """Compute the outcome-marginal summary over a TRAIN-slice of runs.

    Parameters
    ----------
    losses:
        The per-entry :class:`~zicato.core.types.LossProfile`-shaped results
        for ONE train slice. The caller is responsible for having already
        excluded the holdout (the orchestrator threads the same
        ``split_board`` partition it uses elsewhere); this function trusts
        the slice it is given and never widens it.
    operator_marginals:
        Already-SANITIZED extra marginals from the optional operator
        summarizer hook (see :func:`sanitize_operator_marginals` /
        :func:`run_operator_summarizer`). ``None`` (the default) contributes
        nothing — the core-only marginals stand alone.

    Returns
    -------
    OutcomeMarginalSummary
        Aggregate rates only. An empty ``losses`` yields a summary whose
        :meth:`~OutcomeMarginalSummary.is_empty` is ``True`` and which the
        renderer omits entirely, so the proposer prompt is unchanged.
    """
    items = list(losses)
    n = len(items)
    if n == 0:
        return OutcomeMarginalSummary(
            operator_marginals=dict(operator_marginals or {}),
        )

    empty = 0
    terse = 0
    looping = 0

    pass_vals: list[bool] = []
    score_vals: list[float] = []
    recall_vals: list[float] = []
    precision_vals: list[float] = []

    for loss in items:
        chars = int(getattr(loss, "output_chars", 0) or 0)
        if chars <= _EMPTY_OUTPUT_CHARS:
            empty += 1
        elif chars <= _TERSE_OUTPUT_CHARS:
            terse += 1

        if _looping(loss):
            looping += 1

        pf = getattr(loss, "pass_fail", None)
        if pf is not None:
            pass_vals.append(bool(pf))

        score = getattr(loss, "score", None)
        if score is not None:
            try:
                sval = float(score)
            except (TypeError, ValueError):
                sval = None
            if sval is not None and math.isfinite(sval):
                score_vals.append(sval)

        metrics = getattr(loss, "metrics", None)
        rec = _finite_metric(metrics, _RECALL_KEY)
        if rec is not None:
            recall_vals.append(rec)
        prec = _finite_metric(metrics, _PRECISION_KEY)
        if prec is not None:
            precision_vals.append(prec)

    pass_rate = (sum(1 for p in pass_vals if p) / len(pass_vals)) if pass_vals else None
    mean_score = (sum(score_vals) / len(score_vals)) if score_vals else None
    recall_mean = (sum(recall_vals) / len(recall_vals)) if recall_vals else None
    precision_mean = (sum(precision_vals) / len(precision_vals)) if precision_vals else None
    over_retrieval_rate = (
        sum(1 for p in precision_vals if p < _OVER_RETRIEVAL_PRECISION) / len(precision_vals)
        if precision_vals
        else None
    )

    return OutcomeMarginalSummary(
        n_runs=n,
        empty_rate=empty / n,
        terse_rate=terse / n,
        looping_rate=looping / n,
        pass_rate=pass_rate,
        mean_score=mean_score,
        recall_mean=recall_mean,
        precision_mean=precision_mean,
        over_retrieval_rate=over_retrieval_rate,
        operator_marginals=dict(operator_marginals or {}),
    )


#: Maximum length of a sanitized operator-marginal NAME. A marginal name is
#: an aggregate label like ``"no_table_ids"`` — short by nature. A long
#: string is exactly the shape an entry id / question text would take, so we
#: reject anything past this ceiling outright rather than try to scrub it.
_MAX_MARGINAL_NAME_CHARS = 48

#: The character set a sanitized marginal NAME may use: lowercase letters,
#: digits, and the separators an aggregate label needs. Anything else (a
#: space, a quote, a path separator, punctuation that could carry a question
#: fragment or an output token) disqualifies the name — the operator must
#: name its marginals like identifiers, not like prose.
_MARGINAL_NAME_ALLOWED = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_-:.")


def _valid_marginal_name(name: object) -> bool:
    """True iff ``name`` is a safe, identifier-like aggregate label.

    Rejects anything that is not a short, lowercase, identifier-shaped
    token — the shape an aggregate marginal label takes. A free-form string,
    a question fragment, an entry id with mixed case / spaces / punctuation,
    or an over-long token is rejected, so an operator cannot smuggle
    identity through a marginal NAME.
    """
    if not isinstance(name, str):
        return False
    if not name or len(name) > _MAX_MARGINAL_NAME_CHARS:
        return False
    lowered = name.lower()
    if lowered != name:
        # An identity-bearing token (an entry id, a CamelCase question word)
        # is far more likely to carry case than an aggregate label; require
        # all-lowercase so a name is unmistakably a label.
        return False
    return all(ch in _MARGINAL_NAME_ALLOWED for ch in name)


def sanitize_operator_marginals(raw: object) -> dict[str, float]:
    """Strip an operator summarizer's output down to safe numeric marginals.

    The operator summarizer hook is REQUIRED to return a STRUCTURED
    aggregate — a ``{marginal_name: numeric_rate}`` mapping — precisely so
    zicato can enforce bucketing + anonymity on its output. This function is
    that enforcement boundary. It keeps ONLY entries where:

    * the key is a short, lowercase, identifier-like label
      (:func:`_valid_marginal_name`) — so no question text / entry id /
      output token can ride in as a key; and
    * the value coerces to a FINITE float — so no free-text summary, no
      list, no nested object, nothing non-numeric survives.

    A summarizer that returns a free string, an entry id as a key, a value
    that is itself a string / list / dict, or a non-finite number has those
    entries DROPPED here. The result is a plain ``{str: float}`` mapping
    that the aggregator stores and the renderer bands — there is no path by
    which a non-numeric or identity-bearing value reaches the proposer.

    A non-mapping input (the operator returned prose, a list, ``None``)
    yields an empty mapping: the whole un-auditable return is discarded.
    """
    if not isinstance(raw, Mapping):
        return {}
    out: dict[str, float] = {}
    for key, value in raw.items():
        if not _valid_marginal_name(key):
            continue
        # bool is a subclass of int; a bool marginal is meaningless as a
        # rate, so reject it rather than coercing True -> 1.0.
        if isinstance(value, bool):
            continue
        if not isinstance(value, int | float):
            continue
        val = float(value)
        if not math.isfinite(val):
            continue
        out[key] = val
    return out


def run_operator_summarizer(spec: str, losses: Iterable[Any]) -> dict[str, float]:
    """Resolve + invoke the operator outcome-summarizer hook, then sanitize.

    ``spec`` is a dotted path (``pkg.mod:fn`` or ``pkg.mod.fn``) resolved the
    same way predicates / judges resolve theirs
    (:func:`zicato.import_path.import_dotted_path`). The resolved callable
    receives the TRAIN-slice per-entry results and is expected to return a
    STRUCTURED aggregate — a ``{marginal_name: numeric_rate}`` mapping. Its
    return is passed straight through :func:`sanitize_operator_marginals`,
    which strips anything non-numeric or identity-bearing, so a misbehaving
    summarizer (one that returns prose, an entry id, or a free string)
    contributes nothing rather than leaking.

    Best-effort: a bad spec, a non-callable target, or a summarizer that
    raises yields an empty mapping rather than aborting the round — the
    proposer simply runs without the operator's extra marginals, exactly as
    it does when no summarizer is configured.
    """
    if not spec:
        return {}
    try:
        fn = import_dotted_path(spec, label=f"outcome summarizer {spec!r}")
    except (ImportError, ValueError):
        return {}
    if not callable(fn):
        return {}
    try:
        raw = fn(list(losses))
    except Exception:  # noqa: BLE001 — an operator hook must never abort the round
        return {}
    return sanitize_operator_marginals(raw)


__all__ = [
    "OutcomeMarginalSummary",
    "aggregate_outcome_marginals",
    "run_operator_summarizer",
    "sanitize_operator_marginals",
]
