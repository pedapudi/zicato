// test/files.test.mjs — Files-view behaviour tests.
//
// These prove the two Files-view fixes:
//   * the Files route is reachable — bare #/files resolves to a
//     sensible default (the current epoch + its latest generation) and
//     canonicalises into a #/files/{epoch}/{gen} deep link, instead of
//     falling through to the Overview view;
//   * the view renders a side-by-side (split) diff of every file the
//     selected generation changed relative to its parent.

import {
  installDom, test, run, assert, assertEqual, makeEvent,
} from './harness.mjs';

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
  'epoch-analysis', 'lineage-svg', 'heatmap-svg',
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
  '/api/files/ep1/v2/patches': {
    epoch_id: 'ep1', generation_id: 'v2',
    patches: [
      { id: 'p-aaa', op: 'REPLACE', mutation_id: 'site_one', rationale: 'tighten' },
      { id: 'p-bbb', op: 'REPLACE', mutation_id: 'site_two', rationale: 'expand' },
    ],
  },
  '/api/files/ep1/v1/tree': { entries: [{ path: 'a.py', is_dir: false, size: 10 }] },
  '/api/files/ep1/v1/diff': {
    epoch_id: 'ep1', generation_id: 'v1', parent_generation_id: 'v0',
    files: [{
      path: 'a.py', status: 'modified',
      old_content: 'x = 0\n', new_content: 'x = 1\n',
      old_binary: false, new_binary: false,
    }],
  },
  // v1 has EXACTLY ONE patch — the live-reported duplication case.
  '/api/files/ep1/v1/patches': {
    epoch_id: 'ep1', generation_id: 'v1',
    patches: [
      {
        id: 'a4521cb4280446ce81738773b0a53bee',
        op: 'REPLACE', mutation_id: 'web_developer_instruction',
        rationale: 'sharpen the brief',
      },
    ],
  },
  '/api/mutations/ep1': {
    mutations: [
      { mutation_id: 'site_one', role: 'instruction', file: 'agent.py',
        patched_generation_ids: ['v2'] },
      { mutation_id: 'site_two', role: 'instruction', file: 'agent.py',
        patched_generation_ids: [] },
    ],
  },
  // site_one is patched in v2 — its detail carries a version with patched
  // content, so the detail pane renders a `.mutations-diff` block (the
  // horizontally-scrollable viewer the scroll-preservation fix guards).
  '/api/mutations/ep1/site_one': {
    mutation_id: 'site_one',
    baseline: {
      role: 'instruction', file: 'agent.py', line_start: 10, line_end: 12,
      content: 'be concise\n',
    },
    versions: [
      {
        generation_id: 'v2', op: 'REPLACE', rationale: 'sharpen',
        content: 'be concise and cite a source for every claim you make\n',
      },
    ],
  },
};

// /api/files is served from a SEPARATE mutable holder so a test can
// simulate a new generation landing mid-run (Bug 1, live refresh).
let filesIndexResponse = FIXTURE['/api/files'];

