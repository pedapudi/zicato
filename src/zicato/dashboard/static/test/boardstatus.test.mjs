// test/boardstatus.test.mjs — the BOARD-STATUS surface (train/holdout split,
// ladder budget, generalization-gap trend).
//
// Pins: the model derives the split/ladder/gap DEFENSIVELY from /api/epoch
// (graceful when the overfitting `#2`/`#5` fields are absent); the render
// paints train (outline) vs holdout (accent fill) chips, the played-at legend
// with the ladder budget, and the train-vs-holdout sparklines; per-entry +
// per-panel hovercards are wired (the accessible popover); and a no-op repaint
// churns no DOM (digest-gated).

import { installDom, test, run, assert, assertEqual, assertDeep } from './harness.mjs';

installDom();

const bs = await import('../js/views/boardstatus.js');
const ui = await import('../js/ui.js');
const hovercard = await import('../js/hovercard.js');

function allByClass(host, cls) {
  return host.querySelectorAll('[class]').filter((n) =>
    (n.getAttribute('class') || '').split(/\s+/).includes(cls));
}
function hovercardTextOf(node) {
  hovercard.hide();
  node.dispatchEvent({ type: 'mouseenter', target: node });
  const text = hovercard.cardText();
  node.dispatchEvent({ type: 'mouseleave', target: node });
  return text;
}

// A fully-populated epoch payload: a configured split + a ladder summary + a
// gap series across two generations.
const EP_FULL = {
  epoch_id: 'e0',
  board: [
    { entry_id: 'b1', weight: 1.0, tags: ['adversarial'] },
    { entry_id: 'b2', weight: 2.0, tags: ['adversarial', 'rare'] },
    { entry_id: 'b3', weight: 1.0, tags: [] },
  ],
  board_split: {
    configured: true, enabled: true, holdout_fraction: 0.34,
    holdout_tags: ['rare'],
    entries: [
      { entry_id: 'b1', slice: 'train', weight: 1.0 },
      { entry_id: 'b2', slice: 'holdout', tag: 'rare', weight: 2.0 },
      { entry_id: 'b3', slice: 'train', weight: 1.0 },
    ],
    train_count: 2, holdout_count: 1, total: 3,
  },
  holdout: {
    generation_id: 'v1', confirmed: true,
    train_scalar: 0.40, holdout_scalar: 0.55,
    ladder_released: true, ladder_budget_total: 5, ladder_budget_remaining: 3,
    threshold: 0.10,
  },
  experiments: [
    { generation_id: 'v0', train_loss: 0.5, holdout_loss: 0.52, generalization_gap: 0.02 },
    { generation_id: 'v1', train_loss: 0.4, holdout_loss: 0.55, generalization_gap: 0.15 },
  ],
};

test('boardStatusModel: derives split / ladder / gap from a full payload', () => {
  const m = bs.boardStatusModel(EP_FULL);
  assertEqual(m.split.configured, true, 'split is configured');
  assertEqual(m.split.trainCount, 2, 'two train entries');
  assertEqual(m.split.holdoutCount, 1, 'one holdout entry');
  assertEqual(m.split.total, 3, 'three entries total');
  const held = m.split.entries.find((e) => e.slice === 'holdout');
  assertEqual(held.entryId, 'b2', 'b2 is the held-out entry');
  assertEqual(held.tag, 'rare', 'the why-held-out tag is carried for the popover');
  assertEqual(m.ladder.budgetRemaining, 3, 'ladder budget remaining read through');
  assertEqual(m.ladder.budgetTotal, 5, 'ladder budget total read through');
  assertEqual(m.gap.hasAny, true, 'gap series has points');
  assertEqual(m.gap.points.length, 2, 'two gap points');
  assertEqual(m.gap.widening, true, 'a 0.02 → 0.15 gap reads as widening');
});

test('boardStatusModel: empty payload degrades to honest empties (no throw)', () => {
  const m = bs.boardStatusModel({});
  assertEqual(m.split.configured, false, 'no split configured');
  assertEqual(m.split.total, 0, 'no entries');
  assertEqual(m.ladder, null, 'no ladder summary');
  assertEqual(m.gap.hasAny, false, 'no gap series');
  assertEqual(m.gap.widening, null, 'widening is null with no data');
});

test('boardStatusModel: a board with no board_split reads every entry as train', () => {
  const m = bs.boardStatusModel({ board: [{ entry_id: 'b1' }, { entry_id: 'b2' }] });
  assertEqual(m.split.total, 2, 'falls back to the raw board');
  assertEqual(m.split.holdoutCount, 0, 'no holdout without a split block');
  assertEqual(m.split.configured, false, 'not configured');
});

test('boardStatusModel: derives the gap when only the two losses are present', () => {
  const m = bs.boardStatusModel({
    experiments: [{ generation_id: 'g', train_loss: 0.2, holdout_loss: 0.5 }],
  });
  assertEqual(m.gap.points.length, 1, 'one point');
  assert(Math.abs(m.gap.points[0].gap - 0.3) < 1e-9, 'gap = holdout - train when not stamped');
});

test('renderBoardStatus: paints train (outline) + holdout (accent fill) chips', () => {
  const node = bs.renderBoardStatus(bs.boardStatusModel(EP_FULL), {});
  const train = allByClass(node, 'dn-bs-train').filter((n) => n.localName === 'span'
    && (n.getAttribute('class') || '').includes('dn-bs-chip'));
  const holdout = allByClass(node, 'dn-bs-holdout').filter((n) => n.localName === 'span'
    && (n.getAttribute('class') || '').includes('dn-bs-chip'));
  assertEqual(train.length, 2, 'two train chips');
  assertEqual(holdout.length, 1, 'one holdout chip');
});

