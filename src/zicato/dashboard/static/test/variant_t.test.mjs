// test/variant_t.test.mjs — Variant T ("Console IV") unit tests.
//
// T is the round-6 convergence-IV ANCHOR: the P console (data-model TREE
// sidebar, Monokai + Technical) folded with S's first-class side-by-side
// COMPARE, Q's generous spacing, and a FIXED back/up button. These pin every
// carried-forward round-5 capability PLUS the three round-6 folds:
//   * the tree sidebar renders Environment → Epoch → {Generations, Boards,
//     Mutation surface, Publication}; multi-generation nav works;
//   * the promote gate is on the candidate page (stacked, no overlap);
//   * the patch node click opens the per-candidate SIDE-BY-SIDE diff with REAL
//     strings (not "[object Object]");
//   * a candidate shows ALL its match-ups (v0 → ≥2);
//   * the board view is reachable + selecting a run shows the transcript INLINE
//     side by side (no route change to a separate run page);
//   * the trellis is in the Boards view, NOT the epoch overview;
//   * NEW — the "compare with…" affordance SPLITS the detail into TWO
//     candidates side by side;
//   * NEW — the back button navigates UP the hierarchy and renders the
//     destination into the MAIN DETAIL PANE (rail host unchanged);
//   * pickers (monokai + Technical defaults) switch + persist; digest no-op.

import { installDom, test, run, assert, assertEqual } from './harness.mjs';

installDom();

const router = await import('../js/variants/T/router.js');
const svg = await import('../js/variants/T/svg.js');
const ui = await import('../js/variants/T/ui.js');
const shell = await import('../js/variants/T/shell.js');
const data = await import('../js/variants/T/data.js');
const tree = await import('../js/variants/T/tree.js');
const compare = await import('../js/variants/T/compare.js');

const EPOCH_ID = '2026-05-30_e0';

const FIXTURE = {
  '/api/epoch': {
    epoch_id: EPOCH_ID, closed: false, goal: 'Make the presentation agent crisper.',
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
    { champion: 'v0', challenger: 'v1', decision: 'rejected', delta_scalar: 75.71, hypothesis_core_idea: 'Enforce explicit slide-structure output.' },
    { champion: 'v0', challenger: 'v2', decision: 'rejected', delta_scalar: 1.51, hypothesis_core_idea: 'Tighten coordinator oversight.' },
  ] },
  '/api/score-trajectory': { points: [{ generation_id: 'v0', scalar: 70.94 }, { generation_id: 'v1', scalar: 146.65 }, { generation_id: 'v2', scalar: 72.45 }] },
  '/api/workspace': { current_epoch_id: EPOCH_ID, epochs: [{ epoch_id: EPOCH_ID, generation_count: 3, promoted_count: 1, best_scalar: 70.94, closed: false, goal: 'crisper' }], sparkline: [] },
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
    baseline: { generation_id: 'v0', content: 'Default oversight.', file: 'agent/policy.py', role: 'oversight policy', line_start: 1, line_end: 12 },
    versions: [],
  },
  [`/api/files/${EPOCH_ID}/v1/patches`]: { patches: [
    { id: 'p1', mutation_id: 'coordinator_prompt', op: 'edit', new_content: 'You are the coordinator.\nAlways emit an explicit slide structure.', rationale: 'Enforce structure.' },
    { id: 'p2', mutation_id: 'oversight_policy', op: 'edit', new_content: 'Tighten coordinator oversight.' },
  ] },
  [`/api/files/${EPOCH_ID}/v2/patches`]: { patches: [
    { id: 'p3', mutation_id: 'oversight_policy', op: 'edit', new_content: 'Loosen coordinator oversight.' },
  ] },
  [`/api/files/${EPOCH_ID}/v0/patches`]: { patches: [] },
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
FIXTURE['/api/conversation/run_v1_waffles'] = {
  turns: [
    { seq: 0, role: 'user', agent: 'operator', text: 'Make a presentation about waffles.' },
    { seq: 1, role: 'agent', agent: 'coordinator', text: 'Drafting an outline now.', tool_calls: [{ name: 'write_slide' }] },
  ],
  annotations: [{ anchor_seq: 1, kind: 'drift', summary: 'omitted the requested structure' }],
};
FIXTURE['/api/conversation/run_v0_waffles'] = {
  turns: [
    { seq: 0, role: 'user', agent: 'operator', text: 'Make a presentation about waffles.' },
    { seq: 1, role: 'agent', agent: 'coordinator', text: 'Here is a structured outline.', tool_calls: [] },
  ],
  annotations: [],
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
  globalThis.window.location = { hash: '', search: '' };
}
function allByClass(host, cls) {
  return host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').split(/\s+/).includes(cls));
}

