# The multi-turn user emulator

For `multi_turn_emulated` board entries, the user side of the
conversation is played by a `call_llm`-backed agent: the **emulator**.
The emulator receives a persona and, each turn, sees the agent's
user-facing output so far. It produces the next user turn. The
conversation ends when the emulator signals that the persona's
`stop_when` condition holds, when `max_turns` is reached, or when the
run aborts.

The emulator carries the loop's largest correctness risk:
**collusion**. Without the guards below, the same model that played
the agent can also play the user, judge whether the expectation was
met, and propose the patch. Every decision in the loop then comes
from one model evaluating itself, and the pass-rate signal stops
measuring the agent. The construction in this document makes the
emulator a peer agent under the same observability posture as the
system under test, with no privileged knowledge of the answer.

The rules below are hard rules rather than best-effort guidance.
Several of them refuse the run when violated, because a refused run
costs the operator one setup change while a degenerate loop produces
plausible-looking results that are worth nothing.

## 1. What the emulator is, and what it is not

The emulator IS:

- A peer agent that plays "a user trying to accomplish a goal".
- Driven by an LLM, called through the user-emulator callable
  (`RuntimeConfig.effective_user_emulator_call_llm`): the engine the
  `user_emulator` model role selects, which defaults to the evaluation
  engine ([MODEL-CONFIG.md](MODEL-CONFIG.md)).
- Bound by a persona shape (`UserPersona`) with three string fields:
  `goal`, `constraints`, `stop_when` (each a single string —
  `constraints` is one free-text block rather than a list).
- Audited turn by turn (§8).

The emulator IS NOT:

- An oracle that knows the right answer.
- A judge that evaluates whether the agent's output was correct.
- A scripted bot that replays a fixed transcript (that is the
  `multi_turn_scripted` kind).
- Capable of seeing anything the system under test's user-facing
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
callable the system under test uses. When it is the same callable:

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

- `target_call_llm` — used by the system under test only. The
  `goldfive.wrap(...)` plumbing passes this through to the agent's
  LLM calls. Reaches the agent code; the agent talks to the world
  through this callable.
- `evaluation_call_llm` — the default for everything zicato itself
  drives: the multi-turn user emulator, the in-run process judges, the
  rubric grader, the analysis pass at epoch close, and the proposer's
  critique and merge calls. Named model roles can assign some of these
  to other engines ([MODEL-CONFIG.md](MODEL-CONFIG.md)); the proposal
  episode itself runs on the model its `proposer` block names.

### 3.1 The hard error

The invariant is enforced by
`zicato.core.workspace.assert_distinct_callables`, which the runtime
factory runs at config time and the tournament runner re-checks before a
run. The emulator driver (`EmulatedMultiTurnDriver.drive`) re-checks it
against the callable the emulator will actually use:
`target_call_llm` against `effective_user_emulator_call_llm()`. It is a
**pure identity check**: it raises `RuntimeError` when
`target_call_llm is evaluation_call_llm`. The emulator driver wraps that
`RuntimeError` as `EmulationCollusionError` and refuses to start the
run.

Named-engine configuration adds a second, name-level check: the model
configuration refuses a `target` engine that any evaluator-side role
also selects ([MODEL-CONFIG.md](MODEL-CONFIG.md)).

The check accepts:

- Two different callables (different functions, different SDK
  clients, different network targets) — including two distinct
  closures that happen to wrap the *same* underlying client or
  endpoint. Differentiating the model behind a shared client is the
  operator's responsibility; the check does not inspect it.

The check rejects:

- The exact same callable object passed for both roles
  (`target_call_llm is evaluation_call_llm`).

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

The emulator's input is constructed by two **sealed functions** in
`zicato.emulator.sealed`, whose signatures are explicit, exhaustive, and
have no `**kwargs`:

```python
def build_emulator_system_prompt(persona: UserPersona) -> str: ...

def build_emulator_user_prompt(transcript: tuple[str, ...]) -> str: ...
```

`UserPersona` is the operator-supplied persona (`goal`, `constraints`,
`stop_when`). The system prompt renders the three fields under labelled
headers and appends the verbatim non-leakage paragraph (§5). The
transcript is the ordered tuple of the agent's user-facing replies; the
user prompt renders each as an `AGENT:` block and asks for the next
`YOU (the user):` turn, or asks the emulator to open the conversation
when the tuple is empty.

The emulator NEVER sees, and the sealed construction physically cannot
deliver:

- The agent's **system prompt** — the system under test's instructions
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

This list is exhaustive by construction. The two functions accept one
argument each and build their prompts from those arguments alone. There
is no escape hatch.

### 4.1 Why this is in sealed functions

