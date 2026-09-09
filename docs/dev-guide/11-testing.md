# 11 — Testing

> **Covers.** How zicato proves it works: the pytest suite conventions and
> the autouse fixtures (`tests/conftest.py`), the deterministic-contract
> pinning philosophy (`tests/_contract_pins.py`) and its countermeasure
> duty, the two oracles (the known-answer convergence proof and the
> decision-procedure power harness), the subprocess-worker test support,
> the markers and tiers (`make test` / `test-fast` / `node-test` / `check`),
> the `tools/parity.sh` gates one by one, the import contracts +
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
> | V1 | the both-tiers-before-a-merge rule | **A bare `pytest` is the fast tier; a merge needs BOTH tiers.** Only a bare `pytest` drops the `slow` tier (tests measured at 15 s or more ALONE) — naming a file, a test or a marker expression runs what it names. `make test` and `tools/parity.sh` run both tiers locally. Pull requests run the default tier and the `slow` tier as separate checks. Preserve the boundary an integration test protects; remove unrelated setup and duplicate execution. |
> | V2 | the must-fail-with-the-fix-stashed rule | **A regression test MUST fail with the fix stashed.** A test that passes both before and after a fix proves nothing about the fix. |
> | V3 | the never-weaken-an-assertion rule | **Never weaken an assertion to make a test pass.** Fix the code, or pin the new value with a measured justification in the commit. A pinned number moves only with a measured reason. |
> | V4 | the pin-off-and-carry-the-countermeasure rule | **Deterministic contracts pin interacting knobs OFF — AND carry the countermeasure.** Pinning best-of-1 / replicates-1 / gate-off makes a script deterministic, but every shipped default also needs a knob-ON adversarial test. Pinning alone is how the best-of-N tree-mismatch case and the evidence-gate replicate-reuse case hid (`12-bug-casebook.md` cases 6 and 8). |
> | V5 | the dotted-path-callable rule | **A worker subprocess resolves its adapter and callables from a dotted import path** — never a closure or a `sys.modules` monkeypatch. Those do not cross the process boundary. |
> | V6 | the clear-global-state-on-both-sides rule | **Autouse fixtures isolate process-global state on BOTH sides** (clear before AND after) so a test neither inherits nor bequeaths a pin. |
> | V7 | the provenance-scoped-reaper rule | **The dashboard reaper selects by workspace provenance and never signals its own process group.** A provenance-blind reaper once group-killed an innocent concurrent evolve (`12-bug-casebook.md` case 5). |
> | V8 | the parity-green-on-unchanged-behaviour rule | **Every parity gate is GREEN on unchanged behaviour; a RED gate is information.** A golden is re-captured only with a stated behavioural reason, and a re-capture never bakes an unrelated sibling change. |
> | V9 | the contracts-are-lint rule | **The library never imports a driver; the query layer stays dashboard-free; the retired private paths stay retired.** The import linter (`lint-imports`) and `ruff` catch these as violations; no pytest test does. |
> | V10 | the exit-code-is-the-node-signal rule | **A node suite's real signal is the PROCESS EXIT CODE rather than the tail line.** The served joins a suite renders are recorded endpoint responses (§11.9.3). |

---

## 11.0 Map of the subsystem

| File / tool | What it is |
|---|---|
| `tests/conftest.py` | the suite root: `sys.path` pin + the six autouse fixtures (config-pin isolation, mutation-syntax-table isolation, worker-permit redirection, the default-proposer text-shim, the harmonograf launch stub, the provenance-scoped dashboard reaper) |
| `tests/_contract_pins.py` | `pin_deterministic` / `deterministic_weights` — the deterministic scripted-test knob pins |
| `tests/_workspace_support.py` | the builders a read-side test composes its `.zicato/` fixture from — paths off `WorkspaceLayout`, `index.db` off `zicato.index.schema`, so a fixture cannot re-spell either. A new read-side test uses these instead of hand-writing DDL or joining `"epochs"` into a path; the module docstring states the default and the two cases that fall outside it |
| `tests/_subprocess_worker_support.py` | module-level importable adapters + callables the worker subprocess resolves by dotted path (the worker-boundary test support) |
| `tests/_best_of_n_slate_support.py` | the scripted best-of-N slate whose slot-2 fabricates its metrics, so a wrongly mounted tree is detectable |
| `tests/test_convergence_known_answer.py` | **the convergence oracle** — the full loop to an exact, hand-computable floor, no tournament stubs |
| `tests/test_decision_procedure_power.py` | **the power oracle** — the operating characteristics of the decision procedure under seeded noise |
| `tests/test_genstore_conformance.py` | the cross-backend `GenerationStore` conformance suite + the session-template fixtures |
| `tests/test_conftest_dashboard_reaper.py` | the reaper's regression pins (provenance scoping + no self-group-kill) |
| `tools/parity.sh` | the behavior-preserving refactor gates (PYTEST / CONTRACT-HASH / CLI-HELP / REINDEX-DUMP / eight MOCK-GOLDEN lanes / MYPY) |
| `tools/parity/lib/*.py` | the gate helpers: `contract_hash.py`, `cli_help.py`, `normalize.py`, `mock_evolve_capture.py`, `test_mock_golden.py`, `test_reindex_golden.py` |
| `tools/parity/golden/` | the committed golden baselines |
| `pyproject.toml` | `[tool.pytest.ini_options]` (markers, `addopts`), `[tool.zicato.importlinter]` (the five contracts), `[tool.ruff.lint...banned-api]` (the TID251 bans) |
| `Makefile` | the targets (`test` = both tiers / `test-fast` = the default tier / `node-test` / `lint` / `import-lint` / `typecheck` / `check`) |
| `.github/workflows/ci.yml` | the pull-request jobs (Python 3.12 running the DEFAULT tier, dashboard JavaScript, parity, and the Rust supervisor) |
| `.github/workflows/slow-tier.yml` | the `slow` tier on pull requests and on demand |
| `src/zicato/dashboard/static/test/run-all.mjs` | the Node behaviour-suite runner (exit-code-honest) |

Verification covers Python, dashboard JavaScript, and Rust. Parallel Python
tests use temporary directories, dynamically allocated ports, and isolated
worker fixtures. Tests that modify shared process state must restore it.

> ⚠️ TRAP — the repo root is pinned on `sys.path` explicitly by
> `tests/conftest.py`, NOT left to pytest's implicit `rootdir` insertion.
> `tests/` is an importable package (`tests._subprocess_worker_support` is
> loaded by directly-spawned worker subprocesses), and `zicato` lives under
> `src/`, where pytest's implicit path handling is not reliable. If you add
> a helper module under `tests/` that a subprocess must import, it resolves
> because of that pin — do not "clean it up".

