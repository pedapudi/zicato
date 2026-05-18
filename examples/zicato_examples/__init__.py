"""Zicato dogfood examples — the ``zicato_examples`` package.

Each subpackage of ``zicato_examples`` is a self-contained dogfood
target the runtime can be pointed at. The package is distributed
separately (distribution name ``zicato-examples``) and is installed by
``make install`` into the dev environment; it is intentionally NOT
shipped inside the ``zicato`` wheel.

The packages are intentionally side-effect free at import time so
static tooling (the mutation-audit CLI, board validators, the test
suite) can introspect them without pulling in optional runtime
dependencies.
"""
