// test/core.test.mjs — the render-spine foundation tests.
//
// These prove the structural flashing fix and the matchup-click fix:
//   * reconcileList updates rows in place — node identity survives.
//   * a surviving row keeps its event listener (click still fires).
//   * appendRows is strictly additive — no flash on the log tail.
//   * mount is idempotent — a panel node is built once.
//   * patchText / patchAttr write only on a real change.

import { installDom, test, run, assert, assertEqual, makeEvent } from './harness.mjs';

installDom();

const dom = await import('../js/core/dom.js');
const { bus } = await import('../js/core/bus.js');
const fmt = await import('../js/core/format.js');

// --- format helpers --------------------------------------------------

test('fmtDuration formats h:m:s and m:s', () => {
  assertEqual(fmt.fmtDuration(65), '01:05');
  assertEqual(fmt.fmtDuration(3725), '01:02:05');
  assertEqual(fmt.fmtDuration(-1), '—');
});

test('parseIso pins zone-less values to UTC', () => {
  const withZ = fmt.parseIso('2026-05-18T12:00:00Z');
  const without = fmt.parseIso('2026-05-18T12:00:00');
  assertEqual(withZ, without, 'zone-less must parse as UTC');
  const withOffset = fmt.parseIso('2026-05-18T12:00:00+00:00');
  assertEqual(withZ, withOffset);
});

test('fmtDelta signs the value', () => {
  assertEqual(fmt.fmtDelta(0.5), '+0.500');
  assertEqual(fmt.fmtDelta(-0.25), '-0.250');
  assertEqual(fmt.fmtDelta(NaN), '—');
});

// --- mount idempotence ----------------------------------------------

test('mount builds a keyed node once and reuses it', () => {
  const host = document.createElement('div');
  let builds = 0;
  const build = () => { builds += 1; return document.createElement('section'); };
  const a = dom.mount(host, 'panel', build);
  const b = dom.mount(host, 'panel', build);
  assertEqual(builds, 1, 'builder must run only once');
  assert(a === b, 'mount must return the same node');
  assertEqual(host.children.length, 1);
});

// --- patchText / patchAttr write only on change ---------------------

test('patchText writes only when the text differs', () => {
  const n = document.createElement('span');
  dom.patchText(n, 'hello');
  assertEqual(n.textContent, 'hello');
  // Mark the existing text node; a no-op patch must not replace it.
  const before = n.firstChild;
  dom.patchText(n, 'hello');
  assert(n.firstChild === before, 'unchanged patchText must not rebuild');
  dom.patchText(n, 'world');
  assertEqual(n.textContent, 'world');
});

test('patchAttr removes the attribute on a null value', () => {
  const n = document.createElement('div');
  dom.patchAttr(n, 'aria-hidden', 'true');
  assertEqual(n.getAttribute('aria-hidden'), 'true');
  dom.patchAttr(n, 'aria-hidden', null);
  assert(!n.hasAttribute('aria-hidden'), 'null value must remove the attr');
});

// --- reconcileList: node identity + listener survival ---------------

test('reconcileList keeps surviving rows AND their click listeners', () => {
  const host = document.createElement('ul');
  let clicks = 0;
  const build = (item) => {
    const li = document.createElement('li');
    li.setAttribute('data-id', item.id);
    li.addEventListener('click', () => { clicks += 1; });
    return li;
  };
  const update = (row, item) => { row.setAttribute('data-label', item.label); };

  // First render: three rows.
  let items = [
    { id: 'a', label: 'A1' }, { id: 'b', label: 'B1' }, { id: 'c', label: 'C1' },
  ];
  dom.reconcileList(host, items, (i) => i.id, build, update);
  const rowB = host.children[1];
  assertEqual(rowB.getAttribute('data-id'), 'b');

  // Click row B — the listener fires.
  rowB.dispatchEvent(makeEvent('click'));
  assertEqual(clicks, 1);

  // Second render: B's label changed, a new row d appended, c removed.
  items = [
    { id: 'a', label: 'A2' }, { id: 'b', label: 'B2' }, { id: 'd', label: 'D1' },
  ];
  dom.reconcileList(host, items, (i) => i.id, build, update);

  // Row B is the SAME node — identity survived the delta.
  assert(host.children[1] === rowB, 'matchup-click fix: row node identity must survive');
  assertEqual(rowB.getAttribute('data-label'), 'B2', 'updateFn must patch in place');

  // The surviving row's click listener still fires — this is the
  // matchup-click fix: a delta does not detach handlers.
  rowB.dispatchEvent(makeEvent('click'));
  assertEqual(clicks, 2, 'surviving row must keep its listener');

  // The list reconciled — c gone, d present, no clear-and-rebuild.
  assertEqual(host.children.length, 3);
  assertEqual(host.children[2].getAttribute('data-id'), 'd');
});

