# 08 — The Supervisor

> **Covers.** The Rust supervisor (`crates/supervisor/`) as zicato's
> out-of-band notary: its process model, the module map, every guarantee it
> makes (warn-only heartbeat, pid-safety, clamped deadlines,
> confirmed-dead-only reaping, the hash-chained ledger, diff-containment,
> promotion gatekeeping, the divergence auditor, the read-only SQLite
> discipline), the kill-request single-escalator handshake, build/packaging
> and binary resolution, the Rust dev workflow, and the recipe for adding a
> control route.
>
> **Prerequisites.** 07-runtime-and-durability.md (every file the supervisor
> reads is defined there: heartbeat, active runs, the tournament event log,
> control files, `index.db`), 02-architecture.md (the process topology). The
> Python dashboard service is 09-dashboard-and-query.md; the cargo gates are
> summarised in 11-testing.md §"The pre-commit checklist".
>
> **Invariants introduced in this chapter.** The ID is the locator that other
> documents cite; the Name is what prose uses.
>
> | ID | Name | Invariant |
> |----|------|-----------|
> | S1 | out-of-band supervision | The supervisor is a separate OS process that communicates with the orchestrator ONLY through atomic state files. It never shares memory, sockets, or locks with the loop it audits. |
> | S2 | the never-kill-the-orchestrator rule | The supervisor never kills the orchestrator. `decide_heartbeat` has no `Kill` variant by construction; a deeply stale heartbeat escalates the WARNING, nothing else. |
> | S3 | the vetted-pid-signalling rule | A worker pid is signalled only after the full vetting chain: `is_signalable_run_pid` (never pid ≤ 1, never self, never a protected pid) AND `is_same_process(pid, pid_start_time)` (pid-reuse immunity). |
> | S4 | the clamped-deadline rule | Orchestrator-written deadlines are untrusted: the enforced cutoff is clamped to `started_at + max_run_seconds`, so a run is always killable no matter what deadline was written. |
> | S5 | the confirmed-death-before-reaping rule | Reaping (orphan workers + `ztw-snap-*` snapshots + state-file finalization) happens ONLY after a CONFIRMED orchestrator death — an identity check on the heartbeat pid, never a stale timestamp. |
> | S6 | the path-confined snapshot collection rule | Snapshot GC removes only a `ztw-snap-*` root that is a strict descendant of the system temp dir. Any other path is refused, however it got into the record. |
> | S7 | the ledger-records-never-gates rule | The audit ledger is append-only, hash-chained, fsynced per append, torn-tail-repaired at open, and verified on startup. It records; it never gates. |
> | S8 | the read-only version-pinned index rule | The supervisor opens `index.db` read-only, refuses a `user_version` that does not equal its pinned `EXPECTED_SCHEMA_VERSION`, and every index-backed endpoint degrades to an empty/`null` payload with a `note` rather than a 500. |
> | S9 | the sole-worker-signaller rule | The supervisor is the SOLE signaller of worker pids on the escalation path. The Python parent requests kills by writing markers; it never signals workers itself. |
> | S10 | the read-only fail-open integrity check | Every integrity-notary check (diff containment, promotion gate, divergence) is read-only and fail-open on the supervisor side: it alarms on positive observed evidence and reports nothing when the attestation cannot be made. |
> | S11 | the two-loops fixed-trigger-priority rule | The two loops (`heartbeat_loop`, `runs_loop`) are the only active code. Each is a pure-decision-function-plumbed `tokio::time::interval` select-loop; `runs_loop` applies its per-run triggers in a FIXED priority order (confirmed-death reap → kill-request → deadline → staleness), and every escalation is mirrored to the in-memory action ring AND — when configured — the ledger. |
> | S12 | the never-block-never-leak live surface rule | The live surface never blocks and never leaks write intermediates: the filesystem watcher drops `*.tmp` atomic-write intermediates and per-file-debounces; the SSE broker is a bounded broadcast that drops a slow client rather than blocking the watcher or other clients. |
> | S13 | the no-cached-state-across-ticks rule | The supervisor holds NO cached state across ticks except the explicitly-carried trackers (`SeqLiveness`, the integrity de-dup sets, the ledger tail) and the process-lifetime counters. `WorkspacePaths` is read fresh every tick and is the Rust twin of `zicato.runtime.paths`. |
> | S14 | the operational-not-analytical HTTP surface rule | The HTTP read surface is operational. Analytical tournament and health projections belong to `zicato.query`; the supervisor may inspect the index for alarms but never serves it as business truth. |

---

## 8.1 The process model: an out-of-band notary

The supervisor exists because the orchestrator cannot audit itself. A wedged
event loop cannot notice it is wedged; a process that rewrites its own
records is not a trustworthy witness to them. So the supervisor is:

- **a separate OS process** — `zicato-supervisor`, a Rust binary spawned by
  `zicato evolve` (with `--no-dashboard`, so it runs the watchdog loop and
  its `/statusz` surface only) or run standalone by the operator / `zicato
  dashboard`;
- **a pure reader of atomic state files** — everything it knows comes from
  the files chapter 07 defines, read fresh each tick through
  `crates/supervisor/src/reader.rs`. Because every Python writer is atomic
  (tmp→fsync→rename), the supervisor never needs a lock and never observes a
  torn record — out-of-band supervision;
- **never a peer in memory** — no shared queues, no IPC channel, no port the
  orchestrator must answer on. The one "write channel" back toward the loop
  is the same control-file protocol everyone else uses (§8.5, §8.11).

This is what makes its guarantees meaningful: a deadline kill fires even when
the orchestrator's event loop is parked, because nothing about the supervisor
depends on the orchestrator being responsive.

```rust
//! Deadline enforcement is a first-class, default-on trigger: every
//! board-entry run carries a `deadline` (`started_at +
//! wall_clock_budget_seconds`). When `now` passes that deadline the
//! watchdog SIGTERM→SIGKILLs the run's worker pid. Because the supervisor
//! is its own OS process this holds even when the orchestrator's event
//! loop is wedged.
```
— `crates/supervisor/src/watchdog.rs` (module docstring)

A second structural rule keeps the code testable: **every decision is a pure
function of `(state, now, thresholds)`** — `decide_heartbeat`, `decide_run`,
`decide_run_deadline`, `decide_run_kill_request`, `decide_orchestrator_dead`,
`resolve_kill_target`, `reapable_snapshot_root`, `check_row` — and the async
loops just plumb them into `tokio::time::interval`. When you extend the
supervisor, put the decision in a pure function with unit tests beside it and
keep the loop body dumb.

---

## 8.2 Module map — `crates/supervisor/src/`

| Module | Role |
|---|---|
| `main.rs` | CLI (`clap`) + wiring: spawns the two watchdog loops, the filesystem watcher, the HTTP server; opt-in flags for the integrity notary (`--diff-containment`, `--promotion-gate`, `--divergence-audit`, `--ledger-dir`); `--read-only`, `--no-dashboard`, `--daemon`. |
| `lib.rs` | Library facade so the integration tests exercise the same code paths without spawning the binary. |
| `watchdog.rs` | The two loops (`heartbeat_loop`, `runs_loop`) and every pure decision function + `Thresholds` + `SeqLiveness`. |
| `reader.rs` | Reads runtime state files into snapshots; `WorkspacePaths` (the path map twin of `zicato.runtime.paths`); kill-request read/clear; lineage/active-run views. |
| `state.rs` | Serde wire structs mirroring the Python dataclasses — every field `#[serde(default)]`. |
| `signal.rs` | POSIX signal helpers: liveness, `pid_start_time`, `is_same_process`, pgid guards, SIGTERM→grace→SIGKILL escalation (`escalate_target`). |
| `reap.rs` | Confirmed-dead determination + prefix-guarded `ztw-snap-*` snapshot GC. |
| `ledger.rs` | The tamper-evident hash-chained audit ledger + `TransitionObserver` (decision/contract-change observation). |
| `sha256.rs` | Small dependency-free SHA-256 (ledger digests, diff-containment file hashes). |
| `diff_containment.rs` | Integrity record #2: out-of-bounds mutation scan (parent↔child snapshot diff vs the registered mutable surface). |
| `promotion_gate.rs` | Integrity record #3: re-derive the gate's scalar rule per recorded promotion (`check_row`). |
| `divergence.rs` | Integrity record #4: canonical-vs-index join auditor. |
| `index_db.rs` | Read-only SQLite access; `EXPECTED_SCHEMA_VERSION` pin; best-effort row readers. |
| `epoch.rs` | Assembles the current epoch's contract view for the dashboard. |
| `run_log.rs` | Tails recent goldfive events for the log panel. |
| `fold_stats.rs` | Cumulative torn-write / seq-gap counters over the tournament-log fold, surfaced on `/statusz`. |
| `routes.rs` | HTTP handlers; the read-only 403 guard; control-marker writers. |
| `server.rs` | Axum server; binds the first free port in `--port..=--port+10`. |
| `sse.rs` / `watcher.rs` | SSE broker fed by the inotify/FSEvents watcher (100ms debounce). |
| `statusz.rs` | The watchdog's own minimal operational surface (`/statusz`, `/statusz.json`). |
| `static_assets.rs` | Compile-time embedded dashboard assets. |
| `action_log.rs` | In-memory ring buffer of recent watchdog escalations. |
| `log.rs` | Tracing subscriber init. |

