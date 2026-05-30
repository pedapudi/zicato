// test/v2_bench.test.mjs — unit tests for the Bench (live operations)
// view (js/v2/views/bench.js).
//
// The Bench reads the shared AppState (SSE-updated) and renders the live
// tournament. These tests seed `state` directly (the data layer's job;
// the view never fetches), render into a fresh harness host, and assert:
//   - the honest idle state when no run is in flight,
//   - the header / pinned hypothesis / gate / matrix / ticker surfaces,
//   - in-place patching across re-renders (no flash; node identity kept),
//   - the four honest cell states from a tournament snapshot,
//   - matrix-cell drill wiring.
// Run directly: `node static/test/v2_bench.test.mjs`.

import { installDom, makeEvent, test, run, assert, assertEqual } from './harness.mjs';

installDom();

// A no-op fetch so any incidental network call in the module graph is a
// resolved empty body rather than a crash (the view itself reads state,
// not fetch — this only guards transitive imports).
globalThis.fetch = async () => ({ ok: true, status: 200, json: async () => ({}) });

const { state } = await import('../js/core/state.js');
const { renderBench } = await import('../js/v2/views/bench.js');
const { $ } = await import('../js/core/dom.js');

// The view addresses its surfaces by id through document.getElementById
// (one #v2-view host in production). Tests must therefore not leave a
// prior test's host attached, or a stale duplicate-id node would shadow
// the live one. Clear the document body (and the id registry) per host.
function freshHost() {
  while (document.body.firstChild) document.body.removeChild(document.body.firstChild);
  document._byId = new Map();
  const host = document.createElement('div');
  host.id = 'v2-view';
  document.body.appendChild(host);
  document.registerId(host.id, host);
  return host;
}

// Reset the slices the Bench reads to a clean idle baseline.
function resetState() {
  state.activeTournament = null;
  state.activeRuns = [];
  state.logTail = { events: [] };
  state.epochDef = null;
  state.epoch = { id: '2026-05-30_e0', generation: '—', round: '—', startedAt: null };
}

// A representative in-flight tournament snapshot (the /api/active-tournament
// shape after status normalization). Exercises all four cell states.
function liveTournament() {
  return {
    tournament_id: 't-1',
    parent_generation_id: 'v0',
    child_generation_id: 'v1',
    epoch_id: '2026-05-30_e0',
    phase: 'running',
    round_index: 0,
    total_rounds: 1,
    entries: [
      // champion done w/ pass, challenger done w/ pass
      { entry_id: 'waffles_single', side: 'parent', status: 'done',
        loss_summary: { drift_loss: 0.605, pass_fail: 1.0 }, status_raw: 'completed' },
      { entry_id: 'waffles_single', side: 'child', status: 'done',
        loss_summary: { drift_loss: 0.31, pass_fail: 1.0 }, status_raw: 'completed' },
      // champion done w/ fail, challenger running
      { entry_id: 'q3_metrics_outline', side: 'parent', status: 'done',
        loss_summary: { drift_loss: 0.71, pass_fail: 0.0 }, status_raw: 'completed' },
      { entry_id: 'q3_metrics_outline', side: 'child', status: 'running',
        generation_id: 'v1' },
      // champion running, challenger queued
      { entry_id: 'picky_stakeholder', side: 'parent', status: 'running',
        generation_id: 'v0' },
      { entry_id: 'picky_stakeholder', side: 'child', status: 'queued' },
      // champion aborted, challenger queued
      { entry_id: 'revision_loop', side: 'parent', status: 'failed',
        status_raw: 'aborted' },
      { entry_id: 'revision_loop', side: 'child', status: 'queued' },
    ],
    partial_champion_agg: { scalar: 1.315, pass_rate: 0.5, namespace_aggregates: { drift: {} } },
    partial_challenger_agg: { scalar: 0.92, pass_rate: 1.0, namespace_aggregates: { drift: {}, task: {} } },
  };
}

// =====================================================================

test('Bench renders an honest idle state when no run is in flight', () => {
  resetState();
  const host = freshHost();
  renderBench(host, { view: 'bench', params: {} });
  assertEqual(host.getAttribute('data-bench-mode'), 'idle', 'idle mode set');
  const txt = host.textContent;
  assert(txt.includes('No run in flight'), 'honest idle headline');
  assert(txt.includes('Overview') || txt.includes('overview'), 'points to the Overview');
  // The empty stateBlock is used (never an ad-hoc "No data").
  const state_ = host.querySelectorAll('[data-kind]').find((n) => n.getAttribute('data-kind') === 'empty');
  assert(state_, 'uses stateBlock(empty) for idle');
});

