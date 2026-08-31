"""The shared presentation derivations, ported from ``ui.js``.

A SECOND RENDERER, NEVER A SECOND BRAIN. Every number the TUI shows comes off
the dashboard service's payloads unchanged; the only thing this module does is
turn a payload field into the SAME characters the browser would print. Nothing
here re-derives a verdict, an aggregate or a standing — where the browser
derives presentation in JS, that mapping is ported one-for-one and CROSS-PINNED
by a shared fixture asserted against both implementations
(``tests/test_tui_crosspin.py`` and ``static/test/render_crosspin.test.mjs``),
so the two surfaces cannot drift apart about what "stalled" means.

Each function below names its ``ui.js`` original. Change one, change both, and
the cross-pin fixture is where you record what changed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

#: The null render. Never ``0`` — zero is a legal measurement. (``ui.js`` fmt.)
NULL = "—"

#: Below this many settled duels the ridge prior still dominates the fit, so
#: the rating renders with the faint ``provisional`` suffix rather than as a
#: settled strength. (``ui.js`` MIN_RATING_GAMES.)
MIN_RATING_GAMES = 5


def is_num(value: Any) -> bool:
    """``ui.js`` ``isNum`` — a finite real number, and ``bool`` is not one."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    return math.isfinite(float(value))


def num(value: Any) -> float | None:
    """``value`` as a float when :func:`is_num` accepts it, else ``None``.

    The narrowing companion to :func:`is_num`. Every call site that would
    otherwise read ``x if is_num(x) else None`` and then re-convert goes
    through here, so the "is it a number" test and the conversion can never
    disagree — and a reader never has to check whether they did.
    """
    return float(value) if is_num(value) else None


def _fixed(value: float, digits: int) -> str:
    """JS ``Number.prototype.toFixed`` semantics (half-away-from-zero)."""
    if digits < 0:
        raise ValueError("digits must be non-negative")
    scaled = value * (10**digits)
    # JS rounds .5 away from zero; Python's round() is banker's rounding.
    rounded = math.floor(abs(scaled) + 0.5) * (1 if scaled >= 0 else -1)
    out = f"{rounded / (10**digits):.{digits}f}"
    return "0" + out[1:] if out.startswith("-0") and float(out) == 0 else out


def fmt(value: Any, digits: int = 3) -> str:
    """``svg.js`` ``fmt`` — fixed-precision, ``—`` when absent."""
    return _fixed(float(value), digits) if is_num(value) else NULL


def fmt_signed(value: Any, digits: int = 3) -> str:
    """``svg.js`` ``fmtSigned`` — an explicit ``+`` on a positive delta."""
    if not is_num(value):
        return NULL
    return ("+" if float(value) > 0 else "") + _fixed(float(value), digits)


def score_fmt(value: Any, digits: int = 2) -> str:
    """``ui.js`` ``scoreFmt`` — the 0-1 score axis, two decimals by default."""
    return fmt(value, digits)


def fmt_duration_ms(ms: Any) -> str:
    """``ui.js`` ``fmtDurationMs`` — ms/s/m/h, one decimal above a second."""
    if not is_num(ms) or float(ms) < 0:
        return NULL
    v = float(ms)
    if v < 1000:
        return f"{math.floor(v + 0.5):.0f}ms"
    s = v / 1000
    if s < 90:
        return f"{math.floor(s * 10 + 0.5) / 10:g}s"
    m = s / 60
    if m < 90:
        return f"{math.floor(m * 10 + 0.5) / 10:g}m"
    return f"{math.floor((m / 60) * 10 + 0.5) / 10:g}h"


def truncate(value: Any, n: int, *, fallback: str = "") -> str:
    """``ui.js`` ``truncate`` — clip to ``n`` chars with a trailing ellipsis."""
    text = fallback if value is None else str(value)
    return text[: n - 1] + "…" if len(text) > n else text


