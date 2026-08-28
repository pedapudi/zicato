"""Tests for ``validate_patches`` — the proposer's closed loop (issue #147).

Three families:

* **Tier behaviour** — each tier catches what it is there to catch, and the
  pipeline stops at the first failing tier (there is nothing to lint in a
  tree that would not apply). The A1–A4 cases are the point of the feature:
  a dropped import, a vanished marker, a missing required placeholder, and
  a syntax break each come back as an actionable finding rather than as a
  rejected challenger one expensive round later.
* **The errors / notes split** — a missing dev tool or an adapterless
  workspace must NOT reject a well-formed patch. A validator that failed a
  good patch because a linter was uninstalled is a validator the proposer
  learns to ignore.
* **The envelope** — the structural pin behind the governing principle:
  the validator's transitive import closure contains no module that can
  read a board entry, load a harness, or judge a run. This mirrors the
  import-linter contract in ``pyproject.toml`` at runtime, so the property
  survives an import added in a code path lint-imports happens to miss.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from zicato.proposer.tool_context import ProposerToolContext, bind_proposer_tool_context
from zicato.proposer.tools import DEFAULT_PROPOSER_TOOLS
from zicato.proposer.validate import (
    STATIC_CHECKS,
    declared_static_checks,
    run_static_checks,
    validate_patches,
)

# A module with a marked span, an import the span's code needs, and a
# second marked file — enough surface for every A1-A4 case below.
_PROMPTS_PY = '''\
import re

# zicato:mutable id="harness__system_prompt"
SYSTEM_PROMPT = """You are a helpful assistant."""


def normalize(text):
    return re.sub(r"\\s+", " ", text)
'''


def _enumerate(snapshot: Path) -> tuple:
    """Build the bound manifest the way the ORCHESTRATOR builds it.

    Deliberately not :func:`~zicato.testing.make_mutation_point`: that
    helper stamps a placeholder ``content_hash`` (``"0" * 64``), which is
    fine for a manifest nobody hashes but wrong here — the pre-image guard
    compares ``content_hash`` between the bound manifest and a fresh
    enumeration, so a fixture with a fake hash would report every clean
    draft as stale. Enumerating for real is both the fix and the faithful
    thing: ``ProposerContext.mutations`` is an ``enumerate_mutations``
    result in production.
    """
    from zicato.mutation.enumerator import enumerate_mutations

    return tuple(enumerate_mutations([snapshot]))


def _build_snapshot(tmp_path: Path) -> tuple[Path, tuple]:
    """A generation snapshot plus the manifest enumerated from it."""
    snapshot = tmp_path / "snapshot"
    harness = snapshot / "harness"
    harness.mkdir(parents=True)
    (harness / "prompts.py").write_text(_PROMPTS_PY, encoding="utf-8")
    return snapshot, _enumerate(snapshot)


@pytest.fixture
def ctx(tmp_path: Path) -> ProposerToolContext:
    snapshot, mutations = _build_snapshot(tmp_path)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    return ProposerToolContext(
        workspace_root=workspace,
        generation_root=snapshot,
        epoch_id="ep-001",
        mutations=mutations,
        generation_id="v1",
    )


#: A whole-module mutation point — the surface A1 and A4 actually police.
#: A SPAN replace cannot break either: the applier re-quotes span content as
#: a Python string literal, so injected quotes are escaped rather than
#: syntax-breaking, and a span edit cannot touch an import line. A file
#: marker is where the proposer writes raw module text, which is exactly the
#: "emit an entire post-edit module and hope it satisfies A1-A4" workload
#: this tool exists for.
_WHOLE_PY = '# zicato:mutable:file id="harness__whole"\nimport re\n\nVALUE = re.escape("x")\n'


@pytest.fixture
def file_ctx(tmp_path: Path) -> ProposerToolContext:
    """A context whose only point is a FILE marker at the snapshot root.

    ``source_root`` is the snapshot root itself, matching what the
    orchestrator enumerates for a snapshot-rooted mutable tree. That
    alignment is load-bearing for A4: ``validate_post_apply`` locates a
    touched file's pre-apply twin by comparing ``file.relative_to(
    source_root)`` against ``file.relative_to(target_root)``, so a manifest
    whose ``source_root`` is not an ancestor of its ``file`` silently skips
    the import check.
    """
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / "whole.py").write_text(_WHOLE_PY, encoding="utf-8")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    return ProposerToolContext(
        workspace_root=workspace,
        generation_root=snapshot,
        epoch_id="ep-001",
        mutations=_enumerate(snapshot),
        generation_id="v1",
    )


def _whole_replace(new_content: str) -> list[dict]:
    return [
        {
            "mutation_id": "harness__whole",
            "op": "replace",
            "new_content": new_content,
            "rationale": "test",
        }
    ]


def _validate(ctx: ProposerToolContext, patches: object) -> dict:
    with bind_proposer_tool_context(ctx):
        return json.loads(validate_patches(json.dumps(patches)))


def _replace(new_content: str, **extra: object) -> list[dict]:
    return [
        {
            "mutation_id": "harness__system_prompt",
            "op": "replace",
            "new_content": new_content,
            "rationale": "test",
            **extra,
        }
    ]


# ---------------------------------------------------------------------------
# The happy path and the argument surface
# ---------------------------------------------------------------------------


def test_clean_patch_set_validates(ctx: ProposerToolContext) -> None:
    report = _validate(ctx, _replace("You are a terse assistant."))
    assert report["ok"] is True, report
    assert report["errors"] == []
    assert report["tiers"]["apply"]["ran"] is True


def test_accepts_both_the_bare_array_and_the_wrapped_object(
    ctx: ProposerToolContext,
) -> None:
    """A proposer reaching for the shape it emits must not spend a retry."""
    patches = _replace("You are a terse assistant.")
    bare = _validate(ctx, patches)
    wrapped = _validate(ctx, {"patches": patches})
    assert bare["ok"] is True
    assert wrapped["ok"] is True


def test_registered_in_the_default_tool_set(ctx: ProposerToolContext) -> None:
    """The ADK default proposer gets the closed loop, not just an external one."""
    assert validate_patches in DEFAULT_PROPOSER_TOOLS


@pytest.mark.parametrize(
    "bad",
    ["", "not json at all", '{"no_patches_key": 1}', '"a string"'],
)
def test_unusable_argument_raises_actionable_value_error(
    ctx: ProposerToolContext, bad: str
) -> None:
    with bind_proposer_tool_context(ctx), pytest.raises(ValueError, match="validate_patches"):
        validate_patches(bad)


def test_requires_a_bound_context() -> None:
    """Matches every other tool: a contextless call is a loud programming error."""
    with pytest.raises(RuntimeError, match="no bound ProposerToolContext"):
        validate_patches("[]")


# ---------------------------------------------------------------------------
# Tier 1a — structure, cross-checks, and the pre-image guard
# ---------------------------------------------------------------------------


def test_unknown_mutation_id_is_a_structure_error(ctx: ProposerToolContext) -> None:
    patches = _replace("x")
    patches[0]["mutation_id"] = "harness__does_not_exist"
    report = _validate(ctx, patches)
    assert report["ok"] is False
    assert "unknown mutation_id" in report["errors"][0]
    # Nothing downstream ran — there was no tree to lint.
    assert "apply" not in report["tiers"]


def test_op_payload_mismatch_is_a_structure_error(ctx: ProposerToolContext) -> None:
    report = _validate(
        ctx,
        [
            {
                "mutation_id": "harness__system_prompt",
                "op": "replace",
                "new_numeric": 3,
                "rationale": "test",
            }
        ],
    )
    assert report["ok"] is False
    assert any("new_content" in e for e in report["errors"])


def test_stale_pre_image_is_caught(ctx: ProposerToolContext) -> None:
    """The guard's whole reason to exist: the point moved under the draft.

    The bound manifest is the enumeration the proposal was drafted
    against. Rewriting the parent snapshot AFTER that manifest was handed
    out — which is what a concurrent promotion or an operator edit does —
    must be caught, or the patch confidently clobbers whatever changed it.
    """
    target = ctx.generation_root / "harness" / "prompts.py"
    target.write_text(
        _PROMPTS_PY.replace("You are a helpful assistant.", "Someone else already rewrote this."),
        encoding="utf-8",
    )
    report = _validate(ctx, _replace("You are a terse assistant."))
    assert report["ok"] is False, report
    stale = [e for e in report["errors"] if "stale pre-image" in e]
    assert stale, report["errors"]
    # Naming the point is the actionable part — the proposer has to know
    # WHICH of its patches to re-draft.
    assert "harness__system_prompt" in stale[0]


def test_unmoved_points_pass_the_pre_image_guard(ctx: ProposerToolContext) -> None:
    """The guard must be silent on the overwhelmingly common case.

    Nothing is asked of the proposer, so a guard that fired spuriously
    would make every clean draft look stale.
    """
    assert _validate(ctx, _replace("You are a terse assistant."))["ok"] is True


def test_pre_image_guard_ignores_untouched_points(tmp_path: Path) -> None:
    """A point that moved but is NOT patched is none of the guard's business.

    The guard is about the patch's own target. Rejecting a draft because
    some unrelated part of the tree changed would make it unusable in any
    workspace with a live operator.
    """
    snapshot = tmp_path / "snapshot"
    harness = snapshot / "harness"
    harness.mkdir(parents=True)
    (harness / "prompts.py").write_text(_PROMPTS_PY, encoding="utf-8")
    other = harness / "other.py"
    other.write_text(
        '# zicato:mutable id="harness__other"\nOTHER = """original"""\n', encoding="utf-8"
    )
    workspace = tmp_path / "ws"
    workspace.mkdir()
    ctx = ProposerToolContext(
        workspace_root=workspace,
        generation_root=snapshot,
        epoch_id="ep-001",
        mutations=_enumerate(snapshot),
        generation_id="v1",
    )
    # Move the UNPATCHED point only.
    other.write_text(
        '# zicato:mutable id="harness__other"\nOTHER = """moved"""\n', encoding="utf-8"
    )
    report = _validate(ctx, _replace("You are a terse assistant."))
    assert report["ok"] is True, report


def test_patch_carries_no_pre_image_field(ctx: ProposerToolContext) -> None:
    """The guard asks the proposer for nothing — issue #147 forbids a
    schema change, and an opt-in digest would be a guard a model could
    skip by omission.

    A patch object carrying an unknown key is still accepted (the schema
    deliberately leaves ``additionalProperties`` open), but the key has no
    meaning: the verdict is identical with and without it.
    """
    from zicato.core.types import Patch

    assert not hasattr(Patch("i", "m", "replace", None, None, None, "r"), "content_sha256")
    with_key = _validate(ctx, _replace("You are a terse assistant.", content_sha256="0" * 64))
    without = _validate(ctx, _replace("You are a terse assistant."))
    assert with_key["ok"] == without["ok"] is True
    assert with_key["errors"] == without["errors"] == []


def test_content_hash_has_exactly_one_reader() -> None:
    """``MutationPoint.content_hash``'s docstring described a check the
    applier never performed. This module is now the check it described.

    Pinned because the field is written by the enumerator and rendered by
    the CLI and the dashboard — plenty of *mentions*, and for a long time
    zero *readers*, which is exactly how the docstring stayed wrong.
    """
    import subprocess

    root = Path(__file__).resolve().parent.parent / "src" / "zicato"
    hits = subprocess.run(
        ["grep", "-rn", r"\.content_hash", str(root)],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.splitlines()
    # Attribute READS only — the enumerator writes it as a kwarg, which
    # does not match ``.content_hash``.
    readers = {line.split(":")[0].split("/")[-1] for line in hits if line.strip()}
    assert "validate.py" in readers, hits
    comparing = [ln for ln in hits if "==" in ln or "!=" in ln]
    assert comparing, "no module actually COMPARES content_hash any more"
    assert all("validate.py" in ln for ln in comparing), comparing


# ---------------------------------------------------------------------------
# Tier 1b — A1-A4, the reason the feature exists
# ---------------------------------------------------------------------------


def test_whole_module_rewrite_validates(file_ctx: ProposerToolContext) -> None:
    """The file-kind baseline the A1/A4 cases below deviate from."""
    report = _validate(
        file_ctx,
        _whole_replace(
            '# zicato:mutable:file id="harness__whole"\nimport re\n\nVALUE = re.escape("y")\n'
        ),
    )
    assert report["ok"] is True, report


def test_a1_syntax_break_is_caught(file_ctx: ProposerToolContext) -> None:
    """A1: a touched .py file that no longer parses."""
    report = _validate(
        file_ctx,
        _whole_replace('# zicato:mutable:file id="harness__whole"\nimport re\n\ndef broken(\n'),
    )
    assert report["ok"] is False
    assert report["errors"], report
    # The applier's own all-or-nothing gate refuses before validate_post_apply
    # gets the tree; either boundary is a correct place to catch an
    # unparseable file, so assert the CONDITION rather than the site.
    assert any("syntax" in e.lower() or "does not apply" in e for e in report["errors"])


def test_a4_dropped_import_is_caught(file_ctx: ProposerToolContext) -> None:
    """A4: the post-apply top-level import set must be a superset.

    The case the issue names verbatim (``A4: dropped 'import re'``) and the
    one a whole-module rewrite most easily gets wrong. Note the rewrite is
    still perfectly valid Python on its own — only the comparison against
    the pre-apply import set catches it, which is why A4 exists separately
    from A1.
    """
    report = _validate(
        file_ctx,
        _whole_replace('# zicato:mutable:file id="harness__whole"\nVALUE = "y"\n'),
    )
    assert report["ok"] is False
    assert any("import" in e for e in report["errors"]), report["errors"]


def test_a2_vanished_marker_is_caught(file_ctx: ProposerToolContext) -> None:
    """A2: the patched id must still resolve in a fresh enumeration.

    A whole-module rewrite that forgets to carry its own marker forward
    erases the point, so the NEXT round could never find it again.
    """
    report = _validate(file_ctx, _whole_replace('import re\n\nVALUE = re.escape("y")\n'))
    assert report["ok"] is False
    assert report["errors"], report


def test_a_span_replace_cannot_break_syntax(ctx: ProposerToolContext) -> None:
    """Pins WHY the A1/A4 cases above use a file marker.

    The applier re-quotes span content as a Python string literal, so a
    quote in the replacement is escaped rather than syntax-breaking. Span
    edits are structurally safe; whole-module rewrites are where A1-A4 earn
    their keep.
    """
    assert _validate(ctx, _replace('unbalanced "quote'))["ok"] is True


def test_apply_failure_stops_the_pipeline(file_ctx: ProposerToolContext) -> None:
    """Nothing downstream of a tree that would not apply is worth running."""
    report = _validate(
        file_ctx,
        _whole_replace('# zicato:mutable:file id="harness__whole"\nimport re\n\ndef broken(\n'),
    )
    assert report["ok"] is False
    assert "static_checks" not in report["tiers"]
    assert "load_probe" not in report["tiers"]


def test_validation_leaves_the_snapshot_untouched(ctx: ProposerToolContext) -> None:
    """The standing prohibition: no tool writes to the generation snapshot."""
    target = ctx.generation_root / "harness" / "prompts.py"
    before = target.read_text(encoding="utf-8")
    listing_before = sorted(p.name for p in ctx.generation_root.rglob("*"))
    assert _validate(ctx, _replace("You are a terse assistant."))["ok"] is True
    assert target.read_text(encoding="utf-8") == before
    assert sorted(p.name for p in ctx.generation_root.rglob("*")) == listing_before


def test_scratch_tree_is_cleaned_up(
    ctx: ProposerToolContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A per-call scratch tree that outlived its call would leak every round.

    The temp root is redirected to this test's ``tmp_path`` so the assertion
    is about THIS call and not about whatever a concurrent xdist worker
    happens to have in flight in the shared OS temp dir.
    """
    from zicato.proposer.validate import SCRATCH_PREFIX

    temp_root = tmp_path / "tmp"
    temp_root.mkdir()
    monkeypatch.setenv("TMPDIR", str(temp_root))
    _validate(ctx, _replace("You are a terse assistant."))
    assert list(temp_root.glob(f"{SCRATCH_PREFIX}*")) == []