test('reconcileList never writes innerHTML (no flash)', () => {
  const host = document.createElement('ul');
  const build = (i) => { const li = document.createElement('li'); li.setAttribute('data-x', i.id); return li; };
  dom.reconcileList(host, [{ id: '1' }, { id: '2' }], (i) => i.id, build, () => {});
  dom.reconcileList(host, [{ id: '1' }, { id: '2' }, { id: '3' }], (i) => i.id, build, () => {});
  assertEqual(host.innerHTMLWriteCount(), 0, 'reconcile must never touch innerHTML');
});

// --- appendRows: strictly additive (log tail no-flash) --------------

test('appendRows only appends genuinely-new keys', () => {
  const host = document.createElement('div');
  const build = (ev) => {
    const row = document.createElement('div');
    row.textContent = ev.summary;
    return row;
  };
  let n = dom.appendRows(host, [{ seq: 1, summary: 'one' }, { seq: 2, summary: 'two' }],
    (e) => e.seq, build);
  assertEqual(n, 2);
  const firstRow = host.children[0];

  // Re-feed the same events plus a new one: only the new row is added,
  // the existing rows are NOT rebuilt — the log tail does not flash.
  n = dom.appendRows(host, [
    { seq: 1, summary: 'one' }, { seq: 2, summary: 'two' }, { seq: 3, summary: 'three' },
  ], (e) => e.seq, build);
  assertEqual(n, 1, 'only the new event appends');
  assert(host.children[0] === firstRow, 'existing log rows must not be rebuilt');
  assertEqual(host.children.length, 3);
});

test('trimRows bounds the list oldest-first', () => {
  const host = document.createElement('div');
  for (let i = 0; i < 10; i++) {
    const r = document.createElement('div');
    r.setAttribute('data-key', String(i));
    host.appendChild(r);
  }
  dom.trimRows(host, 4);
  assertEqual(host.children.length, 4);
  assertEqual(host.children[0].getAttribute('data-key'), '6', 'oldest rows trimmed first');
});

// --- bus -------------------------------------------------------------

test('bus delivers to every subscriber and supports off()', () => {
  bus._reset();
  let a = 0;
  let b = 0;
  const offA = bus.on('topic', () => { a += 1; });
  bus.on('topic', () => { b += 1; });
  bus.emit('topic');
  assertEqual(a, 1);
  assertEqual(b, 1);
  offA();
  bus.emit('topic');
  assertEqual(a, 1, 'unsubscribed handler must not fire');
  assertEqual(b, 2);
});

// --- harmonografSessionId: ADK session id resolution -----------------
// harmonograf keys session views by the ADK session id.
// Resolution order: adk_session_id first, then legacy aliases, then null.

const { harmonografSessionId, deriveRunId } = await import('../js/core/harmonograf.js');

test('harmonografSessionId: prefers adk_session_id over all other fields', () => {
  const rec = {
    adk_session_id: 'real-adk-id',
    session_id: 'legacy-id',
    harmonograf_session: 'hg-id',
  };
  assertEqual(harmonografSessionId(rec), 'real-adk-id');
});

test('harmonografSessionId: accepts child_adk_session_id', () => {
  const rec = { child_adk_session_id: 'child-adk-abc' };
  assertEqual(harmonografSessionId(rec), 'child-adk-abc');
});

test('harmonografSessionId: accepts parent_adk_session_id', () => {
  const rec = { parent_adk_session_id: 'parent-adk-xyz' };
  assertEqual(harmonografSessionId(rec), 'parent-adk-xyz');
});

test('harmonografSessionId: falls back to session_id when no adk field', () => {
  const rec = { session_id: 'legacy-session' };
  assertEqual(harmonografSessionId(rec), 'legacy-session');
});

test('harmonografSessionId: falls back to harmonograf_session legacy alias', () => {
  const rec = { harmonograf_session: 'hg-session-legacy' };
  assertEqual(harmonografSessionId(rec), 'hg-session-legacy');
});

test('harmonografSessionId: returns null for empty record', () => {
  assertEqual(harmonografSessionId({}), null);
  assertEqual(harmonografSessionId(null), null);
});

test('harmonografSessionId: does NOT fall back to synthetic run-id', () => {
  // The old broken behaviour was to call deriveRunId and use the
  // "{generation}--{entry}" string. That is wrong: harmonograf does not
  // accept synthetic run-ids. Verify the new code does not do this.
  const rec = { generation_id: 'v0', entry_id: 'waffles_single' };
  // This record has no ADK session id and no legacy aliases.
  const sid = harmonografSessionId(rec);
  // Must be null, never "v0--waffles_single".
  assertEqual(sid, null);
  const synth = deriveRunId(rec);
  assertEqual(synth, 'v0--waffles_single', 'deriveRunId still works for callers that need it');
  assert(sid !== synth, 'harmonografSessionId must not fall back to the synthetic run-id');
});

await run();
