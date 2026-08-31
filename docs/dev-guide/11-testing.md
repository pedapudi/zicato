# 11 — Testing

> **Covers.** How zicato proves it works: the pytest suite conventions and
> the autouse fixtures (`tests/conftest.py`), the deterministic-contract
> pinning philosophy (`tests/_contract_pins.py`) and its countermeasure
> duty, the two oracles (the known-answer convergence proof and the
> decision-procedure power harness), the subprocess-worker test support,
> the markers and lanes (`make test` / `test-fast` / `node-test` / `check`),
> the `tools/parity.sh` gates one by one, the seven import contracts +
> the TID251 bans, the Node behaviour-suite conventions, the
> generation-store conformance session templates, CI, and the pre-commit
> checklist — plus the two recipes every contributor eventually needs: write
> a regression test for a bug, and add a test that spawns real workers.
>
> **Prerequisites.** 02-architecture.md (orchestrator / worker / supervisor
> / dashboard as separate processes — what the subprocess and node suites
> actually exercise), 03-contract-and-epochs.md §3.7 (what
> CONTRACT-HASH pins), 04-evaluation-statistics.md (the gate / replication /
> noise-floor machinery the power harness characterizes), 07-runtime-and-
> durability.md (the durability invariants the conformance suites protect),
> 12-bug-casebook.md (every bug this chapter teaches you to re-pin).
>
> **Invariants introduced in this chapter.** Each is a testing discipline;
> breaking one lets a real regression ship green.
>
> Every sentence below cites these disciplines by the name in the second
> column; the ID column is the locator other documents use.
>
> | ID | Name | Invariant |
> |----|------|-----------|
> | V1 | the full-suite-is-the-default rule | **The full suite is the default; the fast lane is opt-in.** `make test` runs everything. A `slow`/`integration` test's runtime IS its coverage — never a candidate for stubbing. |
> | V2 | the must-fail-with-the-fix-stashed rule | **A regression test MUST fail with the fix stashed.** A test that passes both before and after a fix proves nothing about the fix. |
> | V3 | the never-weaken-an-assertion rule | **Never weaken an assertion to make a test pass.** Fix the code, or pin the new value with a measured justification in the commit. A pinned number moves only with a measured reason. |
> | V4 | the pin-off-and-carry-the-countermeasure rule | **Deterministic contracts pin interacting knobs OFF — AND carry the countermeasure.** Pinning best-of-1 / replicates-1 / gate-off makes a script deterministic, but every shipped default also needs a knob-ON adversarial test. Pinning alone is how the best-of-N tree-mismatch case and the evidence-gate replicate-reuse case hid (`12-bug-casebook.md` cases 6 and 8). |
> | V5 | the dotted-path-callable rule | **A worker subprocess resolves its adapter and callables from a dotted import path** — never a closure or a `sys.modules` monkeypatch. Those do not cross the process boundary. |
> | V6 | the clear-global-state-on-both-sides rule | **Autouse fixtures isolate process-global state on BOTH sides** (clear before AND after) so a test neither inherits nor bequeaths a pin. |
> | V7 | the provenance-scoped-reaper rule | **The dashboard reaper selects by workspace provenance and never signals its own process group.** A provenance-blind reaper once group-killed an innocent concurrent evolve (`12-bug-casebook.md` case 5). |
> | V8 | the parity-green-on-unchanged-behaviour rule | **Every parity gate is GREEN on unchanged behaviour; a RED gate is information.** A golden is re-captured only with a stated behavioural reason, and a re-capture never bakes an unrelated sibling change. |
> | V9 | the contracts-are-lint rule | **The library never imports a driver; the query layer stays dashboard-free; the retired private paths stay retired.** The import linter (`lint-imports`) and `ruff` catch these as violations; no pytest test does. |
> | V10 | the exit-code-is-the-node-signal rule | **A node suite's real signal is the PROCESS EXIT CODE rather than the tail line.** The `mock_server` mirrors the Python readers; a divergence is a bug in the mock. |

---

## 11.0 Map of the subsystem

| File / tool | What it is |
|---|---|
| `tests/conftest.py` | the suite root: `sys.path` pin + the six autouse fixtures (config-pin isolation, mutation-syntax-table isolation, worker-permit redirection, the default-proposer text-shim, the harmonograf launch stub, the provenance-scoped dashboard reaper) |
| `tests/_contract_pins.py` | `pin_deterministic` / `deterministic_weights` — the deterministic scripted-test knob pins |
| `tests/_subprocess_worker_support.py` | module-level importable adapters + callables the worker subprocess resolves by dotted path (the worker-boundary test support) |
| `tests/_best_of_n_slate_support.py` | the scripted best-of-N slate whose slot-2 fabricates its metrics, so a wrongly mounted tree is detectable |
| `tests/test_convergence_known_answer.py` | **the convergence oracle** — the full loop to an exact, hand-computable floor, no tournament stubs |
| `tests/test_decision_procedure_power.py` | **the power oracle** — the operating characteristics of the decision procedure under seeded noise |
| `tests/test_genstore_conformance.py` | the cross-backend `GenerationStore` conformance suite + the session-template fixtures |
| `tests/test_conftest_dashboard_reaper.py` | the reaper's regression pins (provenance scoping + no self-group-kill) |
| `tools/parity.sh` | the behavior-preserving refactor gates (PYTEST / CONTRACT-HASH / CLI-HELP / REINDEX-DUMP / eight MOCK-GOLDEN lanes / MYPY) |
| `tools/parity/lib/*.py` | the gate helpers: `contract_hash.py`, `cli_help.py`, `normalize.py`, `mock_evolve_capture.py`, `test_mock_golden.py`, `test_reindex_golden.py` |
| `tools/parity/golden/` | the committed golden baselines |
| `pyproject.toml` | `[tool.pytest.ini_options]` (markers, `addopts`), `[tool.importlinter]` (the five contracts), `[tool.ruff.lint...banned-api]` (the TID251 bans) |
| `Makefile` | the lane targets (`test` / `test-fast` / `node-test` / `lint` / `import-lint` / `typecheck` / `check`) |
| `.github/workflows/ci.yml` | the two CI jobs (Python matrix + the Rust supervisor) |
| `src/zicato/dashboard/static/test/run-all.mjs` | the Node behaviour-suite runner (exit-code-honest) |

The suite is large (2800+ Python tests plus the Node suite plus the Rust
`cargo test`), and it is **process-isolation-clean by construction** —
`tmp_path` everywhere, dynamic ports via `bind(("127.0.0.1", 0))`,
tempdir-isolated worker fixtures — so it fans out under `pytest-xdist`
with no per-test fixes.

> ⚠️ TRAP — the repo root is pinned on `sys.path` explicitly by
> `tests/conftest.py`, NOT left to pytest's implicit `rootdir` insertion.
> `tests/` is an importable package (`tests._subprocess_worker_support` is
> loaded by directly-spawned worker subprocesses), and `zicato` lives under
> `src/`, where pytest's implicit path handling is not reliable. If you add
> a helper module under `tests/` that a subprocess must import, it resolves
> because of that pin — do not "clean it up".

---

## 11.1 Lanes and markers

There is ONE default run (the full suite) and one opt-in fast lane. The
`addopts` in `pyproject.toml` fan out across cores and drop only the Node
shim:

```
addopts = "-n auto -m 'not node'"
markers = [
    "node: shells out to the standalone Node test harness (run via `make node-test`)",
    "slow: real-subprocess / real-server test whose runtime IS the coverage; the fast lane is `-m 'not slow and not node'` (the full suite stays the default run)",
    "integration: crosses a process or network boundary (worker subprocesses, live servers, git subprocesses)",
]
```
— `pyproject.toml`, `[tool.pytest.ini_options]`

The three markers and what they tag:

| Marker | Tags | In the default run? |
|---|---|---|
| `node` | the in-pytest shim (`tests/test_dashboard_js.py`) that re-runs the whole standalone Node suite inside pytest | NO — excluded by `addopts` `-m 'not node'` (it would duplicate `make node-test`) |
| `slow` | real-subprocess / real-server tests (worker isolation, live server boots, live harmonograf launches, the git-backed generation store) whose runtime IS the coverage | YES (the full suite is the default) |
| `integration` | any test crossing a process or network boundary | YES |

The Makefile lanes:

```make
test:
	@cd $(ROOT) && uv run pytest tests/

# The opt-in fast lane: drop the `slow`-marked real-subprocess / real-server
# tests (their runtime IS their coverage — run `make test` before merging).
test-fast:
	@cd $(ROOT) && uv run pytest tests/ -m "not slow and not node"

node-test:
	@cd $(JS_TEST_DIR) && node run-all.mjs

check: lint import-lint typecheck test node-test
```
— `Makefile`

> ⚠️ TRAP — a command-line `-m` REPLACES the `pyproject` `-m 'not node'`, it
> does not AND with it. So the fast lane is `-m "not slow and not node"`
> (both terms), NOT `-m "not slow"` — the latter re-enables the node shim and
> re-runs the whole Node suite inside pytest. The `test-fast` target and the
> `slow` marker's own help string both spell out both terms for this reason.

> ⛔ NEVER stub a `slow`-marked test's real subprocess / server / git call to
> "speed it up". The marker's whole point is that the runtime IS the coverage
> (the full-suite-is-the-default rule): a worker-isolation test proves a real
> subprocess writes to a real discarded checkout; a live-server test proves a
> real port binds. Stub it and
> you have kept the assertion and deleted the thing it asserts. If the fast
> lane needs to skip it, that is what the marker is for — the FULL suite stays
> the default and runs it before every merge.

`make check` runs `lint + import-lint + typecheck + test + node-test`; the
gates are independent (distinct caches, no shared state) so `make -j5 check`
runs them concurrently and finishes in `max(gate)` instead of `sum(gates)`.

### 11.1.1 Suite conventions

Four conventions make the suite fan out cleanly and keep an async test
honest:

```
[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
addopts = "-n auto -m 'not node'"
pythonpath = ["."]
```
— `pyproject.toml`, `[tool.pytest.ini_options]`

- **`asyncio_mode = "auto"`** — an `async def test_*` is collected and run
  without a per-test `@pytest.mark.asyncio`. Most of the loop is async; the
  suite tests it directly with `asyncio.run(...)` inside a sync test (the
  convergence oracle) or as a bare async test.
- **`-n auto`** — one xdist worker per detected core. The suite is
  **process-isolation-clean by construction**: `tmp_path` everywhere,
  dynamic ports via `bind(("127.0.0.1", 0))`, tempdir-isolated worker
  fixtures. `-n0` on the command line overrides back to a single serial
  in-process run for debugging (`pytest -n0 tests/test_foo.py::test_bar`).
- **`pythonpath = ["."]`** — pins the repo root so the src-layout `zicato`
  package resolves via the editable install AND `tests.*` helper imports
  resolve from a subprocess; the `sys.path` pin in `conftest.py` covers the
  same need a second time.
- **The fixture idiom is scope-then-copy.** An expensive immutable artifact
  is session-scoped and copied per test (§11.6); process-global state is
  cleared on both sides of the `yield` (§11.2.1); a real subprocess is
  bounded and no-leak-asserted (§11.16).

> ⚠️ TRAP — the suite's xdist-cleanliness is a PROPERTY YOU CAN BREAK. A test
> that binds a FIXED port, writes to a shared path outside `tmp_path`, or
> mutates a module global without the both-sides clear will pass alone and
> flake under `-n auto` (two workers collide nondeterministically). When a new
> test flakes only in the full run, the first suspect is a shared resource it
> did not isolate — never "xdist is flaky".

---

## 11.2 The autouse fixtures — `tests/conftest.py`

Six autouse fixtures shape every test.

- **Three isolate process-global state**, so a test neither inherits nor
  bequeaths it. `_isolate_config_pins` clears the pinned config overrides
  (§11.2.1). `_isolate_mutation_syntax_table` restores the built-in mutation
  syntax table, so a workspace that declares an extra file suffix cannot
  change what a later test enumerates as mutable surface.
  `_isolate_host_worker_permits` points the host-wide worker-permit pool at
  a session-private directory, so the suite neither throttles nor is
  throttled by an operator's concurrent run.
- **Two neuter production defaults** that would otherwise drag optional
  dependencies or real I/O into a suite about the loop (§11.2.2). Each of
  the two takes a `frozenset` of module names that exercise the real path
  and so skip the stub.
- **One is a session-scoped safety net**: the dashboard reaper (§11.2.3).

