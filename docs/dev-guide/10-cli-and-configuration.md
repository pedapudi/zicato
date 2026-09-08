# 10 — The CLI, contract preparation, and library boundary

This chapter covers the command surface, the library that prepares and
publishes evaluation inputs, explicit configuration transport, the lazy public
API, and packaging. The browser dashboard reads workspace state and provides
run controls; contract authoring uses files, board-authoring APIs, and CLI
operations.

Read [the contract and epoch chapter](03-contract-and-epochs.md) for identity
and publication, [evaluation statistics](04-evaluation-statistics.md) for
calibration and confirmation, and [the process architecture](02-architecture.md)
for worker ownership.

The invariant identifiers below are retained because other chapters cite them.

| ID | Invariant |
|---|---|
| L1 | Contract operations validate supplied edits through the owning typed declarations before replacing values. |
| L2 | A field's declaration, serialization, identity, runtime consumer, and applicable cost or validation behavior must agree. |
| L3 | Contract cost arithmetic belongs to `contract_draft.operations.estimate_cost`; callers consume its breakdown. Evaluation calls and board runs have different units. |
| L4 | Statistical and cost warnings are advisory. Malformed authored data, stale source snapshots, and competing writers block publication. |
| L5 | Publishing edited contract files does not launch a live evolve run. Contract drift is resolved at the next invocation's epoch boundary. |
| L6 | Workers receive selected operational values and their sources through the explicit configuration payload. |
| L7 | The root package is a lazy facade; exported objects are identical to their owning module's objects. |
| L8 | Library packages do not import drivers. The CLI may launch the dashboard; the dashboard must not import the CLI. |

## 10.0 Drivers and library owners

`zicato.cli` assembles the command tree. `zicato.dashboard` serves browser
views and operator controls. Both consume library APIs; the CLI also launches
the dashboard server. Shared preparation and publication belong below these
drivers, so core execution does not depend on a browser service.

| Owner | Responsibility |
|---|---|
| `contract_draft/draft.py` | In-memory evaluation inputs, captured live source, and semantic differences. |
| `contract_draft/operations.py` | Typed edits, advisory validation, cost estimation, and apply orchestration. |
| `contract_draft/publication.py` | Accepted file bytes, stale-source checks, writer ownership, and recoverable publication. |
| `cli/discovery.py` | Explicit assembly of root commands and advanced namespaces. |
| `config.py` and `core/settings.py` | Operational declarations and immutable resolved configuration. |
| `__init__.py` | Lazy public exports. |

## 10.1 Capturing editable evaluation inputs

`TournamentDraft.from_workspace(root)` reads the registered live contract
paths and captures their bytes for conflict detection. Those files describe
what the next invocation will resolve. A selected epoch's frozen files instead
describe a retained evaluation and belong to `EpochExecutionContract`.

The draft carries scoring, ordered board entries, the brief, proposer path,
board-level drift suppression, and the judge-only flag. Scoring records are
frozen; operations replace them with validated instances. Board-level metadata
must survive a board edit and publication because it affects evaluation.

The captured source belongs to the draft. If a registered path or its bytes
change after capture, applying the draft refuses the stale edit. Re-reading a
changed file and silently treating it as the original source would defeat this
check.

## 10.2 Typed contract operations

`contract_draft.operations` owns edits to structure and tournament parameters,
holdout, proposer declarations, weights and gates, optional experimental
settings, telemetry, mutation policy, board entries, judges, and the brief.
Operations return a `DraftPatch` describing the changed fields.

`authored_edit` validates supplied arguments before equality checks or coercion.
The resulting record also applies its declared field constraints. This keeps
values such as a boolean in an integer field from passing as an unchanged
number. Mapping arguments retain the types declared by the operation that owns
them.

Operations have edit semantics as well as value types. A nullable argument may
mean no edit, and a mapping may replace an entire mapping. Read the owning
signature before changing a caller; do not assume an update merges nested
values. Editable and frozen scoring use the same strict configuration decoder.
Do not add another acceptance policy in an operation.

