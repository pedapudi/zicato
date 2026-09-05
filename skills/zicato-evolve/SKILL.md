---
name: zicato-evolve
description: Tier 2 run — the flagship operating skill for driving `zicato evolve` (the self-improving loop). Choose --rounds / --mode / --max-wall-clock-seconds / stop conditions, launch with the live dashboard, and report the URL. Use this to run an evolve loop. ENFORCES the live-run gate — a real-LLM evolve requires the user's explicit go-ahead.
---

# zicato evolve — drive the loop

`zicato evolve` is the single happy-path entry point. It is self-orchestrating:
it resolves the evaluation contract (board + proposer brief + scoring + the
registered target-adapter identity), auto-opens/auto-rolls the epoch if that
contract has drifted, then runs propose -> tournament -> promote for `--rounds`
rounds. You do **not** run `register` / `propose` / `tournament` / `reindex` /
`epoch` by hand — `evolve` drives them.

Always invoke via `.venv/bin/zicato`. Install with `uv sync --all-extras`, never
bare `uv sync`. The hard rules cited here live in the repo-root `AGENTS.md`.

## THE LIVE-RUN GATE (read before doing anything)

A real-LLM `evolve` spends model budget. **Never launch a real-LLM evolve
without the user's explicit go-ahead in this conversation.** This is hard
rule 1 in the repo-root `AGENTS.md`.

To verify your wiring without spending budget, do **not** run a live loop —
instead:

- Run the test suite (`.venv/bin/pytest`) and any CI checks.
- Run the **deterministic mock target** end-to-end (see
  `skills/zicato-bootstrap`, step 4 — the `mocks:target_llm` / `mocks:aux_llm`
  callables). This exercises the full path with zero budget.

Only after the user says go do you run with live engines. Which model each
role runs on is a property of the workspace, named under `models.engines` and
`models.roles` in `.zicato/config.json`. An engine may give a `call_llm` dotted
path in place of a `model`, which is how a deterministic or offline
integration supplies its own callable; the `target` and `evaluation` engines
must then resolve to different Python objects.

## Happy-path invocation

```sh
.venv/bin/zicato evolve --workspace .zicato --rounds 4
```

The dashboard is launched automatically; its URL is printed
(`Dashboard: http://127.0.0.1:7892`). Always surface that URL to the user.

## The flags that matter

`evolve` takes no model options. A workspace that has not answered the
`target` and `evaluation` roles is refused before the round opens, naming the
`models.engines` entry or the `runtime.<role>_call_llm` dotted path to fill.

| Flag | Use |
|---|---|
| `--rounds INTEGER` | Number of propose/tournament/promote rounds to attempt (default 1, must be >=1). |
| `--mode full\|fast` | `fast` (default) = **cache-first**: every `(generation, entry, replicate)` board UNIT is evaluated at most once and reused across all pairings / rounds / structures — only cache misses run. So a carried champion is reused rather than re-run (the round records `champion_eval_mode` = `fast`, or `fast-degraded` when some units had to run to seed the cache). The contract's `replicates` knob reaches this path: on the gauntlet the challenger board runs `replicates` times (default **2** — so the default configuration doubles challenger board runs) while the champion stays ONE frozen cached aggregate, making the noise reduction one-sided; a warning says so and names `--mode full`. `full` = bypass the cache and re-run every unit on both sides. Use `full` for the mock smoke, for independent draws on both sides, and when you don't trust the cache; `fast` for cheaper real runs (and it is what makes a multi-challenger field affordable). |
| `--max-wall-clock-seconds INTEGER` | Total wall-clock budget for the whole invocation. The loop stops cleanly between rounds once spent; a single round that would overrun is cancelled and recorded as aborted. Unset = unbounded. Stacks on top of each board entry's own `wall_clock_budget_seconds`. |
| `--parallelism INTEGER` | Board units in flight at once. Shadows `runtime.parallelism`; wins over the workspace `config.json` value (default 4). Per-*process* only — two concurrent runs on one box admit `2 × parallelism` between them, which is what the host-wide `runtime.host_worker_permits` bounds (`None` = AUTO `max(4, 2 × cpu_count)`, `0` = off; a run whose permits are held queues rather than over-subscribing). |
| `--aux-call-timeout FLOAT` | Per-call budget (seconds) for evaluator-side text consumers (judge, emulator, analysis, and text proposers). Shadows `aux.call_timeout_s` (default 120). |
| `--supervisor-binary PATH` | Path to the zicato-supervisor watchdog binary. Shadows `integration.supervisor_binary`. |
| `--harmonograf-url TEXT` | External harmonograf server URL (opt out of auto-launch). Shadows `integration.harmonograf_url`; wins over the `config.json` `harmonograf_url` key. |
| `--max-consecutive-rejections INTEGER` | Stop early after this many rounds rejected in a row (default 3). |
| `--tournament-structure gauntlet\|racing` + `--tournament-param KEY=VALUE` | **Contract-mutating convenience.** The experimental `single_elim` / `double_elim` / `swiss` are selected in `scoring.json` alongside `experimental.tournament_structures = true`, which admits them. Writes `{structure, params}` into the live `scoring.json` *before* the contract hash is computed, so it participates in the hash and auto-rolls the epoch exactly as a hand-edit would. `--tournament-param` (repeatable, JSON-parsed when possible) applies only alongside `--tournament-structure`. Unset = whatever `scoring.json` says (gauntlet when absent). See `skills/zicato-design-tournament-structure`. |
| `--epoch TEXT` | Pin an explicit epoch, skipping the auto-epoch check entirely. |
| `--no-auto-epoch` | Strict mode: error out on a drifted contract instead of rolling a fresh epoch. |
| `--epoch-name TEXT` | Name for an auto-created epoch (default: the `e{N}` scheme). |
| `--dashboard-port INTEGER` | Dashboard HTTP port, bound on 127.0.0.1 (default 7892). |
| `--no-dashboard` | Skip the dashboard + watchdog supervisor. Use for the mock smoke / CI; do **not** use for an operator-facing run. |

