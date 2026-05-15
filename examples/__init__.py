"""Zicato dogfood examples.

Each subpackage under ``examples/`` is a self-contained dogfood target
the runtime can be pointed at. The packages are intentionally
side-effect free at import time so static tooling (the mutation-audit
CLI, board validators, the test suite) can introspect them without
pulling in optional runtime dependencies.
"""
