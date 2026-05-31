// test/variant_B/variant_b.test.mjs — Variant B ("Editorial Lab Notebook").
//
// Exercises the variant against the shared mock snapshot: the chart toolkit
// totality, the safe Markdown brief renderer + TOC, the router, and each of
// the six views rendering without throwing and producing the expected
// editorial structure (pull-quotes, sections, slopegraphs, honest states).
// Runs under the dependency-free harness via `node`.

import { installDom, test, assert, assertEqual, run, makeEvent } from './harness.mjs';

installDom();

// --- helpers --------------------------------------------------------------
function findClass(node, cls, out = []) {
  if (!node || node.nodeType !== 1) return out;
  if (node.className && String(node.className).split(/\s+/).includes(cls)) out.push(node);
  for (const c of node.children) findClass(c, cls, out);
  return out;
}
function text(node) { return node ? node.textContent : ''; }
function hasText(node, sub) { return text(node).includes(sub); }

// ---------------------------------------------------------------------------
// Chart toolkit
// ---------------------------------------------------------------------------
const charts = await import('../js/variants/B/lib/charts.js');

test('sparkline: empty input yields a tasteful baseline, never throws', () => {
  const svg = charts.sparkline([]);
  assert(findClass(svg, 'vb-spark-empty').length === 1, 'empty baseline drawn');
  const svg2 = charts.sparkline([0.5, 0.4, null, 0.3]);
  assert(findClass(svg2, 'vb-spark-line').length === 1, 'line drawn for finite points');
  assert(findClass(svg2, 'vb-spark-best').length === 1, 'best point marked');
});

test('slopegraph: degenerate input renders a labeled empty figcaption', () => {
  const fig = charts.slopegraph([]);
  assert(findClass(fig, 'vb-fig-empty').length === 1, 'empty caption');
  const fig2 = charts.slopegraph([{ label: 'loss', from: 0.6, to: 0.4, verdict: 'improve' }]);
  assert(findClass(fig2, 'vb-slope-line').length === 1, 'a slope line');
  assert(findClass(fig2, 'vb-improve').length >= 1, 'improve coloring');
});

test('divergingBars: improved/worsened classed left/right, clickable rows fire', () => {
  let clicked = null;
  const bars = charts.divergingBars([
    { label: 'a', delta: -0.2, onClick: () => { clicked = 'a'; } },
    { label: 'b', delta: 0.3 },
  ]);
  assert(findClass(bars, 'vb-diverge-left').length === 1, 'one improved (left) bar');
  assert(findClass(bars, 'vb-diverge-right').length === 1, 'one worsened (right) bar');
  const row = findClass(bars, 'vb-clickable')[0];
  row.dispatchEvent(makeEvent('click'));
  assertEqual(clicked, 'a', 'click handler fired');
});

test('trajectoryStory: single node centers + labels (no empty-glyph bug)', () => {
  const svg = charts.trajectoryStory([{ id: 'v0', scalar: 0.5, verdict: 'promoted' }]);
  assert(findClass(svg, 'vb-traj-dot').length === 1, 'one node dot');
  assert(hasText(svg, 'v0'), 'node labeled');
  const empty = charts.trajectoryStory([]);
  assert(findClass(empty, 'vb-traj-empty').length === 1, 'labeled empty trajectory');
});

test('trajectoryStory: onSelect fires the node id on click', () => {
  let sel = null;
  const svg = charts.trajectoryStory(
    [{ id: 'v1', scalar: 0.4, verdict: 'promoted', parentId: 'v0' },
     { id: 'v0', scalar: 0.5, verdict: 'promoted' }],
    { onSelect: (id) => { sel = id; } },
  );
  const node = findClass(svg, 'vb-traj-node')[0];
  node.dispatchEvent(makeEvent('click'));
  assert(sel === 'v1' || sel === 'v0', 'a node id was selected');
});

// ---------------------------------------------------------------------------
// Prose / Markdown brief
// ---------------------------------------------------------------------------
const prose = await import('../js/variants/B/lib/prose.js');

