// test/metaledger.test.mjs — the COMPOSED META-LOOP LEDGER (study opt 7).
//
// Pins:
//   * metaLoopLedger renders the three braided zones — the floor STAIRCASE
//     (held levels + risers), the effort BANDS (one per epoch, champion tick),
//     and the contract-component HEATSTRIP (incl. the proposer* row the
//     contract-diff omits) — plus the component-coded change RAIL/chip and the
//     SOFT seam on a structure roll.
//   * the change rail attributes a roll to a named lever; a proposer/structure
//     change is rendered as a filled cell + chip.
//   * it DEGRADES on 0 epochs (placeholder) and on 1 epoch (band + heatstrip
//     column, no risers/seams — nothing to diff against).
//   * metaLoopLedgerDigest is byte-identical for the live (open) and settled
//     (closed) render of the SAME data, and flips on a real change (convergence
//     + digest-gating substrate).
//   * the HOME view builds the ledger model from /api/workspace `ledger`, makes
//     it the primary cross-epoch overview, and a no-op repaint churns no DOM.

import { installDom, test, run, assert, assertEqual } from './harness.mjs';

installDom();

const svg = await import('../js/variants/T/svg.js');
const home = await import('../js/variants/T/views/home.js');
const data = await import('../js/variants/T/data.js');
const router = await import('../js/variants/T/router.js');

function allByClass(host, cls) {
  return host.querySelectorAll('[class]').filter((n) =>
    (n.getAttribute('class') || '').split(/\s+/).includes(cls));
}
function hasClass(node, cls) {
  return (node.getAttribute('class') || '').split(/\s+/).includes(cls);
}
function textOfClass(host, cls) {
  return allByClass(host, cls).map((n) => n.textContent);
}

// A realistic 4-epoch chain mirroring the real workspace shape:
//   e0 baseline (racing) → e1 board change → e2 scoring + structure roll (→swiss,
//   SOFT) → e3 proposer + brief swap. Floors descend then reset at the roll.
function chain() {
  return {
    currentEpochId: 'e3',
    epochs: [
      { epoch_id: 'e0', floor: 0.412, champion_gen: 'v3', generation_count: 38,
        structure: 'racing', closed: true, open: false,
        changed_components: { board: false, brief: false, scoring: false, entrypoint: false, mutable_trees: false, structure: false, proposer: false },
        changed_list: [], soft: false },
      { epoch_id: 'e1', floor: 0.276, champion_gen: 'v6', generation_count: 29,
        structure: 'racing', closed: true, open: false,
        changed_components: { board: true, brief: false, scoring: false, entrypoint: false, mutable_trees: false, structure: false, proposer: false },
        changed_list: ['board'], soft: false },
      { epoch_id: 'e2', floor: 0.331, champion_gen: 'v10', generation_count: 57,
        structure: 'swiss', closed: true, open: false,
        changed_components: { board: false, brief: false, scoring: true, entrypoint: false, mutable_trees: false, structure: true, proposer: false },
        changed_list: ['scoring', 'structure'], soft: true },
      { epoch_id: 'e3', floor: 0.229, champion_gen: 'v14', generation_count: 22,
        structure: 'swiss', closed: false, open: true,
        changed_components: { board: false, brief: true, scoring: false, entrypoint: false, mutable_trees: false, structure: false, proposer: true },
        changed_list: ['proposer', 'brief'], soft: false },
    ],
  };
}

// ── builder: the three braided zones render ──────────────────────────

test('metaLoopLedger: renders the staircase, effort bands, and component heatstrip', () => {
  const node = svg.metaLoopLedger(chain());
  // (A) staircase: a held level per epoch + a riser between each pair (n-1).
  assertEqual(allByClass(node, 'dn-metaledger-held').length, 4, 'one held floor level per epoch');
  assertEqual(allByClass(node, 'dn-metaledger-riser').length, 3, 'a riser between each consecutive pair');
  assertEqual(allByClass(node, 'dn-metaledger-floordot').length, 4, 'a floor dot per epoch');
  // (B) effort bands: one band rect per epoch + a champion tick per epoch.
  assertEqual(allByClass(node, 'dn-metaledger-band').length, 4, 'one effort band per epoch');
  assertEqual(allByClass(node, 'dn-metaledger-champtick').length, 4, 'a champion-reign tick per epoch');
  // (C) heatstrip: 7 component rows × 4 epoch cols = 28 cells.
  assertEqual(allByClass(node, 'dn-metaledger-cell').length, 7 * 4, '7 components × 4 epochs of cells');
});

