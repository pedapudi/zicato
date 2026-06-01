// test/variant_h.test.mjs — Variant H ("Atlas II") unit tests.
//
// H is the conservative refinement of E: it keeps E's exact flow, fixes the
// named bugs, and adds two views + a three-theme switcher. These tests pin
// the round-3 mandates:
//   * digest-gated repaint — a heartbeat re-dispatch with identical data does
//     NOT rebuild the DOM;
//   * a COLD deep-link run/transcript renders content (never an empty panel);
//   * the NEW mutation-site × generation matrix renders + drills to a patch diff;
//   * the NEW ACM-style report renders eyebrow/title/meta/abstract/sections;
//   * the Tufte Sankey is fit-to-width — NO pan/zoom viewport wrapper, no
//     overflow scroller around it;
//   * lineage bumps are non-colliding AND clickable → candidate;
//   * the three-theme switch applies (data-h-theme on the root).

import { installDom, test, run, assert, assertEqual } from './harness.mjs';

installDom();

const router = await import('../js/variants/H/router.js');
const svg = await import('../js/variants/H/svg.js');
const sankey = await import('../js/variants/H/diagram/sankey.js');
const data = await import('../js/variants/H/data.js');
const dom = await import('../js/core/dom.js');

const EPOCH_ID = '2026-05-30_e0';
const FIXTURE = {
  '/api/workspace': {
    current_epoch_id: EPOCH_ID,
    epochs: [{ epoch_id: EPOCH_ID, goal: 'Improve the presentation agent.', best_scalar: 70.94, generation_count: 3, promoted_count: 1, closed: false }],
    sparkline: [{ scalar: 88.1 }, { scalar: 75.0 }, { scalar: 70.94 }],
  },
  '/api/health-report': { epoch_id: EPOCH_ID, healthy: true, findings: [] },
  '/api/score-trajectory': { points: [{ generation_id: 'v0', scalar: 70.94 }, { generation_id: 'v1', scalar: 146.65 }, { generation_id: 'v2', scalar: 72.45 }] },
  '/api/lineage': { generations: [
    { generation_id: 'v0', epoch_id: EPOCH_ID, parent_generation_id: '', promoted: true },
    { generation_id: 'v1', epoch_id: EPOCH_ID, parent_generation_id: 'v0', promoted: false },
    { generation_id: 'v2', epoch_id: EPOCH_ID, parent_generation_id: 'v0', promoted: false },
  ] },
  '/api/epoch': {
    epoch_id: EPOCH_ID, closed: false, goal: 'Improve the presentation agent.', brief: '# Goal\nMake it crisper.\n\n- one\n- two',
    board: [
      { id: 'waffles_single', kind: 'single_turn', input_preview: 'Make a presentation about waffles.', expectation_kind: 'predicate', budget_s: 180, weight: 1, tags: ['smoke'] },
      { id: 'picky_stakeholder_emulated', kind: 'multi_turn_emulated', input_preview: null, expectation_kind: null, budget_s: 360, weight: 1, tags: ['hard'] },
    ],
    experiments: [
      { generation_id: 'v0', parent_generation_id: '', outcome: { decision: 'promoted', scalar_score: 70.94 } },
      { generation_id: 'v1', parent_generation_id: 'v0', outcome: { decision: 'rejected', scalar_score: 146.65 } },
      { generation_id: 'v2', parent_generation_id: 'v0', outcome: { decision: 'rejected', scalar_score: 72.45 } },
    ],
  },
  '/api/tournaments': { epoch_id: EPOCH_ID, champion_lineage: ['v0'], matchups: [
    { champion: 'v0', challenger: 'v1', decision: 'rejected', delta_scalar: 75.71 },
    { champion: 'v0', challenger: 'v2', decision: 'rejected', delta_scalar: 1.51 },
  ] },
  // the mutation index (new-view source)
  [`/api/mutations/${EPOCH_ID}`]: {
    epoch_id: EPOCH_ID,
    generations: ['v0', 'v1', 'v2'],
    mutations: [
      { mutation_id: 'researcher_instruction', kind: 'span', file: '/ws/agent/agent.py', role: 'researcher', line_start: 40, line_end: 58, patched_by: [{ generation_id: 'v2', patch_id: 'p2', op: 'replace', rationale: 'tighten topicality' }], patched_generation_ids: ['v2'] },
      { mutation_id: 'web_developer_instruction', kind: 'span', file: '/ws/agent/agent.py', role: 'web_developer', line_start: 60, line_end: 80, patched_by: [{ generation_id: 'v1', patch_id: 'p1', op: 'replace', rationale: 'slide structure' }], patched_generation_ids: ['v1'] },
      { mutation_id: 'reviewer_instruction', kind: 'span', file: '/ws/agent/agent.py', role: 'reviewer', line_start: 90, line_end: 100, patched_by: [], patched_generation_ids: [] },
    ],
  },
  [`/api/files/${EPOCH_ID}/v1/patches`]: { epoch_id: EPOCH_ID, generation_id: 'v1', patches: [
    { id: 'p1', mutation_id: 'web_developer_instruction', op: 'replace', new_content: 'Produce slide-shaped <section> chunks.\nKeep each slide focused.', rationale: 'slide structure' },
  ] },
  [`/api/files/${EPOCH_ID}/v2/patches`]: { epoch_id: EPOCH_ID, generation_id: 'v2', patches: [
    { id: 'p2', mutation_id: 'researcher_instruction', op: 'replace', new_content: 'Stay strictly on topic. No tangents.', rationale: 'tighten topicality' },
  ] },
  // the ACM analysis (new-view source)
  [`/api/epoch/${EPOCH_ID}/analysis`]: {
    epoch_id: EPOCH_ID,
    analysis_md: [
      '<!-- EYEBROW -->',
      'Zicato improvement campaign · epoch analysis report',
      '',
      '# e0',
      '',
      '<!-- META -->',
      '**Epoch id**: `2026-05-30_e0`  ',
      '**Status**: in progress  ',
      '**Generations**: 3 attempted · 1 promoted · 2 rejected',
      '',
      '## Abstract',
      '',
      'This epoch evaluated three generations against the presentation board.',
      '',
      '## Methodology',
      '',
      '### Lineage diagram',
      '',
      '<!-- FIGURE:lineage -->',
      '',
      'Caption: Lineage diagram — promoted spine and rejected branches.',
      '',
      '| gen | scalar | decision |',
      '| --- | --- | --- |',
      '| `v0` | +0.000 | baseline |',
      '| `v1` | +75.71 | rejected |',
      '',
      '<!-- CALLOUT:KEY OBSERVATION -->',
      'Both challengers regressed; the seed v0 holds the crown.',
      '',
      '---',
      '',
      '_Regenerated by zicato from board.jsonl and friends._',
    ].join('\n'),
    analysis_html_inline: '', analysis_html_available: false,
  },
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
  { entry_id: 'picky_stakeholder_emulated', run_id: 'run_v2_picky', drift_loss: 110.0, pass_fail: 0, runtime_ms: 360000, wall_clock_budget_exceeded: true },
] };
FIXTURE[`/api/round/${EPOCH_ID}/v0/v1/gate`] = { decision: 'rejected', delta_scalar: 75.71, reason: 'challenger regressed' };
FIXTURE[`/api/round/${EPOCH_ID}/v0/v2/gate`] = { decision: 'rejected', delta_scalar: 1.51, reason: 'challenger regressed' };
FIXTURE[`/api/run/${EPOCH_ID}/v1/waffles_single/expectations`] = { outcomes: [{ kind: 'predicate', passed: false, detail: 'predicate returned False', judge_name: null, score: null }] };
FIXTURE[`/api/run/${EPOCH_ID}/v1/waffles_single/per-judge`] = { judges: [{ judge_name: 'incorporates_feedback', weighted_loss: 27.0, raw_loss: 27.0, run_count: 1, weight: 1.0 }] };
FIXTURE['/api/conversation/run_v1_waffles'] = {
  turns: [
    { seq: 0, role: 'user', agent: 'operator', text: 'Make a presentation about waffles.' },
    { seq: 1, role: 'agent', agent: 'coordinator', text: 'Drafting an outline now.', tool_calls: [{ name: 'write_slide' }] },
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

const ctx = { navigate() {}, href: router.href };

// ---- router: keeps E's IA + adds the two new routes -----------------

test('router: parses the H-prefixed hashes incl. the two new views', () => {
  assertEqual(router.parseRoute('#/H/').view, 'home');
  assertEqual(router.parseRoute('#/H/epoch').view, 'epoch');
  assertEqual(router.parseRoute('#/H/matchups').view, 'matchups');
  const m = router.parseRoute('#/H/mutations/researcher_instruction');
  assertEqual(m.view, 'mutations');
  assertEqual(m.params.mutationId, 'researcher_instruction');
  const r = router.parseRoute('#/H/report/' + EPOCH_ID);
  assertEqual(r.view, 'report');
  assertEqual(r.params.epochId, EPOCH_ID);
  const c = router.parseRoute('#/H/candidate/v1/waffles_single');
  assertEqual(c.view, 'candidate');
  assertEqual(c.params.entry, 'waffles_single');
});

test('router: a foreign / empty hash defaults to home', () => {
  assertEqual(router.parseRoute('').view, 'home');
  assertEqual(router.parseRoute('#/E/epoch').view, 'home', 'an E hash is foreign to H');
});

// ---- lineage bumps: non-colliding + clickable -----------------------

test('lineage bumps de-collides coincident challengers (v1/v2) and is clickable', () => {
  // v1 and v2 share the rejected lane; de-collision keeps their labels apart.
  let clicked = null;
  const node = svg.bumps({
    width: 640, height: 180,
    nodes: [
      { id: 'v0', x: 0, promoted: true, parent: null },
      { id: 'v1', x: 1, promoted: false, parent: 'v0' },
      { id: 'v2', x: 1, promoted: false, parent: 'v0' },
    ],
    onClick: (n) => { clicked = n.id; },
  });
  assertEqual(node.localName, 'svg');
  const dots = node.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').startsWith('d-bump-node'));
  assertEqual(dots.length, 3, 'one node per generation');
  // The two rejected siblings land on the SAME y (the challenger lane) but
  // are distinct, addressable nodes (no overdraw / no merge).
  dots[1].dispatchEvent({ type: 'click' });
  assert(clicked === 'v1' || clicked === 'v2', 'a node click fires onClick → candidate');
});

test('decollide keeps a minimum gap between coincident positions', () => {
  // three items at the same value must be pushed at least minGap apart.
  const y = (v) => v; // identity scale
  const out = svg.decollide([{ v: 50 }, { v: 50 }, { v: 50 }], y, 13, 0, 200);
  out.sort((a, b) => a - b);
  assert(out[1] - out[0] >= 12.9, 'gap 0→1 ≥ minGap');
  assert(out[2] - out[1] >= 12.9, 'gap 1→2 ≥ minGap');
});

// ---- Tufte Sankey: fit-to-width, NO pan/zoom viewport ---------------

test('tufteSankey renders thin flows + per-entry effect nodes, sized to width', () => {
  const flow = sankey.buildCandidateFlow('v1', FIXTURE[`/api/generation/${EPOCH_ID}/v1/per-entry`].entries);
  const node = sankey.tufteSankey({ width: 880, flow, onEntry() {} });
  assertEqual(node.localName, 'svg');
  // The svg width matches the requested container width (fit-to-width).
  assertEqual(node.getAttribute('width'), '880', 'svg laid out to the container width');
  // One effect node per board entry, click-wired.
  const effects = node.querySelectorAll('[data-cz]').filter((n) => n.getAttribute('data-cz') === 'sankey-effect');
  assertEqual(effects.length, 2, 'one effect node per board entry');
  // The flows are STROKED thin paths (data-ink), not filled ribbons.
  const flows = node.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').startsWith('hs-flow'));
  assert(flows.length >= 2, 'thin stroked flows present');
});

test('candidate view: the Sankey is fit-to-width — NO pan/zoom viewport / overflow scroller', async () => {
  freshState();
  installFetch();
  const cand = await import('../js/variants/H/views/candidate.js');
  const host = document.createElement('div');
  await cand.render(host, ctx, { gen: 'v1' });
  const sankeySvg = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'hs-sankey')[0];
  assert(sankeySvg, 'the Tufte sankey rendered');
  // Walk up to the panel: it must carry .d-fit (fit-to-width) and must NOT
  // be wrapped in an overflow scroller (no inline overflow:auto/scroll).
  let n = sankeySvg.parentNode;
  let panel = null;
  while (n) {
    const cls = n.getAttribute && (n.getAttribute('class') || '');
    if (cls && cls.includes('d-panel')) { panel = n; break; }
    n = n.parentNode;
  }
  assert(panel, 'the sankey lives in a panel');
  assert((panel.getAttribute('class') || '').includes('d-fit'), 'the panel is fit-to-width (.d-fit)');
  // No ancestor of the sankey declares an overflow scroller.
  let anc = sankeySvg.parentNode;
  while (anc) {
    const style = (anc.getAttribute && anc.getAttribute('style')) || '';
    assert(!/overflow[-xy]*\s*:\s*(auto|scroll)/i.test(style), 'no pan/zoom overflow scroller wraps the sankey');
    anc = anc.parentNode;
  }
});

// ---- digest-gated repaint: identical data / heartbeat = no rebuild ---

test('candidate view: a re-render with identical data does NOT rebuild the DOM', async () => {
  freshState();
  installFetch();
  const cand = await import('../js/variants/H/views/candidate.js');
  const host = document.createElement('div');
  await cand.render(host, ctx, { gen: 'v1' });
  assert(host.children.length > 0, 'candidate painted content');
  const digest1 = host.getAttribute('data-e-digest');
  const writes1 = host.innerHTMLWriteCount();
  const firstChild = host.firstChild;
  await cand.render(host, ctx, { gen: 'v1' });
  assertEqual(host.getAttribute('data-e-digest'), digest1, 'digest unchanged on heartbeat re-dispatch');
  assert(host.firstChild === firstChild, 'host not rebuilt (same node identity)');
  assertEqual(host.innerHTMLWriteCount(), writes1, 'no innerHTML writes on the no-op repaint');
});

// ---- the transcript link is a themed button-like link (E bug fix) ---

test('candidate drill-down: the transcript affordance is a themed button-like link', async () => {
  freshState();
  installFetch();
  const cand = await import('../js/variants/H/views/candidate.js');
  const host = document.createElement('div');
  await cand.render(host, ctx, { gen: 'v1', entry: 'waffles_single' });
  const link = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'h-link-btn')[0];
  assert(link, 'the transcript link uses the themed .h-link-btn (not an unstyled anchor)');
  assert((link.getAttribute('href') || '').includes('/H/run/v1/waffles_single'), 'links into the run transcript');
});

