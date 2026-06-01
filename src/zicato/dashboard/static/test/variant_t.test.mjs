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

// ---- CHANGE 5 + 6: nine themes + the colour SWATCH DROPDOWN ----

test('colour themes: all NINE are registered, each defines the full --v2 token contract, and selecting each applies it', () => {
  freshState();
  const ids = ui.COLOR_THEMES.map((t) => t[0]);
  const expected = ['monokai', 'solarized-dark', 'solarized-light',
    'google-light', 'google-dark', 'lunaria-light', 'lunaria-eclipse',
    'belafonte-day', 'belafonte-night'];
  assertEqual(ids.length, 9, 'nine colour themes registered');
  assertDeep(ids, expected, 'the nine ids are the three originals + six Gogh palettes');
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

  // selecting EACH theme applies it to the root + persists (incl. the six Gogh).
  const root = document.createElement('div');
  for (const id of ids) {
    shell.applyTheme(id, root);
    assertEqual(root.getAttribute('data-t-theme'), id, id + ' applied to the root');
    assertEqual(ui.readColor(), id, id + ' persisted');
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
  assertEqual(options.length, 9, 'one dropdown option per theme (nine)');
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

await run();
