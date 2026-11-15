# Open questions after the proposer-signal, frontier and cleanup changes (2026-11-15)

This document lists the decisions and the work left open after pull requests
#534, #557, #582 to #588 merged. It is a dated record: check each item
against the code and the issue tracker before acting on it, and remove an
item when it is settled.

## What merged

| Change | Pull request | Setting, default |
|---|---|---|
| Read-side code with no served caller removed | #582, #583, #584, #585 | none |
| Outcome feedback in the experiment memory; rejection reasons rendered as codes under restricted visibility | #586 | `experimental.outcome_feedback`, `false` |
| Pareto frontier: finer axes, confirmation, a recombination pool and steering | #587 | `experimental.recombine_pool`, `"rejects"`; `experimental.frontier_steering`, `false` |
| Operator-submitted candidates (`zicato proposer submit`) | #588 | none |
| Namespaced form of `metric_name` described in the schema and prompt | #534 | none |
| Mutation containment audited by byte range | #557 | none |

#534, #586 and #587 each change the contract hash, so an existing workspace
rolls one epoch on its next `zicato evolve`.

## Decisions to make

1. **Alternate parent on a plateau.** When the champion stalls, the loop
   could propose from a confirmed frontier member instead of the champion.
   The estimate is about 400 lines across about 12 modules, and it changes
   what a resumed round must restore. Build it now, or defer it until the
   frontier pool has been used in practice?
2. **The changelog.** `CHANGELOG.md` stated that changes are recorded only
   in the git history. These pull requests added entries and reworded that
   statement. Keep adding entries, or remove them and restore the statement?
3. **Recording realised metric movements.** Before #586 no code path
   recorded the realised movement of a metric, so every graded prediction
   counted as missed. #586 records them only when
   `experimental.outcome_feedback` is on. Should they be recorded always?
   Doing so changes the prediction-accuracy band and the calibration block
   in every workspace.
4. **Banded training-task counts.** Under restricted visibility the
   proposer sees the number of training tasks a candidate fixed and broke as
   `0`, `1`, `2-3` or `4+`. Is that band right, or should the counts be
   exact, or absent?
5. **Operator entries under outcome feedback.** An operator entry in the
   experiment memory drops its predicted movements, as it already dropped
   its core idea and its accuracy band. Its realised movements still render.
   This was decided while combining #586 and #588, where neither covered the
   case. Confirm it or reverse it.
6. **Whether a screen veto is final.** A recombination pair that the
   pre-tournament screen vetoes can be minted again in the next round. Both
   the rejects pool and the frontier pool behave this way. Making the veto
   final needs durable state.
7. **Adopting a frontier member by hand.** An operator cannot yet make a
   settled frontier member the champion. It needs the submit path of #588
   and a way to override promotion. Is it wanted?

## Work not started

### Proposer-quality benchmark

There is no measurement of whether proposals are meaningful, varied and
cumulative across rounds. The plan is two targets with a sealed split: one
software-performance target and one agent-quality target (a skill or a tool
description). No committed example is a performance target. The
benchmark must run on committed targets alone.

### Power of the holdout confirmation

A simulation of the gate's rules, written from the documentation and not
from the gate code, suggested that the default holdout check on six tasks
confirms a real gain about 38% of the time and passes a change with no
effect about 19% of the time. Reproduce both numbers against
`src/zicato/tournament/gate.py` before changing anything. Issue #503
proposes a paired test with a fixed budget, which is the likely direction.

### Round context object

One object carrying what a round reads, in place of the arguments now
threaded through the round's functions. Agreed in principle; not designed.

### Overlap among memory, genealogy and calibration

The experiment memory, the genealogy and the calibration block each render
part of the same experiment records in a different shape. They may share one
representation. A single ledger of lessons does not replace them: each
answers a different question for the proposer.

### Smaller items

- `estimate_cost` overstates the cost of the holdout phase.
- Two containment gaps deferred from #557, to be filed as issues: a promoted
  pair left `unverified` raises no alarm, and a violation together with a
  malformed record reads as `unverified`.
- Three names have no production caller after #582:
  `index.query.loss_profiles_for_generation`, the public name
  `experiments_for_epoch`, and `replicate_scores.measurement`.
- Passages that describe removed surfaces: `CONSOLE-SIMPLIFICATION.md`
  near lines 182 and 190, `DASHBOARD-VARIANTS.md` near lines 314 to 322, and
  `DASHBOARD-V2.md` near lines 215 and 291.

## Constraints on all of the above

- **The proposer sees dimensional scores only.** No change may give the
  proposer transcripts, traces or raw training inputs. Proposal
  quality is to improve through better-chosen dimensions, stated
  uncertainty, and feedback on the proposer's own predictions. The
  unredacted exemplars of issue #569 conflict with this.
- **Parsimony.** A change that adds lines must show its value; the line
  budget (`docs/design/LINE-BUDGET.md`) is the measure.
- **Kept as they are:** the Bradley–Terry evidence check, reflection, and
  the existing experimental features.
- **Epoch rolls.** A new `experimental` setting changes the contract hash
  for every workspace. Release such settings together so that workspaces
  roll one epoch.
- **Golden Rule G1** (`docs/dev-guide/01-orientation.md` §4) applies to
  every commit message and squash message.
