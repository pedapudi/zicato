# 12 — The Bug Casebook: Ten Program Bugs as Teaching Cases

> **Covers:** the ten real bugs found and fixed during the 2026-07
> effectiveness/quality program, each as a full case study — symptom, root
> cause with before/after code, why every oracle missed it, the invariant
> class it defines, the fix, the regression-test pattern that pins it, and
> the tell-tale signs you are about to reintroduce it. Closes with the
> meta-lessons that generalize across all ten.
>
> **Prerequisites:** 04-evaluation-statistics.md (the noise doctrine, the
> reserved replicate-base ledger, the unit cache), 03-contract-and-epochs.md
> §"The contract hash", 07-runtime-and-durability.md §"Generation stores",
> 11-testing.md §"Oracles".
>
> **Invariants introduced in this chapter:**
> 1. **Shared mutable state behind per-X artifacts is THE recurring bug
>    class.** Whenever N logical things (replicates, slate candidates,
>    concurrent checkouts, re-derives) share one physical slot (one file, one
>    worktree, one process group, one child-snapshot path), assume the last
>    writer has silently replaced everyone else until proven otherwise.
> 2. **A regression test must fail with the fix stashed.** If you cannot
>    demonstrate the red state, you have pinned nothing.
> 3. **Deterministic contracts pinning interacting knobs OFF is how bugs
>    hide.** The countermeasure is adversarial knob-ON tests.
> 4. **Identity is not location.** Hashes, ids, and cache keys must be
>    derived from what a thing IS, never from where it happens to live
>    (paths, cwd, process groups, argv shapes).
> 5. **The server owns derived truths.** A client (or any second consumer)
>    that re-derives a decision from raw parts will eventually disagree with
>    the owner.

Every case below is recoverable from this branch's git history — the commit
hashes are real, `git show <hash>` reproduces every excerpt, and the fix
commits' messages are themselves excellent teaching documents. Read this
chapter the way a pilot reads accident reports: not for the specific holes,
but for the shape of the holes.

The tally, for orientation:

| # | Bug | Fix commit | Live on defaults? |
|---|---|---|---|
| 1 | Replicate-cache clobbering (worker wrote canonical `loss.json` = r0 slot) | `120f761` | yes, whenever `replicates > 1` |
| 2 | `git worktree prune` vs concurrent `add` race | `6b8f98e` | yes, under concurrent checkouts |
| 3 | A/A calibration false-zero floor (replicate index never stamped) | `6e96f22` | yes, for any stochastic harness |
| 4 | Client champion-scan picked FIRST-promoted, not reigning | `54beb21` | yes (dashboard views) |
| 5 | "Evolve hang" = test-suite reaper killpg-ing concurrent evolves | `9ccbbba` | yes, on shared dev hosts |
| 6 | Best-of-N tree mismatch, gauntlet (shared child-slot re-derivation) | `7025a30` | yes (`best_of_n = 3` default) |
| 7 | Same, field path — plus the diversity misjudgment | `7025a30` | yes, on multi-challenger structures |
| 8 | Evidence-gate replicate-slot reuse (replay CIs / r0 clobber / one-sided sampling) | `eb55266` | scaffold-on (the gate ships enabled in scaffolded contracts) |
| 9 | git `derive_generation` re-derive left a stale shared worktree | `7025a30` | yes (git backend is the default store) |
| 10 | Contract hash embedded the cwd / checkout path | `8d0a94f` | yes, in every relocated or differently-cwd'd checkout |

---

## Case 1 — Replicate-cache clobbering: the worker's canonical `loss.json` IS the r0 slot

**Fix commit:** `120f761` — *feat(tournament): run provenance to the harness +
replicate-keyed loss slots* (the fix rode in with the provenance feature; the
commit message names it "a real replicate-cache aliasing bug").

### Symptom

No visible symptom — that is the point. Replicated matchups (`replicates > 1`)
completed normally and produced plausible aggregates. The corruption was in
the *persisted evidence*: after an N-replicate duel, the on-disk sample for
replicate 0 of each unit was not replicate 0's draw.

### Root cause

The per-unit cache scheme keys a board unit as
`(generation_id, entry_id, replicate_index)` and maps replicate 0 to the
canonical `runs/<entry>/loss.json` while r>0 maps to `loss.r<r>.json`
(`_unit_loss_path`, `src/zicato/tournament/unit_cache.py`). But the worker's
own write path predated that scheme and always wrote to the canonical path:

```python
# BEFORE — src/zicato/tournament/runner.py::_run_single (git show 120f761^:...)
    from zicato.core.workspace import loss_profile_path  # noqa: PLC0415

    loss_path = loss_profile_path(workspace_root, epoch_id, generation.id, entry.id)
```

So a replicated matchup's later replicates **silently clobbered replicate 0's
persisted sample with the last replicate's draw**. One physical file
(`loss.json`) served two roles — "the worker's output for this run" and
"replicate 0's immutable cache slot" — and the second role lost.

### Why every oracle missed it

- The default deterministic contracts pinned `replicates = 1` (and later,
  σ=0 worlds): with one replicate there is nothing to clobber, and under
  determinism every replicate's draw is byte-identical anyway — the clobber
  writes the same bytes.
