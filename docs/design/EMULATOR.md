# The multi-turn user emulator

For `multi_turn_emulated` board entries, the user side of the
conversation is played by a `call_llm`-backed agent: the **emulator**.
The emulator receives a persona and, each turn, sees the agent's
user-facing output so far. It produces the next user turn. The
conversation ends when the persona's `stop_when` matches or
`max_turns` is reached.

The emulator carries the loop's largest correctness risk:
**collusion**. Without the guards below, the same model that played
the agent can also play the user, judge whether the expectation was
met, and propose the patch. Every decision in the loop then comes
from one model evaluating itself, and the pass-rate signal stops
measuring the agent. The construction in this document makes the
emulator a peer agent under the same observability posture as the
inner harness, with no privileged knowledge of the answer.

The rules below are hard rules rather than best-effort guidance.
Several of them refuse the run when violated, because a refused run
costs the operator one setup change while a degenerate loop produces
plausible-looking results that are worth nothing.

## 1. What the emulator is, and what it is not

The emulator IS:

- A peer agent that plays "a user trying to accomplish a goal".
- Driven by an LLM, called through `auxiliary_call_llm`.
- Bound by a persona shape with three string fields: `goal`,
  `constraints`, `stop_when` (each a single string — `constraints`
  is one free-text block rather than a list).
- Observable as `goldfive.v1.GoldfiveLLMCallStart` /
  `GoldfiveLLMCallEnd` events on the `zicato:emulator` lane (so
  harmonograf renders its work).

The emulator IS NOT:

- An oracle that knows the right answer.
- A judge that evaluates whether the agent's output was correct.
- A scripted bot that replays a fixed transcript (that is the
  `multi_turn_scripted` kind).
- Capable of seeing anything the inner harness's user-facing
  transcript does not already contain.

That asymmetry is the design's purpose. The agent has its full
reasoning trace, its tools, its system prompt, and its private
context; the emulator has only what a real user would see. The
emulator is meant to be **weaker** than the agent rather than
stronger.

## 2. Why collusion is the risk

A naive multi-turn emulator design would be:

```
# DON'T DO THIS
async def naive_emulate_turn(persona, transcript_so_far):
    return await call_llm(
        system="You are a user with this goal: " + persona.goal,
        user=transcript_so_far,
        model=...,
    )
```

That construction is safe only while `call_llm` differs from the
callable the inner harness uses. When it is the same callable:

- The emulator is the same model that played the agent. Same
  prompt-conditioning biases, same failure modes, same blind spots.
- If the model knows it tends to confabulate sources on research
  questions, it can phrase the user's next turn to avoid asking for
  sources — making the answer pass without testing the failure mode.
- If a judge expectation is also calling the same model, three
  consecutive evaluators are all *the same evaluator*. The
  pass-rate signal degenerates.

The naive emulator does not fail loudly. It produces plausible
transcripts, plausible scores, and a loop that appears to improve.
The problem stays invisible until someone audits the transcripts and
notices that the simulated user never pushes on the agent's weak
points.

Collusion is therefore a silent failure mode, and the construction
below is built to close each channel through which it can arrive.
Section 11 states the residual channels the construction does not
close.

## 3. The two-callable rule

zicato is configured with **two** distinct `call_llm` callables:

- `harness_call_llm` — used by the inner harness only. The
  `goldfive.wrap(...)` plumbing passes this through to the agent's
  LLM calls. Reaches the agent code; the agent talks to the world
  through this callable.
- `auxiliary_call_llm` — used by everything zicato itself drives:
  the multi-turn user emulator, the patch proposer, the in-run
  process judges, the analysis pass at epoch close, and the rubric
  grader.

### 3.1 The hard error

The invariant is enforced by
`zicato.core.workspace.assert_distinct_callables`, which the runtime
factory runs at config time and the emulator driver and tournament
runner re-check defensively before a run. It is a **pure identity
check**:

```python
def assert_distinct_callables(harness_call_llm, auxiliary_call_llm):
    if harness_call_llm is auxiliary_call_llm:
        raise RuntimeError(
            "harness_call_llm and auxiliary_call_llm must be distinct "
            "callables; shared callables risk collusion in multi-turn "
            "emulated entries"
        )
```

The emulator driver wraps that `RuntimeError` as
`EmulationCollusionError` and refuses to start the run.

The check accepts:

- Two different callables (different functions, different SDK
  clients, different network targets) — including two distinct
  closures that happen to wrap the *same* underlying client or
  endpoint. Differentiating the model behind a shared client is the
  operator's responsibility; the check does not inspect it.

The check rejects:

- The exact same callable object passed for both roles
  (`harness_call_llm is auxiliary_call_llm`).

