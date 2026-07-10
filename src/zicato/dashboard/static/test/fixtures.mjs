// test/fixtures.mjs — shared fixtures + helpers for the variant_t_*.test.mjs
// suite (the mechanical split of the former variant_t.test.mjs monolith).
//
// Everything here is byte-moved from that file: the module handles, the
// FIXTURE map + fetch installers, and every helper/fixture that more than
// one split file references. Each test file calls installDom() BEFORE
// dynamically importing this module, so the js/ module imports below only
// ever evaluate against an installed DOM (same order as the monolith).

import { roundTimelineFromFixtures, racingFieldFromFixtures, racingFieldFromBracket, attachElimStates } from './mock_server.mjs';
export { roundTimelineFromFixtures, racingFieldFromFixtures, racingFieldFromBracket };

export const router = await import('../js/router.js');
export const svg = await import('../js/svg.js');
export const ui = await import('../js/ui.js');
export const shell = await import('../js/shell.js');
export const data = await import('../js/data.js');
export const tree = await import('../js/tree.js');
export const compare = await import('../js/compare.js');
export const livestatus = await import('../js/livestatus.js');
export const coreState = await import('../js/core/state.js');
export const { bus } = await import('../js/core/bus.js');
export const { svgEl } = await import('../js/core/dom.js');
export const rounds = await import('../js/rounds.js');
export const dag = await import('../js/dag.js');
export const hovercard = await import('../js/hovercard.js');
export const live = await import('../js/live.js');
export const STRUCT = await import('../js/views/structure.js');

export const EPOCH_ID = '2026-05-30_e0';

// Stamp a FRESH `ts` (the server's ONE typed ms-epoch liveness timestamp) on
// a heartbeat fixture so the live-status staleness gate (deriveLiveStatus)
// reads it as a live orchestrator pulse. A real heartbeat payload always
// carries the stamp; these UI fixtures elide it, and a heartbeat with no
// ageable timestamp reads STALE (not live). Respects an explicit `ts`
// already on the object (e.g. a deliberately-stale fixture).
export function freshHb(hb) {
  if (hb && hb.ts == null) {
    return { ...hb, ts: Date.now() };
  }
  return hb;
}


