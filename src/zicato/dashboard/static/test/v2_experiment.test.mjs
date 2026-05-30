// test/v2_experiment.test.mjs — the v2 Experiment view.
//
// DASHBOARD-V2 §4.4 + §8 done-criteria. Pins that the view answers "was
// the bet right, and why?" on ONE dense screen:
//   * champion comparison is the DEFAULT (the parent is resolved from
//     the lineage and threaded through every endpoint — no opt-in).
//   * the four endpoints are called with {epoch, champion, challenger}.
//   * hypothesis→outcome renders the bet + predicted/actual drift +
//     an alignment verdict.
//   * the gate ladder + scalar waterfall + per-entry A/B + per-judge
//     attribution + patches each render from their source.
//   * honest states: a section whose fetch fails degrades to a broken
//     block with the reason — the rest of the screen survives.
//   * a per-entry row drills to the Run view (#/v2/run/{entry}/{gen}).
//
// fetch is mocked synchronously; the view's async sections resolve on
// the microtask queue, so each assertion flushes a few microtasks first.

import { installDom, test, run, assert, assertEqual } from './harness.mjs';

installDom();

const { state } = await import('../js/core/state.js');
const { v2Router } = await import('../js/v2/router.js');
const { renderExperiment, resetExperimentView } = await import('../js/v2/views/experiment.js');

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function descendantsWithClass(node, cls) {
  const out = [];
  const walk = (n) => {
    if (!n || n.nodeType !== 1) return;
    if (n.classList && n.classList.contains(cls)) out.push(n);
    for (const c of n.children) walk(c);
  };
  walk(node);
  return out;
}
function firstWithClass(node, cls) { return descendantsWithClass(node, cls)[0] || null; }
function allText(node) { return node && node.textContent ? node.textContent : ''; }

// Flush a handful of microtask turns so chained .then() bodies settle.
async function flush(n = 6) { for (let i = 0; i < n; i++) await Promise.resolve(); }

// A synchronous fetch mock keyed by URL substring → body. A url that
// matches a key whose body is the sentinel ERROR throws (non-ok), so a
// test can assert the honest broken state.
const ERROR = Symbol('http-error');
function mockFetch(routes) {
  const original = globalThis.fetch;
  globalThis.fetch = async (url) => {
    let body;
    for (const [needle, value] of Object.entries(routes)) {
      if (url.includes(needle)) { body = value; break; }
    }
    if (body === ERROR) {
      return { ok: false, status: 500, headers: new Map(), json: async () => ({}), text: async () => '' };
    }
    if (body === undefined) {
      return { ok: false, status: 404, headers: new Map(), json: async () => ({}), text: async () => '' };
    }
    return { ok: true, status: 200, headers: new Map(), json: async () => body, text: async () => JSON.stringify(body) };
  };
  return () => { globalThis.fetch = original; };
}

// Seed the lineage + epoch contract so the view resolves champion=parent
// and the experiment record for v1.
function seed() {
  state.lineage = {
    generations: [
      { generation_id: 'v0', epoch_id: 'e0', parent_generation_id: null, scalar: 0.80, verdict: 'promoted' },
      { generation_id: 'v1', epoch_id: 'e0', parent_generation_id: 'v0', scalar: 0.62, verdict: 'rejected' },
    ],
  };
  state.epochDef = {
    epoch_id: 'e0',
    experiments: [
      {
        generation_id: 'v1',
        hypothesis: {
          core_idea: 'Enforce explicit slide-structure discipline.',
          why: 'The champion rambled past the outline.',
          expected_pass_rate_delta: '+0.10..+0.20',
          expected_drift_movements: [
            { kind: 'off_topic', direction: 'fewer' },
            { kind: 'verbosity', direction: 'more' },
          ],
        },
        outcome: { tournament_decision: 'rejected', summary: 'Held on topic but regressed verbosity.' },
        patches: {
          'mut-1': { mutation_id: 'mut-1', target: 'agents/presenter.py', summary: 'Add outline guard.', diff: '- old\n+ new' },
        },
      },
    ],
  };
}

