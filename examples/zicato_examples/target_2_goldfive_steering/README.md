# Optimizing goldfive's steering layer

Here the inner harness is **goldfive itself**, and the mutable surface
lives inside goldfive's source tree. The proposer edits goldfive's judge
prompts, its refine-prompt template, and its intervention-ladder
threshold knobs. The resulting child generation runs against a board of
synthetic adversarial cases, and the tournament decides whether to
promote it.

The presentation-agent example asks whether the loop converges on an
ordinary agent. This one asks whether it converges when the thing being
optimized is itself the drift detector.

## Why "drift count" cannot be the loss

In every other zicato target the drift signal is the loss: fewer
WARNING and CRITICAL drift events is better. This target inverts that.
What goldfive's steerer produces *is* drift, so scoring it on drift
volume makes the optimal generation one that silences the steerer
entirely — a perfect drift score and a useless detector.

The loss signal here is instead **pass/fail correctness against
synthetic adversarial ground truth**:

- A **synthetic_adversarial** entry pairs a broken-by-construction
  agent from `goldfive.testkit.adversarial` (LoopingAgent,
  HallucinatingAgent, RefusingAgent, WanderingAgent,
  RunawayDelegationAgent) with a non-empty `required_drift_kinds`
  tuple. The entry passes when the run's event log contains at least one
  drift event of every required kind. These entries measure the
  steerer's **recall**: spotting the misbehaviour and emitting the right
  drift.

- A **synthetic_clean** entry pairs a
  `goldfive.testkit.adversarial.CleanAgent` — a well-behaved baseline —
  with an expectation of no WARNING or CRITICAL drift. These entries
  measure **precision**: staying quiet on a normal run.

- A **single_turn** entry runs this directory's own
  `agent_under_test.py`, a two-tool LlmAgent, and checks that it
  produced a usable final output. These entries measure
  **non-interference**: not degrading an ordinary workload with
  over-aggressive refines.

Recall, precision and non-interference are three independent axes, and
the board exercises all three. The scoring weights (`scoring.json`)
weigh the `drift:` channel far below `pass_weight`, because on this
target a drift count is a feature of the run rather than its loss.
Drift counts still feed the scalar, so a pattern detector can surface
something like "drift kind X dropped to zero across the board" as a
signal; they do not drive the gate.

## How the adversarial board works

Each `synthetic_adversarial` row in `board.jsonl` carries:

- `input` — the user-side goal, which the adversarial agent fails to
  complete by construction.
- `adversarial_agent_spec` — a dotted path to one of the testkit
  primitives. These agents are deterministic: LoopingAgent always
  loops, HallucinatingAgent always makes things up. They are
  shipped by goldfive as part of `goldfive.testkit.adversarial`.
- `required_drift_kinds` — the set of drift kinds the steerer must emit
  at least once. `zicato.synthetic.expectations` performs this check at
  run time. The Python-side `predicates.required_drift_fired` hook is a
  permissive supplement to it, and is not itself the check the entry
  passes on.

The `synthetic_clean` rows are the symmetric negative controls: a
`CleanAgent` paired with `predicates.no_warning_or_critical_drift`. The
entry passes when the run completed without aborting and the runtime
layer saw no WARNING or CRITICAL drift. Without these rows, a
trigger-happy steerer would look like an improvement on the adversarial
side alone.

## Registering goldfive as a mutable tree

This target is the case where the mutable surface lives in a different
repository from the entrypoint. Register it with `--mutable-tree`,
pointing at your goldfive checkout:

```
zicato epoch register \
  --adk zicato_examples.target_2_goldfive_steering.agent_under_test:agent \
  --mutable-tree <goldfive checkout>/goldfive
```

> **The tree to register is the package directory rather than the
> repository directory.** A
> snapshot copies each mutable tree under its basename, and the loader
> prepends only the snapshot root to `sys.path`, which resolves top-level
> module names. The basename must therefore be the importable package
> name — `goldfive`, the package inside the repository. The entrypoint
> stays outside the tree, which is the real dependency shape here;
> `epoch register` accepts it with a notice. Each run verifies that every
> tree loaded from the generation snapshot, through a resolution assert
> at load time and the post-run record in
> `generations/{gen}/harness_load.json`. See RUN.md §1.

The adapter walks the goldfive source tree for `# zicato:mutable`
annotations, or the equivalent file-level marker, and produces one
`MutationPoint` per annotated region: judge-prompt strings, threshold
literals, and the refine-template body. The proposer's patches target
those points by id. The patch applier copies the goldfive snapshot and
applies the patches, and the runner mounts that snapshot as goldfive's
source root for the duration of the child generation's runs.

## Running

```
zicato epoch new goldfive-steering-e0 \
  --board examples/zicato_examples/target_2_goldfive_steering/board.jsonl \
  --rubric examples/zicato_examples/target_2_goldfive_steering/rubric.md \
  --scoring examples/zicato_examples/target_2_goldfive_steering/scoring.json

zicato evolve
```

`zicato evolve` then:

1. Snapshots goldfive's source tree as the seed generation.
2. Runs the board against that snapshot, producing a baseline scoreline.
3. Asks the proposer for a hypothesis and a patch set against the
   mutation ids the proposer brief prefers: judge prompts, threshold
   knobs, and the refine template.
4. Materializes a child snapshot, runs the board against it, and settles
   the tournament.

## Reading the outcomes

**A lower `drift_loss` is not necessarily better here.** A round that
reduces `drift_loss` by silencing the reasoning judge also collapses the
adversarial pass rate. The journal reports:

- `pass_rate_delta` — rises on a real improvement.
- `drift_loss_delta` — moves in either direction, and does not gate.
- Per-kind movements — informative for the proposer, and not
  authoritative for the decision.

Promotion is gated on `pass_rate_monotonicity = true` (see
`scoring.json`), so a regression in adversarial recall, clean precision,
or ordinary correctness rejects the child however much the drift side
improved. Whether the steerer got better is answered by all three
pass-rate buckets together, and no single scalar answers it.

## Files in this directory

- `board.jsonl` — 10 entries: 5 adversarial, 2 clean, 3 ordinary.
- `predicates.py` — Python-side outcome predicates for the three kinds.
- `rubric.md` — the proposer brief: the preferred and forbidden edit
  surface, plus style guidance.
- `scoring.json` — the epoch's scoring weights.
- `agent_under_test.py` — the small LlmAgent the ordinary entries run.
