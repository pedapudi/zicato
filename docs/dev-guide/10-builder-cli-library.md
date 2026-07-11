# 10 — The Builder, the CLI, and the Library boundary

> **Covers.** The three *drivers* that sit on top of the zicato library and the
> boundary that keeps them honest: (1) the **builder** — the contract IDE, a
> deterministic draft/ops/dispatch/copilot backend that the form GUI and the
> chat copilot both drive through ONE mutation surface, plus its honest cost
> meter, its statistical pre-flight, its margin-vs-floor validate, and its
> fork/compare slots; (2) the **CLI** — the auto-discovered `click` command
> tree, the flag → pin → config-knob → worker-propagation layer, `zicato config
> env`, and the generated `CLI.md`; (3) the **library facade** — the ~37-name
> lazy `zicato` surface, the five import-linter contracts, the TID251 bans, and
> how a name (or a driver edge) is added; and (4) **packaging** — the wheel, the
> extras, the uv workspace member, and the supervisor-binary build hook.
>
> **Prerequisites.** 01-orientation.md §"Library first, three drivers on top"
> (the shape this chapter formalizes) and §"The Golden Rules" (G5 import
> contracts, G6 omit-at-default, G9 module-level callables across the worker
> boundary); 02-architecture.md §"orchestrator vs workers vs supervisor vs
> dashboard" (the four OS processes the pins and the env-var contracts cross);
> 03-contract-and-epochs.md §"The contract hash" (what "rolls the epoch" means —
> the builder's entire reason to exist); 04-evaluation-statistics.md §"The A/A
> noise floor" (what the pre-flight measures and what the margin must clear);
> 06-tournament-and-selection.md §"The evidence gate" (the `budget × 2 × board`
> term that dominates the cost meter).
>
> **Invariants introduced in this chapter.** Each is load-bearing; the numbered
> callouts below expand every one.
>
> | ID | Invariant |
> |----|-----------|
> | L1 | **One mutation surface.** Every editable contract change flows through exactly one function in `zicato/builder/operations.py`. The form, the copilot, and the REST dispatch all call the same op — never a second edit path. |
> | L2 | **Full-coverage.** A new contract knob is not shipped until it has an op (`operations.py`) + a dispatch arm (`api.py::_dispatch_op`) + a copilot tool (`copilot_tools.py::DEFAULT_BUILDER_TOOLS`) + a GUI control-or-documented-exception + a cost line if it changes the schedule + a `validate` consideration if it can be unsound. Machine-pinned including the GUI surface — the op↔dispatch↔copilot triple by `test_default_builder_tools_registry_covers_every_op`, and the GUI-control-or-exception by `test_builder_gui_coverage.py`. |
> | L3 | **The cost meter is honest and twinned.** Every board-run multiplier the runtime will spend gets a `CostLine`; auxiliary LLM calls are labelled and excluded from the board-runs headline; the Python estimator and the JS twin must agree (a py↔js parity test). |
> | L4 | **Recommend-only.** The builder never hard-blocks `apply`. Even a `refuse`-severity warning or a `refuse` pre-flight verdict informs the operator — it never gates the write. |
> | L5 | **The builder never rolls the epoch and never starts a live evolve.** `apply(confirm=True)` writes the contract source files and lets the auto-epoch machinery roll on the next resolve; the copilot's apply tool is always `confirm=False`. |
> | L6 | **Flags cross the worker boundary via `config_pins`, never an env var.** No environment variable is a configuration knob; operator knobs are CLI flags (pinned via `pin_overrides`) and `config.json` blocks. |
> | L7 | **The library facade is lazy and pure.** `import zicato` imports only `zicato`; every facade name is `is`-identical to its home-module attribute; the `TYPE_CHECKING` mirror is the static view of the runtime lazy surface. |
> | L8 | **The library never imports a driver.** The only driver→driver edges are `cli → dashboard` and `dashboard → builder`; the five import-linter contracts pin exactly that. |

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
| Builder | `src/zicato/builder/` | mounted by the dashboard at `/builder/*` + `zicato builder` | the deterministic contract-editing backend |

The two allowed driver→driver edges are `cli → dashboard` (the CLI launches the
server and resolves its static bundle) and `dashboard → builder` (`server.py`
mounts the builder's REST routes). **Everything else between drivers is
forbidden** — §10.11 is the enforcement.

| File | What lives there | Size |
|---|---|---|
| `src/zicato/builder/config.py` | `BuilderConfig` / `BuilderAgentConfig` / `load_builder_config` — `builder.json` | ~210 lines |
| `src/zicato/builder/draft.py` | `TournamentDraft` (the mutable editable contract), `DraftStore` (sessions + named slots), `ContractDiff` | ~450 lines |
| `src/zicato/builder/operations.py` | **THE mutation surface** — every `set_*` op, `estimate_cost`, `validate`, `compare_drafts`, `preflight`, `apply`, and their result dataclasses | ~1,600 lines |
| `src/zicato/builder/api.py` | `_dispatch_op` + the Starlette routes (`builder_routes`) | ~490 lines |
| `src/zicato/builder/copilot.py` / `copilot_tools.py` | the chat copilot (B1b) + its tool registry `DEFAULT_BUILDER_TOOLS` | ~470 lines |
| `src/zicato/dashboard/static/js/views/builder.js` | the form GUI: rail sections, per-section controls, the Review pane | ~950 lines |
| `src/zicato/dashboard/static/js/builder/model.js` | `paramSpecsFor`, the schematic preview model, and the **client-side cost twin** (`estimateCost`) | ~515 lines |
| `src/zicato/cli/discovery.py` | `build_cli_root`, `ZicatoGroup`, the command auto-discovery | ~370 lines |
| `src/zicato/cli/commands/*.py` | one command (or sub-group) per file — the inventory in §10.9 | — |
| `src/zicato/config.py` | `pin_overrides` / `pinned_override` / `load_config` + `describe_env_vars` | ~690 lines |
| `src/zicato/__init__.py` | the lazy `_EXPORTS` facade + `__getattr__` + `TYPE_CHECKING` mirror | 153 lines |
| `pyproject.toml` | the five import-linter contracts, TID251 bans, extras, uv workspace, wheel packaging | 422 lines |
| `hatch_build.py` | the custom build hook that bundles `zicato-supervisor` into the wheel | ~98 lines |

---

## 10.1 The builder as a contract IDE — the three-layer stack

The builder edits an **evaluation contract** (board + proposer brief + scoring +
proposer dir) as a *draft*, previews the cost and epoch-roll consequences, and
writes it back — at which point the ordinary auto-epoch machinery rolls the
epoch on the next resolve. It does all of this with **no LLM dependency in the
data layer** (the copilot is a thin driver on top) and, critically, through one
shared mutation surface. The module docstring states the doctrine:

```python
# src/zicato/builder/operations.py (module docstring, head)
"""Builder operations — the single source of truth for form + copilot.

Every editable change to a :class:`~zicato.builder.draft.TournamentDraft`
flows through one of the operations here. Both the form's direct edits
(B2) and the copilot's tool calls (B1b) call the *same* functions, so
there is exactly one place each mutation's semantics live.
"""
```

There are three layers, top to bottom, and each is a driver of the one below it:

1. **The draft (`draft.py`).** `TournamentDraft` is a deliberately **mutable**
   working copy of a whole contract — `scoring: ScoringWeights`,
   `entries: list[BoardEntry]`, `brief: str`, `proposer_path: Path | None`.
   `DraftStore` keys one draft per `session_id` and holds store-global **named
   slots** for the fork/compare lifecycle (§10.7). A session new to the store is
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
> copilot, or the GUI (invariant **L1**). If a mutation is not expressible as a
> call to an `operations.py` function, the fix is to add (or extend) an op —
> never to reach around it. Two edit paths means two places for the semantics to
> drift, and the second one will not be the one the cost meter and `validate`
> know about.

> ✅ ALWAYS have an op return a `DraftPatch` whose `changed` maps
> `field → {"from": old, "to": new}` only for fields it actually moved. Every
> existing op skips a no-op assignment (it compares to the current value before
> recording the change), so the chat/UI renders a truthful "what changed"
> summary and a re-issued identical edit reads as a no-op, not a phantom change.

### 10.1.1 The draft as the mutable mirror of the frozen contract

`TournamentDraft` is the ONE mutable thing in a subsystem otherwise built on
frozen dataclasses. The design note is explicit about why:

```python
# src/zicato/builder/draft.py (TournamentDraft docstring, excerpt)
Unlike the frozen contract dataclasses in :mod:`zicato.core.types`, a
:class:`TournamentDraft` is deliberately MUTABLE — operations mutate it in
place and return a structured patch describing what changed. A
:class:`DraftStore` keys independent drafts by ``session_id`` so two
concurrent builder sessions never tread on each other.
```

`scoring` is itself a frozen `ScoringWeights`; every `set_*` op **replaces** it
wholesale with `dataclasses.replace(...)` (the helper `_replace_scoring`). Board
entries are likewise replaced, never mutated in place — which is what makes
`DraftStore._copy_draft` a real fork with only a shallow list copy (§10.7).

The draft's `diff_vs_live` and its three canonicalizers (`_board_canon`,
`_brief_canon`, `_scoring_canon`) deliberately **agree with the contract-hash /
epoch-roll rule**: they reuse `zicato.epoch.contract.round_floats` /
`scoring_to_canon`, the same normalizers the contract hash uses. That agreement
is the reason the builder can honestly tell an operator "this edit rolls the
epoch" before they apply — the diff can never report a change the hash would not
see, nor hide one it would (see 03-contract-and-epochs.md §"The contract hash").

> ⚠️ TRAP — the six diff *components* are not all epoch-rollers. `ContractDiff`
> reports `board` / `brief` / `scoring` / `proposer` / `structure` /
> `overfitting`, but `structure` and `overfitting` are **sub-views of
> `scoring`** surfaced separately so the UI can say *which* part of scoring
> moved. `rolls_epoch` is `board or brief or scoring or proposer` — do NOT add
> `structure`/`overfitting` into that OR or you double-count and a
> structure-only edit reports two rolls.

### 10.1.2 `builder.json` — the builder's own config, and secret safety

The builder owns its OWN configuration file, distinct from the workspace
`config.json` and the per-epoch `scoring.json`: `builder.json` records how the
copilot (B1b) reaches a model, which builder skills it composes, and an optional
UI theme. It lives at `<workspace>/builder.json` or
`<workspace>/.zicato/builder.json`; absent ⇒ every field defaults, the model is
empty, and chat is disabled. `BuilderConfig.chat_enabled` is simply
`bool(agent.model)`.

The load-bearing property here is **secret safety**. The config records only the
*name* of the environment variable that holds the API key, never the value, and
the one surface the REST layer serializes is guaranteed not to resolve it:

```python
# src/zicato/builder/config.py — BuilderAgentConfig.to_public_dict
    def to_public_dict(self) -> dict[str, Any]:
        """Serialize for the UI — carries the env-var *name*, never a secret.

        Only :attr:`api_key_env` (a variable name) is emitted; the
        variable's value is never read here, so no credential can leak.
        """
        return {
            "model": self.model,
            "endpoint": self.endpoint,
            "api_key_env": self.api_key_env,
            "call_llm": self.call_llm,
        }
```

This is the same secrets-boundary posture as the merited env-var set (§10.10.2):
the operator NAMES the variable in config; the credential stays in the
environment and is read only at the point of use, never serialized to the UI.

> ⛔ NEVER add an API key, token, or other secret VALUE to `builder.json` or any
> `to_public_dict`. The builder's contract is that config carries the env-var
> *name* only. A field that resolves and returns a secret leaks it to the
> dashboard the moment `GET /builder/config` is served — and `builder.json` is a
> file operators commit.

### 10.1.3 The two front doors — the REST surface and the copilot

Both front doors call the same ops and return the same envelope
(`{draft, patch, cost, warnings, diff}`), which is the concrete form of invariant
**L1**. The REST surface (`api.py::builder_routes`, mounted by the dashboard at
`/builder/*`):

| Method + route | Handler | What it does |
|---|---|---|
| `GET /builder/config` | `builder_config` | `load_builder_config(root).to_public_dict()` (secret-safe) + the server-derived `vocab` (entry kinds / expectation kinds / reads / judge modes / severities / drift kinds — the GUI never hardcodes an enum) |
| `GET /builder/draft?session=ID` | `builder_draft` | the draft snapshot + cost/warnings/diff/slots + `proposer_dirs` (discovered `<workspace_parent>/proposers/*` candidates; degrades to `[]`) |
| `POST /builder/op` | `builder_op` | `{session, op, args}` → dispatch → the shared envelope |
| `POST /builder/apply` | `builder_apply` | `{session, confirm}` → `ApplyResult.to_dict()` |
| `POST /builder/chat` | `builder_chat` | `{session, message}` → SSE stream of copilot frames |

`_dispatch_op` handles the 16 write ops; the read/lifecycle ops
(`fork`/`switch`/`list_drafts`/`compare`/`revert_to_live`/`undo`/`preflight`)
are handled inline in the `builder_op` handler because they act on store
slots / the undo history or run async. `builder_op` also calls
`store.remember(session)` immediately before every `_dispatch_op` write —
one of the two pre-op capture seams behind the `undo` op (§10.6). A `read_only`
server returns 403 for the POST ops and apply while keeping the GETs live — the
dashboard's read-only mode never lets a viewer mutate a contract.

The copilot (B1b) is a thin ADK agent whose tools are `DEFAULT_BUILDER_TOOLS`
(§10.7). Each tool pulls the session's draft from a contextvar bound by
`bind_builder_tool_context`, calls the matching op, and returns the SAME summary
shape the REST envelope carries — "one source of truth" for what an edit did,
whether it came from a control or a chat turn. `builder_chat` streams the
copilot's frames over SSE (`token` / `tool` / `patch` / `done` / `error`), and the
form applies a `patch` frame to the shared draft exactly as it applies a
`/builder/op` response, then re-renders. The copilot's apply tool is
`preview_apply`, which is ALWAYS `confirm=False` — the chat can preview an apply
but never rolls the epoch (invariant **L5**).

> ⚠️ TRAP — the copilot tool context is a contextvar, not a parameter. A copilot
> tool NEVER accepts the draft/session as an argument (the agent is constructed
> once per session and reused); it calls `_active_context()` on its first line.
> If you add a tool that takes the context as a parameter, the agent cannot
> supply it and every call fails — mirror the existing tools exactly. This is the
> same contextvar-seam pattern the proposer tools use (see 05-proposer.md
> §"The contextvar binding").

### 10.1.4 The board editor — the flagship GUI (B2)

Board authoring is the builder's largest form surface, and it is built on the
same doctrine as every other control: **it drives existing ops only.** The
Board section renders each entry as a clickable row (id + kind + at-a-glance
badges) that toggles an **inline accordion editor** — there is no modal
machinery in the console, and the accordion fits the `gatedSwap` + `section`
idiom the rest of the view uses.

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
Save. This is invariant **L1** in its most literal form: the flagship form adds
zero ops. `tests/test_builder_api.py::test_builder_op_edit_board_entry_whole_entry_round_trip`
pins the byte-stability per kind — the re-read row equals
`entry_to_dict(validate_board_entry(payload))`, which is exactly the save/reopen
loop the editor relies on. Delete drives `remove_board_entry` behind a two-click
confirm; a per-judge badge's × drives `remove_judge` directly; the board-level
`board_meta` panel (drift suppression + judge-only) drives `set_board_meta`,
closing B0's documented GUI exception.

**No client validation twin (invariant L4).** The form carries NO port of
`BoardEntry.validate`. Save is gated only on the PRESENCE of an id (a
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

Every editable contract knob has exactly one write op. This is the enumerable
that invariant **L2** hangs off — memorize its shape, because "add a knob" means
"add a row here plus its four companions" (§10.6). The op string equals the
function name in every case.

| Op / function | Mutates (contract knob) | Key args / knobs |
|---|---|---|
| `set_structure` | `scoring.tournament_structure.structure` (params preserved) | `structure: str` (validated by `TournamentStructure`) |
| `set_param` | one `tournament_structure.params[key]` | `key`, `value` (stored verbatim; `None` removes the key) |
| `set_holdout` | `scoring.overfitting` + per-entry `holdout` tags + nested `ladder` | `enabled`, `fraction`, `tags`, `min_board_size_for_split`, `rotate_holdout`, `restrict_proposer_visibility`, `random_baseline_every_n`, `max_generations_per_contract` (`0` clears), `ladder` (partial dict) |
| `set_proposer` | `draft.proposer_path` | `proposer_path: str \| Path \| None` |
| `set_weights` | scalar + mapping loss-shaping fields on `scoring` | `drift_weight`, `pass_weight`, `per_kind_weights`, `per_judge_weights`, `default_judge_weight`, `plan_revision_weight`, `runtime_weight`, `severity_weights` |
| `set_gate` | the promote gate on `scoring` | `promote_margin`, `monotonicity`, `monotonicity_scope`, `namespace_monotonicity`, `block_on_containment_violation`, `block_on_gate_contradiction`, `regression_gate_enabled`, `regression_test_command`, `regression_timeout_s` |
| `set_namespace_weights` | `scoring.namespace_weights`, `scoring.diff_complexity_weight` | `namespace_weights` (sign encodes worse-direction), `diff_complexity_weight` (≥0) |
| `set_proposer_quality` | nested `scoring.proposer_quality` | `best_of_n` (≥1), `critique_enabled`, `process_exemplars` (≥0) |
| `set_experiment_memory` | `scoring.experiment_memory.cross_epoch` | `cross_epoch: bool` |
| `set_screening` | `scoring.proposer_quality.screen_entries` / `screen_veto_only` | `entries` (≥0), `veto_only` |
| `edit_board_entry` | `draft.entries` (add/replace by id; validates first) | `entry: BoardEntry` |
| `remove_board_entry` | `draft.entries` (delete by id; unknown id raises) | `entry_id` |
| `add_judge` | one entry's `judges` tuple | `entry_id`, `judge: JudgeSpec` |
| `remove_judge` | one entry's `judges` tuple | `entry_id`, `name` |
| `set_brief` | `draft.brief` | `text: str` |
| `set_board_meta` | the board-level `board_meta` header (`draft.disable_drift` / `draft.judge_only`) | `disable_drift` (wholesale token list, validated; `[]` clears, `None` unchanged), `judge_only` |

Three structural facts to internalize:

- **The scoring ops compose on nested blocks.** `set_proposer_quality` and
  `set_screening` both edit the *same* `proposer_quality` block — each touches
  only its own fields and replaces the block with a `dataclasses.replace`. That
  is deliberate: they are two operator-facing concerns (slate quality vs.
  tryout screening) over one contract sub-object. Do not merge them; do not let
  one clobber the other's fields.
- **Every op validates at the boundary, never silently coerces.** `set_gate`
  raises `ValueError` on an invalid `monotonicity_scope`; `set_namespace_weights`
  raises on a negative `diff_complexity_weight`; `edit_board_entry` calls
  `entry.validate()` before the entry lands. A bad edit raises rather than
  corrupting the draft — the dispatch layer turns the raise into a 400.
- **Mapping fields are edited wholesale.** `set_weights`'s `per_kind_weights` /
  `per_judge_weights` / `severity_weights` and `set_gate`'s
  `namespace_monotonicity` REPLACE the whole mapping. The builder is a
  contract editor, not a merge tool; a caller that wants to add one judge weight
  sends the full new mapping.

> ⚠️ TRAP — the "`0` means what?" asymmetry. Across the ops, `None` is
> universally "leave unchanged", but the meaning of an explicit `0` varies by
> field and is documented per-op: `set_holdout(max_generations_per_contract=0)`
> **clears** the ceiling (the field's real "off" is `None`, which the op
> reserves for "unchanged"), while `set_screening(entries=0)` turns the screen
> **off**. When you add a knob whose natural "off" is `None`, you cannot use
> `None` as the "unchanged" sentinel too — pick an explicit sentinel (`0`, a
> flag) and document it in the op docstring, exactly as `set_holdout` does.

---

## 10.3 The honest cost meter

`estimate_cost(draft) -> CostEstimate` is the builder's most important read op:
it prices a contract's **board-runs-per-round** before the operator commits, so
an authoring choice is annotated with its downstream cost. The result shapes:

```python
# src/zicato/builder/operations.py
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
the auxiliary line) adds into `per_round`:

| Term (label) | Formula | Fires when |
|---|---|---|
| base schedule — `duel runs` | `field_size × replicates × board` | gauntlet, or `field_size ≤ 1` |
| base schedule — `bracket-match runs` | `matches × replicates × board`, `matches = field_size−1` (single) / `2·(field_size−1)` (double) | `single_elim` / `double_elim` |
| base schedule — `swiss-pairing runs` | `rounds_n × pairings × replicates × board`, `pairings = field_size//2` | `swiss` |
| base schedule — racing rungs | successive-halving rung sum + final full-board duel (`_racing_cost`) | `racing` |
| `holdout-confirm runs` | `holdout_size × replicates` | any structure with a non-empty holdout |
| `candidate-screen runs` | `proposes × best_of_n × panel`, `panel = min(screen_entries, board)` | `screen_entries > 0 and best_of_n > 1` |
| `best-of-N propose calls` | `proposes × best_of_n` — **auxiliary LLM calls, excluded from the headline** | `best_of_n > 1` |
| `crowning-confirm runs (evidence gate)` | `budget × 2 × board` | `promote_confidence_threshold` is set |
| `placebo-baseline runs (amortized)` | `ceil(replicates × board / random_baseline_every_n)` | `random_baseline_every_n > 0` |

The default `replicates` is read from the selection layer, not hard-coded — the
comment is the load-bearing part:

```python
# src/zicato/builder/operations.py — estimate_cost
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
# src/zicato/builder/operations.py — estimate_cost
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

The evidence-confirm budget is ~16× the base schedule. That is the whole reason
the meter exists: an operator flipping the evidence gate on with the scaffold's
32-replicate budget has multiplied their per-round board spend, and the meter
says so in a line they can read *before* they apply. The op docstring names it:
"with the scaffold's 32-replicate budget this is typically the LARGEST term."

> ✅ ALWAYS add a `CostLine` for any new contract knob that multiplies the
> per-round board sweeps (invariant **L3**). The meter's contract is that its
> headline is a coarse upper-ish bound on the *board runs* a round actually
> spends — a knob that adds runs without a line makes the meter lie by
> omission, and the operator learns the true cost only from their model bill.

> ⚠️ TRAP — auxiliary LLM calls are NOT board runs. The `best-of-N propose
> calls` line is appended to the breakdown but deliberately NOT added to
> `per_round`: those are proposer-side model calls, not board evaluations. Keep
> that distinction when you add a term — if your knob spends model calls that
> are not board sweeps, label the line "auxiliary" and leave it out of the
> headline sum, exactly as best-of-N does. Conflating the two double-charges the
> board-runs headline.

### 10.3.3 The cost twin — the meter is computed twice and must agree

The live builder view drives its meter from the BACKEND: every control POSTs to
`/builder/op` and reads `cost` out of the returned envelope. But there is a
SECOND, client-side implementation of the exact same arithmetic —
`estimateCost` in `builder/model.js` — used so a **read-only** frozen-contract
preview (Settings → Contract, sourced from `/api/epoch` alone) can render the
meter without a `/builder/op` round-trip:

```javascript
// src/zicato/dashboard/static/js/builder/model.js
// PURE port of zicato/builder/operations.py's estimate_cost + validate, so a
// READ-ONLY contract preview can show the cost meter + validation diagnostics
// CLIENT-SIDE — no `/builder/op` round-trip, no backend dependency.
```

The two implementations MUST agree, down to the structure-aware replicate
default (`STRUCTURE_DEFAULT_REPLICATES = {gauntlet:2, single_elim:2,
double_elim:2, swiss:2, racing:1}` in the JS, the JS twin of
`zicato.selection.registry.STRUCTURE_DEFAULT_REPLICATES`). A py↔js parity test
pins the agreement (see `src/zicato/dashboard/static/test/builder.test.mjs`).

> ⛔ NEVER change a cost term in `operations.py::estimate_cost` (or
> `_racing_cost`) without making the identical change in
> `builder/model.js::estimateCost` (and `racingCost`) in the same commit. The
> two are a twinned pair by design (invariant **L3**); a one-sided edit reds the
> py↔js parity test — and if you "fix" that by deleting the assertion, the two
> meters silently diverge and a frozen-contract preview quotes a different price
> than the live builder for the same contract.

---

## 10.4 The statistical pre-flight — a read-op that measures the DRAFT

`preflight` is an `async` read op that runs the SAME measurement `zicato board
preflight` takes — an A/A noise-floor calibration and an achievable-signal probe
— but against the DRAFT's board and scoring, using the workspace's own champion
tree, adapter, and runtime `call_llm`. Its result is `PreflightResult`:

```python
# src/zicato/builder/operations.py
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
# src/zicato/builder/operations.py — preflight (one of several degrade arms)
    epoch_id = current_epoch_id(workspace_root)
    if not epoch_id:
        return PreflightResult(
            available=False,
            reason=(
                "preflight requires a registered target: no current epoch under "
                "this workspace (run `zicato register` / `zicato epoch new` first)"
            ),
        )
```

The degrade arms, in order: empty draft board → no current epoch → no workspace
config → no seeded baseline generation → no configured adapter → no runtime
`call_llm` → no mutation points under the champion snapshot. On success it
returns `available=True` with `verdict=report.verdict`, the report JSON, and the
measured noise floor.

Two properties an extender must preserve:

- **The verdict is recommend-only and never persisted** (invariants **L4**,
  **L5**). The draft is not the live contract, so its measurement must never
  masquerade as the live epoch's — `preflight` never writes onto the epoch
  record. Contrast the epoch-open pre-flight (13-recipes.md §"Add an epoch-open
  step"), which DOES persist onto the never-hashed epoch record.
- **It never starts a live evolve.** It spends only the small K-draw calibration
  budget and is cache-idempotent with `zicato board audit` (re-running is a
  cache hit). See 04-evaluation-statistics.md §"The calibration cache" for the
  reserved replicate base (1000) it draws on.

> ⚠️ TRAP — the pre-flight measures the DRAFT contract but borrows the LIVE
> target. `run_contract_preflight` consumes the draft's `board` and `weights`
> directly (no on-disk materialization) but takes the champion generation,
> adapter, and runtime from the workspace. If you extend the pre-flight to a new
> contract component the builder edits, thread it from the draft; if you extend
> it to a new *target* fact, thread it from the workspace — never invert those,
> or the pre-flight measures a contract the operator did not draft.

---

## 10.5 `validate` and the Review pane verdict

`validate(draft, workspace_root=None, *, noise_floor_max_abs_delta=None) ->
list[Warning]` returns a list of advisory warnings — **never a blocking verdict**
(invariant **L4**). Each warning carries a stable `code`, a human `message`, and
a `severity`:

```python
# src/zicato/builder/operations.py
@dataclass(frozen=True, slots=True)
class Warning:
    code: str        # stable symbolic code the UI keys on
    message: str
    severity: str = "warning"   # "info" / "warning" / "refuse" — never blocks
```

The severity ladder is `info` (advisory) / `warning` (likely a mistake) /
`refuse` (statistically unsound) — and the docstring on the field is emphatic
that even `refuse` "never hard-blocks apply." The warnings the current build
emits:

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

The board-authoring codes are **Python-only**: the JS twin
(`builder/model.js::validateContract`) deliberately mirrors the entry-free
subset only (its scope comment names the excluded codes), because the
read-only frozen-contract preview it serves never has the full entry
objects. Do not twin an entry-level code into the JS.

> ⛔ NEVER import (or `find_spec`) an operator-supplied dotted path inside
> `validate` — the `dotted_path_malformed` check is SHAPE-ONLY by design.
> Resolving a module executes parent-package code, and a draft may be
> copilot-authored, so a server-side import would hand the chat model an
> arbitrary-code-execution path. The warning message points the operator at
> `zicato board audit`, which exercises the path in the workspace's own
> runtime context. The posture is recorded in `validate`'s docstring; keep
> any future path/spec check on the shape side of that line.

### 10.5.1 The margin-vs-floor check — the one `refuse`

The statistically load-bearing check pairs the promote margin against the
measured A/A noise floor. If a floor is known (passed in from a just-run
`preflight`, or read off the current epoch record) AND the evidence gate is off
AND `promote_margin <= floor`, it fires `margin_below_noise_floor` at `refuse`:

```python
# src/zicato/builder/operations.py — validate (the margin-vs-floor arm)
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
`_diff.rolls_epoch` — i.e. there is something to apply.

> ✅ ALWAYS give a new `validate` warning a stable symbolic `code`. The UI keys
> on it (`refuseWarningsPanel` filters on severity; the rail and chips key on
> codes), and a copilot turn may cite it. A message-only warning with no code is
> unstyleable and untestable.

> ⛔ NEVER make `validate` (or the pre-flight verdict) block `apply` (invariant
> **L4**). The builder's posture is that the operator is the authority: it
> surfaces the unsoundness in the loudest recommend-only form it has (`refuse` +
> a ⛔ chip) and writes the contract anyway if they confirm. If a check ever
> *must* block, that is a contract-validation concern for
> `epoch/contract.py`, not a builder warning — and it will block every write
> path, not just the builder's.

### 10.5.3 The apply path — dry-run vs confirm, and why apply does not roll

`apply(draft, workspace_root, confirm)` is where a draft becomes (or previews
becoming) the live contract. It returns an `ApplyResult` carrying `confirmed`,
`rolled`, `components_changed`, `new_contract_hash`, `cost`, `diff`, and
`warnings`. The two branches:

- **Dry run (`confirm=False`).** Nothing is written. The result carries the
  *predicted* contract hash, computed by `_predicted_contract_hash`, which
  materializes the draft's board/brief/scoring into a throwaway
  `tempfile.TemporaryDirectory` and runs the REAL `compute_contract_hash` over
  them (plus the workspace's live entrypoint / mutable-trees and the draft's
  proposer). So the operator sees the EXACT hash an apply would land — without
  touching the workspace. `rolled` is always `False` for a dry run.
- **Confirm (`confirm=True`).** `_write_contract` writes the draft to the
  workspace's LIVE contract source paths — the same `board.jsonl` / `brief.md` /
  `scoring.json` (and proposer dir) that `zicato register` / `zicato epoch new`
  publish, recorded under `config.json`'s `contract` key. The result recomputes
  the hash from the now-written live contract and sets `rolled=diff.rolls_epoch`.

The critical design point (invariant **L5**): **`apply` writes contract *source
files*; it never opens an epoch.** The ordinary auto-epoch machinery rolls the
epoch on the NEXT `zicato evolve` resolve, exactly as it would for a hand-edited
`scoring.json`. The builder is a contract *editor*, not an epoch driver — it
reuses the existing write paths so there is one epoch-roll mechanism, not a
second one the builder owns:

```python
# src/zicato/builder/operations.py — apply (confirm branch)
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
> the builder's local instance of the contract-hash cwd/checkout hazard
> (12-bug-casebook.md §"Bug #10").

> ⚠️ TRAP — the draft must round-trip EVERY board-file component, not just the
> entries. The board's optional `board_meta` header (`disable_drift` /
> `judge_only`) is part of the contract, and `apply` rewrites the whole
> `board.jsonl`: a draft loaded through the entries-only loader would silently
> STRIP the header from the live contract on apply (the B0 bug).
> `TournamentDraft.from_workspace` therefore loads via
> `load_current_board_with_meta`, the draft carries `disable_drift` /
> `judge_only` fields, and BOTH writers (`_write_contract` and
> `_predicted_contract_hash`) pass them to `save_board`. The draft's
> `_board_canon` prepends the header line only-when-non-default, mirroring
> `save_board`'s emit rule (`zicato.board.jsonl.board_meta_to_dict` is the
> shared header builder), so the diff agrees with the on-disk bytes the
> contract hash sees. If you add another board-level header field, thread it
> through all four seams in the same commit.

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
# src/zicato/builder/draft.py — _copy_draft
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

Slots are process-local — they never persist to disk, inheriting the contract
that "drafts have never outlived the dashboard process." `fork` raises on a
malformed or already-taken name (it never silently overwrites a variant).

**The compare read-op (`compare_drafts`, `operations.py`).** A keyed diff between
any two drafts, over the SAME canonicalizers the epoch-roll rule uses, so
"differs here" agrees with "would roll the epoch". It returns
`changed_components` plus per-component detail (`scoring` keys with `a`/`b`
values, `board` `added`/`removed`/`changed` ids, `brief`, `proposer`). Because
its scoring keys come from the contract-canonical form (float-rounded,
omitted-at-default fields absent), the diff never reports a phantom change the
hash would not see.

> ⚠️ TRAP — the copilot's `compare` tool resolves the literal names `"session"`
> and `"live"` specially (current working draft, and a fresh
> `from_workspace`), in addition to slot names. If you add a resolvable name,
> add it to BOTH the copilot's `compare` tool and any API compare path — the
> two share the operator's mental model of "what can I compare against".

### 10.6.1 Undo and revert — the two restore ops

Two lifecycle ops let an operator walk edits back, and both are ops
(invariant **L1** — the GUI's Undo/Reset buttons and the copilot's tools call
the same functions, never a second edit path):

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
  does the same — so a form edit and a chat edit share ONE history and either
  door can undo the other's edit. `remember` dedups against the newest
  snapshot by field equality (a read tool records nothing); `pop_undo`
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

## 10.7 The full-coverage invariant, and how it is enforced

Invariant **L2** is the discipline that keeps the three front doors in lockstep.
A new contract knob is not shipped until it lands on **six** surfaces:

| # | Surface | File | Enforced by |
|---|---|---|---|
| 1 | the op | `operations.py` (a `set_*` function) | code review |
| 2 | the dispatch arm | `api.py::_dispatch_op` (an `if op == "…":` arm) | `tests/test_builder_api.py` knob-dispatch tests |
| 3 | the copilot tool | `copilot_tools.py::DEFAULT_BUILDER_TOOLS` | **`test_default_builder_tools_registry_covers_every_op`** (machine-pinned) |
| 4 | a GUI control (or a documented exception) | `model.js::paramSpecsFor` / `views/builder.js` / `builder/entry_form.js` section | **`test_builder_gui_coverage.py`** (machine-pinned) + node suite |
| 5 | a cost line (if it changes the schedule) | `operations.py::estimate_cost` + `model.js::estimateCost` | py↔js parity test |
| 6 | a `validate` consideration (if it can be unsound) | `operations.py::validate` | `tests/test_builder_operations.py` |

Surfaces 1–4 are **mechanically pinned**. The dispatch is a flat if/elif chain
that falls through to a raise, so an op missing its arm is a 400 the API tests
catch; the copilot registry is pinned by an explicit anti-drift test:

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
`DEFAULT_BUILDER_TOOLS` (minus the pure-read tools — `estimate_cost`, `validate`,
`preflight`, `list_drafts`, `compare`, `preview_apply`), reads `views/builder.js`
+ `builder/entry_form.js` as TEXT, and demands each op is wired as
`runOp('<op>'` / `postOp('<op>'` OR justified in its `GUI_EXCEPTIONS` dict. The
one standing exception today is `add_judge` (judge authoring rides the whole-entry
`edit_board_entry` round-trip — the entry_form judges editor — rather than a
second authoring path). A stale exception (an op that has since gained a control)
reds just as loudly as a missing control, so the doctrine cannot rot in either
direction.

Surfaces 5–6 are **discipline plus parity**: there is no single per-knob test
that asserts "this knob has a cost line AND a validate check", but the py↔js cost
parity test pins any cost line you add to be mirrored, and the design doc
(`docs/design/TOURNAMENT-BUILDER.md` §"The consequence-forward principle") makes
the cost + epoch-roll surfacing a stated requirement of the two builder skills.

> ⛔ NEVER add an op to `operations.py` and its `_dispatch_op` arm but skip the
> copilot tool (invariant **L2**). `test_default_builder_tools_registry_covers_
> every_op` reds immediately — that red is the invariant working. The fix is a
> one-line addition to `DEFAULT_BUILDER_TOOLS` (and its module `__all__`), not a
> weakening of the test.

> ✅ ALWAYS decide surface 4's "GUI control-or-documented-exception" explicitly —
> `test_builder_gui_coverage.py` now forces the choice. The GUI renders every
> `paramSpecsFor` spec as a number input; a boolean knob is a hard-coded toggle in
> a section builder; a knob deliberately left form-invisible (an advanced/rare
> lever) is an entry in that test's `GUI_EXCEPTIONS` dict with a one-line
> justification, not an oversight. A new op with neither a control nor an
> exception reds the pin, naming the op and the two remedies.

The recipe that walks all six is §10.8.

---

## 10.8 Recipe: add a builder op end-to-end

Goal: add a new editable contract knob to the builder so the form, the copilot,
the cost meter, and `validate` all know about it — satisfying invariant **L2**.
The worked example: exposing a hypothetical `set_gate(min_holdout_confirms=…)`
knob. The steps generalize to any knob.

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
   turns into a 400 — so a missing arm is a test failure, not a silent no-op.
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
   for the board-editor ops). If the knob is deliberately form-invisible, add it
   to `tests/test_builder_gui_coverage.py::GUI_EXCEPTIONS` with a one-line
   justification — invariant **L2**'s "documented exception". `runOp`/`postOp`
   with the op string is what the coverage pin greps for, so wire the string
   literally (never build the op name dynamically).
5. **Add the cost line if it changes the schedule.** If the knob multiplies
   per-round board runs, add a `CostLine` in `operations.py::estimate_cost` AND
   the mirror term in `builder/model.js::estimateCost` in the SAME commit
   (invariant **L3**; §10.3.3). Label auxiliary LLM-call terms and leave them
   out of the headline sum.
6. **Add the `validate` consideration if it can be unsound.** If a value can
   make the contract statistically unsound (a margin that cannot clear the noise
   floor, a field that degrades a structure), emit a `Warning` with a stable
   `code` and the right `severity` (`refuse` only for genuine unsoundness) in
   `operations.py::validate`. Recommend-only — never block (invariant **L4**).
7. **Contract accounting.** The knob lives on `ScoringWeights` (or a nested
   block). Confirm it is omitted-at-default from the canonical scoring form
   (`epoch/contract.py`'s omit-at-default set) so existing epochs do not roll
   retroactively, and that a non-default value DOES roll the epoch — the builder
   diff and `apply(rolled=…)` derive from that canonical form. See
   03-contract-and-epochs.md §"Omit-at-default fields".
8. **Tests — four kinds:**
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
   - *cost parity* — if you added a cost line, the py↔js parity test in
     `src/zicato/dashboard/static/test/builder.test.mjs` must stay green.
9. **Verify:**
   ```bash
   uv sync --all-extras
   uv run pytest tests/test_builder_operations.py tests/test_builder_api.py \
       tests/test_builder_copilot.py -x -q
   uv run pytest tests/test_epoch_contract.py \
       tests/test_contract_serializer_completeness.py -q   # omit-at-default + roll
   make node-test        # the JS twin + the py↔js cost parity assertion
   uv run ruff check src/zicato/builder/ && uv run mypy src/zicato/builder/
   ```
   If you skipped step 3, `test_default_builder_tools_registry_covers_every_op`
   reds — the copilot cannot reach your knob. If you skipped step 4,
   `test_builder_gui_coverage.py` reds — the knob has no GUI control and no
   documented exception. If you skipped step 5's JS mirror, the node suite reds —
   a frozen-contract preview would quote the wrong price.

**Definition of done.** The knob is editable from a form control and a chat
turn, it prices correctly on the meter (both implementations), `validate` warns
if it can be unsound, a non-default value rolls the epoch while the default is
byte-identical to before, and all four test kinds are green.

---

## 10.9 The CLI — an auto-discovered command tree

The `zicato` executable is `zicato.cli:main`, which builds a `click` root group
by **auto-discovery**: every importable module under `zicato.cli.commands` is
imported, and each top-level `click.Command` / `click.Group` it defines is
attached to the root. Parallel workstreams ship one command per file without
ever editing the root:

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

Three robustness rules are baked in: a broken command module logs a warning and
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
| `reflect.py` | `reflect` (group) | `run`, `report`, `apply` — board reflection (BOARD-REFLECTION.md R4); `apply` reaches the builder via `zicato.reflection.apply` (a library edge), not a direct `cli → builder` import |
| `register.py` | `register` | — |
| `tournament.py` | `tournament` | — |
| `reindex.py` | `reindex`, `reindex-generations`, `repair-tournament-fk` | — |
| `analyze_telemetry.py` | `analyze-telemetry` | — |
| `regenerate_report.py` | `regenerate-report` | — |
| `repair_judge_losses.py` | `repair-judge-losses` | — |
| `repair_v0_baseline.py` | `repair-v0-baseline` | — |

A module publishes multiple top-level commands via its `__all__` (`epoch.py`,
`reindex.py`). `init_cmd.py` is a helper module (`initialize_workspace`), not a
command — it has no `click` decorators, and `discovery.py` skips `_`-prefixed
modules regardless.

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

> ⚠️ TRAP — `--help` is the source of truth, CLI.md is the mirror. When you add
> or change a command, flag, default, or help string, re-run `uv run zicato …
> --help` and update CLI.md to match — including its "Last reconciled …" date and
> its command-list census (the header enumerates every command by name to catch
> a phantom/renamed command). A CLI change that leaves CLI.md stale is a
> documentation regression, not a harmless omission. See
> 11-testing.md §"The CLI-HELP parity gate" for the byte-check that guards the
> help text itself.

---

## 10.10 Flags → pins → config knob → workers

The single most bug-prone thing about the CLI is how an operator flag reaches
the code that consumes it — especially code that runs in a **worker
subprocess**, a different OS process than the one that parsed the flag. zicato
solves this with a **process-pinned override** layer, and the rule that falls
out of it is invariant **L6**: *no environment variable is a configuration
knob*. The config module states it at the top:

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
    if harness_call_timeout_ms is not None:
        pins.setdefault("runtime", {})["harness_call_timeout_ms"] = harness_call_timeout_ms
    if aux_call_timeout is not None:
        pins.setdefault("aux", {})["call_timeout_s"] = aux_call_timeout
    ...
        pin_overrides(pins)
```

**Hop 2 — pin into the config tree.** `pin_overrides` validates eagerly (an
unknown section/field raises at the pin site, not later as a silently-defaulted
knob) and merges into a process-wide store. `load_config` layers the pins on top
of the dataclass defaults — so every later `load_config()`, however deep in the
call graph, sees the flag:

```python
# src/zicato/config.py — pin_overrides (docstring, excerpt)
    This is the bridge from CLI flags to the config tree: a command
    validates and pins its flag values once at startup, and every later
    :func:`load_config` call — however deep in the call graph — sees
    them layered on top of the environment ...

    The tournament runner serialises the current pins into every worker
    args file and the worker re-pins them at startup, so a pinned knob
    consumed inside the worker subprocess (e.g. the harness call
    timeout) crosses the process boundary without an environment
    variable.
```

For the rare call site that must tell "explicitly pinned" from "at its default"
(e.g. `runtime_factory.make_runtime_config`, where `--parallelism` outranks the
`config.json` value but the mere default must not), there is `pinned_override(
section, field)` returning the pinned value or `None`.

**Hop 3 — pins cross into the worker via the args file, NOT env.** The
tournament runner writes the current pins into each worker's JSON args file
under a `config_pins` key:

```python
# src/zicato/tournament/worker_transport.py — _config_pins
def _config_pins() -> dict[str, dict[str, Any]]:
    ...
    return get_pinned_overrides()
```

The runner writes `"config_pins": _config_pins()` into the args file it hands
the worker. This is deliberate — a flag consumed inside the worker crosses the
process boundary through the args file, not an environment variable.

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
> worker boundary (invariant **L6**). The lesson that motivated this whole layer:
> a flag value read via `os.environ` in the worker is invisible to the orchestrator's
> validation, cannot be pinned-vs-default-distinguished, and drifts from the
> `config.json` fallback. Pin the flag (`pin_overrides`), and let the runner's
> `config_pins` args-file channel carry it. The worker re-pins; `load_config`
> then resolves it identically on both sides of the boundary.

> ⚠️ TRAP — a pinned value must be JSON-serialisable, because it round-trips
> through the worker args file. Every CLI-flag value already is (ints, floats,
> strings, bools). If you pin a non-JSON value, `get_pinned_overrides()` →
> args-file write silently drops or corrupts it and the worker runs on the
> default. Pin the primitive, resolve the object worker-side.

### 10.10.2 The merited env-var set — `zicato config env`

The env vars zicato *does* touch are a small MERITED set — each a
process-boundary contract, never a knob — introspectable via `zicato config
env`, which reads `describe_env_vars()` so the command can never drift from the
code. Each entry carries a **boundary-kind role** (NOT a process label):

| Role | Meaning | Members |
|---|---|---|
| `harness-contract` | set by zicato for the inner harness — part of the run contract | `ZICATO_RUN_SCRATCH_DIR` |
| `internal-handoff` | set and restored by zicato to hand a value across its own processes | `ZICATO_HARMONOGRAF_URL`, `ZICATO_HARMONOGRAF_GRPC` |
| `secrets-boundary` | operator-NAMED variables so credentials stay in the environment, never in files | `<models.<role>.api_key_env>`, `<runtime.worker_env_passthrough>` |
| `external-integration` | another tool's own variable that zicato defers to | `GOLDFIVE_AGENT_CALL_TIMEOUT_MS` |
| `test-toggle` | CI / test switches; never read on an operator path | `ZICATO_SKIP_HOOK_CHECK`, `ZICATO_PARITY_UPDATE` |

The role is a *boundary taxonomy* of exactly five values; which process
sets/reads a variable is prose in the entry's `description` (e.g.
`ZICATO_RUN_SCRATCH_DIR` is "Set BY the tournament worker FOR the inner
harness"). The backing type is `EnvVarInfo(name, role, description)`.

> ⛔ NEVER add an operator tuning knob to `_MERITED_ENV_VARS`. The set is for
> process-boundary contracts only. If your feature needs an operator knob, it is
> a CLI flag (pinned) plus a `config.json` block — the same posture the six
> deleted `ZICATO_HEALTH_*` thresholds moved to. A new env var must justify its
> role from the five above, or it does not belong.

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
   wins). Type it precisely (`int | None`, `float | None`) so "unset" is
   distinguishable from a real value.
3. **Pin it.** In the command's pin helper (`_pin_config_flags` for `evolve`),
   map the non-`None` flag to its `{section: {field: value}}` pin and ensure
   `pin_overrides(pins)` runs once at startup. Do NOT read the flag again
   downstream — consume it via `load_config()` / `pinned_override()`.
4. **Confirm worker propagation IF the knob is consumed in a worker.** The
   runner already threads ALL pins via `config_pins`; you get propagation for
   free. Your job is to prove it: add a test that pins the override, builds the
   worker args (or calls `_config_pins()`), and asserts your `section.field` is
   present in the args payload and re-pins correctly worker-side.
5. **Help text + CLI.md.** Re-run `uv run zicato <command> --help`, confirm the
   flag reads correctly, then reconcile `docs/design/CLI.md` (§10.9.2) — update
   the option, its default, and the "Last reconciled" date.
6. **Verify:**
   ```bash
   uv run pytest tests/test_config.py tests/test_cli_evolve.py \
       tests/test_tournament_worker_transport.py -x -q
   uv run zicato evolve --help          # eyeball the new flag + its shadow note
   # CLI-HELP parity gate (11-testing.md §"parity gates"):
   bash tools/parity.sh --only CLI-HELP
   ```
   If you skipped step 3 and read the flag inline, the worker never sees it
   (invariant **L6**) — the value silently reverts to the `config.json` default
   inside every duel. If you skipped the CLI.md reconcile, the CLI-HELP parity
   gate or the doc census reds.

**Definition of done.** The flag shadows a real `config.json` knob, wins over it,
reaches worker subprocesses via `config_pins` (proven by a test), reads correctly
in `--help`, and CLI.md matches the binary.

---

## 10.11 The library facade and the import boundary

zicato is a library first. The public surface is declared in
`src/zicato/__init__.py` as a **lazy facade**: a dict mapping each public name to
its home module, resolved on first access by a module-level `__getattr__`. There
are **37 lazy exports** (the `_EXPORTS` dict), and `__all__` is
`["__version__", *sorted(_EXPORTS)]` — 38 names counting `__version__`.

> ⚠️ TRAP — the facade is `__init__.py`, and the count is 37, not 36. Older prose
> (including 14-goals-and-roadmap.md §"the boundary") calls it "the 36-name lazy
> facade (`zicato/api.py`)"; both are stale — there is no `src/zicato/api.py`,
> and the current tree exports 37 names. If you cite the count, cite
> `len(zicato._EXPORTS)`.

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

Two properties are load-bearing (invariant **L7**), and both are machine-pinned
in `tests/test_public_api.py`:

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

The `TYPE_CHECKING` block at the bottom of `__init__.py` re-imports all 37 names
in redundant-alias form (`from … import X as X`) so mypy and IDEs see the surface
that `__getattr__` provides only at runtime. Because it is under
`if TYPE_CHECKING:`, none of it runs at import time — laziness is preserved.
`round_log` is the one export whose attribute is `None` (the *module object*
itself is the export), pinned by its own test.

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
> The facade is the *declared* public surface — the evolve loop, the epoch
> lifecycle, the board/scoring layer, the storage seams, the harness-adapter
> contract, the health diagnostics. An internal helper does not belong here; it
> is reachable at its home module for code that legitimately imports deep. The
> facade is what the three drivers and embedding applications are promised.

### 10.11.3 The five import-linter contracts

The library/driver boundary is enforced by `import-linter` (`uv run
lint-imports`, wired into `make check` and CI — Golden Rule G5). There are
exactly five contracts in `pyproject.toml`, all of type `forbidden`:

| # | Name | Forbids |
|---|---|---|
| 1 | the library must not import the drivers (cli / dashboard / builder) | any of ~31 library packages → `zicato.cli` / `zicato.dashboard` / `zicato.builder` |
| 2 | dashboard driver: no import of the cli | `zicato.dashboard` → `zicato.cli` |
| 3 | builder driver: no import of the other drivers | `zicato.builder` → `zicato.cli` / `zicato.dashboard` |
| 4 | cli driver: no direct import of the builder | `zicato.cli` → `zicato.builder` (`allow_indirect_imports = true`) |
| 5 | the query layer stays dashboard-free | `zicato.query` → `zicato.dashboard` |

Contract 1 lists every library package explicitly as a `source_module` —
including `zicato.query`, which is deliberately **library** (the workspace query
layer the dashboard consumes), not a driver. Contract 4 sets
`allow_indirect_imports = true` on purpose: the CLI legitimately reaches the
builder *transitively* through the two declared edges (cli → dashboard.server →
builder.api mount); what it forbids is the CLI growing its OWN direct builder
dependency.

The two — and only two — permitted driver→driver edges fall out of these
contracts: `cli → dashboard` (the CLI launches the server) and `dashboard →
builder` (the server mounts the builder REST routes).

> ⛔ NEVER add a driver→driver edge. You almost certainly do not need one. If the
> CLI needs a builder capability, it reaches it through the dashboard mount (the
> declared transitive path), or the capability belongs in the library where both
> can import it. Adding, say, a `cli → builder` direct import reds contract 4 —
> and the fix is architectural (move the shared code into a library package),
> never loosening the contract. A genuine new edge is a design change requiring
> its own PR and a rewrite of the contract with a documented rationale.

> ✅ ALWAYS add a new library package to contract 1's `source_modules` list when
> you create one. The list is explicit (not a wildcard) precisely so a new
> package is a conscious addition; a package omitted from the list is silently
> unprotected and can grow a driver import nobody notices until it ships.

### 10.11.4 TID251 — the banned private-reach paths

Ruff's `flake8-tidy-imports` (TID251) bans 13 specific cross-module private
reaches that a refactor retired — each promoted to a public seam at its honest
home. The ban keeps the old underscore path from regrowing:

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
> for the OLD path in the same commit. Otherwise the underscore import regrows
> the moment someone copies an old call site, and the promotion's whole point —
> one honest public seam — is undone silently. The ban message names the
> replacement, so the offender gets an actionable error, not a mystery.

---

## 10.12 Packaging

zicato is a hatchling-built wheel with a `src/` layout. Four packaging facts an
extender touches:

**Extras.** `[project.optional-dependencies]` declares three: `adk`
(`google-adk[extensions]`, `goldfive[adk]` — the tool-using proposer path),
`dashboard` (`starlette`, `uvicorn`, `watchdog` — the ASGI server + SSE file
watcher), and `dev` (the full test/lint toolchain plus `zicato-examples`). The
memory rule from 01-orientation.md §G2 restated: **always `uv sync
--all-extras`** — a bare `uv sync` deletes dev tooling from `.venv`.

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
harness is a developer tool (run by `tests/test_dashboard_js.py`), not part of
the shipped bundle.

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

> ⚠️ TRAP — the supervisor binary is generated, not VCS-tracked, so it must be
> `force_include`d (not matched by a package glob) or it never lands in the
> wheel. It is owned by exactly one build target (the wheel); the sdist carries
> source instead and rebuilds at wheel-build time. See 08-supervisor.md
> §"Resolving the binary" for the runtime fallback chain, and 14-goals-and-
> roadmap.md §"the boundary" for the future physical wheel split (`zicato-lib` /
> `zicato-cli` / `zicato-dashboard`) where exactly one wheel would own `_bin/`.

---

## 10.13 Cross-references

- 01-orientation.md §"Library first, three drivers on top" — the shape this
  chapter formalizes; §"G5 parity gates + import contracts", §"G9 module-level
  callables across the worker boundary" (the reason pins, not env, cross to
  workers).
- 02-architecture.md §"orchestrator vs workers vs supervisor vs dashboard" —
  the four processes the pins and env-var contracts cross.
- 03-contract-and-epochs.md §"The contract hash" / §"Omit-at-default fields" —
  what the builder's diff, `rolls_epoch`, and `apply` are computed against.
- 04-evaluation-statistics.md §"The A/A noise floor" — what the builder
  pre-flight measures and what `margin_below_noise_floor` compares against.
- 06-tournament-and-selection.md §"The evidence gate" — the `budget × 2 × board`
  crowning-confirm term that dominates the cost meter.
- 08-supervisor.md §"Resolving the binary" — the runtime fallback chain for the
  `_bin/` supervisor this chapter's build hook bundles.
- 09-dashboard-and-query.md §"The builder view" — the front end of the builder
  backend documented here; the CONTRACTS.md payload discipline the REST envelope
  obeys.
- 11-testing.md §"parity gates" (the CLI-HELP gate), §"Node suite conventions"
  (the py↔js cost twin parity), §"import contracts".
- 13-recipes.md §"Add a CLI flag the RIGHT way" / §"Add a builder op end-to-end"
  — the short-form cookbook entries that point back at §10.8 and §10.10.3.
- `docs/design/TOURNAMENT-BUILDER.md` — the full builder design record (B1a/B1b/B2
  decomposition, the consequence-forward principle); `docs/design/CLI.md` — the
  generated command reference kept in lockstep with `--help`.
