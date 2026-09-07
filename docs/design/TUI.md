# `zicato tui`: terminal review

`zicato tui` reviews a workspace over SSH or in a local terminal. It consumes
JSON and server-sent events from the dashboard service. The service owns
candidate decisions, ratings, aggregates and recommendations; the terminal
formats that evidence and provides keyboard navigation.

```sh
zicato tui
zicato tui --url http://127.0.0.1:7892
zicato tui --view /e/2026-07-04_e2/gen/v4
zicato tui --view /e/2026-07-04_e2/evals
zicato tui --view /logs --ascii
```

The navigation contains Home, Standings and Instrument. Candidate, board and
health evidence opens within those views. The console is read-only: it prints
review and apply commands but never executes them.

## Navigation and evidence

A status line shows the connection and selected address. The navigation sits
beside the content on wide terminals and above it below 100 columns. The
selected view's name uses the accent colour. The evidence drawer describes the
selected row's subject, measurement, uncertainty, decision and
provenance. Long facts wrap in the content area; moving the selection scrolls
its row into view.

Use `j`/`k` or arrows to move, `enter` to open a row, `b` or `escape` to return,
`1`–`3` to change views, `r` to reload, `?` for help and `q` to quit. Filtering
is not provided. Browser paths and terminal shorthands resolve to the same
coordinates; navigating back preserves the selected epoch and generation.

| Navigation | Evidence | Addresses |
| --- | --- | --- |
| Home | Loop verdict, champion, live round, health findings, service identity, runtime and logs | `/e/<epoch>`, `/e/<epoch>/health`, `/logs` |
| Standings | Tournament standings and candidate dossiers | `/e/<epoch>/gens`, `/e/<epoch>/gen/<generation>[/<entry>]` |
| Instrument | Reflection findings, recommendation remedies, board quality and run evidence | `/e/<epoch>/instrument[/<reflection>]`, `/e/<epoch>/evals`, `/e/<epoch>/boards`, `/e/<epoch>/board/<entry>[/<generation>]` |

### Candidate review

A candidate detail reads one shared dossier from
`/api/epoch/<epoch>/candidate/<generation>`. The response owns the candidate's
identity, decision, rating, experiment, parent and children. A generation name
is always paired with its epoch, including a parent from another epoch.
The terminal does not join an unscoped lineage list to reconstruct a decision.

The dossier renders every recorded gate rule, its result and explanation,
`deciding_rule`, scalar and pass-rate margins, regressions, scalar contributions,
recorded rating intervals and operator-override provenance. An older decision
without a rule breakdown remains visible with an explanation that the breakdown
was not recorded. An absent gate is not a rejection.

Facet scores, champion comparisons, per-entry results and judge losses follow
the gate. Opening an entry displays its served run header, expectation outcomes
and judge evidence. Related generations open their own scoped dossiers.

### Board and recommendation review

Instrument's board detail shows the train/holdout split, rotation cadence and
recommendation, holdout budget, minimum detectable effect and power, noisy or
non-discriminating entries, insufficient comparisons, runtime cost and
redundancy. The outcome matrix retains the service's entry and candidate order.
Each cell shows a numeric pass ratio with a density mark; an absent cell is an
em dash. Opening an entry shows its evaluation dossier and recorded candidate
runs, which lead to the same run evidence used by candidate review.

Instrument also shows board-reflection findings and their proposed operation
arguments. The proposer recommendation queue includes each finding's evidence,
remedy text, recorded diff and content digest. Both queues print the existing
CLI apply command. Proposer commands include the owning epoch. The operator
reviews the remedy and runs the command in the intended workspace; no terminal
action writes a recommendation or invokes a shell.

### Health and logs

Home's health detail retains each finding's complete `detail`, including nested
recommendations and measurement evidence. Missing reports say they are
unavailable. The health endpoint describes the active epoch; selecting a
different historical epoch does not relabel that report as historical evidence.
Service identity and workspace runtime remain explicitly workspace observations.

Logs are a refreshed tail of at most 200 records from the selected invocation.
A scoped health address displays only records with that epoch identity; `/logs`
shows the workspace tail. This is not an append cursor or a complete archive.
Rewriting a message can repaint the view even when the record count and cursor
stay unchanged. Use `zicato inspect logs` for further log inspection.

## Rendering rules

Monospace is native to the terminal and supports aligned data columns. Semantic
styles distinguish promotion, warnings, failures and unavailable evidence;
colour always accompanies a word. `NO_COLOR`, a non-UTF-8 locale or `--ascii`
uses the ASCII rendering.

