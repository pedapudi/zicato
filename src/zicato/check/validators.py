"""What the gate can prove about a workspace before it spends a round.

Everything here is provable from the workspace alone, with no model call
and no board entry. A validator yields ``(code, summary, detail)`` for
each defect it finds and reports EVERY one rather than stopping at the
first, so an operator fixes a batch instead of rediscovering the next
failure after each round.

Two severities, and the line between them is what the finding proves:

* a **stop** proves the round cannot produce a valid measurement — a
  duplicated mutation id, no surface at all, an adapter no worker can
  rebuild, an unreadable contract, a role whose credential is not set, or
  declared contract behavior that the selected adapter cannot execute;
* an **advisory** identifies stale annotations that contribute nothing — a
  stale tree path or a span marker that binds to no literal. Those workspaces
  run today, and turning stale-annotation hygiene into a refusal would stop
  runs that were fine.

The one thing a validator must never do is guess. A check that cannot
tell a defect from a legitimate configuration does not belong here at
either severity: a finding nobody can act on trains operators to skim
the block that also carries the stops.

Adding a validator is appending to :data:`VALIDATORS`; making one
advisory is naming its code in :data:`ADVISORY_CODES`.
"""

from __future__ import annotations

import importlib
import json
import os
import signal
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

#: Codes that are reported but do not stop the run. Each names something
#: an operator declared that contributes nothing — true, worth saying,
#: and not a reason to refuse a workspace that runs today.
ADVISORY_CODES: frozenset[str] = frozenset(
    {
        "missing_mutable_tree",
        "tree_enumerates_to_nothing",
        "unbound_span_marker",
        "goldfive_endpoint_revision_unset",
    }
)

#: Seconds the adapter-import subprocess may take. Generous: the point
#: is to catch an import that FAILS rather than one that is slow.
_IMPORT_TIMEOUT_S = 60


def _module_importable(name: str) -> bool:
    """Return whether importing an optional runtime module succeeds."""
    try:
        importlib.import_module(name)
    except Exception:  # noqa: BLE001 - a broken optional install is unavailable
        return False
    return True


#: Rebuilds and loads the adapter the way every tournament worker does.
#: Run in a fresh interpreter so a factory that only works in the parent
#: process is caught here rather than in every worker, mid-round.
_IMPORT_PROBE = """
import json, sys
from pathlib import Path
from zicato.adapter_factory import make_adapter_from_spec
from zicato.import_path import import_dotted_path
adapter = make_adapter_from_spec(json.loads(sys.argv[1]))
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
    board. That is the stop. A single declared tree contributing nothing
    while others carry surface is an advisory — usually a stale path, and
    the loop still learns from the rest.
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
    if ctx.mutable_trees_error is not None:
        yield (
            "mutable_trees_unresolvable",
            "the adapter raised while resolving its mutable roots",
            {"snapshot": str(snapshot), "error": ctx.mutable_trees_error},
        )
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


#: What each unbound-marker reason means to an operator, and the fix.
_UNBOUND_SPAN_REASONS = {
    "not_a_python_file": (
        "a span marker binds to a Python string literal and this file is not "
        "Python; use the region form (:code ... :end) or :file instead"
    ),
    "no_string_literal": (
        "no string literal follows the marker, so it binds to nothing; move it "
        "directly above the literal it should expose"
    ),
}


def _unbound_span_markers(ctx: CheckContext) -> Iterator[Defect]:
    """Span markers that bind to no literal, so they contribute no point.

    The enumerator resolves these during the context's one runtime-
    equivalent walk and hands the facts back structurally, so validation
    never performs a divergent second walk and never has to read prose
    out of a log to learn what happened. Both ways a marker fails to bind
    are covered: a bare span marker in a non-Python file, and one in a
    Python file with no literal beneath it.

    Advisory: the file still looks annotated and the operator almost
    certainly meant it to be surface, but the run itself is unaffected —
    it simply has one fewer mutation point than the markup suggests.
    """
    for marker in ctx.unbound_span_markers:
        yield (
            "unbound_span_marker",
            f"span marker id={marker.id!r} binds to no string literal",
            {
                "mutation_id": marker.id,
                "location": f"{marker.file}:{marker.line}",
                "reason": _UNBOUND_SPAN_REASONS.get(marker.reason, marker.reason),
            },
        )


def adapter_imports(ctx: CheckContext) -> Iterator[Defect]:
    """The adapter must rebuild in a FRESH interpreter, as workers do.

    Every board run is its own subprocess that reconstructs the adapter
    from a serialised spec. An adapter that resolves only in the parent
    process — a factory depending on parent state, a module not on the
    subprocess path — imports fine here and fails in every worker.

    The probe runs under exactly the environment a worker would get
    (:attr:`~zicato.check.context.CheckContext.worker_env`), so on a
    workspace that scrubs the worker env an adapter needing a dropped
    variable is caught here rather than in every worker.
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

    timed_out, returncode, stderr = _run_import_probe(
        spec, snapshot=str(ctx.generation_snapshot or ""), env=ctx.worker_env
    )
    if timed_out:
        yield (
            "adapter_import_timeout",
            f"rebuilding the adapter took longer than {_IMPORT_TIMEOUT_S}s in a fresh process",
            {"kind": spec.get("kind")},
        )
        return
    if returncode != 0:
        last_line = stderr.strip().splitlines()[-1:]
        yield (
            "adapter_import_failed",
            "the adapter does not rebuild in a fresh process, so no worker can run it",
            {"kind": spec.get("kind"), "error": last_line[0] if last_line else ""},
        )


