# Line budgets

The simplification program has two measurable constraints: the tracked total
must shrink, and production runtime must shrink independently. The second gate
prevents a reduction achieved only by deleting tests. These counts are a proxy
for structural simplicity, not a substitute for design review.

## Measurement contract

Run the report locally:

```sh
python tools/line_budget.py
python tools/line_budget.py --check
python tools/line_budget.py --ref f9052dd
```

The checker counts newline characters, matching `wc -l`, in paths returned by
`git ls-files` (or `git ls-tree` for `--ref`). It excludes:

- Markdown (`*.md`, `*.markdown`);
- dependency lockfiles named in `tools/line_budget.py`;
- the explicit generated-artifact paths in that file.

The production subtotal includes runtime code under `src/zicato/`, crate
`src/` directories, integrations, and the build hook. Tests, test fixtures,
and binary or vector assets are not production runtime. The full total still
counts tests and fixtures unless they are explicitly generated artifacts.
The report groups lines by language and subsystem so movement remains visible.

At `f9052dd`, the original raw non-Markdown measurement was 425,755 lines in
975 files. After excluding lockfiles and generated artifacts, the enforceable
baseline is 408,547 total lines; production runtime is 197,588 lines. The
machine-readable values live in `.line-budget.json`.

## Gate and ratchet policy

CI reports and checks both subtotals on every change. During the simplification
program, a child issue may temporarily exceed a ceiling only through
`temporary_overage` in `.line-budget.json`. The entry must name the issue,
explain why staging is temporarily line-positive, and state separate total and
production allowances. Review considers that configuration part of the change.

At program completion, remove the temporary allowance and ratchet both limits
to the final measured totals, which must be below their baselines. Thereafter,
limits only move deliberately. A limit increase must update the adjacent note
with the previous limit, signed delta, new limit, issue, and reason; reductions
ratchet directly to the new lower totals. For example:

```text
production 180000 + 120 = 180120; #321 adds the required parser
```

Minification, concatenation, moving implementation into excluded paths,
checked-in generated replacements, or weakening load-bearing tests cannot
justify a budget change. The metric is intentionally plain; reviewers should
inspect any classification change as closely as an increase.

## Program completion record

The final verification for the umbrella issue must attach the JSON reports for
`f9052dd` and the closing commit, confirm both totals decreased, clear all
temporary allowance, and set the post-program limits to the closing totals.
