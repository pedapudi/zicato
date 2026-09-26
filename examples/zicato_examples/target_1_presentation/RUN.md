# target_1_presentation — running the loop end-to-end

This page gives the commands that point `zicato evolve` at the
presentation agent, the two tournament structures the example ships
contracts for, and what a run leaves on disk.

**A command-line run of this example is a live run.** The agent tree in
[`agent/agent.py`](./agent/agent.py) builds every agent on the model id
named by the `ZICATO_TARGET_1_MODEL` environment variable (its default
is a hosted model id written in that file). The agents declare tools,
so the ADK adapter keeps that function-calling model; it does not route
them through the `target` engine's text-only `call_llm`. A run
therefore sends the agent tree's requests to that model, needs its
credential, and spends budget. Without the credential, every run ends
in an `AuthenticationError` and scores nothing. Start one only with the
operator's go-ahead.

The mocks in [`mocks.py`](./mocks.py) are byte-deterministic
placeholders: `aux_llm` answers the evaluation role (the user emulator,
the inline judges, and the closing analysis). The offline coverage of
this example is the test suite.

## Offline: the tests

Seven test modules under `tests/` drive this example with no model; the
[README](./README.md#2-tests) lists what each one pins. Run them with:

```bash
uv run pytest tests/test_example_target_1_*.py
```

`tests/test_example_target_1_racing.py` runs the racing contract end to
end: a stub adapter stands in for the agent tree and returns canned
per-generation losses, and the test suite's stand-in for the Foe
proposal runtime writes the challengers.

## Prerequisites

The loop imports goldfive (for the system-under-test runner) and the
agent development kit (for the agent tree). Install the repository with
its development extras, which pulls in goldfive, the kit, and the
`zicato-examples` package that makes `zicato_examples.*` importable from
anywhere:

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
those three paths, which are recorded in `.zicato/config.json` under
`contract`. `epoch new` both freezes a per-epoch copy of those files
*and* publishes them to that canonical location, so the two stay in
agreement and `evolve` finds the contract whichever way you reach it.

The board, brief and scoring files referenced below live next to this
file, under `examples/zicato_examples/target_1_presentation/` in a
checkout.

```bash
# Pick a scratch workspace anywhere off the repo.
rm -rf /tmp/zicato-smoke-t1
mkdir -p /tmp/zicato-smoke-t1
cd /tmp/zicato-smoke-t1

# ZICATO is your zicato checkout; the two paths below derive from it.
ZICATO=${ZICATO:?set ZICATO to your zicato checkout}
EX=$ZICATO/examples/zicato_examples/target_1_presentation
PY=$ZICATO/.venv/bin/python

# 1. Bootstrap the workspace.
$PY -m zicato.cli init --workspace .zicato

# 2. Register the agent + the mutable source tree.
$PY -m zicato.cli epoch register --workspace .zicato \
    --adk agent.agent:root_agent \
    --mutable-tree $EX/agent

# 2b. Name what the model roles run on, and declare the proposal
#     runtime. `zicato init` writes a `proposer` block whose binary is
#     the placeholder /path/to/foe, which evolve refuses. The block
#     below is the test suite's Foe stand-in, which edits the tree
#     mechanically with no model; a real `proposer` block names a Foe
#     binary and a model (docs/design/PROPOSER.md).
PYTHONPATH=$ZICATO $PY - <<'PYEOF'
import json, pathlib
from tests._foe_support import stand_in_proposer_block
cfg_path = pathlib.Path(".zicato/config.json")
cfg = json.loads(cfg_path.read_text())
cfg["models"] = {
    "engines": {
        "target": {"call_llm": "zicato_examples.target_1_presentation.mocks:target_llm"},
        "evaluation": {"call_llm": "zicato_examples.target_1_presentation.mocks:aux_llm"},
    },
    "roles": {},
}
cfg["proposer"] = stand_in_proposer_block(pathlib.Path("foe").resolve())
cfg_path.write_text(json.dumps(cfg, indent=2) + "\n")
PYEOF

# 2c. The ADK adapter runs under Goldfive, and a Goldfive-enabled
#     contract must carry a `goldfive` object in scoring.json; an empty
#     one selects the fixed defaults (docs/design/GOLDFIVE-CONFIG.md).
#     Take a copy of the example scoring with that object present.
$PY - "$EX/scoring.json" ./scoring.t1.json <<'PYEOF'
import json, sys
scoring = json.load(open(sys.argv[1]))
scoring.setdefault("goldfive", {})
json.dump(scoring, open(sys.argv[2], "w"), indent=2)
PYEOF

# 3. Open an epoch from the example's board / brief and that scoring.
#    epoch new freezes a per-epoch copy AND publishes these files as the
#    live contract (here: /tmp/zicato-smoke-t1/board.jsonl, brief.md,
#    scoring.json) so the evolve in step 5 resolves the same contract.
$PY -m zicato.cli epoch new t1_smoke --workspace .zicato \
    --board   $EX/board.jsonl \
    --brief   $EX/rubric.md \
    --scoring ./scoring.t1.json

# 4. Inspect the mutation surface the proposer will see (15 ids).
$PY -m zicato.cli inspect mutations --workspace .zicato

# 5. Run two evolve rounds (live: the agent tree calls its model).
#    evolve resolves the contract published in step 3, so it continues
#    the t1_smoke epoch rather than rolling a new one.
$PY -m zicato.cli evolve --workspace .zicato \
    --rounds 2 --mode full

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
# After steps 1-2c above, with the contract files written next to the
# workspace ($EX as defined earlier):
cp $EX/board.jsonl    ./board.jsonl
cp $EX/rubric.md      ./brief.md
cp ./scoring.t1.json  ./scoring.json

# evolve sees no current epoch, resolves the contract from the three
# files above, and auto-opens a date-named epoch (for example
# 2026-09-26_e0) before running the loop.
$PY -m zicato.cli evolve --workspace .zicato \
    --rounds 2 --mode full
```

Editing any of those three files between `evolve` invocations changes
the evaluation contract; the next `evolve` detects the drift, closes
the current epoch, and opens a fresh one automatically.

## Running a racing tournament

Everything above runs the **gauntlet** — one challenger per round, one
full-board duel, king-of-the-hill — because the example's
`scoring.json` selects it in its `tournament` block. A contract with no
`tournament` block runs **racing**, the default. zicato supports
configurable per-epoch tournament structures: `gauntlet` and `racing`,
plus the experimental `single_elim`, `double_elim`, and `swiss` when
`scoring.json` sets `experimental.tournament_structures` to `true` (the
three example contracts for them, `scoring.single_elim.json`,
`scoring.double_elim.json`, and `scoring.swiss.json`, do). This example
ships a ready-made **racing** contract alongside the gauntlet one:
[`scoring.racing.json`](./scoring.racing.json).

**Racing** (successive halving / best-arm identification) is the one
bracket-shaped structure the selection design endorses for zicato's
few-expensive-noisy regime (see
[`docs/design/SELECTION.md`](../../../docs/design/SELECTION.md) §10 and
[`docs/design/TOURNAMENT-STRUCTURES.md`](../../../docs/design/TOURNAMENT-STRUCTURES.md)
§3.5). Per round it proposes a **field** of `field_size` challengers,
races them against the champion on a small board **slice** (a cheap
rung), eliminates the worst `1 − 1/eta` by score, re-races the survivors
on a larger slice, and repeats until one survivor remains. That survivor
then faces the champion on the *full* board through the unchanged
promote gate. Replication is intrinsic, because each rung is a larger
sample; this is where racing gets its robustness to noise without a
bracket's fragility.

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
> (the gauntlet's `field_size` is `1`). `board_ids` — which entries to
> slice over, and in what order — is **optional**: when the contract omits
> it the orchestrator uses the epoch's full board, which is why this
> example lists no ids. Pass an explicit `board_ids` to race on a *subset*
> of the board; an explicit list always overrides the default (see
> `zicato.selection.make_strategy` and
> `src/zicato/selection/strategies/racing.py`). With `field_size=4`,
> `eta=2` and `board_fraction=0.4` over this board's 7 entries: rung 0
> races 4 arms on 3 entries and keeps 2; rung 1 races those 2 on 6 entries
> and keeps 1; the survivor then meets the champion on all 7 entries
> through the promote gate.

The tournament structure is part of the frozen evaluation contract,
because it changes what a promotion means: a gauntlet champion and a
racing champion are selected under different rules. So **changing the
structure rolls the epoch** by contract hash, in the same way that
retuning `promote_margin` does.

There are two ways to run it.

### (a) Point `evolve` at the racing contract

Identical to the gauntlet recipe above, but resolve the contract from
`scoring.racing.json` instead of `scoring.json`:

```bash
# Steps 1-2b (init, register, models + proposer) are identical to the
# gauntlet recipe. In step 2c, copy $EX/scoring.racing.json instead of
# $EX/scoring.json into ./scoring.t1.json.

# Open the epoch from the RACING scoring contract.
$PY -m zicato.cli epoch new t1_racing --workspace .zicato \
    --board   $EX/board.jsonl \
    --brief   $EX/rubric.md \
    --scoring ./scoring.t1.json

# Evolve. The frozen contract carries structure=racing, so each round
# proposes a 4-challenger field and runs the rung ladder.
$PY -m zicato.cli evolve --workspace .zicato \
    --rounds 2 --mode full
```

### (b) Set the structure with CLI flags

`zicato evolve` can write the `tournament` block into the live
`scoring.json` for you. This is a **contract-mutating convenience**: the
written block participates in the contract hash, so it auto-rolls the
epoch if it differs from the current one, in the same way that editing
`scoring.json` by hand would. Starting from the gauntlet `scoring.json`:

```bash
$PY -m zicato.cli evolve --workspace .zicato \
    --rounds 2 --mode full \
    --tournament-structure racing \
    --tournament-param field_size=4 \
    --tournament-param eta=2 \
    --tournament-param board_fraction=0.4 \
    --tournament-param replicates=2
```

Each `--tournament-param KEY=VALUE` is repeatable; `VALUE` is parsed as
JSON when possible (so `field_size=4` becomes the integer `4`), else
taken as a string. A parameter edit preserves the other params, and
neither flag can be combined with `--epoch`.

> The flag form needs no `board_ids`: it defaults to the epoch's full
> board, so the racing rungs slice the board without any ids listed. Pass
> `--tournament-param board_ids='["waffles_single", ...]'` to race on a
> *subset*; an explicit list overrides the default.

## What a run prints

The `evolve` step emits a JSON array on stdout with one object per
round, carrying `parent_generation_id`, `proposed_generation_id`,
`tournament_decision`, `rejection_reason`, `parent_scalar`,
`child_scalar`, and `delta_scalar`. A rejected round names the gate
condition it failed in `rejection_reason`, for example
`insufficient improvement: loss fell by only ...; a promotion needs a
drop of at least 0.010000` when the challenger does not clear
`promote_margin`.

## How the contract separates a challenger from its champion

A contract that cannot tell a challenger from its champion reports every
round as a tie (`delta_scalar = 0.0`) while calling the loop healthy.
Three properties of this example keep that from happening.

1. **`target_llm` reads the `system` prompt, and only the researcher
   carries the marker.** The mutated researcher instruction changes the
   output: a baseline instruction lets the writer slip in an uncited,
   fabricated figure, and a citation-demanding challenger instruction
   replaces it with a cited one. Only the researcher's output carries
   this tail — the web_developer, reviewer, coordinator and debugger
   transcripts do not — so a researcher-only mutation is the sole lever
   over the judged marker. A coordinator or web_developer challenger
   cannot mask it by emitting the fabricated marker itself.
2. **The mock judge answers the real inline-judge protocol.**
   `aux_llm`'s judge branch answers both judge protocols: the JSON
   `{"pass": bool}` shape, and the one-line `VIOLATION` / `OK` contract
   that the inline-criterion judge runtime
   (`zicato.judge_runtime.builder._InlineCriterionJudge`) sends. It
   answers `VIOLATION` on the fabricated-figure marker and `OK` on cited
   output, so a declared `no_fabricated_numbers` judge — built through
   the production `judge_spec_to_goldfive` seam — emits a
   `custom:<name>` drift on a real run.
3. **The contract scores that drift.** Every scoring file in this
   directory carries `per_judge_weights` for the inline judges, so a
   champion whose output trips `no_fabricated_numbers` scores worse than
   the citation-demanding challenger by more than `promote_margin`.

`tests/test_example_target_1_discriminates.py` proves this end to end
with no live model and no agent-kit stack. Its central case,
`test_real_judge_runtime_discriminates_and_weight_is_load_bearing`,
drives the mock output through the inline-judge runtime
(`judge_spec_to_goldfive` plus `mocks.aux_llm`), then the reducer
(`reduce_loss` over a genuine goldfive `events.jsonl`, which attributes
the `custom:no_fabricated_numbers` drift), then the scoring aggregation
(`aggregate_generation_score`). It asserts a promotable `delta_scalar`
whose magnitude depends on the `no_fabricated_numbers` per-judge weight,
so a weight of zero fails the test. Carrying `per_judge_weights` in the
scoring contract rolls the epoch relative to a contract without them,
which is expected for an example.

## Where the artifacts live

After step 6 the scratch directory holds the live contract next to the
workspace, and the workspace holds one directory per epoch:

```
/tmp/zicato-smoke-t1/
  board.jsonl                       # live contract — published by epoch new
  brief.md                          # live contract — published by epoch new
  scoring.json                      # live contract — published by epoch new
  .zicato/
    config.json                     # adapter, models, proposer, and
                                    #   contract: paths to the three files above
    current_epoch                   # marker → the t1_smoke epoch id
    lineage.json                    # cross-cutting DAG of epochs and generations
    index.db                        # derived SQLite index (rebuildable)
    repo/                           # private git repository: one tag per
                                    #   generation source tree
    epochs/
      2026-MM-DD_t1_smoke/
        board.jsonl                 # frozen per-epoch copy of the board
        brief.md                    # frozen per-epoch copy of the brief
        scoring.json                # frozen per-epoch copy of the scoring
        config.json                 # the epoch's configuration
        analysis.md                 # closing analysis
        analysis.html               # self-contained HTML companion
        rounds/{n}/round_log.jsonl  # one durable event log per round
        episodes/                   # one directory per proposal episode
        health/round_{n}.json       # loop-health report per round
        generations/
          v0/
            gen_score.json          # cached aggregate for fast-mode reuse
            runs/{entry_id}/seed-none/
              events.<purpose>.r<n>.jsonl     # telemetry per measurement
              loss.<purpose>.r<n>.json        # reduced loss profile
              result.<purpose>.r<n>.json      # the run's result
              artifacts.<purpose>.r<n>.json   # generated file inventory
              artifacts.<purpose>.r<n>/       # captured presentation files
          v1/
            experiment.json         # hypothesis, patch ids, and outcome
            patches/{patch_id}.json # one record per applied patch
            gen_score.json
            runs/...
          v2/
            ...
```

`<purpose>` names why a measurement ran: `tournament` for a duel,
`calibration` for the noise-floor draws, and `contract_preflight` for
the check that the board can separate generations.

Useful spot checks:

* `$PY -m zicato.cli epoch list --workspace .zicato` — the lineage
  table: each epoch with its promoted and rejected counts.
* `git -C .zicato/repo tag` — one `epoch/<epoch_id>/<generation_id>`
  tag per generation.
* `cat .zicato/epochs/*/generations/v1/experiment.json` — the proposed
  experiment with its `hypothesis` and `outcome.tournament_decision`.
* `cat .zicato/epochs/*/generations/v1/patches/*.json` — the lifted
  `Patch` record: its `mutation_id`, `op`, and `new_content`.
* Open `analysis.html` in a browser — the page is self-contained
  (inline CSS, no external requests) and renders the lineage / scalar
  trajectory.

## Swapping in real models

Two extension points:

1. **Replace the mocks.** Author your own
   `pkg.module:target_call_llm` and `pkg.module:evaluation_call_llm`
   conforming to `Callable[[str, str, str], Awaitable[str]]` and name
   them as the `target` and `evaluation` engines' `call_llm` dotted
   paths. Anything that returns the right text — a real model client, a
   local cache, a replay log — works the same way. The agent tree itself
   runs on `ZICATO_TARGET_1_MODEL`, as the top of this page describes.

2. **Configure the evaluation callable in the workspace.** Edit
   `.zicato/config.json` to declare named engines:

   ```json
   "models": {
     "engines": {
       "target": {"call_llm": "pkg.module:target_call_llm"},
       "evaluation": {"call_llm": "pkg.module:aux_call_llm"}
     }
   }
   ```

   The orchestrator's runtime factory imports those dotted paths. A
   library caller entering through `evolve_n_rounds` may hand it
   resolved callables instead, which is the one override that skips the
   workspace configuration.

The evaluation callable must NOT be the same Python object as the
target callable; the runner enforces `is`-distinctness as a
collusion guard for multi-turn emulated entries.
