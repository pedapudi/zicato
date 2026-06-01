// test/variant_u.test.mjs — Variant U ("Atlas V") unit tests.
//
// U is the round-6 convergence-IV COMFORTABLE sibling of the anchor: the same
// P+S+Q synthesis (a data-model TREE sidebar + a detail pane built around
// FIRST-CLASS side-by-side comparison + a working back button), but rendered
// ROOMY and LIGHT (solarized-light + Sans default). These pin the headline, the
// carried-forward Round-5 fixes, and the Round-6 additions:
//   * the tree sidebar renders the hierarchy AND navigates multiple gens/epochs;
//   * the promote gate is ON the candidate page, stacked, no overlap (fix #1);
//   * the patch node → this candidate's side-by-side diff, REAL strings (fix #2);
//   * ALL match-ups for a candidate, v0 ≥ 2 (fix #3);
//   * the board view is first-class + shows two candidates' transcripts side by
//     side INLINE on a run select (fix #4 / fix #5 — the signature);
//   * the candidate "compare with…" splits the detail into two candidates (R6);
//   * the FIXED back/up button renders the destination into the MAIN detail pane
//     and leaves the rail untouched (R6 — Q's bug, fixed);
//   * the trellis lives in the Boards view, NOT the epoch overview (fix #6);
//   * the colour + typeface pickers + theme pills switch + persist (light/Sans);
//   * digest-gated repaint is a true no-op.

import { installDom, test, run, assert, assertEqual } from './harness.mjs';

installDom();

// Shims the shell needs (navigate sets location.hash + dispatches hashchange).
class HashChangeEventShim { constructor(type) { this.type = type; } }
globalThis.HashChangeEvent = HashChangeEventShim;
globalThis.window._listeners = {};
globalThis.window.addEventListener = (t, fn) => {
  (globalThis.window._listeners[t] = globalThis.window._listeners[t] || []).push(fn);
};
globalThis.window.dispatchEvent = (ev) => {
  const list = (globalThis.window._listeners && globalThis.window._listeners[ev.type]) || [];
  for (const fn of list.slice()) fn(ev);
  return true;
};
// location.hash setter must re-fire hashchange (like a real browser).
let _hash = '';
Object.defineProperty(globalThis.window.location, 'hash', {
  configurable: true,
  get() { return _hash; },
  set(v) { _hash = v; globalThis.window.dispatchEvent(new HashChangeEventShim('hashchange')); },
});
// core/* reads `location` unscoped in some modules; mirror window.location.
globalThis.location = globalThis.window.location;
// The shell calls connectSSE() (core/sse.js → new EventSource('/events')). Stub
// it as an inert, never-erroring source so no reconnect/refresh timer is
// scheduled (which would keep the Node event loop alive after run()).
class EventSourceShim {
  constructor() { this.readyState = 0; }
  addEventListener() {}
  close() {}
}
EventSourceShim.CLOSED = 2;
globalThis.EventSource = EventSourceShim;

const router = await import('../js/variants/U/router.js');
const svg = await import('../js/variants/U/svg.js');
const ui = await import('../js/variants/U/ui.js');
const shell = await import('../js/variants/U/shell.js');
const data = await import('../js/variants/U/data.js');

const EPOCH_ID = '2026-05-30_e0';

