"""Tests for issue #19 phase 3 — dotted-spec scoring plugins.

Covers the eight required behaviours:

* a ``scalar_fn`` / ``drift_reducer`` plugin WRAPS the builtin / transformed
  value (composes on top of the Phase-2 transform);
* FAIL-OPEN: a raising plugin → fallback + logged + provenance records it;
  a NaN / inf / non-numeric return → fallback;
* SOURCE-HASH rolls the epoch on a plugin BODY edit — for a scoring plugin AND
  a predicate / judge;
* worker round-trip drives a ``drift_reducer`` plugin in the worker's
  ``compute_drift_loss`` path (the per_judge_weights trap);
* the mutation guard ignores operator scoring / predicates / judges;
* serde / hash + auto-roll for the new fields;
* the neutral default (no plugin) is the Phase-2 / builtin path.
"""

from __future__ import annotations

import importlib
import json
import logging
import math
import sys
import textwrap
from pathlib import Path

import pytest

from zicato.core import DriftCount, ScoringWeights
from zicato.scoring import builtin_drift_loss, builtin_scalar
from zicato.scoring.api import DriftContext, ScalarContext
from zicato.scoring.dispatch import resolve_drift_loss, resolve_scalar

# ---------------------------------------------------------------------------
# In-test plugin module helpers
# ---------------------------------------------------------------------------


def _install_plugin_module(tmp_path: Path, name: str, body: str) -> str:
    """Write ``<name>.py`` under ``tmp_path``, put it on ``sys.path``, import it.

    Returns the module name (so a dotted spec ``<name>:fn`` resolves). The
    caller is responsible for editing + reloading when testing a body edit.
    """
    if str(tmp_path) not in sys.path:
        sys.path.insert(0, str(tmp_path))
    (tmp_path / f"{name}.py").write_text(textwrap.dedent(body), encoding="utf-8")
    importlib.invalidate_caches()
    sys.modules.pop(name, None)
    importlib.import_module(name)
    return name


def _drift_ctx(weights: ScoringWeights, drift_counts: tuple[DriftCount, ...]) -> DriftContext:
    return DriftContext(
        drift_counts=drift_counts,
        plan_revisions=0,
        task_failure_ratio=0.0,
        runtime_ms=0,
        weights=weights,
        builtin_loss=builtin_drift_loss(
            drift_counts=drift_counts,
            plan_revisions=0,
            task_failure_ratio=0.0,
            runtime_ms=0,
            weights=weights,
        ),
    )


def _scalar_ctx(
    weights: ScoringWeights, *, mean_score: float, drift_loss_mean: float
) -> ScalarContext:
    ns: dict[str, float] = {}
    return ScalarContext(
        pass_rate=mean_score,
        mean_score=mean_score,
        drift_loss_mean=drift_loss_mean,
        namespace_aggregates=ns,
        per_judge_loss={},
        weights=weights,
        builtin_scalar=builtin_scalar(
            mean_score=mean_score,
            drift_loss_mean=drift_loss_mean,
            namespace_aggregates=ns,
            weights=weights,
        ),
    )


# ---------------------------------------------------------------------------
# Neutral default — no plugin is the Phase-2 / builtin path
# ---------------------------------------------------------------------------


def test_neutral_default_no_plugin_is_builtin_path() -> None:
    """Empty scalar_fn / drift_reducer leaves the dispatcher on the builtin path."""
    weights = ScoringWeights()
    assert weights.scalar_fn == "" and weights.drift_reducer == ""

    dctx = _drift_ctx(weights, (DriftCount(kind="off_topic", severity="warning", count=2),))
    loss, prov = resolve_drift_loss(dctx)
    assert loss == dctx.builtin_loss
    assert prov == "builtin"

    sctx = _scalar_ctx(weights, mean_score=0.5, drift_loss_mean=1.0)
    scalar, sprov = resolve_scalar(sctx)
    assert scalar == sctx.builtin_scalar
    assert sprov == "builtin"


# ---------------------------------------------------------------------------
# A plugin WRAPS the builtin via ctx.builtin_*
# ---------------------------------------------------------------------------


def test_scalar_fn_wraps_builtin(tmp_path: Path) -> None:
    name = _install_plugin_module(
        tmp_path,
        "plug_scalar_wrap",
        """
        def add_ten(ctx):
            # wraps the builtin: start from it and add a constant.
            return ctx.builtin_scalar + 10.0
        """,
    )
    weights = ScoringWeights(scalar_fn=f"{name}:add_ten")
    sctx = _scalar_ctx(weights, mean_score=0.5, drift_loss_mean=1.0)
    scalar, prov = resolve_scalar(sctx)
    assert scalar == sctx.builtin_scalar + 10.0
    assert prov == f"plugin:scalar_fn={name}:add_ten"