# ---------------------------------------------------------------------------
# Tier 2 — the contract-declared static-check set
# ---------------------------------------------------------------------------


def test_static_checks_are_omitted_when_undeclared(ctx: ProposerToolContext) -> None:
    report = _validate(ctx, _replace("You are a terse assistant."))
    assert report["tiers"]["static_checks"]["ran"] is False
    assert "no checks declared" in report["tiers"]["static_checks"]["reason"]


def test_declared_static_checks_reads_the_contract_block(tmp_path: Path) -> None:
    (tmp_path / "config.json").write_text(
        json.dumps({"contract": {"proposer_static_checks": ["ruff", "mypy"]}}),
        encoding="utf-8",
    )
    assert declared_static_checks(tmp_path) == ("ruff", "mypy")


@pytest.mark.parametrize(
    "config",
    ["", "not json", '{"contract": {}}', '{"contract": {"proposer_static_checks": "ruff"}}'],
)
def test_declared_static_checks_degrades_to_empty(tmp_path: Path, config: str) -> None:
    """Every malformed shape yields (), which omits tier 2 AND leaves the
    proposer contract component hashing byte-identically."""
    (tmp_path / "config.json").write_text(config, encoding="utf-8")
    assert declared_static_checks(tmp_path) == ()


def test_declared_static_checks_absent_config(tmp_path: Path) -> None:
    assert declared_static_checks(tmp_path) == ()


