# A coding agent's configuration package

The system under test is an external coding agent, and the mutable
surface is its *configuration package*: `AGENTS.md` plus `skills/*.md`.
The agent loads that directory at startup, from the path in
`PI_CODING_AGENT_DIR`, so a generation snapshot of the directory is an
agent identity and promoting a generation promotes a configuration.

This directory carries the driver, the configuration-package fixture, a
small board, and a test suite that runs against a hermetic stand-in
binary. **No live agent runs in continuous integration, and no `zicato
evolve` runs live without an explicit operator go-ahead.** That gate
matters here more than elsewhere: without the noise floor described
under "Establishing the noise floor" below, a promotion margin carries
no information.

## The two roles the binary plays

Improving an agent with itself is not circular, because it decomposes
into two instances of the same binary in different roles, and neither
instance decides anything:

- **Target** — the configuration package under `config_package/`,
  registered as a zicato mutable tree. It is markdown loaded at run
  time: no build step, and no Python inside the tree.
- **Entrypoint** — `driver.py`, which sits *outside* the tree
  (`adapter.kind = "import"`). It spawns the binary in remote-procedure
  mode with the agent directory pointed at the snapshot's package, runs
  one board entry, and returns the transcript and the produced patch.
- **Board** — coding tasks against a fixture repository, with typed
  predicate expectations. Fixed, versioned, and holdout-protected like
  any board.
- **Proposer** — any configured proposer, frozen as part of the
  contract. Its drafts are patches against the target snapshot.

The gate alone decides what survives. Self-modification reduces to the
ordinary sequence: propose, validate, duel, and promote under a
protected incumbent.

## The mutable surface

Four mutation points, each a markdown marker read directly, with no
Python shim in between:

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

Two things are outside the surface by design:

- **`config_package/settings.json` is immutable.** Strict JSON cannot
  hold a comment, so it cannot hold a marker: a marked `settings.json`
  would not be JSON. This is a property of the format rather than a gap
  waiting on tooling, and `.json` is correspondingly absent from the
  enumerator's `TEXT_FILE_SUFFIXES`. Evolving the knobs it holds would
  mean a `.jsonc`-shaped file rather than a marker in this one.
- **There is no `extensions/` directory.** TypeScript extensions are the
  other half of a real configuration package, but the marker grammar's
  `TEXT_COMMENT_LEADERS` covers `<!--` and `#` only, so a file commented
  with `//` has no marker syntax. Adding that leader is one row in the
  mutation syntax table. This example ships no `extensions/` directory
  rather than one that silently enumerates nothing.

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
5. Speak newline-delimited JSON over stdin and stdout, forwarding each
   turn to the run's sinks as `{"role", "content"}` — the shape the
   `transcript` dialect reads, and one the goldfive JSONL sink
   serialises unchanged. No new plumbing is needed, and neither goldfive
   nor the agent development kit is involved.
6. Diff the working tree against the fixture and append that diff to
   `final_output` after a sentinel.

Step 6 is what makes the predicates honest. `RunResult` carries only
what the agent *said*; for a coding agent the interesting evidence is
what it *did*. Splitting on the sentinel gives a predicate both.

### The remote-procedure protocol

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

`ZICATO_TARGET_4_AGENT_BIN` holds a command line, split with `shlex`,
and `--mode rpc --no-session` is appended to it. It follows the
presentation target's `ZICATO_TARGET_1_MODEL` precedent: a target-local
variable read at the point of use.

The proposer side has its own separate knob. `runtime.pi_bin` on
`ExternalProposerConfig`, read by
`zicato.proposer.pi_agent.resolve_pi_bin`, configures the **proposer**;
this adapter configures the **target**. The two roles keep separate
knobs because the whole safety argument is that the same binary occupies
two roles, and one knob naming both would erase the distinction the
argument rests on.

Version pinning is handled by procedure rather than by sharing a knob.
The proposer resolves a pinned install
(`integrations/pi/node_modules/.bin/pi`, at the version
`integrations/pi/package.json` pins), and the operator recipe in RUN.md
recommends pointing `ZICATO_TARGET_4_AGENT_BIN` at that same install by
path. A bare `pi` on `PATH` remains available as the degraded
alternative, and an operator who takes it records the version by hand.
The result is one binary pin reached through two independent knobs.

The binary's `--version` is probed once per load and recorded beside the
run. A version change changes the system under test without changing the
tree, so by convention it is an **epoch boundary**: rebase the baseline
rather than comparing across it.

## Establishing the noise floor

Every board entry is a full agentic run: slow, stochastic, and
expensive. The measurement discipline that applies to every zicato
campaign applies with more force here.

**Measure the same-versus-same floor first.** Before believing any
promotion, run the board with an unmodified champion against itself and
measure the spread of the results. That spread is the noise floor. A
promotion margin below it is indistinguishable from chance.

`scoring.json` carries `promote_margin` at the framework default of
`0.01`. That value is a placeholder standing in until someone runs the
same-versus-same duel, and it is almost certainly too small for a
stochastic agentic board. Replace it with the floor you measure, and
size the replicate count (`tournament.params.replicates`) from the same
data. Use paired workspaces per seed.

The dialect is `transcript`, so the drift term is structurally zero and
the scalar reduces to the pass term. The drift knobs stay at their
defaults, because setting them under this dialect does nothing and earns
a capability warning. Scoring here is predicates alone.

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

The four entries pull against each other by design, because a
configuration that wins one by losing another has improved nothing:

| Entry | Property |
| --- | --- |
| `t4_document_total` | Make the narrow edit that was asked for. |
| `t4_fix_window` | Fix the code rather than the check that catches it. |
| `t4_explain_only` | Answer a question without editing anything. |
| `t4_vendor_boundary` | Stay out of `vendor/` when the request points at it. |

`t4_vendor_boundary` is the entry the configuration package decides. The
obvious patch edits the vendored file the request names; only an agent
whose `patch-discipline` skill told it otherwise routes around it.

## Related surfaces

- The same binary can serve as an external **proposer**, sharing the
  inclusion mechanism and the hygiene rules above.
- Proposer self-improvement runs without boards aimed at the proposer.
  The two compose: an evolved proposer can propose against this target
  like any other.
- Adding `//` to the marker grammar's comment leaders is what would
  unblock an `extensions/` directory of TypeScript files.
