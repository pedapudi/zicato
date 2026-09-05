// test/field_override_controls.test.mjs — the FIELD-TOURNAMENT OVERRIDE CONTROL
// PLANE + provenance (b5). The override CHIP renders the FACT of an override
// (covered by override_taxonomy); THIS pins the CONTROL that creates one.
//
// Pins:
//   * overrideControlCell is CONFIRM-INLINE (arm → reason → POST), never one-
//     click; the bare cell shows only an "override" arm button.
//   * a confirmed POST stamps an OPTIMISTIC 'queued' override (markPending) that
//     survives the digest-gated re-render via the module pending registry, and
//     flows straight into overrideChip / overrideDigest like the durable readback.
//   * DISABLED (not POST-and-fail) when read_only — the button is present but
//     inert and never calls the POSTer.
//   * a SETTLED round / an already-overridden row takes no new override.
//   * pendingOverrideDigest carries NO timestamp — a queued override appearing
//     repaints, a no-op beat is byte-identical (the render-discipline bug class).
//   * end-to-end through standingsTable + structureDigest: the control column
//     renders for ALL structures, firing an override folds the queued stamp into
//     the digest, a no-op beat churns ZERO DOM, and a settled-but-undrained queue
//     reads DRAINED. MULTIPLE promoted ids (ties) + the provenance caption
//     ('gate said … · operator forced …') are pinned.

import { installDom, test, run, assert, assertEqual, makeEvent } from './harness.mjs';

installDom();

const ui = await import('../js/ui.js');
const STRUCT = { ...await import('../js/tournament_model.js'), ...await import('../js/views/structure.js') };
const router = await import('../js/router.js');
const { state } = await import('../js/core/state.js');

function classOf(node) { return (node && node.getAttribute && node.getAttribute('class')) || ''; }
function hasClass(node, cls) { return classOf(node).split(/\s+/).includes(cls); }
function allByClass(host, cls) {
  return host.querySelectorAll('[class]').filter((n) => hasClass(n, cls));
}
function byClass(host, cls) { return allByClass(host, cls)[0] || null; }
const flush = () => Promise.resolve().then(() => Promise.resolve());

// A clean slate before each cell test — the pending registry is module-global.
// A WRITABLE workspace is an explicit `read_only: false` — the same polarity
// the topbar controls use. A null/absent health payload (not yet fetched, or a
// server that omits the field) is NOT writable: a control affordance defaults
// off, never on. Tests that exercise the enabled path must say so.
function reset() { ui._resetPendingOverrides(); state.health = { read_only: false }; }

// ── 1. CONFIRM-INLINE: the cell ARMS before it can POST (never one-click) ─────
test('overrideControlCell: starts disarmed — one "override" arm button, no promote/reject yet', () => {
  reset();
  let posted = 0;
  const cell = ui.overrideControlCell({ gid: 'v7', onPost: () => { posted += 1; return Promise.resolve({ ok: true, status: 202 }); } });
  const arm = byClass(cell, 'dn-ovr-arm');
  assert(arm, 'a bare cell shows the arm button');
  assertEqual(allByClass(cell, 'dn-ovr-confirm').length, 0, 'no confirm buttons before arming — never one-click');
  assertEqual(posted, 0, 'nothing posted on build');
});

test('overrideControlCell: arming reveals the reason field + promote↑ / reject✕ + cancel', () => {
  reset();
  const cell = ui.overrideControlCell({ gid: 'v7', onPost: () => Promise.resolve({ ok: true, status: 202 }) });
  byClass(cell, 'dn-ovr-arm').dispatchEvent(makeEvent('click'));
  assertEqual(cell.getAttribute('data-armed'), '1', 'the cell is armed');
  assert(byClass(cell, 'dn-ovr-reason'), 'a reason field appears');
  assert(byClass(cell, 'dn-ovr-promote'), 'a promote↑ confirm appears');
  assert(byClass(cell, 'dn-ovr-reject'), 'a reject✕ confirm appears');
  const cancel = byClass(cell, 'dn-ovr-cancel');
  assert(cancel, 'a cancel control appears');
  cancel.dispatchEvent(makeEvent('click'));
  assertEqual(cell.getAttribute('data-armed'), '0', 'cancel disarms back to the bare button');
  assert(byClass(cell, 'dn-ovr-arm'), 'the disarmed cell is back to the arm button');
});

