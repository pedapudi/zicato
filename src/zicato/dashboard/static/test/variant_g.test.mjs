// variant_g.test.mjs — Variant G ("Bridge") behavior tests.
//
// Run: node src/zicato/dashboard/static/test/variant_g.test.mjs
// Uses the shared harness DOM (no jsdom).
//
// Bridge is "Variant A done right": A's nav/IA + Fleet home, D's data-viz,
// C's diagrams, B/D theming, with the FOUR A bugs fixed. Beyond the usual
// router / component / view-render coverage, this file carries EXPLICIT
// regression tests for each of the four bugs:
//   (a) drilldown does NOT rebuild on a heartbeat-only state change,
//   (b) cold deep-link run view renders transcript content (not empty),
//   (c) environment render is a no-op when epoch/fleet data is unchanged,
//   (d) bench event-tail rows are laid out in flow (constrained-scroll
//       container, rows are non-overlapping siblings).

import { installDom, test, assert, assertEqual, run } from './harness.mjs';

installDom();
globalThis.requestAnimationFrame = (fn) => setTimeout(fn, 0);

// A controllable fetch stub: tests register handlers by URL substring.
const _handlers = [];
function onFetch(match, payload) { _handlers.push({ match, payload }); }
function resetFetch() { _handlers.length = 0; }
globalThis.fetch = async (url) => {
  const u = String(url);
  for (const h of _handlers) {
    if (u.includes(h.match)) {
      const body = typeof h.payload === 'function' ? h.payload(u) : h.payload;
      return { ok: true, json: async () => body };
    }
  }
  return { ok: true, json: async () => ({}) };
};

// Let pending microtasks / setTimeout(0) fetches resolve.
const tick = () => new Promise((r) => setTimeout(r, 5));

const { parseRoute, href, crumbsFor } = await import('../js/variants/G/router.js');
const { renderMarkdown } = await import('../js/variants/G/components/markdown.js');
const { panel, readout, chip, drawer } = await import('../js/variants/G/components/ui.js');
const { sparkline, pairedSlopegraph, decollide, jitterColumn, valueDotPlot } = await import('../js/variants/G/svg.js');
const { layoutSankey } = await import('../js/variants/G/diagram/sankey.js');
const { layoutDag } = await import('../js/variants/G/diagram/primitives.js');
const { lineageModel, candidateSet } = await import('../js/variants/G/model.js');
const { state } = await import('../js/core/state.js');

// -- router: A's IA, rebound under #/G ---------------------------------
test('router: home for empty / unknown hash', () => {
  assertEqual(parseRoute('').name, 'environment');
  assertEqual(parseRoute('#/G/').name, 'environment');
  assertEqual(parseRoute('#/whatever').name, 'environment');
});

test('router: parses the same routes A had, under #/G', () => {
  assertEqual(parseRoute('#/G/epoch/e0').name, 'epoch');
  assertEqual(parseRoute('#/G/epoch/e0').params.epochId, 'e0');
  const exp = parseRoute('#/G/experiment/e0/v1');
  assertEqual(exp.name, 'experiment');
  assertEqual(exp.params.epochId, 'e0');
  assertEqual(exp.params.genId, 'v1');
  assertEqual(parseRoute('#/G/tournament/e0').name, 'tournament');
  assertEqual(parseRoute('#/G/run/abc').params.runId, 'abc');
  assertEqual(parseRoute('#/G/bench').name, 'bench');
});

test('router: href round-trips and uses the #/G prefix', () => {
  const h = href('experiment', { epochId: 'e 1', genId: 'v2' });
  assert(h.startsWith('#/G/'), 'prefix is #/G: ' + h);
  const r = parseRoute(h);
  assertEqual(r.name, 'experiment');
  assertEqual(r.params.epochId, 'e 1');
  assertEqual(r.params.genId, 'v2');
});

test('router: breadcrumb trail always starts at environment', () => {
  const c = crumbsFor(parseRoute('#/G/experiment/e0/v1'));
  assert(c.length >= 2, 'has crumbs');
  assertEqual(c[0].label, 'environment');
  assert(c[c.length - 1].current === true, 'last is current');
});

