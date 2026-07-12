# zicato — functionality improvement recommendations

> **Status: proposal / recommendations — not implemented.** A functionality-focused
> review of how to make zicato *better* (tournament reliability & efficiency, the
> Rust/Python boundary, proposal-generation quality, and candidate
> rating/winner-resolution). Companion to the behavior-preserving
> `REIMPLEMENTATION.md` design note; these are *intentional functionality changes*
> and can proceed in parallel. Grounded in an audit of the live code, the Rust
> crate, and the design docs.

## Context

A functionality-focused review (distinct from the behavior-preserving cleanup in
`REIMPLEMENTATION.md`) of how to make zicato **better**, not just cleaner. Four
fronts were investigated against the live code + Rust crate + design docs:
(1) running tournaments reliably, (2) running them efficiently, (3) the Rust vs
Python boundary, (4) proposal-generation quality, and (5–6) capabilities that are
documented but unbuilt — including the explicit asks: *can tournaments generate
Elo?* and *how should Bradley–Terry be used?*

Two findings were verified directly against source before being headlined:

- **VERIFIED — the Rust watchdog kills the orchestrator.** `decide_heartbeat`
  returns `Kill { pid }` for the heartbeat (orchestrator) pid past the stale
  threshold (`crates/supervisor/src/watchdog.rs:99`) and `heartbeat_loop`
  escalates SIGTERM→SIGKILL on it (`watchdog.rs:262-264`) with **no `protected`
  set** (that set is built only in the run loop, `watchdog.rs:307`). The safety
  docs (RUNTIME.md §3.2, ROBUSTNESS.md §2.4) promise this never happens. It does.
- **VERIFIED — multi-challenger + holdout-through-structures is shipped**
  (`orchestrator.py:1461` `_evolve_multi_challenger`, `:1541` holdout split,
  `:1924` `confirm_crowning_holdout`; CLI flag `evolve.py:574`). Earlier "pending"
  notes are stale.

---

## 0. Headline: stop the watchdog from killing the orchestrator (do first)

A self-improving harness whose own watchdog kills the loop on a slow LLM call —
while its docs claim immunity — is the worst possible reliability failure: the
system's self-model is wrong about its own safety. **Fix:** make
`decide_heartbeat` warn-only (`Warn`/`MissingHeartbeat`, never `Kill`) and delete
the `escalate()` in `heartbeat_loop` (`watchdog.rs:262-264`). Orchestrator restart
is a process-supervisor concern, exactly as the docs already state. **Impact H,
Effort S, Risk ~none** — it restores documented behavior. This is the single
highest-leverage change in this entire document.

---

## 1. Running tournaments reliably

How a tournament runs today: each `(side, entry, replicate)` is one isolated
subprocess (`python -m zicato._tournament_worker`) over an ephemeral copytree of
the generation snapshot, bounded by an `asyncio.Semaphore(parallelism)`; the
parent enforces budget via `wait_for(budget+30s)` then SIGTERM→SIGKILL; a Rust
supervisor independently watches deadlines and staleness. The reliability gaps,
ranked:

| # | Fix | Why | Impact / Effort |
|---|---|---|---|
| 1 | **Watchdog warn-only for the orchestrator** (§0) | A slow endpoint currently kills the loop; contradicts the docs | H / S |
| 2 | **PID-identity check (start-time), not bare `os.kill(pid,0)`** in both the Python lock (`lock.py:92-125`) and Rust `is_alive` (`signal.rs`) | A recycled PID makes a dead worker look alive (kill declined) or an innocent process look like the lock owner (steal/refuse-start) | H / S |
| 3 | **Capture an `abort_cause` enum** (`parent_kill` / `gone_no_result` / `nonzero_exit:{code}`) on the synthesized aborted `LossProfile` (`runner.py:1174-1193`) | Today the 3 causes are indistinguishable, so loop-health cannot tell a real agent infinite-loop from a transient crash from *our own watchdog over-firing* — the exact signal the loop needs | M / S |
| 4 | **Don't cache infra-aborts.** Only cache genuine wall-clock-budget exhaustion; parent/supervisor-kill and crash aborts must not become a permanent worst-case cache HIT (`runner.py:1707-1723`, `2843-2873`) | An infra blip currently poisons a board unit's score for the rest of the epoch; only `--mode full` recovers it | M / S |
| 5 | **In-worker SIGALRM hard stop** (`signal.setitimer(ITIMER_REAL, budget)`) as a true in-process budget floor (`_tournament_worker.py:589-604`) | The worker budget is cooperative `asyncio.wait_for` only; a GIL-holding C-extension or `while True` never yields, so a wedged run depends entirely on two healthy outer processes | M / S |
| 6 | **Pass `match_id` into the worker** so it writes `loss.json` once, atomically, instead of the parent rewriting it post-exit (`runner.py:1252-1257`) | A parent kill between worker-exit and rewrite leaves a `match_id`-less cached loss → wrong provenance on reindex | L / S |
| 7 | **Ship the conservative resume protocol** (markers exist at RUNTIME.md §4; reading them doesn't) | A mid-tournament kill loses in-flight work to re-runs; the unit cache already makes resume nearly free (completed `loss.json` = HIT) | M / M |
| 8 | **Single state-file owner on kill.** Make both Rust kill paths leave the run state file for the orchestrator reaper (`watchdog.rs:365-368` vs `398-404` disagree) | A double-trigger can delete the file the reaper needed for finalization | L / S |

---

## 2. Running tournaments efficiently

| # | Speedup | Cost removed | Impact / Effort |
|---|---|---|---|
| 1 | **Default to git-worktree snapshots** (`git_genstore.py` — already implemented, opt-in). A worktree checkout is content-addressed (dedups blobs across N generations) and *is* the isolated per-run tree | Removes both the per-generation `copytree` (`genstore.py:396`) and the **per-run** ephemeral `copytree` (`runner.py:576`) — currently O(board×2×replicates) copies/round | H / M |
| 2 | **Warm Python-interpreter pool, Rust-managed** (the worker is the payload, Rust is the cage — see §3a). Long-lived interpreters that import the ADK adapter once and accept board units over a framed pipe | Kills the 100-500ms adapter import × every `(side,entry,replicate)` — ~4-14s/round of pure overhead on a 10-entry board (ROBUSTNESS.md §2.3) — and consolidates the kill/budget path | H / L |
| 3 | **Cache-read the immutable champion side** in `run_tournament` instead of `force_fresh=True` for both sides (`runner.py:2419`) | The gauntlet champion is immutable within an epoch yet is re-run every round; fast-mode already does this, the rigorous path needlessly doesn't | M / S |
| 4 | **Re-enumerate only touched files** after a patch, not the whole tree twice (`mutation/validator.py:117,220-223`) | A full AST re-parse of the mutable tree per applied patch | M / S |
| 5 | **Cross-matchup parallelism** for swiss/elim/racing: lift the semaphore to span concurrent matchups of a round (`orchestrator.py:1737` runs them serially) | Worker-spawn + snapshot overhead is re-paid serially per matchup today | M / M |
| 6 | **One `ScoringWeights` JSON serde** replacing the hand-aligned `_weights_spec`/`_weights_from_args` pair (`runner.py:692-744` ↔ `_tournament_worker.py:685-763`) | Not just speed — the two must stay byte-aligned or the worker silently scores under defaults (the documented `per_judge_weights` desync) | M / S |

---

## 3. Rust vs Python — recommended boundary

Today Rust (`crates/supervisor/`, ~one crate) owns the watchdog (pure decision
functions + two tokio loops), SIGTERM→SIGKILL escalation, a partial-write-tolerant
state reader, an `axum` `/statusz` + dashboard API, and a `rusqlite` **read-only**
index reader. That rationale (separate runtime immune to a Python GIL wedge,
~5-20ms startup, memory-safe) is correct and should be extended:

- **Rust should own all out-of-process *enforcement*, as one policy:**
  - The **single kill escalator.** Today the SIGTERM→SIGKILL protocol is
    duplicated in Python (`runner.py:904-931`) *and* Rust (`signal.rs:49-79`) with
    separate grace constants, racing over the same worker PID. Collapse to one
    Rust killer; the Python parent *requests* a kill via a control marker. One
    escalation policy, no parent/supervisor PID races.
  - A new **per-evolve budget killer** in Rust reading a `deadline` from the
    heartbeat — the only honest whole-invocation budget, since the Python
    `asyncio.wait_for` can't pre-empt a wedged orchestrator.
  - The per-run deadline killer stays where it already is (Rust).
- **Rust should own a new event-stream tailer** (`events.jsonl` incremental tail
  beside the existing `notify` watcher) — IO-bound, must survive an orchestrator
  wedge, feeds the live drift view and flat-drift loop-health.
- **Rust keeps all *read* surfaces** that must work when the orchestrator is
  broken: `/statusz`, index analytics, state snapshots.
- **Python keeps all *policy & authoring*:** proposer, scoring/gate, applier (AST
  surgery), board, LLM wiring, dashboard UI. These change often and have no
  GIL-wedge exposure — Rust would only slow their iteration.

The first concrete move is removing the duplicated kill protocol so it lives in
exactly one place.

### 3a. Should tournament workers be Rust harnesses? No — Rust owns the cage, Python the payload

A natural question, given the above: rewrite the per-entry **tournament worker**
(`_tournament_worker.py`) in Rust? **No.** The worker's job is to run the
*candidate harness*, and that candidate is a **mutated Python source tree**: the
worker `chdir`s into the generation snapshot, **imports it in-process**, and drives
it through the ADK/goldfive adapter (`adapters/adk.py`) with judges, emulator, and
the loss reducer all in that same interpreter. The system under evaluation *is*
Python — that is zicato's premise (the mutation surface is `# zicato:mutable`
Python spans; the applier does AST surgery). A Rust worker cannot import and run a
Python ADK agent; it would have to spawn a Python child anyway, adding a language
boundary for zero gain.

