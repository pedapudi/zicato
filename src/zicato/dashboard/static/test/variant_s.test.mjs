// test/variant_s.test.mjs — Variant S ("Lens") unit tests.
//
// S is the comparison-first convergence-III dashboard: a data-model TREE
// sidebar + a detail pane built around FIRST-CLASS side-by-side comparison
// (solarized-light + Editorial default). These pin the headline + the seven
// mandatory fixes:
//   * the tree sidebar renders the hierarchy AND navigates multiple gens/epochs;
//   * the board view shows two candidates' transcripts side by side INLINE on a
//     run select (no separate-page navigation) — the signature (fix #5);
//   * a "compare" mode splits the candidate detail into two candidates;
//   * the promote gate is on the candidate page, stacked, no overlap (fix #1);
//   * the patch node → this candidate's side-by-side diff, REAL strings (fix #2);
//   * ALL match-ups for a candidate, v0 ≥ 2 (fix #3);
//   * the trellis lives in the Boards view, NOT the epoch overview (fix #6);
//   * the colour + typeface pickers + theme pills switch + persist;
//   * digest-gated repaint is a true no-op.

import { installDom, test, run, assert, assertEqual } from './harness.mjs';

installDom();

const router = await import('../js/variants/S/router.js');
const svg = await import('../js/variants/S/svg.js');
const ui = await import('../js/variants/S/ui.js');
const shell = await import('../js/variants/S/shell.js');
const data = await import('../js/variants/S/data.js');

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
  globalThis.window.location = { hash: '', search: '' };
}
function allByClass(host, cls) {
  return host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').split(/\s+/).includes(cls));
}
const CTX = (sink) => ({ navigate: (v, p, o) => { if (sink) sink.last = { v, p, o }; }, href: router.href });

// ---- router ---------------------------------------------------------

test('router: env default; node paths + comparison suffix parse', () => {
  assertEqual(router.parseRoute('').view, 'env');
  assertEqual(router.parseRoute('#/S/').view, 'env');
  assertEqual(router.parseRoute('#/bogus').view, 'env');
  const c = router.parseRoute('#/S/e/E0/gen/v1~cmp=v2');
  assertEqual(c.view, 'candidate'); assertEqual(c.params.gen, 'v1'); assertEqual(c.cmp, 'v2');
  const patch = router.parseRoute('#/S/e/E0/gen/v1/patch');
  assertEqual(patch.params.sub, 'patch');
  const b = router.parseRoute('#/S/e/E0/board/waffles_single~runs=v0,v1');
  assertEqual(b.view, 'board'); assert(b.runs[0] === 'v0' && b.runs[1] === 'v1', 'two runs parsed');
  assertEqual(router.parseRoute('#/S/e/E0/mut/coordinator_prompt').params.mutId, 'coordinator_prompt');
  assertEqual(router.parseRoute('#/S/e/E0/pub').view, 'publication');
});

// ---- HEADLINE: the data-model tree sidebar -------------------------

test('tree sidebar: renders the hierarchy AND navigates multiple gens', async () => {
  freshState(); installFetch();
  const tree = await import('../js/variants/S/tree.js');
  const m = await tree.buildModel();
  assert(m.epochs.length >= 1, 'at least one epoch resolved');
  assert(m.gens.length === 3 && m.championId === 'v0', 'three generations, v0 champion');

  const host = document.createElement('div');
  const sink = {};
  // open the current epoch + its Generations group so every gen renders.
  window.localStorage.setItem('zicato.S.tree.open', JSON.stringify(['e:' + EPOCH_ID, 'e:' + EPOCH_ID + ':gens', 'e:' + EPOCH_ID + ':boards']));
  const route = router.parseRoute('#/S/e/' + EPOCH_ID + '/gen/v1');
  tree.paintTree(host, m, route, (v, p) => { sink.last = { v, p }; });

  // the four structural groups of the data model are present.
  assert(host.textContent.includes('Environment'), 'Environment root');
  assert(host.textContent.includes('Generations'), 'Generations group');
  assert(host.textContent.includes('Boards'), 'Boards group');
  assert(host.textContent.includes('Mutation surface'), 'Mutation surface leaf');
  assert(host.textContent.includes('Publication'), 'Publication leaf');
  // EVERY generation is its own selectable node (multi-generation nav).
  const labels = allByClass(host, 'vs-ttext').map((n) => n.textContent);
  assert(labels.includes('v0') && labels.includes('v1') && labels.includes('v2'), 'all three generations are tree nodes');
  // a generation node click navigates to that candidate (different gen than current).
  const genLinks = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('vs-tlabel'));
  let v2Link = null;
  for (const ln of genLinks) if ((ln.textContent || '').includes('v2')) v2Link = ln;
  assert(v2Link, 'a v2 node exists');
  v2Link.dispatchEvent({ type: 'click', preventDefault() {}, stopPropagation() {} });
  assert(sink.last && sink.last.v === 'candidate' && sink.last.p.gen === 'v2', 'clicking v2 navigates to candidate v2 (multi-gen nav)');
});