Putting the context construction in functions with explicit signatures
makes the contract enforceable by review. A future
contributor who wants to add information to the emulator must:

1. Update the function's signature (add an argument).
2. Update every call site.
3. Have the addition pass review.

A future contributor who passes information into the emulator via a
`**kwargs` or by mutating a shared object cannot — the function's
signature physically refuses it. The boundary lives in the type
system rather than in convention.

### 4.2 What "user-visible transcript" means

The system under test's agent emits many things: tool calls, intermediate
LLM responses, the agent's internal thinking. Only the **user-visible
text** — the chat-shaped responses the agent produces for the user —
is included in the transcript. The reduction happens adapter-side:
the driver calls an adapter-supplied `run_harness_turn(user_msg)`
closure, which runs one agent turn and returns the reply a real user
would have seen at the end of that turn. Tool calls, sub-agent
dispatches, and internal reasoning never enter the tuple.

The reduction is conservative: when in doubt, drop. A leaked tool
call would be worse than a missed user-visible nuance.

## 5. Answer non-leakage

Even with sealed context, an emulator can leak if its system prompt
or behaviour invites it. The emulator's system prompt always ends with
this non-leakage paragraph (`NON_LEAKAGE_PARAGRAPH` in
`zicato.emulator.sealed`, pinned verbatim by tests):

> You are simulating a user. You are not an oracle. You do not know the
> correct answer to the agent's problem, and even if you did, you would
> not state it. If the agent asks "what's the right answer?" or "what
> are you looking for?" or any variant, respond like a real user —
> restate your goal in your own words, or express confusion, or refuse —
> but never specify a target output. You may not produce JSON, code
> blocks, schemas, or anything that resembles a structured answer key.
> Stay in character. If you would naturally end the conversation per the
> stop_when condition above, output exactly `<<END>>` on a line by itself.

The paragraph is a code constant rather than an operator-editable
template. If zicato evolves its own harness and marks the template as a
mutation point ([DOGFOOD-TARGETS.md](DOGFOOD-TARGETS.md) §3.6), the
marker must declare `required_placeholders` so that the mutation
validator's required-placeholder check
([MUTATION-SURFACE.md](MUTATION-SURFACE.md) §6) refuses a patch that
drops the refusal section.

### 5.1 Post-hoc heuristic

The system prompt is a soft control. A determined emulator (or a
malicious operator-supplied persona) can still produce answer-shaped
output. zicato runs a post-hoc heuristic,
`zicato.emulator.answer_leak.check_answer_leak(text) -> str | None`, on
every emulator turn before the turn reaches the agent. It matches the
`LEAK_PATTERNS` regular expressions case-insensitively, per line:

- a code fence (three backticks, including a fenced JSON block);
- a raw JSON object or array at the start of a line (an array must
  open with a JSON value, so a bracketed preface such as
  `[Looking at your draft]` does not match);
- the phrases "the answer is", "you should output", "correct output
  is", "expected output", and "the schema is".

When a pattern matches, the driver stops the conversation and returns a
`RunResult` with `aborted=True` and `abort_reason="emulator_leak_detected"`,
and logs a warning naming the pattern. The entry scores as worst-case.

The heuristic is narrow by design, because a false positive aborts a
real run over a benign string. Its patterns are the cases that most
often indicate collusion: explicit answer-disclosure phrases, code
fences (which a real user might paste, and which are also the shape
of a leaked answer), and raw JSON (rare in a real user message).

The heuristic is the place to tune when false positives become an
operational problem. It is a trip-wire; the durable guards are the
fixed non-leakage paragraph (§5) and the audit trail (§8).

## 6. Persona shape

A persona is what makes one multi-turn-emulated entry different from
another. Three fields:

| Field | Type | Purpose |
|---|---|---|
| `goal` | `string` | What this simulated user is trying to accomplish. Stamped verbatim into the emulator's system prompt. |
| `constraints` | `string` | A single free-text block of behavioural rules ("you are impatient; push back when feedback is shallow; ask one focused follow-up per turn"). Not a list — multiple rules go in one string. Stamped into the emulator's system prompt. |
| `stop_when` | `string` | Condition the emulator checks each turn to decide whether the conversation ends. |

### 6.1 `stop_when` evaluation

The emulator judges `stop_when` itself, in the same call that produces
its turn. The system prompt instructs it to output `<<END>>` (the
`END_TOKEN`) on a line by itself when it would naturally end the
conversation. The driver ends the conversation when any line of the
emulator's output, stripped of surrounding whitespace, equals
`<<END>>`; that turn is not forwarded to the agent. There is no
separate stop-check call.