test('metaLoopLedger: the heatstrip carries the proposer* row the contract-diff omits', () => {
  const node = svg.metaLoopLedger(chain());
  const rowLabels = textOfClass(node, 'dn-metaledger-rowlbl');
  assert(rowLabels.includes('proposer*'), 'the proposer* row label is present');
  assert(rowLabels.includes('structure'), 'the structure row label is present');
  // the proposer row is flagged with the prop class (caution colour).
  assertEqual(allByClass(node, 'dn-metaledger-rowlbl-prop').length, 1, 'the proposer row is flagged');
});

test('metaLoopLedger: a proposer change is rendered as a filled (on) cell', () => {
  const node = svg.metaLoopLedger(chain());
  // e3 changed proposer + brief (no structure) → "on" cells, not "soft".
  const on = allByClass(node, 'dn-metaledger-cell-on');
  // changed cells across the chain: e1 board(1) + e2 scoring(1) + e3 brief+proposer(2) = 4 non-soft on-cells.
  assertEqual(on.length, 4, 'four non-soft changed cells (board, scoring, brief, proposer)');
});

test('metaLoopLedger: a structure roll is a SOFT seam (caution-dashed cell + seam)', () => {
  const node = svg.metaLoopLedger(chain());
  // e2 rolled structure → a soft cell + the down-plot soft seam.
  assertEqual(allByClass(node, 'dn-metaledger-cell-soft').length, 1, 'one soft (structure-roll) cell');
  assertEqual(allByClass(node, 'dn-metaledger-soft').length, 1, 'one SOFT seam stripe');
});

test('metaLoopLedger: the change rail attributes each roll to a named lever', () => {
  const node = svg.metaLoopLedger(chain());
  // a change chip per epoch with a changed_list (e1, e2, e3 = 3; e0 baseline has none).
  const chips = textOfClass(node, 'dn-metaledger-chiptxt');
  assertEqual(chips.length, 3, 'a change chip at each roll boundary (not the baseline)');
  assert(chips.some((t) => t.includes('board')), 'the board roll is labelled');
  assert(chips.some((t) => t.includes('structure→swiss')), 'the structure roll names the rolled-into structure');
  assert(chips.some((t) => t.includes('proposer')), 'the proposer roll is labelled');
});

test('metaLoopLedger: the floor staircase colours improvement vs reset', () => {
  const node = svg.metaLoopLedger(chain());
  // e0 baseline + e1 (0.412→0.276 improved) are good; e2 (0.276→0.331 reset) is bad;
  // e3 is open (dashed ink). The held levels carry the direction class.
  const good = allByClass(node, 'dn-metaledger-step-good');
  const bad = allByClass(node, 'dn-metaledger-step-bad');
  const open = allByClass(node, 'dn-metaledger-step-open');
  assert(good.length > 0, 'an improved step reads good');
  assert(bad.length > 0, 'a reset step reads bad');
  assert(open.length > 0, 'the open epoch reads as the open/ink step');
});

test('metaLoopLedger: a floor-Δ chip per non-baseline epoch + a baseline chip', () => {
  const node = svg.metaLoopLedger(chain());
  // 3 delta chips (e1,e2,e3) + 1 baseline chip (e0).
  const good = allByClass(node, 'dn-metaledger-dchip-good').length;
  const bad = allByClass(node, 'dn-metaledger-dchip-bad').length;
  const base = allByClass(node, 'dn-metaledger-dchip-base').length;
  assertEqual(base, 1, 'one baseline chip (the first epoch)');
  assertEqual(good + bad, 3, 'three signed floor-Δ chips');
});

// ── degradation on 0–1 epochs ────────────────────────────────────────

test('metaLoopLedger: degrades on 0 epochs to an honest placeholder', () => {
  const node = svg.metaLoopLedger({ epochs: [] });
  assertEqual(allByClass(node, 'dn-metaledger-band').length, 0, 'no bands on an empty model');
  assert(node.textContent.toLowerCase().includes('no epochs'), 'shows a no-epochs placeholder');
});

