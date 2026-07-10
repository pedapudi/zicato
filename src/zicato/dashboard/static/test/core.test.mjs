// test/core.test.mjs — the core-module foundation tests.
//
// These pin the element builders + patch helpers (core/dom.js), the
// event bus, and the harmonograf session-id resolution:
//   * patchText writes only on a real change (the no-repaint helper the
//     long-lived chrome nodes use — the digest no-op discipline itself
//     lives in gatedSwap, pinned by the view suites).

import { installDom, test, run, assert, assertEqual, makeEvent } from './harness.mjs';

installDom();

const dom = await import('../js/core/dom.js');
const { bus } = await import('../js/core/bus.js');

// --- patchText writes only on change ---------------------------------

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
