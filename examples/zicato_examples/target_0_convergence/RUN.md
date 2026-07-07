# target_0_convergence — the known-answer convergence demo

This walkthrough runs `zicato evolve --rounds 3` against a
planted-defect target where **no LLM exists anywhere** — the harness is
deterministic, the proposer is a script — and the loop provably
converges: the champion scalar drops from `3.6` to the exact known
floor of `1.2` through a `promoted → rejected → promoted` decision
sequence. It is the smallest end-to-end proof that the FULL shipped
loop works: real propose → apply → validate → subprocess tournament
workers → reduce → gate → persist, under the default git
generation-store backend.

The CI-runnable form of this demo is
[`tests/test_convergence_known_answer.py`](../../../tests/test_convergence_known_answer.py),
which also pins the exact per-round scalars and the on-disk artifacts.

## How the known answer works

* [`agent/policy.py`](./agent/policy.py) carries the ONE mutation point
  (`style_rules`), seeded with three defect tokens:
  `verbose-prose; omit-summary; skip-citations`.
* The deterministic harness
  ([`harness.py`](./harness.py)) reads that policy from its own
  generation snapshot at run time. Every remaining token emits one
  `drift_detected` frame at severity `info` (`+1.0` drift loss per run),
  and each known token fails exactly one predicate on the five-entry
  board ([`board.jsonl`](./board.jsonl) / [`predicates.py`](./predicates.py)).
* With the contract in [`scoring.json`](./scoring.json)
  (`drift_weight = pass_weight = 1.0`, `runtime_weight = 0` — the zero
  runtime weight is load-bearing: per-run wall-clock varies, so any
  nonzero weight would break the exact floor):

  ```
  scalar(tokens, passes) = tokens + (1 - passes/5)

  v0  (3 tokens, 2/5 pass) = 3.6   seeded baseline
  v1  (2 tokens, 3/5 pass) = 2.4   round 1 removes omit-summary   → PROMOTED
  v2  (3 tokens, 2/5 pass) = 3.6   round 2 ADDS fabricate-metrics → REJECTED
  v3  (1 token,  4/5 pass) = 1.2   round 3 removes skip-citations → PROMOTED (the floor)
  ```

* The scripted proposer ([`mocks.py`](./mocks.py) `aux_llm`) serves
  exactly those three experiments, in order. Round 2 is the negative
  control: the gate must reject a strictly-worse child.

## Prerequisites

```bash
make install     # uv sync --all-extras, from a repo checkout
```

This installs `zicato` and `zicato-examples` editable, so
`zicato_examples.target_0_convergence.*` resolves from anywhere —
including inside the spawned tournament worker subprocesses.

## End-to-end demo (no endpoint anywhere)

```bash
rm -rf /tmp/zicato-smoke-t0
mkdir -p /tmp/zicato-smoke-t0
cd /tmp/zicato-smoke-t0

EX=/home/sunil/git/zicato/examples/zicato_examples/target_0_convergence
PY=/home/sunil/git/zicato/.venv/bin/python

# 1. Bootstrap the workspace.
$PY -m zicato.cli init --workspace .zicato

# 2. Declare the deterministic adapter + the mutable tree + the
#    skills-only proposer dir. `zicato register`'s --adk flag covers only
#    the ADK adapter kind today, so the generic import-kind adapter block
#    is written into config.json directly (the same shape the adapter
#    factory and the subprocess worker both reconstruct):
$PY - <<PYEOF
import json, pathlib
cfg_path = pathlib.Path(".zicato/config.json")
cfg = json.loads(cfg_path.read_text())
cfg["adapter"] = {
    "kind": "import",
    "factory": "zicato_examples.target_0_convergence.harness:make_adapter",
}
# The shell substitutes \$EX before python runs (unquoted heredoc).
cfg["mutable_trees"] = ["$EX/agent"]
cfg["source_roots"] = ["$EX/agent"]
contract = dict(cfg.get("contract") or {})
contract["proposer_path"] = "$EX/proposer"
cfg["contract"] = contract
cfg_path.write_text(json.dumps(cfg, indent=2) + "\n")
PYEOF

# 3. Publish the contract at the canonical location next to the
#    workspace (the streamlined evolve-centric flow: evolve auto-opens
#    the first epoch from these three files). The example vendors no
#    brief.md — the scripted proposer ignores its content, it only
#    shapes the frozen contract — so write a two-line one here. The
#    proposer dir configured in step 2 is skills-only (no agent.py),
#    which selects the single-shot text-shim proposer — driven entirely
#    by the scripted aux callable, so no model endpoint is ever needed.
cp $EX/board.jsonl ./board.jsonl
cp $EX/scoring.json ./scoring.json
cat > brief.md <<'EOF'
# Convergence brief
- Remove defect tokens from the writing policy, one per round.
- Never fabricate metrics.
EOF

# 4. Inspect the mutation surface (exactly one id: style_rules).
$PY -m zicato.cli mutations --workspace .zicato

# 5. Run the three scripted rounds — evolve auto-opens epoch e0 from
#    the contract above, then: v1 PROMOTED (3.6 → 2.4), v2 REJECTED
#    (the negative control), v3 PROMOTED (2.4 → 1.2 — the exact floor).
#    evolve launches the dashboard and prints its URL (e.g.
#    Dashboard: http://127.0.0.1:7892) — watch the bracket live.
$PY -m zicato.cli evolve --workspace .zicato \
    --rounds 3 --mode full \
    --harness-call-llm   zicato_examples.target_0_convergence.mocks:harness_llm \
    --auxiliary-call-llm zicato_examples.target_0_convergence.mocks:aux_llm

# 6. Close the epoch to produce analysis.md / analysis.html.
$PY -m zicato.cli epoch close --workspace .zicato
```