// ---- candidate: promote gate (fix #1) + patch diff (fix #2) + matchups (fix #3) ----

test('candidate: stacked promote gate on the candidate page (fix #1)', async () => {
  freshState(); installFetch();
  const candidate = await import('../js/variants/S/views/candidate.js');
  const host = document.createElement('div');
  await candidate.render(host, CTX(), router.parseRoute('#/S/e/' + EPOCH_ID + '/gen/v1'));
  const gate = allByClass(host, 'dn-gate')[0];
  assert(gate, 'a promote-gate panel rendered ON the candidate page');
  const rules = allByClass(host, 'dn-rule');
  assert(rules.length >= 3, 'each gate rule is its own row (3 short-circuiting rules)');
  assert(allByClass(host, 'dn-sc-table').length >= 1, 'a separate champion-vs-challenger scalar-components block');
  assert(host.textContent.includes('Scalar margin'), 'a rule label present');
});

test('candidate patch node → this candidate’s side-by-side diff, REAL strings (fix #2)', async () => {
  freshState(); installFetch();
  const candidate = await import('../js/variants/S/views/candidate.js');
  const host = document.createElement('div');
  await candidate.render(host, CTX(), router.parseRoute('#/S/e/' + EPOCH_ID + '/gen/v1/patch'));
  const sxs = allByClass(host, 'dn-sxs')[0];
  assert(sxs, 'the side-by-side diff filled when the patch node is open');
  assert(host.textContent.includes('Always emit an explicit slide structure'), 'challenger new_content on the right');
  assert(host.textContent.includes('Draft an outline'), 'baseline.content on the left');
  assert(!host.textContent.includes('[object Object]'), 'never the baseline OBJECT');
});

test('candidate: ALL match-ups for the candidate (fix #3 — v0 shows ≥2)', async () => {
  freshState(); installFetch();
  const candidate = await import('../js/variants/S/views/candidate.js');
  const host = document.createElement('div');
  await candidate.render(host, CTX(), router.parseRoute('#/S/e/' + EPOCH_ID + '/gen/v0'));
  // v0 was champion in v0→v1 AND v0→v2 — both rows present in the match-ups
  // section (fix #3: every round the candidate was in, not just one).
  assert(host.textContent.includes('v0') && host.textContent.includes('v1') && host.textContent.includes('v2'), 'all candidates referenced');
  assert(host.textContent.split('v1').length - 1 >= 1 && host.textContent.split('v2').length - 1 >= 1, 'both v0-vs-v1 and v0-vs-v2 present');
});

// ---- candidate: COMPARE mode splits the detail into two candidates --

test('candidate: a "compare" target splits the detail into two candidates', async () => {
  freshState(); installFetch();
  const candidate = await import('../js/variants/S/views/candidate.js');
  const host = document.createElement('div');
  await candidate.render(host, CTX(), router.parseRoute('#/S/e/' + EPOCH_ID + '/gen/v1~cmp=v2'));
  const sides = allByClass(host, 'vs-split-side');
  assert(sides.length === 2, 'two comparison sides');
  // side A and side B carry the A / B tags.
  const tags = allByClass(host, 'vs-split-tag').map((n) => n.textContent);
  assert(tags.includes('A') && tags.includes('B'), 'A and B side tags');
  // both candidates appear in the split heads.
  assert(host.textContent.includes('v1') && host.textContent.includes('v2'), 'both compared candidates rendered');
  // the compare picker is present and offers the other candidates.
  assert(allByClass(host, 'vs-cmp-select').length >= 1, 'a compare-with picker');
});

// ---- board: INLINE side-by-side transcripts (fix #5 — the signature) ---

test('board view: two candidates’ transcripts side by side INLINE on run select (fix #5)', async () => {
  freshState(); installFetch();
  const board = await import('../js/variants/S/views/board.js');
  const host = document.createElement('div');
  const sink = {};
  // selecting v0 vs v1 runs on this board.
  await board.render(host, CTX(sink), router.parseRoute('#/S/e/' + EPOCH_ID + '/board/waffles_single~runs=v0,v1'));
  assert(host.textContent.includes('Board · waffles_single'), 'the per-board heading');
  // TWO transcript scrollers, INLINE in the board view (no nav-away).
  const scrollers = allByClass(host, 'vs-transcript');
  assert(scrollers.length === 2, 'two inline, independently-scrollable transcript columns');
  // both candidates' transcript text rendered side by side.
  assert(host.textContent.includes('Champion draft with a clear structure'), 'side A (v0 champion) transcript');
  assert(host.textContent.includes('Challenger drafting an outline now'), 'side B (v1 challenger) transcript');
  assert(host.textContent.includes('omitted the requested structure'), 'a drift annotation rendered inline');
  // selecting a run does NOT navigate to a separate run page — it sets `runs`.
  const setRun = allByClass(host, 'dn-board-run')[0];
  assert(setRun, 'a transcript-select control exists');
  setRun.dispatchEvent({ type: 'click', preventDefault() {} });
  assert(sink.last && sink.last.v === 'board', 'selecting a run stays on the board view (no separate run page)');
  assert(sink.last && sink.last.o && Array.isArray(sink.last.o.runs), 'it sets the runs comparison target, not a run route');
});