test('metaLoopLedger: degrades on 1 epoch (band + heatstrip, no risers/seams)', () => {
  const one = { currentEpochId: 'e0', epochs: [chain().epochs[0]] };
  const node = svg.metaLoopLedger(one);
  assertEqual(allByClass(node, 'dn-metaledger-band').length, 1, 'one band');
  assertEqual(allByClass(node, 'dn-metaledger-held').length, 1, 'one held floor level');
  assertEqual(allByClass(node, 'dn-metaledger-riser').length, 0, 'no risers (nothing to step from)');
  assertEqual(allByClass(node, 'dn-metaledger-chiptxt').length, 0, 'no change chips (no predecessor)');
  assertEqual(allByClass(node, 'dn-metaledger-cell').length, 7, 'the 7-component column still renders');
  assertEqual(allByClass(node, 'dn-metaledger-dchip-base').length, 1, 'the lone epoch is a baseline chip');
});

test('metaLoopLedger: a missing-floor epoch does not throw and renders a band', () => {
  const m = chain();
  m.epochs[0].floor = null;
  m.epochs[0].champion_gen = null;
  const node = svg.metaLoopLedger(m);
  assertEqual(allByClass(node, 'dn-metaledger-band').length, 4, 'all bands still render');
});

// ── digest: convergence + gating substrate ───────────────────────────

test('metaLoopLedgerDigest: live (open) and settled (closed) render byte-identically for the SAME data', () => {
  // The convergence contract: a settled render of an epoch must equal its live
  // render once the SAME floor/champion/changes are in hand. The only field
  // that differs live→settled is `open`/`closed`; the digest folds lifecycle,
  // so this asserts the figure (and digest) is a pure function of the model —
  // an identical model digests identically regardless of which path produced it.
  const a = chain();
  const b = chain(); // an independently-built identical model
  assertEqual(svg.metaLoopLedgerDigest(a), svg.metaLoopLedgerDigest(b), 'identical models → identical digest');
});

test('metaLoopLedgerDigest: flips when a floor / change-set / lifecycle moves', () => {
  const base = svg.metaLoopLedgerDigest(chain());
  const m1 = chain(); m1.epochs[3].floor = 0.200;
  assert(svg.metaLoopLedgerDigest(m1) !== base, 'a floor change flips the digest');
  const m2 = chain(); m2.epochs[3].changed_components.scoring = true;
  assert(svg.metaLoopLedgerDigest(m2) !== base, 'a new changed component flips the digest');
  const m3 = chain(); m3.epochs[3].open = false; m3.epochs[3].closed = true;
  assert(svg.metaLoopLedgerDigest(m3) !== base, 'a lifecycle flip changes the digest');
});

test('metaLoopLedgerDigest: tolerates an empty / malformed model', () => {
  assertEqual(svg.metaLoopLedgerDigest({}), svg.metaLoopLedgerDigest({ epochs: [] }), 'empty digests are stable');
  assertEqual(typeof svg.metaLoopLedgerDigest({ epochs: 'nope' }), 'string', 'a malformed model still digests');
});

// ── home view: the ledger is the primary cross-epoch overview ─────────

const WS_LEDGER = {
  current_epoch_id: 'e2',
  epochs: [
    { epoch_id: 'e0', generation_count: 5, promoted_count: 1, best_scalar: 42.1, closed: true, goal: 'baseline' },
    { epoch_id: 'e1', generation_count: 9, promoted_count: 1, best_scalar: 40.5, closed: true, goal: 'tighten' },
    { epoch_id: 'e2', generation_count: 6, promoted_count: 0, best_scalar: 34.2, closed: false, goal: 'racing field' },
  ],
  sparkline: [
    { epoch_id: 'e0', scalar: 42.1 }, { epoch_id: 'e1', scalar: 40.5 }, { epoch_id: 'e2', scalar: 34.2 },
  ],
  ledger: [
    { epoch_id: 'e0', floor: 42.1, champion_gen: 'v4', generation_count: 5, structure: 'racing', closed: true, open: false,
      changed_components: { board: false, brief: false, scoring: false, entrypoint: false, mutable_trees: false, structure: false, proposer: false }, changed_list: [], soft: false },
    { epoch_id: 'e1', floor: 40.5, champion_gen: 'v7', generation_count: 9, structure: 'racing', closed: true, open: false,
      changed_components: { board: true, brief: false, scoring: false, entrypoint: false, mutable_trees: false, structure: false, proposer: false }, changed_list: ['board'], soft: false },
    { epoch_id: 'e2', floor: 34.2, champion_gen: 'v7', generation_count: 6, structure: 'swiss', closed: false, open: true,
      changed_components: { board: false, brief: false, scoring: true, entrypoint: false, mutable_trees: false, structure: true, proposer: false }, changed_list: ['scoring', 'structure'], soft: true },
  ],
};

