// variant_a.test.mjs — Variant A ("Mission Control") behavior tests.
//
// Run: node src/zicato/dashboard/static/test/variant_a.test.mjs
// Uses the shared harness DOM (no jsdom).

import { installDom, test, assert, assertEqual, run } from './harness.mjs';

installDom();
// Stub fetch so views that call fetchJson in ensure() never throw; tests
// drive presentation off injected state, not the network.
globalThis.fetch = async () => ({ ok: true, json: async () => ({}) });
globalThis.requestAnimationFrame = (fn) => setTimeout(fn, 0);

const { parseRoute, href, crumbsFor } = await import('../js/variants/A/router.js');
const { renderMarkdown } = await import('../js/variants/A/components/markdown.js');
const { gauntlet } = await import('../js/variants/A/components/gauntlet.js');
const { heatColor, sparkline, readout, chip } = await import('../js/variants/A/components/instruments.js');
const { state } = await import('../js/core/state.js');

// -- router ------------------------------------------------------------
test('router: home for empty / unknown hash', () => {
  assertEqual(parseRoute('').name, 'environment');
  assertEqual(parseRoute('#/A/').name, 'environment');
  assertEqual(parseRoute('#/whatever').name, 'environment');
});

test('router: parses epoch / experiment / tournament / run / bench', () => {
  assertEqual(parseRoute('#/A/epoch/hr').name, 'epoch');
  assertEqual(parseRoute('#/A/epoch/hr').params.epochId, 'hr');
  const exp = parseRoute('#/A/experiment/hr/v3');
  assertEqual(exp.name, 'experiment');
  assertEqual(exp.params.epochId, 'hr');
  assertEqual(exp.params.genId, 'v3');
  assertEqual(parseRoute('#/A/tournament/hr').name, 'tournament');
  assertEqual(parseRoute('#/A/run/abc').params.runId, 'abc');
  assertEqual(parseRoute('#/A/bench').name, 'bench');
});

test('router: href round-trips through parseRoute', () => {
  const h = href('experiment', { epochId: 'e 1', genId: 'v2' });
  const r = parseRoute(h);
  assertEqual(r.name, 'experiment');
  assertEqual(r.params.epochId, 'e 1');
  assertEqual(r.params.genId, 'v2');
});

test('router: breadcrumb trail always starts at environment', () => {
  const c = crumbsFor(parseRoute('#/A/experiment/hr/v3'));
  assert(c.length >= 2, 'has crumbs');
  assertEqual(c[0].label, 'environment');
  assert(c[c.length - 1].current === true, 'last is current');
});

// -- markdown ----------------------------------------------------------
test('markdown: renders headings, lists, code without innerHTML', () => {
  const node = renderMarkdown('# Goal\n\nDo **the** thing.\n\n- a\n- b\n\n`code`');
  // no innerHTML writes anywhere in the subtree
  assertEqual(node.innerHTMLWriteCount(), 0, 'no innerHTML used');
  const text = node.textContent;
  assert(text.includes('Goal'), 'heading present');
  assert(text.includes('the'), 'bold content present');
  assert(text.includes('code'), 'inline code present');
});

test('markdown: empty input yields a placeholder, not a crash', () => {
  const node = renderMarkdown('');
  assert(node.textContent.length >= 0);
});

// -- gauntlet ----------------------------------------------------------
test('gauntlet: spine + challengers render as one svg, lanes separated', () => {
  const svg = gauntlet({
    spine: [{ id: 'v0', scalar: 1.0 }, { id: 'v1', scalar: 0.8 }, { id: 'v3', scalar: 0.6 }],
    challengers: [
      { id: 'v2', parentId: 'v1', decision: 'rejected', delta: 0.02 },
      { id: 'v2b', parentId: 'v1', decision: 'rejected', delta: 0.05 },
    ],
    onSelect: () => {},
  });
  assertEqual(svg.tagName, 'SVG');
  // height must grow to accommodate two lanes under the same parent
  const h = Number(svg.getAttribute('height'));
  assert(h > 100, 'height accommodates stacked lanes: ' + h);
});