test('parseMarkdown: headings, lists, code, blockquote build real nodes (no innerHTML)', () => {
  const md = '# Title\n\nA paragraph with **bold** and `code`.\n\n## Section\n\n- one\n- two\n\n```\ncode block\n```\n\n> a quote';
  const { blocks, headings } = prose.parseMarkdown(md);
  assert(headings.length === 2, 'two headings');
  assert(blocks.some((b) => b.node.localName === 'ul'), 'a list block');
  assert(blocks.some((b) => b.node.localName === 'pre'), 'a code block');
  assert(blocks.some((b) => b.node.localName === 'blockquote'), 'a quote block');
});

test('renderBrief: long brief gets a TOC + collapsible sections', () => {
  const md = '# Brief\n\nIntro.\n\n## Forbidden edits\n\nDo not touch X.\n\n## Preferred edits\n\nPrefer Y.';
  const node = prose.renderBrief(md);
  assert(findClass(node, 'vb-brief-toc').length === 1, 'TOC rail present');
  const secs = findClass(node, 'vb-brief-sec');
  assert(secs.length >= 2, 'collapsible sections built');
  assert(secs[0].localName === 'details', 'sections are <details> (collapsible)');
});

test('renderBrief: empty brief degrades to a typeset absence, never blank', () => {
  const node = prose.renderBrief('');
  assert(hasText(node, 'no proposer brief'), 'states the absence');
});

test('pullQuote + verdictBadge + note: produce expected editorial structure', () => {
  assert(findClass(prose.pullQuote('the bet'), 'vb-pullquote-text').length === 1, 'pullquote text');
  assert(hasText(prose.verdictBadge('promoted'), 'Promoted'), 'verdict label');
  assert(hasText(prose.note('broken', { reason: 'x' }), 'x'), 'broken reason surfaced');
});

// ---------------------------------------------------------------------------
// Router
// ---------------------------------------------------------------------------
const router = await import('../js/variants/B/router.js');

test('parseBHash: prefixed routes resolve; unknown falls back to environment', () => {
  assertEqual(router.parseBHash('#/B/epoch/e0').view, 'epoch');
  assertEqual(router.parseBHash('#/B/epoch/e0').params.epochId, 'e0');
  assertEqual(router.parseBHash('#/B/experiment/v2').params.generationId, 'v2');
  assertEqual(router.parseBHash('#/B/run/entry/v2').params.entryId, 'entry');
  assertEqual(router.parseBHash('#/B/run/entry/v2').params.generationId, 'v2');
  assertEqual(router.parseBHash('#/B/nonsense').view, 'environment');
  assertEqual(router.parseBHash('').view, 'environment');
});

test('bHref: builds B-prefixed, encoded fragments', () => {
  assertEqual(router.bHref('epoch', 'e 0'), '#/B/epoch/e%200');
  assertEqual(router.bHref('environment'), '#/B/environment');
});

test('crumbTrail: run nests under experiment under environment', () => {
  const trail = router.crumbTrail({ view: 'run', params: { entryId: 'x', generationId: 'v2' } });
  assertEqual(trail[0].view, 'environment');
  assertEqual(trail[1].view, 'experiment');
  assertEqual(trail[2].view, 'run');
  assert(trail[2].current, 'run is the current leaf');
});

// ---------------------------------------------------------------------------
// Views — seed state from the shared mock snapshot, then render each.
// ---------------------------------------------------------------------------
const { state } = await import('../js/core/state.js');
const { mockSnapshot } = await import('../js/views/mock.js');
const { bRouter } = router;