const FIXTURE = {
  '/api/environment': {
    current_epoch_id: EPOCH_ID,
    epochs: [{ epoch_id: EPOCH_ID, generation_count: 3, promoted_count: 1, best_scalar: 70.94, closed: false, goal: 'Make the presentation agent crisper.' }],
  },
  '/api/workspace': {
    current_epoch_id: EPOCH_ID,
    epochs: [{ epoch_id: EPOCH_ID, generation_count: 3, promoted_count: 1, best_scalar: 70.94, closed: false, goal: 'crisper' }],
    sparkline: [],
  },
  '/api/epoch': {
    epoch_id: EPOCH_ID, closed: false, goal: 'Make the presentation agent crisper.', brief: 'Tighten slide structure.',
    experiments: [
      { generation_id: 'v0', parent_generation_id: '', outcome: { decision: 'baseline' } },
      { generation_id: 'v1', parent_generation_id: 'v0', outcome: { decision: 'rejected' } },
      { generation_id: 'v2', parent_generation_id: 'v0', outcome: { decision: 'rejected' } },
    ],
    board: [
      { id: 'waffles_single', kind: 'single_turn', input_preview: 'Make a presentation about waffles.', expectation_kind: 'predicate', budget_s: 180, weight: 1, tags: ['smoke'] },
      { id: 'picky_stakeholder_emulated', kind: 'multi_turn_emulated', input_preview: null, expectation_kind: null, budget_s: 360, weight: 1, tags: ['hard'] },
    ],
  },
  '/api/lineage': { generations: [
    { generation_id: 'v0', epoch_id: EPOCH_ID, parent_generation_id: '', promoted: true },
    { generation_id: 'v1', epoch_id: EPOCH_ID, parent_generation_id: 'v0', promoted: false },
    { generation_id: 'v2', epoch_id: EPOCH_ID, parent_generation_id: 'v0', promoted: false },
  ] },
  '/api/tournaments': { epoch_id: EPOCH_ID, champion_lineage: ['v0'], matchups: [
    { champion: 'v0', challenger: 'v1', decision: 'rejected', delta_scalar: 75.71 },
    { champion: 'v0', challenger: 'v2', decision: 'rejected', delta_scalar: 1.51 },
  ] },
  '/api/score-trajectory': { points: [{ generation_id: 'v0', scalar: 70.94 }, { generation_id: 'v1', scalar: 146.65 }, { generation_id: 'v2', scalar: 72.45 }] },
  '/api/health-report': { epoch_id: EPOCH_ID, healthy: true, findings: [] },
  [`/api/mutations/${EPOCH_ID}`]: {
    generations: ['v0', 'v1', 'v2'],
    mutations: [
      { mutation_id: 'coordinator_prompt', kind: 'prompt', file: 'agent/coordinator.py', role: 'coordinator system prompt', line_start: 10, line_end: 40, patched_generation_ids: ['v1'] },
      { mutation_id: 'oversight_policy', kind: 'policy', file: 'agent/policy.py', role: 'oversight policy', line_start: 1, line_end: 12, patched_generation_ids: ['v1', 'v2'] },
    ],
  },
  [`/api/mutations/${EPOCH_ID}/coordinator_prompt`]: {
    mutation_id: 'coordinator_prompt', epoch_id: EPOCH_ID,
    baseline: { generation_id: 'v0', content: 'You are the coordinator.\nDraft an outline.', file: 'agent/coordinator.py', role: 'coordinator system prompt', line_start: 10, line_end: 40 },
    versions: [{ generation_id: 'v1', op: 'edit', rationale: 'Enforce structure.', content: 'You are the coordinator.\nAlways emit an explicit slide structure.' }],
  },
  [`/api/mutations/${EPOCH_ID}/oversight_policy`]: {
    mutation_id: 'oversight_policy', epoch_id: EPOCH_ID,
    baseline: { generation_id: 'v0', content: 'Oversee loosely.', file: 'agent/policy.py', role: 'oversight policy' },
    versions: [],
  },
  [`/api/files/${EPOCH_ID}/v1/patches`]: { patches: [
    { id: 'p1', mutation_id: 'coordinator_prompt', op: 'edit', new_content: 'You are the coordinator.\nAlways emit an explicit slide structure.', rationale: 'Enforce structure.' },
    { id: 'p2', mutation_id: 'oversight_policy', op: 'edit', new_content: 'Tighten coordinator oversight.' },
  ] },
  [`/api/files/${EPOCH_ID}/v2/patches`]: { patches: [
    { id: 'p3', mutation_id: 'oversight_policy', op: 'edit', new_content: 'Loosen coordinator oversight.' },
  ] },
};
FIXTURE[`/api/generation/${EPOCH_ID}/v0/per-entry`] = { epoch_id: EPOCH_ID, generation_id: 'v0', entries: [
  { entry_id: 'waffles_single', run_id: 'run_v0_waffles', drift_loss: 60.5, pass_fail: 0, runtime_ms: 180000, wall_clock_budget_exceeded: false },
  { entry_id: 'picky_stakeholder_emulated', run_id: 'run_v0_picky', drift_loss: 105.5, pass_fail: 0, runtime_ms: 360000, wall_clock_budget_exceeded: true },
] };
FIXTURE[`/api/generation/${EPOCH_ID}/v1/per-entry`] = { epoch_id: EPOCH_ID, generation_id: 'v1', entries: [
  { entry_id: 'waffles_single', run_id: 'run_v1_waffles', drift_loss: 60.5, pass_fail: 0, runtime_ms: 180000, wall_clock_budget_exceeded: true },
  { entry_id: 'picky_stakeholder_emulated', run_id: 'run_v1_picky', drift_loss: 642.5, pass_fail: 0, runtime_ms: 360000, wall_clock_budget_exceeded: true },
] };
FIXTURE[`/api/generation/${EPOCH_ID}/v2/per-entry`] = { epoch_id: EPOCH_ID, generation_id: 'v2', entries: [
  { entry_id: 'waffles_single', run_id: 'run_v2_waffles', drift_loss: 61.0, pass_fail: 0, runtime_ms: 180000, wall_clock_budget_exceeded: false },
] };
FIXTURE[`/api/matchup-grid/${EPOCH_ID}/v0/v1`] = { epoch_id: EPOCH_ID, champion: 'v0', challenger: 'v1', entry_grid: [
  { entry_id: 'waffles_single', parent_drift_loss: 60.5, child_drift_loss: 60.5, delta: 0, verdict: 'flat', won_by: null },
  { entry_id: 'picky_stakeholder_emulated', parent_drift_loss: 105.5, child_drift_loss: 642.5, delta: 537, verdict: 'regressed', won_by: 'v0' },
] };
FIXTURE[`/api/matchup-grid/${EPOCH_ID}/v0/v2`] = { epoch_id: EPOCH_ID, champion: 'v0', challenger: 'v2', entry_grid: [
  { entry_id: 'waffles_single', parent_drift_loss: 60.5, child_drift_loss: 61.0, delta: 0.5, verdict: 'regressed', won_by: 'v0' },
] };
FIXTURE[`/api/round/${EPOCH_ID}/v0/v1/gate`] = { decision: 'rejected', delta_scalar: 75.71, delta_pass_rate: 0,
  reason: 'challenger regressed: loss rose by 75.71', rules: [
    { id: 'scalar_margin', label: 'Scalar margin', status: 'fail', fired: true, detail: '70.94 → 146.65 (+75.71; needs ≤ -0.01)' },
    { id: 'pass_rate_monotonicity', label: 'Pass-rate monotonicity', status: 'not_reached', fired: false },
    { id: 'namespace_monotonicity', label: 'Namespace monotonicity', status: 'not_reached', fired: false },
  ],
  scalar_components: { champion: { drift: 68.5, schema: 1.43 }, challenger: { drift: 145.64, schema: 0.0 } },
  primary_driver: { judge: 'incorporates_feedback', delta: 24.0 } };
