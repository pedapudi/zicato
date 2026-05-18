"""Thin replay wrapper around :func:`goldfive.sinks.persistence.replay_from_jsonl`.

Two helpers:

* :func:`replay_events` — round-trip a goldfive events JSONL back to a
  list of parsed ``Event`` messages. The wrapper exists so zicato tests
  can ``import`` a stable name without binding to the upstream module
  path and so the import guard is enforced uniformly: when goldfive is
  not installed, the call raises ``ImportError`` (with a clear message)
  rather than a deeper ``ModuleNotFoundError`` from a generated stub.

* :func:`events_to_dicts` — project each event to a plain ``dict`` so
  assertions in tests don't have to handle the proto identity / message
  factory dance. The function tolerates both real proto messages (via
  ``MessageToDict``) and any object that implements either ``to_dict``
  or :func:`dataclasses.asdict`; this matches the permissive replay
  shape :func:`zicato.testing.fixtures.make_synthetic_events_jsonl`
  produces when goldfive is not installed.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any


def replay_events(jsonl_path: Path) -> list[Any]:
    """Read ``jsonl_path`` back as a list of parsed goldfive ``Event`` messages.

    Thin wrapper over :func:`goldfive.sinks.persistence.replay_from_jsonl`.
    The import is deferred to call time so importing
    :mod:`zicato.testing.replay` itself does not require goldfive — the
    function only fails when actually invoked without the dependency.

    Parameters
    ----------
    jsonl_path:
        Path to a goldfive events JSONL file (one JSON-encoded
        ``Event`` per line, as written by
        :class:`goldfive.sinks.persistence.JSONLPersistenceSink`).

    Raises
    ------
    ImportError
        If goldfive (or its proto stubs) is not available. Tests that
        depend on goldfive should call ``pytest.importorskip("goldfive")``
        and skip cleanly when the optional dependency is missing.
    """
    try:
        from goldfive.sinks.persistence import replay_from_jsonl
    except ImportError as exc:  # pragma: no cover - exercised by importorskip
        raise ImportError(
            "zicato.testing.replay.replay_events requires goldfive to be "
            "installed; install zicato with the goldfive dependency or "
            "skip the test with pytest.importorskip('goldfive')."
        ) from exc

    events: list[Any] = replay_from_jsonl(jsonl_path)
    return events


def events_to_dicts(events: list[Any]) -> list[dict[str, Any]]:
    """Project each event to a plain ``dict`` for assertion-friendly comparisons.

    Tries the following in order, per event:

    1. If the event is a proto message (has a ``DESCRIPTOR`` attribute),
       use ``google.protobuf.json_format.MessageToDict`` with
       ``preserving_proto_field_name=True`` so field names match the
       JSONL/wire form (snake_case).
    2. If the event has a ``to_dict`` method, call it.
    3. If the event is a :mod:`dataclasses` instance, use
       :func:`dataclasses.asdict`.
    4. Otherwise, reflect public attributes via :func:`vars`.

    The fallback chain matches the shapes :func:`replay_events` and
    :func:`zicato.testing.fixtures.make_synthetic_events_jsonl` produce
    on both the proto-available and proto-not-available code paths.
    """
    out: list[dict[str, Any]] = []
    for evt in events:
        if isinstance(evt, dict):
            out.append(dict(evt))
            continue
        if hasattr(evt, "DESCRIPTOR"):
            # Local import so the proto path does not pay the import
            # cost when the caller is exclusively on the dict shape.
            from google.protobuf.json_format import MessageToDict

            out.append(MessageToDict(evt, preserving_proto_field_name=True))
            continue
        if hasattr(evt, "to_dict"):
            out.append(dict(evt.to_dict()))
            continue
        if dataclasses.is_dataclass(evt) and not isinstance(evt, type):
            out.append(dataclasses.asdict(evt))
            continue
        out.append({k: v for k, v in vars(evt).items() if not k.startswith("_")})
    return out


__all__ = [
    "replay_events",
    "events_to_dicts",
]
