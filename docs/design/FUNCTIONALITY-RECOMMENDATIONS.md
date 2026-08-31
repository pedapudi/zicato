# zicato — functionality improvement recommendations

> **Status: a set of recommendations, several of which have since been
> built.** Each numbered recommendation below is still written as a proposal.
> Where the recommended change now exists in the tree, the section carries an
> inline note saying so; the summary of what has and has not shipped is
> immediately below. Read a recommendation without such a note as unbuilt.
>
> The review covers four areas: running tournaments reliably, running them
> efficiently, the boundary between the Rust supervisor and the Python loop,
> and proposal-generation quality. Two further sections cover capabilities the
> design documents describe but the tree did not carry, candidate rating and
> winner resolution among them. Its companion is the behavior-preserving
> `REIMPLEMENTATION.md` design note. The changes here alter behavior on
> purpose and can proceed in parallel with it. Each was grounded in an audit
> of the live code, the Rust crate, and the design documents.

## Context

The review asked how to make zicato work better rather than only read more
cleanly, which is what `REIMPLEMENTATION.md` addresses. Six fronts were
investigated against the live code, the Rust crate, and the design documents.
They are running tournaments reliably (§1), running them efficiently (§2), the
division of labour between Rust and Python (§3), proposal-generation quality
(§4), candidate rating and winner resolution (§5), and the remaining
documented-but-unbuilt capabilities (§6). Sections 5 and 6 answer two questions
the operator asked outright: whether a tournament's records can produce an Elo
rating, and how a Bradley–Terry paired-comparison model should be used.

### What has since been built

- **The Rust supervisor has no path that kills the orchestrator.**
  The `HeartbeatAction` enum carries no `Kill` variant by construction
  (`crates/supervisor/src/watchdog.rs`), so the safety claims in RUNTIME.md
  §3.2 and ROBUSTNESS.md §2.4 hold. This was §0's headline defect and §1's
  first recommendation.
- **A process is identified by pid plus start time**, in the Python workspace
  lock (`pid_start_time` in `src/zicato/runtime/lock.py`) and in the
  supervisor's liveness check. That is §1's second recommendation.
- **An aborted `LossProfile` carries an `abort_cause`**, and only genuine
  budget exhaustion is cacheable: `is_infra_abort_cause`
  (`src/zicato/core/loss.py`) is what keeps an infrastructure blip out of the
  unit cache. Those are §1's third and fourth recommendations.
- **The conservative crash-resume protocol is wired**, through the
  `force_fresh=False` path in `src/zicato/tournament/runner.py`. That is §1's
  seventh recommendation.
- **The git generation store is the default backend**
  ([STORAGE.md](STORAGE.md) §7), which removed both the per-generation copy
  and the per-run copy. That is §2's first recommendation.
- **The champion side is cache-read by default** (`champion_force_fresh`
  defaults to false in `runner.py`), which is §2's third recommendation, and
  **a round's matchups share one concurrency semaphore**
  (`src/zicato/tournament/scheduling.py`), which is §2's fifth.
- **Best-of-N sampling with a critic pass ships**
  (`src/zicato/proposer/best_of_n.py`), and **hypothesis predictions are
  scored against actuals** (`src/zicato/proposer/calibration.py`). Those are
  §4's first two recommendations.
- **Diff-complexity regularization ships** (`src/zicato/scoring/diff_complexity.py`)
  and **the random-baseline placebo arm ships** as the opt-in
  `overfitting.random_baseline_every_n`. Those are two of §6's items, and
  every documentation-reconciliation item §6 lists has since been applied.
- **Multi-challenger fields with holdout confirmation ship**
  (`evolve_field_round` and `confirm_crowning_holdout` in
  `src/zicato/evolve/field.py`, with a command-line flag).

### What remains unbuilt

- The in-worker hard-stop timer and the `match_id` handoff to the worker
  (§1, recommendations 5 and 6).
- The warm interpreter pool and the single `ScoringWeights` serializer
  (§2, recommendations 2 and 6).
- Consolidating the kill protocol into Rust (§3).
- Decoding-parameter diversity across proposer slots (§4, recommendation 4).
- The maximal-lottery resolver (§5).

---

