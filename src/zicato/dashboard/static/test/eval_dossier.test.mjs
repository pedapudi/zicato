// test/eval_dossier.test.mjs — the per-entry EVAL DOSSIER on the board view
// (EVAL-VIEW.md §3.2 / §4 / §5).
//
// The dossier mounts BESIDE the live-streaming transcript (ADDITIVE), so these
// tests pin both the section content AND that the transcript render path is
// untouched. Covers:
//   * the champion-spine trajectory figure known-answer (spine-only, round order,
//     NaN for a cell that never ran) + its honest-empty degrade;
//   * the attribution lines + their hrefs into the named candidates' dossiers;
//   * the instrument stat row, including the UNMEASURED flip-rate state (§4 — a
//     null reads "unmeasured", never a fabricated 0.0);
//   * the reflection-finding links (recommend-only) + the empty degrade;
//   * every degrade (found:false cold payload honest-empties every section);
//   * the dossier digest no-op (a re-render with the same payload churns ZERO
//     DOM — the flashing-render bug class);
//   * a PIN that the transcript surface's digest/render path is untouched across
//     a dossier re-render (same xscript node instance + digest);
//   * a PIN that an absent /eval payload (null) mounts NO dossier host, so the
//     board reads byte-identical to before the feature.

import { installDom, test, run, assert, assertEqual, assertDeep } from './harness.mjs';

installDom();

const board = await import('../js/views/board.js');
const router = await import('../js/router.js');
const svg = await import('../js/svg.js');
const { state } = await import('../js/core/state.js');
const data = await import('../js/data.js');

// ---- fixtures --------------------------------------------------------

const DOSSIER_FULL = {
  epoch_id: 'e3', entry_id: 'task_login', found: true,
  slice: 'holdout', tag: 'holdout',
  instrument: {
    flip_rate: 0.2, flip_rate_measured: true, calibration_runs: 5,
    discrimination: 0.75, discrimination_pairs: 4,
    runtime_ms_mean: 41200, runtime_ms_p50: 40100, runtime_ms_max: 61000,
    replicate_total: 12, cached_share: 0.08,
  },
  trajectory: [
    { generation_id: 'g0', round_index: 0, champion_spine: true, drift_loss: 0.62, pass_ratio: 0.0, replicates: 2, cached: false },
    { generation_id: 'g1', round_index: 1, champion_spine: false, drift_loss: 0.90, pass_ratio: 0.0, replicates: 1, cached: false },
    { generation_id: 'g2', round_index: 2, champion_spine: true, drift_loss: 0.31, pass_ratio: 1.0, replicates: 2, cached: false },
    { generation_id: 'g5', round_index: 3, champion_spine: true, drift_loss: null, pass_ratio: null, replicates: 0, cached: false },
  ],
  attribution: { first_passed_by: 'g2', regressed_by: ['g5', 'g8'] },
  reflection_findings: [
    { reflection_id: 'refl_1', finding: { finding_id: 'F1', title: 'task_login flips under paraphrase', severity: 'warn' } },
  ],
};

// The honest cold/unknown-entry degrade — the reader's _empty_dossier shape.
const DOSSIER_EMPTY = {
  epoch_id: 'e3', entry_id: 'coldentry', found: false, slice: 'train', tag: null,
  instrument: {
    flip_rate: null, flip_rate_measured: false, calibration_runs: 0,
    discrimination: null, discrimination_pairs: 0,
    runtime_ms_mean: null, runtime_ms_p50: null, runtime_ms_max: null,
    replicate_total: 0, cached_share: null,
  },
  trajectory: [], attribution: { first_passed_by: null, regressed_by: [] },
  reflection_findings: [], note: 'no such epoch / entry',
};

// ---- DOM helpers -----------------------------------------------------

function ctxReal() { return { navigate() {}, href: router.href }; }

