# target_1_presentation — vendored presentation-agent example

This directory is the first dogfood target for zicato: a vendored copy
of an upstream multi-agent presentation tree (coordinator + research
+ web_developer + reviewer + debugger), annotated with mutation
markers, accompanied by a board, a rubric, predicates, and seed
scoring weights.

The intent of this example is to give the operator something concrete
to point zicato at end-to-end before zicato's runtime is fully wired:

- An importable agent module (`agent/`) the harness adapter can load.
- A board of seven entries (`board.jsonl`) covering single-turn,
  multi-turn-scripted, and multi-turn-emulated kinds.
- A set of pass/fail predicates (`predicates.py`) the board entries
  reference by dotted path.
- An initial operator rubric (`rubric.md`) the proposer reads each
  round.
- Scoring weights (`scoring.json`) that hydrate into
  `zicato.core.types.ScoringWeights`.

When the rest of zicato's runtime lands (the ADK adapter, the board
loader, the runner, the proposer, the tournament), this directory is
the input you point it at to see the whole loop end-to-end.

## Directory layout

```
target_1_presentation/
  README.md                 — this file
  agent/
    __init__.py             — re-exports root_agent + build_agent_tree
    agent.py                — the vendored tree, annotated with
                              # zicato:mutable markers
  board.jsonl               — 7 board entries
  rubric.md                 — operator rubric for epoch e0
  predicates.py             — pass/fail predicates referenced by entries
  scoring.json              — seed ScoringWeights (gauntlet, the default)
  scoring.racing.json       — same weights + a racing tournament block
                              (non-gauntlet structure; see RUN.md)
```

Two scoring contracts ship side by side. `scoring.json` carries no
`tournament` block, so it runs the default **gauntlet** (one challenger
per round). `scoring.racing.json` adds a `tournament` block selecting the
**racing** (successive-halving) structure — a four-challenger field that
the strategy races on escalating board slices before the survivor faces
the champion through the unchanged promote gate.