def test_static_checks_report_only_the_delta(tmp_path: Path) -> None:
    """Pre-existing lint debt must never be blamed on the patch.

    Both trees carry the SAME unused import; only the scratch tree adds a
    second one. A raw diff would report both.
    """
    parent = tmp_path / "parent"
    scratch = tmp_path / "scratch"
    parent.mkdir()
    scratch.mkdir()
    (parent / "m.py").write_text("import os\n", encoding="utf-8")
    (scratch / "m.py").write_text("import os\nimport sys\n", encoding="utf-8")

    errors, notes = run_static_checks(["ruff"], parent, scratch)
    assert notes == []
    joined = "\n".join(errors)
    assert "sys" in joined, errors
    assert "os" not in joined, errors


def test_baseline_check_results_are_memoized_per_parent_tree(tmp_path: Path) -> None:
    """The parent tree is immutable for the round, so its findings are too.

    Without the memo a proposer that validates five drafts pays for five
    identical runs of every declared checker over the parent tree — half
    the cost of exactly the draft-fix-revalidate loop this tool exists to
    make cheap. The scratch side must NOT be cached: it is a fresh tree
    every call.
    """
    from zicato.proposer import validate as validate_mod

    parent = tmp_path / "parent"
    scratch = tmp_path / "scratch"
    parent.mkdir()
    scratch.mkdir()
    (parent / "m.py").write_text("import os\n", encoding="utf-8")
    (scratch / "m.py").write_text("import os\nimport sys\n", encoding="utf-8")

    calls: list[str] = []
    real = validate_mod._run_check

    def counting(name: str, root: Path) -> tuple[bool, list[str]]:
        calls.append(str(root))
        return real(name, root)

    validate_mod._BASELINE_CACHE.clear()
    try:
        validate_mod._run_check = counting  # type: ignore[assignment]
        first = run_static_checks(["ruff"], parent, scratch)
        second = run_static_checks(["ruff"], parent, scratch)
    finally:
        validate_mod._run_check = real  # type: ignore[assignment]
        validate_mod._BASELINE_CACHE.clear()

    assert first == second, "a memoized baseline must not change the verdict"
    assert calls.count(str(parent)) == 1, calls
    assert calls.count(str(scratch)) == 2, calls