const GATE = {
  epoch_id: 'e0', champion: 'v0', challenger: 'v1',
  decision: 'rejected',
  reason: 'scalar margin not met',
  rules: [
    { id: 'regression_suite', label: 'Regression suite', status: 'pass', detail: 'ok', fired: false },
    { id: 'scalar_margin', label: 'Scalar margin', status: 'fail', detail: 'Δ -0.01 < margin 0.05', fired: true },
    { id: 'pass_rate_monotonicity', label: 'Pass-rate monotonicity', status: 'not_reached', detail: '', fired: false },
  ],
  scalar_components: {
    champion: { drift: 0.50, pass: 0.30 },
    challenger: { drift: 0.42, pass: 0.40 },
  },
  primary_driver: { judge: 'on_topic', delta: -0.08 },
};
const DRIFT = {
  epoch_id: 'e0', generation_id: 'v1', champion: 'v0', challenger: 'v1',
  movements: [
    { kind: 'off_topic', champion_count: 8, challenger_count: 3, delta: -5, direction: 'improved' },
    { kind: 'verbosity', champion_count: 2, challenger_count: 6, delta: 4, direction: 'worsened' },
  ],
};
const GRID = {
  epoch_id: 'e0', champion: 'v0', challenger: 'v1',
  entry_grid: [
    { entry_id: 'waffles_single', parent_drift_loss: 60.5, child_drift_loss: 55.0, parent_pass: true, child_pass: true, delta: -5.5, verdict: 'child' },
    { entry_id: 'picky_stakeholder', parent_drift_loss: 40.0, child_drift_loss: 52.0, parent_pass: true, child_pass: false, delta: 12.0, verdict: 'parent' },
  ],
  scalar: null,
};
const JUDGES = {
  epoch_id: 'e0', champion: 'v0', challenger: 'v1',
  judges: [
    { judge_name: 'on_topic', champion_weighted_loss: 0.30, challenger_weighted_loss: 0.22, delta: -0.08 },
    { judge_name: 'verbosity', champion_weighted_loss: 0.10, challenger_weighted_loss: 0.18, delta: 0.08 },
  ],
  primary_driver: 'on_topic',
};

function allRoutes() {
  return {
    'drift-movements': DRIFT,
    'per-judge-comparison': JUDGES, // must precede '/gate' check by substring
    'matchup-grid': GRID,
    '/gate': GATE,
  };
}

function makeHost() { return globalThis.document.createElement('div'); }
function route(genId) { return { view: 'experiment', params: genId == null ? {} : { generationId: genId } }; }

// ===========================================================================

test('experiment: header shows champion → challenger + the verdict glyph', async () => {
  seed();
  resetExperimentView();
  const restore = mockFetch(allRoutes());
  const host = makeHost();
  renderExperiment(host, route('v1'));
  await flush();
  const head = firstWithClass(host, 'v2-exp-head');
  assert(head, 'header rendered');
  const ids = allText(firstWithClass(host, 'v2-exp-ids'));
  assert(ids.includes('v0'), 'champion (parent) shown by default — no opt-in');
  assert(ids.includes('v1'), 'challenger shown');
  const verdict = firstWithClass(host, 'v2-exp-verdict');
  assert(allText(verdict).toLowerCase().includes('rejected'), 'big verdict glyph reads the decision');
  restore();
});

test('experiment: hypothesis→outcome renders the bet + predicted/actual drift + alignment', async () => {
  seed();
  resetExperimentView();
  const restore = mockFetch(allRoutes());
  const host = makeHost();
  renderExperiment(host, route('v1'));
  await flush();
  const bet = firstWithClass(host, 'v2-exp-bet');
  assert(bet, 'the bet section rendered after drift fetch resolved');
  assert(allText(bet).includes('slide-structure'), 'core idea shown');
  assert(allText(bet).includes('rambled'), 'why shown');
  // Two divergingBars — predicted and actual.
  const bars = descendantsWithClass(host, 'dbar');
  assert(bars.length >= 2, `predicted + actual diverging bars (got ${bars.length})`);
  // Alignment verdict: off_topic predicted fewer & actual -5 (match);
  // verbosity predicted more & actual +4 (match) → both held.
  const align = firstWithClass(host, 'v2-exp-align');
  assert(align, 'alignment verdict rendered');
  assert(allText(align).includes('2/2'), 'both predicted directions matched');
  assert(firstWithClass(host, 'v2-exp-align-hit'), 'bet-held badge shown');
  restore();
});

test('experiment: gate ladder renders with the fired rule + reason', async () => {
  seed();
  resetExperimentView();
  const restore = mockFetch(allRoutes());
  const host = makeHost();
  renderExperiment(host, route('v1'));
  await flush();
  const ladder = firstWithClass(host, 'gate-ladder');
  assert(ladder, 'gate ladder rendered');
  const fired = descendantsWithClass(host, 'gate-fired');
  assertEqual(fired.length, 1, 'exactly one fired rule emphasized');
  assert(allText(fired[0]).toLowerCase().includes('scalar margin'), 'the fired rule is the scalar margin');
  assert(allText(host).includes('scalar margin not met'), 'the gate reason is shown');
  restore();
});

test('experiment: scalar waterfall decomposes champion→challenger per component', async () => {
  seed();
  resetExperimentView();
  const restore = mockFetch(allRoutes());
  const host = makeHost();
  renderExperiment(host, route('v1'));
  await flush();
  const wf = firstWithClass(host, 'swfall');
  assert(wf, 'scalar waterfall rendered');
  const names = descendantsWithClass(wf, 'swfall-name').map(allText);
  assert(names.includes('drift'), 'drift component present');
  assert(names.includes('pass'), 'pass component present');
  // drift improved (0.42-0.50 = -0.08), pass regressed (0.40-0.30 = +0.10).
  assert(descendantsWithClass(wf, 'swfall-good').length >= 1, 'an improved component is green');
  assert(descendantsWithClass(wf, 'swfall-bad').length >= 1, 'a regressed component is red');
  restore();
});