## 0. Headline: stop the watchdog from killing the orchestrator

> **Since built.** The `HeartbeatAction` enum has no `Kill` variant by
> construction, so the heartbeat loop cannot escalate against the
> orchestrator pid. The recommendation below is the record of the defect and
> the fix that was applied.

A self-improving harness whose own watchdog killed the loop on a slow model
call, while its documents claimed immunity, was the most damaging kind of
reliability failure available: the system's self-model was wrong about its own
safety. **Fix:** make `decide_heartbeat` warn-only (`Warn`/`MissingHeartbeat`,
never `Kill`) and delete the `escalate()` in `heartbeat_loop`. Restarting the
orchestrator is a process-supervisor concern, which is what the documents
already state. **Impact H, Effort S, Risk ~none** — it restores documented
behavior.

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
| 1 | **Watchdog warn-only for the orchestrator** (§0) | A slow endpoint kills the loop, contradicting the docs. **Since built.** | H / S |
| 2 | **Identify a process by pid plus start time** rather than by a bare `os.kill(pid,0)`, in both the Python lock (`lock.py:92-125`) and Rust `is_alive` (`signal.rs`) | A recycled PID makes a dead worker look alive (kill declined) or an innocent process look like the lock owner (steal/refuse-start). **Since built.** | H / S |
| 3 | **Capture an `abort_cause` enum** (`parent_kill` / `gone_no_result` / `nonzero_exit:{code}`) on the synthesized aborted `LossProfile` (`runner.py:1174-1193`) | The three causes are otherwise indistinguishable, so loop-health cannot separate a real agent infinite-loop from a transient crash from the watchdog over-firing — which is the signal the loop needs. **Since built.** | M / S |
| 4 | **Do not cache infrastructure aborts.** Cache only genuine wall-clock-budget exhaustion; parent-kill, supervisor-kill, and crash aborts must not become a permanent worst-case cache HIT (`runner.py:1707-1723`, `2843-2873`) | An infrastructure blip otherwise poisons a board unit's score for the rest of the epoch, and only `--mode full` recovers it. **Since built** (`is_infra_abort_cause`). | M / S |
| 5 | **In-worker SIGALRM hard stop** (`signal.setitimer(ITIMER_REAL, budget)`) as a true in-process budget floor (`_tournament_worker.py:589-604`) | The worker budget is cooperative `asyncio.wait_for` only; a GIL-holding C-extension or `while True` never yields, so a wedged run depends entirely on two healthy outer processes | M / S |
| 6 | **Pass `match_id` into the worker** so it writes `loss.json` once, atomically, instead of the parent rewriting it post-exit (`runner.py:1252-1257`) | A parent kill between worker-exit and rewrite leaves a `match_id`-less cached loss → wrong provenance on reindex | L / S |
| 7 | **Ship the conservative resume protocol** (the markers exist at RUNTIME.md §4; nothing reads them) | A mid-tournament kill loses in-flight work to re-runs; the unit cache already makes resume nearly free (a completed `loss.json` is a cache HIT). **Since built** (`force_fresh=False`). | M / M |
| 8 | **Single state-file owner on kill.** Make both Rust kill paths leave the run state file for the orchestrator reaper (`watchdog.rs:365-368` vs `398-404` disagree) | A double-trigger can delete the file the reaper needed for finalization | L / S |

---

## 2. Running tournaments efficiently

