// test/l4_conversation_diff.test.mjs — Phase-2 L4 conversation diff.
//
// The L4 (#/run/<epoch>/<gen>/<entry>) view ships:
//   * single-run transcript mode (default, no compare),
//   * compare-mode side-by-side rendering when the picker selects
//     another generation in the same epoch,
//   * a sibling-generation picker built off state.lineage.generations,
//   * compare-mode → re-fetch the compare transcript, render both columns,
//   * picker cleared → return to single mode.
//
// Each test installs a fresh DOM, mocks fetch with synchronous payloads,
// and asserts the resulting DOM.

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

// Helper: install a synchronous fetch mock. ``handler(url)`` returns the
// JSON body; the response wrapper mirrors enough of the Fetch API for
// fetchJson(). Returns a restore function the test calls before
// returning to keep fetch state hermetic across tests.
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

// Minimal lineage seed — three generations on one epoch so the compare
// picker has options. The picker must filter out the focused gen and
// list the other two in sorted order.
function seedLineage() {
  state.lineage = {
    generations: [
      { generation_id: 'v0', epoch_id: 'e0', parent_generation_id: null,
        promoted: true, created_at: '2026-05-20T00:00:00Z' },
      { generation_id: 'v1', epoch_id: 'e0', parent_generation_id: 'v0',
        promoted: false, created_at: '2026-05-20T00:10:00Z' },
      { generation_id: 'v3', epoch_id: 'e0', parent_generation_id: 'v1',
        promoted: true, created_at: '2026-05-20T00:30:00Z' },
    ],
    experiments: [],
  };
}

// Two synthetic transcripts that match the reducer's to_dict() shape.
const FOCUSED_TRANSCRIPT = {
  epoch_id: 'e0', generation_id: 'v3', entry_id: 'sample_entry',
  run_id: 'r-focused',
  event_count: 12,
  complete: true,
  turns: [
    {
      seq: 1, ts: '2026-05-20T02:00:01Z', agent: '', role: 'user',
      kind: 'run_started', text: 'Make a q3 outline.',
      tool_calls: [], tool_results: [], run_id: 'r-focused', run_index: 1,
    },
    {
      seq: 2, ts: '2026-05-20T02:00:02Z', agent: 'coordinator', role: 'agent',
      kind: 'task_completed', text: 'Structured plan: 1) topic, 2) sections.',
      tool_calls: [], tool_results: [], run_id: 'r-focused', run_index: 1,
    },
  ],
  annotations: [],
};

const COMPARE_TRANSCRIPT = {
  epoch_id: 'e0', generation_id: 'v1', entry_id: 'sample_entry',
  run_id: 'r-compare',
  event_count: 8,
  complete: true,
  turns: [
    {
      seq: 1, ts: '2026-05-20T01:00:01Z', agent: '', role: 'user',
      kind: 'run_started', text: 'Make a q3 outline.',
      tool_calls: [], tool_results: [], run_id: 'r-compare', run_index: 1,
    },
    {
      seq: 2, ts: '2026-05-20T01:00:02Z', agent: 'coordinator', role: 'agent',
      kind: 'task_completed', text: 'Rough draft, lost cohesion midway.',
      tool_calls: [], tool_results: [], run_id: 'r-compare', run_index: 1,
    },
  ],
  annotations: [],
};

function _baseFetchHandler(url) {
  if (url.includes('/transcript')) {
    // Route by the (epoch, gen, entry) tuple in the URL — focused vs
    // compare distinguish on the generation segment alone.
    if (url.includes('/e0/v3/sample_entry/transcript')) return FOCUSED_TRANSCRIPT;
    if (url.includes('/e0/v1/sample_entry/transcript')) return COMPARE_TRANSCRIPT;
    // Default empty payload for any other coordinate.
    return {
      epoch_id: 'e0', generation_id: 'unknown', entry_id: 'sample_entry',
      run_id: null, turns: [], annotations: [], event_count: 0, complete: false,
    };
  }
  if (url.includes('/expectations')) return { outcomes: [] };
  if (url.includes('/header')) {
    return {
      epoch_id: 'e0', generation_id: 'v3', entry_id: 'sample_entry',
      drift_loss: null, pass_fail: null, runtime_ms: null,
      tokens_spent: null, output_chars: null, turns_completed: null,
      plan_revisions: null, wall_clock_budget_exceeded: null, run_id: null,
    };
  }
  return { run_id: null, judges: [] };
}

// --- Test 1: single-run mode renders one column ---------------------