// ── 2. the confirmed POST stamps an OPTIMISTIC queued override ───────────────
test('overrideControlCell: confirm → POSTs with the structured body + stamps an optimistic queued override', async () => {
  reset();
  const posts = [];
  let changed = 0;
  const cell = ui.overrideControlCell({
    gid: 'v8', epochId: 'e2', tournamentId: 't9', structure: 'double_elim',
    onPost: (action, gid, reason) => { posts.push({ action, gid, reason }); return Promise.resolve({ ok: true, status: 202 }); },
    onChange: () => { changed += 1; },
  });
  byClass(cell, 'dn-ovr-arm').dispatchEvent(makeEvent('click'));
  byClass(cell, 'dn-ovr-reason').value = 'BT was too uncertain';
  byClass(cell, 'dn-ovr-promote').dispatchEvent(makeEvent('click'));
  await flush();
  assertEqual(posts.length, 1, 'exactly one POST fired on confirm');
  assertEqual(posts[0].action, 'promote', 'the promote↑ confirm posts a promote');
  assertEqual(posts[0].gid, 'v8', 'the challenger id rides the POST');
  assertEqual(posts[0].reason, 'BT was too uncertain', 'the typed reason rides the POST');
  assert(changed >= 1, 'onChange fired so the gated swap busts and the queued stamp repaints');
  // the optimistic stamp is now in the registry, shaped like a readback prov.
  const pend = ui.pendingOverride('v8');
  assert(pend && pend.action === 'promote' && pend.state === 'queued', 'an optimistic queued promote is stamped');
  assertEqual(pend.reason, 'BT was too uncertain', 'the reason is carried on the stamp');
  // it flows into the chip primitive as a queued (caution) override.
  const chip = ui.overrideChip(pend);
  assert(hasClass(chip, 'dn-override-queued'), 'the optimistic stamp renders as a queued (caution) chip');
});

// ── 3. DISABLED when read_only — present but inert (never POST-and-fail) ──────
test('overrideControlCell: read_only → a DISABLED arm button that never calls the POSTer', () => {
  reset();
  let posted = 0;
  const cell = ui.overrideControlCell({ gid: 'v9', readOnly: true, onPost: () => { posted += 1; return Promise.resolve({ ok: true }); } });
  const arm = byClass(cell, 'dn-ovr-arm');
  assert(arm, 'the control is still VISIBLE so the operator sees it exists');
  assertEqual(arm.getAttribute('disabled'), 'disabled', 'but it is DISABLED');
  assertEqual(allByClass(cell, 'dn-ovr-confirm').length, 0, 'a read-only cell never arms a confirm');
  arm.dispatchEvent(makeEvent('click'));
  assertEqual(posted, 0, 'a disabled control never POSTs (disabled, not POST-and-fail)');
});

// ── 4. a SETTLED / already-overridden row takes no new override ──────────────
test('overrideControlCell: settled → no arm control (the field has resolved)', () => {
  reset();
  const cell = ui.overrideControlCell({ gid: 'v9', settled: true, onPost: () => Promise.resolve({ ok: true }) });
  assertEqual(allByClass(cell, 'dn-ovr-arm').length, 0, 'a settled round shows no arm control');
  assert(byClass(cell, 'dn-ovr-na'), 'it reads as not-applicable');
});

