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

import { installDom, test, run, assert, assertEqual, assertDeep } from './harness.mjs';

installDom();

const router = await import('../js/variants/T/router.js');
const svg = await import('../js/variants/T/svg.js');
const ui = await import('../js/variants/T/ui.js');
const shell = await import('../js/variants/T/shell.js');
const data = await import('../js/variants/T/data.js');
const tree = await import('../js/variants/T/tree.js');
const compare = await import('../js/variants/T/compare.js');
const livestatus = await import('../js/variants/T/livestatus.js');
const coreState = await import('../js/core/state.js');

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
// read the scoped stylesheet text (for CSS-contract assertions).
async function readCssAsync() {
  const fs = await import('node:fs');
  return fs.readFileSync(new URL('../css/variants/T/console4.css', import.meta.url), 'utf8');
}
const _cssCache = await readCssAsync();
function readCss() { return _cssCache; }

// ---- router: hierarchical path + the compare (cmp) target ----------

test('router: hierarchical views parse with the epoch + the compare target', () => {
  // CHANGE 1: the vestigial `/T` hash-route prefix is dropped — routes are bare `#/`.
  assertEqual(router.PREFIX, '#', 'the route prefix is bare `#` (no `/T`)');
  assert(!router.href('home', {}).includes('/T'), 'a home href carries no `/T` prefix');
  assert(!router.href('epoch', { epochId: EPOCH_ID }).includes('/T'), 'an epoch href carries no `/T` prefix');
  assertEqual(router.href('home', {}), '#/', 'home is the bare `#/` route');
  assertEqual(router.href('epoch', { epochId: EPOCH_ID }), `#/e/${EPOCH_ID}`, 'epoch href round-trips under the bare prefix');
  // an old `#/T/...` link no longer resolves to an app view (the prefix is gone).
  assertEqual(router.parseRoute(`#/T/e/${EPOCH_ID}`).view, 'home', 'a legacy `#/T/` link falls back to home');
  assertEqual(router.parseRoute('').view, 'home');
  assertEqual(router.parseRoute('#/').view, 'home');
  assertEqual(router.parseRoute('#/bogus').view, 'home');
  const ep = router.parseRoute(`#/e/${EPOCH_ID}`);
  assertEqual(ep.view, 'epoch'); assertEqual(ep.params.epochId, EPOCH_ID);
  // a representative DEEP route parses + its href round-trips.
  const deep = router.parseRoute(`#/e/${EPOCH_ID}/gen/v1/diff/coordinator_prompt`);
  assertEqual(deep.view, 'diff'); assertEqual(deep.params.gen, 'v1'); assertEqual(deep.params.mutId, 'coordinator_prompt');
  assertEqual(router.href('diff', { epochId: EPOCH_ID, gen: 'v1', mutId: 'coordinator_prompt' }),
    `#/e/${EPOCH_ID}/gen/v1/diff/coordinator_prompt`, 'the deep diff href round-trips under the bare prefix');
  const cand = router.parseRoute(`#/e/${EPOCH_ID}/gen/v1`);
  assertEqual(cand.view, 'candidate'); assertEqual(cand.params.gen, 'v1');
  // the side-by-side compare target rides as a ~cmp= suffix and deep-links.
  const cmp = router.parseRoute(`#/e/${EPOCH_ID}/gen/v1~cmp=v2`);
  assertEqual(cmp.view, 'candidate'); assertEqual(cmp.params.gen, 'v1'); assertEqual(cmp.cmp, 'v2');
  assertEqual(router.href('candidate', { epochId: EPOCH_ID, gen: 'v1' }, { cmp: 'v2' }), `#/e/${EPOCH_ID}/gen/v1~cmp=v2`);
  const brd = router.parseRoute(`#/e/${EPOCH_ID}/board/waffles_single/v1`);
  assertEqual(brd.view, 'board'); assertEqual(brd.params.entry, 'waffles_single'); assertEqual(brd.params.gen, 'v1');
  assertEqual(router.parseRoute(`#/e/${EPOCH_ID}/mutations/coordinator_prompt`).params.mutId, 'coordinator_prompt');
  assertEqual(router.parseRoute(`#/e/${EPOCH_ID}/paper`).view, 'publication');
  assertEqual(router.parseRoute(`#/e/${EPOCH_ID}/boards`).view, 'boards');
});

// ---- router.up(): the back/up destination -------------------------

test('router.up: navigates UP the selection hierarchy (incl. collapsing a compare split)', () => {
  assertEqual(router.up(router.parseRoute('#/')), null, 'environment has no parent');
  assertEqual(router.up(router.parseRoute(`#/e/${EPOCH_ID}`)).view, 'home', 'epoch → environment');
  assertEqual(router.up(router.parseRoute(`#/e/${EPOCH_ID}/gens`)).view, 'epoch', 'gens → epoch');
  assertEqual(router.up(router.parseRoute(`#/e/${EPOCH_ID}/gen/v1`)).view, 'gens', 'candidate → generations');
  // a compare split collapses to the bare candidate FIRST (it is a deeper state).
  const upFromCmp = router.up(router.parseRoute(`#/e/${EPOCH_ID}/gen/v1~cmp=v2`));
  assertEqual(upFromCmp.view, 'candidate'); assert(!upFromCmp.cmp, 'back clears the comparison first');
  assertEqual(router.up(router.parseRoute(`#/e/${EPOCH_ID}/board/waffles_single/v1`)).view, 'board', 'board+gen → bare board');
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
  const route = router.parseRoute(`#/e/${EPOCH_ID}`);
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
  tree.buildTree(host, model, router.parseRoute(`#/e/${EPOCH_ID}/boards`), toggles, treeCtx, () => {});
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
  loc._hash = `#/e/${EPOCH_ID}/gen/v1`;
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

  assertEqual(location.hash, `#/e/${EPOCH_ID}/gens`, 'back navigated UP to the generations group');
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
  // CHANGE 3: the typeface options are EXACTLY Editorial/Technical/Display — no Sans.
  assertDeep(typeIds, ['editorial', 'technical', 'display'], 'typefaces are exactly editorial/technical/display (Sans removed)');
  assert(!typeIds.includes('sans'), 'the redundant Sans typeface is gone');
  const colorIds = ui.COLOR_THEMES.map((t) => t[0]);
  assert(['monokai', 'solarized-dark', 'solarized-light'].every((c) => colorIds.includes(c)), 'the three original colour themes are kept');
  shell.applyTheme('solarized-dark', root);
  assertEqual(root.getAttribute('data-t-theme'), 'solarized-dark', 'colour applied to the T root');
  assertEqual(ui.readColor(), 'solarized-dark', 'colour persisted');
  shell.applyTypeface('editorial', root);
  assertEqual(root.getAttribute('data-t-type'), 'editorial', 'typeface applied to the T root');
  assertEqual(ui.readType(), 'editorial', 'typeface persisted');
  assertEqual(ui.normaliseColor('nonsense'), 'monokai', 'unknown colour → monokai');
  assertEqual(ui.normaliseType('nonsense'), 'technical', 'unknown typeface → technical');
  assertEqual(ui.normaliseType('sans'), 'technical', 'the dropped Sans id falls back to Technical');
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
  const route = router.parseRoute(`#/e/${EPOCH_ID}/gen/v0`);
  const toggles = new Set();
  const d1 = tree.treeDigest(model, route, toggles);
  const d2 = tree.treeDigest(model, route, toggles);
  assertEqual(d1, d2, 'a steady heartbeat (identical model/route) is a true digest no-op');
});

// ====================================================================
// Console IV folds (round 7): the SLIM REEL on the epoch view, the
// compact MATCH CARDS on the generations page, and a DENSITY picker.
// ====================================================================

const reel = await import('../js/variants/T/reel.js');

// ---- (a) the epoch view renders the slim reel, NOT the old bumps ----

test('epoch view: renders the SLIM REEL (champion spine + round ticks), NOT the old lineage-bumps', async () => {
  freshState(); installFetch();
  const epoch = await import('../js/variants/T/views/epoch.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await epoch.render(host, ctx, { epochId: EPOCH_ID });

  assert(allByClass(host, 'tr-reel')[0], 'the slim reel rendered on the epoch view');
  assert(allByClass(host, 'tr-spine')[0], 'the reel has the champion spine');
  assert(allByClass(host, 'tr-station-seed')[0], 'the reel has the seed/champion station');
  const ticks = allByClass(host, 'tr-tick');
  assert(ticks.length >= 2, 'the reel has one round tick per challenger (≥2)');
  assert(allByClass(host, 'dn-bumps').length === 0, 'the old lineage-bumps chart is GONE (replaced by the reel)');
  // the heatmap stays on the epoch view (carried forward).
  assert(allByClass(host, 'dn-heatmap')[0], 'the board×generation heatmap is still present on the epoch view');
});

// ---- (b) the reel stays fit-to-width under a MANY-generation fixture ----

const MANY_EPOCH = '2026-05-31_many';
function installManyFetch(roundN) {
  const n = roundN || 11;                       // 11 rounds → 12 generations
  const gens = [{ generation_id: 'v0', epoch_id: MANY_EPOCH, parent_generation_id: '', promoted: true }];
  const matchups = [];
  const points = [{ generation_id: 'v0', scalar: 100 }];
  for (let i = 1; i <= n; i++) {
    const id = 'v' + i;
    gens.push({ generation_id: id, epoch_id: MANY_EPOCH, parent_generation_id: 'v0', promoted: false });
    matchups.push({ champion: 'v0', challenger: id, decision: 'rejected', delta_scalar: i * 1.5,
      ran_at: '2026-05-31T00:' + String(i).padStart(2, '0') + ':00', hypothesis_core_idea: 'Idea ' + i + '.' });
    points.push({ generation_id: id, scalar: 100 + i });
  }
  const MANY = {
    '/api/epoch': { epoch_id: MANY_EPOCH, closed: false, goal: 'Many rounds.',
      experiments: gens.map((g) => ({ generation_id: g.generation_id, parent_generation_id: g.parent_generation_id,
        outcome: { decision: g.promoted ? 'baseline' : 'rejected' } })), board: [{ id: 'b1', kind: 'single_turn' }] },
    '/api/lineage': { generations: gens },
    '/api/tournaments': { epoch_id: MANY_EPOCH, champion_lineage: ['v0'], matchups },
    '/api/score-trajectory': { points },
    '/api/workspace': { current_epoch_id: MANY_EPOCH, epochs: [{ epoch_id: MANY_EPOCH }], sparkline: [] },
    '/api/health-report': { epoch_id: MANY_EPOCH, healthy: true, findings: [] },
  };
  MANY[`/api/generation/${MANY_EPOCH}/v0/per-entry`] = { entries: [{ entry_id: 'b1', drift_loss: 50 }] };
  for (const g of gens) MANY[`/api/generation/${MANY_EPOCH}/${g.generation_id}/per-entry`] =
    MANY[`/api/generation/${MANY_EPOCH}/${g.generation_id}/per-entry`] || { entries: [{ entry_id: 'b1', drift_loss: 50 }] };
  globalThis.fetch = async (path) => (Object.prototype.hasOwnProperty.call(MANY, path)
    ? { ok: true, json: async () => MANY[path] }
    : { ok: false, status: 404, json: async () => ({ error: 'not found: ' + path }) });
}

test('reel: fit-to-width — a fixed-width viewBox; many-round ticks compress and never exceed the viewBox', () => {
  // 11 rounds → 12 stations. Build the reel directly so we can read the SVG.
  const rounds = [];
  for (let i = 1; i <= 11; i++) rounds.push({ challenger: 'v' + i, decision: 'rejected', deltaScalar: i });
  const node = reel.reel({ championId: 'v0', rounds, onSelect() {}, onSeed() {} });
  const svgs = node.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('tr-strip') && n.localName === 'svg');
  const strip = svgs[0];
  assert(strip, 'the reel SVG strip rendered');
  const vb = strip.getAttribute('viewBox');
  assertEqual(vb, '0 0 1000 92', 'the reel uses a FIXED-width viewBox (fit-to-width, no pan/zoom)');
  const VBW = 1000;

  // every positioned element (x / cx / x2) stays within the fixed viewBox width.
  const all = strip.querySelectorAll('[class]');
  let maxX = 0;
  for (const elx of all) {
    const cx = elx.getAttribute('cx'); const x = elx.getAttribute('x'); const x2 = elx.getAttribute('x2');
    for (const v of [cx, x, x2]) { if (v != null && isFinite(+v)) { assert(+v <= VBW, 'no element exceeds the viewBox width (' + v + ' ≤ ' + VBW + ')'); maxX = Math.max(maxX, +v); } }
  }
  assert(maxX > 0 && maxX <= VBW, 'positions are bounded by the fixed viewBox');

  // station spacing COMPRESSES with more rounds: 12 stations sit closer than 4.
  const xsOf = (n) => {
    const rs = [];
    for (let i = 1; i <= n; i++) rs.push({ challenger: 'v' + i, decision: 'rejected', deltaScalar: i });
    const nd = reel.reel({ championId: 'v0', rounds: rs, onSelect() {} });
    const s = nd.querySelectorAll('[class]').filter((q) => (q.getAttribute('class') || '').includes('tr-tick') && q.localName === 'circle');
    return s.map((c) => +c.getAttribute('cx')).sort((a, b) => a - b);
  };
  const few = xsOf(3); const many = xsOf(11);
  const gapFew = few[1] - few[0];
  const gapMany = many[1] - many[0];
  assert(gapMany < gapFew, 'with more rounds the tick spacing compresses (' + gapMany.toFixed(1) + ' < ' + gapFew.toFixed(1) + ')');
});

test('epoch view: the reel fits to width with ~12 generations (no overflow / collision)', async () => {
  freshState(); installManyFetch(11);
  const epoch = await import('../js/variants/T/views/epoch.js');
  const host = document.createElement('div');
  await epoch.render(host, { navigate() {}, href: router.href }, { epochId: MANY_EPOCH });
  const ticks = allByClass(host, 'tr-tick');
  assertEqual(ticks.length, 11, 'one tick per challenger round (11)');
  const strip = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('tr-strip') && n.localName === 'svg')[0];
  assertEqual(strip.getAttribute('viewBox'), '0 0 1000 92', 'still a fixed-width viewBox under many generations');
  for (const c of ticks) assert(+c.getAttribute('cx') <= 1000, 'every round tick stays within the viewBox width');
});

