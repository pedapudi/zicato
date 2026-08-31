# Process exemplars — drift-anchored event windows for the proposer

> **Status.** Implemented. The channel comprises the extractor
> (`zicato/analyzer/process_exemplars.py`), the opt-in contract knob
> (`ProposerQualityConfig.process_exemplars`, default **0 = off**,
> omit-at-default), and the prompt block in both proposer engines. This
> channel touches the overfitting boundary (OVERFITTING.md §11), so the
> redaction rules below are the normative contract: **every rule maps to a
> mechanical function with its own test — there is no LLM redactor.**
> Section 5 is the operator runbook for detecting harm.

## 1. What the existing failure channels omit

The proposer's restricted prompt carries three failure channels, each
narrowed (OVERFITTING.md §11):

| channel | tells the proposer | granularity |
|---|---|---|
| detector patterns (`patterns/detectors.py`) | *that* a failure shape recurs ("`looping_tool_call` fires in ~40% of runs across 4 entries") | aggregate counts/rates, entry ids stripped at render |
| outcome marginals (OVERFITTING.md §11.5, `analyzer/outcome_marginals.py`) | *what* the wrong answers look like in aggregate (over-retrieval vs misses vs empty), sanitized + banded | board-wide rates only |
| experiment memory | *what was already tried*, Δscalar bucketed | per-experiment, banded |

None of these shows **how a failure unfolds**: the plan step that wandered,
the tool call that looped, the steering decision that fired too late. That
sequence lives in each run's goldfive `events.jsonl`, the same stream the
reducer digests into a `LossProfile` and then discards. A proposer told
only "looping fires in 40% of runs" has no view of the mechanism it is
editing against; a proposer shown a single **redacted window of events
around one representative drift** does.

A **process exemplar** is that window: one anchor event chosen for one
detected pattern, plus the few events on either side, passed through a
mechanical redaction layer and rendered as a clearly-bounded prompt block.

## 2. What is extracted

`extract_process_exemplars(workspace_root, epoch_id, patterns, cap=2, *,
parent_generation_id, train_entry_ids)` — a pure read over the **current
champion's** (`parent_generation_id`) `events.jsonl` files, restricted to
the **train slice** the caller passes (the orchestrator threads the same
`split_board` / `rotation_seed` partition it already uses for the patterns,
loss summary, and outcome marginals; the extractor never reads the board
or widens the slice it is given).

For each detected pattern, **at most one** anchor event, found by kind:

| pattern kind | anchor event |
|---|---|
| `drift_kind_frequency` / `drift:*` metric-frequency | first `drift_detected` whose (normalized) kind matches the pattern's drift kind |
| `hot_agent` | first `drift_detected` whose `current_agent_id` matches the pattern's agent |
| `hot_task` | first `task_failed` / `task_blocked` whose `task_id` matches the pattern's task |
| `plan_revision_instability` | first `plan_revised` |
| anything else (cost/rubric frequencies, multi-turn heuristics, …) | **no anchor — skipped** (their failure shape is not localized to an event) |

