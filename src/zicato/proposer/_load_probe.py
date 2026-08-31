"""Subprocess entry point for the ``validate_patches`` tier-3 load probe.

Runs ``adapter.load(scratch_root)`` — the SAME call the tournament makes
before any board entry executes — against a scratch snapshot, and reports
whether the module graph imports. Nothing else. It is the cheapest way to
learn one round early that a patch left the harness unimportable.

Why its own module
------------------
The probe is NOT inlined, by design, into
:mod:`zicato.proposer.validate`. ``adapter.load`` imports the system under
test, which is arbitrary operator code: it can hang, exhaust memory, spawn
threads, or leave import side effects in ``sys.modules`` that would then be
visible to the proposer's own process. Running it in a child process with a
timeout contains all of that, and keeping the entry point in a separate
module means ``validate.py`` never imports
:mod:`zicato.adapter_factory` — so the structural claim that the validate
path has no route to the board holds by import closure rather than by
inspection.
See ``tests/test_proposer_validate.py`` for the pin.

Invoked as::

    python -m zicato.proposer._load_probe <workspace_root> <scratch_root>

and prints a single JSON object to stdout::

    {"ok": true}
    {"ok": false, "error": "...", "traceback": "..."}

Exit status is always ``0`` when the probe itself ran — a harness that
fails to import is a RESULT rather than a probe failure. A non-zero exit means
the probe could not run at all (bad arguments, unreadable workspace), and
the caller reports that distinctly.

The probe NEVER runs a board entry. It only resolves the harness entry
point; :class:`~zicato.adapters.base.RunnableHarness` is returned and
dropped without being invoked.
"""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path


def main(argv: list[str]) -> int:
    """Probe ``adapter.load`` against a scratch tree; print a JSON verdict."""
    if len(argv) != 2:
        print(
            f"usage: python -m zicato.proposer._load_probe "
            f"<workspace_root> <scratch_root> (got {len(argv)} args)",
            file=sys.stderr,
        )
        return 2
    workspace_root = Path(argv[0])
    scratch_root = Path(argv[1])

    try:
        from zicato.adapter_factory import make_adapter_from_config
        from zicato.workspace_loader import load_workspace_config

        adapter = make_adapter_from_config(load_workspace_config(workspace_root))
    except Exception as exc:  # noqa: BLE001 — a workspace we cannot read is a probe failure
        print(
            f"load probe could not build the adapter: {type(exc).__name__}: {exc}", file=sys.stderr
        )
        return 2

    try:
        adapter.load(scratch_root)
    except Exception as exc:  # noqa: BLE001 — an unimportable harness is the RESULT
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "traceback": traceback.format_exc(),
                }
            )
        )
        return 0
    print(json.dumps({"ok": True}))
    return 0


if __name__ == "__main__":  # pragma: no cover — exercised as a subprocess
    raise SystemExit(main(sys.argv[1:]))
