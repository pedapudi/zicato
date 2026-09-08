# Settlement receipt ownership and replay validation

Status: implemented receipt and field-tournament ownership slice under
[issue #411](https://github.com/pedapudi/zicato/issues/411), pending integration review.
The source inventory was checked against revision `3bddf6424e5b13287fa9adaee34e73a97cb62457`.
The [full record audit](persisted-record-ownership-audit.md) retains all 55 approved entries.
The intended reader maintains canonical workspace records and recovery behavior.

A settlement receipt records a resolved tournament decision before its outcomes,
lineage, champion marker, journal entries, and settled tournament record are
published. Restart recovery completes that decision. The receipt remains after
completion to record index repair and promotion-hook delivery status.

The receipt's shape belongs in the epoch domain because it identifies one round
within an epoch and binds that round's candidate decisions to durable records.
Its replay belongs in the round orchestration package because replay performs
ordered writes across those record owners.

## Existing approval permits the ownership extraction

The [recorded design approval](https://github.com/pedapudi/zicato/issues/411#issuecomment-5520548953)
states: “The ownership map and the nine authority rules are approved.” It also
approves strict canonical decoding, typed readers, atomic writes, domain-owned
field-tournament records, and removal of legacy workspace fallbacks.

The [receipt-specific refinement](https://github.com/pedapudi/zicato/issues/411#issuecomment-5554870505)
requires immutable receipt facts and pure decoding below replay orchestration.
It preserves the replay protocol, receipt authority, hook-delivery handling,
repair states, and persisted bytes. No repeated approval is needed for the
already approved ownership rules. A proposed format change or a change to replay
authority would require a separate concrete decision.

Receipt extraction is one part of issue #411. It does not complete ownership of
every record in the approved inventory. The experiment owner already has shared
acceptance and encoding operations. Lineage and generation scores now have typed
owners with their writers and readers migrated; the other inventory gaps remain.

## Use existing domain owners and one receipt module

| Responsibility | Owner | Integration |
| --- | --- | --- |
| Receipt and candidate types, format guard, encoder, decoder, typed reads, ordered receipt enumeration, and pure comparisons against related records. | `epoch/settlement_receipt.py` | Pure decode, construction, typed reading, and consistency inspection use one owner. |
| Receipt filename and path grammar. | `workspace/layout.py` | `field_settlement` declares the path; receipt key helpers derive it. |
| Outcome shape and codec. | Existing `core/experiment.py` and `epoch/journal.py`. | Reuse `OutcomeRecord` and the owner's codec. |
| Field-tournament snapshot shape and codec. | `tournament/records.py` | Builder, decoder, reader, and atomic writer share one owner; the caller supplies the timestamp. |
| Gather and validate canonical inputs before performing ordered settlement writes. | `epoch/settlement_receipt.py` | Read through record owners, then compare independent inputs without I/O. |
| Index repair inspection. | `index/ingest.py` using the receipt owner. | Read accepted receipt progress without importing settlement replay. |
| Receipt-presence check during interrupted-field cleanup. | `runtime/resume.py` using the receipt owner. | Use the receipt owner for the presence-check key. |
| Operator-visible receipt diagnostics. | `health/inputs.py` using the receipt owner. | Catch record errors once and retain a named diagnostic reason. |

Receipt, candidate, progress, and field-tournament values use frozen dataclasses.
The accepted JSON representation is retained as private immutable text. Decoded
outcomes and JSON projections are fresh values, so nested evidence cannot mutate
accepted facts. Retaining the representation also preserves optional-key omission
and integer values that the outcome codec normalizes during interpretation.
There is no shared record superclass or extensible registry.

One encoder and one decoder own each format. Writers construct typed values;
inspection consumes typed values. Views obtain recorded JSON through the owner's
validated projection, retaining its acceptance rules and recorded omissions.

## Receipt fields retain their present meanings

The on-disk receipt is format 3. Its top-level object has these fields:

| Field | Canonical meaning | Consumer or projection |
| --- | --- | --- |
| `format_version` | Required format discriminator, equal to integer 3. | Decoder refusal for absent or incompatible versions. |
| `settlement_id` | Lowercase hexadecimal nonce of length 32 identifying this decision. | Stable per-candidate journal identities and conflict checks. |
| `epoch_id` | Epoch containing the decision. | Must equal the storage namespace and candidate experiments. |
| `round_index` | Nonnegative integer identifying the proposing round. | Must equal the containing round directory and candidate experiments. |
| `primary_promoted_generation_id` | Generation that replaces the champion, or null. | Champion marker and promotion hook; agrees with candidate outcomes and tournament record. |
| `candidates` | Nonempty ordered set of candidate experiment identities and resolved outcomes. | Experiment outcomes, lineage resolution, journal entries, and index rows. |
| `field_tournament_record` | Settled candidate-field record, or null for a two-competitor tournament. | Durable tournament snapshot and its index projection. |
| `state` | Canonical settlement publication is pending or committed. | Recovery selection; mutable replay progress. |
| `index_projection` | Whether the derived index was refreshed or requires repair. | Index startup and health diagnostics; mutable replay progress. |
| `promotion_hook` | Whether an external promotion hook was applicable and what delivery is known. | Hook invocation and health diagnostics; mutable delivery progress. |

Each candidate carries `experiment_id`, `generation_id`, `created_at`,
`parent_scalar`, `child_scalar`, and `outcome`. The identifiers bind the receipt
to the independently stored experiment. The timestamp is generation birth time.
The nullable scalars populate lineage; zero remains a valid measurement.
The ordered candidate list determines the first challenger used in the durable
tournament identifier. Reordering candidates changes immutable receipt facts.

The nested outcome uses the existing outcome record's fields without a second
declaration:

- `ran_at`, `drift_movements`, and `metric_movements` describe observed results.
- `pass_rate_delta`, `drift_loss_delta`, and `scalar_score_delta` describe the
  measured change from the parent.
- `tournament_decision` and `rejection_reason` state the candidate's disposition.
- `structure`, `final_rank`, `eliminated_in_round`, and `match_record` record its
  tournament path.
- `champion_eval_mode` records execution provenance.
- `holdout`, `train_loss`, `holdout_loss`, `generalization_gap`, and `evidence`
  retain the decision's evaluation evidence.
- `operator_override` and `operator_override_reason` retain an explicit override.

The nested field-tournament record has identity fields `tournament_id` and
`epoch_id`; configuration fields `structure` and `structure_params`; participant
and execution fields `competitors`, `rounds`, `standings`, and `field_status`;
and result fields `promoted_generation_id`, `champion_generation_id`, `decision`,
`reason`, `delta_scalar`, `state`, and `ran_at`. The optional
`promoted_generation_ids` and `override_status` are omitted when absent.
The tournament owner preserves those omission rules. Historical field snapshots
without `state` retain their settled meaning and keep that key omitted. Nested
format-3 receipt snapshots still require explicit `state: settled`.

The settled tournament object is the receipt's publication payload. Its copied
decision fields must agree with the candidate outcomes. The primary champion
belongs to the full promoted set; competitor identities and roles match one
incumbent and all receipt candidates. A single-candidate receipt may omit the
separate field record, while a multi-candidate receipt requires it.

## Separate decoding from validation against canonical records

Receipt decoding is pure. `validate_workspace_settlement` reads experiments,
lineage, champion state, and an existing tournament snapshot, then passes those
independent records to `validate_settlement_records` for pure comparisons.

The receipt decoder receives a JSON value and the expected epoch and round from
its storage location. It validates types, finite numeric values, required
identities, candidate uniqueness, progress combinations, and redundant receipt
representations. It returns typed receipt facts without opening files or
importing orchestration. Malformed or contradictory fields raise a record error
naming the receipt and storage key.

Some validation necessarily uses other canonical records. Format 3 candidate
entries do not store their parent identity; recovery reads each candidate's
experiment to obtain it. The record keeps that format. Recovery
therefore loads the independent experiment records before its first write and
checks the following:

- Every experiment matches its candidate, epoch, round, and experiment identity.
- Every candidate names the same nonempty parent generation.
- Existing outcomes are absent or equal to the receipt during pending replay;
  committed receipts require their outcomes to exist and agree.
- Lineage resolutions agree with stored parent, creation, round, disposition,
  reason, and scalar facts; a committed receipt requires resolved entries.
- An existing settled tournament record equals the receipt payload. An
  in-progress record agrees on identity, structure, competitors, and incumbent.
- A pending promotion can replace only its parent marker or the already-written
  primary champion marker. Later unrelated champion progress is not overwritten.

Recovery gathers these canonical inputs and requires successful validation before
any mutation. The pure comparison beside the decoder accepts typed experiments
and a typed field snapshot. Lineage row-consistency rules remain in their existing owner
through `validate_generation_resolution_rows`. Lineage still exposes a raw graph;
its full typed reader-and-writer migration remains open. Neither comparison
reopens a file or reinterprets a lineage row outside its owner.

Pure receipt inspection reports malformed receipt facts. The diagnostic reader
loads related records through the same owners and calls the same comparisons to
retain cross-file corruption coverage. It reads the champion marker through the
workspace layout rather than importing `evolve.generation_phase`. Interrupted
publication prefixes remain legal where pending recovery accepts them.

## Replay progress and authority remain explicit

Only `state`, `index_projection`, and `promotion_hook` may change after receipt
publication. All other encoded facts participate in the same-decision conflict
check. Repeating commit with identical facts resumes pending work or returns
without reversing committed progress. A different decision for the same epoch
and round is refused.

Replay preserves this write order:

1. Publish the pending receipt atomically.
2. Write each candidate outcome through the experiment owner.
3. Resolve candidate lineage entries through the lineage owner.
4. Publish the primary champion marker if promotion occurred.
5. Append each journal entry using its stable settlement-and-candidate identity.
6. Publish the settled field-tournament record when applicable.
7. Refresh the derived index and persist its result in the receipt.
8. Mark the receipt committed.

Index projection moves from `pending` to `succeeded` or `repair_required`.
A failed projection records the exception type. Successful full reconstruction
changes `repair_required` to `repaired` and retains that exception type.
Validate all receipts to acknowledge before writing any acknowledgements, as
the existing recovery code does. Coordinate the acknowledgement with the index
revision mechanism from [issue #481](https://github.com/pedapudi/zicato/issues/481)
so it cannot acknowledge unprojected canonical changes.

A promotion hook starts as `not_applicable` or `pending`. The live caller writes
`delivery_unknown` before invoking an external hook, then records `succeeded`
or `failed` only after that call returns. Recovery converts retained `pending`
delivery to `delivery_unknown` and never retries the external side effect.
The adapter name and failure type retain their existing state-dependent rules.

Single-file receipts use strict parsing because their writer is atomic.
Absence is typed and legal before publication. A malformed present receipt
raises; diagnostics may catch the error and carry its reason. Receipt decoding
does not accept a truncated JSON body. Append-only logs retain their separate
rule allowing an incomplete final JSONL line.

## Compatibility and verification

Generation scores are not an input to receipt decoding or replay validation.
Their writer-and-reader migration is an independent issue #411 slice. The
experiment owner supplies shared acceptance and decoding; lineage supplies
immutable typed rows and a pure settlement comparison in its existing graph format.

`check_record_format` requires the owner's supported integer stamp on every
present record. Receipts require integer format 3. Missing, Boolean,
floating-point, and incompatible stamps are refused before progress can
influence replay.

Canonical writers retain the storage backend's sorted, indented JSON and newline
behavior. Receipt and field snapshot codecs preserve optional-key omission,
array order, and numeric representation. No workspace migration or format bump
is required. Nested confirmation and measurement facts remain with their domain
owners; receipt progress rewrites retain their encoded values without interpreting
or normalizing them.

Direct tests in `tests/test_settlement_receipt_records.py` cover immutable nested
facts, exact output bytes for rejection, promotion, multiple promotions, override
provenance and index failure, required format stamps, finite scalar acceptance,
namespace identity, duplicate candidates, strict absence/corruption handling, and
pure consistency checks. Field snapshot fixtures cover historical omission of
`state` as well as explicitly settled records. Comparisons against the original
snapshot builder preserve exact promotion and override bytes.

`tests/test_field_settlement_recovery.py` retains real-storage interruption checks
after receipt publication, each outcome, lineage, champion marker, each journal
append, settled tournament publication, index projection, and receipt completion.
It also retains index failure and repair acknowledgement, hook-crash handling,
receipt conflicts, sibling cleanup before receipt publication, and diagnostics
that prevent partial repair acknowledgement. The existing field-persistence tests
exercise real strategy output, index reconstruction, and query readback.

Index readiness now refuses an incomplete receipt that claims to be committed.
Index reconstruction refuses malformed field snapshots instead of silently
omitting their rows. Query projections catch shared record errors at their view
boundary and retain an `unreadable` reason. Receipt health inspection retains its
cross-file corruption checks and reports the shared record error type.

The receipt owner adds no lock or transaction layer. Integration with publication
recovery and canonical index revisions belongs to issues
[#480](https://github.com/pedapudi/zicato/issues/480) and
[#481](https://github.com/pedapudi/zicato/issues/481). Those integrations must retain
revision marking and acknowledge repairs only after the corresponding canonical
revision has been projected. Publication intent types remain with their own epoch
owner; receipt authority remains the settled round decision.
