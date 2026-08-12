# Changelog

### Racing slices can be deterministic and tag-balanced

Racing now supports `tournament.params.slice_schedule`.
`"prefix"` preserves the legacy authored-board order, while the new
`"stratified_random_v1"` derives a reproducible, tag-balanced permutation
from the frozen board and uses nested prefixes for its rungs. New workspace
scaffolds opt into the latter; adding it to an existing racing contract rolls
the epoch, so historical evaluations keep their original schedule.

### A guarantee that was documented for years and never enforced

`MutationPoint.content_hash`'s docstring said "the patch applier checks this
before applying a patch so a stale proposer round cannot clobber an
already-rewritten region." The applier never read it. The field was written by
the enumerator, rendered by the CLI and the dashboard, and checked by nothing —
a stale round could clobber a rewritten region exactly as if the sentence had
never been written.

`validate_patches` tier 1 is now that check, and this module is the field's
only reader. It compares `content_hash` for each patched point between the
manifest the proposal was drafted against and a fresh enumeration of the parent
snapshot; a point whose hash moved was rewritten under the proposer, so the
draft is reasoning about text that no longer exists. Caught there, a fix is one
tool call; caught at derive time it would have been a failed round with no
route back to the proposer that could fix it. A point that *vanished* rather
than moved is still left to A2 — one fault should cost one fix, not two.

This replaces the guard shipped in the previous entry, which asked the proposer
to declare a `content_sha256` digest per patch. That was worse twice over: it
made the guard opt-in, so a model that omitted the field was simply not checked
— and the whole value of the guard is the case where the model does not know
anything is wrong — and it asked the model for arithmetic it has no reason to
get right. Comparing two enumerations zicato already computes needs no
cooperation from the model and no change to the `Experiment` schema. `Patch`
carries no pre-image field and must not grow one. The docstring now describes
what actually checks it, and a test pins that the comparison has exactly one
site, since "plenty of mentions, zero readers" is how it stayed wrong.

`ProposerContext` also grows `generation_root` — the parent snapshot the round
is about to patch, resolved once by the orchestrator instead of re-derived from
the generation store's path convention by every consumer. It is a REQUIRED
argument at the single `_propose_child` construction site rather than a
defaulted field, because a field that defaults to `None` is precisely the shape
that goes silently unpopulated while everything appears to keep working.

### The proposer can check its own patch, and ask the run corpus questions

Three additions to the proposer's tool surface, all governed by one line now
written into `docs/design/PROPOSER.md` §2.10:

> **The proposer may check its patch by any means that consumes no board data
> and produces no scores; it may never execute board entries.**

That is the line separating a legitimate self-check from grading your own
work. A proposer that could run against a slice it chose would be doing the
tournament's job with none of the tournament's guards.

