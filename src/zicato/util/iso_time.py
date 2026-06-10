"""The one ISO-8601 UTC timestamp helper used across zicato writers.

Several subsystems (runtime state, heartbeat, control, lock, health
diagnostics) each grew a private second-precision UTC stamper with an
explicit ``Z`` suffix. They were byte-for-byte identical; this module is
their single home. Each caller re-exports :func:`now_iso` under its
historical local name so import paths and monkeypatch points are
unchanged.
"""

from __future__ import annotations

import datetime as _dt


def now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string with a ``Z`` suffix.

    Seconds precision (``microsecond`` zeroed) so the strings diff cleanly
    in journals and carry no microsecond noise. The trailing ``Z`` is the
    explicit UTC marker convention every zicato writer uses.
    """
    return _dt.datetime.now(_dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


__all__ = [
    "now_iso",
]
