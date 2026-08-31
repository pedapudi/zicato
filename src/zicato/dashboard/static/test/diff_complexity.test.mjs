// test/diff_complexity.test.mjs — the DIFF-COMPLEXITY line item.
//
// When a contract carries a non-zero diff_complexity_weight, the scalar grows a
// `diff_complexity` term that penalises a bigger patch. That term rides on
// `gate.scalar_components.{champion,challenger}` (the SAME map the radar plots),
// so we surface it two ways with NO new backend data:
//   * the radar spoke gets a readable label ("Diff complexity") rather than the
//     raw key;
//   * one extra INFORMATIONAL row slots into the gate rules ladder — neutral
//     unless the candidate's patch is strictly costlier (then a caution tone),
//     never short-circuiting (`fired` is never set).
//
// Pins:
//   * diffComplexityRule reads scalar_components[diff_complexity] per side →
//     neutral / caution / null (absent on both sides);
//   * absent (weight 0 / pre-feature) → null → no row → byte-identical to today;
//   * gatePanel appends the row AFTER the deterministic rules;
//   * buildRadarModel prettifies the diff_complexity axis label;
//   * diffComplexityDigest carries NO timestamp + rounds floats, so a no-op
//     heartbeat is byte-identical while a re-score flips it (render discipline).

import { installDom, test, run, assert, assertEqual } from './harness.mjs';

installDom();

const candidate = await import('../js/views/candidate.js');

function classOf(node) { return (node && node.getAttribute && node.getAttribute('class')) || ''; }
function hasClass(node, cls) { return classOf(node).split(/\s+/).includes(cls); }
function allByClass(host, cls) {
  return host.querySelectorAll('[class]').filter((n) => hasClass(n, cls));
}
function mountInto(node) { const h = document.createElement('div'); if (node) h.appendChild(node); return h; }

// ── 1. diffComplexityRule: per-side costs → a rule-shaped dict ────────────────
test('diffComplexityRule: both sides present, candidate simpler → NEUTRAL row with both costs + Δ', () => {
  const rule = candidate.diffComplexityRule({
    scalar_components: {
      champion: { diff_complexity: 0.40, judge_a: 1.0 },
      challenger: { diff_complexity: 0.22, judge_a: 0.9 },
    },
  });
  assert(rule, 'a rule is built when the term resolves on both sides');
  assertEqual(rule.id, 'diff_complexity', 'carries the diff_complexity id');
  assertEqual(rule.label, 'Diff complexity', 'the label is prettified (Title-cased human text)');
  assertEqual(rule.status, 'neutral', 'a simpler candidate patch reads NEUTRAL (no parsimony cost)');
  assertEqual(rule.fired, false, 'the line item NEVER short-circuits (fired is always false)');
  assert(rule.detail.includes('0.40') && rule.detail.includes('0.22'), 'the detail names both per-side costs');
  assert(rule.detail.includes('-0.18'), 'the detail carries the signed Δ (candidate − champion)');
});

test('diffComplexityRule: candidate patch strictly COSTLIER → CAUTION (the term pulling against promote)', () => {
  const rule = candidate.diffComplexityRule({
    scalar_components: {
      champion: { diff_complexity: 0.20 },
      challenger: { diff_complexity: 0.55 },
    },
  });
  assert(rule, 'a rule is built');
  assertEqual(rule.status, 'caution', 'a costlier candidate patch reads CAUTION (it pulls against promotion)');
  assertEqual(rule.fired, false, 'caution is NOT a rejection — still never fires the short-circuit');
  assert(rule.detail.includes('+0.35'), 'the detail shows the positive Δ (the added complexity)');
});

test('diffComplexityRule: equal costs read NEUTRAL (no penalty either way)', () => {
  const rule = candidate.diffComplexityRule({
    scalar_components: { champion: { diff_complexity: 0.33 }, challenger: { diff_complexity: 0.33 } },
  });
  assertEqual(rule.status, 'neutral', 'equal diff-complexity → neutral, not caution');
});

