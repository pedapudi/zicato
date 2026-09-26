---
name: zicato-bootstrap
description: Setup — scaffold a fresh .zicato/ workspace, register a target adapter + mutable trees, configure named model engines, roles and the proposal runtime, validate the wiring without model requests, and prove the loop end-to-end on the deterministic example project before spending any real LLM budget. Use this when starting zicato on a new project, wiring a target, or sanity-checking the plumbing.
---

# zicato bootstrap — zero to first loop

Get a workspace from nothing to a confirmed artifact tree. No real model
calls, no budget spent. Once this passes, an operator can swap in real LLMs
with `skills/zicato-evolve`.

Steps 1–4 wire a system under test the operator already has. Step 5 proves the
loop itself on `zicato init --example`, a complete project that needs no model
and no endpoint; the README's quickstart runs the same project.

Always invoke the CLI from the project's `.venv` (`.venv/bin/zicato ...` or
`.venv/bin/python -m zicato.cli ...`). Use `uv sync --all-extras` to install —
never bare `uv sync` (it strips the dev extras, incl. pytest/ruff/mypy). The hard rules cited here live in
the repo-root `AGENTS.md`.

## 1. Scaffold the workspace (once per project)

```sh
.venv/bin/zicato init --workspace .zicato --instance-id my-project
```

Writes `.zicato/config.json` (identity, `generation_source_backend`, a guided
empty `models` section, and a `proposer` block whose `binary` is the
placeholder `/path/to/foe`), an empty `.zicato/lineage.json`
(`{"epochs": []}`), and — only when absent — an empty `scoring.json` (`{}`)
next to the workspace, which resolves to the recommended contract. Refuses to
clobber an existing workspace without `--force`; `--force` rewrites
config/lineage and never deletes epoch artifacts, and it refuses a lineage that
already records epochs unless `--reset-lineage` is also passed.

## 2. Register the adapter + the mutable tree(s)

`zicato epoch register` records the target-adapter identity and the source roots the proposer
is allowed to rewrite. It merges into `config.json` (preserves the keys `init`
wrote).

```sh
.venv/bin/zicato epoch register --workspace .zicato \
    --adk my_pkg.agent:root_agent \
    --mutable-tree ./my_pkg
```

- `--adk module.path:agent_symbol` — the ADK adapter entrypoint. Two
  shapes are supported. **In-tree** (above): the entrypoint's top-level module
  IS the basename of one `--mutable-tree` (`my_pkg` ↔ `./my_pkg`) — verified
  lexically at register time. **Dependency shape**: the entrypoint lives outside
  every tree and the harness *imports* the mutable trees. The goldfive-steering
  example takes this form: mutate goldfive, and drive it from a module outside
  it. `epoch register` accepts the shape and prints a `NOTICE`, because whether
  the mutated tree actually ran depends on run-time imports. That question is
  answered per run instead, by the load-time resolution assert and the post-run
  `harness_load.json` record.
- `--mutable-tree PATH` — a source root the proposer may mutate; **repeatable**,
  pass it once per tree. Its **basename must be the importable package name**: a
  generation snapshot copies each tree under its basename and the loader only
  prepends the snapshot root to `sys.path`, which resolves top-level names only.
  A tree whose basename Python cannot name can never be shown to have run from
  the snapshot — every mutation to it would be a scored no-op — so `register`
  refuses that up front. Point it at the importable PACKAGE dir
  (`--mutable-tree ./src/my_pkg`, not `./src`).
- `--board` / `--brief` / `--scoring` — optional; pin the canonical contract
  paths up front (default: alongside the workspace parent). `evolve` resolves
  these itself, so you usually leave them.
- A target that is not an ADK agent registers a factory instead of `--adk`:
  `--factory module:callable` (plus `--factory-args` / `--factory-options` and
  a fixed `--import-root` for the driver). See `skills/zicato-override-seams`.

`epoch register` prints the next step: `zicato inspect setup --workspace .zicato`
imports the driver, loads one snapshot in a bounded subprocess, and checks the
grading hooks and configuration without calling a model or running a board
entry. Run it after every wiring change.

## 3. Configure the model engines and the proposal runtime

The target is adapter-defined: it may be a deterministic program, external
service, library, or model-backed agent. Do not configure a target LLM merely
because the role exists. A model-capable adapter consumes the optional
`target` role; an adapter that owns its transport or uses no model ignores it.

For a model-backed workspace, define reusable connections under
`models.engines` and assign jobs under `models.roles`. Engines named `target`
and `evaluation` are the defaults, so the common case needs no role mappings:

```json
{
  "models": {
    "engines": {
      "target": {"model": "target-model"},
      "evaluation": {"model": "evaluation-model"}
    },
    "roles": {}
  }
}
```

An engine is a logical model plus optional `endpoint`, `api_key_env`, and
operator-declared `revision`. A role is the job that selects an engine.
Credentials stay in environment variables. The generated `_guide` object in
`config.json` defines every noun and includes an inactive override example;
it is documentation rather than runtime input.

`evaluation` supplies internal work by default. Override narrowly when the
jobs need different capability or cost, for example:

```json
{
  "models": {
    "engines": {
      "target": {"model": "target-model"},
      "evaluation": {"model": "general-model"},
      "strong": {"model": "strong-model"},
      "small": {"model": "economical-model"}
    },
    "roles": {"proposer": "strong", "user_emulator": "small"}
  }
}
```

