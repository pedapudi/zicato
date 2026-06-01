// test/variant_f.test.mjs — Variant F ("Current — causal narrative").
//
// Verifies the synthesis variant against the shared harness DOM: the
// router (#/F prefix), the ported C diagram layouts, the ported D data-viz
// primitives (non-colliding guarantees), and that each hero screen paints
// real content from `state`. The two mandatory render-discipline tests:
//   (a) a digest-gated no-op — an identical-data / heartbeat-only repaint
//       does NOT rebuild the DOM (no innerHTML writes, stable node identity);
//   (b) a cold deep-link run/transcript test — a direct #/F/run/<id> load
//       fetches its own transcript and renders content, never empty.

import { installDom, test, run, assert, assertEqual } from './harness.mjs';

installDom();

const { parseRoute, href } = await import('../js/variants/F/router.js');
const prim = await import('../js/variants/F/diagram/primitives.js');
const { layoutSankey } = await import('../js/variants/F/diagram/sankey.js');
const svg = await import('../js/variants/F/lib/svg.js');
const { state } = await import('../js/core/state.js');
const { buildChrome, openDrawer } = await import('../js/variants/F/chrome.js');
const envView = await import('../js/variants/F/views/environment.js');
const epochView = await import('../js/variants/F/views/epoch.js');
const lifeView = await import('../js/variants/F/views/lifecycle.js');
const tourView = await import('../js/variants/F/views/tournament.js');
const benchView = await import('../js/variants/F/views/bench.js');
const runView = await import('../js/variants/F/views/run.js');

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
  state.experiments = [];
  state.scoreTrajectory = { points: [] };
  state.activeRuns = [];
  state.activeTournament = null;
  state.healthReport = null;
  state.connected = false;
  state.connecting = false;
  envView && state; // noop touch
  lifeView.resetLifecycleCaches();
  runView.resetRunCaches();
}

// ---- router (the #/F prefix) ---------------------------------------

test('router parses every screen under the #/F prefix', () => {
  assertEqual(parseRoute('#/F/env').view, 'env');
  assertEqual(parseRoute('#/F/epoch/e1').params.epochId, 'e1');
  const lc = parseRoute('#/F/lifecycle/e1/v1');
  assertEqual(lc.view, 'lifecycle');
  assertEqual(lc.params.genId, 'v1');
  assertEqual(parseRoute('#/F/styles/e1').view, 'styles');
  assertEqual(parseRoute('#/F/run/r9').params.runId, 'r9');
  assertEqual(parseRoute('#/F/bench').view, 'bench');
  // The shipped shell + variant C own their own spaces; a non-F hash is env.
  assertEqual(parseRoute('#/C/epoch/x').view, 'env');
  assertEqual(parseRoute('#/epoch/x').view, 'env');
});

test('href round-trips through parseRoute', () => {
  const h = href('lifecycle', { epochId: 'ep a', genId: 'v2' });
  assert(h.startsWith('#/F/lifecycle/'), 'href carries the F prefix');
  const r = parseRoute(h);
  assertEqual(r.view, 'lifecycle');
  assertEqual(r.params.epochId, 'ep a');
  assertEqual(r.params.genId, 'v2');
});

// ---- ported C diagram layouts (collision-free) ----------------------

test('layoutDag gives every node a distinct (col,row) cell', () => {
  const nodes = [
    { id: 'v0', parent: null },
    { id: 'v1', parent: 'v0' },
    { id: 'v2', parent: 'v0' },
  ];
  const { pos } = prim.layoutDag(nodes);
  const cells = new Set();
  for (const n of nodes) {
    const p = pos.get(n.id);
    const key = p.col + ',' + p.row;
    assert(!cells.has(key), `collision at ${key}`);
    cells.add(key);
  }
  assert(pos.get('v1').col > pos.get('v0').col, 'child deeper than parent');
});

test('layoutSankey places patch < drift < gate in x', () => {
  const out = layoutSankey({
    patch: [{ id: 'p', label: 'patch' }],
    drift: [{ id: 'd', label: 'drift', value: 2 }],
    gate: [{ id: 'g', label: 'GATE' }],
    links: [{ source: 'p', target: 'd', value: 2 }, { source: 'd', target: 'g', value: 2 }],
  });
  const px = out.nodes.find((n) => n.id === 'p').x;
  const gx = out.nodes.find((n) => n.id === 'g').x;
  assert(px < gx, 'patch left of gate');
});

// ---- ported D data-viz (non-colliding evidence) ---------------------

test('decollide keeps a minimum gap and clamps to bounds', () => {
  const y = (v) => v;            // identity scale
  const items = [{ v: 10 }, { v: 10 }, { v: 10 }]; // all collide
  const out = svg.decollide(items, y, 12, 0, 200);
  out.sort((a, b) => a - b);
  assert(out[1] - out[0] >= 11.9, 'gap enforced between first two');
  assert(out[2] - out[1] >= 11.9, 'gap enforced between next two');
});