---

## 11.1 Tiers and markers

The suite runs in two tiers. Bare `pytest` runs the default tier during
implementation. The slow tier contains individual tests measured at 15 seconds
or more when run alone.

A merge needs both groups. Pull requests run them as separate required checks.
During implementation, select the relevant tests. A complete local run is
optional when CI will run the same checks; `make test` remains available for
both Python groups. The full verification policy is in §11.11.
Pull requests also report dashboard JavaScript and Rust checks separately.
Repository policy requires
every reported result to pass before merge. `.github/workflows/slow-tier.yml`
also runs the slow tests on demand from the Actions tab.

```
addopts = "-n 4 -m 'not node and not cascade_oc'"
markers = [
    "node: shells out to the standalone Node test harness (run via `make node-test`)",
    "slow: one test measured at 15 s or more (statistical characterizations and end-to-end simulations); deselected ONLY by a bare `pytest` (see tests/conftest.py) — naming a file or a test runs it, `-m slow` runs the tier alone, and `make test` and CI run both tiers",
    "integration: crosses a process or network boundary (worker subprocesses, live servers, git subprocesses); retain the real operations that establish the tested behavior",
    "cascade_oc: the opt-in evaluation-cascade OC measurement suite (CASCADE.md §4); EXCLUDED from the default run via addopts — run with `-m cascade_oc`",
]
```
— `pyproject.toml`, `[tool.pytest.ini_options]`

The four markers and what they tag:

| Marker | Tags | In the default run? |
|---|---|---|
| `node` | the in-pytest shim (`tests/test_dashboard_js.py`) that re-runs the whole standalone Node suite inside pytest | NO — it would duplicate `make node-test` |
| `slow` | one test measured at 15 s or more ALONE (`-n0`) | NO for a BARE `pytest`; YES the moment anything is named (see below) |
| `cascade_oc` | the opt-in evaluation-cascade measurement suite | NO — run it with `-m cascade_oc` |
| `integration` | any test crossing a process or network boundary | YES — most are fast |

`slow` describes measured duration and determines which tier runs a test.
`integration` identifies tests that cross a process or network boundary.
A test can carry either marker, both markers, or neither.

Membership in `slow` is a MEASUREMENT, and the measurement is SERIAL —
`pytest -n0 --durations=0 <the test>`, marked when the total reaches 15 s.
Parallel tests may launch their own workers and contend for CPU and memory.
Measure each candidate test serially so machine load and the pytest worker
count do not determine its tier membership.

`tests/test_slow_tier_registry.py` pins the marked set against a declared
list of node ids and their measured seconds, so a mark added or dropped
without its row reds a test instead of silently moving the tier.

The Makefile routes `test` to both Python selections, `test-fast` to the
default selection, and `node-test` to the independent JavaScript check.
`check` runs the complete plan; `check-fast` uses changed inputs.

**The rule for the `slow` tier, stated once.** A BARE `pytest` is the only
invocation that drops it. Name anything and you get what you named:

```bash
pytest                                    # the default tier
pytest tests/test_convergence_known_answer.py   # BOTH of its tests
pytest tests/x.py::test_y                 # that test, whatever tier it is in
pytest -k convergence                     # what the name matches, both tiers
pytest -m slow                            # the tier alone
pytest -m "not node and not cascade_oc"   # everything
```

`tests/conftest.py`'s `pytest_collection_modifyitems` deselects `slow`
items only when the session named nothing at all — no path, no `-k`, no
`-m`. It lives there
rather than as a `not slow` term in `addopts` because an `addopts` term
applies to EVERY invocation, including one that names a `slow` test —
which would then run nothing and report green. That is not hypothetical:
under the earlier design `pytest tests/test_convergence_known_answer.py`
collected one of two tests, and naming the slow test by node id collected
zero and exited 0. `tests/test_slow_tier_registry.py` pins all four forms.

> ⚠️ TRAP — a command-line `-m` REPLACES the `pyproject` selector, it does
> not AND with it. So the full suite is `-m "not node and not cascade_oc"`
> with both terms restated, and the tier alone is
> `-m "slow and not node and not cascade_oc"` — `-m slow` on its own
> re-enables the Node shim and the cascade measurement.

> Preserve real operations that establish the tested behavior. A worker
> isolation test needs a real subprocess and discarded checkout; a server
> binding test needs a real port. Remove unrelated setup and repeated
> executions when existing tests retain their assertions and distinct failure
> cases. Tests of report formatting can supply measured inputs directly.
> Report publication checks should cover stored narrative, deterministic
> refresh, and matching standalone and embedded report bodies. Browser checks
> should cover published-fragment display, absent-report behavior, interactive
> figures, and redraws after equal-length text corrections.
> Measure subprocess starts, child CPU time, and elapsed time under matching
> conditions. Moving a test to the slow tier does not reduce complete
> verification cost.

`make check` consumes the complete verification plan in `tools/verify.py`.
It runs checks sequentially and bounds worker processes and native threads;
CI invokes subsets under its separate visible job names.

### 11.1.1 Suite conventions

Four conventions make the suite fan out cleanly and keep an async test
honest:

```
[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
addopts = "-n 4 -m 'not node and not cascade_oc'"
pythonpath = ["."]
```
— `pyproject.toml`, `[tool.pytest.ini_options]`

- **`asyncio_mode = "auto"`** — an `async def test_*` is collected and run
  without a per-test `@pytest.mark.asyncio`. Most of the loop is async; the
  suite tests it directly with `asyncio.run(...)` inside a sync test (the
  convergence oracle) or as a bare async test.
- **`-n 4`** — four pytest workers by default, matching the complete verifier.
  Tests isolate their files with `tmp_path` and use dynamic ports via
  `bind(("127.0.0.1", 0))`. An explicit `-n` overrides the worker count;
  `pytest -n0 tests/test_foo.py::test_bar` runs serially for debugging.
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
> flake under parallel pytest (two workers collide nondeterministically). When a new
> test flakes only in the full run, the first suspect is a shared resource it
> did not isolate — never "xdist is flaky".

The epoch-transition tests use one scripted proposal per candidate while
retaining field evaluation, contract changes, interrupted settlement, and
recovery. Proposal sampling at the shipped defaults has dedicated complete-loop
coverage. A successful promotion test also checks its persisted hook status
and absence of a hook-failure finding. The critical-health test checks the
warning, returned summary, and saved report from the same round.

