# The pi runtime for zicato's external proposer

The Node side of issue #147: the coding agent zicato launches when a
workspace configures

```toml
[runtime]
proposer_agent = "zicato.proposer.pi_agent:PiProposerAgent"
```

## Pinned, not vendored

`package.json` pins an **exact** version of
[pi](https://github.com/earendil-works/pi) — no caret, no range. The
**pin** lives in git; the **bytes** arrive from npm:

```sh
npm install     # needs Node >= 22.19
```

There is deliberately no `package-lock.json` here, and it is gitignored.
The published pi package ships its own `npm-shrinkwrap.json` pinning all
144 of its transitive dependencies, so an exact top-level version already
determines the whole tree: a lockfile of ours would only restate what the
publisher already guarantees. Do not add one.

That materializes `node_modules/.bin/pi`, which is what
`zicato.proposer.pi_agent` launches. `runtime.pi_bin` overrides the path
for dev clones and for operators who install pi elsewhere;
`runtime.pi_integration_dir` overrides where this directory is found.

The resolved version — read from the package.json beside whichever binary
is actually selected — is folded into the contract hash, so a pi upgrade
rolls the epoch. The standing rule is unchanged: do not upgrade pi
mid-tournament.

## What we author

Two TypeScript files, hashed by their bytes (they are edited in place, so
they have no version to record):

- **`propose-experiment.ts`** — the terminating structured-output tool.
  Its typebox schema mirrors `EXPERIMENT_JSON_SCHEMA` in
  `src/zicato/proposer/structured.py`, so a shape mismatch is repaired at
  the tool-call layer before Python sees it. The tool itself is inert:
  zicato reads the emitted arguments off the RPC `tool_execution_start`
  event and runs `parse_experiment_json` over them, which stays
  authoritative. `tests/test_proposer_pi_envelope.py` fails if a schema
  property loses its counterpart here.
- **`envelope-probe.ts`** — test-only. Reports `pi.getActiveTools()` at
  session start so the envelope assertion can check what the model can
  actually call. It is never loaded by a run, and it registers no tools of
  its own.

## Testing

The default suite covers the transport against a stub JSONL peer
(`tests/_pi_stub_peer.py`) and never needs Node. Tests marked `pi` launch
the real binary; they skip cleanly when it is not installed, and CI runs
them in the opt-in `pi-proposer` lane.
