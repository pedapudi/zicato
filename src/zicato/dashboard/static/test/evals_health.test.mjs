// test/evals_health.test.mjs — the WS-HEALTH instrument panel (EVAL-VIEW.md §5).
//
// Pins: the model normalizes /api/epoch/{id}/eval-health defensively; the render
// paints the mono floor + MDE strip (stating the formula + n — never a bare
// number, §4.3), the ranked noisy / dead / runtime tables (dataTable idiom, no
// chips), the holdout-budget + rotation-cadence readout, and the redundancy
// deferral; unmeasured / empty payloads honest-empty with a NAMED reason (never a
// fabricated 0.0); a no-op repaint churns no DOM (digest-gated).

import { installDom, test, run, assert, assertEqual } from './harness.mjs';

installDom();

const eh = await import('../js/panels/evals_health.js');
const ui = await import('../js/ui.js');

function allByClass(host, cls) {
  return host.querySelectorAll('[class]').filter((n) =>
    (n.getAttribute('class') || '').split(/\s+/).includes(cls));
}
function textOf(node) { return node.textContent || ''; }

// A fully-populated instrument-health payload (the build_eval_health shape).
const H_FULL = {
  found: true,
  mde: {
    floor_measured: true, floor: 0.06, floor_statistic: 'delta_std', replicates: 6,
    replicates_source: 'contract scoring.json', usable: true,
    formula_n: 6, df: 10, mde: 0.10764, mde_relaxed: 0.09324,
    alpha: 0.05, alpha_relaxed: 0.1, power: 0.8,
    formula: "MDE = (t_{α/2,df} + t_{β,df})·sd·√(2/n),  sd = the floor's delta_std,  df = 2·(n−1)",
    note: null,
  },
  noisiest: [
    { entry_id: 'entryA', flip_rate: 1 / 3, slice: 'train', calibration_runs: 6 },
    { entry_id: 'entryB', flip_rate: 0.0, slice: 'holdout', calibration_runs: 6 },
  ],
  dead: [{ entry_id: 'entryD', discrimination_pairs: 4, slice: 'train' }],
  insufficient: [{ entry_id: 'entryE', discrimination_pairs: 2, slice: 'train' }],
  runtime_cost: [{ entry_id: 'entryA', runtime_ms_mean: 41200, replicate_total: 6, slice: 'train' }],
  holdout_budget: {
    generation_id: 'g3', confirmed: true, ladder_released: true,
    ladder_budget_total: 5, ladder_budget_remaining: 3, threshold: 0.1,
  },
  rotation: {
    rotate_holdout: true, max_generations_per_contract: 8, evaluated_generations: 8,
    refresh_recommended: true, recommendation: 'refresh the contract — roll the epoch',
  },
  redundancy: { available: false, clusters: [], note: 'no reflection built for this epoch' },
};

const H_UNMEASURED = {
  found: true,
  mde: {
    floor_measured: false, floor: null, replicates: 1, usable: false,
    mde: null, mde_relaxed: null, note: 'floor unmeasured — run the A/A calibration',
  },
  noisiest: [], dead: [], insufficient: [], runtime_cost: [],
  holdout_budget: null,
  rotation: {
    rotate_holdout: false, max_generations_per_contract: null, evaluated_generations: 0,
    refresh_recommended: false, recommendation: null,
  },
  redundancy: { available: false, clusters: [], note: 'no reflection built for this epoch' },
};

// ---- model ----------------------------------------------------------

