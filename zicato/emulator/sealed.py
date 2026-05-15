"""Sealed context construction for the multi-turn user emulator.

The two functions in this module are the ONLY way the runtime is allowed
to build the system- and user-prompt strings that go to the emulator
LLM. Their signatures are explicit (no ``**kwargs``, no optional inputs
that could carry side data) so the static surface of the call site is
the audit boundary.

The non-leakage paragraph baked into :func:`build_emulator_system_prompt`
is copied verbatim from the project's emulator policy. Tests assert the
paragraph appears unchanged; do not paraphrase it.
"""

from __future__ import annotations

from zicato.core.types import UserPersona

# The non-leakage rules. Copied verbatim into the system prompt. Tests
# assert this exact text is present, so editing it requires also editing
# the tests — that is intentional friction.
NON_LEAKAGE_PARAGRAPH = (
    "You are simulating a user. You are not an oracle. You do not know the "
    "correct answer to the agent's problem, and even if you did, you would "
    "not state it. If the agent asks \"what's the right answer?\" or \"what "
    "are you looking for?\" or any variant, respond like a real user — "
    "restate your goal in your own words, or express confusion, or refuse — "
    "but never specify a target output. You may not produce JSON, code "
    "blocks, schemas, or anything that resembles a structured answer key. "
    "Stay in character. If you would naturally end the conversation per the "
    "stop_when condition above, output exactly `<<END>>` on a line by itself."
)

#: The token the emulator emits on a line by itself to signal that its
#: persona's ``stop_when`` has fired. The driver loop treats this as the
#: termination signal alongside the entry's ``max_turns`` cap.
END_TOKEN = "<<END>>"


def build_emulator_system_prompt(persona: UserPersona) -> str:
    """Return the emulator's system prompt.

    The prompt contains:

    * The persona's :attr:`UserPersona.goal`, :attr:`UserPersona.constraints`,
      and :attr:`UserPersona.stop_when` rendered verbatim under labeled
      headers.
    * The verbatim :data:`NON_LEAKAGE_PARAGRAPH`.
    * An explicit refusal posture stating the emulator is a user and not
      an oracle.

    Explicit signature — NO ``**kwargs``. Callers cannot smuggle extra
    context into the system prompt. The emulator never sees the agent's
    system prompt, the entry's expectation, the predicate source, or any
    other zicato-internal state.

    Parameters
    ----------
    persona:
        The :class:`UserPersona` for the entry being emulated.

    Returns
    -------
    str
        The fully-rendered system prompt.
    """
    return (
        "You are a simulated user in a multi-turn conversation with an agent. "
        "You are a user, not an oracle.\n"
        "\n"
        "## Your goal\n"
        f"{persona.goal}\n"
        "\n"
        "## Your constraints\n"
        f"{persona.constraints}\n"
        "\n"
        "## When you would naturally stop talking\n"
        f"{persona.stop_when}\n"
        "\n"
        "## Non-leakage rules (mandatory)\n"
        f"{NON_LEAKAGE_PARAGRAPH}\n"
    )


def build_emulator_user_prompt(transcript: tuple[str, ...]) -> str:
    """Return the conversation-so-far block for the emulator.

    The transcript carries ONLY the agent's user-facing outputs in order.
    Tool calls, plan traces, chain-of-thought, goldfive events, and any
    other internal state are not in this tuple by construction (the
    caller is the runner, which assembles ``transcript`` from
    ``RunResult.transcript``-shaped data — user-facing only).

    Each agent turn is prefixed with ``AGENT:`` and the trailing prompt
    asks the emulator to produce the next user turn (``YOU (the user):``).
    When ``transcript`` is empty, the user is asked to open the
    conversation per their persona.

    Parameters
    ----------
    transcript:
        Tuple of agent user-facing turns, ordered.

    Returns
    -------
    str
        The fully-rendered user-prompt body.
    """
    if not transcript:
        return (
            "The conversation has not started yet. Open the conversation "
            "with a single natural user message that pursues your goal.\n"
            "\n"
            "YOU (the user):"
        )

    lines: list[str] = ["Conversation so far:\n"]
    for turn in transcript:
        lines.append("AGENT:")
        lines.append(turn)
        lines.append("")
    lines.append("Produce your next user turn now. Stay in character.")
    lines.append("")
    lines.append("YOU (the user):")
    return "\n".join(lines)


__all__ = [
    "NON_LEAKAGE_PARAGRAPH",
    "END_TOKEN",
    "build_emulator_system_prompt",
    "build_emulator_user_prompt",
]