### 11.2.1 Config-pin isolation

CLI commands pin flag values process-wide via `config.pin_overrides`; the
pins are module-global and would leak across tests. `_isolate_config_pins`
clears them on BOTH sides so a test neither inherits nor bequeaths a pin:

```python
@pytest.fixture(autouse=True)
def _isolate_config_pins() -> Iterator[None]:
    """Clear process-pinned config overrides around every test.

    CLI commands (and tests exercising them) pin flag values process-wide
    via :func:`zicato.config.pin_overrides`; the pins are module-global
    state and would otherwise leak from one test into the next. Cleared
    on BOTH sides so a test neither inherits nor bequeaths pins.
    """
    from zicato.config import clear_pinned_overrides

    clear_pinned_overrides()
    yield
    clear_pinned_overrides()
```
— `tests/conftest.py`, `_isolate_config_pins`

> ✅ ALWAYS clear process-global state on BOTH sides of the `yield` (the
> clear-global-state-on-both-sides rule). A before-only clear leaks a pin
> FORWARD (the test that set it poisons the
> next); an after-only clear leaks a pin BACKWARD (a stray earlier pin
> poisons this test). Under `-n auto` the leak is nondeterministic — the two
> tests may not even land on the same worker — so a one-sided clear produces
> a flake nobody can reproduce.

### 11.2.2 The default-proposer text-shim pin

The production DEFAULT proposer is the tool-using ADK agent, which pulls in
the optional `google-adk` extra and a real model at propose time. The
orchestrator/evolve suites are about the loop rather than the proposer
model. This fixture therefore pins the builtin-default spec to the text-shim
engine driven by the stubbed auxiliary callable. EVERY other spec (a custom
`agent.py`, a skills-only dir) still flows through the real builder:

```python
    module_name = request.module.__name__.rsplit(".", 1)[-1]
    if module_name in _REAL_DEFAULT_PROPOSER_MODULES:
        return

    from zicato.core.types import ProposerSpec
    from zicato.proposer import agent as proposer_agent_mod

    real_build = proposer_agent_mod.build_proposer_agent

    def _build(spec: ProposerSpec, proposer_path: Path | None = None) -> Any:
        if spec == ProposerSpec.default():
            return proposer_agent_mod.DefaultProposerAgent(spec)
        return real_build(spec, proposer_path)

    monkeypatch.setattr(proposer_agent_mod, "build_proposer_agent", _build)
```
— `tests/conftest.py`, `_pin_default_proposer_to_text_shim`

The opt-out set `_REAL_DEFAULT_PROPOSER_MODULES = {"test_proposer_agent",
"test_proposer_adk_agent"}` names the two modules that assert on the REAL
selection (or drive the ADK default through their own monkeypatched
`build_default_adk_agent`), so the real selection path is still tested
directly there. `_stub_harmonograf_launch` follows the identical shape —
it replaces `orchestrator._resolve_or_launch_harmonograf` with a no-op that
returns the same `("", _NoopShutdownHandle())` shape the degraded-install
path returns, saving ~50s of real server startup across the suite, with
`_REAL_HARMONOGRAF_LAUNCH_MODULES` opting out the three modules that test the
launch decision itself.

> ⚠️ TRAP — these two fixtures are why an evolve/orchestrator test runs with
> no `google-adk` and no real model traffic. If you write a test that needs
> the ADK default agent or the real harmonograf launch, add
> your module to the matching opt-out `frozenset` — do NOT monkeypatch around
> the fixture inside your test (a fixture-fighting monkeypatch is fragile and
> hides which path you actually exercise). The opt-out list IS the registry of
> "tests that use the real thing".

### 11.2.3 The provenance-scoped dashboard reaper

The most instructive fixture. It kills a real `python -m zicato.dashboard`
child that a test leaks. The failure it must avoid is killing a dashboard it
does NOT own, and two rules keep it from doing so. Together those rules are
the provenance-scoped-reaper rule.

**Selection is workspace-scoped.** Only dashboards whose `--workspace` argv
points INSIDE this session's pytest temp root are ever selected. The
workspace path is the ownership fingerprint:

```python
def _session_dashboard_pids(tmp_root: Path) -> list[int]:
    """PIDs of live ``python -m zicato.dashboard`` children OWNED BY THIS
    TEST SESSION — i.e. whose ``--workspace`` argv points inside this
    session's pytest temp root.
    ...
    A dashboard serving ANY other
    workspace — an operator's live instance, a concurrently-running
    ``zicato evolve`` on this host — is provably not ours and must never
    be selected. (The previous before/after pid-snapshot heuristic got
    exactly that wrong: a dashboard that a *concurrent* evolve spawned
    mid-session looked "leaked" and was group-killed, taking the whole
    innocent evolve invocation down with it.)
    """
```
— `tests/conftest.py`, `_session_dashboard_pids` (docstring)

**The kill path never signals its own process group.** It escalates
SIGTERM→SIGKILL and prefers a group kill (the evolve spawn helpers use
`start_new_session=True`, so a real dashboard child leads its own group) —
but if the target still shares the test runner's group, it signals only the
bare pid, so the safety net can never take down the pytest session itself:

```python
    own_pgid = os.getpgid(0)
    reaped: list[int] = []
    for pid in _session_dashboard_pids(tmp_root):
        reaped.append(pid)
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                pgid = os.getpgid(pid)
                if pgid == own_pgid:
                    os.kill(pid, sig)
                else:
                    os.killpg(pgid, sig)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    os.kill(pid, sig)
                except (ProcessLookupError, PermissionError, OSError):
                    break
    return reaped
```
— `tests/conftest.py`, `_reap_session_dashboards`

The regression pins live in `tests/test_conftest_dashboard_reaper.py`: a
foreign-workspace dashboard is invisible to the sweep (no signal at all),
and a session-owned leak that still shares the runner's group is signalled
by BARE PID, never `killpg`:

```python
        reaped = suite_conftest._reap_session_dashboards(tmp_path.resolve())
        assert reaped == [child.pid]
        assert signalled, "the sweep must signal a session-owned leak"
        assert all(kind == "kill" for kind, _ in signalled), (
            "a child sharing our process group must be signalled by bare pid, "
            f"never killpg: {signalled}"
        )
```
— `tests/test_conftest_dashboard_reaper.py`, `test_reaper_kills_by_pid_when_child_shares_our_group`

The fixture is SESSION-scoped on purpose: its contract is only that a leaked
child never survives the SESSION, so one sweep at session end (per xdist
worker) keeps that contract at ~0 cost. Tests should never leak a real
dashboard in the first place — the `mock_dashboard_spawn` fixture patches
`asyncio.create_subprocess_exec` with a non-spawning `FakeDashboardProc` so
an `evolve` CLI test (which by design leaves the dashboard serving at a
normal conclusion) never launches a real child.

> ⛔ NEVER write a process-reaper that classifies "leaked" by a before/after
> pid snapshot, and never `killpg` without first checking the target's group
> against your own. A snapshot heuristic once saw a concurrent evolve's
> dashboard appear mid-session, called it "leaked", and group-killed it,
> taking the innocent evolve down (`12-bug-casebook.md` case 5). Scope by an
> OWNERSHIP fingerprint (the
> workspace path under this session's tmp root), and never signal your own
> process group. This lesson recurs anywhere a test spawns real OS processes
> (§11.16).

### 11.2.4 The CLI-evolve scaffolding — `FakeDashboardProc`

A CLI `evolve` test runs the real command to a normal conclusion, and
`evolve` LEAVES the dashboard serving at a clean end by design. So a test
that lets the real spawn happen ORPHANS a dashboard subprocess. The
`mock_dashboard_spawn` fixture patches `asyncio.create_subprocess_exec` with
a non-spawning `FakeDashboardProc` that records terminate/kill without
starting an OS process:

```python
class FakeDashboardProc:
    """Minimal stand-in for an ``asyncio.subprocess.Process``.

    Records terminate/kill so a test can assert the teardown path while
    never starting a real OS process. ...
    """
```
— `tests/conftest.py`, `FakeDashboardProc`

The fake also publishes a fake `runtime/dashboard.json` endpoint file so the
CLI's bound-port readback resolves IMMEDIATELY instead of polling the full
fallback timeout — the real server would write that file once it bound a
port, so the fake short-circuits the wait. Any CLI test that runs `evolve`
to a normal conclusion must use `mock_dashboard_spawn` (directly or
transitively); the session reaper (§11.2.3) catches a spawn that slips
through.

> ✅ ALWAYS use `mock_dashboard_spawn` in a CLI test that runs `evolve` to
> completion. The two-layer defence is deliberate: the fixture stops the real
> spawn (hermetic, fast), the session reaper catches any leak that slips past
> (bounded, provenance-scoped). Relying on the reaper alone leaves a real
> dashboard squatting on a port for the whole session — slow, and a port
> collision under xdist.

---

## 11.3 The deterministic contract pins — pin off, then attack on

The shipped defaults are **noise-aware**: best-of-3 proposer sampling, two
averaged replicates per gauntlet duel, the Bradley–Terry evidence gate
opt-in. But most orchestrator/e2e tests drive SCRIPTED single-shot proposers
and stub reducers whose call sequences assume exactly one propose per round
and one paired run per duel. Such a test pins the interacting knobs
explicitly, the way an operator running a deterministic harness would.

`pin_deterministic(weights)` gives the test a single-run, gate-off duel and
a single-sample proposer:

```python
#: The param pins that restore the historical single-run, gate-off duel.
DETERMINISTIC_PARAM_PINS: dict[str, Any] = {
    "replicates": 1,
    "promote_confidence_threshold": None,
}
```
— `tests/_contract_pins.py`

and, for any key the caller did not already pin, sets `best_of_n=1` on the
proposer quality config. The module docstring states why the pinning is a
design choice rather than a workaround:

```python
"""Pinned deterministic contract knobs for scripted orchestrator tests.
...
Pinning is by-design, not a workaround: the knobs are contract inputs,
and a test whose subject is the SCRIPT (not the sampling/replication
machinery) should pin them. Tests whose subject IS a new default assert
the new value instead.
"""
```
— `tests/_contract_pins.py` (module docstring)

### 11.3.1 The countermeasure duty — knob-ON adversarial tests

Pinning is necessary but NOT sufficient. A suite that ONLY pins the
interacting knobs OFF has zero coverage of the knobs when they are ON, and
that blind spot is how the two defects below shipped. Every pinned-off knob
owes a knob-ON adversarial test.

- **The best-of-N tree-mismatch case hid behind best-of-1**
  (`12-bug-casebook.md` cases 6 and 7). With `best_of_n=1` the mounted child
  tree is the only candidate's, so a tree mismatch cannot manifest. The
  defect reached only `best_of_n>1`, the default, where one shared on-disk
  tree served every sample while the selection could pick an earlier
  candidate. The slate now derives per-slot scratch trees and mounts the pick
  once after selection (05-proposer.md §5.6.5); the
  pin-off-and-carry-the-countermeasure rule applies to that knob all the
  same. The countermeasure is `tests/test_best_of_n_tree_integrity.py`
  driving REAL evolve rounds at the default `best_of_n`, with a scripted
  slate (`tests/_best_of_n_slate_support.py`) whose slot-2 fabricates its
  metrics — a known-answer scalar then detects a wrong mounted tree both by
  content and by arithmetic.
- **The evidence-gate replicate-reuse case hid behind gate-off**
  (`12-bug-casebook.md` case 8). That defect exists only when the evidence
  pre-gate is ON; a deterministic contract with
  `promote_confidence_threshold: None` never runs it. The countermeasure is a
  test whose SUBJECT is the gate — it pins the gate ON and attacks the reuse.

> ⛔ NEVER let a shipped default be tested ONLY through contracts that pin it
> OFF. That is how the tree-mismatch and replicate-reuse defects shipped
> green: every scripted test pinned the noise-aware machinery off, so nothing
> exercised it. Under the pin-off-and-carry-the-countermeasure rule, for every
> knob a deterministic contract pins off, there is at least one test whose
> SUBJECT is that knob ON, driving the real machinery with an adversarial
> fixture designed to expose the failure the knob enables.

> ✅ ALWAYS reach for `deterministic_weights(**kwargs)` /
> `pin_deterministic(weights)` when your test's subject is a SCRIPT (a fixed
> proposer sequence, a stubbed reducer). Reach for the bare `ScoringWeights()`
> defaults when your test's subject is a DEFAULT (the sampling, the
> replication, the gate). The `_contract_pins` helper and the noise-aware
> default are the two halves, and a healthy suite uses both.