test('evalHealthModel: normalizes the payload into the render shape (camelCase)', () => {
  const m = eh.evalHealthModel(H_FULL);
  assertEqual(m.found, true, 'found');
  assertEqual(m.mde.usable, true, 'mde usable');
  assertEqual(m.mde.floor, 0.06, 'floor carried');
  assertEqual(m.mde.formulaN, 6, 'n carried');
  assertEqual(m.noisiest.length, 2, 'two noisy rows');
  assertEqual(m.noisiest[0].entryId, 'entryA', 'noisiest[0]');
  assertEqual(m.dead[0].entryId, 'entryD', 'dead[0]');
  assertEqual(m.dead[0].pairs, 4, 'dead pairs');
  assertEqual(m.insufficient[0].entryId, 'entryE', 'insufficient[0]');
  assertEqual(m.runtimeCost[0].runtimeMsMean, 41200, 'runtime mean');
  assertEqual(m.holdoutBudget.budgetRemaining, 3, 'holdout budget remaining');
  assertEqual(m.rotation.refreshRecommended, true, 'refresh recommended');
});

test('evalHealthModel: a garbage payload degrades to the honest empty shape', () => {
  const m = eh.evalHealthModel(null);
  assertEqual(m.found, false, 'not found');
  assertEqual(m.mde.usable, false, 'mde not usable');
  assertEqual(m.noisiest.length, 0, 'no noisy rows');
  assertEqual(m.holdoutBudget, null, 'no holdout budget');
});

// ---- render: the MDE strip (§4.3 — formula + floor + n, never a bare number) ----

test('renderEvalHealth: the strip states the floor, the n, and the formula', () => {
  const node = eh.renderEvalHealth(eh.evalHealthModel(H_FULL), {});
  const t = textOf(node);
  assert(t.includes('noise floor'), 'labels the noise floor');
  assert(t.includes('replicates (n)'), 'labels the replicate count');
  assert(t.includes('contract scoring.json'), 'names the tier that set the replicate count');
  assert(t.includes('delta_std'), 'names the floor statistic the ladder read');
  assert(t.includes('MDE'), 'shows the MDE');
  assert(t.includes('df=10'), 'the formula line states df');
  assert(allByClass(node, 'dn-stat').length >= 4, 'the strip uses the dn-stat idiom (floor/n/two MDE rungs)');
});

test('renderEvalHealth: an unmeasured floor prints the reason, NEVER a 0.0 bound', () => {
  const node = eh.renderEvalHealth(eh.evalHealthModel(H_UNMEASURED), {});
  const t = textOf(node);
  assert(t.includes('floor unmeasured'), 'names the unmeasured-floor reason');
  assert(!/MDE[^a-z]*0\.0000/.test(t), 'no fabricated 0.0000 MDE bound');
});

// ---- render: the ranked lists ---------------------------------------

test('renderEvalHealth: noisiest table lists measured entries in descending flip order', () => {
  const node = eh.renderEvalHealth(eh.evalHealthModel(H_FULL), {});
  const t = textOf(node);
  assert(t.includes('Noisiest evals'), 'has the noisiest subhead');
  assert(t.includes('33%'), 'entryA renders its 1/3 flip rate as a percentage');
  const posA = t.indexOf('entryA');
  const posB = t.indexOf('entryB');
  assert(posA >= 0 && posB >= 0 && posA < posB, 'entryA (noisier) sorts before entryB');
});

test('renderEvalHealth: the dead panel flags dead channels + names the insufficient honesty case', () => {
  const node = eh.renderEvalHealth(eh.evalHealthModel(H_FULL), {});
  assert(allByClass(node, 'dn-eh-dead').length >= 1, 'entryD renders as a dead row');
  const t = textOf(node);
  assert(t.includes('entryD'), 'dead entry named');
  assert(t.includes('insufficient'), 'the insufficient-comparisons case is named honestly');
});

test('renderEvalHealth: noisiest empty-states with the calibration reason when unmeasured', () => {
  const node = eh.renderEvalHealth(eh.evalHealthModel(H_UNMEASURED), {});
  const t = textOf(node);
  assert(t.includes('flip rate unmeasured'), 'names the unmeasured reason, not an empty table');
  assert(t.includes('no dead channels detected'), 'dead panel empty-states honestly');
});