# ---------------------------------------------------------------------------
# The FOUR absences — never merged into one
# ---------------------------------------------------------------------------
#
# "Nothing here" is four different facts, and collapsing them is the most
# expensive lie this surface could tell, because each one calls for a
# different action from the operator:
#
#   1. NO MEASUREMENT      "—"                          the run produced no
#                                                       value for this cell.
#   2. MEASURED-IMPOSSIBLE "n/a — insufficient          the quantity was
#                          replication"                 attempted and cannot
#                                                       be computed at this n.
#   3. FEATURE OFF         the panel is OMITTED         nothing was asked for;
#                                                       a row would imply it
#                                                       was and came back
#                                                       empty.
#   4. UNMEASURED          "unmeasured · <reason>"      the third verdict:
#                                                       measurement was
#                                                       possible and was not
#                                                       taken, and the reason
#                                                       is the actionable part.
#
# Rendering (2) as "—" tells an operator to go look for missing data that does
# not exist; rendering (4) as "—" hides a to-do; rendering (3) at all invents
# a feature. A lens that cannot tell which case it is in must say so rather than pick.

#: Case 2 — the quantity is defined but not computable from this many runs.
INSUFFICIENT = "n/a — insufficient replication"


def unmeasured(reason: Any = None) -> str:
    """Case 4 — the third verdict: measurable rather than measured, and why.

    The reason IS the payload. ``unmeasured`` on its own is a shrug; the whole
    value of the third verdict is that it names what to do about it.
    """
    text = str(reason).strip() if reason is not None else ""
    return f"unmeasured · {text}" if text else "unmeasured"


def measured(value: Any, *, digits: int = 3, reason: Any = None, enough: bool = True) -> str:
    """Render a measurement, choosing between absences 1, 2 and 4 correctly.

    * a finite number            → the number
    * ``enough=False``           → :data:`INSUFFICIENT` (case 2)
    * a ``reason`` given         → :func:`unmeasured` (case 4)
    * otherwise                  → :data:`NULL` (case 1)

    Case 3 (feature off) is not expressible here on purpose: it is the absence
    of a row, and a function that returns a string cannot express "print
    nothing" without a caller that checks — so callers omit the block instead.
    """
    if is_num(value):
        return fmt(value, digits)
    if not enough:
        return INSUFFICIENT
    if reason is not None and str(reason).strip():
        return unmeasured(reason)
    return NULL


# ---------------------------------------------------------------------------
# Verdicts and decisions — the vocabulary both surfaces share
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LoopVerdict:
    """``ui.js`` ``loopVerdict``'s return: the phrase and its severity class."""

    word: str
    cls: str


def loop_verdict(traj: Any) -> LoopVerdict | None:
    """``ui.js`` ``loopVerdict`` — the loop-communication phrase, or None.

    Only the words that report a PROBLEM live here: ``improving`` and
    ``warming_up`` return ``None`` so a healthy or undecided loop stays quiet.
    ``stalled`` is the no-floor sibling of ``no_signal`` — challengers fielded,
    none promoted, and no A/A floor measured — so the phrase names the
    promotions that did not happen and claims nothing about noise.
    """
    verdict = traj.get("verdict") if isinstance(traj, dict) else None
    if verdict == "no_signal":
        return LoopVerdict("no detectable signal (below noise floor)", "nosignal")
    if verdict == "stalled":
        return LoopVerdict("stalled (no promotions)", "stalled")
    if verdict == "plateaued":
        return LoopVerdict("plateaued", "plateau")
    return None


def decision_of(rec: Any) -> str | None:
    """``ui.js`` ``decisionOf`` — the SERVER-STAMPED decision token, verbatim.

    The server owns the vocabulary (promoted / rejected / deferred); a renderer
    never re-classifies a nested outcome or substring-matches free text.
    """
    if not isinstance(rec, dict):
        return None
    decision = rec.get("decision")
    return decision if isinstance(decision, str) and decision else None


def verdict_label(decision: Any) -> str:
    """``ui.js`` ``verdictPill``'s label text for a decision token."""
    d = decision or "baseline"
    if d == "baseline":
        return "seed (v0)"
    if d == "pending":
        return "racing…"
    return str(d)