def test_unknown_static_check_is_a_note_not_an_error(tmp_path: Path) -> None:
    """An operator typo must be visible, but it is not the proposer's to fix."""
    parent = tmp_path / "parent"
    scratch = tmp_path / "scratch"
    parent.mkdir()
    scratch.mkdir()
    errors, notes = run_static_checks(["ruffff"], parent, scratch)
    assert errors == []
    assert any("not a known check" in n for n in notes)


def test_contract_hash_is_byte_identical_at_the_empty_default() -> None:
    """Omit-at-default, the claim PROPOSER.md §2.10 makes.

    Every workspace that never configures the feature must hash exactly as
    it did before the field existed — otherwise shipping this would roll
    every live epoch for a feature nobody opted into.
    """
    from zicato.epoch.contract import _canon_proposer

    assert _canon_proposer(None, static_checks=()) == _canon_proposer(None)
    assert "validate_static_checks" not in _canon_proposer(None, static_checks=())


def test_declaring_static_checks_rolls_the_proposer_component() -> None:
    """The other half: opting in MUST roll the epoch, because it changes
    which patches the proposer will accept from itself."""
    from zicato.epoch.contract import _canon_proposer

    baseline = _canon_proposer(None, static_checks=())
    opted_in = _canon_proposer(None, static_checks=("ruff",))
    assert opted_in != baseline
    assert "validate_static_checks" in opted_in
    # Declaration ORDER is not semantic — the set is sorted before hashing.
    assert _canon_proposer(None, static_checks=("ruff", "mypy")) == _canon_proposer(
        None, static_checks=("mypy", "ruff")
    )


