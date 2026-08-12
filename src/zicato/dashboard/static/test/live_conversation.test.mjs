// test/live_conversation.test.mjs — the live conversation pane (issue #194 §2).
//
// The four traps §2 names are the four things this file proves, because each
// of them is invisible in a screenshot and only shows up as a bad feeling
// while an operator reads a running conversation:
//
//  1. APPEND, NEVER REBUILD — asserted by NODE IDENTITY. Counting turns is not
//     enough: a pane that threw the thread away and rebuilt it renders the same
//     count. So the tests hold references to the actual turn nodes and require
//     the same objects to still be there after growth.
//  2. LIVE → SETTLED WITHOUT A REMOUNT — the same assertion, across the moment
//     the run finishes: the scroller and its nodes must survive the transition
//     that changes the caption.
//  3. FIDELITY HONESTY — the caption names what was rendered, in the shared
//     vocabulary, and does not claim a verbatim capture that is not there.
//  4. AUTOSCROLL WITH PIN — a reader at the tail is carried along; a reader who
//     scrolled up is not moved, and is told how much they are behind.
//
// Plus the cursor protocol the pane rides on (idempotence, splice-by-index,
// and the two ways a delta can leave a hole).

import { installDom, test, run, assert, assertEqual } from './harness.mjs';

installDom();

const { mountConversationPane, captionText } = await import('../js/convo.js');
const { spliceTurns, mergeAnnotations, createTranscriptStream } = await import('../js/transcript_stream.js');
const { LIVENESS, unitLiveness, hasActiveRunFor } = await import('../js/unit_liveness.js');
const { shortDate } = await import('../js/livestatus.js');

// ---------------------------------------------------------------------------
// A fake server that serves cursor deltas out of a growing turn list.
// ---------------------------------------------------------------------------

function fakeServer(opts) {
  const o = opts || {};
  const srv = {
    turns: [],            // the whole conversation, server-side
    annotations: [],
    complete: false,
    verbatim: !!o.verbatim,
    calls: [],            // every URL asked for, so we can assert the cursor
    // Append a brand-new turn.
    add(text, agent) {
      const i = srv.turns.length;
      srv.turns.push({ seq: i, role: 'agent', agent: agent || ('a' + i), kind: 'task_completed', text, source_index: i, turn_index: i });
      return srv;
    },
    // Grow the LAST turn in place (the llmCall merge case).
    growLast(more) {
      const t = srv.turns[srv.turns.length - 1];
      t.text += more;
      t.source_index = srv.turns.length + srv.calls.length + 100;  // strictly later
      return srv;
    },
    async fetchJson(url) {
      srv.calls.push(url);
      const m = /after=(\d+)/.exec(url);
      // No `after` means "from the top" and ALWAYS answers the whole
      // conversation — that contract is what makes the gap heal possible.
      const after = m ? Number(m[1]) : null;
      const delta = srv.turns
        .map((t, i) => ({ ...t, turn_index: i }))
        .filter((t) => after == null || t.source_index >= after);
      const cursor = srv.turns.reduce((max, t) => Math.max(max, t.source_index + 1), 0);
      return {
        found: true,
        cursor,
        turns: delta,
        annotations: srv.annotations.filter((a) => a.source_index >= after),
        turn_total: srv.turns.length,
        event_count: cursor,
        complete: srv.complete,
        truncated: false,
        fidelity: 'events',
        verbatim_available: srv.verbatim,
        events_path: '/ws/gen/entry/events.jsonl',
      };
    },
  };
  return srv;
}

// A hand-driven SSE subscription, so a test can fire growth frames itself.
function fakeBus() {
  const handlers = [];
  return {
    subscribe: (topic, fn) => { handlers.push(fn); return () => { const i = handlers.indexOf(fn); if (i >= 0) handlers.splice(i, 1); } },
    fire: (frame) => { for (const fn of [...handlers]) fn(frame); },
    get count() { return handlers.length; },
  };
}