def _run_import_probe(
    spec: dict[str, Any], *, snapshot: str, env: dict[str, str] | None
) -> tuple[bool, int, str]:
    """Run the probe, bounded. Returns ``(timed_out, returncode, stderr)``.

    Not ``subprocess.run(timeout=...)``: that keeps waiting on the output
    pipes after the timeout fires, so an adapter whose import leaves a
    grandchild holding them blocks the gate well past its bound. The
    probe gets its own process group and the whole group is signalled, so
    no descendant can hold the gate open.
    """
    proc = subprocess.Popen(  # noqa: S603 — fixed argv, spec is JSON
        [sys.executable, "-c", _IMPORT_PROBE, json.dumps(spec), snapshot],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=True,
    )
    try:
        _, stderr = proc.communicate(timeout=_IMPORT_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        _kill_process_group(proc)
        try:
            # The group has been SIGKILLed; this only reaps it. Whatever it
            # yields is discarded — the verdict is already the timeout.
            proc.communicate(timeout=_IMPORT_TIMEOUT_S)
        except subprocess.TimeoutExpired:  # pragma: no cover — after SIGKILL
            pass
        return True, -1, ""
    return False, proc.returncode, stderr


def _kill_process_group(proc: subprocess.Popen[str]) -> None:
    """SIGKILL the probe's whole process group, descendants included."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (OSError, AttributeError):
        # No process groups (Windows), or the group is already gone.
        proc.kill()


def model_roles(ctx: CheckContext) -> Iterator[Defect]:
    """Every configured model role must resolve.

    A configured ``models.<role>`` block wins over the callable the CLI
    was given: the worker re-resolves the role from its secret-free spec
    in its own interpreter (``worker_transport._role_worker_spec``). Two
    things about such a role are provable here, both free:

    * a ``call_llm`` dotted path either imports to a callable or does not;
    * a model spec naming an ``api_key_env`` either finds that variable
      set or does not. ``models_config.build_adk_model`` raises when it is
      unset, and a scrubbed worker env can only forward a variable the
      orchestrator's own environment already holds — so absence here is
      absence in the worker.

    An unconfigured role is skipped: it falls back to the callable the
    caller passed, which the CLI resolves and reports itself.

    What is NOT proved is that the credential is accepted or that the
    model id exists. That needs a round trip, which this gate does not
    make: :mod:`zicato.check.reachability` makes it, on ``zicato evolve
    --dry-run`` only, over the same set of configured roles.
    """
    from zicato.models_config import MODEL_ROLES  # noqa: PLC0415

    worker_env = ctx.worker_env
    available_env = os.environ if worker_env is None else worker_env
    for role in MODEL_ROLES:
        try:
            spec = ctx.models.role(role)
        except (AttributeError, ValueError) as exc:
            yield (
                "model_role_unreadable",
                f"the models block for role {role!r} cannot be read",
                {"role": role, "error": str(exc)},
            )
            continue
        if spec.is_empty:
            continue
        if spec.uses_call_llm:
            yield from _unresolvable(
                "model_role_unresolvable",
                str(spec.call_llm),
                f"models.{role}.call_llm",
                extra={"role": role},
            )
            continue
        env_name = spec.api_key_env
        if env_name and not available_env.get(env_name):
            yield (
                "model_role_credential_unset",
                f"role {role!r} authenticates with {env_name}, which is not set in the worker",
                {"role": role, "api_key_env": env_name},
            )


def goldfive_integration(ctx: CheckContext) -> Iterator[Defect]:
    """A Goldfive-consuming adapter must bind behavior and endpoint secrets."""

    if ctx.scoring_error is not None:
        return
    if not ctx.has_evaluation_contract:
        return
    from zicato.tournament.worker_transport import adapter_uses_integration  # noqa: PLC0415

    uses_goldfive = adapter_uses_integration(ctx.adapter_spec, "goldfive")
    config = ctx.scoring.goldfive
    if config is not None and not uses_goldfive:
        yield (
            "goldfive_config_unused",
            "scoring.goldfive is configured, but the selected adapter does not declare the "
            "Goldfive integration",
            {"adapter_kind": (ctx.adapter_spec or {}).get("kind")},
        )
        return
    if uses_goldfive and config is None:
        yield (
            "goldfive_config_missing",
            "the selected adapter uses Goldfive and requires scoring.goldfive so its behavior "
            "is part of the contract",
            {"fix": 'add "goldfive": {} to scoring.json to select fixed defaults'},
        )
        return
    if config is None:
        return

    if not _module_importable("goldfive.events") or not _module_importable("google.protobuf"):
        yield (
            "goldfive_runtime_unavailable",
            "the selected adapter declares Goldfive, but its event runtime is not installed",
            {"fix": "install zicato[goldfive]"},
        )
        return
    from zicato.integrations.goldfive import (  # noqa: PLC0415
        GOLDFIVE_IMPLEMENTATION_VERSION,
        installed_goldfive_implementation_version,
        missing_runtime_capabilities,
        normalize_config,
    )

    installed_version = installed_goldfive_implementation_version()
    if installed_version != GOLDFIVE_IMPLEMENTATION_VERSION:
        yield (
            "goldfive_implementation_mismatch",
            "the installed Goldfive implementation does not match this Zicato build",
            {
                "required": GOLDFIVE_IMPLEMENTATION_VERSION,
                "installed": installed_version,
                "fix": "install the pinned Zicato dependency",
            },
        )
        return
    try:
        normalized = normalize_config(config)
    except (ImportError, AttributeError, TypeError, ValueError) as exc:
        yield (
            "goldfive_config_invalid",
            f"scoring.goldfive is invalid: {exc}",
            {},
        )
        return

    for missing in missing_runtime_capabilities(config):
        yield (
            "goldfive_runtime_capability_missing",
            f"the configured Goldfive runtime cannot load {missing}",
            {"missing": missing},
        )

    available_env = os.environ if ctx.worker_env is None else ctx.worker_env
    for endpoint_name in ("embedding", "judge"):
        endpoint = normalized.get(endpoint_name)
        if not isinstance(endpoint, dict):  # pragma: no cover - Goldfive API invariant
            continue
        env_name = endpoint.get("api_key_env")
        if env_name and not available_env.get(env_name):
            yield (
                "goldfive_credential_unset",
                f"scoring.goldfive.{endpoint_name}.api_key_env names {env_name}, which is not set",
                {"endpoint": endpoint_name, "api_key_env": env_name},
            )
        if endpoint.get("base_url") and not endpoint.get("revision"):
            yield (
                "goldfive_endpoint_revision_unset",
                f"scoring.goldfive.{endpoint_name} uses a remote endpoint without a stable "
                "model revision",
                {"endpoint": endpoint_name},
            )


def epoch_implementation_identity(ctx: CheckContext) -> Iterator[Defect]:
    """A frozen epoch may run only under the implementations that created it."""
    if ctx.uses_live_contract or ctx.epoch_id is None or ctx.scoring_error is not None:
        return

    config_path = ctx.workspace_root / "epochs" / ctx.epoch_id / "config.json"
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        yield (
            "epoch_implementation_identity_unreadable",
            f"the selected epoch's implementation identity cannot be read from {config_path}",
            {"error": str(exc)},
        )
        return
    recorded = raw.get("implementation_identity") if isinstance(raw, dict) else None
    if not isinstance(recorded, dict):
        yield (
            "epoch_implementation_identity_unreadable",
            "the selected epoch does not record an implementation_identity object",
            {"path": str(config_path)},
        )
        return

    from zicato.epoch.contract import evaluation_implementation_identity  # noqa: PLC0415

    expected = evaluation_implementation_identity(ctx.scoring)
    if recorded != expected:
        yield (
            "epoch_implementation_identity_mismatch",
            "the selected epoch was created by different evaluation implementations",
            {"recorded": recorded, "required": expected},
        )


def frozen_epoch_contract_identity(ctx: CheckContext) -> Iterator[Defect]:
    """A selected frozen epoch must still match every recorded component."""
    if ctx.uses_live_contract or ctx.epoch_id is None or ctx.scoring_error is not None:
        return

    from dataclasses import replace  # noqa: PLC0415

    from zicato.core.workspace import scoring_path  # noqa: PLC0415
    from zicato.epoch.contract import (  # noqa: PLC0415
        compute_component_hashes,
        compute_contract_hash,
        resolve_contract_inputs,
    )
    from zicato.epoch.lifecycle import load_epoch  # noqa: PLC0415
    from zicato.workspace import WorkspaceLayout  # noqa: PLC0415

    try:
        epoch = load_epoch(ctx.workspace_root, ctx.epoch_id)
        stored_path = WorkspaceLayout.from_root(ctx.workspace_root).contract_components(
            ctx.epoch_id
        )
        raw_components = json.loads(stored_path.read_text(encoding="utf-8"))
        if not isinstance(raw_components, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in raw_components.items()
        ):
            raise ValueError("expected a JSON object of string component hashes")
        current = resolve_contract_inputs(ctx.workspace_root)
        frozen = replace(
            current,
            board_path=epoch.board_path,
            brief_path=epoch.brief_path,
            scoring_path=scoring_path(ctx.workspace_root, ctx.epoch_id),
            proposer_path=epoch.proposer_path,
        )
        required_hash = compute_contract_hash(frozen)
        required_components = compute_component_hashes(frozen)
    except Exception as exc:  # noqa: BLE001 — aggregate operator adapter failures
        yield (
            "frozen_epoch_contract_unreadable",
            "the selected epoch's frozen contract identity cannot be verified",
            {"error": str(exc)},
        )
        return

    stored_components = {str(key): str(value) for key, value in raw_components.items()}
    changed = sorted(
        name
        for name in set(stored_components) | set(required_components)
        if stored_components.get(name) != required_components.get(name)
    )
    if epoch.contract_hash != required_hash or changed:
        yield (
            "frozen_epoch_contract_mismatch",
            "the selected epoch does not match its recorded evaluation contract",
            {
                "changed_components": changed or ["contract"],
                "recorded_contract_hash": epoch.contract_hash,
                "required_contract_hash": required_hash,
            },
        )


def contract_integrity(ctx: CheckContext) -> Iterator[Defect]:
    """The board and the scoring must agree with each other.

    Each of these fails a round rather than the whole run, which is
    worse: the round completes, the scored namespace is silently absent,
    and the loss looks like a measurement rather than a defect.

    Note what is NOT checked: a ``per_judge_weights`` key
    naming no board judge. Those weights are not scoped to board judges.
    :mod:`zicato.scoring.builtins` resolves telemetry ``custom:<name>``
    kinds through the same mapping, so a key legitimately names an
    in-harness process judge; the empty-string key weights the bare
    ``custom`` kind; and :mod:`zicato.reflection.findings` recommends a
    ``{name: 0.0}`` entry as the reversible way to retire a judge.
    Nothing available here tells any of that apart from a typo, so the
    check would refuse workspaces that run correctly today.
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

    for entry in ctx.board:
        for judge in entry.judges:
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


def _unresolvable(
    code: str, dotted: str, where: str, *, extra: dict[str, Any] | None = None
) -> Iterator[Defect]:
    """Yield a defect when ``dotted`` does not import."""
    try:
        import_dotted_path(dotted, label=where)
    except Exception as exc:  # noqa: BLE001 — any import failure is the defect
        yield (code, f"{where} does not resolve: {dotted}", {**(extra or {}), "error": str(exc)})


#: Every validator, in report order. Append to extend.
VALIDATORS: tuple[Callable[[CheckContext], Iterator[Defect]], ...] = (
    duplicate_ids,
    dead_surface,
    adapter_imports,
    model_roles,
    goldfive_integration,
    epoch_implementation_identity,
    frozen_epoch_contract_identity,
    contract_integrity,
)


__all__ = ["ADVISORY_CODES", "VALIDATORS", "Defect"]
