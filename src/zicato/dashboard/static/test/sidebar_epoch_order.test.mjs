// test/sidebar_epoch_order.test.mjs — the sidebar tree lists epochs in the
// SAME canonical chronological order as the fleet cards.
//
// REGRESSION GUARDED: a prior fix made /api/workspace.epochs timestamp-ordered,
// so the fleet cards render epochs chronologically (A -> B -> C). buildTreeModel,
// however, assembled its epoch list by UNION first-appearance order across four
// sparse feeds: /api/lineage generations first, THEN the /api/workspace roster,
// THEN the contract/route epoch. An epoch with ZERO generations (no trajectory
// yet) never appears in /api/lineage, so it was APPENDED LAST from the workspace
// step regardless of its true chronological slot — the sidebar tree then showed
// A, C, B while the fleet showed A, B, C.
//
// THE FIX re-sorts the assembled `epochs` to the /api/workspace.epochs order, so
// sidebar order == fleet order == chronological, and the empty middle epoch lands
// in its correct slot. This file stubs the four feeds buildTreeModel reads and
// asserts the resulting model.epochs order.

import { installDom, test, run, assert, assertEqual, assertDeep } from './harness.mjs';

installDom();

const router = await import('../js/router.js');
const shell = await import('../js/shell.js');
const data = await import('../js/data.js');

// Stub globalThis.fetch keyed by API path — the D.* data layer fetches these.
function installFetch(map) {
  globalThis.fetch = async (path) => {
    // strip any query (e.g. /api/epoch?epoch=C) down to the base path.
    const base = path.indexOf('?') >= 0 ? path.slice(0, path.indexOf('?')) : path;
    const v = Object.prototype.hasOwnProperty.call(map, path) ? map[path]
      : (Object.prototype.hasOwnProperty.call(map, base) ? map[base] : undefined);
    if (v !== undefined) return { ok: true, json: async () => v };
    return { ok: false, status: 404, json: async () => ({ error: 'nf: ' + path }) };
  };
}

function freshState() {
  data.invalidate();
  globalThis.window.location = { hash: '', search: '' };
}

// ---------------------------------------------------------------------------

test('sidebar tree: a ZERO-generation epoch keeps its chronological slot (A,B,C — NOT A,C,B)', async () => {
  freshState();
  // /api/workspace is timestamp-ordered: A (oldest), B (middle, EMPTY), C (newest,
  // current). The lineage carries rows ONLY for A and C — B has no trajectory yet,
  // so a plain union order would append B LAST (A, C, B).
  installFetch({
    '/api/workspace': {
      current_epoch_id: 'C',
      epochs: [{ epoch_id: 'A' }, { epoch_id: 'B' }, { epoch_id: 'C' }],
    },
    '/api/lineage': {
      generations: [
        { epoch_id: 'A', generation_id: 'v0' },
        { epoch_id: 'C', generation_id: 'v1' },
      ],
    },
    '/api/epoch': { epoch_id: 'C' },
    '/api/bracket': {},
  });

  const model = await shell.buildTreeModel({ params: {} });

  // the sidebar tree order MATCHES the fleet (the workspace chronological order):
  // A, B, C — with the empty B in its correct MIDDLE slot rather than appended last.
  assertDeep(model.epochs.map((e) => e.id), ['A', 'B', 'C'],
    'epochs are chronological (A,B,C), not union order (A,C,B)');

  // all three epochs are present and the current marker stays on C.
  assertEqual(model.epochs.length, 3, 'all three epochs are tree nodes');
  const c = model.epochs.find((e) => e.id === 'C');
  assert(c && c.current, 'the current marker stays on C');
  assert(!model.epochs.find((e) => e.id === 'A').current, 'A is not marked current');
  assert(!model.epochs.find((e) => e.id === 'B').current, 'the empty B is not marked current');
});

test('sidebar tree: a routed epoch ABSENT from a sparse workspace digest sorts AFTER the ws-ordered ones (deterministic)', async () => {
  freshState();
  // The workspace digest is STALE and omits the routed epoch Z; the route points
  // at Z and the lineage carries its row. Z is not in ws.epochs, so it sorts after
  // the ws-ordered A, B — the sparse-feed fallback, kept deterministic.
  installFetch({
    '/api/workspace': {
      current_epoch_id: 'A',
      epochs: [{ epoch_id: 'A' }, { epoch_id: 'B' }],
    },
    '/api/lineage': {
      generations: [
        { epoch_id: 'A', generation_id: 'v0' },
        { epoch_id: 'Z', generation_id: 'v9' },
      ],
    },
    '/api/epoch': { epoch_id: 'A' },
    '/api/bracket': {},
  });

  const model = await shell.buildTreeModel(router.parseRoute('#/e/Z'));

  // A, B come first in ws order; Z (absent from ws.epochs) sorts last.
  assertDeep(model.epochs.map((e) => e.id), ['A', 'B', 'Z'],
    'ws-ordered epochs first (A,B), the ws-absent routed epoch Z last');
});

await run();
