"""``validate_patches`` — the proposer's closed loop on its own patch set.

Both existing proposer tiers *emit* a patch set and are done; neither has
ever seen its own output checked. For a span replace of a short instruction
that is fine. For a file-marker ``replace`` — where ``new_content`` is an
entire post-edit module that must satisfy every constraint in
``docs/design/MUTATION-SURFACE.md`` §6 — emitting the whole module in one
shot and hoping it satisfies A1–A4 is exactly the workload a tool-using
agent exists to avoid. Without a validate step, a violation costs a full
retry round-trip through the propose loop, re-sending the entire manifest.

This module closes that loop. :func:`validate_patches` applies a DRAFT
patch set to a scratch copy of the parent snapshot and reports what broke,
so the proposer drafts, validates, sees ``A4: dropped 'import re'``, fixes
it, validates again, and only then answers. The bounded retry is then the
rare fallback rather than the main loop.

The governing principle
-----------------------
**The proposer may check its patch by any means that consumes no board
data and produces no scores; it may never execute board entries.**

That line is normative and is written into ``docs/design/PROPOSER.md``.
Everything here is static: the scratch tree is a copy of the parent
snapshot, the checks read source, and the tier-3 probe resolves the harness
entry point without invoking it. If the proposer could test against a slice
it chose it would be grading its own work — the overfitting failure the
tournament exists to prevent — so no tier here touches the board, the
scoring weights, or the judges.

The claim is enforced STRUCTURALLY rather than by inspection: this module's import
closure excludes the whole capability surface — :mod:`zicato.board` (where
entry text is loaded), :mod:`zicato.adapters` /
:mod:`zicato.adapter_factory` / :mod:`zicato._tournament_worker` (how a
harness is loaded and run), and :mod:`zicato.emulator` /
:mod:`zicato.judge_runtime` (how an entry is judged). It is pinned by an
import-linter contract in ``pyproject.toml`` and, at runtime, by a
transitive import-closure test in ``tests/test_proposer_validate.py``.

That is also why the tier-3 probe lives in its own
:mod:`zicato.proposer._load_probe` module and is reached by SPAWNING a
subprocess rather than by importing the adapter factory here. It is why the
context plumbing this module needs lives in
:mod:`zicato.proposer.tool_context`: importing
:mod:`zicato.proposer.tools` for it would drag the analyzer, and through it
the board loader, into the closure.

:mod:`zicato.scoring` and :mod:`zicato.tournament` are not in that set, and
that is intended: every module in the repo reaches them through
``core.types -> core.scoring_config``, which imports them for TYPE
definitions. That edge is a type-model artifact rather than a capability.

The three tiers
---------------
Each tier runs only if the previous one passed — there is nothing to lint
in a tree that would not apply.

1. **Structure + apply (always on).** The shape pass over the ``patches``
   array (:data:`~zicato.proposer.structured.PATCHES_JSON_SCHEMA`), the
   cross-check pass (:func:`~zicato.proposer.structured.parse_patch_list` —
   mutation-id resolution, op/payload discrimination, ``min``/``max`` and
   enum domains), the pre-image guard (below), the
   pre-apply surface check
   (:func:`zicato.mutation.validator.validate_patches`), the applier's own
   all-or-nothing apply into the scratch tree, and A1–A4
   (:func:`zicato.mutation.validator.validate_post_apply`).
2. **Static analysis (opt-in, contract-declared).** The workspace's
   declared linter / type-checker set, run over the scratch tree — see
   :data:`STATIC_CHECKS` and :func:`declared_static_checks`. Reported as a
   DELTA against the same checks run on the parent tree, so a patch is
   never blamed for the tree's pre-existing lint debt.
3. **Load probe (on whenever the workspace has a config to resolve an
   adapter from).** ``adapter.load`` against the scratch snapshot in a
   subprocess with a timeout — the same call the tournament makes before
   any entry executes, one expensive round earlier.

The pre-image guard
-------------------
:attr:`~zicato.core.mutation.MutationPoint.content_hash` has existed since
the enumerator was written, and its docstring claimed "the patch applier
checks this before applying a patch so a stale proposer round cannot
clobber an already-rewritten region". **The applier never read it** — the
field was written by the enumerator, rendered by the CLI and the dashboard,
and checked by nothing. :func:`_pre_image_problems` is the check that
docstring described, and this module is now its only reader.

The comparison is between two enumerations zicato already computes: the
manifest the proposal was drafted against
(:attr:`ProposerToolContext.mutations`) and a fresh enumeration of the
parent snapshot at validate time. A patched point whose ``content_hash``
moved between them was rewritten under the proposer, so its draft is
reasoning about text the tree no longer holds.

Nothing is asked of the proposer, by design. Making the pre-image a digest
the model declares per patch would make the guard opt-in — a model omitting
the field would simply not be checked — and would ask the model for
arithmetic it has no reason to get right. ``Patch`` carries
no pre-image field and must not grow one — issue #147 is explicit that the
``Experiment`` schema does not change.
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

from zicato.core.types import MutationPoint, Patch
from zicato.mutation.enumerator import enumerate_mutations
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

#: Per-check wall-clock ceiling for a tier-2 static check, in seconds. A
#: linter that has not answered in two minutes on one snapshot is wedged;
#: the check is reported as timed out rather than hanging the proposer.
STATIC_CHECK_TIMEOUT_SECONDS = 120.0

#: Wall-clock ceiling for the tier-3 load probe, in seconds. Importing a
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


#: The CLOSED registry of tier-2 static checks a workspace may declare, by
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


def declared_static_checks(workspace_root: Path) -> tuple[str, ...]:
    """Return the workspace's declared tier-2 static-check names, in order.

    Read from ``{workspace_root}/config.json`` at
    ``contract.proposer_static_checks`` — the same ``contract`` block that
    carries ``proposer_path``, because this set is contract, not
    configuration: changing which checks the proposer holds itself to
    changes which patches it will accept from itself, hence what it
    proposes. :func:`zicato.epoch.contract.resolve_contract_inputs` reads
    the key through this same function and folds it into the proposer
    component of the contract hash.

    Unknown names are dropped (a typo must not silently mean "no checks"
    for a check the operator believes is running — it is reported by
    :func:`run_static_checks` as an explicit finding instead). An absent
    key, an unreadable config, or a malformed value all yield ``()``, which
    omits tier 2 entirely and leaves the contract canon byte-identical to a
    workspace that never heard of this feature.
    """
    try:
        contract = read_workspace_config(workspace_root).contract
    except (OSError, ValueError):
        return ()
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


#: What a tier returns: ``(errors, notes)``. ERRORS are the proposer's to
#: fix and set ``ok`` to ``False``; NOTES describe something that stopped
#: the check from running (a checker not installed, a workspace with no
#: adapter) and are reported without rejecting the patch. Keeping them
#: apart is load-bearing: a validator that failed a well-formed patch
#: because a dev tool was missing would teach the proposer to distrust it.
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
    :func:`_normalize_finding` — are errors. That delta is what makes tier
    2 usable at all: real trees carry lint debt, and a validator that
    blamed a patch for the tree it landed in would fail every draft.

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


def run_load_probe(workspace_root: Path, scratch_root: Path) -> TierResult:
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
    if not workspace_is_initialized(workspace_root):
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
            ],
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


def _pre_image_problems(
    patches: Sequence[Patch],
    drafted_against: Mapping[str, MutationPoint],
    parent_root: Path,
) -> list[str]:
    """Reject patches whose target moved since the manifest was handed out.

    THE actual pre-image guard, and the only thing in zicato that reads
    :attr:`~zicato.core.mutation.MutationPoint.content_hash`. The bound
    manifest (``drafted_against`` — :attr:`ProposerToolContext.mutations`)
    is the enumeration the proposal was drafted against; a fresh
    enumeration of ``parent_root`` is the tree as it stands now. A patched
    point whose ``content_hash`` differs between the two is one the
    proposer reasoned about in a version the tree no longer holds, and
    rewriting it would clobber whatever changed it.

    Nothing is asked of the proposer. Making the pre-image a digest the
    model declares on each patch would be worse twice over: it would make
    the guard opt-in, since a model omitting the field would simply not be
    checked, and it would ask the model to do arithmetic it has no reason
    to get right. Comparing two enumerations zicato already computes needs
    no cooperation and no wire change — ``Patch`` carries no pre-image
    field and must not grow one.

    A point that has VANISHED from the fresh enumeration is left alone
    here: A2 (:func:`~zicato.mutation.validator.validate_post_apply`)
    reports that against the post-apply tree with a better message, and
    double-reporting one fault as two costs the proposer a wasted fix.
    """
    problems: list[str] = []
    try:
        current = {p.id: p for p in enumerate_mutations([parent_root])}
    except (OSError, ValueError, SyntaxError):
        # Enumeration is best-effort here: the apply step re-enumerates and
        # will fail loudly on a tree that cannot be walked. A guard that
        # could not read the tree must not invent a staleness finding.
        return []
    for patch in patches:
        drafted = drafted_against.get(patch.mutation_id)
        live = current.get(patch.mutation_id)
        if drafted is None or live is None:
            continue
        if drafted.content_hash != live.content_hash:
            problems.append(
                f"stale pre-image for {patch.mutation_id!r}: the manifest you "
                f"drafted against has content_hash "
                f"{drafted.content_hash[:16]}… but the parent snapshot now "
                f"holds {live.content_hash[:16]}…. The point was rewritten "
                f"under you; re-read it (list_mutation_points / "
                f"read_mutable_file) and re-draft before patching it"
            )
    return problems


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
    """The tiered validation proper, once the argument has been parsed."""
    mutations_by_id = {mp.id: mp for mp in ctx.mutations}
    tiers: dict[str, Any] = {}

    # --- Tier 1a: shape + cross-check + pre-image, all before any I/O. ---
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
        else:
            structure_errors.extend(
                _pre_image_problems(patches, mutations_by_id, ctx.generation_root.resolve())
            )

    tiers["structure"] = {"ran": True, "errors": structure_errors, "notes": []}
    if structure_errors:
        return _report(structure_errors, tiers)

    # --- Tier 1b: apply into a scratch copy, then A1-A4. ---
    from zicato.mutation.applier import apply_patches  # noqa: PLC0415
    from zicato.mutation.validator import validate_post_apply  # noqa: PLC0415

    parent_root = ctx.generation_root.resolve()
    parent = Path(tempfile.mkdtemp(prefix=SCRATCH_PREFIX))
    scratch_root = parent / "child"
    try:
        try:
            apply_patches(parent_root, patches, scratch_root)
        except (FileNotFoundError, ValueError, KeyError) as exc:
            apply_errors = [f"the patch set does not apply: {exc}"]
            tiers["apply"] = {"ran": True, "errors": apply_errors, "notes": []}
            return _report(apply_errors, tiers)

        apply_errors = validate_post_apply(scratch_root, patches, list(ctx.mutations))
        tiers["apply"] = {"ran": True, "errors": apply_errors, "notes": []}
        if apply_errors:
            return _report(apply_errors, tiers)

        # --- Tier 2: the contract-declared static-check set. ---
        names = declared_static_checks(ctx.workspace_root)
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

        # --- Tier 3: the sandboxed adapter.load probe. ---
        probe_errors, probe_notes = run_load_probe(ctx.workspace_root, scratch_root)
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
    finding came from and which stages ran. Stages run in order and stop at
    the first that fails, because there is nothing to lint in a tree that
    would not apply:

    1. **structure** — schema shape, mutation-id resolution, op/payload
       discrimination, numeric range and enum domain, and the pre-image
       guard: a point whose content changed since the manifest you were
       given was enumerated has been rewritten under you, so re-read it
       and re-draft before patching it.
    2. **apply** — the patch set is applied all-or-nothing to a scratch
       copy of the parent snapshot, then checked against A1–A4: every
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
