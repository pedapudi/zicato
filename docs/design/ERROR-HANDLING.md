# Error handling — making best-effort failures visible

This document specifies how zicato handles the *never-abort-the-round*
class of failure: the dozens of side effects in the evolve loop that
must not abort the optimization, but whose silent failure can leave the
loop running **inert** with no signal that anything is wrong.

Six nested defenses in [ROBUSTNESS.md](ROBUSTNESS.md) keep the loop alive
through hangs and crashes: the per-call and per-budget timeouts,
structured cancellation, the subprocess worker boundary, the orchestrator
watchdog that the Rust supervisor runs, the consecutive-bad circuit
breaker, and atomic writes plus resume markers.
[LOOP-HEALTH.md](LOOP-HEALTH.md) detects when the loop runs cleanly but
produces *no optimization signal*. This
document covers a third, adjacent failure mode. The loop runs and even
optimizes, while a side effect — a dashboard publish, an index dual-write,
a report re-stamp, a target invocation — has been **systematically
failing**, swallowed by a blanket `except Exception`, with no surface
that reports it. The sections below specify how to make that failure
visible.

This document covers:

- The motivating incident: a target that ran inert for ~26 generations
  with nobody noticing (§1).
- The blanket-except idiom and where it lives (§2).
- The `best_effort` context manager and its failure counter — what is
  **already shipped** (§3).
- The remaining gap: the counter is tallied but surfaced nowhere (§4).
- Surfacing it in the loop-health CLI and `/api/health-report` (§5).
- Completing the migration of the remaining blanket blocks (§6).

> **Status.** The `best_effort` mechanism described in §3 is **shipped**
> and wraps 49 call sites, across the evolve loop modules, the tournament
> worker, epoch lifecycle, and the proposer's input capture. What remains
> — and is the actionable part of this document — is §4–§6: the counter
> is collected but **read by nothing**, so the degradation it would
> expose is still invisible; and 244 blanket blocks elsewhere in the tree
> have not been migrated.

## 1. The incident that motivated this

During a dogfood session a target driven through the agent development
kit adapter ran **inert** — every generation produced:

```
output_chars: 0
0/1 tasks completed
```

No judges fired. No drift was measured. The target was, for the loop's
purposes, dead: it returned nothing for the board to evaluate. This
continued for roughly **26 generations** before anyone noticed.

Every robustness layer was satisfied. The orchestrator never hung. Each
round completed and was journaled. The tournament ran the board against
both sides and the gate evaluated. Loop-health's detectors did not fire
loudly enough to stop the burn early. The loop reported itself healthy
and kept proposing, tournamenting, and journaling against a target that
could not have produced a useful result no matter what the proposer did.

The reason the failure was *invisible* rather than *loud* is the subject
of §2: the loop is studded with blanket `except Exception` blocks that
swallow everything to preserve the never-abort invariant. A broken
target invocation, a systematically failing dashboard write, a dead
index dual-write — each is caught, logged at `debug` (if at all), and
stepped over. Twenty-six generations of the same swallowed failure look
identical to zero failures: there is no counter, no rate, no surface
that says "this side effect has failed every round since v0."

The conclusion is the same one loop-health draws: **a loop that runs
cleanly is not thereby a loop that is working.** A swallowed failure that
recurs every round is a degradation. A silent degradation spends
wall-clock and model budget while producing nothing, and the only way it
surfaces today is an operator reading the journal.

## 2. The blanket-except idiom

The never-abort invariant is real and correct: re-stamping a report,
publishing a live status, dual-writing the index, closing a sink, or
tearing down a server is *strictly less important* than the optimization
the loop exists to do. A failure there must not abort the round. The
codebase expressed that with a hand-rolled idiom, repeated across the
tree:

```python
try:
    do_the_side_effect()
except Exception as exc:  # noqa: BLE001 — ... is best-effort
    log.debug("... skipped: %s", exc)
```

The swallow is broad on purpose — the side effect can fail in many ways
and none of them should reach the round. The `# noqa: BLE001` silences
ruff's blind-except lint, which is otherwise correct to flag this.

There are **244** such sites across `src/`, counted as lines carrying the
`# noqa: BLE001` marker. The files holding the most are:

| count | file |
|------:|------|
| 12 | `tournament/runner.py` |
| 12 | `query/eval_view.py` |
| 11 | `telemetry/meta_loop.py` |
| 11 | `reflection/mining.py` |
| 10 | `telemetry/harmonograf_supervisor.py` |
|  9 | `query/judge_view.py` |
|  9 | `evolve/round_context.py` |
|  8 | `reflection/admission.py` |
|  8 | `evolve/dashboard_projection.py` |
|  7 | `proposer/best_of_n.py` |

The cost is uniform across all of them: **a failure here is invisible.**
A systematically broken write or a dead target leaves no signal that
distinguishes "failed every round" from "never failed."

## 3. The `best_effort` context manager (shipped)

