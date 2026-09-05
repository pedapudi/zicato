---
name: zicato-bootstrap
description: Tier 0 setup — scaffold a fresh .zicato/ workspace, register a target adapter + mutable trees, configure named model engines and roles, and prove the loop end-to-end against the deterministic mock target before spending any real LLM budget. Use this when starting zicato on a new project, wiring a target, or sanity-checking the plumbing.
---

# zicato bootstrap — zero to first loop

Get a workspace from nothing to a confirmed artifact tree using deterministic
mocks. No real model calls, no budget spent. Once this passes, an operator can
swap in real LLMs with `skills/zicato-evolve`.

This is the path for wiring a system under test the operator already has. To
see the same seven artifacts already wired and running first, `zicato init
--example` scaffolds a complete project that needs no model and no endpoint;
the README's quickstart runs it end to end.

Always invoke the CLI from the project's `.venv` (`.venv/bin/zicato ...` or
`.venv/bin/python -m zicato.cli ...`). Use `uv sync --all-extras` to install —
never bare `uv sync` (it strips the dev extras, incl. pytest/ruff/mypy). The hard rules cited here live in
the repo-root `AGENTS.md`.

## 1. Scaffold the workspace (once per project)

```sh
.venv/bin/zicato init --workspace .zicato --instance-id my-project
```

Writes `.zicato/config.json` (identity, storage, and a guided empty `models`
section) and an empty
`.zicato/lineage.json` (`{"epochs": []}`). Refuses to clobber an
existing workspace without `--force` (and `--force` only rewrites
config/lineage — it never deletes epoch artifacts).

## 2. Register the adapter + the mutable tree(s)

`register` records the target-adapter identity and the source roots the proposer
is allowed to rewrite. It merges into `config.json` (preserves the keys `init`
wrote).

```sh
.venv/bin/zicato epoch register --workspace .zicato \
    --adk my_pkg.agent:root_agent \
    --mutable-tree ./my_pkg
```

- `--adk module.path:agent_symbol` — the ADK adapter entrypoint (required). Two
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
  refuses that up front (issue #110). Point it at the importable PACKAGE dir
  (`--mutable-tree $EX/agent`, not `$EX`).
- `--board` / `--brief` / `--scoring` — optional; pin the canonical contract
  paths up front (default: alongside the workspace parent). `evolve` resolves
  these itself, so you usually leave them.

## 3. Configure model engines only where the adapter needs them

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
`adjudicator`, and `builder`. See
[`MODEL-CONFIG.md`](../../docs/design/MODEL-CONFIG.md) before adding advanced
overrides. A dotted `call_llm` engine is the advanced text-only/offline form;
it is not interchangeable with a native tool runtime or process-owned model
session.

`zicato evolve` takes no model options. An engine may name a `call_llm`
dotted path instead of a `model`, which is how a deterministic smoke test or
a library integration supplies its own callable; the `target` and
`evaluation` engines must then resolve to different Python objects.

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

You should see one row per `# zicato:mutable id="..."` marker, no warnings, no
duplicate ids. For a deeper audit (forbidden ids, `--show full`, JSON), use
`skills/zicato-mutation-audit`.

## 5. Run the deterministic mock target end-to-end

The vendored presentation target ships byte-deterministic mock LLMs — they
exercise the full propose -> apply -> snapshot -> tournament -> persist ->
journal path without spending budget. Run it from a scratch workspace to prove
your environment is wired correctly:

```sh
EX=examples/zicato_examples/target_1_presentation
PY=.venv/bin/python

rm -rf /tmp/zicato-smoke && mkdir -p /tmp/zicato-smoke && cd /tmp/zicato-smoke

$PY -m zicato.cli init --workspace .zicato
$PY -m zicato.cli epoch register --workspace .zicato \
    --adk agent.agent:root_agent \
    --mutable-tree "$OLDPWD/$EX/agent"
$PY -m zicato.cli epoch new t1_smoke --workspace .zicato \
    --board "$OLDPWD/$EX/board.jsonl" \
    --brief "$OLDPWD/$EX/rubric.md" \
    --scoring "$OLDPWD/$EX/scoring.json"
# `evolve` takes no model options: an engine naming a `call_llm`
# dotted path is how these deterministic mocks reach the two roles.
$PY - <<'PYEOF'
import json, pathlib
cfg_path = pathlib.Path(".zicato/config.json")
cfg = json.loads(cfg_path.read_text())
cfg["models"] = {
    "engines": {
        "target": {"call_llm": "zicato_examples.target_1_presentation.mocks:target_llm"},
        "evaluation": {"call_llm": "zicato_examples.target_1_presentation.mocks:aux_llm"},
    },
    "roles": {},
}
cfg_path.write_text(json.dumps(cfg, indent=2) + "\n")
PYEOF
$PY -m zicato.cli inspect mutations --workspace .zicato   # lists the example's mutable ids
$PY -m zicato.cli evolve --workspace .zicato \
    --rounds 1 --mode full --no-dashboard
```

`epoch new` is shown explicitly here; `evolve` will auto-open/auto-roll epochs
on its own if you skip it (place `board.jsonl` / `brief.md` / `scoring.json`
next to the workspace and let `evolve` resolve the contract). The
`examples/zicato_examples/target_1_presentation/RUN.md` walkthrough is the
canonical reference.

## What success looks like

- `evolve` exits 0 and prints a JSON array, one object per round. With the mock,
  expect `tournament_decision: "rejected"` and `delta_scalar: 0.0` — the
  deterministic mock makes parent and child byte-equivalent, so the gate fires
  "insufficient improvement / margin". **This is the correct outcome.**
- The stderr `goldfive.planner: JSON parse failed` warnings are expected: the
  mock returns prose rather than planner JSON. The plumbing still records real
  `events.jsonl` per entry.
- The artifact tree exists under
  `.zicato/epochs/<id>/generations/{v0,v1,...}/` — each generation has
  `snapshot/`, `runs/<entry>/events.jsonl`, and (for non-baseline) `patches/`
  + `experiment.json`. Spot-check:

```sh
cat .zicato/lineage.json                                 # epochs + generations DAG
cat .zicato/epochs/*/generations/v1/patches/*.json       # the lifted Patch
```

Once this passes the plumbing is proven. Hand off to `skills/zicato-evolve`
(configure live named engines and roles) — and remember the
**live-run gate**: never start a real-LLM `evolve` without the user's explicit
go-ahead.

## Reference

- [docs/design/DOGFOOD-TARGETS.md](../../docs/design/DOGFOOD-TARGETS.md) — the three targets.
- [docs/design/ARCHITECTURE.md](../../docs/design/ARCHITECTURE.md) — read first; the meta-loop.
- [docs/design/MUTATION-SURFACE.md](../../docs/design/MUTATION-SURFACE.md) — marker syntax.
- [examples/zicato_examples/target_1_presentation/RUN.md](../../examples/zicato_examples/target_1_presentation/RUN.md) — full worked walkthrough.
