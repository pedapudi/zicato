"""A model stand-in that proposes: it edits the copy and returns a hypothesis.

The suites that drive the evolve loop are about the tournament, the
lineage and the gate, not about how a candidate was invented. They still
need a real proposal episode, because the proposer is now a Foe episode
and a round that cannot open one is a round that cannot run. This module
is the ``exec``-provider transport that turns those episodes into
proposals without a model, a credential or a network.

What it does is what the charter asks of a proposer, mechanically: it
enumerates the mutation points of the working copy the task names, picks
the first point it can rewrite, changes one string literal inside it, and
returns a hypothesis naming that point. It is a proposer that is always
right about the rules and never has an idea.

The change it makes is a tag: the literal's body keeps its text and gains
``[<candidate id>]``, replacing any tag the parent already carried. That
makes the edit deterministic (the same candidate always produces the same
tree), distinct per candidate (a field of challengers does not collapse
into one diversity soft-reject), and bounded across rounds (a tag replaces
a tag rather than accumulating).

:mod:`tests._foe_transport` is the sibling of this module: it replays a
*written* script, and is what a test that pins one specific episode
uses. This one writes its own script from the tree in front of it, and is
what a suite that only needs candidates uses.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

#: How the task names the tree the episode may write. Produced by
#: :func:`zicato.proposer.foe_request.render_episode_block`, which is the
#: only place the path is stated, so a stand-in that cannot find this line
#: fails loudly rather than proposing against the wrong tree.
_READ_ROOT = re.compile(r"^Read-only parent snapshot: (.+)$", re.MULTILINE)
_WRITE_ROOT = re.compile(r"^Your writable working copy: (.+)$", re.MULTILINE)
_CANDIDATE = re.compile(
    r"^You are producing candidate (\S+?)(?: \(slate slot (\d+)\))?(?: from \S+)?\.$",
    re.MULTILINE,
)

#: The literal forms a rewrite may target, longest delimiter first so a
#: triple-quoted body is not read as an empty single-quoted one.
_LITERALS = (
    re.compile(r'"""(.*?)"""', re.DOTALL),
    re.compile(r"'''(.*?)'''", re.DOTALL),
    re.compile(r'"([^"\n]*)"'),
    re.compile(r"'([^'\n]*)'"),
)

#: The tag this stand-in leaves behind, and recognizes as its own so a
#: second round replaces it instead of nesting inside it.
_TAG = re.compile(r" \[[^\[\]\n]*\]\Z")


def task_of(request: dict[str, Any]) -> str:
    """Every user-authored line of the request, as one string.

    The task arrives as the first user message and the verifier's
    findings arrive as later ones; both are read, because a retry turn
    still has to find the working copy.
    """
    parts: list[str] = []
    for message in request.get("messages") or []:
        if message.get("role") != "user":
            continue
        for item in message.get("content") or []:
            if item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
    return "\n".join(parts)


def tagged(body: str, tag: str) -> str:
    """``body`` carrying exactly ``tag``, replacing one it already carries."""
    return f"{_TAG.sub('', body)} [{tag}]"


def rewrite(content: str, tag: str, body: str | None = None) -> str | None:
    """``content`` with its first string literal rewritten, or ``None``.

    ``body`` replaces the literal outright; absent one, the literal keeps
    its text and gains the candidate's tag. ``None`` says this mutation
    point holds no literal to rewrite, which is a fact about the point
    rather than a failure: the caller moves on to the next point.
    """
    for pattern in _LITERALS:
        match = pattern.search(content)
        if match is None:
            continue
        replacement = body if body is not None else tagged(match.group(1), tag)
        return content[: match.start(1)] + replacement + content[match.end(1) :]
    return None


def edited_file(point: Any, tag: str, body: str | None = None) -> str | None:
    """The whole new text of the file one mutation point lives in.

    The point's line range is replaced as a unit, which is the same unit
    the projection reads a change back as, so an accepted edit cannot
    straddle two points.
    """
    replacement = rewrite(point.content, tag, body)
    if replacement is None:
        return None
    lines = Path(point.file).read_text(encoding="utf-8").splitlines(keepends=True)
    return "".join([*lines[: point.line_start - 1], replacement, *lines[point.line_end :]])


def _broken_edit(read_root: Path, write_root: Path) -> tuple[Path, str] | None:
    """An edit inside a declared point that leaves the file unparseable.

    A dangling ``if`` written over the first Python point's line range.
    That is genuinely invalid source in place, so the copy stops
    declaring the point it was written into and the projection refuses
    it — which is what the verifier reports back as findings.
    """
    from zicato.mutation.enumerator import enumerate_mutations

    for point in enumerate_mutations([read_root]):
        if point.file.suffix != ".py":
            continue
        lines = point.file.read_text(encoding="utf-8").splitlines(keepends=True)
        text = "".join([*lines[: point.line_start - 1], "    if\n", *lines[point.line_end :]])
        return write_root / point.file.relative_to(read_root), text
    return None


def proposal(
    read_root: Path, write_root: Path, tag: str, scripted: dict[str, str] | None = None
) -> tuple[Path, str, str] | None:
    """The edit this stand-in makes, and the point it makes it on.

    Read off the PARENT snapshot rather than the working copy, so the
    content written is a pure function of the tree the round is patching.
    A repair turn therefore rewrites the file whole from the parent
    instead of trying to mend whatever the previous turn left behind —
    which is what lets a broken first turn be repaired at all, since a
    file that no longer parses declares no mutation points to read.

    ``scripted`` names, per mutation point, the literal body this
    candidate writes. A point it names is written verbatim; with no
    script the first point that holds a literal takes the tag.
    """
    from zicato.mutation.enumerator import enumerate_mutations

    for point in enumerate_mutations([read_root]):
        if scripted is not None and point.id not in scripted:
            continue
        text = edited_file(point, tag, scripted.get(point.id) if scripted else None)
        if text is not None:
            return write_root / point.file.relative_to(read_root), text, point.id
    return None


def hypothesis_for(
    point_id: str, tag: str, idea: str | None = None, predict: str | None = None
) -> dict[str, Any]:
    """A schema-valid hypothesis about the one edit that was made.

    The candidate is named in the core idea as well as in the tag,
    because the field's diversity rule reads the targeted point set and
    the core idea together: siblings that touch one point and say the
    same thing about it are one experiment, and this stand-in must field
    as many challengers as it is asked for. A workspace that wants the
    opposite — siblings that DO collapse — fixes ``idea`` and gets it.
    """
    return {
        "core_idea": idea or f"Tag the {point_id} literal for candidate {tag}.",
        "modulating": [point_id],
        "why": (
            "A mechanical stand-in proposes a minimal, well-formed change so "
            "the loop under test has a real candidate to run."
        ),
        "expected_pass_rate_delta": "+0.0 to +0.1",
        "risks": f"The change is cosmetic; candidate {tag} is not expected to move the board.",
        # A movement is required, and this one is honest: a tagged literal
        # is not an idea about off-topic drift, so nothing is predicted to
        # move. The vocabulary has a word for that.
        "expected_metric_movements": [
            {
                "metric_name": predict or "drift:off_topic",
                "direction": "neutral",
                "magnitude": "small",
            }
        ],
    }


def call(name: str, args: dict[str, Any]) -> dict[str, Any]:
    return {"name": name, "args": args}


def steering(request: dict[str, Any]) -> dict[str, Any]:
    """What the workspace asked this stand-in to do, if anything.

    A workspace steers the stand-in through its ``proposer.model.options``
    (see :func:`tests._foe_support.stand_in_proposer_block`), which the
    runtime forwards to the transport as the request's ``options``. Five
    keys are read:

    * ``idea`` fixes the core idea every candidate states, which is how a
      field's siblings are made to duplicate each other on purpose.
    * ``predict`` names the metric every hypothesis claims will move,
      which drives a hypothesis the round's validator refuses.
    * ``hypotheses`` maps a candidate id to either of those two keys, for
      a field whose slots must differ.
    * ``break_first`` counts leading turns that write an edit the verifier
      must reject; ``refuse`` is a blocked code the episode reports
      instead of proposing at all.
    * ``contents`` maps a candidate id — or ``<candidate>#<slate slot>`` —
      to the literal bodies that candidate writes,
      ``{"v1": {"style_rules": "verbose-prose"}}``: how a known-answer
      harness scripts the exact tree each candidate produces.
    """
    options = request.get("options")
    return options if isinstance(options, dict) else {}


def turn_for(request: dict[str, Any]) -> dict[str, Any]:
    """The one turn this request gets: edit first, then return."""
    task = task_of(request)
    read_at = _READ_ROOT.search(task)
    write_at = _WRITE_ROOT.search(task)
    if read_at is None or write_at is None:
        return {"error": "the task names no snapshot and working copy", "retryable": False}
    read_root, write_root = Path(read_at.group(1)), Path(write_at.group(1))
    candidate = _CANDIDATE.search(task)
    tag = candidate.group(1) if candidate else "candidate"
    slot = candidate.group(2) if candidate else None
    options = steering(request)
    per_candidate: dict[str, str] = {}
    if options.get("hypotheses"):
        per_candidate = json.loads(options["hypotheses"]).get(tag) or {}
    idea = per_candidate.get("idea") or options.get("idea")
    predict = per_candidate.get("predict") or options.get("predict")

    if options.get("refuse"):
        return {
            "text": "",
            "calls": [
                call(
                    "block",
                    {
                        "code": str(options["refuse"]),
                        "message": "the stand-in was asked to report a block",
                    },
                )
            ],
        }

    made = sum(1 for m in request.get("messages") or [] if m.get("role") == "assistant")
    broken_turns = int(options.get("break_first") or 0)

    if made < broken_turns:
        # A turn the verifier must reject: a truncated statement inside a
        # declared point, which is what a proposer looks like when it
        # writes source that does not parse in place. It still returns, so
        # the verifier runs and its findings come back as a repair turn.
        broken = _broken_edit(read_root, write_root)
        if broken is not None:
            path, content = broken
            return {
                "text": "",
                "calls": [
                    call("edit", {"path": str(path), "content": content}),
                    call("return", {"value": hypothesis_for("unknown", tag, idea, predict)}),
                ],
            }

    scripted = None
    if options.get("contents"):
        by_candidate = json.loads(options["contents"])
        # A slate's slots share a candidate, so a script that wants them
        # to differ keys on ``<candidate>#<slot>``; a script that does not
        # care keys on the candidate and every slot writes the same tree.
        scripted = by_candidate.get(f"{tag}#{slot}") or by_candidate.get(tag)
    found = proposal(read_root, write_root, tag, scripted)
    if found is None:
        return {
            "text": "",
            "calls": [
                call(
                    "block",
                    {
                        "code": "goal-unreachable",
                        "message": "no declared mutation point holds a literal to rewrite",
                    },
                )
            ],
        }
    path, content, point_id = found
    return {
        "text": f"Tagging {point_id} for {tag}.",
        "calls": [
            call("edit", {"path": str(path), "content": content}),
            call("return", {"value": hypothesis_for(point_id, tag, idea, predict)}),
        ],
    }


def main() -> int:
    from tests._foe_transport import chunks_for

    request = json.loads(sys.stdin.readline() or "{}")
    for chunk in chunks_for(turn_for(request)):
        sys.stdout.write(json.dumps({"chunk": chunk}) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":  # pragma: no cover - invoked as a launcher
    sys.exit(main())
