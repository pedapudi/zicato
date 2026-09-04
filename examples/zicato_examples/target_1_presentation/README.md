# target_1_presentation — the presentation-agent example

A copy of a multi-agent presentation tree (coordinator, research,
web_developer, reviewer, debugger) taken into this repository and
annotated with mutation markers, with a board, a proposer brief,
predicates, and scoring weights beside it. Pointing `zicato evolve` at
this directory runs the whole loop; [`RUN.md`](./RUN.md) gives the
commands.

The pieces:

- An importable agent module (`agent/`) the harness adapter loads.
- A board of seven entries (`board.jsonl`) covering the single-turn,
  scripted multi-turn, and emulated multi-turn kinds.
- Pass/fail predicates (`predicates.py`) the board entries reference by
  dotted path.
- The proposer brief (`rubric.md`) the proposer reads each round.
- Scoring weights (`scoring.json`) that hydrate into
  `zicato.core.types.ScoringWeights`.

## Directory layout

```
target_1_presentation/
  README.md                 — this file
  agent/
    __init__.py             — re-exports root_agent + build_agent_tree
    agent.py                — the agent tree, annotated with
                              # zicato:mutable markers
  board.jsonl               — 7 board entries
  rubric.md                 — the proposer brief
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

**The contract separates a challenger from its champion.** Both scoring
files carry `per_judge_weights` for the declared inline judges
(`no_fabricated_numbers`, `incorporates_feedback`,
`audience_appropriate`), so a firing process judge moves the scalar.
Three facts make a researcher-instruction mutation visible in the score:

- `mocks.target_llm` reads the mutated instruction, and only the
  researcher's output carries the fabricated-versus-cited marker, so a
  researcher-only mutation is the sole lever over it.
- `mocks.aux_llm` answers the inline-judge runtime's `VIOLATION`/`OK`
  protocol, so the judge fires for real rather than through a stubbed
  `{"pass": bool}` shape.
- The mutated output is then scored through the judge runtime, the
  reducer, and the scoring weights.

`tests/test_example_target_1_discriminates.py` proves this end to end.
[`RUN.md`](./RUN.md) documents the mechanism and the remaining gap in
the live stack (the `LLMPlanner` passthrough), and gives two recipes for
running the racing structure — point `evolve` at `scoring.racing.json`,
or pass the `--tournament-structure racing` flags.
`tests/test_example_target_1_racing.py` runs it end to end with no live
model.

The agent module is self-contained. It imports only from `google.adk`,
and only inside `build_agent_tree`, so the file imports cleanly without
the agent development kit installed — which is what lets the
mutation-audit command introspect it statically. It carries no
harmonograf telemetry and no goldfive runner glue; those belong to the
packages that own them.

## Mutation surface

`agent/agent.py` exposes 15 distinct mutation ids. A
`# zicato:mutable id="..." role="..."` comment precedes each editable
string span; a `# zicato:mutable:code` comment opens each editable code
region and `# zicato:mutable:end` closes it. The ids are:

| id | role | what it controls |
|---|---|---|
| `researcher_instruction` | `system_instruction` | research_agent's prompt — how it gathers facts for a topic |
| `web_developer_instruction` | `system_instruction` | web_developer_agent's prompt — how it lays out the deck |
| `reviewer_instruction` | `system_instruction` | reviewer_agent's prompt — what counts as a critical issue |
| `debugger_instruction` | `system_instruction` | debugger_agent's prompt — when to patch and when to locate |
| `coordinator_instruction` | `coordinator_routing` | coordinator's prompt — the routing flow itself lives here |
| `coordinator_files_not_found_routing` | `coordinator_routing` | what the coordinator does when the reviewer reports `files_not_found` |
| `web_developer_topic_naming` | `topic_naming` | the `topic` string the developer passes to `write_webpage` |
| `reviewer_read_path` | `topic_naming` | the `topic` string the reviewer derives its read path from |
| `topic_slugify_logic` | `path_logic` | how a topic is normalized into a directory-name slug |
| `topic_output_dir_logic` | `path_logic` | how the absolute output directory is resolved from a topic |
| `find_presentation_match_logic` | `path_logic` | how `find_presentation_files` matches a topic to an existing directory |
| `write_webpage_tool_description` | `tool_description` | docstring on the `write_webpage` tool |
| `read_presentation_files_tool_description` | `tool_description` | docstring on `read_presentation_files` |
| `find_presentation_files_tool_description` | `tool_description` | docstring on `find_presentation_files` |
| `patch_file_tool_description` | `tool_description` | docstring on `patch_file` |