// ---- router: hierarchical path + the compare (cmp) target ----------

test('router: hierarchical views parse with the epoch + the compare target', () => {
  assertEqual(router.parseRoute('').view, 'home');
  assertEqual(router.parseRoute('#/T/').view, 'home');
  assertEqual(router.parseRoute('#/bogus').view, 'home');
  const ep = router.parseRoute(`#/T/e/${EPOCH_ID}`);
  assertEqual(ep.view, 'epoch'); assertEqual(ep.params.epochId, EPOCH_ID);
  const cand = router.parseRoute(`#/T/e/${EPOCH_ID}/gen/v1`);
  assertEqual(cand.view, 'candidate'); assertEqual(cand.params.gen, 'v1');
  // the side-by-side compare target rides as a ~cmp= suffix and deep-links.
  const cmp = router.parseRoute(`#/T/e/${EPOCH_ID}/gen/v1~cmp=v2`);
  assertEqual(cmp.view, 'candidate'); assertEqual(cmp.params.gen, 'v1'); assertEqual(cmp.cmp, 'v2');
  assertEqual(router.href('candidate', { epochId: EPOCH_ID, gen: 'v1' }, { cmp: 'v2' }), `#/T/e/${EPOCH_ID}/gen/v1~cmp=v2`);
  const brd = router.parseRoute(`#/T/e/${EPOCH_ID}/board/waffles_single/v1`);
  assertEqual(brd.view, 'board'); assertEqual(brd.params.entry, 'waffles_single'); assertEqual(brd.params.gen, 'v1');
  assertEqual(router.parseRoute(`#/T/e/${EPOCH_ID}/mutations/coordinator_prompt`).params.mutId, 'coordinator_prompt');
  assertEqual(router.parseRoute(`#/T/e/${EPOCH_ID}/paper`).view, 'publication');
  assertEqual(router.parseRoute(`#/T/e/${EPOCH_ID}/boards`).view, 'boards');
});

// ---- router.up(): the back/up destination -------------------------

test('router.up: navigates UP the selection hierarchy (incl. collapsing a compare split)', () => {
  assertEqual(router.up(router.parseRoute('#/T/')), null, 'environment has no parent');
  assertEqual(router.up(router.parseRoute(`#/T/e/${EPOCH_ID}`)).view, 'home', 'epoch → environment');
  assertEqual(router.up(router.parseRoute(`#/T/e/${EPOCH_ID}/gens`)).view, 'epoch', 'gens → epoch');
  assertEqual(router.up(router.parseRoute(`#/T/e/${EPOCH_ID}/gen/v1`)).view, 'gens', 'candidate → generations');
  // a compare split collapses to the bare candidate FIRST (it is a deeper state).
  const upFromCmp = router.up(router.parseRoute(`#/T/e/${EPOCH_ID}/gen/v1~cmp=v2`));
  assertEqual(upFromCmp.view, 'candidate'); assert(!upFromCmp.cmp, 'back clears the comparison first');
  assertEqual(router.up(router.parseRoute(`#/T/e/${EPOCH_ID}/board/waffles_single/v1`)).view, 'board', 'board+gen → bare board');
});

// ---- HEADLINE: the data-model TREE sidebar -------------------------