test('Bench header shows epoch · round X/N · champion → challenger · live', () => {
  resetState();
  state.activeTournament = liveTournament();
  const host = freshHost();
  renderBench(host, { view: 'bench', params: {} });
  assertEqual(host.getAttribute('data-bench-mode'), 'live', 'live mode set');
  assert(host.textContent.includes('2026-05-30_e0'), 'epoch id in header');
  assert(host.textContent.includes('round 0/1'), 'round X/N');
  assert(host.textContent.includes('v0 → v1'), 'champion → challenger matchup');
  const status = $('v2-bench-status');
  assertEqual(status.getAttribute('data-live'), 'true', 'live status flagged');
  assert($('v2-bench-status-label').textContent.includes('running'), 'status reads running');
});

test('Bench pins the challenger hypothesis + its prediction', () => {
  resetState();
  state.activeTournament = liveTournament();
  state.epochDef = {
    epoch_id: '2026-05-30_e0',
    experiments: [
      { generation_id: 'v1', hypothesis: {
        core_idea: 'Enforce explicit slide-structure + topic discipline',
        expected_drift_movements: [{ kind: 'topic_drift', direction: 'down', magnitude: 'moderate' }],
        expected_pass_rate_delta: '+0.10..+0.20',
      } },
    ],
  };
  const host = freshHost();
  renderBench(host, { view: 'bench', params: {} });
  const txt = host.textContent;
  assert(txt.includes('HYPOTHESIS'), 'hypothesis section tag');
  assert(txt.includes('Enforce explicit slide-structure'), 'core idea pinned');
  assert(txt.includes('(v1)'), 'challenger generation id shown');
  assert(txt.includes('topic_drift down'), 'predicted drift movement');
  assert(txt.includes('+0.10..+0.20'), 'predicted pass-rate delta');
});

test('Bench falls back to the newest experiment when ids do not line up', () => {
  resetState();
  state.activeTournament = liveTournament();
  state.epochDef = {
    experiments: [
      { generation_id: 'vX', hypothesis: { core_idea: 'older bet' } },
      { generation_id: 'vY', hypothesis: { core_idea: 'newest bet under test' } },
    ],
  };
  const host = freshHost();
  renderBench(host, { view: 'bench', params: {} });
  assert(host.textContent.includes('newest bet under test'), 'newest experiment pinned as fallback');
});

test('Bench renders the four honest matrix cell states from the snapshot', () => {
  resetState();
  state.activeTournament = liveTournament();
  state.activeRuns = [
    { entry_id: 'q3_metrics_outline', generation_id: 'v1', progress: 0.6 },
    { entry_id: 'picky_stakeholder', generation_id: 'v0', progress: 0.23 },
  ];
  const host = freshHost();
  renderBench(host, { view: 'bench', params: {} });
  const matrix = $('v2-bench-matrix');
  assert(matrix.querySelector('[class]'), 'matrix mounted');
  const states = matrix.querySelectorAll('[data-state]').map((n) => n.getAttribute('data-state'));
  for (const st of ['queued', 'running', 'done', 'aborted']) {
    assert(states.includes(st), `cell state ${st} present`);
  }
  const txt = matrix.textContent;
  assert(txt.includes('0.605'), 'champion done loss');
  assert(txt.includes('0.310'), 'challenger done loss');
  assert(txt.includes('✓ pass'), 'pass verdict glyph');
  assert(txt.includes('✗ fail'), 'fail verdict glyph');
  assert(txt.includes('60%'), 'running budget % from active-runs');
  assert(txt.includes('aborted'), 'aborted state word');
});

test('Bench maps champion→parent / challenger→child sides correctly', () => {
  resetState();
  state.activeTournament = liveTournament();
  const host = freshHost();
  renderBench(host, { view: 'bench', params: {} });
  const matrix = $('v2-bench-matrix');
  // The waffles_single row: champion (parent) done 0.605, challenger (child) done 0.310.
  const rows = matrix.querySelectorAll('[data-key]');
  const waffles = rows.find((r) => r.getAttribute('data-key') === 'waffles_single');
  assert(waffles, 'waffles_single row present');
  // children: entry th, champion cell, challenger cell.
  const championCell = waffles.children[1];
  const challengerCell = waffles.children[2];
  assert(championCell.textContent.includes('0.605'), 'champion column = parent side loss');
  assert(challengerCell.textContent.includes('0.310'), 'challenger column = child side loss');
});