function renderSections(dossier, epochId, entryId) {
  const host = document.createElement('div');
  const nodes = board.evalDossierSections(dossier, ctxReal(), epochId || 'e3', entryId || (dossier && dossier.entry_id));
  for (const n of nodes) host.appendChild(n);
  return host;
}
function allByClass(host, cls) {
  return host.querySelectorAll('[class]').filter((n) =>
    (n.getAttribute('class') || '').split(/\s+/).includes(cls));
}
function allTags(node, tag) {
  const out = [];
  const t = tag.toUpperCase();
  const walk = (n) => { for (const c of (n.children || [])) { if (c.tagName === t) out.push(c); walk(c); } };
  walk(node);
  return out;
}
function textOf(node) {
  let s = '';
  const walk = (n) => { for (const c of (n.childNodes || [])) { if (c.nodeType === 3) s += c.textContent; else walk(c); } };
  walk(node);
  return s;
}
// the value / key of every dn-stat chip, as {value, key} pairs.
function statPairs(host) {
  return allByClass(host, 'dn-stat').map((s) => ({
    value: (allByClass(s, 'v')[0] || {}).textContent || '',
    key: (allByClass(s, 'k')[0] || {}).textContent || '',
  }));
}

// ════════════════════════════════════════════════════════════════════
// 1 — the champion-spine trajectory figure known-answer
// ════════════════════════════════════════════════════════════════════

test('trajectory: trajectorySeries keeps ONLY the champion-spine cells, in round order, NaN for a never-ran cell', () => {
  const s = board.trajectorySeries(DOSSIER_FULL);
  assertDeep(s.gens, ['g0', 'g2', 'g5'], 'g1 (off-spine) is excluded; spine gens kept in round order');
  // this dossier is bool-only (no per-cell score), so the series reads the drift
  // channel. the known answer: g0=0.62, g2=0.31, g5 never ran → NaN (pen lifts).
  assertEqual(s.channel, 'drift', 'an unscored entry falls through to the drift channel');
  assertEqual(s.values[0], 0.62, 'g0 drift loss');
  assertEqual(s.values[1], 0.31, 'g2 drift loss');
  assert(Number.isNaN(s.values[2]), 'g5 never ran → NaN, never a fabricated point');
  assertDeep(s.passRatio.slice(0, 2), [0.0, 1.0], 'pass ratios carry through for the ran cells');
});

test('trajectory: the figure renders the sparkline grammar (a responsive, aspect-locked svg + a line path)', () => {
  const host = renderSections(DOSSIER_FULL);
  const spark = allByClass(host, 'dn-spark')[0];
  assert(spark, 'the trajectory renders a dn-spark sparkline');
  assert((spark.getAttribute('class') || '').includes('dn-spark-hero'), 'responsive → the aspect-locked hero class (house rule)');
  assert(/aspect-ratio:/.test(spark.getAttribute('style') || ''), 'the aspect-ratio is pinned so the none-scale is uniform');
  const path = allByClass(host, 'dn-spark-line')[0];
  assert(path && path.getAttribute('d'), 'the spine drift-loss line is drawn');
});

test('trajectory: a spine with NO ran cell honest-empties (no fabricated figure)', () => {
  const noRun = { ...DOSSIER_FULL, trajectory: [
    { generation_id: 'g0', round_index: 0, champion_spine: true, drift_loss: null, pass_ratio: null, replicates: 0, cached: false },
  ] };
  const host = renderSections(noRun);
  assert(!allByClass(host, 'dn-spark')[0], 'no sparkline when nothing ran');
  assert(allByClass(host, 'dn-empty')[0], 'an honest empty stands in for the figure');
});

// ════════════════════════════════════════════════════════════════════
// 2 — attribution lines + hrefs into the named candidates' dossiers
// ════════════════════════════════════════════════════════════════════

test('attribution: first-passed-by / regressed-by each render one quiet line linking to the candidate dossier', () => {
  const host = renderSections(DOSSIER_FULL, 'e3', 'task_login');
  const rows = allByClass(host, 'dn-eval-attr-row');
  assertEqual(rows.length, 2, 'exactly two attribution rows (first-passed, regressed) — no chip per row');
  const firstRow = rows[0];
  assert(/first passed by/.test(textOf(firstRow)), 'the first row is verdict-led "first passed by"');
  const firstLinks = allTags(firstRow, 'a');
  assertEqual(firstLinks.length, 1, 'first-passed-by links exactly the one gen (g2)');
  assertEqual(firstLinks[0].textContent, 'g2', 'the link names the first-passing candidate');
  assertEqual(firstLinks[0].getAttribute('href'), router.href('candidate', { epochId: 'e3', gen: 'g2' }),
    'the href routes into g2\'s candidate dossier');

  const regRow = rows[1];
  assert(/regressed by/.test(textOf(regRow)), 'the second row is "regressed by"');
  const regLinks = allTags(regRow, 'a').map((a) => a.textContent);
  assertDeep(regLinks, ['g5', 'g8'], 'both regressing spine gens are linked, in order');
  const g8 = allTags(regRow, 'a').find((a) => a.textContent === 'g8');
  assertEqual(g8.getAttribute('href'), router.href('candidate', { epochId: 'e3', gen: 'g8' }), 'g8 links to its own dossier');
});

