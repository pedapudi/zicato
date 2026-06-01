// test/variant_w.test.mjs — Variant W ("Arena") unit tests.
//
// W is the convergence-IV CREATIVE broadcast take: the tournament as live
// STANDINGS + MATCH CARDS, with the Console III tree sidebar, S's side-by-side
// comparison detail, and a FIXED back/up control. These pin the headline + the
// carried-forward fixes:
//   * the standings render the champion (defending) + a match card per
//     challenger with a verdict pill and a Δscalar;
//   * clicking a match card opens that challenger's lifecycle / gate;
//   * the tree sidebar renders the hierarchy AND navigates multiple gens;
//   * the promote gate is ON the candidate page (stacked, rules each its row);
//   * the patch node → this candidate's side-by-side diff, REAL strings;
//   * ALL match-ups for a candidate, v0 ≥ 2;
//   * the board view is first-class + INLINE side-by-side transcripts;
//   * the back/up control renders into the MAIN detail pane (NOT the sidebar);
//   * the standings are fit-to-width (no pan/zoom viewport);
//   * the colour + typeface pickers + theme pills switch + persist;
//   * digest-gated repaint is a true no-op.

import { installDom, test, run, assert, assertEqual } from './harness.mjs';

installDom();

const router = await import('../js/variants/W/router.js');
// W reuses P's leaf primitives + read layer (per the round-6 brief: "reuse P's
// views"); the suite exercises them through W's modules + the shared P modules.
const svg = await import('../js/variants/P/svg.js');
const ui = await import('../js/variants/W/ui.js');
const shell = await import('../js/variants/W/shell.js');
const data = await import('../js/variants/P/data.js');
const tree = await import('../js/variants/W/tree.js');

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
    { champion: 'v0', challenger: 'v1', decision: 'rejected', delta_scalar: 75.71, ran_at: '2026-05-30T01:00:00Z',
      hypothesis_core_idea: 'Enforce explicit slide-structure output from the coordinator.' },
    { champion: 'v0', challenger: 'v2', decision: 'rejected', delta_scalar: 1.51, ran_at: '2026-05-30T02:00:00Z',
      hypothesis_core_idea: 'Tighten the coordinator’s oversight policy.' },
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

test('router: home default; node paths + comparison suffix parse', () => {
  assertEqual(router.parseRoute('').view, 'home');
  assertEqual(router.parseRoute('#/W/').view, 'home');
  assertEqual(router.parseRoute('#/bogus').view, 'home');
  const c = router.parseRoute('#/W/e/E0/gen/v1~cmp=v2');
  assertEqual(c.view, 'candidate'); assertEqual(c.params.gen, 'v1'); assertEqual(c.cmp, 'v2');
  const d = router.parseRoute('#/W/e/E0/gen/v1/diff');
  assertEqual(d.view, 'diff'); assertEqual(d.params.gen, 'v1');
  const b = router.parseRoute('#/W/e/E0/board/waffles_single~runs=v0,v1');
  assertEqual(b.view, 'board'); assert(b.runs[0] === 'v0' && b.runs[1] === 'v1', 'two runs parsed');
  assertEqual(router.parseRoute('#/W/e/E0/mutations/coordinator_prompt').params.mutId, 'coordinator_prompt');
  assertEqual(router.parseRoute('#/W/e/E0/paper').view, 'publication');
});

test('router: href round-trips the comparison suffix', () => {
  assertEqual(router.href('candidate', { epochId: 'E0', gen: 'v1' }, { cmp: 'v2' }), '#/W/e/E0/gen/v1~cmp=v2');
  assertEqual(router.href('board', { epochId: 'E0', entry: 'b1' }, { runs: ['v0', 'v1'] }), '#/W/e/E0/board/b1~runs=v0%2Cv1');
});

// ---- HEADLINE: the broadcast STANDINGS + MATCH CARDS ----------------

