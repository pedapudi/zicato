---
name: zicato-analyze-epoch
description: Close an epoch and read its retrospective — run the LLM analysis pass, read analysis.md and the rendered journal, and re-render reports against current on-disk data. Use this when an epoch's rounds are done and you want the hypothesis-vs-outcome ledger and the closing narrative, or when an older epoch's analysis.md needs repairing.
---

# zicato analyze-epoch — close an epoch and read its analysis

An **epoch** is the unit of evaluation contract (one frozen board + proposer
brief + scoring + target-adapter identity + proposer). Within it, the
tournament runs round after round, recording each round. *Closing* an epoch runs the retrospective
LLM analysis pass and writes the per-epoch `analysis.md`. This skill is the
read/close side of an epoch — to drive the loop see `skills/zicato-evolve`; to
explain a single round's verdict see `skills/zicato-tournament-forensics`; for
cross-epoch lineage see `skills/zicato-lineage`.

Always call the CLI from the project venv: `.venv/bin/zicato ...`. See
[AGENTS.md](../../AGENTS.md). Read-only inspection and `--help` only — do not
launch a live `evolve` (it spends LLM budget).

## What lives where (the artifacts)

```
.zicato/
  epochs/{epoch_id}/
    analysis.md     # re-rendered after every round; mid-epoch its masthead
                    #   carries `LIVING DRAFT — through round N`
    analysis.html   # the rendered companion, written alongside analysis.md
    generations/{vN}/experiment.json      # the hypothesis, written before the run
    rounds/{round}/field_settlement.json  # each candidate's recorded outcome
    rounds/{round}/round_log.jsonl        # the round's typed event log
  lineage.json      # cross-epoch ancestry (one file, all epochs)
```

There is no `journal.md` file on disk. The journal is rendered on demand from
the experiment records joined to their round outcomes: the dashboard serves it
at `/api/epoch/<epoch_id>/journal.md` as markdown.

See [EPOCHS-AND-JOURNALING.md](../../docs/design/EPOCHS-AND-JOURNALING.md) §2,
§4, §5.

## The hypothesis-vs-outcome ledger