def test_static_check_registry_is_closed() -> None:
    """A contract-hashed NAME is reviewable; an operator-supplied argv is an
    arbitrary-execution surface a contract edit could widen silently."""
    assert set(STATIC_CHECKS) == {"ruff", "ruff-format", "mypy", "compileall"}


# ---------------------------------------------------------------------------
# Tier 3 — the sandboxed load probe
# ---------------------------------------------------------------------------


def test_load_probe_skipped_without_a_workspace_config(ctx: ProposerToolContext) -> None:
    """An adapterless workspace is the operator's problem, not the patch's."""
    report = _validate(ctx, _replace("You are a terse assistant."))
    assert report["ok"] is True
    probe = report["tiers"]["load_probe"]
    assert probe["errors"] == []
    assert any("no config.json" in n for n in probe["notes"])


def test_load_probe_reports_an_unimportable_harness(tmp_path: Path) -> None:
    """The tier-3 payoff: an import-time break surfaces one round early.

    Drives the probe module directly (it is the subprocess entry point) so
    the assertion is about the probe's verdict, not about process spawning.
    """
    from zicato.proposer import _load_probe

    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "config.json").write_text(
        json.dumps(
            {
                "adapter": {"kind": "import", "entrypoint": "zzz_does_not_exist:harness"},
            }
        ),
        encoding="utf-8",
    )
    rc = _load_probe.main([str(workspace), str(tmp_path)])
    # Either the adapter could not be built (rc 2, a NOTE upstream) or it
    # was built and the load failed (rc 0 with ok=false, an ERROR upstream).
    # Both are correct probe outcomes; what must never happen is a crash.
    assert rc in (0, 2)


