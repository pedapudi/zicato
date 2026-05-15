# target_1_presentation — running the loop end-to-end

This walkthrough takes a fresh checkout of `pedapudi/zicato` and runs
`zicato evolve --rounds 2` against the vendored presentation agent
with deterministic mock LLMs. The whole thing completes in a few
seconds and produces the canonical artifact tree (snapshots,
experiment.json, patches, journal, analysis, lineage).

The intent here is to give an operator something concrete to point at
when wiring real LLMs. The mocks under [`mocks.py`](./mocks.py) are
byte-deterministic placeholders — they exist to exercise the
plumbing, not to produce a meaningful improvement signal.

## Prerequisites

The smoke test exercises the full orchestrator path, which in turn
imports goldfive (for the inner-harness runner) and the ADK SDK (for
the agent tree). Both are optional extras; if your venv does not
already have them, install them once:

```bash
VIRTUAL_ENV=/home/sunil/git/zicato/.venv uv pip install -e /home/sunil/git/goldfive
VIRTUAL_ENV=/home/sunil/git/zicato/.venv uv pip install google-adk
```

(Adjust paths for your machine.)

## End-to-end loop

```bash
# Pick a scratch workspace anywhere off the repo. Symlink the examples/
# tree so the dotted imports resolve.
rm -rf /tmp/zicato-smoke-t1
mkdir -p /tmp/zicato-smoke-t1
cd /tmp/zicato-smoke-t1
ln -s /home/sunil/git/zicato/examples ./examples

PY=/home/sunil/git/zicato/.venv/bin/python

# 1. Bootstrap the workspace.
$PY -m zicato.cli init --workspace .zicato

# 2. Register the agent + the mutable source tree.
$PY -m zicato.cli register --workspace .zicato \
    --adk examples.target_1_presentation.agent.agent:root_agent \
    --mutable-tree /home/sunil/git/zicato/examples/target_1_presentation/agent

# 3. Open an epoch pinned to the example's board / rubric / scoring.
$PY -m zicato.cli epoch new t1_smoke --workspace .zicato \
    --board   /home/sunil/git/zicato/examples/target_1_presentation/board.jsonl \
    --rubric  /home/sunil/git/zicato/examples/target_1_presentation/rubric.md \
    --scoring /home/sunil/git/zicato/examples/target_1_presentation/scoring.json

# 4. Inspect the mutation surface the proposer will see (9 ids).
$PY -m zicato.cli mutations --workspace .zicato

# 5. Run two evolve rounds. PYTHONPATH=. picks up the symlinked
#    examples/ tree.
PYTHONPATH=. $PY -m zicato.cli evolve --workspace .zicato \
    --rounds 2 --mode full \
    --harness-call-llm   examples.target_1_presentation.mocks:harness_llm \
    --auxiliary-call-llm examples.target_1_presentation.mocks:aux_llm

# 6. Close the epoch to produce analysis.md and analysis.html.
$PY -m zicato.cli epoch close --workspace .zicato
```

## What you should see

The `evolve` step emits a JSON array on stdout with one object per
round:

```json
[
  {
    "child_scalar": 1.0,
    "delta_scalar": 0.0,
    "parent_generation_id": "v0",
    "parent_scalar": 1.0,
    "proposed_generation_id": "v1",
    "rejection_reason": "insufficient margin: ...",
    "tournament_decision": "rejected"
  },
  {
    "child_scalar": 1.0,
    "delta_scalar": 0.0,
    "parent_generation_id": "v0",
    "parent_scalar": 1.0,
    "proposed_generation_id": "v2",
    "rejection_reason": "insufficient margin: ...",
    "tournament_decision": "rejected"
  }
]
```

Both rounds are rejected on purpose: the deterministic mock means
parent and child produce identical transcripts, so the tournament
gate fires "insufficient margin". The end-to-end plumbing — propose →
apply → snapshot → tournament → persist → journal — is exercised in
full; only the score delta is necessarily zero.

The mock `aux_llm` rotates the proposed patch across rounds:

* `v1` patches `researcher_instruction` (compact bullets).
* `v2` patches `coordinator_instruction` (sharper routing).

Both patches land in distinct generations and survive the post-apply
validator, so the snapshot diff is real even when the gate refuses
the promotion.

## Where the artifacts live

After step 6 the workspace looks like this:

```
.zicato/
  config.json                       # adapter entrypoint + mutable trees
  current_epoch                     # marker → t1_smoke epoch id
  lineage.json                      # cross-cutting DAG (1 epoch, 3 gens)
  epochs/
    2026-MM-DD_t1_smoke/
      board.jsonl                   # copy of examples/.../board.jsonl
      rubric.md                     # copy of examples/.../rubric.md
      scoring.json                  # copy of examples/.../scoring.json
      config.json                   # EpochConfig with closed=true
      current_generation            # marker → v0 (no promotion happened)
      journal.md                    # one entry per round (v1, v2)
      analysis.md                   # stub narrative + journal snapshot
      analysis.html                 # self-contained HTML companion
      generations/
        v0/
          gen_score.json            # cached aggregate for fast-mode reuse
          snapshot/agent/agent.py   # baseline copy of the registered tree
          runs/{entry_id}/events.jsonl
        v1/
          experiment.json
          gen_score.json
          patches/{patch_id}.json
          snapshot/agent/agent.py   # v0 + the researcher_instruction patch
          runs/{entry_id}/events.jsonl
        v2/
          experiment.json
          gen_score.json
          patches/{patch_id}.json
          snapshot/agent/agent.py   # v0 + the coordinator_instruction patch
          runs/{entry_id}/events.jsonl
```

Useful spot checks:

* `cat .zicato/lineage.json` — three generations (v0 promoted, v1 / v2
  rejected), one epoch.
* `cat .zicato/epochs/*/journal.md` — two markdown entries with
  per-round hypothesis and outcome.
* `python -c 'import json,sys; [print(json.dumps(json.load(open(p)), indent=2)) for p in sys.argv[1:]]' \
    .zicato/epochs/*/generations/v1/experiment.json` — the full
  proposed-experiment record with `outcome.tournament_decision`.
* `cat .zicato/epochs/*/generations/v1/patches/*.json` — the lifted
  Patch dataclass; the `mutation_id` is `researcher_instruction` for v1
  and `coordinator_instruction` for v2.
* Open `analysis.html` in a browser — the page is self-contained
  (inline CSS, no external requests) and renders the lineage / scalar
  trajectory.

## Limits of the smoke test

These are deliberate:

* **`harness_llm` returns prose, not JSON.** goldfive's `LLMPlanner`
  expects a planner-shaped JSON envelope; the mock returns slide-
  shaped prose. The planner falls back to its passthrough behaviour
  and emits the warnings you see on stderr ("JSON parse failed: ...").
  The downstream sinks still record `events.jsonl` so the reducer
  produces a `LossProfile` per entry — the values are uniformly zero,
  but the artifact is real.
* **Multi-turn entries abort with `TypeError`.** The scripted /
  emulated drivers expect a richer harness response than the mock
  produces; they record an aborted `RunResult` with the abort reason
  on the events stream. The reducer treats those as a zero-signal run
  and the tournament continues.
* **Both rounds are rejected.** The gate's `promote_margin` (`0.01`
  from `scoring.json`) means a child needs a strictly-better scalar to
  promote. The mock's deterministic outputs mean parent and child are
  byte-equivalent → zero delta → rejection. This is the correct
  behaviour, not a bug.
* **`analysis.md` is the stub form.** The CLI's `epoch close` does not
  thread a real auxiliary callable through; the close path writes the
  "_no auxiliary LLM was supplied_" stub plus the journal snapshot.
  The HTML companion is the full report shape and is informative on
  its own.

## Swapping in real LLMs

Two extension points:

1. **Replace the mocks.** Author your own
   `pkg.module:harness_call_llm` and `pkg.module:auxiliary_call_llm`
   conforming to `Callable[[str, str, str], Awaitable[str]]` and pass
   them via `--harness-call-llm` / `--auxiliary-call-llm`. Anything
   that returns the right text — a real model client, a local cache,
   a replay log — works the same way.

2. **Configure the auxiliary callable in the workspace.** Edit
   `.zicato/config.json` to add a `runtime` block:

   ```json
   "runtime": {
     "harness_call_llm":   "pkg.module:harness_call_llm",
     "auxiliary_call_llm": "pkg.module:aux_call_llm"
   }
   ```

   The orchestrator's runtime factory will import those dotted paths
   when the CLI does not pass an explicit override. The
   `--*-call-llm` flags still win when supplied — useful for one-off
   replays against a different model without touching config.

The auxiliary callable must NOT be the same Python object as the
harness callable; the runner enforces `is`-distinctness as a
collusion guard for multi-turn emulated entries.
