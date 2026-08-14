# Contract field registry

## Decision

`ScoringWeights` and its nested dataclasses are the schema. Their field
declarations carry defaults and `_knob` metadata; `contract_knobs()` derives a
read-only registry from those declarations at import time.

The registry is not code generation. Nothing derived from it is checked in.
Consumers project only what they own:

- contract canonicalization reads omit-at-default names;
- builder guards read operation and argument mappings;
- field-enumerating serialization reads the dataclasses directly.

This keeps one declaration behind hashing, serialization, and builder
coverage without adding a parallel schema.

## Compatibility

The change is structural. Canonical scoring bytes and hashes remain unchanged.
The frozen omit-set test and contract-hash parity gate detect any drift.

## Adding a field

1. Declare the field and validation on its owning dataclass.
2. Set `_knob(omit_at_default=True)` only for an additive default-off field.
3. Name its builder operation or record a reviewed exemption.
4. Run the registry guards, serializer completeness tests, and contract-hash
   parity gate.