FIXTURE[`/api/round/${EPOCH_ID}/v0/v2/gate`] = { decision: 'rejected', delta_scalar: 1.51, reason: 'regressed', rules: [
  { id: 'scalar_margin', label: 'Scalar margin', status: 'fail', fired: true, detail: '70.94 → 72.45' },
] };
FIXTURE['/api/conversation/run_v0_waffles'] = {
  turns: [
    { seq: 0, role: 'user', agent: 'operator', text: 'Make a presentation about waffles.' },
    { seq: 1, role: 'agent', agent: 'coordinator', text: 'Champion draft with a clear structure.', tool_calls: [{ name: 'write_slide' }] },
  ],
  annotations: [],
};
FIXTURE['/api/conversation/run_v1_waffles'] = {
  turns: [
    { seq: 0, role: 'user', agent: 'operator', text: 'Make a presentation about waffles.' },
    { seq: 1, role: 'agent', agent: 'coordinator', text: 'Challenger drafting an outline now.', tool_calls: [{ name: 'write_slide' }] },
  ],
  annotations: [{ anchor_seq: 1, kind: 'drift', summary: 'omitted the requested structure' }],
};

function installFetch() {
  globalThis.fetch = async (path) => {
    if (Object.prototype.hasOwnProperty.call(FIXTURE, path)) {
      return { ok: true, json: async () => FIXTURE[path] };
    }
    return { ok: false, status: 404, json: async () => ({ error: 'not found: ' + path }) };
  };
}
function freshState() {
  data.invalidate();
  globalThis.window.location.hash = '';
}
function allByClass(host, cls) {
  return host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').split(/\s+/).includes(cls));
}
const CTX = (sink) => ({ navigate: (v, p, o) => { if (sink) sink.last = { v, p, o }; }, href: router.href });

