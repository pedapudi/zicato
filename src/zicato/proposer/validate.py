"""Check a proposed patch set against its captured parent without evaluating tasks.

:func:`validate_patches` applies a draft patch set to a scratch copy of the
parent snapshot and reports failures that the proposer can correct before
submitting it. Checks cover patch structure, source constraints, declared
static analysis, and loading the configured harness.

Source checks and loading
-------------------------
Patch structure and application must succeed before static analysis or the
load probe runs. Static-analysis findings and load failures are then
reported together.

1. **Structure and application.** Validate the patch array, mutation ids,
   operations, payloads, numeric ranges, and enum values. The captured
   mutation policy verifies the parent snapshot and rejects forbidden
   edits. Apply accepted patches to a scratch copy, then check Python
   syntax, mutation-point preservation, required placeholders, and imports
   through :func:`zicato.mutation.validator.validate_post_apply`.
2. **Static analysis.** Run the workspace's declared checks on the parent
   and scratch trees. Report findings introduced by the patch; existing
   findings do not reject it. :data:`STATIC_CHECKS` owns the supported
   checks and :func:`declared_static_checks` reads their declaration.
3. **Load probe.** Resolve the configured harness against the scratch
   snapshot in a subprocess with a timeout. Loading exercises the same
   ``adapter.load`` call used before tournament execution.

Evaluation isolation
--------------------
The proposer may inspect source and load a harness, but must not consume
board data, produce scores, or execute board entries. This module's import
closure excludes :mod:`zicato.board`, :mod:`zicato.adapters`,
:mod:`zicato.adapter_factory`, :mod:`zicato._tournament_worker`,
:mod:`zicato.emulator`, and :mod:`zicato.judge_runtime`. The import contract
and the transitive closure test in ``tests/test_proposer_validate.py``
enforce that restriction.

The load probe runs :mod:`zicato.proposer._load_probe` as a subprocess so
this module does not import the adapter factory. The active context comes
from :mod:`zicato.proposer.tool_context`; importing the broader proposer
read module would also import the board loader.

Parent source identity
----------------------
The captured mutation policy checks the complete parent source identity
and mutation snapshot before application. A proposal cannot use a stale
snapshot even when a changed parent file lies outside its own patch. The
policy also rejects forbidden targets and forbidden nested regions changed
by an allowed whole-file replacement. Episode verification and generation
application use the same policy.

The host computes this identity from source and the frozen mutation
snapshot. The proposer supplies no hashes or additional patch fields.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import jsonschema

from zicato.core.types import Patch
from zicato.mutation.policy import MutationPolicy
from zicato.proposer.structured import (
    PATCHES_JSON_SCHEMA,
    ExperimentParseError,
    parse_patch_list,
)
from zicato.proposer.tool_context import ProposerToolContext, _active_context
from zicato.workspace.config_io import read_workspace_config, workspace_is_initialized

#: Prefix for the throwaway parent dir a validation allocates in the OS temp
#: root. Deliberately DISTINCT from ``ztw-slate-`` so the round pipeline's
#: stale-slate sweep (:func:`zicato.evolve.round._sweep_stale_slate_scratch`)
#: can never reap a live validation's tree. Placed outside the workspace so
#: nothing under it can be mistaken for a canonical generation snapshot, and
#: removed in a ``finally``; a SIGKILL that skips the ``finally`` leaks one
#: dir, which the OS temp cleaner collects.
SCRATCH_PREFIX = "ztw-pvalidate-"

#: Per-check wall-clock ceiling for a declared static check, in seconds. A
#: linter that has not answered in two minutes on one snapshot is wedged;
#: the check is reported as timed out rather than hanging the proposer.
STATIC_CHECK_TIMEOUT_SECONDS = 120.0

#: Wall-clock ceiling for the harness load probe, in seconds. Importing a
#: harness entry point is fast; a probe that exceeds this is reported as a
#: timeout, which is itself a finding worth surfacing (an import that hangs
#: would hang every tournament run too).
LOAD_PROBE_TIMEOUT_SECONDS = 60.0

#: Cap on the characters of any single tool/probe output echoed back to the
#: proposer, mirroring the runaway-context guards on the read tools.
_OUTPUT_LIMIT_CHARS = 4_000


def _argv_ruff(root: Path) -> list[str]:
    return [sys.executable, "-m", "ruff", "check", "--no-cache", "--quiet", str(root)]


def _argv_ruff_format(root: Path) -> list[str]:
    return [sys.executable, "-m", "ruff", "format", "--check", "--no-cache", str(root)]


def _argv_mypy(root: Path) -> list[str]:
    return [sys.executable, "-m", "mypy", "--no-error-summary", "--no-color-output", str(root)]


def _argv_compileall(root: Path) -> list[str]:
    return [sys.executable, "-m", "compileall", "-q", str(root)]


#: The closed registry of static checks a workspace may declare, by
#: name. A closed registry — rather than an operator-supplied argv — is
#: deliberate: the declared set is folded into the contract hash, and a
#: hashed *name* is a stable, reviewable identity while a hashed command
#: line is an arbitrary-execution surface that a contract edit could widen
#: silently. Every check is invoked through ``sys.executable -m`` so it
#: resolves to the tools already installed in zicato's own environment.
#: A workspace needing a checker that is not here should propose adding it
#: to this registry rather than gaining a way to name any command.
STATIC_CHECKS: Mapping[str, Any] = {
    "ruff": _argv_ruff,
    "ruff-format": _argv_ruff_format,
    "mypy": _argv_mypy,
    "compileall": _argv_compileall,
}


def declared_static_checks(
    workspace_root: Path, *, workspace_config: Mapping[str, Any] | None = None
) -> tuple[str, ...]:
    """Return the workspace's declared static-check names, in order.

    Read from ``{workspace_root}/config.json`` at
    ``contract.proposer_static_checks`` — the same ``contract`` block that
    carries ``proposer_path``, because this set is contract, not
    configuration: changing which checks the proposer holds itself to
    changes which patches it will accept from itself, hence what it
    proposes. :func:`zicato.epoch.contract.resolve_contract_inputs` reads
    the key through this same function and folds it into the proposer
    component of the contract hash.

    A supplied workspace_config is authoritative. This keeps contract capture
    and validation independent of subsequent live config edits.

    Nonempty string names are retained in declaration order.
    :func:`run_static_checks` reports unknown names as notes. An absent
    key, an unreadable config, or a malformed value yields ``()``, so no
    static checks run and the declaration contributes no contract input.
    """
    if workspace_config is None:
        try:
            contract = read_workspace_config(workspace_root).contract
        except (OSError, ValueError):
            return ()
    else:
        raw_contract = workspace_config.get("contract", {})
        if not isinstance(raw_contract, Mapping):
            return ()
        contract = raw_contract
    declared = contract.get("proposer_static_checks")
    if not isinstance(declared, list):
        return ()
    return tuple(str(name) for name in declared if isinstance(name, str) and name)


def _truncate(text: str) -> str:
    """Clip tool output to :data:`_OUTPUT_LIMIT_CHARS` with a note."""
    if len(text) <= _OUTPUT_LIMIT_CHARS:
        return text
    head = text[:_OUTPUT_LIMIT_CHARS]
    return f"{head}\n[... truncated: output exceeds {_OUTPUT_LIMIT_CHARS} chars ...]"


#: Strips the ``:LINE:COL:`` / ``:LINE:`` position that every checker in
#: :data:`STATIC_CHECKS` emits after the file path, so a finding can be
#: compared across two trees whose line numbers a patch has shifted.
_POSITION_RE = re.compile(r"^(?P<path>[^:]*):\d+(?::\d+)?:")


def _normalize_finding(line: str, root: Path) -> str:
    """Reduce one checker output line to a position-independent identity.

    The scratch tree and the parent tree differ in their root path AND in
    every line number below an edit, so a raw string diff would report the
    whole file as new findings. Rebasing the path onto its root and
    dropping the ``line:col`` prefix leaves ``<relative path> <message>``,
    which is stable under an edit elsewhere in the file.

    The normalization is APPROXIMATE in one direction by design: a new
    finding textually identical to a pre-existing one at another line in the
    same file is suppressed. That is the right error
    to make for an advisory linter — a validator that cries wolf on
    pre-existing debt is a validator the proposer learns to ignore.
    """
    line = line.strip().replace(str(root), "").lstrip("/")
    match = _POSITION_RE.match(line)
    if match is None:
        return line
    return f"{match.group('path')}: {line[match.end() :].strip()}"


#: Memo of BASELINE check results, keyed ``(check name, parent snapshot
#: path)``. The parent generation's tree is immutable for the whole round —
#: it is the champion's snapshot, the same immutability the read-only tools
#: already rely on — so its findings are the same on every call. Without
#: this, a proposer that validates five drafts pays for five identical runs
#: of every declared checker over the parent tree, which is half the cost of
#: exactly the draft-fix-revalidate loop this tool exists to make cheap.
#:
#: Only BASELINE results are memoized; the scratch tree is fresh per call
#: and never cached. The dict is per-process and unbounded, which is bounded
#: in practice by (checks declared × generations seen in one orchestrator
#: process) — a handful of entries holding a few lines each.
_BASELINE_CACHE: dict[tuple[str, str], tuple[bool, list[str]]] = {}


def _run_check_cached(name: str, parent_root: Path) -> tuple[bool, list[str]]:
    """:func:`_run_check` over the immutable parent tree, memoized."""
    key = (name, str(parent_root))
    cached = _BASELINE_CACHE.get(key)
    if cached is None:
        cached = _run_check(name, parent_root)
        _BASELINE_CACHE[key] = cached
    ran, lines = cached
    return ran, list(lines)


def _run_check(name: str, root: Path) -> tuple[bool, list[str]]:
    """Run one named static check over ``root``.

    Returns ``(ran, finding_lines)``. ``ran`` is ``False`` when the checker
    is not installed or timed out — the caller degrades those to an
    explicit note rather than to a patch rejection, because a missing dev
    tool is the operator's problem rather than the proposer's.
    """
    builder = STATIC_CHECKS[name]
    try:
        proc = subprocess.run(
            builder(root),
            capture_output=True,
            text=True,
            timeout=STATIC_CHECK_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False, []
    if proc.returncode == 0:
        return True, []
    combined = f"{proc.stdout}\n{proc.stderr}"
    return True, [ln for ln in combined.splitlines() if ln.strip()]


#: Validation returns ``(errors, notes)``. Errors reject the patch; notes
#: describe unavailable checks without rejecting it. The proposer can
#: correct source errors but cannot supply missing workspace dependencies.
TierResult = tuple[list[str], list[str]]


def run_static_checks(
    names: Sequence[str],
    parent_root: Path,
    scratch_root: Path,
) -> TierResult:
    """Run the declared checks on both trees; return the NEW findings.

    Each named check runs over ``parent_root`` (the unpatched snapshot) and
    over ``scratch_root`` (the patched one); only findings present in the
    second and absent from the first — compared through
    :func:`_normalize_finding` — are errors. Existing findings in the
    parent tree do not count against the proposed patch.

    A name not in :data:`STATIC_CHECKS`, and a declared check whose tool is
    absent or which timed out, are NOTES: the operator must learn that
    nothing is running them, but neither is the proposer's to fix.

    Each check therefore runs TWICE the first time it is asked about a
    given parent tree and once thereafter — the baseline half is memoized
    in :data:`_BASELINE_CACHE`, since the parent snapshot is immutable for
    the round.
    """
    errors: list[str] = []
    notes: list[str] = []
    for name in names:
        if name not in STATIC_CHECKS:
            notes.append(
                f"static check {name!r} is not a known check "
                f"(declared in contract.proposer_static_checks; known: "
                f"{', '.join(sorted(STATIC_CHECKS))})"
            )
            continue
        ran_parent, parent_lines = _run_check_cached(name, parent_root)
        ran_scratch, scratch_lines = _run_check(name, scratch_root)
        if not ran_parent or not ran_scratch:
            notes.append(
                f"static check {name!r} could not run (tool not installed, or it "
                f"exceeded {STATIC_CHECK_TIMEOUT_SECONDS:.0f}s)"
            )
            continue
        baseline = {_normalize_finding(ln, parent_root) for ln in parent_lines}
        for raw in scratch_lines:
            if _normalize_finding(raw, scratch_root) in baseline:
                continue
            errors.append(f"{name}: {_truncate(raw.strip())}")
    return errors, notes


def run_load_probe(
    workspace_root: Path, scratch_root: Path, *, adapter_configuration_json: bytes | None = None
) -> TierResult:
    """Probe ``adapter.load`` against ``scratch_root`` in a subprocess.

    Spawns :mod:`zicato.proposer._load_probe` — see that module for why the
    probe is a child process and why it lives outside this one — and turns
    its JSON verdict into a :data:`TierResult`. A harness that fails to
    import is an ERROR carrying the exception line plus its traceback (the
    actionable part: the proposer needs the failing import to fix it). A
    timeout is also an error: an import that hangs here would hang every
    tournament run. A probe that could not run at all — no workspace
    config, an unreadable workspace, no adapter configured — is a NOTE; the
    proposer cannot fix the operator's workspace.
    """
    if adapter_configuration_json is None and not workspace_is_initialized(workspace_root):
        return [], [
            f"load probe skipped: no config.json under {workspace_root} to "
            f"resolve an adapter from"
        ]
    try:
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "zicato.proposer._load_probe",
                str(workspace_root),
                str(scratch_root),
                *(["--adapter-stdin"] if adapter_configuration_json is not None else []),
            ],
            input=None
            if adapter_configuration_json is None
            else adapter_configuration_json.decode(),
            capture_output=True,
            text=True,
            timeout=LOAD_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return [
            f"load probe: adapter.load did not return within "
            f"{LOAD_PROBE_TIMEOUT_SECONDS:.0f}s — an import that hangs here would "
            f"hang every tournament run"
        ], []
    except OSError as exc:
        return [], [f"load probe could not be started: {exc}"]

    if proc.returncode != 0:
        return [], [f"load probe could not run: {_truncate(proc.stderr.strip())}"]
    try:
        verdict = json.loads(proc.stdout.strip() or "{}")
    except json.JSONDecodeError:
        return [], [f"load probe returned unparseable output: {_truncate(proc.stdout.strip())}"]
    if verdict.get("ok"):
        return [], []
    return [
        f"load probe: adapter.load raised {verdict.get('error', '(no error reported)')}",
        f"load probe traceback:\n{_truncate(str(verdict.get('traceback', '')))}",
    ], []


def _coerce_patch_array(patches_json: str) -> list[Any]:
    """Accept either a bare ``[...]`` array or ``{"patches": [...]}``.

    The proposer emits its patch set inside an experiment object, so it
    naturally reaches for the wrapped form; a validator that rejected one
    of the two spellings would spend a retry on punctuation. Both are
    accepted and everything downstream sees the array.
    """
    if not patches_json.strip():
        raise ValueError(
            "validate_patches: expected a JSON array of patch objects (the same "
            "'patches' array you will emit), got an empty string"
        )
    try:
        data = json.loads(patches_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"validate_patches: argument is not valid JSON: {exc}") from exc
    if isinstance(data, Mapping):
        data = data.get("patches")
    if not isinstance(data, list):
        raise ValueError(
            "validate_patches: expected a JSON array of patch objects, or an "
            "object with a 'patches' array"
        )
    return data


def _report(errors: Sequence[str], tiers: Mapping[str, Any]) -> str:
    """Render the tool's JSON verdict. ``ok`` is exactly "no errors"."""
    return json.dumps({"ok": not errors, "errors": list(errors), "tiers": dict(tiers)}, indent=2)