A missing measurement renders as `—`, never zero. An undefined statistic says
why it cannot be computed; a disabled feature is omitted. A historical or
unreadable record remains distinguishable from an empty one. Served index
repair notes remain visible. The terminal does not compute a verdict from a
partial payload.

Ratings carry their uncertainty. Confidence-interval whiskers use a shared
scale; without comparable bounds, the graphic is omitted. Braille sparklines
leave missing samples as holes. A live round uses a static lifeline with the
served phase highlighted. Gate margins remain signed numbers rather than an
additional bar convention. The outcome matrix quantizes only its density mark;
its printed pass ratio remains the served value at display precision.

Presentation mappings shared with the browser are checked by the render
cross-pin fixture in `static/test/fixtures/render_crosspin.json`, its browser
test and `tests/test_tui_crosspin.py`. The import boundary prevents the terminal
package from importing dashboard, CLI or builder implementations. It reads a
workspace through HTTP and starts a local service by command arguments.

## Refresh and resource ownership

Content revisions invalidate reads independently of progress. Repeated events
with unchanged revision and progress require no request. Progress advancement,
restarts and reconnect snapshots also refresh. Older servers without revision
metadata use changed regions; periodic reads recover from a disconnected event
stream or a transient read failure. Log events invalidate the views that show
logs.

View construction runs in one worker with at most one pending refresh. Each
read captures its route and width. A navigation counter rejects results from
abandoned addresses, including navigation away and back to the same address.
Only the UI thread applies views and updates widgets.

A content digest then gates painting. It includes rendered rows, evidence and
actions, using display precision for numbers. An unchanged view patches no
rows. Detail digests include nested finding evidence and remedy contents, so a
content edit is not hidden by unchanged counts or timestamps.

Shutdown stops scheduling, closes the HTTP client and ignores late results. It
cannot force-cancel a thread already reading. Ordinary HTTP requests use an
eight-second socket timeout; the event stream uses at least twenty seconds for
its fifteen-second keepalive. Their timers bound inactivity while responses
may continue receiving data. Workers close responses when I/O finishes. An ended event
stream leaves periodic refresh active.

## Browser-only evidence and controls

Every browser evidence surface either has a terminal route above or an explicit
boundary below.

| Browser surface | Terminal boundary |
| --- | --- |
| Builder and settings | Authoring remains in the browser. |
| Publication | Formatted epoch reports remain in the browser. |
| Imported traces | Trace visualization remains in the browser. |
| Mutation sites and candidate source diffs | Source patch inspection remains in the browser. Recommendation remedies are available in Instrument. |
| Conversation and proposal-episode transcripts | Full transcripts remain in the browser. Run headers, expectations and judge losses are available in candidate detail. |
| Side-by-side comparison (`~cmp=`) | The suffix is ignored; the selected candidate still opens. Served champion comparisons remain in its dossier. |
| Adjudication x-ray | Turn-level judge inspection remains in the browser; reflection evidence retains judge and run references. |
| Progressive live racing, swiss and elimination figures | Standings shows served committed rounds; the browser additionally fills an in-flight round board by board. |
| Prediction accuracy and proposer scorecard trends | These diagnostic scorecards remain in the browser. Pending proposer findings and remedies are available in Instrument. |
| Pause, resume and decision overrides | Controls remain in the browser or CLI. Recorded override provenance is available in candidate detail. |

Unsupported browser-only addresses resolve to an existing view and name the
unsupported surface. The preserved `feat/tui-full-six-lens` branch is a review
reference; the implementation uses the three existing navigation identities and
the shared dossier rather than its independent candidate joins.

## Code and validation

`routes.py` owns addresses; `console.py` owns navigation and refresh state;
`app.py` owns widgets and worker scheduling. `lenses/` formats served payloads,
with candidate, board and health detail in `lenses/review.py`. `client.py` owns
HTTP responses and events. The optional `tui` extra supplies the terminal UI
library; see [installation profiles](INSTALL-PROFILES.md).

Focused tests cover real service reads, scoped candidate identity, missing and
historical evidence, recommendation records, nested evidence edits, unchanged
refreshes, keyboard scrolling, narrow layout and ASCII output. Existing Home,
Standings and Instrument snapshots retain their rendering checks. Detail
regressions assert evidence and navigation directly instead of adding another
set of broad snapshots.