// ---- router ---------------------------------------------------------

test('router: env default; node paths + comparison suffix parse', () => {
  assertEqual(router.parseRoute('').view, 'env');
  assertEqual(router.parseRoute('#/U/').view, 'env');
  assertEqual(router.parseRoute('#/bogus').view, 'env');
  const c = router.parseRoute('#/U/e/E0/gen/v1~cmp=v2');
  assertEqual(c.view, 'candidate'); assertEqual(c.params.gen, 'v1'); assertEqual(c.cmp, 'v2');
  const patch = router.parseRoute('#/U/e/E0/gen/v1/patch');
  assertEqual(patch.params.sub, 'patch');
  const b = router.parseRoute('#/U/e/E0/board/waffles_single~runs=v0,v1');
  assertEqual(b.view, 'board'); assert(b.runs[0] === 'v0' && b.runs[1] === 'v1', 'two runs parsed');
  assertEqual(router.parseRoute('#/U/e/E0/mut/coordinator_prompt').params.mutId, 'coordinator_prompt');
  assertEqual(router.parseRoute('#/U/e/E0/pub').view, 'publication');
});

// ---- router.parentRoute: the back/up destination, ONE step up -------

test('router.parentRoute: walks UP the selection hierarchy', () => {
  // a drilled candidate sub-node steps back to the bare candidate first.
  const fromEntry = router.parentRoute(router.parseRoute('#/U/e/E0/gen/v1/entry/waffles_single'));
  assertEqual(fromEntry.view, 'candidate'); assertEqual(fromEntry.params.gen, 'v1');
  // a bare candidate steps up to the epoch.
  const fromCand = router.parentRoute(router.parseRoute('#/U/e/E0/gen/v1'));
  assertEqual(fromCand.view, 'epoch'); assertEqual(fromCand.params.epochId, 'E0');
  // an active comparison steps back to the bare candidate (drops cmp).
  const fromCmp = router.parentRoute(router.parseRoute('#/U/e/E0/gen/v1~cmp=v2'));
  assertEqual(fromCmp.view, 'candidate'); assert(!fromCmp.opts, 'comparison dropped on the way up');
  // a board run pair steps back to the bare board.
  const fromRuns = router.parentRoute(router.parseRoute('#/U/e/E0/board/waffles_single~runs=v0,v1'));
  assertEqual(fromRuns.view, 'board'); assert(!fromRuns.runs, 'board, no run pair');
  // epoch → environment; environment → null (top).
  assertEqual(router.parentRoute(router.parseRoute('#/U/e/E0')).view, 'env');
  assertEqual(router.parentRoute(router.parseRoute('#/U/')), null);
});