def promotion_rate_label(traj: Any) -> str | None:
    """``ui.js`` ``promotionRateLabel`` — ``2/7 · 29%``, or None when unmeasured."""
    if not isinstance(traj, dict):
        return None
    challengers = num(traj.get("challenger_count"))
    rate = num(traj.get("promotion_rate"))
    if rate is None or challengers is None or challengers <= 0:
        return None
    promoted = traj.get("promoted_count") or 0
    pct = math.floor(rate * 100 + 0.5)
    return f"{promoted}/{traj['challenger_count']} · {pct:.0f}%"


def cost_per_promotion_label(cost: Any) -> str | None:
    """``ui.js`` ``costPerPromotionLabel`` — the cost of one promotion."""
    if not isinstance(cost, dict):
        return None
    value = cost.get("cost_per_promotion_ms")
    return fmt_duration_ms(value) if is_num(value) else None


def noise_band(traj: Any, spark_values: Any) -> dict[str, float] | None:
    """``ui.js`` ``noiseBandFor`` — the A/A floor band around the latest point."""
    if not isinstance(traj, dict):
        return None
    floor = traj.get("noise_floor")
    half = num(floor.get("max_abs_delta")) if isinstance(floor, dict) else None
    values = [n for n in (num(v) for v in spark_values or []) if n is not None]
    if half is None or half <= 0 or not values:
        return None
    return {"center": values[-1], "half": half}


# ---------------------------------------------------------------------------
# Ratings — uncertainty is part of the number
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Rating:
    """``ui.js`` ``ratingModel``'s render model for a rating-bearing row."""

    elo: int
    se: int | None
    games: float | None
    provisional: bool
    text: str


def rating_model(src: Any) -> Rating | None:
    """``ui.js`` ``ratingModel`` — ``{elo, elo_se, elo_games}`` to ``1512 ±34``.

    Integer register: a rating is a legibility number, never false precision.
    ``None`` when the row carries no rating at all.
    """
    elo_raw = num(src.get("elo")) if isinstance(src, dict) else None
    if elo_raw is None:
        return None
    elo = int(math.floor(elo_raw + 0.5))
    se_raw = num(src.get("elo_se"))
    se = int(math.floor(se_raw + 0.5)) if se_raw is not None else None
    games = num(src.get("elo_games"))
    return Rating(
        elo=elo,
        se=se,
        games=games,
        provisional=games is not None and games < MIN_RATING_GAMES,
        text=f"{elo} ±{se}" if se is not None else str(elo),
    )


def rating_text(src: Any, *, games: bool = False) -> str:
    """The rating column's full text: value, ``provisional`` suffix, games.

    The browser renders the provisional suffix as a faint sibling span; the
    terminal carries the same three facts on one line. Unrated is ``—``.
    """
    model = rating_model(src)
    if model is None:
        return NULL
    parts = [model.text]
    if games and model.games is not None:
        parts.append(f"· {model.games:g} games")
    if model.provisional:
        parts.append("provisional")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# The progress cursor — the no-op-skip gate
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Progress:
    """``core/state.js`` ``noteProgress``'s return."""

    advanced: bool
    rollover: bool
    present: bool

    @property
    def should_refresh(self) -> bool:
        """True when this frame is worth doing ANY work for.

        An absent ``seq`` degrades to always refreshing: a server that
        serves no progress cursor gives the client nothing to skip on, and a
        stale screen costs more than a wasted fetch.
        """
        return self.advanced or self.rollover or not self.present


def note_progress(seq: Any, terminal: Any, last_seq: int) -> Progress:
    """``core/state.js`` ``noteProgress`` — is this ``state_change`` real work?

    The SSE stream carries no digest, so ``seq`` IS the no-op gate. The three
    outcomes, each load-bearing:

    * **advanced** (``seq`` strictly increased, or it is the first ever seen) —
      genuine forward progress; refetch and repaint.
    * **rollover** (``seq`` went BACKWARDS) — the progress log was cleared on a
      fresh ``evolve`` boot and restarts at 1. The run RESTARTED, so this
      forces a full re-apply and the cursor resets to the low value; treating
      it as "no progress" would freeze the screen on the previous run.
    * **neither** (``seq`` repeated) — a no-op beat. Zero fetches, zero
      patches. This is the case the whole gate exists for.

    ``present=False`` (a non-numeric or absent ``seq``) degrades to refreshing
    on every beat.
    """
    value = num(seq)
    if value is None:
        return Progress(advanced=False, rollover=False, present=False)
    if last_seq < 0 or value > last_seq:
        return Progress(advanced=True, rollover=False, present=True)
    if value < last_seq:
        return Progress(advanced=False, rollover=True, present=True)
    return Progress(advanced=False, rollover=False, present=True)


