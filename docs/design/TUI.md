# `zicato tui` — the Console in the terminal

Zicato's interactive surface used to be the browser dashboard alone. The
terminal surface was command-shaped (`zicato reflect`, `zicato epoch rounds`,
`zicato health`) — right for scripting, wrong for *review*: watching a live
round, triaging reflection findings, reading a candidate's gate evidence,
deciding on a recommendation.

`zicato tui` is that review surface: a keyboard-driven terminal console, over
SSH, without a browser.

```
zicato tui                                  # this workspace, service auto-started
zicato tui --url http://127.0.0.1:7892      # attach to a running evolve loop's service
zicato tui --view /e/2026-07-04_e2/gen/v4   # open a lens directly
zicato tui --ascii                          # force the weight-only rendering
```

## Authority: a second renderer, never a second brain

The TUI renders the **same served model** the dashboard does — the dashboard
service's JSON payloads and its SSE stream. There is no TUI-side re-derivation
of verdicts, aggregates or standings: numbers in the terminal are byte-equal to
the browser's because they are the same bytes.

This is structural, not aspirational. The import-linter contract
`tui driver: no import of the other drivers` forbids `zicato.tui` from importing
`zicato.dashboard`, `zicato.cli` or `zicato.builder`. The console reaches a
workspace over HTTP or not at all; it starts a service by argv
(`python -m zicato.dashboard`, the same path `evolve` uses), never by import.

Where the browser *does* derive presentation in JS — the loop verdict's wording,
when a rating reads `provisional`, whether an absent outcome is a rejection, how
a live tournament payload normalizes — that mapping is ported into
`zicato/tui/present.py` and **cross-pinned**:

| witness | file |
| --- | --- |
| the fixture (the contract) | `src/zicato/dashboard/static/test/fixtures/render_crosspin.json` |
| the browser's implementation | `src/zicato/dashboard/static/test/render_crosspin.test.mjs` |
| the Python port | `tests/test_tui_crosspin.py` |

Changing one implementation without the other turns a suite red. The two
surfaces cannot disagree about what "stalled" means.

## The three disciplines, under harsher constraints

**Quiet precision.** Monospace is native here, so tabular alignment is the
primary structure. `1512 ±34 · 7 games`, a faint `provisional` suffix, `—` for
null — never `0`, because zero is a legal measurement. Uncertainty is part of
the number, not an annotation. `view.columns()` measures content widths and
`pad`/`rpad` add the one shared gutter, so a numeric column right-aligns on its
last significant place.

**Semantic colour only.** One accent (the zicato green) for champion /
promotion / "what is happening now"; the verdict vocabulary's severity colours;
dim for provisional and degraded. Style tokens name *meaning* (`accent`,
`good`, `bad`, `warn`, `faint`), never a colour — the Textual layer maps them.
`NO_COLOR` and a non-UTF-8 locale degrade to weight alone and stay fully
readable, because colour is always redundant with a word on the same line. The
ASCII goldens (`tests/goldens/tui/*.ascii.txt`) are what makes that a fact.

**Density through glyph microtypography** (`zicato/tui/glyphs.py`):

- **Sparklines** — braille cells packing two samples each, every sample a 0–4
  dot column from the cell's baseline. A missing sample is a *hole* (blank),
  never a fabricated zero. Braille numbers dot 1 at the top, so the bit order is
  reversed on purpose; getting it wrong renders every trend upside down, which
  still looks like a plausible chart.
- **CI whiskers** — `╟──┼──╢` (ASCII `|--+--|`) drawn on a **shared scale**, so
  the overlap between two candidates' intervals is *visible* rather than
  inferred. Without a shared scale the whisker is omitted rather than drawn on a
  private axis that would invite a false comparison. The point estimate never
  overwrites a cap: losing a bound would understate the interval.
- **Margin bars** — eighth-blocks (`▏▎▍▌▋▊▉█`), champion-anchored, growing right
  for a challenger ahead and left for one behind, with the promote margin marked
  `┆`. A clamp is flagged `›`, never silent. The Δscalar *number* is never
  sign-flipped; only the bar's direction is, and the block says so.
- **Round lifeline** — `propose ▸ screen ▸ tournament ▸ gate` with the current
  stage lit, replacing the browser's animated surfaces with a static, glanceable
  strip. An unknown stage lights **nothing** rather than guessing.

**The digest discipline, ported.** The browser rule — never rebuild DOM on a
no-op heartbeat — becomes: only patch rows whose text changed. Each lens folds a
digest over exactly the values it *renders* (never `generated_at`, never a
sequence number). `Console.refresh()` compares digests before any renderer is
touched, and `Console.row_patches` counts the lines a repaint rewrites. Both are
pinned per lens by `test_a_no_op_heartbeat_patches_zero_cells`, and end-to-end
through widgets by `test_a_no_op_refresh_updates_zero_widgets`.

## Layout and navigation

Three regions: a one-line **status band** (workspace · lens · connection), a
left **rail** of lenses that collapses to a top strip under ~100 columns, and the
**content region** with an **evidence drawer** that opens on selection — the
hovercard equivalent, showing the five-slot evidence for the row under the
cursor.

The five slots, in fixed order, for every selectable row:

| slot | answers |
| --- | --- |
| `what` | which thing is this |
| `measured` | what number was measured |
| `uncertainty` | how sure, or why unmeasured |
| `decision` | what was decided from it |
| `provenance` | where the evidence lives |

Keyboard: `j`/`k` move, `enter` drill, `b` back, `/` filter, `1`–`6` lens jump,
`a` apply, `r` reload, `?` help, `q` quit.

Every view is addressable from the shell with the **same path the browser's hash
router takes** — `--view /e/<epoch>/gen/<gen>` is the candidate dossier in both
places — plus shorthands (`candidate/v4`, `instrument`). One addressing scheme,
two surfaces.

## Lenses

| # | lens | payloads | answers |
| --- | --- | --- | --- |
| 1 | **Home** | `/api/workspace`, `/api/epoch`, `…/trajectory`, `…/cost`, `/api/live/pipeline`, `/api/lineage` | is the loop learning anything, who is champion, what is happening now |
| 2 | **Standings** | `/api/tournaments`, `/api/active-tournament`, `/api/tournament-structure/…` | who is racing, who is ahead, by how much |
| 3 | **Candidate** | `/api/lineage`, `/api/epoch`, `…/gate`, `…/per-entry`, `…/per-judge` | why did this candidate win or lose |
| 4 | **Board** | `/api/epoch`, `…/eval-health`, `…/evals` | can the instrument still measure |
| 5 | **Instrument** | `/api/reflections`, `/api/reflection/{id}/{summary,scorecards,practices}` | is the contract sound, and what should change |
| 6 | **Health** | `/api/health-report`, `/api/health`, `/api/heartbeat`, `/api/logs` | is the loop healthy, and what has it been saying |

**Applying a recommendation** shells out to the CLI. The Instrument lens's
recommendation queue carries the exact invocation
(`zicato reflect apply <reflection> <finding>`); pressing `a` suspends the app
and runs it in the operator's own shell. The console never mutates a workspace
itself, so the audit trail is identical to having typed the command.

## The render-conformance rule

Every evidence field the browser surfaces must be reachable in the TUI **or
named here**. What v1 does not render, and the consequence of each:

| not in v1 | consequence | where it lives |
| --- | --- | --- |
| the tournament **builder** | authoring stays in the browser | `#/builder`, `zicato builder` |
| **settings** (contract / models / appearance) | read-only review only | `#/settings` |
| **publication** (the epoch paper) | no formatted report | `#/e/<id>/paper` |
| **traces** (imported foreign trajectories) | trace review stays in the browser | `#/e/<id>/traces` |
| **mutations** site view + **diff** view | patches are counted, not shown | `#/e/<id>/mutations`, `…/gen/<g>/diff` |
| **conversation transcripts** | per-run transcripts stay in the browser | `/api/conversation/{run_id}` |
| the **compare** target (`~cmp=`) | no side-by-side dossier | any view's `~cmp=` suffix |
| the adjudication **x-ray** | finding evidence lists the `(judge, run_ref)`; the turn-level x-ray is browser-only | `#/e/<id>/instrument/<r>/<judge>/<run>` |
| the **progressive live models** for racing / swiss / elimination | a live non-gauntlet ladder shows the server's *committed* rounds; the browser additionally fills the in-flight rung board-by-board from heartbeat + active-runs | `views/structure.js` `buildLive*Model` |
| the **prediction-accuracy scorecard** | proposer calibration is browser-only | `/api/hypothesis-accuracy/…` |
| **operator controls** (pause / resume / force promote / reject) | the TUI is read-only; use the CLI or the browser | `/api/control/*` |

Each row is a deliberate v1 boundary, not an oversight. The two that are worth
lifting first are the progressive live models (the live ladder is the most
"watched" surface) and the x-ray (the Instrument lens already lands one keystroke
away from it).

A **proposer recommendation queue** (issue #169) is not yet served. The
Instrument lens's queue is built from board-reflection findings today, and the
row shape plus the apply seam (`_apply_command`) are the join point for the
proposer's own recommendations when the service starts serving them. The queue
honestly shows only the source that exists.

## Degrading

Every lens returns a `View` for every input; none raises at the UI.

- **No service** → the lens says so and prints the command that starts one.
- **No index** → the served `note` ("index not built; run zicato reindex") is
  printed, rather than an empty table implying "no data".
- **A lens bug** → the panel degrades with the exception name; the operator
  keeps their session.
- **Null anywhere** → `—`. Never `0`.

## Layout of the code

```
zicato/tui/
  present.py     the ported ui.js derivations — cross-pinned, no new logic
  glyphs.py      sparkline / whisker / margin bar / lifeline, with ASCII forms
  view.py        Span / Row / Block / View, digest_of, render_text
  client.py      HttpClient (stdlib urllib) + SSE; SnapshotClient for tests
  service.py     attach to a running service, or spawn one and read its port back
  routes.py      the browser's hash-path grammar, one scheme for both surfaces
  console.py     navigation + the repaint discipline (no Textual anywhere)
  app.py         the Textual layer: pixels, keys, the SSE worker — nothing else
  lenses/        one module per lens; each is payload -> View, pure
```

Textual is the only dependency the `tui` extra buys — the service client is
stdlib `urllib`, so a terminal install stays small and can review a workspace
from a machine that could not serve one.
