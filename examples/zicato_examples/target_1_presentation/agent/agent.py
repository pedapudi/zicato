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

import os
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
        output_dir = _topic_output_dir(topic)
        os.makedirs(output_dir, exist_ok=True)

        with open(os.path.join(output_dir, "index.html"), "w") as f:
            f.write(html_content)
        with open(os.path.join(output_dir, "styles.css"), "w") as f:
            f.write(css_content)
        with open(os.path.join(output_dir, "script.js"), "w") as f:
            f.write(js_content)

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
    output_dir = _topic_output_dir(topic)
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

    files: dict[str, str] = {}
    for fname in ("index.html", "styles.css", "script.js"):
        path = os.path.join(candidate_dir, fname)
        try:
            with open(path) as f:
                files[fname] = f.read()
        except OSError as e:
            files[fname] = f"<error reading {path}: {e}>"

    return {"found": True, "directory": candidate_dir, "files": files}


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

    web_developer_agent = Agent(
        name="web_developer_agent",
        model=model,
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