// ---- HEADLINE: the data-model tree sidebar -------------------------

test('tree sidebar: renders the hierarchy AND navigates multiple gens', async () => {
  freshState(); installFetch();
  const tree = await import('../js/variants/U/tree.js');
  const m = await tree.buildModel();
  assert(m.epochs.length >= 1, 'at least one epoch resolved');
  assert(m.gens.length === 3 && m.championId === 'v0', 'three generations, v0 champion');

  const host = document.createElement('div');
  const sink = {};
  window.localStorage.setItem('zicato.U.tree.open', JSON.stringify(['e:' + EPOCH_ID, 'e:' + EPOCH_ID + ':gens', 'e:' + EPOCH_ID + ':boards']));
  const route = router.parseRoute('#/U/e/' + EPOCH_ID + '/gen/v1');
  tree.paintTree(host, m, route, (v, p) => { sink.last = { v, p }; });

  assert(host.textContent.includes('Environment'), 'Environment root');
  assert(host.textContent.includes('Generations'), 'Generations group');
  assert(host.textContent.includes('Boards'), 'Boards group');
  assert(host.textContent.includes('Mutation surface'), 'Mutation surface leaf');
  assert(host.textContent.includes('Publication'), 'Publication leaf');
  const labels = allByClass(host, 'vu-ttext').map((n) => n.textContent);
  assert(labels.includes('v0') && labels.includes('v1') && labels.includes('v2'), 'all three generations are tree nodes');
  const genLinks = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('vu-tlabel'));
  let v2Link = null;
  for (const ln of genLinks) if ((ln.textContent || '').includes('v2')) v2Link = ln;
  assert(v2Link, 'a v2 node exists');
  v2Link.dispatchEvent({ type: 'click', preventDefault() {}, stopPropagation() {} });
  assert(sink.last && sink.last.v === 'candidate' && sink.last.p.gen === 'v2', 'clicking v2 navigates to candidate v2 (multi-gen nav)');
});

// ---- candidate: promote gate (fix #1) + patch diff (fix #2) + matchups (fix #3) ----

test('candidate: stacked promote gate on the candidate page (fix #1)', async () => {
  freshState(); installFetch();
  const candidate = await import('../js/variants/U/views/candidate.js');
  const host = document.createElement('div');
  await candidate.render(host, CTX(), router.parseRoute('#/U/e/' + EPOCH_ID + '/gen/v1'));
  const gate = allByClass(host, 'dn-gate')[0];
  assert(gate, 'a promote-gate panel rendered ON the candidate page');
  const rules = allByClass(host, 'dn-rule');
  assert(rules.length >= 3, 'each gate rule is its own row (3 short-circuiting rules)');
  assert(allByClass(host, 'dn-sc-table').length >= 1, 'a separate champion-vs-challenger scalar-components block');
  assert(host.textContent.includes('Scalar margin'), 'a rule label present');
});

test('candidate patch node → this candidate’s side-by-side diff, REAL strings (fix #2)', async () => {
  freshState(); installFetch();
  const candidate = await import('../js/variants/U/views/candidate.js');
  const host = document.createElement('div');
  await candidate.render(host, CTX(), router.parseRoute('#/U/e/' + EPOCH_ID + '/gen/v1/patch'));
  const sxs = allByClass(host, 'dn-sxs')[0];
  assert(sxs, 'the side-by-side diff filled when the patch node is open');
  assert(host.textContent.includes('Always emit an explicit slide structure'), 'challenger new_content on the right');
  assert(host.textContent.includes('Draft an outline'), 'baseline.content on the left');
  assert(!host.textContent.includes('[object Object]'), 'never the baseline OBJECT');
});

