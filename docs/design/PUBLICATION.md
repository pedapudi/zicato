# The epoch publication

The **publication** is one epoch written up in the form of an academic paper:
what the campaign set out to do, how the tournament was configured, which
challengers were crowned and on what evidence, and how far the result can be
trusted. It is the operator-facing write-up of a single improvement campaign,
distinct from the per-round `insights/round_{N}.md` proposer-feedback files and
from the live dashboard views.

The report combines recorded narrative with measured epoch data. Authored
sections live in `epochs/{id}/analysis.prose.json`; refreshing tables and
figures reads those sections directly. The shared assembler in
`src/zicato/analyzer/report.py` publishes three derived artifacts:

* `analysis.md` — the complete Markdown report, including interpretation,
  deterministic sections, and figure markers.
* `analysis.html` — the self-contained report with styles and static figures.
* `analysis.fragment.html` — the same report body with styles scoped for
  embedding in the dashboard.

The HTML writer renders the body and figures once, then writes both HTML
forms. Each file replacement is atomic; publication across the Markdown and
HTML files is not a transaction. The dashboard reads the saved fragment through
`/api/epoch/{id}/analysis` and appends interactive tournament figures. Opening
`/api/epoch/{id}/analysis.html` serves the standalone document.

The rules below are normative. They fix what every section must carry, when the
document is regenerated and how it degrades mid-epoch, where each fact is
sourced, and how wide content is kept from overflowing the page.

## Design principles

1. **Exact by construction.** Every data-bearing fact is templated directly
   from the structured workspace artifacts. Numbers are never paraphrased,
      rounded, or invented by the narrative author. The paper’s Abstract,
   Introduction, Analysis, and Conclusion are authored from the structured
   epoch view and deterministic sections. The close-of-epoch retrospective
   also receives the journal, experiments, optional patterns, and computed
   tournament outcomes.
2. **Degrade honestly, never fabricate.** A feature that was disabled for the
   epoch renders a one-line "not enabled for this epoch" notice or is omitted
   entirely — never a fabricated number, never a broken placeholder, never a
   half-rendered table. A reader can trust that a present number is real and an
   absent section means the feature did not run.
3. **Living through the run, complete at close.** The document is regenerated
   as the campaign runs so it is always current; by epoch close it reads as a
   finished paper. Mid-epoch it is visibly stamped a draft.
4. **Reporting does not change evaluation.** Report generation stores authored
   narrative and publishes derived documents. It reads evaluation records
   without changing them and contributes nothing to the contract hash.
5. **Contained rendering.** No element ever pushes the page sideways. Wide
   content scrolls inside its own container (per the console design language,
   `CONSOLE-DESIGN-LANGUAGE.md` §5); the page body
   never scrolls horizontally.

## Content spec — the operator-approved outline

The renderer numbers headings, tables, and figures from their positions in
one assembled paper. The paper contains Abstract, Introduction, Methodology,
Approach & Implementation, Experimental Results, Statistical Integrity,
Proposer Analytics, Analysis, Threats to Validity & Limitations, and Conclusion.
The Abstract remains unnumbered. The content requirements are:

**Title + masthead.** Eyebrow, epoch name, the goal, and a metadata grid
(epoch id, status, generation counts, contract hash, span). Mid-epoch the
status cell carries the `LIVING DRAFT — through round N` stamp (see the
freshness model below).

**1. Abstract + headline.** The target and board in a single sentence; the
champion's Δ from the seed; promotions over rounds; total cost and cost per
promotion. One paragraph stating whether the campaign improved the champion and
what it cost.

**2. The contract (method).** The frozen evaluation contract:
* Board composition — entries, kinds, weights, expectations, judges — plus the
  holdout split and rotation cadence.
* Scoring — the weighted drift-derived loss + pass/fail predicates, per-judge
  weights, gates, **and** the `telemetry_dialect` (which reducer vocabulary the
  drift is read through).
* Tournament structure — the structure name (gauntlet by default; single- /
  double-elim / swiss / racing) and its params, including the winner-resolver
  and rating knobs when a non-default resolver/rating layer is configured.
* The full proposer configuration — `best_of_n`, `critique`, `screen`
  (+ veto-only), `process_exemplars`, `genealogy`, `recombine` (+
  `recombine_merge`), and the breadth/depth ensemble roles.