## 10.3 Estimating evaluation cost

`estimate_cost(draft)` returns a `CostEstimate` with board sizes, the structure,
a board-run estimate, and a tuple of `CostLine` entries. It estimates the
schedule. Physical worker launches and execution cost also depend on cache
hits, early termination, missing candidates, and confirmation outcomes.

The estimator reads the strategy's default replicate count when the contract
does not pin one. It includes the structure schedule, holdout confirmation,
candidate screening, independent crowning confirmation, and any enabled
placebo cadence. Racing sums its growing board slices and final duel.

Independent crowning confirmation can spend up to `budget × 2 × train_entries`
board runs: each draw evaluates both fixed contestants on the train board.
These draws require fresh evidence. Reuse of ordinary selection observations
does not make independent confirmation free.

Best-of-N proposal calls are a separate breakdown line and are excluded from
the board-run total. Proposal calls are model evaluations; board runs measure
the target. Mixing their units would make a total misleading. The estimator
does not promise a complete model-call bill for every critique or repair path.

A change to the execution schedule should update the estimator and a focused
cost check. Preserve the distinction between an upper-bound budget and measured
spend.

## 10.4 Advisory validation and executable checks

`validate(draft, workspace_root=None, noise_floor_max_abs_delta=None)` returns
warnings with stable codes, messages, and severities. It checks structure and
field-size mismatches, thin racing slices, replication, holdout viability,
board-authoring errors, and the margin relative to an available noise estimate.
These warnings inform an operator; their severity alone does not authorize or
block publication.

Structural admission is separate. Strict authored decoding, board validation,
stale-source checks, and writer ownership can refuse an edit before anything
is published. A warning-only API must not be mistaken for the full admission
boundary.

Dotted-path checks inspect syntax without importing the referenced module.
Resolving an authored import can execute code. Setup checks and the board's
runtime boundary own that execution. Live board evaluation requires the
operator's authorization.

Statistical preflight is owned by the epoch and check layers. Those consumers
must use the selected or captured candidate inputs. A draft convenience
wrapper must not become a second implementation of measurement or validation.

## 10.5 Applying edits and recovering publication

`apply(draft, root, confirm=False)` validates the candidate and returns its
semantic differences, predicted hash, cost estimate, and warnings without
writing files. `confirm=True` publishes the accepted contract bytes under a
workspace writer. A caller that already owns the writer passes the same lease.

Publication captures board, brief, scoring, and registration bytes together.
Before preparing an intent, it validates the candidate and checks that every
captured source still matches. A source conflict or malformed candidate
therefore leaves the live files untouched.

`prepare_contract_publication` records accepted bytes and their digests.
`publish_prepared_contract` writes the recoverable intent before replacing the
live files. `recover_contract_publication(root, writer=...)` completes an
interrupted publication under the same ownership rules. Recovery replays the
recorded bytes; it does not resolve a different candidate from changed files.
Read-only resolution detects pending publication and refuses a mixed contract.

After publication, the draft captures the published source again. Applying a
contract does not start workers or launch evolve. The next invocation resolves
the changed evaluation identity and performs any required epoch roll.

## 10.6 Editable and frozen inputs have different owners

Live editing uses `TournamentDraft` and its source snapshot. Execution uses the
selected epoch's immutable `EpochExecutionContract`. A later live edit or a
changed current-epoch marker must not change the contract already selected by
an invocation.

The invocation binds configuration and the selected execution contract after
publication recovery. Rounds reuse those captured inputs until an intentional
roll. Loading verifies the supported captured configuration and required hash.
Changed live executable bytes cannot replace the selected epoch's bindings.

## 10.7 Field declarations and complete configuration

Scoring dataclasses own defaults, persisted names, constraints, and descriptions.
`core.configuration` supplies the shared strict decoder and complete dataclass
serializer. Authored and frozen scoring use the same declarations. Every
effective value participates in contract identity, including nested defaults.
Runtime settings use their domain records and the invocation carrier (§10.10).