function seedDom() {
  const doc = installDom();
  globalThis.location = globalThis.window.location;
  globalThis.URLSearchParams = URLSearchParams;
  globalThis.EventSource = class { addEventListener() {} close() {} };
  // The Files view fetches /api/files*; serve the fixture. The
  // generation index is served from the mutable holder above.
  globalThis.fetch = async (path) => {
    const clean = String(path).split('?')[0];
    if (clean === '/api/files') {
      return { ok: true, json: async () => filesIndexResponse };
    }
    return { ok: true, json: async () => FIXTURE[clean] ?? {} };
  };
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

// --- Bug 2: a generation's applied patches render exactly once --------

// The applied-patch <li> rows currently in the patches pane.
function patchRows() {
  const pane = doc.getElementById('files-patches');
  return pane._descendants().filter(
    (n) => n.classList && n.classList.contains('files-patch-item'));
}

test('a generation with N patches renders exactly N applied-patch rows', async () => {
  // v1 has exactly one patch — the live-reported duplication case.
  window.location.hash = '#/files/ep1/v1';
  await render.applyFilesRoute('ep1', 'v1');

  let rows = patchRows();
  assertEqual(rows.length, 1,
    'a one-patch generation must render exactly ONE applied-patch row');
  assert(rows[0].textContent.includes('a4521cb4280446ce81738773b0a53bee'),
    'the patch id must be shown');

  // v2 has two distinct patches — they render as exactly two rows.
  window.location.hash = '#/files/ep1/v2';
  await render.applyFilesRoute('ep1', 'v2');
  rows = patchRows();
  assertEqual(rows.length, 2,
    'a two-patch generation must render exactly TWO applied-patch rows');
});

test('a re-render of the same generation does not duplicate patch rows', async () => {
  window.location.hash = '#/files/ep1/v1';
  await render.applyFilesRoute('ep1', 'v1');
  assertEqual(patchRows().length, 1, 'one patch row after the first render');

  // An SSE-driven repaint: re-enter the route for the same generation.
  await render.applyFilesRoute('ep1', 'v1');
  await render.applyFilesRoute('ep1', 'v1');
  assertEqual(patchRows().length, 1,
    'repeated repaints must NOT append duplicate patch rows');
});

test('concurrent repaints of the same generation do not duplicate patches', async () => {
  // Force a fresh selection so the load path runs its async fetch.
  window.location.hash = '#/files/ep1/v2';
  await render.applyFilesRoute('ep1', 'v2');
  render.filesState.selectedGen = null;

  // Two repaints raced before either resolves — this is the SSE-tick
  // pattern that previously double-cleared then double-appended.
  await Promise.all([
    render.applyFilesRoute('ep1', 'v1'),
    render.applyFilesRoute('ep1', 'v1'),
  ]);
  assertEqual(patchRows().length, 1,
    'racing repaints must still render exactly one patch row');
});

// --- Bug 1: no DOM churn / layout jump on a no-op repaint -------------

test('a no-op repaint produces zero churn in the patches section', async () => {
  window.location.hash = '#/files/ep1/v2';
  await render.applyFilesRoute('ep1', 'v2');

  const pane = doc.getElementById('files-patches');
  const before = patchRows();
  assertEqual(before.length, 2, 'two patch rows before the repaint');
  const beforeUl = pane.firstChild;

  // Repaint with identical data — the keyed reconcile must leave every
  // node identity intact, so listeners survive and the browser repaints
  // nothing (no layout shift).
  await render.applyFilesRoute('ep1', 'v2');

  const after = patchRows();
  assertEqual(after.length, 2, 'still two patch rows after the repaint');
  assert(pane.firstChild === beforeUl,
    'the <ul> shell must be the SAME node — not cleared and rebuilt');
  for (let i = 0; i < before.length; i++) {
    assert(before[i] === after[i],
      `patch row ${i} must keep its node identity across a no-op repaint`);
  }
  assertEqual(pane.innerHTMLWriteCount(), 0,
    'a repaint must never touch innerHTML in the patches section');
});

test('a no-op repaint produces zero churn in the mutation-site section', async () => {
  window.location.hash = '#/files/ep1/v2';
  await render.applyFilesRoute('ep1', 'v2');

  const pane = doc.getElementById('mutations-list-pane');
  const itemsOf = () => pane._descendants().filter(
    (n) => n.classList && n.classList.contains('mutations-list-item'));
  const before = itemsOf();
  assertEqual(before.length, 2, 'two mutation-site rows before the repaint');
  const beforeUl = pane.firstChild;

  await render.applyFilesRoute('ep1', 'v2');

  const after = itemsOf();
  assertEqual(after.length, 2, 'still two mutation-site rows after the repaint');
  assert(pane.firstChild === beforeUl,
    'the mutation-site <ul> must be the SAME node — not rebuilt');
  for (let i = 0; i < before.length; i++) {
    assert(before[i] === after[i],
      `mutation-site row ${i} must keep its node identity across a no-op repaint`);
  }
  assertEqual(pane.innerHTMLWriteCount(), 0,
    'a repaint must never touch innerHTML in the mutation-site section');
});

// --- Bug 1: the generation picker live-updates -----------------------
//
// The Files index is a load-time snapshot. A generation created while
// the tab is open (a new challenger v2 produced mid-run) must appear in
// the picker without a page reload — and a no-op refresh must not churn
// the rows already on screen.

// The generation-button rows in the "What changed" section's picker.
function genButtons() {
  const pane = doc.getElementById('files-changes-controls');
  return pane._descendants().filter(
    (n) => n.classList && n.classList.contains('files-gen-button'));
}

test('a generation added to live state appears in the picker — no reload', async () => {
  // Start with an index that knows only v0 and v1.
  filesIndexResponse = {
    epochs: [{
      epoch_id: 'ep1',
      generations: [
        { generation_id: 'v0', file_count: 2, patch_count: 0 },
        { generation_id: 'v1', file_count: 2, patch_count: 1 },
      ],
    }],
  };
  render.filesState.index = null;
  render.filesState.liveGensKey = null;
  state.lineage = {
    generations: [{ generation_id: 'v0' }, { generation_id: 'v1' }],
    experiments: [],
  };

  window.location.hash = '#/files/ep1/v1';
  await render.applyFilesRoute('ep1', 'v1');
  assertEqual(genButtons().length, 2,
    'the picker starts with the two known generations');

  // A new challenger v2 lands while the tab is open: live AppState gains
  // the generation, and the server-side index now lists it too.
  state.lineage = {
    generations: [
      { generation_id: 'v0' }, { generation_id: 'v1' },
      { generation_id: 'v2' },
    ],
    experiments: [],
  };
  filesIndexResponse = FIXTURE['/api/files'];  // the full three-gen index

  // An SSE-driven repaint re-enters the route — no navigation, no reload.
  await render.applyFilesRoute('ep1', 'v1');
  await render.applyFilesRoute('ep1', 'v1');  // settle the async refresh

  const ids = genButtons().map(
    (b) => b._descendants().find(
      (n) => n.classList && n.classList.contains('files-gen-id')).textContent);
  assertEqual(genButtons().length, 3,
    'the new generation v2 must appear in the picker without a reload');
  assert(ids.includes('v2'), 'the picker must list the new generation v2');
});

test('a no-op live refresh produces zero churn in the generation picker', async () => {
  // The index and live state already agree (three generations).
  filesIndexResponse = FIXTURE['/api/files'];
  render.filesState.index = null;
  render.filesState.liveGensKey = null;
  state.lineage = {
    generations: [
      { generation_id: 'v0' }, { generation_id: 'v1' },
      { generation_id: 'v2' },
    ],
    experiments: [],
  };

  window.location.hash = '#/files/ep1/v2';
  await render.applyFilesRoute('ep1', 'v2');

  const pane = doc.getElementById('files-changes-controls');
  const before = genButtons();
  assertEqual(before.length, 3, 'three generation buttons before the repaint');
  const beforePicker = pane.firstChild;

  // A repaint with an unchanged generation set: the digest gate skips
  // the re-fetch entirely, so every row keeps its node identity.
  await render.applyFilesRoute('ep1', 'v2');
  await render.applyFilesRoute('ep1', 'v2');

  const after = genButtons();
  assertEqual(after.length, 3, 'still three generation buttons after the repaint');
  assert(pane.firstChild === beforePicker,
    'the picker shell must be the SAME node — not cleared and rebuilt');
  for (let i = 0; i < before.length; i++) {
    assert(before[i] === after[i],
      `generation button ${i} must keep its node identity on a no-op refresh`);
  }
  assertEqual(pane.innerHTMLWriteCount(), 0,
    'a no-op refresh must never touch innerHTML in the picker');
});

// --- Bug 2: unpatched mutation sites tag as a generation id ----------

test('an unpatched mutation site is tagged v0, not the word BASELINE', async () => {
  filesIndexResponse = FIXTURE['/api/files'];
  render.filesState.index = null;
  render.filesState.liveGensKey = null;
  state.lineage = {
    generations: [
      { generation_id: 'v0' }, { generation_id: 'v1' },
      { generation_id: 'v2' },
    ],
    experiments: [],
  };

  window.location.hash = '#/files/ep1/v2';
  await render.applyFilesRoute('ep1', 'v2');

  const pane = doc.getElementById('mutations-list-pane');
  const badges = pane._descendants().filter(
    (n) => n.classList && n.classList.contains('mutations-site-badge'));
  assertEqual(badges.length, 2, 'one badge per mutation site');

  // site_one is patched in v2; site_two is unpatched. The unpatched
  // badge's VALUE must be a generation id (the seed generation v0) —
  // consistent with the v1/v2 ids patched sites carry — never a word.
  const patched = badges.find((b) => !b.classList.contains('unpatched'));
  const unpatched = badges.find((b) => b.classList.contains('unpatched'));
  assert(patched && patched.textContent.includes('v2'),
    'the patched site must be tagged with its patching generation id');
  assert(unpatched, 'the unpatched site must still carry a badge');
  assertEqual(unpatched.textContent, 'v0',
    'an unpatched site must be tagged with the seed generation id v0');
  assert(!unpatched.textContent.toLowerCase().includes('baseline'),
    'the tag must be a generation id, never the word BASELINE');
});

// --- "What changed" diff folds unchanged regions ---------------------
//
// A generation that changed only a couple of lines must NOT render the
// whole file in both columns: long runs of unchanged lines collapse to
// a single "… N unchanged lines …" click-to-expand marker, and each
// changed hunk shows with a few lines of surrounding context.

// A file with a long identical middle and ONE changed line near the
// end — the case where the old renderer painted a ~9,000px wall.
const FOLD_OLD = Array.from({ length: 60 }, (_, i) => `line ${i}`).join('\n');
const FOLD_NEW = FOLD_OLD.split('\n')
  .map((l, i) => (i === 40 ? 'line 40 CHANGED' : l)).join('\n');

// A four-generation index whose v3 carries the big folding diff. Served
// from the mutable holder so it does not perturb the three-gen tests.
const FOLD_INDEX = {
  epochs: [{
    epoch_id: 'ep1',
    generations: [
      { generation_id: 'v0', file_count: 2, patch_count: 0 },
      { generation_id: 'v1', file_count: 2, patch_count: 1 },
      { generation_id: 'v2', file_count: 2, patch_count: 1 },
      { generation_id: 'v3', file_count: 2, patch_count: 1 },
    ],
  }],
};
FIXTURE['/api/files/ep1/v3/tree'] = {
  entries: [{ path: 'big.py', is_dir: false, size: 600 }],
};
FIXTURE['/api/files/ep1/v3/diff'] = {
  epoch_id: 'ep1', generation_id: 'v3', parent_generation_id: 'v2',
  files: [{
    path: 'big.py', status: 'modified',
    old_content: FOLD_OLD, new_content: FOLD_NEW,
    old_binary: false, new_binary: false,
  }],
};
FIXTURE['/api/files/ep1/v3/patches'] = {
  epoch_id: 'ep1', generation_id: 'v3', patches: [],
};

function diffLinesIn(pane) {
  return pane._descendants().filter(
    (n) => n.classList && n.classList.contains('diff-line'));
}

test('the "What changed" diff folds unchanged regions', async () => {
  filesIndexResponse = FOLD_INDEX;
  render.filesState.index = null;
  render.filesState.liveGensKey = null;
  state.lineage = {
    generations: [
      { generation_id: 'v0' }, { generation_id: 'v1' },
      { generation_id: 'v2' }, { generation_id: 'v3' },
    ],
    experiments: [],
  };

  window.location.hash = '#/files/ep1/v3';
  await render.applyFilesRoute('ep1', 'v3');

  const diffPane = doc.getElementById('files-changes-diff');

  // The diff must NOT render all 60 lines in each column — long
  // unchanged runs are collapsed. With one change near line 40 and a
  // few context lines, far fewer than 60 old-side lines are shown.
  const oldSide = diffPane._descendants().find(
    (n) => n.classList && n.classList.contains('diff-old'));
  assert(oldSide, 'a split diff old-side column must render');
  const oldLines = oldSide._descendants().filter(
    (n) => n.classList && n.classList.contains('diff-line'));
  assert(oldLines.length < 30,
    `the diff must fold unchanged lines — got ${oldLines.length} shown lines `
      + 'of a 60-line file, expected far fewer');

  // A fold marker is present, labelled with how many lines it hides.
  const folds = diffPane._descendants().filter(
    (n) => n.classList && n.classList.contains('diff-fold'));
  assert(folds.length > 0, 'a "… N unchanged lines …" fold marker must render');
  assert(/\d+\s+unchanged line/.test(folds[0].textContent),
    'the fold marker must say how many unchanged lines it hides');

  // The actual change is visibly marked — both an added and a removed
  // (here: modified) line carry a change class, never plain equal.
  const changed = diffLinesIn(diffPane).filter(
    (n) => n.classList.contains('diff-add') || n.classList.contains('diff-del'));
  assert(changed.length > 0, 'the changed line must be visually marked');
  assert(diffPane.textContent.includes('line 40 CHANGED'),
    'the changed content must be shown — never hidden inside a fold');
});

test('clicking a fold marker expands the hidden unchanged lines', async () => {
  filesIndexResponse = FOLD_INDEX;
  render.filesState.index = null;
  render.filesState.liveGensKey = null;
  window.location.hash = '#/files/ep1/v3';
  await render.applyFilesRoute('ep1', 'v3');

  const diffPane = doc.getElementById('files-changes-diff');
  const before = diffLinesIn(diffPane).length;
  const fold = diffPane._descendants().find(
    (n) => n.classList && n.classList.contains('diff-fold'));
  assert(fold, 'a fold marker must be present to expand');

  // Clicking the marker splices the hidden lines back in.
  fold.dispatchEvent(makeEvent('click'));
  const after = diffLinesIn(diffPane).length;
  assert(after > before,
    'expanding a fold must reveal the hidden unchanged lines');
});

// --- The mutation-site viewer preserves horizontal scroll ------------
//
// The mutation-site detail pane is a horizontally-scrollable viewer.
// An SSE-driven repaint must NOT reset its scrollLeft — the same class
// of bug as the tournament openMatchup scroll-fight. The detail is
// routed through the keyed reconcile spine (swapIfChanged), so a no-op
// repaint leaves the scroll container's node identity intact.

test('a no-op repaint preserves the mutation-site viewer scroll position', async () => {
  filesIndexResponse = FIXTURE['/api/files'];
  render.filesState.index = null;
  render.filesState.liveGensKey = null;
  state.lineage = {
    generations: [
      { generation_id: 'v0' }, { generation_id: 'v1' },
      { generation_id: 'v2' },
    ],
    experiments: [],
  };

  window.location.hash = '#/files/ep1/v2';
  await render.applyFilesRoute('ep1', 'v2');

  // Open a patched mutation site — its detail renders a `.mutations-diff`
  // block, the horizontally-scrollable viewer.
  await render.selectMutationSite('site_one');
  const pane = doc.getElementById('mutations-detail-pane');
  const diffOf = () => pane._descendants().find(
    (n) => n.classList && n.classList.contains('mutations-diff'));
  const before = diffOf();
  assert(before, 'the patched site must render a `.mutations-diff` viewer');

  // The operator scrolls the viewer to the right.
  before.scrollLeft = 240;

  // An SSE-driven repaint re-enters the route with unchanged data.
  await render.applyFilesRoute('ep1', 'v2');
  await render.applyFilesRoute('ep1', 'v2');

  const after = diffOf();
  assert(after === before,
    'the mutation-site viewer must keep its node identity across a no-op '
      + 'repaint — that is what preserves scrollLeft');
  assertEqual(after.scrollLeft, 240,
    'a no-op repaint must NOT reset the viewer horizontal scroll position');
  assertEqual(pane.innerHTMLWriteCount(), 0,
    'a no-op repaint must never touch innerHTML in the mutation-site detail');
});

test('switching the mutation site rebuilds the detail viewer', async () => {
  filesIndexResponse = FIXTURE['/api/files'];
  render.filesState.index = null;
  render.filesState.liveGensKey = null;
  window.location.hash = '#/files/ep1/v2';
  await render.applyFilesRoute('ep1', 'v2');

  await render.selectMutationSite('site_one');
  const pane = doc.getElementById('mutations-detail-pane');
  const first = pane._descendants().find(
    (n) => n.classList && n.classList.contains('mutations-diff'));
  assert(first, 'site_one renders a diff viewer');

  // A genuine selection change must rebuild the detail — site_two is
  // unpatched, so it shows the baseline content instead of a diff.
  await render.selectMutationSite('site_two');
  assert(pane.textContent.includes('no patch has touched this site'),
    'switching to an unpatched site must rebuild the detail pane');
});

// --- Long lines stay reachable — no off-screen overflow --------------
//
// The diff must never drop content: a >100-char line is rendered in
// full, reachable by wrapping (diff) or by scroll (mutation viewer).

test('a long diff line is rendered in full — no content is truncated', async () => {
  const longOld = 'short\n';
  const longNew = 'short\n' + 'x'.repeat(400) + '\n';
  FIXTURE['/api/files/ep1/v3/diff'] = {
    epoch_id: 'ep1', generation_id: 'v3', parent_generation_id: 'v2',
    files: [{
      path: 'big.py', status: 'modified',
      old_content: longOld, new_content: longNew,
      old_binary: false, new_binary: false,
    }],
  };
  filesIndexResponse = FOLD_INDEX;
  render.filesState.index = null;
  render.filesState.liveGensKey = null;
  render.filesState.selectedGen = null;
  window.location.hash = '#/files/ep1/v3';
  await render.applyFilesRoute('ep1', 'v3');

  const diffPane = doc.getElementById('files-changes-diff');
  assert(diffPane.textContent.includes('x'.repeat(400)),
    'the full long line must be present in the DOM — never truncated; '
      + 'CSS soft-wraps it so it stays reachable on screen');

  // Restore the folding-diff fixture for any later use.
  FIXTURE['/api/files/ep1/v3/diff'] = {
    epoch_id: 'ep1', generation_id: 'v3', parent_generation_id: 'v2',
    files: [{
      path: 'big.py', status: 'modified',
      old_content: FOLD_OLD, new_content: FOLD_NEW,
      old_binary: false, new_binary: false,
    }],
  };
});

await run();
