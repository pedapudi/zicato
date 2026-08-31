---
name: zicato-dev-guide
description: The entry point for CHANGING zicato's own source (not operating a workspace). Routes to the full 14-chapter development guide under docs/dev-guide/ (~23k lines, code-grounded), and inlines the non-negotiables an agent must never skip — the 10 Golden Rules, the chapter map (which chapter owns which surface), the pre-commit verification ladder, and the twelve shipped bugs with the one-line tell for each. Use whenever you edit orchestrator/proposer/tournament/selection/scoring/runtime/storage/supervisor/dashboard/builder/CLI code, add a contract knob, touch the evaluation statistics or the overfitting envelope, or write tests. The operator skills (zicato-evolve, zicato-design-boards, …) teach how to RUN the loop; this teaches how to safely CHANGE it.
---

# Contributing to zicato

**The authoritative reference is the development guide: [`docs/dev-guide/`](../../docs/dev-guide/)** (14 chapters, ~23k lines).
Start at **[`docs/dev-guide/00-INDEX.md`](../../docs/dev-guide/00-INDEX.md)** — it
has the how-to-read path, the chapter map, the master invariant index, and the
recipe index. It is a *reference book rather than a tutorial*: read the Golden Rules
every session, then jump to the chapter your task touches. Every claim there is
grounded in the current code; every excerpt is verbatim from the file it names.

> **When docs and code disagree, the code wins.** The guide was accurate when
> written. If a symbol/path it names is gone, fix the guide in the same PR
> (recipe 14) — never invent a symbol to match the prose. `docs/design/CLI.md`
> is a *generated* doc; trust `zicato --help`.

This skill is the doorway. It inlines the four things you must not skip even if
you read nothing else. For depth on any of them, open the cited chapter.

---

## 1. The 10 Golden Rules (full text: `00-INDEX.md` / `01-orientation.md §4`)

If a rule and your task conflict, the rule wins — stop and ask. Each exists
because breaking it caused a real failure.

- **G1 — Vendor rule.** Nothing in git (code, comments, docstrings, tests,
  fixtures, commit messages, PR bodies, trailers) references the model vendor.
  Verify: `git log -p <base>..HEAD | grep -icE "$pat"` with `$pat` assembled per `01-orientation.md §G1` → **0**. A leak in a pushed commit means a history rewrite.
