# Rationale

The other design documents describe **what** zicato does. This
document describes **why** each major choice was made the way it was.
It is structured as a list of decisions, each with the alternatives
considered and the reasoning for the chosen path.

This is the place to come when a design choice feels arbitrary. If a
later contributor wants to relitigate a decision, the burden is on
them to either (a) name a concern in the "why" below that no longer
applies or (b) bring a new concern that wasn't weighed.

## 1. Why annotated mutation points, not free-form source edits

**Alternative considered.** Let the proposer rewrite arbitrary files
in the inner harness's tree. Maximally flexible; the proposer can
restructure the agent if it wants to.

**Chosen.** Source files are not editable except where the operator
has placed a `# zicato:mutable` marker (span or file). The proposer
addresses patches by stable id; the applier rewrites only what an id
resolves to. See [MUTATION-SURFACE.md](MUTATION-SURFACE.md).

**Why.** A multi-agent system is high-leverage, low-reversibility
code. Letting an LLM-driven proposer rewrite arbitrary files is a
machine for generating subtle breakage. The validator can catch
syntax errors and import failures, but it cannot catch "the
researcher's prompt now subtly references a tool that exists but
behaves differently in this codebase". Pruning the search space to
operator-annotated targets shifts the optimisation problem to
"improve these strings" — which is the actual problem worth solving,
not "rewrite the agent".