`zicato inspect mutations` walks these markers and renders them in a
table (see `docs/design/MUTATION-SURFACE.md`). To read the raw markers:

```bash
grep -n 'zicato:mutable' examples/zicato_examples/target_1_presentation/agent/agent.py
```

The proposer's `Patch` objects address these ids by their stable string
handle; the `id` survives across generations even when the content's
line range shifts.

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

### What the expectations read

The deliverable is the rendered webpage the agent writes through
`write_webpage`; its closing chat message is a report *about* that page.
The board grades the two separately:

- The **deliverable** predicates (`wrote_presentation_file`,
  `mentions_waffles`, `mentions_transformers`,
  `mentions_quarterly_metrics`, `has_slide_titles`,
  `has_structured_outline`, `avoids_offtopic_raccoons`) read the durable
  files named by `RunResult.artifacts`. Exactly one direct
  `output/<slug>/index.html` identifies the deck; history snapshots are
  ignored, and multiple direct decks fail as ambiguous. A run that wrote
  no deck fails regardless of what its reply claimed.
- The **conversation** predicates (`stayed_coherent_across_turns`,
  `addressed_picky_feedback`) read the transcript, because cross-turn
  memory and feedback handling live nowhere else.

Grading the reply for a deliverable property is blind in both
directions: a run that narrates slide titles without ever calling
`write_webpage` passes, and a run that writes a good deck and confirms
it in one terse line fails. The `final_output` a live run scores is a
short planner summary the agent does not author, so it carries almost
none of the deck's content.

No entry grades where under the output root the deck landed. The
write/read slug agreement is this board's designed difficulty, and the
`file_findability` judge already scores it as process drift. Grading it
here as well would count it twice, and would make a good deck invisible
for a naming reason.

The `regex`, `expected_text` and `json_schema` kinds match against
`final_output` by construction, so an entry that grades the artifact
must use the `predicate` kind. The worker discovers files only after the run;
the board does not declare their names. During grading the predicate receives
their sorted metadata and durable root through `RunResult.artifacts`, reads only
inventoried paths, and never consults ambient process state. The same tree
remains beside `loss.json` after the temporary run directory is gone.

Notes on individual entries:

- **`waffles_single`** — the canonical smoke test. The predicate asks
  for a usable deck on disk that is about waffles. If this fails, the
  tree is broken end-to-end.
- **`q3_metrics_outline`** — exercises structure rather than content.
  The predicate asserts the deck carries >= 3 slides or >= 3 list items.
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
  latest feedback round. Its weight of 1.5 makes it dominate the scalar
  score, because handling a revision is the most realistic signal of
  agent quality this board can read.
- **`every_expectation_kind_demo`** — a single-turn entry that uses
  the `regex` expectation kind directly, and enumerates the other
  OUTCOME expectation kinds (`predicate`, `expected_text`,
  `json_schema`, `rubric`) in its `context.alt_expectations_for_demo`
  field so future tooling can fan it out per kind without modifying
  `board.jsonl`. Weight 0.5 — this entry exists for the demo more than
  for scoring, which is why it is the one entry left grading the reply:
  the `regex` kind it demonstrates cannot reach the artifact.

The board also exercises two parts of the typed board-authoring API
beyond plain OUTCOME expectations:

- A board-level `board_meta` header line carrying `disable_drift`
  (`user_steer`, `user_pause`) and `judge_only: true`. `disable_drift`
  suppresses the *built-in judges mapped to* the named drift kinds; both
  kinds named here are user-interaction kinds with no mapped built-in
  judge, so on this board the header exercises the authoring API and
  round-trips through the contract without suppressing anything at run
  time. It suppresses judges rather than turning drift off; see
  [BOARD-FORMAT.md](../../../docs/design/BOARD-FORMAT.md). In judge-only
  mode goldfive judges the presentation agent — the drift and process
  judges stay armed — and steers it not at all: no goal-derivation model
  call, no planner replanning, no drift-triggered refine. This board opts
  in because the presentation agent is to be evaluated as it stands,
  without an outer loop steering it mid-run.
- PROCESS `judges` on `transformers_lay_audience` and
  `picky_stakeholder_emulated`. Where an `expectation` grades the
  finished output, a judge observes *how* the run unfolds and reports
  an adverse verdict at the configured `goldfive.DriftSeverity`.

## Running

[`RUN.md`](./RUN.md) carries the end-to-end recipes: the gauntlet loop,
the racing structure, what the run leaves on disk, and how to swap in
real models. This section covers the two ways to read the example
without running the loop.