**`validate_patches` — proposing stops being open-loop.** Both existing
proposer tiers emitted a patch set and were done; neither had ever seen its
own output checked. For a span replace that is fine — the applier re-quotes
span content as a string literal, so a span edit cannot break syntax or drop
an import. For a **file-marker `replace`**, `new_content` is a whole
post-edit module that must satisfy A1–A4 of MUTATION-SURFACE.md §6, and
emitting it in one shot and hoping is exactly the workload a tool-using agent
exists to avoid — a violation cost a full retry round-trip, re-sending the
entire manifest. The new tool applies a draft patch set to a scratch copy of
the parent snapshot and reports what broke, in three tiers that stop at the
first failure: **structure + apply** (the shape and cross-check passes, a new
pre-image guard that catches a point rewritten under the draft, then
A1–A4); **static analysis** (the workspace's declared linters, reported as
the DELTA against the same checks on the unpatched tree, so a patch is never
blamed for the tree's pre-existing lint debt); and a **load probe** that runs
the same `adapter.load` the tournament runs before any entry executes, one
expensive round earlier. Tier 1 reimplements nothing — every check is
machinery the round pipeline already applies *after* the proposer answers.
The contribution is running it before. It is in `DEFAULT_PROPOSER_TOOLS`, so
the ADK default proposer gets the closed loop, not only an external agent.

A missing linter or an adapterless workspace produces a **note**, never a
failure. A validator that rejected a good patch because a dev tool was
uninstalled is a validator the proposer learns to ignore.

**Redacted, train-slice-only run-corpus queries.** The proposer's other
channels are pre-rendered: the orchestrator samples and bands the patterns,
the failure profile and the process exemplars before the proposer sees them.
Three new tools make a small, provably-clean part of the same corpus
queryable on demand — same privacy envelope, asked rather than pushed. They
report banded per-entry incidence of drift kinds, of agent roles, and of
process failures across the champion's train slice. The envelope, not the
feature, is the deliverable: the train slice is **re-derived, never accepted
as an argument**, through the same `split_board` / `rotation_seed` pair the
tournament uses, and fails closed to "unavailable" with no data rather than
ever falling back to the whole board; a second, independent gate re-filters
by entry id afterwards, because a single gate is one refactor away from being
bypassed. Reading is default-deny over a narrow allowlist of closed-vocabulary
event fields with **no free-text field admitted at all**, which is what keeps
the task prompt and every completion summary structurally unreachable rather
than merely scrubbed. Figures are banded and stable within a reign, so there
is no round-over-round surface to hill-climb. The design invariant is
PROPOSER.md §2.5's: **feed the marginal, never the joint**.

**The tools are also served over MCP.** A thin stdio server exposes the same
functions, unchanged, to a proposer that is not an in-process ADK agent. It
reimplements nothing — every call lands on the registry function inside the
same context binding the ADK path uses, so the mutable-root escape guard has
no second implementation to drift — and the tool list is derived by reflecting
over the registry, so a tool added there is served with no edit. One server
per challenger process, because the context var is process-wide.

Three structural notes for anyone editing this code. The tier-3 probe lives
in its own `_load_probe.py` and is reached by *spawning* a subprocess, not by
importing the adapter factory; the contextvar plumbing moved to
`tool_context.py` so the validator can reach the context without dragging the
analyzer, and through it the board loader, into its import closure; and the
R3/R4 redaction primitives moved to `analyzer/redaction.py` so the exemplar
channel and the query surface apply byte-identical redaction. Those three
splits are what make the governing principle enforceable *structurally* — an
import-linter contract forbids the validator any path to the board, the
adapters, the tournament worker, the emulator or the judges, with a runtime
closure test as its twin.

The tier-2 static-check set is **contract, not configuration**
(`contract.proposer_static_checks`), folded into the proposer component of
the contract hash, because changing which checks the proposer holds itself to
changes what it proposes. The declarable names are a closed registry rather
than operator-supplied command lines: a hashed name is a reviewable identity,
a hashed command line is an arbitrary-execution surface a contract edit could
widen silently. The empty default is omitted from the canonical form, so every
workspace that never configures it hashes byte-identically to before.

### The builder reaches every contract knob, and a guard says so

`holdout_margin` and `holdout_entry_regression_budget` — the holdout
confirmation's own bounds — reached a working gate with no builder path at
all. `ladder.threshold` had an op but no GUI row. Neither was caught, because
the completeness pin only ever inspected knobs that already declared
themselves to it: a field with no `builder_op` metadata was invisible, so
"forgot the builder entirely" was the one half-wired shape it could not see.

All three are now settable from the builder (and from the chat copilot),
and the metadata is backfilled across the 24 knobs that had working ops but
no declaration. Both margin-shaped knobs follow the reset asymmetry the
generation ceiling already used: `None` means "leave unchanged", so a
NEGATIVE value is the token that clears the pin back to auto — reuse
`promote_margin` for the holdout margin, derive from it for the Ladder's
release threshold.

The class is closed rather than the three instances patched. Every contract
knob must now either carry a `builder_op` or sit in an explicitly justified
exemption set, and an exemption cannot outlive its field. The eight
survivors are the nested config containers, the dotted callable specs (a GUI
field naming arbitrary importable code is a code-execution surface, not a
knob), and the open TransformSpec mappings that have no fixed row shape.
Backfilling also revealed fifteen knobs the builder's own test suite never
asserted, and a registry keyed by bare field name that silently collapsed
`OverfittingConfig.enabled` onto `LadderConfig.enabled`.

Purely additive: every default is unchanged and the contract hash is
unmoved, so no shipping workspace rolls.

### Target 4: a coding agent's configuration package as the system under test

A fourth dogfood target, shipped as a skeleton. The mutable tree is an
external coding agent's *configuration package* — `AGENTS.md` plus
`skills/*.md`, carrying native markdown markers — which the agent loads at
startup, so a generation snapshot of it is an agent identity and promoting
a generation promotes a configuration.

"An agent improves itself" is not circular here because it decomposes into
two instances of one binary in different roles. The target is the config
package. The entrypoint is a driver *outside* the tree (`adapter.kind =
"import"`) that spawns the binary in rpc mode with its agent directory
pointed at the snapshot, under `telemetry_dialect: transcript` — no
goldfive, no ADK, no Python inside the tree. The gate, not either
instance, decides what survives.

The driver forwards each turn to the run's sinks as `{"role", "content"}`,
which is what the transcript dialect already reads, so the whole path needs
no new plumbing. It also diffs the run's working tree against the fixture
and appends that diff to `final_output`: `RunResult` carries only what an
agent *said*, and for a coding agent the evidence is what it *did*.
Hygiene is per-run — a fresh agent directory so the read-only snapshot is
never mutated, an environment allowlist so a caller's credentials cannot
travel, the wall-clock budget as a hard kill, and the binary's `--version`
probed and recorded (a version bump is an epoch boundary by convention).

Two things are deliberately not surface. `settings.json` is immutable
permanently: strict JSON cannot host a comment, so it cannot host a marker.
`extensions/*.ts` wait on one syntax-table entry (#168) — the example ships
no `extensions/` rather than one that silently enumerates nothing.

Nothing here runs live. The tests drive a hermetic stand-in binary as a
real subprocess speaking the real protocol, with no model anywhere, and the
README states the rule before any real use: measure the agent-vs-itself A/A
floor first, because each board entry is a full agentic run and the shipped
`promote_margin` is a framework default, not a calibration.

### Any file can be a mutation site, not only `*.py`

The native marker pass walked `*.py` and nothing else, so a markdown prompt
or a YAML policy sitting inside a registered mutable tree was invisible to
the enumerator — the only route to a non-Python surface was the
goldfive-shaped manifest bridge. Markers now live in any text file whose
type the contract declares. The built-ins ship the proven set — `.md`,
`.markdown`, `.txt`, `.yaml`, `.yml`, `.toml` under `#` and `<!--` lead-ins
(a trailing `-->` tolerated), plus the reserved `.py` — and the operator
declares anything further in a `mutation_surface` syntax table, naming each
file type's comment leaders and closers. So a TypeScript or SQL surface is
a contract edit rather than a zicato release; see MUTATION-SURFACE.md §2.5.

**The marker requirement is unchanged.** This widens *where* a marker may
live, never whether one is needed; an unmarked `config.yaml` is exactly as
immutable as an unmarked `agent.py`. Nothing is carried speculatively: the
built-in set stays at what the supported formats actually use, and a
declared file type widens the surface — so it rolls the epoch, exactly like
any other contract change. Outside Python the `:file` and `:code` region
forms apply and the bare span form does not — "the nearest
string literal beneath" is an AST fact, and the tempting line-shaped
approximation would let a `replace` swallow the `temperature:` key it was
aimed at. A bare span marker in a text file now warns by name instead of
vanishing. The region form carries the safety argument: both anchors are
explicit, operator-written, and OUTSIDE the mutable range, so a patch can
neither escape the region nor delete its own markers (echoed marker lines
are stripped from the replacement).

Discovery is the declared suffix table, not a content sniff — the enumerator
re-runs after every applied patch, so the walk stays on the hot path, and a
suffix decides without opening a file and cannot wander into a binary. The
declared leaders are load-bearing beyond discovery: the applier strips
echoed marker lines out of a region body under them, so a file type with no
declared comment syntax is one whose regions could not be kept contained —
which is why a leaderless declaration is rejected. Enumeration is one
pipeline, with Python a specialization that contributes the AST context
(docstring lines + span resolver) rather than a second walk alongside.
Vendored directories are pruned from the text pass only (pruning
the Python pass would move existing points), text files over 2 MB are
skipped, and a single-file root now resolves by suffix instead of
enumerating to zero in silence.

Python enumeration is byte-identical: `.py` files are still parsed under
the historical `#`-only grammar, which makes that a property of the
grammar rather than a claim a test has to keep re-establishing. A
golden-equality pin over a fixture tree — captured from the pre-widening
enumerator — holds it, and every parity gate stayed green.

Post-apply, the `.py` syntax gate is unchanged and there is deliberately
no non-Python counterpart: "still parses" has no cheap, dependency-free
meaning for markdown or YAML, and a gate covering only the one format the
standard library can check would buy inconsistent protection at the cost
of a second validation path. Strict JSON is not walked at all — it has no
comment syntax, so it cannot host a marker.

The manifest bridge is unchanged in shape but finally has direct tests,
which turned up a latent bug: it re-derived its effective source root as
`manifest_path.parents[2]`, correct for `goldfive/optimization/manifest.toml`
but one level too high for the bare `optimization/manifest.toml` layout, so
every `source` under that layout resolved to a missing path and the bridge
returned nothing. It now uses the base the manifest was found under. Since
that shape enumerated zero points, the fix can only add points.

### The scalar's discards get a record: the Pareto frontier (issue #139)

The promote gate keeps one generation per round and picks it by a weighted
sum. A weighted sum is a projection, so a challenger that halves cost for a
sliver of rubric loses the scalar, is rejected, and is never mentioned
again. Each epoch now carries `epochs/{epoch}/pareto_frontier.json`: the
settled candidates that beat the reigning champion by at least
`promote_margin` on at least one scoring axis and that nothing else on the
record dominates.

**Record only, and default on.** It never enters the gate, selection, the
proposer's prompt, or the champion pointer — the same posture as the Elo
fold and the RoundLog, which is why it needs no knob to be safe. It also
adds no knob to work: the axes are the non-zero
`ScoringWeights.namespace_weights` keys, the direction is each weight's
sign, the units are the already weight-multiplied `namespace_aggregates`
(so one threshold serves axes whose raw units differ by orders of
magnitude), the threshold is `promote_margin`, and the epoch is the key —
a promotion keeps the frontier, an epoch roll starts it empty.

Admission reuses the gate's own `namespace_monotonicity` rule (promoted to
the public `tournament.gate.regressed_namespaces`, one implementation for
both callers), so a candidate that guts rubric or introduces schema
failures cannot buy its way onto the record with a cost win. Placebo arms
are refused; the multi-challenger path fields them inside the slate, so
that check is load-bearing. On a promotion, members the new champion
dominates or that regress against it are MOVED to a `retired` list with the
round and the reason — never deleted.

Surfaces: one INFO line and one additive `frontier_updated` round-log
event, both only when membership moved, plus an additive `pareto_frontier`
table in the analytical index (schema v13; the workspace file stays
canonical and `zicato reindex` re-derives every row). No UI and no new CLI
command yet. Proposer exposure, frontier recombination, and slate steering
are registered as deferred generator-arsenal work in
`docs/design/PARETO-FRONTIER.md` §8 — they are what turn a record into a
decision, and they ship on their own evidence.

Also, a forward-compat note recorded on `ExpectationKind` while hardening
the hydration boundary below: adding a member to that enum is now a
cache-invalidating change for older readers, because `loss.json` stores the
bare token and the reader coerces it back. `BoardEntryKind` reserves slots
for exactly this; `ExpectationKind` has none, so a sixth member needs a
reserved slot landed a release ahead.
### Round-completeness verification: liveness is not integrity (issue #97)

A long parallel measurement sweep lost its model-endpoint credentials
mid-run. The cell straddling the outage did not fail cleanly: it kept the
rounds that had already run, satisfied a "did we reach the model?"
liveness probe, was marked complete, and contributed a mean built from
fewer duels than its peers. Cell-level accounting reported every cell
healthy while 83 of 496 rounds had emitted no `gate_evaluated` at all —
and the loss was correlated with the arm being measured, so it biased the
baseline the whole comparison was anchored to. `zicato.epoch.round_integrity`
is the check at the granularity the ENDPOINT consumes: a pure reader over
`epochs/{epoch}/rounds/*/round_log.jsonl` that classifies every round
COMPLETE (settled with a gate), SETTLED-DEGRADED (settled without a gate,
but the proposer was reached and returned an invalid patch — a real
measurement of a degraded arm), or VOID (everything else: a torn or partial
log, a gateless round carrying a hard infra error, or one that closed with
neither a measurement nor an explanation). The degraded class is
deliberately narrow — voiding it would send a legitimately-degraded arm
around the retry loop to exhaustion — so VOID is the default and acceptance
needs positive evidence. A cell is accepted iff it contains zero VOID rounds
AND at least one round: zero rounds is vacuously free of void rounds, so
`--verify` fails an empty epoch too rather than letting a cell that never
ran read as healthy. The infra-error vocabulary is matched only against
errors raised at the CALL BOUNDARY: every other string in a round's error
trail is a post-response content rejection quoting text zicato does not
control — validator findings over the child agent's own source, mutation
ids, the model's own offending values — so a challenger that breaks a file
named `auth.py` would otherwise read as a credential outage, and would do
it in an arm-correlated pattern, manufacturing the contamination shape the
check exists to detect. `zicato epoch rounds` renders the per-round evidence —
matched error strings verbatim, never a bare boolean — with `--verify` for
the exit code and `--json` for a harness.

### The campaign doc records its results and its resolution limit (issue #97)

`docs/design/CAMPAIGN.md` was a pre-registration with no results and a cost
model roughly an order of magnitude optimistic (it projected ~2.5 h of
parallel board-run wall-clock; a 144-cell run measured 7 h 23 m). It now
opens with the standing record: every pre-#110-fix run withdrawn, two valid
144-cell runs on deliberately identical designs, both validity gates
passing on both, zero of nine features graduating either time, and the
recommendation that the generator arsenal stays default-off. The design
sections are re-scoped to what the measurements support — the endpoint is
per-duel challenger improvement aggregated to cell means with the CELL as
the unit of analysis (duels within a cell share an evolving champion;
treating them as independent understated every standard error ~2x and
flipped a validity gate), the resolution floor is DERIVED from an in-batch
A/A contrast rather than asserted, both controls (A/A duplicate and
planted-defect arm) are mandatory members of the same batch with the
planted-defect check additionally a cheap pre-gate, and sizing is stated in
cells rather than duels because between-cell variance dominates within-cell
by roughly 4x. The K=6 screen premise is withdrawn; the two-sample MDE
arithmetic the live MDE ladder pins is retained unchanged.

### The holdout confirmation gets its own bounds (issue #118)

`promote_margin` served as both the Rule 1 train threshold and the
holdout confirmation's scalar tolerance. The two are calibrated against
different slice sizes and pull the knob in opposite directions — a slice
of N entries moves its scalar in `1/N` steps and the holdout is the
smaller slice — so on the default-produced 12-train / 6-holdout split
with one holdout entry flipping, NO margin value promotes. Two additive
`ScoringWeights` fields split the bounds:
`holdout_margin` (`None` ⇒ fall back to `promote_margin`) and
`holdout_entry_regression_budget` (`0` ⇒ today's zero-tolerance
pass-rate rule; N tolerates N regressed holdout entries under either
monotonicity scope). Both default-inert and both omitted from the
contract canonical form at their default, so no existing epoch's contract
hash moves. Both are scoped to the confirmation: the Ladder's release
threshold still reads `promote_margin`, because what it gates is a
*train*-measured improvement and a withheld query leaves the train
promote standing unconfirmed. `zicato board preflight` now prints a
holdout-feasibility note when either bound looks infeasible.

### The pre-flight's margin window stops overclaiming (issue #119)

The pre-flight measured `|degraded_scalar - champion_mean|` — DEGRADATION
headroom, how far the scalar moves when a mutation point is destroyed —
and enforced it as the achievable IMPROVEMENT a challenger has to clear.
The two are unrelated in general, so the guard failed in both directions:
a false refuse for a champion anchored near the failing end (little left
to lose, plenty to gain) and a silent false OK at the score ceiling. The
measurement is kept and now persisted as `degradation_signal` (the legacy
`signal` key is retained for existing readers); `margin_above_achievable`
is a WARNING that can no longer hard-refuse a run even under
`preflight_gate="refuse"`, including from records persisted before the
change. The floor-based refusals, which measure honestly, are unchanged.
Improvement headroom remains UNMEASURED — deriving one from the namespace
weights is registered, not built, because the scalar's reachable floor is
not `0` once a namespace carries a negative weight.

### The parsimony term charges the edit, not the file (issue #120)

`diff_size` counted every line of a patch's `new_content` as `added` and
hard-coded `removed = 0`. For a `kind="file"` mutation point the
replacement is the WHOLE FILE, so every proposal paid for the template it
was required to preserve — a byte-identical re-emit of a 37-line file
scored complexity 38, a `0.76` toll at `diff_complexity_weight = 0.02`,
76x the contract's `promote_margin`. The measure now takes the
parent-side CONTENT of each patched mutation point and reports a real
line diff, so a patch that changed nothing counts for nothing, including
its per-patch constant. The orchestrator threads content it already holds
from the parent-snapshot enumeration; the scoring layer stays pure and
takes text, never paths, and absent parent content the historical count
applies per patch unchanged. The exact path is capped at 1000 lines per
side with difflib's `autojunk` heuristic re-enabled above it — the exact
measure is unbounded in running time on a repeated line (measured: 157 s
at 4000 lines), and this runs once per round inside scoring with no
timeout above it. The paired half: a Rule 1 rejection whose toll moved
against the challenger now states the split — raw quality delta versus
parsimony toll, whether the raw delta alone would have cleared the
margin, and the per-side evidence — so a challenger that improved the
board and paid a larger toll is no longer worded identically to one that
regressed. Inert at the default weight. BREAKING for calibration: a
weight or ceiling tuned against the old file-charging numbers is now far
too loose.

### A judge that raised no longer reads as one that found nothing (issue #121)

A board-declared process judge whose callable raises was byte-identical,
in every persisted artifact, to one that ran and found no violation — the
inline judge catches everything and returns a bare `JudgeVerdict()`,
goldfive emits no event for an empty verdict, the reducer writes no drift
count, and `detect_dead_judge` reported the broken judge with the same
words it uses for a healthy judge whose criterion was never met. That
routed the operator into a board audit while the missing drift made the
generation's scalar better than the evidence supports. Per-judge error
provenance now rides the pipeline end to end: both catches return an
errored verdict carrying `errored`/`error`, a process-wide per-judge
invocation/error register sits at zicato's judge boundary, the inline
judge's io_sink records the FAILED call, and the worker stamps an
additive `LossProfile.judge_errors` (empty default — every existing
`loss.json` loads unchanged; the replicate fold SUMS rather than means,
because a mean would report "8.5 errors" for a duel). `detect_dead_judge`
splits the silence accordingly: a judge with errors raises a distinct
`judge_erroring` WARNING naming the counts, the last error type and the
config to check, while `dead_judge` now says call failures were ruled
out. The reflection and reliability surfaces read the flag too — a failed
call is filtered out of adjudication, the scorecards and replicate
disagreement rather than folded in as a silent verdict, so an
intermittently-broken endpoint stops reading as a stable judge, and
`zicato board judges --test-retest` counts failures and warns instead of
reporting a judge that raised on every call as fired 0/k at 0%
disagreement. No abort knob and no goldfive change: a 100%-error round is
indistinguishable from a transient outage, and an outage never
disqualifies a contract.

### Re-measured evidence is archived, not destroyed (issue #122)

Every artifact describing one evaluation is keyed by `(epoch, generation,
entry)` with no round dimension, and the champion defends across many
rounds under one generation id — so `--mode full`, whose stated purpose
is re-sampling for noise, overwrote the sample it would be compared
against. Fixed by archive-on-overwrite, leaving the canonical paths and
the unit-cache keys UNCHANGED (re-keying the cache would turn every
champion lookup into a miss and break its at-most-once discipline):
`gen_score.history.jsonl` appends the full payload with its round and a
monotonic seq before each overwrite, `loss.archive.jsonl` archives the
OUTGOING profile when the slot is already occupied, and
`events.prev.jsonl` keeps exactly one predecessor so the raw layer stays
bounded. The loss archive lives in the WORKER, beside the write that
truncates the canonical `loss.json` — the parent process re-persisting
the identical profile arrived after the displaced measurement was already
gone, and appended a copy of the current one instead, so a unit measured
once read back as two. New readers expose the history
(`read_gen_score_history`, `read_events_history`,
`read_unit_loss_history`). GC needs no change and `gc.py` now says why.

### The journal keeps the proposer's full reasoning (issue #123)

`journal.md` truncated at WRITE time — `why` to its first sentence,
`core_idea` to its first physical line (measured: 7% of a 330-character
`why` surviving). The file is append-only and is the only channel through
which the proposer reads its own prior reasoning back, so the loss was
permanent on disk. Both fields are now recorded in full; the heading
still takes only the first line of `core_idea` so it stays a legible
markdown heading, and a single-line value renders inline exactly as
before. A multi-line value is fenced with a blank line on BOTH sides, so
the following field can no longer fold into its paragraph. The budget
moved to the readers, where dropping text is recoverable:
`proposer.tools.read_journal` — the one uncapped LLM-facing journal
reader — gains a 20,000-character TAIL-biased cap that keeps the NEWEST
entries, cuts on an entry boundary, and prepends a note saying what was
dropped. That boundary is anchored on the heading shape the writer emits
rather than a bare `## `, so an ordinary `## Approach` heading inside a
recorded `why` cannot open the window mid-body and hand the next proposer
a fragment of someone's reasoning dressed as run history. Entries written
before this change stay truncated on disk.

### lineage.json records WHY a generation was rejected (issue #124)

`lineage.json` recorded THAT a generation was rejected and never why,
though the orchestrator computes the reason and both scalars in the same
function that appends the node. `append_to_lineage` now takes keyword-only
`rejection_reason` / `parent_scalar` / `child_scalar` and persists them
plus a derived `delta_scalar` (no new fields on the `Generation`
dataclass), threaded from the gauntlet tail and the multi-challenger
settle loop. Two invariants are enforced at the writer so the tri-state
`promoted` (True/False/None) cannot be misread: a reason lands only when
`promoted` is False — a caller passing one for a promoted or pending node
gets `""` — and absent scalars are null, never `0.0`. The first invariant
is held on the RECORD rather than only at the write: the upsert rewrites
`promoted` unconditionally while a reason was once-set and sticky, so a
node that settled rejected and was later re-recorded promoted kept a
reason its own flag contradicted; the reason is now cleared whenever
`promoted` moves off False. `build_lineage_view` passes all four through
to `/api/lineage`, present-only — no UI renders them yet. Also fixes a
seeding bug found in triage: `zicato init` wrote `lineage.json` as
`{"nodes": [], "edges": []}`, which the loader rejects as malformed; it
now seeds `{"epochs": []}`.

### `evolve` reports its effective parallelism at run start (issue #126)

The parallelism default of 4 bounded every run and appeared in no log
line, so a capped run on a large host was indistinguishable from slow
work. `evolve` now logs the effective parallelism, `propose_parallelism`,
the host worker-permit resolution (`AUTO` ⇒ N) and the usable CPU count
once at run start, naming the tier each was resolved from. The
host-aware default itself is deliberately unchanged.

### A failed lazy import stops blaming a missing symbol (issue #127)

Five loaders conflated an `AttributeError` raised DURING lazy symbol
construction — a PEP-562 module `__getattr__`, or a property — with the
symbol being genuinely absent, and `import_path` re-raised `from None`,
destroying the traceback. An operator debugging a renamed attribute in a
dependency was pointed at their own `agent.py`.
`zicato.import_path.explain_attribute_error` now makes the distinction
once, in the shared home of symbol resolution: it returns `None` for a
genuine absence, so each caller keeps its established wording, and an
explanation when the access itself raised. CPython stamps `.name`/`.obj`
on every `AttributeError` escaping an attribute access, so a differing
name or object proves construction raised; when both match the requested
symbol, a message that is not the one the attribute machinery writes is a
`__getattr__` saying something of its own, and is passed through rather
than overwritten. All five sites route through it, and `import_path` now
chains `from exc`.

### Issue #128 — closed as not reproducible

No `reconcile` parameter exists on the storage protocol or either backend,
nothing documents one, neither store has a `NotImplementedError` path, and
`git log -S"reconcile="` is empty over all history. Invariant guards (not
xfail pins — there is nothing to fix) hold the property the report would
have violated: no parameter is accepted or described unless it works.

### Diagnostics carry their evidence to the reader (issue #129)

Two failures with one shape — the harness knows something and does not
say it. First, a zero-promotion epoch read as a healthy one across eight
surfaces, each degrading to a reassuring value rather than an absent one,
which is the worst direction for the regime an operator most needs to
see. The verdict ladder's stall words now key on a new `settled_count`
(challengers a tournament has actually DECIDED) beside `challenger_count`,
which keeps its published meaning: a pending generation is recorded the
moment its snapshot lands, so the old promotion-count guard could never
be false while a challenger was racing, and a fresh run's very first
round resolved to "stalled (no promotions)" mid-tournament. Settlement is
read as the union of the two tables that record a decision, since a
`tournaments` row is written only at settle time. Second, the evidence
itself: `_summarise_loop_health` names the finding code and appends the
detector's `recommendation` — which the string-only text walker could
never reach, leaving fifteen detectors' remediations only in the round's
health JSON; the two evolve stop messages quote what ended the run;
`detect_stalled_loop` breaks its streak down by gate reason;
`detect_placebo_promoted` cites the delta against `promote_margin` and
the measured noise floor; a deferred reason quotes the fitted `P(...)`;
the namespace-monotonicity reason cites each regressed namespace's
champion and challenger aggregates; the reflect report renders each
practice check's evidence dict; the all-failed field reason folds in the
per-slot breakdown; and the four bare strategy reasons name the missing
precondition and cite the scalars the crowning duel would have compared.
A dev-guide subsection codifies the five-slot evidence convention
(population / measured / compared-against / remedy / remedy safety) as a
render conformance rule — the collection layer was never the defect, so a
third well-shaped field would have reproduced the bug rather than fixed
it. Mermaid edge labels rise from 30 to 60 characters and the bracket
family is escaped alongside the existing entity table: edge labels are
emitted bare between pipes, so one unescaped `(` — and every Rule 1
reason carries its measured pair in parentheses — failed the parse for
the whole graph block, a hazard the old clip kept latent. The stalled-loop
breakdown buckets on the earliest separator that opens a detail clause
rather than the first colon, so a colonless reason no longer clips inside
its own numbers and produces six singleton buckets for six identical
rejections. The lineage figure's canvas takes the max of the requested
and wrapped heights, so it is monotone in generation count.

### Per-entry regressions inside a promoted duel are named (issue #130)

Per-entry `drift_loss` was invisible to every gate rule — Rule 1 decides
on the scalar, Rule 2 on per-entry score and pass/fail, Rule 3 on
per-namespace MEANS — so an entry whose drift collapses 6x while it still
PASSES promoted with an empty reason and was baked into the champion
lineage. `evaluate_gate` now computes attributable per-entry regressions
from rows the aggregates already carry: the outcome axis, plus a drift
axis banded on both a ratio and an absolute limb (`child > max(2 *
parent, parent + 0.05)`; near zero the ratio alone is meaningless). They
travel as an additive `GateOutcome.attributable_regressions` on BOTH
decisions and additively on the `gate_evaluated` round-log event, emitted
only when non-empty. `reason` is untouched — the empty-reason-on-promote
invariant is load-bearing for consumers that read a non-empty reason as a
rejection. A promoted duel carrying regressions raises a new
`attributable_entry_regression` WARNING naming the entries and their
movement. Deliberately no blocking mode and no contract knob: per-entry
evidence is single-sample noisy, and a veto built on it would reject real
winners at an unmeasured rate, so the finding accumulates the evidence
first.

### Contract hash no longer depends on the process cwd or checkout path

`_canon_mutable_trees` previously RESOLVED the registered mutable-tree
paths against the process cwd, folding the absolute checkout path into
the contract hash — the same workspace hashed differently when `evolve`
ran from a different directory, or after the workspace moved, and
spuriously rolled its epoch. Paths are now normalized (never
filesystem-resolved). BREAKING: every existing epoch's contract hash
moves once; the workspace auto-rolls on the next `evolve` (the standard
contract-change behavior). The parity CONTRACT-HASH golden is
re-captured and is now checkout-independent.

All notable changes to zicato are recorded here. Format roughly follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### ⚠️ BREAKING DEFAULTS — noise-aware decision procedure (epochs auto-roll)

**Existing workspaces whose contracts do not pin these knobs will AUTO-ROLL
their epoch on the next `evolve`** — the flipped defaults change the
contract's canonical form, so the contract hash changes and a fresh epoch
opens (generations across epochs are not directly comparable; this is the
intended contract-roll semantics, not a migration bug). Pin the old values
in `scoring.json` to keep an epoch's contract byte-stable
(`"tournament": {"params": {"replicates": 1}}`,
`"proposer_quality": {"best_of_n": 1}`,
`"overfitting": {"min_board_size_for_split": 8}` — see
`examples/zicato_examples/target_0_convergence/scoring.json` for the
canonical pinned deterministic contract).

- **Per-duel `replicates` default 1 → 2** for gauntlet / single-elim /
  double-elim / swiss (racing keeps 1 — its replication is intrinsic to the
  escalating board slices). The gauntlet's full-A/B path (`run_tournament`)
  now honors the structure's resolved replicates: two paired board runs,
  per-replicate cache slots, per-entry losses averaged before the gate. A
  duel decided by one paired run is decided by one noise draw; two averaged
  runs is the cheapest hedge.
- **Proposer `best_of_n` default 1 → 3** with the self-critique pass:
  each propose-step samples a slate of three (each slot steered by a
  distinct edit-class hint) and selects the best. Pin `best_of_n: 1` for
  scripted / deterministic proposers.
- **`overfitting.min_board_size_for_split` default 8 → 6**: boards of 6–7
  entries now derive a holdout split.
- **The Bradley–Terry evidence gate stays OPT-IN** (`promote_confidence_threshold`
  absent ⇒ off) — deliberately. Measured (Tier-2 power harness): the gate
  blocks **100% of A/A false promotes**, but on a two-contestant crowning
  pair its CIs separate only after an **unbroken win streak of ~37 duels**
  (mixed records never separate) — so a silent on-by-default gate with a
  small budget would freeze every true promotion at `inconclusive`, and a
  converging budget costs ~32×2×board fresh runs per crowning. The gate is
  a **soundness** device; decision **power** is bought with per-duel
  replication and a margin calibrated above the measured noise floor. When
  enabled it now reaches BOTH selection shapes: the multi-challenger driver
  AND the gauntlet crowning duel (previously unreachable under the default
  structure).
- **Scaffolds enable the gate explicitly**: `zicato init` writes the full
  effective recommended contract (racing field 4 / eta 2 / board_fraction
  0.4, replicates 2, `promote_confidence_threshold: 0.8`,
  `promote_confidence_replicates: 32`) to the operator's live
  `scoring.json` (only when absent), and the tournament builder's blank
  draft opens on the same contract — the operator sees and prices the
  gate rather than inheriting a silent freeze or silent noise.
- **A/A noise-floor calibration is the default soundness surface**:
  `zicato board audit` (and the opt-in `calibrate_noise_floor` workspace
  knob) duels the champion against itself, persists the measured
  `delta_scalar` spread on the epoch record, and `evolve` WARNS loudly —
  with a `margin_below_noise_floor` health finding — when `promote_margin`
  sits inside the measured noise while the evidence gate is off (measured:
  a naive margin below the floor promotes pure noise — 20/60 A/A promotes
  in the Tier-2 harness).

### Adapters — optional post-promotion hook (`on_promote`)
- **`HarnessAdapter` gains an optional `on_promote` coroutine**, fired
  exactly once per settled promotion from both promote seams (the
  gauntlet's champion-marker advance and the multi-challenger inline
  crowning), immediately after the promotion becomes durable. It exists
  for targets whose evolved state lives outside the mutable tree — a
  database row, a served artifact, a remote config — which previously had
  to poll `lineage.json` from outside the loop to learn the promoted head.
  The hook receives the epoch, the promoted and parent generation ids, the
  promoted snapshot root, and the workspace root.
- **Optional means optional.** Every adapter predating the hook remains a
  `HarnessAdapter`: the runtime `isinstance` gate is keyed on the three
  required methods, and an adapter that declares no hook is never called.
- **Best-effort by contract.** A hook that raises, or that runs past the
  120s ceiling, never un-promotes the generation and never fails the
  round; it logs at ERROR and raises an `on_promote_hook_failed` WARNING
  in the round's loop-health report for the operator to reconcile.
  Contract-declared shell commands were deliberately not adopted as an
  alternative carrier — see ARCHITECTURE.md §4.1.1.

### Dashboard — decision-centric console (Variant T)
- **Cross-epoch meta-loop ledger** on the home view: the fleet of epochs
  and the champion lineage across them now lead the environment overview,
  replacing the full-width cross-epoch sparkline. Each contract change and
  champion-reign tick is anchored to the real generation position.
- **Settings is a routed right-side drawer overlay**, not a full page. Its
  **Contract** tab reuses the tournament builder's live preview, so the
  contract a run will use is previewed in place. Settings also carries a
  launcher into the standalone builder.
- The **tournament builder is its own first-class view** (`#/builder`),
  reachable from the Settings launcher and from the `zicato builder` CLI
  command (which boots the dashboard focused on the builder deep-link).
- **Live racing hero redesigned**: a metadata baseline strip, a full-width
  scalar track with a rung stepper, dense champion-gate rows, and a
  two-column `WHAT'S RUNNING | LIVE ACTIVITY` layout. Per-round racing
  models and the epoch/candidate views now build through one shared
  structure resolver, so non-gauntlet views render identically.
- **S/M/L font-size control** in the typeface picker (recalibrated ladder).
- Non-racing live-hero figures go responsive / full-width.
- **Per-entry views surface a continuous outcome score** plus
  precision / recall, alongside the existing pass/fail.

### Dashboard — static-asset revalidation
- Static dashboard assets are served with an `ETag` derived from the
  file's identity and honor `If-None-Match`: an unchanged file revalidates
  to a bodyless `304` (no re-download), and editing a file changes its
  `ETag` so the browser always picks up CSS/JS edits.

### Proposer
- The **tool-using ADK agent is now the DEFAULT proposer** (in
  `builtin_default` mode, bound to the auxiliary model and the read-only
  proposer tool registry). The skill-composed proposer is now the explicit
  opt-in (a configured `proposers/<name>/` dir). The proposer is contract
  input #5: configuring a proposer dir — or editing one of its skills —
  rolls the epoch.
- A **board-anonymized, train-slice-only failure-mode feedback channel**
  to the proposer via the `outcome_summarizer_spec` operator hook: the
  proposer is fed marginal failure signal computed over the train slice
  only, with board contents anonymized, so it can target failure modes
  without memorizing the board.

### Tournament / runtime
- **Configurable racing wall-clock budgets**: the racing strategy reads
  `matchup_budget_seconds` (caps every duel's total board-unit wall-clock)
  and `final_rung_budget_seconds` (overrides it for the final rung;
  defaults to the matchup budget). Both are opt-in — unset leaves a rung
  uncapped.
- **Cost estimator uses per-structure default replicates**: when
  `params["replicates"]` is unset, the builder's cost meter reads each
  strategy's own `_default_replicates` (swiss / single-elim / double-elim
  = 2; gauntlet / racing = 1) from the selection layer's single source of
  truth, instead of assuming a flat 1, so the meter cannot under-report
  the schedule a structure actually runs.

### Gate / scoring
- **Selectable pass-rate monotonicity scope.** `ScoringWeights`
  `pass_rate_monotonicity_scope` selects `per_entry` (default — reject
  when any entry the parent passed regressed) vs `aggregate` (reject only
  when the child's overall pass-rate falls below the parent's beyond
  tolerance). The on/off switch remains the separate
  `pass_rate_monotonicity` bool. The scope is authored through the builder
  gate-config op (`set_gate`), surfaced in the analyzer report and the
  dashboard breakdown, and now carried through the subprocess-worker
  transport so an operator's `aggregate` choice can never silently
  downgrade to `per_entry` inside a worker.
- **Continuous per-entry outcome scores.** An expectation may return a
  float in `[0, 1]` recorded as a per-entry score plus precision / recall
  metrics, surfaced per entry in the dashboard.

### Correctness
- `per_judge_weights` now survives the subprocess-worker transport
  (symmetric with the other scoring fields), so a worker scores with the
  operator's configured per-judge weights instead of resetting to the
  default.
- In-run process judges now grade the **real tool-call ledger** rather
  than the agent's narration (the `file_findability` judge was reading the
  agent's prose, not the actual tool calls), so a judgement reflects what
  the harness actually did, not what it claimed.

### Storage
- Settled the storage design end-to-end and rewrote `docs/design/STORAGE.md`
  around it. The five data kinds get five fits: runtime state and the
  lineage/experiment/journal records → files (one record per file,
  through `StorageBackend`); telemetry → JSONL (goldfive's format,
  unchanged); generation source trees → a pluggable `GenerationStore`
  (directory snapshots today, git on the roadmap); the cross-run
  analytical index → a real DB (`index.db`, SQLite — DuckDB evaluated
  and deferred until scan latency is felt). The store of record stays
  files-canonical so concurrent subprocess runs stay lock-free and
  crash-isolated.
- Resolved the record-level vs generation-level seam fork: `StorageBackend`
  stays record-level (key→blob); generation source trees get a separate
  peer seam, `GenerationStore`, at the `epoch/` domain layer — not an
  extension of `StorageBackend`. Forcing source-tree transactions into
  the record ABC would make it carry domain vocabulary and stop being an
  honest storage seam.
- `zicato.epoch.genstore` — the `GenerationStore` protocol plus the
  shipped `DirectoryGenerationStore` (the existing directory-snapshot
  mechanism, byte-for-byte). `derive_generation` is the generation-level
  transaction boundary the record seam could not express: copy the
  parent tree, apply the patch set all-or-nothing, return the child
  snapshot root. The orchestrator's snapshot / baseline-seed / patch-apply
  paths now route through this seam — the single site a git backend
  would later be substituted.
- `epoch/` record I/O migrated onto `StorageBackend` (via the new
  `zicato.epoch._storage` adapter, mirroring `runtime/._storage`).
  `experiment.json`, the per-patch files, `lineage.json`, per-epoch
  `config.json` / `scoring.json`, and `journal.md` are now written with
  the same `.tmp` + `fsync` + rename atomicity the runtime layer already
  had — a crash mid-write can no longer leave a truncated record. No
  on-disk bytes moved; every `epoch/` signature is unchanged.
- Continuous indexing promoted from an add-on to the stated design: the
  orchestrator's live dual-write keeps `index.db` current as the loop
  runs; `zicato reindex` is the batch rebuild / repair path. The
  canonical-file-first ordering rule is documented as load-bearing.

### Robustness
- `zicato evolve --max-wall-clock-seconds <S>` — a total wall-clock
  budget for the whole evolve invocation. Previously only individual
  board entries had a `wall_clock_budget_seconds`; an N-entry × M-round
  invocation had no aggregate ceiling. The budget is enforced between
  rounds (the loop stops cleanly once it is spent) and within a round
  (a round that would overrun it is cancelled via `asyncio.wait_for`
  and recorded as an aborted round). It applies on top of — not
  instead of — each entry's per-entry budget. Also reads the
  `ZICATO_MAX_WALL_CLOCK_SECONDS` environment variable. When the loop
  stops on this budget, the `evolve` summary says so explicitly.

### Dashboard
- The Files view gained a **mutation-site browser**. Below the
  generation file tree and patch set, an operator can now browse the
  mutation surface — every `# zicato:mutable` annotated span — and, for
  each site, see a line-level diff of the `v0` baseline content against
  the patched content in any generation whose patch touched that
  mutation id. Sites no patch has touched just show their baseline
  content.
- New endpoints `GET /api/mutations/{epoch}` (the epoch's mutation
  surface, each site with file, role and the generations that patched
  it) and `GET /api/mutations/{epoch}/{mutation_id}` (one site's
  baseline content plus the per-generation patched content for a diff).
  Both are deterministic and backend-neutral: they re-enumerate each
  generation's materialised snapshot through `enumerate_mutations` and
  route patch-set reads through the `GenerationStore` seam, so the
  directory and git backends render identically. No re-apply — a
  generation's snapshot already IS its post-apply state.

## [0.3.0] — 2026-05-15

Observability + analytics release. zicato grows a real dashboard, a
SQLite analytical index, a tournament/competition view distinct from
goldfive's execution view, and loop-health diagnostics. First real run
against a live model landed in this cycle.

### Live dashboard + supervisor
- `zicato-supervisor` Rust binary: watchdog (state-file monitoring +
  SIGTERM→SIGKILL escalation) + HTTP/SSE dashboard server, auto-spawned
  by `zicato evolve`.
- Multi-view dashboard (vanilla HTML/CSS/JS, bundled in the binary):
  **Overview**, **Tree** (cross-epoch lineage graph), **Tournament**
  (the bracket), **Epoch** (the contract). Fragment-routed; SSE-live.
- `/api/epoch` — the epoch's full evaluation contract (board, rubric,
  scoring, registered harness, mutation surface).
