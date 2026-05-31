// test/variant_d_enrich.test.mjs — Variant D enrichment wave.
//
// Pins the four new visualization themes added to Variant D:
//   1. Candidate lifecycle — per-candidate loss-profile sparkbars + a
//      gate-verdict glyph; lineage bumps.
//   2. The boards a candidate faces — a board trellis of small multiples
//      with shared loss scale + per-generation pass/fail glyph rows.
//   3. Per-board scoring + drill-down — a sorted value dot-plot with a
//      champion reference line; entry detail with expectation dots +
//      per-judge bars + a lazy transcript.
//   4. Match-ups across styles — NON-COLLIDING paired slopegraphs (the
//      flagged defect), the gauntlet bumps ladder, and illustrative
//      bracket / round-robin matrix / race-lane alternatives.
//
// Covers both the pure SVG primitives (DOM/SVG assertions) and the views
// (rendered under a stubbed fetch), including a regression check that the
// paired slopegraph offsets coincident lines so they do not collide.

import { installDom, test, run, assert, assertEqual, makeEvent } from './harness.mjs';

installDom();

function findClass(node, cls, out = []) {
  if (!node || node.nodeType !== 1) return out;
  if (node.className && String(node.className).split(/\s+/).includes(cls)) out.push(node);
  for (const c of node.children) findClass(c, cls, out);
  return out;
}
function all(node, localName, out = []) {
  if (!node || node.nodeType !== 1) return out;
  if (node.localName === localName) out.push(node);
  for (const c of node.children) all(c, localName, out);
  return out;
}
function text(node) { return node ? node.textContent : ''; }
function num(el, attr) { return parseFloat(el.getAttribute(attr)); }

const svg = await import('../js/variants/D/svg.js');
const router = await import('../js/variants/D/router.js');

// ===================================================================
// THEME 1 primitive — sparkbar
// ===================================================================

test('sparkbar: one bar per finite entry + a verdict glyph; gaps drawn as ticks', () => {
  const node = svg.sparkbar({
    bars: [{ label: 'a', value: 60 }, { label: 'b', value: NaN }, { label: 'c', value: 120, fail: true }],
    domain: [0, 120], verdict: 'rejected',
  });
  assertEqual(node.localName, 'svg');
  const bars = all(node, 'rect');
  assertEqual(bars.length, 2, 'two finite bars (the NaN entry is a tick, not a bar)');
  const glyph = all(node, 'polygon');
  assertEqual(glyph.length, 1, 'a verdict glyph');
  assert(glyph[0].getAttribute('class').includes('d-bad'), 'rejected → bad-coloured glyph');
});

test('sparkbar: empty input yields a single empty rule, never throws', () => {
  const node = svg.sparkbar({ bars: [] });
  assert(all(node, 'rect').length === 0, 'no bars');
  assert(all(node, 'line').length === 1, 'just the empty rule');
});

// ===================================================================
// THEME 2 primitive — genDots (pass/fail/timeout row)
// ===================================================================

test('genDots: a pass dot, a fail cross, a timeout clock, and a no-run ring', () => {
  const node = svg.genDots({ cells: [
    { pass: 1, ran: true }, { pass: 0, ran: true },
    { timeout: true, ran: true }, { ran: false },
  ] });
  assert(findClass(node, 'd-glyph-pass').length === 1, 'pass dot');
  assert(findClass(node, 'd-glyph-fail').length === 2, 'fail cross = two strokes');
  assert(findClass(node, 'd-glyph-timeout').length === 1, 'timeout glyph');
  assert(findClass(node, 'd-glyph-none').length === 1, 'no-run ring');
});

// ===================================================================
// THEME 3 primitive — valueDotPlot + valueBars
// ===================================================================

test('valueDotPlot: dots classed good/bad vs the champion reference line', () => {
  const node = svg.valueDotPlot({
    items: [
      { label: 'better', value: 40, id: 'better', pass: 1 },
      { label: 'worse', value: 90, id: 'worse', pass: 0 },
    ],
    reference: { value: 60, label: 'champion v0' },
  });
  assert(findClass(node, 'd-ref-rule').length === 1, 'a champion reference rule');
  const good = findClass(node, 'd-good').filter((c) => c.localName === 'circle');
  const bad = findClass(node, 'd-bad').filter((c) => c.localName === 'circle');
  assertEqual(good.length, 1, 'value below champion reads good');
  assertEqual(bad.length, 1, 'value above champion reads bad');
});