// ---- (b) cold deep-link transcript renders content, not empty -------

test('run view: a COLD deep-link fetches the conversation and renders the transcript', async () => {
  freshState();
  installFetch();
  const runView = await import('../js/variants/H/views/run.js');
  const host = document.createElement('div');
  await runView.render(host, ctx, { gen: 'v1', entry: 'waffles_single' });
  const scroller = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'e-transcript')[0];
  assert(scroller, 'the transcript scroll container rendered');
  assert(host.textContent.includes('Drafting an outline'), 'turn text rendered from /api/conversation');
  assert(host.textContent.includes('omitted the requested structure'), 'drift annotation rendered');
});

// ---- NEW view: mutation-site × generation matrix + drill-down -------

test('mutations view: renders the site × generation matrix and drills to a patch diff', async () => {
  freshState();
  installFetch();
  const view = await import('../js/variants/H/views/mutations.js');
  const host = document.createElement('div');
  await view.render(host, ctx, {});
  const matrix = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'hm-matrix')[0];
  assert(matrix, 'the mutation matrix table rendered');
  const patched = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('hm-patched'));
  assert(patched.length >= 2, 'patched cells present (v1→web_developer, v2→researcher)');
  // Drill into a site → its patch diff card.
  freshState();
  await view.render(host, ctx, { mutationId: 'researcher_instruction' });
  const card = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'hd-card')[0];
  assert(card, 'the patch diff card rendered for the selected site');
  assert(host.textContent.includes('Stay strictly on topic'), 'the realized patch content is shown as a diff');
});

