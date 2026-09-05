"""The callables the ``target`` and ``evaluation`` model roles run on.

A model role is a job, and an engine is what that job runs on. An engine
usually names a model and an endpoint; an engine naming a ``call_llm``
dotted path instead points at one of these — a plain async callable
taking ``(system, user, model)`` and returning the answer text. That is
the form an offline or deterministic project uses, and it is why this
example needs no endpoint and no credential.

Two roles have to be answered before a round opens:

``target``
    What the system under test runs on. This example's note writer uses
    no model at all, so the callable below is never reached; the role
    still needs an answer because the loop hands one to every adapter.
``evaluation``
    What zicato's own internal work runs on — the judges, the user
    emulator, and the epoch analysis. This example's board grades with
    predicates and declares no judges, so the only caller is the
    analysis pass, and an empty answer leaves an empty analysis.

The two must be distinct objects. The loop refuses a workspace where
they are the same one, because a system under test sharing a callable
with its own grader can collude with it.

Replacing these is the second edit to make. Point each engine at a real
model in ``config.json`` and delete this module.
"""

from __future__ import annotations


async def target_model(system: str, user: str, model: str) -> str:
    """The ``target`` role: what the system under test would call.

    Unreached in this example — the note writer composes its answer from
    the style policy — so it returns nothing rather than pretending to
    be a model.
    """
    del system, user, model
    return ""


async def evaluation_model(system: str, user: str, model: str) -> str:
    """The ``evaluation`` role: what zicato's own model work calls.

    Returns nothing, which leaves the epoch analysis empty. Every
    decision this example's loop makes comes from the board, so nothing
    that decides a promotion passes through here.
    """
    del system, user, model
    return ""


__all__ = ["evaluation_model", "target_model"]