test('L4 single-run mode renders the focused transcript as one column', async () => {
  installRunSlots();
  seedLineage();
  runV.resetRunCaches();
  state.activeRuns = [];
  state.logTail = { events: [] };
  const restore = mockFetch(_baseFetchHandler);
  try {
    runV.renderPhase0Run({
      epochId: 'e0', generationId: 'v3', entryId: 'sample_entry',
    });
    // Two microtask flushes — first lets the fetch resolve, second lets
    // the render() ensure*() chain repaint with the data.
    await new Promise((r) => setTimeout(r, 0));
    await new Promise((r) => setTimeout(r, 0));
    runV.renderPhase0Run({
      epochId: 'e0', generationId: 'v3', entryId: 'sample_entry',
    });

    const tNode = document.getElementById('phase0-run-transcript');
    const text = tNode.textContent;
    // The placeholder text is replaced with the real transcript content.
    assert(!text.includes('Transcript renders here'),
      `placeholder text must NOT render after wire-up; got: ${text.slice(0, 400)}`);
    assert(text.includes('Structured plan'),
      `focused turn body must render; got: ${text.slice(0, 400)}`);
    // The compare-side body must NOT be present in single mode.
    assert(!text.includes('Rough draft, lost cohesion midway'),
      'single-run mode must NOT render the compare-side text');
    // Exactly one .conversation-column element exists (single mode).
    const cols = tNode.querySelectorAll('[class]');
    let columnCount = 0;
    for (const c of cols) {
      const cls = c.getAttribute('class') || '';
      if (cls.split(/\s+/).includes('conversation-column')) columnCount += 1;
    }
    assert(columnCount === 1,
      `single-run mode must render exactly one conversation-column; got ${columnCount}`);
  } finally {
    restore();
  }
});

// --- Test 2: compare mode renders two columns ----------------------

test('L4 compare mode renders two side-by-side columns aligned by turn', async () => {
  installRunSlots();
  seedLineage();
  runV.resetRunCaches();
  state.activeRuns = [];
  state.logTail = { events: [] };
  // Pre-seed the compare-gen picker state so the very first render
  // already knows it is in compare mode (mirrors a picker change that
  // already fired).
  runV.setCompareGenFor('e0', 'sample_entry', 'v1');
  const restore = mockFetch(_baseFetchHandler);
  try {
    runV.renderPhase0Run({
      epochId: 'e0', generationId: 'v3', entryId: 'sample_entry',
    });
    await new Promise((r) => setTimeout(r, 0));
    await new Promise((r) => setTimeout(r, 0));
    runV.renderPhase0Run({
      epochId: 'e0', generationId: 'v3', entryId: 'sample_entry',
    });

    const tNode = document.getElementById('phase0-run-transcript');
    const text = tNode.textContent;
    // Both transcripts' content must be present in compare mode.
    assert(text.includes('Structured plan'),
      `focused-side content must render; got: ${text.slice(0, 400)}`);
    assert(text.includes('Rough draft, lost cohesion midway'),
      `compare-side content must render; got: ${text.slice(0, 400)}`);
    // Two .conversation-column elements (one per side).
    let columnCount = 0;
    for (const c of tNode.querySelectorAll('[class]')) {
      const cls = c.getAttribute('class') || '';
      if (cls.split(/\s+/).includes('conversation-column')) columnCount += 1;
    }
    assert(columnCount === 2,
      `compare mode must render two conversation-column elements; got ${columnCount}`);
    // The run-id chip surfaces on each side so the operator can tell
    // them apart at a glance.
    assert(text.includes('r-focused') && text.includes('r-compare'),
      `each side must surface its run_id; got: ${text.slice(0, 400)}`);
  } finally {
    restore();
    runV.setCompareGenFor('e0', 'sample_entry', null);
  }
});

// --- Test 3: picker change re-fetches and re-renders -----------------

