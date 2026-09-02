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
- A frozen proposer brief (`brief.md`) — read fresh each
  round but the file's content is the operator's steering document
  for the duration.
- A frozen scoring configuration (`scoring.json`) — weights,
  tournament thresholds, tolerance bands.
- A frozen evaluator identity — Zicato's explicit evaluator revision and any
  enabled integration identity that changes evaluation behavior.
- A frozen inner-harness identity — the validated adapter worker document,
  adapter implementation source outside the mutable trees, and the declared
  mutable-tree paths.
- A frozen **proposer** — the proposing agent's identity, its tools,
  and the skill modules under the configured `proposers/<name>/` dir
  (or the built-in default proposer when none is configured). See
  [PROPOSER.md](PROPOSER.md).

Inside an epoch, generations are linearly ordered (`v0 → v1 → ... →
vN`). `v0` is the baseline — the inner harness as-registered. Each
subsequent generation is the result of a successful tournament:
either a candidate beat its parent (the candidate becomes `vN+1`) or
the parent held (no version bump; the next round proposes again
against the same parent).

**Pattern aggregates reset at epoch boundaries.** Drift counts from
epoch A do not flow into epoch B's pattern detection. Counts gathered
under one contract are not comparable with counts gathered under
another.

### 1.1 What causes an epoch boundary

An operator starts a new epoch when any of the following hold:

- The board changes (entries added, removed, or edited — including a
  change to the board's `disable_drift` set).
- The proposer brief changes semantically, including a change to its
  `## Forbidden` mutation-point list.
- The scoring weights change (e.g. the operator decides pass-rate
  matters more relative to drift, or retunes `per_judge_weights`).
- The tournament structure changes (e.g. `gauntlet → swiss`, or a
  structure param like `swiss.rounds`) — see §9. Generations selected
  under different structures are not comparable.
- The adapter worker document changes. Examples include selecting another
  factory, changing its construction arguments or integrations, or changing an
  ADK entry point.
- Adapter implementation source outside the mutable trees changes. The source
  inside the mutable trees remains generation content and does not cause an
  epoch boundary.
- The registered mutable-tree path set changes.
- The **proposer** changes — a different proposer dir is registered, the
  proposer's custom `agent.py` (or declared identity / tools) is edited,
  or one of its `skills/*.md` modules is added, removed, or
  semantically changed. The agent that proposes the mutations is part of
  the contract, so generations proposed under different proposers are not
  comparable. See [PROPOSER.md](PROPOSER.md).
- The regression baseline rebases (a major refactor of the inner
  harness happened outside the loop and the parent `v0` of the next
  epoch is a fresh snapshot).

The common workflow is automated by contract-hash auto-epoching (§10):
`zicato evolve` detects a material contract change and rolls the epoch
for the operator. `zicato epoch new` / `close` / `switch` are the manual
escape hatches. The list above is the authoritative definition of what
counts as a boundary; auto-epoching applies it mechanically through a
hash.

### 1.2 What does NOT cause an epoch boundary

- Source edits inside a registered mutable tree. That source is generation
  content, including its mutation markers, and is the material Zicato evolves.
- An `auxiliary_call_llm` model swap. The model identity is
  configuration rather than contract, though an epoch boundary is a
  convenient moment to swap.

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
  config.json                        # adapter registration + mutable trees + contract paths
  current_epoch                      # marker: id of the current epoch
  lineage.json                       # cross-epoch generation DAG
  epochs/
    initial/                         # default first epoch
      board.jsonl                    # frozen for this epoch
      brief.md                       # operator-edited; read fresh each round
      scoring.json                   # weights + tournament thresholds
      config.json                    # EpochConfig (id, name, contract_hash, closed)
      mutations.json                 # most-recent mutation-point enumeration
      proposer_inputs.jsonl          # one line per proposer LLM call: its rendered input
      generations/
        v0/
          snapshot/                  # inner-harness source at this generation
          experiment.json            # synthetic seed marker (the baseline)
          gen_score.json
          runs/
            {entry_id}/
              events.jsonl
              loss.json
        v1/
          snapshot/
          experiment.json            # lineage coords + hypothesis + patch_ids + outcome
          patches/
            {patch_id}.json          # one file per patch (see §3.2)
          gen_score.json
          runs/
            {entry_id}/
              events.jsonl
              loss.json
        v2/
          ...
      current_generation             # marker: id of the promoted head
      patterns/
        round_001.json               # detector output, one per round
        round_002.json
        ...
      journal.md                     # running narrative across generations
      analysis.md                    # generated at epoch close (or a stub)
      analysis.html                  # deterministic render, refreshed each round
    epoch_after_board_edit/
      board.jsonl
      brief.md
      scoring.json
      config.json
      generations/
        v0/                          # baseline at this epoch's start
          snapshot/                  # the promoted last vN from `initial`
          ...
        v1/
          ...
      patterns/
      journal.md
      analysis.md
      analysis.html
```

(The proposer-brief filename is `brief.md`. When no `brief.md` sits
beside the `.zicato/` directory, the resolver falls back to `rubric.md`
in the same place, and a workspace `config.json` is read for the brief
path under either a `contract.brief_path` or a `contract.rubric_path`
key.)

A few specifics:

- `v0` is **always** the baseline. In a fresh epoch its `snapshot/`
  is the promoted final generation from the previous epoch (or the
  initial-registered source for the first epoch).
- `v0` carries a **synthetic seed `experiment.json`** (written by
  `write_seed_experiment`; `zicato repair v0-baseline` backfills it
  for older workspaces) so every generation directory has a uniform
  shape. Every subsequent generation carries a real proposer
  `experiment.json`.
- Patches live in **separate `patches/{patch_id}.json` files** under
  each generation directory. The body of `experiment.json` carries
  `patch_ids: [...]` referencing them. See §3.2 for the rationale and
  the write order.

## 3. The Experiment

The proposer's output is a typed `Experiment` carrying both a
hypothesis and the patches that test it, rather than a bare
`list[Patch]`.

### 3.1 Hypothesis schema (mandatory)

Every field is required. Schema-invalid proposer responses are
rejected; the proposer is re-prompted.

The proposer's raw response is a JSON object with two top-level keys,
`hypothesis` and `patches`; `zicato.proposer.structured` validates it
and lifts it into a typed `Experiment`. What lands on disk in
`experiment.json` is the serialized `Experiment`, whose body carries
lineage coordinates plus the hypothesis and a `patch_ids` list (see
§3.2 — the patches live in separate per-patch files). The hypothesis
sub-object:

```json
{
  "epoch_id": "2026-04-08_hardened_research",
  "generation_id": "v1",
  "parent_generation_id": "v0",
  "proposed_at": "2026-04-08T14:32:10Z",
  "hypothesis": {
    "core_idea": "Tighten the researcher's system prompt so it stops asserting facts without citing sources.",

    "modulating": [
      "researcher_instruction",
      "researcher_description"
    ],

    "why": "Pattern observed across rounds 3-5: confabulation_risk fires on 70% of entries tagged `[research]` and 0% on entries tagged `[summarise]`. The researcher's current instruction does not require source citations.",

    "expected_drift_movements": [
      {"kind": "confabulation_risk", "direction": "decrease", "magnitude": "medium"},
      {"kind": "tool_error", "direction": "increase", "magnitude": "small"}
    ],

    "expected_pass_rate_delta": "+0.00 to +0.15",

    "risks": "Tighter prompt may slow the researcher (more tool calls per turn); if sources are unavailable the researcher may refuse instead of approximating.",

    "expected_metric_movements": []
  },
  "patch_ids": [
    "be4c8de0b5234ec4a8d8db4e8af3f8f0",
    "1f29c6a2e9e44ad99c4f55c9f7df0a3e"
  ],
  "outcome": null
}
```

The patch objects themselves live in separate per-patch files — see
§3.2 below. The body of `experiment.json` only references them by id.
This keeps the body small (operator-readable in a terminal pager) and
gives the operator one file per patch when they want to inspect or
hand-edit a specific change.

The hypothesis fields in detail (the `HypothesisSpec` dataclass):

| Field | Type | Purpose |
|---|---|---|
| `core_idea` | `string` (one sentence) | What is being modulated, in plain language. The journal cites this. |
| `modulating` | `list[string]` | Mutation-point ids this hypothesis touches. Every id must resolve in the live mutation manifest; the proposer may list ids it is not patching this round, but all must exist. |
| `why` | `string` | The pattern observation that motivated the change. |
| `expected_drift_movements` | `list[{kind, direction, magnitude}]` | Per drift kind, predicted `direction` (`decrease` / `increase` / `neutral` / `decrease_or_neutral` / `increase_or_neutral`) and `magnitude` (`small` / `medium` / `large`). |
| `expected_pass_rate_delta` | `string` (free text) | Predicted pass-rate band as free text, e.g. `"+0.00 to +0.15"`. Free text rather than a typed range because the proposer expresses uncertainty differently per hypothesis. |
| `risks` | `string` (optional) | One-paragraph description of failure modes the proposer anticipates. Defaults to the empty string. |
| `expected_metric_movements` | `list[{metric_name, direction, magnitude}]` | Generalised predictions over any namespaced metric (`drift:`, `cost:`, `rubric:`, ...). At least one of `expected_drift_movements` / `expected_metric_movements` must be non-empty. |

The schema is enforced at proposer-output time (a JSON Schema pass
plus a cross-check pass in `zicato.proposer.structured`). The proposer
is given the schema in its system prompt; a malformed response is
rejected and re-prompted with the parse error appended.

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

**The two accepted read shapes.** The read helper accepts either an
`experiment.json` carrying `patch_ids` with a sibling `patches/`
directory, or one carrying an inline `patches: [{...}, ...]` array.
Some workspaces hold the inline shape on disk. Every write uses the
per-patch layout and stamps `format_version: 1` (see STORAGE.md §8);
there is no converter between the two, because the reader handles both.

The patch is referenced by mutation id rather than by file path. The
applier resolves the id to a location and rewrites it. See
[MUTATION-SURFACE.md](MUTATION-SURFACE.md) for validator constraints.

### 3.3 Outcome (written after the run)

When the tournament concludes, the tournament runner appends an
`outcome` block to the same `experiment.json` — atomic update, same
file.

```json
{
  "epoch_id": "2026-04-08_hardened_research",
  "generation_id": "v1",
  "parent_generation_id": "v0",
  "proposed_at": "2026-04-08T14:32:10Z",
  "hypothesis": { ... },
  "patch_ids": [ ... ],
  "outcome": {
    "ran_at": "2026-04-08T14:38:42Z",
    "drift_movements": [
      {"kind": "confabulation_risk", "from_rate": 0.70, "to_rate": 0.40, "hypothesis_match": true, "note": ""},
      {"kind": "tool_error", "from_rate": 0.10, "to_rate": 0.10, "hypothesis_match": false, "note": "predicted small increase, observed flat"}
    ],
    "pass_rate_delta": 0.05,
    "drift_loss_delta": -0.18,
    "scalar_score_delta": 0.12,
    "tournament_decision": "promoted",
    "rejection_reason": "",
    "metric_movements": []
  }
}
```

The per-patch files are NOT rewritten when the outcome lands —
`update_experiment_outcome` only re-writes `experiment.json`. The
patches are immutable once written.

The fields (the `OutcomeRecord` dataclass):

| Field | Meaning |
|---|---|
| `ran_at` | ISO-8601 UTC timestamp when the experiment finished evaluating. |
| `drift_movements` | Per-kind realized movements. Each carries `from_rate` (parent per-run mean), `to_rate` (child per-run mean), `hypothesis_match` (whether the realized movement matched the prediction within the magnitude bucket), and an optional `note`. |
| `pass_rate_delta` | Candidate's board-wide pass-rate minus parent's. Range `[-1.0, 1.0]`. |
| `drift_loss_delta` | Change in mean drift loss across the board. Negative = improvement. |
| `scalar_score_delta` | Change in the combined tournament scalar; its sign gates `tournament_decision`. |
| `tournament_decision` | `"promoted"`, `"rejected"`, or `"deferred"`. |
| `rejection_reason` | Symbolic reason when rejected (e.g. `"insufficient margin: ..."`); empty string otherwise. |
| `metric_movements` | Realized movements over any namespaced metric — the generalised superset of `drift_movements`. |

The per-movement `hypothesis_match` flag is the load-bearing signal.
Patches that
ship score deltas are common; patches whose proposer correctly
predicted the drift kinds are rarer and more valuable. Aggregating
hypothesis match-rate across rounds is what the analysis pass uses to
gauge whether the proposer is reasoning or guessing.

### 3.4 The outcome feeds back into proposing (experiment memory)

The `outcome` block is not only a backward-looking audit trail. Once it
is written — and dual-written into the analytical index's `experiments`
table (`tournament_decision`, `rejection_reason`, `scalar_score_delta`)
— it becomes an input to the *next* round's proposer. A capped, curated
digest of prior experiments (each one's `core_idea`, its `modulating`
ids, its verdict, and its Δscalar) is surfaced to the proposer in the
`## What's already been tried` prompt section, so it stops re-proposing
known failures and can build on known wins. The proposer's
own pre-run hypothesis (§3.1) is what makes this digest legible: the
record that began as "what the proposer was thinking" closes the loop as
"what the proposer should remember it already tried".

The digest is advisory context rather than a constraint: it never
enters the hard hypothesis schema, and the only mechanical gate on the
proposer stays the brief's `## Forbidden` list (§7). It is scoped to the current
evaluation contract (one epoch = one contract), because a Δscalar from a
different board is not comparable. The full design — the two scopes
(settled cross-round history plus intra-round sibling awareness in a
multi-challenger field), the curation, and the contract scoping — is in
[EXPERIMENT-MEMORY.md](EXPERIMENT-MEMORY.md).

## 4. The journal (running)

`journal.md` is appended one section per experiment with a short,
human-readable rendering. The tournament runner re-renders the same
section once the outcome is populated, so the proposal appears first
and the verdict follows. Canonical format (one section per generation,
headed by its version label and the one-line `core_idea`):

```markdown
## v1 — Tighten the researcher's system prompt so it stops asserting facts without citing sources.

**proposed_at**: 2026-04-08T14:32:10Z
**modulating**: researcher_instruction, researcher_description
**why**: Pattern observed across rounds 3-5: confabulation_risk fires on 70% of entries tagged `[research]`.
**outcome**: promoted (Δscalar=+0.120, Δdrift_loss=-0.180, Δpass_rate=+0.050)

## v2 — Add a budget hint to the coordinator routing so it stops re-routing.

**proposed_at**: 2026-04-08T14:41:55Z
**modulating**: coordinator_instruction
**why**: Coordinator re-routes the writer on revision turns.
**outcome**: rejected (Δscalar=-0.020, Δdrift_loss=+0.020, Δpass_rate=-0.100)
**rejection_reason**: pass_rate_regression_on_summarise_short
```

The journal is plain markdown, not JSONL, so the append is a single
text write (a crash mid-write leaves the prior journal intact rather
than a truncated record). There is no `zicato journal show` command —
the file is meant to be read directly with `cat` / `less`, or rendered
via the dashboard and `analysis.html`.

## 5. The analysis (per-epoch)

`analysis.md` is generated by an `auxiliary_call_llm` pass at epoch
close — **only when an auxiliary LLM has been configured**. When no
auxiliary callable is available (e.g. `zicato epoch close` run by hand
without one wired through), the close path writes a deterministic stub
`analysis.md` — the journal snapshot plus a `_no auxiliary LLM was
supplied_` placeholder — that the operator can later re-render with
`zicato repair report`. The LLM pass receives:

- The full `journal.md` for the epoch.
- The list of all `experiment.json` files (hypothesis + outcome).
- The `brief.md` for the epoch.
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

The operator closes an epoch with `zicato epoch close [EPOCH_ID]`
(the current epoch when `EPOCH_ID` is omitted). This:

1. Runs the analysis pass (best-effort — only if an auxiliary LLM is
   configured; otherwise writes the stub described in §5 above).
2. Writes `analysis.md` (and re-renders `analysis.html`).
3. Stamps the epoch's `config.json` as closed and records the close
   timestamp in `lineage.json`.

To re-render an existing epoch's report against the current on-disk
data, use `zicato repair report` (deterministic figures/tables
always; `--no-llm` skips the prose pass). `zicato inspect telemetry`
(re)runs the decision-telemetry analyzer for an epoch out of band.

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

### 5.2 The epoch report and its HTML companion

Each epoch directory holds one report in two forms: `analysis.md`, the
markdown source, and `analysis.html`, a self-contained document with no
external CSS, JavaScript, or images, so it renders the same over `file://`
as it does through the dashboard.

Both are refreshed after every settled round, deterministically and with
no LLM call: the data-bearing sections are re-templated from the current
workspace data while any prose the auxiliary LLM has already written is
preserved verbatim, and mid-epoch the masthead carries a `LIVING DRAFT —
through round N` stamp. The refresh is digest-gated — a round that moved
no data rewrites neither file. At close the prose pass runs (§5 above),
the full document is written, and the draft stamp goes away.

`analysis.html` is `render_report_html` (`src/zicato/analyzer/report.py`)
applied to the `analysis.md` beside it, whichever pass wrote that
markdown. One renderer serves every lifecycle phase, so the document an
operator has open does not change shape when the epoch closes, and the
HTML cannot disagree with the markdown it accompanies. The figures —
lineage, score trajectory, hypothesis against outcome, drift-kind
movements, per-board outcomes — are inline SVG drawn from the same
structured view the tables are templated from, so a chart and the table
beside it cannot disagree either.

This gives the operator a current archival snapshot of the epoch at any
moment, even mid-flight:

| Property | `analysis.md` | `analysis.html` |
|---|---|---|
| Cadence | refreshed after every settled round; full render at close | re-rendered from `analysis.md` on every refresh |
| Requires LLM? | for the prose sections, at close | no |
| Persisted across `evolve` exits? | yes | yes |
| Carries the closing-pass narrative? | after close | after close |
| Suitable for `file://` opening mid-epoch? | yes | yes |

#### Relationship to the live dashboard

The dashboard ([DASHBOARD.md](DASHBOARD.md)) is the **live view**
of the in-flight epoch. `analysis.html` is the **archival
snapshot** that persists across `evolve` exits.

The two coexist intentionally:

- During an `evolve`, the operator opens the dashboard URL for
  the live view; `analysis.html` exists as the current snapshot
  but the dashboard supersedes it.
- Between `evolve` invocations (or after the epoch closes), the
  dashboard URL does not answer, because the supervisor has exited;
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

### 5.3 Why the LLM analysis runs at close rather than continuously

Generating the LLM analysis pass that lives inside `analysis.md`
is expensive (a multi-thousand-token LLM call) and the output is
most useful when the epoch is done. Within an epoch, the
per-round journal entry, the patterns aggregate, the live
dashboard, and the deterministically-generated
`analysis.html` are enough. The LLM pass is the retrospective.

## 6. Lineage

`lineage.json` lives at `.zicato/lineage.json` (one file, all epochs)
and records how generations descend from one another across epoch
boundaries, as a directed acyclic graph (DAG).

`zicato init` scaffolds the file as an empty graph in the same
epoch-keyed shape the loader reads:

```json
{"epochs": []}
```

Once `evolve` registers epochs and lands generations, the lineage
mutators (`zicato.epoch.lineage`) populate it — a top-level `epochs`
list, each epoch carrying its `generations`:

```json
{
  "epochs": [
    {
      "id": "initial",
      "name": "initial",
      "started_at": "2026-04-01T10:00:00Z",
      "closed_at": "2026-04-08T14:30:00Z",
      "v0_parent": null,
      "generations": [
        {"id": "v0", "parent_id": null, "promoted": true,  "created_at": "2026-04-01T10:00:00Z",
         "round_index": 0, "rejection_reason": "",
         "parent_scalar": null, "child_scalar": null, "delta_scalar": null},
        {"id": "v1", "parent_id": "v0", "promoted": true,  "created_at": "2026-04-01T10:12:00Z",
         "round_index": 1, "rejection_reason": "",
         "parent_scalar": 0.7188, "child_scalar": 0.7601, "delta_scalar": 0.0413},
        {"id": "v2", "parent_id": "v1", "promoted": false, "created_at": "2026-04-01T10:31:00Z",
         "round_index": 2,
         "rejection_reason": "insufficient improvement: 0.7328 vs 0.7601 (margin 0.0200)",
         "parent_scalar": 0.7601, "child_scalar": 0.7328, "delta_scalar": -0.0273}
      ]
    },
    {
      "id": "hardened_research",
      "name": "hardened_research",
      "started_at": "2026-04-08T14:31:00Z",
      "closed_at": "",
      "v0_parent": "initial:v7",
      "generations": [
        {"id": "v0", "parent_id": "initial:v7", "promoted": true, "created_at": "2026-04-08T14:31:00Z"}
      ]
    }
  ]
}
```

Each generation row carries its `parent_id` — the generation it was
forked from, written as the cross-epoch `epoch:gen` form or as `null`
for a root `v0`. An absent parent is `null` and never the empty string,
so a lineage walker can distinguish a root from a generation whose
parent is literally named with an empty id. Each row also carries a `promoted`
flag (`true` / `false` / `null` while the generation is still in
flight) and the `round_index` that minted it. The settle-time write adds
the gate's `rejection_reason` plus the duel's `parent_scalar` /
`child_scalar` / `delta_scalar`. The reason is non-empty **only** on a
settled rejection: an empty reason means promoted or pending
everywhere else in the system, and the DAG must not disagree. The
scalars are `null` when unrecorded, never `0.0` — zero is a legal
measurement. The DAG is shallow because epochs are linear and the `v0` of a
new epoch points (via `v0_parent`) to the final version of its
predecessor. Per-epoch promotion/rejection counts are derived from the
`generations` list's `promoted` flags rather than stored as separate
fields.

`zicato epoch list` renders `lineage.json` as a table:

```
epoch                started_at           closed_at            promoted  rejected  parent
-------------------  -------------------  -------------------  --------  --------  ----------
initial              2026-04-01 10:00     2026-04-08 14:30     5         2         (root)
hardened_research    2026-04-08 14:31     (open)               2         0         initial:v7
```

## 7. The proposer brief

`brief.md` is the operator's steering document for an epoch
— the operator's brief *to the proposer* for how to rewrite the
inner harness. It is markdown, no schema enforcement: the proposer
reads it verbatim into its system prompt each round.

> **Naming note.** Two separate objects are easy to confuse. The
> **proposer brief** is this epoch-wide steering document. A **rubric**
> is the per-entry `Rubric.score()` outcome check that grades one board
> entry's output (see [BOARD-AUTHORING.md](BOARD-AUTHORING.md) §2.2).
> The brief steers the proposer for a whole epoch; a rubric grades one
> entry. A workspace whose steering file is named `rubric.md` still
> resolves as the brief, and `config.json` still accepts the brief path
> under a `contract.rubric_path` key.

A typical structure:

```markdown
# Proposer brief — epoch: hardened_research

## Focus
- Reduce CONFABULATION_RISK on entries tagged `[research]`.
- Investigate why the coordinator routes the researcher AFTER the
  writer on revision turns.
- Look at why the custom judge `cite-before-metric` fires on
  revision turns specifically.

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

The `## Forbidden` section — the **forbidden-id list** — is **enforced
mechanically**: any patch that targets a mutation-point id in this
list is rejected at validate time by `check_forbidden_ids` (see
[MUTATION-SURFACE.md](MUTATION-SURFACE.md) §6). Every other section is
advisory — the proposer reads them as natural language and uses them
to steer.

The proposer brief is **read fresh every round**. There is no
caching. The operator can edit it between rounds and the next round
picks up the change.

### 7.1 Why edits mid-epoch are fine

Proposer-brief edits are *steering*, not *contract*. The proposer can
change focus mid-epoch and the comparability of generations within
the epoch is preserved (every generation is still measured against
the same board and the same scoring). The exception is the
`## Forbidden` list — adding ids to it shrinks the proposer's action
space and warrants a new epoch by convention; the CLI does not
enforce this but the convention is documented here so operators know.

## 8. Round mechanics

"Round" here means the **outer evolve round** — one `zicato evolve
--rounds N` meta-loop step, one tournament, one crowning. (The richer
structures of §9 play several *inner* rounds — swiss rounds, bracket
rounds, racing rungs — within a single outer round; the two senses are
disambiguated in §9.3.) The steps below describe one *gauntlet* outer
round.

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

Each generation records its **birth round** as a 0-based `round_index`,
persisted into `experiment.json` (`Experiment.round_index`, mirrored from
`Generation.round_index`) so the dashboard's round-timeline / champion-spine
views can attribute a generation to the outer round that minted it. It
defaults to `0` for the seed `v0` and for records that predate the stamp,
and it is always the OUTER evolve round — never an inner bracket round.

Two artifacts live outside the per-generation directory because they
aggregate across rounds:

- `patterns/round_NNN.json` — pattern detector output for the round.
- `journal.md` — running narrative.

## 9. Per-epoch tournament structure

> **Status.** SHIPPED. The five structures (`gauntlet` default,
> `single_elim`, `double_elim`, `swiss`, `racing`) are implemented as
> pluggable selection strategies under `zicato/selection/`, driven by
> `zicato/selection/driver.py:resolve_tournament` and selected per-epoch
> from the scoring `tournament` block. `gauntlet` is the king-of-the-hill
> default; an epoch with no `tournament` key gets it byte-for-byte. The
> full data model (persisted record, dashboard API, CLI) lives in
> [TOURNAMENT-DATA-MODEL.md](TOURNAMENT-DATA-MODEL.md); the selection
> algorithms live in [SELECTION.md](SELECTION.md) /
> [TOURNAMENT.md](TOURNAMENT.md).

### 9.1 The `tournament` block

Each epoch chooses one tournament **structure** — how the crowning
decision is made over the round's candidate field. The choice is a
contract property (a gauntlet champion and a Swiss champion are not
comparable), so it lives in `scoring.json` under a `tournament` key:

```jsonc
{
  // ... weights + gate thresholds ...
  "tournament": {
    "structure": "gauntlet",   // gauntlet|single_elim|double_elim|swiss|racing
    "params": { }               // structure-specific; defaults fill in
  }
}
```

- `structure` — one of five closed tokens; `gauntlet` is the default
  (an epoch with no `tournament` key gets the shipped king-of-the-hill
  gauntlet, byte-for-byte).
- `params` — a structure-specific JSON object the selection logic reads
  (e.g. `swiss.rounds`, `racing.rungs`). See
  [TOURNAMENT-DATA-MODEL.md](TOURNAMENT-DATA-MODEL.md) §1.3 for the
  per-structure params and their defaults.

It is modeled as a frozen field on `ScoringWeights` (a
`TournamentStructure` dataclass), so it is frozen for the epoch's
lifetime in the same way as the weights.

### 9.2 Contract-hash interaction (it rolls the epoch)

Because the `tournament` block lives inside `scoring.json`, it factors
into the contract hash through the **existing** scoring canonicalizer —
`_scoring_to_canon` already serializes every public `ScoringWeights`
field. **Changing the structure or any param therefore rolls the
epoch.** Switching `gauntlet → swiss`, or bumping `swiss.rounds` from 4
to 6, changes the scoring component's canonical form and so changes the
hash. The auto-roll path of §10 then closes the current epoch and opens
a fresh one, as a `promote_margin` retune does. The roll message names
the changed component as `scoring`, because the structure is part of
scoring; there is no separate component label for it.

This is the desired behaviour: it keeps the invariant that all
generations within one epoch were selected under one comparable
structure.

### 9.3 How rounds / matchups journal under each structure

The §8 round mechanics describe one *gauntlet* round (one champion, one
challenger). Under a richer structure a single `zicato evolve` round can
play several matches, but the journaling seams are unchanged — they
**generalize additively**:

- **`experiment.json` `outcome`** — still written once per generation,
  still carrying `tournament_decision` (the crowning verdict for *that*
  generation: did it become / stay champion). It carries additive fields
  — `structure`, `final_rank`, `eliminated_in_round`, and a
  per-generation `match_record` of the matches that generation played —
  plus the runtime `champion_eval_mode` provenance (§9.4). Old journals
  deserialize unchanged (every added field defaults to the gauntlet /
  `full` interpretation).
- **`journal.md`** — still one human-readable section per experiment.
  For a non-gauntlet structure the section additionally renders the
  generation's rank / elimination round, but the
  `## vN — <core_idea>` + outcome-line shape is preserved.
- **Per-match detail** — each match is two (or, for a racing rung, N)
  board runs under two (or N) generations, persisted under the usual
  `generations/{id}/runs/{entry_id}/` layout. No new per-match
  directory: a match is reconstructable from the per-run `loss.json`
  files keyed on `(generation_id, entry_id)`, plus the structure's
  `rounds` state carried on the live `ActiveTournament` and the settled
  `tournaments` index row.

The full persisted shapes (the generalized `ActiveTournament`,
`OutcomeRecord`, and `tournaments` table, with their back-compat
defaults) are specified in
[TOURNAMENT-DATA-MODEL.md](TOURNAMENT-DATA-MODEL.md) §2 and
[STORAGE.md](STORAGE.md) §5.

#### The two senses of "round" (disambiguation)

"Round" is overloaded and the two senses are routinely confused:

- The **outer evolve round** — one `zicato evolve --rounds N` meta-loop
  step. One tournament is played per outer round, ending in one crowning.
  Outer round numbers are global within the epoch (§8). This is the round
  the `--rounds` flag counts and the round that the per-generation
  `round_index` stamp records (§8 / `Experiment.round_index`).
- A tournament's **inner rounds** — the scheduling steps *inside* a single
  tournament for the non-gauntlet structures: swiss rounds, single/double-
  elim **bracket** rounds, and racing **rungs**. These are carried as
  `Matchup.round_index` / `Round.round_index` in `zicato/selection/` and
  do **not** advance the outer `--rounds` counter.

A gauntlet has exactly one inner round (champion vs one challenger), so its
outer and inner round coincide — which is why the two are easy to conflate.
A swiss / elim / racing tournament plays several inner rounds within ONE
outer evolve round, over a multi-challenger field.

### 9.4 `champion_eval_mode` — runtime champion-eval provenance

`OutcomeRecord.champion_eval_mode` records how the **champion** side was
evaluated for the round under the `evolve --mode` knob. It is RUNTIME
provenance only — it carries no weight in the gate and is **not** folded
into the contract hash (flipping `fast`↔`full` does not roll the epoch);
it exists purely so the journal can attribute champion sample freshness +
cost per round.

| Value | Meaning |
|---|---|
| `full` | The champion was run **live** this round (`--mode full` bypasses the unit cache and re-samples every board unit; or fast was not applicable). |
| `fast` | The champion's cached per-board scalars were **reused** and the champion was NOT executed. `--mode fast` (the default) is cache-first: every `(generation, entry, replicate)` board unit is evaluated at most once and reused across pairings/rounds/structures; only cache misses run. |
| `fast-degraded` | Fast was requested but no cache covered the needed boards (the seed/first champion, or a not-yet-covered subset), so the champion ran live **once** to seed the cache. |

Reader's caveat: when `champion_eval_mode` is `fast`, the champion's per-run
numbers were reused rather than freshly sampled. A flat champion trajectory
then records that the champion was not re-run; it says nothing about whether
the champion's behaviour held steady. Re-run with `--mode full` for a clean re-sample.

## 10. Contract-hash auto-epoching

Operators should never have to think about epoch management in the
common workflow. They edit the board, the proposer brief, or the
scoring; they run `zicato evolve`; the right thing happens.
Contract-hash auto-epoching is the mechanism that makes that true.

### 10.1 What's in the contract

The **evaluation contract** has six semantic components:

1. **The board** — test inputs, `expectations`, `judges`, and the
   board's `disable_drift` set (`board.jsonl`).
2. **The proposer brief** — operator steering text
   (`brief.md`).
3. **The scoring** — weights + gate thresholds, **and the per-epoch
   tournament structure** (`scoring.json`; see §9 for the tournament
   block and [TOURNAMENT-DATA-MODEL.md](TOURNAMENT-DATA-MODEL.md) for the
   full data model).
4. **The Zicato evaluator implementation** — an explicit revision of
   measurement and tournament-decision semantics.
5. **The registered inner-harness identity** — the validated worker
   reconstruction document, implementation source outside the mutable surface,
   and the sorted mutable-tree paths.
6. **The proposer** — the proposing agent's identity, its tools, and the
   skill modules under the configured `proposers/<name>/` dir (or the
   built-in default proposer when none is registered). See
   [PROPOSER.md](PROPOSER.md). Note the *proposer brief* (item 2) and the
   *proposer* (item 6) are distinct contract inputs: the brief is
   per-epoch operator steering text, the proposer is the agent (plus its
   skills) that consumes it.

A change to any one of these means generations on either side are no
longer directly comparable, so the epoch must roll.

The inner harness's source content inside the registered mutable trees is not
in the contract. That source is what Zicato mutates within an epoch. The
contract instead records how a worker reconstructs the harness, the immutable
adapter implementation that drives it, and which source trees may change.

### 10.2 Canonical contract paths

`zicato epoch register` records the canonical contract source paths in
`.zicato/config.json` under a `contract` key. The default convention
(used when the operator does not override) is the operator's project
root, alongside the `.zicato/` directory:

- `<workspace_parent>/board.jsonl`
- `<workspace_parent>/brief.md`
- `<workspace_parent>/scoring.json`

`register --board PATH` / `--brief PATH` / `--scoring PATH`
override the default. These are the operator's *live, editable*
copies. On epoch creation / roll they are frozen (copied) into
`epochs/{id}/`.

`register --proposer-path PATH` additionally records
`contract.proposer_path` — the proposer dir whose skills + optional
custom `agent.py` are folded into the hash. An absent flag leaves the
key unset (the built-in default proposer). See [PROPOSER.md](PROPOSER.md).

### 10.3 The contract hash

`zicato/epoch/contract.py` reduces the contract components to a
single `sha256` hex digest, the **contract hash**. It is stored on the
epoch's `EpochConfig` (`contract_hash`) at creation time.

The hash is computed over a **canonicalized** form of each component,
so spurious edits do not roll the epoch:

| Component | Canonicalization |
|---|---|
| board | `load_board()`, sort entries by id, serialize each to a sorted-key JSON dict (including its `expectations` and `judges`), join; the board's `disable_drift` set sorts into the same canonical form — as the sorted, de-duplicated **kind set**, so changing *which* kinds are disabled rolls the epoch while reordering them does not (an empty set canonicalizes to `false`, the historic byte-form, so a board that disables nothing never re-hashes). Semantic content only — reordering rows or reformatting the JSONL is a no-op. |
| proposer brief | Read text, normalize line endings to `\n`, strip trailing whitespace per line, strip leading/trailing blank lines. CRLF churn and re-indentation are no-ops. |
| scoring | Parse into a fully-defaulted `ScoringWeights` — **including the `tournament` structure block** (§9) — preserve every parsed runtime numeric value, then `json.dumps(sort_keys=True)`. Partial and full documents agree, and equivalent JSON spellings of the same number are no-ops. A distinct numeric value, structure, or parameter rolls the epoch. An enabled integration may add system-owned implementation identity before hashing. |
| evaluator_revision | Serialize the explicit Zicato evaluator revision. Increment it only when measurement or tournament-decision semantics change. |
| adapter | Remove `mutable_trees` from the validated worker reconstruction document, recursively normalize its JSON values, sort object keys and integration names, and add source hashes for adapter implementations outside the mutable trees. An ADK entry point is one field in its worker document. |
| mutable_trees | Sorted tuple of normalized, never filesystem-resolved path strings. Registration order is a no-op. |
| proposer | Resolve the proposer dir (or the builtin default) to a `ProposerSpec` and serialize sorted-key: `agent_id`, sorted `tools`, per-skill normalized-body hashes sorted by name, and the custom `agent.py` source hash. Each skill body is normalized in the same way as the proposer brief, so a whitespace-only skill edit is a no-op; a semantic skill edit (or adding / removing / renaming a skill, or editing `agent.py`) rolls the epoch. The builtin default canonicalizes to a stable form, so a workspace that never registers a proposer keeps a stable hash. |

The canonical forms are concatenated and hashed. Missing files are
treated as the empty string for that component (so a board-less
workspace still hashes deterministically) — a warning is logged.

A whitespace-only proposer-brief edit, a whitespace-only skill edit, a
reordered board, or an equivalent numeric spelling in `scoring.json` leaves
the hash unchanged. A changed board input, a retuned `per_judge_weight`, an
evaluator revision bump, an added custom judge, a changed adapter worker
document, an edit to adapter implementation source outside the mutable trees,
an added mutable tree, or a registered or edited proposer changes it.

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
   - **Current epoch's hash matches** (or is unrecorded — see §10.6).
     No roll; `evolve` runs against the current epoch.
   - **Current epoch's hash differs** (the contract drifted). With
     auto-epoching on, `evolve` closes the current epoch (generating
     `analysis.md`), opens a fresh one carrying the new contract, and
     runs against it. The roll prints a message naming which
     components changed. The label is the literal component names
     (`board`, `brief`, `scoring`, `evaluator_revision`, `adapter`, `mutable_trees`,
     `proposer`), comma-joined. It falls back to a generic `contract` when
     the epoch stores no per-component breakdown to compare against:
     ```
     contract changed (brief) — rolled 2026-05-15_e0 -> 2026-05-15_e1
     ```
     With `--no-auto-epoch`, it errors instead of rolling.

The resolved epoch is pinned for every round of the invocation, so the loop
never re-rolls mid-flight. Passing `--epoch <id>` disables auto-rolling. The
workspace gate still verifies the selected epoch's recorded implementation
identity and recomputes its contract hash from the frozen board, brief, scoring,
adapter registration, mutable-tree declaration, and proposer. An unreadable or
drifted frozen contract stops the run instead of silently comparing results
under different rules.

### 10.5 Baselining a rolled epoch

When the auto-roll creates a new epoch, its `v0` baseline is the
**promoted head of the previous epoch** — the last promoted
generation's snapshot — rather than the registered baseline harness.
The lineage therefore continues: the rolled epoch starts from the best
result its predecessor reached. If the predecessor had no promoted
generations beyond `v0`, the rolled epoch seeds from the registered
mutable trees.

The cross-epoch link is recorded in `lineage.json` as the new epoch's
`v0_parent`, pointing back at the closed predecessor.

### 10.6 An epoch that records no contract hash

`EpochConfig.contract_hash` is `None` when the epoch's `config.json`
records no hash; an empty string on disk reads back as `None`, so the
absent case has exactly one in-memory representation. An epoch with no
recorded hash is treated as **always matching**, so `evolve` never rolls
it. The operator keeps full manual control through `zicato epoch new`
until an epoch carrying a hash is created.

### 10.7 Auto-epoch naming

Auto-created epochs are named `e{N}` where `N` is the count of existing
epochs — so `2026-05-15_e0`, `2026-05-15_e1`, and so on. `zicato epoch
new <name>` with an explicit name is unchanged. `zicato evolve
--epoch-name <name>` overrides the `e{N}` scheme for an epoch that
`evolve` auto-creates.

An auto-rolled epoch has **no goal recorded** — the roll happens
mid-`evolve` with no operator-interaction surface, so the epoch's `goal`
field lands empty and the roll prints a `NOTE:` recommending the operator
fill it in with `zicato epoch set-goal --epoch <id> --goal "..."` (§10.8).

### 10.8 The escape hatches

`epoch new` / `close` / `switch` / `set-goal` all keep working
unchanged. They are the manual escape hatches:

- `--no-auto-epoch` makes `evolve` strict: it errors on contract drift
  instead of rolling. Use it to be told about drift and choose the
  response.
- `zicato epoch new` is still the way to start an epoch with a
  hand-chosen name, or to roll for a reason the hash cannot see (e.g.
  a regression-baseline rebase that did not touch any contract file).
  The supplied contract files are frozen into the epoch directory AND
  published as the workspace's live contract, so a subsequent `zicato
  evolve` resolves the same contract and continues this epoch rather
  than spuriously rolling.
- `zicato epoch switch` still re-points the current-epoch marker.
- `zicato epoch set-goal --epoch <id> --goal "..."` sets (or overwrites)
  an epoch's goal after the fact — intended for the auto-roll case where
  the goal lands empty. Idempotent; also refreshes the index `epochs.goal`
  column.

## 11. Cross-references

| Topic | Document |
|---|---|
| Hypothesis schema, proposer contract | this document §3 |
| The proposer brief vs the per-entry `Rubric`; authoring boards | [BOARD-AUTHORING.md](BOARD-AUTHORING.md) |
| Patch shape and validator constraints | [MUTATION-SURFACE.md](MUTATION-SURFACE.md) |
| Loss profile written into each `runs/{id}/loss.json` | [TELEMETRY.md](TELEMETRY.md) |
| Drift loss scalar that drives `tournament_decision` | [SCORING.md](SCORING.md) |
| Per-epoch tournament structure: config block, persisted record, API, UI | [TOURNAMENT-DATA-MODEL.md](TOURNAMENT-DATA-MODEL.md) |
| The proposer as a contract input: tiers, tools, Design A, epoch-roll | [PROPOSER.md](PROPOSER.md), `skills/zicato-design-proposer/SKILL.md` |
| CLI commands for `epoch new` / `close` / `list` | [CLI.md](CLI.md) |
| Atomic-rename helper used by `analysis.html` writes | [RUNTIME.md](RUNTIME.md) §6 |
| Live dashboard that supersedes `analysis.html` during an `evolve` | [DASHBOARD.md](DASHBOARD.md) |
| Git-backed storage that moves generation directories into a private repo | [STORAGE.md](STORAGE.md) |
| Why mandatory structured hypothesis up front | [RATIONALE.md](RATIONALE.md) |
| The experiment record fed back to the proposer (experiment memory) | [EXPERIMENT-MEMORY.md](EXPERIMENT-MEMORY.md) |
