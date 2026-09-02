"""Foe's static episode export, written beside a settled episode's log.

Foe renders a finished episode log to one self-contained HTML page, using
the code its live viewer runs. The page is where an operator reads the
episode at Foe's own depth: every tool call in both its rendered and its
canonical form, the budget the episode consumed, sandbox status, and the
causality figure. Nothing serves it and nothing starts for it — the page
is a file in the episode's directory, and a browser opens it.

The export is one invocation of the same binary that ran the episode. The
Foe command line spells it ``foe view DIR [--serve] [--port N]``, and
without ``--serve`` the binary writes the whole page to standard output
and exits zero::

    foe view <the episode's own directory>

The directory named is the one holding that episode's ``episode.jsonl``,
so the page carries that episode and the episodes it spawned, and nothing
else. A directory with no readable log writes a diagnostic to standard
error and exits one.

Writing the page is best effort and is the last thing the round does with
the episode. A binary that cannot be run, an export that fails or outlives
the bound below, and a directory that cannot be written each leave the log
untouched and the round unaffected; the dashboard then names the log and
this command instead of linking a page. A workspace whose configured
binary is still the placeholder the scaffold writes runs no episode at
all, so it has no page to link and reads that same caption.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from pathlib import Path

from zicato.storage import atomic_write_text

__all__ = [
    "EXPORT_FILENAME",
    "EXPORT_TIMEOUT_S",
    "export_command",
    "write_episode_export",
]

log = logging.getLogger("zicato.proposer.episode_export")

#: What the page is called inside the episode's directory. It sits beside
#: the ``episode.jsonl`` it was rendered from, so an episode directory
#: copied elsewhere carries both.
EXPORT_FILENAME = "episode.html"

#: How long the export may take before the round stops waiting on it. The
#: page is one pass over a log the round has already finished writing, so
#: a run this long means the binary is wedged rather than busy, and the
#: round stops waiting.
EXPORT_TIMEOUT_S = 30.0


def export_command(
    binary: str | os.PathLike[str], episode_dir: str | os.PathLike[str]
) -> list[str]:
    """The argument vector that renders one episode directory to a page.

    The single definition of the spelling. The dashboard shows it as the
    command to run by hand when a workspace has no page, so a change here
    changes both what zicato runs and what it tells an operator to run.
    """
    return [os.fspath(binary), "view", os.fspath(episode_dir)]


async def write_episode_export(
    binary: str | os.PathLike[str], episode_dir: str | os.PathLike[str]
) -> Path | None:
    """Render one settled episode to ``episode.html`` in its own directory.

    Returns the path written, or ``None`` when no page was produced. Every
    way the export can fail — an unrunnable binary, a non-zero exit, empty
    output, a timeout, an unwritable directory — returns ``None`` after a
    debug line, because the caller's round has already succeeded or failed
    on its own terms and this page changes neither.

    The page is written through the atomic-write discipline, so a reader
    that opens the file while a later round is rewriting it sees one whole
    page rather than a truncated one.
    """
    directory = Path(episode_dir)
    command = export_command(binary, directory)
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        log.debug("episode export could not start %s: %s", command[0], exc)
        return None
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=EXPORT_TIMEOUT_S)
    except TimeoutError:
        with contextlib.suppress(OSError, ProcessLookupError):
            process.kill()
        with contextlib.suppress(Exception):  # noqa: BLE001 - the page is best effort
            await process.wait()
        log.debug("episode export for %s outlived %ss", directory, EXPORT_TIMEOUT_S)
        return None
    if process.returncode != 0 or not stdout:
        log.debug(
            "episode export for %s exited %s: %s",
            directory,
            process.returncode,
            stderr.decode("utf-8", "replace").strip(),
        )
        return None
    destination = directory / EXPORT_FILENAME
    try:
        atomic_write_text(destination, stdout.decode("utf-8", "replace"))
    except OSError as exc:
        log.debug("episode export for %s could not be written: %s", directory, exc)
        return None
    return destination
