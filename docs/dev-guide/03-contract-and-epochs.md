# 03 — The Contract and Epochs

> **Covers:** contract identity, complete configuration serialization, strict
> loading, captured execution inputs, epoch publication and recovery, and lineage.
> **Prerequisites:** [workspace layout](01-orientation.md#26-the-workspace-on-disk)
> and the [round pipeline](02-architecture.md).

An epoch seals the evaluation rules that make its generations comparable.
The contract hash identifies those rules. A generation records the mutable
source being evaluated under them.

The eight invariants are:

1. **Identity is independent of checkout location.** Moving equivalent inputs
   to another checkout must preserve their hash.
2. **All scoring configuration values enter the hash.** Defaults are effective
   values and appear in the canonical configuration.
3. **Serialization is complete.** The shared dataclass serializer writes every
   declared field, including nested defaults, under its declared persisted name.
4. **Grading source is part of identity.** Editing a predicate, judge, scoring
   plugin, or outcome summarizer changes the contract.
5. **Measurements do not redefine the contract.** Noise-floor results,
   preflight results, recovery records, and operational concurrency remain
   outside contract identity. Captured execution roles are contract inputs.
6. **A selected epoch must have a valid identity.** Its contract hash contains
   exactly 64 lowercase hexadecimal characters, and `execution.json` is required.
7. **Readers enforce the supported record shape.** Missing required fields,
   malformed values, and unsupported records cannot authorize execution.
8. **Lineage distinguishes pending and settled decisions.** Promotion is `null`
   while unresolved, `true` after promotion, and `false` after rejection.

## 3.0 Map of the subsystem

Paths below are relative to `src/zicato/`.

| Owner | Responsibility |
|---|---|
| `core/configuration.py` | Shared strict dataclass decoder, complete serializer, and generated schema |
| `core/scoring_config.py` | Scoring declarations, defaults, constraints, and the derived field registry |
| `core/epoch.py` | `EpochConfig`, including validation of its required contract hash |
| `epoch/contract.py` | Contract inputs, canonical forms, full hash, and component hashes |
| `scoring/plugins.py` | Grading callable identity through `spec_with_source_hash` |
| `epoch/execution.py` | Captured execution bindings and verification of a selected epoch |
| `epoch/lifecycle.py` | Epoch preparation, publication, closure, and configuration writes |
| `epoch/publication.py`, `epoch/baseline.py` | Durable publication intents and baseline recovery |
| `evolve/epoching.py` | Compare the live contract with the selected epoch and handle drift |
| `epoch/lineage.py` | Typed cross-epoch topology and promotion decisions |
| `epoch/journal.py` | Accepted experiment bodies, patches, and the journal projection |
| `workspace/layout.py`, `epoch/_storage.py` | Record locations and storage keys |

## 3.1 The evaluation contract

`compute_contract_hash` combines seven canonical forms. Component hashes expose
these same forms separately so drift messages can identify the changed input.

| Component | Meaning |
|---|---|
| `board` | Tasks, expectations, judges, and board-level grading controls |
| `brief` | The operator's proposer brief |
| `scoring` | Complete effective scoring and decision configuration |
| `evaluator_revision` | The explicit evaluator implementation revision |
| `adapter` | Worker reconstruction, captured execution roles, and immutable implementation source |
| `mutable_trees` | The declared paths whose contents may vary between generations |
| `proposer` | Proposer implementation, tools, skills, and declared checks |

Mutable candidate source belongs to the generation. Implementation outside that
surface belongs to the contract when it determines reconstruction or grading.
For example, changing a mutable prompt creates a candidate; changing the
predicate that scores that prompt changes the evaluation contract.

### 3.1.1 The on-disk epoch layout

Each epoch stores its accepted inputs under `epochs/<epoch_id>/`:

```text
board.jsonl               frozen board
brief.md                  frozen proposer brief
scoring.json              complete effective scoring configuration
execution.json            captured execution bindings
config.json               EpochConfig, including the required contract hash
contract_components.json  component hashes used to explain drift
journal.md                experiment journal projection
generations/              candidate records, source snapshots, and measurements
```

The workspace's `current_epoch` marker selects an epoch. It does not supply
missing inputs for that epoch. `EpochExecutionContract` keeps the selected
epoch's coordinates and captured bytes, independent of later marker changes.

## 3.2 The canonicalizer, component by component

Canonicalization removes irrelevant representation differences while retaining
values that determine evaluation. Use the existing component owners when adding
an input; a second parser can accept a different contract from the executor.

### 3.2.1 Board — `_canon_board`

The board owner loads entries and metadata in one accepted document.
Canonicalization sorts entries by id and serializes their typed values with
sorted JSON keys. Reordering rows or JSON keys therefore preserves identity;
changing task text, expectations, or grading budgets changes it.

Board-level judges, `disable_drift`, and `judge_only` also participate.
Predicate and Python-judge specifications include their source identities.
The board owner enforces the grading constraints before hashing; a malformed
present board cannot become an empty board. Contract preparation translates
that refusal into its public `ValueError` boundary.

### 3.2.2 Brief — `_canon_brief`

The brief canonicalizer normalizes line endings, trailing whitespace, and blank
lines at the edges. Substantive text remains part of the contract. The authored
path is `contract.brief_path`, with `brief.md` as its default. The brief reader
has one filename policy shared by execution and query consumers.

### 3.2.3 Scoring — `_canon_scoring` and `scoring_to_canon`

Scoring JSON passes through `scoring_weights_from_dict`, then the complete
serializer in `core.configuration`. Nested configuration uses the same path.
Every declared field enters the canonical mapping, including values equal to
their defaults. An omitted authored field and an explicitly supplied default
have the same identity because both decode to the same effective value.

`scoring_to_canon` adds source identity for grading plugins.
`scoring_contract_to_canon` also uses a declared integration's configuration
owner to normalize its document and include its implementation identity.
The integration owns its schema; the generic scoring record does not duplicate
that schema.

### Evaluator revision — `_canon_evaluator_revision`

The evaluator revision identifies implementation changes to measurement and
decision semantics. It participates even when the operator's files are unchanged.
An integration's implementation revision participates when that integration is
part of the contract.

### 3.2.4 Adapter — `_canon_adapter`

Adapter identity includes the accepted worker reconstruction document and
implementation source outside the mutable trees. Captured execution roles and
their callable or factory sources also contribute here. A runtime callable
cannot replace a prepared role while retaining the epoch's identity.

Execution bindings are captured in `execution.json`. Workers reconstruct their
accepted settings from those bindings. Loading a selected epoch checks its
captured inputs and implementation identity; it does not reconstruct missing
bindings from live workspace settings.

### 3.2.5 Mutable trees — location-independent identity

The mutable-tree canonicalizer sorts and normalizes declared paths without
resolving them against the process working directory. Absolute storage paths
and checkout locations are not contract identity. Keep source identities
separate from the filesystem locations used to load those sources.

The contents of mutable trees remain generation data. Including those contents
in the contract would start an epoch for every proposed edit and prevent
comparison with the parent.

### 3.2.6 Proposer — `_canon_proposer`

The proposer component includes its resolved specification, implementation,
tool names, skill contents, and declared static checks. Skills are captured
before publication so the hash and execution use the same accepted values.
A skill-body edit changes identity even when its filename stays the same.

### 3.2.7 Sensitivity and stability

| Change | Contract result |
|---|---|
| Reorder board rows or JSON keys | Same identity |
| Normalize brief line endings or trailing whitespace | Same identity |
| Omit a scoring field or spell out its effective default | Same identity |
| Move equivalent inputs to another checkout | Same identity |
| Change a scoring value, grading source, or captured execution role | Different identity |
| Change a declared default used by sparse authored configuration | Different effective configuration and identity |
| Record a measured noise floor or recover an interrupted publication | Same identity |

## 3.3 Shared grading source identity

`scoring.plugins.spec_with_source_hash` identifies a grading callable by its
specification and resolved source. `epoch.contract._canon_dotted_spec` applies
that mechanism to predicates, Python judges, scoring plugins, and the outcome
summarizer. Keep these consumers on the same source owner.

A test that changes only a dotted name cannot detect missing source hashing.
The regression must also edit a callable body while keeping its specification
unchanged, then verify that the relevant component and full contract hash change.

## 3.4 Complete effective configuration

Defaults are configuration values. Serialization and hashing retain them,
including disabled features and empty nested settings. There is no default-value
omission registry or separate compatibility representation.

Adding a field or changing a default can change contract hashes. That is an
identity change to review and validate, not a reason to hide the field from
canonicalization. Supported records use the same declarations and decoder as
authored configuration. There is no historical-default decoder or promise to
preserve hashes from another configuration shape.

## 3.5 Serializer completeness

`core.configuration.dataclass_to_jsonable` writes every dataclass field under
its `persisted_name` metadata, when declared, or its Python name otherwise.
It recursively handles nested dataclasses, mappings, sequences, enum values,
and paths. `ScoringWeights.to_json` and epoch configuration publication use it.

`authored_dataclass_from_json` supplies the shared strict decoder.
`scoring_weights_from_dict` adds scoring's declared preparation rules and uses
that decoder. Unknown fields, missing required fields, and invalid types or
constraints produce a configuration error identifying the failing path.
Optional authored fields use their declared defaults before serialization.

The Python field `tournament_structure` has the declared persisted name
`tournament`. Readers, writers, schema generation, and hashing derive that
spelling from the field declaration. Do not add a second spelling in a caller.

A serialization test should exercise a meaningful non-default value through
write, read, and its consuming boundary. Verify that the frozen snapshot retains
it and that a causal value changes identity. Field enumeration alone cannot
prove that a worker actually consumes the transported setting.

## 3.6 `EpochConfig` and recorded measurements

`EpochConfig.contract_hash` is required and validated as a 64-character lowercase
hexadecimal digest. Missing, null, empty, uppercase, or malformed hashes are
invalid configuration. None of these states means that the epoch always matches.

The record also stores its location, creation and closure information, goal,
and measured results. Serializing all of `EpochConfig` does not make every
record field a contract input. `epoch.contract` selects the evaluation inputs;
paths that locate records, timestamps, and lifecycle state remain outside them.

### 3.6.1 Noise floor and preflight

`set_epoch_noise_floor` and `set_epoch_preflight` persist measurements after
execution. Their values do not enter the contract hash. Configuration that
controls evaluation is distinct from the result measured under that
configuration. Publication intents, receipts, index revisions, and recovery
progress are likewise records of execution, not evaluation rules.

## 3.7 Computing the hash

`compute_contract_hash` canonicalizes the seven forms, joins them with the
module's fixed separator, and computes SHA-256. `compute_component_hashes`
uses the same component semantics. Keep both paths aligned when adding an
input so drift diagnostics explain the hash that execution checks.

`compute_contract_hash` also verifies captured inputs under their declared
driver import scope. Authored and saved settings use the same decoder,
canonicalizer, and defaults. Required captured execution bindings accompany
the frozen files.

## 3.8 The epoch lifecycle

### 3.8.1 `new_epoch` — retain, publish, and switch

Preparation validates accepted inputs and retains the bytes and source
identities needed for publication. It completes before reconciling or closing
the preceding epoch. Invalid authored input must leave the active epoch intact.

The workspace publication intent records final coordinates and retained
prepared content. Recovery runs under the held writer and resumes publication
from that intent. Prepared directories sit outside completed-epoch enumeration.
The `current_epoch` marker switches only after the required canonical records
have been published.

Baseline generation creation has its own retained intent. `epoch.baseline`
validates prepared source and finishes generation, experiment, and lineage
publication. Recovery uses the retained source and recorded coordinates even
if the live source tree changes.

### 3.8.2 `close_epoch` — closure and analysis

Closure records the closed state and updates lineage. Analysis is derived from
the canonical records. Its publication cannot establish or revise a promotion
decision. Recovery must preserve the recorded closure time and avoid duplicating
already completed work.

### 3.8.3 Auto-roll — `ensure_epoch_for_contract`

The invocation resolves accepted contract inputs, computes their identity, and
compares it with the selected epoch's required hash. Equal hashes retain the
epoch. Drift starts an epoch when automatic rolling is enabled; otherwise it
raises an error naming the changed components. An explicitly selected epoch
must satisfy its own captured contract before execution.

### 3.8.4 Refuse incomplete execution identity

Loading a selected epoch requires its frozen board, brief, scoring, and
`execution.json`, together with a valid `EpochConfig`. Missing execution
bindings or malformed configuration must fail before durable execution or cache
reuse. A current workspace declaration is not a substitute for a missing
captured declaration.

## 3.9 Lineage semantics

`epoch.lineage` owns typed lineage records and their codec. Consumers use
`Lineage`, `LineageEpoch`, and `LineageGeneration`, or the owner's `to_dict`
projection when a mapping is required. They do not decode raw topology again.

Promotion has three meanings: `None` means unresolved, `True` means promoted,
and `False` means rejected. An unresolved update cannot overwrite a settled
decision. The query layer uses canonical lineage to find the reigning champion
and to reject contradictory parent coordinates in other records.

### 3.9.1 The seed generation

The seed generation, `v0`, begins the epoch's lineage. A seed copied from another
epoch retains the complete source epoch and generation coordinates. It is not
a new measurement of that source's performance.

### 3.9.2 Per-generation records

`epoch.journal` owns accepted experiment bodies and their declared patches.
An experiment carries a hypothesis before execution and an outcome after
settlement. Mutation validation and generation derivation must complete before
a partially written child can be accepted. Receipt replay resumes ordered
settlement effects; reading a receipt alone performs no replay.

### 3.9.3 Cross-epoch parent identity

A generation id is local to its epoch. Preserve complete coordinates when a
parent comes from another epoch. Do not reduce an external parent such as
`source:v8` to `v8`, or infer a parent from directory order or the active marker.

## 3.10 Record acceptance and the derived index

Use each canonical record's reader and writer. Required fields and supported
formats belong to those owners. `check_record_format` requires an explicit
integer stamp equal to the owner's supported version. Missing, Boolean,
floating-point, and incompatible stamps are refused. A present malformed
record is distinct from absence; callers may expose that refusal but cannot
substitute defaults that authorize execution. Configuration records use the
shared strict decoder.

The analytical index is derived. Its supported schema is pinned, and a mismatch
requires rebuilding from canonical files through the index owner. Index repair
does not migrate or repair an invalid canonical contract.

## 3.11 Recipe: add a contract field

1. Declare the field's type, default, description, persisted name if needed,
   and constraints on its owning dataclass.
2. Connect its actual consumer. Preserve the accepted value across invocation
   binding and worker reconstruction where the consumer runs out of process.
3. Update related validation and cost estimates when the setting affects them.
4. Verify complete serialization, strict rejection of invalid input, and the
   effect of a non-default value at the consuming boundary.
5. Check full and component hashes. Equivalent authored spellings must agree;
   distinct effective values must differ when they change the contract.
6. Review reference-output changes field by field. Explain changed identity
   rather than omitting a value to retain a previous hash.
7. Update the relevant configuration documentation and require complete
   verification on the source proposed for merge.

## 3.12 Recipe: add an operational setting

Classify the value by what it controls. A setting that changes evaluation,
grading, or captured execution belongs in contract identity. A measured result
belongs in the record produced by the measurement. A service address,
concurrency limit, or scheduling control belongs in operational configuration
unless its actual consumption makes it part of evaluation.

Declare an operational setting in its existing configuration owner, bind it
once per invocation, and transport that accepted value to its consumer. Test
that changes reach the consumer without rereading live configuration. Its
presence on `RuntimeConfig` alone does not decide whether it is causal:
that carrier also holds captured execution settings.

## 3.13 From preparation to a contract change

Preparation accepts a board, brief, scoring document, and execution declaration.
Publication records their full configuration and required hash. Tournament
execution checks the selected epoch's captured bindings before evaluating a
candidate. Measured losses and later noise-floor results leave that identity
unchanged.

If the operator changes a scoring value or grading implementation, resolving
the live inputs produces a different hash. Automatic rolling prepares and
publishes a separate epoch. With automatic rolling disabled, execution reports
the drift. Neither path silently applies changed rules to existing measurements.

## 3.14 Related guides

- [Evaluation statistics](04-evaluation-statistics.md): measurement and decision semantics.
- [Tournament and selection](06-tournament-and-selection.md): worker transport and cache admission.
- [Runtime and durability](07-runtime-and-durability.md): publication, storage, and replay.
- [CLI and configuration](10-cli-and-configuration.md): authored inputs and invocation binding.
- [Contract field registry](../design/CONTRACT-FIELD-REGISTRY.md): declaration ownership.

## 3.15 Verification map

Use focused checks while implementing the relevant boundary. Complete CI must
pass on the source proposed for merge; see [testing](11-testing.md).

| Boundary | Existing verification |
|---|---|
| Canonical forms and source sensitivity | `tests/test_epoch_contract.py`, `tests/test_proposer_contract_identity.py` |
| Complete scoring serialization | `tests/test_contract_serializer_completeness.py` |
| Drift and epoch selection | `tests/test_auto_epoch.py`, `tests/test_evolve_auto_epoch.py` |
| Preparation and recoverable publication | `tests/test_epoch_publication.py`, `tests/test_cli_epoch_publication.py` |
| Typed lineage and score records | `tests/test_lineage_score_records.py` |
| Independent contract identity reference | `tools/parity.sh --only CONTRACT-HASH` |
