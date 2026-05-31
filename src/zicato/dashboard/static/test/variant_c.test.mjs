// test/variant_c.test.mjs — Variant C ("Causal Flow / diagram-first").
//
// Verifies the variant against the shared harness DOM: the router,
// the collision-free DAG layout, the Sankey layout, and that each hero
// screen paints real content from `state` (objective, brief drawer,
// causal Sankey flow, gauntlet graph) plus honest empty states.

import { installDom, test, run, assert, assertEqual } from './harness.mjs';

installDom();

const { parseRoute, href } = await import('../js/variants/C/router.js');
const prim = await import('../js/variants/C/diagram/primitives.js');
const { layoutSankey } = await import('../js/variants/C/diagram/sankey.js');
const { state } = await import('../js/core/state.js');
const { buildChrome, openDrawer } = await import('../js/variants/C/chrome.js');
const envView = await import('../js/variants/C/views/environment.js');
const epochView = await import('../js/variants/C/views/epoch.js');
const expView = await import('../js/variants/C/views/experiment.js');
const tourView = await import('../js/variants/C/views/tournament.js');
const benchView = await import('../js/variants/C/views/bench.js');

function freshStage() {
  const s = document.createElement('div');
  document.body.appendChild(s);
  return s;
}

function resetState() {
  state.heartbeat = null;
  state.epochDef = null;
  state.epochs = [];
  state.lineage = { generations: [], experiments: [] };
  state.activeRuns = [];
  state.activeTournament = null;
  state.healthReport = null;
  state.connected = false;
  state.connecting = false;
  expView.resetExperimentCaches();
}

// ---- router ---------------------------------------------------------

test('router parses every screen under the #/C prefix', () => {
  assertEqual(parseRoute('#/C/env').view, 'env');
  assertEqual(parseRoute('#/C/epoch/e1').view, 'epoch');
  assertEqual(parseRoute('#/C/epoch/e1').params.epochId, 'e1');
  const ex = parseRoute('#/C/experiment/e1/v3');
  assertEqual(ex.view, 'experiment');
  assertEqual(ex.params.epochId, 'e1');
  assertEqual(ex.params.genId, 'v3');
  assertEqual(parseRoute('#/C/tournament/e1').view, 'tournament');
  assertEqual(parseRoute('#/C/run/r9').params.runId, 'r9');
  assertEqual(parseRoute('#/C/bench').view, 'bench');
});

test('router defaults bare / unknown hashes to the environment map', () => {
  assertEqual(parseRoute('').view, 'env');
  assertEqual(parseRoute('#/').view, 'env');
  assertEqual(parseRoute('#/C/nonsense').view, 'env');
  // The shipped shell owns the un-prefixed space; a non-C hash is env.
  assertEqual(parseRoute('#/epoch/x').view, 'env');
});

test('href round-trips through parseRoute', () => {
  const h = href('experiment', { epochId: 'ep a', genId: 'v2' });
  const r = parseRoute(h);
  assertEqual(r.view, 'experiment');
  assertEqual(r.params.epochId, 'ep a');
  assertEqual(r.params.genId, 'v2');
});

// ---- DAG layout: collision-free guarantee ---------------------------

test('layoutDag gives every node a distinct (col,row) cell', () => {
  const nodes = [
    { id: 'v0', parent: null },
    { id: 'v1', parent: 'v0' },
    { id: 'v2', parent: 'v1' },
    { id: 'v3', parent: 'v1' }, // a branch off v1
    { id: 'v4', parent: 'v2' },
  ];
  const { pos } = prim.layoutDag(nodes);
  const cells = new Set();
  for (const n of nodes) {
    const p = pos.get(n.id);
    assert(p != null, `missing position for ${n.id}`);
    const key = p.col + ',' + p.row;
    assert(!cells.has(key), `collision at ${key} (${n.id})`);
    cells.add(key);
  }
  // A child is strictly to the right of its parent → edges go left→right.
  assert(pos.get('v1').col > pos.get('v0').col, 'child must be deeper than parent');
  assert(pos.get('v3').col === pos.get('v2').col, 'siblings share a column');
});

test('layoutDag is cycle-safe and tolerates orphan parents', () => {
  const nodes = [
    { id: 'a', parent: 'b' },
    { id: 'b', parent: 'a' }, // cycle
    { id: 'c', parent: 'ghost' }, // orphan ref
  ];
  const { pos } = prim.layoutDag(nodes);
  assert(pos.get('a') && pos.get('b') && pos.get('c'), 'all nodes placed despite cycle/orphan');
});

// ---- Sankey layout --------------------------------------------------