`zicato.util.best_effort` collapses the idiom in §2 into one place. The
swallow is *identical* — the same broad `Exception` is caught, the same
fall-through control flow results, and `BaseException` subclasses
(`KeyboardInterrupt`, `SystemExit`, `asyncio.CancelledError`) still
propagate — so wrapping a block is **behavior-preserving**. What it adds
is **observability**: every swallowed failure increments a process-local,
per-`label` counter.

```python
from contextlib import contextmanager
from collections import Counter

_FAILURES: Counter[str] = Counter()

@contextmanager
def best_effort(
    label: str,
    *,
    on_error: Callable[[BaseException], None] | None = None,
) -> Iterator[None]:
    try:
        yield
    except Exception as exc:  # noqa: BLE001 — the whole point: a never-abort swallow
        _FAILURES[label] += 1
        if on_error is not None:
            on_error(exc)
        else:
            log.debug("%s skipped: %s", label, exc)


def best_effort_failures() -> dict[str, int]:
    """Snapshot {label: count} of swallowed best-effort failures."""
    return dict(_FAILURES)


def reset_best_effort_failures() -> None:
    """Clear the tally (e.g. to establish a clean measurement window)."""
    _FAILURES.clear()
```

A call site converting a hand-rolled block passes an `on_error`
callback that reproduces that block's own `log` call **byte-for-byte**,
so the emitted output is unchanged:

```python
with best_effort("post-close report re-stamp", on_error=_skip):
    restamp_persisted_report(workspace_root, cur)
```

New call sites can omit `on_error` and get a uniform
`"%s skipped: %s"` debug line.

Two properties make this safe to adopt mechanically:

- **Behavior-preserving.** The `except Exception` it replaces caught the
  same set; the control flow (swallow, never re-raise) is the same. The
  `label` and the counter are pure additions.
- **Non-perturbing.** The counter is process-local and **never
  persisted**. It cannot move any frozen / golden artifact, so it cannot
  change a run's outcome — it only adds a queryable signal.

`best_effort_failures` / `reset_best_effort_failures` are re-exported
from `zicato.health` so the loop-health-facing tooling can read the tally
alongside the pure detectors. The clean log-and-continue blocks in
`orchestrator.py` (14) and `_tournament_worker.py` (8) are already
converted. Value-returning, sibling-`ImportError`, and control-flow
blocks are left untouched, because swapping one of those for the context
manager requires restructuring the surrounding code.

## 4. The gap: collected, but surfaced nowhere

The mechanism described so far would still not have caught the §1
incident, for the following reason.

`best_effort_failures()` is tallied on every swallow and re-exported from
`zicato.health` — but **nothing reads it.** Neither the `zicato health`
CLI (`cli/commands/health.py`) nor the `GET /api/health-report` endpoint
(`dashboard/readers/gate_view.py:build_health_report`) calls it. The
loop-health `LoopHealth` report is built entirely from the pure detectors
over losses, experiments, and the board; the best-effort tally is not in
it.

So the wiring is **half-built**: failures are counted into a
process-local `Counter` that has no consumer. A target that fails to
produce output every round, or a dashboard write that 500s every round,
increments a counter that no surface displays. The observability the
mechanism promises is not yet realized — the degradation is still silent.

There is also a subtlety that the surfacing in §5 must respect:

- The `_FAILURES` counter is **in-process and in-memory**. It lives in
  the running loop process.
- `build_health_report` reads a **persisted** per-round health report
  file off disk (`epoch_health_dir`). It runs in the dashboard process,
  which is a *separate* process from the loop (see
  [RUNTIME-V2.md](RUNTIME-V2.md)).

A dashboard-side endpoint therefore cannot read the loop's live
`_FAILURES` directly. The tally must be **emitted from the loop process**
into something the dashboard reads — either folded into the per-round
health report the loop already writes, or published on the same
control/telemetry path the dashboard already consumes. §5 takes the
former route because it reuses an artifact that already exists.

## 5. Surfacing the tally

### 5.1 In the live loop process (`zicato health`)

The `zicato health` CLI assesses the *current* epoch from on-disk loss
profiles and experiments. When it runs **inside or against the live loop
process**, `best_effort_failures()` is populated and can be appended to
the rendered report directly:

```python
from zicato.health import best_effort_failures

# after render_report(report) ...
failures = best_effort_failures()
if failures:
    click.secho("Best-effort side effects that degraded this run:", fg="yellow")
    for label, count in sorted(failures.items(), key=lambda kv: -kv[1]):
        click.echo(f"  {label}: {count}")
```

This is an additive section that reports no finding: it changes neither
the detector severities nor the command's exit code. A non-empty tally is
an *advisory*. It says that these never-abort side effects failed, that
the optimization still ran, and that the result is therefore likely to
differ from what the loop appears to have produced.

### 5.2 In the per-round health report (for `/api/health-report`)

