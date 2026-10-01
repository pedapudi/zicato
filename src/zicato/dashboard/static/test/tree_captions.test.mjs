// test/tree_captions.test.mjs — a tree row's caption yields before its name.
//
// A row is the mark, the name, the live pulse and the caption (a branch's
// count or gate outcome, a leaf's role word). When the rail is short of room
// the caption truncates with an ellipsis first; the name truncates only once
// the caption is gone. A round row therefore reads "Round 0" beside
// "v0 defends · ↑ v2 promo…", never "Roun…" beside the full caption. The full
// caption stays in the row button's accessible name and in a hovercard.

import { installDom, test, run, assert, assertEqual } from './harness.mjs';

installDom();

const { readCss, allByClass, router, tree, hovercardTextOf } = await import('./fixtures.mjs');
const hovercard = await import('../js/hovercard.js');

// The declarations of the rules whose selector list holds `selector` exactly.
function decls(selector) {
  const src = readCss().replace(/\/\*[\s\S]*?\*\//g, '');
  const out = {};
  for (const m of src.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    const sels = m[1].split(';').pop().split(',').map((s) => s.trim());
    if (!sels.includes(selector)) continue;
    for (const d of m[2].split(';')) {
      const i = d.indexOf(':');
      if (i > 0) out[d.slice(0, i).trim()] = d.slice(i + 1).trim().replace(/\s+/g, ' ');
    }
  }
  return out;
}

test('a row lays out so the name column fills before the caption column gets any room', () => {
  const label = decls('.dt-label');
  assertEqual(label.display, 'grid', 'a row label is a grid');
  const tracks = label['grid-template-columns'].match(/minmax\([^)]*\)|[^\s]+/g);
  assertEqual(tracks.length, 4, 'four columns: mark, name, pulse, caption (' + label['grid-template-columns'] + ')');
  // an intrinsic max-content track is maximised before a flexible track is
  // expanded, so the name reaches its full width before the caption grows.
  assertEqual(tracks[1], 'minmax(0, max-content)', 'the name column grows to the full name and may shrink to 0');
  assertEqual(tracks[3], 'minmax(0, 1fr)', 'the caption column takes only the room left after the name');

  const name = decls('.dt-text');
  assertEqual(name['grid-column'], '2', 'the name sits in the name column');
  for (const cap of ['.dt-sub', '.dt-role']) {
    const d = decls(cap);
    assertEqual(d['grid-column'], '4', cap + ' sits in the caption column');
    assertEqual(d['white-space'], 'nowrap', cap + ' holds one line');
    assertEqual(d.overflow, 'hidden', cap + ' clips to its column');
    assertEqual(d['text-overflow'], 'ellipsis', cap + ' truncates with an ellipsis');
    assertEqual(d['min-width'], '0', cap + ' may shrink to nothing');
  }
});

const EP = '2026-09-27_e0';
function buildRoundTree(route) {
  const host = document.createElement('div');
  const model = {
    epochs: [{ id: EP, current: true }],
    byEpoch: { [EP]: {
      gens: [
        { id: 'v0', parent: null, promoted: true, round_index: 0 },
        { id: 'v1', parent: 'v0', promoted: false, round_index: 0 },
        { id: 'v2', parent: 'v0', promoted: true, round_index: 1, currentChampion: true },
      ],
      rounds: [
        { round_index: 0, championId: 'v0', challengers: [{ id: 'v1' }], gateOutcome: { kind: 'held' } },
        { round_index: 1, championId: 'v0', championEvalMode: 'fast', challengers: [{ id: 'v2' }],
          gateOutcome: { kind: 'promoted', gen: 'v2' } },
      ],
      boards: [{ id: 'conv_body', kindTag: '1-turn' }],
    } },
  };
  const toggles = new Set(['e:' + EP, 'e:' + EP + '/gens', 'e:' + EP + '/gens/r1', 'e:' + EP + '/boards']);
  tree.buildTree(host, model, router.parseRoute(route), toggles, { navigate() {}, href: router.href }, () => {});
  return host;
}

test('every row with a caption carries its full text in the button name and a hovercard', () => {
  const host = buildRoundTree('#/e/' + EP);
  const rows = allByClass(host, 'dt-node');
  const captioned = rows.filter((r) => allByClass(r, 'dt-sub').length || allByClass(r, 'dt-role').length);
  const kinds = new Set(captioned.map((r) => r.getAttribute('data-kind')));
  for (const k of ['env', 'epoch', 'group', 'round', 'gen-carried', 'gen-champ', 'board']) {
    assert(kinds.has(k), 'a ' + k + ' row carries a caption in this tree');
  }
  for (const row of captioned) {
    const cap = allByClass(row, 'dt-sub')[0] || allByClass(row, 'dt-role')[0];
    const name = allByClass(row, 'dt-text')[0].textContent;
    const button = allByClass(row, 'dt-label')[0];
    assert(button.textContent.includes(name) && button.textContent.includes(cap.textContent),
      row.getAttribute('data-kind') + ': the button text holds the name and the full caption');
    assert(hovercard.hasHovercard(cap), row.getAttribute('data-kind') + ': the caption shows a hovercard');
    const kind = row.getAttribute('data-kind');
    assertEqual(cap.getAttribute('tabindex'), null, kind + ': the caption inside the button is not focusable');
    assertEqual(cap.getAttribute('aria-describedby'), null, kind + ': the button\'s accessible name carries the caption');
    assert(!(cap._listeners.focus || []).length, kind + ': only the pointer opens the caption\'s card');
  }
});

test('a round row keeps "Round 1" whole and its hovercard reads the full gate caption', () => {
  const host = buildRoundTree('#/e/' + EP);
  const round = allByClass(host, 'dt-node').find((r) => r.getAttribute('data-kind') === 'round'
    && allByClass(r, 'dt-text')[0].textContent === 'Round 1');
  assert(round, 'the Round 1 row renders with its full name');
  const cap = allByClass(round, 'dt-sub')[0];
  const card = hovercardTextOf(cap).replace(/\s+/g, ' ');
  assertEqual(card, 'Round 1 · v0 defends · v2 promoted', 'the hovercard names the row and the whole caption');
  assert(cap.querySelector('[data-icon="up"]'), 'the caption keeps its drawn promotion mark');
});

await run();
