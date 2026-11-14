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

// A publication markdown with a WIDE table and a long contract hash in the
// masthead metadata — the overflow risks.
const ANALYSIS_MD = [
  'Zicato improvement campaign · epoch analysis report',
  '',
  '# Publication Fixture',
  '',
  '- **Epoch id**: `' + EPOCH + '`',
  '- **Contract hash**: `' + LONG_HASH + '`',
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

async function renderInto(host) {
  data.invalidate();
  installFixtureMap(F);
  await publication.render(host, ctx, { epochId: EPOCH });
}

// ---- (1) a wide markdown table scrolls inside its OWN dn-table-scroll -----
test('publication: a wide report table scrolls in its OWN container (never the page)', async () => {
  const host = document.createElement('div');
  await renderInto(host);
  const body = allByClass(host, 'dn-md-body')[0];
  assert(body, 'the report Markdown is mounted');
  const scroll = allByClass(body, 'dn-table-scroll')[0];
  assert(scroll, 'the report table sits in a dn-table-scroll box so it scrolls inside itself');
  assert(allByClass(scroll, 'dn-md-table').length === 1, 'the scroll box holds the table');
});

// ---- (2) the live figures follow the report --------------------------------
test('publication: the live figures are appended as contained dn-paper-fig figures', async () => {
  const host = document.createElement('div');
  await renderInto(host);
  assert(allByClass(host, 'dn-paper-fig').length >= 1, 'the lineage figure rendered a dn-paper-fig');
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
  // A figure never widens past its container.
  assert(/\.dn-paper-fig\s*\{[^}]*max-width:\s*100%/.test(css),
    '.dn-paper-fig declares max-width: 100%');
  // The paper column itself never scrolls the page sideways.
  assert(/\.dn-paper\s*\{[^}]*overflow-x:\s*hidden/.test(css),
    '.dn-paper declares overflow-x: hidden');
});

// ---------------------------------------------------------------------------
// The publication view renders the report's Markdown.
// ---------------------------------------------------------------------------

function withAnalysis(extra) {
  return Object.assign({}, F, {
    [`/api/epoch/${EPOCH}/analysis`]: Object.assign({ analysis_md: ANALYSIS_MD }, extra || {}),
  });
}

async function renderWith(host, fixtures) {
  data.invalidate();
  installFixtureMap(fixtures);
  await publication.render(host, ctx, { epochId: EPOCH });
}

test('publication: fetches the report from the dedicated analysis endpoint', async () => {
  const host = document.createElement('div');
  data.invalidate();
  installFixtureMap({ ...withAnalysis(), '/api/epoch': { epoch_id: EPOCH } });
  const requests = [];
  const fetch = globalThis.fetch;
  globalThis.fetch = (...args) => {
    requests.push(String(args[0]));
    return fetch(...args);
  };
  try {
    await publication.render(host, ctx, {});
  } finally {
    globalThis.fetch = fetch;
  }
  assert(requests.includes('/api/epoch'), 'the epoch response resolves the publication epoch');
  assert(requests.includes(`/api/epoch/${EPOCH}/analysis`), 'the dedicated endpoint supplies the paper');
  const bodies = allByClass(host, 'dn-md-body');
  assertEqual(bodies.length, 1, 'the report is mounted exactly once');
  assert(bodies[0].textContent.includes('Publication Fixture'), 'the report Markdown is what painted');
  // the report is built as DOM nodes: the Markdown is never written as HTML.
  assertEqual(host.innerHTMLWriteCount(), 0, 'no innerHTML write landed anywhere in the view');
  // the LIVE figures still ride along — they are what the report text cannot carry.
  assert(allByClass(host, 'dn-paper-fig').length >= 1, 'the live interactive figures are still appended');
});

test('publication: an unpublished report retains interactive figures', async () => {
  const host = document.createElement('div');
  await renderWith(host, withAnalysis({ analysis_md: '   ' }));
  assertEqual(allByClass(host, 'dn-md-body').length, 0, 'no report body when the server produced none');
  assert(host.textContent.includes('has not been published'), 'the missing report is explicit');
  assert(allByClass(host, 'dn-paper-fig').length >= 1, 'interactive figures remain available');
});

test('publication (A8): analysis_md is FOLDED into the digest (a regenerated report repaints)', async () => {
  const host = document.createElement('div');
  await renderWith(host, withAnalysis());
  const first = host.getAttribute('data-t-digest');
  const firstNode = host.firstChild;

  // a no-op: identical payload → zero DOM.
  await renderWith(host, withAnalysis());
  assertEqual(host.getAttribute('data-t-digest'), first, 'an identical report is a digest no-op');
  assert(host.firstChild === firstNode, 'the article node survives a no-op re-render');

  // a regenerated report with different text must repaint.
  await renderWith(host, withAnalysis({
    analysis_md: ANALYSIS_MD.replace('Publication Fixture', 'Edited Fixture Title'),
  }));
  assert(host.getAttribute('data-t-digest') !== first, 'a regenerated report flips the digest');
  assert(host.firstChild !== firstNode, 'the article was rebuilt (the new report painted)');
  assert(host.textContent.includes('Edited Fixture Title'), 'the regenerated report is what painted');
});

// ---------------------------------------------------------------------------
// (6) the continuous-score entry_grid contract is honoured.
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
  // digest folding only the original four fields would not repaint.
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

test('publication: a report correction of equal length redraws the report', async () => {
  const host = document.createElement('div');
  await renderWith(host, F);
  const first = host.firstChild;
  const corrected = ANALYSIS_MD.replace('Keep the clause.', 'Drop the clause.');
  assertEqual(corrected.length, ANALYSIS_MD.length, 'the correction preserves text length');
  const revised = {...F, [`/api/epoch/${EPOCH}/analysis`]: {analysis_md: corrected}};
  await renderWith(host, revised);
  assert(host.firstChild !== first, 'a changed report redraws the article');
  const updated = host.firstChild;
  await renderWith(host, revised);
  assert(host.firstChild === updated, 'an unchanged report preserves the article');
});

test('markdown: a link is emitted only for a scheme that cannot run script', () => {
  const host = document.createElement('div');
  host.appendChild(ui.renderMarkdown([
    '[ok](https://example.com/a) [rel](/api/x) [frag](#top)',
    '',
    '[bad](javascript:alert`1`) [worse](JavaScript:alert(1%29) [data](data:text/html,x)',
    '',
    '| cell |',
    '| --- |',
    '| [cellbad](javascript:alert`1`) |',
  ].join('\n')));
  const links = allByClass(host, 'dn-md-link');
  const hrefs = links.map((a) => a.getAttribute('href'));
  assertEqual(JSON.stringify(hrefs), JSON.stringify(['https://example.com/a', '/api/x', '#top']),
    'only http(s), mailto, fragment and root-relative links become anchors');
  assert(host.textContent.includes('[bad](javascript:alert`1`)'), 'an unsafe link stays literal text');
  assert(host.textContent.includes('[cellbad](javascript:'), 'an unsafe link in a table cell stays literal text');
  for (const a of links) assertEqual(a.getAttribute('rel'), 'noopener noreferrer');
});
await run();
