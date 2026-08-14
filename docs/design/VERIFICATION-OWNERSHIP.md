# Verification ownership

The development guide is the authority for implementation verification. Stable
contracts and rationale remain in `docs/design/`; command sequences, fixture
recipes, regression mechanics, and current module paths belong in
`docs/dev-guide/11-testing.md`.

## Non-negotiable coverage

- The convergence and decision-procedure oracles retain their pinned values.
- Incident-derived regressions remain observable-behavior tests and must fail
  when their fixes are removed.
- Consolidation may share setup and payload construction, but never removes or
  weakens an assertion.
- Browser and terminal fixtures originate from the same query-model spellings.
- Golden changes identify the intentional interface change that caused them.
- Browser test files run in isolated module graphs. Render caches model one
  page; sharing them across fixture files creates order-dependent evidence.

## Clean teardown

Successful exit is insufficient if a service leaves tasks, coroutines, sockets,
threads, or child processes behind. Focused lifecycle tests treat warnings as
errors, and the normal parallel suite must remain clean. Cleanup mechanics and
the serial verification recipe live in the development guide.

## Completion evidence

The simplification program records the complete verification ladder and the
machine-produced line-budget report. Test reduction is acceptable only when
the remaining test demonstrates the same behavior and assertions; lower line
count alone is not evidence of redundancy.
