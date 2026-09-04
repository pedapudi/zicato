# Dogfood targets

zicato optimizes systems it is pointed at, and this document specifies
three of them, in increasing order of risk: a presentation agent, the
steering layer of the sibling project goldfive, and zicato applied to
itself. The presentation agent and the steering layer both run on the
shipped stack, each with an example directory below. Applying zicato to
itself is specified without being built, because it forces commitments
the shipped surface has to have made before that target arrives, and
making such a commitment upfront is cheaper than reshaping the surface
later.

| Target | Example directory | What still has to land | What it forces |
|---|---|---|---|
| A presentation agent — a multi-agent tree from harmonograf's reference set | `examples/zicato_examples/target_1_presentation/` | nothing; it runs on the shipped stack | nothing extra; it exercises the whole stack end to end |
| goldfive's own steering layer | `examples/zicato_examples/target_2_goldfive_steering/` | nothing; the synthetic board-entry runner ships in `zicato/synthetic/` | mutation across two source trees; a loss signal that is not drift |
| zicato applied to itself | none; not built | nested zicato instances, and a curated benchmark of labeled proposer inputs and ideal outputs (§3.4) | recursion guards across nested instances |

The examples tree carries two further directories that this document does
not specify. `examples/zicato_examples/target_0_convergence/` is the
deterministic convergence recipe: no model runs anywhere, the harness and
the proposer are scripts, and the champion scalar walks to a known floor,
which makes it the sanctioned end-to-end vehicle when no live run is
authorized. `examples/zicato_examples/target_4_agent_config/` points
zicato at an external coding agent's configuration package — the markdown
files that agent loads at startup — so that promoting a generation
promotes a configuration. Those directory numbers do not index this
document's ordering: there is no `target_3` directory, and neither the
convergence recipe nor the coding-agent configuration appears among the
three targets below.

Risk rises sharply from one target to the next. Running the earlier ones
establishes that the loop works before it is trusted on itself.

## 1. Target 1 — Presentation agent

### 1.1 What it is

A multi-agent system in harmonograf's reference set:
`harmonograf/tests/reference_agents/presentation_agent_orchestrated/`.
A coordinator routes to specialists (researcher, outliner, writer,
reviser) to produce a presentation outline from a user's brief. The
agent is real, exercised by the reference suite, and produces
realistic drift signals.

### 1.2 Why the presentation agent comes first

- It is a real agent with real failure modes: the coordinator sometimes
  routes the writer before the researcher, the researcher occasionally
  confabulates, and the reviser sometimes loops on
  semantically-identical revisions.
- The drift signal that goldfive emits is already the loss zicato needs,
  so no separate loss model has to be written.
- The mutation surface sits in the agent's own tree, so a run mutates
  one repository.
- The harmonograf maintainers have run this agent enough to have a clear
  sense of when a generation is better, which is what calibrating
  zicato's scoring weights requires.

### 1.3 The mutation surface

The example vendored into this repository
(`examples/zicato_examples/target_1_presentation/`) is zicato's own copy
of the agent, and its specialist set differs from the upstream reference
agent described in §1.1: it routes to a researcher, a web developer, a
reviewer and a debugger. It annotates fifteen mutation points, in five
groups:

- **Specialist `instruction=` strings**, one marker per specialist:
  `researcher_instruction`, `web_developer_instruction`,
  `reviewer_instruction`, `debugger_instruction`.
- **Coordinator routing** — the system prompt describing when to
  delegate to which specialist, plus the branch taken when the
  requested files are absent: `coordinator_instruction`,
  `coordinator_files_not_found_routing`.
- **Tool descriptions** — the `description=` field on each leaf tool:
  `write_webpage_tool_description`,
  `read_presentation_files_tool_description`,
  `find_presentation_files_tool_description`,
  `patch_file_tool_description`.
- **Path logic**, marked with the code-region form rather than the
  string-span form: `topic_slugify_logic`, `topic_output_dir_logic`,
  `find_presentation_match_logic`.
- **The topic-naming convention** the specialists agree on, as two
  string spans: `web_developer_topic_naming`, `reviewer_read_path`.

Fifteen points give the proposer enough surface to find leverage, and
are few enough that a score movement can be attributed to a single
point. Mutation ids are plain strings; the example uses the underscore
convention, and any spelling the marker regex accepts works.

