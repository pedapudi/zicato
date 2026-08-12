"""Per-slot edit-class hints for the best-of-N slate, failure-mode aware.

The best-of-N wrapper stamps one edit-class hint on each slate slot's
:class:`~zicato.proposer.agent.ProposerContext` so the N samples explore
different edit strategies instead of the LLM re-rolling one idea. This
module owns the hint strings and the pure slot→hint mapping:

* :data:`EDIT_CLASS_HINTS` — the exploratory rotation (the historical
  behaviour): slot ``i`` gets ``EDIT_CLASS_HINTS[i % len]``.
* :data:`FAILURE_MODE_HINTS` — one mode-appropriate hint per outcome
  failure mode (over-retrieval / misses / empty-terse / looping), keyed
  by the mode token :func:`dominant_failure_mode` returns.
* :func:`hint_for_slot` — the mapping the wrapper calls per slot. When
  the round's bucketed failure-mode profile names a DOMINANT mode, slots
  ``0..N-2`` get that mode's hint (the slate concentrates on the observed
  problem); the LAST slot always stays exploratory — it rotates over the
  remaining exploratory hints — so the slate never goes all-in on one
  reading of the profile. An absent / empty / signal-free profile falls
  back to the pure rotation, byte-identical to the historical prompts.

Visibility discipline (LOAD-BEARING)
------------------------------------
Every hint here is a STATIC, banded instruction string — no board entry
id, no question text, no per-entry value ever appears in a hint. The
selection reads only the ALREADY-BANDED failure-mode profile string the
orchestrator threads to the proposer (built by
:func:`zicato.proposer.prompts.render_failure_mode_profile`, every number
coarsened), so conditioning the hints on it cannot widen the
restricted-visibility envelope (OVERFITTING.md §11): the proposer already
sees that exact string in its prompt.

Why parse the rendered profile string (not the summary object)
---------------------------------------------------------------
``ProposerContext.failure_profile`` is the pre-rendered, bucketed block —
the orchestrator renders it once and every consumer downstream (the
proposer prompt, the best-of-N critic) reads the same string. Selecting
the dominant mode FROM that string keeps this mapping inside the same
envelope by construction: it can never see a finer-grained number than
the proposer itself does. The parse targets the renderer's stable line
shapes and degrades to the exploratory rotation on anything it does not
recognise.
"""

from __future__ import annotations

import re
import zlib

#: Per-slot edit-class steering for the best-of-N slate — the intra-slate
#: DIVERSITY lever and the exploratory rotation. Slot ``i`` of a slate with
#: no dominant failure mode gets ``EDIT_CLASS_HINTS[i % len]`` stamped on
#: its context (``sample_hint``). Static instruction strings only — no
#: board identity, no per-entry data — so the hints compose with the
#: restricted-visibility envelope untouched.
EDIT_CLASS_HINTS: tuple[str, ...] = (
    "For THIS candidate, prefer the smallest grounded fix: the minimal, "
    "most surgical edit that directly addresses an observed failure mode.",
    "For THIS candidate, prefer a structurally different mechanism than "
    "the most recent attempts in the experiment memory — do not re-roll a "
    "variation of the last hypothesis; change the approach, not the dial.",
    "For THIS candidate, target the highest-loss failure mode head-on, "
    "even if the edit is larger — go after the biggest observed cost.",
)

#: Mode-appropriate edit-class hints, keyed by the mode token
#: :func:`dominant_failure_mode` extracts from the bucketed profile. Like
#: :data:`EDIT_CLASS_HINTS`, each value is a static, banded instruction
#: string: it names the failure MODE (which the profile already showed the
#: proposer) and a general repair direction — never an entry, a question,
#: or an exact rate.
FAILURE_MODE_HINTS: dict[str, str] = {
    "over_retrieval": (
        "For THIS candidate, target the observed over-retrieval failure "
        "mode: outputs include too much irrelevant material (precision "
        "down). Prefer an edit that tightens selection or filtering so "
        "only relevant items survive, without narrowing genuine coverage."
    ),
    "misses": (
        "For THIS candidate, target the observed misses failure mode: "
        "relevant items are being left out (recall down). Prefer an edit "
        "that broadens or strengthens retrieval/coverage so relevant "
        "items are found, without indiscriminately widening the output."
    ),
    "empty_terse": (
        "For THIS candidate, target the observed empty/terse-answer "
        "failure mode: runs produce empty or too-short outputs. Prefer an "
        "edit that makes the system commit to a substantive answer — "
        "e.g. remove instructions that license bailing out early."
    ),
    "looping": (
        "For THIS candidate, target the observed looping failure mode: "
        "runs repeat reasoning steps or tool calls. Prefer an edit that "
        "breaks the repetition — tighten stopping criteria, cap retries, "
        "or restructure the step that re-enters itself."
    ),
}