// ---- NEW view: the ACM-style epoch publication ----------------------

test('report view: renders the ACM paper — eyebrow, title, meta, abstract, sections, figure', async () => {
  freshState();
  installFetch();
  const view = await import('../js/variants/H/views/report.js');
  const host = document.createElement('div');
  await view.render(host, ctx, { epochId: EPOCH_ID });
  const eyebrow = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'hp-eyebrow')[0];
  const title = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'hp-title')[0];
  const abstract = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'hp-abstract')[0];
  assert(eyebrow && host.textContent.includes('improvement campaign'), 'eyebrow rendered');
  assert(title, 'title rendered');
  assert(abstract && host.textContent.includes('evaluated three generations'), 'abstract rendered');
  // a live Tufte figure (the lineage bumps) is embedded inline.
  const fig = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'hp-figure')[0];
  assert(fig, 'an inline figure block rendered');
  const bumps = fig.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'd-bumps');
  assert(bumps.length === 1, 'the lineage bumps figure is the live Tufte chart');
  // a markdown table rendered as a typeset table.
  const table = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'hp-table')[0];
  assert(table, 'a markdown table rendered');
});

test('report view: an epoch with no analysis_md degrades to an honest not-yet block', async () => {
  freshState();
  globalThis.fetch = async (path) => {
    if (path === '/api/epoch') return { ok: true, json: async () => ({ epoch_id: EPOCH_ID }) };
    if (path === `/api/epoch/${EPOCH_ID}/analysis`) return { ok: true, json: async () => ({ epoch_id: EPOCH_ID, analysis_md: '' }) };
    return { ok: true, json: async () => ({}) };
  };
  const view = await import('../js/variants/H/views/report.js');
  const host = document.createElement('div');
  await view.render(host, ctx, { epochId: EPOCH_ID });
  assert(host.textContent.toLowerCase().includes('not been generated'), 'honest not-yet block when no report exists');
});