function mount(srv, bus, spec) {
  const host = document.createElement('div');
  return mountConversationPane(host, {
    epochId: 'e1', gen: 'v3', entry: 'waffles', tri: LIVENESS.LIVE, ...(spec || {}),
  }, { fetchJson: srv.fetchJson, subscribe: bus.subscribe });
}

function scrollerOf(handle) {
  return handle.node.querySelector('[data-convo-scroll]');
}

function captionOf(handle) {
  return handle.node.querySelector('[data-convo-caption]').textContent;
}

function pinOf(handle) {
  return handle.node.querySelector('[data-convo-pin]');
}

// Give the scroller real scroll metrics; the bare harness DOM has none, and
// `nearBottom` treats a metric-less node as pinned (which is right for a real
// browser's first paint but useless for testing the scrolled-up branch).
function setScroll(scroller, { top, height, client }) {
  scroller.scrollHeight = height;
  scroller.clientHeight = client;
  scroller.scrollTop = top;
}

// ---------------------------------------------------------------------------
// TRAP 1 — append, never rebuild
// ---------------------------------------------------------------------------

test('the first pull paints every turn the run has produced so far', async () => {
  const srv = fakeServer().add('one').add('two');
  const h = mount(srv, fakeBus());
  await h.ready;

  assertEqual(scrollerOf(h).childNodes.length, 2);
});

test('growth APPENDS: the already-rendered turn nodes keep their identity', async () => {
  const srv = fakeServer().add('one').add('two');
  const bus = fakeBus();
  const h = mount(srv, bus);
  await h.ready;

  const scroller = scrollerOf(h);
  const before = [scroller.childNodes[0], scroller.childNodes[1]];

  srv.add('three');
  await h.refresh();

  assertEqual(scroller.childNodes.length, 3, 'the new turn landed');
  // THE ASSERTION THAT MATTERS: not "there are still two nodes before it" but
  // "they are the SAME two nodes". A rebuild would pass a count check and fail
  // this one.
  assert(scroller.childNodes[0] === before[0], 'turn 1 node was replaced');
  assert(scroller.childNodes[1] === before[1], 'turn 2 node was replaced');
  // And the whole pane was built by node construction, never by an innerHTML
  // write — the harness counts those, and one here would mean the thread was
  // serialized and re-parsed (losing node identity, scroll, and selection).
  assertEqual(h.node.innerHTMLWriteCount(), 0, 'the pane wrote innerHTML');
});

test('a beat that brings nothing writes ZERO dom', async () => {
  const srv = fakeServer().add('one');
  const bus = fakeBus();
  const h = mount(srv, bus);
  await h.ready;

  const scroller = scrollerOf(h);
  const before = scroller.childNodes[0];

  await h.refresh();
  await h.refresh();

  assertEqual(scroller.childNodes.length, 1);
  assert(scroller.childNodes[0] === before, 'a no-op pull rebuilt the turn');
});

test('the OPEN final turn growing re-renders only itself', async () => {
  const srv = fakeServer().add('one').add('thinking');
  const bus = fakeBus();
  const h = mount(srv, bus);
  await h.ready;

  const scroller = scrollerOf(h);
  const first = scroller.childNodes[0];

  srv.growLast(' … decided');
  await h.refresh();

  assertEqual(scroller.childNodes.length, 2, 'the grown turn did not duplicate');
  assert(scroller.childNodes[0] === first, 'the untouched prefix turn was rebuilt');
  assert(String(scroller.childNodes[1].textContent).indexOf('decided') >= 0, 'the growth is not rendered');
});