- The in-memory aggregation used the losses returned from the runs, not the
  files, so scores were correct *within* the round; only later readers
  (reindex, crash-resume, later rounds' cache hits) consumed the corrupted
  slot — and they had no ground truth to compare against.

### The invariant class it defines

**One physical slot per logical artifact.** The moment "the file the worker
writes" and "replicate 0's cache slot" are the same path *by coincidence
rather than by routing*, every new writer aliases them. Generalized: any
persisted artifact consumed under a keyed scheme must be *written* through
the same keyed scheme, by the same path function.

### The fix

Route the worker's write through the keyed path function, using the stamped
replicate index (which the same commit introduced as run provenance):

```python
# AFTER — src/zicato/tournament/runner.py::_run_single
    loss_path = _unit_loss_path(
        workspace_root,
        epoch_id,
        generation.id,
        entry.id,
        _entry_replicate_index(entry),
    )
```

Replicate 0 still maps to the canonical path — byte-identical to before for
every single-replicate path — and r>0 lands in its own `loss.r<r>.json`.

### The regression test pattern that pins it

Replicate-slot *distinctness on disk*: run a replicated duel under a seeded
noisy adapter through real workers, then resolve replicate 0 and replicate 1
of the same unit from the cache and assert they differ somewhere
(`test_noisy_adapter_seeded_draws_cross_the_worker_boundary` in
`tests/test_decision_procedure_power.py` asserts exactly this
"replicate-INDEPENDENT" property). Under the pre-fix code both reads return
the last draw and the assertion fails. Note the oracle needs σ>0: at σ=0 the
draws are equal by value and the clobber is invisible.

### You are about to reintroduce this if…

- you add a new writer of any `loss.json` / `gen_score.json` that computes
  its own path with `loss_profile_path(...)` instead of `_unit_loss_path(...)`
  with the entry's stamped replicate index;
- you "simplify" `_unit_loss_path` by dropping the r>0 branch because "the
  canonical file is right there";
- you add a re-scoring or repair pass that re-persists a profile without
  carrying the replicate index it was drawn at;
- you build a new evaluation that runs at replicate indices > 0 but lets the
  worker write wherever it always wrote (check: does your index reach
  `_run_single` via the entry stamp?).

---

## Case 2 — The `git worktree prune` vs concurrent `add` race

**Fix commit:** `6b8f98e` — *feat(epoch): snapshot GC / retention* (the fix is
the commit's "Also:" paragraph; observed while building 8-way concurrent
`checkout_ephemeral`).

### Symptom

Under 8-way concurrent ephemeral checkouts of one generation (the git
backend's per-run `worktree add --detach`), intermittent hard failures:

```
fatal: Invalid path .../.git/worktrees/<name>
```

Rare, timing-dependent, unreproducible in sequence.

### Root cause

The checkout protocol is a multi-command admin window:
`prune → add → detach (unlink .git pointer) → prune`. git's own repo lock
serializes each *individual command* — but not the window. A sibling
checkout's `prune` running between another's `add` and its registration
completing could collect the half-registered admin entry out from under it.

```python
# BEFORE — src/zicato/epoch/git_genstore.py (git show 6b8f98e^:...)
        # Prune any stale registration first (a crashed run can leave a
        # worktree entry whose directory is gone).
        self._git("worktree", "prune")
        self._git("worktree", "add", "--detach", "--force", str(wt), tag)
```

The pre-fix docstring even asserted the opposite — that `worktree add` "holds
an 'initializing' lock … so a concurrent sibling's prune can never race a
half-created checkout." That claim was wrong in practice; the race was
*observed*, not theorized.

### Why every oracle missed it

- Production checkouts run sequentially on the orchestrator's event-loop
  thread, and the workspace runtime lock guarantees a single orchestrator per
  workspace — so the shipped topology never exercised concurrency at this
  seam.
- The conformance suite exercised checkouts one at a time. The race only
  surfaced when an 8-way concurrent conformance test was *written for the new
  feature* — the bug was found by widening the test topology, not by a field
  report.

### The invariant class it defines

**A lock that covers each command does not cover the protocol.** Multi-step
admin sequences over shared registries (git worktree admin state, index
files, lock directories) need a lock spanning the *window*, keyed by the
shared resource. Corollary: never assume a third-party tool's internal
locking matches your composite operation's atomicity needs — test the
composite under contention.

### The fix

A process-local per-repo lock serializing the whole admin window
(`_worktree_admin_lock`, keyed by resolved repo path), wrapped around every
prune/add sequence:

```python
# AFTER — src/zicato/epoch/git_genstore.py::checkout_ephemeral
            with _worktree_admin_lock(self._repo):
                self._git("worktree", "prune")
                self._git("worktree", "add", "--detach", "--force", str(working_dir), tag)
                (working_dir / ".git").unlink(missing_ok=True)
                self._git("worktree", "prune")
```

A registration orphaned by a crash *inside* the window is still collected by
the next prune-before-add. The commit also records the benchmarked, rejected
alternative (per-run `git archive` + tarfile — no shared admin state at all,
but 1.5–2.5× serial cost and catastrophic Python-side serialization under
threads), so the next agent tempted by "just remove the shared state" can
read why that trade lost.

### The regression test pattern that pins it

Contention as a conformance case: the cross-backend suite runs **8-way
concurrent `checkout_ephemeral` of one generation** and asserts all succeed
and each mounted tree is complete. Without the lock, the race reproduces
statistically across CI runs (it is timing-dependent — which is exactly why
it must live in the permanent suite rather than be verified once by hand).

### You are about to reintroduce this if…

- you add any new `self._git("worktree", ...)` call site that is not inside
  `with _worktree_admin_lock(self._repo):`;
- you move a prune "outside the lock for speed";
- you add a second *process* that performs worktree admin on the same repo
  (the lock is process-local — its sufficiency rests on the
  one-orchestrator-per-workspace runtime lock; break that premise and you
  need a filesystem lock instead);
- you build any other multi-command sequence over shared git state (branch
  reset + commit + tag; tag move + checkout) and reason "git has its own
  lock." It locks commands, not your protocol. See also Case 9, which is the
  *logical* (non-racy) version of the same shared-registry blindness.

---

## Case 3 — The A/A calibration's false-zero floor: the replicate index never reached the harness

**Fix commit:** `6e96f22` — *feat(preflight): contract pre-flight* ("Also
fixes a latent calibration bug this work surfaced").

### Symptom

`measure_noise_floor` on a stochastic (seeded-noise) harness measured a floor
of **exactly 0.0** — the report of a perfectly deterministic evaluation, on a
harness explicitly built to be noisy. Downstream, `margin_below_floor` could
then never warn: every margin clears a zero floor.

### Root cause

The calibration passed the distinct replicate index to the unit **cache**
(distinct slots — so far so good) but never **stamped** it onto the board
entries, and the stamped context is the only channel a seeded harness derives
its noise draw from:

```python
# BEFORE — src/zicato/tournament/calibration.py (git show 6e96f22^:...)
        losses = await _run_board_units_fast(
            adapter=adapter,
            child_gen=generation,
            board=board,                                  # <-- no stamp
            ...
            replicate_index=CALIBRATION_REPLICATE_BASE + draw,   # cache key only
        )
```

The seed tuple is `(workspace_seed, generation_id, entry_id,
replicate_index)`; with the stamp absent, `replicate_index` read as 0 on
every draw, so all K "fresh" draws re-rolled the identical seed — K identical
scalars, spread exactly 0.0. The cache dutifully stored K copies of one
sample in K distinct slots.

### Why every oracle missed it

- The calibration's own e2e test used the **deterministic** target_0 adapter,
  whose *correct* floor is exactly 0.0 — the test asserted the buggy value as
  the right answer, because for that harness it *was* the right answer. The
  bug and the pass condition coincided.
- The seeded-noise adapter that could distinguish "genuinely quiet" from
  "seeding broken" was built in a separate commit (`3cbb637`), and no test
  yet drove the calibration through it. The bug surfaced only when the
  pre-flight work composed the two.

### The invariant class it defines

**The cache key and the harness stamp must be the same number, set at the
same seam.** More generally: when a parameter has two consumers (persistence
keying AND behavior seeding), passing it to one and not the other produces a
system that *stores* correctly and *measures* wrongly — the worst combination,
because the artifacts look healthy. And: **a measured zero has two meanings**
(quiet vs broken); any instrument whose failure mode mimics a valid reading
needs a positive control (here: the σ=0.22 harness must measure ≈0.663).

### The fix

Stamp exactly as the replicated-duel path does, from one local variable so
key and stamp cannot diverge:

```python
# AFTER — src/zicato/tournament/calibration.py::measure_noise_floor
    for draw in range(runs):
        replicate_index = CALIBRATION_REPLICATE_BASE + draw
        losses = await _run_board_units_fast(
            ...
            board=_stamp_replicate_index(board, replicate_index),
            replicate_index=replicate_index,
        )
```

The pre-flight's degraded draw stamps identically.

### The regression test pattern that pins it

The positive control: `test_aa_null_calibration_measures_the_noise_floor`
(power harness) asserts the σ=0.22 world's measured floor lands in
`[0.4, 1.0]` — "a floor of ~0 would mean the draws stopped varying (a seeding
regression)". Plus the component-isolation test
(`test_noisy_session_seed_derives_only_from_stable_identifiers`) that pins
each seed component *independently* moving the draw — a stamp regression in
any one identifier fails loudly and names the component.

### You are about to reintroduce this if…

- you build a new out-of-tournament evaluation (§8 of
  04-evaluation-statistics.md) that passes `replicate_index=` to the runner
  but does not `_stamp_replicate_index` the board first;
- you refactor the runner so the stamp and the key come from different
  variables or different call frames;
- you test a stochastic instrument only against a deterministic harness — if
  the instrument's broken output equals the deterministic harness's correct
  output, your test is a tautology;
- you see a suspiciously clean measurement (zero variance, perfect
  agreement) and ship it as good news without a positive control.

---

## Case 4 — The client champion-scan: first-promoted is not reigning

**Fix commit:** `54beb21` — *feat(dashboard): server-authoritative decision
surface + served racing/round-timeline joins*.

### Symptom

Dashboard views (generations table, epoch view, candidate view, boards,
shell) could crown the **wrong generation** with the champion glyph and key
champion-relative computations off it: after two promotions in one epoch
(v0 → v1 → v3), some views showed v1 — the *first* promoted generation — as
champion instead of the reigning v3.

### Root cause

The frontend re-derived "the champion" from raw experiment records, in
multiple places, with a scan that returns the first match in array order:

```js
// BEFORE — src/zicato/dashboard/static/js/views/gens.js (git show 54beb21^:...)
  const champ = gens.find((g) => g.promoted) || gens.find((g) => !g.parent) || null;
  const championId = champ ? champ.id : null;
```

`gens.find((g) => g.promoted)` answers "some promoted generation", not "the
end of the promoted spine." The correct derivation — walk the promoted
parent→child chain to its last element — existed server-side (the lineage
readers), but the client had grown *its own second implementation* of the
decision, one per view, each subtly different. The same commit's message
generalizes: "The frontend re-implemented decisions the server already owns —
and could disagree with it."

### Why every oracle missed it

- Most test epochs (and most demo runs) contain **exactly one promotion** —
  first-promoted and reigning coincide, so every scan returns the right
  answer on the fixtures.
- The client re-derivations were spread across six views; node-side tests
  exercised rendering with pre-shaped fixtures rather than the derivation
  chain from raw records.
- There was no single "champion pointer" contract to test against — each view
  owned a private notion, so no test could assert cross-view agreement.

### The invariant class it defines

**Server authority over derived truths.** Any value derived from records by a
rule (reigning champion, decision classification, gate deciding-rule) must be
derived ONCE, by the owner of the records, and *stamped onto the payload*;
every consumer reads the stamp verbatim. A consumer that re-derives —
especially one that parses display strings or scans raw arrays — is a fork of
the truth that will drift. Secondary lesson: **"find first" over an unordered
collection is a smell** whenever the domain concept is "latest/last/end of a
chain."

### The fix

`_current_champion` (`src/zicato/query/epoch_view.py`) walks the promoted
spine server-side and stamps ONE `current_champion` pointer on the epoch
payload (falling back to the parentless seed when nothing is promoted yet);
`readers/decisions.py` (now `zicato/query`) became THE one experiment-decision
classifier; the gate breakdown ships structured `deciding_rule` / `margin` /
`regressed_*` fields. The client's six re-derivations were **deleted**, not
fixed:

```js
// AFTER — src/zicato/dashboard/static/js/views/gens.js
  // The REIGNING champion — the server-stamped pointer (never re-scanned).
  const championId = (ep && ep.current_champion != null) ? String(ep.current_champion) : null;
```

### The regression test pattern that pins it

Server-side: `tests/test_dashboard_decision_surface.py` builds an epoch with
a multi-promotion spine and asserts `current_champion` is the spine's END
(this fails under any first-match scan). Client-side: the node fixtures were
re-cut to the canonical payload shapes with a mock server mirroring the
served joins, so a view that resurrects a local re-derivation diverges from
the fixtures immediately. The general pattern: **test the derivation at its
single owner with a fixture where "first" and "last" differ**, then test
consumers only for verbatim consumption.

### You are about to reintroduce this if…

- you add a dashboard view (or any consumer) that computes a champion, a
  decision, or a gate explanation from raw experiment/lineage records instead
  of reading `ep.current_champion` / the stamped `decision` / the structured
  gate verdict;
- you parse a human-readable reason string to recover machine-readable facts
  (the deleted `deriveGateExplain` regex-scraping is the cautionary tale);
- your new payload omits a derived field "because the client can compute it";
- your test fixtures only ever contain one promotion — add a two-promotion
  spine to any fixture that touches champion logic.

---

## Case 5 — The "evolve hang" that was the test suite's reaper killpg-ing a concurrent evolve

**Fix commit:** `9ccbbba` — *fix(evolve): keep a concurrent test session from
killing the loop via its dashboard child*.

### Symptom

`zicato evolve` **with the dashboard** appeared to hang before the first
round, while `--no-dashboard` ran fine. Forensics told a stranger story: the
process died with rc=137 (SIGKILL) within ~1 second of startup, and the log
ended right after the supervisor's port-bind line — which *read* as a startup
hang. The harmonograf launch path kept working, deepening the misdirection.

### Root cause

Two systems, each individually reasonable, composed into a kill:

1. `tests/conftest.py` carried a leaked-dashboard safety net that classified
   **ANY** `python -m zicato.dashboard` process appearing during a pytest
   session as a test leak — selection by argv marker and appearance-time
   only, no ownership proof — and killed it **by process group**:

```python
# BEFORE — tests/conftest.py (git show 9ccbbba^:...)
    before = set(_leaked_dashboard_pids())
    ...
    leaked = [pid for pid in _leaked_dashboard_pids() if pid not in before]
    ...
    for sig in (signal.SIGTERM, signal.SIGKILL):
        os.killpg(pgid, sig)
```

2. `zicato evolve` spawned its dashboard child **in evolve's own process
   group** (plain `create_subprocess_exec`, no `start_new_session`).

So a pytest session opening on the same host while an evolve ran would sweep
the innocent evolve's dashboard — and the group-kill took down the *entire
evolve invocation* sharing that group. The standalone `zicato dashboard`
(different argv, no `-m zicato.dashboard` marker) was exempt, which is why
harmonograf's path was a bystander.

### Why every oracle missed it

- The failure requires **two independent processes on one host** — a running
  evolve AND a concurrently opening test session. No single-process test
  topology can produce it; CI runs the suite in isolation.
- Each side's tests validated its own intent: the reaper reaped fixtures'
  leaks (true), evolve's children spawned and terminated cleanly (true). The
  bug lived in the *composition*, governed by an implicit shared resource
  (process-group membership) neither side's tests modeled.
- The symptom pointed at the victim, not the culprit: everyone debugged
  evolve's startup, because that is where the corpse was.

### The invariant class it defines

Two, both about **process identity**:

- **Blast-radius isolation:** a child that third parties might signal by
  group must be its own session/process-group leader, so group-directed
  signals reach exactly that child's subtree.
- **Provenance before force:** a safety net that kills processes must select
  only processes it can *prove* are its own — a positive ownership
  fingerprint (here: the `--workspace` argv lying inside the session's pytest
  temp root), never "appeared while I was running and matches a shape."
  Appearance-time heuristics classify innocents.

### The fix

Both sides: evolve spawns both children (dashboard service + watchdog
supervisor) with `start_new_session=True` (each its own session leader; a
group-kill of the dashboard takes exactly the dashboard subtree; explicit
teardown signals the child pid directly, unaffected). The conftest reaper
selects only dashboards whose `--workspace` argv lies inside this session's
pytest temp root, and never signals its own process group (bare-pid fallback).

### The regression test pattern that pins it

Reproduce the hostile signal, assert the survivor:
`tests/test_evolve_supervisor_spawn.py` includes a real-process test that
**SIGKILLs the spawned child's whole process group and asserts the spawning
process survives** — the exact live kill, in miniature.
`tests/test_conftest_dashboard_reaper.py` pins the selection contract: a
foreign-workspace dashboard is never signalled; a same-group leak is killed
by bare pid, never killpg. Note the shape: the test does not check for
`start_new_session=True` in the code (an implementation detail) *only* — it
also fires the actual signal and checks who lived.

### You are about to reintroduce this if…

- you add a new child spawn in the evolve/CLI path without
  `start_new_session=True` (the two existing spawn helpers document the
  rationale — copy them);
- you write any test-suite or supervisor "cleanup" that selects victims by
  name/argv shape/appearance window instead of a provable ownership
  fingerprint;
- you reach for `os.killpg` on a group you did not create;
- you debug a "hang" without first checking the exit code and signal — a
  SIGKILL rc=137 one second in is not a hang, and the distinction rewrites
  the suspect list.

---

## Case 6 — Best-of-N tree mismatch (gauntlet): the shared child-slot re-derivation

**Fix commit:** `7025a30` — *fix(proposer): mount the CHOSEN best-of-N
candidate's child tree, not the last-sampled*. **Live on shipped defaults**
(`proposer_quality.best_of_n == 3`).

### Symptom

The tournament could **score a tree that was not the experiment on record**.
With best-of-N sampling, the journaled `experiment.json` described the
critic-chosen candidate while the mounted child snapshot contained the
last-sampled candidate's patches. Every downstream artifact — scalars, gate
evidence, lineage — silently attributed one candidate's behavior to another
candidate's hypothesis.

### Root cause

Every slate sample's post-apply validation derives the SAME fixed child
snapshot in place. The validator seam
(`src/zicato/evolve/round.py::build_post_apply_validator`) is *designed* to
clear and re-derive on retry:

```python
# src/zicato/evolve/round.py — the shared child slot (unchanged, by design)
        child = genstore.derive_generation(
            epoch_id=epoch_id,
            parent_generation_id=parent_id,
            child_generation_id=next_id,     # ONE fixed child id per round
            patches=list(candidate.patches),
        )
        ...
        last_child_snapshot["path"] = child
```

Best-of-N runs this validator once **per slate sample** — N times against one
`child_generation_id` — so after N samples the on-disk tree belongs to the
LAST successfully-validated candidate. The selection step may pick an earlier
one. The evolve pipeline then mounts `last_child_snapshot["path"]` while
persisting the CHOSEN candidate's experiment. One mutable slot
(`next_id`'s snapshot), N logical artifacts (candidate trees), last writer
wins.

### Why every oracle missed it

- **Deterministic contracts pinned `best_of_n = 1`.** With a single sample,
  chosen == last-validated definitionally, and the mismatch cannot occur. The
  convergence oracle, every e2e, and every gate test ran with the interacting
  knob OFF (this is the canonical instance of meta-lesson 2 below).
- When `best_of_n > 1` *was* exercised, the scripted inner proposers returned
  candidates whose critic choice happened to be the last index, or the tests
  asserted the returned `Experiment` object (correct) without inspecting the
  mounted tree (wrong one).

### The invariant class it defines

**Tree/record agreement:** whatever snapshot the tournament mounts must be
derived from exactly the experiment persisted for it. More generally — when a
loop reuses one output slot across iterations and a later step selects among
the iterations, the selection must *re-materialize* the slot for its pick (or
each iteration must get its own slot). "The artifact on disk is whichever
iteration ran last" is never the selection's semantics.

### The fix

At the one seam serving both pipelines
(`BestOfNProposerAgent._align_child_tree`,
`src/zicato/proposer/best_of_n.py`): after selection, if the chosen candidate
is not the last-validated one, re-run `ctx.validate_experiment(chosen)` —
the same idempotent clear-and-reapply a retry performs — so tree and
experiment agree. An unexpected re-validate failure falls back to the
last-validated candidate (restoring *that* tree with one more hook call) and
stamps `:revalidate-fallback` onto the selection mode so the round log
records why the critic's pick was not returned; a double failure raises the
standard `ProposerError`. Either way the pair stays consistent.

### The regression test pattern that pins it

Make the chosen candidate NOT the last one, then check the *tree*, not the
returned object: `tests/test_best_of_n_tree_integrity.py` scripts a
3-candidate slate where the critic picks candidate 0 and asserts the mounted
tree contains candidate 0's patch — through the full known-answer e2e over
target_0 with real subprocess workers, for BOTH pipelines. The wrapper-seam
unit tests in `tests/test_proposer_best_of_n.py` pin the fallback ladder
(re-derive / skip-when-last / fallback / double-failure / raising hook).

### You are about to reintroduce this if…

- you add a new consumer of `last_child_snapshot["path"]` (or any
  "last-attempt" slot) after a selection step, without re-validating the
  selection's pick;
- you add a second sampling loop (a revise pass, a repair retry) that calls
  the post-apply validator and then returns something other than the final
  validated candidate;
- you optimize away the `_align_child_tree` re-derive because "the candidate
  already validated cleanly" — validation *is* the re-derive;
- your test asserts the returned experiment matches expectations but never
  reads the mounted snapshot's bytes.

---

## Case 7 — The field-path extension: same slot bug, plus the diversity misjudgment

**Fix commit:** `7025a30` (same commit as Case 6 — deliberately treated as a
separate case because it fails a *different* mechanism and its oracle gap has
a different shape).

### Symptom

On multi-challenger structures (racing / swiss / elim), the same
tree-vs-experiment mismatch as Case 6 — and one more: the **field-diversity
constraint judged the wrong hypothesis**. The diversity gate compares the
chosen candidate's hypothesis signature against the already-minted field to
soft-reject near-duplicates; it judged the CHOSEN candidate's signature while
the tree that actually entered the field carried the LAST-SAMPLED candidate's
edits. A field could pass diversity screening while being behaviorally
homogeneous — or be rejected as duplicate while behaviorally novel.

### Root cause

Identical slot aliasing to Case 6 — the field pipeline
(`_propose_and_apply_challenger` → `_mint_challenger_field` in
`src/zicato/orchestrator.py`) mounts the shared child snapshot per minted
challenger while persisting the chosen experiment, and additionally feeds the
chosen hypothesis to `_compute_field_diversity`. The diversity judgment is a
*derived decision* over a record that no longer described the artifact — the
mismatch propagated one layer further than in the gauntlet before anything
consumed it.

### Why every oracle missed it

Everything from Case 6, plus: the diversity gate is itself opt-in/soft
(a soft-reject signal), and its tests fed it hypothesis signatures directly —
never through the mount path — so signature-vs-tree agreement was untestable
as specified. There was no assertion anywhere that "the signature judged
belongs to the tree fielded."

### The invariant class it defines

**Derived judgments inherit the identity bug of their inputs.** When record
and artifact can disagree (Case 6's class), every judgment computed from the
record (diversity, parsimony diff-size, exemplar attribution) silently
becomes a judgment about the wrong artifact. Fixing the slot fixes these *by
construction* — which is why the fix commit repaired "all three symptoms" at
one seam rather than patching the diversity call site.

### The fix

The same `_align_child_tree` re-derive (Case 6): once tree == chosen, the
diversity check is correct by construction. No change to the diversity code
itself — the fix is upstream, at the identity seam.

### The regression test pattern that pins it

The field-pipeline e2e in `tests/test_best_of_n_tree_integrity.py` asserts,
for the racing field, the mounted/committed tree, the persisted experiment,
the known scalars, **and the field-diversity signature agreement** — the last
assertion being the one this case adds over Case 6. Pattern to reuse: when a
bug corrupts an *identity*, enumerate every derived judgment downstream of
that identity and pin each one, not just the first artifact.

### You are about to reintroduce this if…

- you add a new per-candidate judgment (novelty, cost estimate, complexity
  score) computed from the experiment record at a point where the mounted
  tree may not be that record's tree;
- you reorder the propose pipeline so any judgment runs between selection and
  the `_align_child_tree` re-derive;
- you cache a hypothesis signature keyed by generation id before the
  generation's tree is final.

---

## Case 8 — Evidence-gate replicate-slot reuse: the soundness device was unsound as wired

**Fix commit:** `eb55266` — *fix(evidence): replicate duels draw fresh at
reserved indices; audit refuses duplicate draws*. The full statistical
context is 04-evaluation-statistics.md §6; this case is the engineering
anatomy.

### Symptom

None visible — worse, the symptom was *reassuring*: the Bradley–Terry
pre-gate's deferred crownings **resolved faster than they should have**. In
fast mode, CIs separated after a modest budget on data that could not
possibly separate them.

### Root cause

Both `_replicate_duel` implementations (gauntlet confirm and multi-challenger
field, `src/zicato/orchestrator.py`) omitted the replication parameters,
defaulting every evidence "replicate" to replicate slot 0 — the tournament's
canonical slot — under one constant matchup id:

```python
# BEFORE — src/zicato/orchestrator.py (git show eb55266^:..., field path)
        async def _replicate_duel(left_id: str, right_id: str) -> MatchupResult:
            return await _run_matchup(
                Matchup(
                    matchup_id=f"bt-replicate:{left_id}:{right_id}",   # same id every call
                    left=Contestant(generation_id=left_id, role="champion"),
                    right=Contestant(generation_id=right_id, role="challenger"),
                )
            )   # no replicates=, no reserved base ⇒ slot 0
```

Three distinct corruptions from one wiring omission:

1. **Fast mode — replay:** the child cache-read its canonical `loss.json`,
   so every replicate was a byte-identical replay of one draw. Feeding N
   copies of one observation to the BT fit shrinks the SE by repetition
   alone — CIs "separate" with no new evidence. **Unsound promotion**, in the
   device that exists to prevent exactly that, and the scaffolded contracts
   ship the gate ON.
2. **Full mode — clobber:** `force_fresh` re-ran at slot 0 and re-persisted
   there, overwriting the canonical `loss.json` that reindex and crash-resume
   key on (Case 1's slot, corrupted by a different writer).
3. **Full mode — one-sided sampling:** the champion side was never re-drawn,
   so the champion's rating variance was understated — CIs narrower than the
   truth even with fresh child draws.

### Why every oracle missed it

- The e2e that covered the loop (`tests/test_gauntlet_evidence_gate_e2e.py`)
  ran under **zero noise**, where a cache replay and a fresh draw are equal
  *by value* — the pre-fix test even documented the replay approvingly as "a
  pure cache read." The deterministic oracle certified the bug as an
  optimization.
- The statistical tests that could have caught it (`test_decision_procedure_power.py`)
  faked the worker boundary and supplied per-duel independence via a seed
  advance in the harness itself — so the *harness* always drew fresh even
  though *production* replayed. The pinned A/A and power numbers were
  computed on the honest behavior the production wiring did not have.
- Nothing asserted the on-disk slot layout during the evidence loop; the
  canonical-slot clobber had no watcher.

### The invariant class it defines

Three, stacked:

- **Independence is a wiring property, not an intention.** "Run another duel"
  produces an independent sample only if every seam (cache slot, seed stamp,
  both sides) is explicitly fresh; the default path re-serves what exists.
- **Reserved index ranges** (the ledger, 04-evaluation-statistics.md §8):
  out-of-tournament draws must live where tournament reads can never find
  them and tournament writes can never be found by them.
- **Structural refusal beats caller discipline:** the consumer of evidence
  (the driver's audit) must itself refuse duplicate data
  (`seen_matchup_ids`), because a future caller WILL get the wiring wrong
  again.

### The fix

`EVIDENCE_REPLICATE_BASE = 4000` joins the reserved ladder; a
`replicate_base` parameter threaded `run_matchup → _run_replicated` puts
evidence replicate j at `4000+j`, drawing BOTH sides fresh on a natural cache
MISS (idempotent on a resumed confirm); matchup ids encode the slot
(`bt-replicate:r{slot}:{left}:{right}`); the driver's confirm loop refuses to
append a result whose matchup id already appears in the audit while still
counting the spend (a duplicate that did not count would loop forever); the
field replicate stops overwriting `gen_score.json` with a single-draw
aggregate (`cache_scores=False`).

### The regression test pattern that pins it

The commit added all three watchers, and each is a reusable pattern:

- **Variance-of-draws:** consecutive evidence replicates must produce
  distinct deltas (`len(set(deltas)) > 1`) and the CHAMPION's own scalars
  must vary — pinned under seeded noise where a replay has zero variance
  (`test_evidence_replicates_are_independent_draws`).
- **Slot integrity:** canonical `loss.json` files byte-identical across the
  evidence loop; the draws persisted under `r4000+` for both sides; the
  champion's reserved draws differ from its canonical r0
  (`test_full_mode_evidence_loop_never_touches_canonical_slots`, run with the
  real persist path enabled).
- **The structural guard in isolation:** a driver unit test where the
  replicate runner replays one draw — the budget is spent, the audit never
  grows, the verdict passes through instead of a repetition-driven crown.

And the honesty discipline: the zero-noise e2e was updated to state what it
does and does not prove, with statistical soundness explicitly delegated to
the noisy harness.

### You are about to reintroduce this if…

- you write a new `run_matchup` caller for any extra/out-of-band duel and do
  not pass a reserved `replicate_base` (grep the ledger first);
- you mint matchup ids that do not encode the draw's identity — the audit
  guard is only as good as the id's uniqueness-per-draw;
- you treat a fast-mode cache hit as "a sample" in any fit, mean, or CI;
- you re-enable `gen_score.json` caching (or any aggregate persist) inside a
  loop that produces partial/single-draw aggregates;
- your new statistical loop's test suite is entirely deterministic
  (meta-lesson 2 — the exact hole this bug lived in).

---

## Case 9 — git `derive_generation` re-derive: the tag moved, the checkout didn't

**Fix commit:** `7025a30` (the "Also fix" paragraph — exposed by Case 6's
repair, which made re-derives of one child id a hot path).

### Symptom

After a re-derive of an already-materialised child generation (a proposer
retry after failed post-apply validation, the best-of-N chosen-candidate
re-derive of Case 6, a crash-resume re-validate), **direct readers of
`snapshot_root` saw the OLD attempt's tree** — a tree that no longer matched
the generation's commit. Workers, which mount the tag via ephemeral checkout,
saw the new content; in-process readers of the shared worktree saw the stale
one. Two views of one generation, silently different.

### Root cause

`GitGenerationStore.derive_generation` of an existing child moved the
generation **tag** to the fresh commit but left the shared **worktree**
checked out at the old commit. The directory backend clears and rebuilds the
child tree on re-derive, so only the git backend had the two-representation
split: tag (authoritative) vs materialised worktree (a cache of the tag that
nothing invalidated).

### Why every oracle missed it

- Re-derives of the same child id were rare before Case 6's fix made them a
  designed operation — the bug was *latent* until an adjacent repair promoted
  the code path from "error recovery" to "every best-of-N round."
- The genstore conformance tests asserted `derive → snapshot_root` returns
  the right content for a FIRST derive; no test re-derived the same id with
  different patches and re-read.
- Workers and direct readers disagreeing is invisible to any test that only
  uses one of the two access paths.

### The invariant class it defines

**Every cache of an authoritative pointer needs an invalidation step in the
pointer's mutation path.** The tag is authority; the materialised worktree is
a cache; moving the authority without invalidating the cache is the whole
bug. It is Case 2's shared-registry blindness in its logical (single-threaded)
form — no race required, just two representations and one update.

### The fix

Drop the stale checkout in the mutation path; the next `snapshot_root`
re-materialises from the moved tag (its `worktree add` path prunes the
orphaned registration first):

```python
# AFTER — src/zicato/epoch/git_genstore.py::derive_generation
        self._commit(message)
        self._tag_generation(epoch_id, child_generation_id)
        # A RE-derive of the same child id ... moves the tag to the fresh
        # commit — but a worktree materialised by an EARLIER attempt stays
        # detached at the old commit ... Drop the stale checkout;
        # ``snapshot_root`` below re-materialises it from the moved tag.
        stale_worktree = self._worktree_path(epoch_id, child_generation_id)
        if stale_worktree.is_dir():
            shutil.rmtree(stale_worktree, ignore_errors=True)
        return self.snapshot_root(epoch_id, child_generation_id)
```

### The regression test pattern that pins it

Re-derive the same child id with **different** patches, then read back
through `snapshot_root` and assert the SECOND patch set's content — on the
git backend specifically (the directory backend passes vacuously). The
integrity e2e additionally asserts the mounted and committed trees agree,
which catches any future re-split of the two representations.

### You are about to reintroduce this if…

- you add a new mutation of a generation's commit/tag (amend, repair,
  re-derive variant) without invalidating the materialised worktree;
- you add a second cached representation of any store-authoritative object
  (an extracted archive, a content index) without wiring its invalidation
  into every path that moves the authority;
- you test store operations only through one access path (worker mount OR
  direct read) — agreement between paths is itself a contract.

---

## Case 10 — The contract hash embedded the checkout path: identity vs location

**Fix commit:** `8d0a94f` — *fix(contract): the hash identifies the contract,
not the checkout*. BREAKING (one-time hash move; workspaces auto-roll once).

### Symptom

The same workspace **hashed differently when evolve ran from a different
directory** — or after the workspace moved — and spuriously rolled its epoch:
lineage reset, warm-start lost, an "epoch roll" event with no contract
change. The parity CONTRACT-HASH gate surfaced it as a golden that was red in
every checkout except the one that captured it.

### Root cause

`_canon_mutable_trees` (`src/zicato/epoch/contract.py`) canonicalized the
registered mutable-tree paths by **filesystem-resolving** them:

```python
# BEFORE — src/zicato/epoch/contract.py (git show 8d0a94f^:...)
def _canon_mutable_trees(mutable_trees: tuple[str, ...]) -> str:
    """Canonical form of the mutable trees: sorted absolute path strings."""
    resolved = sorted(str(Path(p).resolve()) for p in mutable_trees)
    return "\n".join(resolved)
```

`Path(p).resolve()` folds the **process cwd** (for relative registrations)
and the **absolute checkout location** into the string that gets hashed. The
identity being hashed is "which subtrees of the target are mutable" — a
property of the registration — but the implementation hashed "where those
subtrees live on this machine today."

### Why every oracle missed it

- Every test computes the hash inside one process with one cwd and one
  checkout — invariance across cwds/locations was never a test axis, and
  `resolve()` is the reflexive "canonicalize a path" idiom that reads as
  correct in review.
- The golden could not catch it alone either: a golden captured in checkout A
  fails in checkout B, but that failure mode reads as "stale golden,
  re-capture" unless someone asks *why* the bytes differ. The parity gate did
  its job only when someone treated the red golden as a signal instead of a
  chore.

### The invariant class it defines

**Identity vs location.** Anything that participates in an identity — a
contract hash, a cache key, a seed tuple, a dedup fingerprint — must be
derived from intrinsic properties, never from environmental accidents: cwd,
absolute paths, hostnames, tempdir names, process ids, wall clock. (The power
harness's seed discipline — "nothing about process ids, tempdir names, or the
clock leaks into the measurement" — is the same invariant on the statistics
side; the ephemeral snapshot's content-digest fallback identity in the
target_0 harness is another.) Corollary: `Path.resolve()` in any
hash/key/identity context is a red flag in review.

### The fix

Normalize, never resolve: `.`/`..`/separator spelling collapsed,
POSIX-rendered, sorted — pure string computation with no filesystem contact:

```python
# AFTER — src/zicato/epoch/contract.py::_canon_mutable_trees
    normalized = sorted(PurePosixPath(os.path.normpath(p)).as_posix() for p in mutable_trees)
    return "\n".join(normalized)
```

Registration order still never moves the hash; adding/removing a tree still
does. The one-time hash move was taken as a **declared BREAKING change**
(CHANGELOG'd, standard contract-roll behavior) rather than a compatibility
shim — the hash was wrong; keeping it stable would have frozen the wrongness.

### The regression test pattern that pins it

Invariance as an explicit axis: compute the hash from **two unrelated cwds**
and assert identity; compute it with different path *spellings*
(`./x`, `x/`, `a/../x`) and assert identity; and re-capture the golden from a
checkout-independent computation so it is green everywhere. Pattern to reuse
for any identity: enumerate the environmental accidents it must ignore and
pin each one as a test dimension.

### You are about to reintroduce this if…

- you add a field to `ContractInputs` and canonicalize it with anything that
  touches the filesystem or the environment (`resolve()`, `expanduser()`,
  `os.getcwd()`, `socket.gethostname()`);
- you build a new cache key or dedup fingerprint containing an absolute path
  "for uniqueness";
- you fix a red golden by re-capturing it without first explaining the byte
  diff (see 11-testing.md §"Goldens are hypotheses");
- you canonicalize paths differently in two places that feed one identity
  (normalize in the hash, resolve in the serializer — they will disagree on
  the first symlink).

---

## The meta-lessons

Ten bugs, three shapes. If you internalize nothing else from this chapter,
internalize these.

### M1 — Shared mutable state behind per-X artifacts is THE recurring class

Count them: one `loss.json` serving as both worker output and r0 cache slot
(Case 1); one worktree admin registry under concurrent multi-command windows
(Case 2); one child-snapshot slot under N slate candidates (Cases 6, 7); one
materialised worktree under a moving tag (Case 9); one canonical slot under
evidence "replicates" (Case 8); one process group under two owners' signals
(Case 5). **Six of ten cases are the same bug** at different addresses: N
logical artifacts multiplexed onto one physical resource, with last-writer-
wins semantics nobody chose.

The audit question to ask of any design, mechanically: *"For every artifact
this code produces, how many logical instances exist per round × per
replicate × per candidate × per attempt — and does each get its own slot?"*
If the answer involves the word "reuse," write down who invalidates and when,
or you are scheduling a casebook entry.

### M2 — Deterministic test contracts pinning interacting knobs OFF is how bugs hide

Case 6/7 hid behind `best_of_n = 1` in every deterministic contract. Case 8
hid behind σ=0, where replay equals fresh-draw by value — the e2e *praised*
the replay. Case 3 hid behind a deterministic harness whose correct answer
equaled the bug's output. Case 1 needed both `replicates > 1` AND σ>0 to be
observable. The pattern: the oracle's world is configured so the interaction
that breaks cannot occur, and the green suite certifies the configuration,
not the code.

**The countermeasure is adversarial knob-ON tests:** for every pair of
interacting knobs (replication × caching, best-of-N × selection, noise ×
evidence), at least one test must run with both engaged, in a world where
their interaction has observable consequences (seeded noise, multi-candidate
slates, multi-promotion spines, concurrent checkouts). When you add a knob,
your test-plan question is not "does the knob work?" but "which existing
mechanisms does this knob interact with, and where is the test that runs
both?" The power harness (04-evaluation-statistics.md §13) exists to make
knob-ON statistical worlds cheap to build — use it.

### M3 — "The oracle must fail with the fix stashed" is a hard rule

Every regression test in this chapter was validated by demonstrating the red
state: the killpg test fires the actual hostile signal; the slot-integrity
test watches actual bytes; the champion test uses a spine where first ≠ last;
the hash test computes from two real cwds. A regression test that passes both
with and without the fix is documentation-flavored noise — it pins nothing
and it will wave the bug through on reintroduction.

Procedure, every time you fix anything:

1. Write the test.
2. `git stash` the fix (or revert the fix hunk).
3. Run the test. **It must fail, and fail for the stated reason** (read the
   failure text — a test failing on an import error is not pinning your bug).
4. Restore the fix; the test goes green.
5. Only then commit, with the test and fix in one commit so `git show` on the
   fix is forever the executable spec of the bug.

Two corollaries. First, prefer testing *observable consequences* (who
survived the signal, which bytes changed, which tree got mounted) over
implementation details (which flag was passed) — consequence tests survive
refactors and keep failing for the right reason. Second, when a fix's
"honest" numbers move an existing pinned expectation (Case 8 changed the
e2e's budget expectations), update the expectation **in the fix commit with
the explanation inline** — a silently adjusted pin is indistinguishable from
a fudged one.

### The pre-flight checklist for your own change

Before you open a PR against any surface this book touches, answer in
writing:

1. Which physical slots does my change write, and how many logical instances
   map onto each? (M1)
2. Which knobs does my change interact with, and which test runs my code with
   those knobs ON in a world where the interaction is observable? (M2)
3. Does my regression test fail with my fix stashed, for the stated reason?
   (M3)
4. Is every identity my change constructs derived from intrinsic properties
   only? (Case 10)
5. Does any consumer of my change re-derive a truth some owner already
   stamps? (Case 4)
6. If my change kills, sweeps, or cleans anything: can it *prove* ownership
   of every victim? (Case 5)

Cross-references: the statistical machinery these bugs corrupted is
04-evaluation-statistics.md; the storage/worktree model behind Cases 2 and 9
is 07-runtime-and-durability.md; the testing discipline (oracles, goldens,
monkeypatch anchors) is 11-testing.md; how new work gets proposed so it does
not become Case 11 is 14-goals-and-roadmap.md §"How to propose new work".