The supported roles are `target`, `evaluation`, `proposer`,
`proposer_generate`, `proposer_review`, `user_emulator`, `judge`,
and `adjudicator`. See
[`MODEL-CONFIG.md`](../../docs/design/MODEL-CONFIG.md) before adding advanced
overrides. A dotted `call_llm` engine is the advanced text-only/offline form;
it is not interchangeable with a native tool runtime or process-owned model
session.

`zicato evolve` takes no model options. An engine may name a `call_llm`
dotted path instead of a `model`, which is how a deterministic smoke test or
a library integration supplies its own callable; the `target` and
`evaluation` engines must then resolve to different Python objects.

The proposal runtime is configured separately, in the `proposer` block `init`
wrote. Replace the placeholder `binary` with the absolute path of the Foe
executable and fill in `model.provider` / `model.model`; a round refuses to
open while the placeholder remains. The block, its budget, and the
alternative `runtime.proposer_agent` class seam are covered in
`skills/zicato-design-proposer`.

If a text backend exposes separate private-reasoning and answer channels, its
module-level callable may opt into `zicato.reasoning.reasoning_aware_call_llm`.
The backend accepts `ModelRequest`, returns `ModelResponse`, and declares both
channel separation and backend-level reasoning control. The adapter returns
only `content`; it never substitutes or persists private reasoning. It retries
once with reasoning disabled only when the backend explicitly reports
`answer_status="exhausted"`. Do not wrap native tool runtimes with this text
adapter. See
[`REASONING-MODELS.md`](../../docs/design/REASONING-MODELS.md).

## 4. Inspect the mutable surface

Confirm every marker resolves cleanly before running the loop:

```sh
.venv/bin/zicato inspect mutations --workspace .zicato
```

You should see one row per `zicato:mutable id="..."` marker, no warnings, no
duplicate ids, and a `Total: N mutation point(s)` footer. For a deeper audit
(forbidden ids, `--show full`, JSON), use `skills/zicato-mutation-audit`.

## 5. Prove the loop on the deterministic example project

`zicato init --example` writes a complete project next to a fresh workspace: a
system under test with one mutable span, an import-kind adapter that runs it,
predicates that grade it, a scripted proposer class bound through
`runtime.proposer_agent`, deterministic callables for the `target` and
`evaluation` engines, a four-entry board, a brief, and a scoring contract.
Nothing in it calls a model. Run it in a scratch directory:

```sh
ZICATO=/path/to/zicato/checkout
rm -rf /tmp/zicato-smoke && mkdir -p /tmp/zicato-smoke && cd /tmp/zicato-smoke

$ZICATO/.venv/bin/zicato init --example
$ZICATO/.venv/bin/zicato inspect setup --workspace .zicato
$ZICATO/.venv/bin/zicato evolve --workspace .zicato --rounds 1 --no-dashboard
```

No `epoch new` is needed: `evolve` finds no current epoch, resolves the
contract from `board.jsonl`, `brief.md` and `scoring.json` beside the
workspace, and opens epoch `e0`.

## What success looks like

- `evolve` exits 0, prints `evolve: completed all 1 requested rounds.`, and
  prints a JSON array with one object per round: `parent_generation_id`,
  `proposed_generation_id`, `parent_scalar`, `child_scalar`, `delta_scalar`,
  `tournament_decision`, `rejection_reason`. The example's first round
  promotes `v1` over `v0` (`delta_scalar: -0.25`).
- The artifact tree exists under `.zicato/epochs/<epoch_id>/`:
  `generations/{v0,v1}/` each hold `gen_score.json`, `harness_load.json`,
  `experiment.json` and `runs/<entry_id>/seed-none/` (one
  `loss.<purpose>.r<draw>.json` and `result.<purpose>.r<draw>.json` per
  measurement); `v1` also holds `patches/*.json`; `rounds/0/` holds
  `round_log.jsonl` and `field_settlement.json` (the settled outcome). The
  source trees live in `.zicato/repo-worktrees/<epoch_id>/<generation>/`
  under the default git backend, or `generations/<generation>/snapshot/`
  under the directory backend.

Spot-check:

```sh
.venv/bin/zicato epoch list --workspace .zicato             # promoted / rejected counts per epoch
cat .zicato/epochs/*/generations/v1/patches/*.json          # the lifted Patch
jq '.candidates[] | {generation_id, decision: .outcome.tournament_decision}' \
   .zicato/epochs/*/rounds/0/field_settlement.json          # the recorded outcome
```

Once this passes the plumbing is proven. Hand off to `skills/zicato-evolve`
(configure live named engines, roles and the proposal runtime) — and remember
the **live-run gate**: never start a real-LLM `evolve` without the user's
explicit go-ahead.

## Reference

- [docs/design/DOGFOOD-TARGETS.md](../../docs/design/DOGFOOD-TARGETS.md) — the three targets.
- [docs/design/ARCHITECTURE.md](../../docs/design/ARCHITECTURE.md) — read first; the meta-loop.
- [docs/design/MUTATION-SURFACE.md](../../docs/design/MUTATION-SURFACE.md) — marker syntax.
- [examples/zicato_examples/target_0_convergence/RUN.md](../../examples/zicato_examples/target_0_convergence/RUN.md) — a deterministic target with a known answer, driven end to end.
