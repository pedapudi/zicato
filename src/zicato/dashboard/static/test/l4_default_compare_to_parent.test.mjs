// test/l4_default_compare_to_parent.test.mjs
//
// L4 compare picker auto-default — when the user navigates to a
// challenger run the picker should preselect the focused gen's
// parent_generation_id (the champion-at-time-of-challenge). The
// side-by-side is the useful view for any non-seed generation; today
// the picker starts at "(off)" and the operator has to find it and
// pick the parent manually. This file pins the auto-default behaviour
// across the edge cases.
//
// Each test installs a fresh DOM, seeds state.epochDef + state.lineage,
// mocks fetch with synchronous payloads, and asserts the resolved
// picker target + the rendered DOM.

import { installDom, test, run, assert } from './harness.mjs';

installDom();

const { state } = await import('../js/core/state.js');
const runV = await import('../js/views/phase0_run.js');

function installNode(id, tag = 'div') {
  const node = document.createElement(tag);
  node.id = id;
  document.body.appendChild(node);
  return node;
}

function installRunSlots() {
  installNode('phase0-run-header');
  installNode('phase0-run-expectation');
  installNode('phase0-run-judges');
  installNode('phase0-run-transcript');
  installNode('phase0-run-events');
}

function mockFetch(handler) {
  const original = globalThis.fetch;
  globalThis.fetch = async (url) => {
    const body = handler(url);
    return {
      ok: true,
      status: 200,
      headers: new Map(),
      json: async () => body,
      text: async () => JSON.stringify(body),
    };
  };
  return () => { globalThis.fetch = original; };
}

// Five-generation epoch — v0 is the seed (no parent), v1..v4 chain
// off v0 -> v1 -> v2 -> v3 (v2 promoted, v3 rejected). The picker's
// auto-default is "parent_generation_id from state.epochDef.experiments
// for the focused generation".
function seedFiveGenEpoch() {
  state.lineage = {
    generations: [
      { generation_id: 'v0', epoch_id: 'e0', parent_generation_id: null,
        promoted: true, created_at: '2026-05-20T00:00:00Z' },
      { generation_id: 'v1', epoch_id: 'e0', parent_generation_id: 'v0',
        promoted: true, created_at: '2026-05-20T00:10:00Z' },
      { generation_id: 'v2', epoch_id: 'e0', parent_generation_id: 'v1',
        promoted: false, created_at: '2026-05-20T00:20:00Z' },
      { generation_id: 'v3', epoch_id: 'e0', parent_generation_id: 'v1',
        promoted: true, created_at: '2026-05-20T00:30:00Z' },
      { generation_id: 'v4', epoch_id: 'e0', parent_generation_id: 'v3',
        parent_id: 'v3', promoted: false, created_at: '2026-05-20T00:40:00Z' },
    ],
    experiments: [],
  };
  state.epochDef = {
    epoch_id: 'e0',
    experiments: [
      // v0 is the seed — no experiment record (or one with null parent).
      { generation_id: 'v0', parent_generation_id: null, hypothesis: {},
        outcome: { tournament_decision: 'promoted' } },
      { generation_id: 'v1', parent_generation_id: 'v0', hypothesis: {},
        outcome: { tournament_decision: 'promoted' } },
      { generation_id: 'v2', parent_generation_id: 'v1', hypothesis: {},
        outcome: { tournament_decision: 'rejected' } },
      { generation_id: 'v3', parent_generation_id: 'v1', hypothesis: {},
        outcome: { tournament_decision: 'promoted' } },
      { generation_id: 'v4', parent_generation_id: 'v3', hypothesis: {},
        outcome: { tournament_decision: 'rejected' } },
    ],
  };
}

// Synthetic transcripts — one per generation we need to render. The
// fetch handler dispatches by the (epoch, gen, entry) tuple in the URL.
const TRANSCRIPTS = {
  v0: { run_id: 'r-v0', body: 'seed champion outline.' },
  v1: { run_id: 'r-v1', body: 'champion v1 outline content.' },
  v2: { run_id: 'r-v2', body: 'challenger v2 attempt that regressed.' },
  v3: { run_id: 'r-v3', body: 'challenger v3 promoted upgrade.' },
  v4: { run_id: 'r-v4', body: 'challenger v4 rejected attempt.' },
};

