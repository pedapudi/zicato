// test/phase0_round_decision.test.mjs — L3 rebuilt as the DECISION VIEW.
//
// Pins the centerpiece behaviour:
//   1. GATE LADDER renders the /gate rules, the fired rule emphasized.
//   2. Per-entry A/B renders as a DIVERGING BAR and flags a pass→fail
//      flip (a monotonicity regression).
//   3. PRIMARY DRIVER call-out shows the judge + Δ.
//   4. PROMOTE / REJECT controls are DISABLED under a read-only workspace.
//   5. Controls are ENABLED + honestly labelled when writable.
//   6. Cold deep-link (every endpoint 404s) degrades — never throws.

import { installDom, test, run, assert } from './harness.mjs';

installDom();

const { state } = await import('../js/core/state.js');
const round = await import('../js/views/phase0_round.js');

function installRoundSlots() {
  for (const id of ['phase0-round-vs', 'phase0-round-entries',
    'phase0-round-judges', 'phase0-round-decision']) {
    const node = document.createElement('div');
    node.id = id;
    document.body.appendChild(node);
  }
}

function mockFetch(handler) {
  const original = globalThis.fetch;
  globalThis.fetch = async (url) => {
    const r = handler(url);
    const ok = !(r && r.__notfound);
    return {
      ok, status: ok ? 200 : 404, headers: new Map(),
      json: async () => (ok ? r : {}), text: async () => JSON.stringify(ok ? r : {}),
    };
  };
  return () => { globalThis.fetch = original; };
}

// A fully-populated rejected matchup: the scalar_margin rule is the first
// failing rule (fired), pass_rate / namespace are not_reached.
const GATE_REJECTED = {
  decision: 'rejected',
  reason: 'challenger regressed: loss rose by 10.12',
  delta_scalar: 10.12,
  delta_pass_rate: 0.0,
  rules: [
    { id: 'regression_suite', label: 'Regression suite', status: 'pass',
      detail: 'no suite regressions', fired: false },
    { id: 'scalar_margin', label: 'Scalar margin', status: 'fail',
      detail: 'needs Δloss ≤ -0.010; got +10.12', fired: true, margin: 0.01 },
    { id: 'pass_rate_monotonicity', label: 'Pass-rate monotonicity',
      status: 'not_reached', detail: '', fired: false },
    { id: 'namespace_monotonicity', label: 'Namespace monotonicity',
      status: 'not_reached', detail: '', fired: false },
  ],
  scalar_components: { champion: 47.58, challenger: 57.70 },
  primary_driver: { judge: 'incorporates_feedback', delta: 9.5 },
};

const GRID = {
  epoch_id: 'e0', champion: 'v1', challenger: 'v2',
  entry_grid: [
    // alpha flips pass -> fail: must be flagged.
    { entry_id: 'alpha', parent_drift_loss: 10.0, child_drift_loss: 25.0,
      parent_pass: 1, child_pass: 0, delta: 15.0 },
    // beta improves, stays passing.
    { entry_id: 'beta', parent_drift_loss: 40.0, child_drift_loss: 32.0,
      parent_pass: 1, child_pass: 1, delta: -8.0 },
  ],
  scalar: {
    parent: 47.58, child: 57.70, delta: 10.12,
    components: { off_topic: 9.0, verbosity: 1.12 },
  },
  source: 'loss_files',
};

const JUDGES = {
  epoch_id: 'e0', champion: 'v1', challenger: 'v2',
  judges: [
    { judge_name: 'incorporates_feedback', champion_weighted_loss: 3.0,
      challenger_weighted_loss: 12.5, delta: 9.5 },
  ],
  primary_driver: 'incorporates_feedback',
};

function routeAll(url) {
  if (url.includes('/gate')) return GATE_REJECTED;
  if (url.includes('/matchup-grid/')) return GRID;
  if (url.includes('/per-judge-comparison')) return JUDGES;
  return {};
}

// Render twice with a microtask gap so the lazy fetches land in cache,
// then return the slot textContents.
function renderTwice(params) {
  round.renderPhase0Round(params);
  return new Promise((resolve) => {
    setTimeout(() => {
      round.renderPhase0Round(params);
      resolve();
    }, 30);
  });
}

// --- 1. GATE LADDER + fired rule emphasis ----------------------------

test('L3 gate ladder renders rules; the fired rule is emphasized', async () => {
  installRoundSlots();
  round.resetRoundCaches();
  state.epochDef = { epoch_id: 'e0' };
  state.health = { read_only: true };
  const restore = mockFetch(routeAll);
  try {
    await renderTwice({ epochId: 'e0', championId: 'v1', challengerId: 'v2' });
    const vs = document.getElementById('phase0-round-vs');
    const text = vs.textContent;
    assert(text.includes('Regression suite') && text.includes('Scalar margin')
      && text.includes('Pass-rate monotonicity'),
      `all gate rule labels must render; got: ${text.slice(0, 400)}`);
    // The shared gateLadder emphasizes the fired rule by tagging its
    // row with the `gate-fired` class (and the fired row carries the
    // scalar-margin label).
    let firedRow = null;
    const walk = (n) => {
      if (n.className && String(n.className).includes('gate-fired')) firedRow = n;
      for (const c of n.children) walk(c);
    };
    walk(vs);
    assert(firedRow != null, `the fired rule row must carry the gate-fired class`);
    assert(String(firedRow.textContent).includes('Scalar margin'),
      `the fired (gate-fired) row must be the scalar-margin rule; got: ${String(firedRow.textContent).slice(0, 200)}`);
  } finally {
    restore();
  }
});

