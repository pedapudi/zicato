"""The deterministic policy harness — NO LLM anywhere.

:class:`DeterministicPolicyAdapter` is a real
:class:`zicato.adapters.base.HarnessAdapter`-shaped object whose session
synthesises its output purely from the ``STYLE_RULES`` token list in the
generation snapshot it was loaded from. Because the snapshot IS the
input, the full evolve loop (propose → apply → subprocess tournament
worker → reduce → gate) is exercised with a scalar that is an exact,
hand-computable function of the remaining defect tokens:

* every remaining token emits ONE ``drift_detected`` frame at severity
  ``info`` (→ ``+1.0`` drift loss per run under the example contract's
  ``severity_weights``), and
* each KNOWN defect token suppresses one output feature, failing exactly
  one board predicate (see :mod:`.predicates`).

The session implements the rich ``run(entry, sinks, config)`` shape and
emits real goldfive lifecycle frames (``run_started`` …
``drift_detected`` × k … ``run_completed``) through the worker's sink
list, so the REAL reducer computes the loss from a real events file —
no telemetry stubs.

The adapter is subprocess-safe: ``worker_spec()`` returns the
``{"kind": "import", "factory": ...}`` shape both
:func:`zicato.adapter_factory.make_adapter_from_config` and the
tournament worker's ``_build_adapter`` reconstruct from a dotted path.
"""

from __future__ import annotations

import ast
import time
from pathlib import Path
from typing import Any

from zicato.core import RunResult

#: Where the policy lives inside a generation snapshot. The workspace
#: registers the example's ``agent/`` directory as its mutable tree, so
#: the seeded ``v0`` snapshot (and every derived child) carries it under
#: the tree's basename.
POLICY_RELPATH = Path("agent") / "policy.py"

#: The defect tokens the harness understands, mapped to the feature they
#: suppress. Unknown tokens still count as one drift frame each but
#: suppress nothing — a generic defect.
KNOWN_DEFECTS = ("verbose-prose", "omit-summary", "skip-citations", "fabricate-metrics")

#: The filler paragraph appended while ``verbose-prose`` remains. Long
#: enough on its own to blow the ``is_concise`` predicate's character
#: budget (see :data:`.predicates.CONCISE_MAX_CHARS`).
_FILLER = "FILLER: " + "meandering prose that adds nothing " * 24


def parse_style_tokens(policy_source: str) -> list[str]:
    """Extract the ``STYLE_RULES`` token list from policy-module source.

    Parses the module with :mod:`ast` (never imports it — the snapshot
    under evaluation is untrusted, proposer-patched code) and reads the
    string assigned to ``STYLE_RULES``. Tokens are ``;``-separated,
    whitespace-stripped, empties dropped, order preserved.

    A policy that no longer parses, or lost its ``STYLE_RULES``
    assignment, yields the sentinel token ``["broken-policy"]`` — one
    generic defect — rather than raising, so a destructive patch that
    somehow survives validation still scores (badly) instead of
    crashing the worker.
    """
    try:
        tree = ast.parse(policy_source)
    except SyntaxError:
        return ["broken-policy"]
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "STYLE_RULES":
                    value = node.value
                    if isinstance(value, ast.Constant) and isinstance(value.value, str):
                        tokens = [t.strip() for t in value.value.split(";")]
                        return [t for t in tokens if t]
                    return ["broken-policy"]
    return ["broken-policy"]


def synthesize_output(entry_input: str, tokens: list[str]) -> str:
    """Deterministically render the run's ``final_output`` from the tokens.

    The base note always opens with ``NOTE:`` (the stable control
    feature). Each KNOWN defect token then suppresses — or injects —
    exactly one feature:

    * no ``omit-summary``     → a ``SUMMARY:`` line is present.
    * no ``skip-citations``   → a ``[source: ...]`` citation is present.
    * ``verbose-prose``       → the long filler paragraph is appended.
    * ``fabricate-metrics``   → an unverified ``METRIC-CLAIM:`` line is
      appended.
    """
    parts = [f"NOTE: {entry_input.strip()} — handled deterministically."]
    if "omit-summary" not in tokens:
        parts.append("SUMMARY: key points captured.")
    if "skip-citations" not in tokens:
        parts.append("[source: workspace-records]")
    if "fabricate-metrics" in tokens:
        parts.append("METRIC-CLAIM: growth 99.9% (unverified).")
    if "verbose-prose" in tokens:
        parts.append(_FILLER)
    return "\n".join(parts)