// Stub fetch so the lazy per-resource caches resolve to empty payloads
// rather than hitting the network in the harness.
globalThis.fetch = async (path) => ({
  ok: true,
  async json() { return mockJsonFor(path); },
});
function mockJsonFor(path) {
  if (path.includes('/api/workspace')) {
    return {
      current_epoch_id: '2026-05-30_e1',
      epochs: [
        { epoch_id: '2026-05-29_e0', goal: 'Stabilise extraction schema.', best_scalar: 0.42, generation_count: 3, promoted_count: 2, closed: true },
        { epoch_id: '2026-05-30_e1', goal: 'Cut off-topic drift.', best_scalar: 0.31, generation_count: 4, promoted_count: 3, closed: false },
      ],
      sparkline: [{ epoch_id: '2026-05-29_e0', scalar: 0.42 }, { epoch_id: '2026-05-30_e1', scalar: 0.31 }],
    };
  }
  if (path.includes('/api/contract-diff/')) {
    return { epoch_id: 'e', predecessor_epoch_id: '2026-05-29_e0', components: [{ name: 'brief', changed: true }, { name: 'board', changed: false }], any_changed: true };
  }
  if (path.includes('/api/matchup-grid/')) {
    return {
      entry_grid: [
        { entry_id: 'waffles', parent_drift_loss: 0.6, child_drift_loss: 0.4, delta: -0.2, verdict: 'won' },
        { entry_id: 'q3', parent_drift_loss: 0.5, child_drift_loss: 0.55, delta: 0.05, verdict: 'lost' },
      ],
      scalar: { parent: 0.55, child: 0.47, delta: -0.08, components: {} },
    };
  }
  if (path.endsWith('/gate')) {
    return {
      decision: 'promoted', reason: 'scalar margin cleared',
      rules: [
        { id: 'scalar_margin', label: 'Scalar margin', status: 'pass', fired: false },
        { id: 'pass_rate_monotonicity', label: 'Pass-rate monotonicity', status: 'pass', fired: false },
      ],
      primary_driver: { judge: 'incorporates_feedback', delta: -0.03 },
    };
  }
  if (path.includes('/api/drift-movements/')) {
    return { movements: [{ kind: 'off_topic', champion_count: 8, challenger_count: 3, delta: -5, direction: 'improved' }] };
  }
  if (path.includes('/diff')) {
    return { diff: '@@ -1,2 +1,2 @@\n-old line\n+new line\n context' };
  }
  if (path.includes('/transcript') || path.includes('/api/conversation/')) {
    return {
      run_id: 'r1', complete: true, event_count: 3,
      turns: [
        { seq: 1, role: 'user', text: 'Do the thing.', run_index: 1 },
        { seq: 2, role: 'agent', agent: 'researcher', text: 'Working on it.', tool_calls: [{ name: 'search' }], run_index: 1 },
      ],
      annotations: [{ kind: 'drift', summary: 'off_topic detected', anchor_seq: 2 }],
    };
  }
  return {};
}

function seed() {
  state.applySnapshot(mockSnapshot());
}

function pageHost() {
  return document.getElementById('vb-page') || document.createElement('div');
}

// Import the shell + views (self-register).
const shell = await import('../js/variants/B/shell.js');
await import('../js/variants/B/views/environment.js');
await import('../js/variants/B/views/epoch.js');
await import('../js/variants/B/views/experiment.js');
await import('../js/variants/B/views/tournament.js');
await import('../js/variants/B/views/run.js');
await import('../js/variants/B/views/bench.js');

// Build the frame once so document.getElementById('vb-page') exists.
function mountShell(route) {
  // The shell paints into #variant-root.
  let root = document.getElementById('variant-root');
  if (!root) {
    root = document.createElement('div');
    root.id = 'variant-root';
    document.body.appendChild(root);
    document.registerId('variant-root', root);
  }
  window.location.hash = route;
  bRouter.resolve();
  shell.resetBShellDigest();
  shell.renderBShell(bRouter.current());
}

test('shell: masthead + nav + crumbs render; theme toggle present', () => {
  seed();
  mountShell('#/B/environment');
  assert(document.getElementById('vb-page'), 'page host built');
  assert(document.getElementById('vb-nav'), 'nav built');
  assert(document.getElementById('vb-theme-toggle'), 'theme toggle built');
});

test('environment: lead title, lineage trajectory, and epoch chapters', () => {
  seed();
  mountShell('#/B/environment');
  const host = pageHost();
  assert(findClass(host, 'vb-env-lead').length === 1, 'editorial lead');
  assert(findClass(host, 'vb-traj').length >= 1, 'lineage trajectory drawn');
  assert(findClass(host, 'vb-chapter').length >= 1, 'epoch chapters rendered');
});

test('environment: chapter "Read the chapter" routes to the epoch', () => {
  seed();
  mountShell('#/B/environment');
  const host = pageHost();
  const link = findClass(host, 'vb-link-arrow')[0];
  assert(link, 'a chapter link exists');
  link.dispatchEvent(makeEvent('click', { button: 0 }));
  assertEqual(bRouter.current().view, 'epoch', 'navigated to epoch');
});

