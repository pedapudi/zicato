# zicato skills

Agent-driven workflows for operating zicato's self-improvement loop.
Each skill is a directory with a `SKILL.md` (name + description +
instructions, plus any helper scripts). They encode the right command
sequence, the artifacts to read, and the guardrails so a new user can
hand a task to an agent and get a correct, safe run.

To use a skill in an agent session, symlink or copy its directory into
your agent's skills directory. See [`../AGENTS.md`](../AGENTS.md) for
the operating rules every skill assumes (gate live runs,
`uv sync --all-extras`, report the dashboard URL, files-canonical).

## Catalog

The skills are organized by where they sit in the loop. The leverage
is highest at the top: the contract you author drives everything the
loop can discover.

**Designing a new target? Start with the DESIGN skills.** The `*-design-*`
and `manage-epochs-and-rounds` skills teach the *principles* — how to
structure a tournament, design a discriminating board, design judges,
and reason about epochs/rounds — distinct from the operational `author-*`
/ `tune-*` / `evolve` skills that drive the syntax and commands. A good
mental model (epochs/rounds) + a discriminating contract (board + judges
+ structure) is what makes the loop able to improve anything at all.

### Tier 0 — Foundations & setup
| Skill | What it does |
|---|---|
| `zicato-manage-epochs-and-rounds` | The mental model + operations for the epoch → round → generation → run hierarchy: what an epoch is (a sealed contract), contract-hash auto-epoching, the **two senses of "round"** (outer evolve round vs a tournament's inner rounds), champion/challenger vs parent/child, `champion_eval_mode`, the mandatory pre-run hypothesis, and the `zicato epoch` escape hatches. **Read this first.** |
| `zicato-bootstrap` | Zero-to-first-loop: scaffold a workspace, register an inner-harness adapter, wire the two `call_llm` callables, run the deterministic smoke loop, confirm the artifact tree. |

### Tier 1 — Author the evaluation contract (highest leverage)
*Design principles (WHAT + WHY)* — pair each with its operational sibling:
| Skill | What it does |
|---|---|
| `zicato-design-tournament-structure` | **Choose + configure** the per-epoch tournament structure — gauntlet / swiss / single_elim / double_elim / racing — and its params (`field_size`, `replicates`, swiss `rounds_n`, racing `eta`/`board_fraction`). The decision guide + the design principles (replication is the noise lever; the gate protects the incumbent; a racing rung cuts, it does not crown) + the `scoring.json` `tournament` block. |
| `zicato-design-boards` | **What belongs on a board**: coverage of the behaviors you care about, entries that actually separate champion from challenger (avoid all-pass/all-fail), single-turn vs scripted vs emulated, the `board_meta`/`disable_drift`/`judge_only` header. The principles behind `zicato-author-board`. |
| `zicato-design-judges` | **What to measure + how it weighs**: outcome expectations vs in-run process judges, drift kinds → loss, severity, the weighting knobs, and the collusion guard (judge callable distinct from the harness). The principles behind `zicato-author-board` + `zicato-tune-scoring`. |
| `zicato-design-proposer` | **Configure the proposing agent** — the two tiers (skill-composed default: drop `skills/*.md`, no code; vs a custom ADK `agent.py` with its own `model=` + the read-only tool registry), the Design-A model rule (proposer model must differ from the harness model), and that editing the proposer or a skill rolls the epoch like editing the brief. Registered with `register --proposer-path`. |

*Operational (the syntax + commands):*
| Skill | What it does |
|---|---|
| `zicato-author-board` | Write/extend `board.jsonl`: the three entry kinds (single-turn, scripted multi-turn, emulated multi-turn), the five expectation kinds, weights, tags, `board_meta`/`disable_drift`, the emulator two-callable rule. Validate with `zicato board`. |
| `zicato-write-brief` | Author/refine the proposer brief (`brief.md`): the epoch goal, the mutation budget, constraints, and the `## Forbidden` mutation ids the proposer may not touch. |
| `zicato-tune-scoring` | Edit `scoring.json`: drift-loss weights, `per_judge_weights`/`default_judge_weight`, pass/fail predicates, and the promotion gate (drift margin + pass-rate monotonicity). |

*GUI builder copilot (assemble the contract through a draft, apply rolls the epoch). The launch/integration model, the copilot↔draft mechanism, `builder.json`, and the consequence-forward principle are documented in [`docs/design/TOURNAMENT-BUILDER.md`](../docs/design/TOURNAMENT-BUILDER.md):*
| Skill | What it does |
|---|---|
| `zicato-build-tournament` | The **tournament-builder copilot's** whole-contract walkthrough — structure, `field_size`/`replicates`, per-structure params, the board & train/holdout split, the proposer, and the gate — edited as a DRAFT and applied only on confirmation. Consequence-forward: surface the COST (board-runs ≈ `field_size × replicates × rounds/rungs` + holdout-confirm) and the epoch-roll before every `apply`; never starts a live run. Defers structure to `zicato-design-tournament-structure`, board craft to `zicato-build-board`, holdout to `OVERFITTING.md`, proposer to `zicato-design-proposer`, gate to `SCORING.md`. |
| `zicato-build-board` | The **board-builder copilot's** deep board-craft guide — entries (single/multi-turn, expectations, the `holdout` tag, weight), judges (declared/in-run, `judge_name`, `per_judge_weights`, `board_meta`, the collusion-guarded emulator), and how the loss knobs combine to *shape* an objective. Designs for discrimination (avoid the toothless eval) and holds out a slice so the proposer can't memorise the board. Edits a DRAFT, applies on confirmation. Defers the scalar to `SCORING.md`, the schema to `BOARD-FORMAT.md`, anti-overfitting to `OVERFITTING.md`, discrimination diagnosis to `LOOP-HEALTH.md`. |

### Tier 2 — Run the loop
| Skill | What it does |
|---|---|
| `zicato-evolve` | Drive the meta-loop: choose rounds / mode / wall-clock budget / stop conditions, launch with the dashboard, report the URL. Enforces the live-run gate. The flagship operating skill. |
| `zicato-mutation-audit` | Audit the mutable surface with `zicato mutations`: enumerate span/file mutation points, preview current text, spot forbidden ids, decide what the proposer is allowed to change. |

### Tier 3 — Observe a run in flight
| Skill | What it does |
|---|---|
| `zicato-watch-dashboard` | Open and read the live "Console IV" dashboard: navigate Environment (fleet) → Epoch (champion-spine round timeline + loss-floor waterfall) → Generations/round Match-ups → Boards → Mutation surface → Publication; read the structure's match-up figure (racing survival funnel / swiss ladder / elim flow / gauntlet Δ-lanes); tell whether the loop improved; screenshot with browser-use. |
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