The cost is reach: the proposer cannot, in v0, propose a structural
change (e.g. "split the researcher into a literature-lookup
specialist and a fact-checker specialist"). Structural changes are
properly the operator's job. The mutation surface is what the
operator owns; the proposer fills in the strings.

**Also considered.** A "one editable file" model (one mutable
program file; the proposer rewrites that file and nothing else)
works for single-file programs but does not work for multi-agent
systems where the editable surface lives in many files and the
boundaries matter. Span and file markers cover the spectrum: a
narrow marker for one string; a file marker for "this whole prompts
module is yours, rewrite as needed". See §10 for the deeper
discussion of why the single-file model was not adopted.

## 2. Why per-epoch evaluation contract (not a running average)

**Alternative considered.** Score every generation against a running
average of historical scores. Patterns aggregate forever. No epochs.

**Chosen.** Generations are grouped into epochs. Within an epoch the
board, the proposer brief's `## Forbidden` list, and the scoring
weights are frozen. Pattern aggregates reset at epoch boundaries.
Cross-epoch comparison is explicitly fuzzy. See
[EPOCHS-AND-JOURNALING.md](EPOCHS-AND-JOURNALING.md).

**Why.** Without epochs, every operator decision contaminates the
loss signal:

- Adding a new board entry shifts the score's denominator. A
  generation that beat its parent on the old board might lose on the
  new board for reasons unrelated to its patches.
- Changing scoring weights changes what "better" means. A
  generation promoted under one weight setting might have been
  rejected under a different setting.
- Adding an id to `forbidden:` shrinks the mutation surface. Patterns
  about mutation points the proposer can no longer act on are noise.

Each of these is a legitimate operator action. The system has to
accommodate them. Epochs are how: the operator's contract changes
ARE epoch boundaries, the loss signal within an epoch stays clean,
and the lineage records exactly when the contract changed.

The cost is that comparing v7 in epoch A against v3 in epoch B is
fuzzy ("they were measured against different boards"). The benefit
is that v7 vs v6 in epoch A is precise.

## 3. Why mandatory structured hypothesis up front (not just patches)

**Alternative considered.** The proposer's output is just
`list[Patch]`. Journaling captures the patches and the score delta.

**Chosen.** The proposer's output is `Experiment = hypothesis +
patches`. The hypothesis has mandatory structured fields
(`core_idea`, `modulating`, `why`, `expected_drift_movements`,
`expected_pass_rate_delta`, `risks`). Schema-invalid responses are
rejected and re-prompted. See
[EPOCHS-AND-JOURNALING.md](EPOCHS-AND-JOURNALING.md) §3.

**Why.** Without the hypothesis, the journal degenerates. A few
weeks into running a loop the operator wants to ask:

- "Why did the proposer think tightening the researcher's prompt
  would reduce CONFABULATION_RISK? Did it predict the side-effect
  on TOOL_ERROR?"
- "Have we seen this pattern before and rejected a similar
  hypothesis?"
- "Is the proposer reasoning, or just guessing?"

Bare patches don't answer any of these. The pre-run hypothesis does:
the proposer's *reasoning* and *expectations* are in the record
alongside the result. The post-run `outcome` block matches actuals
against expectations, which is exactly the signal you need to
gauge proposer quality independent of patch quality.

Schema enforcement (rejection + re-prompt on malformed responses)
keeps the journal interpretable. A free-text hypothesis would degrade
into prose that resists analysis.

The cost is one extra LLM call's worth of proposer effort per round.
The benefit is a journal that supports learning across rounds. Easy
trade.

## 4. Why collusion-proof emulator construction (and why hard-error)

**Alternative considered.** Ship a default emulator that uses the
same `call_llm` callable as the harness. Warn operators about
collusion in the docs but don't enforce.

**Chosen.** Two distinct `call_llm` callables required. The check
runs at config time and is a HARD ERROR (exit code 8). Context
construction is sealed (no `**kwargs`). The emulator sees only the
persona and the user-facing transcript. A post-hoc heuristic
detects answer leakage. See [EMULATOR.md](EMULATOR.md).

**Why.** Collusion is a silent failure mode. The naive emulator
produces plausible transcripts and plausible scores; the loop looks
healthy; only an audit of the actual transcripts reveals that the
"user" is uncannily aligned with the agent. Operators who do not
audit will never notice. Operators who do audit will lose trust in
the loop.

Warnings get ignored. The cost of refusing — one extra line of
operator setup ("supply two callables") — is tiny. The cost of a
silent collusion bug — a months-long calibration epoch whose
conclusions are degenerate — is enormous. The asymmetry forces the
hard rule.

The sealed-context construction is the other half. Even with two
callables, an emulator that sees the agent's chain-of-thought or
the expectation predicate is still degenerate. Putting the
context-builder behind a typed function whose signature is exhaustive
(no `**kwargs`) makes leakage impossible by construction — a future
contributor who wants to add a field has to update the signature,
which is a reviewable change.

This is one of the few decisions in zicato that is *both* a runtime
check AND a structural type-system guard. The redundancy is
deliberate — each half catches what the other can miss.

## 5. Why goldfive's drift taxonomy as features (not a new typology)

**Alternative considered.** Define zicato's own typed failure shapes.
"Confabulation", "delegation mismatch", "loop", etc., as zicato-side
concepts.

**Chosen.** Use goldfive's `DriftKind` enum directly. Pattern
detectors operate on the symbolic kind strings from goldfive's proto
(`"DRIFT_KIND_CONFABULATION_RISK"`, etc.). See
[TELEMETRY.md](TELEMETRY.md).

**Why.** Goldfive's taxonomy is the result of substantial prior
design work and reflects real multi-agent failure modes. Reinventing
the typology would:

- Duplicate effort with no obvious upside.
- Create a translation layer between goldfive's events and zicato's
  pattern detectors that becomes its own surface to maintain.
- Diverge from harmonograf, which renders goldfive's taxonomy
  directly — operators looking at the same run in harmonograf and in
  zicato's journal would see different vocabularies.

The taxonomy is also designed to be extensible (the `CUSTOM` kind
plus the open enum). When zicato needs to surface a pattern that
doesn't have a goldfive kind (e.g. "agent forgot a fact established
earlier", which is a cross-turn pattern, not a per-turn drift), the
right move is to compute it in the pattern detector — not to add a
new drift kind.

For target 2 (goldfive's steering layer — see
[DOGFOOD-TARGETS.md](DOGFOOD-TARGETS.md)), the loss model can't use
drift counts (circular). But the features are still goldfive's
events; what changes is how they aggregate. The taxonomy remains.

## 6. Why two `call_llm` callables, configured (not defaulted)

Adjacent to §4 but worth its own section.

**Alternative considered.** Ship a default `auxiliary_call_llm` that
chooses a vendor and model for the operator. "Just works" out of the
box.

**Chosen.** No defaults. Operators must supply both callables.
Registration fails without them. See [CLI.md §3.2](CLI.md#32-zicato-register).

**Why.** A default would either:

- Pick a specific vendor — which violates the model-agnostic stance
  and embeds an opinion zicato has no business having.
- Pick a generic vendor — which the operator probably doesn't have
  credentials for, so registration fails anyway.

Either way the operator ends up supplying the callable. The "default"
just becomes a path of stumble. Making it explicit is one extra
sentence in the getting-started docs and saves the operator from
discovering at run time that something silently picked their fallback
model.

Adjacent decision: the default emulator *prompts* (the system prompt
template) ship in zicato. The default *wiring* (the LLM) doesn't.
Prompts are inert; wiring is operational. Zicato ships the inert
defaults and refuses to fabricate the operational ones.

## 7. Why filesystem layout, not SQLite

**Alternative considered.** Embed a SQLite database for epochs,
generations, patterns, loss profiles, journal entries. Faster
queries; one canonical place.

**Chosen.** Filesystem-native. `.zicato/epochs/{epoch}/...` with one
file per artifact. JSON, JSONL, and markdown. See
[ARCHITECTURE.md §5](ARCHITECTURE.md#5-storage-layout) and
[EPOCHS-AND-JOURNALING.md §2](EPOCHS-AND-JOURNALING.md#2-storage-layout).

**Why.** The operator's first-class debugging interface for zicato is
`ls`, `cat`, `grep`, and (for the journal and analysis) `less`. A
filesystem-native layout makes every artifact directly inspectable.
SQLite makes every artifact one query away from inspectable, which is
strictly worse for a tool whose users are developers running it on
their laptops.

The cost of filesystem-native is query performance — patterns over a
hundred generations require walking many files. For v0 this is fine;
the operator's loop is "run a few rounds, look at the journal", not
"query a thousand-row pattern table". When pattern queries become a
bottleneck, the right move is to add an index sidecar (one SQLite
file used as a cache, regenerable from the filesystem), not to make
the filesystem layout the index.

A related decision: every artifact is a JSON (or JSONL) document
with stable key sorting. Git diffs on `.zicato/` are useful — an
operator can `git diff` two snapshots of their workspace and see
exactly what changed across rounds.

## 8. Why fixed per-run wall-clock budget

**Alternative considered.** No budget. Let the run go as long as it
needs.

**Chosen.** Every board entry carries `wall_clock_budget_seconds`.
Exceeded → run aborts and contributes a heavy loss term. See
[BOARD-FORMAT.md §1.2](BOARD-FORMAT.md#12-wall_clock_budget_seconds)
and [SCORING.md §2.3](SCORING.md#23-why-an-abort-is-a-heavy-constant).

**Why.** Drift counts in a 30-second run vs a 4-minute run aren't
apples-to-apples. The same generation against the same entry can
produce wildly different drift profiles depending on how long the
agent spent — a longer run has more opportunities to drift, more
turns, more LLM calls. Without a fixed budget, the loss signal is
contaminated by latency variance.

A fixed budget makes runs directly comparable. The agent gets the
same amount of wall-clock to demonstrate its quality on each entry,
regardless of patches. Patches that make the agent faster but
sloppier get the right credit (more time to recover from sloppiness
within budget); patches that make the agent slower but more careful
get the right credit (if the careful version finishes in budget,
its drift score reflects the carefulness).

This is one of the highest-value single decisions in the loop — the
shape that makes runs directly comparable in the face of latency
variance.

## 9. Why an operator-edited proposer brief per epoch

**Alternative considered.** The proposer learns from past journal
entries; no operator steering.

**Chosen.** Each epoch carries a `brief.md` the operator
hand-edits. The proposer reads it verbatim into its system prompt
each round. Read fresh every round; no caching. See
[EPOCHS-AND-JOURNALING.md §7](EPOCHS-AND-JOURNALING.md#7-the-proposer-brief).

(The proposer brief was once called the epoch "rubric". It is
renamed so that "rubric" refers unambiguously to the per-entry
`Rubric.score()` outcome check on a board entry — a distinct concept.
The naming distinction matters: the proposer brief steers the
*proposer* epoch-wide, a `Rubric` grades one *entry's output*.)

**Why.** The proposer can read patterns and produce hypotheses, but
it cannot read the operator's mind. Some things the operator knows
that the patterns don't:

- "We tried tightening the writer's prompt three epochs ago and it
  was flat. Don't try that again unless something changed."
- "The coordinator routing is delicate; the team agreed to leave it
  alone for now."
- "Prefer terse, imperative prompts to verbose explanatory ones."

These are operator-side context. Encoding them in a brief the
proposer reads gives the operator a steering wheel without writing
code. The proposer reads the brief, the proposer's hypothesis
reflects the brief, the journal records whether the proposer
followed it.

The `## Forbidden` section is mechanically enforced (V5 in
[MUTATION-SURFACE.md](MUTATION-SURFACE.md)). Everything else is
advisory — the proposer reads it as natural language. Forbidden
mechanics handle the "you must not touch this" case; advisory
prose handles the "I'd rather you focused on that" case.

