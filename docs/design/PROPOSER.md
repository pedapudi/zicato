# The proposer — a first-class evaluation-contract input

> **Status.** SHIPPED. The proposer is folded into the contract hash
> (`zicato/epoch/contract.py`), the resolution
> (`zicato/proposer/{skills,agent,adk_agent,tools}.py`) is in the tree and
> exercised by the test suite, and the operator surface
> (`register --proposer-path`) is wired. The **DEFAULT proposer is the
> tool-using ADK agent** (`build_default_adk_agent`, run by
> `ADKProposerAgent`), used whenever a contract configures no proposer dir;
> the skill-composed text-shim engine remains available as an explicit
> opt-in (§2b). §§1–2 describe the shipped design; §3 is the Design-A
> rationale; §§4–5 are the contract/config mechanics. Operator-facing how-to
> lives in the `zicato-design-proposer` skill.

The **proposer** is the agent that, each round, reads the epoch's brief, the
mutation manifest, the loss patterns, and the prior experiments, and emits the
next `Experiment` (`{hypothesis, patches}`) for the tournament to judge. For
most of zicato's life the proposer was an implicit, fixed component. It is now
a **first-class evaluation-contract input**, alongside the board, the proposer
brief, the scoring, and the inner-harness identity
([EPOCHS-AND-JOURNALING.md §10.1](EPOCHS-AND-JOURNALING.md#101-whats-in-the-contract)).

This document is the design + reference companion. The defining decision,
repeated because it is load-bearing: **the proposer is contract, not
configuration.** A different proposing agent — or a different skill in its
prompt — proposes different mutations and reasons differently, so generations
proposed under different proposers are not directly comparable. Changing the
proposer therefore **rolls the epoch**, exactly like changing the board or the
scoring (§4).

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

A workspace points at a proposer dir with `zicato register --proposer-path
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
by the orchestrator). It runs on ADK's own `Runner` (NOT the auxiliary text
shim, which cannot express the function-calls a tool-using agent needs —
§3), so the default proposer can grep the mutable surface, read the parent
snapshot, and consult the journal / analyzer insights *while it reasons*,
out of the box. Because the default deliberately uses the operator's
already-configured auxiliary model, the model-collusion smell test (§3) is
skipped for it — that is the documented, expected posture, not an author
error.

### (b) Skill-composed default — drop `skills/*.md`, no code (opt-in)

The cheapest *customization*. Drop one or more `skills/*.md` into
`proposers/<name>/skills/` and configure the dir; do **not** write an
`agent.py`. zicato runs the built-in `DefaultProposerAgent`, a single-shot text
exchange driven on `--auxiliary-call-llm`: the auxiliary callable is handed a
`(system, user, model) -> str` prompt and the returned string is parsed into
the `Experiment`. Your skill bodies are injected into the **system prompt**, so
they steer *how* this proposer reasons (grounding instructions, house
style, a checklist) without any code. Configuring a proposer dir (without an
`agent.py`) is the explicit opt-in into this single-shot text-shim engine;
the bare, unconfigured default (a) is the tool-using agent.

This is the right tier when you want to shape the proposer's reasoning over
the text shim, not give it new capabilities. The model is the auxiliary
model — you do not own it here.

### (c) Custom ADK agent with tools — `agent.py`

When you want to *own the model* the proposer runs on, give it a curated
tool subset, or write a bespoke instruction, ship a
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
insights. That input now also carries a **failure-mode profile** — a compact,
board-anonymized read of *why* the parent's answers were wrong, not just
*that* a scalar moved.

Historically the proposer saw only a coarsened `Δscalar` plus an LLM digest of
*decision* telemetry; it could not target over-retrieval vs misses vs
empty/looping answers. The channel
(`src/zicato/analyzer/outcome_marginals.py`, rendered by
`render_failure_mode_profile` in `src/zicato/proposer/prompts.py`, wired by
`_render_failure_profile` in `src/zicato/orchestrator.py`) closes that gap by
feeding the proposer **outcome MARGINALS** — board-wide rates — under three
non-negotiable safeguards, each reusing existing machinery:

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
renderer returns the empty string — the proposer prompt stays byte-identical
to before, exactly as the insights / prior-experiments blocks behave.

### Operator hook — `outcome_summarizer_spec`

An OPTIONAL operator summarizer can contribute **board-specific** marginals.
It is a dotted spec — `outcome_summarizer_spec` on the scoring contract
(`ScoringWeights`, `src/zicato/core/types.py`) — resolved exactly like
predicates / judges (`zicato.import_path.import_dotted_path`). The resolved
callable receives the train-slice per-entry results and must return a
**STRUCTURED aggregate** — a `{marginal_name: numeric_rate}` mapping, NOT
prose — precisely so zicato can ENFORCE bucketing + anonymity on its output:
`sanitize_operator_marginals` drops anything non-numeric or identity-bearing
(a free string, an entry id as a key, a list/dict value) before the operator's
marginals are merged and banded. A free-text summary would be an un-auditable
leak vector and is rejected by construction; a misbehaving or raising
summarizer contributes nothing rather than aborting the round.

Because it lives on `ScoringWeights`, the spec folds into the scoring
component of the contract hash automatically: configuring or changing the
summarizer rolls the epoch, exactly like every other contract field
(the empty-string default configures no summarizer, leaving the prompt
unchanged). The shaped numbers that band these marginals are part of the
scoring surface and are owned by SCORING.md — this doc describes only the
*channel*, not the scalar arithmetic.

---

## 2.6 The mechanical recombination slot (WS-REC)

`proposer_quality.recombine` (default OFF) opts in a MECHANICAL merge of two
already-evaluated challengers — no LLM call. The premise: a single champion can
only ever discount ONE challenger's fix, so when two REJECTED challengers of the
current reign each fixed a DISTINCT slice of the board with NON-OVERLAPPING
edits, the last best-of-N slate slot mints the UNION of their patches, and a
non-vetoed mint is chosen with `selection_mode = "recombined"`. A
parsimony-biased selector rejects each single fix; the union clears the gate
that neither half could — so the slot deliberately bypasses the minimal-diff
selection heuristic (whose diff key would otherwise starve the larger union).

It is cost-neutral (the mint REPLACES the slot's auxiliary propose call, never
adds one — a recombining round spends `best_of_n − 1` calls) and
envelope-clean: selection runs on per-entry PASS-FLIP evidence computed
orchestrator-side and intersected with the TRAIN board there — entry ids never
reach the proposer, and the holdout is never eligible. Requires `best_of_n > 1`
to have any effect; flipping it rolls the epoch (a slate that can recombine
proposes under a different rule). The full mechanism — the 8 eligibility
predicates, the 4-key deterministic ranking, the minter, the `recombined`
selection mode, and the KNOWN NARROWING (pure drift-side complementary pairs are
invisible by design; they remain reachable through the in-context genealogy
channel, with a drift-delta-with-confirmation variant as a documented future
seam) — is specified in **[dev-guide 05 §5.6.11](../dev-guide/05-proposer.md)**
(`src/zicato/epoch/recombine.py`, `src/zicato/proposer/recombine.py`).

### 2.6.1 Merge modes — `mechanical` (default) vs `llm`

`proposer_quality.recombine_merge` (a string, default `"mechanical"`; values
`"mechanical" | "llm"`) chooses HOW the slot composes the union once the
selector has picked a pair. It is meaningful only when `recombine` is on and
`best_of_n > 1`; at its `"mechanical"` default it is omitted from the contract
canonical form (byte-identical hash, no retroactive roll), and `"llm"` rolls
the epoch — a slate that can compose an LLM merge proposes under a different
rule. A `"llm"` value set with `recombine` off is accepted-and-inert (the
dependent-knob house style, as `screen_veto_only` is inert without
`screen_entries`); it still rolls the hash, exactly like an inert
`screen_veto_only`.

- **`mechanical`** — the shipped WS-REC behaviour of §2.6: the last slot MINTS
  the concatenation of the two patch sets with NO LLM call. It REQUIRES a
  DISJOINT pair (predicate #7): the applier is last-wins on a duplicate
  target, so overlapping edits would silently drop one side. Cost: a
  recombining round spends `best_of_n − 1` propose calls (the free mint
  replaces the slot's own sample call).

- **`llm`** — the last slot issues ONE auxiliary call (the depth
  refinement-class role, exactly as the self-critique call) rendering a MERGE
  prompt from the selected pair; the response flows through the NORMAL
  proposal parse + `enforce_forbidden` + validate path — it is a proposal like
  any other — is stamped with the same `recombined_from` provenance, and a
  non-vetoed merge is chosen with the same `selection_mode = "recombined"`. On
  any parse/validate failure the slot DEGRADES to a fresh LLM sample (the
  mechanical mint's exact degrade). This mode exists to reach the pairs
  mechanical mint cannot: when two rejected fixes OVERLAP on a mutation target,
  a model can compose a genuine merge (resolving the shared edit) that a
  last-wins concatenation cannot.

**What relaxes, and what never does.** Only predicate #7 (disjointness)
relaxes, and only for PAIR SELECTION in `llm` mode — the model resolves the
overlap, which is the whole point. Overlap does not vanish: it becomes a
RANKING consideration (prefer LESS overlap at equal coverage), slotted into the
existing deterministic key immediately after coverage so that mechanical-mode
selections — where every surviving pair has zero overlap — are byte-identical
to before. Every OTHER predicate holds unchanged in both modes: #1 rejected,
#2 current reign, #3 non-placebo, #4 non-recombined parent, #5 pair-not-tried,
#6 manifest-valid patches, and #8 complementarity ESPECIALLY (each parent must
still carry a distinct win the other lacks — an LLM merge of two identical
fixes is nothing).

**The envelope (LOAD-BEARING).** The merge prompt carries ONLY
proposer-authored artifacts, exactly the genealogy-channel redaction
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

One semantic note: an `llm` merge is **a full proposal, not strictly a
union** — the response validates against the whole mutation manifest, so it
may touch points neither parent did. `recombined_from` records the pair that
*seeded* the merge; only the mechanical mint guarantees the patch set is
exactly the parents' union. The gate adjudicates either way.

**Cost.** `mechanical` spends `best_of_n − 1` propose calls (the mint is free).
`llm` spends `best_of_n` on the happy path: the merge call SUBSTITUTES the
slot's own sample call, so a successful `llm`-merge round costs what a
recombine-OFF round costs. The one exception is the degrade: a merge response
that fails parse/validation has already spent its call, and the fallback
fresh sample adds one more (`best_of_n + 1` for that round — rare, and the
reason the `estimate_cost` figure is documented as an estimate, not a cap).

---

## 2.7 The genealogy channel (WS-GENE) — in-context evolution, envelope-safe

`proposer_quality.genealogy` (an `int`, default `0` = OFF) opts the proposer
into an IN-CONTEXT view of the current reign's candidate lineage — the
zicato analogue of AlphaEvolve's *prompt sampler*, which feeds parent
programs and their scores back into generation so the LLM evolves in
context. Where the mechanical recombination slot (§2.6) merges two rejected
fixes WITHOUT an LLM call, the genealogy channel gives the LLM the raw
material to merge, extend, or diverge from what has already been tried —
the same in-context recombination, but authored by the model, and reachable
even for the pure-drift-side pairs the mechanical slot cannot see (the §2.6
KNOWN NARROWING). It is a RENDER-SIDE channel only: it splices a prompt
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
  landed, the rejection says the gate declined to promote it. That is exactly
  the signal the proposer wants — "this framing moved the needle but did not
  clear the bar" — carried coarsely, without a number.

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
   memory already uses (§2.5; OVERFITTING.md §11.4). The exact
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
evaluation data**: it is candidate genealogy, not board data.

### The banding vocabulary

Reuse, do not reinvent. The whole-candidate outcome is banded through
`_bucket_scalar_delta` (`src/zicato/proposer/prompts.py`) — the exact
`improved` / `flat` / `regressed` three-band vocabulary the prior-experiments
block already renders under `restrict_visibility`. A candidate with no
settled Δscalar (an in-flight sibling — never sampled here, but defensively)
renders no band. No new banding primitive is introduced; a reader who knows
the experiment-memory bands reads genealogy with no new vocabulary.

### The cap discipline

Budget-capped rendering, exactly the process-exemplar cap style (§2.5):
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
sentinel — so a `genealogy = 0` round is byte-identical to today.

### The determinism requirement

The sampler is a PURE, DETERMINISTIC function of (the reign's records, the
ratings, `k`) — **no RNG**. Parents sort by round (the spine order);
inspirations are the greedy max–min-Jaccard walk with a TOTAL tie-break
(Elo DOWN, then generation-id ascending) so the same pool always yields the
same inspirations in the same order, in ANY input order. Determinism is the
leakage budget: a byte-identical block round-over-round (while the reign's
candidate set is unchanged) re-presents nothing new, exactly as the
process-exemplar channel argues.

The full mechanism — `GenealogyItem`, `sample_genealogy`, the greedy
dissimilarity walk, the render block, and the `genealogy` knob — is
specified in **[dev-guide 05 §5.6.13](../dev-guide/05-proposer.md)**
(`src/zicato/proposer/genealogy.py`).

---

## 2.8 The critic-calibration channel (WS-CAL) — feeding prediction accuracy back

`proposer_quality.calibration_feedback` (an `int`, default `0` = OFF) opts the
proposer into an IN-CONTEXT view of ITS OWN PREDICTION CALIBRATION — how the
falsifiable movement predictions it wrote in past hypotheses actually landed
against realized outcomes. The prediction-accuracy grader
(`hypothesis_ledger` / `grade_hypothesis_predictions` in
`src/zicato/tournament/detail.py`, surfaced by the `/api/hypothesis-accuracy`
dashboard feed) already scores every settled hypothesis's predicted-vs-realized
movements, but that score has been CONSUMPTION-ONLY — a dashboard diagnostic
the proposer never saw. This channel closes the loop: a proposer shown its own
MISS PATTERN hypothesizes more honestly — it stops writing confident,
un-earned predictions once it can see that its confident predictions have been
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
  wrong. Strict-all-match for a hit is deliberate: it rewards conservative,
  well-earned prediction over confident over-claiming.
- **unresolved** — the hypothesis made NO gradeable predictions
  (`predictions == 0`), so calibration is silent on it. (Matches the
  experiment-memory reader's "None accuracy = made no graded predictions.")

One rendered-block corollary worth knowing when reading it: the grade and
the banded outcome are INDEPENDENT axes, so a claim can render
`HIT · Δscalar regressed` — every specific prediction verified while the
candidate's overall scalar still worsened. That is honest, not a bug: the
grade measures forecasting skill, the band measures the outcome.

### What the channel carries

A per-reign calibration summary, rendered into the proposer context:

- **Per-claim-type COUNTS** — the hit / miss / unresolved tallies over the
  reign's settled hypotheses.
- **The overall calibration fraction** — `hit / (hit + miss)`, the fraction of
  the proposer's GRADED claims it called correctly. This is the proposer's OWN
  self-accuracy meta-signal, pooled over its own predictions — never a board
  number. Climbing it means predicting more honestly, which is precisely the
  behaviour the channel exists to encourage; it is a calibration target, not a
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
the renderer emits the EMPTY STRING — the "omit this section entirely"
sentinel — so a `calibration_feedback = 0` round, and any round with no graded
claims, renders a byte-identical prompt to today.

### The envelope (LOAD-BEARING)

Stated as hard exclusions, exactly the genealogy vocabulary (§2.7):

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
proposer canonicalizes byte-identically to before this seam existed — its
contract hash does not move, which is pinned in
`tests/test_proposer_external_seam.py`.

What belongs in that identity: the version of the runtime we did not write
(coarse — a patch release that changes no prompt and no tool schema should
not roll an epoch, and the standing rule *do not upgrade mid-tournament*
carries the rest), the bytes of the files we did write (they are edited in
place, so they have no version to record), the tool set, and the launch
envelope. What does **not** belong: the model. A `models.*` role is
runtime infra that has never rolled an epoch, and nothing in the contract
hash has ever named a model. The collusion hazard an external tier
introduces — an agent quietly falling back to its own configured default —
is closed where it happens, at launch: the resolved `ctx.model` is threaded
into the process and an empty one is a hard failure, asserted in
`tests/test_proposer_pi_envelope.py`.

**The first implementation is pi** (`zicato/proposer/pi_agent.py`,
`integrations/pi/`): one `pi --mode rpc` subprocess per challenger, driven
through the *same* `propose_experiment` engine the text shim uses — the
live RPC session is handed to it as the `aux_call_llm` callable, so a
bounded retry becomes a follow-up message on a warm conversation instead
of a cold restart that re-sends the whole manifest. The tier differs in
its transport, not its semantics, which is what makes it an honest A/B
baseline against the shim.

The envelope is the other half, and it is enforced by what the proposer is
shown. A default coding-agent session has `bash`, `read` and `grep`
pointed at the working directory; a proposer with those can read the board
and the holdout slice, and nothing errors and nothing warns. So: built-in
tools off, extension/skill/prompt-template discovery off, context files
off, project-local files untrusted, a fresh isolated agent directory with
credentials copied in deliberately (no packages, no cross-round memory),
no session file (cross-round persistence would be an unhashed side channel
around the overfitting envelope), and a working directory outside every
snapshot — the snapshot is the system under test, and reading it ambiently
would be both an unhashed contract input and an injection path from the
thing being rewritten into the thing rewriting it. CI asserts the running
agent's active tool list equals the sanctioned set.

---

## 3. Design A — why a tool-using proposer owns its own model

The text shim is a single-shot text exchange: zicato hands the auxiliary
callable a `(system, user, model) -> str` prompt and parses the returned
string. **That shim cannot express the function-calls a tool-using agent
needs** — it is text-in / text-out by contract. A proposer that wants to grep
the mutable surface or consult the journal *while it reasons* cannot run on it.
That is precisely why the DEFAULT proposer (§2a) and any custom `agent.py`
(§2c) are ADK agents, not text-shim calls.

**Design A** resolves this by running a tool-using proposer as a **native ADK
agent that declares its own `model=`**, driven on ADK's own `Runner` — NOT
through the auxiliary text shim, and NOT through `goldfive.run`. The
consequences, all deliberate:

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
  build a hard gate. The **built-in default deliberately reuses the auxiliary
  model**, so this smell test is skipped for it — that reuse is the expected
  zero-config posture, not an author error.
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
  bodies are normalized exactly like the proposer brief (line endings folded,
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
(`compute_component_hashes`) names the changed component **`proposer`**, so when
a roll is triggered by a proposer edit the operator sees exactly that.

> **Note.** The builtin-default *spec* (`ProposerSpec.default()`) is
> unchanged by the default-agent flip — the contract canonicalization still
> serializes `agent_id = "builtin:default"`, empty tools/skills, and a `null`
> `agent_source_sha256`. The choice of *which agent backs* that spec
> (`build_proposer_agent` → the tool-using `ADKProposerAgent`) is a runtime
> resolution, not a contract input, so flipping the default does **not** roll
> any existing epoch.

This composes with the brief: the **proposer brief** is per-epoch *operator
guidance* (steering text the proposer reads fresh each round), while the
**proposer** is the *agent + its skills* that consume it. They are distinct
contract inputs; either rolling the epoch is independent of the other.

---

## 5. Configuring it — `register --proposer-path`

`zicato register --proposer-path PATH` records `contract.proposer_path` in
`.zicato/config.json` (absolutised, like the other contract source paths).
`resolve_contract_inputs` reads it back on every `evolve`, resolves a relative
spelling against the project root (the workspace's parent), and feeds it into
the contract hash *before* the hash is computed — so registering a proposer dir
rolls the epoch on the next `evolve`, exactly like editing the brief. Omitting
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

Derive the exact flag surface from `zicato register --help` (the design CLI
docs are known to drift); as of writing the flag is `--proposer-path PATH`.

---

## 6. The proposer scorecard + recommend-only self-reflection

The loop measures the proposer constantly, for free. Every round log records
the proposal attempts it made, the validator errors they hit, the screen's
verdict on each slate candidate, the gate's numbers on the child that reached
it, and the terminal decision. This section is the instrument that READS those
signals as a picture of proposer quality, and the gated path for acting on it.

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

Three honesty rules are structural, not stylistic:

- **Null is not zero.** `Rate.value` is `None` when nothing was observed. A
  proposer that never had a candidate screened has *no* screen-veto rate;
  rendering `0.0` would claim it screened plenty and vetoed none.
- **The sample count rides every rate.** `n` is in the dataclass, in `to_json`,
  in the CLI table, and in the panel, so no surface can show a rate without it.
- **Thin samples are marked.** Under `MIN_SAMPLE_N` a rate is `provisional`
  (a `?` in the CLI and the panel) — reported, because suppressing it loses
  information, but flagged.

**A1–A4 classification is structural.** `validate_post_apply` now prefixes each
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
`Investigation`, and v1's `ScorecardInvestigation` reads the scorecard plus a
BANDED history of prior epochs. A richer substrate — the redacted query
facility of #147 phase 5 — implements the same protocol and returns the same
`Investigation`, so it drops in without reshaping a persisted record. Historical
rates are banded through `band_rate` for the same reason the failure-mode
channel bands its marginals (§2.5): the comparison slot is the one number a
drafting model reads round over round, and the exact rate would be a response
surface to climb.

Emission is **deterministic and free** — no model is called, so the operator's
queue is reproducible from the same round logs. `--draft-with-llm` adds an
optional polish pass over the remedy's prose through the auxiliary-call seam; a
failed or empty call keeps the deterministic remedy rather than degrading it.

### 6.3 The four invariants, and where each one lives

| Invariant | Mechanism |
|---|---|
| **Never mid-epoch** | The only writer into the proposer dir is `apply_recommendation`, and its edit is contract drift, so the next `evolve` rolls the epoch before proposing. |
| **Never self-applied** | There is no import edge from `reflection.py` to `apply_recommendation.py`; a test reads the module source to pin the absent edge. |
| **Redacted evidence only** | `assert_redacted` walks every record at the persist boundary and RAISES on an identity/content key at any depth. The scorecard never carries an `entry_id` by construction (it counts units and ignores `attributable_regressions`); the guard is what keeps a future emitter honest. |
| **Every accepted edit is hashed** | The remedy carries the SHA-256 of the exact bytes; `apply-recommendation` re-verifies before writing, so an edited record cannot be applied under its original id. |

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
| `register` CLI reference | [CLI.md](CLI.md#zicato-register) |
| The post-apply check codes the scorecard classifies on (§6.1) | [MUTATION-SURFACE.md](MUTATION-SURFACE.md), `src/zicato/mutation/validator.py` |
| The recommend-only reflection pattern this mirrors (findings, five-slot evidence, apply-to-a-draft) | [BOARD-REFLECTION.md](BOARD-REFLECTION.md), `src/zicato/reflection/findings.py` |
| The redaction envelope the reflection substrate reuses | [OVERFITTING.md §11](OVERFITTING.md), §2.5 above |