test('L4 compare picker change re-fetches the compare transcript and re-renders', async () => {
  installRunSlots();
  seedLineage();
  runV.resetRunCaches();
  state.activeRuns = [];
  state.logTail = { events: [] };
  // Start in single mode.
  runV.setCompareGenFor('e0', 'sample_entry', null);
  // Count how many times each transcript endpoint is hit.
  let focusedHits = 0;
  let compareHitsV0 = 0;
  let compareHitsV1 = 0;
  const restore = mockFetch((url) => {
    if (url.includes('/transcript')) {
      if (url.includes('/e0/v3/sample_entry/transcript')) {
        focusedHits += 1;
        return FOCUSED_TRANSCRIPT;
      }
      if (url.includes('/e0/v0/sample_entry/transcript')) {
        compareHitsV0 += 1;
        // A second compare-side transcript distinct from v1.
        return { ...COMPARE_TRANSCRIPT,
          generation_id: 'v0', run_id: 'r-compare-v0',
          turns: [
            { seq: 1, ts: '2026-05-20T00:30:00Z', agent: '', role: 'user',
              kind: 'run_started', text: 'Make a q3 outline.',
              tool_calls: [], tool_results: [],
              run_id: 'r-compare-v0', run_index: 1 },
            { seq: 2, ts: '2026-05-20T00:30:01Z', agent: 'coordinator',
              role: 'agent', kind: 'task_completed',
              text: 'Earlier draft, even less coherent.',
              tool_calls: [], tool_results: [],
              run_id: 'r-compare-v0', run_index: 1 },
          ],
        };
      }
      if (url.includes('/e0/v1/sample_entry/transcript')) {
        compareHitsV1 += 1;
        return COMPARE_TRANSCRIPT;
      }
    }
    return _baseFetchHandler(url);
  });
  try {
    runV.renderPhase0Run({
      epochId: 'e0', generationId: 'v3', entryId: 'sample_entry',
    });
    await new Promise((r) => setTimeout(r, 0));
    await new Promise((r) => setTimeout(r, 0));
    runV.renderPhase0Run({
      epochId: 'e0', generationId: 'v3', entryId: 'sample_entry',
    });

    // After the first render, focused has been fetched, compare has not.
    assert(focusedHits >= 1, `focused side must have been fetched once; hits=${focusedHits}`);
    assert(compareHitsV0 === 0, `compare v0 must NOT be fetched in single mode; hits=${compareHitsV0}`);
    assert(compareHitsV1 === 0, `compare v1 must NOT be fetched in single mode; hits=${compareHitsV1}`);

    // Pick a compare target and re-render.
    runV.setCompareGenFor('e0', 'sample_entry', 'v0');
    runV.renderPhase0Run({
      epochId: 'e0', generationId: 'v3', entryId: 'sample_entry',
    });
    await new Promise((r) => setTimeout(r, 0));
    await new Promise((r) => setTimeout(r, 0));
    runV.renderPhase0Run({
      epochId: 'e0', generationId: 'v3', entryId: 'sample_entry',
    });

    assert(compareHitsV0 >= 1,
      `compare v0 must be fetched after the picker switches; hits=${compareHitsV0}`);
    const text = document.getElementById('phase0-run-transcript').textContent;
    assert(text.includes('Earlier draft, even less coherent'),
      `v0 compare-side content must render after picker change; got: ${text.slice(0, 400)}`);

    // Now switch to v1 and confirm the v1 transcript is fetched + rendered.
    runV.setCompareGenFor('e0', 'sample_entry', 'v1');
    runV.renderPhase0Run({
      epochId: 'e0', generationId: 'v3', entryId: 'sample_entry',
    });
    await new Promise((r) => setTimeout(r, 0));
    await new Promise((r) => setTimeout(r, 0));
    runV.renderPhase0Run({
      epochId: 'e0', generationId: 'v3', entryId: 'sample_entry',
    });
    assert(compareHitsV1 >= 1,
      `compare v1 must be fetched after the picker switches; hits=${compareHitsV1}`);
    const text2 = document.getElementById('phase0-run-transcript').textContent;
    assert(text2.includes('Rough draft, lost cohesion midway'),
      `v1 compare-side content must render after second picker change; got: ${text2.slice(0, 400)}`);
  } finally {
    restore();
    runV.setCompareGenFor('e0', 'sample_entry', null);
  }
});

// --- Test 4: clearing the picker returns to single-run mode ---------

test('L4 clearing the compare picker returns to single-run mode', async () => {
  installRunSlots();
  seedLineage();
  runV.resetRunCaches();
  state.activeRuns = [];
  state.logTail = { events: [] };
  runV.setCompareGenFor('e0', 'sample_entry', 'v1');
  const restore = mockFetch(_baseFetchHandler);
  try {
    runV.renderPhase0Run({
      epochId: 'e0', generationId: 'v3', entryId: 'sample_entry',
    });
    await new Promise((r) => setTimeout(r, 0));
    await new Promise((r) => setTimeout(r, 0));
    runV.renderPhase0Run({
      epochId: 'e0', generationId: 'v3', entryId: 'sample_entry',
    });
    const before = document.getElementById('phase0-run-transcript').textContent;
    assert(before.includes('Rough draft, lost cohesion midway'),
      'compare-mode body must render initially');

    // Clear the picker — back to single mode.
    runV.setCompareGenFor('e0', 'sample_entry', null);
    runV.renderPhase0Run({
      epochId: 'e0', generationId: 'v3', entryId: 'sample_entry',
    });
    await new Promise((r) => setTimeout(r, 0));
    await new Promise((r) => setTimeout(r, 0));
    runV.renderPhase0Run({
      epochId: 'e0', generationId: 'v3', entryId: 'sample_entry',
    });

    const tNode = document.getElementById('phase0-run-transcript');
    const after = tNode.textContent;
    // Compare body gone; focused body still present.
    assert(!after.includes('Rough draft, lost cohesion midway'),
      `compare body must NOT render after picker clear; got: ${after.slice(0, 400)}`);
    assert(after.includes('Structured plan'),
      `focused body must still render; got: ${after.slice(0, 400)}`);
    // Exactly one conversation-column again.
    let columnCount = 0;
    for (const c of tNode.querySelectorAll('[class]')) {
      const cls = c.getAttribute('class') || '';
      if (cls.split(/\s+/).includes('conversation-column')) columnCount += 1;
    }
    assert(columnCount === 1,
      `back to single-run mode must render exactly one column; got ${columnCount}`);
  } finally {
    restore();
  }
});

