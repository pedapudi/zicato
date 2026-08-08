// js/facets.js — the shared vocabulary for FACET surfaces.
//
// A `facet:` board tag puts an entry in a named slice (BOARD-FORMAT.md §1.4).
// Two screens render those slices, asking different questions:
//
//   * the candidate DOSSIER — one candidate, every facet: "what is this
//     candidate good and bad at?" Rows are facets, and the candidate's own
//     aggregate is the last row to read against.
//   * the per-BOARD drill-down — one entry, every candidate: "which slices
//     does this entry feed, and how are they moving?" Rows are candidates
//     (the orientation the rest of that page uses, and the one that scales —
//     an epoch grows candidates, not facets).
//
// The orientations differ because the questions differ. Everything ELSE must
// not: the column names, the direction arrows, the number formatting, the
// absent-measurement glyph, and above all the EXPLANATIONS. This module owns
// all of it so the two screens cannot drift apart — they already had two
// wordings of the same hovercard before this existed.
//
// Vocabulary is the codebase's own: `scalar` (the loss the gate compares) and
// `mean score` (the outcome axis), matching the labels already used on the
// racing table and the per-board mean-score caption.

import { el } from './core/dom.js';
import * as svg from './svg.js';
import { hovercardBody } from './ui.js';
import { attachHovercard } from './hovercard.js';

//: Column labels, arrows included. The two run in OPPOSITE directions — one
//: counts problems, the other counts quality — so every surface states the
//: direction rather than relying on the reader to know.
export const SCALAR_LABEL = 'scalar ↓';
export const MEAN_SCORE_LABEL = 'mean score ↑';

//: The tail every facet caption carries. Facets are recorded against a
//: candidate and read nowhere else: not the scalar the gate compares, not the
//: gate, not scheduling, not Pareto admission.
export const DIAGNOSTIC_NOTE = 'diagnostic, not gated';

// A facet number at its rendered precision, or the absent-measurement glyph.
// An em dash is NOT `0.00`: nothing measured differs from measuring a failure.
export function facetNum(value) {
  return svg.isNum(value) ? svg.fmt(value, 2) : '—';
}

// `scored/total`, collapsed to `scored` when they agree. A facet is a SLICE of
// the board, so a racing rung that ran a board subset can thin one to a single
// entry — the denominator is what makes that visible.
export function facetCount(scored, total) {
  const s = Number.isInteger(scored) ? scored : 0;
  const t = Number.isInteger(total) ? total : 0;
  return s === t ? String(s) : `${s}/${t}`;
}

// The ONE explanation of `scalar`, shared by every facet surface.
export function scalarHovercard() {
  return hovercardBody([
    el('div', { class: 'dn-hc-title', text: 'scalar · lower is better' }),
    el('p', { text: 'The same loss the promote gate compares, computed over just this slice at this epoch’s frozen weights: the drift term (every judge’s weighted contribution included), the missed outcome, and the namespace terms.' }),
    el('p', { text: 'Same units as the candidate’s own scalar, so a slice reads directly against it. Diagnostic only — no facet feeds the gate.' }),
  ]);
}

// The ONE explanation of `mean score`. Says what the number is NOT, because
// "81%" invites reading it as a pass percentage, which it is not on any board
// carrying continuous scores.
export function meanScoreHovercard() {
  return hovercardBody([
    el('div', { class: 'dn-hc-title', text: 'mean score · higher is better' }),
    el('p', { text: 'The average outcome over this slice: an entry’s continuous score when it has one, otherwise 1.0 for a pass and 0.0 for a fail.' }),
    el('p', { text: 'NOT a pass percentage — the two coincide only when every entry is plain pass/fail. An entry with no outcome check is left out of the average and shows in “scored”.' }),
  ]);
}

// Attach an explanation to a header cell. `kind` is 'scalar' or 'mean_score'.
// attachHovercard sets tabindex + aria-describedby, so the explanation is
// keyboard-reachable and not mouse-only.
export function attachFacetHover(cell, kind) {
  if (!cell) return cell;
  return attachHovercard(cell, kind === 'mean_score' ? meanScoreHovercard : scalarHovercard);
}

// The header cells of a dataTable-built table, or [] when the shape is not
// what we expect (the harness DOM and the browser agree on children order).
export function tableHeaderCells(table) {
  const head = table && table.children && table.children[0];
  const row = head && head.children && head.children[0];
  return (row && row.children) ? row.children : [];
}

// The caption line every facet surface carries: what the numbers are, then the
// diagnostic note. `what` names the slice-and-subject for that screen.
export function facetCaption(what) {
  return el('div', { class: 'dn-faint dn-facets-head', text: `facets · ${what} · ${DIAGNOSTIC_NOTE}` });
}
