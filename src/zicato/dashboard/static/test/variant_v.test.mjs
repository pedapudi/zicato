// test/variant_v.test.mjs — Variant V ("Reel") unit tests.
//
// V is the round-6 convergence-IV CREATIVE-temporal take: the epoch as a
// horizontal REEL — a timeline / playback of the rounds — built on the Console
// III (P) anchor, with S's side-by-side compare folded in and a fixed back
// button. These pin:
//   * the reel renders the rounds in chronological order (ran_at) with verdicts;
//   * selecting a reel station opens that round's matchup + gate + lifecycle;
//   * the data-model TREE sidebar + multi-generation navigation;
//   * the promote gate ON the candidate page (stacked, no overlap);
//   * the patch node → per-candidate SIDE-BY-SIDE diff with REAL strings;
//   * ALL match-ups for a candidate (v0 → ≥2);
//   * a first-class board view + INLINE side-by-side transcript;
//   * the back button navigates UP and renders into the MAIN detail pane;
//   * the reel is fit-to-width (a fixed-viewBox SVG, no pan/zoom wrapper);
//   * the colour + typeface pickers switch + persist (sol-dark + Display);
//   * S's compare picker + pills;
//   * a digest-gated repaint is a true no-op on a heartbeat.

import { installDom, test, run, assert, assertEqual } from './harness.mjs';

installDom();