test('standings: champion (defending) + a match card per challenger with verdict + Δ', async () => {
  freshState(); installFetch();
  const home = await import('../js/variants/W/views/home.js');
  const host = document.createElement('div');
  await home.render(host, CTX(), router.parseRoute('#/W/'));

  // the billboard header carries the epoch + reigning champion + round count.
  assert(allByClass(host, 'dw-billboard')[0], 'a broadcast billboard header rendered');
  assert(host.textContent.includes('Epoch ' + EPOCH_ID), 'the epoch in the billboard');
  assert(host.textContent.includes('reigning champion'), 'reigning champion label');

  // the champion card — v0 defending the title — at the top of the standings.
  const champCard = allByClass(host, 'dw-champ-card')[0];
  assert(champCard, 'the champion defends the title at the top');
  assert(champCard.textContent.includes('v0'), 'champion is v0');
  assert(champCard.textContent.includes('defending'), 'champion is defending the title');

  // ONE match card per challenger round (v1, v2), each with a verdict pill + Δ.
  const cards = allByClass(host, 'dw-match-card');
  assertEqual(cards.length, 2, 'a match card per challenger round (v0→v1, v0→v2)');
  const text = cards.map((c) => c.textContent).join(' || ');
  assert(text.includes('v1') && text.includes('v2'), 'both challengers carded');
  assert(allByClass(host, 'dn-pill').length >= 2, 'each match card carries a verdict pill');
  assert(allByClass(host, 'dw-match-delta').length >= 2, 'each match card shows a Δscalar');
  assert(text.includes('+75.7') || text.includes('75.71'), 'the v1 round shows its +Δscalar');
  // the hypothesis core idea reads on the card.
  assert(text.includes('Enforce explicit slide-structure'), 'the hypothesis core idea on the card');
});

test('standings double as navigation: clicking a match card opens that challenger', async () => {
  freshState(); installFetch();
  const home = await import('../js/variants/W/views/home.js');
  const host = document.createElement('div');
  await home.render(host, CTX(), router.parseRoute('#/W/'));
  // a match card is an anchor whose href opens the challenger's candidate detail.
  const card = allByClass(host, 'dw-match-card')[0];
  const href = card.getAttribute('href');
  assert(href && href.includes('/gen/v1'), 'the match card links to the challenger candidate detail');
  // and the champion card opens the champion.
  const champHref = allByClass(host, 'dw-champ-card')[0].getAttribute('href');
  assert(champHref && champHref.includes('/gen/v0'), 'the champion card opens the champion detail');
});

test('standings render fit-to-width (no pan/zoom viewport)', async () => {
  freshState(); installFetch();
  const home = await import('../js/variants/W/views/home.js');
  const host = document.createElement('div');
  await home.render(host, CTX(), router.parseRoute('#/W/'));
  // no svg in the standings advertises a fixed pixel viewport width attr beyond
  // fit-to-width primitives; the season sparkline is the only svg and it is
  // bounded (no pannable scroll viewport element). Assert no element carries a
  // pan/zoom hook class and the standings container is a plain flow layout.
  const standings = allByClass(host, 'dw-standings')[0];
  assert(standings, 'the standings container exists');
  // no element opts into a pan/zoom viewport hook (a class token of exactly
  // "pan", "zoom", "viewport", or a data-pannable attribute); the standings are
  // a plain flow/grid layout that fits the container width.
  const panners = host.querySelectorAll('[class]').filter((n) => {
    const toks = (n.getAttribute('class') || '').split(/\s+/);
    return toks.includes('pan') || toks.includes('zoom') || toks.includes('viewport') || toks.includes('pannable') || n.hasAttribute('data-pannable');
  });
  assertEqual(panners.length, 0, 'no pan/zoom viewport element in the standings');
});

// ---- HEADLINE: the data-model tree sidebar -------------------------

test('tree sidebar: renders the hierarchy AND navigates multiple gens', () => {
  const model = {
    epochs: [{ id: EPOCH_ID, current: true }],
    byEpoch: { [EPOCH_ID]: {
      gens: [{ id: 'v0', promoted: true, parent: null }, { id: 'v1', promoted: false, parent: 'v0' }, { id: 'v2', promoted: false, parent: 'v0' }],
      boards: [{ id: 'waffles_single', kindTag: '1-turn' }],
    } },
    current: EPOCH_ID,
  };
  const host = document.createElement('div');
  const sink = {};
  const route = router.parseRoute('#/W/e/' + EPOCH_ID + '/gen/v1');
  const toggles = new Set(['e:' + EPOCH_ID, 'e:' + EPOCH_ID + '/gens', 'e:' + EPOCH_ID + '/boards']);
  tree.buildTree(host, model, route, toggles, CTX(sink), () => {});

  assert(host.textContent.includes('Environment'), 'Environment root');
  assert(host.textContent.includes('Generations'), 'Generations group');
  assert(host.textContent.includes('Boards'), 'Boards group');
  assert(host.textContent.includes('Mutation surface'), 'Mutation surface leaf');
  assert(host.textContent.includes('Publication'), 'Publication leaf');
  const labels = allByClass(host, 'dw-text').map((n) => n.textContent);
  assert(labels.includes('v0') && labels.includes('v1') && labels.includes('v2'), 'all three generations are tree nodes');

  // a generation node click navigates to that candidate (multi-gen nav).
  let v2Label = null;
  for (const ln of allByClass(host, 'dw-label')) if ((ln.textContent || '').includes('v2')) v2Label = ln;
  assert(v2Label, 'a v2 node exists');
  v2Label.dispatchEvent({ type: 'click', preventDefault() {}, stopPropagation() {} });
  assert(sink.last && sink.last.v === 'candidate' && sink.last.p.gen === 'v2', 'clicking v2 navigates to candidate v2 (multi-gen nav)');
});

