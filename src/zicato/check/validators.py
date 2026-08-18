"""The hard stops — provable defects that fail the run.

Everything here is provable from the workspace alone, with no model
call and no board entry. A validator yields ``(code, summary, detail)``
for each defect it finds and reports EVERY one rather than stopping at
the first, so an operator fixes a batch instead of rediscovering the
next failure after each round.

Adding a validator is appending to :data:`VALIDATORS`.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable, Iterator
from typing import Any

from zicato.check.context import CheckContext
from zicato.core.board import ExpectationKind, JudgeMode
from zicato.import_path import import_dotted_path
from zicato.mutation.validator import duplicate_mutation_ids

#: What a validator yields: stable code, one-line summary, structured
#: detail (JSON-friendly, so the report round-trips).
Defect = tuple[str, str, dict[str, Any]]

#: Seconds the adapter-import subprocess may take. Generous: the point
#: is to catch an import that FAILS, not one that is slow.
_IMPORT_TIMEOUT_S = 60

#: Rebuilds and loads the adapter the way every tournament worker does.
#: Run in a fresh interpreter so a factory that only works in the parent
#: process is caught here rather than in every worker, mid-round.
_IMPORT_PROBE = """
import json, sys
from pathlib import Path
from zicato._tournament_worker import _build_adapter
from zicato.import_path import import_dotted_path
adapter = _build_adapter(json.loads(sys.argv[1]))
root = Path(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2] else None
if root is not None:
    resolver = getattr(adapter, "mutable_subpaths", None)
    if callable(resolver):
        resolver(root)
    adapter.load(root)
elif json.loads(sys.argv[1]).get("kind") == "adk":
    import_dotted_path(json.loads(sys.argv[1])["entrypoint"], label="ADK entrypoint")
