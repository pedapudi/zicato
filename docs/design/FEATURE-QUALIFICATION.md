# Evidence for recommended and experimental features

Configuration defaults, implementation correctness, and measured improvement
are separate facts. This inventory is for maintainers deciding whether a
feature belongs in the recommended configuration or the experimental namespace.
It records the available evidence without treating a passing fixture as an
effectiveness result.

Tests must check the calculation against an independent reference and exercise
the intended configuration through real execution. Recommending a feature also
requires measured improvement on an independently assessed target, with
uncertainty, complete cost accounting, and evidence that the result generalizes.

The [recorded feature campaign](CAMPAIGN.md#r4-zero-of-nine-features-graduate--twice)
qualified no treatment in either valid run. Its
[scope limits](CAMPAIGN.md#r6-the-resolution-limit) are one board, one model
configuration, three rounds per independent trial, and tasks with one request
and response. The reported variation in measured improvement was approximately
0.040 per comparison. That summary does not establish the smallest improvement
the campaign could reliably detect. The five tasks supplied no separate
evaluation set hidden from candidate generation, so generalization
was unmeasured. Several treatment intervals also extend above 0.040;
inconclusive results do not exclude every effect above that value.

## Resolved recommendations

The typed defaults and [recommended scaffold](../../src/zicato/core/scoring_config.py)
resolve as follows. These settings have not graduated through the recorded
campaign. Their recommendation is an implementation policy pending independent
assessment, not a claim of empirical qualification.

| Capability | Shared typed and scaffold default | Evidence and remaining condition |
|---|---|---|
| Proposal sampling and critique | Three samples per candidate; critique enabled | Sampling only one proposal also bypasses critique. That combined change had the largest observed improvement in the second campaign run but failed the planned statistical criterion. Changing the default requires independent assessment and the full cost of proposing and evaluating candidates. |
| Candidate screening | Two entries; `screen_veto_only=false` | Confirmed catastrophic regressions exclude candidates; measurements of surviving candidates can also inform selection. The campaign did not establish a benefit. Tests of execution and measurements of benefit and cost are separate requirements. |
| Tournament | Racing with four requested challengers and two replicates per matchup | [Tests of complete execution](../testing/recommended-complete-loop.md) cover successful and failed candidate applications, fresh confirmation measurements, interruption, and recovery. They do not establish that this search policy outperforms a simpler policy. |
| Promotion evidence | Threshold 0.8; at most 32 extra draws | Independent calculations and seeded tests measure false promotions and detection of improvements under their stated assumptions. Tests of complete execution retain these defaults. Reliable detection of small improvements and generalization to real targets remain unproven. |

Screening and proposal sampling remain ordinary settings. An evidence review
may change their recommendation,
but a namespace migration cannot silently make that decision. The scaffold's
explicit experimental flags are all false; the
[registry tests](../../tests/test_knob_registry.py) check that configuration
fact, without asserting empirical qualification.

Selecting gauntlet without parameters, or explicitly supplying empty tournament
parameters, disables confirmation. Historical omissions retain their recorded
meaning; they do not acquire the shared recommendation when read.

Further work measures whether confirmation [reliably detects small improvements
at an acceptable cost](https://github.com/pedapudi/zicato/issues/503), requires
cached results to [match the settings actually used by workers](https://github.com/pedapudi/zicato/issues/504),
and [reconstructs campaign results and costs from raw records](https://github.com/pedapudi/zicato/issues/97).
Recording evaluation purpose and seed alone does not establish equivalent
execution settings. The published campaign summaries have not been independently
recomputed from the raw records.

## Features requiring qualification

Every optimization feature below remains unqualified and lives under
`experimental`. Defaults leave these features inactive. The paths identify the
authored configuration; archived contracts retain their recorded field paths.

| Feature and configuration | Implemented default | Evidence and scope | Graduation or remaining policy condition |
|---|---|---|---|
| Process exemplars, `experimental.process_exemplars` | 0 | Campaign treatment inconclusive; [redaction and execution tests](../../tests/test_process_exemplars_e2e.py) protect the feedback boundary. Generalization was unmeasured in the campaign. | Independently assessed improvement, complete cost, a control that evaluates an unchanged system, and no adverse generalization result. |
| Mechanical recombination, `experimental.recombine` | false | Campaign treatment inconclusive. The [test with predetermined expected results](../../tests/test_recombination_known_answer.py) checks that combining changes can repair defects that neither change fixes alone. | A comparative result must establish that the mechanism improves outcomes on the assessed target within its total cost. |
| Model-assisted recombination, `experimental.recombine_merge` | `mechanical`; inactive while recombination is off | The campaign contrast bundles merge method and eligibility of overlapping patch pairs. The [test with predetermined merge results](../../tests/test_recombination_merge_known_answer.py) exercises composition. | Qualify mechanical recombination first, then assess the additional merge/eligibility policy and its full cost. |
| Genealogy, `experimental.genealogy` | 0 | Campaign treatment inconclusive; [genealogy tests](../../tests/test_genealogy.py) establish bounded record selection. | Improvement and generalization evidence under the restricted feedback policy. Added prompt context must be included in cost. |
| Calibration feedback, `experimental.calibration_feedback` | 0 | Measured in combination with genealogy; no isolated qualifying effect. [Calibration tests](../../tests/test_calibration.py) check the underlying records. | Independent calibration improvement or proposal benefit under the campaign's stated criterion, with generalization and cost assessment. |
| Placebo frequency, `experimental.random_baseline_every_n` | 0 | [Placebo tests](../../tests/test_random_baseline_placebo.py) protect measurement and prevent lineage promotion. This is an experimental control rather than a proposal treatment. | Define the control's sensitivity, false positives, and cost for its intended measurement scope. |
| Generation limit, `experimental.max_generations_per_contract` | null | The control recommends a refresh after the configured generation count; it does not stop execution or roll an epoch. The campaign supplies no effectiveness result. | Assess whether refresh recommendations improve outcomes and account for reuse of evaluation tasks across epoch rolls. |
| Diff regularization and ceiling, `experimental.diff_complexity_weight`, `experimental.diff_complexity_ceiling` | Both 0.0 | Both controls are implemented under `experimental`. [Complexity tests](../../tests/test_scoring_diff_complexity.py) check its calculation. Neither has a qualifying campaign treatment. | Assess the complexity constraint's intended tradeoff and its effect on independently measured improvement. |
| Memory across epochs, `experimental.cross_epoch_memory` | false | No qualifying campaign treatment. | Require compatible evaluation settings and permitted feedback before reusing records, then measure benefit and cost. |
| Standings rating and leader resolver, `experimental.standing_rating`, `experimental.resolver` | Both `none` | Numerical and [independent dominance references](../../tests/test_selection_dominance.py) establish bounded mathematical behavior; the same test file exercises the experimental Swiss driver and final champion gate. No empirical qualification. | Compare selection outcomes and total cost under incomplete fields and noisy independent evaluation. |
| Swiss and elimination structures, `experimental.tournament_structures` | false | [Structure tests](../../tests/test_selection_experimental_structures.py) verify that execution requires explicit enablement. No qualifying comparative campaign result. | Independently measured improvement over ordinary tournament structures, including total cost and evidence of generalization. |

The campaign's change to the breadth and depth of proposal roles and its combined
screening and recombination change also failed to qualify. Neither result establishes an isolated effect
for every component. `uncertainty_gate` is absent from the implemented selection
parameters, so it is not a remaining migration target.

## Safety configuration and migration

Configured regression commands, rejection of changes outside permitted source,
and rejection of contradictory evaluation decisions remain ordinary safety
settings. Their tests establish whether the configured conditions are enforced.
An optimization experiment does not determine whether to retain an operator's
verification requirement.
Enforcement correctness and measured optimization benefit remain separate claims.

The additive `overfitting.ladder.noise_scale` setting is retired. It represented
a fixed threshold increment. Authored files must remove it, setting
`ladder.threshold` to the previous threshold (or `promote_margin` when null)
plus the increment to preserve the required improvement. Removing zero is sufficient.
The field implemented no randomized privacy mechanism.

Authored settings at former feature locations raise a migration error naming
the `experimental` destination. Moving equivalent values preserves the existing
hash representation and does not start another epoch. An actual change to
evaluation behavior follows the existing epoch change rule. Historical readers
preserve enabled behavior and verify the original hash without rewriting stored
files. The move removes the `experiment_memory` container, which held only one
field, and its separate builder operation.

## Namespace and graduation policy

The Experimental editor group owns the optional controls in the inventory.
Recommended configuration leaves that group at its defaults. Screening and
ordinary safety policy retain their configured behavior.

A later qualification must retain its supporting artifact and scope, compare
against a named baseline, apply the planned correction for testing several features, and
include all proposal and evaluation spending. Cost per promotion is undefined
when there are no promotions; zero additional calls does not establish zero
additional token or elapsed time. Generalization conditions require an
independent evaluation subset that actually executes. Passing mathematical or
deterministic integration tests cannot replace these measurements.
