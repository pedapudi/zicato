#!/usr/bin/env python3
"""A stub JSONL peer speaking pi's RPC protocol, for hermetic transport tests.

``pi`` is a Node program that needs a Node runtime, a pinned npm install
and provider credentials. None of those belong in the default CI lane, but
the *transport* — strict-LF framing, correlated command responses, the
terminating-tool event sequence, session reuse across retries — is
zicato's code and must be covered there. So this script stands in for the
binary: :mod:`zicato.proposer.pi_agent` launches it exactly as it launches
pi, and it answers exactly as pi's ``docs/rpc.md`` says pi answers.

It also RECORDS its own launch — argv, environment, working directory, and
every prompt it received — as ``<ZICATO_PI_STUB_RECORD>/launch-<pid>.json``.
One file per launch is what lets a test assert the command that was
actually issued (rather than the one a builder function says it would
issue) *and* that a retry rode the same process rather than a fresh one.

Behaviour is scripted through the JSON file named by
``ZICATO_PI_STUB_SCRIPT``::

    {
      "model": "stub/model-1",
      "turns": [
        {"emit": {...experiment arguments...}},
        {"emit": null},                    # settle without calling the tool
        {"reject": "the agent is busy"}    # fail the prompt command itself
      ]
    }

Turns are consumed in order; running past the end settles silently, which
is the "produced nothing" case the engine's repair turn already targets.

Real-pi coverage lives in the opt-in ``pi`` marker lane, never here.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any


def _emit(payload: dict[str, Any]) -> None:
    """Write one record with LF framing, exactly as the protocol requires."""
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def _record(prompts: list[str]) -> None:
    """Rewrite this launch's record. Called at startup and after each prompt."""
    target = os.environ.get("ZICATO_PI_STUB_RECORD")
    if not target:
        return
    directory = Path(target)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"launch-{os.getpid()}.json").write_text(
        json.dumps(
            {
                "argv": sys.argv,
                "cwd": os.getcwd(),
                # What the process could see from its working directory —
                # where pi discovers AGENTS.md and project-local .pi/.
                "cwd_entries": sorted(os.listdir(".")),
                "env": dict(os.environ),
                "prompts": prompts,
            }
        ),
        encoding="utf-8",
    )


def _script() -> dict[str, Any]:
    path = os.environ.get("ZICATO_PI_STUB_SCRIPT")
    if not path:
        return {}
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _flag_value(name: str) -> str:
    if name in sys.argv:
        index = sys.argv.index(name)
        if index + 1 < len(sys.argv):
            return sys.argv[index + 1]
    return ""


def main() -> int:
    prompts: list[str] = []
    _record(prompts)
    script = _script()
    turns: list[dict[str, Any]] = list(script.get("turns") or [])
    # pi resolves the --model pattern to a concrete id; the stub echoes
    # back whatever the script pins, falling back to the flag it was given
    # so the collusion guard has something real to compare against.
    resolved_model = str(script.get("model") or _flag_value("--model"))

    for line in sys.stdin:
        line = line.rstrip("\n").rstrip("\r")
        if not line.strip():
            continue
        command = json.loads(line)
        kind = command.get("type")
        request_id = command.get("id")

        if kind == "get_state":
            _emit(
                {
                    "id": request_id,
                    "type": "response",
                    "command": "get_state",
                    "success": True,
                    "data": {"model": {"id": resolved_model}, "isStreaming": False},
                }
            )
            continue

        if kind != "prompt":
            _emit({"id": request_id, "type": "response", "command": kind, "success": True})
            continue

        prompts.append(str(command.get("message", "")))
        _record(prompts)
        turn = turns.pop(0) if turns else {"emit": None}
        if "reject" in turn:
            _emit(
                {
                    "id": request_id,
                    "type": "response",
                    "command": "prompt",
                    "success": False,
                    "error": turn["reject"],
                }
            )
            continue

        _emit({"id": request_id, "type": "response", "command": "prompt", "success": True})
        _emit({"type": "agent_start"})
        emitted = turn.get("emit")
        if emitted is not None:
            _emit(
                {
                    "type": "tool_execution_start",
                    "toolCallId": f"call-{request_id}",
                    "toolName": "propose_experiment",
                    "args": emitted,
                }
            )
            _emit(
                {
                    "type": "tool_execution_end",
                    "toolCallId": f"call-{request_id}",
                    "toolName": "propose_experiment",
                    "result": {"content": [{"type": "text", "text": "Experiment recorded."}]},
                    "isError": False,
                }
            )
        _emit({"type": "agent_settled"})

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
