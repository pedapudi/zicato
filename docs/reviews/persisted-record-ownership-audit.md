# Historical audit of persisted record ownership

This audit records the inspection of 55 stored-file categories for
[issue #411](https://github.com/pedapudi/zicato/issues/411#issuecomment-5520548953).
Its statuses describe the inspected source trees, before the shared readers
and supported representations were integrated. They are not an outstanding
work list. The development guide describes the implemented
[configuration and epoch records](../dev-guide/03-contract-and-epochs.md).

The inspected source trees were:

- Record readers: `6118a0e4f05012841f32f41da34d0b1121c69083`.
- Process ownership: `f7ce4107ce144208068983dd6ecf6f1b0da25d70`.
- Configuration and publication: `8256140d65d2f38d794c99fb0e3504cb72fae33d`.
- Measurement storage: `a61a08f83632a2afd8c78a9b3bdca9dc94467840`.
- Captured results: `c99cd03df0bdc8c975299f9fe63ba36da1023e41`.
- Record, query, and browser integration: `c84765e2f75749e257babec93b8c7375393dc58d`.

Paths are relative to the workspace root. Generation files live under
`epochs/<epoch>/generations/<generation>/`; run files add `runs/<entry>/`
and the directories declared by the measurement layout. Reflection files
live under `epochs/<epoch>/reflections/<reflection>/`.

| # | Record, relative to workspace | Owner and evidence | Status | Remaining ownership work |
| --- | --- | --- | --- | --- |
| 1 | `config.json` | `workspace/config_io.py` reads and writes `workspace/config_schema.py::WorkspaceDeclaration` through the strict `core/configuration.py` decoder in the configuration composition. | Integration pending | Integration pending: preserve composed domain declarations and adapter identity; frozen epoch records remain a separate acceptance boundary. |
| 2 | `lineage.json` | `epoch/lineage.py` owns immutable graph/epoch/generation facts, strict decoding, reading, initialization, mutation, and encoding. | Implemented extraction | Complete for this slice: receipt, resume, GC, analysis, CLI, query, and index use the owner. Preserve unresolved `promoted: null`, scalar zero, omitted fields, and cross-epoch coordinates. Mark changed epoch projections before replacement. |
| 3 | `current_epoch` | `epoch/lifecycle.py` owns the scalar marker. | Integration pending | Keep its publication and read authority explicit; coordinate epoch publication recovery. No JSON record type is needed. |
| 4 | `proposer_staged.json` | `proposer/staging.py` owns recommendation ID reading and mutation; `epoch/publication.py` recovery consumes the captured publication intent. | Integration pending | Integration pending: preserve exact-ID acknowledgement after publication. Reflection delegates queue decoding to that owner. Strict staging-record acceptance still needs an explicit audit. |
| 5 | `index.db` | `index/schema.py` owns the derived relational schema and migration. | Integration pending | Keep rebuild authority in canonical files; coordinate revision and repair acknowledgement. It is not a canonical JSON record. |
| 6 | `logs/<stamp>-<pid>.jsonl` | `logging_stream.py::record_to_dict` writes operational rows; `query/log_stream.py::_parse_record_line` independently accepts any JSON object. | Open | Open: share the declared row shape while retaining diagnostic tolerance. The query cursor consumes only newline-terminated rows; malformed complete rows are skipped for diagnostic display. |
| 7 | `runtime/lock.json` | `runtime/lock.py::WorkspaceLock` owns inspection-record encoding and decoding; the process guard owns writer exclusion. | Existing protocol; audit open | Existing protocol: preserve PID/start-token identity and kernel guard authority. Inspection JSON must never substitute for holding the guard; numeric/string coercion in its decoder still needs an explicit compatibility policy. |
| 8 | `runtime/heartbeat.json` | `runtime/state.py::Heartbeat` owns snapshot encoding and decoding; query runtime capture decorates accepted snapshot values. | Existing protocol; audit open | Existing protocol: heartbeat describes observed activity. Orphan cleanup requires process ownership evidence. Its coercion and malformed-snapshot fallback policy remain to be stated consistently across consumers. |
| 9 | `runtime/dashboard.json` | `dashboard/server.py::_publish_endpoint` writes host/port directly; `cli/commands/evolve.py::_read_dashboard_endpoint` parses them independently. | Open | Open: one runtime endpoint codec and atomic writer, preserving absent/not-ready behavior. Record producer identity if the endpoint is to establish readiness after a process restart; do not infer it from host/port alone. |
| 10 | `runtime/active_runs/*.json` | `runtime/state.py::ActiveRun` owns worker and optional producer identities; runner and proposer publication paths populate them before worker execution. | Existing protocol; audit open | Process integration owns the shared guard across advanced commands and supervisor cleanup. Historical missing producer identity remains unproven. Retain strict producer PID/start-token validation and exact snapshot comparisons; audit the remaining snapshot-field coercions separately. |
| 11 | `runtime/active_tournament.json` | Unsupported saved file; readers use the event log in row 12. | Code support removed (#385) | Readers ignore saved snapshots, and runtime cleanup leaves them untouched. |
| 12 | `runtime/active_tournament.events.jsonl` | `runtime/tournament_log.py` owns event variants and folding; `runtime/channel.py::EventLog` owns append and cursor mechanics. | Existing protocol; audit open | Existing protocol: confirm the event payload acceptance and single-writer precondition under composed process ownership. Preserve ordered fold and declared incomplete-tail handling. |
| 13 | `runtime/progress.events.jsonl` | `runtime/progress_log.py` publishes and reads transitions through `runtime/channel.py::EventLog`. | Existing protocol; audit open | Existing protocol: retain event sequence and heartbeat cursor agreement. Event.from_record still copies unvalidated sequence/type fields; define its refusal policy once for all consumers. |
| 14 | `runtime/control/*` and `runtime/control_log/*` | `runtime/channel.py` owns Event/Command records and claim/archive operations; `runtime/control.py` owns command meaning. | Existing protocol; audit open | Existing protocol: keep operator acknowledgement and claim authority. Validate record envelopes in the owner rather than duplicating command parsing at each consumer. |
| 15 | `runtime/control/kill_requests/<run>` | `runtime/control.py` owns a cross-process scalar signal. | Existing scalar/report owner | Keep the simple marker contract; no JSON hierarchy is needed. |
| 16 | `runtime/inconclusive/<generation>.json` | `selection/dead_letter.py` owns typed reading, list reading, inverse decoding, and validated publication. | Implemented extraction | Complete for this slice: the rating view uses accepted fields and checks epoch/champion before using generation-keyed records. Present corruption is explicit; nested evidence remains owned by selection. Historical field bytes and numeric types are preserved. |
| 17 | `epochs/<epoch>/config.json` | `epoch/lifecycle.py` writes the full record with `core.configuration.dataclass_to_jsonable` and reads it with the shared strict decoder. | Shared configuration owner | Required format stamp and 64-character lowercase contract hash; missing required fields, invalid values, and unknown fields are refused. Captured execution also requires `execution.json`. |
| 18 | `epochs/<epoch>/board.jsonl` and header | `board/jsonl.py` owns the board loader; `epoch/execution.py::EpochExecutionContract` captures frozen bytes in the invocation composition. | Integration pending | Integration pending plus open consumers: remove tolerant `workspace/reads.py::read_board` and `query/epoch_view.py::_parse_board` acceptance wherever still present. All composition readers must use one declared board acceptance rule. |
| 19 | `epochs/<epoch>/scoring.json` | `core/scoring_config.py` uses `core.configuration` for complete serialization and strict decoding; epoch loading and hashing share that owner. | Shared configuration owner | All effective values, including nested defaults, are saved and hashed. There is no historical-default decoder or omission registry. Grading plugin source identities remain part of canonicalization. |
| 20 | `epochs/<epoch>/brief.md` | `proposer/brief.py` owns brief interpretation; `epoch/execution.py::EpochExecutionContract` captures its frozen bytes. | Integration pending | Integration pending: remove `query/epoch_view.py` fallback to `rubric.md` from the composed reader. Preview truncation remains a view concern; it must not choose an alternate contract. |
| 21 | `epochs/<epoch>/journal.md` | `epoch/journal.py` renders typed experiments and atomically appends stable settlement identities. | Existing scalar/report owner | Existing rendering owner; preserve idempotent append behavior. |
| 22 | `epochs/<epoch>/analysis.md` and `.html` | `epoch/analysis.py` and `analyzer/report.py` render Markdown and HTML reports. | Open | Open publication work: both still contain direct write_text calls. Use atomic output replacement; no inverse report codec or canonical decision record is needed. |
| 23 | `epochs/<epoch>/mutations.json` | `mutation/inventory.py` owns complete seven-field acceptance and atomic publication from existing `MutationPoint` values. | Implemented extraction | Query mutation/epoch views and analyzer reports use the shared reader. Absence, empty enumeration and present corruption remain distinct; valid producer bytes and extension fields are preserved. The snapshot supplies display facts, not execution containment authority. |
| 24 | `epochs/<epoch>/proposer_inputs.jsonl` | `proposer/input_capture.py` writes and reads dictionaries under its append lock. | Open | Open: introduce one captured-input row codec. The reader skips a malformed final line even when newline-terminated and silently drops non-object rows. Only an unfinished final append may be recoverable; complete malformed rows need an explicit refusal. |
| 25 | `epochs/<epoch>/contract_components.json` | `epoch/contract.py` owns the stored string-to-string map, its reader and atomic writer. | Implemented extraction | Epoch preparation, rollover, frozen checks and query projections share acceptance. Malformed present maps are refused before rollover publication. Future component names and valid stored bytes are preserved; view subsets do not redefine the record. |
| 26 | `epochs/<epoch>/ladder_state.json` and initialization marker | `tournament/governance.py` owns ladder and pending-reservation types, strict state decoding, initialization marker, locking, and writes. | Existing protocol; audit open | Existing protocol: retain reservation recovery and initialization identity. Preserve state/marker agreement; no second ladder-state decoder is needed. |
| 27 | `epochs/<epoch>/pareto_frontier.json` | `epoch/pareto.py` owns frontier/member types and both codec directions. | Open | Open strictness: load_frontier accepts a present non-object as an empty frontier; member decoding skips malformed axes/members and coerces invalid margin to zero. Refuse malformed canonical state while preserving valid omitted historical fields and retired members. |
| 28 | `epochs/<epoch>/current_generation` | `evolve/generation_phase.py` owns marker mutation; layout declares its path. | Integration pending | Receipt inspection must read through layout without importing replay. Coordinate generation publication authority; no compound record is needed. |
| 29 | `epochs/<epoch>/v0_seed_from` | `evolve/epoching.py` constructs the scalar seed marker. | Integration pending | Baseline publication recovery owns the durable write and its ordering. |
| 30 | `epochs/<epoch>/tournaments/field-<id>.json` | `tournament/records.py` owns accepted snapshot facts, builder, decoding, reading, and atomic writes. | Implemented extraction | Index and both query readers use the shared owner; receipt payloads use the same codec. Preserve historical omitted `state`, optional override keys, and numeric bytes. |
| 31 | `epochs/<epoch>/health/round_<n>.json` | `health/diagnostics.py::LoopHealth` declares findings; `evolve/round_prepare.py::_loop_health_to_json` adds persisted epoch/round metadata. | Open | Open: complete persisted health-report codec and reader, including metadata and healthy/findings consistency. Preserve derived diagnostic status without giving the report settlement authority. |
| 32 | `epochs/<epoch>/insights/round_<n>.md` and `latest.md` | `analyzer/report.py` renders round insights and replaces the latest report. | Open | Open publication work: route direct text/HTML replacements through atomic publication. These are derived reports; no canonical decision type is required. |
| 33 | `epochs/<epoch>/episodes/<generation>[-slot]/episode.jsonl` | `query/foe_episode.py` interprets foreign-produced episodes. | Foreign adapter | Keep an explicit foreign format adapter and its compatibility policy; do not reschema the producer's output. |
| 34 | Episode `export.html` | `proposer/episode_export.py` renders the foreign episode and calls storage.atomic_write_text for its HTML output. | Existing scalar/report owner | Existing derived publication: retain the foreign episode adapter and atomic replacement. No inverse HTML codec is required. |
| 35 | `rounds/<n>/round_log.jsonl` | `epoch/round_log.py` owns typed rows, schema, and fold. | Existing protocol; audit open | Existing shared protocol; preserve ordered append and torn-tail semantics. |
| 36 | `rounds/<n>/field_settlement.json` | `epoch/settlement_receipt.py` owns immutable facts, pure decoding/comparison, strict typed reading, scanning, and encoding. | Implemented extraction | `evolve/settlement_recovery.py` retains ordered replay and side effects. Integration must preserve publication revision marks and repair acknowledgement; the format remains 3. See the receipt design. |
| 37 | Reflection `plan.json` | `reflection/plan.py` owns strict typed decoding, location validation, historical omission-preserving encoding, and durable publication. | Implemented extraction | Complete for this slice: query and index use `read_plan`; malformed plans cannot reuse indexed identity. Index revision marking precedes publication, including the completion flag. |
| 38 | Reflection `corpus.jsonl` | `reflection/corpus.py::ObservationRun` owns row codecs and atomic collection publication; measurement traversal adds optional MeasurementDraw. | Integration pending | Integration pending: preserve full seed-qualified run references through corpus and adjudication lookup. The derived corpus reader intentionally skips malformed lines; retain that declared tolerance while validating measurement eligibility through its owner. |
| 39 | Reflection `adjudication/<judge>/<ref>.json` | `reflection/adjudication.py` owns accepted `JudgeAdjudication` facts, verdict classification, strict inverse decoding, reading, and publication. | Implemented extraction | Complete for this slice: engine, query, mining, scorecards, and findings use the owner. Historical absent protocol fields remain omitted and stale. Malformed records cannot trigger automatic adjudication or contribute partial mined evidence. |
| 40 | Reflection `scorecards.json` | `reflection/scorecards.py` owns strict judge-card decoding and the complete reflection collection, reading, and durable publication. | Implemented extraction | Complete for this slice: CLI, query, and index use the owner. Empty and absent collections cannot reuse indexed cards; malformed rows refuse the collection. Typed edits retain recorded number types and extension fields. Projection marking precedes publication. |
| 41 | Reflection `findings.json` | `reflection/findings.py` owns finding facts and the ranked collection, strict inverse decoding, reading, and durable publication. | Implemented extraction | Query, index and CLI reports share acceptance. A malformed member refuses the collection before index changes. Reading checks operation structure without invoking operations; semantic validation belongs to finding emission. |
| 42 | Reflection `practices.json` | `reflection/practices.py` owns strict inverse codecs, reading, and durable publication on the existing check/review types. | Implemented extraction | Complete for this slice: query and CLI share acceptance, including verdict-count consistency and missing-evidence reasons. Typed edits recompute verdict counts and retain extension fields. The index does not consume this record. |
| 43 | Reflection `suggestions.json` | `reflection/suggestions.py` owns strict typed decoding, collection identity checks, ranked atomic publication, and the sole reader. | Implemented extraction | Reports and trace views share acceptance. Malformed members refuse the collection. Existing list APIs retain empty results for absence and valid empty collections. Trace views display server-owned unreadable reasons, including reason changes between refreshes. |
| 44 | `epochs/<epoch>/proposer_reflections/<id>/findings.json` | No active producer or consumer. | Outside supported workflows | Retained files are historical artifacts. Proposer scorecards read round logs and experiment records; they do not consume proposer-edit recommendations. |
| 45 | Generation `experiment.json` | `epoch/journal.py` owns experiment acceptance and patch resolution. Request capture adds read_experiment_contents with accepted body plus resolved patches from one read. | Existing protocol; audit open | Open strictness audit: inline patch fallback is removed, but hypothesis/outcome/coordinate coercions remain in the owner. Preserve historical accepted bodies while deciding which malformed present fields must be refused. Readers must not add independent acceptance. |
| 46 | Generation `patches/<id>.json` | `epoch/journal.py::patch_body` encodes patches; its shared patch decoder resolves every declared sibling file. | Existing protocol; audit open | Existing shared owner; strictness remains open for coerced IDs/numeric fields and operation value validation. Keep missing declared patches distinct from an absent experiment and do not restore inline fallback. |
| 47 | Generation `gen_score.json` | `tournament/scoring.py` owns `GenerationScore`, strict decoding/reading, and atomic publication after history retention. | Implemented extraction | Complete for this slice: workspace raw reads and evolve direct writes are removed; CLI, baseline, report, and query consumers use the owner. Valid historical number types and omitted fields remain unchanged. Seed-qualified index selection must add a projection mark before changing the selected score identity. |
| 48 | Generation `gen_score.history.jsonl` | `tournament/scoring.py` owns `ScoreMeasurement`, strict ordered reads, and durable append. | Implemented extraction | Complete for this slice: sequence begins at zero; absent history is empty; malformed complete lines are refused. Only an unterminated invalid final JSON line is recoverable. Preserve complete prefix bytes and publish history before the flat score. |
| 49 | Generation `harness_load.json` | `_tournament_worker.py::_record_harness_load` writes the snapshot-origin record; `health/inputs.py` and evolve reporting read dictionaries. | Open | Open: an epoch-owned codec for schema, source paths, generation identity, and origin facts. Both health and round-log reporting must consume it; coordinate worker input changes with execution identity. |
| 50 | Run `loss.json` and repeated measurement slots | `telemetry/reducer.py` owns LossProfile read/write and inverse codecs; `core/measurement.py` owns purpose/draw/seed identity. | Integration pending | Measurement composition validates requested epoch/generation/entry and complete observation identity before cache admission. Audit remaining bypass readers and extension preservation independently of that identity check. |
| 51 | Run `loss.archive.jsonl` | `tournament/unit_cache.py` retains best-effort historical LossProfile rows; `tournament/artifacts.py::archive_unit_artifacts` owns complete artifact retention. | Open | The legacy profile-only history skips malformed rows. State that tolerance explicitly and keep it subordinate to complete attempt archives; do not infer that a retained loss proves capture provenance. |
| 52 | Run `result.json` and repeated measurement slots | `tournament/unit_cache.py` encodes RunResult; read_run_result accepts an optional expected LossProfile in the capture composition. | Integration pending; codec open | Known-seed fidelity requires full measurement and run-ID agreement with the paired loss, including archived captures. The inverse API still returns dictionaries; complete typed structural acceptance remains separate work. |
| 53 | Run `events.jsonl`, repeated slots, and retained previous stream | `telemetry/event_log.py` owns the foreign event adapter and explicit per-line tolerance. | Foreign adapter; integration pending | Preserve envelope interpretation and incomplete-line behavior. Measurement identity owns committed attempt enumeration and physical stream provenance; the adapter must not reinterpret slot identities. |
| 54 | Run `judge_io.jsonl` and repeated measurement slots | `judge_runtime/io_capture.py::build_judge_io_record` and read_judge_io share a module but return dictionaries. Capture composition adds expected paired-loss identity checks. | Open | Open: typed capture acceptance, exact version policy, and incomplete-tail handling. The reader skips malformed complete rows and incompatible versions; preserve raw capture visibility while distinguishing unreadable evidence. |
| 55 | Run artifact manifest and copied files | `tournament/artifacts.py` writes the manifest; `query/transcript_view.py` independently parses artifacts.json. | Open | Open: one manifest codec and reader, including recorded provenance and confined copied-file paths. Measurement identity owns physical slot selection; copied bytes remain outside the JSON decoder. |

The reflection plan owner includes its query and index consumers. Adjudication
facts and codecs are separate from execution; query and mining use the same
strict reader. Scorecards include their canonical writer, CLI report, query,
and index consumers. Findings include draft application and refuse malformed
collections before creating a draft. Practice reviews include CLI and query
readers, with recorded verdict counts checked against individual checks.
Suggestions include their inbox, apply, report, and trace consumers. Proposer
reflection records include pending-queue selection, CLI preview/application,
and query refusal reporting. Their decoder does not import reflection
execution or recommendation application. Lineage and generation-score ownership
include their production readers and writers; the open cells retain their scope.

Additional records observed outside the original numbered inventory include
reflection `summary.json`, a derived summary read directly by the query layer.
The epoch and baseline publication work owns `EpochPublication` and `BaselineSeed`
in `epoch/publication.py`, with `epoch_publication.json` at workspace scope and
`baseline_seed.json` inside an epoch. Prepared source trees remain outside completed
epoch/generation enumeration; those owners declare intent codecs and recovery rules. Measurement identity and
confirmation work add nested facts to existing loss/result/outcome records;
their domain codecs own those facts. The mutation containment work adds
`epoch/containment.py`, which owns generation `containment.json` format 1; its
attestation uses the journal owner for experiment acceptance and binds exact
patch bytes, source hashes, and edit intervals. The independent runtime verifier
publishes epoch `health/mutation_containment_<generation>.json` findings.
Measurement work owns seed-qualified observation paths and run references;
reflection corpus and adjudication readers consume that identity without
creating a separate encoding. These additions do not replace any of the
original 55 entries.

Receipt decoding does not read generation scores. The actual prerequisite is
the nested durable tournament snapshot, followed by shared experiment and
lineage comparisons. Implement that dependency sequence without making an
unrelated score migration a prerequisite. Score ownership is implemented as its
own writer-and-reader slice. Neither slice closes the remaining issue #411 inventory.


The lineage and score verification checks canonical corruption refusal, exact
bytes from the prior writers, pending-generation recovery, and changed-epoch
revision failure before canonical replacement. The audited index reads experiment
outcomes and per-run loss records without consulting generation scores. Seed-qualified
index projection changes that dependency: selection by the score's base seed requires
a projection mark before canonical score replacement. The measurement traversal
work owns that integration and its recovery checks.

The workspace parity fixture uses the record owners to write field tournaments
and generation scores. Its corrected per-entry mappings, sequence numbers, and
bracket matches are documented in the validation evidence. All 107 captured
reader outputs match the prior implementation when both use that valid fixture.

The remaining canonical migrations are bounded by domain: frontier state,
mutation inventory, harness-load provenance, complete loop-health metadata,
and contract-component hashes each need their existing producer and all
consumers moved together. Frozen epoch configuration and journal coercion
policies remain open even where a shared decoder already exists. Authored
configuration validation does not settle those historical record policies.

Capture migrations must compose with measurement identity before selecting
result, judge-I/O, or artifact evidence. A missing capture, an incomplete
append, and a malformed complete record need distinct outcomes. Operational
logs and foreign traces may retain their declared tolerant adapters; that
exception does not authorize recovery or cache admission from corrupt evidence.

Runtime endpoint ownership remains separate from heartbeat and process-lock
semantics. Process exclusion belongs to the held guard, while diagnostic
snapshots describe observed state. Scalar markers and rendered reports need
clear publication authority and atomic writes. Domain owners retain these
formats.