An omitted authored value and its explicit default decode identically.
Unknown fields and invalid values are refused. Saved configuration contains
all effective fields; there is no historical-default decoder or omission
metadata. Selected epochs require captured `execution.json` and a valid
64-character lowercase hexadecimal contract hash.

## 10.8 Adding or changing a contract field

1. Define the field on its owning record with its type, default, constraints,
   description, and persisted identity behavior.
2. Pass the resolved value to the actual consumer. If an operation edits it,
   validate the supplied value before converting or comparing it.
3. Update schedule cost or advisory checks where the field changes them.
4. Verify malformed input refusal, a meaningful non-default consumer case,
   complete serialization, and contract identity. Reject unsupported record
   shapes rather than introducing a second decoder.
5. Regenerate affected configuration artifacts and command help from their
   owning declarations.

Choose focused tests at the changed boundary. A new field does not require a
second validation framework or repeated complete-loop simulations.

## 10.9 The CLI command contract

`cli.discovery.build_cli_root` explicitly assembles the command hierarchy.
Adding a file under `cli/commands` does not publish a command automatically.
The ordinary path is `zicato init` followed by `zicato evolve`; advanced
namespaces expose board, epoch, proposer, tournament, inspection, and repair
operations.

The hand-authored [CLI contract](../design/CLI.md) describes this product
surface. `tools/parity/golden/cli_help.txt` is the generated exhaustive help
record. A command change updates both, its operator skills, and focused CLI
checks. Regenerate help with:

```sh
uv run python tools/parity/lib/cli_help.py --update
```

`zicato proposer scorecard` reads proposal quality. Reflection commands under
`zicato inspect reflection` produce diagnostics and evaluation suggestions.
Neither exposes a contract-edit queue. The dashboard's Contract and Models
sections are read-only; Appearance changes browser preferences.

## 10.10 Explicit configuration reaches each worker

The authored `config.json` root is `workspace.config_schema.WorkspaceDeclaration`.
It composes runtime, health, integration, dashboard, model, proposer, adapter,
and contract-source declarations. The file owner validates unknown keys, exact
JSON types, ranges, and relationships before returning typed values or publishing
an edit. Scoring is a separate document owned by `ScoringWeights`. Authored and
frozen scoring use the same strict decoder and complete serializer.

Operational field declarations live in `core/settings.py`. `zicato.config`
exports the domain records, immutable `InvocationOverlay`, and
`resolve_configuration(workspace_config, overlay=...)`. The resolver applies
field defaults, workspace values, and explicit invocation values in that order.
Unknown fields, wrong types, and invalid ranges raise before conversion.

An overlay contains only the fields an invocation explicitly supplies. Its
nested mappings and sequences are detached from caller-owned inputs and frozen.
The resolved object carries typed `values` and a source for every persisted
field: `default`, `workspace`, or `invocation`. Constraints between fields run
after composition, so an overlay can use a related value from the workspace.
Execution identity declarations and run paths cannot be changed by an
operational overlay.

### 10.10.1 Runtime construction and transport use selected values

The CLI and public evolve functions pass an explicit overlay into the validated
invocation scope. The scope acquires the writer, recovers pending contract and
epoch publication, and resolves the workspace settings once. The CLI stages any
tournament edit after recovery and publishes it under that writer before
validation. A dry run checks its candidate in memory and never recovers or
publishes a pending edit. Child services start only after validation succeeds.

`InvocationContext.configuration` retains the resolved values and sources.
`make_runtime_config(..., configuration=resolved)` copies operational fields
from their shared declaration and retains the same object on each round's
runtime. Callables and model roles resolve from the invocation's captured
workspace declaration. Token ledgers remain local to each round.

The selected epoch supplies captured board, scoring, brief, proposer skills,
and adapter declarations through `EpochExecutionContract`. Explicit epoch
selection uses that epoch's driver import roots. Rounds retain the same contract
object and verify its executable dependencies before execution. An intentional
contract roll waits for worker cleanup, releases the previous import scope, and
binds the replacement epoch. Operational settings remain fixed for the invocation.

