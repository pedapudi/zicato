# The proposer — a first-class evaluation-contract input

> **Status.** SHIPPED. The proposer is folded into the contract hash
> (`zicato/epoch/contract.py`), the resolution
> (`zicato/proposer/{skills,agent,adk_agent,tools}.py`) is in the tree and
> exercised by the test suite, and the operator surface
> (`register --proposer-path`) is wired. The **default proposer is the
> tool-using ADK agent** (`build_default_adk_agent`, run by
> `ADKProposerAgent`), used whenever a contract configures no proposer dir;
> the skill-composed text-shim engine is available as an explicit opt-in
> (§2b). Operator-facing how-to lives in the `zicato-design-proposer`
> skill.

The **proposer** is the agent that, each round, reads the epoch's brief, the
mutation manifest, the loss patterns, and the prior experiments, and emits the
next `Experiment` (`{hypothesis, patches}`) for the tournament to judge. It is
a **first-class evaluation-contract input**, alongside the board, the proposer
brief, the scoring, and the inner-harness identity
([EPOCHS-AND-JOURNALING.md §10.1](EPOCHS-AND-JOURNALING.md#101-whats-in-the-contract)).

**The proposer is a contract input rather than configuration.** A different
proposing agent — or a different skill in its prompt — proposes different
mutations and reasons differently, so generations proposed under different
proposers are not directly comparable. Changing the proposer therefore **rolls
the epoch**, in the same way as changing the board or the scoring (§4).

---

## 1. What a proposer is, on disk

A proposer is, on disk, a directory:

```
proposers/<name>/
  skills/
    grounding.md          # SKILL.md-style: optional frontmatter + markdown body
    house-style.md        # zero or more
  agent.py                # OPTIONAL — a custom ADK proposer agent (tier b)
```

- `skills/*.md` — markdown skill modules. Each is SKILL.md-style: an optional
  `---`-fenced frontmatter block (`name` + `description`) followed by a
  free-form markdown body. They are loaded sorted by filename and injected into
  the proposer's system prompt. Zero or more.
- `agent.py` — an OPTIONAL custom proposer agent. Its *presence* selects the
  tier-(b) custom-agent path (§2); its *contents* are part of the contract.

A workspace points at a proposer dir with `zicato epoch register --proposer-path
PATH` (§5). When no proposer dir is configured the proposer is the **built-in
default agent** — a tool-using ADK agent that owns the read-only proposer
tool registry and runs on ADK's own `Runner`
(`ProposerSpec.default()`, `agent_id = "builtin:default"`; the agent is
`build_default_adk_agent`, run by `ADKProposerAgent` in `builtin_default`
mode). The default proposer therefore *reads the world while it reasons* —
the mutation manifest, the parent snapshot, the epoch journal, the analyzer
insights — without any operator configuration.

The proposer dir, like the board / brief / scoring, is the operator's *live,
editable* copy. Its resolved spec is folded into the epoch's contract hash, so
editing it between `evolve` invocations is detected as drift and rolls a fresh
epoch (§4).

---

## 2. The four resolutions

`build_proposer_agent` (`zicato/proposer/agent.py`) resolves a
`ProposerSpec` to a running agent in four ways, in order:

0. **External agent (opt-in)** — `runtime.proposer_agent` names a class by
   dotted path (`spec.external_path` is set). zicato imports it and hands
   it the spec; the class owns its own process, transport and tool
   surface. It resolves first because it is the one tier that is not an
   ADK agent at all. See §2.9.
1. **Custom ADK agent** — a proposer dir ships a `proposers/<name>/agent.py`
   (`spec.agent_source_sha256` is set). zicato loads that author-owned
   `agent` and runs it on ADK's own `Runner`
   (`ADKProposerAgent`, `zicato/proposer/adk_agent.py`).
2. **Built-in default (the DEFAULT)** — no proposer dir is configured, so
   `spec == ProposerSpec.default()`. zicato builds its **built-in
   tool-using ADK agent** (`build_default_adk_agent`) and runs it through
   `ADKProposerAgent` in `builtin_default` mode.
3. **Skill-composed default (EXPLICIT opt-in)** — a proposer dir is
   configured and carries `skills/*.md` but **no** `agent.py`. zicato runs
   the single-shot `DefaultProposerAgent` over `--auxiliary-call-llm`,
   steered by the skill bodies.

### (a) Built-in default — the tool-using ADK agent, zero config

The DEFAULT. With no proposer dir configured, zicato runs
`build_default_adk_agent` — a **native ADK `LlmAgent`** that opts into the
full read-only proposer tool registry
(`zicato.proposer.tools.DEFAULT_PROPOSER_TOOLS`) and is bound, per round, to
the workspace's auxiliary model string (threaded as `ProposerContext.model`
by the orchestrator). It runs on ADK's own `Runner` rather than the auxiliary
text shim, which cannot express the function-calls a tool-using agent needs
(§3). The default
proposer can therefore grep the mutable surface, read the parent snapshot,
and consult the journal and analyzer insights *while it reasons*, with no
operator configuration. Because the default uses the operator's
already-configured auxiliary model, the model-distinctness warning (§3) is
skipped for it; that reuse is the documented, expected posture rather than
an author error.

### (b) Skill-composed default — drop `skills/*.md`, no code (opt-in)

The cheapest *customization*. Drop one or more `skills/*.md` into
`proposers/<name>/skills/` and configure the dir; do **not** write an
`agent.py`. zicato runs the built-in `DefaultProposerAgent`, a single-shot text
exchange driven on `--auxiliary-call-llm`: the auxiliary callable is handed a
`(system, user, model) -> str` prompt and the returned string is parsed into
the `Experiment`. The skill bodies are injected into the **system prompt**, so
they steer *how* this proposer reasons (grounding instructions, house
style, a checklist) without any code. Configuring a proposer dir (without an
`agent.py`) is the explicit opt-in into this single-shot text-shim engine;
the bare, unconfigured default (a) is the tool-using agent.

This tier shapes the proposer's reasoning over the text shim without giving
it new capabilities. The model is the auxiliary model, which this tier does
not own.

### (c) Custom ADK agent with tools — `agent.py`

To own the model the proposer runs on, give it a curated tool subset, or
write a bespoke instruction, ship a
`proposers/<name>/agent.py` that exposes a module-level `agent` — a **native
ADK `LlmAgent`** with its **own `model=`** and a `tools=` list drawn from
zicato's read-only proposer tool registry. zicato loads that agent and runs
it on ADK's own `Runner` (`ADKProposerAgent`, `zicato/proposer/adk_agent.py`).

The read-only tool registry (`zicato.proposer.tools.DEFAULT_PROPOSER_TOOLS`):

| Tool | Reads | Guard |
|---|---|---|
| `list_mutation_points` | The round's mutation manifest — the exact ids the agent may target. | Read-only; renders the bound context's manifest. |
| `read_mutable_file` | One file under the parent generation's mutable subtrees. | Read-only; rejects absolute paths and `..` traversal outside the mutable surface. |
| `grep_mutable` | Regex search across the mutable subtrees, `path:line: text`. | Read-only; match count capped to protect the context window. |
| `read_journal` | The epoch's running narrative journal. | Read-only; empty string when absent. |
| `read_insights` | The epoch's latest analyzer insights — same content the default proposer embeds. | Read-only; empty string when absent. |
| `mutation_track_record` | One mutation point's per-epoch track record from the analytical index — banded aggregates over *experiments touching the point* (multi-patch experiments confound credit; never causal). | Read-only; manifest-scoped ids; bucketed Δscalar only, inside the restricted-visibility envelope. |
| `read_parent_diff` | What the last promotion changed: the parent generation diffed against *its* parent (git backend: one read-only `git diff` between tags; directory backend: the journal's patch records). | Read-only; output capped; explicit notice for a seed generation. |
| `mutation_usage` | Where a mutation point's current value/symbol is referenced across the parent snapshot. | Read-only; delegated to `grep_mutable`, so the mutable-subtree sandbox and match cap apply unchanged. |

Every tool is **read-only** by contract. A proposer tool that wrote to the
snapshot would corrupt the very tree the round is about to patch (and break the
applier's content-hash guard), so the whole surface only reads. The tools read
their per-round runtime context (which snapshot, which manifest, which epoch)
from a `contextvars.ContextVar` that `ADKProposerAgent.propose` binds around
each run, so concurrent challengers never leak context into one another.

A custom `agent.py` opts in with simply:

```python
from zicato.proposer.tools import DEFAULT_PROPOSER_TOOLS
agent = LlmAgent(name="my_proposer", model=..., instruction="...",
                 tools=list(DEFAULT_PROPOSER_TOOLS))
```

The copy-me example is
[`examples/zicato_examples/proposer_with_tools/agent.py`](../../examples/zicato_examples/proposer_with_tools/agent.py)
(`build_agent(model=...)` + a lazily-built module-level `agent`).

---

## 2.5 What the proposer reads — the failure-mode feedback channel

Independent of *which* resolution backs the proposer, every proposer reads the
same per-round task input the orchestrator assembles: the brief, the mutation
manifest, the loss summary, the prior-experiment digest, the analyzer
insights. That input also carries a **failure-mode profile**: a compact,
board-anonymized read of *why* the parent's answers were wrong, rather than
only that a scalar moved.

A coarsened `Δscalar` plus an LLM digest of *decision* telemetry cannot tell
the proposer whether the harness over-retrieves, misses, or answers emptily.
The failure-mode channel (`src/zicato/analyzer/outcome_marginals.py`, rendered
by `render_failure_mode_profile` in `src/zicato/proposer/prompts.py`, wired by
`_render_failure_profile` in `src/zicato/orchestrator.py`) supplies that
distinction by feeding the proposer **outcome marginals** — board-wide rates —
under three safeguards, each reusing existing machinery:

- **Train-slice only.** The orchestrator passes the *train-slice* losses it
  already loaded — the same `split_board` / rotation partition it uses for the
  patterns + loss summary — never the holdout. The aggregator never reads the
  board or the filesystem, so it cannot widen the slice it is handed. This is
  the same anti-leakage discipline as the rest of the proposer's view
  ([OVERFITTING.md §11](OVERFITTING.md)).
- **Bucketed / coarsened.** Every rendered number is banded at the render
  boundary — rates to approximate `~N%` labels, quality means to
  `low`/`medium`/`high` — mirroring the `_bucket_scalar_delta` discipline, so
  no round-over-round response surface leaks that the proposer could climb
  instead of true quality.
- **Identity-free.** Only marginal rates are produced: no entry id, question
  text, or output token exists anywhere in the summary, by construction (the
  aggregator reads only the scalar / count / metric fields of each result).
  The design invariant is *feed the MARGINAL, never the JOINT* — the proposer
  may learn an aggregate property of the agent's behaviour ("over-retrieves
  ~40% of runs") but can never reconstruct any board entry.

When the slice is empty (a baseline round with no parent telemetry) the
renderer returns the empty string, so the block is omitted from the prompt,
in the same way as the insights and prior-experiments blocks.

### Operator hook — `outcome_summarizer_spec`

An OPTIONAL operator summarizer can contribute **board-specific** marginals.
It is a dotted spec — `outcome_summarizer_spec` on the scoring contract
(`ScoringWeights`, `src/zicato/core/types.py`) — resolved in the same way as
predicates and judges (`zicato.import_path.import_dotted_path`). The resolved
callable receives the train-slice per-entry results and must return a
**structured aggregate**, a `{marginal_name: numeric_rate}` mapping rather
than prose, so that zicato can enforce bucketing and anonymity on its output.
`sanitize_operator_marginals` drops anything non-numeric or identity-bearing
(a free string, an entry id as a key, a list/dict value) before the operator's
marginals are merged and banded. A free-text summary would be an un-auditable
leak vector and is rejected by construction; a misbehaving or raising
summarizer contributes nothing rather than aborting the round.

Because it lives on `ScoringWeights`, the spec folds into the scoring
component of the contract hash automatically: configuring or changing the
summarizer rolls the epoch, like every other contract field
(the empty-string default configures no summarizer, leaving the prompt
unchanged). The shaped numbers that band these marginals are part of the
scoring surface and are owned by SCORING.md; this document describes the
*channel* alone.

---

## 2.6 The mechanical recombination slot

`proposer_quality.recombine` (default OFF) opts in a MECHANICAL merge of two
already-evaluated challengers — no LLM call. The premise is that a single
champion can only ever discount ONE challenger's fix. So when two REJECTED
challengers of the current reign each fixed a DISTINCT slice of the board with
NON-OVERLAPPING edits, the last best-of-N slate slot mints the UNION of their
patches. A non-vetoed mint is then chosen with
`selection_mode = "recombined"`. A parsimony-biased selector rejects each
single fix; the union clears the gate
that neither half could, so the slot bypasses the minimal-diff selection
heuristic, whose diff key would otherwise starve the larger union.

The slot is cost-neutral: the mint REPLACES the slot's auxiliary propose call
rather than adding one, so a recombining round spends `best_of_n − 1` calls.
It is also envelope-clean. Selection runs on per-entry PASS-FLIP evidence
computed orchestrator-side and intersected with the TRAIN board there, so
entry ids never reach the proposer and the holdout is never eligible. The knob
requires `best_of_n > 1` to have any effect; flipping it rolls the epoch,
because a slate that can recombine proposes under a different rule.

The full mechanism — the 8 eligibility predicates, the 4-key deterministic
ranking, the minter, the `recombined` selection mode, and the KNOWN NARROWING
(pure drift-side complementary pairs are invisible by design; they remain
reachable through the in-context genealogy channel, with a
drift-delta-with-confirmation variant as a documented future seam) — is
specified in **[dev-guide 05 §5.6.11](../dev-guide/05-proposer.md)**
(`src/zicato/epoch/recombine.py`, `src/zicato/proposer/recombine.py`).

### 2.6.1 Merge modes — `mechanical` (default) vs `llm`

`proposer_quality.recombine_merge` (a string, default `"mechanical"`; values
`"mechanical" | "llm"`) chooses HOW the slot composes the union once the
selector has picked a pair. It is meaningful only when `recombine` is on and
`best_of_n > 1`. At its `"mechanical"` default it is omitted from the contract
canonical form, so the hash is byte-identical and nothing rolls
retroactively. The value `"llm"` rolls the epoch, because a slate that can
compose an LLM merge proposes under a different rule. A `"llm"` value set with
`recombine` off is accepted-and-inert (the
dependent-knob house style, as `screen_veto_only` is inert without
`screen_entries`); it still rolls the hash, like an inert
`screen_veto_only`.

- **`mechanical`** — the behaviour of §2.6: the last slot MINTS
  the concatenation of the two patch sets with NO LLM call. It REQUIRES a
  DISJOINT pair (predicate #7): the applier is last-wins on a duplicate
  target, so overlapping edits would silently drop one side. Cost: a
  recombining round spends `best_of_n − 1` propose calls (the free mint
  replaces the slot's own sample call).

- **`llm`** — the last slot issues ONE auxiliary call (the depth
  refinement-class role, as the self-critique call does) rendering a MERGE
  prompt from the selected pair. The response flows through the NORMAL
  proposal parse, `enforce_forbidden`, and validate path, so it is a proposal
  like any other. It is stamped with the same `recombined_from` provenance,
  and a non-vetoed merge is chosen with the same
  `selection_mode = "recombined"`. On
  any parse/validate failure the slot DEGRADES to a fresh LLM sample (the
  mechanical mint's exact degrade). This mode exists to reach the pairs
  mechanical mint cannot: when two rejected fixes OVERLAP on a mutation target,
  a model can compose a genuine merge (resolving the shared edit) that a
  last-wins concatenation cannot.

**What relaxes, and what never does.** Only the disjointness predicate
relaxes, and only for pair selection in `llm` mode — the model resolves the
overlap, which is the whole point. Overlap does not vanish: it becomes a
RANKING consideration (prefer LESS overlap at equal coverage), slotted into the
existing deterministic key immediately after coverage so that mechanical-mode
selections — where every surviving pair has zero overlap — are byte-identical
to before. Every other predicate holds unchanged in both modes: rejected,
current reign, non-placebo, non-recombined parent, pair-not-tried,
manifest-valid patches, and complementarity above all — each parent must
still carry a distinct win the other lacks, since an LLM merge of two
identical fixes yields nothing.

**The envelope (LOAD-BEARING).** The merge prompt carries ONLY
proposer-authored artifacts, in the genealogy-channel redaction
vocabulary (§2.7): both parents' PATCHES (the `new_content` the proposer
itself wrote), their hypothesis CORE IDEAS, their whole-candidate BANDED
outcomes (through the same `improved`/`flat`/`regressed` `_bucket_scalar_delta`
vocabulary the experiment memory renders), and COUNTS-ONLY complementarity
(how many train entries each parent improved, and the combined
improved/regressed counts). It NEVER carries a board-entry id, a per-entry
result, or an exact Δscalar — the improved/regressed entry-id sets are computed
and discarded inside `_build_recombination_pair`, and the holdout is never
eligible (the `train_entry_ids` filter). The merge call widens the proposer's
visibility by NOTHING the genealogy channel does not already permit.

One semantic note: an `llm` merge is **a full proposal rather than strictly
a union** — the response validates against the whole mutation manifest, so it
may touch points neither parent did. `recombined_from` records the pair that
*seeded* the merge; only the mechanical mint guarantees the patch set is
exactly the parents' union. The gate adjudicates either way.

**Cost.** `mechanical` spends `best_of_n − 1` propose calls (the mint is free).
`llm` spends `best_of_n` on the happy path: the merge call SUBSTITUTES the
slot's own sample call, so a successful `llm`-merge round costs what a
recombine-OFF round costs. The one exception is the degrade: a merge response
that fails parse/validation has already spent its call, and the fallback
fresh sample adds one more (`best_of_n + 1` for that round — rare, and the
reason the `estimate_cost` figure is documented as an estimate rather than a
cap).

---

## 2.7 The genealogy channel — in-context evolution, envelope-safe

`proposer_quality.genealogy` (an `int`, default `0` = OFF) opts the proposer
into an IN-CONTEXT view of the current reign's candidate lineage — the
zicato analogue of AlphaEvolve's *prompt sampler*, which feeds parent
programs and their scores back into generation so the LLM evolves in
context. The mechanical recombination slot (§2.6) merges two rejected fixes
WITHOUT an LLM call. The genealogy channel instead gives the LLM the raw
material to merge, extend, or diverge from what has already been tried. It is
the same in-context recombination authored by the model, and it reaches even
the pure-drift-side pairs the mechanical slot cannot see (the §2.6 KNOWN
NARROWING). It is a RENDER-SIDE channel only: it splices a prompt
block and touches no evaluation, so the cost meter is untouched (the
process-exemplars precedent, §2.5).

The design-first rule (ch04 §12) requires the redaction contract IN
WRITING, before the code, because this channel widens what the proposer
reads about prior candidates. The whole point of the section is that
widening candidate genealogy is NOT widening evaluation data.

### What the channel carries

Two kinds of item, each a proposer-authored artifact plus a BANDED outcome:

- **Parents** — the current champion's OWN promoted patch history: the
  spine of experiments that were promoted up the lineage to the reigning
  champion (the `parent_generation_id` chain), most-recent-first, capped at
  half the item budget. These are the "build on these" ancestors — the
  edits that WORKED, so the proposer can extend the winning line rather than
  re-derive it. Each carries: the hypothesis `core_idea` (proposer-authored
  free text), a `patch_summary` (the targeted mutation-id set + the patch op
  kinds + a coarse size band — plus, at most, a short excerpt of the PATCH
  DIFF TEXT itself, which is proposer-authored and therefore in-envelope, capped),
  and the banded outcome.

- **Inspirations** — DIVERSE rejected candidates of the current reign,
  chosen by mutation-id-set DISSIMILARITY (a greedy max–min Jaccard walk over
  the rejected pool: each pick maximizes its minimum Jaccard DISTANCE to the
  already-chosen set, so the surfaced inspirations span the widest spread of
  DISTINCT ideas rather than N variants of one). These are the "here is what
  else was tried, and how it landed" material — a rejected idea is not a dead
  end when a different framing of it might clear the gate. Same per-item
  payload as a parent; the outcome band reflects the MEASURED Δscalar, NOT the
  gate's verdict — rejected is not regressed. A candidate the gate rejected can
  still band `improved` (a real but insufficient gain, or a win the
  cross-regression / diversity guard vetoed): the band says how the delta
  landed, the rejection says the gate declined to promote it. That is the
  signal the proposer needs — this framing moved the measurement but did not
  clear the threshold — carried coarsely, without a number.

### What the channel NEVER carries

The envelope boundary (dev-guide 05 §5.8; OVERFITTING.md §11), stated as
hard exclusions:

1. **No board-entry ids.** Never an entry id, a question, an answer, or any
   per-entry token. Genealogy is CANDIDATE lineage — patches, ideas, and
   whole-candidate outcomes — never board content.
2. **No per-entry results.** Never a per-entry pass/fail, a per-entry
   drift verdict, or a matchup grid. The outcome is a WHOLE-CANDIDATE band.
3. **No exact deltas.** The Δscalar is coarsened to a band through the
   EXISTING `_bucket_scalar_delta` vocabulary (`improved` / `flat` /
   `regressed`) — the same memorization-resistant banding the experiment
   memory already uses (§2.5; OVERFITTING.md §11 #4). The exact
   response-surface number never reaches the model.
4. **No holdout anything.** The pool is the reign's REJECTED + PROMOTED
   candidates — whole experiments — and nothing is ever read from, sliced
   by, or intersected against the holdout. There is no per-entry read at
   all, so there is no per-entry slice to leak; the holdout cannot enter a
   channel that never looks at board entries.

The only numbers that ride the channel are the banded outcome
(`improved`/`flat`/`regressed`) and coarse patch metadata (a mutation-id
count, an op-kind list, a size band). Everything else is proposer-authored
text (the `core_idea`, the patch diff excerpt) — content the proposer wrote
in the first place, echoed back to it. **This widens NOTHING about
evaluation data**: the channel carries candidate genealogy rather than board
data.

### The banding vocabulary

The whole-candidate outcome is banded through
`_bucket_scalar_delta` (`src/zicato/proposer/prompts.py`) — the same
`improved` / `flat` / `regressed` three-band vocabulary the prior-experiments
block already renders under `restrict_visibility`. A candidate with no
settled Δscalar (an in-flight sibling — never sampled here, but defensively)
renders no band. No new banding primitive is introduced; a reader who knows
the experiment-memory bands reads genealogy with no new vocabulary.

### The cap discipline

Budget-capped rendering, following the process-exemplar cap style (§2.5):
`genealogy = k` bounds the TOTAL items rendered to `k`. Parents take the
first `k // 2` slots (most-recent-first along the champion spine),
inspirations take the remainder (the greedy dissimilarity walk, capped at
what is left). The pool the sampler reads is itself bounded to a small
constant of most-recent candidates (the recombination pool cap precedent),
so the O(pool²) dissimilarity scan stays cheap regardless of epoch length.
Per-item, the two proposer-authored free-text fields are BOTH head-capped
with an elision marker — the patch diff excerpt (`_DIFF_EXCERPT_MAX`) and the
`core_idea` (`_CORE_IDEA_MAX`) — so no single item can balloon the block.
An empty result renders the EMPTY STRING — the "omit this section entirely"
sentinel — so a `genealogy = 0` round adds nothing to the prompt.

### The determinism requirement

The sampler is a PURE, DETERMINISTIC function of (the reign's records, the
ratings, `k`) — **no RNG**. Parents sort by round (the spine order);
inspirations are the greedy max–min-Jaccard walk with a TOTAL tie-break
(Elo DOWN, then generation-id ascending) so the same pool always yields the
same inspirations in the same order, in ANY input order. Determinism is the
leakage budget: a byte-identical block round-over-round (while the reign's
candidate set is unchanged) re-presents nothing new, which is the argument
the process-exemplar channel makes.

The full mechanism — `GenealogyItem`, `sample_genealogy`, the greedy
dissimilarity walk, the render block, and the `genealogy` knob — is
specified in **[dev-guide 05 §5.6.13](../dev-guide/05-proposer.md)**
(`src/zicato/proposer/genealogy.py`).

---

## 2.8 The critic-calibration channel — feeding prediction accuracy back

`proposer_quality.calibration_feedback` (an `int`, default `0` = OFF) opts the
proposer into an IN-CONTEXT view of ITS OWN PREDICTION CALIBRATION — how the
falsifiable movement predictions it wrote in past hypotheses actually landed
against realized outcomes. The prediction-accuracy grader
(`hypothesis_ledger` / `grade_hypothesis_predictions` in
`src/zicato/tournament/detail.py`, surfaced by the `/api/hypothesis-accuracy`
dashboard feed) scores every settled hypothesis's predicted-vs-realized
movements. Without this channel that score reaches only the dashboard. The
channel routes it back to the proposer, so that a proposer shown its own miss
pattern hypothesizes more honestly: it stops writing confident, un-earned
predictions once it can see that its confident predictions have been
missing.

Like the genealogy channel (§2.7) this is a RENDER-SIDE channel only: it
splices a prompt block and touches no evaluation, so the cost meter is
untouched (the process-exemplars precedent, §2.5). The design-first rule (ch04
§12) requires the redaction contract IN WRITING, before the code, because — as
with genealogy — this channel widens what the proposer reads. The whole point
of the section is that showing the proposer its OWN calibration is NOT widening
evaluation data.

### The unit — a settled hypothesis is one "claim"

Each settled hypothesis of the current reign is ONE claim the proposer made
about the world: a `core_idea` (proposer-authored) plus a set of falsifiable
movement predictions the grader can verify. The grader returns, per hypothesis,
`(matches, predictions)` — how many of its predicted movements verified, of how
many it made. From that pair each claim is graded into exactly ONE bucket:

- **hit** — the proposer made falsifiable predictions and EVERY one verified
  (`matches == predictions`, `predictions > 0`). The proposer called it.
- **miss** — the proposer made predictions but at least one did NOT verify
  (`matches < predictions`, `predictions > 0`). The prediction was (partly)
  wrong. A hit requires every prediction to verify, which rewards
  conservative, well-earned prediction over confident over-claiming.
- **unresolved** — the hypothesis made NO gradeable predictions
  (`predictions == 0`), so calibration is silent on it. (Matches the
  experiment-memory reader's "None accuracy = made no graded predictions.")

One rendered-block corollary worth knowing when reading it: the grade and
the banded outcome are INDEPENDENT axes, so a claim can render
`HIT · Δscalar regressed` — every specific prediction verified while the
candidate's overall scalar still worsened. That combination is correct rather
than a defect: the grade measures forecasting skill and the band measures the
outcome.

### What the channel carries

A per-reign calibration summary, rendered into the proposer context:

- **Per-claim-type COUNTS** — the hit / miss / unresolved tallies over the
  reign's settled hypotheses.
- **The overall calibration fraction** — `hit / (hit + miss)`, the fraction of
  the proposer's GRADED claims it called correctly. This is the proposer's OWN
  self-accuracy meta-signal, pooled over its own predictions — never a board
  number. Climbing it means predicting more honestly, which is the behaviour
  the channel exists to encourage. It is a calibration target rather than a
  board-response surface to game.
- **Up to K recent graded claims** — the K most-recent hit/miss claims
  (most-recent-first), each rendered as `(claim text, banded realized outcome,
  hit | miss)`. The claim text is the proposer's own `core_idea` (capped
  head-only, the genealogy `_core_idea` discipline); the banded realized
  outcome is the whole-candidate Δscalar through the EXISTING
  `_bucket_scalar_delta` vocabulary (`improved` / `flat` / `regressed`).
  Unresolved claims carry no realized band to show, so the recent list is
  hit/miss only — the counts still tally the unresolved.

`calibration_feedback = K` bounds the recent list to K; the counts + fraction
are aggregate and always computed when the channel is on. When there is no
GRADED history yet (`hit + miss == 0` — a baseline reign, or one whose settled
hypotheses all made no falsifiable predictions) the sampler returns nothing and
the renderer emits the EMPTY STRING, the "omit this section entirely"
sentinel. A `calibration_feedback = 0` round, and any round with no graded
claims, therefore renders the prompt without the block.

### The envelope (LOAD-BEARING)

Stated as hard exclusions, in the genealogy channel's vocabulary (§2.7):

1. **Claim text is PROPOSER-AUTHORED.** The only free text on the channel is
   the proposer's own `core_idea`, echoed back to it (capped). Never a board
   question, answer, or per-entry token.
2. **Realized outcomes render BANDED.** The per-claim realized outcome is the
   whole-candidate Δscalar coarsened through `_bucket_scalar_delta` — the same
   `improved` / `flat` / `regressed` three-band vocabulary the experiment
   memory and genealogy already use. The exact response-surface number never
   reaches the model.
3. **Never an entry id, never a per-entry result, never an exact delta.** The
   grade (`hit` / `miss`) is a WHOLE-HYPOTHESIS verdict computed from the
   grader's `(matches, predictions)` COUNTS; the counts + fraction are
   aggregates over the proposer's OWN predictions. No per-entry pass/fail, no
   per-movement number, and no exact Δscalar rides the channel.
4. **No holdout anything.** The grader scores predicted-vs-realized MOVEMENTS,
   which are whole-candidate metric aggregates — there is no per-entry read, so
   there is no per-entry slice, and the holdout cannot enter a channel that
   never looks at board entries.

This widens NOTHING about evaluation data: it re-presents the proposer's own
authored predictions and a coarse verdict on whether they held.

### The determinism requirement

The sampler is a PURE, DETERMINISTIC function of (the reign's graded claims,
`k`) — **no RNG, no wall clock**. Counts are order-independent tallies; the
recent list sorts by round DOWN then generation-id ascending (a TOTAL key), so
the same claim set always yields the same block in ANY input order — the
byte-identical-round-over-round leakage-budget argument the genealogy and
process-exemplar channels already make.

The full mechanism — `CalibrationClaim`, `CalibrationSummary`,
`sample_calibration`, the render block, and the `calibration_feedback` knob —
is `src/zicato/proposer/calibration.py` (the pure sampler, mirroring
`genealogy.py`'s pure/no-IO discipline) + `_build_calibration_summary` in
`src/zicato/orchestrator.py` (the once-per-round IO builder, joining the
reign's durable records with the grader's ledger).

---

## 2.9 The external resolution — a proposer that is its own process

`ProposerAgent` is a one-method protocol, so a proposer that runs outside
the ADK `Runner` needs no new machinery — only a way to *name* it and a
way to *hash* it. Both live in `zicato/proposer/external.py`:

```toml
[runtime]
proposer_agent = "zicato.proposer.pi_agent:PiProposerAgent"
```

The class answers `contract_identity(config)` with its **causal surface**,
and that mapping's digest is folded into the `proposer` contract component
beside `agent_source_sha256` (§4). A workspace that names no external
proposer omits the key, so its canonical form and contract hash are
unaffected, which is pinned in `tests/test_proposer_external_seam.py`.

Four things belong in that identity: the version of the third-party runtime,
the bytes of the files zicato owns, the tool set, and the launch envelope. The
runtime version is recorded coarsely, because a patch release that changes no
prompt and no tool schema should not roll an epoch, and the standing rule *do
not upgrade mid-tournament* carries the rest. The files zicato owns are edited
in place, so they have no version to record and their bytes stand in. What
does **not** belong: the model. A `models.*` role is
runtime infrastructure that does not roll an epoch, and the contract hash
names no model. The collusion hazard an external tier
introduces — an agent quietly falling back to its own configured default —
is closed where it happens, at launch: the resolved `ctx.model` is threaded
into the process and an empty one is a hard failure, asserted in
`tests/test_proposer_pi_envelope.py`.

**The first implementation is pi** (`zicato/proposer/pi_agent.py`,
`integrations/pi/`): one `pi --mode rpc` subprocess per challenger, driven
through the *same* `propose_experiment` engine the text shim uses. The live
RPC session is handed to that engine as the `aux_call_llm` callable, so a
bounded retry becomes a follow-up message on a warm conversation instead
of a cold restart that re-sends the whole manifest. The tier differs from
the shim in transport rather than in semantics, which is what makes it an
honest paired baseline against the shim.

When `best_of_n > 1`, pi advertises the optional native-slate capability.
Zicato still owns the visibility envelope, screening, deterministic fallback,
and final tree mount, but generation, comparison, and a screen-triggered
revision all occur as turns in that challenger's one conversation. The review
turn uses the bounded `select_candidate` tool; an absent or out-of-range index
degrades to the deterministic selector. The round log
retains the ordinary `candidate_sampled` events and enriches
`critique_selected` with candidate summaries and the review rationale, making
the in-session decision inspectable without board identities. The default aux
critic records the same two fields by another route — it answers with the
index and one sentence — so a reader of `round_log.jsonl` learns what was
chosen and why regardless of which transport ran.

The session boundary is one proposal slate: it is never shared across board
entries, runs, challengers, or rounds. Pi receives only `ProposerContext`'s
restricted aggregates and mutation surface—never board entries. This
capability belongs only to the proposer seam; emulator, judge, adjudicator,
and target adapters retain their own structured/text execution contracts.

Stage-specific proposer model overrides are rejected on this path: a
separate generation or review model would require another process and
would contradict the native-session contract. They remain supported by the
generic best-of-N wrapper for proposers without this capability.

The envelope is the other half, and it is enforced by what the proposer is
shown. A default coding-agent session has `bash`, `read` and `grep`
pointed at the working directory; a proposer with those can read the board
and the holdout slice, and nothing errors and nothing warns. The sanctioned
launch therefore turns off built-in tools, extension, skill and
prompt-template discovery, and context files. Project-local files are
untrusted. The agent gets a fresh isolated directory with credentials copied
in explicitly, no packages, and no cross-round memory. It writes no session
file, because cross-round persistence would be an unhashed side channel
around the overfitting envelope. Its working directory sits outside every
snapshot: the snapshot is the system under test, and reading it ambiently
would be both an unhashed contract input and an injection path from the
thing being rewritten into the thing rewriting it. Continuous integration
asserts the running agent's active tool list equals the sanctioned set.

---

## 2.10 `validate_patches` — the proposer's closed loop

Every channel above feeds the proposer *inputs*. This tool checks its
*output*.

Without it a proposer emits a patch set and is done, with no check on its own
work. For a span replace of a short instruction that is adequate: the applier
re-quotes span content as a Python string literal, so a span edit is
structurally incapable of breaking syntax or dropping an import. For a
**file-marker `replace`**, though, `new_content` is an entire post-edit module
that must satisfy every constraint in
[MUTATION-SURFACE.md](MUTATION-SURFACE.md) §6 — the parse check (`A1`), id
resolution (`A2`), placeholder survival (`A3`), and import preservation
(`A4`). Emitting a whole module in one shot and hoping it satisfies all four
is the workload a tool-using agent exists to avoid, and a violation costs a
full retry round-trip through the propose loop, re-sending the entire
manifest.

`validate_patches` (`src/zicato/proposer/validate.py`) is a **linter for
patches**. The proposer drafts, validates, sees `A4: dropped 'import re'`,
fixes it, validates again, and only then answers. Zicato's bounded retry is
then the rare fallback rather than the main loop. It is in
`DEFAULT_PROPOSER_TOOLS`, so the closed loop is available to the ADK default
proposer as well as to an external agent, and the default proposer's
instruction tells it to validate before answering.

### The governing principle

> **The proposer may check its patch by any means that consumes no board data
> and produces no scores; it may never execute board entries.**

That line separates a legitimate self-check from a proposer grading its own
work. If the proposer could run against a slice it chose, it would be doing the
tournament's job with none of the tournament's guards — the overfitting failure
the whole meta-loop exists to prevent (see the non-goal in issue #147: *the
proposer does not run the inner harness*). Everything in the tool is therefore
static: the tree it writes to is a disposable scratch copy, the checks read
source, and the load probe resolves the harness entry point **without invoking
it**.

The principle is enforced **structurally rather than by inspection**. An
import-linter contract in `pyproject.toml` ("the proposer's patch validator has no path to
the board") forbids `zicato.proposer.validate` from reaching the entire
capability surface: `zicato.board` (where entry text is loaded),
`zicato.adapters` / `zicato.adapter_factory` / `zicato._tournament_worker` (how
a harness is loaded and run), and `zicato.emulator` / `zicato.judge_runtime`
(how an entry is judged). `tests/test_proposer_validate.py` pins the same
property over the runtime import closure, so a regression is caught by
whichever gate a change hits first.

Two structural consequences follow:

- **The tier-3 probe lives in its own module** (`_load_probe.py`) and is
  reached by *spawning a subprocess* rather than by importing the adapter
  factory. That is what keeps the adapter packages on the forbidden list
  instead of forcing an exemption, and it contains `adapter.load`'s arbitrary
  operator code (which can hang, or leave import side effects) in a child
  process with a timeout.
- **The context plumbing lives in `tool_context.py`.** Importing
  `zicato.proposer.tools` merely to reach `_active_context` would drag the
  analyzer — and through it the board loader — into the validator's import
  closure, making the contract unsatisfiable for a reason that says nothing
  about what the validator does.

`zicato.scoring` and `zicato.tournament` are **not** on the forbidden list:
every module in the repo reaches them through
`core.types → core.scoring_config`, which imports them for *type definitions*.
That edge is a type-model artifact rather than a capability.

### The three tiers

Stages run in order and stop at the first that fails — there is nothing to lint
in a tree that would not apply. The tool returns
`{"ok": bool, "errors": [...], "tiers": {...}}`; `errors` is the flat list to
act on, `tiers` says which stage each finding came from.

| Tier | What it runs | Reuses |
|---|---|---|
| 1 **structure + apply** (always on) | the `patches` shape pass, the cross-check pass (mutation-id resolution, op/payload discrimination, `min`/`max`, enum domains), the pre-image guard (`content_hash` from the drafted-against manifest vs a fresh enumeration), the pre-apply surface check, an all-or-nothing apply into a scratch copy of the parent snapshot, then A1–A4 | `PATCHES_JSON_SCHEMA` + `parse_patch_list` (`structured.py`), `validate_patches` + `validate_post_apply` (`mutation/validator.py`), `apply_patches` (`mutation/applier.py`) |
| 2 **static analysis** (opt-in, contract-declared) | the workspace's declared linter / type-checker set over the scratch tree | the tools already in zicato's environment, via `sys.executable -m` |
| 3 **load probe** (on whenever the workspace has a config to resolve an adapter from) | `adapter.load` against the scratch snapshot in a subprocess with a timeout — the same call the tournament makes before any entry executes, one expensive round earlier | `make_adapter_from_config` + `load_workspace_config`, in the child process |

Tier 1 reimplements nothing: every check it runs is machinery the round
pipeline already applies after the proposer answers. The tool's contribution is
running it *before*.

**Tier 2 reports a delta rather than raw findings.** Each declared check runs
over the parent tree *and* the scratch tree; only findings present in the second and
absent from the first are errors. Real trees carry lint debt, and a validator
that blamed a patch for the tree it landed in would fail every draft and teach
the proposer to ignore it. The comparison normalizes away the file path and the
`line:col` prefix, so an edit that shifts line numbers does not manufacture
findings. That normalization is approximate in one direction: a new finding
textually identical to a pre-existing one elsewhere in the same file is
suppressed, which is the right error for an advisory linter.

**Errors and notes are different things.** A checker that is not installed, a
misspelled check name, a workspace with no adapter to probe — these are *notes*.
They are reported so the operator learns nothing is running, but they never set
`ok: false`. A validator that failed a well-formed patch because a dev tool was
missing is a validator the proposer learns to distrust.

### The static-check set is contract, and it is a closed registry

Tier 2's set is declared at `contract.proposer_static_checks` in the
workspace's `config.json` — the same `contract` block that carries
`proposer_path` — and folded into the proposer component of the contract hash
by `_canon_proposer`. It is contract rather than configuration: changing which
checks the proposer must satisfy before it will emit a patch changes which
patches it accepts from itself, hence what it proposes. The empty default is
**omitted from the canonical form**, so a workspace that configures no static
checks keeps the hash it has (§4's omit-at-default discipline).

The declarable names are a **closed registry** (`STATIC_CHECKS`: `ruff`,
`ruff-format`, `mypy`, `compileall`) rather than operator-supplied command
lines. A hashed *name* is a stable, reviewable identity; a hashed command line
would be an arbitrary-execution surface that a contract edit could widen
silently. A workspace needing a checker that is not there should propose adding
it to the registry rather than gaining a way to name any command.

### The pre-image guard

`MutationPoint.content_hash` records the text a mutation point held at
enumeration. The applier does not compare it; tier 1 does.

Tier 1 compares `content_hash` for each patched point between
**the manifest the proposal was drafted against** (`ProposerToolContext.mutations`,
which is an `enumerate_mutations` result) and **a fresh enumeration of the parent
snapshot** at validate time. A point whose hash moved between the two was
rewritten under the proposer — by a concurrent promotion, or an operator editing
the tree — so the draft is reasoning about text that has been replaced, and
applying it would clobber whatever changed it.

**The guard asks nothing of the proposer.** Making the pre-image a digest the
model declares on each patch would be worse in two ways. It would make the
guard *opt-in*, because a model that omitted the field would not be checked,
and the guard's whole value is in the case where the model does not realise
anything is wrong. It would also ask the model for arithmetic it has no reason
to get right. Comparing two enumerations zicato already computes needs no
cooperation and no wire change — **`Patch` carries no pre-image field and must
not grow one**; issue #147 is explicit that the `Experiment` schema does not
change.

The applier is not the site for this check. It applies a patch set that has
already been validated, all-or-nothing; a staleness rejection there would
surface as a failed derive with no route back to the proposer that could fix it.
Catching it in `validate_patches` puts the finding where a fix is still cheap.
A point that has *vanished* rather than moved is left to the post-apply
id-resolution check (`A2`), which reports it against the post-apply tree with a
clearer message, so that one fault costs one fix rather than two.

> ⛔ The standing prohibition is unchanged: **no proposer tool may write to the
> generation snapshot.** `validate_patches` does not relax it — it writes only
> into a `ztw-pvalidate-*` scratch tree in the OS temp root, removed in a
> `finally`, and never touches the tree the round is about to patch. That
> prefix is distinct from `ztw-slate-*` so the round pipeline's
> stale-slate sweep can never reap a live validation.

---

## 2.11 The redacted query surface

Every channel in §2.5–§2.8 is **pushed**: the orchestrator samples, bands, and
renders it before the proposer sees a byte. A pushed channel gives the proposer
no way to ask a follow-up question, though diagnosing *why* the harness fails
is its job. This surface (`src/zicato/proposer/redacted_query.py`) makes a
small, provably-clean part of the same corpus **pulled**, under the same
privacy envelope.

Three tools, all banded per-entry incidence over the champion's train slice:

| Tool | Answers |
|---|---|
| `train_slice_drift_profile()` | which drift kinds fire, at which severities, and whether a mode is broad or narrow |
| `train_slice_agent_profile()` | which agent role a failure localises to — invoked / drifting / being steered |
| `train_slice_process_profile()` | how runs unfold — task failures, blocks, cancellations, plan revisions |

`restrict_proposer_visibility` exists to stop the proposer memorising "entry 47
wants X", and not to stop it from understanding mechanism. This surface supplies
the mechanism without widening the envelope by a byte.

### The redaction contract

The design invariant is §2.5's: **feed the MARGINAL, never the JOINT.** The
proposer may learn an aggregate property of the *harness's* behaviour; it must
never be able to reconstruct any board entry. Concretely, six commitments, each
independently testable:

1. **Train slice only, derived — never trusted.** No caller passes a slice in.
   Each tool re-derives the partition itself from `workspace_root` + `epoch_id`
   through the same `rotation_seed` → `split_board` pair the tournament runner
   uses, so this surface cannot see a wider slice than the one the round's
   patterns and loss summary were computed over.
2. **Fail closed.** No board, no `scoring.json`, an unparseable either, no
   epoch id, an empty train slice — every failure path returns
   `status: "train slice unavailable"` with a reason and **no data**. There is
   no whole-board fallback; a silently-widened slice is the failure this
   module exists to prevent.
3. **Two independent gates.** Gate 1 opens only train-slice entries' event
   files. Gate 2 (`drop_out_of_slice`) re-filters the collected results by
   entry id afterwards. A single gate is one refactor away from being
   bypassed, because a view that arrives "already filtered" is trusted once
   and then silently is not.
4. **Default-deny reads, with no free-text field admitted at all.** Only a
   narrow allowlist of closed-vocabulary event fields (drift kind, severity,
   steering outcome, intervention level, judge classification) plus harness-side
   agent labels is read; every other payload case, and every unlisted field of
   a listed case, is dropped and its strings join the identity corpus. This
   allowlist is *narrower* than the process-exemplar channel's payload
   allowlist, because that channel is capped at a couple of windows per round
   while this one is queryable on demand. It is what makes
   `run_started.goal_summary` (the task prompt) and every completion summary
   (model output) **structurally unreachable** rather than merely scrubbed.
   The open-vocabulary labels that do survive pass through `scrub_identity`
   then `truncate_free_text`, the free-text truncation and identity-corpus
   scrub primitives shared with the exemplar channel through
   `src/zicato/analyzer/redaction.py`.
5. **Banded, and per-entry incidence rather than per-event counts.** Every
   figure is a rate coarsened through the existing band vocabulary, and each
   entry contributes at most once to each rate, so one chatty run cannot
   dominate a figure and no per-entry magnitude is recoverable. Results are
   ordered by band then name, so neither the value nor the ordering hands back
   a fine-grained response surface (OVERFITTING.md §11).
6. **Stable within a reign.** The champion's event files do not change between
   rounds, so re-asking returns byte-identical answers until the champion
   changes. There is no round-over-round signal to hill-climb — the same
   argument PROCESS-EXEMPLARS.md §2 makes for its refresh semantics.

Entry ids are used to LOCATE files and are never emitted. The tools are
best-effort by contract: a missing file, a malformed line, an unknown payload
are all tolerated, never raised — a diagnostic read must never abort a round.

### Why no query-layer view is exposed

Issue #147 §6 framed this as "expose `zicato/query/` views … through the same
mechanical redaction". No `zicato/query/` view is exposed; every one is
excluded, and the surface is three purpose-built aggregators over the
champion's train-slice `events.jsonl` — the same source `extract_process_exemplars`
reads, differing only in folding events into counts instead of windowing them.

The audit behind that exclusion runs as follows:

> The query layer splits into two halves, and neither half can be redacted
> into this envelope. Its *aggregate* views (`gate_view`, the per-judge
> tables) are keyed by generation with no entry id in the row, so there is
> nothing to filter on: they cannot be narrowed to the train slice, and a
> champion's generation-wide numbers include the holdout runs the promote
> gate played. Its *entry-scoped* views do carry the key, but an entry-keyed
> row is the joint distribution the envelope exists to withhold, and
> filtering one to the train slice still leaves per-entry data in hand. The
> only shape that satisfies both constraints — narrowable to the slice AND
> aggregable to a marginal — is the raw per-run event file, which is
> entry-scoped at the *file path* level and content-free at the *field* level
> once a default-deny allowlist is applied.

A redacted *view* would be a filter argued to be sufficient. A purpose-built
*aggregator* over a default-deny field allowlist is a surface where a leak has
no path to take.

### What is not exposed

The per-view exclusion list:

- **`transcript_view`** (`build_run_transcript`, `resolve_conversation`,
  `empty_run_transcript`) — reconstructs the model's turn-by-turn
  conversation; it *is* task text and model output.
- **`conversations_view`** (`build_matchup_conversations`) — same payload,
  paired per matchup; free-form model output is dropped rather than
  scrubbed.
- **`trace_view`** (`build_trace_detail`, `build_trace_list`,
  `build_suggestion_provenance`) — carries reflection/suggestion prose, which
  quotes board inputs and candidate outputs verbatim.
- **`judge_view` per-judge family** (`build_per_judge_for_generation`,
  `build_per_judge_for_run`, `build_per_judge_trend`,
  `build_per_judge_comparison`) — a judge may be attached to a single board
  entry, so a per-judge figure can be a per-entry measurement presented as an
  aggregate; it is also generation-wide (train plus holdout) with no entry key
  to filter on.
- **`judge_view` per-entry family** (`build_per_entry_for_generation`,
  `build_expectation_outcomes_for_run`, `resolve_run_id_for_entry`,
  `build_run_header`) — an entry-keyed row is the joint distribution rather
  than a marginal; the entry id is the payload.
- **`judge_view` search** (`build_search_results`) — free-text search over
  board/run content; an arbitrary-query read of the corpus is the exact leak
  vector.
- **`gate_view`** (`_drift_counts_for_generation`, `build_gate_breakdown`,
  `build_drift_movements`, `build_score_trajectory`, `build_health_report`,
  `build_rating_view`) — generation-scoped with **no entry key**, so it cannot
  be narrowed to the train slice; the drift counts mix holdout runs in and the
  gate/score views expose the exact holdout-confirmation numbers.
- **`epoch_view`** (`build_epoch_view`, `build_epoch_analysis`,
  `compute_board_split`, `_parse_board`, `_board_input_preview`) — renders the
  board itself: entry ids, input previews, and the train/holdout membership map.
- **`eval_view`** (`build_eval_matrix`, `build_eval_dossier`,
  `build_eval_health`) — per-entry × per-candidate matrix; the joint by
  construction.
- **`tournament_view` / `racing_view` / `rounds_view` / `loop_view`** —
  bracket, matchup and board-slice views keyed by entry id and by
  holdout-confirmed outcomes.
- **`reflection_view`** (`build_judge_scorecards`, `build_adjudication_xray`,
  `entry_candidate_matrix`, `build_practice_review`) — adjudicated per-entry
  evidence, entry-keyed and quoting run content.
- **`run_log` / `log_stream` / `events_index` raw readers** (`build_run_log`,
  `tail_records`, `build_log_view`, `resolve_transcript_events`,
  `read_run_result`) — unredacted event/log passthrough; there is no allowlist
  between them and `run_started.goal_summary`.
- **`journal_view`, `hypothesis_view`, `lineage_view`, `decisions`,
  `runtime_view`, `paths`** — excluded as out of scope rather than as leaks:
  either already reachable through an existing proposer channel (journal,
  prior experiments) or carrying no mechanism signal worth a new surface.

If a future channel needs one of these, the remedy is to build a redacted
aggregate over it, a new marginal, rather than to expose the view and filter
its rows.

### Known limits of the banding

Two caveats apply:

- **On a small train slice the bands are nearly lossless.** `band_rate` rounds
  to 10% steps, so with four train entries `1/4` reads `~30%` and the band
  recovers the count. The memorization resistance on this surface comes mostly
  from the AGGREGATION — per-entry membership sets, so one chatty run cannot
  dominate a figure and no per-entry magnitude survives — rather than from the
  band width. A workspace with a very small board gets correspondingly less
  from the banding.
- **Two exact integers are emitted**, `train_slice_entries` and
  `entries_with_events`. Both are constant within a reign (the split rotates
  per epoch; the champion's files do not change between rounds), and the
  proposer already sees the train run count verbatim in its loss summary
  (`over N runs`). They are not a round-over-round response surface **as long
  as the slice stays per-epoch** — if rotation ever becomes per-round, these
  two need banding too.

### Enforcement

`tests/test_proposer_redacted_query.py` is an **identity-leak probe** rather
than a smoke test. The fixture plants unmistakable sentinel strings as every
board entry id, task prompt, model output, and holdout value — including a
drift `detail` that quotes the task prompt verbatim, which is the case the
identity-corpus scrub exists for — then loops over the module's own exported
`REDACTED_QUERY_TOOLS` tuple. It asserts that no sentinel appears in any
output.
Looping over the tuple rather than a transcribed list covers a newly-added tool
automatically, so no tool can skip the probe. Because a leak probe passes
vacuously when the tools
return nothing, the same file pins the positive content too — the aggregates
must actually be present, and banded, while the sentinels are not.

**`src/zicato/analyzer/redaction.py` is the single source of truth for the
free-text truncation and identity-corpus scrub primitives**
(`truncate_free_text`, `scrub_identity`, `iter_string_leaves`, and their
constants). It has two consumers — the process-exemplar channel and this
surface — and sharing them is what makes both apply byte-identical redaction.
`PROCESS-EXEMPLARS.md` §3 is normative for what the four redaction rules mean.
The payload allowlist and the window-local identity anonymizer stay in
`process_exemplars.py`, because both are bound to the exemplar window's own
structure. Order is load-bearing wherever they are used: **scrub first,
truncate second** — truncation only removes
characters and puts the elision marker between head and tail, so a scrubbed
string can never re-form an identity across the split.

Two notes for a future editor:

- **Gate 2 (`drop_out_of_slice`) catches nothing on any current path, and that
  is the point.** Gate 1 — opening only train-slice event files — is
  sufficient on every path. Gate 2 exists for the refactor where someone
  routes in a result set that arrives "already filtered" and is trusted once.
  Its test feeds it a row gate 1 could never produce, so that it stays alive
  independently. **Do not delete it as dead code.**
- **`_load_events` / `_payload` are duplicated** (about 30 lines) between
  `process_exemplars.py` and `redacted_query.py`. The duplication is
  intentional: the two allowlists differ, and a self-contained parser inside
  the security-critical module is preferred to a shared one. A consolidation
  is a reasonable follow-up, but it must move the *whole* reader rather than
  half of it, because a shared parser paired with a per-consumer allowlist
  invites the wrong allowlist being applied.

A **parameterized drill-down** (a per-drift-kind × agent cross-tab) is a
plausible extension and is not built. A zero-arg tool has no input to validate
and therefore no oracle surface, and a provably clean surface is worth more
than the extra resolution. Revisit it only with the widened envelope documented
before the code.

---

## 3. Why a tool-using proposer owns its own model

The text shim is a single-shot text exchange: zicato hands the auxiliary
callable a `(system, user, model) -> str` prompt and parses the returned
string. **That shim cannot express the function-calls a tool-using agent
needs** — it is text-in / text-out by contract. A proposer that wants to grep
the mutable surface or consult the journal *while it reasons* cannot run on it.
That is why the default proposer (§2a) and any custom `agent.py` (§2c) are ADK
agents rather than text-shim calls.

zicato resolves this by running a tool-using proposer as a **native ADK
agent that declares its own `model=`**, driven on ADK's own `Runner` — NOT
through the auxiliary text shim, and NOT through `goldfive.run`. Three
consequences follow:

- **The agent owns the model.** The agent's `model=` is its own; the
  `--auxiliary-call-llm` callable does not govern it. The per-round task (brief
  + skills + mutation manifest + patterns + loss + prior experiments + the
  JSON-schema demand) is delivered as the agent's run *input* — the agent owns
  its own static instruction (how to work), zicato owns the input (what this
  round is). The built-in default agent's `model=` is bound, per round, to the
  workspace's auxiliary model string; a custom `agent.py` declares its own.
- **A custom proposer's model should differ from the harness model.** Because
  the proposer runs on its own model rather than the shared auxiliary callable,
  the `is`-identity collusion guard
  (`assert_distinct_callables`) does not apply here. The model-distinctness is
  instead a **documented author responsibility**: a proposer scored on the same
  model it is mutating-and-judging risks collusion. When both model strings are
  trivially discoverable zicato emits a soft WARNING on a match; it does not
  build a hard gate. The **built-in default reuses the auxiliary model**, so
  the warning is skipped for it; that reuse is the expected zero-configuration
  posture rather than an author error.
- **The post-response loop is shared.** The agent's final message goes through
  the same parse → forbidden-id enforcement → post-apply validation loop, with
  the JSON salvage/repair and judge-reference normalization in
  `parse_experiment_json` applying identically; a retryable failure feeds its
  feedback into the next run's input, within the same bounded budget. The
  text-shim path (§2b) and both ADK paths therefore share one robustness
  surface.

Every `google.adk` import on this path is lazy, so importing the proposer
modules never forces the optional `google-adk` extra; the extra is pulled in
only when an ADK agent (the built-in default, or a custom `agent.py`) is
actually built at the first `propose`. In practice a live `evolve` already
requires the `adk` extra — the only shipped harness adapter is itself an ADK
agent — so making the default proposer an ADK agent adds no new dependency to
the live path; the text-shim path (§2b) remains usable without it.

---

## 4. Contract / epoch-roll mechanics

The proposer is folded into the contract hash by `_canon_proposer`
(`zicato/epoch/contract.py`). It resolves the proposer dir (or `None` ⇒ the
builtin default) to a `ProposerSpec` via `resolve_proposer_spec`
(`zicato/proposer/skills.py`) and serializes it sorted-key:

- `agent_id` — `"builtin:default"`, `"dir:<name>"`, or `"external:<label>"`;
- `tools` — the tool names, sorted;
- `skills` — `[{name, sha256-of-normalized-body}]`, sorted by name. Skill
  bodies are normalized in the same way as the proposer brief (line endings folded,
  trailing whitespace stripped, leading/trailing blank lines dropped), so a
  whitespace-only skill edit does **not** roll the epoch; a semantic edit — or
  adding / removing / renaming a skill — does;
- `agent_source_sha256` — SHA-256 of a custom `agent.py` (or `null`), so
  editing the custom agent rolls the epoch;
- `external` — present **only** when `runtime.proposer_agent` is
  configured: the dotted path plus the digest of the external agent's
  causal surface (§2.9). Adding the key only when it applies is what keeps
  every other workspace's canonical form, and therefore its hash,
  unchanged.

The builtin default produces a stable canonical string, so a workspace that
never registers a proposer keeps a stable hash. The per-component roll message
(`compute_component_hashes`) names the changed component **`proposer`**, so a
roll triggered by a proposer edit names that component to the operator.

> **Note.** The builtin-default *spec* (`ProposerSpec.default()`)
> canonicalizes as `agent_id = "builtin:default"`, empty tools and skills, and
> a `null` `agent_source_sha256`. Which agent backs that spec
> (`build_proposer_agent` → the tool-using `ADKProposerAgent`) is a runtime
> resolution rather than a contract input, so it never enters an epoch's
> hash.

This composes with the brief: the **proposer brief** is per-epoch *operator
guidance* (steering text the proposer reads fresh each round), while the
**proposer** is the *agent + its skills* that consume it. They are distinct
contract inputs; either rolling the epoch is independent of the other.

---

## 5. Configuring it — `register --proposer-path`

`zicato epoch register --proposer-path PATH` records `contract.proposer_path` in
`.zicato/config.json` (absolutised, like the other contract source paths).
`resolve_contract_inputs` reads it back on every `evolve`, resolves a relative
spelling against the project root (the workspace's parent), and feeds it into
the contract hash *before* the hash is computed. Registering a proposer dir
therefore rolls the epoch on the next `evolve`, in the same way as editing the
brief. Omitting
the flag leaves the key unset, which resolves to the builtin default proposer
(`None`).

A `config.json` sketch (other keys elided):

```jsonc
{
  "adk_entrypoint": "my_pkg.agent:root_agent",
  "mutable_trees": ["src/my_pkg"],
  "contract": {
    "board_path":   "/abs/board.jsonl",
    "rubric_path":  "/abs/brief.md",       // the brief, under its on-disk key
    "scoring_path": "/abs/scoring.json",
    "proposer_path": "/abs/proposers/fancy" // OPTIONAL — absent ⇒ builtin default
  }
}
```

Derive the flag surface from `zicato epoch register --help`, which is
canonical; the design CLI documentation drifts. The flag is
`--proposer-path PATH`.

---

## 6. The proposer scorecard + recommend-only self-reflection

The loop measures the proposer on every round at no extra cost. Every round
log records the proposal attempts it made, the validator errors they hit, the
screen's verdict on each slate candidate, the gate's numbers on the child that
reached it, and the terminal decision. The scorecard (§6.1) reads those signals
as a picture of proposer quality; reflection (§6.2) is the gated path for
acting on it.

### 6.1 The scorecard (`zicato proposer scorecard`)

`zicato/proposer/scorecard.py` is a **pure reader** — it opens round logs,
epoch configs, and per-generation `experiment.json` files, and writes nothing.
One card per epoch, because the proposer is frozen for its epoch (§4): two
epochs' proposals came from two different proposers and are not comparable.

| Aggregate | Read from |
|---|---|
| `validator_failure_rates` (A1–A4 + `unclassified`) | `proposal_attempted.errors`, classified by the code `validate_post_apply` stamps |
| `validation_failure_rate` | the same, any-check |
| `screen_veto_rate` | `candidate_screened.vetoed` over `candidate_screened` |
| `revision_success_rate` | the `revise` screens that were not vetoed |
| `margins` | `gate_evaluated`'s `champion_scalar` / `challenger_scalar` / `margin_required` |
| `cost` | proposal attempts + `unit_completed` count, per promoted round |
| `mutation_sites` | each round's child `experiment.json` patch `mutation_id`s |

Three honesty rules are structural:

- **Null is not zero.** `Rate.value` is `None` when nothing was observed. A
  proposer that never had a candidate screened has *no* screen-veto rate;
  rendering `0.0` would claim it screened plenty and vetoed none.
- **The sample count rides every rate.** `n` is in the dataclass, in `to_json`,
  in the CLI table, and in the panel, so no surface can show a rate without it.
- **Thin samples are marked.** Under `MIN_SAMPLE_N` a rate is `provisional`
  (a `?` in the CLI and the panel) — reported, because suppressing it loses
  information, but flagged.

**Post-apply classification is structural.** `validate_post_apply` prefixes each
error string with its check code (`A4: Post-apply file … dropped top-level
imports: …`) and `classify_post_apply_error` is the one reader of that prefix.
The prose after the code stays free to reword; nothing regexes the sentence.
An error carrying no recognised code counts under `unclassified` — the honest
bucket for a proposer parse failure, a slate slot's credential lapse, or a log
written before the codes existed.

### 6.2 Reflection (`zicato proposer reflect`) — recommend-only

`zicato/proposer/reflection.py` diagnoses the scorecard and drafts the edit:
each finding carries the five-slot evidence convention (population, measured,
compared-against, remedy, remedy-safety) where the **remedy is a ready-to-apply
`skills/*.md` file plus its unified diff and SHA-256**. Records land under
`epochs/<id>/proposer_reflections/<id>/findings.json`.

The **investigation substrate is pluggable**: `InvestigationSource` returns an
`Investigation`, and `ScorecardInvestigation` reads the scorecard plus a
BANDED history of prior epochs. A richer substrate — the redacted query
surface (§2.11) — implements the same protocol and returns the same
`Investigation`, so it drops in without reshaping a persisted record. Historical
rates are banded through `band_rate` for the same reason the failure-mode
channel bands its marginals (§2.5): the comparison slot is the one number a
drafting model reads round over round, and the exact rate would be a response
surface to climb.

Emission is **deterministic and free** — no model is called, so the operator's
queue is reproducible from the same round logs. `--draft-with-llm` adds an
optional polish pass over the remedy's prose through the auxiliary-call seam,
wrapped in `aux_call_timeout_s` like every other aux call site; a failed,
timed-out, or empty call keeps the deterministic remedy rather than degrading
it. The budget matters more here than elsewhere: the remedy is already complete
before the model is asked anything, so a pass that blocked on a dead endpoint
would be waiting for nothing.

### 6.2.1 Two round-log subtleties the reader must respect

**Re-run rounds.** One `round_log.jsonl` can hold more than one attempt at the
same round index — a round that applied patches but died before its experiment
was written never consumes its index, so the next invocation reopens it and
appends. The two families of aggregate therefore take *opposite* slices:

- **gate / decision / generation / unit facts** come from the FINAL attempt
  span only (the same slice `round_integrity._final_attempt_span` takes), because
  a dead attempt's gate is not the round's outcome and counting it could credit a
  second promotion to a round the epoch settled once;
- **proposal-failure and cost facts** come from EVERY attempt, because a failed
  attempt is not noise to be sliced away — it is the signal. Slicing it would
  make the failure rate improve when the proposer did worst, and a call spent
  on an attempt that later died was still spent.

**Revision success.** `ProposalSession` carries no revise counter and folds the
revise's veto in with the slate's, so the rate is read from raw envelopes. A
re-sample that survives the screen is the one the selector then picks
(`critique_selected.reason == "screen_revise_survivor"`), so the screened verdict
and the definitive token agree. The denominator counts re-samples that *produced*
a candidate: a revise whose propose call raised emits no `candidate_screened` at
all, so the rate answers "when the revise produced something, did it survive"
rather than "did the revise mechanism work at all".

### 6.3 The four invariants, and where each one lives

| Invariant | Mechanism |
|---|---|
| **Never mid-epoch** | The only writer into the proposer dir is `apply_recommendation`, and its edit is contract drift, so the next `evolve` rolls the epoch before proposing. |
| **Never self-applied** | There is no import edge from `reflection.py` to `apply_recommendation.py`; a test reads the module source to pin the absent edge. |
| **Redacted evidence only** | `assert_redacted` walks every record at the persist boundary and RAISES on an identity/content key at any depth. The scorecard never carries an `entry_id` by construction (it counts units and ignores `attributable_regressions`); the guard is what keeps a future emitter honest. |
| **Every accepted edit is hashed** | The remedy carries the SHA-256 of the exact bytes; `apply-recommendation` re-verifies before writing, so an edited record cannot be applied under its original id. |

One consequence of that last row: `_canon_proposer` folds a skill as
`{name, sha256(normalized body)}` and does **not** hash the frontmatter
`description`, so rewording a description is cosmetic and correctly does not
roll. A drafted remedy is safe from that because its heading and its
evidence line live in the *body* — but only by layout, so both halves are pinned:
a description-only edit must not roll (the canon's semantics, which this feature
must not drift), and a drafted remedy's replacement must.

### 6.4 The boundary and the apply gate

Pending recommendations are printed at both epoch boundaries — evolve's
auto-roll and `zicato epoch new` — because that is the moment applying one is
free: the epoch is rolling anyway.

`zicato proposer apply-recommendation <id>` writes the skill into the LIVE
proposer dir and parks the id in `proposer_staged.json`. The write **rolls the
contract hash** (skills fold into `_canon_proposer`, §4), so the next `evolve`
opens a fresh epoch — and `new_epoch` drains the queue into that epoch's
`applied_proposer_recommendations`, which is proposer lineage: the record says
*why* the proposer changed. The staged queue exists because apply cannot know
which epoch will pick its edit up; the epoch that actually runs under the
edited proposer claims it.

`applied_proposer_recommendations` is a RECORD about the epoch, never a
contract input — it does not fold into the contract hash. The edit it names
already rolled the hash on its own.

---

## 7. Cross-references

| Topic | Document |
|---|---|
| The epoch as the frozen contract; auto-roll on contract change; the proposer in the contract-input list | [EPOCHS-AND-JOURNALING.md §10](EPOCHS-AND-JOURNALING.md#10-contract-hash-auto-epoching), `src/zicato/epoch/contract.py` |
| Operator-facing: when skills suffice vs a custom agent; how to add a skill; the epoch-roll discipline | `skills/zicato-design-proposer/SKILL.md` |
| The mental model for epochs / rounds / the contract | `skills/zicato-manage-epochs-and-rounds/SKILL.md` |
| The proposer brief (per-epoch operator steering) vs the proposer (the agent) | [EPOCHS-AND-JOURNALING.md §1](EPOCHS-AND-JOURNALING.md#1-epoch-concept), `skills/zicato-write-brief` |
| The hypothesis schema the proposer must emit | [EPOCHS-AND-JOURNALING.md §3](EPOCHS-AND-JOURNALING.md) |
| Selection / tournament the proposer feeds | [SELECTION.md](SELECTION.md), [TOURNAMENT-STRUCTURES.md](TOURNAMENT-STRUCTURES.md) |
| The copy-me tool-using proposer agent | [`examples/zicato_examples/proposer_with_tools/agent.py`](../../examples/zicato_examples/proposer_with_tools/agent.py) |
| The failure-mode feedback channel — anti-leakage (train-slice, banded, identity-free) | [OVERFITTING.md §11](OVERFITTING.md), `src/zicato/analyzer/outcome_marginals.py` |
| The `zicato epoch register` command reference | [CLI.md](CLI.md) |
| The post-apply check codes the scorecard classifies on (§6.1) | [MUTATION-SURFACE.md](MUTATION-SURFACE.md), `src/zicato/mutation/validator.py` |
| The recommend-only reflection pattern this mirrors (findings, five-slot evidence, apply-to-a-draft) | [BOARD-REFLECTION.md](BOARD-REFLECTION.md), `src/zicato/reflection/findings.py` |
| The redaction envelope the reflection substrate reuses | [OVERFITTING.md §11](OVERFITTING.md), §2.5 above |
