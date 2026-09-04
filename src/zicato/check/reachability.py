"""The half of role reachability that needs a network round trip.

:mod:`zicato.check.validators` proves what a configured model role can
be proved to have for free: a ``call_llm`` dotted path imports to a
callable, and a model spec naming an ``api_key_env`` finds that variable
set. What no static read can prove is that the credential is *accepted*,
that the model id *exists*, or that the callable returns a ``str``. Each
of those needs one real call, which today first happens inside a worker,
mid-tournament, where a judge exception is absorbed into
``JudgeReliability.errors`` and the round finishes with a silently
absent scored namespace.

**Where this runs.** ``zicato evolve --dry-run``, and nowhere else.
``--dry-run`` is the "will this work?" gesture: interactive, spending
nothing on the board, typed by an operator who is asking to be told
about wiring. The mandatory gate on the spend paths stays offline-safe
by construction — a networked check there would refuse every offline
workspace, every fixture workspace, and the parity capture.

**Scope.** The roles configured under ``models``, which is the boundary
the static validator holds. A role with no configured engine falls back
to the callable the CLI resolved and reports itself; probing that would
report the CLI's own argument back to the operator who just typed it.

**Reporting.** Per role, never collapsed. Roles are configured
separately, fail separately, and have different remedies — a dead judge
role must never be reported as a dead target role.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass

from zicato.core.runtime import CallLLM
from zicato.models_config import MODEL_ROLES, ModelsConfig, RoleSpec, lazy_text_call_llm

#: Seconds one role's round trip may take. A bound rather than a budget: a
#: hung endpoint must fail its own role rather than wedge a diagnostic
#: the operator is waiting on.
ROLE_TIMEOUT_S = 30.0

#: The whole request, fixed and tiny. The probe asks whether a round
#: trip completes, never whether the answer is any good, so nothing here
#: may depend on which model happens to serve the role.
_PROBE_SYSTEM = "Answer in one word."
_PROBE_USER = "Reply with the word: ok"

#: How a role's callable is built. Injectable so tests drive the probe
#: with stubs instead of reaching an endpoint.
RoleCallLLMFactory = Callable[..., CallLLM]


@dataclass(frozen=True, slots=True)
class RoleProbe:
    """One configured role's round trip, and what came back.

    ``engine`` is the model id, or the dotted path for a ``call_llm``
    role: the thing the operator edits to fix this role. ``error`` is
    one line, empty when :attr:`ok`.
    """

    role: str
    engine: str
    ok: bool
    error: str = ""


def worker_call_llm(spec: RoleSpec, *, role: str) -> CallLLM:
    """Build a role's callable the way a tournament worker builds it.

    ``_tournament_worker._resolve_role_call_llm`` resolves a configured
    ``models_role`` spec through :func:`~zicato.models_config.lazy_text_call_llm`,
    so going through it here exercises whatever authentication the spec
    implies rather than assuming a scheme: a spec naming ``api_key_env``
    reads that variable, and a spec naming none authenticates however
    its endpoint does, which for a cloud-hosted endpoint is usually the
    ambient application-default credentials. Nothing in the probe path
    branches on how a role authenticates, so a keyless spec is probed on
    exactly the same path as a keyed one.
    """
    return lazy_text_call_llm(spec, role=role)


async def probe_role(
    spec: RoleSpec,
    *,
    role: str,
    build_call_llm: RoleCallLLMFactory = worker_call_llm,
    timeout_s: float = ROLE_TIMEOUT_S,
) -> RoleProbe:
    """Send one short request through ``role`` and report what happened.

    Every failure mode lands in the same place — a construction that
    raises, an endpoint that rejects the credential or the model id, a
    callable that returns something other than a ``str``, an endpoint
    that never answers — because to the operator they are one question
    ("is this role wired up?") with one report line each.
    """
    engine = spec.model or spec.call_llm or ""
    try:
        call_llm = build_call_llm(spec, role=role)
        # The model ARGUMENT is the spec's model or empty — never the dotted
        # path: a call_llm-form callable owns its model choice, and every
        # real caller passes "" for it (judge_runtime/builder.py). ``engine``
        # is only the report label.
        answer = await asyncio.wait_for(
            call_llm(_PROBE_SYSTEM, _PROBE_USER, spec.model or ""), timeout_s
        )
    except TimeoutError:
        return RoleProbe(role, engine, False, f"no answer within {timeout_s:g}s")
    except Exception as exc:  # noqa: BLE001 — any failure to answer is the defect
        return RoleProbe(role, engine, False, _one_line(exc))
    if not isinstance(answer, str):
        return RoleProbe(role, engine, False, f"returned {type(answer).__name__}, expected a str")
    return RoleProbe(role, engine, True)


async def probe_configured_roles(
    models: ModelsConfig,
    *,
    build_call_llm: RoleCallLLMFactory = worker_call_llm,
    timeout_s: float = ROLE_TIMEOUT_S,
) -> tuple[RoleProbe, ...]:
    """Probe every configured role, one at a time, in role order.

    Sequential: there are a handful of roles, and a report whose order
    depends on which endpoint answered first is harder to read than one
    that does not. Unconfigured roles are skipped exactly as the static
    validator skips them, so the two halves of the reachability check
    cover the same set of roles.
    """
    probes: list[RoleProbe] = []
    for role in MODEL_ROLES:
        spec = models.role(role)
        if spec.is_empty:
            continue
        probes.append(
            await probe_role(spec, role=role, build_call_llm=build_call_llm, timeout_s=timeout_s)
        )
    return tuple(probes)


def unreachable_roles(probes: tuple[RoleProbe, ...]) -> tuple[RoleProbe, ...]:
    """The probes that did not come back with a string."""
    return tuple(probe for probe in probes if not probe.ok)


def render_reachability(probes: tuple[RoleProbe, ...]) -> str:
    """Render the per-role report as plain text, verdict last.

    A workspace configuring no role gets an explicit statement that
    nothing was probed. "Not checked" and "checked and reachable" are
    different answers, and a report that renders them the same way
    tells an operator their credentials are good when nobody asked.
    """
    if not probes:
        return (
            "Reachability: no role is configured under `models`, so none was probed. "
            "That is not a statement that any role is reachable.\n"
        )
    lines = ["Reachability (one short request per configured role):"]
    for probe in probes:
        status = "ok" if probe.ok else "FAILED"
        detail = f": {probe.error}" if probe.error else ""
        lines.append(f"  [{status}] {probe.role} ({probe.engine}){detail}")
    failed = unreachable_roles(probes)
    if failed:
        named = ", ".join(probe.role for probe in failed)
        lines.append(f"  {len(failed)} of {len(probes)} configured roles did not answer: {named}")
    else:
        lines.append(f"  All {len(probes)} configured roles answered.")
    return "\n".join(lines) + "\n"


def _one_line(exc: BaseException) -> str:
    """The exception's type and its first line — endpoint errors are long."""
    body = str(exc).strip().splitlines()
    return f"{type(exc).__name__}: {body[0]}" if body else type(exc).__name__


__all__ = [
    "ROLE_TIMEOUT_S",
    "RoleCallLLMFactory",
    "RoleProbe",
    "probe_configured_roles",
    "probe_role",
    "render_reachability",
    "unreachable_roles",
    "worker_call_llm",
]