test('overrideControlCell: an already-overridden row reads "overridden" (the control is spent)', () => {
  reset();
  const cell = ui.overrideControlCell({ gid: 'v9', existingOverride: { action: 'promote', state: 'applied', reason: 'r' }, onPost: () => Promise.resolve({ ok: true }) });
  assert(byClass(cell, 'dn-ovr-spent'), 'a durably-overridden row shows the spent state');
  assertEqual(allByClass(cell, 'dn-ovr-arm').length, 0, 'no re-arm on a spent cell');
});

test('overrideControlCell: a 403 (workspace flipped read-only) flags the rejection, no optimistic stamp', async () => {
  reset();
  const cell = ui.overrideControlCell({ gid: 'v9', onPost: () => Promise.resolve({ ok: false, status: 403 }) });
  byClass(cell, 'dn-ovr-arm').dispatchEvent(makeEvent('click'));
  byClass(cell, 'dn-ovr-reject').dispatchEvent(makeEvent('click'));
  await flush();
  assert(byClass(cell, 'dn-ovr-err'), 'a failed POST surfaces an error chip');
  assertEqual(ui.pendingOverride('v9'), null, 'a rejected POST does NOT stamp an optimistic override');
});

// ── 5. pendingOverrideDigest: timestamp-free stability ───────────────────────
test('pendingOverrideDigest: empty when none pending; folds a queued stamp; sorted + no ts', () => {
  reset();
  assertEqual(JSON.stringify(ui.pendingOverrideDigest(['a', 'b'])), '[]', 'no pending → empty digest fragment (back-compat)');
  ui.markPendingOverride('b', 'promote', 'rb');
  ui.markPendingOverride('a', 'reject', 'ra');
  const d = ui.pendingOverrideDigest(['b', 'a', 'c']);
  assertEqual(d.length, 2, 'only the two pending gids contribute (c has none)');
  assertEqual(d[0][0], 'a', 'sorted by gid (stable order across re-render)');
  // the same two stamps yield a byte-identical digest beat-over-beat.
  assertEqual(JSON.stringify(ui.pendingOverrideDigest(['a', 'b'])), JSON.stringify(ui.pendingOverrideDigest(['b', 'a'])),
    'order-independent — two beats are byte-identical');
});

// ── 6. end-to-end: standingsTable control column for ALL structures ──────────
function structFor(structure, opts) {
  const o = opts || {};
  return {
    structure, live: o.live !== false, source: 'active', tournament_id: 't1',
    competitors: [{ generation_id: 'c1', seed: 1 }, { generation_id: 'c2', seed: 2 }],
    rounds: [],
    standings: [
      { generation_id: 'c1', rank: 1, scalar: 47.5, wins: 1, losses: 0, status: o.live === false ? 'champion' : 'competing' },
      { generation_id: 'c2', rank: 2, scalar: 52.1, wins: 0, losses: 1, status: o.live === false ? 'eliminated' : 'competing' },
    ],
    field_status: [],
    override_status: o.override_status,
    promoted_generation_ids: o.promoted_generation_ids,
  };
}

function renderStruct(st) {
  const ctx = { navigate() {}, href: router.href };
  const nodes = STRUCT.renderStructure(st, ctx, 'e0');
  const host = document.createElement('div');
  for (const n of nodes) host.appendChild(n);
  return host;
}

for (const structure of ['gauntlet', 'swiss', 'single_elim', 'double_elim', 'racing']) {
  test('standings (' + structure + '): a LIVE field renders the override CONTROL column for every challenger', () => {
    reset();
    const host = renderStruct(structFor(structure, { live: true }));
    const cells = allByClass(host, 'dn-ovr-ctl');
    assertEqual(cells.length, 2, 'both challengers get an override control cell');
    assert(cells.every((c) => byClass(c, 'dn-ovr-arm')), 'each cell offers the arm control while live');
  });
}

