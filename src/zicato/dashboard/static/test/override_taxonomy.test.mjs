// test/override_taxonomy.test.mjs — the UNIFIED DECISION-STATE TAXONOMY +
// overrideChip primitive (the cockpit foundation BT/b4 + field-override/b5
// consume).
//
// Pins:
//   * verdictPill renders the dormant 'deferred' state end-to-end (the
//     dn-pill.dn-deferred → --v2-caution chain) and normaliseDecision threads a
//     real gate decision='deferred' through;
//   * overrideChip(prov) is a SIBLING to verdictPill that layers operator-
//     override provenance (forced↑ / forced✕ / queued / drained) BESIDE the
//     verdict WITHOUT recoloring it; absent / present:false → null (byte-
//     identical to today);
//   * overrideDigest carries NO timestamp, so an override appearing/changing
//     repaints but a no-op heartbeat is byte-identical (the render-discipline
//     bug class).
//   * end-to-end through the standings table + structureDigest: an override
//     rides beside the status pill, folds into the digest, and a no-op beat
//     churns ZERO DOM while a real override flip repaints.

import { installDom, test, run, assert, assertEqual } from './harness.mjs';

installDom();

const ui = await import('../js/ui.js');
const STRUCT = await import('../js/views/structure.js');
const router = await import('../js/router.js');

function classOf(node) { return (node && node.getAttribute && node.getAttribute('class')) || ''; }
function hasClass(node, cls) { return classOf(node).split(/\s+/).includes(cls); }
function allByClass(host, cls) {
  return host.querySelectorAll('[class]').filter((n) => hasClass(n, cls));
}

// ── 1. verdictPill: the dormant 'deferred' state renders end-to-end ──────────
test('verdictPill: deferred → dn-pill.dn-deferred with a "deferred" label (the caution chain)', () => {
  const pill = ui.verdictPill('deferred');
  assert(hasClass(pill, 'dn-pill'), 'carries the dn-pill base class');
  assert(hasClass(pill, 'dn-deferred'), 'carries the dn-deferred state class (→ --v2-caution in every theme)');
  assertEqual(pill.textContent, 'deferred', 'reads "deferred" (not "racing…"/"seed (v0)")');
});

test('verdictPill: the five-state vocabulary each map to their own class', () => {
  const map = {
    promoted: 'dn-promoted', rejected: 'dn-rejected', deferred: 'dn-deferred',
    baseline: 'dn-baseline', pending: 'dn-pending',
  };
  for (const [decision, cls] of Object.entries(map)) {
    assert(hasClass(ui.verdictPill(decision), cls), decision + ' → ' + cls);
  }
});

// ── 2. decisionOf reads the SERVER-STAMPED decision token VERBATIM ───────────
// The substring classifier (normaliseDecision) is DELETED: the server stamps
// the canonical token and the client never re-classifies.
test('decisionOf: a stamped decision:"deferred" reads back verbatim (not pending/rejected)', () => {
  assertEqual(ui.decisionOf({ decision: 'deferred' }), 'deferred', 'literal deferred threads through');
  assertEqual(ui.decisionOf({ decision: 'promoted' }), 'promoted', 'promoted threads through');
  assertEqual(ui.decisionOf({ decision: 'rejected' }), 'rejected', 'rejected threads through');
  // the gate panel resolves an unresolved gate to 'pending', NOT 'rejected'
  // (Class-B), but a REAL deferred is its own state.
  assertEqual(ui.decisionOf({}), null, 'no decision → null (caller defaults to pending)');
  // the deleted re-derivation: a nested outcome is NEVER classified client-side.
  assertEqual(ui.decisionOf({ outcome: { tournament_decision: 'promoted' } }), null,
    'a raw nested outcome is NOT re-classified — only the stamped token counts');
  assertEqual(ui.normaliseDecision, undefined, 'normaliseDecision (the substring classifier) is deleted');
});

// ── 3. overrideChip: each operator state + the back-compat absent path ───────
test('overrideChip: absent / present:false → null (byte-identical to today)', () => {
  assertEqual(ui.overrideChip(null), null, 'null → no chip');
  assertEqual(ui.overrideChip(undefined), null, 'undefined → no chip');
  assertEqual(ui.overrideChip({}), null, 'empty → no chip');
  assertEqual(ui.overrideChip({ present: false }), null, 'gate.override present:false → no chip (back-compat)');
  assertEqual(ui.overrideChip({ action: 'shrug' }), null, 'an unknown action → no chip');
});