// ---- candidate: gate (fix) + matchups (fix) + compare --------------

test('candidate: stacked promote gate ON the candidate page', async () => {
  freshState(); installFetch();
  const candidate = await import('../js/variants/W/views/candidate.js');
  const host = document.createElement('div');
  await candidate.render(host, CTX(), router.parseRoute('#/W/e/' + EPOCH_ID + '/gen/v1'));
  const gate = allByClass(host, 'dn-gate')[0];
  assert(gate, 'a promote-gate panel rendered ON the candidate page');
  const rules = allByClass(host, 'dn-rule');
  assert(rules.length >= 3, 'each gate rule is its own row (3 short-circuiting rules)');
  assert(allByClass(host, 'dn-sc-table').length >= 1, 'a separate champion-vs-challenger scalar-components block');
  assert(host.textContent.includes('Scalar margin'), 'a rule label present');
});

test('candidate: ALL match-ups for the candidate (v0 shows ≥2)', async () => {
  freshState(); installFetch();
  const candidate = await import('../js/variants/W/views/candidate.js');
  const host = document.createElement('div');
  await candidate.render(host, CTX(), router.parseRoute('#/W/e/' + EPOCH_ID + '/gen/v0'));
  assert(host.textContent.includes('v1') && host.textContent.includes('v2'), 'both challengers referenced');
  // both v0→v1 and v0→v2 rows present in the match-ups table.
  assert(host.textContent.split('v0 → v1').length - 1 >= 1, 'v0 → v1 round present');
  assert(host.textContent.split('v0 → v2').length - 1 >= 1, 'v0 → v2 round present');
});

test('candidate: a "compare with…" target splits the detail into two candidates', async () => {
  freshState(); installFetch();
  const candidate = await import('../js/variants/W/views/candidate.js');
  const host = document.createElement('div');
  await candidate.render(host, CTX(), router.parseRoute('#/W/e/' + EPOCH_ID + '/gen/v1~cmp=v2'));
  const sides = allByClass(host, 'vs-split-side');
  assertEqual(sides.length, 2, 'two comparison sides');
  const tags = allByClass(host, 'vs-split-tag').map((n) => n.textContent);
  assert(tags.includes('A') && tags.includes('B'), 'A and B side tags');
  assert(host.textContent.includes('v1') && host.textContent.includes('v2'), 'both compared candidates rendered');
  assert(allByClass(host, 'vs-cmp-select').length >= 1, 'a compare-with picker');
});

// ---- patch → per-candidate side-by-side diff (REAL strings) --------

test('patch diff view: this candidate’s side-by-side diff, REAL strings', async () => {
  freshState(); installFetch();
  const diff = await import('../js/variants/P/views/diff.js');
  const host = document.createElement('div');
  await diff.render(host, CTX(), router.parseRoute('#/W/e/' + EPOCH_ID + '/gen/v1/diff').params);
  const sxs = allByClass(host, 'dn-sxs')[0];
  assert(sxs, 'the side-by-side diff rendered');
  assert(host.textContent.includes('Always emit an explicit slide structure'), 'challenger new_content on the right');
  assert(host.textContent.includes('Draft an outline'), 'baseline.content on the left');
  assert(!host.textContent.includes('[object Object]'), 'never the baseline OBJECT');
});