For an adapter that declares the `goldfive` integration, add `"goldfive": {}`
to `scoring.json` to select fixed Goldfive defaults. The built-in Google ADK
adapter declares it. Custom detector, steering, endpoint, and inner-agent
settings belong in that nested block rather than process flags. For example,
the wrapped agent's per-call timeout is `goldfive.agent.call_timeout_ms`.
Adapters that do not declare the integration omit the block. See
[`docs/design/GOLDFIVE-CONFIG.md`](../../docs/design/GOLDFIVE-CONFIG.md).

There is **no `--dashboard-bind` flag** — the dashboard always binds 127.0.0.1;
only the port is configurable. Viewing from another host is the operator's
concern (an SSH tunnel, for instance) rather than a zicato flag.

## Choosing the knobs

- **Rounds:** start small (`--rounds 2-4`) on a real run; each round is a full
  tournament across the board.
- **Mode:** `fast` is the default and the right call for real budget — it reuses
  every board unit already evaluated under this contract (the carried champion,
  and any shared challenger pairing) instead of re-running it. Budget for the
  challenger side being replicated `replicates` times (2 by default) even so.
  Use `full` when you want every unit freshly scored (the mock smoke, or when
  you don't trust the cache).
- **Budget:** on any real run set `--max-wall-clock-seconds` as a hard ceiling so
  a runaway loop cannot burn unbounded budget. `--max-consecutive-rejections`
  gives a quality-based early stop when the proposer stalls.
- **Epochs:** leave auto-epoching on (default). Editing `board.jsonl` /
  `brief.md` / `scoring.json` between invocations is detected as contract drift
  and rolls a fresh epoch automatically. So is registering a proposer dir
  (`register --proposer-path`) or editing one of its skills — see
  `skills/zicato-design-proposer`. Use `--no-auto-epoch` only when you want a
  drifted contract to be a hard error.

## The epoch-open pre-flight (it spends budget at round 0)

At evolve start — once per epoch, idempotent, persisted — the loop measures the
contract's A/A noise floor and its *achievable* signal (the champion vs
degraded ephemeral copies of itself, sampled round-robin across the
mutation points' declared `role` metadata), then asserts the margin window
`noise < promote_margin < achievable`. Budget for roughly six extra champion
board evaluations before round 0 does anything (5 A/A draws plus the probes); it
is skipped on an infra abort and a resume re-reads the persisted record.

The knobs are all `runtime.*` (tuning, never contract — flipping them does not
roll the epoch):

- `runtime.preflight_gate` — `"warn"` (default) warns loudly and **always lets
  the run proceed**; `"refuse"` hard-stops before rounds burn budget on a
  contract that cannot be optimized (recorded as `preflight_refused`); `"off"`
  runs no measurement.
- `runtime.preflight_probe_points` — ceiling on how many points the automatic
  sample degrades (default 5; probing stops early once the verdict settles).
- `runtime.preflight_probe_mutation_ids` — pin the points to probe by id
  instead of sampling, when you know which point carries the signal.

`zicato board preflight` is the manual surface for the same measurement.

## Reading the result

`evolve` prints a JSON array, one object per round, each with
`parent_generation_id`, `proposed_generation_id`, `parent_scalar`,
`child_scalar`, `delta_scalar`, `tournament_decision`
(`promoted`/`rejected`), and `rejection_reason`. A promotion needs the child's
loss to drop by at least `promote_margin` (from `scoring.json`) AND satisfy
pass-rate monotonicity — see [docs/design/SCORING.md](../../docs/design/SCORING.md).

After (or during) a run, check loop health to confirm the loop has real
optimization signal — a flat/toothless evaluation is worse than no run:

```sh
.venv/bin/zicato health --workspace .zicato      # exits non-zero on a critical finding
```

Every invocation also writes a structured log stream to
`.zicato/logs/<stamp>-<pid>.jsonl` (the same stream the dashboard's log pane
reads). Tail it live or after the fact — this is where the fast-mode replicate
warning and the pre-flight verdict land:

```sh
.venv/bin/zicato inspect logs --workspace .zicato --follow            # latest invocation
.venv/bin/zicato inspect logs --workspace .zicato --list              # pick an older one
```

To re-serve the dashboard for a finished or in-flight workspace (post-mortem):

```sh
.venv/bin/zicato dashboard --workspace .zicato   # foreground; Ctrl-C to stop
```

## Before promoting changes to the live loop

Use `skills/zicato-mutation-audit` to confirm exactly what the proposer is
allowed to rewrite (and what's `[forbidden]`) before you trust a promotion.

## Reference

- [docs/design/CLI.md](../../docs/design/CLI.md) — full CLI reference.
- [docs/design/SCORING.md](../../docs/design/SCORING.md) — the promotion gate.
- [docs/design/TOURNAMENT.md](../../docs/design/TOURNAMENT.md) — champion-vs-challenger model.
- [docs/design/DASHBOARD.md](../../docs/design/DASHBOARD.md) — the live UI.
- [docs/design/PROPOSER.md](../../docs/design/PROPOSER.md) + `skills/zicato-design-proposer` — configuring the proposer (skills / custom ADK agent), a contract input that rolls the epoch.
- [docs/design/EPOCHS-AND-JOURNALING.md](../../docs/design/EPOCHS-AND-JOURNALING.md) — epoch lifecycle.
- [docs/design/LOOP-HEALTH.md](../../docs/design/LOOP-HEALTH.md) — `zicato health` detectors.