// ---- (c) the generations page renders the banner + match-card grid ----

test('generations view: renders the champion-defends banner + one compact match card per challenger (wrapping grid)', async () => {
  freshState(); installFetch();
  const gens = await import('../js/variants/T/views/gens.js');
  const host = document.createElement('div');
  await gens.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID });

  const banner = allByClass(host, 'dt-champ-banner')[0];
  assert(banner, 'the champion-defends banner rendered');
  assert(host.textContent.includes('CHAMPION'), 'the banner names the champion role');
  assert(host.textContent.includes('v0'), 'the banner shows the champion id (v0)');
  assert(host.textContent.includes('title defence'), 'the banner shows the title-defence count');

  const grid = allByClass(host, 'dt-matchcards')[0];
  assert(grid, 'a match-cards grid container rendered');
  const cards = allByClass(host, 'dt-match-card');
  assertEqual(cards.length, 2, 'one match card per challenger round (v0→v1, v0→v2)');
  // each card carries the versus, a Δ, a one-line hypothesis, and a status link.
  assert(host.textContent.includes('Enforce explicit slide-structure output'), 'the (one-line) hypothesis core idea is on a card');
  assert(allByClass(host, 'dt-match-delta').length === 2, 'each card shows a Δscalar');
  assert(allByClass(host, 'dt-match-open').length === 2, 'each card has a status link (dead-branch/promoted → open)');
  // the decisive driver from the gate.
  assert(host.textContent.includes('incorporates_feedback'), 'the decisive-driver judge appears on the v1 card');
});

// ---- (d) match cards must NOT appear on the environment / workspace view ----

test('match cards: do NOT render on the environment / workspace (home) view', async () => {
  freshState(); installFetch();
  const home = await import('../js/variants/T/views/home.js');
  const host = document.createElement('div');
  await home.render(host, { navigate() {}, href: router.href }, {});
  assert(host.textContent.includes('Environment'), 'the home/environment view rendered');
  assertEqual(allByClass(host, 'dt-match-card').length, 0, 'NO match cards on the environment view');
  assertEqual(allByClass(host, 'dt-champ-banner').length, 0, 'NO champion-defends banner on the environment view');
  assertEqual(allByClass(host, 'tr-reel').length, 0, 'NO reel on the environment view');
});

// ---- (e) CHANGE 2: the density picker is GONE; cozy is the baseline ----

test('density removed: no picker, no density APIs; cozy is the permanent baseline', () => {
  freshState();
  // the density picker + its read/persist/normalise/applyDensity surface is gone.
  assert(ui.DENSITY_THEMES === undefined, 'no DENSITY_THEMES table (the picker is removed)');
  assert(typeof ui.readDensity !== 'function', 'no readDensity (density is not a setting anymore)');
  assert(typeof ui.persistDensity !== 'function', 'no persistDensity');
  assert(typeof ui.applyDensity !== 'function' && typeof shell.applyDensity !== 'function', 'no applyDensity picker plumbing');
  assert(shell.DENSITIES === undefined, 'the shell no longer exposes density ids');
  // cozy is the one permanent baseline.
  assertEqual(ui.DENSITY, 'cozy', 'the active density constant is cozy');

  // the SIZE tokens are FIXED at the cozy values regardless of any argument.
  const cozy = ui.densityTokens();
  assertEqual(cozy.sizeScale, 1, 'cozy sizeScale baseline');
  assertEqual(cozy.heatCell, 16, 'cozy heatmap cell baseline');
  assertEqual(cozy.dagRowStep, 34, 'cozy DAG row-step baseline');
  assertEqual(cozy.reelScale, 1.18, 'cozy reel-scale baseline');
  // an (ignored) argument cannot change the baseline.
  assertEqual(ui.densityTokens('compact').sizeScale, 1, 'a compact arg is ignored — still cozy');
  assertEqual(ui.densityTokens('roomy').heatCell, 16, 'a roomy arg is ignored — still cozy');

  // the shell stamps the cozy baseline (never changes) on mount.
  const root = mountLiveShell('#/');
  assertEqual(root.getAttribute('data-t-density'), 'cozy', 'the mounted root carries the cozy baseline');

  // and the CSS bakes the cozy --dt-* spacing tokens unconditionally on the root,
  // with NO density-conditional selectors left.
  const css = readCss();
  assert(!/\[data-t-density="compact"\]/.test(css), 'no compact density selector in the CSS');
  assert(!/\[data-t-density="roomy"\]/.test(css), 'no roomy density selector in the CSS');
  assert(/#variant-root\[data-variant="T"\]\s*\{[^}]*--dt-rail:\s*288px/.test(css.replace(/\n/g, ' ')),
    'the cozy --dt-rail (288px) is the unconditional baseline on the root');
});

// ====================================================================
// Console IV folds (round 8): visual elements FIT their panes, and the
// density picker scales visual-element SIZE (not only spacing).
// ====================================================================

const dag = await import('../js/variants/T/dag.js');

// helpers to read the painted SVG of a view -------------------------
function svgsByClass(host, cls) {
  return host.querySelectorAll('[class]').filter((n) =>
    n.localName === 'svg' && (n.getAttribute('class') || '').split(/\s+/).includes(cls));
}
// does any ancestor (within host) carry an inline horizontal-scroll style?
function hasScrollWrapperAncestor(node, host) {
  let n = node && node.parentNode;
  while (n && n !== host) {
    const style = (n.getAttribute && n.getAttribute('style')) || '';
    const cls = (n.getAttribute && n.getAttribute('class')) || '';
    // a contained table-scroll wrapper is allowed; a panel/figure scroll is not.
    if (/overflow-x\s*:\s*auto|overflow-x\s*:\s*scroll/.test(style) && !cls.includes('dn-table-scroll')) return true;
    n = n.parentNode;
  }
  return false;
}

// ---- (a) the lifecycle DAG + sankey are fit-to-width responsive SVG ----

test('fit-to-width: the lifecycle DAG renders as a responsive SVG (width:100% + viewBox), with NO horizontal-scroll wrapper around the figure', async () => {
  freshState(); installFetch();
  const candidate = await import('../js/variants/T/views/candidate.js');
  const host = document.createElement('div');
  await candidate.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, gen: 'v1' });

  const dagSvg = svgsByClass(host, 'ezn-dag')[0];
  assert(dagSvg, 'the lifecycle DAG SVG rendered on the candidate page');
  assertEqual(dagSvg.getAttribute('width'), '100%', 'the DAG SVG is width:100% (fit-to-width, not a fixed pixel width)');
  assert((dagSvg.getAttribute('viewBox') || '').startsWith('0 0 '), 'the DAG SVG carries a viewBox so it scales to its pane');
  assert(!hasScrollWrapperAncestor(dagSvg, host), 'no horizontal-scroll wrapper around the lifecycle DAG figure/panel');

  // the unit builder honours the same contract directly.
  const direct = dag.lifecycleDag({ genId: 'v1', parentId: 'v0', entries: [{ entry_id: 'b1', drift_loss: 10, pass_fail: 0 }], decision: 'rejected' });
  assertEqual(direct.getAttribute('width'), '100%', 'lifecycleDag() builds a width:100% SVG');
});

test('fit-to-width: the Tufte sankey renders as a responsive SVG (width:100% + viewBox)', () => {
  const node = svg.sankey({
    width: 720,
    patch: [{ id: 'p', label: 'patch', value: 10 }],
    drift: [{ id: 'd', label: 'drift', value: 10 }],
    gate: [{ id: 'g', label: 'gate', value: 10 }],
    links: [{ source: 'p', target: 'd', value: 10 }, { source: 'd', target: 'g', value: 10 }],
  });
  assertEqual(node.localName, 'svg', 'sankey builds an SVG');
  assertEqual(node.getAttribute('width'), '100%', 'the sankey is width:100% (fit-to-width)');
  assert((node.getAttribute('viewBox') || '').startsWith('0 0 '), 'the sankey carries a viewBox');
});

// ---- (a′) the per-board dot-plot + epoch heatmap are responsive too ----

test('fit-to-width: the epoch heatmap is a responsive SVG and its panel does NOT scroll horizontally', async () => {
  freshState(); installFetch();
  const epoch = await import('../js/variants/T/views/epoch.js');
  const host = document.createElement('div');
  await epoch.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID });
  const hm = svgsByClass(host, 'dn-heatmap')[0];
  assert(hm, 'the heatmap rendered on the epoch view');
  assertEqual(hm.getAttribute('width'), '100%', 'the heatmap SVG is width:100% (fit-to-width)');
  assert(!hasScrollWrapperAncestor(hm, host), 'the heatmap panel does NOT carry a horizontal-scroll wrapper');
});

// ---- (b) the publication view's wide content is CONTAINED -------------

test('contained: the publication view’s wide tables carry their OWN contained overflow — the panel itself does not scroll horizontally', async () => {
  freshState(); installFetch();
  const publication = await import('../js/variants/T/views/publication.js');
  const host = document.createElement('div');
  await publication.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID });

  // the per-matchup-detail + aggregate-scores tables are wrapped in a contained
  // scroll box, so the table can be wide WITHOUT the surrounding paper/panel
  // overflowing.
  const tables = host.querySelectorAll('[class]').filter((n) => n.localName === 'table'
    && /dn-(md|sc|scores|board)-table/.test(n.getAttribute('class') || ''));
  assert(tables.length >= 1, 'the publication rendered at least one table');
  let contained = 0;
  for (const t of tables) {
    let n = t.parentNode; let ok = false;
    while (n && n !== host) { if ((n.getAttribute('class') || '').includes('dn-table-scroll')) { ok = true; break; } n = n.parentNode; }
    if (ok) contained++;
  }
  assert(contained === tables.length, 'every wide publication table sits inside a contained .dn-table-scroll box (' + contained + '/' + tables.length + ')');

  // and the live figures in the paper are responsive (no fixed-pixel-width SVG
  // that could exceed the paper column).
  const figSvgs = host.querySelectorAll('[class]').filter((n) => n.localName === 'svg');
  assert(figSvgs.length >= 1, 'the paper spliced at least one live figure');
  for (const s of figSvgs) assertEqual(s.getAttribute('width'), '100%', 'each paper figure SVG is width:100% (contained within the paper column)');
});

// ---- (c) the fixed cozy SIZE tokens still drive a fit-to-width DAG ----

test('cozy SIZE tokens drive the lifecycle-DAG dimensions and it stays fit-to-width', () => {
  const entries = [{ entry_id: 'b1', drift_loss: 10, pass_fail: 0 }, { entry_id: 'b2', drift_loss: 20, pass_fail: 1 }];
  const ct = ui.densityTokens();   // the cozy baseline (no argument needed)
  const h = Math.max(Math.round(300 * ct.sizeScale), Math.round(120 * ct.sizeScale) + entries.length * ct.dagRowStep);
  const d = dag.lifecycleDag({ genId: 'v1', parentId: 'v0', entries, decision: 'rejected', height: h });
  assert(+d.getAttribute('height') === h, 'the painted DAG SVG height honours the cozy size token');
  // fit-to-width holds (Problem 1): width:100% + a viewBox so it scales to the pane.
  assertEqual(d.getAttribute('width'), '100%', 'the DAG is width:100% (fit-to-width)');
  assert((d.getAttribute('viewBox') || '').startsWith('0 0 '), 'the DAG carries a viewBox');
});

// ====================================================================
// Console IV folds (round 9): a PAGE-WIDE SCALE pill + a FLUID,
// resolution-responsive layout. The operator scales the WHOLE page
// (text + diagrams) — NOT per-pane — and the content uses the full
// viewport width so the side-by-side compare panes (and their SVGs)
// render as large as the screen allows.
// ====================================================================

// shared helper: mount the real shell against a live `location` (the same
// harness plumbing the back-button test uses), returning the root.
function mountLiveShell(initialHash) {
  const listeners = { hashchange: [] };
  globalThis.HashChangeEvent = function HashChangeEvent() {};
  globalThis.EventSource = function EventSource() { this.readyState = 0; this.addEventListener = () => {}; this.close = () => {}; };
  globalThis.EventSource.CLOSED = 2;
  globalThis.window = globalThis.window || {};
  globalThis.window.addEventListener = (t, fn) => { (listeners[t] = listeners[t] || []).push(fn); };
  const loc = { _hash: '' };
  Object.defineProperty(loc, 'hash', {
    get() { return this._hash; },
    set(v) { this._hash = v; for (const fn of (listeners.hashchange || [])) fn(); },
    configurable: true,
  });
  globalThis.location = loc;
  globalThis.window.location = loc;
  globalThis.window.dispatchEvent = () => { for (const fn of (listeners.hashchange || [])) fn(); };

  const root = document.createElement('div');
  document.body.appendChild(root);
  loc._hash = initialHash || '#/';
  shell.mountShell(root);
  return root;
}

// ---- (a) the scale constants + normalisation (the pill's range) ----

test('page scale: ui exposes a 70–150% range (5% steps) with a 100% default and snaps/clamps', () => {
  freshState();
  assertEqual(ui.DEFAULT_SCALE, 100, 'the page scale defaults to 100%');
  assertEqual(ui.SCALE_MIN, 70, 'the pill floors at 70%');
  assertEqual(ui.SCALE_MAX, 150, 'the pill ceils at 150%');
  assertEqual(ui.SCALE_STEP, 5, 'the pill steps by 5%');
  assertEqual(ui.normaliseScale(40), 70, 'below-range clamps up to the min');
  assertEqual(ui.normaliseScale(999), 150, 'above-range clamps down to the max');
  assertEqual(ui.normaliseScale(112), 110, 'an off-grid value snaps to the 5% step grid');
  assertEqual(ui.normaliseScale('nonsense'), 100, 'a non-numeric value falls back to the default');
  // the shell re-exports the same surface for views/tests.
  assertEqual(shell.DEFAULT_SCALE, 100, 'the shell exposes the default scale');
});

// ---- (b) the pill exists in the chrome + drives a PAGE-WIDE scale ----

