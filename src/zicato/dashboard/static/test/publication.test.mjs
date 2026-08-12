// test/publication.test.mjs — the ACM epoch-publication tab: the
// never-overflow rendering contract and the digest no-op.
//
// The operator complaint the overhaul answers is "it spills out of the page
// a lot". These tests pin the house rule (CONSOLE-DESIGN-LANGUAGE.md §wide
// content): every wide element scrolls inside its OWN container, the page
// never scrolls sideways, and a regenerated-but-identical analysis.md rebuilds
// ZERO DOM (no flashing refresh).

import { installDom, test, run, assert, assertEqual } from './harness.mjs';

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

function hasClass(node, cls) {
  const c = (node && node.getAttribute && node.getAttribute('class')) || '';
  return c.split(/\s+/).includes(cls);
}

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

// ---------------------------------------------------------------------------
// (5) A8 — the SERVER-RENDERED paper is preferred over re-rendering markdown.
//
// /api/epoch/{id}/analysis runs the full report renderer on every call to
// produce `analysis_html_inline`. The view used to read ONLY `analysis_md` and
// re-render it client-side, throwing that render away.
// ---------------------------------------------------------------------------

const SERVED_HTML = '<style>.paper{color:red}</style>'
  + '<article class="paper paper-card" data-epoch="' + EPOCH + '">'
  + '<div class="paper-article"><h1>Served Paper Title</h1>'
  + '<p>Rendered by the server report renderer.</p></div></article>';

function withServedHtml(extra) {
  return Object.assign({}, F, {
    [`/api/epoch/${EPOCH}/analysis`]: Object.assign(
      { analysis_md: ANALYSIS_MD, analysis_html_inline: SERVED_HTML },
      extra || {},
    ),
  });
}

async function renderWith(host, fixtures) {
  data.invalidate();
  installFixtureMap(fixtures);
  await publication.render(host, ctx, { epochId: EPOCH });
}

test('publication (A8): a non-empty analysis_html_inline is PREFERRED over the markdown re-render', async () => {
  const host = document.createElement('div');
  await renderWith(host, withServedHtml());
  const served = allByClass(host, 'dn-paper-served')[0];
  assert(served, 'the server fragment is mounted');
  // the harness does not parse innerHTML (it flags the write instead), so the
  // proof the SERVER html is what painted is that exactly one innerHTML write
  // landed on the fragment host — el({html}) is the only path that writes it.
  assertEqual(served.innerHTMLWriteCount(), 1, 'the served HTML fragment was written into its host');
  // the client-side markdown masthead must NOT also render (no double paper).
  assertEqual(allByClass(host, 'dn-paper-masthead').length, 0,
    'the client markdown masthead is not painted when the server render exists');
  // the LIVE figures still ride along — they are what the static fragment cannot carry.
  assert(allByClass(host, 'dn-paper-fig').length >= 1, 'the live interactive figures are still appended');
});

test('publication (A8): the served fragment scrolls in its OWN container (never the page)', async () => {
  const host = document.createElement('div');
  await renderWith(host, withServedHtml());
  const served = allByClass(host, 'dn-paper-served')[0];
  assert(hasClass(served, 'dn-table-scroll'),
    'the fragment host carries dn-table-scroll so a wide server table scrolls inside itself');
});

test('publication (A8): an EMPTY analysis_html_inline falls back to the markdown path', async () => {
  const host = document.createElement('div');
  await renderWith(host, withServedHtml({ analysis_html_inline: '   ' }));
  assertEqual(allByClass(host, 'dn-paper-served').length, 0, 'no served fragment when the server produced none');
  assertEqual(allByClass(host, 'dn-paper-masthead').length, 1, 'the markdown path paints its masthead');
  assert(host.textContent.includes('Publication Fixture'), 'the markdown title renders');
});

test('publication (A8): analysis_html_inline is FOLDED into the digest (a re-render of the paper repaints)', async () => {
  const host = document.createElement('div');
  await renderWith(host, withServedHtml());
  const first = host.getAttribute('data-t-digest');
  const firstNode = host.firstChild;

  // a no-op: identical payload → zero DOM.
  await renderWith(host, withServedHtml());
  assertEqual(host.getAttribute('data-t-digest'), first, 'an identical served render is a digest no-op');
  assert(host.firstChild === firstNode, 'the article node survives a no-op re-render');

  // the SAME markdown, a DIFFERENT server render — the exact case a digest
  // blind to analysis_html_inline would refuse to repaint.
  await renderWith(host, withServedHtml({
    analysis_html_inline: SERVED_HTML.replace('Served Paper Title', 'Regenerated Paper Title'),
  }));
  assert(host.getAttribute('data-t-digest') !== first,
    'a re-rendered server paper (same markdown) flips the digest');
  assert(host.firstChild !== firstNode, 'the article was rebuilt (the new server paper painted)');
});