### 1.4 Drift kinds expected to move

The presentation agent's natural failure modes line up with goldfive's
drift taxonomy:

| Drift kind | Where it fires | Hypothesis-shaped fix |
|---|---|---|
| `DRIFT_KIND_CONFABULATION_RISK` | Researcher produces output without calling a search tool. | Tighten `researcher.instruction` to require source-checking. |
| `DRIFT_KIND_CAPABILITY_MISMATCH` | Coordinator routes the writer before the researcher has reported. | Rewrite `coordinator.routing` to encode the pipeline order. |
| `DRIFT_KIND_LOOPING_REASONING` | Reviser's chain-of-thought repeats across turns. | Add an exit condition to `reviser.instruction`. |
| `DRIFT_KIND_LOOPING_TOOL_CALL` | Reviser calls the same edit tool with the same args. | Add an "if no change is needed, stop" clause. |

These are working hypotheses for the first epoch. The actual round-
by-round proposals are the proposer's output; the table above is what
the operator expects to see.

### 1.5 What the presentation agent exercises

Running it exercises the whole meta-loop, end to end:

- ADK adapter registration works on a real coordinator + specialist
  tree.
- Span markers resolve correctly across an ADK agent definition.
- The goldfive event stream captures the drift kinds the presentation
  agent produces.
- The reducer maps drift events to a meaningful `drift_loss`.
- The proposer produces patches the applier can apply cleanly.
- The validator's pre-apply and post-apply checks
  ([MUTATION-SURFACE.md](MUTATION-SURFACE.md) §6) catch typical
  malformed patches.
- The tournament promotes and rejects candidates correctly.
- The journal renders a readable trace across multiple rounds.

A break anywhere along that path shows up when the presentation agent
runs. The operator's judgment that a given generation really is better is
what calibrates the scoring weights against.

### 1.6 What the presentation agent leaves untested

- Mutation across two source trees. Its mutation surface lives entirely
  in one tree.
- A loss signal other than drift. Here the drift signal is the loss.
- Nested zicato instances. The loop runs once, against one harness.
- The `synthetic_*` board kinds. Its board is single-turn and
  multi-turn-emulated, all real user-shaped prompts.

The steering target covers two-tree mutation, the non-drift loss, and the
synthetic board kinds. Zicato applied to itself covers nesting.

## 2. Target 2 — goldfive's steering layer

### 2.1 What it is

The system under test is **goldfive itself**, and within it the steering
layer: the `Steerer` protocol's default implementation, the LLM-judge
prompts, the intervention ladder thresholds, and the plan-revision
strategy. The agent under test is some other agent — the presentation
agent above serves — **wrapped by goldfive**. The worked example is
`examples/zicato_examples/target_2_goldfive_steering/`.

The setup looks like:

```
zicato
   │
   └─ system under test = (presentation agent) wrapped by (goldfive,
                                                       under
                                                       optimization)
```

zicato proposer proposes patches to **goldfive's** prompts and thresholds.
The presentation agent is the workload that exercises goldfive's
steering.

### 2.2 Why steering quality resists direct measurement

Drift detection is the thing being optimized, so drift counts cannot
serve as the loss:

- A steerer that **never fires** has zero drift counts and is bad,
  because it missed real drift.
- A steerer that **fires constantly** has high drift counts and may be
  catching real drift that a quieter steerer would have missed.

Drift count measures the **agent** as seen through the steerer, rather
than measuring the **steerer**. Measuring the steerer requires ground
truth about when drift should have fired.

### 2.3 The mutation surface

The markers are annotated in goldfive's own source rather than in
zicato. The shipped design admits that:
`HarnessAdapter.mutation_points()` returns a list over a list of source
roots, even though a single-tree target registers one root. The steering
target registers two:

- `path/to/presentation_agent_package/` (the workload — unchanged).
- `path/to/goldfive/goldfive/` (the steerer — the actual mutation
  target).

Mutation points in goldfive include:

| Mutation point id | What it controls |
|---|---|
| `goldfive.steerer.refine_prompt` | The system prompt for the LLM-driven `LLMPlanner.refine`. |
| `goldfive.judge.goal_drift_prompt` | The judge prompt for the goal-drift classifier. |
| `goldfive.judge.reasoning_prompt` | The judge prompt for the three-state reasoning judge. |
| `goldfive.steerer.intervention_thresholds` | The (drift_kind, severity, occurrence_count) → ladder level mapping. |
| `goldfive.steerer.reflective_check_prompt` | The reflective self-progress check prompt. |

