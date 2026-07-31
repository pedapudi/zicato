# 13 — Recipes (the cookbook)

> **Covers.** Fourteen self-contained recipes for the changes agents make most
> often, each grounded in the code you will actually edit. This is the chapter to
> open first: every recipe stands alone — you can complete it without reading
> another chapter — and its traps LINK to the casebook (12-bug-casebook.md) or
> the deep chapter when you need the theory behind a step.
>
> **Prerequisites.** None to *use* a recipe. Each recipe names its own files and
> commands. The recipes assume you have run the one-time setup once:
> `uv sync --all-extras` (01-orientation.md §"Set up"; **always `--all-extras`**
> — a bare `uv sync` deletes dev tooling from `.venv`). For the theory behind a
> change class, each recipe cross-refs the owning chapter.
>
> **How to read a recipe.** Every recipe uses ONE fixed template so you always
> know where to look:
>
> | Section | What it gives you |
> |---|---|
> | **When to use** | the one-line trigger — is this the right recipe? |
> | **Files touched** | every file you will edit, with the symbol in it |
> | **Steps** | numbered, naming real symbols — do them in order |
> | **Traps** | ⚠️ the mistakes that cause bugs, each linked to a bug # or chapter |
> | **Verify** | the exact commands that prove you are done |
> | **Definition of done** | the finish line, in one sentence |
>
> **The two commands every recipe ends near.** Before you propose ANY commit,
> the two oracles must be green (Golden Rule G4), no matter how unrelated your
> change feels:
>
> ```bash
> uv run pytest tests/test_convergence_known_answer.py -q   # the known-answer loop
> uv run pytest tests/test_decision_procedure_power.py -q   # the operating characteristics
> ```
>
> Recipe 10 is the full verification ladder as a standalone checklist.

**Recipe index**

| # | Recipe | Owning chapter |
|---|---|---|
| 1 | Add a health detector | 08-supervisor.md (health surface) |
| 2 | Add a loss-pattern detector | 05-proposer.md (visibility envelope) |
| 3 | Add a scoring namespace / weight | 04-evaluation-statistics.md |
| 4 | Add a board expectation kind | 03-contract-and-epochs.md |
| 5 | Add a goldfive drift-kind consumer | 04-evaluation-statistics.md |
| 6 | Extend the deterministic example target | 11-testing.md |
| 7 | Add an index table / column | 07-runtime-and-durability.md |
| 8 | Add an epoch-open step | 03-contract-and-epochs.md |
| 9 | Touch the orchestrator safely | 02-architecture.md |
| 10 | Run the full local verification ladder | 11-testing.md |
| 11 | Investigate a red parity gate | 11-testing.md |
| 12 | Debug a failing tournament e2e | 06/07 |
| 13 | Safely bump a pinned OC number | 04-evaluation-statistics.md |
| 14 | Add a `skills/` entry | 10-builder-cli-library.md |

---

## Recipe 1 — Add a health detector

**When to use.** You want the loop-health surface (`zicato health`, the
dashboard health ribbon, the per-round health report) to flag a new failure
condition — e.g. "every challenger this epoch changed the same mutation point"
or "the judge panel has gone silent".

**Files touched.**

| File | Symbol | Why |
|---|---|---|
| `src/zicato/health/diagnostics.py` | `detect_<name>(...) -> list[HealthFinding]` | the detector itself |
| `src/zicato/health/diagnostics.py` | `assess_loop_health` | the run-list dispatch |
| `src/zicato/health/diagnostics.py` | `__all__` | export it |
| `src/zicato/health/__init__.py` | re-export | package surface |
| `src/zicato/config.py` | `HealthConfig` | a new threshold, if any |
| `src/zicato/orchestrator.py` | `_assess_and_persist_loop_health` | new inputs, if any |
| `tests/test_health_diagnostics.py` | per-detector test | pin it |

**Steps.**

1. **Write the detector as a pure function** in `diagnostics.py`:
   `def detect_<name>(...) -> list[HealthFinding]`. It takes already-loaded data
   (loss profiles, experiment dicts, the board) — never does I/O — and returns
   zero or more findings. Each `HealthFinding` carries a stable `code`, a
   `severity`, a one-line `summary`, and a structured `detail` dict:

   ```python
   # src/zicato/health/diagnostics.py — HealthFinding
   class HealthFinding:
       code: str
       severity: str        # "info" | "warning" | "critical"
       summary: str
       detail: dict[str, Any] = field(default_factory=dict)
   ```

   Model your detector on an existing one of the same shape:
   `detect_degenerate_scoring` (emits `critical`), `detect_stalled_loop`
   (reject-streak), or `detect_generalization_gap` (warning/critical on a
   train/holdout gap).
2. **Choose severity honestly.** `info` is advisory; `warning`/`critical` mark
   the loop *unhealthy* (any warning-or-critical flips `LoopHealth.healthy` to
   `False`, and the CLI exits non-zero on a `critical`). Reserve `critical` for
   "the loop is not measuring what it thinks it is."
3. **Register it in the run list.** `assess_loop_health` is the dispatch — a
   hard-coded sequence of `findings.extend(detect_*(...))` calls. Add one line
   for your detector there. There is no decorator registry; the run list IS the
   registry:

   ```python
   # src/zicato/health/diagnostics.py — assess_loop_health (the run list)
       findings: list[HealthFinding] = []
       findings.extend(detect_degenerate_scoring(experiments, health))
       findings.extend(detect_non_differentiating_entry(losses_by_generation))
       findings.extend(detect_flat_drift_signal(losses_by_generation))
       ...
       findings.extend(detect_token_budget_clip(token_clip))

       healthy = not any(finding.severity in ("warning", "critical") for finding in findings)
   ```

   Your `findings.extend(detect_<name>(...))` goes in that block — pass it only
   the inputs the function already receives; add a new input via step 6.
4. **Export it.** Add the function name to `__all__` in `diagnostics.py` and
   re-export it from `src/zicato/health/__init__.py`.
5. **Add a threshold only via `HealthConfig`.** If your detector needs a tunable
   bound, add a typed field to `zicato.config.HealthConfig` (the `health` block
   of `config.json`) and read it via `_resolve_health_config` — never a bare
   constant an operator cannot change, and never an environment variable (see
   10-builder-cli-library.md §"the merited env-var set").
6. **Thread new inputs through the orchestrator only if needed.** If your
   detector needs a fact `assess_loop_health` is not already handed, add it to
   `_collect_epoch_health_inputs` and `_assess_and_persist_loop_health` in
   `orchestrator.py`, which writes the per-round report to
   `epochs/{epoch}/health/round_{N}.json`.
