// test/v2_experiment.test.mjs — the v2 Experiment view as a CAUSAL NARRATIVE.
//
// DASHBOARD-V2 §3 (graphical & interactive) + §4.4. The Experiment view
// reads like the sentence zicato measures — a CODE CHANGE → a BEHAVIORAL
// CHANGE → a VERDICT — rendered as THREE LINKED VISUAL PANELS:
//
//   1. CAUSE   — the patch, as a real red/green diff viewer (restored):
//      the instruction text the patch edited, labeled with the mutation
//      id + op + rationale.
//   2. EFFECT  — drift movement, visual: the bet (predicted vs actual),
//      the drift-KIND composition, and the per-entry A/B with pass→fail
//      flips; each entry row drills to its run.
//   3. VERDICT — the gate, visual: the gate ladder + the scalar waterfall.
//
// Pins:
//   * the three numbered panels are present and ordered cause→effect→verdict.
//   * the CAUSE diff renders red (deletion) AND green (addition) lines from
//     the realized file diff, labeled with the mutation id + op + rationale.
//   * the EFFECT drift composition renders per-kind movement bars.
//   * a per-entry pass→fail flip is flagged; a row drills to the Run view.
//   * the VERDICT gate ladder emphasizes the one fired rule + the waterfall
//     decomposes the scalar champion→challenger.
//   * the SEED (v0) renders an HONEST baseline panel in every comparative
//     section — NO red broken/error block (the seed-error regression fix).
//   * honest states: a section whose fetch fails degrades to a broken
//     block with the reason — the rest of the screen survives.
//   * the comparative endpoints carry {epoch, champion, challenger}.
//   * interactivity: hovering a drift kind / patch toggles the cross-panel
//     highlight wash (.v2-exp-lit).
//
// fetch is mocked synchronously; the view's async sections resolve on the
// microtask queue, so each assertion flushes a few microtasks first.

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
function brokenBlocks(node) {
  return descendantsWithClass(node, 'v2-state').filter((n) => n.getAttribute('data-kind') === 'broken');
}
function emptyBlocks(node) {
  return descendantsWithClass(node, 'v2-state').filter((n) => n.getAttribute('data-kind') === 'empty');
}

async function flush(n = 8) { for (let i = 0; i < n; i++) await Promise.resolve(); }

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