def test_load_probe_actually_spawns_the_module(tmp_path: Path) -> None:
    """Exercises the real ``python -m zicato.proposer._load_probe`` path.

    Every other probe test drives ``main()`` in-process, which would keep
    passing even if the module were unreachable by ``-m``. This one spawns
    it for real against a workspace whose adapter cannot be built, and
    asserts the outcome is a NOTE — the operator's problem, reported
    without rejecting the patch.
    """
    from zicato.proposer.validate import run_load_probe

    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "config.json").write_text(
        json.dumps({"adapter": {"kind": "import", "entrypoint": "zzz_missing:harness"}}),
        encoding="utf-8",
    )
    errors, notes = run_load_probe(workspace, tmp_path / "scratch")
    assert errors == [], errors
    assert notes, "an unbuildable adapter must be reported, not silently ignored"
    joined = " ".join(notes)
    # The child reached OUR error path, which proves ``-m`` resolved the
    # module: a spawn that could not import it would report "No module
    # named zicato.proposer._load_probe" instead.
    assert "could not build the adapter" in joined, joined
    assert "No module named zicato" not in joined, joined


def test_load_probe_module_is_reached_by_spawn_not_import() -> None:
    """The probe is a subprocess so the adapters stay OUT of the validator's
    import closure — the split that makes the envelope pin satisfiable."""
    import zicato.proposer.validate as validate_mod

    source = Path(validate_mod.__file__).read_text(encoding="utf-8")
    assert "zicato.proposer._load_probe" in source
    assert "import zicato.proposer._load_probe" not in source
    assert "from zicato.proposer._load_probe" not in source