| # | Speedup | Cost removed | Impact / Effort |
|---|---|---|---|
| 1 | **Default to git-worktree snapshots** (`git_genstore.py`). A worktree checkout is content-addressed, dedups blobs across generations, and *is* the isolated per-run tree | Removes both the per-generation `copytree` (`genstore.py:396`) and the **per-run** ephemeral `copytree` (`runner.py:576`), which together cost O(board × 2 × replicates) copies per round. **Since built** — git is the default backend. | H / M |
| 2 | **Warm Python-interpreter pool, Rust-managed** (the worker is the payload, Rust is the cage — see §3a). Long-lived interpreters that import the ADK adapter once and accept board units over a framed pipe | Kills the 100-500ms adapter import × every `(side,entry,replicate)` — ~4-14s/round of pure overhead on a 10-entry board (ROBUSTNESS.md §2.3) — and consolidates the kill/budget path | H / L |
| 3 | **Cache-read the immutable champion side** in `run_tournament` instead of `force_fresh=True` for both sides (`runner.py:2419`) | The gauntlet champion is immutable within an epoch yet is re-run every round; fast mode already cache-reads it and the rigorous path re-runs it for no gain. **Since built** — `champion_force_fresh` defaults to false. | M / S |
| 4 | **Re-enumerate only touched files** after a patch, rather than the whole tree twice (`mutation/validator.py:117,220-223`) | A full AST re-parse of the mutable tree per applied patch | M / S |
| 5 | **Cross-matchup parallelism** for swiss/elim/racing: lift the semaphore to span concurrent matchups of a round (`orchestrator.py:1737` runs them serially) | Worker-spawn and snapshot overhead is otherwise re-paid serially per matchup. **Since built** — one semaphore spans a round's matchups (`tournament/scheduling.py`). | M / M |
| 6 | **One `ScoringWeights` JSON serializer** replacing the hand-aligned `_weights_spec`/`_weights_from_args` pair (`runner.py:692-744` ↔ `_tournament_worker.py:685-763`) | The gain is correctness as well as speed: the two must stay byte-aligned or the worker silently scores under defaults, which is the documented `per_judge_weights` desync | M / S |

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
  - A **per-evolve budget killer** in Rust reading a `deadline` from the
    heartbeat. It is the only whole-invocation budget that can be enforced,
    because the Python `asyncio.wait_for` cannot pre-empt a wedged
    orchestrator.
  - The per-run deadline killer stays where it already is (Rust).
- **Rust should own an event-stream tailer** that incrementally tails
  `events.jsonl` beside the existing `notify` watcher. The work is IO-bound, it
  must survive an orchestrator wedge, and it feeds the live drift view and the
  flat-drift loop-health detector.
- **Rust keeps all *read* surfaces** that must work when the orchestrator is
  broken: `/statusz`, index analytics, state snapshots.
- **Python keeps all *policy & authoring*:** proposer, scoring/gate, applier (AST
  surgery), board, LLM wiring, dashboard UI. These change often and have no
  GIL-wedge exposure — Rust would only slow their iteration.

The first concrete move is removing the duplicated kill protocol so that one
process owns it.

### 3a. Why the tournament worker stays Python while Rust owns its lifecycle

The boundary above invites rewriting the per-entry **tournament worker**
(`_tournament_worker.py`) in Rust. It should stay Python. The worker's job is
to run the *candidate harness*, and that candidate is a **mutated Python source
tree**. The worker `chdir`s into the generation snapshot, **imports it
in-process**, and drives it through the agent-development-kit adapter to
goldfive (`adapters/adk.py`). Judges, emulator, and the loss reducer all run in
that same interpreter. The system under evaluation is Python, which is zicato's
premise — the mutation surface is `# zicato:mutable` Python spans and the
applier does AST surgery. A Rust worker could not import and run a Python agent;
it would have to spawn a Python child regardless, adding a language boundary for
no gain.

The reliability intuition that a Rust worker cannot wedge does not hold. A
worker wedges in **model I/O** or in **the candidate's own code**, never in its
roughly 200 lines of bookkeeping, and both of those live in the payload the
rewrite cannot touch. zicato already handles a wedged worker: a *separate* Rust
supervisor kills it from outside. Rewriting the guarded process does not
strengthen the guard. Memory safety also pays little for a short-lived,
isolated, disposable process; it pays for the *supervisor*, which is already
Rust.

So the division is between the enclosure and its contents. Rust owns the worker
*lifecycle*: spawn, isolation, the single SIGTERM→SIGKILL escalator, the per-run
budget and deadline, heartbeat staleness, and inter-process framing. Python owns
the *run*: importing and driving the candidate harness, the judges, and the
reducer. A Rust `RustHarnessAdapter` becomes worth building only if zicato ever
evolves a *Rust* inner harness, and the `RunnableHarness` Protocol is already
the seam for adding one adapter at a time rather than rewriting the whole
worker. Every dogfood target is Python, so that case does not arise.

**What Rust should own here instead: a warm-interpreter pool manager.**
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