test('candidate: ALL match-ups for the candidate (fix #3 — v0 shows ≥2)', async () => {
  freshState(); installFetch();
  const candidate = await import('../js/variants/U/views/candidate.js');
  const host = document.createElement('div');
  await candidate.render(host, CTX(), router.parseRoute('#/U/e/' + EPOCH_ID + '/gen/v0'));
  assert(host.textContent.includes('v0') && host.textContent.includes('v1') && host.textContent.includes('v2'), 'all candidates referenced');
  assert(host.textContent.split('v1').length - 1 >= 1 && host.textContent.split('v2').length - 1 >= 1, 'both v0-vs-v1 and v0-vs-v2 present');
});

// ---- candidate: COMPARE mode splits the detail into two candidates (R6) --

test('candidate: a "compare" target splits the detail into two candidates', async () => {
  freshState(); installFetch();
  const candidate = await import('../js/variants/U/views/candidate.js');
  const host = document.createElement('div');
  await candidate.render(host, CTX(), router.parseRoute('#/U/e/' + EPOCH_ID + '/gen/v1~cmp=v2'));
  const sides = allByClass(host, 'vu-split-side');
  assert(sides.length === 2, 'two comparison sides');
  const tags = allByClass(host, 'vu-split-tag').map((n) => n.textContent);
  assert(tags.includes('A') && tags.includes('B'), 'A and B side tags');
  assert(host.textContent.includes('v1') && host.textContent.includes('v2'), 'both compared candidates rendered');
  assert(allByClass(host, 'vu-cmp-select').length >= 1, 'a compare-with picker');
});

// ---- board: first-class + INLINE side-by-side transcripts (fix #4 / #5) ---

test('board view: two candidates’ transcripts side by side INLINE on run select (fix #5)', async () => {
  freshState(); installFetch();
  const board = await import('../js/variants/U/views/board.js');
  const host = document.createElement('div');
  const sink = {};
  await board.render(host, CTX(sink), router.parseRoute('#/U/e/' + EPOCH_ID + '/board/waffles_single~runs=v0,v1'));
  assert(host.textContent.includes('Board · waffles_single'), 'the per-board heading (first-class board view)');
  const scrollers = allByClass(host, 'vu-transcript');
  assert(scrollers.length === 2, 'two inline, independently-scrollable transcript columns');
  assert(host.textContent.includes('Champion draft with a clear structure'), 'side A (v0 champion) transcript');
  assert(host.textContent.includes('Challenger drafting an outline now'), 'side B (v1 challenger) transcript');
  assert(host.textContent.includes('omitted the requested structure'), 'a drift annotation rendered inline');
  const setRun = allByClass(host, 'dn-board-run')[0];
  assert(setRun, 'a transcript-select control exists');
  setRun.dispatchEvent({ type: 'click', preventDefault() {} });
  assert(sink.last && sink.last.v === 'board', 'selecting a run stays on the board view (no separate run page)');
  assert(sink.last && sink.last.o && Array.isArray(sink.last.o.runs), 'it sets the runs comparison target, not a run route');
});

// ---- FIXED back/up button — renders into the MAIN pane, rail unchanged (R6) ---

