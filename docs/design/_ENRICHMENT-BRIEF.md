# Variant enrichment brief — candidate lifecycle, boards, per-board scoring, tournament match-ups

> Temporary working brief for the dashboard-variant enrichment wave. Each
> variant (A/B/C/D) gets the **same four visualization themes**, each
> expressed in that variant's own diagrammatic language. Bind everything to
> the real API below — no invented fields. Delete this file before the
> variants are finalized.

## The four themes (every variant implements all four)

1. **Candidate lifecycle** — visualize one generation's life: born from a
   parent via a *patch* on mutation points → faces the *board* → scores a
   per-entry *loss profile* → meets the champion at the *gate* → is
   *promoted* or recorded as a *dead branch*. The shape should read as a
   life-story, not a form. Make lineage (v0 → {v1, v2}) legible: who the
   parent was, which siblings competed, who holds the crown.

2. **The boards a candidate faces** — the task board is fixed per epoch.
   Show the *set* of entries each candidate runs, their `kind`
   (single_turn / multi_turn_scripted / multi_turn_emulated), tags,
   per-entry budget, and weight. A candidate "faces" all of them; surface
   that as a legible field of tests, not a wall of rows.

3. **Per-board scoring + drill-down** — for a candidate, show how it scored
   on *each* board entry (drift loss + pass/fail + budget-exceeded), then
   let the user drill into one entry: its expectation outcomes, per-judge
   losses, and ultimately the transcript/conversation. Three depths:
   board-field → one entry → the run detail.

4. **Match-ups across tournament styles** — zicato *runs* a king-of-the-hill
   **gauntlet** (one reigning champion per epoch, one challenger per round,
   paired/common-random-number comparison; see SELECTION.md §3). Visualize
   the actual gauntlet from real data, AND show — illustratively, clearly
   labelled as alternative structures over the *same* candidates — how the
   match-ups would look under the other documented styles:
   single-elimination bracket, double-elimination, Swiss pairing, and
   racing / successive-halving (SELECTION.md §2, §5, §6). Use a *different*
   diagram topology per style (bracket tree, round-robin graph, race lanes).
   Be honest: only the gauntlet has real per-round data; the others are
   conceptual overlays on the same generation set — label them as such.

## Data contract (live, verified against the t7 workspace)

Epoch in the live data: `2026-05-30_e0`. Generations: `v0` (seed, promoted
crown), `v1` (rejected), `v2` (rejected). Both challengers lost to v0.

### Lineage / candidates
`GET /api/lineage`
```json
{"generations":[
  {"generation_id":"v0","epoch_id":"2026-05-30_e0","parent_generation_id":"","promoted":true,"created_at":"…"},
  {"generation_id":"v1","epoch_id":"2026-05-30_e0","parent_generation_id":"v0","promoted":false,"created_at":"…"},
  {"generation_id":"v2","epoch_id":"2026-05-30_e0","parent_generation_id":"v0","promoted":false,"created_at":"…"}
]}
```

### Epoch (contract + the board the candidates face)
`GET /api/epoch`
```json
{"epoch_id":"…","contract_hash":"…","closed":false,
 "harness":{"entrypoint":"…","mutable_trees":["…"]},
 "board":[
   {"id":"waffles_single","kind":"single_turn","input_preview":"Make a presentation about waffles.",
    "expectation_kind":"predicate","budget_s":180.0,"weight":1.0,
    "tags":["single_turn","topic_waffles","smoke"]},
   {"id":"q3_metrics_outline","kind":"single_turn","input_preview":"Outline a deck on quarterly metrics for Q3.",
    "expectation_kind":"predicate","budget_s":180.0,"weight":1.0,"tags":[…]},
   {"id":"waffles_revision_scripted","kind":"multi_turn_scripted","input_preview":null,
    "expectation_kind":null,"budget_s":240.0,"weight":1.0,"tags":[…]},
   {"id":"picky_stakeholder_emulated","kind":"multi_turn_emulated", … }
 ]}
```
`input_preview` is null for multi-turn entries. `expectation_kind` can be null.

### Per-entry scoring for one candidate (theme 3, depth 1)
`GET /api/generation/{epoch_id}/{generation_id}/per-entry`
```json
{"epoch_id":"…","generation_id":"v1","tournament_id":"2026-05-30_e0:v0->v1",
 "entries":[
   {"entry_id":"waffles_single","run_id":"f318d5ae…","generation_id":"v1",
    "drift_loss":60.5,"pass_fail":0,"runtime_ms":180000,"wall_clock_budget_exceeded":true},
   {"entry_id":"picky_stakeholder_emulated","run_id":"82b5ff17…","drift_loss":642.5,
    "pass_fail":0,"runtime_ms":360000,"wall_clock_budget_exceeded":true},
   …
 ]}
```
`pass_fail` is 0 / 1 / null (null = no predicate). `drift_loss` is the
per-entry loss (lower better). `wall_clock_budget_exceeded` flags a timeout.

### One entry's expectation outcomes (theme 3, depth 2)
`GET /api/run/{epoch_id}/{generation_id}/{entry_id}/expectations`
```json
{"epoch_id":"…","generation_id":"v1","entry_id":"waffles_single",
 "outcomes":[{"kind":"predicate","passed":false,"detail":"predicate returned False","judge_name":null,"score":null}]}
```