### 11.1.2 Running only what a change can reach

`make test-affected` runs the Python tests reached by branch changes,
staged and unstaged edits, and untracked files. The selector parses imports
in `zicato`, `zicato_examples`, `tests` and `tools`. Tests for repository
tools participate even though pytest's default `testpaths` excludes them.

```bash
make test-affected
make test-affected RANGE=HEAD~3       # committed comparison only
uv run python tools/affected_tests.py --explain
uv run python tools/affected_tests.py --run -- -n0
```

The command emits JSON unless `--run` executes its result. Each result
includes a status, reasons, resolved revisions, changed paths by source,
and a digest of the changed files' contents:

- `selected` names the Python test files reached by the change.
- `known-empty` means no changed file reaches a Python test. The runner
  reports the empty selection and starts no pytest process.
- `unresolved-full` means some dependencies cannot be resolved. The runner
  selects the Python suite and the tests for repository tools.

The default compares `origin/main...HEAD` and includes worktree changes.
An explicit `--range`, or Make's `RANGE`, selects changed paths from that
committed comparison alone. The import graph always uses files in the
working checkout. Missing comparison bases and invalid selected paths
fail visibly. The runner passes paths and pytest options as literal
arguments; shell substitution is unnecessary.

The graph includes literal dynamic imports, modules named after `-m` in
subprocess arguments, and dotted paths named in test text. Changes to
shared fixture modules reach their importers. A document reaches tests
only when a module names its path in executable string data.

Deletions, both sides of renames, unreadable imports, unknown file types,
and untracked files outside prose trees require full Python coverage.
Changes to pytest configuration or a `conftest.py` also require full
coverage. Recognized prose without a reader can produce a known empty
selection.

Affected selection is an iteration command. The complete merge checks
still require both Python test tiers and the applicable browser, Rust,
prose and packaging checks. Other verification commands can consume
`build_selection()` and `run_selection()` from `tools/affected_tests.py`.
A Python selection cannot discharge checks for other languages.

`tests/test_slow_tier_registry.py` uses one repository-wide collection to
verify declared slow-test membership. Command forms use a four-test
repository that imports the actual selection hook. File, node, keyword,
and marker selection therefore retain slow tests without repeatedly
collecting the application suite.

---

## 11.2 The autouse fixtures — `tests/conftest.py`

Five autouse fixtures shape every test.

- **Two isolate shared resources.** `_isolate_mutation_syntax_table`
  restores built-in mutation syntax so a workspace's additional file types do
  not affect later tests. `_isolate_host_worker_permits` selects a session
  directory so tests and an operator's concurrent run use separate pools.
- **Two replace production defaults** that would otherwise require optional
  dependencies or real I/O (§11.2.2). Each fixture lists the modules that
  exercise the real path and skip its replacement.
- **One cleans up dashboard servers** at session end (§11.2.3).

### 11.2.1 Invocation configuration isolation

CLI flags become an immutable `InvocationOverlay`. Each invocation resolves
its own workspace values and passes the resulting configuration to runtime
construction and workers. Tests can interleave different choices without a
fixture that clears configuration state.

`tests/test_invocation_configuration.py` checks that concurrent factories and
worker payloads retain their own values and sources. `tests/test_cli_config_flags.py`
checks flag admission and propagation through a real worker process.

### 11.2.2 The stand-in proposal runtime

A round cannot open without a proposal runtime: there is no built-in
default to fall back to. Runtime construction requires an explicit named
proposal declaration. So every fixture workspace that runs a round declares one,
and `tests/_foe_support.stand_in_proposer_block` writes it:

```python
    (workspace / "config.json").write_text(
        json.dumps(
            {
                ...,
                "proposer": stand_in_proposer_block(tmp_path / "foe"),
            }
        )
    )
```
— `tests/_orchestrator_harness.py`, `bootstrap_workspace`

The block names one executable written under the workspace's own temporary
directory: the stand-in Foe binary (`tests/_fake_foe.py`). It speaks the host
protocol without a credential, a network, or a Rust toolchain. The absolute
path stays on disk because tournament workers are separate interpreters. They
read `config.json` and see nothing this process patched in memory.

The stand-in runtime owns two model fixtures. A scripted fixture replays a
written list of turns when a test pins one episode. The mechanical fixture in
`tests/_foe_stand_in_proposer.py` enumerates the parent snapshot's mutation
points, rewrites one string literal, and returns a hypothesis naming that point.
Its edit is a tag carrying the candidate id. The result is deterministic per
candidate, distinct across a field, and bounded across rounds.

A test whose subject is a *misbehaving* proposer steers it from the
workspace rather than scripting turns:

| Key | The episode then |
|---|---|
| `idea` | states one fixed core idea, so a field's siblings duplicate each other and the diversity guard fires |
| `predict` | predicts one named metric, so an undeclared judge name drives a validation refusal |
| `hypotheses` | takes either of those per candidate id, for a field whose slots must differ |
| `break_first` | writes N leading edits the verifier must reject, driving the repair loop — and, past the retry budget, the block that ends an unsatisfiable episode |
| `refuse` | reports a block instead of proposing at all |
| `contents` | writes an exact literal body per candidate (or per `<candidate>#<slot>`), which is what the known-answer harnesses script |

> ⚠️ TRAP — a proposal is an EPISODE rather than an evaluation call. A test that
> counts proposer spend counts episodes in the workspace's own durable
> capture (`read_proposer_inputs`, filtered to `ROLE_PROPOSAL`), never
> calls into a mock; a test that asserts on what the model saw reads the
> `user` of that same record, never a patched renderer. The evaluation
> callable still serves the critique, the recombination merge and the
> analyzer, so `make_aux_responder([])` is the ordinary spelling for a
> round that needs none of those.

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

`tests/test_decision_procedure_power.py` characterizes the margin gate,
replication, pass-rate monotonicity and independent confirmation under a fixed
noise model. Each observation uses the example target's `draw_measured_tokens`,
output synthesis and board predicates. Its seed is determined by the workspace
seed, generation id, entry id and replicate index. The resulting rates describe
these inputs and seed sets; they do not qualify arbitrary targets or optional
selection features.

The policy trials call production functions directly: `fold_matchup_replicates`
in `tournament/scoring.py`, `aggregate_generation_score`, `evaluate_gate`, the
gauntlet strategy and `evaluate_tournament`. Replicate reduction has one owner
for both the scheduler and the policy trials. Test observations are immutable;
converting them into loss profiles does not require a workspace, worker,
cache file or dashboard record.