Telemetry browser and native endpoints are explicit runtime values. Workers
receive both addresses with the resolved settings; the coordinator does not
change its environment to forward them.

The runner writes `configuration` into each worker argument file using
`_configuration_spec(config)`. The payload contains the selected values and
their sources. `ResolvedConfiguration.from_json` validates both before the
worker constructs its runtime. Evaluation consumers receive the selected
`AuxConfig`, including rubric judging and emulated turns.

`effective_settings` reports the carried configuration. The host worker limit
also records the effective CPU-derived count when its declared value is null.
The report does not re-read flags or workspace files. `load_config` constructs
an explicit configuration over defaults and holds no process-wide overrides.

### 10.10.2 Environment inspection describes process boundaries

`zicato inspect environment` reads `describe_env_vars()`. Entries describe the
child context inheritance, credentials, worker
environment construction, and operating-system inputs. The module/function
inventory in `ENVIRONMENT_BOUNDARIES` is checked against parsed Python access
sites by `tools/check_environment_boundaries.py`; new undeclared access fails.
Credential values are read only by their named boundary owners.

Telemetry endpoints are invocation values, selected from explicit integration
settings, inherited context, or the workspace service record. Browser and native
addresses and required run coordinates travel together in `WorkerRuntimeContext`.
Harnesses read their scratch directory from `config.run_context.scratch_dir`.
Nested target processes read that same record through `inherited_runtime_context()`.
A worker sets one internal
`ZICATO_RUNTIME_CONTEXT` pointer to its own argument file before target imports;
nested processes inherit that pointer. A configured missing or malformed pointer
raises an error. Coordinators never set it, and each argument file must survive
until the worker and its descendants are reaped.

### 10.10.3 Inspect authored values and generate configuration artifacts

`zicato inspect config FIELD` explains a dotted field, including model-engine
names such as `models.engines.judge.api_key_env`. `--schema` emits closed editor
schemas for both configuration files. `--scaffold` emits sparse defaults;
`--scaffold --complete` spells out class defaults without inventing executable
paths or connections. These documents describe an unregistered workspace until
registration supplies its required target and proposer inputs.

`--effective --sources --workspace .zicato` resolves the next invocation's
workspace values and defaults without starting services or reading credentials.
A running invocation's settings remain on its heartbeat. `--reference` renders
the field reference from the same descriptions, types, defaults, bounds,
persisted paths, CLI bindings, scopes, epoch effects, and secret-reference flags.
Adding a field updates these outputs through its declaration.

### 10.10.4 Add an operational field at its owner

1. Declare the field, default, type, and constraint on its domain record. The
   parser, schema, runtime field inventory, and worker transport derive those
   facts from the declaration.
2. If a CLI flag is needed, preserve an unset value and map only explicit
   input into `_configuration_overlay`.
3. Pass the selected domain record to its consumer. Runtime consumers use the
   configuration attached to their runtime.
4. Verify a malformed value is rejected and an explicit value reaches its
   actual consumer. Worker settings require a process-boundary check.

The focused configuration checks are `tests/test_authored_config.py`,
`tests/test_invocation_configuration.py`, and `tests/test_cli_config_flags.py`.

---

## 10.11 The library facade and the import boundary

zicato is a library first. The public surface is declared in
`src/zicato/__init__.py` as a **lazy facade**: a dict mapping each public name to
its home module, resolved on first access by a module-level `__getattr__`. The
surface is limited to evolve entry points, harness protocols, board/config
loaders, and scoring types. All three evolve entry points name
`zicato.orchestrator` as their home — the dispatch surface over the round
pipeline, so the facade pins no module inside `zicato.evolve`. The
reasoning-aware model boundary is an advanced API at `zicato.reasoning`.
`__all__` is derived from `_EXPORTS`.

