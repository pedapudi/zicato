// test/gate_absolute_scalars.test.mjs — ABSOLUTE SCALARS in the gate head.
//
// The gate head used to read only the Δ chips (Δ scalar / Δ pass rate) — the
// GAP between the two endpoints without the endpoints themselves. This surfaces
// the absolute champion_scalar / challenger_scalar as paired dn-stat chips LEFT
// of the Δ chips; while THIS pair's boards are still streaming in, the
// challenger endpoint reads its LIVE PROJECTED scalar in the projStat treatment
// (proj badge + boards_done/total bar) — visibly not a settled endpoint.
//
// Pins:
//   * absoluteScalars(gate) builds the champion-scalar + challenger-scalar chips
//     (settled → plain dn-stat; live mid-flight → projStat with the board bar);
//   * absent / unresolved (champion_scalar/challenger_scalar = null, no live)
//     → null → the gate head is byte-identical to today (back-compat);
//   * the abs block sits LEFT of the Δ chips in gatePanel's head;
//   * absoluteScalarsDigest carries NO timestamp + rounds floats, so a no-op
//     heartbeat is byte-identical while a board landing / a settle flips it
//     (the render-discipline bug class).

import { installDom, test, run, assert, assertEqual } from './harness.mjs';

installDom();

const candidate = await import('../js/views/candidate.js');

function classOf(node) { return (node && node.getAttribute && node.getAttribute('class')) || ''; }
function hasClass(node, cls) { return classOf(node).split(/\s+/).includes(cls); }
function allByClass(host, cls) {
  return host.querySelectorAll('[class]').filter((n) => hasClass(n, cls));
}
function mountInto(node) { const h = document.createElement('div'); if (node) h.appendChild(node); return h; }

// ── 1. absoluteScalars: settled endpoints render as paired dn-stat chips ──────
test('absoluteScalars: settled champion + challenger scalars render as two dn-stat chips with labels', () => {
  const block = candidate.absoluteScalars({ champion_scalar: 47.58, challenger_scalar: 57.70 });
  assert(block, 'a block is built when both endpoints resolve');
  assert(hasClass(block, 'dn-gate-absolutes'), 'wraps in the dn-gate-absolutes row (reuses dn-row)');
  const host = mountInto(block);
  const stats = allByClass(host, 'dn-stat');
  assertEqual(stats.length, 2, 'two endpoint chips (champion + challenger)');
  assert(host.textContent.includes('47.58') && host.textContent.includes('champion scalar'), 'champion endpoint reads its absolute scalar 2dp');
  assert(host.textContent.includes('57.70') && host.textContent.includes('challenger scalar'), 'challenger endpoint reads its absolute scalar 2dp');
  // a settled challenger is a PLAIN chip — NOT the projected treatment.
  assertEqual(allByClass(host, 'dt-proj').length, 0, 'a settled endpoint is not in the projStat (projected) treatment');
});

// ── 2. back-compat: a side / both sides unresolved drops the chip / block ─────
test('absoluteScalars: only the champion resolved → just the champion chip (challenger absent)', () => {
  const block = candidate.absoluteScalars({ champion_scalar: 47.58, challenger_scalar: null });
  assert(block, 'still a block (champion endpoint resolved)');
  const host = mountInto(block);
  assertEqual(allByClass(host, 'dn-stat').length, 1, 'exactly the champion chip');
  assert(host.textContent.includes('champion scalar') && !host.textContent.includes('challenger scalar'), 'no challenger chip when its scalar is null');
});

test('absoluteScalars: NEITHER side resolves (+ no live) → null (byte-identical to today)', () => {
  assertEqual(candidate.absoluteScalars({ champion_scalar: null, challenger_scalar: null }), null, 'both null → null block');
  assertEqual(candidate.absoluteScalars({}), null, 'empty gate (pre-feature) → null block');
  assertEqual(candidate.absoluteScalars(null), null, 'no gate → null');
});