test('page scale: the draggable scale pill exists in the chrome; setting it applies a PAGE-WIDE scale at the app ROOT (not a pane) + persists + restores', () => {
  // start from a clean store so the restore assertion is meaningful.
  try { globalThis.window.localStorage.clear(); } catch (e) { /* ignore */ }
  const root = mountLiveShell('#/');

  // the pill is a draggable range input that lives in the top chrome.
  const pill = allByClass(root, 'dt-scale-pill')[0];
  assert(pill, 'the scale pill rendered in the chrome (beside the pickers)');
  const range = root.querySelectorAll('[class]').filter((n) =>
    n.localName === 'input' && (n.getAttribute('class') || '').includes('dt-scale-range'))[0];
  assert(range, 'the pill is a draggable/keyboard range input');
  assertEqual(range.getAttribute('type'), 'range', 'it is a native range slider (draggable + arrow-key accessible)');
  assertEqual(range.getAttribute('min'), '70', 'the slider min is 70%');
  assertEqual(range.getAttribute('max'), '150', 'the slider max is 150%');
  assertEqual(range.getAttribute('step'), '5', 'the slider steps by 5%');
  assert(allByClass(root, 'dt-scale-readout')[0], 'a % readout sits beside the slider');

  // default is 100% (no clipping; whole page at native size).
  assertEqual(root.getAttribute('data-t-scale'), '100', 'the page starts at 100% scale');

  // DRAG / SET the pill → the WHOLE PAGE scales at the app ROOT (the zoom token
  // changes on the variant root, NOT on any individual pane).
  range.value = '130';
  range.setAttribute('value', '130');
  range.dispatchEvent({ type: 'input', target: range });
  assertEqual(root.getAttribute('data-t-scale'), '130', 'the app ROOT records the new page scale');
  assertEqual(String(root.style.zoom), '1.3', 'the scale is applied as `zoom` on the variant root (page-wide, reflows — no clipping)');
  assertEqual(root.style.cssText.includes('--dt-page-scale:1.3'), true, 'the raw scale ratio is stamped on the root');
  // the readout reflects the new value.
  assert(allByClass(root, 'dt-scale-readout')[0].textContent.includes('130%'), 'the % readout updated to 130%');

  // it is NOT a per-pane control: no pane carries its own scale attribute/zoom.
  const panes = root.querySelectorAll('[class]').filter((n) =>
    (n.getAttribute('class') || '').split(/\s+/).some((c) => /pane|split-side|viewhost/.test(c)));
  for (const p of panes) {
    assert(p.getAttribute('data-t-scale') == null, 'no per-pane scale attribute (scaling is page-wide)');
    assert(!(p.style && p.style.cssText && p.style.cssText.includes('zoom')), 'no per-pane zoom (scaling is page-wide)');
  }

  // PERSIST: the chosen scale was written to localStorage.
  assertEqual(ui.readScale(), 130, 'the chosen page scale persisted to localStorage');

  // RESTORE: a fresh mount reads it back and re-applies it to the root.
  const root2 = mountLiveShell('#/');
  assertEqual(root2.getAttribute('data-t-scale'), '130', 'a fresh mount restores the persisted scale');
  assertEqual(String(root2.style.zoom), '1.3', 'the restored scale is re-applied as root zoom');
  const range2 = root2.querySelectorAll('[class]').filter((n) =>
    n.localName === 'input' && (n.getAttribute('class') || '').includes('dt-scale-range'))[0];
  assertEqual(range2.getAttribute('value'), '130', 'the restored slider reflects the persisted value');
});

// ---- (c) keyboard accessibility (the pill is a native range) -------

test('page scale: the pill is keyboard-accessible — it is a focusable native range with the aria value bounds', () => {
  const root = mountLiveShell('#/');
  const range = root.querySelectorAll('[class]').filter((n) =>
    n.localName === 'input' && (n.getAttribute('class') || '').includes('dt-scale-range'))[0];
  // a native range input is inherently arrow-key adjustable; expose the aria bounds.
  assertEqual(range.getAttribute('aria-valuemin'), '70', 'aria-valuemin set for assistive tech');
  assertEqual(range.getAttribute('aria-valuemax'), '150', 'aria-valuemax set for assistive tech');
  assert(range.getAttribute('aria-valuenow') != null, 'aria-valuenow tracks the current scale');
  assert((range.getAttribute('aria-label') || '').length > 0, 'the slider carries an aria-label');
});

// ---- (d) scale COMPOSES with density (one does not reset the other) ----

test('page scale: persists across re-applies and survives a colour/typeface change (the sole sizing axis)', () => {
  try { globalThis.window.localStorage.clear(); } catch (e) { /* ignore */ }
  const root = mountLiveShell('#/');

  // set a non-default scale, then change colour + typeface (the other axes).
  shell.applyScale(85, root);
  assertEqual(root.getAttribute('data-t-scale'), '85', 'page scale set to 85%');
  shell.applyTheme('solarized-dark', root);
  shell.applyTypeface('display', root);
  // the scale is UNCHANGED by a colour/typeface switch (separate axes).
  assertEqual(root.getAttribute('data-t-scale'), '85', 'switching colour/typeface left the page scale untouched');
  assertEqual(ui.readScale(), 85, 'the page scale is still persisted at 85%');

  // re-apply a new scale — it lands and the cozy density baseline is untouched.
  shell.applyScale(120, root);
  assertEqual(root.getAttribute('data-t-density'), 'cozy', 'the density baseline stays cozy');
  assertEqual(root.getAttribute('data-t-scale'), '120', 'the new page scale applied');
  assertEqual(ui.readScale(), 120, 'scale persists independently');
});

// ---- CHANGE 4: the scale RESET affordance returns to 100% + persists ----

test('page scale RESET: a keyboard-accessible reset button snaps the scale back to 100% and persists', () => {
  try { globalThis.window.localStorage.clear(); } catch (e) { /* ignore */ }
  const root = mountLiveShell('#/');

  // move off 100% first.
  shell.applyScale(135, root);
  assertEqual(root.getAttribute('data-t-scale'), '135', 'scale moved to 135%');
  assertEqual(ui.readScale(), 135, '135% persisted');

  // the reset affordance is a real <button> (keyboard-accessible) beside the pill.
  const resetBtn = root.querySelectorAll('[class]').filter((n) =>
    n.localName === 'button' && (n.getAttribute('class') || '').includes('dt-scale-reset'))[0];
  assert(resetBtn, 'a reset button sits beside the scale pill');
  assert((resetBtn.getAttribute('aria-label') || '').length > 0, 'the reset button carries an aria-label (keyboard/AT accessible)');

  // clicking it snaps the page scale back to 100% and persists.
  resetBtn.dispatchEvent({ type: 'click' });
  assertEqual(root.getAttribute('data-t-scale'), '100', 'reset returned the page scale to 100%');
  assertEqual(String(root.style.zoom), '1', 'the root zoom is back to 1 (100%)');
  assertEqual(ui.readScale(), 100, 'the reset 100% persisted to localStorage');

  // the programmatic resetScale() export does the same.
  shell.applyScale(70, root);
  shell.resetScale(root);
  assertEqual(root.getAttribute('data-t-scale'), '100', 'resetScale() also returns to 100%');
});

// ---- CHANGE 5 + 6: thirteen themes + the colour SWATCH DROPDOWN ----

test('colour themes: all THIRTEEN are registered, each defines the full --v2 token contract, and selecting each applies it', () => {
  freshState();
  const ids = ui.COLOR_THEMES.map((t) => t[0]);
  const expected = ['monokai', 'solarized-dark', 'solarized-light',
    'google-light', 'google-dark', 'lunaria-light', 'lunaria-eclipse',
    'belafonte-day', 'belafonte-night',
    'paper', 'zenburn', 'selenized-black', 'relaxed'];
  assertEqual(ids.length, 13, 'thirteen colour themes registered');
  assertDeep(ids, expected, 'the thirteen ids are the three originals + ten Gogh palettes');
  assertEqual(ui.DEFAULT_COLOR, 'monokai', 'monokai stays the default');

  // every theme defines the FULL --v2 token contract in the scoped CSS.
  const css = readCss();
  const contract = ['paper', 'panel', 'ink', 'ink-soft', 'ink-faint', 'rule', 'rule-soft',
    'good', 'good-soft', 'bad', 'bad-soft', 'caution', 'accent', 'flat', 'cell-empty'];
  for (const id of ids) {
    if (id === 'monokai') continue; // monokai shares the bare-root default block
    const re = new RegExp('\\[data-t-theme="' + id + '"\\]\\s*\\{([^}]*)\\}');
    const m = re.exec(css.replace(/\n/g, ' '));
    assert(m, 'theme ' + id + ' has a CSS block');
    for (const tok of contract) {
      assert(m[1].includes('--v2-' + tok + ':'), 'theme ' + id + ' defines --v2-' + tok);
    }
  }

  // selecting EACH theme applies it to the root + persists (incl. all ten Gogh).
  const root = document.createElement('div');
  for (const id of ids) {
    shell.applyTheme(id, root);
    assertEqual(root.getAttribute('data-t-theme'), id, id + ' applied to the root');
    assertEqual(ui.readColor(), id, id + ' persisted');
  }
});

// ---- ROUND 9: the four NEW Gogh themes (Paper/Zenburn/Selenized Black/Relaxed) ----

test('new themes: Paper/Zenburn/Selenized Black/Relaxed are registered with swatch strips, define the full token contract, and selecting each changes the root attribute + tokens', () => {
  freshState();
  const css = readCss();
  const byId = new Map(ui.COLOR_THEMES.map((t) => [t[0], t]));
  const NEW = ['paper', 'zenburn', 'selenized-black', 'relaxed'];
  const contract = ['paper', 'panel', 'ink', 'ink-soft', 'ink-faint', 'rule', 'rule-soft',
    'good', 'good-soft', 'bad', 'bad-soft', 'caution', 'accent', 'flat', 'cell-empty'];

  // (a) each new theme is registered with a name + a 4–6-colour preview strip.
  for (const id of NEW) {
    const t = byId.get(id);
    assert(t, id + ' is registered in COLOR_THEMES');
    assert(typeof t[1] === 'string' && t[1].length > 0, id + ' has a display name');
    assert(Array.isArray(t[2]) && t[2].length >= 4 && t[2].length <= 6, id + ' has a 4–6-colour swatch strip (got ' + t[2].length + ')');
    for (const c of t[2]) assert(/^#[0-9a-fA-F]{6}$/.test(c), id + ' swatch ' + c + ' is an inlined hex (no network)');
  }

  // (b) each new theme defines the full --v2 token contract in the scoped CSS,
  //     and its token block differs from monokai's (the default) — tokens differ.
  const monokaiBlock = /\[data-t-theme="monokai"\]\s*\{([^}]*)\}/.exec(css.replace(/\n/g, ' '))[1];
  for (const id of NEW) {
    const m = new RegExp('\\[data-t-theme="' + id + '"\\]\\s*\\{([^}]*)\\}').exec(css.replace(/\n/g, ' '));
    assert(m, id + ' has a scoped CSS token block');
    for (const tok of contract) assert(m[1].includes('--v2-' + tok + ':'), id + ' defines --v2-' + tok);
    const paperVal = /--v2-paper:\s*([^;]+);/.exec(m[1])[1].trim();
    const monokaiPaper = /--v2-paper:\s*([^;]+);/.exec(monokaiBlock)[1].trim();
    assert(paperVal.toLowerCase() !== monokaiPaper.toLowerCase(), id + ' ground differs from monokai (tokens differ)');
  }

  // (c) selecting each NEW theme applies it to the root attribute + persists.
  const root = document.createElement('div');
  shell.applyTheme('monokai', root);
  let prev = root.getAttribute('data-t-theme');
  for (const id of NEW) {
    shell.applyTheme(id, root);
    assertEqual(root.getAttribute('data-t-theme'), id, id + ' applied to the root (attribute changed)');
    assert(prev !== id, 'the root theme attribute changed selecting ' + id);
    assertEqual(ui.readColor(), id, id + ' persisted');
    prev = id;
  }
});

test('colour picker is a SWATCH DROPDOWN: a closed trigger with the current swatch+name and one swatch strip per option', () => {
  try { globalThis.window.localStorage.clear(); } catch (e) { /* ignore */ }
  shell.applyTheme('monokai');
  const root = mountLiveShell('#/');

  // the colour control is a dropdown (NOT the old inline button row).
  const dd = allByClass(root, 'dt-cd')[0];
  assert(dd, 'the colour control is a dropdown (dt-cd)');
  assertEqual(allByClass(root, 'dt-theme-btn').length, 0, 'no old inline colour buttons remain');

  // the closed trigger shows the current theme name + a swatch strip preview.
  const trigger = allByClass(root, 'dt-cd-trigger')[0];
  assert(trigger, 'a dropdown trigger button rendered');
  assert(trigger.textContent.includes('monokai'), 'the trigger names the current theme');
  const triggerStrip = trigger.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('dt-swatch-strip'))[0];
  assert(triggerStrip, 'the trigger shows a swatch-strip preview');

  // the listbox has one OPTION per theme, each with a swatch strip (≥4 swatches) + name.
  const options = allByClass(root, 'dt-cd-option');
  assertEqual(options.length, 13, 'one dropdown option per theme (thirteen)');
  // the four new themes each surface as a listed option.
  for (const id of ['paper', 'zenburn', 'selenized-black', 'relaxed']) {
    assert(options.filter((o) => o.getAttribute('data-theme') === id).length === 1, id + ' is a listed dropdown option');
  }
  for (const opt of options) {
    const strip = opt.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('dt-swatch-strip'))[0];
    assert(strip, 'option ' + opt.getAttribute('data-theme') + ' has a swatch strip');
    const swatches = strip.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').split(/\s+/).includes('dt-swatch'));
    assert(swatches.length >= 4, 'the strip shows ≥4 representative colours (got ' + swatches.length + ')');
    // the swatches are real colour values (inline background style).
    assert((swatches[0].getAttribute('style') || '').includes('background:'), 'a swatch carries a colour background');
  }

  // clicking an option applies + persists that theme, and the trigger updates.
  const opt = options.filter((o) => o.getAttribute('data-theme') === 'belafonte-night')[0];
  opt.dispatchEvent({ type: 'click' });
  assertEqual(root.getAttribute('data-t-theme'), 'belafonte-night', 'clicking the option applied the theme');
  assertEqual(ui.readColor(), 'belafonte-night', 'the chosen theme persisted');
  assert(allByClass(root, 'dt-cd-trigger')[0].textContent.includes('belafonte night'), 'the closed trigger now shows the chosen theme');

  // keyboard: ArrowDown on the trigger opens; Enter on the open list selects.
  const triggerKb = allByClass(root, 'dt-cd-trigger')[0];
  triggerKb.dispatchEvent({ type: 'keydown', key: 'ArrowDown', preventDefault() {}, target: triggerKb });
  assert((dd.getAttribute('class') || '').includes('dt-cd-open'), 'ArrowDown opens the dropdown');
  const list = allByClass(root, 'dt-cd-list')[0];
  // arrow to the first option (monokai) and select it.
  list.dispatchEvent({ type: 'keydown', key: 'ArrowUp', preventDefault() {} });
  // Esc closes without further change.
  list.dispatchEvent({ type: 'keydown', key: 'Escape', preventDefault() {} });
  assert(!(dd.getAttribute('class') || '').includes('dt-cd-open'), 'Escape closes the dropdown');
});