test('Bench gate surfaces challenger-vs-champion scalar/pass + N/M counter', () => {
  resetState();
  state.activeTournament = liveTournament();
  const host = freshHost();
  renderBench(host, { view: 'bench', params: {} });
  const scalar = $('v2-bench-gate-scalar');
  assert(scalar.textContent.includes('0.920'), 'challenger scalar shown');
  assert(scalar.textContent.includes('1.315'), 'vs champion scalar');
  assertEqual(scalar.getAttribute('data-sentiment'), 'improve', 'lower challenger scalar = improve');
  const pass = $('v2-bench-gate-pass');
  assertEqual(pass.getAttribute('data-sentiment'), 'improve', 'higher challenger pass = improve');
  assert($('v2-bench-gate-ns').textContent.includes('2 resolved'), 'namespace count');
  // Settled units: 2 done (waffles parent+child) + 1 done (q3 parent) + 1 aborted (revision parent) = 4 of 8.
  assert($('v2-bench-gate-count').textContent.includes('4/8 runs complete'), 'N/M counter');
});

test('Bench activity ticker renders recent run-log events, newest appended', () => {
  resetState();
  state.activeTournament = liveTournament();
  state.logTail = { events: [
    { seq: 1, kind: 'goldfive_llm_call_start', ts: '2026-05-30T12:00:01Z', summary: 'goldfive_llm_call_start: research_agent' },
    { seq: 2, kind: 'reasoning_judge_invoked', ts: '2026-05-30T12:00:05Z', summary: 'reasoning_judge_invoked: incorporates_feedback' },
  ] };
  const host = freshHost();
  renderBench(host, { view: 'bench', params: {} });
  const list = $('v2-bench-ticker-list');
  assertEqual(list.children.length, 2, 'two ticker rows');
  assert(host.textContent.includes('research_agent'), 'proposer/agent event');
  assert(host.textContent.includes('incorporates_feedback'), 'judge event');
});

test('Bench ticker shows an honest not-yet state when no events', () => {
  resetState();
  state.activeTournament = liveTournament();
  state.logTail = { events: [] };
  const host = freshHost();
  renderBench(host, { view: 'bench', params: {} });
  const empty = $('v2-bench-ticker-empty');
  assert(empty.getAttribute('hidden') == null, 'empty block visible');
  const blk = empty.querySelectorAll('[data-kind]').find((n) => n.getAttribute('data-kind') === 'not_yet');
  assert(blk, 'uses stateBlock(not_yet) for an empty ticker');
});

test('Bench patches in place across re-renders (no flash, node identity kept)', () => {
  resetState();
  state.activeTournament = liveTournament();
  const host = freshHost();
  renderBench(host, { view: 'bench', params: {} });
  const headBefore = $('v2-bench-head');
  const matrixBefore = $('v2-bench-matrix');
  const lmTableBefore = matrixBefore.children[0];

  // A cell transitions: challenger q3 finishes. Re-render.
  const t2 = liveTournament();
  t2.entries[3] = { entry_id: 'q3_metrics_outline', side: 'child', status: 'done',
    loss_summary: { drift_loss: 0.635, pass_fail: 1.0 }, status_raw: 'completed' };
  state.activeTournament = t2;
  renderBench(host, { view: 'bench', params: {} });

  assert($('v2-bench-head') === headBefore, 'header node identity preserved');
  assert($('v2-bench-matrix').children[0] === lmTableBefore, 'matrix table identity preserved');
  assertEqual(host.innerHTMLWriteCount(), 0, 'no innerHTML writes across re-render — no flash');
  assert($('v2-bench-matrix').textContent.includes('0.635'), 'updated challenger loss painted in place');
});

test('Bench swaps idle ⇄ live cleanly when a run starts / ends', () => {
  resetState();
  const host = freshHost();
  renderBench(host, { view: 'bench', params: {} });
  assertEqual(host.getAttribute('data-bench-mode'), 'idle', 'starts idle');

  state.activeTournament = liveTournament();
  renderBench(host, { view: 'bench', params: {} });
  assertEqual(host.getAttribute('data-bench-mode'), 'live', 'goes live when a run starts');
  assert(host.textContent.includes('GATE forming'), 'live frame present');

  state.activeTournament = null;
  renderBench(host, { view: 'bench', params: {} });
  assertEqual(host.getAttribute('data-bench-mode'), 'idle', 'returns to idle when the run ends');
  assert(host.textContent.includes('No run in flight'), 'idle headline restored');
});

test('Bench matrix cell is a drillable door (role=button + tabindex)', () => {
  resetState();
  state.activeTournament = liveTournament();
  const host = freshHost();
  renderBench(host, { view: 'bench', params: {} });
  const matrix = $('v2-bench-matrix');
  const cell = matrix.querySelectorAll('[data-state]')[0];
  assertEqual(cell.getAttribute('role'), 'button', 'cell is a button for a11y');
  assertEqual(cell.getAttribute('tabindex'), '0', 'cell is keyboard-focusable');
  assert(cell.classList.contains('v2-lm-cell-drillable'), 'cell carries the drillable affordance');
  // Firing the click must not throw (the router import is lazy/async).
  cell.dispatchEvent(makeEvent('click'));
  assert(true, 'cell click handler runs without throwing');
});

await run();
