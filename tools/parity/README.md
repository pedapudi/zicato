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

Exit code is 0 only when every selected gate passes.

## Gates

| Gate          | What it pins | How |
|---------------|--------------|-----|
| **PYTEST**        | Full behavioral characterization (2800+ tests). | `uv run pytest -q` must pass. The primary safety net. |
| **CONTRACT-HASH** | The epoch contract hash (+ per-component hashes) for a fixed fixture contract. An unchanged contract must never re-hash. | `compute_contract_hash` over the `target_1_presentation` example, diffed against `golden/contract_hash.json`. |
| **CLI-HELP**      | The CLI surface: every command/subcommand `--help`. | Rendered in-process at 80 cols, diffed against `golden/cli_help.txt`. |
| **REINDEX-DUMP**  | The SQLite analytical index — a pure projection of the workspace files. | Rebuild the index from a fixture workspace, `iterdump` to text, diff against `golden/reindex_dump.sql`. |
| **MOCK-GOLDEN**   | A full deterministic, no-live-LLM racing evolve end to end. | Reuses the `test_example_target_1_racing` mocks; captures `gen_score.json` / `experiment.json` / `loss.json` / `lineage.json`, diffs against `golden/mock_evolve_racing.json`. |
| **MYPY**          | Type-checker error count. | `uv run mypy src/zicato/`; gate is "not worse than `golden/mypy_baseline.txt`". |

## In CI

The `parity` job in `.github/workflows/ci.yml` runs `bash tools/parity.sh
--skip PYTEST` on Python 3.12 for every push to `main` and every pull
request. PYTEST is skipped only because the `lint-and-test` job already
runs that exact suite; every other gate runs there, so a golden-covered
surface can no longer move without a red check.

The gates are environment-independent by construction — verified green
across Python 3.11 and 3.12, `TZ=Pacific/Kiritimati`, `LC_ALL=C`, a
relocated `TMPDIR`, a non-tty stdout, and a checkout at a different
absolute path. The one wall-clock dependency that used to exist (the epoch
id's date prefix seeds the holdout rotation, which selects the racing
rung's board slice) is pinned in `lib/mock_evolve_capture.py` by freezing
`_today`.

## The fixture workspace

REINDEX-DUMP and MOCK-GOLDEN share one deterministic source: the racing
mock evolve driven by `lib/mock_evolve_capture.py`. It runs the *real*
example contract (board + `scoring.racing.json` + annotated `agent/` tree +
the example's `mocks.aux_llm` proposer) through `evolve_once` under the
racing structure with the inner harness + loss reducer mocked — exactly the
fidelity `tests/test_example_target_1_racing.py` runs at. No live LLM, no
network, no committed binary fixture to drift.

## Normalization

A few fields are non-deterministic by construction and carry no behavioral
meaning — they are masked to fixed sentinels before diffing (`lib/normalize.py`):

- ISO-8601 wall-clock timestamps → `<TS>`
- the date prefix of an epoch id (`2026-06-10_t1_racing`) → `<DATE>_t1_racing`
- random uuid patch ids (`uuid4().hex`) → `<HEX32>`
- the per-run tmp workspace root (absolute path) → `<TMP>`

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