def test_drift_reducer_wraps_builtin(tmp_path: Path) -> None:
    name = _install_plugin_module(
        tmp_path,
        "plug_drift_wrap",
        """
        def halve(ctx):
            return ctx.builtin_loss / 2.0
        """,
    )
    weights = ScoringWeights(drift_reducer=f"{name}:halve")
    dctx = _drift_ctx(weights, (DriftCount(kind="off_topic", severity="warning", count=4),))
    loss, prov = resolve_drift_loss(dctx)
    assert loss == dctx.builtin_loss / 2.0
    assert prov == f"plugin:drift_reducer={name}:halve"


# ---------------------------------------------------------------------------
# Composition ON TOP of a Phase-2 transform
# ---------------------------------------------------------------------------


def test_scalar_fn_composes_on_top_of_pass_transform(tmp_path: Path) -> None:
    """The plugin sees the POST-TRANSFORM scalar as ctx.builtin_scalar."""
    name = _install_plugin_module(
        tmp_path,
        "plug_scalar_compose",
        """
        # echo proves the plugin received the transformed value, not the raw builtin.
        def echo(ctx):
            return ctx.builtin_scalar
        """,
    )
    # pow(2.0) reshapes the pass/miss term; with the plugin echoing builtin_scalar,
    # the result must equal the transform-only scalar (plugin wraps the transform).
    transform = {"op": "pow", "exponent": 2.0}
    w_transform_only = ScoringWeights(pass_transform=transform)
    w_with_plugin = ScoringWeights(pass_transform=transform, scalar_fn=f"{name}:echo")

    sctx_t = _scalar_ctx(w_transform_only, mean_score=0.3, drift_loss_mean=1.0)
    transform_scalar, transform_prov = resolve_scalar(sctx_t)
    assert transform_prov.startswith("transform:pass=")

    sctx_p = _scalar_ctx(w_with_plugin, mean_score=0.3, drift_loss_mean=1.0)
    plugin_scalar, plugin_prov = resolve_scalar(sctx_p)

    # The plugin echoed the transformed value: composes ON TOP of the transform,
    # NOT on the raw builtin (which differs because pow(2.0) reshaped the miss).
    assert plugin_scalar == transform_scalar
    assert plugin_scalar != sctx_p.builtin_scalar
    assert plugin_prov == f"plugin:scalar_fn={name}:echo"


def test_drift_reducer_composes_on_top_of_kind_aggregation(tmp_path: Path) -> None:
    """The drift_reducer sees the POST-TRANSFORM loss as ctx.builtin_loss."""
    name = _install_plugin_module(
        tmp_path,
        "plug_drift_compose",
        """
        def echo(ctx):
            return ctx.builtin_loss
        """,
    )
    drift = (DriftCount(kind="looping_reasoning", severity="warning", count=4),)
    agg = {"looping_reasoning": {"op": "harmonic"}}
    w_transform_only = ScoringWeights(drift_kind_aggregation=agg)
    w_with_plugin = ScoringWeights(drift_kind_aggregation=agg, drift_reducer=f"{name}:echo")

    transform_loss, transform_prov = resolve_drift_loss(_drift_ctx(w_transform_only, drift))
    assert transform_prov.startswith("transform:drift")

    plugin_loss, plugin_prov = resolve_drift_loss(_drift_ctx(w_with_plugin, drift))
    assert plugin_loss == transform_loss
    assert plugin_prov == f"plugin:drift_reducer={name}:echo"


# ---------------------------------------------------------------------------
# FAIL-OPEN — raise / NaN / inf / non-numeric → fallback to pre-plugin value
# ---------------------------------------------------------------------------