**3. The reign narrative (results).** The champion spine round by round. Each
promotion carries its evidence: gate margins measured against the A/A noise
floor, the Bradley–Terry (BT) rating with its confidence interval at crowning,
replicates spent, holdout confirmation.
Recombined and genealogy-assisted mints are flagged with their
`recombined_from` provenance. Notable rejects are called out.

**4. Ratings.** The final standings table (elo ± se, games — including racing
rung observations) and a rating-trajectory figure.

**5. Statistical integrity (validity).** The evidence that the reported gains
are real. Floor versus observed margins; placebo-arm outcomes (a **promoted placebo is a CRITICAL callout**);
Ladder holdout budget spent; screen veto/confirm counts; evidence-gate
deferral and replication statistics.

**6. Proposer analytics.** The hypothesis-calibration fraction (fed by the
prediction-accuracy grader: how often the proposer's predicted Δ matched the
measured Δ); the slate / selection-mode mix; the cost split by role.

**7. Reflection findings** (when a `reflect` pass ran) + per-judge drift/loss
trends across the promoted lineage.

**8. Limitations + reproduction.** Honest caveats parameterised by the epoch's
real scale; the contract hash; seeds where recorded; the command that
regenerates the document.

The close-of-epoch retrospective adds headline movements, hypotheses that
held or failed, mutation points still available for improvement, and recommended
focus for a subsequent epoch. Its deterministic Tournament outcomes section
includes lineage, scalar trajectory, a score sparkline, and metric movements.
Both the retrospective and the paper’s four interpretive blocks remain stored
in `analysis.prose.json` and participate in the same report assembly.

Every section degrades honestly: absent data yields a one-line notice or an
omission.

## Data binding — where each section is sourced

Use `WorkspaceLayout` for paths, `tournament/scoring.py::read_gen_score` for
accepted generation scores, `workspace.read_loss` for loss records, the generation record's decoder
(`epoch/journal.py` → `read_epoch_experiments`), the durable per-round event log
(`epoch/round_log.py` → `RoundRecord` fold), the frozen scoring config
(`scoring.json`, which serialises `ScoringWeights` including
`tournament_structure`, `telemetry_dialect`, and the nested
`proposer_quality`), and the analytical index over new ad-hoc file-walks.

| Section | Binding |
| --- | --- |
| Masthead | `config.json` (id/name/status/hash/created/closed); goal distilled from `brief.md` `## Goal`. |
| 1 Abstract/headline | `EpochReportData` counts + `final_scalar`; cost from the round records' cost provenance when present, else omitted. |
| 2 Board | `board.jsonl` via `load_board_with_meta` → `BoardEntryView`; holdout split + rotation from `board_meta` / `board/split.py` when recorded. |
| 2 Scoring | `scoring.json` weights/gates + `telemetry_dialect`. |
| 2 Tournament structure | `scoring.json["tournament_structure"]` (name + params + resolver/rating). |
| 2 Proposer config | `scoring.json["proposer_quality"]` (`best_of_n`, `critique_enabled`, `screen_entries`, `screen_veto_only`, `process_exemplars`, `genealogy`, `recombine`, `recombine_merge`, roles). |
| 3 Reign narrative | promoted lineage from `generations/*/experiment.json` outcomes; per-promotion evidence from the round records (`gates`, `holdout`, `evidence_trail`, `decision_provenance`) and `recombined_from`. |
| 4 Ratings | the standings/rating layer (`selection/standings_ext.py`, `selection/rating.py`) when the contract configures a rating; else "not enabled". |
| 5 Statistical integrity | `RoundRecord` fold — screen veto/confirm (`CandidateScreened`), evidence replication (`EvidenceReplicated`), holdout (`HoldoutReleased`), gate rule (`GateEvaluated`), placebo/`decision_provenance`; the measured noise floor from the epoch preflight record. |
| 6 Proposer analytics | hypothesis calibration from each `GenerationView` (predicted vs realised Δ, the same projection the hypothesis-vs-outcome figure uses); slate/selection-mode + cost from the round records. |
| 7 Reflection | the reflection artifacts (`reflection/`) when a pass ran; per-judge trends from `per_judge_loss_totals`. |
| 8 Limitations | `EpochReportData` scale (board size, judge coverage, sample size) + `contract_hash`. |
| Paper interpretation | Stored `ABSTRACT`, `INTRODUCTION`, `ANALYSIS`, and `CONCLUSION` blocks; the authoring prompt receives `EpochReportData` and deterministic sections. |
| Close-of-epoch retrospective | Stored `retrospective` block; the authoring prompt receives bounded journal text, experiment hypotheses and outcomes, optional patterns, and deterministic tournament outcomes. |