#: The renderer's directional markers on the recall/precision decomposition
#: line — its single most actionable signal (see
#: :func:`zicato.proposer.prompts.render_failure_mode_profile`). When one is
#: present it names the dominant mode outright, so it wins over the
#: rate-comparison fallback below.
_MARKER_MODES: tuple[tuple[str, str], ...] = (
    ("=> over-retrieves", "over_retrieval"),
    ("=> misses relevant items", "misses"),
)

#: Rate-bearing profile lines, one regex per mode, matching the renderer's
#: stable line shapes (``over-retrieval (precision<0.5): ~40% of runs``,
#: ``empty / terse answers: ~20% | looping: ~10%``). Each captures the
#: banded rate token (``none`` / ``~all`` / ``~N%``).
_RATE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("over_retrieval", re.compile(r"over-retrieval[^:]*:\s*(none|~all|~\d+%)")),
    ("empty_terse", re.compile(r"empty / terse answers:\s*(none|~all|~\d+%)")),
    ("looping", re.compile(r"looping:\s*(none|~all|~\d+%)")),
)


def _band_to_rate(band: str) -> float:
    """Decode a banded rate token back to its approximate fraction.

    Inverts :func:`zicato.proposer.prompts.band_rate`'s output vocabulary:
    ``none`` → 0.0, ``~all`` → 1.0, ``~N%`` → N/100. Only ever applied to a
    token one of the :data:`_RATE_PATTERNS` captured, so the fallthrough is
    defensive.
    """
    if band == "none":
        return 0.0
    if band == "~all":
        return 1.0
    match = re.fullmatch(r"~(\d+)%", band)
    if match is None:  # pragma: no cover — the capture groups exclude this
        return 0.0
    return int(match.group(1)) / 100.0


def dominant_failure_mode(failure_profile: str) -> str | None:
    """Extract the dominant failure mode from the bucketed profile block.

    Deterministic, two-step read of the renderer's stable line shapes:

    1. **Directional marker first.** The recall/precision decomposition
       line's ``=> over-retrieves`` / ``=> misses relevant items`` suffix is
       the profile's most actionable signal (it compares the two quality
       means directly), so when present it names the mode outright.
    2. **Banded-rate comparison.** Otherwise the per-mode rate bands
       (over-retrieval / empty-terse / looping) are decoded back to their
       approximate fractions and the strictly-largest positive rate wins;
       ties break in that fixed order, so the result is deterministic for
       a fixed profile string.

    Returns a :data:`FAILURE_MODE_HINTS` key, or ``None`` when the profile
    is absent, empty, or carries no positive failure signal — the caller's
    sentinel for "use the exploratory rotation".
    """
    if not failure_profile.strip():
        return None
    for marker, mode in _MARKER_MODES:
        if marker in failure_profile:
            return mode
    best_mode: str | None = None
    best_rate = 0.0
    for mode, pattern in _RATE_PATTERNS:
        match = pattern.search(failure_profile)
        if match is None:
            continue
        rate = _band_to_rate(match.group(1))
        if rate > best_rate:  # strict: ties keep the earlier (fixed-order) mode
            best_rate = rate
            best_mode = mode
    return best_mode