# ---------------------------------------------------------------------------
# The envelope — the structural pin behind the governing principle
# ---------------------------------------------------------------------------

#: Every package that could give the validator a route to board data, a
#: harness execution, or a score. The governing principle of
#: docs/design/PROPOSER.md is that the proposer may check its patch by any
#: means that consumes no board data and produces no scores, and may NEVER
#: execute board entries; this set is that sentence as an import closure.
_FORBIDDEN_PACKAGES = (
    "zicato.board",
    "zicato.adapters",
    "zicato.adapter_factory",
    "zicato._tournament_worker",
    "zicato.emulator",
    "zicato.judge_runtime",
)


def _import_closure(module_name: str) -> set[str]:
    """Transitively collect the ``zicato.*`` modules ``module_name`` pulls in.

    Walks ``sys.modules`` after a fresh import in a clean interpreter would
    be ideal, but the test process has already imported much of the tree;
    instead we walk the module graph statically the way lint-imports does,
    via each module's own recorded imports. Using ``grimp`` keeps this test
    and the ``pyproject.toml`` contract measuring the same thing.
    """
    import grimp

    graph = grimp.build_graph("zicato")
    return set(graph.find_downstream_modules(module_name)) | set(
        graph.find_modules_that_directly_import(module_name)
    )


def test_validator_import_closure_excludes_the_capability_surface() -> None:
    """Runtime twin of the ``pyproject.toml`` import-linter contract.

    lint-imports runs in CI; this runs in the unit suite, so a regression
    is caught by whichever gate a change happens to hit first.
    """
    grimp = pytest.importorskip("grimp")
    graph = grimp.build_graph("zicato")
    reachable = graph.find_descendants("zicato.proposer.validate") | {"zicato.proposer.validate"}
    upstream: set[str] = set()
    for module in reachable:
        upstream |= set(graph.find_upstream_modules(module))

    for forbidden in _FORBIDDEN_PACKAGES:
        offenders = {m for m in upstream if m == forbidden or m.startswith(f"{forbidden}.")}
        assert not offenders, (
            f"zicato.proposer.validate can reach {forbidden} via {sorted(offenders)[:3]}; "
            "the validator must have no import path to board data, a harness "
            "execution, or a score"
        )


def test_validate_patches_never_names_a_board_symbol() -> None:
    """A cheap second reading of the same claim, over the source text.

    The import closure catches a module dependency; this catches a lazy
    import written inside a function body that a graph walk over recorded
    imports could miss.
    """
    import zicato.proposer.validate as validate_mod

    source = Path(validate_mod.__file__).read_text(encoding="utf-8")
    for forbidden in ("zicato.board", "zicato.emulator", "zicato.judge_runtime"):
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith(("import ", "from ")):
                assert forbidden not in stripped, f"{forbidden} imported at {stripped!r}"


