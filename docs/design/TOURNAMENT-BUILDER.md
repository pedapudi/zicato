# The tournament builder — one component, three entry points

> **Status.** Built and in the tree. The deterministic backend lives at
> `zicato/builder/{config,draft,operations,api,copilot,copilot_tools}.py`;
> the frontend is a self-contained console view
> (`dashboard/static/js/views/builder.js` plus `dashboard/static/js/builder/`);
> the launch surfaces are the dashboard's first-class `#/builder` view, a
> launcher rail entry in the Settings drawer
> (`dashboard/static/js/views/settings.js`), and the standalone
> `zicato dashboard --view builder` command
> (`zicato/cli/commands/builder.py`). The whole stack is exercised by the
> test suite (`tests/test_builder_*.py`, the JavaScript `builder.test.mjs`
> and `settings.test.mjs`, and `tests/test_cli_builder.py`).
> Operator-facing instructions live in the two builder skills.

The **tournament builder** is the GUI for composing an epoch's evaluation
contract — as a **draft** that is edited live and applied only on explicit
confirmation. It is the dashboard's flagship authoring surface: the place an
operator turns a vague objective into a concrete, hashable contract without
hand-editing JSON. The GUI authors **every** part of the contract:

- **Structure & field/noise** — the five tournament structures, their per-
  structure params (field size, replicates, the evidence gate, and the
  racing rungs including the `rung0_board_size` override), and candidate
  screening.
- **The board itself** — a full inline board editor (add / edit / delete
  entries per-kind, judges, expectations, the `board_meta` header, paste-JSONL
  import) plus the train/holdout split and the anti-overfitting knobs,
  including the `max_generations_per_contract` board-refresh ceiling.