// --- Test 5: picker lists every sibling gen on the focused epoch ----

test('L4 compare picker lists every sibling generation in the focused epoch', async () => {
  installRunSlots();
  seedLineage();
  runV.resetRunCaches();
  state.activeRuns = [];
  state.logTail = { events: [] };
  runV.setCompareGenFor('e0', 'sample_entry', null);
  const restore = mockFetch(_baseFetchHandler);
  try {
    runV.renderPhase0Run({
      epochId: 'e0', generationId: 'v3', entryId: 'sample_entry',
    });
    await new Promise((r) => setTimeout(r, 0));
    await new Promise((r) => setTimeout(r, 0));
    runV.renderPhase0Run({
      epochId: 'e0', generationId: 'v3', entryId: 'sample_entry',
    });
    const tNode = document.getElementById('phase0-run-transcript');
    // Find the picker — a <select> in the transcript card.
    let picker = null;
    for (const c of tNode.querySelectorAll('[class]')) {
      void c;
    }
    // The simple test-harness querySelector is attribute-only; walk the
    // tree manually for a SELECT.
    const walk = (n) => {
      if (n && n.localName === 'select') return n;
      const kids = (n && n.children) || [];
      for (const k of kids) { const r = walk(k); if (r) return r; }
      return null;
    };
    picker = walk(tNode);
    assert(picker !== null, 'compare picker <select> must be present');
    const opts = picker.children.filter((c) => c.localName === 'option');
    // 1 "off" option + 2 sibling generations (v0, v1) = 3 total. The
    // focused gen v3 must be filtered out.
    assert(opts.length === 3,
      `picker must have 3 options (off + v0 + v1); got ${opts.length}`);
    const values = opts.map((o) => o.getAttribute('value'));
    assert(values.includes('') && values.includes('v0') && values.includes('v1'),
      `picker values must be ['', 'v0', 'v1']; got ${JSON.stringify(values)}`);
    assert(!values.includes('v3'),
      `picker must NOT include the focused gen v3; got ${JSON.stringify(values)}`);
  } finally {
    restore();
  }
});

// --- Test 6: compare-side empty payload renders empty-state ---------

test('L4 compare side with no run on disk renders an empty-state column', async () => {
  installRunSlots();
  seedLineage();
  runV.resetRunCaches();
  state.activeRuns = [];
  state.logTail = { events: [] };
  // Pick a compare target with no events on disk.
  runV.setCompareGenFor('e0', 'sample_entry', 'v1');
  const restore = mockFetch((url) => {
    if (url.includes('/e0/v1/sample_entry/transcript')) {
      // The backend returns an empty payload (HTTP 200) for a missing
      // run — single source of empty-state copy lives in the frontend.
      return {
        epoch_id: 'e0', generation_id: 'v1', entry_id: 'sample_entry',
        run_id: null, turns: [], annotations: [],
        event_count: 0, complete: false,
      };
    }
    return _baseFetchHandler(url);
  });
  try {
    runV.renderPhase0Run({
      epochId: 'e0', generationId: 'v3', entryId: 'sample_entry',
    });
    await new Promise((r) => setTimeout(r, 0));
    await new Promise((r) => setTimeout(r, 0));
    runV.renderPhase0Run({
      epochId: 'e0', generationId: 'v3', entryId: 'sample_entry',
    });
    const tNode = document.getElementById('phase0-run-transcript');
    const text = tNode.textContent;
    // The focused side renders its content; the compare side reports
    // its empty state.
    assert(text.includes('Structured plan'),
      `focused-side content must render; got: ${text.slice(0, 400)}`);
    assert(text.includes('No transcript available for the compare target')
      || text.includes('No transcript recorded for this run'),
      `compare-side empty-state must render; got: ${text.slice(0, 600)}`);
  } finally {
    restore();
    runV.setCompareGenFor('e0', 'sample_entry', null);
  }
});

await run();
