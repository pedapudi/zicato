# The tournament builder — one component, three entry points

> **Status.** SHIPPED. The deterministic backend lives at
> `zicato/builder/{config,draft,operations,api,copilot,copilot_tools}.py`; the
> frontend is a self-contained Console view
> (`dashboard/static/js/variants/T/views/builder.js` + `…/builder/*`); the
> launch surfaces are the dashboard's first-class `#/builder` view, a launcher
> rail entry in the Settings drawer (`…/views/settings.js`), and the standalone
> `zicato builder` CLI (`zicato/cli/commands/builder.py`). The whole stack is exercised by the
> test suite (`tests/test_builder_*.py`, the JS `builder.test.mjs` /
> `settings.test.mjs`, `tests/test_cli_builder.py`). Operator-facing how-to
> lives in the two builder skills.

The **tournament builder** is the GUI for composing an epoch's evaluation
contract — structure, field & noise, the board and its train/holdout split,
the proposer, and the promote gate — as a **draft** that is edited live and
applied only on explicit confirmation. It is the dashboard's flagship authoring
surface: the place an operator turns a vague objective into a concrete,
hashable contract without hand-editing JSON.

The defining decision, repeated because it is load-bearing: **the builder edits
a draft, and applying that draft rolls the epoch.** A different structure, a
different board, a re-weighted loss, a held-out slice, or a swapped proposer all
change the evaluation contract, and a changed contract is — by definition — a
new epoch ([EPOCHS-AND-JOURNALING.md §10.1](EPOCHS-AND-JOURNALING.md#101-whats-in-the-contract)).
So the builder is **consequence-forward**: every choice surfaces its **cost**
(board-runs per round) and its **contract impact** (whether applying rolls the
epoch) *before* the operator commits, and apply is gated behind a deliberate
confirm.

---

## 1. The launch / integration model — one view, three doors

There is exactly **one** builder view component
(`views/builder.js`). It is deliberately self-contained — `render(host)` takes
nothing but a host element, owns its own shared session draft, and never reads
the tree, the route params, or the breadcrumb. That self-containment is what
lets the same component be reached from three doors without a rewrite. The
builder is now its **own first-class view** rendered FULL-WIDTH in the main view
host (`#/builder` is a top-level route returning `{view: "builder"}`, not a
Settings section — `js/variants/T/router.js`); the prior nesting inside Settings
gave it a cramped double-railed centre, so it was promoted out and Settings
keeps only a **launcher** to it:

| Door | Route / entry | What it does |
|---|---|---|
| **First-class view** | top-bar nav → **builder**, or `#/builder` | The flagship home. `#/builder` resolves to the builder view directly and paints full-width in the main host. The same route-agnostic `builder.render(host)` backs every door. |
| **Settings launcher** | top-bar **⚙ settings** → *Tournament builder* rail entry | The Settings drawer (`views/settings.js`) is a section rail (Tournament builder · Contract · Models · Appearance) over one body host. The *Tournament builder* entry is a **launcher** — it does not swap a section; it navigates OUT to `#/builder` so the builder always renders full-width. |
| **Standalone CLI** | `zicato builder` | Boots the same dashboard service as `zicato dashboard` and prints the builder deep-link (`http://127.0.0.1:<port>/#/builder`) so the browser opens on the builder. Loopback-only, same bind rule as `zicato dashboard` / `zicato evolve`. |

Settings does **not** reimplement the builder; it launches it. Its **Contract**
section is a **read-only at-a-glance** of the current epoch's contract that
*reuses the builder's own live preview* (`builder/preview.js` `previewNodes`
fed the current epoch as a draft-shaped contract), rendered read-only — so the
panel reads as "here is the contract as the builder would show it; click out to
the builder to change it." The Settings surface itself is now a routed
right-side **drawer overlay** that paints over the current view (DASHBOARD /
variant-T), not a full page.

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

The copilot is deliberately a **drafting** copilot, never an applying one: no
copilot tool calls apply, so the model can never roll the epoch on its own. The
operator always confirms apply in the form's Review section.

### 2.1 The tools

The copilot tools wrap the same B1a operations the form uses (`set_structure`,
`set_param`, `set_holdout`, `set_gate`, board edits, `set_board_meta` for the
board-level `disable_drift`/`judge_only` header, …), so the form and the
chat speak one operation vocabulary. The draft round-trips the board's
`board_meta` header end-to-end: `from_workspace` loads it, every apply writes
it back, and the dry-run's predicted hash includes it — a builder apply can
never strip `disable_drift`/`judge_only` from a live board.

Board authoring is complete at the op layer: `edit_board_entry`
(add/replace) has its delete twin `remove_board_entry`, and two restore ops
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

## 3. `builder.json` — the copilot's own config

The builder backend owns its **own** config file
(`zicato/builder/config.py`), distinct from the workspace `config.json` and the
per-epoch `scoring.json`. It is read-only to the builder (B1a never writes it),
found at `<workspace>/builder.json` or `<workspace>/.zicato/builder.json`, and
records how the copilot reaches a model:

```jsonc
{
  "agent": {
    "model": "house-model-x",        // empty ⇒ chat disabled, form-only
    "endpoint": null,                 // null ⇒ provider default
    "api_key_env": "HOUSE_API_KEY",   // the NAME of the env var, never the key
    "call_llm": null                  // optional dotted-path override
  },
  "skills": ["zicato-build-tournament", "zicato-build-board"],
  "theme": null
}
```

**Secret safety is structural.** `to_public_dict` — the only surface the REST
layer serializes — carries the API-key *environment-variable name* through but
never resolves it, so a credential can never leak to the UI. The Settings
drawer's *Models* section (which generalised the former read-only "Builder
assistant" read-out into an editable per-role config for harness · auxiliary ·
**builder** · judge, backed by the secret-safe `GET/POST /settings/models`)
surfaces exactly that for the builder role: the model name, the endpoint, and
the `api_key_env` **name** plus a set/unset indicator — never a secret value
(`api_key_env` is a NAME). An absent or empty-model `builder.json` simply
disables chat; the form keeps working. A model / endpoint is runtime infra, so
editing it here does **not** roll the epoch.

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
  flat 1, so the meter matches the schedule a structure actually runs (the
  under-reporting bug class). See
  [`TOURNAMENT-STRUCTURES.md §3`](TOURNAMENT-STRUCTURES.md#3-the-five-concrete-strategies).
* **Contract impact.** The impact pill states whether applying the current
  draft **rolls the epoch** and which components changed; a draft that touches
  nothing contract-relevant reads "no contract change."
* **Gated apply.** The Review section offers a dry-run preview first; the real
  apply requires an explicit second (confirm) click before it writes the
  contract. Applying writes the draft and lets the auto-epoch machinery roll
  the epoch on the next resolve — it is never a silent side effect.

This is the same discipline the two builder skills teach: surface cost + the
epoch-roll before apply, every time.

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
| The `SelectionStrategy` seam + the five shipped structures the builder picks from | [`TOURNAMENT-STRUCTURES.md`](TOURNAMENT-STRUCTURES.md) |
| The proposer as a first-class contract input the builder edits | [`PROPOSER.md`](PROPOSER.md) |
| The train/holdout split + anti-overfitting the board section configures | [`OVERFITTING.md`](OVERFITTING.md) |
| How the gate weights + margin become the scalar loss the tournament consumes | [`SCORING.md`](SCORING.md) |
| The dashboard shell the builder + Settings panel live inside | [`DASHBOARD.md`](DASHBOARD.md) |
| The `zicato builder` / `zicato dashboard` commands | [`CLI.md`](CLI.md) |
