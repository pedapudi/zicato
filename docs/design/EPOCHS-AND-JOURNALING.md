# Epochs and journaling

Two intertwined concepts make zicato's history legible:

- **Epochs** group generations under a stable evaluation contract.
  Within an epoch, generation scores are directly comparable; across
  epochs they are not.
- **Experiment journaling** captures every round as a structured
  hypothesis written BEFORE the run plus an outcome block written
  AFTER. The hypothesis is mandatory; the journal is interpretable
  weeks later because of it.

Both decisions are load-bearing. Without epochs, generations
accumulate against shifting goalposts and lineage analysis is
meaningless. Without hypotheses, journals degenerate into "what
changed, what scored" and the most useful signal — what the proposer
was thinking and whether it was right — is gone.

## 1. Epoch concept

An **epoch** is the unit of evaluation contract. It owns:

- A frozen board (`board.jsonl`).
- A frozen rubric (`rubric.md`) — read fresh each round but the file's
  content is the operator's steering document for the duration.
- A frozen scoring configuration (`scoring.json`) — weights,
  tournament thresholds, tolerance bands.

Inside an epoch, generations are linearly ordered (`v0 → v1 → ... →
vN`). `v0` is the baseline — the inner harness as-registered. Each
subsequent generation is the result of a successful tournament:
either a candidate beat its parent (the candidate becomes `vN+1`) or
the parent held (no version bump; the next round proposes again
against the same parent).

**Pattern aggregates reset at epoch boundaries.** Drift counts from
epoch A do not flow into epoch B's pattern detection. The contract
changed; the past is no longer comparable.

### 1.1 What causes an epoch boundary

An operator starts a new epoch when any of the following hold:

- The board changes (entries added, removed, or edited).
- The rubric's `forbidden:` list changes — the mutation surface the
  proposer can act on is materially different.
- The scoring weights change (e.g. the operator decides pass-rate
  matters more relative to drift).
- The regression baseline rebases (a major refactor of the inner
  harness happened outside the loop and the parent `v0` of the next
  epoch is a fresh snapshot).

The CLI does not automate this — the operator's judgment is what
defines "material contract change". The CLI refuses board edits
mid-epoch without `--force` to make the boundary obvious.

### 1.2 What does NOT cause an epoch boundary

- Rubric text edits that don't change `forbidden:`. The rubric is
  *steering*, not contract. The proposer reads it fresh every round.
- Stylistic edits to the inner harness's source that don't add or
  remove mutation points.
- New `auxiliary_call_llm` model swaps. The model identity is
  configuration, not contract — though epoch boundary is a good time
  to swap if the operator wants to.
- Adding a tag to existing entries. Tags are advisory; pattern
  slicing changes, the entries themselves do not.

The bias is toward NOT starting a new epoch — the cost of throwing
away pattern history is significant, and most edits operators want to
make do not warrant it. The CLI surfaces this in the warnings on
`board add` and `board remove`.

## 2. Storage layout

The epoch is the major axis of the storage layout. Every artifact
that depends on the evaluation contract lives under the epoch
directory.

```
.zicato/
  config.json
  lineage.json                       # cross-epoch generation DAG
  epochs/
    initial/                         # default first epoch
      board.jsonl                    # frozen for this epoch
      rubric.md                      # operator-edited; read fresh each round
      scoring.json                   # weights + tournament thresholds
      generations/
        v0/
          snapshot/                  # inner-harness source at this generation
          patches_applied.json       # absent for v0
          experiment.json            # absent for v0 (the baseline)
          runs/
            {entry_id}/
              events.jsonl
              loss.json
          gen_score.json
        v1/
          snapshot/
          patches_applied.json
          experiment.json            # hypothesis + patches + outcome
          runs/
            {entry_id}/
              events.jsonl
              loss.json
          gen_score.json
        v2/
          ...
      patterns/
        round_001.json               # detector output, one per round
        round_002.json
        ...
      journal.md                     # running narrative across generations
      analysis.md                    # generated at epoch close
    epoch_after_board_edit/
      board.jsonl
      rubric.md
      scoring.json
      generations/
        v0/                          # baseline at this epoch's start
          snapshot/                  # the promoted last vN from `initial`
          ...
        v1/
          ...
      patterns/
      journal.md
      analysis.md
```

A few specifics:

- `v0` is **always** the baseline. In a fresh epoch its `snapshot/`
  is the promoted final generation from the previous epoch (or the
  initial-registered source for the first epoch).
- `experiment.json` is absent for `v0` and present for every
  subsequent generation in the epoch.
- `patches_applied.json` records what the applier actually wrote.
  It is a derivable from `experiment.json["patches"]` but is kept as
  a separate artifact for audit.

## 3. The Experiment

The proposer's output is NOT a bare `list[Patch]` — it is a typed
`Experiment` carrying both a hypothesis and the patches that test it.

### 3.1 Hypothesis schema (mandatory)

Every field is required. Schema-invalid proposer responses are
rejected; the proposer is re-prompted.

```json
{
  "hypothesis": {
    "core_idea": "Tighten the researcher's system prompt so it stops asserting facts without citing sources.",

    "modulating": [
      "researcher.instruction",
      "researcher.description"
    ],

    "why": "Pattern observed across rounds 3-5: DRIFT_KIND_CONFABULATION_RISK fires on 70% of entries tagged `[research]` and 0% on entries tagged `[summarise]`. The researcher's current instruction does not require source citations.",

    "expected_drift_movements": [
      {"kind": "CONFABULATION_RISK", "direction": "down", "magnitude": "moderate"},
      {"kind": "TOOL_ERROR", "direction": "up", "magnitude": "minor"}
    ],

    "expected_pass_rate_delta": {"low": 0.0, "high": 0.15},

    "risks": [
      "Tighter prompt may slow the researcher (more tool calls per turn).",
      "If sources are unavailable the researcher may refuse instead of approximating."
    ]
  },
  "patches": [
    {"mutation_point_id": "researcher.instruction", "new_text": "..."},
    {"mutation_point_id": "researcher.description", "new_text": "..."}
  ]
}
```

The fields in detail:

| Field | Type | Purpose |
|---|---|---|
| `core_idea` | `string` (one sentence) | What is being modulated, in plain language. The journal cites this. |
| `modulating` | `list[string]` | Mutation-point ids being touched. MUST be a non-empty subset of `mutation_points()`. |
| `why` | `string` | The pattern observation that motivated the change. Cites pattern ids when relevant. |
| `expected_drift_movements` | `list[{kind, direction, magnitude}]` | Per drift kind, predicted direction (`up`/`down`/`flat`) and magnitude (`minor`/`moderate`/`major`). |
| `expected_pass_rate_delta` | `{low: float, high: float}` | Predicted pass-rate band, e.g. `{0.0, 0.15}` for "no worse, up to 15 points better". |
| `risks` | `list[string]` | Plausible ways this could go wrong. |

The schema is enforced at proposer-output time. The proposer is given
the schema in its system prompt and the response is parsed as JSON;
malformed responses get one retry with an error message.

### 3.2 Patches schema

```json
[
  {
    "mutation_point_id": "<id from mutation_points()>",
    "new_text": "<the post-patch text>"
  },
  ...
]
```

The patch is referenced by id, not by file path. The applier resolves
the id to a location and rewrites it. See
[MUTATION-SURFACE.md](MUTATION-SURFACE.md) for validator constraints.

### 3.3 Outcome (written after the run)

When the tournament concludes, the tournament runner appends an
`outcome` block to the same `experiment.json` — atomic update, same
file.

```json
{
  "hypothesis": { ... },
  "patches": [ ... ],
  "outcome": {
    "drift_movements_actual": [
      {"kind": "CONFABULATION_RISK", "direction": "down", "magnitude": "moderate"},
      {"kind": "TOOL_ERROR", "direction": "flat", "magnitude": null}
    ],
    "hypothesis_match": [
      {"kind": "CONFABULATION_RISK", "matched": true},
      {"kind": "TOOL_ERROR", "matched": false, "note": "predicted minor up, actual flat"}
    ],
    "drift_loss_delta": -0.18,
    "pass_rate_delta": 0.05,
    "tournament_decision": "promote",
    "rejection_reason": null,
    "wall_clock_seconds": 412.3,
    "round_number": 4
  }
}
```

The fields:

| Field | Meaning |
|---|---|
| `drift_movements_actual` | Per-kind direction + magnitude from the tournament's loss deltas. |
| `hypothesis_match` | For every kind in `expected_drift_movements`, did actual match expected? |
| `drift_loss_delta` | Candidate's drift loss minus parent's, weighted by entry weight. |
| `pass_rate_delta` | Candidate's pass-rate minus parent's. |
| `tournament_decision` | `"promote"` or `"reject"`. |
| `rejection_reason` | Free-form reason if rejected (e.g. `"pass_rate_regression_on_summarise_short"`). Null if promoted. |
| `wall_clock_seconds` | Total wall-clock for the round (proposal + apply + tournament). |
| `round_number` | This round's position within the epoch (1-indexed; v0 is round 0). |

The `hypothesis_match` field is the load-bearing signal. Patches that
ship score deltas are common; patches whose proposer correctly
predicted the drift kinds are rarer and more valuable. Aggregating
hypothesis match-rate across rounds is what the analysis pass uses to
gauge whether the proposer is reasoning or guessing.

## 4. The journal (running)

`journal.md` is appended every round with a short, human-readable
rendering. Format:

```markdown
## Round 1 — v0 → v1   (promote)
- core_idea: Tighten the researcher's system prompt so it stops asserting facts without citing sources.
- drift_loss_delta: -0.18
- pass_rate_delta: +0.05
- hypothesis match: CONFABULATION_RISK ✓ | TOOL_ERROR ✗ (predicted up, flat)
- modulating: researcher.instruction, researcher.description

## Round 2 — v1 → (rejected)
- core_idea: Add a budget hint to the coordinator routing so it stops re-routing.
- drift_loss_delta: +0.02
- pass_rate_delta: -0.10 (regression on `summarise_short`)
- tournament_decision: reject — pass_rate_regression_on_summarise_short
- modulating: coordinator.routing

## Round 3 — v1 → v2   (promote)
...
```

`zicato journal show` renders this file. `zicato journal show --since
round=4` slices the file by round.

## 5. The analysis (per-epoch)

`analysis.md` is generated by an `auxiliary_call_llm` pass at epoch
close. The pass receives:

- The full `journal.md` for the epoch.
- The list of all `experiment.json` files (hypothesis + outcome).
- The `rubric.md` for the epoch.
- The aggregate pattern statistics across the epoch (drift kinds
  that moved most, kinds that stayed flat, tag slices with notable
  pass-rate movement).

The pass writes `analysis.md` with these sections:

```markdown
# Epoch analysis: <epoch_id>

## Headline movements
- ...
- ...

## Hypotheses that held
- Round 1: "Tighten researcher prompt for citation." CONFABULATION_RISK moved as predicted; pass-rate up 5 points.
- ...

## Hypotheses that didn't hold
- Round 4: "Soften coordinator routing." Predicted CAPABILITY_MISMATCH down, observed flat; pass-rate flat.
- ...

## Surface still open
- `writer.tools.summarize.description` has not been touched this epoch.
- ...

## Recommended focus for next epoch
- ...
```

The analysis pass is **bounded**. It receives a token budget on its
input (the journal can be long across many rounds) and produces a
fixed-section output. The schema is enforced at parse time; a malformed
analysis pass result triggers a regenerate.

### 5.1 Closing — manual primary, auto-close fallback

The operator closes an epoch with `zicato epoch close`. This:

1. Runs the analysis pass.
2. Writes `analysis.md`.
3. Marks the epoch's directory read-only (filesystem `chmod`,
   not strictly enforced — convention only).
4. Stamps `lineage.json` with the close timestamp.

If the operator starts a new epoch (`zicato epoch new`) without
closing the previous one, the CLI auto-closes the previous epoch with
a warning:

```
$ zicato epoch new hardened_research
WARNING: previous epoch `initial` was not closed manually; auto-closing now.
         analysis.md may be shorter / lower quality than a manual close.
         To avoid this in the future: zicato epoch close <name> before zicato epoch new.
Running analysis pass on `initial`...
Closed `initial`. Created `hardened_research`.
```

The auto-close runs the same analysis pass; the warning exists so
operators notice they missed the manual step (where they might have
added a `--focus` flag or otherwise steered the pass).

### 5.2 Why analysis at close, not continuously

Generating `analysis.md` is expensive (a multi-thousand-token LLM
call) and the output is most useful when the epoch is done. Within an
epoch, the per-round journal entry and the patterns aggregate are
enough. The analysis is the retrospective.

## 6. Lineage

`lineage.json` lives at `.zicato/lineage.json` (one file, all epochs)
and records the cross-cutting DAG:

```json
{
  "epochs": [
    {
      "id": "initial",
      "started_at": "2026-04-01T10:00:00Z",
      "closed_at": "2026-04-08T14:30:00Z",
      "v0_parent": null,
      "promoted_versions": ["v1", "v2", "v3", "v5", "v7"],
      "rejected_versions": ["v4", "v6"],
      "final_generation": "v7"
    },
    {
      "id": "hardened_research",
      "started_at": "2026-04-08T14:31:00Z",
      "closed_at": null,
      "v0_parent": "initial:v7",
      "promoted_versions": ["v1", "v2"],
      "rejected_versions": [],
      "final_generation": "v2"
    }
  ]
}
```

The DAG is shallow because epochs are linear and the v0 of a new
epoch points to the final version of its predecessor. The interesting
information is in the per-epoch lists: how many rounds promoted, how
many rejected, how the rejection rate evolved.

`zicato epoch list` renders `lineage.json` as a table:

```
epoch                started_at           closed_at            promoted  rejected  parent
-------------------  -------------------  -------------------  --------  --------  ----------
initial              2026-04-01 10:00     2026-04-08 14:30     5         2         (root)
hardened_research    2026-04-08 14:31     (open)               2         0         initial:v7
```

## 7. The rubric

`rubric.md` is the operator's steering document for an epoch. It is
markdown, no schema enforcement — the proposer reads it verbatim into
its system prompt each round.

A typical structure:

```markdown
# Rubric for epoch: hardened_research

## Focus
- Reduce CONFABULATION_RISK on entries tagged `[research]`.
- Investigate why the coordinator routes the researcher AFTER the
  writer on revision turns.

## Style
- Prefer terse, imperative instructions.
- Keep specialist descriptions to one sentence.

## Forbidden
- coordinator.routing
- writer.tools.summarize.description

## Notes
- The previous epoch tried tightening writer prompts and the result
  was flat. Steer away unless drift on writer entries gets worse.
```

The `## Forbidden` section is **enforced mechanically**: any patch
that targets a mutation-point id in this list is rejected at validate
time (constraint V5 in [MUTATION-SURFACE.md](MUTATION-SURFACE.md)).
Every other section is advisory — the proposer reads them as natural
language and uses them to steer.

The rubric is **read fresh every round**. There is no caching. The
operator can edit it between rounds and the next round picks up the
change.

### 7.1 Why edits mid-epoch are fine

Rubric edits are *steering*, not *contract*. The proposer can change
focus mid-epoch and the comparability of generations within the
epoch is preserved (every generation is still measured against the
same board and the same scoring). The exception is the `forbidden:`
list — adding ids to it shrinks the proposer's action space and
warrants a new epoch by convention; the CLI does not enforce this
but the convention is documented here so operators know.

## 8. Round mechanics

A single round, in storage terms:

1. Read patterns from `patterns/round_{NNN-1}.json` (if any).
2. Run the proposer; write `Experiment` to a temporary file.
3. Validate the experiment's hypothesis schema and patch ids.
4. Run the applier; write the candidate snapshot to `vN+1/snapshot/`.
5. Run the tournament; collect per-entry loss profiles for both
   parent and candidate.
6. Write `vN+1/gen_score.json` and the parent's updated
   `vN/gen_score.json` (if the parent's score changed under the
   freshly-run board).
7. Append the `outcome` block to `vN+1/experiment.json`.
8. Append a journal entry to `journal.md`.
9. Run the pattern detectors; write `patterns/round_{NNN}.json`.

Round numbers are global within an epoch (independent of whether the
round promoted). The 17th round is round 17 even if only 12 of those
landed promotions.

Two artifacts live outside the per-generation directory because they
aggregate across rounds:

- `patterns/round_NNN.json` — pattern detector output for the round.
- `journal.md` — running narrative.

## 9. Cross-references

| Topic | Document |
|---|---|
| Hypothesis schema, proposer contract | this document §3 |
| Patch shape and validator constraints | [MUTATION-SURFACE.md](MUTATION-SURFACE.md) |
| Loss profile written into each `runs/{id}/loss.json` | [TELEMETRY.md](TELEMETRY.md) |
| Drift loss scalar that drives `tournament_decision` | [SCORING.md](SCORING.md) |
| CLI commands for `epoch new` / `close` / `list` | [CLI.md](CLI.md) |
| Why mandatory structured hypothesis up front | [RATIONALE.md](RATIONALE.md) |