Advanced APIs live in their owning subpackages, and the facade carries no
forwarding aliases to them.

### 10.11.1 The laziness contract

```python
# src/zicato/__init__.py — the lazy resolver
def __getattr__(name: str) -> Any:
    """Resolve a facade name lazily from its home module."""
    try:
        module_name, attr = _EXPORTS[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    module = importlib.import_module(module_name)
    value: Any = module if attr is None else getattr(module, attr)
    globals()[name] = value  # cache: subsequent access skips __getattr__
    return value
```

Two properties are load-bearing (the lazy-pure-facade rule), and both are
machine-pinned in `tests/test_public_api.py`:

- **`import zicato` imports only `zicato`.** The module body imports nothing but
  `importlib` and `typing`; every export resolves lazily on first touch. This is
  what keeps the CLI's fast `--help` path cheap — help must not pay for the
  orchestrator. The test runs a *fresh interpreter* and asserts the only
  `zicato*` entry in `sys.modules` after `import zicato` is `zicato` itself:

```python
# tests/test_public_api.py — the laziness pin
    loaded = json.loads(out.stdout)
    assert loaded == ["zicato"], f"import zicato eagerly pulled: {loaded}"
```

- **Every facade name is `is`-identical to its home attribute.** The facade is a
  pure re-export layer, never a fork:

```python
# tests/test_public_api.py — the identity pin
    for name, (module_name, attr) in zicato._EXPORTS.items():
        exported = getattr(zicato, name)
        home = importlib.import_module(module_name)
        expected = home if attr is None else getattr(home, attr)
        assert exported is expected, (...)
```

The `TYPE_CHECKING` block at the bottom of `__init__.py` re-imports every name
in redundant-alias form (`from … import X as X`) so mypy and IDEs see the surface
that `__getattr__` provides only at runtime. Because it is under
`if TYPE_CHECKING:`, none of it runs at import time — laziness is preserved.

### 10.11.2 How to add a public name

There is no CONTRIBUTING doc; the procedure is code-enforced by three
touch-points, and the tests catch a half-done addition:

1. **Facade entry.** Add a `"name": ("home.module", "attr")` row to `_EXPORTS`
   in `src/zicato/__init__.py`. (`__all__` and `__dir__` update automatically —
   they are derived from `_EXPORTS`.)
2. **`TYPE_CHECKING` mirror.** Add the matching `from home.module import attr as
   attr` under the `TYPE_CHECKING` block, or mypy/IDEs will not see the name.
3. **Identity test — already automatic.** `test_every_facade_name_resolves_to_
   its_home_module` iterates `_EXPORTS`, so it covers your new row the moment you
   add it; `test_all_lists_exactly_the_declared_surface` pins `__all__`. A name
   in `_EXPORTS` but not `TYPE_CHECKING` (or vice-versa) is caught by these plus
   mypy.

> ✅ ALWAYS put a name on the facade ONLY if it is a genuine driver-facing seam.
> The facade is the *declared* public surface: the evolve loop, harness
> protocols, board/config loaders, and scoring types. Other APIs remain
> reachable at their owning subpackages. Do not add forwarding aliases.

### 10.11.3 Import boundaries

Library code must not import the CLI or dashboard. The CLI may launch the
dashboard, while the dashboard cannot import CLI handlers. Shared behavior
belongs in the library owner that both drivers can call.

`pyproject.toml` declares import contracts, including the dashboard-to-CLI
prohibition, the query layer's dashboard independence, and the proposer patch
validator's isolation from board and evaluation code. The repository's import
checks also verify library-to-driver boundaries. Run `make import-lint` and the
relevant source-policy checks after changing an import edge.

A banned private import is evidence of a misplaced dependency. Use the public
owner or move the shared behavior below its consumers; do not add an alias in a
driver to bypass the boundary. The explicit banned-API declarations are under
`tool.ruff.lint.flake8-tidy-imports.banned-api` in `pyproject.toml`.

## 10.12 Packaging