// ---- (e) the layout is FLUID (not clamped to a narrow column) ------

test('layout: the detail pane + compare grid are FLUID — not clamped to a narrow fixed max-width; the compare split uses the FULL content width', async () => {
  // the detail host fills the width (width:100%), so the two compare panes
  // each take HALF the FULL content width — not half of a narrow column.
  const css = await import('node:fs').then((fs) =>
    fs.readFileSync(new URL('../css/variants/T/console4.css', import.meta.url), 'utf8'));

  // the OLD narrow caps are gone (1160 on the detail pane, 1320 on the viewhost).
  assert(!/\.dt-viewhost\s*\{[^}]*max-width:\s*1160px/.test(css.replace(/\n/g, ' ')),
    'the detail pane is no longer clamped to the narrow 1160px column');
  assert(!/\.dn-viewhost\s*\{[^}]*max-width:\s*1320px/.test(css.replace(/\n/g, ' ')),
    'the legacy viewhost is no longer clamped to the narrow 1320px column');
  // the detail pane is fluid (width:100%).
  assert(/\.dt-viewhost\s*\{[^}]*width:\s*100%/.test(css.replace(/\n/g, ' ')),
    'the detail pane is fluid (width:100%, fills the available column)');

  // the compare split is a two-equal-column grid (1fr 1fr) — so within a FULL-
  // width detail pane each pane is half the FULL width (bigger SVGs on bigger
  // screens). It only collapses to one column on genuinely small screens.
  assert(/\.dt-split\s*\{[^}]*grid-template-columns:\s*1fr\s+1fr/.test(css.replace(/\n/g, ' ')),
    'the compare split is a two-equal-column grid that fills the detail width');

  // and at runtime the split renders two full-width-sharing sides (not a
  // narrow centred column): render a compare view and confirm the split frame
  // is NOT single-column and carries two sides.
  freshState(); installFetch();
  const candidate = await import('../js/variants/T/views/candidate.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await candidate.render(host, ctx, { epochId: EPOCH_ID, gen: 'v1' }, { params: { epochId: EPOCH_ID, gen: 'v1' }, cmp: 'v2' });
  const split = allByClass(host, 'dt-split')[0];
  assert(split && !(split.getAttribute('class') || '').includes('dt-split-single'),
    'the compare view is a two-column split (each pane gets half the FULL content width)');
  assertEqual(allByClass(host, 'dt-split-side').length, 2, 'two compare panes share the full width');
});

// ====================================================================
// Configurable tournament STRUCTURE — the bracket / standings / racing
// renderers, driven by MOCK structure payloads (the live workspace is
// gauntlet-only, so the non-gauntlet renderers are exercised here).
// ====================================================================

const STRUCT = await import('../js/variants/T/views/structure.js');

// mock single-elim structure payload (§3.2 shape)
const SE_STRUCT = {
  epoch_id: EPOCH_ID, tournament_id: 'tourn_e0_se', structure: 'single_elim',
  structure_params: { seed_order: 'scalar' },
  competitors: [
    { generation_id: 'v0', seed: 1, role: 'champion' },
    { generation_id: 'v1', seed: 2, role: 'challenger' },
    { generation_id: 'v2', seed: 3, role: 'challenger' },
    { generation_id: 'v3', seed: 4, role: 'challenger' },
  ],
  rounds: [
    { round_index: 0, label: 'Semifinal', matches: [
      { match_id: 'WB-R0-0', competitors: ['v0', 'v3'], winner: 'v0', decision: 'rejected', delta_scalar: 0.05, bracket_slot: 'WB-R0-0', bye: false },
      { match_id: 'WB-R0-1', competitors: ['v1', 'v2'], winner: 'v1', decision: 'promoted', delta_scalar: -0.12, bracket_slot: 'WB-R0-1', bye: false },
    ] },
    { round_index: 1, label: 'Final', matches: [
      { match_id: 'WB-R1-0', competitors: ['v0', 'v1'], winner: 'v1', decision: 'promoted', delta_scalar: -0.08, bracket_slot: 'WB-R1-0', bye: false },
    ] },
  ],
  standings: [
    { generation_id: 'v1', rank: 1, scalar: 0.41, wins: 2, losses: 0, status: 'champion', role: 'challenger' },
    { generation_id: 'v0', rank: 2, scalar: 0.49, wins: 1, losses: 1, status: 'eliminated', role: 'champion' },
  ],
  source: 'index',
};

const SWISS_STRUCT = {
  epoch_id: EPOCH_ID, tournament_id: 'tourn_e0_sw', structure: 'swiss',
  structure_params: { rounds: 2 },
  competitors: [{ generation_id: 'v0', seed: 1, role: 'champion' }, { generation_id: 'v1', seed: 2, role: 'challenger' }],
  rounds: [
    { round_index: 0, label: 'Round 1', matches: [{ match_id: 'r0m0', competitors: ['v0', 'v1'], winner: 'v1', delta_scalar: -0.1 }] },
    { round_index: 1, label: 'Round 2', matches: [{ match_id: 'r1m0', competitors: ['v1', 'v2'], winner: 'v1', delta_scalar: -0.03 }] },
  ],
  standings: [
    { generation_id: 'v1', rank: 1, scalar: 0.4, wins: 2, losses: 0, status: 'champion' },
    { generation_id: 'v0', rank: 2, scalar: 0.5, wins: 0, losses: 1, status: 'alive' },
  ],
  source: 'index',
};

const RACING_STRUCT = {
  epoch_id: EPOCH_ID, tournament_id: 'tourn_e0_rc', structure: 'racing',
  structure_params: { rungs: [{ fraction: 0.5, keep: 0.5 }, { fraction: 1.0, keep: 0.5 }] },
  competitors: [
    { generation_id: 'v0', seed: 1, role: 'champion' }, { generation_id: 'v1', seed: 2, role: 'challenger' },
    { generation_id: 'v2', seed: 3, role: 'challenger' }, { generation_id: 'v3', seed: 4, role: 'challenger' },
  ],
  rounds: [
    { round_index: 0, label: 'Rung 1', matches: [{ match_id: 'rung1', competitors: ['v0', 'v1', 'v2', 'v3'], survivors: ['v0', 'v1'], cut: ['v2', 'v3'], board_fraction: 0.5 }] },
    { round_index: 1, label: 'Rung 2', matches: [{ match_id: 'rung2', competitors: ['v0', 'v1'], survivors: ['v1'], cut: ['v0'], board_fraction: 1.0 }] },
  ],
  standings: [{ generation_id: 'v1', rank: 1, scalar: 0.39, status: 'champion' }],
  source: 'index',
};

function structFixture(structure, payload, tournamentId) {
  const gens = payload.competitors.map((c) => ({ generation_id: c.generation_id, epoch_id: EPOCH_ID, parent_generation_id: c.role === 'champion' ? '' : 'v0', promoted: c.role === 'champion' }));
  const F = {
    '/api/epoch': { epoch_id: EPOCH_ID, closed: false, goal: 'g', tournament: { structure, params: payload.structure_params },
      experiments: gens.map((g) => ({ generation_id: g.generation_id, parent_generation_id: g.parent_generation_id, outcome: { decision: g.promoted ? 'baseline' : 'rejected' } })), board: [] },
    '/api/lineage': { generations: gens },
    '/api/score-trajectory': { points: gens.map((g, i) => ({ generation_id: g.generation_id, scalar: 70 + i })) },
    '/api/tournaments': { epoch_id: EPOCH_ID, structure, structure_params: payload.structure_params, champion_lineage: ['v0'],
      matchups: [{ champion: 'v0', challenger: 'v1', decision: 'rejected', delta_scalar: 1 }],
      tournaments: [{ tournament_id: tournamentId, structure, structure_params: payload.structure_params, competitors: payload.competitors, rounds: payload.rounds, standings: payload.standings }] },
    [`/api/tournament-structure/${EPOCH_ID}/${tournamentId}`]: payload,
  };
  return F;
}

function installFixtureMap(F) {
  globalThis.fetch = async (path) => {
    if (Object.prototype.hasOwnProperty.call(F, path)) return { ok: true, json: async () => F[path] };
    return { ok: false, status: 404, json: async () => ({ error: 'nf' }) };
  };
}

test('structure helpers: label + non-gauntlet detection', () => {
  assertEqual(STRUCT.isNonGauntlet('gauntlet'), false);
  assertEqual(STRUCT.isNonGauntlet('single_elim'), true);
  assertEqual(STRUCT.isNonGauntlet('swiss'), true);
  assertEqual(STRUCT.isNonGauntlet('racing'), true);
  assert(STRUCT.structureLabel('swiss', { rounds: 4 }).includes('4 rounds'), 'swiss label names its rounds');
  assert(STRUCT.structureLabel('single_elim', { seed_order: 'scalar' }).includes('scalar'), 'single-elim label names the seed order');
  assert(STRUCT.structureLabel('racing', { rungs: [1, 2, 3] }).includes('3 rungs'), 'racing label names its rungs');
});

test('structure: single-elim renders a fit-to-width bracket (real rounds + standings)', async () => {
  freshState();
  installFixtureMap(structFixture('single_elim', SE_STRUCT, 'tourn_e0_se'));
  const gens = await import('../js/variants/T/views/gens.js');
  const host = document.createElement('div');
  await gens.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID });

  // the structure pill names the configured structure (NOT the gauntlet ladder).
  assert(allByClass(host, 'dt-structure-pill').length >= 1, 'a structure pill labels the configured structure');
  assert(host.textContent.includes('Single elimination'), 'the pill names single-elim');
  assertEqual(allByClass(host, 'dt-champ-banner').length, 0, 'NO gauntlet champion-defends banner for a non-gauntlet structure');

  const bracket = svgsByClass(host, 'dn-sbracket')[0];
  assert(bracket, 'a bracket SVG rendered');
  assertEqual(bracket.getAttribute('width'), '100%', 'the bracket is fit-to-width (width:100%)');
  assert((bracket.getAttribute('viewBox') || '').startsWith('0 0 '), 'the bracket carries a viewBox so it scales to its pane');
  assert(!hasScrollWrapperAncestor(bracket, host), 'no horizontal-scroll wrapper around the bracket');
  // both bracket rounds rendered as columns (heads) and the winners are marked.
  assert(host.textContent.includes('Semifinal') && host.textContent.includes('Final'), 'both bracket rounds render as columns');
  // a standings leaderboard rendered too.
  assert(allByClass(host, 'dt-standings').length >= 1, 'a standings leaderboard rendered');
  assert(host.textContent.includes('champion'), 'the standings names the champion status');
});

test('structure: double-elim splits into winners + losers bands', async () => {
  freshState();
  const DE = JSON.parse(JSON.stringify(SE_STRUCT));
  DE.structure = 'double_elim';
  DE.structure_params = { grand_final_reset: true };
  DE.rounds.push({ round_index: 2, label: 'LB Round 1', matches: [
    { match_id: 'LB-R0-0', competitors: ['v0', 'v2'], winner: 'v0', decision: 'rejected', delta_scalar: 0.02, bracket_slot: 'LB-R0-0', bye: false },
  ] });
  installFixtureMap(structFixture('double_elim', DE, 'tourn_e0_de'));
  const gens = await import('../js/variants/T/views/gens.js');
  const host = document.createElement('div');
  await gens.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID });
  assert(host.textContent.includes('Double elimination'), 'the pill names double-elim');
  assert(host.textContent.includes('Winners’ bracket'), 'a winners-bracket band rendered');
  assert(host.textContent.includes('Losers’ bracket'), 'a losers-bracket band rendered from the LB slots');
  assertEqual(svgsByClass(host, 'dn-sbracket').length, 2, 'two bracket SVGs — one per band');
});

test('structure: swiss renders the standings hero + per-round pairings', async () => {
  freshState();
  installFixtureMap(structFixture('swiss', SWISS_STRUCT, 'tourn_e0_sw'));
  const gens = await import('../js/variants/T/views/gens.js');
  const host = document.createElement('div');
  await gens.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID });
  assert(host.textContent.includes('Swiss'), 'the pill names swiss');
  assert(allByClass(host, 'dt-standings').length >= 1, 'the swiss standings leaderboard rendered');
  assert(allByClass(host, 'dt-swiss-pairings').length === 2, 'a pairings table per round (2 rounds)');
  assert(host.textContent.includes('Round 1') && host.textContent.includes('Round 2'), 'both swiss rounds render');
});

test('structure: racing renders a fit-to-width rung ladder with cuts + board fractions', async () => {
  freshState();
  installFixtureMap(structFixture('racing', RACING_STRUCT, 'tourn_e0_rc'));
  const gens = await import('../js/variants/T/views/gens.js');
  const host = document.createElement('div');
  await gens.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID });
  assert(host.textContent.includes('Racing'), 'the pill names racing');
  const ladder = svgsByClass(host, 'dn-raceladder')[0];
  assert(ladder, 'a racing-ladder SVG rendered');
  assertEqual(ladder.getAttribute('width'), '100%', 'the racing ladder is fit-to-width (width:100%)');
  assert((ladder.getAttribute('viewBox') || '').startsWith('0 0 '), 'the racing ladder carries a viewBox');
  assert(!hasScrollWrapperAncestor(ladder, host), 'no horizontal-scroll wrapper around the racing ladder');
  assert(host.textContent.includes('board 50%'), 'a rung shows its board fraction (budget escalation)');
  assert(host.textContent.includes('Rung 1') && host.textContent.includes('Rung 2'), 'both rungs render as columns');
});