test('attribution: absent first-passed / no regression degrades to a quiet faint line (never a link, never a lie)', () => {
  const host = renderSections(DOSSIER_EMPTY, 'e3', 'coldentry');
  const rows = allByClass(host, 'dn-eval-attr-row');
  assertEqual(rows.length, 2, 'both rows still render');
  assertEqual(allTags(rows[0], 'a').length, 0, 'no first-passed link when nothing passed');
  assert(/[Nn]o champion-spine generation passed/.test(textOf(rows[0])), 'an honest rationale, not a fabricated gen');
  assertEqual(allTags(rows[1], 'a').length, 0, 'no regression links when nothing regressed');
});

// ════════════════════════════════════════════════════════════════════
// 3 — instrument stat row (incl. the UNMEASURED flip-rate state, §4)
// ════════════════════════════════════════════════════════════════════

test('instrument stats: the measured channel shows flip rate, discrimination, runtimes, replicates, cached share, slice', () => {
  const host = renderSections(DOSSIER_FULL);
  const pairs = statPairs(host);
  const byKeyIncludes = (frag) => pairs.find((p) => p.key.includes(frag));
  assertEqual(byKeyIncludes('flip rate').value, svg.fmt(0.2, 2), 'the measured flip rate reads 0.20');
  assert(byKeyIncludes('flip rate').key.includes('5 draws'), 'the flip-rate key names the calibration draw count');
  assertEqual(byKeyIncludes('discrimination').value, svg.fmt(0.75, 2), 'discrimination reads 0.75');
  assert(byKeyIncludes('discrimination').key.includes('4 pairs'), 'discrimination names its matchup-pair count');
  assertEqual(byKeyIncludes('runtime mean').value, '41.2s', 'runtime mean is human-formatted seconds');
  // The three runtime stats read the console's canonical duration formatter, so
  // an entry whose runs take minutes reads minutes. A local formatter here used
  // to stop at seconds and print a four-minute mean as `250.0s`.
  const slow = renderSections({ ...DOSSIER_FULL,
    instrument: { ...DOSSIER_FULL.instrument, runtime_ms_mean: 250000 } });
  assertEqual(statPairs(slow).find((p) => p.key.includes('runtime mean')).value, '4.2m',
    'a runtime mean past ninety seconds reads in minutes');
  assertEqual(byKeyIncludes('replicates').value, '12', 'the replicate total reads through');
  assertEqual(byKeyIncludes('cached share').value, '8%', 'the cached share reads a percentage');
  assert(pairs.some((p) => p.key.includes('holdout') && p.value === 'holdout'), 'the holdout slice membership shows');
});

test('instrument stats: an UNMEASURED flip rate reads "unmeasured", NEVER 0.0 (§4 no fabricated numbers)', () => {
  const host = renderSections(DOSSIER_EMPTY, 'e3', 'coldentry');
  const pairs = statPairs(host);
  const flip = pairs.find((p) => p.key.includes('flip rate'));
  assert(flip, 'the flip-rate stat is present');
  assertEqual(flip.value, 'unmeasured', 'the value reads the honest "unmeasured" — not 0.0');
  assert(flip.key.includes('unmeasured'), 'the key flags the unmeasured state too');
  // no absent aggregate is fabricated: an em-dash stands in for a null runtime.
  const rtMean = pairs.find((p) => p.key === 'runtime mean');
  assertEqual(rtMean.value, '—', 'a null runtime mean reads an em-dash, not 0');
});

// ════════════════════════════════════════════════════════════════════
// 4 — reflection-finding links (recommend-only) + the empty degrade
// ════════════════════════════════════════════════════════════════════

