"""The ``exec``-provider transport that replays a written script.

``foe/docs/models.md`` makes a transport executable a provider: the
runtime writes one request object on its standard input and reads
``model/chunk`` lines back. This module is such an executable, and what
it writes is decided by a JSON script rather than by a model.

Which turn a request gets is decided by how many requests the episode
has already made, counted from the ``messages`` the request carries: one
assistant message per completed request. That derivation needs no state
between invocations, so two episodes replaying one script cannot
interfere, and a request past the script's end replays its last turn.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def chunks_for(turn: dict[str, Any]) -> list[dict[str, Any]]:
    """The chunk sequence one scripted turn writes."""
    if "error" in turn:
        return [
            {
                "kind": "error",
                "message": str(turn["error"]),
                "retryable": bool(turn.get("retryable", False)),
            }
        ]
    out: list[dict[str, Any]] = []
    if turn.get("text"):
        out.append({"kind": "text", "delta": str(turn["text"])})
    for index, call in enumerate(turn.get("calls") or []):
        call_id = f"tc_{index}"
        out.append({"kind": "tool_call_start", "id": call_id, "name": str(call["name"])})
        out.append(
            {
                "kind": "tool_call_delta",
                "id": call_id,
                "delta": json.dumps(call.get("args") or {}),
            }
        )
        out.append({"kind": "tool_call_end", "id": call_id})
    out.append({"kind": "done", "stop": "end", "usage": {"input": 0, "output": 0, "cache_read": 0}})
    return out


def turn_index(request: dict[str, Any]) -> int:
    """How many requests this episode has already completed."""
    return sum(1 for m in request.get("messages") or [] if m.get("role") == "assistant")


def main(script_path: str) -> int:
    request = json.loads(sys.stdin.readline() or "{}")
    turns = json.loads(Path(script_path).read_text(encoding="utf-8"))
    if not turns:
        turns = [{"text": "the script is empty", "calls": []}]
    turn = turns[min(turn_index(request), len(turns) - 1)]
    for chunk in chunks_for(turn):
        sys.stdout.write(json.dumps({"chunk": chunk}) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":  # pragma: no cover - invoked as a launcher
    sys.exit(main(sys.argv[1]))
