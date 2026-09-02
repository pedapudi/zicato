// test/matrix_grid.test.mjs — the `dn-mtx` grid, pinned as serialized DOM.
//
// Three surfaces draw the same table grammar: the mutation surface's site ×
// generation grid, the field-diversity matrix's site × challenger grid, and the
// evals view's entry × candidate grid. They share a class vocabulary — a corner
// cell, per-column and per-row headers, an on/off body cell, the filled mark and
// the blank dot — and each keeps interaction detail the other two have no use
// for: the mutation surface pins a row and a cell, the divergence matrix rails
// the pair with the highest overlap, and the evals matrix appends ghost rows for
// board entries that are proposed but not yet scored.
//
// Every case renders one matrix from a payload defined in this file and compares
// a deterministic serialization of its <table> with the string recorded in
// fixtures/matrix_grid.json. The strings were recorded while each builder still
// spelled its own markup, so they are what holds the shared builders to the DOM
// the three surfaces already produced — attribute for attribute.
//
// Re-record with `MATRIX_GRID_RECORD=1 node test/matrix_grid.test.mjs`, and only
// when a rendered change is intended.

import { readFileSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { installDom, test, run, assert, assertEqual } from './harness.mjs';

installDom();

const router = await import('../js/router.js');
const data = await import('../js/data.js');
const svg = await import('../js/svg.js');
const mutations = await import('../js/views/mutations.js');
const evals = await import('../js/views/evals.js');
const harmonograf = await import('../js/core/harmonograf.js');
const coreState = await import('../js/core/state.js');

const CTX = { navigate() {}, href: router.href };
const FIXTURE = join(dirname(fileURLToPath(import.meta.url)), 'fixtures', 'matrix_grid.json');
const RECORDING = !!process.env.MATRIX_GRID_RECORD;
const PINNED = RECORDING ? {} : JSON.parse(readFileSync(FIXTURE, 'utf8'));

// A deterministic serialization of a subtree — tag, attributes in sorted order,
// then the children — standing in for outerHTML, which the harness DOM does not
// implement. Two subtrees that serialize alike carry the same nodes, the same
// attributes and the same text.
function serialize(node) {
  if (!node) return '';
  if (node.nodeType === 3) return '#' + node.textContent;
  const attrs = Object.keys(node._attrs || {}).sort()
    .map((k) => `${k}=${node._attrs[k]}`).join(' ');
  const kids = (node.childNodes || []).map(serialize).join('');
  return `<${node.localName} ${attrs}>${kids}</${node.localName}>`;
}

// The matrix table inside a rendered surface.
function tableIn(host) {
  return host.querySelectorAll('[class]').find(
    (n) => (n.getAttribute('class') || '').split(/\s+/).includes('dn-mtx'));
}

function pin(name, host) {
  const table = tableIn(host);
  assert(table, name + ': the surface rendered a matrix table');
  const got = serialize(table);
  if (RECORDING) { PINNED[name] = got; return; }
  assertEqual(got, PINNED[name], name);
}

// ── the mutation surface ──────────────────────────────────────────────
const MUT_EPOCH = 'mtx-mutations';
const MUT_SURFACE = {
  generations: ['v0', 'v1', 'v2'],
  provenance: 'snapshot',
  mutations: [
    { mutation_id: 'coordinator_prompt', kind: 'prompt', file: 'agents/coordinator.py',
      role: 'system prompt', line_start: 12, line_end: 30, patched_generation_ids: ['v1', 'v2'] },
    // no role — the row label falls back to the kind.
    { mutation_id: 'oversight_policy', kind: 'value', file: 'agents/policy.py',
      role: '', line_start: 40, line_end: 41, patched_generation_ids: ['v2'] },
  ],
};

async function renderMutations(params) {
  data.invalidate();
  globalThis.fetch = async (path) => (
    path === `/api/mutations/${MUT_EPOCH}` ? { ok: true, json: async () => MUT_SURFACE }
      : path.startsWith('/api/epoch') ? { ok: true, json: async () => ({ epoch_id: MUT_EPOCH, closed: false, goal: 'g' }) }
        : { ok: false, status: 404, json: async () => ({ error: 'nf' }) });
  const host = globalThis.document.createElement('div');
  await mutations.render(host, CTX, { epochId: MUT_EPOCH, ...params });
  return host;
}

test('mutation surface: the unpinned grid holds its recorded DOM', async () => {
  pin('mutations', await renderMutations({}));
});

test('mutation surface: a pinned SITE row holds its recorded DOM', async () => {
  pin('mutations_site_pinned', await renderMutations({ mutId: 'coordinator_prompt' }));
});

test('mutation surface: a pinned CELL holds its recorded DOM', async () => {
  pin('mutations_cell_pinned', await renderMutations({ mutId: 'coordinator_prompt', gen: 'v2' }));
});

// ── the field-diversity matrix ────────────────────────────────────────
test('divergence matrix: the railed pair and the clickable columns hold their recorded DOM', () => {
  const figure = svg.diversityMatrix({
    membership: [
      { generation_id: 'g1', sites: ['coordinator_prompt', 'oversight_policy'] },
      { generation_id: 'g2', sites: ['coordinator_prompt'] },
      { generation_id: 'g3', sites: ['retry_budget'] },
    ],
    highlightPair: ['g1', 'g2'],
    onCompetitor: () => {},
  });
  const host = globalThis.document.createElement('div');
  host.appendChild(figure);
  pin('divergence', host);
});

// ── the evals matrix ──────────────────────────────────────────────────
const EVAL_EPOCH = 'mtx-evals';
const EVAL_PATH = `/api/epoch/${EVAL_EPOCH}/evals`;

function evalMatrix() {
  const cell = (pass, evidence) => ({ drift_loss: pass ? 0.31 : 0.72, pass_ratio: pass ? 1 : 0,
    pass_fail: pass, score: pass ? 0.9 : 0.2, replicates: 2, cached: false,
    latest_run_id: pass ? 'run_p' : 'run_f', evidence });
  return {
    epoch_id: EVAL_EPOCH, found: true,
    candidates: [
      { generation_id: 'g0', round_index: 0, promoted: true, decision: 'baseline',
        decision_label: 'seed (v0)', champion_spine: true, seed: true },
      { generation_id: 'g1', round_index: 1, promoted: false, decision: 'rejected',
        decision_label: 'rejected', champion_spine: false },
    ],
    entries: [
      { entry_id: 'task_login', slice: 'train', flip_rate: 0.2, flip_rate_measured: true,
        calibration_runs: 5, calibration_generation: 'g0' },
      { entry_id: 'task_hold', slice: 'holdout', flip_rate: null, flip_rate_measured: false,
        calibration_runs: 0 },
    ],
    cells: [[cell(true, 'replicated'), cell(false, 'single')], [cell(true, 'replicated'), null]],
    calibration: { measured: true, generation_id: 'g0', runs: 5, max_abs_delta: 0.06 },
  };
}

const GHOST_FEED = {
  epoch_id: EVAL_EPOCH, reflection_id: 'refl-x',
  suggestions: [
    { suggestion_id: 's1', artifact_kind: 'board_entry', target_slice: 'train',
      draft_artifact: { id: 'ghost_probe' },
      admission: { noise: { flip_rate: 0.1, runs: 5, measured: true },
        discrimination: { separated: 2, pairs: 3, measured: true },
        leakage: { target_slice_ok: true } } },
  ],
};

test('evals matrix: the round groups, the verdict columns and the ghost row hold their recorded DOM', async () => {
  data.invalidate();
  evals._resetGhostFeedForTest();
  globalThis.window.location = { hash: '', search: '' };
  coreState.state.heartbeat = null;
  coreState.state.activeTournament = null;
  coreState.state.activeRuns = [];
  harmonograf._resetHarmonografUiProbe();
  globalThis.fetch = async (path) => (path === EVAL_PATH
    ? { ok: true, json: async () => evalMatrix() }
    : { ok: false, status: 404, json: async () => ({ error: 'nf' }) });
  const host = globalThis.document.createElement('div');
  evals._setGhostFeedForTest(GHOST_FEED, EVAL_EPOCH);
  await evals.render(host, CTX, { epochId: EVAL_EPOCH });
  pin('evals', host);
});

await run();

if (RECORDING) writeFileSync(FIXTURE, JSON.stringify(PINNED, null, 2) + '\n');
