# target_2_goldfive_steering — end-to-end run

This document walks the operator through standing up a `zicato evolve`
loop where the **system under test is goldfive itself**. The proposer emits
patches against goldfive's own prompt + threshold surface; the runner
mounts a fresh goldfive snapshot per generation; the tournament scores
the snapshots against an adversarial board.

The presentation-agent walkthrough
(`examples/zicato_examples/target_1_presentation/RUN.md`) drives an
ordinary agent end to end. This one drives goldfive's own steering layer
end to end.

## 0. Prerequisites

* Python 3.11+
* `zicato` + `zicato-examples` installed from a repo checkout:
  `make install` (runs `uv sync --all-extras`). This installs the
  `zicato-examples` package so `zicato_examples.*` is importable from
  anywhere — no symlink or `PYTHONPATH` hacks.
* `goldfive` installed editable from a checkout that carries the
  optimization manifest and the adversarial testkit:
  ```
  pip install -e <goldfive checkout>
  ```
* The mock callables in
  `examples/zicato_examples/target_2_goldfive_steering/mocks.py` are
  byte-deterministic, so no LLM credentials are required for the smoke
  run.

Sanity check:
```
python -c "import goldfive; print(goldfive.__file__)"
python -c "from goldfive.testkit.adversarial import LoopingAgent; print(LoopingAgent.__name__)"
python -c "from goldfive.optimization import manifest; print(len(manifest.Manifest.load().mutations))"
```

You should see the goldfive package path, the `LoopingAgent` class
name, and 31 mutations (the prompts + threshold knobs declared in
`goldfive/optimization/manifest.toml`).

## 1. Workspace setup

Pick a scratch directory. The smoke test uses `/tmp/zicato-smoke-t2/`.
Because `zicato-examples` is installed (`make install`), the mock
module is importable by its
`zicato_examples.target_2_goldfive_steering.mocks:target_llm` dotted
path with no symlink:

```
mkdir -p /tmp/zicato-smoke-t2
cd /tmp/zicato-smoke-t2
```

Bootstrap the workspace and register the goldfive worktree as the
mutable tree:

```
python -m zicato.cli init --workspace .zicato
python -m zicato.cli epoch register --workspace .zicato \
    --adk zicato_examples.target_2_goldfive_steering.agent_under_test:agent \
    --mutable-tree <goldfive checkout>/goldfive
```

The `--adk` flag points at the tiny `LlmAgent` shipped in this example
— it backs the **normal** entries on the board. The **adversarial**
and **clean** entries use `goldfive.testkit.adversarial:LoopingAgent`
(et al.) and `goldfive.testkit.adversarial:CleanAgent`; those are
resolved at runtime by `zicato.synthetic.resolve_adversarial_agent` so
no separate registration is needed.

> **`--mutable-tree` names the package directory rather than the
> repository root.** A generation snapshot copies each mutable tree under
> its basename, and the loader prepends only the snapshot root to
> `sys.path`, which resolves top-level module names. The tree's basename
> must therefore be the importable package name. A repository root whose
> directory name is not importable — a hyphenated name, for instance — is
> refused, because the snapshot's mutated copy could then never be shown
> to be the goldfive that ran. The supported form is the `goldfive`
> package directory inside the checkout.
>
> The entrypoint stays outside the mutable tree, which is the real
> dependency shape: mutate goldfive, and drive it from a harness module
> that imports it. `epoch register` accepts it and prints a notice saying
> the trees must be imported by the harness at run time. Verification
> happens where that truth exists. `load` asserts that every registered
> tree resolves inside the generation snapshot, and after each unit the
> worker records which trees were imported in
> `generations/{gen}/harness_load.json`. A tree that no unit of a
> generation ever imported raises a WARNING loop-health finding
> (`tree_never_imported`) — the signal that catches mutations that were
> never under test.

## 2. Enumerate the goldfive optimization surface

`inspect mutations` runs the same enumeration the orchestrator runs, and
is the simplest way to confirm the manifest bridge is wired:

```
python -m zicato.cli inspect mutations --workspace .zicato
```

You should see 31 mutation points — all `kind="span"`, sourced from
`goldfive/optimization/manifest.toml`. Prompt mutations point at the
`.md` body under `goldfive/optimization/prompts/`; threshold mutations
point at the `.py` files the manifest's `source` field names. The
bridge that does this lives in `zicato/synthetic/manifest_bridge.py`
and is invoked from `zicato.mutation.enumerator.enumerate_mutations`.

## 3. Create the epoch