def _validate_against_context(
    raw_patches: Sequence[Mapping[str, Any]],
    ctx: ProposerToolContext,
) -> str:
    """Validate parsed patches against the active proposal context."""
    mutations_by_id = {mp.id: mp for mp in ctx.mutations}
    tiers: dict[str, Any] = {}

    # Validate patch structure and captured parent identity before application.
    structure_errors: list[str] = []
    patches: list[Patch] = []
    try:
        jsonschema.validate(instance=list(raw_patches), schema=PATCHES_JSON_SCHEMA)
    except jsonschema.ValidationError as exc:
        path = "/".join(str(p) for p in exc.absolute_path) or "patches"
        structure_errors.append(f"schema violation at {path}: {exc.message}")
    else:
        try:
            patches = parse_patch_list(raw_patches, mutations_by_id)
        except ExperimentParseError as exc:
            structure_errors.append(str(exc))

    policy = ctx.mutation_policy
    if not structure_errors:
        try:
            if policy is None:
                policy = MutationPolicy.capture(
                    ctx.generation_root, ctx.mutations, ctx.forbidden_ids
                )
            structure_errors.extend(policy.check_patches(patches))
        except (OSError, ValueError) as exc:
            structure_errors.append(str(exc))

    tiers["structure"] = {"ran": True, "errors": structure_errors, "notes": []}
    if structure_errors:
        return _report(structure_errors, tiers)
    assert policy is not None

    # Apply to a scratch copy and verify source constraints.
    from zicato.mutation.applier import apply_patches  # noqa: PLC0415
    from zicato.mutation.validator import validate_post_apply  # noqa: PLC0415

    parent_root = ctx.generation_root.resolve()
    parent = Path(tempfile.mkdtemp(prefix=SCRATCH_PREFIX))
    scratch_root = parent / "child"
    try:
        try:
            apply_patches(
                parent_root, patches, scratch_root, enumeration_roots=policy.enumeration_roots
            )
        except (FileNotFoundError, ValueError, KeyError) as exc:
            apply_errors = [f"the patch set does not apply: {exc}"]
            tiers["apply"] = {"ran": True, "errors": apply_errors, "notes": []}
            return _report(apply_errors, tiers)

        apply_errors = validate_post_apply(
            scratch_root,
            patches,
            list(ctx.mutations),
            enumeration_roots=policy.roots_in(scratch_root),
        )
        apply_errors.extend(policy.check_child(scratch_root))
        tiers["apply"] = {"ran": True, "errors": apply_errors, "notes": []}
        if apply_errors:
            return _report(apply_errors, tiers)

        # Compare declared static checks against the unpatched source.
        names = (
            declared_static_checks(ctx.workspace_root)
            if ctx.static_checks is None
            else ctx.static_checks
        )
        static_errors: list[str] = []
        if not names:
            tiers["static_checks"] = {
                "ran": False,
                "reason": "no checks declared in contract.proposer_static_checks",
                "errors": [],
                "notes": [],
            }
        else:
            static_errors, static_notes = run_static_checks(names, parent_root, scratch_root)
            tiers["static_checks"] = {
                "ran": True,
                "declared": list(names),
                "errors": static_errors,
                "notes": static_notes,
            }

        # Load the configured harness in a bounded subprocess.
        probe_errors, probe_notes = run_load_probe(
            ctx.workspace_root,
            scratch_root,
            **(
                {"adapter_configuration_json": ctx.adapter_configuration_json}
                if ctx.adapter_configuration_json is not None
                else {}
            ),
        )
        tiers["load_probe"] = {"ran": True, "errors": probe_errors, "notes": probe_notes}
    finally:
        shutil.rmtree(parent, ignore_errors=True)

    return _report([*static_errors, *probe_errors], tiers)


