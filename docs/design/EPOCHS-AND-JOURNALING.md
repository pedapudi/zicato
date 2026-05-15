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

Since the contract-hash auto-epoching feature landed (§10), the common
workflow is automated: `zicato evolve` detects a material contract
change and rolls the epoch for the operator. `zicato epoch new` /
`close` / `switch` remain as manual escape hatches. The list above is
still the authoritative definition of "what counts as a boundary" —
auto-epoching simply applies it mechanically via a hash.

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
          experiment.json            # absent for v0 (the baseline)
          runs/
            {entry_id}/
              events.jsonl
              loss.json
          gen_score.json
        v1/
          snapshot/
          experiment.json            # hypothesis + patch_ids + outcome
          patches/
            {patch_id}.json          # one file per patch (see §3.2)
          runs/
            {entry_id}/
              events.jsonl
              loss.json
          gen_score.json
        v2/
          ...
      current_generation             # marker: id of the promoted head
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
- Patches live in **separate `patches/{patch_id}.json` files** under
  each generation directory. The body of `experiment.json` carries
  `patch_ids: [...]` referencing them. See §3.2 for the rationale and
  the write order.

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
  "patch_ids": [
    "be4c8de0b5234ec4a8d8db4e8af3f8f0",
    "1f29c6a2e9e44ad99c4f55c9f7df0a3e"
  ]
}
```

The two patch objects themselves live in separate per-patch files —
see §3.2 below. The body of `experiment.json` only references them
by id. This keeps the `experiment.json` body small (operator-readable
in a terminal pager) and gives the operator one file per patch when
they want to inspect or hand-edit a specific change.

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

### 3.2 Patch storage (per-patch files)

Each patch is serialised to its own file under the generation
directory:

```
generations/v1/
  experiment.json              # body carries patch_ids: [...]
  patches/
    be4c8de0b5234ec4a8d8db4e8af3f8f0.json
    1f29c6a2e9e44ad99c4f55c9f7df0a3e.json
