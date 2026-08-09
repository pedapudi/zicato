"""Multi-agent presentation tree (vendored), annotated for zicato.

This module is a vendored, self-contained copy of the upstream
``presentation_agent_orchestrated`` reference (coordinator + four
specialists). The intent of vendoring is twofold:

1. Give zicato a stable, importable target for mutation-surface
   enumeration. The mutation ids declared in this file are the
   contract the proposer addresses.
2. Decouple from the upstream telemetry / lazy-``app`` plumbing so
   importing this module is side-effect free under test.

Mutation-point annotations
--------------------------

Editable regions in this file are preceded by a ``# zicato:mutable``
marker. Three marker variants are used:

* ``# zicato:mutable id="<id>" role="<role>"`` — a string-literal span
  (an instruction sub-clause or a tool docstring). The marker binds to
  the nearest string literal beneath it, so adjacent pointed clauses
  are written as separate ``+``-joined literals, each with its own id.
* ``# zicato:mutable:code id="<id>" role="<role>"`` ... a code region
  closed by ``# zicato:mutable:end``. The body between the markers is
  real Python control flow the proposer may rewrite verbatim. This is
  how the slugify / output-path logic that determines WHERE files are
  written and HOW they are located is exposed as mutable surface.

The ``role`` hint the audit CLI surfaces groups points by purpose
(``system_instruction``, ``coordinator_routing``, ``topic_naming``,
``tool_description``, ``path_logic``).

String-span ids declared in this file:

* ``researcher_instruction``                   — research_agent's instruction
* ``web_developer_instruction``                — web_developer base instruction
* ``web_developer_topic_naming``               — how the developer names/passes the topic
* ``reviewer_instruction``                     — reviewer base instruction
* ``reviewer_read_path``                       — reviewer read-path derivation + files_not_found
* ``debugger_instruction``                     — debugger_agent's instruction
* ``coordinator_instruction``                  — coordinator base routing flow
* ``coordinator_files_not_found_routing``      — coordinator's files_not_found re-dispatch
* ``write_webpage_tool_description``           — ``write_webpage`` docstring
* ``read_presentation_files_tool_description`` — ``read_presentation_files`` docstring
* ``find_presentation_files_tool_description`` — ``find_presentation_files`` docstring
* ``patch_file_tool_description``              — ``patch_file`` docstring

Code-region ids declared in this file:

* ``topic_slugify_logic``       — the topic → slug normalization rule
* ``topic_output_dir_logic``    — slug → on-disk output directory path
* ``find_presentation_match_logic`` — fuzzy match of a topic against ``output/``

The two slug/path code regions are the surface the proposer needs to
fix the dominant ``files_not_found`` failure: the write path and the
read path both resolve through the same ``topic_output_dir_logic``
helper, so a single coherent edit can make the developer's write slug
and the reviewer's read slug agree.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

try:
    from google.adk.agents import Agent
    from google.adk.tools import AgentTool, FunctionTool
except ImportError:  # pragma: no cover — adk extra optional at import time
    Agent = None  # type: ignore[assignment,misc]
    AgentTool = None  # type: ignore[assignment,misc]
    FunctionTool = None  # type: ignore[assignment,misc]


# ---------------------------------------------------------------------------
# Run-output routing.
#
# This agent's tools write a rendered webpage to disk. They MUST write
# OUTSIDE this module's own source directory — writing next to ``__file__``
# puts run output inside the generation snapshot, and zicato then copies
# that output forward into every derived generation, compounding without
# bound until the disk fills.
#
# The zicato tournament worker exports ``ZICATO_RUN_SCRATCH_DIR``: a fresh
# per-run scratch directory outside the snapshot, discarded when the run
# ends. ``_output_base`` resolves the on-disk root every tool writes
# under: the scratch directory when zicato supplied one, else an
# ``output/`` directory next to this module for a bare standalone run
# (a developer running the agent directly, with no zicato around).
#
# The env var name is the contract pinned in
# ``zicato/epoch/snapshot_scope.py`` (SCRATCH_DIR_ENV); it is duplicated
# here as a bare string so this vendored target has no import dependency
# on zicato internals.
# ---------------------------------------------------------------------------

_SCRATCH_DIR_ENV = "ZICATO_RUN_SCRATCH_DIR"


def _output_base() -> str:
    """Return the directory run output is written under.

    The zicato-supplied per-run scratch directory when present; an
    ``output/`` directory next to this module otherwise. The result
    always exists on return.
    """
    scratch = os.environ.get(_SCRATCH_DIR_ENV)
    if scratch:
        base = os.path.join(scratch, "output")
    else:
        base = os.path.join(os.path.dirname(__file__), "output")
    os.makedirs(base, exist_ok=True)
    return base


# ---------------------------------------------------------------------------
# Measurement mode (opt-in, DEFAULT OFF) — ZICATO_TARGET1_MEASUREMENT_MODE=1
#
# This board's designed difficulty is the write/read slug mismatch below, and
# the affordances here would each dissolve part of it. They exist for running
# the board as a MEASUREMENT INSTRUMENT — comparing proposer configurations
# against each other — where the question is "which config proposes better",
# not "can the loop repair this board". Left on by default they would score a
# broken pipeline the same as a working one, so the gate is off unless asked.
#
#   * canonical deck dir: one fixed location write/read/find all agree on,
#     removing run-to-run variance that has nothing to do with the arm.
#   * salvage: persist the deck when the model DESCRIBES it instead of calling
#     write_webpage, so a run is scoreable ~100% of the time rather than
#     intermittently.
#   * history snapshots: an immutable copy per write, so "did turn N+1 keep
#     what turn N built?" is answerable from the artifact.
#
# None of these are proposer mutation points; they are operator bookkeeping.
#
# A run with the mode on drops a ``MEASUREMENT_MODE`` note in its output base,
# because the artifact tree it produces is NOT comparable with a normal run's
# and nothing else in the tree would say so.
# ---------------------------------------------------------------------------

_MEASUREMENT_ENV = "ZICATO_TARGET1_MEASUREMENT_MODE"
_DECK_DIRNAME = "presentation"
_HISTORY_DIRNAME = "deck_history"

#: Dropped in the canonical deck dir by a real ``write_webpage`` call, and
#: consulted by ``salvage_deck_from_response`` to decide whether the deck on
#: disk is the developer's own tool output (never clobber it) or a previous
#: salvage (refresh it, so a multi-turn revision is what the artifact holds).
#: A file, not a directory, so ``find_presentation_files`` cannot list it as a
#: candidate; and it lives under the per-run scratch dir, so the provenance
#: cannot leak from one run into the next the way a module global would.
_WRITE_MARKER = ".written_by_write_webpage"

#: Written to the output base whenever the mode is engaged, so an artifact
#: tree read back later carries the fact that a scorer cannot distinguish a
#: working pipeline from a broken one in it. See ``_mark_measurement_run``.
_MODE_MARKER = "MEASUREMENT_MODE"

#: Enforced output contract for the web_developer under measurement mode.
#: Setting this as the agent's ADK ``output_schema`` makes the model return a
#: validated JSON object on EVERY turn (structured output is enforced by the
#: API), which is what makes a deck reliably available to salvage.
DECK_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "html_content": {"type": "string"},
        "css_content": {"type": "string"},
        "js_content": {"type": "string"},
    },
    "required": ["html_content", "css_content", "js_content"],
}


def measurement_mode() -> bool:
    """True when the board is being run as a measurement instrument."""
    return os.environ.get(_MEASUREMENT_ENV, "0") == "1"


def _deck_dir() -> str:
    """Canonical per-run deck directory (always exists)."""
    d = os.path.join(_output_base(), _DECK_DIRNAME)
    os.makedirs(d, exist_ok=True)
    _mark_measurement_run()
    return d


def _mark_measurement_run() -> None:
    """Record in the artifact tree that this run had measurement mode on.

    Salvage guarantees a deck exists however the pipeline failed, so a scorer
    reading only the artifact cannot tell a working pipeline from a broken
    one. That is acceptable while measuring, and misleading afterwards — so
    the run leaves the fact on disk next to the deck rather than only in the
    operator's memory of which shell exported the variable.

    Called from ``_deck_dir`` rather than only from ``build_agent_tree``:
    the tree is built once per process (``root_agent`` is cached) while a
    worker process serves many runs, so a build-time-only note would land in
    the first run's output base and nowhere else. Idempotent, best-effort.
    """
    base = _output_base()
    marker = os.path.join(base, _MODE_MARKER)
    if os.path.exists(marker):
        return
    try:
        with open(marker, "w") as fh:
            fh.write(
                f"{_MEASUREMENT_ENV}=1 was set for this run.\n"
                "The deck under presentation/ may have been salvaged from the "
                "developer's prose rather than written by write_webpage, and "
                "read/find resolve to that one directory regardless of topic. "
                "Do not read a file-findability result out of this tree.\n"
            )
    except OSError:
        pass


def snapshot_deck(html: str, css: str, js: str) -> None:
    """Persist an immutable copy of this write as ``deck_history/turn_<n>/``.

    The canonical deck dir is overwritten in place each turn, which erases the
    history a revision-quality metric needs. Turn numbering is the count of
    existing snapshots, so the sequence is write-ordered. Best-effort: a
    snapshot failure must never break the run or the deck just written.
    """
    if not measurement_mode():
        return
    try:
        hist = os.path.join(_output_base(), _HISTORY_DIRNAME)
        os.makedirs(hist, exist_ok=True)
        n = len([q for q in os.listdir(hist) if q.startswith("turn_")])
        turn = os.path.join(hist, f"turn_{n}")
        os.makedirs(turn, exist_ok=True)
        for name, body in (("index.html", html), ("styles.css", css), ("script.js", js)):
            with open(os.path.join(turn, name), "w") as fh:
                fh.write(body)
    except OSError:
        pass


def _extract_fenced(text: str, langs: tuple[str, ...]) -> str:
    """Return the first fenced code block tagged with any of ``langs``."""
    for lang in langs:
        m = re.search(rf"```{lang}\s*\n(.*?)```", text, re.DOTALL | re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return ""


def _deck_from_structured(text: str) -> tuple[str, str, str] | None:
    """Parse the ``DECK_OUTPUT_SCHEMA`` JSON the developer returns, if present.

    Tolerates a stray code fence or leading prose around the JSON.
    """
    candidates = [text]
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        candidates.append(m.group(0))
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict) and "html_content" in obj:
            return (
                str(obj.get("html_content") or ""),
                str(obj.get("css_content") or ""),
                str(obj.get("js_content") or ""),
            )
    return None


def salvage_deck_from_response(callback_context: Any, llm_response: Any) -> Any:
    """Persist the deck the web_developer produced, however it produced it.

    An ``after_model_callback``. Writes the deck to the canonical dir the
    moment the developer responds -- before the coordinator hands off to the
    reviewer -- so the reviewer does not report ``files_not_found`` and the
    debugger does not loop on ``find_presentation_files``.

    Order of preference: the structured JSON deck (the contract), then fenced
    ```html/```css/```js prose, then a raw ``<!DOCTYPE ...></html>`` span.

    Never clobbers a deck a real ``write_webpage`` call put on disk — that
    provenance is the ``_WRITE_MARKER`` file, not mere non-emptiness, because
    a deck this callback salvaged on turn N is exactly the deck turn N+1 must
    be allowed to revise. Guarding on non-emptiness instead would freeze the
    artifact at the first salvage and take the revision history with it.

    Returns None (never mutates the response) and never raises.
    """
    del callback_context
    if not measurement_mode():
        return None
    try:
        content = getattr(llm_response, "content", None)
        parts = getattr(content, "parts", None) or []
        for part in parts:
            fc = getattr(part, "function_call", None)
            if fc is not None and getattr(fc, "name", "") == "write_webpage":
                return None  # a real write is in flight; let the tool handle it
        text = "\n".join(getattr(q, "text", None) or "" for q in parts)
        if not text.strip():
            return None

        structured = _deck_from_structured(text)
        if structured is not None:
            html, css, js = structured
        else:
            html = _extract_fenced(text, ("html", "htm"))
            if not html:
                m = re.search(r"(<!DOCTYPE html.*?</html>)", text, re.DOTALL | re.IGNORECASE)
                html = m.group(1).strip() if m else ""
            css = _extract_fenced(text, ("css",))
            js = _extract_fenced(text, ("javascript", "js"))
        if not html:
            return None

        deck = _deck_dir()
        if os.path.exists(os.path.join(deck, _WRITE_MARKER)):
            return None  # the developer's own tool wrote this deck; leave it
        for name, body in (("index.html", html), ("styles.css", css), ("script.js", js)):
            with open(os.path.join(deck, name), "w") as fh:
                fh.write(body)
        snapshot_deck(html, css, js)
    except (OSError, AttributeError, TypeError, ValueError):
        pass
    return None


# ---------------------------------------------------------------------------
# Slug / output-path logic — the WHERE-files-are-written and
# HOW-they-are-located surface.
#
# The dominant board failure is a write/read slug mismatch: the
# web_developer slugifies an embellished topic while the reviewer derives
# its read path from a different topic string, so ``read_presentation_files``
# returns ``files_not_found``. The fix lives in THIS logic, not just the
# instructions — so the two helpers below carry ``# zicato:mutable:code``
# regions the proposer can rewrite. Both the write tool and the read tool
# resolve their directory through ``_topic_output_dir``, so a single edit
# keeps the write slug and the read slug in lock-step.
# ---------------------------------------------------------------------------


def _slugify_topic(topic: str) -> str:
    """Normalize ``topic`` into an on-disk directory-name slug."""
    # zicato:mutable:code id="topic_slugify_logic" role="path_logic"
    slug = topic.lower().replace(" ", "_").replace("/", "_")
    # zicato:mutable:end
    return slug


def _read_deck_files(directory: str) -> dict[str, str]:
    """Read the three deck files out of ``directory``.

    Missing files come back as ``<error reading ...>`` strings rather than
    raising, which is the shape both read tools have always returned.
    """
    files: dict[str, str] = {}
    for name in ("index.html", "styles.css", "script.js"):
        path = os.path.join(directory, name)
        try:
            with open(path) as f:
                files[name] = f.read()
        except OSError as e:
            files[name] = f"<error reading {path}: {e}>"
    return files


def _topic_output_dir(topic: str) -> str:
    """Resolve the absolute output directory for ``topic``."""
    # zicato:mutable:code id="topic_output_dir_logic" role="path_logic"
    slug = _slugify_topic(topic)
    output_dir = os.path.join(_output_base(), slug)
    # zicato:mutable:end
    return output_dir


# ---------------------------------------------------------------------------
# Tools — write / read / patch the generated presentation files.
#
# Each tool function's docstring is annotated with a ``# zicato:mutable``
# marker so the proposer can rewrite the tool *description* that ADK
# surfaces to the LLM. The slug/path control flow is mutable via the
# ``# zicato:mutable:code`` regions above (and, for the fuzzy matcher,
# inside ``find_presentation_files``).
# ---------------------------------------------------------------------------


def write_webpage(topic: str, html_content: str, css_content: str, js_content: str) -> str:
    # zicato:mutable id="write_webpage_tool_description" role="tool_description"
    """Write an interactive webpage (HTML, CSS, JS) under ``output/``.

    Pass the bare ``topic`` slug (no embellishments). The tool slugifies
    the topic for the on-disk directory name and returns the absolute
    output directory path on success.
    """
    try:
        # Measurement mode pins every write to one canonical directory that
        # write/read/find all agree on, so a slug mismatch cannot make a run
        # unscoreable. Off by default: resolving that mismatch is this board's
        # designed challenge and lives in the mutable logic above.
        output_dir = _deck_dir() if measurement_mode() else _topic_output_dir(topic)
        os.makedirs(output_dir, exist_ok=True)

        with open(os.path.join(output_dir, "index.html"), "w") as f:
            f.write(html_content)
        with open(os.path.join(output_dir, "styles.css"), "w") as f:
            f.write(css_content)
        with open(os.path.join(output_dir, "script.js"), "w") as f:
            f.write(js_content)

        if measurement_mode():
            # Claim the deck as tool-written so salvage stops refreshing it.
            with open(os.path.join(output_dir, _WRITE_MARKER), "w") as f:
                f.write(topic)
        snapshot_deck(html_content, css_content, js_content)
        return f"Successfully created presentation on '{topic}' at {output_dir}"
    except OSError as e:
        return f"Error writing file: {e}"


def read_presentation_files(topic: str) -> dict[str, str]:
    # zicato:mutable id="read_presentation_files_tool_description" role="tool_description"
    """Read the generated presentation files and return name to contents.

    Reads ``index.html``, ``styles.css``, and ``script.js`` from the
    output directory for the given ``topic``. Missing files come back
    as ``<error reading ...>`` strings rather than raising.
    """
    output_dir = _deck_dir() if measurement_mode() else _topic_output_dir(topic)
    files: dict[str, str] = {}
    for name in ("index.html", "styles.css", "script.js"):
        path = os.path.join(output_dir, name)
        try:
            with open(path) as f:
                files[name] = f.read()
        except OSError as e:
            files[name] = f"<error reading {path}: {e}>"
    return files


def find_presentation_files(topic: str) -> dict[str, Any]:
    # zicato:mutable id="find_presentation_files_tool_description" role="tool_description"
    """Locate the generated presentation directory by fuzzy-matching ``topic``.

    The web developer often slugifies an embellished topic while the
    reviewer asks for the bare slug. This tool searches ``output/`` for
    a directory whose name either contains the bare slug or, after
    stripping ``_presentation`` / ``_interactive_presentation`` /
    ``_slideshow`` suffixes, equals it. On a match it reads the three
    presentation files and returns their contents.
    """
    if measurement_mode():
        # The canonical dir is the only place a deck can be, so the fuzzy
        # match has nothing to do. Without this the finder would report
        # found=False for a deck sitting in plain sight: the match logic
        # compares the topic slug against the directory name, and the
        # canonical name is "presentation" for every topic.
        deck = os.path.realpath(_deck_dir())
        return {"found": True, "directory": deck, "files": _read_deck_files(deck)}

    base_dir = os.path.realpath(_output_base())

    if not os.path.isdir(base_dir):
        return {"found": False, "candidates": []}

    entries = sorted(
        name for name in os.listdir(base_dir) if os.path.isdir(os.path.join(base_dir, name))
    )

    # zicato:mutable:code id="find_presentation_match_logic" role="path_logic"
    topic_slug = _slugify_topic(topic)
    suffixes = ("_interactive_presentation", "_presentation", "_slideshow")
    match: str | None = None
    for name in entries:
        stripped = name
        for suffix in suffixes:
            if stripped.endswith(suffix):
                stripped = stripped[: -len(suffix)]
                break
        if topic_slug and (topic_slug in name or stripped == topic_slug):
            match = name
            break
    # zicato:mutable:end

    if match is None:
        return {"found": False, "candidates": entries}

    candidate_dir = os.path.realpath(os.path.join(base_dir, match))
    # Defence in depth: refuse anything that escaped the sandbox.
    if not (candidate_dir == base_dir or candidate_dir.startswith(base_dir + os.sep)):
        return {"found": False, "candidates": entries}

    return {
        "found": True,
        "directory": candidate_dir,
        "files": _read_deck_files(candidate_dir),
    }


def patch_file(path: str, new_content: str) -> str:
    # zicato:mutable id="patch_file_tool_description" role="tool_description"
    """Overwrite ``path`` with ``new_content`` in place.

    Relative paths resolve against ``output/`` so the debugger cannot
    scribble outside the sandbox. Returns a short success / error
    message; never raises on filesystem errors.
    """
    try:
        if not os.path.isabs(path):
            path = os.path.join(_output_base(), path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(new_content)
        return f"Successfully patched {path}"
    except OSError as e:
        return f"Error patching file: {e}"


# ---------------------------------------------------------------------------
# Agent tree — coordinator + four specialists. The mutation surface
# lives in the ``instruction=`` kwarg of each Agent constructor. Where a
# single instruction carries more than one independent decision, the
# decision-specific clause is split into its own ``+``-joined string
# literal with a pointed id so the proposer can rewrite just that
# sub-decision (e.g. topic naming, the files_not_found routing) without
# disturbing the rest of the instruction.
# ---------------------------------------------------------------------------


def build_agent_tree(model: Any) -> Any:
    """Construct the coordinator + specialists tree against ``model``.

    ``model`` is forwarded to every ADK ``Agent`` unchanged — pass a
    LiteLLM-style identifier (e.g. ``"openai/gpt-4o-mini"``) for live
    runs, or a ``BaseLlm`` subclass for mock/offline runs. The function
    rebuilds the tree from scratch on every call so each test gets a
    fresh tree that holds no state from prior calls.
    """
    if Agent is None or AgentTool is None or FunctionTool is None:
        raise RuntimeError(
            "google-adk is not installed; install the adk extra to build "
            "the vendored presentation tree."
        )

    write_webpage_tool = FunctionTool(write_webpage)
    read_presentation_files_tool = FunctionTool(read_presentation_files)
    find_presentation_files_tool = FunctionTool(find_presentation_files)
    patch_file_tool = FunctionTool(patch_file)

    research_agent = Agent(
        name="research_agent",
        model=model,
        # zicato:mutable id="researcher_instruction" role="system_instruction"
        instruction=(
            "You are a researcher. Your goal is to gather information "
            "about the topic the user provides.\nThink step-by-step and "
            "provide a comprehensive synthesis of high-quality bullet "
            "points and facts that can be used to generate a "
            "presentation slideshow."
        ),
        description=(
            "An agent capable of deeply reasoning and synthesizing a "
            "given topic for presentation notes."
        ),
        tools=[],
    )

    # Under measurement mode the developer runs with an ENFORCED output_schema
    # (NOT ADK mode=ANY: mode=ANY cannot end its turn under a single-Runner
    # overlay that drops the tool's escalate action, so it spins write_webpage
    # unboundedly). The schema makes a deck available on every turn and the
    # callback persists it. Both are no-ops when the mode is off.
    _dev_extra: dict[str, Any] = {}
    if measurement_mode():
        _mark_measurement_run()
        _dev_extra = {
            "output_schema": DECK_OUTPUT_SCHEMA,
            "after_model_callback": salvage_deck_from_response,
        }

    web_developer_agent = Agent(
        name="web_developer_agent",
        model=model,
        **_dev_extra,
        instruction=(
            # zicato:mutable id="web_developer_instruction" role="system_instruction"
            "You are an expert Frontend Web Developer. Your goal is to "
            "take research on a topic and generate a stunning, "
            "interactive, single-page presentation slideshow.\nGenerate "
            "beautiful semantic HTML structure, elegant CSS with modern "
            "design trends, animations, and transitions, and JavaScript "
            "for slideshow navigation (next/prev slides).\nThe HTML MUST "
            'include `<link rel="stylesheet" href="styles.css">` and '
            '`<script src="script.js"></script>` so the files are '
            "connected properly.\nRemember to output the absolute final "
            "HTML, CSS, and JS using the `write_webpage` tool! Do not "
            "just print the code out, you must invoke the tool once "
            "everything is ready.\n"
            # zicato:mutable id="web_developer_topic_naming" role="topic_naming"
            + "Pass the ``topic`` argument to ``write_webpage`` exactly as "
            "the coordinator gave it to you — the bare task title, with "
            "no ``_presentation`` suffix and no embellishments — because "
            "the reviewer will derive its read path from that same "
            "string. When you reply to the coordinator, surface the "
            "absolute output directory path that ``write_webpage`` "
            "returned so downstream agents can use it."
        ),
        description=(
            "An expert frontend developer agent that generates "
            "interactive HTML, CSS, and JS slideshow presentations and "
            "saves them to disk."
        ),
        tools=[write_webpage_tool],
    )

    reviewer_agent = Agent(
        name="reviewer_agent",
        model=model,
        instruction=(
            # zicato:mutable id="reviewer_instruction" role="system_instruction"
            "You are a senior frontend code reviewer. You will be given "
            "the topic of a presentation that ``web_developer_agent`` "
            "just generated. Call the ``read_presentation_files`` tool "
            "with the topic to fetch the generated HTML, CSS, and JS, "
            "then produce a structured critique as a list of issues. "
            "Each issue must include a short description and a severity "
            "of 'critical', 'major', or 'minor'. If there are no issues, "
            "return an empty list and say so explicitly so the "
            "coordinator knows to skip debugging.\n"
            # zicato:mutable id="reviewer_read_path" role="topic_naming"
            + "Derive the ``topic`` you pass to ``read_presentation_files`` "
            "from the bare task title exactly as the coordinator gave it "
            "to you — the same string the developer wrote under — so the "
            "read path matches the write path.\n"
            "If ``read_presentation_files`` returns "
            "``<error reading ...>`` for ALL three files, the developer "
            "almost certainly wrote them under a different slug. Do NOT "
            "call ``read_presentation_files`` again with a guessed "
            "alternative topic — that will only loop. Instead, stop "
            "reviewing and report a single structured ``files_not_found`` "
            "critique to the coordinator that includes the exact "
            "``topic`` string you tried and an explicit request to "
            "invoke ``debugger_agent`` to locate the files."
        ),
        description=(
            "A reviewer agent that reads the generated presentation "
            "files and produces a structured critique of issues and "
            "their severity."
        ),
        tools=[read_presentation_files_tool],
    )

    debugger_agent = Agent(
        name="debugger_agent",
        model=model,
        # zicato:mutable id="debugger_instruction" role="system_instruction"
        instruction=(
            "You are a debugging agent with two distinct failure modes "
            "to handle. The first is broken files: when "
            "``reviewer_agent`` flagged critical issues or "
            "``write_webpage`` failed, read the issues and their file "
            "paths, then call the ``patch_file`` tool with the full "
            "corrected content of each file that needs to change, and "
            "report which files you patched when you are done. The "
            "second is missing files: when the reviewer reports "
            "``files_not_found`` because ``read_presentation_files`` "
            "could not find the developer's output under the topic "
            "slug, call the ``find_presentation_files`` tool with the "
            "topic the reviewer tried. If it returns ``found=True``, "
            "report the discovered absolute directory and the file "
            "contents back to the coordinator so the review can proceed "
            "at that path. If it returns ``found=False``, report the "
            "candidate directory list back to the coordinator so it can "
            "re-dispatch the developer with the correct topic — do not "
            "attempt to patch files you have not located."
        ),
        description=(
            "A debugger agent that either patches generated presentation "
            "files in place to resolve critical issues, or locates the "
            "developer's output directory when the reviewer cannot find "
            "it."
        ),
        tools=[find_presentation_files_tool, patch_file_tool],
    )

    return Agent(
        name="coordinator_agent",
        model=model,
        instruction=(
            # zicato:mutable id="coordinator_instruction" role="coordinator_routing"
            "You are the Coordinator Agent. Your task is to work with "
            "the user to pick a topic for an interactive slideshow "
            "presentation.\nFirst, get a topic from the user.\nSecond, "
            "transfer control to the 'research_agent' to gather "
            "comprehensive context and facts about the topic. Make sure "
            "to provide it with the topic!\nThird, after researching, "
            "transfer control to the 'web_developer_agent' and provide "
            "it with all the researched materials. Instruct it to "
            "generate and save the presentation codebase. Pass the bare "
            "task title as the topic, with no ``_presentation`` suffix "
            "and no embellishments, so the reviewer's read path will "
            "match.\nFourth, transfer control to the 'reviewer_agent' "
            "with the SAME bare topic string you gave the developer so "
            "it can read the generated files and produce a structured "
            "critique.\nFifth, if ``write_webpage`` failed or the "
            "reviewer reported any critical issues, transfer control to "
            "the 'debugger_agent' with the reviewer's critique and have "
            "it patch the affected files. Skip this step when the "
            "reviewer reports no critical issues.\n"
            # zicato:mutable id="coordinator_files_not_found_routing" role="coordinator_routing"
            + "If the reviewer instead reports ``files_not_found``, "
            "transfer control to the 'debugger_agent' with the exact "
            "topic string the reviewer tried and ask it to call "
            "``find_presentation_files`` to locate the output. When the "
            "debugger reports ``found=True``, the run can proceed: hand "
            "the discovered directory and file contents back to the "
            "reviewer so it can produce its critique against those "
            "files. When the debugger reports ``found=False``, transfer "
            "control back to 'web_developer_agent' with explicit "
            "instructions to re-run ``write_webpage`` using the bare "
            "task title as the ``topic`` argument — no "
            "``_presentation`` suffix, no embellishments — so that the "
            "reviewer's read path will match.\n"
            "Finally, report back to "
            "the user when the task is complete.\nFlow: research → "
            "web_developer → reviewer → (if critical issues OR "
            "files_not_found) debugger → report."
        ),
        description=(
            "The main coordinator agent that drives the overall process "
            "of creating an interactive slideshow generation."
        ),
        tools=[
            AgentTool(research_agent),
            AgentTool(web_developer_agent),
            AgentTool(reviewer_agent),
            AgentTool(debugger_agent),
        ],
    )


# ---------------------------------------------------------------------------
# Lazy module-level ``root_agent`` — built on first attribute access so
# importing the module without the ADK extra installed does not blow
# up. The tests in ``tests/test_example_target_1_presentation.py`` walk
# the source for mutation markers without ever materialising the tree.
# ---------------------------------------------------------------------------


_MODEL_NAME = os.environ.get("ZICATO_TARGET_1_MODEL", "openai/gpt-4o-mini")

_root_agent: Any | None = None


def __getattr__(name: str) -> Any:
    """PEP 562 lazy attribute — build ``root_agent`` on first access."""
    global _root_agent
    if name == "root_agent":
        if _root_agent is None:
            _root_agent = build_agent_tree(_MODEL_NAME)
        return _root_agent
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# ``root_agent`` is provided lazily via PEP 562 ``__getattr__`` above so
# importing the module doesn't construct the agent eagerly. Ruff can't
# see that and flags it as undefined — silence the specific check.
__all__ = ["build_agent_tree", "root_agent"]  # noqa: F822