test('tree sidebar: renders Environment → Epoch → {Generations, Boards, Mutation surface, Publication}', () => {
  const host = document.createElement('div');
  const model = {
    epochs: [{ id: EPOCH_ID, current: true }],
    byEpoch: { [EPOCH_ID]: {
      gens: [{ id: 'v0', promoted: true, parent: null }, { id: 'v1', promoted: false, parent: 'v0' }, { id: 'v2', promoted: false, parent: 'v0' }],
      boards: [{ id: 'waffles_single' }, { id: 'picky_stakeholder_emulated' }],
    } },
  };
  const toggles = new Set(['e:' + EPOCH_ID, 'e:' + EPOCH_ID + '/gens', 'e:' + EPOCH_ID + '/boards']);
  const route = router.parseRoute(`#/T/e/${EPOCH_ID}`);
  const ctx = { navigate() {}, href: router.href };
  tree.buildTree(host, model, route, toggles, ctx, () => {});

  assert(allByClass(host, 'dt-tree')[0], 'the tree root rendered');
  const txt = host.textContent;
  assert(txt.includes('Environment'), 'Environment root present');
  assert(txt.includes(EPOCH_ID), 'the epoch node present');
  assert(txt.includes('Generations'), 'Generations group present');
  assert(txt.includes('Boards'), 'Boards group present');
  assert(txt.includes('Mutation surface'), 'Mutation surface node present');
  assert(txt.includes('Publication'), 'Publication node present');
  assert(txt.includes('v0') && txt.includes('v1') && txt.includes('v2'), 'every generation is a tree leaf');
  assert(txt.includes('waffles_single') && txt.includes('picky_stakeholder_emulated'), 'every board entry is a tree leaf');
  assert(allByClass(host, 'dt-glyph-gen-champ').length >= 1, 'the champion generation carries a champion glyph');
});

// ---- multi-candidate navigation ------------------------------------

test('candidate view: navigating to a SECOND generation works (multi-candidate nav)', async () => {
  freshState(); installFetch();
  const candidate = await import('../js/variants/T/views/candidate.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await candidate.render(host, ctx, { epochId: EPOCH_ID, gen: 'v1' });
  assert(host.textContent.includes('Candidate v1'), 'v1 rendered');
  await candidate.render(host, ctx, { epochId: EPOCH_ID, gen: 'v2' });
  assert(host.textContent.includes('Candidate v2'), 'v2 rendered after switching');
  assert(!host.textContent.includes('Candidate v1'), 'the previous candidate was replaced (digest changed)');
});

// ---- FIX #1: promote gate ON the candidate page --------------------

test('candidate view: the promote gate is ON the candidate page, stacked, no overlap', async () => {
  freshState(); installFetch();
  const candidate = await import('../js/variants/T/views/candidate.js');
  const host = document.createElement('div');
  await candidate.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, gen: 'v1' });
  const gate = allByClass(host, 'dn-gate')[0];
  assert(gate, 'a promote-gate panel rendered on the candidate page');
  const rules = allByClass(host, 'dn-rule');
  assert(rules.length >= 3, 'each gate rule is its own row (3 short-circuiting rules)');
  assert(allByClass(host, 'dn-sc-table').length >= 1, 'a separate champion-vs-challenger scalar-components block');
  assert(host.textContent.includes('Scalar margin'), 'a rule label present');
});

// ---- FIX #2: patch node → per-candidate side-by-side diff ----------

test('candidate view: the lifecycle PATCH node is clickable → the per-candidate diff route', async () => {
  freshState(); installFetch();
  const candidate = await import('../js/variants/T/views/candidate.js');
  const host = document.createElement('div');
  let navTo = null;
  const ctx = { navigate: (v, p) => { navTo = { v, p }; }, href: router.href };
  await candidate.render(host, ctx, { epochId: EPOCH_ID, gen: 'v1' });
  const patch = allByClass(host, 'ezn-clickable')[0];
  assert(patch, 'the lifecycle patch node is clickable (fix #2)');
  patch.dispatchEvent({ type: 'click' });
  assert(navTo && navTo.v === 'diff' && navTo.p.gen === 'v1' && navTo.p.epochId === EPOCH_ID, 'patch click routes to this candidate’s diff');
});

test('diff view: the per-candidate side-by-side diff renders REAL strings (not "[object Object]")', async () => {
  freshState(); installFetch();
  const diff = await import('../js/variants/T/views/diff.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await diff.render(host, ctx, { epochId: EPOCH_ID, gen: 'v1' });
  assert(host.textContent.includes('Patch diff · v1'), 'the per-candidate diff heading');
  const sxs = allByClass(host, 'dn-sxs')[0];
  assert(sxs, 'a side-by-side diff component rendered (reused from the mutation viewer)');
  assert(host.textContent.includes('Draft an outline'), 'baseline.content (LEFT) — the real STRING');
  assert(host.textContent.includes('Always emit an explicit slide structure'), 'challenger new_content (RIGHT) — the real STRING');
  assert(!host.textContent.includes('[object Object]'), 'never the baseline OBJECT');
});

// ---- FIX #3: ALL match-ups for a candidate -------------------------

test('candidate view: v0 shows ALL its match-ups (v0→v1 AND v0→v2), not just one', async () => {
  freshState(); installFetch();
  const candidate = await import('../js/variants/T/views/candidate.js');
  const host = document.createElement('div');
  await candidate.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, gen: 'v0' });
  const txt = host.textContent;
  assert(txt.includes('v0 → v1'), 'the v0→v1 round shown');
  assert(txt.includes('v0 → v2'), 'the v0→v2 round shown');
});

