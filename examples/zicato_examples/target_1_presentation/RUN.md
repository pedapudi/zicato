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
the agent tree). Install the repo with its dev extra — this pulls in
goldfive, the ADK SDK, and the `zicato-examples` package (which makes
`zicato_examples.*` importable from anywhere, no symlinks needed):

```bash
make install     # uv sync --all-extras, from a repo checkout
```

`make install` installs both `zicato` and `zicato-examples` editable
into the environment. The example modules are then importable by
their dotted path (`zicato_examples.target_1_presentation.*`) without
any `PYTHONPATH` juggling.

## End-to-end loop

The evaluation contract — the board, the proposer brief, and the
scoring config — has one canonical home: the three files
`board.jsonl`, `brief.md`, and `scoring.json` sitting next to the
`.zicato/` directory. `zicato evolve` resolves the live contract from
exactly there (the paths are recorded in `.zicato/config.json` under
`contract`). `epoch new` both freezes a per-epoch copy of those files
*and* publishes them to that canonical location, so the two stay in
agreement and `evolve` finds the contract whichever way you reach it.

The board / brief / scoring files referenced below live next to this
RUN.md, under `examples/zicato_examples/target_1_presentation/` in a
repo checkout. Adjust the absolute paths for your machine.

```bash
# Pick a scratch workspace anywhere off the repo. Because zicato-examples
# is installed (make install), zicato_examples.* imports resolve with
# no symlink or PYTHONPATH hacks.
rm -rf /tmp/zicato-smoke-t1
mkdir -p /tmp/zicato-smoke-t1
cd /tmp/zicato-smoke-t1

EX=/home/sunil/git/zicato/examples/zicato_examples/target_1_presentation
PY=/home/sunil/git/zicato/.venv/bin/python

# 1. Bootstrap the workspace.
$PY -m zicato.cli init --workspace .zicato

# 2. Register the agent + the mutable source tree.
$PY -m zicato.cli register --workspace .zicato \
    --adk zicato_examples.target_1_presentation.agent.agent:root_agent \
    --mutable-tree $EX/agent

# 3. Open an epoch from the example's board / brief / scoring. epoch new
#    freezes a per-epoch copy AND publishes these files as the live
#    contract (here: /tmp/zicato-smoke-t1/board.jsonl, brief.md,
#    scoring.json) so the evolve in step 5 resolves the same contract.
$PY -m zicato.cli epoch new t1_smoke --workspace .zicato \
    --board   $EX/board.jsonl \
    --brief   $EX/rubric.md \
    --scoring $EX/scoring.json

# 4. Inspect the mutation surface the proposer will see (9 ids).
$PY -m zicato.cli mutations --workspace .zicato

# 5. Run two evolve rounds. evolve resolves the contract published in
#    step 3, so it continues the t1_smoke epoch rather than rolling a
#    new one.
$PY -m zicato.cli evolve --workspace .zicato \
    --rounds 2 --mode full \
    --harness-call-llm   zicato_examples.target_1_presentation.mocks:harness_llm \
    --auxiliary-call-llm zicato_examples.target_1_presentation.mocks:aux_llm

# 6. Close the epoch to produce analysis.md and analysis.html.
$PY -m zicato.cli epoch close --workspace .zicato
```

`evolve` also launches the live dashboard and prints its URL — for
example `Dashboard: http://127.0.0.1:7892`. The port is read back from
the dashboard service after it binds, so the printed URL always points
at the dashboard itself (the watchdog supervisor binds a separate
default port and never collides with it).

### The streamlined evolve-centric flow

`epoch new` is the explicit way to open an epoch. You do not have to
use it: `evolve` auto-opens (and later auto-rolls) epochs on its own.
The streamlined flow skips step 3 — instead, place the three contract
files at the canonical location yourself and let `evolve` open the
first epoch:

```bash
# After steps 1-2 above, with the contract files written next to the
# workspace ($EX as defined earlier):
cp $EX/board.jsonl  ./board.jsonl
cp $EX/rubric.md    ./brief.md
cp $EX/scoring.json ./scoring.json

# evolve sees no current epoch, resolves the contract from the three
# files above, and auto-opens epoch e0 before running the loop.
$PY -m zicato.cli evolve --workspace .zicato \
    --rounds 2 --mode full \
    --harness-call-llm   zicato_examples.target_1_presentation.mocks:harness_llm \
    --auxiliary-call-llm zicato_examples.target_1_presentation.mocks:aux_llm
```

Editing any of those three files between `evolve` invocations changes
the evaluation contract; the next `evolve` detects the drift, closes
the current epoch, and opens a fresh one automatically.

## Running a non-gauntlet tournament (racing)

Everything above runs the **gauntlet** — one challenger per round, one
full-board duel, king-of-the-hill (the default when `scoring.json`
carries no `tournament` block). zicato also supports configurable
per-epoch tournament structures: `gauntlet` (default), `single_elim`,
`double_elim`, `swiss`, and `racing`. This example ships a ready-made
**racing** contract alongside the gauntlet one:
[`scoring.racing.json`](./scoring.racing.json).

**Racing** (successive halving / best-arm identification) is the one
bracket-shaped structure the selection design endorses for zicato's
few-expensive-noisy regime (see
[`docs/design/SELECTION.md`](../../../docs/design/SELECTION.md) §10 and
[`docs/design/TOURNAMENT-STRUCTURES.md`](../../../docs/design/TOURNAMENT-STRUCTURES.md)
§3.5). Per round it proposes a **field** of `field_size` challengers,
races them against the champion on a small board **slice** (a cheap
rung), eliminates the worst `1 − 1/eta` by score, re-races the survivors
on a larger slice, and repeats until one survivor remains — which then
faces the champion on the *full* board through the unchanged promote
gate. Replication is intrinsic (each rung is a larger sample), which is
why racing earns its noise robustness without a bracket's fragility.

The racing block in `scoring.racing.json`:

```jsonc
"tournament": {
  "structure": "racing",
  "params": {
    "field_size": 4,          // challengers proposed per round
    "replicates": 2,          // paired runs per duel, averaged (noise lever)
    "eta": 2,                 // keep top 1/eta each rung (cut half)
    "board_fraction": 0.4,    // rung-0 board slice = ceil(0.4 * |board|)
    "rung0_board_size": 0     // 0 ⇒ derive rung-0 size from board_fraction
  }
}
```

> `field_size` is how many challengers the proposer must emit each round
> (the gauntlet's `field_size` is `1`). `board_ids` (which entries to
> slice over, and in what order) is **OPTIONAL**: the orchestrator defaults
> it to the epoch's full board when the contract omits it, so this example
> no longer lists the ids. Pass an explicit `board_ids` only to race on a
> *subset* of the board — an explicit list always overrides the default
> (see `zicato.selection.make_strategy` +
> `src/zicato/selection/strategies/racing.py`). With `field_size=4`,
> `eta=2`, and `board_fraction=0.4` over the example board's 7 entries:
> rung 0 races 4 arms on 3 entries
> and keeps 2; rung 1 races those 2 on 6 entries and keeps 1; then the
> survivor meets the champion on all 7 entries through the promote gate.

Because the tournament structure is part of the frozen evaluation
contract (it changes *what a promotion means* — a gauntlet champion and
a racing champion are selected under different rules), **changing the
structure rolls the epoch** by contract-hash, exactly as retuning
`promote_margin` does.

There are two ways to run it.

### (a) Point `evolve` at the racing contract

Identical to the gauntlet recipe above, but resolve the contract from
`scoring.racing.json` instead of `scoring.json`:

```bash
EX=/home/sunil/git/zicato/examples/zicato_examples/target_1_presentation
PY=/home/sunil/git/zicato/.venv/bin/python

# Steps 1-2 (init + register) are identical to the gauntlet recipe.
$PY -m zicato.cli init     --workspace .zicato
$PY -m zicato.cli register --workspace .zicato \
    --adk zicato_examples.target_1_presentation.agent.agent:root_agent \
    --mutable-tree $EX/agent

# Open the epoch from the RACING scoring contract.
$PY -m zicato.cli epoch new t1_racing --workspace .zicato \
    --board   $EX/board.jsonl \
    --brief   $EX/rubric.md \
    --scoring $EX/scoring.racing.json

# Evolve. The frozen contract carries structure=racing, so each round
# proposes a 4-challenger field and runs the rung ladder.
$PY -m zicato.cli evolve --workspace .zicato \
    --rounds 2 --mode full \
    --harness-call-llm   zicato_examples.target_1_presentation.mocks:harness_llm \
    --auxiliary-call-llm zicato_examples.target_1_presentation.mocks:aux_llm
```

### (b) Set the structure with CLI flags

`zicato evolve` can write the `tournament` block into the live
`scoring.json` for you. This is a **contract-mutating convenience**: the
written block participates in the contract hash, so it auto-rolls the
epoch if it differs from the current one (exactly equivalent to editing
`scoring.json` by hand). Starting from the gauntlet `scoring.json`:

```bash
$PY -m zicato.cli evolve --workspace .zicato \
    --rounds 2 --mode full \
    --tournament-structure racing \
    --tournament-param field_size=4 \
    --tournament-param eta=2 \
    --tournament-param board_fraction=0.4 \
    --tournament-param replicates=2 \
    --harness-call-llm   zicato_examples.target_1_presentation.mocks:harness_llm \
    --auxiliary-call-llm zicato_examples.target_1_presentation.mocks:aux_llm
```

Each `--tournament-param KEY=VALUE` is repeatable; `VALUE` is parsed as
JSON when possible (so `field_size=4` becomes the integer `4`), else
taken as a string. The flags are only applied when
`--tournament-structure` is also passed.

> Note: the flag form **just works** — `board_ids` defaults to the epoch's
> full board, so the racing rungs slice the board without listing any ids.
> Pass `--tournament-param board_ids='["waffles_single", ...]'` only to
> race on a *subset*; an explicit list overrides the default.

### The mock-harness test that runs this

`tests/test_example_target_1_racing.py` drives this exact contract end
to end with **no live LLM**: it loads `scoring.racing.json`, seeds a v0
snapshot from the real `agent/` tree, uses the example's
`mocks.aux_llm` proposer, and asserts the racing path executes — four
challengers proposed + applied, the rung ladder's cuts recorded, a
champion decision, and the persisted `ActiveTournament` envelope +
per-challenger `OutcomeRecord` audit. Run it with
`uv run pytest tests/test_example_target_1_racing.py`.

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

After step 6 the scratch directory looks like this:

```
/tmp/zicato-smoke-t1/
  board.jsonl                       # live contract — published by epoch new
  brief.md                          # live contract — published by epoch new
  scoring.json                      # live contract — published by epoch new
  .zicato/
    config.json                     # adapter entrypoint + mutable trees +
                                    #   contract: paths to the three files above
    current_epoch                   # marker → t1_smoke epoch id
    lineage.json                    # cross-cutting DAG (1 epoch, 3 gens)
    epochs/
      2026-MM-DD_t1_smoke/
        board.jsonl                 # frozen per-epoch copy of the board
        brief.md                    # frozen per-epoch copy of the brief
        scoring.json                # frozen per-epoch copy of the scoring
        config.json                 # EpochConfig with closed=true
        current_generation          # marker → v0 (no promotion happened)
        journal.md                  # one entry per round (v1, v2)
        analysis.md                 # stub narrative + journal snapshot
        analysis.html               # self-contained HTML companion
        generations/
          v0/
            gen_score.json          # cached aggregate for fast-mode reuse
            snapshot/agent/agent.py # baseline copy of the registered tree
            runs/{entry_id}/events.jsonl
          v1/
            experiment.json
            gen_score.json
            patches/{patch_id}.json
            snapshot/agent/agent.py # v0 + the researcher_instruction patch
            runs/{entry_id}/events.jsonl
          v2/
            experiment.json
            gen_score.json
            patches/{patch_id}.json
            snapshot/agent/agent.py # v0 + the coordinator_instruction patch
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