// ── 3. live mid-flight: the challenger endpoint reads the PROJECTED scalar ────
test('absoluteScalars: a live (mid-flight) pair reads the challenger endpoint in the projStat treatment + board bar', () => {
  const block = candidate.absoluteScalars({
    champion_scalar: 47.58,
    challenger_scalar: null,            // not settled yet
    live: { challenger_scalar: 55.21, boards_done: 6, boards_total: 10 },
  });
  const host = mountInto(block);
  // champion is a settled plain chip; challenger is the projected one.
  assert(allByClass(host, 'dt-proj').length === 1, 'exactly the challenger endpoint is projected (projStat)');
  assert(host.textContent.includes('55.21') && host.textContent.includes('challenger scalar'), 'the projected challenger scalar reads (2dp)');
  assert(allByClass(host, 'dt-proj-badge').length === 1, 'carries the "proj" badge (in-flight, not settled)');
  assert(host.textContent.includes('6/10'), 'the projStat board-progress bar reads boards_done/total');
  // champion is still the settled plain chip beside it.
  assert(host.textContent.includes('47.58') && host.textContent.includes('champion scalar'), 'the champion endpoint stays the settled floor');
});

test('absoluteScalars: live overrides the settled challenger_scalar while mid-flight (prefers the projection)', () => {
  const block = candidate.absoluteScalars({
    champion_scalar: 47.58,
    challenger_scalar: 57.70,           // a stale settled value
    live: { challenger_scalar: 55.21, boards_done: 3, boards_total: 10 },
  });
  const host = mountInto(block);
  assertEqual(allByClass(host, 'dt-proj').length, 1, 'the live projection is preferred over the settled challenger value');
  assert(host.textContent.includes('55.21') && !host.textContent.includes('57.70'), 'reads the LIVE projected scalar, not the stale settled one');
});

// ── 4. placement: the abs block sits LEFT of the Δ chips in the gate head ─────
test('gatePanel: the absolute endpoints render LEFT of the Δ chips in the gate head', () => {
  const host = mountInto(candidate.gatePanel({
    decision: 'promoted', champion_scalar: 47.58, challenger_scalar: 57.70,
    delta_scalar: 10.12, delta_pass_rate: 0.05, rules: [],
  }));
  const head = allByClass(host, 'dn-gate-head')[0];
  assert(head, 'the gate head rendered');
  // the absolutes row precedes the deltas row among the head's element children.
  const kids = head.childNodes.filter((n) => n.getAttribute);
  const absIdx = kids.findIndex((n) => hasClass(n, 'dn-gate-absolutes'));
  const deltaIdx = kids.findIndex((n) => hasClass(n, 'dn-gate-deltas'));
  assert(absIdx >= 0, 'the absolutes block is present in the head');
  assert(deltaIdx >= 0, 'the deltas block is present in the head');
  assert(absIdx < deltaIdx, 'the absolute endpoints render LEFT of (before) the Δ chips');
  // both endpoints + both deltas read.
  assert(host.textContent.includes('47.58') && host.textContent.includes('57.70'), 'endpoints read');
  assert(host.textContent.includes('+10.12'), 'the Δ scalar still reads beside the endpoints');
});

test('gatePanel: a pre-feature gate (no champion/challenger scalar, no live) renders NO absolutes block', () => {
  const host = mountInto(candidate.gatePanel({
    decision: 'promoted', delta_scalar: 10.12, delta_pass_rate: 0.05, rules: [],
  }));
  assertEqual(allByClass(host, 'dn-gate-absolutes').length, 0, 'no endpoints → no absolutes block (byte-identical to today)');
  // the Δ chips still render — the head degrades to exactly the pre-feature shape.
  assert(allByClass(host, 'dn-gate-deltas').length === 1 && host.textContent.includes('+10.12'), 'the Δ chips still render unchanged');
});

// ── 5. digest: no-op-beat stability + board-landing / settle repaint ─────────
test('absoluteScalarsDigest: null when nothing resolves (contributes nothing to the gate digest)', () => {
  assertEqual(candidate.absoluteScalarsDigest({}), null, 'empty → null (back-compat digest)');
  assertEqual(candidate.absoluteScalarsDigest({ champion_scalar: null, challenger_scalar: null }), null, 'both null → null');
  assertEqual(candidate.absoluteScalarsDigest(null), null, 'no gate → null');
});