test('valueDotPlot: a row click fires onClick with the entry', () => {
  let clicked = null;
  const node = svg.valueDotPlot({
    items: [{ label: 'e1', value: 50, id: 'e1', pass: 0 }],
    onClick: (it) => { clicked = it; },
  });
  const row = all(node, 'g')[0];
  row.dispatchEvent(makeEvent('click'));
  assert(clicked && clicked.id === 'e1', 'click passes the entry through');
});

test('valueBars: one bar per finite value, sorted-domain shared', () => {
  const node = svg.valueBars({ items: [{ label: 'j1', value: 27 }, { label: 'j2', value: 9 }] });
  assertEqual(all(node, 'rect').length, 2, 'two bars');
});

// ===================================================================
// THEME 4 primitive — pairedSlopegraph (the non-collision regression)
// ===================================================================

test('pairedSlopegraph: one line per entry, coloured by verdict', () => {
  const node = svg.pairedSlopegraph({ series: [
    { label: 'q3', id: 'q3', a: 71, b: 63.5, verdict: 'improved' },
    { label: 'picky', id: 'picky', a: 105.5, b: 642.5, verdict: 'regressed' },
    { label: 'demo', id: 'demo', a: 60.5, b: 60.5, verdict: 'flat' },
  ] });
  const lines = findClass(node, 'd-pslope-line');
  assertEqual(lines.length, 3, 'three duel lines');
  assert(lines.some((l) => l.getAttribute('class').includes('d-good')), 'improved → good');
  assert(lines.some((l) => l.getAttribute('class').includes('d-bad')), 'regressed → bad');
  assert(lines.some((l) => l.getAttribute('class').includes('d-flat')), 'flat → flat');
});

test('pairedSlopegraph REGRESSION: coincident lines are jittered apart (no collision)', () => {
  // Three entries whose CHAMPION value is identical — the classic
  // collision the operator flagged. The nodes must be offset so the
  // lines do not draw on top of each other.
  const node = svg.pairedSlopegraph({ series: [
    { label: 'e1', id: 'e1', a: 60.5, b: 10 },
    { label: 'e2', id: 'e2', a: 60.5, b: 30 },
    { label: 'e3', id: 'e3', a: 60.5, b: 50 },
  ] });
  // The left-column nodes are the 'd-pslope-node' circles at the left axis.
  const nodes = findClass(node, 'd-pslope-node').filter((c) => c.localName === 'circle');
  // The three left nodes share the same cx (left axis); their cy must differ.
  const leftXs = nodes.map((n) => num(n, 'cx'));
  const minX = Math.min(...leftXs);
  const leftNodes = nodes.filter((n) => Math.abs(num(n, 'cx') - minX) < 0.01);
  assert(leftNodes.length === 3, 'three left-column nodes');
  const ys = leftNodes.map((n) => num(n, 'cy')).sort((p, q) => p - q);
  for (let i = 1; i < ys.length; i++) {
    assert(ys[i] - ys[i - 1] > 1.0, `coincident left nodes must be offset, saw ${ys[i - 1]} & ${ys[i]}`);
  }
});

test('jitterColumn: spreads a coincident bucket, leaves singletons alone', () => {
  const out = svg.jitterColumn([100, 100, 100, 250], 4);
  // the three 100s spread around 100; the 250 stays put.
  assertEqual(out[3], 250, 'isolated value unchanged');
  const trio = [out[0], out[1], out[2]].sort((a, b) => a - b);
  assert(trio[2] - trio[0] > 4, 'the coincident trio fans out');
});

test('pairedSlopegraph: empty input renders a labelled empty mark', () => {
  const node = svg.pairedSlopegraph({ series: [] });
  assert(findClass(node, 'd-empty-label').length === 1, 'honest empty');
});

// ===================================================================
// THEME 4 primitives — alternative styles
// ===================================================================

test('bracketMini: champion + challengers + a winner seat', () => {
  const node = svg.bracketMini({ champion: 'v0', challengers: [{ id: 'v1' }, { id: 'v2' }], winner: 'v0' });
  assert(findClass(node, 'd-bracket-seat').length >= 3, 'seats for champ + challengers + final');
  assert(findClass(node, 'd-champ').length >= 1, 'champion marked');
  assert(findClass(node, 'd-win').length >= 1, 'a winning seat');
});

test('roundRobinMatrix: N×N grid with a diagonal; row-wins coloured', () => {
  const node = svg.roundRobinMatrix({ ids: ['v0', 'v1', 'v2'], lossById: { v0: 70, v1: 146, v2: 72 } });
  assert(findClass(node, 'd-rr-diag').length === 3, '3 diagonal self-cells');
  // off-diagonal = 3*3 - 3 = 6 cells
  assert(findClass(node, 'd-rr-cell').length === 6, '6 off-diagonal pairing cells');
});