- `/api/tournaments` + `/api/tournaments/:id` — the gauntlet bracket and
  full per-matchup detail. `/api/health-report` — loop-health findings.
- Static assets served at document root (fixes the relative-path 404
  that rendered the dashboard unstyled).

### SQLite analytical index
- `.zicato/index.db` — a derived, queryable index of cross-run data
  (epochs / generations / experiments / patches / runs / loss_profiles /
  metric_counts / tournaments). Files stay canonical; the index is
  rebuildable via `zicato reindex` and dual-written live by the
  orchestrator. The Rust supervisor reads it via rusqlite.
- Generation source trees are NOT in SQLite (git, per the roadmap);
  per-run event capture stays JSONL. SQLite is the analytical layer only.

### Tournament view — competition, not execution
- The dashboard Tournament view is a king-of-the-hill **bracket**: a
  champion lineage (winners spine) + discarded challengers, each matchup
  parent-vs-child over the board.
- Per-matchup detail: hypothesis, patches, per-entry A/B grid
  (improved/regressed/flat), scalar breakdown, gate verdict + reasoning.
- `zicato/tournament/detail.py` analytics: bracket assembly, A/B grid,
  hypothesis ledger (proposer calibration with explicit sign+magnitude
  match semantics), optimization trajectory + plateau detection,
  mutation heat map (win-correlation), tournament cost.
