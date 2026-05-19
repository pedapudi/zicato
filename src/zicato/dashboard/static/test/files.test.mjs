// test/files.test.mjs — Files-view behaviour tests.
//
// These prove the two Files-view fixes:
//   * the Files route is reachable — bare #/files resolves to a
//     sensible default (the current epoch + its latest generation) and
//     canonicalises into a #/files/{epoch}/{gen} deep link, instead of
//     falling through to the Overview view;
//   * the view renders a side-by-side (split) diff of every file the
//     selected generation changed relative to its parent.

import { installDom, test, run, assert, assertEqual } from './harness.mjs';

// The element ids the render layer's Files view needs, plus the shell
// ids renderAll touches.
const REQUIRED_IDS = [
  'drill-close', 'header-bar', 'footer-bar', 'epoch-id', 'generation-id',
  'round-id', 'elapsed', 'health-badge', 'mock-badge', 'tournament-title',
  'tournament-body', 'tournament-elapsed', 'health-panel', 'tournament-bracket',
  'tournament-detail', 'active-runs', 'log-tail', 'drill-panel', 'drill-title',
  'drill-body', 'dashboard-version', 'dashboard-port', 'dashboard-build',
  'view-overview', 'view-tree', 'view-tournament', 'view-epoch', 'view-files',
  'view-conversation', 'nav-overview', 'nav-tree', 'nav-tournament', 'nav-epoch',
  'nav-files', 'epoch-overview', 'epoch-harness', 'epoch-board', 'epoch-brief',
  'epoch-scoring', 'epoch-mutations', 'epoch-experiment-log', 'epoch-journal',
  'epoch-analysis', 'lineage-svg', 'trajectory-svg', 'heatmap-svg',
  'conversation-panel', 'files-changes-controls', 'files-changes-diff',
  'files-tree-pane', 'files-content-pane', 'files-patches', 'mutations-list-pane',
  'mutations-detail-pane', 'lineage-stage', 'lineage-viewport', 'lineage-zoom-in',
  'lineage-zoom-out', 'lineage-zoom-reset',
];

// The fixture the stubbed fetch serves — one epoch, three generations,
// with a per-generation diff for v2.
const FIXTURE = {
  '/api/files': {
    epochs: [{
      epoch_id: 'ep1',
      generations: [
        { generation_id: 'v0', file_count: 2, patch_count: 0 },
        { generation_id: 'v1', file_count: 2, patch_count: 1 },
        { generation_id: 'v2', file_count: 2, patch_count: 1 },
      ],
    }],
  },
  '/api/files/ep1/v2/tree': { entries: [{ path: 'a.py', is_dir: false, size: 10 }] },
  '/api/files/ep1/v2/diff': {
    epoch_id: 'ep1', generation_id: 'v2', parent_generation_id: 'v1',
    files: [{
      path: 'a.py', status: 'modified',
      old_content: 'x = 1\n', new_content: 'x = 2\n',
      old_binary: false, new_binary: false,
    }],
  },
  '/api/files/ep1/v2/patches': { patches: [] },
  '/api/files/ep1/v1/tree': { entries: [{ path: 'a.py', is_dir: false, size: 10 }] },
  '/api/files/ep1/v1/diff': {
    epoch_id: 'ep1', generation_id: 'v1', parent_generation_id: 'v0',
    files: [{
      path: 'a.py', status: 'modified',
      old_content: 'x = 0\n', new_content: 'x = 1\n',
      old_binary: false, new_binary: false,
    }],
  },
  '/api/files/ep1/v1/patches': { patches: [] },
  '/api/mutations/ep1': { mutations: [] },
};