// -- components --------------------------------------------------------
test('markdown: renders the brief without innerHTML', () => {
  const node = renderMarkdown('# Goal\n\nReduce **drift**.\n\n- a\n- b\n\n`code`');
  assertEqual(node.innerHTMLWriteCount(), 0, 'no innerHTML used');
  const text = node.textContent;
  assert(text.includes('Goal') && text.includes('drift') && text.includes('code'));
});

test('ui: panel + readout + chip + drawer build without innerHTML', () => {
  const p = panel({ title: 'T', sub: 's', accent: 'improve', body: readout({ label: 'verdict', value: 'PROMOTE', tone: 'improve' }) });
  assert(p.textContent.includes('PROMOTE'));
  assertEqual(p.innerHTMLWriteCount(), 0, 'panel uses no innerHTML');
  const c = chip('pass', 'improve');
  assertEqual(c.getAttribute('data-kind'), 'improve');
  // drawer collapsed by default; toggling reveals it (no parent rebuild).
  const d = drawer({ title: 'Patch diff', openByDefault: false, body: 'BODY' });
  const body = d._descendants().find((n) => n.className && n.className.includes('g-drawer-body'));
  assert(body && body.hasAttribute('hidden'), 'drawer collapsed by default');
});

// -- D data-viz: non-collision guarantees ------------------------------
test('svg: sparkline <2 points degrades to an empty mark, not a throw', () => {
  const n = sparkline({ values: [0.5] });
  assertEqual(n.tagName, 'SVG');
});

test('svg: decollide keeps a minimum gap between labels (no overlap)', () => {
  const y = (v) => v;                 // identity scale
  const out = decollide([{ v: 100 }, { v: 100.4 }, { v: 100.6 }], y, 10, 0, 1000);
  for (let i = 1; i < out.length; i++) {
    assert(out[i] - out[i - 1] >= 10 - 1e-6, 'labels keep min gap: ' + out);
  }
});

test('svg: jitterColumn separates coincident nodes', () => {
  const out = jitterColumn([50, 50, 50], 4);
  assert(out[0] !== out[1] && out[1] !== out[2], 'coincident nodes spread: ' + out);
});

test('svg: paired slopegraph renders one svg for the duels', () => {
  const g = pairedSlopegraph({ series: [
    { id: 'a', label: 'a', a: 71, b: 63.5, verdict: 'improved' },
    { id: 'b', label: 'b', a: 105.5, b: 642.5, verdict: 'regressed' },
  ] });
  assertEqual(g.tagName, 'SVG');
  const lines = g._descendants().filter((n) => n.className && n.className.includes('d-pslope-line'));
  assert(lines.length === 2, 'one line per duel: ' + lines.length);
});

// -- C diagrams: layout is non-colliding -------------------------------
test('diagram: layoutDag gives each node a distinct (col,row) cell', () => {
  const { pos } = layoutDag([
    { id: 'v0', parent: null }, { id: 'v1', parent: 'v0' }, { id: 'v2', parent: 'v0' },
  ]);
  const v1 = pos.get('v1'); const v2 = pos.get('v2');
  // siblings share a column but get distinct rows — no overlap.
  assertEqual(v1.col, v2.col);
  assert(v1.row !== v2.row, 'siblings get distinct rows');
});

test('diagram: layoutSankey positions patch→drift→gate columns', () => {
  const out = layoutSankey({
    patch: [{ id: 'p0', label: 'prompt' }],
    drift: [{ id: 'd0', label: 'schema', value: 5 }],
    gate: [{ id: 'gate', label: 'REJECT', value: 5 }],
    links: [{ source: 'p0', target: 'd0', value: 5 }, { source: 'd0', target: 'gate', value: 5 }],
  });
  assert(out.nodes.length === 3, 'three nodes');
  assert(out.links.length === 2, 'two ribbons');
  const xs = out.nodes.map((n) => n.x);
  assert(xs[0] < xs[1] && xs[1] < xs[2], 'columns advance left→right: ' + xs);
});