- Architectural split: harmonograf renders the *execution view* (the
  temporal trace of one run); the zicato dashboard owns the *competition
  view*. A per-run drill-down links the two.

### Incremental board scoring
- The tournament runner now scores each board unit the instant its
  run(s) settle — on the same concurrency fan-out as the runs, not
  batched after every board finishes. As each unit completes its
  per-entry losses fold into a running partial aggregate
  (`_IncrementalScorer`) which is rewritten onto the live
  `ActiveTournament` (`partial_parent_agg` / `partial_child_agg`).
- A finished board's score is therefore available ASAP — while the
  sibling boards are still running — rather than only at round end.
  The dashboard's Partial aggregate panel now consumes the
  server-computed running aggregate (with a legacy fallback), so the
  scalar climbs as the tournament runs instead of sitting at 0.00.

### Integration note
- This release folds together six dashboard / runner / proposer fixes
  developed in parallel: the route-driven Files view with a
  side-by-side changed-files diff; the Overview rebuilt as the
  environment home (with the queued-label fix); the Epoch view's
  narrative redesign; the Tournament view's harmonograf jump-off links
  (`adk_session_id`); incremental per-board scoring; and proposer/patch
  resilience (post-apply validation retry, full proposer content,
  surgical span apply). The partial-aggregate panel now lives in the
  Tournament view's hall — the Overview no longer renders the full
  board.

