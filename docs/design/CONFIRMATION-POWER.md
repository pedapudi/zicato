# Independent confirmation: evidence and measurement cost

Promotion confirmation uses separately identified draws of the selected
champion/challenger pair. Strategy results select that pair and remain in the
audit, but cannot supply confirmation observations. Racing rungs can reuse
underlying measurements, and selecting their winner conditions on their results.
Each confirmation attempt runs one paired board draw. Missing, mixed, or repeated
draw identities cannot count as independent evidence.

The strength-difference interval includes covariance. Its one-sided tail is
`min(1 - threshold, 0.025) / (K * (B + 1))`, where `K` is the number of candidates
planned before application and `B` is the maximum number of confirmation
attempts. The allowance is fixed before outcomes. This normal approximation
does not establish a universal false-promotion bound for arbitrary losses or
repeated adaptive rounds.

## Measured operating characteristics

The noisy convergence fixture has five board entries. Independent defect-token
flips use probability 0.22. The null scalar-difference standard deviation is
0.662794 analytically; the 60 seeded control draws measure 0.598. Planted scalar
improvements have magnitudes 0.336, 0.672, and 2.016.

The statistical test configuration averages 32 ordinary draws for selection,
uses aggregate pass-rate monotonicity, and permits 38 single-draw confirmation
attempts at threshold 0.8. It has one planned candidate. This configuration is
distinct from the shared four-candidate, 32-attempt default.

| Case | Promotions / trials | Total measured board units |
|---|---:|---:|
| Unchanged pair | 0 / 24 | 10,720 |
| Improvement 0.336 | 2 / 12 | 8,280 |
| Improvement 0.672 | 12 / 12 | 6,950 |
| Improvement 2.016 | 12 / 12 | 5,880 |

Board-unit totals include selection and confirmation. Every trial retains its
original seed. The small improvement's single-draw scalar-gate comparison
promotes in 3 of 12 trials. These samples do not support a claim that the
confirmation procedure has greater small-effect power. The statistical tests
check null behavior, monotone power, large-effect detection, actual measurement
counts, and distinct confirmation identities. They do not claim general
qualification from the observed zero false promotions.

The complete deterministic recommended-loop fixture confirms its planted
improvement after 21 independent confirmation draws. Full candidate application,
partial application, and interrupted execution with recovery retain the same
132 planned comparisons. The fixture with no measured candidate evidence
defers and retains the champion. These fixtures demonstrate composition and
durability; deterministic outcomes do not measure statistical power.

## Independent power and cost reference

For the five-entry noise fixture, enumerate the 16 possible measured subsets of
four defect tokens for each entry, score the actual board predicate, and convolve
the five independent entry distributions. The small planted improvement has
paired-difference mean -0.336 and standard deviation 0.681457. A confirmation
draw wins with probability 0.637252, ties with probability 0.102816, and loses
with probability 0.259932.

For a pair with `w` wins and `n - w` losses, the independent scalar likelihood
equation is `w - n * logistic(d) - d / 2 = 0`, where `d` is the fitted strength
difference and the per-strength prior precision is one. The difference variance
is `1 / (n * logistic(d) * (1 - logistic(d)) + 1/2)`. Enumerating the three possible
draw outcomes through the finite stopping rule gives:

| Planned candidates | Attempt budget | Small-effect confirmation probability | Expected confirmation board units |
|---:|---:|---:|---:|
| 1 | 38 | 0.152841 | 369.1 |
| 4 | 16 | 0 | 160.0 |
| 4 | 32 | 0.020694 | 319.4 |
| 4 | 64 | 0.242062 | 609.3 |
| 4 | 128 | 0.761413 | 954.7 |

These probabilities condition on selection of the pair. Ties consume attempts
but add no resolved duel. Each paired five-entry draw costs ten board units.
The table changes only the explicitly stated budget and planned family; it
does not recommend changing the shared defaults.

The sign-based fit discards loss magnitudes. A fixed-look paired-loss test using
the *known synthetic null distribution* reaches conditional power 0.627620 at
32 draws and 0.933730 at 64 draws for four planned candidates. It spends 320 or
640 board units and uses per-candidate tail allowance 0.025/4. That calculation
is a reference for this synthetic model. Unknown loss distributions require
separate qualification.
A replacement requires explicit distributional assumptions, stopping and family
control, treatment of missing measurements, and complete-loop cost validation.

Descriptive index ratings retain point values and observation counts. Their
ledger lacks independent measurement provenance, so their uncertainty field
is null. Confirmation views show the recorded independent evidence and leave
historical selection-only summaries unknown.