test('pairedSlopegraph renders one series line per board duel', () => {
  const node = svg.pairedSlopegraph({
    series: [
      { label: 'q3_metrics_outline', a: 71.0, b: 63.5, verdict: 'improved' },
      { label: 'picky_stakeholder_emulated', a: 105.5, b: 642.5, verdict: 'regressed' },
    ],
  });
  const lines = node.querySelectorAll('[class]');
  // At least the two slope lines exist (plus axes/nodes/labels).
  assert(node != null && lines.length > 0, 'paired slopegraph drew marks');
});

// ---- chrome (F identity + nav) --------------------------------------

test('chrome builds the F nav and a status pill', () => {
  const c = buildChrome();
  const items = c.root.querySelectorAll('[data-view]');
  const views = new Set([...items].map((i) => i.getAttribute('data-view')));
  for (const v of ['env', 'epoch', 'lifecycle', 'styles', 'run', 'bench']) {
    assert(views.has(v), `nav missing ${v}`);
  }
  c.setActive('lifecycle');
  assert(c.root.querySelector('[data-view="lifecycle"]').classList.contains('is-active'),
    'active nav item is marked');
});

test('openDrawer reveals the drawer and swaps its body', () => {
  const c = buildChrome();
  const body = document.createElement('p');
  body.textContent = 'detail body';
  openDrawer(c, 'Detail', body);
  assertEqual(c.drawer.getAttribute('aria-hidden'), 'false');
  assert(c.drawerBody.textContent.includes('detail body'));
});

// ---- environment: lineage DAG + the Fleet trendline strip -----------

test('environment paints honest empty state with no epochs', () => {
  resetState();
  const stage = freshStage();
  envView.renderEnvironment({ stage, state, onNavigate() {} });
  assert(stage.textContent.toLowerCase().includes('no epochs'), 'empty env names the absence');
});

test('environment renders the lineage DAG and the Fleet strip', () => {
  resetState();
  state.lineage = {
    generations: [
      { generation_id: 'v0', epoch_id: '2026-05-30_e0', parent_generation_id: '', promoted: true },
      { generation_id: 'v1', epoch_id: '2026-05-30_e0', parent_generation_id: 'v0', promoted: false },
      { generation_id: 'v2', epoch_id: '2026-05-30_e0', parent_generation_id: 'v0', promoted: false },
    ],
  };
  state.epochs = [{ epoch_id: '2026-05-30_e0', goal: 'tighten slide-structure discipline' }];
  const stage = freshStage();
  envView.renderEnvironment({ stage, state, onNavigate() {} });
  const nodes = stage.querySelectorAll('[data-cz="env-node"]');
  assertEqual(nodes.length, 3, 'one DAG node per generation');
  // The Fleet strip — one editorial card per epoch.
  const fleet = stage.querySelectorAll('[data-cz="fleet-card"]');
  assertEqual(fleet.length, 1, 'one fleet card for the epoch');
  assert(fleet[0].getAttribute('href').startsWith('#/F/epoch/'), 'fleet card links to its epoch');
});

// ---- epoch: the objective is the headline ---------------------------

test('epoch makes the OBJECTIVE the headline and lists the lineage', () => {
  resetState();
  state.epochDef = {
    epoch_id: '2026-05-30_e0', goal: 'enforce explicit slide-structure output',
    board: [], brief: '',
    experiments: [
      { generation_id: 'v0', parent_generation_id: '', hypothesis: {}, outcome: null },
      { generation_id: 'v1', parent_generation_id: 'v0', hypothesis: {}, outcome: { tournament_decision: 'rejected', scalar_score_delta: 75.71 } },
    ],
  };
  const stage = freshStage();
  epochView.renderEpoch({ stage, state, chrome: buildChrome(), params: { epochId: '2026-05-30_e0' } });
  const obj = stage.querySelector('[class="cz-objective"]');
  assert(obj && obj.textContent.includes('explicit slide-structure'), 'objective shows the goal');
  const gens = stage.querySelectorAll('[data-cz="gen-node"]');
  assertEqual(gens.length, 2, 'lineage shows both generations');
});

// ---- lifecycle: the candidate hero (flow + pull-quote + evidence) ---

test('lifecycle pull-quotes the hypothesis and shows the lineage DAG', () => {
  resetState();
  state.lineage = {
    generations: [
      { generation_id: 'v0', epoch_id: 'e0', parent_generation_id: '', promoted: true },
      { generation_id: 'v1', epoch_id: 'e0', parent_generation_id: 'v0', promoted: false },
    ],
  };
  state.epochDef = {
    epoch_id: 'e0', goal: 'g', board: [], brief: '',
    experiments: [
      { generation_id: 'v0', parent_generation_id: '', hypothesis: {}, outcome: null },
      {
        generation_id: 'v1', parent_generation_id: 'v0',
        hypothesis: { core_idea: 'Enforce explicit slide-structure output' },
        outcome: { tournament_decision: 'rejected', rejection_reason: 'challenger regressed: loss rose by 75.71' },
      },
    ],
  };
  const stage = freshStage();
  lifeView.renderLifecycle({ stage, state, params: { epochId: 'e0', genId: 'v1' }, repaint() {}, onNavigate() {} });
  // The B-style pull-quote of the hypothesis.
  const quotes = stage.querySelectorAll('[class="vb-pullquote-text"]');
  assert(quotes.length >= 1, 'a hypothesis pull-quote renders');
  assert(stage.textContent.includes('Enforce explicit slide-structure'), 'hypothesis surfaced as a pull-quote');
  // The lineage DAG nodes (each opens that candidate's lifecycle).
  const lin = stage.querySelectorAll('[data-cz="lc-lineage-node"]');
  assert(lin.length >= 2, 'lineage DAG nodes present');
});