def validate_patches(patches_json: str) -> str:
    """Check a DRAFT patch set without proposing it. Returns a JSON report.

    Pass the same ``patches`` array you intend to emit — a JSON array of
    patch objects, or the whole ``{"patches": [...]}`` object; both are
    accepted. Each patch object takes the usual ``mutation_id`` / ``op`` /
    ``new_content`` | ``new_numeric`` | ``new_enum`` / ``rationale`` keys
    and nothing else; there is no extra field to supply and no digest to
    compute.

    The report is ``{"ok": bool, "errors": [...], "tiers": {...}}``.
    ``errors`` is the flat list to act on; ``tiers`` says which stage each
    finding came from and which stages ran. Structure and application
    failures stop validation. Static checks and the load probe both run
    after successful application:

    1. **structure** — schema shape, mutation-id resolution, op/payload
       discrimination, numeric range and enum domain, and the pre-image
       guard: a point whose content changed since the manifest you were
       given was enumerated has been rewritten under you, so re-read it
       and re-draft before patching it.
    2. **apply** — the patch set is applied all-or-nothing to a scratch
       copy of the parent snapshot, then checked for source integrity: every
       touched ``.py`` file still parses, every patched ``mutation_id``
       still resolves, declared ``required_placeholders`` survive, and the
       post-apply top-level import set is a superset of the pre-apply one.
    3. **static_checks** — the workspace's declared linters / type
       checkers, reported as the delta against the same checks on the
       unpatched tree, so pre-existing lint debt is never blamed on your
       patch. Skipped when the workspace declares none.
    4. **load_probe** — the harness entry point is imported in a
       subprocess, the same ``adapter.load`` the tournament runs before any
       entry executes. Catches an import-time break one round early.

    This is a LINTER FOR PATCHES and nothing more. It consumes no board
    data, calls no model, produces no score, and never executes a board
    entry — see this module's docstring for the governing principle and how
    it is structurally enforced. A clean report means the patch set is
    well-formed and the tree still loads; it says nothing about whether the
    change is a good idea, which is what the tournament is for.

    Raises
    ------
    ValueError
        When the argument is not a usable patch array — an actionable retry
        signal, matching the other tools' contract.
    """
    return _validate_against_context(_coerce_patch_array(patches_json), _active_context())


__all__ = [
    "LOAD_PROBE_TIMEOUT_SECONDS",
    "SCRATCH_PREFIX",
    "STATIC_CHECKS",
    "STATIC_CHECK_TIMEOUT_SECONDS",
    "TierResult",
    "declared_static_checks",
    "run_load_probe",
    "run_static_checks",
    "validate_patches",
]