function _transcriptPayload(gen) {
  const t = TRANSCRIPTS[gen];
  if (!t) {
    return {
      epoch_id: 'e0', generation_id: gen, entry_id: 'sample_entry',
      run_id: null, turns: [], annotations: [], event_count: 0, complete: false,
    };
  }
  return {
    epoch_id: 'e0', generation_id: gen, entry_id: 'sample_entry',
    run_id: t.run_id, event_count: 4, complete: true,
    turns: [
      { seq: 1, ts: '2026-05-20T00:00:01Z', agent: '', role: 'user',
        kind: 'run_started', text: 'Make a q3 outline.',
        tool_calls: [], tool_results: [], run_id: t.run_id, run_index: 1 },
      { seq: 2, ts: '2026-05-20T00:00:02Z', agent: 'coordinator', role: 'agent',
        kind: 'task_completed', text: t.body,
        tool_calls: [], tool_results: [], run_id: t.run_id, run_index: 1 },
    ],
    annotations: [],
  };
}

function _baseFetchHandler(url) {
  if (url.includes('/transcript')) {
    const m = url.match(/\/run\/e0\/([^/]+)\/sample_entry\/transcript/);
    if (m) return _transcriptPayload(m[1]);
    return _transcriptPayload('unknown');
  }
  if (url.includes('/expectations')) return { outcomes: [] };
  if (url.includes('/header')) {
    return {
      epoch_id: 'e0', generation_id: 'unknown', entry_id: 'sample_entry',
      drift_loss: null, pass_fail: null, runtime_ms: null,
      tokens_spent: null, output_chars: null, turns_completed: null,
      plan_revisions: null, wall_clock_budget_exceeded: null, run_id: null,
    };
  }
  return { run_id: null, judges: [] };
}

// Settle a render — two microtask flushes match the existing L4 test
// suite's invariant: first lets the fetch resolve, second lets the
// repaint callback land the data into the DOM.
async function settle(params) {
  runV.renderPhase0Run(params);
  await new Promise((r) => setTimeout(r, 0));
  await new Promise((r) => setTimeout(r, 0));
  runV.renderPhase0Run(params);
}

function _findSelect(node) {
  if (node && node.localName === 'select') return node;
  const kids = (node && node.children) || [];
  for (const k of kids) { const r = _findSelect(k); if (r) return r; }
  return null;
}

function _selectedOption(selectEl) {
  const opts = (selectEl && selectEl.children) || [];
  for (const o of opts) {
    if (o.localName !== 'option') continue;
    const sel = o.getAttribute && o.getAttribute('selected');
    if (sel != null) return o.getAttribute('value');
  }
  // Fall back to .value — the picker writes that too.
  return (selectEl && selectEl.value) || '';
}

function _resetRunState() {
  runV.resetRunCaches();
  state.activeRuns = [];
  state.logTail = { events: [] };
  state.epochDef = null;
}

// --- Test 1: v2 auto-defaults to parent v1 --------------------------

