# `zicato tui` — the Console in the terminal

Zicato's interactive surface used to be the browser dashboard alone. The
terminal surface was command-shaped (`zicato inspect reflection`, `zicato epoch rounds`,
`zicato health`) — right for scripting, wrong for *review*: watching a live
round, triaging reflection findings, reading a candidate's gate evidence,
deciding on a recommendation.

`zicato tui` is that review surface: a keyboard-driven terminal console, over
SSH, without a browser.

```
zicato tui                                  # this workspace, service auto-started
zicato tui --url http://127.0.0.1:7892      # attach to a running evolve loop's service
zicato tui --view /e/2026-07-04_e2/gens     # open a lens directly
zicato tui --ascii                          # force the weight-only rendering
```

**v1 ships three lenses** — Home, Standings, Instrument — and is entirely
**read-only**. Candidate, Board and Health are designed below and deferred;
the render-conformance table names every evidence field that defers with them.
This document describes the whole design, and marks what is built.

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

**The digest discipline, ported — as two gates, not one.**

The SSE stream carries **no digest**. It carries exactly three events —
`snapshot` (the `/api/state` body, on connect), `state_change` (coalesced, with
`seq` + `terminal` + `kinds`), `run_log` — plus a `: ping` comment keepalive.
So the no-op question is answered twice:

*The OUTER gate — `seq`.* `state_change` carries the orchestrator's progress
cursor. `Console.note_progress()` ports `core/state.js`'s `noteProgress`
exactly: a **repeated** `seq` is a no-op beat and is dropped **before any HTTP
request**; a **backwards** `seq` means the progress log was cleared on a fresh
`evolve` boot (it restarts at 1), so it forces a full re-apply rather than
freezing the screen on the finished run; an **absent** `seq` degrades to
always-refresh, because a pre-RUNTIME-V2 server gives no cursor to skip on and
a stale screen is worse than a wasted fetch. This is the gate that keeps an
idle console idle *on the wire*.

*The INNER gate — the content digest.* For everything `seq` cannot see (a
reindex, an operator edit, a service restart), each lens folds a digest over
exactly the values it *renders* — never `generated_at`, never a sequence
number, floats folded at their **rendered** precision, an absent feature
contributing `null` so a pre-feature payload digests identically to one that
has the feature switched off. `Console.refresh()` compares digests before any
renderer is touched, and `Console.row_patches` counts the lines a repaint
rewrites.

`snapshot` always refreshes: the server drops a subscriber whose queue
overflows (256 frames), so `snapshot` — sent on connect and on reconnect — is
the only frame that can resynchronise a screen that missed frames. `run_log`
moves no lens this build ships and is dropped.

Pinned by `test_a_repeated_seq_costs_zero_fetches` (outer, zero requests),
`test_a_no_op_heartbeat_patches_zero_cells` (inner, per lens) and
`test_a_repeated_seq_frame_never_reaches_a_widget` (both, end-to-end).

The browser's own `heartbeat` SSE listener is **dead code** — this server never
emits that event — and is deliberately not ported.

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

Keyboard: `j`/`k` move, `enter` drill, `b` back, `1`–`3` lens jump, `r` reload,
`?` help, `q` quit. Deliberately small — there is no filter and no apply,
because there is nothing to apply.

Every view is addressable from the shell with the **same path the browser's hash
router takes** — `--view /e/<epoch>/gen/<gen>` is the candidate dossier in both
places — plus shorthands (`candidate/v4`, `instrument`). One addressing scheme,
two surfaces.

## Lenses

