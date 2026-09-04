# zicato-examples

Worked targets for the [zicato](../README.md) self-improving harness —
each one a complete agent tree that `zicato evolve` can be pointed at.
This directory is its own installable distribution (`zicato-examples`);
the importable package is `zicato_examples`.

The examples are **not** shipped inside the `zicato` wheel. They are
installed into the development environment by `make install` (as a uv
workspace member) so the `tests/test_example_*.py` smoke tests and the
end-to-end walkthroughs can import them as `zicato_examples.*`.

## Targets

Four targets, in ascending order of how much of the loop they exercise
against real infrastructure. The directory names carry index numbers
that mean nothing beyond ordering, and there is no third directory.

- **The convergence demo** (`zicato_examples/target_0_convergence/`) —
  a planted-defect target with no language model anywhere: the harness
  is deterministic and the proposer is a script, so the champion scalar
  provably falls to a known floor. See
  [`target_0_convergence/RUN.md`](zicato_examples/target_0_convergence/RUN.md).
- **The presentation agent**
  (`zicato_examples/target_1_presentation/`) — a multi-agent
  presentation tree driven end to end by `zicato evolve` with
  deterministic mock models. See
  [`target_1_presentation/RUN.md`](zicato_examples/target_1_presentation/RUN.md).
- **The goldfive steering layer**
  (`zicato_examples/target_2_goldfive_steering/`) — drives the sibling
  project goldfive's own steering layer as the system under test. See
  [`target_2_goldfive_steering/RUN.md`](zicato_examples/target_2_goldfive_steering/RUN.md).
- **The coding-agent configuration**
  (`zicato_examples/target_4_agent_config/`) — an external coding
  agent's configuration package (`AGENTS.md` plus `skills/*.md`) as the
  mutable tree. The tests drive a hermetic stand-in binary; runs against
  the real agent are operator-initiated. See
  [`target_4_agent_config/README.md`](zicato_examples/target_4_agent_config/README.md).

## Installing

From a checkout, `make install` (which runs `uv sync --all-extras`)
installs this package alongside `zicato` as a workspace member — no
`PYTHONPATH` or symlink hacks required. Standalone:

```bash
uv pip install -e examples
```