test('L4 picker auto-defaults to parent v1 when navigating to challenger v2', async () => {
  installRunSlots();
  _resetRunState();
  seedFiveGenEpoch();
  const restore = mockFetch(_baseFetchHandler);
  try {
    await settle({ epochId: 'e0', generationId: 'v2', entryId: 'sample_entry' });
    // The picker module's resolver returns v1 for the v2 focused entry.
    const resolved = runV.compareGenFor('e0', 'sample_entry', 'v2');
    assert(resolved === 'v1',
      `compareGenFor must resolve to parent v1; got ${JSON.stringify(resolved)}`);

    const tNode = document.getElementById('phase0-run-transcript');
    const text = tNode.textContent;
    // Side-by-side renders both transcripts.
    assert(text.includes('challenger v2 attempt that regressed'),
      `focused-side v2 transcript must render; got: ${text.slice(0, 400)}`);
    assert(text.includes('champion v1 outline content'),
      `compare-side v1 transcript must render; got: ${text.slice(0, 400)}`);
    // Two .conversation-column elements (compare mode).
    let columnCount = 0;
    for (const c of tNode.querySelectorAll('[class]')) {
      const cls = c.getAttribute('class') || '';
      if (cls.split(/\s+/).includes('conversation-column')) columnCount += 1;
    }
    assert(columnCount === 2,
      `compare mode must render two columns; got ${columnCount}`);
    // The native <select> reflects the chosen value so a DOM
    // introspection (and the harness preselection) sees v1.
    const sel = _findSelect(tNode);
    assert(sel !== null, 'compare picker <select> must be present');
    assert(_selectedOption(sel) === 'v1',
      `picker must report v1 selected; got ${JSON.stringify(_selectedOption(sel))}`);
  } finally {
    restore();
    runV.setCompareGenFor('e0', 'sample_entry', null);
    runV.resetRunCaches();
  }
});

// --- Test 2: v0 (no parent) stays at (off) --------------------------

test('L4 picker stays at (off) for the seed gen v0 with no parent', async () => {
  installRunSlots();
  _resetRunState();
  seedFiveGenEpoch();
  const restore = mockFetch(_baseFetchHandler);
  try {
    await settle({ epochId: 'e0', generationId: 'v0', entryId: 'sample_entry' });
    const resolved = runV.compareGenFor('e0', 'sample_entry', 'v0');
    assert(resolved === null,
      `compareGenFor must be null for the seed gen; got ${JSON.stringify(resolved)}`);

    const tNode = document.getElementById('phase0-run-transcript');
    // Single-column mode — exactly one conversation-column.
    let columnCount = 0;
    for (const c of tNode.querySelectorAll('[class]')) {
      const cls = c.getAttribute('class') || '';
      if (cls.split(/\s+/).includes('conversation-column')) columnCount += 1;
    }
    assert(columnCount === 1,
      `seed gen must render single-column mode; got ${columnCount}`);
    const sel = _findSelect(tNode);
    assert(sel !== null, 'compare picker <select> must still be present');
    assert(_selectedOption(sel) === '',
      `picker must report (off) selected; got ${JSON.stringify(_selectedOption(sel))}`);
  } finally {
    restore();
    runV.resetRunCaches();
  }
});

// --- Test 3: user explicit "(off)" sticks across re-renders --------

test('L4 user explicitly picks (off) after auto-default — sticks across re-renders', async () => {
  installRunSlots();
  _resetRunState();
  seedFiveGenEpoch();
  const restore = mockFetch(_baseFetchHandler);
  try {
    // Initial render auto-defaults to v1.
    await settle({ epochId: 'e0', generationId: 'v2', entryId: 'sample_entry' });
    assert(runV.compareGenFor('e0', 'sample_entry', 'v2') === 'v1',
      'precondition: auto-default to v1');

    // User explicitly picks "(off)" — setCompareGenFor with null.
    runV.setCompareGenFor('e0', 'sample_entry', null);
    await settle({ epochId: 'e0', generationId: 'v2', entryId: 'sample_entry' });
    assert(runV.compareGenFor('e0', 'sample_entry', 'v2') === null,
      `user "(off)" must override the parent default; got `
      + JSON.stringify(runV.compareGenFor('e0', 'sample_entry', 'v2')));

    // Render again — still off.
    await settle({ epochId: 'e0', generationId: 'v2', entryId: 'sample_entry' });
    assert(runV.compareGenFor('e0', 'sample_entry', 'v2') === null,
      'user "(off)" must stick across subsequent renders');

    const tNode = document.getElementById('phase0-run-transcript');
    let columnCount = 0;
    for (const c of tNode.querySelectorAll('[class]')) {
      const cls = c.getAttribute('class') || '';
      if (cls.split(/\s+/).includes('conversation-column')) columnCount += 1;
    }
    assert(columnCount === 1,
      `user "(off)" must render single column; got ${columnCount}`);
  } finally {
    restore();
    runV.resetRunCaches();
  }
});

