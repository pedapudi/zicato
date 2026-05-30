// test/l4_events_filter.test.mjs — L4 events-log filter + key-event
// emphasis.
//
// The L4 events chip stream was an unfiltered feed. The intuitiveness
// wave adds (1) a key-events-only filter toggle and (2) visual emphasis
// (an accent class) on high-signal event kinds — drift spikes, plan
// revisions, steering interventions, and hard failures. These tests pin
// the classifier, the per-row emphasis, and the filter narrowing.

import { installDom, test, run, assert, assertEqual } from './harness.mjs';

installDom();

const { state } = await import('../js/core/state.js');
const runV = await import('../js/views/phase0_run.js');

function installNode(id, tag = 'div') {
  let stale = document.getElementById(id);
  while (stale) {
    if (stale.parentNode) stale.parentNode.removeChild(stale);
    stale = document.getElementById(id);
  }
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
      ok: true, status: 200, headers: new Map(),
      json: async () => body, text: async () => JSON.stringify(body),
    };
  };
  return () => { globalThis.fetch = original; };
}

function _baseFetchHandler() { return { run_id: null, judges: [] }; }

function settle(params) {
  runV.renderPhase0Run(params);
}

// A mixed feed: 2 high-signal events (drift spike, plan revision), 1
// steering intervention, and 2 routine events.
function seedMixedEvents() {
  state.logTail = {
    events: [
      { kind: 'run_started', ts: '2026-05-20T00:00:01Z', summary: 'run begins' },
      { kind: 'drift_detected', ts: '2026-05-20T00:00:02Z', summary: 'off_topic spike' },
      { kind: 'judgement_emitted', ts: '2026-05-20T00:00:03Z', summary: 'judge scored' },
      { kind: 'plan_revised', ts: '2026-05-20T00:00:04Z', summary: 'replanned task' },
      { kind: 'steering_event', ts: '2026-05-20T00:00:05Z', summary: 'operator nudge' },
    ],
  };
  state.logEventsPath = '/tmp/events.jsonl';
  state.logCursor = 5;
}

function _countRows(node, opts) {
  let n = 0;
  for (const c of node.querySelectorAll('[class]')) {
    const cls = (c.getAttribute('class') || '').split(/\s+/);
    if (!cls.includes('events-row')) continue;
    if (opts && opts.keyOnly && !cls.includes('events-row-key')) continue;
    n += 1;
  }
  return n;
}

// --- classifier -----------------------------------------------------

test('isKeyEvent flags drift / plan / steering / failure, not routine events', () => {
  assert(runV.isKeyEvent({ kind: 'drift_detected' }), 'drift is key');
  assert(runV.isKeyEvent({ kind: 'plan_revised' }), 'plan revision is key');
  assert(runV.isKeyEvent({ kind: 'steering_event' }), 'steering is key');
  assert(runV.isKeyEvent({ kind: 'run_failed' }), 'failure is key');
  assert(!runV.isKeyEvent({ kind: 'run_started' }), 'run_started is routine');
  assert(!runV.isKeyEvent({ kind: 'judgement_emitted' }), 'judgement is routine');
  assert(!runV.isKeyEvent({ kind: '' }), 'empty kind is not key');
});

// --- emphasis + toggle ----------------------------------------------

test('events feed emphasises key rows and renders the filter toggle', () => {
  installRunSlots();
  runV.resetRunCaches();
  seedMixedEvents();
  const restore = mockFetch(_baseFetchHandler);
  try {
    runV.setEventsKeyOnly(false);
    settle({ epochId: 'e0', generationId: 'v1', entryId: 'sample_entry' });
    const node = document.getElementById('phase0-run-events');

    // All five rows render in the unfiltered feed.
    assertEqual(_countRows(node), 5, 'unfiltered feed shows all five rows');
    // Three of them are flagged key (drift, plan, steering).
    assertEqual(_countRows(node, { keyOnly: true }), 3,
      'three rows must carry the key-event emphasis class');

    // The filter toggle checkbox is present + reports the key/total count.
    const toggle = node.querySelector('[data-events-filter="key-only"]');
    assert(toggle != null, 'key-only filter checkbox must render');
    assert(node.textContent.includes('3 key / 5 total'),
      `filter bar must report the key/total count; got: ${node.textContent.slice(0, 200)}`);
  } finally {
    restore();
    runV.resetRunCaches();
  }
});

test('toggling key-only narrows the feed to the high-signal events', () => {
  installRunSlots();
  runV.resetRunCaches();
  seedMixedEvents();
  const restore = mockFetch(_baseFetchHandler);
  try {
    runV.setEventsKeyOnly(true);
    // Force render so the digest gate does not suppress the repaint.
    runV.resetRunRenderDigest();
    settle({ epochId: 'e0', generationId: 'v1', entryId: 'sample_entry' });
    const node = document.getElementById('phase0-run-events');

    assert(runV.eventsKeyOnly() === true, 'key-only mode must be active');
    assertEqual(_countRows(node), 3,
      'key-only feed must show exactly the three high-signal rows');
    // Every visible row must be a key row.
    assertEqual(_countRows(node, { keyOnly: true }), 3,
      'every visible row in key-only mode must carry the emphasis class');
  } finally {
    restore();
    runV.setEventsKeyOnly(false);
    runV.resetRunCaches();
  }
});

await run();
