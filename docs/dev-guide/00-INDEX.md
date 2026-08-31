# The zicato Development Guide

> **Audience:** any coding agent — including a weaker model — that will change
> zicato's source. **Purpose:** let you extend the system *without reintroducing
> a bug the project has already fixed and without violating an invariant the
> project depends on.* Every claim in these chapters is grounded in the actual
> code on the branch that shipped them; every code excerpt is copied verbatim
> from the file it names.
>
> Use this as a reference book. Read §"How to use this guide" once, then jump to
> the chapter your task touches. Never skip the Golden Rules or the pre-commit
> ladder, both below: they are short, and each one exists because breaking it
> caused a real failure.

---

## How to use this guide

You almost never read this book front-to-back. Instead:

1. **Read the 10 Golden Rules below, every session.** They are the non-negotiable
   constraints. If a rule and your task conflict, the rule wins — stop and ask.
2. **Read `01-orientation.md` once** for the vocabulary and the repo map (which
   subpackage owns what, and what may never import what). You cannot ground a
   change you cannot name.
3. **Read `02-architecture.md`** to see where your change sits in one round of the
   loop (the two pipeline walkthroughs).
4. **Jump to the chapter that owns your surface** (the chapter map is below).
5. **Before you touch the orchestrator, the contract hash, the worker boundary,
   the evaluation statistics, the overfitting envelope, or the dashboard render
   path** — read the chapter's *Invariants you must not break* box AND the
   relevant case in `12-bug-casebook.md`. Every documented bug lived on one of
   those surfaces.
6. **Find your task in `13-recipes.md`.** Most changes are a named recipe with
   exact steps and a Verify block. If yours is not, the closest recipe is your
   template.
7. **Before proposing any commit, run the pre-commit ladder** (§below, full
   detail in `11-testing.md §11.11`). A change that has not passed it is not
   done.

> ⛔ **NEVER** treat this guide as authoritative *over the code*. If a symbol,
> path, or line the guide names is not in the current tree, the code is right and
> the guide is stale: fix the guide in the same PR (see `13-recipes.md` recipe 14
> for the docs-land discipline). Never invent a symbol to match the prose.

---

## The 10 Golden Rules (full text in `01-orientation.md §4`)

These are the rules that, if broken, either corrupt the repo's positioning, break
the build for everyone, or silently ship a statistically unsound change. Each is
expanded — with its verification command and the incident that motivated it — in
chapter 01.