Because the dashboard process cannot see the loop's in-memory counter
(§4), the loop must **fold the tally into the per-round health report it
already persists**. Where the orchestrator writes the round's
`LoopHealth` to `epoch_health_dir`, also write a sibling field:

```json
{
  "epoch_id": "...",
  "checked_at": "...",
  "healthy": true,
  "findings": [ ... ],
  "best_effort_failures": { "live-status publish": 26, "index dual-write": 3 }
}
```

`build_health_report` then passes the field through alongside `findings`
and `healthy`, in the same way it already passes `checked_at`:

```python
report["best_effort_failures"] = (
    value.get("best_effort_failures")
    if isinstance(value.get("best_effort_failures"), dict)
    else {}
)
```

The dashboard's loop-health surface renders a non-empty
`best_effort_failures` map as a distinct advisory row, visually separate
from the optimization-signal findings, because it reports a degraded
*mechanism* where those findings report a degraded *signal*.

### 5.3 What the §1 incident would have looked like

With the two surfaces above in place, the inert target would have shown
up after the first round. Once the target-invocation swallow is converted
(§6) it increments a `"target invocation"` label every generation, and
both the command-line advisory and the dashboard row would show
`target invocation: 1`, then `2`, then `26`. A per-label count that
climbs monotonically against a single side effect is the signal that
distinguishes "failed every round" from "never failed", which is the
distinction §1 lacked.

## 6. Migration plan for the remaining blocks

The mechanism (§3) and its surfacing (§5) cover *visibility*. The
remaining work is *coverage*: the 244 blanket blocks that still carry a
bare `# noqa: BLE001` are not yet wrapped, so their failures are still
uncounted. The migration is mechanical and can proceed file-by-file
without coordination, because each conversion is behavior-preserving in
isolation.

**Per-site recipe.** For each `except Exception: # noqa: BLE001` block:

1. Confirm it is a **clean log-and-continue** block — it swallows, logs,
   and falls through, returning nothing the caller branches on. (Skip
   value-returning, sibling-`ImportError`, and control-flow blocks; they
   need restructuring and are out of scope for the mechanical pass.)
2. Choose a **stable `label`** describing the side effect ("live-status
   publish", "index dual-write", "target invocation"). Reuse the same
   label for the same logical side effect across call sites so counts
   aggregate.
3. Wrap the body in `with best_effort(label, on_error=_skip):`, where
   `_skip` reproduces the original `log` call **byte-for-byte** so output
   is unchanged.
4. Remove the now-dead `try/except` and its `# noqa: BLE001`.

**Suggested order** (highest-value first — these sit on the round's hot
path and are the most likely to fail systematically and silently):

1. **Target / adapter invocation** — `adapters/adk.py`,
   `proposer/proposer.py`. This is the §1 failure. A dead target is the
   single most expensive silent degradation; label it consistently and
   it becomes the headline advisory.
2. **Tournament round side effects** — `tournament/runner.py` (11),
   `tournament/worker_transport.py` (6), `_tournament_worker.py`
   (residual 2).
3. **Live dashboard / projection writes** — `evolve/dashboard_projection.py`
   (8), `dashboard/readers/judge_view.py` (10), `dashboard/server.py` (4).
   A systematically broken publish is invisible *by construction* — the
   loop keeps running and the dashboard just shows stale data.
4. **Telemetry / supervisor** — `telemetry/harmonograf_supervisor.py`
   (10), `telemetry/meta_loop.py` (8).
5. **Everything else** — the long tail of single-site files.

**Guardrails.**

- A site that is *load-bearing*, where the swallow masks a bug the loop
  should fail on, should not be wrapped. Fix it instead to catch the
  specific exception it expects. The conversion pass is an opportunity to
  find such sites: a blanket except that turns out to guard a programming
  error is a latent bug rather than a best-effort side effect.
- Keep `on_error` byte-identical to the original log line during the
  mechanical pass. Cleanups to the log text are a separate, reviewable
  change.
- The `# noqa: BLE001` inside `best_effort` itself stays — that one
  swallow is the whole point of the abstraction.

## 7. Why the mechanism keeps the never-abort invariant

The never-abort invariant is not negotiable: the loop's job is to
optimize, and a failed re-stamp must never cost a round. `best_effort`
preserves that invariant, because it narrows no except clause and
re-raises nothing. It changes only what is *observable*. A class of
failure that was indistinguishable from success now has a per-label count
that climbs when a side effect degrades. Surfaced in loop-health (§5),
that count replaces the §1 outcome — an operator reading the journal
after 26 wasted generations — with a first-round advisory reading
`target invocation: 1` that lets an operator stop the run.

See also: [LOOP-HEALTH.md](LOOP-HEALTH.md) (the adjacent "running but
meaningless" subsystem), [ROBUSTNESS.md](ROBUSTNESS.md) (the layers that
keep the loop alive), and [RUNTIME-V2.md](RUNTIME-V2.md) (the
loop/dashboard process split that §4–§5 must respect).
