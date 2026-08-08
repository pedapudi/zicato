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
  assertEqual(facets.facetCount(2, 2), '2', 'a full slice hides the denominator');
  assertEqual(facets.facetCount(0, 1), '0/1', 'nothing scored exposes it');
  assertEqual(facets.facetCount(1, 4), '1/4', 'a partial slice exposes it');
  // The denominator is the count of TAGGED entries, so a slice whose entries
  // were mostly never run still reads as partial. Sizing by what ran would
  // render this '1' — indistinguishable from a genuinely complete slice.
  assertEqual(facets.facetCount(1, 3), '1/3', 'unrun tagged entries stay visible');
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
