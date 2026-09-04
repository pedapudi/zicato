# 10 — The Builder, the CLI, and the Library boundary

> **Covers.** The drivers that sit on top of the zicato library, and the import
> boundary between them and it. Four subjects: (1) the **builder** — the
> contract IDE, a deterministic draft/ops/dispatch/copilot backend that the form
> GUI and the chat copilot both drive through ONE mutation surface, plus its
> honest cost meter, its statistical pre-flight, its margin-vs-floor validate,
> and its fork/compare slots; (2) the **CLI** — the auto-discovered `click`
> command tree, the flag → pin → config-knob → worker-propagation layer, `zicato
> config env`, and `CLI.md`; (3) the **library facade** — the small lazy
> `zicato` surface, the import-linter contracts, the TID251 bans, and how a
> name (or a driver edge) is added; and (4) **packaging** — the wheel, the
> extras, the uv workspace member, and the supervisor-binary build hook.
>
> **Prerequisites.** 01-orientation.md §1.3 "Library first, three drivers on
> top" (the shape this chapter formalizes) and §4 "The Golden Rules" — the
> green-gates rule (parity gates, import contracts, the node suite), the
> omit-at-default rule, and the module-level-callable rule for callables that
> cross the worker boundary; 02-architecture.md §1 "The process topology" (the
> four OS processes the pins and the environment-variable contracts cross);
> 03-contract-and-epochs.md §3.7 "Computing the hash" (what "rolls the epoch"
> means — the builder's entire reason to exist); 04-evaluation-statistics.md §4
> "A/A noise-floor calibration" (what the pre-flight measures and what the
> promote margin must clear) and §6 "The evidence gate" (the `budget × 2 ×
> board` term that dominates the cost meter).
>
> **Invariants introduced in this chapter.** Later sections expand every one.
> Prose cites the name; the `ID` column stays because documents outside this
> chapter cite the ids.
>
> | ID | Name | Invariant |
> |----|------|-----------|
> | L1 | the one-mutation-surface rule | Every editable contract change flows through exactly one function in `zicato/contract_draft/operations.py`. The form, the copilot, and the REST dispatch all call the same op; there is never a second edit path. |
> | L2 | the full-coverage rule for a new knob | A new contract knob ships only once it has an op (`operations.py`), a dispatch arm (`api.py::_dispatch_op`), a copilot tool (`copilot_tools.py::DEFAULT_BUILDER_TOOLS`), a GUI control or a documented exception, a cost line if it changes the schedule, and a `validate` consideration if it can be unsound. Two tests — **the knob-coverage pins** — machine-pin the wiring: `test_default_builder_tools_registry_covers_every_op` for the op↔dispatch↔copilot triple, and `test_builder_gui_coverage.py` for the GUI control or exception. |
> | L3 | the honest-cost-meter rule | Every board-run multiplier the runtime will spend gets a `CostLine`; evaluation LLM calls are labelled and excluded from the board-runs headline. `operations.py::estimate_cost` is the only implementation of the arithmetic; the console renders the served numbers, and a correspondence test pins that what the page shows equals what the estimator computed. |
> | L4 | the recommend-only rule | The builder never hard-blocks `apply`. Even a `refuse`-severity warning or a `refuse` pre-flight verdict informs the operator; it never gates the write. |
> | L5 | the builder-never-rolls-the-epoch rule | The builder never rolls the epoch and never starts a live evolve. `apply(confirm=True)` writes the contract source files and lets the auto-epoch machinery roll on the next resolve; the copilot's apply tool is always `confirm=False`. |
> | L6 | the config-pins-not-environment rule | Flags cross the worker boundary via `config_pins`, never an environment variable. No environment variable is a configuration knob; operator knobs are CLI flags (pinned via `pin_overrides`) and `config.json` blocks. |
> | L7 | the lazy-pure-facade rule | `import zicato` imports only `zicato`; every facade name is `is`-identical to its home-module attribute; the `TYPE_CHECKING` mirror is the static view of the runtime lazy surface. |
> | L8 | the library-never-imports-a-driver rule | The only driver→driver edges are `cli → dashboard` and `dashboard → builder`; the import-linter contracts pin exactly that. |

---

## 10.0 Map of the three drivers

The library declares its surface in `src/zicato/__init__.py`; three drivers
consume it and each other along two — and only two — declared edges:

```
        zicato  (the library: __init__.py facade + ~30 subpackages)
          ▲  ▲  ▲
   consumes  │   consumes
          │  │   └───────────────── zicato.builder   (contract IDE backend)
          │  │                          ▲
          │  └── zicato.dashboard ──────┘  (mounts builder REST routes)
          │            ▲
          └── zicato.cli ──┘  (launches the dashboard server)
```

| Driver | Package | Entry point | What it is |
|---|---|---|---|
| CLI | `src/zicato/cli/` | `zicato = "zicato.cli:main"` (`pyproject.toml` `[project.scripts]`) | the `zicato` executable; an auto-discovered `click` group |
| Dashboard | `src/zicato/dashboard/` | `zicato dashboard` / auto-launched by `evolve` | the Starlette HTTP server + SSE stream (08/09) |
| Builder | `src/zicato/builder/` | mounted by the dashboard at `/builder/*` + `zicato dashboard --view builder` | the deterministic backend serving the contract draft (`src/zicato/contract_draft/`, library code) |

The two allowed driver→driver edges are `cli → dashboard` (the CLI launches the
server and resolves its static bundle) and `dashboard → builder` (`server.py`
mounts the builder's REST routes). **Everything else between drivers is
forbidden** — §10.11 is the enforcement.

| File | What lives there | Size |
|---|---|---|
| `src/zicato/builder/config.py` | `BuilderConfig` / `load_builder_config` — builder skills and theme | ~60 lines |
| `src/zicato/contract_draft/draft.py` | `TournamentDraft` (the mutable editable contract), `DraftStore` (sessions + named slots), `ContractDiff` | ~450 lines |
| `src/zicato/contract_draft/operations.py` | **THE mutation surface** — every `set_*` op, `estimate_cost`, `validate`, `compare_drafts`, `preflight`, `apply`, and their result dataclasses | ~1,600 lines |
| `src/zicato/builder/api.py` | `_dispatch_op` + the Starlette routes (`builder_routes`) | ~490 lines |
| `src/zicato/builder/copilot.py` / `copilot_tools.py` | the chat copilot + its tool registry `DEFAULT_BUILDER_TOOLS` | ~470 lines |
| `src/zicato/dashboard/static/js/views/builder.js` | the form GUI: rail sections, per-section controls, the Review pane | ~950 lines |
| `src/zicato/dashboard/static/js/builder/model.js` | `paramSpecsFor`, the schematic preview model, and the chat-pane width persistence — no cost or validation arithmetic | ~305 lines |
| `src/zicato/cli/discovery.py` | `build_cli_root`, `ZicatoGroup`, the command auto-discovery | ~370 lines |
| `src/zicato/cli/commands/*.py` | one command (or sub-group) per file — the inventory in §10.9 | — |
| `src/zicato/config.py` | `pin_overrides` / `pinned_override` / `load_config` + `describe_env_vars` | ~690 lines |
| `src/zicato/__init__.py` | the lazy `_EXPORTS` facade + `__getattr__` + `TYPE_CHECKING` mirror | ~75 lines |
| `pyproject.toml` | the import-linter contracts, TID251 bans, extras, uv workspace, wheel packaging | ~660 lines |
| `hatch_build.py` | the custom build hook that bundles `zicato-supervisor` into the wheel | ~98 lines |

---

## 10.1 The builder as a contract IDE — the three-layer stack

The builder edits an **evaluation contract** (board + proposer brief + scoring +
proposer dir) as a *draft*, previews the cost and epoch-roll consequences, and
writes it back — at which point the ordinary auto-epoch machinery rolls the
epoch on the next resolve. It does all of this with **no LLM dependency in the
data layer** (the copilot is a thin driver on top) and through one shared
mutation surface. The module docstring states the doctrine:

```python
# src/zicato/contract_draft/operations.py (module docstring, head)
"""Draft operations — the single source of truth for every contract edit.

Every editable change to a :class:`~zicato.contract_draft.draft.TournamentDraft`
flows through one of the operations here. The builder form's direct edits,
its copilot's tool calls, and the reflection adjudicator's staged edits all
call the *same* functions, so there is exactly one place each mutation's
semantics live.
"""
```

There are three layers, top to bottom, and each is a driver of the one below it:

1. **The draft (`draft.py`).** `TournamentDraft` is a **mutable** working copy
   of a whole contract — `scoring: ScoringWeights`,
   `entries: list[BoardEntry]`, `brief: str`, `proposer_path: Path | None`.
   `DraftStore` keys one draft per `session_id` and holds store-global **named
   slots** for the fork/compare lifecycle (§10.6). A session new to the store is
   lazily initialised from the CURRENT live contract via
   `TournamentDraft.from_workspace`, so the builder opens pre-filled with what is
   running.

2. **The operations (`operations.py`).** The one place each edit's semantics
   live. Write ops (`set_structure` … `set_brief`) mutate the draft in place and
   return a structured `DraftPatch`; read ops (`estimate_cost`, `validate`,
   `compare_drafts`, `preflight`) never mutate; `apply` writes the draft to the
   workspace (`confirm=True`) or previews it (`confirm=False`).

3. **The two front doors.** The REST dispatch (`api.py::_dispatch_op`, driven by
   the form GUI over `POST /builder/op`) and the copilot tool registry
   (`copilot_tools.py::DEFAULT_BUILDER_TOOLS`, driven by the chat agent). Both
   reconstruct typed arguments and call the SAME `ops.*` function. They return
   the SAME envelope shape (`{draft, patch, cost, warnings, diff}`) so the form's
   preview renderer reads one shape whether the last edit came from a control or
   a chat turn.

> ⛔ NEVER edit a `TournamentDraft` field directly from the API layer, the
> copilot, or the GUI (the one-mutation-surface rule). If a mutation is not expressible as a
> call to an `operations.py` function, the fix is to add (or extend) an op —
> never to reach around it. Two edit paths means two places for the semantics to
> drift, and the second one will not be the one the cost meter and `validate`
> know about.

> ✅ ALWAYS have an op return a `DraftPatch` whose `changed` maps
> `field → {"from": old, "to": new}` only for fields it actually moved. Every
> existing op skips a no-op assignment (it compares to the current value before
> recording the change), so the chat/UI renders a truthful "what changed"
> summary and a re-issued identical edit reads as a no-op rather than a phantom
> change.

### 10.1.1 The draft as the mutable mirror of the frozen contract

`TournamentDraft` is the ONE mutable thing in a subsystem otherwise built on
frozen dataclasses. The design note is explicit about why:

```python
# src/zicato/contract_draft/draft.py (TournamentDraft docstring, excerpt)
Unlike the frozen contract dataclasses in :mod:`zicato.core.types`, a
:class:`TournamentDraft` is MUTABLE — operations mutate it in
place and return a structured patch describing what changed. A
:class:`DraftStore` keys independent drafts by ``session_id`` so two
concurrent editing sessions never tread on each other.
```

`scoring` is itself a frozen `ScoringWeights`; every `set_*` op **replaces** it
wholesale with `dataclasses.replace(...)` (the helper `_replace_scoring`). Board
entries are likewise replaced, never mutated in place — which is what makes
`DraftStore._copy_draft` a real fork with only a shallow list copy (§10.6).

The draft's `diff_vs_live` and its three canonicalizers (`_board_canon`,
`_brief_canon`, `_scoring_canon`) **agree with the contract-hash and epoch-roll
rule** by construction. Scoring uses
`zicato.epoch.contract.scoring_contract_to_canon`, the same canonicalizer as
the contract hash. For an enabled Goldfive object, that function delegates
defaults and normalization to Goldfive's `RuntimeConfigDocument` before
comparing documents. The builder can therefore report whether an edit rolls
the epoch without inventing a second Goldfive schema.

> ⚠️ TRAP — the six diff *components* are not all epoch-rollers. `ContractDiff`
> reports `board` / `brief` / `scoring` / `proposer` / `structure` /
> `overfitting`, but `structure` and `overfitting` are **sub-views of
> `scoring`** surfaced separately so the UI can say *which* part of scoring
> moved. `rolls_epoch` is `board or brief or scoring or proposer` — do NOT add
> `structure`/`overfitting` into that OR or you double-count and a
> structure-only edit reports two rolls.

### 10.1.2 One model source; builder-only presentation config

The copilot model comes only from the workspace `models.builder` role; endpoint,
credential-variable name, and custom callable therefore use the same validation
and secret boundary as every other role. `builder.json` is narrower: it selects
builder skills and an optional UI theme. It lives at
`<workspace>/builder.json` or `<workspace>/.zicato/builder.json`; absent fields
default. `GET /builder/config` derives `chat_enabled` from `models.builder` and
never serializes engine credentials. One place names the copilot's model, and
form editing stays available on a workspace that configures no model at all.

### 10.1.3 The two front doors — the REST surface and the copilot

Both front doors call the same ops and return the same envelope
(`{draft, patch, cost, warnings, diff}`), which is the concrete form of the
one-mutation-surface rule. The REST surface (`api.py::builder_routes`, mounted
by the dashboard at `/builder/*`):

| Method + route | Handler | What it does |
|---|---|---|
| `GET /builder/config` | `builder_config` | `load_builder_config(root).to_public_dict()` with credential values omitted + the server-derived `vocab` (entry kinds / expectation kinds / reads / judge modes / severities / drift kinds — the GUI never hardcodes an enum) |
| `GET /builder/draft?session=ID` | `builder_draft` | the draft snapshot + cost/warnings/diff/slots + `proposer_dirs` (discovered `<workspace_parent>/proposers/*` candidates; degrades to `[]`) |
| `POST /builder/op` | `builder_op` | `{session, op, args}` → dispatch → the shared envelope |
| `POST /builder/apply` | `builder_apply` | `{session, confirm}` → `ApplyResult.to_dict()` |
| `POST /builder/chat` | `builder_chat` | `{session, message}` → SSE stream of copilot frames |

`_dispatch_op` handles the 20 write ops; the read/lifecycle ops
(`fork`/`switch`/`list_drafts`/`compare`/`revert_to_live`/`undo`/`preflight`)
are handled inline in the `builder_op` handler because they act on store
slots / the undo history or run async. `builder_op` also calls
`store.remember(session)` immediately before every `_dispatch_op` write —
one of the two pre-op capture seams behind the `undo` op (§10.6). A `read_only`
server returns 403 for the POST ops and apply while keeping the GETs live — the
dashboard's read-only mode never lets a viewer mutate a contract.

The copilot is a thin ADK agent whose tools are `DEFAULT_BUILDER_TOOLS`
(§10.7). Each tool pulls the session's draft from a contextvar bound by
`bind_builder_tool_context`, calls the matching op, and returns the SAME summary
shape the REST envelope carries. One shape therefore describes what an edit did,
whether it came from a control or a chat turn. `builder_chat` streams the
copilot's frames over SSE (`token` / `tool` / `patch` / `done` / `error`), and the
form applies a `patch` frame to the shared draft exactly as it applies a
`/builder/op` response, then re-renders. The copilot's apply tool is
`preview_apply`, which is ALWAYS `confirm=False` — the chat can preview an apply
but never rolls the epoch (the builder-never-rolls-the-epoch rule).

> ⚠️ TRAP — the copilot tool context arrives through a contextvar rather than a
> parameter. A copilot
> tool NEVER accepts the draft/session as an argument (the agent is constructed
> once per session and reused); it calls `_active_context()` on its first line.
> If you add a tool that takes the context as a parameter, the agent cannot
> supply it and every call fails — mirror the existing tools exactly. This is the
> same contextvar-seam pattern the proposer tools use (see 05-proposer.md
> §5.9.1 "The contextvar binding").

### 10.1.4 The board editor — a GUI surface that adds no ops

Board authoring is the builder's largest form surface, and it is built on the
same doctrine as every other control: **it drives existing ops only.** The
Board section renders each entry as a clickable row (id + kind + at-a-glance
badges) that toggles an **inline accordion editor**. The console carries no
modal machinery, and the accordion fits the `gatedSwap` + `section` idiom the
rest of the view uses.

The editor lives in `dashboard/static/js/builder/entry_form.js`, a **pure
DOM-builder module** with no fetching and no module state of its own. It is a
function of three things:

- a **buffer** — a plain JSON object shaped exactly like what
  `validate_board_entry` (core/board.py) accepts. `entryToBuffer(row)` reads a
  `draft.board` row (note it carries the short-form `budget_s` the entry
  serializer writes) into a buffer; `bufferToEntryJson(buffer)` emits the
  canonical `wall_clock_budget_seconds` the `edit_board_entry` op parses;
  `newEntryBuffer(kind)` seeds a create-mode buffer with the kind's
  discriminants.
- the server-derived **`vocab`** (from `GET /builder/config`) so the kind /
  expectation-kind / reads / judge-mode / severity / drift-kind selects render
  from the SAME enums the validators enforce — the JS never hardcodes an enum.
- a **handler bag** (`onSave` / `onCancel` / `onDelete` / `onDuplicate` /
  `onChange`) the view (`views/builder.js`) wires to its module state.

**The whole-entry round-trip.** Save posts the WHOLE buffer through the
existing `edit_board_entry` op (a replace-by-id) — there is no separate mutation
path for individual fields, and no per-field ops. A judges-list edit, an
expectation change, and a budget bump all ride the one whole-entry replace on
Save. This is the one-mutation-surface rule in its most literal form: the
largest form surface in the builder adds no ops.
`tests/test_builder_api.py::test_builder_op_edit_board_entry_whole_entry_round_trip`
pins the byte-stability per kind — the re-read row equals
`entry_to_dict(validate_board_entry(payload))`, which is exactly the save/reopen
loop the editor relies on. Delete drives `remove_board_entry` behind a two-click
confirm; a per-judge badge's × drives `remove_judge` directly; the board-level
`board_meta` panel (drift suppression and judge-only) drives `set_board_meta`,
so the board header is an ordinary GUI control rather than a documented
exception.

**No client-side validation (the recommend-only rule).** The form carries no
port of `BoardEntry.validate`. Save is gated only on the PRESENCE of an id (a
presence-only enable/disable, never a semantic check); every structural
objection is the server's field-precise `ValueError`, rendered verbatim in the
editor's inline error strip — and the editor stays open so the operator can fix
it. Failures route to the editor's own `_editError`, never the global flash. The
one client-side check that IS present is a non-blocking convenience: the
JSON-schema spec control shows a `JSON.parse` hint (`✓ parses` / `⚠ not valid
JSON`) so an operator sees a typo before they post — it never disables Save.

**Two controls that must not fight.** The `holdout` tag is owned by the
train/holdout toggle, so `entryToBuffer` STRIPS it from the tags input on load
and `bufferToEntryJson` RE-APPLIES it from the toggle state on save. If the form
carried the tag in its comma input, editing an entry would silently move it in
or out of the holdout — the strip/re-apply keeps the two controls disjoint.
Similarly, the id is LOCKED when editing an existing entry (a replace-by-id
would turn a rename into a silent duplicate); a **Duplicate** button seeds a
create-mode buffer under a cleared id instead.

**Why the editor survives a digest re-render.** The open buffer, the edited id,
the create flag, and the inline error all live in `views/builder.js` MODULE
STATE, and the center digest folds them in. `renderCenter` rebuilds the open
editor from the pinned buffer on every render, so an unrelated re-render (an op
result landing, a chat patch applying) never closes the editor or loses a typed
value. VALUE edits mutate the buffer in place WITHOUT a re-render (focus is
preserved); STRUCTURAL edits (kind switch, add/remove a judge or turn, toggle
the expectation sub-form) mutate the buffer and call `onChange` so the view
re-renders off it. The kind switch clears the inapplicable discriminant fields
and keeps the common ones (a `single_turn`↔`synthetic_adversarial` switch keeps
`input`). A paste-JSONL import box splits lines client-side, routes a
`{"board_meta": true, …}` header line to `set_board_meta` and each entry line to
`edit_board_entry`, and reports per-line results inline (a bad line never blocks
the good ones).

---

## 10.2 The op inventory

Every editable contract knob has exactly one write op. The full-coverage rule
for a new knob is stated over this table: adding a knob means adding a row here
plus its five companion surfaces (§10.7). The op string equals the function name
in every case.

| Op / function | Mutates (contract knob) | Key args / knobs |
|---|---|---|
| `set_structure` | `scoring.tournament_structure.structure` (params preserved) | `structure: str` (validated by `TournamentStructure`) |
| `set_param` | one `tournament_structure.params[key]` | `key`, `value` (stored verbatim; `None` removes the key) |
| `set_holdout` | `scoring.overfitting` + per-entry `holdout` tags + nested `ladder` | `enabled`, `fraction`, `tags`, `min_board_size_for_split`, `rotate_holdout`, `restrict_proposer_visibility`, `random_baseline_every_n`, `max_generations_per_contract` (`0` clears), `ladder` (partial dict) |
| `set_proposer` | `draft.proposer_path` | `proposer_path: str \| Path \| None` |
| `set_weights` | the pass term + the within-channel shapes on `scoring` | `pass_weight`, `per_kind_weights`, `per_judge_weights`, `default_judge_weight`, `plan_revision_weight`, `task_failure_weight`, `not_completed_weight`, `severity_weights` |
| `set_gate` | the promote and holdout gates on `scoring` | `promote_margin`, `holdout_margin`, `holdout_entry_regression_budget`, monotonicity and containment controls, regression-command controls |
| `set_namespace_weights` | namespace and patch-complexity scoring | `namespace_weights`, `diff_complexity_weight`, `diff_complexity_ceiling` |
| `set_proposer_quality` | nested `scoring.proposer_quality` | slate size, critique, process exemplars, recombination, genealogy, calibration feedback, and merge mode |
| `set_experiment_memory` | `scoring.experiment_memory.cross_epoch` | `cross_epoch: bool` |
| `set_goldfive` | optional `scoring.goldfive` JSON document | `config: {}` enables the complete defaulted document, a partial object edits it, and `null` removes it |
| `set_telemetry_dialect` | `scoring.telemetry_dialect` | `dialect`: `goldfive`, `adk_events`, or `transcript` |
| `set_mutation_surface` | `scoring.mutation_surface` | `mutation_surface`: complete suffix-to-comment-syntax mapping |
| `set_screening` | `scoring.proposer_quality.screen_entries` / `screen_veto_only` | `entries` (≥0), `veto_only` |
| `edit_board_entry` | `draft.entries` (add/replace by id; validates first) | `entry: BoardEntry` |
| `add_board_entry` | `draft.entries` (append with duplicate-id refusal) | `entry: BoardEntry` |
| `remove_board_entry` | `draft.entries` (delete by id; unknown id raises) | `entry_id` |
| `add_judge` | one entry's `judges` tuple | `entry_id`, `judge: JudgeSpec` |
| `remove_judge` | one entry's `judges` tuple | `entry_id`, `name` |
| `set_brief` | `draft.brief` | `text: str` |
| `set_board_meta` | the board-level `board_meta` header (`draft.disable_drift` / `draft.judge_only`) | `disable_drift` (wholesale token list, validated; `[]` clears, `None` unchanged), `judge_only` |

Four structural facts:

- **Goldfive owns the fields inside its document.** The Builder exposes one
  JSON-object operation rather than one Zicato control per Goldfive setting.
  `set_goldfive` asks `RuntimeConfigDocument` to validate, apply defaults, and
  return the normalized object. Adding a Goldfive field changes Goldfive's
  document API and scaffold; it does not require a matching dataclass or
  registry entry in Zicato.

- **The scoring ops compose on nested blocks.** `set_proposer_quality` and
  `set_screening` both edit the *same* `proposer_quality` block — each touches
  only its own fields and replaces the block with a `dataclasses.replace`. They
  are two operator-facing concerns — slate quality and tryout screening — over
  one contract sub-object. Do not merge them; do not let one clobber the
  other's fields.
- **Every op validates at the boundary, never silently coerces.** `set_gate`
  raises `ValueError` on an invalid `monotonicity_scope`; `set_namespace_weights`
  raises on a negative `diff_complexity_weight`; `edit_board_entry` calls
  `entry.validate()` before the entry lands. A bad edit raises rather than
  corrupting the draft — the dispatch layer turns the raise into a 400.
- **Mapping fields are edited wholesale.** `set_weights`'s `per_kind_weights` /
  `per_judge_weights` / `severity_weights` and `set_gate`'s
  `namespace_monotonicity` REPLACE the whole mapping. The builder edits a
  contract rather than merging into one; a caller that wants to add one judge
  weight sends the full new mapping.

> ⚠️ TRAP — an explicit `0` does not mean the same thing everywhere. Across the
> ops, `None` is universally "leave unchanged". The meaning of an explicit `0`
> varies by field and is documented per-op:
> `set_holdout(max_generations_per_contract=0)` **clears** the ceiling, because
> that field's real "off" is `None` and the op reserves `None` for "unchanged";
> `set_screening(entries=0)` turns the screen **off**.
> When you add a knob whose natural "off" is `None`, you cannot use
> `None` as the "unchanged" sentinel too — pick an explicit sentinel (`0`, a
> flag) and document it in the op docstring, exactly as `set_holdout` does.

---

## 10.3 The honest cost meter

`estimate_cost(draft) -> CostEstimate` is the builder's most important read op:
it prices a contract's **board-runs-per-round** before the operator commits, so
an authoring choice is annotated with its downstream cost. The result shapes:

```python
# src/zicato/contract_draft/operations.py
@dataclass(frozen=True, slots=True)
class CostLine:
    label: str
    runs: int
    detail: str = ""

@dataclass(frozen=True, slots=True)
class CostEstimate:
    structure: str
    board_size: int
    holdout_size: int
    board_runs_per_round: int
    breakdown: tuple[CostLine, ...]
```

### 10.3.1 The terms, in the order they sum

`estimate_cost` walks the train/holdout split, resolves a **structure-aware**
default `replicates`, then sums one base-schedule term plus up to five
honest-meter terms. Each term that fires appends a `CostLine` and (unless it is
the evaluation line) adds into `per_round`:

| Term (label) | Formula | Fires when |
|---|---|---|
| base schedule — `duel runs` | `field_size × replicates × board` | gauntlet, or `field_size ≤ 1` |
| base schedule — `bracket-match runs` | `matches × replicates × board`, `matches = field_size−1` (single) / `2·(field_size−1)` (double) | `single_elim` / `double_elim` |
| base schedule — `swiss-pairing runs` | `rounds_n × pairings × replicates × board`, `pairings = field_size//2` | `swiss` |
| base schedule — racing rungs | successive-halving rung sum + final full-board duel (`_racing_cost`) | `racing` |
| `holdout-confirm runs` | `holdout_size × replicates` | any structure with a non-empty holdout |
| `candidate-screen runs` | `proposes × best_of_n × panel`, `panel = min(screen_entries, board)` | `screen_entries > 0 and best_of_n > 1` |
| `best-of-N propose calls` | `proposes × best_of_n` — **evaluation LLM calls, excluded from the headline** | `best_of_n > 1` |
| `crowning-confirm runs (evidence gate)` | `budget × 2 × board` | `promote_confidence_threshold` is set |
| `placebo-baseline runs (amortized)` | `ceil(replicates × board / random_baseline_every_n)` | `random_baseline_every_n > 0` |

The default `replicates` is read from the selection layer rather than
hard-coded — the comment carries the reason:

```python
# src/zicato/contract_draft/operations.py — estimate_cost
    # ``replicates`` defaults to the STRUCTURE's own default (swiss / elim
    # default to 2 — replication, not bracket shape, is their noise lever),
    # NOT a flat 1. The default is read from the selection layer's
    # single source of truth (each strategy's ``_default_replicates``), so the
    # meter cannot under-report the schedule a structure actually runs. An
    # EXPLICIT ``replicates`` in params is honored verbatim.
    replicates = max(1, _param_int(params, "replicates", default_replicates_for(structure)))
```

### 10.3.2 The evidence-confirm budget dominates — the sample math

The single most important number on the meter is the evidence gate's
crowning-confirm term. When the contract sets `promote_confidence_threshold`, the
defer→replicate loop may spend a FRESH board sweep for BOTH crowning contestants
per replicate, so `budget × 2 × board`:

```python
# src/zicato/contract_draft/operations.py — estimate_cost
    if read_promote_confidence_threshold(params) is not None:
        budget = read_replicate_budget(params)
        confirm_runs = budget * 2 * board_size
        if confirm_runs:
            lines.append(
                CostLine(
                    "crowning-confirm runs (evidence gate)",
                    confirm_runs,
                    f"budget {budget} × 2 contestants × board {board_size} — per "
                    "confirmed crowning (upper bound)",
                )
            )
            per_round += confirm_runs
```

Worked example — the recommended scaffold on a 20-entry train board, gauntlet,
`field_size = 2`, `replicates = 2`, evidence gate on with `budget = 32`:

| Term | Runs |
|---|---|
| `duel runs` = `2 × 2 × 20` | 80 |
| `holdout-confirm runs` (say 5 held out × 2) | 10 |
| `crowning-confirm runs` = `32 × 2 × 20` | **1,280** |
| **board-runs-per-round (headline)** | **1,370** |

The evidence-confirm budget is 16× the base schedule here. That multiple is why
the meter exists. An operator who turns the evidence gate on with the scaffold's
32-replicate budget has multiplied their per-round board spend, and the meter
says so in a line they can read *before* they apply. The op docstring names it:
"with the scaffold's 32-replicate budget this is typically the LARGEST term."

> ✅ ALWAYS add a `CostLine` for any new contract knob that multiplies the
> per-round board sweeps (the honest-cost-meter rule). The meter's
> contract is that its headline is a coarse upper-ish bound on the *board runs*
> a round actually spends. A knob that adds runs without a line leaves the
> headline short, and the operator learns the true cost only from their model
> bill.

> ⚠️ TRAP — evaluation LLM calls are NOT board runs. The `best-of-N propose
> calls` line is appended to the breakdown but NOT added to `per_round`: those
> are proposer-side model calls rather than board evaluations. Keep that
> distinction when you add a term. If your knob spends model calls that are not
> board sweeps, label the line "evaluation" and leave it out of the headline sum,
> exactly as best-of-N does. Conflating the two double-charges the board-runs
> headline.

### 10.3.3 One owner for the meter, and the test that pins the join

`operations.py::estimate_cost` is the only implementation of the cost
arithmetic, and `operations.py::validate` the only implementation of the
recommend-only lint rules. Both surfaces that show them read a server envelope:
the live builder view POSTs every control change to `/builder/op` and renders
the `cost` and `warnings` the response carries, and Settings → Contract fetches
`/builder/draft` for the same two keys. The console's own modules hold no copy
of either — `builder/preview.js` walks the breakdown it was handed and prints
the labels, run counts and details verbatim.

`tests/test_builder_cost_envelope_correspondence.py` pins that arrangement
across the language boundary. For five drafts — chosen so that between them
they reach every cost term and every finding a draft alone can raise — it
computes the envelope in Python, writes it to a fixture file, renders it under
node through `builder/preview.js` itself (via the
`static/test/cost_envelope_readback.mjs` driver), and compares the numbers and
texts read back off the rendered nodes with the ones Python produced:

```python
# tests/test_builder_cost_envelope_correspondence.py
assert rendered == [
    {
        "name": e["name"],
        "board_runs_per_round": e["cost"]["board_runs_per_round"],
        "breakdown": e["cost"]["breakdown"],
        "warnings": [...],
    }
    for e in envelopes
]
```

Renaming a field on either side breaks it: a key the estimator stops emitting
leaves the renderer showing a default, and a key the renderer stops reading
does the same. A second check in the same module fails if any file under
`static/js/` spells a cost-line label or a finding code as a string literal,
which is what a regrown copy would have to do.

> ✅ ALWAYS add a fixture when you add a cost term or a lint rule. The coverage
> assertion requires the rendered term and code sets to equal the expected
> sets, so a term no fixture reaches reds the suite rather than passing
> untested.

> ⛔ NEVER compute a cost estimate or a lint finding in the browser. The
> operator would then have two prices for one contract with nothing to say
> which is right. Everything the page needs arrives in the envelope; if
> something is missing from it, add it to the envelope.

---

## 10.4 The statistical pre-flight — a read-op that measures the DRAFT

`preflight` is an `async` read op that runs the SAME measurement `zicato board
preflight` takes — an A/A noise-floor calibration and a degradation-signal probe
— but against the DRAFT's board and scoring, using the workspace's own champion
tree, adapter, and runtime `call_llm`. Its result is `PreflightResult`:

```python
# src/zicato/contract_draft/operations.py
@dataclass(frozen=True, slots=True)
class PreflightResult:
    available: bool
    verdict: str | None = None      # "ok" / "warn" / "refuse", when available
    reason: str = ""                # the honest degrade explanation otherwise
    report: dict[str, Any] | None = None
    noise_floor: dict[str, Any] | None = None
```

The whole design is **honest degrade, never a crash**. Each missing prerequisite
returns `available=False` with a `reason` naming exactly what is missing —
never an exception for a workspace that simply is not ready:

```python
# src/zicato/contract_draft/operations.py — preflight (one of several degrade arms)
    epoch_id = current_epoch_id(workspace_root)
    if not epoch_id:
        return PreflightResult(
            available=False,
            reason=(
                "preflight requires a registered target: no current epoch under "
                "this workspace (run `zicato epoch register` / `zicato epoch new` first)"
            ),
        )
```

The degrade arms, in order: empty draft board → no current epoch → no workspace
config → no seeded baseline generation → no configured adapter → no runtime
`call_llm` → no mutation points under the champion snapshot. On success it
returns `available=True` with `verdict=report.verdict`, the report JSON, and the
measured noise floor.

Two properties an extender must preserve:

- **The verdict is recommend-only and never persisted** (the recommend-only
  rule and the builder-never-rolls-the-epoch rule). The draft is not the live
  contract, so its measurement must never be mistaken for the live epoch's —
  `preflight` never writes onto the epoch record. The epoch-open pre-flight
  (13-recipes.md Recipe 8, "Add an epoch-open step") behaves the other way: it
  DOES persist onto the never-hashed epoch record.
- **It never starts a live evolve.** It spends only the small K-draw calibration
  budget and is cache-idempotent with `zicato board audit` (re-running is a
  cache hit). It draws on two claimed replicate bases — 1000 for the A/A
  calibration and 2000 for the pre-flight probes. See
  04-evaluation-statistics.md §8 "THE RESERVED REPLICATE-BASE LEDGER".

> ⚠️ TRAP — the pre-flight measures the DRAFT contract but borrows the LIVE
> target. `run_contract_preflight` consumes the draft's `board` and `weights`
> directly (no on-disk materialization) but takes the champion generation,
> adapter, and runtime from the workspace. If you extend the pre-flight to a new
> contract component the builder edits, thread it from the draft; if you extend
> it to a new *target* fact, thread it from the workspace. Inverting either
> direction makes the pre-flight measure a contract the operator did not draft.

---

## 10.5 `validate` and the Review pane verdict

`validate(draft, workspace_root=None, *, noise_floor_max_abs_delta=None) ->
list[Warning]` returns a list of advisory warnings — **never a blocking verdict**
(the recommend-only rule). Each warning carries a stable `code`, a human `message`, and
a `severity`:

```python
# src/zicato/contract_draft/operations.py
@dataclass(frozen=True, slots=True)
class Warning:
    code: str        # stable symbolic code the UI keys on
    message: str
    severity: str = "warning"   # "info" / "warning" / "refuse" — never blocks
```

The severity ladder is `info` (advisory) / `warning` (likely a mistake) /
`refuse` (statistically unsound) — and the docstring on the field is emphatic
that even `refuse` "never hard-blocks apply." The warnings `validate` emits:

| Code | Severity | Fires when |
|---|---|---|
| `field_size_degrades_to_gauntlet` | warning | a non-gauntlet structure with `field_size == 1` |
| `holdout_disabled_small_board` | info | board below `min_board_size_for_split` with no explicit holdout tag |
| `racing_rung0_slice` | info | racing — surfaces the rung-0 slice size |
| `replicates_recommended_for_brackets` | warning | bracket/swiss with `replicates < 2` |
| `holdout_tags_cover_whole_board` | warning | every entry tagged holdout (no train entries) |
| `duplicate_entry_id` | **refuse** | two entries share an id — `apply` would fail (`save_board` rejects duplicates) and run artifacts would collide |
| `entry_id_unsafe` | warning | an entry id outside `^[A-Za-z0-9][A-Za-z0-9._-]*$` (ids become run directory names) |
| `dotted_path_malformed` | warning | a predicate spec or python-judge body that does not LOOK like `pkg.module.attr` / `pkg.module:attr` — shape-check only (see the security note) |
| `rubric_spec_invalid` | warning | a rubric spec that is not `{"rubric": str, "threshold": number\|null?, "scale": [lo, hi]?}` (mirrors the runtime parse in `board/rubric.py`) |
| `json_schema_spec_invalid` | warning | a json_schema spec that is not valid JSON (or not an object/boolean) |
| `entry_budget_outlier` | info | an entry's wall-clock budget > 10× the board median |
| `judge_only_board` | info | the board_meta `judge_only` flag is set (judges observe, never steer) |
| `margin_below_noise_floor` | **refuse** | see below |

Every code above is produced by `validate` alone and reaches the console
through the response envelope, entry-level codes included: the board-authoring
checks need the full `BoardEntry` objects, which only the server holds.

> ⛔ NEVER import (or `find_spec`) an operator-supplied dotted path inside
> `validate` — the `dotted_path_malformed` check is SHAPE-ONLY by design.
> Resolving a module executes parent-package code, and a draft may be
> copilot-authored, so a server-side import would hand the chat model an
> arbitrary-code-execution path. The warning message points the operator at
> `zicato board audit`, which exercises the path in the workspace's own
> runtime context. The posture is recorded in `validate`'s docstring; keep
> any future path/spec check on the shape side of that line.

### 10.5.1 The margin-vs-floor check — the statistical `refuse`

The statistically load-bearing check pairs the promote margin against the
measured A/A noise floor. If a floor is known (passed in from a just-run
`preflight`, or read off the current epoch record) AND the evidence gate is off
AND `promote_margin <= floor`, it fires `margin_below_noise_floor` at `refuse`:

```python
# src/zicato/contract_draft/operations.py — validate (the margin-vs-floor arm)
        gate_on = read_promote_confidence_threshold(params) is not None
        margin = draft.scoring.promote_margin
        if not gate_on and margin <= floor:
            warnings.append(
                Warning(
                    "margin_below_noise_floor",
                    f"promote_margin {margin:.6g} does not clear the measured A/A "
                    f"noise floor {floor:.6g} and the evidence gate "
                    "(promote_confidence_threshold) is off: a duel decided by the "
                    "margin alone cannot distinguish a real improvement from a "
                    "re-roll of the same tree. Raise promote_margin above the "
                    "floor or enable the evidence gate. Recommend-only — apply "
                    "is not blocked.",
                    severity="refuse",
                )
            )
```

This is the same recommend-only REFUSE posture the contract pre-flight verdict
carries: it names the two fixes (raise the margin, or turn on the evidence gate)
and states in the message itself that apply is not blocked.

### 10.5.2 The Review pane

The GUI's Review section (`reviewSection` in `views/builder.js`) is where these
surface. It renders, in order: the `rolls_epoch` flag and changed components
from the diff, the pre-flight panel (a chip rendering `ok` / `warn` / `refuse` /
`unavailable`), a `refuse`-warnings panel (`severity === 'refuse'` warnings under
a ⛔ glyph), the fork/compare diff, and a **two-click Dry-run / Apply** confirm
(`postApply` → `POST /builder/apply`). The rail marks Review "done" only when
`_diff.rolls_epoch` — that is, only when there is something to apply.

> ✅ ALWAYS give a new `validate` warning a stable symbolic `code`. The UI keys
> on it (`refuseWarningsPanel` filters on severity; the rail and chips key on
> codes), and a copilot turn may cite it. A message-only warning with no code is
> unstyleable and untestable.

> ⛔ NEVER make `validate` (or the pre-flight verdict) block `apply` (the
> recommend-only rule). The builder's posture is that the operator is the
> authority: it surfaces the unsoundness in the loudest recommend-only form it
> has (`refuse` + a ⛔ chip) and writes the contract anyway if they confirm. If a
> check ever *must* block, it belongs to contract validation in
> `epoch/contract.py` rather than to a builder warning — and it will then block
> every write path rather than only the builder's.

### 10.5.3 The apply path — dry-run vs confirm, and why apply does not roll

`apply(draft, workspace_root, confirm)` is where a draft becomes (or previews
becoming) the live contract. It returns an `ApplyResult` carrying `confirmed`,
`rolled`, `components_changed`, `new_contract_hash`, `cost`, `diff`, and
`warnings`. The two branches:

- **Dry run (`confirm=False`).** Nothing is written. The result carries the
  *predicted* contract hash, computed by `_predicted_contract_hash`, which
  materializes the draft's board/brief/scoring into a throwaway
  `tempfile.TemporaryDirectory` and runs the real `compute_contract_hash` over
  them, the workspace's live adapter identity and mutable trees, and the draft's
  proposer. The operator sees the hash an apply would land without touching the
  workspace. `rolled` is always `False` for a dry run.
- **Confirm (`confirm=True`).** `_write_contract` writes the draft to the
  workspace's LIVE contract source paths — the same `board.jsonl` / `brief.md` /
  `scoring.json` (and proposer dir) that `zicato epoch register` / `zicato epoch new`
  publish, recorded under `config.json`'s `contract` key. The result recomputes
  the hash from the now-written live contract and sets `rolled=diff.rolls_epoch`.

The critical design point (the builder-never-rolls-the-epoch rule): **`apply`
writes contract *source files*; it never opens an epoch.** The ordinary
auto-epoch machinery rolls the epoch on the NEXT `zicato evolve` resolve,
exactly as it would for a hand-edited `scoring.json`. The builder edits a
contract; it does not drive epochs. It reuses the existing write paths, so one
mechanism rolls the epoch and the builder owns no second one:

```python
# src/zicato/contract_draft/operations.py — apply (confirm branch)
    _write_contract(draft, workspace_root)
    # Recompute the hash from the now-written live contract so the result
    # reflects exactly what the next resolve will see.
    from zicato.epoch.contract import (  # noqa: PLC0415
        compute_contract_hash,
        resolve_contract_inputs,
    )

    new_hash = compute_contract_hash(resolve_contract_inputs(workspace_root))
    return ApplyResult(
        confirmed=True,
        rolled=diff.rolls_epoch,
        ...
    )
```

> ⚠️ TRAP — a dry-run's predicted hash and a confirmed apply's hash are computed
> two DIFFERENT ways (temp-dir materialization vs. re-resolving the written
> workspace), and they must agree for an unchanged draft. If you add a contract
> input, thread it into BOTH `_predicted_contract_hash`'s `ContractInputs` and
> the live `resolve_contract_inputs` path, or the preview quotes a hash the apply
> does not produce — and the operator's "this rolls to X" promise breaks. This is
> the builder's local instance of the contract-hash cwd/checkout hazard — the
> case where the contract hash embedded the checkout path
> (`12-bug-casebook.md` Case 10).

> ⚠️ TRAP — the draft must round-trip EVERY board-file component, the header as
> well as the entries. The board's optional `board_meta` header (`disable_drift` /
> `judge_only`) is part of the contract, and `apply` rewrites the whole
> `board.jsonl`: a draft loaded through the entries-only loader would silently
> STRIP the header from the live contract on apply.
> `TournamentDraft.from_workspace` therefore loads via
> `load_current_board_with_meta`, the draft carries `disable_drift` /
> `judge_only` fields, and BOTH writers (`_write_contract` and
> `_predicted_contract_hash`) pass them to `save_board`. The draft's
> `_board_canon` prepends the header line only-when-non-default, mirroring
> `save_board`'s emit rule (`zicato.board.jsonl.board_meta_to_dict` is the
> shared header builder), so the diff agrees with the on-disk bytes the
> contract hash sees. If you add another board-level header field, thread it
> through the same four seams in one commit: the loader, both writers, and
> `_board_canon`.

