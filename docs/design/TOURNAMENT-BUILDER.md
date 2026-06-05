# The tournament builder — one component, three entry points

> **Status.** SHIPPED. The deterministic backend lives at
> `zicato/builder/{config,draft,operations,api,copilot,copilot_tools}.py`; the
> frontend is a self-contained Console view
> (`dashboard/static/js/variants/T/views/builder.js` + `…/builder/*`); the
> launch surfaces are the dashboard `#/builder` deep-link, the dashboard
> Settings panel (`…/views/settings.js`), and the standalone `zicato builder`
> CLI (`zicato/cli/commands/builder.py`). The whole stack is exercised by the
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
lets the same component be reached from three doors without a rewrite:

| Door | Route / entry | What it does |
|---|---|---|
| **Settings panel** | top-bar **⚙ settings** → *Tournament builder* section | The flagship home. The Settings surface (`views/settings.js`) is a section rail (Tournament builder · Contract · Builder assistant · Appearance · Dashboard) over one body host; the builder section hands that host straight to `builder.render(host)`. |
| **Deep-link** | `#/builder` | The router resolves `#/builder` into `settings/builder`, so the canonical deep-link still works and opens the surface already focused on the builder. |
| **Standalone CLI** | `zicato builder` | Boots the same dashboard service as `zicato dashboard` and prints the builder deep-link (`http://127.0.0.1:<port>/#/builder`) so the browser opens on the builder. Loopback-only, same bind rule as `zicato dashboard` / `zicato evolve`. |

The Settings panel does **not** reimplement the builder; it re-homes it. The
contract section beside it is a **read-only at-a-glance** of the current epoch's
board · brief · scoring · proposer · overfitting (sourced from `/api/epoch`),
with every row linking *into* the builder to edit — so the panel reads as
"here is the contract, here is where you change it."

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
`set_param`, `set_holdout`, `set_gate`, board edits, …), so the form and the
chat speak one operation vocabulary. Each tool returns a compact JSON summary
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
panel's *Builder assistant* section surfaces exactly that: the model name, the
endpoint, the `api_key_env` **name**, the `chat_enabled` flag, and the composed
builder skills — and nothing that could be a secret value. An absent or
empty-model `builder.json` simply disables chat; the form keeps working.

---

## 4. The consequence-forward principle

Every authoring choice is annotated with its downstream cost before commit:

* **Cost.** The live preview's cost meter shows board-runs per round, broken
  down per contributing factor (field size × board size × replicates, holdout
  re-scoring, …), so an operator sees the compute a structure/field choice
  implies as they make it.
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

The builder and the Settings panel use **theme tokens only** (`--v2-*` and the
`--zicato-accent` brand token), so they inherit the dashboard's active colour
theme and typeface with no separate styling. The Appearance section of the
Settings panel surfaces the active colour theme and typeface and points at the
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