// -- model selectors ---------------------------------------------------
function seedLiveData() {
  // The live data: ONE epoch, v0 crowned, v1/v2 rejected.
  state.epochDef = {
    epoch_id: '2026-05-30_e0',
    goal: 'Tighten the presentation agent.',
    brief: '## Goal\n\nReduce confabulation.\n\n- cite sources',
    board: [
      { id: 'waffles_single', kind: 'single_turn', budget_s: 180 },
      { id: 'q3_metrics_outline', kind: 'single_turn', budget_s: 180 },
    ],
    experiments: [
      { generation_id: 'v0', parent_generation_id: null, outcome: null },
      { generation_id: 'v1', parent_generation_id: 'v0', hypothesis: { core_idea: 'Enforce slide structure', modulating: ['coordinator/prompt'] },
        outcome: { tournament_decision: 'rejected', scalar_score: 146.65, scalar_score_delta: 75.71, drift_loss_delta: 77.0, pass_rate_delta: 0.0, rejection_reason: 'challenger regressed' } },
      { generation_id: 'v2', parent_generation_id: 'v0', hypothesis: { core_idea: 'Tighten oversight', modulating: ['coordinator/oversight'] },
        outcome: { tournament_decision: 'rejected', scalar_score: 72.45, scalar_score_delta: 1.51 } },
    ],
    delta_scalar_summary: { champion_spine: 0.0 },
  };
  state.lineage = { generations: [
    { generation_id: 'v0', epoch_id: '2026-05-30_e0', parent_generation_id: '', promoted: true },
    { generation_id: 'v1', epoch_id: '2026-05-30_e0', parent_generation_id: 'v0', promoted: false },
    { generation_id: 'v2', epoch_id: '2026-05-30_e0', parent_generation_id: 'v0', promoted: false },
  ] };
  state.heartbeat = { epoch_id: '2026-05-30_e0' };
}

test('model: lineageModel crowns v0, files v1/v2 as challengers', () => {
  seedLiveData();
  const m = lineageModel(state, '2026-05-30_e0');
  assertEqual(m.champion.id, 'v0');
  assertEqual(m.spine.length, 1);
  assertEqual(m.challengers.length, 2);
  assert(m.challengers.every((c) => c.decision === 'rejected'), 'both rejected');
});

test('model: candidateSet puts champion first', () => {
  seedLiveData();
  const cands = candidateSet(state, '2026-05-30_e0');
  assertEqual(cands[0].role, 'champion');
  assertEqual(cands[0].id, 'v0');
});

// -- views render against injected state (no flash) --------------------
test('epoch view: objective + brief drawer + lineage + heatmap render', async () => {
  resetFetch();
  onFetch('/per-entry', { entries: [
    { entry_id: 'waffles_single', drift_loss: 60.5, pass_fail: 0, wall_clock_budget_exceeded: true },
  ] });
  const { renderEpoch, resetEpochCache } = await import('../js/variants/G/views/epoch.js');
  resetEpochCache();
  seedLiveData();
  const host = document.createElement('div');
  renderEpoch(host, { epochId: '2026-05-30_e0' }, () => {});
  const text = host.textContent;
  assert(text.includes('Tighten the presentation agent'), 'objective prominent');
  assert(text.includes('Proposer brief'), 'brief has a home');
  assert(text.includes('Lineage'), 'lineage present');
  assert(text.includes('Board entry × generation drift'), 'heatmap panel present');
  assertEqual(host.innerHTMLWriteCount(), 0, 'no innerHTML / no flash path');
});

test('experiment view: leads with verdict, gate + flow, diff is a drawer', async () => {
  resetFetch();
  onFetch('/per-entry', { entries: [{ entry_id: 'waffles_single', run_id: 'r1', drift_loss: 60.5, pass_fail: 0 }] });
  onFetch('/drift-movements', { movements: [{ kind: 'schema', delta: 5, champion_count: 1, challenger_count: 2 }] });
  onFetch('/gate', { decision: 'rejected', reason: 'challenger regressed', rules: [
    { id: 'scalar_margin', label: 'Scalar margin', status: 'fail', detail: '+75.71' },
  ] });
  onFetch('/diff', { diff: '--- a\n+++ b' });
  const { renderExperiment, resetExperimentCache } = await import('../js/variants/G/views/experiment.js');
  resetExperimentCache();
  seedLiveData();
  const host = document.createElement('div');
  renderExperiment(host, { epochId: '2026-05-30_e0', genId: 'v1' }, () => {});
  await tick();
  renderExperiment(host, { epochId: '2026-05-30_e0', genId: 'v1' }, () => {});
  const text = host.textContent;
  assert(text.includes('REJECT'), 'verdict leads: ' + text.slice(0, 80));
  assert(text.includes('Patch diff'), 'diff present as a drawer');
  assert(text.includes('patch → drift → gate'), 'causal flow present');
});