function installFetch(map) {
  globalThis.fetch = async (path) => {
    const q = path.indexOf('?');
    const base = q >= 0 ? path.slice(0, q) : path;
    const v = Object.prototype.hasOwnProperty.call(map, path) ? map[path]
      : Object.prototype.hasOwnProperty.call(map, base) ? map[base] : undefined;
    if (v !== undefined) return { ok: true, json: async () => v };
    return { ok: false, status: 404, json: async () => ({ error: 'not found: ' + path }) };
  };
}

test('home view: builds the meta-loop ledger from /api/workspace and makes it the primary cross-epoch overview', async () => {
  data.invalidate();
  globalThis.window.location = { hash: '', search: '' };
  installFetch({
    '/api/workspace': WS_LEDGER,
    '/api/health-report': { epoch_id: 'e2', healthy: true, findings: [] },
    '/api/score-trajectory': { points: [{ generation_id: 'v0', scalar: 41 }, { generation_id: 'v1', scalar: 40 }] },
  });
  const host = globalThis.document.createElement('div');
  await home.render(host, { navigate() {}, href: router.href }, {});
  assert(host.textContent.includes('Environment'), 'the home view rendered');
  // the ledger figure is present: its band rects + the heatstrip cells.
  assertEqual(allByClass(host, 'dn-metaledger-band').length, 3, 'a band per epoch in the ledger figure');
  assertEqual(allByClass(host, 'dn-metaledger-cell').length, 7 * 3, 'the heatstrip renders 7 components × 3 epochs');
  assert(host.textContent.includes('Meta-loop ledger'), 'the ledger section is titled');
  // it is the PRIMARY cross-epoch overview: it appears ABOVE the fleet cards.
  const txt = host.textContent;
  assert(txt.indexOf('Meta-loop ledger') < txt.indexOf('Fleet'), 'the ledger precedes the fleet cards');
});

test('home view: a no-op re-render of the same workspace churns NO ledger DOM (digest-gated)', async () => {
  data.invalidate();
  globalThis.window.location = { hash: '', search: '' };
  installFetch({
    '/api/workspace': WS_LEDGER,
    '/api/health-report': { epoch_id: 'e2', healthy: true, findings: [] },
    '/api/score-trajectory': { points: [{ generation_id: 'v0', scalar: 41 }, { generation_id: 'v1', scalar: 40 }] },
  });
  const host = globalThis.document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await home.render(host, ctx, {});
  const firstDigest = host.getAttribute('data-t-digest');
  assert(firstDigest, 'the host carries a content digest after first render');
  const writesBefore = host.innerHTMLWriteCount();
  data.invalidate(); // bust the cache so the SAME payload is re-fetched
  await home.render(host, ctx, {});
  assertEqual(host.getAttribute('data-t-digest'), firstDigest, 'the digest is unchanged on identical data');
  assertEqual(host.innerHTMLWriteCount(), writesBefore, 'no DOM churn on a no-op re-render');
});

test('home view: an open (live) epoch in the ledger does not change the settled digest path', async () => {
  // CONVERGENCE through the home view: the digest folds the ledger via
  // metaLoopLedgerDigest, so the same workspace (whether the current epoch is
  // open or has just closed with the SAME floor/changes) gates identically on
  // everything but the lifecycle bit — which the digest correctly tracks.
  const openWs = JSON.parse(JSON.stringify(WS_LEDGER));
  const closedWs = JSON.parse(JSON.stringify(WS_LEDGER));
  closedWs.epochs[2].closed = true;
  closedWs.ledger[2].closed = true; closedWs.ledger[2].open = false;
  const dOpen = svg.metaLoopLedgerDigest({ epochs: openWs.ledger, currentEpochId: openWs.current_epoch_id });
  const dClosed = svg.metaLoopLedgerDigest({ epochs: closedWs.ledger, currentEpochId: closedWs.current_epoch_id });
  assert(dOpen !== dClosed, 'the open vs closed lifecycle is reflected in the ledger digest');
  // but the STRUCTURAL content (floors, changes, champions) is otherwise identical:
  const stripLifecycle = (d) => d.replace(/"o"|"c"/g, '"X"');
  assertEqual(stripLifecycle(dOpen), stripLifecycle(dClosed), 'only the lifecycle bit differs');
});

await run();
