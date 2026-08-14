# Dogfood targets

zicato is built to be used. Three concrete inner-harness targets are
named in escalating order. Each is documented now — even though the
later two are not v0 work — because each forces architectural needs
that the v0 design must accommodate. Pre-engineering the v0 surface
to admit targets 2 and 3 is much cheaper than reshaping it once they
arrive.

The three targets:

| # | Target | Cadence | Forces |
|---|---|---|---|
| 1 | Presentation agent (a known harmonograf reference) | v0 dogfood | nothing extra; validates the v0 stack end-to-end |
| 2 | goldfive's own steering layer | v0+1 | cross-repo mutation; non-drift loss signal |
| 3 | zicato itself | v0+2 | nested zicato instances; recursion guards |

Each rises sharply in risk and meta-ness. The point of running the
earlier ones is to validate the loop before trusting it on itself.

## 1. Target 1 — Presentation agent

### 1.1 What it is

A multi-agent system in harmonograf's reference set:
`harmonograf/tests/reference_agents/presentation_agent_orchestrated/`.
A coordinator routes to specialists (researcher, outliner, writer,
reviser) to produce a presentation outline from a user's brief. The
agent is real, exercised by the reference suite, and produces
realistic drift signals.

### 1.2 Why it's the v0 dogfood

- Real agent with real failure modes (the coordinator sometimes
  routes the writer before the researcher; the researcher
  occasionally confabulates; the reviser sometimes loops on
  semantically-identical revisions).
- The drift signal that goldfive emits IS the loss zicato needs. No
  custom loss model required.
- Mutation surface is in the agent's own tree. No cross-repo
  complications.
- The harmonograf maintainers have run this agent enough that they
  have a clear sense of when a generation is "better" — invaluable
  for calibrating zicato's scoring weights.

### 1.3 The mutation surface

The presentation agent's authors annotate the following with span
markers:

- **Specialist `instruction=` strings.** One marker per specialist.
  Ids: `researcher_instruction`, `outliner_instruction`,
  `writer_instruction`, `reviser_instruction`.
- **Coordinator routing.** The coordinator's system prompt that
  describes when to delegate to which specialist. Id:
  `coordinator_instruction`.
- **Tool descriptions.** The `description=` field on each `AgentTool`
  / leaf tool.

The vendored example
(`examples/zicato_examples/target_1_presentation/`) exposes 9 mutation
points. Enough surface for the proposer to find leverage; small enough
that pattern attribution is unambiguous. (Mutation ids are plain
strings — the example uses the underscore convention; any spelling the
marker regex accepts works.)

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

### 1.5 What target 1 validates about the v0 stack

The full meta-loop, end-to-end:

- ADK adapter registration works on a real coordinator + specialist
  tree.
- Span markers resolve correctly across an ADK agent definition.
- The goldfive event stream captures the drift kinds the presentation
  agent produces.
- The reducer maps drift events to a meaningful `drift_loss`.
- The proposer produces patches the applier can apply cleanly.
- The validator (the pre-apply P1-P3 and post-apply A1-A4 checks in
  [MUTATION-SURFACE.md](MUTATION-SURFACE.md) §6) catches typical
  malformed patches.
- The tournament correctly promotes / rejects candidates.
- The journal renders a readable trace across multiple rounds.

If any of these break, target 1 surfaces it. The operator's "yes that
generation is actually better" judgment is the calibration signal.

### 1.6 What target 1 does NOT validate

- Cross-repo mutation. The presentation agent's mutation surface
  lives entirely in one tree.
- Non-drift loss. The drift signal IS the loss.
- Nested zicato instances. The loop runs once, against one harness.
- The `synthetic_*` board kinds. The board is single-turn and
  multi-turn-emulated, all real user-shaped prompts.

These come in targets 2 and 3.

## 2. Target 2 — goldfive's steering layer

### 2.1 What it is