"""


def duplicate_ids(ctx: CheckContext) -> Iterator[Defect]:
    """Mutation ids that resolve to more than one point.

    ``validate_patches`` already rejects these, but only for ids a patch
    targets and only at apply time — after the proposer has spent its
    tokens. The invariant is global, so check it globally, before spend.
    """
    if ctx.adapter_error is not None or ctx.scoring_error is not None:
        return
    for mutation_id, locations in sorted(duplicate_mutation_ids(ctx.surface).items()):
        yield (
            "duplicate_mutation_id",
            f"mutation id {mutation_id!r} resolves to {len(locations)} points",
            {"mutation_id": mutation_id, "locations": sorted(locations)},
        )


def dead_surface(ctx: CheckContext) -> Iterator[Defect]:
    """A mutation surface the proposer cannot actually edit.

    An empty surface means the loop cannot learn: the proposer has
    nothing to change, so every round is a no-op that still costs a
    board. A declared tree contributing nothing is the same defect
    scoped to one tree, and is usually a stale path.
    """
    snapshot = ctx.generation_snapshot
    if snapshot is None:
        yield (
            "no_mutable_trees",
            "no mutable surface is available, so nothing can be proposed",
            {},
        )
        return

    if ctx.uses_temporary_snapshot:
        for tree in ctx.registered_trees:
            if not tree.exists():
                yield (
                    "missing_mutable_tree",
                    f"declared mutable tree {tree} does not exist",
                    {"tree": str(tree)},
                )

    if ctx.adapter_error is not None or ctx.scoring_error is not None:
        return
    roots = ctx.mutable_trees
    if not roots:
        yield (
            "empty_mutation_surface",
            "the adapter resolves no mutable roots in the generation snapshot",
            {"snapshot": str(snapshot)},
        )
        return

    for tree in roots:
        if not tree.exists():
            yield (
                "missing_mutable_tree",
                f"declared mutable tree {tree} does not exist",
                {"tree": str(tree)},
            )

    if not ctx.surface:
        yield (
            "empty_mutation_surface",
            "the active surface enumerates to zero mutation points",
            {"trees": [str(tree) for tree in roots]},
        )
        return

    counts = {tree.resolve(): 0 for tree in roots if tree.exists()}
    for point in ctx.surface:
        resolved_file = point.file.resolve()
        for prefix in counts:
            if resolved_file == prefix or resolved_file.is_relative_to(prefix):
                counts[prefix] += 1
    for prefix, count in sorted(counts.items(), key=lambda item: str(item[0])):
        if count == 0:
            yield (
                "tree_enumerates_to_nothing",
                f"mutable tree {prefix} contributes no mutation point",
                {"tree": str(prefix)},
            )

    yield from _unbound_span_markers(ctx)


def _unbound_span_markers(ctx: CheckContext) -> Iterator[Defect]:
    """Span markers that bind to no literal.

    The enumerator already detects these and logs a WARNING naming the
    fix. The context captures those warnings during its one runtime-
    equivalent walk so validation never performs a divergent second walk.
    """
    for warning in ctx.surface_warnings:
        if "span marker" in warning:
            yield (
                "unbound_span_marker",
                "a span marker binds to no string literal, so it contributes no point",
                {"detail": warning},
            )


def adapter_imports(ctx: CheckContext) -> Iterator[Defect]:
    """The adapter must rebuild in a FRESH interpreter, as workers do.

    Every board run is its own subprocess that reconstructs the adapter
    from a serialised spec. An adapter that resolves only in the parent
    process — a factory depending on parent state, a module not on the
    subprocess path — imports fine here and fails in every worker.
    """
    if not ctx.has_adapter_config:
        yield ("no_adapter", "no adapter is configured, so no board entry can run", {})
        return
    if ctx.adapter_error is not None:
        yield (
            "adapter_import_failed",
            "the configured adapter cannot be constructed for worker execution",
            {"error": ctx.adapter_error},
        )
        return
    spec = ctx.adapter_spec
    if spec is None:
        yield (
            "adapter_import_failed",
            "the configured adapter does not provide a worker specification",
            {},
        )
        return

    try:
        completed = subprocess.run(  # noqa: S603 — fixed argv, spec is JSON
            [
                sys.executable,
                "-c",
                _IMPORT_PROBE,
                json.dumps(spec),
                str(ctx.generation_snapshot or ""),
            ],
            capture_output=True,
            text=True,
            timeout=_IMPORT_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        yield (
            "adapter_import_timeout",
            f"rebuilding the adapter took longer than {_IMPORT_TIMEOUT_S}s in a fresh process",
            {"kind": spec.get("kind")},
        )
        return

    if completed.returncode != 0:
        yield (
            "adapter_import_failed",
            "the adapter does not rebuild in a fresh process, so no worker can run it",
            {"kind": spec.get("kind"), "error": completed.stderr.strip().splitlines()[-1:]},
        )


def contract_integrity(ctx: CheckContext) -> Iterator[Defect]:
    """The board and the scoring must agree with each other.

    Each of these fails a round rather than the whole run, which is
    worse: the round completes, the scored namespace is silently absent,
    and the loss looks like a measurement rather than a defect.
    """
    if ctx.board_error is not None:
        yield ("board_unreadable", "the board cannot be read", {"error": ctx.board_error})
        return
    if ctx.scoring_error is not None:
        yield (
            "scoring_unreadable",
            "the scoring contract cannot be read or validated",
            {"error": ctx.scoring_error},
        )
        return
    if not ctx.has_evaluation_contract:
        return

    if not ctx.board:
        yield ("empty_board", "the board has no entries, so nothing is evaluated", {})
        return

    judge_names: set[str] = set()
    for entry in ctx.board:
        for judge in entry.judges:
            judge_names.add(judge.name)
            if judge.mode is JudgeMode.PYTHON:
                yield from _unresolvable(
                    "judge_unresolvable", judge.body, f"judge {judge.name!r} on {entry.id!r}"
                )
        if entry.expectation is not None and entry.expectation.kind is ExpectationKind.PREDICATE:
            yield from _unresolvable(
                "predicate_unresolvable",
                entry.expectation.spec,
                f"predicate on entry {entry.id!r}",
            )

    for name in sorted(set(ctx.scoring.per_judge_weights) - judge_names):
        yield (
            "weight_for_absent_judge",
            f"per_judge_weights names {name!r}, which no board entry declares",
            {"judge": name, "declared": sorted(judge_names)},
        )


def _unresolvable(code: str, dotted: str, where: str) -> Iterator[Defect]:
    """Yield a defect when ``dotted`` does not import."""
    try:
        import_dotted_path(dotted, label=where)
    except Exception as exc:  # noqa: BLE001 — any import failure is the defect
        yield (code, f"{where} does not resolve: {dotted}", {"error": str(exc)})


#: Every validator, in report order. Append to extend.
VALIDATORS: tuple[Callable[[CheckContext], Iterator[Defect]], ...] = (
    duplicate_ids,
    dead_surface,
    adapter_imports,
    contract_integrity,
)


__all__ = ["VALIDATORS", "Defect"]
