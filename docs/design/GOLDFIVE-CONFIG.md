# Goldfive configuration

Goldfive is an optional runtime capability for harness adapters. An adapter
selects it by including `"goldfive"` in the `integrations` list returned by
its worker specification. Zicato's built-in Google ADK adapter makes this
declaration, and any other adapter may make the same declaration.

An adapter that selects Goldfive requires a `goldfive` object in
`scoring.json`. The object configures Goldfive's detectors, built-in judges,
steering, context editing, endpoints, and wrapped-agent limits. These settings
belong to the evaluation contract because they can change run behavior or the
evidence that determines champion selection.

## Configuration ownership

Zicato keeps the optional object as generic JSON. It freezes the loaded mapping,
copies it into the epoch snapshot and worker arguments, and includes its
normalized form in the contract hash. Zicato does not maintain a second copy of
Goldfive's field schema.

Goldfive's public `RuntimeConfigDocument` API owns the schema and its behavior:

| Operation | Goldfive responsibility |
|---|---|
| `from_mapping(...)` | Apply defaults and reject unknown fields, invalid JSON values, type errors, range errors, and invalid field combinations. |
| `scaffold(...)` | Produce a complete editable document with documented defaults. |
| `to_mapping()` | Return the canonical JSON representation. |
| `secret_env_names` | List the environment-variable names that hold endpoint credentials. |
| `missing_runtime_capabilities()` | Report optional Goldfive backends required by the document but unavailable in the installation. |
| `build(resolve_secret=...)` | Resolve credentials and construct Goldfive's runtime configuration. |

The Zicato bridge in `src/zicato/integrations/goldfive.py` imports this API
only when a Goldfive-enabled contract needs it. Core imports, generic adapters,
and contracts without a `goldfive` object therefore work without Goldfive
installed.

## Configure a workspace

`zicato init` creates a generic scoring configuration without a `goldfive`
object. Selecting the built-in ADK adapter with `zicato epoch register --adk`
adds an empty object without changing an existing Goldfive configuration. For
a custom adapter that declares the capability, add the object explicitly:

```json
{
  "goldfive": {
    "judge": {
      "base_url": "http://127.0.0.1:8080",
      "model": "local-judge",
      "revision": "sha256:3e79...",
      "api_key_env": "LOCAL_JUDGE_API_KEY"
    },
    "reasoning_drift": {
      "mode": "both",
      "fallback_to_content_when_no_reasoning": true
    },
    "steering": {
      "observation_only": true
    },
    "agent": {
      "call_timeout_ms": 900000
    }
  }
}
```

A sparse object is valid. Goldfive fills omitted fields from its documented
defaults. Zicato supplies one additional default: wrapped agent calls may run
for 1,800,000 milliseconds. An explicit
`goldfive.agent.call_timeout_ms` value takes precedence.

The Builder's Weights section can enable, edit, or remove the object. Enabling
it with `{}` asks Goldfive to apply every default and stores the complete
normalized document. Later edits merge into that document and pass through
Goldfive's validator. Opening the Builder section does not enable the
integration.

Run `zicato check` after editing the configuration. The check verifies that the
selected adapter and scoring object agree, asks Goldfive to validate and
normalize the document, checks optional runtime capabilities, and checks named
credentials. It performs no endpoint request.

## Keep credential values out of configuration

An endpoint's `api_key_env` field contains the name of an environment variable:

```sh
export LOCAL_JUDGE_API_KEY='...'
```

The name is ordinary contract data. Zicato stores and hashes it so operators
can discover which credential a run requires. The credential value remains in
the process environment. It does not enter `scoring.json`, epoch snapshots,
worker argument files, or contract hashes.

When worker-environment scrubbing is enabled, Zicato asks
`RuntimeConfigDocument.secret_env_names` which named variables the worker needs
and copies only those available values across the process boundary. Goldfive
resolves each value during `RuntimeConfigDocument.build(...)`. Validation and
build errors identify the variable name without including its value.

Zicato never calls Goldfive's environment-derived configuration loader.
Goldfive behavior variables therefore cannot silently change the bridge's
detectors, steering, endpoints, or limits. The wrapped target remains user code
and may read its own environment variables; an adapter must account for any
such input that affects evaluation behavior.

For a remote endpoint, use an absolute `http` or `https` URL without embedded
credentials, a query string, or a fragment. Set `revision` to a stable provider
revision or artifact digest when a URL and model name could resolve to
different weights over time. `zicato check` reports an unset revision as an
advisory because an unpinned endpoint weakens reproducibility.

## Install the capabilities a contract uses

Install only the optional capabilities selected by the adapter and document:

```sh
uv add 'zicato[goldfive]'                  # event runtime
uv add 'zicato[goldfive-remote]'           # remote judge or embedding endpoint
uv add 'zicato[goldfive-local-embedding]'  # local embedding detector
uv add 'zicato[adk]'                       # Google ADK adapter plus Goldfive support
```

Goldfive determines which optional runtime capabilities a document requires.
`zicato check` reports a missing capability before a tournament starts. The
`adk` extra is one adapter composition; Goldfive remains available to non-ADK
adapters.

## Adapter and board policies

Declaring `integrations: ["goldfive"]` makes the frozen Goldfive document and
its named credentials available to a worker. The adapter must consume the
document, normally through `zicato.integrations.goldfive`. A capability
declaration alone does not wrap the target or install ADK behavior.

A board's `judge_only` setting is a Zicato run policy. In this mode Goldfive
keeps its detectors and built-in and board-authored judges active while
suppressing goal derivation, planning, refinement, and live corrective
interventions. Goldfive's `steering.observation_only` setting has a different
scope: outside judge-only runs, it can record proposed corrections that
Goldfive still computes.

## Contract identity

Zicato canonicalizes an enabled object through
`RuntimeConfigDocument.to_mapping()`. Sparse and complete documents that mean
the same thing produce the same contract form. A semantic configuration change
opens a new epoch on the next `zicato evolve` unless auto-epoching is disabled.

The contract hash includes the required Goldfive package revision and Zicato
bridge revision. The epoch's `config.json` records both values under
`implementation_identity`, alongside the Zicato evaluator revision, so the
implementations are inspectable without reversing the hash. Upgrading either
Goldfive value opens a new epoch for Goldfive-enabled adapters. Contracts
without a `goldfive` object omit both Goldfive values, so a Goldfive release
does not affect them.