### Loop-health diagnostics
- `zicato/health/` — detects a toothless evaluation loop: degenerate
  scoring (consecutive zero-Δ tournaments), non-differentiating board
  entries, flat drift signal, missing expectations, stalled loop.
- `zicato health` CLI; the orchestrator writes a per-round health
  report, logs a loud warning on a critical finding, and stops the loop
  after sustained degeneracy (`evolve --stop-on-degenerate`).
- Motivated by the first real run — v0 and v1 scored identically
  (1.000000); the loop produced zero optimization signal and that was
  only discoverable by manual inspection.

### Runtime + telemetry
- Orchestrator writes a populated heartbeat (epoch / generation / round
  / phase); per-run progress is bumped on each goldfive event so
  dashboard run cards animate.
- `mutations.json` dumped per epoch so the dashboard can show the
  mutation surface.
- `HarmonografSink` attached when `ZICATO_HARMONOGRAF_URL` is set —
  runs stream live to a harmonograf server; the dashboard deep-links
  each run to its harmonograf session.
- Progressive `analysis.html` — regenerated after every generation.

### Harmonograf companion
- [pedapudi/harmonograf#292](https://github.com/pedapudi/harmonograf/pull/292)
  — a `harmonograf-replay` command ingests a zicato run's `events.jsonl`
  from disk, so any finished run is viewable in harmonograf.

### Design docs
- New: `TOURNAMENT.md`, `ANALYTICAL-INDEX.md`, `LOOP-HEALTH.md`,
  `RUNTIME.md`, `DASHBOARD.md`, `ROBUSTNESS.md`, `STORAGE.md`.
- Updated `ARCHITECTURE.md` / `CLI.md` / `EPOCHS-AND-JOURNALING.md`.

### Tests
- ~961 Python tests + the Rust supervisor suite (48 unit + 19
  integration). The 5 pre-existing environment-dependent failures
  (goldfive editable-install drift) are unchanged.

## [0.2.0] — 2026-05-15

Second alpha. Major surface expansion: drift-free objectives are
first-class, the board API has a friendly Python builder, multi-objective
scoring lands, regression-suite gating protects against breaking-change
patches, and zicato now consumes goldfive's decision telemetry through a
dedicated LLM analyzer.

### Drift-free metrics
- `MetricCount` replaces drift-only counts. Arbitrary namespaces:
  `drift:*`, `cost:*`, `latency:*`, `rubric:*`, `output:*`, `schema:*`.
- `LossProfile.metric_counts` carries the unified view alongside
  `drift_counts` for back-compat. New first-class fields:
  `tokens_spent`, `output_chars`, `schema_failures`.
- `HypothesisSpec.expected_metric_movements` accepted alongside
  `expected_drift_movements`. Proposer schema validates either form.
- `OutcomeRecord.metric_movements` records actual deltas per namespace.
- New detectors: `detect_metric_frequency(namespace=...)`,
  `detect_cost_outliers`, `detect_rubric_score_movement`. The original
  `detect_drift_kind_frequency` is now a thin wrapper.
- Analysis renderer: `render_metric_movement_table(namespace_filter=...)`
  with a "drift" filter producing byte-identical legacy output.

### Multi-objective scoring
- `ScoringWeights.namespace_weights` — per-namespace weights with a
  sign convention (positive = higher-is-worse; negative = higher-is-better;
  zero = excluded from scalar).
- `ScoringWeights.namespace_monotonicity` — per-namespace monotonicity
  flags. Regression on a tracked namespace hard-rejects the candidate
  regardless of overall scalar improvement.
- `aggregate_namespaced_metrics` + `scalar_components` in the
  generation aggregate.
- `evaluate_gate` adds rule 3: per-namespace monotonicity check.

### Regression-suite gate
- `zicato.tournament.regression.run_regression_suite` — asyncio
  subprocess pytest invoker with timeout + failed-id parsing.
- `ScoringWeights.regression_gate_enabled` / `regression_test_command`
  / `regression_timeout_s`. When enabled, any test failure in the
  candidate snapshot hard-rejects regardless of other metrics.
- `tournament --skip-regression` CLI flag.

### Friendlier board API
- `Board` + `Entry` programmatic builder. `Entry(id=..., input=...,
  evaluate=..., budget_s=...)` auto-detects the kind from arguments
  (`turns` → multi-turn-scripted, `persona` → multi-turn-emulated,
  `adversarial_agent_spec` → synthetic-adversarial, etc.).
- `Predicate` factory family: `Predicate.contains` / `.regex` /
  `.schema` / `.python`.
- `Rubric.judge(rubric_text, *, threshold, scale)` — built-in
  LLM-as-judge matcher. Operator supplies the rubric text only; no
  separate Python module needed.
- New expectation kind `"rubric"` with `evaluate_rubric_judge` runtime.
- `budget_s` JSONL field as a back-compat alias for
  `wall_clock_budget_seconds` (preferred on write, accepted on read).

### Decision-telemetry analyzer
- New `zicato.analyzer` module: `DecisionEventSummary` +
  `aggregate_decision_events` parse goldfive's new decision events
  (`SteeringDecisionMade`, `LadderTransitionDecided`,
  `DetectorDispatchOrdered`, `PolicyApplied`, `RetryBudgetSpent`).
- `analyze_epoch_telemetry` calls the auxiliary LLM with a structured
  prompt and writes
  `epochs/{epoch}/insights/round_{N}.md` with sections:
  Headline observations, Suspected over-intervention, Suspected
  under-intervention, Suggested next mutations.
- Orchestrator invokes the analyzer best-effort after every round;
  proposer embeds the latest insights into its user prompt so the next
  proposal is informed by the cross-run patterns.
- CLI: `zicato analyze-telemetry [--workspace ...] [--epoch ...] [--round N]`.

### Goldfive companion PRs (separate repo, awaiting merge)
- [pedapudi/goldfive#440](https://github.com/pedapudi/goldfive/pull/440)
  — manifest expansion from 31 → 60 entries; new proto events
  `LadderTransitionDecided`, `DetectorDispatchOrdered`, `PolicyApplied`,
  `RetryBudgetSpent` at tags 40-43.
- [pedapudi/goldfive#439](https://github.com/pedapudi/goldfive/pull/439)
  — pluggable `Judge` protocol with `JudgeContext` / `JudgeVerdict`;
  `goldfive.wrap(judges=[...])`; built-in detectors refactored as Judge
  instances; new `JudgementEmitted` proto event at tag 44.

### Tests
- 644 → 813 passing (+169). Same 5 pre-existing environment failures
  on `test_adapter_adk` / `test_synthetic_adversarial` /
  `test_telemetry_reducer_real_goldfive` are unchanged (all
  environment-dependent on goldfive-editable-install drift).

### Compatibility
- Every existing `LossProfile`, JSONL board, `expected_drift_movements`
  hypothesis, and tournament scoring config from 0.1 continues to work.
  The drift namespace is preserved verbatim; multi-objective scoring
  uses sensible defaults when namespace weights aren't configured.

## [0.1.0] — 2026-05-15

First public alpha. End-to-end evolve loop functioning against both
intended v0 dogfood targets. The library is usable; the API will still
break in patch releases until v0.2.

### Core loop
- `zicato init` / `register` / `epoch new` / `mutations` / `propose`
  / `tournament` / `evolve` / `epoch close` CLI surface auto-discovered
  via `zicato.cli.commands.*`.
- Annotated mutation surface (`# zicato:mutable` and
  `# zicato:mutable:file`) with AST resolution, applier, validator, and
  a `mutations` audit subcommand.
- Board format (single-turn, multi-turn-scripted, multi-turn-emulated,
  synthetic-adversarial, synthetic-clean) with five expectation kinds.
- Collusion-proof multi-turn emulator with the two-callable rule, sealed
  context construction, and a post-hoc answer-leak heuristic.
- Telemetry ingest via goldfive's `JSONLPersistenceSink` per run + a
  post-run reducer that produces `LossProfile`.
- Six pattern detectors (drift-kind frequency, hot tasks/agents,
  plan-revision instability, multi-turn memory failure, multi-turn
  context loss).
- LLM proposer with structured-output `Experiment` schema (hypothesis +
  patches) and retry-on-parse-failure.
- Tournament runner: full mode + fast inline keep/discard. Promote gate
  enforces a scalar margin AND strict pass-rate monotonicity.
- Epoch lifecycle with manual close + auto-close fallback; running
  `journal.md`; analysis pass at close producing both `analysis.md`
  (LLM-driven narrative) and a self-contained `analysis.html`
  (deterministic — inline SVG lineage, score trajectory, drift heatmap,
  per-experiment cards, dark-mode aware).
- Per-patch JSON files under `generations/v{N}/patches/{patch_id}.json`
  with `patch_ids` references in `experiment.json` (atomic write order:
  patches first, then experiment).

### v1.1 robustness
- `.zicato/runtime/` state file layer (heartbeat, active_runs,
  active_tournament, control, control_log, lock).
- Workspace lock with PID-aware stale-pid stealing.
- Heartbeat beater task lifecycle wired into `evolve_n_rounds`.
- Per-call `aux_call_llm` timeouts (`ZICATO_AUX_CALL_TIMEOUT`, default
  120s) at every auxiliary call site (proposer, judge, emulator,
  analysis).
- Progressive `analysis.html` regeneration after each generation (in
  addition to the final LLM-driven `analysis.md` at epoch close).
- Tournament runner publishes `active_tournament.json` + per-run
  `active_runs/{run_id}.json` for the live dashboard.

### Live dashboard
- Rust `zicato-supervisor` binary (single static binary, ~4.4 MB
  stripped). Auto-spawned by `zicato evolve`; URL printed to stdout.
  Opt out with `--no-dashboard`.
- Watchdog half: inotify-based file watching with SIGTERM → grace →
  SIGKILL signal escalation for stalled orchestrator + stalled runs.
- HTTP + Server-Sent Events server (default `:7892`). Endpoints:
  `/api/state`, `/api/lineage`, `/api/active-runs`,
  `/api/active-tournament`, `/api/heartbeat`, `/events`, `/api/health`,
  plus POST control endpoints (`/api/control/pause`, `…/kill/{id}`,
  `…/promote/{gen}`, `…/reject/{gen}`, `…/rubric`).
- Single-page browser UI (vanilla HTML/CSS/JS, no build step, ~77KB
  total) bundled with the binary via `include_dir!`. Includes lineage
  SVG, score trajectory, drift heatmap, **tournament status panel**
  with deterministic predicted-gate-verdict, active-runs strip, log
  tail, drill-down panels. Dark-mode aware. Action buttons present
  but disabled — v1.3 enables them via the control-file protocol.

### Examples
- `examples/target_1_presentation/` — vendored harmonograf reference
  agent with 9 mutation points + 7-entry board + rubric + scoring +
  deterministic mocks + `RUN.md`.
- `examples/target_2_goldfive_steering/` — adversarial board against
  goldfive's optimization surface (`pedapudi/goldfive` PR
  [#436](https://github.com/pedapudi/goldfive/pull/436)) with
  `LoopingAgent` / `HallucinatingAgent` / `RefusingAgent` etc., a
  `manifest_bridge` translating goldfive's optimization manifest into
  zicato MutationPoints, mocks + `RUN.md`.

### Design docs
- `ARCHITECTURE.md`, `MUTATION-SURFACE.md`, `BOARD-FORMAT.md`,
  `EPOCHS-AND-JOURNALING.md`, `TELEMETRY.md`, `SCORING.md`,
  `EMULATOR.md`, `DOGFOOD-TARGETS.md`, `CLI.md`, `RATIONALE.md`,
  `VOCABULARY.md`, `RUNTIME.md`, `DASHBOARD.md`, `ROBUSTNESS.md`,
  `STORAGE.md` — comprehensive coverage of every load-bearing decision.

### Tooling
- hatchling-based packaging; uv + pip-editable installs work.
- `pytest` + `pytest-asyncio` test suite (~640 passing).
- `ruff` + `mypy --strict` clean on the Python tree.
- `cargo build --release` + `cargo test` + `cargo clippy` clean on the
  Rust supervisor binary.
- GitHub Actions CI workflow over Python 3.11/3.12.
- `Makefile` with `install`, `test`, `lint`, `format`, `typecheck`,
  `check`, `supervisor`, `supervisor-test`, `install-supervisor`,
  `clean` targets.

### Known limitations (documented in RUN.md and the design docs)
- Mock-driven smoke runs produce byte-equal outputs across generations
  in v0; the tournament gate correctly rejects with "insufficient
  margin". Replace mocks with real LLMs for a meaningful loop.
- Multi-turn scripted/emulated drivers abort some examples with
  TypeErrors; the reducer treats those entries as zero-signal and the
  tournament continues. Cleanup is v1.0.1 work.
- Subprocess tournament workers (L3 robustness) and orchestrator
  control-file consumer (v1.3) are NOT yet implemented; see
  `docs/design/ROBUSTNESS.md` for the phasing plan.
- Git-backed generation storage (G1-G10) is documented but not yet
  implemented; v0 uses directory snapshots + per-patch JSON files.
