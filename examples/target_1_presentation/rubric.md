# Epoch e0 — presentation agent baseline

This is the seed rubric for the presentation-agent dogfood target. It is the
operator-facing knob the proposer consults before each round; updating it
opens a new epoch (see `docs/design/EPOCHS-AND-JOURNALING.md`).

## Goal

Produce coherent, structured presentation outputs from the vendored multi-
agent tree in `agent/`. Specifically:

- Final outputs should describe a presentation in slide-shaped chunks (at
  least three "slide" markers in the response when the user asked for a
  deck).
- The agent should stay topical: a waffles prompt produces a waffles deck,
  not a tangent on raccoons. The upstream `_inject_raccoon_drift` callback
  is intentionally NOT vendored into zicato's copy, but if a future
  proposer reintroduces a drift hook the rubric still treats off-topic
  content as a regression.
- On revision turns, the agent should integrate the new instruction
  without losing the established structure. Wandering off-topic on turn
  3 is the dominant failure mode this epoch is trying to attack.

## Preferred edits

The proposer should prefer to touch these mutation points first:

- `researcher_instruction` — research_agent's system prompt
- `writer_instruction` (alias: `web_developer_instruction`) — the
  presentation-builder's system prompt
- `coordinator_instruction` — coordinator routing logic and stage flow

These three are where the bulk of the routing and content-quality
signal lives; tournament wins on epoch e0 are most likely to come from
tightening their instructions.

## Secondary edits

These are fair game but lower priority:

- `reviewer_instruction`, `debugger_instruction` — the failure-recovery
  half of the tree. Worth touching when pattern detectors flag a
  reviewer-loop or debugger-thrash regression.
- Tool descriptions (`write_webpage_tool_description`,
  `read_presentation_files_tool_description`,
  `find_presentation_files_tool_description`,
  `patch_file_tool_description`) — affect when the LLM elects to call
  each tool. Useful when the proposer suspects a tool-selection
  regression rather than a content regression.

## Forbidden edits

None for the baseline epoch.

(Reserved-list slot for future epochs: the operator may pin specific
mutation ids as off-limits once they have been "frozen" by a successful
generation. None are pinned yet at e0.)

## Style

When the proposer rewrites a span, follow these conventions:

- Specialist instructions should be terse and imperative. Cut hedging
  language; the production tree has a tendency to grow long
  instructions over time and the proposer should attack that.
- Tool descriptions should specify parameter semantics, not behavior.
  The behavior is in the Python function; the LLM reads the
  description to decide WHEN to invoke. Mixing the two makes both
  weaker.
- Preserve any double-backtick tool references (`` ``write_webpage`` ``,
  `` ``read_presentation_files`` ``, etc.) so the LLM can resolve them
  to tool names. The patch applier does NOT verify this; the proposer
  is expected to be careful.

## Notes on the board

The board (`board.jsonl`) carries seven entries:

- Three single-turn smoke tests (waffles, Q3 metrics, transformers).
- Two multi-turn-scripted entries — one shallow (waffles 3-turn revision),
  one deeper (transformers 4-turn progressive detail).
- One multi-turn-emulated entry with a picky-stakeholder persona on the
  Q3 metrics topic. Weight 1.5 — this entry is the most realistic
  signal of revision-handling quality.
- One "demo" entry exercising the regex expectation kind, with the
  other expectation kinds enumerated in its `context.alt_expectations_for_demo`
  field so future tooling can fan it out per kind without changing the
  board.

The proposer should consider the picky-stakeholder entry's weight when
deciding which mutation to attack — improvements there move the scalar
score more than improvements to the smoke tests.
