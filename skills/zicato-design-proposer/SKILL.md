---
name: zicato-design-proposer
description: Configure zicato's proposer — the agent that proposes each round's mutation. Two tiers: the skill-composed default (drop skills/*.md, no code, runs single-shot on --auxiliary-call-llm) and a custom ADK agent (proposers/<name>/agent.py with its OWN model= and the read-only tool registry). Use when you want to steer HOW the proposer reasons (add a skill), or give it tools to read the snapshot / journal while it reasons (custom agent). Explains when each tier suffices, the Design-A model rule (the proposer's model must differ from the harness model), the read-only tools, and that editing the proposer or a skill ROLLS THE EPOCH — same as editing the brief.
---

# Designing a zicato proposer

The **proposer** is the agent that, each round, reads the epoch's brief +
mutation manifest + loss patterns + prior experiments and emits the next
`Experiment` (`{hypothesis, patches}`) for the tournament to judge. It is a
**first-class evaluation-contract input** — alongside the board, brief,
scoring, and inner-harness identity. Configuring a proposer dir, or editing one
of its skills, **rolls the epoch** (see "Editing the proposer rolls the epoch"
below).

Sibling skills — the design companions: `zicato-design-boards` (the board the
proposer's challengers are scored on), `zicato-design-judges` (what the loss
measures), `zicato-write-brief` (the per-epoch steering text the proposer
reads); and the operational/loop skills: `zicato-evolve` (the loop that runs
it), `zicato-manage-epochs-and-rounds` (the contract/round model this lives
in), `zicato-design-experiment` (the hypothesis the proposer must articulate).
Spec: [PROPOSER.md](../../docs/design/PROPOSER.md),
[EPOCHS-AND-JOURNALING.md](../../docs/design/EPOCHS-AND-JOURNALING.md).

> **Proposer vs proposer brief — two different things.** The *brief*
> (`brief.md`) is per-epoch operator steering TEXT the proposer reads fresh
> each round. The *proposer* is the AGENT (plus its skills) that consumes it.
> Both are contract inputs; this skill is about the agent, `zicato-write-brief`
> is about the brief.

## The two tiers — which one, when

A proposer lives on disk as `proposers/<name>/`. The *presence of an
`agent.py`* selects the tier.

| You want to… | Tier | What you ship |
|---|---|---|
| Steer HOW the default proposer reasons (grounding rules, house style, a checklist) without code | **(a) skill-composed default** | One or more `skills/*.md`, NO `agent.py`. Runs single-shot on `--auxiliary-call-llm`; your skill bodies are injected into the system prompt. |
| Give the proposer the ability to READ the world while it reasons — grep the mutable surface, inspect the parent snapshot, recall prior rounds | **(b) custom ADK agent** | A `proposers/<name>/agent.py` exposing a module-level `agent` — a native ADK `LlmAgent` with its OWN `model=` and `tools=` from the read-only registry. |

**Default to tier (a).** Reach for tier (b) ONLY when the proposer genuinely
needs tools — a custom agent is more to own (its own model, its own
instruction, the optional `google-adk` extra). If all you need is to change the
*reasoning*, a skill is strictly cheaper.

When no proposer dir is configured at all, the proposer is the built-in default
agent — no skills, no tools.

## (a) Add a skill — drop a `SKILL.md`-format file

A skill is a markdown file under `proposers/<name>/skills/`. It is SKILL.md
format: an optional `---`-fenced frontmatter block (`name` + `description`)
followed by a free-form markdown body. The body is what gets injected into the
default proposer's system prompt.

```
proposers/fancy/
  skills/
    grounding.md
    house-style.md
```

```markdown
---
name: grounding
description: Make the proposer ground every patch in the observed loss.
---

Before proposing a patch, identify the SINGLE loss pattern it targets.
Name the mutation-point id you are changing and the predicted pass-rate
delta. Do not propose a patch you cannot tie to an observed regression.
```

Skills are loaded sorted by filename and concatenated, so order them with
filename prefixes if it matters. A whitespace-only skill edit is a no-op for
the contract; a semantic edit (or adding / removing / renaming a skill) rolls
the epoch.

## (b) Write a custom ADK agent — the Design-A model rule

A tool-using proposer is a **native ADK `LlmAgent` with its OWN `model=`**, run
on ADK's own `Runner` — NOT through the `--auxiliary-call-llm` text shim. The
reason is concrete: the auxiliary callable is `(system, user, model) -> str`,
text-in / text-out, and **cannot express the function-calls a tool-using agent
needs**. So a proposer that wants to call tools must own its model and run on
ADK. This is "Design A" — see [PROPOSER.md §3](../../docs/design/PROPOSER.md).

**THE MODEL RULE (load-bearing):** the proposer's `model=` **MUST differ from
the harness model.** The proposer runs on its own model, so the `is`-identity
collusion guard does not cover it — model-distinctness is YOUR responsibility.
A proposer scored on the same model it is mutating-and-judging risks collusion.
zicato emits a soft WARNING on a discoverable match but does not hard-gate it;
do not rely on the warning — set a distinct model.

The read-only tool registry (`zicato.proposer.tools.DEFAULT_PROPOSER_TOOLS`):

| Tool | Reads |
|---|---|
| `list_mutation_points` | The round's mutation manifest — the exact ids the agent may target. |
| `read_mutable_file` | One file under the parent generation's mutable subtrees (absolute / `..`-escaping paths rejected). |
| `grep_mutable` | Regex search across the mutable subtrees (`path:line: text`, match-capped). |
| `read_journal` | The epoch's running narrative journal. |
| `read_insights` | The epoch's latest analyzer insights. |

Every tool is **read-only** — a proposer that wrote to the snapshot would
corrupt the tree the round is about to patch. A custom agent opts in with:

```python
from zicato.proposer.tools import DEFAULT_PROPOSER_TOOLS
agent = LlmAgent(name="my_proposer", model=...,  # MUST differ from harness model
                 instruction="...", tools=list(DEFAULT_PROPOSER_TOOLS))
```

**Copy the example.** Start from
[`examples/zicato_examples/proposer_with_tools/agent.py`](../../examples/zicato_examples/proposer_with_tools/agent.py)
— it ships `build_agent(model=...)` + a module-level `agent`, opts into a
subset of the tools, and documents the model rule inline. Drop it into
`proposers/<name>/agent.py`, set `model=` to your proposer model, and trim the
tool list to what the agent actually uses. The agent's instruction should tell
it HOW to work (use the tools, then emit the `{hypothesis, patches}` JSON); the
per-round WHAT (brief, skills, manifest, loss, prior experiments, the schema)
is delivered by zicato as the run input.

## Register it

Point the workspace at the proposer dir with `register` (off the happy path —
`evolve` resolves the contract itself, but `register` pins the path):

```sh
.venv/bin/zicato register \
    --adk my_pkg.agent:root_agent \
    --mutable-tree src/my_pkg \
    --proposer-path proposers/fancy
```

This writes `contract.proposer_path` into `.zicato/config.json` (absolutised),
which `evolve` reads back on every run. Derive the exact flag from
`zicato register --help` — the design CLI docs are known to drift. An absent
`--proposer-path` leaves the workspace on the built-in default proposer.

## Editing the proposer rolls the epoch (same as editing the brief)

The proposer is folded into the contract hash (`agent_id`, sorted tool names,
per-skill normalized-body hashes, the custom `agent.py` source hash). So:

- registering a proposer dir, or
- semantically editing its `agent.py` / declared identity / tools, or
- adding / removing / renaming / semantically editing a `skills/*.md`

each **opens a new epoch** on the next `evolve` — the roll message names the
changed component `proposer`. A whitespace-only skill edit is a no-op. This is
by design: a generation proposed by one agent (with one set of skills) and a
generation proposed by a different agent are **not directly comparable**, so
they must not share an epoch's lineage — exactly as editing the brief's
`## Forbidden` set rolls the epoch. See
[EPOCHS-AND-JOURNALING.md §10](../../docs/design/EPOCHS-AND-JOURNALING.md#10-contract-hash-auto-epoching)
and `zicato-manage-epochs-and-rounds`.

## A good proposer design

- **Start with the built-in default; add a skill before you add an agent.**
  Most steering is reasoning, not capability — a `skills/*.md` is the cheaper,
  contract-clean lever.
- **Only go custom-agent when the proposer needs to READ the world** (grep,
  snapshot, journal) while it reasons. Tools are the whole reason tier (b)
  exists.
- **Set the proposer model distinct from the harness model.** It is your
  responsibility, not a hard gate.
- **Treat a skill edit like a brief edit** — it rolls the epoch, so batch
  proposer changes with your other contract edits.
- **Never start a live `zicato evolve` to test a proposer without the
  operator's explicit go-ahead.** Verify the spec resolves + the agent imports
  via the test suite (e.g. the proposer-with-tools example), not a live run.