test('renderEvalHealth: runtime cost renders the mean wall-clock in the console duration register', () => {
  const node = eh.renderEvalHealth(eh.evalHealthModel(H_FULL), {});
  const t = textOf(node);
  assert(t.includes('Runtime cost'), 'has the runtime subhead');
  // This panel spells a duration the way every other panel does: `fmtDurationMs`,
  // which writes no space before the unit and carries the ladder up to minutes
  // and hours. It used to print `41.2 s` from a formatter of its own.
  assert(t.includes('41.2s'), 'formats 41200 ms as 41.2s');
  assertEqual(ui.fmtDurationMs(41200), '41.2s', 'the panel prints what the canonical formatter returns');
});

test('renderEvalHealth: the lifecycle panel shows budget spent + a refresh recommendation', () => {
  const node = eh.renderEvalHealth(eh.evalHealthModel(H_FULL), {});
  const t = textOf(node);
  assert(t.includes('holdout budget spent'), 'labels budget spent');
  assert(t.includes('8 / 8 generations mined'), 'states the cadence status');
  assert(t.includes('refresh the contract'), 'surfaces the recommend-only refresh cue');
});

test('renderEvalHealth: redundancy defers to reflect when no reflection is built', () => {
  const node = eh.renderEvalHealth(eh.evalHealthModel(H_FULL), {});
  const t = textOf(node);
  assert(t.includes('Redundancy clusters'), 'has the redundancy subhead');
  assert(t.includes('run reflect'), 'points at reflect (recommend-only)');
});

test('renderEvalHealth: a reflectHref makes the reflect pointer a real link', () => {
  const node = eh.renderEvalHealth(eh.evalHealthModel(H_FULL), { reflectHref: '#/e/e0/instrument' });
  const links = allByClass(node, 'dn-eh-link');
  assert(links.some((a) => a.getAttribute('href') === '#/e/e0/instrument'), 'the reflect link carries the href');
});

test('renderEvalHealth: onEntry wires each entry into its dossier (recommend-only)', () => {
  let opened = null;
  const node = eh.renderEvalHealth(eh.evalHealthModel(H_FULL), { onEntry: (id) => { opened = id; } });
  const btn = allByClass(node, 'dn-eh-entrybtn')[0];
  assert(btn, 'entry renders as an activatable control when onEntry is supplied');
  btn.dispatchEvent({ type: 'click' });
  assertEqual(opened, 'entryA', 'clicking the entry opens its dossier');
});

test('renderEvalHealth: a not-found payload paints the honest empty panel', () => {
  const node = eh.renderEvalHealth(eh.evalHealthModel({ found: false }), {});
  const t = textOf(node);
  assert(t.includes('No indexed evals'), 'names the cold-epoch empty state');
});

// ---- digest (render discipline) -------------------------------------

test('evalHealthDigest: stable across a no-op repaint, changes on real movement', () => {
  const a = eh.evalHealthDigest(eh.evalHealthModel(H_FULL));
  const b = eh.evalHealthDigest(eh.evalHealthModel(H_FULL));
  assertEqual(a, b, 'identical payload → identical digest (no DOM churn on a no-op beat)');
  const moved = JSON.parse(JSON.stringify(H_FULL));
  moved.mde.replicates = 8;
  assert(eh.evalHealthDigest(eh.evalHealthModel(moved)) !== a, 'a replicate-count change moves the digest');
});

test('evalHealth: a no-op repaint churns no DOM (digest-gated render discipline)', () => {
  const host = document.createElement('div');
  const model = eh.evalHealthModel(H_FULL);
  const digest = eh.evalHealthDigest(model);
  ui.gatedSwap(host, digest, () => [eh.renderEvalHealth(model, {})]);
  const first = host.firstChild;
  const writes1 = host.innerHTMLWriteCount();
  ui.gatedSwap(host, digest, () => [eh.renderEvalHealth(model, {})]);
  assert(host.firstChild === first, 'the panel node identity is preserved on a no-op beat');
  assertEqual(host.innerHTMLWriteCount(), writes1, 'no innerHTML writes on the no-op repaint');
});

await run();
