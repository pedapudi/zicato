"""Scoring helpers: per-run drift loss and per-generation aggregation.

Exact aggregation
-----------------
Every float total on this path is computed with :func:`math.fsum`, never
with the builtin ``sum`` and never with a running ``+=`` accumulator.
``math.fsum`` returns the correctly-rounded exact sum, which makes an
aggregate a function of its inputs alone: independent of the order the
terms arrive in, and — the reason this is an invariant rather than a
preference — independent of the interpreter. CPython 3.12 changed the
builtin ``sum`` over floats to compensated summation, so the same board
scored under 3.11 and 3.12 produced ``0.39999999999999997`` and ``0.4``
for the same inputs. These values are served, compared against contract
margins, and frozen byte-for-byte in the parity goldens; none of that may
depend on which interpreter ran the round.

The aggregator collapses a list of :class:`~zicato.core.LossProfile`
instances (one per board entry executed under one generation) into a
single dict that is comparable across generations under the same
epoch's :class:`~zicato.core.ScoringWeights`. The dict carries:

* ``drift_loss_mean`` — mean ``drift_loss`` across the list.
* ``pass_rate`` — fraction of entries with an attached expectation that
  passed. Entries whose :attr:`LossProfile.pass_fail` is ``None`` (no
  expectation, or expectation could not be evaluated — e.g. budget
  exceeded before the matcher fired) are EXCLUDED from both numerator
  and denominator. When no entries had pass/fail at all, pass rate is
  reported as ``1.0`` so the ``(1 - pass_rate)`` term does not penalize
  a board that simply lacks expectations. Kept for display / the gate's
  per-entry scope alongside the continuous ``mean_score``.
* ``mean_score`` — the UNIFORM continuous outcome axis: the arithmetic
  mean of each entry's :func:`entry_score` (a bool maps to ``1.0`` /
  ``0.0``; a continuous score is clamped to ``[0, 1]``) over the same
  expectation denominator as ``pass_rate``. This is what the scalar's
  pass component runs on. Because a bool maps to exactly
  ``float(pass_fail)``, ``mean_score`` equals ``pass_rate`` byte-for-byte
  on an all-bool board, so the substitution is back-compat-neutral.
* ``expectation_count`` — number of entries that contributed to pass
  rate / mean_score (denominator).
* ``entry_count`` — number of entries that contributed to drift loss
  (denominator for the drift term).
* ``scalar`` — the multi-objective combined score. Lower = better.
* ``per_entry`` — ``{entry_id: {"drift_loss": float, "failure": float,
  "pass_fail": bool|None, "score": float|None}}`` for entry-level deltas the
  gate needs. ``score`` is the per-entry :func:`entry_score` the gate's
  per-entry continuous-monotonicity scope reads; ``failure`` is the entry's
  ``failure:`` channel total, which is what explains an aborted unit whose
  ``drift_loss`` is legitimately ``0.0``.
* ``namespace_aggregates`` — ``{namespace: weighted_aggregate}`` for
  every namespace observed in the inputs or named in
  :attr:`ScoringWeights.namespace_weights`. Each value is the
  namespace's per-run mean already multiplied by its weight, so it is
  ready to add directly into the scalar.
* ``scalar_components`` — ``{component_name: contribution}`` whose
  values sum exactly to ``scalar``: ``"pass"`` (the ``(1 - mean_score)``
  term, which equals ``(1 - pass_rate)`` on an all-bool board) plus one
  entry per namespace, keyed by the colon-stripped namespace
  name and written in sorted namespace order. When the opt-in
  diff-complexity term is active (``diff_complexity_weight > 0`` AND a
  ``diff_size`` was threaded for the candidate) a final ``"diff_complexity"``
  entry is appended; at the default weight ``0.0`` the key is absent.

The aggregation is intentionally cheap and deterministic; it does NOT re-derive
``drift_loss`` from raw drift counts (that derivation lives in the telemetry
reducer). :func:`per_run_drift_loss` is exposed as the canonical hook for
callers who want to re-derive; it returns ``loss.drift_loss``. Should the
telemetry reducer's formula ever need to diverge from the tournament's view,
the divergence is bounded to this single function.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from zicato.core import (
    JudgeError,
    JudgeLoss,
    LossProfile,
    MetricCount,
    MetricSeverity,
    ScoringWeights,
)
from zicato.epoch._storage import RecordError, check_record_format
from zicato.scoring import ScalarContext, builtin_scalar, resolve_scalar
from zicato.scoring.builtins import diff_complexity_component
from zicato.storage import atomic_write_text
from zicato.workspace.layout import WorkspaceLayout


@dataclass(frozen=True, slots=True)
class GenerationScore:
    """One accepted aggregate, retaining historical omissions and numeric types."""

    generation_id: str | None
    scalar: int | float
    drift_loss_mean: int | float | None
    pass_rate: int | float | None
    mean_score: int | float | None
    _json: str = field(repr=False)

    def to_dict(self) -> dict[str, Any]:
        """Return a detached aggregate for gates and presentation."""
        body: dict[str, Any] = json.loads(self._json)
        for key in ("generation_id", "scalar", "drift_loss_mean", "pass_rate", "mean_score"):
            value = getattr(self, key)
            if key in body or value is not None:
                body[key] = value
        return body


@dataclass(frozen=True, slots=True)
class ScoreMeasurement:
    """One retained generation measurement in its recorded write order."""

    score: GenerationScore
    seq: int
    round_index: int | None

    def to_dict(self) -> dict[str, Any]:
        return dict(self.score.to_dict(), seq=self.seq, round_index=self.round_index)


def _score_number(value: Any, name: str, *, nullable: bool = False) -> int | float | None:
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise RecordError(f"gen_score: {name} must be finite" + (" or null" if nullable else ""))
    return value


def _score_count(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RecordError(f"gen_score: {name} must be a nonnegative integer")
    return value


def decode_gen_score(value: Any, *, generation_id: str | None = None) -> GenerationScore:
    """Accept an aggregate without supplying absent historical display fields."""
    if not isinstance(value, dict):
        raise RecordError("gen_score: expected a JSON object")
    check_record_format(value, "gen_score")
    recorded_id = value.get("generation_id")
    if "generation_id" in value and (not isinstance(recorded_id, str) or not recorded_id):
        raise RecordError("gen_score: generation_id must be a nonempty string")
    if generation_id is not None and recorded_id is not None and recorded_id != generation_id:
        raise RecordError(
            f"gen_score: generation_id {recorded_id!r} does not match {generation_id!r}"
        )
    scalar = _score_number(value.get("scalar"), "scalar")
    assert scalar is not None
    numbers = {
        key: _score_number(value[key], key, nullable=True) if key in value else None
        for key in ("drift_loss_mean", "pass_rate", "mean_score")
    }
    for key in ("entry_count", "expectation_count"):
        if key in value:
            _score_count(value[key], key)
    for key in ("namespace_aggregates", "scalar_components", "diff_size", "per_entry"):
        if key not in value:
            continue
        mapping = value[key]
        if not isinstance(mapping, dict) or any(not isinstance(name, str) for name in mapping):
            raise RecordError(f"gen_score: {key} must be an object with string keys")
        for name, item in mapping.items():
            if key == "diff_size":
                _score_count(item, f"diff_size.{name}")
            elif key != "per_entry":
                _score_number(item, f"{key}.{name}")
            else:
                if not isinstance(item, dict):
                    raise RecordError(f"gen_score: per_entry.{name} must be an object")
                for metric in ("drift_loss", "failure", "score"):
                    if metric in item:
                        _score_number(
                            item[metric], f"per_entry.{name}.{metric}", nullable=metric == "score"
                        )
                verdict = item.get("pass_fail")
                if verdict is not None and not isinstance(verdict, bool):
                    raise RecordError(
                        f"gen_score: per_entry.{name}.pass_fail must be boolean or null"
                    )
    for key in ("scalar_provenance", "source_epoch", "source_run"):
        if key in value and not isinstance(value[key], str):
            raise RecordError(f"gen_score: {key} must be a string")
    if "cached" in value and not isinstance(value["cached"], bool):
        raise RecordError("gen_score: cached must be boolean")
    try:
        encoded = json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise RecordError(f"gen_score: {exc}") from exc
    return GenerationScore(
        recorded_id,
        scalar,
        numbers["drift_loss_mean"],
        numbers["pass_rate"],
        numbers["mean_score"],
        encoded,
    )


def read_gen_score(
    layout: WorkspaceLayout, epoch_id: str, generation_id: str
) -> GenerationScore | None:
    """Read the canonical aggregate; absence is distinct from a malformed record."""
    path = layout.gen_score(epoch_id, generation_id)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError) as exc:
        raise RecordError(f"{path}: {exc}") from exc
    try:
        return decode_gen_score(json.loads(text), generation_id=generation_id)
    except (ValueError, RecordError) as exc:
        raise RecordError(f"{path}: {exc}") from exc


def _score_history(text: str, generation_id: str) -> tuple[tuple[ScoreMeasurement, ...], str]:
    """Decode complete records; only an unterminated, invalid final JSON line is torn."""
    rows: list[ScoreMeasurement] = []
    accepted: list[str] = []
    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if not line.strip():
            accepted.append(line)
            continue
        try:
            value = json.loads(line)
        except ValueError as exc:
            if index == len(lines) - 1 and not line.endswith("\n"):
                return tuple(rows), "".join(accepted)
            raise RecordError(f"gen_score.history: line {index + 1}: {exc}") from exc
        if not isinstance(value, dict):
            raise RecordError(f"gen_score.history: line {index + 1} must be an object")
        score = decode_gen_score(
            {key: item for key, item in value.items() if key not in {"seq", "round_index"}},
            generation_id=generation_id,
        )
        seq = _score_count(value.get("seq"), "seq")
        if seq != len(rows):
            raise RecordError(f"gen_score.history: expected seq {len(rows)}, found {seq}")
        if "round_index" not in value:
            raise RecordError("gen_score.history: missing round_index")
        round_index = value["round_index"]
        if round_index is not None:
            _score_count(round_index, "round_index")
        rows.append(ScoreMeasurement(score, seq, round_index))
        accepted.append(line)
    return tuple(rows), text


def _read_score_history(
    path: Path, generation_id: str
) -> tuple[tuple[ScoreMeasurement, ...], str, bool]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return (), "", False
    except (OSError, UnicodeError) as exc:
        raise RecordError(f"{path}: {exc}") from exc
    try:
        rows, prefix = _score_history(text, generation_id)
        return rows, prefix, prefix != text
    except RecordError as exc:
        raise RecordError(f"{path}: {exc}") from exc


def read_gen_score_history(
    layout: WorkspaceLayout,
    epoch_id: str,
    generation_id: str,
) -> tuple[ScoreMeasurement, ...]:
    """Read retained measurements in order, refusing corruption within the history."""
    return _read_score_history(layout.gen_score_history(epoch_id, generation_id), generation_id)[0]


def write_gen_score(
    workspace_root: Path,
    epoch_id: str,
    generation_id: str,
    aggregate: dict[str, Any],
    *,
    round_index: int | None = None,
) -> None:
    """Retain a measurement before atomically publishing its flat aggregate.

    The caller holds the workspace writer lease. History is flushed before the
    flat aggregate is atomically replaced. A crash between those writes leaves
    a complete measurement and the prior aggregate. Only a torn final line is
    replaced when appending; malformed complete records prevent either write.
    Both formats retain sorted keys, original numeric types, and final newlines.
    """
    if round_index is not None:
        _score_count(round_index, "round_index")
    payload = dict(aggregate)
    payload.setdefault("generation_id", generation_id)
    score = decode_gen_score(payload, generation_id=generation_id)
    layout = WorkspaceLayout.from_root(workspace_root)
    # Refuse corruption in either authority before replacing any recorded facts.
    read_gen_score(layout, epoch_id, generation_id)
    history_path = layout.gen_score_history(epoch_id, generation_id)
    history, text, torn_tail = _read_score_history(history_path, generation_id)
    record = score.to_dict()
    record.update(seq=len(history), round_index=round_index)
    line = (
        ("\n" if text and not text.endswith("\n") else "")
        + json.dumps(record, sort_keys=True)
        + "\n"
    )
    if torn_tail or not history_path.exists():
        atomic_write_text(history_path, text + line)
    else:
        with history_path.open("a", encoding="utf-8") as stream:
            stream.write(line)
            stream.flush()
            os.fsync(stream.fileno())
    atomic_write_text(
        layout.gen_score(epoch_id, generation_id),
        json.dumps(score.to_dict(), indent=2, sort_keys=True) + "\n",
    )


def entry_score(loss: LossProfile) -> float | None:
    """Return one loss's continuous outcome score in ``[0, 1]``, or ``None``.

    The single, UNIFORM mapping the scalar and gate read:

    * a profile with an explicit continuous ``score`` returns that value,
      clamped to ``[0.0, 1.0]`` (a non-finite value is treated as a miss,
      ``0.0``, so a rogue scorer can never poison the mean);
    * otherwise the binary ``pass_fail`` bit maps to ``1.0`` / ``0.0``;
    * an entry with neither a score NOR a pass/fail (``pass_fail is None``
      and ``score is None`` — no expectation, or one that could not fire)
      returns ``None`` and is EXCLUDED from the mean, exactly as the binary
      ``pass_rate`` already excludes ``pass_fail is None``.

    The bool path is exactly ``float(pass_fail)``, so a board whose entries
    are all bool produces a per-entry score sequence identical to its
    pass/fail sequence. ``mean_score`` over that sequence therefore equals
    the binary ``pass_rate`` byte-for-byte — the property the all-bool
    back-compat proof test pins.

    Reads ``score`` via ``getattr`` so a duck-typed loss stand-in (or a
    profile materialised before the field existed) that carries only
    ``pass_fail`` still resolves to the binary outcome.
    """
    raw_score = getattr(loss, "score", None)
    if raw_score is not None:
        value = float(raw_score)
        if not math.isfinite(value):
            return 0.0
        if value < 0.0:
            return 0.0
        if value > 1.0:
            return 1.0
        return value
    if loss.pass_fail is None:
        return None
    return 1.0 if loss.pass_fail else 0.0


def per_run_drift_loss(loss: LossProfile, weights: ScoringWeights) -> float:
    """Return the scalar drift-loss for one run.

    The :class:`LossProfile` already carries a reducer-computed
    ``drift_loss`` field; this function exists so the tournament has a
    single canonical re-derivation hook. Today it simply returns
    ``loss.drift_loss``.

    The *weights* argument is accepted for API symmetry with the
    reducer's ``compute_drift_loss`` shape — callers can pass it
    confidently knowing the function will not silently ignore changes
    if the formula ever has to diverge between the two sites.
    """
    # The weights are intentionally unused right now; LossProfile.drift_loss
    # is the reducer's canonical output and the tournament trusts it.
    del weights
    return loss.drift_loss


def _namespace_of(metric_name: str) -> str:
    """Return the ``"<prefix>:"`` namespace of a metric name.

    Returns ``""`` (the empty string) for unnamespaced names so callers
    can skip them uniformly. The namespace is everything up to and
    including the first colon, matching the convention documented on
    :class:`zicato.core.MetricCount`.
    """
    idx = metric_name.find(":")
    if idx < 0:
        return ""
    return metric_name[: idx + 1]


def _within_channel_weight(metric_name: str, weights: ScoringWeights) -> float:
    """Return the within-channel multiplier for one metric, ``1.0`` by default.

    The ``failure:`` channel's two members carry contract magnitudes the way
    ``drift:`` carries ``severity_weights × per_kind_weights`` (applied inside
    ``drift_loss``) and ``judge:`` carries ``per_judge_weights`` (applied
    inside ``per_judge_loss.weighted_loss``). Both live on the contract, so
    retuning either rolls the epoch.

    Every other metric enters its channel at its measured value; the channel's
    ``namespace_weights`` coefficient is what scales it.
    """
    if metric_name == "failure:tasks":
        return weights.task_failure_weight
    if metric_name == "failure:not_completed":
        return weights.not_completed_weight
    return 1.0


def _failure_channel_total(loss: LossProfile, weights: ScoringWeights) -> float:
    """Return one run's ``failure:`` channel total, before its coefficient.

    ``task_failure_weight × task_failure_ratio + not_completed_weight`` (the
    latter only for a run that did not complete) — the same two members
    :meth:`LossProfile.unified_metrics` derives and
    :func:`aggregate_namespaced_metrics` sums, computed per entry so the
    evidence surface can explain an aborted unit's contribution.

    Reads both fields via ``getattr`` so a duck-typed loss stand-in — the
    projected-standings placeholder, or a profile materialised before the
    fields existed — resolves to "completed, nothing failed", mirroring
    :func:`entry_score`'s defensive read.
    """
    total = weights.task_failure_weight * float(getattr(loss, "task_failure_ratio", 0.0) or 0.0)
    if getattr(loss, "not_completed", False):
        total += weights.not_completed_weight
    return total


def aggregate_namespaced_metrics(
    losses: list[LossProfile],
    weights: ScoringWeights,
) -> dict[str, float]:
    """Compute the weighted per-namespace aggregate across ``losses``.

    For the ``"drift:"`` namespace the aggregate is
    ``namespace_weights["drift:"] * mean(LossProfile.drift_loss)``: drift is
    reduced per run into ``drift_loss`` (Seam 1) and enters the scalar as one
    channel rather than as its per-``(kind, severity)`` buckets.

    For every other namespace the aggregate is
    ``namespace_weights[namespace] * mean(Σ within-channel-weighted
    MetricCount.count)`` over the namespace's entries across all losses'
    unified metric view (see :meth:`LossProfile.unified_metrics`, which
    derives the ``judge:`` / ``failure:`` / ``runtime:`` members).
    Namespaces named in :attr:`ScoringWeights.namespace_weights` but absent
    from the loss data appear with an aggregate of ``0.0`` so the keys are
    predictable for downstream consumers.

    Unnamespaced metric names (no colon prefix) are silently ignored.
    Namespaces present in the data but with no weight configured are
    aggregated at weight ``0.0`` (so they show up as ``0.0`` in the
    return value and contribute nothing to the scalar).

    Returns ``{namespace: weighted_aggregate}``. The mapping keys
    preserve the trailing colon (``"drift:"``, ``"cost:"``, ...).
    """
    namespace_means: dict[str, float] = {}

    if losses:
        # ``math.fsum``, never the builtin: see the module docstring on
        # exact aggregation. This mean reaches every served scalar, so a
        # summation whose result depends on the interpreter version would
        # make the parity goldens depend on it too.
        drift_total = math.fsum(per_run_drift_loss(loss, weights) for loss in losses)
        drift_mean = drift_total / len(losses)
    else:
        drift_mean = 0.0
    drift_weight = weights.namespace_weights.get("drift:", 0.0)
    namespace_means["drift:"] = drift_weight * drift_mean

    # Collect the per-namespace running sums across every loss's
    # unified metric view. We track counts per namespace independently
    # so that the per-run mean is well-defined even when one loss
    # contributes no entries for a given namespace (its contribution to
    # the sum is zero by convention — same model as
    # drift_loss_mean / entry_count above).
    # Terms are collected per namespace and summed ONCE with ``math.fsum``
    # rather than accumulated in a running float, so the aggregate does not
    # depend on the order the losses arrive in.
    terms: dict[str, list[float]] = {}
    n_losses = len(losses)
    for loss in losses:
        # Sum within one loss first so a loss with multiple entries in
        # the same namespace counts each entry, and a loss with none
        # contributes zero.
        per_loss: dict[str, list[float]] = {}
        for mc in loss.unified_metrics():
            ns = _namespace_of(mc.name)
            if not ns or ns == "drift:":
                # Drift is already reduced into ``drift_loss`` above; its
                # MetricCount mirrors — including the ``drift:custom``
                # judge-attributed ones the judge: channel scores — are
                # skipped here so nothing is counted twice.
                continue
            per_loss.setdefault(ns, []).append(mc.count * _within_channel_weight(mc.name, weights))
        for ns, values in per_loss.items():
            terms.setdefault(ns, []).append(math.fsum(values))
    sums: dict[str, float] = {ns: math.fsum(values) for ns, values in terms.items()}

    # Promote known-but-absent namespaces to zero aggregates so
    # downstream consumers iterate a stable key set.
    observed_namespaces = set(sums.keys()) | set(weights.namespace_weights.keys())
    for ns in observed_namespaces:
        if ns == "drift:":
            continue
        mean_val = sums.get(ns, 0.0) / n_losses if n_losses > 0 else 0.0
        ns_weight = weights.namespace_weights.get(ns, 0.0)
        namespace_means[ns] = ns_weight * mean_val

    return namespace_means


def _per_judge_loss_aggregate(losses: list[LossProfile]) -> dict[str, float]:
    """Mean per-judge ``weighted_loss`` across ``losses``, keyed by judge_name.

    Carried onto :class:`~zicato.scoring.api.ScalarContext` purely for plugin
    / provenance visibility — the built-in scalar does NOT add it separately
    (the same values reach it as the ``judge:<name>`` metrics of the
    ``judge:`` channel). Summing each judge's per-run
    ``weighted_loss`` and dividing by the run count mirrors the per-run mean
    model the rest of the aggregation uses; a judge absent from a run
    contributes zero to its sum. Returns ``{}`` for empty input.
    """
    if not losses:
        return {}
    terms: dict[str, list[float]] = {}
    for loss in losses:
        # ``getattr`` (not attribute access) so a duck-typed loss stand-in —
        # the projected-standings ``_FakeLoss``, or a profile materialised
        # before the field existed — that carries no ``per_judge_loss`` is
        # tolerated, mirroring :func:`entry_score`'s defensive ``getattr``.
        for jl in getattr(loss, "per_judge_loss", ()) or ():
            terms.setdefault(jl.judge_name, []).append(jl.weighted_loss)
    n = len(losses)
    return {name: math.fsum(values) / n for name, values in terms.items()}


def aggregate_generation_score(
    losses: list[LossProfile],
    weights: ScoringWeights,
    diff_size: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Aggregate per-entry losses into a per-generation summary dict.

    See module docstring for the dict shape. Empty input returns
    ``drift_loss_mean=0.0``, ``pass_rate=1.0``, both counts zero, and
    ``scalar=0.0`` — equivalent to "nothing to compare", which the gate
    treats as a tie (no improvement).

    ``diff_size`` is the OPT-IN parsimony / MDL input: the candidate
    generation's ``{added, removed, patches}`` diff size (see
    :func:`zicato.scoring.diff_complexity.diff_size`). ``None`` (the default)
    or a contract whose :attr:`~zicato.core.types.ScoringWeights.diff_complexity_weight`
    is ``0.0`` leaves the result BYTE-IDENTICAL — the ``diff_complexity``
    component is never written and the scalar is unchanged. The runner threads
    it only for the CHALLENGER side; the champion side passes ``None`` so the
    term measures the challenger's diff, exactly as the gate compares it against
    a champion baseline that pays no parsimony cost.
    """
    incomplete_entries = sorted(
        {loss.entry_id for loss in losses if loss.execution_started is False}
    )
    losses = [loss for loss in losses if loss.execution_started is not False]
    per_entry: dict[str, dict[str, Any]] = {}
    # Collected, then summed once with ``math.fsum`` — see the module
    # docstring on exact aggregation. Counts stay integer accumulators;
    # only the float terms need the exact sum.
    drift_terms: list[float] = []
    score_terms: list[float] = []
    pass_count = 0
    expectation_count = 0

    for loss in losses:
        drift = per_run_drift_loss(loss, weights)
        entry_outcome = entry_score(loss)
        per_entry[loss.entry_id] = {
            "drift_loss": drift,
            # The entry's within-channel ``failure:`` total, the analogue of
            # ``drift_loss`` for the failure channel (both are pre-namespace-
            # coefficient). Without it an aborted unit shows an empty
            # ``drift_loss`` of 0.0 and nothing explaining the loss it
            # contributed.
            "failure": _failure_channel_total(loss, weights),
            "pass_fail": loss.pass_fail,
            # The continuous per-entry outcome the gate's per_entry scope
            # reads. ``None`` for an entry with no expectation; a bool
            # entry carries 1.0/0.0 so the gate can treat bool and float
            # entries uniformly.
            "score": entry_outcome,
        }
        drift_terms.append(drift)
        if loss.pass_fail is not None:
            expectation_count += 1
            if loss.pass_fail:
                pass_count += 1
        if entry_outcome is not None:
            score_terms.append(entry_outcome)

    entry_count = len(losses)
    score_count = len(score_terms)
    drift_loss_mean = math.fsum(drift_terms) / entry_count if entry_count > 0 else 0.0
    pass_rate = pass_count / expectation_count if expectation_count > 0 else 1.0
    # ``mean_score`` is the UNIFORM outcome axis: the arithmetic mean of each
    # entry's continuous :func:`entry_score` over every entry that produced
    # one (``score_count``). On an all-bool board every entry with a
    # pass/fail also produces a score and every entry without one produces
    # neither, so ``score_count == expectation_count`` AND
    # the score terms sum to ``pass_count`` — hence ``mean_score == pass_rate``
    # byte-for-byte. That is the back-compat proof: substituting mean_score
    # for pass_rate in the pass component below is a no-op on all-bool
    # boards. A board with no scored entries reports 1.0, exactly as
    # pass_rate does, so the (1 - mean_score) term contributes zero.
    mean_score = math.fsum(score_terms) / score_count if score_count > 0 else 1.0

    # Namespace aggregates are already weight-multiplied per
    # :func:`aggregate_namespaced_metrics` — they slot straight into
    # the scalar.
    namespace_aggregates = aggregate_namespaced_metrics(losses, weights)

    # The pass component runs on the UNIFORM mean_score rather than the binary
    # pass_rate. On an all-bool board mean_score == pass_rate (see above), so
    # this equals ``pass_weight * (1 - pass_rate)``; on a board with continuous
    # scores it tracks the graded quality with no threshold cliff. It is the
    # scalar's only non-namespace term — see :func:`builtin_scalar` for why
    # pass is not a channel.
    pass_component = weights.pass_weight * (1.0 - mean_score)

    scalar_components: dict[str, float] = {"pass": pass_component}
    # SORTED, mirroring :func:`builtin_scalar`: float addition is not
    # associative, so summing the channels in mapping-iteration order would
    # make the scalar's last bit depend on hash seeding.
    for ns in sorted(namespace_aggregates):
        # Strip the trailing colon for human-readable component names.
        component_name = ns[:-1] if ns.endswith(":") else ns
        scalar_components[component_name] = namespace_aggregates[ns]

    # Parsimony / MDL term (OVERFITTING.md §5 / §12 #4), appended LAST and only
    # when opted in. The component value comes from the SAME seam
    # :func:`builtin_scalar` uses, so the surfaced component and the appended
    # scalar term can never disagree. ``None`` (default weight 0.0 / no diff
    # size) ⇒ the key is never written, so ``scalar_components`` and the scalar
    # are byte-identical to the pre-feature path.
    diff_component = diff_complexity_component(weights, diff_size)
    if diff_component is not None:
        scalar_components["diff_complexity"] = diff_component

    # Seam 2 (issue #19 phase 1): synthesise the scalar through the scoring
    # dispatcher. The built-in formula (drift + pass + non-drift namespaces)
    # is byte-identical to ``fsum(scalar_components.values())`` — the golden
    # test pins that — and the dispatcher returns it with a ``"builtin"``
    # provenance in this phase. ``scalar_components`` is still computed above
    # for the display / gate breakdown; the dispatcher owns the scalar value
    # the later phases (transforms / plugins) hook into.
    scalar, scalar_provenance = resolve_scalar(
        ScalarContext(
            pass_rate=pass_rate,
            mean_score=mean_score,
            drift_loss_mean=drift_loss_mean,
            namespace_aggregates=namespace_aggregates,
            per_judge_loss=_per_judge_loss_aggregate(losses),
            weights=weights,
            builtin_scalar=builtin_scalar(
                mean_score=mean_score,
                namespace_aggregates=namespace_aggregates,
                weights=weights,
                diff_size=diff_size,
            ),
            diff_size=diff_size,
        )
    )

    agg: dict[str, Any] = {
        "drift_loss_mean": drift_loss_mean,
        "pass_rate": pass_rate,
        # The uniform continuous outcome the scalar's pass component and
        # the gate's aggregate scope read. Equals pass_rate on an all-bool
        # board; kept alongside pass_rate so display and the per_entry gate
        # scope still have the binary view.
        "mean_score": mean_score,
        "expectation_count": expectation_count,
        "entry_count": entry_count,
        "scalar": scalar,
        "per_entry": per_entry,
        "namespace_aggregates": namespace_aggregates,
        "scalar_components": scalar_components,
        # Which scoring path produced ``scalar`` (issue #19). PHASE 1:
        # always ``"builtin"``; later phases enrich it. Additive — callers
        # that don't read it are unaffected.
        "scalar_provenance": scalar_provenance,
    }
    # Echo the candidate diff size onto the aggregate when EITHER half of the
    # diff-complexity regularizer is active — the loss-term weight (``> 0`` ⇒
    # ``diff_component is not None``) OR the opt-in parsimony CEILING
    # (``diff_complexity_ceiling > 0``, which the gate's Rule 0 reads off this
    # key). This lets the gate / dashboard surface
    # ``diff_size:challenger:{added,removed,patches}`` evidence and lets the
    # ceiling see the diff size even when the loss weight is off. At BOTH
    # defaults (weight 0.0 and ceiling 0.0), or with no diff size, the key is
    # ABSENT, so the returned dict — and therefore the serialised
    # ``gen_score.json`` golden — is byte-identical to the pre-feature
    # aggregate.
    if incomplete_entries:
        agg["incomplete_entries"] = incomplete_entries
    from zicato.core.measurement import UNKNOWN_SEED  # noqa: PLC0415

    seeds = {
        draw.base_seed if draw is not None else UNKNOWN_SEED
        for loss in losses
        for draw in (
            (loss.measurement,) if loss.measurement else loss.source_measurements or (None,)
        )
    }
    if len(seeds) == 1 and UNKNOWN_SEED not in seeds:
        agg["base_seed"] = next(iter(seeds))
    parsimony_active = diff_component is not None or weights.diff_complexity_ceiling > 0.0
    if parsimony_active and diff_size is not None:
        agg["diff_size"] = dict(diff_size)
    return agg


