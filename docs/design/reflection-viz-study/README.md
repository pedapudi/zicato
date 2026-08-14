# zicato inspect reflection-/instrument-lens visualization study

A set of standalone, self-contained, theme-adaptive study pages (one HTML file
per reflection **surface**) that explore how the Console dashboard could draw
**board reflection** — zicato's Measurement-System-Analysis layer for the
evaluation contract. These are **design mockups**, a companion to the
[`BOARD-REFLECTION.md`](../BOARD-REFLECTION.md) design note — **not** a TODO list
and **not** shipped UI.

Open any file in a browser to view it offline. Each page carries the full
16-theme Console swatch picker (top-right) so every figure recolours live, and
inline sample data in a `const S = {…}`. No CDN / network / build dependencies;
the colour tokens are ported 1:1 from the live Console `--v2-*` roles.

## The lens

Reflection treats the evaluation stack — `board + scoring + judges + gate` — as
a **measurement instrument**, and every promotion as a **measurement**. If the
instrument is noisy, invalid, or insensitive, the loop optimizes against a broken
signal. Reflection audits the instrument along **four pillars**:

| pillar | the question | how it's answered | surface |
| --- | --- | --- | --- |
| **Reliability** | is it *consistent*? | repetition (no ground truth) — noise floor σ, decision-flip rate, judge κ | `noise-cloud.html`, the κ gauge in `judge-audit.html` |
| **Discrimination** | can it *tell candidates apart*? | a spread of real candidates — differentiation, redundancy, min detectable Δ, coverage | the cross-judge graph in `judge-audit.html` |
| **Validity** | is it *correct*? | adjudication only (an independent meta-judge reads the transcript) — judge audit, score↔behaviour coherence | `xray.html`, `coherence.html`, `judge-audit.html` |
| **Calibration** | is it *tuned*? | recommend-only — margin from the noise floor, loss-term decomposition, judge pruning | `waterfall.html` |

The organizing fact (from the design note): **reliability you can measure with
no ground truth; validity you cannot** — so the spine is an **independent
meta-judge** that reads the actual transcript and adjudicates whether each
evaluator got it right, every verdict **span-grounded** so the operator verifies
in seconds.

## Files

| file | surface | one line |
| --- | --- | --- |
| [`index.html`](index.html) | **landing** | navigable index + the four-pillar overview |
| [`bill-of-health.html`](bill-of-health.html) | **bill of health** | top-line verdict + the four-pillar gauge quadrant |
| [`xray.html`](xray.html) | **transcript x-ray** (centerpiece) | a judge's claimed span + the meta-judge's confirm/deny + the TP/FP/FN colour grammar |
| [`coherence.html`](coherence.html) | **score↔behaviour coherence** | "the instrument's lies" — \|scalar move\| vs adjudicated severity; off-diagonal outliers glow |
| [`noise-cloud.html`](noise-cloud.html) | **noise cloud** | a violin of replicated scalars with `promote_margin` drawn inside the noise + the decision-flip rate |
| [`judge-audit.html`](judge-audit.html) | **judge audit** | a 2×2 confusion matrix + P/R/F1/FPR + κ gauge + evidence chips + cross-judge graph |
| [`waterfall.html`](waterfall.html) | **loss decomposition** | the scalar broken into its loss terms — dead terms, dominant terms |
| [`corpus-grid.html`](corpus-grid.html) | **live corpus** | the observation corpus filling in during an active `reflect` run |

## The colour grammar (load-bearing)

Across every page, the six semantic Console roles keep their meaning, plus the
reflection-specific verdict grammar on the x-ray:

- `--good` — **TP**: a *quiet-green seam* under a confirmed failure span; a
  healthy pillar; an on-diagonal (trustworthy) run.
- `--bad` — **FP**: a *loud red mark* where a judge fired but the transcript was
  clean (a false fire); a regression; a dominant loss term drowning the signal;
  a `promote_margin` that sits inside the noise.
- `--caution` — **FN**: a *highlighted missed span* a judge stayed silent on; a
  dead loss term; a pillar that needs a fix.
- `--accent` — the one structural / interactive highlight (the champion / the
  candidate under reflection, the running corpus cell).

`good` and `bad` are **earned by direction, never by identity** — exactly the
Console rule. A judge is not red because it is a judge; it is red only when it
false-fired.

## How to read the study

- **One file per surface.** Each page opens with a one-realistic-sample fact
  strip, then lays out **≥2 variants** of the figure (each with a rationale and a
  live figure rendered from the shared `const S`), mirroring the numbered-options
  layout of the [tournament-viz study](../tournament-viz-study/README.md).
- **Generic sample content.** Every transcript is a neutral assistant/user
  conversation (an itinerary-planning task); no real run data, no proper nouns.
- **Theme-adaptive.** The 16-theme picker (top-right) re-skins every figure with
  zero re-render — the marks read their colour from the active theme's tokens.

## Status

These are **design mockups** preserved alongside the
[`BOARD-REFLECTION.md`](../BOARD-REFLECTION.md) design note. Board reflection is a
proposal — not yet implemented — so unlike the
[tournament-viz study](../tournament-viz-study/README.md) (whose picks are shipped),
nothing here is live. This directory is a visual companion to the design, to make
the instrument-lens concept concrete.
