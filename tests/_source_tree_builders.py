"""System-under-test source trees for mutation and generation-store tests.

Tests that exercise the mutation surface, a generation store or the
proposer's grounding tools need a source tree carrying at least one
``# zicato:mutable`` span to mutate. The tree itself is scaffolding, not
the subject, so every such test built the same one-file tree; this module
holds the single copy.
"""

from __future__ import annotations

import textwrap
from pathlib import Path


def write_dedented(path: Path, body: str) -> None:
    """Write ``body`` with its common leading indentation stripped.

    Callers pass triple-quoted source indented to match the surrounding
    function, so the indentation has to come off before the text is valid
    Python. Parent directories are created on demand.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body), encoding="utf-8")


def mutable_tree(root: Path, *, instr: str = "original") -> Path:
    """A source tree under ``root`` holding one mutation point.

    One file, ``agent/prompts.py``, carrying a single mutable span whose
    id is ``instr`` and whose body is the ``instr`` argument. This is the
    smallest tree the mutation surface will enumerate.
    """
    tree = root / "agent"
    write_dedented(
        tree / "prompts.py",
        f'''
        # zicato:mutable id="instr"
        INSTR = """{instr}"""
        ''',
    )
    return tree