Both carry `per_judge_weights` for the declared inline judges
(`no_fabricated_numbers`, `incorporates_feedback`, `audience_appropriate`)
so those process judges actually MOVE the scalar when they fire. Together
with `mocks.harness_llm` now reading the mutated instruction — where only
the RESEARCHER's output carries the fabricated/cited marker, so a
researcher-only mutation is the sole lever over it — and `mocks.aux_llm`
now answering the REAL inline-judge runtime's `VIOLATION`/`OK` protocol
(not just a JSON `{"pass": bool}` shape), a researcher-instruction mutation
changes the output and is scored through the real judge runtime + reducer +
scoring — the contract can distinguish a challenger from its champion
(issue #84; before, every challenger tied and nothing could promote).
`tests/test_example_target_1_discriminates.py` proves this end to end
through the real judge runtime, reducer, and scoring (its end-to-end case
fails against the pre-fix mock, where the real inline judge never fires);
`RUN.md → "Why it now discriminates"` documents it and the remaining
live-stack (`LLMPlanner` passthrough) gap.
See
[`RUN.md` → "Running a non-gauntlet tournament"](./RUN.md) for the two run
recipes (point `evolve` at `scoring.racing.json`, or pass
`--tournament-structure racing` flags) and
`tests/test_example_target_1_racing.py` for the no-live-LLM test that runs
it end to end.

The vendored agent module is self-contained: it imports only from
`google.adk` (and only inside `build_agent_tree`, so the file imports
cleanly without ADK installed for static introspection tasks like the
mutation-audit CLI). No harmonograf telemetry, no goldfive runner glue
— those belong to upstream packages, not to a zicato target.

## Mutation surface

`agent/agent.py` exposes 9 distinct mutation ids, each annotated with
a `# zicato:mutable id="..." role="..."` comment immediately preceding
the editable span. The ids are:

| id | role | what it controls |
|---|---|---|
| `researcher_instruction` | `system_instruction` | research_agent's prompt — how it gathers facts for a topic |
| `web_developer_instruction` | `system_instruction` | web_developer_agent's prompt — how it lays out the deck |
| `reviewer_instruction` | `system_instruction` | reviewer_agent's prompt — what counts as a critical issue |
| `debugger_instruction` | `system_instruction` | debugger_agent's prompt — when to patch vs locate |
| `coordinator_instruction` | `coordinator_routing` | coordinator's prompt — the routing flow itself lives here |
| `write_webpage_tool_description` | `tool_description` | docstring on the `write_webpage` ADK FunctionTool |
| `read_presentation_files_tool_description` | `tool_description` | docstring on `read_presentation_files` |
| `find_presentation_files_tool_description` | `tool_description` | docstring on `find_presentation_files` |
| `patch_file_tool_description` | `tool_description` | docstring on `patch_file` |

The audit CLI (`zicato mutations`, see `docs/design/MUTATION-SURFACE.md`)
walks these markers and renders them in a table. Until that command is
implemented you can preview the surface with:

```bash
grep -n 'zicato:mutable' examples/zicato_examples/target_1_presentation/agent/agent.py
```

The proposer's `Patch` objects address these ids by their stable
string handle; the `id` survives across generations even when the
content's line range shifts.

## Board entries

`board.jsonl` ships 7 entries:

| id | kind | tags | weight | expectation |
|---|---|---|---|---|
| `waffles_single` | single_turn | smoke | 1.0 | predicate: `mentions_waffles` |
| `q3_metrics_outline` | single_turn | structure | 1.0 | predicate: `has_structured_outline` |
| `transformers_lay_audience` | single_turn | audience_nontech | 1.0 | predicate: `mentions_transformers` |
| `waffles_revision_scripted` | multi_turn_scripted | revision | 1.0 | — |
| `transformers_progressive_scripted` | multi_turn_scripted | progressive_detail | 1.0 | predicate: `stayed_coherent_across_turns` |
| `picky_stakeholder_emulated` | multi_turn_emulated | persona_picky | **1.5** | predicate: `addressed_picky_feedback` |
| `every_expectation_kind_demo` | single_turn | expectation_kinds | 0.5 | regex (with alts in `context`) |

Notes on individual entries:

- **`waffles_single`** — the canonical smoke test. Predicate is the
  weakest possible topical check (`"waffle"` appears anywhere in the
  final output). If this fails, the tree is broken end-to-end.
- **`q3_metrics_outline`** — exercises structure rather than content.
  The predicate asserts a numbered list or bullet list of >= 3 items.
- **`transformers_lay_audience`** — exercises the audience-adaptation
  axis. The predicate accepts the bare word "transformer" OR the
  jargon ("attention", "encoder", "decoder") since the entry asks
  for a non-ML-audience explanation.
- **`waffles_revision_scripted`** — three scripted user turns: ask
  for a deck, ask for an additional slide, reorder. No expectation —
  this entry contributes drift-loss signal only. The point of including
  it without a pass/fail predicate is to demonstrate that the board
  can carry pure drift-signal entries; the scoring math handles them
  via the `pass_fail=None` case.
- **`transformers_progressive_scripted`** — four scripted turns of
  progressive revision. Carries a `conversation_end` predicate that
  walks the whole transcript and asserts every assistant turn stayed
  topical.
- **`picky_stakeholder_emulated`** — the realistic case. The emulator
  plays a Q3-metrics stakeholder with a constraint set that pushes
  back, demands numbers, asks for revisions. The `stop_when` fires
  once the agent produces a revised deliverable that addresses the
  latest feedback round. Weight 1.5 — this entry dominates the
  scalar score, which is intentional: revision-handling is the most
  realistic signal of agent quality.
- **`every_expectation_kind_demo`** — a single-turn entry that uses
  the `regex` expectation kind directly, and enumerates the other
  OUTCOME expectation kinds (`predicate`, `expected_text`,
  `json_schema`, `rubric`) in its `context.alt_expectations_for_demo`
  field so future tooling can fan it out per kind without modifying
  `board.jsonl`. Weight 0.5 — this entry exists for the demo more than
  for scoring.

The board also exercises two parts of the typed board-authoring API
beyond plain OUTCOME expectations:

- A board-level `board_meta` header line carrying `disable_drift`
  (`user_steer`, `user_pause`) — drift kinds suppressed for every run
  on this board — and `judge_only: true`. In judge-only mode goldfive
  JUDGES the presentation agent (drift / process judges stay armed) but
  does ZERO steering: no goal-derivation LLM call, no planner
  replanning, no drift-triggered refine. The presentation agent is meant
  to be evaluated as-is, not actively steered, so this board opts in.
- PROCESS `judges` on `transformers_lay_audience` and
  `picky_stakeholder_emulated`. Where an `expectation` grades the
  finished output, a judge observes *how* the run unfolds and reports
  an adverse verdict at the configured `goldfive.DriftSeverity`.

## Running

This README is the place to ship concrete commands as the runtime
lands. Until the CLI is wired, the example is consumed two ways:

### 1. Static inspection

The mutation surface and the board can be inspected today without any
of zicato's runtime:

```bash
# Mutation surface preview
grep -n 'zicato:mutable' examples/zicato_examples/target_1_presentation/agent/agent.py

# Board entries
python -c "
import json
with open('examples/zicato_examples/target_1_presentation/board.jsonl') as f:
    for line in f:
        e = json.loads(line)
        print(e['kind'], e['id'], 'weight=', e.get('weight', 1.0))
"

# ScoringWeights round-trip
python -c "
import json
from zicato.core.types import ScoringWeights
with open('examples/zicato_examples/target_1_presentation/scoring.json') as f:
    print(ScoringWeights(**json.load(f)))
"
```

### 2. Tests

The example ships a small companion test at
`tests/test_example_target_1_presentation.py` that:

- Imports `zicato_examples.target_1_presentation.agent` and asserts that
  `root_agent` can be obtained (lazily, via `build_agent_tree(...)`
  with a mock model when ADK is available; otherwise the test
  exercises the module-level import only).
- Walks `agent/agent.py` for `# zicato:mutable` markers and asserts
  the unique-id count is >= 6.
- Lazy-imports `zicato.board.jsonl.load_board` and validates
  `board.jsonl` if the module exists; skips gracefully when the
  loader hasn't landed in this branch yet (it ships from a parallel
  branch and will arrive at integration time).