test('back button: renders the destination into the MAIN detail pane, rail unchanged', async () => {
  freshState(); installFetch();
  const root = document.createElement('div');
  // deep-link into a candidate so there IS somewhere to go up to (→ epoch).
  window.localStorage.setItem('zicato.U.tree.open', JSON.stringify(['e:' + EPOCH_ID, 'e:' + EPOCH_ID + ':gens']));
  globalThis.window.location.hash = '#/U/e/' + EPOCH_ID + '/gen/v1';
  shell.mountShell(root);
  // let the initial async dispatch settle.
  await new Promise((r) => setTimeout(r, 0));

  const railHost = root.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('vu-sidebar'))[0];
  const detailHost = root.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('vu-detail'))[0];
  assert(railHost && detailHost, 'shell mounted with a rail host and a detail host');
  // the rail has the tree; the detail has the candidate.
  assert(railHost.textContent.includes('Environment'), 'rail holds the data-model tree');
  assert(detailHost.textContent.includes('Candidate v1'), 'detail holds the candidate v1 view');

  // ACT: go up one level. Must render the EPOCH into the MAIN detail pane —
  // NEVER the sidebar (Q's bug).
  const moved = shell.goBack();
  await new Promise((r) => setTimeout(r, 0));
  assert(moved, 'goBack navigated (there was a parent)');

  // the hash walked up to the epoch.
  assertEqual(router.parseRoute(window.location.hash).view, 'epoch', 'back went UP to the epoch');
  // the DESTINATION rendered into the MAIN detail pane.
  assert(detailHost.textContent.includes('Epoch ' + EPOCH_ID), 'the destination (epoch overview) is in the MAIN detail pane');
  assert(!detailHost.textContent.includes('Candidate v1'), 'the candidate view was replaced in the detail pane');
  // the RAIL still holds the data-model TREE — the back action did NOT paint the
  // destination VIEW into the sidebar (the explicit Q-bug guard). The tree may
  // legitimately re-highlight (its selection moved to the epoch), but it remains
  // the tree: the epoch OVERVIEW (its lede / objective / heatmap) never leaks in.
  assert(railHost.textContent.includes('Environment'), 'rail still holds the tree (Environment root present)');
  assert(railHost.textContent.includes('Generations') && railHost.textContent.includes('Boards'), 'rail still holds the tree groups');
  assert(railHost.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('vu-tree')).length >= 1, 'rail host holds a vu-tree, not a detail view');
  assert(railHost.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('dn-heatmap')).length === 0, 'the epoch overview heatmap did NOT leak into the rail (Q-bug guard)');
  assert(railHost.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('dn-pagehead')).length === 0, 'no detail page-head rendered into the rail');
});

// ---- FIX #6: trellis in the Boards view, NOT the epoch overview ----

test('trellis lives in the Boards view; the epoch overview has the heatmap, no trellis (fix #6)', async () => {
  freshState(); installFetch();
  const epoch = await import('../js/variants/U/views/epoch.js');
  const board = await import('../js/variants/U/views/board.js');
  const ehost = document.createElement('div');
  await epoch.render(ehost, CTX(), router.parseRoute('#/U/e/' + EPOCH_ID));
  assert(allByClass(ehost, 'dn-heatmap')[0], 'epoch overview HAS the heatmap');
  assert(allByClass(ehost, 'dn-trellis').length === 0, 'epoch overview has NO trellis (de-dup)');

  const bhost = document.createElement('div');
  await board.render(bhost, CTX(), router.parseRoute('#/U/e/' + EPOCH_ID + '/board/waffles_single'));
  assert(allByClass(bhost, 'dn-trellis')[0], 'the trellis lives in the Boards view');
  assert(allByClass(bhost, 'dn-heatmap').length === 0, 'the board view has no heatmap');
});

test('epoch heatmap cell routes to the per-board view (by entry id)', async () => {
  freshState(); installFetch();
  const epoch = await import('../js/variants/U/views/epoch.js');
  const host = document.createElement('div');
  const sink = {};
  await epoch.render(host, CTX(sink), router.parseRoute('#/U/e/' + EPOCH_ID));
  const heat = allByClass(host, 'dn-heatmap')[0];
  const cell = heat.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('dn-hm-cell') && !(n.getAttribute('class') || '').includes('dn-hm-empty'))[0];
  assert(cell, 'a valued heatmap cell exists');
  cell.dispatchEvent({ type: 'click' });
  assert(sink.last && sink.last.v === 'board' && sink.last.p.entry, 'heatmap cell routes to the per-board view keyed by entry id');
});

// ---- side-by-side diff primitive (REAL strings) --------------------

