# zicato racing-funnel visualization study

A single standalone, self-contained, theme-adaptive study page comparing **four
visual grammars for the racing `survivalFunnel` figure**, all rendered on the
identical served tournament model (field of 8 · rungs 8→4→2→1 · winner `g3` →
champion gate). A design mockup in the same spirit as
[`../reflection-viz-study/`](../reflection-viz-study/) — **not** shipped UI.

Open [`index.html`](index.html) in a browser to view it offline. No CDN /
network / build dependencies; colours follow the Console's `--v2-*` token roles
(dark default, light via `prefers-color-scheme` / `data-theme`).

## The four options

| option | grammar | speaks the language of | tradeoff |
| --- | --- | --- | --- |
| **A — Tapered stream** | one converging low-tint silhouette | the loss-floor waterfall | calmest; individuals dissolve |
| **B — Bracket rail** | per-generation hairline lanes, ✕ terminations, elbow convergence | `elimFlow` | traceable; busy at 16+ lanes |
| **C — Dot ladder** | dot columns per rung + converging splines | `racingScalarTrack` (its panel-mate) | kinship with the track; attrition shape implied |
| **D — Radial sieve** | concentric rung rings narrowing to the seat | `elimRadial` | compact; arcs fit progress bars awkwardly |

## Decision (2026-07-10)

The operator chose a **mix of B and C**: C's dot-ladder visual ("nicer") with
B's labeling ("better") — dot columns and converging splines, every generation
named at its left-edge entry row, transition-form rung headers (`R0 · 8→4`),
and the quiet ✕ cut marks. The winner's dots and spline carry the accent
end-to-end to the champion seat. Implemented as a pure render-body replacement
of `survivalFunnel` (`dashboard/static/js/svg.js`): the served model, digest,
callbacks, live/projected treatment, and progress sub-bars are unchanged.

Context for the round: this study followed the elimRadial restoration (the
operator vetoed its removal — the radial visual stays, now rendered from the
served `gen_states` model) and a first quiet-precision polish pass on the
funnel that the operator judged insufficient ("visually ugly compared to the
other visuals").