- **The weighted loss** — the scalar coefficients (drift / pass / default-judge
  / plan-revision / runtime), the severity / per-kind / per-judge weight maps
  (per-judge seeded from the board's judges, with add-key rows), and the signed
  namespace weights with an add-key row.
- **The promote gate** — margin, pass-rate monotonicity + scope, the per-
  namespace monotonicity map, the integrity blocks, and the regression pre-gate.
- **The proposer** — a picker over discovered proposer dirs + the builtin
  default + a free-text path, plus the proposer-quality levers and the brief.
- **Lifecycle** — fork/compare draft slots, reset-to-live, and step-undo.

Every write/lifecycle op has a GUI control (or a documented exception),
machine-pinned by `tests/test_builder_gui_coverage.py` (§10.7 of the dev-guide).

The defining decision is that **the builder edits a draft, and applying that
draft rolls the epoch.** A different structure, a different board, a
re-weighted loss, a held-out slice, or a swapped proposer each change the
evaluation contract, and a changed contract is a new epoch
([EPOCHS-AND-JOURNALING.md §10.1](EPOCHS-AND-JOURNALING.md#101-whats-in-the-contract)).
The builder is therefore **consequence-forward**: every choice surfaces its
**cost** (board-runs per round) and its **contract impact** (whether applying
rolls the epoch) *before* the operator commits, and apply is gated behind an
explicit confirmation.

---

## 1. The launch / integration model — one view, three doors

There is exactly **one** builder view component (`views/builder.js`). It is
self-contained: `render(host)` takes nothing but a host element, owns its own
shared session draft, and never reads the tree, the route params, or the
breadcrumb. That self-containment is what lets the same component be reached
from three doors without a rewrite. The builder is its **own first-class
view**, rendered full-width in the main view host, and `#/builder` is a
top-level route returning `{view: "builder"}` (`js/router.js`). Settings
carries a **launcher** to it rather than hosting it:

| Door | Route / entry | What it does |
|---|---|---|
| **First-class view** | top-bar nav → **builder**, or `#/builder` | The flagship home. `#/builder` resolves to the builder view directly and paints full-width in the main host. The same route-agnostic `builder.render(host)` backs every door. |
| **Settings launcher** | top-bar **⚙ settings** → *Tournament builder* rail entry | The Settings drawer (`views/settings.js`) is a section rail (Tournament builder · Contract · Models · Appearance) over one body host. The *Tournament builder* entry is a **launcher** — it does not swap a section; it navigates OUT to `#/builder` so the builder always renders full-width. |
| **Standalone CLI** | `zicato dashboard --view builder` | Boots the same dashboard service as `zicato dashboard` and prints the builder deep-link (`http://127.0.0.1:<port>/#/builder`) so the browser opens on the builder. Loopback-only, same bind rule as `zicato dashboard` / `zicato evolve`. |

Settings launches the builder rather than reimplementing it. Its
**Contract** section is a read-only at-a-glance of the current epoch's
contract that reuses the builder's own live preview (`builder/preview.js`
`previewNodes`, fed the current epoch as a draft-shaped contract). The panel
shows the contract as the builder would show it, and the operator clicks out
to the builder to change it. The Settings surface itself is a routed
right-side **drawer overlay** that paints over the current console view.

---

## 2. The copilot ↔ draft mechanism — one shared draft, two views

The builder is two views of **one server-side draft**, keyed by a stable
session id (the dashboard tab uses `session = "dashboard"`):

* The **form** posts each control change to `POST /builder/op`
  (`{session, op, args}`) and applies the returned
  `{draft, patch, cost, warnings, diff}` envelope to its local mirror, then
  re-renders the center + the live preview.
* The **copilot** (the chat pane) posts to `POST /builder/chat` (SSE) and
  applies each `patch` frame to the **same** session draft, because the
  copilot's tools (`copilot_tools.py`) mutate the identical `DraftStore` entry
  via a bound `BuilderToolContext`. A natural-language request and a form click
  therefore accumulate on one contract — and a subsequent `GET /builder/draft`
  reflects either path's edits.

The copilot **drafts** and does not apply: no copilot tool calls apply, so
the model can never roll the epoch on its own. The operator confirms apply in
the form's Review section.

### 2.1 The tools

The copilot tools wrap the same draft operations the form uses (`set_structure`,
`set_param`, `set_holdout`, `set_gate`, board edits, `set_board_meta` for the
board-level `disable_drift`/`judge_only` header, …), so the form and the
chat speak one operation vocabulary. The draft round-trips the board's
`board_meta` header end-to-end: `from_workspace` loads it, every apply writes
it back, and the dry-run's predicted hash includes it — a builder apply can
never strip `disable_drift`/`judge_only` from a live board.

Board authoring is complete at the op layer AND in the GUI: the inline board
editor drives `edit_board_entry` (add/replace) and its delete twin
`remove_board_entry`, and two restore ops
walk edits back — `revert_to_live` (discard the session's edits, restore
from the running contract) and step-`undo` (a bounded 20-snapshot
per-session history recorded before every write at BOTH front doors, so a
form edit and a chat edit share one undo stack). `validate` adds
recommend-only board-authoring codes (duplicate/unsafe entry ids, malformed
dotted paths — shape-checked only, never imported server-side —
rubric/json-schema spec shape, budget outliers, the judge-only flag), and
the read surface feeds the forms: `GET /builder/config` carries a
server-derived enum `vocab`, `GET /builder/draft` carries discovered
`proposer_dirs`. Each tool returns a compact JSON summary
of its patch so the model can narrate the consequence (a cost jump, a new
warning) on the next turn, and the SSE layer reuses that same patch shape to
push a live frame to the form.

### 2.2 The SSE frame schema

`POST /builder/chat` streams `text/event-stream` frames; `builder/stream.js`
dispatches on `frame.type`:

| `type` | Payload | Effect |
|---|---|---|
| `token` | `{text}` | A reply delta — appended to the chat bubble. |
| `tool` | `{name, args}` | A tool step the copilot took — surfaced as a step chip. |
| `patch` | `{patch, cost, warnings, diff}` | The same envelope `/builder/op` returns — applied to the shared draft so the form + preview update live. |
| `error` | `{message}` | Halts the stream and shows the message (e.g. the graceful-degrade hint when no model is configured). |
| `done` | — | Terminates the stream. |

---

## 3. Builder presentation and model configuration

The copilot uses the workspace's named `models.builder` role. This is the sole
model source, including endpoint, credential-variable name, and custom callable;
the Settings model editor documents and validates it with the other roles.

The separate read-only `builder.json` carries presentation concerns only:

```jsonc
{
  "skills": ["zicato-build-tournament", "zicato-build-board"],
  "theme": null
}
```

`GET /builder/config` exposes these values plus a `chat_enabled` flag derived
from `models.builder`; it does not expose engine details. An absent builder role
disables chat while the form keeps working. Engine edits remain runtime infra
and do not roll the epoch.

---

## 4. The consequence-forward principle

Every authoring choice is annotated with its downstream cost before commit:

* **Cost.** The live preview's cost meter shows board-runs per round, broken
  down per contributing factor (field size × board size × replicates, holdout
  re-scoring, …), so an operator sees the compute a structure/field choice
  implies as they make it. The replicates factor uses the **per-structure
  default** (`default_replicates_for` / `STRUCTURE_DEFAULT_REPLICATES`, the
  single source of truth — swiss / single-elim / double-elim = 2, gauntlet /
  racing = 1) when the draft leaves `replicates` unset, rather than assuming a
  flat 1, so the meter matches the schedule a structure actually runs and does
  not under-report its cost. See
  [`TOURNAMENT-STRUCTURES.md §3`](TOURNAMENT-STRUCTURES.md#3-the-five-concrete-strategies).
  The estimate has one owner, `zicato.builder.operations.estimate_cost`; the
  console renders the numbers the response envelope carries and computes none
  of them.
* **Contract impact.** The impact pill states whether applying the current
  draft **rolls the epoch** and which components changed; a draft that touches
  nothing contract-relevant reads "no contract change."
* **Gated apply.** The Review section offers a dry-run preview first; the real
  apply requires an explicit second click to confirm before it writes the
  contract. Applying writes the draft and lets the auto-epoch machinery roll
  the epoch on the next resolve, which is never a silent side effect.

The two builder skills teach the same discipline: surface the cost and the
epoch roll before every apply.

---

## 5. Theming carry-over

The builder and the Settings drawer use **theme tokens only** (`--v2-*` and the
`--zicato-accent` brand token), so they inherit the dashboard's active colour
theme and typeface with no separate styling. The Appearance section of the
Settings drawer surfaces the active colour theme and typeface and points at the
persistent top-bar pickers rather than duplicating them — one source of truth
for theme, carried across every view including the builder. `builder.json` may
carry an optional `theme` hint, but the live UI defers to the operator's
top-bar selection.

---

## 6. Cross-references

| Topic | Document |
|---|---|
| Operator-facing: assembling a whole tournament contract through the GUI | `skills/zicato-build-tournament/SKILL.md` |
| Operator-facing: deep board craft — entries, judges, the weighted loss | `skills/zicato-build-board/SKILL.md` |
| The `SelectionStrategy` seam and the five structures the builder picks from | [`TOURNAMENT-STRUCTURES.md`](TOURNAMENT-STRUCTURES.md) |
| The proposer as a first-class contract input the builder edits | [`PROPOSER.md`](PROPOSER.md) |
| The train/holdout split and the anti-overfitting knobs the board section configures | [`OVERFITTING.md`](OVERFITTING.md) |
| How the gate weights and margin become the scalar loss the tournament consumes | [`SCORING.md`](SCORING.md) |
| The dashboard shell the builder and the Settings panel live inside | [`DASHBOARD.md`](DASHBOARD.md) |
| The `zicato dashboard --view builder` / `zicato dashboard` commands | [`CLI.md`](CLI.md) |