test('absoluteScalarsDigest: a no-op beat is byte-identical (no timestamp, rounded floats)', () => {
  // the same endpoints + the same live projection on two consecutive beats: the
  // floats differ only past 2dp (sub-threshold jitter) → digest must be EQUAL.
  const a = candidate.absoluteScalarsDigest({
    champion_scalar: 47.5831, challenger_scalar: null,
    live: { challenger_scalar: 55.2102, boards_done: 6, boards_total: 10 },
  });
  const b = candidate.absoluteScalarsDigest({
    champion_scalar: 47.5839, challenger_scalar: null,
    live: { challenger_scalar: 55.2148, boards_done: 6, boards_total: 10 },
  });
  assertEqual(JSON.stringify(a), JSON.stringify(b), 'sub-2dp jitter on a no-op beat → IDENTICAL digest (no flash)');
});

test('absoluteScalarsDigest: a board LANDING (boards_done grows) flips the digest (repaints)', () => {
  const a = candidate.absoluteScalarsDigest({
    champion_scalar: 47.58, challenger_scalar: null,
    live: { challenger_scalar: 55.21, boards_done: 6, boards_total: 10 },
  });
  const b = candidate.absoluteScalarsDigest({
    champion_scalar: 47.58, challenger_scalar: null,
    live: { challenger_scalar: 55.40, boards_done: 7, boards_total: 10 },
  });
  assert(JSON.stringify(a) !== JSON.stringify(b), 'a 7th board landing (boards_done 6→7) flips the digest');
});

test('absoluteScalarsDigest: a SETTLE (live drops, challenger_scalar resolves) flips the digest', () => {
  const live = candidate.absoluteScalarsDigest({
    champion_scalar: 47.58, challenger_scalar: null,
    live: { challenger_scalar: 55.40, boards_done: 10, boards_total: 10 },
  });
  const settled = candidate.absoluteScalarsDigest({
    champion_scalar: 47.58, challenger_scalar: 57.70, live: null,
  });
  assert(JSON.stringify(live) !== JSON.stringify(settled), 'the live→settled transition flips the digest (repaints the head)');
  // and a settled state is stable beat-over-beat.
  const settled2 = candidate.absoluteScalarsDigest({
    champion_scalar: 47.58, challenger_scalar: 57.70, live: null,
  });
  assertEqual(JSON.stringify(settled), JSON.stringify(settled2), 'two settled beats are byte-identical');
});

test('absoluteScalarsDigest: an endpoint APPEARING (absent → present) flips the digest', () => {
  const none = candidate.absoluteScalarsDigest({});
  const some = candidate.absoluteScalarsDigest({ champion_scalar: 47.58, challenger_scalar: null });
  assert(JSON.stringify(none) !== JSON.stringify(some), 'the champion endpoint resolving (null block → present) flips the digest');
});

// ── 6. end-to-end through gatePanel: a no-op beat churns ZERO DOM ────────────
test('gatePanel: re-rendering the SAME gate twice (a no-op beat) the head still carries the same endpoints', () => {
  const gate = {
    decision: 'deferred', champion_scalar: 47.58, challenger_scalar: null,
    live: { challenger_scalar: 55.21, boards_done: 6, boards_total: 10 },
    delta_scalar: 7.63, delta_pass_rate: null, rules: [],
  };
  // (gatePanel builds a fresh node each call — the no-op-skip is enforced at the
  //  view level by candidateDigest/gatedSwap; here we pin that the DIGEST the
  //  view folds in is stable, which is the gate that prevents the rebuild.)
  const host1 = mountInto(candidate.gatePanel(gate));
  const host2 = mountInto(candidate.gatePanel(gate));
  assertEqual(host1.textContent.replace(/\s+/g, ' '), host2.textContent.replace(/\s+/g, ' '), 'two builds of the same gate read identically');
  assertEqual(allByClass(host1, 'dt-proj-badge').length, allByClass(host2, 'dt-proj-badge').length, 'the projected challenger endpoint is stable across beats');
});

await run();
