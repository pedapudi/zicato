# Reasoning-model semantics in the `call_llm` seam

> **Status: design proposal — not implemented.** This document records a
> reliability failure observed on a served reasoning model and proposes a
> reasoning-model-aware `call_llm` adapter that any backend opts into. It
> is companion to
> [ARCHITECTURE.md §4.10](ARCHITECTURE.md#410-the-two-call_llm-callables)
> (the two-callable seam), [EMULATOR.md §3](EMULATOR.md#3-the-two-callable-rule)
> (the collusion rule the seam enforces), and [PROPOSER.md](PROPOSER.md)
> (the first consumer hurt by the gap). The adapter proposed in §4 is not
> built; the parse-side mitigations described in §3 are present in the
> tree.

zicato's most common target is an agent running on a **reasoning model** —
a model that emits a chain-of-thought scratchpad before it answers. zicato's
own LLM seam, the two-callable `(system, user, model) -> str` contract
(`CallLLM` in `zicato/core/runtime.py`), represents no part of that
behaviour. It assumes the string it receives is the answer. For a reasoning
model that assumption is false in a way that is not merely cosmetic: it
produces a non-deterministic, un-retryable failure that has broken a live
proposer round. The measurements in §2 establish the failure; §4 proposes
making reasoning-model semantics a concern of the seam rather than of each
workspace's backend.

---

## 1. The seam's single-channel assumption

The contract is one line (`zicato/core/types.py`, re-exported from
`zicato/core/runtime.py`):

```python
#: The model-agnostic LLM-call shape used everywhere in zicato.
CallLLM = Callable[[str, str, str], Awaitable[str]]
```

`(system, user, model) -> response`. A `RuntimeConfig` binds **two** of these
(plus an optional third for judges): `harness_call_llm` for the inner harness
and `auxiliary_call_llm` for everything zicato itself runs — the proposer, the
judges, the analysis pass, the multi-turn emulator. The two MUST differ by
callable identity or explicit `model=` override; that is the collusion guard
([ARCHITECTURE.md §4.10](ARCHITECTURE.md#410-the-two-call_llm-callables),
[EMULATOR.md §3](EMULATOR.md#3-the-two-callable-rule)).

The seam is model-agnostic: zicato never inspects or switches on `model`,
and it names no vendor SDK. That neutrality is correct and worth keeping.
Model-agnostic has, however, come to carry a second and unintended
assumption, that the returned text is clean answer text. The seam's return
type is a single `str`, and every consumer treats that string as the model's
**answer**. There is exactly one channel, and it is presumed to hold the
requested answer.

A reasoning model does not honour that presumption.

---

## 2. The reality the seam ignores

The served test model is a reasoning model (a gemma-class reasoning model,
served via vLLM). On every call it produces **two** channels rather than
one:

- a `reasoning` channel — the scratchpad / chain-of-thought, the contents of
  the `<think>` block; and
- a `content` channel — the actual answer, which the model only begins to fill
  **once `<think>` terminates**.

The chat template gates `content` on the close of the thinking block. Until the
model emits the stop token that ends reasoning, `content` is **empty** and the
entire token budget is being spent on the scratchpad. A single-`str` return
type therefore cannot distinguish "the model answered" from "the model is
still thinking and never got to the answer".

### 2.1 The runaway, with numbers

On large or complex prompts, which are the prompts zicato's proposer and
judges send, the model **non-deterministically fails to terminate
reasoning**. Observed on the served test model:

- A proposer prompt burned the **full 16384-token output budget on reasoning**
  (≈64K characters of scratchpad) and emitted **empty `content`**. No answer
  was produced at all.
- The *same* prompt converged only when the budget was raised to **32768
  tokens**.
- It is not a fixed cost. vLLM's continuous batching makes the realized
  reasoning length **non-deterministic even at temperature 0**: the same prompt
  sometimes converges at **~7.5K tokens** and sometimes runs away past 16K. So
  a budget that is comfortably sufficient on one call is exhausted on the next,
  for identical input.

The failure is a **chat-template-level instability** in when the model
decides to stop thinking, amplified by batching nondeterminism, rather than a
prompt that is too hard. No wording of the prompt removes it, because the
prompt is not the part that runs away.

### 2.2 Substituting the scratchpad for the answer

The workspace wrapper in use at the time made the failure worse than an empty
answer. Its default behaviour was to return the raw `reasoning` scratchpad
whenever `content` was empty, so that the caller received something rather
than an empty string. The effect was to feed the proposer 64K characters of
thinking-text containing **no JSON**, producing a long run of `could not
extract a JSON object from the response` parse failures. Returning the
scratchpad disguises a non-answer as an answer, and every downstream parser
then fails on text that was never meant to be parsed.

**The scratchpad is not a fallback for the answer.** A reasoning-aware seam
must return `content` and only `content`. An empty `content` is a distinct,
named condition, and it does not authorize substituting the scratchpad.

### 2.3 Why the proposer's retry loop cannot fix it

The proposer already runs a bounded **parse-retry loop**
(`zicato/proposer/proposer.py`): on a parse failure it re-prompts with a repair
section that echoes back the malformed output and, when the prior response was
empty, instructs the model to *"skip all reasoning and emit the JSON object
immediately"* (`feedback_was_empty`, `zicato/proposer/prompts.py`). That is a
repair turn for *prompt-shaped* mistakes — a stray fence, prose around the
object, a `<think>` block that leaked into otherwise-good output.

It cannot fix a chat-template runaway. Re-prompting **re-runs the runaway**:
the same template, the same budget, the same batching
nondeterminism. Each retry is an independent draw from the same unstable
distribution, burning a full budget's worth of tokens to (usually) produce
another empty `content`. The loop is bounded, so it exhausts its attempts and
the round fails. Worse, the "skip all reasoning" instruction is advisory — a
reasoning model under a thinking-gated template does not reliably honour an
in-prompt request to not think. A remedy has to live **below** the prompt, at
the seam.

---

## 3. The parse-side mitigations in the tree

zicato already handles reasoning text in three places, all of them
**downstream of the seam**, on the parse side. None addresses the runaway:

- **Reasoning-wrapper stripping** (`zicato/proposer/structured.py`,
  `_strip_reasoning_wrappers`) — removes `<think>…</think>`,
  `<thinking>…</thinking>`, `<reasoning>…</reasoning>` blocks before JSON
  extraction. Salvages output where the answer *survived* alongside the
  scratchpad; useless when `content` is empty.
- **Empty-vs-malformed discrimination** (`parse_experiment_json`,
  `zicato/proposer/structured.py`) — distinguishes an empty response
  ("likely spent its entire output budget on reasoning") from a malformed one,
  so the retry feedback can target the failure mode.
- **The verifier's findings turn** (`foe.Verified`, declared by
  `build_contract` in `zicato/proposer/foe_request.py`) — the repair the
  episode is given when its working copy does not read back as a
  well-formed patch set, described in §2.3.

Those three are the whole of it. Nothing in the **seam** itself — `CallLLM`,
the runtime binding, the ADK text shim (`zicato/adapters/adk.py`) — knows that
a reasoning model has two channels, that `content` can be legitimately empty,
or that a runaway is a recoverable condition with a deterministic remedy. The
adapters flatten a request to `(system, user)` text and return a single
string. The channel split happens, or fails to happen, inside the operator's
own backend, where zicato cannot observe it. **The seam models no reasoning
channel, and the only working mitigation lives in a workspace-local helper
script that is not part of zicato.** Every workspace that points zicato at a
reasoning model has to re-discover and re-implement that helper, or hit the
runaway in production.

---

## 4. Proposal — a reasoning-aware `call_llm` adapter

Make reasoning-model semantics a first-class, zicato-provided concern: a
**reasoning-aware wrapper** that takes a raw, channel-emitting backend callable
and returns a `CallLLM` honouring the existing `(system, user, model) -> str`
contract — so it drops into either seam (`harness_` / `auxiliary_`) with no
change to any caller. The wrapper owns four behaviours.

### 4.1 Model the two channels explicitly

Internally, the wrapper understands the backend yields a `(reasoning, content)`
pair (however the backend surfaces it — a structured field, a `<think>`-delimited
stream, separate completion fields). It is responsible for the split; callers
never see it. The public return remains a single `str`.

### 4.2 Return only the content channel

The wrapper returns `content` and only `content`. When `content` is empty, it
**does not** substitute `reasoning`; §2.2 records what that substitution
costs. An empty `content` is a named condition that triggers §4.4, and it
never releases the scratchpad to a caller that will try to parse it.

### 4.3 A configurable thinking budget

The wrapper carries a **configurable thinking-token budget**, separate from and
larger than the answer budget — sized so that a typical reasoning trace
converges with margin (the measurements in §2.1 show 16384 is too low for
proposer-scale prompts, while 32768 held). The budget is configuration rather
than contract: it lives on the wrapper or the workspace `models` block, never
in `CallLLM`'s signature. A budget alone does not guarantee termination,
because reasoning length is nondeterministic (§2.1), which is why it is paired
with §4.4 rather than relied upon.

### 4.4 A deterministic think→extract continuation fallback

When a call runs away — the budget is exhausted with empty `content` — the
wrapper does **not** re-ask the same question (that re-runs the runaway, §2.3).
Instead it makes a **second, different call**: it re-asks for *only the answer*,
with **thinking suppressed** at the template level (the backend's
`enable_thinking: false` / equivalent), optionally seeding that call with the
reasoning already produced. Suppressing thinking removes the thing that ran
away, so the formatting/answer step is deterministic — it cannot burn its budget
on a scratchpad it is not allowed to emit. The workspace helper demonstrated
this shape; the proposal is to **own it in zicato** so that each workspace
does not re-implement it.

The fallback is a property of the wrapper and is transparent to callers: from
the proposer's view, the seam returns one answer string. The proposer keeps
its parse-repair loop, which under the wrapper sees a clean `content` to parse
instead of a 64K scratchpad.

### 4.5 Where it lives — provided, opted-into

Two placements are possible:

1. **Push it onto every workspace backend**, which is what happens without a
   provided wrapper. Each operator wires reasoning handling into their own
   `auxiliary_call_llm` or `harness_call_llm`. This placement is what produced
   the workspace-local helper and the scratchpad-substituting default of §2.2.
   The failure is intrinsic to running zicato on a reasoning model, which is
   its most common target, so leaving it to each backend guarantees each
   backend re-hits it.
2. **A zicato-provided reasoning-aware wrapper that any backend opts into**
   (proposed). zicato ships the wrapper; a backend that targets a reasoning
   model wraps its raw callable once and registers the result as its
   `call_llm`. The wrapper is model-agnostic in the seam's spirit (it switches
   on channel *shape* rather than on a named vendor), so it stays
   vendor-clean. A non-reasoning backend does not wrap, and the seam is
   byte-for-byte unchanged for it.

This keeps the seam's signature and its collusion guarantee intact — the wrapper
preserves callable identity semantics, so `harness_` and `auxiliary_` wrapped
separately remain identity-distinct ([EMULATOR.md §3](EMULATOR.md#3-the-two-callable-rule)).

---

## 5. Implications for the proposer and judges

Every zicato-internal consumer that needs **structured / JSON output** is a
direct beneficiary, because the runaway hits hardest where the prompt is large
and the required output is structured:

- **The proposer** (`zicato/proposer/`) emits an `Experiment` as JSON. Its
  prompts are the largest zicato sends (mutation manifest, loss patterns, prior
  experiments, telemetry insights), so it is the most runaway-prone consumer and
  the one observed to fail live. With the wrapper, its parse-repair loop does
  only its intended job of fixing prompt-shaped mistakes, instead of re-drawing
  from a runaway distribution without effect. The `feedback_was_empty` path
  (§3) becomes rare rather than common.
- **The judges / rubric matchers** (`zicato/board/rubric.py`,
  `zicato/board/matchers.py`) also demand structured verdicts. They run on
  `effective_judge_call_llm` — the auxiliary surface or a dedicated
  `judge_call_llm` — and are equally exposed to a reasoning runaway swallowing
  the verdict. The same wrapper, applied to the judge callable, gives them a
  clean `content` to parse.
- **The default tool-using ADK proposer** ([PROPOSER.md §2](PROPOSER.md))
  reasons *while it calls tools* on ADK's own `Runner` rather than over the
  text shim. Its function-calling turns have their own thinking dynamics. The
  wrapper's budget and fallback argument applies wherever a reasoning model
  gates structured output behind a `<think>` block, but the integration point
  on that path is the configured inner model rather than the last-resort
  `call_llm` shim (`zicato/adapters/adk.py`). That path needs its own scoping
  note (§6).

The emulator and the free-text analysis pass carry lower risk, because their
output is prose, so an over-long reasoning trace costs latency rather than
correctness. They still benefit from §4.2, which never hands a caller a
scratchpad in place of an answer.

---

## 6. Non-goals and open questions

- **Not a vendor binding.** The wrapper switches on channel *shape*
  (reasoning/content), never on a model name. The seam stays model-agnostic
  (`zicato/core/types.py`). The served model is referred to generically.
- **Not a streaming redesign.** The contract stays `-> str`; the wrapper is
  free to consume a stream internally to detect `<think>` termination, but the
  public surface is unchanged.
- **Open: where the budget config lives.** Almost certainly the workspace
  `models` block alongside endpoint/model/api-key — to be settled with the
  implementation, kept off `CallLLM`'s signature.
- **Open: telemetry.** The seam should probably surface a runaway event (budget
  exhausted, fallback taken) into the meta-loop so operators can see how often a
  reasoning model is running away on their prompts — a tuning signal for prompt
  size and budget. The proposer already emits paired
  `proposer_call_started` / `proposer_call_completed` events
  (`zicato/proposer/proposer.py`); a `reasoning_runaway` / `fallback_taken`
  marker would slot alongside.
- **Open: ADK inner-model path.** The tool-using proposer of §5 runs on the
  configured inner model rather than the `call_llm` shim. Whether the wrapper
  applies there, or whether ADK's own model layer needs the equivalent budget
  and fallback, needs its own scoping pass.