test('a LATE annotation on turn 1 re-decorates only turn 1', async () => {
  // CAUGHT IN THE BROWSER, not by the earlier tests. Drift detections and judge
  // verdicts anchor to the nearest PRECEDING turn, so a note arriving twenty
  // turns later re-decorates turn 1. Under a prefix-diff reconcile that read as
  // "the prefix diverged" and rebuilt the whole thread on nearly every beat —
  // silently undoing the append-only guarantee against real data.
  const srv = fakeServer().add('one').add('two').add('three');
  const h = mount(srv, fakeBus());
  await h.ready;

  const scroller = scrollerOf(h);
  const before = [...scroller.childNodes];

  srv.annotations.push({ anchor_seq: 0, kind: 'drift', summary: 'wandered off', source_index: 99 });
  await h.refresh();

  assertEqual(scroller.childNodes.length, 3, 'the thread changed length');
  // Turn 1 legitimately changed, so its node is new…
  assert(scroller.childNodes[0] !== before[0], 'the annotated turn was not re-rendered');
  assert(String(scroller.childNodes[0].textContent).indexOf('wandered off') >= 0,
    'the annotation is not rendered on its turn');
  // …but turns 2 and 3 did NOT change, so they must be the very same nodes.
  assert(scroller.childNodes[1] === before[1], 'an unrelated turn was rebuilt');
  assert(scroller.childNodes[2] === before[2], 'an unrelated turn was rebuilt');
});

test('the pane follows its OWN run: a sibling growth frame costs no fetch', async () => {
  const srv = fakeServer().add('one');
  const bus = fakeBus();
  const h = mount(srv, bus);
  await h.ready;
  const after = srv.calls.length;

  bus.fire({ events_path: '/ws/gen/OTHER/events.jsonl' });
  await Promise.resolve();

  assertEqual(srv.calls.length, after, 'a sibling run woke this pane up');

  bus.fire({ events_path: '/ws/gen/entry/events.jsonl' });
  await Promise.resolve(); await Promise.resolve();
  assert(srv.calls.length > after, 'our own run did NOT wake the pane');
});

// ---------------------------------------------------------------------------
// TRAP 2 — live → settled, in place
// ---------------------------------------------------------------------------

test('a run completing transitions the SAME pane, without a remount', async () => {
  const srv = fakeServer().add('one').add('two');
  const bus = fakeBus();
  const h = mount(srv, bus);
  await h.ready;

  const scroller = scrollerOf(h);
  const nodes = [scroller.childNodes[0], scroller.childNodes[1]];
  assert(captionOf(h).indexOf('following') >= 0, 'did not open in follow mode');

  // The run finishes: one more turn, and a terminal event.
  srv.add('done');
  srv.complete = true;
  await h.refresh();

  assertEqual(h.pane.tri, LIVENESS.SETTLED);
  // The scroller itself is the same node…
  assert(scrollerOf(h) === scroller, 'the scroller was remounted on settle');
  // …and so is every turn that was already in it.
  assert(scroller.childNodes[0] === nodes[0], 'settling rebuilt the thread');
  assert(scroller.childNodes[1] === nodes[1], 'settling rebuilt the thread');
  assertEqual(scroller.childNodes.length, 3, 'the final turn is missing');
  // Only the caption changed.
  assert(captionOf(h).indexOf('settled') >= 0, 'the caption still reads live');
});

test('settling stops the follow subscription', async () => {
  const srv = fakeServer().add('one');
  const bus = fakeBus();
  const h = mount(srv, bus);
  await h.ready;
  assertEqual(bus.count, 1, 'a live pane did not subscribe');

  srv.complete = true;
  await h.refresh();

  assertEqual(bus.count, 0, 'a settled pane is still following');
});