def test_python_executable_is_used_for_every_static_check() -> None:
    """Checks resolve to the tools in zicato's own environment, never to
    whatever happens to be first on PATH."""
    for name, builder in STATIC_CHECKS.items():
        argv = builder(Path("/tmp/x"))
        assert argv[0] == sys.executable, name
        assert argv[1] == "-m", name


# ---------------------------------------------------------------------------
# ProposerContext.generation_root — populated, not merely declared
# ---------------------------------------------------------------------------


def test_propose_child_requires_generation_root() -> None:
    """The field must be IMPOSSIBLE to forget at the real construction site.

    A dataclass field defaulting to ``None`` is exactly the shape that goes
    silently unpopulated: everything keeps working, the ADK path quietly
    falls back to re-deriving the store's path convention, and the reason
    the field exists is undone with no signal. ``_propose_child`` therefore
    takes it as a REQUIRED keyword-only argument — a call site that omits
    it is a TypeError, not a ``None``.
    """
    import inspect

    from zicato.evolve.propose_apply import _propose_child

    param = inspect.signature(_propose_child).parameters["generation_root"]
    assert param.default is inspect.Parameter.empty, (
        "generation_root must stay REQUIRED on _propose_child; a default "
        "would let a call site silently reintroduce the derivation"
    )
    assert param.kind is inspect.Parameter.KEYWORD_ONLY


def test_every_propose_child_call_site_passes_generation_root() -> None:
    """The shared candidate producer must pass the resolved snapshot root.

    Candidate generation has one call site for every tournament structure.
    The required-argument pin above makes omission a TypeError; this source
    check also makes a newly introduced bypass visible in review.
    """
    import ast

    root = Path(__file__).resolve().parent.parent / "src" / "zicato"
    calls: list[tuple[Path, ast.Call]] = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        calls.extend(
            (path, node)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_propose_child"
        )

    assert len(calls) == 1, f"expected one shared _propose_child call, got {calls}"
    path, call = calls[0]
    assert path.name == "propose_apply.py"
    assert any(keyword.arg == "generation_root" for keyword in call.keywords)


def test_adk_agent_prefers_the_populated_generation_root(tmp_path: Path) -> None:
    """A populated field wins over the fallback derivation.

    The fallback resolves through the generation store, which for a
    workspace with no such generation yields a path that does not exist —
    so a context carrying an explicit root must not be routed through it.
    """
    from zicato.proposer.adk_agent import _resolve_generation_root
    from zicato.proposer.agent import ProposerContext

    async def _never_called(system: str, user: str, model: str) -> str:  # pragma: no cover
        raise AssertionError("the resolver must not call the model")

    explicit = tmp_path / "explicit-snapshot"
    explicit.mkdir()
    ctx = ProposerContext(
        epoch_id="ep-001",
        parent_generation_id="v1",
        new_generation_id="v2",
        patterns=(),
        mutations=(),
        brief_text="",
        current_loss_summary="",
        aux_call_llm=_never_called,
        workspace_root=tmp_path / "ws",
        generation_root=explicit,
    )
    assert _resolve_generation_root(ctx) == explicit


def test_adk_agent_falls_back_when_generation_root_is_absent(tmp_path: Path) -> None:
    """The compatibility shim still works for a hand-built context."""
    from zicato.proposer.adk_agent import _resolve_generation_root
    from zicato.proposer.agent import ProposerContext

    async def _never_called(system: str, user: str, model: str) -> str:  # pragma: no cover
        raise AssertionError("the resolver must not call the model")

    workspace = tmp_path / "ws"
    workspace.mkdir()
    ctx = ProposerContext(
        epoch_id="ep-001",
        parent_generation_id="v1",
        new_generation_id="v2",
        patterns=(),
        mutations=(),
        brief_text="",
        current_loss_summary="",
        aux_call_llm=_never_called,
        workspace_root=workspace,
    )
    resolved = _resolve_generation_root(ctx)
    assert resolved != workspace
    assert "v1" in str(resolved)