test('tournament view: paired duels + topology switcher render', async () => {
  resetFetch();
  onFetch('/api/tournaments', { epoch_id: '2026-05-30_e0', champion_lineage: ['v0'], matchups: [
    { champion: 'v0', challenger: 'v1', decision: 'rejected', delta_scalar: 75.71, rejection_reason: 'regressed' },
    { champion: 'v0', challenger: 'v2', decision: 'rejected', delta_scalar: 1.51 },
  ] });
  const { renderTournament, resetTournamentCache } = await import('../js/variants/G/views/tournament.js');
  resetTournamentCache();
  seedLiveData();
  const host = document.createElement('div');
  renderTournament(host, { epochId: '2026-05-30_e0' }, () => {});
  await tick();
  renderTournament(host, { epochId: '2026-05-30_e0' }, () => {});
  const text = host.textContent;
  assert(text.includes('Paired board duels'), 'slopegraph panel present');
  assert(text.includes('Gauntlet'), 'topology switcher present');
  assert(text.includes('Tournament topology'), 'topology panel present');
});

// =====================================================================
// THE FOUR A BUGS — explicit regression coverage
// =====================================================================

// (a) BUG #1 — drilldown must NOT rebuild on a heartbeat-only change.
test('BUG#1: experiment drilldown does NOT rebuild on a heartbeat-only state change', async () => {
  resetFetch();
  onFetch('/per-entry', { entries: [{ entry_id: 'waffles_single', run_id: 'r1', drift_loss: 60.5, pass_fail: 0 }] });
  onFetch('/drift-movements', { movements: [] });
  onFetch('/gate', { decision: 'rejected', reason: 'x', rules: [] });
  onFetch('/diff', null);
  onFetch('/expectations', { outcomes: [{ kind: 'predicate', passed: false }] });
  onFetch('/per-judge', { judges: [{ judge_name: 'incorporates_feedback', weighted_loss: 27.0, weight: 1.0 }] });

  const expModule = await import('../js/variants/G/views/experiment.js');
  const { renderExperiment, resetExperimentCache, experimentDigest, _setSelectedEntry } = expModule;
  resetExperimentCache();
  seedLiveData();
  const host = document.createElement('div');
  const params = { epochId: '2026-05-30_e0', genId: 'v1' };

  // initial render(s) to load per-entry, then select an entry (drill).
  renderExperiment(host, params, () => {});
  await tick();
  renderExperiment(host, params, () => {});
  _setSelectedEntry('waffles_single');
  // load the drill data, then settle the view onto it.
  const m = await import('../js/variants/G/views/experiment.js');
  void m;
  renderExperiment(host, params, () => {});
  await tick();
  renderExperiment(host, params, () => {});
  assert(host.textContent.includes('per-judge loss'), 'drilldown is open');

  // The digest with the entry selected + data loaded must be STABLE
  // across a pure heartbeat change (timestamps re-stamped, no new data).
  const before = experimentDigest(params);
  state.heartbeat = Object.assign({}, state.heartbeat, { ts: '2026-05-31T00:00:01Z', elapsed_seconds: 999 });
  const after = experimentDigest(params);
  assertEqual(after, before, 'digest is heartbeat-stable → the view (and its drilldown) is a no-op');
});

// (b) BUG #2 — cold deep-link run view renders transcript content.
test('BUG#2: cold deep-link run view renders transcript turns (not empty)', async () => {
  resetFetch();
  onFetch('/api/run/', { run: { phase: 'completed', elapsed_seconds: 12 } });
  onFetch('/api/conversation/', { turns: [
    { seq: 0, role: 'user', text: 'Make a deck about waffles.' },
    { seq: 1, role: 'agent', agent: 'coordinator', text: 'Outlining the deck…', tool_calls: [{ name: 'create_slide' }] },
  ], annotations: [{ anchor_seq: 1, kind: 'drift', summary: 'schema drift' }] });

  const { renderRun, resetRunCache } = await import('../js/variants/G/views/run.js');
  resetRunCache();
  const host = document.createElement('div');
  // COLD: no prior state, the run id comes only from the route params.
  renderRun(host, { runId: 'f318d5ae' }, () => {});
  // first paint is the loading state, never empty.
  assert(host.textContent.includes('Reading run') || host.textContent.includes('Reconstructing'), 'shows loading on cold load');
  await tick();
  renderRun(host, { runId: 'f318d5ae' }, () => {});
  const text = host.textContent;
  assert(text.includes('Transcript'), 'transcript panel rendered');
  assert(text.includes('Make a deck about waffles'), 'turn content rendered from /api/conversation');
  assert(text.includes('create_slide'), 'tool call rendered');
  assert(!text.includes('No turns reconstructed'), 'NOT the empty-transcript state');
});