---

## 10.6 Fork / compare — iterating on variants without rolling the epoch

The builder lets an operator hold several contract variants side by side and
diff them, all without touching the live workspace. Two mechanisms cooperate:

**Named slots (`DraftStore`, `draft.py`).** `fork(session, name, root)` snapshots
the session's working draft into a named slot and rebinds the session TO that
slot (subsequent edits accumulate on it); `switch(session, name)` rebinds with
state intact; `list_drafts()` lists the slots; `slot(name)` reads one. The fork
is a real copy because the draft's frozen `scoring` and wholesale-replaced
entries make a shallow list copy safe:

```python
# src/zicato/contract_draft/draft.py — _copy_draft
def _copy_draft(draft: TournamentDraft) -> TournamentDraft:
    """A safe working copy of ``draft`` for a named slot.

    ``scoring`` is a frozen dataclass and every operation REPLACES it (and
    replaces board entries wholesale — entries themselves are never
    mutated in place), so a shallow copy of the entries list is a real
    fork: edits to either copy can never leak into the other.
    """
    return TournamentDraft(
        scoring=draft.scoring,
        entries=list(draft.entries),
        brief=draft.brief,
        proposer_path=draft.proposer_path,
    )
```

Slots are process-local and never persist to disk: a draft does not outlive the
dashboard process. `fork` raises on a malformed or already-taken name (it never
silently overwrites a variant).

