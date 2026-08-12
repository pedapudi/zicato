"""Target 4 — a coding agent's configuration package as the system under test.

The **skeleton** for the fourth dogfood target (issue #170). The mutable
tree is a versioned configuration package — `AGENTS.md` plus `skills/*.md`,
carrying native markdown mutation markers — that an external coding agent
loads at startup. Snapshotting it snapshots an agent identity; promoting a
generation promotes a configuration.

* ``config_package/`` — THE MUTABLE TREE. Four mutation points across
  three markdown files; ``settings.json`` sits beside them, immutable.
* ``driver.py`` — :class:`AgentConfigAdapter`, the ``kind="import"``
  entrypoint that lives OUTSIDE the tree and spawns the agent binary in
  rpc mode against the snapshot's package.
* ``stub_agent.py`` — a hermetic stand-in binary speaking the same rpc
  protocol, so CI exercises the driver contract with no model anywhere.
* ``fixtures/toolbox/`` — the working tree board tasks are posed against.
* ``predicates.py`` — pass/fail predicates over what the agent said AND
  the patch it produced.
* ``board.jsonl`` / ``brief.md`` / ``scoring.json`` — the frozen contract,
  under ``telemetry_dialect: transcript``.

See ``README.md`` for the design and its limits, and ``RUN.md`` for the
recipe. Live runs against a real agent binary are operator-initiated.
"""

from __future__ import annotations

__all__: list[str] = []