# ---------------------------------------------------------------------------
# Severity tones — the reflection vocabulary
# ---------------------------------------------------------------------------


def severity_tone(severity: Any) -> str:
    """``instrument.js`` ``severityTone`` — a finding's severity to a tone."""
    s = str(severity or "").lower()
    if s == "critical":
        return "bad"
    if s == "warning":
        return "warn"
    return "faint"


def practice_tone(verdict: Any) -> str:
    """``instrument.js`` ``practiceTone`` — a practice verdict to a tone.

    ``sound`` affirms, ``unsound`` is the anti-practice, ``attend`` is a soft
    deficiency, and anything else is honest-absent rather than good news.
    """
    v = str(verdict or "").lower()
    if v == "sound":
        return "good"
    if v == "unsound":
        return "bad"
    if v == "attend":
        return "warn"
    return "faint"


# ---------------------------------------------------------------------------
# Tournament structure — one normalisation, two renderers
# ---------------------------------------------------------------------------

#: Phases that are NOT a running tournament. (``structure.js``
#: ``normalizeStructure``.)
_SETTLED_PHASES = ("", "idle", "complete", "completed", "done")


def normalize_structure(st: Any, live: bool) -> dict[str, Any] | None:
    """``structure.js`` ``normalizeStructure`` — one renderer input, two sources.

    Folds the LIVE ``/api/active-tournament`` payload and the COMPLETED
    ``/api/tournament-structure`` record into the same shape, so a lens never
    branches on which endpoint answered. ``live`` says the payload came from
    active-tournament; ``running`` additionally requires a phase that is
    actually in flight, because an idle envelope is not a live tournament.

    The ``stage_index`` -> ``round_index`` rename is carried here too: the
    persisted within-tournament stage key was renamed, and workspaces written
    before the rename must still render.
    """
    if not isinstance(st, dict):
        return None
    phase = str(st.get("phase") or "").lower()
    running = bool(live) and phase not in _SETTLED_PHASES

    rounds = []
    for entry in st.get("rounds") or []:
        if isinstance(entry, dict) and entry.get("round_index") is None:
            stage = entry.get("stage_index")
            if stage is not None:
                entry = {**entry, "round_index": stage}
        rounds.append(entry)

    def as_list(key: str) -> list[Any]:
        value = st.get(key)
        return value if isinstance(value, list) else []

    def as_dict_or_none(key: str) -> dict[str, Any] | None:
        value = st.get(key)
        return value if isinstance(value, dict) else None

    return {
        "structure": st.get("structure") or "gauntlet",
        "structure_params": st.get("structure_params") or st.get("params") or {},
        "competitors": as_list("competitors"),
        "entries": as_list("entries"),
        "rounds": rounds,
        "gen_states": st["gen_states"] if isinstance(st.get("gen_states"), list) else None,
        "standings": as_list("standings"),
        "field_status": as_list("field_status"),
        "champion_lineage": as_list("champion_lineage"),
        "partial_champion_agg": as_dict_or_none("partial_champion_agg"),
        "projected": as_dict_or_none("projected"),
        "source": "live" if running else (st.get("source") or "index"),
        "phase": str(st["phase"]) if st.get("phase") is not None else None,
        "live": running,
    }


__all__ = [
    "INSUFFICIENT",
    "MIN_RATING_GAMES",
    "NULL",
    "LoopVerdict",
    "Progress",
    "Rating",
    "cost_per_promotion_label",
    "decision_of",
    "fmt",
    "fmt_duration_ms",
    "fmt_signed",
    "is_num",
    "loop_verdict",
    "measured",
    "noise_band",
    "note_progress",
    "normalize_structure",
    "num",
    "practice_tone",
    "promotion_rate_label",
    "rating_model",
    "rating_text",
    "score_fmt",
    "severity_tone",
    "truncate",
    "unmeasured",
    "verdict_label",
]
