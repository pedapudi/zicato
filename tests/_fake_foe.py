"""A deterministic stand-in for the ``foe`` binary, driven by a script.

Zicato's proposer runs as a Foe episode. Every test that exercises that
path needs a binary that speaks the host protocol
(``foe/docs/protocol.md``) without a model credential, a network, or a
Rust toolchain, and that reaches a chosen outcome on demand. This module
is that binary. It is invoked as an executable through
:func:`fake_foe_binary`, which writes a one-line launcher naming this
file.

Three command forms are accepted, the three the ``foe`` Python package
issues:

    fake-foe --config FILE --host --log-dir DIR    run one episode
    fake-foe plan --json --config FILE             print the fingerprint
    fake-foe view DIR --serve                      print a URL and wait

What the episode does is decided by the *scripted transport* the
document's ``model`` block names under the ``exec`` provider, which
``foe/docs/models.md`` specifies: an executable that reads one request
object on standard input and writes ``model/chunk`` lines. The stand-in
implements that provider alone, so a test writes the model's turns and
this file writes everything around them.

Four built-in tools are implemented against the document's ``grants``,
because the proposer's edit loop needs them to really move bytes:
``read`` and ``grep`` over the read roots, ``edit`` over the write roots,
and ``block``, which ends the episode blocked. A path outside every
granted root fails the call the way the runtime's kernel-enforced
confinement would, so a test can hold the snapshot unwritable. Host
tools are routed over ``host/tool-call`` and every other built-in answers
with a fixed value.

The stand-in is not the runtime and does not pretend to be: it enforces
grants by prefix rather than by kernel ruleset, it counts only the
``model_calls`` budget dimension, and it spawns no children. What it does
guarantee is the protocol, the four outcomes, and the log shape the
``foe`` package parses.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, TextIO

#: The versions the pinned ``foe`` package pairs with. Stating anything
#: else is how a test drives ``foe.CompatibilityError``.
LOG_VERSION = 3
RUNTIME_VERSION = "0.2.0"

#: The build identity a handle reads off ``episode/start``.
BUILD_ID = "sha256:" + "f0e" * 21 + "f"

_BUILTIN_SCHEMAS: dict[str, dict[str, Any]] = {
    name: {"name": name, "description": f"built-in {name}", "parameters": {"type": "object"}}
    for name in ("read", "grep", "edit", "bash", "block", "spawn", "wait", "steer")
}


class Cancelled(Exception):
    """The host wrote a ``cancel`` line."""


class Log:
    """The episode log: a file, and the same lines echoed to stdout."""

    def __init__(self, log_dir: Path, log_version: int | None, die_after: int | None) -> None:
        log_dir.mkdir(parents=True, exist_ok=True)
        self.file: TextIO = (log_dir / "episode.jsonl").open("w", encoding="utf-8")
        self.seq = 0
        self.log_version = log_version
        self.die_after = die_after
        self.events: list[dict[str, Any]] = []

    def emit(self, type_: str, data: dict[str, Any]) -> int:
        event: dict[str, Any] = {"seq": self.seq, "time": int(time.time() * 1000)}
        if self.seq == 0 and self.log_version is not None:
            event["version"] = self.log_version
        event["type"] = type_
        event["data"] = data
        line = json.dumps(event, ensure_ascii=False)
        self.file.write(line + "\n")
        self.file.flush()
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
        self.events.append(event)
        self.seq += 1
        if self.die_after is not None and self.seq > self.die_after:
            # Injected crash: the process dies with the log mid-flight and
            # no `episode/end`, which is the shape the `foe` package turns
            # into a `Failed` outcome naming the exit code.
            sys.stdout.flush()
            raise SystemExit(70)
        return int(event["seq"])


class Grants:
    """The document's read and write roots, checked by resolved prefix."""

    def __init__(self, grants: dict[str, Any]) -> None:
        self.read = [Path(p).resolve() for p in grants.get("read") or []]
        self.write = [Path(p).resolve() for p in grants.get("write") or []]

    def resolve(self, path: str, roots: list[Path], effect: str) -> Path:
        target = Path(path)
        if not target.is_absolute():
            target = (roots[0] if roots else Path.cwd()) / target
        resolved = Path(target).resolve()
        for root in roots:
            if resolved == root or root in resolved.parents:
                return resolved
        raise PermissionError(f"{effect} denied: {resolved} is outside every granted root")