def test_raising_plugin_falls_back_logged_and_provenance(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    name = _install_plugin_module(
        tmp_path,
        "plug_raises",
        """
        def boom(ctx):
            raise RuntimeError("operator bug")
        """,
    )
    weights = ScoringWeights(scalar_fn=f"{name}:boom")
    sctx = _scalar_ctx(weights, mean_score=0.4, drift_loss_mean=2.0)
    with caplog.at_level(logging.WARNING):
        scalar, prov = resolve_scalar(sctx)
    # Falls back to the pre-plugin (builtin) value, never crashes.
    assert scalar == sctx.builtin_scalar
    # Provenance records the fallback + reason so it is visible, not silent.
    assert prov == "builtin (fallback: raised RuntimeError)"
    assert any("RuntimeError" in r.message and "falling back" in r.message for r in caplog.records)


@pytest.mark.parametrize(
    "expr,reason",
    [
        ("float('nan')", "non-finite return"),
        ("float('inf')", "non-finite return"),
        ("'not a number'", "non-finite return"),
        ("None", "non-finite return"),
        ("True", "non-finite return"),  # bool is rejected (must return a number)
    ],
)
def test_non_finite_or_non_numeric_return_falls_back(
    tmp_path: Path, expr: str, reason: str
) -> None:
    name = _install_plugin_module(
        tmp_path,
        "plug_bad_return",
        f"""
        def bad(ctx):
            return {expr}
        """,
    )
    weights = ScoringWeights(drift_reducer=f"{name}:bad")
    dctx = _drift_ctx(weights, (DriftCount(kind="off_topic", severity="warning", count=3),))
    loss, prov = resolve_drift_loss(dctx)
    assert loss == dctx.builtin_loss
    assert prov == f"builtin (fallback: {reason})"
    assert math.isfinite(loss)


def test_fallback_token_records_pre_plugin_transform(tmp_path: Path) -> None:
    """A raising plugin on top of a transform records the TRANSFORM token + fallback."""
    name = _install_plugin_module(
        tmp_path,
        "plug_raises_on_transform",
        """
        def boom(ctx):
            raise ValueError("x")
        """,
    )
    weights = ScoringWeights(
        pass_transform={"op": "pow", "exponent": 2.0}, scalar_fn=f"{name}:boom"
    )
    sctx = _scalar_ctx(weights, mean_score=0.3, drift_loss_mean=1.0)
    scalar, prov = resolve_scalar(sctx)
    # Falls back to the TRANSFORMED value (the pre-plugin value), and the token
    # shows the transform was the fallback target.
    assert prov == "transform:pass=pow(2.0) (fallback: raised ValueError)"


# ---------------------------------------------------------------------------
# SOURCE-HASH rolls the epoch on a plugin BODY edit (scoring + predicate/judge)
# ---------------------------------------------------------------------------


def _build_contract(tmp_path: Path, *, board: str, scoring: str):
    from zicato.epoch.contract import ContractInputs

    board_path = tmp_path / "board.jsonl"
    brief_path = tmp_path / "brief.md"
    scoring_path = tmp_path / "scoring.json"
    board_path.write_text(board)
    brief_path.write_text("# brief\n")
    scoring_path.write_text(scoring)
    return ContractInputs(
        board_path=board_path,
        brief_path=brief_path,
        scoring_path=scoring_path,
        entrypoint="pkg.mod:agent",
        mutable_trees=(str(tmp_path / "agent"),),
    )


def test_scoring_plugin_body_edit_rolls_contract_hash(tmp_path: Path) -> None:
    from zicato.epoch.contract import compute_contract_hash

    name = _install_plugin_module(
        tmp_path,
        "plug_hashed_scoring",
        """
        def my_scalar(ctx):
            return ctx.builtin_scalar
        """,
    )
    scoring = json.dumps({"scalar_fn": f"{name}:my_scalar"})
    board = (
        json.dumps(
            {"id": "e", "kind": "single_turn", "wall_clock_budget_seconds": 60, "input": "x"}
        )
        + "\n"
    )
    inputs = _build_contract(tmp_path, board=board, scoring=scoring)
    h1 = compute_contract_hash(inputs)

    # Edit the plugin BODY (same dotted spec) and reload.
    _install_plugin_module(
        tmp_path,
        "plug_hashed_scoring",
        """
        def my_scalar(ctx):
            return ctx.builtin_scalar + 1.0  # behaviour change
        """,
    )
    h2 = compute_contract_hash(inputs)
    assert h1 != h2, "editing a scoring-plugin body must roll the contract hash"


def test_predicate_body_edit_rolls_contract_hash(tmp_path: Path) -> None:
    from zicato.epoch.contract import compute_contract_hash

    name = _install_plugin_module(
        tmp_path,
        "plug_hashed_pred",
        """
        def passes(result):
            return True
        """,
    )
    board = (
        json.dumps(
            {
                "id": "e",
                "kind": "single_turn",
                "wall_clock_budget_seconds": 60,
                "input": "x",
                "expectation": {"kind": "predicate", "spec": f"{name}:passes"},
            }
        )
        + "\n"
    )
    inputs = _build_contract(tmp_path, board=board, scoring=json.dumps({}))
    h1 = compute_contract_hash(inputs)

    _install_plugin_module(
        tmp_path,
        "plug_hashed_pred",
        """
        def passes(result):
            return False  # behaviour change
        """,
    )
    h2 = compute_contract_hash(inputs)
    assert h1 != h2, "editing a predicate body must roll the contract hash"


def test_python_judge_body_edit_rolls_contract_hash(tmp_path: Path) -> None:
    from zicato.epoch.contract import compute_contract_hash

    name = _install_plugin_module(
        tmp_path,
        "plug_hashed_judge",
        """
        class MyJudge:
            name = "myjudge"
        """,
    )
    board_meta = json.dumps(
        {
            "board_meta": True,
            "judges": [
                {
                    "name": "myjudge",
                    "mode": "python",
                    "body": f"{name}:MyJudge",
                    "severity": "warning",
                }
            ],
        }
    )
    board = (
        board_meta
        + "\n"
        + json.dumps(
            {"id": "e", "kind": "single_turn", "wall_clock_budget_seconds": 60, "input": "x"}
        )
        + "\n"
    )
    inputs = _build_contract(tmp_path, board=board, scoring=json.dumps({}))
    h1 = compute_contract_hash(inputs)

    _install_plugin_module(
        tmp_path,
        "plug_hashed_judge",
        """
        class MyJudge:
            name = "myjudge"
            tweaked = True  # behaviour change
        """,
    )
    h2 = compute_contract_hash(inputs)
    assert h1 != h2, "editing a python-judge body must roll the contract hash"


def test_spec_with_source_hash_shape(tmp_path: Path) -> None:
    from zicato.scoring.plugins import spec_with_source_hash

    name = _install_plugin_module(
        tmp_path,
        "plug_shape",
        """
        def fn(ctx):
            return ctx.builtin_scalar
        """,
    )
    out = spec_with_source_hash(f"{name}:fn")
    assert out["spec"] == f"{name}:fn"
    assert isinstance(out["source_sha256"], str) and len(out["source_sha256"]) == 64
    # Unresolvable spec degrades gracefully (spec string only, null source).
    bad = spec_with_source_hash("nope.nope:nope")
    assert bad == {"spec": "nope.nope:nope", "source_sha256": None}


# ---------------------------------------------------------------------------
# Worker round-trip drives a drift_reducer plugin (the per_judge_weights trap)
# ---------------------------------------------------------------------------


def test_drift_reducer_survives_worker_transport_and_drives_compute_drift_loss(
    tmp_path: Path,
) -> None:
    """drift_reducer survives _weights_spec → JSON → _weights_from_args AND runs
    in the worker's compute_drift_loss path."""
    from zicato._tournament_worker import _weights_from_args
    from zicato.telemetry.reducer import compute_drift_loss
    from zicato.tournament.runner import _weights_spec

    name = _install_plugin_module(
        tmp_path,
        "plug_worker_drift",
        """
        def double(ctx):
            return ctx.builtin_loss * 2.0
        """,
    )
    weights = ScoringWeights(
        severity_weights={"info": 1.0, "warning": 3.0, "critical": 10.0},
        drift_reducer=f"{name}:double",
    )
    spec = _weights_spec(weights)
    assert spec["drift_reducer"] == f"{name}:double"
    round_tripped = _weights_from_args({"weights": json.loads(json.dumps(spec))})
    assert round_tripped.drift_reducer == f"{name}:double"

    drift = (DriftCount(kind="off_topic", severity="warning", count=5),)
    # builtin = 5 × warning 3.0 = 15; the reducer doubles it → 30.
    plugin_loss = compute_drift_loss(
        drift, plan_revisions=0, task_failure_ratio=0.0, runtime_ms=0, weights=round_tripped
    )
    builtin_loss = compute_drift_loss(
        drift,
        plan_revisions=0,
        task_failure_ratio=0.0,
        runtime_ms=0,
        weights=ScoringWeights(severity_weights={"info": 1.0, "warning": 3.0, "critical": 10.0}),
    )
    assert builtin_loss == pytest.approx(15.0)
    assert plugin_loss == pytest.approx(30.0)


def test_scalar_fn_survives_worker_transport(tmp_path: Path) -> None:
    from zicato._tournament_worker import _weights_from_args
    from zicato.tournament.runner import _weights_spec

    weights = ScoringWeights(scalar_fn="some.pkg:my_scalar")
    spec = _weights_spec(weights)
    assert spec["scalar_fn"] == "some.pkg:my_scalar"
    round_tripped = _weights_from_args({"weights": json.loads(json.dumps(spec))})
    assert round_tripped.scalar_fn == "some.pkg:my_scalar"


# ---------------------------------------------------------------------------
# Serde / hash + auto-roll for the new fields
# ---------------------------------------------------------------------------


def test_new_fields_round_trip_through_scoring_serde(tmp_path: Path) -> None:
    from zicato.workspace_loader import scoring_weights_from_dict

    raw = {"scalar_fn": "p.m:s", "drift_reducer": "p.m:d"}
    w = scoring_weights_from_dict(raw)
    assert w.scalar_fn == "p.m:s"
    assert w.drift_reducer == "p.m:d"


def test_configuring_scalar_fn_rolls_contract_hash(tmp_path: Path) -> None:
    from zicato.epoch.contract import compute_contract_hash

    board = (
        json.dumps(
            {"id": "e", "kind": "single_turn", "wall_clock_budget_seconds": 60, "input": "x"}
        )
        + "\n"
    )
    h_none = compute_contract_hash(_build_contract(tmp_path, board=board, scoring=json.dumps({})))

    name = _install_plugin_module(
        tmp_path,
        "plug_roll",
        """
        def s(ctx):
            return ctx.builtin_scalar
        """,
    )
    h_set = compute_contract_hash(
        _build_contract(tmp_path, board=board, scoring=json.dumps({"scalar_fn": f"{name}:s"}))
    )
    assert h_none != h_set, "configuring a scalar_fn must roll the epoch"


def test_non_string_plugin_field_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="scalar_fn must be a dotted-spec string"):
        ScoringWeights(scalar_fn=123)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="drift_reducer must be a dotted-spec string"):
        ScoringWeights(drift_reducer=["a"])  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Mutation guard — operator grading is never enumerated