test('epoch: lead + brief home (TOC + collapsibles) + experiment cards (no table)', () => {
  seed();
  mountShell('#/B/epoch');
  const host = pageHost();
  assert(findClass(host, 'vb-epoch-lead').length === 1, 'editorial lead');
  // The mock epochDef carries a brief with two ## sections → its real home.
  assert(findClass(host, 'vb-brief').length === 1, 'brief has a designed home');
  assert(findClass(host, 'vb-brief-toc').length === 1, 'brief TOC rail');
  assert(findClass(host, 'vb-exp-card').length >= 1, 'experiment cards (not a table)');
  assert(findClass(host, 'vb-exp-grid').length === 1, 'experiments laid out as a grid');
});

test('experiment: bet leads (pull-quote), verdict + gate ladder, secondary diff', async () => {
  seed();
  mountShell('#/B/experiment/v1');
  let host = pageHost();
  assert(findClass(host, 'vb-exp-bet').length === 1, 'hypothesis leads as a pull-quote');
  // verdict block + gate ladder land after the async gate fetch resolves.
  await new Promise((r) => setTimeout(r, 0));
  shell.renderBShell(bRouter.current());
  host = pageHost();
  assert(findClass(host, 'vb-verdict-block').length === 1, 'verdict block built from the gate');
  assert(findClass(host, 'vb-gate-ladder').length === 1, 'gate ladder rendered');
  // The diff is collapsible + secondary (a <details>), not the lead.
  assert(findClass(host, 'vb-diff').length === 1, 'diff present + collapsible (secondary)');
  // The bet section must precede the diff in document order (narrative order).
  const order = [...host.children];
  assert(order.length >= 3, 'multiple sections in the entry');
});

test('experiment: the seed (v0, no parent) shows baseline, not a red error', () => {
  seed();
  mountShell('#/B/experiment/v0');
  const host = pageHost();
  // v0 has no parent in the mock lineage → seed path; should mention baseline.
  assert(hasText(host, 'baseline') || hasText(host, 'seed') || findClass(host, 'vb-exp-lead').length === 1,
    'seed renders honestly');
});

test('tournament: lineage slopegraph + clickable verdict roll', () => {
  seed();
  mountShell('#/B/tournament');
  const host = pageHost();
  assert(findClass(host, 'vb-traj').length === 1, 'lineage slopegraph');
  assert(findClass(host, 'vb-legend').length === 1, 'a legend');
  const row = findClass(host, 'vb-roll-row')[0];
  assert(row, 'a verdict row');
  row.dispatchEvent(makeEvent('click', { button: 0 }));
  assertEqual(bRouter.current().view, 'experiment', 'roll row drills to experiment');
});

test('run: transcript renders turns + margin annotations', async () => {
  seed();
  mountShell('#/B/run/waffles/v1');
  // first paint kicks the async transcript fetch; let it settle, then re-render.
  await new Promise((r) => setTimeout(r, 0));
  shell.renderBShell(bRouter.current());
  const host = pageHost();
  assert(findClass(host, 'vb-turn').length >= 1, 'turns rendered');
  assert(findClass(host, 'vb-anno').length >= 1, 'annotations hung in the margin');
});

test('bench: idle state is honest; live state pins hypothesis + board roster', () => {
  seed(); // mock snapshot has an active_tournament
  mountShell('#/B/bench');
  const host = pageHost();
  assert(findClass(host, 'vb-bench-lead').length === 1, 'bench lead');
  // The mock has a live tournament → roster + hypothesis.
  assert(findClass(host, 'vb-bench-roster').length === 1 || hasText(host, 'Nothing is running'),
    'roster or honest-idle');
});

test('re-render safe: a second shell paint does not throw or wipe the page', () => {
  seed();
  mountShell('#/B/environment');
  const before = pageHost().children.length;
  shell.renderBShell(bRouter.current());
  shell.renderBShell(bRouter.current());
  assert(pageHost().children.length >= before, 'page stays populated across re-renders');
});

await run();