`tests/_decision_report.py` defines immutable reports containing the requested
seed set, effective scoring inputs, implementation digest, observations,
decisions and actual `EvidenceResolution`. Every requested seed must appear
exactly once in order, including rejection, deferral and inconclusive results.
Evidence eligibility comes from the production confirmation attempts: ordinary
selection attempts remain visible with `selection_only` status and cannot
increase the number of independent duels. Budget spent and exclusion reasons
remain available even when confirmation fails.

Related assertions consume the same complete report within a test invocation.
The three planted effects each run once; the small-effect cost assertions reuse
that report. A module fixture shares the single-draw null report among the
noise-floor and effect-size assertions in each worker. There is no persisted
pass cache or cross-invocation statistical fit cache. Changing inputs or code
requires a fresh report; the implementation digest identifies what produced it.

The fixed characterization uses these conditions:

| Condition | Inputs and assertions |
|---|---|
| Noise model | Per-defect flip probability `0.22`; analytical single-draw difference standard deviation about `0.663` |
| Single-draw null | Seeds `0..59`; the observed difference spread must be consistent with the analytical scale |
| Confirmed null | Seeds `0..23`; none may promote an unchanged system |
| Planted effects | Seeds `0..11` for measured improvements `0.336`, `0.672` and `2.016`; power must be monotone and the largest effect must always promote |
| Replicated selection | `32` ordinary draws, margin `0.01`, aggregate pass-rate monotonicity |
| Confirmation | Threshold `0.8`, at most `38` independent single-draw attempts, covariance-aware contrast intervals and planned family/look correction |

A confirmed trial uses at most 700 board units: two generations times five
entries times 32 selection draws plus 38 confirmation draws. Its report records
the actual count. Selection replication does not multiply the sample size of
a confirmation attempt. The measured small-effect power and the assumptions
behind the independent power/cost reference are documented in
[`CONFIRMATION-POWER.md`](../design/CONFIRMATION-POWER.md).

A change in a pinned rate, seed set, threshold or budget requires a measured
technical explanation. Preserve the failed result when correcting an invalid
measurement claim. Cost reduction alone does not justify changing the expected
operating characteristics.

### 11.4.3 Conformance between direct trials and execution

The direct policy trials are paired with focused execution checks.
`test_direct_decisions_match_scheduler_at_measurement_boundaries` compares the
complete production decision and confirmation result against `run_matchup` for
an improvement, a tie and missing execution. Its scheduler path replaces only
`runner._run_single` with the example noise model. The missing-execution case
must remain ineligible and retain the champion.

`test_noisy_adapter_seeded_draws_cross_the_worker_boundary` runs the example
adapter through real subprocess workers and compares its measurements with the
same deterministic reference. Separate confirmation tests exercise reserved
draw indices and verify that fresh confirmation cannot overwrite or reuse
ordinary selection artifacts. Cache identity, cancellation and settlement
retain their own real-boundary tests.

The scheduler publishes partial aggregates only when a live progress consumer
exists. Its conformance tests check both paths: an absent consumer performs
only the two final generation reductions, while a present consumer still
receives progress. Measurement accounting and final gate inputs are unchanged.

