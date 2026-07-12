// test/publication.test.mjs — the ACM epoch-publication tab: the
// never-overflow rendering contract and the digest no-op.
//
// The operator complaint the overhaul answers is "it spills out of the page
// a lot". These tests pin the house rule (CONSOLE-DESIGN-LANGUAGE.md §wide
// content): every wide element scrolls inside its OWN container, the page
// never scrolls sideways, and a regenerated-but-identical analysis.md rebuilds
// ZERO DOM (no flashing refresh).

import { installDom, test, run, assert } from './harness.mjs';

installDom();

const { data, router, ui, allByClass, readCss, installFixtureMap } = await import('./fixtures.mjs');
const publication = await import('../js/views/publication.js');

const EPOCH = '2026-07-12_pub';
const LONG_HASH = 'feedfacecafebabe0123456789abcdef0123456789abcdefdeadbeefc0ffee00';

// A publication markdown with a WIDE table, a figure marker, and a long
// contract hash in the masthead metadata — the three overflow risks.
const ANALYSIS_MD = [
  '<!-- EYEBROW -->',
  'Zicato improvement campaign · epoch analysis report',
  '',
  '# Publication Fixture',
  '',
  '<!-- META -->',
  '**Epoch id**: `' + EPOCH + '`  ',
  '**Contract hash**: `' + LONG_HASH + '`',
  '',
  '## Abstract',
  '',
  'This campaign cut off-topic drift.',
  '',
  '## Methodology',
  '',
  '| mutation id | kind | file |',
  '| --- | --- | --- |',
  '| `sys_prompt` | prompt_text | `/very/long/absolute/path/agent/prompt.txt` |',
  '',
  '<!-- FIGURE:lineage -->',
  '',
  '## Conclusion & Next Directions',
  '',
  'Keep the clause.',
  '',
].join('\n');

const F = {
  [`/api/epoch/${EPOCH}/analysis`]: { analysis_md: ANALYSIS_MD },
  [`/api/lineage?epoch=${EPOCH}`]: {
    generations: [
      { generation_id: 'v0', epoch_id: EPOCH, parent_generation_id: '', promoted: true },
      { generation_id: 'v1', epoch_id: EPOCH, parent_generation_id: 'v0', promoted: true },
    ],
  },
  [`/api/score-trajectory?epoch=${EPOCH}`]: { points: [
    { generation_id: 'v0', scalar: 0.8 }, { generation_id: 'v1', scalar: 0.6 },
  ] },
  [`/api/tournaments?epoch=${EPOCH}`]: { matchups: [] },
};

const ctx = { navigate() {}, href: router.href };

// The harness querySelectorAll only matches attribute selectors, so find a
// descendant by tag name (e.g. TABLE) with a small walk.
function hasTag(node, tag) {
  const want = tag.toUpperCase();
  const walk = (n) => {
    for (const c of n.children) {
      if (c.tagName === want) return true;
      if (walk(c)) return true;
    }
    return false;
  };
  return walk(node);
}

async function renderInto(host) {
  data.invalidate();
  installFixtureMap(F);
  await publication.render(host, ctx, { epochId: EPOCH });
}

// ---- (1) a wide markdown table scrolls inside its OWN dn-table-scroll -----
test('publication: a body table is wrapped in dn-table-scroll', async () => {
  const host = document.createElement('div');
  await renderInto(host);
  const scrollers = allByClass(host, 'dn-table-scroll');
  assert(scrollers.length >= 1, 'at least one dn-table-scroll wrapper is present');
  const wrapsTable = scrollers.some((s) => hasTag(s, 'table'));
  assert(wrapsTable, 'a wide markdown table is wrapped by an overflow-x scroller');
});

// ---- (2) a figure marker splices a contained dn-paper-fig ------------------
test('publication: a FIGURE marker splices a dn-paper-fig figure', async () => {
  const host = document.createElement('div');
  await renderInto(host);
  assert(allByClass(host, 'dn-paper-fig').length >= 1, 'the lineage figure marker rendered a dn-paper-fig');
});

// ---- (3) the digest gate: an identical re-render rebuilds ZERO DOM ---------
test('publication: an identical re-render is a digest no-op (no flash)', async () => {
  const host = document.createElement('div');
  await renderInto(host);
  const first = host.firstChild;
  assert(first, 'the article painted on the first render');
  const beforeDigest = host.getAttribute('data-t-digest');
  await renderInto(host);
  assert(host.firstChild === first, 'the article node is the SAME instance — gatedSwap did not rebuild');
  assert(host.getAttribute('data-t-digest') === beforeDigest, 'the digest is unchanged across an identical re-render');
});

// ---- (4) the CSS contract: the overflow guards are declared ---------------
test('publication: the CSS pins the never-overflow guards', () => {
  const css = readCss();
  // Wide tables scroll inside their own box.
  assert(/\.dn-table-scroll\s*\{[^}]*overflow-x:\s*auto/.test(css),
    '.dn-table-scroll declares overflow-x: auto');
  // A long masthead value (contract hash) breaks inside its grid cell.
  assert(/\.dn-paper-meta-value\s*\{[^}]*overflow-wrap:\s*anywhere/.test(css),
    '.dn-paper-meta-value declares overflow-wrap: anywhere');
  // A figure never widens past its container.
  assert(/\.dn-paper-fig\s*\{[^}]*max-width:\s*100%/.test(css),
    '.dn-paper-fig declares max-width: 100%');
  // The paper column itself never scrolls the page sideways.
  assert(/\.dn-paper\s*\{[^}]*overflow-x:\s*hidden/.test(css),
    '.dn-paper declares overflow-x: hidden');
});

await run();