test('structure: a missing structure payload degrades gracefully (no throw, honest empty)', async () => {
  freshState();
  // epoch names swiss but the structure endpoint 404s + no tournaments[].
  const F = {
    '/api/epoch': { epoch_id: EPOCH_ID, closed: false, goal: 'g', tournament: { structure: 'swiss', params: {} }, experiments: [], board: [] },
    '/api/lineage': { generations: [] },
    '/api/score-trajectory': { points: [] },
    '/api/tournaments': { epoch_id: EPOCH_ID, structure: 'swiss', champion_lineage: [], matchups: [], tournaments: [] },
  };
  installFixtureMap(F);
  const gens = await import('../js/variants/T/views/gens.js');
  const host = document.createElement('div');
  await gens.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID });
  assert(host.textContent.includes('Swiss'), 'the structure pill still names swiss');
  assert(/No tournament|unavailable/i.test(host.textContent), 'an honest empty state renders rather than throwing');
});

test('structure: the epoch view shows the structure pill from the epoch tournament block', async () => {
  freshState();
  installFixtureMap(structFixture('swiss', SWISS_STRUCT, 'tourn_e0_sw'));
  const epoch = await import('../js/variants/T/views/epoch.js');
  const host = document.createElement('div');
  await epoch.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID });
  assert(allByClass(host, 'dt-structure-pill').length >= 1, 'the epoch header carries a structure pill');
  assert(host.textContent.includes('Swiss'), 'the epoch pill names the configured swiss structure');
});

test('structure: the data layer exposes tournamentStructure() + invalidates its cache live', async () => {
  assertEqual(typeof data.tournamentStructure, 'function', 'data.tournamentStructure() exists');
  // the live-invalidation set includes the new prefix.
  const css = '';  // (no css needed) — assert the source carries the prefix.
  const src = await import('node:fs').then((fs) => fs.readFileSync(new URL('../js/variants/T/data.js', import.meta.url), 'utf8'));
  assert(src.includes('/api/tournament-structure/'), 'invalidateLive() busts the tournament-structure prefix');
});

// ---- gauntlet REGRESSION: the default structure is unchanged --------

test('gauntlet (default): the match-ups page still renders the champion banner + match cards (no structure pill)', async () => {
  freshState(); installFetch();  // the default gauntlet fixture (no tournament block)
  const gens = await import('../js/variants/T/views/gens.js');
  const host = document.createElement('div');
  await gens.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID });
  assert(allByClass(host, 'dt-champ-banner').length === 1, 'the gauntlet champion-defends banner still renders');
  assertEqual(allByClass(host, 'dt-match-card').length, 2, 'the gauntlet match cards still render (v0→v1, v0→v2)');
  assertEqual(allByClass(host, 'dn-sbracket').length, 0, 'NO bracket SVG for the gauntlet default');
  assertEqual(allByClass(host, 'dt-structure-pill').length, 0, 'NO structure pill for a gauntlet epoch with no tournament block');
});

// ====================================================================
// LIVE-STATUS — surfacing an ACTIVE run for ANY tournament structure
// (the gauntlet-shaped status pill missed live racing/swiss/elim runs).
// ====================================================================

// ---- (a) a non-idle heartbeat + active-runs + active-tournament running ----

test('live-status: a live RACING run (non-idle phase + in-flight runs + tournament running) shows a RUNNING state with structure + phase', () => {
  const status = livestatus.deriveLiveStatus({
    heartbeat: { phase: 'tournament:round_0:rung0_m3', generation_id: 'v3', round_index: 0, epoch_id: EPOCH_ID },
    activeRuns: Array.from({ length: 14 }, (_, i) => ({ generation_id: 'v' + (i % 4), entry_id: 'b' + i, run_id: 'run_' + i, progress: 0.3 })),
    activeTournament: { structure: 'racing', phase: 'running', competitors: [{ generation_id: 'v0' }], rounds: [], standings: [] },
  });
  assertEqual(status.running, true, 'a live racing run reads as RUNNING (not "nothing is running")');
  assertEqual(status.structure, 'racing', 'the structure is surfaced from active-tournament');
  assertEqual(status.inFlight, 14, 'the in-flight board-unit count is surfaced');
  assertEqual(status.tournamentRunning, true, 'active-tournament phase "running" corroborates');
  assert(status.label.includes('racing'), 'the readable label names the structure (racing)');
  assert(status.label.includes('rung 0'), 'the readable label derives the rung from the phase string');
});

test('live-status: the heartbeat phase ALONE (proposing) lights the running state even before a tournament exists', () => {
  const status = livestatus.deriveLiveStatus({
    heartbeat: { phase: 'proposing:field', generation_id: null },
    activeRuns: [],
    activeTournament: null,
  });
  assertEqual(status.running, true, 'a non-idle proposing phase ⇒ running even with no tournament + no active-runs');
  assert(status.label.includes('proposing'), 'the proposing phase yields a readable "proposing …" label');
  assertEqual(status.inFlight, 0, 'no board-units in flight during proposing');
});

test('live-status: in-flight active-runs alone (no heartbeat) still read as running, structure-agnostic', () => {
  const status = livestatus.deriveLiveStatus({
    heartbeat: null,
    activeRuns: [{ generation_id: 'v1', entry_id: 'b0', run_id: 'r0', progress: 0.5 }],
    activeTournament: { structure: 'swiss', phase: 'running' },
  });
  assertEqual(status.running, true, 'a non-empty active-runs feed alone reads as running');
  assertEqual(status.structure, 'swiss', 'the structure is taken from active-tournament');
});

// ---- (b) idle heartbeat + empty active-runs ⇒ idle/done ----

test('live-status: an IDLE heartbeat + empty active-runs + null tournament shows idle/done (not running)', () => {
  const idle = livestatus.deriveLiveStatus({
    heartbeat: { phase: 'idle' }, activeRuns: [], activeTournament: null,
  });
  assertEqual(idle.running, false, 'an idle phase + nothing in flight is NOT running');
  assertEqual(idle.inFlight, 0, 'no in-flight units when idle');
  assertEqual(idle.label, 'idle', 'the idle label reads "idle"');

  const doneTok = livestatus.deriveLiveStatus({
    heartbeat: { phase: 'done' }, activeRuns: [], activeTournament: { structure: 'racing', phase: 'complete' },
  });
  assertEqual(doneTok.running, false, 'a done phase + a completed (not "running") tournament is NOT running');

  // a fully-absent set (no heartbeat, no runs, no tournament) → "done".
  const empty = livestatus.deriveLiveStatus({});
  assertEqual(empty.running, false, 'an empty environment is not running');
  assertEqual(empty.label, 'done', 'an empty environment reads "done"');
});

test('live-status: isActivePhase distinguishes running phases from idle/terminal ones', () => {
  assertEqual(livestatus.isActivePhase('tournament:round_0:rung0_m3'), true, 'a tournament phase is active');
  assertEqual(livestatus.isActivePhase('proposing:field'), true, 'a proposing phase is active');
  assertEqual(livestatus.isActivePhase('idle'), false, 'idle is not active');
  assertEqual(livestatus.isActivePhase('done'), false, 'done is not active');
  assertEqual(livestatus.isActivePhase(''), false, 'an empty phase is not active');
  assertEqual(livestatus.isActivePhase(null), false, 'an absent phase is not active');
});

// ---- (b′) the chrome status pill reflects the running state, digest-gated ----

test('live-status: the chrome RUN badge lights for a live racing run and the status digest is gated (no flash)', () => {
  try { globalThis.window.localStorage.clear(); } catch (e) { /* ignore */ }
  // seed the shared AppState with a live racing run BEFORE mounting the shell.
  coreState.state.connected = true;
  coreState.state.connecting = false;
  coreState.state.setHeartbeat({ phase: 'tournament:round_0:rung0_m3', generation_id: 'v3' });
  coreState.state.activeRuns = [
    { generation_id: 'v1', entry_id: 'waffles_single', run_id: 'r1', progress: 0.4 },
    { generation_id: 'v2', entry_id: 'waffles_single', run_id: 'r2', progress: 0.2 },
  ];
  coreState.state.activeTournament = { structure: 'racing', phase: 'running' };

  const root = mountLiveShell('#/');
  const statusEl = allByClass(root, 'dt-status')[0];
  assert(statusEl, 'the chrome status pill rendered');
  assert((statusEl.getAttribute('class') || '').includes('dt-running'), 'the status pill carries the dt-running state for a live racing run');
  const label = allByClass(root, 'dt-run-label')[0];
  assert(label && label.textContent.includes('racing'), 'the run badge names the structure (racing)');
  const count = allByClass(root, 'dt-run-count')[0];
  assert(count && count.textContent.includes('2'), 'the run badge shows the in-flight board-unit count (2)');

  // DIGEST-GATE: a steady heartbeat re-tick with IDENTICAL live signals must not
  // rewrite the badge text node (no flash). The same derived verdict ⇒ no DOM.
  const labelNodeBefore = label.firstChild;
  coreState.state._changed();           // a heartbeat-style re-tick, same data.
  assert(allByClass(root, 'dt-run-label')[0].firstChild === labelNodeBefore,
    'an unchanged live status is a digest no-op — the run-label text node is not rewritten');

  // reset to an idle environment so other tests start clean.
  coreState.state.heartbeat = { phase: 'idle' };
  coreState.state.activeRuns = [];
  coreState.state.activeTournament = null;
});

// ---- (c) board-detail surfaces the in-flight runs for an entry ----

test('board view: an entry mid-run with NO completed results renders its in-flight candidates (not empty)', async () => {
  freshState(); installFetch();
  // no completed per-entry rows for this fresh entry, but two runs are live on it.
  const board = await import('../js/variants/T/views/board.js');
  coreState.state.activeRuns = [
    { generation_id: 'v3', entry_id: 'waffles_single', run_id: 'run_v3_waffles', progress: 0.65 },
    { generation_id: 'v4', entry_id: 'waffles_single', run_id: 'run_v4_waffles', progress: 0.1 },
    { generation_id: 'v5', entry_id: 'some_other_entry', run_id: 'run_v5_other', progress: 0.5 },
  ];
  const host = document.createElement('div');
  await board.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, entry: 'waffles_single' });

  const live = allByClass(host, 'dn-board-inflight')[0];
  assert(live, 'a live in-flight panel rendered on the board view');
  assert(host.textContent.includes('2 candidates running'), 'the panel reads "2 candidates running" (filtered to THIS entry)');
  assert(host.textContent.includes('v3') && host.textContent.includes('v4'), 'both in-flight candidates on this entry are listed');
  assert(!host.textContent.includes('v5'), 'a run on a DIFFERENT entry is excluded');
  const fills = allByClass(host, 'dn-progress-fill');
  assert(fills.length >= 2, 'each in-flight candidate shows a progress bar');
  assert(host.textContent.includes('65%'), 'a candidate progress percentage is surfaced');

  coreState.state.activeRuns = [];
});

test('board view: the inflightForEntry filter matches entry_id and tolerates alternate keys', async () => {
  const runs = [
    { generation_id: 'v1', entry_id: 'e1', run_id: 'r1' },
    { generation_id: 'v2', board_entry_id: 'e1', run_id: 'r2' },
    { generation_id: 'v3', entry: 'e1', run_id: 'r3' },
    { generation_id: 'v4', entry_id: 'e2', run_id: 'r4' },
  ];
  const b = await import('../js/variants/T/views/board.js');
  assertEqual(b.inflightForEntry(runs, 'e1').length, 3, 'all three e1 runs match across entry_id / board_entry_id / entry keys');
  assertEqual(b.inflightForEntry(runs, 'nope').length, 0, 'no match for an unknown entry');
  assertEqual(b.inflightForEntry(null, 'e1').length, 0, 'a null active-runs feed yields no in-flight runs');
});

// ---- (d) board-detail still renders completed results for finished runs ----

test('board view: an entry WITH completed results still renders the per-candidate breakdown (regression)', async () => {
  freshState(); installFetch();
  coreState.state.activeRuns = [];   // nothing live — pure completed view.
  const board = await import('../js/variants/T/views/board.js');
  const host = document.createElement('div');
  await board.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, entry: 'waffles_single' });
  assert(host.textContent.includes('Board · waffles_single'), 'the completed-results board view still renders');
  assert(allByClass(host, 'dn-board-table').length >= 1, 'the completed per-candidate breakdown table still renders');
  assertEqual(allByClass(host, 'dn-board-inflight').length, 0, 'no in-flight panel when nothing is live on the entry');
});

// ====================================================================
// STRUCTURE-AWARE polish (round: tournament structures render correctly
// both DURING a live run and after).
//   (a) the epoch reel is structure-aware (no gauntlet spine for racing);
//   (b) a LIVE /api/active-tournament fills the ladder (not "nothing ran")
//       and in-flight competitors are not mislabeled rejected;
//   (c) the richer racing ladder renders rungs with cut/survivor + board
//       fraction + a champion-gate;
//   (d) only the CURRENT champion (last in champion_lineage) is badged
//       "champion ♚"; FORMER champions get a distinct "former" marker.
// ====================================================================

// ---- (a) the epoch reel is structure-aware --------------------------

test('epoch reel: a NON-gauntlet (racing) epoch does NOT render the gauntlet champion-spine reel — it shows a structure strip instead', async () => {
  freshState();
  installFixtureMap(structFixture('racing', RACING_STRUCT, 'tourn_e0_rc'));
  const epoch = await import('../js/variants/T/views/epoch.js');
  const host = document.createElement('div');
  await epoch.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID });

  // the gauntlet sequential-rounds reel (champion spine + r0…rN ticks) is GONE.
  assertEqual(allByClass(host, 'tr-reel').length, 0, 'NO champion-spine reel for a racing epoch');
  assertEqual(allByClass(host, 'tr-spine').length, 0, 'NO champion-spine line for a racing epoch');
  assertEqual(allByClass(host, 'tr-tick').length, 0, 'NO sequential round ticks for a racing epoch');
  // a compact structure strip stands in its place, naming the structure + field.
  const strip = allByClass(host, 'dt-struct-strip')[0];
  assert(strip, 'a compact structure strip replaced the reel');
  assert(host.textContent.includes('Racing'), 'the strip names the racing structure');
  assert(/field of \d+/.test(host.textContent), 'the strip names the field size');
  assert(host.textContent.includes('rung'), 'the strip names the rung count for racing');
  assert(host.textContent.includes('See Match-ups'), 'a "See Match-ups" affordance opens the real ladder');
  // the epoch overview structure is otherwise unchanged (objective + brief).
  assert(host.textContent.includes('objective'), 'the epoch overview keeps its objective block');
});