The reliability intuition ("Rust workers can't wedge") is a mirage: a worker
wedges in **LLM I/O** or in **the candidate's own code**, never in its ~200 lines
of bookkeeping — and those live in the payload you can't rewrite. zicato already
handles a wedged worker correctly: a *separate* Rust supervisor kills it from
outside. You don't strengthen the guard by rewriting the guarded thing. (Nor does
memory-safety pay off for a short-lived, isolated, disposable process — that payoff
is why the *supervisor* is Rust, and it already is.)

So the split is **cage vs payload**: Rust owns the worker *lifecycle* (spawn,
isolation, the single SIGTERM→SIGKILL escalator, per-run budget/deadline,
heartbeat-staleness, IPC framing); Python owns the *run* (importing and driving the
candidate harness, judges, reducer). A Rust `RustHarnessAdapter` becomes worth it
only if zicato ever evolves a *Rust* inner harness — and the `RunnableHarness`
Protocol is already the seam to add one per-adapter, never a global rewrite. Every
dogfood target is Python today, so that case doesn't arise.

**The strong "yes" hiding in the question: a Rust warm-interpreter pool manager.**
Instead of `fork+exec python` per `(side, entry, replicate)`, a long-lived Rust
process keeps a pool of warm Python interpreters (adapter imported once) and
dispatches board units to them. This captures the biggest efficiency win (kills the
per-run import/spawn, §2 item 2) *and* puts spawn/kill/budget/heartbeat in the
memory-safe runtime where enforcement belongs (§3) — while the agent run stays
Python. Concretely:

```
Rust pool manager (one per evolve; the watchdog supervises IT)
  • owns the concurrency cap (replaces the asyncio.Semaphore)
  • keeps warm interpreters keyed by snapshot path
      → one warm set per generation in the duel (gauntlet=2, field of N = N+1);
        keying by path amortizes import across that generation's many
        entries×replicates and sidesteps sys.modules cross-generation collisions
  • per dispatched unit: tracks the deadline; on overrun runs the ONE
        SIGTERM→grace→SIGKILL escalator on that worker pid (no parent/supervisor race)
  • writes active_runs/{run_id}.json + heartbeat the watchdog already reads
  • recycle-on-abort: a worker that aborts/crashes/over-budgets is DISCARDED and
        respawned — never reused — so in-memory residue can't poison the pool

Python worker, new `--serve` mode (`python -m zicato._tournament_worker --serve`)
  • on start: import the ADK adapter once
  • loop: read a length-prefixed frame (4-byte BE len + JSON/msgpack):
        RunUnit { run_id, worktree_path, entry, adapter_spec, role_specs,
                  weights_spec, sink_path, loss_path, budget_s, match_id, seed }
    → chdir worktree_path (a fresh git worktree per run, §2 item 1, so FS writes
      never cross runs even in a reused interpreter)
    → reset RNG from seed; run the entry under asyncio.wait_for(budget_s)
      + a SIGALRM hard floor (§1 item 5)
    → reduce loss in-worker; atomically write loss.json WITH match_id (§1 item 6)
    → reply UnitDone { run_id, loss_summary } | UnitAborted { run_id, cause }  (§1 item 3)
```

Two non-negotiable invariants make this safe: **per-run worktree isolation** (FS)
and **recycle-on-abort** (memory) together reproduce the isolation the current
copytree-per-run buys. **Phasing:** a pure-**Python** pool manager is a legitimate
lower-effort MVP that captures the import-amortization win alone; moving the manager
to Rust is the principled end state that *also* collapses the kill/budget protocol
into one memory-safe owner. Do the Python pool first only if the Rust manager
(which depends on the §3 kill-protocol consolidation) isn't ready — otherwise build
the Rust manager directly, since it subsumes both wins.

---

## 4. Generating great proposals

The default proposer is a tool-using ADK agent with a rich restricted context
(loss summary, valid targets, patterns, mutation points, prior-experiment digest,
failure-mode profile, telemetry insights) and read-only tools
(`list_mutation_points`, `read_mutable_file`, `grep_mutable`, `read_journal`,
`read_insights`), with a parse→validate→retry loop. But generation still has **no
critique and no calibration loop** — and only *partial* diversity pressure
(per-slot edit-class *and* strategy hints exist; per-slot decoding variation
does not; item 4) — the largest untapped quality reservoir. Ranked levers (all stay inside the existing
overfitting-restricted context channels):

1. **Best-of-N + self-critique (top lever).** Sample N experiments per
   propose-step, then a cheap critique pass picks/repairs the best against a
   quality bar (grounded in tools? targets a real failure mode? minimal diff?).
   Today the retry loop only fires on *invalid* output — a valid-but-mediocre
   proposal is never reconsidered. **H / M.** Keep the critic inside the
   restricted prompt context (never holdout).