function seedDom() {
  const doc = installDom();
  globalThis.location = globalThis.window.location;
  globalThis.URLSearchParams = URLSearchParams;
  globalThis.EventSource = class { addEventListener() {} close() {} };
  // The Files view fetches /api/files*; serve the fixture.
  globalThis.fetch = async (path) => ({
    ok: true,
    json: async () => FIXTURE[String(path).split('?')[0]] ?? {},
  });
  for (const id of REQUIRED_IDS) {
    const tag = id.endsWith('-svg') ? 'svg' : 'div';
    const n = doc.createElement(tag);
    n.setAttribute('id', id);
    doc.body.appendChild(n);
  }
  return doc;
}

const doc = seedDom();
const { state } = await import('../js/core/state.js');
const render = await import('../js/views/render.js');

// The current epoch the Files view defaults to when no epoch is routed.
state.epoch = { id: 'ep1', generation: '—', round: '—', startedAt: null };

test('bare #/files is reachable — it resolves a default, not Overview', async () => {
  window.location.hash = '#/files';
  render.showView('files');
  // applyRoute's files branch (and the route-driven view) resolve the
  // bare hash. Await the async entry point directly.
  await render.applyFilesRoute(null, null);

  // The Files view is shown, NOT redirected to Overview.
  assert(!doc.getElementById('view-files').classList.contains('hidden'),
    'the Files view must be visible');
  assert(doc.getElementById('view-overview').classList.contains('hidden'),
    'the Overview view must be hidden — #/files must not redirect to it');
  assert(doc.getElementById('nav-files').classList.contains('active'),
    'the Files nav link must be marked active');

  // The bare hash is canonicalised into a deep link: current epoch +
  // latest generation (v2 is the last in the fixture).
  assertEqual(window.location.hash, '#/files/ep1/v2',
    'bare #/files must canonicalise to #/files/{epoch}/{latest gen}');
  assert(render.filesState.selectedGen
    && render.filesState.selectedGen.generation_id === 'v2',
    'the latest generation must be selected by default');
});

test('a #/files/{epoch}/{gen} deep link selects that exact generation', async () => {
  window.location.hash = '#/files/ep1/v1';
  await render.applyFilesRoute('ep1', 'v1');
  assertEqual(render.filesState.selectedGen.generation_id, 'v1',
    'the routed generation must be selected');
  assertEqual(window.location.hash, '#/files/ep1/v1',
    'an already-canonical deep link must be left unchanged');
});

test('the Files view renders a side-by-side split diff of what changed', async () => {
  window.location.hash = '#/files/ep1/v2';
  await render.applyFilesRoute('ep1', 'v2');

  const diffPane = doc.getElementById('files-changes-diff');
  // A split diff (the components/index.js diff in mode:'split') rendered.
  const splits = diffPane._descendants().filter(
    (n) => n.classList && n.classList.contains('diff-split'),
  );
  assert(splits.length > 0, 'a split diff node must render for the changed file');

  // The split diff has the two side panes — old (left) and new (right).
  const old = diffPane._descendants().filter(
    (n) => n.classList && n.classList.contains('diff-old'));
  const fresh = diffPane._descendants().filter(
    (n) => n.classList && n.classList.contains('diff-new'));
  assert(old.length > 0 && fresh.length > 0,
    'the split diff must have an old (left) and new (right) side');

  // The changed file and its parent comparison are surfaced.
  assert(diffPane.textContent.includes('a.py'),
    'the changed file path must be listed');
  assert(diffPane.textContent.includes('v2 vs v1'),
    'the diff must name the parent generation it compares against');
  // The old content is on the left side, the new content on the right.
  assert(old[0].textContent.includes('x = 1'),
    'the old content must appear on the left side');
  assert(fresh[0].textContent.includes('x = 2'),
    'the new content must appear on the right side');
});

test('switching the routed generation re-renders the diff', async () => {
  window.location.hash = '#/files/ep1/v1';
  await render.applyFilesRoute('ep1', 'v1');
  const diffPane = doc.getElementById('files-changes-diff');
  assert(diffPane.textContent.includes('v1 vs v0'),
    'the diff must follow the routed generation');
});

await run();