const router = await import('../js/variants/V/router.js');
const ui = await import('../js/variants/V/ui.js');
const shell = await import('../js/variants/V/shell.js');
const data = await import('../js/variants/V/data.js');
const tree = await import('../js/variants/V/tree.js');
const reelMod = await import('../js/variants/V/reel.js');

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
  // matchups deliberately OUT of chronological order — the reel must re-order
  // them by ran_at (v1's round ran BEFORE v2's).
  '/api/tournaments': { epoch_id: EPOCH_ID, champion_lineage: ['v0'], matchups: [
    { champion: 'v0', challenger: 'v2', decision: 'rejected', delta_scalar: 1.51, ran_at: '2026-05-30T11:00:00Z', hypothesis_core_idea: 'Tighten coordinator oversight.' },
    { champion: 'v0', challenger: 'v1', decision: 'rejected', delta_scalar: 75.71, ran_at: '2026-05-30T10:00:00Z', hypothesis_core_idea: 'Enforce explicit slide-structure output.' },
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
FIXTURE[`/api/matchup-grid/${EPOCH_ID}/v0/v1`] = { epoch_id: EPOCH_ID, champion: 'v0', challenger: 'v1', entry_grid: [
  { entry_id: 'waffles_single', parent_drift_loss: 60.5, child_drift_loss: 60.5, delta: 0, verdict: 'flat', won_by: null },
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

// ---- router: hierarchical path + the `~cmp=` compare suffix --------

test('router: V prefix parses the hierarchy and the ~cmp= compare suffix', () => {
  assertEqual(router.parseRoute('').view, 'home');
  assertEqual(router.parseRoute('#/V/').view, 'home');
  const cand = router.parseRoute(`#/V/e/${EPOCH_ID}/gen/v1`);
  assertEqual(cand.view, 'candidate'); assertEqual(cand.params.gen, 'v1'); assertEqual(cand.params.epochId, EPOCH_ID);
  const cmp = router.parseRoute(`#/V/e/${EPOCH_ID}/gen/v1~cmp=v2`);
  assertEqual(cmp.view, 'candidate'); assertEqual(cmp.params.gen, 'v1'); assertEqual(cmp.cmp, 'v2');
  assertEqual(router.href('candidate', { epochId: EPOCH_ID, gen: 'v1' }, { cmp: 'v2' }), `#/V/e/${EPOCH_ID}/gen/v1~cmp=v2`);
  assertEqual(router.parseRoute(`#/V/e/${EPOCH_ID}/board/waffles_single/v1`).view, 'board');
  assertEqual(router.parseRoute(`#/V/e/${EPOCH_ID}/paper`).view, 'publication');
});

// ---- HERO: the REEL renders rounds chronologically with verdicts ----

test('reel: renders the rounds in chronological order (ran_at) with verdicts + Δ', () => {
  const spec = {
    championId: 'v0',
    // pass them in ran_at order (the shell sorts; here we assert the rendered order).
    rounds: [
      { challenger: 'v1', decision: 'rejected', deltaScalar: 75.71 },
      { challenger: 'v2', decision: 'rejected', deltaScalar: 1.51 },
    ],
    selected: 'v1',
    onSelect() {}, onSeed() {},
  };
  const host = document.createElement('div');
  host.appendChild(reelMod.reel(spec));

  // a fit-to-width strip SVG exists with a fixed viewBox (NO pan/zoom wrapper).
  const strip = allByClass(host, 'vr-strip')[0];
  assert(strip, 'the reel film-strip SVG rendered');
  assert(strip.getAttribute('viewBox'), 'the strip has a fixed viewBox (laid out, not pannable)');
  assert(!(strip.getAttribute('class') || '').includes('viewport'), 'no pan/zoom viewport class');

  // one station per round (+ the seed/champion station) carrying the verdict.
  const ids = allByClass(host, 'vr-card-id').map((n) => n.textContent);
  const i1 = ids.indexOf('v1'); const i2 = ids.indexOf('v2');
  assert(i1 >= 0 && i2 >= 0, 'both round stations rendered (v1 and v2)');
  assert(i1 < i2, 'rounds render in chronological order (v1 before v2)');
  const txt = host.textContent;
  assert(txt.includes('rejected'), 'a verdict is shown on the station');
  assert(txt.includes('♛'), 'the champion spine station is marked');
  // the scrubber + pips are present.
  assert(allByClass(host, 'vr-scrubber')[0], 'the scrubber/stepper rendered');
  assert(allByClass(host, 'vr-pip').length >= 3, 'a pip per station (seed + 2 rounds)');
  // the selected station carries the selection class (a CSS state, not animation).
  assert(allByClass(host, 'vr-sel').length >= 1, 'the scrubbed station is marked selected');
});

test('reel: selecting a station drives onSelect with that round’s challenger', () => {
  let picked = null;
  const spec = { championId: 'v0', rounds: [{ challenger: 'v1', decision: 'rejected', deltaScalar: 75.71 }], selected: null,
    onSelect: (c) => { picked = c; }, onSeed() {} };
  const host = document.createElement('div');
  host.appendChild(reelMod.reel(spec));
  const station = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('vr-station') && n.getAttribute('data-challenger') === 'v1')[0];
  assert(station, 'a clickable round station exists');
  station.dispatchEvent({ type: 'click' });
  assertEqual(picked, 'v1', 'clicking the station selects that round’s challenger');
});

test('reel: digest-gated — identical structure yields an identical digest (heartbeat no-op)', () => {
  const spec = { championId: 'v0', rounds: [{ challenger: 'v1', decision: 'rejected', deltaScalar: 75.71 }], selected: 'v1' };
  assertEqual(reelMod.reelDigest(spec), reelMod.reelDigest({ ...spec }), 'a steady heartbeat is a true reel-digest no-op');
  const moved = reelMod.reelDigest({ ...spec, selected: null });
  assert(moved !== reelMod.reelDigest(spec), 'a scrub (selection change) changes the digest');
});

// ---- the selected reel station opens that round's matchup + gate + lifecycle

test('reel station → candidate: that round’s matchup + promote gate + lifecycle render', async () => {
  freshState(); installFetch();
  const candidate = await import('../js/variants/V/views/candidate.js');
  const host = document.createElement('div');
  // selecting the v1 station navigates to candidate v1 — its detail must carry
  // the lifecycle, the promote gate (stacked) AND its match-up(s).
  await candidate.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, gen: 'v1' }, { view: 'candidate', params: { epochId: EPOCH_ID, gen: 'v1' }, cmp: null });
  const txt = host.textContent;
  assert(txt.includes('Candidate v1'), 'the round’s challenger candidate rendered');
  assert(allByClass(host, 'ezn-dag')[0], 'the lifecycle DAG (matchup → board → gate → terminal) present');
  assert(allByClass(host, 'dn-gate')[0], 'the stacked promote gate present');
  assert(allByClass(host, 'dn-rule').length >= 3, 'each gate rule its own row');
  assert(allByClass(host, 'dn-sc-table').length >= 1, 'the champion-vs-challenger scalar-components block');
  assert(txt.includes('v0 → v1'), 'the match-up for this round shown');
});

// ---- the data-model TREE sidebar + multi-generation nav ------------

test('tree sidebar: Environment → Epoch → {Generations, Boards, Mutation surface, Publication}', () => {
  const host = document.createElement('div');
  const model = {
    epochs: [{ id: EPOCH_ID, current: true }],
    byEpoch: { [EPOCH_ID]: {
      gens: [{ id: 'v0', promoted: true, parent: null }, { id: 'v1', promoted: false, parent: 'v0' }, { id: 'v2', promoted: false, parent: 'v0' }],
      boards: [{ id: 'waffles_single' }, { id: 'picky_stakeholder_emulated' }],
    } },
  };
  const toggles = new Set(['e:' + EPOCH_ID, 'e:' + EPOCH_ID + '/gens', 'e:' + EPOCH_ID + '/boards']);
  tree.buildTree(host, model, router.parseRoute(`#/V/e/${EPOCH_ID}`), toggles, { navigate() {}, href: router.href }, () => {});
  const txt = host.textContent;
  assert(txt.includes('Environment'), 'Environment root');
  assert(txt.includes('Generations') && txt.includes('Boards'), 'the groups');
  assert(txt.includes('Mutation surface') && txt.includes('Publication'), 'the leaf nodes');
  assert(txt.includes('v0') && txt.includes('v1') && txt.includes('v2'), 'every generation is a tree leaf (multi-gen nav)');
  assert(allByClass(host, 'dp-glyph-gen-champ').length >= 1, 'the champion generation marked');
});

test('candidate view: navigating to a SECOND generation works (multi-candidate nav)', async () => {
  freshState(); installFetch();
  const candidate = await import('../js/variants/V/views/candidate.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await candidate.render(host, ctx, { epochId: EPOCH_ID, gen: 'v1' }, { view: 'candidate', params: {}, cmp: null });
  assert(host.textContent.includes('Candidate v1'), 'v1 rendered');
  await candidate.render(host, ctx, { epochId: EPOCH_ID, gen: 'v2' }, { view: 'candidate', params: {}, cmp: null });
  assert(host.textContent.includes('Candidate v2'), 'v2 rendered after switching');
  assert(!host.textContent.includes('Candidate v1'), 'the previous candidate was replaced');
});

// ---- FIX #2: patch node → per-candidate side-by-side diff ----------

test('candidate view: the lifecycle PATCH node is clickable → the per-candidate diff route', async () => {
  freshState(); installFetch();
  const candidate = await import('../js/variants/V/views/candidate.js');
  const host = document.createElement('div');
  let navTo = null;
  await candidate.render(host, { navigate: (v, p) => { navTo = { v, p }; }, href: router.href }, { epochId: EPOCH_ID, gen: 'v1' }, { view: 'candidate', params: {}, cmp: null });
  const patch = allByClass(host, 'ezn-clickable')[0];
  assert(patch, 'the lifecycle patch node is clickable (fix #2)');
  patch.dispatchEvent({ type: 'click' });
  assert(navTo && navTo.v === 'diff' && navTo.p.gen === 'v1', 'patch click routes to this candidate’s diff');
});

test('diff view: the per-candidate side-by-side diff renders REAL strings (not "[object Object]")', async () => {
  freshState(); installFetch();
  const diff = await import('../js/variants/V/views/diff.js');
  const host = document.createElement('div');
  await diff.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, gen: 'v1' });
  assert(host.textContent.includes('Patch diff · v1'), 'the per-candidate diff heading');
  assert(allByClass(host, 'dn-sxs')[0], 'a side-by-side diff component rendered');
  assert(host.textContent.includes('Draft an outline'), 'baseline.content (LEFT) — the real STRING');
  assert(host.textContent.includes('Always emit an explicit slide structure'), 'challenger new_content (RIGHT) — the real STRING');
  assert(!host.textContent.includes('[object Object]'), 'never the baseline OBJECT');
});

