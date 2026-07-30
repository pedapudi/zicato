# target_2_goldfive_steering — end-to-end run

This document walks the operator through standing up a `zicato evolve`
loop where the **inner harness is goldfive itself**. The proposer emits
patches against goldfive's own prompt + threshold surface; the runner
mounts a fresh goldfive snapshot per generation; the tournament scores
the snapshots against an adversarial board.

If `examples/zicato_examples/target_1_presentation/RUN.md` is "drive a
real ADK presentation agent end-to-end", this is "drive goldfive's own
steering layer end-to-end".

## 0. Prerequisites

* Python 3.11+
* `zicato` + `zicato-examples` installed from a repo checkout:
  `make install` (runs `uv sync --all-extras`). This installs the
  `zicato-examples` package so `zicato_examples.*` is importable from
  anywhere — no symlink or `PYTHONPATH` hacks.
* `goldfive` installed editable from the worktree that ships the
  optimization manifest + adversarial testkit:
  ```
  pip install -e /home/sunil/git/goldfive-zicato-optimization-surface
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
`zicato_examples.target_2_goldfive_steering.mocks:harness_llm` dotted
path with no symlink:

```
mkdir -p /tmp/zicato-smoke-t2
cd /tmp/zicato-smoke-t2
```

Bootstrap the workspace and register the goldfive worktree as the
mutable tree:

```
python -m zicato.cli init --workspace .zicato
python -m zicato.cli register --workspace .zicato \
    --adk zicato_examples.target_2_goldfive_steering.agent_under_test:agent \
    --mutable-tree /home/sunil/git/goldfive-zicato-optimization-surface
```

The `--adk` flag points at the tiny `LlmAgent` shipped in this example
— it backs the **normal** entries on the board. The **adversarial**
and **clean** entries use `goldfive.testkit.adversarial:LoopingAgent`
(et al.) and `goldfive.testkit.adversarial:CleanAgent`; those are
resolved at runtime by `zicato.synthetic.resolve_adversarial_agent` so
no separate registration is needed.

> **This registration is REFUSED as written (issue #110).** `register` now
> requires the entrypoint's top-level module to be the basename of one
> `--mutable-tree`, because a generation snapshot copies each tree under its
> basename and the loader only prepends the snapshot root to `sys.path` —
> which resolves top-level packages only. Here the entrypoint
> (`zicato_examples...`) lives OUTSIDE the mutable tree (the goldfive
> worktree), so the snapshot could never supply it: the import would return
> the installed copy and every mutation would be a scored no-op. Target 2's
> "mutate the harness, not the agent" shape needs a mutable-tree layout whose
> basename the entrypoint resolves through (e.g. registering the goldfive
> package itself as the tree AND driving an entrypoint under it) before this
> recipe can run. Target 1 is unaffected.

## 2. Enumerate the goldfive optimization surface

The `mutations` command exercises the same enumeration the orchestrator
will hit, and is the simplest way to confirm the manifest bridge is
wired:

```
python -m zicato.cli mutations --workspace .zicato
```

You should see 31 mutation points — all `kind="span"`, sourced from
`goldfive/optimization/manifest.toml`. Prompt mutations point at the
`.md` body under `goldfive/optimization/prompts/`; threshold mutations
point at the `.py` files the manifest's `source` field names. The
bridge that does this lives in `zicato/synthetic/manifest_bridge.py`
and is invoked from `zicato.mutation.enumerator.enumerate_mutations`.

## 3. Create the epoch

The board / rubric / scoring files live next to this RUN.md, under
`examples/zicato_examples/target_2_goldfive_steering/` in a checkout
(adjust the absolute paths for your machine):

```
EX=/home/sunil/git/zicato/examples/zicato_examples/target_2_goldfive_steering
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
* 2 `synthetic_clean` (CleanAgent x2). The steerer's precision target
  — these must NOT fire warning/critical drift.
* 3 `single_turn` (normal correctness entries). The non-interference
  target — the steerer must not degrade well-behaved workloads.

The rubric's preferred-edits section steers the proposer at the
`refine_system_prompt`, `reasoning_judge_system_prompt`,
`goal_drift_judge_prompt`, and the reasoning-judge threshold knobs.
The forbidden-edits section blocks anything under
`intervention_ladder/*`.

`scoring.json` weighs **pass-rate heavily over drift count**: target
2's loss is pass/fail correctness against synthetic ground truth, not
drift volume. A child generation that lowers drift count by silencing
the steerer will tank pass-rate on the adversarial board and be
rejected.

## 4. Run the evolve loop

Two rounds against the seeded baseline:

```
python -m zicato.cli evolve --workspace .zicato \
    --rounds 2 \
    --mode full \
    --harness-call-llm zicato_examples.target_2_goldfive_steering.mocks:harness_llm \
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
3. **Propose**: the auxiliary mock returns a structured `{hypothesis,
   patches}` payload targeting one of the preferred-edits mutation
   ids (`refine_system_prompt` for v1, `reasoning_judge_system_prompt`
   for v2).
4. **Apply**: `zicato.mutation.applier.apply_patches` copies
   `v0/snapshot/` to `v1/snapshot/` and rewrites the targeted prompt
   markdown body verbatim. Non-`.py` files take the verbatim path
   (the historical Python-string-wrapping behaviour would corrupt
   markdown).
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
   into a generation-level scalar; `evaluate_gate` compares
   `child_scalar` against `parent_scalar + promote_margin`. The mock
   patches don't move the needle, so v1 and v2 both **reject** for
   "insufficient margin".

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

Rejection is the **expected** outcome for the smoke test — the mocks
don't write a substantively better prompt. What the smoke test proves
is that the wiring works end-to-end: the manifest bridge produces
real mutation ids, the proposer's patches address those ids, the
applier rewrites the markdown bodies in the snapshot, the validator
accepts the snapshot, and the tournament scores both generations
without crashing.

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

* `.zicato/epochs/{epoch}/analysis.md` — markdown narrative of the
  epoch. Without an `aux_call_llm` argument the close-step writes a
  short stub; the smoke test's mock aux_llm produces a placeholder
  but the stub path is what the operator usually sees.
* `.zicato/epochs/{epoch}/analysis.html` — self-contained HTML report
  with a lineage SVG (v0 -> v1 -> v2 boxes connected by colored
  edges), a score-trajectory chart, and per-experiment cards.

## 7. What a real (non-mock) round would look like

The mocks here cover the wiring contract. A real round swaps two
things:

1. **harness_llm**: instead of canned JSON, pipe goldfive's planner /
   goal-deriver / reasoning-judge calls to a real LLM. Goldfive's
   planner produces a 5-20 task DAG instead of a single-task plan;
   the reasoning judge fires `OFF_TOPIC` and `JUSTIFIED_DEVIATION`
   verdicts based on actual reasoning content; the embedding-based
   `OFF_TOPIC` / `LOOPING_REASONING` detectors land on the
   WanderingAgent and LoopingAgent runs, which require an embedding
   model the smoke test's mocks don't supply.
2. **aux_llm**: instead of round-rotating between two canned patches,
   the proposer reads the parent generation's pattern detector output
   (e.g. "hot drift kind: hallucination_suspected") and proposes a
   substantive rewrite of the relevant prompt or threshold. With a
   real proposer driving:

   * `pass_rate_delta` becomes the gate.
     `scoring.json` already has `pass_rate_monotonicity: true`, so a
     real proposer that lowers adversarial recall to suppress drift
     loses on the gate even if `drift_loss_delta` improves.
   * Patches that touch a `numeric` mutation (e.g.
     `reasoning_judge_threshold_warning`) hit a current applier
     limitation: the `set_numeric` op resolves the constant via a
     `# zicato:mutable` marker comment, which the manifest bridge
     does not synthesize. The forward path is to teach the applier
     to resolve the constant via the manifest's `python_attr` field
     instead. For now the smoke proposer sticks to `replace` ops
     against prompt bodies.

## 8. Known limitations

* **Numeric mutations are enumerable but not patchable end-to-end**.
  The applier's `set_numeric` path looks for a marker comment near
  the constant; without manifest-bridge marker synthesis the lookup
  fails. The fix is to extend the applier to honour
  `MutationPoint.metadata["python_attr"]` and walk the AST for the
  named module-level attribute. Tracked separately.
* **Mixed event JSONL shapes**. Goldfive's persistence sink emits some
  events as proto-JSON (camelCase, ISO-string timestamps) and others
  as a snake_case + nested timestamp object. The reducer falls back
  to plain `json.loads` per-line when the strict proto parser
  refuses the file. The fix lives upstream in goldfive.
* **Adversarial detectors are partially exercised**. Goldfive's
  embedding-based detectors (OFF_TOPIC, LOOPING_REASONING) need a
  real embedding model; the smoke test's mock harness_llm does not
  supply one. The RefusingAgent and HallucinatingAgent agents trip
  goldfive's lighter-weight rule-based detectors and DO fire drift
  events; the LoopingAgent / WanderingAgent / RunawayDelegationAgent
  produce reasoning + tool-call patterns that a real run would catch
  but the smoke run does not.

These limitations are documented as the forward path; they do not
block the v0+1 smoke run from producing meaningful artifacts.