test('reflection: a finding that names the entry links into the Instrument view; the framing stays recommend-only', () => {
  const host = renderSections(DOSSIER_FULL, 'e3', 'task_login');
  const link = allTags(host, 'a').find((a) => (a.getAttribute('class') || '').includes('dn-instr-link'));
  assert(link, 'the reflection finding is a link into the Instrument view');
  assertEqual(link.textContent, 'task_login flips under paraphrase', 'the link uses the finding title');
  assertEqual(link.getAttribute('href'), router.href('instrument', { epochId: 'e3', reflectionId: 'refl_1' }),
    'the href opens the Instrument view for the finding\'s reflection');
  assert(/recommend-only/.test(textOf(host)), 'the recommend-only framing is present (the word stays with reflect)');
});

test('reflection: no findings honest-empties, pointing at reflect (recommend-only)', () => {
  const host = renderSections(DOSSIER_EMPTY, 'e3', 'coldentry');
  const empties = allByClass(host, 'dn-empty');
  const reflEmpty = empties.find((e) => /reflect/.test(textOf(e)));
  assert(reflEmpty, 'an honest empty names reflect');
  assert(/recommend-only/.test(textOf(reflEmpty)), 'the empty carries the recommend-only framing');
});

// ════════════════════════════════════════════════════════════════════
// 5 — every degrade: the cold found:false payload honest-empties all four
// ════════════════════════════════════════════════════════════════════

test('degrade: the cold (found:false) payload renders all four sections, each honest-empty, with no fabricated number', () => {
  const host = renderSections(DOSSIER_EMPTY, 'e3', 'coldentry');
  // four sections mount (trajectory, instrument, attribution, reflection).
  const sections = allByClass(host, 'dn-section');
  assertEqual(sections.length, 4, 'all four dossier sections mount even on a cold payload');
  // no sparkline (nothing ran), an empty trajectory + reflection, quiet attribution.
  assert(!allByClass(host, 'dn-spark')[0], 'no fabricated trajectory figure');
  assert(allByClass(host, 'dn-eval-attr-row').length === 2, 'attribution rows still render (quiet)');
  // the whole rendered text carries no "0.0" flip rate (the §4 lie we forbid).
  const flip = statPairs(host).find((p) => p.key.includes('flip rate'));
  assert(flip.value !== '0.00' && flip.value !== '0.0' && flip.value !== '0', 'the flip rate is never fabricated as zero');
});

// ════════════════════════════════════════════════════════════════════
// 6 — the dossier DIGEST (no-op gate)
// ════════════════════════════════════════════════════════════════════

test('digest: an identical payload yields a byte-identical digest; a changed field flips it', () => {
  const a = board.evalDossierDigest(DOSSIER_FULL, 'e3', 'task_login');
  const b = board.evalDossierDigest(JSON.parse(JSON.stringify(DOSSIER_FULL)), 'e3', 'task_login');
  assertEqual(a, b, 'a deep-equal payload → the SAME digest (a no-op beat churns nothing)');
  const moved = JSON.parse(JSON.stringify(DOSSIER_FULL));
  moved.instrument.flip_rate = 0.4;
  assert(board.evalDossierDigest(moved, 'e3', 'task_login') !== a, 'a moved flip rate flips the digest');
  // sub-precision jitter below 3dp must NOT flip it (the digest rounds).
  const jitter = JSON.parse(JSON.stringify(DOSSIER_FULL));
  jitter.instrument.discrimination = 0.7500004;
  assertEqual(board.evalDossierDigest(jitter, 'e3', 'task_login'), a, '4th-place jitter does NOT flip the digest');
});

// ════════════════════════════════════════════════════════════════════
// 7 + 8 — full render: no-op preserves the dossier host; a dossier
// re-render leaves the TRANSCRIPT surface untouched; a null payload
// mounts NO host (byte-identical pre-feature board).
// ════════════════════════════════════════════════════════════════════

const STORE = {
  epoch: { epoch_id: 'e3', board: [{ entry_id: 'task_login', kind: 'single_turn', weight: 1, budget_s: 60 }] },
  lineage: { generations: [
    { generation_id: 'g0', parent_generation_id: null, promoted: true, epoch_id: 'e3' },
    { generation_id: 'g2', parent_generation_id: 'g0', promoted: true, epoch_id: 'e3' },
  ] },
  traj: { points: [] },
  perEntry: {
    g0: { entries: [{ entry_id: 'task_login', drift_loss: 0.62, pass_fail: false, run_id: 'r0' }] },
    g2: { entries: [{ entry_id: 'task_login', drift_loss: 0.31, pass_fail: true, run_id: 'r2' }] },
  },
  transcript: { turns: [{ seq: 0, role: 'user', text: 'log in as admin' }, { seq: 1, role: 'agent', text: 'done' }], annotations: [] },
  dossier: DOSSIER_FULL,
};