---

## 11.4 The two oracles

Two tests carry disproportionate weight. Both drive the REAL loop; neither
stubs the thing it proves. One proves the loop CONVERGES when measurement is
exact; the other proves the DECISION PROCEDURE has the right operating
characteristics when measurement is noisy.

### 11.4.1 The known-answer convergence harness

`tests/test_convergence_known_answer.py` is the end-to-end proof that the
shipped evolve loop converges on a planted-defect target: real propose →
apply → validate → **subprocess tournament workers** → reduce → gate →
persist, under the DEFAULT git generation-store, with a scalar that lands on
an exact, hand-computable floor. Nothing tournament-side is monkeypatched —
only the shared conftest autouse fixtures apply.

**What it proves.** The target seeds three defect tokens; each remaining
token emits one info-severity drift frame (`+1.0` drift loss per run) and
each known token fails exactly one board predicate. The scalar is
hand-computable from the shipped formula:

```python
    scalar(k, passes) = 1.0 * k  +  1.0 * (1 - passes/5)

    v0 (3 tokens, 2/5 pass) = 3.6      — seeded baseline
    v1 (2 tokens, 3/5 pass) = 2.4      — round 1, PROMOTED
    v2 (3 tokens, 2/5 pass) = 3.6      — round 2, REJECTED (control)
    v3 (1 token,  4/5 pass) = 1.2      — round 3, PROMOTED = THE FLOOR
```
— `tests/test_convergence_known_answer.py` (module docstring)

The scripted three-round gauntlet is *remove a token (→ promote), ADD a
token (→ reject, the negative control), remove another (→ promote to the
floor)*. The negative control is load-bearing: a loop that promoted
everything would still pass a "converges" assertion, so the middle round
plants a regression and asserts it is REJECTED (`r2.child_scalar >
r2.parent_scalar, "the negative control must regress"`).

**The exact expected artifacts and event sequence.** The test does not
merely assert the final floor — it asserts the whole durable trail, because
a loop that reaches the right number by the wrong path is still broken:

- **Decisions + lineage:** `[o.tournament_decision for o in outcomes] ==
  ["promoted", "rejected", "promoted"]`, the proposed ids `["v1","v2","v3"]`,
  the parents `["v0","v1","v1"]`, and every scalar equal to its
  hand-computed constant (`r3.child_scalar == EXPECTED_FLOOR`,
  `EXPECTED_FLOOR == 1.2`).
- **The real git backend actually backed it:** `default_generation_store`
  returns a `GitGenerationStore`, `repo/.git` exists, and
  `store.list_generations(epoch_id) == ["v0","v1","v2","v3"]`.
- **The per-round durable event log (`RoundLog`):** its exact transition
  sequence is pinned, and the fold reproduces the round:

```python
        types = [e.type for e in events]
        assert types == (
            ["round_opened", "proposal_attempted", "experiment_minted", "patches_applied"]
            + ["unit_completed"] * (2 * BOARD_SIZE)
            + ["gate_evaluated", "decision_recorded", "round_closed"]
        ), f"round {round_index}: {types}"
```
— `tests/test_convergence_known_answer.py`, `test_gauntlet_converges_to_known_floor`

  The test spells out WHY that exact sequence: the contract pins
  `best_of_n=1` (scripted proposer) so no `candidate_sampled` /
  `critique_selected` events appear; the 5-entry board is below the split
  floor so no holdout events appear; the evidence pre-gate is off so no
  `evidence_replicated` events appear. Each absence is a pinned-knob
  consequence, documented in the test.
- **Per-unit `loss.json` for the final champion:** the exact per-run
  numbers the floor is built from (one info-severity drift frame, 4/5
  predicates passing), asserted entry by entry.
- **Index uniqueness:** the `runs` table keeps every generation's rows
  (`per_gen == {gid: BOARD_SIZE for gid in ("v0","v1","v2","v3")}`,
  `4 * BOARD_SIZE` unique run ids) — the pin against reused run ids, which
  would let a later generation's rows overwrite an earlier one's.
- **Loop health:** no `degenerate_scoring` / `non_differentiating_entry`
  finding in any round (a planted-defect design that stopped differentiating
  generations would trip those).

A sibling test, `test_racing_field_best_arm_survives_to_floor`, drives the
same target through a REAL multi-challenger racing round (field 4, replicates
2, evidence pre-gate at 0.8) and asserts the best-known arm survives every
rung, clears the champion gate, and is promoted at the exact floor — the
knob-ON counterpart, under the pin-off-and-carry-the-countermeasure rule, to
the gauntlet oracle.

> ✅ ALWAYS extend this oracle (not a new stubbed test) when you change the
> scoring formula, the gate, the storage backend default, or the RoundLog
> vocabulary. It is the one test that proves the WHOLE path agrees on an exact
> number — a scoring change that moves the floor moves `EXPECTED_FLOOR`, a
> RoundLog change moves the pinned `types` sequence, and either edit is a
> visible, reviewable claim. A change that passes the unit suite but breaks
> this oracle broke the end-to-end contract.

> ⚠️ TRAP — this oracle uses the example's skills-only proposer dir
> (`EXAMPLE_DIR / "proposer"`), which selects the REAL skill-composed
> text-shim proposer (a `dir:*` spec flows through the real
> `build_proposer_agent`), so it does NOT depend on the conftest
> default-proposer pin. Do not "simplify" it to the bare default — the point
> is that a real, disk-resolved proposer drives the real loop.

### 11.4.2 The decision-procedure power harness

Where the convergence oracle proves the loop converges under exact
measurement, `tests/test_decision_procedure_power.py` proves the decision
procedure itself — the margin gate, replication, pass-rate monotonicity
scope, and the Bradley–Terry evidence pre-gate — has the right OPERATING
CHARACTERISTICS when measurement is noisy, the way it is in production.

**The methodology.** Every trial is reproducible: the noise model is
the example harness's own `draw_measured_tokens`, seeded from the stable
tuple `(workspace seed, generation id, entry id, replicate index)` via
`stable_noise_seed`. Nothing derives from the clock or a global RNG, so the
"rates" the test asserts are DETERMINISTIC functions of the chosen seeds —
calibrated documentation of the procedure's behaviour rather than flaky
statistics:

```python
"""Operating characteristics of the DECISION PROCEDURE under seeded noise.
...
Every trial is exactly reproducible: the noise model is the example
harness's own :func:`draw_measured_tokens`, seeded from the stable
identifier tuple ``(workspace seed, generation id, entry id, replicate
index)`` via :func:`stable_noise_seed`. ... so the "rates" asserted below
are deterministic functions of the chosen seeds, and the assertions are
calibrated documentation of the procedure's behaviour, not flaky statistics.
"""
```
— `tests/test_decision_procedure_power.py` (module docstring)