2. **Hypothesis prediction-accuracy scoring (highest-novelty).** The hypothesis
   already carries falsifiable predictions (`expected_drift_movements`,
   `expected_pass_rate_delta`, `expected_metric_movements`) that are parsed and
   journaled but **never compared to actuals**. Score them after the tournament
   settles and feed the calibration back into experiment memory. Turns decorative
   prediction fields into a real proposer-quality axis. **H / M**, diagnostic-only
   (don't gate promotion on it).
3. **Hard-ish diversity constraint across challenger fields.** EXPERIMENT-MEMORY.md
   §2.2 names the failure: siblings propose the *same* mutation, collapsing a
   field of N into < N experiments. Reject/penalize a challenger whose
   `modulating` id-set + core-idea duplicates a sibling; optionally assign each
   slot a distinct target. Free tournament value. **H / S-M.**
4. **Targeted failure-mode → edit-class prompting — SHIPPED (prompt framing);
   the remaining gap is decoding-parameter diversity.** Both prompt-side
   axes landed. `proposer/best_of_n.py::_sample_slot` threads a DISTINCT
   per-slot edit-class hint (`proposer/hints.py::hint_for_slot` /
   `EDIT_CLASS_HINTS`), conditioning slots `0..N-2` on the failure profile's
   DOMINANT mode (*over-retrieves / misses / empty / looping*) and keeping
   the last slot exploratory; the sibling branch (`origin/prop/quality-levers`,
   `1bf52ca`) then ALSO shipped per-slot *strategy* rotation
   (`hints.py::STRATEGY_HINTS` / `strategy_for_slot` — MINIMAL-SURGICAL /
   STRUCTURAL-REWORK / DEFENSIVE-HARDENING / CONTRARIAN, rotated
   deterministically per `(slot, round)`) composed with the edit-class hint,
   so the N samples inside one best-of-N slate are no longer i.i.d. draws
   from one prompt. What is **still** unbuilt is *decoding* diversity: every
   slot shares one sampling strategy and temperature. The sibling branch
   deliberately did NOT touch this — the `aux_call_llm` seam is
   `(system, user, model) -> str` and carries no sampling params, so the
   variation rides the PROMPT only. Genuine decoding breadth
   (temperature / top-p per slot) is blocked on extending the `CallLLM`
   seam — a real plumbing lift, not a prompt tweak. **M / M.**
5. **Structured per-epoch reflection on rejection *patterns*** (distinct from the
   per-experiment digest, which surfaces instances not patterns). **M / M.**
6. **Richer mutation tooling**: a `read_parent_diff` (what the last promotion
   changed) and `mutation_usage` (where an id's value is referenced) tool, plus a
   soft "ground before proposing" nudge. **M / M.**
7. **Reject semantic retrieval over the journal** for now — epochs are small, the
   relational `prior_experiments_for_epoch` curation is the right abstraction;
   extend it (lever 5) rather than add an embedding dependency + new leak surface.

---

## 5. Documented-but-missing: candidate rating & winner-resolution

> **Status — partially implemented (PR #90 + the evidence gate).** The
> *rating* half of this section has since shipped; the *resolver* half has
> not. Built and live: the visibility rating fold
> (`src/zicato/index/elo.py::fold_elo_into_index` — but as a
> **Bradley–Terry MLE mapped onto the Elo scale**, not the standard-Elo
> update this section first proposed; see the subsection note below), the
> BT rating module (`src/zicato/selection/rating.py::fit_bradley_terry`,
> convex MLE with CIs) and its opt-in θ-rank standings
> (`params["rating"]`), and the BT **uncertainty pre-gate** with
> CI-driven "replicate-first, resolve-second" scheduling
> (`src/zicato/selection/evidence_gate.py` + `selection/driver.py`,
> defer→replicate on the closest-CI duel), and the `resolver` knob —
> Ranked Pairs behind a Smith-set prune (`selection/resolve.py` +
> `standings_ext.py`, opt-in `params["resolver"]`, wired into
> single/double-elim + swiss). Still unbuilt: the maximal-lottery
> resolver (`SELECTION-THEORY.md` remains DESIGN for that). Per-lever
> status is tagged inline below.

Winner-resolution today is two-tier: a per-duel **gate** (scalar margin + pass-rate &
namespace monotonicity + holdout) is the only thing that can promote, and each
structure picks an internal leader by **scalar/Copeland bookkeeping** (Copeland
exists only in swiss) then runs one champion-gate duel. The **selection path**
still carries no global rating — but a read-only visibility rating now folds
over the same ledger post-hoc (below). The complete pairwise-outcome ledger
(`MatchRecord`, `MatchOutcome`, `Standing`, the index `tournaments` table with
per-`match_id` losses) is already persisted — which is exactly what let the
rating layer land with **zero new measurements**.

### Can tournaments generate Elo? — Yes (ship this first)

> **Status — implemented, but via BT (PR #90).** `src/zicato/index/elo.py`
> shipped this read-only fold — however it re-fits `fit_bradley_terry`
> over the de-duplicated match ledger at every reindex/ingest and **maps
> the fitted strength onto the Elo scale** (`elo = 1500 + θ·400/ln 10`)
> rather than running the sequential margin-K Elo update the bullets below
> propose. The BT fit natively carries the CIs (`generations.elo_se`) the
> margin-K approximation lacked, so the "margin-of-victory K-weighting" and
> "provisional-K decay" refinements were superseded (the sub-`MIN_RATING_GAMES`
> `provisional` display suffix stands in for the latter). The
> `generations.elo` / `elo_se` / `elo_games` columns (schema v10 + v12) and
> the standings / gens-roster / candidate-dossier display all shipped.
> Visibility only — never the gate. Racing intermediate rungs contribute
> zero games (no named pairwise winner); a Plackett–Luce set-rating is the
> documented future fix.

Every settled duel is already an Elo "game": winner=1, loser=0, with
`delta_scalar` as a margin. Build a **read-only analytics fold** (`index/elo.py`),
run at index time + on `reindex`, never in the selection path:

- Standard Elo update, processed in `match_id`/`ran_at` order, with two
  zicato-specific refinements the data supports: **margin-of-victory K-weighting**
  (scale K by normalized `|delta_scalar|`, recovering the margin Copeland throws
  away) and **provisional-K decay** for a generation's first games.
- **Store** `generations.elo` / `elo_games` via the established additive
  `_V*_ADDED_COLUMNS` migration (NULL-on-open, re-derived by reindex); optionally a
  `ratings` history table for trajectory plots.
- **Cross-epoch carry-forward**: seed a new generation's Elo at its
  `parent_generation_id`'s rating (a child starts as strong as the parent it was
  derived from), anchor on the champion, and **flag epoch-roll boundaries** on the
  chart (duels across a contract roll are measured under different rules — carry
  the rating as a prior, not a commensurable score).
- **Effort low (~a day), impact medium.** Elo is for *visibility* (a human-legible
  "strength over lineage" number on the dashboard), **not** for the promote
  decision. Build Bradley–Terry for decisions.

### How Bradley–Terry should be used

BT fits a latent strength θᵢ per candidate from pairwise outcomes with confidence
intervals — the noise backbone Elo lacks. Used correctly in zicato:

- **Comparison unit = per-replicate** (feed each replicate of `_run_replicated` as
  its own Bernoulli outcome) for sharp CIs, falling back to per-duel for legacy
  averaged data. BT natively handles the partial/star schedules of elim and racing.
- **Two strictly separated roles:** BT **proposes**, the gate **promotes.** Replace
  the margin-blind Copeland/lowest-scalar *internal leader* pick with **rank-by-θ**
  (drop-in for `swiss._standing_order`, the elim scalar sorts). The leader still
  faces the champion through the unchanged `evaluate_gate` + `confirm_crowning_holdout`.
  BT never replaces the per-task feasibility rules — those guard regressions θ is
  blind to.
- **Gauntlet stays untouched** (BT over two contestants is degenerate; the
  gauntlet is the back-compat anchor). BT shines on multi-candidate fields.
- **Uncertainty gate (BT's signature lever):** add an opt-in **pre-gate** guard —
  *promote only if `P(θ_child > θ_champion) > threshold`* (e.g. 0.95), computed
  from θ and SEs. Below threshold → **defer** (spend more replicates) rather than
  crown on noise. This only ever *blocks* promotion, so the protected-incumbent
  invariant strengthens. It turns the already-existing `decision="deferred"` literal
  into a real outcome.
- **"Replicate first, resolve second" as a schedule, not a slogan:** between
  scheduled batches, fit BT, find the top pair whose CIs overlap most, and spend the
  next replicate **only on that duel** (the cache makes prior replicates free).
  Repeat until separation or a replication budget is hit. Needs a rating-feedback
  hook in `selection/driver.py` (today batches are fixed with no feedback).

### Phased rating/resolution layer (under the schedulers; gate + gauntlet untouched)

- **Phase 0 — config seam.** Two opt-in `TournamentStructure.params` keys:
  `resolver` (`none|copeland|ranked_pairs|maximal_lottery`, default = today) and
  `rating` (`none|bradley_terry|elo`, default `none`). Params already fold into the
  contract hash, so a change rolls the epoch with zero new plumbing. Add the
  additive index columns. **(SHIPPED:** the `rating` knob + additive columns +
  the `resolver` knob (`copeland`/`ranked_pairs`; `maximal_lottery` unbuilt).)**
- **Phase 1 — Elo analytics fold** (above): highest impact/effort ratio, read-only,
  zero selection risk. **(SHIPPED via the BT-on-Elo-scale fold — see the
  subsection status note.)**
- **Phase 2 — BT rating module** (`selection/rating.py`, convex MLE): replace
  Copeland/scalar standings with θ-rank in swiss/elim; persist θ/SE; CI dot-plot.
  **(SHIPPED:** `fit_bradley_terry` + opt-in `params["rating"]` θ-rank standings;
  θ/SE persist as `elo`/`elo_se` on the Elo scale.)**
- **Phase 3 — Ranked Pairs + Smith-set prune resolver** (`selection/resolve.py`,
  pure functions over the `MatchRecord` matrix; the doc's #1 recommendation): plug
  into *leader selection only*, not the gate. **(SHIPPED:** `smith_set` +
  `ranked_pairs` + `resolve_leader` in `selection/resolve.py`, opt-in via
  `params["resolver"]`, wired into single/double-elim + swiss.)**
- **Phase 4 — CI-driven replication + the uncertainty pre-gate + maximal-lottery
  resolver for residual cycles**: biggest correctness win under noise; needs the
  driver-feedback refactor; do last. **(Partial — SHIPPED:** the uncertainty
  pre-gate + CI-driven replication as `selection/evidence_gate.py` +
  `driver.py::confirm_promotion_with_evidence`; **UNBUILT:** the maximal-lottery
  resolver.)**

---

## 6. Other documented-but-missing + doc reconciliation

- **Diff-complexity regularization** (OVERFITTING.md #4, "BUILD — cheap"): a small
  `λ·complexity(diff)` term (mutation-point count + chars changed) in scoring —
  doubles as an anti-overfitting guard *and* a parsimony lever on proposal quality.
  Missing today. **M / M.**
- **Random-baseline holdout sanity check** (OVERFITTING.md #7): score a random
  mutation on the holdout; if a "win" is within noise of it, the gain was
  overfitting. Low priority; build as an optional `zicato health` finding.
- **Cross-contract experiment transfer** (`same_contract=False`,
  EXPERIMENT-MEMORY.md §3.4): an explicit extension point (`query.py:518`), build
  only if epochs turn over fast under one contract hash.
- **Doc reconciliation (cheap, high-trust):** several docs describe shipped
  features as future work — fix the status headers so the harness can trust its own
  docs:
  - EXPERIMENT-MEMORY.md status "DESIGN (not yet implemented)" → **SHIPPED**.
  - OVERFITTING.md §12 future-tense framing → reconcile with the §0 "shipped"
    callouts (#1/#2/#3/#5/#6 are live).
  - ROBUSTNESS.md §4.2 "subprocess workers planned" → **shipped**.
  - RUNTIME.md §3.2 / ROBUSTNESS.md §2.4 "supervisor does NOT kill the
    orchestrator" → currently **false** until §0 lands; fix the code, then the doc
    is true again.
  - Regenerate CLI.md from `zicato --help` (phantom `epochs`/`workspace` commands;
    truncated `repair-*` names).

---

## Recommended sequencing (if you do the high-value few first)

1. **Watchdog warn-only for the orchestrator** (§0) — H/S, fixes a live loop-killer.
2. **PID-identity check** in lock + watchdog (§1.2) — H/S.
3. **Elo analytics fold** (§5) — answers "can we generate Elo?" yes; read-only, ~a day.
4. **Best-of-N + self-critique** and **hypothesis prediction-accuracy scoring**
   (§4.1–4.2) — the biggest proposal-quality wins.
5. **Default git-worktree snapshots** (§2.1) — the biggest efficiency win.
6. **Consolidate the kill protocol into Rust** (§3) — removes the parent/supervisor race.
7. Then the BT rating layer + uncertainty gate (§5 Phases 2–4) and diff-complexity
   regularization (§6).

## Verification & non-goals

- Reliability/efficiency changes verify against the deterministic mock target
  (`examples/zicato_examples/target_1_presentation`) + the test suite; no live
  evolve runs without explicit go-ahead (AGENTS.md rule 1). The watchdog fix needs
  a Rust unit test asserting `decide_heartbeat` never returns `Kill` for the
  heartbeat pid.
- Elo/BT land as **additive** index columns + opt-in `params` — the gauntlet
  default and the promote gate stay byte-identical; rating never gates promotion
  except via the opt-in uncertainty *block*.
- These are functionality changes (they intentionally alter behavior), unlike the
  behavior-preserving `REIMPLEMENTATION.md` program. They can proceed in parallel.