// ---- FIX #3: ALL match-ups for a candidate -------------------------

test('candidate view: v0 shows ALL its match-ups (v0→v1 AND v0→v2), not just one', async () => {
  freshState(); installFetch();
  const candidate = await import('../js/variants/V/views/candidate.js');
  const host = document.createElement('div');
  await candidate.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, gen: 'v0' }, { view: 'candidate', params: {}, cmp: null });
  const txt = host.textContent;
  assert(txt.includes('v0 → v1'), 'the v0→v1 round shown');
  assert(txt.includes('v0 → v2'), 'the v0→v2 round shown');
});

// ---- S compare folded into the candidate detail --------------------

test('compare: the candidate pane splits side by side via the ~cmp= target (S folded in)', async () => {
  freshState(); installFetch();
  const candidate = await import('../js/variants/V/views/candidate.js');
  const host = document.createElement('div');
  // a compare picker is always offered…
  await candidate.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, gen: 'v1' }, { view: 'candidate', params: {}, cmp: null });
  assert(allByClass(host, 'vs-cmp-select')[0], 'a "compare with…" picker is offered');
  assert(allByClass(host, 'vs-split-single').length >= 1, 'one side until a compare target is chosen');

  // …and with a cmp target the SAME pane splits into TWO candidates.
  freshState(); installFetch();
  const host2 = document.createElement('div');
  await candidate.render(host2, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, gen: 'v1' }, { view: 'candidate', params: {}, cmp: 'v2' });
  const sides = allByClass(host2, 'vs-split-side');
  assert(sides.length === 2, 'two candidate panels side by side');
  assert(host2.textContent.includes('v1') && host2.textContent.includes('v2'), 'both candidates present');
  assert(host2.textContent.includes('vs'), 'the head names the comparison (v1 vs v2)');
});

