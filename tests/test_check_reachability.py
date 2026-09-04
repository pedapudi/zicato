"""Tests for the reachability probe ``zicato evolve --dry-run`` runs.

The probe is the half of role checking that a static read cannot do: it
proves a configured role's credential is accepted, its model id exists,
and its callable returns a ``str``. No test here reaches an endpoint —
every role's callable is supplied through the probe's injectable
factory, so what is exercised is the probe's own reporting, bounding,
and role selection.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from tests.test_check_gate import (
    _MUTABLE,
    _VALID_ADK_ENTRYPOINT,
    _entry,
    _evolve,
    _workspace,
)
from zicato.check import reachability
from zicato.models_config import ModelsConfig, RoleSpec

_KEYED = RoleSpec(model="keyed-model", endpoint="https://example.invalid", api_key_env="A_KEY_ENV")
_KEYLESS = RoleSpec(model="keyless-model", endpoint="https://example.invalid")


def _answering(reply: object = "ok"):
    """A factory whose roles all answer with ``reply``."""

    async def call_llm(system: str, user: str, model: str) -> object:
        del system, user, model
        return reply

    def build(spec: RoleSpec, *, role: str):
        del spec, role
        return call_llm

    return build


def _per_role(**replies):
    """A factory giving each named role its own behaviour.

    A value that is an exception is raised on call; anything else is
    returned. Roles not named answer ``"ok"``.
    """

    def build(spec: RoleSpec, *, role: str):
        del spec
        outcome = replies.get(role, "ok")

        async def call_llm(system: str, user: str, model: str) -> object:
            del system, user, model
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        return call_llm

    return build


def _probe(models: ModelsConfig, build, **kwargs) -> tuple[reachability.RoleProbe, ...]:
    return asyncio.run(reachability.probe_configured_roles(models, build_call_llm=build, **kwargs))


# --- what one round trip proves ---------------------------------------------


def test_a_role_that_answers_with_a_string_is_reachable() -> None:
    probes = _probe(ModelsConfig(target=_KEYED), _answering())
    assert [(p.role, p.ok, p.engine) for p in probes] == [("target", True, "keyed-model")]
    assert probes[0].error == ""


def test_a_rejected_credential_fails_only_its_own_role() -> None:
    """The point of per-role reporting: one dead role names itself."""
    models = ModelsConfig(target=_KEYED, judge=_KEYLESS)
    probes = _probe(models, _per_role(judge=PermissionError("401 invalid api key")))
    assert {p.role: p.ok for p in probes} == {"target": True, "judge": False}
    judge = next(p for p in probes if p.role == "judge")
    assert judge.error == "PermissionError: 401 invalid api key"
    assert reachability.unreachable_roles(probes) == (judge,)


def test_only_the_first_line_of_a_long_endpoint_error_is_reported() -> None:
    probes = _probe(
        ModelsConfig(target=_KEYED),
        _per_role(target=ValueError("model not found\nrequest-id: 7\ntrace: ...")),
    )
    assert probes[0].error == "ValueError: model not found"


def test_a_role_that_answers_with_something_other_than_a_string_fails() -> None:
    """``CallLLM`` promises a ``str``; a role that breaks it breaks scoring."""
    probes = _probe(ModelsConfig(target=_KEYED), _answering(reply={"content": "ok"}))
    assert probes[0].ok is False
    assert probes[0].error == "returned dict, expected a str"


def test_a_role_that_never_answers_is_bounded() -> None:
    """A hung endpoint fails its own role instead of wedging the run."""

    def build(spec: RoleSpec, *, role: str):
        del spec, role

        async def call_llm(system: str, user: str, model: str) -> str:
            del system, user, model
            await asyncio.sleep(30)
            return "too late"

        return call_llm

    probes = _probe(ModelsConfig(target=_KEYED), build, timeout_s=0.05)
    assert probes[0].ok is False
    assert probes[0].error == "no answer within 0.05s"


def test_a_role_whose_callable_cannot_even_be_built_fails() -> None:
    def build(spec: RoleSpec, *, role: str):
        raise ValueError(f"models.{role}: nothing to build from {spec.model!r}")

    probes = _probe(ModelsConfig(target=_KEYED), build)
    assert probes[0].ok is False
    assert "nothing to build" in probes[0].error


# --- which roles are probed --------------------------------------------------


def test_an_unconfigured_role_is_skipped() -> None:
    """It falls back to the callable the CLI resolved and reports itself."""
    probes = _probe(ModelsConfig(target=_KEYED), _answering())
    assert [p.role for p in probes] == ["target"]


def test_no_configured_role_probes_nothing_and_says_so() -> None:
    """ "Not checked" must not render the same as "checked and reachable"."""
    probes = _probe(ModelsConfig(), _answering())
    assert probes == ()
    rendered = reachability.render_reachability(probes)
    assert "none was probed" in rendered
    assert "not a statement that any role is reachable" in rendered


def test_a_role_with_no_credential_variable_is_probed_like_a_keyed_one() -> None:
    """A spec may name no ``api_key_env`` at all.

    An endpoint that authenticates through ambient application-default
    credentials configures a model and an endpoint and nothing else.
    Requiring a key environment variable before probing would silently
    skip exactly those roles — the ones whose auth a static read can say
    the least about.
    """
    seen: list[tuple[str, str | None]] = []

    def build(spec: RoleSpec, *, role: str):
        seen.append((role, spec.api_key_env))
        return _answering()(spec, role=role)

    probes = _probe(ModelsConfig(target=_KEYED, judge=_KEYLESS), build)
    assert seen == [("target", "A_KEY_ENV"), ("judge", None)]
    assert [(p.role, p.ok) for p in probes] == [("target", True), ("judge", True)]


def test_the_probe_builds_a_role_the_way_a_worker_does() -> None:
    """Whatever auth the spec implies is exercised, not assumed.

    The probe must not re-derive how a role authenticates; it goes
    through the construction seam ``_tournament_worker`` uses, so the
    scheme the spec implies is the scheme that gets tried. Both sides
    resolve a keyless model spec to the same deferred callable — nothing
    is built and no endpoint is contacted until it is called.
    """
    from zicato._tournament_worker import _resolve_role_call_llm

    worker_side = _resolve_role_call_llm({"models_role": _KEYLESS.to_worker_spec()}, role="judge")
    probe_side = reachability.worker_call_llm(_KEYLESS, role="judge")
    assert callable(worker_side)
    assert probe_side.__qualname__ == worker_side.__qualname__


# --- the report an operator reads -------------------------------------------


def test_the_report_names_every_role_and_ends_on_a_verdict() -> None:
    models = ModelsConfig(target=_KEYED, judge=_KEYLESS)
    rendered = reachability.render_reachability(
        _probe(models, _per_role(judge=PermissionError("401 invalid api key")))
    )
    assert "[ok] target (keyed-model)" in rendered
    assert "[FAILED] judge (keyless-model): PermissionError: 401 invalid api key" in rendered
    assert rendered.rstrip().endswith("1 of 2 configured roles did not answer: judge")


def test_the_report_says_so_when_every_role_answered() -> None:
    rendered = reachability.render_reachability(_probe(ModelsConfig(target=_KEYED), _answering()))
    assert rendered.rstrip().endswith("All 1 configured roles answered.")


# --- the dry run ------------------------------------------------------------


def _clean_workspace(tmp_path: Path) -> Path:
    return _workspace(
        tmp_path / ".zicato",
        config={
            "adapter": {
                "kind": "adk",
                "entrypoint": _VALID_ADK_ENTRYPOINT,
                "mutable_trees": [],
            }
        },
        board=[_entry("e1", expectation={"kind": "expected_text", "spec": "hi"})],
        scoring={},
        trees={"harness": _MUTABLE.format(point_id="p")},
    )


def _stub_probes(monkeypatch, *probes: reachability.RoleProbe) -> None:
    async def fake(models, **kwargs):
        del models, kwargs
        return probes

    monkeypatch.setattr(reachability, "probe_configured_roles", fake)


def test_dry_run_reports_every_configured_role(tmp_path: Path, monkeypatch) -> None:
    _stub_probes(
        monkeypatch,
        reachability.RoleProbe("target", "keyed-model", True),
        reachability.RoleProbe("judge", "keyless-model", True),
    )
    result = _evolve(_clean_workspace(tmp_path), "--dry-run")
    assert result.exit_code == 0
    assert "[ok] target (keyed-model)" in result.output
    assert "[ok] judge (keyless-model)" in result.output
    assert "No board entry ran." in result.output


def test_a_role_that_does_not_answer_fails_the_dry_run(tmp_path: Path, monkeypatch) -> None:
    _stub_probes(
        monkeypatch,
        reachability.RoleProbe("target", "keyed-model", True),
        reachability.RoleProbe("judge", "keyless-model", False, "PermissionError: 401"),
    )
    result = _evolve(_clean_workspace(tmp_path), "--dry-run")
    assert result.exit_code != 0
    assert "[FAILED] judge (keyless-model): PermissionError: 401" in result.output
    assert "did not answer" in result.output


def test_the_probe_runs_only_once_the_offline_gate_has_passed(tmp_path: Path, monkeypatch) -> None:
    """A workspace with a stop must not spend a round trip proving it.

    The gate's own findings are the answer there, and an operator whose
    board does not parse does not need a credential verdict too.
    """
    probed: list[object] = []

    async def fake(models, **kwargs):
        del kwargs
        probed.append(models)
        return ()

    monkeypatch.setattr(reachability, "probe_configured_roles", fake)
    root = _clean_workspace(tmp_path)
    (root.parent / "board.jsonl").write_text("{not json", encoding="utf-8")

    result = _evolve(root, "--dry-run")
    assert result.exit_code != 0
    assert "board_unreadable" in result.output
    assert probed == []


def test_a_dry_run_that_probes_nothing_still_claims_nothing_was_spent(
    tmp_path: Path, monkeypatch
) -> None:
    _stub_probes(monkeypatch)
    result = _evolve(_clean_workspace(tmp_path), "--dry-run")
    assert result.exit_code == 0
    assert "Nothing was spent." in result.output