test('standings: read_only field renders the control DISABLED for every challenger', () => {
  reset();
  state.health = { read_only: true };
  const host = renderStruct(structFor('double_elim', { live: true }));
  const arms = allByClass(host, 'dn-ovr-arm');
  assertEqual(arms.length, 2, 'both rows show the (disabled) control');
  assert(arms.every((a) => a.getAttribute('disabled') === 'disabled'), 'every control is disabled in a read-only workspace');
  state.health = { read_only: false };
});

test('standings: firing an override stamps a queued chip + folds into structureDigest (live), no-op beat ZERO DOM', async () => {
  reset();
  // the rendered standings cell POSTs through the real postFieldOverride →
  // fetch; stub fetch to a 202-accepted control file and capture the path + body.
  const calls = [];
  const prevFetch = globalThis.fetch;
  globalThis.fetch = async (path, init) => {
    calls.push({ path, body: init && init.body ? JSON.parse(init.body) : null });
    return { ok: true, status: 202, json: async () => ({ generation_id: 'c1', ts: 'x' }) };
  };
  const st = structFor('double_elim', { live: true });
  const before = STRUCT.structureDigest(st);
  // fire a force-promote on c1 through the rendered control.
  const host = renderStruct(st);
  const c1cell = allByClass(host, 'dn-ovr-ctl').find((c) => c.getAttribute('data-ovr-ctl') === 'c1');
  byClass(c1cell, 'dn-ovr-arm').dispatchEvent(makeEvent('click'));
  byClass(c1cell, 'dn-ovr-promote').dispatchEvent(makeEvent('click'));
  await flush();
  assert(calls.length === 1, 'exactly one control POST fired');
  assert(/\/api\/control\/promote\/c1$/.test(calls[0].path), 'the POST targets the per-generation promote route');
  assertEqual(calls[0].body.tournament_id, 't1', 'the body names the tournament_id for the readback');
  assertEqual(calls[0].body.structure, 'double_elim', 'the body names the structure');
  assertEqual(calls[0].body.epoch, 'e0', 'the body names the epoch');
  globalThis.fetch = prevFetch;
  assert(ui.pendingOverride('c1'), 'c1 now carries an optimistic queued override');
  // the queued stamp flips the structureDigest (a real change repaints).
  const after = STRUCT.structureDigest(st);
  assert(before !== after, 'the queued override flips the structureDigest (repaints)');
  // re-render with the SAME state → the queued chip rides beside the pill.
  const host2 = renderStruct(st);
  const chips = allByClass(host2, 'dn-override');
  assert(chips.some((c) => hasClass(c, 'dn-override-queued')), 'the queued override renders a caution chip beside the pill');
  // a NO-OP beat (identical state) is byte-identical + churns ZERO DOM under the gate.
  const gateHost = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  ui.gatedSwap(gateHost, STRUCT.structureDigest(st), () => STRUCT.renderStructure(st, ctx, 'e0'));
  const writes = gateHost.innerHTMLWriteCount();
  const tableA = byClass(gateHost, 'dt-standings');
  ui.gatedSwap(gateHost, STRUCT.structureDigest(st), () => STRUCT.renderStructure(st, ctx, 'e0'));
  const tableB = byClass(gateHost, 'dt-standings');
  assert(tableA === tableB, 'a no-op beat over a queued override preserves node identity (zero rebuild)');
  assertEqual(gateHost.innerHTMLWriteCount(), writes, 'a no-op beat writes ZERO additional DOM');
});

test('standings: a durable readback SUPERSEDES the optimistic stamp (no double-stamp)', () => {
  reset();
  ui.markPendingOverride('c1', 'promote', 'optimistic');
  // the readback now carries c1 as a committed (applied) promote.
  const st = structFor('double_elim', {
    live: false,
    override_status: { c1: { action: 'promote', ts: '2026-06-13T00:00:00Z', reason: 'committed', state: 'applied' } },
    promoted_generation_ids: ['c1'],
  });
  const host = renderStruct(st);
  // exactly ONE c1 chip (the durable one), and the optimistic stamp is cleared.
  assertEqual(ui.pendingOverride('c1'), null, 'the optimistic stamp is dropped once the readback supersedes it');
  const c1Chips = allByClass(host, 'dn-override').filter((c) => /committed/.test(c.getAttribute('title') || ''));
  assert(c1Chips.length >= 1, 'the durable (committed) override is the one rendered');
});

