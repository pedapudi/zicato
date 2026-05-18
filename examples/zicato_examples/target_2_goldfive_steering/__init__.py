"""Dogfood target 2 — driving goldfive's own steering layer.

The inner harness here is goldfive itself: the proposer emits patches
against goldfive's prompt + threshold surface and the tournament
scores the snapshots against an adversarial board. See ``RUN.md`` in
this directory for the end-to-end walkthrough.

Side-effect free at import time so static tooling can introspect the
package without pulling in optional runtime dependencies.
"""

from __future__ import annotations

__all__: list[str] = []