// --- 2. per-entry DIVERGING BAR + pass→fail flag ---------------------

test('L3 per-entry diverging bar flags a pass→fail entry', async () => {
  installRoundSlots();
  round.resetRoundCaches();
  state.epochDef = { epoch_id: 'e0' };
  state.health = { read_only: true };
  const restore = mockFetch(routeAll);
  try {
    await renderTwice({ epochId: 'e0', championId: 'v1', challengerId: 'v2' });
    const entries = document.getElementById('phase0-round-entries');
    const text = entries.textContent;
    assert(text.includes('alpha') && text.includes('beta'),
      `both entries must render; got: ${text.slice(0, 400)}`);
    // The diverging-bar component is used (not a flat table).
    let barPresent = false;
    let tableCount = 0;
    let flipFlagged = false;
    const walk = (n) => {
      if (n.className && n.className.includes('dbar')) barPresent = true;
      if (n.localName === 'table') tableCount += 1;
      const title = n.getAttribute && n.getAttribute('title');
      if (title && title.includes('pass→fail')) flipFlagged = true;
      for (const c of n.children) walk(c);
    };
    walk(entries);
    assert(barPresent, `per-entry A/B must render as a diverging bar`);
    assert(tableCount === 0, `per-entry A/B must NOT be a flat table; got ${tableCount}`);
    assert(flipFlagged,
      `the pass→fail entry (alpha) must carry the monotonicity flag; got: ${text.slice(0, 400)}`);
  } finally {
    restore();
  }
});

// --- 3. PRIMARY DRIVER ----------------------------------------------

test('L3 primary-driver call-out shows the judge and Δ', async () => {
  installRoundSlots();
  round.resetRoundCaches();
  state.epochDef = { epoch_id: 'e0' };
  state.health = { read_only: true };
  const restore = mockFetch(routeAll);
  try {
    await renderTwice({ epochId: 'e0', championId: 'v1', challengerId: 'v2' });
    const judges = document.getElementById('phase0-round-judges');
    const text = judges.textContent;
    assert(text.includes('driven by judge'),
      `primary-driver call-out must render; got: ${text.slice(0, 300)}`);
    assert(text.includes('incorporates_feedback'),
      `driver judge name must surface; got: ${text.slice(0, 300)}`);
    assert(text.includes('9.500') || text.includes('+9.500'),
      `driver Δ must surface; got: ${text.slice(0, 300)}`);
  } finally {
    restore();
  }
});

// --- 4. controls DISABLED under read-only ---------------------------

test('L3 promote/reject controls are disabled under read-only', async () => {
  installRoundSlots();
  round.resetRoundCaches();
  state.epochDef = { epoch_id: 'e0' };
  state.health = { read_only: true };
  const restore = mockFetch(routeAll);
  try {
    await renderTwice({ epochId: 'e0', championId: 'v1', challengerId: 'v2' });
    const decision = document.getElementById('phase0-round-decision');
    const buttons = [];
    const walk = (n) => {
      if (n.localName === 'button') buttons.push(n);
      for (const c of n.children) walk(c);
    };
    walk(decision);
    assert(buttons.length === 2, `must render promote + reject buttons; got ${buttons.length}`);
    for (const b of buttons) {
      assert(b.hasAttribute('disabled'),
        `control "${b.textContent}" must be disabled under read-only`);
    }
    assert(decision.textContent.toLowerCase().includes('read-only'),
      `read-only note must surface; got: ${decision.textContent.slice(0, 200)}`);
  } finally {
    restore();
  }
});

// --- 5. controls ENABLED + honest label when writable ---------------

test('L3 controls enabled + honest "manual enactment" note when writable', async () => {
  installRoundSlots();
  round.resetRoundCaches();
  state.epochDef = { epoch_id: 'e0' };
  state.health = { read_only: false };
  const restore = mockFetch(routeAll);
  try {
    await renderTwice({ epochId: 'e0', championId: 'v1', challengerId: 'v2' });
    const decision = document.getElementById('phase0-round-decision');
    const buttons = [];
    const walk = (n) => {
      if (n.localName === 'button') buttons.push(n);
      for (const c of n.children) walk(c);
    };
    walk(decision);
    assert(buttons.length === 2, `must render two controls; got ${buttons.length}`);
    for (const b of buttons) {
      assert(!b.hasAttribute('disabled'),
        `control "${b.textContent}" must be enabled when writable`);
    }
    assert(decision.textContent.toLowerCase().includes('manual enactment'),
      `honest "manual enactment" note must surface; got: ${decision.textContent.slice(0, 200)}`);
  } finally {
    restore();
  }
});

// --- 6. cold deep-link: every endpoint 404s, no throw ---------------

test('L3 cold deep-link degrades gracefully (all endpoints 404)', async () => {
  installRoundSlots();
  round.resetRoundCaches();
  state.epochDef = { epoch_id: 'e0' };
  state.health = { read_only: true };
  const restore = mockFetch(() => ({ __notfound: true }));
  let threw = false;
  try {
    await renderTwice({ epochId: 'e0', championId: 'v1', challengerId: 'v2' });
    const vs = document.getElementById('phase0-round-vs');
    const entries = document.getElementById('phase0-round-entries');
    // Must have painted *something* (empty/loading state) and not crashed.
    assert(vs.textContent.length > 0, `vs slot must paint a fallback`);
    assert(entries.textContent.length > 0, `entries slot must paint a fallback`);
  } catch {
    threw = true;
  } finally {
    restore();
  }
  assert(!threw, `cold deep-link must never throw`);
});

await run();