| # | lens | status | payloads | answers |
| --- | --- | --- | --- | --- |
| 1 | **Home** | **built** | `/api/workspace`, `/api/epoch`, `…/trajectory`, `…/cost`, `/api/live/pipeline`, `/api/lineage` | is the loop learning anything, who is champion, what is happening now |
| 2 | **Standings** | **built** | `/api/tournaments`, `/api/active-tournament`, `/api/tournament-structure/…` | who is racing, who is ahead, by how much |
| 3 | **Instrument** | **built** | `/api/reflections`, `/api/reflection/{id}/{summary,scorecards,practices}` | is the contract sound, and what should change |
| — | **Candidate** | deferred | `/api/lineage`, `/api/epoch`, `…/gate`, `…/per-entry`, `…/per-judge` | why did this candidate win or lose |
| — | **Board** | deferred | `/api/epoch`, `…/eval-health`, `…/evals` | can the instrument still measure |
| — | **Health** | deferred | `/api/health-report`, `/api/health`, `/api/heartbeat`, `/api/logs` | is the loop healthy, and what has it been saying |

### Where the browser's non-route modules land

`structure`, `boardstatus` and `evals_health` are **not** browser routes — they
are modules mounted inside `epoch` / `evals`. Their evidence maps onto lenses,
not onto addresses:

| browser module | lens | status |
| --- | --- | --- |
| `views/structure.js` | Standings | **built** (the served model; see the deferred progressive live fill below) |
| `views/boardstatus.js` | Board | deferred |
| `views/evals_health.js` | Board — MDE / dead / noisiest / redundancy are all "can this instrument measure?" | deferred |
| `views/home.js` health panel | Health | deferred |

**Applying a recommendation is something the operator does, not the console.**
The Instrument lens's recommendation queue PRINTS the exact invocation
(`zicato inspect reflection apply <reflection> <finding>`) for the operator to run. v1 does
not even shell out: there is no execution path in this build at all, which is
the smallest surface to trust and to review. (The service refuses control POSTs
under `read_only` regardless — the same conclusion from the other direction.)

## The render-conformance rule

Every evidence field the browser surfaces must be reachable in the TUI **or
named here**. Two kinds of absence, and they are different promises:
**deferred** work is coming back, and its address already resolves; **browser-
side by design** is a v1 non-goal that is not planned to move.

### Deferred lenses (coming back)

A pre-descope build of all three lives unmerged at `feat/tui-full-six-lens`
(`a2f9d7e`) — kept deliberately as the re-landing base, not stale cruft. Do
not delete it; re-landing is a **port**, not a cherry-pick (it predates the
four-absence vocabulary, the current routes table, and the removal of
`margin_bar`, and its goldens need regenerating).

| not in v1 | consequence | evidence that defers with it |
| --- | --- | --- |
| **Candidate** dossier | no per-candidate gate evidence in the terminal; the standings row + drawer carry the Δscalar, decision and rating instead | the four gate rules with pass/fail and margins, `deciding_rule`, the Bradley–Terry pre-gate (θ̂ whiskers, `p_stronger`), facet table, per-board rows, per-judge losses, lineage strip, operator-override provenance |
| **Board** status | no view of whether the instrument can still measure | train/holdout split, rotation cadence + `refresh_recommended`, holdout budget, MDE + power, dead / noisiest / insufficient entries, redundancy clusters, the per-entry outcome heat-strip |
| **Health** + logs | loop-health findings and the log tail stay in `zicato health` / `zicato inspect logs` | health findings (severity, detector, summary **and `detail` — see below**), service identity, heartbeat phase/paused, the cursor-based log tail |

`/e/<epoch>/gen/<gen>` still resolves: it lands on the Standings row it would
have opened from and the status band says `candidate is not in this build`. An
address the operator can type always resolves, and always admits what it could
not give them.

**When Health is built, read `finding.detail`.** The browser's `home.js`
`healthPanel` renders only `summary`/`message` and drops `detail` — including
`detail.recommendation`, the actionable half. That is a five-slot violation on
the browser side, and the terminal Health lens must not replicate it.

### Browser-side by design (v1 non-goals)