```

A patch file's payload:

```json
{
  "id": "be4c8de0b5234ec4a8d8db4e8af3f8f0",
  "mutation_id": "researcher.instruction",
  "op": "replace",
  "new_content": "...",
  "new_numeric": null,
  "new_enum": null,
  "rationale": "tighter wording to require citations"
}
```

**Write order.** Per-patch files are written FIRST; `experiment.json`
is written LAST. A partial write (crash between the two phases)
leaves orphan patch files behind, which are harmless — no reader
picks them up because `experiment.json`'s `patch_ids` list is the
authoritative source. Writing in the other order would leave a
dangling reference to a missing patch file, which IS harmful.

**In-memory shape.** `Experiment.patches` remains a
`tuple[Patch, ...]` regardless. Only the on-disk shape splits; every
write helper round-trips back to the same tuple of dataclasses on
read.

**Legacy inline form.** Workspaces produced before this layout
landed used an inline `patches: [{...}, ...]` array directly on
`experiment.json`. The read helper transparently accepts that old
shape for backward compatibility; new writes always use the
per-patch layout. The `zicato.epoch.migrate.migrate_inline_to_perpatch`
utility brings an old generation in line with the new layout when
the operator wants a clean conversion.

The patch is referenced by mutation id, not by file path. The
applier resolves the id to a location and rewrites it. See
[MUTATION-SURFACE.md](MUTATION-SURFACE.md) for validator constraints.

### 3.3 Outcome (written after the run)

When the tournament concludes, the tournament runner appends an
`outcome` block to the same `experiment.json` — atomic update, same
file.

```json
{
  "hypothesis": { ... },
  "patch_ids": [ ... ],
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

The per-patch files are NOT rewritten when the outcome lands —
`update_experiment_outcome` only re-writes `experiment.json`. The
patches are immutable once written.

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

### 5.2 Progressive `analysis.html`

`analysis.md` is generated only at epoch close (it requires the
LLM pass). `analysis.html` is different — it is regenerated
**after every generation**, deterministically, with no LLM call.

This gives the operator a current archival snapshot of the epoch
at any moment, even mid-flight:

| Property | `analysis.md` | `analysis.html` |
|---|---|---|
| Cadence | once, at epoch close | regenerated after every generation |
| Requires LLM? | yes (auxiliary pass) | no (deterministic render) |
| Persisted across `evolve` exits? | yes | yes |
| Contains the closing-pass narrative? | yes | only after close (final regeneration appends the LLM sections) |
| Suitable for `file://` opening mid-epoch? | not generated yet | yes |

The flow per generation:

```
generation v{N} promote/reject committed
            │
            ▼
orchestrator at safe point:
            │
            ▼
read every experiment.json in the epoch so far
            │
            ▼
call render_html_report(...)   # deterministic; no LLM
            │
            ▼
write analysis.html.tmp; atomic rename to analysis.html
            │
            ▼
broadcast SSE 'round_finished' to dashboard
            (if supervisor is running — see DASHBOARD.md)
```

#### Atomic write protocol

`analysis.html` uses the same atomic-rename pattern as every
state file (see [RUNTIME.md](RUNTIME.md) §6):

```python
def write_html_report_atomic(epoch_path: pathlib.Path, html: str) -> None:
    target = epoch_path / "analysis.html"
    tmp = target.with_suffix(".html.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        f.write(html)
        f.flush()
        os.fsync(f.fileno())
    tmp.rename(target)
```

This matters because operators frequently have `analysis.html`
open in a browser tab and reload it during long epochs. A partial
read (browser fetches mid-write) would render a broken HTML
document; the atomic rename guarantees readers always see either
the previous full document or the new full document, never a
half-written one.

#### Final regeneration at epoch close

At `zicato epoch close`:

1. The LLM analysis pass runs as before (§5 above), producing
   the narrative sections.
2. `render_html_report` runs once more, this time embedding the
   narrative sections in the dedicated `<section>` blocks.
3. The final `analysis.html` is written atomically.

A closed epoch's `analysis.html` has everything: full lineage,
per-generation experiment cards, score trajectory, drift
heatmap, AND the LLM narrative sections at the top. An open
epoch's `analysis.html` has everything except the LLM
narrative — placeholders where the narrative sections will go.

#### Relationship to the live dashboard

The dashboard ([DASHBOARD.md](DASHBOARD.md)) is the **live view**
of the in-flight epoch. `analysis.html` is the **archival
snapshot** that persists across `evolve` exits.

The two coexist intentionally:

- During an `evolve`, the operator opens the dashboard URL for
  the live view; `analysis.html` exists as the current snapshot
  but the dashboard supersedes it.
- Between `evolve` invocations (or after the epoch closes), the
  dashboard URL no longer works (supervisor has exited);
  `analysis.html` is opened directly via `file://` for the
  same data.
- For sharing — sending a link to a teammate, attaching to a
  ticket, archiving with the project — `analysis.html` is the
  shareable artifact. The dashboard is local-only by default.

The dashboard reads from `.zicato/runtime/*`; `analysis.html`
is regenerated from `.zicato/epochs/{id}/`. They consume
different sources and serve different roles, but the operator
sees roughly the same lineage and score trajectory in both
(rendered by the same shared component library).

### 5.3 Why LLM analysis at close, not continuously

Generating the LLM analysis pass that lives inside `analysis.md`
is expensive (a multi-thousand-token LLM call) and the output is
most useful when the epoch is done. Within an epoch, the
per-round journal entry, the patterns aggregate, the live
dashboard, and the deterministically-generated
`analysis.html` are enough. The LLM pass is the retrospective.

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

## 10. Contract-hash auto-epoching

Operators should never have to think about epoch management in the
common workflow. They edit the board, the rubric, or the scoring; they
run `zicato evolve`; the right thing happens. Contract-hash
auto-epoching is the mechanism that makes that true.

### 10.1 What's in the contract

The **evaluation contract** is exactly four things:

1. **The board** — test inputs + expectations (`board.jsonl`).
2. **The rubric** — operator steering text (`rubric.md`).
3. **The scoring** — weights + gate thresholds (`scoring.json`).
4. **The registered inner-harness IDENTITY** — the `--adk` entrypoint
   string plus the sorted list of `--mutable-tree` paths.

A change to any one of these means generations on either side are no
longer directly comparable, so the epoch must roll.

The inner harness's *source content* is deliberately **not** in the
contract — that source is exactly what zicato mutates within an epoch.
Only the harness *identity* (which agent, which trees) is contractual;
the bytes inside those trees are not.

### 10.2 Canonical contract paths

`zicato register` records the canonical contract source paths in
`.zicato/config.json` under a `contract` key. The default convention
(used when the operator does not override) is the operator's project
root, alongside the `.zicato/` directory:

- `<workspace_parent>/board.jsonl`
- `<workspace_parent>/rubric.md`
- `<workspace_parent>/scoring.json`

`register --board PATH` / `--rubric PATH` / `--scoring PATH` override
the default. These are the operator's *live, editable* copies. On epoch
creation / roll they are frozen (copied) into `epochs/{id}/`.

### 10.3 The contract hash

`zicato/epoch/contract.py` reduces the four contract components to a
single `sha256` hex digest, the **contract hash**. It is stored on the
epoch's `EpochConfig` (`contract_hash`) at creation time.

The hash is computed over a **canonicalized** form of each component,
so spurious edits do not roll the epoch:

| Component | Canonicalization |
|---|---|
| board | `load_board()`, sort entries by id, serialize each to a sorted-key JSON dict, join. Semantic content only — reordering rows or reformatting the JSONL is a no-op. |
| rubric | Read text, normalize line endings to `\n`, strip trailing whitespace per line, strip leading/trailing blank lines. CRLF churn and re-indentation are no-ops. |
| scoring | Parse into a fully-defaulted `ScoringWeights`, round every float to 6 decimal places, `json.dumps(sort_keys=True)`. Partial vs full documents and float-precision noise are no-ops. |
| entrypoint | The string verbatim. |
| mutable_trees | Sorted tuple of absolute path strings. Registration order is a no-op. |

The five canonical forms are concatenated and hashed. Missing files are
treated as the empty string for that component (so a board-less
workspace still hashes deterministically) — a warning is logged.

A whitespace-only rubric edit, a reordered board, or float noise in
`scoring.json` leaves the hash unchanged. A changed board input, a
changed weight, a different entrypoint, or an added mutable tree
changes it.

### 10.4 Roll-at-evolve-time semantics

The hash is checked **at evolve time**, once per `zicato evolve`
invocation, before the round loop starts:

1. Compute the current contract hash from the live contract files +
   the registered harness identity.
2. Look at the current epoch.
   - **No current epoch.** With auto-epoching on, `evolve` creates the
     first epoch (`e0`) from the contract and runs against it. With
     `--no-auto-epoch`, it errors and tells the operator to run
     `zicato epoch new`.
   - **Current epoch's hash matches** (or is empty — see §10.6).
     No roll; `evolve` runs against the current epoch.
   - **Current epoch's hash differs** (the contract drifted). With
     auto-epoching on, `evolve` closes the current epoch (generating
     `analysis.md`), opens a fresh one carrying the new contract, and
     runs against it. The roll prints a clear message naming which
     component changed:
     ```
     contract changed (rubric) — rolled 2026-05-15_e0 -> 2026-05-15_e1
     ```
     With `--no-auto-epoch`, it errors instead of rolling.

The resolved epoch is pinned for every round of the invocation — the
loop never re-rolls mid-flight. Passing `--epoch <id>` explicitly
**skips auto-epoching entirely**: an explicit target always wins.

### 10.5 Baselining a rolled epoch

When the auto-roll creates a new epoch, its `v0` baseline is the
**promoted head of the previous epoch** — the last promoted
generation's snapshot — not the originally-registered harness. This
continues the lineage: the new epoch starts from the best result of
the old one. If the previous epoch had no promoted generations beyond
`v0`, the new epoch seeds from the registered mutable trees as usual.

The cross-epoch link is recorded in `lineage.json` as the new epoch's
`v0_parent`, pointing back at the closed predecessor.

### 10.6 Legacy workspaces and the empty hash

`EpochConfig.contract_hash` defaults to the empty string. An epoch with
an empty `contract_hash` is an epoch created **before** auto-epoching
landed. Such epochs are treated as **always matching** — the
orchestrator never rolls a legacy workspace spuriously. The operator
keeps full manual control via `zicato epoch new` until they create
their first hash-carrying epoch.

### 10.7 Auto-epoch naming

Auto-created epochs are named `e{N}` where `N` is the count of existing
epochs — so `2026-05-15_e0`, `2026-05-15_e1`, and so on. `zicato epoch
new <name>` with an explicit name is unchanged. `zicato evolve
--epoch-name <name>` overrides the `e{N}` scheme for an epoch that
`evolve` auto-creates.

### 10.8 The escape hatches

`epoch new` / `close` / `switch` all keep working unchanged. They are
the manual escape hatches:

- `--no-auto-epoch` makes `evolve` strict: it errors on contract drift
  instead of rolling. Use this when you want to be told about drift
  and decide deliberately.
- `zicato epoch new` is still the way to start an epoch with a
  hand-chosen name, or to roll for a reason the hash cannot see (e.g.
  a regression-baseline rebase that did not touch any contract file).
- `zicato epoch switch` still re-points the current-epoch marker.

## 11. Cross-references

| Topic | Document |
|---|---|
| Hypothesis schema, proposer contract | this document §3 |
| Patch shape and validator constraints | [MUTATION-SURFACE.md](MUTATION-SURFACE.md) |
| Loss profile written into each `runs/{id}/loss.json` | [TELEMETRY.md](TELEMETRY.md) |
| Drift loss scalar that drives `tournament_decision` | [SCORING.md](SCORING.md) |
| CLI commands for `epoch new` / `close` / `list` | [CLI.md](CLI.md) |
| Atomic-rename helper used by `analysis.html` writes | [RUNTIME.md](RUNTIME.md) §6 |
| Live dashboard that supersedes `analysis.html` during an `evolve` | [DASHBOARD.md](DASHBOARD.md) |
| Git-backed storage that moves generation directories into a private repo | [STORAGE.md](STORAGE.md) |
| Why mandatory structured hypothesis up front | [RATIONALE.md](RATIONALE.md) |