Two invariants make the pool safe, and neither is negotiable: **per-run worktree
isolation** on the filesystem and **recycle-on-abort** in memory. Together they
reproduce the isolation that a copytree per run buys today.

**Sequencing.** A pool manager written in Python is a lower-effort first step
that captures the import-amortization win alone. Moving the manager to Rust is
the end state, because it also collapses the kill and budget protocol into one
memory-safe owner. Build the Python pool first only if the Rust manager, which
depends on the §3 kill-protocol consolidation, is not ready; otherwise build the
Rust manager directly, since it delivers both wins.

---

## 4. Improving the proposals the loop generates

> **Partly since built.** Recommendations 1 and 2 below have shipped, as
> `src/zicato/proposer/best_of_n.py` and `src/zicato/proposer/calibration.py`.
> Recommendation 4's prompt-side half shipped and its decoding-parameter half
> did not, as the recommendation itself records. Recommendations 3, 5, 6, and
> 7 are unbuilt.

The default proposer is a tool-using agent with a rich restricted context —
loss summary, valid targets, patterns, mutation points, prior-experiment
digest, failure-mode profile, telemetry insights — plus read-only tools
(`list_mutation_points`, `read_mutable_file`, `grep_mutable`, `read_journal`,
`read_insights`) and a parse-validate-retry loop. Generation nevertheless
carries **no critique and no calibration loop**, and only *partial* diversity
pressure: per-slot edit-class and strategy hints exist, while per-slot decoding
variation does not (recommendation 4). That is the largest untapped source of
proposal quality. The levers below are ranked, and all of them stay inside the
existing overfitting-restricted context channels:

1. **Best-of-N sampling with a self-critique pass (the top lever).** Sample N
   experiments per propose-step, then run a cheap critique pass that picks or
   repairs the best against a quality bar: is it grounded in the tools, does it
   target a real failure mode, is the diff minimal. The retry loop fires only
   on *invalid* output, so a valid but mediocre proposal is never reconsidered.
   **H / M.** Keep the critic inside the restricted prompt context, with no
   holdout access.
2. **Score the hypothesis's predictions against actuals (the most novel
   lever).** The hypothesis already carries falsifiable predictions —
   `expected_drift_movements`, `expected_pass_rate_delta`,
   `expected_metric_movements` — which are parsed and journaled and **never
   compared to what happened**. Score them after the tournament settles and
   feed the calibration back into experiment memory. This turns decorative
   prediction fields into a real proposer-quality axis. **H / M**, and
   diagnostic only: promotion must not gate on it.
3. **Hard-ish diversity constraint across challenger fields.** EXPERIMENT-MEMORY.md
   §2.2 names the failure: siblings propose the *same* mutation, collapsing a
   field of N into < N experiments. Reject/penalize a challenger whose
   `modulating` id-set + core-idea duplicates a sibling; optionally assign each
   slot a distinct target. Free tournament value. **H / S-M.**
4. **Condition each proposer slot on the dominant failure mode — SHIPPED on
   the prompt side; the remaining gap is decoding-parameter diversity.** Both
   prompt-side axes landed. `proposer/best_of_n.py::_sample_slot` threads a
   DISTINCT per-slot edit-class hint (`proposer/hints.py::hint_for_slot` /
   `EDIT_CLASS_HINTS`), conditioning slots `0..N-2` on the failure profile's
   DOMINANT mode (*over-retrieves / misses / empty / looping*) and keeping
   the last slot exploratory. Per-slot *strategy* rotation ships alongside
   it (`hints.py::STRATEGY_HINTS` / `strategy_for_slot`), rotating
   MINIMAL-SURGICAL, STRUCTURAL-REWORK, DEFENSIVE-HARDENING and CONTRARIAN
   deterministically per `(slot, round)`. Composed with the edit-class hint,
   it draws the N samples inside one best-of-N slate from N distinct
   prompts rather than from one. What remains unbuilt is *decoding*
   diversity: every slot shares one sampling strategy and temperature. The
   `aux_call_llm` seam is `(system, user, model) -> str` and carries no
   sampling parameters, so the variation rides the PROMPT alone. Decoding
   breadth — a per-slot temperature or top-p — is blocked on extending the
   `CallLLM` seam, which is a plumbing change rather than a prompt change.
   **M / M.**
