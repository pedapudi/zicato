# Model engines and roles

Zicato separates an **engine**—a reusable model connection—from the **role**
that uses it. Most workspaces need two engines:

- `target` runs the system under test.
- `evaluation` is the default for Zicato's internal model work.

`config.json` stores credentials by environment-variable name, never value:

```json
{
  "models": {
    "engines": {
      "target": {
        "model": "target-model",
        "endpoint": "http://target-host:8080/v1",
        "api_key_env": "TARGET_MODEL_KEY"
      },
      "evaluation": {
        "model": "evaluation-model",
        "endpoint": "http://evaluation-host:8080/v1",
        "api_key_env": "EVALUATION_MODEL_KEY"
      }
    },
    "roles": {}
  }
}
```

The names `target` and `evaluation` are defaults, so the empty `roles` object
is sufficient. Engines may instead contain a `call_llm` import path, but a
single engine cannot mix `call_llm` with `model`, `endpoint`, or
`api_key_env`. An endpoint or credential name also requires `model`.

## Nouns

- **Engine**: a named, reusable connection: logical model id plus optional
  transport URL and credential-variable name.
- **Role**: the job for which an engine is selected.
- **Target**: the model inside the system being measured. It must not share an
  engine with evaluator-side roles.
- **Evaluation**: the default internal engine. Judge, user emulator, proposer,
  and builder inherit it unless overridden.
- **Judge**: scores run behavior.
- **Adjudicator**: independently audits judge decisions. When adjudication is
  enabled it must be independent of the judge.
- **User emulator**: plays the user in multi-turn board entries. It is often a
  good place for a smaller engine.
- **Proposer**: creates candidate changes. It often benefits from a stronger
  engine than routine evaluation.
- **Proposer breadth**: generates the best-of-N candidate slate.
- **Proposer depth**: critiques, selects, and revises candidates.
- **Builder**: assists an operator while editing the evaluation contract; it
  does not run tournament units.

## Overrides

Role values name engines. This configuration gives proposal work a strong
engine while assigning a smaller engine to the user emulator:

```json
{
  "models": {
    "engines": {
      "target": {"model": "target-model"},
      "evaluation": {"model": "evaluation-model"},
      "strong": {"model": "strong-proposal-model"},
      "small": {"model": "small-emulator-model"}
    },
    "roles": {
      "proposer": "strong",
      "user_emulator": "small"
    }
  }
}
```

The proposer precedence is deliberately narrow:

1. `proposer_breadth` or `proposer_depth`, when present;
2. `proposer`;
3. `evaluation`.

For example, cheap sampling with strong critique is:

```json
{
  "roles": {
    "proposer": "strong",
    "proposer_breadth": "small"
  }
}
```

Every other advanced role (`builder`, `judge`, `adjudicator`, and
`user_emulator`) falls directly back to `evaluation`.

## Logical identity and transport

An engine name identifies an operator-chosen logical deployment. `endpoint`
is only its transport address: moving the same deployment does not necessarily
change what is evaluated, while changing model weights behind a stable URL
does. Record deployment revisions in operational change control and use a new
engine name when the logical deployment changes. Credential values are read
only when an engine is resolved and are not written to workspace files, worker
argument files, logs, or dashboard responses.

The settings response includes effective role-to-engine resolution and whether
each mapping was explicit or inherited. A scrubbed tournament worker receives
only the credential variables named by configured engines.

## Validation

Configuration loading rejects unknown keys, unknown engine references, mixed
engine forms, endpoint-only engines, unset named credentials at resolution,
and target/evaluator engine reuse. These are configuration errors rather than
silent fallbacks.