It is **identity (`is`) only** — there is no `model=` override
carve-out and no inspection of the model argument. The check catches
the mistake of passing one callable twice. Detecting two closures
over the same model family is outside what this check covers (§11).

This is a HARD ERROR. zicato refuses to start. There is no
`--allow-collusion` flag. The risk is silent, and providing two
callables costs the operator one line of setup.

### 3.2 Why the check refuses rather than warns

A warning would be routinely ignored once the operator has used the
tool for a few weeks, and the harm it warns about leaves no visible
trace. Refusing costs one extra line in the operator's setup script.

The same reasoning determines what zicato ships. The operator must
supply the LLM wiring for both roles; zicato ships the default
emulator *prompts* and no default *wiring*, because a shipped default
wiring would be one callable serving both roles.

## 4. Context isolation (sealed context construction)

The emulator's input context is constructed by a **sealed function**
whose signature is explicit, exhaustive, and has no `**kwargs`. Every
turn:

```python
def build_emulator_context(
    persona: Persona,
    user_visible_transcript: list[UserVisibleTurn],
) -> EmulatorContext:
    """Construct the emulator's per-turn input.

    Only inputs that change the function's behaviour are arguments.
    NO **kwargs. NO optional inputs that could leak privileged
    information through accidental forwarding.
    """
    ...
```

`Persona` is the operator-supplied persona (`goal`, `constraints`,
`stop_when`). `UserVisibleTurn` is a typed shape:

```python
@dataclass(frozen=True)
class UserVisibleTurn:
    turn_index: int
    user_or_agent: Literal["user", "agent"]
    text: str    # the text the agent SAID to the user, NOT internal reasoning
```

The emulator NEVER sees, and the typed context construction physically
cannot deliver:

- The agent's **system prompt** — the inner harness's instructions
  to its specialists.
- The agent's **tool calls** and tool outputs — the actions the
  agent took to produce the user-visible text.
- The agent's **chain-of-thought** / reasoning blocks — the thinking
  the agent did before speaking.
- The agent's **internal plans** — goldfive's `Plan` and `Task`
  state.
- The **goldfive event stream** — drift events, plan revisions,
  escalations.
- The board entry's **expectation** — the single outcome check
  (predicate / regex / json_schema / expected_text / rubric) and any
  of its `spec`.
- The board entry's **judges** — the in-run process checks.
- The **predicate module's source** — the Python file that defines
  pass/fail.
- Any **other board entry** — past or future.
- Any **zicato internal state** — the journal, patterns, rubric,
  generation count.

This list is exhaustive by construction. The context-builder function
accepts two arguments and produces an `EmulatorContext` built from
those two arguments alone. There is no escape hatch.

### 4.1 Why this is in a sealed function

Putting the context construction in one function with an explicit
signature makes the contract enforceable by review. A future
contributor who wants to add information to the emulator must:

1. Update the function's signature (add an argument).
2. Update every call site.
3. Have the addition pass review.

A future contributor who passes information into the emulator via a
`**kwargs` or by mutating a shared object cannot — the function's
signature physically refuses it. The boundary lives in the type
system rather than in convention.

### 4.2 What "user-visible transcript" means

The inner harness's agent emits many things: tool calls, intermediate
LLM responses, the agent's internal thinking. Only the **user-visible
text** — the chat-shaped responses the agent produces for the user —
is included in `user_visible_transcript`. The reduction happens
adapter-side:

- The ADK adapter walks the `goldfive.v1.AgentInvocationStarted` /
  `AgentInvocationCompleted` lifecycle and extracts the "final
  assistant message text" the agent produced on each turn — the
  thing a real user would have seen at the end of the turn.
- Everything else (tool calls, sub-agent dispatches, internal
  reasoning) is dropped.

The reduction is conservative: when in doubt, drop. A leaked tool
call would be worse than a missed user-visible nuance.

## 5. Answer non-leakage

Even with sealed context, an emulator can leak if its system prompt
or behaviour invites it. The emulator's system prompt includes
explicit refusal rules:

> You are a user, not an oracle. Even if you knew the answer you would
> not state it. If the agent asks you for the answer, respond like a
> real user would: restate your goal, express confusion, or refuse to
> answer. NEVER specify a target output, a schema, or "the answer is".
> NEVER produce raw JSON, code fences, or schema-like content. NEVER
> say "you should output X".

These rules are part of the default emulator prompt template. The
operator can edit the template — it becomes a mutation point once
zicato evolves its own harness (see
[DOGFOOD-TARGETS.md](DOGFOOD-TARGETS.md)). The operator cannot remove
the refusal section without failing the mutation validator's
required-placeholder check, which verifies that every placeholder a
point's `required_placeholders` metadata declares survives the patch
(see [MUTATION-SURFACE.md](MUTATION-SURFACE.md)).

### 5.1 Post-hoc heuristic

