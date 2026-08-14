# Target 2 — Optimizing goldfive's Steering Layer

This example is the v0+1 dogfood target for zicato: the inner harness
is **goldfive itself**, and the mutable surface lives inside goldfive's
source tree. Concretely, zicato will propose edits to goldfive's judge
prompts, refine-prompt template, and intervention-ladder threshold
knobs, run the resulting child generation against a synthetic
adversarial board, and decide tournament-style whether to promote.

If target 1 (presentation agent) is "does the loop converge on a real
agent at all?", target 2 is "does the loop converge when the thing
being optimized is itself the drift detector?".

## Why "drift count" cannot be the loss

In every other zicato target the drift signal IS the loss — fewer
WARNING/CRITICAL drifts is better. Target 2 inverts that. The thing
goldfive's steerer produces is drift; if we score it on drift volume,
the trivially-optimal generation is one that silences the steerer
entirely. That gets a perfect drift score and is functionally useless.

So target 2's loss signal is **pass/fail correctness against synthetic
adversarial ground truth**:

- A **synthetic_adversarial** entry pairs a deliberately-broken agent
  from `goldfive.testkit.adversarial` (LoopingAgent,
  HallucinatingAgent, RefusingAgent, WanderingAgent,
  RunawayDelegationAgent) with a non-empty `required_drift_kinds`
  tuple. The entry passes iff the run's event JSONL contains at least
  one drift event of every required kind. The steerer's job here is
  RECALL: spot the misbehaviour and emit the right drift.

- A **synthetic_clean** entry pairs a `goldfive.testkit.adversarial.CleanAgent`
  (a well-behaved baseline) with a "no WARNING or CRITICAL drift"
  expectation. The steerer's job here is PRECISION: don't cry wolf on
  a normal run.

- A **single_turn** "normal" entry runs zicato's own tiny
  `agent_under_test.py` (a two-tool LlmAgent) and checks that the
  agent produced a usable final output. The steerer's job here is
  NON-INTERFERENCE: don't degrade workloads with over-aggressive
  refines.

Recall, precision, and non-interference are three orthogonal axes; the
board exercises all three. The scoring weights (`scoring.json`)
de-emphasize drift_weight relative to pass_weight precisely because
drift counts are FEATURES, not LOSS, in this target. They feed into
the scalar so pattern detectors can still surface "drift kind X dropped
to zero across the board" as a signal — but they no longer drive the
gate.

## How the adversarial board works

Each `synthetic_adversarial` row in `board.jsonl` carries:

- `input` — the user-side goal (the adversarial agent will fail to
  complete it, by construction).
- `adversarial_agent_spec` — a dotted path to one of the testkit
  primitives. These agents are deterministic: LoopingAgent ALWAYS
  loops, HallucinatingAgent ALWAYS makes things up, etc. They are
  shipped by goldfive as part of `goldfive.testkit.adversarial`
  (links to the testkit module's docs to follow once the runtime
  support lands).
- `required_drift_kinds` — the set of drift kinds the steerer MUST
  emit at least once. Wired through `zicato.synthetic.expectations`
  at run time; the Python-side `predicates.required_drift_fired`
  hook is a permissive supplement, not the real check.

The `synthetic_clean` rows are symmetric negative controls: a
`CleanAgent` paired with `predicates.no_warning_or_critical_drift`.
Pass = run completed without abort AND the runtime layer saw no
WARNING/CRITICAL drift. Without these, a trigger-happy steerer would
look like an improvement on the adversarial side alone.

## Registering goldfive as a mutable tree

Target 2 is the dogfood case that exercises the "mutable surface lives
in a different repo from the entrypoint" path. Use `--mutable-tree`:

```
zicato epoch register \
  --adk zicato_examples.target_2_goldfive_steering.agent_under_test:agent \
  --mutable-tree /home/sunil/git/goldfive/goldfive
```

> **The tree is the PACKAGE directory (issue #110).** A snapshot copies each
> mutable tree under its basename and the loader only prepends the snapshot
> root to `sys.path`, which resolves TOP-LEVEL module names — so the basename
> must be the importable package name (`goldfive`, the package inside the
> repo), not the repo directory. The entrypoint deliberately stays outside the
> tree: this is the dependency shape, `register` accepts it with a NOTICE, and
> each tree is verified to have loaded from the generation snapshot per run
> (load-time resolution assert + the post-run record in
> `generations/{gen}/harness_load.json`). See RUN.md §1.

The adapter walks the goldfive source tree for `# zicato:mutable`
annotations (or the equivalent file-level marker) and produces a
`MutationPoint` per annotated region — judge-prompt strings,
threshold literals, the refine-template body. The proposer's patches
target those points by id; the patch applier copies the goldfive
snapshot, applies the patches, and the runner mounts the snapshot as
goldfive's source root for the duration of the child generation's
runs.

## Running

```
zicato epoch new goldfive-steering-e0 \
  --board examples/zicato_examples/target_2_goldfive_steering/board.jsonl \
  --rubric examples/zicato_examples/target_2_goldfive_steering/rubric.md \
  --scoring examples/zicato_examples/target_2_goldfive_steering/scoring.json

zicato evolve
```

`zicato evolve` will:

1. Snapshot goldfive's source tree as the seed generation.
2. Run the board against that snapshot, producing a baseline scoreline.
3. Ask the proposer for a hypothesis + patch set against the preferred
   mutation ids listed in the rubric (judge prompts, threshold knobs,
   refine template).
4. Materialize a child snapshot, run the board against it, and decide
   tournament-style.

## What outcomes look like

Counterintuitive but important: **lower drift_loss is NOT necessarily
better for target 2**. A round that reduces drift_loss by silencing
the reasoning judge will also tank adversarial pass-rate. The
journal will show:

- `pass_rate_delta` — should track up on a real improvement.
- `drift_loss_delta` — can move either direction; not a gate.
- Per-kind movements — useful for the proposer to read but not
  authoritative.

Promotion is gated on `pass_rate_monotonicity = true` (see
`scoring.json`), so any regression in adversarial recall, clean
precision, or normal correctness will reject the child regardless of
drift-side improvement. The full "did the steerer get better" question
is answered by the combination of the three pass-rate buckets, not by
any single scalar.

## Files in this directory

- `board.jsonl` — 10 entries: 5 adversarial, 2 clean, 3 normal.
- `predicates.py` — Python-side outcome predicates for the three kinds.
- `rubric.md` — preferred/forbidden edit surface, style guidance.
- `scoring.json` — frozen epoch scoring weights.
- `agent_under_test.py` — the tiny LlmAgent the normal entries run.