// ---- FIX #4 + #5: first-class board + INLINE side-by-side transcript

test('board view: first-class + selecting a run shows the transcript INLINE side by side', async () => {
  freshState(); installFetch();
  const board = await import('../js/variants/V/views/board.js');
  const host = document.createElement('div');
  await board.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, entry: 'waffles_single', gen: 'v1' });
  assert(host.textContent.includes('Board · waffles_single'), 'the first-class per-board view');
  const xgrid = allByClass(host, 'dn-xscript-grid')[0];
  assert(xgrid, 'the INLINE side-by-side transcript pane rendered within the board view');
  assert(allByClass(host, 'dn-xscript-col').length === 2, 'two candidates’ transcripts side by side');
  assert(host.textContent.includes('Drafting an outline'), 'the selected run’s transcript rendered INLINE (no route away)');
});

test('board view: a candidate row links INLINE (board+gen), never a separate run page', async () => {
  freshState(); installFetch();
  const board = await import('../js/variants/V/views/board.js');
  const host = document.createElement('div');
  await board.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, entry: 'waffles_single' });
  const runLink = allByClass(host, 'dn-board-run')[0];
  assert(runLink, 'a per-candidate transcript link exists');
  const href = runLink.getAttribute('href') || '';
  assert(href.includes('/board/') && !href.includes('/run/'), 'the link stays inline on the board view, not a /run/ page');
});

// ---- FIX #6: trellis in the Boards view; heatmap at epoch overview --

test('de-dup: the trellis lives in the Boards view; the epoch overview has the heatmap only', async () => {
  freshState(); installFetch();
  const epoch = await import('../js/variants/V/views/epoch.js');
  const boards = await import('../js/variants/V/views/boards.js');
  const ctx = { navigate() {}, href: router.href };
  const ehost = document.createElement('div');
  await epoch.render(ehost, ctx, { epochId: EPOCH_ID });
  assert(allByClass(ehost, 'dn-heatmap')[0], 'the epoch overview keeps the heatmap');
  assert(allByClass(ehost, 'dn-trellis').length === 0, 'the epoch overview has NO trellis');
  const bhost = document.createElement('div');
  await boards.render(bhost, ctx, { epochId: EPOCH_ID });
  assert(allByClass(bhost, 'dn-trellis')[0], 'the Boards view carries the trellis');
  assert(allByClass(bhost, 'dn-heatmap').length === 0, 'the Boards view has NO heatmap');
});

// ---- the BACK button: navigates UP, renders into the MAIN pane -----

test('back/up: upTarget walks the hierarchy up one level (never into the sidebar)', () => {
  // entry drill → candidate → generations → epoch → environment
  let up = router.upTarget(router.parseRoute(`#/V/e/${EPOCH_ID}/gen/v1/waffles_single`));
  assert(up.view === 'candidate' && up.params.gen === 'v1', 'entry drill → its candidate');
  up = router.upTarget(router.parseRoute(`#/V/e/${EPOCH_ID}/gen/v1`));
  assert(up.view === 'gens', 'candidate → the Generations group');
  up = router.upTarget(router.parseRoute(`#/V/e/${EPOCH_ID}/gens`));
  assert(up.view === 'epoch', 'a group → the epoch');
  up = router.upTarget(router.parseRoute(`#/V/e/${EPOCH_ID}`));
  assert(up.view === 'home', 'the epoch → the environment');
  assertEqual(router.upTarget(router.parseRoute('#/V/')), null, 'environment is the root (nothing above)');
});