__all__ = [
    "GenerationScore",
    "ScoreMeasurement",
    "decode_gen_score",
    "read_gen_score",
    "read_gen_score_history",
    "write_gen_score",
    "aggregate_generation_score",
    "aggregate_namespaced_metrics",
    "entry_score",
    "per_run_drift_loss",
]


def _mean_over_present(values: list[float | None]) -> float | None:
    """Mean of the values that are present; ``None`` when none are.

    The "not measured is not zero" fold used for optional continuous
    fields (:attr:`LossProfile.score`, per-key
    :attr:`LossProfile.metrics`): a replicate that produced no value does
    not drag the mean toward zero, it simply does not vote. ``None`` is
    returned only when EVERY replicate abstained, so an entry with no
    expectation folds to ``None`` exactly as it did before replication.
    """
    present = [float(v) for v in values if v is not None]
    if not present:
        return None
    # ``math.fsum`` throughout the replicate folds: a folded value is the
    # score a round is decided on and a golden pins, so it must not depend
    # on the interpreter version or on replicate order.
    return math.fsum(present) / len(present)


def _mean_outcome(profiles: list[LossProfile]) -> float | None:
    """Fold the per-replicate CONTINUOUS OUTCOME across replicates.

    Means each replicate's :func:`~zicato.tournament.scoring.entry_score` —
    the single uniform mapping every scoring/gate consumer reads — rather
    than the raw :attr:`LossProfile.score` field. The distinction is the
    whole correctness of the fold, because ``score`` is unset in two
    materially different situations and only ONE of them is an abstention:

    * **No expectation** (``pass_fail is None`` too) — genuinely not
      measured. ``entry_score`` returns ``None``, the replicate abstains,
      and an entry with no expectation folds to ``None`` however many
      replicates it has.
    * **An expectation that could not fire** — the run was ABORTED (a spent
      wall-clock/token budget, an infra kill: see
      :func:`~zicato.tournament.worker_transport._aborted_loss_profile`,
      which records ``score=None`` with ``pass_fail=False``). That replicate
      observed a FAILURE rather than nothing. ``entry_score`` maps it to ``0.0``
      and it votes.

    Treating the second case as an abstention is how a K-replicate duel
    silently reverts to the single-replicate behaviour #108 removed: with
    one clean pass and one aborted replicate, a raw-``score`` mean reports
    the clean replicate's ``1.0`` verbatim while ``pass_fail``'s majority
    vote says ``False`` — a folded profile that contradicts itself, whose
    ``mean_score`` is a perfect ``1.0`` off a duel half of which never ran.

    Because the mapping is ``entry_score``'s, the fold satisfies
    ``entry_score(folded) == mean(entry_score(r) for r in replicates)``
    over the replicates that produced an outcome — including on an
    all-bool board, where each replicate contributes its ``1.0`` / ``0.0``
    bit and K replicates therefore move the outcome axis instead of being
    collapsed to ``pass_fail``'s single majority bit. The majority vote is
    still folded onto ``pass_fail`` itself, so ``pass_rate`` and every
    display consumer are unchanged.
    """
    return _mean_over_present([entry_score(p) for p in profiles])