It drives the REAL tournament machinery in-process — `run_matchup` (board-
unit scheduling, replicate averaging, the unchanged gate) and
`resolve_tournament` (the gauntlet strategy + the evidence pre-gate's
defer→replicate loop) — swapping ONLY the subprocess-worker boundary
`runner._run_single` (the suite's documented monkeypatch anchor) for an
in-process evaluator built on the SAME noise model, output synthesis, and
REAL board predicates. One test at the bottom drives the actual
`NoisyPolicyAdapter` through real subprocess workers to prove the seeded
draw crosses the process boundary intact.

**The four methodology pillars, each a pinned number:**

1. **Seeded noise.** `NOISE_SIGMA = 0.22`, chosen so one full defect fix
   lands at ~1× the noise floor. The arithmetic is written into the file so a
   future reader can re-derive it (a per-token effect of 1.2 is measured as
   `1.2*(1 - 2*sigma) = 0.672`; the A/A floor is analytically
   `1.6*sqrt(sigma*(1-sigma)) = 0.663`).

2. **A/A nulls.** Trials that race a generation against ITSELF measure the
   null delta-scalar distribution — the noise floor. `AA_TRIALS = 60`
   single-sample duels measure it cheaply; the assertion is that the naive
   single-sample procedure's false-promote rate under the null is what the
   arithmetic predicts.

3. **Planted δ.** `DELTA_CASES` plants three known effects — `small` (~0.5×
   floor, half-fix one defect), `medium` (~1× floor, fully fix one), `large`
   (~3× floor, fix all three) — each with the exact measured delta it plants:

```python
DELTA_CASES: dict[str, tuple[tuple[str, ...], float]] = {
    "small": (("verbose-prose", "omit-summary", "sometimes-50-skip-citations"), 0.336),
    "medium": (("verbose-prose", "omit-summary"), 0.672),
    "large": ((), 2.016),
}
```
— `tests/test_decision_procedure_power.py`

4. **Pinned OC numbers.** The NAIVE contract (`replicates=1`, fixed
   `promote_margin=0.01`, no gate) and the EFFECTIVE contract
   (`replicates=32`, aggregate-scope monotonicity, the BT evidence pre-gate
   at 0.8) each get their operating characteristics pinned. One measured fact
   carries the design: a two-contestant Bradley–Terry confidence interval
   only separates after about 37 duels of an unbroken win streak. The
   pre-gate is therefore a SOUNDNESS device alone, and POWER must be bought
   with replication. Both halves are pinned (`EFFECTIVE_REPLICATES = 32`,
   `EFFECTIVE_THRESHOLD = 0.8`, `EFFECTIVE_BUDGET = 38`).

**When you may move a pinned OC number.** These numbers are the procedure's
characterized behaviour. They move ONLY with a measured justification in the
commit — never nudged to make a red test green.

> ⛔ NEVER change a pinned rate / trial count / threshold in the power harness
> to make it pass. Those numbers ARE the decision procedure's measured
> operating characteristics (the never-weaken-an-assertion rule). If a change
> to the gate or replication moves
> them, that is the test doing its job — the RED tells you the procedure's
> false-promote rate or its power changed. Re-derive the new number from the
> seeded model, WRITE the derivation in the commit (the file's own arithmetic
> comments are the template), and only then move the pin. A pin moved without
> a stated measurement is an un-review-able claim that the procedure is still
> sound.

> ⚠️ TRAP — the harness monkeypatches `runner._run_single` (the ONE
> documented anchor) rather than the gate or the strategy. If you add a NEW
> subprocess
> seam, do not monkeypatch it here — thread your in-process evaluator through
> `_run_single` so the REAL gate and REAL strategy still run. The whole value
> is that everything above the worker boundary is production code; a second
> monkeypatch above `_run_single` would hollow out the coverage.

### 11.4.3 The in-process evaluator seam — `_NoisyWorld`

The mechanism that keeps the power harness fast AND real is `_NoisyWorld`,
which replaces the ONE subprocess-worker boundary with an in-process
evaluator built on the SAME noise model, output synthesis, and REAL board
predicates a worker run reduces to:

```python
class _NoisyWorld:
    """In-process stand-in for the subprocess worker, on the SAME noise model.

    Replaces ``runner._run_single`` (the suite's documented monkeypatch
    anchor) with an evaluator that reproduces exactly what a noisy-adapter
    worker run reduces to: draw the measured tokens with
    :func:`draw_measured_tokens` seeded from ``(config.seed, generation id,
    entry id, replicate index)``, synthesize the REAL output with
    :func:`synthesize_output`, evaluate the entry's REAL predicate on it,
    and score one info-severity drift frame per measured token (the exact
    reduction Tier 1 pinned end-to-end). The replicate index is read from
    ``entry.context`` — the same stamp the real worker path consumes — so
    the production replication threading is exercised, not bypassed.
    """
```
— `tests/test_decision_procedure_power.py`, `_NoisyWorld`

Two properties make this a legitimate substitute rather than a stub of the
thing under test: (1) it reproduces the EXACT reduction the convergence
oracle already pinned end-to-end through real subprocess
workers, so the in-process path and the real path score identically; (2) it
reads the replicate index from `entry.context` — the same stamp the real
worker consumes — so the production REPLICATION threading is exercised
rather than bypassed. Everything ABOVE the worker boundary — `run_matchup`'s
scheduling and averaging, `resolve_tournament`'s gauntlet strategy, the
evidence pre-gate's defer→replicate loop, the gate — is production code. The final
test in the file drives the ACTUAL `NoisyPolicyAdapter` through real
subprocess workers to prove the seeded draw crosses the process boundary
intact, closing the loop between the fast in-process trials and the real
worker path.

The trial counts are sized so the whole file stays fast while the measured
rates stay meaningful: `AA_TRIALS = 60` cheap single-sample null duels,
`AA_EFFECTIVE_TRIALS = 24` and `POWER_TRIALS = 12` for the replicated
procedure (each effective trial runs up to ~39 replicated duels, ~12k board
units in-process). These counts are pinned like every other OC number — they
move only with a measured reason (§11.4.2, the never-weaken-an-assertion
rule).

> ⚠️ TRAP — `_NoisyWorld` is a substitute for the WORKER rather than for the
> decision procedure. Its `install()` silences the best-effort dashboard-live
> append and the per-unit cache persist (orthogonal side channels), but it
> does NOT touch the gate, the strategy, or the averaging — those are the
> subject. If you find yourself patching one of THOSE to make a power trial
> pass, stop: you are stubbing the thing the harness exists to characterize,
> and the pinned rate you are trying to hit means nothing.

---

## 11.5 The worker-boundary test support

`zicato._tournament_worker` runs in a SEPARATE OS process. So the adapter
and `call_llm` callables it uses CANNOT be closures or
`sys.modules`-monkeypatched stubs — they must be real, importable,
module-level objects the worker subprocess can resolve from a dotted path.
`tests/_subprocess_worker_support.py` provides them:

```python
"""Importable stub adapter + callables for the subprocess-worker tests.

The :mod:`zicato._tournament_worker` worker runs in a *separate* OS
process, so the adapter and ``call_llm`` callables it uses cannot be
closures or ``sys.modules``-monkeypatched stubs — they must be real,
importable, module-level objects the worker subprocess can resolve from
a dotted path. This module provides exactly that, mock-driven and with
no goldfive / real-LLM dependency.
"""
```
— `tests/_subprocess_worker_support.py` (module docstring)

Each stub adapter exposes a `worker_spec()` returning the `import` spec the
runner uses to re-construct the adapter INSIDE the worker — a dotted
`factory` path to a module-level function:

```python
    def worker_spec(self) -> dict[str, Any]:
        return {
            "kind": "import",
            "factory": "tests._subprocess_worker_support:make_stub_adapter",
        }
```
— `tests/_subprocess_worker_support.py`, `StubAdapter.worker_spec`

The module is a catalogue of adversarial worker behaviours, each a distinct
importable adapter because each must survive the process crossing:

| Adapter | Behaviour it forces | What it tests |
|---|---|---|
| `StubAdapter` | the two-argument `run(entry, sink_path)` form, writing an empty events file | the happy path, no goldfive dependency |
| `SnapshotWritingAdapter` | writes runtime output INTO the mounted snapshot | per-run checkout isolation — the write must land in a discarded per-run copy, never the canonical snapshot |
| `SleepingAdapter` | a BLOCKING `time.sleep` that wedges the worker's own event loop | forces the PARENT's `wait_for` + SIGTERM/SIGKILL escalation (the cooperative budget can't fire) |
| `CooperativeAdapter` | a CANCELLABLE `asyncio.sleep` | the worker's own cooperative budget fires and it self-aborts, exit 0 |
| `EmittingThenSleepingAdapter` | emits one `run_started` frame then sleeps to cancellation | the terminal-event fix leaves a `run_aborted` frame on disk |
| `AbortingAdapter` | returns an aborted `RunResult` (a simulated crash) | the reducer's not-completed penalty (without it a near-instant crash scores `drift_loss == 0.0`) |
| `ConfigProbeAdapter` | records the WORKER process's resolved typed config to `config_probe.json` | a CLI-flag value pinned in the ORCHESTRATOR crossed the subprocess boundary via the args file, no env var |

`make_sigterm_ignoring_adapter` is the sharpest example of why these live at
module level: it installs a `SIGTERM`-ignoring handler INSIDE the worker
subprocess (the worker calls the factory there), forcing the parent's
escalation all the way to SIGKILL:

```python
def make_sigterm_ignoring_adapter() -> SleepingAdapter:
    """Factory that installs a SIGTERM-ignoring handler, then sleeps forever.

    Runs *inside the worker subprocess* (the worker calls the adapter
    factory there), so the ``signal.signal`` call makes the worker
    survive the parent's SIGTERM and forces escalation to SIGKILL.
    """
    import signal  # noqa: PLC0415

    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    return SleepingAdapter()
```
— `tests/_subprocess_worker_support.py`, `make_sigterm_ignoring_adapter`

> ⛔ NEVER stub a subprocess-worker's adapter or callable with a closure, a
> `monkeypatch.setattr`, or a `sys.modules` injection. NONE of those cross a
> `fork`/`exec` boundary — the child re-imports fresh and sees the real
> module. A worker stub MUST be a module-level object with a dotted `factory`
> path (the dotted-path-callable rule). If your test needs the worker to do
> something new, add a named
> adapter + `make_*` factory to `_subprocess_worker_support.py`, do not reach
> for a monkeypatch that silently no-ops in the child.

> ⚠️ TRAP — a monkeypatch that "works" against an in-process runner will
> SILENTLY do nothing once the runner spawns a real subprocess. The failure is
> not an error; the child just runs the un-patched code and your assertion
> fails for a baffling reason. If a worker test's behaviour ignores your stub,
> the first question is "did the stub cross the process boundary?" — and the
> answer for a closure/monkeypatch is always no.

---

## 11.6 The generation-store conformance suite and its session templates

`tests/test_genstore_conformance.py` is the cross-backend contract: every
test runs against BOTH `DirectoryGenerationStore` AND `GitGenerationStore`,
parametrised on a single `backend` axis. A backend that diverges from the
`GenerationStore` protocol fails here — this is the suite the stale-worktree
bug (07-runtime-and-durability.md §7.4.2) and the prune-vs-add race would
have caught, and the reason a generation-store change is written HERE rather
than in a single-backend file.

The pattern is the **session-template fixture**. Seeding the git backend
costs a dozen-plus `git` subprocess spawns, so the seeded workspace is built
ONCE per backend (session-scoped) and `copytree`-d per test. Each test still
gets a private, writable workspace, without the per-test spawn storm:

```python
@pytest.fixture(scope="session")
def _seeded_ws_templates(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """Per-backend workspace templates with ``e1/v0`` already seeded.

    Seeding the git backend costs a dozen-plus ``git`` subprocess spawns
    (init + identity + add + commit + tag + worktree). Building the seeded
    workspace ONCE per backend and ``copytree``-ing it per test keeps every
    test hermetic — each test still gets a private, writable workspace —
    while dropping the per-test spawn storm. Tests whose contract IS the
    seeding behaviour keep seeding a fresh store instead.
    """
```
— `tests/test_genstore_conformance.py`, `_seeded_ws_templates` (docstring)

The per-test `seeded_store` fixture takes a private `copytree` of the
session template so mutations (derives, new worktrees) never leak between
tests. The last sentence of the docstring is the rule for **what may be
session-scoped vs per-test**:

- **Session-scoped:** the EXPENSIVE, IMMUTABLE setup a test merely READS
  from (the seeded `e1/v0` tree). Copied per test so it is still hermetic.
- **Per-test (fresh store):** any test whose CONTRACT is the expensive
  setup itself — `test_seed_generation_materialises_tree`,
  `test_seed_generation_excludes_run_artifacts`,
  `test_seed_generation_raises_for_missing_source` all seed a FRESH store,
  because their subject is the seeding rather than a pre-seeded tree.

There is one git-specific subtlety the template must handle: a materialised
git worktree registers its ABSOLUTE path inside the repo, which cannot
survive relocation-by-`copytree`. The template drops the worktrees and
prunes the registrations so each copy re-materialises its own on first
`materialize_snapshot()`:

```python
        worktrees = ws / GitGenerationStore.WORKTREES_DIRNAME
        if worktrees.is_dir():
            shutil.rmtree(worktrees)
            subprocess.run(
                ["git", "worktree", "prune"],
                cwd=str(ws / GitGenerationStore.REPO_DIRNAME),
                check=True,
                capture_output=True,
            )
```
— `tests/test_genstore_conformance.py`, `_seeded_ws_templates`

> ⚠️ TRAP — a session-scoped fixture that returns a MUTABLE store instead of a
> COPYABLE template is a cross-test contamination bug: one test's derive would
> leak into the next. The pattern is session-scope the TEMPLATE (immutable,
> the expensive artifact), then `copytree` a private mutable copy per test.
> And never session-scope a fixture a test whose subject IS the setup depends
> on — those seed fresh. Getting this wrong trades a spawn storm for a
> nondeterministic flake, which is a worse deal.

> ✅ ALWAYS write a new generation-store test in the conformance suite
> (parametrised over both backends) rather than in
> `tests/test_epoch_genstore.py` (the
> directory-specific file). The directory backend clears + rebuilds the child
> tree on every derive, so a directory-only test is GREEN while the git
> default is broken — the stale-worktree case. The conformance
> parametrisation is what makes a backend divergence a test failure instead
> of a production surprise.

---

## 11.7 The parity gates, one by one

`tools/parity.sh` is the behavior-preserving refactor oracle: a fixed set of
gates, each GREEN on unchanged behaviour. The goldens record the behaviour
of the feature-complete base, so a refactor that moves any observable
behaviour turns a gate RED (the parity-green-on-unchanged-behaviour rule):

```
# The contract: on UNCHANGED behavior every gate is GREEN. The refactor is
# validated as isomorphic to the feature-complete integration base by
# keeping every gate GREEN throughout. The goldens were captured from that
# base, so a refactor that moves any observable behavior turns a gate RED.
```
— `tools/parity.sh` (header)

The script reports a separate verdict for each of thirteen gates: the full
test suite, three golden diffs (the contract hash, the CLI help text, the
index dump), the eight mock-evolve lanes of §11.7.5, and the mypy error
count. The sections below take them in that order, one kind at a time.

Usage: `bash tools/parity.sh` runs every gate; `--only GATE` / `--skip GATE`
scope it; `--update` re-captures every golden. Exit code is 0 only if every
selected gate passed. Both scoping flags repeat (`--only A --only B`) and
also take a comma list (`--only A,B`). A gate name that matches nothing is
silent: the run prints an empty verdict and exits 0, so check the verdict
lists the gates you asked for.

### 11.7.1 PYTEST

The full suite (2800+ tests) — the primary behavioral characterization. Reds
legitimately whenever a real behaviour changed or a test broke. This is the
same run as `make test`; parity runs it as gate one because a golden diff on
top of a red suite is noise.

### 11.7.2 CONTRACT-HASH (incl. checkout-independence)

Pins the full epoch contract hash AND every per-component hash for a fixed
fixture contract (the `target_1_presentation` example) to a golden. The hash
is the load-bearing identity of an evaluation contract; an UNCHANGED contract
must hash to the SAME value across any behavior-preserving refactor, or every
operator's workspace spuriously rolls its epoch on the next run:

```python
"""Parity CONTRACT-HASH gate helper.
...
which means an UNCHANGED
contract must hash to the SAME value across any behavior-preserving
refactor. If a refactor moves the hash for an unchanged contract, every
operator's workspace would spuriously roll its epoch on the next run.
"""
```
— `tools/parity/lib/contract_hash.py` (module docstring)

The component breakdown (board / brief / scoring / entrypoint /
mutable_trees / proposer) localizes a regression to the exact canonicalizer
that moved. **Reds legitimately** when a canonicalizer changes what it hashes
(rare — usually a bug). The fixture pins fixed entrypoint + mutable_trees so
the hash depends only on committed file contents + those literals, never on
anything host- or clock-derived.

**Checkout-independence** is the related in-suite pin
(`tests/test_epoch_contract.py::test_contract_hash_is_cwd_and_checkout_invariant`).
It guards against the case where registration-relative mutable trees resolve
against the process cwd and fold the absolute checkout path into the hash, so
that the same workspace hashes differently when run from a different
directory (`12-bug-casebook.md` case 10):

```python
def test_contract_hash_is_cwd_and_checkout_invariant(tmp_path, monkeypatch):
    """The hash must identify the CONTRACT, not the checkout.

    Registration-relative mutable trees previously resolved against the
    process cwd, folding the absolute checkout path into the hash — the
    same workspace hashed differently run from a different directory (or
    after being moved) and spuriously rolled its epoch.
    """
```
— `tests/test_epoch_contract.py`

The test computes the hash from two different cwds (`compute_from(tmp_path)
== compute_from(other)`) and asserts `./` and `../` spellings normalize
identically — the contract identity must survive being run from anywhere or
moved.

### 11.7.3 CLI-HELP

Captures `--help` for the root group AND every subcommand into one stable
document (rendered in-process via Click at a pinned 80-col wrap, no
subprocess) and asserts byte-identity. The CLI surface — command set,
options, defaults, help prose — is observable behaviour a refactor must not
move. **Reds legitimately** whenever you change a command, flag, default, or
help string. **The update mechanism:** `--help` is canonical; regenerate the
golden with `uv run python tools/parity/lib/cli_help.py --update` (this is
also how `docs/design/CLI.md` stays accurate — it is a generated artifact).

```python
    # Each chunk already ends with "\n"; the join leaves exactly one trailing
    # newline. Do NOT add another — the end-of-file-fixer pre-commit hook
    # strips a double trailing newline, which would desync the golden from
    # this renderer and break the CLI-HELP gate.
```
— `tools/parity/lib/cli_help.py`, `render_all_help`

### 11.7.4 REINDEX-DUMP

The SQLite index is a PURE projection of the canonical files
(07-runtime-and-durability.md §7.1). This gate drives the deterministic
racing mock evolve, rebuilds the index with `rebuild_index`, dumps it to
stable text via `iterdump` (deterministic order, every row a literal
`INSERT`), normalizes wall-clock/date/uuid noise, and asserts byte-identity.
The dump is the index's FULL contents — schema DDL + every row — so a
refactor that changes which rows the projection produces, or any column
value, moves these bytes. **Reds legitimately** when the projection changes
(a new ingest column, a changed row). **Update:** `ZICATO_PARITY_UPDATE=1`.

### 11.7.5 MOCK-GOLDEN (eight lanes)

The strongest end-to-end gates. Each runs a deterministic, no-live-LLM mock
evolve of the real `target_1_presentation` contract and freezes the EXACT
serialized bytes of every decision artifact — `gen_score.json`,
`experiment.json`, any `loss.json`, each round's `round_log.jsonl`, each
settled `tournaments/field-*.json` snapshot (which carries the round's
recorded `promoted_generation_id` / `champion_generation_id`), and
`lineage.json` — after masking wall-clock noise. Together they exercise the
full orchestrated path: propose N challengers, apply real patches against
real markers, run the rungs + cuts, crown through the champion gate,
confirm on the holdout, persist the audit.

**Why eight.** One capture pins one configuration, and the evolve round
branches on three axes that select different code: the tournament
structure the frozen contract declares, the runtime mode, and how many
rounds the invocation runs. Each lane has its own golden and its own gate,
so a red names the configuration that moved:

| Gate | Structure · mode · rounds | What only this lane executes |
| --- | --- | --- |
| `MOCK-GOLDEN` | racing field 4 · full · 1 | the multi-challenger rungs, cuts, and crowning duel |
| `MOCK-GOLDEN-GAUNTLET` | gauntlet · full · 1 | the `field_n == 1` full-board selector, and the crowning holdout a single-challenger full round still runs |
| `MOCK-GOLDEN-GAUNTLET-FAST` | gauntlet · fast · 1 | the one configuration that deliberately SKIPS the crowning holdout (`field_n == 1 and fast_mode`) |
| `MOCK-GOLDEN-RACING-FAST` | racing field 4 · fast · 1 | every rung resolving both competitors through the unit cache |
| `MOCK-GOLDEN-TWO-ROUND-RACING` | racing field 4 · full · 2 | the between-round carry-over: the promoted head advancing off the seeded `v0`, the crowned generation defending the next round, that generation's patched snapshot supplying the next round's mutable surface, round directories numbering on from `0`, and round 1's settled snapshot naming round 0's winner as its champion |
| `MOCK-GOLDEN-SWISS` | swiss field 4 · full · 1 | fixed-round pairings over champion + challengers, Copeland standings, and the leader's final champion-gate confirmation |
| `MOCK-GOLDEN-SINGLE-ELIM` | single_elim field 4 · full · 1 | challenger-vs-challenger bracket nodes (no incumbent, so the gate's preferred side wins), then the champion-vs-survivor final |
| `MOCK-GOLDEN-DOUBLE-ELIM` | double_elim field 4 · full · 1 | the losers' bracket second life and the grand final feeding the champion gate |

The last three structures are the ones the unified round pipeline reaches
through registries that no other lane touches end to end; their contracts
live beside the racing one as `scoring.swiss.json`,
`scoring.single_elim.json`, and `scoring.double_elim.json` in
`examples/zicato_examples/target_1_presentation/`.

Every lane drives `evolve_n_rounds`, single-round lanes included: at
`rounds=1` the loop's persisted artifacts are byte-identical to a bare
`evolve_once`, so the round count is an ordinary lane parameter rather than
a second code path.

The two fast lanes pre-seed the champion's per-board `loss.json` for every
replicate slot the contract requests and install the PERSISTING reducer
stub. Without both, every cache read raises, fast mode degrades to full, and
the lane would capture the full-mode path under a fast-mode name — the
goldens record `champion_eval_mode: "fast"`, which is the assertion that it
did not.

`tools/parity.sh` selects a lane with `pytest -k <lane name>`, so no lane
name may be a substring of another. The lane table lives in
`tools/parity/lib/mock_evolve_capture.py`; adding a lane there and a
`_mock_golden_lane` line in `tools/parity.sh` is the whole wiring — plus,
for a structure the example does not yet declare, a `scoring.<structure>.json`
beside the others in the example directory.

The capture below is written for the racing lane, and generalises over all
three axes:

```python
"""Deterministic mock-evolve capture for the parity oracle (MOCK-GOLDEN gate).
...
Unlike the unit suite, this exercises the full orchestrated path —
propose N challengers off v0, apply the real proposer patches against the
real mutation markers, run the racing rungs + cuts on board slices, crown
a survivor through the champion gate, and persist the whole audit — and
freezes the EXACT serialized bytes of every decision artifact. A refactor
that changes any loss, any scalar, any decision, any id, any structural
field, or any serialization detail moves these bytes and fails the gate.
"""
```
— `tools/parity/lib/mock_evolve_capture.py` (module docstring)

**Reds legitimately** when any loss / scalar / decision / id / structural
field / round-log event / serialization detail changes. **Update:**
`ZICATO_PARITY_UPDATE=1` (all lanes), or `-k <lane>` for one.
The capture lives OUTSIDE `tests/`, so that conftest's autouse fixtures do
not fire; it replicates the two it needs — pinning the default proposer to
the text shim and neutering the harmonograf launch — so the captured
behaviour matches what the unit suite asserts.

### 11.7.6 The masking discipline (why goldens don't flap)

`normalize.py` masks the handful of fields that are wall-clock / host-path /
date-stamped / random-uuid by construction — timestamps → `<TS>`, the
date-prefixed epoch id → `<DATE>`, a `uuid4().hex` patch id → `<HEX32>`, the
tmp root → `<TMP>`. The masking is NARROW by design: only fields
known-nondeterministic by construction are touched, so a refactor that
silently changes a REAL field still surfaces as a diff:

```python
"""Shared normalization for parity goldens.
...
The masking is deliberately narrow: only fields that are known to be
non-deterministic by construction are touched. A refactor that silently
changes a real field will still surface as a diff.
"""
```
— `tools/parity/lib/normalize.py` (module docstring)

### 11.7.7 MYPY

A count rather than a golden. `mypy src/zicato/` must produce no MORE
`error:` lines than the committed baseline (a refactor should REDUCE it).
**Update:** `--update` writes the current count as the new baseline — do this
only when you have LEGITIMATELY reduced errors, never to paper over a
regression.

### 11.7.8 Never bake a sibling change into a golden

A golden re-capture is a CLAIM that the new bytes are correct. That claim is
only reviewable if the re-capture contains ONLY the change under review.

> ⛔ NEVER run `bash tools/parity.sh --update` with unrelated working-tree
> changes staged, and never re-capture a golden without stating the
> behavioural reason in the commit. A re-capture that also picks up a sibling
> refactor's byte-shift silently launders an unreviewed change into the
> baseline — the next person sees a green gate and trusts a golden nobody
> vetted. Re-capture the ONE gate your change legitimately moved
> (`--only MOCK-GOLDEN`), review the diff, and commit the golden WITH the code
> that justifies it (the parity-green-on-unchanged-behaviour rule).

> ⚠️ TRAP — a RED parity gate is INFORMATION to read before it is a chore.
> A CONTRACT-HASH red
> means an operator's epoch would spuriously roll; a MOCK-GOLDEN red means a
> loss / scalar / decision moved; a REINDEX-DUMP red means the index
> projection changed. Read the diff before you reach for `--update` — the
> question is always "is the new behaviour correct?", and only if the answer
> is a justified yes do you re-capture.

---

## 11.8 The seven import contracts + the TID251 bans

Two static gates keep the architecture from eroding: the import-linter
library/driver contracts (`uv run lint-imports`) and the ruff TID251
banned-api list. Neither is a pytest test — a violation reds the linter, so
they run in `make check` and CI.

### 11.8.1 The seven import contracts

zicato is a LIBRARY first — the surface in `zicato/__init__.py` — with three
DRIVERS on top: `zicato.cli`, `zicato.dashboard`, `zicato.builder`. The
contracts pin which edges exist:

| # | Contract | Forbids |
|---|---|---|
| 1 | the library must not import the drivers | every lib package (`core`, `epoch`, `evolve`, `orchestrator`, `proposer`, `query`, `runtime`, `selection`, `tournament`, `storage`, …) importing `cli` / `dashboard` / `builder` |
| 2 | dashboard driver: no import of the cli | `zicato.dashboard` → `zicato.cli` (the `dashboard → builder` mount is the ONE allowed dashboard→driver edge) |
| 3 | builder driver: no import of the other drivers | `zicato.builder` → `cli` / `dashboard` |
| 4 | cli driver: no DIRECT import of the builder | `zicato.cli` → `zicato.builder` directly (`allow_indirect_imports = true` — the cli reaches the builder legitimately via `cli → dashboard.server → builder.api`) |
| 5 | the query layer stays dashboard-free | `zicato.query` → `zicato.dashboard` (the query-layer-is-library-code rule — 09-dashboard-and-query.md §9.1, doctrine `DQ4`) |
| 6 | tui driver: no import of the other drivers | `zicato.tui` → `cli` / `dashboard` / `builder` (the terminal console speaks HTTP to the served payloads) |
| 7 | the proposer's patch validator has no path to the board | `zicato.proposer`'s validator reaching the board loader, which is what keeps entry text out of the validator's import closure |

The declared driver→driver edges are exactly two: `cli → dashboard` (the CLI
launches the server and resolves its static bundle) and `dashboard →
builder` (server.py mounts the builder's REST routes). Everything else is
forbidden. Contract 4 is the subtle one — it forbids the cli growing its OWN
builder dependency while permitting the transitive reach through the two
declared edges:

```
name = "cli driver: no direct import of the builder (cli -> dashboard is the declared edge)"
...
# Direct only: the cli legitimately reaches the builder TRANSITIVELY
# through the two declared edges (cli -> dashboard.server -> builder.api
# mount); what this contract forbids is the cli growing its own builder
# dependency.
allow_indirect_imports = true
```
— `pyproject.toml`, `[[tool.importlinter.contracts]]`

### 11.8.2 The TID251 bans — retired private reaches

A set of cross-module helpers live at public seams on their home modules.
The TID251 (flake8-tidy-imports banned-api) list rejects any import of the
retired underscore paths, and each ban names the replacement so a reader of
the violation knows the fix:

```
[tool.ruff.lint.flake8-tidy-imports.banned-api]
"zicato.orchestrator._compute_field_diversity".msg = "moved: use zicato.selection.diversity.compute_field_diversity"
"zicato.storage._atomic.read_json".msg = "use the public face: from zicato.storage import read_json"
"zicato.analyzer.aggregator._to_snake".msg = "private to the aggregator: use zicato.query.paths.to_snake"
...
```
— `pyproject.toml`, `[tool.ruff.lint.flake8-tidy-imports.banned-api]` (excerpt)

The storage package owns its private `_atomic` module, so its own modules
(and the `_atomic` unit test) import it directly via a per-file TID251
ignore; everyone else goes through the public `zicato.storage` face.

### 11.8.3 Reading a violation

A `lint-imports` failure names the contract and the offending import chain;
a TID251 failure names the banned symbol and its `.msg` (the move
instruction). Both tell you the fix directly.

> ⛔ NEVER "fix" an import-contract or TID251 failure by loosening the
> contract or deleting the ban. The failure is telling you a NEW edge would
> break the architecture (the contracts-are-lint rule) — a lib package has
> started importing a driver, or a retired private path has regrown. The fix
> is on YOUR side: move the shared code
> to a public seam (the ban's `.msg` names it), or invert the dependency.
> Editing `pyproject.toml` to permit the edge is editing the architecture, and
> that is a design decision rather than a lint fix.

> ⚠️ TRAP — pre-commit lints only CHANGED files, but CI runs `ruff check .`
> and `lint-imports` over the WHOLE tree. A cross-module edge you add can pass
> your local pre-commit (it only saw your one file) and red in CI (which sees
> the contract over the whole graph). Run `make import-lint` and `uv run ruff
> check .` before pushing a structural change. A `known-first-party` isort
> mismatch was found this way: the local pre-commit missed it and CI's
> repo-wide check caught it.

---

## 11.9 Node behaviour-suite conventions

The dashboard JS has its own behaviour suite under
`src/zicato/dashboard/static/test/`, run by `make node-test` (and mirrored by
the `node`-marked `tests/test_dashboard_js.py` shim, excluded from the
default pytest run). The conventions are the enforcement arm of the
digest-gated rendering spec (09-dashboard-and-query.md §9.7).

### 11.9.1 Verify by exit code

The runner aggregates every `*.test.mjs`, but each file prints its OWN
"X passed" line, so the FINAL printed line is the LAST file's count rather
than the grand total. The real signal is the PROCESS EXIT CODE:

```javascript
// FOOTGUN THIS GUARDS AGAINST: each file's harness prints its own
// "X passed, Y failed" line, so the FINAL printed line is just the LAST file's
// count — NOT the grand total. A green-looking tail can hide a failing file. The
// real signal is the PROCESS EXIT CODE (0 = all green, 1 = something failed); the
// "TOTAL:" line below makes the aggregate honest and visible too. Verify success
// by EXIT CODE (`echo $?`), never the tail line.
```
— `src/zicato/dashboard/static/test/run-all.mjs`

The runner gives every file a fresh worker and combines their reports into an
honest `TOTAL:`. This isolation is load-bearing: render modules retain small
digest caches, so importing every file into one module graph lets an earlier
fixture change a later test. The parent alone sets the aggregate exit status.

The Traces termination pin uses a worker with a hard timeout too. It therefore
tests a fresh page-sized module graph without depending on permission to spawn
an operating-system process.

> ⚠️ TRAP — a green-looking tail line can hide a failing FILE. `make node-test`
> is the canonical run and it propagates the exit code; if you ever run
> `node run-all.mjs` by hand, check `echo $?` rather than the last line. This
> is the exit-code-is-the-node-signal rule, and reading the tail instead is
> how a broken suite ships behind a green-looking line.

### 11.9.2 The digest / no-op / DOM-identity assertions

Every live-surface node test pins the render discipline the same way (the
full spec is 09-dashboard-and-query.md §9.7.5): a `*Digest` function folds
identical payloads identically and flips on a change; a controller keeps DOM
NODE IDENTITY across an identical re-serve and rebuilds only on a genuine
advance. `pipeline_stepper.test.mjs` and `seq_render_gate.test.mjs` are the
models — the latter is the render-discipline backbone (the `noteProgress`
cursor, the `core/sse.js` seq skip gate, the four run-states, the chrome
pill's zero-DOM no-op beat).

### 11.9.3 The mock_server parity pin

The server computes the round-timeline and racing-field joins
(09-dashboard-and-query.md §9.2.5), while the node fixtures describe
workspaces in terms of the granular endpoints. `test/mock_server.mjs`
therefore PLAYS THE SERVER, deriving those two served payloads from a
fixture map the way the Python readers do. Its own rule: any divergence is a
bug in the mock, never grounds to re-derive in prod:

```javascript
// It is TEST-ONLY scaffolding — nothing
// under js/ imports it — and any behavioural divergence from the Python
// readers is a bug in THIS file, never grounds to re-derive in prod code.
```
— `src/zicato/dashboard/static/test/mock_server.mjs`

The Python side of that pin is `tests/test_dashboard_racing_and_rounds.py`,
which asserts the real readers produce what the mock mirrors. When you change
`build_round_timeline` / `build_racing_field`, update `mock_server.mjs` to
match (09-dashboard-and-query.md §9.16, step 3) — the mock is a parity
witness rather than a second implementation.

> ⛔ NEVER re-derive a served join in prod JS to make a node test pass. If the
> mock and the prod client disagree, the mock is wrong (the
> exit-code-is-the-node-signal rule) — it exists to prove the client reads the
> server's answer, and never to license the client to compute its own. Fixing
> the divergence in `mock_server.mjs` (to mirror the Python reader) is the
> correct move; re-deriving in `views/*.js` re-opens the client/server
> drift the served join closes (the server-computes-client-renders rule —
> 09-dashboard-and-query.md, doctrine `DQ1`).

The mock mirrors THREE served joins: round-timeline, racing-field, and the
elim `gen_states` fold (`attachElimStates`, mirroring `derive_elim_states` —
09-dashboard-and-query.md §9.2.5). The elim mirror's Python parity witness is
the shared `tests/data/elim_states_fixture.json`, asserted by BOTH the Python
`derive_elim_states` and the Rust `elim_states.rs` fold.

### 11.9.4 The console test-file map, by view

The console's behaviour tests are grouped by dominant view into ten files,
with the shared preamble (the `FIXTURE` map plus the `freshHb` /
`installFetch` / `allByClass` helpers) in `fixtures.mjs`:

| file | covers |
|---|---|
| `variant_t_shell.test.mjs` | the chrome — tree / crumbs / status pill / containment |
| `variant_t_structure.test.mjs` | the tournament-structure figures + standings |
| `variant_t_candidate.test.mjs` | the candidate dossier |
| `variant_t_epoch_scoping.test.mjs` | cross-epoch scoping (the fleet + per-epoch reads) |
| `variant_t_figures.test.mjs` | the figure grammar (heatmaps / bars / radars) |
| `variant_t_home_epoch.test.mjs` | home + epoch views |
| `variant_t_lifecycle_dag.test.mjs` | the mutation surface + lifecycle DAG |
| `variant_t_live.test.mjs` / `variant_t_live_hero.test.mjs` / `variant_t_live_waves.test.mjs` | the SSE-driven live hero / ticker / funnel transitions |

`digest_opts.test.mjs` pins the `digestOpts` rules directly — functions are
dropped, key order is irrelevant, a non-integer number rounds to three
decimal places, and a non-finite number folds to `null`. `bracket.test.mjs`
pins six bracket-topology tests on the served `gen_states` fixtures. When you
split or rename a node test file, the runner (`run-all.mjs`) globs
`*.test.mjs` so it needs no registration — but grep the split target for the
assertion you rely on; the grouping is by DOMINANT view and a few assertions
cross seams.

---

## 11.10 CI

`.github/workflows/ci.yml` runs one job per gate family. The Python job
matrixes over 3.11 and 3.12 and runs the gates in order: ruff → import
contracts → mypy → pytest. Dependencies install with `--frozen` (exactly
what `uv.lock` pins, failing if the lock is stale — reproducible CI):

```yaml
      - name: Sync dependencies
        run: uv sync --all-extras --frozen
      - name: Ruff
        run: uv run ruff check .
      - name: Import contracts
        run: uv run lint-imports
      - name: Mypy
        run: uv run mypy src/zicato/
      - name: Pytest
        run: uv run pytest tests/ tools/test_prose_lint.py
```
— `.github/workflows/ci.yml`

The Rust job builds and tests the supervisor: `cargo fmt --check`,
`cargo clippy --all-targets -- -D warnings`, `cargo test`. A change that
touches a two-language contract (the index schema, the runtime state serde,
the `_is_safe_id` / `to_snake` / start-time twins) must pass BOTH of those
jobs — CI is where the Python/Rust parity is enforced end to end. Two more
jobs need no dependency install: the line-budget check and the prose gate,
both plain-`python` runs of a stdlib-only tool.

> ⚠️ TRAP — CI runs `ruff check .` and `lint-imports` over the WHOLE tree and
> installs with `--frozen`. Two failure modes bite here that a local `make
> test` misses: a whole-tree lint/import violation your changed-files
> pre-commit did not see (§11.8.3), and a stale `uv.lock` (a dependency edit
> that did not re-lock reds `--frozen`). Run `uv sync --all-extras` (never bare
> `uv sync` — it deletes the dev tooling) and `make check` before pushing.

---

## 11.11 The pre-commit checklist

Copy-paste this before a nontrivial commit. The eleven steps run in order:
the fast lane for quick signal, the full suite, format and lint, types,
import contracts, the parity gates, the node suite, the two oracles, the
Rust supervisor, the line budgets, and the vendor scan. Each step is a gate
a real regression could hide behind.

```bash
# 1. Fast lane — quick signal while you iterate.
uv run pytest tests/ -m "not slow and not node" -q

# 2. Full suite — the default; runs the slow/subprocess/server tests.
uv run pytest tests/ -q

# 3. Format + lint (whole tree, the way CI does).
uv run ruff format . && uv run ruff check .

# 4. Types.
uv run mypy src/zicato/

# 5. Import contracts (library/driver boundaries + TID251).
uv run lint-imports

# 6. Parity gates — read any RED diff before you --update.
bash tools/parity.sh

# 7. Node behaviour suite — verify by EXIT CODE, not the tail line.
make node-test ; echo "node exit: $?"

# 8. The two oracles, explicitly (they ride in step 2, pin them here too).
uv run pytest tests/test_convergence_known_answer.py \
    tests/test_decision_procedure_power.py -q

# 9. Rust supervisor (if you touched a two-language contract).
cargo test -p zicato-supervisor

# 10. Simplification budgets — reports language and subsystem totals too.
python tools/line_budget.py --check

# 11. Vendor scan — nothing in git may reference the model vendor (the
#     durable repo rule). Scan the staged diff for the vendor's name and any
#     product / model identifiers; VENDOR must be your local pattern, kept
#     out of the tree. The diff must be clean.
git diff --cached | grep -riE "$VENDOR" && echo "VENDOR LEAK" || echo "clean"

# 12. Prose gate — no new hidden-context constructions in docs, README,
#     docstrings, or comments (ratchet against the committed baseline).
python tools/prose_lint.py --baseline tools/prose_lint_baseline.json
```

`make check` collapses steps 3–5 + 2 + 7 into one target
(`lint import-lint typecheck test node-test`, parallelizable with `-j5`);
run the parity gates and oracles alongside it.

Treat `RuntimeWarning`, unclosed-resource output, and pending-task destruction
as failures even when pytest exits zero. For server lifecycle changes, repeat
the focused serial test with `-W error::RuntimeWarning`; parallel success alone
can hide teardown races. A server thread owns its event loop through shutdown:
after the application stops, cancel and gather remaining tasks, shut down async
generators, then close the loop. On Python 3.11, also close a coroutine when an
already-closing task group rejects it; otherwise garbage collection reports a
false-clean server exit as an un-awaited coroutine.

> ✅ ALWAYS run `uv sync --all-extras` (never bare `uv sync`) when your
> environment might be stale — the all-extras sync rule (`01-orientation.md`
> §4). Bare `uv sync` in zicato DELETES the dev tooling
> from `.venv` — pytest, mypy, ruff, even uv itself — because they live in the
> `dev` extra. A green checklist run on a `.venv` missing half its tools is a
> false green.

### Line-budget gate

Run the report and enforcement from the repository root:

```bash
python tools/line_budget.py
python tools/line_budget.py --check
python tools/line_budget.py --ref f9052dd
```

`measure()` walks `git ls-files` for the worktree or `git ls-tree` for a
reference and counts newline bytes, matching `wc -l`. `_excluded()` owns the
narrow Markdown, lockfile, and generated-artifact exclusions; `_production()`
owns the runtime subtotal. The report groups both by language and subsystem so
movement is reviewable.

`.line-budget.json` contains hard limits without an allowance. Keep the two
independent one-line-overage assertions in `tests/test_line_budget.py`: one
proves total `limit + 1` fails while production is unchanged; the other proves
production `limit + 1` fails while total is unchanged. Run:

```bash
uv run pytest tests/test_line_budget.py -q
python tools/line_budget.py --check
```

The stable measurement contract, final arithmetic, and ratchet policy live in
`docs/design/LINE-BUDGET.md`; implementation mechanics live only here.

### Prose gate

Repository prose is read by people who have the tree and none of its
development history. `tools/prose_lint.py` reports six constructions that need
that history to decode: invented short labels used as vocabulary, wording about
what the tree stopped doing, a subject defined by contrast with an absent
alternative, adverbs asserting conviction in place of information, a trailing
verb with no subject or result, and an issue number standing where a statement
belongs. The tool's docstring states each rule and its reason in full;
`tools/test_prose_lint.py` pins one hitting and one clean sentence per rule, so
a widened pattern that starts swallowing ordinary sentences turns red.

It reads the Markdown, and the Python docstrings and comments, under seven
roots — `CHANGELOG.md`, `README.md`, `docs/`, `examples/`, `skills/`,
`src/zicato/`, and `tools/` — which is every tree whose prose a reader is
expected to act on. Two paths under those roots are skipped, because a hit in
either is owned somewhere else and cannot be fixed where it appears:
`docs/design/CLI.md` is generated from `zicato --help`, so its text is help
literals owned by the command definitions, and the captured bytes under
`tools/parity/golden/` are a record of what a run produced. JavaScript comments
are out of scope, so the dashboard client is unread.

Fenced blocks and backtick spans are quoted material and are masked before
matching, so a document may cite an identifier freely. Python strings other
than docstrings are out of scope. Layer numbers inside standard collocations (a
squared norm, a load balancer or a processor cache at a numbered level) pass
through an allowlist. The adverb rule reports at severity `review` and never
fails a run on its own.

```bash
python tools/prose_lint.py                        # report; fails on any hit
python tools/prose_lint.py --rule codename-label  # one rule
python tools/prose_lint.py --baseline tools/prose_lint_baseline.json
python tools/prose_lint.py --write-baseline tools/prose_lint_baseline.json
```

CI runs the third form. `tools/prose_lint_baseline.json` holds one count per
rule and the run fails only where a count rises above it, so the check guards
the tree while the standing backlog is worked through.

A cleanup change leaves the baseline file alone. Lowering a count is always
green against a higher ceiling, so a prose-fixing branch touches only the prose
and never contends for the shared file — several such branches can be in flight
at once without conflicting. Once they have merged, one change of its own
regenerates the file with the fourth form and lands it, ratcheting every
ceiling down to the measured floor. Regenerate on a tree that is current with
`main`: the counts are whole-tree totals, so a baseline captured before someone
else's merge can record a floor the merged tree fails to meet.

Where a token is quoted from an external system and has to stay, waive it in
place with `prose-lint: allow <rule-id>` on the offending line or the line
above it (several ids may be listed, or `all`). A waiver claims the reader can
decode the token without the repository's history; state that reason beside it.

---

## 11.12 The two hard rules

Two disciplines govern every test change. They are the difference between a
suite that catches regressions and one that rubber-stamps them.

### 11.12.1 A regression test must fail with the fix stashed

A test written to lock a bug fix is only a regression test if it FAILS
against the buggy code. A test that passes both before and after the fix
proves nothing about the fix — it may be asserting something the bug never
touched.

> ⛔ NEVER commit a "regression test" without first proving it fails with the
> fix reverted. `git stash` the fix (or check out the parent commit's source
> for the fixed module), run the new test, and SEE IT RED. Then restore the
> fix and see it green (the must-fail-with-the-fix-stashed rule). A test you
> never watched fail is a test you cannot
> trust to catch the regression's return — and the whole point of a casebook
> regression test (§11.15) is that it catches the return.

### 11.12.2 Never weaken an assertion — pin or justify

When a test goes red, there are exactly two honest responses: fix the code,
or — if the new behaviour is CORRECT — update the assertion to the new value
WITH a measured justification. Loosening an assertion (widening a tolerance,
deleting a check, changing `==` to `>=`) to make red go green is destroying
the test's coverage.

> ⛔ NEVER weaken an assertion to make a test pass. A pinned number
> (`EXPECTED_FLOOR == 1.2`, a power-harness rate, a golden byte) moves ONLY
> with a measured reason stated in the commit (the never-weaken-an-assertion
> rule). If the convergence oracle
> reds, either the loop broke (fix it) or the scalar formula legitimately
> changed (re-derive `EXPECTED_FLOOR`, state the derivation, move the pin). If
> a power-harness rate reds, re-derive it from the seeded model and justify it.
> Widening a tolerance to absorb a real shift is how a suite slowly stops
> testing anything.

> ⚠️ TRAP — "the test is flaky" is almost never the reason to loosen it in
> this suite. The convergence oracle is a hand-computed constant; the power
> harness is seeded and deterministic; the parity goldens mask only
> known-nondeterministic fields. A "flake" in one of those is a real
> nondeterminism you introduced (an unseeded RNG, a wall-clock in a digest, a
> leaked pin — §11.2.1) rather than statistical noise. Find it; do
> not paper over it with a wider assertion.

---

## 11.13 The reader parity harness — a byte-identical-except-ordering oracle

`tests/_reader_parity_harness.py` is the model for a whole class of test:
the **snapshot oracle** for a refactor/migration. It builds a deterministic
multi-epoch fixture, captures EVERY public `build_*` reader response into one
canonical-JSON snapshot, and lets you assert "nothing observable moved"
across a change to the readers — the same discipline as the MOCK-GOLDEN
parity gate (§11.7.5), applied to the query readers.

The fixture is built to expose one defect class: epoch ordering, where
directory-name order disagrees with `created_at` order. It also carries an
empty epoch:

```python
# Chronological (created_at) order — the canonical/correct order:
#     e1 (Jan)  ->  e2 (Feb, EMPTY)  ->  e0 (Mar)
#
# Numeric/name order — the WRONG order the buggy sites produce:
#     e0  ->  e1  ->  e2
#
# ``e0`` is the bug mirror: its name sorts FIRST but it was created LAST.
# ``e2`` is the empty epoch (no generations).
```
— `tests/_reader_parity_harness.py`

`capture_snapshot(ws)` calls every public reader — workspace-wide
(`build_workspace_view`, `build_epochs_summary`, `build_lineage_view`,
`build_meta_loop_ledger`, `build_snapshot`, `build_environment`, …),
per-epoch (`build_epoch_view`, `build_bracket`, `build_score_trajectory`,
…), and per-generation (`build_matchup_detail`, `build_gate_breakdown`, …)
— so the snapshot exercises the leaf path-readers as well as the
enumerations, and freezes the whole read surface in one diffable document.

### 11.13.1 The split: byte-identity vs order-aware equality

A change to epoch ordering legitimately moves ONE thing and must move
NOTHING else, so the harness splits its assertions. Every NON-epoch-list
response must be BYTE-IDENTICAL. Every epoch-list response must carry the
same SET of epochs with identical per-epoch content, in the canonical
timestamp-first order:

```python
# The labels whose epoch ordering the fix corrects. For these the harness
# asserts SET + per-epoch-content equality (order-independent) and that the
# epoch order now equals the canonical ``list_epoch_ids`` order; for every
# other label it asserts byte-identity against the golden.
EPOCH_LIST_LABELS = frozenset(
    {
        "workspace_view",
        "epochs_summary",
        "lineage_view",
        "meta_loop_ledger",
    }
)
```
— `tests/_reader_parity_harness.py`

`epoch_order_of(label, value)` extracts the epoch order each response
presents (the `epochs[].epoch_id` list, or the first-appearance order in the
lineage generation list) so the harness can assert it equals the canonical
`list_epoch_ids` order — the intended fix, and nothing more.

### 11.13.2 The masking discipline — narrow, or you weaken a check

Like every snapshot oracle, it masks ONLY the fields that are
non-deterministic by construction, and the docstrings state the boundary —
the response-stamp `generated_at` is masked, but on-disk-derived
timestamps (`created_at`/`proposed_at`) are deterministic in the fixture and
are NOT masked:

```python
def mask_volatile(value: Any) -> Any:
    """Recursively replace wall-clock noise keys with a constant.

    Keeps the snapshot reproducible across the capture/compare boundary
    without weakening any structural / ordering check.
    """
```
— `tests/_reader_parity_harness.py`, `mask_volatile`

`_normalize_root` collapses the per-run absolute workspace path to `<ws>` so
path-bearing responses (`environment.workspace`) compare stably. The rule is
identical to `tools/parity/lib/normalize.py` (§11.7.6): mask the KNOWN
non-determinism narrowly, so a REAL field change still surfaces as a diff.

> ✅ ALWAYS reach for a snapshot oracle when you refactor a family of pure
> readers (the query layer, a serializer, a canonicalizer): capture every
> response BEFORE, refactor, capture AFTER, assert byte-identity except for
> the ONE thing you meant to change (which gets its own order-aware / value-
> aware assertion). It is the cheapest proof that a large mechanical change
> is behaviour-preserving, and it is what CONTRACT-HASH / MOCK-GOLDEN /
> REINDEX-DUMP do for the whole system.

> ⚠️ TRAP — a snapshot oracle is only as honest as its masking is narrow. Mask
> a field that CAN carry a real change (e.g. blanket-masking every `*_id`
> because some are random) and you blind the oracle to the regression it
> exists to catch. Mask the SPECIFIC known-nondeterministic keys
> (`generated_at`, the tmp root, a `uuid4().hex`), never a broad pattern.

---

## 11.14 The conformance-suite pattern — one contract, every backend

Two suites — the generation store (§11.6) and the storage backends — share a
design pattern worth naming:
a **cross-backend conformance suite** parametrised on a backend axis, so a
new backend is a one-line registration and the whole contract is asserted
against it automatically. `tests/test_storage_conformance.py` is the model:

```python
"""Cross-backend conformance suite for :class:`StorageBackend`.

Every backend in :mod:`zicato.storage` must round-trip the same operations
with the same observable semantics. This module is the canonical contract:
a backend that passes every test here is a drop-in for any zicato domain
routed through the storage seam.

Adding a third backend (the v0+1 git backend) is a one-line change —
append a :class:`BackendSpec` to ``BACKENDS`` describing how to build a
started backend for the test; the parametrised ``backend`` fixture does
the rest.
"""
```
— `tests/test_storage_conformance.py` (module docstring)

The registration is a `BackendSpec` list and one parametrised fixture that
`start()`s / `close()`s the backend for each test:

```python
BACKENDS: list[BackendSpec] = [
    BackendSpec(name="memory", build=lambda _tmp: make_storage_backend("memory")),
    BackendSpec(
        name="files",
        build=lambda tmp: make_storage_backend("files", root=tmp / "ws"),
    ),
]


@pytest.fixture(params=BACKENDS, ids=lambda b: b.name)
def backend(request, tmp_path: Path):
    spec: BackendSpec = request.param
    b = spec.build(tmp_path)
    b.start()
    try:
        yield b
    finally:
        b.close()
```
— `tests/test_storage_conformance.py`

Every test takes `backend` and asserts an observable semantic — a missing
record reads `None`, a write-then-read round-trips, a write replaces the
prior value, the atomic-write contract holds (07-runtime-and-durability.md
§7.3). The `StorageBackend` contract that the file backend, the in-memory
backend, and the planned git backend must ALL satisfy is this file.

The generation-store conformance suite (§11.6) is the same pattern over a
DIFFERENT seam — parametrised on `{directory, git}`, asserting the
`GenerationStore` protocol. Both are the durability-side counterpart to the
reader-side snapshot oracle (§11.13): the snapshot oracle freezes ONE
implementation's whole output, while a conformance suite proves several
implementations share ONE observable contract.

> ✅ ALWAYS add a backend/implementation to its conformance suite's registry
> (`BACKENDS` / `_BACKENDS`), never to a single-backend test file. The whole
> value is that the contract is asserted against every implementation
> automatically — a new backend that reds one conformance test is a backend
> that is not yet a drop-in. This is how the git generation store was held to
> the exact contract the directory backend already met
> (07-runtime-and-durability.md §7.4), and how the stale-worktree case would
> have been caught.

> ⛔ NEVER assert a backend-SPECIFIC behaviour in the conformance suite (a git
> tag name, a directory layout). The conformance suite pins the SHARED
> observable contract; a backend's private details belong in its own file
> (`tests/test_epoch_genstore.py` for the directory backend). Mixing them
> makes the conformance suite fail for a second backend that is perfectly
> correct but implements the shared contract differently.

---

## 11.15 Recipe: write a regression test for a bug (the casebook template)

Every bug in 12-bug-casebook.md has a regression test that fails with the
fix stashed. This is the template. Worked scenario: the client champion-scan
case (`12-bug-casebook.md` case 4), in which the server picked the first
promoted generation instead of the reigning, last-promoted one.

**Step 1 — Reproduce the bug in a test that FAILS on the buggy code.**
Write the assertion for CORRECT behaviour first, against a fixture that
distinguishes right from wrong. For the champion-scan case the
distinguishing fixture is a TWO-promotion lineage; a single-promotion lineage
reads identically either way (09-dashboard-and-query.md §9.2.1):

```python
def test_current_champion_is_the_reigning_not_the_first_promotion(tmp_path):
    # A lineage that promotes TWICE — v1 then v3. The bug returned v1
    # (first promoted); the fix returns v3 (reigning = last promoted).
    workspace = _fixture_with_two_promotions(tmp_path, promoted=["v1", "v3"])
    view = build_epoch_view(WorkspacePaths(workspace / ".zicato"))
    assert view["current_champion"] == "v3"   # reigning, not "v1"
```

**Step 2 — Prove it fails with the fix reverted.** `git stash` the fix (or
check out the pre-fix source for `_current_champion`), run the test, SEE IT
RED (it returns `"v1"`), then restore the fix and see it green. If it passes
on the buggy code, your fixture does not distinguish the bug — a
single-promotion lineage would do that. Fix the fixture until the
test discriminates.

**Step 3 — Choose the right home.** A bug lives in the suite that owns its
subsystem, at the layer the bug lives at:

- a cross-backend durability bug → `tests/test_genstore_conformance.py`
  (both backends), NOT a single-backend file (§11.6);
- a knob-ON bug that a deterministic contract would hide → a test whose
  SUBJECT is the knob ON, at the default (§11.3.1) — the tree-mismatch cases
  live in `tests/test_best_of_n_tree_integrity.py` at `best_of_n=3`;
- a two-language contract bug → both the Python test AND
  `cargo test -p zicato-supervisor`;
- a reaper / process-hygiene bug → `tests/test_conftest_dashboard_reaper.py`
  with a faked `ps` table (§11.2.3), never a real leak.

**Step 4 — Assert the ROOT invariant rather than only the symptom.** The
champion-scan case showed as a wrong champion in one payload. Its root is two
dashboard doctrines — server-computes-client-renders and the champion is the
reigning spine end (09-dashboard-and-query.md, doctrines `DQ1` and `DQ10`):
the server computes and the client renders, so the client must not re-derive;
and the reigning champion is the spine END. Assert the
invariant so the test catches the bug's return through a DIFFERENT surface as
well as the one that broke.

**Step 5 — Name it after the invariant it protects.** A name like
`test_current_champion_is_the_reigning_not_the_first_promotion` documents the
casebook entry, so a future reader greps the invariant rather than a ticket
number.

**Verify**

```bash
# Prove the discrimination:
git stash                                   # remove the fix
uv run pytest tests/path::test_name -q      # MUST be RED
git stash pop                               # restore the fix
uv run pytest tests/path::test_name -q      # MUST be GREEN
```

> ✅ ALWAYS pick the fixture that makes the bug DISCRIMINABLE — the
> two-promotion lineage for a first-vs-reigning bug, the multi-sample slate
> for a tree-mismatch bug, the concurrent-workspace `ps` table for a reaper
> bug. The single hardest part of a regression test is a fixture under which
> right and wrong differ; the assertion is the easy part. If you cannot make
> it fail on the buggy code, you have not reproduced the bug.

---

## 11.16 Recipe: add a test that spawns real workers

Some contracts can only be proven by a REAL subprocess (worker isolation,
budget escalation, config-crossing-the-boundary). These are `slow` /
`integration` tests whose runtime IS the coverage (the
full-suite-is-the-default rule). The discipline is
about staying bounded and leaving nothing behind.

**Step 1 — Make the worker's behaviour a module-level importable adapter.**
Under the dotted-path-callable rule, add a named adapter + `make_*` factory to
`tests/_subprocess_worker_support.py` (§11.5); its `worker_spec()` returns
the dotted `factory` path. A closure or monkeypatch WILL NOT cross the
process boundary — the child re-imports fresh.

**Step 2 — Shrink the budget and the grace so the test is FAST.** A
wedged-run test does not need a real 30-minute budget — it needs a budget
short enough that the escalation fires in the test's runtime. Pin a tiny
`wall_clock_budget_seconds` and a short SIGTERM→SIGKILL grace so the
escalation completes in seconds. The `_SleepingSession` sleeps 3600s so that
it OUTLASTS any test budget and forces the parent's `wait_for` + kill
escalation:

```python
class _SleepingSession:
    """A session whose ``run`` blocks the event loop far past any budget.

    Uses a *blocking* :func:`time.sleep` rather than :func:`asyncio.sleep`
    on purpose: a blocking sleep wedges the worker's event loop so its
    own cooperative ``asyncio.wait_for`` budget CANNOT fire. That is what
    forces the PARENT's ``wait_for`` + SIGTERM/SIGKILL escalation (and,
    in production, the supervisor) to be the layer that stops the run —
    exactly the wedged-run scenario the L3 layer exists for.
    """

    async def run(self, entry: Any, sink_path: Path) -> None:
        del entry, sink_path
        time.sleep(3600.0)
```
— `tests/_subprocess_worker_support.py`, `_SleepingSession`

The distinction between `_SleepingSession` (blocking sleep — forces PARENT
kill) and `_CooperativeSleepSession` (cancellable `asyncio.sleep` — the
worker SELF-aborts, exit 0) IS the test matrix: one proves the escalation
layer, the other proves the cooperative budget.

**Step 3 — Mark it `slow` / `integration`.** So the fast lane drops it and
the marker documents that its runtime is intentional. Never stub the
subprocess to speed it up — that deletes the coverage.

**Step 4 — Set a hard timeout and assert NO LEAK.** The test must bound its
own wait (the worker's budget + the parent's grace + a margin) so a genuine
hang fails the test rather than wedging the suite. And it must assert the
process, the temp checkout, and any child are gone at the end — the
`checkout_ephemeral` conformance tests are the model (`assert list(
_isolated_tempdir.iterdir()) == []` after cleanup; §11.6). A test that
leaks a real subprocess or a `ztw-snap-*` tree is a test that will flake the
NEXT test under xdist.

**Step 5 — Prove the boundary crossing rather than only the outcome.** If the
test is about something crossing INTO the worker (a pinned config flag), read
it back from INSIDE the worker. `ConfigProbeAdapter` writes the worker's
resolved `load_config()` view to `config_probe.json`, so the test proves the
value crossed via the args file with NO env var involved (§11.5). Asserting
the outcome alone can pass for the wrong reason.

**Verify**

```bash
# Run it in isolation, serially, with the real subprocess:
uv run pytest tests/test_your_worker.py -q -n0
# Confirm it's in the slow lane (the fast lane must SKIP it):
uv run pytest tests/test_your_worker.py -m "not slow and not node" -q   # deselected
# Confirm no leak: after the run, the OS temp dir has no ztw-snap-* left:
ls ${TMPDIR:-/tmp} | grep ztw-snap && echo "LEAK" || echo "clean"
```

> ⛔ NEVER spawn a real worker in a test without a hard self-timeout and a
> no-leak assertion. An unbounded wait on a wedged worker hangs the
> whole suite (and under xdist, a whole worker's shard); a leaked subprocess
> or `ztw-snap-*` tree flakes the next test. Bound the wait to
> budget + grace + margin, assert the temp dir is empty at the end, and
> reuse the `is_same_process` / process-group discipline of §11.2.3 for any
> signalling — the provenance-scoped-reaper rule applies to every test that
> touches real OS processes.

> ⚠️ TRAP — a blocking `time.sleep` and a cancellable `asyncio.sleep` test
> DIFFERENT layers and are not interchangeable. `time.sleep` wedges the
> worker's event loop so its cooperative budget CANNOT fire, forcing the
> PARENT's kill escalation; `asyncio.sleep` lets the cooperative budget cancel
> the run cleanly. Pick the one that exercises the layer you are testing — a
> test that means to prove the parent's SIGKILL path but uses `asyncio.sleep`
> proves the cooperative path instead and never touches the escalation.

---

## 11.17 Cross-references

- 03-contract-and-epochs.md §3.7 — what CONTRACT-HASH and the
  checkout-independence test (`12-bug-casebook.md` case 10) pin.
- 04-evaluation-statistics.md — the gate / replication / monotonicity-scope
  / noise-floor machinery the power harness (§11.4.2) characterizes; the
  train/holdout split the convergence oracle stays below.
- 05-proposer.md §5.6.5 — the tree/record agreement invariant the best-of-N
  integrity test (§11.3.1) proves at the default `best_of_n`.
- 06-tournament-and-selection.md — the evidence pre-gate the power harness
  drives; the racing structure the MOCK-GOLDEN capture and the convergence
  oracle's racing test exercise.
- 07-runtime-and-durability.md §7.1 (files canonical / index derived — why
  REINDEX-DUMP can drop-and-rebuild), §7.4 (the generation store and its git
  backend, the contract the conformance suite asserts),
  §7.13/§7.14 (the runtime-state / RoundLog round-trip test recipes).
- 08-supervisor.md — the Rust `cargo test` job and the two-language
  contracts (schema version, state serde, the `_is_safe_id` / start-time
  twins) CI enforces on both sides.
- 09-dashboard-and-query.md §9.7 (the digest-gated rendering spec the node
  conventions enforce), §9.16 (the payload-shape clean break that updates
  the `mock_server` parity pin + goldens).
- 12-bug-casebook.md — every bug whose regression test §11.15 teaches you to
  write; the casebook entry names the invariant, the test protects it.

---

## 11.18 Test map — where each discipline is pinned

The whole chapter is a test map, but this is the index: where to LOOK (and
where to ADD) a test, by concern.

| Concern | Where |
|---|---|
| the autouse fixtures + the dashboard reaper | `tests/conftest.py`, `tests/test_conftest_dashboard_reaper.py` |
| deterministic contract pins | `tests/_contract_pins.py` (used by the scripted orchestrator suites) |
| the knob-ON countermeasure for the tree-mismatch cases | `tests/test_best_of_n_tree_integrity.py` + `tests/_best_of_n_slate_support.py` (real evolve, default `best_of_n`) |
| the convergence oracle — full-loop convergence to an exact floor | `tests/test_convergence_known_answer.py` |
| the power oracle — decision-procedure operating characteristics under noise | `tests/test_decision_procedure_power.py` |
| worker-boundary stubs (module-level, importable) | `tests/_subprocess_worker_support.py` |
| cross-backend generation-store contract + session templates | `tests/test_genstore_conformance.py` |
| cross-backend storage contract | `tests/test_storage_conformance.py` |
| the reader snapshot oracle (epoch ordering) | `tests/_reader_parity_harness.py` + its consuming test |
| the behavior-preserving parity gates | `tools/parity.sh` + `tools/parity/lib/*.py` + `tools/parity/golden/` |
| contract-hash checkout-independence | `tests/test_epoch_contract.py::test_contract_hash_is_cwd_and_checkout_invariant` |
| the CLI surface is canonical | `tools/parity/lib/cli_help.py` (regen: `--update`) |
| the index projection is pure | `tools/parity/lib/test_reindex_golden.py` |
| the whole end-to-end audit bytes | `tools/parity/lib/test_mock_golden.py` + `mock_evolve_capture.py` |
| the library/driver + query-dashboard-free contracts | `pyproject.toml [tool.importlinter]` → `uv run lint-imports` |
| the retired private paths stay retired | `pyproject.toml [tool.ruff...banned-api]` → `uv run ruff check` |
| the digest / no-op / DOM-identity render discipline | `src/zicato/dashboard/static/test/*.test.mjs` → `make node-test` |
| the served-join parity witness | `src/zicato/dashboard/static/test/mock_server.mjs` + `tests/test_dashboard_racing_and_rounds.py` |
| the whole thing, reproducibly, both languages | `.github/workflows/ci.yml` (Python matrix + `cargo test`) |

The single command that runs the most in one shot is `make check` (lint +
import-lint + typecheck + full suite + node); the parity gates and the two
oracles ride the pre-commit checklist (§11.11) alongside it.
