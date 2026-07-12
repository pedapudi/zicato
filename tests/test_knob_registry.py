"""Guard tests for the declarative knob registry (REIMPLEMENTATION.md Finding 3).

Adding one proposer/scoring knob historically touched a fixed set of
hand-maintained registries (the "seven-registry knob tax" traced via the
``genealogy`` knob). Finding 3 makes the FIELD DECLARATION the source of
truth: each participating field on :class:`ScoringWeights` and its nested
config dataclasses carries :func:`~zicato.core.scoring_config._knob` metadata
(``omit_at_default`` + ``builder_op`` + optional ``builder_arg``), and the
mechanical registries derive from / are enforced against it.

Two guards live here:

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
"""

from __future__ import annotations

import inspect
import re
from dataclasses import fields
from pathlib import Path

import zicato.dashboard as _dashboard_pkg
from zicato.builder import api as builder_api
from zicato.builder import copilot_tools, operations
from zicato.epoch.contract import (
    _CONTRACT_KNOB_DATACLASSES,
    _SCORING_OMIT_AT_DEFAULT_FIELDS,
)

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
        "diff_complexity_weight",
        "experiment_memory",
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


def _knob_registry() -> dict[str, tuple[str, str]]:
    """Map every ``builder_op`` field to its ``(op, arg)`` from the metadata.

    ``builder_arg`` defaults to the field name when the metadata leaves it
    ``None`` (the arg name matches the field name); it is set explicitly only
    where they differ (e.g. ``screen_entries`` → the ``entries`` arg).
    """
    registry: dict[str, tuple[str, str]] = {}
    for cls in _CONTRACT_KNOB_DATACLASSES:
        for f in fields(cls):
            op = f.metadata.get("builder_op")
            if op:
                arg = f.metadata.get("builder_arg") or f.name
                registry[f.name] = (op, arg)
    return registry


def _api_dispatch_block(op: str) -> str:
    """The body of api.py's ``if op == "<op>":`` dispatch arm."""
    source = Path(builder_api.__file__).read_text(encoding="utf-8")
    op_esc = re.escape(op)
    match = re.search(
        rf'if op == "{op_esc}":(.*?)(?=\n    if op ==|\n    raise|\Z)',
        source,
        re.S,
    )
    return match.group(1) if match else ""


def _has_op_signature_arg(op: str, arg: str) -> bool:
    fn = getattr(operations, op, None)
    return fn is not None and arg in inspect.signature(fn).parameters


def _has_api_dispatch(op: str, arg: str) -> bool:
    block = _api_dispatch_block(op)
    return f'"{arg}"' in block or f"'{arg}'" in block


def _has_copilot_arg(op: str, arg: str) -> bool:
    fn = getattr(copilot_tools, op, None)
    return fn is not None and arg in inspect.signature(fn).parameters


def _has_gui_row(op: str, arg: str) -> bool:
    """A ``runOp('<op>', { … <arg> … })`` call in builder.js."""
    source = _BUILDER_JS.read_text(encoding="utf-8")
    pattern = re.compile(rf"""runOp\(\s*['"]{re.escape(op)}['"]\s*,[^\n]*\b{re.escape(arg)}\b""")
    return bool(pattern.search(source))


def _has_node_test_assertion(op: str, arg: str) -> bool:
    """A builder.test.mjs assertion naming BOTH the quoted op and the arg.

    The quoted op on the same line is the discriminator that separates a
    real op-posting assertion from the fixture rows (which mention the arg
    but never the op).
    """
    source = _BUILDER_TEST_MJS.read_text(encoding="utf-8")
    quoted_op = re.compile(rf"['\"]{re.escape(op)}['\"]")
    word_arg = re.compile(rf"\b{re.escape(arg)}\b")
    return any(quoted_op.search(line) and word_arg.search(line) for line in source.splitlines())


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
    assert registry.get("genealogy") == ("set_proposer_quality", "genealogy"), (
        "the genealogy knob (Finding 3's traced example) is missing or mis-mapped "
        f"in the metadata-derived registry: {registry.get('genealogy')!r}"
    )
    # The renamed-arg case must resolve to the op's actual arg name.
    assert registry.get("screen_entries") == ("set_screening", "entries")


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
    for field_name, (op, arg) in sorted(registry.items()):
        for letter, description, predicate in _TOUCHPOINTS:
            if not predicate(op, arg):
                failures.append(
                    f"knob {field_name!r} (op {op!r}, arg {arg!r}) is missing "
                    f"touchpoint ({letter}): {description}"
                )
    assert not failures, "half-wired builder knob(s):\n" + "\n".join(failures)