// ---- NEW (round 6): side-by-side COMPARE splits the detail ---------

test('candidate view: "compare with…" SPLITS the detail into TWO candidates side by side', async () => {
  freshState(); installFetch();
  const candidate = await import('../js/variants/T/views/candidate.js');

  // single candidate first — the compare affordance is present, no split yet.
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await candidate.render(host, ctx, { epochId: EPOCH_ID, gen: 'v1' });
  assert(allByClass(host, 'dt-cmp-picker')[0], 'the "compare with…" picker is present');
  assert(allByClass(host, 'dt-split-single')[0], 'no compare target → the frame is single-column');

  // now pass the compare target — the detail splits into two candidate panels.
  freshState(); installFetch();
  const host2 = document.createElement('div');
  await candidate.render(host2, ctx, { epochId: EPOCH_ID, gen: 'v1' }, { params: { epochId: EPOCH_ID, gen: 'v1' }, cmp: 'v2' });
  assert(host2.textContent.includes('Candidate v1') && host2.textContent.includes('vs  v2'), 'the page head names both candidates');
  const split = allByClass(host2, 'dt-split')[0];
  assert(split && !(split.getAttribute('class') || '').includes('dt-split-single'), 'the frame is a two-column split');
  const sides = allByClass(host2, 'dt-split-side');
  assert(sides.length === 2, 'TWO candidate panels (A and B) side by side');
  // each side carries its own lifecycle + gate (S's comparison-first detail).
  assert(allByClass(host2, 'dn-gate').length >= 1, 'a promote gate appears within the split');
});

// ---- FIX #4 + #5: board reachable from tree; INLINE side-by-side transcript ----

test('board view: reachable from the tree and selecting a run shows the transcript INLINE side by side', async () => {
  freshState(); installFetch();
  const host = document.createElement('div');
  const model = { epochs: [{ id: EPOCH_ID, current: true }], byEpoch: { [EPOCH_ID]: { gens: [{ id: 'v0', promoted: true, parent: null }], boards: [{ id: 'waffles_single' }] } } };
  let navTo = null;
  const treeCtx = { navigate: (v, p) => { navTo = { v, p }; }, href: router.href };
  const toggles = new Set(['e:' + EPOCH_ID, 'e:' + EPOCH_ID + '/boards']);
  tree.buildTree(host, model, router.parseRoute(`#/T/e/${EPOCH_ID}/boards`), toggles, treeCtx, () => {});
  const boardLeaf = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('dt-leaf') && n.getAttribute('data-kind') === 'board')[0];
  assert(boardLeaf, 'a Boards leaf exists in the tree');
  const leafBtn = boardLeaf.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('dt-label'))[0];
  leafBtn.dispatchEvent({ type: 'click' });
  assert(navTo && navTo.v === 'board' && navTo.p.entry === 'waffles_single', 'the tree Boards leaf routes to the per-board view by entry id');

  freshState(); installFetch();
  const board = await import('../js/variants/T/views/board.js');
  const bhost = document.createElement('div');
  await board.render(bhost, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, entry: 'waffles_single', gen: 'v1' });
  assert(bhost.textContent.includes('Board · waffles_single'), 'the per-board heading (still the board view)');
  const xgrid = allByClass(bhost, 'dn-xscript-grid')[0];
  assert(xgrid, 'the INLINE side-by-side transcript pane rendered within the board view');
  const cols = allByClass(bhost, 'dn-xscript-col');
  assert(cols.length === 2, 'two candidates’ transcripts side by side');
  assert(bhost.textContent.includes('Drafting an outline'), 'the selected run’s transcript turn rendered INLINE (no route away)');
});