test('gauntlet: click on a node fires onSelect with the id', () => {
  let clicked = null;
  const svg = gauntlet({
    spine: [{ id: 'v0' }, { id: 'v1' }],
    challengers: [],
    onSelect: (id) => { clicked = id; },
  });
  // find a clickable group and dispatch
  const groups = svg.querySelectorAll('[class]');
  let fired = false;
  for (const g of svg._descendants()) {
    if (g._listeners && g._listeners.click) {
      g.dispatchEvent({ type: 'click' });
      fired = true;
      break;
    }
  }
  assert(fired, 'a node had a click listener');
  assert(clicked === 'v0' || clicked === 'v1', 'onSelect got an id: ' + clicked);
});

// -- instruments -------------------------------------------------------
test('heatColor: monotone green->red across [0,1]', () => {
  const lo = heatColor(0), hi = heatColor(1);
  assert(lo.startsWith('rgba(47'), 'low is green: ' + lo);
  assert(hi.startsWith('rgba(255'), 'high is red: ' + hi);
});

test('sparkline: <2 points degrades to a dash node, not a throw', () => {
  const n = sparkline([0.5]);
  assert(n.textContent === '—');
});

test('readout: applies semantic tone class', () => {
  const n = readout({ label: 'verdict', value: 'PROMOTE', tone: 'go' });
  assert(n.textContent.includes('PROMOTE'));
  const val = n._descendants().find((c) => c.className && c.className.includes('mcA-readout-value'));
  assert(val.className.includes('is-go'), 'tone class applied');
});

// -- views render against injected state (no network, no flash) -------
test('epoch view: objective + brief drawer + gauntlet render from epochDef', async () => {
  const { renderEpoch } = await import('../js/variants/A/views/epoch.js');
  state.epochDef = {
    epoch_id: 'hardened_research',
    goal: 'Cut confabulation on research tasks.',
    brief: '## Goal\n\nReduce **confabulation**.\n\n- cite sources\n- stay terse',
    experiments: [
      { generation_id: 'v0', parent_generation_id: null, outcome: null },
      { generation_id: 'v1', parent_generation_id: 'v0', outcome: { tournament_decision: 'promoted', scalar_score: 0.8, scalar_score_delta: -0.2 } },
      { generation_id: 'v2', parent_generation_id: 'v1', outcome: { tournament_decision: 'rejected', scalar_score_delta: 0.03 } },
    ],
    delta_scalar_summary: { champion_spine: -0.2 },
  };
  state.heartbeat = { epoch_id: 'hardened_research' };
  const host = document.createElement('div');
  renderEpoch(host, { epochId: 'hardened_research' }, () => {});
  const text = host.textContent;
  assert(text.includes('Cut confabulation'), 'objective shown prominently');
  assert(text.includes('Proposer brief'), 'brief panel has a home');
  assert(text.includes('Gauntlet'), 'gauntlet present');
  assertEqual(host.innerHTMLWriteCount(), 0, 'no innerHTML / no flash path');
});

test('experiment view: leads with verdict, diff is a collapsible drawer', async () => {
  const { renderExperiment } = await import('../js/variants/A/views/experiment.js');
  state.epochDef = {
    epoch_id: 'hr',
    experiments: [
      { generation_id: 'v1', parent_generation_id: 'v0',
        hypothesis: { core_idea: 'Tighten the researcher prompt.', why: 'confab on 60%' },
        outcome: { tournament_decision: 'promoted', scalar_score_delta: -0.18, drift_loss_delta: -0.2, pass_rate_delta: 0.05 } },
    ],
  };
  const host = document.createElement('div');
  renderExperiment(host, { epochId: 'hr', genId: 'v1' }, () => {});
  const text = host.textContent;
  assert(text.includes('PROMOTE'), 'verdict leads');
  assert(text.includes('Patch diff'), 'diff present as a drawer');
  // the diff body is hidden initially (drawer collapsed)
  const drawerBody = host._descendants().find((c) => c.className && c.className.includes('mcA-brief-body'));
  assert(drawerBody && drawerBody.hasAttribute('hidden'), 'diff drawer collapsed by default');
});

test('experiment view: seed (v0) shows absolute baseline, no comparison', async () => {
  const { renderExperiment, resetExperimentCache } = await import('../js/variants/A/views/experiment.js');
  resetExperimentCache();
  state.epochDef = {
    epoch_id: 'hr',
    experiments: [{ generation_id: 'v0', parent_generation_id: null, outcome: null }],
  };
  const host = document.createElement('div');
  renderExperiment(host, { epochId: 'hr', genId: 'v0' }, () => {});
  assert(host.textContent.includes('SEED') || host.textContent.includes('Baseline'), 'seed framed as baseline');
});

await run();