### Per-judge loss for one candidate (theme 3, depth 2)
`GET /api/generation/{epoch_id}/{generation_id}/per-judge`
```json
{"epoch_id":"…","generation_id":"v1",
 "judges":[{"judge_name":"incorporates_feedback","weighted_loss":27.0,"raw_loss":27.0,"run_count":1,"weight":1.0}]}
```
Also: `GET /api/run/{epoch_id}/{generation_id}/{entry_id}/per-judge`,
`GET /api/run/{epoch_id}/{generation_id}/{entry_id}/header`.

### The run detail (theme 3, depth 3)
`GET /api/conversation/{run_id}` and
`GET /api/run/{epoch_id}/{generation_id}/{entry_id}/transcript` — the
transcript (turns, tool calls, drift events). `run_id` comes from per-entry.

### The actual gauntlet (theme 4, real data)
`GET /api/tournaments`
```json
{"epoch_id":"…","champion_lineage":["v0"],
 "matchups":[
   {"champion":"v0","challenger":"v1","decision":"rejected","delta_scalar":75.71,
    "rejection_reason":"challenger regressed: loss rose by 75.71 …",
    "hypothesis_core_idea":"Enforce explicit slide-structure output …","ran_at":"…"},
   {"champion":"v0","challenger":"v2","decision":"rejected","delta_scalar":1.51,
    "rejection_reason":"…","hypothesis_core_idea":"Tighten the coordinator's oversight …","ran_at":"…"}
 ]}
```
`GET /api/tournaments/{generation_id}` — detail for one matchup.

### Paired per-board match-up grid (theme 4, the heart of a single round)
`GET /api/matchup-grid/{epoch_id}/{champion_id}/{challenger_id}`
```json
{"epoch_id":"…","champion":"v0","challenger":"v1",
 "entry_grid":[
   {"entry_id":"q3_metrics_outline","parent_drift_loss":71.0,"child_drift_loss":63.5,
    "parent_pass":false,"child_pass":false,"delta":-7.5,"verdict":"improved","won_by":"v1",
    "parent_session_id":"cb82…","child_session_id":"00a9…"},
   {"entry_id":"picky_stakeholder_emulated","parent_drift_loss":105.5,"child_drift_loss":642.5,
    "delta":537.0,"verdict":"regressed","won_by":"v0", …},
   {"entry_id":"every_expectation_kind_demo","parent_drift_loss":60.5,"child_drift_loss":60.5,
    "delta":0.0,"verdict":"flat","won_by":null, …}
 ]}
```
`verdict` ∈ improved / regressed / flat. `won_by` is the generation id or
null. This is the paired (common-random-number) per-entry duel — ideal for a
slopegraph, a head-to-head bar pair, or a per-board "who won" strip.

### The gate decision (theme 1 climax + theme 4)
`GET /api/round/{epoch_id}/{champion_id}/{challenger_id}/gate`
```json
{"decision":"rejected","reason":"challenger regressed: loss rose by 75.71 …",
 "delta_scalar":75.71,"delta_pass_rate":0.0,
 "rules":[
   {"id":"regression_suite","label":"Regression suite","status":"skipped","fired":false},
   {"id":"scalar_margin","label":"Scalar margin","status":"fail","detail":"70.94 → 146.65 (+75.71; needs ≤ -0.01)","fired":true},
   {"id":"pass_rate_monotonicity","label":"Pass-rate monotonicity","status":"not_reached","fired":false},
   {"id":"namespace_monotonicity","label":"Namespace monotonicity","status":"not_reached","fired":false}
 ],
 "scalar_components":{
   "champion":{"cost":0.009,"drift":68.5,"latency":0.0,"output":0.0,"pass":1.0,"rubric":-0.0,"schema":1.43},
   "challenger":{"cost":0.009,"drift":145.64,"latency":0.0,"output":0.0,"pass":1.0,"rubric":-0.0,"schema":0.0}},
 "primary_driver":{"judge":"incorporates_feedback","delta":24.0}}
```
The gate is **three rules in order, short-circuiting**: scalar-margin →
pass-rate-monotonicity → namespace-monotonicity. A fired rule stops
evaluation; later rules read `not_reached`. `scalar_components` is the loss
decomposition for both sides. This is the decisive moment of the lifecycle.

### Other useful endpoints
`/api/score-trajectory`, `/api/epoch/{epoch_id}/per-judge-trend`,
`/api/contract-diff/{epoch_id}`, `/api/mutations/{epoch_id}`,
`/api/files/{epoch}/{generation}/content`, `/api/search`,
`/api/active-runs`, `/api/active-tournament`, `/api/heartbeat` (SSE-ish).

## Vocabulary (be exact)
- **champion / challenger** = tournament role; **parent / child** = lineage.
  Same pair, two framings. Use champion/challenger for the duel, parent/child
  for the family tree.
- **scalar = loss**, lower is better. Drift loss + pass/fail predicates +
  weighted per-judge process drift fold into it.
- **mutation point** = a `# zicato:mutable id="…"` region the proposer may edit.
- **patch** = the diff a challenger applies to the champion snapshot.
- **proposer brief** = operator's brief to the proposer for the epoch.
- **dead branch** = a rejected challenger; the champion stands.