test('epoch reel: a GAUNTLET epoch STILL renders the champion-spine reel (regression — unchanged)', async () => {
  freshState(); installFetch();   // the default gauntlet fixture (no tournament block)
  const epoch = await import('../js/variants/T/views/epoch.js');
  const host = document.createElement('div');
  await epoch.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID });
  assert(allByClass(host, 'tr-reel')[0], 'the gauntlet epoch keeps the champion-spine reel');
  assert(allByClass(host, 'tr-spine')[0], 'the gauntlet reel keeps its champion spine');
  assert(allByClass(host, 'tr-tick').length >= 2, 'the gauntlet reel keeps its per-round ticks');
  assertEqual(allByClass(host, 'dt-struct-strip').length, 0, 'NO structure strip for a gauntlet epoch');
});

// ---- (b) a LIVE active-tournament fills the ladder during a run -----

// a live racing /api/active-tournament: first rung decided, second rung still
// racing (no cut/survivors recorded yet — the run is in flight). The eventual
// winner (v1) is NOT yet committed, so it must NOT show as rejected/eliminated.
const LIVE_RACING = {
  structure: 'racing', phase: 'running',
  structure_params: { rungs: [{ fraction: 0.5 }, { fraction: 1.0 }] },
  competitors: [
    { generation_id: 'v0', seed: 1, role: 'champion' }, { generation_id: 'v1', seed: 2, role: 'challenger' },
    { generation_id: 'v2', seed: 3, role: 'challenger' }, { generation_id: 'v3', seed: 4, role: 'challenger' },
  ],
  rounds: [
    { round_index: 0, label: 'Rung 1', matches: [{ match_id: 'rung1', competitors: ['v0', 'v1', 'v2', 'v3'], survivors: ['v0', 'v1'], cut: ['v2', 'v3'], board_fraction: 0.5 }] },
    { round_index: 1, label: 'Rung 2', matches: [{ match_id: 'rung2', competitors: ['v0', 'v1'], survivors: [], cut: [], board_fraction: 1.0 }] },
  ],
  // mid-run the completed-record view would crown v1 already; the LIVE record
  // leaves everyone racing.
  standings: [
    { generation_id: 'v1', rank: 1, scalar: 0.39, status: 'champion' },
    { generation_id: 'v0', rank: 2, scalar: 0.44, status: 'eliminated' },
  ],
};

test('live tournament: during a racing RUN the match-ups ladder fills from /api/active-tournament (not "nothing ran")', async () => {
  freshState();
  // the COMPLETED record is empty (the run has not committed any tournament);
  // only the LIVE active-tournament carries the topology.
  const F = {
    '/api/epoch': { epoch_id: EPOCH_ID, closed: false, goal: 'g', tournament: { structure: 'racing', params: LIVE_RACING.structure_params }, experiments: [], board: [] },
    '/api/lineage': { generations: LIVE_RACING.competitors.map((c) => ({ generation_id: c.generation_id, epoch_id: EPOCH_ID, parent_generation_id: c.role === 'champion' ? '' : 'v0', promoted: false })) },
    '/api/score-trajectory': { points: [] },
    '/api/tournaments': { epoch_id: EPOCH_ID, structure: 'racing', champion_lineage: [], matchups: [], tournaments: [] },
    '/api/active-tournament': LIVE_RACING,
  };
  installFixtureMap(F);
  // seed the live signals so deriveLiveStatus() reports a running racing run.
  coreState.state.setHeartbeat({ phase: 'tournament:round_1:rung1_m0', generation_id: 'v1' });
  coreState.state.activeRuns = [{ generation_id: 'v1', entry_id: 'b0', run_id: 'r1', progress: 0.5 }];
  coreState.state.activeTournament = { structure: 'racing', phase: 'running' };

  const gens = await import('../js/variants/T/views/gens.js');
  const host = document.createElement('div');
  await gens.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID });

  // the ladder rendered from the LIVE record — NOT an empty "nothing ran" state.
  const ladder = svgsByClass(host, 'dn-raceladder')[0];
  assert(ladder, 'the racing ladder rendered from the live active-tournament');
  assert(!/No tournament has run|unavailable/i.test(host.textContent), 'NOT the empty "nothing ran yet" state during a live run');
  assert(allByClass(host, 'dt-live-pill')[0], 'a LIVE badge marks the in-flight tournament');
  assert(host.textContent.includes('Rung 1') && host.textContent.includes('Rung 2'), 'both rungs (incl. the still-racing one) render');

  // the eventual winner v1 is NOT mislabeled rejected/eliminated mid-run: the
  // live standings show everyone racing, not "eliminated".
  assert(!host.textContent.includes('eliminated'), 'no competitor is mislabeled "eliminated" during a live run');
  // v1 is NOT struck through as a cut runner anywhere (it survives rung 1, races rung 2).
  const cutNames = allByClass(host, 'dn-out');
  for (const n of cutNames) assert((n.textContent || '').indexOf('v1') < 0, 'the eventual winner v1 is never struck through (cut) mid-run');

  coreState.state.heartbeat = { phase: 'idle' };
  coreState.state.activeRuns = [];
  coreState.state.activeTournament = null;
});

// ---- (c) the richer racing ladder render ----------------------------

test('racing ladder: renders rungs with board fractions, cut (✕) vs survivor (↑) marks, and a champion-gate', () => {
  const rungs = [
    { label: 'Rung 1', competitors: ['v0', 'v1', 'v2', 'v3'], survivors: ['v0', 'v1'], cut: ['v2', 'v3'], board_fraction: 0.5 },
    { label: 'Rung 2', competitors: ['v0', 'v1'], survivors: ['v1'], cut: ['v0'], board_fraction: 1.0 },
  ];
  const node = svg.racingLadder({ rungs, championId: 'v1', onCompetitor() {} });
  assertEqual(node.localName, 'svg', 'the racing ladder is an SVG');
  assertEqual(node.getAttribute('width'), '100%', 'fit-to-width (width:100%)');
  assert((node.getAttribute('viewBox') || '').startsWith('0 0 '), 'carries a viewBox so it scales to its pane');
  const txt = node.textContent;
  assert(txt.includes('board 50%') && txt.includes('board 100%'), 'each rung shows its board fraction (budget escalation)');
  assert(txt.includes('✕'), 'a cut runner is marked ✕');
  assert(txt.includes('↑'), 'a survivor is marked ↑');
  // the trailing champion-gate column crowns the final survivor as champion.
  assert(txt.includes('champion-gate'), 'a champion-gate column is rendered');
  assert(txt.includes('♚ v1') || txt.includes('♚'), 'the final survivor (v1) flows into the champion-gate seat as the new champion ♚');
  // survivor → next-rung connectors trace the halving.
  assert(allByClass(node, 'dn-raceladder-edge').length >= 1, 'survivor connectors trace the field into the next rung');
});

test('racing ladder: a LIVE race leaves the pending rung neutral (nobody cut) and the gate reads "deciding…"', () => {
  const rungs = [
    { label: 'Rung 1', competitors: ['v0', 'v1', 'v2', 'v3'], survivors: ['v0', 'v1'], cut: ['v2', 'v3'], board_fraction: 0.5 },
    { label: 'Rung 2', competitors: ['v0', 'v1'], survivors: [], cut: [], board_fraction: 1.0, pending: true },
  ];
  const node = svg.racingLadder({ rungs, championId: null, live: true, onCompetitor() {} });
  // only the DECIDED rung shows cuts; the pending rung does not strike anyone.
  const struck = allByClass(node, 'dn-out');
  assertEqual(struck.length, 2, 'only the two decided cuts (v2, v3) are struck — the pending rung strikes nobody');
  assert(node.textContent.includes('deciding'), 'the champion-gate reads "deciding…" while the race is live (no premature crown)');
});

// ====================================================================
// RACING LADDER reconstruction from the PER-CHALLENGER match records.
//
// A racing tournament is persisted as ONE record PER CHALLENGER on
// /api/tournaments (NOT one assembled-rung record). Each entry is the
// flattened view from that challenger's seat:
//   { tournament_id:"<epoch>:<champ>-><chall>", structure:"racing",
//     competitors:[champ, chall], standings:[],
//     rounds:[ {match_id:"rungN_mK"|"racing-final", opponent, won, delta_scalar} ] }
// The ladder must AGGREGATE every record and GROUP matches by the `match_id`
// rung prefix to rebuild rung0 → … → champion-gate. This mirrors the LIVE
// epoch 2026-06-01_e0 (champion v0; field v1–v4; v3 promoted).
// ====================================================================

const RC_EPOCH = '2026-06-01_e0';
// the four per-challenger racing records, verbatim from the brief's live epoch.
const RACING_PER_CHALLENGER = [
  { tournament_id: `${RC_EPOCH}:v0->v1`, structure: 'racing', competitors: ['v0', 'v1'], standings: [],
    rounds: [{ match_id: 'rung0_m0', opponent: 'v0', won: false, delta_scalar: 25.0 }] },
  { tournament_id: `${RC_EPOCH}:v0->v2`, structure: 'racing', competitors: ['v0', 'v2'], standings: [],
    rounds: [{ match_id: 'rung0_m1', opponent: 'v0', won: false, delta_scalar: 3.3 }] },
  { tournament_id: `${RC_EPOCH}:v0->v3`, structure: 'racing', competitors: ['v0', 'v3'], standings: [],
    rounds: [
      { match_id: 'rung0_m2', opponent: 'v0', won: true, delta_scalar: -0.16 },
      { match_id: 'rung1_m0', opponent: 'v0', won: false, delta_scalar: 1.0 },
      { match_id: 'racing-final', opponent: 'v0', won: true, delta_scalar: -32.19 },
    ] },
  { tournament_id: `${RC_EPOCH}:v0->v4`, structure: 'racing', competitors: ['v0', 'v4'], standings: [],
    rounds: [
      { match_id: 'rung0_m3', opponent: 'v0', won: false, delta_scalar: 0.002 },
      { match_id: 'rung1_m1', opponent: 'v0', won: false, delta_scalar: 1.25 },
    ] },
];
const RACING_TOURNAMENTS = {
  epoch_id: RC_EPOCH, structure: 'racing',
  structure_params: { eta: 2, board_fraction: 0.25 },
  champion_lineage: ['v0', 'v3'],
  matchups: RACING_PER_CHALLENGER.map((t) => ({ champion: 'v0', challenger: t.tournament_id.split('->')[1], decision: t.tournament_id.endsWith('v3') ? 'promoted' : 'rejected', delta_scalar: 1 })),
  tournaments: RACING_PER_CHALLENGER,
};

// ---- (a) reconstruct the rungs/field/cuts/survivors -----------------

test('racing reconstruct: groups the per-challenger records into rung0 {v1,v2,v3,v4} (v1/v2 cut ✕, v3/v4 survive ↑) and rung1 {v3,v4} (v4 cut, v3 survives)', () => {
  const st = STRUCT.reconstructRacing(RACING_TOURNAMENTS, RC_EPOCH);
  assert(st, 'a racing structure was reconstructed from the per-challenger records');
  assertEqual(st.structure, 'racing', 'the reconstructed structure is racing');
  // only the RUNG rounds (racing-final is the gate, not a rung).
  const rungRounds = st.rounds.filter((r) => String(r.matches[0].match_id) !== 'racing-final');
  assertEqual(rungRounds.length, 2, 'two rungs reconstructed (rung0, rung1)');

  const r0 = rungRounds[0].matches[0];
  assertDeep([...r0.competitors].sort(), ['v1', 'v2', 'v3', 'v4'], 'rung0 field is the full challenger set {v1,v2,v3,v4}');
  assertDeep([...r0.cut].sort(), ['v1', 'v2'], 'rung0 cuts v1 and v2 (no match at rung1 or the final)');
  assertDeep([...r0.survivors].sort(), ['v3', 'v4'], 'rung0 survivors are v3 and v4 (they appear at rung1)');

  const r1 = rungRounds[1].matches[0];
  assertDeep([...r1.competitors].sort(), ['v3', 'v4'], 'rung1 field narrows to {v3,v4}');
  assertDeep([...r1.cut].sort(), ['v4'], 'rung1 cuts v4');
  assertDeep([...r1.survivors].sort(), ['v3'], 'rung1 survivor is v3 (it reaches the champion gate)');
  // each rung carries the board fraction (budget escalation: 25% → 50%).
  assertEqual(r0.board_fraction, 0.25, 'rung0 covers 25% of the board');
  assertEqual(r1.board_fraction, 0.5, 'rung1 escalates to 50% of the board (×η)');
  // and each runner's Δ-vs-champion at the rung is carried for the mark.
  assertEqual(r0.deltas.v1, 25.0, 'rung0 carries v1’s Δ-vs-champion');
  assertEqual(r0.deltas.v3, -0.16, 'rung0 carries v3’s (winning) Δ-vs-champion');
});

// ---- (b) the champion-gate crowns v3 (NOT "tbd") --------------------

test('racing reconstruct: the champion-gate resolves v3 as the promoted champion ♚ (racing-final won + champion_lineage), NOT "tbd"', () => {
  const st = STRUCT.reconstructRacing(RACING_TOURNAMENTS, RC_EPOCH);
  const gate = st.rounds.find((r) => String(r.matches[0].match_id) === 'racing-final');
  assert(gate, 'a racing-final champion-gate round was reconstructed');
  const gm = gate.matches[0];
  assertEqual(gm.winner, 'v3', 'the gate winner is the promoted survivor v3');
  assertEqual(gm.decision, 'promoted', 'the gate decision is promoted (racing-final won, Δ negative)');
  assertDeep([...gm.competitors].sort(), ['v0', 'v3'], 'the gate pits the champion v0 against the survivor v3');
  assertEqual(st.champion_lineage[st.champion_lineage.length - 1], 'v3', 'champion_lineage confirms v3 is the new champion');

  // and the rendered ladder shows v3 crowned ♚ — never "tbd".
  let navTo = null;
  const ctx = { navigate: (v, p) => { navTo = { v, p }; }, href: router.href };
  const nodes = STRUCT.renderStructure(st, ctx, RC_EPOCH);
  const wrap = document.createElement('div');
  for (const n of nodes) wrap.appendChild(n);
  const ladder = svgsByClass(wrap, 'dn-raceladder')[0];
  assert(ladder, 'the racing ladder rendered from the reconstruction');
  assert(ladder.textContent.includes('champion-gate'), 'a champion-gate column rendered');
  assert(ladder.textContent.includes('♚ v3'), 'the gate crowns v3 as the new champion ♚');
  assert(!ladder.textContent.includes('tbd'), 'the gate is NOT the empty "tbd" skeleton');
  assert(wrap.textContent.includes('v3 promoted'), 'the caption states the champion-gate outcome (v3 promoted)');
});