test('board view: a candidate row links INLINE (to board+gen), never to a separate run page', async () => {
  freshState(); installFetch();
  const board = await import('../js/variants/T/views/board.js');
  const host = document.createElement('div');
  await board.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, entry: 'waffles_single' });
  const runLink = allByClass(host, 'dn-board-run')[0];
  assert(runLink, 'a per-candidate transcript link exists');
  const href = runLink.getAttribute('href') || '';
  assert(href.includes('/board/'), 'the link stays on the board view (inline), not a /run/ page');
  assert(!href.includes('/run/'), 'no navigation to a separate run page');
});

// ---- FIX #6: trellis in the Boards view, NOT the epoch overview ----

test('de-dup: the trellis lives in the Boards view; the epoch overview has the heatmap only', async () => {
  freshState(); installFetch();
  const epoch = await import('../js/variants/T/views/epoch.js');
  const boards = await import('../js/variants/T/views/boards.js');
  const ctx = { navigate() {}, href: router.href };

  const ehost = document.createElement('div');
  await epoch.render(ehost, ctx, { epochId: EPOCH_ID });
  assert(allByClass(ehost, 'dn-heatmap')[0], 'the epoch overview keeps the heatmap');
  assert(allByClass(ehost, 'dn-trellis').length === 0, 'the epoch overview has NO trellis (moved to Boards)');

  const bhost = document.createElement('div');
  await boards.render(bhost, ctx, { epochId: EPOCH_ID });
  assert(allByClass(bhost, 'dn-trellis')[0], 'the Boards view carries the trellis (small-multiples)');
  assert(allByClass(bhost, 'dn-heatmap').length === 0, 'the Boards view has NO heatmap (never both on one page)');
});

// ---- NEW (round 6): the FIXED back button renders into the MAIN pane ----

test('back button: navigates UP and renders the destination into the MAIN detail pane (rail unchanged)', async () => {
  freshState(); installFetch();
  // The shell uses the bare globals `location` / `window` / `HashChangeEvent`
  // (browser globals). Wire a live `location` whose hash setter re-fires the
  // registered hashchange listeners, so driving the back control behaves as in
  // a browser. This is test-harness plumbing only — the shell code is unchanged.
  const listeners = { hashchange: [] };
  globalThis.HashChangeEvent = function HashChangeEvent() {};
  // a no-op EventSource so connectSSE() does not enter an endless reconnect
  // loop (which would keep the node event loop alive); harness plumbing only.
  globalThis.EventSource = function EventSource() { this.readyState = 0; this.addEventListener = () => {}; this.close = () => {}; };
  globalThis.EventSource.CLOSED = 2;
  globalThis.window = globalThis.window || {};
  globalThis.window.localStorage = globalThis.window.localStorage || { getItem() { return null; }, setItem() {} };
  globalThis.window.addEventListener = (t, fn) => { (listeners[t] = listeners[t] || []).push(fn); };
  const loc = { _hash: '' };
  Object.defineProperty(loc, 'hash', {
    get() { return this._hash; },
    set(v) { this._hash = v; for (const fn of (listeners.hashchange || [])) fn(); },
  });
  globalThis.location = loc;
  globalThis.window.location = loc;
  globalThis.window.dispatchEvent = () => { for (const fn of (listeners.hashchange || [])) fn(); };

  // mount the real shell so the back button + the rail/detail hosts exist.
  const root = document.createElement('div');
  document.body.appendChild(root);
  loc._hash = `#/T/e/${EPOCH_ID}/gen/v1`;
  shell.mountShell(root);
  // let the async dispatch settle.
  await new Promise((r) => setTimeout(r, 0));
  await new Promise((r) => setTimeout(r, 0));

  const rail = allByClass(root, 'dt-sidebar')[0];
  const detail = allByClass(root, 'dt-viewhost')[0];
  const backBtn = allByClass(root, 'dt-back')[0];
  assert(rail && detail && backBtn, 'the shell painted a rail, a detail pane, and a back button');
  const railBefore = rail.innerHTML !== undefined ? rail.textContent : '';
  assert(detail.textContent.includes('Candidate v1'), 'the detail pane starts on the candidate (v1)');

  // drive the back control: candidate → generations.
  shell.goBack(router.parseRoute(location.hash));
  await new Promise((r) => setTimeout(r, 0));
  await new Promise((r) => setTimeout(r, 0));

  assertEqual(location.hash, `#/T/e/${EPOCH_ID}/gens`, 'back navigated UP to the generations group');
  // THE FIX: the destination view lands in the MAIN DETAIL pane, NOT the rail.
  assert(detail.textContent.toLowerCase().includes('generation'), 'the destination view rendered into the MAIN detail pane');
  // the rail host still holds the tree (it was not used as the back destination).
  assert(allByClass(rail, 'dt-tree')[0], 'the rail host is unchanged — still the navigation tree, not the destination view');
  assert(railBefore !== undefined, 'rail content captured');
});