def _mean_metrics(profiles: list[LossProfile]) -> dict[str, float] | None:
    """Fold the per-entry ``metrics`` decomposition across replicates.

    Each key is meaned over the replicates that REPORT it (the
    "not measured is not zero" model of :func:`_mean_over_present`) —
    a scorer that emitted ``precision`` on three of four replicates
    reports the mean of those three. Returns ``None`` when no replicate
    carried a decomposition, so a board whose scorers expose none folds
    byte-identically to the pre-replication path.

    This exists so the folded decomposition actually decomposes the
    folded :attr:`LossProfile.score` beside it. Carrying replicate 0's
    ``metrics`` next to an averaged ``score`` would be the one option
    that is actively misleading.
    """
    keys: list[str] = []
    seen: set[str] = set()
    for p in profiles:
        for key in p.metrics or {}:
            if key not in seen:
                seen.add(key)
                keys.append(key)
    if not keys:
        return None
    folded: dict[str, float] = {}
    for key in keys:
        mean = _mean_over_present([(p.metrics or {}).get(key) for p in profiles])
        if mean is not None:
            folded[key] = mean
    return folded


def _mean_metric_counts(profiles: list[LossProfile]) -> tuple[MetricCount, ...]:
    """Fold the namespaced ``metric_counts`` view across replicates.

    Each ``(name, severity)`` bucket is meaned over ALL replicates, with
    an absent bucket contributing ``0.0``. That divisor is deliberate: it
    is exactly the per-run-mean model
    :func:`~zicato.tournament.scoring.aggregate_namespaced_metrics` uses
    ("a loss with none contributes zero"), so the namespace aggregate
    computed over the folded profiles equals the aggregate computed over
    every replicate run individually. Using a present-only divisor here
    would inflate a sparse namespace by the number of replicates that
    never saw it.

    Bucket ORDER is the first-seen order across replicates, so the fold
    is deterministic and replicate 0's ordering is preserved for the
    buckets it carried.

    Scope of that equality: it holds when the replicates agree on which
    :meth:`LossProfile.unified_metrics` BRANCH they take — in production
    they do, because the reducer populates ``metric_counts`` on every
    profile it writes. A set MIXING an explicit-``metric_counts`` replicate
    with one carrying only the int scalars is aggregate-preserving only
    approximately: the fold's non-empty ``metric_counts`` makes the folded
    profile take the explicit branch, so the scalar-only replicate's
    synthesised contribution is dropped from the fold's view. Only a
    hand-built profile, or one written before ``metric_counts`` existed, can
    reach that, and the residual is bounded by those replicates' share of
    the namespace.
    """
    keys: list[tuple[str, MetricSeverity]] = []
    seen: set[tuple[str, MetricSeverity]] = set()
    for p in profiles:
        for mc in p.metric_counts:
            key = (mc.name, mc.severity)
            if key not in seen:
                seen.add(key)
                keys.append(key)
    if not keys:
        return ()
    n = len(profiles)
    folded: list[MetricCount] = []
    for name, severity in keys:
        total = 0.0
        for p in profiles:
            for mc in p.metric_counts:
                if mc.name == name and mc.severity == severity:
                    total += float(mc.count)
        folded.append(MetricCount(name=name, severity=severity, count=total / n))
    return tuple(folded)