function installFetch(store) {
  globalThis.fetch = async (path) => {
    const p = String(path);
    let body = {};
    if (p.includes('/judge-roster')) body = store.roster || null;
    else if (p.includes('/eval/')) body = store.dossier;
    else if (p.startsWith('/api/lineage')) body = store.lineage;
    else if (p.startsWith('/api/score-trajectory')) body = store.traj;
    else if (p.startsWith('/api/epoch')) body = store.epoch;
    else if (p.includes('/per-entry')) {
      const m = p.match(/\/api\/generation\/[^/]+\/([^/]+)\/per-entry/);
      body = (m && store.perEntry[m[1]]) || { entries: [] };
    } else if (p.includes('/transcript') || p.includes('/api/conversation')) body = store.transcript;
    return { ok: true, async json() { return body; } };
  };
}

function resetForRender() {
  state.lastSeq = -1;
  state.activeRuns = [];
  data.invalidate();
}

test('render: a no-op re-render (same dossier) preserves the dossier host node + its digest (ZERO DOM)', async () => {
  resetForRender();
  STORE.dossier = DOSSIER_FULL;
  installFetch(STORE);
  const host = document.createElement('div');
  const ctx = ctxReal();
  const params = { epochId: 'e3', entry: 'task_login', gen: 'g2' };
  await board.render(host, ctx, params);
  const dh = host.querySelector(':scope > [data-node="board-dossier"]');
  assert(dh, 'the dossier host mounted beside the transcript');
  const firstNode = dh.firstChild;
  const digest = dh.getAttribute('data-t-digest');
  assert(firstNode && digest, 'the dossier host has content + a digest');
  // the dossier host sits BEFORE the transcript host in DOM order.
  const xh = host.querySelector(':scope > [data-node="board-xscript"]');
  const kids = host.children.map((c) => c.getAttribute('data-node'));
  assert(kids.indexOf('board-dossier') < kids.indexOf('board-xscript'), 'the dossier mounts between the breakdown and the transcript');

  await board.render(host, ctx, params);
  const dh2 = host.querySelector(':scope > [data-node="board-dossier"]');
  assert(dh2 === dh, 'the dossier host node survives a no-op re-render (not re-created)');
  assertEqual(dh2.getAttribute('data-t-digest'), digest, 'the dossier digest is unchanged on a no-op beat');
  assert(dh2.firstChild === firstNode, 'the dossier content node identity is preserved (no flash)');
  assert(xh === host.querySelector(':scope > [data-node="board-xscript"]'), 'the transcript host is likewise stable');
});

test('render PIN: a CHANGED dossier rebuilds ONLY the dossier host — the transcript surface is untouched', async () => {
  resetForRender();
  STORE.dossier = DOSSIER_FULL;
  installFetch(STORE);
  const host = document.createElement('div');
  const ctx = ctxReal();
  const params = { epochId: 'e3', entry: 'task_login', gen: 'g2' };
  await board.render(host, ctx, params);

  const xh = host.querySelector(':scope > [data-node="board-xscript"]');
  assert(xh && xh.firstChild, 'the transcript surface mounted (a selected candidate renders its transcript)');
  const xDigest = xh.getAttribute('data-t-digest');
  const xFirst = xh.firstChild;
  const dh = host.querySelector(':scope > [data-node="board-dossier"]');
  const dDigestBefore = dh.getAttribute('data-t-digest');

  // change ONLY the dossier payload; bust ONLY its cache key, then re-render.
  const moved = JSON.parse(JSON.stringify(DOSSIER_FULL));
  moved.instrument.flip_rate = 0.45;
  moved.attribution.first_passed_by = 'g0';
  STORE.dossier = moved;
  data.invalidate('/api/epoch/e3/eval/');
  await board.render(host, ctx, params);

  // the transcript host: SAME node, SAME digest, SAME first child (untouched).
  const xh2 = host.querySelector(':scope > [data-node="board-xscript"]');
  assert(xh2 === xh, 'the transcript host node is the SAME instance across the dossier re-render');
  assertEqual(xh2.getAttribute('data-t-digest'), xDigest, 'the transcript digest is unchanged (its render path never fired)');
  assert(xh2.firstChild === xFirst, 'the transcript content node identity is preserved');
  // the dossier host DID rebuild (its digest moved with the payload).
  const dh2 = host.querySelector(':scope > [data-node="board-dossier"]');
  assert(dh2.getAttribute('data-t-digest') !== dDigestBefore, 'the dossier digest advanced with the changed payload');
});

