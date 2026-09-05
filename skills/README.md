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

## Shared model vocabulary

Every skill uses the named-engine schema documented in
[`MODEL-CONFIG.md`](../docs/design/MODEL-CONFIG.md): an **engine** is a reusable
model connection; a **role** selects an engine for a job. The common workspace
defines `target` (an optional LLM binding consumed only when the target adapter
needs one) and `evaluation` (the internal default). Advanced roles are
`proposer`, `proposer_generate`, `proposer_review`, `user_emulator`, `judge`,
`adjudicator`, and `builder`.

`proposer_generate` and `proposer_review` are generic best-of-N stages, not
reasoning settings. They inherit `proposer`, which inherits `evaluation`.
Native-slate proposers own both stages in one isolated proposal session, so
those two overrides are invalid for them. A `call_llm` engine is a constrained
text integration; it cannot stand in for native tools or a process-owned
session. The target itself may use no LLM at all.

A board is never a model-session boundary. Sessions belong to one run (one
generation × entry × replicate); intentional stateful turns belong in one
compound entry. This invariant applies throughout the catalog.

### Tier 0 — Foundations & setup
| Skill | What it does |
|---|---|
| `zicato-manage-epochs-and-rounds` | The mental model + operations for the epoch → round → generation → run hierarchy: what an epoch is (a sealed contract), contract-hash auto-epoching, the **two senses of "round"** (outer evolve round vs a tournament's inner rounds), champion/challenger vs parent/child, `champion_eval_mode`, the mandatory pre-run hypothesis, and the `zicato epoch` escape hatches. **Read this first.** |
| `zicato-bootstrap` | Zero-to-first-loop: scaffold a workspace, register a target adapter, configure named model engines and roles (or deterministic callable overrides), run the deterministic smoke loop, confirm the artifact tree. |
| `zicato-dev-guide` | **For contributors editing zicato itself** (not operating a workspace): the design goals, a file-by-file **map of the codebase**, the data/control flow for an evolve round and for the live dashboard, the **sharp edges** (subprocess weights-transport gap, the four distinct timeout mechanisms, dashboard CSS/cache pitfalls, the pre-commit no-op, `uv sync --all-extras`), and how to build/test/verify locally. Grounded in the source with file:line citations and flags where docs drift from code. |

### Tier 1 — Author the evaluation contract (highest leverage)
*Design principles (WHAT + WHY)* — pair each with its operational sibling:
| Skill | What it does |
|---|---|
| `zicato-design-tournament-structure` | **Choose + configure** the per-epoch tournament structure — gauntlet / racing, or the experimental swiss / single_elim / double_elim under `experimental.tournament_structures` — and its params (`field_size`, `replicates`, swiss `rounds_n`, racing `eta`/`board_fraction`). The decision guide + the design principles (replication is the noise lever; the gate protects the incumbent; a racing rung cuts, it does not crown) + the `scoring.json` `tournament` block. |
| `zicato-configure-tournament` | **The two nested levels + the cost/evolution mechanics** (alias: `configure-tournament`). Distinguishes the WITHIN-round bracket structure from the ACROSS-round `--rounds N` evolution loop, and the non-obvious fact that **the proposer learns from `prior_experiments` + `insights` every round even WITHOUT promotion** (promotion only moves the base genome). The cost estimator (`field_size × replicates × duels_or_rungs × rounds ÷ parallelism`, double-elim ~3–4×, throttling, `wall_clock_budget`×retry), a recommended starting config, and an **"is it actually evolving?"** checklist. Defers structure/param choice to `zicato-design-tournament-structure`, loop-driving to `zicato-evolve`; says "audit the board first with `zicato-audit-board`." |
| `zicato-design-boards` | **What belongs on a board**: coverage of the behaviors you care about, entries that actually separate champion from challenger (avoid all-pass/all-fail), single-turn vs scripted vs emulated, the `board_meta`/`disable_drift`/`judge_only` header. The principles behind `zicato-author-board`. |
| `zicato-design-judges` | **What to measure + how it weighs**: outcome expectations vs in-run process judges, drift kinds → loss, severity, weighting knobs, the `judge` / independent `adjudicator` roles, and target isolation. The principles behind `zicato-author-board` + `zicato-tune-scoring`. |
| `zicato-design-proposer` | **Configure the proposing agent** — the workspace's `proposer` block (the runtime binary, the episode budget, the model the runtime calls), the `proposer_generate` / `proposer_review` stages around the slate, isolation from the target engine, and the fact that editing the proposer or a skill rolls the epoch like editing the brief. The proposer directory is registered with `register --proposer-path`. |

*Operational (the syntax + commands):*
| Skill | What it does |
|---|---|
| `zicato-author-board` | Write/extend `board.jsonl`: the three entry kinds (single-turn, scripted multi-turn, emulated multi-turn), the five expectation kinds, weights, tags, `board_meta`/`disable_drift`, emulator isolation, and one-session-per-run boundaries. Validate with `zicato board`. |
| `zicato-write-brief` | Author/refine the proposer brief (`brief.md`): the epoch goal, the mutation budget, constraints, and the `## Forbidden` mutation ids the proposer may not touch. |
| `zicato-override-seams` | The three **override seams** for when the defaults don't fit: a custom `HarnessAdapter` for a non-ADK target (the mutation surface, `worker_spec`, and the telemetry you now owe — including the dialect choice and what each dialect can measure), the `predicate` expectation for partial credit or a per-entry `metrics` decomposition, and `outcome_summarizer_spec` for a proposer failure category zicato doesn't compute. Every seam attaches by dotted path; each fails quiet in its own way, so the skill says what silence looks like. |
| `zicato-tune-scoring` | Edit `scoring.json`: drift-loss weights, `per_judge_weights`/`default_judge_weight`, pass/fail predicates, and the promotion gate — Rule 0 diff-complexity ceiling (opt-in, `0.0` = off), Rule 1 scalar margin (`promote_margin`), Rule 2 pass-rate monotonicity (on, `per_entry`), Rule 3 per-namespace monotonicity (on for `rubric:` / `schema:`, off for `drift:`), then the holdout confirmation whenever the board has a holdout slice (on by default: any `holdout`-tagged entry, else a hash split once the board reaches 6 entries). |

*GUI builder copilot (assemble the contract through a draft, apply rolls the
epoch). Its model comes only from the named `builder` role; `builder.json`
contains presentation settings (skills and theme), never a second model
connection. The copilot↔draft mechanism and consequence-forward principle are
documented in [`docs/design/TOURNAMENT-BUILDER.md`](../docs/design/TOURNAMENT-BUILDER.md):*
| Skill | What it does |
|---|---|
| `zicato-build-tournament` | The **tournament-builder copilot's** whole-contract walkthrough — structure, `field_size`/`replicates`, per-structure params, the board & train/holdout split, the proposer, and the gate — edited as a DRAFT and applied only on confirmation. Consequence-forward: surface the COST (board-runs ≈ `field_size × replicates × rounds/rungs` + holdout-confirm) and the epoch-roll before every `apply`; never starts a live run. Defers structure to `zicato-design-tournament-structure`, board craft to `zicato-build-board`, holdout to `OVERFITTING.md`, proposer to `zicato-design-proposer`, gate to `SCORING.md`. |
| `zicato-build-board` | The **board-builder copilot's** deep board-craft guide — entries (single/multi-turn, expectations, the `holdout` tag, weight), judges, emulator isolation, one-session-per-run boundaries, and how loss knobs *shape* an objective. Designs for discrimination and holds out a slice so the proposer cannot memorize the board. Edits a DRAFT, applies on confirmation. |

### Tier 2 — Run the loop
| Skill | What it does |
|---|---|
| `zicato-evolve` | Drive the meta-loop: choose rounds / mode / wall-clock budget / stop conditions, launch with the dashboard, report the URL. Enforces the live-run gate. The flagship operating skill. |
| `zicato-mutation-audit` | Audit the mutable surface with `zicato inspect mutations`: enumerate span/file/code mutation points, preview current text, copy exact ids for the brief's `## Forbidden` list, decide what the proposer is allowed to change. |

### Tier 3 — Observe a run in flight
| Skill | What it does |
|---|---|
| `zicato-watch-dashboard` | Open and read the live console dashboard: navigate Environment (fleet) → Epoch (champion-spine round timeline + loss-floor waterfall) → Generations/round Match-ups → Boards → Mutation surface → Publication, plus the three epoch-scoped lenses — **Evals** (`#/evals`, the outcomes transpose: entries × candidates, shaded by evidence), **Instrument** (the board-reflection bill of health, judge audit, adjudication x-ray) and **Traces** (imported foreign trajectories + the board entries they motivated); read the structure's match-up figure (racing survival funnel / swiss ladder / elim flow / gauntlet Δ-lanes); tell whether the loop improved; screenshot with browser-use. |
| `zicato-diagnose-health` | Run `zicato health`, interpret the degeneracy detectors (degenerate scoring, no-expectations, dead-judge, …), and recommend the contract fix for a toothless loop. |
| `zicato-audit-board` | **Audit a board for TARGET correctness rather than candidate quality** (alias: `board-doctor`). The build → known-baseline-run → audit-the-RUN-for-mechanics loop, and the audit checklist: GT winnability, graded-artifact fidelity, judge-fire counts, scalar-ranking sanity, and determinism. Run before trusting any verdict, tournament, or evolution result. |
| `zicato-read-telemetry` | Trace a run through its harmonograf session and `events.jsonl`/`loss.json` (and the `telemetry_dialect` that produced it); tail an invocation's structured log with `zicato inspect logs`; relate the meta-loop session (zicato itself) to the per-board sessions (the system under test). |

### Tier 4 — Understand outcomes
| Skill | What it does |
|---|---|
| `zicato-analyze-epoch` | Close an epoch (`zicato epoch close`) and read its analysis: `analysis.md` + `journal.md` on disk, the hypothesis-vs-outcome ledger; re-render with `zicato repair report` / `analyze-telemetry`. |
| `zicato-tournament-forensics` | Explain a single promote/reject (`zicato tournament run PARENT CHILD`): verdict transparency, the per-entry A/B grid, the mutation heatmap, the score trajectory, and cost. |
| `zicato-lineage` | Read lineage across epochs and generations (`zicato epoch list` / `lineage.json`); compare champion vs challenger; drive the side-by-side conversation diff for a board entry. |

### Tier 5 — Manual control & forensics
| Skill | What it does |
|---|---|
| `zicato-step-loop` | Drive individual loop stages for debugging a single round: `zicato proposer propose` to create a candidate generation, `zicato tournament run PARENT CHILD` to score it, `zicato inspect telemetry` for the decision analysis. (`evolve` orchestrates these internally; there is no standalone `run`/`patch apply`.) |
| `zicato-index-ops` | Rebuild the SQLite analytical index (`zicato repair index`, `zicato repair generations`) and run cross-run read-only SQL against `.zicato/index.db` — the schema table-by-table, including what the index does NOT hold (replicates). |

### Tier 6 — Strategy
| Skill | What it does |
|---|---|
| `zicato-design-experiment` | Help the operator formulate the mandatory pre-run hypothesis, pick a mutation target the loss patterns justify, and predict the outcome before running. |
| `zicato-triage-stuck-loop` | The loop isn't improving — diagnose: degenerate evaluation vs too-hard board vs over-constrained forbidden set vs a proposer stuck in a rut, and prescribe the contract edit. |
