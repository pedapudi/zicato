# Model engines and roles

Zicato separates an **engine**—a reusable model connection—from the **role**
that uses it. The target itself is adapter-defined and may be a library, rule
engine, deterministic program, or agent system; it need not consume an LLM.
When its adapter does accept a model assignment, the `target` role is the
target LLM (`target_llm`). Most model-backed workspaces need two engines:

- `target` supplies the optional target LLM to the adapter.
- `evaluation` is the default for Zicato's internal model work.

`config.json` stores credentials by environment-variable name rather than by
value:

```json
{
  "models": {
    "engines": {
      "target": {
        "model": "target-model",
        "revision": "deployment-2026-08-14",
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

The proposal episode itself runs in the Foe runtime and takes its model from
the `proposer.model` object of the workspace's `proposer` block (provider,
model, and backend options), described in
[PROPOSER.md](PROPOSER.md#the-configuration). The proposer roles in
`models.roles` select the engines for the calls Zicato makes around that
episode: best-of-N critique and the model-assisted recombination merge run
on `proposer_review`. Each best-of-N sample receives the `proposer_generate`
callable and model name on its proposal context; the Foe episode does not
read them, so they reach only an operator-supplied proposer class bound
through `runtime.proposer_agent`.

## Nouns

- **Engine**: a named, reusable connection: logical model id plus optional
  transport URL and credential-variable name.
- **Role**: the job for which an engine is selected.
- **Target**: the adapter-defined system being measured; it may consume no LLM.
- **Target LLM (`target` role)**: the optional model assignment injected into
  a model-capable target adapter. It must not share a named engine with
  evaluator-side roles.
- **Evaluation**: the default internal engine. Judge, user emulator, and
  proposer inherit it unless overridden.
- **Judge**: scores run behavior.
- **Adjudicator**: independently audits judge decisions. When adjudication is
  enabled it must be independent of the judge.
- **User emulator**: plays the user in multi-turn board entries. It is often a
  good place for a smaller engine.
- **Proposer**: the shared default for the two proposer roles below. The
  proposal episode's own model is configured in the `proposer` block.
- **Proposer generate**: the callable and model name supplied to each
  best-of-N sample's proposal context.
- **Proposer review**: critiques and selects among best-of-N candidates, and
  performs the model-assisted recombination merge.

## Overrides

Role values name engines. This configuration gives proposal review a strong
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

A proposer role resolves through this precedence:

1. `proposer_generate` or `proposer_review`, when present;
2. `proposer`;
3. `evaluation`.

For example, a strong critique engine with a smaller engine for the
sampling context is:

```json
{
  "roles": {
    "proposer": "strong",
    "proposer_generate": "small"
  }
}
```

Every other advanced role (`judge`, `adjudicator`, and
`user_emulator`) falls directly back to `evaluation`.

## Execution capabilities

| Configuration / consumer | What executes | Native tools or session? |
|---|---|---|
| Model-form `target` | Adapter receives a native model object when supported; text-only adapters receive the derived callable | Adapter-defined; native tool calling is preserved where supported |
| `call_llm`-form `target` | Text callable | No native tool binding; a tool-requiring adapter rejects the text shim |
| Proposal episode | Foe runtime with the `proposer.model` selection | Foe owns the model call and its editing tools; `models.roles` does not select this model |
| Model-form or `call_llm`-form proposer role | Text callable (plus model name for the model form) | Critique and recombination merge use it as a text call; an operator-supplied proposer class may consume the sampling context's callable |
| Inherited role | Same engine and capability as its inheritance source | No conversion is attempted |
| Judge / user emulator | Constrained text or structured call | Changing the engine does not change the call protocol |
| Adjudicator | Constrained text or structured call, separate from the judge | Must be independently configured when adjudication is active; a judge cannot audit itself |

Engine substitution selects a connection; it does not change a role's
execution protocol.

## Logical identity and transport

The evaluation contract captures every effective role after inheritance, including
its model or callable, revision, and declared transport. Callable implementation
source participates through the existing source hasher. Moving an endpoint changes
the captured execution description. A deployment changed behind a stable endpoint
still requires an operator-supplied revision change; the runtime cannot inspect
remote model weights.

Native connections also capture their resolved backend, project, location, endpoint,
and API version. Workers reconstruct that connection from the captured settings.
Credential values stay outside the contract. The native transport records either a
credential environment-variable name, a credential-file path, or null for both
references to select standard application-default credentials. The two references
cannot both be set. Credentials are resolved through the selected source and passed
explicitly to the client. Standard default credentials therefore remain available
without letting an unrelated ambient key replace them. Native preparation requires
the settings and credentials needed by the installed client to resolve its connection;
resolving platform credentials may require access to that platform.

An explicitly selected epoch rejects changed runtime roles before proposal work,
measurement reuse, or worker execution. Historical epochs without captured roles
remain readable but cannot authorize execution. Prepare a fresh epoch to execute
those workspaces. Role-key order and equivalent inheritance do not change the hash.

The standalone tournament APIs require a prepared epoch. Library callers can use
existing owners to retain explicit callable overrides:

```python
runtime = make_runtime_config(configuration, workspace_root=workspace,
                              target_call_llm=target, evaluation_call_llm=evaluator)
inputs = replace(resolve_contract_inputs(workspace, workspace_config=configuration),
                 execution_roles=execution_roles_for_runtime(runtime))
epoch = new_epoch(workspace, "evaluation", inputs.board_path, inputs.brief_path,
                  weights, contract=inputs)
```

Use the returned epoch and its generations for `run_tournament`, `run_matchup`, or
`run_fast_mode`. The fast path accepts the parent aggregate only when it matches the
selected epoch's canonical recorded score. These entry points do not create epochs
implicitly, since epoch creation also publishes the baseline and changes lineage.

The settings response includes effective role-to-engine resolution and whether
each mapping was explicit or inherited. A scrubbed tournament worker receives
only credential variables required by its captured roles and scoring declarations.

## Session scope

A harness session belongs to exactly one run: one generation × board entry ×
replicate. A session never spans a board or leaks state into another entry or
replicate. When a workflow needs several stateful turns, model it
as one compound board entry (for example, a multi-turn emulated entry); its
turns share that run's session while separate entries remain isolated.

## Validation

Configuration loading rejects unknown keys, unknown engine references, mixed
engine forms, endpoint-only engines, unset named credentials at resolution,
and target/evaluator engine reuse. These are configuration errors rather than
silent fallbacks.

Model connections are declared under `models.engines` and selected through
`models.roles`. The runtime block contains execution controls. Library callers
can supply resolved target and evaluation callables to `make_runtime_config`;
persisted workspaces declare those callables as named engines.