test('a corrected tri-state upgrades the pane IN PLACE, without a remount', async () => {
  // The board paints before the environment read lands, so a pane can mount
  // believing a plainly-running unit is interrupted. The correction must be a
  // state change, not a remount — a remount here would discard the cursor and
  // the turns already on screen.
  const srv = fakeServer().add('one');
  const bus = fakeBus();
  const h = mount(srv, bus, { tri: LIVENESS.INTERRUPTED });
  await h.ready;

  const scroller = scrollerOf(h);
  const first = scroller.childNodes[0];
  assertEqual(bus.count, 0, 'an interrupted pane subscribed to growth');

  h.setTriState(LIVENESS.LIVE);

  assert(scrollerOf(h) === scroller, 'the scroller was remounted');
  assert(scroller.childNodes[0] === first, 'the rendered turn was rebuilt');
  assertEqual(bus.count, 1, 'the corrected pane did not start following');
  assert(captionOf(h).indexOf('following') >= 0);
});

test('a settled or interrupted unit opens in the same component, not following', async () => {
  const srv = fakeServer().add('one');
  const bus = fakeBus();

  const settled = mount(srv, bus, { tri: LIVENESS.SETTLED });
  await settled.ready;
  assertEqual(bus.count, 0, 'a settled pane subscribed to growth');
  assert(captionOf(settled).indexOf('settled') >= 0);

  const stopped = mount(srv, fakeBus(), { tri: LIVENESS.INTERRUPTED });
  await stopped.ready;
  assert(captionOf(stopped).indexOf('interrupted') >= 0);
  // …and it still renders what the run DID produce — interrupted is not empty.
  assertEqual(scrollerOf(stopped).childNodes.length, 1);
});

// ---------------------------------------------------------------------------
// TRAP 3 — fidelity honesty
// ---------------------------------------------------------------------------

test('the caption names the tier that was actually rendered', async () => {
  const srv = fakeServer().add('one');
  const h = mount(srv, fakeBus());
  await h.ready;

  assert(captionOf(h).indexOf('reconstructed from events') >= 0,
    'the caption does not say what these bytes are');
});

test('the caption does not invent a verbatim capture', async () => {
  const without = mount(fakeServer().add('one'), fakeBus());
  await without.ready;
  assert(captionOf(without).indexOf('result.json') < 0, 'claimed a capture that is not there');

  const with_ = mount(fakeServer({ verbatim: true }).add('one'), fakeBus());
  await with_.ready;
  assert(captionOf(with_).indexOf('result.json') >= 0, 'hid a capture that IS there');
});

test('captionText is honest for each mode', () => {
  const stream = { fidelity: 'events', verbatimAvailable: false };
  assert(captionText(LIVENESS.LIVE, 3, stream).indexOf('following') >= 0);
  assert(captionText(LIVENESS.SETTLED, 3, stream).indexOf('settled') >= 0);
  // The interrupted caption must explain ITSELF — "interrupted" alone reads as
  // an error. Two facts the operator needs: this transcript is a fragment, and
  // the run's score was never committed (§1's vocabulary, so the pane and the
  // candidate dossier tell the same story about the same run).
  const stopped = captionText(LIVENESS.INTERRUPTED, 3, stream);
  assert(stopped.indexOf('still going when the loop was interrupted') >= 0, stopped);
  assert(stopped.indexOf('never committed') >= 0, stopped);
  assert(stopped.indexOf('3 turns before it stopped') >= 0, stopped);
  // …and it dates the stop the way the REST of the console dates it. Asserted
  // against §1's shortDate rather than a hardcoded 'Jun 8', because the point
  // is agreement: this pane and the candidate dossier must never name
  // different days for one interruption.
  const stoppedAt = '2026-06-08T03:58:49Z';
  const dated = captionText(LIVENESS.INTERRUPTED, 3, stream, stoppedAt);
  assert(dated.indexOf(shortDate(stoppedAt)) >= 0, dated);
});

// ---------------------------------------------------------------------------
// TRAP 4 — autoscroll with pin
// ---------------------------------------------------------------------------

test('a reader at the tail is carried along by new turns', async () => {
  const srv = fakeServer().add('one');
  const h = mount(srv, fakeBus());
  await h.ready;
  const scroller = scrollerOf(h);
  setScroll(scroller, { top: 800, height: 1000, client: 200 });   // at the tail

  srv.add('two');
  await h.refresh();

  assertEqual(scroller.scrollTop, 1000, 'a tailing reader was not carried along');
  assert(pinOf(h).getAttribute('hidden') != null, 'the pin badge showed to a tailing reader');
});