// --- Test 4: user picks a non-default gen — sticks ------------------

test('L4 user explicitly picks v0 from default-parent v1 — sticks across re-renders', async () => {
  installRunSlots();
  _resetRunState();
  seedFiveGenEpoch();
  const restore = mockFetch(_baseFetchHandler);
  try {
    // v3's parent is v1 — initial auto-default.
    await settle({ epochId: 'e0', generationId: 'v3', entryId: 'sample_entry' });
    assert(runV.compareGenFor('e0', 'sample_entry', 'v3') === 'v1',
      'precondition: auto-default to parent v1');

    // User explicitly picks v0 (not the parent).
    runV.setCompareGenFor('e0', 'sample_entry', 'v0');
    await settle({ epochId: 'e0', generationId: 'v3', entryId: 'sample_entry' });
    assert(runV.compareGenFor('e0', 'sample_entry', 'v3') === 'v0',
      `user pick v0 must override the parent default; got `
      + JSON.stringify(runV.compareGenFor('e0', 'sample_entry', 'v3')));

    // Re-render with no further user action — still v0.
    await settle({ epochId: 'e0', generationId: 'v3', entryId: 'sample_entry' });
    assert(runV.compareGenFor('e0', 'sample_entry', 'v3') === 'v0',
      'user pick v0 must stick across renders');

    const tNode = document.getElementById('phase0-run-transcript');
    const text = tNode.textContent;
    assert(text.includes('seed champion outline'),
      `compare side must render v0 transcript content; got: ${text.slice(0, 400)}`);
    // The picker reflects v0.
    const sel = _findSelect(tNode);
    assert(_selectedOption(sel) === 'v0',
      `picker must report v0 selected; got ${JSON.stringify(_selectedOption(sel))}`);
  } finally {
    restore();
    runV.resetRunCaches();
  }
});

// --- Test 5: cold deep-link — epochDef arrives mid-render ----------

test('L4 cold deep-link — picker is (off) pre-hydration, defaults to parent once state.epochDef lands', async () => {
  installRunSlots();
  _resetRunState();
  // Seed only the lineage so the picker has options to list, but
  // intentionally leave state.epochDef null — this is the cold deep-link
  // where the SSE snapshot has not yet folded the epoch contract in.
  state.lineage = {
    generations: [
      { generation_id: 'v0', epoch_id: 'e0', parent_generation_id: null,
        promoted: true, created_at: '2026-05-20T00:00:00Z' },
      { generation_id: 'v1', epoch_id: 'e0', parent_generation_id: 'v0',
        promoted: true, created_at: '2026-05-20T00:10:00Z' },
      { generation_id: 'v2', epoch_id: 'e0', parent_generation_id: 'v1',
        promoted: false, created_at: '2026-05-20T00:20:00Z' },
    ],
    experiments: [],
  };
  const restore = mockFetch(_baseFetchHandler);
  try {
    // Pre-hydration render: epochDef is null, so the picker resolver
    // can not find the parent and falls back to (off).
    await settle({ epochId: 'e0', generationId: 'v2', entryId: 'sample_entry' });
    const beforeResolved = runV.compareGenFor('e0', 'sample_entry', 'v2');
    assert(beforeResolved === null,
      `pre-hydration picker must be (off); got ${JSON.stringify(beforeResolved)}`);
    const tNodeBefore = document.getElementById('phase0-run-transcript');
    let beforeCols = 0;
    for (const c of tNodeBefore.querySelectorAll('[class]')) {
      const cls = c.getAttribute('class') || '';
      if (cls.split(/\s+/).includes('conversation-column')) beforeCols += 1;
    }
    assert(beforeCols === 1,
      `pre-hydration must be single-column; got ${beforeCols}`);

    // SSE snapshot arrives — state.epochDef gets populated. The next
    // render must pick up the auto-default.
    state.epochDef = {
      epoch_id: 'e0',
      experiments: [
        { generation_id: 'v0', parent_generation_id: null, hypothesis: {},
          outcome: { tournament_decision: 'promoted' } },
        { generation_id: 'v1', parent_generation_id: 'v0', hypothesis: {},
          outcome: { tournament_decision: 'promoted' } },
        { generation_id: 'v2', parent_generation_id: 'v1', hypothesis: {},
          outcome: { tournament_decision: 'rejected' } },
      ],
    };

    await settle({ epochId: 'e0', generationId: 'v2', entryId: 'sample_entry' });
    const afterResolved = runV.compareGenFor('e0', 'sample_entry', 'v2');
    assert(afterResolved === 'v1',
      `post-hydration picker must default to v1; got `
      + JSON.stringify(afterResolved));

    const tNodeAfter = document.getElementById('phase0-run-transcript');
    let afterCols = 0;
    for (const c of tNodeAfter.querySelectorAll('[class]')) {
      const cls = c.getAttribute('class') || '';
      if (cls.split(/\s+/).includes('conversation-column')) afterCols += 1;
    }
    assert(afterCols === 2,
      `post-hydration must be compare mode (two columns); got ${afterCols}`);
    const text = tNodeAfter.textContent;
    assert(text.includes('champion v1 outline content'),
      `compare-side v1 content must render after hydration; got: ${text.slice(0, 400)}`);
  } finally {
    restore();
    runV.resetRunCaches();
  }
});

