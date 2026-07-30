---
name: zicato-audit-board
description: "Audit a zicato eval board for HARNESS correctness — not candidate quality (alias: board-doctor). Use after building or changing a board, before trusting any verdict, tournament, or evolution result. Run a KNOWN baseline (a trivially-correct stub + a trivially-wrong stub) then audit the RUN for mechanics: ground-truth winnability, that the graded artifact is the agent's real output (not a summary proxy), judge-fire counts, loss-scalar ranking sanity, and determinism. Use when pass rates look flat/zero, pass seems uncorrelated with quality, judges contribute nothing, or a 'good' candidate scores worst. Do NOT use to decide if a candidate is good — use it to decide whether the board can be trusted to tell you."
---

# Auditing a zicato board for harness correctness (board-doctor)

The hardest, most expensive bug class in a zicato eval is **not** "is the
candidate good?" — it is "is the BOARD measuring what I think it is measuring?".
A board that grades the wrong artifact, encodes unwinnable ground truth, has dead
judges, or computes a loss scalar that ranks candidates backwards will silently
produce confident-but-wrong verdicts, and every downstream tournament and
evolution decision inherits the error.

This skill is a discipline: **build → run a KNOWN baseline → audit the RUN for
harness mechanics — explicitly NOT for candidate efficacy.** A verdict is only
as trustworthy as the harness that produced it.

This is the *correctness-audit* concern, distinct from authoring/designing a
board. It composes — it does not restate — the sibling skills. Defer:

- **What belongs on a board / discrimination** → [`zicato-design-boards`](../zicato-design-boards/SKILL.md).
- **The board JSON schema, entry kinds, expectations, judges** → [`zicato-author-board`](../zicato-author-board/SKILL.md).
- **OUTCOME expectations vs PROCESS judges, drift→loss** → [`zicato-design-judges`](../zicato-design-judges/SKILL.md).
- **The scalar / weights / promote gate** → [`zicato-tune-scoring`](../zicato-tune-scoring/SKILL.md).
- **The canonical telemetry files (`events.jsonl` / `loss.json`)** → [`zicato-read-telemetry`](../zicato-read-telemetry/SKILL.md).
- **The automated degeneracy detectors (`zicato health`)** → [`zicato-diagnose-health`](../zicato-diagnose-health/SKILL.md).

Run-time partner: [`zicato-configure-tournament`](../zicato-configure-tournament/SKILL.md)
says "audit the board first with this skill" — a misaligned scalar makes
*evolution* learn the wrong lesson, and no tournament config can fix that. See
[`../../AGENTS.md`](../../AGENTS.md) for the operating rules every skill assumes
(read-only here; never start a live `evolve` to produce audit artifacts —
audit the run directories that already exist).

## What this skill is for (and is NOT)

- **For:** deciding whether the *board* can be trusted to tell you which
  candidate is better. It judges the harness mechanics: wiring, fidelity of the
  graded field, judges firing, scalar arithmetic, determinism.
- **NOT for:** deciding whether a candidate is good. It deliberately ignores
  efficacy. "The baseline did well" is not an audit pass; "the known-wrong stub
  failed and the known-correct stub passed, the scalar ranked them correctly,
  every judge fired, and a re-run reproduced the verdict" is.

## The artifacts you audit (zicato's real files)

Every `(epoch, generation, board-entry)` triple maps to one run directory:

```
.zicato/epochs/{epoch}/generations/v{N}/runs/{entry_id}/
  ├── events.jsonl   # canonical goldfive event stream (one event per line)
  └── loss.json      # the reduced LossProfile for this run
.zicato/epochs/{epoch}/generations/v{N}/gen_score.json   # the per-generation aggregate
.zicato/epochs/{epoch}/board.jsonl                       # the frozen board (declares judges)
```

The fields the recipes below key on are the **real** on-disk fields:

| File | Fields the audit reads |
|---|---|
| `loss.json` | `entry_id`, `pass_fail`, `expectation_result.{kind,passed,detail,score,metrics}`, top-level `score` (continuous per-entry quality in `[0,1]`; `None` → derived `1.0`/`0.0` from `pass_fail`) + `metrics` (e.g. `{precision,recall}`), `drift_loss`, `drift_counts[].{kind,severity,count}` (a fired custom judge appears as `kind: "custom:<judge_name>"`), `per_judge_loss[].{judge_name,raw_loss,weight,weighted_loss}`, `adk_session_id` |
| `gen_score.json` | `scalar` (lower=better), `scalar_components` (named contributions that sum to `scalar`: `drift`, `pass`, plus one per non-zero namespace), `pass_rate`, `mean_score` (the uniform continuous-outcome axis — the arithmetic mean of each entry's `[0,1]` score; *equals* `pass_rate` byte-for-byte on an all-bool board), `drift_loss_mean`, `per_entry.{entry_id}.{drift_loss,pass_fail}`, `namespace_aggregates` |
| `board.jsonl` | per entry: `id`, `expectation.{kind,spec,reads}`, `judges[].name` (the declared judge set), `weight`, `tags` |

> `loss.json` is canonical for one run; `gen_score.json` is the aggregate the
> tournament gate scores on. The SQLite `index.db` is **derived** — audit the
> files, not the index (it lags to generation boundaries). See
> [`zicato-read-telemetry`](../zicato-read-telemetry/SKILL.md).

## The core loop: build → baseline-run → audit

1. **Build/locate the board.** Confirm it loads and validates:
   `.venv/bin/zicato board list --workspace .zicato`.
2. **Run a KNOWN baseline.** Wire two stub candidates whose behavior you can
   predict a priori — a **trivially-correct stub** (should pass every entry with
   an expectation) and a **trivially-wrong stub** (should fail them). Use the
   deterministic mock-target path (see [`zicato-bootstrap`](../zicato-bootstrap/SKILL.md),
   the `mocks:harness_llm` / `mocks:aux_llm` callables) so no model budget is
   spent and the behavior is fixed. The point is that *expected verdicts are
   known before you look*.
3. **Audit the run for harness mechanics**, explicitly *not* for whether the
   baseline "did well". Walk the checklist below against the run directories the
   baseline produced.

## The audit checklist

Each item: what to check, why, and the pass/fail bar.

- [ ] **Baseline sanity.** The known-correct stub passes and the known-wrong
      stub fails. If not, **stop** — the board is broken regardless of anything
      else, and no other finding can be trusted.
- [ ] **GT existence / winnability.** Every ground-truth id the expectation
      depends on resolves to a real entry in the corpus, and a one-of-many-valid
      answer is graded as an equivalence class — not a single cherry-picked id.
      Bar: no expectation demands an arbitrary one of several equally-correct
      answers; no GT id is absent from the corpus. (The corpus is the project's,
      not zicato's — verify GT ids against *your* corpus index; zicato cannot.)
- [ ] **Graded-artifact fidelity.** The slice the expectation reads (`reads:
      "final_output"` or `"conversation_end"`) is the agent's *actual* output,
      confirmed by diffing it against the raw `events.jsonl` transcript. It is
      NOT a self-summary / proxy (a `report_task_completed` field, a
      "completed_results" summary) unless that field provably equals the real
      output. The same rule binds **process judges**: a `python` judge that
      grades the deliverable must read the real **tool-call ledger**
      (goldfive's `ctx.session_state.recent_events`, `kind == "tool_observed"`
      — `tool_name` / `result_preview` / `error_message`) or the written
      artifact, NOT the model's reasoning narration / completion summary.
      goldfive dispatches custom judges only at *reasoning* observation points,
      so a judge that scans `ctx.reasoning_text` / the transcript for tool names
      is grading narration the agent *wrote*, not the tool round-trips it *ran*
      — a judge that fires off narration is the process-side proxy bug (worked
      fix: `file_findability` in
      `examples/zicato_examples/target_1_presentation/judges.py`). See
      [`zicato-design-judges`](../zicato-design-judges/SKILL.md).
- [ ] **Judge-fire counts.** Every judge declared in `board.jsonl` fired ≥ 1
      time across the baseline run. A 0-fire judge is mis-wired or its criterion
      is unreachable — dead weight that gives false coverage, never "passing".
      **Automated:** `zicato health` flags this as a `[WARNING] dead_judge`
      finding (see the recipe below).
- [ ] **Scalar hand-check.** For 2–3 runs, confirm `gen_score.json`'s
      `scalar_components` sum to `scalar`, and that sorting by `scalar` agrees
      with the metric you actually care about. No single component (e.g. `drift`)
      should silently dominate and invert the intended ranking.
- [ ] **Determinism.** Re-run identical behavior; confirm an identical verdict.
      Confirm previews/inputs handed to judges are not truncated below the
      signal needed to grade. **Partly automated:** `zicato board judges
      --test-retest` measures each judge's self-disagreement on one frozen
      transcript, and `zicato board audit` measures the whole evaluation's A/A
      noise floor (see the recipes below).
- [ ] **Monotonicity scope matches intent.** The gate's pass-rate
      monotonicity (the no-pass-regression rule) honors a
      `pass_rate_monotonicity_scope` in `scoring.json`: `per_entry` (default —
      *no individual* board entry the champion passed may regress; right for
      invariant/regression boards) or `aggregate` (only the *board-wide
      pass-rate* may not drop; right for sampled evaluation boards). The on/off
      switch is still the `pass_rate_monotonicity` bool — there is no `off`
      scope. Confirm the scope the board's verdicts were decided under matches
      what you intend; the *values/formula* live in
      [`zicato-tune-scoring`](../zicato-tune-scoring/SKILL.md).

## Diagnostic recipes (copy-paste, grounded in real artifacts)

Set `EP` to the epoch and run from the workspace root. All read-only.

```sh
EP=$(cat .zicato/current_epoch)                         # the active epoch id
RUNS=.zicato/epochs/$EP/generations                     # generations root
```

**1. Judge-fire counts — catch dead judges.** Sum each custom-judge attribution
across every run of the epoch. A judge declared on the board but absent here
fired 0 times:

```sh
jq -r '.drift_counts[] | select(.kind|startswith("custom:"))
       | "\(.kind|ltrimstr("custom:"))\t\(.count)"' \
   "$RUNS"/*/runs/*/loss.json \
 | awk -F'\t' '{c[$1]+=$2} END{for(j in c) printf "%-24s fired=%d\n", j, c[j]}'
# compare against the DECLARED set:
jq -r 'select(.board_meta|not) | (.judges // [])[] | .name' \
   .zicato/epochs/$EP/board.jsonl | sort -u
# any declared name missing from the first list fired 0 times.
```

This whole cross-reference is automated — prefer it:

```sh
.venv/bin/zicato health --workspace .zicato     # [WARNING] dead_judge lists every 0-fire declared judge
```

**2. Per-entry graded artifact vs the verdict — see what was graded.** For one
generation, dump each entry's pass/fail, the matcher kind, the matcher's detail
(why it passed/failed), and the drift loss:

```sh
jq -r '[.entry_id, (.pass_fail|tostring), .expectation_result.kind,
        (.expectation_result.detail//""), (.drift_loss|tostring)] | @tsv' \
   "$RUNS"/v0/runs/*/loss.json | column -t -s $'\t'
```

**3. Confirm the graded field == the agent's real output — catch the proxy
bug.** The expectation reads `final_output` / `conversation_end`; the raw
`events.jsonl` carries the agent's actual final reply. Diff them for the same
entry — divergence means you are grading a summary, not the work:

```sh
ENTRY=recall_q1
# the agent's real final reply (last text event in the canonical stream):
jq -rs 'map(.payload // .) | map(select(.text? // .content?)) | last
        | (.text // .content // empty)' \
   "$RUNS"/v0/runs/$ENTRY/events.jsonl
# what the matcher actually graded is what `reads:` points at; if a predicate
# read a self-summary field instead of this, the two will not match.
```
(The exact event shape is goldfive's; `events.jsonl` writes two envelope shapes
— a camelCase `{...DecisionMade}` and a normalized `{kind,payload,...}` — see
[`zicato-read-telemetry`](../zicato-read-telemetry/SKILL.md). The point is to
read the *real* transcript, not trust a downstream summary the predicate
consumed.)

For a **process judge** the analogous proxy bug is grading the *tool-call
ledger* vs the narration: confirm a deliverable-grading `python` judge reads
`ctx.session_state.recent_events` (`kind == "tool_observed"`), not
`ctx.reasoning_text` / the transcript. A `tool_observed`-derived signal is
ground truth; a tool name found in narration is not. Spot-check by listing the
run's real tool round-trips and confirming the judge's verdict tracks those,
not the model's chatter:

```sh
ENTRY=recall_q1
# the real tool ledger goldfive records (one entry per tool call):
jq -rc 'select((.kind // .payload.kind) == "tool_observed")
        | {tool: (.payload.tool_name // .tool_name),
           err:  (.payload.is_error  // .is_error)}' \
   "$RUNS"/v0/runs/$ENTRY/events.jsonl
# a judge that fires on a tool name appearing in reasoning text — but with NO
# matching tool_observed entry here — is grading narration, not the ledger.
```

**4. Recompute the scalar from its components — catch a mis-weighted/inverted
scalar.** `scalar_components` sum exactly to `scalar`; confirm the arithmetic
and that no component swamps the rest:

```sh
jq -r '.generation_id as $g
       | (.scalar_components|to_entries|map(.value)|add) as $sum
       | "\($g)  scalar=\(.scalar)  Σcomponents=\($sum)  "
         + (.scalar_components|to_entries|sort_by(-.value)
            |map("\(.key)=\(.value)")|join(" "))' \
   "$RUNS"/*/gen_score.json
```

**5. Confirm the ranking matches the metric you care about.** Sort generations
by `scalar` (lower=better) and eyeball that the order matches pass-rate / the
real objective — not just drift:

```sh
for g in "$RUNS"/*/gen_score.json; do
  jq -r '"\(.scalar)\t\(.generation_id)\tpass_rate=\(.pass_rate)\tdrift=\(.scalar_components.drift)\tpass=\(.scalar_components.pass)"' "$g"
done | sort -n | awk -F'\t' '{printf "%s  scalar=%s  %s  %s  %s\n",$2,$1,$3,$4,$5}'
# RED FLAG: the higher-pass-rate generation has the WORSE (larger) scalar
# because `drift` dominates — the board ranks by drift, not the metric you want.
# Fix in zicato-tune-scoring (drift_weight / pass_weight / per_kind_weights).
```

**6. Per-judge weighted loss — which judge drove a run's loss.** When a judge
fires, `loss.json` carries its weighted attribution (use it to spot a single
judge swamping the scalar):

```sh
jq -r '.per_judge_loss[] | "\(.judge_name)\traw=\(.raw_loss)\tw=\(.weight)\tweighted=\(.weighted_loss)"' \
   "$RUNS"/v0/runs/*/loss.json
```

**7. Determinism check — identical behavior → identical verdict.** Re-run the
same deterministic baseline into a second workspace, then diff the verdicts:

```sh
diff <(jq -S '{entry_id,pass_fail,drift_loss,drift_counts}' run_a/loss.json) \
     <(jq -S '{entry_id,pass_fail,drift_loss,drift_counts}' run_b/loss.json)
# any diff under identical deterministic behavior means the board is noisy:
# nondeterministic matcher/judge, or a truncated preview dropping the signal.
```

## Automated coverage — let tooling catch what it can

zicato's `zicato health` already automates several board-correctness checks
(read [`zicato-diagnose-health`](../zicato-diagnose-health/SKILL.md) for the
full list). Run it first; it is cheap and read-only:

| Audit item | Automated by `zicato health` |
|---|---|
| Judges that never fire | **`dead_judge`** (`[WARNING]`) — declared judge with 0 fires across the epoch |
| Dead board entries (no discrimination) | `non_differentiating_entry` (`[WARNING]`) |
| Drift telemetry not reaching the reducer | `flat_drift_signal` (`[WARNING]`/`[CRITICAL]`) |
| Scoring not distinguishing candidates | `degenerate_scoring` (`[CRITICAL]`) |
| Pass/fail side mostly absent | `no_expectations` (`[INFO]`) |
| A judge that disagrees with itself | `zicato board judges --test-retest` (separate command) |
| The board cannot out-signal its own noise | `zicato board preflight` / `board audit` (separate commands) |

```sh
.venv/bin/zicato health --workspace .zicato; rc=$?
# exit 1 == a critical finding is present; do NOT trust the lineage until fixed.
```

Two more read-only checks automate audit items `health` does not cover:

```sh
# judge determinism — a judge that disagrees with ITSELF on one frozen
# transcript is pure noise in every custom:<name> drift it emits:
.venv/bin/zicato board judges --workspace .zicato --test-retest \
    --auxiliary-call-llm my_pkg.llms:aux
# the contract's A/A noise floor + whether a deliberate degradation can
# out-signal it (verdict ok / warn / inert / refuse; recommend-only):
.venv/bin/zicato board preflight --workspace .zicato \
    --harness-call-llm my_pkg.llms:harness --auxiliary-call-llm my_pkg.llms:aux
```

`board preflight` spends a small measurement budget (K A/A draws plus the
degradation probes), so it is not free the way `health` is — but a `warn`
(every probe scored identically: the board is saturated) or `refuse` (the
achievable signal is at or below the floor) verdict is the mechanised form of
"this board cannot resolve the difference you are asking it about."

What `zicato health` does **not** yet automate — do these by hand (recipes 2,
3, 5 above): **graded-artifact fidelity** (is the predicate reading the real
output or a proxy?), **GT winnability** (is the GT a real, non-arbitrary
corpus id?), and the **scalar-inversion ranking check**. These need the
project's corpus and the operator's intent, which the harness cannot infer.
(A board-winnability / GT-existence preflight is a reasonable future addition,
but it is inherently project-specific — see "Recommendations" below.)

## Red flags (any one means stop and audit)

- Pass rate is flat or zero across clearly-different candidates.
- Pass/fail appears uncorrelated with obvious answer quality.
- A `zicato health` `dead_judge` finding (a judge fired 0 times).
- A ground-truth id does not exist in the corpus, or demands one arbitrary
  answer out of many valid ones.
- The higher-pass-rate / obviously-better generation gets the **larger**
  (worse) `scalar` — `drift` dominating `pass`.
- Re-running identical deterministic behavior yields a different verdict.

## When to use / when not to use

- **Use when:** you just built or changed a board; before running any
  tournament/evolution on it; whenever a verdict surprises you; pass rates are
  flat/zero; a "good" candidate scores worst; judges seem to contribute nothing.
- **Do NOT use when:** you already trust the board and only want to compare
  candidates — that is candidate eval, not board audit. This skill deliberately
  ignores efficacy.

## When to escalate / re-baseline

- **Fix the board** when an audit item fails structurally: wrong `reads:`
  target, a proxy-graded field, an unwinnable GT, a dead/mis-wired judge, an
  inverted scalar. Board / scoring edits roll the epoch — make them deliberately
  (see [`zicato-author-board`](../zicato-author-board/SKILL.md) /
  [`zicato-tune-scoring`](../zicato-tune-scoring/SKILL.md)).
- **Re-run (don't edit)** when only the determinism check fails and the run was
  nondeterministic by nature (e.g. a `rubric` matcher on the aux LLM): raise
  `replicates` or `promote_margin` rather than treating one noisy verdict as a
  board defect.

## Recommendations (not yet built)

- A **GT-existence / winnability preflight** (`zicato board verify` reading a
  project-supplied corpus index, flagging GT ids absent from the corpus and
  expectations that demand a single one-of-many answer). Not to be confused with
  the shipped `zicato board preflight`, which measures *noise vs achievable
  signal*, never whether a ground truth exists. Genuinely useful, but
  inherently project-specific — the corpus is not zicato's — so it would need a
  pluggable corpus-resolver hook. Left as a recommendation rather than built.
- A **graded-artifact-fidelity lint** that warns when a `predicate` spec's
  `reads` target is a known summary/proxy field name. Cheap heuristic, but
  false-positive-prone without a per-project allowlist.