test('raceLanes: one dot per runner; eliminated runners read bad', () => {
  const node = svg.raceLanes({ runners: [
    { id: 'v0', loss: 70, eliminated: false },
    { id: 'v1', loss: 146, eliminated: true },
  ], cut: 100 });
  const dots = all(node, 'circle');
  assertEqual(dots.length, 2, 'one dot per runner');
  assert(findClass(node, 'd-race-cut').length === 1, 'an elimination cut line');
});

// ===================================================================
// Router — the new routes
// ===================================================================

test('router: lifecycle + run/<gen>/<entry> parse and round-trip', () => {
  assertEqual(router.parseRoute('#/D/lifecycle').view, 'lifecycle');
  const lc = router.parseRoute('#/D/lifecycle/v1');
  assertEqual(lc.params.gen, 'v1');
  const r = router.parseRoute('#/D/run/v1/waffles_single');
  assertEqual(r.view, 'run');
  assertEqual(r.params.gen, 'v1');
  assertEqual(r.params.entry, 'waffles_single');
  // round-trip through href
  const h = router.href('run', { gen: 'v2', entry: 'q3_metrics_outline' });
  const back = router.parseRoute(h);
  assertEqual(back.params.gen, 'v2');
  assertEqual(back.params.entry, 'q3_metrics_outline');
});

// ===================================================================
// Views — render under a stubbed fetch over the live data shapes
// ===================================================================

const EPOCH_ID = '2026-05-30_e0';

globalThis.fetch = async (path) => ({ ok: true, async json() { return mockJsonFor(path); } });