Run it with:

```bash
pytest tests/test_example_target_1_presentation.py -v
```

### 3. End-to-end (future)

Once the zicato CLI and runtime are wired, the canonical invocation
will be (working names):

```bash
# Create an epoch pinned to this directory's board / rubric / scoring
zicato epoch new e0 \
    --board examples/zicato_examples/target_1_presentation/board.jsonl \
    --rubric examples/zicato_examples/target_1_presentation/rubric.md \
    --scoring examples/zicato_examples/target_1_presentation/scoring.json \
    --target zicato_examples.target_1_presentation.agent

# Inspect the mutation surface the proposer will see
zicato mutations

# Run a single board entry under the seed generation
zicato run waffles_single --generation v0

# Kick off a tournament round
zicato propose
zicato tournament
```

The exact subcommands and flags are documented under
`docs/design/CLI.md`; this section will be updated once they freeze.

## Why vendor instead of cross-reference?

The upstream `presentation_agent_orchestrated` reference dynamically
loads its agent tree from goldfive's example by absolute file path.
That works fine in harmonograf's test layout but is the wrong shape
for a dogfood target: zicato must own the editable surface, and the
proposer must be able to patch a snapshot of it without coordinating
with two upstream repositories. Vendoring removes the dynamic
filesystem-path coupling and gives the patch applier a flat,
deterministic source tree to operate over.

The vendored tree is byte-equivalent at the instruction-string level
to the upstream agent at the time of vendoring (modulo formatting
inside Python string concatenation). If upstream evolves materially,
re-vendoring is a one-time operation; zicato's mutation ids remain
stable across the re-vendor because they are addressed by id, not by
line range or content hash.

## Extending the target

To add a new board entry: append a line to `board.jsonl`, validate it
with `zicato.core.types.validate_board_entry`, and (if it uses a
predicate expectation) add the predicate to `predicates.py`. The test
walks `board.jsonl` and re-validates every entry; CI will catch
malformed additions immediately.

To add a new mutation point: pick an id, drop a `# zicato:mutable
id="<id>" role="<role>"` comment immediately above the editable span,
and add a row to the mutation-surface table in this README. The test
walks the markers and asserts the count is >= 6; adding a new one
will not regress that floor.

To change the rubric without opening a new epoch: don't. Rubric
changes are an epoch boundary by design (see
`docs/design/EPOCHS-AND-JOURNALING.md`); make a copy under a new
epoch id instead.
