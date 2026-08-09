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

// `scored/ran/tagged`, collapsed to the shortest form that loses nothing.
// A facet is a SLICE of the board, so a racing rung that runs a board subset
// can leave most of a slice unrun — and the denominators are the only thing
// that shows it. Sizing by what ran would render such a slice as "3",
// indistinguishable from a slice that is genuinely complete.
//
// The three numbers answer three different questions and are NOT redundant:
//   * `tagged` — how many entries the BOARD puts in the slice.
//   * `ran`    — how many of them produced a profile: the SCALAR's denominator.
//   * `scored` — how many produced an outcome: the MEAN SCORE's denominator.
// `1/3` alone cannot distinguish "three ran, two had no outcome check" from
// "one ran, two were skipped" — the first says the board is thin on checks,
// the second says the measurement is missing. So each number appears exactly
// when it differs from the one after it.
export function facetCount(scored, ran, tagged) {
  const s = Number.isInteger(scored) ? scored : 0;
  const t = Number.isInteger(tagged) ? tagged : 0;
  // A payload without `ran_count` degrades to the two-number form rather
  // than inventing a third value.
  const r = Number.isInteger(ran) ? ran : t;
  if (s === r && r === t) return String(s);
  if (r === t) return `${s}/${t}`;
  return `${s}/${r}/${t}`;
}

// The ONE explanation of the count column. Names all three denominators,
// because the collapsed forms hide whichever ones happen to coincide.
export function countHovercard() {
  return hovercardBody([
    el('div', { class: 'dn-hc-title', text: 'scored / ran / tagged' }),
    el('p', { text: 'How many entries the board puts in this slice (tagged), how many produced a run (ran — the scalar’s denominator), and how many of those produced an outcome (scored — the mean score’s denominator).' }),
    el('p', { text: 'Numbers that coincide are collapsed, so a bare “3” means all three agree. A slice is only as trustworthy as its smallest count; none of them carries a noise threshold.' }),
  ]);
}

// The ONE explanation of `scalar`, shared by every facet surface.
export function scalarHovercard() {
  return hovercardBody([
    el('div', { class: 'dn-hc-title', text: 'scalar · lower is better' }),
    el('p', { text: 'The same loss the promote gate compares, computed over just this slice at this epoch’s frozen weights: the drift term (every judge’s weighted contribution included), the missed outcome, and the namespace terms.' }),
    el('p', { text: 'Computed over the tagged entries that RAN, so a slice whose entries were mostly skipped rests on fewer runs than “scored” suggests. Same units as the candidate’s own scalar, so a slice reads directly against it. Diagnostic only — no facet feeds the gate.' }),
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

// Attach an explanation to a header cell. `kind` is 'scalar', 'mean_score' or
// 'count'. attachHovercard sets tabindex + aria-describedby, so the
// explanation is keyboard-reachable and not mouse-only.
const HOVERCARDS = { scalar: scalarHovercard, mean_score: meanScoreHovercard, count: countHovercard };

export function attachFacetHover(cell, kind) {
  if (!cell) return cell;
  return attachHovercard(cell, HOVERCARDS[kind] || scalarHovercard);
}

// One facet SCALAR as the per-board drill-down shows it: the number, plus its
// coverage when the slice is not whole. That table has one cell per
// (candidate × facet) and no room for a count column, but a scalar resting on
// one run of a four-entry slice must not print identically to one resting on
// all four. The suffix appears only when it carries information.
export function facetScalarText(cell) {
  const c = cell || {};
  const num = facetNum(c.scalar);
  const ran = Number.isInteger(c.ran_count) ? c.ran_count : null;
  const tagged = Number.isInteger(c.entry_count) ? c.entry_count : null;
  if (ran === null || tagged === null || ran === tagged) return num;
  return `${num} · ${ran}/${tagged}`;
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