> Why not `epoch new`? An explicit `zicato epoch new` currently freezes
> the epoch WITHOUT the `contract.proposer_path` configured in step 2
> (the CLI does not thread it into the frozen epoch config), so the
> live contract hash differs and the first `evolve` auto-rolls to a
> fresh epoch — same rounds, same numbers, but under an auto-named
> epoch id. The streamlined flow above avoids the wart; the CI test
> (`tests/test_convergence_known_answer.py`) pins the epoch by calling
> `new_epoch(..., proposer_path=...)` directly.

## The racing variant

[`scoring.effective.json`](./scoring.effective.json) is the same
contract under a **racing** tournament (`field_size: 4`,
`replicates: 2`) with the Bradley–Terry evidence pre-gate enabled
(`promote_confidence_threshold: 0.8`). Use
`mocks:racing_aux_llm` as the auxiliary callable — it serves four
GENUINELY DISTINCT experiments per round whose defect-token sets form a
strict superset chain, so the rung cuts are fully deterministic and the
best-known arm (only `verbose-prose` left → scalar `1.2`) survives to
be crowned.

One tuning note: `promote_confidence_replicates` is raised to `32`.
With a fully deterministic harness every crowning duel is decisive in
the same direction, and Bradley–Terry confidence intervals separate
slowly on an all-decisive audit (the fit is evidence-starved, not
noisy) — the pre-gate needs ~25 cache-cheap replicate duels before the
CIs clear. The default budget of 3 would end the round `inconclusive`
(champion stands) even at `P(theta_child > theta_champion) ≈ 0.95`.

Run it from a FRESH workspace (repeat steps 1–3 in a new scratch dir,
publishing `scoring.effective.json` as the live `scoring.json`). Do not
chain it after the gauntlet demo above: a contract roll seeds the new
epoch's `v0` from the previous epoch's promoted head, which is already
AT the floor — every racing arm then ties the champion at `1.2` and is
correctly rejected.

```bash
# In a fresh scratch dir, after steps 1-2 (init + config.json):
cp $EX/board.jsonl            ./board.jsonl
cp $EX/scoring.effective.json ./scoring.json
cat > brief.md <<'EOF'
# Convergence brief
- Remove defect tokens from the writing policy, one per round.
- Never fabricate metrics.
EOF

# One racing round: four distinct challengers race off v0; the best arm
# (v2, scalar 1.2) survives every rung and is PROMOTED at the floor.
$PY -m zicato.cli evolve --workspace .zicato \
    --rounds 1 --mode full \
    --harness-call-llm   zicato_examples.target_0_convergence.mocks:harness_llm \
    --auxiliary-call-llm zicato_examples.target_0_convergence.mocks:racing_aux_llm
```

## Deferred: the live variant

A live variant of this target — same board, same predicates, but a real
model editing the policy through the default tool-using proposer —
is deliberately NOT part of this example yet. The value of target_0 is
that its floor is exact; introducing a live proposer makes the decision
sequence model-dependent and belongs in a separate exercise (with an
operator go-ahead, real endpoints configured under `models`, and the
dashboard watched live). Until then, the deterministic script is the
canonical convergence check.
