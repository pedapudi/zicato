# The zicato Development Guide

> **Audience:** any coding agent — including a weaker model — that will change
> zicato's source. **Purpose:** let you extend the system *without reintroducing
> a bug the project has already fixed and without violating an invariant the
> project depends on.* Every claim in these chapters is grounded in the actual
> code on the branch that shipped them; every code excerpt is copied verbatim
> from the file it names.
>
> This is a **reference book, not a tutorial.** Read §"How to use this guide"
> once, then jump to the chapter your task touches — but never skip the Golden
> Rules (§below) or the pre-commit ladder (§below). They are short, and every
> one of them exists because breaking it caused a real failure.

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
   relevant case in `12-bug-casebook.md`. These five surfaces are where every one
   of the project's ten shipped bugs lived.
6. **Find your task in `13-recipes.md`.** Most changes are a named recipe with
   exact steps and a Verify block. If yours is not, the closest recipe is your
   template.
7. **Before proposing any commit, run the pre-commit ladder** (§below, full
   detail in `11-testing.md §11.11`). A change that has not passed it is not
   done.

> ⛔ **NEVER** treat this guide as authoritative *over the code*. It was accurate
> when written. If a symbol, path, or line the guide names is not in the current
> tree, the code is right and the guide is stale — fix the guide in the same PR
> (see `13-recipes.md` recipe 14 for the docs-land discipline), and never invent
> a symbol to match the prose.

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
| **G3** | No live model run without go-ahead | Live runs cost money and need explicit operator sign-off; the deterministic `target_0` recipe is the sanctioned e2e vehicle. | run `examples/zicato_examples/target_0_convergence/RUN.md`, not a real model |
| **G4** | The two oracles are green before ANY commit | `test_convergence_known_answer` (the loop converges) + `test_decision_procedure_power` (the decision procedure's measured operating characteristics). | `uv run pytest tests/test_convergence_known_answer.py tests/test_decision_procedure_power.py -q` |
| **G5** | Parity + import contracts + node | `bash tools/parity.sh` (6 gates), `uv run lint-imports` (5 contracts), `make node-test`. | all green |
| **G6** | Omit-at-default contract discipline | A new default-off contract field MUST be registered in `_SCORING_OMIT_AT_DEFAULT_FIELDS`, or every existing workspace spuriously rolls its epoch. | `03-contract-and-epochs.md`; contract-hash parity gate |
| **G7** | The reserved replicate-base ledger | Duels `0..`, calibration `1000`, preflight `2000`, screening `3000/3001`, evidence `4000`. Squatting a base corrupts the unit cache (bugs #1, #8). | `04-evaluation-statistics.md §"reserved bases"` |
| **G8** | The restricted-visibility envelope | Nothing entry-identifying (entry ids, task text, holdout data, raw per-entry outcomes) may reach the proposer. Every channel is banded/aggregated/anonymized/redacted. | `05-proposer.md §"envelope"`; adversarial-identity fixtures |
| **G9** | Module-level callables only across the worker boundary | `_callable_dotted_path` rejects closures; scripted proposers/harnesses are module-level functions + module state + `reset()`. | `06-tournament-and-selection.md §"worker boundary"` |
| **G10** | Digest-gated rendering | A no-op SSE heartbeat must cause ZERO DOM rebuild; views fold content digests and swap only on change. | `09-dashboard-and-query.md §"digest gating"`; node DOM-node-identity tests |

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
| 11 | `11-testing.md` | the suites, the two oracles, the six parity gates, the import contracts, the **pre-commit ladder** | before every commit; when you add a test |
| 12 | `12-bug-casebook.md` | the ten shipped bugs as teaching cases + the meta-lessons | before touching any of the five bug-prone surfaces |
| 13 | `13-recipes.md` | the cookbook — 14 self-contained, copy-precise recipes | you are about to make a change (find yours first) |
| 14 | `14-goals-and-roadmap.md` | the north star, the proof state, the endpoint-gated runbook, the deferred register, anti-goals | you are proposing new work or a live run |

Total: **~22,000 lines** across 14 chapters + this index.

---

## Master invariant index

Each surface owns a numbered invariant namespace. Within a chapter, invariants are
cited by their bare id (`T4`, `DQ7`); across chapters, qualify by chapter (`06-T4`).
Break one of these and something the project depends on fails — the citing section
states the exact failure mode.

| Namespace | Owner | Governs | Count |
|---|---|---|---|
| **G1–G10** | `01-orientation.md §4` | the Golden Rules (above) | 10 |
| **(1–8)** | `03-contract-and-epochs.md` | the contract hash + epoch identity (hash-identifies-contract-not-checkout, omit-at-default, serializer-completeness, edit-body-rolls, runtime-never-hashed, legacy-is-None, refuse-on-newer, lineage-tri-state) | 8 |
| **(1–10)** | `04-evaluation-statistics.md` | the noise doctrine (measurements are stochastic; margin-vs-floor; evidence gate = soundness not power; reserved bases; prove-by-operating-characteristics) | 10 |
| **(1–6)** | `05-proposer.md` | the proposer contract (envelope; mounted-tree-matches-chosen; screen-vetoes-never-ranks; pure-prompt-assembler; `ProposerError`-only; byte-identical-at-default) | 6 |
| **T1–T11** | `06-tournament-and-selection.md` | tournament execution + the unit cache (evaluated-once; r0-canonical; infra-never-cached; closures-rejected; pins-not-env; gate-is-the-decider; promoted-only-advances; disjoint-bases; tree-matches-chosen; distinct-draws-only; placebo-never-crowns) | 11 |
| **D1–D12** | `07-runtime-and-durability.md` | persistence + crash-safety (files-canonical; atomic-write; append-only-tolerance; git-store; resume-classification; control-protocol; RoundLog-best-effort; format_version; …) | 12 |
| **S1–S13** | `08-supervisor.md` | out-of-band enforcement (warn-only-heartbeat; pid-start-time-identity; clamped-deadlines; confirmed-dead-reaping; hash-chained-ledger; read-only-index; …) | 13 |
| **DQ1–DQ12** | `09-dashboard-and-query.md` | dashboard/query (server-computes-client-renders; one-wire-spelling; reader-best-effort; query-is-lib; SSE-change-kinds-only; no-op→zero-DOM; noise-honest-verdicts; null-degrade-on-supervisor; control-gating; reigning-champion; clean-break-payloads; `_is_safe_id`) | 12 |
| **L1–L8** | `10-builder-cli-library.md` | builder + library boundary (one-mutation-surface; full-coverage; honest-twinned-cost; recommend-only; builder-never-rolls; pins-not-env; lazy-pure-facade; lib-never-imports-driver) | 8 |
| **V1–V10** | `11-testing.md` | the verification discipline (full-suite-default; must-fail-with-fix-stashed; never-weaken; pin-off-AND-adversarial-on; module-level-stubs; fixtures-clear-both-sides; provenance-scoped-reaper; parity-green-on-unchanged; contracts-enforced; node-exit-code-is-signal) | 10 |

> ⚠️ **TRAP** — `05-proposer.md` also cites **R1–R4**: those are the *redaction
> rules* of the process-exemplar channel, defined in `docs/design/PROCESS-EXEMPLARS.md`
> and summarized in `05-proposer.md §"process exemplars"`. They are a spec, not a
> chapter invariant namespace.

---

## The ten bugs, at a glance (full cases in `12-bug-casebook.md`)

Every one of these escaped the entire test suite because a *deterministic test
contract pinned the interacting knob OFF.* The recurring class — **shared mutable
state across per-candidate / per-replicate / per-feature artifacts** — is the
single most important pattern to internalize. Before you touch a surface, read its
case; it ends with "you are about to reintroduce this if…".

| # | Bug | Surface | The one-line tell you're about to repeat it |
|---|---|---|---|
| 1 | Replicate-cache clobbering | unit cache | you write a per-replicate result into the canonical (r0) slot |
| 2 | `worktree prune` vs concurrent `add` race | git store | you run a repo-global git admin command while siblings run concurrently |
| 3 | A/A calibration false-zero floor | calibration | you draw "independent" samples without varying the replicate index |
| 4 | Client champion-scan (first vs reigning) | dashboard | the client computes a decision the server already owns |
| 5 | The "evolve hang" that was the test reaper | tests | a fixture signals by process group / by name instead of by provenance |
| 6 | Best-of-N tree mismatch (gauntlet) | proposer | the mounted child tree is the last-sampled, not the chosen, candidate |
| 7 | …its field-path + diversity extension | proposer | you judge diversity on a hypothesis whose tree isn't on disk |
| 8 | Evidence-gate replicate-slot reuse | evidence gate | your "replicates" reuse a canonical slot → replay shrinks the CI |
| 9 | Git `derive_generation` stale shared worktree | git store | you move a tag but leave a shared worktree at the old commit |
| 10 | Contract hash embedding cwd/checkout | contract | you `resolve()` a path into an identity that must be location-independent |
| 11 | `judge_view` opened the index READ-WRITE on a read path | index | a read-only query path constructs the connection without the read flag |
| 12 | The `elimFlow` defensive-guard family died with the served model | dashboard | you keep client guards for a shape the server no longer emits |

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
uv run lint-imports                                    # 5. the 5 import contracts (G5)
bash tools/parity.sh                                   # 6. the 6 parity gates (G5)
make node-test ; echo "node exit: $?"                  # 7. the JS behaviour suite (G5/G10)
uv run pytest tests/test_convergence_known_answer.py \
             tests/test_decision_procedure_power.py -q # 8. the two oracles (G4)
git log -p <base>..HEAD | grep -icE "$pat"    # 9. vendor scan (G1): assemble $pat per 01-orientation §G1 → 0
```

> ✅ **ALWAYS** end at rung 9. It is the cheapest rung and the one whose failure is
> least recoverable — a vendor leak in a pushed commit means a history rewrite.

---

## The recipe index (full recipes in `13-recipes.md`)

If your task is here, follow the recipe — do not improvise. Each is self-contained
(When to use / Files touched / Steps / Traps / Verify / Definition of done).

1. Add a health detector · 2. Add a loss-pattern detector · 3. Add a scoring
namespace/weight · 4. Add a board expectation kind · 5. Add a goldfive drift-kind
consumer · 6. Extend the deterministic example target (and update BOTH oracles
honestly) · 7. Add an index table/column (schema bump + migration + golden
re-capture) · 8. Add an epoch-open step · 9. Touch the orchestrator safely (the
seam-ownership map) · 10. Run the full local verification ladder · 11. Investigate
a red parity gate · 12. Debug a failing tournament e2e (the forensic file map) ·
13. Safely bump a pinned operating-characteristic number · 14. Add a `skills/`
entry for a new operator workflow.

Cross-cutting recipes also live in their owning chapters: **add a contract knob**
(`03 §recipe`), **add a tournament structure** / **make a harness adapter**
(`06 §recipes`), **add a proposer tool** / **add a prompt-context channel**
(`05 §recipes`), **add a runtime state field** / **add a RoundLog event**
(`07 §recipes`), **add a control route** (`08 §recipe`), **add a reader + endpoint
+ panel** / **change a payload shape** (`09 §recipes`), **add a builder op** / **add
a CLI flag** (`10 §recipes`).