def _mean_per_judge_loss(profiles: list[LossProfile]) -> tuple[JudgeLoss, ...]:
    """Fold the per-judge loss attribution across replicates.

    ``raw_loss`` / ``weighted_loss`` are meaned over ALL replicates with
    an absent judge contributing zero — the same divisor
    :func:`~zicato.tournament.scoring._per_judge_loss_aggregate` applies
    ("a judge absent from a run contributes zero to its sum"), so the
    per-judge aggregate carried onto
    :class:`~zicato.scoring.api.ScalarContext` is the same whether it is
    taken over the folded profiles or over every replicate run. ``weight``
    is the contract's per-judge multiplier — constant across replicates of
    one epoch — so the first replicate that reports the judge supplies it.
    """
    order: list[str] = []
    weights: dict[str, float] = {}
    for p in profiles:
        for jl in p.per_judge_loss:
            if jl.judge_name not in weights:
                order.append(jl.judge_name)
                weights[jl.judge_name] = jl.weight
    if not order:
        return ()
    n = len(profiles)
    folded: list[JudgeLoss] = []
    for name in order:
        raw_total = 0.0
        weighted_total = 0.0
        for p in profiles:
            for jl in p.per_judge_loss:
                if jl.judge_name == name:
                    raw_total += float(jl.raw_loss)
                    weighted_total += float(jl.weighted_loss)
        folded.append(
            JudgeLoss(
                judge_name=name,
                raw_loss=raw_total / n,
                weight=weights[name],
                weighted_loss=weighted_total / n,
            )
        )
    return tuple(folded)