test('back/up: the destination renders into the MAIN detail host, leaving the rail untouched', async () => {
  freshState(); installFetch();
  // simulate the shell wiring: a tree rail host and a separate detail host. The
  // back action resolves upTarget and the renderer paints into the DETAIL host
  // (NEVER the rail) — the round-6 fix to Q's back-button bug.
  const railHost = document.createElement('div');
  railHost.appendChild(document.createElement('div')); // pretend the tree is mounted
  const railBefore = railHost.firstChild;
  const detailHost = document.createElement('div');

  const route = router.parseRoute(`#/V/e/${EPOCH_ID}/gen/v1`);
  const up = router.upTarget(route); // → gens
  assert(up && up.view === 'gens', 'back from a candidate goes up to Generations');
  const gens = await import('../js/variants/V/views/gens.js');
  await gens.render(detailHost, { navigate() {}, href: router.href }, up.params);
  assert(detailHost.textContent.includes('Generations'), 'the destination painted into the MAIN detail host');
  assert(railHost.firstChild === railBefore, 'the rail host is UNCHANGED by the back action (not the sidebar)');
});

// ---- pickers + pills ------------------------------------------------

test('pickers: colour (sol-dark default) + typeface (Display default) switch + persist', () => {
  freshState();
  const root = document.createElement('div');
  assertEqual(ui.DEFAULT_COLOR, 'solarized-dark', 'solarized-dark is the default colour');
  assertEqual(ui.DEFAULT_TYPE, 'display', 'Display is the default typeface');
  const typeIds = ui.TYPE_THEMES.map((t) => t[0]);
  assert(['sans', 'editorial', 'technical', 'display'].every((t) => typeIds.includes(t)), 'all four typefaces offered');
  const colorIds = ui.COLOR_THEMES.map((t) => t[0]);
  assert(['monokai', 'solarized-dark', 'solarized-light'].every((c) => colorIds.includes(c)), 'all three colours offered');
  shell.applyTheme('monokai', root);
  assertEqual(root.getAttribute('data-v-theme'), 'monokai', 'colour applied to the V root');
  assertEqual(ui.readColor(), 'monokai', 'colour persisted');
  shell.applyTypeface('technical', root);
  assertEqual(root.getAttribute('data-v-type'), 'technical', 'typeface applied to the V root');
  assertEqual(ui.readType(), 'technical', 'typeface persisted');
  assertEqual(ui.normaliseColor('nonsense'), 'solarized-dark', 'unknown colour → sol-dark');
  assertEqual(ui.normaliseType('nonsense'), 'display', 'unknown typeface → display');
});

test('pills: a verdict pill renders for promoted / rejected / baseline', () => {
  const promoted = ui.verdictPill('promoted');
  assert((promoted.getAttribute('class') || '').includes('dn-promoted'), 'promoted pill class');
  const rejected = ui.verdictPill('rejected');
  assert((rejected.getAttribute('class') || '').includes('dn-rejected'), 'rejected pill class');
  const baseline = ui.verdictPill('baseline');
  assert(baseline.textContent.includes('seed'), 'baseline pill reads as the seed');
});

// ---- digest no-op ---------------------------------------------------

test('candidate view: digest-gated — identical data does NOT rebuild the DOM (heartbeat no-op)', async () => {
  freshState(); installFetch();
  const candidate = await import('../js/variants/V/views/candidate.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await candidate.render(host, ctx, { epochId: EPOCH_ID, gen: 'v1' }, { view: 'candidate', params: {}, cmp: null });
  const digest1 = host.getAttribute('data-n-digest');
  const first = host.firstChild;
  const writes1 = host.innerHTMLWriteCount();
  assert(host.children.length > 0, 'candidate painted');
  await candidate.render(host, ctx, { epochId: EPOCH_ID, gen: 'v1' }, { view: 'candidate', params: {}, cmp: null });
  assertEqual(host.getAttribute('data-n-digest'), digest1, 'digest unchanged on the no-op repaint');
  assert(host.firstChild === first, 'no clear-and-rebuild on the no-op repaint');
  assertEqual(host.innerHTMLWriteCount(), writes1, 'no innerHTML writes on the no-op repaint');
});

await run();