The window is the anchor plus **3 events on each side** in file order
(`_WINDOW_RADIUS = 3` — a plan step, a tool/agent invocation, the drift
finding, the steering response typically fit). Selection is fully
deterministic: entries are scanned in sorted-entry-id order (or straight to
the pattern's own entry for `hot_task` / `hot_agent`), the first match
wins, no RNG, no wall clock. At most `cap` exemplars total (default **2**,
matching the outcome-marginal channel's entry cap), taken in the detector
registry's deterministic pattern order, deduplicated by anchor position.

**Refresh semantics.** Because extraction is a pure function of
(pattern-id set, champion's train-slice event files), the block is
**byte-identical round over round** while the pattern set and champion are
unchanged, so re-presenting it leaks nothing new. It changes only when the
pattern set changes or a promotion replaces the champion, which supplies a
different run set. The leakage budget is therefore at most 2 windows **per
(champion, pattern-set) state** rather than per round.

## 3. Redaction rules (normative)

The design invariant, inherited from OVERFITTING.md §11: the proposer may
learn **how failures unfold** but never **which entries fail** or what their
task text or outputs were. Redaction is default-deny and entirely mechanical.

**Rule R1 — payload allowlist, default-deny.** Each event renders as its
window offset (`-3…+3` — never the absolute sequence number, which could
fingerprint an entry) plus its payload case name, plus **only** the fields
the policy table below admits. A payload case not in the table renders as
its **bare case name with no fields** — the window's shape survives, its
content does not. Envelope fields (`event_id`, `run_id`, `session_id`,
timestamps) are always dropped.

Dispositions: **K** = kept verbatim (closed vocabulary or structural),
**T** = truncated free text, **A** = anonymized identity, **D** = dropped.
Unlisted fields of listed cases are dropped.

| payload case | kept (K) | truncated (T) | anonymized (A) | dropped (D) — the load-bearing ones |
|---|---|---|---|---|
| `drift_detected` | kind, severity, lifecycle, authored_by, current_agent_id | detail | current_task_id | **trigger_input** (raw input), ids |
| `plan_revised` | drift_kind, severity, revision_index, target_agent_id, dry_run; plan → **structure only**: `N tasks, M edges` | reason | — | refine_input_summary / refine_output_summary (model text), diff, task titles/descriptions |
| `plan_submitted` | plan → structure only: `N tasks, M edges` | — | — | task titles/descriptions/assignees' task ids |
| `task_started` | — | — | task_id | **detail** (the task description = board text) |
| `task_failed` | recoverable | reason | task_id | — |
| `task_blocked` | — | blocker, needed | task_id | — |
| `task_cancelled` | — | reason | task_id | — |
| `agent_invocation_started` | agent_name | — | task_id | invocation ids, timestamps |
| `agent_invocation_completed` | agent_name | — | task_id | **summary** (model output), timestamps |
| `steering_decision_made` | detector_name, outcome, considered/chosen severity + intervention level, agent_name | reason | task_id | score (fine numeric), drift_id, ids |
| `reasoning_judge_invoked` | on_task, severity, classification, subject_agent_id | reason | task_id | **reasoning_input / raw_response** (raw model text) |
| `judgement_emitted` | judge_name, verdict_kind, drift_kind, severity, metric_name | — | — | **detail** (judges quote the answer), rubric_score / numeric_value / boolean_result (per-entry outcome numbers — the outcome-marginal channel bands those) |
| every other case (`run_started`, `run_completed`, `task_progress`, `task_completed`, llm-call bookends, …) | — | — | — | **all fields** (bare case marker only; `run_started.goal_summary` IS the task prompt, `*_summary` fields ARE model output) |

Why the K column is safe: every kept field is either a **closed
vocabulary** (drift kinds, severities, verdict kinds, outcomes,
intervention levels), a **structural count** (task/edge/revision counts),
or **harness-side identity** (agent names, judge names, detector names).
Those components are the ones the proposer is meant to edit and are already
fully exposed through the mutation manifest; they are contract identity
rather than board identity.

**Rule R2 — identity anonymization.** `entry_id` (the events-path key)
never appears in any output field. Task ids and invocation ids are
**window-local tokens** — each distinct id maps to `task-1`, `task-2`, …
in order of first appearance *within one window* — so "the same task keeps
failing" stays visible while nothing correlates across windows, rounds, or
to the board.

**Rule R3 — free-text truncation.** Every T-class field is capped at
`_FREE_TEXT_LIMIT_CHARS = 160` with head/tail elision: the first 120 and
last 24 characters joined by ` … `. T-class fields are process narration
authored by goldfive's own detectors, judges, and steerer rather than task
or model text, which is why they are admitted at all.

**Rule R4 — identity-corpus scrub (defense in depth).** Before truncation,
every T-class value is scanned against an **identity corpus** built from
the same run: the entry id, run/session ids, every raw task/invocation id,
and **every D-class text value** (goal summaries, task descriptions,
completion/output summaries, trigger inputs, judge details, raw model
text). Any corpus string of ≥ `_MIN_SCRUB_LEN = 12` chars found verbatim
inside a kept text — and any identity token of any length — is replaced by
`[withheld]`. A drift detail that *quotes* the task prompt therefore loses
the quote mechanically. The payload allowlist is the primary guarantee; the
identity-corpus scrub exists because a free-text field can contain
anything.

**Residual risk.** The identity-corpus scrub catches verbatim quotation but
not paraphrase. A goldfive detector that *paraphrases* task content into a
drift detail can leak a fragment of meaning below the corpus threshold.
That residue is bounded by the free-text truncation's 160-character cap, by
the limit of 2 windows per state, and by the §5 runbook. An operator who
cannot accept it leaves the knob at 0.

## 4. Why this stays inside OVERFITTING §11

The mechanism of OVERFITTING.md §11 is to narrow the channel: aggregate,
band, and
**withhold the failing inputs themselves** so the optimizer must produce a
general fix. Exemplars extend the channel from *that a failure recurs and
how often* (patterns) and *what the wrong answers look like in aggregate*
(outcome marginals) to *how the failure unfolds*, while keeping the joint
withheld:

- **No entry identity** (the payload allowlist and identity
  anonymization): the proposer cannot special-case a board entry it cannot
  name. Task text and outputs never render, even when quoted inside
  process text, because the identity-corpus scrub removes the quote.
- **Anchored on already-released information:** an exemplar exists only
  for a pattern the detectors already surfaced. The anchor adds mechanism
  to a failure shape the proposer was already told about, and carries no
  signal about entries it has not been told about.
- **No response surface:** the block contains no per-event scores, no fine
  numerics, and no absolute sequence positions. It is byte-stable across
  rounds under an unchanged (champion, pattern-set) state, so it cannot be
  used to read round-over-round board movement.
- **Promotion is still guarded downstream:** whatever the proposer learns,
  the train/holdout split, the Ladder-mediated holdout confirmation, and
  the `generalization_gap` detector gate the outcome under unchanged
  rules. This channel changes what the prompt renders, and never how
  zicato evaluates.

**The asymmetry with screening.** `screen_entries` ships scaffold-on
because tryouts are evaluation-side: they consume board runs but reveal
only a veto. `process_exemplars` is **not in the scaffold** and defaults to
0 because it widens the proposer-visibility channel, the boundary this
codebase guards most strictly. The operator opts in with the §5 runbook in
hand. Being omit-at-default, the knob never rolls an epoch that leaves it
unset; setting any non-zero cap rolls the epoch, which is correct, because
a proposer shown process windows proposes under a different rule.

## 5. The empirical harm-detection protocol (operator runbook)

The redaction argument above is structural; whether exemplars *actually*
push a given board toward memorization is an empirical question. Enable
the knob only under this protocol:

1. **Baseline.** Note the champion's current train/holdout losses and the
   absence (or level) of any `generalization_gap` finding in the round
   health report.
2. **Enable.** Set `"proposer_quality": {"process_exemplars": 2}` in the
   contract (the epoch rolls — expected). Keep the placebo arm on
   (`overfitting.random_baseline_every_n`, e.g. 5) so gate discrimination
   stays independently monitored.
3. **Watch two alarms, every round:**
   - the **`generalization_gap`** health finding
     (`health/diagnostics.py::detect_generalization_gap`): *train loss
     improving while the train→holdout gap widens past the warn/crit
     thresholds* is the memorization signature this channel could cause;
   - a **promoted placebo** (`placebo_promoted`, CRITICAL): the gate has
     stopped discriminating; any recent wins — exemplar-informed or not —
     are suspect.
4. **Alarm condition** = the gap detector fires warning-or-worse **and**
   train-side loss kept improving over the same generations. Improving on
   train while generalization worsens is the harm this channel could
   cause.
5. **Response.** Set `process_exemplars` back to `0`. The knob is a
   contract field, so the epoch rolls, the holdout rotates
   (`rotate_holdout`), and the suspect lineage stops being mined. Keep the
   generations minted under the enabled knob out of any cross-epoch memory
   opt-in until the gap re-narrows.

A smoke test pins step 3's alarm mechanically: a rigged widening gap must
fire `detect_generalization_gap`, so the runbook's alarm cannot fail
silently.

## 6. Surfaces

- **Extractor:** `zicato/analyzer/process_exemplars.py` —
  `ProcessExemplar` (frozen) + `extract_process_exemplars(...)`; every
  §3 rule is its own function with its own test.
- **Contract:** `ProposerQualityConfig.process_exemplars: int = 0`
  (0 = off; a positive value is the per-round cap), validated `>= 0`,
  listed in `_SCORING_OMIT_AT_DEFAULT_FIELDS`, **not** set by the scaffold.
- **Threading:** the orchestrator extracts **best-effort** (an extraction
  failure logs and renders nothing — it can never abort a round) →
  `ProposerContext.process_exemplars: str` (pre-rendered block body, empty
  = omit) → both engines splice a `## Process exemplars` section
  **directly after the failure-mode profile block**, headed by a banner
  restating the redaction contract. An empty block is omitted, so the
  knob-off prompt and contract hash are unaffected; both are pinned by
  test.
- **Builder:** `set_proposer_quality` gains the knob (op + API dispatch +
  copilot tool); the cost meter is untouched — extraction is a read of
  events already on disk, zero board runs, zero LLM calls. The
  builder-copilot skill carries one honest paragraph, including the §5
  runbook pointer.
- **RoundLog:** no entry. A process exemplar is prompt-side input rather
  than a round event, on the same footing as the failure-mode profile.

Sample of the rendered block (redacted, anonymized, offsets relative):

```
## Process exemplars (train slice — redacted event windows)
Redaction contract (docs/design/PROCESS-EXEMPLARS.md §3): entry ids and
task text stripped, task ids anonymized per window, free text truncated,
model outputs withheld. These show HOW a detected failure unfolds — never
WHICH board entry it unfolded on.

- exemplar 1/2 — pattern drift_kind_frequency (drift kind 'looping_tool_call'):
    -3 plan_submitted plan=3 tasks, 2 edges
    -2 agent_invocation_started agent=researcher task=task-1
    -1 task_started task=task-1
     0 drift_detected kind=looping_tool_call severity=warning agent=researcher task=task-1 detail="tool called 4 times with identical arguments"
    +1 steering_decision_made detector=looping outcome=intervene chosen_severity=warning agent=researcher
    +2 plan_revised drift_kind=plan_divergence severity=warning revision_index=2 reason="collapse repeated search into one step" plan=3 tasks, 2 edges
    +3 task_failed task=task-1 reason="budget exhausted after repeated calls" recoverable=true
```