7. **Test it** in `tests/test_health_diagnostics.py`: a fixture that trips the
   detector (assert the finding's `code`/`severity`/`detail`), and a fixture
   that does NOT (assert empty). If you touched the orchestrator threading, add
   a case to `tests/test_orchestrator_health.py`.

**The five-slot evidence convention — a RENDER conformance rule.**

A detector's job does not end at detecting. Issue #129 generalised eleven
reports into one complaint — zicato says something is wrong and does not say
what — and every instance was a surface that had the numbers in hand and
rendered a verdict instead.

Read that carefully, because it decides where the rule binds. The collection
layer was already in good shape: all nineteen `HealthFinding` sites populate
`detail`, fifteen of them write an explicit `detail["recommendation"]`, and
`PracticeCheck` carries a structured `evidence` dict beside every verdict. The
codebase has now collected well-shaped evidence **twice** and dropped it at the
last hop **both times** — `_summarise_loop_health` skipped `detail` because its
text walker accepted only string attributes, and `_render_practice_section`
simply never read `evidence`. A third well-shaped field on a third dataclass
would reproduce the bug, not fix it.

So the rule is about renderers, not about data shapes:

> **Any renderer that consumes a diagnostic structure must surface that
> structure's evidence.** Adding a field is not the fix; reading it is.

The two pins in `tests/test_issue_129_pins.py` are the enforcement precedents —
`test_loop_health_summary_carries_the_detector_s_recommendation` and
`test_practice_review_renders_the_evidence_behind_its_verdict`. Both assert a
measured quantity appears in rendered operator-facing output, and neither
asserts the phrasing around it. Write a new one in that shape when you add a
renderer: pin the number reaching the reader, leave the prose free to change.

The evidence a renderer must carry fills five slots:

| Slot | The question it answers | Example |
|---|---|---|
| Population | what was looked at, and how much of it | `6 consecutive generations`, `fired 3/10` |
| Measured | the quantity that tripped the rule | `loss fell by only 0.001234` |
| Compared against | the bound it was measured against | `promote_margin 0.005`, `noise floor 0.04` |
| Remedy | what to change | `raise promote_margin above the measured floor` |
| Remedy safety | why the remedy is or is not proposed | `recommendation_raises_margin=False` |

Not every slot applies to every message — a detector with no actionable remedy
should say nothing rather than invent one — but a message that fills *only* the
verdict slot is the defect this convention exists to prevent. The two
collection-side precedents to read before writing a new one:

- `check_promotion_hygiene` (`reflection/practices.py`) fills all five. It
  carries the numbers inline in its `headline`, the structured pair in
  `evidence`, an appliable `proposed_op`, and — where the recommendation would
  *lower* `promote_margin` — `recommendation_raises_margin=False` with no op,
  saying why it declines to propose.
- `GateEvaluated` (`epoch/round_log.py`) is the structural half. It splits the
  contract (`champion_scalar` / `challenger_scalar` / `margin_required`, always
  recorded) from the presentation (`rule_fired`, prose that varies by rule and
  is empty on a promote). Consumers compute on the fields; operators read the
  prose. A number that exists only inside a human-readable string is not
  recorded.

There is a third verdict beyond pass and fail: **unmeasured**. `PracticeCheck`
models it as `VERDICT_UNMEASURED` plus an `unmeasured_reason` naming the missing
input, so "measured, and it is fine" never collapses into "there was nothing to
measure". Any surface that degrades when its input is absent — a run where the
champion is never unseated, a workspace that never ran noise-floor calibration —
needs that third state; reporting the reassuring value in its place is the
worst form of this bug, because it reads as an answer.

*Registered, not built.* Nothing in the workspace persists **rounds since the
last promotion**. Every surface that wants it re-derives it from whatever it
happens to hold — the promoted spine, the experiment list, the tournament rows
— and each one invents its own degradation when the derivation runs short:
`optimization_trajectory` reports `plateaued=False` because the promoted spine
is too short to plateau, and the dashboard's verdict gates on a measured noise
floor that opt-in calibration may never have produced. Those are separate
symptoms of one missing field. Persisting the counter once, at the point a
round settles, would make the whole class unrepresentable rather than patched
per surface — but it touches the epoch record's shape, so it is a design pass,
not a render fix, and it is recorded here for one.

**Traps.**

- ⚠️ **Health findings MAY carry entry/generation ids; proposer-visible patterns
  may NOT.** This is the opposite of Recipe 2. `HealthFinding.detail` is
  *operator-facing* (it renders in `zicato health` and the dashboard, which the
  operator sees) — so naming the offending entry id is correct and useful. Do
  NOT confuse it with the restricted-visibility envelope that governs the
  proposer (Golden Rule G8; 05-proposer.md §"The restricted-visibility
  envelope"). A health detector is not inside that envelope.
- ⚠️ **A detector must never raise into `assess_loop_health`.** The whole health
  subsystem is best-effort — the orchestrator lazy-imports it and degrades to
  `("", False)` if the `zicato.health` sibling is absent (mypy treats it as an
  optional module for exactly this reason). A detector that raises on malformed
  input turns a best-effort surface into a crash. Guard your parsing; return
  `[]` on data you cannot read.
- ⚠️ **Keep it pure and deterministic.** No clock, no RNG, no filesystem read
  inside the detector — the report is re-rendered and re-persisted, and a
  non-deterministic detector makes the round report churn.

**Verify.**

```bash
uv run pytest tests/test_health_diagnostics.py tests/test_orchestrator_health.py -q
uv run zicato health --help        # your detector's report line, if it surfaces to the CLI
uv run ruff check src/zicato/health/ && uv run mypy src/zicato/health/
```

**Definition of done.** The detector fires on the failure condition with the
right severity and structured detail, does nothing on healthy input, is in the
`assess_loop_health` run list and both `__all__`s, and its test is green — and
the two oracles (G4) still pass.

---

## Recipe 2 — Add a loss-pattern detector

**When to use.** You want the proposer to be TOLD about a new class of
train-slice failure — e.g. "runs are timing out on multi-turn entries" — so its
next experiment can target it. Patterns are advisory analyzer observations, not
gates.

**Files touched.**

| File | Symbol | Why |
|---|---|---|
| `src/zicato/patterns/detectors.py` | `detect_<name>(inp) -> list[Pattern]` | the detector |
| `src/zicato/patterns/detectors.py` | `ALL_DETECTORS` | register it |
| `src/zicato/patterns/__init__.py` | re-export | package surface |
| `src/zicato/proposer/prompts.py` | `_LEAKY_DETAIL_KEYS` | banding, IF your detail carries identity |
| `tests/test_patterns_detectors.py` | test | pin it |

**Steps.**

1. **Write the detector against `DetectorInput`.** The contract is
   `DetectorFn = Callable[[DetectorInput], list[Pattern]]`. `DetectorInput` is a
   frozen bundle of already-loaded, train-slice data:
   `losses: list[LossProfile]`, `entries: dict[str, BoardEntry]`,
   `events_paths: dict[str, Path]`. Your function is pure: empty-in → empty-out.
2. **Emit `Pattern` objects.** A `Pattern` carries `kind`, `summary`, a
   `detail: dict[str, str]`, a `severity` (`"info"|"warning"|"critical"`), and
   an `id` (a stable hash of `kind|summary|sorted(affected_ids)` — construct it
   through the module's `_pattern_id` helper so dedup works). Leave
   `affected_mutation_ids=()` — the proposer resolves those; a detector never
   guesses the manifest id.
3. **Register it.** Either append your function to the canonical `ALL_DETECTORS`
   tuple, OR decorate it with `@register_detector` (from
   `src/zicato/patterns/registry.py`) and let a caller compose
   `ALL_DETECTORS + get_all_detectors()`. `detect_patterns(inp)` runs each and
   **dedupes by `Pattern.id`** (first wins), so a stable id matters.
4. **Export it** from `src/zicato/patterns/__init__.py`.
5. **Respect the visibility envelope — the critical step.** Before a pattern
   reaches the proposer, `render_pattern_block(patterns, restrict=...)` in
   `proposer/prompts.py` routes its `detail` through `_aggregate_pattern_detail`
   when `restrict` is on (driven by
   `OverfittingConfig.restrict_proposer_visibility`). That function strips the
   keys in `_LEAKY_DETAIL_KEYS` and replaces them with an `entries_affected=N`
   count:

   ```python
   # src/zicato/proposer/prompts.py
   _LEAKY_DETAIL_KEYS = frozenset({"affected_entry_ids", "entry_id", "task_id", "agent"})
   ```

   If your detector puts a NEW identity-bearing key in `detail` (say
   `"failing_task"`), you MUST add it to `_LEAKY_DETAIL_KEYS`, or that identity
   leaks straight to the proposer under restrict.
6. **Test it** in `tests/test_patterns_detectors.py`: a fixture that produces the
   pattern (assert `kind`/`severity`/`detail`), a fixture that does not, and — if
   you added a detail key — an assertion that `render_pattern_block(...,
   restrict=True)` strips it.

**Traps.**

- ⚠️ **A new identity-bearing `detail` key that is not in `_LEAKY_DETAIL_KEYS`
  leaks the board to the proposer.** This is the restricted-visibility envelope
  (Golden Rule G8; 05-proposer.md §"The channel-author's checklist"). The leak
  is SILENT — the prompt is only ever read by the model, and nothing else in CI
  reads it — so the adversarial banding test is not optional. Plant an entry id
  in your detail and assert it does not survive `render_pattern_block(...,
  restrict=True)`.
- ⚠️ **Detectors are deterministic pure functions.** No RNG/clock — a pattern is
  re-presented across rounds, and non-determinism leaks new information each time
  it is re-rendered.
- ⚠️ **`detect_hot_tasks` / `detect_hot_agents` return `[]` when goldfive is
  absent.** If your detector replays goldfive events, guard the import and
  degrade to `[]` — the patterns layer must import without the optional dep.

**Verify.**

```bash
uv run pytest tests/test_patterns_detectors.py tests/test_proposer_prompts.py -q
uv run ruff check src/zicato/patterns/ && uv run mypy src/zicato/patterns/
```

**Definition of done.** The detector emits a stable-id `Pattern` on the failure
condition, is registered in `ALL_DETECTORS` (or via `register_detector`), any new
identity key is banded under restrict (proven by a test), and the two oracles
(G4) pass.

---

## Recipe 3 — Add a scoring namespace / weight

**When to use.** You want a new *objective* the scalar accounts for — e.g. a
`latency:` namespace so slower generations score worse — or a new weight knob on
an existing objective. A namespace is a metric-name prefix (with its trailing
colon) whose weighted mean folds into the generation scalar.

**Files touched.**

| File | Symbol | Why |
|---|---|---|
| `src/zicato/core/scoring_config.py` | `ScoringWeights.namespace_weights`, `_default_namespace_weights` | declare the namespace |
| `src/zicato/telemetry/reducer.py` | `MetricCount` emission | produce the metric |
| `src/zicato/scoring/builtins.py` | `builtin_scalar` | (usually no edit — the namespace loop is generic) |
| `src/zicato/tournament/gate.py` | `namespace_monotonicity` handling | monotonicity, if wanted |
| `tests/test_scoring_seams.py`, `tests/test_scoring_multi_objective.py` | tests | byte-identity + composition |

**Steps.**

1. **Declare the namespace weight.** Add your key (keep the trailing colon, e.g.
   `"latency:"`) to `namespace_weights` — either as a default in
   `_default_namespace_weights()` in `core/scoring_config.py`, or leave it
   operator-set. The SIGN encodes the "worse" direction: positive = higher is
   worse, negative = higher is better, zero = tracked but unscored.
2. **Emit the metric.** In `telemetry/reducer.py`, emit a
   `MetricCount(name="latency:...")` from the reducer so the namespace has data.
   Once a `MetricCount` under your prefix exists, it flows AUTOMATICALLY:
   `tournament/scoring.py::aggregate_namespaced_metrics` computes
   `{namespace: weight * mean}`, and `builtin_scalar`'s namespace loop folds it
   into the scalar. You usually do NOT edit `builtins.py` at all — adding a
   namespace is a data + weight change, not a seam change.
3. **Understand the scalar composition.** The generation scalar is
   `builtin_scalar` building a `scalar_components` dict (`drift`, `pass`, each
   namespace, `diff_complexity`) and summing it. **Term order in that dict is
   load-bearing** — `tests/test_scoring_seams.py` pins the byte-identity of the
   scalar, and reordering the sum changes float rounding:

   ```python
   # src/zicato/scoring/builtins.py — builtin_scalar (the composition)
       scalar_components: dict[str, float] = {
           "drift": drift_component,
           "pass": pass_component,
       }
       for ns, value in namespace_aggregates.items():
           if ns == "drift:":
               continue
           component_name = ns[:-1] if ns.endswith(":") else ns
           scalar_components[component_name] = value
       ...
       return sum(scalar_components.values())
   ```

   A namespace folds in via that loop — no edit to `builtin_scalar` needed; the
   `diff_complexity` term is appended LAST and only when opted in, so the default
   is byte-identical to the pre-feature formula.
4. **Monotonicity, if wanted.** To make the gate refuse a promotion that
   regresses your namespace, add your key to `ScoringWeights.namespace_monotonicity`
   (a `{namespace: bool}` map). `tournament/gate.py::_regressed_namespaces`
   consumes it. See 04-evaluation-statistics.md §"The promote gate" for the
   monotonicity scope semantics.
5. **Builder + omit-at-default.** The builder already edits `namespace_weights`
   via `set_namespace_weights` (10-builder-cli-library.md §"The op inventory"),
   so a GUI/copilot surface is free. Confirm your key is omitted-at-default from
   the canonical scoring form so existing epochs do not roll retroactively (a
   non-default value MUST roll — that is the contract).
6. **Test** in `tests/test_scoring_multi_objective.py` (the namespace composes
   into the scalar with the right sign and weight) and `tests/test_scoring_seams.py`
   (byte-identity at default: the namespace absent/zero produces the exact prior
   scalar).

**Traps.**

- ⚠️ **Reordering `scalar_components` breaks the byte-identity golden.** The sum
  is order-sensitive at float precision; `test_scoring_seams.py` is the pin. Add
  your term where the composition naturally places it and do not reorder the
  existing terms.
- ⚠️ **A namespace that changes the scalar at default rolls every epoch.** Follow
  the omit-at-default discipline (Golden Rule G6; 03-contract-and-epochs.md
  §"Omit-at-default fields"): default weight `0` (or the key absent from the
  canonical form) means existing epochs hash identically; a real weight rolls the
  epoch, which is correct — a new objective is a new contract.
- ⚠️ **Do NOT reshape a term into a seam edit when a weight will do.** A reshape
  (a transform, a reducer) belongs in the declarative `pass_transform` /
  `drift_kind_aggregation` registry or a `scalar_fn` plugin — not a hand-edit to
  `builtin_scalar`. See 04-evaluation-statistics.md §"The scoring seams".

**Verify.**

```bash
uv run pytest tests/test_scoring_seams.py tests/test_scoring_multi_objective.py \
    tests/test_tournament_scoring.py tests/test_tournament_gate.py -q
uv run pytest tests/test_epoch_contract.py -q      # omit-at-default: default did not roll
```

**Definition of done.** The namespace folds into the scalar with the right sign
and weight, its metric is emitted by the reducer, the default is byte-identical
to before (no retroactive roll), monotonicity works if you wired it, and both
oracles (G4) pass.

---

## Recipe 4 — Add a board expectation kind

**When to use.** You want board entries to be checkable by a new mechanism —
e.g. a `numeric_range` matcher, or a `tool_call_count` matcher — beyond the five
built-in kinds (`expected_text`, `regex`, `json_schema`, `predicate`, `rubric`).

**Files touched.**

| File | Symbol | Why |
|---|---|---|
| `src/zicato/core/board.py` | `ExpectationKind` | the wire token |
| `src/zicato/board/matchers.py` | `_eval_<name>`, `evaluate_expectation` | the matcher + dispatch |
| `src/zicato/board/predicates.py` (or `rubric.py`) | authoring factory | the operator-facing constructor |
| `tests/test_board_matchers.py` | test | pin it |

**Steps.**

1. **Add the enum member.** `ExpectationKind` is a `StrEnum`; a member equals its
   lowercase wire token and serializes with no converter. The board JSONL loader
   accepts exactly the tokens in this enum and rejects anything else:

   ```python
   # src/zicato/core/board.py — ExpectationKind
   class ExpectationKind(StrEnum):
       EXPECTED_TEXT = "expected_text"
       REGEX = "regex"
       JSON_SCHEMA = "json_schema"
       PREDICATE = "predicate"
       RUBRIC = "rubric"
   ```

   Add `NUMERIC_RANGE = "numeric_range"` (or your token).
2. **Write the matcher.** In `board/matchers.py`, write
   `def _eval_<name>(expectation, result, ...) -> ExpectationResult`. Return the
   uniform `ExpectationResult` (`kind`, `passed`, `detail`, optional `score` /
   `metrics`). Model it on `_eval_regex` (pure) or `_eval_json_schema` (parses
   the final output). If your matcher needs the auxiliary LLM, take
   `aux_call_llm` like `_eval_rubric` does — and read the collusion warning
   below.
3. **Add the dispatch arm.** `evaluate_expectation(expectation, result,
   aux_call_llm=None)` coerces `ExpectationKind(expectation.kind)` and branches
   to one matcher; it raises `ValueError` on an unknown kind. Add
   `if kind is ExpectationKind.NUMERIC_RANGE: return _eval_<name>(...)` to the
   branch.
4. **Add an authoring factory.** So operators can write the expectation, add a
   constructor in `board/predicates.py` (or `rubric.py`) that compiles down to a
   `core.Expectation` carrying your `kind` + its `spec` string. The board loader
   then accepts the new wire token automatically (via the enum).
5. **Test** in `tests/test_board_matchers.py`: one async test per branch — a
   passing case, a failing case, and (if scored) the score/metrics shape.

**Traps.**

- ⚠️ **`RUBRIC` — and any LLM-backed matcher — consumes the auxiliary callable,
  which MUST be distinct from the harness callable.** The collusion guard
  (`assert_distinct_callables`) exists so the thing being judged cannot also be
  the judge. If your matcher calls a model, it takes `aux_call_llm` (never the
  harness `call_llm`), exactly as `_eval_rubric` forwards to
  `zicato.board.rubric.evaluate_rubric_judge`. See 03-contract-and-epochs.md
  §"The board" for the judge-collusion contract.
- ⚠️ **An unknown kind must RAISE, not default.** `evaluate_expectation` raising
  `ValueError` on an unrecognized token is the safety property — a
  silently-skipped expectation is a board entry that scores nothing and passes
  vacuously. Do not add a fallthrough `else: return passed`.
- ⚠️ **The kind folds into the contract hash.** A board with your new expectation
  hashes differently — that is correct (the board is a contract input;
  03-contract-and-epochs.md §"The contract hash"). Confirm the JSONL round-trip
  (write → load) preserves the token so the hash is stable across a reload.

**Verify.**

```bash
uv run pytest tests/test_board_matchers.py tests/test_board_predicates.py \
    tests/test_board_jsonl.py -q
uv run pytest tests/test_epoch_contract.py -q      # board hash round-trips
uv run ruff check src/zicato/board/ && uv run mypy src/zicato/board/
```

**Definition of done.** An operator can author the expectation, the worker
evaluates it to a uniform `ExpectationResult`, an unknown token raises, the JSONL
round-trip is stable, and the two oracles (G4) pass.

---

## Recipe 5 — Add a goldfive drift-kind consumer

**When to use.** You want the scalar to weight a goldfive drift signal
differently — e.g. give a specific drift kind extra loss, or route a custom
judge's drift to a named per-judge weight. Drift kinds originate in goldfive; a
brand-new kind can be *weighted* with zero code change, so reach for this recipe
only when you need custom routing or a genuinely new wire kind.

**Files touched.**

| File | Symbol | Why |
|---|---|---|
| `src/zicato/core/scoring_config.py` | `per_kind_weights` / `per_judge_weights` | the weight (often the ONLY change) |
| `src/zicato/telemetry/reducer.py` | `_DRIFT_KIND_INT_TO_STR` | ONLY for a new wire-int kind |
| `src/zicato/core/drift_kinds.py` | the shared normalizer map | ONLY for a new wire-int kind |
| `tests/test_telemetry_reducer.py`, `tests/test_per_judge_loss_promotion.py` | tests | pin it |

**Steps.**

1. **First ask: is the kind already known?** If goldfive already emits your drift
   kind (it is in `_DRIFT_KIND_INT_TO_STR`), you weight it with **zero code
   change**: add its string key to `ScoringWeights.per_kind_weights`. The
   multiplier resolves through `_kind_multiplier(kind, weights)`:

   ```python
   # src/zicato/telemetry/reducer.py — _kind_multiplier
       is_custom, judge_name = split_judge_attributed_kind(kind)
       if is_custom:
           return weights.per_judge_weights.get(judge_name, weights.default_judge_weight)
       return weights.per_kind_weights.get(kind, 1.0)
   ```

   Stop here if a per-kind weight is all you need — the multiplier stacks
   multiplicatively with the severity weight, so your kind's contribution becomes
   `per_kind_weights[kind] × severity_weights[severity] × count`.
2. **For a custom-judge consumer, route via `per_judge_weights`.** A
   custom-judge drift arrives as the kind `"custom:<judge_name>"` (built by
   `_judge_attributed_kind`, split by `split_judge_attributed_kind`). Its loss is
   surfaced by `compute_per_judge_loss(drift_counts, weights)`, weighted by
   `per_judge_weights.get(name, default_judge_weight)`. To give a judge a
   distinct weight, set `per_judge_weights["<judge_name>"]`.
3. **Only for a genuinely NEW wire-int kind** (goldfive added an integer the map
   does not know): add an entry to `_DRIFT_KIND_INT_TO_STR` in `reducer.py` AND
   to the shared `zicato.core.drift_kinds` normalizer (`normalize_wire_drift_kind`
   / `_severity`). The two must agree — the reducer aliases the shared map.
4. **Where it lands.** Drift loss is computed by `compute_drift_loss(...)` →
   `resolve_drift_loss(DriftContext(... builtin_loss=builtin_drift_loss(...)))`
   (the killable-worker seam), and the top reducer `reduce_loss` folds it into a
   `LossProfile` alongside `per_judge_loss` and the `MetricCount` superset.
5. **Test** in `tests/test_telemetry_reducer.py` (the kind gets its multiplier)
   and `tests/test_per_judge_loss_promotion.py` (per-judge routing produces the
   expected `JudgeLoss` weight).

**Traps.**

- ⚠️ **The per-kind multiplier is duplicated on purpose.** `reducer.py` and
  `scoring/builtins.py` each carry a `_kind_multiplier` — the builtins copy is
  dependency-free because it runs on the seam-2 scalar path. If you change the
  multiplier's LOGIC (not just add a weight), change BOTH or the two seams
  disagree. Adding a weight to `per_kind_weights` touches neither copy — that is
  why zero-code weighting works.
- ⚠️ **A new wire-int kind added to only ONE of the two maps mis-normalizes.**
  The reducer's `_DRIFT_KIND_INT_TO_STR` and `core.drift_kinds` are a paired
  contract; a kind in one but not the other reads as `"custom"` (or raises) on
  the other path. Add to both in the same commit.
- ⚠️ **`per_judge_weights` changes are contract changes.** They roll the epoch
  (they shape the loss). Follow omit-at-default (Golden Rule G6) so an empty map
  is byte-identical to before.

**Verify.**

```bash
uv run pytest tests/test_telemetry_reducer.py tests/test_telemetry_reducer_metrics.py \
    tests/test_per_judge_loss_promotion.py tests/test_scoring_seams.py -q
```

**Definition of done.** The drift kind gets the intended multiplier (per-kind or
per-judge), a new wire-int kind is in both maps, the default empty-weights case
is byte-identical, and both oracles (G4) pass.

---

## Recipe 6 — Extend the deterministic example target

**When to use.** You want to add a new defect token + predicate to the
`target_0_convergence` example — usually to exercise a new board/scoring feature
end-to-end under the known-answer harness (no live LLM). This is the target the
convergence oracle (G4) runs.

**Files touched.**

| File | Symbol | Why |
|---|---|---|
| `examples/zicato_examples/target_0_convergence/harness.py` | `KNOWN_DEFECTS`, `synthesize_output` | the token + its effect |
| `.../agent/policy.py` | `STYLE_RULES` | the mutation point (operator only edits the seed) |
| `.../predicates.py` | a new predicate fn | the check |
| `.../board.jsonl` | a new entry | wire the predicate to a board unit |
| `.../scoring.json` **and** `.../scoring.effective.json` | both oracles | keep both honest |
| `.../mocks.py` | `_GAUNTLET_ROUNDS`, `_RACING_FIELD` | the deterministic proposer scripts |
| `tests/test_convergence_known_answer.py` | `_expected_scalar`, `EXPECTED_*` | re-derive the floor |

**Steps.**

1. **Add the defect token.** Append your token (e.g. `"passive-voice"`) to
   `KNOWN_DEFECTS` in `harness.py`. The current set is
   `("verbose-prose", "omit-summary", "skip-citations", "fabricate-metrics")`.
   The single mutation point is `STYLE_RULES` in `agent/policy.py` — a
   semicolon-separated token list; the proposer edits it by removing tokens.
2. **Map the token to an output effect.** In `synthesize_output`, add the
   branch that suppresses (or injects) the feature your token controls — mirror
   how `omit-summary` suppresses the `SUMMARY:` line. Each remaining token also
   emits one `drift_detected` frame (kind `UNEXPECTED_OUTPUT`, severity `INFO`
   → +1.0 loss under the contract).
3. **Add the predicate.** In `predicates.py`, write `def has_<feature>(result)
   -> bool` returning `True` when the feature is present (i.e. the token is
   absent). Model it on `has_summary` / `is_concise`. `predicates.py` is
   operator-owned — NOT a mutation point.
4. **Add the board entry.** Add a single-turn entry to `board.jsonl` with
   `expectation.kind = "predicate"` and `spec` pointing at your predicate (e.g.
   `predicates:has_<feature>`). This grows the board from N to N+1 entries.
5. **Re-derive the known-answer floor.** The scalar is
   `tokens_remaining + (1 − passes/N)`, hand-computable. Adding an Nth entry
   changes the pass-fraction denominator, and each remaining token still
   contributes +1.0 drift. Recompute `_expected_scalar` and the `EXPECTED_V0` /
   `V1` / `V2` / `FLOOR` constants in `tests/test_convergence_known_answer.py`
   (the current floor is `1.2`). Update the proposer scripts in `mocks.py`
   (`_GAUNTLET_ROUNDS`, `_RACING_FIELD`) if your token changes the promote/reject
   sequence.
6. **Update BOTH oracles honestly.** `scoring.json` is the gauntlet oracle;
   `scoring.effective.json` is the racing oracle (evidence gate on, field 4).
   Both drive the same target — if your change alters the board or scoring, edit
   both, and re-derive the expected sequence for each.

**Traps.**

- ⚠️ **Update BOTH oracles, honestly — never tune one to pass.** The gauntlet
  (`scoring.json`) and racing (`scoring.effective.json`) oracles are the repo's
  end-to-end truth anchors (Golden Rule G4). Changing the floor math and only
  fixing one, or nudging an `EXPECTED_*` constant until the test goes green
  without re-deriving it, deletes a measurement. Re-derive the floor from
  `scalar = tokens + (1 − passes/N)` and write down the arithmetic in the commit
  message — the way `RUN.md` narrates `v0=3.6 → v1=2.4 → v2=3.6(reject) →
  v3=1.2`.
- ⚠️ **`mocks.py` callables must stay module-level.** The proposer/aux callables
  are serialized as dotted paths and re-imported in the subprocess worker
  (Golden Rule G9; 12-bug-casebook.md §"module-level callables across the worker
  boundary"). A closure or a lambda cannot cross the boundary — keep them
  top-level functions and rewind counters via `reset`.
- ⚠️ **The floor must remain a strict, hand-computable number.** The value of
  this target is that its answer is known exactly. If your token makes the
  outcome depend on noise, you have moved it out of the Tier-1 known-answer class
  into the `NoisyPolicyAdapter` Tier-2 class — a different test.

**Verify.**

```bash
uv run pytest tests/test_convergence_known_answer.py -q     # the convergence oracle
uv run pytest tests/ -q -k "example or target_0"
bash tools/parity.sh --only MOCK-GOLDEN                     # the deterministic mock evolve golden
```

**Definition of done.** The new token drives its predicate, both oracles converge
on the re-derived floor, `mocks.py` callables are module-level, the MOCK-GOLDEN
gate is green, and the convergence oracle (G4) passes with the new arithmetic
documented in the commit.

---

## Recipe 7 — Add an index table / column

**When to use.** You want a new analytics fact queryable from the derived SQLite
index (`index.db`) — e.g. a new column on `generations`, or a whole new table.
Remember the doctrine first: the index is DERIVED. The fact must already exist in
a canonical file; the index row is a projection (07-runtime-and-durability.md
§"files canonical, index derived").

**Files touched.**

| File | Symbol | Why |
|---|---|---|
| `src/zicato/index/schema.py` | `SCHEMA_VERSION`, `_TABLE_STATEMENTS`, `_V<N>_ADDED_COLUMNS`, `_migrate_inplace` | schema + migration |
| `src/zicato/index/ingest.py` | `_upsert_<table>`, `rebuild_index` | populate it |
| `src/zicato/index/query.py` | selector + `_select_optional_columns` | read it back-compatibly |
| `tests/test_index_schema.py`, `tests/test_index_v<N>_schema.py` | tests | pin the column contract |

**Steps.**

1. **Bump `SCHEMA_VERSION`.** It is a module int in `schema.py` (currently `10`).
   Increment it. The schema is dual-stamped (`PRAGMA user_version` + a
   `schema_meta` row).
2. **Add to the fresh-build shape.** Edit the relevant `CREATE TABLE` in
   `_TABLE_STATEMENTS` (for a new column) or add a `CREATE TABLE IF NOT EXISTS`
   block (for a new table). This is the shape a brand-new database gets.
3. **Add to the incremental-open migration.** A pre-existing older database is
   migrated in place. Add a `_V<N>_ADDED_COLUMNS` tuple and a new `if current <
   N:` block in `_migrate_inplace` that `ALTER TABLE … ADD COLUMN`s it (additive
   only — never drop/rename). The v10 wave is the model:

   ```python
   # src/zicato/index/schema.py — the v10 column-add tuple
   _V10_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
       ("generations", "elo", "REAL"),
       ("generations", "elo_games", "INTEGER"),
   )
   ```

   The migration block is idempotent (guarded by `_column_names`), so a
   half-applied open is safe to re-run.
4. **Populate it in ingest.** Teach the upsert writer (`_upsert_<table>` in
   `ingest.py`) to read the value off the canonical record and add it to the
   `INSERT … ON CONFLICT DO UPDATE` column list — `COALESCE` for a nullable
   provenance column so a re-ingest never nulls a set value. For a
   derived-analytics column, follow the `_fold_elo` precedent (a post-ingest
   fold run by `rebuild_index`).
5. **Read it back-compatibly.** In `query.py`, expose the column through
   `_select_optional_columns`, which emits `NULL AS <col>` when the column is
   absent — so a LEGACY index (built before your bump) still loads. Never `SELECT
   <col>` directly from a table that a legacy index lacks.
6. **Re-capture the goldens.** Update the exact column-contract lists in
   `tests/test_index_schema.py` and add a `tests/test_index_v<N>_schema.py`
   mirroring `test_index_v10_schema.py` (fresh-build columns + a migration test
   from the prior version). Re-run the REINDEX-DUMP parity gate.

**Traps.**

- ⚠️ **Miss step 2 or step 3 and the fresh vs migrated schemas diverge.** A
  column added to `_TABLE_STATEMENTS` but not to a migration block means a
  freshly-built index has it and an upgraded one does not (and vice-versa). Both
  paths must reach the same shape; `test_index_v<N>_schema.py` tests both a fresh
  build AND a migration, precisely to catch this.
- ⚠️ **The reader refuses a NEWER index; never re-stamp it down.** `apply_schema`
  raises `IndexSchemaNewerError` when the on-disk `user_version` exceeds
  `SCHEMA_VERSION` — invariant D12 (07-runtime-and-durability.md §"refuse-on-
  newer"). This is why the index is safe to throw away:

  ```python
  # src/zicato/index/schema.py — IndexSchemaNewerError (why the refusal exists)
  class IndexSchemaNewerError(RuntimeError):
      """The index database was written by a NEWER zicato than this build.

      Raised by :func:`apply_schema` when ``PRAGMA user_version`` exceeds
      :data:`SCHEMA_VERSION`: an older writer must never silently re-stamp
      a newer database DOWN ...
      """
  ```

  An operator on an older build gets a clear refusal, not silent
  misinterpretation. Do not add a down-migration — the recovery is `zicato
  reindex`, not a schema downgrade.
- ⚠️ **A row with no canonical file source vanishes on `zicato reindex`.** The
  index is derived (invariant D1); if your column has no file to re-derive from,
  it disappears on the next rebuild and the vanish looks like someone else's bug.
  Land the fact in a canonical record first.

**Verify.**

```bash
uv run pytest tests/test_index_schema.py tests/test_index_v10_schema.py \
    tests/test_index_ingest.py tests/test_index_query.py -q
# add + run your new tests/test_index_v<N>_schema.py
bash tools/parity.sh --only REINDEX-DUMP        # the rebuilt-index golden
```

**Definition of done.** A new database and an upgraded one reach the same schema,
the column is populated by ingest from a canonical record, a legacy index still
loads (optional-column read), the REINDEX-DUMP golden is re-captured, and the two
oracles (G4) pass.

---

## Recipe 8 — Add an epoch-open step

**When to use.** You want the loop to compute something ONCE when an epoch opens
and record it on the epoch — e.g. a calibration measurement, a pre-flight probe —
WITHOUT it becoming a contract input (so it never rolls the epoch). The precedent
is the contract pre-flight and the noise-floor calibration.

**Files touched.**

| File | Symbol | Why |
|---|---|---|
| `src/zicato/epoch/<your_step>.py` | your measurement fn | the computation |
| `src/zicato/orchestrator.py` | `_maybe_<your_step>` | the gated, best-effort, once-per-epoch call |
| `src/zicato/epoch/lifecycle.py` | `set_epoch_<field>`, `_config_to_dict`, `_config_from_dict` | persist the never-hashed field |
| `tests/test_<your_step>.py` | test | pin "never rolls" |

**Steps.**

1. **Write the measurement.** A pure-ish async function in `epoch/` that computes
   the datum from the champion snapshot + board (never mutating real lineage —
   the pre-flight degrades the FIRST mutation point in an ephemeral
   `TemporaryDirectory`). Return a JSON-able report. If it draws replicates, use
   a RESERVED replicate base so its draws never collide with duels/calibration/
   screening/evidence (`PREFLIGHT_REPLICATE_BASE = 2000` is the precedent; the
   ledger is 06-tournament-and-selection.md §"The reserved replicate ladder").
2. **Add the additive, never-hashed epoch field.** In `lifecycle.py`, add a
   `set_epoch_<field>` that loads the epoch config, `replace(cfg, <field>=…)`,
   and writes it back — mirroring `set_epoch_preflight`. Thread the field through
   `_config_to_dict` / `_config_from_dict` with a default. The docstring must
   state the invariant plainly:

   ```python
   # src/zicato/epoch/lifecycle.py — set_epoch_preflight (the precedent)
       The verdict is a RUNTIME measurement,
       never a contract input — writing it does not touch ``contract_hash``
       and never rolls the epoch. Re-running overwrites the prior record.
   ```
3. **Gate + call it once at epoch open.** In `orchestrator.py`, add
   `_maybe_<your_step>(...)` beside `_maybe_contract_preflight` /
   `_maybe_calibrate_noise_floor`, wrapped in `best_effort(...)`. Gate on a
   `config.json` knob (e.g. `workspace_config.get("<your_step>")`); return early
   if unset, malformed, or if the epoch field is ALREADY set (fire exactly once
   per epoch). Call it from `evolve_once`'s epoch-open sequence.
4. **Test the never-rolls property.** In `tests/test_<your_step>.py`, capture the
   epoch's `contract_hash` before, call `set_epoch_<field>`, reload, and assert
   the hash is UNCHANGED — the exact pattern `tests/test_contract_preflight.py`
   uses.

**Traps.**

- ⚠️ **The persisted field must NEVER be a contract input, or every epoch rolls.**
  `contract_hash` is computed only in `new_epoch` from `ContractInputs`
  (`board`, `brief`, `scoring`, `entrypoint`, `mutable_trees`, `proposer`) —
  your field is NOT among them. Add it as an *additive* config field written by
  `set_epoch_<field>`, and pin the hash-unchanged assertion. If your datum ever
  DOES belong in the hash, it is not an epoch-open measurement — it is a contract
  component, and it rolls the epoch (03-contract-and-epochs.md §"The contract
  hash").
- ⚠️ **Fire exactly once per epoch, and best-effort.** The gate checks the field
  is unset before measuring, so a resumed or re-entered epoch does not re-measure
  (and re-spend the budget). Wrap it in `best_effort` — a measurement failure
  must never fail the round (invariant D11-style discipline).
- ⚠️ **Do not persist a DRAFT measurement as the live epoch's.** Contrast the
  builder's `preflight` (10-builder-cli-library.md §"The statistical pre-flight"),
  which measures a draft and is recommend-only, NEVER persisted — because the
  draft is not the live contract. The epoch-open step persists BECAUSE it
  measures the live epoch's own champion.

**Verify.**

```bash
uv run pytest tests/test_contract_preflight.py tests/test_<your_step>.py \
    tests/test_epoch_lifecycle.py -q
uv run pytest tests/test_epoch_contract.py -q      # the field did not enter the hash
```

**Definition of done.** The step computes once per epoch, gated by a config knob,
best-effort, persisted on the epoch record, provably never in the contract hash
(hash-unchanged test), and the two oracles (G4) pass.

---

## Recipe 9 — Touch the orchestrator safely

**When to use.** Your change adds or edits a per-round step in the evolve loop —
proposing, finalizing a generation, the end-of-round tail, field minting, or
override application. The orchestrator is the riskiest file in the tree; this
recipe is the seam map that keeps a change from landing on one pipeline only.

**Files touched.**

| Seam (in `src/zicato/orchestrator.py`) | Owns | RoundLog duty |
|---|---|---|
| `_propose_child` | builds the single `ProposerContext` both pipelines share; calls `proposer_agent.propose`; stamps `round_index` | emits `proposal_attempted`, `experiment_minted`, `patches_applied` (via `round_emitter`) |
| `_finalize_generation` | the ONE write pipeline every round tail flows through (outcome + index dual-write + lineage + journal) | emits NOTHING itself — the caller emits `decision_recorded` / `round_closed` |
| `_round_epilogue` | the shared end-of-round tail (health assessment + decision analyzer + report regen) | no direct emission |
| `_mint_challenger_field` | PURE — the field-diversity accept/soft-reject decision (`_FieldMintDecision`) | none (I/O-free) |
| `_apply_field_overrides` | PURE — re-resolves field crowning under operator overrides | none (provenance goes to the field record) |

**Steps.**

1. **Find the seam that owns your concern** from the table above. A new propose
   input goes on `_propose_child`; a new write-tail step goes in
   `_finalize_generation`; a new end-of-round side effect goes in
   `_round_epilogue`; a change to field accept/reject goes in the PURE
   `_mint_challenger_field`; an override rule goes in the PURE
   `_apply_field_overrides`.
2. **Edit the seam, never inline the logic into a pipeline.** These are extracted
   shared helpers precisely so a step "can never land on one pipeline only" — the
   gauntlet and the field paths BOTH call them. Inlining your step into one path
   is the exact shape of bugs #6 and #7 (12-bug-casebook.md §"Case 6" / §"Case
   7"): the gauntlet got a fix the field path did not.
3. **Preserve the monkeypatch names.** The N-round loop lives in
   `zicato.evolve.loop` and resolves these collaborators through the module
   object at call time so the test suite's monkeypatches keep biting. The
   `__all__` block says so explicitly:

   ```python
   # src/zicato/orchestrator.py — __all__ (excerpt)
       # Collaborators the N-round loop (``zicato.evolve.loop``) resolves
       # through THIS module object at call time so the test suite's
       # monkeypatches keep biting. ... All must stay
       # attributes of this module and reachable as exports for the loop's
       # ``_orch.<name>`` access.
   ```

   Do not rename a seam, and do not turn a module-level function into a method or
   a closure — a test that does `orch._mint_challenger_field = fake` must keep
   working.
4. **Emit the right RoundLog events at the right seam.** Follow the duty column:
   `_propose_child` emits the propose events; the DECISION site (the caller of
   `_finalize_generation`, e.g. `_persist_rejected_round`) emits
   `decision_recorded` / `round_closed`. Emission is best-effort (invariant D11)
   — compute the payload OUTSIDE any `getattr` chain that could throw, and never
   let emission fail the round (07-runtime-and-durability.md §"Emission is
   best-effort").
5. **Keep the pure seams pure.** `_mint_challenger_field` and
   `_apply_field_overrides` return decisions (`_FieldMintDecision`, a tuple) and
   do NO I/O — that is what makes them unit-testable without a workspace. If your
   change needs to WRITE, it belongs in the caller, not the pure seam.
6. **Test at the seam.** `tests/test_orchestrator_decomposition.py` monkeypatch-
   calls the pure seams by name; add your case there. For the I/O seams, the two
   oracles (G4) exercise the full loop — a seam regression turns one of them red.

**Traps.**

- ⚠️ **Inlining a step into one pipeline is the bug #6/#7 shape.** The whole
  reason these seams exist is that a best-of-N tree fix landed on the gauntlet
  and not the field path, mounting the wrong child tree (12-bug-casebook.md
  §"Case 6" and §"Case 7"). Edit the shared seam; if the gauntlet and field
  paths genuinely need different behaviour, branch INSIDE the seam, visibly.
- ⚠️ **Renaming a seam silently breaks the loop's monkeypatch resolution.** The
  loop does `_orch.<name>`; a rename that misses `__all__` (or turns the function
  into something un-monkeypatchable) means the loop calls the real function while
  a test thinks it patched it — a green test over untested code.
- ⚠️ **RoundLog emission must never raise.** A round is authoritative in the
  canonical stores; the RoundLog is a durable trace, emitted best-effort
  (invariant D11). A `getattr` in the payload that throws would fail the round
  through the emit path — mirror the defensive payload shapes already in place.

**Verify.**

```bash
uv run pytest tests/test_orchestrator_decomposition.py tests/test_round_log_emission.py -q
uv run pytest tests/test_convergence_known_answer.py -q     # the full loop — seam regressions surface here
uv run mypy src/zicato/orchestrator.py
```

**Definition of done.** The step lives on the correct shared seam (never inlined
into one pipeline), seam names are preserved for monkeypatching, RoundLog duties
are honored best-effort, the pure seams stayed pure, and both oracles (G4) pass.

---

## Recipe 10 — Run the full local verification ladder

**When to use.** Before you propose ANY commit. This is the pre-commit checklist
as a standalone, copy-pasteable ladder — run it top to bottom; each rung catches
a different class of break.

**Files touched.** None — this recipe runs checks, it does not edit.

**Steps.**

1. **Sync the environment — with all extras.** A bare `uv sync` deletes dev
   tooling (`pytest`, `mypy`, `ruff`, even `uv` itself) from `.venv`. ALWAYS:

   ```bash
   uv sync --all-extras
   ```

2. **Lint + format.** Ruff is pinned to the exact version the pre-commit hook
   uses, so local and hook never diverge:

   ```bash
   uv run ruff check .
   uv run ruff format --check .
   ```

3. **Typecheck.** mypy is strict; the error count is itself a parity gate (it
   must not get worse):

   ```bash
   uv run mypy src/zicato/
   ```

4. **Import contracts.** The five library/driver contracts (10-builder-cli-
   library.md §"The five import-linter contracts"):

   ```bash
   uv run lint-imports
   ```

5. **The two oracles (Golden Rule G4) — no matter how unrelated your change
   feels:**

   ```bash
   uv run pytest tests/test_convergence_known_answer.py -q
   uv run pytest tests/test_decision_procedure_power.py -q
   ```

6. **The full suite.** Fans out across cores via `pytest-xdist` (`-n auto` is the
   default; the `node` shim is excluded from the in-pytest run):

   ```bash
   uv run pytest -q
   ```

7. **The parity gates + the node suite (Golden Rule G5):**

   ```bash
   bash tools/parity.sh        # six gates: PYTEST, CONTRACT-HASH, CLI-HELP, REINDEX-DUMP, MOCK-GOLDEN, MYPY
   make node-test              # the dashboard JS behaviour suite
   ```

8. **The supervisor, if you touched the Rust crate:**

   ```bash
   cargo test -p zicato-supervisor
   ```

9. **Or run the whole thing in one target.** `make check` is `lint import-lint
   typecheck test node-test`; the git pre-commit hook additionally runs
   `end-of-file-fixer`, `trailing-whitespace`, `ruff`, and `ruff-format` on
   changed files:

   ```bash
   make check
   uv run pre-commit run --all-files
   ```

**Traps.**

- ⚠️ **`uv sync` without `--all-extras` silently removes your test tools.** The
  next `uv run pytest` fails with a mysterious import error; the fix is to
  re-sync WITH `--all-extras` (Golden Rule G2; 01-orientation.md §"Set up"). Make
  it muscle memory.
- ⚠️ **A green unit-test run hides an integration break.** Steps 5 and 7 exist
  because unit tests pass while the loop is broken — the next contributor bisects
  YOUR commit out of a red oracle or a red parity gate. Never skip the oracles
  because "my change is unrelated."
- ⚠️ **Never start a live model run to "verify."** Verification is the test suite
  and the deterministic gates — the two oracles run the full loop with mock
  callables, no live LLM. A live `zicato evolve` requires the operator's explicit
  go-ahead (Golden Rule G3); it is never part of your local ladder.

**Verify.** The ladder IS the verification. Green top to bottom = ready to
propose a commit.

**Definition of done.** `uv run ruff check .`, `uv run mypy src/zicato/`,
`uv run lint-imports`, both oracles, the full pytest suite, `bash
tools/parity.sh`, and `make node-test` are all green — and, if the Rust crate
changed, `cargo test -p zicato-supervisor`.

---

## Recipe 11 — Investigate a red parity gate

**When to use.** `bash tools/parity.sh` reports a red gate and you need to decide:
did I legitimately change the captured surface (update the golden) or did I break
it (fix the code)? This is a decision tree, not a "re-capture and move on."

**Files touched.** Depends on the verdict — either the code you changed, or the
golden under `tools/parity/golden/` (only after you have justified the update).

**Steps.**

1. **Read which gate is red, and map it to your change class:**

   | Red gate | Captures | A legitimate red means you changed… |
   |---|---|---|
   | `CONTRACT-HASH` | the epoch contract hash (+ per-component hashes) | a contract component (board / brief / scoring / proposer / entrypoint) — the hash SHOULD move |
   | `CLI-HELP` | `zicato --help` + every subcommand `--help` | a command, flag, default, or help string |
   | `REINDEX-DUMP` | the SQLite index rebuilt from a fixture workspace | the index schema or an ingest projection |
   | `MOCK-GOLDEN` | a deterministic no-live-LLM racing mock evolve | the loop's decision path, event ordering, or scoring |
   | `MYPY` | the mypy error count vs. the committed baseline | types (the count must not get WORSE) |
   | `PYTEST` | the full suite | anything |

2. **Decide legitimate-update vs regression.** Ask: *did I intend to change this
   surface?* If you edited scoring and `CONTRACT-HASH` moved — legitimate, the
   hash is supposed to track the contract. If you edited an UNRELATED file and
   `CONTRACT-HASH` moved — regression; something leaked into the hash. The gate
   is INFORMATION: a red on a surface you did not mean to touch is the gate doing
   its job.
3. **For a legitimate update, re-capture — deliberately.** The goldens are
   normalized (timestamps → `<TS>`, tmp paths and date-prefixed epoch ids masked
   by `tools/parity/lib/normalize.py`) so only real content diffs. Re-capture:

   ```bash
   ZICATO_PARITY_UPDATE=1 bash tools/parity.sh --only <GATE>   # re-capture that golden
   git diff tools/parity/golden/                                # READ the diff before staging
   ```

   Read the diff and confirm every changed line is a change you intended. Stage
   the golden and say WHY in the commit message.
4. **For a regression, fix the code — never the golden.** If the diff shows a
   surface you did not mean to change, the golden is right and your code is
   wrong. Revert the leak; do not re-capture to make it pass.
5. **For `CLI-HELP`, also reconcile `CLI.md`.** A legitimate CLI change updates
   both the help golden AND the hand-reconciled `docs/design/CLI.md`
   (10-builder-cli-library.md §"CLI.md is a GENERATED artifact").

**Traps.**

- ⚠️ **Re-capturing a golden you did not read is how a regression ships green.**
  `ZICATO_PARITY_UPDATE=1` makes ANY red go green — that is the danger. Always
  `git diff tools/parity/golden/` and confirm each line. A blind re-capture
  converts a caught regression into a committed one.
- ⚠️ **`MYPY` red means the error count got WORSE, not that mypy is clean.** The
  gate is a ratchet against the committed baseline. Do not "fix" it by adding
  `# type: ignore` unless the ignore is genuinely warranted (and `warn_unused_
  ignores` will flag a stale one).
- ⚠️ **A moved statistical golden is the same trap as a widened test bound.** If
  `MOCK-GOLDEN` shows a decision that flipped, the loop's BEHAVIOUR changed —
  either your change is wrong, or the new behaviour is honest and the commit
  documents it (the same discipline as Recipe 13; 12-bug-casebook.md §"Case 3"
  is the cautionary A/A false-zero-floor tale of a pinned number that was wrong).

**Verify.**

```bash
bash tools/parity.sh --only <GATE>      # green after a justified re-capture or a fix
git diff tools/parity/golden/           # every changed line intended
```

**Definition of done.** The gate is green because you either fixed a real
regression or re-captured a golden whose diff you read and justified in the commit
message — never because you blindly re-captured.

---

## Recipe 12 — Debug a failing tournament e2e

**When to use.** A tournament e2e (`tests/test_convergence_known_answer.py`,
`tests/test_gauntlet_evidence_gate_e2e.py`, or a live run) failed or hung, and you
need to read the forensic trail a round leaves on disk.

**Files touched.** None — you READ the workspace's artifacts. (The failing test
leaves its `tmp_path` workspace; a live run leaves `.zicato/`.)

**The forensic file map** — every artifact a round leaves, in one place:

| Artifact | Path (relative to workspace root) | What it tells you |
|---|---|---|
| Round log | `epochs/{epoch}/rounds/{round}/round_log.jsonl` | the decision trail (typed events, gap-free `seq`) |
| Per-run loss | `runs/<entry>/loss.json` (the canonical `r0` slot) | the scalar for one board unit |
| Replicate losses | the unit cache under RESERVED bases (`_unit_loss_path`) | replicates >0 (r0 = the canonical path) |
| Heartbeat | `.zicato/runtime/heartbeat.json` | the live PHASE (`proposing` / `screening:r{n}` / `tournament:…` / `holdout` / `gate`) |
| Active runs | `.zicato/runtime/active_runs/{run_id}.json` | in-flight runs; one present-but-unsettled = a worker that never returned |
| Worker args | a temp file (`python -m zicato._tournament_worker <args-file>`) | the worker spec + `config_pins` (ephemeral — cleaned in a `finally`) |
| Journal + lineage | `epochs/{epoch}/journal…`, `lineage.json` | only OUTCOMED generations (invariant D8) |
| Health report | `epochs/{epoch}/health/round_{N}.json` | the per-round `LoopHealth` findings |

**Steps.**

1. **Read the round log first — it is the durable store-of-record for a round.**
   Path convention: `epochs/{epoch}/rounds/{round}/round_log.jsonl` (constructed
   by `round_log_path`). It is an append-only JSONL of typed events with a
   gap-free `seq`; fold it with `zicato.epoch.round_log.fold_round_record`. The
   event vocabulary (`EVENT_TYPES`): `RoundOpened`, `ProposalAttempted`,
   `CandidateSampled`, `CandidateScreened`, `CritiqueSelected`,
   `ExperimentMinted`, `PatchesApplied`, `ValidationFailed`, `UnitCompleted`,
   `GateEvaluated`, `HoldoutReleased`, `EvidenceReplicated`, `DecisionRecorded`,
   `RoundClosed`. A round that never reached `DecisionRecorded` tells you where
   it stalled.
2. **Read the per-run loss files.** Each board unit writes `runs/<entry>/loss.json`
   — this is the CANONICAL replicate-0 (`r0`) slot. Replicates >0 live in the
   unit cache under RESERVED bases (`_unit_loss_path`); `r0` maps to the
   canonical path. A wrong scalar traces back to a specific `loss.json`.
3. **Read the heartbeat for the phase it died in.** `.zicato/runtime/heartbeat.json`
   carries the live phase string — `proposing`,
   `proposing:round_{n}:{next_id}`, `screening:r{n}`,
   `tournament:round_{n}:{matchup_id}`, `holdout`, `gate`. A hang's heartbeat
   phase names the step that wedged.
4. **Read `active_runs/` for in-flight runs.** `.zicato/runtime/active_runs/{run_id}.json`
   records each per-run state; a run present here but never settled is a worker
   that did not return.
5. **Inspect the worker args file for the spawn.** The runner spawns `python -m
   zicato._tournament_worker <args-file>`; the args file carries the worker spec
   and the `config_pins` (10-builder-cli-library.md §"Flags cross the worker
   boundary via config_pins"). It is a TEMP file cleaned up in a `finally`, so
   capture it during a hang (or from a crash that skipped cleanup) — it tells you
   exactly what the worker was told to run.
6. **Cross-check the journal + lineage.** `journal` and `lineage.json` record only
   OUTCOMED generations (invariant D8); a generation in the round log but absent
   from lineage was never given an outcome — which on resume is discarded safely,
   and in a debug is the un-finished generation.

**Traps.**

- ⚠️ **An absent round-log event does NOT mean the step did not happen.** RoundLog
  emission is best-effort (invariant D11; 07-runtime-and-durability.md §"Emission
  is best-effort") — the canonical stores (`loss.json`, the journal, lineage)
  stay authoritative. Corroborate a missing event against the canonical files
  before concluding the step was skipped.
- ⚠️ **`runs/<entry>/loss.json` is the canonical `r0` slot — do not confuse it
  with a replicate.** Replicate 0 IS the canonical run; replicates >0 live under
  reserved bases in the unit cache. A tool that overwrites `loss.json` with a
  replicate's bytes is exactly the replicate-cache clobber (12-bug-casebook.md
  §"Case 1"). When reading, treat `loss.json` as `r0`, not "the latest run."
- ⚠️ **A hung run may be a killed CONCURRENT process, not your loop.** The reaper
  kills by process-group; a test-suite reaper `killpg`-ing a concurrently-running
  evolve looks like a hang in the wrong process (12-bug-casebook.md §"Case 5").
  Confirm the pid/start-time identity (invariant D9) before blaming the loop
  under test.

**Verify.**

```bash
# re-run the failing e2e with the workspace preserved:
uv run pytest tests/test_convergence_known_answer.py -q -x
# fold a preserved round log to read the decision trail (RoundLog takes
# workspace_root, epoch_id, round_index; .read() -> events; fold -> RoundRecord):
uv run python -c "from zicato.epoch.round_log import RoundLog, fold_round_record; \
print(fold_round_record(RoundLog('WS','EPOCH',0).read()))"
```

**Definition of done.** You located the failing round from `round_log.jsonl`,
confirmed the scalar against the canonical `loss.json`, identified the wedged
phase from the heartbeat, and distinguished a real loop failure from a
concurrent-process or best-effort-emission artifact.

---

## Recipe 13 — Safely bump a pinned OC number

**When to use.** A change to the decision procedure (the margin gate, replication,
the Bradley–Terry pre-gate, the screen) moves a pinned **operating characteristic**
— a false-promotion rate, a power number, a CI-separation requirement — and you
need to change the number the power harness asserts. These numbers are the repo's
statistical truth; changing one is a deliberate, documented act.

**Files touched.**

| File | Symbol | Why |
|---|---|---|
| the procedure module (e.g. `src/zicato/selection/…`) | your change | the behaviour that moved |
| `tests/test_decision_procedure_power.py` | the pinned bound / `EXPECTED_*` | the OC number |
| `tests/test_convergence_known_answer.py` | `EXPECTED_*` (if the loop outcome moved) | the known-answer oracle |
| the commit message | the justification | the audit trail |

**Steps.**

1. **State the claim quantitatively BEFORE you touch the number.** "The new X
   reduces false promotions under the A/A null from A to B at σ=0.22 without
   reducing power at the 1× planted delta by more than C." If you cannot phrase
   it this way, you are not ready — see 04-evaluation-statistics.md §"Recipe:
   proving a change to the decision procedure" for the full protocol.
2. **Measure, do not guess.** The power harness (`tests/test_decision_procedure_
   power.py`) runs deterministic seeded trials — an A/A null world and planted
   deltas at 0.5×/1×/3× the floor. Run it and READ the printed rates; the new
   number is the one the harness MEASURES, not the one you hoped for.
3. **Keep the failing alternative visible.** An OC test documents the rule it
   replaces by computing the naive rule's rate on the IDENTICAL seeded draws
   (04-evaluation-statistics.md §"Operating characteristics as pinned tests").
   Preserve that comparison — the pin is "rule A beats rule B on these draws," not
   "rule A hits a bound."
4. **Change the number and the bound together, tightly.** Pin acceptance bounds
   loose enough to survive re-seeding but tight enough to catch a regression.
   Never widen a bound just to make a flaky-looking test pass (see Traps).
5. **Re-run the whole power file AND the convergence oracle.** Your change must
   leave EVERY other pinned number standing. If a second number moved, the commit
   message must say exactly which and why that is honest — the `eb55266` commit
   (updating "budget 48 → confirmed" expectations) is the model.
6. **Document the move in the commit message.** Name the old number, the new
   number, the measured rates, and the reason. A pinned-number change with no
   justification is indistinguishable from a silently-widened bound.

**Traps.**

- ⚠️ **Never "stabilize" a statistical test by widening its bound until it
  passes.** These tests are deterministic given their seeds — if a rate moved,
  the PROCEDURE's behaviour moved, and the correct responses are (a) your change
  is wrong, or (b) the new rate is the honest new characteristic and the commit
  documents it. A silently widened bound is a DELETED measurement
  (04-evaluation-statistics.md §"Operating characteristics as pinned tests").
- ⚠️ **A pinned number can be WRONG — that is exactly bug #3.** The A/A
  calibration once pinned a false-zero floor because a replicate index never
  reached the harness (12-bug-casebook.md §"Case 3"). If a number looks too good
  (a zero floor, a perfect rate), suspect the measurement before you pin it — an
  OC that cannot be wrong is not measuring anything.
- ⚠️ **If your change touches persistence or replicate indices, add a
  slot-integrity test.** Prove the canonical `r0` bytes are unchanged and your
  draws land under your RESERVED base for every side (the
  `test_full_mode_evidence_loop_never_touches_canonical_slots` pattern) — this is
  the bug #1 / bug #8 guard (12-bug-casebook.md §"Case 1", §"Case 8").

**Verify.**

```bash
uv run pytest tests/test_decision_procedure_power.py tests/test_convergence_known_answer.py -q
git log -1 --format='%B'      # the commit message NAMES the moved number and why
```

**Definition of done.** The moved OC number is the one the power harness measures
(not a guess), every other pinned number still stands, the failing alternative is
still shown hot on the same draws, and the commit message documents the old→new
change and its justification.

---

## Recipe 14 — Add a `skills/` entry

**When to use.** You want to package an operator workflow as a reusable skill —
e.g. `zicato-tune-holdout` — so an agent can load focused instructions for that
task. Skills are the operator-facing counterpart to this guide's recipes.

**Files touched.**

| File | Why |
|---|---|
| `skills/zicato-<verb-noun>/SKILL.md` | the skill itself |
| `skills/README.md` | the catalog row |

**Steps.**

1. **Name it `zicato-<verb-noun>`.** Every skill follows this convention
   (`zicato-tune-scoring`, `zicato-diagnose-health`, `zicato-write-brief`). Create
   the directory `skills/zicato-<verb-noun>/`.
2. **Write `SKILL.md` with two-field YAML frontmatter.** Exactly `name` (matching
   the directory) and `description` (a single long line: what it does + WHEN to
   use it + a load-bearing invariant). The `zicato-tune-scoring` frontmatter is
   the model:

   ```
   ---
   name: zicato-tune-scoring
   description: Edit a zicato scoring.json — drift-loss weights, per_judge_weights/default_judge_weight, severity and per-kind weights, the declarative transform registry (pass_transform / drift_kind_aggregation), the dotted-spec scalar_fn / drift_reducer plugins, and the promotion gate (promote_margin + pass_rate_monotonicity). Use when calibrating how generations are scored or when tournament decisions disagree with operator intuition. Lower scalar = better.
   ---
   ```

   Note how the `description` ends with an invariant (`Lower scalar = better`);
   `zicato-evolve` ends with `ENFORCES the live-run gate`. That trailing invariant
   is what lets an agent pick the right skill and obey its guardrail.
3. **Write the body.** After the frontmatter: an H1 title, prose instructions,
   tables of keys/flags, code blocks, and a closing `## Reference` list linking
   the relevant `docs/design/*.md` and sibling skills. Copy the structure of
   `skills/zicato-tune-scoring/SKILL.md` (248 lines) or the shorter
   `skills/zicato-evolve/SKILL.md` (127 lines).
4. **Add the catalog row.** In `skills/README.md`, add a
   `| zicato-<verb-noun> | What it does |` row under the correct Tier table (Tier
   0 Foundations … Tier 6 Strategy).
5. **Optional helper scripts** live alongside `SKILL.md` in the directory.

**Traps.**

- ⚠️ **No vendor names, anywhere — Golden Rule G1.** Nothing in git references the
  model vendor. A skill body that names a model provider (or an attribution
  trailer in your commit) violates the durable repo rule (01-orientation.md
  §"G1 — The vendor rule"). Write model-agnostic instructions.
- ⚠️ **`name:` must match the directory exactly.** The loader keys on the
  frontmatter `name`; a mismatch with the directory name makes the skill
  unresolvable.
- ⚠️ **The `description` is the router — make it a single line with a WHEN and an
  invariant.** A vague description means an agent never selects the skill (or
  selects it wrongly). State the trigger ("Use when …") and the guardrail, as the
  exemplars do.

**Verify.**

```bash
# frontmatter sanity — name matches the directory, description is one line:
head -4 skills/zicato-<verb-noun>/SKILL.md
grep -n 'zicato-<verb-noun>' skills/README.md          # the catalog row exists
# G1 vendor scan — the skill must name no model vendor (01-orientation.md §"G1").
# The pattern is assembled at runtime so this guide never spells the stems:
pat="$(printf 'c%s|a%s' 'laude' 'nthropic')"
grep -rilE "$pat" skills/zicato-<verb-noun>/           # must print nothing
```

**Definition of done.** The skill directory holds a `SKILL.md` whose `name`
matches the directory and whose one-line `description` states what/when/invariant,
the body ends with a `## Reference` block, the catalog row is in `skills/README.md`,
and nothing references a model vendor.

---

## 13.15 Cross-references

Each recipe's owning chapter carries the theory the recipe applies:

- 02-architecture.md §"The evolve round" — the pipeline the orchestrator seams
  (Recipe 9) sit in; the four processes Recipe 12's forensics span.
- 03-contract-and-epochs.md §"The contract hash" / §"Omit-at-default fields" —
  what rolls the epoch (Recipes 3, 4, 8); §"The board" — the judge-collusion
  contract (Recipe 4).
- 04-evaluation-statistics.md §"The scoring seams" (Recipe 3, 5), §"The promote
  gate" (Recipe 3 monotonicity), §"Operating characteristics as pinned tests" +
  §"Recipe: proving a change to the decision procedure" (Recipe 13).
- 05-proposer.md §"The restricted-visibility envelope" + §"The channel-author's
  checklist" — the banding Recipe 2 must satisfy.
- 06-tournament-and-selection.md §"The reserved replicate ladder" — the base
  ledger Recipes 8, 13 draw on (duels 0.., calibration 1000, preflight 2000,
  screening 3000/3001, evidence 4000).
- 07-runtime-and-durability.md §"files canonical, index derived" + §"refuse-on-
  newer" (Recipe 7), §"Emission is best-effort" (Recipes 9, 12), §"RoundLog"
  (Recipe 12).
- 08-supervisor.md §"The health surface" — the consumer of Recipe 1's detector.
- 10-builder-cli-library.md §"The op inventory" (Recipe 3's builder surface),
  §"CLI.md is a GENERATED artifact" (Recipe 11), §"Flags cross the worker
  boundary via config_pins" (Recipe 12).
- 11-testing.md §"parity gates" (Recipes 10, 11), §"Node suite conventions"
  (Recipe 10), §"The two oracles" (every recipe's finish line).
- 12-bug-casebook.md — Case 1 (replicate-cache clobber; Recipes 12, 13), Case 3
  (A/A false-zero floor; Recipes 11, 13), Case 5 (reaper killpg; Recipe 12),
  Cases 6 & 7 (tree/field mismatch; Recipe 9), Case 8 (evidence replicate reuse;
  Recipe 13), Case 10 (contract-hash cwd/checkout; Recipe 8's never-hashed
  discipline).