The inner harness is **goldfive itself** — specifically the steering
layer (the `Steerer` protocol's default implementation plus the
LLM-judge prompts, the intervention ladder thresholds, the
plan-revision strategy). The agent-under-test is some other agent
(e.g. the same presentation agent from target 1) **wrapped by
goldfive**.

The setup looks like:

```
zicato
   │
   └─ inner harness = (presentation agent) wrapped by (goldfive,
                                                       under
                                                       optimization)
```

zicato proposer proposes patches to **goldfive's** prompts and thresholds.
The presentation agent is the workload that exercises goldfive's
steering.

### 2.2 Why this is interesting

Steering quality is hard to measure intrinsically. Drift detection is
the very thing being optimized; you can't use drift counts as the
loss because:

- A steerer that **never fires** has zero drift counts but is
  obviously bad (it missed real drift).
- A steerer that **fires constantly** has high drift counts but
  might be detecting real drift the alternative would have missed.

Drift count is a metric of the **agent**, not of the **steerer**. To
measure the steerer, you need ground truth about when drift *should*
have fired.

### 2.3 The mutation surface

Annotated in goldfive's own source, NOT in zicato. v0 anticipates
this — `HarnessAdapter.mutation_points()` returns a list over a list
of source roots even though v0 typically uses one. For target 2 the
list has two roots:

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
markers. Goldfive doesn't depend on zicato (one-way dependency) — the
markers are inert Python comments to goldfive's runtime.

### 2.4 The CLI surface for cross-repo registration

`zicato epoch register` accepts repeated `--mutable-tree` flags:

```
zicato epoch register --adk presentation_agent_package.agent:root_agent \
    --mutable-tree path/to/presentation_agent_package \
    --mutable-tree path/to/goldfive/goldfive
```