// ---------------------------------------------------------------------------
// (6) A10 — the #18 continuous-score entry_grid contract is honoured.
//
// tournament_view.build_matchup_grid serves parent_score / child_score /
// parent_metrics / child_metrics / won_by / parent_session_id /
// child_session_id; the per-match-up table read only the drift-loss pair.
// ---------------------------------------------------------------------------

const GRID_EPOCH = EPOCH;
const SCORED_F = Object.assign({}, F, {
  [`/api/tournaments?epoch=${GRID_EPOCH}`]: { matchups: [{ champion: 'v0', challenger: 'v1', decision: 'promoted' }] },
  [`/api/matchup-grid/${GRID_EPOCH}/v0/v1`]: {
    epoch_id: GRID_EPOCH, champion: 'v0', challenger: 'v1', source: 'loss_files',
    entry_grid: [
      { entry_id: 'waffles', parent_drift_loss: 60.5, child_drift_loss: 40.25,
        parent_pass: false, child_pass: true,
        parent_score: 0.41, child_score: 0.87,
        parent_metrics: { precision: 0.5, recall: 0.33 },
        child_metrics: { precision: 0.9, recall: 0.8 },
        delta: -20.25, verdict: 'improved', won_by: 'v1',
        parent_session_id: 'sess-parent-1', child_session_id: 'sess-child-1' },
      { entry_id: 'picky', parent_drift_loss: 10, child_drift_loss: 12,
        parent_score: null, child_score: null, parent_metrics: null, child_metrics: null,
        delta: 2, verdict: 'regressed', won_by: 'v0' },
    ],
  },
});

test('publication (A10): the per-match-up table renders the continuous score pair + precision/recall', async () => {
  const host = document.createElement('div');
  await renderWith(host, SCORED_F);
  const txt = host.textContent;
  assert(txt.includes('score v0') && txt.includes('score v1'), 'both score columns are headed by their generation');
  assert(txt.includes('0.41') && txt.includes('0.87'), 'both continuous scores render');
  assert(txt.includes('P 0.90 / R 0.80'), 'the challenger precision/recall decomposition renders');
});

test('publication (A10): `won_by` is rendered as the server called it (never re-derived)', async () => {
  const host = document.createElement('div');
  await renderWith(host, SCORED_F);
  const txt = host.textContent;
  assert(txt.includes('won by'), 'the won-by column is headed');
  // the challenger won the first board and the champion the second — both named.
  assert(/v1/.test(txt) && /v0/.test(txt), 'both winners are named');
});

test('publication (A10): a bool-only grid grows NO score columns (back-compat)', async () => {
  const plain = JSON.parse(JSON.stringify(SCORED_F[`/api/matchup-grid/${GRID_EPOCH}/v0/v1`]));
  for (const r of plain.entry_grid) {
    r.parent_score = null; r.child_score = null; r.parent_metrics = null; r.child_metrics = null;
    delete r.parent_session_id; delete r.child_session_id;
  }
  const host = document.createElement('div');
  await renderWith(host, Object.assign({}, SCORED_F, { [`/api/matchup-grid/${GRID_EPOCH}/v0/v1`]: plain }));
  const txt = host.textContent;
  assert(!txt.includes('score v0'), 'no score columns when nothing on the grid was scored');
  assert(!txt.includes('precision / recall'), 'no metrics column when no scorer exposed one');
  assertEqual(allByClass(host, 'dn-paper-trace').length, 0,
    'no harmonograf trace cell when no session ids were recorded');
});

test('publication (A10): the score / metrics / won_by / session fields are FOLDED into the digest', async () => {
  const host = document.createElement('div');
  await renderWith(host, SCORED_F);
  const first = host.getAttribute('data-t-digest');

  // move ONLY the child score — every drift loss and verdict stays equal. A
  // digest folding only the old four fields would not repaint.
  const moved = JSON.parse(JSON.stringify(SCORED_F));
  moved[`/api/matchup-grid/${GRID_EPOCH}/v0/v1`].entry_grid[0].child_score = 0.95;
  await renderWith(host, moved);
  assert(host.getAttribute('data-t-digest') !== first, 'a moved continuous score flips the digest');

  // and `won_by` changing hands alone must flip it too.
  const handed = JSON.parse(JSON.stringify(SCORED_F));
  handed[`/api/matchup-grid/${GRID_EPOCH}/v0/v1`].entry_grid[0].won_by = 'v0';
  const host2 = document.createElement('div');
  await renderWith(host2, SCORED_F);
  const base2 = host2.getAttribute('data-t-digest');
  await renderWith(host2, handed);
  assert(host2.getAttribute('data-t-digest') !== base2, 'won_by changing hands flips the digest');
});

await run();