// ---- the three-theme switch applies ---------------------------------

test('theme switch sets data-h-theme on the variant root and persists', async () => {
  freshState();
  installFetch();
  globalThis.__ATLAS_H_NO_AUTOBOOT__ = true;
  // The shell reads the global `location` (a browser global) and registers a
  // hashchange listener + an SSE-driven re-dispatch; give it a real-enough
  // global location so a deferred dispatch resolves against the fixtures.
  const loc = { hash: '#/H/', search: '' };
  globalThis.location = loc;
  globalThis.window.location = loc;
  globalThis.HashChangeEvent = globalThis.HashChangeEvent || function () { return { type: 'hashchange' }; };
  // Stub EventSource so connectSSE neither throws nor schedules infinite
  // reconnect timers (which would keep the test process alive).
  class FakeEventSource { constructor() { this.readyState = 1; } addEventListener() {} close() {} }
  FakeEventSource.CLOSED = 2;
  globalThis.EventSource = FakeEventSource;
  const shell = await import('../js/variants/H/shell.js');
  const root = document.createElement('div');
  root.id = 'variant-root';
  document.body.appendChild(root);
  shell.mountShell(root);
  // Default theme is solarized-dark.
  assertEqual(root.getAttribute('data-h-theme'), 'solarized-dark', 'H defaults to solarized-dark');
  assertEqual(shell.currentTheme(), 'solarized-dark');
  // Switching applies the new theme + persists it.
  shell.applyTheme('monokai');
  assertEqual(root.getAttribute('data-h-theme'), 'monokai', 'switch applies monokai');
  assertEqual(globalThis.window.localStorage.getItem('zicato:variant-H:theme'), 'monokai', 'theme persisted');
  shell.applyTheme('solarized-light');
  assertEqual(root.getAttribute('data-h-theme'), 'solarized-light', 'switch applies solarized-light');
  // A bogus theme falls back to the default.
  shell.applyTheme('not-a-theme');
  assertEqual(root.getAttribute('data-h-theme'), 'solarized-dark', 'bogus theme → default');
});

await run();
