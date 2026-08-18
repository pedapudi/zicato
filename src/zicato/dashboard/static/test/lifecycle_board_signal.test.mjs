// test/lifecycle_board_signal.test.mjs — the lifecycle figure's BOARD stage
// plots the quantity the verdict was decided on.
//
// The bug this pins: the BOARD stage plotted per-entry DRIFT LOSS, which is a
// structural 0.000 on every entry for any adapter that emits no drift stream.
// The stage meant to explain the promotion showed a column of zeroes, its Σ
// node summed a quantity unrelated to the GATE beside it, and an entry stuck at
// the floor (0.35 → 0.35) rendered identically to a control held at the ceiling
// (1.0 → 1.0). The figure now reads the SERVER's per-entry comparison
// (`/api/matchup-grid`, handed in as `compare`): Δ score, positive = better,
// with the replicate spread beneath it.

import { installDom, test, run, assert, assertEqual } from './harness.mjs';

installDom();

const { svg, dag, hovercard, hovercardTextOf, router, EPOCH_ID, installFetch, freshState } = await import('./fixtures.mjs');

function boardNodesOf(svgNode) {
  return svgNode.querySelectorAll('[class]').filter((n) =>
    n.localName === 'g' && (n.getAttribute('class') || '').split(/\s+/).includes('ezn-board-node'));
}
function childByClass(g, cls) {
  return g.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').split(/\s+/).includes(cls))[0] || null;
}
function byKeyOf(svgNode) {
  const out = {};
  for (const n of boardNodesOf(svgNode)) out[n.getAttribute('data-key')] = n;
  return out;
}
// the Σ node: the aggregate spine box, identified by the Δ it exposes.
function sigmaNodeOf(svgNode) {
  return svgNode.querySelectorAll('[data-channel]').filter((n) =>
    n.localName === 'g' && !(n.getAttribute('class') || '').includes('ezn-board-node'))[0] || null;
}
function nodeByClass(svgNode, cls) {
  return svgNode.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').split(/\s+/).includes(cls))[0] || null;
}
function textOf(n) { return n ? n.textContent : ''; }

// The promotion from the issue: two entries carried it, one control slipped
// slightly, two holdouts did not move. No drift stream anywhere.
const SCORED_ENTRIES = [
  { entry_id: 'entry_a', drift_loss: 0, pass_fail: true, score: 1.0 },
  { entry_id: 'entry_c', drift_loss: 0, pass_fail: true, score: 0.959 },
  { entry_id: 'holdout_a', drift_loss: 0, pass_fail: false, score: 0.35 },
];
const SCORED_COMPARE = {
  entry_a: { deltaScore: 0.585, champScore: 0.415, candScore: 1.0, se: 0.065, replicates: 3, decidedBy: 'score' },
  entry_c: { deltaScore: -0.041, champScore: 1.0, candScore: 0.959, se: 0.041, replicates: 3, decidedBy: 'score' },
  holdout_a: { deltaScore: 0.0, champScore: 0.35, candScore: 0.35, se: null, replicates: 1, decidedBy: 'score' },
};

function scoredDag(overrides) {
  return dag.lifecycleDag(Object.assign({
    genId: 'v2', parentId: 'v1', decision: 'promoted', promoted: true,
    championId: 'v1', entries: SCORED_ENTRIES, compare: SCORED_COMPARE, driftPresent: false,
  }, overrides || {}));
}

test('BOARD stage: each disc plots the SERVED Δ score, not the drift loss', () => {
  const byKey = byKeyOf(scoredDag());
  assertEqual(Object.keys(byKey).length, 3, 'one disc per board entry');
  for (const n of Object.values(byKey)) {
    assertEqual(n.getAttribute('data-channel'), 'score', 'the disc declares the channel it plots');
  }
  // the value INSIDE the disc is the signed Δ, compact enough to fit.
  assert(/^\+\.5\d$/.test(textOf(childByClass(byKey.entry_a, 'ezn-board-loss'))),
    'the carrying entry reads its +0.585 gain, not a drift loss of 0');
  assert(/^-\.04$/.test(textOf(childByClass(byKey.entry_c, 'ezn-board-loss'))),
    'the control that slipped reads a NEGATIVE delta — the regression is visible');
});

test('BOARD stage: positive Δ is GOOD — the loss channel’s colouring is inverted', () => {
  const byKey = byKeyOf(scoredDag());
  const gain = childByClass(byKey.entry_a, 'ezn-board-cmp');
  const slip = childByClass(byKey.entry_c, 'ezn-board-cmp');
  const flat = childByClass(byKey.holdout_a, 'ezn-board-cmp');
  assert((gain.getAttribute('class') || '').includes('ezn-cmp-better'), 'a score GAIN is coloured better');
  assert((slip.getAttribute('class') || '').includes('ezn-cmp-worse'), 'a score LOSS is coloured worse');
  assert((flat.getAttribute('class') || '').includes('ezn-cmp-even'), 'no movement is coloured even');
  // the key line states the convention, so the two channels cannot be confused.
  const key = nodeByClass(scoredDag(), 'ezn-dag-key');
  assert(/\+ = better/.test(textOf(key)), 'the key states + = better on the score channel');
});