test('render PIN: an ABSENT /eval payload (null) mounts NO dossier host — the board reads byte-identical to pre-feature', async () => {
  resetForRender();
  // a pre-feature server: the /eval GET fails → cachedJson degrades to null.
  const store2 = { ...STORE, dossier: null };
  globalThis.fetch = async (path) => {
    const p = String(path);
    if (p.includes('/eval/')) throw new Error('HTTP 404'); // endpoint absent
    return {
      ok: true,
      async json() {
        if (p.startsWith('/api/lineage')) return store2.lineage;
        if (p.startsWith('/api/score-trajectory')) return store2.traj;
        if (p.startsWith('/api/epoch')) return store2.epoch;
        if (p.includes('/per-entry')) {
          const m = p.match(/\/api\/generation\/[^/]+\/([^/]+)\/per-entry/);
          return (m && store2.perEntry[m[1]]) || { entries: [] };
        }
        if (p.includes('/transcript') || p.includes('/api/conversation')) return store2.transcript;
        return {};
      },
    };
  };
  const host = document.createElement('div');
  await board.render(host, ctxReal(), { epochId: 'e3', entry: 'task_login', gen: 'g2' });
  assert(!host.querySelector(':scope > [data-node="board-dossier"]'), 'no dossier host when the payload is absent (byte-identical pre-feature board)');
  // the pre-feature hosts are exactly the two originals.
  const kids = host.children.map((c) => c.getAttribute('data-node'));
  assertDeep(kids, ['board-upper', 'board-xscript'], 'only the two pre-feature hosts mount');
});

test('teardown: reset shared AppState + data cache for the next file', () => {
  state.lastSeq = -1;
  state.activeRuns = [];
  if (typeof data.invalidate === 'function') data.invalidate();
  assert(true, 'shared singletons reset');
});

await run();

// ---- the FACET panel on the per-board drill-down --------------------------
// Drilling into a board entry from a candidate should say which facet slices
// that entry feeds, and how each candidate scores on them. Both halves ride on
// the per-entry payload the view already fetches: the entry ROW names its
// facets, each candidate's `facet_scores` carries that candidate's aggregate.

function storeWithFacets() {
  return {
    ...STORE,
    perEntry: {
      g0: {
        entries: [{ entry_id: 'task_login', drift_loss: 0.62, pass_fail: false, run_id: 'r0',
                    facets: ['auth', 'data_cleaning'] }],
        facet_scores: { facets: {
          auth: { scalar: 1.20, mean_score: 0.10, scored_count: 1, entry_count: 1 },
          data_cleaning: { scalar: 0.90, mean_score: 0.40, scored_count: 2, entry_count: 2 },
        }, overall: null },
      },
      g2: {
        entries: [{ entry_id: 'task_login', drift_loss: 0.31, pass_fail: true, run_id: 'r2',
                    facets: ['auth', 'data_cleaning'] }],
        facet_scores: { facets: {
          auth: { scalar: 0.40, mean_score: 0.90, scored_count: 1, entry_count: 1 },
          data_cleaning: { scalar: 0.55, mean_score: 0.80, scored_count: 2, entry_count: 2 },
        }, overall: null },
      },
    },
  };
}

function facetPanelCells(host) {
  const tables = host.querySelectorAll('[class]').filter((n) =>
    (n.getAttribute('class') || '').split(/\s+/).includes('dn-facet-table'));
  if (!tables.length) return [];
  const out = [];
  for (const part of tables[0].children) {
    for (const tr of part.children) out.push([...tr.children].map((c) => (c.textContent || '').trim()));
  }
  return out;
}