| not in v1 | consequence | where it lives |
| --- | --- | --- |
| the tournament **builder** | authoring stays in the browser | `#/builder`, `zicato dashboard --view builder` |
| **settings** (contract / models / appearance) | read-only review only | `#/settings` |
| **publication** (the epoch paper) | no formatted report | `#/e/<id>/paper` |
| **traces** (imported foreign trajectories) | trace review stays in the browser | `#/e/<id>/traces` |
| **mutations** site view + **diff** view | patch contents are not shown | `#/e/<id>/mutations`, `…/gen/<g>/diff` |
| **conversation transcripts** | per-run transcripts stay in the browser | `/api/conversation/{run_id}` |
| the **compare** target (`~cmp=`) | no side-by-side dossier | any view's `~cmp=` suffix |
| the adjudication **x-ray** | finding evidence lists the `(judge, run_ref)`; the turn-level x-ray is browser-only | `#/e/<id>/instrument/<r>/<judge>/<run>` |
| the **progressive live models** for racing / swiss / elimination | a live non-gauntlet ladder shows the server's *committed* rounds; the browser additionally fills the in-flight rung board-by-board from heartbeat + active-runs | `views/structure.js` `buildLive*Model` |
| the **prediction-accuracy scorecard** | proposer calibration is browser-only | `/api/hypothesis-accuracy/…` |
| **operator controls** (pause / resume / force promote / reject) | the TUI is read-only; use the CLI or the browser | `/api/control/*` |

Each row is a deliberate boundary, not an oversight. Ordered by what is worth
lifting first: **Candidate** (the gate evidence is the reason to open a
terminal at all mid-round), then the **progressive live models** (the live
ladder is the most-watched surface), then **Board**, then the **x-ray** (the
Instrument lens already lands one keystroke away from it).

A **proposer recommendation queue** (issue #169) is not yet served. The
Instrument lens's queue is built from board-reflection findings today, and the
row shape plus `_apply_command` are the join point for the proposer's own
recommendations when the service starts serving them. The queue honestly shows
only the source that exists.

## Degrading

Every lens returns a `View` for every input; none raises at the UI.

- **No service** → the lens says so and prints the command that starts one.
- **No index** → the served `note` ("index not built; run zicato repair index") is
  printed, rather than an empty table implying "no data".
- **A lens bug** → the panel degrades with the exception name; the operator
  keeps their session.
- **Degrade shapes are not success shapes.** At least five endpoints serve
  *fewer keys* when their inputs are absent (`tournament-structure` without
  `field_status`, `per-entry` without `mean_score`/`facet_scores`, `gate`
  without `scalar_decomposition`/`override`, `tournaments` without
  `structure`, `reflection` summary/practices). Lenses code on key PRESENCE,
  never on an assumed shape. `heartbeat` and `active-tournament` can also be
  JSON `null` outright.

### The four absences

"Nothing here" is four different facts and they never collapse into one glyph,
because the operator's next action differs in each case:

| case | renders | means |
| --- | --- | --- |
| no measurement | `—` | the run produced no value. **Never `0`** — zero is a legal measurement. |
| measured-impossible | `n/a — insufficient replication` | defined, but not computable at this `n` |
| feature off | the row/panel is **omitted** | nothing was asked for; a row would imply it was, and came back empty |
| unmeasured (the third verdict) | `unmeasured · <reason>` | measurable, not measured — and the reason is the actionable part |

`present.measured()` picks between the first, second and fourth; the third is
the absence of a row, so callers omit the block. One caveat worth stating: where
the TUI mirrors a **cross-pinned** browser rendering it keeps the browser's
answer even if a richer absence exists — an unrated generation prints `—` in
the standings because `ui.js` does, and the cross-pin is the stronger
constraint.

## Layout of the code

```
zicato/tui/
  present.py     the ported ui.js derivations — cross-pinned, no new logic
  glyphs.py      sparkline / whisker / lifeline, each with an ASCII form
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

The broader `observability` profile installs both renderers, their service, and
live execution telemetry. The `all` profile adds every other runtime feature;
see [`INSTALL-PROFILES.md`](INSTALL-PROFILES.md).
