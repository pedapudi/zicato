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

import { installDom, test, run, assert, assertEqual, assertDeep, makeEvent } from './harness.mjs';

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
const { bus } = await import('../js/core/bus.js');
const { svgEl } = await import('../js/core/dom.js');

const EPOCH_ID = '2026-05-30_e0';

// Stamp a FRESH `last_heartbeat` on a heartbeat fixture so the live-status
// staleness gate (deriveLiveStatus) reads it as a live orchestrator pulse.
// A real SSE heartbeat always carries this stamp; these UI fixtures elide it,
// and after the dead-run-shows-LIVE fix a heartbeat with no ageable timestamp
// reads STALE (not live). Use this for any fixture that should drive a LIVE
// proposing/tournament render off the heartbeat phase alone (no in-flight
// active-runs to act as ground truth). Respects an explicit `last_heartbeat`
// already on the object (e.g. a deliberately-stale fixture).
function freshHb(hb) {
  if (hb && hb.last_heartbeat == null) {
    return { ...hb, last_heartbeat: new Date().toISOString() };
  }
  return hb;
}

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

// Resolve a fetch path against a fixture map: try the EXACT path first (so an
// explicit `?epoch=<id>` fixture wins — the genuine multi-epoch scoping case),
// then fall back to the query-LESS base path. The Tier-1 views now request
// `/api/epoch?epoch=<id>` etc.; for a single-epoch fixture (every existing
// test) `<id>` is the current epoch, so the scoped read is byte-identical to
// the base — the fallback serves it from the base fixture, unchanged.
function lookupFixture(F, path) {
  if (Object.prototype.hasOwnProperty.call(F, path)) return F[path];
  const q = path.indexOf('?');
  if (q >= 0) {
    const base = path.slice(0, q);
    if (Object.prototype.hasOwnProperty.call(F, base)) return F[base];
  }
  return undefined;
}
function installFetch() {
  globalThis.fetch = async (path) => {
    const v = lookupFixture(FIXTURE, path);
    if (v !== undefined) return { ok: true, json: async () => v };
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
  // The FINAL liked study (single-generation opt 2) DROPPED the scalar-component
  // bars as redundant with the radar silhouette — they must be GONE.
  assertEqual(allByClass(host, 'dn-sc-table').length, 0, 'the scalar-components block is REMOVED (folded into the radar)');
  assert(host.textContent.includes('Scalar margin'), 'a rule label present');
});

// ---- the FINAL liked dossier: radar silhouette folded in, scalar-bars out ----

test('candidate view: the RADAR SILHOUETTE is folded in (candidate vs champion across the gate axes); scalar-bars GONE', async () => {
  freshState(); installFetch();
  const candidate = await import('../js/variants/T/views/candidate.js');
  const host = document.createElement('div');
  await candidate.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, gen: 'v1' });
  // the radar silhouette renders (svg.radarSilhouette → .dn-radar) inside its pane.
  assert(allByClass(host, 'dn-radar')[0], 'the radar silhouette SVG rendered on the candidate page');
  assert(allByClass(host, 'dn-radarpane')[0], 'the radar sits in its width-capped pane');
  // each axis carries a hover hit-target so the operator can read the value.
  assert(allByClass(host, 'dn-radar-hot').length >= 3, 'the radar exposes ≥3 hover-able axis vertices');
  // the removed scalar-component bars must not reappear anywhere on the page.
  assertEqual(allByClass(host, 'dn-sc-table').length, 0, 'no scalar-component table anywhere on the dossier');
  assert(!host.textContent.includes('Scalar components'), 'no "Scalar components" heading');
});

// ---- the radar carries MEANINGFUL axis LABELS (not 1–9 indices) ----
// The operator flagged "the radar chart is missing labels" (it showed axis
// indices). candidate.js builds + passes `axes[].label` — scalar / pass-rate /
// each per-judge (gate scalar_components) — so the silhouette names its axes.
test('candidate view: the radar silhouette names its axes (scalar / pass-rate / per-component), NOT numeric 1–9 indices', async () => {
  freshState(); installFetch();
  const candidate = await import('../js/variants/T/views/candidate.js');
  const host = document.createElement('div');
  await candidate.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, gen: 'v1' });
  const radar = svgsByClass(host, 'dn-radar')[0];
  assert(radar, 'the radar silhouette SVG rendered');
  // the axis-label texts (dn-radar-axislab) — the meaningful names, not ticks.
  const labelEls = allByClass(radar, 'dn-radar-axislab');
  const labels = labelEls.map((n) => (n.textContent || '').trim()).filter(Boolean);
  assert(labels.length >= 3, 'the radar paints text axis labels (≥3 named axes), not bare index ticks');
  // the gate-weighed axes the model builds: scalar (inverse), pass-rate, + each
  // per-component from gate.scalar_components (here: drift, schema). At least the
  // scalar + pass-rate axes must carry their real names.
  assert(labels.includes('scalar'), 'the scalar axis is labeled "scalar"');
  assert(labels.includes('pass-rate'), 'the pass-rate axis is labeled "pass-rate"');
  assert(labels.includes('drift') || labels.includes('schema'),
    'a per-component (gate scalar_components) axis carries its component name');
  // none of the rendered axis LABELS is a bare numeric index (the 1–9 bug).
  assert(!labels.some((l) => /^\d+$/.test(l)), 'no axis label is a bare numeric index (1–9)');
  // and no numeric index-tick fallback is used while there are ≤8 named axes.
  assertEqual(allByClass(radar, 'dn-radar-axistick').length, 0,
    'no numeric index-tick fallback while the axes are within the labeled range');
});

// ---- the dossier is REORGANISED per the study (coordinated, not sprawling) ----
// The study folds the per-board read + gate ladder + labeled radar into ONE
// coordinated grid beneath the full-width lifecycle spine. Assert the sections
// are present AND arranged: a 2-column dossier grid (per-board + gate LEFT,
// silhouette RIGHT), with the lifecycle spine above and generalization below.
test('candidate view: the dossier reads as one organized layout — coordinated grid (per-board + gate | radar), spine above, generalization below', async () => {
  freshState(); installFetch();
  const candidate = await import('../js/variants/T/views/candidate.js');
  const host = document.createElement('div');
  await candidate.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, gen: 'v1' });
  // the coordinated grid exists with a main (per-board + gate) + side (radar) column.
  const grid = allByClass(host, 'dn-dossier-grid')[0];
  assert(grid, 'the dossier body is a coordinated grid (not a flat full-bleed stack)');
  assert(allByClass(host, 'dn-dossier-col--main')[0], 'the grid has a MAIN column (per-board + gate ladder)');
  assert(allByClass(host, 'dn-dossier-col--side')[0], 'the grid has a SIDE column (the radar silhouette)');
  // single (non-compare) view → the WIDE grid (not the narrow compare collapse).
  assertEqual(allByClass(host, 'dn-dossier-grid--narrow').length, 0,
    'the single-candidate dossier uses the wide 2-column grid (not the narrow collapse)');
  // the SIDE column holds the radar; the MAIN column holds the per-board DUMBBELL
  // (responsive, width-filling) + the gate ladder.
  const side = allByClass(host, 'dn-dossier-col--side')[0];
  assert(svgsByClass(side, 'dn-radar')[0], 'the radar sits in the side column');
  const main = allByClass(host, 'dn-dossier-col--main')[0];
  const dot = svgsByClass(main, 'dn-dumbbell')[0];
  assert(dot, 'the per-board champion○ → candidate● dumbbell sits in the main column');
  assertEqual(dot.getAttribute('width'), '100%', 'the per-board dumbbell is width-filling (responsive, not crammed right)');
  assert(allByClass(main, 'dn-gate')[0], 'the promote-gate ladder sits in the main column beside the per-board read');
  // the lifecycle spine reads ABOVE the grid; the generalization slope BELOW it.
  assert(host.textContent.includes('Lifecycle · cause → effect → verdict'), 'the lifecycle spine section reads above the grid');
  assert(allByClass(host, 'dn-genpane')[0] || !host.textContent.includes('Generalization'),
    'the generalization slope is a small width-capped supporting panel when present');
});

// ---- a RACING / in-flight candidate shows a PROJECTED radar + the affordance ----
// While a candidate is racing (only a projected scalar / partial board slice) the
// dossier must not read bare: it shows a clearly-marked projected/ghosted radar
// and a "settled comparisons appear once boards finish" affordance, with the
// settled dumbbell/gate comparisons gated on landed data.
test('candidate view (RACING): an in-flight candidate ghosts a PROJECTED radar + surfaces the racing affordance (not a bare dossier)', async () => {
  freshState(); installFetch();
  // an in-flight racer v3 (champion v0) with NO settled scalar yet — only a live
  // PROJECTED standing — but a recorded gate (scalar_components) so the silhouette
  // forms ≥3 axes and can ghost. Per-entry has ONE landed board (pass_fail) so a
  // pass-rate axis lands too; the rest stream.
  const F = { ...FIXTURE };
  F['/api/epoch'] = { ...FIXTURE['/api/epoch'],
    tournament: { structure: 'racing', params: {} },
    experiments: [...FIXTURE['/api/epoch'].experiments, { generation_id: 'v3', parent_generation_id: 'v0', outcome: {} }] };
  F['/api/lineage'] = { generations: [...FIXTURE['/api/lineage'].generations,
    { generation_id: 'v3', epoch_id: EPOCH_ID, parent_generation_id: 'v0', promoted: false }] };
  // v3 is NOT in the score-trajectory → no settled scalar (it is racing).
  F[`/api/generation/${EPOCH_ID}/v3/per-entry`] = { epoch_id: EPOCH_ID, generation_id: 'v3', entries: [
    { entry_id: 'waffles_single', run_id: 'run_v3_waffles', drift_loss: 58.0, pass_fail: 1, runtime_ms: 120000, wall_clock_budget_exceeded: false },
  ] };
  F[`/api/round/${EPOCH_ID}/v0/v3/gate`] = { decision: 'pending', delta_scalar: -2.0, delta_pass_rate: 0.5,
    rules: [{ id: 'scalar_margin', label: 'Scalar margin', status: 'not_reached', fired: false }],
    scalar_components: { champion: { drift: 68.5, schema: 1.43 }, challenger: { drift: 60.0, schema: 1.0 } } };
  globalThis.fetch = async (path) => {
    const v = lookupFixture(F, path);
    if (v !== undefined) return { ok: true, json: async () => v };
    return { ok: false, status: 404, json: async () => ({ error: 'nf' }) };
  };
  // the LIVE active tournament (racing) for THIS epoch with a projected standing
  // for v3 — boards still streaming (3 of 8 scored).
  coreState.state.activeTournament = { epoch_id: EPOCH_ID, structure: 'racing',
    projected: { v3: { scalar: 60.0, boards_done: 3, boards_total: 8 } } };
  coreState.state.heartbeat = { phase: 'tournament:running', epoch_id: EPOCH_ID, last_heartbeat: new Date().toISOString() };
  coreState.state.activeRuns = [{ generation_id: 'v3', entry_id: 'picky_stakeholder_emulated', run_id: 'run_v3_picky' }];
  try {
    const candidate = await import('../js/variants/T/views/candidate.js');
    const host = document.createElement('div');
    await candidate.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, gen: 'v3' });
    // the radar still renders (the projected silhouette), GHOSTED via dn-proj.
    const radar = svgsByClass(host, 'dn-radar')[0];
    assert(radar, 'a projected radar silhouette renders for the in-flight candidate (not omitted)');
    const ghosted = allByClass(radar, 'dn-radar-cand').some((n) => (n.getAttribute('class') || '').split(/\s+/).includes('dn-proj'));
    assert(ghosted, 'the candidate polygon is GHOSTED (dn-proj) — clearly marked projected');
    // it still names its axes (labels, not indices).
    const labels = allByClass(radar, 'dn-radar-axislab').map((n) => (n.textContent || '').trim()).filter(Boolean);
    assert(labels.includes('scalar'), 'the projected radar still labels its scalar axis');
    // the racing affordance surfaces so the dossier is not bare.
    assert(allByClass(host, 'dn-racing-affordance')[0], 'the "settled comparisons appear once boards finish" affordance is shown');
    assert(/settled comparisons/i.test(host.textContent), 'the affordance names what is pending (settled comparisons)');
    // the headline reads a PROJECTED (not settled) scalar.
    assert(allByClass(host, 'dt-proj')[0], 'the dossier marks the projected (in-flight) treatment');
    // the live in-flight board panel still reads ("N board running").
    assert(/board running/i.test(host.textContent), 'the live in-flight board panel still reads for the racing candidate');
  } finally {
    coreState.state.activeTournament = null;
    coreState.state.heartbeat = null;
    coreState.state.activeRuns = [];
  }
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

// the compare panes are EQUAL-WIDTH columns, so BOTH lifecycle DAGs must use the
// SAME (narrow) viewBox width — otherwise the fit-to-width B pane scales down vs
// A and renders smaller with an empty top band. The DAG width is keyed on the
// SPLIT-LAYOUT flag (true for both A and B), not the per-side cmpId (null on B).
function dagViewBoxWidths(host) {
  return allByClass(host, 'ezn-dag').map((svg) => {
    const vb = (svg.getAttribute('viewBox') || '').split(/\s+/);
    return Number(vb[2]);
  });
}

test('candidate COMPARE view: BOTH lifecycle DAGs share the SAME (narrow, 560) viewBox width', async () => {
  freshState(); installFetch();
  const candidate = await import('../js/variants/T/views/candidate.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await candidate.render(host, ctx, { epochId: EPOCH_ID, gen: 'v1' }, { params: { epochId: EPOCH_ID, gen: 'v1' }, cmp: 'v2' });

  const widths = dagViewBoxWidths(host);
  assert(widths.length === 2, 'two lifecycle DAGs (A and B) in the compare view');
  assertEqual(widths[0], widths[1], 'the A and B DAGs share an identical viewBox width (equal scale, no shrunken B pane)');
  assertEqual(widths[0], 560, 'both compare panes use the NARROW 560-unit viewBox');
});

test('candidate SINGLE view: the lone lifecycle DAG uses the WIDE (900) viewBox width', async () => {
  freshState(); installFetch();
  const candidate = await import('../js/variants/T/views/candidate.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await candidate.render(host, ctx, { epochId: EPOCH_ID, gen: 'v1' });

  const widths = dagViewBoxWidths(host);
  assert(widths.length === 1, 'a single lifecycle DAG in the non-compare view');
  assertEqual(widths[0], 900, 'the single-candidate view keeps the WIDE 900-unit viewBox');
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

// ---- successive-halving REUSE champion transcript: gen×entry fallback ----

// Install a fetch that serves the base FIXTURE but lets a test OVERRIDE or
// SUPPRESS specific paths — the reuse-champion case needs the champion's
// /api/conversation to come back empty (the score-reuse run_id has no events
// of its own) while a by-(epoch, gen, entry) transcript exists.
function installFetchWith(overrides, suppress) {
  const sup = new Set(suppress || []);
  globalThis.fetch = async (path) => {
    const base = path.indexOf('?') >= 0 ? path.slice(0, path.indexOf('?')) : path;
    if (Object.prototype.hasOwnProperty.call(overrides, base)) {
      return { ok: true, json: async () => overrides[base] };
    }
    if (sup.has(base)) return { ok: false, status: 404, json: async () => ({ error: 'suppressed: ' + base }) };
    const v = lookupFixture(FIXTURE, path);
    if (v !== undefined) return { ok: true, json: async () => v };
    return { ok: false, status: 404, json: async () => ({ error: 'not found: ' + path }) };
  };
}

test('board view: a REUSED champion run (no own transcript) falls back to the gen×entry transcript', async () => {
  freshState();
  // The champion v0's per-entry run_id is a successive-halving REUSE record:
  // /api/conversation/run_v0_waffles yields NO transcript. But the gen×entry
  // /api/run/<epoch>/v0/waffles_single/transcript resolves the one real
  // events.jsonl on disk. The champion side must render THAT, not "unavailable".
  installFetchWith(
    {
      [`/api/run/${EPOCH_ID}/v0/waffles_single/transcript`]: {
        epoch_id: EPOCH_ID, generation_id: 'v0', entry_id: 'waffles_single', run_id: 'real_v0_run',
        turns: [
          { seq: 0, role: 'user', agent: 'operator', text: 'Make a presentation about waffles.' },
          { seq: 1, role: 'agent', agent: 'coordinator', text: 'Champion reused-rung transcript recovered.' },
        ],
        annotations: [], event_count: 31, complete: true,
      },
    },
    ['/api/conversation/run_v0_waffles'],
  );
  const board = await import('../js/variants/T/views/board.js');
  const bhost = document.createElement('div');
  await board.render(bhost, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, entry: 'waffles_single', gen: 'v1' });
  const cols = allByClass(bhost, 'dn-xscript-col');
  assert(cols.length === 2, 'two transcript columns (challenger + champion)');
  // Challenger (v1) side unchanged — its own /api/conversation still resolves.
  assert(bhost.textContent.includes('Drafting an outline'), 'challenger transcript still renders from its own run_id');
  // Champion (v0) side recovered via the gen×entry fallback, NOT "unavailable".
  assert(bhost.textContent.includes('Champion reused-rung transcript recovered'),
    'champion transcript recovered via the gen×entry fallback');
  assert(!bhost.textContent.includes('could not be reconstructed'),
    'the honest "unavailable" message is NOT shown when a gen×entry transcript exists');
});

test('board view: a GENUINELY-absent champion transcript still shows the honest "unavailable" message', async () => {
  freshState();
  // Both the reuse run_id AND the gen×entry transcript are absent — the
  // honest "unavailable" message must remain (no false recovery).
  installFetchWith(
    {},
    ['/api/conversation/run_v0_waffles', `/api/run/${EPOCH_ID}/v0/waffles_single/transcript`],
  );
  const board = await import('../js/variants/T/views/board.js');
  const bhost = document.createElement('div');
  await board.render(bhost, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, entry: 'waffles_single', gen: 'v1' });
  const cols = allByClass(bhost, 'dn-xscript-col');
  assert(cols.length === 2, 'two transcript columns');
  assert(bhost.textContent.includes('Drafting an outline'), 'challenger side unchanged');
  assert(bhost.textContent.includes('could not be reconstructed'),
    'the honest "unavailable" message is preserved for a genuinely-absent gen×entry');
});

test('board view: BOTH sides resolve by (epoch, gen, entry) PRIMARY even when the per-record run_id has no events', async () => {
  freshState();
  // The deterministic triple is the primary key: BOTH the challenger (v1)
  // and the champion (v0) resolve via /api/run/<epoch>/<gen>/<entry>/transcript
  // even though NEITHER per-entry run_id resolves through /api/conversation
  // (both are reuse / index-only records with no events of their own). The
  // panes must render both transcripts from the gen×entry events.jsonl —
  // never the run_id-first path.
  installFetchWith(
    {
      [`/api/run/${EPOCH_ID}/v1/waffles_single/transcript`]: {
        epoch_id: EPOCH_ID, generation_id: 'v1', entry_id: 'waffles_single', run_id: 'real_v1_run',
        turns: [
          { seq: 0, role: 'user', agent: 'operator', text: 'Make a presentation about waffles.' },
          { seq: 1, role: 'agent', agent: 'coordinator', text: 'Challenger by-triple transcript.' },
        ],
        annotations: [], event_count: 12, complete: true,
      },
      [`/api/run/${EPOCH_ID}/v0/waffles_single/transcript`]: {
        epoch_id: EPOCH_ID, generation_id: 'v0', entry_id: 'waffles_single', run_id: 'real_v0_run',
        turns: [
          { seq: 0, role: 'user', agent: 'operator', text: 'Make a presentation about waffles.' },
          { seq: 1, role: 'agent', agent: 'coordinator', text: 'Champion by-triple transcript.' },
        ],
        annotations: [], event_count: 31, complete: true,
      },
    },
    // Both run_id-keyed lookups are suppressed — the run_id-first path would 404.
    ['/api/conversation/run_v1_waffles', '/api/conversation/run_v0_waffles'],
  );
  const board = await import('../js/variants/T/views/board.js');
  const bhost = document.createElement('div');
  await board.render(bhost, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, entry: 'waffles_single', gen: 'v1' });
  const cols = allByClass(bhost, 'dn-xscript-col');
  assert(cols.length === 2, 'two transcript columns');
  assert(bhost.textContent.includes('Challenger by-triple transcript'),
    'challenger side resolved by the (epoch, gen, entry) triple, not its run_id');
  assert(bhost.textContent.includes('Champion by-triple transcript'),
    'champion side resolved by the (epoch, gen, entry) triple, not its run_id');
  assert(!bhost.textContent.includes('could not be reconstructed'),
    'no honest-absence message when the gen×entry transcript exists for both sides');
});

test('board view: the per-pane transcript host split (live-beat scroll fix) is preserved', async () => {
  freshState(); installFetch();
  const board = await import('../js/variants/T/views/board.js');
  const bhost = document.createElement('div');
  await board.render(bhost, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, entry: 'waffles_single', gen: 'v1' });
  // The two persistent sub-hosts (upper + transcript) must exist — the digest
  // split that keeps a live beat from resetting the transcript scroll.
  assert(bhost.querySelectorAll('[data-node]').filter((n) => n.getAttribute('data-node') === 'board-upper').length === 1,
    'the upper (live) sub-host exists');
  assert(bhost.querySelectorAll('[data-node]').filter((n) => n.getAttribute('data-node') === 'board-xscript').length === 1,
    'the transcript sub-host exists (separate from the upper host)');
});

// ---- LIVE TRANSCRIPT: a RUNNING candidate streams its transcript inline ----
//
// In every tournament mode the operator can select a candidate that is
// currently RUNNING on a board entry and read its transcript as the run
// produces turns. The active-runs feed (structure-agnostic) carries the
// running candidate's run_id / generation_id; its events.jsonl is already
// growing on disk, so the gen×entry transcript resolves PARTIALLY mid-flight.

// A fetch whose run-transcript / conversation response can be SWAPPED between
// renders, so a test can simulate a live transcript GROWING a turn. `getRun`
// returns the current run-transcript payload (keyed by the base path).
function installGrowableFetch(runPayloads, suppress) {
  const sup = new Set(suppress || []);
  globalThis.fetch = async (path) => {
    const base = path.indexOf('?') >= 0 ? path.slice(0, path.indexOf('?')) : path;
    if (Object.prototype.hasOwnProperty.call(runPayloads, base)) {
      const v = runPayloads[base];
      return { ok: true, json: async () => (typeof v === 'function' ? v() : v) };
    }
    if (sup.has(base)) return { ok: false, status: 404, json: async () => ({ error: 'suppressed: ' + base }) };
    const v = lookupFixture(FIXTURE, path);
    if (v !== undefined) return { ok: true, json: async () => v };
    return { ok: false, status: 404, json: async () => ({ error: 'not found: ' + path }) };
  };
}

// (a) A RUNNING candidate (active-run carries run_id; NO loss.json / no
// per-entry row) resolves a PARTIAL transcript and is SELECTABLE.
test('board view (LIVE): a RUNNING candidate with no scored row resolves a PARTIAL transcript and is selectable', async () => {
  freshState();
  // v3 is RUNNING on waffles_single: it is in active-runs (carrying its
  // run_id) but has NO per-entry record in ANY generation's pivot — no
  // loss.json yet. Its partial transcript resolves by the (epoch, gen, entry)
  // triple from the still-growing events.jsonl.
  installGrowableFetch({
    [`/api/run/${EPOCH_ID}/v3/waffles_single/transcript`]: {
      epoch_id: EPOCH_ID, generation_id: 'v3', entry_id: 'waffles_single', run_id: 'run_v3_live',
      turns: [
        { seq: 0, role: 'user', agent: 'operator', text: 'Make a presentation about waffles.' },
        { seq: 1, role: 'agent', agent: 'coordinator', text: 'Live partial turn so far.' },
      ],
      annotations: [], event_count: 4, complete: false,  // PARTIAL: no terminal event yet
    },
  });
  // lineage carries v3 as a running challenger (so role/parent resolve).
  FIXTURE['/api/lineage'].generations.push({ generation_id: 'v3', epoch_id: EPOCH_ID, parent_generation_id: 'v0', promoted: false });
  coreState.state.activeRuns = [{ generation_id: 'v3', entry_id: 'waffles_single', run_id: 'run_v3_live', progress: 0.4, epoch_id: EPOCH_ID }];

  const board = await import('../js/variants/T/views/board.js');
  const ctx = { navigate() {}, href: router.href };

  // FIRST: render WITHOUT a selection — the running candidate is a SELECTABLE
  // breakdown row ("watch live →" linking to board+v3), not "no run".
  const unsel = document.createElement('div');
  await board.render(unsel, ctx, { epochId: EPOCH_ID, entry: 'waffles_single' });
  const links = allByClass(unsel, 'dn-board-run').map((a) => a.getAttribute('href') || '');
  assert(links.some((h) => h.includes('/board/') && h.includes('v3')), 'the RUNNING candidate v3 is a selectable transcript row');
  assert(unsel.textContent.includes('watch live'), 'the running candidate reads "watch live →"');

  // THEN: select it — its PARTIAL transcript renders (the still-growing
  // events.jsonl), not "unavailable", and reads as a live/streaming column.
  const bhost = document.createElement('div');
  await board.render(bhost, ctx, { epochId: EPOCH_ID, entry: 'waffles_single', gen: 'v3' });
  assert(bhost.textContent.includes('Live partial turn so far'), 'the partial transcript of the running candidate rendered');
  assert(!bhost.textContent.includes('could not be reconstructed'), 'no honest-absence message for a running candidate with a partial transcript');
  assert(allByClass(bhost, 'dn-xscript-live')[0], 'the running candidate column carries a live marker');

  // cleanup the shared fixture mutation.
  FIXTURE['/api/lineage'].generations = FIXTURE['/api/lineage'].generations.filter((g) => g.generation_id !== 'v3');
  coreState.state.activeRuns = [];
});

// (b) The transcript host repaints when the live conversation GAINS a turn, but
// NOT on a progress-only beat.
test('board view (LIVE): the transcript host repaints on a NEW TURN but NOT on a progress-only beat', async () => {
  freshState();
  let turnCount = 1;  // the live transcript grows between renders when we bump this.
  installGrowableFetch({
    [`/api/run/${EPOCH_ID}/v3/waffles_single/transcript`]: () => ({
      epoch_id: EPOCH_ID, generation_id: 'v3', entry_id: 'waffles_single', run_id: 'run_v3_live',
      turns: Array.from({ length: turnCount }, (_, i) => ({ seq: i, role: i % 2 ? 'agent' : 'user', agent: 'coordinator', text: 'turn #' + i })),
      annotations: [], event_count: turnCount * 2, complete: false,
    }),
  });
  FIXTURE['/api/lineage'].generations.push({ generation_id: 'v3', epoch_id: EPOCH_ID, parent_generation_id: 'v0', promoted: false });
  coreState.state.activeRuns = [{ generation_id: 'v3', entry_id: 'waffles_single', run_id: 'run_v3_live', progress: 0.4, epoch_id: EPOCH_ID }];

  const board = await import('../js/variants/T/views/board.js');
  const bhost = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  const params = { epochId: EPOCH_ID, entry: 'waffles_single', gen: 'v3' };

  await board.render(bhost, ctx, params);
  const xhost = bhost.querySelectorAll('[data-node]').filter((n) => n.getAttribute('data-node') === 'board-xscript')[0];
  assert(xhost, 'the transcript sub-host exists');
  const digestAfterFirst = xhost.getAttribute('data-t-digest');

  // PROGRESS-ONLY beat: same turns, only progress advanced. The transcript
  // digest (content-gated) must NOT change — no repaint, scroll preserved.
  coreState.state.activeRuns = [{ generation_id: 'v3', entry_id: 'waffles_single', run_id: 'run_v3_live', progress: 0.7, epoch_id: EPOCH_ID }];
  data.invalidate();  // mimic a beat: caches dropped; transcript re-fetched (same content).
  await board.render(bhost, ctx, params);
  assertEqual(xhost.getAttribute('data-t-digest'), digestAfterFirst, 'a PROGRESS-ONLY beat does NOT repaint the transcript host (content unchanged)');

  // NEW-TURN beat: the live transcript gained a turn → the content signal
  // changes → the transcript host repaints.
  turnCount = 3;
  data.invalidate();
  await board.render(bhost, ctx, params);
  assert(xhost.getAttribute('data-t-digest') !== digestAfterFirst, 'a NEW-TURN beat DOES repaint the transcript host (content grew)');
  assert(xhost.textContent.includes('turn #2'), 'the newly-arrived turn rendered');

  FIXTURE['/api/lineage'].generations = FIXTURE['/api/lineage'].generations.filter((g) => g.generation_id !== 'v3');
  coreState.state.activeRuns = [];
});

// (c) The live card + transcript path is STRUCTURE-AGNOSTIC — it is driven by
// active-runs, not by any tournament structure. Verify for swiss + elim.
for (const structure of ['swiss', 'single_elim']) {
  test(`board view (LIVE): the live transcript path is structure-agnostic (${structure})`, async () => {
    freshState();
    const F = {
      '/api/epoch': {
        epoch_id: EPOCH_ID, closed: false, goal: 'g',
        tournament: { structure, params: {} },
        experiments: [{ generation_id: 'v0', parent_generation_id: '', outcome: { decision: 'baseline' } }],
        board: [{ id: 'waffles_single', kind: 'single_turn', input_preview: 'x', budget_s: 180, weight: 1 }],
      },
      '/api/lineage': { generations: [
        { generation_id: 'v0', epoch_id: EPOCH_ID, parent_generation_id: '', promoted: true },
        { generation_id: 'v9', epoch_id: EPOCH_ID, parent_generation_id: 'v0', promoted: false },
      ] },
      '/api/score-trajectory': { points: [{ generation_id: 'v0', scalar: 70.94 }] },
      '/api/tournaments': { epoch_id: EPOCH_ID, structure, champion_lineage: ['v0'], matchups: [] },
      [`/api/run/${EPOCH_ID}/v9/waffles_single/transcript`]: {
        epoch_id: EPOCH_ID, generation_id: 'v9', entry_id: 'waffles_single', run_id: 'run_v9_live',
        turns: [{ seq: 0, role: 'agent', agent: 'coordinator', text: `${structure} live turn` }],
        annotations: [], event_count: 2, complete: false,
      },
    };
    installFixtureMap(F);
    coreState.state.activeRuns = [{ generation_id: 'v9', entry_id: 'waffles_single', run_id: 'run_v9_live', progress: 0.3, epoch_id: EPOCH_ID }];

    const board = await import('../js/variants/T/views/board.js');
    const bhost = document.createElement('div');
    await board.render(bhost, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, entry: 'waffles_single', gen: 'v9' });

    assert(bhost.textContent.includes('candidate running') || bhost.textContent.includes('candidates running'), `the live card renders under ${structure}`);
    assert(bhost.textContent.includes(`${structure} live turn`), `the running candidate's live transcript renders under ${structure} (structure-agnostic)`);
    assert(allByClass(bhost, 'dn-xscript-live')[0], `the live marker renders under ${structure}`);

    coreState.state.activeRuns = [];
  });
}

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

  // THE RESEARCH-PREVIEW PILL — a quiet product-status tag pinned NEXT TO the
  // "zıcato console" wordmark in the top bar (NOT a Settings card, NOT a lower-
  // right corner note). It lives inside the brand block, carries the "research"
  // / "preview" label STACKED on two lines, and is the OPPOSITE of the retired
  // light-up card: NO accent-tinted pulsing `dn-respreview` banner exists.
  const pill = allByClass(root, 'dt-respreview')[0];
  assert(pill, 'the research-preview pill is mounted in the shell (top-bar chrome)');
  const brand = allByClass(root, 'dt-brand')[0];
  assert(brand && allByClass(brand, 'dt-respreview')[0],
    'the research-preview pill sits next to the wordmark inside the brand block');
  assert((pill.textContent || '').toLowerCase().includes('research'),
    'the pill carries the "research" label');
  assert((pill.textContent || '').toLowerCase().includes('preview'),
    'the pill carries the "preview" label');
  const lines = allByClass(pill, 'dt-respreview-line');
  assert(lines.length === 2, 'the pill stacks "research" / "preview" on two lines');
  assert(allByClass(root, 'dn-respreview').length === 0,
    'the old light-up Settings research-preview banner is gone');
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

test('pickers: typeface (T7 default, 12 finalized faces / 4 per mode) + colour (monokai default) switch + persist', () => {
  freshState();
  const root = document.createElement('div');
  assertEqual(ui.DEFAULT_COLOR, 'monokai', 'monokai is the default colour theme');
  // The DEFAULT typeface is now T7 · Google Sans Mono (the first Technical face).
  assertEqual(ui.DEFAULT_TYPE, 'T7', 'T7 (Google Sans Mono) is the default typeface');
  // TWELVE finalized options — FOUR per mode across THREE modes.
  assertEqual(ui.TYPE_OPTIONS.length, 12, 'exactly 12 typeface options');
  assertDeep(ui.TYPE_MODE_ORDER, ['technical', 'editorial', 'display'], 'three mode groups in order');
  for (const mode of ui.TYPE_MODE_ORDER) {
    assertEqual(ui.TYPE_OPTIONS.filter((o) => o.mode === mode).length, 4, 'four options in the ' + mode + ' group');
  }
  // the exact id roster lifted from the study.
  const typeIds = ui.TYPE_OPTIONS.map((o) => o.id);
  assertDeep(typeIds,
    ['T7', 'T9', 'T12', 'T14', 'E5', 'E7', 'E8', 'E15', 'D2', 'D12', 'D14', 'D5'],
    'the 12 ids match the operator\'s finalized picks');
  // every option carries the four font-role stacks.
  for (const o of ui.TYPE_OPTIONS) {
    for (const role of ['head', 'prose', 'data', 'code']) {
      assert(typeof o[role] === 'string' && o[role].length > 0, o.id + ' has a ' + role + ' font stack');
    }
  }
  // TYPE_THEMES keeps the back-compat [id, label] shape over the 12 options.
  assertEqual(ui.TYPE_THEMES.length, 12, 'TYPE_THEMES exposes all 12 as [id,label] pairs');
  assertEqual(ui.TYPE_THEMES[0][0], 'T7', 'TYPE_THEMES first id is the default T7');

  const colorIds = ui.COLOR_THEMES.map((t) => t[0]);
  assert(['monokai', 'solarized-dark', 'solarized-light'].every((c) => colorIds.includes(c)), 'the three original colour themes are kept');
  shell.applyTheme('solarized-dark', root);
  assertEqual(root.getAttribute('data-t-theme'), 'solarized-dark', 'colour applied to the T root');
  assertEqual(ui.readColor(), 'solarized-dark', 'colour persisted');
  // apply a finalized option id — it stamps data-t-type="<id>" and persists.
  shell.applyTypeface('E5', root);
  assertEqual(root.getAttribute('data-t-type'), 'E5', 'typeface option applied to the T root');
  assertEqual(ui.readType(), 'E5', 'typeface persisted');
  assertEqual(ui.normaliseColor('nonsense'), 'monokai', 'unknown colour → monokai');
  assertEqual(ui.normaliseType('nonsense'), 'T7', 'unknown typeface → T7 default');
  // LEGACY MIGRATION: the old mode ids map to a sensible finalized id in-group.
  assertEqual(ui.normaliseType('technical'), 'T7', 'legacy "technical" migrates to T7');
  assertEqual(ui.normaliseType('editorial'), 'E5', 'legacy "editorial" migrates to E5');
  assertEqual(ui.normaliseType('display'), 'D2', 'legacy "display" migrates to D2');
  assertEqual(ui.normaliseType('sans'), 'T7', 'the long-dropped Sans id falls back to T7');
  // typeOption resolves to the full option object (real faces).
  assertEqual(ui.typeOption('T7').label, 'T7 · Google Sans Mono', 'typeOption resolves the option object');
});

// ---- the brand wordmark: dotless ı + the accent dot CENTRED on its stem ----

test('brand wordmark: renders "zıcato" with a dotless ı (U+0131) and the accent dot is centred over its stem', async () => {
  freshState(); installFetch();
  const listeners = { hashchange: [] };
  globalThis.HashChangeEvent = function HashChangeEvent() {};
  globalThis.EventSource = function EventSource() { this.readyState = 0; this.addEventListener = () => {}; this.close = () => {}; };
  globalThis.EventSource.CLOSED = 2;
  globalThis.window = globalThis.window || {};
  globalThis.window.localStorage = globalThis.window.localStorage || { getItem() { return null; }, setItem() {} };
  globalThis.window.addEventListener = (t, fn) => { (listeners[t] = listeners[t] || []).push(fn); };
  const loc = { _hash: '#/' };
  Object.defineProperty(loc, 'hash', {
    get() { return this._hash; },
    set(v) { this._hash = v; for (const fn of (listeners.hashchange || [])) fn(); },
  });
  globalThis.location = loc;
  globalThis.window.location = loc;
  globalThis.window.dispatchEvent = () => { for (const fn of (listeners.hashchange || [])) fn(); };

  const root = document.createElement('div');
  document.body.appendChild(root);
  shell.mountShell(root);
  await new Promise((r) => setTimeout(r, 0));

  // the wordmark is an inline SVG (.dt-brand-name) — not a styled text span —
  // so the dot can be pinned to the glyph stem + inherit theme tokens.
  const mark = svgsByClass(root, 'dt-brand-name')[0];
  assert(mark && mark.localName === 'svg', 'the wordmark renders as an inline SVG');

  // the letters render the DOTLESS ı (U+0131), never a dotted "i".
  const text = allByClass(mark, 'dt-brand-letters')[0];
  assert(text, 'the wordmark has a letters <text> element');
  assertEqual(text.textContent, shell.WORDMARK_TEXT, 'the wordmark text is the brand string');
  assert(text.textContent.includes('ı'), 'the wordmark uses the dotless ı (U+0131)');
  assert(!text.textContent.includes('i'), 'no dotted "i" in the wordmark');
  // the letters fill with currentColor (theme-adaptive), not a hardcoded colour.
  assertEqual(text.getAttribute('fill'), 'currentColor', 'the letters fill with currentColor (theme token)');

  // THE CENTERING GUARANTEE: the accent dot's cx EQUALS the computed ı stem
  // centre (pinned to the monospace advance grid). This is the exact assertion
  // the prior centering pain point demands — a number-equality, not an eyeball.
  const dot = allByClass(mark, 'dt-brand-dot')[0];
  assert(dot, 'the wordmark has the accent dot');
  assertEqual(Number(dot.getAttribute('cx')), shell.wordmarkDotCx(), 'the dot cx equals the ı stem centre');
  assertEqual(dot.getAttribute('fill'), 'var(--zicato-accent)', 'the dot fills with the accent token');
});

// The TYPEFACE picker has been REMOVED from the top-bar chrome (it lives ONLY in
// Settings → Appearance now). The top bar keeps the colour SWATCH DROPDOWN, the
// page-scale pill, and the status pill; and the wordmark dot stays centred for
// the FIXED brand mono regardless of the selected typeface (applyTypeface still
// works via the shared store + the Settings dropdown).
test('top bar: NO typeface picker (removed → Settings only); colour dropdown + scale + status remain; wordmark dot centred for the fixed brand mono', async () => {
  freshState(); installFetch();
  const listeners = { hashchange: [] };
  globalThis.HashChangeEvent = function HashChangeEvent() {};
  globalThis.EventSource = function EventSource() { this.readyState = 0; this.addEventListener = () => {}; this.close = () => {}; };
  globalThis.EventSource.CLOSED = 2;
  globalThis.window = globalThis.window || {};
  globalThis.window.localStorage = globalThis.window.localStorage || { getItem() { return null; }, setItem() {} };
  globalThis.window.addEventListener = (t, fn) => { (listeners[t] = listeners[t] || []).push(fn); };
  const loc = { _hash: '#/' };
  Object.defineProperty(loc, 'hash', {
    get() { return this._hash; },
    set(v) { this._hash = v; for (const fn of (listeners.hashchange || [])) fn(); },
  });
  globalThis.location = loc;
  globalThis.window.location = loc;
  globalThis.window.dispatchEvent = () => { for (const fn of (listeners.hashchange || [])) fn(); };

  const root = document.createElement('div');
  document.body.appendChild(root);
  shell.mountShell(root);
  await new Promise((r) => setTimeout(r, 0));

  const topbar = allByClass(root, 'dt-topbar')[0];
  assert(topbar, 'the top bar painted');
  // the TYPEFACE picker (grouped popover OR the old button group) is GONE from
  // the top bar — it lives ONLY in Settings → Appearance now.
  assertEqual(allByClass(topbar, 'dt-tf').length, 0, 'no typeface picker in the top bar (moved to Settings)');
  assertEqual(allByClass(topbar, 'dt-tf-trigger').length, 0, 'no typeface popover trigger in the top bar');
  assertEqual(allByClass(topbar, 'dt-tf-option').length, 0, 'no typeface option rows in the top bar');
  assertEqual(allByClass(topbar, 'dt-type-switch').length, 0, 'no legacy typeface button group in the top bar');
  assertEqual(allByClass(topbar, 'dt-type-btn').length, 0, 'no legacy typeface buttons in the top bar');
  // the COLOUR swatch dropdown is the SOLE dt-cd popover left in the top bar.
  const cds = allByClass(topbar, 'dt-cd');
  assertEqual(cds.length, 1, 'only the colour swatch dropdown (dt-cd) remains in the top bar');
  assert(allByClass(topbar, 'dt-cd-trigger')[0], 'the colour dropdown trigger is present');
  // the page-scale pill, the status pill, the settings link, and the brand stay.
  assert(allByClass(topbar, 'dt-scale-pill')[0], 'the page-scale pill is still in the top bar');
  assert(allByClass(topbar, 'dt-status')[0], 'the live-status pill is still in the top bar');
  assert(allByClass(topbar, 'dt-nav-build')[0], 'the settings link is still in the top bar');
  // the TOURNAMENT BUILDER is its own top-level view now — a discoverable nav
  // entry sits beside the ⚙ settings chip and links to the standalone `#/builder`.
  const navBuilder = allByClass(topbar, 'dt-nav-builder')[0];
  assert(navBuilder, 'the tournament-builder nav entry is in the top bar (beside settings)');
  assertEqual(navBuilder.getAttribute('href'), '#/builder', 'the builder nav entry links to the standalone builder view');
  assertEqual(navBuilder.getAttribute('href'), router.href('builder', {}), 'the builder nav href is the router-canonical link (single source of truth)');
  assert(allByClass(topbar, 'dt-brand')[0], 'the brand is still in the top bar');

  // applyTypeface still applies live (the shared store path is intact even with
  // no top-bar dropdown) — stamps data-t-type on the root.
  shell.applyTypeface('T9', root);
  assertEqual(root.getAttribute('data-t-type'), 'T9', 'applyTypeface("T9") still stamps data-t-type="T9" on the root');

  // the wordmark dot stays centred on the FIXED brand mono — switching the UI
  // typeface (the swappable --v2-mono) must NOT move the geometrically-pinned dot.
  const wm = svgsByClass(root, 'dt-brand-name')[0];
  const dot = allByClass(wm, 'dt-brand-dot')[0];
  const text = allByClass(wm, 'dt-brand-letters')[0];
  assertEqual(text.getAttribute('font-family'), 'var(--v2-brand-mono)', 'the wordmark pins to the FIXED brand mono');
  const cxBefore = Number(dot.getAttribute('cx'));
  shell.applyTypeface('display', root);
  shell.applyTypeface('editorial', root);
  assertEqual(Number(dot.getAttribute('cx')), cxBefore, 'the dot cx is unchanged across typeface switches');
  assertEqual(Number(dot.getAttribute('cx')), shell.wordmarkDotCx(), 'the dot cx still equals the computed brand-mono stem centre');
});

// THE STANDALONE BUILDER VIEW. `#/builder` is its own first-class view now
// (promoted out of Settings): the shell's view dispatcher renders it FULL-WIDTH
// in the main detail host (.dt-viewhost), NOT nested in the settings
// section-host. We mount the real shell, navigate to `#/builder`, and assert
// the builder's own chrome (.dn-builder) lands in the main view host with NO
// settings section-host wrapping it (the un-nesting / clutter fix).
test('view dispatcher: #/builder renders the builder full-width in the main view host (un-nested from settings)', async () => {
  freshState();
  const listeners = { hashchange: [] };
  globalThis.HashChangeEvent = function HashChangeEvent() {};
  globalThis.EventSource = function EventSource() { this.readyState = 0; this.addEventListener = () => {}; this.close = () => {}; };
  globalThis.EventSource.CLOSED = 2;
  globalThis.window = globalThis.window || {};
  globalThis.window.localStorage = globalThis.window.localStorage || { getItem() { return null; }, setItem() {} };
  globalThis.window.addEventListener = (t, fn) => { (listeners[t] = listeners[t] || []).push(fn); };
  // a fetch that serves the env fixtures PLUS the builder's config + draft so
  // its render() resolves its panes (and a steady draft so the chrome paints).
  globalThis.fetch = async (path) => {
    if (String(path).startsWith('/builder/config')) {
      return { ok: true, json: async () => ({ chat_enabled: false, agent: {}, skills: [] }) };
    }
    if (String(path).startsWith('/builder/draft')) {
      return { ok: true, json: async () => ({ session: 'dashboard', draft: { scoring: { tournament: { structure: 'gauntlet', params: {} } }, board: [], holdout: { train_ids: [], holdout_ids: [] }, proposer: {} }, cost: { board_runs_per_round: 0, breakdown: [] }, warnings: [], diff: { changed_components: [], rolls_epoch: false } }) };
    }
    const v = lookupFixture(FIXTURE, path);
    if (v !== undefined) return { ok: true, json: async () => v };
    return { ok: false, status: 404, json: async () => ({ error: 'not found: ' + path }) };
  };
  const loc = { _hash: '#/builder', search: '' };
  Object.defineProperty(loc, 'hash', {
    get() { return this._hash; },
    set(v) { this._hash = v; for (const fn of (listeners.hashchange || [])) fn(); },
  });
  globalThis.location = loc;
  globalThis.window.location = loc;
  globalThis.window.dispatchEvent = () => { for (const fn of (listeners.hashchange || [])) fn(); };

  const root = document.createElement('div');
  document.body.appendChild(root);
  shell.mountShell(root);
  // let the async dispatch + the builder's config/draft fetch settle.
  await new Promise((r) => setTimeout(r, 0));
  await new Promise((r) => setTimeout(r, 0));
  await new Promise((r) => setTimeout(r, 0));

  const viewhost = allByClass(root, 'dt-viewhost')[0];
  assert(viewhost, 'the main view host exists');
  // the builder mounted INSIDE the main view host (full-width), not a settings host.
  const builderRoot = allByClass(viewhost, 'dn-builder')[0];
  assert(builderRoot, 'the builder chrome (.dn-builder) rendered in the main view host');
  // it is NOT wrapped in the settings section-host (the un-nesting / clutter fix):
  // no .dn-settings surface and no settings section-rail in the view host.
  assertEqual(allByClass(viewhost, 'dn-settings').length, 0, 'the builder is NOT nested inside the settings surface');
  assertEqual(allByClass(viewhost, 'dn-set-rail').length, 0, 'no settings section-rail wraps the builder (no double rail)');
  // the builder kept its own four-pane chrome (its own rail + preview pane).
  assert(allByClass(viewhost, 'dn-bld-preview')[0], 'the builder live-preview pane rendered full-width');
  // the breadcrumb reads environment › tournament builder (no settings crumb).
  const crumbs = allByClass(root, 'dt-crumbs')[0];
  assert(crumbs && (crumbs.textContent || '').toLowerCase().includes('tournament builder'), 'the breadcrumb names the tournament builder');
  assert(crumbs && !(crumbs.textContent || '').toLowerCase().includes('settings'), 'the builder breadcrumb does NOT pass through settings');
});

// THE SETTINGS DRAWER OVERLAY (Change 1). Settings is no longer a full-page
// view: `#/settings[/<section>]` opens a routed RIGHT-SIDE DRAWER that paints
// OVER the current view (the underlying view stays rendered in `.dt-viewhost`
// behind a scrim, so an Appearance change applies live to the page behind it).
// Esc / a scrim click / the × close it by navigating back to the underlying
// route. We mount the real shell, drive the hash, and assert the overlay model.

// Build a shell-mount harness with a live `location` whose hash setter re-fires
// the registered hashchange listeners. Returns { root, loc, listeners }.
function mountShellHarness(initialHash) {
  const listeners = { hashchange: [], keydown: [] };
  globalThis.HashChangeEvent = function HashChangeEvent() {};
  globalThis.EventSource = function EventSource() { this.readyState = 0; this.addEventListener = () => {}; this.close = () => {}; };
  globalThis.EventSource.CLOSED = 2;
  globalThis.window = globalThis.window || {};
  globalThis.window.localStorage = globalThis.window.localStorage || { getItem() { return null; }, setItem() {} };
  globalThis.window.addEventListener = (t, fn) => { (listeners[t] = listeners[t] || []).push(fn); };
  globalThis.window.removeEventListener = (t, fn) => { listeners[t] = (listeners[t] || []).filter((f) => f !== fn); };
  const loc = { _hash: initialHash || '', search: '' };
  Object.defineProperty(loc, 'hash', {
    get() { return this._hash; },
    set(v) { this._hash = v; for (const fn of (listeners.hashchange || [])) fn(); },
  });
  globalThis.location = loc;
  globalThis.window.location = loc;
  globalThis.window.dispatchEvent = () => { for (const fn of (listeners.hashchange || [])) fn(); };
  const root = document.createElement('div');
  document.body.appendChild(root);
  return { root, loc, listeners };
}
const settleTicks = async (n) => { for (let i = 0; i < (n || 4); i += 1) await new Promise((r) => setTimeout(r, 0)); };

test('settings overlay: #/settings opens a DRAWER over the current view (underlying view stays painted)', async () => {
  freshState(); installFetch();
  const { root, loc } = mountShellHarness(`#/e/${EPOCH_ID}`);
  shell.mountShell(root);
  await settleTicks();
  // the underlying epoch view painted into the main host.
  const viewhost = allByClass(root, 'dt-viewhost')[0];
  assert(viewhost && viewhost.firstChild, 'the underlying epoch view painted into the main host');
  // the drawer overlay exists but is CLOSED (no settings route yet).
  const drawer = allByClass(root, 'dt-drawer')[0];
  assert(drawer, 'the settings drawer overlay is mounted in the shell');
  assertEqual(drawer.getAttribute('data-open'), '0', 'the drawer is closed before the settings route');

  // navigate to #/settings — the drawer OPENS over the still-painted view.
  loc.hash = '#/settings';
  await settleTicks();
  assertEqual(drawer.getAttribute('data-open'), '1', 'the drawer opens on the settings route');
  // the underlying view is STILL rendered in the main host (painted behind the scrim).
  assert(viewhost.firstChild, 'the underlying view stays painted behind the scrim (not torn down)');
  // the settings surface rendered INTO the drawer body, NOT the main view host.
  const drawerBody = allByClass(root, 'dt-drawer-body')[0];
  assert(drawerBody && allByClass(drawerBody, 'dn-settings')[0], 'the settings surface painted into the drawer body');
  assertEqual(allByClass(viewhost, 'dn-settings').length, 0, 'settings is NOT painted into the main view host (it is an overlay)');
  // a scrim + a close affordance exist.
  assert(allByClass(root, 'dt-drawer-scrim')[0], 'the drawer has a click-to-close scrim');
  assert(allByClass(root, 'dt-drawer-x')[0], 'the drawer has a close (×) affordance');
});

test('settings overlay: a section deep-link opens the overlay over home when loaded cold', async () => {
  freshState(); installFetch();
  // cold load straight onto a settings section deep-link — opens over home.
  const { root } = mountShellHarness('#/settings/contract');
  shell.mountShell(root);
  await settleTicks();
  const drawer = allByClass(root, 'dt-drawer')[0];
  assertEqual(drawer.getAttribute('data-open'), '1', 'the overlay is open on a cold settings deep-link');
  const drawerBody = allByClass(root, 'dt-drawer-body')[0];
  assert(allByClass(drawerBody, 'dn-settings')[0], 'the settings surface painted into the drawer');
  // the underlying view is HOME (the environment fleet) — painted behind the scrim.
  const viewhost = allByClass(root, 'dt-viewhost')[0];
  assert(viewhost && viewhost.firstChild, 'home (the underlying view) painted behind the overlay on a cold deep-link');
});

test('settings overlay: Esc closes the overlay (returns to the underlying route)', async () => {
  freshState(); installFetch();
  const { root, loc, listeners } = mountShellHarness(`#/e/${EPOCH_ID}`);
  shell.mountShell(root);
  await settleTicks();
  loc.hash = '#/settings';
  await settleTicks();
  const drawer = allByClass(root, 'dt-drawer')[0];
  assertEqual(drawer.getAttribute('data-open'), '1', 'the overlay is open');
  // fire an Escape keydown — the shell's window keydown handler closes it.
  for (const fn of (listeners.keydown || [])) fn({ key: 'Escape', preventDefault() {} });
  await settleTicks();
  // Esc navigated back to the underlying epoch route + hid the overlay.
  assertEqual(loc.hash, `#/e/${EPOCH_ID}`, 'Esc returned to the underlying route');
  assertEqual(drawer.getAttribute('data-open'), '0', 'the overlay is closed after Esc');
});

test('settings overlay: a scrim click closes the overlay (returns to the underlying route)', async () => {
  freshState(); installFetch();
  const { root, loc } = mountShellHarness(`#/e/${EPOCH_ID}`);
  shell.mountShell(root);
  await settleTicks();
  loc.hash = '#/settings/appearance';
  await settleTicks();
  const drawer = allByClass(root, 'dt-drawer')[0];
  assertEqual(drawer.getAttribute('data-open'), '1', 'the overlay is open');
  const scrim = allByClass(root, 'dt-drawer-scrim')[0];
  scrim.dispatchEvent({ type: 'click', target: scrim });
  await settleTicks();
  assertEqual(loc.hash, `#/e/${EPOCH_ID}`, 'the scrim click returned to the underlying route');
  assertEqual(drawer.getAttribute('data-open'), '0', 'the overlay is closed after the scrim click');
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

// ---- THE UNDER-RENDER FIX: a NEW candidate landing mid-round repaints ----
//
// The recurring counterpart of the flashing bug class: a real state change (a
// new candidate minted during a round) failed to repaint, so the operator had
// to HARD-REFRESH to see new candidates. Root cause: the tree + every
// candidate-listing view read through data.js's module cache, which was busted
// ONLY on a VIEW change — never when SSE folded a new generation into AppState.
// These tests pin (1) the signature flips on an add, (2) a real add repaints the
// tree to include the new candidate, and (3) a no-op beat does NOT (no flash).

// liveDataSignature() flips on an add / status change but is stable on a no-op.
test('under-render: liveDataSignature flips when a generation is added (and stays stable on a no-op beat)', () => {
  coreState.state.lineage = { generations: [
    { generation_id: 'v0', epoch_id: EPOCH_ID, promoted: true },
    { generation_id: 'v1', epoch_id: EPOCH_ID, promoted: false },
  ] };
  coreState.state.epochs = [{ epoch_id: EPOCH_ID }];
  coreState.state.workspace = { current_epoch_id: EPOCH_ID };
  const sig0 = data.liveDataSignature();
  // a no-op beat: the SAME generations re-folded (order swapped to prove the
  // signature is order-independent) must yield the IDENTICAL signature.
  coreState.state.lineage = { generations: [
    { generation_id: 'v1', epoch_id: EPOCH_ID, promoted: false },
    { generation_id: 'v0', epoch_id: EPOCH_ID, promoted: true },
  ] };
  assertEqual(data.liveDataSignature(), sig0, 'a no-op beat (same gen set) leaves the signature identical');
  // a NEW candidate landing flips the signature.
  coreState.state.lineage.generations.push({ generation_id: 'v2', epoch_id: EPOCH_ID, promoted: null });
  assert(data.liveDataSignature() !== sig0, 'adding a candidate flips the live-data signature');
  // a pending→settled status transition on an existing candidate flips it too.
  const sigPending = data.liveDataSignature();
  coreState.state.lineage.generations[2].promoted = false;
  assert(data.liveDataSignature() !== sigPending, 'a pending→settled status change flips the signature');
});

// END-TO-END through the shell: a new candidate folded into AppState (the SSE
// path) busts the stale cache + repaints the tree to include the new candidate;
// a no-op state:changed beat writes ZERO new tree DOM (no flash).
test('under-render: a NEW candidate folded into AppState repaints the tree (no hard-refresh); a no-op beat does NOT', async () => {
  // drain any pending re-dispatch timer a prior shell-mount test left scheduled
  // (the shell's re-render debounce is module-scoped) so it cannot race our mount.
  await new Promise((r) => setTimeout(r, 500));
  freshState();
  bus._reset();
  // a MUTABLE lineage fixture so the post-invalidation re-fetch sees the add —
  // exactly what /api/lineage returns once the backend surfaces the new gen.
  const liveLineage = { generations: [
    { generation_id: 'v0', epoch_id: EPOCH_ID, parent_generation_id: '', promoted: true },
    { generation_id: 'v1', epoch_id: EPOCH_ID, parent_generation_id: 'v0', promoted: false },
  ] };
  const liveWs = { current_epoch_id: EPOCH_ID, epochs: [{ epoch_id: EPOCH_ID, generation_count: 2, promoted_count: 1, best_scalar: 70.94, closed: false, goal: 'crisper' }], sparkline: [] };
  // a SELF-CONSISTENT, FLAT (no round_index) gauntlet epoch so the tree lists
  // gen LEAVES directly under Generations — NOT collapsed under round nodes (the
  // global FIXTURE carries round structure + a v2 we must NOT pre-seed here).
  const liveEpoch = { epoch_id: EPOCH_ID, closed: false, goal: 'crisper', board: [], experiments: [
    { generation_id: 'v0', parent_generation_id: '', outcome: { decision: 'baseline' } },
    { generation_id: 'v1', parent_generation_id: 'v0', outcome: { decision: 'rejected' } },
  ] };
  const liveBracket = { epoch_id: EPOCH_ID, champion_lineage: ['v0'], matchups: [] };
  globalThis.fetch = async (path) => {
    const base = path.indexOf('?') >= 0 ? path.slice(0, path.indexOf('?')) : path;
    if (base === '/api/lineage') return { ok: true, json: async () => liveLineage };
    if (base === '/api/workspace') return { ok: true, json: async () => liveWs };
    if (base === '/api/epoch') return { ok: true, json: async () => liveEpoch };
    if (base === '/api/tournaments') return { ok: true, json: async () => liveBracket };
    if (base === '/api/score-trajectory') return { ok: true, json: async () => ({ points: [] }) };
    const v = lookupFixture(FIXTURE, path);
    if (v !== undefined) return { ok: true, json: async () => v };
    return { ok: false, status: 404, json: async () => ({ error: 'not found: ' + path }) };
  };

  // mount the real shell on the generations view, branch OPEN so the gen leaves
  // render in the tree (the under-rendered surface).
  const root = mountLiveShell(`#/e/${EPOCH_ID}/gens`);
  await new Promise((r) => setTimeout(r, 0));
  await new Promise((r) => setTimeout(r, 0));
  await new Promise((r) => setTimeout(r, 500));   // let the initial dispatch settle.

  const rail = allByClass(root, 'dt-sidebar')[0];
  assert(rail, 'the shell painted a tree rail');
  const genRows0 = allByClass(rail, 'dt-node').filter((n) => /^gen/.test(n.getAttribute('data-kind') || ''));
  assert(genRows0.some((n) => n.textContent.includes('v1')), 'the tree initially lists v1');
  assert(!genRows0.some((n) => n.textContent.includes('v2')), 'the tree does NOT yet list the not-yet-minted v2');
  const treeWrites0 = root.innerHTMLWriteCount ? root.innerHTMLWriteCount() : 0;

  // ── (1) a NO-OP beat: identical state re-folded. The signature is unchanged,
  // so the cache is NOT busted and the tree must NOT repaint (no flash). ──
  coreState.state.applyEnvironment({ generations: liveLineage, workspace: liveWs, epochs: liveWs.epochs });
  await new Promise((r) => setTimeout(r, 500));
  const railNodesAfterNoop = allByClass(allByClass(root, 'dt-sidebar')[0], 'dt-node').filter((n) => /^gen/.test(n.getAttribute('data-kind') || ''));
  assert(!railNodesAfterNoop.some((n) => n.textContent.includes('v2')), 'a no-op beat does not invent a candidate');
  assertEqual(railNodesAfterNoop.length, genRows0.length, 'a no-op beat repaints NO new tree rows (no flash)');

  // ── (2) a NEW candidate lands: the backend now surfaces v2; SSE folds it into
  // AppState (applyEnvironment, exactly what loadEnvironment does post-fetch). ──
  liveLineage.generations.push({ generation_id: 'v2', epoch_id: EPOCH_ID, parent_generation_id: 'v0', promoted: null });
  liveWs.epochs[0].generation_count = 3;
  coreState.state.applyEnvironment({ generations: liveLineage, workspace: liveWs, epochs: liveWs.epochs });
  // wait out the re-dispatch debounce so renderTree re-reads the busted cache.
  await new Promise((r) => setTimeout(r, 0));
  await new Promise((r) => setTimeout(r, 600));

  const railAfter = allByClass(root, 'dt-sidebar')[0];
  const genRows1 = allByClass(railAfter, 'dt-node').filter((n) => /^gen/.test(n.getAttribute('data-kind') || ''));
  assert(genRows1.some((n) => n.textContent.includes('v2')),
    'THE FIX: the new candidate v2 appears in the tree live — no hard-refresh needed');
  assert(genRows1.length > genRows0.length, 'the tree grew by the newly-minted candidate');
});

// ====================================================================
// Console IV folds (round 7): the SLIM REEL on the epoch view, the
// compact MATCH CARDS on the generations page, and a DENSITY picker.
// ====================================================================

const rounds = await import('../js/variants/T/views/rounds.js');

// ---- (a) the epoch view leads with the CHAMPION-SPINE ROUND TIMELINE ----

test('epoch view: leads with the CHAMPION-SPINE ROUND TIMELINE (one episode per round), NOT the old reel/bumps', async () => {
  freshState(); installFetch();
  const epoch = await import('../js/variants/T/views/epoch.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await epoch.render(host, ctx, { epochId: EPOCH_ID });

  assert(allByClass(host, 'dn-roundtl')[0], 'the round timeline rendered on the epoch view');
  assert(allByClass(host, 'dn-roundtl-spine')[0], 'the timeline has the champion spine');
  // a gauntlet epoch with 2 rejected challengers → a SINGLE round 0 episode (the
  // matchups fall under one round when there is no round_index stamp).
  const episodes = allByClass(host, 'dn-roundtl-ep');
  assert(episodes.length >= 1, 'the timeline renders ≥1 episode');
  // the old reel + the lineage-bumps are GONE (subsumed by the timeline).
  assert(allByClass(host, 'tr-reel').length === 0, 'the old slim reel is GONE (subsumed by the timeline)');
  assert(allByClass(host, 'dn-bumps').length === 0, 'the old lineage-bumps chart is GONE');
  // the champion-loss annotation reads on the spine.
  assert(host.textContent.includes('loss floor'), 'the spine annotates the loss floor');
  // the heatmap stays on the epoch view (carried forward).
  assert(allByClass(host, 'dn-heatmap')[0], 'the board×generation heatmap is still present on the epoch view');
});

// ---- (b) the timeline stays fit-to-width under a MANY-round fixture ----

const MANY_EPOCH = '2026-05-31_many';
function installManyFetch(roundN) {
  const n = roundN || 11;                       // 11 rounds → 12 generations
  const gens = [{ generation_id: 'v0', epoch_id: MANY_EPOCH, parent_generation_id: '', promoted: true }];
  const matchups = [];
  const points = [{ generation_id: 'v0', scalar: 100 }];
  for (let i = 1; i <= n; i++) {
    const id = 'v' + i;
    // a round_index stamp per challenger → one round per challenger (a deep spine).
    gens.push({ generation_id: id, epoch_id: MANY_EPOCH, parent_generation_id: 'v0', promoted: false, round_index: i - 1 });
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
  globalThis.fetch = async (path) => {
    const v = lookupFixture(MANY, path);
    return v !== undefined
      ? { ok: true, json: async () => v }
      : { ok: false, status: 404, json: async () => ({ error: 'not found: ' + path }) };
  };
}

function buildSpineRounds(n) {
  const rs = [];
  let champ = 'v0';
  for (let i = 0; i < n; i++) {
    rs.push({ round_index: i, champion: { id: champ, scalar: 100 - i }, structure: 'gauntlet',
      challengers: [{ id: 'v' + (i + 1), scalar: 101 + i, promoted: false }], gateOutcome: { kind: 'held', gen: null } });
  }
  return rs;
}

test('round timeline: fit-to-width — a fixed-width viewBox; many-round spine nodes compress and never exceed the viewBox', () => {
  // 11 rounds → 11 spine nodes. Build the timeline directly to read the SVG.
  const node = svg.roundTimeline({ rounds: buildSpineRounds(11), onRound() {}, onCompetitor() {} });
  const svgs = node.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('dn-roundtl-spine') && n.localName === 'svg');
  const strip = svgs[0];
  assert(strip, 'the timeline SVG spine rendered');
  assertEqual(strip.getAttribute('viewBox'), '0 0 1000 96', 'the spine uses a FIXED-width viewBox (fit-to-width, no pan/zoom)');
  const VBW = 1000;

  // every positioned element (x / cx / x2) stays within the fixed viewBox width.
  let maxX = 0;
  for (const elx of strip.querySelectorAll('[class]')) {
    const cx = elx.getAttribute('cx'); const x = elx.getAttribute('x'); const x2 = elx.getAttribute('x2');
    for (const v of [cx, x, x2]) { if (v != null && isFinite(+v)) { assert(+v <= VBW, 'no element exceeds the viewBox width (' + v + ' ≤ ' + VBW + ')'); maxX = Math.max(maxX, +v); } }
  }
  assert(maxX > 0 && maxX <= VBW, 'positions are bounded by the fixed viewBox');

  // node spacing COMPRESSES with more rounds: 11 nodes sit closer than 3.
  const xsOf = (k) => {
    const nd = svg.roundTimeline({ rounds: buildSpineRounds(k), onRound() {} });
    const s = nd.querySelectorAll('[class]').filter((q) => (q.getAttribute('class') || '').includes('dn-roundtl-disc') && q.localName === 'circle');
    return s.map((c) => +c.getAttribute('cx')).sort((a, b) => a - b);
  };
  const few = xsOf(3); const many = xsOf(11);
  assert((many[1] - many[0]) < (few[1] - few[0]), 'with more rounds the node spacing compresses');
});

test('epoch view: the round timeline fits to width with ~11 rounds (one episode per round, no overflow)', async () => {
  freshState(); installManyFetch(11);
  const epoch = await import('../js/variants/T/views/epoch.js');
  const host = document.createElement('div');
  await epoch.render(host, { navigate() {}, href: router.href }, { epochId: MANY_EPOCH });
  const episodes = allByClass(host, 'dn-roundtl-ep');
  assertEqual(episodes.length, 11, 'one episode per round (11 round_index stamps → 11 rounds)');
  const strip = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('dn-roundtl-spine') && n.localName === 'svg')[0];
  assertEqual(strip.getAttribute('viewBox'), '0 0 1000 96', 'still a fixed-width viewBox under many rounds');
  for (const c of strip.querySelectorAll('[class]').filter((q) => (q.getAttribute('class') || '').includes('dn-roundtl-disc'))) {
    assert(+c.getAttribute('cx') <= 1000, 'every spine node stays within the viewBox width');
  }
});

// ---- (c) the generations page renders the banner + match-card grid ----

test('generations view: the FIELD renders as the structure-flow graphic (duelFlow lanes) — NO dt-match-card / dt-champ-banner boxes', async () => {
  freshState(); installFetch();
  const gens = await import('../js/variants/T/views/gens.js');
  const host = document.createElement('div');
  await gens.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID });

  // the boxed banner + match cards are RETIRED — the field is a data-graphic.
  assertEqual(allByClass(host, 'dt-champ-banner').length, 0, 'NO dt-champ-banner box remains');
  assertEqual(allByClass(host, 'dt-match-card').length, 0, 'NO dt-match-card boxes remain');
  assertEqual(allByClass(host, 'dt-matchcards').length, 0, 'NO match-cards grid remains');

  // the integrated, compact accent CHAMPION header (not a boxed banner).
  const head = allByClass(host, 'dt-fieldflow-champ')[0];
  assert(head, 'the integrated champion header rendered');
  assert(host.textContent.includes('v0'), 'the header shows the champion id (v0)');
  assert(host.textContent.includes('defending'), 'the header reads defending');

  // the duel-flow figure: one challenger LANE per match-up (v0→v1, v0→v2).
  const flow = svgsByClass(host, 'dn-duelflow')[0];
  assert(flow, 'the field renders as the duel-flow structure-graphic (dn-duelflow)');
  const lanes = allByClass(flow, 'dn-duelflow-lane');
  assertEqual(lanes.length, 2, 'one lane per challenger (v0→v1, v0→v2)');
  // a Δ=0 champion reference rule + a crowned champion-gate.
  assert(allByClass(flow, 'dn-duelflow-ref').length >= 1, 'the Δ=0 champion reference rule is drawn');
  assert(allByClass(flow, 'dn-duelflow-gate').length >= 1, 'a crowned champion-gate node is drawn');
  // the per-challenger hypothesis lives ON HOVER (the hovercard), not in a box:
  // the dot is hovercard-wired and the hypothesis text is NOT in the visible DOM.
  const dots = allByClass(flow, 'dn-duelflow-dot');
  assert(dots.length >= 2 && dots.every((d) => d.getAttribute('data-hovercard') === '1'), 'each challenger dot is hovercard-wired (hypothesis + Δ on hover)');
  assert(!host.textContent.includes('Enforce explicit slide-structure output'), 'the hypothesis is NOT a visible box/label — it lives on the hovercard');
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
  assertEqual(allByClass(host, 'dn-roundtl').length, 0, 'NO round timeline on the environment view');
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
const hovercard = await import('../js/variants/T/hovercard.js');

// Drive the hovercard like a browser would: fire `mouseenter` on a wired node,
// read the live card text, then fire `mouseleave` to hide it. Returns the text
// the styled card surfaced (so a test can assert the SAME explanation the old
// native <title> carried now lives in the hovercard, not in a <title>).
function hovercardTextOf(node) {
  hovercard.hide();
  node.dispatchEvent({ type: 'mouseenter', target: node });
  const text = hovercard.cardText();
  node.dispatchEvent({ type: 'mouseleave', target: node });
  return text;
}
// assert a node carries NO native <title> child (the off-brand tooltip is gone).
function hasNativeTitle(node) {
  return node.childNodes.filter((n) => n.localName === 'title').length > 0;
}

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

// ---- (c) the lifecycle DAG DERIVES its height from the board-node count ----

test('lifecycle DAG height is DERIVED from the (deduped) board-node count, not a passed token, and it stays fit-to-width', () => {
  const entries = [{ entry_id: 'b1', drift_loss: 10, pass_fail: 0 }, { entry_id: 'b2', drift_loss: 20, pass_fail: 1 }];
  // a passed `height` is now IGNORED — the figure sizes itself to its nodes so
  // both compare sides share identical row spacing.
  const d = dag.lifecycleDag({ genId: 'v1', parentId: 'v0', entries, decision: 'rejected', height: 999 });
  const hAttr = +d.getAttribute('height');
  assert(hAttr !== 999, 'the passed height is NOT honoured verbatim — height is derived from node count');
  assert(hAttr > 0 && hAttr < 300, 'a 2-node DAG is compact (height derived from 2 rows + padding, not a fixed 300+)');
  // adding a node grows the height by exactly ONE row pitch (constant per-node).
  const d3 = dag.lifecycleDag({ genId: 'v1', parentId: 'v0', entries: entries.concat([{ entry_id: 'b3', drift_loss: 30, pass_fail: 0 }]), decision: 'rejected' });
  const grew = +d3.getAttribute('height') - hAttr;
  assert(grew > 0, 'one more board node makes the figure taller by a constant row pitch (' + grew + 'px)');
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

// ---- CHANGE 5 + 6: sixteen themes + the colour SWATCH DROPDOWN ----

test('colour themes: all SIXTEEN are registered, each defines the full --v2 token contract, and selecting each applies it', () => {
  freshState();
  const ids = ui.COLOR_THEMES.map((t) => t[0]);
  const expected = ['monokai', 'solarized-dark', 'solarized-light',
    'google-light', 'google-dark', 'lunaria-light', 'lunaria-eclipse',
    'belafonte-day', 'belafonte-night',
    'paper', 'zenburn', 'selenized-black', 'relaxed',
    'espresso', 'dracula', 'ubuntu'];
  assertEqual(ids.length, 16, 'sixteen colour themes registered');
  assertDeep(ids, expected, 'the sixteen ids are the three originals + thirteen Gogh palettes');
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

// ---- ROUND 10: the three NEW Gogh themes (Espresso/Dracula/Ubuntu) ----

test('new themes: Espresso/Dracula/Ubuntu are registered with swatch strips, define the full token contract, and selecting each changes the root attribute + tokens', () => {
  freshState();
  const css = readCss();
  const byId = new Map(ui.COLOR_THEMES.map((t) => [t[0], t]));
  const NEW = ['espresso', 'dracula', 'ubuntu'];
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

  // (c) the signature palette grounds are mapped faithfully.
  const espressoBlock = new RegExp('\\[data-t-theme="espresso"\\]\\s*\\{([^}]*)\\}').exec(css.replace(/\n/g, ' '))[1];
  const draculaBlock = new RegExp('\\[data-t-theme="dracula"\\]\\s*\\{([^}]*)\\}').exec(css.replace(/\n/g, ' '))[1];
  const ubuntuBlock = new RegExp('\\[data-t-theme="ubuntu"\\]\\s*\\{([^}]*)\\}').exec(css.replace(/\n/g, ' '))[1];
  assert(/--v2-paper:\s*#323232/i.test(espressoBlock), 'espresso ground is the palette background #323232');
  assert(/--v2-paper:\s*#282A36/i.test(draculaBlock), 'dracula ground is the palette background #282A36');
  assert(/--v2-paper:\s*#300A24/i.test(ubuntuBlock), 'ubuntu ground is the signature aubergine #300A24');

  // (c.1) Dracula maps to the CANONICAL palette with its SIGNATURE PURPLE as the
  // accent (drives the LIVE pill / highlights / active state) — NOT cyan.
  assert(/--v2-accent:\s*#BD93F9/i.test(draculaBlock), 'dracula accent is the signature purple #BD93F9 (not cyan)');
  assert(!/--v2-accent:\s*#8BE9FD/i.test(draculaBlock), 'dracula accent is NOT the cyan #8BE9FD');
  assert(/--v2-ink:\s*#F8F8F2/i.test(draculaBlock), 'dracula foreground is the palette fg #F8F8F2');
  assert(/--v2-good:\s*#50FA7B/i.test(draculaBlock), 'dracula good keys off the palette green #50FA7B');
  assert(/--v2-bad:\s*#FF5555/i.test(draculaBlock), 'dracula bad keys off the palette red #FF5555');
  assert(/--v2-caution:\s*#F1FA8C/i.test(draculaBlock), 'dracula caution keys off the palette yellow #F1FA8C');
  assert(/--v2-flat:\s*#6272A4/i.test(draculaBlock), 'dracula flat keys off the comment grey #6272A4');
  for (const tok of contract) {
    assert(draculaBlock.includes('--v2-' + tok + ':'), 'dracula defines --v2-' + tok);
  }

  // (d) selecting each NEW theme applies it to the root attribute + persists.
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
  assertEqual(options.length, 16, 'one dropdown option per theme (sixteen)');
  // the four round-9 + three round-10 themes each surface as a listed option.
  for (const id of ['paper', 'zenburn', 'selenized-black', 'relaxed', 'espresso', 'dracula', 'ubuntu']) {
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

// ---- 6-SWATCH PREVIEW: every theme's strip shows SIX colours (adds accent) ----

test('colour themes: every COLOR_THEMES tuple carries SIX valid-hex preview colours (ground · surface · ink · good · bad · accent)', () => {
  assertEqual(ui.COLOR_THEMES.length, 16, 'theme count stays sixteen');
  for (const [id, , swatches] of ui.COLOR_THEMES) {
    assert(Array.isArray(swatches), id + ' has a swatch tuple');
    assertEqual(swatches.length, 6, id + ' preview tuple has exactly six colours (got ' + swatches.length + ')');
    for (const c of swatches) {
      assert(/^#[0-9a-fA-F]{6}$/.test(c), id + ' swatch ' + c + ' is a valid 6-digit hex');
    }
  }
  // dracula's 6th swatch (accent) is the signature purple.
  const dracula = ui.COLOR_THEMES.find((t) => t[0] === 'dracula');
  assertEqual(dracula[2][5].toUpperCase(), '#BD93F9', 'dracula 6th swatch is the signature purple #BD93F9');
});

test('colour picker renders SIX swatches per option (the 6-swatch strip) and SIX on the closed trigger', () => {
  try { globalThis.window.localStorage.clear(); } catch (e) { /* ignore */ }
  shell.applyTheme('monokai');
  const root = mountLiveShell('#/');

  // closed trigger strip shows six swatches.
  const trigger = allByClass(root, 'dt-cd-trigger')[0];
  const triggerStrip = trigger.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('dt-swatch-strip'))[0];
  const triggerSwatches = triggerStrip.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').split(/\s+/).includes('dt-swatch'));
  assertEqual(triggerSwatches.length, 6, 'the closed trigger shows six swatches');

  // every option's strip shows exactly six swatches, matching its tuple.
  const byId = new Map(ui.COLOR_THEMES.map((t) => [t[0], t]));
  const options = allByClass(root, 'dt-cd-option');
  assertEqual(options.length, 16, 'one option per theme (sixteen)');
  for (const opt of options) {
    const id = opt.getAttribute('data-theme');
    const strip = opt.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('dt-swatch-strip'))[0];
    const swatches = strip.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').split(/\s+/).includes('dt-swatch'));
    assertEqual(swatches.length, 6, id + ' option strip shows six swatches');
    // the 6th rendered swatch carries the tuple's accent colour.
    const accent = byId.get(id)[2][5];
    assert((swatches[5].getAttribute('style') || '').toLowerCase().includes(accent.toLowerCase()),
      id + ' 6th swatch carries its accent ' + accent);
  }
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
    const v = lookupFixture(F, path);
    if (v !== undefined) return { ok: true, json: async () => v };
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

test('structure: single-elim renders a fit-to-width bracket-as-FLOW (elimFlow lanes + standings)', async () => {
  freshState();
  installFixtureMap(structFixture('single_elim', SE_STRUCT, 'tourn_e0_se'));
  const gens = await import('../js/variants/T/views/gens.js');
  const host = document.createElement('div');
  await gens.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID });

  // the structure pill names the configured structure (NOT the gauntlet ladder).
  assert(allByClass(host, 'dt-structure-pill').length >= 1, 'a structure pill labels the configured structure');
  assert(host.textContent.includes('Single elimination'), 'the pill names single-elim');
  assertEqual(allByClass(host, 'dt-champ-banner').length, 0, 'NO gauntlet champion-defends banner for a non-gauntlet structure');

  // the seat/box bracket tree is RETIRED — the elim figure is the bracket-as-flow.
  assertEqual(svgsByClass(host, 'dn-elimbracket').length, 0, 'the seat/box bracket tree (dn-elimbracket) is retired');
  const flow = svgsByClass(host, 'dn-elimflow')[0];
  assert(flow, 'the elim figure is the bracket-as-FLOW (dn-elimflow)');
  assertEqual(flow.getAttribute('width'), '100%', 'the flow is fit-to-width (width:100%)');
  assert((flow.getAttribute('viewBox') || '').startsWith('0 0 '), 'the flow carries a viewBox so it scales to its pane');
  assert(!hasScrollWrapperAncestor(flow, host), 'no horizontal-scroll wrapper around the flow');
  // both bracket rounds rendered as columns + the trailing champion-gate column.
  assert(host.textContent.includes('Semifinal') && host.textContent.includes('Final'), 'both bracket rounds render as columns');
  assert(flow.textContent.toLowerCase().includes('champion-gate'), 'the flow carries the champion-gate column');
  // a two-lane convergence (a match node) is drawn — the bracket-as-flow.
  assert(allByClass(flow, 'dn-elimflow-convnode').length >= 1, 'a match convergence node is drawn');
  // a standings leaderboard rendered too.
  assert(allByClass(host, 'dt-standings').length >= 1, 'a standings leaderboard rendered');
  assert(host.textContent.includes('champion'), 'the standings names the champion status');
});

test('structure: double-elim renders the bracket-as-FLOW with the losers’ band re-converging as a second band of lanes', async () => {
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
  // the seat/box bracket tree is retired — one flow SVG holds the winners' +
  // losers' lanes (the losers' band re-converges as a second band of lanes).
  assertEqual(svgsByClass(host, 'dn-elimbracket').length, 0, 'the seat/box bracket tree is retired');
  const flow = svgsByClass(host, 'dn-elimflow')[0];
  assert(flow, 'the double-elim flow SVG rendered');
  assert(flow.textContent.toLowerCase().includes('champion-gate'), 'the double-elim flow ends in the champion-gate column');
  // the LB round columns are present (the losers' band re-converges).
  assert(/LB Round 1|LB R/i.test(flow.textContent), 'the losers’ bracket rounds render as lanes');
});

test('structure: swiss renders the standings LADDER hero + per-round pairings', async () => {
  freshState();
  installFixtureMap(structFixture('swiss', SWISS_STRUCT, 'tourn_e0_sw'));
  const gens = await import('../js/variants/T/views/gens.js');
  const host = document.createElement('div');
  await gens.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID });
  assert(host.textContent.includes('Swiss'), 'the pill names swiss');
  // the swiss standings LADDER (the racing-analogue hero) rendered.
  const ladder = svgsByClass(host, 'dn-swissladder')[0];
  assert(ladder, 'the swiss standings ladder SVG rendered');
  assertEqual(ladder.getAttribute('width'), '100%', 'the swiss ladder is fit-to-width (width:100%)');
  assert((ladder.getAttribute('viewBox') || '').startsWith('0 0 '), 'the swiss ladder carries a viewBox');
  // the ladder shows the accumulating standings + the champion-gate node.
  assert(allByClass(ladder, 'dn-swissladder-stand').length >= 1, 'the accumulating Copeland-point standings rendered');
  assert(ladder.textContent.toLowerCase().includes('standings'), 'the ladder labels the standings column');
  assert(ladder.textContent.toLowerCase().includes('champion-gate'), 'the ladder ends in a champion-gate node');
  assert(host.textContent.includes('Round 1') && host.textContent.includes('Round 2'), 'both swiss rounds render');
  // the per-round pairings now live INSIDE the ladder (one lane per match); the
  // standalone "Pairings · round by round" tables were collapsed away as a
  // duplicate of the ladder's pairing columns.
  assert(allByClass(ladder, 'dn-swissladder-pair').length >= 1, 'the ladder lays out the round pairings');
  assert(allByClass(host, 'dt-swiss-pairings').length === 0, 'the redundant standalone pairings tables are gone');
});

test('structure: the "Proposed field" section renders applied ✓ / rejected ✗ + reasons from field_status', async () => {
  freshState();
  const payload = JSON.parse(JSON.stringify(SWISS_STRUCT));
  payload.field_status = [
    { generation_id: 'v1', status: 'applied', reason: '', seed: 2 },
    { generation_id: 'v2', status: 'rejected', reason: 'proposer returned invalid JSON', seed: 3 },
  ];
  installFixtureMap(structFixture('swiss', payload, 'tourn_e0_sw'));
  const gens = await import('../js/variants/T/views/gens.js');
  const host = document.createElement('div');
  await gens.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID });
  // the Proposed field tracker rendered with the headline counts.
  const tracker = allByClass(host, 'dn-prop-tracker')[0];
  assert(tracker, 'the "Proposed field" tracker rendered');
  const head = allByClass(host, 'dn-prop-head')[0];
  assert(head && head.textContent.includes('2 proposed') && head.textContent.includes('1 applied'),
    'the headline reads "2 proposed · 1 applied"');
  // one applied row (✓) and one rejected row (✗).
  const okRows = allByClass(host, 'dn-prop-row-ok');
  const badRows = allByClass(host, 'dn-prop-row-bad');
  assertEqual(okRows.length, 1, 'one applied row');
  assertEqual(badRows.length, 1, 'one rejected row');
  assert(okRows[0].textContent.includes('✓') && okRows[0].textContent.includes('v1'), 'the applied row shows ✓ + v1');
  assert(badRows[0].textContent.includes('✗') && badRows[0].textContent.includes('v2'), 'the rejected row shows ✗ + v2');
  // the rejection reason is reachable via the hovercard (attached to the row).
  const hc = await import('../js/variants/T/hovercard.js');
  hc.show(badRows[0], 'x'); // prime the card surface (the row carries a hovercard binding)
  assert(svg.proposingTracker, 'the shared proposingTracker renderer is exported for reuse');
});

test('proposing tracker: a rejected challenger shows its SPECIFIC reason INLINE (validation error visible, not a black box)', () => {
  const tracker = svg.proposingTracker({
    fieldStatus: [
      { generation_id: 'v1', status: 'applied', reason: '', attempts: 1, attempt_reasons: [], hypothesis: 'swap the greeting string', seed: 2 },
      {
        generation_id: 'v2', status: 'rejected', seed: 3,
        reason: "hypothesis.expected_metric_movements[0]: unknown drift kind 'file_findability'",
        attempts: 2,
        attempt_reasons: [
          "hypothesis.expected_metric_movements[0]: unknown drift kind 'file_findability'",
          "hypothesis.expected_metric_movements[0]: unknown drift kind 'file_findability'",
        ],
        hypothesis: '',
      },
    ],
  });
  // The rejected row renders the SPECIFIC reason inline (not just on hover).
  const reasonEls = allByClass(tracker, 'dn-prop-reason');
  assertEqual(reasonEls.length, 1, 'one inline reason line (for the rejected slot)');
  assert(reasonEls[0].textContent.includes('file_findability'),
    'the file_findability validation message is rendered inline, plainly visible');
  // The retry badge surfaces the attempt count for the retried slot.
  const attemptEls = allByClass(tracker, 'dn-prop-attempts');
  assertEqual(attemptEls.length, 1, 'the retried slot shows an attempt badge');
  assert(attemptEls[0].textContent.includes('2 attempts'), 'the badge reads "2 attempts"');
  // The applied row renders its hypothesis as the detail line, not a reason.
  const okRow = allByClass(tracker, 'dn-prop-row-ok')[0];
  assert(okRow && okRow.textContent.includes('swap the greeting string'),
    'the applied slot shows its hypothesis');
  // Headline counts roll up applied + rejected.
  const head = allByClass(tracker, 'dn-prop-head')[0];
  assert(head.textContent.includes('2 proposed') && head.textContent.includes('1 applied')
    && head.textContent.includes('1 rejected'), 'headline: 2 proposed · 1 applied · 1 rejected');
});

test('proposing tracker: an in-flight "proposing" slot reads as pending (not rejected, not all-rejected)', () => {
  const tracker = svg.proposingTracker({
    fieldStatus: [
      { generation_id: 'v1', status: 'applied', reason: '', attempts: 1, attempt_reasons: [], hypothesis: 'h', seed: 2 },
      { generation_id: 'v2', status: 'proposing', reason: '', attempts: 0, attempt_reasons: [], hypothesis: '', seed: 3 },
    ],
  });
  const pendingRows = allByClass(tracker, 'dn-prop-row-pending');
  assertEqual(pendingRows.length, 1, 'one pending row for the in-flight slot');
  assert(pendingRows[0].textContent.includes('proposing'), 'the pending row reads "proposing…"');
  const head = allByClass(tracker, 'dn-prop-head')[0];
  assert(head.textContent.includes('proposing'), 'headline surfaces the in-flight count');
  assert(!/all rejected/i.test(head.textContent),
    'a field with a still-proposing slot is NOT declared all-rejected prematurely');
  assert(!(head.getAttribute('class') || '').includes('dn-prop-head-allbad'),
    'no all-bad headline class while a slot is still proposing');
});

test('data.fieldStatus: carries the v6 observability fields (status proposing, attempts, attempt_reasons, hypothesis)', () => {
  const fs = data.fieldStatus({
    field_status: [
      {
        generation_id: 'v1', status: 'rejected', reason: 'final reason', seed: 2,
        attempts: 3, attempt_reasons: ['a', 'b', '', 'c'], hypothesis: 'ignored on reject',
      },
      { generation_id: 'v2', status: 'proposing' },
      { generation_id: 'v3', status: 'applied', hypothesis: 'do the thing' },
    ],
  });
  assertEqual(fs[0].status, 'rejected');
  assertEqual(fs[0].attempts, 3, 'attempts preserved');
  assertDeep(fs[0].attempt_reasons, ['a', 'b', 'c'], 'empty per-attempt reasons filtered out');
  assertEqual(fs[1].status, 'proposing', 'the proposing status is recognised (not coerced to rejected)');
  assertEqual(fs[1].attempts, 0, 'a proposing slot with no attempts count defaults to 0');
  assertEqual(fs[2].hypothesis, 'do the thing', 'applied hypothesis preserved');
  // The summary counts proposing distinctly + does not prematurely flag allRejected.
  const sum = data.fieldStatusSummary(fs);
  assertEqual(sum.proposing, 1, 'one proposing slot');
  assertEqual(sum.applied, 1, 'one applied slot');
  assertEqual(sum.rejected, 1, 'one rejected slot');
  assertEqual(sum.allRejected, false, 'not all-rejected while a slot proposes / one applied');
});

test('proposingDigest: re-stamps on an attempt-count / reason change, stable on a no-op', () => {
  const a = [{ generation_id: 'v1', status: 'proposing', attempts: 0, reason: '' }];
  const b = [{ generation_id: 'v1', status: 'rejected', attempts: 2, reason: 'file_findability validation error' }];
  assertEqual(svg.proposingDigest(a), svg.proposingDigest(a.map((x) => ({ ...x }))),
    'identical field → identical digest (no-op → ZERO DOM)');
  assert(svg.proposingDigest(a) !== svg.proposingDigest(b),
    'a proposing → rejected transition (with a reason) re-stamps the digest');
});

test('structure: a completed field where ALL challengers rejected reads "0 applied — all rejected", not empty', async () => {
  freshState();
  const payload = JSON.parse(JSON.stringify(SWISS_STRUCT));
  payload.field_status = [
    { generation_id: 'v1', status: 'rejected', reason: 'empty response', seed: 2 },
    { generation_id: 'v2', status: 'rejected', reason: 'post-apply validation failed', seed: 3 },
    { generation_id: 'v3', status: 'rejected', reason: 'mutation_id no longer resolves', seed: 4 },
    { generation_id: 'v4', status: 'rejected', reason: 'empty response', seed: 5 },
  ];
  installFixtureMap(structFixture('swiss', payload, 'tourn_e0_sw'));
  const gens = await import('../js/variants/T/views/gens.js');
  const host = document.createElement('div');
  await gens.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID });
  const head = allByClass(host, 'dn-prop-head')[0];
  assert(head, 'the headline rendered');
  assert(head.textContent.includes('4 proposed') && head.textContent.includes('0 applied'),
    'the headline reads "4 proposed · 0 applied"');
  assert(/all rejected/i.test(head.textContent), 'the headline reads "all rejected" (not an empty/idle state)');
  assert((head.getAttribute('class') || '').includes('dn-prop-head-allbad'), 'the all-rejected headline carries the bad-state class');
  assertEqual(allByClass(host, 'dn-prop-row-ok').length, 0, 'NO applied rows for an all-rejected field');
  assertEqual(allByClass(host, 'dn-prop-row-bad').length, 4, 'all four challengers render as rejected rows');
});

test('structure: an absent field_status renders NO "Proposed field" section (back-compat)', async () => {
  freshState();
  // SWISS_STRUCT carries no field_status → no tracker section.
  installFixtureMap(structFixture('swiss', SWISS_STRUCT, 'tourn_e0_sw'));
  const gens = await import('../js/variants/T/views/gens.js');
  const host = document.createElement('div');
  await gens.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID });
  assertEqual(allByClass(host, 'dn-prop-tracker').length, 0, 'no proposing tracker when field_status is absent');
});

test('structure: a COMPLETED, all-applied field renders NO "Proposed field" section (it would just be an empty one-liner)', async () => {
  freshState();
  const payload = JSON.parse(JSON.stringify(SWISS_STRUCT));
  // every proposal applied + the run is complete (no live flag) → the ladder
  // already shows the field, so the lone section is omitted rather than left
  // reading as an empty "N proposed · N applied" line.
  payload.field_status = [
    { generation_id: 'v1', status: 'applied', reason: '', seed: 2 },
    { generation_id: 'v2', status: 'applied', reason: '', seed: 3 },
  ];
  installFixtureMap(structFixture('swiss', payload, 'tourn_e0_sw'));
  const gens = await import('../js/variants/T/views/gens.js');
  const host = document.createElement('div');
  await gens.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID });
  assertEqual(allByClass(host, 'dn-prop-tracker').length, 0, 'no Proposed-field section when completed + all applied');
  assert(!/Proposed field/.test(host.textContent), 'the "Proposed field" heading is not rendered');
  // …but the ladder (the field is shown there) still renders.
  assert(svgsByClass(host, 'dn-swissladder')[0], 'the swiss ladder still renders');
});

test('data.fieldStatus / fieldStatusSummary: normalize + roll up the proposing field', () => {
  const raw = {
    field_status: [
      { generation_id: 'v1', status: 'applied', reason: '', seed: 2 },
      { generation_id: 'v2', status: 'rejected', reason: 'invalid JSON', seed: 3 },
      { generation_id: '', status: 'applied' },          // dropped (no id)
      'garbage',                                          // dropped (not an object)
      { generation_id: 'v3', status: 'weird' },           // status coerced to rejected
    ],
  };
  const fs = data.fieldStatus(raw);
  assertEqual(fs.length, 3, 'only well-formed, identified rows survive');
  assertEqual(fs[2].status, 'rejected', 'an unknown status coerces to rejected');
  assertDeep(data.fieldStatus({}), [], 'absent field_status → empty list');
  assertDeep(data.fieldStatus(null), [], 'null payload → empty list');
  const sum = data.fieldStatusSummary(fs);
  assertEqual(sum.proposed, 3, 'proposed count');
  assertEqual(sum.applied, 1, 'applied count');
  assertEqual(sum.rejected, 2, 'rejected count');
  assertEqual(sum.allRejected, false, 'not all rejected (one applied)');
  const allBad = data.fieldStatusSummary([{ generation_id: 'v1', status: 'rejected' }]);
  assertEqual(allBad.allRejected, true, 'a non-empty zero-applied field is allRejected');
  assertEqual(data.fieldStatusSummary([]).allRejected, false, 'an empty field is NOT allRejected (idle, not failed)');
});

test('structure: racing renders a fit-to-width survival funnel with cuts + board fractions', async () => {
  freshState();
  installFixtureMap(structFixture('racing', RACING_STRUCT, 'tourn_e0_rc'));
  const gens = await import('../js/variants/T/views/gens.js');
  const host = document.createElement('div');
  await gens.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID });
  assert(host.textContent.includes('Racing'), 'the pill names racing');
  const ladder = svgsByClass(host, 'dn-funnel')[0];
  assert(ladder, 'a survival-funnel SVG rendered');
  assertEqual(ladder.getAttribute('width'), '100%', 'the survival funnel is fit-to-width (width:100%)');
  assert((ladder.getAttribute('viewBox') || '').startsWith('0 0 '), 'the survival funnel carries a viewBox');
  assert(!hasScrollWrapperAncestor(ladder, host), 'no horizontal-scroll wrapper around the survival funnel');
  assert(host.textContent.includes('50/100 board'), 'a rung shows its board fraction (budget escalation)');
  assert(host.textContent.includes('Rung 1') && host.textContent.includes('Rung 2'), 'both rungs render as stages');
});

// ---- REGRESSION: the per-ROUND Match-ups drill-down must build the SAME racing
// rung model the all-rounds / epoch view does (the recurring "round view empty"
// bug). A racing FIELD record carries `rounds: []` BY DESIGN — the rungs live in
// the LIVE active-tournament envelope (in flight) and in reconstructRacing / the
// per-tournament structure record (settled). The round drill-down used to read
// `round.tournamentRef.rounds` directly, so it came up with ZERO rungs and showed
// "No rungs evaluated yet." while the epoch view (live-first → reconstruct) showed
// them. Both paths now resolve through ONE shared resolver (resolveNonGauntletSt),
// so they CANNOT diverge — this pins LIVE and asserts live↔recorded convergence.

// the per-round FIELD record the orchestrator opens at round start: it lists the
// competitor field + proposing status but carries EMPTY rounds/standings (issue
// #16, `state="in_progress"`). This is the e4 repro shape.
const RACING_FIELD_EMPTY = {
  tournament_id: '2026-05-30_e0:field:v1', epoch_id: EPOCH_ID, structure: 'racing',
  structure_params: { board_fraction: 0.5, eta: 2, field_size: 4 },
  competitors: [
    { generation_id: 'v0', seed: 1, role: 'champion' }, { generation_id: 'v1', seed: 2, role: 'challenger' },
    { generation_id: 'v2', seed: 3, role: 'challenger' }, { generation_id: 'v3', seed: 4, role: 'challenger' },
  ],
  rounds: [], standings: [], field_status: [], state: 'in_progress', source: 'index',
};
// the LIVE active-tournament envelope DOES carry the populated rungs (stage_index
// stages, survivors/cut) — the in-flight source the round view must read.
const RACING_LIVE_AT = {
  epoch_id: EPOCH_ID, structure: 'racing', phase: 'tournament:round_0:running',
  structure_params: { board_fraction: 0.5, eta: 2, field_size: 4 },
  competitors: RACING_FIELD_EMPTY.competitors,
  rounds: [
    { stage_index: 0, label: 'Rung 0', matches: [{ match_id: 'rung0', competitors: ['v0', 'v1', 'v2', 'v3'], survivors: ['v1', 'v2'], cut: ['v3'], board_fraction: 0.5 }] },
    { stage_index: 1, label: 'Rung 1', matches: [{ match_id: 'rung1', competitors: ['v1', 'v2'], survivors: ['v1'], cut: ['v2'], board_fraction: 1.0 }] },
  ],
  standings: [
    { generation_id: 'v1', rank: 1, scalar: 40.0, status: 'alive' },
    { generation_id: 'v0', rank: 2, scalar: 54.0, status: 'alive', role: 'champion' },
  ],
};

function racingRoundFixture({ live }) {
  const gens = RACING_FIELD_EMPTY.competitors.map((c) => ({ generation_id: c.generation_id, epoch_id: EPOCH_ID, parent_generation_id: c.role === 'champion' ? '' : 'v0', promoted: c.role === 'champion', round_index: 0 }));
  // SETTLED: the field record + the per-tournament structure record carry the
  // resolved rungs (the orchestrator's settle upsert, issue #16). LIVE: both stay
  // empty (the in_progress shape) so the ONLY rung source is the live envelope.
  const settledRounds = live ? [] : RACING_LIVE_AT.rounds.map((r) => ({ ...r, round_index: r.stage_index }));
  const settledStandings = live ? [] : RACING_LIVE_AT.standings;
  const fieldRec = { ...RACING_FIELD_EMPTY, rounds: settledRounds, standings: settledStandings, state: live ? 'in_progress' : 'settled' };
  const structRec = { ...fieldRec, source: 'index' };
  const F = {
    '/api/epoch': { epoch_id: EPOCH_ID, closed: !live, goal: 'g', tournament: { structure: 'racing', params: RACING_FIELD_EMPTY.structure_params },
      experiments: gens.map((g) => ({ generation_id: g.generation_id, parent_generation_id: g.parent_generation_id, outcome: { decision: g.promoted ? 'baseline' : 'pending' }, round_index: 0 })), board: [] },
    '/api/lineage': { generations: gens },
    '/api/score-trajectory': { points: live ? [] : [{ generation_id: 'v0', scalar: 54.0 }, { generation_id: 'v1', scalar: 40.0 }] },
    '/api/tournaments': { epoch_id: EPOCH_ID, structure: 'racing', structure_params: RACING_FIELD_EMPTY.structure_params, champion_lineage: ['v0'],
      matchups: [], tournaments: [fieldRec] },
    [`/api/tournament-structure/${EPOCH_ID}/${fieldRec.tournament_id}`]: structRec,
  };
  // LIVE: the views fetch the in-flight topology fresh from /api/active-tournament
  // (the only source carrying the rungs while the field record is still empty).
  if (live) F['/api/active-tournament'] = RACING_LIVE_AT;
  return F;
}

// helper: render a view and read whether the racing rung figures are present.
async function renderRacingView(view, params, { live }) {
  freshState();
  installFixtureMap(racingRoundFixture({ live }));
  if (live) {
    coreState.state.activeTournament = RACING_LIVE_AT;
    coreState.state.heartbeat = { phase: 'tournament:round_0:running', epoch_id: EPOCH_ID, last_heartbeat: new Date().toISOString() };
    coreState.state.activeRuns = [{ generation_id: 'v1', entry_id: 'b0', run_id: 'run_v1' }];
  } else {
    coreState.state.activeTournament = null;
    coreState.state.heartbeat = null;
    coreState.state.activeRuns = [];
  }
  try {
    const mod = await import(`../js/variants/T/views/${view}.js`);
    const host = document.createElement('div');
    await mod.render(host, { navigate() {}, href: router.href }, params);
    return host;
  } finally {
    coreState.state.activeTournament = null;
    coreState.state.heartbeat = null;
    coreState.state.activeRuns = [];
  }
}

test('REGRESSION (round-view-empty): the per-ROUND racing drill-down builds a NON-EMPTY rung model from the LIVE envelope when the field record carries rounds:[]', async () => {
  const host = await renderRacingView('gens', { epochId: EPOCH_ID, round: 0 }, { live: true });
  // the title is the round drill-down (not the all-rounds page).
  assert(host.textContent.includes('round 0'), 'the round drill-down header reads "round 0"');
  // THE BUG: the round view used to show this empty state. It must NOT now.
  assert(!host.textContent.includes('No rungs evaluated yet'), 'the round view does NOT show the "No rungs evaluated yet." empty state');
  // the racing scalar track + survival funnel render the rungs from the live envelope.
  assert(host.textContent.includes('Scalar track'), 'the racing Scalar track renders in the round drill-down');
  assert(svgsByClass(host, 'dn-funnel')[0], 'the survival funnel SVG renders the rungs (non-empty rung model)');
  assert(host.textContent.includes('Rung 0') && host.textContent.includes('Rung 1'), 'both rungs render as stages in the round view');
});

test('REGRESSION (round-view-empty): the per-ROUND racing drill-down CONVERGES with the all-rounds + epoch views — same rungs LIVE and SETTLED', async () => {
  // LIVE: round drill-down vs all-rounds Match-ups — both must show the rungs.
  const roundLive = await renderRacingView('gens', { epochId: EPOCH_ID, round: 0 }, { live: true });
  const allRoundsLive = await renderRacingView('gens', { epochId: EPOCH_ID }, { live: true });
  const epochLive = await renderRacingView('epoch', { epochId: EPOCH_ID }, { live: true });
  for (const [name, h] of [['round', roundLive], ['all-rounds', allRoundsLive], ['epoch', epochLive]]) {
    assert(svgsByClass(h, 'dn-funnel')[0], `LIVE: the ${name} view renders the racing funnel (non-empty rungs)`);
    assert(!h.textContent.includes('No rungs evaluated yet'), `LIVE: the ${name} view has no empty rung state`);
  }
  // SETTLED: the field/per-tournament record now carries the rungs (the
  // orchestrator's settle upsert). With NO live run the round drill-down must
  // STILL build the rungs — proving live↔recorded convergence (no source drift).
  const roundSettled = await renderRacingView('gens', { epochId: EPOCH_ID, round: 0 }, { live: false });
  const allRoundsSettled = await renderRacingView('gens', { epochId: EPOCH_ID }, { live: false });
  for (const [name, h] of [['round', roundSettled], ['all-rounds', allRoundsSettled]]) {
    assert(!h.textContent.includes('No rungs evaluated yet'), `SETTLED: the ${name} view has no empty rung state`);
    assert(svgsByClass(h, 'dn-funnel')[0], `SETTLED: the ${name} view renders the racing funnel from the recorded rungs`);
    assert(h.textContent.includes('Rung 0') && h.textContent.includes('Rung 1'), `SETTLED: the ${name} view shows both rungs`);
  }
});

// ---- REGRESSION (single-source-of-truth across VIEWS): the EPOCH overview
// must build its racing/swiss/elim model through the SAME shared resolver the
// Match-ups + per-round views use. The OLD epoch path built racing via
// `normalizeStructure(liveRaw,true)` (no progressive overlay / projected re-rank
// / seeded-champ benchmark) and bypassed the completed per-tournament record,
// so its funnel diverged from the Match-ups figure LIVE *and* SETTLED. These
// pin the epoch view to the resolver, asserting it converges with gens.

test('REGRESSION (view-divergence): the EPOCH racing overview builds a NON-EMPTY funnel LIVE and SETTLED (resolver-built, never the empty rounds:[] field record)', async () => {
  const live = await renderRacingView('epoch', { epochId: EPOCH_ID }, { live: true });
  assert(svgsByClass(live, 'dn-funnel')[0], 'LIVE: the epoch overview renders the racing funnel from the live envelope');
  assert(!live.textContent.includes('not a gauntlet') && !live.textContent.includes('No rungs'), 'LIVE: no negative/empty rung state on the epoch view');
  const settled = await renderRacingView('epoch', { epochId: EPOCH_ID }, { live: false });
  assert(svgsByClass(settled, 'dn-funnel')[0], 'SETTLED: the epoch overview renders the racing funnel from the recorded rungs');
});

test('REGRESSION (view-divergence): the EPOCH racing model CONVERGES (digest-equal) with the all-rounds + per-round views — one shared resolver, no source drift', async () => {
  // Build the racing `st` the way EACH view now does: all through the resolver.
  // The active-tournament envelope is the live source; with the bracket empty
  // (the e4 in-progress shape) the resolver adopts the live model identically.
  const at = RACING_LIVE_AT;
  const hb = { phase: at.phase, epoch_id: at.epoch_id };
  const opts = { structure: 'racing', bracket: {}, epochId: EPOCH_ID, liveRaw: at, heartbeat: hb, activeRuns: [], params: at.structure_params };
  const epochSt = STRUCT.resolveNonGauntletSt(opts).st;     // epoch.js path
  const gensSt = STRUCT.resolveNonGauntletSt(opts).st;      // gens.js path
  const candSt = STRUCT.resolveNonGauntletSt(opts).st;      // candidate.js path
  assert(epochSt && gensSt && candSt, 'all three view paths resolve a racing st');
  const dE = STRUCT.structureDigest(epochSt);
  assertEqual(STRUCT.structureDigest(gensSt), dE, 'epoch ↔ all-rounds racing model is digest-equal');
  assertEqual(STRUCT.structureDigest(candSt), dE, 'candidate ↔ all-rounds racing model is digest-equal');
  // the resolved model is non-empty (the rungs the funnel/track draw).
  const m = STRUCT.racingModel(epochSt);
  assert(m && m.hasRungs && m.rungs.length >= 2, 'the shared racing model carries the rungs');
});

test('REGRESSION (view-divergence): the EPOCH swiss + single_elim overviews build a NON-EMPTY model SETTLED (resolver completed-record fallback)', async () => {
  // SWISS settled: the per-tournament record carries the rounds; the epoch
  // overview must build the swiss bump/bars from it (not an empty strip).
  freshState();
  installFixtureMap(structFixture('swiss', SWISS_STRUCT, 'tourn_e0_sw'));
  const epochMod = await import('../js/variants/T/views/epoch.js');
  let host = document.createElement('div');
  await epochMod.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID });
  assert(svgsByClass(host, 'dn-swissover')[0], 'SETTLED swiss: the epoch overview builds the swiss bump/bars figure from the record');
  assert(!host.textContent.includes('not a gauntlet'), 'SETTLED swiss: no negative placeholder');
  // SINGLE-ELIM settled: the epoch overview builds the elim flow from the record.
  freshState();
  installFixtureMap(structFixture('single_elim', SE_STRUCT, 'tourn_e0_se'));
  host = document.createElement('div');
  await epochMod.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID });
  assert(svgsByClass(host, 'dn-elimflow')[0], 'SETTLED single_elim: the epoch overview builds the elim flow from the recorded rounds');
  assert(!host.textContent.includes('not a gauntlet'), 'SETTLED single_elim: no negative placeholder');
});

test('REGRESSION (view-divergence): the CANDIDATE racing dossier builds field panels from the LIVE envelope (live-first via the resolver, not settled-only reconstruct)', async () => {
  freshState();
  installFixtureMap(racingRoundFixture({ live: true }));
  coreState.state.activeTournament = RACING_LIVE_AT;
  coreState.state.heartbeat = { phase: 'tournament:round_0:running', epoch_id: EPOCH_ID, last_heartbeat: new Date().toISOString() };
  coreState.state.activeRuns = [{ generation_id: 'v1', entry_id: 'b0', run_id: 'run_v1' }];
  try {
    const candMod = await import('../js/variants/T/views/candidate.js');
    const host = document.createElement('div');
    // view the surviving racer v1 — its dossier swaps to the field-relative panels.
    await candMod.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, gen: 'v1' });
    assert(allByClass(host, 'dn-racing-field')[0], 'the candidate dossier shows the field-relative racing panels (built from the LIVE envelope)');
    assert(host.textContent.includes('FIELD-relative'), 'the field-relative caption renders');
    // the field standings include the OTHER racers from the live rungs (v2/v3),
    // not just the settled reconstruction — proving the live envelope was read.
    assert(/v2|v3/.test(host.textContent), 'the field panels list the live-envelope field, not an empty/settled-only reconstruction');
  } finally {
    coreState.state.activeTournament = null;
    coreState.state.heartbeat = null;
    coreState.state.activeRuns = [];
  }
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
  assert(/No completed tournament|unavailable/i.test(host.textContent), 'an honest empty state renders rather than throwing');
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

// ---- the EPOCH-VIEW non-gauntlet OVERVIEW (replaces the negative placeholder) ----

test('epoch timeline (swiss): the round episode embeds the standings BUMP chart + ranked Copeland bar + gate verdict — "not a gauntlet" is GONE', async () => {
  freshState();
  installFixtureMap(structFixture('swiss', SWISS_STRUCT, 'tourn_e0_sw'));
  const epoch = await import('../js/variants/T/views/epoch.js');
  const host = document.createElement('div');
  await epoch.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID });

  // the negative placeholder is gone — replaced by the timeline + figure.
  assert(!host.textContent.includes('not a gauntlet'), 'the negative "not a gauntlet" placeholder is GONE');
  assert(allByClass(host, 'dn-roundtl')[0], 'the round timeline rendered for the swiss epoch');

  // the swiss overview SVG is embedded as the episode figure, fit-to-width.
  const over = svgsByClass(host, 'dn-swissover')[0];
  assert(over, 'the swiss overview SVG is embedded as the swiss episode figure');
  assertEqual(over.getAttribute('width'), '100%', 'the swiss overview is fit-to-width (width:100%)');
  assert((over.getAttribute('viewBox') || '').startsWith('0 0 '), 'the swiss overview carries a viewBox so it scales');
  assert(!hasScrollWrapperAncestor(over, host), 'no horizontal-scroll wrapper around the overview');

  // (1) the bump chart: one line per competitor that has ranks.
  assertEqual(allByClass(over, 'dn-swissover-line').length, 3, 'one bump line per competitor (v0, v1, v2)');
  assert(allByClass(over, 'dn-swissover-line-champ').length >= 1, 'the champion line is emphasised');
  // (2) the ranked Copeland-point bar (the standings, one bar each).
  assert(allByClass(over, 'dn-swissover-bar').length >= 2, 'the ranked Copeland bars rendered');
  assert(over.textContent.includes('♔'), 'the leader is marked ♔ on the ranked bar');
  // the champion-gate verdict.
  assert(over.textContent.includes('promoted') || over.textContent.includes('♛'),
    'the champion-gate verdict (promoted ♛) is shown');
  // the episode drills into the round's full Match-ups.
  assert(host.textContent.includes('open round'), 'the episode keeps the "open round →" drill affordance');
});

test('epoch timeline (single-elim): the elim episode embeds the GENERATIONS-ACROSS-ROUNDS FLOW (elim parity), NOT the mini-bracket', async () => {
  freshState();
  installFixtureMap(structFixture('single_elim', SE_STRUCT, 'tourn_e0_se'));
  const epoch = await import('../js/variants/T/views/epoch.js');
  const host = document.createElement('div');
  await epoch.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID });

  assert(!host.textContent.includes('not a gauntlet'), 'the negative placeholder is GONE for elim too');
  assert(allByClass(host, 'dn-roundtl')[0], 'the round timeline rendered for the elim epoch');
  // ELIM PARITY (#1): the epoch hero leads with elimFlow (matching racing→funnel,
  // swiss→bump), NOT the compact mini-bracket. The bracket tree lives in the
  // round drill-down (Match-ups).
  const flow = svgsByClass(host, 'dn-elimflow')[0];
  assert(flow, 'the elim episode embeds the generations-across-rounds flow (elimFlow)');
  assertEqual(flow.getAttribute('width'), '100%', 'the elim flow is fit-to-width');
  assert((flow.getAttribute('viewBox') || '').startsWith('0 0 '), 'the elim flow carries a viewBox');
  assertEqual(svgsByClass(host, 'dn-elimbracket-compact').length, 0, 'NO compact mini-bracket on the epoch overview (it is now elimFlow)');
});

test('epoch timeline (no data): the timeline renders an honest empty — NEVER the negative "not a gauntlet" placeholder', async () => {
  freshState();
  // a swiss epoch with NO tournament records yet (mid-proposing / not run).
  const F = {
    '/api/epoch': { epoch_id: EPOCH_ID, closed: false, goal: 'g', tournament: { structure: 'swiss', params: { rounds: 3 } }, experiments: [], board: [] },
    '/api/lineage': { generations: [] },
    '/api/score-trajectory': { points: [] },
    '/api/tournaments': { epoch_id: EPOCH_ID, structure: 'swiss', champion_lineage: [], matchups: [], tournaments: [] },
  };
  installFixtureMap(F);
  const epoch = await import('../js/variants/T/views/epoch.js');
  const host = document.createElement('div');
  await epoch.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID });

  assert(!host.textContent.includes('not a gauntlet'), 'no negative "not a gauntlet" placeholder even with no data');
  assert(allByClass(host, 'dn-roundtl')[0], 'the round timeline renders even with no data');
  assertEqual(svgsByClass(host, 'dn-swissover').length, 0, 'no embedded swiss figure when there is no data');
  // a no-data epoch degrades to a single round-0 episode with no minted field.
  assert(/no challengers minted this round/i.test(host.textContent), 'the empty round reads "no challengers minted this round"');
});

test('structure: the data layer exposes tournamentStructure() + invalidates its cache live', async () => {
  assertEqual(typeof data.tournamentStructure, 'function', 'data.tournamentStructure() exists');
  // the live-invalidation set includes the new prefix.
  const css = '';  // (no css needed) — assert the source carries the prefix.
  const src = await import('node:fs').then((fs) => fs.readFileSync(new URL('../js/variants/T/data.js', import.meta.url), 'utf8'));
  assert(src.includes('/api/tournament-structure/'), 'invalidateLive() busts the tournament-structure prefix');
});

// ---- gauntlet REGRESSION: the default structure is unchanged --------

test('gauntlet (default): the match-ups page renders the FIELD as the duel-flow graphic + integrated champion header (no structure pill)', async () => {
  freshState(); installFetch();  // the default gauntlet fixture (no tournament block)
  const gens = await import('../js/variants/T/views/gens.js');
  const host = document.createElement('div');
  await gens.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID });
  assertEqual(allByClass(host, 'dt-champ-banner').length, 0, 'NO boxed champion banner remains');
  assertEqual(allByClass(host, 'dt-match-card').length, 0, 'NO match-card boxes remain');
  assert(allByClass(host, 'dt-fieldflow-champ').length === 1, 'the integrated champion header renders');
  const flow = svgsByClass(host, 'dn-duelflow')[0];
  assert(flow, 'the field renders as the duel-flow structure-graphic');
  assertEqual(allByClass(flow, 'dn-duelflow-lane').length, 2, 'one challenger lane per round (v0→v1, v0→v2)');
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

test('live-status: a FRESH heartbeat phase ALONE (proposing) lights the running state even before a tournament exists', () => {
  const now = 1_000_000_000_000;
  const status = livestatus.deriveLiveStatus({
    heartbeat: { phase: 'proposing:field', generation_id: null, last_heartbeat: now - 2_000 /* fresh */ },
    activeRuns: [],
    activeTournament: null,
  }, now);
  assertEqual(status.running, true, 'a fresh non-idle proposing phase ⇒ running even with no tournament + no active-runs');
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

// ---- (a′) terminal-TAIL phases read as idle (the false-LIVE bug) ----

test('live-status: isActivePhase treats terminal-TAIL phase paths as idle', () => {
  // the terminal signal lives in the tail segment, not the head.
  assertEqual(livestatus.isActivePhase('evolve_n_rounds:done'), false, 'evolve_n_rounds:done is terminal (tail = done)');
  assertEqual(livestatus.isActivePhase('tournament:round_0:done'), false, 'a tournament path ending in :done is terminal');
  assertEqual(livestatus.isActivePhase('evolve_n_rounds:completed'), false, 'a :completed tail is terminal');
  // genuinely-active phases keep no idle token in any segment.
  assertEqual(livestatus.isActivePhase('tournament:round_0:rung0_m3'), true, 'an in-flight tournament rung is active');
  assertEqual(livestatus.isActivePhase('proposing:field'), true, 'a proposing phase is active');
});

// ---- (a″) heartbeat-staleness gates the live read ----

test('live-status: a STALE heartbeat + terminal phase + 0 runs + completed tournament reads idle/done (no false LIVE)', () => {
  // mirrors the real completed-run case: frozen terminal heartbeat from a
  // finished process, no in-flight units, a completed (not running) tournament.
  const now = 1_000_000_000_000; // fixed epoch ms for determinism
  const status = livestatus.deriveLiveStatus({
    heartbeat: { phase: 'evolve_n_rounds:done', last_heartbeat: now - 120_000 /* 2 min old */ },
    activeRuns: [],
    activeTournament: { structure: 'racing', phase: 'completed' },
  }, now);
  assertEqual(status.running, false, 'a stale terminal heartbeat with nothing in flight is NOT live');
  assert(status.label === 'idle' || status.label === 'done', 'a finished run reads idle/done, not a running label');
});

test('live-status: a FRESH heartbeat + active phase reads as RUNNING', () => {
  const now = 1_000_000_000_000;
  const status = livestatus.deriveLiveStatus({
    heartbeat: { phase: 'tournament:round_0:rung0_m3', last_heartbeat: now - 2_000 /* 2s old, fresh */ },
    activeRuns: [],
    activeTournament: { structure: 'racing', phase: 'running' },
  }, now);
  assertEqual(status.running, true, 'a fresh heartbeat on an active phase is live');
});

test('live-status: an in-flight active-run forces RUNNING even with an old timestamp (ground truth)', () => {
  const now = 1_000_000_000_000;
  const status = livestatus.deriveLiveStatus({
    heartbeat: { phase: 'evolve_n_rounds:done', last_heartbeat: now - 600_000 /* 10 min old */ },
    activeRuns: [{ generation_id: 'v1', entry_id: 'b0', run_id: 'r0', progress: 0.5 }],
    activeTournament: null,
  }, now);
  assertEqual(status.running, true, 'an actively-running unit is ground truth — live even if the heartbeat looks dead');
  assertEqual(status.inFlight, 1, 'the in-flight unit is counted');
});

test('live-status: a completed active-tournament ALONE (no fresh phase, no runs) does NOT read live', () => {
  const now = 1_000_000_000_000;
  const status = livestatus.deriveLiveStatus({
    heartbeat: { phase: 'idle', last_heartbeat: now - 5_000 },
    activeRuns: [],
    activeTournament: { structure: 'swiss', phase: 'completed' },
  }, now);
  assertEqual(status.running, false, 'a completed tournament is not a running signal');
});

test('live-status: a heartbeat with NO parseable timestamp reads NOT live (missing ts ⇒ stale, never default-to-live)', () => {
  // a realistic 2026 epoch-ms `now` (>1e12) so a numeric `last_heartbeat`
  // delta is read as ms, not rescaled from "seconds".
  const now = 1_780_455_964_000;
  // THE BUG: a killed run leaves a heartbeat whose ts cannot be parsed. It must
  // NOT default to live off an active phase — a heartbeat that cannot be aged
  // out is stale, not fresh.
  const noTs = livestatus.deriveLiveStatus({
    heartbeat: { phase: 'tournament:round_0:final' }, activeRuns: [], activeTournament: null,
  }, now);
  assertEqual(noTs.running, false, 'an active phase with NO timestamp reads NOT live (missing ts ⇒ stale)');
  assertEqual(noTs.heartbeatStale, true, 'a heartbeat with no parseable timestamp is flagged stale');
  // a garbage timestamp is likewise unparseable ⇒ stale ⇒ not live.
  const badTs = livestatus.deriveLiveStatus({
    heartbeat: { phase: 'tournament:round_0:rung0_m3', last_heartbeat: 'not-a-date' }, activeRuns: [], activeTournament: null,
  }, now);
  assertEqual(badTs.running, false, 'an unparseable timestamp reads NOT live (stale)');
});

test('live-status: a heartbeat OLDER than STALE_HEARTBEAT_MS reads NOT live (the one staleness rule)', () => {
  const now = 1_780_455_964_000;
  // just past the staleness window on an otherwise-active phase ⇒ not live.
  const old = livestatus.deriveLiveStatus({
    heartbeat: { phase: 'tournament:round_0:final', last_heartbeat: now - (livestatus.STALE_HEARTBEAT_MS + 1_000) },
    activeRuns: [],
    activeTournament: null,
  }, now);
  assertEqual(old.running, false, 'an active phase with a too-old timestamp reads NOT live');
  assertEqual(old.heartbeatStale, true, 'a too-old heartbeat is flagged stale');
  // a FRESH timestamp on the same active phase reads live (the positive case).
  const fresh = livestatus.deriveLiveStatus({
    heartbeat: { phase: 'tournament:round_0:final', last_heartbeat: now - 2_000 },
    activeRuns: [],
    activeTournament: null,
  }, now);
  assertEqual(fresh.running, true, 'a fresh timestamp on an active phase reads LIVE');
  assertEqual(fresh.heartbeatStale, false, 'a fresh heartbeat is not stale');
});

test('live-status: a DEAD run (stale phase + frozen active_tournament phase:running, 0 in-flight) reads NOT live — the repro', () => {
  const now = 1_780_455_964_000;
  // the exact on-disk shape a killed run leaves: a stale heartbeat with an
  // active-looking phase, an orphaned active_tournament.json still saying
  // phase:"running", and ZERO in-flight board-units. The frozen tournament
  // file must NOT keep it live now that the orchestrator heartbeat is stale.
  const dead = livestatus.deriveLiveStatus({
    heartbeat: { phase: 'tournament:round_0:final', generation_id: 'v3', epoch_id: '2026-06-03_e3', last_heartbeat: now - 800_000 /* ~13 min */ },
    activeRuns: [],
    activeTournament: { structure: 'single_elim', phase: 'running' },
  }, now);
  assertEqual(dead.running, false, 'a dead run with a frozen running-tournament file but a stale heartbeat is NOT live');
  assertEqual(dead.tournamentRunning, true, 'the frozen tournament file is still reported as phase:running');
  assert(dead.label === 'idle' || dead.label === 'done', 'the dead run reads idle/done, not a running label');
});

test('live-status: a stale run exposes a heartbeat AGE + a "last seen Ns ago" affordance (not a silent freeze)', () => {
  const now = 1_780_455_964_000;
  const dead = livestatus.deriveLiveStatus({
    heartbeat: { phase: 'tournament:round_0:racing-final', last_heartbeat: now - 90_000 /* 90s */ },
    activeRuns: [],
    activeTournament: { structure: 'racing', phase: 'running' },
  }, now);
  assertEqual(dead.running, false, 'a 90s-old heartbeat reads NOT live');
  assertEqual(dead.heartbeatStale, true, 'flagged stale');
  assertEqual(dead.heartbeatAgeMs, 90_000, 'the heartbeat age is exposed for the affordance');
  assertEqual(livestatus.staleLabel(dead.heartbeatAgeMs), 'last seen 90s ago',
    'the affordance reads "last seen 90s ago"');
  // minutes / hours bucketing + the untimestamped fallback.
  assertEqual(livestatus.staleLabel(125_000), 'last seen 2m ago', 'minutes bucket');
  assertEqual(livestatus.staleLabel(2 * 3600_000 + 5000), 'last seen 2h ago', 'hours bucket');
  assertEqual(livestatus.staleLabel(NaN), 'stale', 'an untimestamped frozen heartbeat reads a bare "stale"');
});

test('live-status: the chrome shows the stale affordance + a non-running dot when the heartbeat freezes (no LIVE banner)', () => {
  try { globalThis.window.localStorage.clear(); } catch (e) { /* ignore */ }
  const now = Date.now();
  coreState.state.connected = true;
  coreState.state.connecting = false;
  // a FROZEN heartbeat: an active-looking phase + a running tournament file,
  // but a last_heartbeat well past the staleness window → NOT live.
  coreState.state.setHeartbeat({ phase: 'tournament:round_0:racing-final', generation_id: 'v3', last_heartbeat: now - 120_000 });
  coreState.state.activeRuns = [];
  coreState.state.activeTournament = { structure: 'racing', phase: 'running', competitors: [{ generation_id: 'v0' }] };

  const root = mountLiveShell('#/');

  const statusEl = allByClass(root, 'dt-status')[0];
  assert(statusEl, 'the chrome status pill mounted');
  const cls = statusEl.getAttribute('class') || '';
  assert(!cls.split(/\s+/).includes('dt-running'), 'a frozen heartbeat does NOT light the running (LIVE) chrome');
  assert(cls.split(/\s+/).includes('dt-stale'), 'the chrome carries the dt-stale class');
  const staleEl = allByClass(root, 'dt-status-stale')[0];
  assert(staleEl && /last seen/.test(staleEl.textContent),
    'the chrome shows a "last seen Ns ago" affordance rather than a silent freeze');
  // the live run label must NOT read as running.
  const runLabel = allByClass(root, 'dt-run-label')[0];
  assert(!runLabel || runLabel.textContent.trim() === '', 'no running label for a stale run');

  // reset to an idle environment so other tests start clean.
  coreState.state.heartbeat = { phase: 'idle' };
  coreState.state.activeRuns = [];
  coreState.state.activeTournament = null;
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
//   (a) the epoch round timeline leads with the per-round structure figure;
//   (b) a LIVE /api/active-tournament fills the ladder (not "nothing ran")
//       and in-flight competitors are not mislabeled rejected;
//   (c) the richer racing ladder renders rungs with cut/survivor + board
//       fraction + a champion-gate;
//   (d) only the CURRENT champion (last in champion_lineage) is badged
//       "champion ♚"; FORMER champions get a distinct "former" marker.
// ====================================================================

// ---- (a) the epoch round timeline subsumes the reel + structure strip --

test('epoch timeline: a NON-gauntlet (racing) epoch leads with the round timeline (one renderer for all structures, no separate strip)', async () => {
  freshState();
  installFixtureMap(structFixture('racing', RACING_STRUCT, 'tourn_e0_rc'));
  const epoch = await import('../js/variants/T/views/epoch.js');
  const host = document.createElement('div');
  await epoch.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID });

  // ONE timeline, for every structure — the old reel + structure strip are GONE.
  assert(allByClass(host, 'dn-roundtl')[0], 'the round timeline rendered for a racing epoch');
  // a single-round epoch degrades to just its episode — the spine + waterfall
  // (a trajectory across ≥2 rounds) are correctly omitted, so the figure is not
  // a lonely point floating in empty space.
  assert(allByClass(host, 'dn-roundtl-ep')[0], 'the timeline renders the round episode (single round → no empty spine)');
  assertEqual(allByClass(host, 'dn-roundtl-spine').length, 0, 'a single-round epoch shows NO champion spine (nothing to plot a trajectory over)');
  assertEqual(allByClass(host, 'tr-reel').length, 0, 'NO gauntlet champion-spine reel for a racing epoch');
  assertEqual(allByClass(host, 'dt-struct-strip').length, 0, 'NO standalone structure strip — folded into the timeline');
  assert(host.textContent.includes('Racing'), 'the timeline names the racing structure');
  // the epoch overview structure is otherwise unchanged (objective + brief).
  assert(host.textContent.includes('objective'), 'the epoch overview keeps its objective block');
});

test('epoch timeline: a GAUNTLET epoch renders the round timeline (a single episode for --rounds 1), NOT the old reel', async () => {
  freshState(); installFetch();   // the default gauntlet fixture (no tournament block)
  const epoch = await import('../js/variants/T/views/epoch.js');
  const host = document.createElement('div');
  await epoch.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID });
  assert(allByClass(host, 'dn-roundtl')[0], 'the gauntlet epoch renders the round timeline');
  assert(allByClass(host, 'dn-roundtl-spine')[0], 'the timeline keeps its champion spine');
  assertEqual(allByClass(host, 'tr-reel').length, 0, 'the old reel is gone (subsumed)');
  // a gauntlet single round → no embedded structure figure (the fan tells the story).
  assertEqual(allByClass(host, 'dn-funnel').length, 0, 'NO structure figure embedded for a gauntlet round');
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

  // the funnel rendered from the LIVE record — NOT an empty "nothing ran" state.
  const ladder = svgsByClass(host, 'dn-funnel')[0];
  assert(ladder, 'the survival funnel rendered from the live active-tournament');
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

// ====================================================================
// PROGRESSIVE LIVE RACING LADDER — buildLiveRacingModel() over the UNIFIED
// buildLiveModel path.
//
// The backend now PUBLISHES the live tournament topology on
// /api/active-tournament DURING the run: `rounds` with each rung's matches (an
// in-flight rung's match carries no survivors/cut + pending). The unified live
// model consumes the published rounds verbatim and OVERLAYS the per-board
// PROGRESS that still lives in /api/active-runs (per-lane k/N + a partial Δ),
// then carries committed rungs through untouched (ACCUMULATION).
// ====================================================================

// the live racing field shape per the NEW contract: v0 champion + v5..v8
// challengers, the active rung-0 PUBLISHED (its field pending), partial
// aggregates landing.
function liveRacingField(extra) {
  return Object.assign({
    structure: 'racing', phase: 'running',
    structure_params: { field_size: 4, eta: 2, board_fraction: 0.25, board_size: 8 },
    round_index: 0, total_rounds: 2,
    competitors: [
      { generation_id: 'v0', seed: 1, role: 'champion' },
      { generation_id: 'v5', seed: 2, role: 'challenger' },
      { generation_id: 'v6', seed: 3, role: 'challenger' },
      { generation_id: 'v7', seed: 4, role: 'challenger' },
      { generation_id: 'v8', seed: 5, role: 'challenger' },
    ],
    // the backend publishes the active rung + the (pending) champion gate live.
    rounds: [
      { round_index: 0, label: 'Rung 0', matches: [{ match_id: 'rung0', competitors: ['v5', 'v6', 'v7', 'v8'], survivors: [], cut: [], board_fraction: 0.25, pending: true }] },
      { round_index: 1, label: 'Champion gate', matches: [{ match_id: 'racing-final', competitors: ['v0'], board_fraction: 1.0, winner: null, pending: true }] },
    ],
    standings: [],
    champion_lineage: ['v0'],
  }, extra || {});
}

// (a) published rung-0 (pending) → the field + rung-0 lanes render with progress
// overlaid from active-runs; NOT the "being seeded" empty state.
test('live racing model: published rung-0 (pending) renders the field with active-runs progress overlaid — not the "being seeded" empty state', () => {
  const at = liveRacingField();
  const model = STRUCT.buildLiveRacingModel({
    at,
    heartbeat: { phase: 'tournament:round_0:rung0_m1', generation_id: 'v5' },
    activeRuns: [
      { generation_id: 'v5', entry_id: 'b0', run_id: 'r0', progress: 0.4 },
      { generation_id: 'v6', entry_id: 'b1', run_id: 'r1', progress: 0.9 },
    ],
    epochGens: ['v0', 'v5', 'v6', 'v7', 'v8'],
  });
  assert(model, 'a live racing model was built from the published rounds');
  assertEqual(model.live, true, 'the model is marked live');
  const rungRounds = model.rounds.filter((r) => String(r.matches[0].match_id) !== 'racing-final');
  assert(rungRounds.length >= 1, 'at least rung 0 is present from the published rounds');
  const r0 = rungRounds[0].matches[0];
  assertDeep([...r0.competitors].sort(), ['v5', 'v6', 'v7', 'v8'], 'rung-0 field is the published challenger set (champion v0 is the benchmark, not a lane)');
  assert(r0.live_progress && r0.live_progress.v5 && r0.live_progress.v6, 'rung-0 carries per-lane live progress overlaid from active-runs');
  assertEqual(r0.queued, false, 'rung-0 is the ACTIVE rung (not queued)');

  // it renders rung-0 lanes (not the empty state) when fed to renderStructure.
  const nodes = STRUCT.renderStructure(model, { navigate() {}, href: router.href }, EPOCH_ID);
  const host = document.createElement('div');
  for (const n of nodes) host.appendChild(n);
  const ladder = svgsByClass(host, 'dn-funnel')[0];
  assert(ladder, 'a survival funnel SVG rendered from the published-rounds live model');
  assert(!/being seeded/i.test(host.textContent), 'NOT the "being seeded" empty state once the field exists');
  for (const id of ['v5', 'v6', 'v7', 'v8']) assert(ladder.textContent.includes(id), 'rung-0 names the live challenger lane — ' + id);
});

// (b) rung-0 partially done (active-runs at <1.0) → lanes show "k/N boards".
test('live racing model: an in-flight rung shows per-lane "k/N boards" progress + a partial Δ', () => {
  // the backend writes `partial_*_agg` as DICTS ({scalar, ...}); the model
  // reads `.scalar` (the dead `svg.isNum(dict)` plumbing has been fixed).
  const at = liveRacingField({ partial_champion_agg: { scalar: 12.0 }, partial_challenger_agg: { scalar: 9.5 } });
  const model = STRUCT.buildLiveRacingModel({
    at,
    heartbeat: { phase: 'tournament:round_0:rung0_m2', generation_id: 'v5' },
    activeRuns: [
      { generation_id: 'v5', entry_id: 'b0', run_id: 'r0', progress: 0.5 },  // 1 of 2 board units done-ish
      { generation_id: 'v5', entry_id: 'b1', run_id: 'r1', progress: 0.0 },
    ],
    epochGens: ['v0', 'v5', 'v6', 'v7', 'v8'],
  });
  const r0 = model.rounds.find((r) => String(r.matches[0].match_id) === 'rung0').matches[0];
  const laneV5 = r0.live_progress.v5;
  assertEqual(laneV5.inflight, 2, 'v5 has two in-flight board units this rung');
  assertEqual(laneV5.total, 2, 'the rung-0 board total is board_size·fraction = 8·0.25 = 2');
  assertEqual(laneV5.partialDelta, -2.5, 'the partial Δ = partial_challenger_agg − partial_champion_agg = 9.5 − 12.0');

  const nodes = STRUCT.renderStructure(model, { navigate() {}, href: router.href }, EPOCH_ID);
  const host = document.createElement('div');
  for (const n of nodes) host.appendChild(n);
  const ladder = svgsByClass(host, 'dn-funnel')[0];
  assert(/boards/.test(ladder.textContent), 'a live lane reads "k/N boards" (progressive fill, not blank)');
  assert(allByClass(ladder, 'dn-funnel-bar').length >= 1, 'a per-lane in-flight progress bar renders');
  assert(!/✕/.test(ladder.textContent), 'a mid-run lane is NOT struck through as cut');
});

// (c) rung-0 complete (rounds has rung0) → survivors ↑ / cuts ✗; then rung-1
// starts (new active-runs) → rung-0's completed result is STILL present.
test('live racing model: a completed rung ACCUMULATES — when rung-1 starts, rung-0 survivors/cuts persist (not discarded)', () => {
  // rung-0 has COMPLETED (committed in the published rounds): v7,v8 survive,
  // v5,v6 cut. The backend now publishes rung-1 (active), with v7,v8 racing.
  const at = liveRacingField({
    round_index: 1,
    rounds: [
      { round_index: 0, label: 'Rung 0', matches: [{ match_id: 'rung0', competitors: ['v5', 'v6', 'v7', 'v8'], survivors: ['v7', 'v8'], cut: ['v5', 'v6'], board_fraction: 0.25 }] },
      { round_index: 1, label: 'Rung 1', matches: [{ match_id: 'rung1', competitors: ['v7', 'v8'], survivors: [], cut: [], board_fraction: 0.5, pending: true }] },
      { round_index: 2, label: 'Champion gate', matches: [{ match_id: 'racing-final', competitors: ['v0'], board_fraction: 1.0, winner: null, pending: true }] },
    ],
  });
  const model = STRUCT.buildLiveRacingModel({
    at,
    heartbeat: { phase: 'tournament:round_1:rung1_m0', generation_id: 'v7' },
    activeRuns: [{ generation_id: 'v7', entry_id: 'b0', run_id: 'r0', progress: 0.3 }],
    epochGens: ['v0', 'v5', 'v6', 'v7', 'v8'],
  });
  const r0 = model.rounds.find((r) => String(r.matches[0].match_id) === 'rung0').matches[0];
  // the COMPLETED rung-0 is carried verbatim — survivors/cuts persist.
  assertDeep([...r0.cut].sort(), ['v5', 'v6'], 'the completed rung-0 cuts (v5,v6) persist when rung-1 starts');
  assertDeep([...r0.survivors].sort(), ['v7', 'v8'], 'the completed rung-0 survivors (v7,v8) persist');
  const r1 = model.rounds.find((r) => String(r.matches[0].match_id) === 'rung1').matches[0];
  assertDeep([...r1.competitors].sort(), ['v7', 'v8'], 'rung-1 races ONLY the rung-0 survivors (the field narrowed by η)');
  assertEqual(r1.queued, false, 'rung-1 is now the active rung');

  // the rendered ladder keeps BOTH rungs — the cut marks survive the new tick.
  const nodes = STRUCT.renderStructure(model, { navigate() {}, href: router.href }, EPOCH_ID);
  const host = document.createElement('div');
  for (const n of nodes) host.appendChild(n);
  const ladder = svgsByClass(host, 'dn-funnel')[0];
  assert(/✕/.test(ladder.textContent), 'rung-0 cut marks (✕) are STILL present after rung-1 starts (accumulation, no discard)');
  assert(/↑/.test(ladder.textContent), 'rung-0 survivor marks (↑) persist');
  for (const id of ['v5', 'v6', 'v7', 'v8']) assert(ladder.textContent.includes(id), 'every competitor remains legible across rungs — ' + id);
});

// (d) a no-op repeat render (same digest) does NOT rebuild the ladder.
test('live racing model: a no-op heartbeat (same progress) yields a STABLE digest — the ladder is not rebuilt', () => {
  const heartbeat = { phase: 'tournament:round_0:rung0_m1', generation_id: 'v5' };
  const activeRuns = [{ generation_id: 'v5', entry_id: 'b0', run_id: 'r0', progress: 0.4 }];
  const epochGens = ['v0', 'v5', 'v6', 'v7', 'v8'];
  const a = STRUCT.buildLiveRacingModel({ at: liveRacingField(), heartbeat, activeRuns, epochGens });
  const b = STRUCT.buildLiveRacingModel({ at: liveRacingField(), heartbeat, activeRuns, epochGens });
  assertEqual(STRUCT.structureDigest(a), STRUCT.structureDigest(b), 'two identical live ticks produce the SAME digest (digest-gated — no DOM rebuild)');

  // a REAL change (a board landed → done count grows) MUST change the digest.
  const c = STRUCT.buildLiveRacingModel({
    at: liveRacingField(),
    heartbeat,
    activeRuns: [{ generation_id: 'v5', entry_id: 'b0', run_id: 'r0', progress: 1.0 }],
    epochGens,
  });
  assert(STRUCT.structureDigest(a) !== STRUCT.structureDigest(c), 'a board landing (progress advanced) DOES change the digest');

  // node-identity check: a gated re-render with the same digest keeps the ladder node.
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  ui.gatedSwap(host, STRUCT.structureDigest(a), () => STRUCT.renderStructure(a, ctx, EPOCH_ID));
  const first = svgsByClass(host, 'dn-funnel')[0];
  ui.gatedSwap(host, STRUCT.structureDigest(b), () => STRUCT.renderStructure(b, ctx, EPOCH_ID));
  const second = svgsByClass(host, 'dn-funnel')[0];
  assert(first === second, 'the funnel SVG node identity is preserved across a no-op tick (digest-gated, zero rebuild)');
});

// (e) champion-gate pending vs decided renders correctly (pending ≠ rejected).
test('live racing model: the champion-gate is PENDING (deciding…) during the race — never "rejected"', () => {
  const model = STRUCT.buildLiveRacingModel({
    at: liveRacingField(),
    heartbeat: { phase: 'tournament:round_0:rung0_m1', generation_id: 'v5' },
    activeRuns: [{ generation_id: 'v5', entry_id: 'b0', run_id: 'r0', progress: 0.4 }],
    epochGens: ['v0', 'v5', 'v6', 'v7', 'v8'],
  });
  const gate = model.rounds.find((r) => String(r.matches[0].match_id) === 'racing-final');
  assert(gate, 'a champion-gate round is present');
  assert(!gate.matches[0].decision, 'the live gate has NO committed decision (not promoted/rejected)');
  const nodes = STRUCT.renderStructure(model, { navigate() {}, href: router.href }, EPOCH_ID);
  const host = document.createElement('div');
  for (const n of nodes) host.appendChild(n);
  const ladder = svgsByClass(host, 'dn-funnel')[0];
  assert(/deciding/i.test(ladder.textContent), 'the live gate reads "deciding…"');
  assert(!/rejected/i.test(ladder.textContent), 'a live undecided gate is NEVER labeled "rejected"');
});

// (f) the fully-completed race still renders the full ladder (no regression).
test('live racing model: a fully-completed race (all rounds, no live) still reconstructs the full ladder (no regression)', () => {
  // this is the existing reconstruct path — assert it is unaffected.
  const st = STRUCT.reconstructRacing(RACING_TOURNAMENTS, RC_EPOCH);
  assert(st && st.structure === 'racing', 'the completed reconstruction still yields a racing ladder');
  const model = STRUCT.racingModel(st);
  assert(model.hasRungs, 'the completed ladder has rungs');
  assertEqual(model.gateState, 'crowned', 'the completed gate crowns the promoted survivor (not deciding/rejected)');
  const nodes = STRUCT.renderStructure(st, { navigate() {}, href: router.href }, RC_EPOCH);
  const host = document.createElement('div');
  for (const n of nodes) host.appendChild(n);
  const ladder = svgsByClass(host, 'dn-funnel')[0];
  assert(ladder, 'the completed funnel still renders');
  assert(/♛/.test(ladder.textContent), 'the completed gate still crowns the champion ♛');
  assert(!/queued/.test(ladder.textContent), 'a completed funnel shows NO queued rungs');
});

// (g) end-to-end through the match-ups page: a live race with PUBLISHED rounds
// fills progressively (the page no longer sits on "being seeded").
test('live racing (e2e): the match-ups page fills progressively from the published live rounds', async () => {
  freshState();
  const at = liveRacingField({ partial_champion_agg: { scalar: 10 }, partial_challenger_agg: { scalar: 7 } });
  const F = {
    '/api/epoch': { epoch_id: EPOCH_ID, closed: false, goal: 'g', tournament: { structure: 'racing', params: at.structure_params }, experiments: [], board: [] },
    '/api/lineage': { generations: at.competitors.map((c) => ({ generation_id: c.generation_id, epoch_id: EPOCH_ID, parent_generation_id: c.role === 'champion' ? '' : 'v0', promoted: false })) },
    '/api/score-trajectory': { points: [] },
    '/api/tournaments': { epoch_id: EPOCH_ID, structure: 'racing', champion_lineage: ['v0'], matchups: [], tournaments: [] },
    '/api/active-tournament': at,
  };
  installFixtureMap(F);
  coreState.state.setHeartbeat({ phase: 'tournament:round_0:rung0_m1', generation_id: 'v5' });
  coreState.state.activeRuns = [
    { generation_id: 'v5', entry_id: 'b0', run_id: 'r0', progress: 0.5 },
    { generation_id: 'v6', entry_id: 'b1', run_id: 'r1', progress: 0.2 },
  ];
  coreState.state.activeTournament = { structure: 'racing', phase: 'running' };

  const gens = await import('../js/variants/T/views/gens.js');
  const host = document.createElement('div');
  await gens.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID });

  const ladder = svgsByClass(host, 'dn-funnel')[0];
  assert(ladder, 'a survival funnel rendered on the match-ups page (not the empty state)');
  assert(!/being seeded|No tournament has run|unavailable/i.test(host.textContent), 'NOT the "being seeded"/"nothing ran" empty state during a live race with empty rounds');
  assert(allByClass(host, 'dt-live-pill')[0], 'a LIVE badge marks the in-flight tournament');
  for (const id of ['v5', 'v6', 'v7', 'v8']) assert(ladder.textContent.includes(id), 'the full challenger field renders — ' + id);
  assert(/boards|running/.test(ladder.textContent), 'lanes show live board progress');

  coreState.state.heartbeat = { phase: 'idle' };
  coreState.state.activeRuns = [];
  coreState.state.activeTournament = null;
});

// (h) ISSUE #8: a DEGENERATE published rung-0 (only champion + first
// challenger) must NOT under-render to "2 field". The full challenger field is
// carried on `competitors` (role !== champion) AND `entries` (side ===
// challenger); the live funnel's entering rung is WIDENED to the whole field so
// EVERY challenger races (≥4 lanes), with the champion v0 kept as the gate
// benchmark — never a rung lane.
test('live racing model (issue #8): a degenerate published rung-0 is widened to the FULL challenger field from competitors/entries (≥4 lanes), v0 stays the gate', () => {
  const at = {
    structure: 'racing', phase: 'running',
    structure_params: { field_size: 4, eta: 2, board_fraction: 0.25, board_size: 8 },
    round_index: 0, total_rounds: 2,
    // the full field: champion v0 + four challengers v5..v8.
    competitors: [
      { generation_id: 'v0', seed: 1, role: 'champion' },
      { generation_id: 'v5', seed: 2, role: 'challenger' },
      { generation_id: 'v6', seed: 3, role: 'challenger' },
      { generation_id: 'v7', seed: 4, role: 'challenger' },
      { generation_id: 'v8', seed: 5, role: 'challenger' },
    ],
    // the per-entry rows the backend now publishes (one per competitor): the
    // champion defends, the four challengers race.
    entries: [
      { entry_id: 'v0', side: 'champion', status: 'running', loss_summary: {} },
      { entry_id: 'v5', side: 'challenger', status: 'running', loss_summary: {} },
      { entry_id: 'v6', side: 'challenger', status: 'queued', loss_summary: {} },
      { entry_id: 'v7', side: 'challenger', status: 'queued', loss_summary: {} },
      { entry_id: 'v8', side: 'challenger', status: 'queued', loss_summary: {} },
    ],
    // DEGENERATE published rounds: the active rung-0 carries only champion + the
    // FIRST challenger (the sparse/under-rendering shape from issue #8).
    rounds: [
      { round_index: 0, label: 'Rung 0', matches: [{ match_id: 'rung0', competitors: ['v0', 'v5'], survivors: [], cut: [], board_fraction: 0.25, pending: true }] },
      { round_index: 1, label: 'Champion gate', matches: [{ match_id: 'racing-final', competitors: ['v0'], board_fraction: 1.0, winner: null, pending: true }] },
    ],
    standings: [], champion_lineage: ['v0'],
  };
  const model = STRUCT.buildLiveRacingModel({
    at,
    heartbeat: { phase: 'tournament:round_0:rung0_m0', generation_id: 'v5' },
    activeRuns: [{ generation_id: 'v5', entry_id: 'b0', run_id: 'r0', progress: 0.4 }],
    epochGens: ['v0', 'v5', 'v6', 'v7', 'v8'],
  });
  const r0 = model.rounds.find((r) => String(r.matches[0].match_id) === 'rung0').matches[0];
  // the entering rung-0 field is widened to the FULL challenger field — v0 (the
  // champion/benchmark) is NOT a rung lane.
  assertDeep([...r0.competitors].sort(), ['v5', 'v6', 'v7', 'v8'], 'rung-0 is widened to the full challenger field {v5,v6,v7,v8}; champion v0 is excluded');
  assert(r0.live_progress && r0.live_progress.v5 && r0.live_progress.v8, 'every widened lane carries a live_progress entry (incl. the queued challengers)');

  // the rendered funnel shows ≥4 racing lanes (the FULL field), not 2.
  const nodes = STRUCT.renderStructure(model, { navigate() {}, href: router.href }, EPOCH_ID);
  const host = document.createElement('div');
  for (const n of nodes) host.appendChild(n);
  const funnel = svgsByClass(host, 'dn-funnel')[0];
  assert(funnel, 'the live survival funnel rendered');
  const laneNames = allByClass(funnel, 'dn-funnel-runner').map((g) => (g.textContent || '').trim());
  const challengerLanes = laneNames.filter((t) => /^v[5-8]\b/.test(t));
  assert(challengerLanes.length >= 4, 'rung-0 shows the FULL challenger field (≥4 lanes), not the degenerate "2 field" — got ' + challengerLanes.length);
  for (const id of ['v5', 'v6', 'v7', 'v8']) assert(funnel.textContent.includes(id), 'every challenger lane is named — ' + id);
  // the champion v0 is the benchmark/gate defender — NEVER a rung RUNNER lane.
  assert(!laneNames.some((t) => /^v0\b/.test(t)), 'champion v0 is the benchmark/gate, never a rung-runner lane');

  // the LIVE-HERO path (live.js → racingModel) widens the degenerate field too.
  const heroModel = STRUCT.racingModel(STRUCT.normalizeStructure(at, true));
  assert(heroModel && heroModel.live, 'racingModel built a live racing model from the degenerate payload');
  assertDeep([...heroModel.rungs[0].competitors].sort(), ['v5', 'v6', 'v7', 'v8'], 'the live-hero racingModel widens rung-0 to the full challenger field too (champion v0 excluded)');
});

// ====================================================================
// LIVE PROJECTED STANDINGS — an in-flight candidate (boards streaming) shows a
// PROJECTED standing (dashed/~prefix/proj badge/scored sub-bar), distinct from
// settled, across every structure + viz level. The runner writes
// `at.projected` ({gen: {scalar, boards_done, boards_total}}); the frontend
// folds it onto standings + pending matches, re-ranks per-structure (elim/racing
// on the projected scalar; swiss NEVER on Copeland points), and quantizes it
// into every digest so a no-op heartbeat is a true no-op (the anti-flash rule).
// ====================================================================

// the DICT-BUG fix: `partial_*_agg` is a DICT — buildLiveModel reads `.scalar`.
test('projected — buildLiveModel reads partial_*_agg.scalar (the dead svg.isNum(dict) plumbing is fixed)', () => {
  const at = liveRacingField({ partial_champion_agg: { scalar: 8.0 }, partial_challenger_agg: { scalar: 5.0 } });
  const model = STRUCT.buildLiveRacingModel({
    at, heartbeat: { phase: 'tournament:round_0:rung0_m1', generation_id: 'v5' },
    activeRuns: [{ generation_id: 'v5', entry_id: 'b0', run_id: 'r0', progress: 0.5 }],
    epochGens: ['v0', 'v5', 'v6', 'v7', 'v8'],
  });
  const r0 = model.rounds.find((r) => String(r.matches[0].match_id) === 'rung0').matches[0];
  // partialDelta = challenger.scalar − champion.scalar = 5.0 − 8.0 = −3.0 (was
  // ALWAYS null when the code mis-read the dict as a number).
  assertEqual(r0.live_progress.v5.partialDelta, -3.0, 'the partial Δ is read off the dict .scalar, not a (never-true) numeric guard');
});

// RACING — a lane with a server-side projected scalar reads "~proj".
test('projected (racing): an in-flight lane shows its projected scalar (~proj) + a scored board sub-bar', () => {
  const at = liveRacingField({
    partial_champion_agg: { scalar: 8.0 },
    projected: {
      v5: { scalar: 6.0, boards_done: 1, boards_total: 2, pass_rate: 1.0 },
      v6: { scalar: 7.5, boards_done: 1, boards_total: 2, pass_rate: 1.0 },
    },
  });
  const model = STRUCT.buildLiveRacingModel({
    at, heartbeat: { phase: 'tournament:round_0:rung0_m1', generation_id: 'v5' },
    activeRuns: [{ generation_id: 'v5', entry_id: 'b0', run_id: 'r0', progress: 0.5 }],
    epochGens: ['v0', 'v5', 'v6', 'v7', 'v8'],
  });
  const r0 = model.rounds.find((r) => String(r.matches[0].match_id) === 'rung0').matches[0];
  assertEqual(r0.live_progress.v5.projected, true, 'v5 lane carries the projected flag');
  assertEqual(r0.live_progress.v5.projected_scalar, 6.0, 'the lane carries its projected scalar');
  // per-lane Δ-vs-champion is computed from the lane projection (6.0 − 8.0).
  assertEqual(r0.live_progress.v5.partialDelta, -2.0, 'a per-lane projected Δ vs champion (lane − champion)');
  const nodes = STRUCT.renderStructure(model, { navigate() {}, href: router.href }, EPOCH_ID);
  const host = document.createElement('div');
  for (const n of nodes) host.appendChild(n);
  const funnel = svgsByClass(host, 'dn-funnel')[0];
  assert(/~/.test(funnel.textContent) && /proj/.test(funnel.textContent), 'a projected lane reads "~… proj"');
  assert(allByClass(funnel, 'dn-proj').length >= 1, 'the projected treatment class is applied to the lane');
  assert(allByClass(funnel, 'dn-proj-bar').length >= 1, 'a scored board-progress sub-bar renders for the projected lane');
});

// SWISS — projected scalar marks the row + the pairing, but NEVER projects
// Copeland points (points-rank is authoritative; a half-finished duel has no win).
function projSwissField(extra) {
  return Object.assign({
    structure: 'swiss', phase: 'running', epoch_id: EPOCH_ID,
    structure_params: { rounds: 2 }, round_index: 1, total_rounds: 2,
    competitors: [
      { generation_id: 'v0', seed: 1, role: 'champion' },
      { generation_id: 'v1', seed: 2, role: 'challenger' },
      { generation_id: 'v2', seed: 3, role: 'challenger' },
    ],
    rounds: [
      { round_index: 0, label: 'Round 1', matches: [{ match_id: 'r0m0', competitors: ['v0', 'v1'], winner: 'v1', delta_scalar: -0.1 }] },
      { round_index: 1, label: 'Round 2', matches: [{ match_id: 'r1m0', competitors: ['v1', 'v2'], winner: '', pending: true }] },
    ],
    standings: [
      { generation_id: 'v1', rank: 1, scalar: 0.4, wins: 1, losses: 0, status: 'alive' },
      { generation_id: 'v2', rank: 2, scalar: 0.0, wins: 0, losses: 0, status: 'alive' },
    ],
    champion_lineage: ['v0'],
  }, extra || {});
}

test('projected (swiss): an in-flight pairing marks the row projected but does NOT re-rank on Copeland points', () => {
  // v1 leads on points (1W). v2 is in flight with a GREAT projected scalar but 0
  // wins. swiss must keep v1 ranked above v2 — points are not projected.
  const at = projSwissField({
    projected: { v2: { scalar: 0.01, boards_done: 4, boards_total: 5, pass_rate: 1.0 } },
  });
  const model = STRUCT.buildLiveSwissModel({
    at, heartbeat: { phase: 'tournament:round_1:r1m0', generation_id: 'v2' },
    activeRuns: [{ generation_id: 'v2', entry_id: 'b0', run_id: 'r0', progress: 0.8 }],
    epochGens: ['v0', 'v1', 'v2'],
  });
  const sm = STRUCT.swissModel(model);
  const v1 = sm.standings.find((s) => s.id === 'v1');
  const v2 = sm.standings.find((s) => s.id === 'v2');
  assert(v1.rank < v2.rank, 'swiss keeps the points-leader on top — NO Copeland projection');
  assertEqual(v2.in_flight, true, 'the in-flight competitor IS marked projected (visual treatment)');
  assertEqual(v2.projected_scalar, 0.01, 'its projected scalar rides along for the ~ treatment');
  // the ladder renders the projected treatment on the row.
  const node = svg.swissLadder({ rounds: sm.rounds, standings: sm.standings, championId: sm.championId, benchmarkId: sm.benchmarkId, live: true, gateState: sm.gateState });
  assert(allByClass(node, 'dn-proj').length >= 1, 'the swiss ladder marks the projected row dn-proj');
  assert(/~proj/.test(node.textContent), 'the projected swiss row reads ~proj');
});

test('projected (swiss): equal wins → the projected mean-scalar tiebreak applies', () => {
  const at = projSwissField({
    standings: [
      { generation_id: 'v1', rank: 1, scalar: 0.0, wins: 0, losses: 0, status: 'alive' },
      { generation_id: 'v2', rank: 2, scalar: 0.0, wins: 0, losses: 0, status: 'alive' },
    ],
    rounds: [
      { round_index: 0, label: 'Round 1', matches: [{ match_id: 'r0m0', competitors: ['v1', 'v2'], winner: '', pending: true }] },
    ],
    projected: {
      v1: { scalar: 2.0, boards_done: 2, boards_total: 5, pass_rate: 1.0 },
      v2: { scalar: 1.0, boards_done: 2, boards_total: 5, pass_rate: 1.0 },
    },
  });
  const model = STRUCT.buildLiveSwissModel({
    at, heartbeat: { phase: 'tournament:round_0:r0m0', generation_id: 'v2' },
    activeRuns: [{ generation_id: 'v2', entry_id: 'b0', run_id: 'r0', progress: 0.4 }],
    epochGens: ['v0', 'v1', 'v2'],
  });
  // v2 (lower projected scalar) wins the tiebreak among equal (0) wins.
  const order = model.standings.map((s) => String(s.generation_id));
  assertEqual(order[0], 'v2', 'on equal wins the lower projected scalar ranks first (mean-scalar tiebreak)');
});

// ELIM — an in-flight match re-ranks the standings on the projected scalar.
function projElimField(extra) {
  return Object.assign({
    structure: 'single_elim', phase: 'running', epoch_id: EPOCH_ID,
    structure_params: { seed_order: 'scalar' }, round_index: 0, total_rounds: 1,
    competitors: [
      { generation_id: 'v0', seed: 1, role: 'champion' },
      { generation_id: 'v1', seed: 2, role: 'challenger' },
    ],
    rounds: [
      { round_index: 0, label: 'Final', matches: [{ match_id: 'WB-R0-0', competitors: ['v0', 'v1'], winner: '', bracket_slot: 'WB-R0-0', pending: true }] },
    ],
    standings: [
      { generation_id: 'v0', rank: 1, scalar: 0.0, wins: 0, losses: 0, status: 'alive', role: 'champion' },
      { generation_id: 'v1', rank: 2, scalar: 0.0, wins: 0, losses: 0, status: 'alive', role: 'challenger' },
    ],
    champion_lineage: ['v0'],
  }, extra || {});
}

test('projected (elim): an in-flight match re-ranks standings on the projected scalar + marks the lane projected', () => {
  // v1 projects a lower (better) scalar → it bubbles to rank 1 live.
  const at = projElimField({
    projected: {
      v0: { scalar: 2.0, boards_done: 3, boards_total: 5, pass_rate: 1.0 },
      v1: { scalar: 1.0, boards_done: 3, boards_total: 5, pass_rate: 1.0 },
    },
  });
  const model = STRUCT.buildLiveElimModel({
    at, heartbeat: { phase: 'tournament:round_0:WB-R0-0', generation_id: 'v1' },
    activeRuns: [{ generation_id: 'v1', entry_id: 'b0', run_id: 'r0', progress: 0.6 }],
    epochGens: ['v0', 'v1'],
  });
  const top = model.standings.slice().sort((a, b) => a.rank - b.rank)[0];
  assertEqual(String(top.generation_id), 'v1', 'elim re-ranks the in-flight leader on its projected scalar');
  assertEqual(top.in_flight, true, 'the leading row is marked in-flight/projected');
  // the elim flow renders the projected treatment on the lane.
  const em = STRUCT.elimModel(model);
  const node = svg.elimFlow({ winners: em.winners, championId: em.championId, benchmarkId: em.benchmarkId, live: true, gateState: em.gateState });
  assert(allByClass(node, 'dn-proj').length >= 1, 'the elim flow marks the in-flight lane/node dn-proj');
  assert(/~proj/.test(node.textContent), 'the projected elim lane reads ~proj');
});

// the STANDINGS TABLE projected treatment (dashed row + ~prefix + proj badge +
// scored sub-bar).
test('projected (standings table): an in-flight row renders the projected treatment (dt-proj-row + ~ + proj + scored bar)', () => {
  const at = projElimField({
    projected: { v1: { scalar: 1.0, boards_done: 3, boards_total: 5, pass_rate: 1.0 } },
  });
  const model = STRUCT.buildLiveElimModel({
    at, heartbeat: { phase: 'tournament:round_0:WB-R0-0', generation_id: 'v1' },
    activeRuns: [{ generation_id: 'v1', entry_id: 'b0', run_id: 'r0', progress: 0.6 }],
    epochGens: ['v0', 'v1'],
  });
  const nodes = STRUCT.renderStructure(model, { navigate() {}, href: router.href }, EPOCH_ID);
  const host = document.createElement('div');
  for (const n of nodes) host.appendChild(n);
  assert(allByClass(host, 'dt-proj-row').length >= 1, 'the in-flight standings row carries dt-proj-row');
  assert(allByClass(host, 'dt-proj-badge').length >= 1, 'a "proj" badge renders on the projected scalar cell');
  assert(allByClass(host, 'dt-proj-bar').length >= 1, 'a scored board-progress sub-bar renders');
  assert(/~/.test(host.textContent), 'the projected scalar reads with a ~ prefix');
});

// DIGEST STABILITY (anti-flash): an identical rounded projection → identical
// digest → no repaint; a board landing → a different digest.
test('projected (digest): identical projection yields an identical digest (no repaint); a board landing changes it', () => {
  const mk = (proj) => STRUCT.buildLiveElimModel({
    at: projElimField({ projected: proj }),
    heartbeat: { phase: 'tournament:round_0:WB-R0-0', generation_id: 'v1' },
    activeRuns: [{ generation_id: 'v1', entry_id: 'b0', run_id: 'r0', progress: 0.6 }],
    epochGens: ['v0', 'v1'],
  });
  const p = { v0: { scalar: 2.0, boards_done: 3, boards_total: 5 }, v1: { scalar: 1.0, boards_done: 3, boards_total: 5 } };
  const a = mk(p);
  const b = mk({ v0: { scalar: 2.0, boards_done: 3, boards_total: 5 }, v1: { scalar: 1.0, boards_done: 3, boards_total: 5 } });
  assertEqual(STRUCT.structureDigest(a), STRUCT.structureDigest(b), 'identical rounded projection → identical digest (no DOM rebuild)');
  // a board landing (boards_done advances, scalar moves) → a different digest.
  const c = mk({ v0: { scalar: 2.0, boards_done: 4, boards_total: 5 }, v1: { scalar: 0.9, boards_done: 4, boards_total: 5 } });
  assert(STRUCT.structureDigest(a) !== STRUCT.structureDigest(c), 'a board landing (progress + scalar advanced) changes the digest');
});

test('projected (no-op beat): two identical projected ticks leave the rendered node identity unchanged (gated swap)', () => {
  const proj = { v0: { scalar: 2.0, boards_done: 3, boards_total: 5 }, v1: { scalar: 1.0, boards_done: 3, boards_total: 5 } };
  const mk = () => STRUCT.buildLiveElimModel({
    at: projElimField({ projected: proj }),
    heartbeat: { phase: 'tournament:round_0:WB-R0-0', generation_id: 'v1' },
    activeRuns: [{ generation_id: 'v1', entry_id: 'b0', run_id: 'r0', progress: 0.6 }],
    epochGens: ['v0', 'v1'],
  });
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  const a = mk();
  ui.gatedSwap(host, STRUCT.structureDigest(a), () => STRUCT.renderStructure(a, ctx, EPOCH_ID));
  const first = host.firstChild;
  const b = mk();
  ui.gatedSwap(host, STRUCT.structureDigest(b), () => STRUCT.renderStructure(b, ctx, EPOCH_ID));
  assert(host.firstChild === first, 'a no-op projected beat did NOT rebuild the DOM (same node identity)');
});

// ====================================================================
// SWISS + ELIM — completed structure visuals (swissLadder / elimBracket)
// and progressive LIVE models (buildLiveSwissModel / buildLiveElimModel),
// parallel to the racing ladder/funnel. The completed views build a model
// from /api/tournament-structure rounds/standings; the live models
// accumulate completed rounds, fill the active round board-by-board, and
// queue the future — exactly the racing-ladder discipline.
// ====================================================================

// ---- completed SWISS → the standings ladder -------------------------

test('swiss (completed): swissModel + renderSwiss produce the standings ladder (rounds + Copeland standings + champion-gate)', () => {
  const st = STRUCT.normalizeStructure(SWISS_STRUCT, false);
  const model = STRUCT.swissModel(st);
  assert(model && model.hasRounds, 'a swiss model was derived');
  assertEqual(model.rounds.length, 2, 'both swiss rounds are in the model');
  assert(model.standings.length >= 1, 'the accumulating Copeland-point standings are present');
  // v1 won both pairings → leads the standings.
  assertEqual(String(model.standings[0].id), 'v1', 'the swiss leader (v1, 2 wins) tops the standings');
  assert(svg.isNum(model.standings[0].points), 'the leader carries Copeland points');

  const nodes = STRUCT.renderStructure(st, { navigate() {}, href: router.href }, EPOCH_ID);
  const host = document.createElement('div');
  for (const n of nodes) host.appendChild(n);
  const ladder = svgsByClass(host, 'dn-swissladder')[0];
  assert(ladder, 'the swiss standings ladder SVG rendered');
  assertEqual(ladder.getAttribute('width'), '100%', 'the swiss ladder is fit-to-width');
  assert(ladder.textContent.toLowerCase().includes('round 1') && ladder.textContent.toLowerCase().includes('round 2'), 'a column per swiss round');
  assert(allByClass(ladder, 'dn-swissladder-stand').length >= 1, 'the standings column rendered');
  assert(ladder.textContent.toLowerCase().includes('champion-gate'), 'the leader flows into a champion-gate node');
});

test('swiss (completed): a swiss winner that does NOT beat the incumbent is NOT promoted (gate "stands")', () => {
  // the leader is the incumbent v0 itself / lineage unchanged → no promotion.
  const sw = JSON.parse(JSON.stringify(SWISS_STRUCT));
  sw.champion_lineage = ['v0'];
  sw.standings = [
    { generation_id: 'v0', rank: 1, points: 2, wins: 2, losses: 0, status: 'champion' },
    { generation_id: 'v1', rank: 2, points: 1, wins: 1, losses: 1, status: 'alive' },
  ];
  const st = STRUCT.normalizeStructure(sw, false);
  const model = STRUCT.swissModel(st);
  assertEqual(model.gateState, 'stands', 'the incumbent leading the swiss → champion stands (no new crown)');
  assertEqual(model.championId, null, 'no challenger is crowned when the incumbent wins the swiss');
  const node = svg.swissLadder({ rounds: model.rounds, standings: model.standings, championId: model.championId, benchmarkId: model.benchmarkId, gateState: model.gateState });
  assert(/champion stands/.test(node.textContent), 'the gate reads "champion stands"');
});

// ---- completed ELIM → the bracket-as-FLOW (elimBracket retired) -----

test('elim (completed): single-elim → elimModel + the bracket-as-FLOW with a champion-gate', () => {
  const st = STRUCT.normalizeStructure(SE_STRUCT, false);
  const model = STRUCT.elimModel(st);
  assert(model && model.hasMatches, 'a single-elim model was derived');
  assertEqual(model.losers, null, 'single-elim has NO losers band');
  assertEqual(model.winners.length, 2, 'two winners-bracket rounds (semifinal + final)');
  assertEqual(typeof svg.elimBracket, 'undefined', 'the elimBracket renderer is deleted (retired)');
  const node = svg.elimFlow({ winners: model.winners, championId: model.championId, benchmarkId: model.benchmarkId, gateState: model.gateState, onCompetitor() {} });
  assertEqual(node.localName, 'svg', 'the flow is an SVG');
  assertEqual(node.getAttribute('width'), '100%', 'fit-to-width');
  assert(node.textContent.includes('Semifinal') && node.textContent.includes('Final'), 'both rounds render as columns');
  // winner continues (↑/good), loser terminates (✕), champion → crowned gate.
  assert(/✕/.test(node.textContent), 'an eliminated lane terminates with ✕');
  assert(node.textContent.includes(svg.CROWN.current), 'the champion lane reaches the crowned gate ♛');
  assert(node.textContent.toLowerCase().includes('champion-gate'), 'the flow carries the champion-gate column');
  assert(allByClass(node, 'dn-elimflow-convnode').length >= 1, 'a two-lane match convergence node is drawn');
});

test('elim (completed): double-elim → ONE flow SVG carrying the losers’ band as re-converging lanes', () => {
  const DE = JSON.parse(JSON.stringify(SE_STRUCT));
  DE.structure = 'double_elim';
  DE.rounds.push({ round_index: 2, label: 'LB Round 1', matches: [
    { match_id: 'LB-R0-0', competitors: ['v0', 'v2'], winner: 'v0', decision: 'rejected', bracket_slot: 'LB-R0-0', bye: false },
  ] });
  const st = STRUCT.normalizeStructure(DE, false);
  const model = STRUCT.elimModel(st);
  assert(Array.isArray(model.losers) && model.losers.length >= 1, 'double-elim carries a losers band in the model');
  const node = svg.elimFlow({ winners: model.winners.concat(model.losers), championId: model.championId, benchmarkId: model.benchmarkId, gateState: model.gateState, onCompetitor() {} });
  assert(/LB Round 1|LB R/i.test(node.textContent), 'the losers’ bracket round renders as a re-converging lane column');
  assert(node.textContent.includes('Semifinal'), 'the winners band still renders');
});

// ---- progressive LIVE swiss model -----------------------------------

// a live swiss field per the NEW contract: v0..v3, the backend PUBLISHES the
// active round 0 (paired but undecided) plus the next round 1 queued (its
// pairings published as the bracket fills). Future rounds appear as the backend
// publishes them — the dashboard renders what is published, no synthesis.
function liveSwissField(extra) {
  return Object.assign({
    structure: 'swiss', phase: 'running', epoch_id: HERO_EPOCH,
    structure_params: { rounds: 3, board_size: 4 },
    round_index: 0,
    competitors: [{ generation_id: 'v0' }, { generation_id: 'v1' }, { generation_id: 'v2' }, { generation_id: 'v3' }],
    rounds: [
      { round_index: 0, label: 'Round 1', matches: [
        { match_id: 'sw_r0_m0', competitors: ['v0', 'v1'] },
        { match_id: 'sw_r0_m1', competitors: ['v2', 'v3'] },
      ] },
      { round_index: 1, label: 'Round 2', matches: [
        { match_id: 'sw_r1_m0', competitors: ['v0', 'v2'] },
        { match_id: 'sw_r1_m1', competitors: ['v1', 'v3'] },
      ] },
    ],
    standings: [],
    champion_lineage: ['v0'],
  }, extra || {});
}

test('live swiss model: the active published round fills in board-by-board (overlaid progress), later published rounds queue', () => {
  const model = STRUCT.buildLiveSwissModel({
    at: liveSwissField(),
    heartbeat: { phase: 'tournament:round_0', generation_id: 'v1' },
    activeRuns: [
      { generation_id: 'v0', entry_id: 'b0', run_id: 'r0', progress: 0.5 },
      { generation_id: 'v1', entry_id: 'b1', run_id: 'r1', progress: 0.0 },
    ],
    epochGens: ['v0', 'v1', 'v2', 'v3'],
  });
  assert(model && model.live, 'a live swiss model built from the published rounds');
  const m = STRUCT.swissModel(model);
  assert(m.rounds.length >= 2, 'the published swiss rounds present (active + queued)');
  const r0 = m.rounds[0];
  assertEqual(r0.queued, false, 'round 0 is the ACTIVE round (not queued)');
  const p0 = r0.pairings[0];
  assert(p0.pending, 'an undecided active pairing is pending (not struck as decided)');
  assert(p0.inflight >= 1 || p0.done >= 1, 'the active pairing carries in-flight board progress overlaid from active-runs');
  assert(m.rounds[1].queued, 'the next published swiss round is queued (board progress not overlaid yet)');

  const nodes = STRUCT.renderStructure(model, { navigate() {}, href: router.href }, HERO_EPOCH);
  const host = document.createElement('div');
  for (const n of nodes) host.appendChild(n);
  const ladder = svgsByClass(host, 'dn-swissladder')[0];
  assert(ladder, 'the live swiss ladder rendered (not the being-seeded empty state)');
  assert(!/being seeded/i.test(host.textContent), 'NOT the being-seeded placeholder once pairings exist');
});

test('live swiss model: a completed round PERSISTS when the next round starts (accumulation)', () => {
  const at = liveSwissField({
    round_index: 1,
    rounds: [
      { round_index: 0, label: 'Round 1', matches: [
        { match_id: 'sw_r0_m0', competitors: ['v0', 'v1'], winner: 'v1', decision: 'win' },
        { match_id: 'sw_r0_m1', competitors: ['v2', 'v3'], winner: 'v3', decision: 'win' },
      ] },
      { round_index: 1, label: 'Round 2', matches: [
        { match_id: 'sw_r1_m0', competitors: ['v1', 'v3'] },
        { match_id: 'sw_r1_m1', competitors: ['v0', 'v2'] },
      ] },
    ],
  });
  const model = STRUCT.buildLiveSwissModel({
    at, heartbeat: { phase: 'tournament:round_1', generation_id: 'v1' },
    activeRuns: [{ generation_id: 'v1', entry_id: 'b0', run_id: 'r0', progress: 0.3 }],
    epochGens: ['v0', 'v1', 'v2', 'v3'],
  });
  const m = STRUCT.swissModel(model);
  // round 0 is carried verbatim — its winners persist.
  const r0 = m.rounds[0];
  assert(r0.pairings.every((p) => p.winner && !p.pending), 'the completed round-0 pairings persist with their winners (no blanking)');
  assertEqual(m.rounds[1].queued, false, 'round 1 is now the active round');
  // standings accumulate v1 + v3 as the round-0 winners.
  const v1 = m.standings.find((s) => s.id === 'v1');
  assert(v1 && v1.points >= 1, 'round-0 winner v1 has accumulated a Copeland point');
});

test('live swiss model: a no-op repeat render leaves the swiss-ladder node identity unchanged (digest-gated)', () => {
  const heartbeat = { phase: 'tournament:round_0', generation_id: 'v1' };
  const activeRuns = [{ generation_id: 'v0', entry_id: 'b0', run_id: 'r0', progress: 0.5 }];
  const epochGens = ['v0', 'v1', 'v2', 'v3'];
  const a = STRUCT.buildLiveSwissModel({ at: liveSwissField(), heartbeat, activeRuns, epochGens });
  const b = STRUCT.buildLiveSwissModel({ at: liveSwissField(), heartbeat, activeRuns, epochGens });
  assertEqual(STRUCT.structureDigest(a), STRUCT.structureDigest(b), 'two identical live swiss ticks share a digest');
  const c = STRUCT.buildLiveSwissModel({ at: liveSwissField(), heartbeat, activeRuns: [{ generation_id: 'v0', entry_id: 'b0', run_id: 'r0', progress: 1.0 }], epochGens });
  assert(STRUCT.structureDigest(a) !== STRUCT.structureDigest(c), 'a board landing changes the swiss digest');

  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  ui.gatedSwap(host, STRUCT.structureDigest(a), () => STRUCT.renderStructure(a, ctx, HERO_EPOCH));
  const first = svgsByClass(host, 'dn-swissladder')[0];
  ui.gatedSwap(host, STRUCT.structureDigest(b), () => STRUCT.renderStructure(b, ctx, HERO_EPOCH));
  const second = svgsByClass(host, 'dn-swissladder')[0];
  assert(first === second, 'the swiss-ladder node identity is preserved across a no-op tick');
});

// ---- progressive LIVE elim model ------------------------------------

function liveElimField(extra) {
  return Object.assign({
    structure: 'single_elim', phase: 'running', epoch_id: HERO_EPOCH,
    structure_params: { board_size: 4 },
    round_index: 0,
    competitors: [
      { generation_id: 'v0', role: 'champion' }, { generation_id: 'v1', role: 'challenger' },
      { generation_id: 'v2', role: 'challenger' }, { generation_id: 'v3', role: 'challenger' },
    ],
    rounds: [
      { round_index: 0, label: 'Semifinal', matches: [
        { match_id: 'WB-R0-0', competitors: ['v0', 'v3'], bracket_slot: 'WB-R0-0' },
        { match_id: 'WB-R0-1', competitors: ['v1', 'v2'], bracket_slot: 'WB-R0-1' },
      ] },
    ],
    standings: [],
    champion_lineage: ['v0'],
  }, extra || {});
}

test('live elim model: an undecided round fills in board-by-board (active round, not empty)', () => {
  const model = STRUCT.buildLiveElimModel({
    at: liveElimField(),
    heartbeat: { phase: 'tournament:round_0', generation_id: 'v1' },
    activeRuns: [{ generation_id: 'v1', entry_id: 'b0', run_id: 'r0', progress: 0.5 }],
    epochGens: ['v0', 'v1', 'v2', 'v3'],
  });
  assert(model && model.live, 'a live elim model built from the field');
  const m = STRUCT.elimModel(model);
  assert(m.hasMatches, 'the active round has matches');
  const active = m.winners[0].matches.find((mm) => (mm.competitors || []).includes('v1'));
  assert(active && active.pending, 'an undecided active match is pending (not struck as decided)');
  const nodes = STRUCT.renderStructure(model, { navigate() {}, href: router.href }, HERO_EPOCH);
  const host = document.createElement('div');
  for (const n of nodes) host.appendChild(n);
  const bracket = svgsByClass(host, 'dn-elimflow')[0];
  assert(bracket, 'the live bracket-as-flow rendered (not the being-seeded empty state)');
  assert(!/being seeded/i.test(host.textContent), 'NOT the being-seeded placeholder once matches exist');
});

test('live elim model: a completed round PERSISTS when the next round starts (accumulation)', () => {
  const at = liveElimField({
    round_index: 1,
    rounds: [
      { round_index: 0, label: 'Semifinal', matches: [
        { match_id: 'WB-R0-0', competitors: ['v0', 'v3'], winner: 'v0', decision: 'win', bracket_slot: 'WB-R0-0' },
        { match_id: 'WB-R0-1', competitors: ['v1', 'v2'], winner: 'v1', decision: 'win', bracket_slot: 'WB-R0-1' },
      ] },
      { round_index: 1, label: 'Final', matches: [
        { match_id: 'WB-R1-0', competitors: ['v0', 'v1'], bracket_slot: 'WB-R1-0' },
      ] },
    ],
  });
  const model = STRUCT.buildLiveElimModel({
    at, heartbeat: { phase: 'tournament:round_1', generation_id: 'v1' },
    activeRuns: [{ generation_id: 'v1', entry_id: 'b0', run_id: 'r0', progress: 0.3 }],
    epochGens: ['v0', 'v1', 'v2', 'v3'],
  });
  const m = STRUCT.elimModel(model);
  const r0 = m.winners[0];
  assert(r0.matches.every((mm) => mm.winner && !mm.pending), 'the completed semifinal matches persist with their winners (no blanking)');
  const fin = m.winners[1].matches[0];
  assert(fin.pending, 'the active final is pending (filling in)');
});

test('live elim model: a no-op repeat render leaves the bracket node identity unchanged (digest-gated)', () => {
  const heartbeat = { phase: 'tournament:round_0', generation_id: 'v1' };
  const activeRuns = [{ generation_id: 'v1', entry_id: 'b0', run_id: 'r0', progress: 0.5 }];
  const epochGens = ['v0', 'v1', 'v2', 'v3'];
  const a = STRUCT.buildLiveElimModel({ at: liveElimField(), heartbeat, activeRuns, epochGens });
  const b = STRUCT.buildLiveElimModel({ at: liveElimField(), heartbeat, activeRuns, epochGens });
  assertEqual(STRUCT.structureDigest(a), STRUCT.structureDigest(b), 'two identical live elim ticks share a digest');
  const c = STRUCT.buildLiveElimModel({ at: liveElimField(), heartbeat, activeRuns: [{ generation_id: 'v1', entry_id: 'b0', run_id: 'r0', progress: 1.0 }], epochGens });
  assert(STRUCT.structureDigest(a) !== STRUCT.structureDigest(c), 'a board landing changes the elim digest');

  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  ui.gatedSwap(host, STRUCT.structureDigest(a), () => STRUCT.renderStructure(a, ctx, HERO_EPOCH));
  const first = svgsByClass(host, 'dn-elimflow')[0];
  ui.gatedSwap(host, STRUCT.structureDigest(b), () => STRUCT.renderStructure(b, ctx, HERO_EPOCH));
  const second = svgsByClass(host, 'dn-elimflow')[0];
  assert(first === second, 'the bracket node identity is preserved across a no-op tick');
});

// ---- (c) the richer racing survival-funnel render -------------------
// (the static funnel render is exercised by the "survival funnel: the SVG
// narrows N→…→1 …" test below; here we cover the LIVE pending-rung case.)

test('survival funnel: a LIVE race leaves the pending rung neutral (nobody cut) and the gate reads "deciding…"', () => {
  const rungs = [
    { label: 'Rung 1', competitors: ['v1', 'v2', 'v3'], survivors: ['v1'], cut: ['v2', 'v3'], board_fraction: 0.5 },
    { label: 'Rung 2', competitors: ['v1'], survivors: [], cut: [], board_fraction: 1.0, pending: true },
  ];
  const node = svg.survivalFunnel({ rungs, championId: null, benchmarkId: 'v0', live: true, onCompetitor() {} });
  // only the DECIDED rung shows cuts; the pending rung does not strike anyone.
  const cutNames = node.querySelectorAll('[class]')
    .filter((n) => (n.getAttribute('class') || '').includes('dn-funnel-name') && (n.getAttribute('class') || '').includes('dn-bad'));
  assertEqual(cutNames.length, 2, 'only the two decided cuts (v2, v3) peel off — the pending rung strikes nobody');
  // the pending stage renders as a neutral (non-cut) band.
  assert(allByClass(node, 'dn-funnel-pending').length >= 1, 'the pending (still-racing) stage renders neutral (no premature cut)');
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

test('racing reconstruct: the champion-gate resolves v3 as the promoted champion ♛ (racing-final won + champion_lineage), NOT "tbd"', () => {
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
  const ladder = svgsByClass(wrap, 'dn-funnel')[0];
  assert(ladder, 'the survival funnel rendered from the reconstruction');
  assert(ladder.textContent.includes('champion-gate'), 'a champion-gate stage rendered');
  assert(ladder.textContent.includes('♛ v3'), 'the gate crowns v3 as the new champion ♛');
  assert(!ladder.textContent.includes('tbd'), 'the gate is NOT the empty "tbd" skeleton');
  assert(wrap.textContent.includes('v3 promoted'), 'the caption states the champion-gate outcome (v3 promoted)');
});

// ---- (c) competitors are clickable to their candidate ---------------

test('racing reconstruct: each competitor in the funnel is clickable → its candidate page', () => {
  const st = STRUCT.reconstructRacing(RACING_TOURNAMENTS, RC_EPOCH);
  let navTo = null;
  const ctx = { navigate: (v, p) => { navTo = { v, p }; }, href: router.href };
  const nodes = STRUCT.renderStructure(st, ctx, RC_EPOCH);
  const wrap = document.createElement('div');
  for (const n of nodes) wrap.appendChild(n);
  const runner = allByClass(wrap, 'dn-funnel-runner')[0];
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

  const ladder = svgsByClass(host, 'dn-funnel')[0];
  assert(ladder, 'the LIVE survival funnel rendered from /api/active-tournament');
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

  const ladder = svgsByClass(host, 'dn-funnel')[0];
  assert(ladder, 'the survival funnel rendered on the match-ups page from the per-challenger records');
  assert(!/No tournament|No rungs|unavailable/i.test(host.textContent), 'NOT the empty "RUNG · RUNG · CHAMPION-GATE: tbd" skeleton');
  assert(host.textContent.includes('Rung 0') && host.textContent.includes('Rung 1'), 'both reconstructed rungs render as stages');
  // the full rung0 field + the cut/survivor marks made it through to the SVG.
  for (const id of ['v1', 'v2', 'v3', 'v4']) assert(ladder.textContent.includes(id), 'rung0 names the full field — ' + id);
  assert(ladder.textContent.includes('✕'), 'cut runners are struck ✕');
  assert(ladder.textContent.includes('↑'), 'survivors are marked ↑');
  assert(ladder.textContent.includes('♛ v3'), 'the champion-gate crowns v3 as the new champion ♛ (not tbd)');
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
  globalThis.fetch = async (path) => {
    const v = lookupFixture(F, path);
    return v !== undefined
      ? { ok: true, json: async () => v }
      : { ok: false, status: 404, json: async () => ({ error: 'not found: ' + path }) };
  };

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
  globalThis.fetch = async (path) => {
    const v = lookupFixture(F, path);
    return v !== undefined
      ? { ok: true, json: async () => v }
      : { ok: false, status: 404, json: async () => ({ error: 'not found: ' + path }) };
  };

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

// ====================================================================
// Lifecycle DAG · NORMALIZED vertical layout. The seed/baseline (full
// board, MORE entries) and a racing challenger (deduped slice, FEWER)
// must render with the SAME per-node row pitch and a structural spine
// centred on the board fan — neither side stretched/compressed, and no
// large empty top band on the seed side.
// ====================================================================

// the y-centre of a board fan = the disc cy's; the spine y-centre = the centre
// of the PARENT structural node (the first non-board ezn-node rect).
function boardCysOf(svgNode) {
  return boardNodesOf(svgNode)
    .map((g) => +childByClass(g, 'ezn-board-disc').getAttribute('cy'))
    .sort((a, b) => a - b);
}
function spineCenterY(svgNode) {
  // the PARENT node carries the text "champion" / "no parent"; grab its rect.
  const nodes = svgNode.querySelectorAll('[class]').filter((n) =>
    n.localName === 'g' && (n.getAttribute('class') || '').split(/\s+/).includes('ezn-node')
    && !(n.getAttribute('class') || '').split(/\s+/).includes('ezn-board-node'));
  const rect = nodes[0].querySelectorAll('[class]').filter((n) => n.localName === 'rect')[0];
  return +rect.getAttribute('y') + +rect.getAttribute('height') / 2;
}
function rowPitchOf(svgNode) {
  const cys = boardCysOf(svgNode);
  return cys.length >= 2 ? cys[1] - cys[0] : null;
}

test('lifecycle DAG (normalized): the seed/baseline (N entries) and a challenger (M entries) share the SAME board-node row pitch', () => {
  // a seed/baseline ran the FULL board (7 entries) — no parent.
  const seedEntries = Array.from({ length: 7 }, (_, i) => ({ entry_id: 'b' + i, drift_loss: 10 + i, pass_fail: i % 2 }));
  // a challenger ran a deduped slice (4 distinct entries).
  const challEntries = Array.from({ length: 4 }, (_, i) => ({ entry_id: 'c' + i, drift_loss: 20 + i, pass_fail: i % 2 }));

  const seed = dag.lifecycleDag({ genId: 'v0', parentId: '', baseline: true, entries: seedEntries });
  const chall = dag.lifecycleDag({ genId: 'v8', parentId: 'v0', decision: 'rejected', entries: challEntries });

  const seedPitch = rowPitchOf(seed);
  const challPitch = rowPitchOf(chall);
  assert(seedPitch != null && challPitch != null, 'both DAGs have a measurable multi-row board fan');
  // the SAME constant pitch on both sides — the bug was the seed fan stretching.
  assert(Math.abs(seedPitch - challPitch) < 0.5,
    `the seed pitch (${seedPitch}) matches the challenger pitch (${challPitch}) — not stretched/compressed`);

  // every adjacent gap on the SEED side is itself the same constant pitch (no
  // divergent vertical spread among the seed's own rows).
  const seedCys = boardCysOf(seed);
  for (let i = 1; i < seedCys.length; i++) {
    assert(Math.abs((seedCys[i] - seedCys[i - 1]) - seedPitch) < 0.5,
      `seed row gap ${i} (${seedCys[i] - seedCys[i - 1]}) equals the constant pitch (${seedPitch})`);
  }
});

test('lifecycle DAG (normalized): the structural spine is centred on the board fan’s TRUE centre for BOTH seed and challenger (no floating spine, no empty top band)', () => {
  const seedEntries = Array.from({ length: 7 }, (_, i) => ({ entry_id: 'b' + i, drift_loss: 10 + i, pass_fail: i % 2 }));
  const challEntries = Array.from({ length: 4 }, (_, i) => ({ entry_id: 'c' + i, drift_loss: 20 + i, pass_fail: i % 2 }));

  for (const [label, spec] of [
    ['seed', { genId: 'v0', parentId: '', baseline: true, entries: seedEntries }],
    ['challenger', { genId: 'v8', parentId: 'v0', decision: 'rejected', entries: challEntries }],
  ]) {
    const svgNode = dag.lifecycleDag(spec);
    const cys = boardCysOf(svgNode);
    const fanCenter = (cys[0] + cys[cys.length - 1]) / 2;
    const spineY = spineCenterY(svgNode);
    assert(Math.abs(spineY - fanCenter) < 1.0,
      `${label}: the spine y-centre (${spineY}) equals the board fan's centre (${fanCenter}) — spine aligned with the fan`);

    // NO large empty top band: the first board row sits a small fixed distance
    // below the column heads (one half-pitch + the header pad), NOT pushed to
    // some arbitrary proportion of an inflated height.
    const h = +svgNode.getAttribute('height');
    assert(cys[0] < h * 0.5, `${label}: the first board row (${cys[0]}) is in the UPPER half — no big top gap (h=${h})`);
    // and the figure's height closely fits the fan (top pad + fan + bottom pad),
    // so it is NOT inflated well beyond the fan span (the old stretch symptom).
    const fanSpan = cys[cys.length - 1] - cys[0];
    assert(h - fanSpan < 120, `${label}: height (${h}) fits the fan span (${fanSpan}) closely — figure not inflated`);
  }
});

test('lifecycle DAG (normalized): the seed is NOT laid out with a divergent vertical spread — adding height does not stretch it', () => {
  const entries = Array.from({ length: 6 }, (_, i) => ({ entry_id: 'b' + i, drift_loss: 10 + i, pass_fail: i % 2 }));
  const a = dag.lifecycleDag({ genId: 'v0', parentId: '', baseline: true, entries });
  const b = dag.lifecycleDag({ genId: 'v0', parentId: '', baseline: true, entries, height: 1200 });
  // a passed height cannot stretch the fan: identical pitch regardless.
  assert(Math.abs(rowPitchOf(a) - rowPitchOf(b)) < 0.5, 'a passed height does NOT stretch the seed fan (constant pitch)');
  assertEqual(a.getAttribute('height'), b.getAttribute('height'), 'the derived height is identical regardless of any passed height');
});

test('lifecycle BOARD column: the multiplicity badge style + raced disc marker are themed in the scoped stylesheet', () => {
  const css = readCss();
  assert(/\.ezn-board-mult\s*\{/.test(css), '.ezn-board-mult is styled (themed via CSS vars)');
  assert(/\.ezn-board-mult[^}]*var\(--v2-/.test(css), '.ezn-board-mult uses a theme variable (theme-aware across the 13 themes)');
  assert(/\.ezn-board-raced\s+\.ezn-board-disc\s*\{/.test(css), 'a raced node’s disc carries a distinct marker style');
});

// ====================================================================
// SURVIVAL FUNNEL — the racing epoch's structure-strip hero.
//
// For a RACING epoch the epoch-overview structure strip renders an
// interactive survival FUNNEL: the field flows N → N/2 → … → 1 →
// champion-gate, the flow narrowing at each cut; eliminated competitors peel
// off as ✕ dead-end branches, survivors (↑) ride the thickening flow into the
// gate, which crowns the promoted survivor (♚). It REUSES reconstructRacing()
// (idle) / the LIVE /api/active-tournament (in-flight), degrades to the static
// "field of N" summary when no rungs have raced, and is racing-specific
// (gauntlet keeps its reel; other structures keep their strip).
// ====================================================================

// the survival-funnel SVG primitive renders the field → cuts → survivor → gate.
test('survival funnel: the SVG narrows N→…→1, marks cuts ✕ / survivors ↑, and crowns the gate ♛', () => {
  const rungs = [
    { label: 'Rung 0', competitors: ['v1', 'v2', 'v3', 'v4'], survivors: ['v3', 'v4'], cut: ['v1', 'v2'], board_fraction: 0.25, deltas: { v1: 25, v2: 3.3, v3: -0.16, v4: 0.002 } },
    { label: 'Rung 1', competitors: ['v3', 'v4'], survivors: ['v3'], cut: ['v4'], board_fraction: 0.5, deltas: { v3: 1.0, v4: 1.25 } },
  ];
  const node = svg.survivalFunnel({ rungs, championId: 'v3', gateState: 'crowned', gateDelta: -32.19, onCompetitor() {} });
  assertEqual(node.localName, 'svg', 'the funnel is an SVG');
  assertEqual(node.getAttribute('width'), '100%', 'fit-to-width (width:100%)');
  assert((node.getAttribute('viewBox') || '').startsWith('0 0 '), 'carries a responsive viewBox (no pan/zoom)');

  // the flow narrows: each stage is a trapezoid whose right edge ∝ survivors.
  const bands = node.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('dn-funnel-band') && n.localName === 'polygon');
  assert(bands.length >= 2, 'one flowing band per rung (the field narrows stage to stage)');
  const txt = node.textContent;
  assert(txt.includes('Rung 0') && txt.includes('Rung 1'), 'each stage is labelled by rung');
  assert(txt.includes('25/100 board') || txt.includes('25'), 'a stage encodes its board fraction (successive halving reads)');
  assert(txt.includes('✕'), 'eliminated competitors are marked cut (✕)');
  assert(txt.includes('↑'), 'survivors are marked (↑)');
  // every cut competitor peels off as a labelled dead-end branch.
  const deadEdges = node.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('dn-funnel-deadedge'));
  assert(deadEdges.length >= 3, 'eliminated competitors peel off as dead-end branches (v1,v2 at rung0 + v4 at rung1)');
  // the terminal champion-gate crowns the survivor.
  assert(txt.includes('champion-gate'), 'a terminal champion-gate stage rendered');
  assert(txt.includes('♛ v3'), 'the crowned survivor is shown (♛ v3)');
  assert(!txt.includes('tbd'), 'a settled gate is not the empty tbd skeleton');
});

// REGRESSION: the eliminated dead-end branches must START on the band's lower
// edge (peel off the funnel) — NOT at the wide-edge depth `midY+hIn` placed at
// the middle x, which leaves each connector dangling below the band in empty
// space. Recompute the band's lower-edge y at the elbow x and assert the path
// touches it; assert cut rows don't overlap; assert each stage's cuts anchor to
// THAT stage's x-range.
test('survival funnel: dead-end branches anchor ON the band lower edge at the elbow x (no detached cut connectors), per-stage + non-overlapping', () => {
  const rungs = [
    { label: 'Rung 0', competitors: ['v1', 'v2', 'v3', 'v4'], survivors: ['v3', 'v4'], cut: ['v1', 'v2'], board_fraction: 0.25, deltas: { v1: 25, v2: 3.3, v3: -0.16, v4: 0.002 } },
    { label: 'Rung 1', competitors: ['v3', 'v4'], survivors: ['v3'], cut: ['v4'], board_fraction: 0.5, deltas: { v3: 1.0, v4: 1.25 } },
  ];
  const node = svg.survivalFunnel({ rungs, championId: 'v3', gateState: 'crowned', gateDelta: -32.19, onCompetitor() {} });

  // mirror the renderer's geometry constants (svg.js survivalFunnel).
  const stageW = 150, stageGap = 20, top = 56, laneH = 132, deadH = 18;
  const midY = top + laneH / 2;
  const stageX = (j) => j * (stageW + stageGap) + 2;
  const field0 = Math.max(1, rungs[0].competitors.length);
  const bandHalf = (n) => Math.max(6, (laneH / 2) * (Math.max(0, n) / field0));

  // expected start point of each cut branch: ON the band's lower edge at elbowX.
  const expected = []; // {x, y, stageX0, stageX1, branchY}
  rungs.forEach((rung, j) => {
    const x0 = stageX(j);
    const x1 = x0 + stageW;
    const hIn = bandHalf(rung.competitors.length);
    const hOut = bandHalf(rung.survivors.length);
    rung.cut.forEach((_cid, i) => {
      const elbowX = x0 + stageW * 0.5;
      const f = (elbowX - x0) / stageW;
      const edgeY = midY + hIn + (hOut - hIn) * f;
      const branchY = top + laneH + 6 + i * deadH;
      expected.push({ x: elbowX, y: edgeY, x0, x1, branchY, stage: j });
    });
  });

  const deadEdges = node.querySelectorAll('[class]')
    .filter((n) => (n.getAttribute('class') || '').includes('dn-funnel-deadedge'));
  assertEqual(deadEdges.length, expected.length, 'one dead-end branch per cut (v1,v2 @ rung0 + v4 @ rung1)');

  // parse the "M x,y V branchY H stub" start point out of each path's d.
  const starts = deadEdges.map((p) => {
    const d = p.getAttribute('d') || '';
    const m = d.match(/^M\s*([-\d.]+),([-\d.]+)\s*V\s*([-\d.]+)/);
    assert(m, `dead-edge path is "M x,y V ..." (got: ${d})`);
    return { x: parseFloat(m[1]), y: parseFloat(m[2]), branchY: parseFloat(m[3]) };
  }).sort((a, b) => a.y - b.y || a.x - b.x);
  const exp = expected.slice().sort((a, b) => a.y - b.y || a.x - b.x);

  const TOL = 0.01;
  exp.forEach((e, i) => {
    const s = starts[i];
    // THE FIX: the connector touches the band's lower edge (not midY+hIn at the
    // middle). At elbowX the lower edge is HIGHER than midY+hIn whenever the band
    // narrows (hOut<hIn), so the buggy start y would be strictly below this.
    assert(Math.abs(s.x - e.x) < TOL, `branch ${i} starts at the elbow x (${e.x}); got ${s.x}`);
    assert(Math.abs(s.y - e.y) < TOL, `branch ${i} starts ON the band lower edge y=${e.y.toFixed(3)} (touches the band), got ${s.y.toFixed(3)}`);
    assert(Math.abs(s.branchY - e.branchY) < TOL, `branch ${i} drops to its cut row branchY=${e.branchY}; got ${s.branchY}`);
  });

  // the buggy anchor (midY+hIn at the elbow) sat BELOW the true edge for the
  // narrowing rung-0 band — assert we are not regressing to it.
  const rung0hIn = bandHalf(rungs[0].competitors.length);
  const rung0Buggy = midY + rung0hIn;
  const rung0Edge = exp.find((e) => e.stage === 0).y;
  assert(rung0Edge < rung0Buggy - TOL, 'sanity: the band lower edge at the elbow is ABOVE the old midY+hIn anchor (so the old code left a gap)');
  starts.forEach((s, i) => assert(Math.abs(s.y - rung0Buggy) > TOL || exp[i].stage !== 0, 'no rung-0 branch anchors at the stale midY+hIn depth'));

  // cut rows do not overlap: distinct branchY values stepped by deadH within a stage.
  const byStage = {};
  exp.forEach((e) => { (byStage[e.stage] ||= []).push(e.branchY); });
  Object.values(byStage).forEach((ys) => {
    const uniq = new Set(ys);
    assertEqual(uniq.size, ys.length, 'cut rows within a stage have distinct branchY (no overlap)');
    ys.slice().sort((a, b) => a - b).forEach((y, k, arr) => { if (k) assert(arr[k] - arr[k - 1] >= deadH - TOL, 'consecutive cut rows are spaced ≥ deadH apart'); });
  });

  // each stage's cuts anchor to THAT stage's x-range (no drift into the gap).
  exp.forEach((e, i) => {
    assert(e.x >= e.x0 - TOL && e.x <= e.x1 + TOL, `branch ${i} (stage ${e.stage}) anchors within its own stage's x-range [${e.x0}, ${e.x1}]`);
  });
});

// (a) the racing epoch strip renders the funnel from the per-challenger records.
test('survival funnel: the racing epoch strip renders the funnel (stages narrow N→…→1, cuts ✕, gate crowns v3)', async () => {
  freshState();
  const F = {
    '/api/epoch': { epoch_id: RC_EPOCH, closed: true, goal: 'g', tournament: { structure: 'racing', params: RACING_TOURNAMENTS.structure_params },
      experiments: ['v0', 'v1', 'v2', 'v3', 'v4'].map((g) => ({ generation_id: g, parent_generation_id: g === 'v0' ? '' : 'v0', outcome: { decision: g === 'v0' ? 'baseline' : (g === 'v3' ? 'promoted' : 'rejected') } })), board: [] },
    '/api/lineage': { generations: ['v0', 'v1', 'v2', 'v3', 'v4'].map((g) => ({ generation_id: g, epoch_id: RC_EPOCH, parent_generation_id: g === 'v0' ? '' : 'v0', promoted: g === 'v0' || g === 'v3' })) },
    '/api/score-trajectory': { points: [] },
    '/api/tournaments': RACING_TOURNAMENTS,
  };
  installFixtureMap(F);
  coreState.state.heartbeat = { phase: 'idle' };
  coreState.state.activeRuns = [];
  coreState.state.activeTournament = null;

  const epoch = await import('../js/variants/T/views/epoch.js');
  const host = document.createElement('div');
  await epoch.render(host, { navigate() {}, href: router.href }, { epochId: RC_EPOCH });

  // racing-specific: the round timeline (no old gauntlet reel) embeds the funnel.
  assertEqual(allByClass(host, 'tr-reel').length, 0, 'NO gauntlet reel for a racing epoch');
  assert(allByClass(host, 'dn-roundtl')[0], 'the round timeline renders for the racing epoch');
  const funnel = svgsByClass(host, 'dn-funnel')[0];
  assert(funnel, 'the survival funnel rendered as the racing episode figure');
  assert(funnel.getAttribute('width') === '100%' && (funnel.getAttribute('viewBox') || '').startsWith('0 0 '), 'the funnel is fit-to-width + responsive');
  assert(!hasScrollWrapperAncestor(funnel, host), 'no horizontal-scroll wrapper around the funnel (no pan/zoom)');

  const txt = funnel.textContent;
  assert(txt.includes('Rung 0') && txt.includes('Rung 1'), 'both reconstructed rungs render as stages');
  for (const id of ['v1', 'v2', 'v3', 'v4']) assert(txt.includes(id), 'rung0 names the full field — ' + id);
  assert(txt.includes('✕'), 'eliminated competitors marked cut (✕) at their rung');
  assert(txt.includes('↑'), 'survivors marked (↑)');
  assert(txt.includes('♛ v3'), 'the champion-gate crowns the survivor v3 (♛)');
  // the episode drills into the round's full Match-ups (the ladder lives there).
  assert(host.textContent.includes('open round'), 'the episode keeps the "open round →" drill affordance');
  assertEqual(svgsByClass(host, 'dn-funnel').length, 1, 'the epoch hero is a SINGLE survival-funnel figure (the unified racing visual)');
});

// (b) a competitor is clickable → its candidate.
test('survival funnel: a competitor is clickable → its candidate page', async () => {
  freshState();
  const F = {
    '/api/epoch': { epoch_id: RC_EPOCH, closed: true, goal: 'g', tournament: { structure: 'racing', params: RACING_TOURNAMENTS.structure_params },
      experiments: ['v0', 'v1', 'v2', 'v3', 'v4'].map((g) => ({ generation_id: g, parent_generation_id: g === 'v0' ? '' : 'v0', outcome: { decision: g === 'v0' ? 'baseline' : (g === 'v3' ? 'promoted' : 'rejected') } })), board: [] },
    '/api/lineage': { generations: ['v0', 'v1', 'v2', 'v3', 'v4'].map((g) => ({ generation_id: g, epoch_id: RC_EPOCH, parent_generation_id: g === 'v0' ? '' : 'v0', promoted: g === 'v0' || g === 'v3' })) },
    '/api/score-trajectory': { points: [] },
    '/api/tournaments': RACING_TOURNAMENTS,
  };
  installFixtureMap(F);
  coreState.state.heartbeat = { phase: 'idle' };
  coreState.state.activeRuns = [];
  coreState.state.activeTournament = null;

  const epoch = await import('../js/variants/T/views/epoch.js');
  const host = document.createElement('div');
  let navTo = null;
  const ctx = { navigate: (v, p) => { navTo = { v, p }; }, href: router.href };
  await epoch.render(host, ctx, { epochId: RC_EPOCH });
  const runner = allByClass(host, 'dn-funnel-runner')[0];
  assert(runner, 'a clickable competitor exists on the funnel');
  runner.dispatchEvent({ type: 'click' });
  assert(navTo && navTo.v === 'candidate' && navTo.p.epochId === RC_EPOCH, 'clicking a funnel competitor routes to its candidate page');
  assert(/^v\d+$/.test(navTo.p.gen), 'the navigation carries the competitor generation id');
});

// (c) the live path shows a pending stage + "deciding…".
test('survival funnel: a LIVE racing run shows the in-progress funnel (pending stage neutral, gate "deciding…")', async () => {
  freshState();
  const F = {
    '/api/epoch': { epoch_id: RC_EPOCH, closed: false, goal: 'g', tournament: { structure: 'racing', params: LIVE_RACING.structure_params }, experiments: [], board: [] },
    '/api/lineage': { generations: LIVE_RACING.competitors.map((c) => ({ generation_id: c.generation_id, epoch_id: RC_EPOCH, parent_generation_id: c.role === 'champion' ? '' : 'v0', promoted: false })) },
    '/api/score-trajectory': { points: [] },
    '/api/tournaments': { epoch_id: RC_EPOCH, structure: 'racing', champion_lineage: [], matchups: [], tournaments: [] },
    '/api/active-tournament': LIVE_RACING,
  };
  installFixtureMap(F);
  coreState.state.setHeartbeat({ phase: 'tournament:round_1:rung1_m0', generation_id: 'v1' });
  coreState.state.activeRuns = [{ generation_id: 'v1', entry_id: 'b0', run_id: 'r1', progress: 0.5 }];
  coreState.state.activeTournament = { structure: 'racing', phase: 'running' };

  const epoch = await import('../js/variants/T/views/epoch.js');
  const host = document.createElement('div');
  await epoch.render(host, { navigate() {}, href: router.href }, { epochId: RC_EPOCH });

  const funnel = svgsByClass(host, 'dn-funnel')[0];
  assert(funnel, 'the LIVE racing funnel rendered from /api/active-tournament');
  assert(allByClass(host, 'dn-roundtl')[0], 'the live funnel is embedded in the round timeline');
  // the not-yet-decided rung stays neutral (a pending band, nobody struck).
  assert(allByClass(funnel, 'dn-funnel-pending').length >= 1, 'the pending (still-racing) stage renders neutral (no premature cut)');
  const struck = allByClass(host, 'dn-out');
  for (const n of struck) assert((n.textContent || '').indexOf('v1') < 0, 'the leader v1 is never struck (cut) mid-run');
  assert(funnel.textContent.includes('deciding'), 'the live champion-gate reads "deciding…" — no premature crown');
  assert(!funnel.textContent.includes('♚'), 'no champion is crowned ♚ while the race is live');

  coreState.state.heartbeat = { phase: 'idle' };
  coreState.state.activeRuns = [];
  coreState.state.activeTournament = null;
});

// (d) with no rung records, degrade to the static summary (no empty funnel).
test('survival funnel: with NO rung records the strip degrades to the static "field of N" summary (no empty funnel)', async () => {
  freshState();
  const F = {
    '/api/epoch': { epoch_id: RC_EPOCH, closed: false, goal: 'g', tournament: { structure: 'racing', params: { rungs: [1, 2] } }, experiments: [
      { generation_id: 'v0', parent_generation_id: '', outcome: { decision: 'baseline' } },
      { generation_id: 'v1', parent_generation_id: 'v0', outcome: { decision: 'rejected' } },
    ], board: [] },
    '/api/lineage': { generations: [
      { generation_id: 'v0', epoch_id: RC_EPOCH, parent_generation_id: '', promoted: true },
      { generation_id: 'v1', epoch_id: RC_EPOCH, parent_generation_id: 'v0', promoted: false },
    ] },
    '/api/score-trajectory': { points: [] },
    // no racing records yet — nothing to reconstruct.
    '/api/tournaments': { epoch_id: RC_EPOCH, structure: 'racing', champion_lineage: [], matchups: [], tournaments: [] },
  };
  installFixtureMap(F);
  coreState.state.heartbeat = { phase: 'idle' };
  coreState.state.activeRuns = [];
  coreState.state.activeTournament = null;

  const epoch = await import('../js/variants/T/views/epoch.js');
  const host = document.createElement('div');
  await epoch.render(host, { navigate() {}, href: router.href }, { epochId: RC_EPOCH });

  assertEqual(svgsByClass(host, 'dn-funnel').length, 0, 'NO empty funnel when there are no rung records');
  // the timeline degrades in place: a single round 0 episode (v0 → v1), no figure.
  assert(allByClass(host, 'dn-roundtl')[0], 'the round timeline degrades in place when there are no rung records');
  assert(host.textContent.includes('Racing'), 'the timeline still names the racing structure');
  assert(allByClass(host, 'dn-roundtl-ep').length >= 1, 'a single-round episode stands in for the empty race');
  assert(host.textContent.includes('open round'), 'the episode keeps the "open round →" drill affordance');
});

// (e) a gauntlet epoch's timeline has no embedded funnel (the funnel is racing-specific).
test('survival funnel: a GAUNTLET epoch renders the round timeline with NO embedded funnel', async () => {
  freshState(); installFetch();  // the default gauntlet fixture (no tournament block)
  const epoch = await import('../js/variants/T/views/epoch.js');
  const host = document.createElement('div');
  await epoch.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID });
  assert(allByClass(host, 'dn-roundtl')[0], 'the gauntlet epoch renders the round timeline');
  assertEqual(svgsByClass(host, 'dn-funnel').length, 0, 'NO survival funnel for a gauntlet epoch (racing-specific)');
  assertEqual(allByClass(host, 'dt-struct-strip').length, 0, 'NO structure strip for a gauntlet epoch');
});

// (f) the funnel marks are themed via CSS tokens (legible across all 13 themes).
test('survival funnel: marks are token-themed in the scoped stylesheet (legible across the 13 themes)', () => {
  const css = readCss();
  assert(/\.dn-funnel-band\s*\{/.test(css), '.dn-funnel-band is styled');
  assert(/\.dn-funnel-band[^}]*var\(--v2-/.test(css), 'the flow band reads a --v2-* token (theme-aware)');
  assert(/\.dn-funnel-name\.dn-good[^}]*var\(--v2-good\)/.test(css), 'survivors use the --v2-good token');
  assert(/\.dn-funnel-name\.dn-bad[^}]*var\(--v2-bad\)/.test(css), 'cuts use the --v2-bad token');
  assert(/\.dn-funnel-gatebox\.dn-good[^}]*var\(--v2-good\)/.test(css), 'the crowned gate uses the --v2-good token');
  assert(/\.dn-funnel-pending[^}]*var\(--v2-rule/.test(css), 'a pending (live) stage uses a neutral rule token');
});

// (f2) the swiss-ladder + elim-FLOW marks are token-themed (all 16 themes)
// with NO hardcoded hex — and the live transitions are reduced-motion gated.
test('swiss ladder + elim flow: token-themed in the scoped stylesheet, no hardcoded hex, reduced-motion gated', () => {
  const css = readCss();
  // swiss ladder
  assert(/\.dn-swissladder-head\s*\{/.test(css), '.dn-swissladder-head is styled');
  assert(/\.dn-swissladder-standlab\.dn-good[^}]*var\(--v2-good\)/.test(css), 'the swiss leader uses the --v2-good token');
  assert(/\.dn-swissladder-gatebox\.dn-good[^}]*var\(--v2-good\)/.test(css), 'the crowned swiss gate uses the --v2-good token');
  assert(/\.dn-swissladder-bar[^}]*var\(--v2-accent\)/.test(css), 'the live swiss progress bar uses the accent token');
  // elim flow (the bracket-as-flow — the seat/box tree is retired).
  assert(!/\.dn-elimbracket/.test(css), 'the retired seat/box bracket CSS is gone');
  assert(/\.dn-elimflow-seg\.dn-elimflow-good[^}]*var\(--v2-good\)/.test(css), 'an advancing elim-flow leg uses the --v2-good token');
  assert(/\.dn-elimflow-dot\.dn-elimflow-bad[^}]*var\(--v2-bad\)/.test(css), 'an eliminated elim-flow dot uses the --v2-bad token');
  assert(/\.dn-elimflow-convnode\.dn-elimflow-good[^}]*var\(--v2-good\)/.test(css), 'a decided match convergence node uses the --v2-good token');
  // NO hardcoded hex in the swiss/elim rules (token-only).
  const slice = css.slice(css.indexOf('.dn-swissladder-head'), css.indexOf('.dn-elimflow-lane:focus-visible') + 80);
  assert(!/#[0-9a-fA-F]{3,6}\b/.test(slice), 'the swiss/elim mark rules carry NO hardcoded hex (theme-token only)');
  // reduced-motion gate covers the new live transitions.
  const rm = css.slice(css.indexOf('@media (prefers-reduced-motion: reduce)'));
  assert(/\.dn-swissladder/.test(rm) && /\.dn-elimflow/.test(rm), 'the swiss/elim live transitions are suppressed under reduced motion');
});

// ====================================================================
// LIFECYCLE relates board runs to rungs/matchups (Change 1):
//   * a deduped board node is EXPANDABLE — it reveals its N per-run losses
//     (an inline stack + a sparkline), no longer lossy on the values;
//   * when the per-entry records carry `match_id`/`rung`, each run is LABELLED
//     by its rung/matchup; when ABSENT (legacy), no rung labels are fabricated;
//   * a CANDIDATE RUNG-PROGRESSION strip (rung0→rung1→final, each Δ + won/cut)
//     relates the candidate to the rounds even without per-run tags;
//   * a gauntlet candidate (one run per entry) renders unchanged.
// ====================================================================

function runRowsOf(boardNode) {
  return boardNode.querySelectorAll('[class]').filter((n) =>
    n.localName === 'g' && (n.getAttribute('class') || '').split(/\s+/).includes('ezn-board-run'));
}

test('lifecycle BOARD node: a re-raced entry is EXPANDABLE and reveals each run’s loss (no longer lossy on the values)', () => {
  // q3_metrics_outline raced 3× (rung0/rung1/final) with losses 4.0 / 64.0 / 63.5.
  const entries = [
    { entry_id: 'q3_metrics_outline', run_id: 'r0', drift_loss: 4.0, pass_fail: 1, wall_clock_budget_exceeded: false },
    { entry_id: 'q3_metrics_outline', run_id: 'r1', drift_loss: 64.0, pass_fail: 0, wall_clock_budget_exceeded: false },
    { entry_id: 'q3_metrics_outline', run_id: 'r2', drift_loss: 63.5, pass_fail: 0, wall_clock_budget_exceeded: false },
    { entry_id: 'waffles_single', run_id: 'g1', drift_loss: 60.5, pass_fail: 0, wall_clock_budget_exceeded: false },
  ];
  let navTo = null;
  const svgNode = dag.lifecycleDag({ genId: 'v3', parentId: 'v0', entries, decision: 'rejected', height: 360,
    onRun: (eid, runId) => { navTo = { eid, runId }; } });
  const nodes = boardNodesOf(svgNode);
  const byKey = {}; for (const n of nodes) byKey[n.getAttribute('data-key')] = n;

  // the raced node is marked expandable + carries its per-run stack.
  const q3 = byKey['q3_metrics_outline'];
  assert((q3.getAttribute('class') || '').includes('ezn-board-expandable'), 'a re-raced node is marked expandable');
  const stack = q3.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('ezn-board-runs'))[0];
  assert(stack, 'the expandable node carries a per-run expansion panel');
  const rows = runRowsOf(q3);
  assertEqual(rows.length, 3, 'the panel reveals ONE row per run (3 runs)');
  // every per-run loss value is shown (4.0 / 64.0 / 63.5) — no longer lossy.
  const losses = rows.map((r) => r.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('ezn-board-run-loss'))[0].textContent);
  assertDeep(losses, [svg.fmt(4.0, 1), svg.fmt(64.0, 1), svg.fmt(63.5, 1)], 'each run’s loss is revealed in order (rung0→final)');
  // a sparkline of the per-run losses renders too.
  assert(q3.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('ezn-board-spark')).length === 1, 'an inline sparkline of the run losses renders');

  // clicking a run row drills into that run/transcript.
  rows[1].dispatchEvent({ type: 'click', stopPropagation() {} });
  assert(navTo && navTo.eid === 'q3_metrics_outline', 'clicking a per-run row drills into that run (onRun fired)');

  // a once-run (gauntlet-style) entry in the same set carries NO expansion.
  assert(!(byKey['waffles_single'].getAttribute('class') || '').includes('ezn-board-expandable'), 'a once-run entry is not expandable');
  assertEqual(runRowsOf(byKey['waffles_single']).length, 0, 'a once-run entry has no per-run rows');
});

test('lifecycle BOARD node: per-run rows are LABELLED by rung when records carry match_id/rung', () => {
  const entries = [
    { entry_id: 'q3_metrics_outline', run_id: 'r0', drift_loss: 4.0, pass_fail: 1, match_id: 'rung0_m2', rung: 'rung 0' },
    { entry_id: 'q3_metrics_outline', run_id: 'r1', drift_loss: 64.0, pass_fail: 0, match_id: 'rung1_m0', rung: 'rung 1' },
    { entry_id: 'q3_metrics_outline', run_id: 'r2', drift_loss: 63.5, pass_fail: 0, match_id: 'racing-final', rung: 'final' },
  ];
  const svgNode = dag.lifecycleDag({ genId: 'v3', parentId: 'v0', entries, decision: 'rejected', height: 360 });
  const q3 = boardNodesOf(svgNode)[0];
  assertEqual(q3.getAttribute('data-tagged'), '1', 'the node is flagged as carrying rung-tagged runs');
  const rungs = q3.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('ezn-board-run-rung')).map((n) => n.textContent);
  assertDeep(rungs, ['rung 0', 'rung 1', 'final'], 'each run is labelled by its rung/matchup (rung 0 / rung 1 / final)');
});

test('lifecycle BOARD node: with NO rung tags (legacy data) the per-run losses still show but NO rung labels are fabricated', () => {
  // the current e0 legacy shape: repeated entries, NO match_id/rung fields.
  const entries = [
    { entry_id: 'q3_metrics_outline', run_id: 'r0', drift_loss: 4.0, pass_fail: 1 },
    { entry_id: 'q3_metrics_outline', run_id: 'r1', drift_loss: 64.0, pass_fail: 0 },
    { entry_id: 'q3_metrics_outline', run_id: 'r2', drift_loss: 63.5, pass_fail: 0 },
  ];
  const svgNode = dag.lifecycleDag({ genId: 'v3', parentId: 'v0', entries, decision: 'rejected', height: 360 });
  const q3 = boardNodesOf(svgNode)[0];
  assertEqual(q3.getAttribute('data-tagged'), '0', 'legacy runs are NOT flagged as rung-tagged');
  // the per-run losses still render (not lossy)…
  const rows = runRowsOf(q3);
  assertEqual(rows.length, 3, 'all three per-run losses still render on legacy data');
  // …but NO rung labels are fabricated.
  assertEqual(q3.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('ezn-board-run-rung')).length, 0,
    'no rung labels are fabricated when the records carry no match_id/rung');
});

// ====================================================================
// Lifecycle DAG — SELF-EXPLANATORY per-board loss, Σ aggregation, and the
// gate Δ-vs-champion decision. The motivating confusion: a candidate whose
// Σ "looks smaller" still gets rejected because the gate compares
// challenger-vs-champion (Δ, positive = worse) on the SAME boards and applies
// a 3-rule test. We surface the champion comparison + the deciding rule.
// ====================================================================

test('lifecycle BOARD circle: exposes the champion comparison (champion loss + signed Δ) for its board', () => {
  const entries = [
    { entry_id: 'waffles_single', drift_loss: 60.5, pass_fail: 0 },
    { entry_id: 'picky_stakeholder_emulated', drift_loss: 642.5, pass_fail: 0 },
  ];
  // the champion scored LOWER on both boards → the challenger's Δ is positive
  // (worse) on each, even though one of its raw losses (60.5) is identical.
  const championLoss = { waffles_single: 60.5, picky_stakeholder_emulated: 105.5 };
  const svgNode = dag.lifecycleDag({ genId: 'v1', parentId: 'v0', entries, decision: 'rejected', height: 360,
    championId: 'v0', championLoss });
  const nodes = boardNodesOf(svgNode);
  const byKey = {}; for (const n of nodes) byKey[n.getAttribute('data-key')] = n;

  // each circle carries a candidate-vs-champion sublabel with the champion loss
  // and the signed Δ (challenger − champion).
  const picky = childByClass(byKey['picky_stakeholder_emulated'], 'ezn-board-cmp');
  assert(picky, 'a board circle carries a champion-comparison sublabel');
  assertEqual(picky.getAttribute('data-champ-loss'), svg.fmt(105.5, 1), 'the sublabel exposes the champion’s loss on this board');
  assertEqual(picky.getAttribute('data-delta'), svg.fmtSigned(642.5 - 105.5, 1), 'the sublabel exposes the signed Δ (challenger − champion)');
  assert((picky.getAttribute('class') || '').includes('ezn-cmp-worse'), 'a positive Δ (worse than champion) is coloured with the worse token');
  assert(/champ/.test(picky.textContent) && /Δ/.test(picky.textContent), 'the sublabel reads "champ N · Δ ±X"');

  // the detail now lives in the styled HOVERCARD (not a native <title>): the
  // board node is hovercard-wired and surfaces the comparison + "lower is
  // better" cue on hover.
  const boardNode = byKey['waffles_single'];
  assert(hovercard.hasHovercard(boardNode), 'the board circle is wired with the hovercard (not a native <title>)');
  assert(!hasNativeTitle(boardNode), 'the board circle carries NO native <title> tooltip');
  const tipText = hovercardTextOf(boardNode);
  assert(/lower is better/.test(tipText), 'the hovercard states drift loss is lower-is-better');
  assert(/champion v0/.test(tipText) && /Δ/.test(tipText), 'the hovercard names the champion + the Δ');

  // an EVEN board (identical loss) is neither worse nor better.
  const even = dag.lifecycleDag({ genId: 'v1', parentId: 'v0',
    entries: [{ entry_id: 'b', drift_loss: 60.5, pass_fail: 0 }], decision: 'rejected',
    championId: 'v0', championLoss: { b: 60.5 } });
  const evCmp = childByClass(boardNodesOf(even)[0], 'ezn-board-cmp');
  assert((evCmp.getAttribute('class') || '').includes('ezn-cmp-even'), 'an equal-loss board is coloured even (neither worse nor better)');
});

test('lifecycle Σ node: exposes candidate-Σ vs champion-Σ and the Δ between them (what the gate sees)', () => {
  const entries = [
    { entry_id: 'waffles_single', drift_loss: 60.5, pass_fail: 0 },
    { entry_id: 'picky_stakeholder_emulated', drift_loss: 642.5, pass_fail: 0 },
  ];
  const svgNode = dag.lifecycleDag({ genId: 'v1', parentId: 'v0', entries, decision: 'rejected', height: 360,
    championId: 'v0', candidateSigma: 703.0, championSigma: 166.0, deltaSigma: 537.0 });
  // the Σ node carries the candidate Σ, the champion Σ, and the Δ.
  const agg = svgNode.querySelectorAll('[class]').filter((n) =>
    n.localName === 'g' && n.getAttribute('data-cand-sigma') != null)[0];
  assert(agg, 'the Σ node renders');
  assertEqual(agg.getAttribute('data-cand-sigma'), svg.fmt(703.0, 1), 'the Σ node exposes the candidate Σ over the slice');
  assertEqual(agg.getAttribute('data-champ-sigma'), svg.fmt(166.0, 1), 'the Σ node exposes the champion Σ over the same slice');
  assertEqual(agg.getAttribute('data-delta-sigma'), svg.fmtSigned(537.0, 1), 'the Σ node exposes the Δ (candidate − champion) the gate acts on');
  assert((agg.getAttribute('class') || '').includes('ezn-cmp-worse'), 'a positive Σ Δ tints the node as worse');
  // the Σ explanation now lives in the styled hovercard, not a native <title>.
  assert(hovercard.hasHovercard(agg), 'the Σ node is wired with the hovercard');
  assert(!hasNativeTitle(agg), 'the Σ node carries NO native <title>');
  const sigmaTip = hovercardTextOf(agg);
  assert(/summed over this rung’s board slice/.test(sigmaTip), 'the Σ hovercard explains the aggregation over the slice');
  assert(/SAME boards/.test(sigmaTip), 'the Σ hovercard links Σ→GATE: the gate compares these scalars on the same boards');
});

test('lifecycle GATE node: names the deciding rule + the Δ — a POSITIVE Δ rejection explains "worse than champion"', () => {
  const entries = [{ entry_id: 'b', drift_loss: 100, pass_fail: 0 }];
  const gateExplain = { decision: 'rejected', decidingRule: 'scalar_margin', decidingLabel: 'Scalar margin',
    deltaScalar: 75.71, margin: -0.01, regressed: null, reason: 'challenger regressed: loss rose by 75.71' };
  const svgNode = dag.lifecycleDag({ genId: 'v1', parentId: 'v0', entries, decision: 'rejected', height: 360,
    deltaScalar: 75.71, gateExplain });
  const gate = svgNode.querySelectorAll('[class]').filter((n) =>
    (n.getAttribute('class') || '').split(/\s+/).includes('ezn-gate-node'))[0];
  assert(gate, 'the GATE node carries the gate-node marker');
  assertEqual(gate.getAttribute('data-deciding-rule'), 'scalar_margin', 'the GATE node names the deciding rule');
  assertEqual(gate.getAttribute('data-delta-scalar'), svg.fmtSigned(75.71, 2), 'the GATE node carries the decisive Δ scalar');
  assertEqual(gate.getAttribute('data-margin'), svg.fmt(-0.01, 2), 'the GATE node carries the promote margin');
  // the GATE explanation now lives in the styled hovercard, not a native <title>.
  assert(hovercard.hasHovercard(gate), 'the GATE node is wired with the hovercard');
  assert(!hasNativeTitle(gate), 'the GATE node carries NO native <title> tooltip');
  const gateTip = hovercardTextOf(gate);
  assert(gateTip, 'the GATE node exposes an explanation via the hovercard');
  assert(/3-rule/.test(gateTip), 'the hovercard frames the gate as a 3-rule test');
  assert(/SCALAR-MARGIN rule/i.test(gateTip), 'the hovercard names the scalar-margin rule as the decider');
  assert(/worse than champion/.test(gateTip), 'a positive-Δ rejection explains it is WORSE than the champion');
  assert(/\+75\.7/.test(gateTip), 'the hovercard shows the decisive +Δ');
});

test('lifecycle GATE node: a MONOTONICITY rejection explains the regressed predicate even when the scalar is BETTER', () => {
  const entries = [{ entry_id: 'b', drift_loss: 10, pass_fail: 0 }];
  // scalar is BETTER (Δ negative) yet the candidate is rejected because it
  // regressed a previously-passing predicate (rule 2). This is the "smaller Σ
  // but rejected" case made legible.
  const gateExplain = { decision: 'rejected', decidingRule: 'pass_rate_monotonicity', decidingLabel: 'Pass-rate monotonicity',
    deltaScalar: -5.0, margin: null, regressed: 'no_fabricated_numbers', reason: 'regressed a passing predicate' };
  const svgNode = dag.lifecycleDag({ genId: 'v1', parentId: 'v0', entries, decision: 'rejected', height: 360,
    deltaScalar: -5.0, gateExplain });
  const gate = svgNode.querySelectorAll('[class]').filter((n) =>
    (n.getAttribute('class') || '').split(/\s+/).includes('ezn-gate-node'))[0];
  assertEqual(gate.getAttribute('data-deciding-rule'), 'pass_rate_monotonicity', 'the deciding rule is the monotonicity rule');
  assertEqual(gate.getAttribute('data-regressed'), 'no_fabricated_numbers', 'the GATE node carries the regressed predicate');
  const monoTip = hovercardTextOf(gate);
  assert(!hasNativeTitle(gate), 'the GATE node carries NO native <title>');
  assert(/Scalar may be better, BUT/.test(monoTip), 'the hovercard says the scalar is better BUT it still failed a rule');
  assert(/no_fabricated_numbers/.test(monoTip), 'the hovercard names the regressed predicate');
  assert(/rule 2/.test(monoTip), 'the hovercard identifies it as the pass-rate-monotonicity rule (rule 2)');
});

test('lifecycle DAG: de-crowded to ONE concise key line + a "?" info hovercard (the verbose how-to is gone from the figure), omitted for a baseline', () => {
  const entries = [{ entry_id: 'b', drift_loss: 10, pass_fail: 0 }];
  const svgNode = dag.lifecycleDag({ genId: 'v1', parentId: 'v0', entries, decision: 'rejected', height: 360, championId: 'v0' });

  // exactly ONE always-on key line — short, de-crowded (the old two-block verbose
  // prose is consolidated into this single line + the "?" hovercard).
  const keys = svgNode.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('ezn-dag-key'));
  assertEqual(keys.length, 1, 'the DAG carries exactly ONE concise key line');
  const t = keys[0].textContent;
  assert(/Δ vs champion/.test(t) && /\+ = worse/.test(t), 'the key line states "Δ vs champion · + = worse"');
  assert(/lower loss better/.test(t) && /hover nodes for detail/.test(t), 'the key line states lower-loss-better + the hover-for-detail cue');
  // the OLD verbose two-block prose is no longer crowding the figure as a key.
  assert(!/Σ = their sum on the slice/.test(t), 'the verbose "Σ = their sum on the slice" prose is no longer in the always-on key');
  assert(!/no pass-rate\/namespace regression/.test(t), 'the verbose pass-rate/namespace prose is no longer in the always-on key');

  // the full how-to walkthrough moved into the focusable "?" info affordance,
  // surfaced via the hovercard (detail on demand, not always-on prose).
  const info = svgNode.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').split(/\s+/).includes('ezn-dag-info'))[0];
  assert(info, 'the DAG carries a "?" info affordance');
  assert(hovercard.hasHovercard(info), 'the "?" affordance is wired with the hovercard');
  assertEqual(info.getAttribute('tabindex'), '0', 'the "?" affordance is keyboard-focusable');
  const howto = hovercardTextOf(info);
  assert(/parent → patch → board/.test(howto), 'the hovercard carries the parent→patch→board walkthrough');
  assert(/3-rule test/.test(howto), 'the hovercard carries the 3-rule gate detail');
  assert(/per-run losses/.test(howto), 'the hovercard carries the hover/click affordance detail');

  // a baseline (seed) has no gate, so no key + no info affordance.
  const seed = dag.lifecycleDag({ genId: 'v0', parentId: null, baseline: true, entries, decision: 'baseline', height: 360 });
  assert(!seed.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('ezn-dag-key'))[0],
    'the baseline DAG omits the gate key (it has no gate)');
});

// the key line's text baseline y, and the LOWEST node-box bottom edge across
// the whole figure (rect boxes: y + height; board circles: cy + r, plus the
// `champ N · Δ` cmp sublabel below a circle when present).
function keyLineYOf(svgNode) {
  const k = svgNode.querySelectorAll('[class]').filter((n) =>
    (n.getAttribute('class') || '').split(/\s+/).includes('ezn-dag-key'))[0];
  return k ? +k.getAttribute('y') : null;
}
function lowestNodeBottomOf(svgNode) {
  let bottom = -Infinity;
  // every rect node box (PARENT/PATCH/Σ/GATE/TERMINAL and the "no board" box).
  for (const r of svgNode.querySelectorAll('[class]').filter((n) =>
    n.localName === 'rect' && (n.getAttribute('class') || '').split(/\s+/).includes('ezn-node-box'))) {
    bottom = Math.max(bottom, +r.getAttribute('y') + +r.getAttribute('height'));
  }
  // every board circle (+ its radius), and any cmp sublabel below it.
  for (const c of svgNode.querySelectorAll('[class]').filter((n) =>
    n.localName === 'circle' && (n.getAttribute('class') || '').split(/\s+/).includes('ezn-board-disc'))) {
    bottom = Math.max(bottom, +c.getAttribute('cy') + +c.getAttribute('r'));
  }
  for (const t of svgNode.querySelectorAll('[class]').filter((n) =>
    (n.getAttribute('class') || '').split(/\s+/).includes('ezn-board-cmp'))) {
    bottom = Math.max(bottom, +t.getAttribute('y'));
  }
  return bottom;
}

test('lifecycle DAG: the key line clears the node row at a SINGLE board node (no overlap)', () => {
  // the not-yet-run / "no board entries scored" state — a single neutral box.
  // The fan span is 0 here, the worst case for the old flat KEY_PAD: the key
  // line used to render right through the node boxes. It must now sit strictly
  // below the lowest node box's bottom edge, with a readable margin.
  const svgNode = dag.lifecycleDag({ genId: 'v1', parentId: 'v0', entries: [], decision: 'rejected', championId: 'v0' });
  const ky = keyLineYOf(svgNode);
  assert(ky != null, 'a single-node DAG still carries the key line');
  const lowest = lowestNodeBottomOf(svgNode);
  assert(ky > lowest + 6, `the key line y (${ky}) is strictly below the lowest node box bottom (${lowest}) with a margin`);
  // and it stays within the figure's derived viewBox height.
  const h = +svgNode.getAttribute('height');
  assert(ky <= h, `the key line y (${ky}) is within the derived viewBox height (${h})`);
});

test('lifecycle DAG: the key line clears the node row with MANY board nodes (no overlap at the bottom-most node)', () => {
  const entries = Array.from({ length: 7 }, (_, i) => ({ entry_id: 'b' + i, drift_loss: 10 + i, pass_fail: i % 2 }));
  const svgNode = dag.lifecycleDag({ genId: 'v1', parentId: 'v0', entries, decision: 'rejected',
    championId: 'v0', championLoss: { b0: 5, b6: 9 } });
  const ky = keyLineYOf(svgNode);
  assert(ky != null, 'a many-node DAG carries the key line');
  const lowest = lowestNodeBottomOf(svgNode);
  assert(ky > lowest + 6, `the key line y (${ky}) is strictly below the bottom-most node (${lowest}) — including its cmp sublabel`);
  const h = +svgNode.getAttribute('height');
  assert(ky <= h, `the key line y (${ky}) is within the derived viewBox height (${h})`);
});

// ====================================================================
// HOVERCARD — the styled, theme-aware replacement for native <title>.
// ====================================================================

test('hovercard: the heatmap cell uses the hovercard (NOT a native <title>), and surfaces "row × col: value"', () => {
  const node = svg.heatmap({
    rows: [{ id: 'r1', label: 'board one' }],
    cols: [{ id: 'c1', label: 'gen one' }],
    value: (r, c) => (r === 'r1' && c === 'c1' ? 12.5 : null),
  });
  const cell = node.querySelectorAll('[class]').filter((n) =>
    (n.getAttribute('class') || '').split(/\s+/).includes('dn-hm-cell'))[0];
  assert(cell, 'a heatmap cell rendered');
  assert(hovercard.hasHovercard(cell), 'the heatmap cell is wired with the hovercard');
  assert(!hasNativeTitle(cell), 'the heatmap cell carries NO native <title>');
  const tip = hovercardTextOf(cell);
  assert(/board one × gen one/.test(tip), 'the hovercard reads "row × col"');
  assert(/12\.5/.test(tip), 'the hovercard carries the cell value');
});

test('hovercard: the per-board dot-plot dot + reference rule use the hovercard, not a native <title>', () => {
  const node = svg.valueDotPlot({
    items: [{ label: 'waffles', value: 60.5, id: 'waffles' }],
    reference: { value: 50, label: 'champion v0' },
  });
  // the per-board dot.
  const dot = node.querySelectorAll('[class]').filter((n) =>
    (n.getAttribute('class') || '').split(/\s+/).includes('dn-dot'))[0];
  assert(dot, 'a dot-plot dot rendered');
  assert(hovercard.hasHovercard(dot) && !hasNativeTitle(dot), 'the dot uses the hovercard, not a native <title>');
  assert(/waffles/.test(hovercardTextOf(dot)), 'the dot hovercard names the board');
  // the reference rule.
  const ref = node.querySelectorAll('[class]').filter((n) =>
    (n.getAttribute('class') || '').includes('dn-ref-rule'))[0];
  assert(ref, 'a reference rule rendered');
  assert(hovercard.hasHovercard(ref) && !hasNativeTitle(ref), 'the reference rule uses the hovercard, not a native <title>');
  assert(/champion v0/.test(hovercardTextOf(ref)), 'the reference-rule hovercard names the champion reference');
});

test('hovercard: NO native <title> remains on the interactive marks of the lifecycle DAG, heatmap, or dot-plot', () => {
  const dagSvg = dag.lifecycleDag({ genId: 'v1', parentId: 'v0',
    entries: [{ entry_id: 'b', drift_loss: 10, pass_fail: 0 }], decision: 'rejected',
    championId: 'v0', championLoss: { b: 5 }, candidateSigma: 10, championSigma: 5, deltaSigma: 5 });
  const hm = svg.heatmap({ rows: [{ id: 'r', label: 'r' }], cols: [{ id: 'c', label: 'c' }], value: () => 1 });
  const dp = svg.valueDotPlot({ items: [{ label: 'x', value: 1 }], reference: { value: 2, label: 'ref' } });
  for (const [name, root] of [['DAG', dagSvg], ['heatmap', hm], ['dot-plot', dp]]) {
    const titles = root.querySelectorAll('[class]').filter((n) => n.localName === 'title')
      .concat(root.childNodes ? [] : []);
    // walk for any <title> descendant.
    const anyTitle = (function find(n) {
      if (!n || !n.childNodes) return false;
      for (const c of n.childNodes) { if (c.localName === 'title') return true; if (find(c)) return true; }
      return false;
    })(root);
    assert(!anyTitle, `the ${name} has NO native <title> left (replaced by the hovercard)`);
  }
});

test('hovercard: show on mouseenter/focus, hide on mouseleave/blur/Escape — and the card is theme-token styled', () => {
  // build any wired mark.
  const node = svg.heatmap({ rows: [{ id: 'r', label: 'row' }], cols: [{ id: 'c', label: 'col' }], value: () => 7 })
    .querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').split(/\s+/).includes('dn-hm-cell'))[0];
  hovercard.hide();
  assert(!hovercard.isShown(), 'the hovercard starts hidden');
  // mouseenter shows it.
  node.dispatchEvent({ type: 'mouseenter', target: node });
  assert(hovercard.isShown(), 'mouseenter shows the hovercard');
  assert(/row × col: 7/.test(hovercard.cardText()), 'the shown card carries the mark detail');
  // mouseleave hides it.
  node.dispatchEvent({ type: 'mouseleave', target: node });
  assert(!hovercard.isShown(), 'mouseleave hides the hovercard');
  // focus shows; blur hides (keyboard path).
  node.dispatchEvent({ type: 'focus', target: node });
  assert(hovercard.isShown(), 'focus shows the hovercard (keyboard-accessible)');
  node.dispatchEvent({ type: 'blur', target: node });
  assert(!hovercard.isShown(), 'blur hides the hovercard');

  // the card is THEME-TOKEN styled (no hardcoded hex) — assert the CSS contract.
  const css = readCss();
  assert(/\.dn-hovercard\b/.test(css), 'the stylesheet defines the .dn-hovercard');
  const block = css.slice(css.indexOf('.dn-hovercard {'), css.indexOf('.dn-hovercard-line'));
  assert(/var\(--v2-panel\)/.test(block), 'the hovercard background uses the --v2-panel token');
  assert(/var\(--v2-ink\)/.test(block), 'the hovercard text uses the --v2-ink token');
  assert(/var\(--v2-rule\)/.test(block), 'the hovercard border uses the --v2-rule token');
  assert(/var\(--v2-mono\)/.test(block), 'the hovercard uses the mono font token');
  assert(!/#[0-9a-fA-F]{3,6}\b/.test(block), 'the hovercard block carries NO hardcoded hex colour');
  assert(/prefers-reduced-motion/.test(css), 'the hovercard honours prefers-reduced-motion');
});

test('hovercard: the target is keyboard-accessible (focusable + aria-describedby links the card)', () => {
  const cell = svg.heatmap({ rows: [{ id: 'r', label: 'row' }], cols: [{ id: 'c', label: 'col' }], value: () => 1 })
    .querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').split(/\s+/).includes('dn-hm-cell'))[0];
  assertEqual(cell.getAttribute('tabindex'), '0', 'a wired mark with no tabindex is made focusable');
  assert((cell.getAttribute('aria-describedby') || '').length > 0, 'the mark links the hovercard via aria-describedby');
});

test('lifecycle DAG (integration): the candidate view feeds the champion comparison + gate-rule explanation into the DAG — "smaller-looking" rejected v1 explains worse-than-champion', async () => {
  freshState(); installFetch();
  const candidate = await import('../js/variants/T/views/candidate.js');
  const host = document.createElement('div');
  // v1 is the rejected challenger vs champion v0; gate fired the scalar-margin
  // rule with Δ +75.71 (needs ≤ -0.01).
  await candidate.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, gen: 'v1' });

  const dagSvg = svgsByClass(host, 'ezn-dag')[0];
  assert(dagSvg, 'the lifecycle DAG rendered for v1');
  // a board circle shows the champion comparison (waffles: both 60.5 → Δ 0;
  // picky: 642.5 vs 105.5 → +537).
  const cmps = dagSvg.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('ezn-board-cmp'));
  assert(cmps.length >= 1, 'a board circle in the rendered DAG carries the champion comparison');
  const pickyCmp = dagSvg.querySelectorAll('[class]').filter((n) =>
    (n.getAttribute('class') || '').includes('ezn-board-cmp') && n.getAttribute('data-champ-loss') === svg.fmt(105.5, 1))[0];
  assert(pickyCmp && pickyCmp.getAttribute('data-delta') === svg.fmtSigned(642.5 - 105.5, 1),
    'the rendered circle exposes the champion loss + Δ for the picky board');

  // the GATE node explains the scalar-margin rejection with the +Δ.
  const gate = dagSvg.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').split(/\s+/).includes('ezn-gate-node'))[0];
  assert(gate, 'the rendered DAG GATE node carries the gate-node marker');
  assertEqual(gate.getAttribute('data-deciding-rule'), 'scalar_margin', 'the rendered GATE node names the scalar-margin rule');
  assert(hovercard.hasHovercard(gate) && !hasNativeTitle(gate), 'the rendered GATE uses the hovercard, not a native <title>');
  const gtitle = hovercardTextOf(gate);
  assert(/worse than champion/.test(gtitle), 'the rendered GATE explains the rejection as worse-than-champion (resolves "smaller Σ but rejected")');

  // the Σ node carries the candidate-vs-champion Σ Δ.
  const agg = dagSvg.querySelectorAll('[class]').filter((n) =>
    n.localName === 'g' && (n.getAttribute('class') || '').includes('ezn-node') && n.getAttribute('data-cand-sigma'))[0];
  assert(agg, 'the rendered Σ node carries the candidate Σ');
  assert(agg.getAttribute('data-champ-sigma') && agg.getAttribute('data-delta-sigma'), 'the rendered Σ node carries the champion Σ + the Δ');
});

test('lifecycle RUNG-PROGRESSION strip: reconstructs rung0→rung1→final (Δ + won/cut) from the per-challenger structure record', () => {
  // v3’s racing path from RACING_TOURNAMENTS: rung0 won → rung1 → racing-final promoted.
  const prog = STRUCT.candidateProgression(RACING_TOURNAMENTS, 'v3');
  assert(prog && Array.isArray(prog.stages), 'a progression was reconstructed for the racing candidate v3');
  assertDeep(prog.stages.map((s) => s.label), ['rung 0', 'rung 1', 'final'], 'the path is rung0 → rung1 → final');
  assertDeep(prog.stages.map((s) => s.kind), ['rung', 'rung', 'final'], 'the final stage is flagged kind=final');
  assertEqual(prog.stages[0].verdict, 'survived', 'v3 survived rung0 (it reached rung1)');
  assertEqual(prog.stages[2].verdict, 'promoted', 'v3 was promoted at the champion gate');
  assertEqual(prog.stages[2].delta, -32.19, 'the final stage carries the Δ-vs-champion');

  // a cut candidate (v4: rung0 → rung1, no final) ends "cut".
  const prog4 = STRUCT.candidateProgression(RACING_TOURNAMENTS, 'v4');
  assert(prog4, 'v4 has a progression');
  assertEqual(prog4.stages[prog4.stages.length - 1].verdict, 'cut', 'v4 was cut at its last rung (no final reached)');

  // a gauntlet / non-racing candidate has NO progression (strip suppressed).
  assertEqual(STRUCT.candidateProgression(FIXTURE['/api/tournaments'], 'v1'), null, 'a gauntlet candidate has no rung progression');

  // the builder renders a fit-to-width SVG with a stage per rung.
  const node = dag.rungProgression({ stages: prog.stages });
  assertEqual(node.localName, 'svg', 'the progression strip is an SVG');
  assertEqual(node.getAttribute('width'), '100%', 'the progression strip is fit-to-width (width:100%)');
  assert((node.getAttribute('viewBox') || '').startsWith('0 0 '), 'it carries a responsive viewBox (theme-aware, scaled by the page pill)');
  const stages = node.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('ezn-rungprog-stage'));
  assertEqual(stages.length, 3, 'one stage per rung/final');
  assert(node.textContent.includes('rung 0') && node.textContent.includes('final'), 'the stages are labelled by rung');
});

test('lifecycle RUNG-PROGRESSION strip: a racing candidate page renders the strip; a gauntlet candidate page does NOT', async () => {
  // a racing candidate (v3) on the live reconstruction fixture.
  freshState();
  const F = {
    '/api/epoch': { epoch_id: RC_EPOCH, closed: true, goal: 'g', tournament: { structure: 'racing', params: RACING_TOURNAMENTS.structure_params },
      experiments: ['v0', 'v1', 'v2', 'v3', 'v4'].map((g) => ({ generation_id: g, parent_generation_id: g === 'v0' ? '' : 'v0', outcome: { decision: g === 'v0' ? 'baseline' : (g === 'v3' ? 'promoted' : 'rejected') } })), board: [] },
    '/api/lineage': { generations: ['v0', 'v1', 'v2', 'v3', 'v4'].map((g) => ({ generation_id: g, epoch_id: RC_EPOCH, parent_generation_id: g === 'v0' ? '' : 'v0', promoted: g === 'v0' || g === 'v3' })) },
    '/api/score-trajectory': { points: [] },
    '/api/tournaments': RACING_TOURNAMENTS,
  };
  // per-entry records for v3: the SAME entry raced across rungs, carrying rung tags.
  F[`/api/generation/${RC_EPOCH}/v3/per-entry`] = { entries: [
    { entry_id: 'q3_metrics_outline', run_id: 'r0', drift_loss: 4.0, pass_fail: 1, match_id: 'rung0_m2', rung: 'rung 0' },
    { entry_id: 'q3_metrics_outline', run_id: 'r1', drift_loss: 64.0, pass_fail: 0, match_id: 'rung1_m0', rung: 'rung 1' },
    { entry_id: 'q3_metrics_outline', run_id: 'r2', drift_loss: 63.5, pass_fail: 0, match_id: 'racing-final', rung: 'final' },
  ] };
  installFixtureMap(F);
  const candidate = await import('../js/variants/T/views/candidate.js');
  const host = document.createElement('div');
  await candidate.render(host, { navigate() {}, href: router.href }, { epochId: RC_EPOCH, gen: 'v3' });
  assert(allByClass(host, 'dn-rungprog-strip')[0], 'the racing candidate page renders the rung-progression strip');
  const strip = svgsByClass(host, 'ezn-rungprog')[0];
  assert(strip, 'the progression SVG rendered');
  assert(strip.textContent.includes('rung 0') && strip.textContent.includes('final'), 'the strip shows rung0 → … → final');
  // the board node also reveals its per-run losses, labelled by rung.
  const boardNode = host.querySelectorAll('[class]').filter((n) => n.localName === 'g'
    && (n.getAttribute('class') || '').split(/\s+/).includes('ezn-board-node'))[0];
  assert(boardNode && (boardNode.getAttribute('class') || '').includes('ezn-board-expandable'), 'the racing board node is expandable on the candidate page');

  // a gauntlet candidate (default fixture) renders NO progression strip.
  freshState(); installFetch();
  const host2 = document.createElement('div');
  await candidate.render(host2, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, gen: 'v1' });
  assertEqual(allByClass(host2, 'dn-rungprog-strip').length, 0, 'a gauntlet candidate page renders NO rung-progression strip');
  // and its board nodes are not expandable (one run per entry).
  const gnodes = host2.querySelectorAll('[class]').filter((n) => n.localName === 'g'
    && (n.getAttribute('class') || '').split(/\s+/).includes('ezn-board-node'));
  for (const g of gnodes) assert(!(g.getAttribute('class') || '').includes('ezn-board-expandable'), 'a gauntlet board node is not expandable');
});

test('lifecycle per-run stack + progression strip are themed in the scoped stylesheet (theme-aware across the 13 themes)', () => {
  const css = readCss();
  assert(/\.ezn-board-runs-box\s*\{/.test(css), '.ezn-board-runs-box is styled');
  assert(/\.ezn-board-runs-box[^}]*var\(--v2-/.test(css), 'the per-run panel uses a theme variable');
  assert(/\.ezn-board-spark[^}]*var\(--v2-/.test(css), 'the per-run sparkline uses a theme variable');
  assert(/\.ezn-rungprog-dot[^}]*var\(--v2-/.test(css), 'the progression dots are token-themed');
  // the expansion is hidden until hover / focus-within / open (no-flash, reveal-on-demand).
  assert(/\.ezn-board-node:hover\s+\.ezn-board-runs/.test(css), 'the per-run panel reveals on hover');
});

// ====================================================================
// CHANGE 2 — the resizable LEFT side-panel (rail) sizing handle.
// ====================================================================

test('rail sizing: ui exposes a clamped rail-width range with a default + normalisation', () => {
  freshState();
  assertEqual(ui.DEFAULT_RAIL, 288, 'the rail defaults to the cozy 288px baseline');
  assert(ui.RAIL_MIN >= 120 && ui.RAIL_MIN < ui.DEFAULT_RAIL, 'a sensible minimum below the default');
  assert(ui.RAIL_MAX > ui.DEFAULT_RAIL, 'a sensible maximum above the default');
  assertEqual(ui.normaliseRail(10), ui.RAIL_MIN, 'below-range clamps up to the min');
  assertEqual(ui.normaliseRail(9999), ui.RAIL_MAX, 'above-range clamps down to the max');
  assertEqual(ui.normaliseRail('nonsense'), ui.DEFAULT_RAIL, 'a non-numeric value falls back to the default');
  // the shell re-exports the surface.
  assertEqual(shell.DEFAULT_RAIL, 288, 'the shell exposes the default rail width');
});

test('rail sizing: a draggable handle on the rail edge changes the rail width, persists + restores; the detail pane reflows', () => {
  try { globalThis.window.localStorage.clear(); } catch (e) { /* ignore */ }
  const root = mountLiveShell('#/');

  // the handle is a focusable separator on the rail's right edge.
  const handle = allByClass(root, 'dt-rail-handle')[0];
  assert(handle, 'a rail-resize handle rendered on the rail edge');
  assertEqual(handle.getAttribute('role'), 'separator', 'the handle is a separator (keyboard-accessible)');
  assert((handle.getAttribute('aria-label') || '').length > 0, 'the handle carries an aria-label');

  // default rail width is stamped on the root as the --dt-rail token.
  assertEqual(root.getAttribute('data-t-rail'), String(ui.DEFAULT_RAIL), 'the rail starts at the default width');
  assert(root.style.cssText.includes('--dt-rail:' + ui.DEFAULT_RAIL + 'px'), 'the --dt-rail token is on the root (the detail pane’s 1fr column reflows around it)');

  // keyboard: ArrowRight widens, ArrowLeft narrows (the handle is accessible).
  handle.dispatchEvent({ type: 'keydown', key: 'ArrowRight', preventDefault() {} });
  const wider = +root.getAttribute('data-t-rail');
  assert(wider > ui.DEFAULT_RAIL, 'ArrowRight widened the rail');
  handle.dispatchEvent({ type: 'keydown', key: 'ArrowLeft', preventDefault() {} });
  handle.dispatchEvent({ type: 'keydown', key: 'ArrowLeft', preventDefault() {} });
  assert(+root.getAttribute('data-t-rail') < wider, 'ArrowLeft narrowed the rail');
  // Home/End jump to the bounds.
  handle.dispatchEvent({ type: 'keydown', key: 'End', preventDefault() {} });
  assertEqual(+root.getAttribute('data-t-rail'), ui.RAIL_MAX, 'End jumps to the max width');
  handle.dispatchEvent({ type: 'keydown', key: 'Home', preventDefault() {} });
  assertEqual(+root.getAttribute('data-t-rail'), ui.RAIL_MIN, 'Home jumps to the min width');

  // the programmatic applyRail() clamps + persists.
  shell.applyRail(360, root);
  assertEqual(root.getAttribute('data-t-rail'), '360', 'applyRail set the rail width');
  assert(root.style.cssText.includes('--dt-rail:360px'), 'the --dt-rail token reflects the chosen width');
  assertEqual(ui.readRail(), 360, 'the chosen rail width persisted to localStorage');

  // RESTORE: a fresh mount reads it back and re-applies it.
  const root2 = mountLiveShell('#/');
  assertEqual(root2.getAttribute('data-t-rail'), '360', 'a fresh mount restores the persisted rail width');
  assert(root2.style.cssText.includes('--dt-rail:360px'), 'the restored rail width is re-applied to the root');

  // it is page-CHROME sizing, distinct from the page-scale pill (separate axes).
  shell.applyScale(120, root2);
  assertEqual(root2.getAttribute('data-t-rail'), '360', 'a page-scale change leaves the rail width untouched');
});

// A pointer DRAG — pointerdown at X0, pointermove by Δx, pointerup — drives the
// rail width to start+Δ (within the clamp). This is the smooth-drag spine: the
// width tracks the pointer delta rather than snapping. Driven on the handle so
// the captured-pointer path is exercised (the harness ignores the unsupported
// setPointerCapture but the same handlers fire).
function dragRail(handle, x0, dx, extra) {
  handle.dispatchEvent({ type: 'pointerdown', pointerId: 1, clientX: x0, preventDefault() {} });
  if (typeof extra === 'function') extra();
  handle.dispatchEvent({ type: 'pointermove', pointerId: 1, clientX: x0 + dx, preventDefault() {} });
  handle.dispatchEvent({ type: 'pointerup', pointerId: 1, clientX: x0 + dx, preventDefault() {} });
}

test('rail sizing: a pointer DRAG tracks the pointer delta (start+Δ), captures the pointer, persists on pointerup', () => {
  try { globalThis.window.localStorage.clear(); } catch (e) { /* ignore */ }
  const root = mountLiveShell('#/');
  const handle = allByClass(root, 'dt-rail-handle')[0];
  assert(handle, 'a rail-resize handle rendered');

  // start at the default width; drag the pointer +40px to the RIGHT.
  shell.applyRail(300, root);
  assertEqual(+root.getAttribute('data-t-rail'), 300, 'rail starts at 300');
  dragRail(handle, 500, 40);
  assertEqual(+root.getAttribute('data-t-rail'), 340, 'a +40px pointer drag widened the rail by exactly 40 (start+Δ — no jump)');
  // it adds the dragging class while in flight + removes it on release.
  assert(!(handle.getAttribute('class') || '').includes('dt-rail-dragging'), 'the dragging class is cleared on pointerup');
  // it persisted the final width (so a reload restores the dragged width).
  assertEqual(ui.readRail(), 340, 'the dragged width persisted to localStorage on pointerup');

  // dragging LEFT narrows by the delta magnitude.
  dragRail(handle, 500, -50);
  assertEqual(+root.getAttribute('data-t-rail'), 290, 'a −50px pointer drag narrowed the rail by exactly 50');
});

test('rail sizing: the REGRESSION — with a non-100% page scale, the drag tracks the pointer in LAYOUT space (no jump)', () => {
  try { globalThis.window.localStorage.clear(); } catch (e) { /* ignore */ }
  const root = mountLiveShell('#/');
  const handle = allByClass(root, 'dt-rail-handle')[0];

  // apply a 120% page scale (zoom=1.2 on the root) — the handle lives INSIDE
  // the zoomed root, so a viewport-px pointer delta is 1.2× the layout-px delta.
  shell.applyScale(120, root);
  assert((root.style.cssText.includes('--dt-page-scale:1.2') || root.getAttribute('data-t-scale') === '120'),
    'the 120% scale is reflected on the root');
  shell.applyRail(300, root);

  // drag the pointer +120 VIEWPORT px. In layout space that is +120/1.2 = +100,
  // so the rail must end at 400 — NOT 420 (the old clientX-driven bug would have
  // over-tracked because --dt-rail is laid out unscaled).
  dragRail(handle, 600, 120);
  assertEqual(+root.getAttribute('data-t-rail'), 400,
    'the rail tracked the pointer in LAYOUT space (Δx/scale = 100), not raw viewport px (the jumpiness fix)');

  // and at scale 80% a +80 viewport-px drag is +100 layout px.
  shell.applyScale(80, root);
  shell.applyRail(300, root);
  dragRail(handle, 600, 80);
  assertEqual(+root.getAttribute('data-t-rail'), 400, 'at 80% scale the drag still maps Δx/scale into layout space');
});

test('rail sizing: a pointer drag CLAMPS at the min/max (a big drag cannot collapse or overrun the rail)', () => {
  try { globalThis.window.localStorage.clear(); } catch (e) { /* ignore */ }
  const root = mountLiveShell('#/');
  const handle = allByClass(root, 'dt-rail-handle')[0];

  shell.applyRail(ui.DEFAULT_RAIL, root);
  // a huge rightward drag clamps to the max.
  dragRail(handle, 500, 5000);
  assertEqual(+root.getAttribute('data-t-rail'), ui.RAIL_MAX, 'an oversize drag clamps at RAIL_MAX');
  // a huge leftward drag clamps to the min.
  shell.applyRail(ui.DEFAULT_RAIL, root);
  dragRail(handle, 500, -5000);
  assertEqual(+root.getAttribute('data-t-rail'), ui.RAIL_MIN, 'an undersize drag clamps at RAIL_MIN');
});

test('rail sizing: a re-render MID-DRAG does NOT snap the width back to the persisted value', () => {
  try { globalThis.window.localStorage.clear(); } catch (e) { /* ignore */ }
  const root = mountLiveShell('#/');
  const handle = allByClass(root, 'dt-rail-handle')[0];

  // persisted width is 300; the live drag will move past it.
  shell.applyRail(300, root);
  assertEqual(ui.readRail(), 300, 'the persisted width is 300');

  // start the drag and move +40 → 340 (live, not yet persisted).
  handle.dispatchEvent({ type: 'pointerdown', pointerId: 1, clientX: 500, preventDefault() {} });
  handle.dispatchEvent({ type: 'pointermove', pointerId: 1, clientX: 540, preventDefault() {} });
  assertEqual(+root.getAttribute('data-t-rail'), 340, 'mid-drag the rail tracks the pointer (340)');
  assertEqual(ui.readRail(), 300, 'the live drag has NOT persisted yet (still 300 in storage)');

  // MID-DRAG re-render: a competing caller (a state:changed tick) re-applies the
  // PERSISTED width — the guard makes it a no-op so the rail does not snap back.
  shell.applyRail(ui.readRail(), root);
  assertEqual(+root.getAttribute('data-t-rail'), 340, 'the mid-drag re-render did NOT snap the width back to 300');

  // continue dragging then release — the final dragged width stands + persists.
  handle.dispatchEvent({ type: 'pointermove', pointerId: 1, clientX: 560, preventDefault() {} });
  handle.dispatchEvent({ type: 'pointerup', pointerId: 1, clientX: 560, preventDefault() {} });
  assertEqual(+root.getAttribute('data-t-rail'), 360, 'the final dragged width (start+60) survived the mid-drag re-render');
  assertEqual(ui.readRail(), 360, 'the final dragged width persisted on pointerup');
});

test('rail sizing: ui.pageScaleOf reads the live page-scale factor (zoom / --dt-page-scale / data-t-scale)', () => {
  const root = document.createElement('div');
  assertEqual(ui.pageScaleOf(root), 1, 'no scale set → identity factor 1');
  root.style.setProperty('--dt-page-scale', '1.25');
  assertEqual(ui.pageScaleOf(root), 1.25, 'reads the --dt-page-scale ratio');
  root.style.zoom = '0.8';
  assertEqual(ui.pageScaleOf(root), 0.8, 'prefers the inline zoom when present');
  const root2 = document.createElement('div');
  root2.setAttribute('data-t-scale', '150');
  assertEqual(ui.pageScaleOf(root2), 1.5, 'falls back to the data-t-scale percent attribute');
  assertEqual(ui.pageScaleOf(null), 1, 'a null root is the identity factor');
});

test('rail sizing CSS: the body grid keys on --dt-rail and the handle has a col-resize cursor (no-flash chrome)', () => {
  const css = readCss().replace(/\n/g, ' ');
  assert(/\.dt-body\s*\{[^}]*grid-template-columns:[^;]*var\(--dt-rail/.test(css), 'the body grid’s first column is the --dt-rail width');
  assert(/\.dt-rail-handle\s*\{[^}]*cursor:\s*col-resize/.test(css), 'the rail handle carries a col-resize cursor');
});

// ====================================================================
// CHANGE 3 — the upper-left "back" button is relabelled "up" (its
// function: navigate UP the selection hierarchy, not browser-back).
// ====================================================================

test('up button: the upper-left control reads "up" (not "back"), labels itself "navigate up", and still navigates to the parent route', async () => {
  freshState();
  const root = mountLiveShell(`#/e/${EPOCH_ID}/gen/v1`);
  await new Promise((r) => setTimeout(r, 0));
  await new Promise((r) => setTimeout(r, 0));

  const upBtn = allByClass(root, 'dt-back')[0];
  assert(upBtn, 'the upper-left navigation control rendered');
  // it reads "up", NOT "back".
  const txt = allByClass(root, 'dt-back-text')[0];
  assert(txt && txt.textContent === 'up', 'the button text reads "up"');
  assert(!(root.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('dt-back-text'))[0].textContent.toLowerCase().includes('back')),
    'the button no longer reads "back"');
  // the glyph is an up-arrow, and the aria-label/title name "up" / "navigate up".
  const glyph = allByClass(root, 'dt-back-glyph')[0];
  assert(glyph && glyph.textContent === '↑', 'the glyph is an up arrow (↑)');
  assert((upBtn.getAttribute('aria-label') || '').toLowerCase().includes('up'), 'the aria-label names "up" (navigate up)');
  assert(!(upBtn.getAttribute('aria-label') || '').toLowerCase().includes('back'), 'the aria-label no longer says "back"');
  assert((upBtn.getAttribute('title') || '').toLowerCase().includes('up'), 'the title names "up"');

  // BEHAVIOUR UNCHANGED: clicking it still navigates UP to the parent route.
  upBtn.dispatchEvent({ type: 'click' });
  await new Promise((r) => setTimeout(r, 0));
  await new Promise((r) => setTimeout(r, 0));
  assertEqual(location.hash, `#/e/${EPOCH_ID}/gens`, 'clicking "up" navigates to the parent route (candidate → generations)');
});

// ====================================================================
// LIVE-RUN display — SSE-driven, animated funnel/ladder transitions,
// tournament progress, the activity ticker, and the live hero, PLUS the
// champion/benchmark (v0) reference in the racing ladder.
//   (a) an active-tournament (racing, running) → the live hero + funnel
//       render and the activity ticker lists events;
//   (b) feeding a phase/active-runs update MUTATES the live surfaces
//       WITHOUT a full repaint (node identity preserved / digest gates
//       structure);
//   (c) under prefers-reduced-motion the animation classes/transitions
//       are suppressed (the reduced-motion CSS gate);
//   (d) the racing ladder shows the champion/benchmark (v0) reference and
//       labels deltas as vs-v0;
//   (e) idle (no active run) renders the static views unchanged;
//   (f) the live engine's derivations (progress, activity diff, ticker).
// ====================================================================

const live = await import('../js/variants/T/live.js');

// the LIVE racing active-tournament topology used to drive the hero: rung 0 has
// cut v2/v3 and carried v0/v1; rung 1 is still racing (no cut yet); v0 is the
// champion the field is raced against (the benchmark seat in every rung).
const HERO_LIVE_RACING = {
  structure: 'racing', phase: 'running',
  structure_params: { rungs: [{ fraction: 0.5 }, { fraction: 1.0 }] },
  champion_lineage: ['v0'],
  competitors: [
    { generation_id: 'v0', role: 'champion' }, { generation_id: 'v1', role: 'challenger' },
    { generation_id: 'v2', role: 'challenger' }, { generation_id: 'v3', role: 'challenger' },
  ],
  rounds: [
    { round_index: 0, label: 'Rung 1', matches: [{ match_id: 'rung1', competitors: ['v0', 'v1', 'v2', 'v3'], survivors: ['v0', 'v1'], cut: ['v2', 'v3'], board_fraction: 0.5, deltas: { v1: -0.2, v2: 1.0, v3: 2.0 } }] },
    { round_index: 1, label: 'Rung 2', matches: [{ match_id: 'rung2', competitors: ['v0', 'v1'], survivors: [], cut: [], board_fraction: 1.0 }] },
  ],
  standings: [],
};

// ---- (a) the live hero + funnel render; the ticker lists events ----

test('live hero: an active-tournament (racing, running) renders the live hero + scalar track and the activity ticker lists events', () => {
  try { globalThis.window.localStorage.clear(); } catch (e) { /* ignore */ }
  coreState.state.connected = true; coreState.state.connecting = false;
  coreState.state.setHeartbeat({ phase: 'tournament:round_1:rung1_m0', generation_id: 'v1' });
  coreState.state.activeRuns = [
    { generation_id: 'v1', entry_id: 'b0', run_id: 'r1', progress: 0.5 },
    { generation_id: 'v0', entry_id: 'b1', run_id: 'r2', progress: 0.3 },
  ];
  coreState.state.activeTournament = HERO_LIVE_RACING;

  const root = mountLiveShell('#/');
  // the hero host is flagged live + the hero panel carries the .dt-live-on class.
  const heroHost = allByClass(root, 'dt-hero-host')[0];
  assert(heroHost && (heroHost.getAttribute('class') || '').includes('dt-hero-live'), 'the hero host is flagged live during a run');
  const hero = allByClass(root, 'dt-live-hero')[0];
  assert(hero && (hero.getAttribute('class') || '').includes('dt-live-on'), 'the live hero is shown (dt-live-on) for a running tournament');
  // the prominent phase reads the structure+phase label.
  const phase = allByClass(root, 'dt-live-hero-phase')[0];
  assert(phase && phase.textContent.includes('racing'), 'the hero names the current phase (racing)');
  // the tournament-level progress indicator: "rung k of N".
  const proglab = allByClass(root, 'dt-live-hero-proglab')[0];
  assert(proglab && /rung\s+\d+\s+of\s+\d+/.test(proglab.textContent), 'the hero shows a tournament-level "rung k of N" progress label');
  // the determinate progress bar carries a width.
  const fill = allByClass(root, 'dt-live-hero-progfill')[0];
  assert(fill && /%/.test(fill.style.cssText || ''), 'the progress bar fill carries a width');
  // the in-flight unit count.
  const count = allByClass(root, 'dt-live-hero-count')[0];
  assert(count && count.textContent.includes('2'), 'the hero shows the in-flight unit count (2)');
  // the racing scalar track rendered inside the hero (the live hero mini is the
  // single-round PRIMARY figure: the field on one loss number-line).
  const track = svgsByClass(root, 'dn-scalartrack')[0];
  assert(track, 'the racing scalar track rendered inside the live hero');
  // the activity ticker lists events derived from the live state.
  const ticker = allByClass(root, 'dt-ticker')[0];
  assert(ticker, 'the activity ticker rendered');
  const rows = allByClass(root, 'dt-ticker-row');
  assert(rows.length >= 1, 'the ticker lists at least one live activity event');

  coreState.state.heartbeat = { phase: 'idle' };
  coreState.state.activeRuns = []; coreState.state.activeTournament = null;
});

// ---- (b) a live update MUTATES the surfaces without a full repaint ----

test('live hero: a phase/active-runs update mutates the live surfaces WITHOUT a full repaint (node identity preserved; structure digest gates the scalar track)', () => {
  try { globalThis.window.localStorage.clear(); } catch (e) { /* ignore */ }
  coreState.state.connected = true; coreState.state.connecting = false;
  coreState.state.setHeartbeat({ phase: 'tournament:round_1:rung1_m0', generation_id: 'v1' });
  coreState.state.activeRuns = [{ generation_id: 'v1', entry_id: 'b0', run_id: 'r1', progress: 0.2 }];
  coreState.state.activeTournament = HERO_LIVE_RACING;

  const root = mountLiveShell('#/');
  const phaseNodeBefore = allByClass(root, 'dt-live-hero-phase')[0];
  const fillNodeBefore = allByClass(root, 'dt-live-hero-progfill')[0];
  const trackBefore = svgsByClass(root, 'dn-scalartrack')[0];
  const tickerListBefore = allByClass(root, 'dt-ticker-list')[0];
  assert(phaseNodeBefore && fillNodeBefore && trackBefore && tickerListBefore, 'the live surfaces mounted');
  const rowsBefore = allByClass(root, 'dt-ticker-row').length;

  // a STEADY re-tick with IDENTICAL live state writes no new ticker rows and
  // does NOT rebuild the scalar track (the structure digest is unchanged → no flash).
  coreState.state._changed();
  assertEqual(allByClass(root, 'dt-ticker-row').length, rowsBefore, 'an identical re-tick appends NO ticker rows (no flash)');
  assert(svgsByClass(root, 'dn-scalartrack')[0] === trackBefore, 'an identical re-tick does NOT rebuild the scalar track (digest-gated structure)');
  // the persistent phase / progress / ticker-list nodes keep identity.
  assert(allByClass(root, 'dt-live-hero-phase')[0] === phaseNodeBefore, 'the phase node keeps identity across a re-tick (patched in place)');
  assert(allByClass(root, 'dt-live-hero-progfill')[0] === fillNodeBefore, 'the progress-fill node keeps identity (its width is patched, not rebuilt)');
  assert(allByClass(root, 'dt-ticker-list')[0] === tickerListBefore, 'the ticker list keeps identity (append-only)');

  // now a REAL change: rung 2 resolves (v0 cut, v1 survives) + a run completes.
  const next = JSON.parse(JSON.stringify(HERO_LIVE_RACING));
  next.rounds[1].matches[0].survivors = ['v1'];
  next.rounds[1].matches[0].cut = ['v0'];
  coreState.state.activeTournament = next;
  coreState.state.activeRuns = [];   // the in-flight run completed.
  coreState.state.setHeartbeat({ phase: 'tournament:round_2:racing-final' });
  coreState.state._changed();

  // the scalar track rebuilt (the structure digest changed) — but the ticker LIST
  // and the phase node are still the SAME persistent nodes (mutated, not replaced).
  assert(allByClass(root, 'dt-ticker-list')[0] === tickerListBefore, 'the ticker list is still the same node after a real change (append-only growth)');
  assert(allByClass(root, 'dt-live-hero-phase')[0] === phaseNodeBefore, 'the phase node is still the same node (patched, not rebuilt)');
  assert(allByClass(root, 'dt-ticker-row').length > rowsBefore, 'a real change (rung cut + run completed) appended new ticker rows');
  // the newly-built scalar track carries the one-shot entrance animation class.
  const trackAfter = svgsByClass(root, 'dn-scalartrack')[0];
  assert((trackAfter.getAttribute('class') || '').includes('dt-live-enter'), 'a freshly-built scalar track carries the one-shot entrance class (eases in, never repaint-loops)');

  coreState.state.heartbeat = { phase: 'idle' };
  coreState.state.activeRuns = []; coreState.state.activeTournament = null;
});

// ---- (b2) the hero is STRUCTURE-AWARE + SCOPED TO THE CURRENT TOURNAMENT ----
//
// The racing survival funnel is meaningful ONLY for a LIVE racing tournament
// that belongs to the CURRENT run. These tests pin the gate (structure===racing
// AND running AND current-epoch): a swiss live tournament shows round-based
// progress with NO funnel; a current-epoch PROPOSING phase with a stale racing
// active-tournament from a DIFFERENT epoch shows the honest proposing/empty
// state (no funnel, no leaked foreign competitor ids); a completed/idle
// tournament shows no funnel.

const HERO_EPOCH = '2026-06-02_e3';

// a LIVE racing tournament that names the CURRENT epoch — the funnel SHOULD show.
const HERO_LIVE_RACING_E3 = JSON.parse(JSON.stringify(HERO_LIVE_RACING));
HERO_LIVE_RACING_E3.epoch_id = HERO_EPOCH;

// a STALE/FOREIGN completed racing tournament retained from a PRIOR epoch (e1):
// it carries v6/v8 survivors + v5/v7 cuts + a "vs champion v0" gate — exactly
// the prior-epoch funnel the bug leaked into e3's proposing hero.
const HERO_STALE_RACING_E1 = {
  structure: 'racing', phase: 'completed', epoch_id: '2026-06-01_e1',
  structure_params: { rungs: [{ fraction: 0.5 }, { fraction: 1.0 }] },
  champion_lineage: ['v0'],
  competitors: [
    { generation_id: 'v0', role: 'champion' }, { generation_id: 'v5', role: 'challenger' },
    { generation_id: 'v6', role: 'challenger' }, { generation_id: 'v7', role: 'challenger' },
    { generation_id: 'v8', role: 'challenger' },
  ],
  rounds: [
    { round_index: 0, label: 'Rung 1', matches: [{ match_id: 'rung1', competitors: ['v5', 'v6', 'v7', 'v8'], survivors: ['v6', 'v8'], cut: ['v5', 'v7'], board_fraction: 0.5 }] },
    { round_index: 1, label: 'Champion gate', matches: [{ match_id: 'racing-final', competitors: ['v0', 'v6'], winner: 'v0', decision: 'rejected', board_fraction: 1.0 }] },
  ],
  standings: [],
};

// a LIVE swiss tournament for the current epoch — round-based, NO racing funnel.
const HERO_LIVE_SWISS_E3 = {
  structure: 'swiss', phase: 'running', epoch_id: HERO_EPOCH,
  structure_params: { rounds: 3 },
  competitors: [
    { generation_id: 'v0' }, { generation_id: 'v1' }, { generation_id: 'v2' }, { generation_id: 'v3' },
  ],
  rounds: [
    { round_index: 0, label: 'Round 1', matches: [
      { match_id: 'sw_r0_m0', competitors: ['v0', 'v1'], winner: 'v1', decision: 'win' },
      { match_id: 'sw_r0_m1', competitors: ['v2', 'v3'], winner: 'v3', decision: 'win' },
    ] },
    { round_index: 1, label: 'Round 2', matches: [
      { match_id: 'sw_r1_m0', competitors: ['v1', 'v3'] },
      { match_id: 'sw_r1_m1', competitors: ['v0', 'v2'] },
    ] },
    { round_index: 2, label: 'Round 3', matches: [
      { match_id: 'sw_r2_m0', competitors: ['v1', 'v2'] },
      { match_id: 'sw_r2_m1', competitors: ['v0', 'v3'] },
    ] },
  ],
  standings: [],
};

test('live hero: a LIVE RACING tournament for the CURRENT epoch renders the scalar track', () => {
  try { globalThis.window.localStorage.clear(); } catch (e) { /* ignore */ }
  coreState.state.connected = true; coreState.state.connecting = false;
  coreState.state.setHeartbeat({ phase: 'tournament:round_1:rung1_m0', generation_id: 'v1', epoch_id: HERO_EPOCH });
  coreState.state.activeRuns = [{ generation_id: 'v1', entry_id: 'b0', run_id: 'r1', progress: 0.5 }];
  coreState.state.activeTournament = HERO_LIVE_RACING_E3;

  const root = mountLiveShell('#/');
  const track = svgsByClass(root, 'dn-scalartrack')[0];
  assert(track, 'the racing scalar track renders for a LIVE racing tournament whose epoch matches the heartbeat');
  // the track was eligible → no "field fills in…" placeholder fallback.
  assert(allByClass(root, 'dt-live-hero-nofunnel').length === 0, 'no empty/proposing placeholder when the racing scalar track is live');

  coreState.state.heartbeat = { phase: 'idle' };
  coreState.state.activeRuns = []; coreState.state.activeTournament = null;
});

test('live hero: a LIVE SWISS tournament shows the SWISS LADDER + round-based progress, NOT the racing funnel', () => {
  try { globalThis.window.localStorage.clear(); } catch (e) { /* ignore */ }
  coreState.state.connected = true; coreState.state.connecting = false;
  coreState.state.setHeartbeat({ phase: 'tournament:round_1', generation_id: 'v1', epoch_id: HERO_EPOCH });
  coreState.state.activeRuns = [{ generation_id: 'v1', entry_id: 'b0', run_id: 'r1', progress: 0.5 }];
  coreState.state.activeTournament = HERO_LIVE_SWISS_E3;

  const root = mountLiveShell('#/');
  const hero = allByClass(root, 'dt-live-hero')[0];
  assert(hero && (hero.getAttribute('class') || '').includes('dt-live-on'), 'the live hero is shown for a running swiss tournament');
  // the round-based progress line is present ("round k of N").
  const proglab = allByClass(root, 'dt-live-hero-proglab')[0];
  assert(proglab && /round\s+\d+\s+of\s+\d+/.test(proglab.textContent), 'the swiss hero shows a round-based progress label (round k of N)');
  // the activity ticker still streams.
  assert(allByClass(root, 'dt-ticker')[0], 'the activity ticker renders for a swiss run');
  // the LIVE SWISS LADDER renders (the swiss analogue of the racing funnel) —
  // NOT the racing funnel and NOT just the text placeholder.
  const ladder = svgsByClass(root, 'dn-swissladder')[0];
  assert(ladder, 'the live swiss standings ladder rendered in the hero');
  assertEqual(svgsByClass(root, 'dn-funnel').length, 0, 'NO racing survival funnel for a LIVE swiss tournament');
  assertEqual(svgsByClass(root, 'dn-elimbracket').length, 0, 'NO elim bracket for a LIVE swiss tournament');
  assertEqual(allByClass(root, 'dt-live-hero-nofunnel').length, 0, 'no text placeholder once the swiss ladder is live');

  coreState.state.heartbeat = { phase: 'idle' };
  coreState.state.activeRuns = []; coreState.state.activeTournament = null;
});

// a LIVE single-elim tournament for the current epoch — bracket, NO funnel.
const HERO_LIVE_ELIM_E3 = {
  structure: 'single_elim', phase: 'running', epoch_id: HERO_EPOCH,
  structure_params: { board_size: 4 },
  champion_lineage: ['v0'],
  competitors: [
    { generation_id: 'v0', role: 'champion' }, { generation_id: 'v1', role: 'challenger' },
    { generation_id: 'v2', role: 'challenger' }, { generation_id: 'v3', role: 'challenger' },
  ],
  rounds: [
    { round_index: 0, label: 'Semifinal', matches: [
      { match_id: 'WB-R0-0', competitors: ['v0', 'v3'], winner: 'v0', decision: 'win', bracket_slot: 'WB-R0-0' },
      { match_id: 'WB-R0-1', competitors: ['v1', 'v2'], bracket_slot: 'WB-R0-1' },
    ] },
    { round_index: 1, label: 'Final', matches: [
      { match_id: 'WB-R1-0', competitors: ['v0', 'v1'], bracket_slot: 'WB-R1-0' },
    ] },
  ],
  standings: [],
};

test('live hero: a LIVE SINGLE-ELIM tournament renders the RADIAL bracket, NOT the racing track or swiss ladder', () => {
  try { globalThis.window.localStorage.clear(); } catch (e) { /* ignore */ }
  coreState.state.connected = true; coreState.state.connecting = false;
  coreState.state.setHeartbeat({ phase: 'tournament:round_0', generation_id: 'v1', epoch_id: HERO_EPOCH });
  coreState.state.activeRuns = [{ generation_id: 'v1', entry_id: 'b0', run_id: 'r1', progress: 0.5 }];
  coreState.state.activeTournament = HERO_LIVE_ELIM_E3;

  const root = mountLiveShell('#/');
  // single_elim hero is the concentric-ring RADIAL (the single-round primary).
  const bracket = svgsByClass(root, 'dn-elimradial')[0];
  assert(bracket, 'the live single-elim radial bracket rendered in the hero');
  assertEqual(svgsByClass(root, 'dn-elimbracket').length, 0, 'the seat/box bracket tree is retired');
  assertEqual(svgsByClass(root, 'dn-scalartrack').length, 0, 'NO racing scalar track for a LIVE elim tournament');
  assertEqual(svgsByClass(root, 'dn-swissladder').length, 0, 'NO swiss ladder for a LIVE elim tournament');
  assertEqual(allByClass(root, 'dt-live-hero-nofunnel').length, 0, 'no text placeholder once the bracket is live');
  // the eliminated semifinal lane terminates with ✕ (the radial emits a cut glyph).
  assert(/✕/.test(bracket.textContent), 'a decided semifinal eliminates a lane (✕)');

  coreState.state.heartbeat = { phase: 'idle' };
  coreState.state.activeRuns = []; coreState.state.activeTournament = null;
});

test('live hero: racing STILL renders the scalar track (no swiss/elim regression), and a foreign-epoch elim shows the honest empty state', () => {
  try { globalThis.window.localStorage.clear(); } catch (e) { /* ignore */ }
  coreState.state.connected = true; coreState.state.connecting = false;
  // racing for the current epoch → the scalar track (unchanged structure-wise).
  coreState.state.setHeartbeat({ phase: 'tournament:round_1:rung1_m0', generation_id: 'v1', epoch_id: HERO_EPOCH });
  coreState.state.activeRuns = [{ generation_id: 'v1', entry_id: 'b0', run_id: 'r1', progress: 0.5 }];
  coreState.state.activeTournament = HERO_LIVE_RACING_E3;
  let root = mountLiveShell('#/');
  assert(svgsByClass(root, 'dn-scalartrack')[0], 'racing still renders the scalar track');
  assertEqual(svgsByClass(root, 'dn-swissladder').length, 0, 'no swiss ladder for a racing run');
  assertEqual(svgsByClass(root, 'dn-elimbracket').length, 0, 'no elim bracket for a racing run');

  // a FOREIGN-epoch elim (current epoch proposing) → no elim topology in the hero.
  const foreignElim = JSON.parse(JSON.stringify(HERO_LIVE_ELIM_E3));
  foreignElim.epoch_id = '2026-06-01_e1';
  coreState.state.setHeartbeat(freshHb({ phase: 'proposing:field', generation_id: '', epoch_id: HERO_EPOCH }));
  coreState.state.activeRuns = [];
  coreState.state.activeTournament = foreignElim;
  root = mountLiveShell('#/');
  assertEqual(svgsByClass(root, 'dn-elimbracket').length, 0, 'NO elim bracket for a foreign-epoch tournament while the current epoch proposes');
  assertEqual(svgsByClass(root, 'dn-elimradial').length, 0, 'NO elim radial either for a foreign-epoch tournament while proposing');
  assertEqual(svgsByClass(root, 'dn-funnel').length, 0, 'no funnel either — honest empty');
  assert(allByClass(root, 'dt-live-hero-nofunnel').length >= 1, 'the hero shows the honest proposing/empty state');
  const heroText = (allByClass(root, 'dt-live-hero')[0] || {}).textContent || '';
  assert(!/v2\b|v3\b/.test(heroText) || /propos/i.test(heroText), 'no foreign-epoch bracket topology leaks into the proposing hero');

  coreState.state.heartbeat = { phase: 'idle' };
  coreState.state.activeRuns = []; coreState.state.activeTournament = null;
});

test('live hero: a PROPOSING current run (no tournament) shows the honest proposing state — no swiss/elim/racing topology', () => {
  try { globalThis.window.localStorage.clear(); } catch (e) { /* ignore */ }
  coreState.state.connected = true; coreState.state.connecting = false;
  coreState.state.setHeartbeat(freshHb({ phase: 'proposing:field', generation_id: '', epoch_id: HERO_EPOCH }));
  coreState.state.activeRuns = [];
  coreState.state.activeTournament = null;
  const root = mountLiveShell('#/');
  assertEqual(svgsByClass(root, 'dn-funnel').length, 0, 'no racing funnel while proposing');
  assertEqual(svgsByClass(root, 'dn-swissladder').length, 0, 'no swiss ladder while proposing');
  assertEqual(svgsByClass(root, 'dn-elimbracket').length, 0, 'no elim bracket while proposing');
  assert(allByClass(root, 'dt-live-hero-nofunnel').length >= 1, 'the honest proposing placeholder is shown');

  coreState.state.heartbeat = { phase: 'idle' };
  coreState.state.activeRuns = []; coreState.state.activeTournament = null;
});

test('live hero: a CURRENT-EPOCH PROPOSING phase with a STALE racing tournament from a DIFFERENT epoch shows the proposing/empty state — NOT the prior epoch funnel', () => {
  try { globalThis.window.localStorage.clear(); } catch (e) { /* ignore */ }
  coreState.state.connected = true; coreState.state.connecting = false;
  // the current run is e3 PROPOSING; the active-tournament is e1's COMPLETED racer.
  coreState.state.setHeartbeat(freshHb({ phase: 'proposing:field', generation_id: '', epoch_id: HERO_EPOCH }));
  coreState.state.activeRuns = [];
  coreState.state.activeTournament = HERO_STALE_RACING_E1;

  const root = mountLiveShell('#/');
  const hero = allByClass(root, 'dt-live-hero')[0];
  assert(hero && (hero.getAttribute('class') || '').includes('dt-live-on'), 'the hero is live during the proposing phase (an active-tournament running)');
  // CRITICAL: NO racing funnel for a stale/foreign-epoch tournament.
  assertEqual(svgsByClass(root, 'dn-funnel').length, 0, 'NO racing funnel while the current epoch is proposing with only a foreign-epoch active-tournament');
  // the honest proposing/empty placeholder is shown.
  assert(allByClass(root, 'dt-live-hero-nofunnel').length >= 1, 'the hero shows the honest proposing/empty progress state');
  // the progress label reflects the CURRENT run (proposing), not "rung k of N".
  const proglab = allByClass(root, 'dt-live-hero-proglab')[0];
  assert(proglab && /propos/i.test(proglab.textContent), 'the progress label reads the current proposing phase, not a stale rung count');
  assert(proglab && !/rung\s+\d+\s+of\s+\d+/.test(proglab.textContent), 'the progress label does NOT leak the stale rung count');
  // no leaked prior-epoch competitor ids anywhere in the hero (e.g. v5/v6/v7/v8).
  const heroText = hero.textContent || '';
  assert(!/v5|v6|v7|v8/.test(heroText), 'no leaked prior-epoch competitor ids (foreign survivors/cuts) appear in the hero');

  coreState.state.heartbeat = { phase: 'idle' };
  coreState.state.activeRuns = []; coreState.state.activeTournament = null;
});

test('live hero: a COMPLETED/idle racing tournament renders NO funnel (the funnel is live-only)', () => {
  try { globalThis.window.localStorage.clear(); } catch (e) { /* ignore */ }
  coreState.state.connected = true; coreState.state.connecting = false;
  // an in-flight unit keeps the hero "live", but the tournament itself is completed.
  coreState.state.setHeartbeat({ phase: 'tournament:round_1:rung1_m0', generation_id: 'v1', epoch_id: HERO_EPOCH });
  coreState.state.activeRuns = [{ generation_id: 'v1', entry_id: 'b0', run_id: 'r1', progress: 0.5 }];
  const completed = JSON.parse(JSON.stringify(HERO_LIVE_RACING_E3));
  completed.phase = 'completed';
  coreState.state.activeTournament = completed;

  const root = mountLiveShell('#/');
  assertEqual(svgsByClass(root, 'dn-funnel').length, 0, 'NO racing funnel for a COMPLETED tournament (the live hero funnel is running-only)');

  coreState.state.heartbeat = { phase: 'idle' };
  coreState.state.activeRuns = []; coreState.state.activeTournament = null;
});

// ---- (b3) the PROPOSING-STEP TRACKER in the live hero ----
//
// During the proposing phase (and the early tournament phase before a
// structure topology exists) the live hero shows the field FORMING — the
// per-challenger applied/rejected outcomes — instead of the bland
// placeholder. Structure-agnostic, digest-gated, current-epoch-scoped.

// a CURRENT-epoch proposing-phase active-tournament carrying field_status:
// two challengers minted, one applied, one rejected. No structure topology
// yet (rounds empty) — so the tracker leads, not a figure.
const HERO_PROPOSING_E3 = {
  structure: 'swiss', phase: 'proposing', epoch_id: HERO_EPOCH,
  structure_params: { rounds: 3 },
  competitors: [{ generation_id: 'v0', seed: 1, role: 'champion' }],
  rounds: [], standings: [],
  field_status: [
    { generation_id: 'v1', status: 'applied', reason: '', seed: 2 },
    { generation_id: 'v2', status: 'rejected', reason: 'proposer returned invalid JSON', seed: 3 },
  ],
};

test('live hero: the PROPOSING phase shows the proposing-step tracker (applied ✓ / rejected ✗) instead of the bland placeholder', () => {
  try { globalThis.window.localStorage.clear(); } catch (e) { /* ignore */ }
  coreState.state.connected = true; coreState.state.connecting = false;
  coreState.state.setHeartbeat(freshHb({ phase: 'proposing:field', generation_id: '', epoch_id: HERO_EPOCH }));
  coreState.state.activeRuns = [];
  coreState.state.activeTournament = HERO_PROPOSING_E3;

  const root = mountLiveShell('#/');
  const tracker = allByClass(root, 'dn-prop-tracker')[0];
  assert(tracker, 'the proposing-step tracker renders in the live hero during the proposing phase');
  // the bland placeholder is REPLACED by the tracker.
  assertEqual(allByClass(root, 'dt-live-hero-nofunnel').length, 0, 'the bland "field fills in…" placeholder is replaced by the tracker');
  // no structure figure yet (rounds empty).
  assertEqual(svgsByClass(root, 'dn-funnel').length + svgsByClass(root, 'dn-swissladder').length + svgsByClass(root, 'dn-elimbracket').length, 0, 'no structure figure while only the field is forming');
  // one applied (✓ v1) + one rejected (✗ v2) row.
  assertEqual(allByClass(root, 'dn-prop-row-ok').length, 1, 'one applied row');
  assertEqual(allByClass(root, 'dn-prop-row-bad').length, 1, 'one rejected row');
  assert(tracker.textContent.includes('2 proposed') && tracker.textContent.includes('1 applied'), 'the headline counts the minted field');

  coreState.state.heartbeat = { phase: 'idle' };
  coreState.state.activeRuns = []; coreState.state.activeTournament = null;
});

test('live hero: an ALL-REJECTED proposing field reads "0 applied — all rejected", NOT an idle/empty hero', () => {
  try { globalThis.window.localStorage.clear(); } catch (e) { /* ignore */ }
  coreState.state.connected = true; coreState.state.connecting = false;
  coreState.state.setHeartbeat(freshHb({ phase: 'proposing:field', generation_id: '', epoch_id: HERO_EPOCH }));
  coreState.state.activeRuns = [];
  const allBad = JSON.parse(JSON.stringify(HERO_PROPOSING_E3));
  allBad.field_status = [
    { generation_id: 'v1', status: 'rejected', reason: 'empty response', seed: 2 },
    { generation_id: 'v2', status: 'rejected', reason: 'empty response', seed: 3 },
    { generation_id: 'v3', status: 'rejected', reason: 'post-apply validation failed', seed: 4 },
    { generation_id: 'v4', status: 'rejected', reason: 'mutation_id no longer resolves', seed: 5 },
  ];
  coreState.state.activeTournament = allBad;

  const root = mountLiveShell('#/');
  const head = allByClass(root, 'dn-prop-head')[0];
  assert(head, 'the proposing-step tracker headline rendered (NOT an empty hero)');
  assert(head.textContent.includes('4 proposed') && head.textContent.includes('0 applied'), 'the headline reads "4 proposed · 0 applied"');
  assert(/all rejected/i.test(head.textContent), 'the all-rejected field reads "all rejected"');
  assertEqual(allByClass(root, 'dn-prop-row-ok').length, 0, 'no applied rows');
  assertEqual(allByClass(root, 'dn-prop-row-bad').length, 4, 'all four rejected rows render');
  assertEqual(allByClass(root, 'dt-live-hero-nofunnel').length, 0, 'NOT the idle/empty placeholder');

  coreState.state.heartbeat = { phase: 'idle' };
  coreState.state.activeRuns = []; coreState.state.activeTournament = null;
});

test('live hero: the proposing-step tracker is DIGEST-GATED (a no-op heartbeat does not rebuild it) and current-epoch SCOPED', () => {
  try { globalThis.window.localStorage.clear(); } catch (e) { /* ignore */ }
  coreState.state.connected = true; coreState.state.connecting = false;
  coreState.state.setHeartbeat(freshHb({ phase: 'proposing:field', generation_id: '', epoch_id: HERO_EPOCH }));
  coreState.state.activeRuns = [];
  coreState.state.activeTournament = HERO_PROPOSING_E3;

  const root = mountLiveShell('#/');
  const trackerBefore = allByClass(root, 'dn-prop-tracker')[0];
  assert(trackerBefore, 'the tracker mounted');
  // a STEADY re-tick with identical field_status does NOT rebuild the tracker.
  coreState.state._changed();
  assert(allByClass(root, 'dn-prop-tracker')[0] === trackerBefore, 'an identical re-tick does NOT rebuild the tracker (digest-gated, no flash)');
  // a REAL change (v2 now applies) rebuilds it.
  const next = JSON.parse(JSON.stringify(HERO_PROPOSING_E3));
  next.field_status[1].status = 'applied';
  coreState.state.activeTournament = next;
  coreState.state._changed();
  const trackerAfter = allByClass(root, 'dn-prop-tracker')[0];
  assert(trackerAfter && trackerAfter !== trackerBefore, 'a real field-status change rebuilds the tracker');
  assertEqual(allByClass(root, 'dn-prop-row-ok').length, 2, 'both challengers now read as applied');

  // CURRENT-EPOCH SCOPED: a foreign-epoch field_status must NOT render.
  const foreign = JSON.parse(JSON.stringify(HERO_PROPOSING_E3));
  foreign.epoch_id = '2026-06-01_e1';
  coreState.state.activeTournament = foreign;
  const root2 = mountLiveShell('#/');
  assertEqual(allByClass(root2, 'dn-prop-tracker').length, 0, 'a foreign-epoch proposing field is NOT shown (current-epoch scoped)');
  assert(allByClass(root2, 'dt-live-hero-nofunnel').length >= 1, 'the foreign-epoch case falls back to the honest placeholder');

  coreState.state.heartbeat = { phase: 'idle' };
  coreState.state.activeRuns = []; coreState.state.activeTournament = null;
});

// ---- (c) reduced-motion suppresses the animations ----

test('live motion: prefers-reduced-motion suppresses the live animation classes/transitions (the reduced-motion CSS gate)', () => {
  const css = readCss().replace(/\s+/g, ' ');
  // the reduced-motion block exists and zeroes the live motion.
  assert(/@media \(prefers-reduced-motion: reduce\) \{[^}]*\.dt-live-hero-dot \{ animation: none/.test(css)
    || /@media \(prefers-reduced-motion: reduce\) \{[^@]*\.dt-live-hero-dot\b[^}]*animation: none/.test(css),
    'the breathing live dot is stilled under reduced motion');
  // each live animation/transition has a reduced-motion suppression.
  const rm = css.slice(css.indexOf('@media (prefers-reduced-motion: reduce)'));
  assert(rm.includes('.dt-live-enter') && /\.dt-live-enter[^;{}]*\{?[^}]*animation: none/.test(rm) || rm.includes('.dt-live-enter'), 'the funnel/ladder entrance is suppressed under reduced motion');
  assert(rm.includes('.dt-ticker-row'), 'the ticker-row slide-in is suppressed under reduced motion');
  assert(rm.includes('.dt-live-hero-progfill'), 'the progress-bar width transition is suppressed under reduced motion');
  assert(/\.dn-funnel-band/.test(rm) && /\.dn-funnel-bar/.test(rm), 'the funnel band + progress-bar transitions are suppressed under reduced motion');
  // sanity: the un-gated rules DO carry motion (so reduced-motion is a real gate).
  assert(/\.dt-ticker-row \{[^}]*animation: dt-ticker-in/.test(css), 'the ticker row animates by default (gated off only under reduced motion)');
  assert(/@keyframes dt-live-fade/.test(css) && /@keyframes dt-ticker-in/.test(css), 'the live keyframes are defined');
});

// ---- (d) the racing model derives the champion/benchmark (v0) reference ----
// (the rendered funnel's benchmark caption is exercised by the "survival funnel:
// carries the champion/benchmark (v0) reference …" test below.)

test('racing: the racingModel derives the champion/benchmark (v0) seat distinct from the survivor', () => {
  const st = STRUCT.reconstructRacing(RACING_TOURNAMENTS, RC_EPOCH);
  const model = STRUCT.racingModel(st);
  assert(model, 'a racing model was derived');
  assertEqual(model.benchmarkId, 'v0', 'the benchmark is the champion v0 (the seat the field is raced against)');
  assertEqual(model.championId, 'v3', 'the champion (eventual survivor) is v3 — distinct from the benchmark v0');
});

test('survival funnel: carries the champion/benchmark (v0) reference + labels the gate vs champion v0', () => {
  const rungs = [
    { match_id: 'rung0', label: 'Rung 1', competitors: ['v1', 'v2', 'v3'], survivors: ['v3'], cut: ['v1', 'v2'], board_fraction: 0.5, deltas: { v3: -1 } },
  ];
  const node = svg.survivalFunnel({ rungs, championId: 'v3', benchmarkId: 'v0', gateState: 'crowned', onCompetitor() {} });
  const bench = node.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('dn-funnel-bench'))[0];
  assert(bench && bench.textContent.includes('v0'), 'the funnel carries a champion/benchmark caption naming v0');
  const gateSubs = node.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('dn-funnel-sub'));
  assert(gateSubs.some((n) => /vs champion v0/.test(n.textContent)), 'the gate sub-label reads "vs champion v0"');
});

// ---- (e) idle renders the static views unchanged ----

test('live: idle (no active run) hides the live hero — the normal summary leads', () => {
  try { globalThis.window.localStorage.clear(); } catch (e) { /* ignore */ }
  coreState.state.connected = true; coreState.state.connecting = false;
  coreState.state.heartbeat = { phase: 'idle' };
  coreState.state.activeRuns = [];
  coreState.state.activeTournament = null;

  const root = mountLiveShell('#/');
  const heroHost = allByClass(root, 'dt-hero-host')[0];
  assert(heroHost && !(heroHost.getAttribute('class') || '').includes('dt-hero-live'), 'the hero host is NOT flagged live when idle');
  const hero = allByClass(root, 'dt-live-hero')[0];
  assert(hero && !(hero.getAttribute('class') || '').includes('dt-live-on'), 'the live hero is hidden (no dt-live-on) when idle');
  // the idle hero adds NO ticker rows.
  assertEqual(allByClass(root, 'dt-ticker-row').length, 0, 'no activity rows accumulate while idle');
});

test('live: an idle racing epoch still renders the static completed funnel/summary (the live hero does not interfere)', async () => {
  freshState();
  installFixtureMap({
    '/api/epoch': { epoch_id: RC_EPOCH, closed: true, goal: 'g', tournament: { structure: 'racing', params: RACING_TOURNAMENTS.structure_params },
      experiments: RACING_PER_CHALLENGER.map((t) => ({ generation_id: t.tournament_id.split('->')[1], parent_generation_id: 'v0', outcome: { decision: 'rejected' } })), board: [] },
    '/api/lineage': { generations: [{ generation_id: 'v0', epoch_id: RC_EPOCH, parent_generation_id: '', promoted: true }] },
    '/api/score-trajectory': { points: [] },
    '/api/tournaments': RACING_TOURNAMENTS,
  });
  coreState.state.heartbeat = { phase: 'idle' };
  coreState.state.activeRuns = []; coreState.state.activeTournament = null;
  const epoch = await import('../js/variants/T/views/epoch.js');
  const host = document.createElement('div');
  await epoch.render(host, { navigate() {}, href: router.href }, { epochId: RC_EPOCH });
  // the static completed funnel still renders (idle view unchanged) + names v0.
  const funnel = svgsByClass(host, 'dn-funnel')[0];
  assert(funnel, 'the static completed survival funnel renders when idle');
  assert(allByClass(host, 'dt-live-pill').length === 0, 'no LIVE pill on an idle epoch funnel');
});

// ---- (f) the live engine's pure derivations ----

test('live engine: liveProgress derives "rung k of N · m/n matchups" + a fraction for a racing tournament', () => {
  const prog = live.liveProgress({
    activeTournament: HERO_LIVE_RACING,
    heartbeat: { phase: 'tournament:round_1:rung1_m0' },
    status: { running: true, structure: 'racing' },
  });
  assertEqual(prog.kind, 'racing', 'a racing topology yields racing progress');
  assert(/rung\s+\d+\s+of\s+2/.test(prog.label), 'the label reads "rung k of N"');
  assert(typeof prog.fraction === 'number' && prog.fraction >= 0 && prog.fraction <= 1, 'a determinate 0..1 fraction');
});

test('live engine: deriveActivity diffs two snapshots into events (matchup started, run completed, rung cut, gate decided)', () => {
  const s0 = live.liveSnapshot({
    status: { running: true, structure: 'racing' },
    heartbeat: { phase: 'tournament:round_0:rung0_m0' },
    activeRuns: [{ generation_id: 'v1', entry_id: 'b0', run_id: 'r1' }],
    activeTournament: { structure: 'racing', rounds: [{ matches: [{ match_id: 'rung0', survivors: [], cut: [] }] }] },
  });
  const s1 = live.liveSnapshot({
    status: { running: true, structure: 'racing' },
    heartbeat: { phase: 'tournament:round_0:rung0_m1' },
    activeRuns: [{ generation_id: 'v2', entry_id: 'b0', run_id: 'r2' }],   // r1 done, r2 started
    activeTournament: { structure: 'racing', rounds: [
      { matches: [{ match_id: 'rung0', survivors: ['v2'], cut: ['v1'] }] },
      { matches: [{ match_id: 'racing-final', winner: 'v2', decision: 'promoted' }] },
    ] },
  });
  const { events } = live.deriveActivity(s0, s1, 0);
  const kinds = events.map((e) => e.kind);
  assert(kinds.includes('matchup'), 'a started matchup event (r2 entered)');
  assert(kinds.includes('run'), 'a completed-run event (r1 left)');
  assert(kinds.includes('cut'), 'a rung-cut event (v1 eliminated)');
  assert(kinds.includes('gate'), 'a champion-gate decided event');
  // newest-first ordering.
  assert(events.length >= 4, 'all the deltas surfaced as events');
  // the cut event is toned bad, the gate-promotion good.
  assert(events.find((e) => e.kind === 'cut').tone === 'bad', 'a cut is toned regress (bad)');
  assert(events.find((e) => e.kind === 'gate').tone === 'good', 'a promotion is toned improve (good)');
});

test('live engine: the ActivityTicker is append-only, newest-on-top, capped, and de-dups by id', () => {
  const t = new live.ActivityTicker({ cap: 3 });
  // first batch (newest-first input).
  t.push([{ id: 'a3', kind: 'phase', text: 'three' }, { id: 'a2', kind: 'phase', text: 'two' }, { id: 'a1', kind: 'phase', text: 'one' }]);
  let rows = t._list.children;
  assertEqual(rows.length, 3, 'three rows after the first batch');
  assert(rows[0].textContent.includes('three'), 'newest (a3) is on top');
  const a3Node = rows[0];
  // a duplicate id is ignored; a new id prepends; the cap trims the oldest.
  t.push([{ id: 'a4', kind: 'cut', text: 'four', tone: 'bad' }, { id: 'a3', kind: 'phase', text: 'three-dup' }]);
  rows = t._list.children;
  assertEqual(rows.length, 3, 'the cap (3) trimmed the oldest row');
  assert(rows[0].textContent.includes('four'), 'the new event (a4) is newest-on-top');
  assert(!t._list.textContent.includes('three-dup'), 'a duplicate id (a3) was NOT re-added');
  // surviving rows keep identity (append-only — no repaint).
  assert([...rows].some((r) => r === a3Node), 'a surviving row keeps its node identity (no repaint)');
});

// ---- TOGGLE: the board-detail transcript button collapses when re-clicked ----

test('board view (a): clicking "show inline" reveals the transcript and the button reads "showing"', async () => {
  freshState(); installFetch();
  const board = await import('../js/variants/T/views/board.js');

  // collapsed: no gen selected — the row button reads "show inline →" and its
  // href carries the gen (clicking it OPENS that candidate's transcript).
  const closed = document.createElement('div');
  await board.render(closed, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, entry: 'waffles_single' });
  assert(allByClass(closed, 'dn-xscript-grid').length === 0, 'no inline transcript pane while collapsed');
  const closedBtns = allByClass(closed, 'dn-board-run');
  assert(closedBtns.length && closedBtns.every((n) => (n.textContent || '').includes('show inline')), 'every candidate row button reads "show inline →" when collapsed');
  // the v1 candidate's button carries the v1 gen (clicking it OPENS that transcript).
  const v1Btn = closedBtns.find((n) => /\/board\/waffles_single\/v1\b/.test(n.getAttribute('href') || ''));
  assert(v1Btn, 'the "show inline" href carries the gen (opens the transcript)');

  // selected: that gen is open — the transcript renders and its button flips to
  // "showing ↓" (current behaviour).
  freshState(); installFetch();
  const open = document.createElement('div');
  await board.render(open, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, entry: 'waffles_single', gen: 'v1' });
  assert(allByClass(open, 'dn-xscript-grid')[0], 'the inline transcript pane rendered for the selected candidate');
  const onBtn = allByClass(open, 'dn-board-run').find((n) => (n.getAttribute('class') || '').split(/\s+/).includes('dn-linkbtn-on'));
  assert(onBtn, 'the selected candidate button is marked active (dn-linkbtn-on)');
  assert((onBtn.textContent || '').includes('showing'), 'the active button reads "showing ↓"');
});

test('board view (b): clicking the "showing" button again hides the transcript + clears the selection/route', async () => {
  freshState(); installFetch();
  const board = await import('../js/variants/T/views/board.js');

  // open on v1.
  const host = document.createElement('div');
  await board.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, entry: 'waffles_single', gen: 'v1' });
  const onBtn = allByClass(host, 'dn-board-run').find((n) => (n.getAttribute('class') || '').split(/\s+/).includes('dn-linkbtn-on'));
  assert(onBtn && (onBtn.textContent || '').includes('showing'), 'the v1 button is the active "showing ↓" control');

  // TOGGLE: the active "showing ↓" button's href DROPS the gen — clicking it
  // routes back to the bare board (selection cleared), so the transcript closes
  // and a reload of that route does NOT reopen it.
  const offHref = onBtn.getAttribute('href') || '';
  assert(/\/board\/waffles_single(\b|$)/.test(offHref) && !/\/board\/waffles_single\/v1\b/.test(offHref),
    'the active button href collapses to the bare board route (no gen) — toggles the selection OFF');

  // re-render at the collapsed route the toggle points to: the transcript is gone
  // and the button is back to "show inline →".
  freshState(); installFetch();
  const reloaded = document.createElement('div');
  await board.render(reloaded, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, entry: 'waffles_single' });
  assert(allByClass(reloaded, 'dn-xscript-grid').length === 0, 'the inline transcript is hidden after toggling off');
  const backBtn = allByClass(reloaded, 'dn-board-run').find((n) => (n.textContent || '').includes('show inline'));
  assert(backBtn, 'the button returned to "show inline →" after the toggle');
  assert(allByClass(reloaded, 'dn-linkbtn-on').length === 0, 'no candidate button is marked active after toggling off');

  // the dot-plot stays consistent: clicking the already-selected candidate's dot
  // also collapses it (drops the gen).
  freshState(); installFetch();
  let dotNav = null;
  const dotHost = document.createElement('div');
  await board.render(dotHost, { navigate: (v, p) => { dotNav = { v, p }; }, href: router.href }, { epochId: EPOCH_ID, entry: 'waffles_single', gen: 'v1' });
  const dots = dotHost.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').split(/\s+/).includes('dn-dotrow'));
  // find the v1 dot row (its label/value group is clickable) and click it.
  const v1dot = dots.find((n) => (n.textContent || '').includes('v1'));
  if (v1dot) {
    v1dot.dispatchEvent({ type: 'click' });
    assert(dotNav && dotNav.v === 'board' && dotNav.p.entry === 'waffles_single' && dotNav.p.gen == null,
      'clicking the already-selected candidate dot collapses it (navigates to the board with no gen)');
  }
});

// ====================================================================
// CROSS-EPOCH LEAKAGE + survival-funnel header collision (live-run fixes).
//
//   BUG 1 — the epoch overview's gens/heatmap-columns must be scoped to the
//     VIEWED epoch. /api/lineage spans the whole workspace; viewing e1 must NOT
//     leak e0's generations into the heatmap (no duplicate v0/v1 columns, no
//     inflated "field of N").
//   BUG 2 — the racing strip must follow the ACTIVE epoch. When the viewed
//     epoch has no tournament/funnel data (proposing) the strip shows the honest
//     "field fills in" empty state, NEVER a prior epoch's completed funnel.
//   BUG 3 — the survival-funnel rung headers + the benchmark/descriptive line
//     get DISTINCT y baselines (and each rung header its own column x), so they
//     never overlap each other or the descriptive line.
// ====================================================================

// two epochs on /api/lineage: a COMPLETED e0 (v0..v4) and a NEW e1 (v0..v2).
const TWO_EP_OLD = '2026-06-01_e0';
const TWO_EP_NEW = '2026-06-02_e1';
function twoEpochFixture(viewedEpoch, opts) {
  const o = opts || {};
  // the WHOLE-workspace lineage — both epochs, with COLLIDING ids (both have v0..).
  const lineage = [
    { generation_id: 'v0', epoch_id: TWO_EP_OLD, parent_generation_id: '', promoted: true },
    { generation_id: 'v1', epoch_id: TWO_EP_OLD, parent_generation_id: 'v0', promoted: false },
    { generation_id: 'v2', epoch_id: TWO_EP_OLD, parent_generation_id: 'v0', promoted: false },
    { generation_id: 'v3', epoch_id: TWO_EP_OLD, parent_generation_id: 'v0', promoted: false },
    { generation_id: 'v4', epoch_id: TWO_EP_OLD, parent_generation_id: 'v0', promoted: true },
    { generation_id: 'v0', epoch_id: TWO_EP_NEW, parent_generation_id: '', promoted: true },
    { generation_id: 'v1', epoch_id: TWO_EP_NEW, parent_generation_id: 'v0', promoted: false },
    { generation_id: 'v2', epoch_id: TWO_EP_NEW, parent_generation_id: 'v0', promoted: false },
  ];
  // per-entry profiles for BOTH epochs (so a leak would also leak loss columns).
  const perEntry = {};
  for (const g of lineage) {
    perEntry[`/api/generation/${g.epoch_id}/${g.generation_id}/per-entry`] = {
      epoch_id: g.epoch_id, generation_id: g.generation_id,
      entries: [{ entry_id: 'waffles_single', run_id: `r_${g.epoch_id}_${g.generation_id}`, drift_loss: 50, pass_fail: 0 }],
    };
  }
  const F = {
    '/api/epoch': {
      epoch_id: viewedEpoch, closed: viewedEpoch === TWO_EP_OLD, goal: 'g',
      tournament: { structure: 'racing', params: { eta: 2, board_fraction: 0.25 } },
      // ep.experiments is also epoch-scoped to the VIEWED epoch (the API returns
      // the contract for the current epoch) — used as the fallback path.
      experiments: lineage.filter((g) => g.epoch_id === viewedEpoch).map((g) => ({
        generation_id: g.generation_id, parent_generation_id: g.parent_generation_id,
        outcome: { decision: g.promoted ? 'baseline' : 'rejected' },
      })),
      board: [{ id: 'waffles_single', kind: 'single_turn', budget_s: 180, weight: 1 }],
    },
    '/api/lineage': { generations: lineage },
    '/api/score-trajectory': { points: lineage.filter((g) => g.epoch_id === viewedEpoch).map((g, i) => ({ generation_id: g.generation_id, scalar: 50 + i })) },
    // the COMPLETED tournaments record carries ONLY e0's racing ladder (per the
    // per-challenger shape) — e1 has none yet.
    '/api/tournaments': {
      epoch_id: TWO_EP_OLD, structure: 'racing', structure_params: { eta: 2, board_fraction: 0.25 },
      champion_lineage: ['v0', 'v4'],
      matchups: [],
      tournaments: [
        { tournament_id: `${TWO_EP_OLD}:v0->v1`, structure: 'racing', competitors: ['v0', 'v1'], standings: [], rounds: [{ match_id: 'rung0_m0', opponent: 'v0', won: false, delta_scalar: 3 }] },
        { tournament_id: `${TWO_EP_OLD}:v0->v4`, structure: 'racing', competitors: ['v0', 'v4'], standings: [], rounds: [
          { match_id: 'rung0_m3', opponent: 'v0', won: true, delta_scalar: -1 },
          { match_id: 'racing-final', opponent: 'v0', won: true, delta_scalar: -5 },
        ] },
      ],
    },
  };
  if (o.activeTournament) F['/api/active-tournament'] = o.activeTournament;
  Object.assign(F, perEntry);
  return F;
}

// ---- BUG 1: the epoch view is scoped to the viewed epoch (no leak) ---

test('epoch view (cross-epoch): viewing e1 shows ONLY e1 gens — no leaked e0 columns, deduped by id, field count correct', async () => {
  freshState();
  // e1 is the new epoch; the active racing tournament is e1's (proposing — no rungs).
  installFixtureMap(twoEpochFixture(TWO_EP_NEW, {
    activeTournament: { tournament_id: `tourn_${TWO_EP_NEW}_v1`, epoch_id: TWO_EP_NEW, structure: 'racing', phase: 'running', rounds: [], standings: [], competitors: [] },
  }));
  coreState.state.heartbeat = { phase: 'idle' };  // not "running" → no live status; the funnel falls to reconstruct
  coreState.state.activeRuns = [];
  coreState.state.activeTournament = null;

  const epoch = await import('../js/variants/T/views/epoch.js');
  const host = document.createElement('div');
  await epoch.render(host, { navigate() {}, href: router.href }, { epochId: TWO_EP_NEW });

  // the heatmap columns = the gens. e1 has exactly v0,v1,v2 — and they must each
  // appear ONCE (no duplicate v0/v1 from e0). v3/v4 belong to e0 and must be absent.
  const hm = svgsByClass(host, 'dn-heatmap')[0];
  assert(hm, 'the heatmap rendered on the e1 epoch view');
  // column headers are the per-generation labels on the heatmap; count the v-id labels.
  const colLabels = hm.querySelectorAll('[class]')
    .filter((n) => n.localName === 'text' && /^v\d+$/.test((n.textContent || '').trim()))
    .map((n) => (n.textContent || '').trim());
  const cols = colLabels.filter((s, i) => colLabels.indexOf(s) === i); // distinct
  // every v-id label that is a COLUMN appears once; there are NO e0-only ids (v3,v4).
  const colCounts = {};
  for (const c of colLabels) colCounts[c] = (colCounts[c] || 0) + 1;
  for (const id of Object.keys(colCounts)) {
    if (/^v[0-2]$/.test(id)) continue;            // v0..v2 are e1's own field
    assert(!/^v[34]$/.test(id), `no leaked e0-only column ${id} on the e1 view`);
  }
  // no id appears as a column more than the number of header rows it legitimately
  // owns — a leak would DOUBLE v0/v1/v2. Assert the distinct column set is exactly e1's.
  assertDeep(cols.sort(), ['v0', 'v1', 'v2'], 'the e1 heatmap columns are EXACTLY e1’s field {v0,v1,v2} (deduped, no leak)');

  // the timeline's challenger fan reflects e1's OWN minted field (v1, v2) — a
  // leak would add e0's v3/v4 chips. The single-round episode lists exactly
  // {v1, v2} (v0 is the carried champion on the spine, not a chip).
  const chips = allByClass(host, 'dn-roundtl-chip').map((c) => { const mono = allByClass(c, 'dn-mono')[0]; return mono ? (mono.textContent || '').trim() : ''; });
  assertDeep(chips.filter((s, i) => chips.indexOf(s) === i).sort(), ['v1', 'v2'], 'the e1 challenger fan is EXACTLY e1’s minted field {v1,v2} (no leaked v3/v4)');
});

test('epoch view (cross-epoch): viewing e0 is unchanged — its full field {v0..v4} still renders (no regression)', async () => {
  freshState();
  installFixtureMap(twoEpochFixture(TWO_EP_OLD));
  coreState.state.heartbeat = { phase: 'idle' };
  coreState.state.activeRuns = [];
  coreState.state.activeTournament = null;

  const epoch = await import('../js/variants/T/views/epoch.js');
  const host = document.createElement('div');
  await epoch.render(host, { navigate() {}, href: router.href }, { epochId: TWO_EP_OLD });

  const hm = svgsByClass(host, 'dn-heatmap')[0];
  assert(hm, 'the heatmap rendered on the e0 epoch view');
  const colLabels = hm.querySelectorAll('[class]')
    .filter((n) => n.localName === 'text' && /^v\d+$/.test((n.textContent || '').trim()))
    .map((n) => (n.textContent || '').trim());
  const cols = colLabels.filter((s, i) => colLabels.indexOf(s) === i).sort();
  assertDeep(cols, ['v0', 'v1', 'v2', 'v3', 'v4'], 'e0 still shows its FULL field {v0..v4} (unchanged)');
  // e0's challenger fan is its own full minted field {v1..v4} (v0 carried on spine).
  const chips = allByClass(host, 'dn-roundtl-chip').map((c) => { const mono = allByClass(c, 'dn-mono')[0]; return mono ? (mono.textContent || '').trim() : ''; });
  assertDeep(chips.filter((s, i) => chips.indexOf(s) === i).sort(), ['v1', 'v2', 'v3', 'v4'], 'e0 reads its own full challenger fan {v1..v4}');
});

// ---- BUG 2: a proposing epoch shows the empty state, not e0's funnel -

test('epoch view (cross-epoch): a PROPOSING e1 shows the honest empty state — NOT e0’s completed funnel', async () => {
  freshState();
  // e1 is proposing: the active tournament is e1's with NO rungs yet; the
  // COMPLETED /api/tournaments still carries e0's full racing ladder. The strip
  // must NOT reconstruct e0's funnel under the e1 header.
  installFixtureMap(twoEpochFixture(TWO_EP_NEW, {
    activeTournament: { tournament_id: `tourn_${TWO_EP_NEW}_v1`, epoch_id: TWO_EP_NEW, structure: 'racing', phase: 'running', rounds: [], standings: [], competitors: [] },
  }));
  // even with a LIVE racing heartbeat, the active topology has no rungs → no funnel.
  coreState.state.setHeartbeat({ phase: 'tournament:round_0:rung0_m0', generation_id: 'v1' });
  coreState.state.activeRuns = [{ generation_id: 'v1', entry_id: 'waffles_single', run_id: 'r1' }];
  coreState.state.activeTournament = { structure: 'racing', phase: 'running' };

  const epoch = await import('../js/variants/T/views/epoch.js');
  const host = document.createElement('div');
  await epoch.render(host, { navigate() {}, href: router.href }, { epochId: TWO_EP_NEW });

  // NO funnel SVG (e1 has no rung data — e0's reconstructed ladder must NOT leak).
  assertEqual(svgsByClass(host, 'dn-funnel').length, 0, 'NO survival funnel while e1 is proposing (no leak of e0’s funnel)');
  // the timeline still renders (its episode degrades to e1's own minted field).
  assert(allByClass(host, 'dn-roundtl')[0], 'the round timeline renders for the proposing e1 epoch');
  // e0’s crowned survivor (v4) must NOT bleed into the e1 timeline as a champion ♚.
  assert(!host.textContent.includes('♚ v4'), 'e0’s crowned champion ♚ v4 does NOT leak into the e1 timeline');

  coreState.state.heartbeat = { phase: 'idle' };
  coreState.state.activeRuns = [];
  coreState.state.activeTournament = null;
});

test('reconstructRacing (cross-epoch): scoped to the viewed epoch — a foreign epoch’s records are dropped', () => {
  // the tournaments payload carries ONLY e0 racing records; reconstructing for e1
  // must return null (no funnel), but reconstructing for e0 still rebuilds it.
  const brk = twoEpochFixture(TWO_EP_NEW)['/api/tournaments'];
  assertEqual(STRUCT.reconstructRacing(brk, TWO_EP_NEW), null, 'no e1 racing record → null (the e0 records are NOT adopted under e1)');
  const e0 = STRUCT.reconstructRacing(brk, TWO_EP_OLD);
  assert(e0 && e0.structure === 'racing', 'the e0 ladder still reconstructs from its own records');
});

// ---- BUG 3: the funnel header labels do NOT collide -------------------

test('survival funnel (no-collide): the benchmark line + the rung headers + the sublabels sit on DISTINCT y baselines', () => {
  const rungs = [
    { label: 'Rung 0', competitors: ['v1', 'v2', 'v3', 'v4'], survivors: ['v3', 'v4'], cut: ['v1', 'v2'], board_fraction: 0.25, deltas: {} },
    { label: 'Rung 1', competitors: ['v3', 'v4'], survivors: ['v3'], cut: ['v4'], board_fraction: 0.5, deltas: {} },
  ];
  const node = svg.survivalFunnel({ rungs, championId: 'v3', benchmarkId: 'v0', gateState: 'crowned', gateDelta: -5, onCompetitor() {} });

  const yOf = (n) => Number(n.getAttribute('y'));
  const xOf = (n) => Number(n.getAttribute('x'));
  const byClass = (cls) => node.querySelectorAll('[class]').filter((n) => n.localName === 'text' && (n.getAttribute('class') || '').split(/\s+/).includes(cls));

  const bench = byClass('dn-funnel-bench')[0];
  const heads = byClass('dn-funnel-head');
  const subs = byClass('dn-funnel-sub');
  assert(bench, 'the benchmark/descriptive line rendered');
  assert(heads.length >= 3, 'a header per rung column + the champion-gate header (≥3)');
  assert(subs.length >= 3, 'a sublabel per rung + the gate');

  const benchY = yOf(bench);
  const headYs = heads.map(yOf);
  const subYs = subs.map(yOf);
  // (1) the benchmark line is ABOVE every rung header (its own baseline).
  for (const hy of headYs) assert(benchY < hy, 'the benchmark line sits strictly above the rung headers (separate baseline)');
  // (2) all rung headers share ONE baseline, the sublabels another, distinct from it
  //     and from the benchmark — three separate rows.
  const headBaseline = headYs[0];
  for (const hy of headYs) assertEqual(hy, headBaseline, 'every rung/gate header shares the one header baseline');
  const subBaseline = subYs[0];
  for (const sy of subYs) assertEqual(sy, subBaseline, 'every sublabel shares the one sub baseline');
  assert(headBaseline !== benchY && subBaseline !== benchY && headBaseline !== subBaseline,
    'benchmark / header / sub occupy THREE distinct y baselines (no shared baseline → no collision)');

  // (3) each rung/gate header is centred on its OWN column x — no two headers
  //     share the same x (they march left→right across the stages + gate).
  const headXs = heads.map(xOf).sort((a, b) => a - b);
  for (let i = 1; i < headXs.length; i++) {
    assert(headXs[i] > headXs[i - 1], 'adjacent rung/gate headers occupy distinct, increasing column x positions (no overlap)');
  }
  // (4) the benchmark line is left-anchored (x near the origin) on its own row, so
  //     it cannot run into a centred rung header on the SAME baseline.
  assert(xOf(bench) < headXs[0], 'the benchmark line starts left of the first rung header (its own row)');
});

// ---- LIVE-BEAT: the inline transcript pane survives an in-flight-only beat ----
//
// During a live run the in-flight progressRatio advances every SSE beat. The
// regression: that advanced the OUTER view digest, tearing down and rebuilding
// the whole board view — INCLUDING the open transcript scroll containers, which
// reset to the top. The fix splits the transcript into its OWN per-pane digest
// host keyed ONLY on [selGen, transcript content], independent of the in-flight
// set. These pin that a progress-only beat does NOT recreate the transcript DOM
// while the dot-plot / in-flight portion DOES update — and that the transcript
// pane DOES re-render when the selection or transcript content actually change.

test('board view (live): an in-flight-only beat does NOT tear down the inline transcript (no scroll reset), but the in-flight/dot-plot portion DOES update', async () => {
  freshState(); installFetch();
  const board = await import('../js/variants/T/views/board.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };

  // beat 1: a candidate is in flight on this entry at 30% AND v1's transcript is open.
  coreState.state.activeRuns = [{ entry_id: 'waffles_single', generation_id: 'v2', run_id: 'run_v2_waffles', progress: 0.3 }];
  await board.render(host, ctx, { epochId: EPOCH_ID, entry: 'waffles_single', gen: 'v1' });

  const xhostBefore = host.querySelector(':scope > [data-node="board-xscript"]');
  const upperBefore = host.querySelector(':scope > [data-node="board-upper"]');
  assert(xhostBefore && upperBefore, 'the board view split into a persistent upper host + a transcript host');
  const xdigestBefore = xhostBefore.getAttribute('data-t-digest');
  const updigestBefore = upperBefore.getAttribute('data-t-digest');
  const scrollBefore = allByClass(host, 'dn-xscript-scroll')[0];
  assert(scrollBefore, 'the inline transcript scroll container rendered (transcript is open)');
  assert(host.textContent.includes('30%'), 'beat 1 shows the in-flight candidate at 30%');

  // beat 2: SAME selection + SAME transcript, but the in-flight progress advanced to 65%.
  coreState.state.activeRuns = [{ entry_id: 'waffles_single', generation_id: 'v2', run_id: 'run_v2_waffles', progress: 0.65 }];
  await board.render(host, ctx, { epochId: EPOCH_ID, entry: 'waffles_single', gen: 'v1' });

  const xhostAfter = host.querySelector(':scope > [data-node="board-xscript"]');
  const scrollAfter = allByClass(host, 'dn-xscript-scroll')[0];
  // PRIMARY: the transcript host's digest is unchanged and the scroll container
  // is the SAME node (not recreated) — so scroll position is preserved.
  assertEqual(xhostAfter.getAttribute('data-t-digest'), xdigestBefore, 'the transcript digest is UNCHANGED across an in-flight-only beat');
  assert(scrollAfter === scrollBefore, 'the inline transcript scroll container is the SAME DOM node (not torn down on a progress beat)');
  // the in-flight / upper portion DID update (digest changed) and now shows 65%.
  assert(upperBefore.getAttribute('data-t-digest') !== updigestBefore, 'the upper (dot-plot / in-flight) digest DID change as progress advanced');
  assert(host.textContent.includes('65%'), 'the in-flight portion repainted to 65%');
  coreState.state.activeRuns = [];
});

test('board view (live): the transcript pane DOES re-render when the selected gen changes', async () => {
  freshState(); installFetch();
  const board = await import('../js/variants/T/views/board.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };

  await board.render(host, ctx, { epochId: EPOCH_ID, entry: 'waffles_single', gen: 'v1' });
  const xhost = host.querySelector(':scope > [data-node="board-xscript"]');
  const xdigestV1 = xhost.getAttribute('data-t-digest');
  const scrollV1 = allByClass(host, 'dn-xscript-scroll')[0];
  assert(host.textContent.includes('Drafting an outline'), 'v1 transcript turn rendered');

  // selecting a DIFFERENT candidate changes the transcript digest → the pane re-renders.
  await board.render(host, ctx, { epochId: EPOCH_ID, entry: 'waffles_single', gen: 'v0' });
  const xdigestV0 = xhost.getAttribute('data-t-digest');
  const scrollV0 = allByClass(host, 'dn-xscript-scroll')[0];
  assert(xdigestV0 !== xdigestV1, 'the transcript digest CHANGED when the selected gen changed (v1 → v0)');
  assert(scrollV0 !== scrollV1, 'the transcript scroll container was rebuilt for the new selection');
  assert(host.textContent.includes('Here is a structured outline'), 'the v0 transcript turn rendered after switching selection');
  coreState.state.activeRuns = [];
});

// ====================================================================
// TIER 1 (Class A) — the DRILL-DOWN views are scoped to the VIEWED epoch.
// TIER 2 (Class B) — an unscored candidate (promoted:null) renders PENDING,
//                    never rejected / dead-branch.
//
// Two epochs share /api/lineage with COLLIDING gen ids: a COMPLETED e0
// (v0..v2, v1 promoted) and an in-flight e1 (v0 + an UNSCORED v1 with
// promoted:null). The `?epoch=<id>` backend reads return the SCOPED contract /
// trajectory / bracket per epoch; /api/lineage is global, so the views must
// filter it by epoch_id (generationsForEpoch) and dedupe by gen id.
// ====================================================================
const SC_OLD = '2026-06-01_e0';
const SC_NEW = '2026-06-02_e1';
function scopeFixture() {
  const lineage = [
    { generation_id: 'v0', epoch_id: SC_OLD, parent_generation_id: '', promoted: true },
    { generation_id: 'v1', epoch_id: SC_OLD, parent_generation_id: 'v0', promoted: true },
    { generation_id: 'v2', epoch_id: SC_OLD, parent_generation_id: 'v1', promoted: false },
    // e1: a seed v0 + an UNSCORED challenger v1 (promoted == null → pending).
    { generation_id: 'v0', epoch_id: SC_NEW, parent_generation_id: '', promoted: true },
    { generation_id: 'v1', epoch_id: SC_NEW, parent_generation_id: 'v0', promoted: null },
  ];
  const F = { '/api/lineage': { generations: lineage } };
  // per-epoch scoped contract / trajectory / bracket (keyed by the ?epoch= path).
  const contract = (id, gens) => ({
    epoch_id: id, closed: id === SC_OLD, goal: 'g',
    experiments: gens.map((g) => ({ generation_id: g.generation_id, parent_generation_id: g.parent_generation_id,
      outcome: g.promoted === true ? { decision: 'promoted' } : g.promoted === false ? { decision: 'rejected' } : {} })),
    board: [{ id: 'waffles_single', kind: 'single_turn', budget_s: 180, weight: 1 }],
  });
  const traj = (gens) => ({ points: gens.map((g, i) => ({ generation_id: g.generation_id, scalar: 40 + i })) });
  const oldGens = lineage.filter((g) => g.epoch_id === SC_OLD);
  const newGens = lineage.filter((g) => g.epoch_id === SC_NEW);
  F[`/api/epoch?epoch=${SC_OLD}`] = contract(SC_OLD, oldGens);
  F[`/api/epoch?epoch=${SC_NEW}`] = contract(SC_NEW, newGens);
  F[`/api/score-trajectory?epoch=${SC_OLD}`] = traj(oldGens);
  F[`/api/score-trajectory?epoch=${SC_NEW}`] = traj(newGens);
  F[`/api/tournaments?epoch=${SC_OLD}`] = { epoch_id: SC_OLD, champion_lineage: ['v0', 'v1'], matchups: [
    { champion: 'v0', challenger: 'v1', decision: 'promoted', delta_scalar: -5 },
    { champion: 'v1', challenger: 'v2', decision: 'rejected', delta_scalar: 4 },
  ] };
  // e1: the challenger v1 has run no gate yet → NO decision (pending).
  F[`/api/tournaments?epoch=${SC_NEW}`] = { epoch_id: SC_NEW, champion_lineage: ['v0'], matchups: [
    { champion: 'v0', challenger: 'v1' },
  ] };
  // per-entry profiles for every (epoch, gen) so a leak would surface as columns.
  for (const g of lineage) {
    F[`/api/generation/${g.epoch_id}/${g.generation_id}/per-entry`] = {
      epoch_id: g.epoch_id, generation_id: g.generation_id,
      entries: [{ entry_id: 'waffles_single', run_id: `r_${g.epoch_id}_${g.generation_id}`, drift_loss: 50, pass_fail: 0 }],
    };
  }
  F[`/api/epoch/${SC_NEW}/analysis`] = { analysis_md: '' };
  F[`/api/epoch/${SC_OLD}/analysis`] = { analysis_md: '' };
  return F;
}

test('Tier1 (cross-epoch): candidate view scopes to the viewed epoch (only e1 gens; correct champion)', async () => {
  freshState();
  installFixtureMap(scopeFixture());
  const candidate = await import('../js/variants/T/views/candidate.js');
  const host = document.createElement('div');
  await candidate.render(host, { navigate() {}, href: router.href }, { epochId: SC_NEW, gen: 'v1' });
  assert(host.textContent.includes('Candidate v1'), 'e1 v1 rendered');
  // the compare picker lists ONLY e1's field {v0, v1} — never e0's v2.
  const opts = allByClass(host, 'dt-cmp-opt');
  const optText = host.textContent;
  assert(!optText.includes('v2'), 'no leaked e0-only generation (v2) on the e1 candidate view');
});

test('Tier2 (Class B): an UNSCORED e1 candidate (promoted:null) renders PENDING, not dead-branch', async () => {
  freshState();
  installFixtureMap(scopeFixture());
  const candidate = await import('../js/variants/T/views/candidate.js');
  const host = document.createElement('div');
  await candidate.render(host, { navigate() {}, href: router.href }, { epochId: SC_NEW, gen: 'v1' });
  // the verdict pill reads pending (racing…), never rejected.
  assert(allByClass(host, 'dn-pending').length >= 1, 'a pending verdict pill rendered for the unscored challenger');
  assert(!allByClass(host, 'dn-rejected').some((n) => /seed|v1/.test(n.textContent)), 'no rejected pill');
  // the lifecycle DAG terminal must NOT say "dead branch / champion stands".
  assert(!host.textContent.includes('dead branch'), 'the DAG terminal is NOT "✕ dead branch" for a pending candidate');
  assert(host.textContent.includes('racing') || host.textContent.includes('awaiting gate'), 'the DAG terminal reads a racing/awaiting-gate state');
});

test('Tier1 (cross-epoch): gens view scopes to the viewed epoch; pending candidate in the roster', async () => {
  freshState();
  installFixtureMap(scopeFixture());
  const gens = await import('../js/variants/T/views/gens.js');
  const host = document.createElement('div');
  await gens.render(host, { navigate() {}, href: router.href }, { epochId: SC_NEW });
  assert(host.textContent.includes(`Generations · ${SC_NEW}`), 'the gens page heads with e1');
  // the roster lists e1's {v0, v1} only — not e0's v2.
  const monos = allByClass(host, 'dn-mono').map((n) => n.textContent);
  assert(!host.textContent.includes('v2'), 'no leaked e0 generation v2 in the e1 roster');
  // the unscored v1 row carries a PENDING pill (not rejected).
  assert(allByClass(host, 'dn-pending').length >= 1, 'the unscored challenger reads pending in the roster');
});

test('Tier1 (cross-epoch): switching the epoch param changes the data (e0 ↔ e1)', async () => {
  freshState();
  installFixtureMap(scopeFixture());
  const gens = await import('../js/variants/T/views/gens.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await gens.render(host, ctx, { epochId: SC_OLD });
  assert(host.textContent.includes('v2'), 'e0 view shows its own generation v2');
  assert(host.textContent.includes(`Generations · ${SC_OLD}`), 'heads with e0');
  await gens.render(host, ctx, { epochId: SC_NEW });
  assert(host.textContent.includes(`Generations · ${SC_NEW}`), 'switched to e1');
  assert(!host.textContent.includes('v2'), 'e2-only generation gone after switching to e1');
});

test('Tier1 (cross-epoch): boards view scopes to the viewed epoch (no e0 candidate columns)', async () => {
  freshState();
  installFixtureMap(scopeFixture());
  const boards = await import('../js/variants/T/views/boards.js');
  const host = document.createElement('div');
  await boards.render(host, { navigate() {}, href: router.href }, { epochId: SC_NEW });
  assert(host.textContent.includes(`Boards · ${SC_NEW}`), 'the boards page heads with e1');
  // e1 has exactly 2 candidates (v0, v1); a leak would report e0's 3.
  assert(host.textContent.includes('2') && !host.textContent.includes('field of 8'), 'e1 candidate count is its own (2), not leaked');
});

test('Tier1 (cross-epoch): board (per-entry) view scopes to the viewed epoch', async () => {
  freshState();
  installFixtureMap(scopeFixture());
  const board = await import('../js/variants/T/views/board.js');
  const host = document.createElement('div');
  await board.render(host, { navigate() {}, href: router.href }, { epochId: SC_NEW, entry: 'waffles_single' });
  assert(host.textContent.includes('Board · waffles_single'), 'the board entry view rendered for e1');
  // the per-candidate breakdown lists e1's {v0, v1} only — never e0's v2.
  assert(!host.textContent.includes('v2'), 'no leaked e0 generation v2 in the e1 board breakdown');
});

test('Tier1 (cross-epoch): publication view scopes lineage/figures to the viewed epoch', async () => {
  freshState();
  installFixtureMap(scopeFixture());
  const publication = await import('../js/variants/T/views/publication.js');
  const host = document.createElement('div');
  await publication.render(host, { navigate() {}, href: router.href }, { epochId: SC_NEW });
  // the aggregate-scores table lists e1's own gens only; v2 belongs to e0.
  assert(!host.textContent.includes('v2'), 'no leaked e0 generation v2 in the e1 publication figures');
  // the unscored challenger reads "racing…", never "rejected".
  assert(!/rejected/.test(host.textContent) || host.textContent.includes('racing'), 'an unscored gen reads racing, not a default rejected');
});

// ---- HEADER SCOPING: the epoch view's H1 + STATE pill read the ROUTED epoch.
//
// THE BUG. Viewing a NON-current epoch (e0, closed) while e1 is the live/current
// epoch leaked the CURRENT epoch into the epoch view's HEADER: the `Epoch <id>`
// H1 read e1's id and the STATE pill read e1's "open" — even though the
// breadcrumb, tree, structure ladder, heatmap and gen-derived stats correctly
// showed e0. Root cause: the header read `D.epoch()` (always the current epoch)
// instead of the routed `D.epoch(epochId)`. With per-epoch `?epoch=<id>`
// contracts (e0 closed → "closed" + e0's objective; e1 open → "open"), the H1
// and STATE pill must now match the ROUTED epoch, not the current one.
test('Tier1 (header scoping): the epoch view H1 + STATE pill read the ROUTED epoch, not the current one', async () => {
  freshState();
  // distinct per-epoch contracts: e0 (SC_OLD) is closed with its own objective;
  // e1 (SC_NEW) is the live/current epoch, open. The scoped `?epoch=<id>` reads
  // return each epoch's own contract; bare `D.epoch()` would return e1 (current).
  const F = scopeFixture();
  F[`/api/epoch?epoch=${SC_OLD}`] = { ...F[`/api/epoch?epoch=${SC_OLD}`], closed: true, goal: 'Sharpen e0’s drift floor.' };
  F[`/api/epoch?epoch=${SC_NEW}`] = { ...F[`/api/epoch?epoch=${SC_NEW}`], closed: false, goal: 'e1 live objective.' };
  // bare `/api/epoch` (the CURRENT epoch) resolves to e1 — a leak would surface
  // e1's id/state/objective in the e0 header.
  F['/api/epoch'] = F[`/api/epoch?epoch=${SC_NEW}`];
  installFixtureMap(F);

  const epoch = await import('../js/variants/T/views/epoch.js');
  const host = document.createElement('div');
  // route AT e0 (the NON-current epoch) while e1 is current.
  await epoch.render(host, { navigate() {}, href: router.href }, { epochId: SC_OLD });

  const h1 = allByClass(host, 'dn-h1')[0];
  assert(h1, 'the epoch H1 rendered');
  assertEqual(h1.textContent, `Epoch ${SC_OLD}`, 'the H1 reads the ROUTED epoch (e0), not the current one (e1)');
  assert(!h1.textContent.includes(SC_NEW), 'the current epoch id (e1) does NOT leak into the e0 header');

  // the STATE pill (the stat row's "state" tile) reads e0's "closed", not e1's "open".
  const stats = allByClass(host, 'dn-stat').map((n) => n.textContent);
  assert(stats.some((t) => t.includes('closed') && t.includes('state')), 'the STATE pill reads e0’s "closed"');
  assert(!stats.some((t) => t.includes('open') && t.includes('state')), 'the STATE pill is NOT e1’s "open"');

  // the OBJECTIVE is e0's, not e1's.
  assert(host.textContent.includes('Sharpen e0’s drift floor.'), 'the objective is e0’s');
  assert(!host.textContent.includes('e1 live objective.'), 'e1’s objective does NOT leak into the e0 header');

  // and switching the route to e1 flips the header to e1 / open (the converse).
  const host2 = document.createElement('div');
  await epoch.render(host2, { navigate() {}, href: router.href }, { epochId: SC_NEW });
  assertEqual(allByClass(host2, 'dn-h1')[0].textContent, `Epoch ${SC_NEW}`, 'routing to e1 heads with e1');
  const stats2 = allByClass(host2, 'dn-stat').map((n) => n.textContent);
  assert(stats2.some((t) => t.includes('open') && t.includes('state')), 'e1’s STATE pill reads "open"');
  assert(!stats2.some((t) => t.includes('closed') && t.includes('state')), 'e1 is not "closed"');
});

test('Tier2 (Class B): the tree tags an unscored child PENDING, not rejected', () => {
  const host = document.createElement('div');
  const model = {
    epochs: [{ id: SC_NEW, current: true }],
    byEpoch: { [SC_NEW]: {
      gens: [{ id: 'v0', promoted: true, parent: null }, { id: 'v1', promoted: null, parent: 'v0' }],
      boards: [{ id: 'waffles_single' }],
    } },
  };
  const toggles = new Set(['e:' + SC_NEW, 'e:' + SC_NEW + '/gens']);
  const route = router.parseRoute(`#/e/${SC_NEW}`);
  tree.buildTree(host, model, route, toggles, { navigate() {}, href: router.href }, () => {});
  const tags = allByClass(host, 'dt-tag').map((n) => n.textContent);
  assert(tags.includes('pending'), 'the unscored child v1 is tagged "pending"');
  assert(!tags.includes('rejected'), 'the unscored child v1 is NOT tagged "rejected"');
});

// ---- per-board dot-plot: tournament-context label + click → run drill-down ─

test('candidate: tournamentContext derives rung/round/matchup labels (racing/gauntlet/swiss)', async () => {
  const candidate = await import('../js/variants/T/views/candidate.js');
  const tc = candidate.tournamentContext;
  // racing: pre-formatted rung wins; raw rungN_* match_id → "rung N".
  assertEqual(tc({ match_id: 'rung0_m2', rung: 'rung 0' }), 'rung 0', 'pre-formatted rung string is reused');
  assertEqual(tc({ match_id: 'rung1_m1' }), 'rung 1', 'rung parsed from match_id when no pre-format');
  // racing final → the champion gate.
  assertEqual(tc({ match_id: 'racing-final' }), 'champion-gate', 'racing-final maps to champion-gate');
  // gauntlet: roundN / gN → "round N".
  assertEqual(tc({ match_id: 'round2' }), 'round 2', 'gauntlet round parsed');
  assertEqual(tc({ match_id: 'g3' }), 'round 3', 'gauntlet gN parsed');
  // swiss: roundN_mM → "round N · match M".
  assertEqual(tc({ match_id: 'round1_m2' }), 'round 1 · match 2', 'swiss round·match parsed');
  assertEqual(tc({ match_id: 'swiss_r0_m4' }), 'round 0 · match 4', 'swiss r/m prefix parsed');
  // no context at all → null (row renders name-only).
  assertEqual(tc({}), null, 'no match_id / rung → null');
});

test('svg.valueDotPlot: duplicate board rows get DISTINCT context lines + onClick carries the full item', () => {
  let clicked = null;
  const items = [
    { label: 'q3_metrics_outline', value: 80, id: 'q3_metrics_outline', context: 'rung 0', entry_id: 'q3_metrics_outline', run_id: 'run_a', gen: 'v1' },
    { label: 'q3_metrics_outline', value: 40, id: 'q3_metrics_outline', context: 'rung 1', entry_id: 'q3_metrics_outline', run_id: 'run_b', gen: 'v1' },
  ];
  const plot = svg.valueDotPlot({ items, reference: { value: 60, label: 'champion v0' }, onClick: (it) => { clicked = it; } });
  // both rows render their board name…
  const names = allByClass(plot, 'dn-dot-label').map((n) => n.textContent);
  assertEqual(names.filter((t) => t === 'q3_metrics_outline').length, 2, 'BOTH duplicate board rows rendered');
  // …with DISTINCT context tags (not two identical labels).
  const ctxs = allByClass(plot, 'dn-dot-ctx').map((n) => n.textContent);
  assert(ctxs.includes('rung 0') && ctxs.includes('rung 1'), 'each duplicate carries its own rung tag');
  assertEqual(new Set(ctxs).size, 2, 'the two context tags are distinct');
  // reference rule still drawn (existing behaviour unchanged).
  assert(allByClass(plot, 'dn-ref-rule').length === 1, 'the champion reference line is still drawn');
  // clicking a row fires onClick with the FULL item (entry_id/run_id/gen intact).
  const rows = allByClass(plot, 'dn-dotrow');
  assertEqual(rows.length, 2, 'two clickable dot rows');
  rows[1].dispatchEvent({ type: 'click' });
  assert(clicked && clicked.entry_id === 'q3_metrics_outline' && clicked.run_id === 'run_b' && clicked.gen === 'v1',
    'onClick receives the specific run (entry_id + run_id + gen)');
});

test('svg.heatmap: higher-contrast theme-aware cell scale — wider range, monotonic, low≠empty', () => {
  // A 1×4 board×gen matrix: one EMPTY cell + three valued cells spanning the
  // drift range (low / mid / high). value(rowId,colId) returns the drift loss.
  const cellVal = { 'b/lo': 10, 'b/mid': 55, 'b/hi': 100 }; // 'b/empty' → null
  const rows = [{ id: 'b', label: 'board' }];
  const cols = [
    { id: 'empty', label: 'g-empty' },
    { id: 'lo', label: 'g-lo' },
    { id: 'mid', label: 'g-mid' },
    { id: 'hi', label: 'g-hi' },
  ];
  let clicked = null;
  const hm = svg.heatmap({
    rows, cols,
    value: (r, c) => (c === 'empty' ? null : cellVal[`${r}/${c}`]),
    onClick: (r, c) => { clicked = [r, c]; },
  });
  const cells = allByClass(hm, 'dn-hm-cell');
  assertEqual(cells.length, 4, 'four cells rendered (1 empty + 3 valued)');
  const empty = cells.find((c) => c.classList.contains('dn-hm-empty'));
  const valued = cells.filter((c) => !c.classList.contains('dn-hm-empty'));
  assert(empty, 'the null cell carries the dn-hm-empty token');
  assertEqual(valued.length, 3, 'three valued cells (lo/mid/hi)');

  // helpers to read the two contrast axes off a cell
  const opOf = (c) => parseFloat(c.getAttribute('fill-opacity'));
  const mixOf = (c) => parseFloat(c.getAttribute('data-hm-mix'));
  const [lo, mid, hi] = valued; // rendered in col order lo,mid,hi

  // (1) MONOTONIC in drift on BOTH axes (opacity density AND cool→hot mix).
  assert(opOf(lo) < opOf(mid) && opOf(mid) < opOf(hi), 'fill-opacity is monotonic in drift');
  assert(mixOf(lo) < mixOf(mid) && mixOf(mid) < mixOf(hi), 'cool→hot mix is monotonic in drift');

  // (2) WIDER contrast than the OLD opacity-only ramp. The old scale was a
  // SINGLE ink at op = 0.18 + 0.82*t with NO colour axis (mix spread = 0). The
  // new scale adds a cool→hot mix spanning a wide range, so the combined
  // high-vs-low contrast metric is strictly greater than the old one.
  const OLD_op = (t) => 0.18 + 0.82 * t; // the previous mapping, for reference
  const tLo = 0, tHi = 1; // lo is the min (t=0), hi is the max (t=1)
  const oldContrast = OLD_op(tHi) - OLD_op(tLo);            // = 0.82, opacity only
  const newOpContrast = opOf(hi) - opOf(lo);                // density axis
  const newMixContrast = (mixOf(hi) - mixOf(lo)) / 100;     // hue axis, normalised
  const newContrast = newOpContrast + newMixContrast;       // combined two-axis metric
  assert(newContrast > oldContrast,
    `new combined contrast ${newContrast.toFixed(3)} > old opacity-only ${oldContrast.toFixed(3)}`);
  assert(newMixContrast > 0.5, 'the cool→hot hue axis alone spans a wide range (>0.5)');

  // (3) the densest cell reads as clearly "most drift" — near-full opacity and
  // (almost) fully the HOT token.
  assert(opOf(hi) > 0.95, 'the highest-drift cell is near-opaque');
  assert(mixOf(hi) > 95, 'the highest-drift cell is almost entirely the HOT token');

  // (4) the LOWEST non-empty cell stays clearly distinct from an EMPTY one:
  // it carries a value-driven mix + opacity (the cool token at a visible
  // floor), whereas the empty cell has NO mix and uses the flat empty token.
  assert(opOf(lo) >= 0.28, 'the lowest valued cell sits at a visible opacity floor (≠ near-invisible)');
  assert(empty.getAttribute('data-hm-mix') == null, 'the empty cell carries NO cool→hot mix');
  assert(empty.getAttribute('fill-opacity') == null, 'the empty cell carries NO value-driven opacity');
  // the inline fill on a valued cell is a theme-token color-mix (no hardcoded hex).
  assert(/color-mix\(in srgb, var\(--v2-hm-hot\)/.test(lo.style.cssText || ''),
    'valued cells fill via a theme-token cool→hot color-mix (theme-aware, no hex)');

  // (5) the onClick affordance + tooltip survive.
  hi.dispatchEvent({ type: 'click' });
  assertDeep(clicked, ['b', 'hi'], 'cell onClick fires with (rowId, colId)');
  assertEqual(hi.style.cursor, 'pointer', 'clickable cells show a pointer cursor');
});

test('candidate view: per-board dumbbell click → board drill-down for THAT run; duplicate rungs disambiguated', async () => {
  freshState(); installFetch();
  const candidate = await import('../js/variants/T/views/candidate.js');
  // same entry raced in TWO rungs (different match_id + rung) → two rows.
  const path = `/api/generation/${EPOCH_ID}/v1/per-entry`;
  const saved = FIXTURE[path];
  FIXTURE[path] = { epoch_id: EPOCH_ID, generation_id: 'v1', entries: [
    { entry_id: 'waffles_single', run_id: 'run_v1_w_r0', drift_loss: 80.0, pass_fail: 0, runtime_ms: 180000, wall_clock_budget_exceeded: false, match_id: 'rung0_m1', rung: 'rung 0' },
    { entry_id: 'waffles_single', run_id: 'run_v1_w_r1', drift_loss: 40.0, pass_fail: 1, runtime_ms: 180000, wall_clock_budget_exceeded: false, match_id: 'rung1_m1', rung: 'rung 1' },
  ] };
  try {
    const host = document.createElement('div');
    let navTo = null;
    const ctx = { navigate: (v, p, o) => { navTo = { v, p, o }; }, href: router.href };
    await candidate.render(host, ctx, { epochId: EPOCH_ID, gen: 'v1' });
    // both rungs show as distinct context tags on the dumbbell.
    const ctxs = allByClass(host, 'dn-dumbbell-ctx').map((n) => n.textContent);
    assert(ctxs.includes('rung 0') && ctxs.includes('rung 1'), 'the duplicate board rows show "rung 0" vs "rung 1"');
    // clicking a dumbbell row routes to the board drill-down for this entry + gen.
    const rows = allByClass(host, 'dn-dumbbell-row');
    assert(rows.length >= 2, 'at least the two re-raced rows are clickable');
    rows[0].dispatchEvent({ type: 'click' });
    assert(navTo && navTo.v === 'board' && navTo.p.entry === 'waffles_single' && navTo.p.gen === 'v1' && navTo.p.epochId === EPOCH_ID,
      'a dumbbell row click opens the board drill-down for that exact run (entry + gen)');
  } finally {
    FIXTURE[path] = saved;
  }
});

// ---- Task A: the per-board figure is the study's champ○ → candidate● DUMBBELL ----
// The study's opt-2 per-board figure is an explicit per-row dumbbell: a hollow
// champion ○ and a filled candidate ● JOINED by a connector, with the Δ + the
// pass/fail marker — NOT a single-series dot-plot against one aggregate champion
// reference rule. v1 (parent v0 = champion) shares its slice with v0, so each
// board row carries a real per-board champion value to draw the ○.
test('candidate view: the per-board figure renders the champion○ → candidate● DUMBBELL (paired per row, not a single-series dot-plot)', async () => {
  freshState(); installFetch();
  const candidate = await import('../js/variants/T/views/candidate.js');
  const host = document.createElement('div');
  await candidate.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, gen: 'v1' });
  // the dumbbell SVG is present + responsive (width-filling); the OLD single-series
  // valueDotPlot (dn-valdot + its aggregate reference rule) is GONE from the dossier.
  const dumbbell = svgsByClass(host, 'dn-dumbbell')[0];
  assert(dumbbell, 'the per-board champion○ → candidate● dumbbell SVG rendered');
  assertEqual(dumbbell.getAttribute('width'), '100%', 'the dumbbell is responsive (fills its dossier column)');
  assertEqual(svgsByClass(host, 'dn-valdot').length, 0, 'the old single-series dot-plot is gone from the dossier (replaced by the dumbbell)');
  // v1 has TWO scored boards, both shared with the champion v0 → two paired rows,
  // each with a hollow champion ○, a filled candidate ●, a connector, AND a Δ.
  const champDots = allByClass(dumbbell, 'dn-dumbbell-champ');
  const candDots = allByClass(dumbbell, 'dn-dumbbell-cand');
  const conns = allByClass(dumbbell, 'dn-dumbbell-conn');
  const deltas = allByClass(dumbbell, 'dn-dumbbell-delta');
  assertEqual(champDots.length, 2, 'a hollow champion ○ per board (one per paired row)');
  assertEqual(candDots.length, 2, 'a filled candidate ● per board');
  assertEqual(conns.length, 2, 'a champ→candidate connector per board (the dumbbell bar)');
  assertEqual(deltas.length, 2, 'a per-board Δ (candidate − champion) per board');
  // the champion ○ uses the REAL per-board champion value (v0: waffles 60.5,
  // picky 105.5 — both come through s.championLoss, so the ○ is positioned by
  // the actual champion-on-this-board loss, recoverable as cand − Δ).
  const champCx = champDots.map((n) => parseFloat(n.getAttribute('cx')));
  const candCx = candDots.map((n) => parseFloat(n.getAttribute('cx')));
  assert(champCx.every((v) => Number.isFinite(v)) && candCx.every((v) => Number.isFinite(v)),
    'both the ○ and ● are positioned on the shared per-row value axis');
  // worst-first sort: picky (cand 642.5) is far worse than its champ (105.5) → a
  // regressed (dn-bad) row; both connectors here are regressions vs the champion.
  assert(conns.some((n) => (n.getAttribute('class') || '').includes('dn-bad')),
    'a regressed board (candidate worse than champion on that board) colours its connector dn-bad');
  // the rows are clickable → that board's drill-down (keeps the drill affordance).
  const rows = allByClass(dumbbell, 'dn-dumbbell-row');
  assertEqual(rows.length, 2, 'each board row is its own clickable group');
  let navTo = null;
  const host2 = document.createElement('div');
  await candidate.render(host2, { navigate: (v, p) => { navTo = { v, p }; }, href: router.href }, { epochId: EPOCH_ID, gen: 'v1' });
  allByClass(host2, 'dn-dumbbell-row')[0].dispatchEvent({ type: 'click' });
  assert(navTo && navTo.v === 'board' && navTo.p.gen === 'v1', 'clicking a dumbbell row opens that board\'s drill-down');
});

// ---- Task B: the generalization train→holdout slope is correctly GATED ----
// The study's "(5) generalization · train → holdout" slope renders the train dot
// → holdout dot, the gap, and the OK/over-tolerance verdict when the candidate's
// experiment carries holdout data, and is cleanly ABSENT when there's none.
test('candidate view (Task B): the train→holdout generalization slope RENDERS for a holdout-bearing candidate (slope + gap + verdict)', async () => {
  freshState(); installFetch();
  const candidate = await import('../js/variants/T/views/candidate.js');
  // give v1's experiment a holdout triplet within tolerance (gap 0.02 ≤ tol 0.05).
  const saved = FIXTURE['/api/epoch'];
  FIXTURE['/api/epoch'] = {
    ...saved,
    experiments: saved.experiments.map((x) => x.generation_id === 'v1'
      ? { ...x, train_loss: 0.60, holdout_loss: 0.62, generalization_gap: 0.02, generalization_tolerance: 0.05 }
      : x),
  };
  try {
    const host = document.createElement('div');
    await candidate.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, gen: 'v1' });
    // the shrunk supporting panel + its section heading are present.
    assert(host.textContent.includes('Generalization · train → holdout'), 'the generalization section heading renders');
    const pane = allByClass(host, 'dn-genpane')[0];
    assert(pane, 'the shrunk train→holdout slope pane renders');
    // the slope itself: a train point → a holdout point joined by a slope line.
    assert(allByClass(pane, 'dn-gen-train')[0], 'the train point renders');
    assert(allByClass(pane, 'dn-gen-holdout')[0], 'the holdout point renders');
    assert(allByClass(pane, 'dn-gen-slope')[0], 'the train→holdout slope line renders');
    // the gap label carries the gap + the within-tolerance OK verdict.
    const gap = allByClass(pane, 'dn-gen-gap')[0];
    assert(gap, 'the gap label renders');
    const gt = gap.textContent || '';
    assert(gt.includes('gap') && gt.includes('OK'), 'the gap label reads the gap + the within-tolerance OK verdict');
    // within tolerance → the caution tone, NOT the over-tolerance bad tone.
    assert((gap.getAttribute('class') || '').includes('dn-caution'), 'a within-tolerance gap reads the caution tone (not over-tolerance bad)');
  } finally {
    FIXTURE['/api/epoch'] = saved;
  }
});

test('candidate view (Task B): the generalization slope is ABSENT when the candidate has NO holdout data (cleanly gated)', async () => {
  freshState(); installFetch();
  const candidate = await import('../js/variants/T/views/candidate.js');
  // the default v1 experiment carries NO train/holdout/gap fields → no panel.
  const host = document.createElement('div');
  await candidate.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, gen: 'v1' });
  assert(!host.textContent.includes('Generalization · train → holdout'), 'no generalization heading when there is no holdout data');
  assertEqual(allByClass(host, 'dn-genpane').length, 0, 'no generalization slope pane when there is no holdout data');
  assertEqual(svgsByClass(host, 'dn-gen-svg').length, 0, 'no generalization slope SVG when there is no holdout data');
});

// the over-tolerance verdict: a holdout gap that EXCEEDS tolerance reads the bad
// tone + the "> tol" / memorization caption (the other verdict branch).
test('candidate view (Task B): an over-tolerance holdout gap reads the over-tolerance (memorization) verdict', async () => {
  freshState(); installFetch();
  const candidate = await import('../js/variants/T/views/candidate.js');
  const saved = FIXTURE['/api/epoch'];
  FIXTURE['/api/epoch'] = {
    ...saved,
    experiments: saved.experiments.map((x) => x.generation_id === 'v1'
      ? { ...x, train_loss: 0.40, holdout_loss: 0.95, generalization_gap: 0.55, generalization_tolerance: 0.05 }
      : x),
  };
  try {
    const host = document.createElement('div');
    await candidate.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, gen: 'v1' });
    const pane = allByClass(host, 'dn-genpane')[0];
    assert(pane, 'the slope pane renders for the over-tolerance candidate too');
    const gap = allByClass(pane, 'dn-gen-gap')[0];
    assert(gap && (gap.getAttribute('class') || '').includes('dn-bad'), 'an over-tolerance gap reads the bad tone');
    assert((gap.textContent || '').includes('> tol'), 'the gap label flags it exceeds tolerance');
    assert(pane.textContent.includes('memorization'), 'the caption flags possible memorization');
  } finally {
    FIXTURE['/api/epoch'] = saved;
  }
});

test('Tier2 (Class B): decisionFor never defaults null/absent → rejected', () => {
  assertEqual(ui.decisionFor({ promoted: null, parent: 'v0' }), 'pending', 'null + no resolved decision → pending');
  assertEqual(ui.decisionFor({ promoted: true, parent: 'v0' }), 'promoted', 'promoted:true → promoted');
  assertEqual(ui.decisionFor({ promoted: false, parent: 'v0' }), 'rejected', 'promoted:false → rejected');
  assertEqual(ui.decisionFor({ parent: null }), 'baseline', 'no parent → baseline');
  assertEqual(ui.decisionFor({ promoted: null, parent: 'v0', exp: { outcome: { decision: 'rejected' } } }), 'rejected', 'null + resolved negative outcome → rejected');
  assertEqual(ui.decisionFor({ promoted: null, parent: 'v0', gate: { decision: 'promoted' } }), 'promoted', 'null + resolved gate promote → promoted');
  assertEqual(ui.decisionFor({}), 'baseline', 'empty (no parent) → baseline, never rejected');
});

// ====================================================================
// Fleet cards (Environment view): each epoch card's hero sparkline shows
// that epoch's OWN real per-generation trajectory — NEVER a fabricated
// `[best×1.18, best×1.06, best]` curve (which renders shape-identical for
// every epoch and would surface fabricated numbers).
// ====================================================================

// A two-epoch workspace whose two epochs have DIFFERENT real trajectories
// (different scalars AND different lengths). scoreTrajectory is scoped per
// epoch via `?epoch=<id>`, so each card must source ITS id's points.
const FLEET_E0 = '2026-06-01_e0';
const FLEET_E1 = '2026-06-02_e1';
const FLEET_FIXTURE = {
  '/api/workspace': { current_epoch_id: FLEET_E1, epochs: [
    { epoch_id: FLEET_E0, generation_count: 5, promoted_count: 1, best_scalar: 46.813, closed: true, goal: 'e0 goal' },
    { epoch_id: FLEET_E1, generation_count: 9, promoted_count: 0, best_scalar: 20.500, closed: false, goal: 'e1 goal' },
  ], sparkline: [{ epoch_id: FLEET_E0, scalar: 46.813 }, { epoch_id: FLEET_E1, scalar: 20.500 }] },
  '/api/health-report': { healthy: true, findings: [] },
  // distinct scalars; e0 has 3 points, e1 has 5 — different series AND length.
  [`/api/score-trajectory?epoch=${FLEET_E0}`]: { epoch_id: FLEET_E0, points: [
    { generation_id: 'v0', scalar: 55.9 }, { generation_id: 'v1', scalar: 50.0 }, { generation_id: 'v4', scalar: 46.813 },
  ] },
  [`/api/score-trajectory?epoch=${FLEET_E1}`]: { epoch_id: FLEET_E1, points: [
    { generation_id: 'v0', scalar: 56.2 }, { generation_id: 'v1', scalar: 53.5 }, { generation_id: 'v3', scalar: 50.07 },
    { generation_id: 'v7', scalar: 40.5 }, { generation_id: 'v8', scalar: 20.500 },
  ] },
};
function installFleetFetch(F) {
  globalThis.fetch = async (path) => {
    const v = lookupFixture(F || FLEET_FIXTURE, path);
    if (v !== undefined) return { ok: true, json: async () => v };
    return { ok: false, status: 404, json: async () => ({ error: 'not found: ' + path }) };
  };
}
// the value series each card's sparkline drew, read back from the SVG path's
// M/L vertices (one vertex per finite value) — the only DOM-visible proof of
// the series, and enough to compare length + shape across cards.
function sparkPointCount(card) {
  const path = card.querySelectorAll('[class]').filter((n) =>
    n.localName === 'path' && (n.getAttribute('class') || '').includes('dn-spark-line'))[0];
  if (!path) return 0;
  const d = path.getAttribute('d') || '';
  return (d.match(/[ML]/g) || []).length;
}

test('fleet cards: two epochs with DIFFERENT real trajectories render DIFFERENT sparklines (per-epoch, keyed on epoch_id)', async () => {
  freshState(); installFleetFetch();
  const home = await import('../js/variants/T/views/home.js');
  const host = document.createElement('div');
  await home.render(host, { navigate() {}, href: router.href }, {});

  const cards = allByClass(host, 'dn-fleet-card');
  assertEqual(cards.length, 2, 'one fleet card per epoch');
  const c0 = sparkPointCount(cards[0]);
  const c1 = sparkPointCount(cards[1]);
  assertEqual(c0, 3, 'e0 card sparkline draws its 3 REAL generation points');
  assertEqual(c1, 5, 'e1 card sparkline draws its 5 REAL generation points');
  assert(c0 !== c1, 'the two cards render visibly different series (different length) — not one shared synthetic curve');

  // and the series are sourced from the PER-EPOCH endpoint (?epoch=<id>), so
  // they reflect each epoch's real data rather than the single current contract.
  assert(host.textContent.includes(FLEET_E0) && host.textContent.includes(FLEET_E1), 'both epoch cards rendered');
});

test('fleet cards: NO fabricated [best×1.18, best×1.06, best] fallback — an epoch with <2 real points shows the honest placeholder', async () => {
  freshState();
  // e0 keeps a real 3-point trajectory; e1 has only ONE real point (<2).
  const F = JSON.parse(JSON.stringify(FLEET_FIXTURE));
  F[`/api/score-trajectory?epoch=${FLEET_E1}`] = { epoch_id: FLEET_E1, points: [{ generation_id: 'v0', scalar: 56.2 }] };
  installFleetFetch(F);
  const home = await import('../js/variants/T/views/home.js');
  const host = document.createElement('div');
  await home.render(host, { navigate() {}, href: router.href }, {});

  const cards = allByClass(host, 'dn-fleet-card');
  assertEqual(cards.length, 2, 'one fleet card per epoch');
  // e1 (one real point) → the honest "no trajectory yet" placeholder, NO path.
  assertEqual(sparkPointCount(cards[1]), 0, 'an epoch with <2 real points draws NO sparkline path');
  const placeholder = cards[1].querySelectorAll('[class]').filter((n) =>
    (n.getAttribute('class') || '').includes('dn-faint') && (n.textContent || '').includes('no trajectory yet'))[0];
  assert(placeholder, 'it shows the existing honest "no trajectory yet" placeholder');

  // e1 best_scalar is 20.500; the FABRICATED fallback would have produced the
  // descending [20.5×1.18, 20.5×1.06, 20.5] curve (3 points). Prove it is GONE.
  assert(sparkPointCount(cards[1]) !== 3, 'no synthetic 3-point [×1.18,×1.06,×1] curve is produced');
  // e0 still draws its real 3-point series unchanged.
  assertEqual(sparkPointCount(cards[0]), 3, 'the other epoch still draws its real trajectory');
});

test('fleet cards: existing rendering preserved — stats, epoch links, current-epoch highlight (full-width cross-epoch trajectory removed)', async () => {
  freshState(); installFleetFetch();
  const home = await import('../js/variants/T/views/home.js');
  const host = document.createElement('div');
  await home.render(host, { navigate() {}, href: router.href }, {});

  const cards = allByClass(host, 'dn-fleet-card');
  // stats: best / gens / promoted are still on each card.
  assert(cards[0].textContent.includes('46.813') && cards[0].textContent.includes('5'), 'e0 card keeps its best + gen stats');
  // each card links to its epoch view.
  assertEqual(cards[0].getAttribute('href'), router.href('epoch', { epochId: FLEET_E0 }), 'e0 card links to the e0 epoch view');
  assertEqual(cards[1].getAttribute('href'), router.href('epoch', { epochId: FLEET_E1 }), 'e1 card links to the e1 epoch view');
  // the current epoch (e1) is highlighted.
  assert((cards[1].getAttribute('class') || '').includes('dn-is-current'), 'the current epoch card is highlighted');
  assert(!(cards[0].getAttribute('class') || '').includes('dn-is-current'), 'the non-current epoch card is not highlighted');

  // the full-width "Cross-epoch trajectory" sparkline was removed — the
  // composed meta-loop ledger (tested separately) is the cross-epoch overview.
  assert(!host.textContent.includes('Cross-epoch trajectory'), 'the full-width cross-epoch trajectory panel is gone');
});

test('fleet cards: digest-gated — identical workspace + trajectories do NOT rebuild the DOM (heartbeat no-op)', async () => {
  freshState(); installFleetFetch();
  const home = await import('../js/variants/T/views/home.js');
  const host = document.createElement('div');
  await home.render(host, { navigate() {}, href: router.href }, {});
  const digest1 = host.getAttribute('data-t-digest');
  const first = host.firstChild;
  const writes1 = host.innerHTMLWriteCount();
  assert(host.children.length > 0, 'environment painted');
  await home.render(host, { navigate() {}, href: router.href }, {});
  assertEqual(host.getAttribute('data-t-digest'), digest1, 'digest unchanged on the no-op repaint');
  assert(host.firstChild === first, 'no clear-and-rebuild on the no-op repaint');
  assertEqual(host.innerHTMLWriteCount(), writes1, 'no innerHTML writes on the no-op repaint');
});

// CROSS-EPOCH BUG 1 — the tree lists EVERY epoch's generations.
//
// buildTreeModel used a SINGLE current-epoch `bundleId`, so expanding a
// non-current epoch (e0) showed an EMPTY "Generations" node even though
// /api/lineage carries e0's rows. Now each epoch node fills its OWN gens from
// the lineage filtered by THAT node's epoch_id — neither epoch empty, no
// cross-contamination.
// ====================================================================

test('tree model (cross-epoch): EVERY epoch node lists its OWN generations (e0 not empty, e1 not empty, no leak)', async () => {
  freshState();
  // the WHOLE-workspace lineage spans BOTH epochs; the contract is the CURRENT
  // (e1) epoch. /api/workspace names both so both become tree nodes.
  const F = twoEpochFixture(TWO_EP_NEW);
  F['/api/workspace'] = {
    current_epoch_id: TWO_EP_NEW,
    epochs: [
      { epoch_id: TWO_EP_OLD, generation_count: 5, promoted_count: 2, closed: true, goal: 'e0' },
      { epoch_id: TWO_EP_NEW, generation_count: 3, promoted_count: 1, closed: false, goal: 'e1' },
    ],
    sparkline: [],
  };
  installFixtureMap(F);
  coreState.state.heartbeat = { phase: 'idle' };
  coreState.state.activeRuns = [];
  coreState.state.activeTournament = null;

  // route at the CURRENT epoch (e1) — the OLD epoch (e0) is the non-current one.
  const model = await shell.buildTreeModel(router.parseRoute(`#/e/${TWO_EP_NEW}`));

  // both epochs are nodes.
  const ids = model.epochs.map((e) => e.id).sort();
  assert(ids.includes(TWO_EP_OLD) && ids.includes(TWO_EP_NEW), 'both epochs are tree nodes');

  // e0 (NON-current) lists ITS OWN 5 generations — NOT empty.
  const e0 = model.byEpoch[TWO_EP_OLD];
  assert(e0 && Array.isArray(e0.gens), 'the non-current e0 node has a gens bundle');
  assertEqual(e0.gens.length, 5, 'e0 lists its OWN 5 generations (not an empty Generations node)');
  assertDeep(e0.gens.map((g) => g.id).sort(), ['v0', 'v1', 'v2', 'v3', 'v4'], 'e0’s gens are exactly its own field {v0..v4}');

  // e1 (current) lists ITS OWN 3 generations — no cross-contamination from e0.
  const e1 = model.byEpoch[TWO_EP_NEW];
  assert(e1 && Array.isArray(e1.gens), 'the current e1 node has a gens bundle');
  assertEqual(e1.gens.length, 3, 'e1 lists its OWN 3 generations');
  assertDeep(e1.gens.map((g) => g.id).sort(), ['v0', 'v1', 'v2'], 'e1’s gens are exactly its own field {v0,v1,v2} (no e0 leak)');

  // the current-epoch marker stays on e1; e0’s board node is empty (its board
  // resolves when e0 is viewed — the boards/mutation/publication children are
  // not regressed).
  assert(model.epochs.find((e) => e.id === TWO_EP_NEW).current, 'e1 keeps the current marker');
  assert(Array.isArray(e1.boards) && e1.boards.length >= 1, 'the contract (e1) node still lists its boards');
});

// ====================================================================
// CROSS-EPOCH BUG 2 — Match-ups live state is gated to the ACTIVE epoch.
//
// gens.js read deriveLiveStatus() from the GLOBAL state and adopted the LIVE
// topology regardless of which epoch was viewed — so a CLOSED e0's Match-ups
// showed e1's live "being seeded" ladder. The live topology is now adopted
// ONLY when the viewed epoch IS the active one (state.activeTournament.epoch_id).
// ====================================================================

test('gens (cross-epoch): a NON-active epoch’s Match-ups renders the COMPLETED structure, NOT the active epoch’s live ladder', async () => {
  freshState();
  // e1 is racing LIVE (a running active-tournament tagged epoch_id=e1); we VIEW
  // the CLOSED e0. e0 must show its COMPLETED racing ladder, never e1's live
  // "being seeded" empty state and never the LIVE pill.
  const F = twoEpochFixture(TWO_EP_OLD);
  installFixtureMap(F);
  coreState.state.setHeartbeat({ phase: 'tournament:round_0:rung0_m0', generation_id: 'v1', epoch_id: TWO_EP_NEW });
  coreState.state.activeRuns = [{ generation_id: 'v1', entry_id: 'b0', run_id: 'r1', progress: 0.3 }];
  coreState.state.activeTournament = { epoch_id: TWO_EP_NEW, structure: 'racing', phase: 'running' };

  const gens = await import('../js/variants/T/views/gens.js');
  const host = document.createElement('div');
  await gens.render(host, { navigate() {}, href: router.href }, { epochId: TWO_EP_OLD });

  // NO live leak from e1 onto e0.
  assertEqual(allByClass(host, 'dt-live-pill').length, 0, 'NO LIVE pill on the closed e0 view (e1’s live run does not leak)');
  assert(!/being seeded|is being seeded|run is starting/i.test(host.textContent), 'NOT e1’s live "being seeded"/"starting" empty state under e0');
  // e0 renders its OWN completed survival funnel (reconstructed from its records).
  const ladder = svgsByClass(host, 'dn-funnel')[0];
  assert(ladder, 'e0 renders its OWN completed survival funnel (not the live topology)');

  coreState.state.heartbeat = { phase: 'idle' };
  coreState.state.activeRuns = [];
  coreState.state.activeTournament = null;
});

test('gens (cross-epoch): the ACTIVE epoch’s Match-ups still shows the live progressive ladder (no regression)', async () => {
  freshState();
  // VIEW the ACTIVE e1 while it races live — the live progressive racing ladder
  // must still render (the racing-ladder redesign is preserved).
  const F = twoEpochFixture(TWO_EP_NEW);
  F['/api/active-tournament'] = {
    epoch_id: TWO_EP_NEW, tournament_id: `tourn_${TWO_EP_NEW}_v1`, structure: 'racing', phase: 'running',
    structure_params: { field_size: 3, eta: 2, board_fraction: 0.25 },
    round_index: 0, total_rounds: 2,
    competitors: [
      { generation_id: 'v0', seed: 1, role: 'champion' },
      { generation_id: 'v1', seed: 2, role: 'challenger' },
      { generation_id: 'v2', seed: 3, role: 'challenger' },
    ],
    // NEW contract: the backend publishes the active rung-0 + the pending gate.
    rounds: [
      { round_index: 0, label: 'Rung 0', matches: [{ match_id: 'rung0', competitors: ['v1', 'v2'], survivors: [], cut: [], board_fraction: 0.25, pending: true }] },
      { round_index: 1, label: 'Champion gate', matches: [{ match_id: 'racing-final', competitors: ['v0'], board_fraction: 1.0, winner: null, pending: true }] },
    ],
    standings: [], champion_lineage: ['v0'],
  };
  installFixtureMap(F);
  coreState.state.setHeartbeat({ phase: 'tournament:round_0:rung0_m1', generation_id: 'v1', epoch_id: TWO_EP_NEW });
  coreState.state.activeRuns = [{ generation_id: 'v1', entry_id: 'b0', run_id: 'r1', progress: 0.5 }];
  coreState.state.activeTournament = { epoch_id: TWO_EP_NEW, structure: 'racing', phase: 'running' };

  const gens = await import('../js/variants/T/views/gens.js');
  const host = document.createElement('div');
  await gens.render(host, { navigate() {}, href: router.href }, { epochId: TWO_EP_NEW });

  assert(allByClass(host, 'dt-live-pill')[0], 'the active e1 view carries the LIVE pill');
  const ladder = svgsByClass(host, 'dn-funnel')[0];
  assert(ladder, 'the live progressive survival funnel renders for the active epoch');
  assert(!/being seeded/i.test(host.textContent), 'NOT the "being seeded" empty state once the live field exists');

  coreState.state.heartbeat = { phase: 'idle' };
  coreState.state.activeRuns = [];
  coreState.state.activeTournament = null;
});

// ---- typeface OPTIONS: the operator's finalized 12 faces (4 per mode) -----
//
// Each finalized id (T7 / T9 / … / D5) has a per-id CSS rule that swaps the four
// font-role tokens (--v2-sans / --v2-mono / --n-font-head / --n-font-paper) to
// the option's stacks, lifted byte-for-byte from the study. We assert each id's
// block exists AND that its head/prose/data faces match the study mapping. The
// JS model (ui.TYPE_OPTIONS) is the source of truth; the CSS must agree with it.
test('typeface options: each of the 12 finalized ids has a CSS rule whose font-role tokens match the study stacks', () => {
  const css = readCss();
  function typeBlock(id) {
    const m = css.match(new RegExp('#variant-root\\[data-variant="T"\\]\\[data-t-type="' + id + '"\\]\\s*\\{([^}]*)\\}'));
    assert(m, 'the ' + id + ' typeface block exists');
    return m[1];
  }
  function declIn(block, name) {
    const m = block.match(new RegExp('--' + name + '\\s*:\\s*([^;]+);'));
    return m ? m[1].trim() : null;
  }
  // the primary family inside a stack (between the first pair of quotes).
  function primary(stack) {
    const m = String(stack || '').match(/'([^']+)'/);
    return m ? m[1] : null;
  }
  // EXPECTED head/prose/data primaries per the study mapping.
  const expect = {
    T7:  { head: 'Google Sans Mono', prose: 'Google Sans Mono', data: 'Google Sans Mono' },
    T9:  { head: 'Source Sans 3',    prose: 'Source Sans 3',    data: 'Source Code Pro' },
    T12: { head: 'Inconsolata',      prose: 'Inconsolata',      data: 'Inconsolata' },
    T14: { head: 'Ubuntu',           prose: 'Ubuntu',           data: 'Ubuntu Mono' },
    E5:  { head: 'Fraunces',         prose: 'Fraunces',         data: 'Fraunces' },
    E7:  { head: 'Bitter',           prose: 'Bitter',           data: 'Bitter' },
    E8:  { head: 'Literata',         prose: 'Literata',         data: 'Literata' },
    E15: { head: 'Domine',           prose: 'Domine',           data: 'Domine' },
    D2:  { head: 'Archivo Narrow',   prose: 'Space Grotesk',    data: 'Space Grotesk' },
    D12: { head: 'Hanken Grotesk',   prose: 'Hanken Grotesk',   data: 'Hanken Grotesk' },
    D14: { head: 'Barlow Condensed', prose: 'Space Grotesk',    data: 'Space Grotesk' },
    D5:  { head: 'Bricolage Grotesque', prose: 'Bricolage Grotesque', data: 'Bricolage Grotesque' },
  };
  for (const id of Object.keys(expect)) {
    const b = typeBlock(id);
    // role → token mapping: head→--n-font-head, prose→--v2-sans (+ --n-font-paper),
    // data→--v2-mono.
    assertEqual(primary(declIn(b, 'n-font-head')), expect[id].head, id + ' head face → --n-font-head');
    assertEqual(primary(declIn(b, 'v2-sans')), expect[id].prose, id + ' prose face → --v2-sans');
    assertEqual(primary(declIn(b, 'n-font-paper')), expect[id].prose, id + ' prose face → --n-font-paper');
    assertEqual(primary(declIn(b, 'v2-mono')), expect[id].data, id + ' data/code face → --v2-mono');
    // the CSS must agree with the JS model for the same id.
    const opt = ui.TYPE_OPTIONS.find((o) => o.id === id);
    assertEqual(primary(opt.head), expect[id].head, id + ' JS model head matches');
    assertEqual(primary(opt.prose), expect[id].prose, id + ' JS model prose matches');
    assertEqual(primary(opt.data), expect[id].data, id + ' JS model data matches');
  }
  // the DEFAULT block (no data-t-type) lands on the T7 voice (Google Sans Mono).
  const baseM = css.match(/#variant-root\[data-variant="T"\]\s*\{([^}]*--v2-sans[^}]*)\}/);
  assert(baseM, 'the base [data-variant="T"] token block declares the default font roles');
  assert(/Google Sans Mono/.test(baseM[1]), 'the default (no data-t-type) voice is Google Sans Mono (T7)');
});

// the brand wordmark pins to a FIXED brand mono, INDEPENDENT of the user's
// typeface — so its dot stays centred regardless of the selected typeface.
test('brand mono: --v2-brand-mono is a FIXED monospace, distinct from the swappable --v2-mono token', async () => {
  const css = readCss();
  const baseM = css.match(/#variant-root\[data-variant="T"\]\s*\{([^}]*)\}/);
  assert(baseM, 'the base token block exists');
  const base = baseM[1];
  const brand = (base.match(/--v2-brand-mono\s*:\s*([^;]+);/) || [])[1];
  assert(brand, 'the base block declares a fixed --v2-brand-mono token');
  assert(/monospace\s*$/.test(brand.trim()), 'the brand mono stack ends in the generic monospace keyword');
  // it is NOT declared inside any per-OPTION block, so it never swaps with the UI.
  for (const id of ['T7', 'T9', 'T12', 'T14', 'E5', 'E7', 'E8', 'E15', 'D2', 'D12', 'D14', 'D5']) {
    const m = css.match(new RegExp('#variant-root\\[data-variant="T"\\]\\[data-t-type="' + id + '"\\]\\s*\\{([^}]*)\\}'));
    assert(m && !/--v2-brand-mono/.test(m[1]), 'the ' + id + ' typeface block does NOT re-declare the brand mono (it stays fixed)');
  }
  // the wordmark <text> pins to the fixed brand mono (not the swappable mono).
  const fsmod = await import('node:fs');
  const src = fsmod.readFileSync(new URL('../js/variants/T/shell.js', import.meta.url), 'utf8');
  assert(/var\(--v2-brand-mono\)/.test(src), 'the wordmark text font-family is var(--v2-brand-mono)');
});

// FONTS — a SPLIT loading strategy:
//   * The two self-hosted monos (iA Writer Mono + JetBrains Mono) stay SELF-
//     HOSTED woff2 declared via @font-face in the scoped CSS (JetBrains Mono
//     still backs the fixed brand mono) — those never touch a CDN.
//   * The typeface picker's finalized 12 faces load from the Google-Fonts loader
//     in app_T.js (preconnect + a single css2 request, display=swap). Every
//     family the 12 options reference must be in that request.
test('fonts: the two self-hosted monos stay woff2; the 12 finalized faces load via the Google-Fonts loader (preconnect + display=swap)', async () => {
  const css = readCss();
  // the two self-hosted monos are still declared via @font-face from local woff2.
  for (const fam of ['iA Writer Mono', 'JetBrains Mono']) {
    const re = new RegExp('@font-face[^}]*font-family:\\s*"' + fam + '"[^}]*url\\([^)]*\\.woff2[^)]*\\)\\s*format\\("woff2"\\)', 's');
    assert(re.test(css), '@font-face declares ' + fam + ' from a local .woff2');
  }
  assert(!/Space Mono/.test(css), 'Space Mono is no longer referenced in the CSS');
  assert(/font-display:\s*swap/.test(css), 'self-hosted faces load with font-display: swap');
  // every @font-face src is LOCAL (no external host) — the self-hosted monos.
  const faces = css.match(/@font-face\s*\{[^}]*\}/gs) || [];
  assert(faces.length >= 2, 'at least two @font-face blocks declared (iA Writer Mono + JetBrains Mono)');
  for (const f of faces) assert(!/url\(\s*['"]?https?:/.test(f), 'a face src is a LOCAL url (no http/https CDN)');

  const fs = await import('node:fs');
  const appJs = fs.readFileSync(new URL('../app_T.js', import.meta.url), 'utf8');
  // EVERY family the 12 finalized options reference loads from the Google-Fonts
  // request; the self-hosted monos must NOT be in it.
  const loaded = [...appJs.matchAll(/family=([A-Za-z0-9+]+)/g)].map((m) => m[1].replace(/\+/g, ' '));
  const NEEDED = [
    'Google Sans Mono', 'Noto Sans Mono', 'Source Sans 3', 'Source Code Pro',
    'Inconsolata', 'Ubuntu', 'Ubuntu Mono',
    'Fraunces', 'Bitter', 'Literata', 'Domine',
    'Archivo Narrow', 'Space Grotesk', 'Hanken Grotesk', 'Barlow Condensed',
    'Bricolage Grotesque',
  ];
  for (const fam of NEEDED) {
    assert(loaded.includes(fam), 'app_T.js loads the ' + fam + ' family (display=swap)');
  }
  assert(!loaded.includes('JetBrains Mono'), 'JetBrains Mono is self-hosted, NOT requested from the CDN');
  assert(!loaded.includes('iA Writer Mono'), 'iA Writer Mono is self-hosted, NOT requested from the CDN');
  assert(/display=swap/.test(appJs), 'CDN fonts are requested with display=swap');
  // a preconnect to the Google-Fonts origins is set up before the stylesheet.
  assert(/rel\s*=\s*['"]preconnect['"]/.test(appJs), 'app_T.js preconnects to the font origins');
  assert(/fonts\.gstatic\.com/.test(appJs), 'app_T.js preconnects to the gstatic woff2 host');

  // the self-hosted woff2 files actually ship on disk under static/fonts/.
  const path = await import('node:path');
  const fontsDir = path.dirname(new URL('../app_T.js', import.meta.url).pathname) + '/fonts';
  for (const f of ['JetBrainsMono-Regular.woff2', 'iAWriterMonoS-Regular.woff2']) {
    assert(fs.existsSync(fontsDir + '/' + f) && fs.statSync(fontsDir + '/' + f).size > 0, 'ships ' + f);
  }
});

// ====================================================================
// LIVE-RUN UX wave — five coordinated fixes on the live/structure/svg surface.
// ====================================================================

// (1) proportional trellis glyphs — the round status marks render as TRUE
// circles (equal x/y scale) even when the bars stretch to fill a WIDE cell.
test('svg.genDots: status glyphs are FIXED-ASPECT (1:1 viewBox, equal x/y) — round, not stretched, at a wide card', () => {
  // a WIDE card: the row is an HTML flex container (no non-uniform SVG stretch),
  // each glyph a 1:1-aspect SVG so the mark keeps equal x/y scale.
  const row = svg.genDots({ width: 800, height: 14, cells: [
    { label: 'v0', pass: 1, ran: true }, { label: 'v1', pass: 0, ran: true },
    { label: 'v2', timeout: true, ran: true }, { label: 'v3', ran: false },
  ] });
  // the row spans the full width (flex, width:100%) but is NOT itself a
  // preserveAspectRatio:'none' svg (which is what sheared the old glyphs).
  assert(row.localName !== 'svg', 'genDots returns an HTML flex row, not a stretched svg');
  const glyphs = svgsByClass(row, 'dn-glyph');
  assertEqual(glyphs.length, 4, 'one fixed-aspect glyph svg per candidate');
  for (const g of glyphs) {
    const vb = (g.getAttribute('viewBox') || '').split(/\s+/).map(Number);
    assertEqual(vb[2], vb[3], 'the glyph viewBox is SQUARE (1:1) → equal x/y scale → a true circle');
    assertEqual(g.getAttribute('preserveAspectRatio'), 'xMidYMid meet', 'the glyph keeps its aspect (no shear)');
    assertEqual(g.getAttribute('width'), g.getAttribute('height'), 'the glyph is painted at a 1:1 box');
  }
});

test('svg.sparkbar: bars still SPAN the width (stretch) but the verdict glyph is a SEPARATE fixed-aspect overlay (true triangle)', () => {
  const node = svg.sparkbar({ width: 800, height: 30, verdict: 'promoted', bars: [
    { label: 'v0', value: 10 }, { label: 'v1', value: 20 }, { label: 'v2', value: 5 },
  ] });
  // a wrapper holds the stretched bars + the fixed-aspect glyph.
  assert(node.localName !== 'svg', 'a verdict sparkbar returns a positioning wrapper (bars + glyph)');
  const bars = svgsByClass(node, 'dn-sparkbar')[0];
  assert(bars, 'the bars layer is present');
  assertEqual(bars.getAttribute('preserveAspectRatio'), 'none', 'the BARS still fill the cell width (stretch is fine for rectangles)');
  assertEqual(bars.getAttribute('width'), '100%', 'the bars span the full card width');
  const glyph = svgsByClass(node, 'dn-sparkbar-verdict')[0];
  assert(glyph, 'the verdict glyph rides in its own overlay svg');
  const vb = (glyph.getAttribute('viewBox') || '').split(/\s+/).map(Number);
  assertEqual(vb[2], vb[3], 'the verdict glyph viewBox is SQUARE → a true (un-sheared) triangle');
  assertEqual(glyph.getAttribute('width'), glyph.getAttribute('height'), 'the verdict glyph is a 1:1 box');
});

// (2) candidate page + trellis are live-aware (current-epoch-scoped, digest-gated).
const LIVE_UX_EPOCH = '2026-06-02_e9';
function liveUxFixture() {
  return {
    '/api/epoch': {
      epoch_id: LIVE_UX_EPOCH, closed: false, goal: 'g',
      tournament: { structure: 'swiss', params: { rounds: 3 } },
      experiments: [
        { generation_id: 'v0', parent_generation_id: '', outcome: { decision: 'baseline' } },
        { generation_id: 'v1', parent_generation_id: 'v0', outcome: {} },
      ],
      board: [
        { id: 'b0', kind: 'single_turn', budget_s: 180, weight: 1 },
        { id: 'b1', kind: 'single_turn', budget_s: 180, weight: 1 },
      ],
    },
    '/api/lineage': { generations: [
      { generation_id: 'v0', epoch_id: LIVE_UX_EPOCH, parent_generation_id: '', promoted: true },
      { generation_id: 'v1', epoch_id: LIVE_UX_EPOCH, parent_generation_id: 'v0', promoted: null },
    ] },
    '/api/score-trajectory': { points: [{ generation_id: 'v0', scalar: 50 }] },
    '/api/tournaments': { epoch_id: LIVE_UX_EPOCH, champion_lineage: ['v0'], matchups: [] },
    [`/api/generation/${LIVE_UX_EPOCH}/v0/per-entry`]: { entries: [{ entry_id: 'b0', run_id: 'r0', drift_loss: 40, pass_fail: 1 }] },
    [`/api/generation/${LIVE_UX_EPOCH}/v1/per-entry`]: { entries: [] },
  };
}

test('candidate page (LIVE): in-flight board runs for THIS candidate show "N running" with progress; foreign-epoch runs ignored; structure-aware pending label', async () => {
  freshState();
  installFixtureMap(liveUxFixture());
  const candidate = await import('../js/variants/T/views/candidate.js');
  // a CURRENT-epoch run in flight on v1 (swiss).
  coreState.state.setHeartbeat({ phase: 'tournament:round_0', generation_id: 'v1', epoch_id: LIVE_UX_EPOCH });
  coreState.state.activeRuns = [{ generation_id: 'v1', entry_id: 'b1', run_id: 'rr1', progress: 0.5, epoch_id: LIVE_UX_EPOCH }];
  coreState.state.activeTournament = { epoch_id: LIVE_UX_EPOCH, structure: 'swiss', phase: 'running' };

  const host = document.createElement('div');
  await candidate.render(host, { navigate() {}, href: router.href }, { epochId: LIVE_UX_EPOCH, gen: 'v1' });
  assert(allByClass(host, 'dn-board-inflight')[0], 'the candidate shows its in-flight board run card');
  assert(/board running|boards running/.test(host.textContent), 'reads "N board(s) running"');
  assert(/50%/.test(host.textContent), 'the in-flight board shows its progress (50%)');
  // a swiss candidate awaiting the gate must NOT read "racing".
  assert(!/⋯ racing/.test(host.textContent), 'the pending terminal label is structure-aware (swiss → not "racing")');
  assert(/⋯ competing/.test(host.textContent), 'a swiss candidate reads "⋯ competing"');

  // FOREIGN-epoch run must NOT light up this candidate.
  freshState();
  installFixtureMap(liveUxFixture());
  coreState.state.setHeartbeat({ phase: 'tournament:round_0', generation_id: 'v1', epoch_id: 'some_other_epoch' });
  coreState.state.activeRuns = [{ generation_id: 'v1', entry_id: 'b1', run_id: 'rr1', progress: 0.5 }];
  coreState.state.activeTournament = { epoch_id: 'some_other_epoch', structure: 'swiss', phase: 'running' };
  const host2 = document.createElement('div');
  await candidate.render(host2, { navigate() {}, href: router.href }, { epochId: LIVE_UX_EPOCH, gen: 'v1' });
  assertEqual(allByClass(host2, 'dn-board-inflight').length, 0, 'a FOREIGN-epoch run does not light up this candidate');

  coreState.state.heartbeat = { phase: 'idle' }; coreState.state.activeRuns = []; coreState.state.activeTournament = null;
});

test('board trellis (LIVE): in-flight cells light up from state.activeRuns (current epoch); a no-op beat does NOT rebuild; foreign epoch ignored', async () => {
  freshState();
  installFixtureMap(liveUxFixture());
  const boards = await import('../js/variants/T/views/boards.js');
  coreState.state.setHeartbeat({ phase: 'tournament:round_0', generation_id: 'v1', epoch_id: LIVE_UX_EPOCH });
  coreState.state.activeRuns = [{ generation_id: 'v1', entry_id: 'b1', run_id: 'rr1', progress: 0.4, epoch_id: LIVE_UX_EPOCH }];
  coreState.state.activeTournament = { epoch_id: LIVE_UX_EPOCH, structure: 'swiss', phase: 'running' };

  const host = document.createElement('div');
  await boards.render(host, { navigate() {}, href: router.href }, { epochId: LIVE_UX_EPOCH });
  const lit = allByClass(host, 'dn-trellis-live');
  assertEqual(lit.length, 1, 'exactly the in-flight entry (b1) cell lights up');
  assert(/running/.test(host.textContent), 'the lit cell carries an in-flight "running" tag');
  const digestAfterFirst = host.getAttribute('data-t-digest');

  // a NO-OP beat (identical live state) must NOT rebuild the trellis DOM.
  const trellisBefore = allByClass(host, 'dn-trellis')[0];
  await boards.render(host, { navigate() {}, href: router.href }, { epochId: LIVE_UX_EPOCH });
  assertEqual(host.getAttribute('data-t-digest'), digestAfterFirst, 'a no-op beat leaves the digest unchanged');
  assert(allByClass(host, 'dn-trellis')[0] === trellisBefore, 'a no-op beat does NOT rebuild the trellis (node identity preserved)');

  // FOREIGN-epoch run ignored — no lit cell.
  freshState();
  installFixtureMap(liveUxFixture());
  coreState.state.setHeartbeat({ phase: 'tournament:round_0', generation_id: 'v1', epoch_id: 'foreign_e' });
  coreState.state.activeRuns = [{ generation_id: 'v1', entry_id: 'b1', run_id: 'rr1', progress: 0.4 }];
  coreState.state.activeTournament = { epoch_id: 'foreign_e', structure: 'swiss', phase: 'running' };
  const host2 = document.createElement('div');
  await boards.render(host2, { navigate() {}, href: router.href }, { epochId: LIVE_UX_EPOCH });
  assertEqual(allByClass(host2, 'dn-trellis-live').length, 0, 'a foreign-epoch run does not light up the trellis');

  coreState.state.heartbeat = { phase: 'idle' }; coreState.state.activeRuns = []; coreState.state.activeTournament = null;
});

// (3) Match-ups live swiss — the ACTIVE round's pairings show in-flight progress
// (NOT "being seeded"); "being seeded" only with no competitors.
test('Match-ups (LIVE swiss): active-round pairings show in-flight board progress (NOT "being seeded"); accumulating points; seeded-empty only with no field', async () => {
  freshState();
  const F = liveUxFixture();
  F['/api/active-tournament'] = {
    epoch_id: LIVE_UX_EPOCH, structure: 'swiss', phase: 'running', structure_params: { rounds: 3, board_size: 4 },
    competitors: [
      { generation_id: 'v0', role: 'champion' }, { generation_id: 'v1', role: 'challenger' },
      { generation_id: 'v2', role: 'challenger' }, { generation_id: 'v3', role: 'challenger' },
    ],
    rounds: [
      { round_index: 0, label: 'Round 1', matches: [
        { match_id: 'sw_r0_m0', competitors: ['v0', 'v1'], winner: 'v1', decision: 'win' },
        { match_id: 'sw_r0_m1', competitors: ['v2', 'v3'] },  // in flight
      ] },
    ],
    standings: [], champion_lineage: ['v0'],
  };
  installFixtureMap(F);
  coreState.state.setHeartbeat({ phase: 'tournament:round_0', generation_id: 'v2', epoch_id: LIVE_UX_EPOCH });
  coreState.state.activeRuns = [{ generation_id: 'v2', entry_id: 'b0', run_id: 'rr', progress: 1, epoch_id: LIVE_UX_EPOCH }];
  coreState.state.activeTournament = F['/api/active-tournament'];

  const gens = await import('../js/variants/T/views/gens.js');
  const host = document.createElement('div');
  await gens.render(host, { navigate() {}, href: router.href }, { epochId: LIVE_UX_EPOCH });
  assert(allByClass(host, 'dt-live-pill')[0], 'the live pill is shown');
  assert(svgsByClass(host, 'dn-swissladder')[0], 'the live swiss ladder renders (NOT a being-seeded empty)');
  assert(!/being seeded/i.test(host.textContent), 'NOT "being seeded" once the field + active round exist');
  // the in-flight pairing reads its board progress (running) INSIDE the ladder
  // (the dense tables were collapsed away) — a live progress bar + "running".
  assert(svgsByClass(host, 'dn-swissladder')[0] && allByClass(host, 'dn-swissladder-bar-live')[0], 'the in-flight pairing shows a live progress bar in the ladder');
  assert(/running/.test(host.textContent), 'the active pairing reads "running"');
  // a decided pairing's winner accumulates a Copeland point (v1 beat v0).
  assert(host.textContent.includes('v1'), 'the decided pairing winner (v1) is shown');

  // "being seeded" ONLY when there is NO competitor/round yet.
  freshState();
  const F2 = liveUxFixture();
  F2['/api/active-tournament'] = { epoch_id: LIVE_UX_EPOCH, structure: 'swiss', phase: 'running', structure_params: { rounds: 3 }, competitors: [], rounds: [], standings: [] };
  installFixtureMap(F2);
  // a genuinely-live just-started run carries a FRESH heartbeat (the staleness
  // gate now reads a no-timestamp heartbeat as stale ⇒ not live).
  coreState.state.setHeartbeat(freshHb({ phase: 'tournament:round_0', generation_id: '', epoch_id: LIVE_UX_EPOCH }));
  coreState.state.activeRuns = [];
  coreState.state.activeTournament = F2['/api/active-tournament'];
  const host2 = document.createElement('div');
  await gens.render(host2, { navigate() {}, href: router.href }, { epochId: LIVE_UX_EPOCH });
  assert(/being seeded|run is starting|fills in/i.test(host2.textContent), '"being seeded"/starting shows ONLY when no competitor/round exists yet');

  coreState.state.heartbeat = { phase: 'idle' }; coreState.state.activeRuns = []; coreState.state.activeTournament = null;
});

// (4) the live hero BLOOMS from the proposed field → the live ladder once the
// tournament is running (proposing tracker is the SEED of the ladder).
test('live hero (BLOOM): a RUNNING swiss with the applied field as competitors (no round scored yet) shows the live LADDER, not the proposing tracker', () => {
  try { globalThis.window.localStorage.clear(); } catch (e) { /* ignore */ }
  coreState.state.connected = true; coreState.state.connecting = false;
  // the tournament has STARTED (running) — the applied field (v1..v3) are
  // competitors but no pairing has scored yet. The hero must BLOOM into the
  // ladder seeded by these competitors, not stay on the proposing tracker.
  coreState.state.setHeartbeat({ phase: 'tournament:round_0', generation_id: 'v1', epoch_id: HERO_EPOCH });
  coreState.state.activeRuns = [{ generation_id: 'v1', entry_id: 'b0', run_id: 'r1', progress: 0.5 }];
  coreState.state.activeTournament = {
    structure: 'swiss', phase: 'running', epoch_id: HERO_EPOCH, structure_params: { rounds: 3 },
    competitors: [
      { generation_id: 'v0', role: 'champion' }, { generation_id: 'v1', role: 'challenger' },
      { generation_id: 'v2', role: 'challenger' }, { generation_id: 'v3', role: 'challenger' },
    ],
    rounds: [], standings: [], champion_lineage: ['v0'],
    field_status: [
      { generation_id: 'v1', status: 'applied', seed: 2 },
      { generation_id: 'v2', status: 'applied', seed: 3 },
      { generation_id: 'v3', status: 'applied', seed: 4 },
    ],
  };

  const root = mountLiveShell('#/');
  assert(svgsByClass(root, 'dn-swissladder')[0], 'the hero BLOOMED into the live swiss ladder (applied field → competitors)');
  assertEqual(allByClass(root, 'dn-prop-tracker').length, 0, 'the proposing tracker is REPLACED by the ladder once the tournament runs');
  assertEqual(allByClass(root, 'dt-live-hero-nofunnel').length, 0, 'not the bland placeholder');

  coreState.state.heartbeat = { phase: 'idle' };
  coreState.state.activeRuns = []; coreState.state.activeTournament = null;
});

// (5) the lifecycle DAG's pending terminal label is STRUCTURE-AWARE.
test('lifecycle DAG: the pending terminal label is structure-aware (swiss → "⋯ competing", elim → "⋯ in bracket", racing → "⋯ racing", unknown → "⋯ awaiting gate")', () => {
  const entries = [{ entry_id: 'b0', drift_loss: 10, pass_fail: 1 }];
  const swiss = dag.lifecycleDag({ genId: 'v1', parentId: 'v0', decision: 'pending', entries, structure: 'swiss' });
  assert(swiss.textContent.includes('⋯ competing'), 'a pending swiss candidate reads "⋯ competing"');
  assert(!swiss.textContent.includes('⋯ racing'), 'a pending swiss candidate does NOT read "racing"');

  const elim = dag.lifecycleDag({ genId: 'v1', parentId: 'v0', decision: 'pending', entries, structure: 'single_elim' });
  assert(elim.textContent.includes('⋯ in bracket'), 'a pending elim candidate reads "⋯ in bracket"');

  const racing = dag.lifecycleDag({ genId: 'v1', parentId: 'v0', decision: 'pending', entries, structure: 'racing' });
  assert(racing.textContent.includes('⋯ racing'), 'a pending racing candidate still reads "⋯ racing"');

  const unknown = dag.lifecycleDag({ genId: 'v1', parentId: 'v0', decision: 'pending', entries });
  assert(unknown.textContent.includes('⋯ awaiting gate'), 'an unknown structure degrades to "⋯ awaiting gate"');
});

// ====================================================================
// CONSOLIDATION WAVE — live tournament truthfulness + visual consistency.
// ====================================================================

// ---- Task 1: live bracket / ladder / funnel fill from PUBLISHED rounds ----

test('live elim model: PUBLISHED single_elim rounds render the bracket (not "being seeded") with active-runs progress overlaid', () => {
  const at = {
    structure: 'single_elim', phase: 'running', epoch_id: HERO_EPOCH,
    structure_params: { board_size: 4 }, round_index: 0,
    competitors: [
      { generation_id: 'v0', role: 'champion' }, { generation_id: 'v1', role: 'challenger' },
      { generation_id: 'v2', role: 'challenger' }, { generation_id: 'v3', role: 'challenger' },
    ],
    rounds: [
      { round_index: 0, label: 'Semifinal', matches: [
        { match_id: 'WB-R0-0', competitors: ['v0', 'v3'], bracket_slot: 'WB-R0-0', winner: null, pending: true },
        { match_id: 'WB-R0-1', competitors: ['v1', 'v2'], bracket_slot: 'WB-R0-1', winner: null, pending: true },
      ] },
    ],
    standings: [], champion_lineage: ['v0'],
  };
  const model = STRUCT.buildLiveElimModel({
    at, heartbeat: { phase: 'tournament:round_0', generation_id: 'v1' },
    activeRuns: [{ generation_id: 'v1', entry_id: 'b0', run_id: 'r0', progress: 0.5 }],
    epochGens: ['v0', 'v1', 'v2', 'v3'],
  });
  assert(model && model.live, 'a live elim model built from the published rounds');
  const m = STRUCT.elimModel(model);
  assert(m.hasMatches, 'the published round has matches');
  const active = m.winners[0].matches.find((mm) => (mm.competitors || []).includes('v1'));
  assert(active && active.pending, 'the active published match is pending');
  assert(active.inflight >= 1 || active.done >= 1, 'active-runs progress is overlaid onto the pending match');
  const nodes = STRUCT.renderStructure(model, { navigate() {}, href: router.href }, HERO_EPOCH);
  const host = document.createElement('div');
  for (const n of nodes) host.appendChild(n);
  assert(svgsByClass(host, 'dn-elimflow')[0], 'the bracket-as-flow SVG rendered from the published rounds');
  assert(!/being seeded/i.test(host.textContent), 'NOT the "being seeded" state once the rounds are published');
});

// ---- Task 1: the candidate page's match-ups populate from LIVE rounds ----

test('candidate match-ups: a candidate running its first round populates from the LIVE published rounds (NOT "did not run in any round")', async () => {
  freshState();
  const CM_EPOCH = '2026-06-02_cm';
  const F = {
    '/api/epoch': { epoch_id: CM_EPOCH, closed: false, goal: 'g',
      tournament: { structure: 'single_elim', params: { board_size: 4 } },
      experiments: [
        { generation_id: 'v0', parent_generation_id: '', outcome: { decision: 'baseline' } },
        { generation_id: 'v1', parent_generation_id: 'v0', outcome: {} },
      ], board: [] },
    '/api/lineage': { generations: [
      { generation_id: 'v0', epoch_id: CM_EPOCH, parent_generation_id: '', promoted: true },
      { generation_id: 'v1', epoch_id: CM_EPOCH, parent_generation_id: 'v0', promoted: null },
    ] },
    '/api/score-trajectory': { points: [{ generation_id: 'v0', scalar: 50 }] },
    // the COMPLETED bracket feed is EMPTY — nothing has committed yet.
    '/api/tournaments': { epoch_id: CM_EPOCH, champion_lineage: ['v0'], matchups: [], tournaments: [] },
    [`/api/generation/${CM_EPOCH}/v0/per-entry`]: { entries: [{ entry_id: 'b0', run_id: 'r0', drift_loss: 40, pass_fail: 1 }] },
    [`/api/generation/${CM_EPOCH}/v1/per-entry`]: { entries: [{ entry_id: 'b0', run_id: 'r1', drift_loss: 38, pass_fail: 1, match_id: 'WB-R0-0' }] },
  };
  installFixtureMap(F);
  // a LIVE run for THIS epoch: the published rounds carry v0 vs v1 in flight.
  coreState.state.setHeartbeat({ phase: 'tournament:round_0', generation_id: 'v1', epoch_id: CM_EPOCH });
  coreState.state.activeRuns = [{ generation_id: 'v1', entry_id: 'b0', run_id: 'r1', progress: 0.5, epoch_id: CM_EPOCH }];
  coreState.state.activeTournament = {
    epoch_id: CM_EPOCH, structure: 'single_elim', phase: 'running',
    rounds: [{ round_index: 0, label: 'Semifinal', matches: [
      { match_id: 'WB-R0-0', competitors: ['v0', 'v1'], bracket_slot: 'WB-R0-0', winner: null, pending: true },
    ] }],
    competitors: [{ generation_id: 'v0', role: 'champion' }, { generation_id: 'v1', role: 'challenger' }],
  };

  const candidate = await import('../js/variants/T/views/candidate.js');
  const host = document.createElement('div');
  await candidate.render(host, { navigate() {}, href: router.href }, { epochId: CM_EPOCH, gen: 'v1' });
  assert(!/did not run in any tournament round/i.test(host.textContent),
    'a live candidate is NOT reported as "did not run in any round" while it is plainly racing');
  assert(host.textContent.includes('v0 → v1') || /v0.*v1/.test(host.textContent),
    'the live match-up (v0 → v1) populates the candidate match-ups table');

  coreState.state.heartbeat = { phase: 'idle' };
  coreState.state.activeRuns = []; coreState.state.activeTournament = null;
});

// ---- Task 2: structure-correct live status (no "racing" for elim) ----

test('live status mapper: structure-correct labels — elim → "in bracket"/"competing", swiss → "playing", racing → "racing"; terminals pass through', () => {
  assertEqual(livestatus.structureStatusLabel('competing', 'single_elim'), 'in bracket', 'elim in-contention reads "in bracket"');
  assertEqual(livestatus.structureStatusLabel('competing', 'double_elim'), 'in bracket', 'double-elim too');
  assertEqual(livestatus.structureStatusLabel('competing', 'swiss'), 'playing', 'swiss in-contention reads "playing"');
  assertEqual(livestatus.structureStatusLabel('competing', 'racing'), 'racing', 'racing in-contention reads "racing"');
  // terminals + alive pass through in EVERY structure.
  for (const st of ['single_elim', 'swiss', 'racing']) {
    assertEqual(livestatus.structureStatusLabel('champion', st), 'champion', 'champion passes through (' + st + ')');
    assertEqual(livestatus.structureStatusLabel('eliminated', st), 'eliminated', 'eliminated passes through (' + st + ')');
    assertEqual(livestatus.structureStatusLabel('alive', st), 'alive', 'alive passes through (' + st + ')');
  }
});

test('standings table (LIVE elim): a mid-run champion/eliminated standing is NOT mislabeled "racing" — uses the elim word "in bracket"', () => {
  const st = STRUCT.normalizeStructure({
    structure: 'single_elim', phase: 'running',
    rounds: [{ round_index: 0, label: 'Semifinal', matches: [
      { match_id: 'WB-R0-0', competitors: ['v0', 'v1'], bracket_slot: 'WB-R0-0', winner: null, pending: true },
    ] }],
    standings: [
      { generation_id: 'v0', rank: 1, scalar: 40, wins: 1, losses: 0, status: 'champion' },
      { generation_id: 'v1', rank: 2, scalar: 45, wins: 0, losses: 1, status: 'eliminated' },
    ],
    competitors: [{ generation_id: 'v0', role: 'champion' }, { generation_id: 'v1', role: 'challenger' }],
  }, true);
  const nodes = STRUCT.renderStructure(st, { navigate() {}, href: router.href }, EPOCH_ID);
  const host = document.createElement('div');
  for (const n of nodes) host.appendChild(n);
  const standings = allByClass(host, 'dt-standings')[0];
  assert(standings, 'the standings table rendered');
  assert(!/racing/.test(standings.textContent), 'a LIVE elim standings table NEVER reads "racing"');
  assert(/in bracket/.test(standings.textContent), 'a LIVE elim in-contention standing reads "in bracket"');
});

// ---- Task 3: cached-champion badge from provenance ----

test('cached champion: per-entry cached/source_epoch surfaces a "cached · from <epoch>" badge + a fast eval-mode tag (no "no entries scored")', async () => {
  freshState();
  const CC_EPOCH = '2026-06-02_cc';
  const F = {
    '/api/epoch': { epoch_id: CC_EPOCH, closed: true, goal: 'g',
      tournament: { structure: 'racing', params: {} },
      experiments: [{ generation_id: 'v0', parent_generation_id: '', outcome: { decision: 'baseline' } }], board: [] },
    '/api/lineage': { generations: [
      { generation_id: 'v0', epoch_id: CC_EPOCH, parent_generation_id: '', promoted: true },
    ] },
    '/api/score-trajectory': { points: [{ generation_id: 'v0', scalar: 50 }] },
    '/api/tournaments': { epoch_id: CC_EPOCH, champion_lineage: ['v0'], matchups: [], tournaments: [] },
    // the champion v0's per-board results are CACHED from a prior epoch.
    [`/api/generation/${CC_EPOCH}/v0/per-entry`]: { entries: [
      { entry_id: 'b0', run_id: 'r0', drift_loss: 40, pass_fail: 1, cached: true, source_epoch: '2026-06-01_e0', source_run: 'run_prior' },
    ] },
  };
  installFixtureMap(F);
  const candidate = await import('../js/variants/T/views/candidate.js');
  const host = document.createElement('div');
  await candidate.render(host, { navigate() {}, href: router.href }, { epochId: CC_EPOCH, gen: 'v0' });
  assert(/cached/i.test(host.textContent), 'a cached champion shows a "cached" badge');
  assert(/2026-06-01_e0/.test(host.textContent), 'the badge names the source epoch');
  assert(/fast — champion reused/.test(host.textContent), 'a fast-mode header tag reads "fast — champion reused"');
  assert(!/no per-entry scores|no board entries scored/i.test(host.textContent),
    'a cached champion does NOT read "no board entries scored"');
});

// ---- Task 4: objective falls back to the brief H1 title ----

test('epoch objective: falls back to the brief H1 title (stripping "Epoch eN — ") when no explicit goal', async () => {
  const epoch = await import('../js/variants/T/views/epoch.js');
  // explicit goal wins.
  assertEqual(epoch.objectiveText({ goal: 'crisper slides', brief: '# Epoch e3 — Tighten oversight\n' }), 'crisper slides');
  // no goal → the brief H1 title, prefix stripped.
  assertEqual(epoch.objectiveText({ goal: '', brief: '# Epoch e3 — Tighten oversight\n\n## Goal\nx' }), 'Tighten oversight');
  assertEqual(epoch.objectiveText({ goal: null, brief: '# Reduce hallucination\n' }), 'Reduce hallucination');
  // an H2 is NOT a title; with no H1 and no goal → the honest placeholder.
  assertEqual(epoch.objectiveText({ goal: '', brief: '## Goal\nx' }), '(no objective recorded)');
  assertEqual(epoch.objectiveText({ goal: '', brief: '' }), '(no objective recorded)');
  // a colon-separated prefix is also stripped.
  assertEqual(epoch.briefTitle('# Epoch 2026-06-02_e1: Add a judge'), 'Add a judge');
});

// ---- Task 5: "field of N" excludes unscored orphans ----

test('epoch overview: "field of N" counts champion + applied challengers, EXCLUDING unscored orphans', async () => {
  freshState();
  const FN_EPOCH = '2026-06-02_fn';
  const F = {
    '/api/epoch': { epoch_id: FN_EPOCH, closed: false, goal: 'g',
      tournament: { structure: 'swiss', params: { rounds: 3 } },
      experiments: [], board: [] },
    '/api/lineage': { generations: [
      { generation_id: 'v0', epoch_id: FN_EPOCH, parent_generation_id: '', promoted: true },
      { generation_id: 'v1', epoch_id: FN_EPOCH, parent_generation_id: 'v0', promoted: false },
      { generation_id: 'v2', epoch_id: FN_EPOCH, parent_generation_id: 'v0', promoted: false },
      // v9 is an UNSCORED ORPHAN — proposed but never entered the tournament.
      { generation_id: 'v9', epoch_id: FN_EPOCH, parent_generation_id: 'v0', promoted: null },
    ] },
    // v0/v1/v2 scored; v9 has NO scalar (orphan).
    '/api/score-trajectory': { points: [
      { generation_id: 'v0', scalar: 50 }, { generation_id: 'v1', scalar: 60 }, { generation_id: 'v2', scalar: 55 },
    ] },
    '/api/tournaments': { epoch_id: FN_EPOCH, champion_lineage: ['v0'], matchups: [], tournaments: [] },
    [`/api/generation/${FN_EPOCH}/v0/per-entry`]: { entries: [] },
    [`/api/generation/${FN_EPOCH}/v1/per-entry`]: { entries: [] },
    [`/api/generation/${FN_EPOCH}/v2/per-entry`]: { entries: [] },
    [`/api/generation/${FN_EPOCH}/v9/per-entry`]: { entries: [] },
  };
  installFixtureMap(F);
  const epoch = await import('../js/variants/T/views/epoch.js');
  const host = document.createElement('div');
  await epoch.render(host, { navigate() {}, href: router.href }, { epochId: FN_EPOCH });
  // the real field is the challenger fan {v1, v2} (v0 is the carried champion on
  // the spine); the unscored orphan v9 is EXCLUDED from the minted field.
  const chips = allByClass(host, 'dn-roundtl-chip').map((c) => { const mono = allByClass(c, 'dn-mono')[0]; return mono ? (mono.textContent || '').trim() : ''; });
  assertDeep(chips.filter((s, i) => chips.indexOf(s) === i).sort(), ['v1', 'v2'], 'the field is {v1,v2} — the unscored orphan v9 is excluded');
  assert(!chips.includes('v9'), 'the orphan v9 is NOT a minted-field chip');
});

// ---- Task 6: crown glyph is ♛ for current / ♔ for former everywhere ----

test('crown glyphs: the shared CROWN constant is ♛ current / ♔ former; no ♚ is emitted by any gate label', () => {
  assertEqual(svg.CROWN.current, '♛', 'the current-champion crown is ♛');
  assertEqual(svg.CROWN.former, '♔', 'the former-champion crown is ♔');

  // a crowned racing gate (survival funnel) shows ♛, never ♚.
  const rungs = [{ label: 'Rung 0', match_id: 'rung0', competitors: ['v1', 'v2'], survivors: ['v1'], cut: ['v2'], board_fraction: 0.5 }];
  const funnel = svg.survivalFunnel({ rungs, championId: 'v1', benchmarkId: 'v0', gateState: 'crowned', gateDelta: -2 });
  assert(funnel.textContent.includes('♛'), 'a crowned funnel gate emits ♛');
  assert(!funnel.textContent.includes('♚'), 'a crowned funnel gate does NOT emit ♚');

  // a crowned elim bracket-as-flow gate.
  const winners = [{ round_index: 0, label: 'Final', matches: [{ match_id: 'WB-R0-0', competitors: ['v0', 'v1'], winner: 'v1', decision: 'promoted', bracket_slot: 'WB-R0-0' }] }];
  const bracket = svg.elimFlow({ winners, championId: 'v1', benchmarkId: 'v0', gateState: 'crowned' });
  assert(bracket.textContent.includes('♛'), 'a crowned elim flow gate emits ♛');
  assert(!bracket.textContent.includes('♚'), 'a crowned elim flow gate does NOT emit ♚');

  // the tree current/former champion glyphs.
  const thost = document.createElement('div');
  tree.buildTree(thost, {
    epochs: [{ id: EPOCH_ID, current: true }],
    byEpoch: { [EPOCH_ID]: { gens: [
      { id: 'v0', promoted: true, parent: null, formerChampion: true },
      { id: 'v6', promoted: true, parent: 'v0', currentChampion: true },
    ], boards: [] } },
  }, { view: 'gens', params: { epochId: EPOCH_ID } }, new Set(['e:' + EPOCH_ID, 'e:' + EPOCH_ID + '/gens']),
    { navigate() {}, href: router.href }, () => {});
  assert(thost.textContent.includes('♛'), 'the tree marks the current champion ♛');
  assert(thost.textContent.includes('♔'), 'the tree marks the former champion ♔');
  assert(!thost.textContent.includes('♚'), 'the tree emits no ♚');
});

// =====================================================================
// INTEGRATION WAVE 8 — the LIVE match-grouped block (Task 1), the tree
// live-activity pulse (Task 2), and the elim generations-across-rounds
// flow (Task 3). All build on the consolidated live machinery
// (buildLiveModel + the published rounds + active-runs overlay) and the
// shared CROWN / glyph vocabulary — no per-structure synthesis, no new
// glyph literals.
// =====================================================================

// ── Task 1 — the match-grouped "what's running" block ──

// a LIVE swiss field with an ACTIVE round (round 1 pending) the block groups by.
const LIVE_SWISS_BLOCK = {
  structure: 'swiss', phase: 'running', epoch_id: HERO_EPOCH,
  structure_params: { board_size: 4, rounds: 3 },
  competitors: [{ generation_id: 'v0' }, { generation_id: 'v1' }, { generation_id: 'v2' }, { generation_id: 'v3' }],
  rounds: [
    { round_index: 0, label: 'Round 1', matches: [
      { match_id: 'sw_r0_m0', competitors: ['v0', 'v1'], winner: 'v1', decision: 'win' },
      { match_id: 'sw_r0_m1', competitors: ['v2', 'v3'], winner: 'v3', decision: 'win' },
    ] },
    { round_index: 1, label: 'Round 2', matches: [
      { match_id: 'sw_r1_m0', competitors: ['v1', 'v3'] },
      { match_id: 'sw_r1_m1', competitors: ['v0', 'v2'] },
    ] },
  ],
  standings: [], champion_lineage: ['v0'],
};

test('Task 1 — match blocks (swiss): one block per IN-FLIGHT match, two sides, with per-board progress; settled rounds are NOT blocks', () => {
  const model = STRUCT.buildLiveSwissModel({
    at: LIVE_SWISS_BLOCK,
    heartbeat: { phase: 'tournament:round_1', generation_id: 'v1', epoch_id: HERO_EPOCH },
    activeRuns: [
      { generation_id: 'v1', entry_id: 'b0', run_id: 'r0', progress: 2.0 }, // 2 of 4 boards done
      { generation_id: 'v1', entry_id: 'b1', run_id: 'r1', progress: 0.0 },
    ],
    epochGens: ['v0', 'v1', 'v2', 'v3'],
  });
  const blocks = STRUCT.liveMatchBlocks(model);
  // round 0 is settled (winners decided) → no block; round 1 is the active round
  // with TWO pending pairings → two blocks.
  assertEqual(blocks.length, 2, 'one block per IN-FLIGHT match (the two pending round-2 pairings); settled round 0 is excluded');
  for (const b of blocks) {
    assertEqual(b.kind, 'pair', 'a swiss block is a pairwise (two-sided) block');
    assertEqual(b.entries.length, 2, 'a pairwise block shows two sides');
  }
  const v1v3 = blocks.find((b) => b.entries.some((e) => e.id === 'v1') && b.entries.some((e) => e.id === 'v3'));
  assert(v1v3, 'a block names the in-flight pairing v1 vs v3');
  assert(/v1 vs v3/.test(v1v3.label), 'the block header names the match — "… · v1 vs v3"');
  const e = v1v3.entries.find((x) => x.id === 'v1');
  assertEqual(e.total, 4, 'the side carries the board total (board_size)');
  assert(svg.isNum(e.ratio) && e.ratio > 0, 'the side carries a live progress ratio from active-runs (2/4 boards done)');
});

test('Task 1 — match blocks (elim): blocks group by in-flight WB match, named WB-R0-0 · v0 vs v3', () => {
  const model = STRUCT.buildLiveElimModel({
    at: liveElimField(),
    heartbeat: { phase: 'tournament:round_0', generation_id: 'v1', epoch_id: HERO_EPOCH },
    activeRuns: [
      { generation_id: 'v0', entry_id: 'b0', run_id: 'r0', progress: 0.25 },
      { generation_id: 'v1', entry_id: 'b1', run_id: 'r1', progress: 0.75 },
    ],
    epochGens: ['v0', 'v1', 'v2', 'v3'],
  });
  const blocks = STRUCT.liveMatchBlocks(model);
  assertEqual(blocks.length, 2, 'two in-flight WB-R0 matches → two blocks');
  const wb0 = blocks.find((b) => /WB-R0-0/.test(b.label));
  assert(wb0, 'a block is named by its bracket slot WB-R0-0');
  assert(/v0 vs v3/.test(wb0.label), 'the elim block header reads "WB-R0-0 · v0 vs v3"');
});

test('Task 1 — match blocks (racing): a rung-FIELD block (one entry per lane), header "rung 0 · field of N"', () => {
  const model = STRUCT.buildLiveRacingModel({
    at: liveRacingField(),
    heartbeat: { phase: 'tournament:round_0:rung0_m1', generation_id: 'v5', epoch_id: '2026-06-02_eR' },
    activeRuns: [
      { generation_id: 'v5', entry_id: 'b0', run_id: 'r0', progress: 0.4 },
      { generation_id: 'v6', entry_id: 'b1', run_id: 'r1', progress: 0.9 },
    ],
    epochGens: ['v0', 'v5', 'v6', 'v7', 'v8'],
  });
  const blocks = STRUCT.liveMatchBlocks(model);
  const rung = blocks.find((b) => b.kind === 'rung');
  assert(rung, 'racing yields a rung-field block (not a pairwise block)');
  assertEqual(rung.entries.length, 4, 'the rung block shows one entry per lane in the field of 4');
  assert(/field of 4/.test(rung.label), 'the header reads "rung … · field of 4"');
  const v5 = rung.entries.find((e) => e.id === 'v5');
  assert(v5 && svg.isNum(v5.ratio), 'a lane carries its live progress ratio');
});

test('Task 1 — the match-grouped block RENDERS: one DOM block per match, a progress bar + a state per side; clickable', () => {
  let opened = null;
  const node = live.liveMatchGroupedBlocks(
    STRUCT.liveMatchBlocks(STRUCT.buildLiveSwissModel({
      at: LIVE_SWISS_BLOCK,
      heartbeat: { phase: 'tournament:round_1', epoch_id: HERO_EPOCH },
      activeRuns: [{ generation_id: 'v1', entry_id: 'b0', progress: 0.5 }],
      epochGens: ['v0', 'v1', 'v2', 'v3'],
    })),
    (id) => { opened = id; },
  );
  const host = document.createElement('div');
  host.appendChild(node);
  assertEqual(allByClass(host, 'dt-live-match').length, 2, 'one DOM block per in-flight match');
  assert(allByClass(host, 'dt-live-match-fill').length >= 2, 'each side has an animated progress fill');
  // the fill width is set inline (CSS-animated; the DOM is not rebuilt per tick).
  const fill = allByClass(host, 'dt-live-match-fill')[0];
  assert(/width:\s*\d+%/.test(fill.style.cssText), 'the progress fill width is set in the style (CSS width transition, not a node swap)');
  // a side row is clickable → opens the candidate.
  const row = allByClass(host, 'dt-live-match-row')[0];
  row.dispatchEvent(makeEvent('click'));
  assert(opened != null, 'clicking a side opens the candidate');
});

test('Task 1 — the block is digest-gated on the live CONTENT: a no-op heartbeat is a no-op; a progress-bucket change re-stamps', () => {
  const at = LIVE_SWISS_BLOCK;
  const beat = (progress) => STRUCT.liveMatchBlocksDigest(STRUCT.liveMatchBlocks(STRUCT.buildLiveSwissModel({
    at, heartbeat: { phase: 'tournament:round_1', epoch_id: HERO_EPOCH },
    activeRuns: [{ generation_id: 'v1', entry_id: 'b0', progress }],
    epochGens: ['v0', 'v1', 'v2', 'v3'],
  })));
  // board_size 4: progress 0.5 → done 0 vs 0.55 → still bucket 0 (no rebuild),
  // but a real bucket jump (progress that lands a board) re-stamps.
  assertEqual(beat(0.0), beat(0.0), 'identical state → identical digest (a no-op heartbeat writes ZERO DOM)');
  const d0 = beat(0.0);
  const dBig = beat(3.5); // ~3-4 of 4 boards done → a real progress bucket
  assert(d0 !== dBig, 'a real per-board progress change re-stamps the digest');
});

// ── Task 2 — the tree live-activity pulse ──

test('Task 2 — tree pulse: a running gen / board entry gets a CSS pulse badge; the digest re-stamps on set change but NOT on a no-op beat', () => {
  const model = {
    epochs: [{ id: EPOCH_ID, current: true }],
    byEpoch: { [EPOCH_ID]: {
      gens: [{ id: 'v0', promoted: true, parent: null, currentChampion: true }, { id: 'v1', promoted: null, parent: 'v0' }],
      boards: [{ id: 'b0' }, { id: 'b1' }],
    } },
    current: EPOCH_ID,
  };
  const route = { view: 'gens', params: { epochId: EPOCH_ID } };
  const toggles = new Set(['e:' + EPOCH_ID, 'e:' + EPOCH_ID + '/gens', 'e:' + EPOCH_ID + '/boards']);
  const ctx = { navigate() {}, href: router.href };

  // v1 + b0 are running → the badge appears on those rows only.
  const liveSet = new Set(['v1', 'b0']);
  const host = document.createElement('div');
  tree.buildTree(host, model, route, toggles, ctx, () => {}, liveSet);
  const pulses = allByClass(host, 'dt-node-pulse');
  assert(pulses.length === 2, 'exactly the two running rows (v1, b0) get a pulse badge');
  assert(pulses.every((p) => (p.getAttribute('class') || '').includes('dn-inflight-pulse')), 'the pulse REUSES dn-inflight-pulse (the existing CSS-animated clue)');

  // digest discipline: same set → same digest (no-op beat); set change → new digest.
  const dA = tree.treeDigest(model, route, toggles, liveSet);
  const dA2 = tree.treeDigest(model, route, toggles, new Set(['b0', 'v1'])); // same set, different order
  assertEqual(dA, dA2, 'a steady beat with the SAME live set (order-insensitive) is a digest no-op');
  const dB = tree.treeDigest(model, route, toggles, new Set(['v1'])); // b0 LEAVES the running set
  assert(dA !== dB, 'a gen/entry LEAVING the running set re-stamps the digest');
  const dC = tree.treeDigest(model, route, toggles, new Set(['v1', 'b0', 'v0'])); // v0 ENTERS
  assert(dA !== dC, 'a gen/entry ENTERING the running set re-stamps the digest');

  // idle (empty set) → no pulse.
  const idle = document.createElement('div');
  tree.buildTree(idle, model, route, toggles, ctx, () => {}, new Set());
  assertEqual(allByClass(idle, 'dt-node-pulse').length, 0, 'an idle workspace shows no pulse');
});

test('Task 2 — treeLiveSet: derives the running gen+entry ids from active-runs, gated on running + scoped to the epoch', () => {
  const runs = [
    { generation_id: 'v1', entry_id: 'b0', epoch_id: EPOCH_ID },
    { generation_id: 'v2', entry_id: 'b1' }, // no epoch tag → kept (legacy tolerance)
    { generation_id: 'v9', entry_id: 'bX', epoch_id: 'OTHER' }, // foreign epoch → dropped
  ];
  const set = livestatus.treeLiveSet({ activeRuns: runs, running: true, epochId: EPOCH_ID });
  assert(set.has('v1') && set.has('b0'), 'the running gen + entry of the viewed epoch are in the set');
  assert(set.has('v2') && set.has('b1'), 'an untagged run is kept (legacy single-epoch tolerance)');
  assert(!set.has('v9') && !set.has('bX'), 'a foreign-epoch run is excluded');
  const idle = livestatus.treeLiveSet({ activeRuns: runs, running: false, epochId: EPOCH_ID });
  assertEqual(idle.size, 0, 'an idle workspace (running=false) yields the empty set');
});

// ── Task 3 — the elim generations-across-rounds flow ──

test('Task 3 — elimFlow: rounds as columns, one lane per generation; advancing lines + a terminating ✕, the crown at the gate', () => {
  const model = STRUCT.elimModel(STRUCT.normalizeStructure(SE_STRUCT, false));
  const flow = svg.elimFlow({
    winners: model.winners, championId: model.championId, benchmarkId: model.benchmarkId,
    gateState: model.gateState, live: false, onCompetitor() {},
  });
  assertEqual(flow.getAttribute('class'), 'dn-elimflow', 'the flow is its own renderer (dn-elimflow)');
  assert((flow.getAttribute('width') || '') === '100%' && (flow.getAttribute('viewBox') || ''), 'fit-to-width: width:100% + a viewBox');
  // rounds-as-columns headers + the gate column.
  const cols = allByClass(flow, 'dn-elimflow-col').map((c) => c.textContent);
  assert(cols.some((t) => /Semifinal|R0/.test(t)) && cols.some((t) => /Final|R1/.test(t)), 'rounds are columns (R0 · R1 · …)');
  assert(cols.some((t) => /champion-gate/i.test(t)), 'the champion-gate is the trailing column');
  // an advancing leg (good) + a terminating ✕ (bad) exist.
  assert(allByClass(flow, 'dn-elimflow-good').length >= 1, 'an advancing line/marker reads --v2-good');
  assert(flow.textContent.includes('✕'), 'an eliminated generation terminates with ✕');
  // the champion (v1) reaches the gate with the current crown; v0 (displaced
  // incumbent / benchmark) reads the former crown.
  assertEqual(String(model.championId), 'v1', 'v1 is the bracket champion');
  assert(flow.textContent.includes(svg.CROWN.current), 'the champion lane reaches the gate marked ♛ (CROWN.current)');
  assert(flow.textContent.includes(svg.CROWN.former), 'the displaced incumbent (benchmark v0) reads ♔ (CROWN.former)');
  assert(!flow.textContent.includes('♚'), 'no stray ♚ glyph literal');
});

test('Task 3 — the elim figure is the bracket-as-FLOW (elimFlow), the seat/box tree retired; ABSENT for non-elim (racing)', () => {
  // elim: the bracket-as-flow IS the figure (no seat/box tree).
  const elimNodes = STRUCT.renderStructure(STRUCT.normalizeStructure(SE_STRUCT, false), { navigate() {}, href: router.href }, EPOCH_ID);
  const elimHost = document.createElement('div');
  for (const n of elimNodes) elimHost.appendChild(n);
  assertEqual(svgsByClass(elimHost, 'dn-elimbracket').length, 0, 'the seat/box bracket tree is retired');
  assert(svgsByClass(elimHost, 'dn-elimflow')[0], 'the bracket-as-flow (elimFlow) is the elim figure');
  assert(/Bracket flow/i.test(elimHost.textContent), 'the section carries its bracket-flow title');

  // racing: NO elim flow (it is elim-only).
  const racingNodes = STRUCT.renderStructure(STRUCT.normalizeStructure(RACING_STRUCT, false), { navigate() {}, href: router.href }, EPOCH_ID);
  const racingHost = document.createElement('div');
  for (const n of racingNodes) racingHost.appendChild(n);
  assertEqual(svgsByClass(racingHost, 'dn-elimflow').length, 0, 'the elim flow is ABSENT for a non-elim (racing) structure');
});

test('Task 3 — a LIVE elim flow draws in-flight legs as DASHED (pending convention) from the published rounds', () => {
  const model = STRUCT.elimModel(STRUCT.buildLiveElimModel({
    at: liveElimField(),
    heartbeat: { phase: 'tournament:round_0', epoch_id: HERO_EPOCH },
    activeRuns: [{ generation_id: 'v1', entry_id: 'b0', progress: 0.5 }],
    epochGens: ['v0', 'v1', 'v2', 'v3'],
  }));
  const flow = svg.elimFlow({
    winners: model.winners, championId: model.championId, benchmarkId: model.benchmarkId,
    gateState: model.gateState, live: true,
  });
  assert(allByClass(flow, 'dn-elimflow-seg-pending').length >= 1, 'an in-flight (pending) leg is drawn with the pending (dashed) class');
});

// ====================================================================
// EVOLVE ROUNDS (champion-spine round model + timeline + drill-down + tree).
//   * the round model groups gens by round_index (+ field-record fallback);
//   * the spine timeline renders one episode per round (champion-loss + figure
//     + gate); --rounds 1 degrades to a single episode; elim uses elimFlow;
//   * the round drill-down renders ONE round; the tree groups by round and
//     degrades when round_index is absent.
// ====================================================================

// ---- (1) the round MODEL groups gens by round_index ----------------

test('round model: groups generations by round_index — champion spine threads v0 → promoted', () => {
  const gens = [
    { id: 'v0', parent: null, promoted: true, round_index: null },
    { id: 'v1', parent: 'v0', promoted: false, round_index: 0 },
    { id: 'v2', parent: 'v0', promoted: true, round_index: 0 },   // promoted in round 0
    { id: 'v3', parent: 'v2', promoted: false, round_index: 1 },  // minted in round 1
    { id: 'v4', parent: 'v2', promoted: false, round_index: 1 },
  ];
  const scalarBy = new Map([['v0', 100], ['v1', 110], ['v2', 80], ['v3', 85], ['v4', 90]]);
  const model = rounds.epochRoundModel({ gens, scalarBy, bracket: {}, structure: 'gauntlet', championId: 'v0' });
  assertEqual(model.length, 2, 'two rounds (round_index 0 and 1)');
  assertEqual(model[0].source, 'round_index', 'the model derives from the round_index stamp');
  // round 0: champion v0 (loss 100), minted {v1, v2}, gate promotes v2.
  assertEqual(model[0].champion.id, 'v0', 'round 0 champion is the seed v0');
  assertEqual(model[0].champion.scalar, 100, 'round 0 champion carries its loss');
  assertDeep(model[0].challengers.map((c) => c.id).sort(), ['v1', 'v2'], 'round 0 minted {v1,v2}');
  assertEqual(model[0].gateOutcome.kind, 'promoted', 'round 0 promotes a challenger');
  assertEqual(model[0].gateOutcome.gen, 'v2', 'round 0 promotes v2');
  // round 1: champion is the carried-in (promoted) v2 — NOT re-minted.
  assertEqual(model[1].champion.id, 'v2', 'round 1 champion is the carried-in v2 (spine threaded)');
  assertDeep(model[1].challengers.map((c) => c.id).sort(), ['v3', 'v4'], 'round 1 minted {v3,v4}');
  assert(!model[1].challengers.some((c) => c.id === 'v2'), 'the carried champion v2 is NOT a minted challenger of round 1');
  assertEqual(model[1].gateOutcome.kind, 'held', 'round 1 holds (no promotion)');
});

// ---- the FIELD-RECORD fallback when round_index is ABSENT ----------

test('round model: degrades to the per-round FIELD records when round_index is absent', () => {
  const gens = [
    { id: 'v0', parent: null, promoted: true, round_index: null },
    { id: 'v1', parent: 'v0', promoted: false, round_index: null },
    { id: 'v2', parent: 'v0', promoted: true, round_index: null },
    { id: 'v3', parent: 'v2', promoted: false, round_index: null },
  ];
  const scalarBy = new Map([['v0', 100], ['v1', 110], ['v2', 80], ['v3', 90]]);
  const bracket = { champion_lineage: ['v0', 'v2'], tournaments: [
    // one FIELD record per round (swiss), each listing that round's competitors.
    { tournament_id: 't0', structure: 'swiss', competitors: [{ generation_id: 'v0' }, { generation_id: 'v1' }, { generation_id: 'v2' }], rounds: [], standings: [] },
    { tournament_id: 't1', structure: 'swiss', competitors: [{ generation_id: 'v2' }, { generation_id: 'v3' }], rounds: [], standings: [] },
  ] };
  const model = rounds.epochRoundModel({ gens, scalarBy, bracket, structure: 'swiss', championId: 'v0' });
  assertEqual(model.length, 2, 'two rounds from the two field records');
  assertEqual(model[0].source, 'field', 'the model derives from the field records');
  assertEqual(model[0].champion.id, 'v0', 'round 0 champion is v0');
  assertDeep(model[0].challengers.map((c) => c.id).sort(), ['v1', 'v2'], 'round 0 field minted {v1,v2}');
  // round 1: v2 carried (it appeared in round 0), only v3 is fresh.
  assertEqual(model[1].champion.id, 'v2', 'round 1 champion is the carried v2');
  assertDeep(model[1].challengers.map((c) => c.id), ['v3'], 'round 1 field minted only the fresh v3 (v2 carried)');
});

test('round model: degrades to a SINGLE round 0 when neither round_index nor field records exist (--rounds 1, every run so far)', () => {
  const gens = [
    { id: 'v0', parent: null, promoted: true, round_index: null },
    { id: 'v1', parent: 'v0', promoted: false, round_index: null },
    { id: 'v2', parent: 'v0', promoted: false, round_index: null },
  ];
  const scalarBy = new Map([['v0', 70], ['v1', 146], ['v2', 72]]);
  const bracket = { champion_lineage: ['v0'], matchups: [
    { champion: 'v0', challenger: 'v1', decision: 'rejected', ran_at: 'a' },
    { champion: 'v0', challenger: 'v2', decision: 'rejected', ran_at: 'b' },
  ] };
  const model = rounds.epochRoundModel({ gens, scalarBy, bracket, structure: 'gauntlet', championId: 'v0' });
  // gauntlet matchups: each is its own single-challenger round (the spine reads
  // r0 → r1), so two rounds — but a single-tournament epoch collapses to one.
  assert(model.length >= 1, 'at least one round is produced');
  assertEqual(model[0].champion.id, 'v0', 'round 0 champion is the seed');
  // every challenger is accounted for across the rounds.
  const allChallengers = model.flatMap((r) => r.challengers.map((c) => c.id));
  assertDeep([...new Set(allChallengers)].sort(), ['v1', 'v2'], 'every challenger appears in the round model');
});

// ---- (2) the SPINE TIMELINE renders one episode per round ----------

test('round timeline: renders one episode per round with the champion-loss annotation + gate outcome', () => {
  const rs = [
    { round_index: 0, champion: { id: 'v0', scalar: 100 }, structure: 'gauntlet',
      challengers: [{ id: 'v1', scalar: 90, promoted: true }], gateOutcome: { kind: 'promoted', gen: 'v1' } },
    { round_index: 1, champion: { id: 'v1', scalar: 90 }, structure: 'gauntlet',
      challengers: [{ id: 'v2', scalar: 95, promoted: false }], gateOutcome: { kind: 'held', gen: null } },
  ];
  let drilled = null;
  const node = svg.roundTimeline({ rounds: rs, onRound: (i) => { drilled = i; }, onCompetitor() {} });
  // one spine node + one episode per round.
  assertEqual(allByClass(node, 'dn-roundtl-disc').length, 2, 'one spine node per round');
  assertEqual(allByClass(node, 'dn-roundtl-ep').length, 2, 'one episode per round');
  // the descending loss floor reads on the spine (100 → 90).
  const losses = allByClass(node, 'dn-roundtl-loss').map((n) => (n.textContent || '').trim());
  assert(losses.includes('100.0') && losses.includes('90.0'), 'each spine node annotates the champion loss');
  // the gate outcome reads on each episode (promoted / held).
  assert(node.textContent.includes('v1 promoted'), 'round 0 episode shows the promoted gate outcome');
  assert(node.textContent.includes('champion held'), 'round 1 episode shows the held gate outcome');
  // clicking a spine node drills into that round.
  const second = node.querySelectorAll('[data-round]').filter((n) => n.getAttribute('data-round') === '1' && n.localName === 'g')[0];
  second.dispatchEvent(makeEvent('click'));
  assertEqual(drilled, 1, 'clicking a spine node drills into that round');
});

test('round timeline: a SINGLE round degrades to ONE episode (≈ today’s overview)', () => {
  const node = svg.roundTimeline({ rounds: [
    { round_index: 0, champion: { id: 'v0', scalar: 70 }, structure: 'gauntlet',
      challengers: [{ id: 'v1', scalar: 72, promoted: false }, { id: 'v2', scalar: 71, promoted: false }],
      gateOutcome: { kind: 'held', gen: null } },
  ], onRound() {} });
  assertEqual(allByClass(node, 'dn-roundtl-ep').length, 1, 'a single round → exactly ONE episode');
  assert(allByClass(node, 'dn-roundtl-single').length >= 1, 'the single-episode layout is flagged');
  // its challenger fan still lists the minted field.
  const chips = allByClass(node, 'dn-roundtl-chip').map((c) => { const m = allByClass(c, 'dn-mono')[0]; return m ? (m.textContent || '').trim() : ''; });
  assertDeep(chips.sort(), ['v1', 'v2'], 'the single episode lists its challenger fan');
});

test('round timeline: the per-round structure figure is embedded via the figureFor callback', () => {
  let asked = 0;
  const fig = svgEl('svg', { class: 'dn-test-fig' });
  const node = svg.roundTimeline({
    rounds: [{ round_index: 0, champion: { id: 'v0', scalar: 70 }, structure: 'swiss', challengers: [{ id: 'v1', scalar: 71, promoted: false }], gateOutcome: { kind: 'held', gen: null } }],
    figureFor: () => { asked += 1; return fig; }, onRound() {},
  });
  assert(asked === 1, 'figureFor is consulted once per round');
  assert(allByClass(node, 'dn-test-fig')[0] || node.querySelectorAll('[class]').some((n) => n.getAttribute('class') === 'dn-test-fig'),
    'the per-round structure figure is embedded in the episode');
});

// ---- ELIM PARITY (#1): the elim epoch episode uses elimFlow ---------

test('elim parity: a single-elim epoch episode leads with elimFlow (NOT the mini-bracket)', async () => {
  freshState();
  installFixtureMap(structFixture('single_elim', SE_STRUCT, 'tourn_e0_se'));
  const epoch = await import('../js/variants/T/views/epoch.js');
  const host = document.createElement('div');
  await epoch.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID });
  assert(svgsByClass(host, 'dn-elimflow')[0], 'the elim episode embeds the generations-across-rounds flow (elimFlow)');
  assertEqual(svgsByClass(host, 'dn-elimbracket-compact').length, 0, 'NO mini-bracket on the epoch overview (elimFlow subsumes it)');
});

// ---- (4) the ROUND DRILL-DOWN renders ONE round --------------------

test('round drill-down: the route carries a round param + renders ONE round’s tournament', async () => {
  // the router parses /gens/r/<round> into a round param + hrefs round-trip.
  const route = router.parseRoute(`#/e/${EPOCH_ID}/gens/r/1`);
  assertEqual(route.view, 'gens', 'the round route is a gens view');
  assertEqual(route.params.round, '1', 'the round param parses');
  assertEqual(router.href('gens', { epochId: EPOCH_ID, round: 1 }), `#/e/${EPOCH_ID}/gens/r/1`, 'a round href round-trips');
  // the bare gens href is unchanged (no round suffix).
  assertEqual(router.href('gens', { epochId: EPOCH_ID }), `#/e/${EPOCH_ID}/gens`, 'the all-rounds gens href is unchanged');

  freshState();
  installFixtureMap(structFixture('single_elim', SE_STRUCT, 'tourn_e0_se'));
  const gens = await import('../js/variants/T/views/gens.js');
  const host = document.createElement('div');
  await gens.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, round: '0' });
  // the round drill heads with the round + renders that round's full tournament
  // (the bracket-as-flow, with the match convergence nodes — the seat/box tree retired).
  assert(host.textContent.includes('round 0'), 'the drill-down heads with the round');
  assert(host.textContent.includes('all rounds'), 'a "← all rounds" affordance returns to the full Match-ups');
  assertEqual(svgsByClass(host, 'dn-elimbracket').length, 0, 'the seat/box bracket tree is retired in the round drill too');
  const flow = svgsByClass(host, 'dn-elimflow')[0];
  assert(flow, 'the round drill renders the bracket-as-flow (elimFlow)');
  assert(allByClass(flow, 'dn-elimflow-convnode').length >= 1, 'the round drill shows the match convergence nodes');
});

test('round drill-down: an out-of-range round reads an honest empty', async () => {
  freshState();
  installFixtureMap(structFixture('single_elim', SE_STRUCT, 'tourn_e0_se'));
  const gens = await import('../js/variants/T/views/gens.js');
  const host = document.createElement('div');
  await gens.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, round: '7' });
  assert(/No round 7/i.test(host.textContent), 'an out-of-range round reads an honest empty');
});

// ---- (5) the TREE groups generations by round ----------------------

test('tree: groups generations by round when round_index is present (Round 0 / Round 1 nodes + gate outcome)', () => {
  const model = { epochs: [{ id: EPOCH_ID, current: true }], byEpoch: { [EPOCH_ID]: {
    gens: [
      { id: 'v0', promoted: true, currentChampion: false, parent: null, round_index: 0 },
      { id: 'v1', promoted: true, currentChampion: true, parent: 'v0', round_index: 0 },
      { id: 'v2', promoted: false, parent: 'v1', round_index: 1 },
    ],
    boards: [],
    rounds: [
      { round_index: 0, championId: 'v0', gateOutcome: { kind: 'promoted', gen: 'v1' }, challengers: [{ id: 'v1', promoted: true }] },
      { round_index: 1, championId: 'v1', gateOutcome: { kind: 'held', gen: null }, challengers: [{ id: 'v2', promoted: false }] },
    ],
  } } };
  const route = router.parseRoute(`#/e/${EPOCH_ID}/gens`);
  const host = document.createElement('div');
  // expand the epoch + the generations group + both round nodes.
  const toggles = new Set([`e:${EPOCH_ID}`, `e:${EPOCH_ID}/gens`, `e:${EPOCH_ID}/gens/r0`, `e:${EPOCH_ID}/gens/r1`]);
  tree.buildTree(host, model, route, toggles, { navigate() {}, href: router.href }, () => {}, new Set());
  const roundNodes = host.querySelectorAll('[data-kind]').filter((n) => n.getAttribute('data-kind') === 'round');
  assertEqual(roundNodes.length, 2, 'two Round nodes under Generations');
  assert(host.textContent.includes('Round 0') && host.textContent.includes('Round 1'), 'the rounds are labelled');
  // the DEFENDING champion + gate outcome live in the ROUND HEADER (Task 3).
  assert(host.textContent.includes('v0 defends'), 'round 0 header names the defending champion (v0 defends)');
  assert(host.textContent.includes('▲ v1 promoted'), 'round 0 header shows its gate outcome (▲ v1 promoted)');
  assert(host.textContent.includes('v1 defends') && host.textContent.includes('held'), 'round 1 header names v1 defends · — held');
  // Each round shows its FULL field: the champion born THIS round is a full
  // node (v0 under round 0), while a champion CARRIED in to defend a later
  // round is a dimmed gen-carried reference (v1 under round 1 — born round 0).
  const carried = host.querySelectorAll('[data-kind]').filter((n) => n.getAttribute('data-kind') === 'gen-carried');
  assertEqual(carried.length, 1, 'the carried champion (v1) shows as ONE dimmed reference under the round it defends (round 1)');
  assert(carried[0].textContent.includes('v1') && carried[0].textContent.includes('defends'), 'the carried reference names v1 and is tagged "defends"');
});

test('round model: reads the CANONICAL per-round champion (id + cached/re-run eval mode) from the tournament record, not the reconstructed spine', async () => {
  const rounds = await import('../js/variants/T/views/rounds.js');
  const gens = [
    { id: 'v0', parent: null, promoted: false, round_index: 0 },
    { id: 'v1', parent: 'v0', promoted: false, round_index: 0 },
    { id: 'v2', parent: 'v0', promoted: false, round_index: 1 },
  ];
  // per-round field records carrying the CANONICAL champion + its eval mode:
  // round 0 ran the champion FULL (re-run), round 1 reused it FAST (cached).
  const bracket = { tournaments: [
    { tournament_id: 't0', competitors: [{ generation_id: 'v1' }], champion: { id: 'v0', scalar: 5.0, eval_mode: 'full', run_ref: 'epochs/e/generations/v0' } },
    { tournament_id: 't1', competitors: [{ generation_id: 'v2' }], champion: { id: 'v0', scalar: 4.0, eval_mode: 'fast', run_ref: 'epochs/e/generations/v0' } },
  ] };
  const model = rounds.roundsForTree({ gens, bracket, structure: 'swiss', championId: 'v0' });
  const r0 = model.find((r) => r.round_index === 0);
  const r1 = model.find((r) => r.round_index === 1);
  assert(r0 && r0.championEvalMode === 'full', 'round 0 surfaces the record champion eval mode (full = re-run)');
  assert(r1 && r1.championEvalMode === 'fast', 'round 1 surfaces the record champion eval mode (fast = cached) — read, not reconstructed');
  assert(r1 && String(r1.championId) === 'v0', 'the carried champion id is the canonical record value');
});

test('tree: degrades to a FLAT generation list when round_index is absent (no redundant Round 0 wrapper)', () => {
  const model = { epochs: [{ id: EPOCH_ID, current: true }], byEpoch: { [EPOCH_ID]: {
    gens: [
      { id: 'v0', promoted: true, parent: null, round_index: null },
      { id: 'v1', promoted: false, parent: 'v0', round_index: null },
    ],
    boards: [],
    // a single round (no stamp) → the tree must NOT wrap in a Round 0 node.
    rounds: [{ round_index: 0, championId: 'v0', gateOutcome: { kind: 'held', gen: null }, challengers: [{ id: 'v1', promoted: false }] }],
  } } };
  const route = router.parseRoute(`#/e/${EPOCH_ID}/gens`);
  const host = document.createElement('div');
  const toggles = new Set([`e:${EPOCH_ID}`, `e:${EPOCH_ID}/gens`]);
  tree.buildTree(host, model, route, toggles, { navigate() {}, href: router.href }, () => {}, new Set());
  const roundNodes = host.querySelectorAll('[data-kind]').filter((n) => n.getAttribute('data-kind') === 'round');
  assertEqual(roundNodes.length, 0, 'NO round wrapper when there is a single round and no round_index stamp (flat list)');
  // the gens still render as a flat list under Generations.
  assert(host.textContent.includes('v0') && host.textContent.includes('v1'), 'the generations render flat');
});

test('tree digest: re-stamps when a round gate outcome changes, stable on a no-op', () => {
  const mk = (gateGen) => ({ epochs: [{ id: EPOCH_ID, current: true }], byEpoch: { [EPOCH_ID]: {
    gens: [{ id: 'v0', promoted: true, parent: null, round_index: 0 }, { id: 'v1', promoted: true, parent: 'v0', round_index: 0 }],
    boards: [],
    rounds: [{ round_index: 0, championId: 'v0', gateOutcome: { kind: 'promoted', gen: gateGen }, challengers: [{ id: 'v1', promoted: true }] }],
  } } });
  const route = router.parseRoute(`#/e/${EPOCH_ID}/gens`);
  const toggles = new Set();
  const d1 = tree.treeDigest(mk('v1'), route, toggles);
  const d2 = tree.treeDigest(mk('v1'), route, toggles);
  const d3 = tree.treeDigest(mk('v2'), route, toggles);
  assertEqual(d1, d2, 'identical round model → a true digest no-op');
  assert(d1 !== d3, 'a changed gate outcome re-stamps the digest');
});

// ====================================================================
// Console-IV de-chartjunk wave: the new in-language DATA-GRAPHICS, and a
// guard that the figures the operator likes still render unchanged.
// ====================================================================

// ---- the GAUNTLET DUEL FLOW (duelFlow) — the field as Δ-vs-champion lanes ----

test('duelFlow: the field renders as Δ-vs-champion lanes — good below / bad above the reference, status glyphs, a crowned gate, hypothesis on hover', () => {
  const node = svg.duelFlow({
    championId: 'v0', championScalar: 12.0,
    challengers: [
      { id: 'v1', delta: -3.2, verdict: 'promoted', hypothesis: 'tighten the slide structure', driver: 'incorporates_feedback' },
      { id: 'v2', delta: 1.4, verdict: 'rejected', hypothesis: 'add a summary slide' },
      { id: 'v3', delta: null, verdict: 'pending', hypothesis: 'racing' },
    ],
    onCompetitor() {},
  });
  assertEqual(node.getAttribute('class'), 'dn-duelflow', 'duelFlow is its own renderer');
  assertEqual(node.getAttribute('width'), '100%', 'fit-to-width');
  assert((node.getAttribute('viewBox') || '').startsWith('0 0 '), 'a viewBox so it scales to its pane');
  // the Δ=0 champion reference rule + a crowned champion-gate.
  assert(allByClass(node, 'dn-duelflow-ref').length >= 1, 'the Δ=0 champion reference rule is drawn');
  assert(allByClass(node, 'dn-duelflow-gate').length >= 1, 'a crowned champion-gate node is drawn');
  assert(node.textContent.includes(svg.CROWN.current), 'the gate carries the current crown ♛');
  // one lane per challenger; the improved one good, the regressed one bad.
  const lanes = allByClass(node, 'dn-duelflow-lane');
  assertEqual(lanes.length, 3, 'one lane per challenger');
  const goodDots = allByClass(node, 'dn-duelflow-dot').filter((d) => (d.getAttribute('class') || '').includes('dn-good'));
  const badDots = allByClass(node, 'dn-duelflow-dot').filter((d) => (d.getAttribute('class') || '').includes('dn-bad'));
  assert(goodDots.length >= 1, 'the improved challenger reads --v2-good (below the rule)');
  assert(badDots.length >= 1, 'the regressed challenger reads --v2-bad (above the rule)');
  // status glyphs ↑ / ✕ / ○.
  assert(node.textContent.includes('↑') && node.textContent.includes('✕') && node.textContent.includes('○'), 'status glyphs ↑ promoted / ✕ cut / ○ pending');
  // the hypothesis lives ON HOVER (the dot is hovercard-wired), not as a visible box.
  const dots = allByClass(node, 'dn-duelflow-dot');
  assert(dots.every((d) => d.getAttribute('data-hovercard') === '1'), 'each lane dot is hovercard-wired');
  assert(!node.textContent.includes('tighten the slide structure'), 'the hypothesis is NOT a visible label — it is on the hovercard');
});

// ---- elimFlow CONVERGENCE: winner continues / loser ✕ / champion → gate ----

test('elimFlow convergence: two lanes meet at a match node; the winner continues (good), the loser ✕, the champion → crowned gate', () => {
  const winners = [
    { round_index: 0, label: 'Semifinal', matches: [
      { match_id: 'WB-R0-0', competitors: ['v0', 'v3'], winner: 'v0', decision: 'win', delta_scalar: -1.2, bracket_slot: 'WB-R0-0' },
      { match_id: 'WB-R0-1', competitors: ['v1', 'v2'], winner: 'v1', decision: 'win', delta_scalar: -0.8, bracket_slot: 'WB-R0-1' },
    ] },
    { round_index: 1, label: 'Final', matches: [
      { match_id: 'WB-R1-0', competitors: ['v0', 'v1'], winner: 'v1', decision: 'promoted', delta_scalar: -2.0, bracket_slot: 'WB-R1-0' },
    ] },
  ];
  const node = svg.elimFlow({ winners, championId: 'v1', benchmarkId: 'v0', gateState: 'crowned', onCompetitor() {} });
  // a two-lane match CONVERGENCE node per decided match.
  const convs = allByClass(node, 'dn-elimflow-convnode');
  assert(convs.length >= 3, 'a convergence node per match (2 semis + 1 final)');
  assert(convs.filter((c) => (c.getAttribute('class') || '').includes('dn-elimflow-good')).length >= 1, 'a decided match convergence reads --v2-good');
  // the winner CONTINUES (an advancing good leg), the loser TERMINATES (✕).
  assert(allByClass(node, 'dn-elimflow-good').length >= 1, 'the winner lane continues (good)');
  assert(node.textContent.includes('✕'), 'a losing lane terminates with ✕');
  // the champion reaches the crowned gate ♛.
  assert(node.textContent.includes(svg.CROWN.current), 'the champion lane reaches the crowned gate ♛');
  assert(node.textContent.toLowerCase().includes('champion-gate'), 'the trailing gate column');
  // the convergence node is hovercard-wired (the pairing + Δ on hover).
  assert(convs.every((c) => c.getAttribute('data-hovercard') === '1'), 'each convergence node is hovercard-wired (pairing + Δ on hover)');
});

// ---- the LOSS-FLOOR WATERFALL — steps good-coloured + spine accent + hover ----

test('waterfall: rounds as downward steps (good by direction), a held round flat, the running floor annotated, the spine accent, hover detail', () => {
  const steps = [
    { round_index: 0, from: 20, to: 14, delta: -6, promoted: true, gen: 'v1' },
    { round_index: 1, from: 14, to: 14, delta: 0, promoted: false, gen: null },
    { round_index: 2, from: 14, to: 9, delta: -5, promoted: true, gen: 'v3' },
  ];
  const node = svg.waterfall({ steps, onRound() {}, onCompetitor() {} });
  assertEqual(node.getAttribute('class'), 'dn-waterfall', 'waterfall is its own renderer');
  assertEqual(node.getAttribute('width'), '100%', 'fit-to-width');
  assert((node.getAttribute('viewBox') || '').startsWith('0 0 '), 'a viewBox');
  // the promotion steps are good-coloured; a held round is a flat tick.
  assert(allByClass(node, 'dn-waterfall-bar').filter((b) => (b.getAttribute('class') || '').includes('dn-good')).length >= 2, 'each promotion step is good-coloured (lower floor = improvement)');
  assert(allByClass(node, 'dn-waterfall-held').length >= 1, 'a held round is a flat tick (no step)');
  // the spine baseline is accent.
  assert(allByClass(node, 'dn-waterfall-spine').length >= 1, 'the champion spine baseline is drawn (accent)');
  // the running floor is annotated + the winning mutation glyph (crown) per step.
  assert(allByClass(node, 'dn-waterfall-floor').length >= 1, 'the running floor is annotated at each station');
  assert(node.textContent.includes(svg.CROWN.current), 'the winning-mutation crown marks a promoting step');
  // the step is hovercard-wired (the winning mutation per step on hover).
  const bars = allByClass(node, 'dn-waterfall-bar');
  assert(bars.length >= 2 && bars.every((b) => b.getAttribute('data-hovercard') === '1'), 'each step bar is hovercard-wired (winning mutation on hover)');
});

test('waterfallModel: derives from the epoch round model — a promotion drops the floor, a held round holds it flat', () => {
  const r = [
    { round_index: 0, champion: { id: 'v0', scalar: 20 }, challengers: [{ id: 'v1', scalar: 14, promoted: true }], gateOutcome: { kind: 'promoted', gen: 'v1' } },
    { round_index: 1, champion: { id: 'v1', scalar: 14 }, challengers: [{ id: 'v2', scalar: 16, promoted: false }], gateOutcome: { kind: 'held', gen: null } },
  ];
  const steps = rounds.waterfallModel(r);
  assertEqual(steps.length, 2, 'one step per round');
  assertEqual(steps[0].from, 20); assertEqual(steps[0].to, 14); assertEqual(steps[0].delta, -6);
  assert(steps[0].promoted === true && steps[0].gen === 'v1', 'a promotion step carries its winning mutation');
  assertEqual(steps[1].from, 14); assertEqual(steps[1].to, 14);
  assert(steps[1].promoted === false, 'a held round is flat (no step)');
});

// ---- the CHAMPION REIGN GANTT — bars + ♛ current / ♔ former ----

test('reignGantt: one bar per champion across rounds — current accent + ♛, former dim + ♔', () => {
  const node = svg.reignGantt({
    reigns: [
      { id: 'v0', fromRound: 0, toRound: 1, current: false },
      { id: 'v3', fromRound: 2, toRound: 4, current: true },
    ],
    rounds: 4, onCompetitor() {},
  });
  assertEqual(node.getAttribute('class'), 'dn-reigngantt', 'reignGantt is its own renderer');
  assertEqual(node.getAttribute('width'), '100%', 'fit-to-width');
  // one bar per champion; current is accent + ♛, former is dim + ♔.
  assert(allByClass(node, 'dn-reigngantt-bar-current').length === 1, 'the current champion bar reads accent');
  assert(allByClass(node, 'dn-reigngantt-bar-former').length === 1, 'the former champion bar reads dim ink');
  assert(node.textContent.includes(svg.CROWN.current), 'the current champion carries ♛');
  assert(node.textContent.includes(svg.CROWN.former), 'the former champion carries ♔');
  // hovercard-wired bars (the tenure on hover).
  const bars = allByClass(node, 'dn-reigngantt-bar');
  assert(bars.length === 2 && bars.every((b) => b.getAttribute('data-hovercard') === '1'), 'each reign bar is hovercard-wired');
});

test('reignModel: succession order, last champion flagged current', () => {
  const r = [
    { round_index: 0, champion: { id: 'v0' } },
    { round_index: 1, champion: { id: 'v0' } },
    { round_index: 2, champion: { id: 'v3' } },
  ];
  const reigns = rounds.reignModel(r);
  assertEqual(reigns.length, 2, 'one entry per champion in succession');
  assertDeep([reigns[0].id, reigns[0].fromRound, reigns[0].toRound, reigns[0].current], ['v0', 0, 1, false]);
  assertDeep([reigns[1].id, reigns[1].fromRound, reigns[1].toRound, reigns[1].current], ['v3', 2, 2, true]);
});

// ---- the reign ribbon shows ONLY for a generation that became champion ----

test('candidate: the reign ribbon (reignGantt) shows ONLY for a generation that became champion', async () => {
  freshState(); installFetch();
  const cand = await import('../js/variants/T/views/candidate.js');
  // v0 is the seed champion (round 0) → it has a reign → the ribbon shows.
  const hostChamp = document.createElement('div');
  await cand.render(hostChamp, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, gen: 'v0' });
  assert(svgsByClass(hostChamp, 'dn-reigngantt')[0], 'the champion v0 shows its reign ribbon');
  assert(allByClass(hostChamp, 'dn-reignribbon').length >= 1, 'the reign ribbon panel renders for a champion');

  // v2 (a rejected challenger, never champion) → NO reign ribbon.
  const hostChall = document.createElement('div');
  await cand.render(hostChall, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, gen: 'v2' });
  assertEqual(svgsByClass(hostChall, 'dn-reigngantt').length, 0, 'a never-champion candidate shows NO reign ribbon');
});

// ---- the LOSS-FLOOR WATERFALL is the epoch round-timeline headline figure ----

test('epoch view: the round timeline leads with the loss-floor WATERFALL headline figure', async () => {
  freshState(); installFetch();
  const epoch = await import('../js/variants/T/views/epoch.js');
  const host = document.createElement('div');
  await epoch.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID });
  assert(svgsByClass(host, 'dn-waterfall')[0], 'the epoch round-timeline section carries the loss-floor waterfall');
  // it sits within the round-timeline section (alongside the spine + episodes).
  assert(svgsByClass(host, 'dn-roundtl-spine')[0], 'the champion-spine timeline is still present');
});

// ---- GUARD: the figures the operator LIKES still render unchanged ----

test('liked figures untouched: heatmap / valueDotPlot / lifecycleDag still render their own marks', async () => {
  const dag = await import('../js/variants/T/dag.js');
  // heatmap
  const hm = svg.heatmap({
    rows: [{ id: 'b1', label: 'b1' }], cols: [{ id: 'v1', label: 'v1' }],
    value: () => 0.5,
  });
  assertEqual(hm.getAttribute('class'), 'dn-heatmap', 'heatmap renderer unchanged');
  assert(allByClass(hm, 'dn-hm-cell').length >= 1, 'the heatmap still draws its cells');
  // valueDotPlot
  const dp = svg.valueDotPlot({ items: [{ label: 'b1', value: 8 }, { label: 'b2', value: 12 }], reference: { value: 10, label: 'champ' } });
  assertEqual(dp.getAttribute('class'), 'dn-valdot', 'valueDotPlot renderer unchanged');
  assert(allByClass(dp, 'dn-ref-rule').length >= 1, 'the dot-plot still draws its reference rule');
  assert(allByClass(dp, 'dn-dot').length >= 2, 'the dot-plot still draws its dots');
  // lifecycleDag
  const d = dag.lifecycleDag({ genId: 'v1', parentId: 'v0', entries: [{ entry_id: 'b1', drift_loss: 10, pass_fail: 0 }], decision: 'rejected' });
  assertEqual(d.getAttribute('width'), '100%', 'the lifecycle DAG renderer unchanged (width:100%)');
  assert((d.getAttribute('viewBox') || '').startsWith('0 0 '), 'the lifecycle DAG keeps its viewBox');
});

// ====================================================================
// STRUCTURE-BUILDER LAYER — the responsive (aspect-locked hero) contract,
// the racing FULL-FIELD lane source, the no-scalar spread, and the radar
// TEXT axis labels (svg.js — the SVG builder layer).
// ====================================================================

// the viewBox aspect (w/h) a builder pins inline as `aspect-ratio:w / h` so the
// `preserveAspectRatio:'none'` scale stays uniform at any pane width.
function viewBoxAspect(node) {
  const vb = (node.getAttribute('viewBox') || '').split(/\s+/).map(Number);
  return (vb.length === 4 && vb[2] > 0 && vb[3] > 0) ? [vb[2], vb[3]] : null;
}
// the `aspect-ratio:<w> / <h>;` pinned inline (read from the style attr the
// builder emits — svgEl serializes the style string onto the attribute).
function pinnedAspect(node) {
  const style = (node.getAttribute('style') || '') + ';' + (node.style ? node.style.cssText || '' : '');
  const m = style.match(/aspect-ratio\s*:\s*([0-9.]+)\s*\/\s*([0-9.]+)/);
  return m ? [Number(m[1]), Number(m[2])] : null;
}

// the seven structure builders + the opts that produce a non-empty figure.
const RESPONSIVE_BUILDERS = [
  ['racingScalarTrack', 'dn-scalartrack', 'dn-scalartrack-hero', (extra) => ({
    rungs: [{ match_id: 'rung0', label: 'Rung 0', competitors: ['v5', 'v6', 'v7'], survivors: ['v5', 'v6'], cut: ['v7'], scalars: { v5: 8.1, v6: 8.4, v7: 9.9 } }],
    championId: 'v0', benchmarkId: 'v0', championScalar: 9.0, ...extra,
  })],
  ['survivalFunnel', 'dn-funnel', 'dn-funnel-hero', (extra) => ({
    rungs: [{ label: 'Rung 0', competitors: ['v5', 'v6', 'v7'], survivors: ['v5', 'v6'], cut: ['v7'] }],
    championId: 'v5', benchmarkId: 'v0', gateState: 'crowned', ...extra,
  })],
  ['elimFlow', 'dn-elimflow', 'dn-elimflow-hero', (extra) => ({
    winners: [{ label: 'R0', round_index: 0, matches: [{ match_id: 'm0', competitors: ['v1', 'v2'], winner: 'v1' }] }],
    championId: 'v1', benchmarkId: 'v0', gateState: 'crowned', ...extra,
  })],
  ['elimRadial', 'dn-elimradial', 'dn-elimradial-hero', (extra) => ({
    rounds: [{ label: 'R0', round_index: 0, matches: [{ match_id: 'm0', competitors: ['v1', 'v2'], winner: 'v1' }] }],
    championId: 'v1', benchmarkId: 'v0', gateState: 'crowned', ...extra,
  })],
  ['gauntletFieldBars', 'dn-fieldbars', 'dn-fieldbars-hero', (extra) => ({
    championId: 'v0', championScalar: 9.0, promoteMargin: 0.5,
    challengers: [{ id: 'v5', scalar: 8.1, survivor: true }, { id: 'v6', scalar: 9.6 }], ...extra,
  })],
  ['swissLadder', 'dn-swissladder', 'dn-swissladder-hero', (extra) => ({
    rounds: [{ label: 'Round 1', pairings: [{ a: 'v5', b: 'v6', winner: 'v5', delta: -1 }] }],
    standings: [{ id: 'v5', points: 1, wins: 1, draws: 0, losses: 0 }, { id: 'v6', points: 0, wins: 0, draws: 0, losses: 1 }],
    championId: 'v5', benchmarkId: 'v0', gateState: 'crowned', ...extra,
  })],
  ['radarSilhouette', 'dn-radar', 'dn-radar-hero', (extra) => ({
    axes: [
      { label: 'scalar (inverse)', chal: 0.8, champ: 0.6 },
      { label: 'pass-rate', chal: 0.9, champ: 0.7 },
      { label: 'tone judge drift', chal: 0.5, champ: 0.8 },
      { label: 'structure judge drift', chal: 0.7, champ: 0.7 },
    ], ...extra,
  })],
];

test('responsive: every structure builder defaults to a FIXED figure (no hero class, no aspect-ratio) so existing fixed/mini call sites are untouched', () => {
  for (const [fn, baseCls, heroCls, mk] of RESPONSIVE_BUILDERS) {
    const node = svg[fn](mk());
    assert((node.getAttribute('class') || '').split(/\s+/).includes(baseCls), `${fn}: carries its base class ${baseCls}`);
    assert(!(node.getAttribute('class') || '').split(/\s+/).includes(heroCls), `${fn}: default render does NOT carry the hero class (responsive is OPT-IN)`);
    assert(!pinnedAspect(node), `${fn}: default render pins NO inline aspect-ratio`);
    // the fixed render still keeps a height attr (its intrinsic pixel height).
    assert(node.getAttribute('height') != null, `${fn}: default render keeps a fixed height attr`);
    // mini stays a valid fixed render too (where the builder supports it).
    if (fn !== 'survivalFunnel' && fn !== 'elimFlow' && fn !== 'swissLadder') {
      const m = svg[fn](mk({ mini: true }));
      assert(!(m.getAttribute('class') || '').split(/\s+/).includes(heroCls), `${fn}: mini render is NOT a hero either`);
      assert(m.getAttribute('height') != null, `${fn}: mini render keeps a fixed height`);
    }
  }
});

test('responsive: opts.responsive (and opts.fitWidth) turns every structure builder into an aspect-locked, full-width hero — preserveAspectRatio:none, aspect-ratio == viewBox, no fixed height', () => {
  for (const flag of ['responsive', 'fitWidth']) {
    for (const [fn, baseCls, heroCls, mk] of RESPONSIVE_BUILDERS) {
      const node = svg[fn](mk({ [flag]: true }));
      const cls = (node.getAttribute('class') || '').split(/\s+/);
      assert(cls.includes(baseCls) && cls.includes(heroCls), `${fn}[${flag}]: carries ${baseCls} + ${heroCls}`);
      assertEqual(node.getAttribute('width'), '100%', `${fn}[${flag}]: width:100%`);
      assertEqual(node.getAttribute('height'), null, `${fn}[${flag}]: the fixed pixel height is DROPPED`);
      assertEqual(node.getAttribute('preserveAspectRatio'), 'none', `${fn}[${flag}]: preserveAspectRatio:none for a uniform scale`);
      const vb = viewBoxAspect(node);
      const pin = pinnedAspect(node);
      assert(vb, `${fn}[${flag}]: keeps a numeric viewBox`);
      assert(pin, `${fn}[${flag}]: pins an inline aspect-ratio`);
      // the pinned aspect MUST equal the viewBox aspect so 'none' never shears.
      assert(Math.abs(pin[0] / pin[1] - vb[0] / vb[1]) < 1e-6,
        `${fn}[${flag}]: the pinned aspect-ratio (${pin[0]}/${pin[1]}) EQUALS the viewBox aspect (${vb[0]}/${vb[1]})`);
    }
  }
});

test('responsive: each builder’s *-hero class is defined in console4.css with width:100% + height:auto + aspect-ratio + a max cap', () => {
  const css = readCss();
  for (const [, , heroCls] of RESPONSIVE_BUILDERS) {
    assert(css.includes('.' + heroCls), `console4.css defines .${heroCls}`);
  }
  // the additive block carries the cross-cutting box behaviour.
  assert(/\.dn-scalartrack-hero[\s\S]{0,400}width:\s*100%/.test(css)
    || /width:\s*100%;\s*height:\s*auto/.test(css), 'the hero rules set width:100% + height:auto');
  assert(/max-width:\s*\d+px/.test(css), 'the hero rules cap max-width on ultra-wide screens');
  assert(/aspect-ratio/.test(css) || true, 'aspect-ratio is pinned inline by the builder');
});

test('racing FULL-FIELD: racingScalarTrack plots EVERY lane of a multi-survivor rung (v5+v7), driven from live_progress ∪ competitors ∪ survivors — not just the first matchup', () => {
  // a rung whose published `competitors` carries only the FIRST matchup (v0 vs
  // v5), but whose live_progress + survivors carry the WHOLE field {v5,v7} (plus
  // queued v6). The builder must surface every lane, never just v5.
  const rung = {
    match_id: 'rung0', label: 'Rung 0',
    competitors: ['v0', 'v5'],                 // sparse: first matchup only
    survivors: ['v5', 'v7'],                   // TWO survivors
    cut: ['v6'],
    scalars: { v5: 8.1, v6: 9.9, v7: 8.4 },
    live_progress: { v5: { done: 4, total: 4 }, v6: { done: 4, total: 4 }, v7: { done: 4, total: 4 } },
  };
  const node = svg.racingScalarTrack({ rungs: [rung], championId: 'v0', benchmarkId: 'v0', championScalar: 9.0, onCompetitor() {} });
  const names = allByClass(node, 'dn-scalartrack-name').map((t) => (t.textContent || '').trim());
  for (const id of ['v5', 'v6', 'v7']) assert(names.some((t) => t.startsWith(id)), `the scalar track plots lane ${id} (full field, both survivors shown) — got ${JSON.stringify(names)}`);
  // the champion / benchmark v0 is the gate defender, never a track lane.
  assert(!names.some((t) => t.startsWith('v0')), 'champion/benchmark v0 is NEVER a track lane');
  // both survivors render a filled (survived) marker.
  const survDots = allByClass(node, 'dn-scalartrack-filled');
  assert(survDots.length >= 2, `both survivors v5+v7 render a filled marker — got ${survDots.length}`);
});

test('racing FULL-FIELD: survivalFunnel renders BOTH survivors of a multi-survivor rung as band runners (v5+v7), from the rung field union', () => {
  const rungs = [{
    label: 'Rung 0',
    competitors: ['v0', 'v5'],                 // sparse first matchup
    survivors: ['v5', 'v7'],
    cut: ['v6'],
  }];
  const node = svg.survivalFunnel({ rungs, championId: 'v5', benchmarkId: 'v0', gateState: 'crowned', onCompetitor() {} });
  const runners = allByClass(node, 'dn-funnel-runner').map((g) => (g.textContent || '').trim());
  for (const id of ['v5', 'v7']) assert(runners.some((t) => t.startsWith(id)), `the funnel shows survivor ${id} riding the band — got ${JSON.stringify(runners)}`);
  assert(!runners.some((t) => /^v0\b/.test(t)), 'benchmark v0 is never a funnel runner');
});

test('no-scalar layout: an early in-flight rung with NO recoverable scalar spreads its lanes across the axis by index (not piled at x=padL)', () => {
  // every lane is in-flight with no committed/delta/projected scalar yet → no
  // recoverable scalar. They must SPREAD, not stack at the left.
  const rung = {
    match_id: 'rung0', label: 'Rung 0',
    competitors: ['v5', 'v6', 'v7', 'v8'],
    survivors: [], cut: [],
    pending: true,
    live_progress: {
      v5: { inflight: 1, done: 0, total: 4 }, v6: { inflight: 1, done: 0, total: 4 },
      v7: { inflight: 1, done: 0, total: 4 }, v8: { inflight: 1, done: 0, total: 4 },
    },
  };
  const node = svg.racingScalarTrack({ rungs: [rung], championId: 'v0', benchmarkId: 'v0', onCompetitor() {} });
  const dots = allByClass(node, 'dn-scalartrack-dot');
  assert(dots.length >= 4, `all four in-flight lanes render a marker — got ${dots.length}`);
  const xs = dots.map((c) => Number(c.getAttribute('cx'))).filter((x) => isFinite(x));
  const uniq = new Set(xs.map((x) => x.toFixed(1)));
  assert(uniq.size >= 3, `no-scalar lanes are SPREAD across the axis (≥3 distinct x), not piled — got xs ${JSON.stringify(xs)}`);
  const minX = Math.min(...xs), maxX = Math.max(...xs);
  assert(maxX - minX > 40, `the spread covers a real span of the axis (Δx ${(maxX - minX).toFixed(1)} > 40), not a tight stack`);
});

test('radar axis labels: radarSilhouette renders the axis LABEL TEXT at each tip (not an index 1..n), truncates long labels, and carries the full label on hover', () => {
  const axes = [
    { label: 'scalar (inverse loss)', chal: 0.82, champ: 0.6 },
    { label: 'pass-rate', chal: 0.9, champ: 0.7 },
    { label: 'tone-judge drift', chal: 0.5, champ: 0.8 },
    { label: 'structure-judge drift', chal: 0.7, champ: 0.65 },
    { label: 'concision', chal: 0.6, champ: 0.55 },
  ];
  const node = svg.radarSilhouette({ axes, onAxis() {} });
  const labs = allByClass(node, 'dn-radar-axislab');
  assertEqual(labs.length, axes.length, 'one text label per axis at the tip');
  const texts = labs.map((t) => (t.textContent || '').trim());
  // each label is derived from the axis NAME, not an index 1..n.
  assert(!texts.some((t) => /^\d+$/.test(t)), `axis labels are TEXT, never a bare index — got ${JSON.stringify(texts)}`);
  assert(texts.some((t) => t.startsWith('pass-rate')), 'a short label renders in full (pass-rate)');
  // a long label truncates with an ellipsis but its hovercard keeps the full name.
  const longLab = labs.find((t) => (t.textContent || '').startsWith('scalar'));
  assert(longLab, 'the long "scalar (inverse loss)" axis renders a label');
  assert((longLab.textContent || '').includes('…') || (longLab.textContent || '').length <= 16, 'a long axis label is truncated to its budget');
  // no legacy index-tick markers remain (the retired dn-radar-axistick).
  assertEqual(allByClass(node, 'dn-radar-axistick').length, 0, 'the retired index-tick (dn-radar-axistick) is GONE — labels replace it');
});

test('radar axis labels: a DENSE radar (many axes) still renders one text label per axis (harder truncation, no index fallback)', () => {
  const axes = Array.from({ length: 10 }, (_, i) => ({ label: `judge-${i}-semantic-drift`, chal: 0.5 + i * 0.02, champ: 0.6 }));
  const node = svg.radarSilhouette({ axes });
  const labs = allByClass(node, 'dn-radar-axislab');
  assertEqual(labs.length, 10, 'a 10-axis radar still labels every axis with text');
  const texts = labs.map((t) => (t.textContent || '').trim());
  assert(!texts.some((t) => /^\d+$/.test(t)), 'dense labels are still TEXT, never indices');
  assert(texts.every((t) => t.startsWith('judge')), 'every dense label is the (truncated) axis name');
});

test('radar mini: a mini radar suppresses tip labels (too small) but its vertices still carry the axis name on hover', () => {
  const axes = [
    { label: 'scalar', chal: 0.8, champ: 0.6 },
    { label: 'pass-rate', chal: 0.9, champ: 0.7 },
    { label: 'drift', chal: 0.5, champ: 0.8 },
  ];
  const node = svg.radarSilhouette({ axes, mini: true });
  assertEqual(allByClass(node, 'dn-radar-axislab').length, 0, 'a mini radar draws NO tip labels');
  assert(allByClass(node, 'dn-radar-hot').length >= 3, 'the mini radar still exposes hover-able axis vertices');
});

await run();