// --- Test 6: defaultCompareGenFor — null safety + missing record ----

test('L4 defaultCompareGenFor handles null epochDef / missing experiment / empty parent gracefully', () => {
  _resetRunState();
  // No epochDef — null.
  assert(runV.defaultCompareGenFor('v2') === null,
    'null epochDef must return null');

  // epochDef without experiments array — null.
  state.epochDef = { epoch_id: 'e0' };
  assert(runV.defaultCompareGenFor('v2') === null,
    'epochDef without experiments must return null');

  // experiments array but no matching record — null.
  state.epochDef = { epoch_id: 'e0', experiments: [
    { generation_id: 'v1', parent_generation_id: 'v0' },
  ] };
  assert(runV.defaultCompareGenFor('v2') === null,
    'missing experiment record must return null');

  // experiment with null parent — null.
  state.epochDef = { epoch_id: 'e0', experiments: [
    { generation_id: 'v0', parent_generation_id: null },
  ] };
  assert(runV.defaultCompareGenFor('v0') === null,
    'experiment with null parent must return null');

  // experiment with empty-string parent — null.
  state.epochDef = { epoch_id: 'e0', experiments: [
    { generation_id: 'v2', parent_generation_id: '' },
  ] };
  assert(runV.defaultCompareGenFor('v2') === null,
    'experiment with empty parent must return null');

  // No generationId argument — null.
  assert(runV.defaultCompareGenFor(null) === null,
    'null generationId must return null');
  assert(runV.defaultCompareGenFor('') === null,
    'empty generationId must return null');

  // Happy path — returns the parent.
  state.epochDef = { epoch_id: 'e0', experiments: [
    { generation_id: 'v2', parent_generation_id: 'v1' },
  ] };
  assert(runV.defaultCompareGenFor('v2') === 'v1',
    'happy path must return parent gen id');

  runV.resetRunCaches();
});

// --- Context-preserving L3→L4 transition ----------------------------
//
// When the user drills into an entry FROM a decision (L3), the run hash
// carries the matchup champion as a 4th segment (``…/vs-<champion>``).
// The compare picker must DEFAULT to that champion — the exact matchup
// the operator was judging — instead of the lineage parent.

function _setHash(h) {
  if (typeof window === 'undefined') globalThis.window = {};
  if (!window.location) window.location = { hash: '', search: '' };
  window.location.hash = h;
}