Reading fresh every round (no caching) means the operator can edit
the brief between rounds and see the effect immediately. Caching
would create a stale-brief bug class; the cost of re-reading a
small markdown file is zero.

## 10. Why we did not lift the "one editable file" model

**Alternative considered.** Constrain the mutation surface to one
file (one mutable program module; the proposer can rewrite that file
and only that file). Maximally bounded search; one file to think
about.

**Chosen.** Annotated mutation points (span or file markers) across
multiple files. See [MUTATION-SURFACE.md](MUTATION-SURFACE.md).

**Why.** A multi-agent system's editable surface intrinsically lives
in many files: the coordinator's prompt is in one place, the
researcher's prompt is in another, the writer's tool descriptions
are in a third. Forcing all of them into one editable file would
either:

- Require restructuring the inner harness (every prompt has to be in
  the master file; the agent's modular structure becomes a façade
  over a single mutable blob). This is invasive and harms the
  agent's own design.
- Or require concatenating the prompts into one editable string and
  parsing it back, which is a homegrown templating system zicato has
  no business inventing.

The annotated-mutation-points design solves the same
"bound-the-search-space" problem at the right granularity. The
mutation surface is exactly what the operator marked, no more, no
less. The search space is bounded; the inner harness's modular
structure is preserved.

Other ideas adjacent to the single-file framing that DID fit into
zicato:

- **Fixed wall-clock budget per experiment.** See §8.
- **Operator-edited markdown rubric per epoch.** See §9.
- **Optional fast inline keep/discard mode.** Shipped as
  `zicato evolve --mode fast`. See [SCORING.md §7](SCORING.md#7-fast-mode-and-the-tournament).

The single-file editable-program constraint was the only one that
did not transfer cleanly to a multi-agent target; the others all
generalised.

## 11. Why drift counts ARE features even though zicato is "model-agnostic"

A pedantic question worth answering: zicato is model-agnostic, but
the drift kinds in goldfive's taxonomy are a model-specific opinion
about how multi-agent systems fail. Are we secretly model-specific?

**Answer.** No. "Model-agnostic" means zicato doesn't import a
vendor SDK and routes every LLM call through a caller-supplied
`call_llm`. "Framework-agnostic on the inner harness" means zicato
doesn't assume the inner harness is ADK, LangChain, or anything else
— it talks to the inner harness through a `HarnessAdapter`.

The drift taxonomy is **ecosystem-specific**: goldfive ships it,
zicato consumes it. That's not the same as model-specific or
vendor-specific. An adapter implementer can use any inner harness
they want; the inner harness emits goldfive events because the
adapter wraps the harness with `goldfive.wrap`. The taxonomy is
the contract between adapter and zicato, not between zicato and any
particular model.

## 12. Why no zicato-specific EventSink

**Alternative considered.** Define `ZicatoSink` that wraps
`JSONLPersistenceSink` and adds zicato-specific behaviour (e.g.
in-process accumulation, per-entry metadata stamping).

**Chosen.** Use goldfive's `JSONLPersistenceSink(mode="write")`
directly. Post-run reducer is a function, not a sink. See
[TELEMETRY.md §1](TELEMETRY.md#1-no-zicato-specific-eventsink).

**Why.** Goldfive's sink already does the right thing. A
zicato-specific wrapper would couple zicato to the goldfive
`EventSink` ABI without adding value. Reducing post-run is the right
shape — sinks must make incremental decisions about each event;
reducers have full visibility, which is what loss derivation needs.

This also has the practical benefit that zicato's loss reducer is
testable with a fixture JSONL file. No async sink setup needed; just
`reduce_run(fixture_path)`. Tests for the reducer are about a third
the size of tests for an equivalent custom-sink design would be.

## 13. Why filesystem-native AND not git-aware

**Adjacent.** The storage layout is filesystem-native; one might
expect "and we use git as the underlying versioning". We don't.

**Why.** Generation snapshots are full copies, not git refs. The
snapshot directory is self-contained; it can be `rm -rf`'d without
worrying about losing history. The journal and analysis are
markdown files an operator might track in git themselves — but
zicato does not commit, push, or rely on git.

The cost is disk usage: many full copies of the inner harness's
tree. For typical inner harnesses (a few dozen Python files plus
prompts), this is on the order of megabytes per generation. The
benefit is that snapshots are operations on a filesystem, not
operations on a repo, and zicato doesn't have to reason about merge
conflicts, branch hygiene, or remote sync.

The operator who wants their `.zicato/` tracked in git can do so
externally. The operator who doesn't want it tracked in git can
`.gitignore .zicato/`. Both work without any code in zicato.

## 14. Why no scoring "feels right" defaults

**Alternative considered.** Ship calibrated weights in
`scoring.json` so the loop works "out of the box".

**Chosen.** Ship starter weights and explicitly call them
uncalibrated. The first few epochs are calibration epochs; the
operator tunes weights based on what they observe. See
[SCORING.md §4.1](SCORING.md#41-default-weights-and-the-calibration-problem).

**Why.** "Good defaults" depend on which inner harness, which drift
kinds matter for the project, and what the operator considers a
regression. None of these are knowable at the library level.

Pretending otherwise — shipping confident-feeling weights that
happen to look reasonable for the presentation-agent dogfood — would
silently bias every project that didn't change the defaults. The
honest position is to surface the calibration problem in the docs
and to make the operator's tuning loop explicit (run an epoch,
inspect the journal, tune weights, run a new epoch). The starter
weights are a starting point, not a default.

## 15. Why no live live-tail UX in v0

**Alternative considered.** Live drift-count display as the run
progresses. Operator can watch a run in zicato's own terminal.

**Chosen.** No live-tail in v0. Harmonograf is the live view. See
[TELEMETRY.md §1.3](TELEMETRY.md#13-no-live-ux-in-v0).

**Why.** Harmonograf already does this, well. Building a less-good
version in zicato's CLI would duplicate effort and create a
secondary UX surface that diverges from harmonograf. The right
integration is "if you want the live view, run harmonograf
alongside; the same JSONL records get rendered with proper
multi-agent semantics".

`zicato run --tail` is mentioned in [CLI.md §3.5](CLI.md#35-zicato-run)
as a future ergonomic — when v0 has shipped and operators ask for it.

## 16. Why we deliberately rejected free-text-diff loss

**Alternative considered.** Instead of typed drift counts, use a
free-text diff between the agent's output and an operator-provided
reference output. Score by edit-distance or by LLM judge.

**Chosen.** Typed drift counts plus optional pass/fail predicates.
See [SCORING.md §1](SCORING.md#1-why-both-signals).

**Why.** Free-text diff loss is uninterpretable. A change from "the
solar panel converts sunlight to electricity" to "solar panels
convert light from the sun into electrical energy" has a large edit
distance but no meaningful quality difference. A change from "the
solar panel converts sunlight to electricity" to "the solar panel
converts sunlight to magic" has a small edit distance but a
catastrophic quality difference.

LLM-judge on text diff loss is one step better but introduces a new
LLM in a load-bearing position with its own biases and failure
modes — and a new source of collusion risk (the judge's model and
the agent's model can conspire silently).

Typed drift counts have clear semantics ("CONFABULATION_RISK fired
five times"), are computable from the event stream (no extra LLM
calls), and combine cleanly with optional pass/fail predicates for
the cases where text-level correctness matters. The trade-off is
that drift counts don't catch quality issues that don't manifest as
drift — but the pass/fail predicate is exactly the operator's hook
for those cases.

## 17. Cross-references

| Topic | Document |
|---|---|
| Marker syntax, validator rules | [MUTATION-SURFACE.md](MUTATION-SURFACE.md) |
| Hypothesis schema, outcome block | [EPOCHS-AND-JOURNALING.md](EPOCHS-AND-JOURNALING.md) |
| Two-callable rule and emulator sealing | [EMULATOR.md](EMULATOR.md) |
| Loss profile and goldfive integration | [TELEMETRY.md](TELEMETRY.md) |
| Wall-clock budget semantics | [BOARD-FORMAT.md](BOARD-FORMAT.md), [SCORING.md](SCORING.md) |
| Three dogfood targets and what they force on v0 | [DOGFOOD-TARGETS.md](DOGFOOD-TARGETS.md) |
| Glossary | [VOCABULARY.md](VOCABULARY.md) |