test('board drill-down: the facet panel names the slices this entry feeds, per candidate', async () => {
  resetForRender();
  installFetch(storeWithFacets());
  const host = document.createElement('div');
  await board.render(host, ctxReal(), { epochId: 'e3', entry: 'task_login', gen: 'g2' });

  const cells = facetPanelCells(host);
  // One column per candidate that reported facets, in lineage order.
  // The unit rides on the row header: every cell is that candidate's scalar.
  // The champion is named in its header rather than by weighting its numbers: dimming
  // a column reads as emphasis on the others, which would imply a verdict this
  // table must not carry.
  // CANDIDATES are the rows (the orientation the rest of the page uses, and
  // the one that scales — an epoch grows candidates rather than facets). The champion
  // is named in its cell, never by weighting its numbers.
  assertDeep(cells[0], ['candidate · scalar ↓', 'auth', 'data_cleaning'], 'the unit + a column per facet');
  assertDeep(cells[1], ['g0 ○', '1.20', '0.90'], 'the champion row, marked');
  assertDeep(cells[2], ['g2', '0.40', '0.55'], 'the challenger improved on both slices');
  // The unit header explains itself on hover, keyboard-reachable.
  const tbl = host.querySelectorAll('[class]').filter((n) =>
    (n.getAttribute('class') || '').split(/\s+/).includes('dn-facet-table'))[0];
  const unitHead = tbl.children[0].children[0].children[0];
  assertEqual(unitHead.getAttribute('data-hovercard'), '1', 'the unit header explains the scalar');
  assertEqual(unitHead.getAttribute('tabindex'), '0', 'and is keyboard-reachable');
  // Every number cell carries the SAME class — no column is visually weighted.
  const bodyCells = [...tbl.children[1].children].flatMap((tr) => [...tr.children].slice(1));
  assert(bodyCells.length > 0, 'the table has number cells');
  assert(bodyCells.every((c) => (c.getAttribute('class') || '') === 'dn-num'),
    'no candidate column is dimmed or emphasised');
});

test('board drill-down: the facet panel scales with candidates, not columns', async () => {
  resetForRender();
  // Eight candidates, two facets — the shape a real epoch reaches by round
  // eight. Rows grow, columns do NOT: the table stays three wide.
  const gens = Array.from({ length: 8 }, (_, i) => 'g' + i);
  const perEntry = {};
  for (const g of gens) {
    perEntry[g] = {
      entries: [{ entry_id: 'task_login', drift_loss: 0.3, pass_fail: true, run_id: 'r_' + g,
                  facets: ['auth', 'data_cleaning'] }],
      facet_scores: { facets: {
        auth: { scalar: 1.0, mean_score: 0.5, scored_count: 1, entry_count: 1 },
        data_cleaning: { scalar: 0.5, mean_score: 0.8, scored_count: 1, entry_count: 1 },
      }, overall: null },
    };
  }
  installFetch({
    ...STORE,
    lineage: { generations: gens.map((g, i) => ({
      generation_id: g, parent_generation_id: i ? gens[i - 1] : null,
      promoted: i === 0, epoch_id: 'e3',
    })) },
    perEntry,
  });
  const host = document.createElement('div');
  await board.render(host, ctxReal(), { epochId: 'e3', entry: 'task_login', gen: 'g2' });

  const cells = facetPanelCells(host);
  assertEqual(cells[0].length, 3, 'still three columns wide with eight candidates');
  assertEqual(cells.length, 9, 'one header row + one row per candidate');
  assertDeep(cells[0], ['candidate · scalar ↓', 'auth', 'data_cleaning'], 'columns are the facets');
});

test('board drill-down: no facet panel when the entry carries no facet tags', async () => {
  resetForRender();
  installFetch(STORE);   // the base fixture's rows carry no `facets`.
  const host = document.createElement('div');
  await board.render(host, ctxReal(), { epochId: 'e3', entry: 'task_login', gen: 'g2' });

  assertEqual(facetPanelCells(host).length, 0, 'an untagged entry paints no facet panel');
});

test('board drill-down: a no-op re-render over a facet-bearing entry churns NO DOM', async () => {
  resetForRender();
  installFetch(storeWithFacets());
  const host = document.createElement('div');
  const ctx = ctxReal();
  const params = { epochId: 'e3', entry: 'task_login', gen: 'g2' };
  await board.render(host, ctx, params);
  const upper = host.querySelector(':scope > [data-node="board-upper"]');
  const digest1 = upper.getAttribute('data-t-digest');
  const first = upper.firstChild;
  await board.render(host, ctx, params);
  assertEqual(upper.getAttribute('data-t-digest'), digest1, 'digest unchanged on a no-op beat');
  assert(upper.firstChild === first, 'no clear-and-rebuild on the no-op beat');
});