test('candidate lifecycle PATCH node routes to the diff view', async () => {
  freshState(); installFetch();
  const candidate = await import('../js/variants/W/views/candidate.js');
  const host = document.createElement('div');
  const sink = {};
  await candidate.render(host, CTX(sink), router.parseRoute('#/W/e/' + EPOCH_ID + '/gen/v1'));
  // the explicit "open patch diff" link target.
  const link = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('dn-linkbtn') && (n.textContent || '').includes('patch diff'))[0];
  assert(link, 'a patch-diff affordance exists on the candidate page');
  assert((link.getAttribute('href') || '').includes('/gen/v1/diff'), 'it targets this candidate’s diff view');
});

// ---- board: INLINE side-by-side transcripts ------------------------

test('board view: two candidates’ transcripts side by side INLINE on run select', async () => {
  freshState(); installFetch();
  const board = await import('../js/variants/W/views/board.js');
  const host = document.createElement('div');
  const sink = {};
  await board.render(host, CTX(sink), router.parseRoute('#/W/e/' + EPOCH_ID + '/board/waffles_single~runs=v0,v1'));
  assert(host.textContent.includes('Board · waffles_single'), 'the per-board heading');
  const scrollers = allByClass(host, 'vs-transcript');
  assertEqual(scrollers.length, 2, 'two inline, independently-scrollable transcript columns');
  assert(host.textContent.includes('Champion draft with a clear structure'), 'side A (v0 champion) transcript');
  assert(host.textContent.includes('Challenger drafting an outline now'), 'side B (v1 challenger) transcript');
  assert(host.textContent.includes('omitted the requested structure'), 'a drift annotation rendered inline');
  // selecting a run stays on the board view (sets the runs target, no separate page).
  const setRun = allByClass(host, 'dn-board-run')[0];
  assert(setRun, 'a transcript-select control exists');
  setRun.dispatchEvent({ type: 'click', preventDefault() {} });
  assert(sink.last && sink.last.v === 'board', 'selecting a run stays on the board view (no separate run page)');
  assert(sink.last && sink.last.o && Array.isArray(sink.last.o.runs), 'it sets the runs comparison target, not a run route');
});

test('board view is first-class + trellis lives here; epoch overview has the heatmap', async () => {
  freshState(); installFetch();
  const epoch = await import('../js/variants/P/views/epoch.js');
  const board = await import('../js/variants/W/views/board.js');
  const ehost = document.createElement('div');
  await epoch.render(ehost, CTX(), router.parseRoute('#/W/e/' + EPOCH_ID).params);
  assert(allByClass(ehost, 'dn-heatmap')[0], 'epoch overview HAS the heatmap');
  assert(allByClass(ehost, 'dn-trellis').length === 0, 'epoch overview has NO trellis (de-dup)');

  const bhost = document.createElement('div');
  await board.render(bhost, CTX(), router.parseRoute('#/W/e/' + EPOCH_ID + '/board/waffles_single'));
  assert(allByClass(bhost, 'dn-trellis')[0], 'the trellis lives in the board view');
  assert(allByClass(bhost, 'dn-heatmap').length === 0, 'the board view has no heatmap');
});

// ---- the FIXED back/up control -------------------------------------

