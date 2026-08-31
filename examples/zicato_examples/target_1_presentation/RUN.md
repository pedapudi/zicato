# target_1_presentation — running the loop end-to-end

This walkthrough takes a fresh checkout and runs `zicato evolve
--rounds 2` against the presentation agent with deterministic mock
models. It completes in a few seconds and produces the full artifact
tree: snapshots, `experiment.json`, patches, journal, analysis, and
lineage.

The walkthrough gives an operator something concrete to point at when
wiring real models. The mocks under [`mocks.py`](./mocks.py) are
byte-deterministic placeholders that exercise the plumbing; they produce
no meaningful improvement signal.

## Prerequisites

The walkthrough exercises the full orchestrator path, which imports
goldfive (for the inner-harness runner) and the agent development kit
(for the agent tree). Install the repository with its development
extras, which pulls in goldfive, the kit, and the `zicato-examples`
package that makes `zicato_examples.*` importable from anywhere:

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
# Pick a scratch workspace anywhere off the repo. Because zicato-examples
# is installed (make install), zicato_examples.* imports resolve with
# no symlink or PYTHONPATH hacks.
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

# 3. Open an epoch from the example's board / brief / scoring. epoch new
#    freezes a per-epoch copy AND publishes these files as the live
#    contract (here: /tmp/zicato-smoke-t1/board.jsonl, brief.md,
#    scoring.json) so the evolve in step 5 resolves the same contract.
$PY -m zicato.cli epoch new t1_smoke --workspace .zicato \
    --board   $EX/board.jsonl \
    --brief   $EX/rubric.md \
    --scoring $EX/scoring.json

# 4. Inspect the mutation surface the proposer will see (15 ids).
$PY -m zicato.cli inspect mutations --workspace .zicato

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
ZICATO=${ZICATO:?set ZICATO to your zicato checkout}
EX=$ZICATO/examples/zicato_examples/target_1_presentation
PY=$ZICATO/.venv/bin/python

# Steps 1-2 (init + register) are identical to the gauntlet recipe.
$PY -m zicato.cli init           --workspace .zicato
$PY -m zicato.cli epoch register --workspace .zicato \
    --adk agent.agent:root_agent \
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
epoch if it differs from the current one, in the same way that editing
`scoring.json` by hand would. Starting from the gauntlet `scoring.json`:

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

> The flag form needs no `board_ids`: it defaults to the epoch's full
> board, so the racing rungs slice the board without any ids listed. Pass
> `--tournament-param board_ids='["waffles_single", ...]'` to race on a
> *subset*; an explicit list overrides the default.

### The mock-harness test that runs this

`tests/test_example_target_1_racing.py` drives this contract end to end
with **no live model**. It loads `scoring.racing.json`, seeds a `v0`
snapshot from the `agent/` tree, uses the example's `mocks.aux_llm`
proposer, and asserts that the racing path executes: four challengers
proposed and applied, the rung ladder's cuts recorded, a champion
decision, and the persisted `ActiveTournament` envelope with its
per-challenger `OutcomeRecord` audit. Run it with `uv run pytest
tests/test_example_target_1_racing.py`.

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

The end-to-end plumbing — propose, apply, snapshot, tournament, persist,
journal — is exercised in full. The `delta_scalar: 0.0` seen through the
live goldfive and agent-kit stack comes from one cause: the `LLMPlanner`
passthrough gap described under "Limits of the smoke test" below. The
contract itself separates a challenger from its champion, as the next
section shows.

The mock `aux_llm` rotates the proposed patch across rounds:

* `v1` patches `researcher_instruction` (compact bullets / citations).
* `v2` patches `coordinator_instruction` (sharper routing).

Both patches land in distinct generations and survive the post-apply
validator, so the snapshot diff is real.

## How the contract separates a challenger from its champion

A contract that cannot tell a challenger from its champion reports every
round as a tie (`delta_scalar = 0.0`) while calling the loop healthy.
Three properties of this example keep that from happening.

1. **`harness_llm` reads the `system` prompt, and only the researcher
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
3. **The contract scores that drift.** `scoring.json` and
   `scoring.racing.json` carry `per_judge_weights` for the inline
   judges, so a champion whose output trips `no_fabricated_numbers`
   scores worse than the citation-demanding challenger by more than
   `promote_margin`.

`tests/test_example_target_1_discriminates.py` proves this end to end
with no live model and no agent-kit stack. Its load-bearing case,
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
            runs/{entry_id}/artifacts.json # generated inventory
            runs/{entry_id}/artifacts/     # captured presentation files
          v1/
            experiment.json
            gen_score.json
            patches/{patch_id}.json
            snapshot/agent/agent.py # v0 + the researcher_instruction patch
            runs/{entry_id}/events.jsonl
            runs/{entry_id}/artifacts.json
            runs/{entry_id}/artifacts/
          v2/
            experiment.json
            gen_score.json
            patches/{patch_id}.json
            snapshot/agent/agent.py # v0 + the coordinator_instruction patch
            runs/{entry_id}/events.jsonl
            runs/{entry_id}/artifacts.json
            runs/{entry_id}/artifacts/
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

Each of these is a known boundary of the mock stack, and each is stated
so a reader does not mistake it for a fault.

* **A live run still shows `delta_scalar = 0.0`, for one reason.** The
  deterministic harness, the inline judge runtime, the reducer and the
  scoring weights together already promote a researcher-instruction
  challenger — `tests/test_example_target_1_discriminates.py` proves it.
  The one path still open is the live goldfive and agent-kit stack: the
  harness's instruction-sensitive output has to reach `final_output`
  intact for the live planner to score the difference, and the
  `LLMPlanner` prose passthrough drops it. Closing that gap needs live
  endpoints and an operator go-ahead.
* **`harness_llm` returns prose rather than JSON.** goldfive's
  `LLMPlanner` expects a planner-shaped JSON envelope; the mock returns
  slide-shaped prose. The planner falls back to its passthrough
  behaviour and emits the `JSON parse failed: ...` warnings visible on
  stderr. The downstream sinks still record `events.jsonl`, so the
  reducer produces a `LossProfile` per entry — but the passthrough means
  the scored `final_output` does not carry the harness's
  instruction-sensitive text, and the live-stack delta stays zero. A
  mock harness that returns planner-shaped JSON would close it.
* **Multi-turn entries abort with `TypeError`.** The scripted and
  emulated drivers expect a richer harness response than the mock
  produces, so they record an aborted `RunResult` with the abort reason
  on the events stream. The reducer treats those as a zero-signal run
  and the tournament continues.
* **`analysis.md` is the stub form.** `epoch close` does not thread a
  real auxiliary callable through, so the close path writes the "_no
  auxiliary LLM was supplied_" stub plus the journal snapshot. The HTML
  companion carries the full report shape and is informative on its own.

## Swapping in real models

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