zicato is a hatchling-built wheel with a `src/` layout. Four packaging facts an
extender touches:

**Extras.** Narrow profiles expose individual integrations. `observability`
composes the browser dashboard and live telemetry;
`all` installs every shipped runtime feature. The base retains the core loop
and JSONL telemetry. `docs/design/INSTALL-PROFILES.md` is the profile contract.
The all-extras sync rule (`01-orientation.md §4`) applies here: **always `uv
sync --all-extras`** — a bare `uv sync` deletes dev tooling from `.venv`.

**The uv workspace member.** The vendored dogfood targets under `examples/` are a
separate distribution (`zicato-examples`), declared as a uv workspace member so
`uv sync --all-extras` installs it editable alongside zicato — no PYTHONPATH
hacks. The tests import it as `zicato_examples.*`; it is never shipped in the
zicato wheel:

```toml
# pyproject.toml
[tool.uv.workspace]
members = ["examples"]

[tool.uv.sources]
zicato-examples = { workspace = true }
```

This is the precedent for adding another vendored distribution: make it a
workspace member and map it in `[tool.uv.sources]`.

**The wheel + its exclusions.** The wheel ships `src/zicato` (hatchling strips the
`src/` prefix) and *excludes* `src/zicato/dashboard/static/test` — the JS test
harness is a developer tool, run by `tests/test_dashboard_js.py`, and stays out
of the shipped bundle.

**The supervisor binary — `_bin` ownership.** `zicato evolve` spawns a compiled
`zicato-supervisor` watchdog. To make it available to every install, a custom
hatch build hook compiles the Rust crate and bundles the binary at
`zicato/_bin/zicato-supervisor` inside the wheel:

```python
# hatch_build.py — SupervisorBinaryBuildHook.initialize (tail)
        dest = root / _BUNDLED_REL
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(built, dest)
        dest.chmod(0o755)
        ...
        # force-include guarantees the binary lands in the wheel even
        # though it is generated (not VCS-tracked). The wheel path is
        # zicato/_bin/zicato-supervisor (src/ prefix stripped).
        artifact = str(dest)
        build_data.setdefault("force_include", {})[artifact] = "zicato/_bin/zicato-supervisor"
```

The hook is **best-effort by design**: no `cargo`, or a missing crate (e.g. an
sdist that excluded `crates/`), logs a warning and leaves the wheel without the
binary. The CLI's `_resolve_supervisor_binary` then falls back to the
`--supervisor-binary` flag, the system `PATH`, and — for checkouts — the
workspace `target/release/` walk. The sdist carries the crate source
(`crates`, `Cargo.toml`, `Cargo.lock`, `hatch_build.py`) so a downstream wheel
build can still run the hook.

> ⚠️ TRAP — the supervisor binary is generated rather than VCS-tracked, so it
> must be `force_include`d (a package glob will not match it) or it never lands
> in the wheel. It is owned by exactly one build target (the wheel); the sdist
> carries source instead and rebuilds at wheel-build time. See 08-supervisor.md
> §8.11 "Build, packaging, and binary resolution" for the runtime fallback
> chain, and the wheel-split row of the deferred register
> (14-goals-and-roadmap.md §4) for the split into `zicato-lib` / `zicato-cli` /
> `zicato-dashboard`, where exactly one wheel would own `_bin/`.

---

## 10.13 Cross-references

- [Contract and epochs](03-contract-and-epochs.md): semantic hashes, selected
  inputs, writer ownership, and publication recovery.
- [Evaluation statistics](04-evaluation-statistics.md): calibration,
  independent confirmation, and the distinction between evidence and cost.
- [CLI contract](../design/CLI.md): command locations and help generation.
- [Dashboard](../design/DASHBOARD.md): HTTP, event streams, and read-only views.
- [Installation profiles](../design/INSTALL-PROFILES.md): optional interfaces
  and runtime integrations.
- [Contract field registry](../design/CONTRACT-FIELD-REGISTRY.md): declared
  defaults, complete serialization, and contract identity.