test('sideBySideDiff: two columns of real strings (NOT "[object Object]")', () => {
  const mark = svg.sideBySideDiff({
    baseline: 'You are the coordinator.\nDraft an outline.',
    challenger: 'You are the coordinator.\nAlways emit an explicit slide structure.',
    leftLabel: 'champion baseline', rightLabel: 'challenger new',
  });
  assert(mark.textContent.includes('Always emit an explicit slide structure'), 'the new content rendered');
  assert(mark.textContent.includes('Draft an outline'), 'the baseline rendered');
  assert(!mark.textContent.includes('[object Object]'), 'strings, not objects');
  assertEqual(allByClass(mark, 'dn-sxs-col-h').length, 2, 'two side-by-side columns');
});

// ---- pickers + pills + theme defaults (light + Sans) ---------------

test('pickers + pills: solarized-light + Sans defaults switch + persist', () => {
  freshState();
  const root = document.createElement('div');
  assertEqual(ui.DEFAULT_COLOR, 'solarized-light', 'solarized-light is the default colour theme');
  assertEqual(ui.DEFAULT_TYPE, 'sans', 'Sans is the default typeface');
  const typeIds = ui.TYPE_THEMES.map((t) => t[0]);
  assert(['sans', 'editorial', 'technical', 'display'].every((t) => typeIds.includes(t)), 'all four typefaces offered');
  const colorIds = ui.COLOR_THEMES.map((t) => t[0]);
  assert(['solarized-light', 'solarized-dark', 'monokai'].every((t) => colorIds.includes(t)), 'all three colour themes offered');
  shell.applyTheme('monokai', root);
  assertEqual(root.getAttribute('data-u-theme'), 'monokai', 'colour applied to root');
  assertEqual(ui.readColor(), 'monokai', 'colour persisted');
  shell.applyTypeface('technical', root);
  assertEqual(root.getAttribute('data-u-type'), 'technical', 'typeface applied to root');
  assertEqual(ui.readType(), 'technical', 'typeface persisted');
  assertEqual(ui.normaliseColor('nonsense'), 'solarized-light', 'unknown colour → solarized-light');
  assertEqual(ui.normaliseType('nonsense'), 'sans', 'unknown typeface → sans');
  const pill = ui.verdictPill('rejected');
  assert((pill.getAttribute('class') || '').includes('dn-rejected'), 'verdict pill carries its decision class');
});

// ---- digest-gated repaint (no-op) ----------------------------------

test('candidate view: digest-gated — identical data does NOT rebuild the DOM', async () => {
  freshState(); installFetch();
  const candidate = await import('../js/variants/U/views/candidate.js');
  const host = document.createElement('div');
  await candidate.render(host, CTX(), router.parseRoute('#/U/e/' + EPOCH_ID + '/gen/v1'));
  const digest1 = host.getAttribute('data-u-digest');
  const first = host.firstChild;
  const writes1 = host.innerHTMLWriteCount();
  assert(host.children.length > 0, 'candidate painted');
  await candidate.render(host, CTX(), router.parseRoute('#/U/e/' + EPOCH_ID + '/gen/v1'));
  assertEqual(host.getAttribute('data-u-digest'), digest1, 'digest unchanged');
  assert(host.firstChild === first, 'no clear-and-rebuild on the no-op repaint');
  assertEqual(host.innerHTMLWriteCount(), writes1, 'no innerHTML writes on the no-op repaint');
});

// ---- cold deep-link hydration --------------------------------------

test('cold deep-link: a board runs= deep-link hydrates both transcripts', async () => {
  freshState(); installFetch();
  const board = await import('../js/variants/U/views/board.js');
  const host = document.createElement('div');
  await board.render(host, CTX(), router.parseRoute('#/U/e/' + EPOCH_ID + '/board/waffles_single~runs=v0,v1'));
  assert(allByClass(host, 'vu-transcript').length === 2, 'both transcript columns hydrate from a cold deep-link');
});

await run();