test('layoutSankey places three stages left to right and links them', () => {
  const out = layoutSankey({
    patch: [{ id: 'p0', label: 'researcher.instruction' }],
    drift: [{ id: 'd0', label: 'Confab', value: 3 }, { id: 'd1', label: 'Tool', value: 1 }],
    gate: [{ id: 'g', label: 'PROMOTED' }],
    links: [
      { source: 'p0', target: 'd0', value: 3 },
      { source: 'p0', target: 'd1', value: 1 },
      { source: 'd0', target: 'g', value: 3 },
      { source: 'd1', target: 'g', value: 1 },
    ],
  });
  const px = out.nodes.find((n) => n.id === 'p0').x;
  const dx = out.nodes.find((n) => n.id === 'd0').x;
  const gx = out.nodes.find((n) => n.id === 'g').x;
  assert(px < dx && dx < gx, 'patch < drift < gate in x');
  assertEqual(out.links.length, 4, 'all links laid out');
  // Bigger movement → taller drift node.
  const d0 = out.nodes.find((n) => n.id === 'd0').h;
  const d1 = out.nodes.find((n) => n.id === 'd1').h;
  assert(d0 > d1, 'larger magnitude drift node is taller');
});

// ---- chrome ---------------------------------------------------------

test('chrome builds nav for all six screens + a status pill', () => {
  const c = buildChrome();
  const items = c.root.querySelectorAll('[data-view]');
  const views = new Set([...items].map((i) => i.getAttribute('data-view')));
  for (const v of ['env', 'epoch', 'experiment', 'tournament', 'run', 'bench']) {
    assert(views.has(v), `nav missing ${v}`);
  }
  c.setActive('epoch');
  const active = c.root.querySelector('[data-view="epoch"]');
  assert(active.classList.contains('is-active'), 'active nav item is marked');
});

test('openDrawer reveals the drawer and swaps its body', () => {
  const c = buildChrome();
  const body = document.createElement('p');
  body.textContent = 'brief body';
  openDrawer(c, 'Proposer brief', body);
  assertEqual(c.drawer.getAttribute('aria-hidden'), 'false');
  assert(c.drawer.classList.contains('is-open'));
  assert(c.drawerBody.textContent.includes('brief body'));
});

// ---- environment screen ---------------------------------------------

test('environment paints honest empty state with no epochs', () => {
  resetState();
  const stage = freshStage();
  envView.renderEnvironment({ stage, state, onNavigate() {} });
  assert(stage.textContent.toLowerCase().includes('no epochs'),
    'empty environment names the absence of epochs');
});

test('environment renders one lane per epoch and nodes per generation', () => {
  resetState();
  state.lineage = {
    generations: [
      { generation_id: 'v0', epoch_id: 'e1', parent_generation_id: null, promoted: true },
      { generation_id: 'v1', epoch_id: 'e1', parent_generation_id: 'v0', promoted: false },
      { generation_id: 'v2', epoch_id: 'e1', parent_generation_id: 'v0', promoted: true },
    ],
  };
  state.epochs = [{ epoch_id: 'e1', goal: 'harden research' }];
  const stage = freshStage();
  envView.renderEnvironment({ stage, state, onNavigate() {} });
  // Three generation nodes, each an anchor to the experiment screen.
  const nodes = stage.querySelectorAll('[data-cz="env-node"]');
  assertEqual(nodes.length, 3, 'one node per generation');
  const link = nodes[0].getAttribute('href');
  assert(link.startsWith('#/C/experiment/'), 'node links to its causal flow');
});

// ---- epoch screen ---------------------------------------------------

test('epoch makes the OBJECTIVE the headline', () => {
  resetState();
  state.epochDef = { epoch_id: 'e1', goal: 'tighten citation discipline', experiments: [], board: [], brief: '' };
  const stage = freshStage();
  epochView.renderEpoch({ stage, state, chrome: buildChrome(), params: { epochId: 'e1' } });
  const obj = stage.querySelector('[class="cz-objective"]');
  assert(obj != null, 'objective headline present');
  assert(obj.textContent.includes('tighten citation discipline'), 'objective shows the goal');
});

test('epoch brief button opens the brief in the drawer', () => {
  resetState();
  state.epochDef = {
    epoch_id: 'e1', goal: 'g',
    brief: '## Goal\nReduce confabulation.\n\n## Constraints\nDo not regress latency.',
    experiments: [], board: [],
  };
  const chrome = buildChrome();
  const stage = freshStage();
  epochView.renderEpoch({ stage, state, chrome, params: { epochId: 'e1' } });
  // Find the primary brief button and click it.
  const btns = stage.querySelectorAll('[data-cz="brief-btn"]');
  assert(btns.length >= 1, 'a proposer-brief button renders');
  btns[0].dispatchEvent({ type: 'click', _stopped: false, stopPropagation() {}, preventDefault() {} });
  assertEqual(chrome.drawer.getAttribute('aria-hidden'), 'false', 'brief drawer opened');
  assert(chrome.drawerBody.textContent.includes('Reduce confabulation'), 'brief prose surfaced');
  assert(chrome.drawerBody.textContent.includes('Constraints'), 'brief headings surfaced');
});