### 1. Static inspection

The mutation surface and the board are readable without starting a
run:

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

Seven test modules cover this example, all under `tests/`:

| module | what it pins |
|---|---|
| `test_example_target_1_presentation.py` | the agent tree imports, the mutation markers parse and re-apply, every board entry validates, and the scoring weights hydrate |
| `test_example_target_1_predicates.py` | each predicate in `predicates.py` on hand-built inputs |
| `test_example_target_1_deck_predicates.py` | the deliverable predicates against a written deck |
| `test_example_target_1_file_findability.py` | the write/read slug agreement the `file_findability` judge scores |
| `test_example_target_1_discriminates.py` | that a researcher-instruction mutation moves the scalar through the real judge runtime |
| `test_example_target_1_racing.py` | the racing structure end to end with no live model |
| `test_example_target_1_measurement_mode.py` | that measurement mode is inert unless armed |

Run them with:

```bash
pytest tests/test_example_target_1_*.py -v
```

## Measurement mode — running the board as an instrument

`ZICATO_TARGET1_MEASUREMENT_MODE=1` (default off) changes what this
board is for. Off, it is a puzzle: the write/read slug mismatch is its
designed difficulty and the thing the proposer has to solve. On, it is
an instrument for comparing proposer *configurations* against each
other, where that difficulty is only noise between the arms:

- **Canonical deck dir.** `write_webpage`, `read_presentation_files`
  and `find_presentation_files` all resolve to one fixed
  `output/presentation/`, so no topic string can make a run
  unscoreable.
- **Enforced output contract + salvage.** The `web_developer` gets
  `output_schema=DECK_OUTPUT_SCHEMA`, and an `after_model_callback`
  writes the deck to the canonical dir the moment the developer
  responds — from the structured JSON, or from fenced
  ` ```html/```css/```js ` prose, or from a raw `<!DOCTYPE …></html>`
  span. It never clobbers a deck a real `write_webpage` call put there.
- **History snapshots.** An immutable `output/deck_history/turn_<n>/`
  copy per write, so "did turn N+1 keep what turn N built?" is
  answerable from the artifact rather than from the transcript.

The contract is an `output_schema` rather than ADK `mode=ANY`, because
`mode=ANY` cannot end its turn under a single-Runner overlay that drops
the tool's `escalate` action and so spins `write_webpage` unboundedly.

**Do not read a file-findability result out of a measurement-mode
run.** Salvage guarantees a deck exists however the pipeline failed, so
a scorer reading only the artifact cannot tell a working pipeline from a
broken one. The canonical directory compounds this: it makes the
reviewer's read succeed whatever slug it asks for. A run with the mode on leaves a
`MEASUREMENT_MODE` note in its output base saying so, so an
artifact tree read back later carries the caveat with it.

`tests/test_example_target_1_measurement_mode.py` pins that the mode is
inert when it is off: with the variable absent, no measurement artifact
reaches disk — neither the canonical directory, nor the history, nor
either marker. Only the string `1` arms the mode; any other value leaves
it off.

## Why the tree is copied in rather than referenced

The `presentation_agent_orchestrated` reference this tree came from
loads its agent tree from goldfive's example by absolute file path at
run time. That suits harmonograf's test layout, but a zicato target
needs the opposite: zicato owns the editable surface, and the proposer
patches a snapshot of it without coordinating with two other
repositories. Holding the copy here removes the filesystem-path coupling
and gives the patch applier a flat, deterministic source tree.

At the instruction-string level the copy matches the source agent as it
stood when it was taken, apart from formatting inside Python string
concatenation. Taking a fresh copy is a one-time operation; the mutation
ids survive it, because a patch addresses an id rather than a line range
or a content hash.

## Extending the target

To add a board entry: append a line to `board.jsonl`, validate it with
`zicato.core.types.validate_board_entry`, and add the predicate to
`predicates.py` if the entry uses a predicate expectation. The test
walks `board.jsonl` and re-validates every entry, so continuous
integration catches a malformed addition.

To add a mutation point: pick an id, put a `# zicato:mutable id="<id>"
role="<role>"` comment immediately above the editable span, and add a
row to the mutation-surface table above. The test walks the markers and
asserts a lower bound on the count, which a new marker cannot break.

Editing the proposer brief is an epoch boundary by design (see
`docs/design/EPOCHS-AND-JOURNALING.md`). To change it, open a new epoch
with the edited copy rather than editing the frozen one in place.
