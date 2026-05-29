# zicato skills

Agent-driven workflows for operating zicato's self-improvement loop.
Each skill is a directory with a `SKILL.md` (name + description +
instructions, plus any helper scripts). They encode the right command
sequence, the artifacts to read, and the guardrails so a new user can
hand a task to an agent and get a correct, safe run.

To use a skill in a Claude Code session, symlink or copy its directory
into `.claude/skills/<name>/`. See [`../AGENTS.md`](../AGENTS.md) for
the operating rules every skill assumes (gate live runs,
`uv sync --all-extras`, report the dashboard URL, files-canonical).

## Catalog

The skills are organized by where they sit in the loop. The leverage
is highest at the top: the contract you author drives everything the
loop can discover.

### Tier 0 — Setup
| Skill | What it does |
|---|---|
| `zicato-bootstrap` | Zero-to-first-loop: scaffold a workspace, register an inner-harness adapter, wire the two `call_llm` callables, run the deterministic smoke loop, confirm the artifact tree. |

### Tier 1 — Author the evaluation contract (highest leverage)
| Skill | What it does |
|---|---|
| `zicato-author-board` | Write/extend `board.jsonl`: the three entry kinds (single-turn, scripted multi-turn, emulated multi-turn), the five expectation kinds, weights, tags, `board_meta`/`disable_drift`, the emulator two-callable rule. Validate with `zicato board`. |
| `zicato-write-brief` | Author/refine the proposer brief (`brief.md`): the epoch goal, the mutation budget, constraints, and the `## Forbidden` mutation ids the proposer may not touch. |
| `zicato-tune-scoring` | Edit `scoring.json`: drift-loss weights, `per_judge_weights`/`default_judge_weight`, pass/fail predicates, and the promotion gate (drift margin + pass-rate monotonicity). |

### Tier 2 — Run the loop
| Skill | What it does |
|---|---|
| `zicato-evolve` | Drive the meta-loop: choose rounds / mode / wall-clock budget / stop conditions, launch with the dashboard, report the URL. Enforces the live-run gate. The flagship operating skill. |
| `zicato-mutation-audit` | Audit the mutable surface with `zicato mutations`: enumerate span/file mutation points, preview current text, spot forbidden ids, decide what the proposer is allowed to change. |

### Tier 3 — Observe a run in flight
| Skill | What it does |
|---|---|
| `zicato-watch-dashboard` | Open and read the dashboard (L0 workspace → L1 epoch → L2 generation → L3 round → L4 run), screenshot it, narrate what's happening, and follow harmonograf deep-links. |
| `zicato-diagnose-health` | Run `zicato health`, interpret the degeneracy detectors (degenerate scoring, no-expectations, …), and recommend the contract fix for a toothless loop. |
| `zicato-read-telemetry` | Trace a run through its harmonograf session and `events.jsonl`/`loss.json`; relate the meta-loop session (zicato itself) to the per-board sessions (the system under test). |

### Tier 4 — Understand outcomes
| Skill | What it does |
|---|---|
| `zicato-analyze-epoch` | Close an epoch (`zicato epoch close`) and read its analysis: `analysis.md` + `journal.md` on disk, the hypothesis-vs-outcome ledger; re-render with `zicato regenerate-report` / `analyze-telemetry`. |
| `zicato-tournament-forensics` | Explain a single promote/reject (`zicato tournament PARENT CHILD`): verdict transparency, the per-entry A/B grid, the mutation heatmap, the score trajectory, and cost. |
| `zicato-lineage` | Read lineage across epochs and generations (`zicato epoch list` / `lineage.json`); compare champion vs challenger; drive the side-by-side conversation diff for a board entry. |

### Tier 5 — Manual control & forensics
| Skill | What it does |
|---|---|
| `zicato-step-loop` | Drive individual loop stages for debugging a single round: `zicato propose` to create a candidate generation, `zicato tournament PARENT CHILD` to score it, `zicato analyze-telemetry` for the decision analysis. (`evolve` orchestrates these internally; there is no standalone `run`/`patch apply`.) |
| `zicato-index-ops` | Rebuild the SQLite analytical index (`zicato reindex`, `zicato reindex-generations`) and run cross-run read-only SQL against `.zicato/index.db`. |

### Tier 6 — Strategy
| Skill | What it does |
|---|---|
| `zicato-design-experiment` | Help the operator formulate the mandatory pre-run hypothesis, pick a mutation target the loss patterns justify, and predict the outcome before running. |
| `zicato-triage-stuck-loop` | The loop isn't improving — diagnose: degenerate evaluation vs too-hard board vs over-constrained forbidden set vs a proposer stuck in a rut, and prescribe the contract edit. |