class Episode:
    """One run of one document, to one of the four outcomes."""

    def __init__(
        self,
        config: dict[str, Any],
        log_dir: Path,
        options: Options,
    ) -> None:
        self.config = config
        self.runtime_version = options.runtime_version
        self.log = Log(log_dir, options.log_version, options.die_after)
        self.model: dict[str, Any] | None = config.get("model")
        self.host_tools: dict[str, Any] = config.get("host_tools") or {}
        self.grants = Grants(config.get("grants") or {})
        self.held: list[dict[str, Any]] = []
        self.pending_inbox: list[int] = []
        self.consumed_before: set[int] = set()

    # -- the host's side of the pipe -----------------------------------

    def read_line(self) -> dict[str, Any]:
        """The next host line, handling inbox items and cancel inline."""
        while True:
            raw = sys.stdin.readline()
            if not raw:
                self.end({"kind": "failed", "error": "host closed standard input"})
                sys.exit(1)
            try:
                obj: dict[str, Any] = json.loads(raw)
            except ValueError:
                self.end({"kind": "failed", "error": "protocol: host line is not JSON"})
                sys.exit(1)
            kind = obj.get("type")
            if kind == "inbox/item":
                self.held.append(
                    {
                        "source": obj["source"],
                        "content": obj["content"],
                        "from": obj.get("from"),
                        "message_id": obj.get("message_id"),
                    }
                )
                continue
            if kind == "cancel":
                raise Cancelled
            if kind not in ("model/chunk", "tool/result"):
                self.end({"kind": "failed", "error": f"protocol: unknown host line {kind!r}"})
                sys.exit(1)
            return obj

    # -- the projection the model sees ---------------------------------

    def messages(self, consumed: list[int]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        seen = set(consumed) | self.consumed_before
        for event in self.log.events:
            data = event["data"]
            if event["type"] == "inbox/item" and event["seq"] in seen:
                if out and out[-1]["role"] == "user":
                    out[-1]["content"].extend(data["content"])
                else:
                    out.append({"role": "user", "content": list(data["content"])})
            elif event["type"] == "assistant/message":
                out.append(
                    {"role": "assistant", "text": data["text"], "tool_calls": data["tool_calls"]}
                )
            elif event["type"] == "tool/result":
                out.append(
                    {
                        "role": "tool",
                        "call_id": data["call_id"],
                        "name": data["name"],
                        "rendered": data["rendered"],
                        "is_error": data["is_error"],
                    }
                )
        return out

    # -- the loop ------------------------------------------------------

    def end(self, outcome: dict[str, Any]) -> None:
        self.log.emit("episode/end", {"outcome": outcome})

    def run(self) -> int:
        config = self.config
        contract = {k: v for k, v in config.items() if k != "task"}
        self.log.emit(
            "episode/start",
            {
                "id": "ep_zicato_fake",
                "parent_id": None,
                "fork_origin": None,
                "team_id": None,
                "contract": contract,
                "contract_fingerprint": contract_fingerprint_of(contract),
                "task": config["task"],
                "runtime": {"version": self.runtime_version, "build": BUILD_ID},
                "sandbox": {
                    "mode": (config.get("sandbox") or {}).get("mode", "best-effort"),
                    "landlock_abi": 0,
                },
            },
        )
        self.pending_inbox.append(
            self.log.emit(
                "inbox/item",
                {
                    "source": "task",
                    "content": [{"type": "text", "text": config["task"]}],
                    "from": None,
                    "message_id": None,
                },
            )
        )
        header, header_seq = self._write_header()
        done_when = config.get("done_when") or {}
        budget_calls = int(config["budget"]["model_calls"])
        # `done_when.retries` bounds how many times findings are fed back
        # before the episode ends blocked, which is what makes an
        # unsatisfiable verifier a block rather than a spent budget.
        retries_left = int(done_when.get("retries", 2)) if "verify" in done_when else 0
        calls = 0
        step = 0
        try:
            while True:
                step += 1
                calls += 1
                request_id = f"rq_{step:02d}"
                for item in self.held:
                    self.pending_inbox.append(self.log.emit("inbox/item", item))
                self.held.clear()
                consumed = list(self.pending_inbox)
                self.pending_inbox.clear()
                messages = self.messages(consumed)
                self.consumed_before.update(consumed)
                max_output = config["budget"].get("output_tokens")
                self.log.emit(
                    "model/request",
                    {
                        "step": step,
                        "attempt": 1,
                        "request_id": request_id,
                        "header_seq": header_seq,
                        "consumed": consumed,
                        "messages": messages,
                        "max_output_tokens": max_output,
                    },
                )
                chunks = self._chunks(request_id, header, messages, max_output)
                text, made, stop, usage, error = self._collect(step, request_id, chunks)
                if error is not None:
                    self.end({"kind": "failed", "error": error})
                    return 1
                self.log.emit(
                    "assistant/message",
                    {
                        "step": step,
                        "request_id": request_id,
                        "text": text,
                        "tool_calls": made,
                        "stop": stop,
                        "usage": usage,
                        "interrupted": False,
                    },
                )
                blocked, returned = self._execute(step, made)
                if blocked is not None:
                    self.end(blocked)
                    return 2
                if returned is not None or (not made and "returns" not in done_when):
                    value: Any = returned[0] if returned is not None else text
                    if "verify" in done_when:
                        findings = self._verify(step, str(done_when["verify"]), value)
                        if findings:
                            if retries_left <= 0:
                                self.end(
                                    {
                                        "kind": "blocked",
                                        "code": "verification-unsatisfiable",
                                        "message": "\n".join(findings),
                                    }
                                )
                                return 2
                            retries_left -= 1
                            self.held.append(
                                {
                                    "source": "verify",
                                    "content": [{"type": "text", "text": "\n".join(findings)}],
                                    "from": None,
                                    "message_id": None,
                                }
                            )
                            if calls >= budget_calls:
                                self.end({"kind": "exhausted", "limit": "model_calls"})
                                return 3
                            continue
                    self.end({"kind": "completed", "value": value})
                    return 0
                if calls >= budget_calls:
                    self.end({"kind": "exhausted", "limit": "model_calls"})
                    return 3
        except Cancelled:
            self.end({"kind": "failed", "error": "cancelled"})
            return 1

    def _write_header(self) -> tuple[dict[str, Any], int]:
        config = self.config
        schemas: list[dict[str, Any]] = []
        sections = config["instructions"]
        instructions = "\n\n".join(sections[k] for k in sorted(sections))
        for name in config["tools"]:
            if name in self.host_tools:
                spec = self.host_tools[name]
                schemas.append(
                    {"name": name, "description": spec["description"], "parameters": spec["params"]}
                )
                if spec.get("instruction"):
                    instructions += "\n\n" + spec["instruction"]
            elif name in (config.get("tool_defs") or {}):
                schemas.append(
                    {
                        "name": name,
                        "description": config["tool_defs"][name]["description"],
                        "parameters": {"type": "object", "properties": {"args": {"type": "array"}}},
                    }
                )
            else:
                schemas.append(_BUILTIN_SCHEMAS[name])
        done_when = config.get("done_when") or {}
        if "returns" in done_when:
            # The runtime nests the declared schema under a required
            # `value`; a stand-in advertising the bare schema would teach
            # its callers a call the real binary rejects.
            schemas.append(
                {
                    "name": "return",
                    "description": "Return the result.",
                    "parameters": {
                        "type": "object",
                        "properties": {"value": done_when["returns"]},
                        "required": ["value"],
                    },
                }
            )
        route = {"provider": "host", "model": "host"}
        if self.model is not None:
            route = {"provider": self.model["provider"], "model": self.model["model"]}
        header = {"reason": "initial", "system": instructions, "tools": schemas, "model": route}
        return header, self.log.emit("request/header", header)

    def _chunks(
        self,
        request_id: str,
        header: dict[str, Any],
        messages: list[dict[str, Any]],
        max_output: int | None,
    ) -> list[dict[str, Any]]:
        """One request's chunks, from the `exec` transport or the host."""
        if self.model is None:
            return list(self._host_chunks(request_id))
        model = self.model
        if model.get("provider") != "exec":
            named = model.get("provider")
            return [
                {
                    "kind": "error",
                    "message": (
                        "model.provider: this stand-in implements exec alone, " f"not {named!r}"
                    ),
                    "retryable": False,
                }
            ]
        fixed = ("provider", "model", "max_output_tokens", "exec")
        request = {
            "type": "model/request",
            "request_id": request_id,
            "model": model["model"],
            "system": header["system"],
            "tools": header["tools"],
            "messages": messages,
            "max_output_tokens": max_output,
            "options": {k: v for k, v in model.items() if k not in fixed},
        }
        completed = subprocess.run(
            [model["exec"], model["model"]],
            input=json.dumps(request) + "\n",
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            return [
                {
                    "kind": "error",
                    "message": (
                        f"model.exec: {model['exec']} exited with code "
                        f"{completed.returncode}: {completed.stderr.strip()}"
                    ),
                    "retryable": False,
                }
            ]
        return [json.loads(line)["chunk"] for line in completed.stdout.splitlines() if line.strip()]

    def _host_chunks(self, request_id: str) -> Any:
        while True:
            line = self.read_line()
            if line.get("type") != "model/chunk" or line.get("request_id") != request_id:
                self.end({"kind": "failed", "error": "protocol: chunk for an unknown request"})
                sys.exit(1)
            yield line["chunk"]

    def _collect(
        self, step: int, request_id: str, chunks: Any
    ) -> tuple[str, list[dict[str, Any]], str, dict[str, int], str | None]:
        text = ""
        partial: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        for chunk in chunks:
            self.log.emit(
                "assistant/chunk", {"step": step, "request_id": request_id, "chunk": chunk}
            )
            kind = chunk["kind"]
            if kind == "text":
                text += chunk["delta"]
            elif kind == "tool_call_start":
                partial[chunk["id"]] = {"id": chunk["id"], "name": chunk["name"], "buffer": ""}
                order.append(chunk["id"])
            elif kind == "tool_call_delta":
                partial[chunk["id"]]["buffer"] += chunk["delta"]
            elif kind == "done":
                calls = [
                    {
                        "id": cid,
                        "name": partial[cid]["name"],
                        "args": json.loads(partial[cid]["buffer"] or "{}"),
                    }
                    for cid in order
                ]
                return text, calls, chunk["stop"], chunk["usage"], None
            elif kind == "error":
                return text, [], "end", _NO_USAGE, chunk["message"]
        return text, [], "end", _NO_USAGE, "model transport ended without a done or error chunk"

    def _execute(
        self, step: int, calls: list[dict[str, Any]]
    ) -> tuple[dict[str, Any] | None, tuple[Any] | None]:
        blocked: dict[str, Any] | None = None
        returned: tuple[Any] | None = None
        for call in calls:
            name = call["name"]
            if name in self.host_tools:
                self._host_call(step, call)
            elif name == "block":
                blocked = {
                    "kind": "blocked",
                    "code": call["args"]["code"],
                    "message": call["args"].get("message", ""),
                }
                self._result(step, call, {"ok": True}, "blocked", False)
            elif name == "return":
                returned = (call["args"]["value"],)
                self._result(step, call, {"ok": True}, "returned", False)
            else:
                value, rendered, is_error = self._builtin(name, call["args"])
                self._result(step, call, value, rendered, is_error)
        return blocked, returned

    def _builtin(self, name: str, args: dict[str, Any]) -> tuple[Any, str, bool]:
        """Run one built-in tool against the document's grants."""
        try:
            if name == "read":
                path = self.grants.resolve(str(args["path"]), self.grants.read, "read")
                text = path.read_text(encoding="utf-8")
                return {"path": str(path), "text": text}, text, False
            if name == "grep":
                pattern = re.compile(str(args["pattern"]))
                hits = [
                    f"{p}:{i + 1}:{line}"
                    for root in self.grants.read
                    for p in sorted(root.rglob("*"))
                    if p.is_file()
                    for i, line in enumerate(_lines(p))
                    if pattern.search(line)
                ]
                return {"hits": hits}, f"{len(hits)} hits", False
            if name == "edit":
                path = self.grants.resolve(str(args["path"]), self.grants.write, "write")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(str(args["content"]), encoding="utf-8")
                return {"path": str(path), "written": True}, f"wrote {path}", False
        except (OSError, PermissionError, KeyError, re.error) as exc:
            message = f"{type(exc).__name__}: {exc}"
            return {"error": message}, message, True
        return {"ok": True}, "ok", False

    def _result(
        self, step: int, call: dict[str, Any], value: Any, rendered: str, err: bool
    ) -> None:
        self.log.emit(
            "tool/result",
            {
                "step": step,
                "call_id": call["id"],
                "name": call["name"],
                "value": value,
                "rendered": rendered,
                "is_error": err,
                "spill": None,
                "duration_ms": 0,
                "synthetic": False,
            },
        )

    def _host_call(self, step: int, call: dict[str, Any]) -> dict[str, Any]:
        self.log.emit(
            "host/tool-call",
            {"step": step, "call_id": call["id"], "name": call["name"], "args": call["args"]},
        )
        while True:
            line = self.read_line()
            if line.get("type") == "tool/result" and line.get("call_id") == call["id"]:
                break
            self.end({"kind": "failed", "error": "protocol: result for an unknown call"})
            sys.exit(1)
        value = line["value"]
        rendered = line.get("rendered")
        if rendered is None:
            rendered = json.dumps(value, separators=(",", ":"))
        self._result(step, call, value, rendered, bool(line.get("is_error", False)))
        return line

    def _verify(self, step: int, verifier: str, candidate: Any) -> list[str]:
        spec = self.host_tools.get(verifier)
        if spec is None:
            return []
        properties = list((spec["params"].get("properties") or {}).keys())
        arg = properties[0] if properties else "candidate"
        line = self._host_call(
            step, {"id": f"tc_verify_{step}", "name": verifier, "args": {arg: candidate}}
        )
        value = line["value"]
        return [str(f) for f in value] if isinstance(value, list) else []


_NO_USAGE = {"input": 0, "output": 0, "cache_read": 0}


def _lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []


def contract_fingerprint_of(contract: dict[str, Any]) -> str:
    """A fingerprint with the real one's exclusions.

    ``foe/docs/design.md`` excludes the model route and the resolved
    permission paths and includes the permission *shape*, so this reduces
    each grant list to its length and drops the ``model`` and ``sandbox``
    keys. That is what makes a test able to hold a reworded tool
    description moving the hash while a moved grant path does not.
    """
    hashed = {k: v for k, v in contract.items() if k not in ("model", "sandbox")}
    if "grants" in hashed:
        hashed["grants"] = {k: len(v) for k, v in hashed["grants"].items()}
    canonical = json.dumps(hashed, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


class Options:
    """The leading options a test bakes into its stand-in launcher.

    ``--log-version`` and ``--runtime-version`` state a version the ``foe``
    package does not pair with, which is how the compatibility refusal is
    driven. ``--die-after`` kills the process once that many events have
    been written, which is the injected crash.
    """

    def __init__(
        self, log_version: int | None, runtime_version: str, die_after: int | None
    ) -> None:
        self.log_version = log_version
        self.runtime_version = runtime_version
        self.die_after = die_after


def _take_options(argv: list[str]) -> tuple[list[str], Options]:
    log_version: int | None = LOG_VERSION
    runtime = RUNTIME_VERSION
    die_after: int | None = None
    while argv[:1] in (["--log-version"], ["--runtime-version"], ["--die-after"]):
        flag, value, argv = argv[0], argv[1], argv[2:]
        if flag == "--log-version":
            log_version = None if value == "none" else int(value)
        elif flag == "--runtime-version":
            runtime = value
        else:
            die_after = int(value)
    return argv, Options(log_version, runtime, die_after)


def main(argv: list[str]) -> int:
    argv, options = _take_options(argv)
    if argv[:1] == ["plan"]:
        config = json.loads(Path(argv[argv.index("--config") + 1]).read_text(encoding="utf-8"))
        contract = {k: v for k, v in config.items() if k != "task"}
        print(
            json.dumps(
                {"contract_fingerprint": contract_fingerprint_of(contract), "contract": contract}
            )
        )
        return 0
    if argv[:1] == ["view"] and "--serve" in argv:
        print("http://127.0.0.1:34567/", flush=True)
        sys.stdin.read()
        return 0
    if "--host" not in argv:
        print("fake foe: only the --host form runs an episode", file=sys.stderr)
        return 1
    config_path = Path(argv[argv.index("--config") + 1])
    log_dir = Path(argv[argv.index("--log-dir") + 1])
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if "task" not in config:
        print("task: required", file=sys.stderr)
        return 1
    return Episode(config, log_dir, options).run()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