test('a reader who scrolled up is NOT moved, and is told how far behind', async () => {
  const srv = fakeServer().add('one');
  const h = mount(srv, fakeBus());
  await h.ready;
  const scroller = scrollerOf(h);
  setScroll(scroller, { top: 0, height: 1000, client: 200 });      // scrolled up

  srv.add('two');
  await h.refresh();
  assertEqual(scroller.scrollTop, 0, 'the reader was yanked to the bottom');
  assertEqual(pinOf(h).textContent, '1 new turn ↓');

  srv.add('three');
  await h.refresh();
  assertEqual(scroller.scrollTop, 0);
  assertEqual(pinOf(h).textContent, '2 new turns ↓', 'the backlog did not accumulate');
});

test('clicking the pin returns to the tail and clears the backlog', async () => {
  const srv = fakeServer().add('one');
  const h = mount(srv, fakeBus());
  await h.ready;
  const scroller = scrollerOf(h);
  setScroll(scroller, { top: 0, height: 1000, client: 200 });

  srv.add('two');
  await h.refresh();
  assert(pinOf(h).getAttribute('hidden') == null, 'the pin badge did not appear');

  pinOf(h).dispatchEvent({ type: 'click' });

  assertEqual(scroller.scrollTop, scroller.scrollHeight, 'the pin did not return to the tail');
  assertEqual(h.pane.unseen, 0);
  assert(pinOf(h).getAttribute('hidden') != null, 'the badge outlived its condition');
});

test('scrolling back to the tail clears the backlog without a click', async () => {
  const srv = fakeServer().add('one');
  const h = mount(srv, fakeBus());
  await h.ready;
  const scroller = scrollerOf(h);
  setScroll(scroller, { top: 0, height: 1000, client: 200 });

  srv.add('two');
  await h.refresh();
  assertEqual(h.pane.unseen, 1);

  scroller.scrollTop = 800;                       // the reader scrolls back down
  scroller.dispatchEvent({ type: 'scroll' });

  assertEqual(h.pane.unseen, 0);
  assert(pinOf(h).getAttribute('hidden') != null);
});

// ---------------------------------------------------------------------------
// The cursor protocol underneath
// ---------------------------------------------------------------------------

test('the pane hands the cursor back rather than re-reading from the top', async () => {
  const srv = fakeServer().add('one').add('two');
  const h = mount(srv, fakeBus());
  await h.ready;
  assert(srv.calls[0].indexOf('after=') < 0, 'the first read should have no cursor');

  srv.add('three');
  await h.refresh();
  assert(srv.calls[1].indexOf('after=2') >= 0, 'the second read did not carry the cursor: ' + srv.calls[1]);
});

test('spliceTurns lands a grown turn back on itself, not beside it', () => {
  const first = spliceTurns([], [{ turn_index: 0, text: 'a' }, { turn_index: 1, text: 'b' }]);
  assertEqual(first.turns.length, 2);
  assertEqual(first.gap, false);

  const grown = spliceTurns(first.turns, [{ turn_index: 1, text: 'b — and more' }]);
  assertEqual(grown.turns.length, 2, 'the grown turn was appended as a new one');
  assertEqual(grown.turns[1].text, 'b — and more');
});

test('spliceTurns reports a GAP rather than rendering holes', () => {
  const out = spliceTurns([{ text: 'a' }], [{ turn_index: 5, text: 'f' }]);
  assertEqual(out.gap, true, 'a delta starting past the end was spliced silently');
});