`--adk` is a DOTTED MODULE PATH, never a filesystem path. Each registered
root's BASENAME must be the importable package name (`presentation_agent_package`,
`goldfive` above): the snapshot copies each root under its basename and the
loader only prepends the snapshot root to `sys.path`, which resolves top-level
names only — a root whose basename Python cannot name as a module could never
be shown to have run from the snapshot, so `register` refuses it (issue #110).

The entrypoint may live inside one of those roots (as above) or outside all of
them — the dependency shape, which target 2 uses: mutate `goldfive` and drive
it from a harness module that imports it. Either way EVERY registered root is
verified per run, not just the entrypoint's: `load` asserts each root's
top-level name resolves inside the generation snapshot, and after each unit the
worker records which roots were actually imported in
`generations/{gen}/harness_load.json`. In the two-root example above, a round
that mutates `goldfive` but whose units never import it raises the
`tree_never_imported` loop-health WARNING instead of scoring a no-op.

The first registered root is conventionally the package containing the
agent factory; additional roots are added with repeated
`--mutable-tree` flags. The adapter's `mutation_points()` walks every
registered root and concatenates the results.

### 2.5 The non-drift loss model

This is the architectural change target 2 forces. Drift cannot be the
loss; therefore something else must be.

The target-2 loss model has three terms:

#### 2.5.1 Outcome predicates on real entries

On a normal board entry (single-turn or multi-turn-emulated), the
agent's task is judged with a normal expectation. Pass means the
steerer didn't make things worse. Fail means the steerer interfered
badly (or didn't catch a real failure). This is the same pass-rate
signal as target 1 but here it scores the *steerer*, not the agent.

#### 2.5.2 Adversarial recall via `synthetic_adversarial`

A new board entry kind: `synthetic_adversarial`. The entry wires a
**known-bad agent** in place of the real workload — an agent
deliberately constructed to loop, hallucinate, or refuse. The
expectation is "drift fires of the right kind".

Schema (forward-looking; the `synthetic_adversarial` kind and its
discriminant fields ship in the v0 `BoardEntry` type; the runner
implementation lands with target 2):

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
`required_drift_kinds` (each validated against the registered drift
kind set). Pass = the kinds in `required_drift_kinds` fire. Fail =
drift missed.

The synthetic agent's source is in the operator's project, not in
zicato. `adversarial_agent_spec` is a dotted path the adapter resolves
at run time. A small library of known-bad agents (looping,
confabulating, refusing, off-topic) gives the operator a starter set.

#### 2.5.3 Specificity via `synthetic_clean`

Another new entry kind: `synthetic_clean`. Wires a **known-good
agent** that just does its job. The intent is "no spurious drift
fires". Pass = drift count zero (or below a tolerance). Fail = drift
fired when none was warranted.

The `synthetic_clean` kind also ships in the v0 `BoardEntry` type;
`validate` requires only `input` for it (the clean-run thresholds live
in the entry's `context` map and the target-2 loss model, not as
first-class discriminant fields):

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

The adversarial set measures **recall** (does the steerer catch real
drift?); the clean set measures **specificity** (does the steerer
avoid false-positive drift?). Together they balance the steerer's
sensitivity.

#### 2.5.4 Cost from the goldfive event stream

Goldfive's `GoldfiveLLMCallStart` / `GoldfiveLLMCallEnd` events carry
per-call latency. A steerer that fires more judge calls per turn
costs more. Loss term:

```
cost_units[run] = count of goldfive-lane LLM call ends in the run
```

The target-2 loss combines these into a non-drift scalar:

```
loss[entry] = (1 - pass_fail) * outcome_weight
            + adversarial_miss_count * adversarial_weight
            + spurious_drift_count * specificity_weight
            + cost_units * cost_weight
```

The weights are tuned operator-side. The v0 scoring infrastructure
admits this loss shape because the `LossProfile` type is open-ended on
new fields and the per-kind weights live in `scoring.json` — adding
`outcome_weight`, `adversarial_weight`, `specificity_weight`,
`cost_weight` doesn't break the schema.

### 2.6 Why drift cannot be the loss (one more time)

Said differently to make sure it lands: if zicato proposer proposed a patch to
goldfive's refine prompt that caused goldfive to never fire drift
again, the drift count would go to zero and the patch would "win" on
the drift signal. The patched goldfive would also be useless. Drift
counts measure the agent through goldfive's lens; they cannot measure
goldfive itself.

The non-drift loss (`outcome predicate + adversarial recall +
specificity + cost`) measures goldfive on its actual job: catching
real drift and ignoring fake drift, at reasonable cost.

### 2.7 v0 architectural commitments that target 2 forces

Even though target 2 is post-v0, the v0 design must admit it without
schema breakage:

1. **Two `call_llm` callables.** Already pinned in
   [EMULATOR.md](EMULATOR.md) for emulator collusion. Target 2
   reinforces — the goldfive being optimized must NOT share an LLM
   with zicato's auxiliary work.
2. **`mutation_points()` over a list of source roots.** Already
   pinned in [MUTATION-SURFACE.md](MUTATION-SURFACE.md) §5. v0
   typically uses one; target 2 uses two.
3. **`BoardEntry.kind` already reserves the synthetic slots.**
   Pinned in [BOARD-FORMAT.md](BOARD-FORMAT.md) §6. `synthetic_adversarial`
   and `synthetic_clean` ship today as reserved members of the
   `BoardEntryKind` literal (with their discriminant fields and
   `validate` rules already in the v0 `BoardEntry`); target 2 lands
   only the runner, not a schema change.
4. **`LossProfile` is open-ended on new fields.** Already pinned in
   [TELEMETRY.md](TELEMETRY.md). The target-2 cost / adversarial /
   specificity fields plug in.
5. **Scoring weights are configurable per project.** Already pinned
   in [SCORING.md](SCORING.md). New weights for new loss terms drop
   into `scoring.json`.

If any of these were not pinned in v0, target 2 would force a
schema-breaking change. None of them are expensive to commit to
upfront.

## 3. Target 3 — zicato itself

### 3.1 What it is

Bootstrapping. The inner harness is **zicato** — specifically the
prompts and heuristics zicato uses: the proposer's system prompt, the
analysis-pass prompt, the emulator persona template, the rubric
template. The thing under optimization is the thing doing the
optimizing.

The setup:

```
outer zicato
   │
   └─ inner harness = (inner zicato)
                         │
                         └─ inner inner harness = (presentation agent
                                                   = target 1)
```

The outer zicato optimizes the inner zicato; the inner zicato
optimizes the presentation agent.

### 3.2 Why this is the natural endpoint

The whole zicato thesis is "agents that improve themselves on the
loss of their own outputs". Applied to a meta-harness that itself has
prompts, the loop closes. Target 3 is the proof point that the same
mechanism that optimizes other agents can optimize the optimizer.

### 3.3 Why naive improvement-velocity is intractable

The obvious loss for target 3 is "how fast does the inner zicato
improve target 1 in successive epochs?". This is intractable:

- Each "round" of the outer zicato is an entire **epoch** of the
  inner zicato (multiple rounds of generation + tournament against
  the presentation agent's board).
- That's hours of wall clock per round, minimum, and the cost of N
  rounds for a meaningful tournament balloons.
- The signal is noisy at this scale — the inner zicato's
  improvement velocity depends on the presentation agent's board
  composition, which can shift between rounds in ways that mask
  the actual improvement from the outer zicato's patches.

The naive loop is technically defined but operationally infeasible.

### 3.4 The labeled-pair offline benchmark

The practical answer: a **labeled benchmark** of
`(LossProfile, ideal Experiment)` pairs. The outer zicato is
evaluated by how well its proposer would have generated the labeled
ideal `Experiment` given the labeled `LossProfile`.

The benchmark is offline:

1. The operator (or a trusted prior zicato run) curates pairs:
   "given this loss-profile aggregate, the right hypothesis was X,
   the right patches were Y, and they would have moved the score
   by Z".
2. The outer zicato's proposer is run against each labeled
   `LossProfile`. The hypothesis it generates is scored against
   the labeled ideal:
   - `core_idea` similarity (via auxiliary LLM judge).
   - `modulating` overlap (exact mutation-point id match).
   - `expected_drift_movements` direction match.
   - `risks` quality (judge: are the listed risks plausible?).
3. The aggregate similarity score is the loss for the round.

The benchmark is fast (no real agent runs — just proposer evaluations)
and the signal is dense (every pair contributes). The trade-off is
that the benchmark is only as good as the labels; curating it is
real work.

### 3.5 The recursion / instance_id need

Nested zicato instances must NOT cross-talk:

- Outer zicato's workspace lives at `.zicato/instances/outer/`.
- Inner zicato's workspace lives at `.zicato/instances/inner/`.
- Outer zicato's runtime sees its own epochs / generations / patterns;
  inner zicato sees only its own.

v0 anticipates this with an `instance_id` in the runtime config:

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

When `instance_id == "default"` (the v0 case), the workspace path
reduces to `.zicato/`. When `instance_id` is set, the workspace is
keyed under it.

Recursion guards in the runner are easy to forget if not planned for.
v0 plans for them now:

- The outer zicato's `harness_call_llm` is the inner zicato's
  `auxiliary_call_llm` plumbing. Strictly: when the outer zicato's
  `HarnessAdapter` invokes the inner zicato, the inner zicato gets
  its own two `call_llm` callables, distinct from the outer's, with
  the same hard two-callable check.
- The outer zicato's `events.jsonl` path includes the
  `instance_id`; the inner zicato's writes never collide with the
  outer's.
- The control channel (if any) is keyed by `instance_id`; an outer
  zicato's pause signal cannot reach an inner zicato by accident.

### 3.6 The target-3 mutation surface

Annotated in zicato's own code:

- `zicato.proposer.system_prompt` — the proposer's system prompt.
- `zicato.analysis.system_prompt` — the analysis-pass system prompt.
- `zicato.emulator.default_prompt_template` — the default emulator
  prompt template (the one with the answer-leak refusal section).
- `zicato.rubric.template` — the default rubric template (the
  `zicato init` skeleton).

Note: the default emulator prompt template is **mutable** but every
patch to it must preserve the answer-leak refusal section (the
`required_placeholders` / A3 post-apply check in
[MUTATION-SURFACE.md](MUTATION-SURFACE.md) §6). Removing the refusal
section would break the emulator's collusion-proof construction.

The validator rules carry forward into target 3 because the validator
is part of zicato's own code — the outer zicato can patch zicato's
prompts but cannot patch the validator that protects the prompts.

### 3.7 Sequencing

Target 1 first. Hold target 2 until target 1 has shown the loop
converges. Hold target 3 until target 2 has produced at least one
full epoch with real evidence that the loop improves something.

The risk and meta-ness rise sharply with each target; the value of
running the earlier ones is to validate the loop before trusting it
on itself.

## 4. v0 design summary

Concrete v0 commitments driven by knowing about targets 2 and 3:

| Commitment | Pinned in |
|---|---|
| Two distinct `call_llm` callables, hard-validated at config time. | [EMULATOR.md](EMULATOR.md) §3 |
| `HarnessAdapter.mutation_points()` walks a list of source roots, not a single tree. | [MUTATION-SURFACE.md](MUTATION-SURFACE.md) §5 |
| `BoardEntry.kind` is string-typed against a registered set. | [BOARD-FORMAT.md](BOARD-FORMAT.md) §6 |
| `LossProfile` is open-ended on new fields; weights live in per-epoch `scoring.json`. | [TELEMETRY.md](TELEMETRY.md) §3, [SCORING.md](SCORING.md) §2 |
| Runtime config carries `instance_id`; workspace is keyed by it. | this document §3.5 |

None of these add real cost to v0. Each prevents a schema break
later. The architectural philosophy is "the v0 surface should not
need to apologize when targets 2 and 3 land".

## 5. Cross-references

| Topic | Document |
|---|---|
| Mutation surface annotation rules, AST resolution | [MUTATION-SURFACE.md](MUTATION-SURFACE.md) |
| Board entry kinds, open-ended discriminator | [BOARD-FORMAT.md](BOARD-FORMAT.md) |
| LossProfile fields and the reducer | [TELEMETRY.md](TELEMETRY.md) |
| Scoring weights and the non-drift loss extension | [SCORING.md](SCORING.md) |
| Emulator collusion-proofing (relevant to target 2 + 3) | [EMULATOR.md](EMULATOR.md) |
| Why mandatory hypothesis schema (the offline-benchmark target) | [RATIONALE.md](RATIONALE.md) |
| The post-promotion hook contract (`on_promote`) | [ARCHITECTURE.md](ARCHITECTURE.md) §4.1.1 |

## 6. Targets whose state lives outside the tree

All three targets above share a property that is easy to mistake for a
law: their evolved state IS the mutable tree. Promote a generation and
the promoted snapshot plus the `current_generation` marker is the entire
result — there is nothing else to update, which is why none of them
needed a promotion hook to be built.

That property does not generalize. A target can perfectly well be
evolvable through a source tree while its *operative* state lives
somewhere the tree cannot reach: a prompt or policy row in a database, a
config served to a fleet, a compiled artifact in an object store, a
cache the running system reads. For those, "the champion advanced" is
not the end of the round — it is the trigger for a write the loop knows
nothing about.

Two supported ways to close that gap:

**1. The adapter hook (preferred, Python targets).** Declare the
optional `on_promote` coroutine on your `HarnessAdapter`; zicato calls
it exactly once per settled promotion, right after the champion marker
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

What is deliberately not offered is a contract-declared shell command —
a promotion hook spelled as a command string in the epoch contract. The
contract is a data file the loop reads, rewrites, and hands to a
proposer; making it an executable surface is a different trust boundary
than "run the operator's registered adapter", and it is not one this
feature takes on. A non-Python target uses the polling fallback, or
wraps its integration in a thin Python adapter.
