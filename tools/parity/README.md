# Parity harness — the behavior-preserving refactor oracle

`tools/parity.sh` is the gate a behavior-preserving reimplementation runs
against. It pins the *observable behavior* of the feature-complete
integration base to committed golden baselines, so a refactor is validated
as **isomorphic to that base** by keeping every gate GREEN.

```
bash tools/parity.sh            # run every gate, print PASS/FAIL + verdict
bash tools/parity.sh --update   # (re)capture every golden baseline
bash tools/parity.sh --only PYTEST
bash tools/parity.sh --skip PYTEST
```

The script prints the selected gates before execution. Exit code is zero only
when a nonempty selection completes successfully. Unknown names, missing option
operands, and exclusions that remove every gate fail before any checker runs.
Comma-separated and repeated selection options are supported.

## Gates

| Gate          | What it pins | How |
|---------------|--------------|-----|
| **PYTEST**        | Both required Python test tiers. | `uv run pytest -q -m "not node and not cascade_oc"` must pass. |
| **CONTRACT-HASH** | The epoch contract hash (+ per-component hashes) for a fixed fixture contract. An unchanged contract must never re-hash. | `compute_contract_hash` over the `target_1_presentation` example, diffed against `golden/contract_hash.json`. |
| **CLI-HELP**      | The CLI surface: every command/subcommand `--help`. | Rendered in-process at 80 cols, diffed against `golden/cli_help.txt`. |
| **REINDEX-DUMP**  | The SQLite analytical index — a pure projection of the workspace files. | Rebuild the index from a fixture workspace, `iterdump` to text, diff against `golden/reindex_dump.sql`. |
| **MOCK-GOLDEN**   | A full deterministic, no-live-LLM racing evolve end to end. | Reuses the `test_example_target_1_racing` mocks; captures `gen_score.json` / `experiment.json` / `loss.json` / `lineage.json`, diffs against `golden/mock_evolve_racing.json`. |
| **MYPY**          | Successful type checking. | `uv run mypy src/zicato/` must exit with status zero. |

The mock captures have separate gates for racing and gauntlet in full and fast
mode, two consecutive racing rounds, Swiss, and single and double elimination.
The gate table in `tools/parity.sh` owns their names and execution order.

Every checker failure includes its exit status in the report. Type checking has
no golden to update: `--update` still requires successful checker completion.
`tests/test_parity_runner.py` exercises selection and process failures with a
substitute command runner, so its tests do not repeat the verification suites.

## In CI

The `parity` job in `.github/workflows/ci.yml` runs `bash tools/parity.sh
--skip PYTEST` on Python 3.12 for every push to `main` and every pull
request. The default tests run in `lint-and-test`; the statistical and end-to-end
tests run in `.github/workflows/slow-tier.yml`. Both test results are required.

The gates are verified green across Python 3.11 and 3.12, `TZ` far from
UTC, `LC_ALL=C`, a relocated `TMPDIR`, a non-tty stdout, and a checkout at
a different absolute path. Keep it that way: a gate that flakes is worse
than no gate.

## The fixture workspace

REINDEX-DUMP and MOCK-GOLDEN share one deterministic source: the racing
mock evolve driven by `lib/mock_evolve_capture.py`. It runs the *real*
example contract (board + `scoring.racing.json` + annotated `agent/` tree +
the example's `mocks.aux_llm` proposer) through `evolve_once` under the
racing structure with the system under test + loss reducer mocked — exactly the
fidelity `tests/test_example_target_1_racing.py` runs at. No live LLM, no
network, no committed binary fixture to drift.

## Normalization

A few fields are non-deterministic by construction and carry no behavioral
meaning — they are masked to fixed sentinels before diffing (`lib/normalize.py`):

- ISO-8601 wall-clock timestamps → `<TS>`
- the date prefix of an epoch id (`2026-06-10_t1_racing`) → `<DATE>_t1_racing`
- random uuid patch ids (`uuid4().hex`) → `<HEX32>`
- the per-run tmp workspace root (absolute path) → `<TMP>`

REINDEX-DUMP normalizes one more thing (`lib/test_reindex_golden.py`): the
*spelling* of REAL literals. SQLite's REAL-to-text formatter changed in
3.41, so one stored double prints as `-3.999999999999999111e-01` against an
older library and `-0.39999999999999991` against a newer one. Pinning
either spelling makes the golden hostage to whichever SQLite captured it,
so REALs are re-spelled through Python's shortest round-trip `repr`.
Quoted strings and integers are left alone — the first is payload, and the
second keeps an INTEGER that starts rendering as a float moving the gate.

Everything else — every scalar, loss, component, decision, structural id,
and serialization detail — is compared verbatim.

## Proving the oracle has teeth

The harness was validated by injecting two deliberate behavior changes and
confirming RED, then reverting:

1. A sign flip in the scalar **pass** component → caught by **PYTEST**
   (11 failures). The deterministic fixture's board is all-passing
   (`mean_score = 1.0`), so the pass component is `0` there and the golden
   gates do not see it — which is exactly why PYTEST is the backbone.
2. A 1.0001× perturbation of the scalar **drift** component (the fixture's
   `drift_loss_mean = 0.4`, so this *does* move the score) → caught
   directly by **MOCK-GOLDEN** and **REINDEX-DUMP**, independent of pytest.

Together these show the golden gates have independent teeth on the surface
the fixture exercises, and PYTEST backstops everything the fixture does not.
The committed harness sits on clean code with all gates GREEN.

## Files

```
tools/parity.sh                         # the runner
tools/parity/lib/normalize.py           # shared field masking
tools/parity/lib/contract_hash.py       # CONTRACT-HASH gate
tools/parity/lib/cli_help.py            # CLI-HELP gate
tools/parity/lib/mock_evolve_capture.py # deterministic mock-evolve engine
tools/parity/lib/test_mock_golden.py    # MOCK-GOLDEN gate (pytest-driven)
tools/parity/lib/test_reindex_golden.py # REINDEX-DUMP gate (pytest-driven)
tools/parity/golden/                     # committed baselines
```