test('experiment: per-entry A/B flags a pass→fail flip and drills to the run', async () => {
  seed();
  resetExperimentView();
  const restore = mockFetch(allRoutes());
  const host = makeHost();
  renderExperiment(host, route('v1'));
  await flush();
  const table = firstWithClass(host, 'v2-dt');
  assert(table, 'per-entry data table rendered');
  // picky_stakeholder is a pass→fail regression flip.
  const flip = firstWithClass(host, 'v2-exp-flip-regress');
  assert(flip, 'a pass→fail flip is flagged');
  // Click the row → navigate to the run, with the generation context.
  let dest = null;
  const origGo = v2Router.go;
  v2Router.go = (...args) => { dest = args; };
  const rows = descendantsWithClass(table, 'v2-dt-row-drillable');
  assert(rows.length >= 1, 'rows are drillable');
  rows[0].dispatchEvent({ type: 'click', target: rows[0], preventDefault() {}, stopPropagation() {} });
  v2Router.go = origGo;
  assertEqual(dest[0], 'run', 'drills to the Run view');
  assertEqual(dest[2], 'v1', 'carries the generation context');
  restore();
});

test('experiment: per-judge attribution names the primary driver', async () => {
  seed();
  resetExperimentView();
  const restore = mockFetch(allRoutes());
  const host = makeHost();
  renderExperiment(host, route('v1'));
  await flush();
  const driver = firstWithClass(host, 'v2-exp-driver');
  assert(driver, 'primary driver rendered');
  assert(allText(driver).includes('on_topic'), 'the primary driver judge is named');
  const judgeTables = descendantsWithClass(host, 'v2-dt');
  assert(judgeTables.length >= 2, 'a per-judge table renders alongside the per-entry table');
  restore();
});

test('experiment: patches render the exact change', async () => {
  seed();
  resetExperimentView();
  const restore = mockFetch(allRoutes());
  const host = makeHost();
  renderExperiment(host, route('v1'));
  await flush();
  const patch = firstWithClass(host, 'v2-exp-patch');
  assert(patch, 'a patch card rendered');
  assert(allText(patch).includes('agents/presenter.py'), 'the patch target is shown');
  assert(allText(patch).includes('outline guard'), 'the patch summary is shown');
  restore();
});

test('experiment: a failing section degrades to a broken state — the screen survives', async () => {
  seed();
  resetExperimentView();
  // Gate endpoint errors; everything else resolves.
  const routes = { ...allRoutes(), '/gate': ERROR };
  const restore = mockFetch(routes);
  const host = makeHost();
  renderExperiment(host, route('v1'));
  await flush();
  // The gate section shows a broken block with the verbatim reason...
  const broken = descendantsWithClass(host, 'v2-state').filter((n) => n.getAttribute('data-kind') === 'broken');
  assert(broken.length >= 1, 'the gate failure surfaces as a broken state');
  assert(allText(broken[0]).includes('500'), 'the HTTP reason is shown verbatim');
  // ...but the per-entry A/B (a different endpoint) still rendered.
  assert(firstWithClass(host, 'v2-dt'), 'an independent section still rendered — one failure does not break the screen');
  restore();
});

test('experiment: no generation selected shows an honest empty state, not a blank', async () => {
  seed();
  resetExperimentView();
  const host = makeHost();
  renderExperiment(host, route(null));
  await flush();
  const empty = descendantsWithClass(host, 'v2-state').filter((n) => n.getAttribute('data-kind') === 'empty');
  assert(empty.length >= 1, 'an empty state, not a blank screen');
  assert(allText(host).toLowerCase().includes('no generation selected'));
});

test('experiment: champion is always resolved from the parent — endpoints carry {epoch,champion,challenger}', async () => {
  seed();
  resetExperimentView();
  const seen = [];
  const original = globalThis.fetch;
  globalThis.fetch = async (url) => {
    seen.push(url);
    const routes = allRoutes();
    let body;
    for (const [needle, value] of Object.entries(routes)) {
      if (url.includes(needle)) { body = value; break; }
    }
    return { ok: true, status: 200, headers: new Map(), json: async () => body || {}, text: async () => '{}' };
  };
  const host = makeHost();
  renderExperiment(host, route('v1'));
  await flush();
  globalThis.fetch = original;
  const gateCall = seen.find((u) => u.includes('/gate'));
  assert(gateCall && gateCall.includes('/e0/v0/v1/gate'), `gate URL threads epoch/champion/challenger: ${gateCall}`);
  const gridCall = seen.find((u) => u.includes('matchup-grid'));
  assert(gridCall && gridCall.includes('/e0/v0/v1'), `grid URL threads epoch/champion/challenger: ${gridCall}`);
  const judgeCall = seen.find((u) => u.includes('per-judge-comparison'));
  assert(judgeCall && judgeCall.includes('/e0/v0/v1/'), `per-judge URL threads epoch/champion/challenger: ${judgeCall}`);
  const driftCall = seen.find((u) => u.includes('drift-movements'));
  assert(driftCall && driftCall.includes('/v1'), 'drift-movements keyed on the challenger');
});

await run();