test('a gap makes the pane re-read from the top instead of rendering holes', async () => {
  const srv = fakeServer().add('one');
  const h = mount(srv, fakeBus());
  await h.ready;

  // The server jumps ahead: turns 2 and 3 exist, but only turn 3 is offered
  // (a delta the client cannot splice onto what it holds).
  srv.add('two').add('three');
  srv.turns[1].source_index = -5;            // turn 2 falls below the cursor
  await h.refresh();

  // Every turn is present and in order — no undefined node, no hole.
  assertEqual(scrollerOf(h).childNodes.length, 3);
  assert(String(scrollerOf(h).childNodes[1].textContent).indexOf('two') >= 0);
});

test('mergeAnnotations does not double a replayed note', () => {
  const a = [{ source_index: 4, kind: 'drift', summary: 'x' }];
  const merged = mergeAnnotations(a, [{ source_index: 4, kind: 'drift', summary: 'x' }]);
  assertEqual(merged.length, 1);
  assertEqual(mergeAnnotations(merged, [{ source_index: 9, kind: 'judge', summary: 'y' }]).length, 2);
});

test('a transient fetch failure is survivable, not fatal', async () => {
  const boom = { fetchJson: async () => { throw new Error('offline'); } };
  const host = document.createElement('div');
  const h = mountConversationPane(host, { epochId: 'e', gen: 'g', entry: 'n', tri: LIVENESS.LIVE },
    { fetchJson: boom.fetchJson, subscribe: fakeBus().subscribe });
  await h.ready;

  assertEqual(scrollerOf(h).childNodes.length, 0);
  // The pane is still there and still following — the next frame retries.
  assert(h.node != null);
});

test('the stream only ever ADVANCES its cursor', async () => {
  let body = { found: true, cursor: 10, turns: [], annotations: [], turn_total: 0, complete: false };
  const stream = createTranscriptStream({ epochId: 'e', gen: 'g', entry: 'n' },
    { fetchJson: async () => body });
  await stream.pull();
  assertEqual(stream.cursor, 10);

  // A stale response arriving late must not rewind us into re-delivery.
  body = { ...body, cursor: 4 };
  await stream.pull();
  assertEqual(stream.cursor, 10, 'the cursor went backwards');
});

// ---------------------------------------------------------------------------
// The unit verdict the pane keys on
// ---------------------------------------------------------------------------
//
// §1 derives liveness per WORKSPACE — one evolve loop holds the lock, so it
// answers "is the loop running". The pane needs "is THIS unit running", which
// is a composition of that verdict with whether the unit has an active-run
// record. These pin the composition; §1 owns the loop verdict itself.

test('a unit with no active-run record is settled, whatever the loop is doing', () => {
  assertEqual(unitLiveness({ liveness: { live: true }, hasActiveRun: false }), LIVENESS.SETTLED);
  assertEqual(unitLiveness({ liveness: { live: false }, hasActiveRun: false }), LIVENESS.SETTLED);
});

test('a unit is live only when the LOOP is live and the unit has a record', () => {
  assertEqual(unitLiveness({ liveness: { live: true }, hasActiveRun: true }), LIVENESS.LIVE);
});

test('a record against a dead loop reads interrupted, NEVER live', () => {
  // Issue #194 §1's headline bug in one assertion: the June-dead workspace
  // rendered stale active-run records as units in flight. That unit was
  // mid-run when the loop died — its score was never committed.
  assertEqual(unitLiveness({ liveness: { live: false, state: 'interrupted' }, hasActiveRun: true }),
    LIVENESS.INTERRUPTED);
});

test('the record lookup is exact — a sibling unit does not make this one live', () => {
  const runs = [{ generation_id: 'v3', entry_id: 'other' }, { generation_id: 'v9', entry_id: 'waffles' }];
  assertEqual(hasActiveRunFor(runs, 'v3', 'waffles'), false);
  assertEqual(hasActiveRunFor(runs, 'v3', 'other'), true);
  assertEqual(hasActiveRunFor(null, 'v3', 'waffles'), false);
});



await run();