**The compare read-op (`compare_drafts`, `operations.py`).** A keyed diff between
any two drafts, over the SAME canonicalizers the epoch-roll rule uses, so
"differs here" agrees with "would roll the epoch". It returns
`changed_components` plus per-component detail (`scoring` keys with `a`/`b`
values, `board` `added`/`removed`/`changed` ids, `brief`, `proposer`). Because
its scoring keys come from the contract-canonical form (exact parsed numeric
values, omitted-at-default fields absent, and system-owned integration identity
included), the diff never reports a phantom change the hash would not see.

> ⚠️ TRAP — the copilot's `compare` tool resolves the literal names `"session"`
> and `"live"` specially (current working draft, and a fresh
> `from_workspace`), in addition to slot names. If you add a resolvable name,
> add it to BOTH the copilot's `compare` tool and any API compare path, so both
> surfaces offer the operator the same set of things to compare against.

### 10.6.1 Undo and revert — the two restore ops

Two lifecycle ops let an operator walk edits back, and both are ops
(the one-mutation-surface rule — the GUI's Undo/Reset buttons and the copilot's
tools call the same functions, never a second edit path):

- **`revert_to_live`** discards the session draft's edits by restoring it
  from a fresh `TournamentDraft.from_workspace(root)`. The restore is
  performed by `operations.restore_draft(draft, source)` — **in place**,
  never a rebind, so a session bound to a named slot stays bound and the slot
  itself sees the restored state. The pre-revert state is remembered first,
  so `undo` brings the discarded edits back.