When a binding's source is absent for an epoch (e.g. no round has settled yet,
the measure's feature was disabled — screening off, no holdout — or no rating
layer was configured), the section degrades to its honest one-liner. The
scaffolding is present and lights up automatically once the source is
populated, with no change to the report code.

## Freshness model — refreshed when a round settles

The publication is refreshed by **round-settle events** rather than on a
wall-clock timer.

* **After each settled round** the orchestrator refreshes deterministic
  sections and figures without an evaluation call. The shared assembler reads
  authored blocks from `analysis.prose.json`; it does not recover narrative by
  parsing the previous Markdown report. Unchanged Markdown and HTML bytes do
  not trigger file replacements.
* **The refresh is strictly best-effort.** A report failure NEVER aborts a
  round: the regeneration is wrapped so any exception is swallowed and logged
  quietly (debug level, structured/stdlib log). The optimization loop takes
  priority over report generation.
* **Mid-epoch the document is a LIVING DRAFT.** While the epoch is open the
  masthead status carries a `LIVING DRAFT — through round N` stamp. The stamp
  is data-derived (it is present iff `config.json` has not been marked closed);
  the close render removes it automatically because the epoch is then closed.
* **Closing an epoch records its retrospective.** With an evaluation callable,
  the close path authors the five retrospective sections. During automatic
  epoch rollover, a further bounded call authors the paper’s four prose blocks.
  Both paths update stored narrative and invoke the shared assembler. Closing
  without an evaluation callable publishes measured results and existing prose;
  the closed epoch status removes the draft stamp.
* **`zicato repair report` renders on demand.** It regenerates the paper’s four
  prose blocks and all derived artifacts. With `--no-llm`, it refreshes measured
  content while retaining recorded narrative. Missing prose blocks receive an
  explicit unavailable notice.

The dashboard fetches the published HTML fragment and displays it directly.
It does not render Markdown or substitute report figure markers. Interactive
score, lineage, matchup, and drift figures remain below the published report,
along with per-matchup detail. If the fragment is absent, the tab reports that
the document is unpublished and still displays available tournament figures.

The redraw digest includes the full Markdown and fragment text, generation
rows, scalar trajectory, matchups, entry grids, and liveness. Identical display
inputs leave the DOM intact; equal-length text corrections still redraw.

## Rendering rules — never overflow the page

Per the console design language, inherently-wide content scrolls inside its
**own** container; the page body must never scroll horizontally. The
publication enforces this on every wide element, in both rendered forms:

* **Tables** rendered by `_render_md_table` scroll inside
  `figure.paper-table` in both HTML forms. The dashboard also wraps the saved
  fragment and interactive tables in `dn-table-scroll`. Long paths and hashes
  in `code` cells break within the cell.
* **Figures** (spliced SVG) scale to container width, aspect-locked
  (`max-width: 100%`), never fixed-width past their box.
* **Code / `pre` blocks** scroll inside themselves (`overflow-x: auto`).
* **The masthead metadata grid** wraps; long unbroken tokens — contract
  **hashes** above all — break rather than overflow their cell (the masthead
  shows a truncated hash; full hashes elsewhere get `overflow-wrap: anywhere`).
* **Prose** uses `max-width: 100%` and breaks long inline tokens.

Node and Python tests pin the wrapper classes on tables and figures and the
digest no-op so a regression re-introducing an overflow or a flashing refresh
fails the suite.

## Where the code lives

* `report_data.py` — walks one epoch's tree into a frozen `EpochReportData`
  view (JSON-friendly; consumed by both the renderers and the LLM prompt).
* `report_sections.py` — the deterministic, exact-by-construction markdown
  sections.
* `report_figures.py` + `svg/` — the inline-SVG figures, drawn from the same
  view; labels are kept inside their boxes by the shared text-fitting
  primitives.
* `report_prompts.py` — the bounded evaluation-LLM prompt for the four prose
  blocks.
* `report.py` — stored prose, shared report assembly, deterministic refresh,
  and the bounded four-block authoring entry point. Its HTML writer renders
  one body for standalone and embedded publication.
* `epoch/analysis.py` — the five-section retrospective and deterministic
  tournament outcomes, passed to the shared assembler.
* `query/epoch_view.py` — reads published Markdown and HTML artifacts without
  gathering measurements or rendering report figures during a request.
* `dashboard/static/js/views/publication.js` — displays the published fragment
  and adds interactive tournament figures and per-matchup tables.