test('diffComplexityRule: only one side resolved → still a row, the present side reads', () => {
  const onlyChall = candidate.diffComplexityRule({
    scalar_components: { champion: {}, challenger: { diff_complexity: 0.30 } },
  });
  assert(onlyChall, 'a row is built when only the candidate term resolves');
  assertEqual(onlyChall.status, 'neutral', 'a one-sided term cannot be "costlier than champion" → neutral');
  assert(onlyChall.detail.includes('0.30') && onlyChall.detail.toLowerCase().includes('champion term absent'), 'names the candidate cost + the absent champion side');
});

// ── 2. back-compat: absent (weight 0 / pre-feature) → null (byte-identical) ───
test('diffComplexityRule: the term absent from BOTH sides (weight 0) → null (no row)', () => {
  assertEqual(candidate.diffComplexityRule({
    scalar_components: { champion: { judge_a: 1.0 }, challenger: { judge_a: 0.9 } },
  }), null, 'a default-off run (no diff_complexity key) → null → byte-identical to today');
});

test('diffComplexityRule: no scalar_components at all (pre-#19 / malformed) → null', () => {
  assertEqual(candidate.diffComplexityRule({}), null, 'no scalar_components → null');
  assertEqual(candidate.diffComplexityRule(null), null, 'no gate → null');
  assertEqual(candidate.diffComplexityRule({ scalar_components: { champion: null, challenger: null } }), null, 'both component maps null → null');
});

// ── 3. gatePanel: the row slots into the ladder AFTER the deterministic rules ─
test('gatePanel: the diff-complexity row appends after the deterministic rules', () => {
  const host = mountInto(candidate.gatePanel({
    decision: 'promoted', rules: [
      { id: 'scalar_margin', label: 'Scalar margin', status: 'pass', detail: '', fired: false },
    ],
    scalar_components: {
      champion: { diff_complexity: 0.20 },
      challenger: { diff_complexity: 0.55 },
    },
  }));
  const ladder = allByClass(host, 'dn-rules')[0];
  assert(ladder, 'the rules ladder rendered');
  const rows = ladder.childNodes.filter((n) => n.getAttribute && hasClass(n, 'dn-rule'));
  assertEqual(rows.length, 2, 'the deterministic rule + the diff-complexity row');
  // the diff-complexity row is LAST (it free-rides after the short-circuit rules).
  const last = rows[rows.length - 1];
  assert(last.textContent.includes('Diff complexity'), 'the last row is the (prettified) Diff complexity line item');
  assert(hasClass(last, 'dn-rule-caution'), 'a costlier candidate patch tones the row caution (shipped --v2-caution token)');
  assert(host.textContent.includes('Scalar margin'), 'the deterministic rule still reads above it');
});

test('gatePanel: a default-off gate (no diff_complexity term) renders the ladder byte-identical to today', () => {
  const baseRules = [{ id: 'scalar_margin', label: 'Scalar margin', status: 'pass', detail: '', fired: false }];
  const withCompNoTerm = mountInto(candidate.gatePanel({
    decision: 'promoted', rules: baseRules,
    scalar_components: { champion: { judge_a: 1.0 }, challenger: { judge_a: 0.9 } },
  }));
  const preFeature = mountInto(candidate.gatePanel({ decision: 'promoted', rules: baseRules }));
  const rowsA = allByClass(withCompNoTerm, 'dn-rule').length;
  const rowsB = allByClass(preFeature, 'dn-rule').length;
  assertEqual(rowsA, 1, 'no diff_complexity key → no extra row');
  assertEqual(rowsA, rowsB, 'the ladder is byte-identical to the pre-feature path');
  assertEqual(allByClass(withCompNoTerm, 'dn-rule-caution').length, 0, 'no caution row appears');
});

test('gatePanel: the diff-complexity row renders even when there are NO deterministic rules', () => {
  const host = mountInto(candidate.gatePanel({
    decision: 'deferred', rules: [],
    scalar_components: { champion: { diff_complexity: 0.40 }, challenger: { diff_complexity: 0.22 } },
  }));
  const rows = allByClass(host, 'dn-rule');
  assertEqual(rows.length, 1, 'the single diff-complexity row renders on its own');
  assert(rows[0].textContent.includes('Diff complexity'), 'it is the diff-complexity line item');
  assert(hasClass(rows[0], 'dn-rule-neutral'), 'a simpler patch reads neutral (flat dot, informational)');
});