function mockJsonFor(path) {
  if (path.includes('/api/lineage')) {
    return { generations: [
      { generation_id: 'v0', epoch_id: EPOCH_ID, parent_generation_id: '', promoted: true },
      { generation_id: 'v1', epoch_id: EPOCH_ID, parent_generation_id: 'v0', promoted: false },
      { generation_id: 'v2', epoch_id: EPOCH_ID, parent_generation_id: 'v0', promoted: false },
    ] };
  }
  if (/\/api\/epoch$/.test(path)) {
    return {
      epoch_id: EPOCH_ID, closed: false, goal: 'Tighten the planner.', brief: '# Goal\nTighten it.',
      board: [
        { entry_id: 'waffles_single', kind: 'single_turn', input_preview: 'Make a presentation about waffles.', expectation_kind: 'predicate', budget_s: 180, weight: 1.0, tags: ['smoke'] },
        { entry_id: 'q3_metrics_outline', kind: 'single_turn', input_preview: 'Outline a deck.', expectation_kind: 'predicate', budget_s: 180, weight: 1.0, tags: [] },
        { entry_id: 'picky_stakeholder_emulated', kind: 'multi_turn_emulated', input_preview: null, expectation_kind: null, budget_s: 360, weight: 1.0, tags: [] },
      ],
      experiments: [
        { generation_id: 'v0', parent_generation_id: null, outcome: { decision: 'baseline' }, hypothesis: {} },
        { generation_id: 'v1', parent_generation_id: 'v0', outcome: { decision: 'rejected', rejection_reason: 'loss rose' }, hypothesis: { core_idea: 'enforce structure' } },
        { generation_id: 'v2', parent_generation_id: 'v0', outcome: { decision: 'rejected' }, hypothesis: {} },
      ],
      delta_scalar_summary: { champion_spine: 0 },
    };
  }
  if (path.includes('/api/score-trajectory')) {
    return { epoch_id: EPOCH_ID, points: [
      { generation_id: 'v0', scalar: 70.94 }, { generation_id: 'v1', scalar: 146.65 }, { generation_id: 'v2', scalar: 72.45 },
    ] };
  }
  if (path.includes('/per-entry')) {
    // /api/generation/{e}/{g}/per-entry
    const gen = path.includes('/v1/') ? 'v1' : path.includes('/v2/') ? 'v2' : 'v0';
    const base = { v0: 60.5, v1: 90.0, v2: 62.0 }[gen];
    return { epoch_id: EPOCH_ID, generation_id: gen, entries: [
      { entry_id: 'waffles_single', run_id: gen + '-waffles', drift_loss: base, pass_fail: 0, runtime_ms: 180000, wall_clock_budget_exceeded: gen === 'v1' },
      { entry_id: 'q3_metrics_outline', run_id: gen + '-q3', drift_loss: base + 3, pass_fail: 0, runtime_ms: 120000, wall_clock_budget_exceeded: false },
      { entry_id: 'picky_stakeholder_emulated', run_id: gen + '-picky', drift_loss: base + 45, pass_fail: 0, runtime_ms: 360000, wall_clock_budget_exceeded: true },
    ] };
  }
  if (path.includes('/expectations')) {
    return { epoch_id: EPOCH_ID, generation_id: 'v1', entry_id: 'waffles_single',
      outcomes: [{ kind: 'predicate', passed: false, detail: 'predicate returned False', judge_name: null, score: null }] };
  }
  if (path.includes('/per-judge')) {
    return { judges: [{ judge_name: 'incorporates_feedback', weighted_loss: 27.0, raw_loss: 27.0, run_count: 1, weight: 1.0 }] };
  }
  if (path.includes('/api/conversation/')) {
    return { run_id: 'r', complete: true, event_count: 2,
      turns: [{ seq: 1, role: 'user', text: 'Make a deck.', run_index: 1 }, { seq: 2, role: 'agent', agent: 'planner', text: 'Working.', tool_calls: [{ name: 'write' }], run_index: 1 }],
      annotations: [{ kind: 'drift', summary: 'off-topic', anchor_seq: 2 }] };
  }
  if (path.includes('/api/tournaments')) {
    return { epoch_id: EPOCH_ID, champion_lineage: ['v0'], matchups: [
      { champion: 'v0', challenger: 'v1', decision: 'rejected', delta_scalar: 75.71 },
      { champion: 'v0', challenger: 'v2', decision: 'rejected', delta_scalar: 1.51 },
    ] };
  }
  if (path.includes('/api/matchup-grid/')) {
    const chall = path.includes('/v1') ? 'v1' : 'v2';
    return { epoch_id: EPOCH_ID, champion: 'v0', challenger: chall, entry_grid: [
      { entry_id: 'q3_metrics_outline', parent_drift_loss: 71.0, child_drift_loss: 63.5, delta: -7.5, verdict: 'improved', won_by: chall },
      { entry_id: 'picky_stakeholder_emulated', parent_drift_loss: 60.5, child_drift_loss: 642.5, delta: 582.0, verdict: 'regressed', won_by: 'v0' },
      { entry_id: 'waffles_single', parent_drift_loss: 60.5, child_drift_loss: 60.5, delta: 0.0, verdict: 'flat', won_by: null },
    ], scalar: { parent: 70.94, child: 146.65, delta: 75.71, components: {} } };
  }
  if (path.includes('/api/workspace')) {
    return { current_epoch_id: EPOCH_ID, epochs: [{ epoch_id: EPOCH_ID, goal: 'Tighten.', best_scalar: 70.94, generation_count: 3, promoted_count: 1, closed: false }] };
  }
  if (path.includes('/api/health-report')) return { healthy: true, findings: [] };
  if (path.includes('/diff')) return { files: [] };
  return {};
}

// Bust the variant's module cache between view renders so each test sees
// fresh fetches against the stub.
const data = await import('../js/variants/D/data.js');
function fresh() { data.invalidate(); }

const ctxStub = { navigate: () => {} };
function host() { return document.createElement('section'); }

const lifecycle = await import('../js/variants/D/views/lifecycle.js');
const bench = await import('../js/variants/D/views/bench.js');
const runView = await import('../js/variants/D/views/run.js');
const tournament = await import('../js/variants/D/views/tournament.js');

test('lifecycle view: a sparkbar small multiple per candidate + a lineage bumps', async () => {
  fresh();
  const h = host();
  await lifecycle.render(h, ctxStub, {});
  const cards = findClass(h, 'd-lifecycle-card');
  assertEqual(cards.length, 3, 'one small multiple per candidate (v0, v1, v2)');
  assert(findClass(h, 'd-sparkbar').length >= 3, 'each candidate has a loss-profile sparkbar');
  assert(findClass(h, 'd-bumps').length === 1, 'the lineage bumps chart');
  // the crowned candidate carries a promoted glyph somewhere in the strip
  assert(findClass(h, 'd-verdict-glyph').length >= 1, 'gate-verdict glyphs present');
});