The board, brief and scoring files live next to this file, under
`examples/zicato_examples/target_2_goldfive_steering/` in a checkout:

```
ZICATO=${ZICATO:?set ZICATO to your zicato checkout}
EX=$ZICATO/examples/zicato_examples/target_2_goldfive_steering
python -m zicato.cli epoch new t2_smoke --workspace .zicato \
    --board   $EX/board.jsonl \
    --rubric  $EX/rubric.md \
    --scoring $EX/scoring.json
```

The board ships 10 entries:
* 5 `synthetic_adversarial` (LoopingAgent, HallucinatingAgent,
  RefusingAgent, WanderingAgent, RunawayDelegationAgent). Each
  declares `required_drift_kinds` — the steerer's recall target for
  that adversarial pattern.
* 2 `synthetic_clean` (two CleanAgent runs). The steerer's precision
  target: these must not fire warning or critical drift.
* 3 `single_turn` correctness entries. The non-interference target: the
  steerer must not degrade a well-behaved workload.

The proposer brief's preferred-edits section steers the proposer at the
`refine_system_prompt`, `reasoning_judge_system_prompt`,
`goal_drift_judge_prompt`, and the reasoning-judge threshold knobs.
The forbidden-edits section blocks anything under
`intervention_ladder/*`.

`scoring.json` weighs **pass rate far above drift count**, because the
loss on this target is pass/fail correctness against synthetic ground
truth rather than drift volume. A child generation that lowers its drift
count by silencing the steerer collapses its pass rate on the
adversarial board and is rejected.

## 4. Run the evolve loop

Two rounds against the seeded baseline:

```
python -m zicato.cli evolve --workspace .zicato \
    --rounds 2 \
    --mode full \
    --harness-call-llm zicato_examples.target_2_goldfive_steering.mocks:target_llm \
    --auxiliary-call-llm zicato_examples.target_2_goldfive_steering.mocks:aux_llm
```

What happens, step by step:

1. **Seed v0**: the orchestrator's `_ensure_baseline_snapshot` notices
   there are no generations yet and copies the registered mutable
   tree into `epochs/{epoch}/generations/v0/snapshot/<tree_name>/`.
2. **Enumerate**: the orchestrator walks `v0/snapshot/` for mutation
   points. The native marker pass finds nothing (goldfive carries no
   `# zicato:mutable` comments); the manifest bridge finds the 31
   manifest-declared points.
3. **Propose**: the evaluation mock returns a structured `{hypothesis,
   patches}` payload targeting one of the preferred-edits mutation
   ids (`refine_system_prompt` for v1, `reasoning_judge_system_prompt`
   for v2).
4. **Apply**: `zicato.mutation.applier.apply_patches` copies
   `v0/snapshot/` to `v1/snapshot/` and rewrites the targeted prompt
   markdown body verbatim. A file that is not `.py` takes the verbatim
   path, because wrapping its content as a Python string would corrupt
   the markdown.
5. **Validate**: `zicato.mutation.validator.validate_post_apply`
   re-enumerates, checks the post-apply mutation point still
   resolves, and confirms `.py` files still parse. Non-`.py` files
   skip the ast.parse and import-survival checks.
6. **Run the tournament**: every board entry executes under both
   `v0/snapshot/` and `v1/snapshot/`. Synthetic kinds route through
   `zicato.synthetic.run_adversarial_entry` /
   `run_clean_entry`; single_turn entries route through the ADK
   adapter. Both paths drop events.jsonl under
   `epochs/{epoch}/generations/{vN}/runs/{entry_id}/`.
7. **Gate**: `aggregate_generation_score` rolls per-run loss profiles
   into a generation-level scalar, and `evaluate_gate` compares
   `child_scalar` against `parent_scalar + promote_margin`. The mock
   patches change the score by nothing measurable, so v1 and v2 are both
   **rejected** for "insufficient margin".

Expected output:
```
[
  {"parent_generation_id": "v0", "proposed_generation_id": "v1",
   "tournament_decision": "rejected", "rejection_reason": "insufficient margin: ...",
   "parent_scalar": 1.05..., "child_scalar": 1.05..., "delta_scalar": ~0},
  {"parent_generation_id": "v0", "proposed_generation_id": "v2",
   "tournament_decision": "rejected", ...}
]
```

Rejection is the **expected** outcome here, because the mocks do not
write a substantively better prompt. What the run proves is that the
wiring works end to end: the manifest bridge produces real mutation ids,
the proposer's patches address those ids, the applier rewrites the
markdown bodies in the snapshot, the validator accepts the snapshot, and
the tournament scores both generations without crashing.