test('BOARD stage: the sublabel names BOTH sides — floor and ceiling stop looking alike', () => {
  const byKey = byKeyOf(scoredDag({
    entries: SCORED_ENTRIES.concat([{ entry_id: 'holdout_c', drift_loss: 0, pass_fail: true, score: 1.0 }]),
    compare: Object.assign({}, SCORED_COMPARE, {
      holdout_c: { deltaScore: 0.0, champScore: 1.0, candScore: 1.0, se: null, replicates: 1, decidedBy: 'score' },
    }),
  }));
  const floor = textOf(childByClass(byKey.holdout_a, 'ezn-board-cmp'));
  const ceiling = textOf(childByClass(byKey.holdout_c, 'ezn-board-cmp'));
  assert(/0\.35/.test(floor), 'the entry stuck at the floor shows 0.35 → 0.35');
  assert(/1\.00/.test(ceiling), 'the control held at the ceiling shows 1.00 → 1.00');
  assert(floor !== ceiling, 'two entries that both moved by 0.000 no longer render identically');
});

test('BOARD stage: an unmeasured spread renders `--`, never ±0.000', () => {
  const byKey = byKeyOf(scoredDag());
  const measured = textOf(childByClass(byKey.entry_a, 'ezn-board-cmp'));
  const single = textOf(childByClass(byKey.holdout_a, 'ezn-board-cmp'));
  assert(/±0\.065/.test(measured), 'three replicates report their standard error');
  assert(/--/.test(single) && !/±/.test(single),
    'a single replicate says `--` — it must not imply a precision it does not have');
  assertEqual(childByClass(byKey.holdout_a, 'ezn-board-cmp').getAttribute('data-se'), '',
    'and carries no se in its data attribute');
  // the accessible name says the same thing.
  assert(/standard error unavailable/.test(byKey.holdout_a.getAttribute('aria-label')),
    'the aria-label states the spread is unavailable rather than reading a zero');
});

test('Σ node: the mean of the served Δs — the figure’s own arithmetic, reconciling with the gate', () => {
  const svgNode = scoredDag();
  const agg = sigmaNodeOf(svgNode);
  assert(agg && agg.getAttribute('data-channel') === 'score', 'the Σ node rendered on the score channel');
  const mean = (0.585 - 0.041 + 0.0) / 3;
  assertEqual(agg.getAttribute('data-delta-sigma'), svg.fmtSigned(mean, 3),
    'Σ is the MEAN of the per-entry Δs — the movement in the board-level mean score');
  assert(/Σ Δ score/.test(textOf(agg)), 'the node is labelled for the channel it aggregates');
  assert(/3 entries/.test(textOf(agg)), 'and says how many entries it averaged');
  assert(/mean of the per-entry/.test(hovercardTextOf(agg)),
    'the hovercard states the identity rather than leaving the reader to infer it');
});

test('Σ node: only entries BOTH sides ran feed the mean (the gate’s own restriction)', () => {
  const svgNode = scoredDag({
    entries: SCORED_ENTRIES.concat([{ entry_id: 'challenger_only', drift_loss: 0, pass_fail: true, score: 0.9 }]),
  });
  const agg = sigmaNodeOf(svgNode);
  assert(/3 entries/.test(textOf(agg)),
    'the unpaired entry carries no Δ and is not counted — the mean stays a like-for-like comparison');
});

