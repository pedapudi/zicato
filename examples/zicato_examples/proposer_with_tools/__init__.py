"""A copy-me tool-using proposer example (Design A).

This package is a worked example of a *custom* zicato proposer — a native
ADK agent that reads the parent generation snapshot and the epoch journal
while it reasons about the next experiment, then emits the structured
``{hypothesis, patches}`` JSON the proposer contract demands.

Lift the ``agent.py`` in this package into a real proposer dir
(``proposers/<name>/agent.py``) and set its ``model=`` to your proposer
model — which MUST differ from the harness model — to use it. The runtime
loads the module-level ``agent`` symbol; tests import ``build_agent`` and
pass a fake model.

Side-effect free at import time: ``google.adk`` is imported only inside
``build_agent`` / on first ``agent`` access, so static tooling can
introspect this package without the optional ADK extra installed.
"""