These are checked into goldfive's source as `# zicato:mutable`
markers. The dependency runs one way: goldfive does not depend on
zicato, and the markers are inert Python comments to goldfive's runtime.

### 2.4 The CLI surface for cross-repo registration

`zicato epoch register` accepts repeated `--mutable-tree` flags:

```
zicato epoch register --adk presentation_agent_package.agent:root_agent \
    --mutable-tree path/to/presentation_agent_package \
    --mutable-tree path/to/goldfive/goldfive
```

`--adk` is a DOTTED MODULE PATH, never a filesystem path. Each registered
root's BASENAME must be the importable package name — above, that is
`presentation_agent_package` and `goldfive`. The snapshot copies each root
under its basename, and the loader prepends only the snapshot root to
`sys.path`, which resolves top-level names alone. A root whose basename
Python cannot name as a module could therefore never be shown to have run
from the snapshot, so `register` refuses it (issue #110).

The entrypoint may live inside one of those roots, as above, or outside all
of them. The steering target uses the second shape: mutate `goldfive` and
drive it from a harness module that imports it. Either way every registered
root is verified per run, rather than the entrypoint's root alone. `load`
asserts that each root's top-level name resolves inside the generation
snapshot, and after each unit the worker records which roots were imported
in `generations/{gen}/harness_load.json`. In the two-root example above, a
round that mutates `goldfive` but whose units never import it raises the
`tree_never_imported` loop-health WARNING instead of scoring a no-op.

The first registered root is conventionally the package containing the
agent factory; additional roots are added with repeated
`--mutable-tree` flags. The adapter's `mutation_points()` walks every
registered root and concatenates the results.

### 2.5 The loss model that replaces drift

This is the architectural change the steering target forces. Drift cannot
be the loss, so something else must be. The steering loss has four terms,
described in the four subsections below.

#### 2.5.1 Outcome predicates on real entries

On an ordinary board entry, single-turn or multi-turn-emulated, the
agent's task is judged with an ordinary expectation. A pass means the
steerer did not make things worse; a fail means the steerer either
interfered badly or missed a real failure. This is the same pass-rate
signal the presentation agent produces, applied here to score the
steerer rather than the agent.

#### 2.5.2 Adversarial recall via `synthetic_adversarial`

The `synthetic_adversarial` board-entry kind wires a **known-bad agent**
in place of the real workload — an agent built to loop, hallucinate, or
refuse. The expectation is that drift fires, of the right kind.

The `synthetic_adversarial` kind and its discriminant fields already ship
in the `BoardEntry` type; the runner that executes them lands with the
steering target. The entry shape is:

```json
{
  "id": "looping_research",
  "kind": "synthetic_adversarial",
  "input": "Research the history of the printing press.",
  "adversarial_agent_spec": "myproj.synthetic.LoopingResearcher",
  "required_drift_kinds": ["looping_reasoning", "looping_tool_call"],
  "wall_clock_budget_seconds": 240,
  "tags": ["adversarial", "looping"]
}
```

`BoardEntry.validate` requires a `synthetic_adversarial` entry to
carry `input`, a non-empty `adversarial_agent_spec`, and a non-empty
`required_drift_kinds`, each kind validated against the registered drift
kind set. The entry passes when every kind in `required_drift_kinds`
fires, and fails when the steerer missed one.

The synthetic agent's source lives in the operator's project rather than
in zicato. `adversarial_agent_spec` is a dotted path the adapter resolves
at run time. A small library of known-bad agents — looping,
confabulating, refusing, off-topic — gives the operator a starting set.

#### 2.5.3 Specificity via `synthetic_clean`

The `synthetic_clean` board-entry kind wires a **known-good agent** that
does its job without misbehaving. The entry passes when the drift count
is zero or below a tolerance, and fails when drift fired with nothing to
report.

The `synthetic_clean` kind also ships in the `BoardEntry` type, where
`validate` requires only `input`. The clean-run thresholds live in the
entry's `context` map and in the steering loss model rather than as
first-class discriminant fields:

```json
{
  "id": "clean_summarisation",
  "kind": "synthetic_clean",
  "input": "Summarise the attached three-paragraph brief.",
  "wall_clock_budget_seconds": 180,
  "context": {"max_drift_events": "0", "tolerated_drift_kinds": "new_work_discovered"},
  "tags": ["clean", "specificity"]
}
```

The adversarial set measures **recall** — the fraction of real drift the
steerer catches. The clean set measures **specificity** — how rarely the
steerer reports drift on a clean run. Together they hold the steerer's
sensitivity in balance.

#### 2.5.4 Cost from the goldfive event stream

Goldfive's `GoldfiveLLMCallStart` / `GoldfiveLLMCallEnd` events carry
per-call latency. A steerer that fires more judge calls per turn
costs more. Loss term:

```
cost_units[run] = count of goldfive-lane LLM call ends in the run
```

The steering loss combines the four terms into one scalar:

```
loss[entry] = (1 - pass_fail) * outcome_weight
            + adversarial_miss_count * adversarial_weight
            + spurious_drift_count * specificity_weight
            + cost_units * cost_weight
```

The operator tunes the weights. The shipped scoring infrastructure admits
this loss shape, because the `LossProfile` type is open-ended on new
fields and the per-kind weights live in `scoring.json`. Adding
`outcome_weight`, `adversarial_weight`, `specificity_weight`, and
`cost_weight` therefore leaves the schema intact.

### 2.6 The degenerate optimum drift would reward

Suppose the proposer patched goldfive's refine prompt so that goldfive
never fired drift again. The drift count would go to zero and the patch
would win on the drift signal, while the patched goldfive would be
useless. Drift counts measure the agent as goldfive sees it; they cannot
measure goldfive itself.

The four-term loss — outcome predicate, adversarial recall, specificity,
and cost — measures goldfive on its own job: catching real drift,
ignoring drift that is not there, at a reasonable number of judge calls.

### 2.7 Commitments the shipped design makes for the steering target

Five commitments let the steering target run without a schema break.
All five hold in the shipped design:

1. **Two `call_llm` callables.** Pinned in
   [EMULATOR.md](EMULATOR.md) to keep the emulator from colluding with
   the agent it emulates. The steering target relies on the same
   separation: the goldfive under optimization must not share a model
   endpoint with zicato's evaluation work.
2. **`mutation_points()` over a list of source roots.** Pinned in
   [MUTATION-SURFACE.md](MUTATION-SURFACE.md) §5. A single-tree target
   registers one root; the steering target registers two.
3. **`BoardEntry.kind` carries the synthetic slots.**
   Pinned in [BOARD-FORMAT.md](BOARD-FORMAT.md) §6. `synthetic_adversarial`
   and `synthetic_clean` are members of the `BoardEntryKind` literal,
   with their discriminant fields and `validate` rules in `BoardEntry`.
   Their runner is `zicato/synthetic/` (`run_adversarial_entry` and
   `run_clean_entry`), dispatched by `_tournament_worker.py` ahead of the
   adapter session, so the steering target needed no schema change.
4. **`LossProfile` is open-ended on new fields.** Pinned in
   [TELEMETRY.md](TELEMETRY.md). The cost, adversarial, and specificity
   fields plug in.
5. **Scoring weights are configurable per project.** Pinned
   in [SCORING.md](SCORING.md). Weights for the new loss terms drop
   into `scoring.json`.

Without those five commitments, the steering target would have forced a
schema-breaking change. Each was cheap to make upfront.

## 3. Target 3 — zicato itself

### 3.1 What it is

The system under test is **zicato**, and within it the prompts and heuristics
zicato itself runs on: the proposer's system prompt, the analysis-pass
prompt, the emulator persona template, and the rubric template. The
system under optimization is the system doing the optimizing. No example
directory exists for this target.

The setup:

```
outer zicato
   │
   └─ system under test = inner zicato
                          │
                          └─ system under test = presentation agent (target 1)
```

The outer zicato optimizes the inner zicato; the inner zicato
optimizes the presentation agent.

### 3.2 What optimizing zicato with zicato would show

zicato's claim is that an agent can improve itself against the loss of
its own outputs. A meta-harness has prompts of its own, so the same claim
applies to it, and running zicato against zicato is what would show that
the mechanism which optimizes other agents also optimizes the optimizer.

### 3.3 Why improvement velocity cannot serve as the loss

The obvious loss is how fast the inner zicato improves the presentation
agent over successive epochs. Three properties make that loss
impractical:

- Each round of the outer zicato is an entire **epoch** of the inner
  zicato: several rounds of generation plus a tournament against the
  presentation agent's board.
- That costs hours of wall clock per round at minimum, and running
  enough rounds for a meaningful tournament multiplies the cost.
- The signal is noisy at that granularity. The inner zicato's
  improvement velocity depends on the presentation agent's board
  composition, which can shift between rounds and mask the effect of the
  outer zicato's patches.

The loop is well defined and operationally infeasible.

### 3.4 The labeled-pair offline benchmark

The workable loss instead comes from a **labeled benchmark** of
`(LossProfile, ideal Experiment)` pairs. The outer zicato is scored on
how closely its proposer reproduces the labeled ideal `Experiment` given
the labeled `LossProfile`.

The benchmark runs offline:

1. The operator (or a trusted prior zicato run) curates pairs:
   "given this loss-profile aggregate, the right hypothesis was X,
   the right patches were Y, and they would have moved the score
   by Z".
2. The outer zicato's proposer is run against each labeled
   `LossProfile`. The hypothesis it generates is scored against
   the labeled ideal:
   - `core_idea` similarity (via evaluation LLM judge).
   - `modulating` overlap (exact mutation-point id match).
   - `expected_drift_movements` direction match.
   - `risks` quality, judged on whether the listed risks are plausible.
3. The aggregate similarity score is the loss for the round.

The benchmark is fast, because it runs proposer evaluations and no real
agent runs, and its signal is dense, because every pair contributes. The
trade-off is that the benchmark is only as good as its labels, and
curating them is real work.

### 3.5 The recursion / instance_id need

Nested zicato instances must NOT cross-talk:

- Outer zicato's workspace lives at `.zicato/instances/outer/`.
- Inner zicato's workspace lives at `.zicato/instances/inner/`.
- Outer zicato's runtime sees its own epochs / generations / patterns;
  inner zicato sees only its own.

The shipped runtime config carries an `instance_id` that keys the
workspace apart:

```python
@dataclass
class RuntimeConfig:
    instance_id: str = "default"
    workspace_root: Path = Path(".zicato")
    # ... other fields ...

    @property
    def instance_workspace(self) -> Path:
        return self.workspace_root / "instances" / self.instance_id
```

When `instance_id == "default"`, which is what every target running today
uses, the workspace path reduces to `.zicato/`. When `instance_id` is
set, the workspace is keyed under it.

Recursion guards in the runner are easy to forget unless they are
provided for in advance. The shipped design provides for three:

- The outer zicato's `target_call_llm` is the inner zicato's
  `evaluation_call_llm` plumbing. Strictly: when the outer zicato's
  `HarnessAdapter` invokes the inner zicato, the inner zicato gets
  its own two `call_llm` callables, distinct from the outer's, with
  the same hard two-callable check.
- The outer zicato's `events.jsonl` path includes the
  `instance_id`; the inner zicato's writes never collide with the
  outer's.
- The control channel (if any) is keyed by `instance_id`; an outer
  zicato's pause signal cannot reach an inner zicato by accident.

### 3.6 The mutation surface inside zicato

Annotated in zicato's own code:

- `zicato.proposer.system_prompt` — the proposer's system prompt.
- `zicato.analysis.system_prompt` — the analysis-pass system prompt.
- `zicato.emulator.default_prompt_template` — the default emulator
  prompt template (the one with the answer-leak refusal section).
- `zicato.rubric.template` — the default rubric template (the
  `zicato init` skeleton).

The default emulator prompt template is **mutable**, but every patch to
it must preserve the answer-leak refusal section. The post-apply check
that declared required placeholders survive enforces this — check code
`A3` in [MUTATION-SURFACE.md](MUTATION-SURFACE.md) §6, driven by the
point's `required_placeholders` metadata. Removing the refusal section
would break the emulator's collusion-proof construction.

Those validator rules still hold when zicato is the target, because the
validator is part of zicato's own code: the outer zicato can patch
zicato's prompts and cannot patch the validator that protects them.

### 3.7 Sequencing

Run the presentation agent first. Hold the steering target until the
presentation agent has shown the loop converges. Hold zicato-on-itself
until the steering target has produced at least one full epoch with real
evidence that the loop improves something.

Risk rises sharply from one target to the next, so running the earlier
ones establishes that the loop works before it is trusted on itself.

## 4. Commitments the shipped design already makes

The following are pinned today because the steering target and
zicato-on-itself are known to be coming:

| Commitment | Pinned in |
|---|---|
| Two distinct `call_llm` callables, hard-validated at config time. | [EMULATOR.md](EMULATOR.md) §3 |
| `HarnessAdapter.mutation_points()` walks a list of source roots rather than a single tree. | [MUTATION-SURFACE.md](MUTATION-SURFACE.md) §5 |
| `BoardEntry.kind` is string-typed against a registered set. | [BOARD-FORMAT.md](BOARD-FORMAT.md) §6 |
| `LossProfile` is open-ended on new fields; weights live in per-epoch `scoring.json`. | [TELEMETRY.md](TELEMETRY.md) §3, [SCORING.md](SCORING.md) §2 |
| Runtime config carries `instance_id`; workspace is keyed by it. | this document §3.5 |

None of the five adds real cost today, and each prevents a schema break
later. The rule they follow is that the shipped surface should already
admit the steering target and zicato-on-itself when either one arrives.

## 5. Cross-references

| Topic | Document |
|---|---|
| Mutation surface annotation rules, AST resolution | [MUTATION-SURFACE.md](MUTATION-SURFACE.md) |
| Board entry kinds, open-ended discriminator | [BOARD-FORMAT.md](BOARD-FORMAT.md) |
| LossProfile fields and the reducer | [TELEMETRY.md](TELEMETRY.md) |
| Scoring weights and the non-drift loss extension | [SCORING.md](SCORING.md) |
| Emulator collusion-proofing, which the steering target and zicato-on-itself both depend on | [EMULATOR.md](EMULATOR.md) |
| Why the hypothesis schema is mandatory, which is what makes the offline benchmark possible | [RATIONALE.md](RATIONALE.md) |
| The post-promotion hook contract (`on_promote`) | [ARCHITECTURE.md](ARCHITECTURE.md) §4.1.1 |

## 6. Targets whose state lives outside the tree

All three targets above share a property that does not hold in general:
their evolved state is the mutable tree. Promote a generation, and the
promoted snapshot plus the `current_generation` marker is the entire
result. There is nothing else to update, which is why none of the three
needed a promotion hook.

A target can be evolvable through a source tree while its *operative*
state lives somewhere the tree cannot reach. That state may be a prompt or
policy row in a database, a config served to a fleet, a compiled artifact
in an object store, or a cache the running system reads. For such a target,
the champion
advancing is not the end of the round; it is the trigger for a write the
loop knows nothing about.

Two supported ways to close that gap:

**1. The adapter hook (preferred, Python targets).** Declare the
optional `on_promote` coroutine on your `HarnessAdapter`; zicato calls
it once per settled promotion, right after the champion marker
advances, with the epoch, the promoted and parent generation ids, the
promoted snapshot root, and the workspace root. It is best-effort by
contract — a failure never un-promotes the generation, and surfaces as
an ERROR log plus an `on_promote_hook_failed` health WARNING for the
operator to reconcile. The full contract is
[ARCHITECTURE.md](ARCHITECTURE.md) §4.1.1.

Because the hook runs in the evolve process, make the side effect
idempotent: the one window the hook cannot cover is a crash between the
marker advance and the call, which loses the notification rather than
repeating it. An idempotent write plus the reconcile below makes that
window harmless.

**2. Poll `lineage.json` (the fallback, and the answer for non-Python
targets).** The promoted head is the last lineage entry with
`promoted: true`; compare it against your own record of the last head
you applied and reconcile the difference. This works from any language
and any process, it is what the hook exists to make unnecessary rather
than to forbid, and it remains the correct backstop even for targets
that DO use the hook.

One shape is not offered: a promotion hook spelled as a command string
in the epoch contract, which zicato would then execute. The
contract is a data file the loop reads, rewrites, and hands to a
proposer; making it an executable surface is a different trust boundary
than "run the operator's registered adapter", and it is not one this
feature takes on. A non-Python target uses the polling fallback, or
wraps its integration in a thin Python adapter.
