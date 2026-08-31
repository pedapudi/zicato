"""trace_view — the trajectory-bootstrap UI read surface.

The read side of the trajectory-bootstrap visualisation (TRAJECTORY-UI.md): the
imported foreign traces (TRAJECTORY-BOOTSTRAP.md §3) and the drafted suggestions
(§5) rendered as the three surfaces the two sibling view-agents build — the
Traces viewer, the suggestion provenance chain, and the Evals ghost rows. Three
server-derived readers:

* :func:`build_trace_list` — the trace list: per-trace summary + the
  PRE-COMPUTED strip-model (the render model the JS draws straight from).
* :func:`build_trace_detail` — one trace: its strip-model + the reconstructed
  conversation (the transcript turn vocabulary) + episode spans with anchors +
  the linked suggestion ids.
* :func:`build_suggestion_provenance` — one suggestion's chain: suggestion →
  episodes → per-episode trace-segment strip-models + the admission-stat
  visuals (the BT-whisker / pip vocabulary).

Every reader is best-effort and honest (EVAL-VIEW.md §3): an
unknown / cold reflection, an unknown trace / suggestion, or a malformed record
degrades to a SAME-SHAPE payload (``found: False`` / empties, never a raise,
never a fabricated number).

**Dashboard-free by construction (the import contract).** ``zicato.query`` may
not reach the dashboard driver, and ``reflection.mining`` transitively imports
``dashboard.transcript`` (via the adjudicator). So the readers do NOT re-run the
miner; they derive the episode overlays from the **persisted suggestions**
(``reflection.suggestions`` — dashboard-free) whose provenance already carries
``source_episodes`` (the episode ids), ``source_refs = [source_file,
signal_kind]``, and the ``foreign_source`` block (TRAJECTORY-BOOTSTRAP.md §5.3).
The trace figure itself (lane / signals / budget) reads the reduced
:class:`~zicato.telemetry.dialects.DialectSignals` off the persisted
``ImportedTrace`` (``reflection.trace_import`` — dashboard-free). This reads the
REAL pipeline output, adds no engine coupling, and keeps the query layer clean.

The strip-model computation (:func:`build_strip_model`) and its pure helpers
(:func:`lane_marks`, :func:`budget_fill`, :func:`signal_ticks`) do NO I/O and
are unit-tested against known answers; every position/size is a normalized
``[0, 1]`` float rounded to 4 decimals so the payloads are byte-stable.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from zicato.query.paths import WorkspacePaths
    from zicato.reflection.suggestions import Suggestion
    from zicato.reflection.trace_import import ImportedTrace

# --- the ADK drift kinds the strip reads (TELEMETRY-DIALECTS.md §3.2) --------
_ADK_ERROR_KIND: str = "tool_error"
_ADK_RETRY_KIND: str = "looping_tool_call"
_ADK_TRANSFER_KIND: str = "agent_transfer"

# --- signal-kind tokens (mirror mining._SIG_*, carried in source_refs[1]) ----
_SIG_ERROR_CASCADE: str = "error_cascade"
_SIG_ABORT_PATTERN: str = "abort_pattern"
_SIG_RETRY_LOOP: str = "retry_loop"
_SIG_BUDGET_BLOWOUT: str = "budget_blowout"
_SIG_TRANSFER_CHURN: str = "transfer_churn"
_SIG_BEHAVIORAL: str = "behavioral"

#: Fixed kind order so the signal row + episode brackets are deterministic
#: (TRAJECTORY-UI.md §3.4). Behavioral is last (it brackets the whole lane).
_SIGNAL_ORDER: tuple[str, ...] = (
    _SIG_ERROR_CASCADE,
    _SIG_ABORT_PATTERN,
    _SIG_RETRY_LOOP,
    _SIG_BUDGET_BLOWOUT,
    _SIG_TRANSFER_CHURN,
    _SIG_BEHAVIORAL,
)

#: signal_kind → (tone, glyph) — the ONE source, no new colour vocabulary
#: (TRAJECTORY-UI.md §3.5). Tones map to the design-language §2 role tokens.
_TONE_GLYPH: dict[str, tuple[str, str]] = {
    _SIG_ERROR_CASCADE: ("bad", "✕"),
    _SIG_ABORT_PATTERN: ("bad", "✕"),
    _SIG_RETRY_LOOP: ("caution", "↻"),
    _SIG_BUDGET_BLOWOUT: ("caution", "⏱"),
    _SIG_TRANSFER_CHURN: ("neutral", "⇄"),
    _SIG_BEHAVIORAL: ("neutral", "○"),
}

#: The cost ceilings the budget-ground fraction is measured against. Mirrored
#: from ``mining.MAX_TOKENS`` / ``mining.MAX_LLM_CALLS`` — NOT imported, because
#: ``mining`` transitively pulls the dashboard driver and the query layer must
#: stay dashboard-free (module docstring). Kept in lockstep with the miner.
_MAX_TOKENS: int = 100_000
_MAX_LLM_CALLS: int = 50

#: The advisory flip-rate ceiling the flip-whisker reference rule sits at
#: (EVAL-SYNTHESIS.md §5 ``RECOMMENDED_FLIP_CEILING``).
_FLIP_CEILING: float = 0.25

#: The PER-MARK extent cap on the turn lane (TRAJECTORY-UI.md §3.4): no single
#: turn mark may occupy more than a quarter of the lane's width, whatever the
#: turn count. Exported rather than private because it is part of the strip-model
#: contract the figure + its geometry tests assert against. See
#: :func:`lane_marks` for the compressive scale this caps.
LANE_EXTENT_CAP: float = 0.25


def _r4(value: float) -> float:
    """Round a normalized quantity to 4 decimals — byte-stable payloads."""
    return round(float(value), 4)


def _tone_glyph(signal_kind: str) -> tuple[str, str]:
    return _TONE_GLYPH.get(signal_kind, ("neutral", "○"))


# ---------------------------------------------------------------------------
# Signal counts (off the reduced DialectSignals)
# ---------------------------------------------------------------------------


def _drift_count(signals: Any, kind: str) -> int:
    return sum(int(dc.count) for dc in signals.drift_counts if dc.kind == kind)


def _signal_counts(trace: ImportedTrace) -> dict[str, int]:
    s = trace.signals
    return {
        "tool_errors": _drift_count(s, _ADK_ERROR_KIND),
        "task_started": int(getattr(s, "task_started", 0)),
        "task_failed": int(getattr(s, "task_failed", 0)),
        "retry_loops": _drift_count(s, _ADK_RETRY_KIND),
        "transfers": _drift_count(s, _ADK_TRANSFER_KIND),
        "llm_calls": int(getattr(s, "llm_call_count", 0)),
        "tokens": int(getattr(s, "token_count", 0)),
    }


def _raw_signal_count(trace: ImportedTrace, signal_kind: str) -> int:
    """The magnitude a signal tick carries — read off the reduced signals."""
    counts = _signal_counts(trace)
    if signal_kind == _SIG_ERROR_CASCADE:
        return counts["tool_errors"] + counts["task_failed"]
    if signal_kind == _SIG_ABORT_PATTERN:
        return counts["task_failed"]
    if signal_kind == _SIG_RETRY_LOOP:
        return counts["retry_loops"]
    if signal_kind == _SIG_BUDGET_BLOWOUT:
        return max(counts["llm_calls"], counts["tokens"] // 1000)
    if signal_kind == _SIG_TRANSFER_CHURN:
        return counts["transfers"]
    return 0


def _signal_label(trace: ImportedTrace, signal_kind: str) -> str:
    """A compact tick label from the reduced-signal counts (§3.4)."""
    c = _signal_counts(trace)
    if signal_kind == _SIG_ERROR_CASCADE:
        return f"{c['tool_errors']} tool err · {c['task_failed']}/{c['task_started']} failed"
    if signal_kind == _SIG_ABORT_PATTERN:
        return f"{c['task_failed']}/{c['task_started']} failed"
    if signal_kind == _SIG_RETRY_LOOP:
        return f"{c['retry_loops']} retry loop"
    if signal_kind == _SIG_BUDGET_BLOWOUT:
        return f"{c['llm_calls']} calls / {c['tokens']} tok"
    if signal_kind == _SIG_TRANSFER_CHURN:
        return f"{c['transfers']} transfers"
    return signal_kind


# ---------------------------------------------------------------------------
# Pure strip-model computation (TRAJECTORY-UI.md §3.4) — no I/O, deterministic
# ---------------------------------------------------------------------------


def lane_marks(user_turns: list[str], agent_turns: list[str]) -> list[dict[str, Any]]:
    """Alternating user/agent lane marks on the COMPRESSIVE extent scale (§3.4).

    Zips the two ordered sides into ``[u0, a0, u1, a1, …]`` (a trailing
    unmatched turn is appended) and lays them end-to-end from ``x0 = 0``.

    THE EXTENT SCALE (load-bearing). A raw ``chars / total`` share forces the
    marks to TILE the lane no matter how few turns there are, so a 2-turn trace
    renders as two half-lane slabs that fuse into one solid block: two
    full-height ink rectangles spanning the whole strip. The extent is instead
    **exactly proportional to
    ``sqrt(chars + 1)``** under ONE global scale:

    * ``sqrt`` COMPRESSES honestly and monotonically — a 4096-char answer is 8×
      a 64-char prompt's width rather than 64×, so a long agent turn does not
      swamp the terse turns around it while the ordering stays truthful;
    * the global scale is ``min(EXTENT_CAP / max(w), 1 / sum(w))`` — the first
      term is the **per-mark cap** (the widest mark is at most
      ``LANE_EXTENT_CAP`` of the lane), the second the **saturation fit** (the
      marks together never exceed the lane). Because it is a single scalar
      multiplier, the extent RATIOS are preserved exactly — nothing is flattened
      or redistributed.

    Consequence, by design: a short 2-turn trace reads as two proportioned bars
    over a mostly-empty lane. The lane measures CAPACITY rather than dividing a
    fixed total, so the empty room is itself the honest signal that the trace
    has two turns. A many-turn
    trace saturates and tiles the lane exactly. The vertical extent is left to
    the figure: ``size`` = ``chars / max_chars`` and ``svg.js`` maps it onto a
    BOUNDED bar (≤ 40 % of the lane height), never a full-lane slab.

    A zero-char trace degrades to even spacing (all weights equal) and to
    ``size`` 0.0 — no text, no height claim.
    """
    seq: list[tuple[str, str]] = []
    for i in range(max(len(user_turns), len(agent_turns))):
        if i < len(user_turns):
            seq.append(("user", user_turns[i]))
        if i < len(agent_turns):
            seq.append(("agent", agent_turns[i]))
    if not seq:
        return []
    chars = [len(text) for _role, text in seq]
    max_chars = max(chars)
    # the compressive weights (+1 so an empty turn still carries a weight, and a
    # zero-char trace degrades to equal weights ⇒ even spacing).
    weights = [math.sqrt(c + 1) for c in chars]
    total_w = sum(weights)
    # ONE global scale: cap the widest mark, and fit the whole run in the lane.
    cap_scale = LANE_EXTENT_CAP / max(weights)
    fit_scale = 1.0 / total_w
    scale = min(cap_scale, fit_scale)
    saturated = fit_scale <= cap_scale  # the lane tiles exactly
    marks: list[dict[str, Any]] = []
    cursor = 0.0
    for i, ((role, _text), c) in enumerate(zip(seq, chars, strict=True)):
        x0 = cursor
        x1 = cursor + weights[i] * scale
        cursor = x1
        # A text-free lane makes NO height claim: ``size`` 0.0 ⇒ the figure's
        # minimum hairline. It must never be 1.0 — that would draw the tallest
        # possible bar for the least informative input (a dialect reader that
        # extracted no turn bodies), and a saturated all-empty lane of maximum
        # bars is the densest thing this figure can still paint.
        size = (c / max_chars) if max_chars > 0 else 0.0
        marks.append(
            {
                "i": i,
                "role": role,
                "x0": _r4(x0),
                "x1": _r4(min(1.0, x1)),
                "size": _r4(min(1.0, size)),
                "chars": c,
            }
        )
    # Pin the final right edge to 1.0 ONLY when the lane does saturate
    # (float drift over many turns); an under-filled lane must stay under-filled.
    if saturated:
        marks[-1]["x1"] = 1.0
    return marks


def signal_ticks(present: list[tuple[str, int, str]]) -> list[dict[str, Any]]:
    """Aggregate adverse-signal ticks, evenly distributed (§1.1 — NOT positioned).

    ``present`` is ``[(signal_kind, count, label), …]`` already in the fixed
    kind order. Each tick rides its tone + glyph (§3.5); ``x`` is the even
    ``(k+1)/(n+1)`` slot and ``positioned`` is ``False`` — the honesty flag,
    because the reduced signals carry counts rather than per-event positions.
    """
    n = len(present)
    ticks: list[dict[str, Any]] = []
    for k, (kind, count, label) in enumerate(present):
        tone, glyph = _tone_glyph(kind)
        ticks.append(
            {
                "kind": kind,
                "tone": tone,
                "glyph": glyph,
                "count": int(count),
                "label": label,
                "x": _r4((k + 1) / (n + 1)),
                "positioned": False,
            }
        )
    return ticks


def budget_fill(tokens: int, llm_calls: int, *, max_tokens: int, max_calls: int) -> dict[str, Any]:
    """The shaded budget-ground model (§3.4).

    ``fill`` = the fraction of the cost ceiling reached (clamped to 1.0);
    ``over`` = a ceiling was crossed; ``shaded`` = any budget was spent.
    """
    tok_frac = (tokens / max_tokens) if max_tokens > 0 else 0.0
    call_frac = (llm_calls / max_calls) if max_calls > 0 else 0.0
    fill = min(1.0, max(tok_frac, call_frac))
    over = (max_tokens > 0 and tokens >= max_tokens) or (max_calls > 0 and llm_calls >= max_calls)
    tok_label = f"{tokens // 1000}k tok" if tokens >= 1000 else f"{tokens} tok"
    return {
        "shaded": fill > 0.0,
        "fill": _r4(fill),
        "over": bool(over),
        "tokens": int(tokens),
        "llm_calls": int(llm_calls),
        "label": f"{llm_calls} calls · {tok_label}",
    }


def _episode_span(signal_kind: str, ticks: list[dict[str, Any]]) -> dict[str, Any]:
    """The bracketed span for one episode over the strip (§3.4).

    A signal episode anchors to its matching signal tick (``x ± 0.05``, clamped);
    a behavioral episode brackets the whole lane (``0 → 1``, ``anchor:lane``).
    """
    if signal_kind == _SIG_BEHAVIORAL:
        return {"x0": 0.0, "x1": 1.0, "anchor": "lane"}
    tick_x = next((t["x"] for t in ticks if t["kind"] == signal_kind), 0.5)
    return {
        "x0": _r4(max(0.0, tick_x - 0.05)),
        "x1": _r4(min(1.0, tick_x + 0.05)),
        "anchor": "signal",
    }


def build_strip_model(
    trace: ImportedTrace,
    episodes: list[dict[str, Any]],
    *,
    focus_episode_id: str | None = None,
) -> dict[str, Any]:
    """The PRE-COMPUTED strip render model for one trace (TRAJECTORY-UI.md §3.4).

    Pure — the ONE place the render math lives; the JS draws from this and
    derives nothing. ``episodes`` are this trace's drafted episodes (derived from
    the persisted suggestions, each an :func:`_episode_dict`); ``focus_episode_id``
    is set only for a provenance mini-strip.
    """
    signals = trace.signals

    # The signal ticks: one per DRAFTED signal_kind present on this trace, in the
    # fixed kind order. Count/label read off the reduced signals (the magnitude).
    drafted_kinds = {str(e["signal_kind"]) for e in episodes}
    present: list[tuple[str, int, str]] = [
        (kind, _raw_signal_count(trace, kind), _signal_label(trace, kind))
        for kind in _SIGNAL_ORDER
        if kind != _SIG_BEHAVIORAL and kind in drafted_kinds
    ]
    ticks = signal_ticks(present)
    marks = lane_marks(list(trace.user_turns), list(trace.agent_turns))
    budget = budget_fill(
        int(getattr(signals, "token_count", 0)),
        int(getattr(signals, "llm_call_count", 0)),
        max_tokens=_MAX_TOKENS,
        max_calls=_MAX_LLM_CALLS,
    )

    ep_models: list[dict[str, Any]] = []
    for ep in episodes:
        signal_kind = str(ep["signal_kind"])
        tone, glyph = _tone_glyph(signal_kind)
        span = _episode_span(signal_kind, ticks)
        ep_models.append(
            {
                "episode_id": ep["episode_id"],
                "kind": ep["episode_type"],
                "signal_kind": signal_kind,
                "tone": tone,
                "glyph": glyph,
                "x0": span["x0"],
                "x1": span["x1"],
                "anchor": span["anchor"],
                "severity_rank": int(ep["severity_rank"]),
                "suggestion_ids": list(ep["suggestion_ids"]),
            }
        )

    return {
        "trace_id": trace.trace_id,
        "dialect": trace.dialect,
        "lane": {"turn_count": len(marks), "marks": marks},
        "signals": ticks,
        "budget": budget,
        "episodes": ep_models,
        "focus_episode_id": focus_episode_id,
    }


# ---------------------------------------------------------------------------
# Episodes derived from the persisted suggestions (dashboard-free)
# ---------------------------------------------------------------------------


def _episode_dict(sug: Suggestion) -> dict[str, Any] | None:
    """One drafted episode derived from a bootstrap suggestion, or ``None``.

    Reads the suggestion's provenance (TRAJECTORY-BOOTSTRAP.md §5.3): the episode
    id (``source_episodes[0]``), the ``signal_kind`` (``source_refs[1]``), the
    trace it came from (``foreign_source``), and the suggestion's own severity /
    summary. Returns ``None`` for a non-bootstrap suggestion (no foreign source).
    """
    prov = sug.provenance or {}
    foreign = prov.get("foreign_source")
    if not isinstance(foreign, dict):
        return None
    eps = prov.get("source_episodes") or []
    refs = prov.get("source_refs") or []
    signal_kind = str(refs[1]) if len(refs) >= 2 else ""
    episode_type = "imported_behavioral" if signal_kind == _SIG_BEHAVIORAL else "imported_signal"
    return {
        "episode_id": str(eps[0]) if eps else "",
        "signal_kind": signal_kind,
        "episode_type": episode_type,
        "severity_rank": int(sug.severity_rank),
        "summary": sug.summary,
        "suggestion_ids": [sug.suggestion_id],
        "trace_id": str(foreign.get("trace_id", "")),
        "source_file": str(foreign.get("source_file", "")),
    }


def _episodes_by_trace(suggestions: list[Suggestion]) -> dict[str, list[dict[str, Any]]]:
    """``{trace_id: [episode_dict, …]}`` — deduped on episode id, suggestion ids folded.

    Two suggestions citing the same episode (rare) fold into one episode whose
    ``suggestion_ids`` unions both. Episodes are ordered by the fixed signal
    order so the overlays are deterministic.
    """
    by_trace: dict[str, dict[str, dict[str, Any]]] = {}
    for sug in suggestions:
        ep = _episode_dict(sug)
        if ep is None or not ep["episode_id"]:
            continue
        bucket = by_trace.setdefault(ep["trace_id"], {})
        existing = bucket.get(ep["episode_id"])
        if existing is None:
            bucket[ep["episode_id"]] = ep
        else:
            for sid in ep["suggestion_ids"]:
                if sid not in existing["suggestion_ids"]:
                    existing["suggestion_ids"].append(sid)

    order = {kind: i for i, kind in enumerate(_SIGNAL_ORDER)}
    out: dict[str, list[dict[str, Any]]] = {}
    for trace_id, bucket in by_trace.items():
        eps = list(bucket.values())
        eps.sort(key=lambda e: (order.get(str(e["signal_kind"]), 99), e["episode_id"]))
        out[trace_id] = eps
    return out


# ---------------------------------------------------------------------------
# Shared reflection resolution (the one place that touches disk)
# ---------------------------------------------------------------------------


def _resolve(paths: WorkspacePaths, reflection_id: str) -> tuple[str | None, list[Any], list[Any]]:
    """``(epoch_id, traces, suggestions)`` for a reflection — tolerant, dashboard-free.

    Resolves the owning epoch (index-first, tree-walk fallback), reads the
    persisted imported traces + the persisted suggestions. A cold / unknown
    reflection yields ``(None, [], [])``.
    """
    from zicato.query.reflection_view import _resolve_epoch  # noqa: PLC0415
    from zicato.reflection.suggestions import read_suggestions  # noqa: PLC0415
    from zicato.reflection.trace_import import read_imported_traces  # noqa: PLC0415

    try:
        epoch_id = _resolve_epoch(paths, reflection_id)
    except Exception:  # noqa: BLE001 — best-effort resolution
        epoch_id = None
    if not epoch_id:
        return None, [], []
    traces = read_imported_traces(paths.root, epoch_id, reflection_id)
    suggestions = read_suggestions(paths.root, epoch_id, reflection_id)
    return epoch_id, traces, suggestions


# ---------------------------------------------------------------------------
# Reader 1 — build_trace_list (TRAJECTORY-UI.md §3.1)
# ---------------------------------------------------------------------------


def _empty_list(reflection_id: str) -> dict[str, Any]:
    return {
        "reflection_id": reflection_id,
        "epoch_id": None,
        "found": False,
        "trace_count": 0,
        "traces": [],
    }


def build_trace_list(paths: WorkspacePaths, reflection_id: str) -> dict[str, Any]:
    """The trace list: per-trace summary + the pre-computed strip-model (§3.1)."""
    epoch_id, traces, suggestions = _resolve(paths, reflection_id)
    if epoch_id is None or not traces:
        empty = _empty_list(reflection_id)
        if epoch_id is not None:
            empty["epoch_id"] = epoch_id
            empty["found"] = True  # a resolved-but-trace-less reflection is honest-empty
        return empty

    eps_by_trace = _episodes_by_trace(suggestions)
    rows: list[dict[str, Any]] = []
    for trace in traces:
        trace_eps = eps_by_trace.get(trace.trace_id, [])
        rows.append(
            {
                "trace_id": trace.trace_id,
                "source_file": trace.source_file,
                "dialect": trace.dialect,
                "turn_counts": {
                    "user": len(trace.user_turns),
                    "agent": len(trace.agent_turns),
                    "total": len(trace.user_turns) + len(trace.agent_turns),
                },
                "signal_counts": _signal_counts(trace),
                "episode_count": len(trace_eps),
                "line_count": int(trace.line_count),
                "malformed_line_count": int(trace.malformed_line_count),
                "strip_model": build_strip_model(trace, trace_eps),
            }
        )
    # Richest traces lead: episode count desc, then source_file (deterministic).
    rows.sort(key=lambda r: (-r["episode_count"], r["source_file"]))
    return {
        "reflection_id": reflection_id,
        "epoch_id": epoch_id,
        "found": True,
        "trace_count": len(rows),
        "traces": rows,
    }


# ---------------------------------------------------------------------------
# Reader 2 — build_trace_detail (TRAJECTORY-UI.md §3.2)
# ---------------------------------------------------------------------------


def _empty_detail(reflection_id: str, trace_id: str) -> dict[str, Any]:
    return {
        "reflection_id": reflection_id,
        "epoch_id": None,
        "found": False,
        "trace_id": trace_id,
        "source_file": "",
        "dialect": "",
        "line_count": 0,
        "malformed_line_count": 0,
        "signal_counts": {},
        "strip_model": {},
        "turns": [],
        "reconstruction_note": "",
        "episodes": [],
    }


def _reconstructed_turns(trace: ImportedTrace) -> list[dict[str, Any]]:
    """The reconstructed conversation as alternating speaker rows (§3.2).

    The same ``[u0, a0, u1, a1, …]`` alternation the lane marks draw, but
    carrying the turn TEXT — the transcript turn vocabulary the run-level
    diff speaks.
    """
    user = list(trace.user_turns)
    agent = list(trace.agent_turns)
    seq: list[tuple[str, str]] = []
    for i in range(max(len(user), len(agent))):
        if i < len(user):
            seq.append(("user", user[i]))
        if i < len(agent):
            seq.append(("agent", agent[i]))
    return [
        {
            "index": i,
            "role": role,
            "text": text,
            "chars": len(text),
            # The persisted turn is already head-capped (trace_import); flag it.
            "truncated": text.endswith("…[elided]"),
        }
        for i, (role, text) in enumerate(seq)
    ]


def build_trace_detail(paths: WorkspacePaths, reflection_id: str, trace_id: str) -> dict[str, Any]:
    """One trace: strip-model + reconstructed conversation + episode anchors (§3.2)."""
    epoch_id, traces, suggestions = _resolve(paths, reflection_id)
    if epoch_id is None:
        return _empty_detail(reflection_id, trace_id)
    trace = next((t for t in traces if t.trace_id == trace_id), None)
    if trace is None:
        empty = _empty_detail(reflection_id, trace_id)
        empty["epoch_id"] = epoch_id
        return empty

    trace_eps = _episodes_by_trace(suggestions).get(trace_id, [])
    strip = build_strip_model(trace, trace_eps)

    episode_rows: list[dict[str, Any]] = []
    for ep in trace_eps:
        signal_kind = str(ep["signal_kind"])
        tone, glyph = _tone_glyph(signal_kind)
        span = next(
            (e for e in strip["episodes"] if e["episode_id"] == ep["episode_id"]),
            {"x0": 0.0, "x1": 1.0, "anchor": "lane"},
        )
        episode_rows.append(
            {
                "episode_id": ep["episode_id"],
                "episode_type": ep["episode_type"],
                "signal_kind": signal_kind,
                "summary": ep["summary"],
                "severity_rank": int(ep["severity_rank"]),
                "tone": tone,
                "glyph": glyph,
                "span": {"x0": span["x0"], "x1": span["x1"], "anchor": span["anchor"]},
                "suggestion_ids": list(ep["suggestion_ids"]),
            }
        )

    return {
        "reflection_id": reflection_id,
        "epoch_id": epoch_id,
        "found": True,
        "trace_id": trace.trace_id,
        "source_file": trace.source_file,
        "dialect": trace.dialect,
        "line_count": int(trace.line_count),
        "malformed_line_count": int(trace.malformed_line_count),
        "signal_counts": _signal_counts(trace),
        "strip_model": strip,
        "turns": _reconstructed_turns(trace),
        "reconstruction_note": (
            "turns are the reducer's reconstruction; user and agent sides are zipped by index"
        ),
        "episodes": episode_rows,
    }


# ---------------------------------------------------------------------------
# Reader 3 — build_suggestion_provenance (TRAJECTORY-UI.md §3.3)
# ---------------------------------------------------------------------------


def _empty_provenance(reflection_id: str, suggestion_id: str) -> dict[str, Any]:
    return {
        "reflection_id": reflection_id,
        "epoch_id": None,
        "found": False,
        "suggestion_id": suggestion_id,
        "suggestion_type": "",
        "subject": "",
        "summary": "",
        "target_slice": "",
        "foreign_source": None,
        "admission_viz": _admission_viz(None),
        "episodes": [],
    }


def _admission_viz(admission: dict[str, Any] | None) -> dict[str, Any]:
    """Render-ready admission marks (§3.3) — the BT-whisker / pip vocabulary.

    Honest: an unmeasured probe reads ``measured: false`` and carries no
    fabricated number (EVAL-SYNTHESIS.md §5). ``evidence_tier`` is ``probed``
    when a probe was spent, else ``planned``.
    """
    if not isinstance(admission, dict):
        return {
            "measured": False,
            "evidence_tier": "planned",
            "flip": {
                "measured": False,
                "rate": None,
                "runs": None,
                "over_ceiling": False,
                "ceiling": _FLIP_CEILING,
            },
            "discrimination": {"measured": False, "separated": 0, "pairs": 0},
            "leakage_ok": None,
        }

    raw_noise = admission.get("noise")
    noise: dict[str, Any] = raw_noise if isinstance(raw_noise, dict) else {}
    raw_disc = admission.get("discrimination")
    disc: dict[str, Any] = raw_disc if isinstance(raw_disc, dict) else {}
    raw_leak = admission.get("leakage")
    leak: dict[str, Any] = raw_leak if isinstance(raw_leak, dict) else {}
    spent = bool(admission.get("spent", False))

    flip_measured = bool(noise.get("measured", False))
    rate = noise.get("flip_rate")
    over_ceiling = bool(isinstance(rate, int | float) and rate > _FLIP_CEILING)
    disc_measured = bool(disc.get("measured", False))

    leakage_checked = bool(admission.get("leakage_checked", False)) or bool(leak.get("checked"))
    leakage_ok: bool | None
    if not leakage_checked:
        leakage_ok = None
    else:
        leakage_ok = not (
            leak.get("target_slice_ok") is False or bool(leak.get("self_preference_flag"))
        )

    return {
        "measured": bool(flip_measured or disc_measured or spent),
        "evidence_tier": "probed" if spent else "planned",
        "flip": {
            "measured": flip_measured,
            "rate": rate if flip_measured else None,
            "runs": noise.get("runs") if flip_measured else None,
            "over_ceiling": over_ceiling,
            "ceiling": _FLIP_CEILING,
        },
        "discrimination": {
            "measured": disc_measured,
            "separated": int(disc.get("separated", 0) or 0),
            "pairs": int(disc.get("pairs", 0) or 0),
        },
        "leakage_ok": leakage_ok,
    }


def build_suggestion_provenance(
    paths: WorkspacePaths, reflection_id: str, suggestion_id: str
) -> dict[str, Any]:
    """One suggestion's chain: suggestion → episodes → trace-segment strips (§3.3)."""
    epoch_id, traces, suggestions = _resolve(paths, reflection_id)
    if epoch_id is None:
        return _empty_provenance(reflection_id, suggestion_id)

    sug = next((s for s in suggestions if s.suggestion_id == suggestion_id), None)
    if sug is None:
        empty = _empty_provenance(reflection_id, suggestion_id)
        empty["epoch_id"] = epoch_id
        return empty

    eps_by_trace = _episodes_by_trace(suggestions)
    traces_by_id = {t.trace_id: t for t in traces}
    # This suggestion's episode ids (usually one for a bootstrap entry).
    source_episode_ids = [str(e) for e in (sug.provenance.get("source_episodes") or [])]
    # Index every drafted episode by id (across traces) for the chain lookup.
    ep_by_id: dict[str, dict[str, Any]] = {
        e["episode_id"]: e for eps in eps_by_trace.values() for e in eps
    }

    chain: list[dict[str, Any]] = []
    for eid in source_episode_ids:
        ep = ep_by_id.get(eid)
        if ep is None:
            continue
        signal_kind = str(ep["signal_kind"])
        tone, glyph = _tone_glyph(signal_kind)
        trace = traces_by_id.get(ep["trace_id"])
        segment = (
            build_strip_model(
                trace,
                eps_by_trace.get(trace.trace_id, []),
                focus_episode_id=eid,
            )
            if trace is not None
            else {}
        )
        chain.append(
            {
                "episode_id": ep["episode_id"],
                "episode_type": ep["episode_type"],
                "signal_kind": signal_kind,
                "summary": ep["summary"],
                "tone": tone,
                "glyph": glyph,
                "severity_rank": int(ep["severity_rank"]),
                "trace_id": ep["trace_id"],
                "source_file": ep["source_file"],
                "segment_strip_model": segment,
            }
        )

    foreign = sug.provenance.get("foreign_source")
    return {
        "reflection_id": reflection_id,
        "epoch_id": epoch_id,
        "found": True,
        "suggestion_id": sug.suggestion_id,
        "suggestion_type": sug.suggestion_type,
        "subject": sug.subject,
        "summary": sug.summary,
        "target_slice": sug.target_slice,
        "foreign_source": dict(foreign) if isinstance(foreign, dict) else None,
        "admission_viz": _admission_viz(sug.admission),
        "episodes": chain,
    }


__all__ = [
    "LANE_EXTENT_CAP",
    "budget_fill",
    "build_strip_model",
    "build_suggestion_provenance",
    "build_trace_detail",
    "build_trace_list",
    "lane_marks",
    "signal_ticks",
]