Two loops own everything active: `heartbeat_loop` (orchestrator liveness,
warn-only) and `runs_loop` (kill requests, confirmed-dead reaping, per-run
deadline + staleness enforcement, and — when enabled — the three
integrity-notary scans plus ledger transition observation).

---

## 8.3 Guarantee: the warn-only heartbeat — no `Kill` variant by construction

A watchdog that kills the orchestrator on a stale heartbeat kills healthy runs,
because a slow LLM call ages the timestamp while the loop makes real progress.
This is the headline finding of `docs/design/FUNCTIONALITY-RECOMMENDATIONS.md`,
and the answer is to remove the capability rather than to raise the threshold:

```rust
/// **The watchdog never kills the orchestrator.** An orchestrator whose
/// heartbeat has gone stale may simply be slow — a GC pause, a slow LLM
/// endpoint, or a process paused under a debugger — none of which is a
/// reason to destroy in-flight tournament work. Past the kill threshold
/// the watchdog therefore *escalates the warning* (`Stale`) rather than
/// signalling the orchestrator pid; automatic orchestrator restart is a
/// process-supervisor concern (systemd/supervisord/k8s), exactly as
/// RUNTIME.md §3.2 and ROBUSTNESS.md §2.4 already promise. There is no
/// `Kill` variant by design: `decide_heartbeat` cannot ever produce one.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HeartbeatAction {
    /// Heartbeat is fresh; nothing to do.
    Nothing,
    /// Heartbeat is stale past the warn threshold — surface it.
    Warn,
    /// Heartbeat is stale past the (former kill) deep-stale threshold —
    /// surface it more loudly, but still **only a warning**. The
    /// orchestrator pid is never signalled.
    Stale,
    /// No heartbeat file / no `last_heartbeat` field yet.
    MissingHeartbeat,
}
```
— `crates/supervisor/src/watchdog.rs`

**What goes wrong without it:** the watchdog kills the very loop it exists
to protect — a paused epoch (frozen `seq`, fresh timestamp), a debugger
session, or one slow model call destroys hours of in-flight tournament work.
The `heartbeat_stale_kill` threshold survives only as the *deep-stale*
boundary that raises `Warn` → `Stale`.

Liveness itself is seq-first (see 07-runtime-and-durability.md §7.6.1). The `SeqLiveness` tracker
is shared between `heartbeat_loop`, which advances it, and `/statusz`, which
reads it without advancing. It classifies on the age of the last **seq change**
when the heartbeat carries a `seq`, and falls back to timestamp age for a
heartbeat that carries none. A fresh timestamp over an unmoving seq
reads as stale, which catches a wedged loop; a slow call between two transitions
reads as alive, which spares a working one. Both paths share `classify_age`, and
neither can return a kill.

> ⛔ NEVER add a code path that signals the pid carried by
> `heartbeat.json`. If you believe you need one, you are re-introducing the
> verified bug; restart-on-death belongs to an OS-level process supervisor.

---

## 8.4 Guarantee: pid-safety and `(pid, start_time)` identity

Every signal the supervisor sends passes a two-stage guard: the
vetted-pid-signalling rule.

**Stage 1 — the signalable set** (`is_signalable_run_pid`): never pid ≤ 1
(pid 0 addresses the whole process group; pid 1 is init), never the
supervisor's own pid, never a pid in the protected set (the orchestrator's
heartbeat pid). Pure function; unit-tested without spawning anything.

**Stage 2 — identity** (`signal::is_same_process(pid, pid_start_time)`): a
pid number is not an identity — after the owner exits, the kernel can reissue
the number to an unrelated process. The worker records its start-time token
(Linux `/proc/<pid>/stat` field 22) into `active_runs/{run_id}.json` as
`pid_start_time`; the supervisor re-reads the live token and signals only on
a match. The Python twin is `zicato.runtime.lock.is_same_process` — the two
implementations must keep agreeing on the token's meaning (both carry it as a
float; the values are integer-valued so equality is exact — see the field
comment in `crates/supervisor/src/state.rs`).

**Group kills are guarded twice more.** Workers are spawned with
`start_new_session`, so a worker leads its own process group (`pgid == pid`).
`resolve_kill_target` upgrades a vetted single-pid kill to a group kill only
when ALL of: the record carries a `pgid`; that pgid IS the vetted leader's
own pid (a pgid ≠ pid is a foreign group nobody identity-matched — refuse);
and `is_negatable_pgid` passes (pgid > 1, and neither the supervisor's nor the
orchestrator's own group, computed by `protected_pgids`). Any failure falls
back to the always-safe single-pid `KillTarget::Leader`.

**What goes wrong without it:** a recycled pid gets an innocent process
SIGKILLed; a negated foreign pgid takes down the supervisor or the
orchestrator itself (`kill(-pgid, …)` signals every member); grandchildren
(shells and helper tools the inner harness spawned) leak when only the leader
dies — the group kill exists to collect them, and its guards exist so it can
never collect anything else.

---

## 8.5 Guarantee: untrusted, clamped deadlines

The per-run deadline is orchestrator-written and therefore untrusted — a
far-future value (bug or hostility) would silently disable the watchdog. The
enforced cutoff is the clamped `effective_deadline`, which is the
clamped-deadline rule:

```rust
/// The deadline a run record carries is orchestrator-written and untrusted:
/// a far-future value would disable the watchdog. When the run has a
/// `started_at` we cap the enforced cutoff at `started_at + max_run_seconds`,
/// so the run is always killable no matter what deadline was written. When
/// `started_at` is absent there is no anchor to clamp against, so the
/// written deadline is used as-is (the watchdog has nothing better).
///
/// Returns `None` only when the run carries no deadline at all.
pub fn effective_deadline(
    run: &crate::state::ActiveRun,
    max_run_seconds: Duration,
) -> Option<DateTime<Utc>> {
    let written = run.deadline?;
    let Some(started) = run.started_at else {
        // No anchor → cannot clamp; honour the written deadline.
        return Some(written);
    };
```
— `crates/supervisor/src/watchdog.rs`

`decide_run_deadline` then walks: before the effective deadline → `None`;
past it within `--run-kill-grace` → `Sigterm`; past it + grace with the
worker still alive → `Sigkill`. Pid vetting applies before anything is sent; a worker that exits during the grace collapses back to `None`.

The staleness trigger (`decide_run`) is separate and complementary: it fires
on `last_progress` not advancing. Its kill threshold is `2 × the run's own
budget` when the record carries one; the fixed `run_stale_kill` is the backstop
for budget-less records. The worker's `RunHeartbeatBeater` bumps every ~3s, so
staleness past that threshold means the worker process itself is wedged rather
than merely waiting on a slow model.

**What goes wrong without the clamp:** one malformed deadline makes one run
immortal; the whole point of an out-of-band budget enforcer evaporates on
the very input it exists for. `--max-run-seconds` defaults to 6h — far
above any per-board budget, so legitimate runs are never clipped.

---

## 8.6 Guarantee: confirmed-dead-only reaping and the `ztw-snap-` contract

When the orchestrator dies mid-run, its workers are orphaned and each run's
ephemeral checkout (`${TMPDIR}/ztw-snap-*`) is leaked. The supervisor is the
only process positioned to clean up, and the danger is cleaning up a
*slow* orchestrator's live work. Two rails carry the
confirmed-death-before-reaping rule and the path-confined snapshot collection
rule (`crates/supervisor/src/reap.rs`):

**Rail 1 — conservative dead determination.** `decide_orchestrator_dead`
returns true ONLY when the heartbeat's pid fails the identity check (gone, or
a recycled-pid impostor). Staleness is irrelevant; the unit test says it
outright:

```rust
    #[test]
    fn alive_orchestrator_is_not_dead_even_when_stale() {
        // The supervisor's own pid stands in for a live orchestrator. A
        // wildly stale timestamp must NOT flip it to dead — only liveness
        // matters. This is the slow-but-alive case we refuse to reap.
        let me = std::process::id() as i32;
        let hb = Heartbeat {
            pid: Some(me),
            // An absurdly old timestamp: staleness must not trigger a reap.
            last_heartbeat: Some(Utc::now() - chrono::Duration::days(7)),
            ..Default::default()
        };
        assert!(
            !decide_orchestrator_dead(Some(&hb)),
            "a live (if stale) orchestrator must never be declared dead",
        );
    }
```
— `crates/supervisor/src/reap.rs`

On a confirmed death, `reap_dead_orchestrator_runs` (in `watchdog.rs`) does
three things per active run: group-kill the worker through the same vetted
escalation path the other triggers use; GC the leaked snapshot; and — unlike
the alive-orchestrator triggers, which deliberately LEAVE state files for the
orchestrator's own reaper — remove `active_runs/{run_id}.json`, because there
is no orchestrator left to finalize it.