def _drift_event(run_id: str, sequence: int, token: str) -> Any:
    """Build one ``drift_detected`` frame for a remaining defect token.

    Constructed directly on the proto (via :func:`goldfive.events.new_event`
    plus the ``goldfive.v1`` enum values) so the ``kind`` / ``severity``
    fields land on the wire exactly as the reducer's normaliser expects:
    kind ``unexpected_output``, severity ``info`` — a ``1.0``
    contribution per frame under the example contract's
    ``severity_weights`` (and any contract keeping ``info`` at ``1.0``).
    """
    from goldfive.events import new_event  # noqa: PLC0415
    from goldfive.pb.goldfive.v1 import types_pb2  # noqa: PLC0415

    evt = new_event(run_id, sequence)
    evt.drift_detected.kind = types_pb2.DriftKind.Value("DRIFT_KIND_UNEXPECTED_OUTPUT")
    evt.drift_detected.severity = types_pb2.DriftSeverity.Value("DRIFT_SEVERITY_INFO")
    evt.drift_detected.detail = f"planted defect token: {token}"
    return evt


class _PolicySession:
    """One loaded generation: synthesise output + frames from the policy."""

    def __init__(self, generation_root: Path) -> None:
        self._generation_root = Path(generation_root)

    async def run(self, entry: Any, sinks: Any, config: Any) -> RunResult:
        """Drive one board entry deterministically (rich session shape).

        Reads ``agent/policy.py`` from THIS session's generation root —
        the per-run snapshot copy the worker mounted — so the output is
        a pure function of the generation under evaluation. Emits the
        real goldfive lifecycle frames through ``sinks`` (the worker's
        JSONL persistence sink), then returns the :class:`RunResult`
        the worker evaluates the entry's predicate expectation against.
        """
        del config
        started = time.monotonic()
        run_id = f"conv-{entry.id}"

        policy_path = self._generation_root / POLICY_RELPATH
        try:
            policy_source = policy_path.read_text(encoding="utf-8")
        except OSError:
            policy_source = ""
        tokens = parse_style_tokens(policy_source)
        final_output = synthesize_output(str(getattr(entry, "input", "") or ""), tokens)

        # Emit the lifecycle frames: run_started, one drift_detected per
        # remaining defect token, run_completed. Guarded on goldfive being
        # importable so the adapter degrades (no frames, zero drift) in a
        # stripped environment where the worker attached no sinks anyway.
        try:
            from goldfive.events import (  # noqa: PLC0415
                emit,
                run_completed_event,
                run_started_event,
            )

            sink_list = list(sinks or [])
            if sink_list:
                seq = 1
                await emit(
                    sink_list,
                    run_started_event(
                        run_id=run_id,
                        sequence=seq,
                        goal_summary=str(getattr(entry, "input", "") or ""),
                    ),
                )
                for token in tokens:
                    seq += 1
                    await emit(sink_list, _drift_event(run_id, seq, token))
                seq += 1
                await emit(
                    sink_list,
                    run_completed_event(
                        run_id=run_id,
                        sequence=seq,
                        outcome_summary=f"deterministic note with {len(tokens)} defect(s)",
                    ),
                )
        except ModuleNotFoundError:
            pass

        runtime_ms = max(1, int((time.monotonic() - started) * 1000))
        return RunResult(
            run_id=run_id,
            entry_id=str(entry.id),
            final_output=final_output,
            transcript=(final_output,),
            runtime_ms=runtime_ms,
        )


class DeterministicPolicyAdapter:
    """Adapter whose sessions score a snapshot's policy deterministically.

    ``load`` captures the generation root it is handed (the worker's
    per-run ephemeral snapshot copy) and passes it to the session, so
    each run reads the policy of exactly the generation under
    evaluation. ``mutation_points()`` returns the empty list — the
    orchestrator enumerates the ``# zicato:mutable`` markers from the
    snapshot itself.
    """

    name = "deterministic_policy"

    def load(self, generation_root: Path) -> _PolicySession:
        return _PolicySession(generation_root)

    def mutation_points(self, source_roots: Any = None) -> list[Any]:
        del source_roots
        return []

    def worker_spec(self) -> dict[str, Any]:
        """The subprocess-worker reconstruction spec (kind='import').

        The same shape :func:`zicato.adapter_factory.make_adapter_from_config`
        accepts in ``config.json``, so the workspace declares this
        adapter honestly and the worker rebuilds the identical object.
        """
        return {
            "kind": "import",
            "factory": "zicato_examples.target_0_convergence.harness:make_adapter",
        }


def make_adapter() -> DeterministicPolicyAdapter:
    """Module-level factory for the ``import`` adapter spec."""
    return DeterministicPolicyAdapter()


__all__ = [
    "DeterministicPolicyAdapter",
    "KNOWN_DEFECTS",
    "POLICY_RELPATH",
    "make_adapter",
    "parse_style_tokens",
    "synthesize_output",
]
