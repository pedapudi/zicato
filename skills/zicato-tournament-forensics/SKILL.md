---
name: zicato-tournament-forensics
description: Explain a single promote/reject decision — re-score one champion/challenger generation pair in isolation and read exactly why the gate fired (verdict transparency, the per-entry A/B grid, the scalar breakdown, the mutation heatmap, the score trajectory, and cost). Use this when you need to understand why a specific round promoted or rejected.
---

# zicato tournament-forensics — read one promote/reject decision

A **tournament** is a king-of-the-hill matchup: the reigning champion (the
**parent** generation) versus one **challenger** (the **child** proposed against
it), scored over the frozen board, decided by the two-sided promotion gate. This
skill is for forensics on a *single* matchup — "why did this round promote /
reject?". For the epoch-wide retrospective see `skills/zicato-analyze-epoch`; for
cross-epoch lineage see `skills/zicato-lineage`; for the live bracket UI see
`skills/zicato-dashboard`.

Always call the CLI from the project venv: `.venv/bin/zicato ...`. See
[AGENTS.md](../../AGENTS.md).

## Re-score a pair in isolation

```sh
.venv/bin/zicato tournament PARENT CHILD \
    --workspace .zicato [--epoch <id>] [--mode full|fast] [--skip-regression]
```

- `PARENT` and `CHILD` are **generation ids** under the resolved epoch
  (`v2`, `v3`, ...) — parent is the champion, child is the challenger. With
  `--epoch` omitted, the epoch resolves from the workspace's `current_epoch`
  marker.
- `--mode full` (default): runs the **whole board** under both generations and
  applies the gate against the live A/B comparison.
- `--mode fast`: runs only the **child** and applies the gate against the
  parent's historical aggregate cached in the workspace — one board pass, not
  two (see [SCORING.md §7](../../docs/design/SCORING.md#7-fast-mode-and-the-tournament)).
- `--skip-regression`: skip the regression-suite gate even when scoring enables
  it (a per-invocation override).
- Output is the `GateOutcome` printed as **JSON** (`decision`, `reason`,
  `delta_scalar`, `delta_pass_rate`).

> **WARNING — this is a live, budget-spending run** in `full` mode (it executes
> the inner harness over every board entry, twice). Per the project rules, do
> NOT run a live tournament without the user's explicit go-ahead. For *reading*
> an already-settled decision, inspect the artifacts (below) instead of
> re-running. The shipped CLI has **no `--no-record-outcome` flag** (the
> [CLI.md §3.x](../../docs/design/CLI.md) reference to it is aspirational); a
> live re-run records its outcome into the workspace.

## Exit code

The two-command happy path and CLI exit-code contract
([CLI.md](../../docs/design/CLI.md)): **`0` = promote, `6` = reject** (with a
successful, informational JSON payload — useful for scripts that branch on the
verdict). `2`/`3` are usage/config errors. The standalone command's JSON
`decision` field (`"promoted"` / `"rejected"`) is the authoritative verdict; the
exit code mirrors it for scripting.

## Read the verdict without re-running

A settled round's full forensic record is already on disk:

```
.zicato/epochs/{id}/generations/{child}/experiment.json   # hypothesis + outcome
.zicato/epochs/{id}/generations/{child}/patches/*.json    # what it changed
.zicato/epochs/{id}/generations/{*}/runs/.../loss.json     # per-entry loss profiles
```

The `outcome` block in `experiment.json` carries `tournament_decision`,
`rejection_reason`, `drift_loss_delta`, `pass_rate_delta`, and the
`hypothesis_match` array
([EPOCHS-AND-JOURNALING.md §3.3](../../docs/design/EPOCHS-AND-JOURNALING.md#33-outcome-written-after-the-run)).

## How to read *why* the gate fired

The gate is **two-sided** ([SCORING.md §5](../../docs/design/SCORING.md#5-the-tournament-promotion-gate)).
A challenger must satisfy **both** to promote:

1. **Margin (drift side).** The scalar is a *loss* — lower is better. Promotion
   needs `child_scalar <= parent_scalar - promote_margin`. A child that improved
   but by less than the margin is a **near-miss reject**; a child whose loss
   *rose* is a **regressed reject**. `delta_scalar = child - parent` (negative =
   improvement).
2. **Strict monotonicity (pass-rate side).** No board entry the parent passed
   may regress (child fails, errors, or no longer evaluates it). Any such entry
   → `rejection_reason = pass_rate_regression_on_<entry_id>` and the candidate
   is rejected regardless of drift improvement.

The matchup detail in the Tournament view
([TOURNAMENT.md §3](../../docs/design/TOURNAMENT.md#3-per-matchup-detail)) lays
this out in five sections; read them in order to localize the verdict:

- **Hypothesis** — the proposer's stated intent before the run.
- **Patches** — the `mutation_id`, `op`, and rationale of each change.
- **Per-entry A/B grid** — the board entry by entry, champion side vs challenger
  side, with the per-entry drift Δ and which passes **flipped**. This is the
  heart of a verdict: it shows *which* entries the challenger won, lost, or
  tied, and exactly which pass regressed (the monotonicity trip).
- **Scalar breakdown** — how the per-entry numbers aggregate into the two
  `gen_score`s the gate consumes (`weighted_drift`, `pass_rate`, the weighted
  terms).
- **Gate verdict** — the decision with both sides shown: the margin computation
  (`parent.score - candidate.score` vs `required margin`) and the monotonicity
  check, plus the exact `rejection_reason`. An operator override is never silent
  — it shows the would-have-been verdict alongside the override.

## The cross-round analytics (the wider forensics)

Beyond one matchup, the Tournament view offers epoch-wide analytics
([TOURNAMENT.md §4](../../docs/design/TOURNAMENT.md#4-tournament-detail-analytics)),
all served from the analytical index:

- **Verdict transparency** (§4.1) — the §3.5 gate panel for every round; no
  promotion or discard is ever a black box.
- **Per-entry A/B grid, aggregated** (§4.2) — which entries consistently
  differentiate generations and which never do (the non-differentiating-entry
  signal that loop-health escalates).
- **Optimization trajectory** (§4.4) — champion `gen_score` over rounds
  (monotonically non-increasing) plus every challenger's score, including
  discarded ones; a flat champion line is the *stalled loop* signal.
- **Mutation heat map** (§4.5) — which mutation points correlate with winning
  (touched vs promoted). A **correlation, not causation** — a surface touched
  five times and promoted once is *resisting* improvement; consider the proposer
  brief's `## Forbidden` list.
- **Tournament cost** (§4.6) — wall-clock, aux-LLM calls, and board runs per
  round and per epoch; fast mode shows up as one board pass instead of two.

## Guardrails

- venv-only (`.venv/bin/zicato`); never bare `uv sync` (use `--all-extras`).
- Do NOT run a live `tournament` (especially `--mode full`) without explicit
  user go-ahead — it spends LLM budget and records an outcome. Prefer reading
  the settled artifacts.
- The verdict is `0`/promote vs `6`/reject; the JSON `decision` is
  `promoted`/`rejected`.

## See also

- [TOURNAMENT.md](../../docs/design/TOURNAMENT.md) — gauntlet structure, matchup detail, the six analytics.
- [SCORING.md](../../docs/design/SCORING.md) — the scalar and the two-sided promotion gate.
- `skills/zicato-analyze-epoch` — the epoch-wide hypothesis-vs-outcome retrospective.
- `skills/zicato-dashboard` — the live Tournament view (bracket + matchup detail).