export const FIXTURE = {
  '/api/epoch': {
    epoch_id: EPOCH_ID, closed: false, goal: 'Make the presentation agent crisper.',
    // the REIGNING champion — the server-stamped pointer the views read
    // (never re-scanned from the generation list client-side).
    current_champion: 'v0',
    experiments: [
      // each record carries the CANONICAL server-stamped decision surface
      // (`decision` + tri-state `promoted`) beside the raw outcome.
      { generation_id: 'v0', parent_generation_id: '', outcome: { decision: 'baseline' }, decision: 'baseline', promoted: false },
      { generation_id: 'v1', parent_generation_id: 'v0', outcome: { decision: 'rejected' }, decision: 'rejected', promoted: false },
      { generation_id: 'v2', parent_generation_id: 'v0', outcome: { decision: 'rejected' }, decision: 'rejected', promoted: false },
    ],
    board: [
      { entry_id: 'waffles_single', kind: 'single_turn', input_preview: 'Make a presentation about waffles.', expectation_kind: 'predicate', budget_s: 180, weight: 1, tags: ['smoke'] },
      { entry_id: 'picky_stakeholder_emulated', kind: 'multi_turn_emulated', input_preview: null, expectation_kind: null, budget_s: 360, weight: 1, tags: ['hard'] },
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
  { entry_id: 'waffles_single', run_id: 'run_v0_waffles', drift_loss: 60.5, pass_fail: false, runtime_ms: 180000, wall_clock_budget_exceeded: false },
  { entry_id: 'picky_stakeholder_emulated', run_id: 'run_v0_picky', drift_loss: 105.5, pass_fail: false, runtime_ms: 360000, wall_clock_budget_exceeded: true },
] };
FIXTURE[`/api/generation/${EPOCH_ID}/v1/per-entry`] = { epoch_id: EPOCH_ID, generation_id: 'v1', entries: [
  { entry_id: 'waffles_single', run_id: 'run_v1_waffles', drift_loss: 60.5, pass_fail: false, runtime_ms: 180000, wall_clock_budget_exceeded: true },
  { entry_id: 'picky_stakeholder_emulated', run_id: 'run_v1_picky', drift_loss: 642.5, pass_fail: false, runtime_ms: 360000, wall_clock_budget_exceeded: true },
] };
FIXTURE[`/api/generation/${EPOCH_ID}/v2/per-entry`] = { epoch_id: EPOCH_ID, generation_id: 'v2', entries: [
  { entry_id: 'waffles_single', run_id: 'run_v2_waffles', drift_loss: 61.0, pass_fail: false, runtime_ms: 180000, wall_clock_budget_exceeded: false },
] };
FIXTURE[`/api/round/${EPOCH_ID}/v0/v1/gate`] = { decision: 'rejected', delta_scalar: 75.71, delta_pass_rate: 0,
  deciding_rule: 'scalar_margin', margin: 0.01, regressed_predicate: null, regressed_namespace: null,
  reason: 'challenger regressed: loss rose by 75.71', rules: [
    { id: 'scalar_margin', label: 'Scalar margin', status: 'fail', fired: true, detail: '70.94 → 146.65 (+75.71; needs ≤ -0.01)' },
    { id: 'pass_rate_monotonicity', label: 'Pass-rate monotonicity', status: 'not_reached', fired: false },
    { id: 'namespace_monotonicity', label: 'Namespace monotonicity', status: 'not_reached', fired: false },
  ],
  scalar_components: { champion: { drift: 68.5, schema: 1.43 }, challenger: { drift: 145.64, schema: 0.0 } },
  // scalar-provenance decomposition (#19): the challenger's pass term was
  // reshaped by a pow transform and its drift by a harmonic drift transform;
  // the champion was plain built-in. No fail-open here.
  scalar_decomposition: { present: true, fail_open: false,
    champion: { scalar: { present: true, kind: 'builtin', source: 'built-in formula', transforms: [], fail_open: false, fallback_reason: null },
                drift: { present: true, kind: 'builtin', source: 'built-in formula', transforms: [], fail_open: false, fallback_reason: null } },
    challenger: { scalar: { present: true, kind: 'transform', source: 'pow(2.0)', transforms: [{ kind: 'pass', op: 'pow(2.0)' }], fail_open: false, fallback_reason: null },
                  drift: { present: true, kind: 'transform', source: 'drift transform', transforms: [{ kind: 'looping_reasoning', op: 'harmonic' }], fail_open: false, fallback_reason: null } } },
  primary_driver: { judge: 'incorporates_feedback', delta: 24.0 } };
FIXTURE[`/api/round/${EPOCH_ID}/v0/v2/gate`] = { decision: 'rejected', delta_scalar: 1.51, reason: 'regressed',
  deciding_rule: 'scalar_margin', margin: 0.01, regressed_predicate: null, regressed_namespace: null, rules: [
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
export function lookupFixture(F, path) {
  if (Object.prototype.hasOwnProperty.call(F, path)) return F[path];
  // The two SERVED joins (round timeline + racing field) are derived from the
  // granular fixtures by the MOCK SERVER (mirroring the Python readers), so a
  // fixture map does not have to hand-write them — exactly like the real
  // dashboard service derives them from the same underlying records. An
  // EXPLICIT fixture (above) always wins.
  let m = /^\/api\/epoch\/([^/?]+)\/round-timeline$/.exec(path);
  if (m) return roundTimelineFromFixtures(F, decodeURIComponent(m[1]));
  m = /^\/api\/epoch\/([^/?]+)\/racing-field$/.exec(path);
  if (m) return racingFieldFromFixtures(F, decodeURIComponent(m[1]));
  const q = path.indexOf('?');
  if (q >= 0) {
    const base = path.slice(0, q);
    if (Object.prototype.hasOwnProperty.call(F, base)) return F[base];
  }
  return undefined;
}
export function installFetch() {
  globalThis.fetch = async (path) => {
    const v = lookupFixture(FIXTURE, path);
    if (v !== undefined) return { ok: true, json: async () => v };
    return { ok: false, status: 404, json: async () => ({ error: 'not found: ' + path }) };
  };
}
export function freshState() {
  data.invalidate();
  globalThis.window.location = { hash: '', search: '' };
}
export function allByClass(host, cls) {
  return host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').split(/\s+/).includes(cls));
}
// read the scoped stylesheet text (for CSS-contract assertions).
export async function readCssAsync() {
  const fs = await import('node:fs');
  return fs.readFileSync(new URL('../css/console.css', import.meta.url), 'utf8');
}
export const _cssCache = await readCssAsync();
export function readCss() { return _cssCache; }

// Drive the hovercard like a browser would: fire `mouseenter` on a wired node,
// read the live card text, then fire `mouseleave` to hide it. Returns the text
// the styled card surfaced (so a test can assert the SAME explanation the old
// native <title> carried now lives in the hovercard, not in a <title>).
export function hovercardTextOf(node) {
  hovercard.hide();
  node.dispatchEvent({ type: 'mouseenter', target: node });
  const text = hovercard.cardText();
  node.dispatchEvent({ type: 'mouseleave', target: node });
  return text;
}

// assert a node carries NO native <title> child (the off-brand tooltip is gone).
export function hasNativeTitle(node) {
  return node.childNodes.filter((n) => n.localName === 'title').length > 0;
}

// helpers to read the painted SVG of a view -------------------------
export function svgsByClass(host, cls) {
  return host.querySelectorAll('[class]').filter((n) =>
    n.localName === 'svg' && (n.getAttribute('class') || '').split(/\s+/).includes(cls));
}

// does any ancestor (within host) carry an inline horizontal-scroll style?
export function hasScrollWrapperAncestor(node, host) {
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

// shared helper: mount the real shell against a live `location` (the same
// harness plumbing the back-button test uses), returning the root.
export function mountLiveShell(initialHash) {
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

// mock single-elim structure payload (§3.2 shape)
export const SE_STRUCT = {
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

export const SWISS_STRUCT = {
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

export const RACING_STRUCT = {
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

export function structFixture(structure, payload, tournamentId) {
  // PLAY THE SERVER: the real /api/tournaments + /api/tournament-structure
  // payloads carry the served ELIM MODEL (attach_elim_states — sorted rounds
  // + bracket_side/loser + gen_states); the fixture attaches it through the
  // mock mirror so the views render exactly what the server serves.
  const served = attachElimStates({ ...payload, structure });
  const gens = payload.competitors.map((c) => ({ generation_id: c.generation_id, epoch_id: EPOCH_ID, parent_generation_id: c.role === 'champion' ? '' : 'v0', promoted: c.role === 'champion' }));
  const F = {
    '/api/epoch': { epoch_id: EPOCH_ID, closed: false, goal: 'g', current_champion: 'v0', tournament: { structure, params: payload.structure_params },
      experiments: gens.map((g) => ({ generation_id: g.generation_id, parent_generation_id: g.parent_generation_id, outcome: { decision: g.promoted ? 'baseline' : 'rejected' }, decision: g.promoted ? 'baseline' : 'rejected', promoted: false })), board: [] },
    '/api/lineage': { generations: gens },
    '/api/score-trajectory': { points: gens.map((g, i) => ({ generation_id: g.generation_id, scalar: 70 + i })) },
    '/api/tournaments': { epoch_id: EPOCH_ID, structure, structure_params: payload.structure_params, champion_lineage: ['v0'],
      matchups: [{ champion: 'v0', challenger: 'v1', decision: 'rejected', delta_scalar: 1 }],
      tournaments: [{ tournament_id: tournamentId, structure, structure_params: payload.structure_params, competitors: payload.competitors, rounds: served.rounds, ...(served.gen_states ? { gen_states: served.gen_states } : {}), standings: payload.standings }] },
    [`/api/tournament-structure/${EPOCH_ID}/${tournamentId}`]: served,
  };
  return F;
}

export function installFixtureMap(F) {
  globalThis.fetch = async (path) => {
    const v = lookupFixture(F, path);
    if (v !== undefined) return { ok: true, json: async () => v };
    return { ok: false, status: 404, json: async () => ({ error: 'nf' }) };
  };
}

// a live racing /api/active-tournament: first rung decided, second rung still
// racing (no cut/survivors recorded yet — the run is in flight). The eventual
// winner (v1) is NOT yet committed, so it must NOT show as rejected/eliminated.
export const LIVE_RACING = {
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

// the live racing field shape per the NEW contract: v0 champion + v5..v8
// challengers, the active rung-0 PUBLISHED (its field pending), partial
// aggregates landing.
export function liveRacingField(extra) {
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

export function liveElimField(extra) {
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

export const RC_EPOCH = '2026-06-01_e0';

// the four per-challenger racing records, verbatim from the brief's live epoch.
export const RACING_PER_CHALLENGER = [
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

export const RACING_TOURNAMENTS = {
  epoch_id: RC_EPOCH, structure: 'racing',
  structure_params: { eta: 2, board_fraction: 0.25 },
  champion_lineage: ['v0', 'v3'],
  matchups: RACING_PER_CHALLENGER.map((t) => ({ champion: 'v0', challenger: t.tournament_id.split('->')[1], decision: t.tournament_id.endsWith('v3') ? 'promoted' : 'rejected', delta_scalar: 1 })),
  tournaments: RACING_PER_CHALLENGER,
};

export const HERO_EPOCH = '2026-06-02_e3';

// two epochs on /api/lineage: a COMPLETED e0 (v0..v4) and a NEW e1 (v0..v2).
export const TWO_EP_OLD = '2026-06-01_e0';

export const TWO_EP_NEW = '2026-06-02_e1';

export function twoEpochFixture(viewedEpoch, opts) {
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
      entries: [{ entry_id: 'waffles_single', run_id: `r_${g.epoch_id}_${g.generation_id}`, drift_loss: 50, pass_fail: false }],
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
      board: [{ entry_id: 'waffles_single', kind: 'single_turn', budget_s: 180, weight: 1 }],
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