| # | Rule | One-line | Verify |
|---|---|---|---|
| **G1** | The vendor rule | Nothing in git — code, comments, docstrings, tests, fixtures, commit messages, PR bodies, or trailers — references the model vendor. | `git log -p <base>..HEAD \| grep -icE "$pat"` (assemble `$pat` per `01-orientation.md §G1`; the check never spells the stems) → **0** |
| **G2** | `uv sync --all-extras`, always | A bare `uv sync` deletes the dev tooling (pytest/mypy/ruff/uv itself) from `.venv`. | `uv run pytest --version` |
| **G3** | No live model run without go-ahead | Live runs cost money and need explicit operator sign-off; the deterministic convergence example is the sanctioned end-to-end vehicle. | run `examples/zicato_examples/target_0_convergence/RUN.md` rather than a real model |
| **G4** | The two oracles are green before ANY commit | `test_convergence_known_answer` (the loop converges) + `test_decision_procedure_power` (the decision procedure's measured operating characteristics). | `uv run pytest tests/test_convergence_known_answer.py tests/test_decision_procedure_power.py -q` |
| **G5** | Parity + import contracts + node | `bash tools/parity.sh` (13 gates), `uv run lint-imports` (7 contracts), `make node-test`. | all green |
| **G6** | Omit-at-default contract discipline | A new default-off contract field MUST be registered in `_SCORING_OMIT_AT_DEFAULT_FIELDS`, or every existing workspace spuriously rolls its epoch. | `03-contract-and-epochs.md`; contract-hash parity gate |
| **G7** | The reserved replicate-base ledger | Duels `0..`, calibration `1000`, preflight `2000`, screening `3000/3001`, evidence `4000`, board reflection `5000`, eval-synthesis admission `6000`. Squatting a base corrupts the unit cache (bugs #1, #8); a reader that globs `loss*.json` instead of filtering by base reads the preflight's degraded probes as real behaviour (`unit_cache.is_own_code_board_draw`). | `04-evaluation-statistics.md §8` |
| **G8** | The restricted-visibility envelope | Nothing entry-identifying (entry ids, task text, holdout data, raw per-entry outcomes) may reach the proposer. Every channel is banded/aggregated/anonymized/redacted. | `05-proposer.md §"envelope"`; adversarial-identity fixtures |
| **G9** | Module-level callables only across the worker boundary | `_callable_dotted_path` rejects closures; scripted proposers/harnesses are module-level functions + module state + `reset()`. | `06-tournament-and-selection.md §"worker boundary"` |
| **G10** | Digest-gated rendering | A no-op SSE heartbeat must cause ZERO DOM rebuild; views fold content digests and swap only on change. | `09-dashboard-and-query.md §9.7`; node DOM-node-identity tests |

---

## Chapter map

| Ch | File | Covers | Read it when… |
|---|---|---|---|
| 01 | `01-orientation.md` | vocabulary, the repo map, the 10 Golden Rules, your-first-hour | first session, always |
| 02 | `02-architecture.md` | one round twice (gauntlet + multi-challenger), the data-type flow, the extracted-seam inventory | you need to see where a change sits |
| 03 | `03-contract-and-epochs.md` | the contract hash + every canonicalizer, omit-at-default, epoch lifecycle, **add-a-contract-knob** | you add/change any contract knob or epoch behavior |
| 04 | `04-evaluation-statistics.md` | the measurement chain, the gate, the **noise doctrine**, the reserved-base ledger, how to prove a statistical change | you touch scoring, the gate, replication, calibration, or the evidence gate |
| 05 | `05-proposer.md` | the three proposer paths, `ProposerContext`, best-of-N + screen + critique + align-tree, the **restricted-visibility envelope** | you change how candidates are generated or what the proposer sees |
| 06 | `06-tournament-and-selection.md` | `run_tournament`/`resolve_tournament`, the five structures, the **worker boundary**, the **unit cache** | you touch tournament execution, structures, the worker, or caching |
| 07 | `07-runtime-and-durability.md` | CQRS persistence, atomic writes, the git generation store, GC, crash-resume, the control protocol, RoundLog | you touch state files, storage, resume, or the round log |
| 08 | `08-supervisor.md` | the Rust watchdog/notary — heartbeat, reaping, the hash-chained ledger, diff-containment, the read-only index | you change the supervisor or a state file it reads |
| 09 | `09-dashboard-and-query.md` | `zicato/query` (lib) vs `zicato/dashboard` (driver), **server-authority**, **digest gating**, the add-a-panel recipe | you change a reader, an endpoint, or a view |
| 10 | `10-builder-cli-library.md` | the builder contract-IDE, the CLI + flag→pin, the library facade + import contracts | you change the builder, add a CLI flag, or extend the public API |
| 11 | `11-testing.md` | the suites, the two oracles, the parity gates, the import contracts, the **pre-commit ladder** | before every commit; when you add a test |
| 12 | `12-bug-casebook.md` | the twelve shipped bugs as teaching cases + the meta-lessons | before touching any of the six bug-prone surfaces |
| 13 | `13-recipes.md` | the cookbook — fourteen self-contained, copy-precise recipes | you are about to make a change (find yours first) |
| 14 | `14-goals-and-roadmap.md` | the north star, the proof state, the endpoint-gated runbook, the deferred register, anti-goals | you are proposing new work or a live run |

Total: roughly 24,000 lines across 14 chapters plus this index.

---

## Master invariant index

Each surface owns an invariant namespace. Every namespace is a table in its
owning chapter, and each row carries an id, a descriptive name, and the
invariant's statement. Prose cites the name; the id is the locator a document
outside this guide uses, and a cross-chapter citation qualifies it by chapter
(`06-T4`). Break one of these and something the project depends on fails; the
citing section states the failure mode.

| Namespace | Owner | Governs |
|---|---|---|
| **G1–G10** | `01-orientation.md §4` | the Golden Rules (above) |
| **numbered in place** | `03-contract-and-epochs.md` | the contract hash and epoch identity: hash-identifies-contract-not-checkout, omit-at-default, serializer-completeness, edit-the-body-rolls, runtime-never-hashed, absent-hash-is-`None`, refuse-on-newer, lineage-tri-state |
| **numbered in place** | `04-evaluation-statistics.md` | the noise doctrine: measurements are stochastic, margin is read against the floor, the evidence gate buys soundness rather than power, replicate bases are reserved, and a statistical change is proven by its operating characteristics |
| **numbered in place** | `05-proposer.md` | the proposer contract: the restricted-visibility envelope, mounted-tree-matches-chosen, screen-vetoes-never-ranks, pure-prompt-assembler, `ProposerError`-only, byte-identical-at-default |
| **T** | `06-tournament-and-selection.md` | tournament execution and the unit cache: evaluate-once, the canonical replicate slot, cache-only-budget-exhaustion, importable worker callables, config pins rather than environment, the gate as the per-duel decider, only-promotion-advances-the-champion, disjoint reserved bases, mounted-tree-matches-the-chosen-candidate, distinct-draws-only, placebo-never-crowns |
| **D** | `07-runtime-and-durability.md` | persistence and crash-safety: files canonical and the index derived, best-effort index writes, atomic record writes, torn-tail tolerance for append-only logs, transactional derivation, known-shape ephemeral checkouts, prune-trees-never-records, outcome-before-journal-and-lineage, pid-plus-start-time identity, one writer per event log, best-effort round-log emission, refuse-a-newer-record-format |
| **S** | `08-supervisor.md` | the supervisor's out-of-band enforcement: out-of-band supervision, never-kill-the-orchestrator, vetted pid signalling, clamped deadlines, confirmed death before reaping, path-confined snapshot collection, a ledger that records without gating, a read-only version-pinned index, the sole worker signaller, read-only fail-open integrity checks, two loops with a fixed trigger priority, a live surface that never blocks or leaks, no cached state across ticks, an operational rather than analytical HTTP surface |
| **DQ** | `09-dashboard-and-query.md` | the dashboard and query doctrine: server-computes-client-renders, one spelling per wire field, every reader is best-effort, the query layer is library code, change-signals carry no content, a no-op heartbeat rebuilds zero DOM, verdicts are honest about the noise floor, null-degrade under the Rust supervisor, controls gate on writability, the champion is the reigning spine end, a payload-shape change is a clean break, validate an id before it touches the workspace, every JSON GET has a declared contract, lineage owns topology, composite readers share walks |
| **L** | `10-builder-cli-library.md` | the builder and library boundary: one mutation surface, full coverage for a new knob, an honest twinned cost meter, recommend-only, the builder never rolls the epoch, config pins rather than environment, a lazy pure facade, the library never imports a driver |
| **V** | `11-testing.md` | the verification discipline: the full suite is the default, a regression test must fail with the fix stashed, never weaken an assertion, pin a knob off and carry the adversarial countermeasure, a worker resolves callables from a dotted path, fixtures clear global state on both sides, the reaper selects by workspace provenance, parity gates stay green on unchanged behaviour, the import contracts are lint, the exit code is the node signal |

> ⚠️ **TRAP** — `05-proposer.md` also cites the process-exemplar redaction rules
> (`R1`–`R4`): the payload allowlist, identity anonymization, free-text
> truncation, and the identity-corpus scrub. They are defined in
> `docs/design/PROCESS-EXEMPLARS.md` and summarized in `05-proposer.md`. They are
> a spec of their own rather than a chapter invariant namespace.

---

## The twelve bugs, at a glance (full cases in `12-bug-casebook.md`)

Most of these escaped the entire test suite because a deterministic test
contract pinned the interacting knob off. The recurring class — **shared mutable
state across per-candidate, per-replicate, or per-feature artifacts** — is the
single most important pattern to internalize. Before you touch a surface, read
its case; each ends with "you are about to reintroduce this if…".

| # | Bug | Surface | The one-line tell you're about to repeat it |
|---|---|---|---|
| 1 | Replicate-cache clobbering | unit cache | you write a per-replicate result into the canonical (r0) slot |
| 2 | `worktree prune` vs concurrent `add` race | git store | you run a repo-global git admin command while siblings run concurrently |
| 3 | A/A calibration false-zero floor | calibration | you draw "independent" samples without varying the replicate index |
| 4 | Client champion-scan (first vs reigning) | dashboard | the client computes a decision the server already owns |
| 5 | The "evolve hang" that was the test reaper | tests | a fixture signals by process group / by name instead of by provenance |
| 6 | Best-of-N tree mismatch (gauntlet) | proposer | the mounted child tree comes from the last-sampled candidate rather than the chosen one |
| 7 | …its field-path + diversity extension | proposer | you judge diversity on a hypothesis whose tree isn't on disk |
| 8 | Evidence-gate replicate-slot reuse | evidence gate | your "replicates" reuse a canonical slot → replay shrinks the CI |
| 9 | Git `derive_generation` stale shared worktree | git store | you move a tag but leave a shared worktree at the old commit |
| 10 | Contract hash embedding cwd/checkout | contract | you `resolve()` a path into an identity that must be location-independent |
| 11 | `judge_view` opened the index READ-WRITE on a read path | index | a read-only query path constructs the connection without the read flag |
| 12 | The `elimFlow` defensive-guard family, kept for a payload the server now serves whole | dashboard | you keep client guards for a shape the server does not emit |

---

## The pre-commit ladder (full detail in `11-testing.md §11.11`)

Run this, in order, before proposing any commit. A red rung is a blocked commit —
fix it or justify it (a pinned-number change needs a measured justification in the
commit body; a legitimately-moved golden needs the never-bake-a-sibling-change
rule honored).

```bash
uv run pytest tests/ -m "not slow and not node" -q     # 1. fast lane (~15s) — quick signal
uv run pytest tests/ -q                                # 2. full suite (the default; ~50s)
uv run ruff format . && uv run ruff check .            # 3. style
uv run mypy src/zicato/                                # 4. types
uv run lint-imports                                    # 5. the import contracts
bash tools/parity.sh                                   # 6. the parity gates
make node-test ; echo "node exit: $?"                  # 7. the JS behaviour suite
uv run pytest tests/test_convergence_known_answer.py \
             tests/test_decision_procedure_power.py -q # 8. the two oracles
python tools/line_budget.py --check                      # 9. simplification budgets
git log -p <base>..HEAD | grep -icE "$pat"    # 10. vendor scan: assemble $pat per 01-orientation §G1 → 0
python tools/prose_lint.py \
  --baseline tools/prose_lint_baseline.json              # 11. prose gate (ratchet)
```

> ✅ **ALWAYS** run the vendor scan (rung 10). It is the cheapest rung and the one
> whose failure is least recoverable: a vendor leak in a pushed commit means a
> history rewrite.

---

## The recipe index (full recipes in `13-recipes.md`)

If your task is here, follow the recipe — do not improvise. Each is self-contained
(When to use / Files touched / Steps / Traps / Verify / Definition of done).

1. Add a health detector · 2. Add a loss-pattern detector · 3. Add a scoring
namespace / weight · 4. Add a board expectation kind · 5. Add a goldfive
drift-kind consumer · 6. Extend the deterministic example target (updating both
oracles honestly) · 7. Add an index table / column (schema bump + migration +
golden re-capture) · 8. Add an epoch-open step · 9. Change the round pipeline
safely (the seam-ownership map) · 10. Run the full local verification ladder ·
11. Investigate a red parity gate · 12. Debug a failing tournament end-to-end run
(the forensic file map) · 13. Safely bump a pinned operating-characteristic
number · 14. Add a `skills/` entry for a new operator workflow.

Cross-cutting recipes also live in their owning chapters: **add a contract knob**
(`03 §recipe`), **add a tournament structure** / **make a harness adapter**
(`06 §recipes`), **add a proposer tool** / **add a prompt-context channel**
(`05 §recipes`), **add a runtime state field** / **add a RoundLog event**
(`07 §recipes`), **add a control route** (`08 §recipe`), **add a reader + endpoint
+ panel** / **change a payload shape** (`09 §recipes`), **add a builder op** / **add
a CLI flag** (`10 §recipes`).
