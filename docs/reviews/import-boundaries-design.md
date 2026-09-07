# Exhaustive namespace ownership for import checks

Status: implemented in the accompanying changes for [issue #410](https://github.com/pedapudi/zicato/issues/410), pending integration verification.
The source observations refer to revision `3bddf6424e5b13287fa9adaee34e73a97cb62457`.
The intended reader maintains the Python library and its development checks.

The namespace inventory makes import restrictions exhaustive. Reporting consumes
the domain-owned brief parser. The checks retain transitive restrictions and
the existing permitted driver imports.

## Approval already covers the architectural direction

The issue initially requires design approval before implementation. The
[recorded approval](https://github.com/pedapudi/zicato/issues/410#issuecomment-5520548643)
states: “The proposed contracts are approved as written.” It approves the
execution and primitive restrictions, retains `runtime_factory` in the execution
restriction, and rejects weaker direct-only substitutes for failing transitive
restrictions. The approved contract-draft extraction is present in the source.

The [inventory refinement](https://github.com/pedapudi/zicato/issues/410#issuecomment-5554870418)
requires exhaustive role assignment and one brief-goal extraction implementation.
The role inventory and check integration below implement those requests.
They add no operator-facing configuration.

## Every namespace receives one declared role

An import graph built from this revision contains 374 modules and 1,661 internal
imports. The production package has 44 immediate child namespaces: packages with
Python modules and standalone Python modules. Its root initializer is considered
separately because naming `zicato` as an import-linter source also names every
descendant.

The table assigns each child namespace one role. A primitive may import other
primitives. Execution code may import primitives and other library code allowed
by the existing restrictions. Coordination code assembles operations or views.
Mixed library code has an existing dependency that prevents its inclusion in the
execution restriction. Every library role is forbidden from importing a driver.

| Namespace under `zicato` | Role | Responsibility |
| --- | --- | --- |
| `_tournament_worker` | Mixed library | Run one evaluation in an isolated process. |
| `adapter_factory` | Execution | Resolve the registered target adapter. |
| `adapters` | Execution | Adapt target execution to the evaluation interface. |
| `analyzer` | Coordination | Gather and render evaluation reports. |
| `aux_timeout` | Primitive | Bound auxiliary call duration. |
| `board` | Execution | Load, validate, partition, and prepare evaluation tasks. |
| `builder` | Driver | Serve contract-editing requests. |
| `check` | Coordination | Assemble workspace validation results. |
| `cli` | Driver | Parse commands and launch library operations or consoles. |
| `config` | Primitive | Load process configuration. |
| `contract_draft` | Coordination | Store and apply edits to evaluation-contract drafts. |
| `core` | Execution | Declare domain types and shared contract values. |
| `dashboard` | Driver | Serve HTTP resources and browser assets. |
| `emulator` | Execution | Run task judging and simulated counterpart behavior. |
| `epoch` | Mixed library | Persist contracts, generations, and lineage; manage epoch lifecycle. |
| `evolve` | Coordination | Execute rounds and settle their recorded decisions. |
| `example_workspace` | Execution | Construct target-example workspace content. |
| `health` | Coordination | Gather diagnostics and describe workspace failures. |
| `import_path` | Primitive | Resolve dotted Python imports. |
| `index` | Mixed library | Build and maintain the derived analytical database. |
| `integrations` | Primitive | Load optional external integration interfaces. |
| `judge_runtime` | Execution | Execute judges and capture their inputs and outputs. |
| `logging_stream` | Primitive | Write structured operator logs. |
| `models_config` | Execution | Resolve configured model-backend roles. |
| `mutation` | Execution | Enumerate, validate, and apply structured source edits. |
| `orchestrator` | Coordination | Expose the public round-operation facade. |
| `patterns` | Execution | Derive feedback patterns from recorded measurements. |
| `proposer` | Mixed library | Prepare, execute, validate, and record proposed edits. |
| `query` | Coordination | Assemble workspace read models. |
| `reasoning` | Execution | Apply configured call-reasoning settings. |
| `reflection` | Coordination | Evaluate and propose evaluation-contract improvements. |
| `runtime` | Mixed library | Own process state, controls, locking, and restart cleanup. |
| `runtime_factory` | Execution | Assemble effective runtime services and settings. |
| `scoring` | Execution | Compute configurable score transformations. |
| `selection` | Execution | Rate candidates and resolve tournament decisions. |
| `storage` | Primitive | Read and write storage records atomically. |
| `synthetic` | Execution | Prepare synthetic target behavior and mutable overlays. |
| `telemetry` | Execution | Capture execution telemetry and reduce it to loss records. |
| `testing` | Execution | Supply deterministic target and test-support functions. |
| `tournament` | Mixed library | Schedule evaluations, cache completed units, and apply governance. |
| `tui` | Driver | Render workspace data obtained through HTTP. |
| `util` | Primitive | Supply text previews, timestamp formatting, and observable best-effort error handling. |
| `workspace` | Execution | Resolve artifact locations and enumerate canonical records. |
| `workspace_loader` | Mixed library | Assemble workspace configuration and resolved inputs. |

The role counts are seven primitives, eighteen execution namespaces, eight
coordination namespaces, seven mixed library namespaces, and four drivers.
The root initializer remains an explicit facade exemption from package-level
contracts. A check of its actual imports must forbid a path from that exact
module to a driver; the exemption must not cover child modules.

## Derive restrictions from the role inventory

The development configuration contains one namespace-to-role mapping.
The `tools/check_imports.py` command discovers immediate namespaces from
production Python files and requires equality with the mapping before checking imports. A renamed,
removed, duplicated, or unclassified namespace fails with the relevant path.
Discovery includes directories containing Python source even without an
initializer, so an implicit namespace cannot escape inspection.

Derive the three repeated package restrictions from that mapping:

| Restriction | Sources | Forbidden destinations |
| --- | --- | --- |
| Library code cannot import drivers. | All forty non-driver children. | All four drivers. |
| Execution code cannot import coordination or drivers. | Primitives and execution namespaces together. | Coordination namespaces and drivers. |
| Primitives cannot import the rest of the library. | All seven primitives. | Every other child namespace. |

Keep the six existing specialized restrictions as explicit declarations:

- The terminal console cannot import the CLI, dashboard, or builder.
- The dashboard cannot import the CLI.
- The builder cannot import the CLI or dashboard.
- The CLI cannot import the builder directly; its existing path through the
  dashboard remains permitted.
- The proposer patch validator cannot reach board-loading or task-execution
  capabilities named by its existing contract.
- The query package cannot import the dashboard.

The CLI imports the terminal console. Preserve that allowed edge.
The terminal console's existing prohibition on other drivers is not a complete
prohibition on reading workspace state; do not describe it as such.

The command validates discovery, expands the repeated declarations, and invokes
import-linter with a temporary configuration. Import-linter checks transitive
paths and reports violations. Its configuration is generated input; the
namespace mapping and specialized declarations remain the only committed facts.
The command propagates import-linter's failure status.

`make import-lint` invokes the command. The specialized declarations live under
`tool.zicato.importlinter` so a stale direct `lint-imports` invocation cannot
report partial success without checking the generated restrictions. The import
check owned by [issue #498](https://github.com/pedapudi/zicato/issues/498) invokes
the complete command once.

## The unclassified example package can enter existing restrictions

At the inspected revision, `example_workspace` was absent from every contract's
source list. Its imports reach `core`, `mutation`, and dependency-neutral
proposer modules. The graph contains no path from it to coordination code or
drivers, so its assigned execution role is enforceable.

A temporary configuration added `zicato.example_workspace` to the existing
library and execution source lists. Running `lint-imports --no-cache` against
that configuration kept all nine contracts with zero broken contracts. This is
direct evidence that classifying the package as execution code needs no code
movement.

The mixed library role records actual exemptions. For example, `epoch.preflight`
imports `evolve.generation_phase`, while `epoch.analysis` imports reporting.
Receipt extraction can remove specific imports from index and diagnostic readers;
it cannot establish a package-wide restriction by itself. Recompute the graph
before changing any mixed library assignment.

## Brief parsing belongs with the proposer brief

The brief parser in `proposer/brief.py` owns goal extraction. The function takes
text and returns the full first prose paragraph under a goal heading, or `None`.
It performs no filesystem access and imports no query, reporting, or round code.

Preserve the existing rules for case-insensitive headings, hard-wrapped lines,
word-ending hyphens, punctuation hyphens, and paragraph termination by headings,
blank lines, lists, blockquotes, and fences. These are observable display rules;
the extraction must not silently adopt a different Markdown parser.

Both query and reporting preserve the existing preview policy: strip surrounding
whitespace, keep at most 120 characters, and append `...` if text was truncated.
Reporting converts absence to an empty string at report assembly. The text
preview operation in `util/text.py` supplies the shared default limit.
The report-data forwarding helper and query's parser copy are removed.

The direct `analyzer.report_data` import of `query.epoch_view` is removed.
Only assert a broader analyzer-to-query restriction after a fresh transitive
check proves it. Removing this helper does not prove that every reporting cycle
has disappeared.

## Integration and verification

1. Add the namespace inventory and generated configuration command without
   moving production code. Include `example_workspace` in the derived sources.
2. Verify actual discovery equality, root-facade handling, all existing
   restrictions, and the expected driver permissions.
3. Move brief extraction and its direct tests in a separate code change. Keep
   one independent expected paragraph and display-limit example for each view.
4. Recompute the graph and add only restrictions supported by its resulting
   paths. Follow receipt extraction with the same measurement discipline.

Use tiny fixture packages to prove that an unclassified namespace fails, that
a classified namespace with a forbidden import fails, and that an indirect
forbidden path fails. Include a standalone module and an implicit namespace
directory. Verify the permitted CLI-to-dashboard-to-builder chain and direct
CLI-to-terminal import. Inject an import-linter failure and require a failing
command status.

Retain the behavioral cases in `tests/test_brief_goal_hard_wrap.py` while moving
parser-focused assertions to the owner. Add long-paragraph and absent-goal view
checks with independently written expected results. This work needs no worker
subprocesses, full-round fixtures, or new statistical executions.
