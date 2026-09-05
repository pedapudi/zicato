"""Running one board entry against one generation of the note writer.

An adapter is the seam between zicato and the thing being evolved. It
answers three questions: which sub-trees of a generation snapshot the
proposer may rewrite, how to load a snapshot into something runnable, and
what a run of one board entry produces.

This one loads nothing: it reads ``STYLE_RULES`` straight out of the
snapshot's source and composes the note that policy describes. Reading
the snapshot rather than importing it is what makes a generation's score
a function of the generation — the worker evaluating a candidate holds
its own copy of the tree, and this adapter reads that copy.

A system under test that runs on a model composes its answer by awaiting
``config.target_call_llm(system, user, model)`` instead. It must never
use ``config.evaluation_call_llm``: that callable serves the judges and
the emulator, and a system under test sharing it could collude with its
own grader.
"""

from __future__ import annotations

import ast
import time
from pathlib import Path
from typing import Any

from zicato.core import RunResult

#: Where the policy lives inside a generation snapshot. The snapshot
#: copies each mutable tree under its own directory name, so this is the
#: tree's name followed by the module holding the marker.
POLICY_RELATIVE_PATH = Path("system_under_test") / "__init__.py"

#: The filler the ``verbose-prose`` token appends. Long enough to push the
#: note past the conciseness predicate's budget on its own.
_FILLER = (
    " Additionally, and as has been noted at considerable length in prior "
    "correspondence on this subject, the situation continues to develop in "
    "ways that merit further elaboration, restatement, and the sort of "
    "extended contextual framing that a reader may find exhaustive rather "
    "than merely thorough, which is to say that brevity has not been the "
    "governing consideration in the composition of this particular note."
)


def read_style_tokens(generation_root: Path) -> list[str]:
    """Return the style tokens ``STYLE_RULES`` holds in one snapshot.

    Parses the assignment rather than importing the module: the snapshot
    is a copy of the tree on disk, and parsing it reads the bytes under
    evaluation themselves, with no import-cache question. A snapshot whose
    policy is missing or unparseable yields no tokens, which scores as a
    note with every feature present.
    """
    try:
        source = (generation_root / POLICY_RELATIVE_PATH).read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        module = ast.parse(source)
    except SyntaxError:
        return []
    for node in ast.walk(module):
        if not isinstance(node, ast.Assign):
            continue
        names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if "STYLE_RULES" not in names:
            continue
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            return [token.strip() for token in node.value.value.split(";") if token.strip()]
    return []


def compose_note(prompt: str, tokens: list[str]) -> str:
    """Compose the note the policy in ``tokens`` describes.

    Every feature is present unless a token suppresses it, so each token
    the proposer removes flips a single board entry from fail to pass.
    """
    parts = [f"NOTE: {prompt}".strip()]
    if "skip-citations" not in tokens:
        parts.append("[source: the briefing packet]")
    if "omit-summary" not in tokens:
        parts.append("SUMMARY: one line, stating the decision.")
    note = " ".join(parts)
    if "verbose-prose" in tokens:
        note += _FILLER
    return note


class _NoteWriterGeneration:
    """One loaded generation of the note writer.

    Stateless across runs, as the adapter protocol requires: the runner
    builds one of these per generation and calls :meth:`run` once per
    board entry.
    """

    def __init__(self, generation_root: Path) -> None:
        self._generation_root = Path(generation_root)

    async def run(self, entry: Any, sinks: Any, config: Any) -> RunResult:
        """Answer one board entry under this generation.

        ``sinks`` is the telemetry sink list the runner owns. This writer
        emits no events, so it forwards nothing; a system under test built
        on goldfive passes the list straight through to it, and the drift
        frames it emits then reach the ``drift:`` scoring channel.
        """
        del sinks, config
        started = time.monotonic()
        tokens = read_style_tokens(self._generation_root)
        note = compose_note(str(getattr(entry, "input", "") or ""), tokens)
        return RunResult(
            run_id=f"run-{entry.id}",
            entry_id=str(entry.id),
            final_output=note,
            transcript=(note,),
            runtime_ms=max(1, int((time.monotonic() - started) * 1000)),
        )


class NoteWriterAdapter:
    """The adapter the workspace's ``adapter`` block names."""

    name = "example_note_writer"
    run_output_names: tuple[str, ...] = ()

    def mutable_subpaths(self, generation_root: Path) -> list[Path]:
        """The one sub-tree of a snapshot the proposer may rewrite."""
        return [Path(generation_root) / "system_under_test"]

    def load(self, generation_root: Path) -> _NoteWriterGeneration:
        """Bind a runnable instance to one generation snapshot."""
        return _NoteWriterGeneration(generation_root)

    def mutation_points(self, source_roots: Any = None) -> list[Any]:
        """Declare no adapter-supplied mutation points.

        The loop enumerates ``# zicato:mutable`` markers from the snapshot
        itself. An adapter returns points here only when it can name spans
        the marker syntax cannot reach — a value in a database row, say.
        """
        del source_roots
        return []

    def worker_spec(self) -> dict[str, Any]:
        """How a tournament worker rebuilds this adapter in its own process.

        Board units run in subprocesses, which share no memory with the
        loop, so an adapter says how to reconstruct itself. This is the
        same block ``config.json`` carries.
        """
        return {"kind": "import", "factory": "example_wiring.adapter:make_adapter"}


def make_adapter() -> NoteWriterAdapter:
    """Build the adapter. Named by ``adapter.factory`` in ``config.json``."""
    return NoteWriterAdapter()


__all__ = [
    "POLICY_RELATIVE_PATH",
    "NoteWriterAdapter",
    "compose_note",
    "make_adapter",
    "read_style_tokens",
]