test('boards (bench) view: a trellis cell per board entry, sorted, shared scale', async () => {
  fresh();
  const h = host();
  await bench.render(h, ctxStub);
  const cells = findClass(h, 'd-trellis-cell');
  assertEqual(cells.length, 3, 'one micro-chart per board entry');
  // multi-turn entry sorts before single-turn (KIND_ORDER)
  const firstId = text(findClass(cells[0], 'd-trellis-id')[0]);
  assertEqual(firstId, 'picky_stakeholder_emulated', 'emulated multi-turn sorts first');
  assert(findClass(h, 'd-sparkbar').length === 3, 'one sparkbar per entry');
  assert(findClass(h, 'd-genrow').length === 3, 'a per-generation pass/fail row per entry');
});

test('scoring view (depth 1): sorted value dot-plot with champion reference', async () => {
  fresh();
  const h = host();
  await runView.render(h, ctxStub, { gen: 'v1' });
  assert(findClass(h, 'd-valdot').length === 1, 'the per-board scoring dot-plot');
  assert(findClass(h, 'd-ref-rule').length === 1, 'a champion reference line');
  // worst entry sorts first (descending loss): picky (135) > q3 (93) > waffles (90)
  const labels = findClass(h, 'd-dot-label').map(text);
  assert(labels[0].includes('picky'), 'worst entry sorts to the top');
});

test('scoring view (depth 2/3): entry detail shows expectations, judges, transcript panel', async () => {
  fresh();
  const h = host();
  await runView.render(h, ctxStub, { gen: 'v1', entry: 'waffles_single' });
  assert(findClass(h, 'd-expect-row').length >= 1, 'expectation outcome rows');
  assert(findClass(h, 'd-expect-dot').length >= 1, 'a pass/fail expectation dot');
  assert(findClass(h, 'd-vbars').length === 1, 'per-judge bars');
  // depth 3: the transcript panel is present (lazy — collapsed by default)
  const details = all(h, 'details');
  assert(details.length >= 1, 'a collapsible transcript panel');
  assert(text(h).toLowerCase().includes('transcript'), 'transcript section labelled');
});

test('match-ups view: gauntlet bumps + a paired slopegraph per round + 3 illustrative styles', async () => {
  fresh();
  const h = host();
  await tournament.render(h, ctxStub);
  assert(findClass(h, 'd-bumps').length === 1, 'the real gauntlet ladder (bumps)');
  const pslopes = findClass(h, 'd-pslope');
  assertEqual(pslopes.length, 2, 'one paired slopegraph per matchup (v0→v1, v0→v2)');
  // the alternative-styles section, clearly illustrative
  assert(findClass(h, 'd-illustrative-banner').length === 1, 'an honest illustrative banner');
  assert(findClass(h, 'd-bracket').length === 1, 'a bracket diagram');
  assert(findClass(h, 'd-rrmatrix').length === 1, 'a round-robin matrix');
  assert(findClass(h, 'd-race').length === 1, 'race lanes');
});

test('match-ups view: the paired slopegraph for v0→v1 does not collide on coincident champion values', async () => {
  fresh();
  const h = host();
  await tournament.render(h, ctxStub);
  // The v0→v1 grid has two entries at champion loss 60.5 (waffles, picky)
  // plus q3 at 71 — the two coincident ones must be offset.
  const pslope = findClass(h, 'd-pslope')[0];
  const leftNodes = findClass(pslope, 'd-pslope-node').filter((c) => c.localName === 'circle');
  const xs = leftNodes.map((n) => num(n, 'cx'));
  const minX = Math.min(...xs);
  const atLeft = leftNodes.filter((n) => Math.abs(num(n, 'cx') - minX) < 0.01).map((n) => num(n, 'cy'));
  // at least two left nodes share the champion-axis column; if two had the
  // SAME loss they must now sit at different y.
  const rounded = atLeft.map((y) => Math.round(y));
  const uniq = new Set(rounded);
  assert(uniq.size === rounded.length, 'coincident champion-value nodes are de-collided on the real grid');
});

test('views degrade gracefully when the epoch is absent', async () => {
  // Point fetch at empties so /api/epoch yields no epoch_id.
  const saved = globalThis.fetch;
  globalThis.fetch = async () => ({ ok: true, async json() { return {}; } });
  fresh();
  const h1 = host(); await lifecycle.render(h1, ctxStub, {});
  const h2 = host(); await bench.render(h2, ctxStub);
  const h3 = host(); await runView.render(h3, ctxStub, {});
  const h4 = host(); await tournament.render(h4, ctxStub);
  for (const h of [h1, h2, h3, h4]) {
    assert(findClass(h, 'd-empty').length >= 1, 'an honest empty state, no throw');
  }
  globalThis.fetch = saved;
});

await run();