// ---- (c) competitors are clickable to their candidate ---------------

test('racing reconstruct: each competitor in the ladder is clickable → its candidate page', () => {
  const st = STRUCT.reconstructRacing(RACING_TOURNAMENTS, RC_EPOCH);
  let navTo = null;
  const ctx = { navigate: (v, p) => { navTo = { v, p }; }, href: router.href };
  const nodes = STRUCT.renderStructure(st, ctx, RC_EPOCH);
  const wrap = document.createElement('div');
  for (const n of nodes) wrap.appendChild(n);
  const runner = allByClass(wrap, 'dn-raceladder-runner')[0];
  assert(runner, 'a clickable competitor row exists');
  runner.dispatchEvent({ type: 'click' });
  assert(navTo && navTo.v === 'candidate' && navTo.p.epochId === RC_EPOCH, 'clicking a competitor routes to its candidate page');
  assert(/^v\d+$/.test(navTo.p.gen), 'the navigation carries the competitor generation id');
});

// ---- (d) the LIVE path still renders an in-progress ladder ----------

test('racing reconstruct: the LIVE /api/active-tournament path still renders the in-progress ladder with a pending rung + a "deciding…" gate', async () => {
  freshState();
  const F = {
    '/api/epoch': { epoch_id: RC_EPOCH, closed: false, goal: 'g', tournament: { structure: 'racing', params: LIVE_RACING.structure_params }, experiments: [], board: [] },
    '/api/lineage': { generations: LIVE_RACING.competitors.map((c) => ({ generation_id: c.generation_id, epoch_id: RC_EPOCH, parent_generation_id: c.role === 'champion' ? '' : 'v0', promoted: false })) },
    '/api/score-trajectory': { points: [] },
    // the COMPLETED record is empty — only the LIVE active-tournament has the topology.
    '/api/tournaments': { epoch_id: RC_EPOCH, structure: 'racing', champion_lineage: [], matchups: [], tournaments: [] },
    '/api/active-tournament': LIVE_RACING,
  };
  installFixtureMap(F);
  coreState.state.setHeartbeat({ phase: 'tournament:round_1:rung1_m0', generation_id: 'v1' });
  coreState.state.activeRuns = [{ generation_id: 'v1', entry_id: 'b0', run_id: 'r1', progress: 0.5 }];
  coreState.state.activeTournament = { structure: 'racing', phase: 'running' };

  const gens = await import('../js/variants/T/views/gens.js');
  const host = document.createElement('div');
  await gens.render(host, { navigate() {}, href: router.href }, { epochId: RC_EPOCH });

  const ladder = svgsByClass(host, 'dn-raceladder')[0];
  assert(ladder, 'the LIVE racing ladder rendered from /api/active-tournament');
  assert(allByClass(host, 'dt-live-pill')[0], 'a LIVE badge marks the in-flight tournament');
  assert(host.textContent.includes('Rung 1') && host.textContent.includes('Rung 2'), 'both rungs render (incl. the still-racing one)');
  // the not-yet-decided rung stays neutral (nobody struck) and the gate reads "deciding…".
  const struck = allByClass(host, 'dn-out');
  for (const n of struck) assert((n.textContent || '').indexOf('v1') < 0, 'the leader v1 is never struck (cut) mid-run');
  assert(ladder.textContent.includes('deciding'), 'the live champion-gate reads "deciding…" — no premature crown');
  assert(!ladder.textContent.includes('♚'), 'no champion is crowned ♚ while the race is live');

  coreState.state.heartbeat = { phase: 'idle' };
  coreState.state.activeRuns = [];
  coreState.state.activeTournament = null;
});

// ---- (e) the match-ups page reconstructs the ladder end-to-end ------

test('racing reconstruct: the match-ups page rebuilds the full ladder from the per-challenger /api/tournaments records (not an empty skeleton)', async () => {
  freshState();
  const F = {
    '/api/epoch': { epoch_id: RC_EPOCH, closed: true, goal: 'g', tournament: { structure: 'racing', params: RACING_TOURNAMENTS.structure_params },
      experiments: ['v0', 'v1', 'v2', 'v3', 'v4'].map((g) => ({ generation_id: g, parent_generation_id: g === 'v0' ? '' : 'v0', outcome: { decision: g === 'v0' ? 'baseline' : (g === 'v3' ? 'promoted' : 'rejected') } })), board: [] },
    '/api/lineage': { generations: ['v0', 'v1', 'v2', 'v3', 'v4'].map((g) => ({ generation_id: g, epoch_id: RC_EPOCH, parent_generation_id: g === 'v0' ? '' : 'v0', promoted: g === 'v0' || g === 'v3' })) },
    '/api/score-trajectory': { points: [] },
    '/api/tournaments': RACING_TOURNAMENTS,
  };
  installFixtureMap(F);
  // idle — no live run; the ladder must reconstruct from the completed records.
  coreState.state.heartbeat = { phase: 'idle' };
  coreState.state.activeRuns = [];
  coreState.state.activeTournament = null;

  const gens = await import('../js/variants/T/views/gens.js');
  const host = document.createElement('div');
  await gens.render(host, { navigate() {}, href: router.href }, { epochId: RC_EPOCH });

  const ladder = svgsByClass(host, 'dn-raceladder')[0];
  assert(ladder, 'the racing ladder rendered on the match-ups page from the per-challenger records');
  assert(!/No tournament|No rungs|unavailable/i.test(host.textContent), 'NOT the empty "RUNG · RUNG · CHAMPION-GATE: tbd" skeleton');
  assert(host.textContent.includes('Rung 0') && host.textContent.includes('Rung 1'), 'both reconstructed rungs render as columns');
  // the full rung0 field + the cut/survivor marks made it through to the SVG.
  for (const id of ['v1', 'v2', 'v3', 'v4']) assert(ladder.textContent.includes(id), 'rung0 names the full field — ' + id);
  assert(ladder.textContent.includes('✕'), 'cut runners are struck ✕');
  assert(ladder.textContent.includes('↑'), 'survivors are marked ↑');
  assert(ladder.textContent.includes('♚ v3'), 'the champion-gate crowns v3 as the new champion ♚ (not tbd)');
  assert(allByClass(host, 'dt-live-pill').length === 0, 'idle reconstruction carries NO live badge');
});

// ---- (d) current-vs-former champion badge in the tree ---------------

test('tree champion badge: with champion_lineage ["v0","v3"], ONLY v3 is the current "champion"; v0 is a "former champion"', () => {
  const host = document.createElement('div');
  // the model the shell assembles: v0 + v3 both promoted, but the lineage's
  // LAST id (v3) is the current crown.
  const model = {
    epochs: [{ id: EPOCH_ID, current: true }],
    byEpoch: { [EPOCH_ID]: {
      gens: [
        { id: 'v0', promoted: true, parent: null, currentChampion: false, formerChampion: true },
        { id: 'v1', promoted: false, parent: 'v0', currentChampion: false, formerChampion: false },
        { id: 'v3', promoted: true, parent: 'v0', currentChampion: true, formerChampion: false },
      ],
      boards: [],
    } },
  };
  const toggles = new Set(['e:' + EPOCH_ID, 'e:' + EPOCH_ID + '/gens']);
  tree.buildTree(host, model, router.parseRoute(`#/e/${EPOCH_ID}`), toggles, { navigate() {}, href: router.href }, () => {});

  // exactly ONE current champion (v3) carries the gen-champ badge…
  const champs = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('dt-leaf') && n.getAttribute('data-kind') === 'gen-champ');
  assertEqual(champs.length, 1, 'exactly one CURRENT champion badge (v3)');
  assert((champs[0].textContent || '').includes('v3'), 'the current champion is v3');
  assert((champs[0].textContent || '').includes('champion') && !(champs[0].textContent || '').includes('former'), 'v3 reads "champion" (not "former")');
  // …and the FORMER champion (v0) carries the distinct former-champion marker.
  const formers = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('dt-leaf') && n.getAttribute('data-kind') === 'gen-former');
  assertEqual(formers.length, 1, 'exactly one FORMER champion marker (v0)');
  assert((formers[0].textContent || '').includes('v0') && (formers[0].textContent || '').includes('former'), 'v0 reads "former champion"');
  // a rejected branch is unchanged.
  assert(host.textContent.includes('rejected'), 'the rejected branch (v1) is unchanged');
});

test('tree champion badge: the shell tree model marks the current champion from champion_lineage (and the digest re-stamps when the crown moves)', () => {
  // two promoted gens, lineage crowns the LAST (v3); the digest must differ
  // from a model where v0 is the current crown (so the badge re-stamps).
  const cur3 = { epochs: [{ id: EPOCH_ID, current: true }], byEpoch: { [EPOCH_ID]: { gens: [
    { id: 'v0', promoted: true, parent: null, currentChampion: false, formerChampion: true },
    { id: 'v3', promoted: true, parent: 'v0', currentChampion: true, formerChampion: false },
  ], boards: [] } } };
  const cur0 = { epochs: [{ id: EPOCH_ID, current: true }], byEpoch: { [EPOCH_ID]: { gens: [
    { id: 'v0', promoted: true, parent: null, currentChampion: true, formerChampion: false },
    { id: 'v3', promoted: true, parent: 'v0', currentChampion: false, formerChampion: true },
  ], boards: [] } } };
  const route = router.parseRoute(`#/e/${EPOCH_ID}`);
  const toggles = new Set();
  assert(tree.treeDigest(cur3, route, toggles) !== tree.treeDigest(cur0, route, toggles),
    'the tree digest changes when the crown moves (v3 current vs v0 current) — the badge re-stamps');
});

test('tree champion badge: a legacy model with NO current/former split keeps the champion badge for a promoted generation (back-compat)', () => {
  const host = document.createElement('div');
  const model = { epochs: [{ id: EPOCH_ID, current: true }], byEpoch: { [EPOCH_ID]: {
    gens: [{ id: 'v0', promoted: true, parent: null }], boards: [],
  } } };
  const toggles = new Set(['e:' + EPOCH_ID, 'e:' + EPOCH_ID + '/gens']);
  tree.buildTree(host, model, router.parseRoute(`#/e/${EPOCH_ID}`), toggles, { navigate() {}, href: router.href }, () => {});
  assert(allByClass(host, 'dt-glyph-gen-champ').length >= 1, 'a legacy promoted generation still carries the champion glyph');
});

// ====================================================================
// WALKTHROUGH bug fixes:
//   BUG 1 — mutation-surface click semantics. A CELL (site × generation)
//     opens ONLY that one generation's side-by-side diff for the site; the
//     SITE row label opens ALL generations that patched the site, stacked.
//   BUG 2 — the tree reliably lists an existing epoch on EVERY route; the
//     "No epochs" empty state shows only when there are genuinely zero.
// ====================================================================

// ---- BUG 1 (a): the router carries the per-cell generation -----------

test('mutations route: a CELL pins mutId + gen; the SITE pins mutId only (round-trips)', () => {
  // a bare mutId → the SITE (all generations) selection.
  const site = router.parseRoute(`#/e/${EPOCH_ID}/mutations/oversight_policy`);
  assertEqual(site.view, 'mutations');
  assertEqual(site.params.mutId, 'oversight_policy');
  assert(!site.params.gen, 'a bare mutId carries NO generation (the all-gens SITE view)');
  // a mutId + gen → ONE site×generation CELL selection.
  const cell = router.parseRoute(`#/e/${EPOCH_ID}/mutations/oversight_policy/v2`);
  assertEqual(cell.params.mutId, 'oversight_policy');
  assertEqual(cell.params.gen, 'v2', 'the trailing segment is the pinned cell generation');
  // both hrefs round-trip.
  assertEqual(router.href('mutations', { epochId: EPOCH_ID, mutId: 'oversight_policy' }),
    `#/e/${EPOCH_ID}/mutations/oversight_policy`, 'the SITE href omits the gen');
  assertEqual(router.href('mutations', { epochId: EPOCH_ID, mutId: 'oversight_policy', gen: 'v2' }),
    `#/e/${EPOCH_ID}/mutations/oversight_policy/v2`, 'the CELL href appends the gen');
  // back/up: a cell steps up to the site (all-gens) view; the site steps to the epoch.
  assertEqual(router.up(cell).view, 'mutations');
  assertEqual(router.up(cell).params.gen, undefined, 'a cell steps up to the SITE (all gens) — gen dropped');
  assertEqual(router.up(cell).params.mutId, 'oversight_policy', 'the site view keeps the mutId');
  // the bare-mutId SITE view steps up to the mutation-surface root (mutId dropped).
  const upSite = router.up(site);
  assertEqual(upSite.view, 'mutations', 'the site steps up to the mutation-surface root');
  assert(!upSite.params.mutId, 'the mutation-surface root drops the mutId');
});

// ---- BUG 1 (a): clicking a CELL renders exactly ONE generation's diff ----

test('mutation surface: clicking a CELL renders exactly ONE generation’s side-by-side diff for that site (not all)', async () => {
  freshState(); installFetch();
  const mutations = await import('../js/variants/T/views/mutations.js');

  // oversight_policy was patched by BOTH v1 and v2 with DIFFERENT content.
  // Pin the v2 CELL → only v2's diff appears (not v1's).
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await mutations.render(host, ctx, { epochId: EPOCH_ID, mutId: 'oversight_policy', gen: 'v2' });

  const blocks = allByClass(host, 'dn-patch-block');
  assertEqual(blocks.length, 1, 'exactly ONE generation diff block for the pinned cell (v2 only — not v1+v2)');
  const sxs = allByClass(host, 'dn-sxs');
  assertEqual(sxs.length, 1, 'exactly one side-by-side diff component');
  // it is v2's content, with REAL strings (never the baseline object).
  assert(host.textContent.includes('Loosen coordinator oversight'), 'the v2 challenger new_content (the pinned cell) is shown');
  assert(!host.textContent.includes('Tighten coordinator oversight'), 'v1’s patch is NOT shown when only the v2 cell is pinned');
  assert(host.textContent.includes('Default oversight'), 'the champion baseline string (LEFT) is shown');
  assert(!host.textContent.includes('[object Object]'), 'never the baseline OBJECT');
  // the cells carry both identities so the click can pin one generation.
  const cells = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('dn-mtx-cell') && n.getAttribute('data-gen'));
  assert(cells.length >= 1, 'matrix cells carry data-gen + data-site (the cell’s generation + site identity)');
  assert(cells.every((c) => c.getAttribute('data-site')), 'every cell carries its data-site');
  // the cell link targets the mutId+gen route (one generation), not the bare site.
  const cellLink = allByClass(host, 'dn-mtx-celllink')[0];
  assert(cellLink && (cellLink.getAttribute('href') || '').endsWith('/mutations/coordinator_prompt/v1')
    || (cellLink.getAttribute('href') || '').includes('/mutations/'), 'a cell link routes to mutId/gen');
  const anyCellHrefHasGen = allByClass(host, 'dn-mtx-celllink')
    .some((a) => /\/mutations\/[^/]+\/v\d+$/.test(a.getAttribute('href') || ''));
  assert(anyCellHrefHasGen, 'at least one cell link carries the trailing /<gen> (one-generation affordance)');
});