Use direct production computation for broad policy characterization and small
independent mathematical examples for numerical correctness. Keep focused
scheduler, worker and durable-record checks for the behaviors those boundaries
own. A test that substitutes a gate, strategy or confirmation verdict cannot
establish the operating characteristics of that substituted component.

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
| `StubAdapter` | `run(entry, sinks, config)` without emitted events | the happy path, no goldfive dependency |
| `SnapshotWritingAdapter` | writes runtime output INTO the mounted snapshot | per-run checkout isolation — the write must land in a discarded per-run copy, never the canonical snapshot |
| `SleepingAdapter` | a BLOCKING `time.sleep` that wedges the worker's own event loop | forces the PARENT's `wait_for` + SIGTERM/SIGKILL escalation (the cooperative budget can't fire) |
| `CooperativeAdapter` | a CANCELLABLE `asyncio.sleep` | the worker's own cooperative budget fires and it self-aborts, exit 0 |
| `EmittingThenSleepingAdapter` | emits one `run_started` frame then sleeps to cancellation | the terminal-event fix leaves a `run_aborted` frame on disk |
| `AbortingAdapter` | returns an aborted `RunResult` (a simulated crash) | the reducer's not-completed penalty (without it a near-instant crash scores `drift_loss == 0.0`) |
| `ConfigProbeAdapter` | records the WORKER process's resolved typed config to `config_probe.json` | the invocation overlay crossed the subprocess boundary through the serialized configuration |

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

`make parity` invokes the shared golden check with `--skip PYTEST,MYPY`.
The complete plan owns those Python-suite and type-check results separately.
For an individual golden comparison, use `bash tools/parity.sh --only`
with its declared gate name. Both `--only` and `--skip` accept repeated
options or comma-separated names. `--update` recaptures selected goldens.

An unknown name, missing operand or empty selection fails before any gate
runs. A successful exit requires every selected command to complete
successfully. A checker failure cannot be accepted as an empty diagnostic
count or written into a success baseline.

### 11.7.1 PYTEST

This gate selects both Python tiers with `pytest -q -m "not node and
not cascade_oc"`, matching `make test`. The complete verification plan runs
the two tiers separately and skips this parity gate to avoid repeating them.

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

The component breakdown (board / brief / scoring / evaluator revision / adapter /
mutable_trees / proposer) localizes a regression to the exact canonicalizer
that moved. **Reds legitimately** when a canonicalizer changes what it hashes
(rare — usually a bug). The fixture pins a fixed adapter reconstruction
document and fixed mutable-tree paths. The hash therefore depends only on
committed file contents and those literals, never on host- or clock-derived
state.

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
golden with `uv run python tools/parity/lib/cli_help.py --update` and reconcile the hand-authored command contract in
`docs/design/CLI.md` with the same help output.

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
| `MOCK-GOLDEN-GAUNTLET` | gauntlet · full · 1 | the `field_n == 1` branches: one crowning duel with no rungs or cuts, and the crowning holdout confirmation on it |
| `MOCK-GOLDEN-GAUNTLET-FAST` | gauntlet · fast · 1 | cache-first slot resolution under a single challenger, and a fast round's crowning holdout confirmation |
| `MOCK-GOLDEN-RACING-FAST` | racing field 4 · fast · 1 | every rung resolving both competitors through the unit cache |
| `MOCK-GOLDEN-TWO-ROUND-RACING` | racing field 4 · full · 2 | the between-round carry-over: the promoted head advancing off the seeded `v0`, the crowned generation defending the next round, that generation's patched snapshot supplying the next round's mutable surface, round directories numbering on from `0`, and round 1's settled snapshot naming round 0's winner as its champion |
| `MOCK-GOLDEN-SWISS` | swiss field 4 · full · 1 | fixed-round pairings over champion + challengers, Copeland standings, and the leader's final champion-gate confirmation |
| `MOCK-GOLDEN-SINGLE-ELIM` | single_elim field 4 · full · 1 | challenger-vs-challenger bracket nodes (no incumbent, so the gate's preferred side wins), then the champion-vs-survivor final |
| `MOCK-GOLDEN-DOUBLE-ELIM` | double_elim field 4 · full · 1 | the losers' bracket second life and the grand final feeding the champion gate |

The last three structures are the ones the unified round pipeline reaches
through registries that no other lane touches end to end; their contracts
live beside the racing one as `scoring.swiss.json`,
`scoring.single_elim.json`, and `scoring.double_elim.json` in
`examples/zicato_examples/target_1_presentation/`; each sets
`experimental.tournament_structures` to `true`, which admits its structure.

Every lane drives `evolve_n_rounds`, single-round lanes included: at
`rounds=1` the loop's persisted artifacts are byte-identical to a bare
`evolve_once`, so the round count is an ordinary lane parameter rather than
a second code path.

Every lane also holds part of its board back, so every golden witnesses the
train-slice selection and the crowning holdout confirmation. Which entries
are held back is hash-derived and salted by the epoch id, so a lane's
`epoch_name` decides it; under a name that puts no entry in the holdout, the
split degrades to the whole board and the lane exercises no holdout rule
while still capturing and still passing. `test_lane_board_splits_to_a_non_
empty_holdout` asserts the property per lane, under the same `-k <lane name>`
selector as the byte comparison. When it fails, choose a different
`epoch_name` for that lane and recapture it: the name is the salt, so a
descriptive alternative that splits non-empty is usually one or two tries
away.

The two fast lanes pre-seed the champion's per-board `loss.json` for every
replicate slot the contract requests and install the PERSISTING reducer
stub. Without both, every cache read raises, fast mode degrades to full, and
the lane would capture the full-mode path under a fast-mode name — the
goldens record `champion_eval_mode: "fast"`, which is the assertion that it
did not.

`tools/parity.sh` selects a lane with `pytest -k <lane name>`, so no lane
name may be a substring of another. The lane table lives in
`tools/parity/lib/mock_evolve_capture.py`; adding a lane there and a
entry in the gate table in `tools/parity.sh` is the wiring — plus,
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

`uv run mypy src/zicato/` must finish with exit status zero. A checker that
cannot start, rejects its arguments, or terminates abnormally fails the gate
even when its output contains no type-error diagnostics. This gate has no
golden baseline; `--update` also requires successful completion.

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

## 11.8 The import contracts + the TID251 bans

Two static gates keep the architecture from eroding: the import-linter
contracts (`make import-lint`) and the ruff TID251 banned-api list.
Neither is a pytest test — a violation reds the linter, so they run in
`make check` and CI.

### 11.8.1 The import contracts

Namespace roles in `pyproject.toml [tool.zicato.namespace_roles]` are the
inventory used by `tools/check_imports.py`. Every production namespace has one
role. Library code cannot import the CLI or dashboard. Primitives and execution
code cannot import coordination code; primitives may import only primitives.
Packages with mixed responsibilities retain their declared library role.

The explicit import-linter contracts add narrower restrictions:

| Source | Forbidden dependency |
| --- | --- |
| Dashboard | CLI |
| Query readers | Dashboard |
| Proposer patch validator | Board execution, judges, emulators, adapters and workers |

The CLI may launch the dashboard. `make import-lint` verifies the complete role
inventory and these import restrictions.

### 11.8.2 The TID251 bans — retired private reaches

A set of cross-module helpers live at public seams on their home modules.
The TID251 (flake8-tidy-imports banned-api) list rejects any import of the
retired underscore paths, and each ban names the replacement so a reader of
the violation knows the fix:

```
[tool.ruff.lint.flake8-tidy-imports.banned-api]
"zicato.orchestrator._compute_field_diversity".msg = "moved: use zicato.selection.diversity.compute_field_diversity"
"zicato.storage._atomic.read_json".msg = "use the public face: from zicato.storage import read_json"
"zicato.analyzer.aggregator._to_snake".msg = "deleted: use zicato.telemetry.event_log.to_snake"
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

### 11.9.3 The recorded fixtures

The server computes the round-timeline, racing-field and matchup-grid joins
and the elimination fold (09-dashboard-and-query.md §9.2.5), and the node
suite holds no Python. `static/test/recorded.mjs` therefore serves what the
Python endpoints answered: `tests/data/endpoint_route_snapshot.json` records
every probed route over the workspaces `tests/_console_scenarios.py` writes
(one per browser scenario — the shared console epoch, the racing ladder, the
cross-epoch pair, the structure records, the round-model cases), and
`tests/data/endpoint_route_probes.json` names the URL behind each label, so
`recordedRoutes('single_elim')` is a fixture map keyed the way the views
fetch. The elimination round lists the suite draws are declared once in
`tests/data/elim_states_cases.json`, and `tests/data/elim_states_served.json`
is `derive_elim_states` over each of them, so `elimCase(name)` hands a test
the server's model of its bracket.

Both recordings are pinned by Python: `tests/test_dashboard_endpoint_table.py`
serves every workspace through the real application and compares each body
byte for byte, and `tests/test_tournament_view_elim_states.py` folds every
declared case. A reader change that is meant to move a response is
re-recorded with `ZICATO_ENDPOINT_SNAPSHOT_UPDATE=1` on those two suites
(09-dashboard-and-query.md §9.16, step 3), and a shape no recorded workspace
covers is added as a scenario with its probes rather than written by hand.

> ⛔ NEVER hand-write a served join or an elimination model in a node fixture
> to make a test pass. The suite exists to prove the client renders the
> server's answer; a fixture the server never produced proves nothing about
> the console. Add the scenario on the Python side and record it.

### 11.9.4 The console test-file map, by view

The console's behaviour tests are grouped by dominant view into ten files,
with the shared preamble (the `FIXTURE` map plus the `freshHb` /
`installFetch` / `allByClass` helpers) in `fixtures.mjs`:

| file | covers |
|---|---|
| `shell.test.mjs` | the chrome — tree / crumbs / status pill / containment |
| `tournament_structures.test.mjs` | the tournament-structure figures + standings |
| `candidate_surfaces.test.mjs` | the candidate dossier |
| `epoch_scoping.test.mjs` | cross-epoch scoping (the fleet + per-epoch reads) |
| `figures.test.mjs` | the figure grammar (heatmaps / bars / radars) |
| `home_epoch.test.mjs` | home + epoch views |
| `lifecycle_dag.test.mjs` | the mutation surface + lifecycle DAG |
| `live_surface.test.mjs` / `live_hero.test.mjs` / `live_waves.test.mjs` | the SSE-driven live hero / ticker / funnel transitions |

`digest_opts.test.mjs` pins the `digestOpts` rules directly — functions are
dropped, key order is irrelevant, a non-integer number rounds to three
decimal places, and a non-finite number folds to `null`. `bracket.test.mjs`
pins six bracket-topology tests on the served `gen_states` fixtures. When you
split or rename a node test file, the runner (`run-all.mjs`) globs
`*.test.mjs` so it needs no registration — but grep the split target for the
assertion you rely on; the grouping is by DOMINANT view and a few assertions
cross seams.

### 11.9.5 Verify server values in rendered browser output

A browser test should verify values produced by the owning Python reader and
rendered through the production JavaScript module. Comparing only the response
mapping misses errors in labels, formatting and DOM construction.

Use fixed inputs to generate the reader payload, render that payload, and read
the resulting nodes. Assert the displayed values and the set of fields covered
by the fixture. Keep statistical arithmetic in its Python owner; browser
rendering tests verify how those results are displayed.

---

## 11.10 CI

`.github/workflows/ci.yml` and `.github/workflows/slow-tier.yml` invoke
selected checks from `tools/verify.py`. Stable job names retain separate
results for default Python tests, statistical and end-to-end oracles,
parity, JavaScript, Rust, prose and line budgets. The check-plan tests
require every complete check to appear once across those workflows.

The Python default and slow selections partition the required suite.
Both discover tests under `tools/`; parity owns its separate golden test
modules. Parity excludes its Python-suite and type-check copies because
those checks have separate owners.

Each invocation runs checks sequentially with four pytest workers and
one native-library thread per process. Cargo also uses four build jobs.
`--workers` changes the worker bound. Per-check reports record wall time,
status, command, selected tests and available process observations. Reports
are evidence from an invocation and never suppress subsequent checks.

The packaging check validates an installed supervisor from an isolated
wheel installation locally. The parity job validates the wheel installed
by its setup step, using `--installed-wheel` to avoid another build.

## 11.11 Complete validation before merge

```sh
make check-fast       # focused checks during implementation
make check            # complete local alternative when CI is unavailable
```

Require one complete successful CI run on the source proposed for merge.
Do not also require an equivalent complete local run. Local full verification
remains available when CI cannot run or when a particular diagnosis needs it.

`tools/verify.py` owns required commands, languages, input paths and
selection rules. `--list` reports the selected checks as JSON. `--only`
requests a named subset for diagnosis or one CI step; a partial invocation
is not complete validation. Unknown names, repeated names, missing
programs and empty required selections fail.

The complete plan runs both Python tiers once, including the known-answer
and statistical oracles. It also runs style, type, import-boundary,
golden, JavaScript, Rust, packaging, prose and line-budget checks. A
separate ledger check prevents dropping recorded accounting rows.
No additional oracle invocation is needed after the same complete
revision passes. A source change invalidates that conclusion for the
revision proposed for merge.

Use `--report-dir PATH` to retain reports under a chosen directory. Each
invocation creates its own directory; prior reports are never reused as
passing results. The report includes the revision, worktree status,
commands, worker bound and outcomes. The timing helper also records
collection, setup, execution, teardown and available process counts.

Before publishing, apply the attribution scan in `01-orientation.md §G1`
to the staged diff and authored text.

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
reference and counts newline bytes, matching `wc -l`, and returns three
numbers. `_excluded()` owns the narrow Markdown, lockfile, and
`EXCLUDED_FROM_BUDGET` exclusions that shape the total, and that tuple states
per entry why the path holds no implementation. `_production()` owns the
runtime subtotal. `_logic()` reduces each production file to its executable
lines: `ast` and `tokenize` drop Python docstrings and comment-only lines, a
left-stripping scan drops JavaScript `//` and `/* … */` lines, a character scan
drops Rust comments and every line of an item carrying `#[cfg(test)]`, and a
file type with no counter keeps its raw count. The Rust scan blanks string
bodies, regular and raw alike, so a brace inside a literal cannot move the
brace depth that finds a test module's close. Prose and comments therefore
reach the total and the production subtotal only, so deleting a docstring buys
no room under the logic ceiling. The report groups the total by language and
subsystem so movement is reviewable.

`--report` prints every subsystem's total, production subset, production
logic, and prose share (the share of production lines that do not execute) in
descending order of production logic; the three columns partition the
repository-wide counts. `--write-summary` rewrites the
per-subsystem table in `docs/design/LINE-BUDGET.md` from the same measurement,
and `--check-ledger` fails while that table differs from the tree, so a change
to production code lands with the table it produces; the CI job writes the
`--report` table into the job summary. `--history [--since <ref>] [--subsystem
<name>]` prints the production-logic series per subsystem along the
first-parent chain ending at `--ref` (default `HEAD`), from the baseline
reference by default, through a content-addressed cache under `.cache/` that a
change to the counters discards.

`.line-budget.json` contains hard limits without an allowance. Keep the three
independent one-line-overage assertions in `tests/test_line_budget.py`: each
proves that one limit fails at `limit + 1` while the other two are unchanged.
Three fixture tests beside them pin the split the logic count makes — a Python
file whose docstring and comment lines count in `production` and not in
`production_logic`, a JavaScript file with line and block comments, and a Rust
file carrying doc comments, a nested block comment, a block comment that ends
before code on the same line, a `#[cfg(test)]` module with nested braces, and
string literals holding braces, one of them unbalanced — and one more pins
that every path in `EXCLUDED_FROM_BUDGET` is tracked in the tree.
Run:

```bash
uv run pytest tests/test_line_budget.py -q
python tools/line_budget.py --check
```

The stable measurement contract, final arithmetic, and ratchet policy live in
`docs/design/LINE-BUDGET.md`; implementation mechanics live only here.

### Prose gate

Repository prose is read by people who have the tree and none of its
development history. `tools/prose_lint.py` reports seven constructions that need
that history to decode: invented short labels used as vocabulary, wording about
what the tree stopped doing, a subject defined by contrast with an absent
alternative, adverbs asserting conviction in place of information, a trailing
verb with no subject or result, an issue number standing where a statement
belongs, and a word pinning a claim to the unnamed moment it was written. The
tool's docstring states each rule and its reason in full;
`tools/test_prose_lint.py` pins one hitting and one clean sentence per rule, so
a widened pattern that starts swallowing ordinary sentences turns red.

It reads the Markdown, and the Python docstrings and comments, under seven
roots — `CHANGELOG.md`, `README.md`, `docs/`, `examples/`, `skills/`,
`src/zicato/`, and `tools/` — which is every tree whose prose a reader is
expected to act on. The hand-authored command contract in `docs/design/CLI.md`
is included. Generated captures under `tools/parity/golden/` are excluded;
correct their source values or renderers before regenerating the captures.
JavaScript comments are outside the scanner's scope.

One file is exempt from one rule, through a per-file table beside that skipped
list: `CHANGELOG.md` is an explicitly historical document whose chronology is
the content it carries, so the rule against wording about what the tree stopped
doing does not read it. The other six rules read it like any other file.

Fenced blocks and backtick spans are quoted material and are masked before
matching, so a document may cite an identifier freely. Python strings other
than docstrings are out of scope. Layer numbers inside standard collocations (a
squared norm, a load balancer or a processor cache at a numbered level) pass
through an allowlist. Two rules report at severity `review` and never fail a
run on their own: the adverb rule, because a few uses are load-bearing, and the
temporal-hedge rule, because a named date or a stated condition beside the
hedge makes it exact.

`tools/prose_lint_baseline.json` holds one count per rule over the whole
tree. The ratchet form fails only where a rule's count rises above its
entry, so a rule with a standing backlog still catches every new hit. Lowering
the floor after a cleanup means rewriting the file from the tree and committing
it with the cleanup.

```bash
python tools/prose_lint.py                        # report; fails on any hit
python tools/prose_lint.py --rule codename-label  # one rule
python tools/prose_lint.py --baseline tools/prose_lint_baseline.json
python tools/prose_lint.py --write-baseline tools/prose_lint_baseline.json
```

CI runs the `--baseline` form. `tools/prose_lint_baseline.json` holds one count
per rule, and the run fails only where a count rises above its ceiling, so the
check guards the tree while the standing backlog is worked through.

A cleanup change leaves the baseline file alone. Lowering a count is always
green against a higher ceiling, so a prose-fixing branch touches only the prose
and never contends for the shared file — several such branches can be in flight
at once without conflicting. Once they have merged, one change of its own
regenerates the file with `--write-baseline` and lands it, ratcheting every
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

## 11.13 The reader parity harnesses — snapshot oracles over the workspace readers

Two harnesses pin what the code reads off a `.zicato/` workspace. Each builds
a deterministic fixture workspace, calls every reader in its scope, and
serialises the results into one canonical-JSON document compared against a
committed golden. Together they are the oracle for any refactor of the read
path: capture BEFORE, refactor, capture AFTER, and every difference has to be
explained.

| Harness | Scope | Fixture | Golden |
|---|---|---|---|
| `tests/_reader_parity_harness.py` | the dashboard query layer's public `build_*` readers | three epochs, one of them empty | `tests/data/reader_parity_snapshot.json` |
| `tests/_workspace_reader_parity_harness.py` | `zicato.analyzer`, `zicato.reflection`, `zicato.health`, `zicato.index`, `zicato.workspace`, and the `zicato health` command's own loaders in `zicato.cli` | two epochs, eleven generations, replicates, reflections, round logs | `tests/data/workspace_reader_parity_snapshot.json` |

The goldens are independent. A change to the query layer never re-records the
workspace-reader golden, and the reverse holds too, so a re-record is always
scoped to the reader family that moved.

### 11.13.1 The query-layer harness — epoch ordering

The query fixture is built to expose one defect class: epoch ordering, where
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

### 11.13.2 The workspace-reader harness — generation ordering

The readers outside the query layer walk the same tree from six packages, and
they do not all order it the same way. The second harness pins each of them
under a `<package>.<function>` label, with the coordinate the reader was
called on appended after `::` where one reader is captured several times
(`zicato.index.runs_for_generation::v0`).

Its fixture is built around the orderings those readers disagree about:

- **Eleven generations, `v0` through `v10`.** Numeric-aware ordering puts
  `v2` before `v10`; lexical ordering puts `v10` between `v1` and `v2`. The
  golden records both: `zicato.epoch.journal.read_epoch_experiments` and
  the `zicato health` command sort numerically, while the index's
  `experiments_for_epoch` selector orders by the id column and so sorts
  lexically.
- **Board entry ids `t1`, `t2`, `t10` and reflection ids `r-2`, `r-10`**,
  which put the same question to the per-entry and per-reflection walks.
- **Two epochs, `e2` (January) and `e10` (February)**, whose directory names
  sort in the opposite order to their recorded creation times under a lexical
  sort — the query harness's epoch axis, carried here so the cross-epoch
  readers are pinned on it too.
- **Per-run replicates**, including one slot in the contract pre-flight's
  reserved band, which the observation corpus must refuse: a pre-flight probe
  patches the champion's snapshot and runs it under the champion's own
  generation id, so the slot describes code the champion does not have.
- A per-round event log, a durable field-tournament snapshot, two board
  reflections, persisted loop-health reports, per-generation
  mutated-tree-import provenance, and a derived SQLite index — so every
  reader has real material rather than an empty directory.

The gate runs at two levels. `ORDER_ENFORCED` maps a label to whatever
identifies one of its rows — a JSON key, a tuple position, or `None` for rows
that are bare strings — and the harness compares those identifier sequences
FIRST:

```python
ORDER_ENFORCED: dict[str, str | int | None] = {
    # Numeric-aware generation order: v2 before v10.
    "zicato.epoch.journal.read_epoch_experiments::e2": 0,
    "zicato.cli.health.generation_ids::e2": None,
    ...
}
```
— `tests/_workspace_reader_parity_harness.py`

Byte identity over the canonical JSON subsumes that check, since a reordered
list is a different document. The order pass exists so the common failure is
legible: it reports the label and both orders instead of a diff several
thousand lines into the golden. Flipping the `zicato health` command's
generation sort from numeric to lexical produces:

```
AssertionError: reader 'zicato.cli.health.generation_ids::e2' changed its row order:
    ['v0', 'v1', 'v10', 'v2', 'v3', 'v4', 'v5', 'v6', 'v7', 'v8', 'v9']
    ['v0', 'v1', 'v2', 'v3', 'v4', 'v5', 'v6', 'v7', 'v8', 'v9', 'v10']
```

Add a label to `ORDER_ENFORCED` whenever a reader's row order is part of what
callers rely on. Leave it out when the order is incidental; byte identity
still pins it either way.

### 11.13.3 The masking discipline — narrow, or you weaken a check

Both harnesses mask ONLY the fields that are non-deterministic by
construction, and the docstrings state the boundary — the response-stamp
`generated_at` is masked, but on-disk-derived timestamps
(`created_at`/`proposed_at`) are deterministic in the fixtures and are NOT
masked:

```python
def mask_volatile(value: Any) -> Any:
    """Recursively replace wall-clock noise keys with a constant.

    Keeps the snapshot reproducible across the capture/compare boundary
    without weakening any structural / ordering check.
    """
```
— `tests/_reader_parity_harness.py`, `mask_volatile`

`_normalize_root` collapses the per-run absolute workspace path to `<ws>` so
path-bearing responses (`environment.workspace`, the corpus' artifact refs)
compare stably. The rule is identical to `tools/parity/lib/normalize.py`
(§11.7.6): mask the KNOWN non-determinism narrowly, so a REAL field change
still surfaces as a diff.

The workspace-reader harness adds two masks of its own, each scoped to one
reader rather than applied by key name:

- The loop-health report's `checked_at` is the moment the assessment was
  taken. It is masked on that one field of that one report, because the
  fixture's persisted pre-flight verdict and per-round health reports carry a
  `checked_at` of their own that is read off disk and stays pinned.
- A mined episode's `episode_id` is a digest over the absolute paths of the
  artifacts it references, so it varies with the temporary directory before
  the workspace-root normalization can reach it.
  `_stabilize_episode_ids` renames the distinct ids to `episode-1`,
  `episode-2` … in first-appearance order, which keeps the episode count, the
  sharing of ids between rows, and the rank order under the golden's control
  while dropping only the opaque digest.

`test_fixture_is_reproducible` builds the fixture twice under different
temporary roots and compares the two snapshots, so a per-run value that
escapes both masks fails there rather than as an intermittent golden diff.

### 11.13.4 Recording a golden

Both harnesses re-record under the same environment variable, and only the
test you run re-records its own golden:

```
ZICATO_PARITY_UPDATE=1 uv run pytest -q tests/test_workspace_reader_parity.py
ZICATO_PARITY_UPDATE=1 uv run pytest -q tests/test_dashboard_reader_parity.py
```

Re-record only once you can name, for every label that moved, what changed
and why — then put that list in the change's description. A golden re-recorded
to make a red test green is a golden that has stopped being an oracle. Read
`git diff` on the golden before committing it; the labels are sorted, so the
diff reads as a list of affected readers.

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

Worker isolation, forced termination, and configuration delivery to workers
require real subprocesses. Keep those operations in their integration tests.
Bound execution time and assert cleanup; unrelated setup can use direct
calls to the functions that own it.

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

**Step 3 — Mark it `integration`.** The marker documents that a real
process boundary is the point, so nobody later "speeds it up" by stubbing
the subprocess away — that would delete the coverage. Add `slow` ONLY if
`pytest --durations=0` reports the finished test at 15 s or more, and add
its measured row to `tests/test_slow_tier_registry.py` in the same commit.
A bounded worker test (step 2) usually lands in single-digit seconds and
stays in the default tier.

**Step 4 — Set a hard timeout and assert NO LEAK.** The test must bound its
own wait (the worker's budget + the parent's grace + a margin) so a genuine
hang fails the test rather than wedging the suite. And it must assert the
process, the temp checkout, and any child are gone at the end — the
`checkout_ephemeral` conformance tests are the model (`assert list(
_isolated_tempdir.iterdir()) == []` after cleanup; §11.6). A test that
leaks a real subprocess or a `ztw-snap-*` tree is a test that will flake the
NEXT test under xdist.

**Step 5 — Prove the boundary crossing rather than only the outcome.** If the
test is about something crossing INTO the worker (an invocation configuration value), read
it back from INSIDE the worker. `ConfigProbeAdapter` writes the worker's
resolved `load_config()` view to `config_probe.json`, so the test proves the
value crossed via the args file with NO env var involved (§11.5). Asserting
the outcome alone can pass for the wrong reason.

**Verify**

```bash
# Run it in isolation, serially, with the real subprocess:
uv run pytest tests/test_your_worker.py -q -n0
# Read its cost, which is what decides its tier:
uv run pytest tests/test_your_worker.py -q -n0 --durations=0
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
  conventions enforce), §9.16 (the payload-shape clean break that re-records
  the node fixtures + goldens).
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
| the reader snapshot oracle — query layer, epoch ordering | `tests/_reader_parity_harness.py` + `tests/test_dashboard_reader_parity.py` |
| the reader snapshot oracle — analyzer / reflection / health / index / workspace / CLI | `tests/_workspace_reader_parity_harness.py` + `tests/test_workspace_reader_parity.py` |
| the behavior-preserving parity gates | `tools/parity.sh` + `tools/parity/lib/*.py` + `tools/parity/golden/` |
| contract-hash checkout-independence | `tests/test_epoch_contract.py::test_contract_hash_is_cwd_and_checkout_invariant` |
| the CLI surface is canonical | `tools/parity/lib/cli_help.py` (regen: `--update`) |
| the index projection is pure | `tools/parity/lib/test_reindex_golden.py` |
| the whole end-to-end audit bytes | `tools/parity/lib/test_mock_golden.py` + `mock_evolve_capture.py` |
| the library/driver + query-dashboard-free contracts | `pyproject.toml [tool.zicato.importlinter]` → `make import-lint` |
| the retired private paths stay retired | `pyproject.toml [tool.ruff...banned-api]` → `uv run ruff check` |
| the digest / no-op / DOM-identity render discipline | `src/zicato/dashboard/static/test/*.test.mjs` → `make node-test` |
| the node suite renders what the endpoints serve | `tests/_console_scenarios.py` + `tests/test_dashboard_endpoint_table.py` → `static/test/recorded.mjs` |
| the whole thing, reproducibly, in Python, JavaScript, and Rust | `.github/workflows/ci.yml` (default Python tier + dashboard JavaScript + `cargo test`) and `.github/workflows/slow-tier.yml` (statistical and end-to-end oracles) |

`make check` runs the complete verification plan, including parity and
both oracle suites. `make check-fast` runs the iteration selection.