test('epoch gauntlet places promoted on a spine and rejected on a branch', () => {
  resetState();
  state.epochDef = {
    epoch_id: 'e1', goal: 'g', board: [], brief: '',
    experiments: [
      { generation_id: 'v0', parent_generation_id: null, hypothesis: {}, outcome: null },
      { generation_id: 'v1', parent_generation_id: 'v0', hypothesis: {}, outcome: { tournament_decision: 'promoted', scalar_score_delta: -0.2 } },
      { generation_id: 'v2', parent_generation_id: 'v1', hypothesis: {}, outcome: { tournament_decision: 'rejected', scalar_score_delta: 0.05 } },
    ],
  };
  const stage = freshStage();
  epochView.renderEpoch({ stage, state, chrome: buildChrome(), params: { epochId: 'e1' } });
  const spine = stage.querySelectorAll('[data-spine="1"]');
  // v0 (baseline) + v1 (promoted) on the spine.
  assert(spine.length >= 2, `spine carries promoted+baseline; got ${spine.length}`);
  // v2 is a rejected branch node.
  const all = stage.querySelectorAll('[data-cz="gen-node"]');
  assertEqual(all.length, 3, 'three generation nodes');
});

test('epoch renders an in-flight challenger as a running tip on the spine', () => {
  resetState();
  state.heartbeat = { epoch_id: 'e1', generation_id: 'v2', phase: 'TOURNAMENT RUNNING' };
  state.epochDef = {
    epoch_id: 'e1', goal: 'g', board: [], brief: '',
    experiments: [
      { generation_id: 'v0', parent_generation_id: null, hypothesis: {}, outcome: null },
      { generation_id: 'v1', parent_generation_id: 'v0', hypothesis: {}, outcome: { tournament_decision: 'promoted', scalar_score_delta: -0.2 } },
    ],
  };
  const stage = freshStage();
  epochView.renderEpoch({ stage, state, chrome: buildChrome(), params: { epochId: 'e1' } });
  // v0, v1, and the injected live v2 — three gen nodes, all on the spine.
  const all = stage.querySelectorAll('[data-cz="gen-node"]');
  assertEqual(all.length, 3, 'live tip injected as a node');
  const spineNodes = stage.querySelectorAll('[data-spine="1"]');
  assertEqual(spineNodes.length, 3, 'the in-flight tip sits ON the spine, not as a branch');
});

// ---- experiment screen (the signature Sankey) -----------------------

test('experiment renders patch→drift→gate column headers and a verdict', () => {
  resetState();
  state.epochDef = {
    epoch_id: 'e1', goal: 'g', board: [], brief: '',
    experiments: [{
      generation_id: 'v1', parent_generation_id: 'v0',
      hypothesis: { core_idea: 'tighten researcher prompt', modulating: ['researcher.instruction'] },
      outcome: { tournament_decision: 'promoted', scalar_score_delta: -0.2, drift_loss_delta: -0.3, pass_rate_delta: 0.1 },
    }],
  };
  const stage = freshStage();
  expView.renderExperiment({ stage, state, params: { epochId: 'e1', genId: 'v1' }, chrome: buildChrome(), repaint() {} });
  const heads = stage.querySelectorAll('[class="cz-sankey-col-head"]');
  assertEqual(heads.length, 3, 'three Sankey stage headers');
  const txt = stage.textContent;
  assert(txt.includes('PATCH'), 'PATCH stage labelled');
  assert(txt.includes('DRIFT'), 'DRIFT stage labelled');
  assert(txt.includes('GATE'), 'GATE stage labelled');
  assert(txt.includes('PROMOTED'), 'verdict surfaced');
});

test('experiment shows a baseline (seed) flow for v0', () => {
  resetState();
  state.epochDef = {
    epoch_id: 'e1', goal: 'g', board: [], brief: '',
    experiments: [{ generation_id: 'v0', parent_generation_id: null, hypothesis: {}, outcome: null }],
  };
  const stage = freshStage();
  expView.renderExperiment({ stage, state, params: { epochId: 'e1', genId: 'v0' }, chrome: buildChrome(), repaint() {} });
  assert(stage.textContent.includes('BASELINE'), 'baseline seed surfaced as such');
});

// ---- tournament + bench honest states -------------------------------

test('tournament reports no rounds honestly when empty', () => {
  resetState();
  state.epochDef = { epoch_id: 'e1', goal: 'g', board: [], brief: '', experiments: [] };
  const stage = freshStage();
  tourView.renderTournament({ stage, state, params: { epochId: 'e1' } });
  assert(stage.textContent.toLowerCase().includes('no rounds'), 'empty gauntlet is explicit');
});

test('bench surfaces loop-health findings with severity', () => {
  resetState();
  state.connected = true;
  state.healthReport = {
    epoch_id: 'e1', healthy: false,
    findings: [{ severity: 'critical', detector: 'degenerate_scoring', summary: 'zero variance', remedy: 'inspect loss.json' }],
  };
  const stage = freshStage();
  benchView.renderBench({ stage, state });
  assert(stage.textContent.includes('degenerate_scoring'), 'finding name shown');
  assert(stage.textContent.toLowerCase().includes('critical'), 'severity shown');
});

await run();