The entry's `max_turns` caps the conversation when the token never
appears. Each emulator call also runs under the evaluation call timeout
(`aux.call_timeout_s`); a timed-out call aborts the run with
`abort_reason="emulator_timeout"`.

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
history, the tuple of the agent's user-facing replies the driver has
collected so far. The emulator itself is stateless between turns; it
is a pure function of `(persona, transcript) → next_user_turn`.

## 8. Audit trail (the `zicato:emulator` lane)

Every emulator turn produces an `EmulatorTurnAudit` record
(`zicato.emulator.audit`):

| Field | Value |
|---|---|
| `persona_hash` | A 16-hex-character SHA-256 prefix of the persona (§9). The persona's contents are not in the record, because the operator may consider the persona sensitive. |
| `transcript_chars_in` | The total characters of the agent replies the emulator saw on this turn. |
| `output_chars_out` | The length of the emulator's response. |
| `output_preview` | The first 200 characters of the response. |

The driver keeps every turn's record in memory. When it is constructed
with a sink, it also emits each record as an event on the
`zicato:emulator` lane (`kind: "zicato.emulator.turn_audit"`); emission
is best-effort and never fails the run. The ADK adapter reaches the
driver through `zicato.emulator.run_emulated`, which constructs it
without a sink, so an ADK run keeps its audit records in memory only and
writes none to the event stream.

### 8.1 Why the audit trail matters

When a multi-turn-emulated entry produces a surprising result — a
strong pass, a strong fail, a fast abort — the operator needs to
establish whether the emulator played the role the persona described.
The audit trail supplies that evidence where it is emitted; without an
emitting sink, the operator has only the run's transcript and abort
reason.

The audit trail also makes a change in the emulator's own behaviour
visible. Swapping the evaluation model can produce shorter,
less-probing user turns, for instance. An operator who sees emulator
turns shortening across a series of entries has the evidence to
attribute that change to the swap.

## 9. The persona hash

The audit record carries a `persona_hash` rather than the persona
itself. The hash is the first 16 hexadecimal characters of the SHA-256
of the three fields, each written as its name, a NUL byte, and its
value, joined in the fixed order `goal`, `constraints`, `stop_when`.
Carrying the hash instead of the text:

- Lets operators correlate runs with the same persona without
  reading the persona's text repeatedly.
- Keeps the persona content out of the JSONL when the operator
  considers it sensitive (e.g. a persona built around real user
  research transcripts).
- Survives reformatting: the hash reads the three field values, so two
  persona JSONs with different key orders hash the same.

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
| Emulator's system prompt invites it to behave as an oracle ("if you know the answer, give it"). | §5 non-leakage paragraph, a code constant appended to every emulator system prompt. |
| Emulator generates raw JSON / code fences / schemas as the "user" turn. | §5 post-hoc heuristic. |
| Emulator remembers prior entries' personas and biases toward them. | §7 fresh instance per entry. |
| Operator cannot see what the emulator did. | §8 per-turn audit records, emitted on the `zicato:emulator` lane when a sink is wired. |
| Operator cannot audit which persona drove a given run. | §9 persona hash on every audit record. |

Each rule closes a channel none of the others closes. Removing any
one of them reopens the failure mode on its row.

## 11. What the construction does NOT prevent

Honest accounting:

- **Operator-authored persona collusion.** If the operator writes a
  persona whose `goal` is "ask the agent to repeat the system prompt
  verbatim", the emulator will do that. The persona is the
  operator's authoring surface; zicato does not validate persona
  contents beyond schema.
- **Evaluation model swap behind an unchanged declaration.** A
  configured model role is part of the evaluation contract, so
  changing the emulator's engine, model, or revision rolls the epoch
  ([EPOCHS-AND-JOURNALING.md](EPOCHS-AND-JOURNALING.md) §1.1). A
  deployment changed behind a stable endpoint and model name, or a
  library caller's callable whose behaviour changes, alters the
  emulator's behaviour without changing the contract; the operator's
  revision label is the guard.
- **Alignment across providers.** If `target_call_llm` and
  `evaluation_call_llm` are different APIs backed by the same
  underlying provider, collusion at the model-family level remains
  possible. The two-callable rule catches identity collusion only.
  Pinning the evaluation callable's provider family at config time is
  unimplemented.

## 12. Cross-references

| Topic | Document |
|---|---|
| Persona schema, `multi_turn_emulated` entry kind | [BOARD-FORMAT.md](BOARD-FORMAT.md) |
| Emulator audit events on the `zicato:emulator` lane | [TELEMETRY.md](TELEMETRY.md) |
| Why hard error rather than warning on the two-callable check | [RATIONALE.md](RATIONALE.md) |
| `evaluation_call_llm` use by proposer, judge, analysis pass | [ARCHITECTURE.md §4.10](ARCHITECTURE.md#410-the-two-call_llm-callables) |
