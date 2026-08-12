"""A stand-in agent binary that speaks target 4's rpc protocol. NO model.

The real system under test is an external coding agent; CI has none, and a
live agentic run is slow, stochastic, and operator-gated. This module is the
hermetic double: it accepts the same argv, speaks the same newline-delimited
JSON, reads its configuration from the same ``PI_CODING_AGENT_DIR``, and
edits files in the same working tree — so the driver contract is exercised
end to end with a real subprocess and no model anywhere.

Run it as the binary by pointing the driver's ``ZICATO_TARGET_4_AGENT_BIN``
at an interpreter invocation::

    ZICATO_TARGET_4_AGENT_BIN="$(which python) -m zicato_examples.target_4_agent_config.stub_agent"

What it does is scripted, not decided: :data:`STUB_PLAN_ENV` carries a JSON
plan of turns to emit, files to write, and a final output to return. With no
plan it acknowledges the request and changes nothing. Either way it appends
``config-fingerprint: <digest>`` to its final output, digested from the
config package it was pointed at — which is how a test proves the run
mounted the SNAPSHOT's package and not the working tree's.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from zicato_examples.target_4_agent_config.driver import (
    AGENT_CONFIG_DIR_ENV,
    config_fingerprint,
)

#: JSON plan controlling this invocation. Keys, all optional:
#:
#: * ``turns`` — list of ``{"role": ..., "content": ...}`` to emit before
#:   the result.
#: * ``writes`` — ``{relative path: content}`` written into the cwd.
#: * ``final`` — the final output text (default: an acknowledgement).
#: * ``sleep`` — seconds to stall before replying; drives the wall-clock
#:   budget test.
STUB_PLAN_ENV = "ZICATO_TARGET_4_STUB_PLAN"

#: Reported by ``--version``. A fixed string: the point of the probe is that
#: SOMETHING is recorded per run, and a pinned value keeps CI deterministic.
STUB_VERSION = "zicato-target4-stub 0.1.0"

#: Prefix of the line the stub appends to every final output.
FINGERPRINT_PREFIX = "config-fingerprint: "


def _emit(event: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(event, sort_keys=True) + "\n")
    sys.stdout.flush()


def _plan() -> dict[str, Any]:
    raw = os.environ.get(STUB_PLAN_ENV, "") or "{}"
    try:
        plan = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return plan if isinstance(plan, dict) else {}


def _apply_writes(writes: dict[str, Any], cwd: Path) -> None:
    """Write the planned files, refusing to escape the working tree."""
    root = cwd.resolve()
    for relative, content in writes.items():
        target = (root / str(relative)).resolve()
        if not target.is_relative_to(root):
            raise ValueError(f"stub write escapes the working tree: {relative!r}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(content), encoding="utf-8")


def main(argv: list[str]) -> int:
    """Serve exactly one rpc request, then exit."""
    if "--version" in argv:
        print(STUB_VERSION)
        return 0

    line = sys.stdin.readline()
    if not line.strip():
        return 1
    request = json.loads(line)
    prompt = str(request.get("input", ""))
    cwd = Path(str(request.get("cwd", "") or os.getcwd()))

    plan = _plan()
    stall = float(plan.get("sleep", 0.0) or 0.0)
    if stall > 0:
        time.sleep(stall)

    for turn in plan.get("turns", []) or []:
        if isinstance(turn, dict):
            _emit(
                {
                    "type": "turn",
                    "role": str(turn.get("role", "assistant")),
                    "content": str(turn.get("content", "")),
                }
            )

    writes = plan.get("writes") or {}
    if isinstance(writes, dict):
        _apply_writes(writes, cwd)

    fingerprint = config_fingerprint(Path(os.environ.get(AGENT_CONFIG_DIR_ENV, ".")))
    final = str(plan.get("final", f"acknowledged: {prompt}"))
    _emit({"type": "result", "final_output": f"{final}\n{FINGERPRINT_PREFIX}{fingerprint}"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