test('renderBoardStatus: the legend shows the ladder budget remaining / total', () => {
  const node = bs.renderBoardStatus(bs.boardStatusModel(EP_FULL), {});
  const val = allByClass(node, 'dn-bs-ladder-val')[0];
  assert(val != null, 'a ladder value is painted');
  assertEqual(val.textContent, '3 / 5', 'budget remaining / total');
});

test('renderBoardStatus: ladder budget falls back to "—" + "after a run" when absent', () => {
  const node = bs.renderBoardStatus(bs.boardStatusModel({ board: [{ entry_id: 'b1' }] }), {});
  const val = allByClass(node, 'dn-bs-ladder-val')[0];
  assertEqual(val.textContent, '—', 'em-dash when no ladder budget');
  const after = allByClass(node, 'dn-faint').some((n) => (n.textContent || '').includes('after a run'));
  assert(after, 'shows the "after a run" graceful hint');
});

test('renderBoardStatus: the gap trend paints two sparklines + a verdict', () => {
  const node = bs.renderBoardStatus(bs.boardStatusModel(EP_FULL), {});
  const sparks = node.querySelectorAll('[class]').filter((n) =>
    n.localName === 'svg' && (n.getAttribute('class') || '').includes('dn-spark'));
  assert(sparks.length >= 2, 'a train + a holdout sparkline');
  const verdict = allByClass(node, 'dn-bs-verdict')[0];
  assert(verdict != null, 'a verdict line is painted');
  assert((verdict.getAttribute('class') || '').includes('dn-bad'), 'a widening gap reads as bad');
});

test('renderBoardStatus: empty gap shows the "after a run" empty state, no sparkline', () => {
  const node = bs.renderBoardStatus(bs.boardStatusModel({ board: [{ entry_id: 'b1' }] }), {});
  const sparks = node.querySelectorAll('[class]').filter((n) =>
    n.localName === 'svg' && (n.getAttribute('class') || '').includes('dn-spark'));
  assertEqual(sparks.length, 0, 'no sparkline without loss data');
  const emptyTxt = allByClass(node, 'dn-empty').some((n) => (n.textContent || '').includes('after a run'));
  assert(emptyTxt, 'an honest empty state for the gap');
});

test('renderBoardStatus: per-entry hovercard carries id, slice, weight, why-held-out', () => {
  const node = bs.renderBoardStatus(bs.boardStatusModel(EP_FULL), {});
  const holdoutChip = allByClass(node, 'dn-bs-holdout').filter((n) => n.localName === 'span'
    && (n.getAttribute('class') || '').includes('dn-bs-chip'))[0];
  assert(hovercard.hasHovercard(holdoutChip), 'the chip is hovercard-wired (accessible popover)');
  const text = hovercardTextOf(holdoutChip);
  assert(text.includes('b2'), 'card names the entry');
  assert(text.includes('holdout'), 'card names the slice');
  assert(text.includes('rare'), 'card explains why it is held out (the tag)');
  assert(text.includes('weight'), 'card carries the weight');
});

test('renderBoardStatus: the ladder readout has an explainer hovercard with the doc link', () => {
  const node = bs.renderBoardStatus(bs.boardStatusModel(EP_FULL), {});
  const ladder = allByClass(node, 'dn-bs-ladder')[0];
  assert(hovercard.hasHovercard(ladder), 'the ladder panel is hovercard-wired');
  const text = hovercardTextOf(ladder);
  assert(text.toLowerCase().includes('ladder'), 'card explains the ladder');
  assert(text.includes('overfitting design'), 'card carries the doc link');
});

test('renderBoardStatus: a board chip activates the per-board view via onEntry', () => {
  let opened = null;
  const node = bs.renderBoardStatus(bs.boardStatusModel(EP_FULL), { onEntry: (id) => { opened = id; } });
  const chip = allByClass(node, 'dn-bs-chip')[0];
  chip.dispatchEvent({ type: 'click', target: chip });
  assert(opened != null, 'clicking a chip opens its board');
});

test('boardStatusDigest: stable across a no-op repaint, changes on real movement', () => {
  const a = bs.boardStatusDigest(bs.boardStatusModel(EP_FULL));
  const b = bs.boardStatusDigest(bs.boardStatusModel(JSON.parse(JSON.stringify(EP_FULL))));
  assertEqual(a, b, 'identical payload → identical digest (no DOM churn on a no-op beat)');
  const moved = JSON.parse(JSON.stringify(EP_FULL));
  moved.holdout.ladder_budget_remaining = 2;
  assert(bs.boardStatusDigest(bs.boardStatusModel(moved)) !== a, 'a budget spend changes the digest');
});

test('boardStatus: a no-op repaint churns no DOM (digest-gated render discipline)', () => {
  const host = document.createElement('div');
  const model = bs.boardStatusModel(EP_FULL);
  const digest = bs.boardStatusDigest(model);
  ui.gatedSwap(host, digest, () => [bs.renderBoardStatus(model, {})]);
  const first = host.firstChild;
  const writes1 = host.innerHTMLWriteCount();
  // same digest → gatedSwap must NOT rebuild
  ui.gatedSwap(host, digest, () => [bs.renderBoardStatus(model, {})]);
  assert(host.firstChild === first, 'the panel node identity is preserved on a no-op beat');
  assertEqual(host.innerHTMLWriteCount(), writes1, 'no innerHTML writes on the no-op repaint');
});

await run();