- **G2 — `uv sync --all-extras`, always.** Bare `uv sync` deletes pytest/mypy/ruff/uv from `.venv`.
- **G3 — No live model run without explicit operator go-ahead.** The deterministic `examples/zicato_examples/target_0_convergence/RUN.md` is the sanctioned e2e vehicle. Every live run also reports its dashboard URL.
- **G4 — The two oracles are green before ANY commit.** `tests/test_convergence_known_answer.py` (the loop converges to an exact floor) + `tests/test_decision_procedure_power.py` (the decision procedure's measured operating characteristics).
- **G5 — Parity + import contracts + node.** `bash tools/parity.sh` (6 gates), `uv run lint-imports` (5 contracts), `make node-test`.
- **G6 — Omit-at-default.** A new default-off contract field MUST declare `metadata=_knob(omit_at_default=True)` — `_SCORING_OMIT_AT_DEFAULT_FIELDS` is DERIVED from that flag — or every workspace spuriously rolls its epoch (`03-contract-and-epochs.md`).
- **G7 — Reserved replicate-base ledger.** Duels `0..`, calibration `1000`, preflight `2000`, screening `3000/3001`, evidence `4000`. Squatting a base corrupts the unit cache — this was bugs #1 and #8 (`04-evaluation-statistics.md`).
- **G8 — Restricted-visibility envelope.** Nothing entry-identifying (entry ids, task text, holdout data, raw per-entry outcomes) reaches the proposer; every channel is banded/aggregated/anonymized/redacted (`05-proposer.md`).
- **G9 — Module-level callables only across the worker boundary.** Closures are rejected by `_callable_dotted_path`; scripted proposers/harnesses are module-level functions + module state + `reset()` (`06-tournament-and-selection.md`).
- **G10 — Digest-gated rendering.** A no-op SSE heartbeat must cause ZERO DOM rebuild (`09-dashboard-and-query.md`).

---

## 2. Which chapter owns your surface

| You are changing… | Read | Key invariant namespace |
|---|---|---|
| the round pipeline / orchestrator seams | `02-architecture.md`, `13-recipes.md` recipe 9 | the extracted seams; never inline into the god-functions |
| a contract knob / epoch behavior | `03-contract-and-epochs.md` | contract invariants 1–8 (+ G6) |
| scoring / the gate / replication / calibration / evidence gate / contract pre-flight | `04-evaluation-statistics.md` | statistics 1–10 (+ G7) |
| how candidates are generated / what the proposer sees | `05-proposer.md` | proposer 1–6 (+ G8) |
| tournament execution / structures / the worker / caching | `06-tournament-and-selection.md` | the unit-cache and gate rules, **T1–T11** (+ G9) |
| state files / storage / resume / the round log | `07-runtime-and-durability.md` | the persistence and crash-safety rules, **D1–D12** |
| the Rust supervisor | `08-supervisor.md` | the out-of-band enforcement rules, **S1–S14** |
| a reader / endpoint / view | `09-dashboard-and-query.md` | the dashboard/query doctrine, **DQ1–DQ15** (+ G10) |
| the builder / a CLI flag / the public API | `10-builder-cli-library.md` | the builder and library-boundary rules, **L1–L8** |
| tests | `11-testing.md` | the verification discipline, **V1–V10** |

Cross-cutting **recipes** live in `13-recipes.md` (14 of them) and in the owning
chapters (add-a-contract-knob, add-a-tournament-structure, make-a-harness-adapter,
add-a-proposer-tool, add-a-reader+endpoint+panel, add-a-CLI-flag, …). Find your
recipe **before** you start; if there isn't one, the closest is your template.

---

## 3. The pre-commit ladder (full detail: `11-testing.md §11.11`)

Run in order before proposing any commit. A red rung blocks the commit — fix it,
or justify it (a pinned operating-characteristic number needs a *measured*
justification in the commit body; a legitimately-moved golden must honor the
never-bake-a-sibling-change rule).

```bash
uv run pytest tests/ -m "not slow and not node" -q      # 1. fast lane (~15s)
uv run pytest tests/ -q                                 # 2. full suite (default; ~50s)
uv run ruff format . && uv run ruff check .             # 3. style
uv run mypy src/zicato/                                 # 4. types
uv run lint-imports                                     # 5. import contracts (G5)
bash tools/parity.sh                                    # 6. parity gates (G5)
make node-test ; echo "node exit: $?"                   # 7. JS suite (G5/G10)
uv run pytest tests/test_convergence_known_answer.py \
             tests/test_decision_procedure_power.py -q  # 8. the two oracles (G4)
cargo test -p zicato-supervisor                         # 9. if you touched a two-language contract
git log -p <base>..HEAD | grep -icE "$pat"    # 10. vendor scan (G1): assemble $pat per 01-orientation §G1 → 0
```

---

## 4. The twelve shipped bugs — the tell you're about to repeat one (`12-bug-casebook.md`)

Every one escaped the whole suite because a deterministic test contract pinned the
interacting knob OFF. The recurring class is **shared mutable state across
per-candidate / per-replicate / per-feature artifacts**. Read the case before you
touch its surface.

1. **Replicate-cache clobbering** — you write a per-replicate result into the canonical (r0) slot.
2. **`worktree prune` vs concurrent `add`** — you run a repo-global git admin command while siblings run concurrently.
3. **A/A false-zero floor** — you draw "independent" samples without varying the replicate index.
4. **Client champion-scan** — the client computes a decision the server already owns.
5. **The "evolve hang"** — a fixture signals by process group / by name instead of by provenance.
6. **Best-of-N tree mismatch (gauntlet)** — the mounted child tree is the last-sampled candidate rather than the chosen one.
7. **…field-path + diversity** — you judge diversity on a hypothesis whose tree isn't on disk.
8. **Evidence-gate replicate reuse** — your "replicates" reuse a canonical slot → replay shrinks the CI.
9. **Git stale shared worktree** — you move a tag and leave a shared worktree at the commit it named before.
10. **Contract hash embeds cwd/checkout** — you `resolve()` a path into an identity that must be location-independent.
11. **`judge_view` opened the index READ-WRITE on a read path** — a reader holds a writable connection, contends with the live ingester and drifts the parity goldens.
12. **`elimFlow`'s defensive-guard family** — the client derives a domain model the payload should have served, against the rule that the server computes and the client renders, accreting about 100 lines of guards.

**Two hard test rules (V-invariants):** a regression test MUST fail with the fix
stashed; never weaken an assertion — pin the knob and add an adversarial knob-ON
test (that adversarial test is exactly what would have caught bugs #6 and #8).

---

## 5. Your first hour

```bash
uv sync --all-extras                                    # G2
uv run pytest tests/ -m "not slow and not node" -q      # confirm a green fast lane
uv run pytest tests/test_convergence_known_answer.py -q # watch the loop converge (the oracle)
# then read: docs/dev-guide/00-INDEX.md → 01-orientation.md → the chapter for your task
```

The operator workflows (running the loop, designing boards, tuning scoring) live
in the sibling `zicato-*` skills. This one is only for changing zicato itself.
