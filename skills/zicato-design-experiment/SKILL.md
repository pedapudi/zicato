---
name: zicato-design-experiment
description: Help the operator author the MANDATORY pre-run hypothesis for a zicato experiment — pick a mutation target justified by observed loss patterns and predict the outcome BEFORE the run. Use when designing the next change to test; this is a thinking/authoring skill, not a run command.
---

# zicato design-experiment (the mandatory pre-run hypothesis)

Every zicato experiment carries a **hypothesis written before the run** and an
**outcome written after** (AGENTS.md rule 6). This skill helps you author that
hypothesis: choose a mutation point justified by the loss patterns, and predict
the effect *up front*. The prediction-vs-outcome match is the load-bearing
learning signal — so **do not backfill a hypothesis to match a result.** If you
have already seen the tournament outcome, you cannot author an honest hypothesis
for that run; design the next one instead.

> This is an authoring/reasoning skill. It produces text (a hypothesis the
> proposer or operator records); it does not call LLMs or spend budget. All the
> grounding commands below are read-only.

## Step 1 — ground the hypothesis in observed loss

Look at where loss actually concentrates before deciding what to change.
Read-only:

```sh
Z=.venv/bin/zicato

# Decision-telemetry analyzer for the current epoch (no proposer budget).
$Z analyze-telemetry                 # writes insights/latest.md
# then read .zicato/epochs/<epoch>/insights/latest.md

# What does the epoch exist to test? (the goal anchors the hypothesis)
$Z epoch list
```

```sql
-- Read-only: hottest drift metrics this epoch (see zicato-index-ops).
sqlite3 -readonly .zicato/index.db "
  SELECT mc.namespace, mc.name, mc.severity, SUM(mc.count) total
  FROM metric_counts mc JOIN runs r ON r.run_id = mc.run_id
  WHERE r.epoch_id = 'e3'
  GROUP BY mc.namespace, mc.name, mc.severity
  ORDER BY total DESC LIMIT 20;"
```

A good hypothesis names a *pattern* ("CONFABULATION_RISK fires on 70% of
`[research]` entries and 0% on `[summarise]`"), not a vibe.

The proposer now also sees an **outcome failure-mode profile** alongside the
decision-telemetry digest: a board-anonymized, **train-slice-only**, bucketed
summary of *outcome marginals* — board-wide rates of generic failure modes
(over-retrieval, misses, empty/terse answers) plus precision/recall from any
scorer `metrics`. It is the **marginal, never the joint**: aggregate rates
("over-retrieves ~40% of runs"), never an entry id, question, or output token,
and it reuses the same holdout split + bucketing the loss summary already uses
(never the holdout slice). An operator may extend it with a numeric-only
`outcome_summarizer_spec` hook in `scoring.json` (sanitized + bucketed before it
reaches the proposer). When you ground a hypothesis, this profile is a second,
*outcome*-side signal to name a pattern from — it tells you *why* answers were
wrong (over-retrieval vs miss), not just that a scalar moved. It is OUTPUT-only
and is NOT a mutation point (see `zicato-mutation-audit`).

## Step 2 — pick a mutation target that is allowed and justified

The proposer may only touch enumerated mutation points, and never anything in
the proposer brief's `## Forbidden` list (validator V5).

```sh
$Z mutations --show preview                 # the mutable surface
$Z mutations --id 'researcher_*'            # filter by id glob
$Z mutations --format json                  # to script it
```

The chosen `modulating` ids MUST be a non-empty subset of those listed and MUST
NOT appear in `## Forbidden` (read the epoch's `brief.md`). Justify *why this
point* plausibly moves *that pattern* — the link from observed loss to the
mutation target is the heart of the hypothesis.

## Step 3 — write the hypothesis (schema is mandatory, enforced at propose time)

Schema-invalid hypotheses are rejected (exit `4`) and re-prompted. Every field
is required. Shape (`docs/design/EPOCHS-AND-JOURNALING.md` §3.1):

```json
{
  "hypothesis": {
    "core_idea": "Tighten the researcher's instruction so it cites sources before asserting facts.",
    "modulating": ["researcher.instruction", "researcher.description"],
    "why": "Pattern across rounds 3-5: CONFABULATION_RISK fires on 70% of [research] entries, 0% on [summarise]. The current instruction does not require citations.",
    "expected_drift_movements": [
      {"kind": "CONFABULATION_RISK", "direction": "down", "magnitude": "moderate"},
      {"kind": "TOOL_ERROR",        "direction": "up",   "magnitude": "minor"}
    ],
    "expected_pass_rate_delta": {"low": 0.0, "high": 0.15},
    "risks": [
      "Tighter prompt may add tool calls per turn (slower).",
      "If sources are unavailable the researcher may refuse instead of approximating."
    ]
  },
  "patch_ids": ["<patch hash>", "..."]
}
```

Field discipline:

- **core_idea** — one sentence, plain language; the journal cites it verbatim.
- **modulating** — non-empty subset of `zicato mutations` ids; not in `## Forbidden`.
- **why** — the pattern observation from Step 1, citing pattern/metric ids.
- **expected_drift_movements** — per drift kind: `direction` ∈ `up`/`down`/`flat`,
  `magnitude` ∈ `minor`/`moderate`/`major`. Predict honestly, including the
  kinds you expect to get *worse* (that is what makes the match informative).
- **expected_pass_rate_delta** — `{low, high}` band, e.g. `{0.0, 0.15}` = "no
  worse, up to 15 points better". Note the gate's pass-rate monotonicity has a
  `pass_rate_monotonicity_scope` (`scoring.json`): under `per_entry` (default) a
  single entry the champion passed regressing is a hard reject regardless of the
  band; under `aggregate` only the board-wide pass-rate matters. Predict with
  the active scope in mind (`zicato-tune-scoring` owns the values).
- **risks** — plausible failure modes; these become the things to check first
  if the outcome disappoints.

## Step 4 — predict, then run, then let the outcome land on its own

State your prediction clearly *before* running. After the tournament,
`evolve` / `tournament` writes the `outcome` block (including `hypothesis_match`
per predicted drift kind) into the same `experiment.json` — you do not write it.
A wrong prediction is a successful, informative experiment; do not retro-edit
`expected_drift_movements` to make `hypothesis_match` look better.

The actual change is generated by `zicato propose` (creates the candidate
generation and writes `experiment.json`) — see `zicato-step-loop`. Running
`propose` / `tournament` spends budget: get the operator's go-ahead first.

## See also

- `docs/design/EPOCHS-AND-JOURNALING.md` §3 — full hypothesis/experiment schema.
- `docs/design/RATIONALE.md` — why the structured hypothesis is mandatory.
- `docs/design/MUTATION-SURFACE.md` — mutation points, validators, `## Forbidden`.
- `docs/design/SCORING.md` — how drift movements + pass-rate become the scalar.
- sibling skills: `zicato-step-loop` (run the experiment), `zicato-index-ops`
  (query the patterns), `zicato-triage-stuck-loop` (if predictions keep missing).