# ---------------------------------------------------------------------------


def test_mutation_enumerator_skips_grading_file(tmp_path: Path) -> None:
    from zicato.mutation.enumerator import enumerate_mutations

    graded = tmp_path / "scoring.py"
    graded.write_text(
        textwrap.dedent(
            """
            # zicato:grading — operator-owned scoring; never a mutation point.
            # zicato:mutable id="should_be_ignored"
            PROMPT = "even a marked literal here must NOT be enumerated"
            """
        ),
        encoding="utf-8",
    )
    points = enumerate_mutations([tmp_path])
    assert points == [], "a # zicato:grading file must contribute zero mutation points"


def test_mutation_enumerator_still_enumerates_non_grading_file(tmp_path: Path) -> None:
    from zicato.mutation.enumerator import enumerate_mutations

    normal = tmp_path / "agent.py"
    normal.write_text(
        textwrap.dedent(
            """
            # zicato:mutable id="prompt"
            PROMPT = "this is mutable"
            """
        ),
        encoding="utf-8",
    )
    ids = {p.id for p in enumerate_mutations([tmp_path])}
    assert "prompt" in ids


def test_example_scoring_plugins_are_grading_guarded() -> None:
    """The shipped example scoring plugin module carries the grading sentinel."""
    from zicato.mutation.enumerator import enumerate_mutations

    mod = importlib.import_module("zicato_examples.target_1_presentation.scoring")
    path = Path(mod.__file__)  # type: ignore[arg-type]
    assert enumerate_mutations([path]) == []