def _sum_judge_errors(profiles: list[LossProfile]) -> tuple[JudgeError, ...]:
    """Fold per-judge call-failure provenance across replicates by SUMMING.

    Deliberately not a mean, unlike every other fold here. ``invocations``
    and ``errors`` are event COUNTS of a thing that either happened or did
    not, and the question the fold has to keep answerable is the operator's:
    "did this judge ever fail to answer, and how often?". Meaning them would
    divide a real failure by the replicate count — three of four replicates
    clean and one that raised 34 times reports "8.5 errors", a number that
    describes no run — and, worse, it would shrink toward zero as K grows,
    so the more evidence a duel gathers the less a broken judge looks broken.
    The sum is the honest total across the duel, and
    :func:`~zicato.health.diagnostics.detect_dead_judge` re-aggregates over
    every profile it is handed anyway, so both the folded and the unfolded
    view lead to the same finding.

    ``last_error_type`` comes from the LAST replicate reporting the judge —
    a per-judge scalar rather than a count; the most recent failure is the one an
    operator would check first. Judge ORDER is first-seen across replicates.
    Empty when no replicate recorded a failure, which is every healthy duel.
    """
    order: list[str] = []
    totals: dict[str, list[int]] = {}
    last_types: dict[str, str] = {}
    for p in profiles:
        for je in p.judge_errors:
            if je.judge_name not in totals:
                order.append(je.judge_name)
                totals[je.judge_name] = [0, 0]
            totals[je.judge_name][0] += int(je.invocations)
            totals[je.judge_name][1] += int(je.errors)
            if je.last_error_type:
                last_types[je.judge_name] = je.last_error_type
    return tuple(
        JudgeError(
            judge_name=name,
            invocations=totals[name][0],
            errors=totals[name][1],
            last_error_type=last_types.get(name, ""),
        )
        for name in order
    )