## 5. Verify artifacts

After the run, you should see (per generation):

```
.zicato/epochs/{epoch}/generations/v1/
├── experiment.json       # hypothesis + outcome record
├── patches/
│   └── <patch-id>.json   # the single patch the proposer emitted
├── gen_score.json        # cached aggregate for fast-mode rounds
├── runs/
│   ├── looping_research_2_turn/events.jsonl
│   ├── hallucinating_fact_fetch/events.jsonl
│   ├── ...
│   └── normal_summary_print_press/events.jsonl
└── snapshot/             # goldfive copy with the patched prompt
```

Confirm that:

* `experiment.json` carries a `patches` array with at least one entry
  whose `mutation_id` matches a real goldfive manifest entry
  (`refine_system_prompt` for v1, `reasoning_judge_system_prompt` for
  v2).
* `patches/*.json` rationale references the manifest-bridged surface.
* `events.jsonl` exists for every board entry — synthetic kinds and
  single_turn kinds alike.
* The `refusing_research` events.jsonl contains a `DRIFT_KIND_AGENT_REFUSAL`
  drift at `DRIFT_SEVERITY_WARNING`. This is the steerer's recall
  signal firing on the RefusingAgent (one of the few adversarial
  patterns that goldfive's lightweight detectors catch without an
  embedding model). `grep DRIFT_KIND refusing_research/events.jsonl`
  to see it.

## 6. Close the epoch

```
python -m zicato.cli epoch close --workspace .zicato
```

This writes:

* `.zicato/epochs/{epoch}/analysis.md` — a markdown narrative of the
  epoch. Without an `aux_call_llm` argument the close step writes a
  short stub, which is what an operator usually sees; this walkthrough's
  mock produces a placeholder instead.
* `.zicato/epochs/{epoch}/analysis.html` — self-contained HTML report
  with a lineage SVG (v0 -> v1 -> v2 boxes connected by colored
  edges), a score-trajectory chart, and per-experiment cards.

## 7. Running against real models

The mocks cover the wiring contract. Running against real models means
replacing two callables.

1. **target_llm**: route goldfive's planner, goal-deriver, and
   reasoning-judge calls to a real model instead of returning canned
   JSON. Goldfive's planner then produces a task graph of five to twenty
   tasks rather than a single-task plan, and the reasoning judge issues
   `OFF_TOPIC` and `JUSTIFIED_DEVIATION` verdicts from the actual
   reasoning content. The embedding-based `OFF_TOPIC` and
   `LOOPING_REASONING` detectors need an embedding model, which the
   mocks do not supply; with one, they land on the WanderingAgent and
   LoopingAgent runs.
2. **aux_llm**: instead of rotating between two canned patches, the
   proposer reads the parent generation's pattern-detector output — for
   example "hot drift kind: hallucination_suspected" — and proposes a
   substantive rewrite of the relevant prompt or threshold. With a real
   proposer driving, `pass_rate_delta` decides the round: `scoring.json`
   sets `pass_rate_monotonicity: true`, so a proposer that lowers
   adversarial recall to suppress drift loses at the gate however much
   `drift_loss_delta` improves.

## 8. Known limitations

* **A numeric mutation can be enumerated but not patched end to end.**
  The applier's `set_numeric` path looks for a `# zicato:mutable` marker
  comment near the constant, and the manifest bridge synthesizes no such
  marker, so the lookup fails. Closing this means teaching the applier to
  honour `MutationPoint.metadata["python_attr"]` and to walk the AST for
  the named module-level attribute. Until then the mock proposer uses
  `replace` operations against prompt bodies only.
* **The event log carries two shapes.** Goldfive's persistence sink
  emits some events as proto-JSON (camelCase keys, ISO-string
  timestamps) and others as snake_case with a nested timestamp object.
  The reducer falls back to a plain per-line `json.loads` when the strict
  proto parser refuses the file. The single-shape fix belongs in
  goldfive.
* **The adversarial detectors are only partly exercised.** Goldfive's
  embedding-based detectors (OFF_TOPIC, LOOPING_REASONING) need a real
  embedding model, which the mock `target_llm` does not supply. The
  RefusingAgent and HallucinatingAgent runs trip goldfive's rule-based
  detectors and do fire drift events. The LoopingAgent, WanderingAgent
  and RunawayDelegationAgent produce reasoning and tool-call patterns
  that a run with real models would catch and this one does not.

None of these limitations stops the walkthrough from producing
meaningful artifacts.
