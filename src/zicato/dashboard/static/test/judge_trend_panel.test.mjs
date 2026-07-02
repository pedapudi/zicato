// test/judge_trend_panel.test.mjs — the PER-JUDGE TREND panel (WS4-A item 2).
//
// The epoch view's first consumer of the long-shipped
// /api/epoch/{id}/per-judge-trend read (reader + endpoint + D.perJudgeTrend
// existed with ZERO view consumers). One sparkline per judge across the spine
// generations, digest-gated.
//
// Pins:
//   * absent / degraded / empty reads → null (the epoch view stays
//     byte-identical to today, incl. against the Rust supervisor which does
//     not serve the endpoint);
//   * one dn-judgetrend-row per judge that has ≥1 plottable value; a judge
//     with NO values in the spine is dropped (never an all-empty lane);
//   * a spine gap (a generation with no loss for that judge) renders as a
//     null point — the row survives, the series pen-lifts;
//   * the last-value column carries the judge's latest weighted loss;
//   * judgeTrendDigest: a no-op beat is byte-identical; a moved loss or a new
//     spine column flips it (the render-discipline contract).

import { installDom, test, run, assert, assertEqual } from './harness.mjs';

installDom();

const epoch = await import('../js/views/epoch.js');

function classOf(node) { return (node && node.getAttribute && node.getAttribute('class')) || ''; }
function hasClass(node, cls) { return classOf(node).split(/\s+/).includes(cls); }
function allByClass(host, cls) {
  return host.querySelectorAll('[class]').filter((n) => hasClass(n, cls));
}
function mountInto(node) { const h = document.createElement('div'); if (node) h.appendChild(node); return h; }

// A realistic build_per_judge_trend payload (keys verbatim): the spine plus
// two judges — one full series, one with a spine gap — and one judge that
// never scored anything in this epoch.
function trendFixture() {
  return {
    epoch_id: 'e0',
    generations: ['v0', 'v1', 'v3'],
    judges: [
      { judge_name: 'coordinator', by_generation: { v0: 2.4, v1: 1.8, v3: 1.2 } },
      { judge_name: 'fact_checker', by_generation: { v0: 0.9, v3: 0.4 } },
      { judge_name: 'never_fired', by_generation: {} },
    ],
  };
}

test('buildJudgeTrendPanel: absent / degraded / empty → null (byte-identical to today)', () => {
  assertEqual(epoch.buildJudgeTrendPanel(null), null, 'null read (Rust supervisor) → no panel');
  assertEqual(epoch.buildJudgeTrendPanel(undefined), null, 'absent → no panel');
  assertEqual(epoch.buildJudgeTrendPanel({ generations: [], judges: [], note: 'index not built' }),
    null, 'the never-indexed degrade shape → no panel');
  assertEqual(epoch.buildJudgeTrendPanel({ generations: ['v0'], judges: [] }),
    null, 'no judges → no panel');
});

test('buildJudgeTrendPanel: one row per judge with data; a valueless judge is dropped', () => {
  const host = mountInto(epoch.buildJudgeTrendPanel(trendFixture()));
  const rows = allByClass(host, 'dn-judgetrend-row');
  assertEqual(rows.length, 2, 'coordinator + fact_checker render; never_fired is dropped');
  assertEqual(rows[0].getAttribute('data-judge'), 'coordinator', 'rows keyed by judge name');
  assertEqual(rows[1].getAttribute('data-judge'), 'fact_checker');
  // each row carries a name, ONE sparkline, and the last value.
  const names = allByClass(host, 'dn-judgetrend-name').map((n) => n.textContent);
  assertEqual(names.join(','), 'coordinator,fact_checker', 'name column renders');
  const lasts = allByClass(host, 'dn-judgetrend-last').map((n) => n.textContent);
  assertEqual(lasts[0], '1.200', 'the latest weighted loss renders (coordinator v3)');
  assertEqual(lasts[1], '0.400', 'a gapped series still reports its latest value');
  const sparks = allByClass(host, 'dn-spark');
  assertEqual(sparks.length, 2, 'one sparkline per rendered judge');
});

test('buildJudgeTrendPanel: a spine gap pen-lifts (two path segments), never fabricates a value', () => {
  const host = mountInto(epoch.buildJudgeTrendPanel(trendFixture()));
  const rows = allByClass(host, 'dn-judgetrend-row');
  // fact_checker has no v1 value → its path holds TWO M (move) commands
  // (pen up over the gap) rather than one continuous line.
  const gapRow = rows[1];
  const paths = allByClass(gapRow, 'dn-spark-line');
  assertEqual(paths.length, 1, 'the gapped series still draws one path element');
  const d = paths[0].getAttribute('d') || '';
  assertEqual((d.match(/M/g) || []).length, 2, 'the gap pen-lifts: two disjoint segments');
});

test('judgeTrendDigest: no-op beat byte-identical; a moved loss or new column flips it', () => {
  const a = JSON.stringify(epoch.judgeTrendDigest(trendFixture()));
  const b = JSON.stringify(epoch.judgeTrendDigest(trendFixture()));
  assertEqual(a, b, 'identical reads fold identically (no-op beat = zero DOM)');
  const moved = trendFixture();
  moved.judges[0].by_generation.v3 = 1.1;
  assert(JSON.stringify(epoch.judgeTrendDigest(moved)) !== a, 'a moved loss flips the digest');
  const grown = trendFixture();
  grown.generations.push('v4');
  assert(JSON.stringify(epoch.judgeTrendDigest(grown)) !== a, 'a new spine column flips the digest');
  assertEqual(epoch.judgeTrendDigest(null), null, 'a null read folds to a stable null');
});

run();
