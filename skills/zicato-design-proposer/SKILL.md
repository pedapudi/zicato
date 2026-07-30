---
name: zicato-design-proposer
description: Configure zicato's proposer — the agent that proposes each round's mutation. The DEFAULT (no proposer dir) is a tool-using ADK agent that owns the read-only tool registry and runs on ADK's own Runner. Two opt-in customizations: a skill-composed text-shim proposer (drop skills/*.md, no code, runs single-shot on --auxiliary-call-llm) and a custom ADK agent (proposers/<name>/agent.py with its OWN model= and the read-only tool registry). Use when you want to steer HOW the proposer reasons (add a skill), or own the model / curate the tools (custom agent). Explains when each suffices, the Design-A model rule (a custom proposer's model should differ from the harness model), the read-only tools, and that editing the proposer or a skill ROLLS THE EPOCH — same as editing the brief.
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

## The default + two opt-in customizations — which one, when

The DEFAULT — **no proposer dir configured at all** — is a built-in
**tool-using ADK agent**: it owns the full read-only tool registry and runs on
ADK's own `Runner`, bound per round to the workspace's auxiliary model. So out
of the box the proposer already greps the mutable surface, reads the parent
snapshot, and consults the journal / analyzer insights while it reasons. You
need configure NOTHING to get a capable proposer.

To CUSTOMIZE, a proposer lives on disk as `proposers/<name>/`. The *presence of
an `agent.py`* selects which customization you get:

| You want to… | Customization | What you ship |
|---|---|---|
| Steer HOW the proposer reasons (grounding rules, house style, a checklist) over the text shim, without code | **(a) skill-composed (text shim)** | One or more `skills/*.md`, NO `agent.py`. Runs single-shot on `--auxiliary-call-llm`; your skill bodies are injected into the system prompt. (This is the text-shim engine — it does NOT have tools.) |
| OWN the model the proposer runs on, curate its tool subset, or write a bespoke instruction | **(b) custom ADK agent** | A `proposers/<name>/agent.py` exposing a module-level `agent` — a native ADK `LlmAgent` with its OWN `model=` and `tools=` from the read-only registry. |

**Prefer the default.** The default already has tools, so reach for a
customization only when you specifically want to change the *reasoning* (a
skill — but note it drops to the text shim, which has no tools) or *own the
model* (a custom agent). If you want tools AND custom reasoning, write a custom
`agent.py`.

## (a) Add a skill — drop a `SKILL.md`-format file

A skill is a markdown file under `proposers/<name>/skills/`. It is SKILL.md
format: an optional `---`-fenced frontmatter block (`name` + `description`)
followed by a free-form markdown body. The body is what gets injected into the
skill-composed (text-shim) proposer's system prompt. (Adding a skill, with no
`agent.py`, switches the proposer from the tool-using default to the text-shim
engine — the skill bodies steer it, but it has no tools.)

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
| `mutation_track_record` | One mutation point's banded per-epoch fertility record — touches, promotions, a BUCKETED Δscalar summary, a coarse recency flag. Aggregates only. |
| `read_parent_diff` | What the LAST PROMOTION changed, so the agent can build on (or depart from) what just worked. |
| `mutation_usage` | Where a mutation point's symbol / current value is referenced across the parent snapshot's mutable subtrees. |

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

## The `proposer_quality` knobs (how it proposes, independent of the tier)

Whichever tier you pick, `scoring.json`'s `proposer_quality` block
(`ProposerQualityConfig`) shapes the propose-step itself. It is part of the
frozen contract, so every knob here **rolls the epoch**.

| Knob | Default | What it does |
|---|---|---|
| `best_of_n` | `3` | Sample N candidate experiments per propose-step. Pin `1` for the historical single-sample proposer (scripted/mock proposers do). |
| `critique_enabled` | `true` | The self-critique pass that selects the best of the slate. Sees ONLY the same restricted prompt context the proposer sees. |
| `recombine` | `false` | Offer a recombination slot in the slate — union two prior patches instead of proposing fresh. |
| `recombine_merge` | `"mechanical"` | `mechanical` unions the patches directly; `llm` spends one auxiliary merge call (the depth role). |
| `genealogy` | `0` | Rounds of candidate-genealogy context handed to the proposer. Carries lineage only, never board data. |
| `calibration_feedback` | `0` | Rounds of per-claim-type hit/miss/unresolved COUNTS — did its own predictions land — fed back into the prompt. |
| `process_exemplars` | `0` | Number of process exemplars included. |
| `screen_entries` / `screen_veto_only` | `0` / `false` | Pre-tournament candidate screening (tryouts); `zicato-tune-scoring` owns the values. |

The slate also splits across two **ensemble roles**: `breadth` steers the slate
sampling, `depth` the critique + revise calls. Both default to the auxiliary
role, and can be pointed at separate models via a `models.proposer_breadth` /
`models.proposer_depth` block. The collusion identity-guard deliberately does
NOT apply between them — they are two halves of one proposer.

## The failure-mode feedback channel (what every proposer reads)

Independent of which tier you pick, the proposer's per-round input carries a
compact **failure-mode profile** — a bucketed, board-anonymized,
**train-slice-only** outcome-marginal block rendered into the prompt
(`render_failure_mode_profile`, `zicato.proposer.prompts`). It tells the
proposer *why* answers are wrong (over-retrieval vs misses vs empty answers),
not just *that* a scalar moved: a recall/precision decomposition plus banded
marginal rates. Two invariants make it leakage-safe and contract-clean:

- **Identity-free + holdout-free.** It is aggregated over the SAME train slice
  the round duels on — the holdout is excluded — and carries only marginal
  rates (no entry id, question, or output token), so it is board-anonymous by
  construction and cannot leak the held-out slice (OVERFITTING.md §11.4).
- **Banded, so it cannot memorize.** Every number is coarsened to a band, so no
  exact per-run value and no round-over-round response surface leaks. An empty
  train slice (no runs) renders the EMPTY STRING — the proposer prompt stays
  byte-identical to today when there is no outcome data.

An operator can extend the marginals via the **`outcome_summarizer_spec`**
hook on `ScoringWeights` (a dotted callable spec): it contributes extra
marginals over the same train slice, each still bucketed and identity-free
before it reaches the prompt (`orchestrator.py` `render_failure_mode_profile`
wiring). It is a *scoring* field, so setting it rolls the epoch like any other
contract change. This channel is on by default and needs no proposer-dir
configuration; you only touch `outcome_summarizer_spec` to add custom marginals.

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

- **Start with the built-in default — it already has tools.** The default
  tool-using ADK agent greps the surface, reads the snapshot, and consults the
  journal out of the box. Configure nothing unless you have a specific reason.
- **A skill drops you to the text shim (no tools).** Add a `skills/*.md` only
  when you want to steer the *reasoning* over the text-shim engine and don't
  need tools; it is the cheaper, contract-clean lever for pure reasoning
  changes, but it trades away the default's tools.
- **Go custom-agent to OWN the model, curate the tool subset, or write a
  bespoke instruction** while keeping tools. That is the whole reason the
  custom-agent path exists.
- **Set a custom proposer's model distinct from the harness model.** It is your
  responsibility, not a hard gate. (The built-in default reuses the auxiliary
  model on purpose — that smell test is skipped for it.)
- **Treat a skill edit like a brief edit** — it rolls the epoch, so batch
  proposer changes with your other contract edits.
- **Never start a live `zicato evolve` to test a proposer without the
  operator's explicit go-ahead.** Verify the spec resolves + the agent imports
  via the test suite (e.g. the proposer-with-tools example), not a live run.
