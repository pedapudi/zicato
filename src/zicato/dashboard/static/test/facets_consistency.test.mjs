// test/facets_consistency.test.mjs — the two FACET surfaces stay consistent.
//
// The candidate dossier and the per-board drill-down render facets for
// different questions, so they differ in ORIENTATION (dossier: one candidate ×
// every facet; board: one entry's facets × every candidate). Everything else —
// labels, direction arrows, number formatting, the absent-measurement glyph,
// and the EXPLANATIONS — comes from js/facets.js so the two cannot drift.
//
// They already had two different wordings of the same hovercard before that
// module existed; this file is what keeps that from happening again.

import { installDom, test, run, assert, assertEqual } from './harness.mjs';

installDom();

const facets = await import('../js/facets.js');

test('facets: the absent measurement is an em dash, never 0.00', () => {
  assertEqual(facets.facetNum(null), '—', 'null renders as the em dash');
  assertEqual(facets.facetNum(undefined), '—', 'undefined renders as the em dash');
  assertEqual(facets.facetNum(NaN), '—', 'a non-number renders as the em dash');
  assertEqual(facets.facetNum(0), '0.00', 'a real zero is NOT the em dash');
  assertEqual(facets.facetNum(0.807), '0.81', 'two decimal places');
});

test('facets: the count collapses only when the slice was fully scored', () => {
  assertEqual(facets.facetCount(2, 2, 2), '2', 'a full slice hides the denominators');
  assertEqual(facets.facetCount(0, 1, 1), '0/1', 'nothing scored exposes the size');
  assertEqual(facets.facetCount(1, 4, 4), '1/4', 'a partial slice exposes it');
  // The outer denominator is the count of TAGGED entries, so a slice whose
  // entries were mostly never run still reads as partial. Sizing by what ran
  // would render this '1' — indistinguishable from a complete slice.
  assertEqual(facets.facetCount(1, 1, 3), '1/1/3', 'unrun tagged entries stay visible');
  // The middle number is the SCALAR's denominator and is not derivable from
  // the other two: these two cases would both collapse to '1/3'.
  assertEqual(facets.facetCount(1, 3, 3), '1/3', 'three ran, two had no outcome check');
  assertEqual(facets.facetCount(1, 1, 3), '1/1/3', 'one ran, two were skipped');
  // A payload with no `ran_count` degrades to the two-number form rather
  // than inventing a third value.
  assertEqual(facets.facetCount(1, null, 4), '1/4', 'an absent ran_count is not fabricated');
});

test('facets: a scalar prints its coverage when the slice is not whole', () => {
  // The per-board table has one cell per (candidate × facet) and no room for
  // a count column, so the coverage rides on the number itself — but only
  // when it says something.
  assertEqual(facets.facetScalarText({ scalar: 0.77, ran_count: 4, entry_count: 4 }), '0.77',
    'a whole slice prints the bare number');
  assertEqual(facets.facetScalarText({ scalar: 0.77, ran_count: 1, entry_count: 4 }), '0.77 · 1/4',
    'a thin slice cannot print identically to a whole one');
  assertEqual(facets.facetScalarText({ scalar: null, ran_count: 0, entry_count: 3 }), '— · 0/3',
    'a slice that ran nothing is an em dash AND a visible denominator');
  assertEqual(facets.facetScalarText({ scalar: 0.5 }), '0.50', 'counts absent ⇒ no suffix');
  assertEqual(facets.facetScalarText(null), '—', 'a missing cell is the absent glyph');
});

test('facets: the count explanation names all three denominators', () => {
  const text = facets.countHovercard().textContent;
  assert(/tagged/.test(text), 'names the board-side count');
  assert(/ran/.test(text), 'names the scalar’s denominator');
  assert(/scored/.test(text), 'names the mean score’s denominator');
  assert(/noise threshold/.test(text), 'and repeats that none of them is calibrated');
});

test('facets: both column labels carry their direction', () => {
  assert(/↓/.test(facets.SCALAR_LABEL), 'scalar is marked lower-is-better');
  assert(/↑/.test(facets.MEAN_SCORE_LABEL), 'mean score is marked higher-is-better');
  // The two run OPPOSITE ways; a surface that shows both must say so.
  assert(facets.SCALAR_LABEL !== facets.MEAN_SCORE_LABEL, 'the labels are distinct');
});

test('facets: every caption carries the diagnostic note', () => {
  const node = facets.facetCaption('this candidate re-scored per board tag');
  assert(node.textContent.includes(facets.DIAGNOSTIC_NOTE),
    'the caption states that facets are not gated');
  assert(node.textContent.startsWith('facets ·'), 'and names itself the same way');
});

test('facets: the scalar explanation names the gate, the judges, and the weights', () => {
  const text = facets.scalarHovercard().textContent;
  assert(/lower is better/.test(text), 'states the direction');
  assert(/gate/.test(text), 'ties it to the gate’s own number');
  assert(/judge/.test(text), 'says the judges are folded in');
  assert(/weights/.test(text), 'says it uses the epoch’s weights');
});

test('facets: the mean-score explanation says it is NOT a pass percentage', () => {
  const text = facets.meanScoreHovercard().textContent;
  assert(/higher is better/.test(text), 'states the direction');
  assert(/NOT a pass percentage/.test(text),
    'kills the misreading that 0.81 means "81% of entries passed"');
});

test('facets: attaching an explanation makes the header keyboard-reachable', () => {
  const cell = document.createElement('th');
  facets.attachFacetHover(cell, 'scalar');
  assertEqual(cell.getAttribute('data-hovercard'), '1', 'the header is marked hoverable');
  assertEqual(cell.getAttribute('tabindex'), '0', 'and focusable, so it is not mouse-only');
});

run();
