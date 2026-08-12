# Target 4 — A Coding Agent's Configuration Package

This example is the **skeleton** for zicato's fourth dogfood target
(issue #170): the system under test is an external coding agent, and the
mutable surface is its *configuration package* — `AGENTS.md` plus
`skills/*.md`. The agent loads that directory at startup (the path comes
from `PI_CODING_AGENT_DIR`), so a generation snapshot of it literally is
an agent identity, and promoting a generation promotes a configuration.

Skeleton means: the driver, the config-package fixture, a smoke board,
and a test suite that runs against a hermetic stand-in binary. **No live
agent runs in CI, and no `zicato evolve` runs live without an explicit
operator go-ahead.** See "Before you believe a number" below — that gate
is not ceremony here, it is the difference between a measurement and a
coin flip.

## The two-instance role split

"An agent improves itself" sounds circular. It is not, because it
decomposes into two instances of the same binary in different roles, and
neither one decides anything:

- **Target** — the configuration package under `config_package/`,
  registered as a zicato mutable tree. Runtime-loaded markdown; no build
  step, no Python inside the tree.
- **Entrypoint** — `driver.py`, deliberately *outside* the tree
  (`adapter.kind = "import"`). It spawns the binary in rpc mode with the
  agent directory pointed at the snapshot's package, runs one board
  entry, and returns the transcript and the produced patch.
- **Board** — coding tasks against a fixture repository, with typed
  predicate expectations. Fixed, versioned, holdout-protected like any
  board.
- **Proposer** — any configured proposer, *frozen as contract*. Its
  drafts are patches against the target snapshot.

The gate decides what survives — not either instance. Self-modification
becomes: propose, validate, duel, promote under a protected incumbent.

## The mutable surface

Four mutation points, all native markdown markers (`#154`; no Python
shim in between):

| Mutation id | File | Kind |
| --- | --- | --- |
| `agents_operating_rules` | `config_package/AGENTS.md` | `code` |
| `agents_tool_policy` | `config_package/AGENTS.md` | `code` |
| `skill_repo_navigation` | `config_package/skills/repo-navigation.md` | `file` |
| `skill_patch_discipline_rules` | `config_package/skills/patch-discipline.md` | `code` |

`code` regions are the workhorse form for a non-Python file: the region
has an explicit opening marker AND an explicit `zicato:mutable:end`
sentinel, both owned by the operator and both *outside* the mutable
range, so a patch is bounded by construction rather than by a resolution
heuristic.

Two things are deliberately **not** surface:

- **`config_package/settings.json` is immutable.** Strict JSON cannot
  host a comment, so it cannot host a marker — a marked `settings.json`
  would not be JSON. This is not a gap waiting on tooling; it is a
  property of the format. `.json` is correspondingly absent from the
  enumerator's `TEXT_FILE_SUFFIXES`. If the knobs in it ever need to
  evolve, the move is a `.jsonc`-shaped file, not a marker in this one.
- **`extensions/*.ts` are not built yet.** TypeScript extensions are the
  other half of a real configuration package, and they are the natural
  follow-on — but the marker grammar's `TEXT_COMMENT_LEADERS` covers
  `<!--` and `#` only, so a `//`-commented file has no marker syntax
  today. That is one entry in the syntax table (issue #168). This
  example ships no `extensions/` directory rather than shipping one that
  silently enumerates nothing.

## How one run works

The driver is a `HarnessAdapter`; `load(generation_root)` binds a
session to the snapshot, and each `run` does:

1. Resolve `<generation_root>/config_package`.
2. Copy it to a **fresh agent directory** under the run scratch dir. The
   agent may write state; the snapshot stays read-only and two
   concurrent units cannot see each other.
3. Copy the entry's fixture repository to a fresh working tree.
4. Spawn the binary with a **minimal offline environment** — an
   allowlist (`PATH`, `HOME`, `TMPDIR`, …) plus explicit additions
   through the `ZICATO_TARGET_4_AGENT_ENV_*` prefix. API keys and proxy
   settings in the caller's environment are dropped, so a run cannot
   reach the network by inheriting someone's shell.
5. Speak newline-delimited JSON over stdin/stdout, forwarding each turn
   to the run's sinks as `{"role", "content"}` — exactly what the
   `transcript` dialect reads, and a shape the goldfive JSONL sink
   serialises unchanged. No new plumbing, no goldfive, no ADK.
6. Diff the working tree against the fixture and append that diff to
   `final_output` after a sentinel.

Step 6 is what makes the predicates honest. `RunResult` carries only
what the agent *said*; for a coding agent the interesting evidence is
what it *did*. Splitting on the sentinel gives a predicate both.

### The rpc protocol

The driver writes one request line and reads events until a terminal
one:

```json
{"op": "run", "input": "…", "cwd": "/abs/path"}
{"type": "turn", "role": "assistant", "content": "…"}
{"type": "result", "final_output": "…"}
```

Unparseable lines and unknown `type` values are ignored, so a binary
that interleaves diagnostics on stdout still drives cleanly. This is the
zicato-side shape; a binary speaking a different wire needs a shim in
`driver.py` and no change anywhere else in zicato.

### Naming the binary

`ZICATO_TARGET_4_AGENT_BIN` is a command line (shlex-split), following
target 1's `ZICATO_TARGET_1_MODEL` precedent of a target-local variable
read at the point of use. `--mode rpc --no-session` is appended.

A `runtime.pi_bin` knob does now exist, added by #173 — but it belongs
to the *other* surface. `zicato.proposer.pi_agent.resolve_pi_bin` reads
it off `ExternalProposerConfig`, which configures the **proposer**; this
adapter configures the **target**. #170 keeps those two roles apart on
purpose, because "the same binary in two roles" is the entire safety
argument, and a single knob naming both would erase the distinction it
rests on. So the env var stays until a target-side knob exists.

**Open question worth deciding before a live round.** The proposer now
resolves a *pinned* install (`integrations/pi/node_modules/.bin/pi`, at
the version `integrations/pi/package.json` pins), while this target
defaults to bare `pi` on `PATH`. Pointing the target at the same pinned
install would make the version-pinning discipline automatic rather than
a thing the operator must remember. It would also couple the target's
identity to the proposer's tooling, which is exactly the coupling the
role split avoids. Deliberately not decided here.

The binary's `--version` is probed once per load and recorded beside the
run. A version bump changes the system under test without changing the
tree, so by convention it is an **epoch boundary** — rebase the
baseline, do not compare across it.

## Before you believe a number

Every board entry is a full agentic run: slow, stochastic, and
expensive. The campaign discipline applies *more* here than anywhere
else in zicato, not less.

**The A/A floor comes first.** Before any promotion is believed, run the
board against an unmodified champion versus itself and measure the
spread. That number is the noise floor. A promotion margin smaller than
it is not evidence of anything.

`scoring.json` ships `promote_margin` at the framework default of
`0.01`. That is a placeholder, not a measurement — it is what the file
has to say before anyone has run the A/A duel, and it is almost
certainly too small for a stochastic agentic board. Replace it from the
floor you measure, and size replicates (`tournament.params.replicates`)
from the same data. Paired workspaces per seed.

The dialect is `transcript`, so the drift term is structurally zero and
the scalar reduces to the pass term. The drift knobs are left at their
defaults on purpose: setting them under this dialect is inert and earns
a capability warning. Scoring here is predicates, and predicates only.

## Files

- `config_package/` — **the mutable tree**: `AGENTS.md`, `skills/*.md`,
  and the immutable `settings.json`.
- `driver.py` — the adapter, the rpc client, the offline environment,
  the tree diff.
- `stub_agent.py` — the hermetic stand-in binary. No model.
- `fixtures/toolbox/` — the working tree: `ops.py` (holds a planted
  off-by-one), `vendor/legacy_format.py` (must not be edited),
  `checks/verify_ops.py` (must not be edited to pass).
- `board.jsonl` — four `single_turn` entries: narrow edit, bugfix,
  restraint, and the vendor boundary.
- `predicates.py` — operator-owned pass/fail over the spoken output and
  the produced patch.
- `brief.md` / `scoring.json` — the frozen contract.
- `RUN.md` — the recipe.

## What the board measures

The four entries pull against each other on purpose, because a
configuration that wins one by losing another is not an improvement:

| Entry | Property |
| --- | --- |
| `t4_document_total` | Make the narrow edit that was asked for. |
| `t4_fix_window` | Fix the code, not the check that catches it. |
| `t4_explain_only` | Answer a question without editing anything. |
| `t4_vendor_boundary` | Stay out of `vendor/` when the request points at it. |

`t4_vendor_boundary` is the entry the configuration package actually
decides: the naive patch edits the vendored file the request names, and
only an agent whose `patch-discipline` skill told it otherwise routes
around it.

## Related

- **#147** — the same binary as an external *proposer*; shares the
  inclusion mechanism and this hygiene set.
- **#169** — proposer self-improvement without boards aimed at the
  proposer. The two compose: an evolved proposer can propose against
  this target like any other.
- **#168** — the `.ts` syntax-table entry that unblocks `extensions/`.
