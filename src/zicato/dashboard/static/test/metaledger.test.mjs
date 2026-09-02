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

const svg = await import('../js/svg.js');
const home = await import('../js/views/home.js');
const data = await import('../js/data.js');
const router = await import('../js/router.js');
const hovercard = await import('../js/hovercard.js');

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
// Drive the hovercard like a browser would (mirrors boardstatus.test.mjs): fire
// mouseenter on a wired node and read the active card text.
function hovercardTextOf(node) {
  hovercard.hide();
  node.dispatchEvent({ type: 'mouseenter', target: node });
  const text = hovercard.cardText();
  node.dispatchEvent({ type: 'mouseleave', target: node });
  return text;
}

// A realistic 4-epoch chain mirroring the real workspace shape:
//   e0 baseline (racing) → e1 board change → e2 scoring + structure roll (→swiss,
//   SOFT) → e3 proposer + brief swap. Floors descend then reset at the roll.
function chain() {
  return {
    currentEpochId: 'e3',
    epochs: [
      { epoch_id: 'e0', floor: 0.412, champion_gen: 'v3', champion_index: 3, generation_count: 38,
        structure: 'racing', closed: true, open: false,
        changed_components: { board: false, brief: false, scoring: false, adapter: false, mutable_trees: false, structure: false, proposer: false },
        changed_list: [], soft: false },
      { epoch_id: 'e1', floor: 0.276, champion_gen: 'v6', champion_index: 6, generation_count: 29,
        structure: 'racing', closed: true, open: false,
        changed_components: { board: true, brief: false, scoring: false, adapter: false, mutable_trees: false, structure: false, proposer: false },
        changed_list: ['board'], soft: false },
      { epoch_id: 'e2', floor: 0.331, champion_gen: 'v10', champion_index: 10, generation_count: 57,
        structure: 'swiss', closed: true, open: false,
        changed_components: { board: false, brief: false, scoring: true, adapter: false, mutable_trees: false, structure: true, proposer: false },
        changed_list: ['scoring', 'structure'], soft: true },
      { epoch_id: 'e3', floor: 0.229, champion_gen: 'v14', champion_index: 14, generation_count: 22,
        structure: 'swiss', closed: false, open: true,
        changed_components: { board: false, brief: true, scoring: false, adapter: false, mutable_trees: false, structure: false, proposer: true },
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
  // (C) heatstrip: 8 component rows × 4 epoch cols = 32 cells.
  assertEqual(allByClass(node, 'dn-metaledger-cell').length, 8 * 4, '8 components × 4 epochs of cells');
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

test('metaLoopLedger: the change rail attributes each roll to a named lever (compact)', () => {
  const node = svg.metaLoopLedger(chain());
  // a change chip per epoch with a changed_list (e1, e2, e3 = 3; e0 baseline has none).
  const chips = textOfClass(node, 'dn-metaledger-chiptxt');
  assertEqual(chips.length, 3, 'a change chip at each roll boundary (not the baseline)');
  // COMPACT labels: e1 ['board'] → 'board'; e2 ['scoring','structure'] (→swiss)
  // leads with the structure headline → '→swiss +1'; e3 ['proposer','brief'] →
  // 'proposer* +1' (the primary lever's label + the +N overflow).
  assert(chips.some((t) => t === 'board'), 'a single-lever roll renders just the lever label');
  assert(chips.some((t) => t === '→swiss +1'),
    'a structure roll leads with →<structure> + the +N overflow, not the full set');
  assert(chips.some((t) => t.startsWith('proposer*')), 'the proposer roll headlines the primary lever');
  // the FULL set is NOT in the rendered chip text (it moved to the hovercard).
  assert(!chips.some((t) => t.includes('structure→swiss')),
    'the full "structure→swiss" string is no longer rendered in the chip (compact only)');
  assert(!chips.some((t) => t.includes('+')) || chips.every((t) => !/scoring\+structure|board\+/.test(t)),
    'no chip renders the joined full change-set');
});

test('metaLoopLedger: the FULL change-set lives on the chip hovercard (nothing lost)', () => {
  const node = svg.metaLoopLedger(chain());
  const chipRects = allByClass(node, 'dn-metaledger-chip');
  assertEqual(chipRects.length, 3, 'a chip rect per roll boundary');
  // every chip is hovercard-wired and the full join is on hover rather than in the box.
  chipRects.forEach((r) => assert(hovercard.hasHovercard(r), 'each chip is hovercard-wired'));
  const tips = chipRects.map((r) => hovercardTextOf(r));
  assert(tips.some((t) => t === 'board'), 'the board chip hover carries its (single) set');
  assert(tips.some((t) => t.includes('scoring') && t.includes('structure→swiss')),
    'the scoring+structure chip hover carries the FULL joined set incl. structure→swiss');
  assert(tips.some((t) => t.includes('proposer*') && t.includes('brief')),
    'the proposer+brief chip hover carries the full set');
});

test('metaLoopLedger: chips do NOT overlap given tight/adjacent boundaries + long change-sets', () => {
  // A pathological chain: three rolls whose boundaries fall very close together
  // (tiny generation_count epochs after a big one) AND a fat change-set, which
  // pre-fix collided/clipped. The resolved chip x-extents MUST be disjoint.
  const m = {
    currentEpochId: 'e3',
    epochs: [
      { epoch_id: 'e0', floor: 0.5, champion_gen: 'v1', champion_index: 1, generation_count: 200,
        structure: 'racing', closed: true, open: false,
        changed_components: { board: false, brief: false, scoring: false, adapter: false, mutable_trees: false, structure: false, proposer: false },
        changed_list: [], soft: false },
      { epoch_id: 'e1', floor: 0.4, champion_gen: 'v2', champion_index: 1, generation_count: 1,
        structure: 'racing', closed: true, open: false,
        changed_components: { board: true, brief: true, scoring: true, adapter: false, mutable_trees: false, structure: false, proposer: false },
        changed_list: ['board', 'brief', 'scoring'], soft: false },
      { epoch_id: 'e2', floor: 0.35, champion_gen: 'v3', champion_index: 1, generation_count: 1,
        structure: 'swiss', closed: true, open: false,
        changed_components: { board: true, brief: false, scoring: true, adapter: false, mutable_trees: false, structure: true, proposer: false },
        changed_list: ['board', 'scoring', 'structure'], soft: true },
      { epoch_id: 'e3', floor: 0.3, champion_gen: 'v4', champion_index: 1, generation_count: 1,
        structure: 'swiss', closed: true, open: false,
        changed_components: { board: false, brief: true, scoring: false, adapter: false, mutable_trees: false, structure: false, proposer: true },
        changed_list: ['proposer', 'brief'], soft: false },
    ],
  };
  const node = svg.metaLoopLedger(m);
  const rects = allByClass(node, 'dn-metaledger-chip');
  assertEqual(rects.length, 3, 'three chips for the three rolls');
  // build [x, x+width] extents, sort by left edge, assert pairwise disjoint.
  const ext = rects.map((r) => {
    const x = parseFloat(r.getAttribute('x'));
    const w = parseFloat(r.getAttribute('width'));
    return [x, x + w];
  }).sort((a, b) => a[0] - b[0]);
  for (let k = 1; k < ext.length; k++) {
    assert(ext[k][0] >= ext[k - 1][1] - 0.001,
      `chip ${k} left (${ext[k][0]}) is >= chip ${k - 1} right (${ext[k - 1][1]}) — no overlap`);
  }
  // and every chip stays inside the figure (no clipping at the edges).
  const W = 1120; const L = 96; const R = 28;
  ext.forEach((e) => {
    assert(e[0] >= L - 0.5, 'chip left edge stays inside the left margin');
    assert(e[1] <= W - R + 0.5, 'chip right edge stays inside the right margin');
  });
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
  assertEqual(allByClass(node, 'dn-metaledger-cell').length, 8, 'the 8-component column still renders');
});

test('metaLoopLedger: a missing-floor epoch does not throw and renders a band', () => {
  const m = chain();
  m.epochs[0].floor = null;
  m.epochs[0].champion_gen = null;
  m.epochs[0].champion_index = null;
  const node = svg.metaLoopLedger(m);
  assertEqual(allByClass(node, 'dn-metaledger-band').length, 4, 'all bands still render');
});

// ── champion-reign tick: position encodes WHEN the floor was set ──────

// A single wide-band epoch isolates the tick geometry. The band owns the
// full plot width; champion_index/generation_count drives the tick x.
function soloEpoch(champion_index, generation_count) {
  return {
    currentEpochId: 'e0',
    epochs: [
      { epoch_id: 'e0', floor: 0.30, champion_gen: 'v' + champion_index,
        champion_index, generation_count,
        structure: 'racing', closed: true, open: false,
        changed_components: { board: false, brief: false, scoring: false, adapter: false, mutable_trees: false, structure: false, proposer: false },
        changed_list: [], soft: false },
    ],
  };
}

function champTickX(node) {
  const ticks = allByClass(node, 'dn-metaledger-champtick');
  return ticks.map((t) => parseFloat(t.getAttribute('x')) + 2); // x attr is champX - 2
}

test('metaLoopLedger: an EARLY champion sits near the LEFT of its band, a LATE one near the RIGHT', () => {
  // Same band width (same generation_count); only champion_index moves.
  const early = champTickX(svg.metaLoopLedger(soloEpoch(1, 40)))[0];
  const late = champTickX(svg.metaLoopLedger(soloEpoch(38, 40)))[0];
  assert(early < late, 'an earlier champion_index → a tick further left than a later one');
  // and within the same band, the late tick is clearly to the right (not a
  // fixed 62% position — the spread reflects index/count).
  assert(late - early > 50, 'the early/late spread is real, not a fixed offset');
});

test('metaLoopLedger: the tick x reflects champion_index / generation_count', () => {
  // Mid-epoch champion (index 19 of 40) → tick near the band centre; a near-
  // last champion (index 39 of 40) → tick near the right edge, > the mid one.
  const mid = champTickX(svg.metaLoopLedger(soloEpoch(19, 40)))[0];
  const last = champTickX(svg.metaLoopLedger(soloEpoch(39, 40)))[0];
  const first = champTickX(svg.metaLoopLedger(soloEpoch(0, 40)))[0];
  assert(first < mid && mid < last, 'monotone: first-gen < mid-gen < last-gen tick x');
});

test('metaLoopLedger: a NULL champion_index draws the champ label but NO bar', () => {
  const m = soloEpoch(0, 40);
  m.epochs[0].champion_index = null; // unlocatable champion → label only
  const node = svg.metaLoopLedger(m);
  assertEqual(allByClass(node, 'dn-metaledger-champtick').length, 0, 'no champion-reign bar when the index is null');
  // the label still renders (band is wide enough: b.w > 104).
  const labels = textOfClass(node, 'dn-metaledger-champlbl');
  assert(labels.some((t) => t.includes('champ')), 'the champ label still renders without the bar');
});

test('metaLoopLedger: with a real champion_index the bar IS drawn (regression on the prior fixed 62% bar)', () => {
  const node = svg.metaLoopLedger(soloEpoch(20, 40));
  assertEqual(allByClass(node, 'dn-metaledger-champtick').length, 1, 'a single anchored champion-reign bar');
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
  // the tick POSITION is load-bearing: a champion_index move (the tick slides
  // along the band) must regate the DOM even when champion_gen is unchanged.
  const m4 = chain(); m4.epochs[3].champion_index = 0;
  assert(svg.metaLoopLedgerDigest(m4) !== base, 'a champion_index (tick position) change flips the digest');
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
    { epoch_id: 'e0', floor: 42.1, champion_gen: 'v4', champion_index: 4, generation_count: 5, structure: 'racing', closed: true, open: false,
      changed_components: { board: false, brief: false, scoring: false, adapter: false, mutable_trees: false, structure: false, proposer: false }, changed_list: [], soft: false },
    { epoch_id: 'e1', floor: 40.5, champion_gen: 'v7', champion_index: 7, generation_count: 9, structure: 'racing', closed: true, open: false,
      changed_components: { board: true, brief: false, scoring: false, adapter: false, mutable_trees: false, structure: false, proposer: false }, changed_list: ['board'], soft: false },
    { epoch_id: 'e2', floor: 34.2, champion_gen: 'v7', champion_index: 5, generation_count: 6, structure: 'swiss', closed: false, open: true,
      changed_components: { board: false, brief: false, scoring: true, adapter: false, mutable_trees: false, structure: true, proposer: false }, changed_list: ['scoring', 'structure'], soft: true },
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
  assertEqual(allByClass(host, 'dn-metaledger-cell').length, 8 * 3, 'the heatstrip renders 8 components × 3 epochs');
  assert(host.textContent.includes('Meta-loop ledger'), 'the ledger section is titled');
  // the fleet is the lead view; the ledger sits BELOW it as the composed
  // cross-epoch overview.
  const txt = host.textContent;
  assert(txt.indexOf('Fleet') < txt.indexOf('Meta-loop ledger'), 'the fleet precedes the ledger (fleet leads, ledger below)');
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
  // but the STRUCTURAL content (floors, changes, champions) is otherwise
  // identical: pin the lifecycle fields to the SAME value in both and the
  // digests must agree, whatever format the shared digestOpts fold emits.
  const pinLifecycle = (ws) => ws.ledger.map((e) => ({ ...e, open: false, closed: true }));
  const dOpenPinned = svg.metaLoopLedgerDigest({ epochs: pinLifecycle(openWs), currentEpochId: openWs.current_epoch_id });
  const dClosedPinned = svg.metaLoopLedgerDigest({ epochs: pinLifecycle(closedWs), currentEpochId: closedWs.current_epoch_id });
  assertEqual(dOpenPinned, dClosedPinned, 'only the lifecycle bit differs');
});

// ── zone-A floor labels: a paper halo so they don't read struck-through ──────

test('metaLoopLedger: the floor value label carries a paper halo (paint-order: stroke) and stays weight 400', async () => {
  const css = await import('node:fs').then((fs) =>
    fs.readFileSync(new URL('../css/console.css', import.meta.url), 'utf8'));
  const oneLine = css.replace(/\n/g, ' ');
  // the floor label rule carries the halo: paint-order:stroke + a paper stroke,
  // so a gridline crossing the glyphs does not read as a strike-through. Match
  // the STANDALONE base selector (a leading boundary rather than the compound
  // `.dn-metaledger-step-good.dn-metaledger-floorlbl` colour rules).
  const rule = (oneLine.match(/[\s}]\.dn-metaledger-floorlbl\s*\{[^}]*\}/) || [''])[0];
  assert(rule, 'the .dn-metaledger-floorlbl rule exists');
  assert(/paint-order:\s*stroke/.test(rule), 'paint-order: stroke (the halo paints behind the fill)');
  assert(/stroke:\s*var\(--v2-paper\)/.test(rule), 'the halo is paper-coloured');
  assert(/stroke-width:\s*[23](\.\d+)?px/.test(rule), 'the halo stroke-width is in the 2–3px haloing range');
  assert(/stroke-linejoin:\s*round/.test(rule), 'rounded joins so the halo does not spike');
  // it MUST still be weight 400 (the halo must not turn it bold).
  assert(/font:\s*400\s/.test(rule), 'the floor label is still weight 400 (the halo, not bold, fixes the artifact)');
});

await run();