- **`undo`** steps back one edit. `DraftStore` keeps a **bounded (20)
  per-session history** of `_copy_draft` value snapshots; `store.remember(
  session)` records the PRE-op state at BOTH front doors — `builder_op` calls
  it immediately before every `_dispatch_op` write, and
  `BuilderToolContext.draft()` (the accessor every copilot tool starts with)
  does the same. A form edit and a chat edit therefore share ONE history, and
  either door can undo the other's edit. `remember` dedups against the newest
  snapshot by field equality, so a read tool records nothing. `pop_undo`
  discards snapshots equal to the current state on the way down and hands
  back the newest one that differs. An exhausted history yields a
  `DraftPatch(op="undo", note="nothing to undo")` — never an error.

> ✅ ALWAYS restore a draft IN PLACE (`operations.restore_draft`) rather than
> rebinding the session to a fresh object. The store's session entry, a named
> slot the session is on, and the copilot's bound context may all reference
> the SAME draft object — a rebind silently detaches the session from its
> slot and the next slot read shows stale state. This is the same reason the
> ops mutate the draft rather than returning a new one.

---

## 10.7 The full-coverage rule, and how it is enforced

The full-coverage rule for a new knob keeps the two front doors and the GUI in
step with the op set. A new contract knob is not shipped until it lands on
**six** surfaces:

| # | Surface | File | Enforced by |
|---|---|---|---|
| 1 | the op | `operations.py` (a `set_*` function) | code review |
| 2 | the dispatch arm | `api.py::_dispatch_op` (an `if op == "…":` arm) | `tests/test_builder_api.py` knob-dispatch tests |
| 3 | the copilot tool | `copilot_tools.py::DEFAULT_BUILDER_TOOLS` | **`test_default_builder_tools_registry_covers_every_op`** (machine-pinned) |
| 4 | a GUI control (or a documented exception) | `model.js::paramSpecsFor` / `views/builder.js` / `builder/entry_form.js` section | **`test_builder_gui_coverage.py`** (machine-pinned) + node suite |
| 5 | a cost line (if it changes the schedule) | `operations.py::estimate_cost` | the correspondence test's coverage assertion |
| 6 | a `validate` consideration (if it can be unsound) | `operations.py::validate` | `tests/test_builder_operations.py` |

Surfaces 2–4 are **mechanically pinned**, surfaces 3 and 4 by the two tests
this chapter calls **the knob-coverage pins**. The dispatch is a flat if/elif
chain that falls through to a raise, so an op missing its arm is a 400 the API
tests catch. The copilot registry is pinned by an explicit anti-drift test:

```python
# tests/test_builder_copilot.py — test_default_builder_tools_registry_covers_every_op
    """ANTI-DRIFT PIN: the copilot's tool registry carries every builder op —
    the write ops (incl. the knob-coverage ops), the read ops, and the
    build-time statistical preflight. A new op added to operations.py / the
    API dispatch without a copilot tool fails here."""
```

and the GUI surface is pinned by a second registry-derived test:

```python
# tests/test_builder_gui_coverage.py — test_every_write_op_has_a_gui_control_or_exception
    """THE PIN: every builder write / lifecycle op is reachable from the GUI.
    Each mutating op must appear as runOp('<op>' / postOp('<op>' in the builder
    frontend source, or carry a justified GUI_EXCEPTIONS entry. A new op added to
    operations.py + the copilot registry without either reds here."""
```

`test_builder_gui_coverage.py` derives the write/lifecycle op set from
`DEFAULT_BUILDER_TOOLS`, minus the pure-read tools (`estimate_cost`, `validate`,
`preflight`, `list_drafts`, `compare`, `preview_apply`). It then reads
`views/builder.js` + `builder/entry_form.js` as TEXT and demands each op is
wired as `runOp('<op>'` / `postOp('<op>'` OR justified in its `GUI_EXCEPTIONS`
dict. The one standing exception is `add_judge` (judge authoring rides the
whole-entry `edit_board_entry` round-trip — the entry_form judges editor —
rather than a
second authoring path). A stale exception (an op that has gained a control since
the exception was written) reds just as loudly as a missing control, so the
coverage claim stays true in both directions.

