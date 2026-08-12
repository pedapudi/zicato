# zicato-examples

Vendored dogfood targets for the [zicato](../README.md) self-improving
harness. This directory is its own installable distribution
(`zicato-examples`); the importable package is `zicato_examples`.

The examples are **not** shipped inside the `zicato` wheel. They are
installed into the dev environment by `make install` (a uv workspace
member) so the `tests/test_example_*.py` smoke tests and the
end-to-end walkthroughs can import them as `zicato_examples.*`.

## Targets

- `zicato_examples/target_1_presentation/` — a vendored multi-agent
  presentation tree driven end-to-end by `zicato evolve` with
  deterministic mock LLMs. See
  [`target_1_presentation/RUN.md`](zicato_examples/target_1_presentation/RUN.md).
- `zicato_examples/target_2_goldfive_steering/` — drives goldfive's
  own steering layer as the inner harness. See
  [`target_2_goldfive_steering/RUN.md`](zicato_examples/target_2_goldfive_steering/RUN.md).
- `zicato_examples/target_4_agent_config/` — an external coding agent's
  configuration package (`AGENTS.md` + `skills/*.md`) as the mutable
  tree. Skeleton: the tests drive a hermetic stand-in binary, and live
  runs are operator-initiated. See
  [`target_4_agent_config/README.md`](zicato_examples/target_4_agent_config/README.md).

## Installing

From a checkout, `make install` (which runs `uv sync --all-extras`)
installs this package alongside `zicato` as a workspace member — no
`PYTHONPATH` or symlink hacks required. Standalone:

```bash
uv pip install -e examples
```