// ---- pickers + digest no-op ----------------------------------------

test('pickers: typeface (Technical default) + colour (monokai default) switch + persist', () => {
  freshState();
  const root = document.createElement('div');
  assertEqual(ui.DEFAULT_COLOR, 'monokai', 'monokai is the default colour theme');
  assertEqual(ui.DEFAULT_TYPE, 'technical', 'Technical is the default typeface');
  const typeIds = ui.TYPE_THEMES.map((t) => t[0]);
  assert(['sans', 'editorial', 'technical', 'display'].every((t) => typeIds.includes(t)), 'all four typefaces offered');
  const colorIds = ui.COLOR_THEMES.map((t) => t[0]);
  assert(['monokai', 'solarized-dark', 'solarized-light'].every((c) => colorIds.includes(c)), 'all three colour themes offered');
  shell.applyTheme('solarized-dark', root);
  assertEqual(root.getAttribute('data-t-theme'), 'solarized-dark', 'colour applied to the T root');
  assertEqual(ui.readColor(), 'solarized-dark', 'colour persisted');
  shell.applyTypeface('editorial', root);
  assertEqual(root.getAttribute('data-t-type'), 'editorial', 'typeface applied to the T root');
  assertEqual(ui.readType(), 'editorial', 'typeface persisted');
  assertEqual(ui.normaliseColor('nonsense'), 'monokai', 'unknown colour → monokai');
  assertEqual(ui.normaliseType('nonsense'), 'technical', 'unknown typeface → technical');
});

test('compare primitives: comparePicker reflects the value; splitFrame yields two sides only when B is given', () => {
  let chosen = '__unset__';
  const picker = compare.comparePicker({
    label: 'compare with…', current: 'v1', value: 'v2',
    options: [{ id: 'v0' }, { id: 'v1' }, { id: 'v2' }],
    onChange: (v) => { chosen = v; },
  });
  const sel = picker.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('dt-cmp-select'))[0];
  assert(sel, 'a select rendered');
  assertEqual(sel.value, 'v2', 'the picker reflects the current compare value');

  const single = compare.splitFrame({ a: { title: 'A', build() {} } });
  assert((single.getAttribute('class') || '').includes('dt-split-single'), 'no B → single column');
  const dual = compare.splitFrame({ a: { title: 'A', build() {} }, b: { title: 'B', build() {} } });
  assert(!(dual.getAttribute('class') || '').includes('dt-split-single'), 'B given → two columns');
});

test('candidate view: digest-gated — identical data does NOT rebuild the DOM (heartbeat no-op)', async () => {
  freshState(); installFetch();
  const candidate = await import('../js/variants/T/views/candidate.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await candidate.render(host, ctx, { epochId: EPOCH_ID, gen: 'v1' });
  const digest1 = host.getAttribute('data-t-digest');
  const first = host.firstChild;
  const writes1 = host.innerHTMLWriteCount();
  assert(host.children.length > 0, 'candidate painted');
  await candidate.render(host, ctx, { epochId: EPOCH_ID, gen: 'v1' });
  assertEqual(host.getAttribute('data-t-digest'), digest1, 'digest unchanged on the no-op repaint');
  assert(host.firstChild === first, 'no clear-and-rebuild on the no-op repaint');
  assertEqual(host.innerHTMLWriteCount(), writes1, 'no innerHTML writes on the no-op repaint');
});

test('tree sidebar: digest-gated — same model + route + toggles yields the same digest (heartbeat no-op)', () => {
  const model = { epochs: [{ id: EPOCH_ID, current: true }], byEpoch: { [EPOCH_ID]: { gens: [{ id: 'v0', promoted: true, parent: null }], boards: [{ id: 'waffles_single' }] } } };
  const route = router.parseRoute(`#/T/e/${EPOCH_ID}/gen/v0`);
  const toggles = new Set();
  const d1 = tree.treeDigest(model, route, toggles);
  const d2 = tree.treeDigest(model, route, toggles);
  assertEqual(d1, d2, 'a steady heartbeat (identical model/route) is a true digest no-op');
});

await run();
