"""Shared normalization for parity goldens.

A behavior-preserving refactor must not change *what* the system computes,
but a handful of fields in the persisted artifacts are wall-clock or
host-path dependent (timestamps, absolute tmp paths, date-stamped epoch
ids, random uuid patch ids). Those carry no behavioral meaning, so the
parity oracle masks them to fixed sentinels before diffing. Everything else
— every scalar, every loss, every decision, every structural id, every
field — is compared verbatim.

The masking is deliberately narrow: only fields that are known to be
non-deterministic by construction are touched. A refactor that silently
changes a real field will still surface as a diff.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

# ISO-8601 UTC timestamps, e.g. 2026-06-10T09:46:01+00:00 (with optional
# fractional seconds / Z spelling). These come from `_now_iso()` and are
# pure wall-clock noise.
_ISO_TS = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?")

# A date-prefixed epoch id, e.g. ``2026-06-10_t1_racing``. ``new_epoch``
# stamps the creation date into the id, so the id (and every reference to
# it — directory names, lineage keys, foreign keys) moves every calendar
# day. The date is creation-clock noise; the slug after it is the stable,
# behavioral part. Collapse just the date prefix to ``<DATE>`` so the
# golden is day-independent while still pinning the epoch slug. Anchored so
# it only fires on a leading date (not the date *inside* an ISO timestamp,
# which the timestamp rule already handles).
_EPOCH_DATE_PREFIX = re.compile(r"(?<![\dT-])(\d{4}-\d{2}-\d{2})(?=_)")

# A bare 32-char lowercase-hex token — a ``uuid4().hex`` patch id. These
# are random per-run by construction (the proposer mints a fresh id for
# each patch), so the VALUE carries no behavior; only the structure (each
# challenger carries exactly one patch id) does. Masked to a sentinel so a
# patch-id *count* / *placement* change still shows, but the random value
# does not break the golden. The whole-token rule (^...$) never clips a
# longer hash (e.g. a 64-char sha256 contract hash, which is preserved).
_UUID_HEX = re.compile(r"^[0-9a-f]{32}$")

#: Keys whose VALUES are timestamps and are masked wherever they appear.
_TS_KEYS = frozenset(
    {
        "created_at",
        "closed_at",
        "proposed_at",
        "completed_at",
        "started_at",
        "finished_at",
        "evaluated_at",
        "timestamp",
        "ts",
    }
)

#: Keys whose VALUES are runtime measurements (host-speed dependent). The
#: mock harness pins runtime_ms to 100, but other timing fields can leak
#: real wall-clock; mask any that are plausibly timing-derived.
_RUNTIME_KEYS = frozenset({"runtime_ms", "wall_clock_ms", "elapsed_ms", "duration_ms"})

_TS_SENTINEL = "<TS>"
_PATH_SENTINEL = "<TMP>"


def _mask_string(value: str, tmp_root: str | None) -> str:
    """Mask wall-clock timestamps, the date-prefixed epoch id, a random
    uuid patch id, and the volatile tmp root in a string."""
    if _UUID_HEX.match(value):
        return "<HEX32>"
    out = _ISO_TS.sub(_TS_SENTINEL, value)
    out = _EPOCH_DATE_PREFIX.sub("<DATE>", out)
    if tmp_root and tmp_root in out:
        out = out.replace(tmp_root, _PATH_SENTINEL)
    return out


def normalize_obj(obj: Any, *, tmp_root: str | None = None) -> Any:
    """Recursively normalize a JSON-able object for stable comparison.

    - Timestamp-valued keys collapse to ``<TS>``.
    - Any string that *contains* an ISO timestamp has it masked (covers
      run ids / paths that embed a timestamp).
    - The date-prefixed epoch id collapses to ``<DATE>...`` (value AND key).
    - A bare uuid patch id collapses to ``<HEX32>``.
    - The per-run tmp workspace root (an absolute path) collapses to
      ``<TMP>`` so the golden is host/dir independent.
    - Runtime measurement keys collapse to a fixed sentinel.
    """
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for key, val in obj.items():
            # Mask the date-prefixed epoch id when it is used as a KEY (it
            # shows up as a map key in some artifacts, e.g. per-epoch maps).
            out_key = _EPOCH_DATE_PREFIX.sub("<DATE>", key) if isinstance(key, str) else key
            if key in _TS_KEYS and isinstance(val, str):
                out[out_key] = _TS_SENTINEL if val else val
            elif key in _RUNTIME_KEYS and isinstance(val, int | float):
                out[out_key] = "<RUNTIME>"
            else:
                out[out_key] = normalize_obj(val, tmp_root=tmp_root)
        return out
    if isinstance(obj, list):
        return [normalize_obj(v, tmp_root=tmp_root) for v in obj]
    if isinstance(obj, str):
        return _mask_string(obj, tmp_root)
    return obj


def normalize_json_text(text: str, *, tmp_root: str | None = None) -> str:
    """Parse JSON text, normalize, and re-emit canonical sorted JSON."""
    data = json.loads(text)
    norm = normalize_obj(data, tmp_root=tmp_root)
    return json.dumps(norm, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def normalize_json_file(path: Path, *, tmp_root: str | None = None) -> str:
    return normalize_json_text(path.read_text(encoding="utf-8"), tmp_root=tmp_root)


__all__ = [
    "normalize_obj",
    "normalize_json_text",
    "normalize_json_file",
]