test('drift: hidden when the workspace emits no drift stream, kept when it does', () => {
  // no score anywhere AND no drift stream → the pass predicate is all there is,
  // and the figure says so instead of plotting structural zeroes.
  const bare = dag.lifecycleDag({
    genId: 'v2', parentId: 'v1', decision: 'rejected', championId: 'v1', driftPresent: false,
    entries: [{ entry_id: 'a', drift_loss: 0, pass_fail: true }, { entry_id: 'b', drift_loss: 0, pass_fail: false }],
  });
  const bareKeys = byKeyOf(bare);
  assertEqual(bareKeys.a.getAttribute('data-channel'), 'pass', 'the figure falls through to the pass channel');
  assertEqual(textOf(childByClass(bareKeys.a, 'ezn-board-loss')), '✓', 'a passing entry reads ✓, not 0');
  assertEqual(textOf(childByClass(bareKeys.b, 'ezn-board-loss')), '✕', 'a failing entry reads ✕, not 0');
  assert(/no continuous score, no drift stream/.test(textOf(nodeByClass(bare, 'ezn-dag-key'))),
    'the key states why there is no magnitude to plot');

  // the same board WITH a drift stream keeps today's drift rendering.
  const drifting = dag.lifecycleDag({
    genId: 'v2', parentId: 'v1', decision: 'rejected', championId: 'v1', driftPresent: true,
    entries: [{ entry_id: 'a', drift_loss: 60.5, pass_fail: false }],
    compare: { a: { champDrift: 105.5 } },
  });
  const dk = byKeyOf(drifting);
  assertEqual(dk.a.getAttribute('data-channel'), 'drift', 'a drift-bearing workspace still reads drift');
  assert(/champ 106/.test(textOf(childByClass(dk.a, 'ezn-board-cmp'))), 'and still shows the champion’s loss');
  assert(/lower is better/.test(hovercardTextOf(dk.a)), 'with the loss channel’s own sign convention');
});

test('the score channel wins over drift when BOTH are populated', () => {
  const byKey = byKeyOf(dag.lifecycleDag({
    genId: 'v2', parentId: 'v1', decision: 'promoted', championId: 'v1', driftPresent: true,
    entries: [{ entry_id: 'a', drift_loss: 60.5, pass_fail: true, score: 0.8 }],
    compare: { a: { deltaScore: 0.3, champScore: 0.5, candScore: 0.8, se: null, replicates: 1, champDrift: 105.5 } },
  }));
  assertEqual(byKey.a.getAttribute('data-channel'), 'score',
    'the continuous outcome is the default: it is what the gate aggregates');
  assert(/higher is better/.test(hovercardTextOf(byKey.a)), 'the hovercard states the score convention');
});

test('a re-raced entry expands to its per-replicate SCORES on the score channel', () => {
  const svgNode = dag.lifecycleDag({
    genId: 'v2', parentId: 'v1', decision: 'promoted', championId: 'v1', driftPresent: false,
    entries: [
      { entry_id: 'a', drift_loss: 0, pass_fail: true, score: 0.6, rung: 'rung 0', run_id: 'r0' },
      { entry_id: 'a', drift_loss: 0, pass_fail: true, score: 0.8, rung: 'rung 1', run_id: 'r1' },
    ],
    compare: { a: { deltaScore: 0.3, champScore: 0.5, candScore: 0.8, se: 0.1, replicates: 2 } },
  });
  const node = byKeyOf(svgNode).a;
  const values = node.querySelectorAll('[class]')
    .filter((n) => (n.getAttribute('class') || '').split(/\s+/).includes('ezn-board-run-loss'))
    .map((n) => n.textContent);
  assertEqual(values.length, 2, 'one row per run in the expansion panel');
  assert(values.includes(svg.fmt(0.6, 2)) && values.includes(svg.fmt(0.8, 2)),
    'the rows read the per-run SCORES — never a column of drift zeroes');
});

test('the whole figure stays hovercard-wired (no native <title> regressions)', () => {
  const byKey = byKeyOf(scoredDag());
  for (const [id, n] of Object.entries(byKey)) {
    assert(hovercard.hasHovercard(n), `${id}'s disc carries a hovercard`);
  }
});

// ── the client no longer joins the champion itself ──────────────────────────
// The dossier used to fetch the champion's per-entry payload a SECOND time and
// join, slice and sum it in the browser. That join, the slice restriction and
// the verdict are the server's (DQ1): the view reads one matchup-grid row per
// entry. A regression here would silently reintroduce a client-side definition
// of "the same board slice", which is how the Σ and the gate drifted apart.

test('candidate dossier: the champion comparison is FETCHED as a matchup grid, never re-joined client-side', async () => {
  freshState();
  const seen = [];
  installFetch();
  const inner = globalThis.fetch;
  globalThis.fetch = async (path) => { seen.push(path); return inner(path); };
  try {
    const candidate = await import('../js/views/candidate.js');
    const host = document.createElement('div');
    await candidate.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, gen: 'v1' });
    const perEntry = seen.filter((p) => /\/per-entry$/.test(p));
    const own = perEntry.filter((p) => p.includes('/v1/per-entry'));
    const champion = perEntry.filter((p) => p.includes('/v0/per-entry'));
    assert(own.length >= 1, 'the dossier still reads its OWN per-entry rows');
    assertEqual(champion.length, 0, 'it does NOT read the champion’s per-entry rows to join them itself');
    assert(seen.some((p) => p.startsWith(`/api/matchup-grid/${EPOCH_ID}/v0/v1`)),
      'the champion comparison comes from the served matchup grid instead');
  } finally {
    globalThis.fetch = inner;
  }
});

await run();