// ── 7. DRAINED — a queued promote the settle never advanced ──────────────────
test('standings: a queued promote on a SETTLED field whose gid did not advance reads DRAINED', () => {
  reset();
  ui.markPendingOverride('c2', 'promote', 'long shot');
  // settled, and the advanced set promoted c1 only (c2 drained).
  const st = structFor('double_elim', { live: false, promoted_generation_ids: ['c1'] });
  const host = renderStruct(st);
  const drained = allByClass(host, 'dn-override-drained');
  assert(drained.length >= 1, 'the queued-but-never-fired override reads drained');
  // the provenance caption names the drained outcome.
  const cap = byClass(host, 'dt-standings-override');
  assert(cap && /drained/.test(cap.textContent), 'the caption names the drained override');
});

// ── 8. the provenance caption + MULTIPLE promoted (ties) ─────────────────────
test('standings: a durable override readback renders the "gate said … · operator forced …" caption', () => {
  reset();
  const st = structFor('swiss', {
    live: false,
    override_status: {
      c1: { action: 'promote', ts: '2026-06-13T00:00:00Z', reason: 'operator call', state: 'applied' },
      c2: { action: 'reject', ts: '2026-06-13T00:00:00Z', reason: 'spurious', state: 'applied' },
    },
    promoted_generation_ids: ['c1'],
  });
  const host = renderStruct(st);
  const cap = byClass(host, 'dt-standings-override');
  assert(cap, 'the override-provenance caption renders');
  assert(/gate said/.test(cap.textContent), 'the caption names what the gate said');
  assert(/forced 1 promotion/.test(cap.textContent), 'it names the forced promotion');
  assert(/forced 1 rejection/.test(cap.textContent), 'and the forced rejection');
});

test('standings: MULTIPLE promoted (ties) — two force-promotions read as a pluralised caption + two chips', () => {
  reset();
  const st = structFor('racing', {
    live: false,
    override_status: {
      c1: { action: 'promote', ts: '2026-06-13T00:00:00Z', reason: 'tie a', state: 'applied' },
      c2: { action: 'promote', ts: '2026-06-13T00:00:00Z', reason: 'tie b', state: 'applied' },
    },
    promoted_generation_ids: ['c1', 'c2'],
  });
  const host = renderStruct(st);
  const promoteChips = allByClass(host, 'dn-override-promote');
  assertEqual(promoteChips.length, 2, 'both tied challengers carry a force-promote chip (multiple promoted supported)');
  const cap = byClass(host, 'dt-standings-override');
  assert(/forced 2 promotions/.test(cap.textContent), 'the caption pluralises the two promotions');
});

// ── 9. back-compat: no override + no control state → byte-identical structurally ─
test('structureDigest: a settled field with no overrides folds NO pending/promoted noise (back-compat stable)', () => {
  reset();
  const a = structFor('double_elim', { live: false });
  const b = structFor('double_elim', { live: false });
  assertEqual(STRUCT.structureDigest(a), STRUCT.structureDigest(b), 'two clean beats are byte-identical');
  const clean = STRUCT.structureDigest(a);
  // a queued override APPEARING must flip the digest vs the clean baseline (the
  // control plane folds the optimistic stamp in).
  ui.markPendingOverride('c1', 'promote', 'x');
  assert(STRUCT.structureDigest(a) !== clean, 'a queued override appearing flips the digest off the clean baseline');
  reset();
  assertEqual(STRUCT.structureDigest(a), clean, 'cleared again → byte-identical to the clean baseline');
});

await run();