test('back/up control renders the destination into the MAIN detail pane, not the sidebar', async () => {
  freshState(); installFetch();
  // parentRoute computes the up-target; assert it is correct hierarchy-wise.
  const candRoute = router.parseRoute('#/W/e/' + EPOCH_ID + '/gen/v1');
  const up = router.parentRoute(candRoute);
  assert(up && up.view === 'gens' && up.params.epochId === EPOCH_ID, 'candidate backs up to its Generations group');
  assertEqual(router.parentRoute(router.parseRoute('#/W/e/' + EPOCH_ID + '/board/b1')).view, 'boards', 'board backs up to Boards');
  assertEqual(router.parentRoute(router.parseRoute('#/W/e/' + EPOCH_ID)).view, 'home', 'epoch backs up to home');
  assertEqual(router.parentRoute(router.parseRoute('#/W/')), null, 'home is the root (no up)');

  // mount the shell, navigate into a candidate, click back, and assert the MAIN
  // detail pane holds the destination (gens) while the SIDEBAR is untouched.
  globalThis.__ARENA_W_NO_AUTOBOOT__ = true;
  // in a browser `location` and `window.location` are the same global; the
  // harness only installs window.location, so mirror it for the shell.
  globalThis.location = globalThis.window.location;
  globalThis.HashChangeEvent = globalThis.HashChangeEvent || function HashChangeEvent(t) { return { type: t }; };
  // a no-op EventSource so connectSSE() succeeds without scheduling reconnect
  // timers that would keep node alive after the suite resolves.
  globalThis.EventSource = globalThis.EventSource || class { constructor() { this.readyState = 1; } addEventListener() {} close() {} };
  const root = document.createElement('div');
  root.id = 'variant-root';
  document.body.appendChild(root);
  globalThis.window.location.hash = '#/W/e/' + EPOCH_ID + '/gen/v1';
  shell.mountShell(root);
  await new Promise((r) => setTimeout(r, 0));
  await new Promise((r) => setTimeout(r, 0));

  const sidebar = root.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('dw-sidebar'))[0];
  const viewhost = root.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('dw-viewhost'))[0];
  assert(sidebar && viewhost, 'shell mounted with a sidebar + a main detail pane');
  const railBefore = sidebar.firstChild;
  assert((viewhost.textContent || '').includes('Candidate v1'), 'the candidate is in the MAIN pane');

  const fireHashChange = () => {
    const ls = (globalThis.window._listeners && globalThis.window._listeners.hashchange) || [];
    for (const fn of [...ls]) fn({ type: 'hashchange' });
  };
  const back = root.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('dw-back'))[0];
  assert(back, 'the back/up control exists in the chrome');
  back.dispatchEvent({ type: 'click', preventDefault() {} });
  fireHashChange(); // the harness does not auto-fire hashchange on hash set.
  await new Promise((r) => setTimeout(r, 0));
  await new Promise((r) => setTimeout(r, 0));

  // the destination (the Generations group) is in the MAIN detail pane…
  assert((viewhost.textContent || '').includes('Generations'), 'back rendered the destination (Generations) into the MAIN pane');
  // …and the SIDEBAR was NOT used as the render target (it still holds the tree).
  assert((sidebar.textContent || '').includes('Environment'), 'the sidebar still holds the data-model tree (NOT the destination view)');
  assert(!(sidebar.textContent || '').includes('Roster'), 'the destination view did NOT render into the sidebar (Q’s bug)');
  globalThis.__ARENA_W_NO_AUTOBOOT__ = false;
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

test('pickers + pills: monokai + Display defaults switch + persist', () => {
  freshState();
  const root = document.createElement('div');
  assertEqual(ui.DEFAULT_COLOR, 'monokai', 'monokai is the default colour theme');
  assertEqual(ui.DEFAULT_TYPE, 'display', 'Display is the default typeface');
  const typeIds = ui.TYPE_THEMES.map((t) => t[0]);
  assert(['sans', 'editorial', 'technical', 'display'].every((t) => typeIds.includes(t)), 'all four typefaces offered');
  const colorIds = ui.COLOR_THEMES.map((t) => t[0]);
  assert(['solarized-light', 'solarized-dark', 'monokai'].every((t) => colorIds.includes(t)), 'all three colour themes offered');
  shell.applyTheme('solarized-dark', root);
  assertEqual(root.getAttribute('data-w-theme'), 'solarized-dark', 'colour applied to root');
  assertEqual(ui.readColor(), 'solarized-dark', 'colour persisted');
  shell.applyTypeface('technical', root);
  assertEqual(root.getAttribute('data-w-type'), 'technical', 'typeface applied to root');
  assertEqual(ui.readType(), 'technical', 'typeface persisted');
  assertEqual(ui.normaliseColor('nonsense'), 'monokai', 'unknown colour → monokai');
  assertEqual(ui.normaliseType('nonsense'), 'display', 'unknown typeface → display');
  const pill = ui.verdictPill('rejected');
  assert((pill.getAttribute('class') || '').includes('dn-rejected'), 'verdict pill carries its decision class');
});

// ---- digest-gated repaint (no-op) ----------------------------------

test('standings view: digest-gated — identical data does NOT rebuild the DOM', async () => {
  freshState(); installFetch();
  const home = await import('../js/variants/W/views/home.js');
  const host = document.createElement('div');
  await home.render(host, CTX(), router.parseRoute('#/W/'));
  const digest1 = host.getAttribute('data-w-digest');
  const first = host.firstChild;
  const writes1 = host.innerHTMLWriteCount();
  assert(host.children.length > 0, 'standings painted');
  await home.render(host, CTX(), router.parseRoute('#/W/'));
  assertEqual(host.getAttribute('data-w-digest'), digest1, 'digest unchanged');
  assert(host.firstChild === first, 'no clear-and-rebuild on the no-op repaint');
  assertEqual(host.innerHTMLWriteCount(), writes1, 'no innerHTML writes on the no-op repaint');
});

await run();
