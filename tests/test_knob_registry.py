"""Guard tests for the declarative knob registry (REIMPLEMENTATION.md Finding 3).

Adding one proposer/scoring knob historically touched a fixed set of
hand-maintained registries (the "seven-registry knob tax" traced via the
``genealogy`` knob). Finding 3 makes the FIELD DECLARATION the source of
truth: each participating field on :class:`ScoringWeights` and its nested
config dataclasses carries :func:`~zicato.core.scoring_config._knob` metadata
(``omit_at_default`` + ``builder_op`` + optional ``builder_arg`` + an
optional :class:`~zicato.core.constraints.KnobConstraint` bound), and the
mechanical registries derive from / are enforced against it.

Three guards live here:

* :func:`test_derived_omit_set_equals_frozen_literal` — the contract
  canonicalizer's omit set is now DERIVED from the ``omit_at_default``
  metadata; this pins the FROZEN current set so a future metadata typo
  cannot silently add/drop an omit field and move the CONTRACT hash. The
  frozen literal that used to live in ``epoch/contract.py`` now lives HERE,
  as the guard.

* :func:`test_every_builder_op_knob_is_fully_wired` — every field with a
  ``builder_op`` must be wired through all five remaining touchpoints (op
  signature, API dispatch, copilot tool, GUI row, node test). Forgetting ANY
  one reds THIS test with a message naming exactly which touchpoint is
  missing for which knob. Enforcement over generation: the ops are NOT
  code-generated; this test is the net that catches a half-wired knob.

* :func:`test_loader_and_builder_refuse_alike` — every knob that declares a
  bound is refused by the contract loader and by the builder operation that
  sets it, with the same message. Each surface used to carry its own copy of
  each rule, and the copies drifted.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

import zicato.dashboard as _dashboard_pkg
from zicato.builder import api as builder_api
from zicato.builder import copilot_tools
from zicato.contract_draft import operations
from zicato.contract_draft.draft import TournamentDraft
from zicato.core.constraints import knob_constraint
from zicato.core.scoring_config import (
    ContractKnob,
    ExperimentalConfig,
    LadderConfig,
    OverfittingConfig,
    ProposerQualityConfig,
    ScoringWeights,
    contract_knobs,
    omit_at_default_fields,
    recommended_scaffold_weights,
)
from zicato.core.tournament import TournamentStructure

_SCORING_OMIT_AT_DEFAULT_FIELDS = omit_at_default_fields()

# ---------------------------------------------------------------------------
# Guard 1 — the derived omit set must equal the pinned frozen literal.
# ---------------------------------------------------------------------------

#: The FROZEN omit-at-default field-name set as captured against the parity
#: goldens. This literal used to live in ``epoch/contract.py`` where a typo
#: could silently move the contract hash; it now lives HERE as the guard the
#: metadata-derived set is pinned against. Adding a genuinely-new additive
#: knob means updating BOTH the field metadata AND this literal — a deliberate
#: two-hands ritual, because either alone would be a contract-hash bug.
_FROZEN_OMIT_AT_DEFAULT_FIELDS = frozenset(
    {
        # The declared mutation-site syntax table (issue #168). Additive and
        # empty by default — every workspace that never declares a file type
        # keeps the hash it has, while a declared suffix widens the surface
        # and rolls the epoch.
        "mutation_surface",
        "diff_complexity_weight",
        "diff_complexity_ceiling",
        # The holdout confirmation's own bounds (issue #118). Additive and
        # default-inert: ``holdout_margin=None`` reuses ``promote_margin`` and
        # a budget of 0 is the historical zero-tolerance rule, so omitting
        # both at their default keeps every existing epoch's hash where it is.
        "holdout_margin",
        "holdout_entry_regression_budget",
        "experiment_memory",
        # The opt-ins for features without a measured case (issue #394).
        # Omitted while every flag is off, so a contract naming none of
        # them keeps its hash; a flag turned on rolls the epoch.
        "experimental",
        "random_baseline_every_n",
        "block_on_containment_violation",
        "block_on_gate_contradiction",
        "screen_entries",
        "screen_veto_only",
        "process_exemplars",
        "recombine",
        "recombine_merge",
        "genealogy",
        "calibration_feedback",
        "telemetry_dialect",
        # Optional integration settings are absent from generic contracts.
        # Activating the block makes every nested value contract-bearing.
        "goldfive",
    }
)


def test_derived_omit_set_equals_frozen_literal() -> None:
    """The metadata-derived omit set is byte-identical to the frozen literal.

    ``_SCORING_OMIT_AT_DEFAULT_FIELDS`` is now derived from the per-field
    ``omit_at_default`` metadata. A metadata typo (flag added to the wrong
    field, or dropped from an omit field) would change which keys the
    canonicalizer emits at their default and therefore the contract hash for
    every existing epoch. This pins the derived set to the frozen current set
    so any such drift reds HERE (loudly, per-field) instead of silently in the
    hash.
    """
    added = _SCORING_OMIT_AT_DEFAULT_FIELDS - _FROZEN_OMIT_AT_DEFAULT_FIELDS
    dropped = _FROZEN_OMIT_AT_DEFAULT_FIELDS - _SCORING_OMIT_AT_DEFAULT_FIELDS
    assert not added and not dropped, (
        "the metadata-derived omit-at-default set drifted from the frozen "
        f"literal — added {sorted(added)}, dropped {sorted(dropped)}. An "
        "omit_at_default metadata flag was added to / removed from a field "
        "without the deliberate matching update to _FROZEN_OMIT_AT_DEFAULT_FIELDS "
        "in this test. Because the omit set decides which default-valued keys "
        "the contract canonicalizer emits, this drift would move the CONTRACT "
        "hash for existing epochs. Reconcile the field metadata and this literal."
    )


# ---------------------------------------------------------------------------
# Guard 2 — registry-driven completeness: every builder_op knob is fully wired.
# ---------------------------------------------------------------------------

_STATIC_DIR = Path(_dashboard_pkg.__file__).resolve().parent / "static"
_BUILDER_JS = _STATIC_DIR / "js" / "views" / "builder.js"
_BUILDER_TEST_MJS = _STATIC_DIR / "test" / "builder.test.mjs"


def _knob_registry() -> dict[str, tuple[str, str, str]]:
    """Map every ``builder_op`` field to its ``(op, arg, subkey)``.

    Keyed by ``"<Dataclass>.<field>"``, NOT the bare field name: two knob
    dataclasses may legitimately share a field name (``OverfittingConfig``
    and ``LadderConfig`` both have ``enabled``) and a bare-name key would
    silently drop one of them from the completeness guard.

    ``builder_arg`` defaults to the field name when the metadata leaves it
    ``None``; it is set explicitly only where they differ (e.g.
    ``screen_entries`` → the ``entries`` arg). A DOTTED ``builder_arg``
    (``"ladder.threshold"``) splits into the op's mapping arg plus the
    SUBKEY within it; ``subkey`` is ``""`` for a plain argument.
    """
    registry: dict[str, tuple[str, str, str]] = {}
    for knob in contract_knobs():
        if knob.builder_op:
            arg, _, subkey = knob.builder_arg.partition(".")
            registry[knob.key] = (knob.builder_op, arg, subkey)
    return registry


def _api_dispatch_block(op: str) -> str:
    """The body of api.py's ``if op == "<op>":`` dispatch arm.

    Deliberately FAILS CLOSED: the extraction assumes top-level
    ``if op == "…":`` arms at 4-space indent (true of api.py today). A
    refactor to elif/match-case reds this test as a scan-assumption
    failure — widen the extraction, don't weaken the guard.
    """
    source = Path(builder_api.__file__).read_text(encoding="utf-8")
    op_esc = re.escape(op)
    match = re.search(
        rf'if op == "{op_esc}":(.*?)(?=\n    if op ==|\n    raise|\Z)',
        source,
        re.S,
    )
    return match.group(1) if match else ""


def _has_op_signature_arg(op: str, arg: str, subkey: str) -> bool:
    fn = getattr(operations, op, None)
    return fn is not None and arg in inspect.signature(fn).parameters


def _has_api_dispatch(op: str, arg: str, subkey: str) -> bool:
    block = _api_dispatch_block(op)
    return f'"{arg}"' in block or f"'{arg}'" in block


def _has_copilot_arg(op: str, arg: str, subkey: str) -> bool:
    fn = getattr(copilot_tools, op, None)
    return fn is not None and arg in inspect.signature(fn).parameters


def _has_gui_row(op: str, arg: str, subkey: str) -> bool:
    """A ``runOp('<op>', { … <arg> … })`` call in builder.js.

    For a mapping subkey (``ladder.threshold``) the SAME line must also
    name the subkey: without that, one sibling's row (``ladder.enabled``)
    would vacuously satisfy every other ladder knob — which is precisely
    how ``ladder.threshold`` reached the op with no GUI row at all.

    Deliberately FAILS CLOSED: the scan assumes the call fits one line
    (every runOp in builder.js does today) — a reformat that wraps a call,
    or an alias like ``const g = runOp``, reds this test rather than
    passing silently. If that happens, this is a scan-assumption failure,
    not a missing GUI row; widen the pattern, don't weaken the guard.
    """
    source = _BUILDER_JS.read_text(encoding="utf-8")
    pattern = re.compile(rf"""runOp\(\s*['"]{re.escape(op)}['"]\s*,[^\n]*\b{re.escape(arg)}\b""")
    return any(
        pattern.search(line) and (not subkey or re.search(rf"\b{re.escape(subkey)}\b", line))
        for line in source.splitlines()
    )


def _has_node_test_assertion(op: str, arg: str, subkey: str) -> bool:
    """A builder.test.mjs assertion naming the op's calls and the arg.

    TWO lines must exist: (i) a line carrying the quoted op AND the arg —
    the op-call lookup or posting, separating a real test from fixture rows
    that mention the arg without the op; and (ii) an ``assert`` line
    carrying the arg as a word — the actual value check. Requiring the
    ``assert`` line closes the review-found hole where a comment mentioning
    the op + arg satisfied the touchpoint with no assertion at all.

    A mapping subkey is checked on the SUBKEY rather than the mapping arg,
    for the same anti-vacuity reason as :func:`_has_gui_row`.
    """
    source = _BUILDER_TEST_MJS.read_text(encoding="utf-8")
    quoted_op = re.compile(rf"['\"]{re.escape(op)}['\"]")
    word_arg = re.compile(rf"\b{re.escape(subkey or arg)}\b")
    lines = source.splitlines()
    references_op_call = any(quoted_op.search(ln) and word_arg.search(ln) for ln in lines)
    asserts_arg = any("assert" in ln and word_arg.search(ln) for ln in lines)
    return references_op_call and asserts_arg


#: The five touchpoints a ``builder_op`` knob must satisfy, each a
#: (letter, human description, predicate) triple. The letters match the
#: REIMPLEMENTATION.md Finding 3 enumeration (a)–(e).
_TOUCHPOINTS = (
    ("a", "an arg on the builder operation's signature (operations.py)", _has_op_signature_arg),
    ("b", "an API dispatch entry (builder/api.py)", _has_api_dispatch),
    ("c", "a copilot tool arg (builder/copilot_tools.py)", _has_copilot_arg),
    ("d", "a runOp('<op>', {…}) GUI row (views/builder.js)", _has_gui_row),
    ("e", "an arg-level assertion (test/builder.test.mjs)", _has_node_test_assertion),
)


def test_knob_registry_is_non_empty_and_covers_genealogy() -> None:
    """Sanity: the registry actually resolves and includes the traced knob.

    Guards against a metadata-walk that silently resolves to nothing (which
    would make the completeness guard vacuously green).
    """
    registry = _knob_registry()
    assert registry, "the knob registry derived from field metadata is empty"
    traced = registry.get("ProposerQualityConfig.genealogy")
    assert traced == ("set_proposer_quality", "genealogy", ""), (
        "the genealogy knob (Finding 3's traced example) is missing or mis-mapped "
        f"in the metadata-derived registry: {traced!r}"
    )
    # The renamed-arg case must resolve to the op's actual arg name.
    assert registry.get("ProposerQualityConfig.screen_entries") == (
        "set_screening",
        "entries",
        "",
    )
    # The dotted mapping-subkey case splits into (mapping arg, subkey).
    assert registry.get("LadderConfig.threshold") == ("set_holdout", "ladder", "threshold")
    # Same-named fields on two knob dataclasses must BOTH be registered — a
    # bare-field-name key used to collapse them onto one entry, silently
    # exempting whichever lost the race.
    assert "OverfittingConfig.enabled" in registry
    assert "LadderConfig.enabled" in registry


#: Contract knob fields with NO builder op, each with the reason it is
#: EXEMPT rather than missing. Reviewed as part of the exemption guard
#: below: adding a field here is a deliberate statement that the builder
#: should not expose it, not a placeholder for "not wired yet".
_NO_BUILDER_OP_KNOBS = {
    # Nested config CONTAINERS: the knobs are the fields INSIDE them, each
    # of which carries its own builder_op (the walk recurses into these
    # dataclasses), so a container-level op would be a second way to say
    # the same thing.
    "ScoringWeights.overfitting": "container — its fields carry the ops",
    "ScoringWeights.proposer_quality": "container — its fields carry the ops",
    "ScoringWeights.experiment_memory": "container — its fields carry the ops",
    "ScoringWeights.experimental": "container — its fields carry the ops",
    # Dotted CALLABLE specs (``pkg.mod:fn``) resolved by the same importer
    # predicates / judges use. A GUI field that names arbitrary importable
    # code to run is a code-execution surface, not a knob; these stay
    # contract-file-only, and the canonicalizer hashes the resolved
    # module's SOURCE so editing the plugin still rolls the epoch.
    "ScoringWeights.outcome_summarizer_spec": "dotted callable spec — contract-file only",
    "ScoringWeights.drift_reducer": "dotted callable spec — contract-file only",
    "ScoringWeights.scalar_fn": "dotted callable spec — contract-file only",
    # Declarative TransformSpec mappings ({"op": …, …params}) whose param
    # set varies per op; there is no fixed row shape for a GUI to render.
    "ScoringWeights.pass_transform": "open TransformSpec mapping — no fixed GUI row shape",
    "ScoringWeights.drift_kind_aggregation": "open TransformSpec mapping — no fixed GUI row shape",
}


def test_every_contract_knob_is_exposed_or_explicitly_exempt() -> None:
    """THE EXEMPTION GUARD: no knob may skip the builder by staying silent.

    The five-touchpoint pin below only ever saw fields that ALREADY declared
    a ``builder_op`` — a knob with no metadata at all was invisible to it, so
    "forgot to wire the builder entirely" was the one half-wired shape it
    could not catch. That is exactly how ``holdout_margin`` and
    ``holdout_entry_regression_budget`` (issue #118) shipped with a working
    gate and no way to set them from the builder. This guard closes the
    class: every contract knob field either carries a ``builder_op`` or is
    named in :data:`_NO_BUILDER_OP_KNOBS` with the reason.
    """
    unexplained: list[str] = []
    for knob in contract_knobs():
        if not knob.builder_op and knob.key not in _NO_BUILDER_OP_KNOBS:
            unexplained.append(knob.key)
    assert not unexplained, (
        "contract knob(s) with no builder op and no recorded exemption: "
        f"{sorted(unexplained)}. Either wire the knob through a builder op "
        "(_knob(builder_op=...) on the field, then the five touchpoints the "
        "companion guard checks) or add it to _NO_BUILDER_OP_KNOBS with the "
        "reason the builder should not expose it."
    )
    # And the exemption list may not rot: an entry for a field that no
    # longer exists (or has since GAINED an op) is stale.
    live = {knob.key for knob in contract_knobs() if not knob.builder_op}
    stale = set(_NO_BUILDER_OP_KNOBS) - live
    assert not stale, (
        f"stale _NO_BUILDER_OP_KNOBS entries {sorted(stale)} — the field was "
        "removed or has since gained a builder_op; drop the exemption."
    )


def test_every_builder_op_knob_is_fully_wired() -> None:
    """THE PIN: every ``builder_op`` knob is wired through all five touchpoints.

    Forgetting ANY of the (a)–(e) touchpoints for ANY knob reds this ONE test
    with a message naming exactly which touchpoint is missing for which knob —
    so a half-wired knob (the ``genealogy``-style silent half-works defect) can
    never ship. The ops are NOT generated from the metadata (the doc keeps
    generation minimal); this is enforcement, not generation.
    """
    registry = _knob_registry()
    failures: list[str] = []
    for field_name, (op, arg, subkey) in sorted(registry.items()):
        shown = f"{arg}.{subkey}" if subkey else arg
        for letter, description, predicate in _TOUCHPOINTS:
            if not predicate(op, arg, subkey):
                failures.append(
                    f"knob {field_name!r} (op {op!r}, arg {shown!r}) is missing "
                    f"touchpoint ({letter}): {description}"
                )
    assert not failures, "half-wired builder knob(s):\n" + "\n".join(failures)


# ---------------------------------------------------------------------------
# Guard 3 — one declared bound per knob, honoured by loader and builder alike.
# ---------------------------------------------------------------------------

#: One inadmissible value per knob that declares a bound. Both the contract
#: loader (constructing the owning dataclass) and the builder operation the
#: knob's own metadata names must refuse it, with the SAME message — which is
#: what :func:`test_loader_and_builder_refuse_alike` asserts. The wording
#: drifted for years while the two surfaces kept private copies of each rule
#: (``screen_entries must be >= 0`` against ``screen entries must be >= 0``).
_INADMISSIBLE_VALUES: dict[tuple[type, str], object] = {
    (ScoringWeights, "pass_weight"): float("nan"),
    (ScoringWeights, "default_judge_weight"): float("inf"),
    (ScoringWeights, "plan_revision_weight"): float("nan"),
    (ScoringWeights, "task_failure_weight"): float("nan"),
    (ScoringWeights, "not_completed_weight"): float("nan"),
    (ScoringWeights, "diff_complexity_weight"): -1.0,
    (ScoringWeights, "diff_complexity_ceiling"): -1.0,
    (ScoringWeights, "promote_margin"): -0.05,
    (ScoringWeights, "holdout_entry_regression_budget"): -1,
    (ScoringWeights, "pass_rate_monotonicity_scope"): "per_namespace",
    (ScoringWeights, "regression_timeout_s"): 0,
    (ScoringWeights, "telemetry_dialect"): "syslog",
    (OverfittingConfig, "min_board_size_for_split"): -1,
    # Not ``0``: ``set_holdout`` reserves that as the token that CLEARS the
    # ceiling, since ``None`` there already means "leave unchanged".
    (OverfittingConfig, "max_generations_per_contract"): -1,
    (OverfittingConfig, "random_baseline_every_n"): -1,
    (LadderConfig, "threshold"): -0.5,
    (LadderConfig, "budget"): -1,
    (LadderConfig, "noise_scale"): -0.1,
    (ProposerQualityConfig, "best_of_n"): 0,
    (ProposerQualityConfig, "screen_entries"): -1,
    (ProposerQualityConfig, "process_exemplars"): -1,
    (ProposerQualityConfig, "genealogy"): -1,
    (ProposerQualityConfig, "calibration_feedback"): -1,
    (ProposerQualityConfig, "recombine_merge"): "union",
}


def _bounded_knobs() -> list[ContractKnob]:
    """Every knob that declares a bound, in declaration order."""
    return [knob for knob in contract_knobs() if _declares_a_bound(knob)]


def _declares_a_bound(knob: ContractKnob) -> bool:
    try:
        knob_constraint(knob.owner, knob.name)
    except KeyError:
        return False
    return True


def _set_through_builder(knob: ContractKnob, value: object) -> None:
    """Set one knob to ``value`` through the operation its metadata names.

    A DOTTED ``builder_arg`` (``ladder.threshold``) names a subkey of a
    partial-mapping argument, so the value is wrapped in that mapping — the
    same reading of the metadata the wiring guard above applies.
    """
    argument, _, subkey = knob.builder_arg.partition(".")
    payload = {subkey: value} if subkey else value
    getattr(operations, knob.builder_op)(TournamentDraft(), **{argument: payload})


@pytest.mark.parametrize("knob", _bounded_knobs(), ids=lambda knob: knob.key)
def test_loader_and_builder_refuse_alike(knob: ContractKnob) -> None:
    """Contract load and the builder operation reject with one wording."""
    value = _INADMISSIBLE_VALUES[(knob.owner, knob.name)]
    with pytest.raises(ValueError) as from_loader:
        knob.owner(**{knob.name: value})
    with pytest.raises(ValueError) as from_builder:
        _set_through_builder(knob, value)
    assert str(from_loader.value) == str(from_builder.value)
    expected_name = knob_constraint(knob.owner, knob.name).label or knob.name
    assert str(from_loader.value).startswith(expected_name)


def test_every_bounded_knob_has_an_inadmissible_value() -> None:
    """No knob may declare a bound with no case pinning both surfaces to it."""
    declared = {(knob.owner, knob.name) for knob in _bounded_knobs()}
    missing = sorted(
        f"{owner.__name__}.{name}" for owner, name in declared - set(_INADMISSIBLE_VALUES)
    )
    assert not missing, (
        f"knob(s) {missing} declare a bound with no entry in _INADMISSIBLE_VALUES — "
        "add a value the bound forbids."
    )
    stale = sorted(
        f"{owner.__name__}.{name}" for owner, name in set(_INADMISSIBLE_VALUES) - declared
    )
    assert not stale, f"_INADMISSIBLE_VALUES names knob(s) {stale} that declare no bound."


def test_recommended_scaffold_enables_no_experimental_knob() -> None:
    """Every flag in the ``experimental`` block stays off in the scaffold.

    The block holds features without a measured case (issue #394). A
    feature graduates by moving out of it; the scaffold turns no flag on.
    Walking the dataclass fields keeps the pin true for a flag added later.
    """
    scaffold = recommended_scaffold_weights().experimental
    enabled = [
        knob.name
        for knob in contract_knobs()
        if knob.owner is ExperimentalConfig and getattr(scaffold, knob.name) != knob.default
    ]
    assert not enabled, f"the recommended scaffold enables experimental knob(s) {enabled}"


def test_promote_margin_may_not_invert_the_gate() -> None:
    """A negative promote margin is refused rather than promoting a regression.

    The gate's scalar rule is ``delta_scalar <= -promote_margin``, so a margin
    of ``-0.05`` would promote a challenger that scored 0.05 WORSE than the
    champion. Nothing rejected it before: the field was checked for
    finiteness alone.
    """
    with pytest.raises(ValueError, match="promote_margin must be >= 0"):
        ScoringWeights(promote_margin=-0.05)
    # A zero margin is a bar of zero, not an inversion.
    ScoringWeights(promote_margin=0.0)


@pytest.mark.parametrize("replicates", [0, -2])
def test_zero_or_negative_replicates_is_refused(replicates: int) -> None:
    """A duel count below one is refused at load instead of clamped at run time.

    ``replicates`` lives in the untyped structure-params mapping, where every
    strategy that reads it clamps with ``max(1, ...)`` — so an operator who
    wrote ``0`` got single-run duels and no indication their setting was
    ignored.
    """
    with pytest.raises(ValueError, match=r'tournament params\["replicates"\] must be >= 1'):
        TournamentStructure(structure="swiss", params={"replicates": replicates})
    with pytest.raises(ValueError, match=r'tournament params\["replicates"\] must be >= 1'):
        operations.set_param(TournamentDraft(), "replicates", replicates)