def test_example_drift_reducer_reproduces_harmonic_looping() -> None:
    """The example harmonic_looping_reducer wraps the builtin into a harmonic curve."""
    from zicato_examples.target_1_presentation.scoring import harmonic_looping_reducer

    weights = ScoringWeights(severity_weights={"warning": 1.0})
    drift = (DriftCount(kind="looping_reasoning", severity="warning", count=3),)
    dctx = _drift_ctx(weights, drift)
    # builtin: 3 loops × warning 1.0 = 3.0 linear.
    assert dctx.builtin_loss == pytest.approx(3.0)
    # harmonic: 1 + 1/2 + 1/3 = 1.8333…, replacing the linear 3.0.
    got = harmonic_looping_reducer(dctx)
    assert got == pytest.approx(1.0 + 0.5 + 1.0 / 3.0)
    # Wired through the dispatcher it produces the same value + plugin provenance.
    weights_plugin = ScoringWeights(
        severity_weights={"warning": 1.0},
        drift_reducer="zicato_examples.target_1_presentation.scoring:harmonic_looping_reducer",
    )
    loss, prov = resolve_drift_loss(_drift_ctx(weights_plugin, drift))
    assert loss == pytest.approx(1.0 + 0.5 + 1.0 / 3.0)
    assert prov.startswith("plugin:drift_reducer=")
