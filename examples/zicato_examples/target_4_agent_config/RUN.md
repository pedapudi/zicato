# target_4_agent_config — driving the target

Two recipes. The first runs everywhere and calls no model; the second
needs a real agent binary and an explicit operator go-ahead.

## Prerequisites

```bash
make install     # uv sync --all-extras, from a repo checkout
```

This installs `zicato` and `zicato-examples` editable, so
`zicato_examples.target_4_agent_config.*` resolves from anywhere —
including inside the spawned tournament worker subprocesses.

## The form that runs in continuous integration

[`tests/test_example_target_4_agent_config.py`](../../../tests/test_example_target_4_agent_config.py)
is the executable version of everything below: it enumerates the
markdown surface, validates the board, drives the driver against the
stub binary as a real subprocess, proves the snapshot's config package
is what gets mounted, checks the wall-clock abort, and applies a patch
to a marked region.

```bash
uv run pytest tests/test_example_target_4_agent_config.py
```

## 1. Drive one board entry, no model anywhere

`stub_agent.py` is a stand-in binary. It takes the same argv, speaks the
same remote-procedure protocol, and reads the same
`PI_CODING_AGENT_DIR`; its responses are scripted rather than decided.
Point the driver at it and run an entry by hand.

```bash
cd <your zicato checkout>

uv run python - <<'PYEOF'
import asyncio, json, os, sys

os.environ["ZICATO_TARGET_4_AGENT_BIN"] = (
    f"{sys.executable} -m zicato_examples.target_4_agent_config.stub_agent"
)
# The agent environment is an allowlist, so the stub's plan travels
# through the driver's explicit passthrough prefix.
os.environ["ZICATO_TARGET_4_AGENT_ENV_ZICATO_TARGET_4_STUB_PLAN"] = json.dumps(
    {"final": "renamed nothing", "writes": {"NOTES.md": "hello\n"}}
)

from zicato.board.jsonl import load_board
from zicato_examples.target_4_agent_config import predicates
from zicato_examples.target_4_agent_config.driver import EXAMPLE_DIR, make_adapter

entry = load_board(EXAMPLE_DIR / "board.jsonl")[0]
session = make_adapter().load(EXAMPLE_DIR)
result = asyncio.run(session.run(entry, [], None))

print("binary version:", session.agent_version)
print("patched:", sorted(predicates.patched_paths(result)))
print(result.final_output)
PYEOF
```

You should see the stub's final output, the `config-fingerprint:` line
it digests from the config package it was pointed at, and a unified diff
of `NOTES.md` after the sentinel.

## 2. Wire a workspace and audit the surface

`zicato epoch register`'s `--adk` flag covers only the agent-kit adapter
kind, so the generic `import`-kind block is written into `config.json`
directly — the same shape the adapter factory and the subprocess worker
both reconstruct. The convergence example uses the same pattern.

```bash
rm -rf /tmp/zicato-smoke-t4
mkdir -p /tmp/zicato-smoke-t4
cd /tmp/zicato-smoke-t4

# ZICATO is your zicato checkout; the two paths below derive from it.
ZICATO=${ZICATO:?set ZICATO to your zicato checkout}
EX=$ZICATO/examples/zicato_examples/target_4_agent_config
PY=$ZICATO/.venv/bin/python

# 1. Bootstrap the workspace.
$PY -m zicato.cli init --workspace .zicato

# 2. Declare the import-kind adapter and the config package as the
#    mutable tree. The entrypoint (driver.py) stays OUTSIDE the tree by
#    design — that is the dependency shape, and each run is verified to
#    have mounted the snapshot rather than the checkout.
$PY - <<PYEOF
import json, pathlib
cfg_path = pathlib.Path(".zicato/config.json")
cfg = json.loads(cfg_path.read_text())
cfg["adapter"] = {
    "kind": "import",
    "factory": "zicato_examples.target_4_agent_config.driver:make_adapter",
}
# The shell substitutes \$EX before python runs (unquoted heredoc).
cfg["mutable_trees"] = ["$EX/config_package"]
cfg["source_roots"] = ["$EX/config_package"]
cfg_path.write_text(json.dumps(cfg, indent=2) + "\n")
PYEOF

# 3. Publish the contract at the canonical location next to the
#    workspace.
cp $EX/board.jsonl ./board.jsonl
cp $EX/scoring.json ./scoring.json
cp $EX/brief.md     ./brief.md

# 4. Audit the surface the proposer will see: four ids, three `code`
#    regions and one `file`. settings.json is absent — strict JSON hosts
#    no marker.
$PY -m zicato.cli inspect mutations --workspace .zicato
```

Expected:

```
id                            kind   lines    file
agents_operating_rules        code   12-15    AGENTS.md
agents_tool_policy            code   21-23    AGENTS.md
skill_patch_discipline_rules  code   8-11     skills/patch-discipline.md
skill_repo_navigation         file   1-18     skills/repo-navigation.md

Total: 4 mutation point(s)  [code=3, file=1]  ~29 mutable line(s)
```

## 3. A live round — operator-initiated only

Everything above is hermetic. A live round is not, and no agent starts
one on its own: `zicato evolve` against this target spawns a real coding
agent for every board entry, in every generation, at every replicate.

Before a live round is worth running:

1. **Point the target at the pinned install** — the recommended
   default, and the same binary the proposer resolves:

   ```bash
   export ZICATO_TARGET_4_AGENT_BIN=$ZICATO/integrations/pi/node_modules/.bin/pi
   ```

   `npm install` in `integrations/pi/` materializes that path at the
   version `integrations/pi/package.json` pins, so the target and the
   proposer run the same pinned binary without sharing a knob. A bare
   `pi` on `PATH` is the degraded alternative: it works, but nothing
   pins what it resolves to.
2. Record the version. On the pinned route
   `integrations/pi/package.json` already fixes it; on the `PATH` route,
   run `pi --version` and record the result by hand. Either way a
   version change is an **epoch boundary**: rebase the baseline rather
   than comparing across it.
3. **Measure the same-versus-same floor.** Run the board with the
   champion against itself and look at the spread of the scalar. Until
   that number exists, `promote_margin` in `scoring.json` is the
   framework default rather than a calibrated threshold, and a
   "promotion" is indistinguishable from noise. Size
   `tournament.params.replicates` from the same data.
4. Only then run the loop, with the dashboard up:

```bash
$PY -m zicato.cli evolve --workspace .zicato --rounds 1 --mode full
```

`evolve` launches the dashboard and prints its URL (e.g.
`Dashboard: http://127.0.0.1:7892`).

## Known limitations

- **TypeScript files are not part of the surface.** The marker grammar
  has no `//` comment leader, which is one row in the mutation syntax
  table. This target evolves markdown only.
- **`settings.json` is permanently immutable.** Strict JSON cannot hold
  a marker without ceasing to be JSON.
- **The remote-procedure protocol is zicato's own shape rather than a
  published standard.** A binary that speaks a different wire needs a
  shim in `driver.py`.
- **Cost and noise are the binding constraint here, not mechanism.**
  Each entry is a full agentic run. See README.md, "Establishing the
  noise floor".
