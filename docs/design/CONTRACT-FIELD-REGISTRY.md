# Contract field registry

`ScoringWeights` and its nested dataclasses own the scoring schema. Field
declarations carry defaults, descriptions, persisted names, constraints, and
canonical omission metadata. `contract_knobs()` derives a read-only registry
from these declarations.

Canonicalization reads the declared omission rules. Serialization, generated
configuration help, and schemas read the same field declarations. Contract
operations consult the declared constraints before changing a value.

## Adding a field

1. Declare its type, default, description, and validation on the owning record.
2. Define its persisted name and contract-identity behavior. Use
   `omit_at_default` only when omitting that value preserves recorded meaning.
3. Update its runtime consumer, cost estimate, or validation where required.
4. Verify serialization and sparse/expanded identity, including retained
   historical records when canonical behavior changes.

Historical decoding and recorded-hash verification have explicit owners. A
constructor default must not silently change an archived evaluation contract.