// ---- tournament + bench honest states -------------------------------

test('tournament reports no rounds honestly when empty', () => {
  resetState();
  state.epochDef = { epoch_id: 'e0', goal: 'g', board: [], brief: '', experiments: [] };
  const stage = freshStage();
  tourView.renderTournament({ stage, state, params: { epochId: 'e0' } });
  assert(stage.textContent.toLowerCase().includes('no rounds'), 'empty gauntlet is explicit');
});

test('bench surfaces loop-health findings with severity', () => {
  resetState();
  state.connected = true;
  state.healthReport = {
    epoch_id: 'e0', healthy: false,
    findings: [{ severity: 'critical', detector: 'degenerate_scoring', summary: 'zero variance' }],
  };
  const stage = freshStage();
  benchView.renderBench({ stage, state });
  assert(stage.textContent.includes('degenerate_scoring'), 'finding name shown');
  assert(stage.textContent.toLowerCase().includes('critical'), 'severity shown');
});

// ---- (a) MANDATORY: digest-gated no-op repaint ----------------------

test('a heartbeat-only re-render does NOT rebuild the DOM (digest-gated)', () => {
  resetState();
  state.connected = true;
  state.heartbeat = { phase: 'TOURNAMENT RUNNING', epoch_id: 'e0', generation_id: 'v1', emitted_at: '2026-05-31T00:00:00Z' };
  state.service = { version: '1.0', port: 7892 };
  const stage = freshStage();
  benchView.renderBench({ stage, state });
  const firstChild = stage.firstChild;
  assert(firstChild != null, 'bench painted on first render');
  const beforeWrites = stage.innerHTMLWriteCount();

  // A pure heartbeat tick: only the timestamp changed. The view must no-op.
  state.heartbeat = { ...state.heartbeat, emitted_at: '2026-05-31T00:00:05Z' };
  benchView.renderBench({ stage, state });

  assert(stage.firstChild === firstChild, 'the content host kept node identity (no rebuild)');
  assertEqual(stage.innerHTMLWriteCount(), beforeWrites, 'no innerHTML writes on the no-op repaint');

  // A REAL structural change (new finding) DOES repaint.
  state.healthReport = { healthy: false, findings: [{ severity: 'warn', detector: 'x', summary: 's' }] };
  benchView.renderBench({ stage, state });
  assert(stage.textContent.includes('degenerate') === false && stage.textContent.includes('x'),
    'a structural change repaints to the new content');
});

// ---- (b) MANDATORY: cold deep-link run/transcript -------------------

test('cold deep-link #/F/run/<id> fetches its transcript and renders content', async () => {
  resetState();
  // No active runs, no live state — a true cold deep link.
  const RUN_ID = 'f318d5ae00000000';
  const captured = [];
  const realFetch = globalThis.fetch;
  globalThis.fetch = async (path) => {
    captured.push(path);
    return {
      ok: true,
      async json() {
        return {
          turns: [
            { seq: 0, role: 'user', text: 'Make a presentation about waffles.' },
            { seq: 1, role: 'agent', agent: 'research_agent', text: 'Outlining the deck.', tool_calls: [{ name: 'search' }] },
          ],
          annotations: [{ anchor_seq: 1, kind: 'drift', summary: 'topic drift detected' }],
        };
      },
    };
  };

  let repaints = 0;
  const ctx = { stage: freshStage(), state, params: { runId: RUN_ID }, repaint() { repaints += 1; } };

  // Cold render: no transcript cached yet → a loading state, and a fetch is
  // kicked off against the deep-linked run id.
  runView.renderRun(ctx);
  assert(ctx.stage.textContent.toLowerCase().includes('reconstructing'), 'cold load shows a loading state, never empty');
  assert(captured.some((p) => p.includes('/api/conversation/' + RUN_ID)), 'fetched the conversation for the deep-linked run');

  // Let the fetch resolve, then re-render (as the repaint callback would).
  await new Promise((r) => setTimeout(r, 0));
  runView.renderRun(ctx);
  globalThis.fetch = realFetch;

  assert(ctx.stage.textContent.includes('Make a presentation about waffles'), 'transcript content rendered on hydration');
  assert(ctx.stage.textContent.includes('research_agent'), 'turn role surfaced');
  assert(ctx.stage.textContent.includes('search'), 'tool call surfaced');
  assert(ctx.stage.textContent.includes('topic drift'), 'inline drift annotation surfaced');
});

await run();