// Seed the lineage + epoch contract: v0 is the SEED (no parent), v1 is the
// challenger derived from v0 with a structured patch.
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
        generation_id: 'v0',
        parent_generation_id: null,
        hypothesis: { core_idea: 'Baseline presenter instructions.' },
        outcome: null,
        patches: {},
      },
      {
        generation_id: 'v1',
        parent_generation_id: 'v0',
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
          'presenter.instruction': {
            mutation_id: 'presenter.instruction',
            op: 'replace',
            target: 'agents/presenter.py',
            rationale: 'Add an outline guard so the agent does not ramble.',
            new_content: 'Follow the slide outline strictly.\nDo not exceed the agreed scope.',
          },
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
const PATCHES = {
  epoch_id: 'e0', generation_id: 'v1',
  patches: [
    {
      id: 'p1', mutation_id: 'presenter.instruction', op: 'replace',
      target: 'agents/presenter.py',
      rationale: 'Add an outline guard so the agent does not ramble.',
      new_content: 'Follow the slide outline strictly.\nDo not exceed the agreed scope.',
    },
  ],
};
const DIFF = {
  epoch_id: 'e0', generation_id: 'v1', parent_generation_id: 'v0',
  files: [
    {
      path: 'agents/presenter.py', status: 'modified',
      old_content: 'Present the deck.\nBe thorough.',
      new_content: 'Follow the slide outline strictly.\nDo not exceed the agreed scope.',
      old_binary: false, new_binary: false,
    },
  ],
};

function allRoutes() {
  return {
    'drift-movements': DRIFT,
    'matchup-grid': GRID,
    'per-entry': { entries: [{ entry_id: 'q3_metrics_outline', drift_loss: 71.0, pass_fail: 0 }, { entry_id: 'waffles_single', drift_loss: 60.5, pass_fail: 0 }] },
    '/patches': PATCHES,   // must precede '/diff' (substring) — both under /api/files
    '/diff': DIFF,
    '/gate': GATE,
  };
}

function makeHost() { return globalThis.document.createElement('div'); }
function route(genId) { return { view: 'experiment', params: genId == null ? {} : { generationId: genId } }; }

// ===========================================================================

test('experiment: three numbered panels — cause → effect → verdict', async () => {
  seed();
  resetExperimentView();
  const restore = mockFetch(allRoutes());
  const host = makeHost();
  renderExperiment(host, route('v1'));
  await flush();
  const panels = descendantsWithClass(host, 'v2-exp-panel');
  assertEqual(panels.length, 3, 'exactly three panels');
  assertEqual(panels[0].getAttribute('data-panel'), '1', 'panel 1 is the cause');
  assertEqual(panels[1].getAttribute('data-panel'), '2', 'panel 2 is the effect');
  assertEqual(panels[2].getAttribute('data-panel'), '3', 'panel 3 is the verdict');
  assert(allText(panels[0]).toLowerCase().includes('change'), 'panel 1 titled "The change"');
  assert(allText(panels[1]).toLowerCase().includes('moved'), 'panel 2 titled "What moved"');
  assert(allText(panels[2]).toLowerCase().includes('verdict'), 'panel 3 titled "The verdict"');
  restore();
});

test('experiment: header shows champion → challenger + the verdict glyph', async () => {
  seed();
  resetExperimentView();
  const restore = mockFetch(allRoutes());
  const host = makeHost();
  renderExperiment(host, route('v1'));
  await flush();
  const ids = allText(firstWithClass(host, 'v2-exp-ids'));
  assert(ids.includes('v0'), 'champion (parent) shown by default — no opt-in');
  assert(ids.includes('v1'), 'challenger shown');
  const verdict = firstWithClass(host, 'v2-exp-verdict');
  assert(allText(verdict).toLowerCase().includes('rejected'), 'big verdict glyph reads the decision');
  restore();
});

test('experiment: CAUSE renders the patch as a real red/green diff', async () => {
  seed();
  resetExperimentView();
  const restore = mockFetch(allRoutes());
  const host = makeHost();
  renderExperiment(host, route('v1'));
  await flush();
  const card = firstWithClass(host, 'v2-diff-card');
  assert(card, 'a diff card rendered');
  // Labeled with the mutation id + op + rationale.
  assert(allText(card).includes('presenter.instruction'), 'mutation-point id labeled');
  assert(allText(card).includes('replace'), 'the op is labeled');
  assert(allText(card).includes('outline guard'), 'the operator rationale is shown');
  // A real line diff: at least one removed (red) line AND one added (green) line.
  const dels = descendantsWithClass(card, 'v2-diff-del');
  const adds = descendantsWithClass(card, 'v2-diff-add');
  assert(dels.length >= 1, `a red deletion line (got ${dels.length})`);
  assert(adds.length >= 1, `a green addition line (got ${adds.length})`);
  assert(dels.some((n) => allText(n).includes('thorough')), 'the old (removed) instruction text is shown');
  assert(adds.some((n) => allText(n).includes('outline')), 'the new (added) instruction text is shown');
  restore();
});

test('experiment: EFFECT bet renders predicted/actual drift + alignment', async () => {
  seed();
  resetExperimentView();
  const restore = mockFetch(allRoutes());
  const host = makeHost();
  renderExperiment(host, route('v1'));
  await flush();
  const bet = firstWithClass(host, 'v2-exp-bet');
  assert(bet, 'the bet figure rendered');
  assert(allText(bet).includes('slide-structure'), 'core idea shown');
  assert(allText(bet).includes('rambled'), 'why shown');
  const bars = descendantsWithClass(host, 'dbar');
  assert(bars.length >= 2, `predicted + actual diverging bars (got ${bars.length})`);
  const align = firstWithClass(host, 'v2-exp-align');
  assert(align, 'alignment verdict rendered');
  assert(allText(align).includes('2/2'), 'both predicted directions matched');
  assert(firstWithClass(host, 'v2-exp-align-hit'), 'bet-held badge shown');
  restore();
});

test('experiment: EFFECT drift composition shows which behaviors moved, by kind', async () => {
  seed();
  resetExperimentView();
  const restore = mockFetch(allRoutes());
  const host = makeHost();
  renderExperiment(host, route('v1'));
  await flush();
  const comp = firstWithClass(host, 'v2-driftcomp');
  assert(comp, 'the drift-kind composition rendered');
  const rows = descendantsWithClass(comp, 'v2-driftcomp-row');
  assertEqual(rows.length, 2, 'one row per drift kind');
  const kinds = rows.map((r) => r.getAttribute('data-kind'));
  assert(kinds.includes('off_topic'), 'off_topic kind present');
  assert(kinds.includes('verbosity'), 'verbosity kind present');
  // off_topic improved (−5 → green), verbosity worsened (+4 → red).
  assert(descendantsWithClass(comp, 'v2-driftcomp-good').length >= 1, 'an improved kind is green');
  assert(descendantsWithClass(comp, 'v2-driftcomp-bad').length >= 1, 'a worsened kind is red');
  restore();
});

test('experiment: EFFECT per-entry A/B flags a pass→fail flip and drills to the run', async () => {
  seed();
  resetExperimentView();
  const restore = mockFetch(allRoutes());
  const host = makeHost();
  renderExperiment(host, route('v1'));
  await flush();
  const table = firstWithClass(host, 'v2-dt');
  assert(table, 'per-entry data table rendered');
  const flip = firstWithClass(host, 'v2-exp-flip-regress');
  assert(flip, 'a pass→fail flip is flagged');
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

test('experiment: VERDICT gate ladder emphasizes the fired rule + waterfall decomposes the scalar', async () => {
  seed();
  resetExperimentView();
  const restore = mockFetch(allRoutes());
  const host = makeHost();
  renderExperiment(host, route('v1'));
  await flush();
  const verdictPanel = descendantsWithClass(host, 'v2-exp-panel')[2];
  // Gate ladder with exactly one fired rule.
  const ladder = firstWithClass(verdictPanel, 'gate-ladder');
  assert(ladder, 'gate ladder rendered in the verdict panel');
  const fired = descendantsWithClass(verdictPanel, 'gate-fired');
  assertEqual(fired.length, 1, 'exactly one fired rule emphasized');
  assert(allText(fired[0]).toLowerCase().includes('scalar margin'), 'the fired rule is the scalar margin');
  assert(allText(verdictPanel).includes('scalar margin not met'), 'the gate reason is shown');
  // Scalar waterfall decomposes per component, both signs.
  const wf = firstWithClass(verdictPanel, 'swfall');
  assert(wf, 'scalar waterfall rendered');
  const names = descendantsWithClass(wf, 'swfall-name').map(allText);
  assert(names.includes('drift') && names.includes('pass'), 'drift + pass components present');
  assert(descendantsWithClass(wf, 'swfall-good').length >= 1, 'an improved component is green');
  assert(descendantsWithClass(wf, 'swfall-bad').length >= 1, 'a regressed component is red');
  restore();
});

test('experiment: SEED (v0) renders honest baseline panels — NO error blocks', async () => {
  seed();
  resetExperimentView();
  const restore = mockFetch(allRoutes());
  const host = makeHost();
  renderExperiment(host, route('v0'));
  await flush();
  // The header reads "seed", not a champion id.
  const ids = allText(firstWithClass(host, 'v2-exp-ids'));
  assert(ids.includes('seed'), 'the header marks v0 as the seed');
  // CRITICAL: no section hard-errors for the seed.
  const broken = brokenBlocks(host);
  assertEqual(broken.length, 0, `the seed shows NO red error block (got ${broken.length})`);
  // The comparative panels say so honestly.
  const empties = emptyBlocks(host).map(allText).join(' ');
  assert(empties.toLowerCase().includes('seed') || empties.toLowerCase().includes('baseline'),
    'an honest seed/baseline panel is shown');
  // All three panels still render (the screen stays up).
  assertEqual(descendantsWithClass(host, 'v2-exp-panel').length, 3, 'all three panels present for the seed');
  restore();
});

test('experiment: SEED never calls the gate / matchup endpoints', async () => {
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
  renderExperiment(host, route('v0'));
  await flush();
  globalThis.fetch = original;
  assert(!seen.some((u) => u.includes('/gate')), 'no gate fetch for the seed');
  assert(!seen.some((u) => u.includes('matchup-grid')), 'no matchup fetch for the seed');
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
  const broken = brokenBlocks(host);
  assert(broken.length >= 1, 'the gate failure surfaces as a broken state');
  assert(broken.some((n) => allText(n).includes('500')), 'the HTTP reason is shown verbatim');
  // ...but the per-entry A/B (a different endpoint) still rendered.
  assert(firstWithClass(host, 'v2-dt'), 'an independent section still rendered — one failure does not break the screen');
  restore();
});

test('experiment: the diff endpoint failing does NOT sink the CAUSE — structured patches still render', async () => {
  seed();
  resetExperimentView();
  const routes = { ...allRoutes(), '/diff': ERROR };
  const restore = mockFetch(routes);
  const host = makeHost();
  renderExperiment(host, route('v1'));
  await flush();
  const card = firstWithClass(host, 'v2-diff-card');
  assert(card, 'the structured patch still renders a card when the diff endpoint fails');
  assert(allText(card).includes('presenter.instruction'), 'the mutation id is still shown');
  // Falls back to the patch new_content as added-only.
  assert(descendantsWithClass(card, 'v2-diff-add').length >= 1, 'the new instruction text shows as added');
  const cause = descendantsWithClass(host, 'v2-exp-panel')[0];
  assertEqual(brokenBlocks(cause).length, 0, 'the CAUSE panel does not show a broken block');
  restore();
});

test('experiment: no generation selected shows an honest empty state, not a blank', async () => {
  seed();
  resetExperimentView();
  const host = makeHost();
  renderExperiment(host, route(null));
  await flush();
  assert(emptyBlocks(host).length >= 1, 'an empty state, not a blank screen');
  assert(allText(host).toLowerCase().includes('no generation selected'));
});

test('experiment: comparative endpoints carry {epoch, champion, challenger}', async () => {
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
  const driftCall = seen.find((u) => u.includes('drift-movements'));
  assert(driftCall && driftCall.includes('/v1'), 'drift-movements keyed on the challenger');
  const patchesCall = seen.find((u) => u.includes('/patches'));
  assert(patchesCall && patchesCall.includes('/e0/v1/'), `patches URL threads epoch/gen: ${patchesCall}`);
  const diffCall = seen.find((u) => u.includes('/diff'));
  assert(diffCall && diffCall.includes('/e0/v1/'), `diff URL threads epoch/gen: ${diffCall}`);
});

test('experiment: interactivity — hovering a drift kind toggles the cross-panel highlight wash', async () => {
  seed();
  resetExperimentView();
  const restore = mockFetch(allRoutes());
  const host = makeHost();
  renderExperiment(host, route('v1'));
  await flush();
  const kindRow = firstWithClass(host, 'v2-driftcomp-row');
  assert(kindRow, 'a drift kind row exists');
  const card = firstWithClass(host, 'v2-diff-card');
  assert(card, 'a cause diff card exists');
  // Before hover: nothing lit.
  assert(!card.classList.contains('v2-exp-lit'), 'cause card not lit at rest');
  // Hover the drift kind → the CAUSE panel washes (the change under study).
  kindRow.dispatchEvent({ type: 'mouseenter', target: kindRow });
  assert(card.classList.contains('v2-exp-lit'), 'hovering a drift kind lights the cause card');
  // Leave → clears.
  kindRow.dispatchEvent({ type: 'mouseleave', target: kindRow });
  assert(!card.classList.contains('v2-exp-lit'), 'leaving clears the wash');
  restore();
});

await run();