test('matchupChampionFromHash parses the vs-<champion> segment off the run route', () => {
  assert(runV.matchupChampionFromHash('#/run/e0/v3/sample_entry/vs-v2') === 'v2',
    'matchup champion v2 must be recovered from the run hash');
  // Plain run deep-link (no matchup) → null.
  assert(runV.matchupChampionFromHash('#/run/e0/v3/sample_entry') === null,
    'a plain run deep-link must yield no matchup champion');
  // Non-run route → null.
  assert(runV.matchupChampionFromHash('#/gen/e0/v3') === null,
    'a non-run route must yield no matchup champion');
  // Malformed 4th segment (not vs-prefixed) → null.
  assert(runV.matchupChampionFromHash('#/run/e0/v3/sample_entry/whatever') === null,
    'a 4th segment without the vs- prefix must yield no matchup champion');
});

test('L4 from a decision: picker defaults to the matchup champion, not the lineage parent', async () => {
  installRunSlots();
  _resetRunState();
  seedFiveGenEpoch();
  // v3's lineage parent is v1; but the matchup the operator judged was
  // v3 vs v0 (carried on the hash). The picker must land on v0.
  _setHash('#/run/e0/v3/sample_entry/vs-v0');
  const restore = mockFetch(_baseFetchHandler);
  try {
    await settle({ epochId: 'e0', generationId: 'v3', entryId: 'sample_entry' });
    const resolved = runV.compareGenFor('e0', 'sample_entry', 'v3');
    assert(resolved === 'v0',
      `matchup context must default the picker to champion v0; got ${JSON.stringify(resolved)}`);

    const tNode = document.getElementById('phase0-run-transcript');
    const text = tNode.textContent;
    assert(text.includes('seed champion outline'),
      `compare side must render the matchup champion (v0) transcript; got: ${text.slice(0, 400)}`);
    const sel = _findSelect(tNode);
    assert(sel !== null && _selectedOption(sel) === 'v0',
      `picker must report v0 (the matchup champion) selected; got ${JSON.stringify(sel && _selectedOption(sel))}`);
  } finally {
    restore();
    _setHash('');
    runV.setCompareGenFor('e0', 'sample_entry', null);
    runV.resetRunCaches();
  }
});

test('L4 from a decision: an explicit user pick still overrides the matchup default', async () => {
  installRunSlots();
  _resetRunState();
  seedFiveGenEpoch();
  _setHash('#/run/e0/v3/sample_entry/vs-v0');
  const restore = mockFetch(_baseFetchHandler);
  try {
    await settle({ epochId: 'e0', generationId: 'v3', entryId: 'sample_entry' });
    assert(runV.compareGenFor('e0', 'sample_entry', 'v3') === 'v0',
      'precondition: matchup default v0');
    // User picks v1 — explicit override wins over the matchup hint.
    runV.setCompareGenFor('e0', 'sample_entry', 'v1');
    await settle({ epochId: 'e0', generationId: 'v3', entryId: 'sample_entry' });
    assert(runV.compareGenFor('e0', 'sample_entry', 'v3') === 'v1',
      `explicit user pick must override the matchup default; got `
      + JSON.stringify(runV.compareGenFor('e0', 'sample_entry', 'v3')));
  } finally {
    restore();
    _setHash('');
    runV.resetRunCaches();
  }
});

test('non-matchup cold deep-link still defaults to the lineage parent (no regression)', async () => {
  installRunSlots();
  _resetRunState();
  seedFiveGenEpoch();
  // Plain run deep-link — no vs- segment. Must keep the parent default.
  _setHash('#/run/e0/v3/sample_entry');
  const restore = mockFetch(_baseFetchHandler);
  try {
    await settle({ epochId: 'e0', generationId: 'v3', entryId: 'sample_entry' });
    const resolved = runV.compareGenFor('e0', 'sample_entry', 'v3');
    assert(resolved === 'v1',
      `plain deep-link must default to lineage parent v1; got ${JSON.stringify(resolved)}`);
  } finally {
    restore();
    _setHash('');
    runV.resetRunCaches();
  }
});

await run();
