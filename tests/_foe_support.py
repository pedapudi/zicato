"""Builders for the Foe stand-in binary and the model turns it replays.

A test that exercises the proposer needs two executables: a binary for
``FoeProposerConfig.binary`` and, under it, the ``exec``-provider
transport that answers each model request. Both are written here as
one-line launchers over Python modules in this package, so a test names
a temporary directory and gets a pair of absolute paths back.

The transport replays a *script*: a list of turns, one per model request,
each naming the text and the tool calls that request produces. A test
therefore writes what the proposer model does and nothing else.
"""

from __future__ import annotations

import json
import stat
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent


def _executable(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def fake_foe_binary(
    directory: Path,
    *,
    log_version: int | str | None = None,
    runtime_version: str | None = None,
    die_after: int | None = None,
    name: str = "foe",
) -> Path:
    """Write an executable Foe stand-in into ``directory`` and return it.

    The options are the stand-in's own: ``log_version`` and
    ``runtime_version`` make it state a version the ``foe`` package
    refuses, and ``die_after`` makes it exit once that many log events
    have been written, without an ``episode/end``.
    """
    directory.mkdir(parents=True, exist_ok=True)
    leading: list[str] = []
    if log_version is not None:
        leading += ["--log-version", str(log_version)]
    if runtime_version is not None:
        leading += ["--runtime-version", runtime_version]
    if die_after is not None:
        leading += ["--die-after", str(die_after)]
    body = (
        f"#!{sys.executable}\n"
        "import sys\n"
        f"sys.path.insert(0, {str(_HERE.parent)!r})\n"
        "from tests._fake_foe import main\n"
        f"sys.exit(main({leading!r} + sys.argv[1:]))\n"
    )
    return _executable(directory / name, body)


def text_turn(text: str) -> dict[str, Any]:
    """A turn with no tool calls: the model answers in prose."""
    return {"text": text, "calls": []}


def call_turn(*calls: tuple[str, dict[str, Any]], text: str = "") -> dict[str, Any]:
    """A turn issuing the named tool calls with the given arguments."""
    return {"text": text, "calls": [{"name": n, "args": a} for n, a in calls]}


def return_turn(value: Any, *, text: str = "") -> dict[str, Any]:
    """A turn calling the synthesized ``return`` tool.

    The runtime nests a ``done_when.returns`` value under a required
    ``value`` property, and nothing between the model and the runtime adds
    that wrapper, so it is written here.
    """
    return call_turn(("return", {"value": value}), text=text)


def block_turn(code: str, message: str = "") -> dict[str, Any]:
    """A turn calling the built-in ``block`` tool with a closed-set code."""
    return call_turn(("block", {"code": code, "message": message}))


def error_turn(message: str, *, retryable: bool = False) -> dict[str, Any]:
    """A turn the transport reports as a failed model request."""
    return {"error": message, "retryable": retryable}


def scripted_transport(
    directory: Path, turns: Sequence[dict[str, Any]], *, name: str = "turns"
) -> Path:
    """Write an ``exec``-provider transport replaying ``turns`` in order.

    Requests past the end of the script replay the last turn, so a script
    of one turn answers an episode of any length. That keeps a test's
    script as short as the behaviour it is pinning.
    """
    directory.mkdir(parents=True, exist_ok=True)
    script_path = directory / f"{name}.json"
    script_path.write_text(json.dumps(list(turns)), encoding="utf-8")
    body = (
        f"#!{sys.executable}\n"
        "import sys\n"
        f"sys.path.insert(0, {str(_HERE.parent)!r})\n"
        "from tests._foe_transport import main\n"
        f"sys.exit(main({str(script_path)!r}))\n"
    )
    return _executable(directory / f"{name}-transport", body)


def stand_in_transport(directory: Path, *, name: str = "stand-in") -> Path:
    """Write the mechanical proposer transport (:mod:`tests._foe_stand_in_proposer`).

    Unlike :func:`scripted_transport` this one carries no script: it reads
    the working copy the task names and proposes against whatever it
    finds, so a workspace can declare it once and every round of every
    epoch gets a well-formed candidate.
    """
    directory.mkdir(parents=True, exist_ok=True)
    body = (
        f"#!{sys.executable}\n"
        "import sys\n"
        f"sys.path.insert(0, {str(_HERE.parent)!r})\n"
        "from tests._foe_stand_in_proposer import main\n"
        "sys.exit(main())\n"
    )
    return _executable(directory / f"{name}-transport", body)


def stand_in_proposer_block(
    directory: Path,
    *,
    model_calls: int = 6,
    idea: str = "",
    break_first: int = 0,
    refuse: str = "",
    predict: str = "",
    hypotheses: Mapping[str, Mapping[str, str]] | None = None,
    contents: Mapping[str, Mapping[str, str]] | None = None,
) -> dict[str, Any]:
    """The ``proposer`` block a loop-suite workspace declares.

    Every fixture workspace that runs a round needs one, because a
    workspace that declares no proposal runtime cannot open a round. The
    block written here names the stand-in binary and the mechanical
    transport above, both placed under ``directory``, so the workspace
    proposes for real — one episode per candidate, over the host protocol
    — without a credential, a network, or a model.

    ``directory`` must outlive the workspace: the absolute paths written
    here are read back by the tournament workers, which are separate
    interpreters and see nothing this process patched in memory.

    ``idea`` fixes the core idea every candidate states, which is how a
    test makes a field's siblings duplicate each other on purpose;
    ``break_first`` is how many opening turns write an edit the verifier
    must reject, which is how a test drives the repair loop and, when it
    exceeds the retry budget, the block that ends an unsatisfiable
    episode, and ``refuse`` is a blocked code the episode reports instead
    of proposing at all. ``predict`` names the metric every hypothesis
    claims will
    move, which is how a test drives a hypothesis the round's validator
    refuses. ``hypotheses`` maps a candidate id to either of those two
    keys, for a field that wants them to differ by candidate rather than
    across the board. ``contents`` maps a candidate id to the literal
    bodies that
    candidate writes — ``{"v1": {"style_rules": "verbose-prose"}}`` —
    which is how a known-answer harness scripts the exact tree each
    candidate produces. All of them ride the model options, because that
    is the one part of the block Foe forwards to the transport.
    """
    options = {"exec": str(stand_in_transport(directory))}
    if idea:
        options["idea"] = idea
    if break_first:
        options["break_first"] = str(break_first)
    if refuse:
        options["refuse"] = refuse
    if predict:
        options["predict"] = predict
    if hypotheses:
        options["hypotheses"] = json.dumps({k: dict(v) for k, v in hypotheses.items()})
    if contents:
        options["contents"] = json.dumps({k: dict(v) for k, v in contents.items()})
    return {
        "binary": str(fake_foe_binary(directory)),
        "budget": {"model_calls": model_calls, "seconds": 300},
        "model": {"provider": "exec", "model": "stand-in", "options": options},
    }


def foe_environment(directory: Path) -> dict[str, str]:
    """A credential directory for a stand-in run, as a mapping to merge.

    The stand-in reads no credential, so this exists to keep a test from
    depending on the operator's real ``~/.config/foe`` tree.
    """
    home = directory / "foe-home"
    (home / "credentials").mkdir(parents=True, exist_ok=True)
    return {"HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config")}


def read_episode_log(log_dir: Path) -> list[dict[str, Any]]:
    """Every event of the episode the stand-in wrote under ``log_dir``."""
    text = (Path(log_dir) / "episode.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def request_texts(log_dir: Path) -> str:
    """Every byte the model was shown, as one string.

    The holdout-exclusion proof reads this: a board entry that reaches the
    model appears here, and one the redaction removed does not, whichever
    section of the request carried it.
    """
    parts: list[str] = []
    for event in read_episode_log(log_dir):
        if event["type"] in ("request/header", "model/request", "inbox/item"):
            parts.append(json.dumps(event["data"], ensure_ascii=False))
    return "\n".join(parts)


__all__ = [
    "block_turn",
    "call_turn",
    "error_turn",
    "fake_foe_binary",
    "foe_environment",
    "read_episode_log",
    "request_texts",
    "return_turn",
    "scripted_transport",
    "stand_in_proposer_block",
    "stand_in_transport",
    "text_turn",
]