// ── 4. radar: the diff_complexity axis label is prettified ───────────────────
test('buildRadarModel: the diff_complexity scalar component plots as a "Diff complexity" spoke', () => {
  const model = candidate.buildRadarModel({
    primaryGate: {
      delta_pass_rate: null,
      scalar_components: {
        champion: { diff_complexity: 0.40, judge_a: 1.2, judge_b: 0.8 },
        challenger: { diff_complexity: 0.22, judge_a: 1.1, judge_b: 0.7 },
      },
    },
    championScalar: 47.5,
    settledScalar: 45.0,
    projected: null,
    entries: [],
  });
  assert(model && Array.isArray(model.axes), 'a radar model is built (≥3 axes)');
  const labels = model.axes.map((a) => a.label);
  assert(labels.includes('Diff complexity'), 'the diff_complexity axis carries its human label');
  assert(!labels.includes('diff_complexity'), 'the raw machine key does NOT leak onto a spoke');
  // a per-judge component key with no prettify entry passes through verbatim.
  assert(labels.includes('judge_a') && labels.includes('judge_b'), 'unknown component keys pass through unchanged');
});

// ── 5. digest: no-op-beat stability + a re-score / appearance flips it ────────
test('diffComplexityDigest: null when the term is absent on both sides (contributes nothing)', () => {
  assertEqual(candidate.diffComplexityDigest({}), null, 'no components → null (back-compat digest)');
  assertEqual(candidate.diffComplexityDigest({ scalar_components: { champion: {}, challenger: {} } }), null, 'no diff_complexity key → null');
  assertEqual(candidate.diffComplexityDigest(null), null, 'no gate → null');
});

test('diffComplexityDigest: a no-op beat is byte-identical (no timestamp, rounded floats)', () => {
  const a = candidate.diffComplexityDigest({
    scalar_components: { champion: { diff_complexity: 0.4012 }, challenger: { diff_complexity: 0.2241 } },
  });
  const b = candidate.diffComplexityDigest({
    scalar_components: { champion: { diff_complexity: 0.4038 }, challenger: { diff_complexity: 0.2249 } },
  });
  assertEqual(JSON.stringify(a), JSON.stringify(b), 'sub-2dp jitter on a no-op beat → IDENTICAL digest (no flash)');
});

test('diffComplexityDigest: a RE-SCORE (the term moving past 2dp) flips the digest (repaints)', () => {
  const a = candidate.diffComplexityDigest({
    scalar_components: { champion: { diff_complexity: 0.40 }, challenger: { diff_complexity: 0.22 } },
  });
  const b = candidate.diffComplexityDigest({
    scalar_components: { champion: { diff_complexity: 0.40 }, challenger: { diff_complexity: 0.55 } },
  });
  assert(JSON.stringify(a) !== JSON.stringify(b), 'the candidate term moving 0.22→0.55 flips the digest');
});

test('diffComplexityDigest: the term APPEARING (weight turned on) flips the digest', () => {
  const off = candidate.diffComplexityDigest({ scalar_components: { champion: { judge_a: 1 }, challenger: { judge_a: 1 } } });
  const on = candidate.diffComplexityDigest({ scalar_components: { champion: { diff_complexity: 0.4 }, challenger: { diff_complexity: 0.2 } } });
  assert(JSON.stringify(off) !== JSON.stringify(on), 'weight 0 (null digest) → present flips the digest (repaints the ladder)');
});

// ── 6. end-to-end: two builds of the same gate read identically (no-op beat) ──
test('gatePanel: re-building the SAME diff-complexity gate twice reads byte-identical (stable across beats)', () => {
  const gate = {
    decision: 'deferred', rules: [],
    scalar_components: { champion: { diff_complexity: 0.20 }, challenger: { diff_complexity: 0.55 } },
  };
  const host1 = mountInto(candidate.gatePanel(gate));
  const host2 = mountInto(candidate.gatePanel(gate));
  assertEqual(host1.textContent.replace(/\s+/g, ' '), host2.textContent.replace(/\s+/g, ' '), 'two builds of the same gate read identically');
  assertEqual(allByClass(host1, 'dn-rule-caution').length, allByClass(host2, 'dn-rule-caution').length, 'the caution row is stable across beats');
});

await run();