Surfaces 5–6 are **discipline plus parity**. No single per-knob test asserts
"this knob has a cost line AND a validate check". What holds them is narrower:
the correspondence test forces any cost line you add to reach a fixture, and the
design doc (`docs/design/TOURNAMENT-BUILDER.md` §4, "The consequence-forward
principle") makes the cost and epoch-roll surfacing a stated requirement of the
two builder skills.

> ⛔ NEVER add an op to `operations.py` and its `_dispatch_op` arm but skip the
> copilot tool (the full-coverage rule).
> `test_default_builder_tools_registry_covers_
> every_op` reds immediately — that red is the rule working. The fix is a
> one-line addition to `DEFAULT_BUILDER_TOOLS` (and its module `__all__`) rather
> than a weakening of the test.

> ✅ ALWAYS decide surface 4's "GUI control-or-documented-exception" —
> `test_builder_gui_coverage.py` forces the choice. The GUI renders every
> `paramSpecsFor` spec as a number input; a boolean knob is a hard-coded toggle in
> a section builder; a knob left form-invisible (an advanced or rare lever) is an
> entry in that test's `GUI_EXCEPTIONS` dict carrying a one-line justification
> rather than an unrecorded omission. A new op with neither a control nor an
> exception reds the pin, naming the op and the two remedies.

### 10.7.1 The declarative knob registry

The six surfaces above are the *op-level* net (every op has a dispatch arm, a
copilot tool, a GUI control). One layer finer sits the **per-field knob
registry**, which makes the **field declaration the source of truth** for a
scoring or proposer knob. Without it the same field would be mirrored across
seven hand-kept sites, and the omit-at-default set would be a hand-maintained
literal that a typo could silently corrupt.

Each field on `ScoringWeights` and its nested config dataclasses is exposed by
`core/scoring_config.py::contract_knobs()`. The registry is derived at import
time from `dataclasses.fields()`; no generated source is checked in. Fields
carry `dataclasses.field(metadata=_knob(...))` declaring:

- **`omit_at_default: bool`** — the field is dropped from the contract
  canonical form while it holds its default. The canonicalizer's omit set
  (`epoch/contract.py::_SCORING_OMIT_AT_DEFAULT_FIELDS`) is **derived** by
  `omit_at_default_fields()` rather than hand-maintained.
  `test_knob_registry.py::test_derived_omit_set_equals_frozen_literal` pins the
  derived set to a frozen literal, so a metadata typo reds THAT test (loudly,
  per-field) instead of silently moving the CONTRACT hash for every epoch.
- **`builder_op: str | None`** + optional **`builder_arg`** — the builder op
  that exposes the knob (e.g. `"set_proposer_quality"`), and the arg name when
  it differs from the field name (e.g. `screen_entries` is the `entries` arg of
  `set_screening`).

`ScoringWeights.goldfive` registers only the optional document as one Builder
operation. Goldfive's `RuntimeConfigDocument` owns every nested field. Do not
mirror those fields into Zicato's dataclasses, knob registry, API dispatch, or
GUI controls; update Goldfive's schema and scaffold instead.

The canonicalizer and the registry-driven completeness guard both consume the
same runtime registry. The latter,
`test_knob_registry.py::test_every_builder_op_knob_is_fully_wired`, walks the
metadata and asserts every `builder_op` knob is wired through all **five**
remaining touchpoints — (a) the op signature, (b) the API dispatch entry, (c)
the copilot tool arg, (d) a `runOp('<op>', {…})` GUI row in `views/builder.js`,
and (e) an arg-level assertion in `test/builder.test.mjs` — by
introspection/source-scan. Forgetting ANY one reds this ONE test with a message
naming exactly which touchpoint is missing for which knob (e.g. *"knob
'genealogy' … is missing touchpoint (b): an API dispatch entry"*).

> ✅ **To add an omit-at-default or proposer-quality knob: declare the field and
> its `_knob(...)` metadata, then let the runtime registry feed canonicalization
> and let the guard test name every remaining touchpoint.** Run
> `test_every_builder_op_knob_is_fully_wired` and fix each named gap until it is
> green. You do not have to remember the five sites; the test enumerates the ones
> you missed. The ops are **not** code-generated from the metadata: the registry
> enforces the wiring rather than producing it. The
> op-level pins (`test_builder_gui_coverage.py`) and the serializer-completeness
> table (`test_contract_serializer_completeness.py`) stay as the coarser nets.

The recipe that walks all six op-level surfaces is §10.8.

---

## 10.8 Recipe: add a builder op end-to-end

Goal: add a new editable contract knob to the builder so the form, the copilot,
the cost meter, and `validate` all know about it — satisfying the full-coverage
rule. The worked example: exposing a hypothetical
`set_gate(min_holdout_confirms=…)` knob. The steps generalize to any knob.

1. **Write the op in `operations.py`.** Add (or extend) a `set_*` function.
   Mutate through `_replace_scoring` (or the entry helpers) — never assign a
   draft field for a scoring knob. Compare to the current value before recording
   the change; validate at the boundary and raise `ValueError` (not a silent
   coerce) on bad input; return a `DraftPatch(op="set_…", changed={…})` that
   records ONLY fields you moved. If your knob's natural "off" is `None`, pick a
   different "unchanged" sentinel and document it (§10.2 trap). Add the function
   name to the module `__all__`.
2. **Add the dispatch arm in `api.py::_dispatch_op`.** One `if op == "set_…":`
   arm that pulls typed args out of the request dict (use `_opt_int` / `_opt_bool`
   for the tri-state numeric/boolean knobs) and calls your op. The chain falls
   through to `raise ValueError(f"unknown builder op {op!r}")`, which the handler
   turns into a 400, so a missing arm fails a test rather than passing as a
   silent no-op.
3. **Add the copilot tool in `copilot_tools.py`.** Write a module-level function
   that pulls the draft from `_active_context()`, calls your op, and returns
   `_result_json(_summary(patch))` (the shared envelope). Its docstring is
   model-facing (ADK surfaces it as the tool description) — state the knob, its
   units, and its bounds. Append the function to `DEFAULT_BUILDER_TOOLS` and to
   the module `__all__`.
4. **Add the GUI control (or document the exception).** In `builder/model.js`,
   if the knob is a structure param, add a spec to `paramSpecsFor` (it renders
   as a number input keyed on `min`/`max`/`step`/`int`/`removeAtZero`). If it is
   a boolean or a scoring-block field, add a hard-coded control to the relevant
   section builder in `views/builder.js` (a `checkInput` toggle, a `numInput`,
   or a `<select>`), wired to `runOp('set_…', {…})` (or `postOp('set_…', {…})`
   for the board-editor ops). If the knob is meant to stay form-invisible, add it
   to `tests/test_builder_gui_coverage.py::GUI_EXCEPTIONS` with a one-line
   justification — the full-coverage rule's "documented exception".
   `runOp`/`postOp` with the op string is what the coverage pin greps for, so
   wire the string
   literally (never build the op name dynamically).
5. **Add the cost line if it changes the schedule.** If the knob multiplies
   per-round board runs, add a `CostLine` in `operations.py::estimate_cost` —
   the meter's only implementation (the honest-cost-meter rule; §10.3.3) — and
   a fixture that reaches it in
   `tests/test_builder_cost_envelope_correspondence.py`. Label evaluation
   LLM-call terms and leave them out of the headline sum.
6. **Add the `validate` consideration if it can be unsound.** A value is
   unsound when it stops the contract from telling a real improvement from
   noise — a margin that cannot clear the noise floor, or a field that degrades
   a structure. Emit a `Warning` from `operations.py::validate` with a stable
   `code` and the right `severity`, reserving `refuse` for real unsoundness.
   Recommend-only — never block (the recommend-only rule).
7. **Contract accounting.** The knob lives on `ScoringWeights` (or a nested
   block). Confirm it is omitted-at-default from the canonical scoring form
   (`epoch/contract.py`'s omit-at-default set) so existing epochs do not roll
   retroactively, and that a non-default value DOES roll the epoch — the builder
   diff and `apply(rolled=…)` derive from that canonical form. See
   03-contract-and-epochs.md §3.4 (the omit-at-default discipline).
8. **Tests — five kinds:**
   - *op semantics* in `tests/test_builder_operations.py` (the `changed` map,
     the no-op-when-unchanged behaviour, the raise-on-bad-input);
   - *dispatch* in `tests/test_builder_api.py` (the knob dispatches through
     `POST /builder/op` and lands on the serialized draft; a malformed arg is a
     400);
   - *copilot registry* is auto-covered by
     `test_default_builder_tools_registry_covers_every_op` — run it to confirm
     your tool is in the set;
   - *GUI coverage* is auto-covered by
     `test_builder_gui_coverage.py::test_every_write_op_has_a_gui_control_or_exception`
     — run it to confirm your knob has a control (or a justified exception);
   - *cost correspondence* — if you added a cost line, give it a fixture in
     `tests/test_builder_cost_envelope_correspondence.py`, whose coverage
     assertion reds until the term is reached.
9. **Verify:**
   ```bash
   uv sync --all-extras
   uv run pytest tests/test_builder_operations.py tests/test_builder_api.py \
       tests/test_builder_copilot.py -x -q
   uv run pytest tests/test_epoch_contract.py \
       tests/test_contract_serializer_completeness.py -q   # omit-at-default + roll
   make node-test        # the console behaviour suite
   uv run ruff check src/zicato/contract_draft/ src/zicato/builder/ \
       && uv run mypy src/zicato/contract_draft/ src/zicato/builder/
   ```
   If you skipped step 3, `test_default_builder_tools_registry_covers_every_op`
   reds — the copilot cannot reach your knob. If you skipped step 4,
   `test_builder_gui_coverage.py` reds — the knob has no GUI control and no
   documented exception. If you skipped step 5's JS mirror, the node suite reds —
   a frozen-contract preview would quote the wrong price.

**Definition of done.** The knob is editable from a form control and a chat
turn, it prices correctly on both implementations of the meter, and `validate`
warns if it can be unsound. A non-default value rolls the epoch, while the
default leaves the canonical contract form byte-identical so existing epochs do
not roll. All five test kinds are green.

---

## 10.9 The CLI — an auto-discovered command tree

The `zicato` executable is `zicato.cli:main`, which builds a `click` root group
by **auto-discovery**: every importable module under `zicato.cli.commands` is
imported, and each top-level `click.Command` / `click.Group` it defines is
attached to the root. A contributor ships one command per file without ever
editing the root:

```python
# src/zicato/cli/discovery.py — build_cli_root (tail)
    for module_name in _iter_command_modules():
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:  # pragma: no cover - exercised via tests
            logger.warning(
                "zicato.cli: failed to import command module %r: %s",
                module_name,
                exc,
            )
            continue

        commands = _extract_commands(module)
        if not commands:
            continue
```

Three robustness rules hold: a broken command module logs a warning and
is skipped (one bad plugin never kills the CLI); a module with no `click.Command`
is silently ignored (modules can hold helpers); and a duplicate command name is
refused first-wins. The root is a `ZicatoGroup` (a `click.Group` subclass) that
renders `--help` in two labelled sections — the happy path (`init`, `evolve`)
and the advanced/debugging commands — instead of one flat list.

### 10.9.1 The command inventory

| File | click command / group | Subcommands |
|---|---|---|
| `init.py` | `init` | — |
| `evolve.py` | `evolve` | — |
| `board.py` | `board` (group) | `add`, `list`, `remove`, `audit`, `preflight`, `judges` |
| `epoch.py` | `epoch` (group) **+** `repair-epoch-goals` | epoch: `new`, `close`, `list`, `switch`, `gc`, `set-goal` |
| `config.py` | `config` (group) | `env` |
| `dashboard.py` | `dashboard` | — |
| `builder.py` | `builder` | — |
| `health.py` | `health` | — |
| `mutations.py` | `mutations` | — |
| `propose.py` | `propose` | — |
| `reflect.py` | `reflect` (group) | `run`, `report`, `apply` — board reflection (`docs/design/BOARD-REFLECTION.md`); `apply` stages the edit through `zicato.reflection.apply`, which reaches the draft and its operations in the library package `zicato.contract_draft` rather than through the builder driver |
| `register.py` | `register` | — |
| `tournament.py` | `tournament` | — |
| `reindex.py` | `reindex`, `reindex-generations`, `repair-tournament-fk` | — |
| `analyze_telemetry.py` | `analyze-telemetry` | — |
| `regenerate_report.py` | `regenerate-report` | — |
| `repair_judge_losses.py` | `repair-judge-losses` | — |
| `repair_v0_baseline.py` | `repair-v0-baseline` | — |

A module publishes multiple top-level commands via its `__all__` (`epoch.py`,
`reindex.py`). `init_cmd.py` is a helper module (`initialize_workspace`) rather
than a command — it has no `click` decorators, and `discovery.py` skips
`_`-prefixed modules regardless.

> ✅ ALWAYS ship a new command as a new file under `zicato/cli/commands/` with a
> top-level `@click.command(name="…")` (or a `@click.group`). Do not edit
> `discovery.py` or the root group — that is the whole point of auto-discovery.
> If the command is off the happy path (everything except `init`/`evolve`), you
> are done; `ZicatoGroup` files it under "Advanced commands" automatically.

### 10.9.2 CLI.md is a GENERATED artifact — `--help` is canonical

`docs/design/CLI.md` is a reference doc kept in lockstep with `zicato --help`.
It is **not** machine-generated by a script (there is no generator command in the
tree); it is hand-reconciled by running `uv run zicato <command> --help` for
every command and mirroring the output. Its header states the contract:

```
> **Generated doc.** This file is generated to match `zicato --help` and
> should be regenerated whenever the CLI changes. The live `--help` output
> is the source of truth; if this document and the binary ever disagree,
> trust `zicato --help` / `zicato help <command>`.
```
— `docs/design/CLI.md` (header)

> ⚠️ TRAP — `--help` is the source of truth and CLI.md is the mirror. When you
> add or change a command, flag, default, or help string, re-run `uv run zicato
> … --help` and update CLI.md to match. The update includes its "Last reconciled
> …" date and its command-list census; that census enumerates every command by
> name so a phantom or renamed command is caught. A CLI change that leaves
> CLI.md stale is a
> documentation regression rather than a harmless omission. See
> 11-testing.md §11.7.3 "CLI-HELP" for the byte-check that guards the help text
> itself.

---

## 10.10 Flags → pins → config knob → workers

The single most bug-prone thing about the CLI is how an operator flag reaches
the code that consumes it — especially code that runs in a **worker
subprocess**, a different OS process than the one that parsed the flag. zicato
solves this with a **process-pinned override** layer, and the rule that falls
out of it is the config-pins-not-environment rule: *no environment variable is a
configuration knob*. The config module states it at the top:

```python
# src/zicato/config.py (module docstring, excerpt)
:func:`load_config` honours NO environment variables; the former
operator-env surface was deleted outright:
...
What remains is a small MERITED set of environment variables zicato
deliberately touches — each one an actual process-boundary contract,
not a configuration knob
```

### 10.10.1 The four-hop path

**Hop 1 — flag to pin.** A command validates its flags once at startup and pins
them as a nested `{section: {field: value}}` override. `evolve` does this in
`_pin_config_flags`:

```python
# src/zicato/cli/commands/evolve.py — _pin_config_flags (excerpt)
    if parallelism is not None:
        pins.setdefault("runtime", {})["parallelism"] = parallelism
    if aux_call_timeout is not None:
        pins.setdefault("aux", {})["call_timeout_s"] = aux_call_timeout
    ...
        pin_overrides(pins)
```

**Hop 2 — pin into the config tree.** `pin_overrides` validates eagerly: an
unknown section or field raises at the pin site rather than surfacing later as a
silently-defaulted knob. It merges into a process-wide store, and `load_config`
layers the pins on top
of the dataclass defaults — so every later `load_config()`, however deep in the
call graph, sees the flag:

```python
# src/zicato/config.py — pin_overrides (docstring, excerpt)
    This is the bridge from CLI flags to the config tree: a command
    validates and pins its flag values once at startup, and every later
    :func:`load_config` call — however deep in the call graph — sees
    them layered on top of the dataclass defaults ...

    The tournament runner serialises the current pins into every worker
    args file and the worker re-pins them at startup, so a pinned knob
    consumed inside the worker subprocess (for example, board-unit
    parallelism) crosses the process boundary without an environment variable.
```

For the rare call site that must tell "explicitly pinned" from "at its default"
(e.g. `runtime_factory.make_runtime_config`, where `--parallelism` outranks the
`config.json` value but the mere default must not), there is `pinned_override(
section, field)` returning the pinned value or `None`.

**Hop 3 — pins cross into the worker through the args file.** The
tournament runner writes the current pins into each worker's JSON args file
under a `config_pins` key:

```python
# src/zicato/tournament/worker_transport.py — _config_pins
def _config_pins() -> dict[str, dict[str, Any]]:
    ...
    return get_pinned_overrides()
```

The runner writes `"config_pins": _config_pins()` into the args file it hands
the worker. A flag consumed inside the worker therefore crosses the process
boundary through the args file rather than an environment variable.

**Hop 4 — the worker re-pins before any `load_config`.** The fresh worker
interpreter reads the args file and re-pins before it touches config:

```python
# src/zicato/_tournament_worker.py (excerpt)
    config_pins = args.get("config_pins")
    if config_pins:
        ...
        pin_overrides(config_pins)
```

> ⛔ NEVER add an environment variable to carry an operator knob across the
> worker boundary (the config-pins-not-environment rule). A flag value read via
> `os.environ` in the worker is invisible to the orchestrator's validation,
> cannot be told apart from a default, and drifts from the
> `config.json` fallback. Pin the flag (`pin_overrides`), and let the runner's
> `config_pins` args-file channel carry it. The worker re-pins; `load_config`
> then resolves it identically on both sides of the boundary.

> ⚠️ TRAP — a pinned value must be JSON-serialisable, because it round-trips
> through the worker args file. Every CLI-flag value already is (ints, floats,
> strings, bools). If you pin a non-JSON value, `get_pinned_overrides()` →
> args-file write silently drops or corrupts it and the worker runs on the
> default. Pin the primitive, resolve the object worker-side.

### 10.10.2 The merited env-var set — `zicato config env`

The env vars zicato *does* touch are a small MERITED set, each one a
process-boundary contract and never a knob. `zicato config env` prints the set
by reading `describe_env_vars()`, so the command can never drift from the code.
Each entry carries a **boundary-kind role** (NOT a process label):

| Role | Meaning | Members |
|---|---|---|
| `harness-contract` | set by zicato for the system under test — part of the run contract | `ZICATO_RUN_SCRATCH_DIR` |
| `internal-handoff` | set and restored by zicato to hand a value across its own processes | `ZICATO_HARMONOGRAF_URL`, `ZICATO_HARMONOGRAF_GRPC` |
| `secrets-boundary` | Configuration records a variable name while its credential value remains in the process environment | `<models.<role>.api_key_env>`, Goldfive names returned by `RuntimeConfigDocument.secret_env_names`, and `<runtime.worker_env_passthrough>` when used for credentials |
| `test-toggle` | CI / test switches; never read on an operator path | `ZICATO_SKIP_HOOK_CHECK`, `ZICATO_PARITY_UPDATE` |

The role is a *boundary taxonomy* of five values; which process
sets/reads a variable is prose in the entry's `description` (e.g.
`ZICATO_RUN_SCRATCH_DIR` is "Set BY the tournament worker FOR the system
under test"). The backing type is `EnvVarInfo(name, role, description)`.

> ⛔ NEVER add an operator tuning knob to `_MERITED_ENV_VARS`. The set is for
> process-boundary contracts only. If your feature needs an operator knob, it is
> a CLI flag (pinned) plus a `config.json` block, the posture every operator
> threshold takes. A new env var must justify its role from the five above, or
> it does not belong.

### 10.10.3 Recipe: add a CLI flag the right way

Goal: add an operator flag to `evolve` (or any command) so it shadows a
`config.json` knob and reaches the workers correctly.

1. **Add the config field first.** The knob lives on a `ZicatoConfig` sub-config
   (`runtime`, `aux`, `integration`, `health`, …) with a safe default, and — if
   operators should be able to set it without the flag — a `config.json` reader
   in `workspace_loader`. The flag *shadows* this knob; it is not a second
   source of truth.
2. **Add the `@click.option`.** In the command file, add the option with a help
   string that NAMES the config knob it shadows (the CLI-HELP convention — every
   flag's `--help` says which `config.json` knob it overrides and that the flag
   wins). Give it an exact type (`int | None`, `float | None`) so "unset"
   is distinguishable from a real value.
3. **Pin it.** In the command's pin helper (`_pin_config_flags` for `evolve`),
   map the non-`None` flag to its `{section: {field: value}}` pin and ensure
   `pin_overrides(pins)` runs once at startup. Do NOT read the flag again
   downstream — consume it via `load_config()` / `pinned_override()`.
4. **Confirm worker propagation IF the knob is consumed in a worker.** The
   runner already threads ALL pins via `config_pins`; you get propagation for
   free. Your job is to prove it: add a test that pins the override, builds the
   worker args (or calls `_config_pins()`), and asserts your `section.field` is
   present in the args payload and re-pins correctly worker-side. Both halves —
   flag→config threading and the `config_pins` worker payload — live in
   `tests/test_cli_config_flags.py`; add yours beside them.
5. **Help text + CLI.md.** Re-run `uv run zicato <command> --help`, confirm the
   flag reads correctly, then reconcile `docs/design/CLI.md` (§10.9.2) — update
   the option, its default, and the "Last reconciled" date.
6. **Verify:**
   ```bash
   uv run pytest tests/test_config.py tests/test_cli_config_flags.py -x -q
   uv run zicato evolve --help          # eyeball the new flag + its shadow note
   # CLI-HELP parity gate (11-testing.md §"parity gates"):
   bash tools/parity.sh --only CLI-HELP
   ```
   If you skipped step 3 and read the flag inline, the worker never sees it
   (the config-pins-not-environment rule) — the value silently reverts to the
   `config.json` default inside every duel. If you skipped the CLI.md reconcile, the CLI-HELP parity
   gate or the doc census reds.

**Definition of done.** The flag shadows a real `config.json` knob, wins over it,
reaches worker subprocesses via `config_pins` (proven by a test), reads correctly
in `--help`, and CLI.md matches the binary.

---

## 10.11 The library facade and the import boundary

zicato is a library first. The public surface is declared in
`src/zicato/__init__.py` as a **lazy facade**: a dict mapping each public name to
its home module, resolved on first access by a module-level `__getattr__`. The
surface is limited to evolve entry points, harness protocols, board/config
loaders, and scoring types. All three evolve entry points name
`zicato.orchestrator` as their home — the dispatch surface over the round
pipeline, so the facade pins no module inside `zicato.evolve`. The
reasoning-aware model boundary is an advanced API at `zicato.reasoning`.
`__all__` is derived from `_EXPORTS`.

Advanced APIs live in their owning subpackages, and the facade carries no
forwarding aliases to them.

### 10.11.1 The laziness contract

```python
# src/zicato/__init__.py — the lazy resolver
def __getattr__(name: str) -> Any:
    """Resolve a facade name lazily from its home module."""
    try:
        module_name, attr = _EXPORTS[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    module = importlib.import_module(module_name)
    value: Any = module if attr is None else getattr(module, attr)
    globals()[name] = value  # cache: subsequent access skips __getattr__
    return value
```

Two properties are load-bearing (the lazy-pure-facade rule), and both are
machine-pinned in `tests/test_public_api.py`:

- **`import zicato` imports only `zicato`.** The module body imports nothing but
  `importlib` and `typing`; every export resolves lazily on first touch. This is
  what keeps the CLI's fast `--help` path cheap — help must not pay for the
  orchestrator. The test runs a *fresh interpreter* and asserts the only
  `zicato*` entry in `sys.modules` after `import zicato` is `zicato` itself:

```python
# tests/test_public_api.py — the laziness pin
    loaded = json.loads(out.stdout)
    assert loaded == ["zicato"], f"import zicato eagerly pulled: {loaded}"
```

- **Every facade name is `is`-identical to its home attribute.** The facade is a
  pure re-export layer, never a fork:

```python
# tests/test_public_api.py — the identity pin
    for name, (module_name, attr) in zicato._EXPORTS.items():
        exported = getattr(zicato, name)
        home = importlib.import_module(module_name)
        expected = home if attr is None else getattr(home, attr)
        assert exported is expected, (...)
```

The `TYPE_CHECKING` block at the bottom of `__init__.py` re-imports every name
in redundant-alias form (`from … import X as X`) so mypy and IDEs see the surface
that `__getattr__` provides only at runtime. Because it is under
`if TYPE_CHECKING:`, none of it runs at import time — laziness is preserved.

### 10.11.2 How to add a public name

There is no CONTRIBUTING doc; the procedure is code-enforced by three
touch-points, and the tests catch a half-done addition:

1. **Facade entry.** Add a `"name": ("home.module", "attr")` row to `_EXPORTS`
   in `src/zicato/__init__.py`. (`__all__` and `__dir__` update automatically —
   they are derived from `_EXPORTS`.)
2. **`TYPE_CHECKING` mirror.** Add the matching `from home.module import attr as
   attr` under the `TYPE_CHECKING` block, or mypy/IDEs will not see the name.
3. **Identity test — already automatic.** `test_every_facade_name_resolves_to_
   its_home_module` iterates `_EXPORTS`, so it covers your new row the moment you
   add it; `test_all_lists_exactly_the_declared_surface` pins `__all__`. A name
   in `_EXPORTS` but not `TYPE_CHECKING` (or vice-versa) is caught by these plus
   mypy.

> ✅ ALWAYS put a name on the facade ONLY if it is a genuine driver-facing seam.
> The facade is the *declared* public surface: the evolve loop, harness
> protocols, board/config loaders, and scoring types. Other APIs remain
> reachable at their owning subpackages. Do not add forwarding aliases.

### 10.11.3 The import-linter contracts

`import-linter` enforces the boundaries (`uv run lint-imports`, wired into
`make check` and CI — the green-gates rule, `01-orientation.md §4`). Every
contract in `pyproject.toml` is of type `forbidden`, and they fall into two
groups. Prose cites a contract by its name, which is the string
`lint-imports` prints.

The driver boundary:

| Name | Forbids |
|---|---|
| the library must not import the drivers (cli / dashboard / builder) | any of the 36 listed library packages → `zicato.cli` / `zicato.dashboard` / `zicato.builder` / `zicato.tui` |
| tui driver: no import of the other drivers (it speaks HTTP, not Python) | `zicato.tui` → `zicato.cli` / `zicato.dashboard` / `zicato.builder` |
| dashboard driver: no import of the cli | `zicato.dashboard` → `zicato.cli` |
| builder driver: no import of the other drivers | `zicato.builder` → `zicato.cli` / `zicato.dashboard` |
| cli driver: no direct import of the builder | `zicato.cli` → `zicato.builder` (`allow_indirect_imports = true`) |
| the query layer stays dashboard-free | `zicato.query` → `zicato.dashboard` |

The cuts inside the library:

| Name | Forbids |
|---|---|
| the proposer's patch validator has no path to the board | `zicato.proposer.validate` → the board, the judge runtime, the emulator, the adapters, the adapter factory, the tournament worker |
| the modelling and execution layer does not import the loop, the reports, the diagnostics, the read layer, the contract draft, or the drivers | any of the 24 listed modelling and execution packages → `zicato.analyzer` / `zicato.check` / `zicato.contract_draft` / `zicato.evolve` / `zicato.health` / `zicato.orchestrator` / `zicato.query` / `zicato.reflection` / the four drivers |
| the shared primitives import nothing else in the library | `zicato.aux_timeout` / `zicato.config` / `zicato.import_path` / `zicato.integrations` / `zicato.logging_stream` / `zicato.storage` / `zicato.util` → every other top-level package |

The library-must-not-import-the-drivers contract lists every library package
explicitly as a `source_module` — including `zicato.query`, which is
**library** code (the workspace query layer the dashboard consumes) rather
than a driver. The cli-no-direct-import-of-the-builder contract sets
`allow_indirect_imports = true` on purpose: the CLI legitimately reaches the
builder *transitively* through the two declared edges (cli → dashboard.server →
builder.api mount); what it forbids is the CLI growing its OWN direct builder
dependency.

The two — and only two — permitted driver→driver edges fall out of these
contracts: `cli → dashboard` (the CLI launches the server) and `dashboard →
builder` (the server mounts the builder REST routes).

The modelling-and-execution contract declares a cut, not an ordering. Its
source list is the code that defines the data model, runs harnesses, scores
entries and reads and writes the workspace; its forbidden list is the code
that decides a round, renders reports, diagnoses a workspace, serves reads,
adjudicates the evaluation contract, edits a contract as a draft, or drives a
session. Seven library packages are deliberately outside the source list
because each imports
something in the forbidden set: `epoch` and `proposer` import the report
renderer, `index` and `runtime` import `evolve.settlement_recovery`, and
`tournament`, `workspace_loader` and `_tournament_worker` reach the renderer
through those. Packages on the same side of the cut stay free to import each
other, and no contract orders them: the shared type model puts
`core.scoring_config` on a path from every package to the epoch serialiser,
the scoring transforms and the evidence gate, so any finer ordering would
fail for reasons that describe the type model rather than the layering.

> ⛔ NEVER add a driver→driver edge. You almost certainly do not need one. If the
> CLI needs a builder capability, it reaches it through the dashboard mount (the
> declared transitive path), or the capability belongs in the library where both
> can import it. Adding, say, a `cli → builder` direct import reds the
> cli-no-direct-import-of-the-builder contract — and the fix is architectural
> (move the shared code into a library package), never loosening the contract. A
> genuine new edge is a design change requiring its own PR and a rewrite of the
> contract with a documented rationale.

> ✅ ALWAYS add a new library package to the `source_modules` list of the
> library-must-not-import-the-drivers contract when you create one, and to the
> modelling-and-execution contract's list if it imports nothing above the cut.
> Both lists are explicit rather than wildcards, so a new package is a conscious
> addition; a package omitted from a list is silently unprotected and can grow a
> forbidden import nobody notices until it ships.

### 10.11.4 TID251 — the banned private-reach paths

Ruff's `flake8-tidy-imports` (TID251) bans twenty specific cross-module private
reaches. Every banned name has a public seam at its owning module, and the ban
stops the underscore path from reappearing:

```toml
# pyproject.toml — [tool.ruff.lint.flake8-tidy-imports.banned-api] (excerpt)
"zicato.orchestrator._compute_field_diversity".msg = "moved: use zicato.selection.diversity.compute_field_diversity"
"zicato.runtime._atomic".msg = "deleted shim: use the public zicato.storage face (read_json / atomic_write_json / atomic_write_text / atomic_claim)"
"zicato.epoch.contract._scoring_to_canon".msg = "promoted: use zicato.epoch.contract.scoring_to_canon"
"zicato.board.jsonl._entry_to_dict".msg = "promoted: use zicato.board.jsonl.entry_to_dict"
```

The storage package owns its private `_atomic` module, so `src/zicato/storage/**`
and its unit test carry a per-file TID251 ignore; everyone else goes through the
public `zicato.storage` face.

> ⚠️ TRAP — when you promote a private helper to a public name, add a TID251 ban
> for the private path in the same commit. Otherwise the underscore import
> reappears the moment someone copies a call site written before the promotion,
> and the promotion's whole point — one honest public seam — is undone silently.
> The ban message names the replacement, so the offender gets an actionable
> error rather than a mystery.

---

## 10.12 Packaging

zicato is a hatchling-built wheel with a `src/` layout. Four packaging facts an
extender touches:

**Extras.** Narrow profiles expose individual integrations. `observability`
composes the browser, builder route, terminal renderer, and live telemetry;
`all` installs every shipped runtime feature. The base retains the core loop
and JSONL telemetry. `docs/design/INSTALL-PROFILES.md` is the profile contract.
The all-extras sync rule (`01-orientation.md §4`) applies here: **always `uv
sync --all-extras`** — a bare `uv sync` deletes dev tooling from `.venv`.

**The uv workspace member.** The vendored dogfood targets under `examples/` are a
separate distribution (`zicato-examples`), declared as a uv workspace member so
`uv sync --all-extras` installs it editable alongside zicato — no PYTHONPATH
hacks. The tests import it as `zicato_examples.*`; it is never shipped in the
zicato wheel:

```toml
# pyproject.toml
[tool.uv.workspace]
members = ["examples"]

[tool.uv.sources]
zicato-examples = { workspace = true }
```

This is the precedent for adding another vendored distribution: make it a
workspace member and map it in `[tool.uv.sources]`.

**The wheel + its exclusions.** The wheel ships `src/zicato` (hatchling strips the
`src/` prefix) and *excludes* `src/zicato/dashboard/static/test` — the JS test
harness is a developer tool, run by `tests/test_dashboard_js.py`, and stays out
of the shipped bundle.

**The supervisor binary — `_bin` ownership.** `zicato evolve` spawns a compiled
`zicato-supervisor` watchdog. To make it available to every install, a custom
hatch build hook compiles the Rust crate and bundles the binary at
`zicato/_bin/zicato-supervisor` inside the wheel:

```python
# hatch_build.py — SupervisorBinaryBuildHook.initialize (tail)
        dest = root / _BUNDLED_REL
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(built, dest)
        dest.chmod(0o755)
        ...
        # force-include guarantees the binary lands in the wheel even
        # though it is generated (not VCS-tracked). The wheel path is
        # zicato/_bin/zicato-supervisor (src/ prefix stripped).
        artifact = str(dest)
        build_data.setdefault("force_include", {})[artifact] = "zicato/_bin/zicato-supervisor"
```

The hook is **best-effort by design**: no `cargo`, or a missing crate (e.g. an
sdist that excluded `crates/`), logs a warning and leaves the wheel without the
binary. The CLI's `_resolve_supervisor_binary` then falls back to the
`--supervisor-binary` flag, the system `PATH`, and — for checkouts — the
workspace `target/release/` walk. The sdist carries the crate source
(`crates`, `Cargo.toml`, `Cargo.lock`, `hatch_build.py`) so a downstream wheel
build can still run the hook.

> ⚠️ TRAP — the supervisor binary is generated rather than VCS-tracked, so it
> must be `force_include`d (a package glob will not match it) or it never lands
> in the wheel. It is owned by exactly one build target (the wheel); the sdist
> carries source instead and rebuilds at wheel-build time. See 08-supervisor.md
> §8.11 "Build, packaging, and binary resolution" for the runtime fallback
> chain, and the wheel-split row of the deferred register
> (14-goals-and-roadmap.md §4) for the split into `zicato-lib` / `zicato-cli` /
> `zicato-dashboard`, where exactly one wheel would own `_bin/`.

---

## 10.13 Cross-references

- 01-orientation.md §1.3 "Library first, three drivers on top" — the shape this
  chapter formalizes; §4 "The Golden Rules" for the green-gates rule (parity
  gates plus import contracts) and the module-level-callable rule (why pins
  rather than environment variables cross to workers).
- 02-architecture.md §1 "The process topology" — the four processes the pins and
  the environment-variable contracts cross.
- 03-contract-and-epochs.md §3.7 "Computing the hash" and §3.4 "The
  omit-at-default discipline" — what the builder's diff, `rolls_epoch`, and
  `apply` are computed against.
- 04-evaluation-statistics.md §4 "A/A noise-floor calibration" — what the
  builder pre-flight measures and what `margin_below_noise_floor` compares
  against; §6 "The evidence gate" — the `budget × 2 × board` crowning-confirm
  term that dominates the cost meter; §8 "THE RESERVED REPLICATE-BASE LEDGER" —
  the bases the pre-flight draws on.
- 08-supervisor.md §8.11 "Build, packaging, and binary resolution" — the runtime
  fallback chain for the `_bin/` supervisor this chapter's build hook bundles.
- 09-dashboard-and-query.md §9.4.3 "The route table" — where the server mounts
  the builder REST routes documented here, and the payload discipline the
  builder envelope obeys.
- 11-testing.md §11.7 "The six parity gates, one by one" (including the CLI-HELP
  gate), §11.9 "Node behaviour-suite conventions" (including §11.9.5, the
  cross-language correspondence pattern this chapter's cost meter uses), and
  §11.8 "The import contracts + the TID251 bans".
- 13-recipes.md — the short-form cookbook; §10.8 and §10.10.3 here are the
  long-form builder-op and CLI-flag procedures.
- `docs/design/TOURNAMENT-BUILDER.md` — the full builder design record (the
  operations layer, the copilot, and the form GUI; §4 "The consequence-forward
  principle"); `docs/design/CLI.md` — the generated command reference kept in
  lockstep with `--help`.