#: Per-slot STRATEGY framings for the best-of-N slate — the SECOND intra-slate
#: diversity axis (the edit-class hint above says WHICH failure to target; the
#: strategy says HOW to approach the fix). Composed WITH the edit-class hint on
#: each slate slot so the N samples do not all share one strategic framing. Each
#: is a static instruction string carrying no board identity — the
#: restricted-visibility envelope is untouched — and the vocabulary is a small
#: fixed set rotated deterministically per (slot, round) by
#: :func:`strategy_for_slot` (no RNG).
STRATEGY_HINTS: tuple[str, ...] = (
    "Strategy for THIS candidate: MINIMAL-SURGICAL. Make the smallest possible "
    "change that could plausibly work; touch as few mutation points as you can.",
    "Strategy for THIS candidate: STRUCTURAL-REWORK. Do not settle for a local "
    "tweak — reshape the mechanism (the control flow, the prompt structure, the "
    "orchestration) even if the diff is larger.",
    "Strategy for THIS candidate: DEFENSIVE-HARDENING. Assume the current failure "
    "is an unhandled edge or a missing guard; add the check, the fallback, or the "
    "constraint that makes the failure mode impossible rather than less likely.",
    "Strategy for THIS candidate: CONTRARIAN. Reject the obvious fix the other "
    "candidates will reach for and try a DIFFERENT mechanism entirely — if the "
    "consensus is to add something, consider removing something instead.",
)


def strategy_for_slot(sample_index: int, generation_id: str) -> str:
    """Return the STRATEGY framing for slate slot ``sample_index`` this round.

    Deterministic per ``(slot, round)`` with NO RNG: the round is identified by
    ``generation_id`` (the child id being proposed this round — stable within a
    round, distinct across rounds), reduced to a stable rotation offset via
    :func:`zlib.crc32` (a fixed, seed-independent hash — unlike :func:`hash`,
    which is salted per process). Slot ``i`` of round ``g`` gets
    ``STRATEGY_HINTS[(crc32(g) + i) % len]``, so the N slots of one round span
    distinct strategies AND the same slot draws a different strategy in a
    different round — the two-axis slate diversity — while re-running the SAME
    round yields the SAME assignment (the determinism pin).

    A static instruction string carrying no board identity, so it composes with
    the restricted-visibility envelope untouched.
    """
    seed = zlib.crc32(generation_id.encode("utf-8"))
    return STRATEGY_HINTS[(seed + sample_index) % len(STRATEGY_HINTS)]


def hint_for_slot(sample_index: int, n: int, failure_profile: str) -> str:
    """Return the edit-class hint for slate slot ``sample_index`` of ``n``.

    Pure and deterministic — the same ``(sample_index, n, failure_profile)``
    always yields the same hint string.

    * **No dominant mode** (absent / empty / signal-free profile): the
      historical exploratory rotation, ``EDIT_CLASS_HINTS[i % len]`` —
      byte-identical hints, so every prompt downstream is byte-identical
      to the pre-conditioning behaviour.
    * **Dominant mode, slots ``0..n-2``:** the mode's
      :data:`FAILURE_MODE_HINTS` entry — the slate concentrates its
      samples on the failure mode the round's profile actually shows.
    * **Dominant mode, LAST slot (``n-1``):** always exploratory — rotates
      over the exploratory hints (minus the mode hint, were it ever to
      collide) so the slate never goes all-in on one reading of the
      profile; a mis-diagnosed dominant mode still leaves one candidate
      exploring freely.

    ``sample_index`` is the 0-based slot within a slate of ``n`` samples
    (``0 <= sample_index < n`` by the caller's loop contract).
    """
    mode = dominant_failure_mode(failure_profile)
    if mode is None:
        return EDIT_CLASS_HINTS[sample_index % len(EDIT_CLASS_HINTS)]
    if sample_index < n - 1:
        return FAILURE_MODE_HINTS[mode]
    mode_hint = FAILURE_MODE_HINTS[mode]
    remaining = tuple(h for h in EDIT_CLASS_HINTS if h != mode_hint) or EDIT_CLASS_HINTS
    return remaining[sample_index % len(remaining)]


__all__ = [
    "EDIT_CLASS_HINTS",
    "FAILURE_MODE_HINTS",
    "STRATEGY_HINTS",
    "dominant_failure_mode",
    "hint_for_slot",
    "strategy_for_slot",
]