**Rail 2 — the prefix-guarded rmtree.** `reapable_snapshot_root` is the only
path by which the supervisor ever deletes a directory, and it refuses
everything that is not provably a run's ephemeral checkout:

- the recorded `snapshot_path` must be absolute;
- walking ancestors (self first), the FIRST directory whose basename starts
  with `SNAPSHOT_PREFIX` (`"ztw-snap-"`) is the candidate root;
- that root must be a STRICT descendant of the canonicalized system temp dir
  (`$TMPDIR`-honouring, symlink-resolved), and never the temp dir itself;
- the basename prefix is re-asserted at the return (belt and braces).

Anything else — a path outside the temp dir, no `ztw-snap-*` ancestor, an
empty or relative path — yields `None`, and the caller logs a refusal.

This is one half of a **two-language contract**. The Python side is
`zicato.epoch.genstore.EPHEMERAL_SNAPSHOT_PREFIX` and the
`checkout_ephemeral` contract (every backend's per-run tree lives under a
`ztw-snap-{run_id}-*` mkdtemp parent in the OS temp dir — see
07-runtime-and-durability.md §"checkout_ephemeral — per-run detached
worktrees"). The Rust constant carries the pointer:

```rust
/// The mkdtemp prefix every generation-store backend uses for a run's
/// ephemeral snapshot root (`tempfile.mkdtemp(prefix="ztw-snap-…")`). Must
/// match `zicato.epoch.genstore.EPHEMERAL_SNAPSHOT_PREFIX`.
pub const SNAPSHOT_PREFIX: &str = "ztw-snap-";
```
— `crates/supervisor/src/reap.rs`

> ⛔ NEVER change the `ztw-snap-` prefix, move ephemeral checkouts out of the
> system temp dir, or add a second deletion path that bypasses
> `reapable_snapshot_root`. Break the first two and crashed runs leak disk
> forever (the reaper refuses to touch them); break the third and a
> malformed or hostile `snapshot_path` can delete an arbitrary tree.

**What goes wrong without the dead-confirmation rail:** a slow orchestrator's
runs get reaped out from under it, which is the same class of self-inflicted
kill that §8.3 closes for the orchestrator itself.

---

## 8.7 Guarantee: the hash-chained audit ledger

Everything else the supervisor observes lands in state the orchestrator can
rewrite. The ledger (`crates/supervisor/src/ledger.rs`) is the record the
audited party cannot edit undetected, under the ledger-records-never-gates
rule: opt-in via
`--ledger-dir` (which should point OUTSIDE the orchestrator's mutable trees),
one JSONL file `audit_ledger.jsonl`, one hash-chained record per line:

```text
{"seq":N,"prev":"<hex>","ts":"<rfc3339>","kind":"...","payload":{...},"digest":"<hex>"}
```

**Chain rules.** `digest = SHA-256(seq ‖ prev ‖ ts ‖ kind ‖
canonical(payload))` with `0x1f` unit separators between fields; `prev` is
the previous record's digest; genesis links to 64 zeros (`GENESIS_PREV`).
Removing, reordering, or editing any record breaks the chain at a specific
`seq`. Record kinds are a closed serde enum (`RecordKind`):
`supervisor_start`, `watchdog_action`, `decision_observed`,
`contract_change`, `diff_containment_alert`, `promotion_contradiction`,
`divergence_finding` — new kinds are purely additive (the digest covers the
raw kind string, so an older verifier still hash-checks an unknown kind).

**Open sequence** (`AuditLedger::open`), in order:

1. **Torn-tail repair** — `repair_torn_tail` truncates a trailing
   half-written line BEFORE anything reads or chains onto the file.
   Deliberately surgical: only the TRAILING unparseable line can be a torn
   append (the writer emits exactly one `line + '\n'` per record and never
   rewrites earlier bytes); an unparseable *interior* line is left in place
   for `verify_chain` to flag as a break. Same tail doctrine as `RoundLog`
   (07-runtime-and-durability.md §"Torn-tail truncation on the write path").
2. **Verify-on-startup** — `verify_chain` walks the repaired file,
   recomputing every digest and checking every `prev` link; a break is
   surfaced as a WARN with `first_break_seq` + reason. Alarm-only: the
   supervisor still starts.
3. **Tail seeding** — the in-memory `(next_seq, prev)` tail is derived from
   the last record so a restart continues the chain.

**Append semantics.** `append` serializes, writes, flushes, and
`sync_all()`s — fsync-per-append is affordable at a handful of records per
run, and durability is the whole point of an audit trail. On ANY I/O error
the in-memory tail is NOT advanced, so the next append retries the same
`seq`/`prev` rather than chaining onto a record that never reached disk;
best-effort tail semantics all the way down. `verify` re-reads the file
fresh (never the in-memory tail) so out-of-band tampering since the last
append is caught.

**What feeds it.** `record_action` mirrors every watchdog escalation into
the ledger alongside the in-memory ring; `TransitionObserver` stamps each
newly observed promote/reject decision and epoch contract-hash change
(de-duplicated, so a steady-state poll appends nothing); the three
integrity-notary scans append their findings. `/statusz` shows a chain-break
indicator and `GET /api/audit/verify` runs a full walk — both stay mounted
even under `--no-dashboard` (chain integrity must be checkable in
watchdog-only mode).

> ✅ ALWAYS route a new supervisor-observed event through
> `AuditLedger::append` with a new additive `RecordKind` — never write the
> ledger file directly, and never make ledger success a precondition for the
> action itself. The ledger records; it never gates.

---

## 8.8 Guarantee: the integrity notary — three read-only scans

All three are per-tick scans in `runs_loop`, off by default, each behind its
own flag, each writing its latest result into a shared findings store that
`/statusz` surfaces and (when configured) the ledger records. All three obey
the read-only fail-open integrity check: read-only, alarm-only, fail-open.

### 8.8.1 Diff containment (`--diff-containment`, record #2)

For each materialised child generation, independently recompute the on-disk
diff of the child snapshot against its PARENT snapshot and assert every file
OUTSIDE the registered mutable surface is byte-identical. A changed / added /
deleted out-of-bounds file is a mutation that escaped its sandbox
(`DiffKind::{Changed, Added, Deleted}` in
`crates/supervisor/src/diff_containment.rs`). v1 is the coarse file-level
check; fail-open-to-alarm — an unreadable snapshot or missing parent yields
*no violation* ("the attestation cannot be made"), never a false quarantine.

**The Python blocking-mode mirror.** The supervisor's scan is alarm-only by
design; the in-band twin on the exact same rule surface is
`zicato.evolve.containment.check_containment`, consulted by the orchestrator
immediately before finalizing a promotion when the contract opts into
`ScoringWeights.block_on_containment_violation`:

```python
The Rust supervisor's integrity notary re-hashes child snapshots
OUT-OF-BAND and alarms when a mutation escaped the registered mutable
surface (``crates/supervisor/src/diff_containment.rs`` — alarm-only by
design). This module is the IN-BAND twin on the exact same rule surface,
consulted by the orchestrator immediately before finalizing a promotion
when the contract opts into
:attr:`~zicato.core.scoring_config.ScoringWeights.block_on_containment_violation`:
```
— `src/zicato/evolve/containment.py` (module docstring)

The two are kept in lockstep — mutable-tree basenames as the
in-bounds surface, empty `mutable_trees` ⇒ the whole snapshot is in-bounds,
coarse file granularity, fail-open skips. If you change the rule on either
side, change both in the same commit and say so in both docstrings; a skew
here means the blocking gate and the alarm disagree about what "escaped"
means. (A sibling knob, `block_on_gate_contradiction`, does the same in-band
promotion of record #3 below.)

### 8.8.2 Promotion gatekeeping (`--promotion-gate`, record #3)

Each tick re-applies the gate's scalar rule to every recorded promotion in
the current epoch: the scalar is a LOSS, and a promotion needs
`delta_scalar = child_scalar − parent_scalar ≤ −promote_margin` (the margin
read from the epoch's `scoring.json`, defaulting to
`DEFAULT_PROMOTE_MARGIN = 0.01`, which must track
`ScoringWeights.promote_margin`'s default in
`src/zicato/core/scoring_config.py`). `check_row` returns a `RowVerdict`:
`NotAPromotion`, `SkippedNoEvidence` (no usable scalars — never a false
alarm), `Supported`, or a `Contradiction` carrying a direction-precise
detail string.

The alarm is deliberately one-directional: a promotion is gated by ALL of
the gate's rules (scalar AND pass-rate AND namespace monotonicity), so a
promotion whose scalar rule alone fails is *definitively* unsupported — a
hard contradiction. The reject direction is NOT alarmed: a reject can be
driven by the other rules even when the scalar margin cleared, so a
scalar-only recompute cannot prove a reject wrong.

### 8.8.3 The divergence auditor (`--divergence-audit`, record #4)

One tick joins the canonical, file-derived view (`build_lineage_view` +
`build_epoch_view`) against the SQLite index — the two should always agree
because the index is a projection of the files (07-runtime-and-durability.md
§"The persistence doctrine"). Flags, per the module docstring: (a)
per-generation `promoted` / `parent_generation_id` divergence, (b) epoch
`contract_hash` divergence plus any non-empty hash that is not 64-hex, and
(c) an in-flight generation whose worker pid is dead and whose decision
never resolved past `--divergence-stuck-age-seconds` (default 3600).
Unresolved in-flight generations are SKIPPED for join (a) — mid-tournament
the canonical decision is legitimately `None` and the index may lag, so
comparing would be a false positive; check (c) catches the stuck ones. A missing index degrades to "nothing to cross-check".

---

## 8.9 Guarantee: the read-only SQLite discipline

`crates/supervisor/src/index_db.rs` is the only module that touches
`index.db`, and it enforces the read-only version-pinned index rule at three
layers:

**Layer 1 — read-only connections.** Every open uses
`SQLITE_OPEN_READ_ONLY` (there is a unit test, `open_is_read_only`, proving a
write through the handle fails). The index is Python-built (`zicato
reindex` + live dual-writes); the supervisor never writes a byte of it.

**Layer 2 — the pinned schema tripwire.**

```rust
/// The SQLite `user_version` this supervisor's positional row readers are
/// written against. Pinned to the Python `SCHEMA_VERSION`
/// (`src/zicato/index/schema.py`); a test in this module fails loudly if
/// the two drift, so a Python schema bump cannot silently leave the Rust
/// reader decoding rows by stale column positions.
```
— `crates/supervisor/src/index_db.rs`

A database whose `user_version` ≠ `EXPECTED_SCHEMA_VERSION` (currently `14`)
returns `IndexError::StaleSchema` instead of risking rows decoded against
the wrong schema generation. The cross-language pin has teeth on both sides:
a cargo test asserts the constant equals the Python value, and a Python-side
bump without the Rust bump reds `cargo test` — this is the canonical example
of "when a Python change requires Rust parity" (§8.12).

**Layer 3 — audit-only consumption.** Index rows feed the divergence and
promotion-gate alarms. They do not feed dashboard decisions. A missing,
stale, or malformed index makes those checks fail open with no attestation;
the operational HTTP surface remains file-backed.

**The API/static subset it serves.** The router (`routes.rs::router`)
mounts two tiers. Unconditionally — the watchdog's own surface: `/statusz`,
`/statusz.json`, `/api/audit/verify`. Under `--no-dashboard` that is ALL you
get. Otherwise the full dashboard surface: the embedded static UI (`/`,
`/static/*`, fallback asset resolution), the state APIs (`/api/state`,
`/api/epoch`, `/api/lineage`, `/api/run-log`, `/api/active-runs`,
`/api/active-tournament`, `/api/heartbeat`, `/api/health`), the SSE stream
(`/events`, fed by the filesystem watcher), and the control POSTs (§8.11).

**What degrades on the index being absent/stale.** File-backed endpoints
(heartbeat, active runs, active tournament, run log) keep working. Integrity
audits report no claim when their index side cannot be read. Analytical
endpoints are not mounted here; add them to `zicato.query` rather than to the
supervisor.

> ⛔ NEVER return a 500 from a supervisor GET because a workspace file or
> the index is missing. The supervisor's contract is to run against a
> workspace that has never booted an orchestrator; every reader treats
> absence as a valid state. (400 for a malformed id and 403 for read-only
> POSTs are the deliberate exceptions.)

---

## 8.10 The kill-request single-escalator handshake

The sole-worker-signaller rule has two halves, each in its own language, each
pointing at the other:

**Python writes markers.** When the parent decides a worker must die (its
own budget logic, an operator's dashboard kill), it writes
`control/kill_requests/{run_id}` via
`zicato.runtime.state.request_worker_kill` — and never signals the pid:

```python
    Writes a ``control/kill_requests/{run_id}`` marker. The Rust
    supervisor — the single SIGTERM→grace→SIGKILL escalator — reads it,
    runs the escalation on the worker's pid, and clears the marker. The
    Python parent therefore never signals the worker itself, so there is
    no parent↔supervisor race over the same pid.
```
— `src/zicato/runtime/state.py`, `request_worker_kill`

**The supervisor is the sole signaller.** In `runs_loop`, the kill-request
trigger is "Trigger 0" — highest priority among the per-run triggers (the
parent already decided; no deadline/staleness condition applies).
`decide_run_kill_request` applies the standard vetting (signalable and alive;
identity via the escalation path); `resolve_kill_target` upgrades to
a group kill when safe; `escalate_target` runs SIGTERM → grace → SIGKILL.
The marker is cleared afterwards — and also when there is nothing safe to
signal (absent/unsafe/dead pid), so a request is never retried forever:

```rust
                    if kill_requests.contains(&run.run_id) {
                        match decide_run_kill_request(run, &protected) {
                            None => {
                                // No signalable pid (absent / unsafe / dead):
                                // nothing to escalate, but the request is
                                // satisfied — clear it so it isn't retried.
                                reader::clear_kill_request(&paths, &run.run_id);
                            }
                            Some(pid) => {
                                let target =
                                    resolve_kill_target(run, pid, &protected_pgids);
```
— `crates/supervisor/src/watchdog.rs`, `runs_loop`

The Python side also clears its own marker on run cleanup
(`clear_worker_kill_request`) so a marker never outlives its run — a
recycled run id must not inherit a stale request.

**Why a handshake at all:** two escalators racing the same pid can double-
signal, signal a recycled pid the other side already reaped, or interleave
SIGTERM/SIGKILL windows unpredictably. One writer of intent, one owner of
signals — the dashboard's `POST /api/control/kill/:run_id` writes the same
marker shape, so operator kills flow through the identical vetted path.

---

## 8.11 Build, packaging, and binary resolution

**Building.** `make supervisor` → `cargo build --release -p
zicato-supervisor`. The full local gate is `make supervisor-check` →
`cargo fmt --check && cargo clippy --all-targets -- -D warnings && cargo
test`, which is what CI's Rust job runs.

**Packaging.** A hatchling build hook (`hatch_build.py`, wired via
`[tool.hatch.build.targets.wheel.hooks.custom]` in `pyproject.toml`)
compiles the crate at wheel-build time and stages the binary at
`src/zicato/_bin/zicato-supervisor` so it ships inside the wheel.
Best-effort by design: no `cargo` on PATH, or an sdist without `crates/`,
logs a warning and ships a wheel without the binary — the CLI still
resolves one (below) or degrades to running without a supervisor. The sdist
carries the crate source (`crates/`, `Cargo.toml`, `Cargo.lock`) so a
downstream wheel build can run the hook.

**Resolution order** — `_resolve_supervisor_binary` in
`src/zicato/cli/commands/evolve.py`:

1. `IntegrationConfig.supervisor_binary` — the `--supervisor-binary` flag
   pinned at command startup (tests point this at sentinel scripts);
2. the fresher of the two checkout/install candidates: the bundled
   `zicato/_bin/zicato-supervisor` vs the dev checkout's
   `<repo>/target/release/zicato-supervisor` — in a dev checkout BOTH can
   exist and the bundled copy goes stale the moment you rebuild, so the
   release build wins when it is at least as new by mtime;
3. the system `PATH`;
4. the dev-checkout release build as a last resort (a bare checkout that
   never built a wheel has no `_bin/`).

`None` resolves to a warning and an evolve without a supervisor —
supervision is protective, never load-bearing for the loop itself.

> ⚠️ TRAP: after editing Rust code, `zicato evolve` in a dev checkout picks
> up your change only once `cargo build --release` has run — the mtime rule
> in step 2 is what makes the fresh build win over the stale bundled
> binary. If behaviour looks unchanged after a Rust edit, you are almost
> certainly running the stale copy: rebuild, or pass `--supervisor-binary
> target/release/zicato-supervisor` explicitly.

`zicato evolve` spawns the resolved binary with `--no-dashboard` (the
watchdog + `/statusz` only — the UI is the separate Python dashboard
service, whose port walk range 7892–7902 is deliberately disjoint from the
supervisor's 7920–7930; see the `--port` doc in `main.rs`).

---

## 8.12 The Rust dev workflow

**The gates.** Every supervisor change must pass, locally, what CI runs:

```bash
cargo fmt --check
cargo clippy --all-targets -- -D warnings
cargo test -p zicato-supervisor
```

(`make supervisor-check` bundles them.) Clippy warnings are errors; do not
`#[allow]` your way past one without a comment explaining why the lint is
wrong here.

**Test layout conventions.**

- *Pure-decision unit tests* live in a `#[cfg(test)] mod tests` beside the
  function — `watchdog.rs`, `reap.rs`, `signal.rs`, `ledger.rs`,
  `promotion_gate.rs`, `index_db.rs` all follow this. New guarantees get
  the same treatment: extract the decision, unit-test the matrix
  (`no_heartbeat_is_not_dead`, `refuses_a_path_outside_the_temp_dir` are
  the tone to match — one named property per test).
- *Route/end-to-end tests* live in `crates/supervisor/tests/integration_test.rs`
  (~2,500 lines): build a synthetic workspace with `make_workspace()`
  (tempdir + `runtime/active_runs`, `runtime/control`, `epochs/`),
  construct `ServeOptions` via the `serve_opts(read_only)` helper, bind an
  EPHEMERAL port (`port 0`) through `server::serve`, then drive real HTTP
  against `handle.addr`. Because `lib.rs` exposes every module, the tests
  exercise the same code paths as the binary without spawning it.
- *Index-backed tests* create a scratch SQLite file and stamp
  `PRAGMA user_version = EXPECTED_SCHEMA_VERSION` — see the fixtures inside
  `index_db.rs`'s test module; a schema-shape test there is also the drift
  tripwire against the Python `SCHEMA_VERSION`.

**When a Python payload change requires Rust parity.** The supervisor
mirrors Python-written state through `#[serde(default)]` structs, so purely
*additive* Python fields are free — an older supervisor ignores them
(07-runtime-and-durability.md §"Recipe: add a runtime state field"). Parity
work is required when:

| Change class | Rust work required |
|---|---|
| Additive field the supervisor should ignore | none (serde ignores unknown keys; defaults cover absence) |
| Additive field the supervisor must surface | mirror field in `state.rs` + wire into the route/statusz view + a deserialization test |
| Semantic/shape change to a served payload | change the Python service AND the Rust route in the same commit — the heartbeat-ts lesson: when the dashboard schema clean-break made `ts` THE one typed liveness timestamp (integer ms, stamped server-side from `last_heartbeat`), both the Python reader and the Rust supervisor's heartbeat route had to move atomically, or the two dashboards would disagree about liveness (see 12-bug-casebook.md) |
| Index schema change | bump Python `SCHEMA_VERSION` AND Rust `EXPECTED_SCHEMA_VERSION` together, update the row readers, re-capture the REINDEX-DUMP golden (11-testing.md §11.7) |
| Control-file shape change | update the Rust marker writers in `routes.rs` AND the Python consumer in the same commit (§8.13's checklist) |

> ✅ ALWAYS grep BOTH languages when you touch a shared name. The shared
> vocabulary is small and explicit: `ztw-snap-` / `SNAPSHOT_PREFIX`,
> `SCHEMA_VERSION` / `EXPECTED_SCHEMA_VERSION`, `promote_margin` /
> `DEFAULT_PROMOTE_MARGIN`, the runtime file field names in `state.py` /
> `state.rs`, the control-file names (`pause_epoch`, `skip_round`,
> `kill_runs/`, `promote/`, `reject/`), and the start-time token semantics
> in `lock.py` / `signal.rs`. Each pair carries a comment pointing at its
> twin — keep the comments true.

---

## 8.13 Recipe: add a control route

Scenario: a new operator gesture — say "rotate the board now" — needs a
dashboard button. Control gestures are files (07-runtime-and-durability.md
§"The control protocol"); the supervisor's role is to expose a POST that
writes the marker. Six steps, two languages, tests on both sides.

**Step 1 — Python endpoint.** Add the marker constant + write path in
`zicato.runtime.control` (a flag file `control/rotate_board`, or a targeted
`control/rotate_board/<arg>` — copy the `CMD_SKIP_ROUND` /
`CMD_KILL_RUN_PREFIX` patterns, including `write_command`'s shape
dispatch), and the matching POST on the Python dashboard service
(`src/zicato/dashboard/endpoints.py`) so both UIs offer the gesture.

**Step 2 — Rust parity route.** In `crates/supervisor/src/routes.rs`:
register the route in `router()` inside the `!state.dashboard_disabled`
block, and write the handler on the established skeleton — read-only guard
first, id validation for targeted commands, atomic write, `202 ACCEPTED`
with the payload echoed:

```rust
async fn control_skip_round(State(s): State<AppState>, body: Option<Json<EmptyBody>>) -> Response {
    if let Some(r) = forbidden_if_read_only(&s) {
        return r;
    }
    let reason = body.and_then(|Json(b)| b.reason).unwrap_or_default();
    write_control_marker(
        &s,
        "skip_round",
        serde_json::json!({"reason": reason, "ts": chrono::Utc::now()}),
    )
    .await
}
```
— `crates/supervisor/src/routes.rs` (the pattern to copy)

Notes on the skeleton: `write_control_marker` / `atomic_write` already
implement tmp-write + rename (the Rust twin of the Python atomic contract) —
use them, never a bare `tokio::fs::write`. Targeted routes MUST validate the
path parameter with `is_safe_id` and answer `400` on failure (see
`control_kill` / `control_promote`) — the id becomes a filename.

**Step 3 — read-only 403.** The `forbidden_if_read_only` guard is
non-negotiable on every POST: a `--read-only` supervisor is the "attach an
observability supervisor to a run it should not police" mode, and a control
write from it would violate that promise. There is an existing integration
test asserting POSTs return 403 under `read_only: true` — extend it to your
route.

**Step 4 — consumer.** A command nobody consumes is the "dead
producer-consumer" RUNTIME-V2 names. Wire the evolve-loop semantics in
`zicato.runtime.control_consumer` at the correct safe point (between rounds
/ top of `evolve_once` / at the gate — see the safe-point table in
07-runtime-and-durability.md §"The control protocol"), consuming via
`consume_command` with `source=CONSUMER_SOURCE` and a real `reason` so the
audit trail stays complete. Decide the staleness story explicitly: does the
command survive an epoch roll (pause does) or must it be drained
(promote/reject are)?

**Step 5 — tests, both sides.**

- Rust: an integration test in `crates/supervisor/tests/integration_test.rs`
  — POST against an ephemeral-port server, assert `202` and that the marker
  file exists with the expected JSON body; a second assertion under
  `read_only: true` expecting `403`; for a targeted route, a malformed-id
  `400`.
- Python: a consumer test in `tests/` — write the command via
  `write_command`, drive the safe-point function, assert the effect AND the
  `control_log/` audit record (name, arg, source, reason).

**Step 6 — frontend + docs.** If the dashboard UI grows a button, the node
suite needs the behaviour test (11-testing.md §11.9);
`--help` text changes ripple into the CLI-HELP parity golden only if you
added a CLI verb.

**Verify**

```bash
cargo fmt --check && cargo clippy --all-targets -- -D warnings
cargo test -p zicato-supervisor            # includes the new route tests
uv run pytest tests/ -q -k "control"       # consumer + audit-log coverage
uv run pytest tests/ -q                    # nothing else regressed
```

---

## 8.14 Anatomy of the two loops

§8.2 named the two loops; this is how they actually run. `main.rs` wires them
identically: build `WorkspacePaths` from the workspace root, build `Thresholds`
from the CLI flags, spawn the filesystem watcher (100ms debounce), construct the
shared trackers, open the ledger (if `--ledger-dir` was given) and stamp a
`SupervisorStart` record, then `tokio::spawn` each loop on the shared `--interval`
tick. Both loops select on a broadcast shutdown channel so a clean exit stops
them together.

```rust
    let seq_liveness = Arc::new(std::sync::Mutex::new(watchdog::SeqLiveness::new()));
    ...
    let fold_diagnostics = Arc::new(zicato_supervisor::fold_stats::FoldDiagnostics::new());
    ...
    let led = Arc::new(zicato_supervisor::ledger::AuditLedger::open(dir));
    ... led.append(zicato_supervisor::ledger::RecordKind::SupervisorStart, ...)
    ...
    let interval = Duration::from_secs(cli.interval.max(1));
    tokio::spawn(async move {
        watchdog::heartbeat_loop(hb_paths, thresholds, interval, hb_log, hb_seq, hb_shutdown).await
    });
    tokio::spawn(async move { watchdog::runs_loop(...).await });
```
— `crates/supervisor/src/main.rs` (abridged)

The `SeqLiveness` tracker is a *shared* `Arc<Mutex>`: `heartbeat_loop` advances it
(the only writer) and `/statusz` reads it without advancing, so both agree on the
last seq-change age, under the two-loops fixed-trigger-priority rule. This is
the structural half of the seq-first liveness
of §8.3.

### 8.14.1 `heartbeat_loop` — observe, classify, warn (never kill)

```rust
                let hb = reader::read_heartbeat(&paths);
                let obs = match seq_liveness.lock() {
                    Ok(mut tracker) => tracker.observe(hb.as_ref(), Utc::now(), &thresholds),
                    Err(_) => continue,
                };
                match obs.action {
                    HeartbeatAction::Nothing => {}
                    HeartbeatAction::Warn => { warn!(?hb, ..., "heartbeat is stale (warn threshold)"); }
                    HeartbeatAction::Stale => {
                        // Deep-stale past the former kill threshold. We do NOT
                        // signal the orchestrator: restart is an operator /
                        // process-supervisor decision. Surface it loudly and move on.
                        warn!(?hb, ..., "orchestrator heartbeat is deeply stale; NOT killing it ...");
                    }
                    HeartbeatAction::MissingHeartbeat => { tracing::debug!("no heartbeat file present"); }
                }
```
— `crates/supervisor/src/watchdog.rs`, `heartbeat_loop` (abridged)

There is no fourth arm. `decide_heartbeat` cannot produce a `Kill` (§8.3), so the
loop that consumes it structurally cannot signal the orchestrator pid. That is
the never-kill-the-orchestrator rule enforced by exhaustiveness — a reviewer
adding a kill would have to
add a `HeartbeatAction` variant first, which is the diff §8.3's `⛔ NEVER`
tells you to reject.

### 8.14.2 `runs_loop` — the fixed trigger priority

Each tick, `runs_loop` first runs the enabled integrity scans (ledger transition
observation, diff-containment, promotion-gate, divergence — §8.7/§8.8), computes
the protected pid set (the heartbeat pid) and protected pgid set (its own +
the orchestrator's groups), reads the pending kill-requests, reads the active
runs, and then applies FOUR triggers in strict priority order:

| Priority | Trigger | Fires when | State file |
|---|---|---|---|
| −1 (highest) | **confirmed orchestrator death** | `decide_orchestrator_dead` (pid identity fails; a stale timestamp does not count) AND runs exist | REMOVED (no orchestrator left to finalize) |
| 0 | **kill-request** | the parent wrote `control/kill_requests/{run_id}` | LEFT for the orchestrator reaper; the marker is cleared |
| 1 | **wall-clock deadline** | `decide_run_deadline` past the clamped `effective_deadline` | LEFT for the orchestrator reaper |
| 2 | **staleness** | `decide_run` — `last_progress` not advancing past the run's own `2×budget` | REMOVED (so it is not re-escalated) |

The ordering is not cosmetic. Trigger −1 `continue`s the whole tick (the
orchestrator is gone; the per-run triggers are moot), and Trigger 0 `continue`s
the per-run loop iteration (the parent already decided; do not also apply the
deadline/staleness thresholds). The confirmed-death reaper does three things per
run — the same vetted escalation the live triggers use, then the snapshot GC,
then the state-file finalization the dead orchestrator cannot do:

```rust
        // 1. Group-kill the orphaned worker, when there is a live, vetted pid.
        if let Some(pid) = decide_run_kill_request(run, &protected) {
            let target = resolve_kill_target(run, pid, protected_pgids);
            ...
            let out = escalate_target(target, thresholds.run_kill_grace).await;
            record_action(log, ledger, Action { ..., trigger: Trigger::OrchestratorReap, ... });
        }
        // 2. GC the leaked ztw-snap-* ephemeral snapshot (prefix-guarded).
        reap::reap_orphaned_snapshot(run);
        // 3. Finalize the state file the dead orchestrator's reaper can no longer remove.
        let run_file = paths.active_runs_dir().join(format!("{}.json", run.run_id));
        ... std::fs::remove_file(&run_file) ...
```
— `crates/supervisor/src/watchdog.rs`, `reap_dead_orchestrator_runs` (abridged)

> ⚠️ TRAP — the "leave the state file vs remove it" distinction is load-bearing
> and easy to get backwards. The alive-orchestrator triggers (kill-request,
> deadline) LEAVE `active_runs/{run_id}.json` because the orchestrator's own
> reaper owns that lifecycle (it detects the dead worker, cleans up, and records
> the run aborted — 06-tournament-and-selection.md §"The abort-cause decision
> tree"). The staleness and confirmed-death paths REMOVE it, because leaving it
> would make the supervisor re-escalate the same run every tick (staleness) or
> leave a phantom active run behind a dead orchestrator (death). If you add a
> trigger, decide its state-file ownership explicitly.

### 8.14.3 The two graces, and `record_action` mirroring

There are two distinct grace windows, and mixing them up changes kill timing:
the **deadline / kill-request / reap** escalations use `thresholds.run_kill_grace`
(the `--run-kill-grace` flag), while the **staleness** escalation uses the fixed
`thresholds.grace` (5s in `main.rs`). Every escalation — from any trigger —
funnels through `record_action`, which mirrors it into BOTH the in-memory action
ring (`action_log`, surfaced on `/statusz`) AND, when a ledger is configured, the
hash-chained ledger (`Trigger::{KillRequest, RunDeadline, RunStale,
OrchestratorReap}` → a `WatchdogAction` record). That dual sink is why an
escalation is visible both in the live operational surface and in the
tamper-evident audit trail (§8.7, §8.19).

### 8.14.4 The integrity-scan de-dup sets

`runs_loop` carries three `HashSet`s across ticks so a STANDING violation alarms
ONCE rather than every tick: `quarantined` (diff-containment, keyed by `(epoch, gen)`),
`gate_flagged` (promotion-gate, same key), and `divergence_seen` (keyed by
`(code, gen)`), plus the stateful `TransitionObserver` (§8.17). These are the
ONLY state the loop keeps across ticks besides the ledger tail — everything else
is re-read fresh each tick, under the no-cached-state-across-ticks rule. A new
integrity scan you add must carry its own
de-dup set the same way, or it will re-alarm on a steady-state condition forever.

---

## 8.15 The reader and the path map

`crates/supervisor/src/reader.rs` is the supervisor's entire input surface: it
turns the runtime state files into typed snapshots, read FRESH each tick.
Its `WorkspacePaths` struct is the Rust twin of `zicato.runtime.paths` — the same
path map, in the other language:

- `heartbeat()`, `lock()`, `active_runs_dir()`, `active_tournament()` +
  `active_tournament_log()`, `lineage()`, `control_dir()`, `current_epoch_marker()`,
  and the `runtime` / `epochs` / `workspace` roots. `watcher.rs::classify` and
  every reader resolve against these, so the Python and Rust sides agree on
  *where every file lives* by construction.
- `read_heartbeat` / `read_active_runs` / `read_kill_requests` /
  `read_active_tournament` (folding the event log — §8.18) / `build_snapshot`
  (the composite `/api/state` shape). Each is best-effort: a missing/malformed
  file degrades to `None`/empty, never a panic — the file-side twin of the
  read-only version-pinned index rule.

Because every Python writer is atomic (tmp→fsync→rename — 07-runtime-and-
durability.md §"The atomic-write contract"), the reader never needs a lock and
never observes a torn record — it reads the whole file and deserializes it in one
shot. That is the concrete meaning of out-of-band supervision (§8.1): the
supervisor shares
no memory, no lock, no socket with the orchestrator; the atomic file rename IS
the synchronization.

> ✅ ALWAYS add a new runtime file's path to BOTH `zicato.runtime.paths` and
> `WorkspacePaths`, and add its `ChangeKind` to `watcher.rs::classify`, in the
> same commit. A path that exists on only one side is a file the supervisor
> either cannot find or cannot notify on — the silent-skew class §8.12's
> `grep BOTH languages` rule exists to catch.

---

## 8.16 The signal layer in depth

`crates/supervisor/src/signal.rs` is where every kill actually happens. It is
pure POSIX (`nix`), best-effort (a vanished pid is never an error), and every
primitive is unit-tested without spawning where possible. Four pieces underlie
the pid-safety guarantees of §8.4.

### 8.16.1 `pid_start_time` and `pgid_of` — reading `/proc`

The identity token is the process start time from `/proc/<pid>/stat` field 22,
and the parsing has a real trap the code calls out:

```rust
    let raw = std::fs::read_to_string(format!("/proc/{pid}/stat")).ok()?;
    // Field 2 (comm) is wrapped in parens and may itself contain ')' and
    // spaces, so tokenize everything after the LAST ')'.
    let rparen = raw.rfind(')')?;
    let rest: Vec<&str> = raw[rparen + 1..].split_whitespace().collect();
    // rest[0] is field 3 (state); field 22 (starttime) is rest[19].
    rest.get(19)
        .and_then(|s| s.parse::<u64>().ok().map(|t| t as f64))
```
— `crates/supervisor/src/signal.rs`, `pid_start_time`

The `rfind(')')` is load-bearing: the `comm` field (field 2) is
parenthesized and can itself contain `)` and spaces (a process named `foo) bar`),
so tokenizing from the start would misalign every field. `pgid_of` reads field 5
(`pgrp`) the same way, to fence the orchestrator's group out of the negatable
set. The token is carried as `f64` to compare exactly against the Python writer's
JSON float (`116371304.0`) — integer-valued, so equality is exact (the
`state.rs` field comment and 07-runtime-and-durability.md §7.6.2 both
pin this).

`is_same_process(pid, expected_start_time)` is the composed check: not alive →
`false`; alive with no recorded time → fall back to bare liveness (`true`); alive
with an unreadable current time → `true` (cannot *disprove* identity, so do not
manufacture a mismatch); alive with both known → `true` iff equal. This
conservative bias is deliberate — the supervisor would rather fail to reap than
kill an innocent recycled-pid process.

### 8.16.2 `escalate_target` — the SIGTERM→grace→SIGKILL state machine

```rust
pub async fn escalate_target(target: KillTarget, grace: Duration) -> EscalationOutcome {
    let leader = target.leader_pid();
    if !is_alive(leader) {
        return EscalationOutcome::AlreadyGone;
    }
    if let Err(e) = target.send_sigterm() { ...; return EscalationOutcome::Failed; }
    let poll_interval = Duration::from_millis(100);
    let mut elapsed = Duration::ZERO;
    while elapsed < grace {
        tokio::time::sleep(poll_interval).await;
        elapsed += poll_interval;
        if !is_alive(leader) { return EscalationOutcome::TerminatedGracefully; }
    }
    if !is_alive(leader) { return EscalationOutcome::TerminatedGracefully; }
    match target.send_sigkill() { Ok(_) => EscalationOutcome::KilledForcefully, Err(_) => ... }
}
```
— `crates/supervisor/src/signal.rs`, `escalate_target` (abridged)

The four `EscalationOutcome` values (`AlreadyGone`, `TerminatedGracefully`,
`KilledForcefully`, `Failed`) are what `record_action` stamps into the ledger and
the ring. The whole machine tracks the group LEADER pid for liveness even in the
group case: `KillTarget::Group { pgid, leader_pid }` sends `killpg(-pgid, …)` to
the whole group but polls `leader_pid` for exit — "the group is considered gone
once its leader exits", exactly like a single-pid kill.

### 8.16.3 The group-kill guards, again from the primitive side

`is_negatable_pgid` is the pure gate every group kill must pass (`pgid > 1`, not
in the protected set) — §8.4's guard, here at the metal:

```rust
pub fn is_negatable_pgid(pgid: i32, protected: &std::collections::HashSet<i32>) -> bool {
    if pgid <= 1 {
        return false;
    }
    !protected.contains(&pgid)
}
```
— `crates/supervisor/src/signal.rs`, `is_negatable_pgid`

`resolve_kill_target` (in `watchdog.rs`) composes it with the pgid-equals-leader
check (§8.4) and falls back to `KillTarget::Leader` — the always-safe single-pid
kill — on any failure. `pgid <= 1` is the catastrophic case the guard closes:
`killpg(0, …)` means "my own group" and `killpg(1, …)` is init's; either would
signal the supervisor or the whole system.

> ⛔ NEVER call `send_sigterm_group` / `send_sigkill_group` (or construct a
> `KillTarget::Group`) without going through `resolve_kill_target`. The group
> primitives explicitly "do not re-check" the vetting — they trust the caller
> vetted the pgid through `is_negatable_pgid` AND confirmed the leader is alive
> and identity-matched. A raw group send is how you turn a stray record into a
> system-wide SIGKILL.

---

## 8.17 The ledger internals

§8.7 gave the ledger's guarantees; this is the code behind them. Three functions
carry the weight.

### 8.17.1 `compute_digest` — the chained preimage

```rust
    let payload_bytes = serde_json::to_vec(payload).unwrap_or_default();
    let mut preimage = Vec::with_capacity(payload_bytes.len() + 96);
    preimage.extend_from_slice(&seq.to_be_bytes());
    preimage.push(0x1f); // unit separator between fields
    preimage.extend_from_slice(prev.as_bytes());
    preimage.push(0x1f);
    preimage.extend_from_slice(ts.as_bytes());
    preimage.push(0x1f);
    preimage.extend_from_slice(kind.as_str().as_bytes());
    preimage.push(0x1f);
    preimage.extend_from_slice(&payload_bytes);
    sha256::hex_digest(&preimage)
```
— `crates/supervisor/src/ledger.rs`, `compute_digest`

The `0x1f` unit separators between fields are what stop a boundary attack (moving
bytes between `ts` and `kind` cannot produce the same preimage), and the digest
covers `kind.as_str()`, the raw string rather than an enum discriminant, which is why
a new `RecordKind` is purely additive: an older verifier still hash-checks a
record whose kind it does not recognize (§8.7's closed-enum-but-additive
property). `prev` binds each record to its predecessor's digest; genesis links to
`GENESIS_PREV` (64 zeros).

### 8.17.2 `repair_torn_tail` — surgical, byte-level, trailing-only

```rust
    An unparseable line in the *middle* of the file cannot be a torn
    append and is left in place for `verify_chain` to flag as a break.
    Returns the number of bytes truncated, or `None` when the file is
    absent, empty, or ends in a complete record. Best-effort: an I/O
    failure leaves the file untouched (the ledger never blocks boot).
```
— `crates/supervisor/src/ledger.rs`, `repair_torn_tail`

It operates on BYTES rather than on lines-as-strings, because a torn append can split a
multi-byte UTF-8 character (a string read would fail outright). It finds the last
non-empty line, trims trailing whitespace/newlines, and truncates iff that final
record does not parse — the ONE shape that is provably a torn append (the writer
emits exactly one `line + '\n'` per record and never rewrites earlier bytes). An
interior tear is left for `verify_chain` to flag: this is the same torn-tail
doctrine as `RoundLog` (07-runtime-and-durability.md §"Torn-tail tolerance is for
APPEND-ONLY logs only") — "only the tail can be torn" is a theorem rather than a general
error-handling posture.

### 8.17.3 `append` — best-effort tail, and `verify` reads fresh

`append` computes the digest, serializes the record, then `write_line` (which
`create(true).append(true)` opens, `write_all`, `flush`, `sync_all`). On ANY I/O
error the in-memory tail is NOT advanced, so the next append retries the same
`seq`/`prev` rather than chaining onto a record that never reached disk. `verify`
re-reads the file FRESH (never the in-memory tail), so out-of-band tampering
since the last append is caught — `verify_chain` checks three things per record:
the digest recomputes, `prev` links to the prior digest, and `seq` is contiguous
from 0. Any failure pins the break to a specific `seq`.

### 8.17.4 `TransitionObserver` — record each decision once

The observer (§8.7's "what feeds it") is stateful across ticks and de-duplicated:

```rust
pub struct TransitionObserver {
    /// Generations whose resolved decision has already been recorded, keyed
    /// by `(epoch_id, generation_id)`.
    recorded_decisions: std::collections::HashSet<(String, String)>,
    /// The last contract hash recorded, per epoch.
    recorded_contract: std::collections::HashMap<String, String>,
}
```
— `crates/supervisor/src/ledger.rs`, `TransitionObserver`

A generation's decision is recorded once (the first time it resolves from
in-flight), and a contract hash once per distinct value — "so a steady-state poll
appends nothing". This is what keeps the ledger a low-frequency artifact (a
handful of records per run), which in turn is what makes the fsync-per-append of
§8.7 affordable.

---

## 8.18 The live surface: the filesystem watcher and SSE

The supervisor's dashboard tier is fed by an inotify/FSEvents watcher and an SSE
broker, both built to satisfy the never-block-never-leak live surface rule.

### 8.18.1 The watcher — debounce + the `.tmp` filter

`watcher.rs::spawn` watches the `runtime` tree recursively, the `epochs` tree
recursively (when present), and the workspace root non-recursively (for
`current_epoch` / `lineage.json`). A dedicated thread drains raw `notify` events,
debounces per-file, and classifies each path into a `ChangeKind`. The single most
important filter is the atomic-write intermediate:

```rust
/// Whether `path` is an atomic-write intermediate (`*.tmp`). The Python
/// side writes state files as `path.tmp` then renames to `path`; the
/// `.tmp` create/modify/remove events are pure noise and must never
/// reach an SSE client.
fn is_tmp_path(path: &Path) -> bool { ... }
```
— `crates/supervisor/src/watcher.rs`, `is_tmp_path`

This is the watcher's side of the two-language atomic-write contract: because the
Python writer renames `path.tmp → path` (07-runtime-and-durability.md §"The
atomic-write contract"), the `.tmp` events are noise and the REAL event is the
rename into place. Emitting the `.tmp` intermediates would fan a write storm out
to every SSE client. The per-file debounce (100ms) further collapses a rename
storm. `classify` resolves each path against `WorkspacePaths` (§8.15) into
`ChangeKind::{Heartbeat, ActiveRuns, ActiveTournament, Lineage, Lock, Epoch,
Control, Unknown}` — the same tokens the SSE `state_change.kind` field carries.

### 8.18.2 The SSE broker — snapshot then live, bounded, non-blocking

```rust
    let live = BroadcastStream::new(rx)
        .filter_map(|res| async move { res.ok() })
        .map(|ev| { ... SseEvent::default().event("state_change").data(payload) });
    let merged = initial.chain(live);
    Sse::new(merged).keep_alive(KeepAlive::new().interval(Duration::from_secs(15)).text("ping"))
```
— `crates/supervisor/src/sse.rs`, `build_sse` (abridged)

A new client gets a `snapshot` event (a full `build_snapshot` — §8.15) followed
by live `state_change` events as files mutate, with a 15s keep-alive `ping`. The
channel is a bounded `tokio::sync::broadcast`: a slow client that cannot keep up
is DROPPED (`filter_map(res.ok())` discards its lag errors) rather than
back-pressuring the watcher or other clients. The `/events` route serves
this; it is part of the full-dashboard tier, so it is absent under
`--no-dashboard` (§8.9).

### 8.18.3 `fold_stats` — the torn-write / seq-gap counters

The active-tournament view is a single-writer append-only JSONL the supervisor
folds on every read. Two failure modes were historically invisible, and
`fold_stats.rs` makes them countable on `/statusz`:

```rust
//!   * **Torn writes** — a line that fails to parse as JSON. ... This is
//!     the Rust-drops-vs-Python-raises divergence: the Python reader raises
//!     on a bad line; the Rust reader swallows it. Counting it restores
//!     visibility without changing the lenient behavior.
//!   * **Non-monotonic `seq`** — ... A gap (or a backwards step) means the
//!     writer lost events or republished out of order.
```
— `crates/supervisor/src/fold_stats.rs` (module docstring)

`FoldDiagnostics` holds three `AtomicU64`s (`parse_failures`, `seq_gaps`,
`folds`), shared by `Arc` and read by `/statusz`. They are cumulative
*process-lifetime* counters — a restart starts them at zero, "the honest thing to
report". The `folds` counter (incremented on every fold, even a clean one) lets a
reader tell "0 folds, 0 problems" apart from "many folds, 0 problems". This is
the deliberate design point where the Rust reader stays LENIENT (it drops a torn
line, matching the fold's `filter_map`) while surfacing the divergence from the
Python reader's raise — visibility without a behavior change.

---

## 8.19 The operational surface: `/statusz`

`/statusz` (HTML) and `/statusz.json` are the watchdog's own minimal surface,
mounted UNCONDITIONALLY — even under `--no-dashboard` — because a watchdog's
health must be checkable in watchdog-only mode (§8.9). The `StatuszView` payload
(`statusz.rs`) carries:

| Field | Source | What it tells an operator |
|---|---|---|
| version / build / bound port / `uptime_seconds` | the supervisor process | the notary itself is alive and which build |
| `orchestrator_uptime_seconds` | `heartbeat.started_at` | how long the audited loop has run (`None` when no heartbeat) |
| `watchdog_actions` | the `action_log` ring (`WatchdogLog::snapshot`) | the recent SIGTERM/SIGKILL escalations, per trigger + outcome |
| `fold_diagnostics` | `FoldDiagnostics::view` (§8.18.3) | cumulative torn-write / seq-gap counts over the tournament fold |
| ledger chain status | `AuditLedger::verify` → `LedgerStatus` | `configured` + `intact` + `records`, and `first_break_seq` + `break_reason` on a break |

The `action_log` (`action_log.rs`) is an in-memory ring buffer of the most recent
escalations — the fast, always-available operational view that `record_action`
writes alongside the ledger (§8.14.3). The two are complementary: the ring is
process-lifetime and lossy (a bounded ring), the ledger is durable and
hash-chained (§8.7). `GET /api/audit/verify` runs a full fresh chain walk on
demand; like `/statusz`, it stays mounted even under `--no-dashboard`, so chain
integrity is checkable without the dashboard tier.

> ✅ ALWAYS surface a new supervisor-observed condition on BOTH `/statusz`
> (the live view) and — when it is an integrity finding — the ledger (the
> durable record). The `fold_diagnostics` counters are the model: a condition
> the fold silently swallowed became a visible cumulative counter without
> changing the lenient fold behavior. A finding that lands in only one place is
> either invisible to a live operator or absent from the audit trail.

---

## 8.20 The integrity-notary scans in code

§8.8 gave the three scans' contracts; this is the re-derivation code, because a
notary that re-derives a rule WRONG is worse than no notary. All three read the
index or the snapshots and never write.

### 8.20.1 `check_row` — re-applying the gate's scalar rule (record #3)

The promotion-gate notary re-derives the gate's scalar-margin rung
(06-tournament-and-selection.md §6.6.1) and returns a four-way verdict:

```rust
pub fn check_row(row: &TournamentRow, promote_margin: f64) -> RowVerdict {
    let decision = match &row.decision { Some(d) => d, None => return RowVerdict::NotAPromotion };
    if !is_promote_decision(decision) { return RowVerdict::NotAPromotion; }
    let delta = match row_delta_scalar(row) {
        Some(d) => d,
        None => return RowVerdict::SkippedNoEvidence,
    };
    // Supported iff the loss dropped by at least the margin.
    if delta <= -promote_margin {
        return RowVerdict::Supported;
    }
    ...
    RowVerdict::Contradiction(Contradiction { ... })
}
```
— `crates/supervisor/src/promotion_gate.rs`, `check_row` (abridged)

`RowVerdict::{NotAPromotion, SkippedNoEvidence, Supported, Contradiction}` maps
directly to the fail-open, direction-precise alarm of §8.8.2: a reject or unknown
decision is not checked; a row with no usable scalar evidence is SKIPPED (never a
false alarm); a promotion whose `delta_scalar <= -promote_margin` is Supported;
and ONLY a promotion the scalars do not support is a Contradiction. The
`promote_margin` is read from the epoch's `scoring.json`
(`promote_margin_for_epoch`), defaulting to `DEFAULT_PROMOTE_MARGIN = 0.01` —
which carries the same "must track `ScoringWeights.promote_margin`'s default"
comment the Python side does (§8.12's shared-vocabulary list). `is_promote_decision`
uses the SAME lenient decision mapping the lineage view uses, so the two never
disagree about what counts as a promotion.

> ⚠️ TRAP — the alarm is intentionally ONE-directional (promote-only). A reject
> can be driven by the pass-rate or namespace-monotonicity rules even when the
> scalar margin cleared, so a scalar-only recompute cannot prove a reject wrong.
> Do not "symmetrize" the notary to alarm on rejects — you would manufacture
> false alarms on every reject a non-scalar rule drove.

### 8.20.2 The read-only SQLite open, in code (record #3/#4's substrate)

Both the promotion-gate and divergence scans read through `index_db.rs`, which is
where the read-only version-pinned index rule's three layers live. The schema
pin, verbatim:

```rust
/// Opening a database whose `user_version` does not
/// match this constant returns [`IndexError::StaleSchema`] rather than
/// risking a row decoded against the wrong schema.
pub const EXPECTED_SCHEMA_VERSION: i64 = 14;
```
— `crates/supervisor/src/index_db.rs`

`IndexError` has exactly three variants — `Absent` (the file is genuinely not
there, so a route can attach a `note`), `Query` (there but a query failed), and
`StaleSchema { .. }` — and every scan pattern-matches all three into "nothing to
check, fail-open":

```rust
    let conn = match index_db::open(&paths.index_db()) {
        Ok(c) => c,
        Err(IndexError::Absent)
        | Err(IndexError::Query(_))
        | Err(IndexError::StaleSchema { .. }) => {
            return PromotionGateView {
                scanned: true,
                ..Default::default()
            };
        }
    };
```
— `crates/supervisor/src/promotion_gate.rs`, `scan_current_epoch`

This is the read-only version-pinned index rule and the null-degradation
contract (§8.9) meeting at the scan layer: a
stale or absent index degrades the notary to "scanned, no contradiction", never a
false alarm and never a crash. `EXPECTED_SCHEMA_VERSION = 14` is the cross-
language pin — bump it in lockstep with the Python `SCHEMA_VERSION` (07-runtime-
and-durability.md §"`zicato repair index`"), and a cargo test in this module reds if
they drift (§8.12's canonical "Python change requires Rust parity" example).

### 8.20.3 The diff-containment mutable surface (record #2)

The diff-containment scan (§8.8.1) re-derives the in-bounds surface from the
registered `mutable_trees`, and the derivation is the exact twin of the Python
in-band mirror's — a snapshot copies each mutable tree under its BASENAME, so
inside a snapshot root the mutable surface is `snapshot/<basename>`:

```rust
/// When `mutable_trees` is empty the surface is the WHOLE snapshot ...
/// every file is in-bounds ...
pub fn mutable_basenames(mutable_trees: &[String]) -> BTreeSet<String> { ... }
```
— `crates/supervisor/src/diff_containment.rs`, `mutable_basenames`

`is_in_bounds(rel, basenames)` is true iff `rel`'s first path component is a
mutable basename; an EMPTY `basenames` means the whole snapshot is in-bounds
(mirroring `mutable_subpaths`' fallback). A file OUTSIDE that surface that is not
byte-identical parent↔child is a `DiffKind::{Changed, Added, Deleted}`
`Violation` — a mutation that escaped its sandbox. Every failure mode is
fail-open-to-alarm: an unreadable file is skipped, a missing parent snapshot
yields *no* violation ("the attestation cannot be made"), never a false
quarantine. This is the same coarse file-level v1 rule the Python
`check_containment` blocking mirror enforces (§8.8.1) — the two MUST change
together, because a skew means the alarm and the blocking gate disagree about
what "escaped" means.

---

## 8.21 Cross-references

- 07-runtime-and-durability.md — every file this chapter's loops read; the
  `ztw-snap-` checkout contract; the control protocol's Python half; the
  seq-vs-timestamp liveness design.
- 09-dashboard-and-query.md — the separate Python dashboard service that
  serves the full UI; the supervisor's embedded UI is the minimal twin.
- 11-testing.md — `make supervisor-check` in the pre-commit checklist; the
  REINDEX-DUMP gate that pins the shared index schema; route-test patterns.
- 12-bug-casebook.md — the watchdog-kills-orchestrator finding that
  produced the never-kill-the-orchestrator rule; the heartbeat timestamp
  payload-parity lesson; the reaper incidents.
- `docs/design/RUNTIME.md` §3–4 and `docs/design/ROBUSTNESS.md` §2 — the
  design record for the watchdog's promises.
