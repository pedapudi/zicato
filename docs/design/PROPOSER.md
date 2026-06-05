# The proposer — a first-class evaluation-contract input

> **Status.** SHIPPED. The proposer is folded into the contract hash
> (`zicato/epoch/contract.py`), the two-tier resolution
> (`zicato/proposer/{skills,agent,adk_agent,tools}.py`) is in the tree and
> exercised by the test suite, and the operator surface
> (`register --proposer-path`) is wired. §§1–2 describe the shipped design;
> §3 is the Design-A rationale; §§4–5 are the contract/config mechanics.
> Operator-facing how-to lives in the `zicato-design-proposer` skill.

The **proposer** is the agent that, each round, reads the epoch's brief, the
mutation manifest, the loss patterns, and the prior experiments, and emits the
next `Experiment` (`{hypothesis, patches}`) for the tournament to judge. For
most of zicato's life the proposer was an implicit, fixed component. It is now
a **first-class evaluation-contract input**, alongside the board, the proposer
brief, the scoring, and the inner-harness identity
([EPOCHS-AND-JOURNALING.md §10.1](EPOCHS-AND-JOURNALING.md#101-whats-in-the-contract)).

This document is the design + reference companion. The defining decision,
repeated because it is load-bearing: **the proposer is contract, not
configuration.** A different proposing agent — or a different skill in its
prompt — proposes different mutations and reasons differently, so generations
proposed under different proposers are not directly comparable. Changing the
proposer therefore **rolls the epoch**, exactly like changing the board or the
scoring (§4).

---

## 1. What a proposer is, on disk

A proposer is, on disk, a directory:

```
proposers/<name>/
  skills/
    grounding.md          # SKILL.md-style: optional frontmatter + markdown body
    house-style.md        # zero or more
  agent.py                # OPTIONAL — a custom ADK proposer agent (tier b)
```

- `skills/*.md` — markdown skill modules. Each is SKILL.md-style: an optional
  `---`-fenced frontmatter block (`name` + `description`) followed by a
  free-form markdown body. They are loaded sorted by filename and injected into
  the proposer's system prompt. Zero or more.
- `agent.py` — an OPTIONAL custom proposer agent. Its *presence* selects the
  tier-(b) custom-agent path (§2); its *contents* are part of the contract.

A workspace points at a proposer dir with `zicato register --proposer-path
PATH` (§5). When no proposer dir is configured the proposer is the **built-in
default agent** — no skills, no tools, no custom module
(`ProposerSpec.default()`, `agent_id = "builtin:default"`).

The proposer dir, like the board / brief / scoring, is the operator's *live,
editable* copy. Its resolved spec is folded into the epoch's contract hash, so
editing it between `evolve` invocations is detected as drift and rolls a fresh
epoch (§4).

---

## 2. The two tiers

There are exactly two ways to shape the proposer, and the *presence of
`agent.py`* selects between them. `build_proposer_agent`
(`zicato/proposer/agent.py`) makes the call: a custom `agent.py` (i.e.
`spec.agent_source_sha256` is set) ⇒ the custom-agent path; otherwise the
skill-composed default.

### (a) Skill-composed default — drop `skills/*.md`, no code

The cheapest customization. Drop one or more `skills/*.md` into
`proposers/<name>/skills/` and configure the dir; do **not** write an
`agent.py`. zicato runs the built-in `DefaultProposerAgent`, a single-shot text
exchange driven on `--auxiliary-call-llm`: the auxiliary callable is handed a
`(system, user, model) -> str` prompt and the returned string is parsed into
the `Experiment`. Your skill bodies are injected into the **system prompt**, so
they steer *how* the default proposer reasons (grounding instructions, house
style, a checklist) without any code.

This is the right tier when you only want to shape the proposer's reasoning,
not give it new capabilities. The model is the auxiliary model — you do not own
it here.

### (b) Custom ADK agent with tools — `agent.py`

When the proposer needs to *read the world* while it reasons — grep the
mutable surface, inspect the parent snapshot, recall what prior rounds tried —
the skill-only default is not enough, because the text shim is text-in /
text-out and cannot express the function-calls a tool-using agent needs (§3).

For that, ship a `proposers/<name>/agent.py` that exposes a module-level
`agent` — a **native ADK `LlmAgent`** with its **own `model=`** and a `tools=`
list drawn from zicato's read-only proposer tool registry. zicato loads that
agent and runs it on ADK's own `Runner`
(`ADKProposerAgent`, `zicato/proposer/adk_agent.py`).

The read-only tool registry (`zicato.proposer.tools.DEFAULT_PROPOSER_TOOLS`):

| Tool | Reads | Guard |
|---|---|---|
| `list_mutation_points` | The round's mutation manifest — the exact ids the agent may target. | Read-only; renders the bound context's manifest. |
| `read_mutable_file` | One file under the parent generation's mutable subtrees. | Read-only; rejects absolute paths and `..` traversal outside the mutable surface. |
| `grep_mutable` | Regex search across the mutable subtrees, `path:line: text`. | Read-only; match count capped to protect the context window. |
| `read_journal` | The epoch's running narrative journal. | Read-only; empty string when absent. |
| `read_insights` | The epoch's latest analyzer insights — same content the default proposer embeds. | Read-only; empty string when absent. |

Every tool is **read-only** by contract. A proposer tool that wrote to the
snapshot would corrupt the very tree the round is about to patch (and break the
applier's content-hash guard), so the whole surface only reads. The tools read
their per-round runtime context (which snapshot, which manifest, which epoch)
from a `contextvars.ContextVar` that `ADKProposerAgent.propose` binds around
each run, so concurrent challengers never leak context into one another.

A custom `agent.py` opts in with simply:

```python
from zicato.proposer.tools import DEFAULT_PROPOSER_TOOLS
agent = LlmAgent(name="my_proposer", model=..., instruction="...",
                 tools=list(DEFAULT_PROPOSER_TOOLS))
```

The copy-me example is
[`examples/zicato_examples/proposer_with_tools/agent.py`](../../examples/zicato_examples/proposer_with_tools/agent.py)
(`build_agent(model=...)` + a lazily-built module-level `agent`).

---

## 3. Design A — why a tool-using proposer owns its own model

The default proposer is a single-shot text exchange: zicato hands the auxiliary
callable a `(system, user, model) -> str` prompt and parses the returned
string. **That shim cannot express the function-calls a tool-using agent
needs** — it is text-in / text-out by contract. A proposer that wants to grep
the mutable surface or consult the journal *while it reasons* cannot run on it.

**Design A** resolves this by running a tool-using proposer as a **native ADK
agent that declares its own `model=`**, driven on ADK's own `Runner` — NOT
through the auxiliary text shim, and NOT through `goldfive.run`. The
consequences, all deliberate:

- **The agent author owns the model.** The agent's `model=` is its own; the
  `--auxiliary-call-llm` callable does not govern it. The per-round task (brief
  + skills + mutation manifest + patterns + loss + prior experiments + the
  JSON-schema demand) is delivered as the agent's run *input* — the custom
  agent owns its own static instruction (how to work), zicato owns the input
  (what this round is).
- **The proposer's model must differ from the harness model.** Because the
  proposer runs on its own model rather than the shared auxiliary callable, the
  `is`-identity collusion guard
  (`assert_distinct_callables`) does not apply here. The model-distinctness is
  instead a **documented author responsibility**: a proposer scored on the same
  model it is mutating-and-judging risks collusion. When both model strings are
  trivially discoverable zicato emits a soft WARNING on a match; it does not
  build a hard gate.
- **The post-response loop is shared.** The agent's final message goes through
  the same parse → forbidden-id enforcement → post-apply validation loop the
  default proposer uses; a retryable failure feeds its feedback into the next
  run's input, within the same bounded budget.

Every `google.adk` import on this path is lazy, so importing the proposer
modules never forces the optional `google-adk` extra on the skill-only default
path.

---

## 4. Contract / epoch-roll mechanics

The proposer is folded into the contract hash by `_canon_proposer`
(`zicato/epoch/contract.py`). It resolves the proposer dir (or `None` ⇒ the
builtin default) to a `ProposerSpec` via `resolve_proposer_spec`
(`zicato/proposer/skills.py`) and serializes it sorted-key:

- `agent_id` — `"builtin:default"` or `"dir:<name>"`;
- `tools` — the tool names, sorted;
- `skills` — `[{name, sha256-of-normalized-body}]`, sorted by name. Skill
  bodies are normalized exactly like the proposer brief (line endings folded,
  trailing whitespace stripped, leading/trailing blank lines dropped), so a
  whitespace-only skill edit does **not** roll the epoch; a semantic edit — or
  adding / removing / renaming a skill — does;
- `agent_source_sha256` — SHA-256 of a custom `agent.py` (or `null`), so
  editing the custom agent rolls the epoch.

The builtin default produces a stable canonical string, so a workspace that
never registers a proposer keeps a stable hash. The per-component roll message
(`compute_component_hashes`) names the changed component **`proposer`**, so when
a roll is triggered by a proposer edit the operator sees exactly that.

This composes with the brief: the **proposer brief** is per-epoch *operator
guidance* (steering text the proposer reads fresh each round), while the
**proposer** is the *agent + its skills* that consume it. They are distinct
contract inputs; either rolling the epoch is independent of the other.

---

## 5. Configuring it — `register --proposer-path`

`zicato register --proposer-path PATH` records `contract.proposer_path` in
`.zicato/config.json` (absolutised, like the other contract source paths).
`resolve_contract_inputs` reads it back on every `evolve`, resolves a relative
spelling against the project root (the workspace's parent), and feeds it into
the contract hash *before* the hash is computed — so registering a proposer dir
rolls the epoch on the next `evolve`, exactly like editing the brief. Omitting
the flag leaves the key unset, which resolves to the builtin default proposer
(`None`).

A `config.json` sketch (other keys elided):

```jsonc
{
  "adk_entrypoint": "my_pkg.agent:root_agent",
  "mutable_trees": ["src/my_pkg"],
  "contract": {
    "board_path":   "/abs/board.jsonl",
    "rubric_path":  "/abs/brief.md",       // the brief, under its on-disk key
    "scoring_path": "/abs/scoring.json",
    "proposer_path": "/abs/proposers/fancy" // OPTIONAL — absent ⇒ builtin default
  }
}
```

Derive the exact flag surface from `zicato register --help` (the design CLI
docs are known to drift); as of writing the flag is `--proposer-path PATH`.

---

## 6. Cross-references

| Topic | Document |
|---|---|
| The epoch as the frozen contract; auto-roll on contract change; the proposer in the contract-input list | [EPOCHS-AND-JOURNALING.md §10](EPOCHS-AND-JOURNALING.md#10-contract-hash-auto-epoching), `src/zicato/epoch/contract.py` |
| Operator-facing: when skills suffice vs a custom agent; how to add a skill; the epoch-roll discipline | `skills/zicato-design-proposer/SKILL.md` |
| The mental model for epochs / rounds / the contract | `skills/zicato-manage-epochs-and-rounds/SKILL.md` |
| The proposer brief (per-epoch operator steering) vs the proposer (the agent) | [EPOCHS-AND-JOURNALING.md §1](EPOCHS-AND-JOURNALING.md#1-epoch-concept), `skills/zicato-write-brief` |
| The hypothesis schema the proposer must emit | [EPOCHS-AND-JOURNALING.md §3](EPOCHS-AND-JOURNALING.md) |
| Selection / tournament the proposer feeds | [SELECTION.md](SELECTION.md), [TOURNAMENT-STRUCTURES.md](TOURNAMENT-STRUCTURES.md) |
| The copy-me tool-using proposer agent | [`examples/zicato_examples/proposer_with_tools/agent.py`](../../examples/zicato_examples/proposer_with_tools/agent.py) |
| `register` CLI reference | [CLI.md](CLI.md#zicato-register) |