def average_replicate_losses(
    runs: list[dict[str, LossProfile]],
) -> dict[str, LossProfile]:
    """Fold N replicate runs of a board into one per-entry loss map.

    This is the replication primitive: :attr:`ScoringWeights` never sees
    the individual replicates, so EVERY scalar-bearing field must be
    aggregated here or the replicates buy nothing. The rule this function
    holds to is: **a field the scalar or the gate reads is aggregated; a
    field neither reads carries the representative replicate (replicate
    ``0``) and is named below with the reason it may.**

    Aggregated
    ----------
    ``drift_loss``
        Mean across replicates. Reaches the scalar as the ``"drift"``
        component (``namespace_weights["drift:"] × drift_loss_mean``).
    ``task_failure_ratio``
        Mean across replicates. It is the ``failure:tasks`` channel member,
        so replicating a unit averages how badly its tasks failed.
    ``not_completed``
        ORed across replicates: a unit that could not be completed even
        ONCE did not complete. This is deliberately not a mean or a
        majority — the field is a bool, and the contract property the
        ``failure:`` channel exists to hold is that crashing is never free.
        A mean would let a crash be diluted by replication (and shrink
        toward zero as K grows), and a majority would make a crash in half
        the replicates cost nothing at all, so a challenger that crashes
        intermittently would out-score one that runs. The cost is that a
        single flaky infra abort charges the full not-completed magnitude
        for the whole duel; that is the intended direction of the error.
    ``runtime_ms``
        Rounded mean across replicates — it is the ``runtime:seconds``
        channel member (default coefficient ``0.0``, so most contracts do
        not score it, but one that does must see the duel's duration rather
        than the first replicate's). The field is milliseconds by contract,
        hence the rounding.
    ``score``
        Mean of each replicate's RESOLVED OUTCOME
        (:func:`_mean_outcome` — ``entry_score`` rather than the raw field), so a
        replicate whose expectation was recorded as failed WITHOUT a score
        (an aborted run) votes its ``0.0`` instead of abstaining. ``None``
        only when no replicate produced an outcome at all, so a board with
        no expectations is unchanged. This is the field
        :func:`~zicato.tournament.scoring.entry_score` reads FIRST, hence
        the continuous outcome axis the duel actually turns on.
    ``metrics``
        Per-key mean over the replicates reporting the key
        (:func:`_mean_metrics`) — the decomposition has to decompose the
        folded ``score`` sitting next to it.
    ``metric_counts``, ``tokens_spent``, ``output_chars``, ``schema_failures``
        Namespace-bearing: they reach the scalar through
        :func:`~zicato.tournament.scoring.aggregate_namespaced_metrics`,
        whose per-namespace values are appended to ``scalar_components``
        and summed into the scalar for any contract with a non-zero
        ``cost:`` / ``output:`` / ``schema:`` weight. ``metric_counts`` is
        the one that matters in production — the reducer always populates
        it, and :meth:`LossProfile.unified_metrics` then reads it in
        preference to synthesising from the three scalars — so it is
        meaned exactly (:func:`_mean_metric_counts`). The three int-typed
        scalars carry the ROUNDED mean: the fields are integer counts by
        contract, and they are consulted only on the synthesised path
        (a profile with no ``metric_counts``) and by display. That rounding
        is the ONE place the reducer's "scalar and its MetricCount mirror
        agree" invariant relaxes across the fold — a folded
        ``cost:tokens_spent`` of ``100.5`` sits beside ``tokens_spent=100``.
        The mirror is what the scalar reads, so the scalar is exact and the
        disagreement is display-only and sub-unit. Note ``round`` is
        banker's rounding, so a mean of exactly ``0.5`` floors to ``0`` and
        ``unified_metrics``' truthiness check then omits the synthesised
        bucket entirely — reachable only on the synthesised path.
    ``per_judge_loss``
        Meaned per judge (:func:`_mean_per_judge_loss`); it is carried onto
        :class:`~zicato.scoring.api.ScalarContext`, so a scalar PLUGIN can
        read it.
    ``judge_errors``
        SUMMED per judge (:func:`_sum_judge_errors`), the one field here that
        is deliberately not meaned — see that function for why a mean would
        make a broken judge look less broken the more replicates a duel runs.
        It is not scalar-bearing (a failed judge call contributes no drift,
        which is exactly the defect it records); it is aggregated anyway
        because the operator-facing finding it feeds must survive the fold.
    ``pass_fail``
        Strict-majority vote (``None`` preserved when the entry has no
        expectation). NOTE: now that ``score`` is folded, this vote no
        longer decides the scalar — :func:`entry_score` returns the folded
        continuous outcome before it can consult ``pass_fail``. The vote
        still drives the binary ``pass_rate`` and the gate's ``pass_fail``
        fallback for score-less aggregates, so it stays a majority rather
        than a mean. It can therefore legitimately disagree in sign with
        the folded ``score`` (2 of 5 replicates passing is ``pass_fail``
        ``False`` and ``score`` ``0.4``); that is the binary and continuous
        views of the same duel rather than an inconsistency.

    Replicate-0 pass-through, and why each may be
    ---------------------------------------------
    ``run_id``, ``expectation_result``
        Raw provenance of the representative replicate, deliberately NOT
        synthesised: the fold is not a run and has no matcher verdict of
        its own. The AGGREGATED outcome lives in the first-class ``score``
        / ``metrics`` / ``pass_fail`` fields, which are the ones scoring
        and the gate read; ``expectation_result`` stays the untouched raw
        evidence from one replicate.
    ``drift_counts``
        The per-``(kind, severity)`` buckets are NOT scalar-bearing: the
        ``"drift:"`` namespace is explicitly excluded from
        :func:`aggregate_namespaced_metrics` precisely because
        ``drift_loss`` — which IS meaned above — owns the drift axis. The
        buckets are int-typed attribution/display, and the folded
        ``metric_counts`` already carries their meaned ``"drift:"`` mirror.
    ``entry_id``, ``generation_id``, ``epoch_id``, ``match_id``
        Invariant across the replicates of one unit by construction.
    ``plan_revisions``,
    ``turns_completed``, ``memory_failure_count``, ``context_loss_count``,
    ``adk_session_id``, ``cached`` / ``source_epoch`` / ``source_run``,
    ``scoring_provenance``, ``wall_clock_budget_exceeded``, ``abort_cause``,
    ``not_completed_reason``, ``started_at`` / ``ended_at``
        Neither the scalar nor the gate reads them. They describe ONE
        execution (its wall-clock span, its abort and why, which cache slot
        it came from) and have no meaningful fold, so they
        report the representative replicate. A folded span in particular
        would be a fiction: N replicates are N disjoint spans, and a reader
        wanting the true extent reads the per-replicate ``loss.r{n}.json``
        files the fold left untouched. Consumers that count per-round infra
        aborts across a duel therefore see replicate 0's provenance only —
        see the follow-up note on ``_count_infra_aborted_runs``.

    ``dataclasses.replace`` keeps the profile shape intact, so a field
    added to :class:`LossProfile` later defaults to pass-through and this
    docstring is the place to justify it.
    """
    from dataclasses import replace as _replace  # noqa: PLC0415

    if not runs:
        return {}
    entry_ids = list(runs[0].keys())
    out: dict[str, LossProfile] = {}
    for entry_id in entry_ids:
        profiles = [r[entry_id] for r in runs if entry_id in r]
        if not profiles:
            continue
        n = len(profiles)
        mean_drift = math.fsum(float(p.drift_loss) for p in profiles) / n
        pass_votes = [p.pass_fail for p in profiles if p.pass_fail is not None]
        if pass_votes:
            true_count = sum(1 for v in pass_votes if v)
            majority_pass: bool | None = true_count * 2 > len(pass_votes)
        else:
            majority_pass = None
        out[entry_id] = _replace(
            profiles[0],
            measurement=profiles[0].measurement if n == 1 else None,
            source_measurements=tuple(
                draw
                for profile in profiles
                for draw in (
                    (profile.measurement,)
                    if profile.measurement
                    else profile.source_measurements or (None,)
                )
            )
            if n > 1
            else profiles[0].source_measurements,
            execution_started=(
                False
                if any(p.execution_started is False for p in profiles)
                else profiles[0].execution_started
            ),
            drift_loss=mean_drift,
            task_failure_ratio=math.fsum(float(p.task_failure_ratio) for p in profiles) / n,
            not_completed=any(p.not_completed for p in profiles),
            runtime_ms=round(sum(p.runtime_ms for p in profiles) / n),
            pass_fail=majority_pass,
            score=_mean_outcome(profiles),
            metrics=_mean_metrics(profiles),
            metric_counts=_mean_metric_counts(profiles),
            tokens_spent=round(sum(p.tokens_spent for p in profiles) / n),
            output_chars=round(sum(p.output_chars for p in profiles) / n),
            schema_failures=round(sum(p.schema_failures for p in profiles) / n),
            per_judge_loss=_mean_per_judge_loss(profiles),
            judge_errors=_sum_judge_errors(profiles),
        )
    return out


def fold_matchup_replicates(
    runs: list[tuple[dict[str, LossProfile], dict[str, LossProfile]]],
) -> tuple[dict[str, LossProfile], dict[str, LossProfile]]:
    """Fold a matchup's per-slot loss maps into one pair of maps.

    ``runs`` is SLOT-major — replicate 0 first — because
    :func:`~zicato.tournament.scoring.average_replicate_losses` carries the
    fields it cannot fold from the first map it is given, and that
    representative has to be replicate 0 rather than whichever slot
    happened to settle first. A single slot returns its maps unfolded.
    """
    if len(runs) == 1:
        return runs[0][0], runs[0][1]
    return average_replicate_losses([r[0] for r in runs]), average_replicate_losses(
        [r[1] for r in runs]
    )