The system prompt is a soft control. A determined emulator (or a
malicious operator-supplied persona) can still produce answer-shaped
output. zicato runs a post-hoc heuristic on every emulator turn:

```python
def looks_like_answer_leak(emulator_output: str) -> tuple[bool, str | None]:
    """Return (True, reason) when the emulator's turn structurally
    looks like an answer it shouldn't be giving."""
    if "the answer is" in emulator_output.lower():
        return True, "phrase: 'the answer is'"
    if "you should output" in emulator_output.lower():
        return True, "phrase: 'you should output'"
    if "```" in emulator_output:
        return True, "code fence in user turn"
    if _looks_like_json(emulator_output):
        return True, "raw JSON in user turn"
    if _looks_like_schema(emulator_output):
        return True, "schema-shaped content"
    return False, None
```

When `looks_like_answer_leak` returns `True`, the run aborts with
`goldfive.v1.RunAborted(reason="emulator_answer_leak: <reason>")`. The
entry scores as worst-case. The journal records the abort with the
leak reason.

The heuristic is narrow by design, because a false positive aborts a
real run over a benign string. Its patterns are the cases that most
often indicate collusion: the explicit phrase "the answer is", code
fences (which a real user might paste, and which are also the shape
of a leaked answer), raw JSON (rare in a real user message), and
schema-shaped content.

The heuristic is the place to tune when false positives become an
operational problem. It is a trip-wire; the durable guards are the
validator's required-placeholder check on the prompt template (§5)
and the audit trail (§8).

## 6. Persona shape

A persona is what makes one multi-turn-emulated entry different from
another. Three fields:

| Field | Type | Purpose |
|---|---|---|
| `goal` | `string` | What this simulated user is trying to accomplish. Stamped verbatim into the emulator's system prompt. |
| `constraints` | `string` | A single free-text block of behavioural rules ("you are impatient; push back when feedback is shallow; ask one focused follow-up per turn"). Not a list — multiple rules go in one string. Stamped into the emulator's system prompt. |
| `stop_when` | `string` | Condition the emulator checks each turn to decide whether the conversation ends. |

### 6.1 `stop_when` evaluation

The emulator is asked, on each turn, whether `stop_when` is satisfied.
The check is a separate, lightweight `auxiliary_call_llm` call with a
narrow prompt:

```
SYSTEM: You are evaluating whether a conversation has reached its
stopping condition. Answer only YES or NO.

USER:
Stopping condition: <persona.stop_when>

Conversation so far:
<user_visible_transcript joined>