test('overrideChip: a force-promote (gate.override shape) reads forced↑ + earns the GOOD direction', () => {
  const chip = ui.overrideChip({ present: true, action: 'promote', reason: 'operator call' });
  assert(chip, 'a chip is built');
  assert(hasClass(chip, 'dn-override'), 'carries the dn-override base');
  assert(hasClass(chip, 'dn-override-promote'), 'carries the promote (good-direction) class');
  assert(!hasClass(chip, 'dn-promoted') && !hasClass(chip, 'dn-pill'), 'is NOT a verdict pill — a SIBLING primitive');
  assertEqual(chip.getAttribute('data-override'), 'promote', 'data-override marks the kind');
  assert(/forced↑/.test(chip.textContent), 'reads "forced↑"');
  assert(/operator/.test(chip.textContent), 'attributes the override to the operator');
});

test('overrideChip: a force-reject reads forced✕ + earns the BAD direction', () => {
  const chip = ui.overrideChip({ present: true, action: 'reject', reason: 'spurious' });
  assert(hasClass(chip, 'dn-override-reject'), 'carries the reject (bad-direction) class');
  assert(/forced✕/.test(chip.textContent), 'reads "forced✕"');
});

test('overrideChip: a queued override (state:"queued") reads caution; a drained one reads faint', () => {
  const queued = ui.overrideChip({ action: 'promote', state: 'queued', reason: 'pending' });
  assert(hasClass(queued, 'dn-override-queued'), 'queued → caution-toned class');
  assert(/queued/.test(queued.textContent), 'reads "queued"');
  const drained = ui.overrideChip({ action: 'reject', state: 'drained' });
  assert(hasClass(drained, 'dn-override-drained'), 'drained → faint-toned class');
  assert(/drained/.test(drained.textContent), 'reads "drained"');
});

test('overrideChip: the structure override_status shape (state:"applied") resolves by action direction', () => {
  const promote = ui.overrideChip({ action: 'promote', ts: '2026-06-13T00:00:00Z', reason: 'r', state: 'applied' });
  assert(hasClass(promote, 'dn-override-promote'), 'applied promote → promote class');
  const reject = ui.overrideChip({ action: 'reject', ts: '2026-06-13T00:00:00Z', reason: 'r', state: 'applied' });
  assert(hasClass(reject, 'dn-override-reject'), 'applied reject → reject class');
});

// ── 4. overrideChip does NOT recolor the verdict it rides beside ─────────────
test('overrideChip: a force-promote chip does NOT touch a DEFERRED verdict pill it sits beside', () => {
  const verdict = ui.verdictPill('deferred');
  const chip = ui.overrideChip({ present: true, action: 'promote', reason: 'x' });
  // the verdict keeps its deferred (caution) class — the chip is the ONLY
  // carrier of the override colour, so the gate verdict is never overwritten.
  assert(hasClass(verdict, 'dn-deferred'), 'the verdict stays deferred');
  assert(!hasClass(verdict, 'dn-promoted'), 'the verdict is NOT recolored to promoted by the override');
  assert(hasClass(chip, 'dn-override-promote'), 'the override carries its own direction independently');
});

// ── 5. overrideDigest: timestamp-free stability ──────────────────────────────
test('overrideDigest: null when absent; carries kind+action+state+reason but NO timestamp', () => {
  assertEqual(ui.overrideDigest(null), null, 'absent → null (back-compat digest)');
  assertEqual(ui.overrideDigest({ present: false }), null, 'present:false → null');
  const a = ui.overrideDigest({ action: 'promote', state: 'applied', reason: 'r', ts: '2026-06-13T00:00:01Z' });
  const b = ui.overrideDigest({ action: 'promote', state: 'applied', reason: 'r', ts: '2026-06-13T09:59:59Z' });
  assertEqual(JSON.stringify(a), JSON.stringify(b), 'two beats of the SAME override, different ts → IDENTICAL digest (no ts leak)');
  const c = ui.overrideDigest({ action: 'reject', state: 'applied', reason: 'r', ts: '2026-06-13T00:00:01Z' });
  assert(JSON.stringify(a) !== JSON.stringify(c), 'a CHANGED action flips the digest');
});