5. **Structured per-epoch reflection on rejection *patterns***, which the
   per-experiment digest does not supply because it surfaces individual
   instances. **M / M.**
6. **Richer mutation tooling**: a `read_parent_diff` tool reporting what the
   last promotion changed, a `mutation_usage` tool reporting where an id's
   value is referenced, and a soft instruction to ground a proposal in the
   tools before writing it. **M / M.**
7. **Do not add semantic retrieval over the journal.** Epochs are small and the
   relational `prior_experiments_for_epoch` curation is the right abstraction.
   Extend it, as recommendation 5 does, rather than adding an embedding
   dependency and a further channel through which holdout information could
   leak.

---

## 5. Documented-but-missing: candidate rating & winner-resolution

> **Status — partially implemented** (issue #90 plus the evidence gate). The
> *rating* half of this section has shipped and one resolver has; the
> maximal-lottery resolver has not. Four things are built and live.
>
> 1. The visibility rating fold
>    (`src/zicato/index/elo.py::fold_elo_into_index`), implemented as a
>    **Bradley–Terry maximum-likelihood fit mapped onto the Elo scale** rather
>    than as the standard-Elo update this section first proposed. See the
>    subsection note below.
> 2. The Bradley–Terry rating module
>    (`src/zicato/selection/rating.py::fit_bradley_terry`), a convex
>    maximum-likelihood fit with confidence intervals, plus its opt-in θ-rank
>    standings (`params["rating"]`).
> 3. The Bradley–Terry **uncertainty pre-gate**, with
>    confidence-interval-driven "replicate first, resolve second" scheduling
>    (`src/zicato/selection/evidence_gate.py` plus `selection/driver.py`). It
>    defers and spends a replicate on the duel whose confidence intervals
>    overlap most.
> 4. The `resolver` knob — Ranked Pairs behind a Smith-set prune
>    (`selection/resolve.py` plus `standings_ext.py`, opt-in
>    `params["resolver"]`), wired into single-elimination,
>    double-elimination, and swiss.
>
> Still unbuilt: the maximal-lottery resolver, for which
> `SELECTION-THEORY.md` remains a design. Per-lever status is tagged inline
> below.

Winner resolution has two tiers. A per-duel **gate** — scalar margin, pass-rate
and per-namespace monotonicity, and holdout confirmation — is the only thing
that can promote. Each structure picks an internal leader by scalar or Copeland
bookkeeping, with Copeland present only in swiss, and then runs one
champion-gate duel. The **selection path** carries no global rating, though a
read-only visibility rating now folds over the same ledger after the fact
(below). The complete pairwise-outcome ledger — `MatchRecord`, `MatchOutcome`,
`Standing`, and the index `tournaments` table with per-`match_id` losses — is
already persisted, which is what let the rating layer land without taking a
single new measurement.

### A tournament's records do produce an Elo rating, and this ships first

> **Status — implemented, through a Bradley–Terry fit** (issue #90).
> `src/zicato/index/elo.py` shipped this read-only fold. It re-fits
> `fit_bradley_terry` over the de-duplicated match ledger at every reindex or
> ingest and **maps the fitted strength onto the Elo scale**
> (`elo = 1500 + θ·400/ln 10`) rather than running the sequential margin-K Elo
> update the bullets below propose. The Bradley–Terry fit carries confidence
> intervals natively (`generations.elo_se`), which the margin-K approximation
> lacked, so the margin-of-victory K-weighting and provisional-K decay
> refinements were superseded; the `provisional` display suffix on a
> generation below `MIN_RATING_GAMES` stands in for the latter. The
> `generations.elo` / `elo_se` / `elo_games` columns (index schema versions 10
> and 12) and the standings, generation roster, and candidate dossier displays
> all shipped. The rating is for visibility and never reaches the gate. A
> racing structure's intermediate rungs contribute zero games, because they
> name no pairwise winner; a Plackett–Luce set-rating is the documented fix.

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
  `parent_generation_id`'s rating, so a child starts as strong as the parent it
  was derived from, and anchor on the champion. **Flag epoch-roll boundaries**
  on the chart, because duels across a contract roll were measured under
  different rules; the rating carries across as a prior rather than as a
  commensurable score.
- **Effort low (about a day), impact medium.** Elo serves *visibility* — a
  human-legible strength-over-lineage number on the dashboard — and must stay
  out of the promote decision. Build Bradley–Terry for decisions.

### How Bradley–Terry should be used

The Bradley–Terry model fits a latent strength θᵢ per candidate from pairwise
outcomes, with confidence intervals, which is the account of noise that Elo
lacks. Five rules govern its use in zicato:

- **The comparison unit is one replicate.** Feed each replicate of
  `_run_replicated` in as its own Bernoulli outcome for tight confidence
  intervals, falling back to per-duel for data that was averaged before the
  per-replicate ledger existed. The model handles the partial and star
  schedules of elimination and racing structures natively.
- **The model proposes and the gate promotes**, and the two roles stay
  separate. Replace the margin-blind Copeland or lowest-scalar pick of the
  *internal leader* with a rank by θ, dropping into `swiss._standing_order` and
  the elimination scalar sorts. The leader still faces the champion through the
  unchanged `evaluate_gate` and `confirm_crowning_holdout`. The model never
  replaces the per-task feasibility rules, which guard against regressions θ
  cannot see.
- **The gauntlet stays untouched.** A Bradley–Terry fit over two contestants is
  degenerate, and the gauntlet is the compatibility anchor. The model earns its
  keep on multi-candidate fields.
- **The uncertainty gate is the model's signature lever.** Add an opt-in
  pre-gate guard that promotes only when `P(θ_child > θ_champion)` exceeds a
  threshold, say 0.95, computed from θ and its standard errors. Below the
  threshold the round **defers** and spends more replicates instead of crowning
  on noise. The guard can only block a promotion, so it strengthens the
  protected-incumbent invariant, and it gives the existing
  `decision="deferred"` literal a real outcome to name.
- **Replicate first, resolve second, as a schedule.** Between scheduled
  batches, fit the model, find the top pair whose confidence intervals overlap
  most, and spend the next replicate on that duel alone; the cache makes prior
  replicates free. Repeat until the pair separates or a replication budget is
  spent. This needs a rating-feedback hook in `selection/driver.py`, where
  batches are currently fixed with no feedback.

### The rating and resolution layer, in five steps (under the schedulers; gate and gauntlet untouched)

- **The configuration seam.** Two opt-in `TournamentStructure.params` keys:
  `resolver` (`none|copeland|ranked_pairs|maximal_lottery`, defaulting to
  current behaviour) and `rating` (`none|bradley_terry|elo`, default `none`).
  Params already fold into the contract hash, so a change rolls the epoch with
  no new plumbing. Add the additive index columns. **(SHIPPED:** the `rating`
  knob, the additive columns, and the `resolver` knob for `copeland` and
  `ranked_pairs`; `maximal_lottery` is unbuilt.)**
- **The Elo analytics fold** (above): the highest ratio of impact to effort,
  read-only, with no selection risk. **(SHIPPED, as the Bradley–Terry fit
  mapped onto the Elo scale — see the subsection status note.)**
- **The Bradley–Terry rating module** (`selection/rating.py`, a convex
  maximum-likelihood fit): replace Copeland and scalar standings with a θ rank
  in swiss and elimination, persist θ and its standard error, and plot the
  confidence intervals. **(SHIPPED:** `fit_bradley_terry` plus opt-in
  `params["rating"]` θ-rank standings; θ and its standard error persist as
  `elo` and `elo_se` on the Elo scale.)**
- **The Ranked Pairs resolver behind a Smith-set prune**
  (`selection/resolve.py`, pure functions over the `MatchRecord` matrix; this
  document's leading recommendation): plug it into leader selection alone and
  keep it out of the gate. **(SHIPPED:** `smith_set`, `ranked_pairs`, and
  `resolve_leader` in `selection/resolve.py`, opt-in via `params["resolver"]`,
  wired into single-elimination, double-elimination, and swiss.)**
- **Confidence-interval-driven replication, the uncertainty pre-gate, and a
  maximal-lottery resolver for cycles that survive the prune**: the largest
  correctness win under noise, requiring the driver-feedback change, and best
  done last. **(Partly SHIPPED:** the uncertainty pre-gate and
  confidence-interval-driven replication landed as `selection/evidence_gate.py`
  and `driver.py::confirm_promotion_with_evidence`; **UNBUILT:** the
  maximal-lottery resolver.)**

---

## 6. Other documented-but-missing capabilities, and documentation reconciliation

> **Partly since built.** Diff-complexity regularization ships as
> `src/zicato/scoring/diff_complexity.py`, and the random-baseline check ships
> as the opt-in placebo arm `overfitting.random_baseline_every_n`. Every
> documentation-reconciliation item listed below has been applied.

- **Diff-complexity regularization** (OVERFITTING.md #4, "BUILD — cheap"): a
  small `λ·complexity(diff)` term over the mutation-point count and the number
  of characters changed, added to scoring. It serves both as an
  anti-overfitting guard and as a parsimony lever on proposal quality. **M / M.**
  **Since built.**
- **Random-baseline holdout sanity check** (OVERFITTING.md #7): score a random
  mutation on the holdout, and treat a "win" that falls within noise of it as
  overfitting. Low priority; build as an optional `zicato health` finding.
  **Since built** as the placebo arm.
- **Cross-contract experiment transfer** (`same_contract=False`,
  EXPERIMENT-MEMORY.md §3.4): an explicit extension point (`query.py:518`), to
  build only if epochs turn over fast under one contract hash.
- **Documentation reconciliation (cheap, and it restores trust):** several
  documents describe shipped features as future work. Fixing their status
  headers is what lets the harness trust its own documents. Every item below
  has since been applied:
  - EXPERIMENT-MEMORY.md status "DESIGN (not yet implemented)" → **SHIPPED**.
  - OVERFITTING.md §12 future-tense framing → reconcile with the §0 "shipped"
    callouts (#1/#2/#3/#5/#6 are live).
  - ROBUSTNESS.md §4.2 "subprocess workers planned" → **shipped**.
  - RUNTIME.md §3.2 / ROBUSTNESS.md §2.4 "supervisor does NOT kill the
    orchestrator" → was **false** until §0's fix landed; the code was fixed, so
    the claim now holds.
  - Regenerate CLI.md from `zicato --help` (phantom `epochs`/`workspace` commands;
    truncated `repair-*` names).

---

## Recommended sequencing, highest value first

The order below is the one this review recommended. All but the last two items
have since been built; the two that remain are marked.

1. **Watchdog warn-only for the orchestrator** (§0) — H/S, and it fixes a
   defect that killed live loops.
2. **Identify a process by pid plus start time**, in the workspace lock and the
   watchdog (§1, recommendation 2) — H/S.
3. **The Elo analytics fold** (§5) — read-only, about a day, and it answers
   whether tournament records can produce a rating.
4. **Best-of-N with self-critique** and **scoring the hypothesis's predictions
   against actuals** (§4, recommendations 1 and 2) — the two largest
   proposal-quality wins.
5. **Git-worktree snapshots by default** (§2, recommendation 1) — the largest
   efficiency win.
6. **Consolidate the kill protocol into Rust** (§3), which removes the race
   between the parent and the supervisor. **Still unbuilt.**
7. Then the Bradley–Terry rating layer with its uncertainty gate (§5) and
   diff-complexity regularization (§6). **Built, apart from the
   maximal-lottery resolver.**

## Verification and non-goals

- Reliability and efficiency changes verify against the deterministic mock
  target (`examples/zicato_examples/target_1_presentation`) plus the test
  suite. No live evolve run happens without explicit operator go-ahead
  (AGENTS.md rule 1). The watchdog fix needs a Rust unit test asserting that
  `decide_heartbeat` never returns `Kill` for the heartbeat pid.
- The rating work lands as **additive** index columns plus opt-in `params`, so
  the gauntlet default and the promote gate stay byte-identical. A rating never
  gates a promotion except through the opt-in uncertainty guard, which can only
  block.
- These recommendations alter behavior on purpose, which is what distinguishes
  them from the behavior-preserving `REIMPLEMENTATION.md` program. The two can
  proceed in parallel.