// ---- FIX #6: trellis in the Boards view, NOT the epoch overview ----

test('trellis lives in the Boards view; the epoch overview has the heatmap, no trellis (fix #6)', async () => {
  freshState(); installFetch();
  const epoch = await import('../js/variants/S/views/epoch.js');
  const board = await import('../js/variants/S/views/board.js');
  const ehost = document.createElement('div');
  await epoch.render(ehost, CTX(), router.parseRoute('#/S/e/' + EPOCH_ID));
  assert(allByClass(ehost, 'dn-heatmap')[0], 'epoch overview HAS the heatmap');
  assert(allByClass(ehost, 'dn-trellis').length === 0, 'epoch overview has NO trellis (de-dup)');

  const bhost = document.createElement('div');
  await board.render(bhost, CTX(), router.parseRoute('#/S/e/' + EPOCH_ID + '/board/waffles_single'));
  assert(allByClass(bhost, 'dn-trellis')[0], 'the trellis lives in the Boards view');
  assert(allByClass(bhost, 'dn-heatmap').length === 0, 'the board view has no heatmap');
});

test('epoch heatmap cell routes to the per-board view (by entry id)', async () => {
  freshState(); installFetch();
  const epoch = await import('../js/variants/S/views/epoch.js');
  const host = document.createElement('div');
  const sink = {};
  await epoch.render(host, CTX(sink), router.parseRoute('#/S/e/' + EPOCH_ID));
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

// ---- pickers + pills + theme defaults ------------------------------

test('pickers + pills: solarized-light + Editorial defaults switch + persist', () => {
  freshState();
  const root = document.createElement('div');
  assertEqual(ui.DEFAULT_COLOR, 'solarized-light', 'solarized-light is the default colour theme');
  assertEqual(ui.DEFAULT_TYPE, 'editorial', 'Editorial is the default typeface');
  const typeIds = ui.TYPE_THEMES.map((t) => t[0]);
  assert(['sans', 'editorial', 'technical', 'display'].every((t) => typeIds.includes(t)), 'all four typefaces offered');
  const colorIds = ui.COLOR_THEMES.map((t) => t[0]);
  assert(['solarized-light', 'solarized-dark', 'monokai'].every((t) => colorIds.includes(t)), 'all three colour themes offered');
  shell.applyTheme('monokai', root);
  assertEqual(root.getAttribute('data-s-theme'), 'monokai', 'colour applied to root');
  assertEqual(ui.readColor(), 'monokai', 'colour persisted');
  shell.applyTypeface('technical', root);
  assertEqual(root.getAttribute('data-s-type'), 'technical', 'typeface applied to root');
  assertEqual(ui.readType(), 'technical', 'typeface persisted');
  assertEqual(ui.normaliseColor('nonsense'), 'solarized-light', 'unknown colour → solarized-light');
  assertEqual(ui.normaliseType('nonsense'), 'editorial', 'unknown typeface → editorial');
  // the verdict pill is themed.
  const pill = ui.verdictPill('rejected');
  assert((pill.getAttribute('class') || '').includes('dn-rejected'), 'verdict pill carries its decision class');
});

// ---- digest-gated repaint (no-op) ----------------------------------

test('candidate view: digest-gated — identical data does NOT rebuild the DOM', async () => {
  freshState(); installFetch();
  const candidate = await import('../js/variants/S/views/candidate.js');
  const host = document.createElement('div');
  await candidate.render(host, CTX(), router.parseRoute('#/S/e/' + EPOCH_ID + '/gen/v1'));
  const digest1 = host.getAttribute('data-s-digest');
  const first = host.firstChild;
  const writes1 = host.innerHTMLWriteCount();
  assert(host.children.length > 0, 'candidate painted');
  await candidate.render(host, CTX(), router.parseRoute('#/S/e/' + EPOCH_ID + '/gen/v1'));
  assertEqual(host.getAttribute('data-s-digest'), digest1, 'digest unchanged');
  assert(host.firstChild === first, 'no clear-and-rebuild on the no-op repaint');
  assertEqual(host.innerHTMLWriteCount(), writes1, 'no innerHTML writes on the no-op repaint');
});

// ---- cold deep-link hydration --------------------------------------

test('cold deep-link: a board runs= deep-link hydrates both transcripts', async () => {
  freshState(); installFetch();
  const board = await import('../js/variants/S/views/board.js');
  const host = document.createElement('div');
  await board.render(host, CTX(), router.parseRoute('#/S/e/' + EPOCH_ID + '/board/waffles_single~runs=v0,v1'));
  assert(allByClass(host, 'vs-transcript').length === 2, 'both transcript columns hydrate from a cold deep-link');
});

await run();