// ── 6. end-to-end: standings table + structureDigest no-op-beat / repaint ────
function structWithOverride(overrideStatus) {
  return {
    structure: 'double_elim', live: false, source: 'index',
    competitors: [{ generation_id: 'v5', seed: 1 }, { generation_id: 'v6', seed: 2 }],
    rounds: [],
    standings: [
      { generation_id: 'v5', rank: 1, scalar: 47.5, wins: 2, losses: 0, status: 'champion' },
      { generation_id: 'v6', rank: 2, scalar: 52.1, wins: 0, losses: 2, status: 'eliminated' },
    ],
    field_status: [],
    override_status: overrideStatus || undefined,
  };
}

test('standings: an override_status rides BESIDE the status pill (the chip + the pill coexist)', () => {
  const st = structWithOverride({
    v5: { action: 'promote', ts: '2026-06-13T00:00:00Z', reason: 'operator promoted v5 over the gate', state: 'applied' },
  });
  const ctx = { navigate() {}, href: router.href };
  const nodes = STRUCT.renderStructure(st, ctx, 'e0');
  const host = document.createElement('div');
  for (const n of nodes) host.appendChild(n);
  const chips = allByClass(host, 'dn-override');
  assertEqual(chips.length, 1, 'exactly the one overridden row carries a chip');
  assert(hasClass(chips[0], 'dn-override-promote'), 'the v5 override reads as a force-promote');
  // the pill is still there beside it — the override never replaced the verdict.
  assert(allByClass(host, 'dn-pill').length >= 2, 'every standing keeps its status pill (the override is additive)');
});

test('standings: NO override_status → ZERO chips (byte-identical to the pre-feature path)', () => {
  const st = structWithOverride(undefined);
  const ctx = { navigate() {}, href: router.href };
  const nodes = STRUCT.renderStructure(st, ctx, 'e0');
  const host = document.createElement('div');
  for (const n of nodes) host.appendChild(n);
  assertEqual(allByClass(host, 'dn-override').length, 0, 'no override → no chip');
});

test('structureDigest: a no-op beat over an overridden standing churns ZERO DOM; a flip repaints', () => {
  const ov = { v5: { action: 'promote', ts: '2026-06-13T00:00:00Z', reason: 'r', state: 'applied' } };
  // SAME override, a LATER heartbeat ts → the digest must be byte-identical
  // (the timestamp must NOT leak into the structural digest).
  const a = structWithOverride({ v5: { action: 'promote', ts: '2026-06-13T00:00:00Z', reason: 'r', state: 'applied' } });
  const b = structWithOverride({ v5: { action: 'promote', ts: '2026-06-13T11:11:11Z', reason: 'r', state: 'applied' } });
  assertEqual(STRUCT.structureDigest(a), STRUCT.structureDigest(b), 'two beats of the SAME override produce the SAME structureDigest');

  // node-identity + zero-write check across a gated no-op re-render.
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  ui.gatedSwap(host, STRUCT.structureDigest(a), () => STRUCT.renderStructure(a, ctx, 'e0'));
  const writesAfterFirst = host.innerHTMLWriteCount();
  const tableFirst = allByClass(host, 'dt-standings')[0];
  ui.gatedSwap(host, STRUCT.structureDigest(b), () => STRUCT.renderStructure(b, ctx, 'e0'));
  const tableSecond = allByClass(host, 'dt-standings')[0];
  assert(tableFirst === tableSecond, 'the standings table node identity is preserved across a no-op tick (digest-gated, zero rebuild)');
  assertEqual(host.innerHTMLWriteCount(), writesAfterFirst, 'a no-op beat writes ZERO additional DOM');

  // a REAL override change (promote → reject) MUST flip the digest + repaint.
  const c = structWithOverride({ v5: { action: 'reject', ts: '2026-06-13T00:00:00Z', reason: 'r', state: 'applied' } });
  assert(STRUCT.structureDigest(a) !== STRUCT.structureDigest(c), 'a promote→reject override flip changes the digest (repaints)');

  // an override APPEARING (absent → present) flips the digest too.
  const none = structWithOverride(undefined);
  assert(STRUCT.structureDigest(none) !== STRUCT.structureDigest(a), 'an override appearing changes the digest');
  // and an absent override is byte-stable beat-over-beat.
  assertEqual(STRUCT.structureDigest(none), STRUCT.structureDigest(structWithOverride(undefined)), 'two no-override beats are byte-identical');
  void ov;
});

await run();