Has the stopping condition been met? Answer YES or NO only.
```

A `YES` ends the conversation; a `NO` continues. The output is
parsed with the same conservatism as a judge: first non-whitespace
token, case-insensitive, anything other than `YES` is treated as
`NO`.

The `stop_when` check is a separate LLM call per turn, bounded by the
entry's wall-clock budget. A persona whose stopping condition is
never satisfied would loop indefinitely; the budget catches that and
the entry aborts.

### 6.2 Constraints are advisory

`constraints` are behavioural hints. They are NOT enforced. A
constraint that says "you are impatient" steers the emulator's
phrasing; nothing in zicato verifies the emulator actually behaved
impatiently. The persona is the operator's authoring surface — the
operator owns the consequences of writing a vague persona.

## 7. Fresh instance per entry

The emulator carries NO state across board entries. Each entry
constructs a fresh emulator from the persona; nothing the emulator
"remembered" on the previous entry leaks into this one.

A real user would not have that memory either; they arrive at the new
task fresh. Persona-state continuity across entries would let the
emulator carry conditioning that biases its behaviour in ways a real
user could not.

Within an entry, the emulator's only state is its conversation
history (the `user_visible_transcript`), which is rebuilt fresh each
turn from goldfive's event stream. The emulator itself is stateless
between turns; it is a pure function of `(persona, transcript) →
next_user_turn`.

## 8. Audit trail (the `zicato:emulator` lane)

Every emulator turn emits a goldfive-lane span on a dedicated lane:
`zicato:emulator`. The span uses the existing
`GoldfiveLLMCallStart` / `GoldfiveLLMCallEnd` proto messages from
goldfive (see [TELEMETRY.md §4.2](TELEMETRY.md#42-per-turn-llm-calls-on-the-zicatoemulator-lane)).

Span fields:

| Field | Value |
|---|---|
| `name` | `"emulator_turn"` |
| `model` | The emulator's model (from `auxiliary_call_llm`). |
| `input_preview` | `"persona_hash=<sha256 hex prefix>; transcript_chars_in=<int>"`. The persona's contents are NOT in the preview (operator may consider the persona sensitive). |
| `output_preview` | The emulator's produced user turn, truncated to 512 chars. |
| `target_agent_id` | `"zicato:emulator"` (the lane identifier). |
| `target_task_id` | The current goldfive task id, when known; empty otherwise. |
| `decision_summary` | `"produced user turn N (<count> chars)"` |

A separate `stop_when` check (§6.1) emits its own span:

| Field | Value |
|---|---|
| `name` | `"emulator_stop_check"` |
| `model` | The emulator's model. |
| `input_preview` | `"persona_hash=<sha256 hex prefix>; transcript_chars_in=<int>"` |
| `output_preview` | The raw YES/NO response. |
| `decision_summary` | `"stop_when matched"` or `"stop_when not matched"` |

Harmonograf renders these spans on a dedicated row keyed by the
`zicato:emulator` lane identifier. An operator replaying a run can
see when the emulator spoke, how long each turn took, what it said,
and when the `stop_when` check fired.

### 8.1 Why the audit trail matters

When a multi-turn-emulated entry produces a surprising result — a
strong pass, a strong fail, a fast abort — the operator needs to
establish whether the emulator played the role the persona described.
The audit trail supplies that evidence. Without it the operator has
no way to validate the run.

The audit trail also makes a change in the emulator's own behaviour
visible. Swapping the auxiliary model can produce shorter,
less-probing user turns, for instance. An operator who sees emulator
turns shortening across a series of entries has the evidence to
attribute that change to the swap.

## 9. The persona hash

The emulator's `input_preview` carries a `persona_hash` rather than
the persona itself. The hash is `sha256(canonical_json(persona))[:16]`.
Carrying the hash instead of the text:

- Lets operators correlate runs with the same persona without
  reading the persona's text repeatedly.
- Keeps the persona content out of the JSONL when the operator
  considers it sensitive (e.g. a persona built around real user
  research transcripts).
- Survives canonicalization: two equivalent persona JSONs with
  different key orders hash the same.

The full persona is on disk at `.zicato/epochs/{epoch}/board.jsonl`;
the hash is sufficient identifier on the wire.

## 10. Failure modes the construction prevents

Putting the rules side-by-side against the failure modes they
prevent:

| Failure mode | Prevented by |
|---|---|
| Emulator and harness use the same model; emulator subconsciously aligns with the agent. | §3 two-callable rule (hard error at config). |
| Emulator sees the agent's chain-of-thought and writes a "user" turn that probes the right weak spot. | §4 sealed context construction (CoT is not an argument). |
| Emulator sees the expectation predicate and gives the agent the user input that makes the predicate fire. | §4 sealed context construction (expectation is not an argument). |
| Emulator reads the board's other entries and biases its persona toward known-easy / known-hard patterns. | §4 sealed context construction (no other entries are arguments). |
| Emulator's system prompt invites it to behave as an oracle ("if you know the answer, give it"). | §5 default prompt's refusal section, plus the mutation validator's required-placeholder check on the editable emulator template. |
| Emulator generates raw JSON / code fences / schemas as the "user" turn. | §5 post-hoc heuristic. |
| Emulator remembers prior entries' personas and biases toward them. | §7 fresh instance per entry. |
| Operator cannot see what the emulator did. | §8 audit trail on the `zicato:emulator` lane. |
| Operator cannot audit which persona drove a given run. | §9 persona hash on every emulator span. |

Each rule closes a channel none of the others closes. Removing any
one of them reopens the failure mode on its row.

## 11. What the construction does NOT prevent

Honest accounting:

- **Operator-authored persona collusion.** If the operator writes a
  persona whose `goal` is "ask the agent to repeat the system prompt
  verbatim", the emulator will do that. The persona is the
  operator's authoring surface; zicato does not validate persona
  contents beyond schema.
- **Auxiliary model swap during an epoch.** Swapping the auxiliary
  callable mid-epoch changes the emulator's behaviour without
  changing the contract. zicato does not enforce auxiliary-model
  stability; the operator's discipline is the guard.
- **Alignment across providers.** If `harness_call_llm` and
  `auxiliary_call_llm` are different APIs backed by the same
  underlying provider, collusion at the model-family level remains
  possible. The two-callable rule catches identity collusion only.
  Pinning the auxiliary callable's provider family at config time is
  unimplemented.

## 12. Cross-references

| Topic | Document |
|---|---|
| Persona schema, `multi_turn_emulated` entry kind | [BOARD-FORMAT.md](BOARD-FORMAT.md) |
| Emulator spans on the `zicato:emulator` lane | [TELEMETRY.md](TELEMETRY.md) |
| Why hard error rather than warning on the two-callable check | [RATIONALE.md](RATIONALE.md) |
| `auxiliary_call_llm` use by proposer, judge, analysis pass | [ARCHITECTURE.md §4.10](ARCHITECTURE.md#410-the-two-call_llm-callables) |