// ---- BUG 1 (b): clicking the SITE row label renders ALL generations ----

test('mutation surface: clicking the SITE row label renders ALL generations that patched the site, stacked', async () => {
  freshState(); installFetch();
  const mutations = await import('../js/variants/T/views/mutations.js');

  // pin the SITE (no gen) → BOTH v1 and v2 diffs for oversight_policy stack.
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await mutations.render(host, ctx, { epochId: EPOCH_ID, mutId: 'oversight_policy' });

  const blocks = allByClass(host, 'dn-patch-block');
  assertEqual(blocks.length, 2, 'BOTH generations (v1, v2) that patched the site are stacked');
  const sxs = allByClass(host, 'dn-sxs');
  assertEqual(sxs.length, 2, 'two side-by-side diff components (one per generation)');
  assert(host.textContent.includes('Tighten coordinator oversight'), 'v1’s patch is shown in the all-gens view');
  assert(host.textContent.includes('Loosen coordinator oversight'), 'v2’s patch is shown in the all-gens view');
  assert(host.textContent.includes('Default oversight'), 'the champion baseline string (LEFT) is shown');
  assert(!host.textContent.includes('[object Object]'), 'never the baseline OBJECT');

  // the SITE row label is the all-gens affordance — it links to the BARE mutId
  // (no trailing gen), distinct from the per-cell links.
  const siteLink = allByClass(host, 'dn-mtx-sitelink')[0];
  assert(siteLink, 'the site row label is a link');
  const siteHref = siteLink.getAttribute('href') || '';
  assert(/\/mutations\/[^/]+$/.test(siteHref), 'the site link routes to the BARE mutId (all generations) — no trailing gen');
});

// ---- BUG 2: the tree lists an existing epoch on every route ----------

test('tree (BUG 2): /api/lineage generations across an epoch make the tree LIST that epoch — never "No epochs"', async () => {
  freshState();
  // A workspace feed with NO epochs roster (the sparse-route case that produced
  // the bug), an /api/epoch that 404s, but /api/lineage plainly carries the
  // epoch's generations grouped by epoch_id. The tree must STILL list the epoch.
  const PUB_EPOCH = '2026-06-01_e0';
  const F = {
    '/api/workspace': { current_epoch_id: null, sparkline: [] },   // no `epochs` array
    '/api/lineage': { generations: [
      { generation_id: 'v0', epoch_id: PUB_EPOCH, parent_generation_id: '', promoted: true },
      { generation_id: 'v1', epoch_id: PUB_EPOCH, parent_generation_id: 'v0', promoted: false },
    ] },
    '/api/tournaments': { epoch_id: PUB_EPOCH, champion_lineage: ['v0'], matchups: [] },
    // NB: /api/epoch is deliberately absent → 404 (the publication-route case).
  };
  globalThis.fetch = async (path) => (Object.prototype.hasOwnProperty.call(F, path)
    ? { ok: true, json: async () => F[path] }
    : { ok: false, status: 404, json: async () => ({ error: 'not found: ' + path }) });

  // mount the live shell on the PUBLICATION route for this epoch.
  const root = mountLiveShell(`#/e/${PUB_EPOCH}/paper`);
  await new Promise((r) => setTimeout(r, 0));
  await new Promise((r) => setTimeout(r, 0));
  await new Promise((r) => setTimeout(r, 0));

  const rail = allByClass(root, 'dt-sidebar')[0];
  assert(rail, 'the rail mounted');
  assert(!rail.textContent.includes('No epochs in this workspace yet'),
    'the tree does NOT show the empty state when /api/lineage carries the epoch');
  assert(rail.textContent.includes(PUB_EPOCH), 'the tree LISTS the existing epoch (from /api/lineage, on the publication route)');
  // and the epoch's generations resolve into its bundle.
  const epochs2 = [{ id: PUB_EPOCH, current: true }];
  assert(epochs2.length === 1, 'fixture sanity');
});

test('tree (BUG 2): the "No epochs" empty state shows ONLY when there are genuinely zero epochs', async () => {
  freshState();
  // every authoritative source is empty: no workspace epochs, no lineage gens,
  // no /api/epoch, and the route is the bare environment root (no routed epoch).
  const F = {
    '/api/workspace': { current_epoch_id: null, epochs: [], sparkline: [] },
    '/api/lineage': { generations: [] },
    '/api/tournaments': { matchups: [] },
  };
  globalThis.fetch = async (path) => (Object.prototype.hasOwnProperty.call(F, path)
    ? { ok: true, json: async () => F[path] }
    : { ok: false, status: 404, json: async () => ({ error: 'not found: ' + path }) });

  const root = mountLiveShell('#/');
  await new Promise((r) => setTimeout(r, 0));
  await new Promise((r) => setTimeout(r, 0));
  await new Promise((r) => setTimeout(r, 0));

  const rail = allByClass(root, 'dt-sidebar')[0];
  assert(rail, 'the rail mounted');
  assert(rail.textContent.includes('No epochs in this workspace yet'),
    'with genuinely zero epochs the empty state IS shown');
});

// ====================================================================
// Lifecycle DAG · BOARD column: dedupe per ENTRY (rung multiplicity),
// and label/circle text-spacing. A RACING candidate re-runs the SAME
// board entry across rungs (rung0 slice → rung1 → racing-final full
// board), so the raw per-entry stream repeats an entry_id N times.
// ====================================================================

// collect the BOARD-column node groups of a freshly built lifecycle DAG.
function boardNodesOf(svgNode) {
  return svgNode.querySelectorAll('[class]').filter((n) =>
    n.localName === 'g' && (n.getAttribute('class') || '').split(/\s+/).includes('ezn-board-node'));
}
function childByClass(g, cls) {
  return g.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').split(/\s+/).includes(cls))[0] || null;
}

test('lifecycle BOARD column: a RACING candidate dedupes to ONE node per distinct entry (count == distinct entries, not total runs) + annotates rung multiplicity', () => {
  // v3: a racing candidate. The SAME entries recur across rungs:
  //   q3_metrics_outline ×3, waffles_single ×2, picky_stakeholder_emulated (once).
  // The last run per entry is the racing-final / full-board run (representative).
  const entries = [
    { entry_id: 'q3_metrics_outline', run_id: 'r0a', drift_loss: 90.0, pass_fail: 0, wall_clock_budget_exceeded: false },
    { entry_id: 'q3_metrics_outline', run_id: 'r1a', drift_loss: 85.0, pass_fail: 0, wall_clock_budget_exceeded: false },
    { entry_id: 'q3_metrics_outline', run_id: 'r2a', drift_loss: 80.0, pass_fail: 1, wall_clock_budget_exceeded: false },
    { entry_id: 'waffles_single', run_id: 'r0b', drift_loss: 60.0, pass_fail: 0, wall_clock_budget_exceeded: false },
    { entry_id: 'waffles_single', run_id: 'r1b', drift_loss: 61.0, pass_fail: 0, wall_clock_budget_exceeded: true },
    { entry_id: 'picky_stakeholder_emulated', run_id: 'r0c', drift_loss: 105.0, pass_fail: 0, wall_clock_budget_exceeded: false },
  ];
  const distinct = new Set(entries.map((e) => e.entry_id));
  const svgNode = dag.lifecycleDag({ genId: 'v3', parentId: 'v0', entries, decision: 'rejected', height: 360 });
  const nodes = boardNodesOf(svgNode);

  assertEqual(nodes.length, distinct.size, 'ONE board node per DISTINCT entry (3), not one per run (6)');
  const keys = nodes.map((n) => n.getAttribute('data-key')).sort();
  assertDeep(keys, ['picky_stakeholder_emulated', 'q3_metrics_outline', 'waffles_single'], 'each distinct entry appears exactly once');

  // multiplicity: q3 (×3) and waffles (×2) carry a badge; picky (×1) does not.
  const byKey = {}; for (const n of nodes) byKey[n.getAttribute('data-key')] = n;
  assertEqual(byKey['q3_metrics_outline'].getAttribute('data-mult'), '3', 'q3 ran across 3 rungs');
  assertEqual(byKey['waffles_single'].getAttribute('data-mult'), '2', 'waffles ran across 2 rungs');
  assertEqual(byKey['picky_stakeholder_emulated'].getAttribute('data-mult'), '1', 'picky ran once');

  const q3mult = childByClass(byKey['q3_metrics_outline'], 'ezn-board-mult');
  assert(q3mult && q3mult.textContent === '×3', 'a re-raced entry carries a "×N rungs" multiplicity badge (×3)');
  assert(childByClass(byKey['waffles_single'], 'ezn-board-mult'), 'waffles (raced ×2) carries a multiplicity badge');
  assert(!childByClass(byKey['picky_stakeholder_emulated'], 'ezn-board-mult'), 'a once-run entry carries NO multiplicity badge');

  // representative loss = the LAST (racing-final / full-board) run, NOT rung0.
  const q3loss = childByClass(byKey['q3_metrics_outline'], 'ezn-board-loss');
  assertEqual(q3loss.textContent, svg.fmt(80.0, 0), 'the node shows the representative (final full-board) loss, not the rung0 loss');

  // the raced nodes carry the marker class so the disc renders distinctly.
  assert((byKey['q3_metrics_outline'].getAttribute('class') || '').includes('ezn-board-raced'), 'a re-raced node is marked ezn-board-raced');
  assert(!(byKey['picky_stakeholder_emulated'].getAttribute('class') || '').includes('ezn-board-raced'), 'a once-run node is NOT marked raced');
});

test('lifecycle BOARD column: labels never overlap the loss disc + rows are vertically spaced', () => {
  const entries = [
    { entry_id: 'q3_metrics_outline', drift_loss: 80, pass_fail: 0 },
    { entry_id: 'q3_metrics_outline', drift_loss: 80, pass_fail: 0 },
    { entry_id: 'waffles_single', drift_loss: 60, pass_fail: 0 },
    { entry_id: 'picky_stakeholder_emulated', drift_loss: 105, pass_fail: 0 },
    { entry_id: 'every_expectation_kind', drift_loss: 40, pass_fail: 1 },
  ];
  const h = 360;
  const svgNode = dag.lifecycleDag({ genId: 'v3', parentId: 'v0', entries, decision: 'rejected', height: h });
  const nodes = boardNodesOf(svgNode);

  // (b) the entry label is offset from the disc — anchored at its END and placed
  // to the LEFT of the circle (x < disc cx) — so a label can NEVER sit on the disc.
  for (const g of nodes) {
    const disc = childByClass(g, 'ezn-board-disc');
    const label = childByClass(g, 'ezn-board-label');
    const cx = +disc.getAttribute('cx');
    const r = +disc.getAttribute('r');
    const lx = +label.getAttribute('x');
    assertEqual(label.getAttribute('text-anchor'), 'end', 'the label is end-anchored (grows leftward, away from the disc)');
    assert(lx <= cx - r, `the label x (${lx}) is left of the disc’s left edge (${cx - r}) — no overlap with the circle`);
    // long ids are clipped with an ellipsis so the label never runs into the disc.
    assert(label.textContent.length <= 18, 'a long entry id is truncated for the node label');
  }

  // adjacent rows are spaced by a comfortable vertical gap (legible, no overlap).
  const cys = nodes.map((g) => +childByClass(g, 'ezn-board-disc').getAttribute('cy')).sort((a, b) => a - b);
  for (let i = 1; i < cys.length; i++) {
    assert(cys[i] - cys[i - 1] >= 24, `row gap (${cys[i] - cys[i - 1]}) is at least a 24px minimum so labels never collide`);
  }
});

test('lifecycle BOARD column: a GAUNTLET candidate (one run per entry) renders unchanged — one node per entry, NO spurious multiplicity badge', () => {
  // the gauntlet path: each board entry is run exactly once. Dedupe is a no-op.
  const entries = [
    { entry_id: 'waffles_single', run_id: 'g1', drift_loss: 60.5, pass_fail: 0, wall_clock_budget_exceeded: false },
    { entry_id: 'picky_stakeholder_emulated', run_id: 'g2', drift_loss: 105.5, pass_fail: 0, wall_clock_budget_exceeded: true },
  ];
  const svgNode = dag.lifecycleDag({ genId: 'v1', parentId: 'v0', entries, decision: 'rejected', height: 360 });
  const nodes = boardNodesOf(svgNode);
  assertEqual(nodes.length, entries.length, 'one node per entry (gauntlet: dedupe is a no-op)');
  for (const g of nodes) {
    assertEqual(g.getAttribute('data-mult'), '1', 'each gauntlet entry has multiplicity 1');
    assert(!childByClass(g, 'ezn-board-mult'), 'no spurious multiplicity badge on a gauntlet node');
    assert(!(g.getAttribute('class') || '').includes('ezn-board-raced'), 'a gauntlet node is not marked raced');
  }
});

test('lifecycle BOARD column: the multiplicity badge style + raced disc marker are themed in the scoped stylesheet', () => {
  const css = readCss();
  assert(/\.ezn-board-mult\s*\{/.test(css), '.ezn-board-mult is styled (themed via CSS vars)');
  assert(/\.ezn-board-mult[^}]*var\(--v2-/.test(css), '.ezn-board-mult uses a theme variable (theme-aware across the 13 themes)');
  assert(/\.ezn-board-raced\s+\.ezn-board-disc\s*\{/.test(css), 'a raced node’s disc carries a distinct marker style');
});

await run();
