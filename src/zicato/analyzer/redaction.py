"""The mechanical redaction primitives — one source of truth, two consumers.

These functions are the *shared* half of the default-deny redaction the
process-exemplar channel introduced. ``docs/design/PROCESS-EXEMPLARS.md``
§3 stays NORMATIVE for rules R1–R4; this module is only where the two
rules that are pure string transforms live, so both consumers apply
byte-identical redaction:

* :mod:`zicato.analyzer.process_exemplars` — the drift-anchored, redacted
  event windows rendered into the proposer prompt (R1 payload allowlist
  and R2 window-local anonymization stay there, since both are bound to
  the exemplar window's own structure);
* :mod:`zicato.proposer.redacted_query` — the proposer's on-demand
  redacted query surface over the train slice, which emits only closed
  vocabulary plus a small number of harness-side labels and passes every
  one of those labels through the same scrub/truncate pair.

The two rules implemented here:

* **R3** :func:`truncate_free_text` — head/tail elision at a fixed cap.
* **R4** :func:`scrub_identity` — replace every identity fragment / token
  in a kept string with :data:`WITHHELD`. :func:`iter_string_leaves` is
  the corpus-building helper each consumer uses to reach the string
  leaves of a nested payload.

ORDER IS LOAD-BEARING: scrub FIRST, truncate SECOND (see the
:func:`truncate_free_text` docstring). No LLM redactor, ever — every rule
here is mechanical and deterministic, so the same input always redacts to
the same output and re-presenting a redacted value leaks nothing new.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from typing import Any

#: Free-text truncation (R3): total ceiling and the head/tail split. Only
#: T-class fields — process narration authored by goldfive's own
#: detectors / judges / steerer — ever reach this; task text and model
#: outputs are D-class and never render at all.
FREE_TEXT_LIMIT_CHARS = 160
FREE_TEXT_HEAD_CHARS = 120
FREE_TEXT_TAIL_CHARS = 24
ELISION = " … "

#: Identity-corpus scrub (R4): a dropped string value must be at least this
#: long to be scrubbed by substring (shorter dropped values — enum-ish
#: strings like ``"warning"`` — would mangle legitimate process text).
#: Identity TOKENS (entry / task / invocation / run ids) are scrubbed at
#: any length via word-boundary matching instead.
MIN_SCRUB_LEN = 12

#: Replacement marker for scrubbed identity content.
WITHHELD = "[withheld]"


def truncate_free_text(text: str) -> str:
    """Cap free text at :data:`FREE_TEXT_LIMIT_CHARS` with head/tail elision.

    Keeps the first :data:`FREE_TEXT_HEAD_CHARS` and the last
    :data:`FREE_TEXT_TAIL_CHARS` characters joined by :data:`ELISION`.
    Runs AFTER the identity scrub (R4) — truncation only removes
    characters, and the elision marker sits between head and tail, so a
    scrubbed text can never re-form an identity string across the split.
    """
    if len(text) <= FREE_TEXT_LIMIT_CHARS:
        return text
    head = text[:FREE_TEXT_HEAD_CHARS].rstrip()
    tail = text[-FREE_TEXT_TAIL_CHARS:].lstrip()
    return f"{head}{ELISION}{tail}"


def iter_string_leaves(obj: Any) -> Iterator[str]:
    """Yield every string leaf of a nested payload structure."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, Mapping):
        for v in obj.values():
            yield from iter_string_leaves(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from iter_string_leaves(v)


def scrub_identity(text: str, corpus_texts: frozenset[str], tokens: frozenset[str]) -> str:
    """Replace every corpus occurrence inside kept free text with a marker.

    Two passes (PROCESS-EXEMPLARS.md §3 R4): long dropped-field strings are
    replaced by plain substring match (longest first, so a fragment nested
    inside a longer fragment cannot survive its outer replacement); identity
    tokens are replaced on word boundaries at any length (so a short id
    like ``t1`` cannot mangle unrelated words). This is defense in depth
    behind the R1 allowlist — it catches verbatim quotation of dropped
    content inside admitted process text.
    """
    for fragment in sorted(corpus_texts, key=len, reverse=True):
        if fragment in text:
            text = text.replace(fragment, WITHHELD)
    for token in sorted(tokens, key=len, reverse=True):
        if not token:
            continue
        text = re.sub(rf"(?<![\w-]){re.escape(token)}(?![\w-])", WITHHELD, text)
    return text


__all__ = [
    "ELISION",
    "FREE_TEXT_HEAD_CHARS",
    "FREE_TEXT_LIMIT_CHARS",
    "FREE_TEXT_TAIL_CHARS",
    "MIN_SCRUB_LEN",
    "WITHHELD",
    "iter_string_leaves",
    "scrub_identity",
    "truncate_free_text",
]
