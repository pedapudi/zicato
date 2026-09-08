# Contract field registry

`ScoringWeights` and its nested dataclasses own the scoring configuration.
Their fields declare types, defaults, descriptions, persisted names, and
constraints. `contract_knobs()` derives the field registry from those
declarations for configuration help and operations.

`core.configuration.dataclass_to_jsonable` writes every declared field,
including nested defaults. `scoring_weights_from_dict` uses the shared strict
dataclass decoder for both authored and frozen configuration. Unknown fields,
invalid types, and violated constraints are errors. There is one supported
configuration shape and no historical-default decoder.

All effective scoring values participate in contract identity.
`epoch.contract` adds source identities for grading plugins and the declared
integration's canonical configuration and implementation identity. Defaults
are included; there is no omission metadata to preserve an earlier hash.

To add a field:

1. Declare its type, default, description, persisted name if needed, and constraints.
2. Connect its consumer and any affected validation or cost estimate.
3. Verify complete serialization and a meaningful non-default value at the consumer.
4. Verify equivalent authored values produce equal hashes and changed effective
   values produce the intended identity change.

Epoch publication saves complete effective configuration. A selected epoch
requires its captured `execution.json` and a contract hash containing exactly
64 lowercase hexadecimal characters. Measurements and recovery progress do
not enter that hash.