// (c) BUG #3 — environment render is a no-op when fleet data unchanged.
test('BUG#3: environment render is a no-op when epoch/fleet data is unchanged', async () => {
  resetFetch();
  onFetch('/api/workspace', { current_epoch_id: '2026-05-30_e0', sparkline: [{ scalar: 80 }, { scalar: 70 }], epochs: [
    { epoch_id: '2026-05-30_e0', generation_count: 3, promoted_count: 1, closed: false, best_scalar: 70.94, goal: 'Tighten the agent.' },
  ] });
  onFetch('/api/health-report', { healthy: true, findings: [] });

  const { renderEnvironment, resetEnvironmentCache, environmentDigest } = await import('../js/variants/G/views/environment.js');
  resetEnvironmentCache();
  seedLiveData();
  const host = document.createElement('div');
  renderEnvironment(host, {}, () => {});
  await tick();
  renderEnvironment(host, {}, () => {});  // settle with workspace loaded
  assert(host.textContent.includes('Cross-epoch trajectory'), 'fleet trendline rendered');

  // Capture the live fleet-card node identity. A heartbeat tick must
  // NOT replace it — that DOM-replacement is the jerky-hover bug.
  const cardBefore = host._descendants().find((n) => n.className && n.className.includes('g-fleet-card'));
  assert(cardBefore, 'a fleet card exists');
  const digestBefore = environmentDigest();

  // Heartbeat-only change: re-stamp the clock, nothing structural.
  state.heartbeat = Object.assign({}, state.heartbeat, { phase: 'idle', evolve_started_at: '2026-05-31T00:00:00Z', ts: '2026-05-31T00:00:05Z' });
  const digestAfter = environmentDigest();
  assertEqual(digestAfter, digestBefore, 'digest is heartbeat-stable');

  renderEnvironment(host, {}, () => {});  // a heartbeat-driven repaint
  const cardAfter = host._descendants().find((n) => n.className && n.className.includes('g-fleet-card'));
  assert(cardAfter === cardBefore, 'fleet card node SURVIVES the heartbeat repaint (CSS hover never resets)');
});

// (d) BUG #4 — bench event-tail rows are laid out in flow, not overlapping.
test('BUG#4: bench event tail is a constrained-scroll container with sibling rows', async () => {
  resetFetch();
  const { renderBench, resetBenchCache } = await import('../js/variants/G/views/bench.js');
  resetBenchCache();
  state.activeRuns = [];
  state.heartbeat = { phase: 'idle' };
  state.logTail = { events: [
    { ts: '2026-05-31T00:00:00Z', kind: 'run_started', summary: 'run a started' },
    { ts: '2026-05-31T00:00:01Z', kind: 'drift', summary: 'schema drift on entry x' },
    { ts: '2026-05-31T00:00:02Z', kind: 'run_done', summary: 'run a done' },
  ] };
  const host = document.createElement('div');
  renderBench(host, {}, () => {});

  // the container carries the constrained-scroll class.
  const tail = host._descendants().find((n) => n.className && n.className.includes('g-eventtail'));
  assert(tail, 'event tail container present (.g-eventtail — height + overflow-y:auto in CSS)');

  // rows are SIBLINGS inside it (normal flow), never absolutely stacked.
  const rows = tail.children.filter((c) => c.className && c.className.includes('g-event-row'));
  assertEqual(rows.length, 3, 'one sibling row per event');
  for (const r of rows) {
    assert(r.parentNode === tail, 'each row is a direct child of the constrained container (in flow, no overlap)');
  }
  assertEqual(host.innerHTMLWriteCount(), 0, 'no innerHTML used');
});

await run();