Each generation's `experiment.json` carries the proposer's **hypothesis**
(written *before* the run). After the tournament concludes, the candidate's
**outcome** is recorded in that round's `rounds/{round}/field_settlement.json`
under `candidates[]`, and every reader joins it to the experiment by
`experiment_id`; the `outcome` key inside `experiment.json` itself stays `null`
([EPOCHS-AND-JOURNALING.md §3.3](../../docs/design/EPOCHS-AND-JOURNALING.md#33-outcome-written-after-the-run)).
The hypothesis names its predictions in `expected_metric_movements` (metric
name, direction, and a `small` / `medium` / `large` magnitude). Each prediction
is graded against the realised movement on **sign AND magnitude** — "predicted
decrease medium, observed decrease small" is a **miss** (no partial credit).
This is what distinguishes a proposer that *reasons* from one that *guesses*;
the Publication's "Hypothesis vs outcome" table and the dashboard's
hypothesis-accuracy view show the grades.

The rendered journal has one section per experiment, headed `## v{N} — <core
idea>`: when it was proposed, the modulating mutation points, the `why`, and
once its round commits the decision with its scalar, drift-loss and pass-rate
deltas (plus the rejection reason when rejected).
`analysis.md` aggregates the whole epoch into an academic-paper-style
publication with fixed sections — the data-bearing ones templated
deterministically from the workspace, the prose ones written by one bounded
evaluation-engine call:

```
## Abstract                                  (prose)
## Introduction                               (prose)
## Methodology                                (deterministic)
## Approach & Implementation                  (deterministic)
## Experimental Results                       (deterministic)
## Statistical Integrity                      (deterministic)
## Proposer Analytics                         (deterministic)
## Analysis — What Worked and What Didn't     (prose)
## Threats to Validity & Limitations          (deterministic)
## Conclusion & Next Directions               (prose)
```

## 1. Close an epoch (runs the analysis pass)

```sh
.venv/bin/zicato epoch close [EPOCH_ID] --workspace .zicato
```

- `EPOCH_ID` omitted → closes the **current** epoch.
- Stamps `closed` + `closed_at` on the epoch's `config.json` and in
  `lineage.json`, then re-stamps the persisted report's masthead so the
  `LIVING DRAFT` line becomes "closed". Nothing is chmod'ed — a closed epoch is
  read-only by convention rather than by permissions.
- **The CLI close does NOT run the LLM prose pass**: `zicato epoch close` wires
  no evaluation callable, so it leaves an existing `analysis.md` in place
  (re-stamped) and writes a *stub* only when none exists yet. It is `evolve`'s
  auto-close — which rolls the contract with the aux callable in hand — that
  runs the full prose render. To get the prose pass by hand after a manual
  close, run `zicato repair report` without `--no-llm` (step 3)
  ([EPOCHS-AND-JOURNALING.md §5.1](../../docs/design/EPOCHS-AND-JOURNALING.md#51-closing--manual-primary-auto-close-fallback)).

**A closed epoch is read-only.** Its board, brief, scoring, and generation
artifacts are frozen — that is what makes its matchups comparable. Re-opening or
re-running it is not a supported operation; start a fresh epoch instead (a new
epoch's `v0` baselines off the closed epoch's final champion). You *can* still
re-render its reports (steps 3-4) — that reads the frozen data and only rewrites
`analysis.md` / `analysis.html`.

## 2. Read the journal and the analysis

There is **no `zicato journal` / `zicato analysis` subcommand**. Read the
report files directly, and the journal through the dashboard:

```sh
# the retrospective publication (present mid-epoch too, stamped LIVING DRAFT)
$EDITOR .zicato/epochs/<epoch_id>/analysis.md

# the archival HTML snapshot — openable mid-epoch via file://
xdg-open .zicato/epochs/<epoch_id>/analysis.html

# the journal, rendered from the records (needs a running `zicato dashboard`)
curl -s http://127.0.0.1:7892/api/epoch/<epoch_id>/journal.md
```

To read one experiment's full ledger row, pair its `experiment.json`
(hypothesis) with its candidate entry in the round's `field_settlement.json`
(outcome):

```sh
jq '.candidates[] | {generation_id, outcome}' \
   .zicato/epochs/<epoch_id>/rounds/<round>/field_settlement.json
```

## 3. Re-render the report against current data

```sh
.venv/bin/zicato repair report --workspace .zicato [--epoch <id>] [--no-llm]
```

Re-renders `analysis.md` / `analysis.html` from the current on-disk files.
Idempotent and read-only against everything except those two files. Use it to
repair an epoch whose report was written by a buggy older orchestrator (e.g. the
data sections rendered empty). `--no-llm` skips the prose pass and substitutes
placeholders — the **deterministic** figures, tables, and scores are re-rendered
regardless, so `--no-llm` is the safe, budget-free repair.

## 4. (Re)run the decision-telemetry analyzer

```sh
.venv/bin/zicato inspect telemetry --workspace .zicato [--epoch <id>] [--round N]
```

Runs the decision-telemetry analyzer for an epoch out of band, writing an
insight to `epochs/{id}/insights/round_{N:04d}.md` (or `insights/latest.md` when
`--round` is omitted). `evolve` runs this per round; use it to regenerate an
insight without re-running the loop. The analysis covers the training slice's
runs only. The next round's proposal evidence carries the highest-numbered
`round_{N:04d}.md`, so a file written with `--round` can reach the proposer;
`latest.md` never does.

## Guardrails

- venv-only (`.venv/bin/zicato`); never `uv sync` (use `uv sync --all-extras`).
- Do **not** start a live `evolve`/`tournament` to "freshen" an epoch — that
  spends budget. Closing and report regeneration are the read-side tools.
- Only `analysis.md`'s **prose** sections need the evaluation LLM; its data
  sections and `analysis.html` are deterministic. Prefer `--no-llm` when you
  only need the figures/tables back — without it, `zicato repair report`
  resolves the evaluation callable from config and spends a call.
- A closed epoch is frozen — treat `epochs/{id}/` as read-only.

## See also

- [EPOCHS-AND-JOURNALING.md](../../docs/design/EPOCHS-AND-JOURNALING.md) — epoch lifecycle, the Experiment, journal + analysis.
- [SCORING.md](../../docs/design/SCORING.md) — the scalar and the promotion gate behind every outcome.
- `skills/zicato-tournament-forensics` — explain one round's promote/reject decision.
- `skills/zicato-lineage` — read lineage across epochs and generations.
